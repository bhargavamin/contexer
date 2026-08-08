"""Tests for MCP tool entry points in contexer/server.py.

share_decision is the one MCP tool that reaches the network from inside FastMCP's event
loop. It AWAITS the async-native share path (share_ids_async), so a slow/hung cloud never
freezes the loop AND a wedged push is cancelled at the deadline (nothing lingers). No
pytest-asyncio: the async tool is driven with asyncio.run(...), matching test_remote.py.
"""
import asyncio
import inspect
import json
import threading

import pytest

import contexer.share as share_mod
from contexer import config as _config_mod
from contexer import server, store


def test_share_decision_is_async_tool():
    # Guards the fix: a regression back to a sync `def` would reintroduce the
    # asyncio.run-in-a-running-loop failure the MCP path hit.
    assert inspect.iscoroutinefunction(server.share_decision)


def test_share_decision_short_circuits_without_repo(monkeypatch):
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "")
    called = {"share": False}
    monkeypatch.setattr(share_mod, "share", lambda *a, **k: called.__setitem__("share", True))
    result = asyncio.run(server.share_decision("d", ""))
    assert result == "Skipped — repo path not detected."
    assert called["share"] is False  # never reaches the network layer


def test_share_decision_awaits_async_share_path_on_the_loop(monkeypatch):
    # The async share path is AWAITED on the event loop (no thread offload), and its args +
    # return value pass through unchanged — a single id becomes a one-element selection.
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo/x")
    seen = {}

    async def fake_share_ids(repo, ids, **kw):
        seen["thread"] = threading.current_thread()
        seen["args"] = (repo, ids)
        return "shared srv-1"

    monkeypatch.setattr(share_mod, "share_ids_async", fake_share_ids)

    async def driver():
        seen["loop_thread"] = threading.current_thread()
        return await server.share_decision("dec-42", "/repo/x", confirm=True)

    result = asyncio.run(driver())
    assert result == "shared srv-1"
    assert seen["args"] == ("/repo/x", ["dec-42"])
    assert seen["thread"] is seen["loop_thread"]  # awaited on the loop, not offloaded to a thread


def test_share_decision_does_not_block_the_event_loop(monkeypatch):
    # While the awaited share path is in-flight, the loop must stay free to make progress on
    # other coroutines. If share_decision froze the loop, no tick could land before it finished.
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo")
    order = []

    async def slow_share_ids(repo, ids, **kw):
        await asyncio.sleep(0.2)  # stands in for a slow network round-trip
        order.append("share_done")
        return "ok"

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
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo")
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
    # must queue the selection itself — otherwise "the outbox retries it" is a false promise.
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo")
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


# ── cloud-push preview gate + review_pending ─────────────────────────────────────


def test_share_decision_previews_by_default_without_pushing(monkeypatch):
    # confirm=False (default) + skip_confirm off -> preview only, NOTHING is pushed.
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo")
    # Team-configured + authenticated: the preview gate only fires when a push could actually happen.
    monkeypatch.setattr(_config_mod, "load_profile",
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
    assert pushed["n"] == 0  # dry run — nothing left the machine


def test_share_decision_skip_confirm_pushes_without_preview(monkeypatch):
    # A developer who set skip_confirm bypasses the preview even with confirm=False.
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(_config_mod, "load_profile",
                        lambda *a, **k: _config_mod.Profile(skip_confirm=True))

    async def fake_share_ids(repo, ids, **k):
        return "pushed"

    monkeypatch.setattr(share_mod, "share_ids_async", fake_share_ids)

    result = asyncio.run(server.share_decision("ab12cd34", "/repo"))
    assert result == "pushed"


def test_share_decision_local_mode_skips_preview(monkeypatch):
    # #2: with no team configured, don't preview a push that would no-op — go straight to share().
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(_config_mod, "load_profile", lambda *a, **k: _config_mod.Profile())  # local
    previewed = {"n": 0}
    monkeypatch.setattr(server.store, "format_share_preview",
                        lambda *a, **k: previewed.__setitem__("n", previewed["n"] + 1) or "PREVIEW")

    async def fake_share_ids(repo, ids, **k):
        return "not configured"

    monkeypatch.setattr(share_mod, "share_ids_async", fake_share_ids)

    result = asyncio.run(server.share_decision("ab12cd34", "/repo"))
    assert result == "not configured"
    assert previewed["n"] == 0  # never previewed in local mode


def test_share_decision_team_no_token_skips_preview(monkeypatch):
    # #B: team mode + endpoint but no resolvable token -> from_profile None -> no misleading preview.
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(_config_mod, "load_profile",
                        lambda *a, **k: _config_mod.Profile(mode="team", endpoint="https://x/mcp"))
    monkeypatch.setattr("contexer.remote.RemoteStore.from_profile", lambda p: None)
    previewed = {"n": 0}
    monkeypatch.setattr(server.store, "format_share_preview",
                        lambda *a, **k: previewed.__setitem__("n", 1) or "PREVIEW")

    async def fake_share_ids(repo, ids, **k):
        return "not configured"

    monkeypatch.setattr(share_mod, "share_ids_async", fake_share_ids)

    result = asyncio.run(server.share_decision("ab12cd34", "/repo"))
    assert result == "not configured"
    assert previewed["n"] == 0  # never advertised a push that can't happen


def test_review_pending_returns_identified_list(monkeypatch):
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(server.store, "format_pending_review", lambda r: "PENDING-LIST")
    assert server.review_pending("/repo") == "PENDING-LIST"


def test_review_pending_no_repo(monkeypatch):
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "")
    assert server.review_pending("") == "No repo path detected."


