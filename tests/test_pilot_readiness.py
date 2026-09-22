import copy
import json
import os
import sys
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import pytest

from benchmarks.applicability import pilot_readiness as pilot


HASH_A = "a" * 64
HASH_B = "b" * 64
HEAD = "c" * 40


@pytest.fixture
def _qualified_readiness_for_coordinator(monkeypatch):
    """Isolate coordinator state-machine tests from the evidence-gate fixture."""
    monkeypatch.setattr(pilot, "evaluate_readiness", lambda _path: {
        kind: {"status": "pass", "reasons": [], "metrics": {"test_fixture": True}}
        for kind in pilot.GATE_KINDS
    })


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _common_evidence(kind, *, checks=None):
    check_id, protocol = pilot.EVIDENCE_PROTOCOLS[kind]
    return {
        "schema_version": 1,
        "evidence_kind": kind,
        "integration_source": {"head": HEAD, "diff_sha256": HASH_A},
        "candidate_source_sha256": HASH_B,
        "measured_at": "2026-09-20T12:00:00+00:00",
        "checks": checks or [{
            "check_id": check_id,
            "evidence_class": "measured_live_prerequisite",
            "protocol": protocol,
            "artifact_sha256": HASH_A,
            "artifact_path": f"artifacts/{kind}.json",
            "validator_sha256": HASH_B,
        }],
    }


def _negative_payload(family_count=72):
    return {
        "negative_families": [
            {
                "family_id": f"negative-{index:03d}",
                "bundle_size": 1,
                "unexpected_full_injections": 0,
                "independent_draw": True,
                "sampling_distribution": "frozen-equal-family-v1",
            }
            for index in range(family_count)
        ],
        "hard_negative_errors": 0,
    }


def _qualification_measurements(family_count=72):
    positives = [{
        "case_id": f"positive-{index:03d}",
        "family_id": f"qualification-positive-{index:03d}",
        "category": "positive",
        "route": "task",
        "content_tier": "full",
        "expected_revisions": [f"revision-{index:03d}"],
        "baseline_revisions": [],
        "candidate_revisions": [f"revision-{index:03d}"],
    } for index in range(6)]
    negatives = [{
        "case_id": f"negative-{index:03d}",
        "family_id": f"negative-{index:03d}",
        "category": "negative",
        "route": "task",
        "content_tier": "full",
        "expected_revisions": [],
        "baseline_revisions": [],
        "candidate_revisions": [],
        "independent_draw": True,
        "sampling_distribution": "frozen-equal-family-v1",
        "hard_negative": index < 6,
    } for index in range(family_count)]
    observations = positives + negatives
    return {
        "label_manifest_sha256": HASH_A,
        "live_task_family_ids": ["family-0", "family-1", "family-2"],
        "relevance_observations": observations,
        "review_records": [{
            "case_id": row["case_id"],
            "reviewer_id": "reviewer-1",
            "rationale_sha256": HASH_A,
            "predictions_hidden": True,
            "implementation_reviewer": False,
            "exposure": "unexposed",
            "adjudication": "accepted",
        } for row in observations],
        "authority_regression_ids": [],
        "legacy_regression_ids": [],
        "new_task_recall_notice_ids": [],
    }


def _performance_measurements(kind):
    measurements = {
        "fixed_machine": {
            "machine_id": "fixed-test-host", "os": "test-os", "cpu": "test-cpu",
        },
        "coverage_instrumentation": False,
    }
    if kind == "contract05_performance":
        measurements["campaign"] = _contract05_campaign()
    if kind == "route_performance":
        measurements.update({
            "warm_hook_ms": [8.0] * 20,
            "cold_hook_ms": [12.0] * 5,
            "missing_index_hook_ms": [4.0] * 5,
            "requests_prepared_at": "2026-09-20T11:59:00+00:00",
            "measurement_started_at": "2026-09-20T12:00:00+00:00",
            "prompt_path_work": {
                "model": False, "network": False, "process": False,
                "git": False, "store_lock": False,
            },
            "context_bytes": 1200,
            "estimated_tokens": 300,
        })
    return measurements


def _contract05_arm(source, *, candidate=False):
    manifest = json.loads(pilot.CONTRACT05_MANIFEST_PATH.read_text(encoding="utf-8"))
    cases = {row["name"]: row for row in manifest["cases"]}
    results = []
    for name, case in cases.items():
        expected = manifest["expected_behaviors"][name]
        elapsed = 7.0 if candidate and name == "dense_full" else 10.0
        results.append({
            "name": name,
            "median_ms": elapsed,
            "p95_nearest_rank_ms": elapsed,
            "min_ms": elapsed,
            "max_ms": elapsed,
            "samples_ms": [elapsed] * manifest["samples_per_arm_per_batch"],
            "output_count": expected["output_count"],
            "output_bytes": expected["output_bytes"],
            "output_sha256": HASH_A,
            "meta_sha256": HASH_A,
            "state_sha256": HASH_A,
            "behavior_sha256": expected["behavior_sha256"],
            "ledger_size": expected["ledger_size"],
            "probes": case["expected_probes"],
            "route_probes": case["expected_route_probes"],
        })
    return {
        "manifest_sha256": pilot.sha256(pilot.CONTRACT05_MANIFEST_PATH),
        "fixture_sha256": manifest["fixture_sha256"],
        "benchmark_sha256": pilot.sha256(pilot.CONTRACT05_BENCHMARK_PATH),
        "lock_sha256": HASH_A,
        "source_lock_sha256": source,
        "source_tree_sha256_before": source,
        "source_tree_sha256_after": source,
        "source_sha256": {"store": source, "working_set": source},
        "git": {
            "head": HEAD,
            "diff_sha256": source,
            "status_sha256": HASH_A,
        },
        "warmup": manifest["warmup"],
        "samples_per_case": manifest["samples_per_arm_per_batch"],
        "results": results,
    }


def _contract05_campaign(*, candidate_dense_ms=7.0):
    manifest = json.loads(pilot.CONTRACT05_MANIFEST_PATH.read_text(encoding="utf-8"))
    phases = {"calibration": [], "comparison": [], "drift": []}
    for index in range(manifest["pairs_per_phase"]):
        order = ["a", "b"] if index % 2 == 0 else ["b", "a"]
        phases["calibration"].append({
            "pair": index + 1, "order": order,
            "a": _contract05_arm(HASH_A), "b": _contract05_arm(HASH_A),
        })
        candidate = _contract05_arm(HASH_B, candidate=True)
        for row in candidate["results"]:
            if row["name"] == "dense_full":
                row.update(
                    median_ms=candidate_dense_ms,
                    p95_nearest_rank_ms=candidate_dense_ms,
                    min_ms=candidate_dense_ms,
                    max_ms=candidate_dense_ms,
                    samples_ms=[candidate_dense_ms]
                    * manifest["samples_per_arm_per_batch"],
                )
        phases["comparison"].append({
            "pair": index + 1, "order": order,
            "a": _contract05_arm(HASH_A), "b": candidate,
        })
        phases["drift"].append({
            "pair": index + 1, "order": order,
            "a": _contract05_arm(HASH_A), "b": _contract05_arm(HASH_A),
        })
    return {
        "accepted": True,
        "settings": {
            "base_root": "/frozen/base",
            "candidate_root": "/frozen/candidate",
            "warmup": manifest["warmup"],
            "samples_per_arm_per_batch": manifest["samples_per_arm_per_batch"],
            "pairs_per_phase": manifest["pairs_per_phase"],
            "selected_cases": None,
        },
        "phases": phases,
    }


def _isolation_measurements():
    return {
        "profile": {
            "boundary_kind": "container",
            "profile_sha256": HASH_A,
            "candidate_credentials_present": False,
        },
        "probes": [{
            "probe_id": probe_id,
            "attempted": True,
            "expected": "pass",
            "observed": "pass",
            "mechanism": "reviewer-owned boundary probe",
        } for probe_id in sorted(pilot.ISOLATION_PROBES)],
    }


def _artifact(kind, measurements, *, mode="measured", synthetic=False, stub=False):
    return {
        "schema_version": 1,
        "artifact_kind": kind,
        "provenance": {
            "mode": mode,
            "synthetic": synthetic,
            "stub": stub,
            "producer": pilot.EVIDENCE_PROTOCOLS[kind][1],
            "producer_sha256": HASH_B,
            "integration_source": {"head": HEAD, "diff_sha256": HASH_A},
            "candidate_source_sha256": HASH_B,
            "collected_at": "2026-09-20T12:00:00+00:00",
            "run_id": f"{kind}-run-1",
        },
        "measurements": measurements,
    }


def _qualification_evidence():
    return _common_evidence("qualification")


def _performance_evidence(kind):
    return _common_evidence(kind)


