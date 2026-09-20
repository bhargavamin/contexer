#!/usr/bin/env python3
"""Contract-06 ordinary-task retrieval and protected stub-outcome experiment.

This runner is offline-only. It exercises the real default-off prompt router and host
formatters against synthetic stores, then evaluates synthetic artifacts with frozen,
reviewer-owned validator specifications. It never invokes an agent, installs hooks, reads a
real Contexer store, or treats fixture results as evidence of user benefit.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import inspect
import json
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from benchmarks.applicability import relevance_baseline  # noqa: E402
from benchmarks.applicability import task_outcome_fixtures  # noqa: E402
from benchmarks.applicability import task_outcome_validator  # noqa: E402
from contexer import retrieval, store, working_set  # noqa: E402
from contexer.adapters import claude, cursor, gemini  # noqa: E402


SCHEMA_VERSION = 1
RUNNER_VERSION = "1"
VARIANT = retrieval._ORDINARY_TASK_VARIANT
FIXTURE_PATH = Path(__file__).with_name("task_outcome_cases.json")
SPLIT_COUNTS = {"development": 30, "validation": 15, "heldout": 15}
CATEGORIES = {"ordinary_positive", "negative", "existing_route_control"}
SUPPORTED_HOSTS = {"claude", "codex", "gemini"}
RESULT_STATES = {"success", "failure", "unknown"}
STATUS_STATES = {"pass", "fail", "unknown", "unchecked"}
REVIEW_STATES = {"unreviewed_synthetic", "independently_reviewed", "unresolved"}
SCENARIO_AXES = {
    "lexical_positives",
    "paraphrases_and_synonyms",
    "ambiguous_action_words",
    "acknowledgements",
    "general_non_project_requests",
    "quoted_task_instructions",
    "negation",
    "no_relevant_knowledge",
    "hub_files",
    "incidental_mentions",
    "more_than_three_relevant_decisions",
    "pending_conflicting_retired_withheld_guidance",
    "scope_collisions",
    "revisions",
    "repeated_prompts",
    "task_switches",
    "compaction",
}


class FixtureError(ValueError):
    """The frozen synthetic experiment input is incomplete or internally inconsistent."""


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_sha256(root: Path) -> str:
    return _digest([
        (path.relative_to(root).as_posix(), _sha256(path))
        for path in sorted(root.rglob("*.py"))
        if "__pycache__" not in path.parts
    ])


def _git_identity() -> dict[str, str]:
    def run(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(ROOT), *args], capture_output=True, text=True, check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    return {
        "head": run("rev-parse", "HEAD"),
        "diff_sha256": _digest(run("diff", "--no-ext-diff", "--binary", "HEAD")),
    }


def _safe_relative_path(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise FixtureError(f"{field} must be a non-empty relative path")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise FixtureError(f"{field} must stay within its synthetic root")
    return path


def expand_cases(fixture: dict[str, Any]) -> list[dict[str, Any]]:
    expanded = []
    for group in fixture.get("scenario_groups", []):
        shared = {key: value for key, value in group.items() if key != "cases"}
        for case in group.get("cases", []):
            expanded_case = {**shared, **case}
            if case.get("category") == "negative" and "applicable" not in case:
                expanded_case["applicable"] = []
            expanded.append(expanded_case)
    return expanded


def validate_fixture(fixture: dict[str, Any]) -> None:
    if not isinstance(fixture, dict) or fixture.get("schema_version") != SCHEMA_VERSION:
        raise FixtureError("fixture schema_version must be 1")
    if fixture.get("candidate_variant") != VARIANT:
        raise FixtureError("fixture candidate_variant does not name the frozen candidate")
    provenance = fixture.get("review_provenance")
    if not isinstance(provenance, dict) or not isinstance(
        provenance.get("independent_review_complete"), bool
    ) or not isinstance(provenance.get("holdout_opened"), bool):
        raise FixtureError("fixture requires explicit review and holdout provenance")
    if provenance["independent_review_complete"] and not (
        provenance.get("candidate_predictions_hidden_from_reviewer") is True
        and provenance["holdout_opened"] is True
    ):
        raise FixtureError(
            "independent review requires blinded labels and an explicitly opened holdout"
        )
    repositories = fixture.get("repositories")
    if not isinstance(repositories, list) or len(repositories) < 3:
        raise FixtureError("fixture requires at least three synthetic repositories")
    repo_ids: set[str] = set()
    for repo in repositories:
        if not isinstance(repo, dict):
            raise FixtureError("repositories must be objects")
        repo_id = repo.get("repo_id")
        _safe_relative_path(repo_id, "repository id")
        if not repo_id.startswith("synthetic/") or repo_id in repo_ids:
            raise FixtureError("repository ids must be unique synthetic paths")
        repo_ids.add(repo_id)
        decisions = repo.get("decisions")
        if not isinstance(decisions, list) or not decisions:
            raise FixtureError(f"{repo_id}: decisions must be a non-empty list")
        decision_ids: set[str] = set()
        revision_ids: set[str] = set()
        for decision in decisions:
            if not isinstance(decision, dict):
                raise FixtureError(f"{repo_id}: decisions must be objects")
            for field in (
                "decision_id", "revision_id", "content", "title", "status",
                "subtype", "timestamp",
            ):
                if not isinstance(decision.get(field), str) or not decision[field]:
                    raise FixtureError(f"{repo_id}: decision missing {field}")
            if decision["status"] not in {
                "approved", "suggested", "pending_approval", "ignored",
            }:
                raise FixtureError(f"{repo_id}: invalid decision status")
            if decision["decision_id"] in decision_ids:
                raise FixtureError(f"{repo_id}: duplicate decision id")
            if decision["revision_id"] in revision_ids:
                raise FixtureError(f"{repo_id}: duplicate revision id")
            decision_ids.add(decision["decision_id"])
            revision_ids.add(decision["revision_id"])

    cases = expand_cases(fixture)
    if Counter(case.get("split") for case in cases) != Counter(SPLIT_COUNTS):
        raise FixtureError("fixture must contain the frozen 30/15/15 split")
    case_ids = [case.get("case_id") for case in cases]
    if len(case_ids) != len(set(case_ids)) or any(
        not isinstance(case_id, str) or not case_id for case_id in case_ids
    ):
        raise FixtureError("case ids must be unique non-empty strings")
    coverage = fixture.get("scenario_coverage")
    if not isinstance(coverage, dict) or set(coverage) != SCENARIO_AXES:
        raise FixtureError("fixture must declare every required scenario-coverage axis")
    known_case_ids = set(case_ids)
    for axis, sources in coverage.items():
        if not isinstance(sources, dict):
            raise FixtureError(f"{axis}: malformed scenario coverage")
        fixture_cases = sources.get("fixture_cases")
        regression_tests = sources.get("regression_tests")
        if not isinstance(fixture_cases, list) or not isinstance(regression_tests, list):
            raise FixtureError(f"{axis}: scenario coverage sources must be lists")
        if not fixture_cases and not regression_tests:
            raise FixtureError(f"{axis}: scenario coverage cannot be empty")
        if any(case_id not in known_case_ids for case_id in fixture_cases):
            raise FixtureError(f"{axis}: scenario coverage names an unknown fixture case")
        for node_id in regression_tests:
            if not isinstance(node_id, str) or "::" not in node_id:
                raise FixtureError(f"{axis}: malformed regression-test node id")
            relative_path, *parts = node_id.split("::")
            test_path = ROOT / relative_path
            if not test_path.is_file() or not parts:
                raise FixtureError(f"{axis}: missing regression test")
            test_name = parts[-1].split("[")[0]
            if f"def {test_name}(" not in test_path.read_text(encoding="utf-8"):
                raise FixtureError(f"{axis}: missing regression test")
    family_splits: dict[str, set[str]] = defaultdict(set)
    for case in cases:
        for field in ("family", "repo_id", "host", "category", "prompt", "review_status"):
            if not isinstance(case.get(field), str) or not case[field]:
                raise FixtureError(f"{case.get('case_id')}: missing {field}")
        if case["repo_id"] not in repo_ids:
            raise FixtureError(f"{case['case_id']}: unknown repository")
        if case["host"] not in SUPPORTED_HOSTS | {"cursor"}:
            raise FixtureError(f"{case['case_id']}: unknown host")
        if case["category"] not in CATEGORIES:
            raise FixtureError(f"{case['case_id']}: unknown category")
        if case["review_status"] not in REVIEW_STATES:
            raise FixtureError(f"{case['case_id']}: unknown review status")
        if not isinstance(case.get("applicable"), list):
            raise FixtureError(f"{case['case_id']}: applicable must be a list")
        for label in case["applicable"]:
            if not isinstance(label, dict) or not all(
                isinstance(label.get(field), str) and label[field]
                for field in ("decision_id", "revision_id", "required_tier")
            ):
                raise FixtureError(f"{case['case_id']}: malformed applicability label")
            if label["required_tier"] not in {"prompt_full", "pointer"}:
                raise FixtureError(f"{case['case_id']}: unknown required tier")
        family_splits[case["family"]].add(case["split"])
    if any(len(splits) != 1 for splits in family_splits.values()):
        raise FixtureError("one task family cannot cross fixture splits")
    for split in ("validation", "heldout"):
        counts = Counter(case["category"] for case in cases if case["split"] == split)
        if counts != Counter({category: 5 for category in CATEGORIES}):
            raise FixtureError(f"{split} must contain five cases in every category")

    validators = fixture.get("validators")
    assignments = fixture.get("outcome_assignments")
    if not isinstance(validators, dict) or len(validators) < 6:
        raise FixtureError("at least six task-family validators are required")
    if not isinstance(assignments, list) or not assignments:
        raise FixtureError("outcome assignments must be non-empty")
    assignment_ids = [row.get("assignment_id") for row in assignments]
    if len(assignment_ids) != len(set(assignment_ids)) or any(
        not isinstance(assignment_id, str) or not assignment_id
        for assignment_id in assignment_ids
    ):
        raise FixtureError("outcome assignment ids must be unique non-empty strings")
    represented = {row.get("family") for row in assignments}
    if not set(validators).issubset(represented):
        raise FixtureError("every validator family needs a stub assignment")
    if any(row.get("expected_aggregate") not in RESULT_STATES for row in assignments):
        raise FixtureError("every outcome assignment needs a frozen aggregate expectation")
    if any(
        row.get("task_kind") == "clarification_required"
        and row.get("expected_aggregate") != "unknown"
        for row in assignments
    ):
        raise FixtureError("clarification tasks cannot receive implementation credit")
    for assignment in assignments:
        if not isinstance(assignment, dict) or not isinstance(
            assignment.get("artifact"), dict
        ) or not isinstance(assignment.get("baseline_artifact"), dict):
            raise FixtureError("outcome assignments require artifact objects")
        verification = assignment["artifact"].get("verification")
        if verification is not None and not isinstance(verification, dict):
            raise FixtureError(
                f"{assignment.get('assignment_id')}: verification must be an object"
            )
        if isinstance(verification, dict) and "import_path" in verification:
            _safe_relative_path(
                verification["import_path"],
                f"{assignment.get('assignment_id')}: import_path",
            )
        if assignment.get("task_kind") == "clarification_required":
            continue
        for field in ("implementation_fixture", "baseline_implementation_fixture"):
            fixture_id = assignment.get(field)
            if fixture_id not in task_outcome_fixtures.IMPLEMENTATIONS:
                raise FixtureError(
                    f"{assignment.get('assignment_id')}: unknown {field}"
                )
    for family, validator in validators.items():
        if family not in task_outcome_fixtures.MODULE_FILENAMES:
            raise FixtureError(f"{family}: unsupported validator family")
        if not validator.get("functional_checks") or not validator.get("conditions"):
            raise FixtureError(f"{family}: functional and condition checks are required")
        if validator.get("candidate_modules") != [
            f"artifact.{task_outcome_fixtures.MODULE_FILENAMES[family][:-3]}"
        ]:
            raise FixtureError(f"{family}: candidate module must name the executable artifact")
        check_ids = {
            check["check_id"]
            for check in validator["functional_checks"] + validator["conditions"]
        }
        if len(check_ids) != len(validator["functional_checks"] + validator["conditions"]):
            raise FixtureError(f"{family}: duplicate check id")
        functional_ids = tuple(
            check.get("check_id") for check in validator["functional_checks"]
        )
        condition_ids = tuple(check.get("check_id") for check in validator["conditions"])
        expected_ids = task_outcome_validator.CHECK_IDS[family]
        if functional_ids != expected_ids[:1] or condition_ids != expected_ids[1:]:
            raise FixtureError(f"{family}: validator manifest differs from reviewer schema")


def load_fixture(path: Path = FIXTURE_PATH) -> dict[str, Any]:
    try:
        fixture = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError(f"invalid fixture: {exc}") from exc
    validate_fixture(fixture)
    return fixture


def _repo_spec(fixture: dict[str, Any], repo_id: str) -> dict[str, Any]:
    return next(repo for repo in fixture["repositories"] if repo["repo_id"] == repo_id)


def _prompt_output(repo: str, case: dict[str, Any], arm: str) -> dict[str, Any]:
    variant = VARIANT if arm == "candidate" else None
    prompt = case["prompt"]
    session_id = f"{case['case_id']}:{arm}"
    observed_meta: dict[str, Any] = {"kind": "", "count": 0, "topics": []}
    observed_text = ""

    def lookup_meta(*args, **kwargs):
        nonlocal observed_meta, observed_text
        observed_text, observed_meta = store.get_context_for_prompt_with_meta(
            *args, **kwargs, experiment_variant=variant)
        return observed_text, observed_meta

    def lookup_text(*args, **kwargs):
        nonlocal observed_meta, observed_text
        observed_text, observed_meta = store.get_context_for_prompt_with_meta(
            *args, **kwargs, experiment_variant=variant)
        return observed_text

    raw = json.dumps({"prompt": prompt, "session_id": session_id})
    if case["host"] == "gemini":
        # The comparison is a per-prompt route experiment, not a first-session bootstrap
        # experiment. Freeze the already-started session state outside the measured request.
        marker_slug = hashlib.sha256(session_id.encode()).hexdigest()[:24]
        marker = store.sidecar_path("gemini_first", slug=marker_slug)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1", encoding="utf-8")
    started = time.perf_counter_ns()
    if case["host"] in {"claude", "codex"}:
        payload = claude._recall_payload(
            repo, raw, case["host"], prompt_lookup=lookup_meta)
        user_notice = bool(payload.get("systemMessage"))
        supported = True
    elif case["host"] == "gemini":
        payload = json.loads(gemini.before_agent(repo, raw, prompt_lookup=lookup_text))
        user_notice = bool(payload.get("systemMessage"))
        supported = True
    else:
        payload = cursor.format_prompt_passthrough()
        text = ""
        user_notice = False
        supported = False
    elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
    payload_context = payload.get("hookSpecificOutput", {}).get("additionalContext", "")
    text = observed_text

    records = working_set.records(repo, session_id) if supported else []
    entries = {entry["id"]: entry for entry in store.load(repo).get("entries", [])}
    full = []
    for record in records:
        entry = entries.get(record["id"])
        if entry is not None:
            full.append({
                "decision_id": entry["id"],
                "revision_id": entry.get("current_revision_id"),
                "tier": "prompt_full",
            })
    # The callback records metadata from the exact router call made by the adapter. The
    # adapter payload remains the authority for emitted text; no second lookup can consume
    # working-set credit or infer an experiment origin from prompt shape.
    meta = observed_meta if supported else {"kind": "", "count": 0, "topics": []}
    return {
        "arm": arm,
        "supported": supported,
        "text": text,
        "payload": payload,
        "payload_context": payload_context,
        "router_context_in_payload": not text or text in payload_context,
        "user_notice": user_notice,
        "meta": meta,
        "full_emissions": full,
        "output_bytes": len(text.encode("utf-8")),
        "output_tokens_estimated": max(0, len(text.encode("utf-8")) // 4),
        "elapsed_ms": elapsed_ms,
    }


def run_retrieval_case(fixture: dict[str, Any], case: dict[str, Any], root: Path) -> dict[str, Any]:
    repo_spec = _repo_spec(fixture, case["repo_id"])
    arms = {}
    original_store_dir = store.store_dir
    original_home = os.environ.get("HOME")
    sentinel = root / "outside-store" / "sentinel"
    sentinel.parent.mkdir(parents=True, exist_ok=True)
    sentinel.write_text("unchanged", encoding="utf-8")
    sentinel_before = _sha256(sentinel)
    try:
        for arm in ("baseline", "candidate"):
            arm_root = root / arm
            isolated_home = arm_root / "home"
            isolated_store = isolated_home / ".contexer"
            repo = arm_root / case["repo_id"]
            repo.mkdir(parents=True, exist_ok=True)
            isolated_home.mkdir(parents=True, exist_ok=True)
            os.environ["HOME"] = str(isolated_home)
            store.store_dir = lambda isolated_store=isolated_store: isolated_store
            isolated_store.parent.mkdir(parents=True, exist_ok=True)
            store.ensure_store_dir()
            entries = [relevance_baseline._entry(item) for item in repo_spec["decisions"]]
            store.save(str(repo), {"repo_path": str(repo), "entries": entries})
            store.save_global({"entries": []})
            arms[arm] = _prompt_output(str(repo), case, arm)
    finally:
        store.store_dir = original_store_dir
        if original_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = original_home
    if _sha256(sentinel) != sentinel_before:
        raise AssertionError("isolated experiment modified the out-of-scope store sentinel")
    stages = {}
    for arm, output in arms.items():
        selected = copy.deepcopy(output["full_emissions"])
        stages[arm] = {
            "admission_qualified": bool(
                output["supported"]
                and not arms["baseline"]["text"]
                and retrieval.ordinary_task_request(case["prompt"])
            ) if arm == "candidate" else False,
            "task_route_selected": output["meta"].get("origin") == VARIANT,
            "selected_revisions": selected,
            "emitted_revisions": (
                selected if output["router_context_in_payload"] and output["text"] else []
            ),
            "pointer_emitted": bool(output["text"] and not selected),
        }
    return {
        "case_id": case["case_id"],
        "split": case["split"],
        "family": case["family"],
        "repo_id": case["repo_id"],
        "host": case["host"],
        "category": case["category"],
        "review_status": case["review_status"],
        "applicable": copy.deepcopy(case["applicable"]),
        "expected_silence": case.get("expected_silence", False),
        "hard_negative": case.get("hard_negative", False),
        "arms": arms,
        "stages": stages,
    }


def _ratio(numerator: int, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _wilson(numerator: int, denominator: int, z: float = 1.96) -> list[float] | None:
    if denominator == 0:
        return None
    p = numerator / denominator
    scale = 1 + z * z / denominator
    center = (p + z * z / (2 * denominator)) / scale
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * denominator)) / denominator) / scale
    return [max(0.0, center - margin), min(1.0, center + margin)]


def _cluster_interval(rows: list[tuple[str, int, int]]) -> list[float] | None:
    """Frozen family-clustered percentile bootstrap (seed 606, 2,000 resamples)."""
    clusters: dict[str, list[tuple[int, int]]] = defaultdict(list)
    for family, numerator, denominator in rows:
        clusters[family].append((numerator, denominator))
    families = sorted(clusters)
    if not families:
        return None
    rng = random.Random(606)
    samples = []
    for _ in range(2_000):
        selected = [rng.choice(families) for _ in families]
        numerator = sum(n for family in selected for n, _ in clusters[family])
        denominator = sum(d for family in selected for _, d in clusters[family])
        if denominator:
            samples.append(numerator / denominator)
    if not samples:
        return None
    samples.sort()
    return [samples[int(0.025 * (len(samples) - 1))],
            samples[int(0.975 * (len(samples) - 1))]]


def relevance_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    heldout = [row for row in rows if row["split"] == "heldout"]
    coverage = {}
    for arm in ("baseline", "candidate"):
        by_task = []
        for row in heldout:
            if row["category"] != "ordinary_positive":
                continue
            required = {
                (item["decision_id"], item["revision_id"])
                for item in row["applicable"] if item["required_tier"] == "prompt_full"
            }
            emitted = {
                (item["decision_id"], item["revision_id"])
                for item in row["arms"][arm]["full_emissions"]
            }
            by_task.append({
                "case_id": row["case_id"], "family": row["family"],
                "numerator": len(required & emitted), "denominator": len(required),
                "whole_task": bool(required) and required <= emitted,
            })
        numerator = sum(item["numerator"] for item in by_task)
        denominator = sum(item["denominator"] for item in by_task)
        ratios = [item["numerator"] / item["denominator"] for item in by_task
                  if item["denominator"]]
        coverage[arm] = {
            "numerator": numerator,
            "denominator": denominator,
            "pooled": _ratio(numerator, denominator),
            "equal_task_mean": sum(ratios) / len(ratios) if ratios else None,
            "whole_tasks": sum(item["whole_task"] for item in by_task),
            "assigned_tasks": len(by_task),
            "family_clustered_interval": _cluster_interval([
                (item["family"], item["numerator"], item["denominator"])
                for item in by_task
            ]),
            "by_task": by_task,
        }

    precision_rows = []
    unresolved = []
    for row in heldout:
        baseline_silent = not row["arms"]["baseline"]["text"]
        if not baseline_silent or row["category"] == "existing_route_control":
            continue
        applicable = {
            (item["decision_id"], item["revision_id"])
            for item in row["applicable"]
        }
        emissions = row["arms"]["candidate"]["full_emissions"]
        if emissions and row["review_status"] != "independently_reviewed":
            unresolved.extend(
                {"case_id": row["case_id"], **item} for item in emissions
            )
        true = sum(
            (item["decision_id"], item["revision_id"]) in applicable
            for item in emissions
        )
        precision_rows.append({
            "case_id": row["case_id"], "family": row["family"],
            "numerator": true, "denominator": len(emissions),
        })
    precision_num = sum(row["numerator"] for row in precision_rows)
    precision_den = sum(row["denominator"] for row in precision_rows)
    distinct_emissions = {
        (row["case_id"], item["decision_id"], item["revision_id"])
        for row in heldout for item in row["arms"]["candidate"]["full_emissions"]
        if not row["arms"]["baseline"]["text"]
        and row["category"] != "existing_route_control"
    }
    emitting = [row for row in precision_rows if row["denominator"]]
    equal_prompt = (
        sum(row["numerator"] / row["denominator"] for row in emitting) / len(emitting)
        if emitting else None
    )
    pooled = _ratio(precision_num, precision_den)
    precision_status = "inconclusive"
    if not unresolved and pooled is not None and equal_prompt is not None:
        precision_status = "pass" if pooled >= 0.90 and equal_prompt >= 0.90 else "fail"

    negatives = [row for row in heldout if row["category"] == "negative"]
    negative_full = sum(bool(row["arms"]["candidate"]["full_emissions"]) for row in negatives)
    negative_pointers = sum(
        row["stages"]["candidate"]["pointer_emitted"] for row in negatives
    )
    hard_unexpected = [
        row["case_id"] for row in negatives
        if row["hard_negative"] and row["arms"]["candidate"]["text"]
    ]
    heldout_positive_reviewed = all(
        row["review_status"] == "independently_reviewed"
        for row in heldout if row["category"] == "ordinary_positive"
    )
    coverage_delta = (
        100 * (coverage["candidate"]["pooled"] - coverage["baseline"]["pooled"])
        if coverage["candidate"]["pooled"] is not None
        and coverage["baseline"]["pooled"] is not None else None
    )
    coverage_gate = "inconclusive"
    if heldout_positive_reviewed and coverage_delta is not None:
        coverage_gate = "pass" if coverage_delta >= 10 else "fail"
    negative_interval = _wilson(negative_full, len(negatives))
    noise_gate = "inconclusive"
    if all(row["review_status"] == "independently_reviewed" for row in negatives):
        if hard_unexpected or (negative_full and negative_full / len(negatives) > 0.05):
            noise_gate = "fail"
        elif negative_interval is not None and negative_interval[1] <= 0.05:
            noise_gate = "pass"
    return {
        "ordinary_task_coverage": {
            **coverage,
            "delta_points": coverage_delta,
            "gate": coverage_gate,
        },
        "new_route_precision": {
            "numerator": precision_num,
            "denominator": precision_den,
            "pooled": pooled,
            "equal_prompt_mean": equal_prompt,
            "emitting_prompts": len(emitting),
            "independent_families": len({row["family"] for row in emitting}),
            "family_clustered_interval": _cluster_interval([
                (row["family"], row["numerator"], row["denominator"])
                for row in precision_rows
            ]),
            "unresolved_emissions": unresolved,
            "distinct_emissions": len(distinct_emissions),
            "redundant_emissions": max(0, precision_den - len(distinct_emissions)),
            "gate": precision_status,
        },
        "no_relevant_decision": {
            "numerator": negative_full,
            "denominator": len(negatives),
            "full_injection_rate": _ratio(negative_full, len(negatives)),
            "wilson_95": negative_interval,
            "pointer_numerator": negative_pointers,
            "pointer_rate": _ratio(negative_pointers, len(negatives)),
            "hard_negative_unexpected_output": hard_unexpected,
            "gate": noise_gate,
        },
    }


def retrieval_invariants(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ordinary = [row for row in rows if row["category"] == "ordinary_positive"]
    controls = [row for row in rows if row["category"] == "existing_route_control"]
    expected_silence = [row for row in rows if row["expected_silence"]]
    task_outputs = [
        row["arms"]["candidate"] for row in rows
        if row["arms"]["candidate"]["meta"].get("origin") == VARIANT
    ]
    cursor_rows = [row for row in rows if row["host"] == "cursor"]
    return {
        "default_off_ordinary_silence": all(
            not row["arms"]["baseline"]["text"] for row in ordinary
        ),
        "existing_route_text_parity": all(
            row["arms"]["baseline"]["text"] == row["arms"]["candidate"]["text"]
            for row in controls
        ),
        "existing_route_delivery_parity": all(
            row["arms"]["baseline"]["full_emissions"]
            == row["arms"]["candidate"]["full_emissions"]
            for row in controls
        ),
        "expected_silence_preserved": all(
            not row["arms"]["candidate"]["text"] for row in expected_silence
        ),
        "task_origin_has_no_user_notice": bool(task_outputs) and all(
            not output["user_notice"] for output in task_outputs
        ),
        "router_context_reaches_adapter_payload": all(
            output["router_context_in_payload"]
            for row in rows for output in row["arms"].values()
        ),
        "cursor_explicitly_unsupported": bool(cursor_rows) and all(
            not row["arms"]["candidate"]["supported"]
            and not row["arms"]["candidate"]["text"]
            for row in cursor_rows
        ),
    }


def retrieval_breakdowns(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Transparent counts by the contract's repository, host, and family strata."""
    result = {}
    for dimension in ("repo_id", "host", "family"):
        groups = defaultdict(list)
        for row in rows:
            groups[row[dimension]].append(row)
        result[dimension] = {
            key: {
                "case_ids": [row["case_id"] for row in values],
                "cases": len(values),
                "candidate_admission_qualified": sum(
                    row["stages"]["candidate"]["admission_qualified"] for row in values
                ),
                "candidate_task_route_selected": sum(
                    row["stages"]["candidate"]["task_route_selected"] for row in values
                ),
                "candidate_full_emissions": sum(
                    len(row["stages"]["candidate"]["emitted_revisions"])
                    for row in values
                ),
                "candidate_pointer_emissions": sum(
                    row["stages"]["candidate"]["pointer_emitted"] for row in values
                ),
            }
            for key, values in sorted(groups.items())
        }
    return result


