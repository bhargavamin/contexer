"""Tests for C4 share: store.get_shareable + contexer/share.py.

RemoteStore is faked (monkeypatched from_profile) so no network is touched. The team
profile is passed explicitly to share() to avoid reading a real config.toml.
"""
import asyncio
import contextlib
import threading
import time
import types

import pytest

import contexer.remote as remote
from contexer import config, share, share_status, store
from contexer.remote import (
    RemoteAuthError,
    RemoteRateLimitError,
    RemoteUnavailableError,
)

TEAM = config.Profile(mode="team", endpoint="https://t/mcp", token="tok")


class _FakeRS:
    def __init__(self, ret="srv-1", exc=None):
        self.ret, self.exc, self.calls = ret, exc, []
        self.batches = []  # push_decisions arg-lists (bulk path: share_all / share_ids / drain)

    def push_decision(self, **kw):
        self.calls.append(kw)
        if self.exc is not None:
            raise self.exc
        return self.ret

    def push_decisions(self, kwargs_list):
        self.batches.append(kwargs_list)
        if self.exc is not None:
            raise self.exc
        return [f"srv-{i}" for i in range(len(kwargs_list))], []  # (saved_ids, skipped_ids)


def _fake(monkeypatch, **kw):
    fake = _FakeRS(**kw)
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: fake))
    remote.reset_degradation_warnings()
    return fake


class _AsyncFakeRS:
    """The async twin of _FakeRS: the in-loop share path awaits ``apush_decision``/``apush_decisions``."""

    def __init__(self, ret="srv-1", exc=None):
        self.ret, self.exc, self.calls = ret, exc, []
        self.batches = []

    async def apush_decision(self, **kw):
        self.calls.append(kw)
        if self.exc is not None:
            raise self.exc
        return self.ret

    async def apush_decisions(self, kwargs_list):
        self.batches.append(kwargs_list)
        if self.exc is not None:
            raise self.exc
        return [f"srv-{i}" for i in range(len(kwargs_list))], []


def _afake(monkeypatch, **kw):
    fake = _AsyncFakeRS(**kw)
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: fake))
    remote.reset_degradation_warnings()
    return fake


# ── store.get_shareable ──────────────────────────────────────────────────────────

def test_get_shareable_none_when_empty(tmp_repo):
    assert store.get_shareable(tmp_repo) is None


def test_get_shareable_latest_by_default(tmp_repo):
    _, id1 = store.update_decision(tmp_repo, "first decision here", "s1", subtype="architecture")
    _, id2 = store.update_decision(tmp_repo, "second newer decision", "s1", subtype="constraint")
    dec = store.get_shareable(tmp_repo)
    assert dec["id"] == id2
    assert dec["type"] == "constraint"


def test_get_shareable_by_id_prefix(tmp_repo):
    _, id1 = store.update_decision(tmp_repo, "alpha decision content", "s1", subtype="architecture")
    store.update_decision(tmp_repo, "beta decision content", "s1", subtype="constraint")
    dec = store.get_shareable(tmp_repo, id1[:8])
    assert dec["id"] == id1
    assert dec["type"] == "architecture"


def test_get_shareable_not_found(tmp_repo):
    store.update_decision(tmp_repo, "some decision content", "s1", subtype="architecture")
    assert store.get_shareable(tmp_repo, "no-such-id") is None


def test_get_shareable_excludes_ignored(tmp_repo):
    _, id1 = store.update_decision(tmp_repo, "keep this decision here", "s1", subtype="architecture")
    _, id2 = store.update_decision(tmp_repo, "ignore this one please now", "s1", subtype="constraint")
    store.approve_decision(tmp_repo, id2, "ignore")
    assert store.get_shareable(tmp_repo)["id"] == id1  # latest non-ignored


def test_get_shareable_carries_provenance(tmp_repo):
    _, id1 = store.update_decision(tmp_repo, "decision with provenance data", "s1", subtype="architecture")
    dec = store.get_shareable(tmp_repo, id1)
    assert dec["source"] == "ai"
    assert isinstance(dec["confidence"], int)
    assert dec["evidence"] is None or isinstance(dec["evidence"], list)


# ── store.get_shareable_all ──────────────────────────────────────────────────────

def test_get_shareable_all_empty(tmp_repo):
    assert store.get_shareable_all(tmp_repo) == []


def test_get_shareable_all_returns_all_non_ignored(tmp_repo):
    _, id1 = store.update_decision(tmp_repo, "first decision here", "s1", subtype="architecture")
    _, id2 = store.update_decision(tmp_repo, "second newer decision", "s1", subtype="constraint")
    _, id3 = store.update_decision(tmp_repo, "ignore this one please now", "s1", subtype="pattern")
    store.approve_decision(tmp_repo, id3, "ignore")
    decs = store.get_shareable_all(tmp_repo)
    assert [d["id"] for d in decs] == [id1, id2]  # oldest first, ignored excluded
    assert decs[0]["type"] == "architecture"
    assert decs[1]["type"] == "constraint"


def test_get_shareable_all_order_unaffected_by_shared_marker(tmp_repo):
    # Guard against a future refactor sorting get_shareable_all by shared-status: it controls
    # the actual PUSH order (server's updatedSince consumers see decisions chronologically),
    # which is independent of - and must never be reordered by - the picker's display sort.
    _, id1 = store.update_decision(tmp_repo, "first decision here", "s1", subtype="architecture")
    _, id2 = store.update_decision(tmp_repo, "second newer decision", "s1", subtype="constraint")
    _, id3 = store.update_decision(tmp_repo, "third newest decision", "s1", subtype="convention")
    share._mark_shared([id1, id3], "https://t/mcp")  # mark the OLDEST and NEWEST as shared
    decs = store.get_shareable_all(tmp_repo)
    assert [d["id"] for d in decs] == [id1, id2, id3]  # still strictly oldest-first, unsorted


# ── share.share ──────────────────────────────────────────────────────────────────

def test_share_nothing_to_share(tmp_repo):
    assert share.share(tmp_repo, profile=TEAM).outcome == share_status.NO_MATCH


def test_share_local_mode_message(tmp_repo, monkeypatch):
    store.update_decision(tmp_repo, "a decision to maybe share", "s1", subtype="architecture")
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: None))
    assert share.share(
        tmp_repo, profile=config.Profile()).outcome == share_status.NOT_TEAM_MODE


def test_share_happy_path_wire_args(tmp_repo, monkeypatch):
    _, did = store.update_decision(tmp_repo, "use postgres for storage", "s1", subtype="architecture")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    fake = _fake(monkeypatch, ret="srv-9")
    msg = share.share(tmp_repo, profile=TEAM)
    assert msg.outcome == share_status.SYNCED
    assert msg.server_id == "srv-9"
    assert (msg.sent, msg.total) == (1, 1)
    rendered = share_status.describe(msg)      # the wording is the renderer's job now
    assert "personal" in rendered.lower()
    assert "won't see" in rendered.lower()     # honest about visibility
    assert len(fake.calls) == 1
    kw = fake.calls[0]
    assert kw["type"] == "architecture"
    assert "postgres" in kw["content"].lower()
    assert kw["repo"] == "github.com/a/b"
    assert kw["decision_id"] == did  # local id -> idempotent re-share
    assert kw["source"] == "ai"


def test_share_happy_path_includes_title(tmp_repo, monkeypatch):
    store.update_decision(tmp_repo, "use postgres for storage", "s1",
                          subtype="architecture", title="Storage: Postgres")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    fake = _fake(monkeypatch, ret="srv-9")
    share.share(tmp_repo, profile=TEAM)
    assert fake.calls[0]["title"] == "Storage: Postgres"


def test_share_no_git_origin_pushes_repo_none(tmp_repo, monkeypatch):
    store.update_decision(tmp_repo, "decision without a remote origin", "s1", subtype="constraint")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    fake = _fake(monkeypatch, ret="srv-x")
    assert share.share(tmp_repo, profile=TEAM).server_id == "srv-x"
    assert fake.calls[0]["repo"] is None


def test_share_by_id_prefix(tmp_repo, monkeypatch):
    _, id1 = store.update_decision(tmp_repo, "alpha shareable decision", "s1", subtype="architecture")
    store.update_decision(tmp_repo, "beta newer decision here", "s1", subtype="constraint")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    fake = _fake(monkeypatch, ret="srv-a")
    share.share(tmp_repo, id1[:8], profile=TEAM)
    assert fake.calls[0]["decision_id"] == id1  # shared the requested one, not the latest


def test_share_degraded_unreachable(tmp_repo, monkeypatch, capsys):
    store.update_decision(tmp_repo, "decision that fails to sync", "s1", subtype="architecture")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    _fake(monkeypatch, exc=RemoteUnavailableError("down"))
    msg = share.share(tmp_repo, profile=TEAM)
    assert msg.outcome == share_status.QUEUED and msg.queued == 1
    assert "unreachable" in capsys.readouterr().err.lower()


def test_share_degraded_auth(tmp_repo, monkeypatch, capsys):
    store.update_decision(tmp_repo, "decision with bad token sync", "s1", subtype="architecture")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    _fake(monkeypatch, exc=RemoteAuthError("401"))
    assert share.share(tmp_repo, profile=TEAM).outcome == share_status.QUEUED
    err = capsys.readouterr().err
    assert "contexer login" in err and "--team" not in err  # a flag that never existed


def test_reconcile_previews_then_atomically_submits_team_candidate(tmp_repo, monkeypatch):
    _, did = store.update_decision(
        tmp_repo, "scope adapter rule to new assistant targets", "s1", subtype="constraint")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")

    class Fake:
        def __init__(self):
            self.events = []

        def list_teams(self):
            return [remote.RemoteTeam("team-1", "Platform", "member")]

        def get_capabilities(self):
            return remote.ServerCapabilities(remote.DecisionReconciliationCapabilities(
                1, True, True, False))

        def preview_decision_reconciliation(self, decision_id, team_id, **decision):
            self.events.append(("preview", decision_id, team_id, decision))
            return remote.DecisionReconciliationPreview(
                "personal-head", "team-head", None, "diverged", "submit_update", [],
                ["submit"], remote.RemoteTeam("team-1", "Platform", "member"))

        def submit_team_decision(self, decision_id, revision_id, team_id, **kwargs):
            self.events.append(("submit", decision_id, revision_id, team_id, kwargs))
            return remote.TeamSubmissionResult(
                "submitted", "update", "personal-new", "team-head", "candidate-1",
                revision_id, False, remote.RemoteTeam("team-1", "Platform", "member"))

    fake = Fake()
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: fake))
    out = share.reconcile(tmp_repo, did[:8], profile=TEAM)

    assert out.outcome == share_status.SUBMITTED
    assert (out.candidate_id, out.team_name) == ("candidate-1", "Platform")
    assert fake.events[0][:3] == ("preview", did, "team-1")
    assert fake.events[1][0] == "submit" and fake.events[1][1] == did
    assert fake.events[1][2] and fake.events[1][3] == "team-1"
    assert fake.events[1][4]["expected_personal_head"] == "personal-head"
    assert fake.events[1][4]["expected_team_head"] == "team-head"
    assert fake.events[1][4]["idempotency_key"]


def test_reconcile_requires_explicit_team_when_memberships_are_ambiguous(tmp_repo, monkeypatch):
    _, did = store.update_decision(tmp_repo, "use the shared deployment helper", "s1",
                                   subtype="convention")

    class Fake:
        def get_capabilities(self):
            raise remote.RemoteStoreError("Unknown tool: get_capabilities")

        def list_teams(self):
            return [
                remote.RemoteTeam("t-1", "Platform", "member"),
                remote.RemoteTeam("t-2", "Security", "member"),
            ]

    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: Fake()))
    out = share.reconcile(tmp_repo, did, profile=TEAM)
    assert out.outcome == share_status.TEAM_CHOICE_REQUIRED
    assert out.teams == ("Platform (t-1)", "Security (t-2)")


def test_cli_reconcile_confirms_and_routes_team(monkeypatch, capsys):
    from contexer import cli
    monkeypatch.setattr(store, "git_root", lambda p: "/repo")
    monkeypatch.setattr(config, "load_profile", lambda: TEAM)
    monkeypatch.setattr("builtins.input", lambda *a: "y")
    captured = {}
    plan = object()
    monkeypatch.setattr(share, "prepare_reconciliation", lambda repo, did, team, **kwargs:
                        (captured.update(repo=repo, did=did, team=team), (plan, None))[1])
    monkeypatch.setattr(share, "format_reconciliation_preview", lambda p: "server preview")
    monkeypatch.setattr(share, "submit_reconciliation", lambda p, **kwargs:
                        share_status.ReconcileStatus(
                            share_status.SUBMITTED, team_name="Platform", candidate_id="cand-1"))

    cli.reconcile_cmd(["abc12345", "--team", "Platform"])
    assert captured == {"repo": "/repo", "did": "abc12345", "team": "Platform"}
    output = capsys.readouterr().out
    assert "server preview" in output and "Submitted decision cand-1" in output


def test_confirmed_atomic_reconciliation_queues_one_stable_operation(tmp_repo, monkeypatch):
    secret = "AKIAIOSFODNN7EXAMPLE"
    _, did = store.update_decision(
        tmp_repo, f"keep atomic retries identical with {secret}", "s1", subtype="constraint")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")

    class Fake:
        def list_teams(self):
            return [remote.RemoteTeam("team-1", "Platform", "member")]

        def get_capabilities(self):
            return remote.ServerCapabilities(remote.DecisionReconciliationCapabilities(
                1, True, True, False))

        def preview_decision_reconciliation(self, decision_id, team_id, **decision):
            return remote.DecisionReconciliationPreview(
                "ph-1", "th-1", None, "diverged", "submit_update", [], ["submit"],
                remote.RemoteTeam(team_id, "Platform", "member"))

        def submit_team_decision(self, *args, **kwargs):
            raise RemoteUnavailableError("offline")

    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: Fake()))
    out = share.reconcile(tmp_repo, did, profile=TEAM)
    queued = share._load_reconcile_outbox()
    assert out.outcome == share_status.UNREACHABLE_QUEUED and len(queued) == 1
    assert queued[0]["decision_id"] == did
    assert queued[0]["expected_personal_head"] == "ph-1"
    assert queued[0]["expected_team_head"] == "th-1"
    assert queued[0]["idempotency_key"]
    assert queued[0]["stage"] == "confirmed"
    assert secret not in queued[0]["payload"]["content"]
    assert "[REDACTED:aws_key]" in queued[0]["payload"]["content"]


def test_heads_changed_is_not_blindly_queued(tmp_repo, monkeypatch):
    _, did = store.update_decision(tmp_repo, "review moved heads first", "s1",
                                   subtype="constraint")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)

    class Fake:
        def list_teams(self):
            return [remote.RemoteTeam("team-1", "Platform", "member")]

        def get_capabilities(self):
            return remote.ServerCapabilities(remote.DecisionReconciliationCapabilities(
                1, True, True, False))

        def preview_decision_reconciliation(self, decision_id, team_id, **decision):
            return remote.DecisionReconciliationPreview(
                "ph-1", "th-1", None, "diverged", "submit_update", [], ["submit"],
                remote.RemoteTeam(team_id, "Platform", "member"))

        def submit_team_decision(self, decision_id, revision_id, team_id, **kwargs):
            return remote.TeamSubmissionResult(
                "heads_changed", "update", "ph-2", "th-2", None, revision_id, False,
                remote.RemoteTeam(team_id, "Platform", "member"))

    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: Fake()))
    out = share.reconcile(tmp_repo, did, profile=TEAM)
    assert out.outcome == share_status.HEADS_CHANGED
    assert share._load_reconcile_outbox() == []


