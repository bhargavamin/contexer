"""Executable A2 invariants for the cross-repository transition contract.

The scenario fixture beside this test is intentionally byte-identical to the Teams copy at
``packages/db/test/fixtures/decision-sharing-transition-invariants.v1.json``. It is a
pre-implementation oracle: Phase B-D production tests must load the same scenarios rather than
restate weaker expectations.
"""

import hashlib
import json
import re
from datetime import datetime
from pathlib import Path


_FIXTURES = Path(__file__).parent / "fixtures"
_CONTRACT_PATH = _FIXTURES / "decision-sharing-transition-contract.v1.json"
_INVARIANTS_PATH = _FIXTURES / "decision-sharing-transition-invariants.v1.json"
_INVARIANTS_SHA256 = "3c5ee30d6385ee48d15b1b111b784a86e4d296efe2f7b600b56326444a1fe761"
_CONTRACT = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))
_INVARIANTS = json.loads(_INVARIANTS_PATH.read_text(encoding="utf-8"))


def _assert_allowed_pair(operation: str, result: str, reason_code: str) -> None:
    allowed = _CONTRACT["observability"]["operationOutcomes"][operation][
        "allowedResultsByReasonCode"
    ]
    assert reason_code in allowed
    assert result in allowed[reason_code]


class _CapturingSink:
    def __init__(self) -> None:
        self.items: list[dict] = []
        self.calls = 0

    def emit(self, channel: str, payload: dict | str) -> None:
        self.calls += 1
        self.items.append({"channel": channel, "payload": payload})


class _ThrowingSink:
    def __init__(self) -> None:
        self.calls = 0

    def emit(self, channel: str, payload: dict | str) -> None:
        self.calls += 1
        raise RuntimeError("SENTINEL_TELEMETRY_SINK_FAILURE_A2")


def _channels_for_operation(operation: str) -> list[str]:
    bindings = _INVARIANTS["observability"]["outputBindings"]
    channels = ["spanAttributesAndEvents"]
    for channel in ("contexerJsonStderr", "teamsJsonStdout", "teamsOtlpLogRecords"):
        if operation in bindings[channel]:
            channels.append(channel)
    return channels


def _teams_pino_record(record: dict, trace_context: dict[str, str]) -> dict:
    logger_name = (
        "decision.proposal"
        if _INVARIANTS["observability"]["runtimeOwnership"][record["operation"]]
        == "contexer_client"
        else "decision.transition"
    )
    return {
        "level": "info",
        "time": "2026-08-30T00:00:00.000Z",
        "service": "contexer",
        "name": logger_name,
        "trace_id": trace_context["traceId"],
        "span_id": trace_context["spanId"],
        "trace_flags": "01",
        **record["log"]["fields"],
        "msg": record["log"]["message"],
    }


def _map_pino_to_otlp(pino_record: dict) -> dict:
    skipped_attributes = {
        "level", "time", "msg", "trace_id", "span_id", "trace_flags"
    }
    attributes = {
        key: value
        for key, value in pino_record.items()
        if key not in skipped_attributes and isinstance(value, str | int | float | bool)
    }
    attributes["trace_id"] = pino_record["trace_id"]
    attributes["span_id"] = pino_record["span_id"]
    timestamp = int(
        datetime.fromisoformat(pino_record["time"].replace("Z", "+00:00")).timestamp()
        * 1000
    )
    return {
        "severityNumber": 9,
        "severityText": "INFO",
        "body": pino_record["msg"],
        "attributes": attributes,
        "timestamp": timestamp,
        "traceId": pino_record["trace_id"],
        "spanId": pino_record["span_id"],
        "traceFlags": 1,
    }


def _raw_inputs_for_record(record: dict) -> dict[str, dict[str, str]]:
    injection_by_source = {
        case["source"]: {"valueClass": case["valueClass"], "value": case["sentinel"]}
        for case in _INVARIANTS["observability"]["sensitiveInjectionCases"]
    }
    return {source: injection_by_source[source] for source in record["injectedSources"]}