def _artifact_tree_sha256(root: Path) -> str:
    return _digest([
        (path.relative_to(root).as_posix(), _sha256(path))
        for path in sorted(root.rglob("*")) if path.is_file()
    ])


def _materialize_artifact(assignment: dict[str, Any], root: Path) -> None:
    """Write one executable synthetic candidate plus concrete verification artifacts."""
    artifact = assignment.get("artifact", {})
    fixture_id = assignment.get("implementation_fixture")
    verification = artifact.get("verification") if isinstance(artifact, dict) else None
    verification = verification if isinstance(verification, dict) else {}
    import_path = _safe_relative_path(
        verification.get("import_path", "artifact"), "artifact import_path",
    ) if fixture_id else None
    root.mkdir(parents=True)
    (root / "artifact.json").write_text(_canonical(artifact), encoding="utf-8")
    if not fixture_id:
        return
    source = assignment.get("implementation_source")
    if source is None:
        source = task_outcome_fixtures.IMPLEMENTATIONS[fixture_id]
    module_dir = root / import_path
    if not module_dir.resolve().is_relative_to(root.resolve()):
        raise FixtureError("artifact import_path escaped its synthetic root")
    module_dir.mkdir(parents=True)
    (module_dir / "__init__.py").write_text("", encoding="utf-8")
    filename = task_outcome_fixtures.MODULE_FILENAMES[assignment["family"]]
    (module_dir / filename).write_text(source.strip() + "\n", encoding="utf-8")
    if verification.get("tests_enabled") is True:
        tests = root / "tests"
        tests.mkdir()
        (tests / "test_candidate.py").write_text(
            "# frozen synthetic verification\n", encoding="utf-8",
        )
    config = "baseline\n" if verification.get("config_untouched") is True else "weakened\n"
    (root / "config.lock").write_text(config, encoding="utf-8")


