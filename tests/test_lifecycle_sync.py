"""Capability-negotiated revision + lifecycle sync to Contexer Teams (plan E1/E2/E3, PR 11).

Three concerns, one story, so they live in one file rather than being scattered across
test_remote/test_share/test_team_context: what the server is asked (E1), what may leave the
machine (E2), and what a team tombstone does to the local decision (E3).

The network seam is `remote._acall_tool`, monkeypatched exactly as `tests/test_remote.py`
patches it, so every assertion here is against the REAL `_wire_args` output — never a
projection. That distinction is the point of the E2 block: a projection can be honest and the
wire still leak, so the never-sync items are pinned where the bytes actually go.

`remote._WIRE_LIFECYCLE` ships CLOSED (the server's field spelling is unverified from this
repo), so the tests that exercise the mechanism open it explicitly, and
`test_closed_gate_ships_legacy_shape_even_to_an_advertising_server` pins the shipped default.
"""
import types
import uuid
from datetime import datetime, timezone

import pytest

import contexer.remote as remote
from contexer import config, lifecycle, share, spool, store, team_context
from contexer.remote import (
    RemoteContext,
    RemoteDecision,
    RemoteStore,
    RemoteStoreError,
    RemoteUnavailableError,
)

TEAM = config.Profile(mode="team", endpoint="https://t/mcp", token="tok")

_LIFECYCLE_CAPS = {"version": 1, "revisions": True, "tombstones": True,
                   "retirementReasons": True}

_RETIRED = {"event_id": "ev-1", "kind": "retired", "occurred_at": "2026-08-01T00:00:00+00:00",
            "actor": "human", "reason": "superseded by the new queue design",
            "revision_id": "rev-3", "replacement_decision_id": None}


def _result(*, content=None, structured=None):
    return types.SimpleNamespace(content=content or [], structuredContent=structured,
                                 isError=False)


def _text(s):
    return types.SimpleNamespace(type="text", text=s)


def _caps_seam(monkeypatch, advertised, *, on_push=None, capabilities_exc=None):
    """Fake transport advertising `advertised` (a capabilities dict; `{}` is a server that has
    the tool but advertises nothing). Records every tool name and every push payload."""
    seen = {"pushes": [], "names": []}

    async def fake(endpoint, token, name, arguments, timeout):
        seen["names"].append(name)
        if name == "get_capabilities":
            if capabilities_exc is not None:
                raise capabilities_exc
            return _result(structured={"capabilities": advertised or {}})
        seen["pushes"].append(arguments)
        return on_push or _result(
            content=[_text("Saved decision srv-1 to your personal context.")])

    monkeypatch.setattr(remote, "_acall_tool", fake)
    return seen


def _push(rs, **extra):
    return rs.push_decision(type="constraint", content="never commit to main",
                            repo="github.com/a/b", decision_id="dec-1", **extra)


def _legacy_args():
    """The pre-feature payload for the decision `_push` sends."""
    return remote._wire_args(type="constraint", content="never commit to main",
                             repo="github.com/a/b", decision_id="dec-1")


@pytest.fixture
def wire_open(monkeypatch):
    """Open the constant gate — the mechanism under test is what the server negotiates."""
    monkeypatch.setattr(remote, "_WIRE_LIFECYCLE", True)


# ── E1: capability discovery ─────────────────────────────────────────────────────

def test_lifecycle_capability_parsed_from_advertisement(monkeypatch):
    _caps_seam(monkeypatch, {"decisionLifecycle": _LIFECYCLE_CAPS})
    caps = RemoteStore("https://t/mcp", "tok").get_capabilities().decision_lifecycle
    assert caps == remote.DecisionLifecycleCapabilities(
        version=1, revisions=True, tombstones=True, retirement_reasons=True)


def test_lifecycle_capability_absent_reads_as_none(monkeypatch):
    _caps_seam(monkeypatch, {"decisionReconciliation": {"version": 1, "preview": True}})
    caps = RemoteStore("https://t/mcp", "tok").get_capabilities()
    assert caps.decision_lifecycle is None and caps.decision_reconciliation.preview