def test_reconcile_falls_back_for_server_without_capability_tool(tmp_repo, monkeypatch):
    _, did = store.update_decision(tmp_repo, "support the older team server", "s1",
                                   subtype="constraint")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)

    class Fake:
        def __init__(self):
            self.events = []

        def list_teams(self):
            return [remote.RemoteTeam("team-1", "Platform", "member")]

        def get_capabilities(self):
            raise remote.RemoteStoreError("Unknown tool: get_capabilities")

        def push_decision(self, **kwargs):
            self.events.append("push")
            return "personal-1"

        def submit_decision_to_team(self, decision_id, team_id):
            self.events.append("submit")
            return remote.TeamShareResult(
                "submitted", "update", "candidate-1",
                remote.RemoteTeam(team_id, "Platform", "member"))

    fake = Fake()
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: fake))
    out = share.reconcile(tmp_repo, did, profile=TEAM)
    assert fake.events == ["push", "submit"]
    assert out.outcome == share_status.SUBMITTED and out.candidate_id == "candidate-1"


def test_reconcile_falls_back_with_explicit_team_when_discovery_is_missing(
        tmp_repo, monkeypatch):
    _, did = store.update_decision(tmp_repo, "support team-id fallback", "s1",
                                   subtype="constraint")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)

    class Fake:
        def __init__(self):
            self.events = []

        def get_capabilities(self):
            raise remote.RemoteStoreError("Unknown tool: get_capabilities")

        def list_teams(self):
            raise remote.RemoteStoreError("Unknown tool: list_teams")

        def push_decision(self, **kwargs):
            self.events.append(("push", kwargs["decision_id"]))
            return "personal-1"

        def submit_decision_to_team(self, decision_id, team_id):
            self.events.append(("submit", decision_id, team_id))
            return remote.TeamShareResult(
                "submitted", "update", "candidate-1",
                remote.RemoteTeam(team_id, team_id, "member"))

    fake = Fake()
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: fake))
    out = share.reconcile(tmp_repo, did, team="team-legacy", profile=TEAM)
    assert fake.events == [("push", did), ("submit", did, "team-legacy")]
    assert out.outcome == share_status.SUBMITTED and out.candidate_id == "candidate-1"


def test_reconcile_falls_back_with_explicit_team_when_capabilities_are_generic(
        tmp_repo, monkeypatch):
    _, did = store.update_decision(tmp_repo, "support generic capability servers", "s1",
                                   subtype="constraint")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)

    class Fake:
        def __init__(self):
            self.events = []

        def get_capabilities(self):
            return remote.ServerCapabilities(None)

        def list_teams(self):
            raise remote.RemoteStoreError("Unknown tool: list_teams")

        def push_decision(self, **kwargs):
            self.events.append(("push", kwargs["decision_id"]))
            return "personal-1"

        def submit_decision_to_team(self, decision_id, team_id):
            self.events.append(("submit", decision_id, team_id))
            return remote.TeamShareResult(
                "submitted", "update", "candidate-1",
                remote.RemoteTeam(team_id, team_id, "member"))

    fake = Fake()
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: fake))
    out = share.reconcile(tmp_repo, did, team="team-legacy", profile=TEAM)
    assert fake.events == [("push", did), ("submit", did, "team-legacy")]
    assert out.outcome == share_status.SUBMITTED and out.candidate_id == "candidate-1"


def test_reconciliation_drain_reuses_payload_and_idempotency_key(tmp_repo, monkeypatch):
    operation = {
        "operation": "submit_team_decision", "idempotency_key": "idem-stable",
        "decision_id": "d1", "revision_id": "r1", "team_id": "t1",
        "team_name": "Platform", "expected_personal_head": "ph1",
        "expected_team_head": "th1", "payload": {"type": "constraint", "content": "new"},
        "stage": "confirmed", "attempts": 0,
    }
    share._enqueue_reconciliation(operation)

    class Fake:
        def __init__(self):
            self.calls = []

        def get_capabilities(self):
            return remote.ServerCapabilities(remote.DecisionReconciliationCapabilities(
                1, True, True, False))

        def submit_team_decision(self, decision_id, revision_id, team_id, **kwargs):
            self.calls.append((decision_id, revision_id, team_id, kwargs))
            return remote.TeamSubmissionResult(
                "already_pending", "update", "ph2", "th1", "c1", revision_id, True,
                remote.RemoteTeam(team_id, "Platform", "member"))

    fake = Fake()
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: fake))
    assert share.drain_outbox(TEAM) == 1
    assert fake.calls[0][3]["idempotency_key"] == "idem-stable"
    assert fake.calls[0][3]["expected_personal_head"] == "ph1"
    assert fake.calls[0][3]["content"] == "new"
    assert share._load_reconcile_outbox() == []


def _atomic_plan(monkeypatch, *, raises):
    """A previewed atomic ReconciliationPlan whose submit raises `raises`."""
    class Fake:
        def submit_team_decision(self, *a, **k):
            raise raises

    preview = remote.DecisionReconciliationPreview(
        "ph-1", "th-1", None, "diverged", "submit_update", [], ["submit"],
        remote.RemoteTeam("t1", "Platform", "member"))
    return share.ReconciliationPlan(
        {"id": "d1", "revision_id": "r1", "type": "constraint", "content": "c",
         "confidence": None, "evidence": None, "source": "ai"},
        None, remote.RemoteTeam("t1", "Platform", "member"), Fake(), preview,
        True, "idem-1", False)


def _queue_one_confirmed_operation():
    share._enqueue_reconciliation({
        "operation": "submit_team_decision", "idempotency_key": "idem-stable",
        "decision_id": "d1", "revision_id": "r1", "team_id": "t1",
        "team_name": "Platform", "expected_personal_head": "ph1",
        "expected_team_head": "th1", "payload": {"type": "constraint", "content": "new"},
        "stage": "confirmed", "attempts": 0,
    })


def _drain_refusing(monkeypatch, exc):
    """Drain the reconciliation queue against a server that refuses with `exc`."""

    class Fake:
        def get_capabilities(self):
            return remote.ServerCapabilities(remote.DecisionReconciliationCapabilities(
                1, True, True, False))

        def submit_team_decision(self, *args, **kwargs):
            raise exc

    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: Fake()))
    return share.drain_outbox(TEAM)


def test_rate_limited_reconciliation_stays_queued_for_retry(tmp_repo, monkeypatch):
    """The retry-versus-drop choice for a CONFIRMED team write. A rate limit is transient, so the
    entry keeps its `confirmed` stage and its stable idempotency key and the drain stops - a later
    drain resubmits it. This was decided by matching the words "rate" and "limit" in the server's
    error text, so one rephrase upstream would have marked the write terminal and stopped
    retrying it; it is now decided by the error's TYPE."""
    _queue_one_confirmed_operation()
    assert _drain_refusing(monkeypatch, RemoteRateLimitError("Rate limit exceeded")) == 0
    queued = share._load_reconcile_outbox()
    assert len(queued) == 1
    assert queued[0]["stage"] == "confirmed"     # NOT "attention" - it will be retried
    assert queued[0]["attempts"] == 1
    assert queued[0]["idempotency_key"] == "idem-stable"


def test_every_transient_refusal_class_is_queued_by_submit_reconciliation(tmp_repo, monkeypatch):
    """One table, both decision sites. The drain asked `_RETRYABLE_REFUSALS` while
    `submit_reconciliation` re-enumerated the same classes in its own except-cascade, so a fourth
    transient class added to the set would have been reported as terminal here - "Nothing was
    queued" for a write the developer had already confirmed. This walks the table itself, so
    adding a class covers it automatically."""
    assert set(share._RETRYABLE_REFUSALS) == set(share._TRANSIENT_REFUSALS)
    for cls, (queued_outcome, _stranded) in share._TRANSIENT_REFUSALS.items():
        share._save_reconcile_outbox([])
        plan = _atomic_plan(monkeypatch, raises=cls("nope"))
        status = share.submit_reconciliation(plan, profile=TEAM)
        assert status.outcome == queued_outcome, cls
        assert len(share._load_reconcile_outbox()) == 1, cls


def test_a_new_transient_class_needs_no_change_in_submit_reconciliation(tmp_repo, monkeypatch):
    """The point of the table: registering a class is the whole change. Without it this test sees
    SUBMISSION_REFUSED and an empty queue."""
    class NewlyTransient(remote.RemoteStoreError):
        pass

    monkeypatch.setitem(share._TRANSIENT_REFUSALS, NewlyTransient,
                        (share_status.UNREACHABLE_QUEUED, share_status.UNREACHABLE_NOT_QUEUED))
    plan = _atomic_plan(monkeypatch, raises=NewlyTransient("brand new"))
    status = share.submit_reconciliation(plan, profile=TEAM)
    assert status.outcome == share_status.UNREACHABLE_QUEUED
    assert len(share._load_reconcile_outbox()) == 1


def test_terminal_refusal_moves_the_reconciliation_to_attention(tmp_repo, monkeypatch):
    """The other side of the same branch: a refusal that is not transient stops being retried and
    records why, so the developer sees it in the queue instead of it looping forever."""
    _queue_one_confirmed_operation()
    assert _drain_refusing(monkeypatch, remote.RemoteStoreError("invalid revision")) == 0
    queued = share._load_reconcile_outbox()
    assert len(queued) == 1
    assert queued[0]["stage"] == "attention"
    assert queued[0]["reason"] == "invalid revision"


def test_reconciliation_outbox_full_refuses_new_distinct_operation(tmp_repo, monkeypatch):
    monkeypatch.setattr(share, "_OUTBOX_CAP", 2)
    for did in ("d1", "d2"):
        share._enqueue_reconciliation({
            "operation": "submit_team_decision", "idempotency_key": f"idem-{did}",
            "decision_id": did, "revision_id": f"r-{did}", "team_id": "t1",
            "team_name": "Platform", "payload": {"type": "constraint", "content": did},
        })
    with pytest.raises(RuntimeError, match="queue is full"):
        share._enqueue_reconciliation({
            "operation": "submit_team_decision", "idempotency_key": "idem-d3",
            "decision_id": "d3", "revision_id": "r-d3", "team_id": "t1",
            "team_name": "Platform", "payload": {"type": "constraint", "content": "d3"},
        })
    assert [e["decision_id"] for e in share._load_reconcile_outbox()] == ["d1", "d2"]


def test_reconciliation_enqueue_refuses_corrupt_queue_without_overwriting(tmp_repo):
    path = share._reconcile_outbox_path()
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")

    with pytest.raises(RuntimeError, match="cannot read reconciliation retry queue"):
        share._enqueue_reconciliation({
            "operation": "submit_team_decision", "idempotency_key": "idem-d1",
            "decision_id": "d1", "revision_id": "r-d1", "team_id": "t1",
            "team_name": "Platform", "payload": {"type": "constraint", "content": "d1"},
        })
    assert path.read_text(encoding="utf-8") == "{not json"


def test_discard_outbox_also_clears_confirmed_reconciliations(tmp_repo):
    share._enqueue_reconciliation({
        "decision_id": "d1", "team_id": "t1", "idempotency_key": "i1",
        "stage": "confirmed"})
    assert share.discard_outbox() == (1, 0)
    assert share._load_reconcile_outbox() == []


# ── share.share_all ──────────────────────────────────────────────────────────────

def test_share_all_nothing_to_share(tmp_repo):
    assert share.share_all(tmp_repo, profile=TEAM).outcome == share_status.NOTHING_TO_SHARE


def test_share_all_local_mode_message(tmp_repo, monkeypatch):
    store.update_decision(tmp_repo, "a decision to maybe share", "s1", subtype="architecture")
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: None))
    assert share.share_all(
        tmp_repo, profile=config.Profile()).outcome == share_status.NOT_TEAM_MODE


def test_share_all_happy_path_pushes_every_decision(tmp_repo, monkeypatch):
    _, id1 = store.update_decision(tmp_repo, "use postgres for storage", "s1", subtype="architecture")
    _, id2 = store.update_decision(tmp_repo, "never commit secrets ever", "s1", subtype="constraint")
    _, id3 = store.update_decision(tmp_repo, "snake case file naming", "s1", subtype="convention")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    fake = _fake(monkeypatch, ret="srv-9")
    msg = share.share_all(tmp_repo, profile=TEAM)
    assert msg.outcome == share_status.BATCH_DONE
    assert (msg.sent, msg.total) == (3, 3)
    assert (msg.at_capacity, msg.invalid, msg.lost) == (0, 0, 0)
    assert len(fake.batches) == 1  # ONE network call for all three, not one per decision
    assert [c["decision_id"] for c in fake.batches[0]] == [id1, id2, id3]  # oldest first
    assert all(c["repo"] == "github.com/a/b" for c in fake.batches[0])
    assert share._load_outbox() == []


def test_share_all_happy_path_includes_title(tmp_repo, monkeypatch):
    store.update_decision(tmp_repo, "use postgres for storage", "s1",
                          subtype="architecture", title="Storage: Postgres")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    fake = _fake(monkeypatch, ret="srv-9")
    share.share_all(tmp_repo, profile=TEAM)
    assert fake.batches[0][0]["title"] == "Storage: Postgres"


def test_share_all_excludes_ignored(tmp_repo, monkeypatch):
    _, id1 = store.update_decision(tmp_repo, "keep this decision here", "s1", subtype="architecture")
    _, id2 = store.update_decision(tmp_repo, "ignore this one please now", "s1", subtype="constraint")
    store.approve_decision(tmp_repo, id2, "ignore")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    fake = _fake(monkeypatch, ret="srv-1")
    share.share_all(tmp_repo, profile=TEAM)
    assert [c["decision_id"] for c in fake.batches[0]] == [id1]


def test_share_all_failure_enqueues_failed_and_remaining(tmp_repo, monkeypatch):
    """First push succeeds, second fails: the failed decision AND everything after it
    are queued for retry - stop pushing, the cloud is likely down (drain semantics)."""
    _, id1 = store.update_decision(tmp_repo, "use postgres for storage", "s1", subtype="architecture")
    _, id2 = store.update_decision(tmp_repo, "never commit secrets ever", "s1", subtype="constraint")
    _, id3 = store.update_decision(tmp_repo, "snake case file naming", "s1", subtype="convention")

    class _FlakyRS:
        def __init__(self):
            self.batches = []

        def push_decisions(self, kwargs_list):
            self.batches.append(kwargs_list)
            if any(kw["decision_id"] == id2 for kw in kwargs_list):
                raise RemoteUnavailableError("down")
            return [f"srv-{i}" for i in range(len(kwargs_list))], []

    fake = _FlakyRS()
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: fake))
    remote.reset_degradation_warnings()
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    monkeypatch.setattr(share, "_BATCH_SIZE", 1)  # one decision per chunk -> partial progress
    msg = share.share_all(tmp_repo, profile=TEAM)
    assert msg.outcome == share_status.BATCH_INTERRUPTED
    assert (msg.sent, msg.queued, msg.total) == (1, 2, 3)   # 1 synced, the rest queued
    assert [b[0]["decision_id"] for b in fake.batches] == [id1, id2]  # stopped after the failing chunk
    assert [e["decision_id"] for e in share._load_outbox()] == [id2, id3]


