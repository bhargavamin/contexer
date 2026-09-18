"""Capability-negotiated revision + lifecycle sync to Contexer Teams (plan E1/E2/E3, PR 11).

Three concerns, one story, so they live in one file rather than being scattered across
test_remote/test_share/test_team_context: what the server is asked (E1), what may leave the
machine (E2), and what a team tombstone does to the local decision (E3).

The network seam is `remote._acall_tool`, monkeypatched exactly as `tests/test_remote.py`
patches it, so every assertion here is against the REAL `_wire_args` output - never a
projection. That distinction is the point of the E2 block: a projection can be honest and the
wire still leak, so the never-sync items are pinned where the bytes actually go.

`remote._WIRE_LIFECYCLE` ships OPEN since Task 08 (the server contract was read and proved live
against a running migrated endpoint). The tests that exercise the mechanism still patch it
explicitly, so they say what they depend on rather than inheriting it; the shipped value is
pinned by `test_the_shipped_gate_is_open_and_negotiates`, and the one-line rollback the constant's
comment promises by `test_a_closed_gate_ships_legacy_shape_even_to_an_advertising_server`.
"""
import hashlib
import json
import pathlib
import types
import uuid
from datetime import datetime, timezone

import pytest

import contexer.remote as remote
from contexer import config, lifecycle, share, share_status, spool, store, team_context
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
    """Open the constant gate - the mechanism under test is what the server negotiates."""
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


def test_a_closed_gate_ships_legacy_shape_even_to_an_advertising_server(monkeypatch):
    # `_WIRE_LIFECYCLE` answers a DIFFERENT question from capability discovery - "do we know the
    # field spelling this server expects" - so with it closed an advertising server changes
    # nothing and the probe is not even made. The constant is open as shipped (validated live
    # against a real server, see its comment); this pins the rollback path still works, since a
    # rollback is the one-line change that comment promises.
    monkeypatch.setattr(remote, "_WIRE_LIFECYCLE", False)
    seen = _caps_seam(monkeypatch, {"decisionLifecycle": _LIFECYCLE_CAPS})
    _push(RemoteStore("https://t/mcp", "tok"), revision_id="rev-3", lifecycle=[_RETIRED])
    assert seen["pushes"] == [_legacy_args()]
    assert "get_capabilities" not in seen["names"]


