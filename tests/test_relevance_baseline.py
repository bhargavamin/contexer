"""Contract tests for the deterministic decision-relevance baseline."""

from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from benchmarks.applicability import relevance_baseline as baseline
from contexer import store


@pytest.fixture(scope="module")
def fixture_data():
    return baseline.load_fixture()


@pytest.fixture(scope="module")
def report(fixture_data):
    return baseline.build_report(fixture_data)


def _case(report, family):
    return next(case for case in report["cases"] if case["family"] == family)


def _assertion(report, family, key):
    case = _case(report, family)
    return next(item for item in case["assertions"] if item["key"] == key)


def test_fixture_covers_all_eighteen_families(fixture_data):
    assert {case["family"] for case in fixture_data["cases"]} == {
        f"R{i:02d}" for i in range(1, 19)
    }
    assert len(fixture_data["cases"]) == 18


def test_fixture_has_stable_identity_and_applicability_fields(fixture_data):
    for case in fixture_data["cases"]:
        assert case["case_id"]
        assert case["repo_id"].startswith("synthetic/")
        assert not Path(case["repo_id"]).is_absolute()
        for decision in case.get("decisions", []) + case.get("global_decisions", []):
            assert decision["decision_id"]
            assert decision["revision_id"]
            assert decision["timestamp"] == "2026-01-15T12:00:00+00:00"
        for label in case["applicable"]:
            assert label["required_tier"] in baseline.TIERS
            assert label["authority"]


def test_report_schema_and_version_provenance(report):
    assert report["schema_version"] == 1
    assert len(report["code_revision"]) == 40
    assert len(report["fixture_sha256"]) == 64
    assert len(report["runner_sha256"]) == 64
    assert report["fixture_version"] == "1.0.4"
    assert report["runner_version"] == "2"


def test_only_registered_gaps_remain(report):
    assert report["summary"]["unexpected_failures"] == []
    assert {item["gap"] for item in report["summary"]["known_gaps"]} == {
        "ordinary-task-trigger-gap",
        "revision-id-working-set-dedup",
    }


@pytest.mark.parametrize(
    ("family", "key"),
    [
        pytest.param(
            "R01", "imperative-supplies-payment-decision",
            marks=pytest.mark.xfail(strict=True, raises=AssertionError,
                                    reason="ordinary-task-trigger-gap; later experiment"),
        ),
        pytest.param(
            "R02", "same-session-receives-current-revision",
            marks=pytest.mark.xfail(strict=True, raises=AssertionError,
                                    reason="revision-id-working-set-dedup; revision contract"),
        ),
    ],
)
def test_desired_behavior_known_gaps_are_narrow(report, family, key):
    assert _assertion(report, family, key)["status"] == "passed"


def test_text_and_json_render_from_the_same_report(report):
    encoded = json.dumps(report, sort_keys=True)
    assert json.loads(encoded) == report
    text = baseline.render_text(report)
    assert f"cases: {report['summary']['assigned_cases']}" in text
    assert f"known gaps: {len(report['summary']['known_gaps'])}" in text


def test_two_fresh_runs_are_deterministic(fixture_data):
    assert baseline.build_report(fixture_data) == baseline.build_report(fixture_data)


def test_case_order_does_not_change_results(fixture_data):
    assert baseline.build_report(fixture_data) == baseline.build_report(fixture_data, reverse=True)


def test_invalid_fixture_fails_before_execution(fixture_data):
    invalid = copy.deepcopy(fixture_data)
    invalid["cases"].pop()
    with pytest.raises(baseline.FixtureError, match="R01-R18"):
        baseline.validate_fixture(invalid)


@pytest.mark.parametrize("invalid_id", [None, 7, "", "   "])
def test_invalid_case_ids_are_rejected(fixture_data, invalid_id):
    invalid = copy.deepcopy(fixture_data)
    invalid["cases"][0]["case_id"] = invalid_id
    with pytest.raises(baseline.FixtureError, match="unique non-empty strings"):
        baseline.validate_fixture(invalid)


def test_strict_mode_fails_while_known_gaps_remain(report):
    assert baseline.exit_code(report) == 0
    assert baseline.exit_code(report, strict=True) == 1


def test_zero_denominators_are_null():
    metrics = baseline._metrics([{
        "case_id": "empty", "host": "synthetic", "applicable": [],
        "observations": [], "outcomes": [],
    }])
    assert metrics["full_guidance_precision"]["ratio"] is None
    assert metrics["required_guidance_coverage"]["ratio"] is None
    assert metrics["whole_case_coverage"]["supported"]["ratio"] is None


