"""Schema, security, and concurrency contracts for remembered proposal sidecars."""
import contextlib
import json
import os
import re
import threading
import time
import uuid

import pytest

from contexer import remote, revisions, share_policy, store
from contexer.config import ConfigError, Profile


NOW = "2026-08-30T12:00:00Z"
FINGERPRINT = "acctfp_v1_7M4Q2PX9C6N8"
_REAL_START_IN_PROCESS_DRAINER = share_policy.start_in_process_drainer


@pytest.fixture(autouse=True)
def _quiet_diagnostics(monkeypatch):
    monkeypatch.setattr(
        share_policy.decision_observability,
        "emit_decision_operation",
        lambda *_args, **_kwargs: None,
    )


def _entry(decision_id="decision-1", revision_id="revision-1", *, status="approved",
           source="ai", approved_by="human"):
    entry = {
        "id": decision_id,
        "type": "decision",
        "status": status,
        "approved_by": approved_by,
        "current_revision_id": revision_id,
        "revisions": [{
            "revision_id": revision_id,
            "decision_id": decision_id,
            "version_number": 1,
            "content": "Keep secrets local",
            "source": source,
        }],
    }
    return entry


def _policy(tmp_repo, entries=(), *, include_existing=False):
    return share_policy.build_policy(
        repo_path=tmp_repo,
        repo_key="github.com/org/repo",
        endpoint="https://mcp.contexer.ai/mcp",
        account_fingerprint=FINGERPRINT,
        team_id="team-1",
        team_name="Platform",
        entries=list(entries),
        include_existing=include_existing,
        now=NOW,
        policy_generation="policy-1",
    )


def _intent(tmp_repo, *, decision_id="decision-1", revision_id="revision-2",
            idempotency_key="intent-1"):
    return {
        "schema_version": 1,
        "idempotency_key": idempotency_key,
        "policy_generation": "policy-1",
        "decision_id": decision_id,
        "revision_id": revision_id,
        "repo_path": tmp_repo,
        "repo_key": "github.com/org/repo",
        "endpoint": "https://mcp.contexer.ai/mcp",
        "account_fingerprint": FINGERPRINT,
        "team_id": "team-1",
        "queued_at": NOW,
        "attempts": 0,
        "last_error_code": None,
        "last_error_class": None,
        "diagnostic_id": None,
    }


def _receipt(*, decision_id="decision-1", revision_id="revision-2", state="queued"):
    return {
        "schema_version": 1,
        "policy_generation": "policy-1",
        "endpoint": "https://mcp.contexer.ai/mcp",
        "account_fingerprint": FINGERPRINT,
        "repo_key": "github.com/org/repo",
        "team_id": "team-1",
        "decision_id": decision_id,
        "revision_id": revision_id,
        "state": state,
        "candidate_id": "candidate-1" if state in {"submitted", "already_pending"} else None,
        "reason": "not_member" if state == "attention" else None,
        "recorded_at": NOW,
    }


def _attention(tmp_repo, *, decision_id="decision-1", revision_id="revision-2",
               idempotency_key="intent-1"):
    intent = _intent(tmp_repo, decision_id=decision_id, revision_id=revision_id,
                     idempotency_key=idempotency_key)
    return {
        **{key: value for key, value in intent.items()
           if key not in {"queued_at", "attempts"}},
        "reason": "not_member",
        "moved_at": NOW,
        "last_error_code": "not_member",
        "last_error_class": "authorization",
        "diagnostic_id": "diag_4Z7K2N8Q5W1C9M6P",
    }


def test_contract_sidecar_examples_parse_byte_for_field(tmp_repo):
    contract = json.loads(
        (store.Path(__file__).parent / "fixtures" /
         "decision-sharing-transition-contract.v1.json").read_text(encoding="utf-8")
    )["sidecars"]

    assert share_policy.parse_policy(contract["policy"]) == contract["policy"]
    assert share_policy.parse_intent(contract["intent"]) == contract["intent"]
    assert share_policy.parse_attention(contract["attention"]) == contract["attention"]
    assert [share_policy.parse_receipt(row) for row in contract["receipts"]] == \
        contract["receipts"]


@pytest.mark.parametrize("mutate", [
    lambda p: p.update(schema_version=2),
    lambda p: p.update(mode="manual"),
    lambda p: p.update(account_fingerprint="raw-user-id"),
    lambda p: p.update(repo_key=""),
    lambda p: p.update(include_existing="false"),
    lambda p: p.update(baseline_revision_ids=["same", "same"]),
    lambda p: p.update(paused_reason="free-form error prose"),
])
def test_policy_schema_is_strict(tmp_repo, mutate):
    policy = _policy(tmp_repo)
    mutate(policy)
    with pytest.raises(ValueError):
        share_policy.parse_policy(policy)


def test_intent_rejects_unknown_fields_and_non_http_destination(tmp_repo):
    intent = {**_intent(tmp_repo), "content": "SENTINEL_SECRET"}
    with pytest.raises(ValueError, match="fields"):
        share_policy.parse_intent(intent)
    intent = _intent(tmp_repo)
    intent["endpoint"] = "file:///private/token"
    with pytest.raises(ValueError, match="endpoint"):
        share_policy.parse_intent(intent)


def test_destination_match_is_exact_for_every_binding(tmp_repo):
    policy = _policy(tmp_repo)
    assert share_policy.destination_matches(policy, policy)
    for field in ("policy_generation", "endpoint", "account_fingerprint", "repo_key", "team_id"):
        changed = {**policy, field: policy[field].upper()}
        assert not share_policy.destination_matches(policy, changed), field
    assert not share_policy.destination_matches(policy, {**policy, "repo_key": None})


def test_future_only_baseline_includes_every_current_decision_revision(tmp_repo):
    approved = _entry("approved", "rev-approved", status="approved")
    pending = _entry("pending", "rev-pending", status="pending_approval")
    policy = _policy(tmp_repo, [approved, pending])

    assert policy["baseline_revision_ids"] == ["rev-approved", "rev-pending"]
    assert [row["state"] for row in share_policy.baseline_receipts(
        policy, [approved, pending])] == ["baseline", "baseline"]
    assert _policy(tmp_repo, [approved], include_existing=True)["baseline_revision_ids"] == []


def test_eligibility_requires_approved_repo_revision_after_baseline(tmp_repo):
    policy = _policy(tmp_repo, [_entry()])
    newer = _entry(revision_id="revision-2")

    assert share_policy.eligibility(
        newer, policy, {}, repo_key=policy["repo_key"], is_global=False).eligible
    assert share_policy.eligibility(
        _entry(status="suggested"), policy, {}, repo_key=policy["repo_key"],
        is_global=False).reason_code == \
        "ineligible_revision"
    assert share_policy.eligibility(
        _entry(), policy, {}, repo_key=policy["repo_key"],
        is_global=False).reason_code == "baseline_revision"
    assert share_policy.eligibility(
        newer, policy, {}, repo_key=None, is_global=True).reason_code == "global_decision"
    assert share_policy.eligibility(
        newer, policy, {}, repo_key="github.com/org/other", is_global=False).reason_code == \
        "repo_mismatch"
    receipts = share_policy.fold_receipts([_receipt(state="submitted")])
    assert share_policy.eligibility(
        newer, policy, receipts, repo_key=policy["repo_key"],
        is_global=False).reason_code == "duplicate_receipt"


@pytest.mark.parametrize("source", ["memory", "scan", "bootstrap", "ai", "plan"])
def test_born_approved_nonhuman_revision_is_never_automatically_eligible(tmp_repo, source):
    policy = _policy(tmp_repo)
    entry = _entry(revision_id=f"revision-{source}", source=source, approved_by=None)

    result = share_policy.eligibility(
        entry, policy, {}, repo_key=policy["repo_key"], is_global=False)

    assert not result.eligible
    assert result.reason_code == "ineligible_revision"


def test_direct_human_revision_is_explicit_approval_without_reviewer_stamp(tmp_repo):
    policy = _policy(tmp_repo)
    entry = _entry(revision_id="revision-human", source="human", approved_by=None)
    assert share_policy.eligibility(
        entry, policy, {}, repo_key=policy["repo_key"], is_global=False).eligible


def test_missing_or_non_current_revision_is_ineligible(tmp_repo):
    policy = _policy(tmp_repo)
    no_revision = _entry()
    no_revision["revisions"] = []
    assert share_policy.eligibility(
        no_revision, policy, {}, repo_key=policy["repo_key"],
        is_global=False).reason_code == \
        "ineligible_revision"