def _prepare_reference_telemetry(
    record: dict,
    raw_inputs: dict[str, dict[str, str]],
    *,
    held_locks: tuple[str, ...] = (),
) -> dict:
    forbidden_locks = set(
        _INVARIANTS["observability"]["transportSafety"]["forbiddenHeldLocks"]
    )
    if set(held_locks) & forbidden_locks:
        return {"accepted": False, "payloads": []}

    for source, raw_input in raw_inputs.items():
        value_class = raw_input["valueClass"]
        value = raw_input["value"]
        assert source in record["injectedSources"]
        assert value
        if value_class == "raw_exception_message_or_stack":
            try:
                raise RuntimeError(value)
            except RuntimeError as error:
                assert error.args == (value,)
        else:
            assert len(value) > 0

    trace_context = _INVARIANTS["observability"]["traceContext"]
    payloads = []
    for channel in _channels_for_operation(record["operation"]):
        if channel == "spanAttributesAndEvents":
            payload = {
                **record["span"],
                "events": [],
                "context": trace_context,
            }
        elif channel == "contexerJsonStderr":
            payload = json.dumps(
                {"message": record["log"]["message"], **record["log"]["fields"]},
                separators=(",", ":"),
                sort_keys=True,
            )
        elif channel == "teamsJsonStdout":
            payload = json.dumps(
                _teams_pino_record(record, trace_context),
                separators=(",", ":"),
                sort_keys=True,
            )
        else:
            payload = _map_pino_to_otlp(_teams_pino_record(record, trace_context))
        payloads.append({"channel": channel, "payload": payload})
    return {"accepted": True, "payloads": payloads}


def _deliver_reference_telemetry(
    payloads: list[dict], sink: _CapturingSink | _ThrowingSink
) -> dict[str, bool]:
    sink_failed = False
    for item in payloads:
        try:
            sink.emit(item["channel"], item["payload"])
        except RuntimeError:
            sink_failed = True
    return {"sinkFailed": sink_failed}


def test_fixture_is_pinned_to_the_merged_a1_contract():
    assert hashlib.sha256(_INVARIANTS_PATH.read_bytes()).hexdigest() == _INVARIANTS_SHA256
    assert _INVARIANTS["schemaVersion"] == 1
    assert _INVARIANTS["baseContract"] == {
        "version": 1,
        "sha256": hashlib.sha256(_CONTRACT_PATH.read_bytes()).hexdigest(),
    }
    assert _INVARIANTS["bindingPolicy"] == {
        "phaseA2Purpose": "executable_preimplementation_oracle",
        "futureProductionTestsMustLoadThisFixture": True,
        "fixtureOnlyDoesNotSatisfyRuntimeVerification": True,
    }


def test_pending_candidate_cannot_change_check_or_guard_authority():
    scenario = _INVARIANTS["invariants"]["pendingCandidateCheckInvariance"]
    assert scenario["pendingCandidateState"] == "shared_candidate"
    assert scenario["checkInputAfter"] == scenario["checkInputBefore"]
    assert scenario["pendingCandidateId"] not in scenario["checkInputAfter"][
        "applicableDecisionIds"
    ]
    assert scenario["pendingCandidateId"] not in scenario["checkInputAfter"][
        "blockingDecisionIds"
    ]
    assert scenario["checkAfter"] == scenario["checkBefore"]
    assert scenario["pendingCandidateId"] not in scenario["checkAfter"]["findingDecisionIds"]
    assert scenario["pendingCandidateId"] not in scenario["checkAfter"][
        "guardBlockingDecisionIds"
    ]
    assert scenario["authoritativeDecisionId"] in scenario["checkAfter"]["findingDecisionIds"]
    assert scenario["expected"] == {
        "candidateInCheckContext": False,
        "candidateInBlockingIds": False,
        "findingVerdictUnchanged": True,
        "scoreUnchanged": True,
        "githubConclusionUnchanged": True,
    }


def test_stale_predecessor_fails_closed_without_partial_writes():
    scenario = _INVARIANTS["invariants"]["stalePredecessorApproval"]
    assert scenario["candidate"]["supersedesDecisionId"] == scenario["predecessor"]["id"]
    assert scenario["predecessor"]["stateAtApproval"] != "team_approved"
    outcome = scenario["outcome"]
    _assert_allowed_pair("transitionApprove", outcome["result"], outcome["reasonCode"])
    assert outcome == {
        "result": "conflict",
        "reasonCode": "stale_predecessor",
        "candidateState": "shared_candidate",
        "predecessorState": "superseded",
        "replacementApproved": False,
        "resolutionInserted": False,
        "auditInserted": False,
    }