def test_lifecycle_advertisement_does_not_hide_the_reconciliation_block(monkeypatch):
    # Blocks are parsed independently; advertising one must never blank the other.
    _caps_seam(monkeypatch, {"decisionLifecycle": _LIFECYCLE_CAPS,
                             "decisionReconciliation": {"version": 2, "atomicSubmit": True}})
    caps = RemoteStore("https://t/mcp", "tok").get_capabilities()
    assert caps.decision_lifecycle.tombstones
    assert caps.decision_reconciliation.version == 2
    assert caps.decision_reconciliation.atomic_submit


def test_lifecycle_capability_unparseable_version_reads_as_zero(monkeypatch):
    _caps_seam(monkeypatch, {"decisionLifecycle": {**_LIFECYCLE_CAPS, "version": "v1"}})
    caps = RemoteStore("https://t/mcp", "tok").get_capabilities().decision_lifecycle
    assert caps.version == 0 and caps.tombstones


def test_non_dict_lifecycle_advertisement_reads_as_none(monkeypatch):
    _caps_seam(monkeypatch, {"decisionLifecycle": True})
    assert RemoteStore("https://t/mcp", "tok").get_capabilities().decision_lifecycle is None


# ── E1: what each server actually receives ───────────────────────────────────────

def test_old_server_receives_byte_identical_legacy_shape(wire_open, monkeypatch):
    # The regression this gate exists to prevent: a server that never advertised
    # `decisionLifecycle` receives EXACTLY what it received before this feature existed.
    seen = _caps_seam(monkeypatch, {})
    _push(RemoteStore("https://t/mcp", "tok"), revision_id="rev-3", lifecycle=[_RETIRED])
    assert seen["pushes"] == [_legacy_args()]


def test_server_without_capabilities_tool_degrades_to_legacy_shape(wire_open, monkeypatch):
    seen = _caps_seam(monkeypatch, None,
                      capabilities_exc=RemoteStoreError("Unknown tool: get_capabilities"))
    _push(RemoteStore("https://t/mcp", "tok"), revision_id="rev-3", lifecycle=[_RETIRED])
    assert seen["pushes"] == [_legacy_args()]


def test_unreachable_capability_probe_degrades_to_legacy_shape(wire_open, monkeypatch):
    # Discovery failure degrades to the OLD shape, never the new one: an unknown server is an
    # old server, and a probe that did not succeed must never upgrade a push.
    seen = _caps_seam(monkeypatch, None, capabilities_exc=RemoteUnavailableError("down"))
    _push(RemoteStore("https://t/mcp", "tok"), revision_id="rev-3", lifecycle=[_RETIRED])
    assert seen["pushes"] == [_legacy_args()]


def test_new_server_receives_lifecycle_deltas(wire_open, monkeypatch):
    seen = _caps_seam(monkeypatch, {"decisionLifecycle": _LIFECYCLE_CAPS})
    _push(RemoteStore("https://t/mcp", "tok"), revision_id="rev-3", lifecycle=[_RETIRED])
    args = seen["pushes"][0]
    assert args["revision_id"] == "rev-3"
    assert args["lifecycle"] == [{"kind": "retired", "event_id": "ev-1", "revision_id": "rev-3",
                                  "occurred_at": "2026-08-01T00:00:00+00:00", "actor": "human",
                                  "reason": "superseded by the new queue design"}]


def test_tombstones_without_retirement_reasons_sends_events_without_prose(wire_open, monkeypatch):
    seen = _caps_seam(monkeypatch, {"decisionLifecycle": {
        "version": 1, "revisions": True, "tombstones": True, "retirementReasons": False}})
    _push(RemoteStore("https://t/mcp", "tok"), revision_id="rev-3", lifecycle=[_RETIRED])
    events = seen["pushes"][0]["lifecycle"]
    assert events and events[0]["kind"] == "retired" and "reason" not in events[0]


def test_revisions_without_tombstones_sends_no_lifecycle(wire_open, monkeypatch):
    seen = _caps_seam(monkeypatch, {"decisionLifecycle": {
        "version": 1, "revisions": True, "tombstones": False, "retirementReasons": True}})
    _push(RemoteStore("https://t/mcp", "tok"), revision_id="rev-3", lifecycle=[_RETIRED])
    assert seen["pushes"][0]["revision_id"] == "rev-3"
    assert "lifecycle" not in seen["pushes"][0]


def test_tombstones_without_revisions_sends_no_revision_id(wire_open, monkeypatch):
    seen = _caps_seam(monkeypatch, {"decisionLifecycle": {
        "version": 1, "revisions": False, "tombstones": True, "retirementReasons": True}})
    _push(RemoteStore("https://t/mcp", "tok"), revision_id="rev-3", lifecycle=[_RETIRED])
    assert "revision_id" not in seen["pushes"][0] and seen["pushes"][0]["lifecycle"]


