"""Tests for MCP tool entry points in contexer/server.py.

share_decision is the one MCP tool that reaches the network from inside FastMCP's event
loop. It AWAITS the async-native share path (share_ids_async), so a slow/hung cloud never
freezes the loop AND a wedged push is cancelled at the deadline (nothing lingers). No
pytest-asyncio: the async tool is driven with asyncio.run(...), matching test_remote.py.
"""
import asyncio
import inspect
import json
import os
import subprocess
import sys
import threading
import uuid
from datetime import datetime, timezone

import pytest

import contexer.share as share_mod
import contexer.share_status as share_status
from contexer import config as _config_mod
from contexer import review
from contexer import server, store


def test_share_decision_is_async_tool():
    # Guards the fix: a regression back to a sync `def` would reintroduce the
    # asyncio.run-in-a-running-loop failure the MCP path hit.
    assert inspect.iscoroutinefunction(server.share_decision)


def test_share_decision_short_circuits_without_repo(monkeypatch):
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: "")
    called = {"share": False}
    monkeypatch.setattr(share_mod, "share", lambda *a, **k: called.__setitem__("share", True))
    result = asyncio.run(server.share_decision("d", ""))
    assert result == "Skipped - repo path not detected."
    assert called["share"] is False  # never reaches the network layer


def test_share_decision_awaits_async_share_path_on_the_loop(monkeypatch):
    # The async share path is AWAITED on the event loop (no thread offload), and its args +
    # return value pass through unchanged - a single id becomes a one-element selection.
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: "/repo/x")
    seen = {}

    async def fake_share_ids(repo, ids, **kw):
        seen["thread"] = threading.current_thread()
        seen["args"] = (repo, ids)
        return share_status.ShareStatus(
            share_status.SYNCED, sent=1, total=1, server_id="srv-1")

    monkeypatch.setattr(share_mod, "share_ids_async", fake_share_ids)

    async def driver():
        seen["loop_thread"] = threading.current_thread()
        return await server.share_decision("dec-42", "/repo/x", confirm=True)

    result = asyncio.run(driver())
    assert "srv-1" in result          # the tool answers a model, so it renders the status
    assert seen["args"] == ("/repo/x", ["dec-42"])
    assert seen["thread"] is seen["loop_thread"]  # awaited on the loop, not offloaded to a thread


def test_share_decision_does_not_block_the_event_loop(monkeypatch):
    # While the awaited share path is in-flight, the loop must stay free to make progress on
    # other coroutines. If share_decision froze the loop, no tick could land before it finished.
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: "/repo")
    order = []

    async def slow_share_ids(repo, ids, **kw):
        await asyncio.sleep(0.2)  # stands in for a slow network round-trip
        order.append("share_done")
        return share_status.ShareStatus(share_status.SYNCED, sent=1, total=1)

    monkeypatch.setattr(share_mod, "share_ids_async", slow_share_ids)

    async def ticker():
        for _ in range(5):
            await asyncio.sleep(0.02)
            order.append("tick")

    async def driver():
        await asyncio.gather(server.share_decision("d", "/repo", confirm=True), ticker())

    asyncio.run(driver())
    assert "tick" in order
    assert order.index("tick") < order.index("share_done")


def test_share_decision_cancels_wedged_push_on_timeout(monkeypatch):
    # #108: the push is AWAITED, so the deadline CANCELS it. The cancellation reaches the
    # push coroutine (no leaked worker/socket, unlike the old thread offload), and the tool
    # returns the local-first degradation message.
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(server, "_SHARE_TIMEOUT", 0.03)
    state = {"started": False, "cancelled": False}

    async def wedged_share_ids(repo, ids, **kw):
        state["started"] = True
        try:
            await asyncio.sleep(3600)  # a cloud that never answers
        except asyncio.CancelledError:
            state["cancelled"] = True  # the awaited push is torn down at the deadline
            raise
        return "late"

    monkeypatch.setattr(share_mod, "share_ids_async", wedged_share_ids)
    monkeypatch.setattr(share_mod, "enqueue_ids_for_retry", lambda repo, ids, **k: len(ids))
    result = asyncio.run(server.share_decision("d", "/repo", confirm=True))
    assert state["started"] is True
    assert state["cancelled"] is True   # THE fix: cancelled, not left running in the background
    assert "Saved locally" in result and "did not respond" in result