def test_share_all_partial_enqueue_failure_message_is_accurate(tmp_repo, monkeypatch):
    """If the outbox write itself dies mid-queue, the message must state exactly how
    many made it into the outbox rather than claim none did (review finding, PR #95)."""
    _, id1 = store.update_decision(tmp_repo, "use postgres for storage", "s1",
                                   subtype="architecture")
    store.update_decision(tmp_repo, "never commit secrets ever", "s1", subtype="constraint")
    store.update_decision(tmp_repo, "snake case file naming", "s1", subtype="convention")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    _fake(monkeypatch, exc=RemoteUnavailableError("down"))  # first push fails -> queue all 3
    real_enqueue = share._enqueue
    calls = {"n": 0}

    def flaky_enqueue(payload):
        if calls["n"] >= 1:
            raise OSError("disk full")
        calls["n"] += 1
        real_enqueue(payload)

    monkeypatch.setattr(share, "_enqueue", flaky_enqueue)
    msg = share.share_all(tmp_repo, profile=TEAM)
    assert msg.outcome == share_status.BATCH_STRANDED
    # The count no caller could read before: 1 queued, so 2 of the 3 are recorded NOWHERE.
    assert (msg.sent, msg.queued, msg.lost, msg.total) == (0, 1, 2, 3)
    assert [e["decision_id"] for e in share._load_outbox()] == [id1]


def test_share_all_total_failure_queues_everything(tmp_repo, monkeypatch):
    _, id1 = store.update_decision(tmp_repo, "use postgres for storage", "s1", subtype="architecture")
    _, id2 = store.update_decision(tmp_repo, "never commit secrets ever", "s1", subtype="constraint")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    _fake(monkeypatch, exc=RemoteUnavailableError("down"))
    msg = share.share_all(tmp_repo, profile=TEAM)
    assert msg.outcome == share_status.BATCH_INTERRUPTED
    assert (msg.sent, msg.queued, msg.lost) == (0, 2, 0)
    assert [e["decision_id"] for e in share._load_outbox()] == [id1, id2]


# ── outbox: enqueue on share ──────────────────────────────────────────────────────

def test_share_degraded_enqueues_payload(tmp_repo, monkeypatch):
    _, did = store.update_decision(tmp_repo, "decision that fails to sync", "s1", subtype="architecture")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    _fake(monkeypatch, exc=RemoteUnavailableError("down"))
    msg = share.share(tmp_repo, profile=TEAM)
    assert msg.outcome == share_status.QUEUED and msg.queued == 1
    entries = share._load_outbox()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["decision_id"] == did
    assert entry["type"] == "architecture"
    assert "sync" in entry["content"].lower()
    assert entry["repo"] == "github.com/a/b"
    assert entry["source"] == "ai"
    assert entry["attempts"] == 0
    assert isinstance(entry["queued_at"], float)


def test_share_degraded_enqueue_preserves_title(tmp_repo, monkeypatch):
    # A queued offline share must carry the decision's title into the outbox row so a
    # later drain can still send it (round-trip guarantee, Decision Titles v2 Task 4).
    store.update_decision(tmp_repo, "decision that fails to sync", "s1",
                          subtype="architecture", title="Sync failure heading")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    _fake(monkeypatch, exc=RemoteUnavailableError("down"))
    share.share(tmp_repo, profile=TEAM)
    entry = share._load_outbox()[0]
    assert entry["title"] == "Sync failure heading"


def test_share_degraded_auth_also_enqueues(tmp_repo, monkeypatch):
    store.update_decision(tmp_repo, "decision with bad token sync", "s1", subtype="constraint")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    _fake(monkeypatch, exc=RemoteAuthError("401"))
    share.share(tmp_repo, profile=TEAM)
    assert len(share._load_outbox()) == 1  # auth failures enqueue too -- retry after re-login


def test_share_nothing_to_share_does_not_enqueue(tmp_repo):
    share.share(tmp_repo, profile=TEAM)
    assert share._load_outbox() == []


def test_share_local_mode_does_not_enqueue(tmp_repo, monkeypatch):
    store.update_decision(tmp_repo, "a decision to maybe share", "s1", subtype="architecture")
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: None))
    share.share(tmp_repo, profile=config.Profile())
    assert share._load_outbox() == []


def test_share_happy_path_does_not_enqueue(tmp_repo, monkeypatch):
    store.update_decision(tmp_repo, "use postgres for storage", "s1", subtype="architecture")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    _fake(monkeypatch, ret="srv-9")
    share.share(tmp_repo, profile=TEAM)
    assert share._load_outbox() == []


# ── outbox: _enqueue dedupe + cap ─────────────────────────────────────────────────

def test_enqueue_dedupe_by_decision_id_replaces(tmp_repo):
    share._enqueue({"decision_id": "d1", "content": "stale content", "attempts": 0})
    share._enqueue({"decision_id": "d1", "content": "fresh content", "attempts": 0})
    entries = share._load_outbox()
    assert len(entries) == 1
    assert entries[0]["content"] == "fresh content"


def test_enqueue_caps_at_50_drops_oldest(tmp_repo):
    for i in range(55):
        share._enqueue({"decision_id": f"d{i}", "content": f"c{i}", "attempts": 0})
    entries = share._load_outbox()
    assert len(entries) == 50
    ids = [e["decision_id"] for e in entries]
    assert "d0" not in ids  # oldest 5 dropped
    assert "d4" not in ids
    assert "d5" in ids  # oldest survivor
    assert "d54" in ids  # newest kept


def test_enqueue_at_cap_never_evicts_a_pending_lifecycle_delta(tmp_repo, monkeypatch):
    monkeypatch.setattr(share, "_OUTBOX_CAP", 2)
    share._enqueue({"decision_id": "pending", "content": "history", "attempts": 0,
                    "stage": "lifecycle_pending", "capability": "v1"})
    share._enqueue({"decision_id": "ordinary-1", "content": "one", "attempts": 0})
    share._enqueue({"decision_id": "ordinary-2", "content": "two", "attempts": 0})

    entries = share._load_outbox()
    assert [(e["decision_id"], e.get("stage")) for e in entries] == [
        ("pending", "lifecycle_pending"), ("ordinary-2", None)]


def test_enqueue_refuses_instead_of_evicting_when_cap_is_all_lifecycle(tmp_repo, monkeypatch):
    monkeypatch.setattr(share, "_OUTBOX_CAP", 1)
    share._enqueue({"decision_id": "pending", "content": "history", "attempts": 0,
                    "stage": "lifecycle_pending", "capability": "v1"})

    with pytest.raises(RuntimeError, match="no safely evictable row"):
        share._enqueue({"decision_id": "new", "content": "new", "attempts": 0,
                        "stage": "lifecycle_pending", "capability": "v2"})
    assert [e["decision_id"] for e in share._load_outbox()] == ["pending"]


def test_enqueue_does_not_report_success_after_evicting_its_own_ordinary_row(
        tmp_repo, monkeypatch):
    monkeypatch.setattr(share, "_OUTBOX_CAP", 1)
    share._enqueue({"decision_id": "pending", "content": "history", "attempts": 0,
                    "stage": "lifecycle_pending", "capability": "v1"})

    with pytest.raises(RuntimeError, match="no safely evictable row"):
        share._enqueue({"decision_id": "ordinary", "content": "body", "attempts": 0})
    assert [e["decision_id"] for e in share._load_outbox()] == ["pending"]


# ── outbox: drain_outbox ──────────────────────────────────────────────────────────

def test_drain_outbox_noop_when_empty(tmp_repo, monkeypatch):
    fake = _fake(monkeypatch, ret="srv-1")
    assert share.drain_outbox(TEAM) == 0
    assert fake.calls == []


def test_drain_outbox_noop_when_not_configured(tmp_repo, monkeypatch):
    share._enqueue({"decision_id": "d1", "type": "architecture", "content": "c",
                    "repo": None, "rationale": None, "confidence": None,
                    "evidence": None, "source": "ai", "queued_at": 1.0, "attempts": 0})
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: None))
    assert share.drain_outbox(config.Profile()) == 0
    assert len(share._load_outbox()) == 1  # left queued, untouched


def test_drain_outbox_sends_fifo_and_removes_successes(tmp_repo, monkeypatch):
    share._enqueue({"decision_id": "d1", "type": "architecture", "content": "first",
                    "repo": "r", "rationale": None, "confidence": 80,
                    "evidence": None, "source": "ai", "queued_at": 1.0, "attempts": 0})
    share._enqueue({"decision_id": "d2", "type": "constraint", "content": "second",
                    "repo": "r", "rationale": None, "confidence": 90,
                    "evidence": None, "source": "ai", "queued_at": 2.0, "attempts": 0})
    fake = _fake(monkeypatch, ret="srv-ok")
    sent = share.drain_outbox(TEAM)
    assert sent == 2
    assert [c["decision_id"] for c in fake.batches[0]] == ["d1", "d2"]  # FIFO order, one batch
    assert share._load_outbox() == []


def test_drain_outbox_stops_at_first_failure_keeps_tail(tmp_repo, monkeypatch):
    share._enqueue({"decision_id": "d1", "type": "architecture", "content": "first",
                    "repo": "r", "rationale": None, "confidence": 80,
                    "evidence": None, "source": "ai", "queued_at": 1.0, "attempts": 0})
    share._enqueue({"decision_id": "d2", "type": "constraint", "content": "second",
                    "repo": "r", "rationale": None, "confidence": 90,
                    "evidence": None, "source": "ai", "queued_at": 2.0, "attempts": 0})
    _fake(monkeypatch, exc=RemoteUnavailableError("down"))
    sent = share.drain_outbox(TEAM)
    assert sent == 0
    remaining = share._load_outbox()
    assert [e["decision_id"] for e in remaining] == ["d1", "d2"]  # kept, in order
    assert remaining[0]["attempts"] == 1  # incremented on the failed attempt


def test_drain_outbox_partial_success_then_failure(tmp_repo, monkeypatch):
    share._enqueue({"decision_id": "d1", "type": "architecture", "content": "first",
                    "repo": "r", "rationale": None, "confidence": 80,
                    "evidence": None, "source": "ai", "queued_at": 1.0, "attempts": 0})
    share._enqueue({"decision_id": "d2", "type": "constraint", "content": "second",
                    "repo": "r", "rationale": None, "confidence": 90,
                    "evidence": None, "source": "ai", "queued_at": 2.0, "attempts": 0})

    class _FlakyRS:
        def __init__(self):
            self.batches = []

        def push_decisions(self, kwargs_list):
            self.batches.append(kwargs_list)
            if any(kw["decision_id"] == "d2" for kw in kwargs_list):
                raise RemoteUnavailableError("down")
            return [f"srv-{i}" for i in range(len(kwargs_list))], []

    fake = _FlakyRS()
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: fake))
    remote.reset_degradation_warnings()
    monkeypatch.setattr(share, "_BATCH_SIZE", 1)  # one entry per chunk -> partial progress
    sent = share.drain_outbox(TEAM)
    assert sent == 1  # d1 sent, d2 kept
    remaining = share._load_outbox()
    assert [e["decision_id"] for e in remaining] == ["d2"]


def test_drain_outbox_concurrent_enqueue_survives_final_save(tmp_repo, monkeypatch):
    """An entry that lands on disk mid-drain (simulating another process's _enqueue while
    this drain is running) must not be wiped out by drain_outbox's final save."""
    share._enqueue({"decision_id": "d1", "type": "architecture", "content": "first",
                    "repo": "r", "rationale": None, "confidence": 80,
                    "evidence": None, "source": "ai", "queued_at": 1.0, "attempts": 0})

    class _ConcurrentEnqueueRS:
        def __init__(self):
            self.batches = []

        def push_decisions(self, kwargs_list):
            self.batches.append(kwargs_list)
            # Simulate a second process enqueueing a brand-new item while we're mid-drain --
            # it writes straight to the on-disk outbox, bypassing our in-memory `entries`.
            share._enqueue({"decision_id": "concurrent-1", "type": "constraint",
                            "content": "concurrently enqueued", "repo": "r",
                            "rationale": None, "confidence": 70, "evidence": None,
                            "source": "ai", "queued_at": 2.0, "attempts": 0})
            return [f"srv-{i}" for i in range(len(kwargs_list))], []

    fake = _ConcurrentEnqueueRS()
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: fake))
    remote.reset_degradation_warnings()

    sent = share.drain_outbox(TEAM)

    assert sent == 1  # d1 sent successfully
    remaining = share._load_outbox()
    ids = [e["decision_id"] for e in remaining]
    assert "concurrent-1" in ids  # must survive the final save, not be silently dropped
    assert "d1" not in ids  # successfully sent, not re-queued


def test_drain_outbox_sends_title_on_retry(tmp_repo, monkeypatch):
    # A queued offline share (carrying its title in the outbox row) must send that title
    # when a later drain succeeds - the round-trip this task guarantees. An entry queued
    # before this feature (no "title" key) must still drain fine (backward compatible).
    share._enqueue({"decision_id": "d1", "type": "architecture", "content": "first",
                    "repo": "r", "rationale": None, "confidence": 80, "evidence": None,
                    "source": "ai", "title": "Queued heading", "queued_at": 1.0, "attempts": 0})
    share._enqueue({"decision_id": "d2", "type": "constraint", "content": "second",
                    "repo": "r", "rationale": None, "confidence": 90, "evidence": None,
                    "source": "ai", "queued_at": 2.0, "attempts": 0})  # no "title" key at all
    fake = _fake(monkeypatch, ret="srv-ok")
    sent = share.drain_outbox(TEAM)
    assert sent == 2
    by_id = {kw["decision_id"]: kw for kw in fake.batches[0]}
    assert by_id["d1"]["title"] == "Queued heading"
    assert by_id["d2"]["title"] is None  # legacy row without a title -> omitted, never fabricated