def _isolation_evidence():
    return _common_evidence("isolation")


def _manifest(tmp_path, *, qualification=None, artifact_overrides=None):
    evidence_documents = {
        "qualification": qualification or _qualification_evidence(),
        "contract05_performance": _performance_evidence("contract05_performance"),
        "route_performance": _performance_evidence("route_performance"),
        "isolation": _isolation_evidence(),
    }
    evidence = {}
    for kind, document in evidence_documents.items():
        artifact = tmp_path / "artifacts" / f"{kind}.json"
        measurements = {
            "qualification": _qualification_measurements(),
            "contract05_performance": _performance_measurements(
                "contract05_performance"
            ),
            "route_performance": _performance_measurements("route_performance"),
            "isolation": _isolation_measurements(),
        }[kind]
        artifact_value = _artifact(kind, measurements)
        if artifact_overrides and kind in artifact_overrides:
            artifact_value = artifact_overrides[kind]
        _write_json(artifact, artifact_value)
        for check in document["checks"]:
            check["artifact_path"] = artifact.relative_to(tmp_path).as_posix()
            check["artifact_sha256"] = pilot.sha256(artifact)
            check["validator_sha256"] = HASH_B
        path = tmp_path / "evidence" / f"{kind}.json"
        _write_json(path, document)
        evidence[kind] = {
            "path": path.relative_to(tmp_path).as_posix(),
            "sha256": pilot.sha256(path),
        }
    tasks = [
        {
            "task_id": f"task-{index}",
            "repo_id": f"fixtures/repo-{index % 3}",
            "family_id": f"family-{index % 3}",
            "prompt_sha256": f"{index + 1:064x}",
            "validator_sha256": f"{index + 11:064x}",
            "required_checks": [f"task-{index}.behavior", f"task-{index}.condition"],
        }
        for index in range(6)
    ]
    manifest = {
        "schema_version": 1,
        "campaign_id": "contract07-test",
        "candidate_variant": "ordinary_task_v1",
        "development_only": True,
        "integration_source": {"head": HEAD, "diff_sha256": HASH_A},
        "candidate_source_sha256": HASH_B,
        "label_manifest_sha256": HASH_A,
        "evidence": evidence,
        "tasks": tasks,
        "repetitions": 2,
        "randomization_seed": 7007,
        "analysis": {
            "sampling_method": "family_cluster_percentile_bootstrap_v1",
            "sampling_seed": 17007,
            "resamples": 1000,
            "confidence": 0.95,
        },
        "canaries": {
            "ordinary_revision_id": "revision-ordinary",
            "legacy_revision_id": "revision-legacy",
            "ordinary_initially_standing": False,
            "ordinary_initially_working_set": False,
        },
        "budget": {
            "max_sessions": 26,
            "per_session_max_cost": 2.0,
            "max_total_cost": 52.0,
            "currency": "USD",
            "pricing_basis": "operator-supplied-test-pricing",
            "pricing_date": "2026-09-20T00:00:00+00:00",
            "concurrency": 1,
            "per_session_limit_enforced": True,
        },
    }
    path = tmp_path / "pilot-manifest.json"
    _write_json(path, manifest)
    return path, manifest


def _approval(tmp_path, manifest_path, manifest, ledger_path):
    path = tmp_path / "approval.json"
    approval = {
        "schema_version": 1,
        "approval_id": "approval-test-1",
        "campaign_id": manifest["campaign_id"],
        "manifest_sha256": pilot.sha256(manifest_path),
        "manifest_path": str(manifest_path.resolve()),
        "approval_path": str(path.resolve()),
        "ledger_path": str(ledger_path.resolve()),
        "operator_approved": True,
        "approved_at": datetime.now(timezone.utc).isoformat(),
        "expires_at": (datetime.now(timezone.utc) + timedelta(days=1)).isoformat(),
        "approved_run_ids": [row["run_id"] for row in pilot.frozen_schedule(manifest)],
        "budget": copy.deepcopy(manifest["budget"]),
        "host": "approved-host",
        "model": "approved-model",
        "destination_endpoints": ["https://approved.invalid/model"],
    }
    _write_json(path, approval)
    return path, approval


def _canaries(manifest):
    ordinary = manifest["canaries"]["ordinary_revision_id"]
    legacy = manifest["canaries"]["legacy_revision_id"]
    rows = []
    for arm in pilot.ARMS:
        rows.append({
            "arm": arm,
            "run_id": f"{manifest['campaign_id']}:canary:{arm}",
            "invocation_id": f"canary-invocation-{arm}",
            "hook_success": True,
            "session_interruptions": 0,
            "user_notices": 0,
            "prompts": [
                {
                    "kind": "ordinary",
                    "initial_standing": False,
                    "initial_working_set": False,
                    "received_revisions": [] if arm == "baseline" else [ordinary],
                    "task_route_receipt": arm == "candidate",
                    "host_receipt": arm == "candidate",
                    "tier": None if arm == "baseline" else "full",
                    "origin": None if arm == "baseline" else "ordinary_task_v1",
                    "authority_labels_valid": arm == "candidate",
                },
                {
                    "kind": "legacy",
                    "received_revisions": [legacy],
                    "origin": "legacy",
                    "host_receipt": True,
                    "suppressed_by_working_set": False,
                },
            ],
        })
    return rows


def _outcome(run_id, outcome):
    return {
        "run_id": run_id,
        "outcome": outcome,
        "invocation_complete": True,
        "artifact_complete": True,
        "checks": [{
            "check_id": "required",
            "status": "fail" if outcome == "failure" else "pass",
            "evidence_valid": True,
        }],
    }


def _complete_canaries(ledger_path, rows):
    for row in rows:
        pilot.launch_stubbed_run(
            ledger_path,
            row["run_id"],
            lambda run, row=row: {"invocation_id": row["invocation_id"]},
            lambda run, invocation, row=row: {
                "invocation_id": invocation["invocation_id"],
                "actual_cost": 1.0,
                "canary_evidence_sha256": pilot.digest(row),
            },
        )


def _simple_schedule(count):
    rows = []
    for index in range(count):
        for arm in pilot.ARMS:
            rows.append({
                "run_id": f"r{index}:{arm}",
                "kind": "task",
                "arm": arm,
                "repetition": 1,
                "task_id": f"task-{index}",
                "family_id": f"family-{index}",
                "repo_id": "repo",
                "required_checks": ["required"],
            })
    return rows


def test_manifest_freezes_26_unique_balanced_sessions(tmp_path):
    path, _ = _manifest(tmp_path)

    schedule = pilot.frozen_schedule(pilot.load_manifest(path))

    assert len(schedule) == 26
    assert len({row["run_id"] for row in schedule}) == 26
    assert sum(row["kind"] == "canary" for row in schedule) == 2
    assert Counter(row["arm"] for row in schedule) == {"baseline": 13, "candidate": 13}


@pytest.mark.parametrize("mutation, message", [
    (lambda manifest: manifest.update(development_only=False), "development_only"),
    (lambda manifest: manifest["tasks"].pop(), "six tasks"),
    (lambda manifest: manifest["budget"].update(max_sessions=25), "session ceiling"),
    (lambda manifest: manifest["budget"].update(max_total_cost=10), "reserve every session"),
    (lambda manifest: manifest.update(candidate_variant="changed"), "ordinary_task_v1"),
])
def test_manifest_rejects_scope_and_budget_drift(tmp_path, mutation, message):
    _, manifest = _manifest(tmp_path)
    mutation(manifest)

    with pytest.raises(pilot.PilotError, match=message):
        pilot.validate_manifest(manifest)


def test_manifest_rejects_duplicate_task_and_check_ids(tmp_path):
    _, manifest = _manifest(tmp_path)
    manifest["tasks"][1]["task_id"] = manifest["tasks"][0]["task_id"]
    with pytest.raises(pilot.PilotError, match="task ids"):
        pilot.validate_manifest(manifest)
    manifest["tasks"][1]["task_id"] = "unique"
    manifest["tasks"][0]["required_checks"] = ["same", "same"]
    with pytest.raises(pilot.PilotError, match="required checks"):
        pilot.validate_manifest(manifest)


def test_evidence_path_escape_is_rejected(tmp_path):
    path, manifest = _manifest(tmp_path)
    manifest["evidence"]["qualification"]["path"] = "../qualification.json"
    _write_json(path, manifest)

    with pytest.raises(pilot.PilotError, match="contained relative path"):
        pilot.load_manifest(path)