def test_share_decision_timeout_enqueues_selection_for_retry(monkeypatch):
    # Greptile #1: cancellation bypasses share_async's own enqueue, so the timeout handler
    # must queue the selection itself - otherwise "the outbox retries it" is a false promise.
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(server, "_SHARE_TIMEOUT", 0.03)

    async def wedged(repo, ids, **kw):
        await asyncio.sleep(3600)

    monkeypatch.setattr(share_mod, "share_ids_async", wedged)
    enqueued = {}

    def fake_enqueue(repo, ids, **k):
        enqueued["repo"] = repo
        enqueued["ids"] = ids
        return len(ids)

    monkeypatch.setattr(share_mod, "enqueue_ids_for_retry", fake_enqueue)
    result = asyncio.run(server.share_decision("ab12,cd34", "/repo", confirm=True))
    assert enqueued["ids"] == ["ab12", "cd34"]  # the parsed selection was queued for retry
    assert enqueued["repo"] == "/repo"
    assert "outbox retries it" in result


def _wedged_share(monkeypatch, enqueue):
    """A share that never returns, so the timeout branch runs, with `enqueue` standing in for
    the queue-for-retry step."""
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(server, "_SHARE_TIMEOUT", 0.03)

    async def wedged(repo, ids, **kw):
        await asyncio.sleep(3600)

    monkeypatch.setattr(share_mod, "share_ids_async", wedged)
    monkeypatch.setattr(share_mod, "enqueue_ids_for_retry", enqueue)


def test_timeout_does_not_promise_a_retry_when_the_queue_refused(monkeypatch):
    """`_enqueue_unlocked` refuses rather than overwriting an unreadable outbox, so the queue-for-
    retry step can raise. The message must then NOT claim an automatic retry, which is the standard
    share._finish_share keeps for its own failure branch."""
    def refuses(repo, ids, **k):
        raise RuntimeError("cannot read the share retry queue: JSONDecodeError")

    _wedged_share(monkeypatch, refuses)
    result = asyncio.run(server.share_decision("ab12", "/repo", confirm=True))
    assert "could NOT be queued" in result
    assert "outbox retries it" not in result
    assert "your local decision is unchanged" in result.lower()


def test_timeout_does_not_promise_a_retry_when_nothing_resolved(monkeypatch):
    """Queuing records nothing in two ways, not one: a refusal, and no id resolving to a
    shareable decision. Branching on the returned count covers both; branching on whether the
    call raised would only have covered the first."""
    _wedged_share(monkeypatch, lambda repo, ids, **k: 0)
    result = asyncio.run(server.share_decision("ab12", "/repo", confirm=True))
    assert "could NOT be queued" in result
    assert "outbox retries it" not in result


# ── cloud-push preview gate + review_pending ─────────────────────────────────────


def test_share_decision_previews_by_default_without_pushing(monkeypatch):
    # confirm=False (default) + skip_confirm off -> preview only, NOTHING is pushed.
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: "/repo")
    # Team-configured + authenticated: the preview gate only fires when a push could actually happen.
    # Patched on share.py's own bare-name binding (`from contexer.config import load_profile`),
    # since share_decision_flow now owns the preview gate and reads that binding, not
    # contexer.config's module attribute.
    monkeypatch.setattr(share_mod, "load_profile",
                        lambda *a, **k: _config_mod.Profile(mode="team", endpoint="https://x/mcp"))
    monkeypatch.setattr("contexer.remote.RemoteStore.from_profile", lambda p: object())
    monkeypatch.setattr(server.store, "format_share_preview", lambda r, d, profile=None: "PREVIEW-TEXT")
    pushed = {"n": 0}

    async def counting_share_ids(*a, **k):
        pushed["n"] += 1
        return "pushed"

    monkeypatch.setattr(share_mod, "share_ids_async", counting_share_ids)

    result = asyncio.run(server.share_decision("ab12cd34", "/repo"))
    assert result == "PREVIEW-TEXT"
    assert pushed["n"] == 0  # dry run - nothing left the machine