def _run_behavioral_validator(
    verifier_path: Path,
    artifact_root: Path,
    family: str,
    module_name: str,
) -> list[dict[str, Any]]:
    result = subprocess.run(
        [sys.executable, "-I", str(verifier_path), str(artifact_root), family, module_name],
        cwd=verifier_path.parent,
        env={"PATH": os.environ.get("PATH", ""), "PYTHONHASHSEED": "0"},
        capture_output=True,
        text=True,
        check=False,
        timeout=5,
    )
    if result.returncode != 0:
        raise RuntimeError("behavioral validator failed")
    if len(result.stdout.encode("utf-8")) > 65_536:
        raise RuntimeError("behavioral validator output exceeded limit")
    checks = json.loads(result.stdout)
    if not isinstance(checks, list):
        raise RuntimeError("behavioral validator returned malformed results")
    return checks


def aggregate_outcome(row: dict[str, Any]) -> dict[str, Any]:
    """Apply Contract 06's deterministic truth table to independently valid results."""
    if not row.get("evidence_valid"):
        return {"result": "unknown", "reason": "invalid_evidence"}
    checks = row.get("checks") or []
    functional = [check for check in checks if check.get("kind") == "functional"]
    conditions = [check for check in checks if check.get("kind") == "condition"]
    if any(check.get("status") == "fail" for check in functional + conditions):
        return {"result": "failure", "reason": "valid_failure"}
    missing = []
    if row.get("invocation_status") != "completed":
        missing.append("invocation")
    if not row.get("artifact_complete"):
        missing.append("artifact")
    if not functional or any(check.get("status") != "pass" for check in functional):
        missing.append("functional")
    missing.extend(
        check.get("check_id", "condition")
        for check in conditions if check.get("status") != "pass"
    )
    expected = set(row.get("expected_check_ids") or [])
    observed = {check.get("check_id") for check in checks}
    missing.extend(sorted(expected - observed))
    if missing:
        return {"result": "unknown", "reason": "incomplete_evidence",
                "missing": sorted(set(missing))}
    return {"result": "success", "reason": "all_mandatory_criteria_passed"}


