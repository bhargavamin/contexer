#!/usr/bin/env python3
"""Contract-08 qualification preparation and deterministic offline collection.

This module is benchmark-local.  It imports only explicitly granted files, treats their
contents as data, runs the existing baseline/candidate retrieval seam, and produces a
source-bound artifact for Contract 07.  It has no model, network, host-launch, approval,
budget, or production-hook surface.
"""

from __future__ import annotations

import argparse
import copy
import fcntl
import hashlib
import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from benchmarks.applicability import task_outcome_experiment


SCHEMA_VERSION = 1
QUALIFIED_EVIDENCE_VERSION = 1
RUNNER_VERSION = "1"
CANDIDATE_VARIANT = "ordinary_task_v1"
SUPPORTED_HOSTS = {"claude", "codex", "gemini", "cursor"}
CASE_CATEGORIES = {"positive", "negative", "control"}
EXPOSURE_EVENTS = {
    "draft", "frozen_unopened", "opening_intent", "consumed", "exposure_uncertain",
}
EXPOSURE_TRANSITIONS = {
    None: {"draft"},
    "draft": {"draft", "frozen_unopened", "exposure_uncertain"},
    "frozen_unopened": {"opening_intent", "exposure_uncertain"},
    "opening_intent": {"consumed", "exposure_uncertain"},
    "consumed": set(),
    "exposure_uncertain": set(),
}
RUN_EVENTS = {"started", "completed", "failed_or_interrupted"}
MAX_IMPORT_FILES = 256
MAX_IMPORT_FILE_BYTES = 2 * 1024 * 1024
MAX_IMPORT_TOTAL_BYTES = 32 * 1024 * 1024
MAX_CASES = 256
MAX_REFERENCE_BYTES = 8 * 1024 * 1024
QUALIFICATION_COUNTS = {"positive": 24, "negative": 80, "control": 12}
SENSITIVE_NAMES = {
    ".env", "credentials", "credentials.json", "id_rsa", "id_ed25519",
    "service-account.json",
}


class QualificationError(ValueError):
    """A Contract-08 input is unsafe, incomplete, or internally inconsistent."""


def canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def manifest_definition_digest(manifest: dict[str, Any]) -> str:
    """Identity of the reviewed packet, excluding later review/campaign bindings."""
    reviewed = copy.deepcopy(manifest)
    reviewed.pop("review_attestation_ref", None)
    reviewed.pop("final_evaluation_binding", None)
    return digest(reviewed)


def _runtime_source_chain() -> dict[str, str]:
    """Hash the code and lock file that execute a qualification retrieval."""
    root = Path(__file__).resolve().parents[2]
    files = {
        "qualification_preparation": Path(__file__).resolve(),
        "task_outcome_experiment": Path(task_outcome_experiment.__file__).resolve(),
        "relevance_baseline": Path(
            task_outcome_experiment.relevance_baseline.__file__
        ).resolve(),
        "dependency_lock": root / "uv.lock",
    }
    package_root = root / "contexer"
    files.update({
        f"contexer/{path.relative_to(package_root).as_posix()}": path
        for path in package_root.rglob("*.py")
        if path.is_file() and not path.is_symlink()
    })
    return {name: sha256(path) for name, path in sorted(files.items())}