def test_cross_team_cross_repo_and_global_candidates_never_match():
    scenario = _INVARIANTS["invariants"]["exactTransitionMatching"]
    cases = {case["name"]: case for case in scenario["cases"]}
    assert set(cases) == {
        "candidate_team_mismatch", "predecessor_team_mismatch", "finding_team_mismatch",
        "candidate_finding_repo_mismatch",
        "candidate_predecessor_repo_mismatch", "supersedes_finding_mismatch",
        "global_candidate", "global_predecessor", "global_finding",
    }
    assert cases["candidate_team_mismatch"]["candidateTeamId"] != cases[
        "candidate_team_mismatch"
    ][
        "predecessorTeamId"
    ]
    assert cases["candidate_team_mismatch"]["candidateTeamId"] != cases[
        "candidate_team_mismatch"
    ]["findingTeamId"]
    assert cases["predecessor_team_mismatch"]["predecessorTeamId"] != cases[
        "predecessor_team_mismatch"
    ]["candidateTeamId"]
    assert cases["finding_team_mismatch"]["findingTeamId"] != cases[
        "finding_team_mismatch"
    ]["candidateTeamId"]
    assert cases["candidate_finding_repo_mismatch"]["candidateRepoKey"] != cases[
        "candidate_finding_repo_mismatch"
    ]["findingRepoKey"]
    assert cases["candidate_predecessor_repo_mismatch"]["candidateRepoKey"] != cases[
        "candidate_predecessor_repo_mismatch"
    ]["predecessorRepoKey"]
    assert cases["supersedes_finding_mismatch"]["candidateSupersedesDecisionId"] != cases[
        "supersedes_finding_mismatch"
    ]["findingDecisionId"]
    assert cases["global_candidate"]["candidateRepoKey"] is None
    assert cases["global_predecessor"]["predecessorRepoKey"] is None
    assert cases["global_finding"]["findingRepoKey"] is None
    for case in cases.values():
        assert case["latestMatchingFindingId"] is None
        assert case["candidateSupersedesDecisionId"] == case["predecessorDecisionId"]
        assert case["publicRefusal"] == scenario["publicRefusal"]
        _assert_allowed_pair("transitionRead", case["result"], case["reasonCode"])
    assert scenario["publicRefusal"] == "transition not found or not available"


def test_approval_rollback_preserves_d1_and_leaves_d2_pending():
    scenario = _INVARIANTS["invariants"]["approvalRollback"]
    assert scenario["injectedFailurePoints"] == [
        "approve_replacement", "tombstone_predecessor", "apply_enforcement_choice",
        "insert_decision_updated_resolution", "insert_transition_audit",
    ]
    assert scenario["stateAfterEachFailure"] == scenario["stateBefore"]
    assert scenario["stateAfterEachFailure"] == {
        "predecessorState": "team_approved",
        "predecessorEnforcement": "blocking",
        "predecessorEnforcementEpoch": 7,
        "candidateState": "shared_candidate",
        "candidateEnforcement": None,
        "candidateEnforcementEpoch": None,
        "resolutionCount": 0,
        "auditCount": 0,
    }
    outcome = scenario["outcome"]
    _assert_allowed_pair("transitionApprove", outcome["result"], outcome["reasonCode"])
    assert outcome["committed"] is False


def test_busy_proposal_lock_never_blocks_capture_or_approval():
    cases = _INVARIANTS["invariants"]["busyProposalLock"]["cases"]
    assert {(case["operation"], case["lockCondition"]) for case in cases} == {
        ("capture", "busy"), ("capture", "unavailable"),
        ("approval", "busy"), ("approval", "unavailable"),
    }
    for case in cases:
        assert case["functionalMutationCommitted"] is True
        assert case["returned"] is True
        assert case["blockingWaitMs"] == 0
        assert case["networkCallsWhileLocked"] == 0
        assert case["telemetryFlushesWhileLocked"] == 0
        _assert_allowed_pair("enqueue", case["sidecarResult"], case["sidecarReasonCode"])