def test_drain_outbox_source_files_gate_is_checked_at_drain_time(tmp_repo, monkeypatch):
    """issue #174 Task 5 - the wire gate is read at DRAIN time, never cached from queue time,
    in BOTH directions: an entry queued while gated off egresses its files once the gate opens,
    and one queued while open stops egressing them if the gate is rolled back. Both legs are set
    explicitly rather than leaning on the shipped default, so this keeps testing the drain-time
    read no matter which way the constant ships. Uses the real RemoteStore (network seam
    `remote._acall_tool` mocked, like test_remote.py) rather than _FakeRS, so the real
    _wire_args gate check is actually exercised - _FakeRS bypasses it entirely."""
    share._enqueue({"decision_id": "d1", "type": "architecture", "content": "use jwt for auth",
                    "repo": "r", "rationale": None, "confidence": 80, "evidence": None,
                    "source": "ai", "title": None, "source_files": ["auth/jwt.py"],
                    "queued_at": 1.0, "attempts": 0})
    captured = {}

    async def fake_call(endpoint, token, name, arguments, timeout):
        captured.update(name=name, args=arguments)
        return types.SimpleNamespace(
            content=[], isError=False,
            structuredContent={"results": [{"decisionId": "d1", "id": "srv-1"}], "skipped": []})

    monkeypatch.setattr(remote, "_acall_tool", fake_call)
    remote.reset_degradation_warnings()

    # Gate CLOSED at drain: source_files was on the queued entry but must NOT reach the wire.
    monkeypatch.setattr(remote, "_WIRE_SOURCE_FILES", False)
    assert share.drain_outbox(TEAM) == 1
    assert "source_files" not in captured["args"]["decisions"][0]

    # Re-queue the identical entry and flip the gate ON before draining again: now it must
    # reach the wire, scrubbed - proving the gate is read fresh at drain, never cached from
    # queue time.
    share._enqueue({"decision_id": "d1", "type": "architecture", "content": "use jwt for auth",
                    "repo": "r", "rationale": None, "confidence": 80, "evidence": None,
                    "source": "ai", "title": None, "source_files": ["auth/jwt.py"],
                    "queued_at": 1.0, "attempts": 0})
    monkeypatch.setattr(remote, "_WIRE_SOURCE_FILES", True)
    assert share.drain_outbox(TEAM) == 1
    assert captured["args"]["decisions"][0]["source_files"] == ["auth/jwt.py"]


def test_load_outbox_corrupt_file_reads_empty(tmp_repo):
    path = share._outbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json")
    assert share._load_outbox() == []


# ── an unreadable queue must never be overwritten ────────────────────────────────────
# `_load_outbox` answering "empty" for a damaged file is correct for every RENDER path and
# wrong for a WRITE: enqueueing through it turns "empty plus the new row" into the whole file,
# so one queued share destroyed every share already waiting. The rule is stated in
# `_read_outbox`'s own docstring and already honoured by `_enqueue_reconciliation`, which
# raises for the identical failure.

def _damaged_outbox(count: int = 3):
    path = share._outbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    for i in range(count):
        share._enqueue({"decision_id": f"queued-{i}", "type": "architecture",
                        "content": f"queued decision {i}", "repo": "r", "rationale": None,
                        "confidence": 80, "evidence": None, "source": "ai",
                        "queued_at": 1.0, "attempts": 0})
    assert len(share._load_outbox()) == count
    damaged = '[{"decision_id": "queued-0", "conten'
    path.write_text(damaged, encoding="utf-8")
    return path, damaged


def test_enqueue_refuses_rather_than_destroying_an_unreadable_queue(tmp_repo):
    path, damaged = _damaged_outbox()
    with pytest.raises(RuntimeError, match="cannot read the share retry queue"):
        share._enqueue({"decision_id": "new-one", "type": "architecture", "content": "new",
                        "repo": "r", "rationale": None, "confidence": 80, "evidence": None,
                        "source": "ai", "queued_at": 2.0, "attempts": 0})
    # Byte-for-byte untouched: refusing to write is the whole point. Before this, the file
    # came back holding only "new-one" and the three queued shares were gone.
    assert path.read_text(encoding="utf-8") == damaged


def test_a_failed_queue_is_reported_as_not_queued_not_as_queued(tmp_repo, monkeypatch):
    """The honest message already existed and was simply unreachable for this failure. A share
    that could not be queued must not claim "it will retry automatically".

    Uses a REAL projection, not a hand-made dict: a malformed dict makes `_payload` throw before
    the outbox is touched at all, so the test would pass for the wrong reason and keep passing
    if the fix were reverted."""
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    _, did = store.update_decision(tmp_repo, "a decision that cannot be queued", "s1",
                                   subtype="constraint")
    dec = store.get_shareable(tmp_repo, did)
    assert dec is not None
    _damaged_outbox()

    status = share._finish_share(dec, "r", None, "https://example.test")
    assert status.outcome == share_status.NOT_QUEUED
    assert (status.lost, status.queued) == (1, 0)   # unsaved, and it does not claim a retry


def test_share_reports_not_queued_when_lifecycle_rows_consume_the_cap(tmp_repo, monkeypatch):
    monkeypatch.setattr(share, "_OUTBOX_CAP", 1)
    share._enqueue({"decision_id": "pending", "content": "history", "attempts": 0,
                    "stage": "lifecycle_pending", "capability": "v1"})
    store.update_decision(tmp_repo, "a new decision cannot displace lifecycle history", "s1",
                          subtype="constraint")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    _fake(monkeypatch, exc=RemoteUnavailableError("down"))

    status = share.share(tmp_repo, profile=TEAM)

    assert status.outcome == share_status.NOT_QUEUED
    assert (status.lost, status.queued) == (1, 0)
    assert [e["decision_id"] for e in share._load_outbox()] == ["pending"]


def test_cancellation_still_wins_when_queueing_fails(tmp_repo, monkeypatch):
    """The two cancellation paths queue on the way out. A queue refusal there must not replace
    the CancelledError, or a cancelled share stops looking cancelled to its caller."""
    _damaged_outbox()
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    _, did = store.update_decision(tmp_repo, "a decision to share while cancelled", "s1",
                                   subtype="constraint")

    async def _cancel(*_a, **_k):
        raise asyncio.CancelledError()
    monkeypatch.setattr(share, "awith_local_fallback", _cancel)
    path = share._outbox_path()
    before = path.read_text(encoding="utf-8")
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(share.share_async(tmp_repo, did, profile=TEAM))
    # And it still refused to overwrite on the way out.
    assert path.read_text(encoding="utf-8") == before


def test_share_drains_queued_items_before_new_push(tmp_repo, monkeypatch):
    """A queued item from an earlier offline share is sent first, then the new decision --
    ordering is preserved."""
    share._enqueue({"decision_id": "queued-1", "type": "architecture", "content": "old queued item",
                    "repo": "r", "rationale": None, "confidence": 80,
                    "evidence": None, "source": "ai", "queued_at": 1.0, "attempts": 0})
    _, did = store.update_decision(tmp_repo, "brand new decision to share", "s1", subtype="constraint")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    fake = _fake(monkeypatch, ret="srv-ok")
    msg = share.share(tmp_repo, profile=TEAM)
    assert msg.outcome == share_status.SYNCED and msg.server_id == "srv-ok"
    # queued-1 drains via the batch path; the new decision via the single push - drain runs first.
    assert [c["decision_id"] for c in fake.batches[0]] == ["queued-1"]
    assert [c["decision_id"] for c in fake.calls] == [did]
    assert share._load_outbox() == []  # the queued item drained, only the new push happened


def test_share_survives_drain_failure(tmp_repo, monkeypatch):
    """A broken drain (e.g. a disk error saving the outbox) must not block the share
    itself - share() never raises for infrastructure problems."""
    _, did = store.update_decision(tmp_repo, "decision to share anyway", "s1", subtype="constraint")
    fake = _fake(monkeypatch)

    def boom(profile=None):
        raise OSError("disk full")

    monkeypatch.setattr(share, "drain_outbox", boom)
    msg = share.share(tmp_repo, profile=TEAM)
    assert msg.outcome == share_status.SYNCED
    assert [c["decision_id"] for c in fake.calls] == [did]  # the push still happened


def test_share_survives_enqueue_failure(tmp_repo, monkeypatch, capsys):
    """A failing _enqueue (disk full saving the outbox) must not escape share() - and the
    message must not promise a queued retry that was never recorded."""
    store.update_decision(tmp_repo, "decision that fails to sync", "s1", subtype="architecture")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    _fake(monkeypatch, exc=RemoteUnavailableError("down"))

    def boom(payload):
        raise OSError("disk full")

    monkeypatch.setattr(share, "_enqueue", boom)
    msg = share.share(tmp_repo, profile=TEAM)
    assert msg.outcome == share_status.NOT_QUEUED   # honest: nothing was recorded for retry
    assert (msg.lost, msg.queued) == (1, 0)


# ── shared-marker sidecar (.shared.json, endpoint-scoped, cosmetic) ───────────────

def test_share_success_marks_shared(tmp_repo, monkeypatch):
    _, did = store.update_decision(tmp_repo, "use postgres for storage", "s1", subtype="architecture")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    _fake(monkeypatch, ret="srv-9")
    share.share(tmp_repo, profile=TEAM)
    marked = share.shared_map(TEAM.endpoint)
    assert did in marked
    assert isinstance(marked[did], str) and marked[did]  # iso8601 timestamp recorded


def test_share_failure_does_not_mark_shared(tmp_repo, monkeypatch):
    _, did = store.update_decision(tmp_repo, "decision that fails to sync", "s1", subtype="architecture")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    _fake(monkeypatch, exc=RemoteUnavailableError("down"))
    share.share(tmp_repo, profile=TEAM)
    assert did not in share.shared_map(TEAM.endpoint)


def test_share_all_capacity_skip_not_marked_shared(tmp_repo, monkeypatch):
    # A batch push marks only the ids the server actually SAVED - an at-capacity skip (still
    # re-queued for later) must not show as shared until it genuinely drains.
    projs = [{"id": f"id{i}", "type": "architecture", "content": f"d{i}",
              "confidence": None, "evidence": None, "source": "ai"} for i in range(3)]
    monkeypatch.setattr(store, "get_shareable_all", lambda repo: projs)
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: _CapacityRS()))
    remote.reset_degradation_warnings()
    share.share_all(tmp_repo, profile=TEAM)
    marked = share.shared_map(TEAM.endpoint)
    assert "id0" in marked  # the one row the fake server actually stored
    assert "id1" not in marked and "id2" not in marked  # at-capacity skips, not (yet) shared


def test_share_all_invalid_skip_not_marked_shared(tmp_repo, monkeypatch):
    # A PERMANENTLY invalid row (server rejected type/content) is dropped, never queued, and
    # must never show as shared either - it was never actually stored.
    projs = [{"id": f"id{i}", "type": "architecture", "content": f"d{i}",
              "confidence": None, "evidence": None, "source": "ai"} for i in range(3)]
    monkeypatch.setattr(store, "get_shareable_all", lambda repo: projs)
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: _RejectRS()))
    remote.reset_degradation_warnings()
    share.share_all(tmp_repo, profile=TEAM)
    marked = share.shared_map(TEAM.endpoint)
    assert "id0" in marked
    assert "id1" not in marked and "id2" not in marked  # permanently invalid, never saved


def test_drain_outbox_marks_genuinely_saved_only(tmp_repo, monkeypatch):
    for did in ("d1", "d2"):
        share._enqueue({"decision_id": did, "type": "architecture", "content": did,
                        "repo": "r", "rationale": None, "confidence": 80, "evidence": None,
                        "source": "ai", "queued_at": 1.0, "attempts": 0})
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: _CapacityRS()))
    remote.reset_degradation_warnings()
    share.drain_outbox(TEAM)
    marked = share.shared_map(TEAM.endpoint)
    assert "d1" in marked  # saved
    assert "d2" not in marked  # at-capacity, kept queued - not genuinely drained yet


def test_drain_outbox_invalid_dropped_not_marked_shared(tmp_repo, monkeypatch):
    for did in ("d1", "d2"):
        share._enqueue({"decision_id": did, "type": "architecture", "content": did,
                        "repo": "r", "rationale": None, "confidence": 80, "evidence": None,
                        "source": "ai", "queued_at": 1.0, "attempts": 0})
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: _RejectRS()))
    remote.reset_degradation_warnings()
    share.drain_outbox(TEAM)
    marked = share.shared_map(TEAM.endpoint)
    assert "d1" in marked
    assert "d2" not in marked  # permanently invalid: dropped from outbox but NEVER marked shared


def test_shared_map_endpoint_scoped(tmp_repo, monkeypatch):
    # Pushed while pointed at endpoint A; a profile resolved to a DIFFERENT endpoint must show
    # NO marker - switching endpoints must never leak a stale/false "already shared" hint.
    _, did = store.update_decision(tmp_repo, "use postgres for storage", "s1", subtype="architecture")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    _fake(monkeypatch, ret="srv-9")
    share.share(tmp_repo, profile=TEAM)
    assert did in share.shared_map(TEAM.endpoint)
    other = config.Profile(mode="team", endpoint="https://other-host/mcp", token="tok2")
    assert did not in share.shared_map(other.endpoint)
    assert share.shared_map(other.endpoint) == {}


def test_shared_map_missing_file_reads_empty(tmp_repo):
    assert share.shared_map(TEAM.endpoint) == {}


def test_shared_map_corrupt_file_reads_empty_and_does_not_raise(tmp_repo):
    path = share._shared_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json")
    assert share.shared_map(TEAM.endpoint) == {}


def test_shared_map_no_endpoint_reads_empty(tmp_repo):
    share._mark_shared(["d1"], TEAM.endpoint)  # something IS marked for a real endpoint...
    assert share.shared_map(None) == {}  # ...but no endpoint given -> empty, never guesses


def test_mark_shared_write_failure_does_not_raise_or_block_share(tmp_repo, monkeypatch):
    # A marker is purely cosmetic: a write failure to .shared.json must not surface, and must
    # not block or alter the (successful) share's own return value.
    store.update_decision(tmp_repo, "use postgres for storage", "s1", subtype="architecture")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    _fake(monkeypatch, ret="srv-9")

    def boom(*a, **k):
        raise OSError("disk full")

    monkeypatch.setattr(share, "_append_shared", boom)
    msg = share.share(tmp_repo, profile=TEAM)
    # the push itself is unaffected by the marker write failing
    assert msg.outcome == share_status.SYNCED and msg.server_id == "srv-9"


def test_mark_shared_recovers_from_corrupt_file(tmp_repo, monkeypatch):
    # A corrupt sidecar degrades to the empty shape on read, so a fresh mark just starts a new
    # file - it never raises, and never blocks the push it's recording.
    path = share._shared_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json at all")
    _, did = store.update_decision(tmp_repo, "use postgres for storage", "s1", subtype="architecture")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    _fake(monkeypatch, ret="srv-9")
    msg = share.share(tmp_repo, profile=TEAM)
    assert msg.outcome == share_status.SYNCED and msg.server_id == "srv-9"
    assert did in share.shared_map(TEAM.endpoint)  # recovered - the fresh mark succeeded


# ── CLI ──────────────────────────────────────────────────────────────────────────

def test_cli_share_prints_result(monkeypatch, capsys):
    from contexer import cli
    monkeypatch.setattr(store, "git_root", lambda p: "/repo")
    monkeypatch.setattr(share, "share", lambda repo, decision_id="", **kw:
                        _ok_status(server_id=f"srv-{decision_id or 'latest'}"))
    cli.share_cmd(["abc123", "--yes"])  # --yes bypasses the push-confirm preview
    assert "srv-abc123" in capsys.readouterr().out