def test_approve_decision_bulk_ids_and_all(monkeypatch, tmp_path):
    from contexer import store
    monkeypatch.setattr(store, "STORE_DIR", tmp_path)
    repo = "/bulk/repo"
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: repo)
    for c in ("Never commit secrets", "Never log PII", "Never disable TLS verification"):
        store.update_decision(repo, c, "s", "constraint")
    ids = [d["id"][:8] for d in store.get_pending_decisions(repo)]
    # comma-separated bulk: approve the first two
    out = server.approve_decision(f"{ids[0]},{ids[1]}", "approve")
    assert "Applied 'approve' to 2 of 2" in out
    assert len(store.get_pending_decisions(repo)) == 1
    # "all" clears the remainder in one gesture
    server.approve_decision("all", "approve")
    assert store.get_pending_decisions(repo) == []


def test_approve_all_caps_to_displayed_never_approves_unseen(monkeypatch, tmp_path):
    # Greptile #1: 'all' must only act on what review_pending SHOWED (the display cap), never
    # trust decisions beyond the cap that the developer never saw.
    from contexer import store
    monkeypatch.setattr(store, "STORE_DIR", tmp_path)
    repo = "/cap/repo"
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: repo)
    data = store._load(repo)
    for i in range(store._FILTERED_DISPLAY + 2):  # 27 pending, cap is 25
        data["entries"].append(store._new_decision_entry(
            f"Constraint {i} distinct text {i}", "s", "constraint", status="pending_approval"))
    store._save(repo, data)

    out = server.approve_decision("all", "approve")
    assert f"to {store._FILTERED_DISPLAY} of {store._FILTERED_DISPLAY}" in out
    assert "2 more pending" in out
    assert len(store.get_pending_decisions(repo)) == 2  # the 2 unseen stay pending


def test_approve_decision_bulk_reports_failures(monkeypatch, tmp_path):
    # Greptile #2: a stale/invalid id in a bulk call must not read as success.
    from contexer import store
    monkeypatch.setattr(store, "STORE_DIR", tmp_path)
    repo = "/fail/repo"
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: repo)
    _ok, eid = store.update_decision(repo, "Never commit secrets", "s", "constraint")

    out = server.approve_decision(f"{eid[:8]},bogus99", "approve")
    assert "Applied 'approve' to 1 of 2 decision(s) (1 failed" in out
    assert "not found" in out.lower()


def test_approve_decision_bulk_edit_rejected(monkeypatch):
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo")
    assert "Bulk 'edit' isn't supported" in server.approve_decision("a,b", "edit", content="x")


def test_approve_decision_all_nothing_pending(monkeypatch):
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(server.store, "get_pending_decisions", lambda r: [])
    assert server.approve_decision("all", "approve") == "Nothing pending review."


def test_approve_decision_all_with_source_files_raises_before_bulk_route(monkeypatch):
    # M7a: store.approve_decisions has no source_files param and would silently drop
    # the anchor, so server.approve_decision must reject an "all" target carrying
    # source_files BEFORE ever routing to the bulk path.
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo")

    def _boom(*a, **k):
        raise AssertionError("must not route to store.approve_decisions")
    monkeypatch.setattr(server.store, "approve_decisions", _boom)
    monkeypatch.setattr(server.store, "get_pending_decisions", _boom)

    with pytest.raises(ValueError, match="single decision id"):
        server.approve_decision("all", "approve", source_files=["a.py"])


