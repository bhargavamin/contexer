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