def test_cli_share_all_flag(monkeypatch, capsys):
    from contexer import cli
    monkeypatch.setattr(store, "git_root", lambda p: "/repo")
    monkeypatch.setattr(share, "share_all", lambda repo, **kw:
                        share_status.ShareStatus(share_status.BATCH_DONE, sent=3, total=3))
    cli.share_cmd(["--all", "--yes"])
    assert "Synced 3 decision(s)" in capsys.readouterr().out


def test_cli_share_previews_and_cancels_on_no(monkeypatch, capsys):
    # Without --yes, share_cmd previews and asks; answering 'n' pushes nothing.
    from contexer import cli, config
    monkeypatch.setattr(store, "git_root", lambda p: "/repo")
    monkeypatch.setattr(config, "load_profile", lambda *a, **k: config.Profile())
    monkeypatch.setattr(store, "get_shareable",
                        lambda repo, did="": {"id": "abc12345", "type": "constraint", "content": "never X"})
    pushed = {"n": 0}
    monkeypatch.setattr(share, "share", lambda *a, **k: pushed.__setitem__("n", pushed["n"] + 1) or _ok_status())
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    cli.share_cmd(["abc12345"])
    out = capsys.readouterr().out
    assert "never X" in out and "Cancelled" in out
    assert pushed["n"] == 0


def test_share_ids_shares_selected_in_one_batch(tmp_repo, monkeypatch):
    # A multi-pick resolves each id then pushes them all in ONE batched call, not one per id.
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    monkeypatch.setattr(store, "get_shareable", lambda repo, did="": {
        "id": did, "type": "constraint", "content": f"c-{did}",
        "confidence": None, "evidence": None, "source": "ai"})
    fake = _fake(monkeypatch, ret="srv-1")
    out = share.share_ids(tmp_repo, ["a", "b"], profile=TEAM)
    assert len(fake.batches) == 1  # one call for both, not one per id
    assert [x["decision_id"] for x in fake.batches[0]] == ["a", "b"]
    assert out.outcome == share_status.BATCH_DONE and (out.sent, out.total) == (2, 2)


def test_share_ids_reports_unknown_ids(tmp_repo, monkeypatch):
    # A typo'd id in a multi-pick is REPORTED, not silently dropped; the valid id still shares.
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)

    def _get(repo, did=""):
        return ({"id": did, "type": "constraint", "content": f"c-{did}",
                 "confidence": None, "evidence": None, "source": "ai"}
                if did == "good1234" else None)

    monkeypatch.setattr(store, "get_shareable", _get)
    fake = _fake(monkeypatch, ret="srv-1")
    out = share.share_ids(tmp_repo, ["good1234", "bad99999"], profile=TEAM)
    assert out.unknown_ids == ("bad99999",)     # reported as data, not as a prose prefix
    assert "Skipped 1 unknown id" in share_status.describe(out)
    assert [x["decision_id"] for x in fake.batches[0]] == ["good1234"]  # only the valid one shared


def test_share_ids_empty_shares_most_recent(tmp_repo, monkeypatch):
    # `tmp_repo` patches STORE_DIR as well as providing the path: `share_ids` takes the outbox
    # lock before dispatch, so the test must never create `.outbox.lock` in the real store.
    monkeypatch.setattr(share, "share", lambda repo, did="", **k:
                        _ok_status(server_id=f"recent:{did}"))
    assert share.share_ids(tmp_repo, [], profile=TEAM).server_id == "recent:"


def _ok_status(sent=1, **kw):
    """A successful ShareStatus for CLI fakes whose subject is routing, not wording.

    These tests replace a share function to check WHICH ids the CLI passed and that it printed
    what came back. They used to return a bare sentence, because a sentence was the return type."""
    return share_status.ShareStatus(share_status.SYNCED, sent=sent, total=sent, **kw)
def _three_shareable(monkeypatch):
    from contexer import config
    monkeypatch.setattr(store, "git_root", lambda p: "/repo")
    monkeypatch.setattr(config, "load_profile", lambda *a, **k: config.Profile())
    monkeypatch.setattr(store, "get_shareable_all", lambda repo: [
        {"id": "aaa11111", "type": "constraint", "content": "never X"},
        {"id": "bbb22222", "type": "architecture", "content": "use Y"},
        {"id": "ccc33333", "type": "convention", "content": "do Z"},
    ])


def test_cli_share_no_args_picker_multi_select(monkeypatch, capsys):
    from contexer import cli
    _three_shareable(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a: "1,3")
    got = {}

    def fake_ids(repo, ids, **k):
        got["ids"] = ids
        return share_status.ShareStatus(share_status.BATCH_DONE, sent=2, total=2)

    monkeypatch.setattr(share, "share_ids", fake_ids)
    cli.share_cmd([])  # no id, no --all -> numbered picker
    out = capsys.readouterr().out
    assert got["ids"] == ["aaa11111", "ccc33333"]  # selection 1,3 -> those ids
    assert "Synced 2 decision(s)" in out


def _mixed_status_shareable(monkeypatch):
    """Two not-yet-approved decisions + one approved, for the unapproved-share guard."""
    from contexer import config
    monkeypatch.setattr(store, "git_root", lambda p: "/repo")
    monkeypatch.setattr(config, "load_profile", lambda *a, **k: config.Profile())
    monkeypatch.setattr(store, "get_shareable_all", lambda repo: [
        {"id": "aaa11111", "type": "constraint", "content": "never X", "status": "suggested"},
        {"id": "bbb22222", "type": "architecture", "content": "use Y", "status": "approved"},
        {"id": "ccc33333", "type": "convention", "content": "do Z", "status": "pending_approval"},
    ])


def test_pending_review_warning_counts_only_pending_approval():
    from contexer import cli
    projs = [{"status": "suggested"}, {"status": "suggested"},
             {"status": "pending_approval"}, {"status": "approved"}]
    lines = cli._pending_review_warning(projs)
    assert "1 of 4 are PENDING REVIEW" in lines[0]
    # `suggested` must NOT trigger the gate: auto-injection already serves approved+suggested,
    # so sharing one promotes nothing that isn't already trusted context locally.
    assert cli._pending_review_warning([{"status": "suggested"}, {"status": "suggested"}]) == []
    # all-approved (and status-less legacy projections) produce no warning at all
    assert cli._pending_review_warning([{"status": "approved"}, {}]) == []


def test_cli_share_picker_guards_unapproved_and_cancels(monkeypatch, capsys):
    # Picker path has no other confirm step, so an unreviewed decision must be gated here.
    from contexer import cli
    _mixed_status_shareable(monkeypatch)
    answers = iter(["3", "n"])  # item 3 is the pending_approval one; decline the guard
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    called = {}
    monkeypatch.setattr(share, "share_ids", lambda *a, **k: called.__setitem__("hit", True) or _ok_status())
    cli.share_cmd([])
    out = capsys.readouterr().out
    assert "PENDING REVIEW" in out and "auto-approves" in out
    assert "Cancelled" in out
    assert "hit" not in called  # nothing pushed


def test_cli_share_picker_guard_proceeds_on_yes(monkeypatch, capsys):
    from contexer import cli
    _mixed_status_shareable(monkeypatch)
    answers = iter(["3", "y"])  # pending_approval item, accept the guard
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    got = {}

    def fake_ids(repo, ids, **k):
        got["ids"] = ids
        return _ok_status()

    monkeypatch.setattr(share, "share_ids", fake_ids)
    cli.share_cmd([])
    assert got["ids"] == ["ccc33333"]
    assert "Synced decision" in capsys.readouterr().out


def test_cli_share_picker_no_guard_for_suggested_or_approved(monkeypatch, capsys):
    # Neither `approved` nor `suggested` gates: both are already served by auto-injection, so
    # sharing them promotes nothing unreviewed. Only ONE input is consumed (no second prompt).
    from contexer import cli
    _mixed_status_shareable(monkeypatch)
    answers = iter(["1,2"])  # aaa11111 (suggested) + bbb22222 (approved)
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    got = {}

    def fake_ids(repo, ids, **k):
        got["ids"] = ids
        return _ok_status()

    monkeypatch.setattr(share, "share_ids", fake_ids)
    cli.share_cmd([])
    out = capsys.readouterr().out
    assert got["ids"] == ["aaa11111", "bbb22222"]
    assert "PENDING REVIEW" not in out  # guard stayed silent


def test_cli_share_confirm_path_warns_inline_before_single_prompt(monkeypatch, capsys):
    # `share <id>` already gates on y/N, so the warning is inline there - not a second prompt.
    from contexer import cli
    _mixed_status_shareable(monkeypatch)
    monkeypatch.setattr(store, "get_shareable", lambda repo, i="": {
        "id": "ccc33333", "type": "convention", "content": "do Z", "status": "pending_approval"})
    answers = iter(["n"])  # the ONE prompt this path has
    monkeypatch.setattr("builtins.input", lambda *a: next(answers))
    monkeypatch.setattr(share, "share", lambda *a, **k: _ok_status())
    cli.share_cmd(["aaa11111"])
    out = capsys.readouterr().out
    assert "PENDING REVIEW" in out
    assert "Cancelled" in out


def test_cli_share_picker_shows_marker_and_orders_shared_last(monkeypatch, capsys):
    # bbb22222 (the MIDDLE item in get_shareable_all's oldest-first order) is already shared:
    # it must render "✓ shared" and move to the END of the picker; aaa11111/ccc33333 (unshared)
    # come first, in their original relative order.
    from contexer import cli
    _three_shareable(monkeypatch)
    monkeypatch.setattr(share, "shared_map", lambda endpoint: {"bbb22222": "2026-01-01T00:00:00+00:00"})
    monkeypatch.setattr("builtins.input", lambda *a: "q")
    cli.share_cmd([])
    out = capsys.readouterr().out
    assert out.index("aaa11111") < out.index("ccc33333") < out.index("bbb22222")
    assert out.count("✓ shared") == 1
    bbb_at = out.index("bbb22222")
    assert "✓ shared" in out[bbb_at:bbb_at + 40]  # marker sits on bbb22222's own id line
    aaa_at = out.index("aaa11111")
    assert "✓ shared" not in out[aaa_at:aaa_at + 40]  # unshared entries carry no marker


def test_cli_share_picker_stable_order_within_each_group(monkeypatch, capsys):
    from contexer import cli, config
    monkeypatch.setattr(store, "git_root", lambda p: "/repo")
    monkeypatch.setattr(config, "load_profile", lambda *a, **k: config.Profile())
    # ids are exactly 8 chars (_share_item_block truncates to id[:8]) so the printed text
    # matches these literally, with no truncation ambiguity.
    items = [
        {"id": "id1shar1", "type": "constraint", "content": "one"},
        {"id": "id2plan1", "type": "constraint", "content": "two"},
        {"id": "id3shar2", "type": "constraint", "content": "three"},
        {"id": "id4plan2", "type": "constraint", "content": "four"},
    ]
    monkeypatch.setattr(store, "get_shareable_all", lambda repo: items)
    monkeypatch.setattr(share, "shared_map",
                        lambda endpoint: {"id1shar1": "t", "id3shar2": "t"})
    monkeypatch.setattr("builtins.input", lambda *a: "q")
    cli.share_cmd([])
    out = capsys.readouterr().out
    # Unshared first (id2plan1 before id4plan2 - original relative order kept), shared last
    # (id1shar1 before id3shar2 - original relative order kept within that group too).
    order = ("id2plan1", "id4plan2", "id1shar1", "id3shar2")
    positions = [out.index(x) for x in order]
    assert positions == sorted(positions)


def test_cli_share_picker_shared_entry_still_selectable(monkeypatch, capsys):
    # Re-sharing is legitimate (it updates the row server-side) - a shared entry must remain
    # selectable, and its display-index (post-reorder) must resolve to the right id.
    from contexer import cli
    _three_shareable(monkeypatch)
    monkeypatch.setattr(share, "shared_map", lambda endpoint: {"bbb22222": "t"})
    monkeypatch.setattr("builtins.input", lambda *a: "3")  # 3rd item shown = bbb22222 (moved last)
    got = {}
    monkeypatch.setattr(share, "share_ids", lambda repo, ids, **k: got.__setitem__("ids", ids) or _ok_status())
    cli.share_cmd([])
    assert got["ids"] == ["bbb22222"]


def test_cli_share_picker_no_shared_marker_when_shared_map_empty(monkeypatch, capsys):
    # Sanity: local mode (no endpoint) -> shared_map is naturally empty -> no markers, no
    # reordering (matches the pre-feature picker output exactly).
    from contexer import cli
    _three_shareable(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a: "q")
    cli.share_cmd([])
    out = capsys.readouterr().out
    assert "✓ shared" not in out


def test_cli_share_picker_cancel(monkeypatch, capsys):
    from contexer import cli
    _three_shareable(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda *a: "q")
    pushed = {"n": 0}
    monkeypatch.setattr(share, "share_ids", lambda *a, **k: pushed.__setitem__("n", 1) or _ok_status())
    cli.share_cmd([])
    out = capsys.readouterr().out
    assert "Cancelled" in out
    assert pushed["n"] == 0


def test_cli_share_picker_cancel_on_keyboard_interrupt(monkeypatch, capsys):
    from contexer import cli
    _three_shareable(monkeypatch)

    def boom(*a):
        raise KeyboardInterrupt

    monkeypatch.setattr("builtins.input", boom)
    pushed = {"n": 0}
    monkeypatch.setattr(share, "share_ids", lambda *a, **k: pushed.__setitem__("n", 1) or _ok_status())
    cli.share_cmd([])
    out = capsys.readouterr().out
    assert "Cancelled" in out
    assert pushed["n"] == 0


def test_cli_share_picker_all_on_single_page_returns_shown_ids(monkeypatch, capsys):
    # `all` with <= _SHARE_PAGE shareable decisions shares exactly the shown set.
    from contexer import cli
    _three_shareable(monkeypatch)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda p="": (prompts.append(p), "all")[1])
    got = {}
    monkeypatch.setattr(share, "share_ids", lambda repo, ids, **k: got.__setitem__("ids", ids) or _ok_status())
    cli.share_cmd([])
    assert got["ids"] == ["aaa11111", "bbb22222", "ccc33333"]
    assert "all (3)" in prompts[0]  # label carries the exact loaded count


def _many_shareable(monkeypatch, n):
    from contexer import config
    monkeypatch.setattr(store, "git_root", lambda p: "/repo")
    monkeypatch.setattr(config, "load_profile", lambda *a, **k: config.Profile())
    items = [{"id": f"id{i:06d}", "type": "convention", "title": f"Decision {i}",
              "content": f"Decision {i} body."} for i in range(1, n + 1)]
    monkeypatch.setattr(store, "get_shareable_all", lambda repo: items)
    return items