def evaluate_stub_assignment(
    fixture: dict[str, Any], assignment: dict[str, Any], root: Path, *,
    source_sha256: str | None = None,
) -> dict[str, Any]:
    """Snapshot one synthetic artifact and validate it across a hash-pinned boundary."""
    validator = fixture["validators"][assignment["family"]]
    family_group = next(
        group for group in fixture["scenario_groups"]
        if group["family"] == assignment["family"]
    )
    fixture_sha256 = _digest(fixture)
    split_sha256 = _digest({
        "split": family_group["split"],
        "family": family_group["family"],
        "cases": family_group["cases"],
    })
    source_sha256 = source_sha256 or _tree_sha256(ROOT / "contexer")
    verifier_root = root / "reviewer-owned"
    artifact_root = root / "agent-artifact"
    check_root = root / "validator-copy"
    verifier_root.mkdir(parents=True)
    artifact_root.mkdir(parents=True)
    check_root.mkdir(parents=True)
    verifier_path = verifier_root / "task_outcome_validator.py"
    shutil.copyfile(Path(__file__).with_name("task_outcome_validator.py"), verifier_path)
    verifier_hash = _sha256(verifier_path)
    staged_path = artifact_root / "staged"
    _materialize_artifact(assignment, staged_path)
    artifact_hash = _artifact_tree_sha256(staged_path)
    immutable_path = artifact_root / f"snapshot-{artifact_hash}"
    staged_path.rename(immutable_path)
    for path in immutable_path.rglob("*"):
        if path.is_file():
            path.chmod(0o444)

    validation_started = time.perf_counter_ns()
    evidence_valid = True
    invalid_reasons = []
    checks: list[dict[str, Any]] = []
    clarification_task = assignment.get("task_kind") == "clarification_required"
    try:
        if assignment.get("artifact_escape"):
            escaped = root / "escaped-artifact"
            shutil.copytree(immutable_path, escaped)
            shutil.rmtree(immutable_path)
            immutable_path.symlink_to(escaped)
        if assignment.get("tamper_verifier"):
            verifier_path.write_text("{}", encoding="utf-8")
        if _sha256(verifier_path) != verifier_hash:
            evidence_valid = False
            invalid_reasons.append("verifier_identity")
        if immutable_path.is_symlink() or not immutable_path.resolve().is_relative_to(
            artifact_root.resolve()
        ):
            evidence_valid = False
            invalid_reasons.append("artifact_path_escape")
        copied = check_root / "artifact"
        shutil.copytree(immutable_path, copied)
        if assignment.get("evaluator_error"):
            raise RuntimeError("synthetic evaluator failure")
        artifact = json.loads((copied / "artifact.json").read_text(encoding="utf-8"))
        if clarification_task:
            evidence = assignment.get("clarification_evidence") or {}
            checks = [
                {
                    "check_id": "clarification.required_detail",
                    "kind": "clarification",
                    "status": "pass" if evidence.get("asked_required_detail") is True
                    else "fail",
                },
                {
                    "check_id": "clarification.prohibited_change",
                    "kind": "clarification",
                    "status": "pass" if evidence.get("unsafe_change") is False
                    and artifact == assignment.get("baseline_artifact") else "fail",
                },
            ]
        else:
            checks = _run_behavioral_validator(
                verifier_path,
                copied,
                assignment["family"],
                validator["candidate_modules"][0],
            )
        if assignment.get("drop_check"):
            checks = [check for check in checks if check["check_id"] != assignment["drop_check"]]
        if assignment.get("unchecked_condition"):
            for check in checks:
                if check["kind"] == "condition":
                    check["status"] = "unchecked"
                    break
        if assignment.get("duplicate_check") and checks:
            checks.append(copy.deepcopy(checks[0]))
        if assignment.get("mislabel_check_kind") and checks:
            checks[0]["kind"] = "condition" if checks[0]["kind"] == "functional" \
                else "functional"
        if _artifact_tree_sha256(immutable_path) != artifact_hash:
            evidence_valid = False
            invalid_reasons.append("artifact_identity")
        if _sha256(verifier_path) != verifier_hash:
            evidence_valid = False
            invalid_reasons.append("verifier_identity_after")
    except Exception as exc:
        expected = (
            [("clarification.required_detail", "clarification"),
             ("clarification.prohibited_change", "clarification")]
            if clarification_task else [
                (check["check_id"],
                 "functional" if check in validator["functional_checks"] else "condition")
                for check in validator["functional_checks"] + validator["conditions"]
            ]
        )
        checks = [{"check_id": check_id, "kind": kind, "status": "unknown"}
                  for check_id, kind in expected]
        invalid_reasons.append(f"evaluator_error:{type(exc).__name__}")

    expected_kinds = (
        {
            "clarification.required_detail": "clarification",
            "clarification.prohibited_change": "clarification",
        }
        if clarification_task else {
            check["check_id"]: kind
            for kind, rows in (
                ("functional", validator["functional_checks"]),
                ("condition", validator["conditions"]),
            )
            for check in rows
        }
    )
    expected_check_ids = list(expected_kinds)
    observed_check_ids = [check.get("check_id") for check in checks]
    if len(observed_check_ids) != len(expected_check_ids) or set(observed_check_ids) != set(
        expected_check_ids
    ):
        evidence_valid = False
        invalid_reasons.append("missing_expected_result")
    if any(
        check.get("check_id") in expected_kinds
        and check.get("kind") != expected_kinds[check["check_id"]]
        for check in checks
    ):
        evidence_valid = False
        invalid_reasons.append("result_kind_mismatch")
    if any(check.get("status") not in STATUS_STATES for check in checks):
        evidence_valid = False
        invalid_reasons.append("invalid_result_status")
    evidence_integrity_valid = all(
        reason == "missing_expected_result" for reason in invalid_reasons
    )
    artifact_complete = bool(assignment.get("artifact")) and (
        _digest({
            "artifact": assignment.get("artifact"),
            "implementation_fixture": assignment.get("implementation_fixture"),
            "implementation_source": assignment.get("implementation_source"),
        }) != _digest({
            "artifact": assignment.get("baseline_artifact"),
            "implementation_fixture": assignment.get("baseline_implementation_fixture"),
            "implementation_source": None,
        })
    )
    evidence_is_valid = evidence_valid and not invalid_reasons
    clarification_result = "not_applicable"
    if clarification_task:
        if not evidence_is_valid or any(check["status"] == "unknown" for check in checks):
            clarification_result = "unverified"
        elif all(check["status"] == "pass" for check in checks):
            clarification_result = "appropriate"
        else:
            clarification_result = "inappropriate"
    row = {
        "assignment_id": assignment["assignment_id"],
        "task_id": assignment["task_id"],
        "family": assignment["family"],
        "repo_id": family_group["repo_id"],
        "split": family_group["split"],
        "arm": assignment.get("arm", "candidate_stub"),
        "repetition": assignment.get("repetition", 1),
        "mode": "stub",
        "host_configuration": "offline_structured_stub",
        "agent_configuration": {"agent": "stub", "model": None, "network": False},
        "source_sha256": source_sha256,
        "fixture_sha256": fixture_sha256,
        "split_sha256": split_sha256,
        "invocation_status": assignment.get("invocation_status", "completed"),
        "artifact_complete": artifact_complete,
        "baseline_artifact_sha256": _digest(assignment.get("baseline_artifact")),
        "artifact_sha256": artifact_hash,
        "artifact_snapshot_immutable": not immutable_path.is_symlink(),
        "validator": {
            "sha256": verifier_hash,
            "candidate_modules": copy.deepcopy(validator["candidate_modules"]),
            "protocol": "reviewer_owned_verdict_with_stateful_candidate_subprocess_v4",
            "arguments": [assignment["family"]],
            "runtime": sys.version,
            "collector_sha256": _sha256(Path(__file__)),
            "limits": {
                "validator_timeout_seconds": 5,
                "validator_stdout_bytes": 65_536,
                "candidate_call_timeout_seconds": 1,
                "candidate_stdout_bytes": 32_768,
            },
            "protection": "isolated Python mode and integrity detection; not an OS sandbox",
        },
        "expected_checks": [
            {"check_id": check_id, "kind": kind}
            for check_id, kind in expected_kinds.items()
        ],
        "expected_check_ids": expected_check_ids,
        "checks": checks,
        "evidence_valid": evidence_is_valid,
        "evidence_integrity_valid": evidence_integrity_valid,
        "invalid_reasons": invalid_reasons,
        "clarification": clarification_result,
        "latency_ms": (time.perf_counter_ns() - validation_started) / 1_000_000,
        "input_tokens": 0,
        "output_tokens": 0,
        "token_measurement": "exact stub count; no model invoked",
    }
    row["aggregate"] = (
        {"result": "unknown", "reason": "clarification_task_not_implementation"}
        if clarification_task else aggregate_outcome(row)
    )
    row["expected_aggregate"] = assignment["expected_aggregate"]
    row["expectation_met"] = row["aggregate"]["result"] == assignment["expected_aggregate"]
    row["unknown_reasons"] = (
        sorted(set(
            invalid_reasons
            + row["aggregate"].get("missing", [])
            + [row["aggregate"]["reason"]]
        ))
        if row["aggregate"]["result"] == "unknown" else []
    )
    return row