def test_legacy_server_keeps_manual_reconciliation_but_refuses_automatic_send():
    cases = _INVARIANTS["invariants"]["legacyServerRefusal"]["cases"]
    assert {case["name"] for case in cases} == {
        "missing_automatic_capability", "missing_account_binding",
        "missing_atomic_reconciliation",
    }
    for case in cases:
        prerequisites = (
            case["automaticDecisionProposalPresent"],
            case["accountFingerprintPresent"],
            case["atomicReconciliationPresent"],
        )
        assert prerequisites.count(False) == 1
        assert case["manualReconciliationAllowed"] is True
        outcome = case["automaticOutcome"]
        _assert_allowed_pair(outcome["operation"], outcome["result"], outcome["reasonCode"])
        assert outcome["automaticSubmitWireCalls"] == 0


def test_every_operation_uses_the_exact_a1_span_log_and_result_reason_contract():
    observability = _INVARIANTS["observability"]
    contract = _CONTRACT["observability"]
    records = {record["operation"]: record for record in observability["operationRecords"]}
    assert set(records) == set(contract["operations"])

    allowed_span_keys = set(contract["spanContract"]["requiredAttributes"])
    allowed_span_keys.update(contract["spanContract"]["allowedCorrelationAttributes"])
    allowed_span_keys.update(contract["spanContract"]["allowedDiagnosticAttributes"])
    allowed_log_keys = set(contract["logContract"]["requiredFields"])
    allowed_log_keys.update(contract["logContract"]["allowedCorrelationFields"])
    allowed_log_keys.update(contract["logContract"]["allowedDiagnosticFields"])
    observed_span_keys: set[str] = set()
    observed_log_keys: set[str] = set()

    for operation, record in records.items():
        operation_contract = contract["operations"][operation]
        attributes = record["span"]["attributes"]
        fields = record["log"]["fields"]
        assert record["span"]["name"] == operation_contract["span"]
        assert fields["action"] == operation_contract["terminalLogEvent"]
        assert record["log"]["message"] == observability["fixedMessage"]
        assert set(contract["spanContract"]["requiredAttributes"]) <= set(attributes)
        assert set(contract["logContract"]["requiredFields"]) <= set(fields)
        assert set(attributes) <= allowed_span_keys
        assert set(fields) <= allowed_log_keys
        assert (attributes["contexer.result"], attributes["contexer.reason_code"]) == (
            fields["result"],
            fields["reasonCode"],
        )
        _assert_allowed_pair(operation, fields["result"], fields["reasonCode"])
        observed_span_keys.update(attributes)
        observed_log_keys.update(fields)

    assert observed_span_keys == allowed_span_keys
    assert observed_log_keys == allowed_log_keys


