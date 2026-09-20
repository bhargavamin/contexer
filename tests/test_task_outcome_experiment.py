"""Contract tests for the offline Contract-06 experiment and outcome verifier."""

from __future__ import annotations

import copy
import json
import os
from collections import Counter

import pytest

from benchmarks.applicability import task_outcome_experiment as experiment


@pytest.fixture(scope="module")
def fixture_data():
    return experiment.load_fixture()


@pytest.fixture(scope="module")
def report(fixture_data):
    return experiment.build_report(fixture_data)


def test_fixture_freezes_sixty_scenarios_three_splits_and_three_repositories(fixture_data):
    cases = experiment.expand_cases(fixture_data)
    assert Counter(case["split"] for case in cases) == Counter({
        "development": 30, "validation": 15, "heldout": 15,
    })
    assert len({case["repo_id"] for case in cases}) == 3
    assert len({case["family"] for case in cases}) == 6
    assert all(case["repo_id"].startswith("synthetic/") for case in cases)


@pytest.mark.parametrize("split", ["validation", "heldout"])
def test_validation_splits_have_five_positive_negative_and_control_cases(
    fixture_data, split
):
    cases = [case for case in experiment.expand_cases(fixture_data) if case["split"] == split]
    assert Counter(case["category"] for case in cases) == Counter({
        "ordinary_positive": 5,
        "negative": 5,
        "existing_route_control": 5,
    })


def test_families_do_not_cross_splits_and_negative_cases_have_no_applicable_label(fixture_data):
    by_family = {}
    for case in experiment.expand_cases(fixture_data):
        by_family.setdefault(case["family"], set()).add(case["split"])
        if case["category"] == "negative":
            assert case["applicable"] == []
    assert all(len(splits) == 1 for splits in by_family.values())


def test_hollowed_or_cross_split_fixture_is_rejected(fixture_data):
    invalid = copy.deepcopy(fixture_data)
    invalid["scenario_groups"][0]["cases"].pop()
    with pytest.raises(experiment.FixtureError, match="30/15/15"):
        experiment.validate_fixture(invalid)

    invalid = copy.deepcopy(fixture_data)
    invalid["scenario_groups"][2]["family"] = invalid["scenario_groups"][0]["family"]
    with pytest.raises(experiment.FixtureError, match="cannot cross"):
        experiment.validate_fixture(invalid)

    invalid = copy.deepcopy(fixture_data)
    invalid["scenario_groups"][0]["review_status"] = "self_certified"
    with pytest.raises(experiment.FixtureError, match="unknown review status"):
        experiment.validate_fixture(invalid)

    invalid = copy.deepcopy(fixture_data)
    invalid["review_provenance"]["independent_review_complete"] = True
    with pytest.raises(experiment.FixtureError, match="blinded labels"):
        experiment.validate_fixture(invalid)

    invalid = copy.deepcopy(fixture_data)
    invalid["scenario_coverage"]["negation"] = {
        "fixture_cases": [], "regression_tests": [],
    }
    with pytest.raises(experiment.FixtureError, match="coverage cannot be empty"):
        experiment.validate_fixture(invalid)

    invalid = copy.deepcopy(fixture_data)
    del invalid["repositories"][0]["decisions"][0]["subtype"]
    with pytest.raises(experiment.FixtureError, match="decision missing subtype"):
        experiment.validate_fixture(invalid)

    invalid = copy.deepcopy(fixture_data)
    invalid["validators"]["payment_retry"]["conditions"][0]["check_id"] = (
        "payment.renamed"
    )
    with pytest.raises(experiment.FixtureError, match="differs from reviewer schema"):
        experiment.validate_fixture(invalid)

    invalid = copy.deepcopy(fixture_data)
    invalid["outcome_assignments"][0]["artifact"]["verification"]["import_path"] = (
        "../../outside"
    )
    with pytest.raises(experiment.FixtureError, match="must stay within"):
        experiment.validate_fixture(invalid)