def _expected_checks(row: dict[str, Any]) -> dict[str, str]:
    return {
        expected["check_id"]: expected["kind"]
        for expected in row.get("expected_checks", [])
        if isinstance(expected, dict)
        and isinstance(expected.get("check_id"), str)
        and isinstance(expected.get("kind"), str)
    }


def _verified_check_ids(row: dict[str, Any]) -> set[str]:
    """Return uniquely observed, typed, terminal checks from an intact validator run."""
    if not row.get("evidence_integrity_valid"):
        return set()
    expected = _expected_checks(row)
    observed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for check in row.get("checks", []):
        observed[check.get("check_id")].append(check)
    return {
        check_id for check_id, kind in expected.items()
        if len(observed.get(check_id, [])) == 1
        and observed[check_id][0].get("kind") == kind
        and observed[check_id][0].get("status") in {"pass", "fail"}
    }


def _verification_complete(row: dict[str, Any]) -> bool:
    expected = _expected_checks(row)
    return bool(expected) and (
        row.get("invocation_status") == "completed"
        and row.get("artifact_complete") is True
        and _verified_check_ids(row) == set(expected)
    )


def outcome_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    implementable = [row for row in rows if row["clarification"] == "not_applicable"]
    clarification = [row for row in rows if row["clarification"] != "not_applicable"]
    counts = Counter(row["aggregate"]["result"] for row in implementable)
    if set(counts) - RESULT_STATES:
        raise AssertionError("unknown aggregate outcome state")
    assigned = len(implementable)
    required_conditions = [
        (row, check_id)
        for row in implementable
        for check_id, kind in _expected_checks(row).items()
        if kind == "condition"
    ]
    valid_conditions = sum(
        check_id in _verified_check_ids(row) for row, check_id in required_conditions
    )
    complete = sum(_verification_complete(row) for row in implementable)
    return {
        "assigned_implementable": assigned,
        "success": counts["success"],
        "failure": counts["failure"],
        "unknown": counts["unknown"],
        "conservative_success_rate": _ratio(counts["success"], assigned),
        "upper_sensitivity_bound": _ratio(counts["success"] + counts["unknown"], assigned),
        "required_condition_coverage": {
            "numerator": valid_conditions,
            "denominator": len(required_conditions),
            "ratio": _ratio(valid_conditions, len(required_conditions)),
        },
        "tasks_with_complete_verification": complete,
        "implementation_reconciliation": (
            counts["success"] + counts["failure"] + counts["unknown"]
        ),
        "assigned_total": len(rows),
        "assigned_reconciliation": (
            counts["success"] + counts["failure"] + counts["unknown"]
            + len(clarification)
        ),
        "functional_results": dict(Counter(
            check["status"] for row in implementable for check in row["checks"]
            if check["kind"] == "functional"
        )),
        "clarification": {
            "assigned": len(clarification),
            "appropriate": sum(row["clarification"] == "appropriate" for row in clarification),
            "inappropriate": sum(
                row["clarification"] == "inappropriate" for row in clarification
            ),
            "unverified": sum(row["clarification"] == "unverified" for row in clarification),
        },
    }


