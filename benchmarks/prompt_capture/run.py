#!/usr/bin/env python3
"""Run the deterministic prompt-capture benchmark against isolated local state."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import statistics
import sys
import tempfile
import time
from collections import Counter
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contexer import prompt_capture, review, revisions, share_policy, store, updates  # noqa: E402
from contexer.adapters import base, claude, cursor, gemini  # noqa: E402

SCHEMA_VERSION = 1
RUNNER_VERSION = "1"
FIXTURE_PATH = Path(__file__).with_name("cases.json")
KINDS = {"scope", "lifecycle", "target", "capture", "adapter"}
FAMILIES = {f"P{i:02d}" for i in range(1, 13)}
HOSTS = {"claude", "codex", "cursor", "gemini"}
SAFETY_FIELDS = {
    "result_target", "live_status", "live_content", "proposal_content", "proposal_source",
    "revision_count", "approved_by",
}


class FixtureError(ValueError):
    """The gold fixture is incomplete or internally inconsistent."""


def _fixture_hash(data: dict[str, Any]) -> str:
    raw = json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def load_fixture(path: Path = FIXTURE_PATH) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError(f"invalid fixture: {exc}") from exc
    validate_fixture(data)
    return data


def validate_fixture(data: dict[str, Any]) -> None:
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise FixtureError("schema_version must be 1")
    if not isinstance(data.get("fixture_version"), str) or not data["fixture_version"]:
        raise FixtureError("fixture_version must be a non-empty string")
    cases = data.get("cases")
    if not isinstance(cases, list) or len(cases) < 60:
        raise FixtureError("fixture must contain at least 60 authored cases")
    ids = [case.get("case_id") for case in cases if isinstance(case, dict)]
    if len(ids) != len(cases) or any(not isinstance(value, str) or not value for value in ids):
        raise FixtureError("every case needs a non-empty case_id")
    if len(set(ids)) != len(ids):
        raise FixtureError("case_id values must be unique")
    families = {case.get("family") for case in cases}
    if families != FAMILIES:
        raise FixtureError("fixture must cover exactly P01-P12")
    counts = Counter(case["family"] for case in cases)
    if any(counts[family] < 3 for family in FAMILIES):
        raise FixtureError("every family needs at least three authored cases")
    required = set(data.get("required_case_ids", []))
    if not required or not required.issubset(ids):
        raise FixtureError("required_case_ids must be a non-empty subset of cases")
    assertion_ids: set[str] = set()
    for case in cases:
        if case.get("kind") not in KINDS:
            raise FixtureError(f"{case.get('case_id')}: unknown kind")
        if not isinstance(case.get("prompt"), str):
            raise FixtureError(f"{case['case_id']}: prompt must be a string")
        expected = case.get("expected")
        if not isinstance(expected, dict) or not expected:
            raise FixtureError(f"{case['case_id']}: expected must be non-empty")
        if case["kind"] == "adapter" and case.get("host") not in HOSTS:
            raise FixtureError(f"{case['case_id']}: invalid host")
        for field in expected:
            assertion_ids.add(f"{case['case_id']}:{field}")
    gaps = data.get("known_gaps")
    if not isinstance(gaps, list):
        raise FixtureError("known_gaps must be a list")
    gap_ids: set[str] = set()
    for gap in gaps:
        assertion_id = gap.get("assertion_id") if isinstance(gap, dict) else None
        if assertion_id not in assertion_ids or assertion_id in gap_ids:
            raise FixtureError("known gaps must name unique existing assertions")
        if assertion_id.rsplit(":", 1)[-1] in SAFETY_FIELDS:
            raise FixtureError(f"{assertion_id}: safety assertions cannot be known gaps")
        if not all(isinstance(gap.get(field), str) and gap[field]
                   for field in ("category", "baseline", "rationale", "owner")):
            raise FixtureError(f"{assertion_id}: incomplete known-gap metadata")
        gap_ids.add(assertion_id)


def _git_revision() -> str:
    try:
        import subprocess
        return subprocess.run(
            ["git", "-C", str(ROOT), "rev-parse", "HEAD"], check=True,
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _git_dirty() -> bool | None:
    try:
        import subprocess
        result = subprocess.run(
            ["git", "-C", str(ROOT), "status", "--porcelain"], check=True,
            capture_output=True, text=True, timeout=3,
        )
        return bool(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        return None


def _source_hash() -> str:
    paths = (
        Path(__file__), ROOT / "contexer" / "prompt_capture.py",
        ROOT / "contexer" / "store.py", ROOT / "contexer" / "evidence.py",
        ROOT / "contexer" / "adapters" / "claude.py",
        ROOT / "contexer" / "adapters" / "cursor.py",
        ROOT / "contexer" / "adapters" / "gemini.py",
    )
    digest = hashlib.sha256()
    for path in paths:
        digest.update(path.relative_to(ROOT).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def _stable_id(alias: str) -> str:
    digest = hashlib.sha256(alias.encode()).hexdigest()
    return f"{digest[:8]}-{digest[8:12]}-{digest[12:16]}-{digest[16:20]}-{digest[20:32]}"


def _seed_entry(spec: dict[str, Any], session: str = "seed") -> dict[str, Any]:
    entry = store._new_decision_entry(
        spec["content"], session, spec.get("subtype", "constraint"),
        created_by=spec.get("created_by", "human"), status=spec.get("status", "approved"),
        preserve_case=True,
    )
    entry_id = _stable_id(spec["alias"])
    entry["id"] = entry_id
    for revision in entry["revisions"]:
        revision["decision_id"] = entry_id
    if proposal := spec.get("proposal"):
        entry["proposed_revision"] = review.build_proposal(
            entry, proposal["content"], entry["subtype"], "proposal-session",
            "2026-01-01T00:00:00+00:00", source=proposal.get("source", "ai"),
            preserve_case=True,
        )
    return entry


@contextmanager
def _isolated_case() -> Iterator[tuple[str, Path]]:
    old_store_dir = store.store_dir
    old_env = os.environ.get("CONTEXER_NO_UPDATE_CHECK")
    old_refresh = updates.spawn_refresh
    old_scan = base._scan_automatic_proposals
    old_claude_scan = claude._scan_automatic_proposals
    old_enqueue = share_policy.enqueue_after_local_mutation
    with tempfile.TemporaryDirectory(prefix="contexer-prompt-capture-") as raw:
        root = Path(raw)
        repo = root / "repo"
        repo.mkdir()
        store_root = root / "store"
        store.store_dir = lambda: store_root
        os.environ["CONTEXER_NO_UPDATE_CHECK"] = "1"
        updates.spawn_refresh = lambda: None
        base._scan_automatic_proposals = lambda _repo: None
        claude._scan_automatic_proposals = lambda _repo: None
        share_policy.enqueue_after_local_mutation = lambda _repo, _entry_id: None
        try:
            yield str(repo), root
        finally:
            store.store_dir = old_store_dir
            updates.spawn_refresh = old_refresh
            base._scan_automatic_proposals = old_scan
            claude._scan_automatic_proposals = old_claude_scan
            share_policy.enqueue_after_local_mutation = old_enqueue
            if old_env is None:
                os.environ.pop("CONTEXER_NO_UPDATE_CHECK", None)
            else:
                os.environ["CONTEXER_NO_UPDATE_CHECK"] = old_env


def _seed(repo: str, case: dict[str, Any]) -> dict[str, str]:
    data = store.load(repo)
    aliases: dict[str, str] = {}
    for spec in case.get("seed", []):
        entry = _seed_entry(spec)
        data["entries"].append(entry)
        aliases[spec["alias"]] = entry["id"]
    store.save(repo, data)
    return aliases


def _alias_for(entry_id: str | None, aliases: dict[str, str], new_ids: set[str]) -> str | None:
    if entry_id is None:
        return None
    for alias, actual in aliases.items():
        if actual == entry_id:
            return alias
    return "new" if entry_id in new_ids else "unknown"


def _capture_observation(case: dict[str, Any], repo: str) -> dict[str, Any]:
    aliases = _seed(repo, case)
    before = store.load(repo)
    before_ids = {entry["id"] for entry in before["entries"] if entry.get("type") == "decision"}
    result = store.capture_user_constraint_with_meta(
        repo, case["prompt"], case.get("session_id", "benchmark-session"),
        source="benchmark_prompt",
    )
    entry_id, content, status, meta = result
    if case.get("review_action") and entry_id:
        store.approve_decision(
            repo, entry_id, case["review_action"], content=case.get("review_content", ""),
        )
    after = store.load(repo)
    decisions = [entry for entry in after["entries"] if entry.get("type") == "decision"]
    new_ids = {entry["id"] for entry in decisions} - before_ids
    selected = next((entry for entry in decisions if entry["id"] == entry_id), None)
    if selected is None and case.get("inspect") in aliases:
        selected = next(entry for entry in decisions
                        if entry["id"] == aliases[case["inspect"]])
    proposal = (selected or {}).get("proposed_revision") or {}
    return {
        "result_status": status,
        "result_content": content,
        "result_target": _alias_for(entry_id, aliases, new_ids),
        "entry_count": len(decisions),
        "live_status": (selected or {}).get("status"),
        "live_content": revisions.current_content(selected) if selected else None,
        "proposal_content": proposal.get("content"),
        "proposal_source": proposal.get("source"),
        "revision_count": len((selected or {}).get("revisions", [])),
        "approved_by": (selected or {}).get("approved_by"),
        "recurrence": bool(meta.get("recurrence")),
    }


def _adapter_observation(case: dict[str, Any], repo: str) -> dict[str, Any]:
    aliases = _seed(repo, case)
    raw = json.dumps({"prompt": case["prompt"], "session_id": "benchmark-session",
                      "cwd": repo})
    host = case["host"]
    if host in {"claude", "codex"}:
        output = claude.capture_constraint(repo, raw)
    elif host == "cursor":
        output = cursor.capture_constraint(repo, raw)
    else:
        output = gemini.before_agent(repo, raw)
    parsed = json.loads(output)
    hook = parsed.get("hookSpecificOutput", {})
    guidance = hook.get("additionalContext") or parsed.get("additional_context") or ""
    decisions = [entry for entry in store.load(repo)["entries"]
                 if entry.get("type") == "decision"]
    selected = decisions[-1] if decisions else None
    return {
        "entry_count": len(decisions),
        "live_status": (selected or {}).get("status"),
        "guidance_contains": case.get("guidance") in guidance if case.get("guidance") else False,
        "guidance_present": bool(guidance),
        "passthrough": parsed.get("continue") is True,
        "target": _alias_for((selected or {}).get("id"), aliases,
                             {entry["id"] for entry in decisions} - set(aliases.values())),
    }


def observe(case: dict[str, Any]) -> dict[str, Any]:
    kind = case["kind"]
    if kind == "scope":
        return {"captured": prompt_capture.environment_scope_declaration(case["prompt"])}
    if kind == "lifecycle":
        result = prompt_capture.environment_lifecycle_revision(case["prompt"])
        return {"environment": result[0] if result else None,
                "content": result[1] if result else None}
    if kind == "target":
        entries = [_seed_entry(spec) for spec in case.get("seed", [])]
        target = prompt_capture.find_environment_lifecycle_target(
            case["environment"], case["prompt"], entries,
        )
        aliases = {entry["id"]: spec["alias"]
                   for entry, spec in zip(entries, case.get("seed", []), strict=True)}
        return {"target": aliases.get((target or {}).get("id"))}
    with _isolated_case() as (repo, _root):
        if kind == "capture":
            return _capture_observation(case, repo)
        return _adapter_observation(case, repo)


def run_benchmark(fixture: dict[str, Any] | None = None) -> dict[str, Any]:
    data = fixture or load_fixture()
    validate_fixture(data)
    gaps = {gap["assertion_id"]: gap for gap in data["known_gaps"]}
    assertions: list[dict[str, Any]] = []
    case_times: list[float] = []
    auxiliary_output_lines = 0
    started = time.perf_counter()
    for case in data["cases"]:
        case_started = time.perf_counter()
        error = ""
        try:
            auxiliary = io.StringIO()
            with redirect_stdout(auxiliary):
                actual = observe(case)
            auxiliary_output_lines += len(auxiliary.getvalue().splitlines())
        except Exception as exc:  # benchmark errors are data, never known gaps
            actual = {}
            error = f"{type(exc).__name__}: {exc}"
        case_times.append((time.perf_counter() - case_started) * 1000)
        for field, expected in case["expected"].items():
            assertion_id = f"{case['case_id']}:{field}"
            observed = actual.get(field)
            passed = not error and observed == expected
            known_gap = assertion_id in gaps
            outcome = ("error" if error else "passed" if passed and not known_gap
                       else "xpass" if passed else "known_gap" if known_gap else "failed")
            assertions.append({
                "assertion_id": assertion_id, "case_id": case["case_id"],
                "family": case["family"], "field": field, "expected": expected,
                "actual": observed, "outcome": outcome, "error": error,
            })
    elapsed_ms = (time.perf_counter() - started) * 1000
    counts = Counter(item["outcome"] for item in assertions)
    semantic = {
        "assertions": len(assertions),
        "passed": counts["passed"],
        "known_gaps": counts["known_gap"],
        "unexpected_failures": counts["failed"],
        "unexpected_passes": counts["xpass"],
        "errors": counts["error"],
        "pass_rate": round(counts["passed"] / len(assertions), 4) if assertions else None,
    }
    sorted_times = sorted(case_times)
    p95_index = max(0, min(len(sorted_times) - 1, int(len(sorted_times) * 0.95) - 1))
    return {
        "schema_version": SCHEMA_VERSION,
        "runner_version": RUNNER_VERSION,
        "fixture_version": data["fixture_version"],
        "fixture_sha256": _fixture_hash(data),
        "git_revision": _git_revision(),
        "git_dirty": _git_dirty(),
        "runner_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "source_sha256": _source_hash(),
        "python": sys.version.split()[0],
        "semantic": semantic,
        "efficiency": {
            "cases": len(data["cases"]), "elapsed_ms": round(elapsed_ms, 2),
            "cases_per_second": round(len(data["cases"]) / (elapsed_ms / 1000), 2),
            "case_p50_ms": round(statistics.median(case_times), 3),
            "case_p95_ms": round(sorted_times[p95_index], 3),
            "captured_auxiliary_output_lines": auxiliary_output_lines,
        },
        "families": dict(sorted(Counter(case["family"] for case in data["cases"]).items())),
        "known_gap_registry": data["known_gaps"],
        "assertions": assertions,
    }


def gate_failures(report: dict[str, Any], *, strict: bool = False) -> list[str]:
    bad = {"failed", "error", "xpass"}
    if strict:
        bad.add("known_gap")
    return [item["assertion_id"] for item in report["assertions"] if item["outcome"] in bad]


def _text_report(report: dict[str, Any]) -> str:
    semantic = report["semantic"]
    efficiency = report["efficiency"]
    lines = [
        f"prompt-capture benchmark {report['fixture_version']} ({efficiency['cases']} cases)",
        (f"semantic: {semantic['passed']}/{semantic['assertions']} passed, "
         f"{semantic['known_gaps']} known gaps, "
         f"{semantic['unexpected_failures']} unexpected failures, "
         f"{semantic['unexpected_passes']} unexpected passes, {semantic['errors']} errors"),
        (f"efficiency: {efficiency['elapsed_ms']:.2f} ms total, "
         f"{efficiency['cases_per_second']:.2f} cases/s, "
         f"p50 {efficiency['case_p50_ms']:.3f} ms, p95 {efficiency['case_p95_ms']:.3f} ms"),
    ]
    for item in report["assertions"]:
        if item["outcome"] != "passed":
            lines.append(
                f"{item['outcome'].upper()} {item['assertion_id']}: "
                f"expected={item['expected']!r} actual={item['actual']!r} {item['error']}".rstrip())
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=FIXTURE_PATH)
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--strict", action="store_true",
                        help="also fail while any declared known gap remains")
    args = parser.parse_args(argv)
    report = run_benchmark(load_fixture(args.fixture))
    rendered = (json.dumps(report, indent=2, sort_keys=True) + "\n"
                if args.format == "json" else _text_report(report) + "\n")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        sys.stdout.write(rendered)
    return 1 if gate_failures(report, strict=args.strict) else 0


if __name__ == "__main__":
    raise SystemExit(main())