def test_telemetry_exercises_closed_values_without_leaking_secret_sentinels():
    observability = _INVARIANTS["observability"]
    contract = _CONTRACT["observability"]
    values = contract["valuePolicy"]
    assert set(observability["sensitiveInputs"]) == set(contract["forbiddenValueClasses"])
    injection_cases = observability["sensitiveInjectionCases"]
    assert {case["valueClass"] for case in injection_cases} == set(
        observability["sensitiveInputs"]
    )
    for value_class, canonical_sentinel in observability["sensitiveInputs"].items():
        assert canonical_sentinel in {
            case["sentinel"]
            for case in injection_cases
            if case["valueClass"] == value_class
        }
    assert len({case["source"] for case in injection_cases}) == len(injection_cases)
    assert any(case["source"].endswith("thrownException") for case in injection_cases)
    assert observability["spanEventPolicy"] == {
        "recordRawExceptions": False,
        "allowedEvents": [],
    }
    assert observability["runtimeOwnership"] == {
        "capabilityRead": "contexer_client",
        "policyChange": "contexer_client",
        "scan": "contexer_client",
        "enqueue": "contexer_client",
        "drain": "contexer_client",
        "transitionRead": "teams",
        "transitionApprove": "teams",
        "transitionRecheck": "teams",
    }

    records = {record["operation"]: record for record in observability["operationRecords"]}
    output_bindings = observability["outputBindings"]
    assert set(output_bindings) == {
        "spanAttributesAndEvents", "contexerJsonStderr", "teamsJsonStdout",
        "teamsOtlpEnabled", "teamsOtlpLogRecords",
    }
    assert set(output_bindings["spanAttributesAndEvents"]) == set(records)
    assert set(output_bindings["contexerJsonStderr"]) == {
        "capabilityRead", "policyChange", "scan", "enqueue", "drain",
    }
    assert set(output_bindings["teamsJsonStdout"]) == {
        "transitionRead", "transitionApprove", "transitionRecheck",
    }
    assert output_bindings["teamsOtlpEnabled"] is True
    assert set(output_bindings["teamsOtlpLogRecords"]) == {
        "transitionRead", "transitionApprove", "transitionRecheck",
    }

    observed_injected_sources: set[str] = set()
    observed_injected_classes: set[str] = set()
    captured_sink = _CapturingSink()
    for record in records.values():
        raw_inputs = _raw_inputs_for_record(record)
        observed_injected_sources.update(raw_inputs)
        observed_injected_classes.update(
            raw_input["valueClass"] for raw_input in raw_inputs.values()
        )
        serialized_inputs = json.dumps(raw_inputs, sort_keys=True)
        for raw_input in raw_inputs.values():
            assert raw_input["value"] in serialized_inputs
        calls_before_prepare = captured_sink.calls
        prepared = _prepare_reference_telemetry(record, raw_inputs)
        assert prepared["accepted"] is True
        assert captured_sink.calls == calls_before_prepare
        assert _deliver_reference_telemetry(prepared["payloads"], captured_sink) == {
            "sinkFailed": False
        }
        payloads = {
            item["channel"]: item["payload"] for item in prepared["payloads"]
        }
        assert payloads["spanAttributesAndEvents"]["context"] == observability[
            "traceContext"
        ]
        assert payloads["spanAttributesAndEvents"]["events"] == []
        if record["operation"] in output_bindings["contexerJsonStderr"]:
            stderr_line = payloads["contexerJsonStderr"]
            assert isinstance(stderr_line, str)
            assert "\n" not in stderr_line
            assert json.loads(stderr_line) == {
                "message": record["log"]["message"],
                **record["log"]["fields"],
            }
        if record["operation"] in output_bindings["teamsJsonStdout"]:
            stdout_line = payloads["teamsJsonStdout"]
            assert isinstance(stdout_line, str)
            assert "\n" not in stdout_line
            stdout_record = json.loads(stdout_line)
            expected_pino_record = _teams_pino_record(
                record, observability["traceContext"]
            )
            assert stdout_record == expected_pino_record
            assert stdout_record["msg"] == record["log"]["message"]
            assert stdout_record["trace_flags"] == "01"
            otlp_record = payloads["teamsOtlpLogRecords"]
            assert otlp_record == _map_pino_to_otlp(expected_pino_record)
            assert otlp_record["body"] == stdout_record["msg"]
            assert otlp_record["attributes"] == {
                "service": stdout_record["service"],
                "name": stdout_record["name"],
                **record["log"]["fields"],
                "trace_id": stdout_record["trace_id"],
                "span_id": stdout_record["span_id"],
            }
            assert otlp_record["traceId"] == stdout_record["trace_id"]
            assert otlp_record["spanId"] == stdout_record["span_id"]
            assert otlp_record["traceFlags"] == 1
    assert observed_injected_sources == {
        case["source"] for case in observability["sensitiveInjectionCases"]
    }
    assert observed_injected_classes == set(observability["sensitiveInputs"])
    serialized_outputs = json.dumps(captured_sink.items, sort_keys=True)
    for injection_case in observability["sensitiveInjectionCases"]:
        assert injection_case["sentinel"] not in serialized_outputs

    for record in observability["operationRecords"]:
        attributes = record["span"]["attributes"]
        fields = record["log"]["fields"]
        for value in (*attributes.values(), *fields.values(), record["log"]["message"]):
            if isinstance(value, str):
                assert len(value) <= values["maxStringLength"]
        assert fields["action"] in contract["logContract"]["allowedActionValues"]
        assert fields["result"] in values["results"]
        assert fields["reasonCode"] in values["reasonCodes"]
        if "errorClass" in fields:
            assert fields["errorClass"] in values["errorClasses"]
        if "enforcementChoice" in fields:
            assert fields["enforcementChoice"] in values["enforcementChoices"]
        if "recheckOutcome" in fields:
            assert fields["recheckOutcome"] in values["recheckOutcomes"]
        if "diagnosticId" in fields:
            assert re.fullmatch(
                _CONTRACT["persistedErrorPolicy"]["diagnosticIdPattern"],
                fields["diagnosticId"],
            )
        mirrored_fields = {
            "contexer.team_id": "teamId",
            "contexer.decision_id": "decisionId",
            "contexer.candidate_id": "candidateId",
            "contexer.policy_generation": "policyGeneration",
            "contexer.idempotency_key": "idempotencyKey",
            "contexer.error_class": "errorClass",
            "contexer.diagnostic_id": "diagnosticId",
            "contexer.attempt": "attempt",
            "contexer.queue_depth": "queueDepth",
            "contexer.replayed": "replayed",
            "contexer.enforcement_choice": "enforcementChoice",
            "contexer.recheck_outcome": "recheckOutcome",
        }
        attributes = record["span"]["attributes"]
        for span_key, log_key in mirrored_fields.items():
            if span_key in attributes:
                assert fields[log_key] == attributes[span_key]
        if "contexer.error_class" in attributes:
            assert attributes["contexer.error_class"] in values["errorClasses"]
        if "contexer.enforcement_choice" in attributes:
            assert attributes["contexer.enforcement_choice"] in values["enforcementChoices"]
        if "contexer.recheck_outcome" in attributes:
            assert attributes["contexer.recheck_outcome"] in values["recheckOutcomes"]
        if "contexer.diagnostic_id" in attributes:
            assert re.fullmatch(
                _CONTRACT["persistedErrorPolicy"]["diagnosticIdPattern"],
                attributes["contexer.diagnostic_id"],
            )


