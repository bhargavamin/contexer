"""Schema, security, and concurrency contracts for remembered proposal sidecars."""
import json
import re
import threading

import pytest

from contexer import revisions, share_policy, store


NOW = "2026-08-30T12:00:00Z"
FINGERPRINT = "acctfp_v1_7M4Q2PX9C6N8"


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
    receipts = share_policy.fold_receipts([_receipt()])
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