def _source_chain(manifest_path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    root = manifest_path.resolve().parent
    evaluated_sources = {}
    for arm in ("baseline", "candidate"):
        source = manifest["sources"][arm]
        evaluated_sources[arm] = {
            "head": source["head"],
            "diff_sha256": source["diff_sha256"],
            "patch_sha256": sha256(resolve_ref(root, source["patch_ref"], f"{arm} patch")),
            "untracked_sha256": sha256(
                resolve_ref(root, source["untracked_ref"], f"{arm} untracked")
            ),
        }
    case_inputs = [{
        "case_id": case["case_id"],
        "repo_id": case["repo_id"],
        "prompt_sha256": case["prompt_ref"]["sha256"],
        "snapshot_sha256": case["snapshot_ref"]["sha256"],
        "label_sha256": case["label_ref"]["sha256"],
        "rationale_sha256": case["rationale_ref"]["sha256"],
        "initial_context_sha256": case["initial_context_ref"]["sha256"],
    } for case in manifest["cases"]]
    return {
        "qualification_manifest_sha256": sha256(manifest_path),
        "qualification_definition_sha256": manifest_definition_digest(manifest),
        "evaluated_sources": evaluated_sources,
        "runtime_sources": _runtime_source_chain(),
        "pilot_core_sha256": manifest["pilot_core_ref"]["sha256"],
        "case_inputs_sha256": digest(case_inputs),
    }


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _hash(value: object, field: str) -> str:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise QualificationError(f"{field} must be a sha256")
    return value


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise QualificationError(f"{field} must be a non-empty string")
    return value


def _timestamp(value: object, field: str) -> str:
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise QualificationError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise QualificationError(f"{field} must include a timezone")
    return text


def load_json(path: Path) -> dict[str, Any]:
    try:
        raw = path.read_bytes()
        if len(raw) > MAX_REFERENCE_BYTES:
            raise QualificationError(f"{path} exceeds the reference size limit")
        value = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise QualificationError(f"cannot read JSON from {path}") from exc
    if not isinstance(value, dict):
        raise QualificationError(f"{path} must contain a JSON object")
    return value


def _contained(root: Path, relative: object, field: str) -> Path:
    text = _text(relative, field)
    candidate = Path(text)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise QualificationError(f"{field} must be a contained relative path")
    resolved_root = root.resolve()
    unresolved = resolved_root / candidate
    if unresolved.is_symlink():
        raise QualificationError(f"{field} cannot be a symlink")
    resolved = unresolved.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise QualificationError(f"{field} escapes its granted root")
    return resolved


def _safe_relative_identifier(value: object, field: str) -> str:
    text = _text(value, field)
    path = Path(text)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise QualificationError(f"{field} must be a contained relative identifier")
    return text


def resolve_ref(root: Path, ref: object, field: str) -> Path:
    if not isinstance(ref, dict) or set(ref) != {"path", "sha256"}:
        raise QualificationError(f"{field} must contain only path and sha256")
    path = _contained(root, ref["path"], f"{field} path")
    expected = _hash(ref["sha256"], f"{field} sha256")
    if not path.is_file() or path.is_symlink():
        raise QualificationError(f"{field} does not name a regular file")
    if path.stat().st_size > MAX_REFERENCE_BYTES or sha256(path) != expected:
        raise QualificationError(f"{field} identity mismatch")
    return path


def make_ref(root: Path, path: Path) -> dict[str, str]:
    resolved_root = root.resolve()
    resolved = path.resolve()
    if resolved_root not in resolved.parents:
        raise QualificationError("referenced file must stay below the manifest root")
    return {"path": resolved.relative_to(resolved_root).as_posix(), "sha256": sha256(resolved)}


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_create_json(path: Path, value: dict[str, Any]) -> None:
    """Create one immutable JSON artifact; never replace an earlier revision."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise QualificationError(f"refusing to overwrite immutable artifact: {path}")
    raw = json.dumps(value, indent=2, sort_keys=True) + "\n"
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as target:
            target.write(raw)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_create_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise QualificationError(f"refusing to overwrite immutable artifact: {path}")
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as target:
            target.write(value)
            target.flush()
            os.fsync(target.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def materialize_snapshot(
    source_root: Path,
    relative_paths: list[str],
    destination: Path,
    *,
    grant_id: str,
    consent_record: str,
    max_files: int = MAX_IMPORT_FILES,
    max_file_bytes: int = MAX_IMPORT_FILE_BYTES,
    max_total_bytes: int = MAX_IMPORT_TOTAL_BYTES,
) -> Path:
    """Copy bounded explicit files as inert bytes and return the snapshot manifest path."""
    _text(grant_id, "grant_id")
    _text(consent_record, "consent_record")
    if destination.exists():
        raise QualificationError("snapshot destination already exists")
    if not relative_paths or len(relative_paths) > max_files:
        raise QualificationError("snapshot file count is outside the configured bound")
    source_root = source_root.resolve()
    if not source_root.is_dir():
        raise QualificationError("source_root must be an existing directory")
    selected: list[tuple[str, Path, int]] = []
    seen: set[str] = set()
    total = 0
    for relative in relative_paths:
        path = _contained(source_root, relative, "import path")
        normalized = path.relative_to(source_root).as_posix()
        if normalized in seen:
            raise QualificationError("snapshot paths must be unique")
        seen.add(normalized)
        if path.is_symlink() or not path.is_file():
            raise QualificationError("snapshot imports only regular non-symlink files")
        if path.name.lower() in SENSITIVE_NAMES:
            raise QualificationError("sensitive file requires a separately approved data scope")
        size = path.stat().st_size
        if size > max_file_bytes:
            raise QualificationError("snapshot file exceeds the per-file byte limit")
        total += size
        if total > max_total_bytes:
            raise QualificationError("snapshot exceeds the total byte limit")
        selected.append((normalized, path, size))

    destination.mkdir(parents=True, mode=0o700)
    files_root = destination / "files"
    rows = []
    try:
        for relative, source, size in selected:
            target = files_root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            with source.open("rb") as reader, target.open("xb") as writer:
                shutil.copyfileobj(reader, writer, length=64 * 1024)
                writer.flush()
                os.fsync(writer.fileno())
            rows.append({"path": relative, "bytes": size, "sha256": sha256(target)})
        snapshot = {
            "schema_version": SCHEMA_VERSION,
            "artifact_kind": "materialized_snapshot",
            "snapshot_id": digest(rows),
            "source_grant": {
                "grant_id": grant_id,
                "consent_record": consent_record,
                "local_evaluation_only": True,
            },
            "execution": {"commands_executed": False, "network_used": False},
            "files": rows,
            "total_bytes": total,
        }
        manifest_path = destination / "snapshot.json"
        atomic_create_json(manifest_path, snapshot)
        return manifest_path
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def _history_rows(path: Path, dataset_id: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.is_symlink() or path.stat().st_size > MAX_REFERENCE_BYTES:
        raise QualificationError("exposure history is unsafe or too large")
    rows = []
    previous = "0" * 64
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise QualificationError("cannot read exposure history") from exc
    prior_event = None
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise QualificationError("exposure history contains invalid JSON") from exc
        if not isinstance(row, dict) or row.get("dataset_id") != dataset_id:
            raise QualificationError("exposure history dataset mismatch")
        event = row.get("event")
        if event not in EXPOSURE_EVENTS:
            raise QualificationError("exposure history event is invalid")
        if event not in EXPOSURE_TRANSITIONS[prior_event]:
            raise QualificationError(
                f"illegal exposure transition: {prior_event or 'start'} -> {event}"
            )
        _timestamp(row.get("timestamp"), "exposure timestamp")
        if row.get("previous_hash") != previous:
            raise QualificationError("exposure history chain is broken")
        expected = digest({key: value for key, value in row.items() if key != "record_hash"})
        if row.get("record_hash") != expected:
            raise QualificationError("exposure history record hash mismatch")
        previous = expected
        rows.append(row)
        prior_event = event
    return rows


def append_exposure(path: Path, dataset_id: str, event: str, **details: object) -> dict[str, Any]:
    if event not in EXPOSURE_EVENTS:
        raise QualificationError("unknown exposure event")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        rows = _history_rows(path, dataset_id)
        prior_event = rows[-1]["event"] if rows else None
        if event not in EXPOSURE_TRANSITIONS[prior_event]:
            if prior_event in {"opening_intent", "consumed", "exposure_uncertain"}:
                raise QualificationError(
                    "opened, consumed, or uncertain evidence can never be resealed"
                )
            raise QualificationError(
                f"illegal exposure transition: {prior_event or 'start'} -> {event}"
            )
        row = {
            "dataset_id": dataset_id,
            "event": event,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "previous_hash": rows[-1]["record_hash"] if rows else "0" * 64,
            "details": details,
        }
        row["record_hash"] = digest(row)
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, (canonical(row) + "\n").encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)
    _fsync_directory(path.parent)
    return row


def exposure_state(path: Path, dataset_id: str) -> str:
    rows = _history_rows(path, dataset_id)
    if not rows:
        return "draft"
    if any(
        row["event"] in {"opening_intent", "consumed", "exposure_uncertain"}
        for row in rows
    ):
        return "consumed"
    return rows[-1]["event"]


def _run_history_rows(path: Path, dataset_id: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.is_symlink() or path.stat().st_size > MAX_REFERENCE_BYTES:
        raise QualificationError("run history is unsafe or too large")
    previous = "0" * 64
    rows = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise QualificationError("cannot read run history") from exc
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise QualificationError("run history contains invalid JSON") from exc
        if not isinstance(row, dict) or row.get("dataset_id") != dataset_id \
                or row.get("event") not in RUN_EVENTS \
                or row.get("previous_hash") != previous:
            raise QualificationError("run history chain is invalid")
        _timestamp(row.get("timestamp"), "run timestamp")
        _text(row.get("attempt_id"), "attempt_id")
        expected = digest({key: value for key, value in row.items() if key != "record_hash"})
        if row.get("record_hash") != expected:
            raise QualificationError("run history record hash mismatch")
        previous = expected
        rows.append(row)
    return rows


def append_run_history(
    path: Path,
    dataset_id: str,
    event: str,
    attempt_id: str,
    **details: object,
) -> dict[str, Any]:
    if event not in RUN_EVENTS:
        raise QualificationError("unknown run-history event")
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_name(f"{path.name}.lock")
    lock_descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        rows = _run_history_rows(path, dataset_id)
        if event != "started" and not any(
            row["event"] == "started" and row["attempt_id"] == attempt_id for row in rows
        ):
            raise QualificationError("run completion has no durable start record")
        if event == "started" and any(row["attempt_id"] == attempt_id for row in rows):
            raise QualificationError("run attempt identity already exists")
        row = {
            "dataset_id": dataset_id,
            "event": event,
            "attempt_id": attempt_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "previous_hash": rows[-1]["record_hash"] if rows else "0" * 64,
            "details": details,
        }
        row["record_hash"] = digest(row)
        descriptor = os.open(path, os.O_APPEND | os.O_CREAT | os.O_WRONLY, 0o600)
        try:
            os.write(descriptor, (canonical(row) + "\n").encode("utf-8"))
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)
    _fsync_directory(path.parent)
    return row


def _validate_decision_snapshot(value: dict[str, Any], repo_id: str) -> None:
    if value.get("schema_version") != SCHEMA_VERSION or value.get("repo_id") != repo_id:
        raise QualificationError(f"{repo_id}: repository snapshot identity mismatch")
    _text(value.get("snapshot_id"), f"{repo_id}: snapshot_id")
    decisions = value.get("decisions")
    if not isinstance(decisions, list) or not decisions:
        raise QualificationError(f"{repo_id}: decisions must be a non-empty list")
    decision_ids: set[str] = set()
    revisions: set[str] = set()
    for decision in decisions:
        if not isinstance(decision, dict):
            raise QualificationError(f"{repo_id}: malformed decision")
        for field in (
            "decision_id", "revision_id", "content", "title", "status", "subtype",
            "timestamp", "scope",
        ):
            _text(decision.get(field), f"{repo_id}: decision {field}")
        if decision["scope"] not in {"personal", "global"}:
            raise QualificationError(f"{repo_id}: decision scope is invalid")
        if decision["status"] not in {
            "approved", "suggested", "pending_approval", "ignored"
        }:
            raise QualificationError(f"{repo_id}: decision status is invalid")
        if decision["decision_id"] in decision_ids or decision["revision_id"] in revisions:
            raise QualificationError(f"{repo_id}: duplicate decision or revision identity")
        decision_ids.add(decision["decision_id"])
        revisions.add(decision["revision_id"])
        if not isinstance(decision.get("source_files"), list):
            raise QualificationError(f"{repo_id}: source_files must be a list")
        if "proposal" not in decision or decision["proposal"] is not None \
                and not isinstance(decision["proposal"], dict):
            raise QualificationError(f"{repo_id}: proposal state must be explicit")
        if not isinstance(decision.get("conflict"), bool):
            raise QualificationError(f"{repo_id}: conflict state must be explicit")


def validate_pilot_core(path: Path) -> dict[str, Any]:
    core = load_json(path)
    root = path.resolve().parent
    if core.get("schema_version") != SCHEMA_VERSION \
            or core.get("artifact_kind") != "pilot_task_core":
        raise QualificationError("pilot core schema is invalid")
    _text(core.get("core_id"), "pilot core id")
    tasks = core.get("tasks")
    if not isinstance(tasks, list) or len(tasks) != 6:
        raise QualificationError("pilot core must freeze exactly six tasks")
    task_ids: set[str] = set()
    family_ids: set[str] = set()
    repo_ids: set[str] = set()
    for task in tasks:
        if not isinstance(task, dict):
            raise QualificationError("pilot tasks must be objects")
        task_id = _text(task.get("task_id"), "pilot task id")
        repo_id = _text(task.get("repo_id"), f"{task_id}: repo id")
        family_id = _text(task.get("family_id"), f"{task_id}: family id")
        if task_id in task_ids:
            raise QualificationError("pilot task ids must be unique")
        task_ids.add(task_id)
        family_ids.add(family_id)
        repo_ids.add(repo_id)
        prompt = resolve_ref(root, task.get("prompt_ref"), f"{task_id}: prompt")
        snapshot_path = resolve_ref(root, task.get("snapshot_ref"), f"{task_id}: snapshot")
        initial_path = resolve_ref(
            root, task.get("initial_context_ref"), f"{task_id}: initial context"
        )
        checks_path = resolve_ref(root, task.get("check_spec_ref"), f"{task_id}: checks")
        if sha256(prompt) != task.get("prompt_sha256"):
            raise QualificationError(f"{task_id}: prompt identity mismatch")
        snapshot = load_json(snapshot_path)
        _validate_decision_snapshot(snapshot, repo_id)
        if snapshot.get("snapshot_id") != task.get("snapshot_id") \
                or task.get("snapshot_sha256") != sha256(snapshot_path):
            raise QualificationError(f"{task_id}: snapshot identity mismatch")
        initial = load_json(initial_path)
        if task.get("initial_context_sha256") != sha256(initial_path):
            raise QualificationError(f"{task_id}: initial context identity mismatch")
        if set(initial) != {"schema_version", "standing_revisions", "working_set"} \
                or initial["schema_version"] != SCHEMA_VERSION \
                or not isinstance(initial["standing_revisions"], list) \
                or not isinstance(initial["working_set"], list):
            raise QualificationError(f"{task_id}: initial context is invalid")
        known_revisions = {
            (row["scope"], row["decision_id"], row["revision_id"])
            for row in snapshot["decisions"]
        }
        for row in initial["standing_revisions"]:
            if not isinstance(row, dict) or (
                row.get("scope", "personal"), row.get("decision_id"),
                row.get("revision_id"),
            ) not in known_revisions or row.get("tier") not in {"prompt_full", "pointer"}:
                raise QualificationError(f"{task_id}: standing context identity is invalid")
        for row in initial["working_set"]:
            if not isinstance(row, dict) or (
                row.get("scope", "personal"), row.get("decision_id"),
                row.get("revision_id"),
            ) not in known_revisions:
                raise QualificationError(f"{task_id}: working-set identity is invalid")
        checks = load_json(checks_path)
        functional = checks.get("functional_checks")
        conditions = checks.get("mandatory_conditions")
        if not isinstance(functional, list) or not functional \
                or not isinstance(conditions, list) or not conditions:
            raise QualificationError(f"{task_id}: checks must cover behavior and conditions")
        check_ids = [
            row.get("check_id") for row in functional + conditions if isinstance(row, dict)
        ]
        if len(check_ids) != len(functional + conditions) \
                or len(check_ids) != len(set(check_ids)) \
                or any(not isinstance(item, str) or not item for item in check_ids):
            raise QualificationError(f"{task_id}: check ids must be unique strings")
        if task.get("check_spec_sha256") != sha256(checks_path):
            raise QualificationError(f"{task_id}: check specification identity mismatch")
    if len(repo_ids) < 3 or len(family_ids) < 3:
        raise QualificationError("pilot core needs six tasks across at least three snapshots")
    return core


def _validate_label(path: Path, case: dict[str, Any], snapshot: dict[str, Any]) -> dict[str, Any]:
    label = load_json(path)
    if label.get("schema_version") != SCHEMA_VERSION \
            or label.get("case_id") != case["case_id"]:
        raise QualificationError(f"{case['case_id']}: label identity mismatch")
    applicable = label.get("applicable")
    if not isinstance(applicable, list):
        raise QualificationError(f"{case['case_id']}: applicable labels must be a list")
    known = {
        (row["decision_id"], row["revision_id"]): row
        for row in snapshot["decisions"]
    }
    seen: set[tuple[str, str]] = set()
    for row in applicable:
        if not isinstance(row, dict):
            raise QualificationError(f"{case['case_id']}: malformed applicable label")
        identity = (row.get("decision_id"), row.get("revision_id"))
        if identity in seen or identity not in known:
            raise QualificationError(f"{case['case_id']}: stale or duplicate label")
        seen.add(identity)
        if row.get("required_tier") not in {"prompt_full", "pointer"}:
            raise QualificationError(f"{case['case_id']}: required tier is invalid")
    if case["category"] == "positive" and not applicable:
        raise QualificationError(f"{case['case_id']}: positive case needs a label")
    if case["category"] == "negative" and applicable:
        raise QualificationError(f"{case['case_id']}: negative case cannot be applicable")
    for field in (
        "expected_silence", "expected_clarification", "available_but_inapplicable",
    ):
        if not isinstance(label.get(field), bool):
            raise QualificationError(f"{case['case_id']}: {field} must be explicit")
    assertions = label.get("control_assertions", [])
    if not isinstance(assertions, list):
        raise QualificationError(f"{case['case_id']}: control assertions must be a list")
    if case["category"] == "control" and not assertions:
        raise QualificationError(f"{case['case_id']}: control case needs assertions")
    for assertion in assertions:
        if not isinstance(assertion, dict) or assertion.get("arm") not in {
            "baseline", "candidate"
        } or assertion.get("field") not in {
            "supported", "user_notice", "task_route_selected", "pointer_emitted",
            "emitted_revision_ids", "authority_valid",
        } or assertion.get("operator") not in {"eq", "contains", "empty"}:
            raise QualificationError(f"{case['case_id']}: control assertion is invalid")
        _text(assertion.get("assertion_id"), f"{case['case_id']}: assertion id")
        if assertion.get("control_kind") not in {"authority", "legacy", "notice"}:
            raise QualificationError(f"{case['case_id']}: control kind is invalid")
    return label


def validate_manifest(path: Path) -> dict[str, Any]:
    manifest = load_json(path)
    root = path.resolve().parent
    if manifest.get("schema_version") != SCHEMA_VERSION \
            or manifest.get("artifact_kind") != "qualification_manifest":
        raise QualificationError("qualification manifest schema is invalid")
    dataset_id = _text(manifest.get("dataset_id"), "dataset_id")
    if manifest.get("candidate_variant") != CANDIDATE_VARIANT:
        raise QualificationError("manifest must freeze ordinary_task_v1")
    data_origin = manifest.get("data_origin")
    if data_origin not in {"development", "qualification"}:
        raise QualificationError("data_origin must be development or qualification")
    if manifest.get("execution_kind") != "deterministic_offline_retrieval":
        raise QualificationError("execution_kind must be deterministic offline retrieval")
    sources = manifest.get("sources")
    if not isinstance(sources, dict) or set(sources) != {
        "baseline", "candidate", "dependency_lock", "collector", "formatter"
    }:
        raise QualificationError("manifest must freeze every evaluated source identity")
    for arm in ("baseline", "candidate"):
        source = sources[arm]
        if not isinstance(source, dict):
            raise QualificationError(f"{arm} source must be an object")
        _text(source.get("head"), f"{arm} head")
        _hash(source.get("diff_sha256"), f"{arm} diff")
        for name in ("patch_ref", "untracked_ref"):
            resolve_ref(root, source.get(name), f"{arm} {name}")
    for name in ("dependency_lock", "collector", "formatter"):
        resolve_ref(root, sources[name], name)
    if sources["collector"]["sha256"] != sha256(Path(__file__).resolve()):
        raise QualificationError("collector identity differs from this validated owner")
    if sources["formatter"]["sha256"] != sha256(
        Path(task_outcome_experiment.__file__).resolve()
    ):
        raise QualificationError("formatter identity differs from the frozen route owner")
    if sources["dependency_lock"]["sha256"] != _runtime_source_chain()[
        "dependency_lock"
    ]:
        raise QualificationError("dependency lock differs from the executed environment")

    grants = manifest.get("source_grants")
    if not isinstance(grants, list) or not grants:
        raise QualificationError("manifest requires explicit source grants")
    grant_ids = set()
    for grant in grants:
        if not isinstance(grant, dict):
            raise QualificationError("source grants must be objects")
        grant_id = _text(grant.get("grant_id"), "source grant id")
        if grant_id in grant_ids or grant.get("local_evaluation_only") is not True:
            raise QualificationError("source grants must be unique and local-evaluation-only")
        grant_ids.add(grant_id)
        _text(grant.get("consent_record"), f"{grant_id}: consent record")
        _timestamp(grant.get("granted_at"), f"{grant_id}: granted_at")

    pilot_ref = manifest.get("pilot_core_ref")
    pilot_path = resolve_ref(root, pilot_ref, "pilot core")
    pilot_core = validate_pilot_core(pilot_path)
    pilot_families = {task["family_id"] for task in pilot_core["tasks"]}
    development_families = set(manifest.get("development_family_ids", []))
    if any(not isinstance(item, str) or not item for item in development_families):
        raise QualificationError("development family ids must be strings")

    cases = manifest.get("cases")
    if not isinstance(cases, list) or not cases or len(cases) > MAX_CASES:
        raise QualificationError("qualification cases are missing or exceed the case limit")
    case_ids: set[str] = set()
    families: set[str] = set()
    repo_ids: set[str] = set()
    counts = {category: 0 for category in CASE_CATEGORIES}
    aliases = manifest.get("family_aliases", [])
    if not isinstance(aliases, list) or any(
        not isinstance(row, list) or len(row) < 2
        or any(not isinstance(item, str) or not item for item in row)
        for row in aliases
    ):
        raise QualificationError("family aliases must be documented string groups")
    for case in cases:
        if not isinstance(case, dict):
            raise QualificationError("cases must be objects")
        case_id = _text(case.get("case_id"), "case id")
        family_id = _text(case.get("family_id"), f"{case_id}: family id")
        repo_id = _safe_relative_identifier(case.get("repo_id"), f"{case_id}: repo id")
        if case_id in case_ids:
            raise QualificationError("case ids must be unique")
        case_ids.add(case_id)
        families.add(family_id)
        repo_ids.add(repo_id)
        category = case.get("category")
        if category not in CASE_CATEGORIES:
            raise QualificationError(f"{case_id}: category is invalid")
        counts[category] += 1
        if case.get("host") not in SUPPORTED_HOSTS:
            raise QualificationError(f"{case_id}: host is invalid")
        if case.get("dataset_membership") != data_origin:
            raise QualificationError(f"{case_id}: dataset membership mismatch")
        if case.get("source_grant_id") not in grant_ids:
            raise QualificationError(f"{case_id}: source grant is missing")
        prompt = resolve_ref(root, case.get("prompt_ref"), f"{case_id}: prompt")
        if not prompt.read_bytes() or sha256(prompt) != case.get("prompt_sha256"):
            raise QualificationError(f"{case_id}: prompt identity mismatch")
        snapshot_path = resolve_ref(root, case.get("snapshot_ref"), f"{case_id}: snapshot")
        snapshot = load_json(snapshot_path)
        _validate_decision_snapshot(snapshot, repo_id)
        if snapshot.get("snapshot_id") != case.get("snapshot_id"):
            raise QualificationError(f"{case_id}: snapshot identity mismatch")
        label_path = resolve_ref(root, case.get("label_ref"), f"{case_id}: label")
        rationale_path = resolve_ref(
            root, case.get("rationale_ref"), f"{case_id}: rationale"
        )
        if not rationale_path.read_bytes():
            raise QualificationError(f"{case_id}: rationale cannot be empty")
        _validate_label(label_path, case, snapshot)
        initial_path = resolve_ref(
            root, case.get("initial_context_ref"), f"{case_id}: initial context"
        )
        initial = load_json(initial_path)
        if initial.get("schema_version") != SCHEMA_VERSION \
                or not isinstance(initial.get("standing_revisions"), list) \
                or not isinstance(initial.get("working_set"), list):
            raise QualificationError(f"{case_id}: initial context must be explicit")
        known_revisions = {
            (row["scope"], row["decision_id"], row["revision_id"])
            for row in snapshot["decisions"]
        }
        for row in initial["standing_revisions"]:
            if not isinstance(row, dict) or (
                row.get("scope", "personal"), row.get("decision_id"),
                row.get("revision_id"),
            ) not in known_revisions or row.get("tier") not in {"prompt_full", "pointer"}:
                raise QualificationError(f"{case_id}: standing context identity is invalid")
        for row in initial["working_set"]:
            if not isinstance(row, dict) or (
                row.get("scope", "personal"), row.get("decision_id"),
                row.get("revision_id"),
            ) not in known_revisions:
                raise QualificationError(f"{case_id}: working-set identity is invalid")
        exposure = case.get("exposure_history")
        if not isinstance(exposure, list) or not exposure \
                or any(item not in {"authored", "reviewed", "opened", "unknown"}
                       for item in exposure):
            raise QualificationError(f"{case_id}: exposure history is unexplained")
        if data_origin == "qualification" and "unknown" in exposure:
            raise QualificationError(f"{case_id}: unknown exposure cannot be sealed")
        if category == "negative":
            if case.get("independent_draw") is not True \
                    or not isinstance(case.get("sampling_distribution"), str):
                raise QualificationError(f"{case_id}: negative independence is unresolved")

    alias_groups = [set(row) for row in aliases]
    overlap = families & pilot_families
    alias_overlap = [
        sorted(group) for group in alias_groups
        if group & families and group & pilot_families
    ]
    if overlap or alias_overlap:
        raise QualificationError("qualification and pilot families overlap or alias")
    if data_origin == "qualification" and families & development_families:
        raise QualificationError("qualification and development families overlap")
    if data_origin == "qualification":
        if counts != QUALIFICATION_COUNTS:
            raise QualificationError("qualification sampling counts must be frozen at 24/80/12")
        negative_families = [
            case["family_id"] for case in cases if case["category"] == "negative"
        ]
        if len(negative_families) != len(set(negative_families)):
            raise QualificationError("qualification negatives require one prompt per family")
        if len(repo_ids) < 3:
            raise QualificationError("qualification requires at least three snapshots")
    sampling = manifest.get("sampling")
    if not isinstance(sampling, dict) \
            or not isinstance(sampling.get("seed"), int) \
            or not isinstance(sampling.get("target_population"), str) \
            or not isinstance(sampling.get("selection_rule"), str) \
            or not isinstance(sampling.get("stopping_rule"), str) \
            or sampling.get("counts") != counts:
        raise QualificationError("sampling plan must freeze population, rule, seed, counts, and stop")
    history_path = _contained(root, manifest.get("exposure_history_path"), "exposure history")
    _history_rows(history_path, dataset_id)
    run_history_path = _contained(
        root, manifest.get("run_history_path"), "run history"
    )
    _run_history_rows(run_history_path, dataset_id)
    review_ref = manifest.get("review_attestation_ref")
    if review_ref is not None:
        resolve_ref(root, review_ref, "review attestation")
    binding = manifest.get("final_evaluation_binding")
    if binding is not None:
        candidate = sources["candidate"]
        expected_source = {
            "head": candidate["head"],
            "diff_sha256": candidate["diff_sha256"],
        }
        if not isinstance(binding, dict) \
                or not isinstance(binding.get("campaign_id"), str) \
                or not binding["campaign_id"].strip() \
                or binding.get("integration_source") != expected_source \
                or binding.get("integration_source_sha256") != candidate["diff_sha256"]:
            raise QualificationError(
                "final evaluation binding must name the executed candidate source"
            )
    return manifest


def build_review_packet(manifest_path: Path) -> dict[str, Any]:
    manifest = validate_manifest(manifest_path)
    root = manifest_path.resolve().parent
    cases = []
    for case in manifest["cases"]:
        cases.append({
            "case_id": case["case_id"],
            "family_id": case["family_id"],
            "repo_id": case["repo_id"],
            "category": case["category"],
            "prompt": resolve_ref(root, case["prompt_ref"], "prompt").read_text(
                encoding="utf-8"
            ),
            "prompt_sha256": case["prompt_sha256"],
            "snapshot": load_json(resolve_ref(root, case["snapshot_ref"], "snapshot")),
            "label": load_json(resolve_ref(root, case["label_ref"], "label")),
            "label_sha256": case["label_ref"]["sha256"],
            "rationale": resolve_ref(root, case["rationale_ref"], "rationale").read_text(
                encoding="utf-8"
            ),
            "rationale_sha256": case["rationale_ref"]["sha256"],
            "ambiguous": case.get("ambiguous", False),
            "safety_critical": case.get("safety_critical", False),
        })
    return {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "blinded_review_packet",
        "dataset_id": manifest["dataset_id"],
        "qualification_definition_sha256": manifest_definition_digest(manifest),
        "candidate_predictions_included": False,
        "implementation_agent_judgments_included": False,
        "cases": cases,
    }


def validate_review_attestation(
    attestation_path: Path | None,
    manifest_path: Path,
    packet_sha256: str,
) -> list[str]:
    if attestation_path is None or not attestation_path.is_file():
        return ["independent_review_pending"]
    manifest = validate_manifest(manifest_path)
    try:
        attestation = load_json(attestation_path)
    except QualificationError:
        return ["review_attestation_invalid"]
    reasons = []
    if attestation.get("schema_version") != SCHEMA_VERSION \
            or attestation.get("artifact_kind") != "operator_imported_review" \
            or attestation.get("dataset_id") != manifest["dataset_id"]:
        reasons.append("review_attestation_schema_invalid")
    if attestation.get("review_packet_sha256") != packet_sha256:
        reasons.append("review_packet_identity_mismatch")
    operator = attestation.get("operator")
    if not isinstance(operator, dict) \
            or not isinstance(operator.get("operator_id"), str) \
            or operator.get("external_review_import_confirmed") is not True \
            or operator.get("collector_generated") is not False:
        reasons.append("operator_review_boundary_missing")
    records = attestation.get("records")
    if not isinstance(records, list):
        return sorted(set(reasons + ["review_records_missing"]))
    by_case = {
        row.get("case_id"): row for row in records
        if isinstance(row, dict) and isinstance(row.get("case_id"), str)
    }
    if len(by_case) != len(records) or set(by_case) != {
        case["case_id"] for case in manifest["cases"]
    }:
        reasons.append("review_assignment_mismatch")
        return sorted(set(reasons))
    cases = {case["case_id"]: case for case in manifest["cases"]}
    for case_id, row in by_case.items():
        case = cases[case_id]
        if row.get("label_sha256") != case["label_ref"]["sha256"] \
                or row.get("rationale_sha256") != case["rationale_ref"]["sha256"] \
                or row.get("review_packet_sha256") != packet_sha256:
            reasons.append(f"{case_id}:reviewed_artifact_identity_mismatch")
        reviewers = row.get("reviewers")
        if not isinstance(reviewers, list) or not reviewers:
            reasons.append(f"{case_id}:independent_reviewer_missing")
            continue
        reviewer_ids = set()
        verdicts = set()
        needs_adjudication = bool(case.get("ambiguous") or case.get("safety_critical"))
        for reviewer in reviewers:
            if not isinstance(reviewer, dict):
                reasons.append(f"{case_id}:reviewer_record_invalid")
                continue
            reviewer_id = reviewer.get("reviewer_id")
            if not isinstance(reviewer_id, str) or not reviewer_id \
                    or reviewer_id in reviewer_ids \
                    or reviewer.get("human_independent") is not True \
                    or reviewer.get("implementation_agent") is not False \
                    or reviewer.get("conflict_attestation") not in {"none", "disclosed"} \
                    or reviewer.get("verdict") not in {"accept", "reject", "ambiguous"} \
                    or not isinstance(reviewer.get("rationale"), str) \
                    or not reviewer["rationale"].strip():
                reasons.append(f"{case_id}:reviewer_record_invalid")
                continue
            reviewer_ids.add(reviewer_id)
            verdicts.add(reviewer["verdict"])
            needs_adjudication = needs_adjudication or (
                reviewer.get("conflict_attestation") == "disclosed"
                or reviewer.get("verdict") in {"reject", "ambiguous"}
            )
            try:
                _timestamp(reviewer.get("reviewed_at"), "reviewed_at")
            except QualificationError:
                reasons.append(f"{case_id}:reviewer_record_invalid")
        if needs_adjudication and len(reviewers) < 2:
            reasons.append(f"{case_id}:second_independent_reviewer_missing")
        if needs_adjudication:
            adjudication = row.get("adjudication")
            if not isinstance(adjudication, dict) \
                    or adjudication.get("status") != "accepted" \
                    or adjudication.get("resolved_verdict") != "accept" \
                    or not isinstance(adjudication.get("adjudicator_id"), str) \
                    or not adjudication["adjudicator_id"].strip() \
                    or adjudication.get("adjudicator_id") in reviewer_ids \
                    or adjudication.get("reviewer_ids") != sorted(reviewer_ids) \
                    or not isinstance(adjudication.get("rationale"), str) \
                    or not adjudication["rationale"].strip():
                reasons.append(f"{case_id}:adjudication_missing")
            else:
                try:
                    _timestamp(adjudication.get("adjudicated_at"), "adjudicated_at")
                except QualificationError:
                    reasons.append(f"{case_id}:adjudication_missing")
        elif verdicts != {"accept"}:
            reasons.append(f"{case_id}:review_rejection_unresolved")
    return sorted(set(reasons))


def import_review_attestation(
    manifest_path: Path,
    source_path: Path,
    output_path: Path,
    *,
    manifest_output_path: Path | None = None,
) -> dict[str, Any]:
    """Import a user-selected external attestation without manufacturing its contents."""
    if source_path.resolve() == output_path.resolve():
        raise QualificationError("review import source and destination must differ")
    packet_hash = digest(build_review_packet(manifest_path))
    reasons = validate_review_attestation(source_path, manifest_path, packet_hash)
    if reasons:
        raise QualificationError("review import rejected: " + ", ".join(reasons))
    attestation = load_json(source_path)
    atomic_create_json(output_path, attestation)
    if manifest_output_path is not None:
        manifest = validate_manifest(manifest_path)
        root = manifest_output_path.resolve().parent
        if manifest_path.resolve().parent != root:
            raise QualificationError("manifest revisions must share one artifact root")
        manifest["review_attestation_ref"] = make_ref(root, output_path)
        atomic_create_json(manifest_output_path, manifest)
        validate_manifest(manifest_output_path)
        history = _contained(
            root, manifest["exposure_history_path"], "exposure history"
        )
        append_exposure(
            history,
            manifest["dataset_id"],
            "frozen_unopened",
            review_attestation_sha256=sha256(output_path),
            manifest_definition_sha256=manifest_definition_digest(manifest),
        )
    return attestation


def _assertion_value(observation: dict[str, Any], assertion: dict[str, Any]) -> object:
    return observation["arms"][assertion["arm"]].get(assertion["field"])


def _assertion_passes(observation: dict[str, Any], assertion: dict[str, Any]) -> bool:
    value = _assertion_value(observation, assertion)
    if assertion["operator"] == "eq":
        return value == assertion.get("value")
    if assertion["operator"] == "contains":
        return isinstance(value, list) and assertion.get("value") in value
    return isinstance(value, (list, str, dict)) and not value


def _validated_observation(
    manifest_root: Path,
    case: dict[str, Any],
    observation: dict[str, Any],
) -> tuple[dict[str, list[str]], list[str]]:
    """Reconcile summaries with the actual routed emissions and frozen inputs."""
    errors = []
    for field in ("case_id", "family_id", "repo_id", "category", "host"):
        if observation.get(field) != case[field]:
            errors.append(f"{field}_mismatch")
    if observation.get("status") != "observed":
        errors.append("not_observed")
    snapshot = load_json(resolve_ref(manifest_root, case["snapshot_ref"], "snapshot"))
    initial = load_json(
        resolve_ref(manifest_root, case["initial_context_ref"], "initial context")
    )
    decisions = {
        row["revision_id"]: row for row in snapshot["decisions"]
    }
    expected_standing = [
        row["revision_id"] for row in initial["standing_revisions"]
        if row.get("tier") == "prompt_full"
    ]
    if observation.get("initial_standing_revisions") != expected_standing:
        errors.append("initial_standing_context_mismatch")
    expected_working = sorted(
        (row.get("scope", "personal"), row["decision_id"])
        for row in initial["working_set"]
    )
    arms = observation.get("arms")
    if not isinstance(arms, dict) or set(arms) != {"baseline", "candidate"}:
        return {"baseline": [], "candidate": []}, errors + ["arm_set_invalid"]
    validated: dict[str, list[str]] = {}
    for arm_name in ("baseline", "candidate"):
        arm = arms[arm_name]
        prefix = f"{arm_name}_"
        if not isinstance(arm, dict):
            errors.append(prefix + "record_invalid")
            validated[arm_name] = []
            continue
        if arm.get("supported") is not True:
            errors.append(prefix + "unsupported")
        emitted = arm.get("emitted_revisions")
        emitted_ids = arm.get("emitted_revision_ids")
        found = arm.get("found_revisions")
        selected = arm.get("selected_revisions")
        if not all(isinstance(value, list) for value in (emitted, emitted_ids, found, selected)):
            errors.append(prefix + "emission_schema_invalid")
            validated[arm_name] = []
            continue
        structured_ids = []
        for row in emitted:
            if not isinstance(row, dict):
                errors.append(prefix + "emission_schema_invalid")
                continue
            revision_id = row.get("revision_id")
            decision = decisions.get(revision_id)
            if decision is None \
                    or row.get("decision_id") != decision["decision_id"] \
                    or row.get("tier") != "prompt_full" \
                    or row.get("scope", decision.get("scope", "personal")) \
                    != decision.get("scope", "personal"):
                errors.append(prefix + "emission_identity_invalid")
                continue
            structured_ids.append(revision_id)
        if emitted_ids != structured_ids or len(structured_ids) != len(set(structured_ids)):
            errors.append(prefix + "emission_summary_mismatch")
        selected_ids = [
            row.get("revision_id") for row in selected if isinstance(row, dict)
        ]
        found_ids = [row.get("revision_id") for row in found if isinstance(row, dict)]
        if len(selected_ids) != len(selected) or selected_ids != structured_ids:
            errors.append(prefix + "selection_emission_mismatch")
        if len(found_ids) != len(found) or not set(structured_ids).issubset(found_ids):
            errors.append(prefix + "found_emission_mismatch")
        route_selected = arm.get("task_route_selected")
        origin = arm.get("origin")
        if not isinstance(route_selected, bool) \
                or route_selected != (origin == CANDIDATE_VARIANT):
            errors.append(prefix + "route_origin_mismatch")
        if arm_name == "baseline" and route_selected:
            errors.append(prefix + "unexpected_task_route")
        if case["category"] == "control" and route_selected:
            errors.append(prefix + "legacy_control_route_mismatch")
        if structured_ids:
            if arm.get("router_context_in_payload") is not True \
                    or arm.get("content_tier") != "strong" \
                    or arm.get("pointer_emitted") is not False:
                errors.append(prefix + "payload_delivery_mismatch")
            if arm_name == "candidate" \
                    and case["category"] in {"positive", "negative"} \
                    and not route_selected:
                errors.append(prefix + "task_route_missing")
        if arm.get("authority_valid") is not all(
            decisions[revision_id].get("status") in {"approved", "suggested"}
            for revision_id in structured_ids if revision_id in decisions
        ):
            errors.append(prefix + "authority_summary_mismatch")
        records = arm.get("initial_working_set_records")
        if not isinstance(records, list) or any(
            not isinstance(row, dict)
            or not isinstance(row.get("fingerprint"), str)
            or not row["fingerprint"]
            for row in records
        ):
            errors.append(prefix + "initial_working_set_invalid")
        else:
            actual_working = sorted(
                (row.get("scope", "personal"), row.get("id")) for row in records
            )
            if actual_working != expected_working:
                errors.append(prefix + "initial_working_set_mismatch")
        if not isinstance(arm.get("user_notice"), bool) \
                or not isinstance(arm.get("router_context_in_payload"), bool) \
                or not isinstance(arm.get("pointer_emitted"), bool):
            errors.append(prefix + "delivery_schema_invalid")
        validated[arm_name] = structured_ids
    return validated, errors


def _collect_case(
    manifest_root: Path,
    case: dict[str, Any],
    destination: Path,
) -> dict[str, Any]:
    destination = destination.resolve()
    destination.mkdir(parents=True, exist_ok=True)
    snapshot = load_json(resolve_ref(manifest_root, case["snapshot_ref"], "snapshot"))
    label = load_json(resolve_ref(manifest_root, case["label_ref"], "label"))
    initial = load_json(
        resolve_ref(manifest_root, case["initial_context_ref"], "initial context")
    )
    prompt = resolve_ref(manifest_root, case["prompt_ref"], "prompt").read_text(
        encoding="utf-8"
    )
    execution_repo_id = f"synthetic/qualification-{digest(case['repo_id'])[:16]}"
    retrieval_case = {
        "case_id": case["case_id"],
        "split": "contract08",
        "family": case["family_id"],
        "repo_id": execution_repo_id,
        "host": case["host"],
        "category": (
            "ordinary_positive" if case["category"] == "positive"
            else "negative" if case["category"] == "negative"
            else "existing_route_control"
        ),
        "review_status": "independently_reviewed",
        "prompt": prompt,
        "applicable": copy.deepcopy(label["applicable"]),
        "expected_silence": label["expected_silence"],
        "hard_negative": case.get("hard_negative", False),
        "initial_working_set": copy.deepcopy(initial["working_set"]),
    }
    fixture = {"repositories": [{
        "repo_id": execution_repo_id,
        "decisions": snapshot["decisions"],
    }]}
    raw = task_outcome_experiment.run_retrieval_case(fixture, retrieval_case, destination)
    by_revision = {
        row["revision_id"]: row for row in snapshot["decisions"]
    }
    arms = {}
    for arm in ("baseline", "candidate"):
        output = raw["arms"][arm]
        stages = raw["stages"][arm]
        emitted = stages["emitted_revisions"]
        arms[arm] = {
            "supported": output["supported"],
            "found_revisions": output["found_revisions"],
            "selected_revisions": stages["selected_revisions"],
            "emitted_revisions": emitted,
            "emitted_revision_ids": [row["revision_id"] for row in emitted],
            "pointer_emitted": stages["pointer_emitted"],
            "task_route_selected": stages["task_route_selected"],
            "origin": output["meta"].get("origin"),
            "content_tier": output["meta"].get("kind") or None,
            "user_notice": output["user_notice"],
            "router_context_in_payload": output["router_context_in_payload"],
            "authority_valid": all(
                by_revision.get(row["revision_id"], {}).get("status")
                in {"approved", "suggested"}
                for row in emitted
            ),
            "initial_working_set_records": output["initial_working_set_records"],
        }
    return {
        "case_id": case["case_id"],
        "family_id": case["family_id"],
        "repo_id": case["repo_id"],
        "category": case["category"],
        "host": case["host"],
        "status": "observed",
        "initial_standing_revisions": [
            row["revision_id"] for row in initial["standing_revisions"]
            if row.get("tier") == "prompt_full"
        ],
        "arms": arms,
    }


def collect(
    manifest_path: Path,
    output_path: Path,
    *,
    open_qualification: bool = False,
    case_collector: Any = None,
) -> dict[str, Any]:
    if output_path.exists():
        raise QualificationError("refusing to overwrite immutable observation output")
    manifest = validate_manifest(manifest_path)
    manifest_sha256_before = sha256(manifest_path)
    root = manifest_path.resolve().parent
    history = _contained(root, manifest["exposure_history_path"], "exposure history")
    run_history = _contained(root, manifest["run_history_path"], "run history")
    origin = manifest["data_origin"]
    review_packet = build_review_packet(manifest_path)
    packet_hash = digest(review_packet)
    attestation_path = None
    if manifest.get("review_attestation_ref") is not None:
        attestation_path = resolve_ref(root, manifest["review_attestation_ref"], "review")
    review_reasons = validate_review_attestation(
        attestation_path, manifest_path, packet_hash,
    )
    if origin == "qualification":
        if not open_qualification:
            raise QualificationError("reserved qualification requires explicit opening")
        if review_reasons:
            raise QualificationError("reserved qualification review is incomplete")
        if exposure_state(history, manifest["dataset_id"]) != "frozen_unopened":
            raise QualificationError("reserved qualification is not frozen and unopened")
        binding = manifest.get("final_evaluation_binding")
        if not isinstance(binding, dict) \
                or not isinstance(binding.get("campaign_id"), str) \
                or not isinstance(binding.get("integration_source_sha256"), str):
            raise QualificationError("final integration and campaign binding is required")
        source_chain = _source_chain(manifest_path, manifest)
        source_chain_sha256 = digest(source_chain)
        append_exposure(
            history,
            manifest["dataset_id"],
            "opening_intent",
            qualification_manifest_sha256=sha256(manifest_path),
            output_path=str(output_path.resolve()),
            binding=binding,
            source_chain_sha256=source_chain_sha256,
        )
    else:
        binding = None
        source_chain = _source_chain(manifest_path, manifest)
        source_chain_sha256 = digest(source_chain)

    collector = case_collector or _collect_case
    observations = []
    run_status = "completed"
    prior_attempts = {
        row["attempt_id"] for row in _run_history_rows(run_history, manifest["dataset_id"])
        if row["event"] == "started"
    }
    attempt_id = f"{manifest['dataset_id']}:attempt:{len(prior_attempts) + 1}"
    run_started = False
    try:
        append_run_history(
            run_history,
            manifest["dataset_id"],
            "started",
            attempt_id,
            output_path=str(output_path.resolve()),
            qualification_manifest_sha256=manifest_sha256_before,
            independent_sample=False,
            retry_of=sorted(prior_attempts)[-1] if prior_attempts else None,
        )
        run_started = True
        with tempfile.TemporaryDirectory(prefix="contexer-contract08-") as temporary:
            temporary_root = Path(temporary)
            for index, case in enumerate(manifest["cases"]):
                case_directory = (temporary_root / f"case-{index:04d}").resolve()
                if temporary_root.resolve() not in case_directory.parents:
                    raise QualificationError("generated case directory escaped temporary root")
                try:
                    observations.append(collector(root, case, case_directory))
                except Exception as exc:
                    observations.append({
                        "case_id": case["case_id"],
                        "family_id": case["family_id"],
                        "repo_id": case["repo_id"],
                        "category": case["category"],
                        "host": case["host"],
                        "status": "unknown",
                        "reason": type(exc).__name__,
                        "initial_standing_revisions": [],
                        "arms": {},
                    })
        validate_manifest(manifest_path)
        if sha256(manifest_path) != manifest_sha256_before:
            raise QualificationError("qualification sources changed during measurement")
        document = {
            "schema_version": SCHEMA_VERSION,
            "artifact_kind": "qualification_observations",
            "dataset_id": manifest["dataset_id"],
            "qualification_manifest_sha256": manifest_sha256_before,
            "source_chain": source_chain,
            "source_chain_sha256": source_chain_sha256,
            "data_origin": origin,
            "execution_kind": "deterministic_offline_retrieval",
            "collector_sha256": manifest["sources"]["collector"]["sha256"],
            "formatter_sha256": manifest["sources"]["formatter"]["sha256"],
            "host_receipt_observed": False,
            "model_invoked": False,
            "network_used": False,
            "repository_commands_executed": False,
            "cases": observations,
        }
        atomic_create_json(output_path, document)
        return document
    except BaseException:
        run_status = "failed_or_interrupted"
        raise
    finally:
        try:
            if run_started:
                append_run_history(
                    run_history,
                    manifest["dataset_id"],
                    run_status,
                    attempt_id,
                    output_exists=output_path.exists(),
                    output_sha256=sha256(output_path) if output_path.is_file() else None,
                )
        finally:
            if origin == "qualification":
                append_exposure(
                    history,
                    manifest["dataset_id"],
                    "consumed",
                    result=run_status,
                    output_exists=output_path.exists(),
                )


def _control_failures(
    observations: dict[str, dict[str, Any]],
    manifest: dict[str, Any],
    root: Path,
) -> tuple[dict[str, list[str]], list[str]]:
    failures = {"authority": [], "legacy": [], "notice": []}
    missing = []
    for case in manifest["cases"]:
        if case["category"] != "control":
            continue
        row = observations.get(case["case_id"])
        label = load_json(resolve_ref(root, case["label_ref"], "label"))
        if row is None or row.get("status") != "observed":
            missing.append(case["case_id"])
            continue
        for assertion in label["control_assertions"]:
            if not _assertion_passes(row, assertion):
                failures[assertion["control_kind"]].append(assertion["assertion_id"])
    return failures, missing


def build_projection(
    manifest_path: Path,
    observations_path: Path,
    attestation_path: Path | None = None,
) -> tuple[dict[str, Any], list[str]]:
    manifest = validate_manifest(manifest_path)
    root = manifest_path.resolve().parent
    observations_document = load_json(observations_path)
    if observations_document.get("schema_version") != SCHEMA_VERSION \
            or observations_document.get("artifact_kind") != "qualification_observations" \
            or observations_document.get("dataset_id") != manifest["dataset_id"] \
            or observations_document.get("qualification_manifest_sha256") != sha256(
                manifest_path
            ):
        raise QualificationError("observation artifact identity mismatch")
    if observations_document.get("collector_sha256") != manifest["sources"][
        "collector"
    ]["sha256"] or observations_document.get("formatter_sha256") != manifest["sources"][
        "formatter"
    ]["sha256"]:
        raise QualificationError("observation collector or formatter identity mismatch")
    if observations_document.get("data_origin") != manifest["data_origin"] \
            or observations_document.get("execution_kind") \
            != "deterministic_offline_retrieval" \
            or any(observations_document.get(field) is not False for field in (
                "host_receipt_observed", "model_invoked", "network_used",
                "repository_commands_executed",
            )):
        raise QualificationError("observation execution boundary mismatch")
    expected_source_chain = _source_chain(manifest_path, manifest)
    if observations_document.get("source_chain") != expected_source_chain \
            or observations_document.get("source_chain_sha256") != digest(
                expected_source_chain
            ):
        raise QualificationError("observation execution source chain mismatch")
    rows = observations_document.get("cases")
    if not isinstance(rows, list):
        raise QualificationError("observation cases are missing")
    by_case = {
        row.get("case_id"): row for row in rows
        if isinstance(row, dict) and isinstance(row.get("case_id"), str)
    }
    assigned = {case["case_id"] for case in manifest["cases"]}
    reasons = []
    if len(by_case) != len(rows) or set(by_case) != assigned:
        reasons.append("assigned_observation_set_mismatch")
    unknown = {
        case_id for case_id in assigned
        if by_case.get(case_id, {}).get("status") != "observed"
    }
    validated_emissions = {}
    validated_by_case = dict(by_case)
    for case in manifest["cases"]:
        observation = by_case.get(case["case_id"], {})
        emissions, observation_errors = _validated_observation(root, case, observation)
        validated_emissions[case["case_id"]] = emissions
        if observation_errors:
            unknown.add(case["case_id"])
            reasons.append("qualification_observation_contradiction")
            invalid = copy.deepcopy(observation)
            invalid["status"] = "unknown"
            validated_by_case[case["case_id"]] = invalid
    if unknown:
        reasons.append("qualification_observations_unknown")
    review_packet = build_review_packet(manifest_path)
    packet_hash = digest(review_packet)
    if attestation_path is None and manifest.get("review_attestation_ref") is not None:
        attestation_path = resolve_ref(root, manifest["review_attestation_ref"], "review")
    review_reasons = validate_review_attestation(
        attestation_path, manifest_path, packet_hash,
    )
    reasons.extend(review_reasons)
    review_records = []
    if attestation_path and not review_reasons:
        attestation = load_json(attestation_path)
        categories = {
            case["case_id"]: case["category"] for case in manifest["cases"]
        }
        for record in attestation["records"]:
            if categories[record["case_id"]] == "control":
                continue
            review_records.append({
                "case_id": record["case_id"],
                "reviewer_id": record["reviewers"][0]["reviewer_id"],
                "rationale_sha256": record["rationale_sha256"],
                "predictions_hidden": True,
                "implementation_reviewer": False,
                "exposure": "unexposed",
                "adjudication": "accepted",
            })
    projection_rows = []
    pointer_diagnostics = []
    for case in manifest["cases"]:
        if case["category"] == "control":
            continue
        observed = by_case.get(case["case_id"], {})
        label = load_json(resolve_ref(root, case["label_ref"], "label"))
        expected = [row["revision_id"] for row in label["applicable"]]
        standing = observed.get("initial_standing_revisions", [])
        arms = observed.get("arms", {})
        emissions = validated_emissions.get(case["case_id"], {})
        if case["case_id"] in unknown:
            baseline_emitted = []
            candidate_emitted = []
        else:
            baseline_emitted = emissions.get("baseline", [])
            candidate_emitted = emissions.get("candidate", [])
        baseline_coverage = list(dict.fromkeys(standing + baseline_emitted))
        candidate_coverage = list(dict.fromkeys(standing + candidate_emitted))
        projection_rows.append({
            "case_id": case["case_id"],
            "family_id": case["family_id"],
            "category": case["category"],
            "route": "task",
            "content_tier": "full",
            "baseline_task_route_selected": arms.get("baseline", {}).get(
                "task_route_selected"
            ),
            "candidate_task_route_selected": arms.get("candidate", {}).get(
                "task_route_selected"
            ),
            "baseline_content_tier": arms.get("baseline", {}).get("content_tier"),
            "candidate_content_tier": arms.get("candidate", {}).get("content_tier"),
            "expected_revisions": expected,
            "baseline_revisions": baseline_emitted,
            "candidate_revisions": candidate_emitted,
            "baseline_coverage_revisions": baseline_coverage,
            "candidate_coverage_revisions": candidate_coverage,
            "standing_revisions": standing,
            "independent_draw": case.get("independent_draw"),
            "sampling_distribution": case.get("sampling_distribution"),
            "hard_negative": case.get("hard_negative", False),
        })
        for arm in ("baseline", "candidate"):
            if arms.get(arm, {}).get("pointer_emitted"):
                pointer_diagnostics.append({"case_id": case["case_id"], "arm": arm})
    control_failures, missing_controls = _control_failures(
        validated_by_case, manifest, root
    )
    if missing_controls:
        reasons.append("regression_control_observations_missing")
    pilot_core = validate_pilot_core(
        resolve_ref(root, manifest["pilot_core_ref"], "pilot core")
    )
    projection = {
        "qualified_evidence_schema_version": QUALIFIED_EVIDENCE_VERSION,
        "qualification_manifest_sha256": sha256(manifest_path),
        "label_manifest_sha256": sha256(manifest_path),
        "pilot_core_sha256": manifest["pilot_core_ref"]["sha256"],
        "live_task_family_ids": sorted({row["family_id"] for row in pilot_core["tasks"]}),
        "assigned_case_ids": sorted(assigned),
        "unknown_case_ids": sorted(unknown),
        "relevance_observations": projection_rows,
        "review_records": review_records,
        "authority_regression_ids": sorted(control_failures["authority"]),
        "legacy_regression_ids": sorted(control_failures["legacy"]),
        "new_task_recall_notice_ids": sorted(control_failures["notice"]),
        "regression_control_case_ids": sorted(
            case["case_id"] for case in manifest["cases"]
            if case["category"] == "control"
        ),
        "pointer_diagnostics": pointer_diagnostics,
        "adapter_emission_is_host_receipt": False,
    }
    return projection, sorted(set(reasons))


def build_qualified_artifact(
    manifest_path: Path,
    observations_path: Path,
    output_path: Path,
) -> dict[str, Any]:
    manifest = validate_manifest(manifest_path)
    root = manifest_path.resolve().parent
    projection, reasons = build_projection(manifest_path, observations_path)
    source_chain_sha256 = digest(_source_chain(manifest_path, manifest))
    history_path = _contained(root, manifest["exposure_history_path"], "exposure history")
    run_history_path = _contained(root, manifest["run_history_path"], "run history")
    state = exposure_state(history_path, manifest["dataset_id"])
    if manifest["data_origin"] == "qualification" and state != "consumed":
        reasons.append("qualification_packet_not_consumed")
    if manifest["data_origin"] != "qualification":
        reasons.append("development_data_cannot_qualify")
    binding = manifest.get("final_evaluation_binding") or {
        "campaign_id": f"development:{manifest['dataset_id']}",
        "integration_source": {
            "head": manifest["sources"]["candidate"]["head"],
            "diff_sha256": manifest["sources"]["candidate"]["diff_sha256"],
        },
    }
    references = {
        "qualification_manifest": make_ref(output_path.parent, manifest_path),
        "pilot_core": make_ref(
            output_path.parent,
            resolve_ref(root, manifest["pilot_core_ref"], "pilot core"),
        ),
        "observations": make_ref(output_path.parent, observations_path),
        "exposure_history": make_ref(output_path.parent, history_path),
        "run_history": make_ref(output_path.parent, run_history_path),
    }
    if manifest.get("review_attestation_ref") is not None:
        references["review_attestation"] = make_ref(
            output_path.parent,
            resolve_ref(root, manifest["review_attestation_ref"], "review"),
        )
    artifact = {
        "schema_version": SCHEMA_VERSION,
        "artifact_kind": "qualification",
        "qualified_evidence_schema_version": QUALIFIED_EVIDENCE_VERSION,
        "provenance": {
            "mode": "measured",
            "synthetic": manifest["data_origin"] != "qualification",
            "stub": False,
            "producer": "qualification_review_v2",
            "producer_sha256": sha256(Path(__file__).resolve()),
            "integration_source": binding["integration_source"],
            "candidate_source_sha256": manifest["sources"]["candidate"]["diff_sha256"],
            "source_chain_sha256": source_chain_sha256,
            "collected_at": datetime.now(timezone.utc).isoformat(),
            "run_id": binding["campaign_id"],
            "data_origin": manifest["data_origin"],
            "execution_kind": manifest["execution_kind"],
            "host_receipt_observed": False,
        },
        "references": references,
        "measurements": projection,
        "producer_validation": {
            "status": "pass" if not reasons else "inconclusive",
            "reasons": sorted(set(reasons)),
        },
    }
    atomic_create_json(output_path, artifact)
    return artifact


def validate_qualified_artifact(
    artifact_path: Path,
    *,
    campaign_manifest: dict[str, Any] | None = None,
) -> dict[str, Any]:
    artifact = load_json(artifact_path)
    root = artifact_path.resolve().parent
    reasons = []
    if artifact.get("schema_version") != SCHEMA_VERSION \
            or artifact.get("artifact_kind") != "qualification" \
            or artifact.get("qualified_evidence_schema_version") \
            != QUALIFIED_EVIDENCE_VERSION:
        return {"status": "inconclusive", "reasons": ["qualified_schema_invalid"]}
    refs = artifact.get("references")
    required = {
        "qualification_manifest", "pilot_core", "observations", "exposure_history",
        "review_attestation", "run_history",
    }
    if not isinstance(refs, dict) or set(refs) != required:
        return {"status": "inconclusive", "reasons": ["qualified_references_missing"]}
    try:
        manifest_path = resolve_ref(root, refs["qualification_manifest"], "manifest")
        pilot_path = resolve_ref(root, refs["pilot_core"], "pilot core")
        observations_path = resolve_ref(root, refs["observations"], "observations")
        history_path = resolve_ref(root, refs["exposure_history"], "exposure history")
        run_history_path = resolve_ref(root, refs["run_history"], "run history")
        review_path = resolve_ref(root, refs["review_attestation"], "review attestation")
        manifest = validate_manifest(manifest_path)
        validate_pilot_core(pilot_path)
        projection, projection_reasons = build_projection(
            manifest_path, observations_path, review_path,
        )
        reasons.extend(projection_reasons)
    except QualificationError as exc:
        return {
            "status": "inconclusive",
            "reasons": ["qualified_reference_invalid", str(exc)],
        }
    if artifact.get("measurements") != projection:
        reasons.append("qualified_projection_mismatch")
    source_chain_sha256 = digest(_source_chain(manifest_path, manifest))
    binding = manifest.get("final_evaluation_binding")
    if not isinstance(binding, dict):
        reasons.append("qualified_final_binding_missing")
        binding = {}
    provenance = artifact.get("provenance")
    if not isinstance(provenance, dict) \
            or provenance.get("mode") != "measured" \
            or provenance.get("synthetic") is not False \
            or provenance.get("stub") is not False \
            or provenance.get("producer") != "qualification_review_v2" \
            or provenance.get("producer_sha256") != sha256(Path(__file__).resolve()) \
            or provenance.get("data_origin") != "qualification" \
            or provenance.get("execution_kind") != "deterministic_offline_retrieval" \
            or provenance.get("host_receipt_observed") is not False \
            or provenance.get("source_chain_sha256") != source_chain_sha256 \
            or provenance.get("integration_source") != binding.get(
                "integration_source"
            ) \
            or provenance.get("candidate_source_sha256") != manifest[
                "sources"
            ]["candidate"]["diff_sha256"] \
            or provenance.get("run_id") != binding.get("campaign_id"):
        reasons.append("qualified_provenance_invalid")
    history_rows = _history_rows(history_path, manifest["dataset_id"])
    if exposure_state(history_path, manifest["dataset_id"]) != "consumed":
        reasons.append("qualified_exposure_invalid")
    if not any(
        row["event"] == "opening_intent"
        and row.get("details", {}).get("qualification_manifest_sha256")
        == refs["qualification_manifest"]["sha256"]
        and row.get("details", {}).get("source_chain_sha256") == source_chain_sha256
        and row.get("details", {}).get("binding") == binding
        for row in history_rows
    ):
        reasons.append("qualified_opening_source_binding_missing")
    run_rows = _run_history_rows(run_history_path, manifest["dataset_id"])
    if not any(
        row["event"] == "completed"
        and row.get("details", {}).get("output_sha256") == refs["observations"]["sha256"]
        for row in run_rows
    ):
        reasons.append("qualified_run_history_incomplete")
    if refs["pilot_core"]["sha256"] != manifest["pilot_core_ref"]["sha256"]:
        reasons.append("qualified_pilot_core_mismatch")
    if campaign_manifest is not None:
        if campaign_manifest.get("schema_version") != 2:
            reasons.append("campaign_schema_not_qualified")
        campaign_ref = campaign_manifest.get("pilot_task_core")
        if not isinstance(campaign_ref, dict) \
                or campaign_ref.get("sha256") != refs["pilot_core"]["sha256"]:
            reasons.append("campaign_pilot_core_mismatch")
        core = validate_pilot_core(pilot_path)
        expected_tasks = [{
            "task_id": row["task_id"],
            "repo_id": row["repo_id"],
            "family_id": row["family_id"],
            "prompt_sha256": row["prompt_sha256"],
            "snapshot_id": row["snapshot_id"],
            "snapshot_sha256": row["snapshot_sha256"],
            "initial_context_sha256": row["initial_context_sha256"],
            "check_spec_sha256": row["check_spec_sha256"],
        } for row in core["tasks"]]
        actual_tasks = [{
            key: row.get(key) for key in expected_tasks[0]
        } for row in campaign_manifest.get("tasks", [])]
        if actual_tasks != expected_tasks:
            reasons.append("campaign_pilot_task_core_mismatch")
        if provenance.get("integration_source") != campaign_manifest.get(
            "integration_source"
        ) or provenance.get("candidate_source_sha256") != campaign_manifest.get(
            "candidate_source_sha256"
        ) or campaign_manifest.get(
            "qualification_source_chain_sha256"
        ) != source_chain_sha256 \
                or campaign_manifest.get("campaign_id") != binding.get("campaign_id"):
            reasons.append("campaign_source_binding_mismatch")
    return {
        "status": "pass" if not reasons else "inconclusive",
        "reasons": sorted(set(reasons)),
        "measurements": projection,
        "bindings": {
            "qualification_manifest_sha256": refs["qualification_manifest"]["sha256"],
            "pilot_core_sha256": refs["pilot_core"]["sha256"],
            "observations_sha256": refs["observations"]["sha256"],
            "review_attestation_sha256": refs["review_attestation"]["sha256"],
            "exposure_history_sha256": refs["exposure_history"]["sha256"],
            "run_history_sha256": refs["run_history"]["sha256"],
            "source_chain_sha256": source_chain_sha256,
        },
    }


def build_report(
    manifest_path: Path,
    observations_path: Path | None = None,
) -> dict[str, Any]:
    manifest = validate_manifest(manifest_path)
    root = manifest_path.resolve().parent
    packet = build_review_packet(manifest_path)
    packet_hash = digest(packet)
    review_path = None
    if manifest.get("review_attestation_ref") is not None:
        review_path = resolve_ref(root, manifest["review_attestation_ref"], "review")
    review_reasons = validate_review_attestation(
        review_path, manifest_path, packet_hash,
    )
    history = _contained(root, manifest["exposure_history_path"], "exposure history")
    run_history = _contained(root, manifest["run_history_path"], "run history")
    state = exposure_state(history, manifest["dataset_id"])
    measured = observations_path is not None and observations_path.is_file()
    projection_reasons = []
    projection = None
    if measured:
        try:
            projection, projection_reasons = build_projection(
                manifest_path, observations_path, review_path,
            )
        except QualificationError as exc:
            projection_reasons = [str(exc)]
    return {
        "contract": 8,
        "dataset_id": manifest["dataset_id"],
        "data_origin": manifest["data_origin"],
        "execution_kind": manifest["execution_kind"],
        "counts": manifest["sampling"]["counts"],
        "tooling_ready": True,
        "packet": {
            "review_packet_sha256": packet_hash,
            "review_status": "complete" if not review_reasons else "pending",
            "review_reasons": review_reasons,
            "exposure_state": state,
            "prepared": not review_reasons and state == "frozen_unopened",
        },
        "qualification": {
            "measured": measured,
            "accepted": False,
            "projection_valid": measured and not projection_reasons,
            "reasons": projection_reasons,
            "projection": projection,
        },
        "attempts": _run_history_rows(run_history, manifest["dataset_id"]),
        "campaign_binding": (
            "present" if manifest.get("final_evaluation_binding") else "pending"
        ),
        "performance": "not_run",
        "isolation": "not_run",
        "pricing": "absent",
        "live_readiness": False,
        "live_sessions_run": 0,
    }


def render_text(report: dict[str, Any]) -> str:
    packet = report["packet"]
    qualification = report["qualification"]
    return "\n".join([
        "Contract 08 qualification preparation",
        f"dataset: {report['dataset_id']}",
        f"data origin: {report['data_origin']}",
        f"tooling ready: {str(report['tooling_ready']).lower()}",
        f"review: {packet['review_status']}",
        f"exposure: {packet['exposure_state']}",
        f"packet prepared: {str(packet['prepared']).lower()}",
        f"qualification measured: {str(qualification['measured']).lower()}",
        "qualification accepted: false",
        f"campaign binding: {report['campaign_binding']}",
        "performance: not_run",
        "isolation: not_run",
        "live readiness: false",
        "live sessions run: 0",
    ]) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate")
    validate_parser.add_argument("--manifest", type=Path, required=True)

    import_parser = subparsers.add_parser("import-snapshot")
    import_parser.add_argument("--root", type=Path, required=True)
    import_parser.add_argument("--file", action="append", required=True)
    import_parser.add_argument("--output", type=Path, required=True)
    import_parser.add_argument("--grant-id", required=True)
    import_parser.add_argument("--consent-record", required=True)

    packet_parser = subparsers.add_parser("review-packet")
    packet_parser.add_argument("--manifest", type=Path, required=True)
    packet_parser.add_argument("--output", type=Path, required=True)

    review_parser = subparsers.add_parser("review-import")
    review_parser.add_argument("--manifest", type=Path, required=True)
    review_parser.add_argument("--attestation", type=Path, required=True)
    review_parser.add_argument("--output", type=Path, required=True)
    review_parser.add_argument("--manifest-output", type=Path, required=True)

    measure_parser = subparsers.add_parser("measure")
    measure_parser.add_argument("--manifest", type=Path, required=True)
    measure_parser.add_argument("--output", type=Path, required=True)
    measure_parser.add_argument("--open-final", action="store_true")

    export_parser = subparsers.add_parser("export")
    export_parser.add_argument("--manifest", type=Path, required=True)
    export_parser.add_argument("--observations", type=Path, required=True)
    export_parser.add_argument("--output", type=Path, required=True)

    report_parser = subparsers.add_parser("report")
    report_parser.add_argument("--manifest", type=Path, required=True)
    report_parser.add_argument("--observations", type=Path)
    report_parser.add_argument("--format", choices=("json", "text"), default="text")
    report_parser.add_argument("--output", type=Path)

    args = parser.parse_args(argv)
    if args.command == "validate":
        report = build_report(args.manifest)
        print(render_text(report), end="")
        return 0
    if args.command == "import-snapshot":
        path = materialize_snapshot(
            args.root,
            args.file,
            args.output,
            grant_id=args.grant_id,
            consent_record=args.consent_record,
        )
        print(path)
        return 0
    if args.command == "review-packet":
        atomic_create_json(args.output, build_review_packet(args.manifest))
        return 0
    if args.command == "review-import":
        import_review_attestation(
            args.manifest,
            args.attestation,
            args.output,
            manifest_output_path=args.manifest_output,
        )
        return 0
    if args.command == "measure":
        collect(args.manifest, args.output, open_qualification=args.open_final)
        return 0
    if args.command == "export":
        build_qualified_artifact(args.manifest, args.observations, args.output)
        return 0
    report = build_report(args.manifest, args.observations)
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n" \
        if args.format == "json" else render_text(report)
    if args.output:
        atomic_create_text(args.output, rendered)
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