def test_closed_gate_ships_legacy_shape_even_to_an_advertising_server(monkeypatch):
    # The shipped default. `_WIRE_LIFECYCLE` answers a different question from capability
    # discovery — "do we know the field spelling" — so an advertising server changes nothing
    # until a human opens it, and the probe is not even made.
    assert remote._WIRE_LIFECYCLE is False
    seen = _caps_seam(monkeypatch, {"decisionLifecycle": _LIFECYCLE_CAPS})
    _push(RemoteStore("https://t/mcp", "tok"), revision_id="rev-3", lifecycle=[_RETIRED])
    assert seen["pushes"] == [_legacy_args()]
    assert "get_capabilities" not in seen["names"]


def test_no_lifecycle_data_costs_no_capability_round_trip(wire_open, monkeypatch):
    seen = _caps_seam(monkeypatch, {"decisionLifecycle": _LIFECYCLE_CAPS})
    _push(RemoteStore("https://t/mcp", "tok"))
    assert seen["names"] == ["push_decision"]


def test_capabilities_discovered_once_per_store(wire_open, monkeypatch):
    seen = _caps_seam(monkeypatch, {"decisionLifecycle": _LIFECYCLE_CAPS})
    rs = RemoteStore("https://t/mcp", "tok")
    _push(rs, lifecycle=[_RETIRED])
    _push(rs, lifecycle=[_RETIRED])
    assert seen["names"].count("get_capabilities") == 1
    assert all("lifecycle" in p for p in seen["pushes"])


def test_a_server_that_does_not_advertise_is_not_re_probed(wire_open, monkeypatch):
    # None is a real answer ("this server does not do lifecycle"), distinct from "not asked".
    seen = _caps_seam(monkeypatch, {})
    rs = RemoteStore("https://t/mcp", "tok")
    _push(rs, lifecycle=[_RETIRED])
    _push(rs, lifecycle=[_RETIRED])
    assert seen["names"].count("get_capabilities") == 1


def test_batch_push_discovers_capabilities_once_for_the_whole_batch(wire_open, monkeypatch):
    seen = _caps_seam(monkeypatch, {"decisionLifecycle": _LIFECYCLE_CAPS},
                      on_push=_result(structured={
                          "results": [{"id": "s1", "decisionId": "dec-1"},
                                      {"id": "s2", "decisionId": "dec-2"}], "skipped": []}))
    rows = [{"type": "constraint", "content": f"c{i}", "repo": "github.com/a/b",
             "decision_id": f"dec-{i + 1}", "lifecycle": [_RETIRED]} for i in range(2)]
    RemoteStore("https://t/mcp", "tok").push_decisions(rows)
    assert seen["names"].count("get_capabilities") == 1
    assert all(d["lifecycle"] for d in seen["pushes"][0]["decisions"])


# ── E2: bound_lifecycle, the whitelist and the bounds ────────────────────────────

def test_bound_lifecycle_drops_an_unknown_kind():
    assert remote.bound_lifecycle([{"kind": "exfiltrated", "reason": "r"}]) == []


def test_bound_lifecycle_omits_an_unrecognized_actor():
    # An actor is a CATEGORY. Free text there could carry a name or an address, so an
    # unrecognized value omits the key rather than passing through.
    assert "actor" not in remote.bound_lifecycle([{**_RETIRED, "actor": "alice@example.com"}])[0]


def test_bound_lifecycle_keeps_only_whitelisted_keys():
    row = remote.bound_lifecycle([{**_RETIRED, "session_id": "s-9", "evidence_ids": ["e1"],
                                   "prompt": "the raw prompt"}])[0]
    assert set(row) <= {"kind", "event_id", "revision_id", "replacement_decision_id",
                        "occurred_at", "actor", "reason"}


def test_bound_lifecycle_truncates_a_long_reason():
    row = remote.bound_lifecycle([{**_RETIRED, "reason": "x" * 900}])[0]
    assert len(row["reason"]) == remote._WIRE_LIFECYCLE_MAX_REASON