def test_cli_share_picker_pages_and_selects_from_second_page(monkeypatch, capsys):
    # >_SHARE_PAGE shareable decisions: 'm' reveals the next page, numbering stays continuous,
    # and a page-2 number (11) resolves to the right decision.
    from contexer import cli
    items = _many_shareable(monkeypatch, 30)
    prompts = []
    inputs = iter(["m", "11"])
    monkeypatch.setattr("builtins.input", lambda p="": (prompts.append(p), next(inputs))[1])
    got = {}
    monkeypatch.setattr(share, "share_ids", lambda repo, ids, **k: got.__setitem__("ids", ids) or _ok_status())
    cli.share_cmd([])
    assert "all (10)" in prompts[0] and "m=more" in prompts[0]   # first prompt: page 1 only
    assert "all (20)" in prompts[1] and "m=more" in prompts[1]   # after 'm': two pages loaded
    assert got["ids"] == [items[10]["id"]]  # 11th item (0-indexed 10)


def test_cli_share_picker_all_after_paging_returns_loaded_set(monkeypatch, capsys):
    # 'all' after paging shares exactly the currently-loaded set (not the whole store, and
    # not just the first page) - the count in the prompt label makes that unambiguous.
    from contexer import cli
    items = _many_shareable(monkeypatch, 30)
    inputs = iter(["m", "all"])
    monkeypatch.setattr("builtins.input", lambda *a: next(inputs))
    got = {}
    monkeypatch.setattr(share, "share_ids", lambda repo, ids, **k: got.__setitem__("ids", ids) or _ok_status())
    cli.share_cmd([])
    assert got["ids"] == [it["id"] for it in items[:20]]  # two pages loaded, not all 30
    assert len(got["ids"]) == 20


def test_cli_share_picker_first_page_hides_m_and_unloaded_count(monkeypatch, capsys):
    # Before paging, only page 1's count is offered and unpicked items aren't selectable yet.
    from contexer import cli
    _many_shareable(monkeypatch, 30)
    prompts = []
    monkeypatch.setattr("builtins.input", lambda p="": (prompts.append(p), "26")[1])  # unloaded
    got = {}
    monkeypatch.setattr(share, "share_ids", lambda repo, ids, **k: got.__setitem__("ids", ids) or _ok_status())
    cli.share_cmd([])
    out = capsys.readouterr().out
    assert "all (10)" in prompts[0]
    assert "m=more" in prompts[0]
    assert "…and 20 more" in out
    assert "Cancelled" in out  # 26 wasn't a valid selection on page 1 -> nothing picked


def test_cli_share_picker_page_size_is_ten(monkeypatch, capsys):
    # The picker pages at _SHARE_PAGE (10), NOT _FILTERED_DISPLAY (the agent-context token
    # budget) - the two are independent knobs that merely started at the same number.
    from contexer import cli
    _many_shareable(monkeypatch, 30)
    monkeypatch.setattr("builtins.input", lambda *a: "q")
    cli.share_cmd([])
    out = capsys.readouterr().out
    assert store._SHARE_PAGE == 10
    assert out.count("id:    ") == 10  # exactly one page of blocks rendered


class TestParseSelection:
    """`_parse_selection` - single numbers and inclusive ranges, order preserved, deduped."""

    def test_single_numbers(self):
        from contexer import cli
        assert cli._parse_selection("1,3", 10) == ([1, 3], [])

    def test_range_expands_inclusive(self):
        from contexer import cli
        assert cli._parse_selection("1-4", 10) == ([1, 2, 3, 4], [])

    def test_mixed_numbers_and_ranges_keep_typed_order(self):
        from contexer import cli
        assert cli._parse_selection("5,1-3,9", 10) == ([5, 1, 2, 3, 9], [])

    def test_descending_range_reads_the_same(self):
        from contexer import cli
        assert cli._parse_selection("4-1", 10) == ([1, 2, 3, 4], [])

    def test_overlapping_selections_dedupe(self):
        from contexer import cli
        assert cli._parse_selection("1-3,2,3-4", 10) == ([1, 2, 3, 4], [])

    def test_range_past_the_end_is_clamped_and_reported(self):
        from contexer import cli
        rows, ignored = cli._parse_selection("1-20", 10)
        assert rows == list(range(1, 11))
        assert ignored == ["1-20 (clamped to 1-10)"]

    def test_range_wholly_outside_window_is_ignored(self):
        from contexer import cli
        assert cli._parse_selection("15-20", 10) == ([], ["15-20"])

    def test_junk_and_negatives_are_ignored_not_selected(self):
        from contexer import cli
        # "-3" must not read as row 3: a leading dash is a malformed range, not a number.
        assert cli._parse_selection("xyz,-3,0,99", 10) == ([], ["xyz", "-3", "0", "99"])

    def test_whitespace_tolerated(self):
        from contexer import cli
        assert cli._parse_selection(" 1 - 3 , 5 ", 10) == ([1, 2, 3, 5], [])


def test_cli_share_picker_accepts_a_range(monkeypatch, capsys):
    # End-to-end: "1-4" in the picker pushes exactly the first four decisions.
    from contexer import cli
    items = _many_shareable(monkeypatch, 12)
    monkeypatch.setattr("builtins.input", lambda *a: "1-4")
    got = {}
    monkeypatch.setattr(share, "share_ids", lambda repo, ids, **k: got.__setitem__("ids", ids) or _ok_status())
    cli.share_cmd([])
    assert got["ids"] == [it["id"] for it in items[:4]]


def test_cli_share_picker_reports_clamped_range_before_pushing(monkeypatch, capsys):
    # A range past the loaded page still pushes what it can, but says what it dropped -
    # the push is outward, so the developer must not learn the real count only afterwards.
    from contexer import cli
    items = _many_shareable(monkeypatch, 30)
    monkeypatch.setattr("builtins.input", lambda *a: "1-25")
    got = {}
    monkeypatch.setattr(share, "share_ids", lambda repo, ids, **k: got.__setitem__("ids", ids) or _ok_status())
    cli.share_cmd([])
    out = capsys.readouterr().out
    assert got["ids"] == [it["id"] for it in items[:10]]
    assert "Ignored: 1-25 (clamped to 1-10)" in out


def test_cli_share_picker_quit_is_not_reported_as_ignored(monkeypatch, capsys):
    # 'q' is a documented key, not a malformed token - quitting must stay silent.
    from contexer import cli
    _many_shareable(monkeypatch, 12)
    monkeypatch.setattr("builtins.input", lambda *a: "q")
    cli.share_cmd([])
    out = capsys.readouterr().out
    assert "Ignored" not in out
    assert "Cancelled" in out


def test_cli_share_nothing_to_share_no_false_cancel(monkeypatch, capsys):
    # #4: an empty share prints only 'Nothing to share', not a contradictory 'Cancelled'.
    from contexer import cli, config
    monkeypatch.setattr(store, "git_root", lambda p: "/repo")
    monkeypatch.setattr(config, "load_profile", lambda *a, **k: config.Profile())
    monkeypatch.setattr(store, "get_shareable", lambda repo, did="": None)
    cli.share_cmd(["missing"])
    out = capsys.readouterr().out
    assert "Nothing to share" in out
    assert "Cancelled" not in out


def test_cli_share_all_with_id_rejected(monkeypatch, capsys):
    from contexer import cli
    monkeypatch.setattr(store, "git_root", lambda p: "/repo")
    with pytest.raises(SystemExit):
        cli.share_cmd(["--all", "abc123"])
    assert "exactly one" in capsys.readouterr().err.lower()


def test_cli_share_no_repo_exits(monkeypatch):
    from contexer import cli
    monkeypatch.setattr(store, "git_root", lambda p: "")
    monkeypatch.setattr(store, "resolve_repo", lambda p: "")
    with pytest.raises(SystemExit):
        cli.share_cmd([])


# ── wire-source normalization ──────────────────────────────────────────────────────
# The cloud's push_decision accepts a CLOSED source allowlist (ai|human|scan|bootstrap|
# memory|plan) and rejects anything else with a hard -32602 that silently poisons the
# outbox. `plan` (OSS provisional provenance - created_by leaks into revision.source) is
# accepted and PRESERVED end-to-end since contexer-teams#91; only a genuinely-unknown
# source degrades to "ai" so it can never brick the outbox.

def test_wire_source_preserves_plan():
    # The cloud accepts source="plan" (contexer-teams PUSH_DECISION_SOURCES), so the
    # provisional signal is preserved on the wire, not coerced to "ai".
    assert share._wire_source("plan") == "plan"


def test_wire_source_passes_canonical_sources_through():
    for s in ("ai", "human", "scan", "bootstrap", "memory"):
        assert share._wire_source(s) == s


def test_wire_source_coerces_unknown_string_to_ai():
    # Defense against future taxonomy drift: an unknown *non-null* source must never reach
    # the cloud verbatim (that is what bricks the outbox); it degrades to the safe "ai".
    assert share._wire_source("something-new") == "ai"


def test_wire_source_passes_none_through():
    # None = genuinely-unknown provenance. push_decision OMITS source when it is None (the
    # cloud stores NULL), so None must NOT be fabricated into "ai" - that mislabels a
    # decision of unknown origin as AI-authored.
    assert share._wire_source(None) is None


def test_share_plan_sourced_decision_preserves_plan_on_wire(tmp_repo, monkeypatch):
    _, did = store.update_decision(
        tmp_repo, "provisional plan decision to sync", "s1",
        subtype="architecture", created_by="plan")
    assert store.get_shareable(tmp_repo, did)["source"] == "plan"  # local value is plan
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    fake = _fake(monkeypatch, ret="srv-9")
    share.share(tmp_repo, did, profile=TEAM)
    assert fake.calls[0]["source"] == "plan"  # ... and the cloud now accepts it, unchanged


def test_share_all_plan_sourced_preserves_plan_on_wire(tmp_repo, monkeypatch):
    store.update_decision(tmp_repo, "provisional plan decision one", "s1",
                          subtype="architecture", created_by="plan")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    fake = _fake(monkeypatch, ret="srv-1")
    share.share_all(tmp_repo, profile=TEAM)
    assert all(kw["source"] == "plan" for kw in fake.calls)


def test_payload_preserves_plan_source(tmp_repo, monkeypatch):
    _, did = store.update_decision(
        tmp_repo, "plan decision that fails to sync", "s1",
        subtype="architecture", created_by="plan")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    _fake(monkeypatch, exc=RemoteUnavailableError("down"))
    share.share(tmp_repo, did, profile=TEAM)
    # The queued outbox entry preserves "plan" (the cloud accepts it), so a later drain
    # re-sends it faithfully.
    assert share._load_outbox()[0]["source"] == "plan"


def test_drain_outbox_preserves_plan_source(tmp_repo, monkeypatch):
    # An outbox entry with source="plan" drains faithfully now that the cloud accepts it.
    share._enqueue({
        "decision_id": "plan-1", "type": "architecture", "content": "queued plan entry",
        "repo": "github.com/a/b", "rationale": None, "confidence": 30, "evidence": None,
        "source": "plan", "queued_at": 0.0, "attempts": 3,
    })
    fake = _fake(monkeypatch, ret="srv-plan")
    sent = share.drain_outbox(profile=TEAM)
    assert sent == 1
    assert fake.batches[0][0]["source"] == "plan"


# ── #108: async share path (awaited by the in-loop server.share_decision tool) ─────
# share_async / share_ids_async / _adrain_outbox_unlocked are the async twins of share /
# share_ids / _drain_outbox_unlocked. They await RemoteStore.apush_decision so a wedged push is
# CANCELLABLE, and reuse every local helper (_finish_share, _entry_push_kwargs, _payload,
# _enqueue) so the sync and async paths can't drift.
#
# There is no public `adrain_outbox` wrapper: it was deleted along with the async-native
# reconciliation drain it called, neither of which any production path reached. The async drain
# below is entered through the real `async_outbox_lock`, which is how the live share paths reach
# it and what keeps that lock on a tested route.

async def _adrain(profile):
    async with share.async_outbox_lock():
        return await share._adrain_outbox_unlocked(profile)


def test_store_lock_non_blocking_refuses_a_lock_already_held(tmp_repo):
    """The capability `async_outbox_lock` polls with. flock is per open file description, so two
    separate opens contend even inside one process."""
    with store.store_lock("poll-probe"):
        with pytest.raises(BlockingIOError):
            with store.store_lock("poll-probe", blocking=False):
                pass


def test_a_failed_non_blocking_acquire_releases_what_it_took(tmp_repo, monkeypatch):
    """`_acquire_outbox_lock` takes the process-local mutex BEFORE the file lock. If the file lock
    then refuses, the mutex has to come back, or every later share in this process blocks on a
    lock nothing holds - a permanent deadlock, not a slow path."""
    monkeypatch.setattr(store, "store_lock",
                        lambda slug, blocking=True: (_ for _ in ()).throw(BlockingIOError("held")))
    with pytest.raises(BlockingIOError):
        share._acquire_outbox_lock(blocking=False)
    assert share._OUTBOX_LOCAL_LOCK.acquire(blocking=False), "the local mutex was leaked"
    share._OUTBOX_LOCAL_LOCK.release()


def test_a_successful_acquire_hands_back_a_release_for_both_locks(tmp_repo):
    stack = share._acquire_outbox_lock(blocking=False)
    assert not share._OUTBOX_LOCAL_LOCK.acquire(blocking=False)   # held while the stack is open
    stack.close()
    assert share._OUTBOX_LOCAL_LOCK.acquire(blocking=False)       # and released by closing it
    share._OUTBOX_LOCAL_LOCK.release()


def test_async_outbox_lock_does_not_stall_the_event_loop(tmp_repo):
    """The stall this fixed: `async_outbox_lock` took the blocking file lock synchronously inside
    the loop. A blocking flock has no await point, so a single concurrent `contexer share` holding
    the lock froze EVERY tool on the MCP server, and `server.share_decision`'s own
    `asyncio.wait_for` could not cancel the wait because there was nothing to cancel.

    Both halves are asserted: other work on the loop keeps running while the lock is held
    elsewhere, and the caller's deadline actually fires."""
    held, release = threading.Event(), threading.Event()

    def holder():
        with share.outbox_lock():
            held.set()
            release.wait(10)

    thread = threading.Thread(target=holder, daemon=True)
    thread.start()
    assert held.wait(10), "the holder thread never took the lock"

    async def waiter():
        async with share.async_outbox_lock():
            return "acquired"

    async def scenario():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.005)

        spin = asyncio.ensure_future(ticker())
        try:
            with pytest.raises((TimeoutError, asyncio.TimeoutError)):
                await asyncio.wait_for(waiter(), 0.25)
        finally:
            spin.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await spin
        return ticks

    try:
        ticks = asyncio.run(scenario())
    finally:
        release.set()
        thread.join(10)
    assert ticks > 1, "the event loop was blocked while waiting for the outbox lock"


def test_async_outbox_lock_still_reentrant_for_a_nested_enqueue(tmp_repo, monkeypatch):
    """Every async share path enqueues while already holding this lock, and `_enqueue` takes the
    SYNC `outbox_lock`. Reentrancy rides on a ContextVar, so the depth token has to be set in the
    awaiting coroutine's own context - acquiring in a worker thread would set it in that thread's
    copy, the nested call would read depth 0, and it would deadlock on the non-reentrant
    `store_lock`. Bounded by a timeout so a regression fails instead of hanging the suite."""
    async def scenario():
        async with share.async_outbox_lock():
            share._enqueue({"decision_id": "nested-1", "type": "architecture", "content": "c",
                            "repo": "r", "rationale": None, "confidence": 80, "evidence": None,
                            "source": "ai", "queued_at": 1.0, "attempts": 0})
            return [e["decision_id"] for e in share._load_outbox()]

    assert asyncio.run(asyncio.wait_for(scenario(), 10)) == ["nested-1"]


