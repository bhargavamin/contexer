"""Cross-repository decision-sharing/review/drift-transition V1 contract.

The fixture beside this test is intentionally byte-identical to
``contexer-teams/packages/db/test/fixtures/decision-sharing-transition-contract.v1.json``.
Neither repository imports the other: each owns its copy, pins the same digest, and validates the
fields it will later produce or consume.
"""

import hashlib
import json
import re
from pathlib import Path

from contexer import store


_CONTRACT_PATH = (
    Path(__file__).parent / "fixtures" / "decision-sharing-transition-contract.v1.json"
)
_CONTRACT_SHA256 = "716eb561a4c45e97bc3d9fc693dfa413894cffac93a2a63184142cab4f70c2cf"
_CONTRACT = json.loads(_CONTRACT_PATH.read_text(encoding="utf-8"))


def test_fixture_is_the_cross_repository_bytes_agreed_for_v1():
    assert hashlib.sha256(_CONTRACT_PATH.read_bytes()).hexdigest() == _CONTRACT_SHA256
    assert _CONTRACT["contractVersion"] == 1


def test_capability_is_additive_account_bound_and_fail_closed_on_legacy_server():
    response = _CONTRACT["capabilityResponse"]
    assert set(response) == {"accountFingerprint", "capabilities"}
    assert response["accountFingerprint"].startswith("acctfp_v1_")
    assert "@" not in response["accountFingerprint"]
    assert response["capabilities"]["automaticDecisionProposal"] == {"version": 1}
    assert response["capabilities"]["decisionReconciliation"] == {
        "version": 1,
        "atomicSubmit": True,
        "preview": True,
        "threeWayMerge": False,
        "legacyShareDecision": True,
    }

    legacy = _CONTRACT["legacyCapabilityResponse"]
    assert "accountFingerprint" not in legacy
    assert "automaticDecisionProposal" not in legacy["capabilities"]


def test_atomic_reconciliation_outcomes_keep_the_existing_wire_shape():
    outcomes = _CONTRACT["reconciliationOutcomes"]
    assert set(outcomes) == {
        "initial", "update", "alreadyPending", "unchanged", "staleHead",
        "unauthorizedMcpError",
    }
    result_keys = {
        "status", "kind", "personalHead", "teamHead", "candidateId", "revisionId", "replayed",
        "team",
    }
    expected = {
        "initial": ("submitted", "initial"),
        "update": ("submitted", "update"),
        "alreadyPending": ("already_pending", "update"),
        "unchanged": ("unchanged", "unchanged"),
        "staleHead": ("heads_changed", "conflict"),
    }
    for name, (status, kind) in expected.items():
        assert set(outcomes[name]) == result_keys
        assert (outcomes[name]["status"], outcomes[name]["kind"]) == (status, kind)
        assert outcomes[name]["team"] == {
            "id": "40000000-0000-4000-8000-000000000001",
            "name": "Platform",
        }
    assert outcomes["unauthorizedMcpError"] == {
        "isError": True,
        "content": [{"type": "text", "text": "team not found or caller is not a member"}],
    }


def test_policy_and_intent_pin_schema_v1_and_the_complete_destination_binding():
    sidecars = _CONTRACT["sidecars"]
    policy = sidecars["policy"]
    intent = sidecars["intent"]
    assert set(policy) == {
        "schema_version", "mode", "policy_generation", "repo_key", "repo_slug", "endpoint",
        "account_fingerprint", "team_id", "team_name_at_confirmation", "enabled_at",
        "include_existing", "baseline_revision_ids", "paused_reason",
    }
    assert set(intent) == {
        "schema_version", "idempotency_key", "policy_generation", "decision_id", "revision_id",
        "repo_path", "repo_key", "endpoint", "account_fingerprint", "team_id", "queued_at",
        "attempts", "last_error_code", "last_error_class", "diagnostic_id",
    }
    assert policy["schema_version"] == intent["schema_version"] == 1
    assert policy["mode"] == "propose_approved"
    assert policy["include_existing"] is False
    for field in ("policy_generation", "repo_key", "endpoint", "account_fingerprint", "team_id"):
        assert intent[field] == policy[field]
    assert isinstance(policy["team_id"], str)  # one destination, never an accidental team array
    assert policy["repo_slug"] == store.repo_slug(intent["repo_path"])