def test_bound_lifecycle_scrubs_secrets_from_a_reason():
    row = remote.bound_lifecycle(
        [{**_RETIRED, "reason": "rotated AKIAIOSFODNN7EXAMPLE"}], redact_on=True)[0]
    assert "AKIAIOSFODNN7EXAMPLE" not in row["reason"] and "REDACTED" in row["reason"]


def test_bound_lifecycle_drops_an_implausible_id():
    row = remote.bound_lifecycle([{**_RETIRED, "event_id": "e" * 500}])[0]
    assert "event_id" not in row and row["kind"] == "retired"


def test_bound_lifecycle_keeps_the_most_recent_events():
    kept = remote.bound_lifecycle([{**_RETIRED, "event_id": f"ev-{i}"} for i in range(40)])
    assert len(kept) == remote._WIRE_LIFECYCLE_MAX_EVENTS
    assert kept[-1]["event_id"] == "ev-39"


def test_bound_lifecycle_omits_a_null_replacement():
    assert "replacement_decision_id" not in remote.bound_lifecycle([_RETIRED])[0]


def test_bound_lifecycle_survives_a_non_dict_record():
    assert remote.bound_lifecycle(["not a record", None, _RETIRED])[0]["kind"] == "retired"


# ── E2: the outbound privacy boundary, asserted on the real wire payload ─────────
# Every test below builds a REAL local entry carrying the never-sync field, drives it through
# the production path (share projection -> push kwargs -> _wire_args) and asserts on the
# payload bytes. A projection assertion would not be evidence: the wire is where it matters.

def _wire_for(repo, entry_id, *, caps=_LIFECYCLE_CAPS, wire_open=True, monkeypatch=None):
    """The actual push payload for one stored decision, with the gate open and a fully
    advertising server — the most permissive configuration this client has, so anything absent
    here can never egress in any configuration."""
    monkeypatch.setattr(remote, "_WIRE_LIFECYCLE", wire_open)
    seen = _caps_seam(monkeypatch, {"decisionLifecycle": caps} if caps else {})
    proj = store.get_shareable(repo, entry_id)
    assert proj is not None
    RemoteStore("https://t/mcp", "tok").push_decision(
        **share._dec_push_kwargs(proj, "github.com/a/b"))
    return seen["pushes"][0]


def _payload_text(args) -> str:
    """The whole payload as one searchable string — a leak in any nested field shows up."""
    return repr(args)


def _seed(repo, content="never commit directly to main", **extra):
    ok, did = store.update_decision(repo, content, "sess-1", subtype="constraint")
    assert ok
    data = store.load(repo)
    entry = next(e for e in data["entries"] if e["id"] == did)
    entry.update(extra)
    store.save(repo, data)
    return did


def test_proposed_revision_never_reaches_the_wire(tmp_repo, monkeypatch):
    did = _seed(tmp_repo, proposed_revision={
        "content": "UNREVIEWED: rewrite the rule to allow main pushes",
        "title": "UNREVIEWED title", "source": "ai", "created_at": "2026-08-01T00:00:00+00:00"})
    args = _wire_for(tmp_repo, did, monkeypatch=monkeypatch)
    assert "proposed_revision" not in args
    assert "UNREVIEWED" not in _payload_text(args)


def test_proposed_lifecycle_never_reaches_the_wire(tmp_repo, monkeypatch):
    did = _seed(tmp_repo)
    store.approve_decision(tmp_repo, did, "approve")
    result = lifecycle.propose_lifecycle(
        tmp_repo, did, "retire", "PENDINGREASON: the service was decommissioned", source="ai")
    assert result["ok"]
    args = _wire_for(tmp_repo, did, monkeypatch=monkeypatch)
    assert "proposed_lifecycle" not in args
    assert "PENDINGREASON" not in _payload_text(args)
    assert "lifecycle" not in args      # a proposal is not a completed event


def test_evidence_summary_never_reaches_the_wire(tmp_repo, monkeypatch):
    did = _seed(tmp_repo, evidence_summary=[{"event_ids": ["EVIDENCEEVENT-1"],
                                             "disposition": "accepted",
                                             "candidate_id": "CANDIDATE-9"}])
    args = _wire_for(tmp_repo, did, monkeypatch=monkeypatch)
    assert "evidence_summary" not in args
    assert "EVIDENCEEVENT" not in _payload_text(args)
    assert "CANDIDATE-9" not in _payload_text(args)