def test_share_async_is_coroutine():
    assert asyncio.iscoroutinefunction(share.share_async)
    assert asyncio.iscoroutinefunction(share.share_ids_async)
    assert asyncio.iscoroutinefunction(share._adrain_outbox_unlocked)


def test_share_async_happy_path_awaits_apush(tmp_repo, monkeypatch):
    _, did = store.update_decision(tmp_repo, "use postgres for storage", "s1", subtype="architecture")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    fake = _afake(monkeypatch, ret="srv-9")
    msg = asyncio.run(share.share_async(tmp_repo, profile=TEAM))
    assert msg.outcome == share_status.SYNCED and msg.server_id == "srv-9"
    # same honest-visibility message as sync share(), from the same renderer
    assert "won't see" in share_status.describe(msg).lower()
    assert len(fake.calls) == 1
    kw = fake.calls[0]
    assert kw["decision_id"] == did
    assert kw["repo"] == "github.com/a/b"
    assert kw["source"] == "ai"


def test_share_async_nothing_to_share(tmp_repo):
    assert asyncio.run(
        share.share_async(tmp_repo, profile=TEAM)).outcome == share_status.NO_MATCH


def test_share_async_local_mode_message(tmp_repo, monkeypatch):
    store.update_decision(tmp_repo, "a decision to maybe share", "s1", subtype="architecture")
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: None))
    assert asyncio.run(share.share_async(
        tmp_repo, profile=config.Profile())).outcome == share_status.NOT_TEAM_MODE


def test_share_ids_async_shares_each_selected(tmp_repo, monkeypatch):
    _, id1 = store.update_decision(tmp_repo, "use postgres for the database", "s1", subtype="architecture")
    _, id2 = store.update_decision(tmp_repo, "never hardcode secret api keys", "s1", subtype="constraint")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    fake = _afake(monkeypatch, ret="srv-ok")
    msg = asyncio.run(share.share_ids_async(tmp_repo, [id1[:8], id2[:8]], profile=TEAM))
    assert len(fake.batches) == 1  # one awaited batched call for both, not one per id
    assert [c["decision_id"] for c in fake.batches[0]] == [id1, id2]
    assert msg.outcome == share_status.BATCH_DONE and (msg.sent, msg.total) == (2, 2)


def test_share_ids_async_empty_shares_most_recent(tmp_repo, monkeypatch):
    _, did = store.update_decision(tmp_repo, "the newest decision to share", "s1", subtype="constraint")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    fake = _afake(monkeypatch, ret="srv-1")
    asyncio.run(share.share_ids_async(tmp_repo, [], profile=TEAM))
    assert fake.calls[0]["decision_id"] == did


def test_share_async_degraded_enqueues(tmp_repo, monkeypatch, capsys):
    _, did = store.update_decision(tmp_repo, "decision that fails to sync", "s1", subtype="architecture")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    _afake(monkeypatch, exc=RemoteUnavailableError("down"))
    msg = asyncio.run(share.share_async(tmp_repo, profile=TEAM))
    assert msg.outcome == share_status.QUEUED and msg.queued == 1
    assert [e["decision_id"] for e in share._load_outbox()] == [did]
    assert "unreachable" in capsys.readouterr().err.lower()


def test_share_async_serializes_overlapping_tasks(tmp_repo, monkeypatch):
    store.update_decision(tmp_repo, "decision shared from overlapping tasks", "s1",
                          subtype="architecture")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    calls = []

    async def scenario():
        first_entered = asyncio.Event()
        release_first = asyncio.Event()

        class BlockingAsyncRemote:
            async def apush_decision(self, **kw):
                calls.append(kw)
                if len(calls) == 1:
                    first_entered.set()
                    await release_first.wait()
                return f"srv-{len(calls)}"

            async def apush_decisions(self, kwargs_list):
                return [f"srv-{i}" for i in range(len(kwargs_list))], []

        monkeypatch.setattr(
            share.RemoteStore, "from_profile", staticmethod(lambda p: BlockingAsyncRemote()))
        first = asyncio.create_task(share.share_async(tmp_repo, profile=TEAM))
        await asyncio.wait_for(first_entered.wait(), timeout=1)
        second = asyncio.create_task(share.share_async(tmp_repo, profile=TEAM))
        await asyncio.sleep(0.05)
        assert len(calls) == 1
        release_first.set()
        await asyncio.wait_for(asyncio.gather(first, second), timeout=2)

    asyncio.run(scenario())
    assert len(calls) == 2


def test_async_drain_sends_fifo_and_removes_successes(tmp_repo, monkeypatch):
    share._enqueue({"decision_id": "d1", "type": "architecture", "content": "first",
                    "repo": "r", "rationale": None, "confidence": 80,
                    "evidence": None, "source": "ai", "queued_at": 1.0, "attempts": 0})
    share._enqueue({"decision_id": "d2", "type": "constraint", "content": "second",
                    "repo": "r", "rationale": None, "confidence": 90,
                    "evidence": None, "source": "ai", "queued_at": 2.0, "attempts": 0})
    fake = _afake(monkeypatch, ret="srv-ok")
    sent = asyncio.run(_adrain(TEAM))
    assert sent == 2
    assert [c["decision_id"] for c in fake.batches[0]] == ["d1", "d2"]  # FIFO, one awaited batch
    assert share._load_outbox() == []


def test_async_drain_stops_at_first_failure_keeps_tail(tmp_repo, monkeypatch):
    share._enqueue({"decision_id": "d1", "type": "architecture", "content": "first",
                    "repo": "r", "rationale": None, "confidence": 80,
                    "evidence": None, "source": "ai", "queued_at": 1.0, "attempts": 0})
    _afake(monkeypatch, exc=RemoteUnavailableError("down"))
    sent = asyncio.run(_adrain(TEAM))
    assert sent == 0
    remaining = share._load_outbox()
    assert [e["decision_id"] for e in remaining] == ["d1"]
    assert remaining[0]["attempts"] == 1


def test_share_async_drains_queued_before_new_push(tmp_repo, monkeypatch):
    share._enqueue({"decision_id": "queued-1", "type": "architecture", "content": "old queued item",
                    "repo": "r", "rationale": None, "confidence": 80,
                    "evidence": None, "source": "ai", "queued_at": 1.0, "attempts": 0})
    _, did = store.update_decision(tmp_repo, "brand new decision to share", "s1", subtype="constraint")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
    fake = _afake(monkeypatch, ret="srv-ok")
    asyncio.run(share.share_async(tmp_repo, profile=TEAM))
    # queued-1 drains via the awaited batch path; the new decision via the single awaited push.
    assert [c["decision_id"] for c in fake.batches[0]] == ["queued-1"]
    assert [c["decision_id"] for c in fake.calls] == [did]
    assert share._load_outbox() == []


def test_enqueue_ids_for_retry_queues_each(tmp_repo, monkeypatch):
    _, id1 = store.update_decision(tmp_repo, "use postgres for the database", "s1", subtype="architecture")
    _, id2 = store.update_decision(tmp_repo, "never hardcode secret api keys", "s1", subtype="constraint")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    n = share.enqueue_ids_for_retry(tmp_repo, [id1, id2])
    assert n == 2
    assert [e["decision_id"] for e in share._load_outbox()] == [id1, id2]


def test_enqueue_ids_for_retry_empty_queues_most_recent(tmp_repo, monkeypatch):
    _, did = store.update_decision(tmp_repo, "the newest decision here now", "s1", subtype="constraint")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    n = share.enqueue_ids_for_retry(tmp_repo, [])
    assert n == 1
    assert share._load_outbox()[0]["decision_id"] == did


def test_enqueue_ids_for_retry_is_idempotent(tmp_repo, monkeypatch):
    _, did = store.update_decision(tmp_repo, "a decision to queue twice", "s1", subtype="architecture")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    share.enqueue_ids_for_retry(tmp_repo, [did])
    share.enqueue_ids_for_retry(tmp_repo, [did])  # dedup by decision_id
    assert len([e for e in share._load_outbox() if e["decision_id"] == did]) == 1


def test_enqueue_ids_for_retry_skips_missing(tmp_repo, monkeypatch):
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    n = share.enqueue_ids_for_retry(tmp_repo, ["no-such-id"])
    assert n == 0
    assert share._load_outbox() == []


def test_share_async_preserves_plan_source_on_wire(tmp_repo, monkeypatch):
    _, did = store.update_decision(
        tmp_repo, "provisional plan decision to sync", "s1",
        subtype="architecture", created_by="plan")
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    fake = _afake(monkeypatch, ret="srv-9")
    asyncio.run(share.share_async(tmp_repo, did, profile=TEAM))
    assert fake.calls[0]["source"] == "plan"


def test_drain_outbox_coerces_unknown_source(tmp_repo, monkeypatch):
    # A queued entry with a genuinely off-taxonomy source still degrades to "ai" on drain,
    # so an unknown value can never brick the outbox.
    share._enqueue({
        "decision_id": "weird-1", "type": "architecture", "content": "legacy weird entry",
        "repo": "github.com/a/b", "rationale": None, "confidence": 30, "evidence": None,
        "source": "totally-bogus", "queued_at": 0.0, "attempts": 26,
    })
    fake = _fake(monkeypatch, ret="srv-weird")
    sent = share.drain_outbox(profile=TEAM)
    assert sent == 1
    assert fake.batches[0][0]["source"] == "ai"


# ── batch capacity-skip + chunking (per-row best-effort) ─────────────────────────────

class _CapacityRS:
    """A fake that stores the FIRST row of each batch and reports the rest as at-capacity skips
    (server per-row best-effort). Sync + async twins."""

    def __init__(self):
        self.batches = []

    def _split(self, kwargs_list):
        self.batches.append(kwargs_list)
        saved = [f"srv-{kwargs_list[0]['decision_id']}"] if kwargs_list else []
        skipped = [{"decision_id": kw["decision_id"], "reason": "quota_exceeded"}
                   for kw in kwargs_list[1:]]
        return saved, skipped

    def push_decisions(self, kwargs_list):
        return self._split(kwargs_list)

    async def apush_decisions(self, kwargs_list):
        return self._split(kwargs_list)


def test_share_all_capacity_skip_requeues_only_skipped(tmp_repo, monkeypatch):
    # Server stores what fits and reports the overflow as skipped: the skipped rows are re-queued
    # (not lost, not blocking the saved one) and the message says "at capacity".
    projs = [{"id": f"id{i}", "type": "architecture", "content": f"d{i}",
              "confidence": None, "evidence": None, "source": "ai"} for i in range(3)]
    monkeypatch.setattr(store, "get_shareable_all", lambda repo: projs)
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: _CapacityRS()))
    remote.reset_degradation_warnings()
    msg = share.share_all(tmp_repo, profile=TEAM)
    assert msg.outcome == share_status.BATCH_DONE
    assert (msg.sent, msg.at_capacity, msg.lost) == (1, 2, 0)
    assert {e["decision_id"] for e in share._load_outbox()} == {"id1", "id2"}  # id0 saved, not queued


def test_a_capacity_skip_before_an_outage_is_not_counted_as_lost(tmp_repo, monkeypatch):
    """`lost` comes from the chunk index, not from `sent`.

    Three decisions, one per chunk. The server saves the first, skips the second at capacity (so
    `_requeue_skipped` queues it and counts it under `at_capacity`), then the transport fails on
    the third AND the outbox write for that third one fails too. Exactly ONE decision is
    unrecorded.

    `sent` trails `start` here, because the at-capacity row advanced the index without being
    sent. Deriving from `sent` gives `total - sent` = 2, so the status would claim TWO decisions
    were dropped when one was, and the one it double-counts is already safely on disk. That was a
    vague overstatement while it was only prose ("of the remaining 2"); as a number a caller acts
    on, it is a false report of data loss. No test distinguished the two, which is why this one
    drives the outbox write to fail for the last decision only."""
    projs = [{"id": f"id{i}", "type": "architecture", "content": f"d{i}",
              "confidence": None, "evidence": None, "source": "ai"} for i in range(3)]
    monkeypatch.setattr(store, "get_shareable_all", lambda repo: projs)
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    monkeypatch.setattr(share, "_BATCH_SIZE", 1)          # one decision per chunk
    calls = {"n": 0}

    class Staged:
        def push_decisions(self, rows):
            calls["n"] += 1
            if calls["n"] == 1:
                return ["srv-0"], []                       # id0 saved
            if calls["n"] == 2:                            # id1 skipped at capacity
                return [], [{"decision_id": rows[0]["decision_id"], "reason": "quota_exceeded"}]
            raise RemoteUnavailableError("down")           # id2: transport dies

    real_enqueue = share._enqueue

    def enqueue_but_not_id2(payload):
        if payload.get("decision_id") == "id2":
            raise OSError("disk full")                     # only the LAST one fails to queue
        real_enqueue(payload)

    monkeypatch.setattr(share, "_enqueue", enqueue_but_not_id2)
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: Staged()))
    remote.reset_degradation_warnings()
    msg = share.share_all(tmp_repo, profile=TEAM)

    assert msg.outcome == share_status.BATCH_STRANDED
    assert msg.sent == 1                                   # id0
    assert msg.at_capacity == 1                            # id1, queued and counted once
    assert msg.queued == 0                                 # id2 never made it to the outbox
    assert msg.lost == 1                                   # id2 only - NOT 2
    assert {e["decision_id"] for e in share._load_outbox()} == {"id1"}
    assert "Queued 0 for retry before the outbox write failed" in share_status.describe(msg)


def test_drain_outbox_capacity_skip_keeps_skipped_queued(tmp_repo, monkeypatch):
    for did, content in (("d1", "first"), ("d2", "second")):
        share._enqueue({"decision_id": did, "type": "architecture", "content": content,
                        "repo": "r", "rationale": None, "confidence": 80, "evidence": None,
                        "source": "ai", "queued_at": 1.0, "attempts": 0})
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: _CapacityRS()))
    remote.reset_degradation_warnings()
    sent = share.drain_outbox(TEAM)
    assert sent == 1  # d1 saved
    assert [e["decision_id"] for e in share._load_outbox()] == ["d2"]  # d2 at capacity -> kept


def test_share_ids_async_capacity_skip_requeues(tmp_repo, monkeypatch):
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    monkeypatch.setattr(store, "get_shareable", lambda repo, did="": {
        "id": did, "type": "constraint", "content": f"c-{did}",
        "confidence": None, "evidence": None, "source": "ai"})
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: _CapacityRS()))
    remote.reset_degradation_warnings()
    msg = asyncio.run(share.share_ids_async(tmp_repo, ["a", "b"], profile=TEAM))
    assert msg.outcome == share_status.BATCH_DONE and msg.at_capacity == 1
    assert {e["decision_id"] for e in share._load_outbox()} == {"b"}  # a saved, b at capacity -> queued


