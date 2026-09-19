import copy
import importlib.util
import json
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
BENCHMARK_PATH = ROOT / "benchmarks" / "applicability" / "working_set_lookup.py"
MANIFEST_PATH = BENCHMARK_PATH.with_name("working_set_lookup_cases.json")


def _benchmark_module():
    spec = importlib.util.spec_from_file_location("working_set_lookup_benchmark", BENCHMARK_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _report(median: float, p95: float, behavior: str = "same",
            source: str = "source") -> dict:
    return {
        "manifest_sha256": "manifest",
        "fixture_sha256": "fixture",
        "benchmark_sha256": "benchmark",
        "lock_sha256": "lock",
        "source_lock_sha256": f"{source}-lock",
        "source_tree_sha256_before": f"{source}-tree",
        "source_tree_sha256_after": f"{source}-tree",
        "source_sha256": {
            "store": f"{source}-store",
            "working_set": f"{source}-working-set",
        },
        "results": [{
            "name": "dense_full",
            "median_ms": median,
            "p95_nearest_rank_ms": p95,
            "output_count": 1,
            "output_bytes": 10,
            "output_sha256": "output",
            "meta_sha256": "meta",
            "state_sha256": "state",
            "behavior_sha256": behavior,
            "ledger_size": 500,
            "probes": 1000,
            "route_probes": {"file": 0, "ranked": 500, "topic": 500, "other": 0},
        }],
    }


def _pairs(left: tuple[float, float], right: tuple[float, float]) -> list[dict]:
    return [{
        "a": _report(*left),
        "b": _report(*right),
    } for _ in range(5)]


def test_manifest_freezes_required_routes_queries_and_expected_behavior():
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    cases = {case["name"]: case for case in manifest["cases"]}

    assert manifest["fixture_sha256"]
    assert {
        "file_combined", "file_global_anchor", "file_mention_only", "no_session_repeated",
        "compaction_then_omitted", "diagnostics_on_first_relevant", "missing_index_relevant",
        "missing_index_irrelevant", "corrupt_index_relevant", "corrupt_index_irrelevant",
    } <= cases.keys()
    assert cases["no_session_repeated"]["repetitions"] == 2
    assert cases["compaction_then_omitted"]["sequence"] == "compaction_prompt"
    assert set(manifest["expected_behaviors"]) == set(cases)
    assert all(case["query_sha256"] for case in cases.values())
    assert all(case["expected_route_probes"] for case in cases.values())


def test_exact_parity_rejects_same_length_behavior_change():
    benchmark = _benchmark_module()
    left = _report(10.0, 11.0)
    right = copy.deepcopy(left)
    right["results"][0]["behavior_sha256"] = "different"

    with pytest.raises(AssertionError, match="behavior_sha256 mismatch"):
        benchmark._assert_reports_compatible([left, right])


def test_timed_invoke_uses_query_precomputed_with_fixture(monkeypatch):
    benchmark = _benchmark_module()
    observed = []

    monkeypatch.setattr(
        benchmark, "_query",
        lambda *_args: (_ for _ in ()).throw(AssertionError("query rebuilt during request")),
    )
    monkeypatch.setattr(
        benchmark.store, "get_context_for_prompt_with_meta",
        lambda repo, prompt, session_id: (
            observed.append((repo, prompt, session_id)) or "", {"kind": "", "count": 0}
        ),
    )

    outcome = benchmark._invoke(
        {"repo": "/fixture", "queries": {"closed": "precomputed query"}},
        {"ledger": "no_session", "query": "closed"},
    )

    assert observed == [("/fixture", "precomputed query", "")]
    assert outcome == {"responses": [{
        "kind": "prompt", "text": "", "meta": {"kind": "", "count": 0},
    }]}


def test_calibration_comparison_and_drift_apply_predeclared_rules():
    benchmark = _benchmark_module()
    calibration_pairs = _pairs((10.0, 11.0), (10.01, 11.01))
    calibration = benchmark._calibration(calibration_pairs)

    assert calibration["dense_full"]["median_ms"]["stable"]
    assert calibration["dense_full"]["p95_nearest_rank_ms"]["stable"]

    comparison = benchmark._evaluate_comparison(
        _pairs((10.0, 11.0), (7.0, 8.0)), calibration,
    )
    assert comparison["dense_full"]["median_ms"]["status"] == "pass"
    assert comparison["dense_full"]["median_ms"]["dense_target_met"]
    assert comparison["dense_full"]["p95_nearest_rank_ms"]["dense_target_met"]
    assert not comparison["dense_full"]["median_ms"]["large_slowdown_alarm"]

    drift = benchmark._evaluate_drift(
        _pairs((10.0, 11.0), (10.005, 11.005)), calibration,
    )
    assert drift["dense_full"]["median_ms"]["status"] == "pass"
    assert drift["dense_full"]["p95_nearest_rank_ms"]["status"] == "pass"


def test_comparison_large_slowdown_alarm_is_independent_of_allowance():
    benchmark = _benchmark_module()
    calibration = benchmark._calibration(_pairs((10.0, 11.0), (10.01, 11.01)))
    comparison = benchmark._evaluate_comparison(
        _pairs((10.0, 11.0), (10.25, 11.25)), calibration,
    )

    assert comparison["dense_full"]["median_ms"]["status"] == "fail"
    assert comparison["dense_full"]["median_ms"]["large_slowdown_alarm"]


def test_full_campaign_stops_before_candidate_timing_when_calibration_is_noisy(monkeypatch):
    benchmark = _benchmark_module()
    calls = []

    def noisy_pair(*_args, **kwargs):
        calls.append(kwargs["phase"])
        return {"a": _report(10.0, 11.0), "b": _report(10.2, 11.2)}

    monkeypatch.setattr(benchmark, "_pair", noisy_pair)
    report = benchmark.campaign(
        base_root=ROOT,
        candidate_root=ROOT,
        warmup=20,
        samples=100,
        pairs=5,
        selected=None,
    )

    assert calls == ["calibration"] * 5
    assert report["failure"] == "noisy_calibration"
    assert report["comparison"] is None and report["drift"] is None
    assert not report["accepted"]


def test_campaign_rejects_base_source_change_between_batches(monkeypatch):
    benchmark = _benchmark_module()
    calls = []

    def changing_pair(*_args, **kwargs):
        calls.append(kwargs["phase"])
        source = "base-changed" if kwargs["pair_index"] == 2 else "base-original"
        return {
            "a": _report(10.0, 11.0, source=source),
            "b": _report(10.01, 11.01, source=source),
        }

    monkeypatch.setattr(benchmark, "_pair", changing_pair)
    with pytest.raises(
        AssertionError, match="base source identity changed between benchmark batches"
    ):
        benchmark.campaign(
            base_root=ROOT,
            candidate_root=ROOT,
            warmup=20,
            samples=100,
            pairs=5,
            selected=None,
        )

    assert calls == ["calibration"] * 5
