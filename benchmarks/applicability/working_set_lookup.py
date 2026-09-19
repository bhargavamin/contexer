"""Reproducible Contract-05 prompt-route benchmark and paired release gate.

Single-arm diagnostic/timing run against one checkout::

    CONTEXER_BENCH_SOURCE_ROOT=/path/to/checkout \
      uv run --frozen python benchmarks/applicability/working_set_lookup.py --format json

Full acceptance campaign (five calibration pairs, five alternating comparison pairs, and five
post-run drift pairs)::

    uv run --frozen python benchmarks/applicability/working_set_lookup.py --campaign \
      --base-root /path/to/clean/base --candidate-root /path/to/candidate \
      --output /path/to/private/contract-05-campaign.json

Fixtures and state resets happen outside the timed request. Diagnostic runs count probes through
the real router. Timing runs are uninstrumented and compare exact output, metadata and normalized
post-request state with the diagnostic and with every other arm in the campaign.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import platform
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = Path(__file__).resolve()
SOURCE_ROOT = Path(os.environ.get("CONTEXER_BENCH_SOURCE_ROOT", ROOT)).resolve()
if str(SOURCE_ROOT) not in sys.path:
    sys.path.insert(0, str(SOURCE_ROOT))

from contexer import decision_impact, revisions, store, working_set  # noqa: E402


MANIFEST_PATH = SCRIPT_PATH.with_name("working_set_lookup_cases.json")
SESSION = "contract-05-benchmark"
FIXTURE_AT = "2026-01-01T00:00:00+00:00"
FIXTURE_NAMESPACE = uuid.UUID("712cd8c0-725d-50b7-a339-b341db1e50ee")
STATS = ("median_ms", "p95_nearest_rank_ms")
VOLATILE_STATE_KEYS = frozenset({
    "created_at", "cursor_secret", "elapsed_ms", "epoch", "event_id", "first", "last",
    "occurred_at", "receipt_id", "recorded_at", "timestamp", "ts",
})


def _topic_route_start() -> int:
    lines, start = inspect.getsourcelines(store._get_context_for_prompt)
    return start + next(
        index for index, line in enumerate(lines) if line.strip() == "if prompt_topics:"
    )


TOPIC_ROUTE_START = _topic_route_start()


def _canonical(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _tree_sha256(root: Path) -> str:
    rows = []
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" not in path.parts:
            rows.append((path.relative_to(root).as_posix(), _sha256(path)))
    return _digest(rows)


def _git_identity(root: Path) -> dict:
    def command(*args: str) -> str:
        result = subprocess.run(
            ["git", "-C", str(root), *args], capture_output=True, check=False, text=True,
        )
        return result.stdout.strip() if result.returncode == 0 else ""

    return {
        "head": command("rev-parse", "HEAD"),
        "status_sha256": _digest(command("status", "--short", "--untracked-files=all")),
        "diff_sha256": _digest(command("diff", "--no-ext-diff", "--binary", "HEAD")),
    }


def _token(index: int) -> str:
    value = index
    letters = []
    for _ in range(4):
        value, remainder = divmod(value, 26)
        letters.append(chr(ord("a") + remainder))
    return "token" + "".join(reversed(letters))


def _nearest_rank_p95(samples: list[float]) -> float:
    ordered = sorted(samples)
    return ordered[max(0, (95 * len(ordered) + 99) // 100 - 1)]


def _fixture_entry(index: int, *, global_scope: bool = False) -> dict:
    identity = f"global-{index}" if global_scope else f"personal-{index}"
    decision_id = str(uuid.uuid5(FIXTURE_NAMESPACE, f"decision:{identity}"))
    revision_id = str(uuid.uuid5(FIXTURE_NAMESPACE, f"revision:{identity}:1"))
    token = _token(index)
    if global_scope:
        content = "Secrets loading in contexer/global_config.py requires explicit ordering"
        title = "Global configuration loading"
    elif index == 1:
        content = f"Database {token} calculations live in billing.py and must round half up"
        title = f"{token} billing rule"
    else:
        content = f"Database {token} governs durable storage rule {index}"
        title = f"{token} storage rule"
    revision = revisions.new_revision(
        decision_id,
        1,
        content,
        "human",
        approved_at=FIXTURE_AT,
        created_at=FIXTURE_AT,
        title=title,
    )
    revision["revision_id"] = revision_id
    entry = {
        "id": decision_id,
        "type": "decision",
        "subtype": "architecture",
        "status": "approved",
        "current_revision_id": revision_id,
        "session_id": "fixture",
        "session_ids": ["fixture"],
        "timestamp": FIXTURE_AT,
        "updated_at": FIXTURE_AT,
        "occurrence_count": 1,
        "created_by": "human",
        "approved_by": "human",
        "revisions": [revision],
    }
    revisions.sync_decision_cache(entry)
    if global_scope or index == 0:
        entry["source_files"] = [
            "contexer/global_config.py" if global_scope else "contexer/ledger.py"
        ]
    return entry


def _build_fixture(root: Path, size: int) -> dict:
    store_root = root / ".contexer"
    store.store_dir = lambda: store_root
    repo = root / "repo"
    repo.mkdir()
    entries = [_fixture_entry(index) for index in range(size)]
    global_entries = [_fixture_entry(0, global_scope=True)]
    store.save(str(repo), {"repo_path": str(repo), "entries": entries})
    store.save_global({"entries": global_entries})
    index_path = store.sidecar_path("retrieval_index", slug=store.repo_slug(str(repo)))
    index_raw = index_path.read_text(encoding="utf-8")
    index = json.loads(index_raw)
    assert index is not None and len(index["docs"]) == size
    rows = [{
        "scope": "personal",
        "id": decision_id,
        "fingerprint": doc["guidance_fingerprint"],
    } for decision_id, doc in index["docs"].items()]
    identity = {
        "version": 2,
        "entries": entries,
        "global_entries": global_entries,
        "index": index,
        "rows": rows,
    }
    return {
        "repo": str(repo),
        "index": index,
        "index_raw": index_raw,
        "rows": rows,
        "queries": _queries(size),
        "fixture_sha256": _digest(identity),
    }


def _queries(size: int) -> dict[str, str]:
    tokens = [_token(index) for index in range(size)]
    return {
        "dense": "what database?",
        "absent": "what is tokenzzzz?",
        "one_first": f"what is {tokens[0]}?",
        "one_last": f"what is {tokens[-1]}?",
        "five": "what are " + " ".join(tokens[:5]) + "?",
        "fifty": "what are " + " ".join(tokens[:50]) + "?",
        "all_unique": "what are " + " ".join(tokens) + "?",
        "file_dense": "what database policy applies in contexer/ledger.py?",
        "file_global": "update contexer/global_config.py loading order",
        "file_mention": "check billing.py logic",
        "closed": "please continue with the ordinary task",
        "legacy_relevant": "why database?",
        "legacy_irrelevant": "please continue with the ordinary task",
    }


def _query(kind: str, size: int) -> str:
    return _queries(size)[kind]


def _ledger_rows(kind: str, rows: list[dict]) -> list[dict]:
    if kind == "empty":
        return []
    if kind == "first_10":
        return rows[:10]
    if kind == "first_50":
        return rows[:50]
    if kind == "one_miss":
        changed = [dict(row) for row in rows]
        changed[0]["fingerprint"] += ":stale"
        return changed
    if kind == "mixed_five":
        changed = [dict(row) for row in rows]
        changed[1]["fingerprint"] += ":stale"
        changed[3]["fingerprint"] += ":stale"
        return [changed[0], *changed[5:250], changed[2], *changed[250:], changed[4], changed[1],
                changed[3]]
    return [dict(row) for row in rows]


def _normalize_state(value: object, fixture_identities: frozenset[str]) -> object:
    if isinstance(value, dict):
        return {
            key: (
                "<volatile>" if key in VOLATILE_STATE_KEYS
                else _normalize_state(item, fixture_identities)
            )
            for key, item in sorted(value.items())
        }
    if isinstance(value, list):
        return [_normalize_state(item, fixture_identities) for item in value]
    if isinstance(value, str) and value in fixture_identities:
        return "<fixture-identity>"
    return value


def _optional_json(path: Path) -> object:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"unreadable_sha256": _sha256(path)}


def _json_lines(path: Path) -> object:
    if not path.exists():
        return None
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"unreadable_sha256": _sha256(path)}


def _snapshot(fixture: dict, session_id: str) -> dict:
    repo = fixture["repo"]
    slug = store.repo_slug(repo)
    fixture_identities = frozenset({
        decision_impact.identity("repository", store.canonical_store_key(repo)),
        decision_impact.identity("checkout", repo),
    })
    state = {
        "working_set": working_set.read(repo, session_id),
        "delivery_tally": _optional_json(working_set.delivery_path(repo)),
        "delivery_gaps": _optional_json(store.sidecar_path("delivery_gaps", slug=slug)),
        "retrieval_log": _json_lines(store.sidecar_path("retrieval_log", slug=slug)),
        "decision_impact": _optional_json(store.sidecar_path("decision_impact", slug=slug)),
    }
    return _normalize_state(state, fixture_identities)


def _reset(fixture: dict, case: dict) -> str:
    repo = fixture["repo"]
    slug = store.repo_slug(repo)
    index_path = store.sidecar_path("retrieval_index", slug=slug)
    index_mode = case.get("index", "present")
    if index_mode == "present":
        store.atomic_write(index_path, fixture["index_raw"])
    elif index_mode == "missing":
        index_path.unlink(missing_ok=True)
    else:
        index_path.parent.mkdir(parents=True, exist_ok=True)
        index_path.write_text("{corrupt", encoding="utf-8")

    session_id = "" if case["ledger"] == "no_session" else SESSION
    if session_id:
        assert working_set.write(
            repo, session_id, _ledger_rows(case["ledger"], fixture["rows"]), []
        )
    else:
        working_set.path(repo, SESSION).unlink(missing_ok=True)
    config_path = store.store_dir() / "config.toml"
    if case.get("diagnostics"):
        store.atomic_write(config_path, "[diagnostics]\ndecision_impact = true\n")
    else:
        config_path.unlink(missing_ok=True)
    for sidecar in (
        store.sidecar_path("retrieval_log", slug=slug),
        working_set.delivery_path(repo),
        store.sidecar_path("delivery_gaps", slug=slug),
        store.sidecar_path("decision_impact", slug=slug),
    ):
        sidecar.unlink(missing_ok=True)
    return session_id


def _invoke(fixture: dict, case: dict) -> dict:
    repo = fixture["repo"]
    session_id = "" if case["ledger"] == "no_session" else SESSION
    query = fixture["queries"][case["query"]]
    responses: list[dict] = []
    if case.get("sequence") == "compaction_prompt":
        responses.append({
            "kind": "compaction",
            "payload": store.post_compact_payload(repo, session_id),
        })
    for _ in range(case.get("repetitions", 1)):
        text, meta = store.get_context_for_prompt_with_meta(repo, query, session_id)
        responses.append({"kind": "prompt", "text": text, "meta": meta})
    return {"responses": responses}


def _output_texts(outcome: dict) -> list[str]:
    values = []
    for response in outcome["responses"]:
        if response["kind"] == "prompt":
            values.append(response["text"])
        else:
            values.append(response["payload"].get("context", ""))
    return values


def _normalize_fixture_paths(value: object, repo_path: str) -> object:
    if isinstance(value, dict):
        return {key: _normalize_fixture_paths(item, repo_path) for key, item in value.items()}
    if isinstance(value, list):
        return [_normalize_fixture_paths(item, repo_path) for item in value]
    if isinstance(value, str):
        return value.replace(repo_path, "<fixture-repo>")
    return value


def _behavior(fixture: dict, case: dict, outcome: dict) -> dict:
    session_id = "" if case["ledger"] == "no_session" else SESSION
    normalized_outcome = _normalize_fixture_paths(outcome, fixture["repo"])
    texts = _output_texts(normalized_outcome)
    metadata = [
        response.get("meta")
        for response in normalized_outcome["responses"]
        if response["kind"] == "prompt"
    ]
    state = _snapshot(fixture, session_id)
    return {
        "output_count": len(texts),
        "output_bytes": sum(len(text.encode("utf-8")) for text in texts),
        "output_sha256": _digest(texts),
        "meta_sha256": _digest(metadata),
        "state_sha256": _digest(state),
        "behavior_sha256": _digest({"outcome": normalized_outcome, "state": state}),
        "ledger_size": len(state["working_set"]["records"]),
    }


def _probe_route() -> str:
    frame = inspect.currentframe()
    try:
        frame = frame.f_back if frame is not None else None
        while frame is not None:
            if Path(frame.f_code.co_filename).resolve() == Path(store.__file__).resolve():
                if frame.f_code.co_name == "_prompt_file_hits":
                    return "file"
                if frame.f_code.co_name == "<listcomp>":
                    return "ranked"
                if frame.f_code.co_name == "_get_context_for_prompt":
                    return "topic" if frame.f_lineno >= TOPIC_ROUTE_START else "ranked"
            frame = frame.f_back
    finally:
        del frame
    return "other"


def _assert_case_contract(case: dict, behavior: dict, route_probes: dict) -> None:
    expected_routes = case["expected_route_probes"]
    if route_probes != expected_routes:
        raise AssertionError(
            f"{case['name']}: expected route probes {expected_routes}, observed {route_probes}"
        )
    expected = case.get("expected_behavior")
    if expected is not None:
        observed = {key: behavior[key] for key in expected}
        if observed != expected:
            raise AssertionError(
                f"{case['name']}: expected behavior {expected}, observed {observed}"
            )


def _diagnose(fixture: dict, case: dict) -> dict:
    _reset(fixture, case)
    original_probe = working_set.has_credit
    original_build = getattr(working_set, "credit_lookup", None)
    counts = {
        "probes": 0,
        "builds": 0,
        "route_probes": {"file": 0, "ranked": 0, "topic": 0, "other": 0},
    }

    def counted_probe(*args, **kwargs):
        counts["probes"] += 1
        counts["route_probes"][_probe_route()] += 1
        return original_probe(*args, **kwargs)

    def counted_build(*args, **kwargs):
        counts["builds"] += 1
        return original_build(*args, **kwargs)

    working_set.has_credit = counted_probe
    if original_build is not None:
        working_set.credit_lookup = counted_build
    try:
        outcome = _invoke(fixture, case)
    finally:
        working_set.has_credit = original_probe
        if original_build is not None:
            working_set.credit_lookup = original_build
    behavior = _behavior(fixture, case, outcome)
    _assert_case_contract(case, behavior, counts["route_probes"])
    return {**counts, **behavior}


def _measure(fixture: dict, case: dict, warmup: int, samples: int, expected: dict) -> dict:
    for _ in range(warmup):
        _reset(fixture, case)
        _invoke(fixture, case)
    elapsed = []
    for _ in range(samples):
        _reset(fixture, case)
        started = time.perf_counter_ns()
        outcome = _invoke(fixture, case)
        elapsed.append((time.perf_counter_ns() - started) / 1_000_000)
        observed = _behavior(fixture, case, outcome)
        for key in (
            "output_count", "output_bytes", "output_sha256", "meta_sha256", "state_sha256",
            "behavior_sha256", "ledger_size",
        ):
            if observed[key] != expected[key]:
                raise AssertionError(
                    f"{case['name']}: {key} changed between diagnostic and timing run"
                )
    return {
        "median_ms": statistics.median(elapsed),
        "p95_nearest_rank_ms": _nearest_rank_p95(elapsed),
        "min_ms": min(elapsed),
        "max_ms": max(elapsed),
        "samples_ms": elapsed,
    }


def _validate_manifest(manifest: dict) -> None:
    if (manifest.get("warmup"), manifest.get("samples_per_arm_per_batch"),
            manifest.get("pairs_per_phase")) != (20, 100, 5):
        raise ValueError("manifest release schedule changed without updating the frozen gate")
    size = manifest["corpus_size"]
    names = set()
    for case in manifest["cases"]:
        if case["name"] in names:
            raise ValueError(f"duplicate case: {case['name']}")
        names.add(case["name"])
        query_hash = _digest(_query(case["query"], size))
        if query_hash != case["query_sha256"]:
            raise ValueError(f"{case['name']}: frozen query hash mismatch")
        if sum(case["expected_route_probes"].values()) != case["expected_probes"]:
            raise ValueError(f"{case['name']}: route probes do not sum to expected_probes")
    if set(manifest["expected_behaviors"]) != names:
        raise ValueError("expected behavior cases do not match the frozen case set")


def run(*, warmup: int, samples: int, selected: set[str] | None = None,
        label: str = "arm") -> dict:
    source_before = _tree_sha256(SOURCE_ROOT / "contexer")
    benchmark_before = _sha256(SCRIPT_PATH)
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    _validate_manifest(manifest)
    cases = [
        {**case, "expected_behavior": manifest["expected_behaviors"][case["name"]]}
        for case in manifest["cases"]
        if selected is None or case["name"] in selected
    ]
    missing = (selected or set()) - {case["name"] for case in cases}
    if missing:
        raise SystemExit(f"unknown cases: {', '.join(sorted(missing))}")
    with tempfile.TemporaryDirectory(prefix="contexer-working-set-bench-") as temp:
        fixture = _build_fixture(Path(temp), manifest["corpus_size"])
        expected_fixture = manifest.get("fixture_sha256")
        if expected_fixture and fixture["fixture_sha256"] != expected_fixture:
            raise AssertionError(
                f"fixture hash mismatch: {fixture['fixture_sha256']} != {expected_fixture}"
            )
        results = []
        for case in cases:
            diagnostic = _diagnose(fixture, case)
            timing = _measure(fixture, case, warmup, samples, diagnostic)
            results.append({
                "name": case["name"],
                "ledger": case["ledger"],
                "expected_probes": case["expected_probes"],
                "probes": diagnostic["probes"],
                "route_probes": diagnostic["route_probes"],
                "lookup_builds": diagnostic["builds"],
                "output_count": diagnostic["output_count"],
                "output_bytes": diagnostic["output_bytes"],
                "output_sha256": diagnostic["output_sha256"],
                "meta_sha256": diagnostic["meta_sha256"],
                "state_sha256": diagnostic["state_sha256"],
                "behavior_sha256": diagnostic["behavior_sha256"],
                "ledger_size": diagnostic["ledger_size"],
                **timing,
            })

    source_after = _tree_sha256(SOURCE_ROOT / "contexer")
    benchmark_after = _sha256(SCRIPT_PATH)
    if source_after != source_before or benchmark_after != benchmark_before:
        raise AssertionError("source changed during benchmark arm")

    imported = {
        "store": Path(store.__file__).resolve(),
        "working_set": Path(working_set.__file__).resolve(),
    }
    return {
        "label": label,
        "manifest_sha256": _sha256(MANIFEST_PATH),
        "fixture_sha256": fixture["fixture_sha256"],
        "benchmark_sha256": benchmark_after,
        # Every arm is launched by this checkout's one frozen interpreter environment. Retain
        # the source checkout's lock separately because a historical base can legitimately pin
        # the package's earlier self-version while still running in the identical environment.
        "lock_sha256": _sha256(ROOT / "uv.lock"),
        "source_lock_sha256": _sha256(SOURCE_ROOT / "uv.lock"),
        "source_tree_sha256_before": source_before,
        "source_tree_sha256_after": source_after,
        "git": _git_identity(SOURCE_ROOT),
        "python": sys.version,
        "platform": platform.platform(),
        "warmup": warmup,
        "samples_per_case": samples,
        "imports": {name: str(path) for name, path in imported.items()},
        "source_sha256": {name: _sha256(path) for name, path in imported.items()},
        "results": results,
    }


def _child_run(source_root: Path, *, label: str, warmup: int, samples: int,
               selected: set[str] | None) -> dict:
    command = [
        sys.executable, str(SCRIPT_PATH), "--format", "json", "--warmup", str(warmup),
        "--samples", str(samples), "--label", label,
    ]
    for name in sorted(selected or set()):
        command.extend(("--case", name))
    env = dict(os.environ)
    env["CONTEXER_BENCH_SOURCE_ROOT"] = str(source_root)
    result = subprocess.run(command, capture_output=True, check=False, text=True, env=env)
    if result.returncode != 0:
        raise RuntimeError(
            f"benchmark arm {label} failed ({result.returncode}):\n{result.stderr}\n{result.stdout}"
        )
    return json.loads(result.stdout)


def _result_map(report: dict) -> dict[str, dict]:
    return {row["name"]: row for row in report["results"]}


def _assert_reports_compatible(reports: list[dict]) -> None:
    first = reports[0]
    identity_keys = ("manifest_sha256", "fixture_sha256", "benchmark_sha256", "lock_sha256")
    for report in reports[1:]:
        for key in identity_keys:
            if report[key] != first[key]:
                raise AssertionError(f"campaign {key} mismatch")
        left = _result_map(first)
        right = _result_map(report)
        if left.keys() != right.keys():
            raise AssertionError("campaign case set mismatch")
        for name in left:
            for key in (
                "output_count", "output_bytes", "output_sha256", "meta_sha256", "state_sha256",
                "behavior_sha256", "ledger_size", "probes", "route_probes",
            ):
                if left[name][key] != right[name][key]:
                    raise AssertionError(f"{name}: base/candidate {key} mismatch")


def _source_identity(report: dict) -> dict:
    before = report["source_tree_sha256_before"]
    after = report["source_tree_sha256_after"]
    if before != after:
        raise AssertionError("source changed during benchmark arm")
    return {
        "source_tree_sha256": before,
        "source_lock_sha256": report["source_lock_sha256"],
        "source_sha256": report["source_sha256"],
    }


def _assert_role_sources_stable(phases: dict[str, list[dict]]) -> None:
    reports_by_role = {"base": [], "candidate": []}
    for pair in phases["calibration"]:
        reports_by_role["base"].extend((pair["a"], pair["b"]))
    for pair in phases["comparison"]:
        reports_by_role["base"].append(pair["a"])
        reports_by_role["candidate"].append(pair["b"])
    for pair in phases["drift"]:
        reports_by_role["base"].extend((pair["a"], pair["b"]))

    for role, reports in reports_by_role.items():
        if not reports:
            continue
        expected = _source_identity(reports[0])
        for report in reports[1:]:
            if _source_identity(report) != expected:
                raise AssertionError(
                    f"{role} source identity changed between benchmark batches"
                )


def _pair(root_a: Path, root_b: Path, *, phase: str, pair_index: int, reverse: bool,
          warmup: int, samples: int, selected: set[str] | None) -> dict:
    arms = [("a", root_a), ("b", root_b)]
    if reverse:
        arms.reverse()
    reports = {}
    order = []
    for arm, root in arms:
        label = f"{phase}-{pair_index + 1}-{arm}"
        order.append(arm)
        reports[arm] = _child_run(
            root, label=label, warmup=warmup, samples=samples, selected=selected,
        )
    return {"pair": pair_index + 1, "order": order, "a": reports["a"], "b": reports["b"]}


def _calibration(pairs: list[dict]) -> dict:
    cases = _result_map(pairs[0]["a"])
    result = {}
    for name in cases:
        result[name] = {}
        for stat in STATS:
            values = []
            differences = []
            for pair in pairs:
                a = _result_map(pair["a"])[name][stat]
                b = _result_map(pair["b"])[name][stat]
                values.extend((a, b))
                differences.append(abs(a - b))
            reference = statistics.median(values)
            allowance = max(differences)
            stable = reference > 0 and allowance <= 0.05 * reference and allowance <= 0.1
            result[name][stat] = {
                "R": reference,
                "T": allowance,
                "pair_differences_ms": differences,
                "stable": stable,
            }
    return result


def _evaluate_comparison(pairs: list[dict], calibration: dict) -> dict:
    cases = _result_map(pairs[0]["a"])
    result = {}
    for name in cases:
        result[name] = {}
        for stat in STATS:
            deltas = []
            alarms = []
            target_passes = []
            for pair in pairs:
                base = _result_map(pair["a"])[name][stat]
                candidate = _result_map(pair["b"])[name][stat]
                delta = candidate - base
                deltas.append(delta)
                alarms.append(delta > 0.1 * base or delta > 0.2)
                if name == "dense_full":
                    ratio = 0.75 if stat == "median_ms" else 0.80
                    target_passes.append(candidate <= ratio * base)
            allowance = calibration[name][stat]["T"]
            passing = sum(delta <= allowance for delta in deltas)
            failing = sum(delta > allowance for delta in deltas)
            status = "pass" if passing >= 4 else "fail" if failing >= 4 else "inconclusive"
            result[name][stat] = {
                "deltas_ms": deltas,
                "within_T": passing,
                "status": status,
                "large_slowdown_alarm": sum(alarms) >= 2,
                "dense_target_passes": sum(target_passes) if target_passes else None,
                "dense_target_met": sum(target_passes) >= 4 if target_passes else None,
            }
    return result


def _evaluate_drift(pairs: list[dict], calibration: dict) -> dict:
    cases = _result_map(pairs[0]["a"])
    result = {}
    for name in cases:
        result[name] = {}
        for stat in STATS:
            values = []
            differences = []
            for pair in pairs:
                a = _result_map(pair["a"])[name][stat]
                b = _result_map(pair["b"])[name][stat]
                values.extend((a, b))
                differences.append(abs(a - b))
            allowance = calibration[name][stat]["T"]
            pair_passes = sum(value <= allowance for value in differences)
            reference_delta = abs(statistics.median(values) - calibration[name][stat]["R"])
            result[name][stat] = {
                "pair_differences_ms": differences,
                "within_T": pair_passes,
                "reference_delta_ms": reference_delta,
                "status": "pass" if pair_passes >= 4 and reference_delta <= allowance else "fail",
            }
    return result


def campaign(*, base_root: Path, candidate_root: Path, warmup: int, samples: int, pairs: int,
             selected: set[str] | None) -> dict:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    _validate_manifest(manifest)
    phases = {"calibration": [], "comparison": [], "drift": []}
    for index in range(pairs):
        phases["calibration"].append(_pair(
            base_root, base_root, phase="calibration", pair_index=index, reverse=bool(index % 2),
            warmup=warmup, samples=samples, selected=selected,
        ))
    _assert_role_sources_stable(phases)
    calibration = _calibration(phases["calibration"])
    stable = all(row[stat]["stable"] for row in calibration.values() for stat in STATS)
    full_schedule = (
        pairs == manifest["pairs_per_phase"]
        and samples == manifest["samples_per_arm_per_batch"]
        and warmup == manifest["warmup"]
        and selected is None
    )
    acceptance_eligible = stable and full_schedule
    if full_schedule and not stable:
        return {
            "accepted": False,
            "acceptance_eligible": False,
            "failure": "noisy_calibration",
            "settings": {
                "base_root": str(base_root),
                "candidate_root": str(candidate_root),
                "warmup": warmup,
                "samples_per_arm_per_batch": samples,
                "pairs_per_phase": pairs,
                "selected_cases": None,
            },
            "calibration": calibration,
            "comparison": None,
            "drift": None,
            "phases": phases,
        }

    for index in range(pairs):
        phases["comparison"].append(_pair(
            base_root, candidate_root, phase="comparison", pair_index=index,
            reverse=bool(index % 2), warmup=warmup, samples=samples, selected=selected,
        ))
    _assert_role_sources_stable(phases)
    for index in range(pairs):
        phases["drift"].append(_pair(
            base_root, base_root, phase="drift", pair_index=index, reverse=bool(index % 2),
            warmup=warmup, samples=samples, selected=selected,
        ))
    _assert_role_sources_stable(phases)

    all_reports = [
        pair[arm] for phase in phases.values() for pair in phase for arm in ("a", "b")
    ]
    _assert_reports_compatible(all_reports)
    comparison = _evaluate_comparison(phases["comparison"], calibration)
    drift = _evaluate_drift(phases["drift"], calibration)
    controls_pass = all(
        row[stat]["status"] == "pass" and not row[stat]["large_slowdown_alarm"]
        for row in comparison.values() for stat in STATS
    )
    dense_pass = all(comparison["dense_full"][stat]["dense_target_met"] for stat in STATS)
    drift_pass = all(row[stat]["status"] == "pass" for row in drift.values() for stat in STATS)
    accepted = acceptance_eligible and controls_pass and dense_pass and drift_pass
    return {
        "accepted": accepted,
        "acceptance_eligible": acceptance_eligible,
        "failure": None if accepted else "release_gate_not_met",
        "settings": {
            "base_root": str(base_root),
            "candidate_root": str(candidate_root),
            "warmup": warmup,
            "samples_per_arm_per_batch": samples,
            "pairs_per_phase": pairs,
            "selected_cases": sorted(selected) if selected else None,
        },
        "calibration": calibration,
        "comparison": comparison,
        "drift": drift,
        "phases": phases,
    }


def _write_report(report: dict, output: Path | None) -> str:
    rendered = json.dumps(report, indent=2)
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(rendered + "\n", encoding="utf-8")
    return rendered


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--warmup", type=int, default=20)
    parser.add_argument("--samples", type=int, default=100)
    parser.add_argument("--case", action="append", dest="cases")
    parser.add_argument("--format", choices=("json", "text"), default="text")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--label", default="arm")
    parser.add_argument("--campaign", action="store_true")
    parser.add_argument("--base-root", type=Path)
    parser.add_argument("--candidate-root", type=Path)
    parser.add_argument("--pairs", type=int, default=5)
    args = parser.parse_args()
    if args.warmup < 0 or args.samples <= 0 or args.pairs <= 0:
        parser.error("--warmup must be >= 0; --samples and --pairs must be > 0")
    selected = set(args.cases) if args.cases else None
    if args.campaign:
        if args.base_root is None or args.candidate_root is None:
            parser.error("--campaign requires --base-root and --candidate-root")
        report = campaign(
            base_root=args.base_root.resolve(),
            candidate_root=args.candidate_root.resolve(),
            warmup=args.warmup,
            samples=args.samples,
            pairs=args.pairs,
            selected=selected,
        )
        rendered = _write_report(report, args.output)
        if args.format == "json":
            print(rendered)
        else:
            print(
                f"accepted={report['accepted']} eligible={report['acceptance_eligible']} "
                f"report={args.output or '<stdout suppressed>'}"
            )
        full_schedule = (
            args.pairs == 5 and args.samples == 100 and args.warmup == 20 and selected is None
        )
        if full_schedule and not report["accepted"]:
            raise SystemExit(1)
        return

    report = run(
        warmup=args.warmup, samples=args.samples, selected=selected, label=args.label,
    )
    rendered = _write_report(report, args.output)
    if args.format == "json":
        print(rendered)
        return
    for row in report["results"]:
        print(
            f"{row['name']}: probes={row['probes']} routes={row['route_probes']} "
            f"builds={row['lookup_builds']} median={row['median_ms']:.3f}ms "
            f"p95={row['p95_nearest_rank_ms']:.3f}ms bytes={row['output_bytes']} "
            f"behavior={row['behavior_sha256'][:12]}"
        )


if __name__ == "__main__":
    main()