def test_intent_folding_keeps_first_idempotency_key(tmp_repo):
    first = _intent(tmp_repo, idempotency_key="first")
    duplicate = _intent(tmp_repo, idempotency_key="later")
    duplicate["attempts"] = 3

    assert share_policy.fold_intents([first, duplicate]) == [first]


def test_intent_identity_keeps_distinct_policy_generations(tmp_repo):
    old = _intent(tmp_repo, idempotency_key="old-generation")
    replacement = {
        **_intent(tmp_repo, idempotency_key="replacement-generation"),
        "policy_generation": "policy-2",
    }

    assert share_policy.fold_intents([old, replacement]) == [old, replacement]
    assert share_policy.intent_key(old) != share_policy.intent_key(replacement)
    assert share_policy.receipt_key(old) == share_policy.receipt_key(replacement)


def test_corrupt_outbox_refuses_enqueue_without_overwrite(tmp_repo):
    path = share_policy.proposal_outbox_path()
    path.parent.mkdir(parents=True)
    original = b"{not-json\nSENTINEL_OLD_QUEUE"
    path.write_bytes(original)

    outcome = share_policy.enqueue_intent(_intent(tmp_repo))

    assert outcome.result == "failure"
    assert outcome.reason_code == "corrupt_queue"
    assert re.fullmatch(r"diag_[A-Z0-9]{16}", outcome.diagnostic_id or "")
    assert path.read_bytes() == original


def test_outbox_cap_refuses_new_identity_and_keeps_existing(tmp_repo, monkeypatch):
    monkeypatch.setattr(share_policy, "OUTBOX_CAP", 1)
    assert share_policy.enqueue_intent(_intent(tmp_repo)).result == "queued"

    outcome = share_policy.enqueue_intent(
        _intent(tmp_repo, decision_id="decision-2", revision_id="revision-3"))

    assert outcome.result == "failure"
    assert [row["decision_id"] for row in share_policy.read_outbox()] == ["decision-1"]


def test_nonblocking_busy_queue_lock_returns_without_mutation(tmp_repo):
    with share_policy._sidecar_lock(share_policy.proposal_outbox_lock_path()):
        outcome = share_policy.enqueue_intent(_intent(tmp_repo), blocking=False)

    assert outcome == share_policy.OperationOutcome("no_op", "lock_busy")
    assert not share_policy.proposal_outbox_path().exists()


def test_policy_lock_is_independent_from_outbox_lock(tmp_repo):
    with share_policy._sidecar_lock(share_policy.policy_lock_path(tmp_repo)):
        outcome = share_policy.enqueue_intent(_intent(tmp_repo), blocking=False)
    assert outcome.result == "queued"


def test_remove_after_drain_preserves_intent_enqueued_after_snapshot(tmp_repo):
    first = _intent(tmp_repo)
    second = _intent(tmp_repo, decision_id="decision-2", revision_id="revision-3",
                     idempotency_key="intent-2")
    share_policy.enqueue_intent(first)
    snapshot = share_policy.read_outbox()
    share_policy.enqueue_intent(second)

    assert share_policy.remove_intents(snapshot) == 1
    assert share_policy.read_outbox() == [second]


def test_receipt_corruption_refuses_append_without_overwrite(tmp_repo):
    path = share_policy.proposal_receipts_path()
    path.parent.mkdir(parents=True)
    original = b'{"schema_version":1}\nnot-json\n'
    path.write_bytes(original)

    with pytest.raises(share_policy.SidecarDataError) as raised:
        share_policy.append_receipt(_receipt())

    assert re.fullmatch(r"diag_[A-Z0-9]{16}", raised.value.diagnostic_id)
    assert path.read_bytes() == original