def test_default_off_baseline_is_silent_and_existing_routes_are_unchanged(report):
    positives = [
        row for row in report["retrieval"]["cases"]
        if row["category"] == "ordinary_positive" and row["host"] != "cursor"
    ]
    assert positives
    assert all(not row["arms"]["baseline"]["text"] for row in positives)
    assert any(row["arms"]["candidate"]["text"] for row in positives)
    controls = [
        row for row in report["retrieval"]["cases"]
        if row["category"] == "existing_route_control"
        and row["host"] != "cursor" and not row["expected_silence"]
    ]
    assert controls
    assert all(
        row["arms"]["candidate"]["text"] == row["arms"]["baseline"]["text"]
        for row in controls
    )


def test_new_task_guidance_is_silent_to_users_and_cursor_remains_unsupported(report):
    task_rows = [
        row for row in report["retrieval"]["cases"]
        if row["arms"]["candidate"]["meta"].get("origin") == experiment.VARIANT
    ]
    assert task_rows
    assert all(not row["arms"]["candidate"]["user_notice"] for row in task_rows)
    cursor_rows = [row for row in report["retrieval"]["cases"] if row["host"] == "cursor"]
    assert cursor_rows
    assert all(not row["arms"]["candidate"]["supported"] for row in cursor_rows)
    assert all(not row["arms"]["candidate"]["text"] for row in cursor_rows)


def test_hard_negatives_and_no_relevant_cases_do_not_emit(report):
    rows = report["retrieval"]["cases"]
    hard = [row for row in rows if row["hard_negative"]]
    negatives = [row for row in rows if row["category"] == "negative"]
    assert hard and negatives
    assert all(not row["arms"]["candidate"]["text"] for row in hard)
    assert all(not row["arms"]["candidate"]["text"] for row in negatives)


def _precision_row(case_id, family, applicable, emissions, *, reviewed=True):
    return {
        "case_id": case_id,
        "split": "heldout",
        "family": family,
        "repo_id": "synthetic/test",
        "host": "claude",
        "category": "ordinary_positive",
        "review_status": "independently_reviewed" if reviewed else "unreviewed_synthetic",
        "applicable": [
            {"decision_id": decision_id, "revision_id": revision_id,
             "required_tier": "prompt_full"}
            for decision_id, revision_id in applicable
        ],
        "expected_silence": False,
        "hard_negative": False,
        "arms": {
            "baseline": {"text": "", "full_emissions": []},
            "candidate": {"text": "context" if emissions else "", "full_emissions": [
                {"decision_id": decision_id, "revision_id": revision_id,
                 "tier": "prompt_full"}
                for decision_id, revision_id in emissions
            ]},
        },
    }


def test_precision_gate_rejects_one_applicable_plus_two_irrelevant_emissions():
    rows = [_precision_row(
        "counterexample", "family-a", [("right", "r1")],
        [("right", "r1"), ("wrong-a", "r1"), ("wrong-b", "r1")],
    )]
    precision = experiment.relevance_metrics(rows)["new_route_precision"]
    assert precision["pooled"] == pytest.approx(1 / 3)
    assert precision["equal_prompt_mean"] == pytest.approx(1 / 3)
    assert precision["gate"] == "fail"


def test_empty_precision_denominator_is_inconclusive():
    rows = [_precision_row("silent", "family-a", [("right", "r1")], [])]
    precision = experiment.relevance_metrics(rows)["new_route_precision"]
    assert precision["denominator"] == 0
    assert precision["pooled"] is None
    assert precision["equal_prompt_mean"] is None
    assert precision["gate"] == "inconclusive"