def test_approve_decision_comma_list_with_source_files_raises_before_bulk_route(monkeypatch):
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo")

    def _boom(*a, **k):
        raise AssertionError("must not route to store.approve_decisions")
    monkeypatch.setattr(server.store, "approve_decisions", _boom)

    with pytest.raises(ValueError, match="single decision id"):
        server.approve_decision("id1,id2", "approve", source_files=["a.py"])


def test_list_shareable_returns_list(monkeypatch):
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(server.store, "format_shareable_list", lambda r: "SHAREABLE-LIST")
    assert server.list_shareable("/repo") == "SHAREABLE-LIST"


def test_share_decision_multi_id_previews_whole_selection(monkeypatch):
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(_config_mod, "load_profile",
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
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(_config_mod, "load_profile",
                        lambda *a, **k: _config_mod.Profile(mode="team", endpoint="https://x/mcp"))
    got = {}

    async def fake_share_ids(repo, ids, **k):
        got["ids"] = ids
        return "done"

    monkeypatch.setattr(share_mod, "share_ids_async", fake_share_ids)
    result = asyncio.run(server.share_decision(" ab12 , cd34 ,", "/repo", confirm=True))
    assert got["ids"] == ["ab12", "cd34"]  # parsed, trimmed, blanks dropped
    assert result == "done"


def test_get_context_logs_followup_on_matching_pointer(tmp_repo):
    # A prior pointer nudge ("db" topic) plus a matching get_context(query="db") call must
    # log a follow-through event — proof the router's pointers actually get chased. The
    # returned context itself is unaffected (log-only side effect).
    store.update_decision(
        tmp_repo, "Postgres migrations run through Alembic for the orders database",
        "s1", "architecture",
    )
    store.get_context_for_prompt(tmp_repo, "why the schema design here?", "sess-x")
    path = store.STORE_DIR / f".retrieval_{store._slug(tmp_repo)}.jsonl"
    assert json.loads(path.read_text().splitlines()[-1])["e"] == "pointer"

    expected = store.get_context(tmp_repo, "db")
    result = server.get_context(tmp_repo, "db")

    assert result == expected  # log-only side effect — the returned context is unchanged
    events = [json.loads(line) for line in path.read_text().splitlines() if line]
    assert events[-1]["e"] == "followup"
    assert events[-1]["query"] == "db"


def test_get_context_no_followup_without_prior_pointer(tmp_repo):
    store.update_decision(
        tmp_repo, "Postgres migrations run through Alembic for the orders database",
        "s1", "architecture",
    )
    server.get_context(tmp_repo, "db")  # no pointer was ever logged for this repo
    path = store.STORE_DIR / f".retrieval_{store._slug(tmp_repo)}.jsonl"
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
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo/x")
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
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo/x")
    monkeypatch.setattr(server.store, "bootstrap_scan",
                        lambda *a, **k: {"gaps": [{"question": "Tests in scope?"}]})
    assert "how_to_ask" in json.loads(server.bootstrap_context("/repo/x", apply=False))


# ── capture_lint: bounce narrative-shaped AI captures ───────────────────────

def test_update_context_bounces_narrative(tmp_repo, monkeypatch):
    monkeypatch.setattr(store, "_resolve_repo", lambda p: tmp_repo)
    narrative = ("Investigated (2026-08-05) the loader bug at length. " +
                 " ".join(["detail"] * 150))
    out = server.update_context(content=narrative)
    assert "Not stored" in out
    # nothing was written
    assert store.get_pending_decisions(tmp_repo) == []


def test_update_global_context_bounce_names_itself(monkeypatch):
    # capture_lint's shared bounce text says "call update_context again" — the global
    # tool must retarget that to its own name, or a restated GLOBAL rule gets re-filed
    # repo-scoped instead of global.
    narrative = ("Investigated (2026-08-05) the loader bug at length. " +
                 " ".join(["detail"] * 150))
    out = server.update_global_context(content=narrative)
    assert "Not stored" in out
    assert "call update_global_context again" in out
    assert "call update_context again" not in out
