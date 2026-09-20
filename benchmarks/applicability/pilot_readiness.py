#!/usr/bin/env python3
"""Contract-07 offline pilot readiness, accounting, and outcome analysis.

The module deliberately has no model or network client.  Its CLI validates a frozen pilot
manifest and source-bound evidence, exercises the proposed schedule with an in-memory stub,
and renders a report.  Chargeable execution is available only to an embedding that supplies
an approved host launcher after the live-only gates pass.
"""

from __future__ import annotations

import argparse
import contextlib
import copy
import fcntl
import hashlib
import json
import math
import os
import random
import resource
import selectors
import signal
import subprocess
import tempfile
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from benchmarks.applicability import working_set_lookup as contract05_benchmark


SCHEMA_VERSION = 1
RUNNER_VERSION = "1"
CANDIDATE_VARIANT = "ordinary_task_v1"
ARMS = ("baseline", "candidate")
EVIDENCE_STATUSES = {"pass", "fail", "inconclusive", "not_run"}
OUTCOME_STATES = {"success", "failure", "unknown"}
RUN_STATES = {
    "planned", "launch_pending", "started", "completed", "launch_unknown",
    "proven_unlaunched", "cancelled",
}
GATE_KINDS = (
    "qualification", "contract05_performance", "route_performance", "isolation",
)
EVIDENCE_PROTOCOLS = {
    "qualification": ("qualification.reviewed_measurements", "qualification_review_v1"),
    "contract05_performance": (
        "contract05.acceptance_campaign", "working_set_lookup_campaign_v1",
    ),
    "route_performance": ("route.fixed_machine_comparison", "route_timing_v1"),
    "isolation": ("isolation.os_boundary_probe", "os_isolation_probe_v1"),
}
ISOLATION_PROBES = {
    "checkout_write_allowed", "gold_read_denied", "reviewer_write_denied",
    "cross_arm_denied", "real_home_denied", "candidate_network_denied",
    "credentials_absent", "process_group_reaped", "stream_limits_enforced",
    "symlink_escape_denied", "result_forgery_denied",
}
MAX_STUB_SESSIONS = 26
CONTRACT05_BENCHMARK_PATH = Path(contract05_benchmark.__file__).resolve()
CONTRACT05_MANIFEST_PATH = contract05_benchmark.MANIFEST_PATH.resolve()


class PilotError(ValueError):
    """A frozen pilot input or state transition is unsafe or internally inconsistent."""


class CampaignBusy(PilotError):
    """Another coordinator owns the canonical campaign ledger."""


class SimulatedCrash(RuntimeError):
    """A test-only crash boundary was reached."""


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _is_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(
        char in "0123456789abcdef" for char in value
    )


def _is_git_hash(value: object) -> bool:
    return isinstance(value, str) and len(value) in {40, 64} and all(
        char in "0123456789abcdef" for char in value
    )