def test_unreviewed_emissions_keep_apparent_perfect_precision_inconclusive(report):
    precision = report["retrieval"]["metrics"]["new_route_precision"]
    assert precision["pooled"] == 1.0
    assert precision["equal_prompt_mean"] == 1.0
    assert precision["unresolved_emissions"]
    assert precision["gate"] == "inconclusive"


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"evidence_valid": False}, "unknown"),
        ({
            "evidence_valid": True, "invocation_status": "completed",
            "artifact_complete": True, "expected_check_ids": ["f", "c"],
            "checks": [
                {"check_id": "f", "kind": "functional", "status": "pass"},
                {"check_id": "c", "kind": "condition", "status": "fail"},
            ],
        }, "failure"),
        ({
            "evidence_valid": True, "invocation_status": "crashed",
            "artifact_complete": False, "expected_check_ids": ["f", "c", "u"],
            "checks": [
                {"check_id": "f", "kind": "functional", "status": "pass"},
                {"check_id": "c", "kind": "condition", "status": "fail"},
                {"check_id": "u", "kind": "condition", "status": "unchecked"},
            ],
        }, "failure"),
        ({
            "evidence_valid": True, "invocation_status": "completed",
            "artifact_complete": True, "expected_check_ids": ["f", "c"],
            "checks": [
                {"check_id": "f", "kind": "functional", "status": "pass"},
                {"check_id": "c", "kind": "condition", "status": "unchecked"},
            ],
        }, "unknown"),
        ({
            "evidence_valid": True, "invocation_status": "completed",
            "artifact_complete": True, "expected_check_ids": ["f", "c"],
            "checks": [
                {"check_id": "f", "kind": "functional", "status": "pass"},
                {"check_id": "c", "kind": "condition", "status": "pass"},
            ],
        }, "success"),
    ],
)
def test_aggregation_truth_table(row, expected):
    assert experiment.aggregate_outcome(row)["result"] == expected


def test_mutants_cannot_become_success(report):
    runs = {row["assignment_id"]: row for row in report["outcomes"]["runs"]}
    good = {assignment_id for assignment_id in runs if assignment_id.endswith("GOOD")}
    assert len(good) == 6
    assert all(runs[assignment_id]["aggregate"]["result"] == "success"
               for assignment_id in good)
    assert all(row["aggregate"]["result"] != "success"
               for assignment_id, row in runs.items()
               if assignment_id not in good and row["clarification"] == "not_applicable")


def test_behavioral_validator_executes_candidate_instead_of_trusting_claims(
    fixture_data, tmp_path
):
    assignment = copy.deepcopy(fixture_data["outcome_assignments"][0])
    assignment["assignment_id"] = "synthetic-broken-implementation"
    assignment["implementation_source"] = '''
REVISION = "v2"

def retry_plan(operation_id, attempts):
    return []
'''
    assignment["expected_aggregate"] = "failure"

    row = experiment.evaluate_stub_assignment(fixture_data, assignment, tmp_path)

    assert assignment["artifact"]["behavior"] == "retries_enabled"
    assert row["checks"][0] == {
        "check_id": "payment.functional", "kind": "functional", "status": "fail",
    }
    assert row["aggregate"]["result"] == "failure"
    assert row["validator"]["protocol"] == (
        "reviewer_owned_verdict_with_stateful_candidate_subprocess_v4"
    )


def test_candidate_cannot_replace_reviewer_probes_in_memory(fixture_data, tmp_path):
    assignment = copy.deepcopy(fixture_data["outcome_assignments"][0])
    assignment["assignment_id"] = "synthetic-reviewer-memory-tamper"
    assignment["implementation_source"] = '''
import __main__

__main__.PROBES["payment_retry"] = lambda module: (True, True, True, True)
'''
    assignment["expected_aggregate"] = "failure"

    row = experiment.evaluate_stub_assignment(fixture_data, assignment, tmp_path)

    assert row["checks"][0] == {
        "check_id": "payment.functional", "kind": "functional", "status": "fail",
    }
    assert row["aggregate"]["result"] == "failure"
    assert row["evidence_valid"] is True


def test_stateful_candidate_is_reused_for_idempotency_probe(fixture_data, tmp_path):
    assignment = copy.deepcopy(fixture_data["outcome_assignments"][0])
    assignment["assignment_id"] = "synthetic-stateful-non-idempotent"
    assignment["implementation_source"] = '''
REVISION = "v2"
calls = 0

def retry_plan(operation_id, attempts):
    global calls
    calls += 1
    key = operation_id if calls == 1 else f"{operation_id}-{calls}"
    return [
        {"attempt": attempt, "delay": min(2 ** attempt, 8), "idempotency_key": key}
        for attempt in range(attempts)
    ]
'''
    assignment["expected_aggregate"] = "failure"

    row = experiment.evaluate_stub_assignment(fixture_data, assignment, tmp_path)
    checks = {check["check_id"]: check["status"] for check in row["checks"]}

    assert checks["payment.functional"] == "pass"
    assert checks["payment.idempotent"] == "fail"
    assert row["aggregate"]["result"] == "failure"