def test_share_decision_skip_confirm_pushes_without_preview(monkeypatch):
    # A developer who set skip_confirm bypasses the preview even with confirm=False.
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(share_mod, "load_profile",
                        lambda *a, **k: _config_mod.Profile(skip_confirm=True))

    async def fake_share_ids(repo, ids, **k):
        return share_status.ShareStatus(share_status.SYNCED, sent=1, total=1, server_id="srv-1")

    monkeypatch.setattr(share_mod, "share_ids_async", fake_share_ids)

    result = asyncio.run(server.share_decision("ab12cd34", "/repo"))
    assert "srv-1" in result


def test_share_decision_local_mode_skips_preview(monkeypatch):
    # #2: with no team configured, don't preview a push that would no-op - go straight to share().
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(share_mod, "load_profile", lambda *a, **k: _config_mod.Profile())  # local
    previewed = {"n": 0}
    monkeypatch.setattr(server.store, "format_share_preview",
                        lambda *a, **k: previewed.__setitem__("n", previewed["n"] + 1) or "PREVIEW")

    async def fake_share_ids(repo, ids, **k):
        return share_status.ShareStatus(share_status.NOT_TEAM_MODE)

    monkeypatch.setattr(share_mod, "share_ids_async", fake_share_ids)

    result = asyncio.run(server.share_decision("ab12cd34", "/repo"))
    assert "Not in team mode" in result
    assert previewed["n"] == 0  # never previewed in local mode


def test_share_decision_team_no_token_skips_preview(monkeypatch):
    # #B: team mode + endpoint but no resolvable token -> from_profile None -> no misleading preview.
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(share_mod, "load_profile",
                        lambda *a, **k: _config_mod.Profile(mode="team", endpoint="https://x/mcp"))
    monkeypatch.setattr("contexer.remote.RemoteStore.from_profile", lambda p: None)
    previewed = {"n": 0}
    monkeypatch.setattr(server.store, "format_share_preview",
                        lambda *a, **k: previewed.__setitem__("n", 1) or "PREVIEW")

    async def fake_share_ids(repo, ids, **k):
        return share_status.ShareStatus(share_status.NOT_TEAM_MODE)

    monkeypatch.setattr(share_mod, "share_ids_async", fake_share_ids)

    result = asyncio.run(server.share_decision("ab12cd34", "/repo"))
    assert "Not in team mode" in result
    assert previewed["n"] == 0  # never advertised a push that can't happen


def test_review_pending_returns_identified_list(monkeypatch):
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(server.store, "format_pending_review", lambda r: "PENDING-LIST")
    assert server.review_pending("/repo") == "PENDING-LIST"


def test_review_pending_no_repo(monkeypatch):
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: "")
    assert server.review_pending("") == "No repo path detected."


def test_resolve_conflict_delegates(monkeypatch):
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: "/repo")
    seen = {}

    def fake_record(repo, entry_id, choice, session_id=""):
        seen["args"] = (repo, entry_id, choice, session_id)
        return True, "MEMO-RECORDED"

    # Patched on the OWNER, not through store's back-compat facade: server.py reaches
    # conflicts directly, so a facade patch would no longer intercept the call.
    monkeypatch.setattr(server.conflicts, "record_conflict_memo", fake_record)
    result = server.resolve_conflict("ab12cd34", "update", "/repo")
    assert result == "MEMO-RECORDED"
    assert seen["args"] == ("/repo", "ab12cd34", "update", server.SESSION_ID)


def test_resolve_conflict_no_repo(monkeypatch):
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: "")
    assert server.resolve_conflict("ab12cd34", "update", "") == "Skipped - repo path not detected."


# ── bulk approval is refused outright ─────────────────────────────────────────
# A blanket approve rubber-stamps whatever happens to be in the queue, and the queue is
# exactly where mis-captured decisions land. _apply_approval stamps approved_by="human"
# on anything it approves, which makes even an ai-sourced entry guard-trusted at commit
# time - so a bulk gesture could silently promote a misfire to trusted standing context.
# Every action is refused, not just approve: 'ignore' in bulk discards decisions the
# developer never actually read.