def build_report(fixture: dict[str, Any] | None = None) -> dict[str, Any]:
    fixture = fixture or load_fixture()
    validate_fixture(fixture)
    runner_path = Path(__file__).resolve()
    runner_before = _sha256(runner_path)
    source_before = _tree_sha256(ROOT / "contexer")
    candidate_hash = hashlib.sha256(
        inspect.getsource(retrieval.ordinary_task_request).encode("utf-8")
    ).hexdigest()
    with tempfile.TemporaryDirectory(prefix="contexer-task-outcome-") as temp:
        temp_root = Path(temp)
        retrieval_rows = [
            run_retrieval_case(fixture, case, temp_root / "retrieval" / case["case_id"])
            for case in expand_cases(fixture)
        ]
        outcome_rows = [
            evaluate_stub_assignment(
                fixture, assignment, temp_root / "outcomes" / assignment["assignment_id"],
                source_sha256=source_before,
            )
            for assignment in fixture["outcome_assignments"]
        ]
        paired_rows = []
        for assignment in fixture["outcome_assignments"]:
            if assignment["expected_aggregate"] != "success":
                continue
            baseline = copy.deepcopy(assignment)
            baseline.update({
                "assignment_id": f"{assignment['assignment_id']}-PAIRED-BASELINE",
                "arm": "baseline",
                "artifact": copy.deepcopy(assignment["baseline_artifact"]),
                "implementation_fixture": assignment["baseline_implementation_fixture"],
                "expected_aggregate": "failure",
            })
            candidate = copy.deepcopy(assignment)
            candidate.update({
                "assignment_id": f"{assignment['assignment_id']}-PAIRED-CANDIDATE",
                "arm": "candidate",
            })
            for paired in (baseline, candidate):
                paired_rows.append(evaluate_stub_assignment(
                    fixture,
                    paired,
                    temp_root / "paired" / paired["assignment_id"],
                    source_sha256=source_before,
                ))
    source_after = _tree_sha256(ROOT / "contexer")
    if source_after != source_before:
        raise AssertionError("source changed during experiment")
    if _sha256(runner_path) != runner_before:
        raise AssertionError("result collector changed during experiment")
    imported = {
        "store": Path(inspect.getfile(store)).resolve(),
        "retrieval": Path(inspect.getfile(retrieval)).resolve(),
    }
    if any(not path.is_relative_to(ROOT) for path in imported.values()):
        raise AssertionError("experiment imported candidate modules outside the pinned source root")
    source_identity = {
        "tree_sha256_before": source_before,
        "tree_sha256_after": source_after,
        "git": _git_identity(),
        "imports": {name: str(path) for name, path in imported.items()},
        "module_sha256": {name: _sha256(path) for name, path in imported.items()},
    }
    metrics = relevance_metrics(retrieval_rows)
    invariants = retrieval_invariants(retrieval_rows)
    breakdowns = retrieval_breakdowns(retrieval_rows)
    outcomes = outcome_metrics(outcome_rows)
    independent_review = fixture["review_provenance"]["independent_review_complete"] and all(
        row["review_status"] == "independently_reviewed"
        for row in retrieval_rows if row["split"] in {"validation", "heldout"}
    )
    engineering_ready = (
        len(retrieval_rows) == 60
        and all(invariants.values())
        and outcomes["assigned_reconciliation"] == outcomes["assigned_total"]
        and all(row["expectation_met"] for row in outcome_rows)
        and len(paired_rows) == 12
        and all(row["expectation_met"] for row in paired_rows)
        and all(
            not row["arms"]["candidate"]["user_notice"]
            for row in retrieval_rows
            if row["arms"]["candidate"]["meta"].get("origin") == VARIANT
        )
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "fixture_version": fixture["fixture_version"],
        "fixture_sha256": _digest(fixture),
        "runner_sha256": runner_before,
        "candidate_variant": VARIANT,
        "candidate_classifier_sha256": candidate_hash,
        "source_identity_by_arm": {
            "baseline": copy.deepcopy(source_identity),
            "candidate": copy.deepcopy(source_identity),
        },
        "provenance": {
            "synthetic": True,
            "fixture_review": copy.deepcopy(fixture["review_provenance"]),
            "isolation": {
                "distinct_temporary_home_per_arm": True,
                "distinct_store_and_session_state_per_arm": True,
                "out_of_scope_sentinel_verified": True,
                "remote_services_enabled": False,
            },
            "independent_validation_review_complete": independent_review,
            "holdout_status": (
                "independently_reviewed" if independent_review
                else "historical_unreviewed; effectiveness gates inconclusive"
            ),
            "live_agent_invocations": 0,
        },
        "statuses": {
            "engineering_ready": engineering_ready,
            "pilot_eligible": False,
            "pilot_reason": (
                "fixed-hardware timing gate not run" if independent_review
                else "independent validation and holdout review incomplete"
            ),
            "release_accepted": False,
            "release_reason": "live comparative outcomes and Contract 05 timing remain open",
        },
        "retrieval": {
            "cases": retrieval_rows,
            "metrics": metrics,
            "invariants": invariants,
            "breakdowns": breakdowns,
        },
        "outcomes": {
            "runs": outcome_rows,
            "paired_stub_runs": paired_rows,
            "metrics": outcomes,
        },
        "runtime": {
            "candidate_context_bytes": sum(
                row["arms"]["candidate"]["output_bytes"] for row in retrieval_rows
            ),
            "candidate_context_tokens_estimated": sum(
                row["arms"]["candidate"]["output_tokens_estimated"]
                for row in retrieval_rows
            ),
            "token_measurement": "four-bytes-per-token estimate; no model tokenizer invoked",
            "candidate_measured_ms": sum(
                row["arms"]["candidate"]["elapsed_ms"] for row in retrieval_rows
            ),
            "stub_validation_measured_ms": sum(
                row["latency_ms"] for row in outcome_rows + paired_rows
            ),
            "timing_gate": "not_run_fixed_hardware",
        },
        "limitations": [
            "Synthetic stub outcomes validate the harness, not agent performance.",
            "The task classifier is English-oriented and intentionally misses valid tasks.",
            "No paid agent, real-session collection, network campaign, or release is authorized.",
            "Contract 05 formal performance acceptance remains open.",
        ],
    }