def test_legacy_schema_one_qualification_is_diagnostic_and_cannot_pass_readiness(
    tmp_path,
):
    path, _ = _manifest(tmp_path)

    gates = pilot.evaluate_readiness(path)

    assert gates["qualification"]["status"] == "inconclusive"
    assert gates["qualification"]["reasons"] == [
        "legacy_qualification_evidence_diagnostic_only"
    ]
    assert all(
        gate["status"] == "pass" for name, gate in gates.items()
        if name != "qualification"
    )
    manifest = pilot.load_manifest(path)
    ledger_path = tmp_path / "legacy-ledger.json"
    approval_path, _ = _approval(tmp_path, path, manifest, ledger_path)
    ledger = pilot.initialize_ledger(path, approval_path, ledger_path)
    with pytest.raises(pilot.PilotError, match="readiness gates"):
        pilot.launch_stubbed_run(
            ledger_path,
            next(iter(ledger["runs"])),
            lambda _row: {"invocation_id": "must-not-launch"},
            lambda _row, _invocation: {"actual_cost": 0},
        )


def test_qualification_accepts_manifest_derived_disjoint_pilot_families(tmp_path):
    path, _ = _manifest(tmp_path)

    gate = pilot.evaluate_readiness(path)["qualification"]

    assert gate["status"] == "inconclusive"
    assert gate["reasons"] == ["legacy_qualification_evidence_diagnostic_only"]
    assert gate["metrics"]["source_and_label_identity_pass"] is True
    assert gate["metrics"]["reported_pilot_families_match_manifest"] is True
    assert gate["metrics"]["qualification_pilot_family_overlap"] == []


def test_qualification_rejects_actual_pilot_family_overlap(tmp_path):
    path, manifest = _manifest(tmp_path)
    manifest["tasks"][0]["family_id"] = "qualification-positive-000"
    evidence_path = tmp_path / manifest["evidence"]["qualification"]["path"]
    document = json.loads(evidence_path.read_text(encoding="utf-8"))
    artifact_path = tmp_path / document["checks"][0]["artifact_path"]
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["measurements"]["live_task_family_ids"] = sorted({
        task["family_id"] for task in manifest["tasks"]
    })
    _write_json(artifact_path, artifact)
    document["checks"][0]["artifact_sha256"] = pilot.sha256(artifact_path)
    _write_json(evidence_path, document)
    manifest["evidence"]["qualification"]["sha256"] = pilot.sha256(evidence_path)
    _write_json(path, manifest)

    gate = pilot.evaluate_readiness(path)["qualification"]

    assert gate["status"] == "inconclusive"
    assert "qualification_pilot_family_overlap" in gate["reasons"]
    assert gate["metrics"]["reported_pilot_families_match_manifest"] is True
    assert gate["metrics"]["qualification_pilot_family_overlap"] == [
        "qualification-positive-000"
    ]
    assert gate["metrics"]["source_and_label_identity_pass"] is False


def test_qualification_rejects_stale_reported_pilot_family_list(tmp_path):
    path, manifest = _manifest(tmp_path)
    evidence_path = tmp_path / manifest["evidence"]["qualification"]["path"]
    document = json.loads(evidence_path.read_text(encoding="utf-8"))
    artifact_path = tmp_path / document["checks"][0]["artifact_path"]
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["measurements"]["live_task_family_ids"] = ["family-0", "family-1"]
    _write_json(artifact_path, artifact)
    document["checks"][0]["artifact_sha256"] = pilot.sha256(artifact_path)
    _write_json(evidence_path, document)
    manifest["evidence"]["qualification"]["sha256"] = pilot.sha256(evidence_path)
    _write_json(path, manifest)

    gate = pilot.evaluate_readiness(path)["qualification"]

    assert gate["status"] == "inconclusive"
    assert gate["reasons"] == [
        "legacy_qualification_evidence_diagnostic_only",
        "qualification_live_task_families_mismatch",
    ]
    assert gate["metrics"]["reported_pilot_families_match_manifest"] is False
    assert gate["metrics"]["qualification_pilot_family_overlap"] == []
    assert gate["metrics"]["source_and_label_identity_pass"] is False


def test_contract05_gate_recomputes_frozen_rules_and_rejects_relaxed_thresholds(tmp_path):
    path, manifest = _manifest(tmp_path)
    evidence_path = tmp_path / manifest["evidence"]["contract05_performance"]["path"]
    document = json.loads(evidence_path.read_text(encoding="utf-8"))
    artifact_path = tmp_path / document["checks"][0]["artifact_path"]
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    artifact["measurements"]["campaign"] = _contract05_campaign(candidate_dense_ms=100.0)
    artifact["measurements"]["comparison_rows"] = [{
        "path": "warm", "base_ms": 5.0, "candidate_ms": 100.0,
        "max_added_ms": 200.0,
    }]
    _write_json(artifact_path, artifact)
    document["checks"][0]["artifact_sha256"] = pilot.sha256(artifact_path)
    _write_json(evidence_path, document)
    manifest["evidence"]["contract05_performance"]["sha256"] = pilot.sha256(evidence_path)
    _write_json(path, manifest)

    gate = pilot.evaluate_readiness(path)["contract05_performance"]

    assert gate["status"] == "fail"
    assert gate["reasons"] == ["contract05_release_gate_not_met"]
    assert gate["metrics"]["dense_improvement_targets_pass"] is False
    assert "dense_full:median_ms" in gate["metrics"]["large_slowdown_alarms"]


def test_contract05_campaign_sources_must_match_approved_manifest_identities(tmp_path):
    path, manifest = _manifest(tmp_path)
    evidence_path = tmp_path / manifest["evidence"]["contract05_performance"]["path"]
    document = json.loads(evidence_path.read_text(encoding="utf-8"))
    artifact_path = tmp_path / document["checks"][0]["artifact_path"]
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    phases = artifact["measurements"]["campaign"]["phases"]
    for phase, pairs in phases.items():
        for pair in pairs:
            identities = {
                "a": "d" * 64,
                "b": "e" * 64 if phase == "comparison" else "d" * 64,
            }
            for arm, identity in identities.items():
                report = pair[arm]
                report["source_lock_sha256"] = identity
                report["source_tree_sha256_before"] = identity
                report["source_tree_sha256_after"] = identity
                report["source_sha256"] = {"store": identity, "working_set": identity}
                report["git"]["diff_sha256"] = identity
    _write_json(artifact_path, artifact)
    document["checks"][0]["artifact_sha256"] = pilot.sha256(artifact_path)
    _write_json(evidence_path, document)
    manifest["evidence"]["contract05_performance"]["sha256"] = pilot.sha256(evidence_path)
    _write_json(path, manifest)

    gate = pilot.evaluate_readiness(path)["contract05_performance"]

    assert gate["status"] == "inconclusive"
    assert "approved integration" in gate["reasons"][0]


def test_qualification_rejects_duplicate_full_task_route_emissions(tmp_path):
    path, manifest = _manifest(tmp_path)
    evidence_path = tmp_path / manifest["evidence"]["qualification"]["path"]
    document = json.loads(evidence_path.read_text(encoding="utf-8"))
    artifact_path = tmp_path / document["checks"][0]["artifact_path"]
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    positive = next(
        row for row in artifact["measurements"]["relevance_observations"]
        if row["category"] == "positive"
    )
    correct = positive["expected_revisions"][0]
    positive["candidate_revisions"] = [correct] * 10 + ["irrelevant-revision"]
    _write_json(artifact_path, artifact)
    document["checks"][0]["artifact_sha256"] = pilot.sha256(artifact_path)
    _write_json(evidence_path, document)
    manifest["evidence"]["qualification"]["sha256"] = pilot.sha256(evidence_path)
    _write_json(path, manifest)

    gate = pilot.evaluate_readiness(path)["qualification"]

    assert gate["status"] == "inconclusive"
    assert gate["reasons"] == ["duplicate_or_invalid_revision_emission"]


def test_report_status_field_cannot_replace_evidence(tmp_path):
    path, manifest = _manifest(tmp_path)
    evidence_path = tmp_path / manifest["evidence"]["isolation"]["path"]
    document = json.loads(evidence_path.read_text(encoding="utf-8"))
    artifact_path = tmp_path / document["checks"][0]["artifact_path"]
    artifact = json.loads(artifact_path.read_text(encoding="utf-8"))
    document["status"] = "pass"
    next(
        probe for probe in artifact["measurements"]["probes"]
        if probe["probe_id"] == "gold_read_denied"
    )["observed"] = "fail"
    _write_json(artifact_path, artifact)
    document["checks"][0]["artifact_sha256"] = pilot.sha256(artifact_path)
    _write_json(evidence_path, document)
    manifest["evidence"]["isolation"]["sha256"] = pilot.sha256(evidence_path)
    _write_json(path, manifest)

    gate = pilot.evaluate_readiness(path)["isolation"]

    assert gate["status"] == "fail"
    assert "gold_read_denied_failed" in gate["reasons"]