@pytest.mark.parametrize("target", ["all", "ALL", "*", " all "])
def test_approve_decision_all_is_refused(monkeypatch, tmp_path, target):
    from contexer import store
    monkeypatch.setattr(store, "STORE_DIR", tmp_path)
    repo = "/bulk/repo"
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: repo)
    for c in ("Never commit secrets", "Never log PII"):
        store.update_decision(repo, c, "s", "constraint")

    out = server.approve_decision(target, "approve")
    assert "one at a time" in out
    assert len(store.get_pending_decisions(repo)) == 2  # nothing was touched


def test_approve_decision_comma_list_is_refused(monkeypatch, tmp_path):
    from contexer import store
    monkeypatch.setattr(store, "STORE_DIR", tmp_path)
    repo = "/comma/repo"
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: repo)
    for c in ("Never commit secrets", "Never log PII"):
        store.update_decision(repo, c, "s", "constraint")
    ids = [d["id"][:8] for d in store.get_pending_decisions(repo)]

    out = server.approve_decision(f"{ids[0]},{ids[1]}", "approve")
    assert "one at a time" in out
    assert len(store.get_pending_decisions(repo)) == 2  # nothing was touched


def test_bulk_refusal_covers_ignore_too(monkeypatch, tmp_path):
    from contexer import store
    monkeypatch.setattr(store, "STORE_DIR", tmp_path)
    repo = "/ignorebulk/repo"
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: repo)
    store.update_decision(repo, "Never commit secrets", "s", "constraint")

    out = server.approve_decision("all", "ignore")
    assert "one at a time" in out
    assert len(store.get_pending_decisions(repo)) == 1


def test_approve_decision_single_id_still_works(monkeypatch, tmp_path):
    from contexer import store
    monkeypatch.setattr(store, "STORE_DIR", tmp_path)
    repo = "/single/repo"
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: repo)
    _ok, eid = store.update_decision(repo, "Never commit secrets", "s", "constraint")

    server.approve_decision(eid[:8], "approve")
    assert store.get_pending_decisions(repo) == []


def test_store_no_longer_exposes_bulk_approval():
    """The bulk engine is gone, not merely unrouted - nothing can call it back into life."""
    from contexer import store
    assert not hasattr(store, "approve_decisions")


@pytest.mark.parametrize("target", ["all", "*", "id1,id2"])
def test_multi_target_with_source_files_raises_not_refusal_string(monkeypatch, target):
    """A multi-target carrying source_files is caller misuse of the API, so it keeps RAISING
    rather than returning the developer-facing bulk-refusal text - and it must never reach
    the store."""
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: "/repo")

    def _boom(*a, **k):
        raise AssertionError("must not reach the store")
    monkeypatch.setattr(server.store, "approve_decision", _boom)
    monkeypatch.setattr(server.store, "get_pending_decisions", _boom)

    with pytest.raises(ValueError, match="single decision id"):
        server.approve_decision(target, "approve", source_files=["a.py"])


def test_list_shareable_returns_list(monkeypatch):
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(server.store, "format_shareable_list", lambda r: "SHAREABLE-LIST")
    assert server.list_shareable("/repo") == "SHAREABLE-LIST"


def test_share_decision_multi_id_previews_whole_selection(monkeypatch):
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(share_mod, "load_profile",
                        lambda *a, **k: _config_mod.Profile(mode="team", endpoint="https://x/mcp"))
    monkeypatch.setattr("contexer.remote.RemoteStore.from_profile", lambda p: object())
    got = {}

    def fake_preview(r, d, profile=None):
        got["decision_id"] = d
        return "PREVIEW"

    monkeypatch.setattr(server.store, "format_share_preview", fake_preview)
    result = asyncio.run(server.share_decision("ab12,cd34", "/repo"))
    assert result == "PREVIEW"
    assert got["decision_id"] == "ab12,cd34"  # the comma-separated selection flows to preview


def test_share_decision_multi_id_pushes_parsed_ids(monkeypatch):
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: "/repo")
    # confirm=True short-circuits the preview gate regardless of the profile's content, but
    # share_decision_flow still calls load_profile() unconditionally - patch it anyway so this
    # stays hermetic instead of reading the real ~/.contexer/config.toml.
    monkeypatch.setattr(share_mod, "load_profile",
                        lambda *a, **k: _config_mod.Profile(mode="team", endpoint="https://x/mcp"))
    got = {}

    async def fake_share_ids(repo, ids, **k):
        got["ids"] = ids
        return share_status.ShareStatus(share_status.BATCH_DONE, sent=2, total=2)

    monkeypatch.setattr(share_mod, "share_ids_async", fake_share_ids)
    result = asyncio.run(server.share_decision(" ab12 , cd34 ,", "/repo", confirm=True))
    assert got["ids"] == ["ab12", "cd34"]  # parsed, trimmed, blanks dropped
    assert "Synced 2 decision(s)" in result