def test_attention_pins_schema_v1_terminal_reason_and_original_destination_identity():
    sidecars = _CONTRACT["sidecars"]
    intent = sidecars["intent"]
    attention = sidecars["attention"]
    assert set(attention) == {
        "schema_version", "idempotency_key", "policy_generation", "decision_id", "revision_id",
        "repo_path", "repo_key", "endpoint", "account_fingerprint", "team_id", "reason",
        "moved_at", "last_error_code", "last_error_class", "diagnostic_id",
    }
    assert attention["schema_version"] == 1
    for field in (
        "idempotency_key", "policy_generation", "decision_id", "revision_id", "repo_path",
        "repo_key", "endpoint", "account_fingerprint", "team_id",
    ):
        assert attention[field] == intent[field]
    assert attention["reason"] == "not_member"
    assert attention["last_error_code"] == "not_member"
    assert attention["last_error_class"] == "authorization"
    assert attention["diagnostic_id"].startswith("diag_")


def test_persisted_errors_use_closed_codes_and_independent_diagnostic_ids():
    policy = _CONTRACT["persistedErrorPolicy"]
    assert set(policy) == {
        "codeValues", "classValues", "maxCodeLength", "maxClassLength",
        "diagnosticIdPattern", "diagnosticIdMaxLength", "diagnosticIdIndependentRandom",
    }
    assert policy["diagnosticIdIndependentRandom"] is True

    attention = _CONTRACT["sidecars"]["attention"]
    assert attention["last_error_code"] in policy["codeValues"]
    assert attention["last_error_class"] in policy["classValues"]
    assert len(attention["last_error_code"]) <= policy["maxCodeLength"]
    assert len(attention["last_error_class"]) <= policy["maxClassLength"]

    diagnostic_id = attention["diagnostic_id"]
    assert re.fullmatch(policy["diagnosticIdPattern"], diagnostic_id)
    assert len(diagnostic_id) <= policy["diagnosticIdMaxLength"]
    for sensitive_value in (
        _CONTRACT["capabilityResponse"]["accountFingerprint"],
        _CONTRACT["capabilityResponse"]["accountFingerprint"].removeprefix("acctfp_v1_"),
        _CONTRACT["sidecars"]["policy"]["team_id"],
        _CONTRACT["sidecars"]["policy"]["repo_key"],
        attention["last_error_code"],
    ):
        assert sensitive_value not in diagnostic_id


def test_receipts_pin_all_v1_states_and_destination_identity_fields():
    receipts = _CONTRACT["sidecars"]["receipts"]
    receipt_keys = {
        "schema_version", "policy_generation", "endpoint", "account_fingerprint", "repo_key",
        "team_id", "decision_id", "revision_id", "state", "candidate_id", "reason",
        "recorded_at",
    }
    by_state = {receipt["state"]: receipt for receipt in receipts}
    assert set(by_state) == {
        "queued", "submitted", "already_pending", "unchanged", "attention", "baseline"
    }
    for receipt in receipts:
        assert set(receipt) == receipt_keys
        assert receipt["schema_version"] == 1
    assert by_state["submitted"]["candidate_id"]
    assert by_state["already_pending"]["candidate_id"]
    assert by_state["attention"]["reason"] == "not_member"
    assert by_state["baseline"]["revision_id"] == "revision-1"


def test_transition_shape_requires_exact_repo_team_lineage_and_explicit_enforcement():
    assert _CONTRACT["repositoryMatching"] == {
        "mode": "exact_non_null_canonical",
        "automaticGlobalDecisions": False,
    }
    with_drift = _CONTRACT["transitionCandidate"]
    without_drift = _CONTRACT["transitionCandidateWithoutMatchingDrift"]
    transition_keys = {
        "candidate", "predecessor", "latestMatchingDriftFinding",
        "otherRecentAffectedFindingCount", "requiresExplicitEnforcementChoice",
        "allowedEnforcementChoices",
    }
    for transition in (with_drift, without_drift):
        assert set(transition) == transition_keys
        assert set(transition["candidate"]) == {
            "id", "teamId", "state", "sourceDecisionId", "supersedesDecisionId", "repoKey",
            "title", "content", "rationale", "sourceFiles",
        }
        assert set(transition["predecessor"]) == {
            "id", "teamId", "repoKey", "title", "content", "rationale", "sourceFiles",
            "enforcement", "enforcementReason",
        }
        assert transition["candidate"]["supersedesDecisionId"] == transition["predecessor"]["id"]
        assert transition["candidate"]["teamId"] == transition["predecessor"]["teamId"]
        assert transition["candidate"]["repoKey"] == transition["predecessor"]["repoKey"]
        assert transition["candidate"]["repoKey"] == "github.com/org/repo"
        assert transition["requiresExplicitEnforcementChoice"] is True
        assert transition["allowedEnforcementChoices"] == ["blocking", "advisory"]

    finding = with_drift["latestMatchingDriftFinding"]
    assert set(finding) == {
        "findingId", "checkId", "repoKey", "pullRequestNumber", "commitSha", "severity",
        "explanation", "remediation",
    }
    assert finding["repoKey"] == with_drift["candidate"]["repoKey"]
    assert with_drift["otherRecentAffectedFindingCount"] == 2
    assert without_drift["latestMatchingDriftFinding"] is None
    assert without_drift["otherRecentAffectedFindingCount"] == 0