def test_anchor_candidates_key_never_reaches_the_wire(tmp_repo, monkeypatch):
    # The ratified exception stays exactly as it is: `_share_projection` still FALLS BACK to
    # the candidates for `source_files` (issue #174), so the paths egress under that field.
    # What must never egress is the candidate field itself, i.e. the claim that these are
    # blessed anchors.
    did = _seed(tmp_repo, anchor_candidates=["src/queue.py"])
    args = _wire_for(tmp_repo, did, monkeypatch=monkeypatch)
    assert "anchor_candidates" not in args
    assert args.get("source_files") == ["src/queue.py"]   # the ratified fallback, undisturbed


def test_raw_evidence_spool_never_reaches_the_wire(tmp_repo, monkeypatch):
    # Raw events, held evidence, `.gap` and receipts are a separate store entirely: nothing on
    # the wire path reads the spool, and this pins that a spooled event stays home. The
    # `.reconcile_<slug>.jsonl` receipt log is named explicitly beside it — same rule, different
    # file, and it is the one an "evidence spool" sentinel would otherwise miss by name.
    spool.append_evidence(tmp_repo, {
        "schema_version": 1, "event_id": str(uuid.uuid4()), "session_id": "sess-1",
        "repo_key": tmp_repo, "kind": "file_changed",
        "occurred_at": datetime.now(timezone.utc).isoformat(), "source": "test",
        "summary": "SPOOLEDSECRET: the raw prompt and the full diff",
        "files": ["src/app.py"], "attributes": {}})
    store.STORE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    (store.STORE_DIR / f".reconcile_{store.repo_slug(tmp_repo)}.jsonl").write_text(
        '{"receipt": "RECEIPTLOGSECRET"}\n', encoding="utf-8")
    did = _seed(tmp_repo)
    args = _wire_for(tmp_repo, did, monkeypatch=monkeypatch)
    assert "SPOOLEDSECRET" not in _payload_text(args)
    assert "RECEIPTLOGSECRET" not in _payload_text(args)
    assert not any(k in args for k in ("evidence_events", "held", "gap", "receipts"))


def test_prompts_responses_diffs_and_test_output_never_reach_the_wire(tmp_repo, monkeypatch):
    # Whatever a future writer stashes on an entry, `_wire_args` builds its payload out of
    # NAMED parameters, so an unlisted key has no route to the wire at all.
    did = _seed(tmp_repo, prompt="PROMPTLEAK", agent_response="RESPONSELEAK",
                diff="DIFFLEAK", test_output="TESTOUTPUTLEAK",
                candidates=[{"content": "UNAPPROVEDCANDIDATE"}])
    args = _wire_for(tmp_repo, did, monkeypatch=monkeypatch)
    text = _payload_text(args)
    assert not any(marker in text for marker in
                   ("PROMPTLEAK", "RESPONSELEAK", "DIFFLEAK", "TESTOUTPUTLEAK",
                    "UNAPPROVEDCANDIDATE"))


def test_a_completed_lifecycle_record_does_reach_the_wire(tmp_repo, monkeypatch):
    # The other half of the pending-vs-completed rule: once a human has actually retired and
    # restored the decision, that history is team knowledge and egresses.
    did = _seed(tmp_repo)
    store.approve_decision(tmp_repo, did, "approve")
    ok, _ = lifecycle.retire_decision(tmp_repo, did, "the queue moved to Kafka")
    assert ok
    ok, _ = lifecycle.restore_decision(tmp_repo, did, "the migration was reverted")
    assert ok
    args = _wire_for(tmp_repo, did, monkeypatch=monkeypatch)
    kinds = [e["kind"] for e in args["lifecycle"]]
    assert kinds == ["retired", "restored"]
    assert args["lifecycle"][0]["reason"] == "the queue moved to Kafka"
    assert args["lifecycle"][0]["actor"] == "human"


def test_a_completed_record_stays_home_for_an_old_server(tmp_repo, monkeypatch):
    did = _seed(tmp_repo)
    store.approve_decision(tmp_repo, did, "approve")
    lifecycle.retire_decision(tmp_repo, did, "the queue moved to Kafka")
    lifecycle.restore_decision(tmp_repo, did, "the migration was reverted")
    args = _wire_for(tmp_repo, did, caps=None, monkeypatch=monkeypatch)
    assert "lifecycle" not in args and "revision_id" not in args


# ── E2: a queued row drains under CURRENT knowledge ──────────────────────────────

