#!/usr/bin/env python3
"""Reviewer-owned behavioral validator for Contract-06 synthetic artifacts."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any


CHECK_IDS = {
    "payment_retry": (
        "payment.functional", "payment.bounded", "payment.idempotent",
        "payment.revision", "payment.tests", "payment.config", "payment.import",
    ),
    "audit_redaction": (
        "redaction.functional", "redaction.secret", "redaction.identifier",
        "redaction.tests", "redaction.config", "redaction.import",
    ),
    "schema_migration": (
        "migration.functional", "migration.compatibility", "migration.staged",
        "migration.tests", "migration.config", "migration.import",
    ),
    "cache_invalidation": (
        "cache.functional", "cache.versioned", "cache.bounded",
        "cache.tests", "cache.config", "cache.import",
    ),
    "token_refresh": (
        "auth.functional", "auth.rotation", "auth.revocation",
        "auth.tests", "auth.config", "auth.import",
    ),
    "request_logging": (
        "logging.functional", "logging.secret", "logging.identifier",
        "logging.tests", "logging.config", "logging.import",
    ),
}


_CANDIDATE_RUNNER = r'''
import contextlib
import importlib.util
import json
import os
import sys

path, module_name, requests_json = sys.argv[1:]
requests = json.loads(requests_json)
try:
    with open(os.devnull, "w", encoding="utf-8") as sink:
        with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
            spec = importlib.util.spec_from_file_location(module_name, path)
            if spec is None or spec.loader is None:
                raise ImportError(module_name)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            observations = []
            for request in requests:
                try:
                    if request["operation"] == "getattr":
                        result = getattr(module, request["name"])
                    else:
                        result = getattr(module, request["name"])(*request["args"])
                    observations.append({"ok": True, "result": result})
                except BaseException as exc:
                    observations.append({"ok": False, "error_type": type(exc).__name__})
    response = {"ok": True, "observations": observations}
except BaseException as exc:
    response = {"ok": False, "error_type": type(exc).__name__}
sys.stdout.write(json.dumps(response, sort_keys=True))
'''


def _execute_candidate_batch(
    path: Path,
    module_name: str,
    requests: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collect stateful raw observations without loading candidate code here."""
    failure = {"ok": False, "error_type": "executor_failure"}
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-c",
                _CANDIDATE_RUNNER,
                str(path),
                module_name,
                json.dumps(requests, sort_keys=True),
            ],
            env={"PATH": os.environ.get("PATH", ""), "PYTHONHASHSEED": "0"},
            capture_output=True,
            text=True,
            check=False,
            timeout=max(1, len(requests)),
        )
    except (OSError, subprocess.TimeoutExpired):
        return [failure.copy() for _ in requests]
    if result.returncode != 0 or len(result.stdout.encode("utf-8")) > 32_768:
        return [failure.copy() for _ in requests]
    try:
        response = json.loads(result.stdout)
    except json.JSONDecodeError:
        return [failure.copy() for _ in requests]
    observations = response.get("observations") if isinstance(response, dict) else None
    if not isinstance(response, dict) or response.get("ok") is not True or not isinstance(
        observations, list
    ) or (
        len(observations) != len(requests)
    ):
        return [failure.copy() for _ in requests]
    if any(
        not isinstance(observation, dict)
        or observation.get("ok") not in {True, False}
        for observation in observations
    ):
        return [failure.copy() for _ in requests]
    return observations


def _result(observation: dict[str, Any]) -> Any:
    return observation.get("result") if observation.get("ok") is True else None


def _payment(path: Path, module_name: str) -> tuple[bool, ...]:
    plan_observation, repeat_observation, revision_observation = _execute_candidate_batch(
        path,
        module_name,
        [
            {"operation": "call", "name": "retry_plan", "args": ["op-7", 6]},
            {"operation": "call", "name": "retry_plan", "args": ["op-7", 6]},
            {"operation": "getattr", "name": "REVISION", "args": []},
        ],
    )
    plan = _result(plan_observation)
    repeat = _result(repeat_observation)
    functional = (
        isinstance(plan, list) and len(plan) == 6
        and all(
            isinstance(row, dict) and row.get("attempt") == index
            for index, row in enumerate(plan)
        )
    )
    bounded = functional and all(0 < row.get("delay", 0) <= 8 for row in plan)
    idempotent = functional and plan == repeat and all(
        row.get("idempotency_key") == "op-7" for row in plan
    )
    revision = _result(revision_observation)
    return functional, bounded, idempotent, revision == "v2"