def test_fixture_import_path_is_rejected_before_any_out_of_root_write(
    fixture_data, tmp_path
):
    assignment = copy.deepcopy(fixture_data["outcome_assignments"][0])
    assignment["assignment_id"] = "synthetic-import-path-escape"
    outside = tmp_path / "outside"
    assignment["artifact"]["verification"]["import_path"] = str(outside)

    with pytest.raises(experiment.FixtureError, match="must stay within"):
        experiment.evaluate_stub_assignment(
            fixture_data, assignment, tmp_path / "evaluation",
        )

    assert not outside.exists()


def test_protected_boundary_detects_tampering_missing_results_and_evaluator_failure(report):
    runs = {row["assignment_id"]: row for row in report["outcomes"]["runs"]}
    assert runs["O-AUT-VERIFIER-TAMPER"]["aggregate"] == {
        "result": "unknown", "reason": "invalid_evidence",
    }
    assert "missing_expected_result" in runs["O-CAC-MISSING-RESULT"]["invalid_reasons"]
    assert runs["O-PAY-EVALUATOR-ERROR"]["aggregate"]["result"] == "unknown"


def test_protected_boundary_rejects_symlink_escape(fixture_data, tmp_path):
    assignment = copy.deepcopy(fixture_data["outcome_assignments"][0])
    assignment["assignment_id"] = "synthetic-path-escape"
    assignment["artifact_escape"] = True
    assignment["expected_aggregate"] = "unknown"

    row = experiment.evaluate_stub_assignment(fixture_data, assignment, tmp_path)

    assert row["aggregate"] == {"result": "unknown", "reason": "invalid_evidence"}
    assert "artifact_path_escape" in row["invalid_reasons"]


@pytest.mark.parametrize(
    ("mutation", "reason"),
    [
        ("duplicate_check", "missing_expected_result"),
        ("mislabel_check_kind", "result_kind_mismatch"),
    ],
)
def test_result_collector_rejects_duplicate_or_mislabeled_results(
    fixture_data, tmp_path, mutation, reason
):
    assignment = copy.deepcopy(fixture_data["outcome_assignments"][0])
    assignment["assignment_id"] = f"synthetic-{mutation}"
    assignment[mutation] = True
    assignment["expected_aggregate"] = "unknown"

    row = experiment.evaluate_stub_assignment(
        fixture_data, assignment, tmp_path / mutation)

    assert row["aggregate"] == {"result": "unknown", "reason": "invalid_evidence"}
    assert reason in row["invalid_reasons"]


def test_required_condition_coverage_uses_frozen_manifest(fixture_data, tmp_path):
    assignment = copy.deepcopy(next(
        row for row in fixture_data["outcome_assignments"]
        if row["assignment_id"] == "O-CAC-MISSING-RESULT"
    ))
    row = experiment.evaluate_stub_assignment(fixture_data, assignment, tmp_path)

    coverage = experiment.outcome_metrics([row])["required_condition_coverage"]

    assert coverage == {"numerator": 4, "denominator": 5, "ratio": 0.8}


def test_invalid_verifier_evidence_cannot_inflate_coverage(fixture_data, tmp_path):
    assignment = copy.deepcopy(next(
        row for row in fixture_data["outcome_assignments"]
        if row["assignment_id"] == "O-AUT-VERIFIER-TAMPER"
    ))
    row = experiment.evaluate_stub_assignment(fixture_data, assignment, tmp_path)

    coverage = experiment.outcome_metrics([row])["required_condition_coverage"]

    assert coverage == {"numerator": 0, "denominator": 5, "ratio": 0.0}


def test_failed_task_with_unchecked_condition_is_not_complete(fixture_data, tmp_path):
    assignment = copy.deepcopy(fixture_data["outcome_assignments"][0])
    assignment["assignment_id"] = "synthetic-failure-and-unchecked"
    assignment["implementation_source"] = '''
REVISION = "v2"

def retry_plan(operation_id, attempts):
    return []
'''
    assignment["unchecked_condition"] = True
    assignment["expected_aggregate"] = "failure"
    row = experiment.evaluate_stub_assignment(fixture_data, assignment, tmp_path)

    metrics = experiment.outcome_metrics([row])

    assert row["aggregate"]["result"] == "failure"
    assert metrics["tasks_with_complete_verification"] == 0


