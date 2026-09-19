"""Correctness and integrity gates for the dedicated prompt-capture benchmark."""

from copy import deepcopy

import pytest

from benchmarks.prompt_capture import run


FIXTURE = run.load_fixture()
REPORT = run.run_benchmark(FIXTURE)
GAPS = {item["assertion_id"]: item for item in FIXTURE["known_gaps"]}


def _params():
    params = []
    for assertion in REPORT["assertions"]:
        marks = []
        if assertion["assertion_id"] in GAPS:
            marks.append(pytest.mark.xfail(
                strict=True, raises=AssertionError,
                reason=GAPS[assertion["assertion_id"]]["rationale"],
            ))
        params.append(pytest.param(assertion, id=assertion["assertion_id"], marks=marks))
    return params


@pytest.mark.parametrize("assertion", _params())
def test_prompt_capture_assertion(assertion):
    if assertion["error"]:
        raise RuntimeError(assertion["error"])
    assert assertion["actual"] == assertion["expected"]


def test_default_gate_has_no_unexpected_result():
    assert run.gate_failures(REPORT) == []


def test_fixture_pins_all_families_and_required_seeds():
    assert set(REPORT["families"]) == run.FAMILIES
    assert REPORT["efficiency"]["cases"] >= 60
    assert set(FIXTURE["required_case_ids"]).issubset(
        {case["case_id"] for case in FIXTURE["cases"]})


def test_fixture_rejects_a_removed_required_case():
    fixture = deepcopy(FIXTURE)
    required = fixture["required_case_ids"][0]
    fixture["cases"] = [case for case in fixture["cases"] if case["case_id"] != required]
    with pytest.raises(run.FixtureError):
        run.validate_fixture(fixture)


def test_fixture_rejects_unknown_or_duplicate_gap_assertions():
    fixture = deepcopy(FIXTURE)
    gap = {
        "assertion_id": "missing:assertion", "category": "recall",
        "baseline": "deadbeef", "rationale": "synthetic mutation", "owner": "benchmark-plan",
    }
    fixture["known_gaps"] = [gap, dict(gap)]
    with pytest.raises(run.FixtureError):
        run.validate_fixture(fixture)


def test_fixture_rejects_expected_failure_on_safety_assertion():
    fixture = deepcopy(FIXTURE)
    fixture["known_gaps"] = [{
        "assertion_id": "P08-human-proposal:live_content", "category": "safety",
        "baseline": "deadbeef", "rationale": "synthetic mutation", "owner": "benchmark-plan",
    }]
    with pytest.raises(run.FixtureError, match="safety assertions"):
        run.validate_fixture(fixture)


def test_xpass_is_a_default_gate_failure():
    report = {"assertions": [{"assertion_id": "case:field", "outcome": "xpass"}]}
    assert run.gate_failures(report) == ["case:field"]


def test_strict_mode_rejects_known_gaps():
    report = {"assertions": [{"assertion_id": "case:field", "outcome": "known_gap"}]}
    assert run.gate_failures(report) == []
    assert run.gate_failures(report, strict=True) == ["case:field"]


def test_mutated_target_oracle_is_an_unexpected_failure():
    fixture = deepcopy(FIXTURE)
    case = next(case for case in fixture["cases"] if case["case_id"] == "P07-unique-subject")
    case["expected"]["target"] = "wrong-target"
    report = run.run_benchmark(fixture)
    assert "P07-unique-subject:target" in run.gate_failures(report)


def test_reversed_scenario_order_has_identical_semantics():
    fixture = deepcopy(FIXTURE)
    fixture["cases"].reverse()
    reversed_report = run.run_benchmark(fixture)
    expected = {item["assertion_id"]: (item["actual"], item["outcome"])
                for item in REPORT["assertions"]}
    actual = {item["assertion_id"]: (item["actual"], item["outcome"])
              for item in reversed_report["assertions"]}
    assert actual == expected


@pytest.mark.perf
def test_prompt_capture_benchmark_efficiency_smoke():
    """Fixed-hardware smoke guard; CI and coverage intentionally skip perf markers."""
    reports = [run.run_benchmark(FIXTURE) for _ in range(5)]
    slowest = max(report["efficiency"]["elapsed_ms"] for report in reports)
    assert slowest < 2000
    assert min(report["efficiency"]["cases_per_second"] for report in reports) > 30