def test_stub_and_placeholder_artifacts_never_satisfy_live_readiness(tmp_path):
    measurements = {
        "qualification": _qualification_measurements(),
        "contract05_performance": _performance_measurements(
            "contract05_performance"
        ),
        "route_performance": _performance_measurements("route_performance"),
        "isolation": _isolation_measurements(),
    }
    overrides = {
        kind: _artifact(kind, value, mode="stub", synthetic=True, stub=True)
        for kind, value in measurements.items()
    }
    path, _ = _manifest(tmp_path, artifact_overrides=overrides)

    gates = pilot.evaluate_readiness(path)

    assert all(gate["status"] == "inconclusive" for gate in gates.values())
    assert all(
        "stub_evidence_not_live_eligible" in gate["reasons"]
        for gate in gates.values()
    )
    assert pilot.build_report(path)["statuses"]["measured_pilot_permitted"] is False


def test_evidence_hash_and_source_drift_are_inconclusive(tmp_path):
    path, manifest = _manifest(tmp_path)
    evidence_path = tmp_path / manifest["evidence"]["route_performance"]["path"]
    evidence_path.write_text(evidence_path.read_text() + "\n", encoding="utf-8")
    assert pilot.evaluate_readiness(path)["route_performance"]["status"] == "inconclusive"

    document = _performance_evidence("route_performance")
    document["integration_source"]["head"] = "d" * 40
    _write_json(evidence_path, document)
    manifest["evidence"]["route_performance"]["sha256"] = pilot.sha256(evidence_path)
    _write_json(path, manifest)
    gate = pilot.evaluate_readiness(path)["route_performance"]
    assert gate["status"] == "inconclusive"
    assert "integration_source_mismatch" in gate["reasons"]


def test_hashed_check_artifact_must_exist_and_match(tmp_path):
    path, manifest = _manifest(tmp_path)
    artifact = tmp_path / "artifacts" / "isolation.json"
    artifact.write_text("tampered\n", encoding="utf-8")

    gate = pilot.evaluate_readiness(path)["isolation"]

    assert gate["status"] == "inconclusive"
    assert "check_artifact_identity_mismatch" in gate["reasons"]


def test_missing_evidence_is_not_run(tmp_path):
    path, manifest = _manifest(tmp_path)
    (tmp_path / manifest["evidence"]["qualification"]["path"]).unlink()

    gate = pilot.evaluate_readiness(path)["qualification"]

    assert gate["status"] == "not_run"


def test_invalid_check_evidence_cannot_count_as_pass(tmp_path):
    path, manifest = _manifest(tmp_path)
    evidence_path = tmp_path / manifest["evidence"]["contract05_performance"]["path"]
    document = _performance_evidence("contract05_performance")
    document["checks"][0].pop("artifact_sha256")
    _write_json(evidence_path, document)
    manifest["evidence"]["contract05_performance"]["sha256"] = pilot.sha256(evidence_path)
    _write_json(path, manifest)

    gate = pilot.evaluate_readiness(path)["contract05_performance"]

    assert gate["status"] == "inconclusive"
    assert "derived_check_schema_invalid" in gate["reasons"]


def test_exact_negative_bound_requires_72_independent_families():
    assert pilot.clopper_pearson_upper(0, 71) == pytest.approx(0.0506294, rel=1e-5)
    assert pilot.clopper_pearson_upper(0, 72) == pytest.approx(0.0499441, rel=1e-5)
    assert pilot.negative_evidence_metrics(_negative_payload(71))[
        "passes"
    ] is False
    assert pilot.negative_evidence_metrics(_negative_payload(72))[
        "passes"
    ] is True


def test_paraphrases_do_not_inflate_independent_family_count():
    payload = _negative_payload(2)
    for family in payload["negative_families"]:
        family["bundle_size"] = 40

    metrics = pilot.negative_evidence_metrics(payload)

    assert metrics["independent_families"] == 2
    assert metrics["pooled_prompts"] == 80
    assert metrics["family_error_upper_95"] == pytest.approx(0.841886, rel=1e-5)
    assert metrics["passes"] is False


def test_any_bundle_error_counts_as_family_error_and_hard_error_always_fails():
    payload = _negative_payload()
    payload["negative_families"][0].update(
        bundle_size=40, unexpected_full_injections=1,
    )
    metrics = pilot.negative_evidence_metrics(payload)
    assert metrics["family_errors"] == 1
    assert metrics["passes"] is False

    payload = _negative_payload()
    payload["hard_negative_errors"] = 1
    assert pilot.negative_evidence_metrics(payload)["passes"] is False


def test_empty_all_error_and_unresolved_dependence_are_not_qualified():
    payload = _negative_payload(0)
    assert pilot.negative_evidence_metrics(payload)["family_error_upper_95"] is None
    payload = _negative_payload()
    for family in payload["negative_families"]:
        family["unexpected_full_injections"] = 1
    assert pilot.negative_evidence_metrics(payload)["family_error_upper_95"] == 1.0
    payload = _negative_payload()
    payload["negative_families"][0]["independent_draw"] = False
    metrics = pilot.negative_evidence_metrics(payload)
    assert metrics["independence_resolved"] is False
    assert metrics["passes"] is False

    payload = _negative_payload()
    payload["negative_families"][0]["bundle_size"] = 2
    metrics = pilot.negative_evidence_metrics(payload)
    assert metrics["equal_bundle_sizes"] is False
    assert metrics["passes"] is False


def test_differential_canaries_require_candidate_task_and_shared_legacy_delivery(tmp_path):
    _, manifest = _manifest(tmp_path)

    assert pilot.validate_canaries(_canaries(manifest), manifest) == {
        "status": "pass", "reasons": [],
    }


@pytest.mark.parametrize("mutate, reason", [
    (lambda rows: rows[1]["prompts"][0].update(
        received_revisions=[], task_route_receipt=False,
    ), "candidate_task_delivery_invalid"),
    (lambda rows: rows[0].update(hook_success=False), "baseline_hook_not_proven"),
    (lambda rows: rows[1]["prompts"][0].update(task_route_receipt=False),
     "candidate_task_delivery_invalid"),
    (lambda rows: rows[0]["prompts"][1].update(suppressed_by_working_set=True),
     "baseline_legacy_control_suppressed"),
    (lambda rows: rows[0]["prompts"][0].update(
        received_revisions=["revision-ordinary"], task_route_receipt=True,
    ), "baseline_unexpected_task_delivery"),
])
def test_canary_regressions_fail(tmp_path, mutate, reason):
    _, manifest = _manifest(tmp_path)
    rows = _canaries(manifest)
    mutate(rows)

    result = pilot.validate_canaries(rows, manifest)

    assert result["status"] == "fail"
    assert reason in result["reasons"]


def test_missing_outcome_bounds_preserve_the_required_counterexample():
    schedule = _simple_schedule(100)
    outcomes = []
    for index in range(100):
        outcomes.append(_outcome(f"r{index}:candidate", "success"))
        if index < 60:
            outcomes.append(_outcome(f"r{index}:baseline", "success"))

    metrics = pilot.paired_outcome_metrics(schedule, outcomes)

    assert metrics["observed_paired_difference"] == pytest.approx(0.40)
    assert metrics["missing_outcome_bounds"] == pytest.approx([0.0, 0.40])
    assert metrics["arms"]["baseline"]["unknown_rate"] == pytest.approx(0.40)
    assert metrics["outcome_quality_gate"] is False
    assert metrics["benefit_qualified"] is False


def test_arm_swapped_unknowns_and_all_unknowns_preserve_denominators():
    schedule = _simple_schedule(10)
    candidate_known = [
        _outcome(f"r{index}:candidate", "success") for index in range(6)
    ]
    baseline_known = [
        _outcome(f"r{index}:baseline", "success") for index in range(10)
    ]
    metrics = pilot.paired_outcome_metrics(schedule, candidate_known + baseline_known)
    assert metrics["missing_outcome_bounds"] == pytest.approx([-0.4, 0.0])
    assert metrics["arms"]["candidate"]["assigned"] == 10
    assert metrics["arms"]["baseline"]["assigned"] == 10

    metrics = pilot.paired_outcome_metrics(schedule, [])
    assert metrics["arms"]["candidate"]["unknown"] == 10
    assert metrics["arms"]["baseline"]["unknown"] == 10
    assert metrics["missing_outcome_bounds"] == pytest.approx([-1.0, 1.0])


def test_invalid_success_becomes_unknown_but_a_checked_failure_remains_failure():
    schedule = _simple_schedule(1)
    success = _outcome("r0:candidate", "success")
    success["checks"] = []
    failure = _outcome("r0:baseline", "failure")
    failure["artifact_complete"] = False

    metrics = pilot.paired_outcome_metrics(schedule, [success, failure])

    assert metrics["arms"]["candidate"]["unknown"] == 1
    assert metrics["arms"]["baseline"]["failure"] == 1