def _nonempty(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PilotError(f"{field} must be a non-empty string")
    return value


def _positive_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PilotError(f"{field} must be a positive integer")
    return value


def _finite_nonnegative(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise PilotError(f"{field} must be numeric")
    converted = float(value)
    if not math.isfinite(converted) or converted < 0:
        raise PilotError(f"{field} must be finite and non-negative")
    return converted


def _parse_time(value: object, field: str) -> datetime:
    text = _nonempty(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PilotError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise PilotError(f"{field} must include a timezone")
    return parsed


def _safe_relative(root: Path, value: object, field: str) -> Path:
    text = _nonempty(value, field)
    candidate = Path(text)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise PilotError(f"{field} must be a contained relative path")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise PilotError(f"{field} escapes the manifest root")
    return resolved


def load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PilotError(f"cannot read JSON from {path}") from exc
    if not isinstance(value, dict):
        raise PilotError(f"{path} must contain a JSON object")
    return value


def validate_manifest(manifest: dict[str, Any]) -> None:
    if manifest.get("schema_version") != SCHEMA_VERSION:
        raise PilotError("manifest schema_version must be 1")
    _nonempty(manifest.get("campaign_id"), "campaign_id")
    if manifest.get("candidate_variant") != CANDIDATE_VARIANT:
        raise PilotError("manifest must freeze ordinary_task_v1")
    if manifest.get("development_only") is not True:
        raise PilotError("manifest must remain development_only until separately approved")

    source = manifest.get("integration_source")
    if not isinstance(source, dict) or not _is_git_hash(source.get("head")) \
            or not _is_hash(source.get("diff_sha256")):
        raise PilotError("integration_source must pin head and diff_sha256")
    if not _is_hash(manifest.get("candidate_source_sha256")):
        raise PilotError("candidate_source_sha256 must be a sha256")
    if not _is_hash(manifest.get("label_manifest_sha256")):
        raise PilotError("label_manifest_sha256 must be a sha256")

    evidence = manifest.get("evidence")
    if not isinstance(evidence, dict) or set(evidence) != set(GATE_KINDS):
        raise PilotError("manifest must reference every readiness evidence kind")
    for kind, ref in evidence.items():
        if not isinstance(ref, dict) or not isinstance(ref.get("path"), str) \
                or not _is_hash(ref.get("sha256")):
            raise PilotError(f"{kind} evidence must have path and sha256")

    tasks = manifest.get("tasks")
    repetitions = _positive_int(manifest.get("repetitions"), "repetitions")
    if not isinstance(tasks, list) or not tasks:
        raise PilotError("manifest tasks must be a non-empty list")
    task_ids: set[str] = set()
    repo_ids: set[str] = set()
    family_ids: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise PilotError("tasks must be objects")
        task_id = _nonempty(task.get("task_id"), "task_id")
        repo_id = _nonempty(task.get("repo_id"), "repo_id")
        family_id = _nonempty(task.get("family_id"), "family_id")
        if task_id in task_ids:
            raise PilotError("task ids must be unique")
        task_ids.add(task_id)
        repo_ids.add(repo_id)
        family_ids.add(family_id)
        if not _is_hash(task.get("prompt_sha256")) or not _is_hash(
            task.get("validator_sha256")
        ):
            raise PilotError(f"{task_id}: prompt and validator hashes are required")
        checks = task.get("required_checks")
        if not isinstance(checks, list) or not checks or len(checks) != len(set(checks)) \
                or any(not isinstance(check, str) or not check for check in checks):
            raise PilotError(f"{task_id}: required checks must be unique strings")
    if len(tasks) != 6 or len(repo_ids) < 3:
        raise PilotError("the proposed pilot requires six tasks across at least three repos")
    if repetitions != 2:
        raise PilotError("the proposed pilot requires two repetitions")

    canaries = manifest.get("canaries")
    if not isinstance(canaries, dict):
        raise PilotError("manifest must define differential canaries")
    ordinary_revision = _nonempty(canaries.get("ordinary_revision_id"), "ordinary canary")
    legacy_revision = _nonempty(canaries.get("legacy_revision_id"), "legacy canary")
    if ordinary_revision == legacy_revision:
        raise PilotError("ordinary and legacy canaries must use distinct revisions")
    if canaries.get("ordinary_initially_standing") is not False \
            or canaries.get("ordinary_initially_working_set") is not False:
        raise PilotError("ordinary canary guidance must be absent from initial context")

    budget = manifest.get("budget")
    if not isinstance(budget, dict):
        raise PilotError("manifest must declare budget limits")
    max_sessions = _positive_int(budget.get("max_sessions"), "max_sessions")
    expected_sessions = len(tasks) * repetitions * len(ARMS) + len(ARMS)
    if max_sessions != expected_sessions or max_sessions > MAX_STUB_SESSIONS:
        raise PilotError("session ceiling must exactly cover the frozen schedule")
    per_session = _finite_nonnegative(budget.get("per_session_max_cost"), "per-session cost")
    total = _finite_nonnegative(budget.get("max_total_cost"), "total cost")
    if per_session <= 0 or total < per_session * max_sessions:
        raise PilotError("budget must reserve every session at its worst-case cost")
    _nonempty(budget.get("currency"), "budget currency")
    _nonempty(budget.get("pricing_basis"), "pricing basis")
    _parse_time(budget.get("pricing_date"), "pricing_date")
    if budget.get("concurrency") != 1:
        raise PilotError("the first pilot must run one live session at a time")
    if budget.get("per_session_limit_enforced") is not True:
        raise PilotError("the approved host must enforce the per-session limit")

    _positive_int(manifest.get("randomization_seed"), "randomization_seed")
    if len(family_ids) < 3:
        raise PilotError("pilot tasks must cover at least three predeclared families")
    analysis = manifest.get("analysis")
    if not isinstance(analysis, dict) \
            or analysis.get("sampling_method") != "family_cluster_percentile_bootstrap_v1" \
            or analysis.get("confidence") != 0.95:
        raise PilotError("manifest must freeze the family-clustered 95% interval method")
    _positive_int(analysis.get("sampling_seed"), "sampling_seed")
    if _positive_int(analysis.get("resamples"), "resamples") < 1000:
        raise PilotError("sampling analysis requires at least 1000 frozen resamples")


def load_manifest(path: Path) -> dict[str, Any]:
    manifest = load_json(path)
    validate_manifest(manifest)
    root = path.resolve().parent
    for kind, ref in manifest["evidence"].items():
        _safe_relative(root, ref["path"], f"{kind} evidence path")
    return manifest


def frozen_schedule(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    validate_manifest(manifest)
    rows = [
        {
            "run_id": f"{manifest['campaign_id']}:canary:{arm}",
            "kind": "canary",
            "arm": arm,
            "repetition": 0,
            "task_id": None,
            "family_id": None,
            "repo_id": None,
        }
        for arm in ARMS
    ]
    pairs = []
    rng = random.Random(manifest["randomization_seed"])
    for task in manifest["tasks"]:
        for repetition in range(1, manifest["repetitions"] + 1):
            arms = list(ARMS)
            rng.shuffle(arms)
            for arm in arms:
                pairs.append({
                    "run_id": (
                        f"{manifest['campaign_id']}:task:{task['task_id']}:"
                        f"r{repetition}:{arm}"
                    ),
                    "kind": "task",
                    "arm": arm,
                    "repetition": repetition,
                    "task_id": task["task_id"],
                    "family_id": task["family_id"],
                    "repo_id": task["repo_id"],
                    "prompt_sha256": task["prompt_sha256"],
                    "validator_sha256": task["validator_sha256"],
                    "required_checks": copy.deepcopy(task["required_checks"]),
                })
    return rows + pairs


def _binomial_cdf(successes: int, trials: int, probability: float) -> float:
    if probability <= 0:
        return 1.0
    if probability >= 1:
        return 1.0 if successes >= trials else 0.0
    total = 0.0
    for observed in range(successes + 1):
        total += math.comb(trials, observed) * probability ** observed * (
            1 - probability
        ) ** (trials - observed)
    return total


def clopper_pearson_upper(errors: int, families: int, confidence: float = 0.95) -> float | None:
    """Exact two-sided Clopper-Pearson upper endpoint for a binomial rate."""
    if isinstance(errors, bool) or isinstance(families, bool) \
            or not isinstance(errors, int) or not isinstance(families, int):
        raise PilotError("errors and families must be integers")
    if families < 0 or errors < 0 or errors > families:
        raise PilotError("invalid binomial counts")
    if not 0 < confidence < 1:
        raise PilotError("confidence must be between zero and one")
    if families == 0:
        return None
    if errors == families:
        return 1.0
    alpha_tail = (1 - confidence) / 2
    low, high = 0.0, 1.0
    for _ in range(100):
        midpoint = (low + high) / 2
        if _binomial_cdf(errors, families, midpoint) > alpha_tail:
            low = midpoint
        else:
            high = midpoint
    return (low + high) / 2


def negative_evidence_metrics(payload: dict[str, Any]) -> dict[str, Any]:
    families = payload.get("negative_families")
    if not isinstance(families, list):
        raise PilotError("qualification evidence requires negative_families")
    ids: set[str] = set()
    family_errors = 0
    prompt_count = 0
    prompt_errors = 0
    unresolved_dependence = False
    bundle_sizes: set[int] = set()
    for family in families:
        if not isinstance(family, dict):
            raise PilotError("negative families must be objects")
        family_id = _nonempty(family.get("family_id"), "negative family id")
        if family_id in ids:
            raise PilotError("negative family ids must be unique")
        ids.add(family_id)
        bundle_size = _positive_int(family.get("bundle_size"), "negative bundle size")
        errors = family.get("unexpected_full_injections")
        if isinstance(errors, bool) or not isinstance(errors, int) \
                or errors < 0 or errors > bundle_size:
            raise PilotError("negative injection count is invalid")
        if family.get("independent_draw") is not True \
                or not isinstance(family.get("sampling_distribution"), str) \
                or not family["sampling_distribution"]:
            unresolved_dependence = True
        bundle_sizes.add(bundle_size)
        prompt_count += bundle_size
        prompt_errors += errors
        family_errors += errors > 0
    upper = clopper_pearson_upper(family_errors, len(families))
    pooled_rate = prompt_errors / prompt_count if prompt_count else None
    hard_errors = payload.get("hard_negative_errors")
    if isinstance(hard_errors, bool) or not isinstance(hard_errors, int) or hard_errors < 0:
        raise PilotError("hard_negative_errors must be a non-negative integer")
    passes = (
        len(families) >= 72
        and not unresolved_dependence
        and pooled_rate is not None and pooled_rate <= 0.05
        and upper is not None and upper <= 0.05
        and hard_errors == 0
    )
    return {
        "independent_families": len(families),
        "family_errors": family_errors,
        "family_error_rate": family_errors / len(families) if families else None,
        "family_error_upper_95": upper,
        "pooled_prompts": prompt_count,
        "pooled_errors": prompt_errors,
        "pooled_rate": pooled_rate,
        "hard_negative_errors": hard_errors,
        "independence_resolved": not unresolved_dependence,
        "equal_bundle_sizes": len(bundle_sizes) <= 1,
        "passes": passes and len(bundle_sizes) <= 1,
    }


def _verified_evidence_artifact(
    kind: str,
    document: dict[str, Any],
    manifest: dict[str, Any],
    root: Path,
) -> tuple[dict[str, Any] | None, list[str]]:
    reasons = []
    expected_check, expected_producer = EVIDENCE_PROTOCOLS[kind]
    if document.get("schema_version") != SCHEMA_VERSION:
        reasons.append("schema_mismatch")
    if document.get("evidence_kind") != kind:
        reasons.append("evidence_kind_mismatch")
    if document.get("integration_source") != manifest["integration_source"]:
        reasons.append("integration_source_mismatch")
    if document.get("candidate_source_sha256") != manifest["candidate_source_sha256"]:
        reasons.append("candidate_source_mismatch")
    try:
        _parse_time(document.get("measured_at"), "measured_at")
    except PilotError:
        reasons.append("measurement_time_invalid")
    checks = document.get("checks")
    if not isinstance(checks, list) or len(checks) != 1 or not isinstance(checks[0], dict):
        return None, sorted(set(reasons + ["exactly_one_derived_check_required"]))
    check = checks[0]
    if "status" in check or check.get("check_id") != expected_check \
            or check.get("evidence_class") != "measured_live_prerequisite" \
            or check.get("protocol") != expected_producer \
            or not _is_hash(check.get("artifact_sha256")) \
            or not _is_hash(check.get("validator_sha256")) \
            or not isinstance(check.get("artifact_path"), str):
        return None, sorted(set(reasons + ["derived_check_schema_invalid"]))
    try:
        artifact_path = _safe_relative(root, check["artifact_path"], "check artifact path")
    except PilotError:
        return None, sorted(set(reasons + ["check_artifact_path_invalid"]))
    if not artifact_path.is_file() or sha256(artifact_path) != check["artifact_sha256"]:
        return None, sorted(set(reasons + ["check_artifact_identity_mismatch"]))
    try:
        artifact = load_json(artifact_path)
    except PilotError:
        return None, sorted(set(reasons + ["check_artifact_schema_invalid"]))
    provenance = artifact.get("provenance")
    if artifact.get("schema_version") != SCHEMA_VERSION \
            or artifact.get("artifact_kind") != kind \
            or not isinstance(artifact.get("measurements"), dict) \
            or not isinstance(provenance, dict):
        reasons.append("check_artifact_schema_invalid")
    else:
        if provenance.get("mode") != "measured" \
                or provenance.get("synthetic") is not False \
                or provenance.get("stub") is not False:
            reasons.append("stub_evidence_not_live_eligible")
        if provenance.get("producer") != expected_producer \
                or provenance.get("producer_sha256") != check["validator_sha256"]:
            reasons.append("evidence_producer_mismatch")
        if provenance.get("integration_source") != manifest["integration_source"] \
                or provenance.get("candidate_source_sha256") \
                != manifest["candidate_source_sha256"]:
            reasons.append("artifact_source_mismatch")
        try:
            _parse_time(provenance.get("collected_at"), "artifact collected_at")
        except PilotError:
            reasons.append("artifact_collection_time_invalid")
        if not isinstance(provenance.get("run_id"), str) or not provenance["run_id"]:
            reasons.append("artifact_run_identity_missing")
    return (artifact if not reasons else None), sorted(set(reasons))


def _qualification_status(
    document: dict[str, Any], manifest: dict[str, Any], root: Path,
) -> tuple[str, list[str], dict[str, Any]]:
    artifact, reasons = _verified_evidence_artifact(
        "qualification", document, manifest, root,
    )
    if artifact is None:
        return "inconclusive", reasons, {}
    measurements = artifact["measurements"]
    observations = measurements.get("relevance_observations")
    reviews = measurements.get("review_records")
    if not isinstance(observations, list) or not observations \
            or not isinstance(reviews, list):
        return "inconclusive", ["qualification_observations_missing"], {}
    observation_ids = []
    qualification_families = set()
    positive_rows = []
    negative_rows = []
    for row in observations:
        if not isinstance(row, dict) or row.get("category") not in {"positive", "negative"} \
                or not isinstance(row.get("case_id"), str) or not row["case_id"] \
                or not isinstance(row.get("family_id"), str) or not row["family_id"] \
                or row.get("route") != "task" or row.get("content_tier") != "full" \
                or any(not isinstance(row.get(field), list) for field in (
                    "expected_revisions", "baseline_revisions", "candidate_revisions",
                )):
            return "inconclusive", ["qualification_observation_schema_invalid"], {}
        revision_lists = [
            row[field] for field in (
                "expected_revisions", "baseline_revisions", "candidate_revisions",
            )
        ]
        if any(
            any(not isinstance(revision, str) or not revision for revision in revisions)
            or len(revisions) != len(set(revisions))
            for revisions in revision_lists
        ):
            return "inconclusive", ["duplicate_or_invalid_revision_emission"], {}
        observation_ids.append(row["case_id"])
        qualification_families.add(row["family_id"])
        (positive_rows if row["category"] == "positive" else negative_rows).append(row)
    if len(observation_ids) != len(set(observation_ids)) or not positive_rows \
            or not negative_rows:
        return "inconclusive", ["qualification_observation_set_invalid"], {}
    review_by_case = {
        row.get("case_id"): row for row in reviews if isinstance(row, dict)
    }
    reviewed = len(review_by_case) == len(reviews) and set(review_by_case) == set(
        observation_ids
    ) and all(
        isinstance(row.get("reviewer_id"), str) and row["reviewer_id"]
        and _is_hash(row.get("rationale_sha256"))
        and row.get("predictions_hidden") is True
        and row.get("implementation_reviewer") is False
        and row.get("exposure") == "unexposed"
        and row.get("adjudication") == "accepted"
        for row in reviews
    )

    candidate_emissions = [
        revision for row in observations for revision in row["candidate_revisions"]
    ]
    correct_emissions = sum(
        revision in row["expected_revisions"]
        for row in observations for revision in row["candidate_revisions"]
    )
    pooled_precision = correct_emissions / len(candidate_emissions) \
        if candidate_emissions else 0.0
    emitting_precision = [
        sum(revision in row["expected_revisions"] for revision in row["candidate_revisions"])
        / len(row["candidate_revisions"])
        for row in observations if row["candidate_revisions"]
    ]
    equal_prompt_precision = sum(emitting_precision) / len(emitting_precision) \
        if emitting_precision else 0.0
    coverage_gains = []
    for row in positive_rows:
        expected = set(row["expected_revisions"])
        if not expected:
            return "inconclusive", ["positive_case_without_required_revision"], {}
        baseline = len(expected & set(row["baseline_revisions"])) / len(expected)
        candidate = len(expected & set(row["candidate_revisions"])) / len(expected)
        coverage_gains.append(candidate - baseline)
    equal_task_coverage_gain = sum(coverage_gains) / len(coverage_gains)

    negative_groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in negative_rows:
        negative_groups[row["family_id"]].append(row)
    negative_payload = {
        "negative_families": [{
            "family_id": family_id,
            "bundle_size": len(rows),
            "unexpected_full_injections": sum(bool(row["candidate_revisions"]) for row in rows),
            "independent_draw": all(row.get("independent_draw") is True for row in rows),
            "sampling_distribution": rows[0].get("sampling_distribution"),
        } for family_id, rows in sorted(negative_groups.items())],
        "hard_negative_errors": sum(
            row.get("hard_negative") is True and bool(row["candidate_revisions"])
            for row in negative_rows
        ),
    }
    try:
        negative = negative_evidence_metrics(negative_payload)
    except PilotError as exc:
        return "inconclusive", [str(exc)], {}
    pilot_families = {task["family_id"] for task in manifest["tasks"]}
    live_families = measurements.get("live_task_family_ids")
    live_families_valid = (
        isinstance(live_families, list)
        and bool(live_families)
        and all(isinstance(family_id, str) and family_id for family_id in live_families)
        and len(live_families) == len(set(live_families))
    )
    reported_families_match = (
        live_families_valid and set(live_families) == pilot_families
    )
    pilot_family_overlap = qualification_families & pilot_families
    identity_pass = (
        measurements.get("label_manifest_sha256") == manifest["label_manifest_sha256"]
        and reported_families_match
        and not pilot_family_overlap
    )
    regression_lists = (
        measurements.get("authority_regression_ids"),
        measurements.get("legacy_regression_ids"),
        measurements.get("new_task_recall_notice_ids"),
    )
    regressions_valid = all(isinstance(rows, list) for rows in regression_lists)
    regressions_absent = regressions_valid and all(not rows for rows in regression_lists)
    metrics = {
        "independent_review_complete": reviewed,
        "full_precision_pooled": pooled_precision,
        "full_precision_equal_prompt": equal_prompt_precision,
        "paired_coverage_gain_equal_task": equal_task_coverage_gain,
        "negative_evidence": negative,
        "source_and_label_identity_pass": identity_pass,
        "reported_pilot_families_match_manifest": reported_families_match,
        "qualification_pilot_family_overlap": sorted(pilot_family_overlap),
        "regressions_absent": regressions_absent,
    }
    if not reviewed:
        reasons.append("independent_review_incomplete")
    if measurements.get("label_manifest_sha256") != manifest["label_manifest_sha256"]:
        reasons.append("qualification_identity_mismatch")
    if not reported_families_match:
        reasons.append("qualification_live_task_families_mismatch")
    if pilot_family_overlap:
        reasons.append("qualification_pilot_family_overlap")
    if not regressions_valid:
        reasons.append("regression_measurements_missing")
    failed = []
    if pooled_precision < 0.90 or equal_prompt_precision < 0.90 \
            or equal_task_coverage_gain < 0.10:
        failed.append("relevance_thresholds_not_met")
    if regressions_valid and not regressions_absent:
        failed.append("authority_or_legacy_regression")
    if not negative["passes"]:
        if negative["independent_families"] < 72 or not negative["independence_resolved"] \
                or not negative["equal_bundle_sizes"]:
            reasons.append("negative_population_support_inconclusive")
        else:
            failed.append("negative_population_gate_failed")
    if reasons:
        return "inconclusive", sorted(set(reasons + failed)), metrics
    if failed:
        return "fail", sorted(set(failed)), metrics
    return "pass", [], metrics


def _numeric_samples(value: object, field: str, minimum: int) -> list[float]:
    if not isinstance(value, list) or len(value) < minimum:
        raise PilotError(f"{field} requires at least {minimum} samples")
    return [_finite_nonnegative(item, field) for item in value]


def _validate_contract05_arm(
    report: dict[str, Any], frozen_manifest: dict[str, Any],
    manifest: dict[str, Any], role: str,
) -> None:
    if report.get("manifest_sha256") != sha256(CONTRACT05_MANIFEST_PATH) \
            or report.get("fixture_sha256") != frozen_manifest["fixture_sha256"] \
            or report.get("benchmark_sha256") != sha256(CONTRACT05_BENCHMARK_PATH):
        raise PilotError("contract05 frozen fixture or validator identity mismatch")
    if report.get("warmup") != frozen_manifest["warmup"] \
            or report.get("samples_per_case") \
            != frozen_manifest["samples_per_arm_per_batch"]:
        raise PilotError("contract05 arm sampling schedule mismatch")
    for field in (
        "lock_sha256", "source_lock_sha256", "source_tree_sha256_before",
        "source_tree_sha256_after",
    ):
        if not _is_hash(report.get(field)):
            raise PilotError("contract05 source identity is invalid")
    source_hashes = report.get("source_sha256")
    if not isinstance(source_hashes, dict) or not source_hashes \
            or any(not _is_hash(value) for value in source_hashes.values()):
        raise PilotError("contract05 imported source identity is invalid")
    git = report.get("git")
    if not isinstance(git, dict) or not _is_git_hash(git.get("head")) \
            or not _is_hash(git.get("diff_sha256")) \
            or not _is_hash(git.get("status_sha256")):
        raise PilotError("contract05 git source identity is invalid")
    if role == "base" and (
        git["head"] != manifest["integration_source"]["head"]
        or git["diff_sha256"] != manifest["integration_source"]["diff_sha256"]
    ):
        raise PilotError("contract05 base source does not match the approved integration")
    if role == "candidate" and report["source_tree_sha256_before"] \
            != manifest["candidate_source_sha256"]:
        raise PilotError("contract05 candidate source does not match the approved candidate")
    results = report.get("results")
    if not isinstance(results, list):
        raise PilotError("contract05 arm results are missing")
    by_name = {
        row.get("name"): row for row in results if isinstance(row, dict)
    }
    cases = {row["name"]: row for row in frozen_manifest["cases"]}
    if len(by_name) != len(results) or set(by_name) != set(cases):
        raise PilotError("contract05 arm case set is incomplete")
    for name, case in cases.items():
        row = by_name[name]
        expected = frozen_manifest["expected_behaviors"][name]
        for field in ("output_count", "output_bytes", "ledger_size", "behavior_sha256"):
            if row.get(field) != expected[field]:
                raise PilotError(f"contract05 {name} frozen behavior mismatch")
        if row.get("probes") != case["expected_probes"] \
                or row.get("route_probes") != case["expected_route_probes"]:
            raise PilotError(f"contract05 {name} route coverage mismatch")
        samples = _numeric_samples(
            row.get("samples_ms"), f"contract05 {name} samples",
            frozen_manifest["samples_per_arm_per_batch"],
        )
        if len(samples) != frozen_manifest["samples_per_arm_per_batch"]:
            raise PilotError(f"contract05 {name} sample count mismatch")
        median = contract05_benchmark.statistics.median(samples)
        p95 = contract05_benchmark._nearest_rank_p95(samples)
        if not math.isclose(
            _finite_nonnegative(row.get("median_ms"), "contract05 median"), median,
            rel_tol=1e-12, abs_tol=1e-12,
        ) or not math.isclose(
            _finite_nonnegative(row.get("p95_nearest_rank_ms"), "contract05 p95"), p95,
            rel_tol=1e-12, abs_tol=1e-12,
        ):
            raise PilotError(f"contract05 {name} timing summary mismatch")


def _contract05_campaign_status(
    measurements: dict[str, Any], manifest: dict[str, Any],
) -> tuple[str, list[str], dict[str, Any]]:
    campaign = measurements.get("campaign")
    if not isinstance(campaign, dict):
        return "inconclusive", ["contract05_campaign_missing"], {}
    try:
        frozen_manifest = load_json(CONTRACT05_MANIFEST_PATH)
        contract05_benchmark._validate_manifest(frozen_manifest)
        settings = campaign.get("settings")
        if not isinstance(settings, dict) or (
            settings.get("warmup"), settings.get("samples_per_arm_per_batch"),
            settings.get("pairs_per_phase"), settings.get("selected_cases"),
        ) != (
            frozen_manifest["warmup"], frozen_manifest["samples_per_arm_per_batch"],
            frozen_manifest["pairs_per_phase"], None,
        ):
            raise PilotError("contract05 full sampling schedule mismatch")
        phases = campaign.get("phases")
        if not isinstance(phases, dict) or set(phases) != {
            "calibration", "comparison", "drift",
        } or any(
            not isinstance(phases[phase], list)
            or len(phases[phase]) != frozen_manifest["pairs_per_phase"]
            for phase in phases
        ):
            raise PilotError("contract05 complete phase schedule is missing")
        for phase in ("calibration", "comparison", "drift"):
            for index, pair in enumerate(phases[phase]):
                expected_order = ["a", "b"] if index % 2 == 0 else ["b", "a"]
                if not isinstance(pair, dict) or pair.get("pair") != index + 1 \
                        or pair.get("order") != expected_order \
                        or not isinstance(pair.get("a"), dict) \
                        or not isinstance(pair.get("b"), dict):
                    raise PilotError("contract05 pair order or identity mismatch")
                _validate_contract05_arm(pair["a"], frozen_manifest, manifest, "base")
                role_b = "candidate" if phase == "comparison" else "base"
                _validate_contract05_arm(pair["b"], frozen_manifest, manifest, role_b)
        contract05_benchmark._assert_role_sources_stable(phases)
        all_reports = [
            pair[arm] for phase in phases.values() for pair in phase for arm in ("a", "b")
        ]
        contract05_benchmark._assert_reports_compatible(all_reports)
        calibration = contract05_benchmark._calibration(phases["calibration"])
        comparison = contract05_benchmark._evaluate_comparison(
            phases["comparison"], calibration,
        )
        drift = contract05_benchmark._evaluate_drift(phases["drift"], calibration)
    except (AssertionError, IndexError, KeyError, PilotError, TypeError, ValueError) as exc:
        return "inconclusive", [f"contract05_campaign_invalid:{exc}"], {}

    calibration_stable = all(
        row[stat]["stable"] for row in calibration.values()
        for stat in contract05_benchmark.STATS
    )
    comparison_inconclusive = any(
        row[stat]["status"] == "inconclusive" for row in comparison.values()
        for stat in contract05_benchmark.STATS
    )
    controls_pass = all(
        row[stat]["status"] == "pass" and not row[stat]["large_slowdown_alarm"]
        for row in comparison.values() for stat in contract05_benchmark.STATS
    )
    dense_pass = all(
        comparison["dense_full"][stat]["dense_target_met"]
        for stat in contract05_benchmark.STATS
    )
    drift_pass = all(
        row[stat]["status"] == "pass" for row in drift.values()
        for stat in contract05_benchmark.STATS
    )
    metrics = {
        "pairs_per_phase": frozen_manifest["pairs_per_phase"],
        "samples_per_arm_per_batch": frozen_manifest["samples_per_arm_per_batch"],
        "case_count": len(frozen_manifest["cases"]),
        "calibration_stable": calibration_stable,
        "control_rules_pass": controls_pass,
        "dense_improvement_targets_pass": dense_pass,
        "post_run_drift_pass": drift_pass,
        "large_slowdown_alarms": sorted(
            f"{name}:{stat}" for name, row in comparison.items()
            for stat in contract05_benchmark.STATS if row[stat]["large_slowdown_alarm"]
        ),
    }
    if not calibration_stable:
        return "inconclusive", ["contract05_noisy_calibration"], metrics
    if comparison_inconclusive or not drift_pass:
        return "inconclusive", ["contract05_timing_or_drift_inconclusive"], metrics
    if not controls_pass or not dense_pass:
        return "fail", ["contract05_release_gate_not_met"], metrics
    return "pass", [], metrics


def _performance_status(
    kind: str, document: dict[str, Any], manifest: dict[str, Any], root: Path,
) -> tuple[str, list[str], dict[str, Any]]:
    artifact, reasons = _verified_evidence_artifact(kind, document, manifest, root)
    if artifact is None:
        return "inconclusive", reasons, {}
    measurements = artifact["measurements"]
    machine = measurements.get("fixed_machine")
    if not isinstance(machine, dict) or any(
        not isinstance(machine.get(field), str) or not machine[field]
        for field in ("machine_id", "os", "cpu")
    ):
        return "inconclusive", ["fixed_machine_identity_missing"], {}
    if measurements.get("coverage_instrumentation") is not False:
        return "inconclusive", ["coverage_instrumentation_not_excluded"], {}
    if kind == "contract05_performance":
        status, reasons, metrics = _contract05_campaign_status(measurements, manifest)
        metrics["fixed_machine"] = machine
        return status, reasons, metrics

    try:
        warm = _numeric_samples(measurements.get("warm_hook_ms"), "warm_hook_ms", 20)
        cold = _numeric_samples(measurements.get("cold_hook_ms"), "cold_hook_ms", 5)
        missing = _numeric_samples(
            measurements.get("missing_index_hook_ms"), "missing_index_hook_ms", 5,
        )
        prepared = _parse_time(measurements.get("requests_prepared_at"), "prepared_at")
        started = _parse_time(measurements.get("measurement_started_at"), "started_at")
    except PilotError as exc:
        return "inconclusive", [str(exc)], {}
    forbidden_work = measurements.get("prompt_path_work")
    if not isinstance(forbidden_work, dict) or set(forbidden_work) != {
        "model", "network", "process", "git", "store_lock",
    } or any(value is not False for value in forbidden_work.values()):
        return "inconclusive", ["prompt_path_work_measurement_invalid"], {}
    if prepared > started:
        return "inconclusive", ["requests_not_precomputed"], {}
    p95 = _percentile(warm, 0.95)
    p99 = _percentile(warm, 0.99)
    metrics = {
        "warm_hook_p95_ms": p95,
        "warm_hook_p99_ms": p99,
        "warm_samples": len(warm),
        "cold_samples": len(cold),
        "missing_index_samples": len(missing),
        "context_bytes": measurements.get("context_bytes"),
        "estimated_tokens": measurements.get("estimated_tokens"),
        "fixed_machine": machine,
    }
    failed = []
    if p95 > 10:
        failed.append("warm_p95_exceeds_10ms")
    if p99 > 25:
        failed.append("warm_p99_exceeds_25ms")
    return ("fail", failed, metrics) if failed else ("pass", [], metrics)


def _isolation_status(
    document: dict[str, Any], manifest: dict[str, Any], root: Path,
) -> tuple[str, list[str], dict[str, Any]]:
    artifact, reasons = _verified_evidence_artifact("isolation", document, manifest, root)
    if artifact is None:
        return "inconclusive", reasons, {}
    measurements = artifact["measurements"]
    profile = measurements.get("profile")
    probes = measurements.get("probes")
    if not isinstance(profile, dict) \
            or profile.get("boundary_kind") not in {"container", "vm", "os_sandbox"} \
            or not _is_hash(profile.get("profile_sha256")) \
            or profile.get("candidate_credentials_present") is not False \
            or not isinstance(probes, list):
        return "inconclusive", ["isolation_measurement_schema_invalid"], {}
    by_id = {probe.get("probe_id"): probe for probe in probes if isinstance(probe, dict)}
    if len(by_id) != len(probes) or set(by_id) != ISOLATION_PROBES:
        return "inconclusive", ["isolation_probe_set_incomplete"], {}
    invalid = [
        probe_id for probe_id, probe in by_id.items()
        if probe.get("attempted") is not True
        or probe.get("expected") != "pass"
        or probe.get("observed") not in {"pass", "fail"}
        or not isinstance(probe.get("mechanism"), str) or not probe["mechanism"]
    ]
    if invalid:
        return "inconclusive", ["isolation_probe_schema_invalid"], {}
    failed = sorted(
        probe_id for probe_id, probe in by_id.items() if probe["observed"] != "pass"
    )
    metrics = {
        "boundary_kind": profile["boundary_kind"],
        "probe_count": len(probes),
        "failed_probes": failed,
    }
    return ("fail", [f"{probe}_failed" for probe in failed], metrics) \
        if failed else ("pass", [], metrics)


def evaluate_readiness(manifest_path: Path) -> dict[str, Any]:
    manifest = load_manifest(manifest_path)
    root = manifest_path.resolve().parent
    gates = {}
    for kind in GATE_KINDS:
        ref = manifest["evidence"][kind]
        path = _safe_relative(root, ref["path"], f"{kind} evidence path")
        base = {
            "status": "not_run", "reasons": ["evidence_file_missing"],
            "path": ref["path"], "expected_sha256": ref["sha256"],
            "actual_sha256": None, "metrics": {},
        }
        if not path.is_file():
            gates[kind] = base
            continue
        actual = sha256(path)
        base["actual_sha256"] = actual
        if actual != ref["sha256"]:
            base.update(status="inconclusive", reasons=["evidence_hash_mismatch"])
            gates[kind] = base
            continue
        try:
            document = load_json(path)
            if kind == "qualification":
                status, reasons, metrics = _qualification_status(document, manifest, root)
            elif kind in {"contract05_performance", "route_performance"}:
                status, reasons, metrics = _performance_status(
                    kind, document, manifest, root,
                )
            else:
                status, reasons, metrics = _isolation_status(document, manifest, root)
        except PilotError as exc:
            status, reasons, metrics = "inconclusive", [str(exc)], {}
        base.update(status=status, reasons=reasons, metrics=metrics)
        gates[kind] = base
    return gates


def validate_canaries(rows: list[dict[str, Any]], manifest: dict[str, Any]) -> dict[str, Any]:
    by_arm = {row.get("arm"): row for row in rows if isinstance(row, dict)}
    reasons = []
    if set(by_arm) != set(ARMS) or len(rows) != len(ARMS):
        return {"status": "inconclusive", "reasons": ["two_canary_sessions_required"]}
    expected_ordinary = manifest["canaries"]["ordinary_revision_id"]
    expected_legacy = manifest["canaries"]["legacy_revision_id"]
    for arm in ARMS:
        row = by_arm[arm]
        if row.get("run_id") != f"{manifest['campaign_id']}:canary:{arm}" \
                or not isinstance(row.get("invocation_id"), str) \
                or not row["invocation_id"]:
            reasons.append(f"{arm}_invocation_identity_invalid")
        if row.get("hook_success") is not True:
            reasons.append(f"{arm}_hook_not_proven")
        if row.get("session_interruptions") or row.get("user_notices"):
            reasons.append(f"{arm}_unexpected_interruption")
        prompts = row.get("prompts")
        if not isinstance(prompts, list) or len(prompts) != 2:
            reasons.append(f"{arm}_prompt_sequence_invalid")
            continue
        ordinary, legacy = prompts
        if ordinary.get("kind") != "ordinary" or legacy.get("kind") != "legacy":
            reasons.append(f"{arm}_prompt_order_invalid")
            continue
        if ordinary.get("initial_standing") or ordinary.get("initial_working_set"):
            reasons.append(f"{arm}_ordinary_preexisting")
        emitted = ordinary.get("received_revisions")
        if arm == "baseline":
            if emitted != [] or ordinary.get("task_route_receipt"):
                reasons.append("baseline_unexpected_task_delivery")
        elif emitted != [expected_ordinary] \
                or ordinary.get("task_route_receipt") is not True \
                or ordinary.get("host_receipt") is not True \
                or ordinary.get("tier") != "full" \
                or ordinary.get("origin") != "ordinary_task_v1" \
                or ordinary.get("authority_labels_valid") is not True:
            reasons.append("candidate_task_delivery_invalid")
        if legacy.get("received_revisions") != [expected_legacy] \
                or legacy.get("origin") != "legacy" \
                or legacy.get("host_receipt") is not True:
            reasons.append(f"{arm}_legacy_control_invalid")
        if legacy.get("suppressed_by_working_set"):
            reasons.append(f"{arm}_legacy_control_suppressed")
    return {"status": "pass" if not reasons else "fail", "reasons": sorted(set(reasons))}


def _assigned_outcome(row: dict[str, Any]) -> str:
    value = row.get("outcome", "unknown")
    if value not in OUTCOME_STATES:
        raise PilotError("outcome must be success, failure, or unknown")
    return value


def paired_outcome_metrics(
    schedule: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    assigned = [row for row in schedule if row["kind"] == "task"]
    expected_ids = {row["run_id"] for row in assigned}
    observed: dict[str, dict[str, Any]] = {}
    for row in outcomes:
        run_id = row.get("run_id")
        if run_id not in expected_ids or run_id in observed:
            raise PilotError("outcomes must name each assigned run at most once")
        observed[run_id] = row
    rows = []
    for assignment in assigned:
        supplied = observed.get(assignment["run_id"], {})
        reported_outcome = _assigned_outcome(supplied)
        required = assignment["required_checks"]
        checks = supplied.get("checks", [])
        grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for check in checks:
            if isinstance(check, dict):
                grouped[check.get("check_id")].append(check)
        valid_checks = {
            check_id: grouped[check_id][0]
            for check_id in required
            if len(grouped.get(check_id, [])) == 1
            and grouped[check_id][0].get("status") in {"pass", "fail"}
            and grouped[check_id][0].get("evidence_valid") is True
        }
        verification_complete = (
            supplied.get("invocation_complete") is True
            and supplied.get("artifact_complete") is True
            and set(required) == set(valid_checks)
        )
        if any(check["status"] == "fail" for check in valid_checks.values()):
            outcome = "failure"
        elif verification_complete and all(
            check["status"] == "pass" for check in valid_checks.values()
        ):
            outcome = "success"
        else:
            outcome = "unknown"
        rows.append({
            **assignment,
            "outcome": outcome,
            "reported_outcome": reported_outcome,
            "verified_check_ids": sorted(valid_checks),
            "verification_complete": verification_complete,
            "wall_time_ms": supplied.get("wall_time_ms"),
            "input_tokens": supplied.get("input_tokens"),
            "output_tokens": supplied.get("output_tokens"),
            "actual_cost": supplied.get("actual_cost"),
            "delivery": copy.deepcopy(supplied.get("delivery", {})),
        })

    arm_counts = {}
    for arm in ARMS:
        counts = Counter(row["outcome"] for row in rows if row["arm"] == arm)
        denominator = sum(counts.values())
        arm_counts[arm] = {
            "assigned": denominator,
            "success": counts["success"],
            "failure": counts["failure"],
            "unknown": counts["unknown"],
            "conservative_success_rate": counts["success"] / denominator,
            "upper_sensitivity_bound": (
                counts["success"] + counts["unknown"]
            ) / denominator,
            "unknown_rate": counts["unknown"] / denominator,
            "required_check_coverage": {
                "numerator": sum(
                    len(row["verified_check_ids"]) for row in rows if row["arm"] == arm
                ),
                "denominator": sum(
                    len(row["required_checks"]) for row in rows if row["arm"] == arm
                ),
            },
            "tasks_with_complete_verification": sum(
                row["verification_complete"] for row in rows if row["arm"] == arm
            ),
        }
        coverage = arm_counts[arm]["required_check_coverage"]
        coverage["ratio"] = coverage["numerator"] / coverage["denominator"] \
            if coverage["denominator"] else None

    pairs: dict[tuple[str, int], dict[str, str]] = defaultdict(dict)
    families: dict[str, set[str]] = defaultdict(set)
    task_family = {}
    for row in rows:
        pairs[(row["task_id"], row["repetition"])][row["arm"]] = row["outcome"]
        families[row["family_id"]].add(row["task_id"])
        task_family[row["task_id"]] = row["family_id"]
    task_values: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    incomplete = []
    for (task_id, repetition), pair in pairs.items():
        if set(pair) != set(ARMS):
            incomplete.append(f"{task_id}:r{repetition}")
            continue
        candidate, baseline = pair["candidate"], pair["baseline"]
        success_c = candidate == "success"
        success_b = baseline == "success"
        unknown_c = candidate == "unknown"
        unknown_b = baseline == "unknown"
        observed_difference = float(success_c) - float(success_b)
        lower = float(success_c) - float(success_b or unknown_b)
        upper = float(success_c or unknown_c) - float(success_b)
        task_values[task_id].append((observed_difference, lower, upper))
    task_means = {
        task_id: tuple(sum(values[index] for values in repetitions) / len(repetitions)
                       for index in range(3))
        for task_id, repetitions in task_values.items()
    }
    denominator = len(task_means)
    observed_difference = (
        sum(value[0] for value in task_means.values()) / denominator if denominator else None
    )
    bounds = (
        [sum(value[index] for value in task_means.values()) / denominator for index in (1, 2)]
        if denominator else [None, None]
    )
    outcome_quality_pass = all(arm_counts[arm]["unknown_rate"] <= 0.05 for arm in ARMS)
    sampling = _sampling_interval(task_means, task_family, analysis)
    measurements = {}
    for arm in ARMS:
        arm_rows = [row for row in rows if row["arm"] == arm]
        measurements[arm] = {
            "observed_runs": sum(
                any(row[field] is not None for field in (
                    "wall_time_ms", "input_tokens", "output_tokens", "actual_cost",
                ))
                for row in arm_rows
            ),
            "wall_time_ms": _sum_if_complete(arm_rows, "wall_time_ms"),
            "input_tokens": _sum_if_complete(arm_rows, "input_tokens"),
            "output_tokens": _sum_if_complete(arm_rows, "output_tokens"),
            "actual_cost": _sum_if_complete(arm_rows, "actual_cost"),
            "delivery_stages": {
                stage: sum(row["delivery"].get(stage) is True for row in arm_rows)
                for stage in ("found", "selected", "emitted", "host_received", "checked")
            },
        }
    return {
        "arms": arm_counts,
        "observed_paired_difference": observed_difference,
        "missing_outcome_bounds": bounds,
        "task_means": {
            key: {"observed": value[0], "lower": value[1], "upper": value[2]}
            for key, value in sorted(task_means.items())
        },
        "incomplete_pairs": incomplete,
        "independent_families": len(families),
        "sampling_uncertainty": sampling,
        "outcome_quality_gate": outcome_quality_pass,
        "measurements": measurements,
        "benefit_qualified": False,
        "benefit_reason": "small diagnostic pilot is never release evidence",
    }


def _sum_if_complete(rows: list[dict[str, Any]], field: str) -> float | int | None:
    values = [row[field] for row in rows]
    if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values):
        return None
    return sum(values)


def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    weight = position - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _sampling_interval(
    task_means: dict[str, tuple[float, float, float]],
    task_family: dict[str, str],
    analysis: dict[str, Any] | None,
) -> dict[str, Any]:
    if not isinstance(analysis, dict) \
            or analysis.get("sampling_method") != "family_cluster_percentile_bootstrap_v1" \
            or analysis.get("confidence") != 0.95:
        return {
            "status": "inconclusive", "method": None, "interval": [None, None],
            "reason": "predeclared_sampling_method_unavailable",
        }
    family_values: dict[str, list[float]] = defaultdict(list)
    for task_id, values in task_means.items():
        family_values[task_family[task_id]].append(values[1])
    if len(family_values) < 2:
        return {
            "status": "inconclusive",
            "method": analysis["sampling_method"],
            "interval": [None, None],
            "reason": "insufficient_independent_families",
        }
    clusters = list(family_values.values())
    rng = random.Random(_positive_int(analysis.get("sampling_seed"), "sampling_seed"))
    resamples = _positive_int(analysis.get("resamples"), "resamples")
    if resamples < 1000:
        raise PilotError("sampling analysis requires at least 1000 frozen resamples")
    estimates = []
    for _ in range(resamples):
        sampled = [rng.choice(clusters) for _ in clusters]
        estimates.append(
            sum(value for cluster in sampled for value in cluster)
            / sum(len(cluster) for cluster in sampled)
        )
    return {
        "status": "descriptive",
        "method": analysis["sampling_method"],
        "confidence": 0.95,
        "interval": [_percentile(estimates, 0.025), _percentile(estimates, 0.975)],
        "estimand": "worst_case_missing_outcome_difference",
        "independent_clusters": len(family_values),
        "resamples": resamples,
        "reason": "diagnostic_only",
    }


def validate_approval(
    approval_path: Path,
    manifest_path: Path,
    manifest: dict[str, Any],
    ledger_path: Path,
    *,
    require_current: bool = True,
) -> dict[str, Any]:
    approval = load_json(approval_path)
    if approval.get("schema_version") != SCHEMA_VERSION:
        raise PilotError("approval schema_version must be 1")
    if approval.get("campaign_id") != manifest["campaign_id"]:
        raise PilotError("approval campaign mismatch")
    if approval.get("manifest_sha256") != sha256(manifest_path):
        raise PilotError("approval manifest hash mismatch")
    if Path(approval.get("manifest_path", "")).resolve() != manifest_path.resolve():
        raise PilotError("approval is bound to a different manifest location")
    if Path(approval.get("approval_path", "")).resolve() != approval_path.resolve():
        raise PilotError("copied approval records are not authoritative")
    if Path(approval.get("ledger_path", "")).resolve() != ledger_path.resolve():
        raise PilotError("approval is bound to a different canonical ledger")
    if approval.get("operator_approved") is not True:
        raise PilotError("fresh operator approval is required")
    approved_at = _parse_time(approval.get("approved_at"), "approved_at")
    expires_at = _parse_time(approval.get("expires_at"), "expires_at")
    if approved_at >= expires_at:
        raise PilotError("approval validity interval is invalid")
    if require_current:
        now = datetime.now(timezone.utc)
        if approved_at > now:
            raise PilotError("approval is not yet valid")
        if expires_at <= now:
            raise PilotError("approval has expired")
    schedule_ids = {row["run_id"] for row in frozen_schedule(manifest)}
    approved_ids = approval.get("approved_run_ids")
    if not isinstance(approved_ids, list) or set(approved_ids) != schedule_ids \
            or len(approved_ids) != len(schedule_ids):
        raise PilotError("approval must bind every scheduled run exactly once")
    if approval.get("budget") != manifest["budget"]:
        raise PilotError("approval budget does not match the manifest")
    _nonempty(approval.get("approval_id"), "approval_id")
    _nonempty(approval.get("host"), "approved host")
    _nonempty(approval.get("model"), "approved model")
    destinations = approval.get("destination_endpoints")
    if not isinstance(destinations, list) or not destinations \
            or any(not isinstance(item, str) or not item for item in destinations):
        raise PilotError("approved destination endpoints are required")
    return approval


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(value, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _process_group_members(process_group_id: int) -> set[int]:
    """Inspect ownership so a successful leader cannot hide live descendants."""
    listing = subprocess.run(
        ["/bin/ps", "-axo", "pid=,pgid="],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if listing.returncode != 0:
        raise PilotError("stub process-group membership could not be inspected")
    members = set()
    for line in listing.stdout.splitlines():
        fields = line.split()
        if len(fields) != 2:
            continue
        try:
            process_id, group_id = map(int, fields)
        except ValueError:
            continue
        if group_id == process_group_id:
            members.add(process_id)
    return members


def _terminate_owned_process_group(process: subprocess.Popen[bytes]) -> None:
    members = _process_group_members(process.pid)
    parent_running = process.poll() is None
    descendants = members - {process.pid}
    if not parent_running and not descendants:
        return
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        if _process_group_members(process.pid) - {process.pid}:
            raise PilotError("stub process group could not be reaped") from exc


def _forbid_stub_descendants() -> None:
    """Prevent the untrusted stub from escaping supervision through a new session."""
    resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))


def run_offline_stub_process(
    command: list[str],
    *,
    sandbox_root: Path,
    cwd: Path,
    home: Path,
    timeout_seconds: float = 1.0,
    output_limit_bytes: int = 32_768,
) -> dict[str, Any]:
    """Run an untrusted offline stub with process limits, never as an OS sandbox claim."""
    if not isinstance(command, list) or not command \
            or any(not isinstance(part, str) or not part for part in command):
        raise PilotError("stub command must be a non-empty argv list")
    root = sandbox_root.resolve()
    resolved_cwd = cwd.resolve()
    resolved_home = home.resolve()
    for value, field in ((resolved_cwd, "cwd"), (resolved_home, "home")):
        if value != root and root not in value.parents:
            raise PilotError(f"stub {field} escapes the sandbox root")
    if not resolved_cwd.is_dir() or not resolved_home.is_dir():
        raise PilotError("stub cwd and home must already exist")
    if not isinstance(timeout_seconds, (int, float)) or timeout_seconds <= 0 \
            or not math.isfinite(float(timeout_seconds)):
        raise PilotError("stub timeout must be finite and positive")
    if isinstance(output_limit_bytes, bool) or not isinstance(output_limit_bytes, int) \
            or output_limit_bytes <= 0:
        raise PilotError("stub output limit must be a positive integer")

    environment = {
        "HOME": str(resolved_home),
        "TMPDIR": str(resolved_home / "tmp"),
        "PATH": "/usr/bin:/bin",
        "PYTHONNOUSERSITE": "1",
        "LC_ALL": "C.UTF-8",
    }
    Path(environment["TMPDIR"]).mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    timed_out = False
    output_limited = False
    process = subprocess.Popen(
        command,
        cwd=resolved_cwd,
        env=environment,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        preexec_fn=_forbid_stub_descendants,
    )
    streams = selectors.DefaultSelector()
    assert process.stdout is not None and process.stderr is not None
    streams.register(process.stdout, selectors.EVENT_READ, "stdout")
    streams.register(process.stderr, selectors.EVENT_READ, "stderr")
    buffers = {"stdout": bytearray(), "stderr": bytearray()}
    total = 0
    try:
        while streams.get_map():
            remaining_time = timeout_seconds - (time.monotonic() - started)
            if remaining_time <= 0:
                timed_out = True
                break
            for key, _mask in streams.select(timeout=min(0.01, remaining_time)):
                remaining_output = output_limit_bytes - total
                chunk = os.read(key.fd, min(4096, remaining_output + 1))
                if not chunk:
                    streams.unregister(key.fileobj)
                    continue
                buffers[key.data].extend(chunk[:remaining_output])
                total += min(len(chunk), remaining_output)
                if len(chunk) > remaining_output:
                    output_limited = True
                    break
            if output_limited:
                break
    finally:
        _terminate_owned_process_group(process)
        process.wait(timeout=1)
        streams.close()
        process.stdout.close()
        process.stderr.close()
    stdout = bytes(buffers["stdout"])
    stderr = bytes(buffers["stderr"])
    return {
        "returncode": process.returncode,
        "timed_out": timed_out,
        "output_limited": output_limited,
        "stdout": stdout[:output_limit_bytes].decode("utf-8", errors="replace"),
        "stderr": stderr[:output_limit_bytes].decode("utf-8", errors="replace"),
        "elapsed_ms": (time.monotonic() - started) * 1000,
        "environment_keys": sorted(environment),
        "protection": (
            "descendant creation denied; bounded process group with sanitized environment; "
            "not an OS sandbox"
        ),
    }


def parse_stub_observation(text: str, invocation_id: str) -> dict[str, Any]:
    """Accept bounded raw observations while keeping verdict construction reviewer-owned."""
    if len(text.encode("utf-8")) > 32_768:
        raise PilotError("stub observation exceeds the result-channel limit")
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PilotError("stub observation is not valid JSON") from exc
    if not isinstance(value, dict) or set(value) != {"invocation_id", "observations"}:
        raise PilotError("stub result channel accepts observations only")
    if value["invocation_id"] != invocation_id:
        raise PilotError("stub result invocation identity mismatch")
    observations = value["observations"]
    if not isinstance(observations, list) or len(observations) > 100:
        raise PilotError("stub observations must be a bounded list")
    allowed = {"probe_id", "value", "error"}
    for observation in observations:
        if not isinstance(observation, dict) or not set(observation) <= allowed \
                or not isinstance(observation.get("probe_id"), str) \
                or not observation["probe_id"]:
            raise PilotError("stub observation shape is invalid")
        if "value" not in observation and "error" not in observation:
            raise PilotError("stub observation must contain a value or error")
        if "value" in observation and "error" in observation:
            raise PilotError("stub observation cannot contain both value and error")
    probe_ids = [observation["probe_id"] for observation in observations]
    if len(probe_ids) != len(set(probe_ids)):
        raise PilotError("stub observation probe ids must be unique")
    return value


@contextlib.contextmanager
def campaign_lock(ledger_path: Path, *, blocking: bool = False) -> Iterator[None]:
    ledger_path = ledger_path.resolve()
    lock_path = ledger_path.with_name(f"{ledger_path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    operation = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
    try:
        try:
            fcntl.flock(descriptor, operation)
        except BlockingIOError as exc:
            raise CampaignBusy("another coordinator owns the campaign ledger") from exc
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def initialize_ledger(
    manifest_path: Path, approval_path: Path, ledger_path: Path
) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    approval_path = approval_path.resolve()
    ledger_path = ledger_path.resolve()
    manifest = load_manifest(manifest_path)
    approval = validate_approval(approval_path, manifest_path, manifest, ledger_path)
    manifest_hash = sha256(manifest_path)
    approval_hash = sha256(approval_path)
    with campaign_lock(ledger_path):
        if ledger_path.exists():
            existing = load_json(ledger_path)
            _validate_ledger(existing, ledger_path)
            if existing.get("manifest_sha256") != manifest_hash \
                    or existing.get("approval_sha256") != approval_hash:
                raise PilotError("canonical ledger identity conflicts with this campaign")
            return existing
        ledger = {
            "schema_version": SCHEMA_VERSION,
            "campaign_id": manifest["campaign_id"],
            "manifest_sha256": manifest_hash,
            "manifest_path": str(manifest_path),
            "approval_id": approval["approval_id"],
            "approval_sha256": approval_hash,
            "approval_path": str(approval_path.resolve()),
            "canonical_ledger_path": str(ledger_path.resolve()),
            "budget": copy.deepcopy(manifest["budget"]),
            "reserved_cost": 0.0,
            "reconciled_cost": 0.0,
            "reserved_sessions": 0,
            "runs": {
                row["run_id"]: {**row, "state": "planned", "history": ["planned"]}
                for row in frozen_schedule(manifest)
            },
        }
        atomic_write_json(ledger_path, ledger)
        return ledger


def _validate_ledger(ledger: dict[str, Any], ledger_path: Path) -> None:
    ledger_path = ledger_path.resolve()
    if ledger.get("schema_version") != SCHEMA_VERSION \
            or Path(ledger.get("canonical_ledger_path", "")).resolve() != ledger_path.resolve():
        raise PilotError("ledger is not the canonical campaign ledger")
    runs = ledger.get("runs")
    if not isinstance(runs, dict) or any(
        not isinstance(row, dict) or row.get("state") not in RUN_STATES
        for row in runs.values()
    ):
        raise PilotError("ledger run state is invalid")
    if isinstance(ledger.get("reserved_sessions"), bool) \
            or not isinstance(ledger.get("reserved_sessions"), int) \
            or ledger["reserved_sessions"] < 0:
        raise PilotError("ledger reserved session accounting is invalid")
    _finite_nonnegative(ledger.get("reserved_cost"), "ledger reserved cost")
    _finite_nonnegative(ledger.get("reconciled_cost"), "ledger reconciled cost")
    reserving = [
        row for row in runs.values()
        if row["state"] in {"launch_pending", "started", "launch_unknown"}
    ]
    if ledger["reserved_sessions"] != len(reserving) or not math.isclose(
        float(ledger["reserved_cost"]),
        sum(_finite_nonnegative(row.get("reserved_cost"), "run reserved cost")
            for row in reserving),
    ):
        raise PilotError("ledger reservations do not reconcile")
    completed = [row for row in runs.values() if row["state"] == "completed"]
    if not math.isclose(
        float(ledger["reconciled_cost"]),
        sum(_finite_nonnegative(row.get("reconciled_cost"), "run reconciled cost")
            for row in completed),
    ):
        raise PilotError("ledger completed costs do not reconcile")
    invocation_ids = [row.get("invocation_id") for row in runs.values() if row.get("invocation_id")]
    if len(invocation_ids) != len(set(invocation_ids)):
        raise PilotError("ledger invocation identities must be unique")
    if any(key != row.get("run_id") for key, row in runs.items()):
        raise PilotError("ledger run keys and immutable ids diverge")


def _load_authorized_ledger(ledger_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    ledger_path = ledger_path.resolve()
    ledger = load_json(ledger_path)
    _validate_ledger(ledger, ledger_path)
    manifest_path = Path(ledger.get("manifest_path", ""))
    approval_path = Path(ledger.get("approval_path", ""))
    manifest = load_manifest(manifest_path)
    approval = validate_approval(approval_path, manifest_path, manifest, ledger_path)
    if ledger.get("manifest_sha256") != sha256(manifest_path) \
            or ledger.get("approval_sha256") != sha256(approval_path) \
            or ledger.get("approval_id") != approval["approval_id"]:
        raise PilotError("ledger authorization identities no longer match")
    if ledger.get("budget") != manifest["budget"] \
            or ledger.get("budget") != approval["budget"]:
        raise PilotError("ledger budget does not match the approved manifest")
    if any(gate["status"] != "pass" for gate in evaluate_readiness(manifest_path).values()):
        raise PilotError("all source-bound readiness gates must pass before launch")
    expected = {row["run_id"]: row for row in frozen_schedule(manifest)}
    if set(ledger["runs"]) != set(expected):
        raise PilotError("ledger runs do not match the frozen schedule")
    immutable_fields = (
        "run_id", "kind", "arm", "repetition", "task_id", "family_id", "repo_id",
        "prompt_sha256", "validator_sha256", "required_checks",
    )
    for run_id, frozen in expected.items():
        if any(ledger["runs"][run_id].get(field) != frozen.get(field)
               for field in immutable_fields):
            raise PilotError("ledger assignment identity diverges from the frozen schedule")
    return ledger, manifest


def _reserve(
    ledger: dict[str, Any], run_id: str, approved_budget: dict[str, Any],
) -> dict[str, Any]:
    run = ledger["runs"].get(run_id)
    if not run or run["state"] != "planned":
        raise PilotError("run is not available for a first launch")
    amount = float(approved_budget["per_session_max_cost"])
    if ledger["reserved_sessions"] + 1 > approved_budget["max_sessions"] \
            or ledger["reserved_cost"] + ledger["reconciled_cost"] + amount \
            > approved_budget["max_total_cost"]:
        raise PilotError("campaign allowance cannot cover the next worst-case reservation")
    run["state"] = "launch_pending"
    run["reserved_cost"] = amount
    run["history"].append("launch_pending")
    ledger["reserved_sessions"] += 1
    ledger["reserved_cost"] += amount
    return run


def launch_stubbed_run(
    ledger_path: Path,
    run_id: str,
    start: Callable[[dict[str, Any]], dict[str, Any]],
    collect: Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]],
    *,
    failpoint: str | None = None,
) -> dict[str, Any]:
    """Exercise the durable launch protocol with a caller-supplied offline host stub."""
    ledger_path = ledger_path.resolve()
    with campaign_lock(ledger_path):
        ledger, manifest = _load_authorized_ledger(ledger_path)
        unresolved = sorted(
            row["run_id"] for row in ledger["runs"].values()
            if row["state"] in {"launch_pending", "started", "launch_unknown"}
        )
        if unresolved:
            raise PilotError(
                "authoritative reconciliation required before another launch: "
                + ", ".join(unresolved)
            )
        requested = ledger["runs"].get(run_id)
        if requested and requested.get("kind") == "task" \
                and ledger.get("canary_simulation_validation", {}).get("status") != "pass":
            raise PilotError("stub tasks require a passing offline canary simulation")
        if failpoint == "before_intent":
            raise SimulatedCrash(failpoint)
        run = _reserve(ledger, run_id, manifest["budget"])
        run["execution_mode"] = "stub"
        atomic_write_json(ledger_path, ledger)
        if failpoint == "after_intent":
            raise SimulatedCrash(failpoint)
        invocation = start(copy.deepcopy(run))
        if not isinstance(invocation, dict) or not isinstance(
            invocation.get("invocation_id"), str
        ) or not invocation["invocation_id"]:
            run["state"] = "launch_unknown"
            run["history"].append("launch_unknown")
            atomic_write_json(ledger_path, ledger)
            raise PilotError("launcher did not return a durable invocation identity")
        if failpoint == "after_spawn":
            raise SimulatedCrash(failpoint)
        run["invocation_id"] = invocation["invocation_id"]
        run["state"] = "started"
        run["history"].append("started")
        atomic_write_json(ledger_path, ledger)
        result = collect(copy.deepcopy(run), invocation)
        if failpoint == "after_result":
            raise SimulatedCrash(failpoint)
        if not isinstance(result, dict) or result.get("invocation_id") != run["invocation_id"]:
            raise PilotError("collector result identity does not match the launched invocation")
        actual_cost = _finite_nonnegative(result.get("actual_cost"), "actual cost")
        if actual_cost > run["reserved_cost"]:
            raise PilotError("actual model usage exceeded the reserved per-session maximum")
        run["result"] = copy.deepcopy(result)
        run["state"] = "completed"
        run["history"].append("completed")
        ledger["reserved_cost"] -= run["reserved_cost"]
        ledger["reserved_sessions"] -= 1
        ledger["reconciled_cost"] += actual_cost
        run["reconciled_cost"] = actual_cost
        atomic_write_json(ledger_path, ledger)
        return copy.deepcopy(run)


def reconcile_incomplete_launch(
    ledger_path: Path,
    run_id: str,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    ledger_path = ledger_path.resolve()
    with campaign_lock(ledger_path):
        ledger, _manifest = _load_authorized_ledger(ledger_path)
        run = ledger["runs"].get(run_id)
        if not run or run["state"] not in {"launch_pending", "started", "launch_unknown"}:
            raise PilotError("run does not need launch reconciliation")
        if evidence.get("authoritative_no_launch") is True:
            if run["state"] == "started" or run.get("invocation_id"):
                raise PilotError("a started invocation cannot be declared unlaunched")
            run["state"] = "planned"
            run["history"].append("proven_unlaunched")
            ledger["reserved_cost"] -= run["reserved_cost"]
            ledger["reserved_sessions"] -= 1
            run["reservation_released_by"] = _nonempty(
                evidence.get("evidence_id"), "no-launch evidence id"
            )
        elif evidence.get("invocation_id") and evidence.get("campaign_owned") is True:
            run["invocation_id"] = evidence["invocation_id"]
            run["state"] = "launch_unknown"
            run["history"].append("launch_unknown")
        else:
            run["state"] = "launch_unknown"
            if not run["history"] or run["history"][-1] != "launch_unknown":
                run["history"].append("launch_unknown")
        atomic_write_json(ledger_path, ledger)
        return copy.deepcopy(run)


def record_canary_validation(
    ledger_path: Path,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    ledger_path = ledger_path.resolve()
    with campaign_lock(ledger_path):
        ledger, manifest = _load_authorized_ledger(ledger_path)
        validation = validate_canaries(rows, manifest)
        binding = _ledger_evidence_binding(rows, ledger, kind="canary")
        if validation["status"] != "pass" or binding["status"] != "pass":
            raise PilotError("differential canary evidence is incomplete or invalid")
        ordered = sorted(rows, key=lambda row: row["run_id"])
        runs = [ledger["runs"][row["run_id"]] for row in ordered]
        modes = {run.get("execution_mode") for run in runs}
        if modes == {"stub"}:
            key = "canary_simulation_validation"
            evidence_class = "offline_simulation"
        else:
            raise PilotError(
                "live canary verification is unavailable in the offline coordinator"
            )
        ledger[key] = {
            "status": "pass",
            "evidence_class": evidence_class,
            "evidence_sha256": digest(ordered),
            "run_ids": [row["run_id"] for row in ordered],
        }
        atomic_write_json(ledger_path, ledger)
        return copy.deepcopy(ledger[key])


def _ledger_evidence_binding(
    rows: list[dict[str, Any]] | None,
    ledger: dict[str, Any] | None,
    *,
    kind: str,
    execution_mode: str | None = None,
    require_live_verification: bool = False,
) -> dict[str, Any]:
    if rows is None:
        return {"status": "not_run", "reasons": [f"{kind}_evidence_not_supplied"]}
    if ledger is None:
        return {"status": "inconclusive", "reasons": ["canonical_ledger_not_loaded"]}
    reasons = []
    for row in rows:
        run_id = row.get("run_id")
        run = ledger["runs"].get(run_id)
        if not run or run.get("kind") != kind:
            reasons.append(f"{run_id}:unassigned_run")
            continue
        if run.get("state") != "completed":
            reasons.append(f"{run_id}:run_not_completed")
            continue
        if execution_mode is not None and run.get("execution_mode") != execution_mode:
            reasons.append(f"{run_id}:execution_mode_mismatch")
        if run.get("invocation_id") != row.get("invocation_id"):
            reasons.append(f"{run_id}:invocation_identity_mismatch")
        result = run.get("result")
        if not isinstance(result, dict) \
                or result.get(f"{kind}_evidence_sha256") != digest(row):
            reasons.append(f"{run_id}:evidence_identity_mismatch")
        if require_live_verification:
            reasons.append(f"{run_id}:reviewer_owned_live_verification_unavailable")
    return {"status": "pass" if not reasons else "fail", "reasons": reasons}


def _remaining_schedule_covered(ledger: dict[str, Any] | None) -> bool:
    if ledger is None:
        return False
    if any(row["state"] in {"launch_pending", "started", "launch_unknown"}
           for row in ledger["runs"].values()):
        return False
    if any(row["state"] not in {"planned", "completed"}
           for row in ledger["runs"].values()):
        return False
    remaining = sum(
        row["state"] == "planned" for row in ledger["runs"].values()
    ) * float(ledger["budget"]["per_session_max_cost"])
    return ledger["reconciled_cost"] + remaining <= ledger["budget"]["max_total_cost"]


def build_report(
    manifest_path: Path,
    *,
    outcomes: list[dict[str, Any]] | None = None,
    canaries: list[dict[str, Any]] | None = None,
    ledger_path: Path | None = None,
) -> dict[str, Any]:
    manifest_path = manifest_path.resolve()
    ledger_path = ledger_path.resolve() if ledger_path is not None else None
    manifest = load_manifest(manifest_path)
    schedule = frozen_schedule(manifest)
    gates = evaluate_readiness(manifest_path)
    outcome_metrics = paired_outcome_metrics(schedule, outcomes or [], manifest["analysis"])
    canary_metrics = validate_canaries(canaries, manifest) if canaries is not None else {
        "status": "not_run", "reasons": ["authorized_host_canaries_not_run"],
    }
    ledger = None
    approval_current = False
    if ledger_path is not None:
        ledger = load_json(ledger_path)
        _validate_ledger(ledger, ledger_path)
        if ledger.get("manifest_sha256") != sha256(manifest_path):
            raise PilotError("ledger manifest identity does not match the report")
        approval_path = Path(ledger.get("approval_path", ""))
        approval = validate_approval(
            approval_path, manifest_path, manifest, ledger_path, require_current=False,
        )
        now = datetime.now(timezone.utc)
        approval_current = (
            _parse_time(approval["approved_at"], "approved_at") <= now
            < _parse_time(approval["expires_at"], "expires_at")
        )
        if ledger.get("approval_sha256") != sha256(approval_path) \
                or ledger.get("approval_id") != approval["approval_id"]:
            raise PilotError("ledger approval identity does not match the report")
        if ledger.get("budget") != manifest["budget"] \
                or ledger.get("budget") != approval["budget"]:
            raise PilotError("ledger budget does not match the approved manifest")
        expected_ids = {row["run_id"] for row in schedule}
        if set(ledger["runs"]) != expected_ids:
            raise PilotError("ledger runs do not match the frozen schedule")
    canary_binding = _ledger_evidence_binding(canaries, ledger, kind="canary")
    live_canary_binding = _ledger_evidence_binding(
        canaries, ledger, kind="canary", execution_mode="live",
        require_live_verification=True,
    )
    simulation_canary_binding = _ledger_evidence_binding(
        canaries, ledger, kind="canary", execution_mode="stub",
    )
    outcome_binding = _ledger_evidence_binding(outcomes, ledger, kind="task")
    outcome_metrics["ledger_binding"] = outcome_binding
    readiness_pass = all(gate["status"] == "pass" for gate in gates.values())
    engineering_ready = (
        len(schedule) == manifest["budget"]["max_sessions"]
        and len({row["run_id"] for row in schedule}) == len(schedule)
    )
    ready_for_authorized_canaries = (
        readiness_pass and ledger is not None and approval_current
    )
    measured_pilot_permitted = (
        ready_for_authorized_canaries
        and canary_metrics["status"] == "pass"
        and live_canary_binding["status"] == "pass"
        and ledger.get("canary_validation", {}).get("status") == "pass"
        and ledger["canary_validation"].get("evidence_class") == "verified_live_host"
        and ledger["canary_validation"].get("evidence_sha256") == digest(
            sorted(canaries, key=lambda row: row["run_id"])
        )
        and _remaining_schedule_covered(ledger)
    )
    states = Counter(
        row["state"] for row in ledger["runs"].values()
    ) if ledger else Counter()
    recorded_invocations = sum(
        bool(row.get("invocation_id")) for row in ledger["runs"].values()
    ) if ledger else 0
    stub_invocations = sum(
        bool(row.get("invocation_id")) and row.get("execution_mode") == "stub"
        for row in ledger["runs"].values()
    ) if ledger else 0
    claimed_live_invocations = sum(
        bool(row.get("invocation_id"))
        and row.get("execution_mode") == "live"
        for row in ledger["runs"].values()
    ) if ledger else 0
    verified_live_invocations = 0
    return {
        "schema_version": SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "campaign_id": manifest["campaign_id"],
        "manifest_sha256": sha256(manifest_path),
        "candidate_variant": CANDIDATE_VARIANT,
        "mode": "approved_campaign_report" if ledger else "offline_dry_run",
        "provenance": {
            "recorded_invocations": recorded_invocations,
            "stub_invocations": stub_invocations,
            "claimed_live_invocations": claimed_live_invocations,
            "verified_live_agent_invocations": verified_live_invocations,
            "live_agent_invocations": verified_live_invocations,
            "network_calls": None if ledger else 0,
            "real_home_mutations": None if ledger else 0,
            "contract06_fixtures_used_as_effectiveness_evidence": False,
        },
        "schedule": {
            "sessions": len(schedule),
            "canaries": sum(row["kind"] == "canary" for row in schedule),
            "task_sessions": sum(row["kind"] == "task" for row in schedule),
            "run_ids": [row["run_id"] for row in schedule],
        },
        "readiness_gates": gates,
        "canaries": {
            **canary_metrics,
            "ledger_binding": canary_binding,
            "simulation_ledger_binding": simulation_canary_binding,
            "live_ledger_binding": live_canary_binding,
        },
        "outcomes": outcome_metrics,
        "accounting": {
            "ledger_loaded": ledger is not None,
            "approval_current": approval_current,
            "states": dict(states),
            "reserved_cost": ledger.get("reserved_cost") if ledger else None,
            "reconciled_cost": ledger.get("reconciled_cost") if ledger else None,
            "reserved_sessions": ledger.get("reserved_sessions") if ledger else None,
            "remaining_schedule_covered": _remaining_schedule_covered(ledger),
        },
        "statuses": {
            "engineering_ready": engineering_ready,
            "ready_for_authorized_canaries": ready_for_authorized_canaries,
            "measured_pilot_permitted": measured_pilot_permitted,
            "pilot_complete": (
                bool(ledger)
                and states == {"completed": len(schedule)}
                and verified_live_invocations == len(schedule)
                and canary_metrics["status"] == "pass"
                and live_canary_binding["status"] == "pass"
                and outcome_binding["status"] == "pass"
                and len(outcomes or []) == len([row for row in schedule if row["kind"] == "task"])
            ),
            "offline_simulation_complete": (
                bool(ledger)
                and states == {"completed": len(schedule)}
                and stub_invocations == len(schedule)
                and canary_metrics["status"] == "pass"
                and simulation_canary_binding["status"] == "pass"
                and outcome_binding["status"] == "pass"
                and len(outcomes or []) == len([row for row in schedule if row["kind"] == "task"])
            ),
            "release_candidate": False,
        },
        "limitations": ([
            "No live host or model was invoked.",
        ] if ledger is None else [
            "Invocation counts come from the approval-bound ledger; network and real-home "
            "effects remain unknown unless independently checked.",
        ]) + [
            "Independent review, fixed-machine timing, isolation, and fresh approval must be "
            "provided as source-bound evidence.",
            "The 24-task-session pilot is diagnostic and cannot establish release readiness.",
        ],
    }


def render_text(report: dict[str, Any]) -> str:
    arms = report["outcomes"]["arms"]
    bounds = report["outcomes"]["missing_outcome_bounds"]
    lines = [
        "Contract 07 pilot readiness",
        f"campaign: {report['campaign_id']}",
        f"mode: {report['mode']}",
        f"sessions: {report['schedule']['sessions']} "
        f"({report['schedule']['canaries']} canaries, "
        f"{report['schedule']['task_sessions']} task sessions)",
        "readiness gates:",
    ]
    for name, gate in report["readiness_gates"].items():
        reason = ", ".join(gate["reasons"]) if gate["reasons"] else "all checks passed"
        lines.append(f"  {name}: {gate['status']} ({reason})")
    lines.extend([
        f"canaries: {report['canaries']['status']}",
        "offline canary simulation binding: "
        f"{report['canaries']['simulation_ledger_binding']['status']}",
        "verified live canary binding: "
        f"{report['canaries']['live_ledger_binding']['status']}",
        f"baseline outcomes: {arms['baseline']['success']} success, "
        f"{arms['baseline']['failure']} failure, {arms['baseline']['unknown']} unknown",
        f"candidate outcomes: {arms['candidate']['success']} success, "
        f"{arms['candidate']['failure']} failure, {arms['candidate']['unknown']} unknown",
        f"observed paired change: {report['outcomes']['observed_paired_difference']}",
        f"missing-outcome bounds: {bounds}",
        f"outcome-quality gate: {report['outcomes']['outcome_quality_gate']}",
        f"engineering ready: {report['statuses']['engineering_ready']}",
        f"approval current: {report['accounting']['approval_current']}",
        f"ready for authorized canaries: "
        f"{report['statuses']['ready_for_authorized_canaries']}",
        f"measured pilot permitted: {report['statuses']['measured_pilot_permitted']}",
        "release candidate: false",
        f"recorded invocations: {report['provenance']['recorded_invocations']}",
        f"offline stub invocations: {report['provenance']['stub_invocations']}",
        "verified live agent invocations: "
        f"{report['provenance']['verified_live_agent_invocations']}",
    ])
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--format", choices=("json", "text"), default="text")
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--mode", choices=("dry-run", "live"), default="dry-run",
        help="live is intentionally refused by this offline runner",
    )
    args = parser.parse_args(argv)
    if args.mode == "live":
        parser.error(
            "live execution requires a separately approved embedding with an enforced "
            "host/OS isolation launcher"
        )
    report = build_report(args.manifest)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n" \
        if args.format == "json" else render_text(report)
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