def test_receipt_append_is_serialized_and_tail_capped(tmp_repo, monkeypatch):
    monkeypatch.setattr(share_policy, "RECEIPT_LOG_CAP", 20)
    errors = []

    def append(index):
        try:
            share_policy.append_receipt(_receipt(
                decision_id=f"decision-{index}", revision_id=f"revision-{index}"))
        except Exception as exc:  # pragma: no cover - assertion reports the worker error
            errors.append(exc)

    threads = [threading.Thread(target=append, args=(index,)) for index in range(30)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    receipts = share_policy.read_receipts()
    assert len(receipts) == 20
    assert len({row["decision_id"] for row in receipts}) == 20


def test_receipt_compaction_keeps_recent_update_not_first_seen_order(tmp_repo, monkeypatch):
    monkeypatch.setattr(share_policy, "RECEIPT_LOG_CAP", 2)
    a_queued = _receipt(decision_id="a", revision_id="ra")
    b_queued = _receipt(decision_id="b", revision_id="rb")
    a_submitted = {**a_queued, "state": "submitted", "candidate_id": "candidate-a"}
    c_queued = _receipt(decision_id="c", revision_id="rc")

    for receipt in (a_queued, b_queued, a_submitted, c_queued):
        share_policy.append_receipt(receipt)

    folded = share_policy.fold_receipts(share_policy.read_receipts())
    assert set(folded) == {
        share_policy.receipt_key(a_submitted),
        share_policy.receipt_key(c_queued),
    }
    assert folded[share_policy.receipt_key(a_submitted)]["state"] == "submitted"


def test_attention_is_deduplicated_bounded_and_corruption_safe(tmp_repo, monkeypatch):
    monkeypatch.setattr(share_policy, "ATTENTION_CAP", 1)
    first = _attention(tmp_repo)
    share_policy.append_attention(first)
    share_policy.append_attention(first)
    assert share_policy.read_attention() == [first]
    with pytest.raises(RuntimeError, match="queue is full"):
        share_policy.append_attention(_attention(
            tmp_repo, decision_id="decision-2", revision_id="revision-3",
            idempotency_key="intent-2"))

    path = share_policy.proposal_attention_path()
    path.write_text("not-json", encoding="utf-8")
    with pytest.raises(share_policy.SidecarDataError):
        share_policy.append_attention(first)
    assert path.read_text(encoding="utf-8") == "not-json"


def test_policy_write_refuses_to_overwrite_corruption(tmp_repo):
    path = share_policy.policy_path(tmp_repo)
    path.parent.mkdir(parents=True)
    path.write_text("not-json", encoding="utf-8")

    with pytest.raises(share_policy.SidecarDataError):
        share_policy.save_policy(tmp_repo, _policy(tmp_repo))
    assert path.read_text(encoding="utf-8") == "not-json"


def test_enqueue_telemetry_cannot_receive_sensitive_values(tmp_repo, monkeypatch):
    calls = []
    monkeypatch.setattr(
        share_policy.decision_observability,
        "emit_decision_operation",
        lambda operation, **fields: calls.append((operation, fields)),
    )
    intent = _intent(tmp_repo)
    intent.update({
        "repo_path": "/private/SENTINEL_REPOSITORY",
        "endpoint": "https://SENTINEL_ENDPOINT.invalid/mcp",
        "account_fingerprint": "acctfp_v1_SENTINELACCOUNT1",
    })

    assert share_policy.enqueue_intent(intent).result == "queued"

    encoded = json.dumps(calls)
    for sentinel in ("SENTINEL_REPOSITORY", "SENTINEL_ENDPOINT", "SENTINELACCOUNT"):
        assert sentinel not in encoded
    assert calls[0][0] == "enqueue"


def test_enqueue_telemetry_carries_only_opaque_runtime_correlation(tmp_repo, monkeypatch):
    calls = []
    monkeypatch.setattr(
        share_policy.decision_observability,
        "emit_decision_operation",
        lambda operation, **fields: calls.append((operation, fields)),
    )
    intent = {
        **_intent(tmp_repo),
        "decision_id": "20000000-0000-4000-8000-000000000001",
        "revision_id": "21000000-0000-4000-8000-000000000001",
        "policy_generation": "30000000-0000-4000-8000-000000000001",
        "team_id": "40000000-0000-4000-8000-000000000001",
        "idempotency_key": "50000000-0000-4000-8000-000000000001",
        "attempts": 3,
    }

    assert share_policy.enqueue_intent(intent).result == "queued"

    operation, fields = calls[0]
    assert operation == "enqueue"
    assert fields["decision_id"] == intent["decision_id"]
    assert fields["policy_generation"] == intent["policy_generation"]
    assert fields["idempotency_key"] == intent["idempotency_key"]
    assert fields["team_id"] == intent["team_id"]
    assert fields["attempt"] == 3
    assert fields["queue_depth"] == 1
    assert "endpoint" not in fields and "account_fingerprint" not in fields


def test_status_distinguishes_local_queue_from_remote_states(tmp_repo):
    snapshot = share_policy.status_snapshot(
        _policy(tmp_repo), [_intent(tmp_repo)],
        [_receipt(state="submitted"),
         _receipt(decision_id="decision-2", revision_id="revision-3", state="unchanged")],
        [_attention(tmp_repo)], uploading=True,
    )

    assert snapshot == {
        "policy": "active",
        "paused_reason": None,
        "queued": 1,
        "uploading": True,
        "pending_lead_review": 1,
        "already_current": 1,
        "attention": 1,
        "baseline": 0,
    }
    rendered = share_policy.render_status(snapshot)
    assert "queued" in rendered and "pending lead review" in rendered
    assert "shared" not in rendered


def _prepare_activation(tmp_repo, monkeypatch, *, entries=(), include_existing=False,
                        teams=None, automatic=True, skip_confirm=True):
    remote_store = _ProposalRemote(automatic=automatic)
    if teams is not None:
        remote_store.teams = teams
    monkeypatch.setattr(
        share_policy.remote.RemoteStore,
        "from_profile",
        staticmethod(lambda _profile, **kwargs: (
            remote_store if kwargs == {"reactive_refresh": False}
            else pytest.fail("policy activation must pin the inspected credential")
        )),
    )
    monkeypatch.setattr(store, "run_git", lambda *_args: "git@github.com:org/repo.git")
    monkeypatch.setattr(store, "load", lambda _repo: {"entries": list(entries)})
    profile = Profile(
        mode="team", endpoint="https://mcp.contexer.ai/mcp", token="opaque-token",
        skip_confirm=skip_confirm, redact_secrets=True,
    )
    return share_policy.prepare_policy_activation(
        tmp_repo, "Platform", include_existing=include_existing, profile=profile), remote_store


def test_policy_activation_preview_contains_exact_safe_confirmation_fields(
        tmp_repo, monkeypatch):
    entries = [_entry("existing", "existing-revision")]
    (preview, outcome), remote_store = _prepare_activation(
        tmp_repo, monkeypatch, entries=entries)

    assert outcome == share_policy.OperationOutcome("success", "none")
    assert preview is not None
    rendered = share_policy.format_policy_activation_preview(preview)
    assert "https://mcp.contexer.ai/mcp" in rendered
    assert "…X9C6N8" in rendered
    assert FINGERPRINT not in rendered
    assert "github.com/org/repo" in rendered
    assert "Platform (team-1)" in rendered
    assert "future-only" in rendered
    assert "Secret redaction: on" in rendered
    assert "No policy has been changed yet" in rendered
    assert not share_policy.policy_path(tmp_repo).exists()
    assert remote_store.list_calls == 1


def test_policy_output_strips_endpoint_userinfo_query_and_fragment(tmp_repo):
    policy = {
        **_policy(tmp_repo),
        "endpoint": "https://user:secret@example.test/mcp?token=SENTINEL#private",
    }
    preview = share_policy.PolicyActivationPreview(
        repo_path=tmp_repo, policy=policy, entries=[], include_existing=False,
        initial_proposal_count=0, baseline_count=0, redaction_enabled=True,
        replacing_policy=False,
    )
    rendered_preview = share_policy.format_policy_activation_preview(preview)
    rendered_status = share_policy.format_policy_status({
        "policy": "active", "paused_reason": None, "queued": 0, "uploading": False,
        "pending_lead_review": 0, "already_current": 0, "attention": 0,
        "repo_key": policy["repo_key"], "team_name": "Platform", "team_id": "team-1",
        "endpoint": policy["endpoint"], "account_fingerprint_suffix": "X9C6N8",
        "scope": "future-only",
    })

    assert "https://example.test/mcp" in rendered_preview
    assert "https://example.test/mcp" in rendered_status
    for secret in ("user", "secret", "token", "SENTINEL", "private"):
        assert secret not in rendered_preview
        assert secret not in rendered_status


def test_future_only_activation_persists_baseline_without_queueing_existing(
        tmp_repo, monkeypatch):
    entry = _entry("existing", "existing-revision")
    (preview, _outcome), _remote_store = _prepare_activation(
        tmp_repo, monkeypatch, entries=[entry])

    result = share_policy.activate_policy(preview)

    assert result == share_policy.ScanOutcome("success", "none", 0, 0, 0)
    assert share_policy.load_policy(tmp_repo)["baseline_revision_ids"] == ["existing-revision"]
    assert [row["state"] for row in share_policy.read_receipts()] == ["baseline"]
    assert share_policy.read_outbox() == []


def test_future_only_activation_queues_first_later_human_approval_without_prompt(
        tmp_repo, monkeypatch):
    existing = _entry("existing", "existing-revision")
    (preview, outcome), _remote_store = _prepare_activation(
        tmp_repo, monkeypatch, entries=[existing])
    assert outcome == share_policy.OperationOutcome("success", "none")
    assert preview is not None
    assert share_policy.activate_policy(preview).result == "success"

    approved_later = _entry(
        "approved-later", "approved-later-revision", source="human", approved_by="reviewer")
    monkeypatch.setattr(
        store, "load", lambda _repo: {"entries": [existing, approved_later]})

    scanned = share_policy.scan_and_enqueue(tmp_repo, start_worker=False)

    assert scanned == share_policy.ScanOutcome("queued", "none", 2, 1, 1)
    assert [row["revision_id"] for row in share_policy.read_outbox()] == [
        "approved-later-revision",
    ]
    assert [row["state"] for row in share_policy.read_receipts()] == [
        "baseline", "queued",
    ]


def test_activation_refuses_before_replacing_policy_when_global_sidecar_is_corrupt(
        tmp_repo, monkeypatch):
    monkeypatch.setattr(store, "run_git", lambda *_args: "git@github.com:org/repo.git")
    existing = _policy(tmp_repo, include_existing=True)
    share_policy.save_policy(tmp_repo, existing)
    receipts = share_policy.proposal_receipts_path()
    receipts.parent.mkdir(parents=True, exist_ok=True)
    receipts.write_text("not-json", encoding="utf-8")
    replacement = share_policy.PolicyActivationPreview(
        repo_path=tmp_repo,
        policy={**existing, "policy_generation": "policy-2"},
        entries=[],
        include_existing=True,
        initial_proposal_count=0,
        baseline_count=0,
        redaction_enabled=True,
        replacing_policy=True,
    )

    with pytest.raises(share_policy.SidecarDataError):
        share_policy.activate_policy(replacement)

    assert share_policy.load_policy(tmp_repo)["policy_generation"] == "policy-1"


@pytest.mark.parametrize("failure,reason", [
    (OSError("disk unavailable"), "validation_error"),
    (share_policy.SidecarDataError(
        "proposal receipts", "diag_4Z7K2N8Q5W1C9M6P"), "corrupt_queue"),
])
def test_activation_reports_committed_policy_when_baseline_mirror_fails(
        tmp_repo, monkeypatch, failure, reason):
    monkeypatch.setattr(store, "run_git", lambda *_args: "git@github.com:org/repo.git")
    entry = _entry("existing", "existing-revision")
    policy = _policy(tmp_repo, [entry])
    preview = share_policy.PolicyActivationPreview(
        repo_path=tmp_repo, policy=policy, entries=[entry], include_existing=False,
        initial_proposal_count=0, baseline_count=1, redaction_enabled=True,
        replacing_policy=False,
    )
    monkeypatch.setattr(
        share_policy, "append_receipts", lambda _rows: (_ for _ in ()).throw(failure))

    outcome = share_policy.activate_policy(preview)

    assert outcome == share_policy.ScanOutcome("success", reason)
    stored = share_policy.load_policy(tmp_repo)
    assert stored is not None
    assert stored["paused_reason"] is None
    assert stored["baseline_revision_ids"] == ["existing-revision"]


def test_include_existing_activation_queues_only_eligible_human_approved_revisions(
        tmp_repo, monkeypatch):
    eligible = _entry("eligible", "eligible-revision")
    nonhuman = _entry("nonhuman", "nonhuman-revision", source="ai", approved_by=None)
    entries = [eligible, nonhuman]
    (preview, _outcome), _remote_store = _prepare_activation(
        tmp_repo, monkeypatch, entries=entries, include_existing=True)

    result = share_policy.activate_policy(preview)

    assert result == share_policy.ScanOutcome("queued", "none", 2, 1, 1)
    assert share_policy.load_policy(tmp_repo)["baseline_revision_ids"] == []
    assert [row["decision_id"] for row in share_policy.read_outbox()] == ["eligible"]


def test_include_existing_activation_preserves_queue_when_detached_launch_fails(
        tmp_repo, monkeypatch):
    entry = _entry("eligible", "eligible-revision")
    (preview, _outcome), _remote_store = _prepare_activation(
        tmp_repo, monkeypatch, entries=[entry], include_existing=True)
    monkeypatch.setattr(share_policy, "start_detached_drainer", lambda: False)

    result = share_policy.activate_policy(preview)

    assert result.result == "queued"
    assert result.reason_code == "validation_error"
    assert result.queued == 1
    assert [row["decision_id"] for row in share_policy.read_outbox()] == ["eligible"]


def test_include_existing_preview_count_honors_terminal_receipts(tmp_repo, monkeypatch):
    entry = _entry("decision-1", "revision-2")
    share_policy.append_receipt(_receipt(state="submitted"))

    (preview, outcome), _remote_store = _prepare_activation(
        tmp_repo, monkeypatch, entries=[entry], include_existing=True)

    assert outcome == share_policy.OperationOutcome("success", "none")
    assert preview is not None
    assert preview.initial_proposal_count == 0


def test_policy_activation_requires_capability_and_unique_exact_team(
        tmp_repo, monkeypatch):
    (preview, outcome), _remote_store = _prepare_activation(
        tmp_repo, monkeypatch, automatic=False)
    assert preview is None and outcome.reason_code == "unsupported_protocol"
    assert not share_policy.policy_path(tmp_repo).exists()
    duplicate_name = [
        remote.RemoteTeam("team-1", "Platform", "member"),
        remote.RemoteTeam("team-2", "Platform", "member"),
    ]
    (preview, outcome), _remote_store = _prepare_activation(
        tmp_repo, monkeypatch, teams=duplicate_name)
    assert preview is None and outcome.reason_code == "team_mismatch"
    assert not share_policy.policy_path(tmp_repo).exists()


def test_policy_activation_fails_closed_on_malformed_profile(tmp_repo, monkeypatch):
    monkeypatch.setattr(
        share_policy, "load_profile",
        lambda: (_ for _ in ()).throw(ConfigError("SENTINEL_CONFIG_CONTENT")),
    )

    preview, outcome = share_policy.prepare_policy_activation(tmp_repo, "Platform")

    assert preview is None
    assert outcome.result == "refused" and outcome.reason_code == "validation_error"
    assert outcome.diagnostic_id is not None


def test_disabling_policy_preserves_global_queue_receipts_and_attention(tmp_repo):
    share_policy.save_policy(tmp_repo, _policy(tmp_repo, include_existing=True))
    intent = _intent(tmp_repo)
    assert share_policy.enqueue_intent(intent).result == "queued"
    share_policy.append_receipt(share_policy.queued_receipt(intent))
    share_policy.append_attention(_attention(tmp_repo))

    assert share_policy.disable_policy(tmp_repo) is True

    assert share_policy.load_policy(tmp_repo) is None
    assert share_policy.read_outbox() == [intent]
    assert len(share_policy.read_receipts()) == 1
    assert len(share_policy.read_attention()) == 1


def test_policy_status_uses_authoritative_baseline_and_never_says_shared(
        tmp_repo, monkeypatch):
    policy = _policy(tmp_repo, [_entry("existing", "existing-revision")])
    share_policy.save_policy(tmp_repo, policy)
    monkeypatch.setattr(store, "run_git", lambda *_args: "https://github.com/org/repo.git")

    snapshot = share_policy.policy_status(tmp_repo)
    rendered = share_policy.format_policy_status(snapshot)

    assert snapshot["baseline"] == 1
    assert "policy active" in rendered
    assert "future-only" in rendered
    assert "shared" not in rendered.lower()


def test_retry_attention_reopens_receipt_then_queues_stable_idempotency_key(
        tmp_repo, monkeypatch):
    share_policy.save_policy(tmp_repo, _policy(tmp_repo, include_existing=True))
    attention = _attention(tmp_repo)
    share_policy.append_attention(attention)
    share_policy.append_receipt(_receipt(state="attention"))
    starts = []
    monkeypatch.setattr(
        share_policy, "start_detached_drainer", lambda *_args: starts.append(True) or True)

    outcome = share_policy.retry_attention(tmp_repo, "intent-1")

    assert outcome == share_policy.OperationOutcome("queued", "none")
    assert share_policy.read_attention() == []
    assert share_policy.read_outbox()[0]["idempotency_key"] == "intent-1"
    assert share_policy.fold_receipts(share_policy.read_receipts())[
        share_policy.receipt_key(attention)]["state"] == "queued"
    assert starts == [True]


def test_retry_attention_preserves_durable_intent_when_detached_launch_fails(
        tmp_repo, monkeypatch):
    share_policy.save_policy(tmp_repo, _policy(tmp_repo, include_existing=True))
    attention = _attention(tmp_repo)
    share_policy.append_attention(attention)
    share_policy.append_receipt(_receipt(state="attention"))
    monkeypatch.setattr(share_policy, "start_detached_drainer", lambda: False)

    outcome = share_policy.retry_attention(tmp_repo, "intent-1")

    assert outcome.result == "queued"
    assert outcome.reason_code == "validation_error"
    assert outcome.diagnostic_id is not None
    assert share_policy.read_attention() == []
    assert share_policy.read_outbox()[0]["idempotency_key"] == "intent-1"


def test_retry_attention_keeps_item_when_queue_lock_is_busy(tmp_repo, monkeypatch):
    share_policy.save_policy(tmp_repo, _policy(tmp_repo, include_existing=True))
    share_policy.append_attention(_attention(tmp_repo))
    monkeypatch.setattr(
        share_policy, "enqueue_intent",
        lambda *_args, **_kwargs: share_policy.OperationOutcome("no_op", "lock_busy"),
    )

    outcome = share_policy.retry_attention(tmp_repo, "intent-1")

    assert outcome == share_policy.OperationOutcome("no_op", "lock_busy")
    assert len(share_policy.read_attention()) == 1
    assert share_policy.fold_receipts(share_policy.read_receipts())[
        share_policy.receipt_key(_attention(tmp_repo))]["state"] == "attention"


@pytest.mark.parametrize("failure_kind", ["full", "corrupt"])
def test_retry_attention_failure_restores_terminal_receipt(
        tmp_repo, monkeypatch, failure_kind):
    share_policy.save_policy(tmp_repo, _policy(tmp_repo, include_existing=True))
    attention = _attention(tmp_repo)
    share_policy.append_attention(attention)
    share_policy.append_receipt(_receipt(state="attention"))
    if failure_kind == "full":
        monkeypatch.setattr(share_policy, "OUTBOX_CAP", 0)
    else:
        path = share_policy.proposal_outbox_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("not-json", encoding="utf-8")

    outcome = share_policy.retry_attention(tmp_repo, "intent-1")

    expected = "validation_error" if failure_kind == "full" else "corrupt_queue"
    assert outcome.reason_code == expected
    assert len(share_policy.read_attention()) == 1
    assert share_policy.fold_receipts(share_policy.read_receipts())[
        share_policy.receipt_key(attention)]["state"] == "attention"


def test_policy_control_telemetry_never_contains_destination_or_user_input(
        tmp_repo, monkeypatch):
    calls = []
    monkeypatch.setattr(
        share_policy.decision_observability,
        "emit_decision_operation",
        lambda operation, **fields: calls.append((operation, fields)),
    )
    sentinel = "SENTINEL_PRIVATE_TEAM_NAME"
    (preview, outcome), _remote_store = _prepare_activation(
        tmp_repo, monkeypatch,
        teams=[remote.RemoteTeam("team-1", sentinel, "member")],
    )

    assert preview is None and outcome.result == "refused"
    encoded = json.dumps(calls)
    assert sentinel not in encoded
    assert FINGERPRINT not in encoded
    assert "https://mcp.contexer.ai/mcp" not in encoded
    assert calls[0][0] == "policyChange"


def test_atomic_sidecar_writes_are_owner_only(tmp_repo):
    share_policy.save_policy(tmp_repo, _policy(tmp_repo))
    share_policy.enqueue_intent(_intent(tmp_repo))
    share_policy.append_receipt(_receipt())
    share_policy.append_attention(_attention(tmp_repo))

    for path in (share_policy.policy_path(tmp_repo), share_policy.proposal_outbox_path(),
                 share_policy.proposal_receipts_path(), share_policy.proposal_attention_path()):
        assert path.stat().st_mode & 0o777 == 0o600


def test_current_revision_helper_remains_the_single_revision_resolver(tmp_repo, monkeypatch):
    seen = []
    real = revisions.current_revision
    monkeypatch.setattr(revisions, "current_revision", lambda entry: seen.append(entry) or real(entry))
    share_policy.activation_baseline([_entry()], include_existing=False)
    assert len(seen) == 1


def _activate_scanner(tmp_repo, monkeypatch, entries):
    share_policy.save_policy(tmp_repo, _policy(tmp_repo, include_existing=True))
    monkeypatch.setattr(store, "run_git", lambda *_args: "git@github.com:org/repo.git")
    monkeypatch.setattr(store, "load", lambda _repo: {"entries": entries})


def test_scanner_queues_human_approved_revision_and_receipt_without_prose(
        tmp_repo, monkeypatch):
    entry = _entry(decision_id="12345678-decision", revision_id="revision-human")
    entry["content"] = "SENTINEL_DECISION_PROSE"
    entry["evidence"] = [{"content": "SENTINEL_EVIDENCE"}]
    _activate_scanner(tmp_repo, monkeypatch, [entry])

    outcome = share_policy.enqueue_after_local_mutation(tmp_repo, "12345678")

    assert outcome == share_policy.ScanOutcome("queued", "none", 1, 1, 0)
    intents = share_policy.read_outbox()
    assert len(intents) == 1
    assert intents[0]["decision_id"] == entry["id"]
    receipts = share_policy.read_receipts()
    assert len(receipts) == 1
    assert receipts[0]["state"] == "queued"
    encoded = json.dumps({"intents": intents, "receipts": receipts})
    assert "SENTINEL_DECISION_PROSE" not in encoded
    assert "SENTINEL_EVIDENCE" not in encoded


def test_scanner_starts_detached_only_after_intent_and_receipt_are_durable(
        tmp_repo, monkeypatch):
    entry = _entry(revision_id="revision-human")
    _activate_scanner(tmp_repo, monkeypatch, [entry])
    observed = []

    def start(_profile=None):
        observed.append((share_policy.read_outbox(), share_policy.read_receipts()))
        return True

    monkeypatch.setattr(share_policy, "start_detached_drainer", start)

    outcome = share_policy.scan_and_enqueue(tmp_repo)

    assert outcome.result == "queued"
    assert len(observed) == 1
    assert len(observed[0][0]) == 1
    assert observed[0][1][-1]["state"] == "queued"


def test_scanner_can_leave_durable_intent_for_external_hook_worker(
        tmp_repo, monkeypatch):
    entry = _entry(revision_id="revision-human")
    _activate_scanner(tmp_repo, monkeypatch, [entry])
    monkeypatch.setattr(
        share_policy, "start_detached_drainer",
        lambda *_args: pytest.fail("hook scan must not start an in-process daemon thread"),
    )

    outcome = share_policy.scan_and_enqueue(tmp_repo, start_worker=False)

    assert outcome.result == "queued"
    assert len(share_policy.read_outbox()) == 1
    assert share_policy.read_receipts()[-1]["state"] == "queued"


def test_scanner_is_idempotent_and_heals_missing_queued_receipt(tmp_repo, monkeypatch):
    entry = _entry(revision_id="revision-human")
    _activate_scanner(tmp_repo, monkeypatch, [entry])
    intent = share_policy.build_intent(
        _policy(tmp_repo, include_existing=True), tmp_repo, entry,
        now=NOW, idempotency_key="stable-intent",
    )
    assert share_policy.enqueue_intent(intent).result == "queued"

    healed = share_policy.scan_and_enqueue(tmp_repo)
    repeated = share_policy.scan_and_enqueue(tmp_repo)

    assert healed == share_policy.ScanOutcome("success", "none", 1, 0, 1)
    assert repeated == share_policy.ScanOutcome("no_op", "duplicate_receipt", 1, 0, 1)
    assert share_policy.read_outbox() == [intent]
    assert len(share_policy.read_receipts()) == 1


def test_scanner_rebuilds_outbox_when_only_nonterminal_queued_receipt_remains(
        tmp_repo, monkeypatch):
    entry = _entry(revision_id="revision-human")
    policy = _policy(tmp_repo, include_existing=True)
    _activate_scanner(tmp_repo, monkeypatch, [entry])
    orphaned_receipt = share_policy.queued_receipt(
        share_policy.build_intent(policy, tmp_repo, entry, now=NOW))
    share_policy.append_receipt(orphaned_receipt)

    rebuilt = share_policy.scan_and_enqueue(tmp_repo)
    repeated = share_policy.scan_and_enqueue(tmp_repo)

    assert rebuilt == share_policy.ScanOutcome("queued", "none", 1, 1, 0)
    assert repeated == share_policy.ScanOutcome("no_op", "duplicate_receipt", 1, 0, 1)
    assert len(share_policy.read_outbox()) == 1


def test_new_policy_generation_is_not_suppressed_by_old_queued_receipt(
        tmp_repo, monkeypatch):
    entry = _entry(revision_id="revision-human")
    old_policy = _policy(tmp_repo, include_existing=True)
    _activate_scanner(tmp_repo, monkeypatch, [entry])
    old_intent = share_policy.build_intent(
        old_policy, tmp_repo, entry, now=NOW, idempotency_key="old-intent")
    assert share_policy.enqueue_intent(old_intent).result == "queued"
    share_policy.append_receipt(share_policy.queued_receipt(old_intent))
    new_policy = {**old_policy, "policy_generation": "policy-2"}
    share_policy.save_policy(tmp_repo, new_policy)

    outcome = share_policy.scan_and_enqueue(tmp_repo)

    assert outcome == share_policy.ScanOutcome("queued", "none", 1, 1, 0)
    assert [row["policy_generation"] for row in share_policy.read_outbox()] == [
        "policy-1", "policy-2",
    ]
    folded = share_policy.fold_receipts(share_policy.read_receipts())
    assert next(iter(folded.values()))["policy_generation"] == "policy-2"


def test_scanner_disabled_policy_is_local_no_op_before_git_or_store(tmp_repo, monkeypatch):
    monkeypatch.setattr(store, "run_git", lambda *_args: pytest.fail("git must not run"))
    monkeypatch.setattr(store, "load", lambda _repo: pytest.fail("store must not load"))

    assert share_policy.scan_and_enqueue(tmp_repo) == \
        share_policy.ScanOutcome("skipped", "policy_disabled")
    assert not share_policy.proposal_outbox_path().exists()


def test_scanner_excludes_global_baseline_and_nonhuman_revisions(tmp_repo, monkeypatch):
    baseline = _entry("baseline", "baseline-revision")
    nonhuman = _entry("nonhuman", "nonhuman-revision", source="scan", approved_by=None)
    policy = _policy(tmp_repo, [baseline])
    share_policy.save_policy(tmp_repo, policy)
    monkeypatch.setattr(store, "run_git", lambda *_args: "https://github.com/org/repo.git")
    monkeypatch.setattr(store, "load", lambda _repo: {"entries": [baseline, nonhuman]})

    outcome = share_policy.scan_and_enqueue(tmp_repo)
    global_outcome = share_policy.scan_and_enqueue(tmp_repo, is_global=True)

    assert outcome == share_policy.ScanOutcome("success", "none", 2, 0, 2)
    assert global_outcome == share_policy.ScanOutcome("skipped", "global_decision")
    assert not share_policy.proposal_outbox_path().exists()


def test_scanner_uses_strict_eligibility_not_manual_shareability(tmp_repo, monkeypatch):
    entry = _entry(source="memory", approved_by=None)
    _activate_scanner(tmp_repo, monkeypatch, [entry])
    monkeypatch.setattr(
        store, "get_shareable_all", lambda *_args: pytest.fail("manual list must not be read"))

    outcome = share_policy.scan_and_enqueue(tmp_repo)

    assert outcome == share_policy.ScanOutcome(
        "skipped", "ineligible_revision", 1, 0, 1)


def test_scanner_is_bounded_by_store_decision_cap(tmp_repo, monkeypatch):
    entries = [
        _entry(f"decision-{index}", f"revision-{index}", source="scan", approved_by=None)
        for index in range(store.MAX_ENTRIES + 1)
    ]
    _activate_scanner(tmp_repo, monkeypatch, entries)

    outcome = share_policy.scan_and_enqueue(tmp_repo)

    assert outcome.scanned == store.MAX_ENTRIES
    assert outcome.skipped == store.MAX_ENTRIES
    assert outcome.queued == 0


def test_scanner_fails_soft_on_repo_mismatch_corruption_and_busy_locks(tmp_repo, monkeypatch):
    entry = _entry(revision_id="revision-human")
    _activate_scanner(tmp_repo, monkeypatch, [entry])
    monkeypatch.setattr(store, "run_git", lambda *_args: "https://github.com/org/other.git")
    assert share_policy.scan_and_enqueue(tmp_repo).reason_code == "validation_error"

    monkeypatch.setattr(store, "run_git", lambda *_args: "https://github.com/org/repo.git")
    receipts = share_policy.proposal_receipts_path()
    receipts.parent.mkdir(parents=True, exist_ok=True)
    receipts.write_text("not-json", encoding="utf-8")
    assert share_policy.scan_and_enqueue(tmp_repo).reason_code == "corrupt_queue"

    receipts.unlink()
    with share_policy._sidecar_lock(share_policy.proposal_outbox_lock_path()):
        busy = share_policy.scan_and_enqueue(tmp_repo)
    assert busy == share_policy.ScanOutcome("no_op", "lock_busy", 1, 0, 1)


def test_scanner_never_acquires_decision_store_lock(tmp_repo, monkeypatch):
    entry = _entry(revision_id="revision-human")
    _activate_scanner(tmp_repo, monkeypatch, [entry])
    monkeypatch.setattr(
        store, "store_lock", lambda *_args, **_kwargs: pytest.fail("store lock must not run"))

    assert share_policy.scan_and_enqueue(tmp_repo).result == "queued"


class _ProposalRemote:
    def __init__(self, *, automatic=True, fingerprint=FINGERPRINT,
                 atomic=True, statuses=("submitted",), member=True):
        self.capabilities = remote.ServerCapabilities(
            decision_reconciliation=remote.DecisionReconciliationCapabilities(
                version=1, atomic_submit=atomic, preview=atomic, three_way_merge=True),
            automatic_decision_proposal=(
                remote.AutomaticDecisionProposalCapabilities(version=1)
                if automatic else None
            ),
            account_fingerprint=fingerprint,
        )
        self.statuses = list(statuses)
        self.teams = [remote.RemoteTeam("team-1", "Platform", "member")] if member else []
        self.capability_calls = 0
        self.list_calls = 0
        self.preview_calls = []
        self.submit_calls = []

    def get_capabilities(self):
        self.capability_calls += 1
        return self.capabilities

    def list_teams(self):
        self.list_calls += 1
        return self.teams

    def preview_decision_reconciliation(self, decision_id, team_id, **decision):
        self.preview_calls.append((decision_id, team_id, decision))
        attempt = len(self.preview_calls)
        return remote.DecisionReconciliationPreview(
            personal_head=f"personal-{attempt}",
            team_head=f"team-{attempt}",
            pending_candidate_id=None,
            state="ready",
            operation="submit",
            fields=[],
            available_actions=["submit"],
            team=remote.RemoteTeam(team_id, "Platform", "member"),
        )

    def submit_team_decision(self, decision_id, revision_id, team_id, **kwargs):
        self.submit_calls.append((decision_id, revision_id, team_id, kwargs))
        status = self.statuses.pop(0)
        return remote.TeamSubmissionResult(
            status=status,
            kind="update",
            personal_head="personal-result",
            team_head="team-result",
            candidate_id=("candidate-1" if status in {"submitted", "already_pending"} else None),
            revision_id=revision_id,
            replayed=status == "already_pending",
            team=remote.RemoteTeam(team_id, "Platform", "member"),
        )


def _ready_drain(tmp_repo, monkeypatch, remote_store):
    policy = _policy(tmp_repo, include_existing=True)
    share_policy.save_policy(tmp_repo, policy)
    intent = _intent(tmp_repo)
    assert share_policy.enqueue_intent(intent).result == "queued"
    share_policy.append_receipt(share_policy.queued_receipt(intent))
    secret = "AKIAIOSFODNN7EXAMPLE"
    decision = {
        "id": intent["decision_id"],
        "revision_id": intent["revision_id"],
        "type": "decision",
        "content": f"Never expose {secret}",
        "confidence": 100,
        "evidence": [f"Observed {secret}"],
        "source": "human",
        "title": f"Protect {secret}",
        "source_files": ["config.py"],
        "status": "approved",
    }
    monkeypatch.setattr(
        share_policy, "_fresh_local_projection",
        lambda *_args, **_kwargs: (decision, "none"),
    )
    monkeypatch.setattr(
        share_policy.remote.RemoteStore,
        "from_profile",
        staticmethod(lambda _profile, **kwargs: (
            remote_store if kwargs == {"reactive_refresh": False}
            else pytest.fail("automatic drainer must pin the validated credential")
        )),
    )
    profile = Profile(
        mode="team", endpoint=policy["endpoint"], token="test-token", redact_secrets=True)
    return intent, policy, profile, secret


def test_drainer_uses_fresh_preview_redacts_and_receipts_before_removal(
        tmp_repo, monkeypatch):
    remote_store = _ProposalRemote()
    intent, _policy_row, profile, secret = _ready_drain(
        tmp_repo, monkeypatch, remote_store)
    events = []
    real_append = share_policy.append_receipt
    real_remove = share_policy.remove_intents
    monkeypatch.setattr(
        share_policy, "append_receipt",
        lambda receipt, **kwargs: events.append(("receipt", receipt["state"]))
        or real_append(receipt, **kwargs),
    )
    monkeypatch.setattr(
        share_policy, "remove_intents",
        lambda rows: events.append(("remove", rows[0]["idempotency_key"]))
        or real_remove(rows),
    )

    outcomes = share_policy.drain_once(profile)

    assert outcomes == [share_policy.OperationOutcome("submitted", "none")]
    assert events[-2:] == [("receipt", "submitted"), ("remove", intent["idempotency_key"])]
    assert share_policy.read_outbox() == []
    assert share_policy.read_receipts()[-1]["state"] == "submitted"
    assert len(remote_store.preview_calls) == len(remote_store.submit_calls) == 1
    encoded = json.dumps({
        "preview": remote_store.preview_calls,
        "submit": remote_store.submit_calls,
    })
    assert secret not in encoded
    assert "[REDACTED:aws_key]" in encoded


def test_drainer_telemetry_never_receives_destination_or_decision_prose(
        tmp_repo, monkeypatch):
    remote_store = _ProposalRemote()
    intent, policy, profile, secret = _ready_drain(
        tmp_repo, monkeypatch, remote_store)
    calls = []
    monkeypatch.setattr(
        share_policy.decision_observability,
        "emit_decision_operation",
        lambda operation, **fields: calls.append((operation, fields)),
    )

    assert share_policy.drain_once(profile)[0].result == "submitted"

    drain_calls = [fields for operation, fields in calls if operation == "drain"]
    assert len(drain_calls) == 1
    encoded = json.dumps(drain_calls)
    for sensitive in (
        secret, policy["endpoint"], policy["account_fingerprint"], policy["repo_key"],
        intent["repo_path"], "Platform",
    ):
        assert sensitive not in encoded


def test_empty_drainer_emits_terminal_noop(tmp_repo, monkeypatch):
    calls = []
    monkeypatch.setattr(
        share_policy.decision_observability,
        "emit_decision_operation",
        lambda operation, **fields: calls.append((operation, fields)),
    )

    assert share_policy.drain_once() == []

    assert calls == [("drain", {
        "result": "no_op",
        "reason_code": "none",
        "error_class": "none",
        "started_ns": calls[0][1]["started_ns"],
        "diagnostic_id": None,
        "team_id": None,
        "decision_id": None,
        "policy_generation": None,
        "idempotency_key": None,
        "candidate_id": None,
        "attempt": None,
        "queue_depth": 0,
        "replayed": None,
    })]


def test_drainer_recovers_receipt_before_removal_crash_without_resubmitting(
        tmp_repo, monkeypatch):
    remote_store = _ProposalRemote()
    intent, _policy_row, profile, _secret = _ready_drain(
        tmp_repo, monkeypatch, remote_store)
    share_policy.append_receipt(share_policy._receipt_for_intent(
        intent, "submitted", candidate_id="candidate-crash"))

    outcomes = share_policy.drain_once(profile)

    assert outcomes == [share_policy.OperationOutcome("no_op", "duplicate_receipt")]
    assert share_policy.read_outbox() == []
    assert not remote_store.preview_calls and not remote_store.submit_calls


def test_drainer_refreshes_preview_once_after_head_conflict(tmp_repo, monkeypatch):
    remote_store = _ProposalRemote(statuses=("heads_changed", "already_pending"))
    _intent_row, _policy_row, profile, _secret = _ready_drain(
        tmp_repo, monkeypatch, remote_store)

    outcomes = share_policy.drain_once(profile)

    assert outcomes == [share_policy.OperationOutcome("already_pending", "none")]
    assert len(remote_store.preview_calls) == len(remote_store.submit_calls) == 2
    assert remote_store.submit_calls[0][3]["expected_personal_head"] == "personal-1"
    assert remote_store.submit_calls[1][3]["expected_personal_head"] == "personal-2"
    assert remote_store.submit_calls[0][3]["idempotency_key"] == \
        remote_store.submit_calls[1][3]["idempotency_key"]


@pytest.mark.parametrize("automatic,atomic", [(False, True), (True, False)])
def test_drainer_parks_legacy_capabilities_once_and_pauses_policy(
        tmp_repo, monkeypatch, automatic, atomic):
    remote_store = _ProposalRemote(automatic=automatic, atomic=atomic)
    _intent_row, _policy_row, profile, _secret = _ready_drain(
        tmp_repo, monkeypatch, remote_store)

    outcomes = share_policy.drain_once(profile)

    assert outcomes[0].result == "attention"
    assert outcomes[0].reason_code == "unsupported_protocol"
    assert not remote_store.preview_calls and not remote_store.submit_calls
    assert share_policy.read_outbox() == []
    assert share_policy.read_attention()[0]["reason"] == "unsupported_protocol"
    assert share_policy.read_receipts()[-1]["state"] == "attention"
    assert share_policy.load_policy(tmp_repo)["paused_reason"] == "unsupported_protocol"
    assert remote_store.capability_calls == 1

    assert share_policy.drain_once(profile) == []
    assert remote_store.capability_calls == 1


def test_submission_unsupported_protocol_parks_intent_and_pauses_policy(
        tmp_repo, monkeypatch):
    remote_store = _ProposalRemote(statuses=("unsupported_protocol",))
    _intent_row, _policy_row, profile, _secret = _ready_drain(
        tmp_repo, monkeypatch, remote_store)

    outcomes = share_policy.drain_once(profile)

    assert outcomes[0].result == "attention"
    assert outcomes[0].reason_code == "unsupported_protocol"
    assert len(remote_store.preview_calls) == len(remote_store.submit_calls) == 1
    assert share_policy.read_outbox() == []
    assert share_policy.read_attention()[0]["reason"] == "unsupported_protocol"
    assert share_policy.load_policy(tmp_repo)["paused_reason"] == "unsupported_protocol"


def test_same_destination_reenable_then_retry_recovers_unsupported_attention(
        tmp_repo, monkeypatch):
    unsupported = _ProposalRemote(automatic=False)
    intent, paused_policy, profile, _secret = _ready_drain(
        tmp_repo, monkeypatch, unsupported)
    assert share_policy.drain_once(profile)[0].reason_code == "unsupported_protocol"

    recovered = _ProposalRemote()
    monkeypatch.setattr(
        share_policy.remote.RemoteStore,
        "from_profile",
        staticmethod(lambda _profile, **kwargs: (
            recovered if kwargs == {"reactive_refresh": False}
            else pytest.fail("automatic flows must pin the validated credential")
        )),
    )
    monkeypatch.setattr(store, "run_git", lambda *_args: "git@github.com:org/repo.git")
    preview, outcome = share_policy.prepare_policy_activation(
        tmp_repo, "Platform", profile=profile)

    assert outcome == share_policy.OperationOutcome("success", "none")
    assert preview is not None
    assert preview.policy["policy_generation"] == paused_policy["policy_generation"]
    monkeypatch.setattr(share_policy, "start_detached_drainer", lambda: True)
    share_policy.activate_policy(preview)
    assert share_policy.retry_attention(tmp_repo, intent["idempotency_key"]).result == "queued"

    assert share_policy.drain_once(profile) == [
        share_policy.OperationOutcome("submitted", "none")]
    assert share_policy.read_outbox() == []
    assert share_policy.read_attention() == []
    assert len(recovered.submit_calls) == 1


def test_drainer_account_mismatch_moves_attention_and_pauses(tmp_repo, monkeypatch):
    remote_store = _ProposalRemote(fingerprint="acctfp_v1_9Z8Y7X6W5V4U")
    _intent_row, _policy_row, profile, _secret = _ready_drain(
        tmp_repo, monkeypatch, remote_store)

    outcomes = share_policy.drain_once(profile)

    assert outcomes[0].result == "attention"
    assert outcomes[0].reason_code == "account_mismatch"
    assert not remote_store.preview_calls and not remote_store.submit_calls
    assert share_policy.read_outbox() == []
    assert share_policy.read_attention()[0]["reason"] == "account_mismatch"
    assert share_policy.read_receipts()[-1]["state"] == "attention"
    assert share_policy.load_policy(tmp_repo)["paused_reason"] == "account_mismatch"


def test_drainer_refreshes_membership_and_refuses_removed_target(tmp_repo, monkeypatch):
    remote_store = _ProposalRemote(member=False)
    _intent_row, _policy_row, profile, _secret = _ready_drain(
        tmp_repo, monkeypatch, remote_store)

    outcomes = share_policy.drain_once(profile)

    assert remote_store.list_calls == 1
    assert outcomes[0].result == "attention"
    assert outcomes[0].reason_code == "not_member"
    assert not remote_store.preview_calls and not remote_store.submit_calls
    assert share_policy.load_policy(tmp_repo)["paused_reason"] == "not_member"


def test_drainer_transient_failure_keeps_stable_intent_and_closed_error(
        tmp_repo, monkeypatch):
    remote_store = _ProposalRemote()
    intent, _policy_row, profile, _secret = _ready_drain(
        tmp_repo, monkeypatch, remote_store)
    monkeypatch.setattr(
        remote_store,
        "get_capabilities",
        lambda: (_ for _ in ()).throw(remote.RemoteUnavailableError("SENTINEL_SECRET_ERROR")),
    )

    outcomes = share_policy.drain_once(profile)

    assert outcomes[0].result == "retry"
    assert outcomes[0].reason_code == "transport_error"
    queued = share_policy.read_outbox()
    assert queued[0]["idempotency_key"] == intent["idempotency_key"]
    assert queued[0]["attempts"] == 1
    assert queued[0]["last_error_code"] == "transport_error"
    assert queued[0]["last_error_class"] == "transport"
    assert "SENTINEL" not in json.dumps(queued)


def test_offline_intent_reconnects_and_submits_exactly_once(tmp_repo, monkeypatch):
    offline = _ProposalRemote()
    intent, _policy_row, profile, _secret = _ready_drain(tmp_repo, monkeypatch, offline)
    monkeypatch.setattr(
        offline,
        "get_capabilities",
        lambda: (_ for _ in ()).throw(remote.RemoteUnavailableError("offline")),
    )

    first = share_policy.drain_once(profile)

    assert len(first) == 1
    assert first[0].result == "retry"
    assert first[0].reason_code == "transport_error"
    assert first[0].diagnostic_id is not None
    assert share_policy.read_outbox()[0]["idempotency_key"] == intent["idempotency_key"]

    recovered = _ProposalRemote()
    monkeypatch.setattr(
        share_policy.remote.RemoteStore,
        "from_profile",
        staticmethod(lambda _profile, **kwargs: (
            recovered if kwargs == {"reactive_refresh": False}
            else pytest.fail("automatic drainer must pin the validated credential")
        )),
    )

    second = share_policy.drain_once(profile)
    third = share_policy.drain_once(profile)

    assert second == [share_policy.OperationOutcome("submitted", "none")]
    assert third == []
    assert share_policy.read_outbox() == []
    assert len(recovered.submit_calls) == 1
    assert recovered.submit_calls[0][3]["idempotency_key"] == intent["idempotency_key"]


def test_transport_401_after_fingerprint_check_never_submits_as_refreshed_account(
        tmp_repo, monkeypatch):
    remote_store = _ProposalRemote()
    intent, _policy_row, profile, _secret = _ready_drain(
        tmp_repo, monkeypatch, remote_store)
    expired = remote.RemoteAuthError("transport 401")
    expired._transport_auth = True
    monkeypatch.setattr(
        remote_store, "list_teams", lambda: (_ for _ in ()).throw(expired))

    outcomes = share_policy.drain_once(profile)

    assert outcomes[0].result == "retry"
    assert outcomes[0].reason_code == "transient_error"
    assert not remote_store.preview_calls and not remote_store.submit_calls
    queued = share_policy.read_outbox()
    assert queued[0]["idempotency_key"] == intent["idempotency_key"]
    assert queued[0]["attempts"] == 1


def test_drainer_marks_superseded_revision_stale_before_remote_creation(
        tmp_repo, monkeypatch):
    remote_store = _ProposalRemote()
    _intent_row, _policy_row, profile, _secret = _ready_drain(
        tmp_repo, monkeypatch, remote_store)
    monkeypatch.setattr(
        share_policy, "_fresh_local_projection", lambda *_args, **_kwargs: (None, "stale_intent"))
    monkeypatch.setattr(
        share_policy.remote.RemoteStore,
        "from_profile",
        staticmethod(lambda _profile: pytest.fail("remote must not be created")),
    )

    outcomes = share_policy.drain_once(profile)

    assert outcomes[0].result == "attention"
    assert outcomes[0].reason_code == "stale_intent"
    assert share_policy.read_outbox() == []


def test_drainer_rechecks_revision_after_preview_before_submit(tmp_repo, monkeypatch):
    remote_store = _ProposalRemote()
    _intent_row, _policy_row, profile, _secret = _ready_drain(
        tmp_repo, monkeypatch, remote_store)
    stable_projection = share_policy._fresh_local_projection
    reads = 0

    def superseded_during_preview(*args, **kwargs):
        nonlocal reads
        reads += 1
        if reads == 1:
            return stable_projection(*args, **kwargs)
        return None, "stale_intent"

    monkeypatch.setattr(share_policy, "_fresh_local_projection", superseded_during_preview)

    outcomes = share_policy.drain_once(profile)

    assert outcomes[0].result == "attention"
    assert outcomes[0].reason_code == "stale_intent"
    assert len(remote_store.preview_calls) == 1
    assert not remote_store.submit_calls


def test_drainer_rechecks_policy_after_preview_before_submit(tmp_repo, monkeypatch):
    remote_store = _ProposalRemote()
    intent, policy, profile, _secret = _ready_drain(
        tmp_repo, monkeypatch, remote_store)
    original_preview = remote_store.preview_decision_reconciliation

    def rebind_during_preview(*args, **kwargs):
        preview = original_preview(*args, **kwargs)
        share_policy.save_policy(intent["repo_path"], {
            **policy,
            "policy_generation": "policy-rebound",
            "team_id": "team-rebound",
        })
        return preview

    monkeypatch.setattr(remote_store, "preview_decision_reconciliation", rebind_during_preview)

    outcomes = share_policy.drain_once(profile)

    assert outcomes[0].result == "attention"
    assert outcomes[0].reason_code == "policy_mismatch"
    assert not remote_store.submit_calls
    assert share_policy.load_policy(tmp_repo)["policy_generation"] == "policy-rebound"


def test_expired_worker_abstains_after_submit_lease_takeover(tmp_repo, monkeypatch):
    remote_store = _ProposalRemote()
    _intent_row, _policy_row, profile, _secret = _ready_drain(
        tmp_repo, monkeypatch, remote_store)
    original_submit = remote_store.submit_team_decision

    def take_over_during_submit(*args, **kwargs):
        result = original_submit(*args, **kwargs)
        path = share_policy.proposal_drainer_lock_path()
        with share_policy._sidecar_lock(path):
            share_policy._write_drainer_claim(path, {
                "owner": str(uuid.uuid4()),
                "renewed_at": time.time(),
            })
        return result

    monkeypatch.setattr(remote_store, "submit_team_decision", take_over_during_submit)

    outcomes = share_policy.drain_once(profile)

    assert outcomes == [share_policy.OperationOutcome("no_op", "lock_busy")]
    assert len(share_policy.read_outbox()) == 1
    assert [row["state"] for row in share_policy.read_receipts()] == ["queued"]
    assert share_policy.read_attention() == []


def test_drainer_network_runs_outside_every_sidecar_lock(tmp_repo, monkeypatch):
    remote_store = _ProposalRemote()
    _intent_row, _policy_row, profile, _secret = _ready_drain(
        tmp_repo, monkeypatch, remote_store)
    real_lock = share_policy._sidecar_lock
    depth = 0

    @contextlib.contextmanager
    def tracked_lock(*args, **kwargs):
        nonlocal depth
        with real_lock(*args, **kwargs):
            depth += 1
            try:
                yield
            finally:
                depth -= 1

    monkeypatch.setattr(share_policy, "_sidecar_lock", tracked_lock)
    for name in (
        "get_capabilities", "list_teams", "preview_decision_reconciliation",
        "submit_team_decision",
    ):
        original = getattr(remote_store, name)

        def checked(*args, _original=original, **kwargs):
            assert depth == 0
            return _original(*args, **kwargs)

        monkeypatch.setattr(remote_store, name, checked)

    assert share_policy.drain_once(profile)[0].result == "submitted"


def test_drainer_lease_refuses_second_worker_without_network(tmp_repo, monkeypatch):
    remote_store = _ProposalRemote()
    _intent_row, _policy_row, profile, _secret = _ready_drain(
        tmp_repo, monkeypatch, remote_store)
    with share_policy.proposal_drainer_lock(blocking=False):
        outcomes = share_policy.drain_once(profile)
    assert outcomes == [share_policy.OperationOutcome("no_op", "lock_busy")]
    assert not remote_store.preview_calls and not remote_store.submit_calls


def test_two_concurrent_drainers_create_one_candidate(tmp_repo, monkeypatch):
    remote_store = _ProposalRemote()
    _intent_row, _policy_row, profile, _secret = _ready_drain(
        tmp_repo, monkeypatch, remote_store)
    entered_network = threading.Event()
    release_network = threading.Event()
    original_capabilities = remote_store.get_capabilities

    def blocked_capabilities():
        entered_network.set()
        assert release_network.wait(timeout=2)
        return original_capabilities()

    monkeypatch.setattr(remote_store, "get_capabilities", blocked_capabilities)
    outcomes = {}

    def run(name):
        outcomes[name] = share_policy.drain_once(profile)

    first = threading.Thread(target=run, args=("first",))
    second = threading.Thread(target=run, args=("second",))
    first.start()
    try:
        assert entered_network.wait(timeout=1)
        second.start()
        second.join(timeout=1)
        assert not second.is_alive()
    finally:
        release_network.set()
        first.join(timeout=2)
        if second.ident is not None:
            second.join(timeout=2)
    assert not first.is_alive()

    assert sorted(outcomes.values(), key=lambda rows: rows[0].result) == [
        [share_policy.OperationOutcome("no_op", "lock_busy")],
        [share_policy.OperationOutcome("submitted", "none")],
    ]
    assert len(remote_store.submit_calls) == 1
    assert share_policy.read_outbox() == []


def test_malformed_drainer_claim_expires_by_file_age(tmp_repo, monkeypatch):
    monkeypatch.setattr(share_policy, "_DRAINER_LEASE_SECONDS", 1)
    path = share_policy.proposal_drainer_lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"owner":"partial"}', encoding="utf-8")
    stale = time.time() - 2
    os.utime(path, (stale, stale))

    with share_policy.proposal_drainer_lock(blocking=False) as owner:
        assert str(uuid.UUID(owner)) == owner


def test_detached_start_returns_before_worker_finishes(tmp_repo, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    finished = threading.Event()

    def blocked_drain(_profile=None):
        entered.set()
        release.wait(timeout=2)
        finished.set()
        return []

    monkeypatch.setattr(share_policy, "drain_once", blocked_drain)
    assert _REAL_START_IN_PROCESS_DRAINER() is True
    assert entered.wait(timeout=1)
    assert _REAL_START_IN_PROCESS_DRAINER() is False
    release.set()
    assert finished.wait(timeout=1)