def test_get_context_logs_followup_on_matching_pointer(tmp_repo):
    # A prior pointer nudge ("db" topic) plus a matching get_context(query="db") call must
    # log a follow-through event - proof the router's pointers actually get chased. The
    # returned context itself is unaffected (log-only side effect).
    store.update_decision(
        tmp_repo, "Postgres migrations run through Alembic for the orders database",
        "s1", "architecture",
    )
    store.get_context_for_prompt(tmp_repo, "why the schema design here?", "sess-x")
    path = store.STORE_DIR / f".retrieval_{store.repo_slug(tmp_repo)}.jsonl"
    assert json.loads(path.read_text().splitlines()[-1])["e"] == "pointer"

    expected = store.get_context(tmp_repo, "db")
    result = server.get_context(tmp_repo, "db")

    assert result == expected  # log-only side effect - the returned context is unchanged
    events = [json.loads(line) for line in path.read_text().splitlines() if line]
    assert events[-1]["e"] == "followup"
    assert events[-1]["query"] == "db"


def test_get_context_no_followup_without_prior_pointer(tmp_repo):
    store.update_decision(
        tmp_repo, "Postgres migrations run through Alembic for the orders database",
        "s1", "architecture",
    )
    server.get_context(tmp_repo, "db")  # no pointer was ever logged for this repo
    path = store.STORE_DIR / f".retrieval_{store.repo_slug(tmp_repo)}.jsonl"
    assert not path.exists()


def test_get_context_passes_files_through(tmp_repo):
    store.update_decision(
        tmp_repo, "Decided to use JWT for stateless auth tokens", "s1", "architecture",
        created_by="human", source_files=["auth/jwt.py"],
    )
    result = server.get_context(tmp_repo, files=["auth/jwt.py"])
    assert "JWT" in result
    assert result == store.get_context(tmp_repo, files=["auth/jwt.py"])


def test_bootstrap_context_attaches_ask_shape_only_when_gaps_exist(monkeypatch):
    """The gap-question ask shape rides the tool result, not the session-start injection:
    it is unusable without gaps, while the injected block is paid on every context-less
    session start including the skip path."""
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: "/repo/x")
    monkeypatch.setattr(
        server.store, "bootstrap_apply",
        lambda *a, **k: {"gaps": [{"question": "What does this repo do?"}], "stored": 3},
    )
    with_gaps = json.loads(server.bootstrap_context("/repo/x"))
    assert with_gaps["how_to_ask"] == store.GAP_ASK_GUIDE
    assert with_gaps["gaps"] and with_gaps["stored"] == 3, "result passes through unchanged"

    monkeypatch.setattr(server.store, "bootstrap_apply",
                        lambda *a, **k: {"gaps": [], "stored": 3})
    assert "how_to_ask" not in json.loads(server.bootstrap_context("/repo/x"))


def test_bootstrap_context_ask_shape_on_the_read_only_preview(monkeypatch):
    monkeypatch.setattr(server.store, "resolve_repo", lambda p: "/repo/x")
    monkeypatch.setattr(server.store, "bootstrap_scan",
                        lambda *a, **k: {"gaps": [{"question": "Tests in scope?"}]})
    assert "how_to_ask" in json.loads(server.bootstrap_context("/repo/x", apply=False))


# ── capture_lint: bounce narrative-shaped AI captures ───────────────────────

def test_update_context_bounces_narrative(tmp_repo, monkeypatch):
    # The WRITE path resolves verbosely (it stamps repo_source onto the new entry), so the
    # double has to mirror that - patching _resolve_repo alone no longer intercepts it.
    monkeypatch.setattr(store, "resolve_repo_verbose", lambda p: (tmp_repo, "argument"))
    narrative = ("Investigated (2026-08-05) the loader bug at length. " +
                 " ".join(["detail"] * 150))
    out = server.update_context(content=narrative)
    assert "Not stored" in out
    # nothing was written
    assert store.get_pending_decisions(tmp_repo) == []