def render_text(report: dict[str, Any]) -> str:
    precision = report["retrieval"]["metrics"]["new_route_precision"]
    coverage = report["retrieval"]["metrics"]["ordinary_task_coverage"]
    outcomes = report["outcomes"]["metrics"]
    return "\n".join([
        "Contract 06 ordinary-task and outcome experiment",
        f"engineering ready: {report['statuses']['engineering_ready']}",
        f"pilot eligible: {report['statuses']['pilot_eligible']} "
        f"({report['statuses']['pilot_reason']})",
        f"cases: {len(report['retrieval']['cases'])}",
        f"heldout coverage delta points: {coverage['delta_points']}",
        f"new-route precision: {precision['numerator']}/{precision['denominator']} "
        f"pooled={precision['pooled']} equal-prompt={precision['equal_prompt_mean']} "
        f"gate={precision['gate']}",
        f"stub outcomes: success={outcomes['success']} failure={outcomes['failure']} "
        f"unknown={outcomes['unknown']} assigned={outcomes['assigned_implementable']}",
        "live agent invocations: 0",
        f"release accepted: {report['statuses']['release_accepted']}",
    ])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=FIXTURE_PATH)
    parser.add_argument("--format", choices=("json", "text"), default="text")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = build_report(load_fixture(args.fixture))
    rendered = json.dumps(report, indent=2) if args.format == "json" else render_text(report)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