def _redaction(path: Path, module_name: str) -> tuple[bool, ...]:
    [observation] = _execute_candidate_batch(path, module_name, [{
        "operation": "call",
        "name": "write_audit",
        "args": [{"event_id": "event-1", "secret": "hidden", "action": "login"}],
    }])
    result = _result(observation)
    functional = isinstance(result, dict) and result.get("action") == "login"
    return functional, functional and "secret" not in result, (
        functional and result.get("event_id") == "event-1"
    )


def _migration(path: Path, module_name: str) -> tuple[bool, ...]:
    record = {"id": "1", "legacy_name": "Ada"}
    migrated_observation, untouched_observation = _execute_candidate_batch(
        path,
        module_name,
        [
            {"operation": "call", "name": "migrate", "args": [record, 10]},
            {"operation": "call", "name": "migrate", "args": [record, 90]},
        ],
    )
    migrated = _result(migrated_observation)
    untouched = _result(untouched_observation)
    functional = isinstance(migrated, dict) and migrated.get("schema_version") == 2
    compatible = functional and migrated.get("legacy_name") == "Ada"
    staged = isinstance(untouched, dict) and untouched == record
    return functional, compatible, staged


def _cache(path: Path, module_name: str) -> tuple[bool, ...]:
    [observation] = _execute_candidate_batch(path, module_name, [{
        "operation": "call", "name": "invalidate", "args": ["users", "42", 900],
    }])
    result = _result(observation)
    functional = isinstance(result, dict) and result.get("invalidated") is True
    versioned = functional and result.get("key") == "v2:users:42"
    bounded = functional and 0 < result.get("expires_in", 0) <= 300
    return functional, versioned, bounded


def _auth(path: Path, module_name: str) -> tuple[bool, ...]:
    refreshed_observation, revoked = _execute_candidate_batch(
        path,
        module_name,
        [
            {"operation": "call", "name": "refresh", "args": ["old", []]},
            {"operation": "call", "name": "refresh", "args": ["old", ["old"]]},
        ],
    )
    refreshed = _result(refreshed_observation)
    functional = isinstance(refreshed, str) and bool(refreshed)
    rotation = functional and refreshed != "old"
    revocation = revoked == {"ok": False, "error_type": "PermissionError"}
    return functional, rotation, revocation


def _logging(path: Path, module_name: str) -> tuple[bool, ...]:
    [observation] = _execute_candidate_batch(path, module_name, [{
        "operation": "call",
        "name": "log_request",
        "args": [{"request_id": "request-1", "secret": "hidden", "path": "/"}],
    }])
    result = _result(observation)
    functional = isinstance(result, dict) and result.get("path") == "/"
    return functional, functional and "secret" not in result, (
        functional and result.get("request_id") == "request-1"
    )


def _observe(path: Path, module_name: str, family: str) -> tuple[bool, ...]:
    if family == "payment_retry":
        return _payment(path, module_name)
    if family == "audit_redaction":
        return _redaction(path, module_name)
    if family == "schema_migration":
        return _migration(path, module_name)
    if family == "cache_invalidation":
        return _cache(path, module_name)
    if family == "token_refresh":
        return _auth(path, module_name)
    if family == "request_logging":
        return _logging(path, module_name)
    raise ValueError(f"unknown family: {family}")


def validate(root: Path, family: str, module_name: str) -> list[dict[str, Any]]:
    expected_path = root.joinpath(*module_name.split(".")).with_suffix(".py")
    import_valid = expected_path.is_file() and expected_path.resolve().is_relative_to(
        root.resolve()
    )
    observed: tuple[bool, ...]
    if import_valid:
        try:
            observed = _observe(expected_path, module_name, family)
        except Exception:
            observed = tuple(False for _ in CHECK_IDS[family][:-3])
    else:
        observed = tuple(False for _ in CHECK_IDS[family][:-3])

    tests_valid = (root / "tests" / "test_candidate.py").read_text(
        encoding="utf-8"
    ) == "# frozen synthetic verification\n" if (
        root / "tests" / "test_candidate.py"
    ).is_file() else False
    config_valid = (root / "config.lock").read_text(encoding="utf-8") == "baseline\n" if (
        root / "config.lock"
    ).is_file() else False
    statuses = (*observed, tests_valid, config_valid, import_valid)
    return [
        {
            "check_id": check_id,
            "kind": "functional" if index == 0 else "condition",
            "status": "pass" if status else "fail",
        }
        for index, (check_id, status) in enumerate(zip(CHECK_IDS[family], statuses))
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("artifact_root", type=Path)
    parser.add_argument("family", choices=sorted(CHECK_IDS))
    parser.add_argument("module_name")
    args = parser.parse_args()
    print(json.dumps(validate(args.artifact_root, args.family, args.module_name), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