def test_update_context_relays_the_refusal_ack(tmp_repo, monkeypatch):
    # Issue #202: the refused correction must not come back as a "pending review" prompt.
    monkeypatch.setattr(store, "resolve_repo_verbose", lambda p: (tmp_repo, "argument"))
    standing = "Use Postgres for the decision store; SQLite won't handle concurrent sessions"
    store.update_decision(tmp_repo, standing, "s1", "architecture")
    data = store.load(tmp_repo)
    entry = data["entries"][0]
    entry["status"] = "approved"
    entry["proposed_revision"] = review.build_proposal(
        entry, "Switch to DynamoDB for the decision store", "architecture", "s2",
        datetime.now(timezone.utc).isoformat(), source="human")
    store.save(tmp_repo, data)
    out = server.update_context(content="Switch to Cassandra for the decision store",
                                subtype="architecture", replace_id=entry["id"])
    assert "Correction NOT stored" in out and entry["id"][:8] in out
    assert "Cassandra" not in out, "the approval prompt for a stored correction, not this"


def test_update_global_context_bounce_names_itself(monkeypatch):
    # capture_lint's shared bounce text says "call update_context again" - the global
    # tool must retarget that to its own name, or a restated GLOBAL rule gets re-filed
    # repo-scoped instead of global.
    narrative = ("Investigated (2026-08-05) the loader bug at length. " +
                 " ".join(["detail"] * 150))
    out = server.update_global_context(content=narrative)
    assert "Not stored" in out
    assert "call update_global_context again" in out
    assert "call update_context again" not in out


def test_update_context_stamps_which_signal_chose_the_store(tmp_repo, monkeypatch):
    # End-to-end: the branch that picked this store is recorded on the entry, so a decision
    # that lands in the wrong repo is diagnosable (scope_audit reads it back).
    monkeypatch.setattr(store, "resolve_repo_verbose", lambda p: (tmp_repo, "pointer"))
    server.update_context(content="Use Postgres for the orders schema", subtype="architecture")
    (entry,) = store.load(tmp_repo)["entries"]
    assert entry["repo_source"] == "pointer"


def test_update_context_bounces_a_multi_section_document(tmp_repo, monkeypatch):
    monkeypatch.setattr(store, "resolve_repo_verbose", lambda p: (tmp_repo, "argument"))
    filler = " ".join(["detail"] * 60)
    blob = (f"PRT-98 - why the picker never showed. WHY IT IS INVISIBLE: it is a session "
            f"task. {filler} WHAT THIS ADDS: a create dialog. {filler} "
            f"DECIDED SCOPE BOUNDARIES: sales only. {filler}")
    out = server.update_context(content=blob)
    assert "multi-section document" in out
    assert store.load(tmp_repo)["entries"] == []      # nothing written


# ── SESSION_ID: real host session id when the CLI's env carries it ─────────────────────────

def _session_id_from_subprocess(env_overrides):
    # SESSION_ID is read once, at module import time - the only honest way to test that is a
    # fresh subprocess with a controlled env (pytest has already imported contexer.server in
    # this process, so an in-process monkeypatch of os.environ would prove nothing). Mirrors
    # the sys.executable + "-c" probe pattern in test_anchors.py/test_guard_engine.py's
    # TestImportOrderRegression.
    env = {k: v for k, v in os.environ.items() if k != "CLAUDE_CODE_SESSION_ID"}
    env.update(env_overrides)
    probe = "import contexer.server as s; print(s.SESSION_ID)"
    result = subprocess.run([sys.executable, "-c", probe],
                             capture_output=True, text=True, timeout=30, env=env)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_session_id_uses_host_env_var_when_present():
    seen = _session_id_from_subprocess({"CLAUDE_CODE_SESSION_ID": "real-transcript-session-id"})
    assert seen == "real-transcript-session-id"


def test_session_id_falls_back_to_a_fresh_uuid4_when_env_var_absent():
    first = _session_id_from_subprocess({})
    second = _session_id_from_subprocess({})
    assert uuid.UUID(first).version == 4
    assert uuid.UUID(second).version == 4
    assert first != second  # a fresh random id per process, not a constant