def test_a_row_queued_before_discovery_drains_under_current_knowledge(tmp_repo, monkeypatch):
    # The `source_files` precedent, one step further: the row is queued while nothing is known
    # about the server, and the draining store's own discovery decides what egresses.
    did = _seed(tmp_repo)
    store.approve_decision(tmp_repo, did, "approve")
    lifecycle.retire_decision(tmp_repo, did, "the queue moved to Kafka")
    lifecycle.restore_decision(tmp_repo, did, "the migration was reverted")
    proj = store.get_shareable(tmp_repo, did)
    share._enqueue(share._payload(proj, "github.com/a/b"))
    assert share._load_outbox()[0]["lifecycle"], "the outbox must carry it regardless of any gate"

    monkeypatch.setattr(remote, "_WIRE_LIFECYCLE", True)
    seen = _caps_seam(monkeypatch, {"decisionLifecycle": _LIFECYCLE_CAPS},
                      on_push=_result(structured={
                          "results": [{"id": "s1", "decisionId": proj["id"]}], "skipped": []}))
    monkeypatch.setattr(share.RemoteStore, "from_profile",
                        staticmethod(lambda p, **kw: RemoteStore("https://t/mcp", "tok")))
    share.drain_outbox(profile=TEAM)
    assert seen["pushes"][0]["decisions"][0]["lifecycle"][0]["kind"] == "retired"


def test_the_queued_row_is_already_bounded_and_scrubbed(tmp_repo):
    # The projection half of the two-layer bound: the durable outbox must hold exactly what a
    # drain will send, not the raw on-disk record. `_wire_args` bounds again as the guarantee,
    # but a row that sat in the outbox with an unscrubbed secret in it was already a leak of a
    # different kind — the outbox is a file, and it outlives the push.
    did = _seed(tmp_repo)
    data = store.load(tmp_repo)
    entry = next(e for e in data["entries"] if e["id"] == did)
    entry["lifecycle"] = [{"event_id": f"ev-{i}", "kind": "retired", "actor": "human",
                           "occurred_at": "2026-08-01T00:00:00+00:00",
                           "reason": "rotated AKIAIOSFODNN7EXAMPLE " + "x" * 900,
                           "session_id": "SESSIONLEAK"} for i in range(40)]
    store.save(tmp_repo, data)
    share._enqueue(share._payload(store.get_shareable(tmp_repo, did), "github.com/a/b"))

    events = share._load_outbox()[0]["lifecycle"]
    assert len(events) == remote._WIRE_LIFECYCLE_MAX_EVENTS
    assert len(events[0]["reason"]) == remote._WIRE_LIFECYCLE_MAX_REASON
    assert "AKIAIOSFODNN7EXAMPLE" not in repr(events)
    assert "SESSIONLEAK" not in repr(events)


def test_the_same_queued_row_stays_legacy_against_an_old_server(tmp_repo, monkeypatch):
    did = _seed(tmp_repo)
    store.approve_decision(tmp_repo, did, "approve")
    lifecycle.retire_decision(tmp_repo, did, "the queue moved to Kafka")
    lifecycle.restore_decision(tmp_repo, did, "the migration was reverted")
    proj = store.get_shareable(tmp_repo, did)
    share._enqueue(share._payload(proj, "github.com/a/b"))

    monkeypatch.setattr(remote, "_WIRE_LIFECYCLE", True)
    seen = _caps_seam(monkeypatch, {}, on_push=_result(structured={
        "results": [{"id": "s1", "decisionId": proj["id"]}], "skipped": []}))
    monkeypatch.setattr(share.RemoteStore, "from_profile",
                        staticmethod(lambda p, **kw: RemoteStore("https://t/mcp", "tok")))
    share.drain_outbox(profile=TEAM)
    assert "lifecycle" not in seen["pushes"][0]["decisions"][0]


# ── E3: delta convergence ────────────────────────────────────────────────────────

class _FakeRS:
    """One scripted `get_context` per call, so a test can pull a tombstone then a restore."""

    def __init__(self, contexts):
        self._contexts = list(contexts)
        self.calls = []

    def get_context(self, repo=None, updated_since=None):
        self.calls.append(updated_since)
        return self._contexts.pop(0)


def _row(id, content, cursor_repo="github.com/a/b"):
    return RemoteDecision(id=id, type="constraint", title=None, content=content, rationale=None,
                          repo=cursor_repo, agent=None, scope="team")