@pytest.mark.parametrize(
    ("evidence_id", "expected"),
    [
        ("pass", "verified_compliant"),
        ("violation", "violation_observed"),
        ("partial-allow", "unknown"),
        ("unrelated-pass", "unknown"),
        ("model-claim", "unknown"),
        ("missing-identity", "unknown"),
        ("wrong-policy", "unknown"),
        ("missing-validator-version", "unknown"),
        ("clarification", "clarification_appropriate"),
        ("clarification-missing-identity", "unknown"),
    ],
)
def test_outcome_evidence_classification(report, evidence_id, expected):
    outcomes = _case(report, "R18")["outcomes"]
    actual = next(item for item in outcomes if item["evidence_id"] == evidence_id)
    assert actual["result"] == expected
    assert actual["scope"]


def test_cursor_is_unsupported_not_empty_success(report):
    cursor = next(
        obs for obs in _case(report, "R15")["observations"]
        if obs["action_id"] == "cursor"
    )
    assert cursor["status"] == "unsupported"
    assert cursor["reason"] == "host_unsupported"


def test_precision_uses_action_level_host_and_surface(report):
    metrics = baseline._metrics([_case(report, "R15")])
    precision = metrics["full_guidance_precision"]
    assert set(precision["by_host"]) == {"claude", "codex", "gemini"}
    assert set(precision["by_surface"]) == {
        "claude_rationale_hook", "codex_rationale_hook", "gemini_before_agent_hook",
    }
    assert all(row == {"numerator": 1, "denominator": 1, "ratio": 1.0}
               for row in precision["by_host"].values())


def test_unsupported_requests_do_not_lower_supported_coverage(report):
    case = copy.deepcopy(_case(report, "R15"))
    cursor = next(obs for obs in case["observations"] if obs["action_id"] == "cursor")
    case["delivery_expectations"].append({
        "action_id": "cursor", "session_id": cursor["session_id"],
        "request_id": cursor["request_id"], "host": cursor["host"],
        "surface": cursor["surface"], "decision_id": "r15-parity",
        "revision_id": "rev-r15-parity-a", "proposal_id": None,
        "required_tier": "prompt_full", "authority": "approved",
    })

    metrics = baseline._metrics([case])
    coverage = metrics["required_guidance_coverage"]
    assert (coverage["numerator"], coverage["denominator"], coverage["ratio"]) == (3, 3, 1.0)
    cursor_row = next(row for row in coverage["by_request"] if row["action_id"] == "cursor")
    assert cursor_row["supported"] is False
    assert (cursor_row["numerator"], cursor_row["denominator"]) == (0, 1)
    assert metrics["whole_case_coverage"]["supported"]["denominator"] == 0
    assert metrics["whole_case_coverage"]["all_assigned_conservative"] == {
        "numerator": 0, "denominator": 1, "ratio": 0.0,
    }


def test_pending_and_proposal_authority_are_preserved(report):
    assert _assertion(report, "R11", "pending-authority-labeled")["status"] == "passed"
    assert _assertion(report, "R13", "proposal-authority-labeled")["status"] == "passed"
    assert report["summary"]["metrics"]["authority_errors"] == 0


def test_authority_metric_counts_a_missing_rendered_pending_label(fixture_data, monkeypatch):
    case = copy.deepcopy(next(case for case in fixture_data["cases"] if case["family"] == "R11"))
    real_prompt = store.get_context_for_prompt_with_meta

    def without_pending_label(*args, **kwargs):
        text, meta = real_prompt(*args, **kwargs)
        return text.replace(" [pending]", ""), meta

    monkeypatch.setattr(store, "get_context_for_prompt_with_meta", without_pending_label)
    result = baseline.run_case(case)
    authority = next(
        item for item in result["assertions"] if item["key"] == "pending-authority-labeled"
    )
    assert authority["status"] == "failed"
    assert baseline._metrics([result])["authority_errors"] == 1


def test_metrics_count_delivery_per_request_not_candidates(report):
    coverage = report["summary"]["metrics"]["required_guidance_coverage"]
    rows = {(row["case_id"], row["action_id"]): row for row in coverage["by_request"]}
    assert rows[("R17-cap-and-candidate-coverage", "prompt")]["numerator"] == 0
    assert rows[("R17-cap-and-candidate-coverage", "prompt")]["denominator"] == 5
    assert rows[("R01-imperative-vs-rationale", "imperative")]["numerator"] == 0
    assert rows[("R01-imperative-vs-rationale", "rationale")]["numerator"] == 1
    assert rows[("R02-revision-working-set", "revision-b-same-session")]["numerator"] == 0
    assert rows[("R02-revision-working-set", "revision-b-fresh-session")]["numerator"] == 1