def test_all_assigned_tasks_remain_in_primary_denominator(report):
    metrics = report["outcomes"]["metrics"]
    assert metrics["assigned_implementable"] == 20
    assert metrics["implementation_reconciliation"] == 20
    assert metrics["assigned_total"] == 23
    assert metrics["assigned_reconciliation"] == 23
    assert metrics["success"] + metrics["failure"] + metrics["unknown"] == 20
    assert metrics["conservative_success_rate"] == 6 / 20
    assert metrics["upper_sensitivity_bound"] == 11 / 20
    assert metrics["clarification"] == {
        "assigned": 3, "appropriate": 1, "inappropriate": 1, "unverified": 1,
    }


def test_paired_stub_runs_and_manifests_are_complete(report):
    rows = report["outcomes"]["paired_stub_runs"]
    assert len(rows) == 12
    pairs = {}
    for row in rows:
        pairs.setdefault(row["task_id"], {})[row["arm"]] = row
        assert row["repo_id"].startswith("synthetic/")
        assert row["repetition"] == 1
        assert row["source_sha256"]
        assert row["fixture_sha256"]
        assert row["split_sha256"]
        assert row["validator"]["sha256"]
        assert row["validator"]["candidate_modules"]
        assert row["input_tokens"] == row["output_tokens"] == 0
    assert all(set(pair) == {"baseline", "candidate"} for pair in pairs.values())
    assert all(pair["baseline"]["aggregate"]["result"] == "failure"
               for pair in pairs.values())
    assert all(pair["candidate"]["aggregate"]["result"] == "success"
               for pair in pairs.values())


def test_retrieval_report_exposes_stages_and_required_strata(report):
    assert set(report["retrieval"]["breakdowns"]) == {"repo_id", "host", "family"}
    for row in report["retrieval"]["cases"]:
        assert set(row["stages"]) == {"baseline", "candidate"}
        assert row["arms"]["candidate"]["router_context_in_payload"] is True
        assert row["stages"]["candidate"]["selected_revisions"] == \
            row["stages"]["candidate"]["emitted_revisions"]


def test_report_is_explicitly_offline_unreviewed_and_not_release_evidence(report):
    assert report["provenance"]["synthetic"] is True
    assert report["provenance"]["live_agent_invocations"] == 0
    assert report["provenance"]["independent_validation_review_complete"] is False
    isolation = report["provenance"]["isolation"]
    assert isolation["distinct_temporary_home_per_arm"] is True
    assert isolation["distinct_store_and_session_state_per_arm"] is True
    assert isolation["out_of_scope_sentinel_verified"] is True
    assert isolation["remote_services_enabled"] is False
    assert report["statuses"] == {
        "engineering_ready": True,
        "pilot_eligible": False,
        "pilot_reason": "independent validation and holdout review incomplete",
        "release_accepted": False,
        "release_reason": "live comparative outcomes and Contract 05 timing remain open",
    }


def test_experiment_restores_process_home(fixture_data, monkeypatch):
    original = "/synthetic/caller-home"
    monkeypatch.setenv("HOME", original)

    experiment.build_report(fixture_data)

    assert os.environ["HOME"] == original


def test_report_is_deterministic_except_runtime_observations(fixture_data):
    left = experiment.build_report(fixture_data)
    right = experiment.build_report(fixture_data)
    for report in (left, right):
        report["runtime"] = {}
        for case in report["retrieval"]["cases"]:
            for arm in case["arms"].values():
                arm["elapsed_ms"] = 0
        for row in report["outcomes"]["runs"] + report["outcomes"]["paired_stub_runs"]:
            row["latency_ms"] = 0
    assert left == right


def test_cli_writes_only_to_an_explicit_output(tmp_path, capsys):
    before = set(tmp_path.iterdir())
    assert experiment.main(["--format", "text"]) == 0
    assert set(tmp_path.iterdir()) == before
    assert "live agent invocations: 0" in capsys.readouterr().out
    output = tmp_path / "report.json"
    assert experiment.main(["--format", "json", "--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["schema_version"] == 1