@pytest.fixture
def team_env(tmp_repo, monkeypatch):
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    remote.reset_degradation_warnings()
    return tmp_repo


def _script(monkeypatch, contexts):
    fake = _FakeRS(contexts)
    monkeypatch.setattr(team_context.RemoteStore, "from_profile",
                        staticmethod(lambda p, **kw: fake))
    return fake


def test_a_team_tombstone_removes_the_cached_row_without_touching_the_local_decision(
        team_env, monkeypatch):
    local_id = _seed(team_env, "the local decision the team also holds")
    store.approve_decision(team_env, local_id, "approve")
    _script(monkeypatch, [
        RemoteContext(decisions=[_row("team-1", "team rule")], deleted=[], cursor="c1"),
        RemoteContext(decisions=[], deleted=["team-1"], cursor="c2"),
    ])
    team_context.pull(team_env, profile=TEAM)
    assert [d["id"] for d in team_context._load_cache(team_env)["decisions"]] == ["team-1"]

    team_context.pull(team_env, profile=TEAM)
    assert team_context._load_cache(team_env)["decisions"] == []
    # The local lifecycle is the developer's: a team row is not a local approval.
    entry = store.entry_by_id(store.load(team_env)["entries"], local_id)
    assert entry is not None and store.entry_status(entry) == "approved"
    assert "lifecycle" not in entry and "proposed_lifecycle" not in entry
    assert store.load_deleted(team_env)["entries"] == []


def test_a_later_restore_re_adds_the_row(team_env, monkeypatch):
    _script(monkeypatch, [
        RemoteContext(decisions=[_row("team-1", "team rule")], deleted=[], cursor="c1"),
        RemoteContext(decisions=[], deleted=["team-1"], cursor="c2"),
        RemoteContext(decisions=[_row("team-1", "team rule, restored")], deleted=[], cursor="c3"),
    ])
    for _ in range(3):
        team_context.pull(team_env, profile=TEAM)
    rows = team_context._load_cache(team_env)["decisions"]
    assert [(r["id"], r["content"]) for r in rows] == [("team-1", "team rule, restored")]


def test_a_byte_identical_resend_after_the_inclusive_cursor_is_not_a_new_row(
        team_env, monkeypatch):
    # The server's `updatedSince` is INCLUSIVE, so a row stamped exactly at the cursor comes
    # back every delta fetch. It must not read as new (it would re-inject every poll window).
    _script(monkeypatch, [
        RemoteContext(decisions=[_row("team-1", "team rule")], deleted=[], cursor="c1"),
        RemoteContext(decisions=[_row("team-1", "team rule")], deleted=[], cursor="c1"),
    ])
    assert team_context.pull(team_env, profile=TEAM) == (1, 0)
    assert team_context.pull(team_env, profile=TEAM) == (0, 0)


def test_the_remote_cursor_and_the_local_evidence_spool_advance_independently(
        team_env, monkeypatch):
    # They advance for different reasons, so they are kept in different files with no shared
    # state: syncing the cloud must never consume local evidence, and spooling local evidence
    # must never move the remote cursor.
    def _event(summary):
        return {"schema_version": 1, "event_id": str(uuid.uuid4()), "session_id": "sess-1",
                "repo_key": team_env, "kind": "file_changed",
                "occurred_at": datetime.now(timezone.utc).isoformat(), "source": "test",
                "summary": summary, "files": ["src/app.py"], "attributes": {}}

    spool.append_evidence(team_env, _event("before the sync"))
    _script(monkeypatch, [
        RemoteContext(decisions=[_row("team-1", "team rule")], deleted=[], cursor="c1"),
        RemoteContext(decisions=[_row("team-2", "second rule")], deleted=[], cursor="c2"),
    ])
    team_context.pull(team_env, profile=TEAM)
    assert team_context._load_cache(team_env)["cursor"] == "c1"
    assert len(spool.list_pending_evidence(team_env)) == 1     # the cursor moved, evidence did not

    spool.append_evidence(team_env, _event("after the sync"))
    assert team_context._load_cache(team_env)["cursor"] == "c1"   # evidence moved, cursor did not

    team_context.pull(team_env, profile=TEAM)
    assert team_context._load_cache(team_env)["cursor"] == "c2"
    assert len(spool.list_pending_evidence(team_env)) == 2