def test_every_observation_has_request_and_session_identity(report):
    for case in report["cases"]:
        for observation in case["observations"]:
            assert observation["session_id"]
            assert observation["request_id"]
            assert observation["host"]
            assert observation["surface"]


def test_r03_uses_production_title_and_content_ranker(report):
    case = _case(report, "R03")
    lookups = {item["action_id"]: item for item in case["action_details"]}
    assert lookups["uncapped"]["prompt_rank_calls"] == 1
    assert lookups["capped"]["prompt_rank_calls"] == 1
    assert lookups["uncapped"]["ranked_ids"][:2] == ["r03-best", "r03-incidental"]
    assert _assertion(report, "R03", "capped-retains-relevant-winner")["status"] == "passed"


def test_approved_revision_identity_comes_from_rendered_store_state(report):
    case = _case(report, "R02")
    approval = next(item for item in case["action_details"] if item["action_id"] == "approve-b")
    assert approval["current_revision_id"] == "00000000-0000-0000-0000-00000000002b"
    assert approval["proposal_id"] is None
    fresh = [
        observation for observation in case["observations"]
        if observation["action_id"] == "revision-b-fresh-session"
        and observation["decision_id"] == "r02-revision"
    ]
    assert {item["revision_id"] for item in fresh} == {approval["current_revision_id"]}
    assert all(item["proposal_id"] is None for item in fresh)
    assert all(item["revision_id"] != "fixture-only-rev-r02-b" for item in fresh)


def test_missing_index_is_observed_not_rebuilt(report):
    assert _assertion(report, "R16", "prompt-does-not-rebuild-missing-index")["status"] == "passed"
    assert _assertion(report, "R16", "prompt-leaves-corrupt-index")["status"] == "passed"


def test_perturbing_r02_expected_revision_fails(fixture_data):
    case = copy.deepcopy(next(case for case in fixture_data["cases"] if case["family"] == "R02"))
    assertion = next(item for item in case["desired_assertions"]
                     if item["key"] == "fresh-session-receives-current-revision")
    assertion["revision_id"] = "rev-r02-wrong"
    result = baseline.run_case(case)
    changed = next(item for item in result["assertions"] if item["key"] == assertion["key"])
    assert changed["status"] == "failed"


def test_perturbing_r03_expected_winner_fails(fixture_data):
    case = copy.deepcopy(next(case for case in fixture_data["cases"] if case["family"] == "R03"))
    assertion = next(item for item in case["desired_assertions"]
                     if item["key"] == "bm25-candidate-winner")
    assertion["decision_ids"].reverse()
    result = baseline.run_case(case)
    changed = next(item for item in result["assertions"] if item["key"] == assertion["key"])
    assert changed["status"] == "failed"


def test_perturbing_r03_displayed_winner_is_unexpected_failure(fixture_data):
    case = copy.deepcopy(next(case for case in fixture_data["cases"] if case["family"] == "R03"))
    assertion = next(item for item in case["desired_assertions"]
                     if item["key"] == "capped-retains-relevant-winner")
    assertion["decision_id"] = "r03-incidental"
    assertion["revision_id"] = "rev-r03-incidental-a"

    result = baseline.run_case(case)

    changed = next(item for item in result["assertions"] if item["key"] == assertion["key"])
    assert changed["status"] == "failed"
    assert changed["known_gap"] is None
    failures = [item for item in result["assertions"] if item["status"] == "failed"]
    assert [item["key"] for item in failures] == [assertion["key"]]


def test_supplied_fixture_data_controls_report_hash(fixture_data):
    changed = copy.deepcopy(fixture_data)
    changed["fixture_version"] = "1.0.0-test-variant"
    assert baseline.build_report(changed)["fixture_sha256"] != baseline.build_report(
        fixture_data
    )["fixture_sha256"]


def test_runner_restores_store_directory(fixture_data):
    before = store.store_dir
    baseline.run_case(fixture_data["cases"][0])
    assert store.store_dir is before


def test_main_writes_only_when_output_is_explicit(tmp_path, capsys):
    before = set(tmp_path.iterdir())
    assert baseline.main(["--format", "text"]) == 0
    assert set(tmp_path.iterdir()) == before
    assert "known gaps: 2" in capsys.readouterr().out
    output = tmp_path / "report.json"
    assert baseline.main(["--format", "json", "--output", str(output)]) == 0
    assert json.loads(output.read_text(encoding="utf-8"))["schema_version"] == 1