def test_the_shipped_gate_is_open_and_negotiates(monkeypatch):
    # The shipped default, after the Task 08 live validation: an advertising server receives the
    # deltas with no test-local patching of the constant.
    assert remote._WIRE_LIFECYCLE is True
    seen = _caps_seam(monkeypatch, {"decisionLifecycle": _LIFECYCLE_CAPS})
    _push(RemoteStore("https://t/mcp", "tok"), revision_id="rev-3", lifecycle=[_RETIRED])
    assert seen["pushes"][0]["revision_id"] == "rev-3"
    assert seen["pushes"][0]["lifecycle"][0]["kind"] == "retired"


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
    advertising server - the most permissive configuration this client has, so anything absent
    here can never egress in any configuration."""
    monkeypatch.setattr(remote, "_WIRE_LIFECYCLE", wire_open)
    seen = _caps_seam(monkeypatch, {"decisionLifecycle": caps} if caps else {})
    proj = store.get_shareable(repo, entry_id)
    assert proj is not None
    RemoteStore("https://t/mcp", "tok").push_decision(
        **share._dec_push_kwargs(proj, "github.com/a/b"))
    return seen["pushes"][0]


def _payload_text(args) -> str:
    """The whole payload as one searchable string - a leak in any nested field shows up."""
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


def test_recent_edit_anchor_candidates_never_reach_the_wire(tmp_repo, monkeypatch):
    # A recent-edit sidecar is only a possible relationship. Teams has no candidate-certainty
    # bit and treats source_files as applicability, so neither the field nor its path may egress.
    did = _seed(tmp_repo, anchor_candidates=["src/queue.py"])
    args = _wire_for(tmp_repo, did, monkeypatch=monkeypatch)
    assert "anchor_candidates" not in args
    assert args.get("source_files") is None


def test_structurally_confirmed_candidates_reach_the_wire_as_exact_scope(tmp_repo, monkeypatch):
    did = _seed(tmp_repo, anchor_candidates=["src/generated/client.ts"],
                anchor_candidates_confirmed=True)
    args = _wire_for(tmp_repo, did, monkeypatch=monkeypatch)
    assert "anchor_candidates" not in args
    assert args.get("source_files") == ["src/generated/client.ts"]


def test_raw_evidence_spool_never_reaches_the_wire(tmp_repo, monkeypatch):
    # Raw events, held evidence, `.gap` and receipts are a separate store entirely: nothing on
    # the wire path reads the spool, and this pins that a spooled event stays home. The
    # `.reconcile_<slug>.jsonl` receipt log is named explicitly beside it - same rule, different
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
    # different kind - the outbox is a file, and it outlives the push.
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


# ── the canonical contract fixture (Task 08) ─────────────────────────────────────
# `tests/fixtures/lifecycle-contract.v1.json` is a BYTE-FOR-BYTE copy of the file generated in
# the contexer-teams worktree (`packages/db/test/fixtures/lifecycle-contract.v1.json`), and both
# suites assert against it. Two hand-written interpretations of one wire contract is exactly how
# a field spelling drifts into a permanently stuck outbox row, so there is one file.
#
# The digest is the always-on drift detector: an edit to THIS copy fails here, and an edit to the
# Teams copy fails the Teams suite (its own tests derive from the same file). Regenerating the
# fixture means re-copying it and updating this constant in the same change.
_CONTRACT_SHA256 = "3a50a747ad19e45ae86761f1116600301b563163d39ee02107240036025dc55b"
_CONTRACT_PATH = pathlib.Path(__file__).parent / "fixtures" / "lifecycle-contract.v1.json"
_CONTRACT = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))


def test_the_contract_fixture_is_the_bytes_both_repositories_agreed_on():
    digest = hashlib.sha256(_CONTRACT_PATH.read_bytes()).hexdigest()
    assert digest == _CONTRACT_SHA256


def test_the_client_wire_bounds_are_the_contract_bounds():
    assert _CONTRACT["bounds"] == {
        "maxEvents": remote._WIRE_LIFECYCLE_MAX_EVENTS,
        "maxIdLength": remote._WIRE_LIFECYCLE_MAX_ID,
        "maxReasonLength": remote._WIRE_LIFECYCLE_MAX_REASON,
    }
    assert tuple(_CONTRACT["kinds"]) == remote._WIRE_LIFECYCLE_KINDS
    assert tuple(_CONTRACT["actors"]) == remote._WIRE_LIFECYCLE_ACTORS


def test_the_serializer_emits_the_contract_payload_byte_for_byte(wire_open, monkeypatch):
    seen = _caps_seam(monkeypatch, {"decisionLifecycle": _CONTRACT["capabilities"]["full"]})
    RemoteStore("https://t/mcp", "tok").push_decision(
        type="constraint", content="never commit directly to main", repo="github.com/a/b",
        decision_id="dec-1", revision_id="rev-3", lifecycle=[_CONTRACT["events"]["full"]])
    expected = dict(_CONTRACT["payloads"]["singularValid"])
    # The serializer omits a null optional rather than sending it (the server reads an absent key
    # as unset), so the fixture's explicit null is the only difference between the stored record
    # and its wire projection.
    expected["lifecycle"] = [{k: v for k, v in expected["lifecycle"][0].items() if v is not None}]
    assert seen["pushes"][0] == expected


def test_an_old_server_receives_the_contract_legacy_payload(wire_open, monkeypatch):
    seen = _caps_seam(monkeypatch, {})
    RemoteStore("https://t/mcp", "tok").push_decision(
        type="constraint", content="never commit directly to main", repo="github.com/a/b",
        decision_id="dec-1", revision_id="rev-3", lifecycle=[_CONTRACT["events"]["full"]])
    assert seen["pushes"] == [_CONTRACT["payloads"]["legacy"]]


# ── the optional-protocol fallback (Task 08) ─────────────────────────────────────
# A server that ADVERTISED the capability and then refuses the augmented push. The base decision
# is what the developer asked to sync; losing it to a mis-negotiated optional field is the whole
# failure this gate was closed against.

def _refusing_seam(monkeypatch, *, message="Invalid arguments for tool push_decision",
                   fail_when=lambda args: "lifecycle" in args or "revision_id" in args,
                   advertised=None, tool="push_decision", on_legacy=None):
    """Advertises the lifecycle capability, then refuses any push matching `fail_when`."""
    seen = {"pushes": [], "names": []}

    async def fake(endpoint, token, name, arguments, timeout):
        seen["names"].append(name)
        if name == "get_capabilities":
            return _result(structured={"capabilities": {
                "decisionLifecycle": advertised if advertised is not None else _LIFECYCLE_CAPS}})
        seen["pushes"].append(arguments)
        rows = arguments.get("decisions") or [arguments]
        if any(fail_when(r) for r in rows):
            return types.SimpleNamespace(content=[_text(message)], structuredContent=None,
                                         isError=True)
        return on_legacy or _result(
            content=[_text("Saved decision srv-1 to your personal context.")])

    monkeypatch.setattr(remote, "_acall_tool", fake)
    return seen


def test_a_refused_lifecycle_payload_still_syncs_the_base_decision(wire_open, monkeypatch):
    seen = _refusing_seam(monkeypatch)
    rs = RemoteStore("https://t/mcp", "tok")
    assert _push(rs, revision_id="rev-3", lifecycle=[_RETIRED]) == "srv-1"
    # Two pushes: the augmented one that was refused, then the byte-identical legacy shape.
    assert len(seen["pushes"]) == 2
    assert seen["pushes"][1] == _legacy_args()
    assert [b["decision_id"] for b in rs.lifecycle_blocked] == ["dec-1"]
    assert rs.lifecycle_blocked[0]["capability"] == "v1:r1t1p1"


def test_the_capability_is_disabled_after_a_refusal_so_the_next_push_is_legacy(
        wire_open, monkeypatch):
    seen = _refusing_seam(monkeypatch)
    rs = RemoteStore("https://t/mcp", "tok")
    _push(rs, revision_id="rev-3", lifecycle=[_RETIRED])
    seen["pushes"].clear()
    _push(rs, revision_id="rev-3", lifecycle=[_RETIRED])
    # One push, already legacy: the client does not re-offer a field this server just refused.
    assert seen["pushes"] == [_legacy_args()]
    assert seen["names"].count("get_capabilities") == 1


def test_an_auth_failure_is_never_retried_as_legacy(wire_open, monkeypatch):
    seen = _refusing_seam(monkeypatch, message="insufficient_scope: write scope required")
    rs = RemoteStore("https://t/mcp", "tok")
    with pytest.raises(remote.RemoteAuthError):
        _push(rs, revision_id="rev-3", lifecycle=[_RETIRED])
    assert len(seen["pushes"]) == 1
    assert rs.lifecycle_blocked == []


def test_a_quota_failure_is_never_retried_as_legacy(wire_open, monkeypatch):
    seen = _refusing_seam(
        monkeypatch, message="Rate limit exceeded - too many requests. Retry in 42s.")
    rs = RemoteStore("https://t/mcp", "tok")
    with pytest.raises(RemoteStoreError):
        _push(rs, revision_id="rev-3", lifecycle=[_RETIRED])
    assert len(seen["pushes"]) == 1
    assert rs.lifecycle_blocked == []


def test_an_unrelated_validation_failure_raises_and_records_no_lifecycle_blockage(
        wire_open, monkeypatch):
    # The retry IS the discriminator: the legacy payload fails identically, so the fields were
    # never the problem. The original error propagates and nothing is marked lifecycle-blocked.
    seen = _refusing_seam(monkeypatch, message="content must not be empty",
                          fail_when=lambda args: True)
    rs = RemoteStore("https://t/mcp", "tok")
    with pytest.raises(RemoteStoreError, match="content must not be empty"):
        _push(rs, revision_id="rev-3", lifecycle=[_RETIRED])
    assert len(seen["pushes"]) == 2      # tried, retried as legacy, gave up
    assert rs.lifecycle_blocked == []


def test_a_batch_row_the_server_rejects_for_lifecycle_is_re_pushed_as_legacy(
        wire_open, monkeypatch):
    saved = _result(structured={"results": [{"id": "s1", "decisionId": "dec-1"}], "skipped": []})
    rejected = _result(structured={
        "results": [], "skipped": [{"decisionId": "dec-1", "reason": "invalid_lifecycle"}]})
    seen = {"pushes": []}

    async def fake(endpoint, token, name, arguments, timeout):
        if name == "get_capabilities":
            return _result(structured={"capabilities": {"decisionLifecycle": _LIFECYCLE_CAPS}})
        seen["pushes"].append(arguments)
        carries = any("lifecycle" in d for d in arguments["decisions"])
        return rejected if carries else saved

    monkeypatch.setattr(remote, "_acall_tool", fake)
    rs = RemoteStore("https://t/mcp", "tok")
    saved_ids, skipped = rs.push_decisions([{
        "type": "constraint", "content": "never commit to main", "repo": "github.com/a/b",
        "decision_id": "dec-1", "revision_id": "rev-3", "lifecycle": [_RETIRED]}])
    # The base row saved on the legacy retry, and `invalid_lifecycle` never reaches the caller as
    # a skip - left there it would read as a PERMANENT rejection and the decision would be dropped.
    assert saved_ids == ["s1"] and skipped == []
    assert [b["decision_id"] for b in rs.lifecycle_blocked] == ["dec-1"]
    assert "lifecycle" not in seen["pushes"][1]["decisions"][0]


def test_a_blocked_delta_stays_durably_pending_in_the_outbox(tmp_repo, wire_open, monkeypatch):
    did = _seed(tmp_repo)
    store.approve_decision(tmp_repo, did, "approve")
    lifecycle.retire_decision(tmp_repo, did, "the queue moved to Kafka")
    lifecycle.restore_decision(tmp_repo, did, "the migration was reverted")
    _refusing_seam(monkeypatch)
    monkeypatch.setattr(share.RemoteStore, "from_profile",
                        staticmethod(lambda p, **kw: RemoteStore("https://t/mcp", "tok")))
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")

    status = share.share(tmp_repo, did, profile=TEAM)
    assert status.outcome == share_status.SYNCED
    assert status.lifecycle_pending == 1 and status.lifecycle_lost == 0
    rendered = share_status.describe(status)
    assert "Synced decision" in rendered and "lifecycle update" in rendered
    rows = share._load_outbox()
    assert [r["stage"] for r in rows] == ["lifecycle_pending"]
    assert rows[0]["lifecycle"][0]["kind"] == "retired"
    assert rows[0]["capability"] == "v1:r1t1p1"
    assert rows[0]["blocked_reason"]


def test_a_pending_delta_is_not_re_offered_while_the_capability_is_unchanged(
        tmp_repo, wire_open, monkeypatch):
    did = _seed(tmp_repo)
    store.approve_decision(tmp_repo, did, "approve")
    lifecycle.retire_decision(tmp_repo, did, "the queue moved to Kafka")
    lifecycle.restore_decision(tmp_repo, did, "the migration was reverted")
    seen = _refusing_seam(monkeypatch)
    monkeypatch.setattr(share.RemoteStore, "from_profile",
                        staticmethod(lambda p, **kw: RemoteStore("https://t/mcp", "tok")))
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    share.share(tmp_repo, did, profile=TEAM)

    seen["pushes"].clear()
    assert share.drain_outbox(profile=TEAM) == 0
    assert seen["pushes"] == []          # nothing re-asked: the server's answer has not moved
    assert [r["stage"] for r in share._load_outbox()] == ["lifecycle_pending"]


def test_a_pending_delta_is_re_offered_once_the_capability_changes(
        tmp_repo, wire_open, monkeypatch):
    did = _seed(tmp_repo)
    store.approve_decision(tmp_repo, did, "approve")
    lifecycle.retire_decision(tmp_repo, did, "the queue moved to Kafka")
    lifecycle.restore_decision(tmp_repo, did, "the migration was reverted")
    _refusing_seam(monkeypatch)
    monkeypatch.setattr(share.RemoteStore, "from_profile",
                        staticmethod(lambda p, **kw: RemoteStore("https://t/mcp", "tok")))
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    share.share(tmp_repo, did, profile=TEAM)

    # The server ships a fix: a NEW advertisement, and it now accepts the payload.
    seen = _caps_seam(monkeypatch, {"decisionLifecycle": {**_LIFECYCLE_CAPS, "version": 2}})
    assert share.drain_outbox(profile=TEAM) == 1
    assert share._load_outbox() == []
    assert seen["pushes"][0]["lifecycle"][0]["kind"] == "retired"


def test_a_blocked_delta_is_never_quarantined_or_dropped(tmp_repo, wire_open, monkeypatch):
    # Ten drains against a server that keeps refusing: the delta is still there, still complete,
    # and no attempt storm was spent on it.
    did = _seed(tmp_repo)
    store.approve_decision(tmp_repo, did, "approve")
    lifecycle.retire_decision(tmp_repo, did, "the queue moved to Kafka")
    lifecycle.restore_decision(tmp_repo, did, "the migration was reverted")
    seen = _refusing_seam(monkeypatch)
    monkeypatch.setattr(share.RemoteStore, "from_profile",
                        staticmethod(lambda p, **kw: RemoteStore("https://t/mcp", "tok")))
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    share.share(tmp_repo, did, profile=TEAM)

    seen["pushes"].clear()
    for _ in range(10):
        share.drain_outbox(profile=TEAM)
    rows = share._load_outbox()
    assert len(rows) == 1 and rows[0]["stage"] == "lifecycle_pending"
    assert rows[0]["lifecycle"][0]["reason"] == "the queue moved to Kafka"
    assert seen["pushes"] == []


def _queued_offline_row(tmp_repo, monkeypatch):
    """One ordinary queued share whose decision carries completed lifecycle history, sitting in
    the outbox exactly as an offline share leaves it - the state a drain starts from."""
    did = _seed(tmp_repo)
    store.approve_decision(tmp_repo, did, "approve")
    lifecycle.retire_decision(tmp_repo, did, "the queue moved to Kafka")
    lifecycle.restore_decision(tmp_repo, did, "the migration was reverted")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    proj = store.get_shareable(tmp_repo, did)
    share._enqueue(share._payload(proj, "github.com/a/b"))
    assert [r.get("stage") for r in share._load_outbox()] == [None]
    return proj["id"]


def _drain_refusing_seam(monkeypatch, shape):
    """A batch seam that advertises the capability and then refuses any chunk carrying lifecycle,
    in either of the two shapes the fallback is built for."""
    seen = {"pushes": []}

    async def fake(endpoint, token, name, arguments, timeout):
        if name == "get_capabilities":
            return _result(structured={"capabilities": {"decisionLifecycle": _LIFECYCLE_CAPS}})
        seen["pushes"].append(arguments)
        rows = arguments["decisions"]
        carries = any("lifecycle" in d or "revision_id" in d for d in rows)
        if carries and shape == "per_row":
            return _result(structured={
                "results": [],
                "skipped": [{"decisionId": d["decisionId"], "reason": "invalid_lifecycle"}
                            for d in rows]})
        if carries and shape == "whole_call":
            return types.SimpleNamespace(
                content=[_text("MCP error -32602: Input validation error")],
                structuredContent=None, isError=True)
        return _result(structured={
            "results": [{"id": f"srv-{i}", "decisionId": d["decisionId"]}
                        for i, d in enumerate(rows)], "skipped": []})

    monkeypatch.setattr(remote, "_acall_tool", fake)
    monkeypatch.setattr(share.RemoteStore, "from_profile",
                        staticmethod(lambda p, **kw: RemoteStore("https://t/mcp", "tok")))
    return seen


@pytest.mark.parametrize("shape", ["per_row", "whole_call"])
def test_a_delta_refused_DURING_a_drain_survives_the_drain(shape, tmp_repo, wire_open, monkeypatch):
    # The drain both DELIVERS the base and CREATES the pending row for the same decision id, so a
    # purge keyed on that id alone deleted the delta in the same breath as recording it - silently,
    # with the drain reporting success. Both refusal shapes, because the two arrive by different
    # routes (a per-row skip, and a whole-call reject that raises).
    did = _queued_offline_row(tmp_repo, monkeypatch)
    _drain_refusing_seam(monkeypatch, shape)

    assert share.drain_outbox(profile=TEAM) == 1      # the base decision did sync
    rows = share._load_outbox()
    assert [r.get("stage") for r in rows] == ["lifecycle_pending"], (
        "the refused delta must outlive the drain that refused it")
    assert rows[0]["decision_id"] == did
    assert rows[0]["lifecycle"][0]["kind"] == "retired"
    assert rows[0]["capability"] == "v1:r1t1p1"       # what it was refused under
    assert rows[0]["blocked_reason"]                  # why, in the server's words


def test_a_delta_refused_during_a_drain_is_re_offered_once_the_capability_moves(
        tmp_repo, wire_open, monkeypatch):
    # The other half: a row created by the drain path must be reachable by the retry path, not
    # merely present. Surviving into a file nothing re-offers would be quarantine by another name.
    _queued_offline_row(tmp_repo, monkeypatch)
    _drain_refusing_seam(monkeypatch, "per_row")
    share.drain_outbox(profile=TEAM)

    seen = _caps_seam(monkeypatch, {"decisionLifecycle": {**_LIFECYCLE_CAPS, "version": 2}})
    assert share.drain_outbox(profile=TEAM) == 1
    assert share._load_outbox() == []
    assert seen["pushes"][0]["lifecycle"][0]["kind"] == "retired"


def test_a_refused_lifecycle_reason_is_still_scrubbed_on_the_retry_path(
        wire_open, monkeypatch):
    # Redaction is last-mile, so it must hold on BOTH the augmented push and the legacy retry.
    seen = _refusing_seam(monkeypatch)
    rs = RemoteStore("https://t/mcp", "tok")
    rs.push_decision(type="constraint", content="never commit AKIAIOSFODNN7EXAMPLE to main",
                     repo="github.com/a/b", decision_id="dec-1", revision_id="rev-3",
                     lifecycle=[{**_RETIRED, "reason": "rotated AKIAIOSFODNN7EXAMPLE"}])
    assert len(seen["pushes"]) == 2   # augmented refused, then legacy
    assert "AKIAIOSFODNN7EXAMPLE" not in repr(seen["pushes"])


# ── the fallback partition (external review, Issue 3) ────────────────────────────
# `lifecycle_pending` asserts "the base decision is already synced, only its history is
# outstanding". Both fallbacks used to mark every lifecycle-carrying row blocked immediately after
# the legacy retry CALL, before inspecting what came back, so a row the retry itself rejected was
# filed as history-pending for a decision that does not exist remotely. Blocked records are now
# created ONLY for ids the retry confirmed in `results`.

def _partition_seam(monkeypatch, legacy, *, shape="whole_call", augmented=None):
    """Advertising server that refuses any lifecycle-carrying batch in `shape`, then answers the
    legacy retry with `legacy` (a {results, skipped} dict, or a callable taking the sent rows).

    `augmented` overrides the FIRST response, for the per-row reasons the server mixes in.
    """
    seen = {"pushes": [], "legacy_pushes": []}

    async def fake(endpoint, token, name, arguments, timeout):
        if name == "get_capabilities":
            return _result(structured={"capabilities": {"decisionLifecycle": _LIFECYCLE_CAPS}})
        seen["pushes"].append(arguments)
        rows = arguments["decisions"]
        carries = any("lifecycle" in d or "revision_id" in d for d in rows)
        if carries:
            if augmented is not None:
                return _result(structured=augmented(rows) if callable(augmented) else augmented)
            if shape == "per_row":
                return _result(structured={
                    "results": [],
                    "skipped": [{"decisionId": d["decisionId"], "reason": "invalid_lifecycle"}
                                for d in rows]})
            return types.SimpleNamespace(
                content=[_text("MCP error -32602: Input validation error")],
                structuredContent=None, isError=True)
        seen["legacy_pushes"].append(rows)
        return _result(structured=legacy(rows) if callable(legacy) else legacy)

    monkeypatch.setattr(remote, "_acall_tool", fake)
    monkeypatch.setattr(share.RemoteStore, "from_profile",
                        staticmethod(lambda p, **kw: RemoteStore("https://t/mcp", "tok")))
    return seen


def _rows(*ids):
    return [{"type": "constraint", "content": f"rule {i}", "repo": "github.com/a/b",
             "decision_id": i, "revision_id": "rev-3", "lifecycle": [_RETIRED]} for i in ids]


def _push_rows(rs, ids, *, is_async=False):
    if is_async:
        import asyncio
        return asyncio.run(rs.apush_decisions(_rows(*ids)))
    return rs.push_decisions(_rows(*ids))


_SAVED = {"results": [{"id": "srv-1", "decisionId": "dec-1"}], "skipped": []}
_INVALID = {"results": [], "skipped": [{"decisionId": "dec-1", "reason": "invalid_content"}]}
_QUOTA = {"results": [], "skipped": [{"decisionId": "dec-1", "reason": "quota_exceeded"}]}


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("shape", ["whole_call", "per_row"])
def test_a_legacy_retry_that_saved_the_base_records_the_delta_as_blocked(
        shape, is_async, wire_open, monkeypatch):
    _partition_seam(monkeypatch, _SAVED, shape=shape)
    rs = RemoteStore("https://t/mcp", "tok")
    saved, skipped = _push_rows(rs, ["dec-1"], is_async=is_async)
    assert saved == ["srv-1"] and skipped == []
    assert [b["decision_id"] for b in rs.lifecycle_blocked] == ["dec-1"]


@pytest.mark.parametrize("is_async", [False, True], ids=["sync", "async"])
@pytest.mark.parametrize("shape", ["whole_call", "per_row"])
@pytest.mark.parametrize("legacy,reason", [(_INVALID, "invalid_content"),
                                           (_QUOTA, "quota_exceeded")],
                         ids=["permanent", "transient"])
def test_a_legacy_retry_that_did_not_save_the_base_records_nothing_as_blocked(
        shape, is_async, legacy, reason, wire_open, monkeypatch):
    # The defect itself. The base decision does not exist remotely, so there is no synced decision
    # for history to be outstanding ON - the row belongs to its own skip arm, not to lifecycle.
    _partition_seam(monkeypatch, legacy, shape=shape)
    rs = RemoteStore("https://t/mcp", "tok")
    saved, skipped = _push_rows(rs, ["dec-1"], is_async=is_async)
    assert saved == []
    assert skipped == [{"decision_id": "dec-1", "reason": reason}]
    assert rs.lifecycle_blocked == [], "a base that never saved has no history pending"


def test_a_transient_base_failure_keeps_an_ordinary_row_carrying_the_full_lifecycle(
        tmp_repo, wire_open, monkeypatch):
    # The `quota_exceeded` arm end to end: the row stays an ORDINARY base-pending row (never
    # `lifecycle_pending`), and it still carries the complete lifecycle payload, so the delta rides
    # back out with the base once space frees.
    did = _queued_offline_row(tmp_repo, monkeypatch)
    _partition_seam(monkeypatch, {"results": [],
                                  "skipped": [{"decisionId": did, "reason": "quota_exceeded"}]})
    assert share.drain_outbox(profile=TEAM) == 0
    rows = share._load_outbox()
    assert [r.get("stage") for r in rows] == [None], "a base that never synced is not history-pending"
    assert rows[0]["lifecycle"][0]["kind"] == "retired"
    assert rows[0]["revision_id"]


def test_a_permanent_base_rejection_during_a_drain_queues_no_pending_delta(
        tmp_repo, wire_open, monkeypatch):
    did = _queued_offline_row(tmp_repo, monkeypatch)
    _partition_seam(monkeypatch, {"results": [],
                                  "skipped": [{"decisionId": did, "reason": "invalid_content"}]})
    share.drain_outbox(profile=TEAM)
    assert share._load_outbox() == []          # dropped by the permanent-skip contract
    assert not any(r.get("stage") == "lifecycle_pending" for r in share._load_outbox())


def test_a_response_that_does_not_account_for_a_row_fails_the_batch(wire_open, monkeypatch):
    # The unaccounted arm: the server confirmed nothing about this row, so the caller must keep it
    # queued rather than treat silence as either outcome.
    _partition_seam(monkeypatch, {"results": [], "skipped": []})
    rs = RemoteStore("https://t/mcp", "tok")
    with pytest.raises(RemoteStoreError, match="did not account for"):
        rs.push_decisions(_rows("dec-1"))
    assert rs.lifecycle_blocked == []


def test_a_mixed_batch_partitions_every_row_into_its_own_arm(wire_open, monkeypatch):
    # saved + quota + permanent-invalid + lifecycle-blocked, in ONE legacy response.
    legacy = {"results": [{"id": "srv-1", "decisionId": "dec-saved"}],
              "skipped": [{"decisionId": "dec-quota", "reason": "quota_exceeded"},
                          {"decisionId": "dec-bad", "reason": "invalid_type"}]}
    _partition_seam(monkeypatch, legacy)
    rs = RemoteStore("https://t/mcp", "tok")
    saved, skipped = rs.push_decisions(_rows("dec-saved", "dec-quota", "dec-bad"))
    assert saved == ["srv-1"]
    assert {s["decision_id"]: s["reason"] for s in skipped} == {
        "dec-quota": "quota_exceeded", "dec-bad": "invalid_type"}
    assert [b["decision_id"] for b in rs.lifecycle_blocked] == ["dec-saved"]


def test_no_pending_row_exists_whose_base_was_not_in_the_confirmed_saved_results(
        tmp_repo, wire_open, monkeypatch):
    # The property, asserted over the whole outbox rather than one row: every `lifecycle_pending`
    # row's decision id must appear in what the server confirmed it saved.
    did = _queued_offline_row(tmp_repo, monkeypatch)
    confirmed: list[str] = []

    def legacy(rows):
        # Save only the first row offered; anything else is refused permanently.
        out = {"results": [], "skipped": []}
        for i, d in enumerate(rows):
            if i == 0:
                confirmed.append(d["decisionId"])
                out["results"].append({"id": "srv-1", "decisionId": d["decisionId"]})
            else:
                out["skipped"].append({"decisionId": d["decisionId"], "reason": "invalid_content"})
        return out

    _partition_seam(monkeypatch, legacy)
    share.drain_outbox(profile=TEAM)
    pending = [r for r in share._load_outbox() if r.get("stage") == "lifecycle_pending"]
    assert {r["decision_id"] for r in pending} <= set(confirmed)
    assert [r["decision_id"] for r in pending] == [did]


def test_share_all_partitions_the_same_way_as_the_drain(tmp_repo, wire_open, monkeypatch):
    # The direct path, not the outbox one: share_all pushes projections it just read.
    did = _seed(tmp_repo)
    store.approve_decision(tmp_repo, did, "approve")
    lifecycle.retire_decision(tmp_repo, did, "the queue moved to Kafka")
    lifecycle.restore_decision(tmp_repo, did, "the migration was reverted")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    _partition_seam(monkeypatch, {"results": [],
                                  "skipped": [{"decisionId": did, "reason": "invalid_content"}]})
    status = share.share_all(tmp_repo, profile=TEAM)
    assert status.outcome == share_status.BATCH_DONE
    assert status.sent == 0 and status.invalid == 1
    assert "Synced 0 decision(s)" in share_status.describe(status)
    assert not any(r.get("stage") == "lifecycle_pending" for r in share._load_outbox())


def test_share_all_records_the_delta_when_the_base_did_save(tmp_repo, wire_open, monkeypatch):
    did = _seed(tmp_repo)
    store.approve_decision(tmp_repo, did, "approve")
    lifecycle.retire_decision(tmp_repo, did, "the queue moved to Kafka")
    lifecycle.restore_decision(tmp_repo, did, "the migration was reverted")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    _partition_seam(monkeypatch, lambda rows: {
        "results": [{"id": "srv-1", "decisionId": d["decisionId"]} for d in rows], "skipped": []})
    share.share_all(tmp_repo, profile=TEAM)
    rows = share._load_outbox()
    assert [r.get("stage") for r in rows] == ["lifecycle_pending"]
    assert rows[0]["decision_id"] == did


def test_repeated_drains_after_a_partitioned_failure_do_not_storm(tmp_repo, wire_open, monkeypatch):
    # Stability: the transient arm keeps ONE ordinary row and re-offers it once per drain (that is
    # what a transient failure is for), and never mutates into a pending row along the way.
    did = _queued_offline_row(tmp_repo, monkeypatch)
    seen = _partition_seam(monkeypatch, {"results": [],
                                         "skipped": [{"decisionId": did, "reason": "quota_exceeded"}]})
    for _ in range(5):
        share.drain_outbox(profile=TEAM)
    rows = share._load_outbox()
    assert len(rows) == 1 and rows[0].get("stage") is None
    assert len(seen["legacy_pushes"]) == 5     # one legacy retry per drain, never more


# ── lifecycle_conflict: the server refused the whole row on purpose ──────────────
# The Teams side (Issue 4) added a reason that is the OPPOSITE of `invalid_lifecycle`. Nothing of
# the row was saved, so re-pushing the base alone is the resurrection the server just refused.

_CONFLICT_TEXT = ("lifecycle_conflict: event_id ev-1 is already recorded against a different "
                  "decision; the decision was not saved.")


def test_a_batch_lifecycle_conflict_is_never_re_pushed_as_legacy(wire_open, monkeypatch):
    seen = _partition_seam(monkeypatch, _SAVED, augmented=lambda rows: {
        "results": [], "skipped": [{"decisionId": d["decisionId"],
                                    "reason": "lifecycle_conflict"} for d in rows]})
    rs = RemoteStore("https://t/mcp", "tok")
    saved, skipped = rs.push_decisions(_rows("dec-1"))
    assert saved == []
    assert skipped == [{"decision_id": "dec-1", "reason": "lifecycle_conflict"}]
    assert seen["legacy_pushes"] == [], "a contested event id must never trigger a base-only retry"
    assert rs.lifecycle_blocked == []


def test_a_batch_lifecycle_conflict_never_queues_a_pending_delta(tmp_repo, wire_open, monkeypatch):
    did = _queued_offline_row(tmp_repo, monkeypatch)
    _partition_seam(monkeypatch, _SAVED, augmented=lambda rows: {
        "results": [], "skipped": [{"decisionId": d["decisionId"],
                                    "reason": "lifecycle_conflict"} for d in rows]})
    share.drain_outbox(profile=TEAM)
    assert did
    assert not any(r.get("stage") == "lifecycle_pending" for r in share._load_outbox())


def test_a_conflict_row_is_reported_in_its_own_words_not_as_bad_content(wire_open, monkeypatch):
    # The decision's type and content were fine; telling the developer otherwise sends them to
    # edit a decision that has nothing wrong with it.
    retry, invalid, contested = share._split_skips(
        [{"decision_id": "dec-1", "reason": "lifecycle_conflict"}])
    assert retry == set() and invalid == 0 and contested == 1
    status = share_status.ShareStatus(
        share_status.BATCH_DONE, contested=contested, total=contested)
    rendered = share_status.describe(status)
    assert "lifecycle event id is already recorded" in rendered
    assert "unsupported type or content" not in rendered


def test_a_singular_lifecycle_conflict_is_never_retried_as_legacy(wire_open, monkeypatch):
    seen = _refusing_seam(monkeypatch, message=_CONFLICT_TEXT)
    rs = RemoteStore("https://t/mcp", "tok")
    with pytest.raises(RemoteStoreError, match="lifecycle_conflict"):
        _push(rs, revision_id="rev-3", lifecycle=[_RETIRED])
    assert len(seen["pushes"]) == 1, "the base must not be re-pushed without its lifecycle"
    assert rs.lifecycle_blocked == []
    assert rs._lifecycle_caps is not None, "a contested id says nothing about the capability"


def test_a_singular_lifecycle_conflict_queues_no_pending_delta(tmp_repo, wire_open, monkeypatch):
    did = _seed(tmp_repo)
    store.approve_decision(tmp_repo, did, "approve")
    lifecycle.retire_decision(tmp_repo, did, "the queue moved to Kafka")
    lifecycle.restore_decision(tmp_repo, did, "the migration was reverted")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    _refusing_seam(monkeypatch, message=_CONFLICT_TEXT)
    monkeypatch.setattr(share.RemoteStore, "from_profile",
                        staticmethod(lambda p, **kw: RemoteStore("https://t/mcp", "tok")))
    share.share(tmp_repo, did, profile=TEAM)
    assert not any(r.get("stage") == "lifecycle_pending" for r in share._load_outbox())


# ── the outbox stage transition, stated explicitly ──────────────────────────────

def test_an_ordinary_row_does_not_downgrade_a_pending_one(tmp_repo, wire_open, monkeypatch):
    # The dedupe is keyed on decision_id and last write wins, which is right within a stage and
    # wrong across stages: an ordinary row overwriting a pending one discards the capability
    # fingerprint that stops the delta being re-offered against an unchanged server.
    did = _queued_offline_row(tmp_repo, monkeypatch)
    _drain_refusing_seam(monkeypatch, "per_row")
    share.drain_outbox(profile=TEAM)
    assert [r.get("stage") for r in share._load_outbox()] == ["lifecycle_pending"]

    proj = store.get_shareable(tmp_repo, did)
    share._enqueue(share._payload(proj, "github.com/a/b"))    # an ordinary row for the same id
    rows = share._load_outbox()
    assert [r.get("stage") for r in rows] == ["lifecycle_pending"]
    assert rows[0]["capability"] == "v1:r1t1p1"
    assert rows[0]["blocked_reason"]


def test_a_pending_row_replaces_an_ordinary_one_and_a_fresher_pending_one(tmp_repo, wire_open,
                                                                          monkeypatch):
    # The two legitimate directions, both plain replaces.
    did = _queued_offline_row(tmp_repo, monkeypatch)
    _drain_refusing_seam(monkeypatch, "per_row")
    share.drain_outbox(profile=TEAM)
    assert [r.get("stage") for r in share._load_outbox()] == ["lifecycle_pending"]

    share._enqueue({**share._payload(store.get_shareable(tmp_repo, did), "github.com/a/b"),
                    "stage": "lifecycle_pending", "blocked_reason": "a newer refusal",
                    "capability": "v2:r1t1p1"})
    rows = share._load_outbox()
    assert len(rows) == 1 and rows[0]["capability"] == "v2:r1t1p1"
    assert rows[0]["blocked_reason"] == "a newer refusal"