def test_forged_failure_and_duplicate_check_evidence_become_unknown():
    schedule = _simple_schedule(1)
    failure = _outcome("r0:baseline", "failure")
    failure["checks"][0]["evidence_valid"] = False
    success = _outcome("r0:candidate", "success")
    success["checks"].append(copy.deepcopy(success["checks"][0]))

    metrics = pilot.paired_outcome_metrics(schedule, [failure, success])

    assert metrics["arms"]["baseline"]["unknown"] == 1
    assert metrics["arms"]["candidate"]["unknown"] == 1
    assert metrics["arms"]["baseline"]["required_check_coverage"] == {
        "numerator": 0, "denominator": 1, "ratio": 0.0,
    }


def test_family_clustered_interval_is_predeclared_and_deterministic(tmp_path):
    manifest_path, manifest = _manifest(tmp_path)
    schedule = pilot.frozen_schedule(manifest)
    outcomes = []
    for row in schedule:
        if row["kind"] != "task":
            continue
        outcome = {
            "run_id": row["run_id"],
            "outcome": "success" if row["arm"] == "candidate" else "failure",
            "invocation_complete": True,
            "artifact_complete": True,
            "checks": [{
                "check_id": check_id,
                "status": "pass" if row["arm"] == "candidate" else "fail",
                "evidence_valid": True,
            } for check_id in row["required_checks"]],
        }

        outcomes.append(outcome)

    left = pilot.build_report(manifest_path, outcomes=outcomes)["outcomes"][
        "sampling_uncertainty"
    ]
    right = pilot.build_report(manifest_path, outcomes=outcomes)["outcomes"][
        "sampling_uncertainty"
    ]

    assert left == right
    assert left["status"] == "descriptive"
    assert left["method"] == "family_cluster_percentile_bootstrap_v1"
    assert left["independent_clusters"] == 3
    assert left["reason"] == "diagnostic_only"


def test_outcomes_reject_forged_or_duplicate_run_ids():
    schedule = _simple_schedule(1)
    with pytest.raises(pilot.PilotError, match="at most once"):
        pilot.paired_outcome_metrics(schedule, [_outcome("forged", "success")])
    row = _outcome("r0:candidate", "success")
    with pytest.raises(pilot.PilotError, match="at most once"):
        pilot.paired_outcome_metrics(schedule, [row, row])


@pytest.mark.parametrize("reported_outcome", ["success", "unknown"])
def test_valid_required_failure_controls_outcome_despite_reported_summary(
    reported_outcome,
):
    schedule = _simple_schedule(1)
    row = _outcome("r0:candidate", reported_outcome)
    row["checks"][0]["status"] = "fail"

    metrics = pilot.paired_outcome_metrics(schedule, [row])

    assert metrics["arms"]["candidate"]["failure"] == 1
    assert metrics["arms"]["candidate"]["unknown"] == 0
    assert metrics["arms"]["candidate"]["tasks_with_complete_verification"] == 1


