"""Closed-vocabulary, fail-soft diagnostics for decision proposal operations."""
import json
import re

from contexer import decision_observability


def test_capability_read_emits_correlated_span_and_terminal_log(monkeypatch):
    batches = []
    monkeypatch.setattr(
        decision_observability,
        "_enqueue_records",
        lambda records: batches.append(list(records)),
    )

    decision_observability.emit_decision_operation(
        "capabilityRead",
        result="failure",
        reason_code="transport_error",
        error_class="transport",
        started_ns=0,
    )

    assert len(batches) == 1
    span, log = batches[0]
    assert span["name"] == "decision_proposal.capability_read"
    assert span["durationMs"] >= 0
    assert span["events"] == []
    assert span["attributes"] == {
        "contexer.result": "failure",
        "contexer.reason_code": "transport_error",
        "contexer.error_class": "transport",
        "contexer.diagnostic_id": span["attributes"]["contexer.diagnostic_id"],
    }
    assert log["message"] == "decision operation completed"
    assert log["fields"] == {
        "action": "decisionProposalCapabilityRead",
        "result": "failure",
        "reasonCode": "transport_error",
        "errorClass": "transport",
        "diagnosticId": span["attributes"]["contexer.diagnostic_id"],
    }
    assert log["traceId"] == span["traceId"]
    assert log["spanId"] == span["spanId"]
    assert re.fullmatch(r"diag_[A-Z0-9]{16}", log["fields"]["diagnosticId"])


def test_invalid_diagnostic_vocabulary_is_not_copied(monkeypatch):
    records = []
    monkeypatch.setattr(
        decision_observability,
        "_enqueue_records",
        lambda emitted: records.extend(emitted),
    )

    sentinel = "SENTINEL_CREDENTIAL_ACCOUNT_ENDPOINT_EXCEPTION"
    decision_observability.emit_decision_operation(
        "capabilityRead",
        result=sentinel,
        reason_code=sentinel,
        error_class=sentinel,
        diagnostic_id=sentinel,
    )

    serialized = json.dumps(records)
    assert sentinel not in serialized
    assert records[0]["attributes"]["contexer.result"] == "failure"
    assert records[0]["attributes"]["contexer.reason_code"] == "validation_error"
    assert records[1]["fields"]["errorClass"] == "telemetry"


def test_diagnostic_enqueue_failure_is_swallowed(monkeypatch):
    def broken_sink(_records):
        raise OSError("SENTINEL_EXCEPTION_A2 at /private/source.py:42")

    monkeypatch.setattr(decision_observability, "_enqueue_records", broken_sink)

    assert decision_observability.emit_decision_operation(
        "capabilityRead",
        result="success",
        reason_code="none",
        error_class="none",
    ) is None


def test_enqueue_emits_only_canonical_opaque_correlation_and_bounded_counts(monkeypatch):
    records = []
    monkeypatch.setattr(
        decision_observability,
        "_enqueue_records",
        lambda emitted: records.extend(emitted),
    )

    decision_observability.emit_decision_operation(
        "enqueue",
        result="queued",
        reason_code="none",
        error_class="none",
        team_id="40000000-0000-4000-8000-000000000001",
        decision_id="20000000-0000-4000-8000-000000000001",
        policy_generation="30000000-0000-4000-8000-000000000001",
        idempotency_key="50000000-0000-4000-8000-000000000001",
        candidate_id="SENTINEL_CANDIDATE_PROSE",
        attempt=2,
        queue_depth=7,
    )

    span, log = records
    assert set(span["attributes"]) == {
        "contexer.result", "contexer.reason_code", "contexer.error_class",
        "contexer.team_id", "contexer.decision_id", "contexer.policy_generation",
        "contexer.idempotency_key", "contexer.attempt", "contexer.queue_depth",
    }
    assert set(log["fields"]) == {
        "action", "result", "reasonCode", "errorClass", "teamId", "decisionId",
        "policyGeneration", "idempotencyKey", "attempt", "queueDepth",
    }
    assert span["attributes"]["contexer.decision_id"] == \
        "20000000-0000-4000-8000-000000000001"
    assert span["attributes"]["contexer.attempt"] == 2
    assert log["fields"]["queueDepth"] == 7
    assert "SENTINEL_CANDIDATE_PROSE" not in json.dumps(records)


def test_drain_emits_terminal_span_log_and_replay_flag(monkeypatch):
    records = []
    monkeypatch.setattr(
        decision_observability,
        "_enqueue_records",
        lambda emitted: records.extend(emitted),
    )

    decision_observability.emit_decision_operation(
        "drain",
        result="already_pending",
        reason_code="none",
        error_class="none",
        candidate_id="10000000-0000-4000-8000-000000000002",
        replayed=True,
    )

    span, log = records
    assert span["name"] == "decision_proposal.drain"
    assert span["attributes"]["contexer.result"] == "already_pending"
    assert span["attributes"]["contexer.replayed"] is True
    assert log["fields"]["action"] == "decisionProposalDrain"
    assert log["fields"]["candidateId"] == "10000000-0000-4000-8000-000000000002"
    assert log["fields"]["replayed"] is True


def test_untrusted_optional_telemetry_values_are_omitted(monkeypatch):
    records = []
    monkeypatch.setattr(
        decision_observability,
        "_enqueue_records",
        lambda emitted: records.extend(emitted),
    )
    sentinel = "SENTINEL_ACCOUNT_ENDPOINT_REPOSITORY_CONTENT"

    decision_observability.emit_decision_operation(
        "scan",
        result="success",
        reason_code="none",
        error_class="none",
        team_id=sentinel,
        decision_id=sentinel,
        policy_generation=sentinel,
        idempotency_key=sentinel,
        attempt=-1,
        queue_depth=1_000_001,
    )

    assert sentinel not in json.dumps(records)
    assert "contexer.team_id" not in records[0]["attributes"]
    assert "queueDepth" not in records[1]["fields"]