def test_telemetry_sink_failure_and_dispatch_cannot_change_outcomes_or_hold_locks():
    observability = _INVARIANTS["observability"]
    records = {record["operation"]: record for record in observability["operationRecords"]}
    for scenario in observability["sinkFailureScenarios"]:
        assert scenario["failingSinkOutcome"] == scenario["healthySinkOutcome"]
        assert scenario["failingSinkState"] == scenario["healthySinkState"]
        assert scenario["sinkErrorSwallowed"] is True
        assert scenario["rawExceptionRecorded"] is False
        _assert_allowed_pair(
            scenario["operation"],
            scenario["failingSinkOutcome"]["result"],
            scenario["failingSinkOutcome"]["reasonCode"],
        )
        record = records[scenario["operation"]]
        raw_inputs = _raw_inputs_for_record(record)
        throwing_sink = _ThrowingSink()
        prepared = _prepare_reference_telemetry(record, raw_inputs)
        assert prepared["accepted"] is True
        assert throwing_sink.calls == 0
        delivery = _deliver_reference_telemetry(prepared["payloads"], throwing_sink)
        assert delivery == {"sinkFailed": True}
        assert throwing_sink.calls > 0

    transport = observability["transportSafety"]
    assert transport["contexer"] == {
        "exportMode": "stderr_jsonl_only",
        "networkExporter": False,
        "nonBlocking": True,
        "deliveryMode": "enqueue_and_return",
    }
    assert {item["operation"] for item in transport["dispatches"]} == set(
        _CONTRACT["observability"]["operations"]
    )
    forbidden_locks = set(transport["forbiddenHeldLocks"])
    for dispatch in transport["dispatches"]:
        assert not (set(dispatch["locksHeld"]) & forbidden_locks)
    record = records["enqueue"]
    for forbidden_lock in forbidden_locks:
        rejected = _prepare_reference_telemetry(
            record,
            {},
            held_locks=(forbidden_lock,),
        )
        assert rejected == {"accepted": False, "payloads": []}