def test_session_id_falls_back_when_env_var_is_empty_string():
    # The reason for `os.environ.get(...) or ...` rather than `.get(key, default)`: a `.get`
    # with a default would return "" here (the key IS present, just empty), stamping every
    # decision this process ever writes with an empty session id. `or` catches that.
    seen = _session_id_from_subprocess({"CLAUDE_CODE_SESSION_ID": ""})
    assert seen != ""
    assert uuid.UUID(seen).version == 4


# ── evaluate_policy ──────────────────────────────────────────────────────────────
# The tool REPORTS. It refuses nothing, writes nothing, and raises nothing - a `block` is a
# sentence for the model to relay to the developer, not a refusal, and an `allow` is not
# permission to stop asking them.


def _armed_secret_rule(tmp_repo):
    from tests.test_policy_api import _arm, _seed
    entry = _seed(tmp_repo, "Never commit credentials", title="No secrets")
    _arm(tmp_repo, entry["id"], "secret")
    return entry


def test_evaluate_policy_reports_a_block_as_text_and_refuses_nothing(tmp_repo, monkeypatch):
    from tests.test_policy_api import AWS_KEY
    monkeypatch.setattr(store, "resolve_repo", lambda p: tmp_repo)
    entry = _armed_secret_rule(tmp_repo)
    before = store.load(tmp_repo)

    out = server.evaluate_policy(tmp_repo, operation="commit", artifact_kind="diff",
                                 artifact=f"+key={AWS_KEY}\n")

    assert isinstance(out, str) and "verdict: block" in out
    assert entry["id"] in out                       # names which decision objected
    assert store.load(tmp_repo) == before           # a read tool: nothing written


def test_evaluate_policy_redacts_the_artifact_out_of_what_it_returns(tmp_repo, monkeypatch):
    from tests.test_policy_api import AWS_KEY
    monkeypatch.setattr(store, "resolve_repo", lambda p: tmp_repo)
    _armed_secret_rule(tmp_repo)
    out = server.evaluate_policy(tmp_repo, operation="commit", artifact_kind="diff",
                                 artifact=f"+AWS_ACCESS_KEY_ID={AWS_KEY}\n")
    assert "verdict: block" in out and AWS_KEY not in out


def test_evaluate_policy_returns_errors_instead_of_raising(tmp_repo, monkeypatch):
    monkeypatch.setattr(store, "resolve_repo", lambda p: tmp_repo)
    out = server.evaluate_policy(tmp_repo, operation="rm-rf")
    assert "Not evaluated" in out and "operation must be one of" in out


def test_evaluate_policy_reports_gaps_rather_than_a_clean_pass(tmp_repo, monkeypatch):
    monkeypatch.setattr(store, "resolve_repo", lambda p: tmp_repo)
    _armed_secret_rule(tmp_repo)
    out = server.evaluate_policy(tmp_repo, operation="commit")   # no artifact at all
    assert "evaluation_status: partial" in out and "omitted" in out


def test_evaluate_policy_delegates_to_the_shared_facade(tmp_repo, monkeypatch):
    # The tool must stay a wrapper: no policy logic of its own, no second selection path.
    seen = {}

    def fake(repo_path, **kw):
        seen["repo_path"] = repo_path
        seen.update(kw)
        return {"verdict": "allow", "evaluation_status": "complete", "basis": "deterministic",
                "matches": [], "unchecked": [], "policy_set_version": "sha256:x",
                "repo_path": repo_path, "errors": []}

    monkeypatch.setattr(server.policy_api, "evaluate_operation", fake)
    server.evaluate_policy("/repo/x", intent="ship it", operation="deploy",
                           files=["a.py"], artifact_kind="deployment", artifact="plan")
    assert seen == {"repo_path": "/repo/x", "intent": "ship it", "operation": "deploy",
                    "files": ["a.py"], "artifact_kind": "deployment", "artifact": "plan"}


def test_evaluate_policy_docstring_is_self_approval_proofed():
    """The one guardrail a tool docstring can carry: the model must not read `block` as a
    refusal it should obey silently, nor `allow` as the developer's agreement."""
    doc = server.evaluate_policy.__doc__
    assert "ADVISORY" in doc
    assert "does not refuse" in doc
    assert "not permission" in doc