def test_decision_updated_resolution_pins_replacement_and_origin_provenance():
    resolution = _CONTRACT["decisionUpdatedResolution"]
    assert set(resolution) == {
        "teamId", "decisionId", "repo", "resolution", "replacementDecisionId",
        "originCheckId", "originFindingId", "note", "resolvedByUserId",
    }
    assert resolution["resolution"] == "decision_updated"
    assert resolution["decisionId"] == _CONTRACT["transitionCandidate"]["predecessor"]["id"]
    assert resolution["replacementDecisionId"] == _CONTRACT["transitionCandidate"]["candidate"]["id"]
    assert resolution["originCheckId"] == (
        _CONTRACT["transitionCandidate"]["latestMatchingDriftFinding"]["checkId"]
    )
    assert resolution["originFindingId"] == (
        _CONTRACT["transitionCandidate"]["latestMatchingDriftFinding"]["findingId"]
    )


def test_observability_contract_requires_tracing_and_forbids_sensitive_payloads():
    observability = _CONTRACT["observability"]
    assert set(observability) == {
        "operations", "operationOutcomes", "spanContract", "logContract", "valuePolicy",
        "forbiddenValueClasses", "contexerLocalTelemetry", "teamsTelemetry",
        "telemetryFailureMustNotChangeOutcome",
    }
    operations = observability["operations"]
    assert set(operations) == {
        "capabilityRead", "policyChange", "scan", "enqueue", "drain", "transitionRead",
        "transitionApprove", "transitionRecheck",
    }
    for operation in operations.values():
        assert set(operation) == {"span", "terminalLogEvent"}
        assert operation["span"]
        assert operation["terminalLogEvent"]

    values = observability["valuePolicy"]
    operation_outcomes = observability["operationOutcomes"]
    expected_outcomes = {
        "capabilityRead": {
            "none": ["success"], "unsupported_protocol": ["refused"],
            "not_authorized": ["refused"], "rate_limited": ["failure"],
            "transport_error": ["failure"], "validation_error": ["failure"],
        },
        "policyChange": {
            "none": ["success"], "unsupported_protocol": ["refused"],
            "account_mismatch": ["refused"], "repo_mismatch": ["refused"],
            "team_mismatch": ["refused"], "not_member": ["refused"],
            "not_authorized": ["refused"], "validation_error": ["refused"],
            "cancelled": ["no_op"],
        },
        "scan": {
            "none": ["success", "queued"], "policy_disabled": ["skipped"],
            "ineligible_revision": ["skipped"], "global_decision": ["skipped"],
            "baseline_revision": ["skipped"], "duplicate_receipt": ["no_op"],
            "corrupt_queue": ["failure"], "lock_busy": ["no_op"],
            "validation_error": ["failure"],
        },
        "enqueue": {
            "none": ["queued"], "policy_disabled": ["refused"],
            "policy_mismatch": ["refused"], "duplicate_receipt": ["no_op"],
            "corrupt_queue": ["failure"], "lock_busy": ["no_op"],
            "validation_error": ["failure"],
        },
        "drain": {
            "none": ["submitted", "already_pending", "unchanged"],
            "unsupported_protocol": ["refused"], "account_mismatch": ["attention"],
            "policy_mismatch": ["attention"], "repo_mismatch": ["attention"],
            "team_mismatch": ["attention"], "not_member": ["attention"],
            "not_authorized": ["attention"], "stale_head": ["conflict"],
            "stale_intent": ["attention"], "duplicate_receipt": ["no_op"],
            "corrupt_queue": ["failure"], "lock_busy": ["no_op"],
            "rate_limited": ["retry"], "quota_exceeded": ["attention"],
            "trial_expired": ["attention"], "transient_error": ["retry"],
            "transport_error": ["retry"], "validation_error": ["failure"],
        },
        "transitionRead": {
            "none": ["success"], "repo_mismatch": ["refused"],
            "team_mismatch": ["refused"], "not_member": ["refused"],
            "not_authorized": ["refused"], "stale_candidate": ["no_op"],
            "origin_finding_mismatch": ["refused"], "database_error": ["failure"],
            "validation_error": ["failure"],
        },
        "transitionApprove": {
            "none": ["success"], "not_authorized": ["refused"],
            "team_mismatch": ["refused"], "repo_mismatch": ["refused"],
            "stale_predecessor": ["conflict"], "stale_candidate": ["conflict"],
            "candidate_rejected": ["conflict"], "candidate_withdrawn": ["conflict"],
            "missing_enforcement_choice": ["refused"],
            "origin_finding_mismatch": ["refused"], "database_error": ["failure"],
            "rollback": ["failure"], "validation_error": ["failure"],
        },
        "transitionRecheck": {
            "none": ["queued"], "stale_head": ["failure"],
            "runner_launch_failed": ["partial_success"], "no_managed_check": ["no_op"],
            "already_running": ["already_running"], "database_error": ["failure"],
            "rate_limited": ["failure"], "transport_error": ["failure"],
            "validation_error": ["failure"],
        },
    }
    assert operation_outcomes == {
        operation: {"allowedResultsByReasonCode": outcomes}
        for operation, outcomes in expected_outcomes.items()
    }
    for outcomes in expected_outcomes.values():
        assert set(outcomes) <= set(values["reasonCodes"])
        assert {result for results in outcomes.values() for result in results} <= set(
            values["results"]
        )
    assert "success" not in expected_outcomes["transitionApprove"]["rollback"]
    assert "success" not in expected_outcomes["transitionApprove"][
        "missing_enforcement_choice"
    ]

    assert observability["spanContract"] == {
        "requiredAttributes": ["contexer.result", "contexer.reason_code"],
        "allowedCorrelationAttributes": [
            "contexer.team_id", "contexer.decision_id", "contexer.candidate_id",
            "contexer.policy_generation", "contexer.idempotency_key",
        ],
        "allowedDiagnosticAttributes": [
            "contexer.error_class", "contexer.diagnostic_id", "contexer.attempt",
            "contexer.queue_depth",
            "contexer.replayed", "contexer.enforcement_choice", "contexer.recheck_outcome",
        ],
        "recordRawExceptions": False,
    }
    assert observability["logContract"] == {
        "requiredFields": ["action", "result", "reasonCode"],
        "allowedCorrelationFields": [
            "teamId", "decisionId", "candidateId", "policyGeneration", "idempotencyKey",
        ],
        "allowedDiagnosticFields": [
            "errorClass", "diagnosticId", "attempt", "queueDepth", "replayed",
            "enforcementChoice", "recheckOutcome",
        ],
        "allowedActionValues": [
            "decisionProposalCapabilityRead", "decisionProposalPolicyChange",
            "decisionProposalScan", "decisionProposalEnqueue", "decisionProposalDrain",
            "decisionTransitionRead", "decisionTransitionApprove",
            "decisionTransitionRecheck",
        ],
        "messagePolicy": "fixed_event_message_only",
        "correlateWithActiveTrace": True,
    }
    assert set(observability["logContract"]["allowedActionValues"]) == {
        operation["terminalLogEvent"] for operation in operations.values()
    }
    assert values["mode"] == "closed_allowlist"
    assert values["maxStringLength"] == 128
    assert "failure" in values["results"]
    assert "not_member" in values["reasonCodes"]
    assert "transport" in values["errorClasses"]
    assert "database" in values["errorClasses"]
    assert values["enforcementChoices"] == ["none", "blocking", "advisory"]
    assert values["recheckOutcomes"] == [
        "none", "queued", "already_running", "no_managed_check", "runner_launch_failed",
    ]
    assert {
        "policy_disabled", "global_decision", "baseline_revision", "duplicate_receipt",
        "corrupt_queue", "stale_predecessor", "missing_enforcement_choice",
        "origin_finding_mismatch", "database_error", "rollback", "cancelled",
    } <= set(values["reasonCodes"])
    assert set(observability["forbiddenValueClasses"]) == {
        "credential_or_token", "account_identity", "raw_endpoint", "repository_identity_or_path",
        "decision_or_evidence_prose", "finding_or_resolution_prose",
        "raw_exception_message_or_stack", "person_or_team_display_data",
    }
    assert observability["contexerLocalTelemetry"] == {
        "exportMode": "stderr_jsonl_only",
        "networkExporter": False,
        "flushWhileStoreOrSidecarLockHeld": False,
        "nonBlocking": True,
    }
    assert observability["teamsTelemetry"] == {
        "logger": "@contexer/observability.getLogger",
        "tracer": "@contexer/observability.withSpan",
        "recordRawExceptions": False,
    }
    assert observability["telemetryFailureMustNotChangeOutcome"] is True