def test_approval_binds_manifest_location_ledger_budget_and_every_run(tmp_path):
    manifest_path, manifest = _manifest(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    approval_path, approval = _approval(tmp_path, manifest_path, manifest, ledger_path)

    assert pilot.validate_approval(
        approval_path, manifest_path, manifest, ledger_path,
    )["approval_id"] == "approval-test-1"

    copied = tmp_path / "copied-approval.json"
    _write_json(copied, approval)
    with pytest.raises(pilot.PilotError, match="copied approval"):
        pilot.validate_approval(copied, manifest_path, manifest, ledger_path)

    approval["approved_run_ids"].pop()
    _write_json(approval_path, approval)
    with pytest.raises(pilot.PilotError, match="every scheduled run"):
        pilot.validate_approval(approval_path, manifest_path, manifest, ledger_path)


def test_expired_and_forged_approval_are_rejected(tmp_path):
    manifest_path, manifest = _manifest(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    approval_path, approval = _approval(tmp_path, manifest_path, manifest, ledger_path)
    approval["manifest_sha256"] = HASH_A
    _write_json(approval_path, approval)
    with pytest.raises(pilot.PilotError, match="manifest hash"):
        pilot.validate_approval(approval_path, manifest_path, manifest, ledger_path)

    approval["manifest_sha256"] = pilot.sha256(manifest_path)
    approval["approved_at"] = "2019-01-01T00:00:00+00:00"
    approval["expires_at"] = "2020-01-01T00:00:00+00:00"
    _write_json(approval_path, approval)
    with pytest.raises(pilot.PilotError, match="expired"):
        pilot.validate_approval(approval_path, manifest_path, manifest, ledger_path)


def test_future_and_reversed_approval_intervals_are_rejected(tmp_path):
    manifest_path, manifest = _manifest(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    approval_path, approval = _approval(tmp_path, manifest_path, manifest, ledger_path)
    approval["approved_at"] = (datetime.now(timezone.utc) + timedelta(days=1)).isoformat()
    approval["expires_at"] = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    _write_json(approval_path, approval)

    with pytest.raises(pilot.PilotError, match="not yet valid"):
        pilot.validate_approval(approval_path, manifest_path, manifest, ledger_path)
    with pytest.raises(pilot.PilotError, match="not yet valid"):
        pilot.initialize_ledger(manifest_path, approval_path, ledger_path)

    approval["approved_at"] = "2026-09-22T00:00:00+00:00"
    approval["expires_at"] = "2026-09-21T00:00:00+00:00"
    _write_json(approval_path, approval)
    with pytest.raises(pilot.PilotError, match="validity interval"):
        pilot.validate_approval(
            approval_path, manifest_path, manifest, ledger_path, require_current=False,
        )


def test_ledger_initialization_is_idempotent_and_rejects_conflicting_identity(tmp_path):
    manifest_path, manifest = _manifest(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    approval_path, _ = _approval(tmp_path, manifest_path, manifest, ledger_path)

    first = pilot.initialize_ledger(manifest_path, approval_path, ledger_path)
    second = pilot.initialize_ledger(manifest_path, approval_path, ledger_path)

    assert first == second
    assert len(first["runs"]) == 26
    first["approval_sha256"] = HASH_A
    _write_json(ledger_path, first)
    with pytest.raises(pilot.PilotError, match="identity conflicts"):
        pilot.initialize_ledger(manifest_path, approval_path, ledger_path)


def test_task_launch_is_blocked_until_differential_canaries_are_recorded(
    tmp_path, _qualified_readiness_for_coordinator,
):
    manifest_path, manifest = _manifest(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    approval_path, _ = _approval(tmp_path, manifest_path, manifest, ledger_path)
    ledger = pilot.initialize_ledger(manifest_path, approval_path, ledger_path)
    task_id = next(
        run_id for run_id, row in ledger["runs"].items() if row["kind"] == "task"
    )

    with pytest.raises(pilot.PilotError, match="offline canary simulation"):
        pilot.launch_stubbed_run(
            ledger_path, task_id, lambda row: {"invocation_id": "forbidden"},
            lambda row, inv: {"invocation_id": "forbidden", "actual_cost": 0},
        )


def test_stale_or_changed_approval_stops_resume(tmp_path):
    manifest_path, manifest = _manifest(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    approval_path, approval = _approval(tmp_path, manifest_path, manifest, ledger_path)
    ledger = pilot.initialize_ledger(manifest_path, approval_path, ledger_path)
    approval["approved_at"] = "2019-01-01T00:00:00+00:00"
    approval["expires_at"] = "2020-01-01T00:00:00+00:00"
    _write_json(approval_path, approval)

    with pytest.raises(pilot.PilotError, match="expired|identities"):
        pilot.launch_stubbed_run(
            ledger_path, next(iter(ledger["runs"])),
            lambda row: {"invocation_id": "forbidden"},
            lambda row, inv: {"invocation_id": "forbidden", "actual_cost": 0},
        )


def test_tampered_ledger_accounting_is_rejected(tmp_path):
    manifest_path, manifest = _manifest(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    approval_path, _ = _approval(tmp_path, manifest_path, manifest, ledger_path)
    ledger = pilot.initialize_ledger(manifest_path, approval_path, ledger_path)
    ledger["reserved_sessions"] = 1
    _write_json(ledger_path, ledger)

    with pytest.raises(pilot.PilotError, match="reservations do not reconcile"):
        pilot.build_report(manifest_path, ledger_path=ledger_path)


def test_tampered_ledger_budget_cannot_expand_approved_allowance(tmp_path):
    manifest_path, manifest = _manifest(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    approval_path, _ = _approval(tmp_path, manifest_path, manifest, ledger_path)
    ledger = pilot.initialize_ledger(manifest_path, approval_path, ledger_path)
    ledger["budget"]["per_session_max_cost"] = 99.0
    ledger["budget"]["max_total_cost"] = 2574.0
    _write_json(ledger_path, ledger)
    calls = []

    with pytest.raises(pilot.PilotError, match="approved manifest"):
        pilot.launch_stubbed_run(
            ledger_path, next(iter(ledger["runs"])),
            lambda row: calls.append(row) or {"invocation_id": "forbidden"},
            lambda row, invocation: {
                "invocation_id": invocation["invocation_id"], "actual_cost": 99.0,
            },
        )

    assert calls == []


def test_tampered_or_added_ledger_assignment_cannot_launch(
    tmp_path, _qualified_readiness_for_coordinator,
):
    manifest_path, manifest = _manifest(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    approval_path, _ = _approval(tmp_path, manifest_path, manifest, ledger_path)
    ledger = pilot.initialize_ledger(manifest_path, approval_path, ledger_path)
    template = copy.deepcopy(next(iter(ledger["runs"].values())))
    template["run_id"] = "forged-extra-run"
    ledger["runs"][template["run_id"]] = template
    _write_json(ledger_path, ledger)

    with pytest.raises(pilot.PilotError, match="frozen schedule"):
        pilot.launch_stubbed_run(
            ledger_path, template["run_id"],
            lambda row: {"invocation_id": "forged"},
            lambda row, inv: {"invocation_id": "forged", "actual_cost": 0},
        )


@pytest.mark.parametrize(("field", "value"), [
    ("prompt_sha256", HASH_A),
    ("validator_sha256", HASH_A),
    ("required_checks", ["forged-check"]),
])
def test_tampered_executable_ledger_assignment_cannot_launch(
    tmp_path, field, value, _qualified_readiness_for_coordinator,
):
    manifest_path, manifest = _manifest(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    approval_path, _ = _approval(tmp_path, manifest_path, manifest, ledger_path)
    ledger = pilot.initialize_ledger(manifest_path, approval_path, ledger_path)
    run_id = next(
        candidate for candidate, row in ledger["runs"].items() if row["kind"] == "task"
    )
    ledger["runs"][run_id][field] = value
    _write_json(ledger_path, ledger)
    calls = []

    with pytest.raises(pilot.PilotError, match="assignment identity"):
        pilot.launch_stubbed_run(
            ledger_path, run_id,
            lambda row: calls.append(row) or {"invocation_id": "forged"},
            lambda row, inv: {"invocation_id": "forged", "actual_cost": 0},
        )

    assert calls == []


def test_ledger_write_failure_before_intent_launches_nothing(
    tmp_path, monkeypatch, _qualified_readiness_for_coordinator,
):
    manifest_path, manifest = _manifest(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    approval_path, _ = _approval(tmp_path, manifest_path, manifest, ledger_path)
    ledger = pilot.initialize_ledger(manifest_path, approval_path, ledger_path)
    run_id = next(iter(ledger["runs"]))
    calls = []
    original = pilot.atomic_write_json

    def fail_write(path, value):
        if value.get("runs", {}).get(run_id, {}).get("state") == "launch_pending":
            raise OSError("disk full")
        return original(path, value)

    monkeypatch.setattr(pilot, "atomic_write_json", fail_write)
    with pytest.raises(OSError, match="disk full"):
        pilot.launch_stubbed_run(
            ledger_path, run_id,
            lambda run: calls.append(run) or {"invocation_id": "inv-1"},
            lambda run, invocation: {},
        )

    assert calls == []
    assert pilot.load_json(ledger_path)["runs"][run_id]["state"] == "planned"


def test_launch_reserves_before_start_and_reconciles_actual_cost(
    tmp_path, _qualified_readiness_for_coordinator,
):
    manifest_path, manifest = _manifest(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    approval_path, _ = _approval(tmp_path, manifest_path, manifest, ledger_path)
    ledger = pilot.initialize_ledger(manifest_path, approval_path, ledger_path)
    run_id = next(iter(ledger["runs"]))
    observed = []

    run = pilot.launch_stubbed_run(
        ledger_path,
        run_id,
        lambda row: observed.append(row["state"]) or {"invocation_id": "inv-1"},
        lambda row, invocation: {
            "invocation_id": invocation["invocation_id"], "actual_cost": 1.25,
        },
    )

    stored = pilot.load_json(ledger_path)
    assert observed == ["launch_pending"]
    assert run["state"] == "completed"
    assert stored["reserved_cost"] == 0
    assert stored["reserved_sessions"] == 0
    assert stored["reconciled_cost"] == 1.25
    with pytest.raises(pilot.PilotError, match="first launch"):
        pilot.launch_stubbed_run(ledger_path, run_id, lambda row: {}, lambda row, inv: {})


@pytest.mark.parametrize("failpoint, expected_state, starts", [
    ("before_intent", "planned", 0),
    ("after_intent", "launch_pending", 0),
    ("after_spawn", "launch_pending", 1),
    ("after_result", "started", 1),
])
def test_launch_crash_windows_never_auto_release_or_duplicate(
    tmp_path, failpoint, expected_state, starts, _qualified_readiness_for_coordinator,
):
    manifest_path, manifest = _manifest(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    approval_path, _ = _approval(tmp_path, manifest_path, manifest, ledger_path)
    ledger = pilot.initialize_ledger(manifest_path, approval_path, ledger_path)
    run_id = next(iter(ledger["runs"]))
    calls = []

    with pytest.raises(pilot.SimulatedCrash, match=failpoint):
        pilot.launch_stubbed_run(
            ledger_path,
            run_id,
            lambda row: calls.append(row) or {"invocation_id": "inv-crash"},
            lambda row, invocation: {
                "invocation_id": invocation["invocation_id"], "actual_cost": 0.5,
            },
            failpoint=failpoint,
        )

    stored = pilot.load_json(ledger_path)
    assert stored["runs"][run_id]["state"] == expected_state
    assert len(calls) == starts
    if expected_state == "planned":
        assert stored["reserved_sessions"] == 0
    else:
        assert stored["reserved_sessions"] == 1
        assert stored["reserved_cost"] == 2.0


def test_unknown_launch_keeps_reservation_until_authoritative_reconciliation(
    tmp_path, _qualified_readiness_for_coordinator,
):
    manifest_path, manifest = _manifest(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    approval_path, _ = _approval(tmp_path, manifest_path, manifest, ledger_path)
    ledger = pilot.initialize_ledger(manifest_path, approval_path, ledger_path)
    run_id = next(iter(ledger["runs"]))
    with pytest.raises(pilot.SimulatedCrash):
        pilot.launch_stubbed_run(
            ledger_path, run_id, lambda row: {"invocation_id": "inv-unknown"},
            lambda row, invocation: {}, failpoint="after_spawn",
        )

    row = pilot.reconcile_incomplete_launch(ledger_path, run_id, {})
    stored = pilot.load_json(ledger_path)
    assert row["state"] == "launch_unknown"
    assert stored["reserved_sessions"] == 1
    assert stored["reserved_cost"] == 2.0


def test_unresolved_launch_blocks_every_subsequent_session(
    tmp_path, _qualified_readiness_for_coordinator,
):
    manifest_path, manifest = _manifest(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    approval_path, _ = _approval(tmp_path, manifest_path, manifest, ledger_path)
    ledger = pilot.initialize_ledger(manifest_path, approval_path, ledger_path)
    first, second = list(ledger["runs"])[:2]
    with pytest.raises(pilot.SimulatedCrash):
        pilot.launch_stubbed_run(
            ledger_path, first, lambda row: {"invocation_id": "inv-first"},
            lambda row, invocation: {}, failpoint="after_spawn",
        )
    starts = []

    with pytest.raises(pilot.PilotError, match="authoritative reconciliation"):
        pilot.launch_stubbed_run(
            ledger_path, second,
            lambda row: starts.append(row) or {"invocation_id": "inv-second"},
            lambda row, invocation: {
                "invocation_id": invocation["invocation_id"], "actual_cost": 0,
            },
        )

    assert starts == []


def test_authoritative_no_launch_can_release_but_started_invocation_cannot(
    tmp_path, _qualified_readiness_for_coordinator,
):
    manifest_path, manifest = _manifest(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    approval_path, _ = _approval(tmp_path, manifest_path, manifest, ledger_path)
    ledger = pilot.initialize_ledger(manifest_path, approval_path, ledger_path)
    run_ids = list(ledger["runs"])
    with pytest.raises(pilot.SimulatedCrash):
        pilot.launch_stubbed_run(
            ledger_path, run_ids[0], lambda row: {}, lambda row, inv: {},
            failpoint="after_intent",
        )
    row = pilot.reconcile_incomplete_launch(
        ledger_path, run_ids[0],
        {"authoritative_no_launch": True, "evidence_id": "provider-no-launch-1"},
    )
    assert row["state"] == "planned"
    assert "proven_unlaunched" in row["history"]

    with pytest.raises(pilot.SimulatedCrash):
        pilot.launch_stubbed_run(
            ledger_path, run_ids[1], lambda row: {"invocation_id": "inv-started"},
            lambda row, inv: {"invocation_id": "inv-started", "actual_cost": 0},
            failpoint="after_result",
        )
    with pytest.raises(pilot.PilotError, match="started invocation"):
        pilot.reconcile_incomplete_launch(
            ledger_path, run_ids[1],
            {"authoritative_no_launch": True, "evidence_id": "false-proof"},
        )


def test_usage_over_reservation_is_rejected_and_reservation_stays_held(
    tmp_path, _qualified_readiness_for_coordinator,
):
    manifest_path, manifest = _manifest(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    approval_path, _ = _approval(tmp_path, manifest_path, manifest, ledger_path)
    ledger = pilot.initialize_ledger(manifest_path, approval_path, ledger_path)
    run_id = next(iter(ledger["runs"]))

    with pytest.raises(pilot.PilotError, match="exceeded"):
        pilot.launch_stubbed_run(
            ledger_path, run_id, lambda row: {"invocation_id": "inv-cost"},
            lambda row, inv: {"invocation_id": "inv-cost", "actual_cost": 2.01},
        )

    stored = pilot.load_json(ledger_path)
    assert stored["runs"][run_id]["state"] == "started"
    assert stored["reserved_cost"] == 2.0


def test_campaign_lock_is_exclusive(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    with pilot.campaign_lock(ledger_path):
        with pytest.raises(pilot.CampaignBusy):
            with pilot.campaign_lock(ledger_path):
                pass


def test_report_is_read_only_and_never_claims_release(tmp_path):
    manifest_path, _ = _manifest(tmp_path)
    before = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*") if path.is_file()
    }

    report = pilot.build_report(manifest_path)

    after = {
        path.relative_to(tmp_path).as_posix(): path.read_bytes()
        for path in tmp_path.rglob("*") if path.is_file()
    }
    assert before == after
    assert report["provenance"] == {
        "recorded_invocations": 0,
        "stub_invocations": 0,
        "claimed_live_invocations": 0,
        "verified_live_agent_invocations": 0,
        "live_agent_invocations": 0,
        "network_calls": 0,
        "real_home_mutations": 0,
        "contract06_fixtures_used_as_effectiveness_evidence": False,
    }
    assert report["statuses"]["engineering_ready"] is False
    assert report["statuses"]["ready_for_authorized_canaries"] is False
    assert report["statuses"]["measured_pilot_permitted"] is False
    assert report["statuses"]["release_candidate"] is False


def test_stub_canaries_only_qualify_offline_simulation(
    tmp_path, _qualified_readiness_for_coordinator,
):
    manifest_path, manifest = _manifest(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    approval_path, _ = _approval(tmp_path, manifest_path, manifest, ledger_path)
    pilot.initialize_ledger(manifest_path, approval_path, ledger_path)
    canaries = _canaries(manifest)
    _complete_canaries(ledger_path, canaries)
    validation = pilot.record_canary_validation(ledger_path, canaries)

    report = pilot.build_report(
        manifest_path, ledger_path=ledger_path, canaries=canaries,
    )

    assert all(
        gate["status"] == "pass" for gate in report["readiness_gates"].values()
    )
    ledger = pilot.load_json(ledger_path)
    assert validation["evidence_class"] == "offline_simulation"
    assert "canary_validation" not in ledger
    assert ledger["canary_simulation_validation"]["status"] == "pass"
    assert report["canaries"]["simulation_ledger_binding"]["status"] == "pass"
    assert report["canaries"]["live_ledger_binding"]["status"] == "fail"
    assert report["statuses"]["ready_for_authorized_canaries"] is True
    assert report["statuses"]["measured_pilot_permitted"] is False
    assert report["statuses"]["release_candidate"] is False


def test_mutable_ledger_claims_cannot_promote_stub_canaries_to_live(
    tmp_path, _qualified_readiness_for_coordinator,
):
    manifest_path, manifest = _manifest(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    approval_path, _ = _approval(tmp_path, manifest_path, manifest, ledger_path)
    pilot.initialize_ledger(manifest_path, approval_path, ledger_path)
    canaries = _canaries(manifest)
    _complete_canaries(ledger_path, canaries)
    ledger = pilot.load_json(ledger_path)
    for row in canaries:
        run = ledger["runs"][row["run_id"]]
        run["execution_mode"] = "live"
        run["result"]["live_invocation_verified"] = True
        run["result"]["live_verifier_sha256"] = HASH_A
    ledger["canary_validation"] = {
        "status": "pass",
        "evidence_class": "verified_live_host",
        "evidence_sha256": pilot.digest(sorted(canaries, key=lambda row: row["run_id"])),
        "run_ids": sorted(row["run_id"] for row in canaries),
    }
    _write_json(ledger_path, ledger)

    with pytest.raises(pilot.PilotError, match="unavailable in the offline coordinator"):
        pilot.record_canary_validation(ledger_path, canaries)
    report = pilot.build_report(
        manifest_path, ledger_path=ledger_path, canaries=canaries,
    )

    assert report["provenance"]["claimed_live_invocations"] == 2
    assert report["provenance"]["verified_live_agent_invocations"] == 0
    assert report["canaries"]["live_ledger_binding"]["status"] == "fail"
    assert report["statuses"]["measured_pilot_permitted"] is False


def test_expired_approval_is_historical_not_current_launch_authority(
    tmp_path, monkeypatch,
):
    manifest_path, manifest = _manifest(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    approval_path, _ = _approval(tmp_path, manifest_path, manifest, ledger_path)
    pilot.initialize_ledger(manifest_path, approval_path, ledger_path)

    class FutureDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=2)

    monkeypatch.setattr(pilot, "datetime", FutureDateTime)
    report = pilot.build_report(manifest_path, ledger_path=ledger_path)

    assert report["accounting"]["approval_current"] is False
    assert report["statuses"]["ready_for_authorized_canaries"] is False
    assert report["statuses"]["measured_pilot_permitted"] is False


def test_reconciled_diagnostic_pilot_can_complete_but_never_accept_release(
    tmp_path, _qualified_readiness_for_coordinator,
):
    manifest_path, manifest = _manifest(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    approval_path, _ = _approval(tmp_path, manifest_path, manifest, ledger_path)
    pilot.initialize_ledger(manifest_path, approval_path, ledger_path)
    canaries = _canaries(manifest)
    _complete_canaries(ledger_path, canaries)
    pilot.record_canary_validation(ledger_path, canaries)
    schedule = pilot.frozen_schedule(manifest)
    outcomes = []
    for assignment in schedule:
        if assignment["kind"] != "task":
            continue
        invocation_id = f"invocation-{len(outcomes):02d}"
        row = {
            "run_id": assignment["run_id"],
            "invocation_id": invocation_id,
            "outcome": "success",
            "invocation_complete": True,
            "artifact_complete": True,
            "checks": [{
                "check_id": check_id, "status": "pass", "evidence_valid": True,
            } for check_id in assignment["required_checks"]],
            "wall_time_ms": 100,
            "input_tokens": 10,
            "output_tokens": 20,
            "actual_cost": 1.0,
            "delivery": {
                "found": True, "selected": True, "emitted": True,
                "host_received": True, "checked": True,
            },
        }
        pilot.launch_stubbed_run(
            ledger_path,
            assignment["run_id"],
            lambda run, invocation_id=invocation_id: {"invocation_id": invocation_id},
            lambda run, invocation, row=row: {
                "invocation_id": invocation["invocation_id"],
                "actual_cost": row["actual_cost"],
                "task_evidence_sha256": pilot.digest(row),
            },
        )
        outcomes.append(row)

    report = pilot.build_report(
        manifest_path, outcomes=outcomes, canaries=canaries, ledger_path=ledger_path,
    )

    assert report["statuses"]["pilot_complete"] is False
    assert report["statuses"]["offline_simulation_complete"] is True
    assert report["statuses"]["release_candidate"] is False
    assert report["provenance"]["recorded_invocations"] == 26
    assert report["provenance"]["stub_invocations"] == 26
    assert report["provenance"]["claimed_live_invocations"] == 0
    assert report["provenance"]["verified_live_agent_invocations"] == 0
    assert report["provenance"]["live_agent_invocations"] == 0
    assert report["outcomes"]["ledger_binding"]["status"] == "pass"
    assert report["outcomes"]["measurements"]["candidate"]["actual_cost"] == 12


def test_text_report_exposes_gates_bounds_cost_and_no_live_invocations(tmp_path):
    manifest_path, _ = _manifest(tmp_path)

    rendered = pilot.render_text(pilot.build_report(manifest_path))

    assert "readiness gates:" in rendered
    assert "missing-outcome bounds:" in rendered
    assert "outcome-quality gate:" in rendered
    assert "release candidate: false" in rendered
    assert "recorded invocations: 0" in rendered
    assert "offline stub invocations: 0" in rendered
    assert "verified live agent invocations: 0" in rendered


def test_text_report_matches_json_for_two_recorded_stub_sessions(
    tmp_path, _qualified_readiness_for_coordinator,
):
    manifest_path, manifest = _manifest(tmp_path)
    ledger_path = tmp_path / "ledger.json"
    approval_path, _ = _approval(tmp_path, manifest_path, manifest, ledger_path)
    pilot.initialize_ledger(manifest_path, approval_path, ledger_path)
    canaries = _canaries(manifest)
    _complete_canaries(ledger_path, canaries)
    report = pilot.build_report(manifest_path, ledger_path=ledger_path)

    rendered = pilot.render_text(report)

    assert report["provenance"]["recorded_invocations"] == 2
    assert report["provenance"]["stub_invocations"] == 2
    assert report["provenance"]["verified_live_agent_invocations"] == 0
    assert "recorded invocations: 2" in rendered
    assert "offline stub invocations: 2" in rendered
    assert "verified live agent invocations: 0" in rendered


def test_cli_writes_only_explicit_output_and_refuses_live_mode(tmp_path, capsys):
    manifest_path, _ = _manifest(tmp_path)
    before = set(tmp_path.iterdir())
    assert pilot.main(["--manifest", str(manifest_path), "--format", "text"]) == 0
    assert set(tmp_path.iterdir()) == before
    assert "verified live agent invocations: 0" in capsys.readouterr().out

    output = tmp_path / "report.json"
    assert pilot.main([
        "--manifest", str(manifest_path), "--format", "json", "--output", str(output),
    ]) == 0
    assert json.loads(output.read_text())["mode"] == "offline_dry_run"
    with pytest.raises(SystemExit):
        pilot.main(["--manifest", str(manifest_path), "--mode", "live"])


def test_dry_run_does_not_mutate_home_or_environment(tmp_path, monkeypatch):
    manifest_path, _ = _manifest(tmp_path)
    original_home = str(tmp_path / "caller-home")
    monkeypatch.setenv("HOME", original_home)
    before = dict(os.environ)

    pilot.build_report(manifest_path)

    assert os.environ["HOME"] == original_home
    assert dict(os.environ) == before


def test_offline_stub_uses_sanitized_home_and_no_caller_credentials(tmp_path, monkeypatch):
    root = tmp_path / "sandbox"
    cwd = root / "checkout"
    home = root / "home"
    cwd.mkdir(parents=True)
    home.mkdir()
    monkeypatch.setenv("SECRET_MODEL_TOKEN", "must-not-cross")
    code = (
        "import json, os, pathlib; "
        "pathlib.Path(os.environ['HOME'], 'proof').write_text('ok'); "
        "print(json.dumps({'home': os.environ['HOME'], "
        "'secret': os.environ.get('SECRET_MODEL_TOKEN')}))"
    )

    result = pilot.run_offline_stub_process(
        [sys.executable, "-I", "-c", code],
        sandbox_root=root, cwd=cwd, home=home,
    )

    payload = json.loads(result["stdout"])
    assert payload == {"home": str(home.resolve()), "secret": None}
    assert (home / "proof").read_text() == "ok"
    assert result["timed_out"] is False
    assert "SECRET_MODEL_TOKEN" not in result["environment_keys"]
    assert result["protection"].endswith("not an OS sandbox")


def test_offline_stub_bounds_output_and_timeout(tmp_path):
    root = tmp_path / "sandbox"
    cwd = root / "checkout"
    home = root / "home"
    cwd.mkdir(parents=True)
    home.mkdir()

    flooded = pilot.run_offline_stub_process(
        [sys.executable, "-I", "-c", "print('x' * 100000)"],
        sandbox_root=root, cwd=cwd, home=home, output_limit_bytes=1024,
    )
    timed = pilot.run_offline_stub_process(
        [sys.executable, "-I", "-c", "import time; time.sleep(5)"],
        sandbox_root=root, cwd=cwd, home=home, timeout_seconds=0.05,
    )

    assert flooded["output_limited"] is True
    assert len(flooded["stdout"].encode()) <= 1024
    assert timed["timed_out"] is True
    assert timed["elapsed_ms"] < 1000


def test_offline_stub_denies_lingering_descendants(tmp_path):
    root = tmp_path / "sandbox"
    cwd = root / "checkout"
    home = root / "home"
    cwd.mkdir(parents=True)
    home.mkdir()
    sentinel = root / "late-write"
    child = (
        "import pathlib,time; time.sleep(0.3); "
        f"pathlib.Path({str(sentinel)!r}).write_text('escaped')"
    )
    parent = (
        "import subprocess,sys,time; "
        f"subprocess.Popen([sys.executable,'-I','-c',{child!r}]); time.sleep(5)"
    )

    result = pilot.run_offline_stub_process(
        [sys.executable, "-I", "-c", parent],
        sandbox_root=root, cwd=cwd, home=home, timeout_seconds=0.05,
    )
    time.sleep(0.4)

    assert result["returncode"] != 0
    assert result["timed_out"] is False
    assert not sentinel.exists()


def test_offline_stub_reaps_devnull_descendant_after_successful_parent_exit(tmp_path):
    root = tmp_path / "sandbox"
    cwd = root / "checkout"
    home = root / "home"
    cwd.mkdir(parents=True)
    home.mkdir()
    sentinel = root / "late-success-write"
    child = (
        "import pathlib,time; time.sleep(0.3); "
        f"pathlib.Path({str(sentinel)!r}).write_text('escaped')"
    )
    parent = (
        "import subprocess,sys; "
        "\ntry: subprocess.Popen([sys.executable,'-I','-c',"
        f"{child!r}],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)"
        "\nexcept OSError: pass"
    )

    result = pilot.run_offline_stub_process(
        [sys.executable, "-I", "-c", parent],
        sandbox_root=root, cwd=cwd, home=home, timeout_seconds=1.0,
    )
    time.sleep(0.4)

    assert result["returncode"] == 0
    assert result["timed_out"] is False
    assert not sentinel.exists()


def test_offline_stub_denies_new_session_descendant_escape(tmp_path):
    root = tmp_path / "sandbox"
    cwd = root / "checkout"
    home = root / "home"
    cwd.mkdir(parents=True)
    home.mkdir()
    sentinel = root / "escaped-session-write"
    child = (
        "import pathlib,time; time.sleep(0.3); "
        f"pathlib.Path({str(sentinel)!r}).write_text('escaped')"
    )
    parent = (
        "import subprocess,sys; "
        "\ntry: subprocess.Popen([sys.executable,'-I','-c',"
        f"{child!r}],start_new_session=True,stdout=subprocess.DEVNULL,"
        "stderr=subprocess.DEVNULL)"
        "\nexcept OSError: pass"
    )

    result = pilot.run_offline_stub_process(
        [sys.executable, "-I", "-c", parent],
        sandbox_root=root, cwd=cwd, home=home, timeout_seconds=1.0,
    )
    time.sleep(0.4)

    assert result["returncode"] == 0
    assert result["timed_out"] is False
    assert not sentinel.exists()
    assert result["protection"].startswith("descendant creation denied")


@pytest.mark.parametrize("payload", [
    {"invocation_id": "inv-1", "observations": [], "verdict": "pass"},
    {"invocation_id": "other", "observations": []},
    {"invocation_id": "inv-1", "observations": [{"probe_id": "p", "status": "pass"}]},
    {"invocation_id": "inv-1", "observations": [
        {"probe_id": "p", "value": 1}, {"probe_id": "p", "value": 2},
    ]},
    {"invocation_id": "inv-1", "observations": [
        {"probe_id": "p", "value": 1, "error": "forged ambiguity"},
    ]},
])
def test_stub_result_channel_rejects_verdicts_forgery_and_unknown_fields(payload):
    with pytest.raises(pilot.PilotError):
        pilot.parse_stub_observation(json.dumps(payload), "inv-1")


def test_stub_result_channel_returns_only_bounded_raw_observations():
    payload = {
        "invocation_id": "inv-1",
        "observations": [{"probe_id": "behavior", "value": {"seen": True}}],
    }

    assert pilot.parse_stub_observation(json.dumps(payload), "inv-1") == payload