def test_share_all_capacity_skip_and_enqueue_failure_reports_lost(tmp_repo, monkeypatch):
    # A capacity-skipped row whose re-queue ALSO fails (disk full) is neither stored nor queued -
    # it must be counted+reported as lost, not silently swallowed (Greptile share.py#228).
    projs = [{"id": "id0", "type": "architecture", "content": "d0",
              "confidence": None, "evidence": None, "source": "ai"},
             {"id": "id1", "type": "architecture", "content": "d1",
              "confidence": None, "evidence": None, "source": "ai"}]
    monkeypatch.setattr(store, "get_shareable_all", lambda repo: projs)
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: _CapacityRS()))
    monkeypatch.setattr(share, "_enqueue", lambda payload: (_ for _ in ()).throw(OSError("disk full")))
    remote.reset_degradation_warnings()
    msg = share.share_all(tmp_repo, profile=TEAM)
    # id1 skipped AND un-queueable: `lost` is the count that says its share intent is gone
    assert msg.outcome == share_status.BATCH_DONE and msg.lost == 1
    assert share._load_outbox() == []


def test_share_all_chunks_large_selection(tmp_repo, monkeypatch):
    # More decisions than one batch allows -> ceil(N / _BATCH_SIZE) calls, one round-trip per chunk.
    projs = [{"id": f"id{i}", "type": "architecture", "content": f"d{i}",
              "confidence": None, "evidence": None, "source": "ai"} for i in range(5)]
    monkeypatch.setattr(store, "get_shareable_all", lambda repo: projs)
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    monkeypatch.setattr(share, "_BATCH_SIZE", 2)
    fake = _fake(monkeypatch, ret="srv-1")
    msg = share.share_all(tmp_repo, profile=TEAM)
    assert msg.outcome == share_status.BATCH_DONE and (msg.sent, msg.total) == (5, 5)
    assert [len(b) for b in fake.batches] == [2, 2, 1]  # 5 -> chunks of 2,2,1
    assert [a["decision_id"] for b in fake.batches for a in b] == [f"id{i}" for i in range(5)]
    assert share._load_outbox() == []


class _RejectRS:
    """Stores the FIRST row, PERMANENTLY rejects the rest (invalid type/content). Sync + async."""

    def __init__(self):
        self.batches = []

    def _split(self, kwargs_list):
        self.batches.append(kwargs_list)
        saved = [f"srv-{kwargs_list[0]['decision_id']}"] if kwargs_list else []
        skipped = [{"decision_id": kw["decision_id"], "reason": "invalid_type"}
                   for kw in kwargs_list[1:]]
        return saved, skipped

    def push_decisions(self, kwargs_list):
        return self._split(kwargs_list)

    async def apush_decisions(self, kwargs_list):
        return self._split(kwargs_list)


def test_share_all_invalid_skip_reported_and_dropped(tmp_repo, monkeypatch):
    # Server per-row-rejects invalid rows (unsupported type/content): REPORTED and DROPPED, not
    # re-queued (retrying is futile) - unlike capacity skips, which stay queued.
    projs = [{"id": f"id{i}", "type": "architecture", "content": f"d{i}",
              "confidence": None, "evidence": None, "source": "ai"} for i in range(3)]
    monkeypatch.setattr(store, "get_shareable_all", lambda repo: projs)
    monkeypatch.setattr(store, "run_git", lambda repo, *a: None)
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: _RejectRS()))
    remote.reset_degradation_warnings()
    msg = share.share_all(tmp_repo, profile=TEAM)
    assert msg.outcome == share_status.BATCH_DONE and msg.invalid == 2
    assert share._load_outbox() == []  # id0 saved; id1/id2 invalid -> dropped, NOT queued


def test_drain_outbox_invalid_skip_dropped_not_retried(tmp_repo, monkeypatch):
    for did in ("d1", "d2"):
        share._enqueue({"decision_id": did, "type": "architecture", "content": did,
                        "repo": "r", "rationale": None, "confidence": 80, "evidence": None,
                        "source": "ai", "queued_at": 1.0, "attempts": 0})
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: _RejectRS()))
    remote.reset_degradation_warnings()
    sent = share.drain_outbox(TEAM)
    assert sent == 1  # d1 saved
    assert share._load_outbox() == []  # d2 invalid -> dropped from outbox (can never sync)


def test_mark_shared_serializes_concurrent_writers(tmp_path, monkeypatch):
    """Two writers marking different ids must both survive (lost-update guard, PR #144).

    Without the lock each writer reads the same base, adds its own id, and the second save
    clobbers the first - leaving a genuinely-pushed decision looking unshared."""
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    ep = "http://localhost:8080/mcp"
    barrier = threading.Barrier(2)

    def writer(did):
        barrier.wait()          # maximize overlap on the read-modify-write
        share._mark_shared([did], ep)

    threads = [threading.Thread(target=writer, args=(f"id{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(share.shared_map(ep)) == {"id0", "id1"}


def test_mark_shared_still_fail_soft_when_locking_unavailable(tmp_path, monkeypatch):
    # A marker is cosmetic: even with no fcntl (non-POSIX) it must record, never raise.
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    monkeypatch.setattr(store, "fcntl", None)
    share._mark_shared(["abc"], "http://localhost:8080/mcp")
    assert "abc" in share.shared_map("http://localhost:8080/mcp")


def test_mark_shared_survives_concurrency_without_posix_locks(tmp_path, monkeypatch):
    """The append-only log must not lose markers where advisory locking is unavailable.

    `store.store_lock` yields WITHOUT serializing when fcntl is missing (non-POSIX), so a
    read-modify-write design would still drop a concurrent writer's marker there. Appending
    self-contained lines has no read to lose, so both writers survive on every platform."""
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    monkeypatch.setattr(store, "fcntl", None)  # simulate a runtime with no advisory locks
    ep = "http://localhost:8080/mcp"
    barrier = threading.Barrier(4)

    def writer(did):
        barrier.wait()
        share._mark_shared([did], ep)

    threads = [threading.Thread(target=writer, args=(f"id{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert set(share.shared_map(ep)) == {"id0", "id1", "id2", "id3"}


def test_shared_log_compacts_once_past_the_threshold(tmp_path, monkeypatch):
    # Append-only would grow without bound on repeated re-shares; compaction folds it back
    # to one record per (endpoint, id) while preserving every marker.
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    monkeypatch.setattr(share, "_SHARED_LOG_MAX_LINES", 10)
    ep = "http://localhost:8080/mcp"
    for _ in range(12):  # re-share the same two ids repeatedly
        share._mark_shared(["dup1", "dup2"], ep)
    lines = share._shared_path().read_text(encoding="utf-8").splitlines()
    # Bounded, not unbounded: compaction fires once the log passes the threshold, so the file
    # stays near it instead of growing to 24 lines. (It won't be exactly 2 - appends after the
    # last compaction are still there.)
    assert len(lines) <= share._SHARED_LOG_MAX_LINES
    assert set(share.shared_map(ep)) == {"dup1", "dup2"}  # nothing lost


@pytest.mark.skipif(store.fcntl is None, reason="advisory locks unavailable on this platform")
def test_shared_log_append_is_excluded_during_compaction(tmp_path, monkeypatch):
    """Compaction replaces the log wholesale, so it must exclude concurrent appends.

    Appending is atomic against other appends, but NOT against the rewrite: a marker landing
    between compaction's fold and its atomic replace goes to an inode the rename is about to
    discard. Both sides take the same lock, so the append waits instead of vanishing. The
    replace is stalled here to hold that window wide open - without the lock the fresh marker
    lands on the doomed inode and is lost."""
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    monkeypatch.setattr(share, "_SHARED_LOG_MAX_LINES", 10)
    ep = "http://localhost:8080/mcp"
    # Seed past the threshold via _append_shared (which never compacts), so compaction is
    # armed but has not run yet.
    share._append_shared([{"endpoint": ep, "id": f"old{i}", "at": "t"} for i in range(11)])

    folded = threading.Event()
    real_atomic = store.atomic_write

    def stalled_atomic(path, text):
        folded.set()        # compaction has read the log; the replace is now pending
        time.sleep(0.2)     # widen the fold->replace window the appender must not slip into
        real_atomic(path, text)

    monkeypatch.setattr(store, "atomic_write", stalled_atomic)

    def appender():
        folded.wait(5)
        share._append_shared([{"endpoint": ep, "id": "fresh", "at": "t"}])

    t = threading.Thread(target=appender)
    t.start()
    share._compact_shared()
    t.join(10)

    markers = share.shared_map(ep)
    assert "fresh" in markers                                 # survived the rewrite
    assert {f"old{i}" for i in range(11)} <= set(markers)      # and nothing pre-existing lost


# ── share --global (issue #239) ──────────────────────────────────────────────────
# Global rules live in _global.json, which no repo_path can select, so every repo-scoped
# share path is structurally blind to them. These pin the one thing that makes them
# reachable: selection from the global store, and `repo=None` on the wire so the server
# stores an unbound row rather than binding a cross-repo rule to one arbitrary repo.

def _add_globals(*contents: str) -> list[str]:
    return [store.update_global_decision(c, "s1", "convention")[1] for c in contents]


def test_share_global_nothing_to_share(tmp_repo):
    status = share.share_global(profile=TEAM)
    assert (status.outcome, status.scope) == (share_status.NOTHING_TO_SHARE, "global")


def test_share_global_local_mode_message(tmp_repo, monkeypatch):
    _add_globals("always use conventional commits here")
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: None))
    assert share.share_global(
        profile=config.Profile()).outcome == share_status.NOT_TEAM_MODE


def test_share_global_pushes_every_rule_unbound(tmp_repo, monkeypatch):
    ids = _add_globals("always use conventional commits here",
                       "never use an em dash in prose")
    fake = _fake(monkeypatch, ret="srv-9")
    msg = share.share_global(profile=TEAM)
    assert msg.outcome == share_status.BATCH_DONE and (msg.sent, msg.total) == (2, 2)
    assert len(fake.batches) == 1                                   # one batched call
    assert [c["decision_id"] for c in fake.batches[0]] == ids       # oldest first
    # The whole point: no repo binding. _wire_args omits `repo` when it is None, which is
    # what makes the server row unbound (repo IS NULL) and therefore global.
    assert all(c["repo"] is None for c in fake.batches[0])
    assert share._load_outbox() == []


def test_share_global_does_not_call_git(tmp_repo, monkeypatch):
    """No repo means no origin to derive a key from. A stray _git call here would be the
    bug the repo-scoped paths have: binding a cross-repo rule to whatever repo cwd is in."""
    _add_globals("always use conventional commits here")
    monkeypatch.setattr(store, "run_git", lambda *a, **k: pytest.fail("share_global must not shell out to git"))
    _fake(monkeypatch, ret="srv-1")
    share.share_global(profile=TEAM)


def test_share_global_excludes_ignored(tmp_repo, monkeypatch):
    keep, drop = _add_globals("always use conventional commits here",
                              "never use an em dash in prose")
    data = store.load_global()
    next(e for e in data["entries"] if e["id"] == drop)["status"] = "ignored"
    store.save_global(data)
    fake = _fake(monkeypatch, ret="srv-1")
    share.share_global(profile=TEAM)
    assert [c["decision_id"] for c in fake.batches[0]] == [keep]


def test_share_global_carries_title_and_redaction(tmp_repo, monkeypatch):
    """Reuses _share_projection, so title derivation and egress redaction come for free -
    pinned here because a hand-rolled global projection would silently lose both."""
    store.update_global_decision("never hardcode AKIAIOSFODNN7EXAMPLE in any repo", "s1",
                                 "constraint", title="No hardcoded keys")
    fake = _fake(monkeypatch, ret="srv-1")
    share.share_global(profile=TEAM)
    sent = fake.batches[0][0]
    assert sent["title"] == "No hardcoded keys"
    assert "AKIAIOSFODNN7EXAMPLE" not in sent["content"]


def test_share_global_failure_queues_with_no_repo(tmp_repo, monkeypatch):
    """A queued global keeps repo=None through the outbox, so a later drain still pushes
    it unbound rather than re-deriving a repo from wherever the drain happens to run."""
    _add_globals("always use conventional commits here")
    _fake(monkeypatch, exc=RemoteUnavailableError("down"))
    msg = share.share_global(profile=TEAM)
    assert msg.outcome == share_status.BATCH_INTERRUPTED and msg.queued == 1
    queued = share._load_outbox()
    assert len(queued) == 1 and queued[0]["repo"] is None


def test_cli_share_global_flag(monkeypatch, capsys, tmp_repo):
    from contexer import cli
    monkeypatch.setattr(store, "git_root", lambda p: None)   # NOT in a git repo
    monkeypatch.setattr(share, "share_global", lambda **kw:
                        share_status.ShareStatus(share_status.BATCH_DONE, sent=2, total=2))
    cli.share_cmd(["--global", "--yes"])
    assert "Synced 2 decision(s)" in capsys.readouterr().out


def test_cli_share_global_needs_no_repo(monkeypatch, capsys, tmp_repo):
    """The repo check is skipped for --global: global rules belong to no repo, so exiting
    on 'no git repo detected' would be a gate with nothing behind it."""
    from contexer import cli
    monkeypatch.setattr(store, "git_root", lambda p: None)
    monkeypatch.setattr(store, "resolve_repo", lambda p: "")
    monkeypatch.setattr(share, "share_global", lambda **kw: _ok_status())
    cli.share_cmd(["--global", "--yes"])          # must not SystemExit
    assert "Synced decision" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["--global", "--all"], ["--global", "abc12345"]])
def test_cli_share_global_is_exclusive(monkeypatch, capsys, argv):
    from contexer import cli
    monkeypatch.setattr(store, "git_root", lambda p: "/repo")
    with pytest.raises(SystemExit):
        cli.share_cmd(argv + ["--yes"])
    assert "exactly one" in capsys.readouterr().err


def test_cli_share_global_previews_and_cancels_on_no(monkeypatch, capsys, tmp_repo):
    from contexer import cli
    monkeypatch.setattr(store, "git_root", lambda p: None)
    monkeypatch.setattr(config, "load_profile", lambda *a, **k: config.Profile())
    store.update_global_decision("never use an em dash in prose", "s1", "convention")
    pushed = {"n": 0}
    monkeypatch.setattr(share, "share_global", lambda **k: pushed.__setitem__("n", pushed["n"] + 1) or _ok_status())
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    cli.share_cmd(["--global"])
    out = capsys.readouterr().out
    assert "em dash" in out and "GLOBAL" in out and "Cancelled" in out
    assert pushed["n"] == 0


def test_cli_share_all_points_at_the_global_flag(monkeypatch, capsys, tmp_repo):
    """`--all` is repo-scoped by construction, so it must not let 'all' read as all."""
    from contexer import cli
    store.update_global_decision("never use an em dash in prose", "s1", "convention")
    monkeypatch.setattr(store, "git_root", lambda p: "/repo")
    monkeypatch.setattr(share, "share_all", lambda repo, **kw: _ok_status())
    cli.share_cmd(["--all", "--yes"])
    out = capsys.readouterr().out
    assert "1 global rule(s) were not included" in out and "--global" in out
