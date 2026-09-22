from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from benchmarks.applicability import pilot_readiness
from benchmarks.applicability import qualification_preparation as qualification


HASH_A = "a" * 64
HEAD = "c" * 40


def _write(path: Path, value: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, (dict, list)):
        path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    else:
        path.write_text(str(value), encoding="utf-8")
    return path


def _ref(root: Path, path: Path) -> dict[str, str]:
    return {
        "path": path.relative_to(root).as_posix(),
        "sha256": qualification.sha256(path),
    }


def _snapshot(root: Path, repo_index: int) -> tuple[Path, dict]:
    value = {
        "schema_version": 1,
        "snapshot_id": f"snapshot-{repo_index}",
        "repo_id": f"synthetic/repo-{repo_index}",
        "decisions": [{
            "decision_id": f"decision-{repo_index}",
            "revision_id": f"revision-{repo_index}",
            "content": (
                "Payment retries use bounded exponential backoff and idempotency keys."
                if repo_index == 0
                else f"Repository {repo_index} uses explicit bounded validation rules."
            ),
            "title": "Bound payment retries" if repo_index == 0 else f"Validate repo {repo_index}",
            "status": "approved",
            "subtype": "architecture",
            "scope": "personal",
            "timestamp": "2026-09-20T00:00:00+00:00",
            "source": "human",
            "source_files": [f"src/repo_{repo_index}.py"],
            "proposal": None,
            "conflict": False,
        }],
    }
    return _write(root / "snapshots" / f"repo-{repo_index}.json", value), value


def _pilot_core(root: Path, snapshots: list[tuple[Path, dict]]) -> Path:
    tasks = []
    for index in range(6):
        repo_index = index % 3
        snapshot_path, snapshot = snapshots[repo_index]
        prompt = _write(root / "pilot" / f"task-{index}.txt", f"Implement pilot task {index}\n")
        initial = _write(root / "pilot" / f"initial-{index}.json", {
            "schema_version": 1,
            "standing_revisions": [],
            "working_set": [],
        })
        checks = _write(root / "pilot" / f"checks-{index}.json", {
            "schema_version": 1,
            "functional_checks": [{
                "check_id": f"task-{index}.functional", "description": "works",
            }],
            "mandatory_conditions": [{
                "check_id": f"task-{index}.condition", "description": "keeps the rule",
            }],
        })
        tasks.append({
            "task_id": f"task-{index}",
            "repo_id": snapshot["repo_id"],
            "family_id": f"pilot-family-{index % 3}",
            "prompt_ref": _ref(root, prompt),
            "prompt_sha256": qualification.sha256(prompt),
            "snapshot_ref": _ref(root, snapshot_path),
            "snapshot_id": snapshot["snapshot_id"],
            "snapshot_sha256": qualification.sha256(snapshot_path),
            "initial_context_ref": _ref(root, initial),
            "initial_context_sha256": qualification.sha256(initial),
            "check_spec_ref": _ref(root, checks),
            "check_spec_sha256": qualification.sha256(checks),
        })
    return _write(root / "pilot-core.json", {
        "schema_version": 1,
        "artifact_kind": "pilot_task_core",
        "core_id": "contract08-test-core",
        "tasks": tasks,
    })


def _case(
    root: Path,
    snapshots: list[tuple[Path, dict]],
    index: int,
    category: str,
    *,
    origin: str,
    initial_working_set: bool = False,
) -> dict:
    repo_index = index % 3
    snapshot_path, snapshot = snapshots[repo_index]
    decision = snapshot["decisions"][0]
    case_id = f"{category}-{index:03d}"
    if category == "positive":
        prompt_text = (
            "Implement payment retry backoff"
            if repo_index == 0 else f"Implement bounded validation for repo {repo_index}"
        )
        applicable = [{
            "decision_id": decision["decision_id"],
            "revision_id": decision["revision_id"],
            "required_tier": "prompt_full",
        }]
        assertions = []
    elif category == "negative":
        prompt_text = f"Add unrelated dashboard color {index}"
        applicable = []
        assertions = []
    else:
        prompt_text = f"Why does repository {repo_index} use bounded validation rules?"
        applicable = [{
            "decision_id": decision["decision_id"],
            "revision_id": decision["revision_id"],
            "required_tier": "prompt_full",
        }]
        assertions = [{
            "assertion_id": f"control-{index}.supported",
            "arm": "candidate",
            "field": "supported",
            "operator": "eq",
            "value": True,
            "control_kind": ["authority", "legacy", "notice"][index % 3],
        }]
    prompt = _write(root / "cases" / f"{case_id}.txt", prompt_text + "\n")
    label = _write(root / "labels" / f"{case_id}.json", {
        "schema_version": 1,
        "case_id": case_id,
        "applicable": applicable,
        "expected_silence": category == "negative",
        "expected_clarification": False,
        "available_but_inapplicable": category == "negative",
        "control_assertions": assertions,
    })
    rationale = _write(
        root / "rationales" / f"{case_id}.txt",
        f"Source-grounded rationale for {case_id}.\n",
    )
    working_set = []
    if initial_working_set:
        working_set = [{
            "scope": "personal",
            "decision_id": decision["decision_id"],
            "revision_id": decision["revision_id"],
        }]
    initial = _write(root / "initial" / f"{case_id}.json", {
        "schema_version": 1,
        "standing_revisions": [],
        "working_set": working_set,
    })
    return {
        "case_id": case_id,
        "family_id": f"{origin}-family-{index:03d}",
        "repo_id": snapshot["repo_id"],
        "category": category,
        "host": "claude",
        "prompt_ref": _ref(root, prompt),
        "prompt_sha256": qualification.sha256(prompt),
        "snapshot_ref": _ref(root, snapshot_path),
        "snapshot_id": snapshot["snapshot_id"],
        "label_ref": _ref(root, label),
        "rationale_ref": _ref(root, rationale),
        "initial_context_ref": _ref(root, initial),
        "source_grant_id": "grant-1",
        "source_origin": "generated-test-data",
        "dataset_membership": origin,
        "exposure_history": ["authored"],
        "independent_draw": category == "negative",
        "sampling_distribution": "frozen-test-population-v1" if category == "negative" else None,
        "hard_negative": category == "negative" and index == 0,
        "ambiguous": False,
        "safety_critical": False,
    }


def _manifest(tmp_path: Path, *, origin: str = "development") -> tuple[Path, dict]:
    root = tmp_path
    snapshots = [_snapshot(root, index) for index in range(3)]
    core = _pilot_core(root, snapshots)
    if origin == "qualification":
        cases = (
            [_case(root, snapshots, i, "positive", origin=origin) for i in range(24)]
            + [_case(root, snapshots, i, "negative", origin=origin) for i in range(80)]
            + [_case(root, snapshots, i, "control", origin=origin) for i in range(12)]
        )
    else:
        cases = [
            _case(root, snapshots, 0, "positive", origin=origin),
            _case(root, snapshots, 1, "positive", origin=origin, initial_working_set=True),
            _case(root, snapshots, 2, "negative", origin=origin),
            _case(root, snapshots, 3, "control", origin=origin),
        ]
    support = {}
    for name in ("base.patch", "base.untracked"):
        support[name] = _write(root / "source" / name, f"frozen {name}\n")
    executed_source = qualification._executed_checkout_source()
    for name, content in (
        ("candidate.patch", executed_source["patch"]),
        ("candidate.untracked", executed_source["untracked"]),
    ):
        path = root / "source" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        support[name] = path
    support["uv.lock"] = _write(
        root / "source" / "uv.lock",
        (Path(qualification.__file__).resolve().parents[2] / "uv.lock").read_text(
            encoding="utf-8"
        ),
    )
    support["collector.py"] = _write(
        root / "source" / "collector.py",
        Path(qualification.__file__).read_text(encoding="utf-8"),
    )
    support["formatter.py"] = _write(
        root / "source" / "formatter.py",
        Path(qualification.task_outcome_experiment.__file__).read_text(encoding="utf-8"),
    )
    history = root / "exposure.jsonl"
    qualification.append_exposure(history, f"dataset-{origin}", "draft")
    counts = {category: sum(row["category"] == category for row in cases)
              for category in qualification.CASE_CATEGORIES}
    base_patch_sha256 = qualification.sha256(support["base.patch"])
    base_untracked_sha256 = qualification.sha256(support["base.untracked"])
    base_source_sha256 = qualification._source_state_digest(
        HEAD, base_patch_sha256, base_untracked_sha256,
    )
    candidate_source = executed_source["identity"]
    manifest = {
        "schema_version": 1,
        "artifact_kind": "qualification_manifest",
        "dataset_id": f"dataset-{origin}",
        "candidate_variant": "ordinary_task_v1",
        "data_origin": origin,
        "execution_kind": "deterministic_offline_retrieval",
        "sources": {
            "baseline": {
                "head": HEAD,
                "diff_sha256": base_source_sha256,
                "patch_ref": _ref(root, support["base.patch"]),
                "untracked_ref": _ref(root, support["base.untracked"]),
            },
            "candidate": {
                "head": candidate_source["head"],
                "diff_sha256": candidate_source["diff_sha256"],
                "patch_ref": _ref(root, support["candidate.patch"]),
                "untracked_ref": _ref(root, support["candidate.untracked"]),
            },
            "dependency_lock": _ref(root, support["uv.lock"]),
            "collector": _ref(root, support["collector.py"]),
            "formatter": _ref(root, support["formatter.py"]),
        },
        "source_grants": [{
            "grant_id": "grant-1",
            "consent_record": "User designated generated test material for local evaluation.",
            "local_evaluation_only": True,
            "granted_at": "2026-09-20T00:00:00+00:00",
        }],
        "pilot_core_ref": _ref(root, core),
        "development_family_ids": [],
        "family_aliases": [],
        "cases": cases,
        "sampling": {
            "target_population": "generated Contract-08 test tasks",
            "selection_rule": "predeclared deterministic construction",
            "seed": 808,
            "counts": counts,
            "stopping_rule": "use every predeclared case exactly once",
        },
        "exposure_history_path": history.relative_to(root).as_posix(),
        "run_history_path": "runs.jsonl",
    }
    if origin == "qualification":
        manifest["final_evaluation_binding"] = {
            "campaign_id": "contract08-final-test",
            "integration_source_sha256": candidate_source["diff_sha256"],
            "integration_source": {
                "head": candidate_source["head"],
                "diff_sha256": candidate_source["diff_sha256"],
            },
        }
    path = _write(root / "qualification-manifest.json", manifest)
    return path, manifest


def _attach_review(manifest_path: Path) -> Path:
    manifest = qualification.validate_manifest(manifest_path)
    packet = qualification.build_review_packet(manifest_path)
    packet_hash = qualification.digest(packet)
    records = []
    for case in manifest["cases"]:
        records.append({
            "case_id": case["case_id"],
            "label_sha256": case["label_ref"]["sha256"],
            "rationale_sha256": case["rationale_ref"]["sha256"],
            "review_packet_sha256": packet_hash,
            "reviewers": [{
                "reviewer_id": "human-reviewer-1",
                "human_independent": True,
                "implementation_agent": False,
                "conflict_attestation": "none",
                "verdict": "accept",
                "rationale": "Reviewed source, task, and expected revision.",
                "reviewed_at": "2026-09-20T12:00:00+00:00",
            }],
        })
    attestation = _write(manifest_path.parent / "review-attestation.json", {
        "schema_version": 1,
        "artifact_kind": "operator_imported_review",
        "dataset_id": manifest["dataset_id"],
        "review_packet_sha256": packet_hash,
        "operator": {
            "operator_id": "operator-1",
            "external_review_import_confirmed": True,
            "collector_generated": False,
        },
        "records": records,
    })
    manifest["review_attestation_ref"] = _ref(manifest_path.parent, attestation)
    _write(manifest_path, manifest)
    qualification.append_exposure(
        manifest_path.parent / manifest["exposure_history_path"],
        manifest["dataset_id"],
        "frozen_unopened",
        review_attestation_sha256=qualification.sha256(attestation),
    )
    return attestation


def _synthetic_observations(manifest_path: Path) -> Path:
    manifest = qualification.validate_manifest(manifest_path)
    root = manifest_path.parent
    rows = []
    for case in manifest["cases"]:
        label = qualification.load_json(root / case["label_ref"]["path"])
        emitted = [{
            "decision_id": item["decision_id"],
            "revision_id": item["revision_id"],
            "tier": "prompt_full",
        } for item in label["applicable"]] if case["category"] == "positive" else []
        arms = {}
        for arm in ("baseline", "candidate"):
            arm_emitted = emitted if arm == "candidate" else []
            arms[arm] = {
                "supported": True,
                "found_revisions": arm_emitted,
                "selected_revisions": arm_emitted,
                "emitted_revisions": arm_emitted,
                "emitted_revision_ids": [row["revision_id"] for row in arm_emitted],
                "pointer_emitted": False,
                "task_route_selected": arm == "candidate" and case["category"] == "positive",
                "origin": "ordinary_task_v1" if arm_emitted else None,
                "content_tier": "strong" if arm_emitted else None,
                "user_notice": False,
                "router_context_in_payload": True,
                "authority_valid": True,
                "initial_working_set_records": [],
            }
        rows.append({
            "case_id": case["case_id"],
            "family_id": case["family_id"],
            "repo_id": case["repo_id"],
            "category": case["category"],
            "host": case["host"],
            "status": "observed",
            "initial_standing_revisions": [],
            "arms": arms,
        })
    source_chain = qualification._source_chain(manifest_path, manifest)
    path = _write(root / "qualification-observations.json", {
        "schema_version": 1,
        "artifact_kind": "qualification_observations",
        "dataset_id": manifest["dataset_id"],
        "qualification_manifest_sha256": qualification.sha256(manifest_path),
        "source_chain": source_chain,
        "source_chain_sha256": qualification.digest(source_chain),
        "data_origin": manifest["data_origin"],
        "execution_kind": "deterministic_offline_retrieval",
        "collector_sha256": manifest["sources"]["collector"]["sha256"],
        "formatter_sha256": manifest["sources"]["formatter"]["sha256"],
        "host_receipt_observed": False,
        "model_invoked": False,
        "network_used": False,
        "repository_commands_executed": False,
        "cases": rows,
    })
    history = root / manifest["exposure_history_path"]
    qualification.append_exposure(
        history,
        manifest["dataset_id"],
        "opening_intent",
        qualification_manifest_sha256=qualification.sha256(manifest_path),
        binding=manifest["final_evaluation_binding"],
        source_chain_sha256=qualification.digest(source_chain),
        test_fixture=True,
    )
    qualification.append_exposure(
        history, manifest["dataset_id"], "consumed", result="completed",
    )
    run_history = root / manifest["run_history_path"]
    attempt_id = f"{manifest['dataset_id']}:attempt:1"
    qualification.append_run_history(
        run_history, manifest["dataset_id"], "started", attempt_id,
        output_path=str(path),
    )
    qualification.append_run_history(
        run_history, manifest["dataset_id"], "completed", attempt_id,
        output_exists=True, output_sha256=qualification.sha256(path),
    )
    return path


def _campaign(manifest_path: Path) -> dict:
    qualification_manifest = qualification.load_json(manifest_path)
    root = manifest_path.parent
    core_path = root / qualification_manifest["pilot_core_ref"]["path"]
    tasks = qualification.pilot_core_task_bindings(core_path)
    candidate_source = qualification_manifest["sources"]["candidate"]
    return {
        "schema_version": 2,
        "campaign_id": "contract08-final-test",
        "candidate_variant": "ordinary_task_v1",
        "development_only": True,
        "integration_source": {
            "head": candidate_source["head"],
            "diff_sha256": candidate_source["diff_sha256"],
        },
        "candidate_source_sha256": candidate_source["diff_sha256"],
        "qualification_source_chain_sha256": qualification.digest(
            qualification._source_chain(manifest_path, qualification_manifest)
        ),
        "label_manifest_sha256": qualification.sha256(manifest_path),
        "pilot_task_core": _ref(root, core_path),
        "evidence": {
            kind: {"path": f"{kind}.json", "sha256": HASH_A}
            for kind in pilot_readiness.GATE_KINDS
        },
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
            "ordinary_revision_id": "ordinary",
            "legacy_revision_id": "legacy",
            "ordinary_initially_standing": False,
            "ordinary_initially_working_set": False,
        },
        "budget": {
            "max_sessions": 26,
            "per_session_max_cost": 2.0,
            "max_total_cost": 52.0,
            "currency": "USD",
            "pricing_basis": "operator supplied",
            "pricing_date": datetime.now(timezone.utc).isoformat(),
            "concurrency": 1,
            "per_session_limit_enforced": True,
        },
    }


def test_development_manifest_and_report_are_honest_about_missing_live_gates(tmp_path):
    manifest_path, _ = _manifest(tmp_path)

    report = qualification.build_report(manifest_path)

    assert report["tooling_ready"] is True
    assert report["packet"]["review_status"] == "pending"
    assert report["qualification"]["measured"] is False
    assert report["performance"] == "not_run"
    assert report["isolation"] == "not_run"
    assert report["pricing"] == "absent"
    assert report["live_readiness"] is False
    assert report["live_sessions_run"] == 0


def test_snapshot_import_is_bounded_inert_and_rejects_escape_symlink_and_overwrite(tmp_path):
    source = tmp_path / "source"
    _write(source / "safe.txt", "safe data")
    destination = tmp_path / "snapshot"

    path = qualification.materialize_snapshot(
        source, ["safe.txt"], destination,
        grant_id="grant", consent_record="local synthetic evaluation",
    )

    snapshot = qualification.load_json(path)
    assert snapshot["execution"] == {"commands_executed": False, "network_used": False}
    assert (destination / "files" / "safe.txt").read_text() == "safe data"
    with pytest.raises(qualification.QualificationError, match="already exists"):
        qualification.materialize_snapshot(
            source, ["safe.txt"], destination,
            grant_id="grant", consent_record="local synthetic evaluation",
        )
    with pytest.raises(qualification.QualificationError, match="contained"):
        qualification.materialize_snapshot(
            source, ["../outside"], tmp_path / "escape",
            grant_id="grant", consent_record="local synthetic evaluation",
        )
    (source / "link").symlink_to(source / "safe.txt")
    with pytest.raises(qualification.QualificationError, match="symlink"):
        qualification.materialize_snapshot(
            source, ["link"], tmp_path / "linked",
            grant_id="grant", consent_record="local synthetic evaluation",
        )
    with pytest.raises(qualification.QualificationError, match="per-file"):
        qualification.materialize_snapshot(
            source, ["safe.txt"], tmp_path / "oversized",
            grant_id="grant", consent_record="local synthetic evaluation",
            max_file_bytes=1,
        )


@pytest.mark.parametrize("writer", [
    lambda path: qualification.atomic_create_text(path, "challenger\n"),
    lambda path: qualification.atomic_create_json(path, {"writer": "challenger"}),
])
def test_immutable_artifact_publication_cannot_replace_concurrent_winner(
    tmp_path, monkeypatch, writer,
):
    destination = tmp_path / "artifact"
    original_link = qualification.os.link

    def publish_competing_winner(source, target):
        Path(target).write_text("winner\n", encoding="utf-8")
        return original_link(source, target)

    monkeypatch.setattr(qualification.os, "link", publish_competing_winner)

    with pytest.raises(qualification.QualificationError, match="overwrite"):
        writer(destination)

    assert destination.read_text(encoding="utf-8") == "winner\n"


def test_snapshot_source_replacement_cannot_import_symlink_target(tmp_path, monkeypatch):
    source = tmp_path / "source"
    safe = _write(source / "safe.txt", "approved")
    outside = _write(tmp_path / "outside.txt", "unauthorized")
    destination = tmp_path / "snapshot"
    original_path_open = Path.open
    original_os_open = qualification.os.open
    replaced = False

    def replace_source():
        nonlocal replaced
        if not replaced:
            replaced = True
            safe.unlink()
            safe.symlink_to(outside)

    def racing_path_open(path, *args, **kwargs):
        if path == safe and args and args[0] == "rb":
            replace_source()
        return original_path_open(path, *args, **kwargs)

    def racing_os_open(path, flags, *args, **kwargs):
        if path == "safe.txt" and kwargs.get("dir_fd") is not None:
            replace_source()
        return original_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_path_open)
    monkeypatch.setattr(qualification.os, "open", racing_os_open)

    with pytest.raises(qualification.QualificationError, match="non-symlink"):
        qualification.materialize_snapshot(
            source, ["safe.txt"], destination,
            grant_id="grant", consent_record="local synthetic evaluation",
        )

    assert not destination.exists()


def test_snapshot_source_growth_cannot_bypass_byte_limit(tmp_path, monkeypatch):
    source = tmp_path / "source"
    selected = _write(source / "selected.txt", "1234")
    destination = tmp_path / "snapshot"
    original_path_open = Path.open
    original_os_open = qualification.os.open
    grown = False

    def grow_source():
        nonlocal grown
        if not grown:
            grown = True
            with original_path_open(selected, "ab") as target:
                target.write(b"5678")

    def racing_path_open(path, *args, **kwargs):
        if path == selected and args and args[0] == "rb":
            grow_source()
        return original_path_open(path, *args, **kwargs)

    def racing_os_open(path, flags, *args, **kwargs):
        if path == "selected.txt" and kwargs.get("dir_fd") is not None:
            grow_source()
        return original_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(Path, "open", racing_path_open)
    monkeypatch.setattr(qualification.os, "open", racing_os_open)

    with pytest.raises(qualification.QualificationError, match="per-file"):
        qualification.materialize_snapshot(
            source, ["selected.txt"], destination,
            grant_id="grant", consent_record="local synthetic evaluation",
            max_file_bytes=4,
        )

    assert not destination.exists()


def test_failed_snapshot_import_never_deletes_concurrent_winner(tmp_path, monkeypatch):
    source = tmp_path / "source"
    _write(source / "safe.txt", "approved")
    destination = tmp_path / "snapshot"

    def concurrent_publish(_path, _value):
        destination.mkdir()
        _write(destination / "winner.txt", "published elsewhere")
        raise qualification.QualificationError("concurrent publication")

    monkeypatch.setattr(qualification, "atomic_create_json", concurrent_publish)

    with pytest.raises(qualification.QualificationError, match="concurrent publication"):
        qualification.materialize_snapshot(
            source, ["safe.txt"], destination,
            grant_id="grant", consent_record="local synthetic evaluation",
        )

    assert (destination / "winner.txt").read_text() == "published elsewhere"


def test_run_attempt_cannot_transition_between_terminal_states(tmp_path):
    history = tmp_path / "runs.jsonl"
    dataset_id = "dataset-terminal"
    attempt_id = "attempt-1"
    qualification.append_run_history(history, dataset_id, "started", attempt_id)
    qualification.append_run_history(
        history, dataset_id, "failed_or_interrupted", attempt_id,
    )

    with pytest.raises(qualification.QualificationError, match="durable start"):
        qualification.append_run_history(
            history, dataset_id, "completed", attempt_id, output_sha256=HASH_A,
        )

    rows = qualification._run_history_rows(history, dataset_id)
    forged = {
        "dataset_id": dataset_id,
        "event": "completed",
        "attempt_id": attempt_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "previous_hash": rows[-1]["record_hash"],
        "details": {"output_sha256": HASH_A},
    }
    forged["record_hash"] = qualification.digest(forged)
    with history.open("a", encoding="utf-8") as target:
        target.write(qualification.canonical(forged) + "\n")

    with pytest.raises(qualification.QualificationError, match="transition"):
        qualification._run_history_rows(history, dataset_id)


def test_pilot_core_is_complete_and_family_overlap_or_core_edit_is_rejected(tmp_path):
    manifest_path, manifest = _manifest(tmp_path, origin="qualification")
    qualification.validate_manifest(manifest_path)

    changed = copy.deepcopy(manifest)
    changed["cases"][0]["family_id"] = "pilot-family-0"
    _write(manifest_path, changed)
    with pytest.raises(qualification.QualificationError, match="overlap"):
        qualification.validate_manifest(manifest_path)

    _write(manifest_path, manifest)
    core_path = tmp_path / manifest["pilot_core_ref"]["path"]
    core = qualification.load_json(core_path)
    core["tasks"].pop()
    _write(core_path, core)
    manifest["pilot_core_ref"]["sha256"] = qualification.sha256(core_path)
    _write(manifest_path, manifest)
    with pytest.raises(qualification.QualificationError, match="exactly six"):
        qualification.validate_manifest(manifest_path)


def test_blinded_packet_has_no_predictions_and_collector_cannot_mint_review(tmp_path):
    manifest_path, _ = _manifest(tmp_path)
    packet = qualification.build_review_packet(manifest_path)

    assert packet["candidate_predictions_included"] is False
    assert packet["implementation_agent_judgments_included"] is False
    assert "candidate_revisions" not in qualification.canonical(packet)
    bad = _write(tmp_path / "bad-review.json", {
        "schema_version": 1,
        "artifact_kind": "operator_imported_review",
        "dataset_id": "dataset-development",
        "review_packet_sha256": qualification.digest(packet),
        "operator": {
            "operator_id": "collector",
            "external_review_import_confirmed": True,
            "collector_generated": True,
        },
        "records": [],
    })
    reasons = qualification.validate_review_attestation(
        bad, manifest_path, qualification.digest(packet),
    )
    assert "operator_review_boundary_missing" in reasons
    assert "review_assignment_mismatch" in reasons


def test_development_collector_uses_real_routes_and_excludes_initial_credit(tmp_path):
    manifest_path, _ = _manifest(tmp_path)
    observations_path = tmp_path / "observations.json"

    observations = qualification.collect(manifest_path, observations_path)

    by_case = {row["case_id"]: row for row in observations["cases"]}
    first = by_case["positive-000"]
    assert first["arms"]["baseline"]["emitted_revisions"] == []
    assert first["arms"]["candidate"]["task_route_selected"] is True
    assert first["arms"]["candidate"]["emitted_revision_ids"] == ["revision-0"]
    credited = by_case["positive-001"]
    assert credited["arms"]["candidate"]["initial_working_set_records"]
    assert credited["arms"]["candidate"]["emitted_revisions"] == []
    negative = by_case["negative-002"]
    assert negative["arms"]["candidate"]["emitted_revisions"] == []
    assert observations["host_receipt_observed"] is False
    assert observations["model_invoked"] is False


@pytest.mark.parametrize("module_name", ["conflicts.py", "revisions.py"])
def test_behavior_dependency_change_invalidates_observations(
    tmp_path, monkeypatch, module_name,
):
    manifest_path, _ = _manifest(tmp_path)
    observations_path = tmp_path / "observations.json"
    observations = qualification.collect(manifest_path, observations_path)
    source_key = f"contexer/{module_name}"
    assert source_key in observations["source_chain"]["runtime_sources"]
    target = Path(qualification.__file__).resolve().parents[2] / "contexer" / module_name
    actual_sha256 = qualification.sha256

    def changed_sha256(path):
        if Path(path).resolve() == target:
            return "f" * 64
        return actual_sha256(path)

    monkeypatch.setattr(qualification, "sha256", changed_sha256)

    with pytest.raises(qualification.QualificationError, match="source chain mismatch"):
        qualification.build_projection(manifest_path, observations_path)


@pytest.mark.parametrize("component", ["head", "patch", "untracked"])
def test_candidate_source_must_match_executed_checkout(
    tmp_path, component,
):
    manifest_path, manifest = _manifest(tmp_path)
    candidate = manifest["sources"]["candidate"]
    if component == "head":
        candidate["head"] = "d" * 40
    else:
        ref_name = f"{component}_ref"
        source_path = tmp_path / candidate[ref_name]["path"]
        source_path.write_bytes(source_path.read_bytes() + b"forged source state\n")
        candidate[ref_name] = _ref(tmp_path, source_path)
    candidate["diff_sha256"] = qualification._source_state_digest(
        candidate["head"],
        candidate["patch_ref"]["sha256"],
        candidate["untracked_ref"]["sha256"],
    )
    _write(manifest_path, manifest)

    with pytest.raises(qualification.QualificationError, match="executed retrieval"):
        qualification.validate_manifest(manifest_path)


def test_source_witness_detects_source_replaced_with_identical_bytes(tmp_path):
    source = _write(tmp_path / "source.py", "stable source\n")
    replacement = _write(tmp_path / "replacement.py", "stable source\n")
    expected_sha256 = qualification.sha256(source)
    witness = qualification._source_witness_for_paths(
        tmp_path, ["source.py"], ["."],
    )

    replacement.replace(source)

    assert qualification.sha256(source) == expected_sha256
    assert qualification._source_witness_for_paths(
        tmp_path, ["source.py"], ["."],
    ) != witness


def test_collection_aborts_when_checkout_changes_between_case_checks(
    tmp_path, monkeypatch,
):
    manifest_path, _ = _manifest(tmp_path)
    output_path = tmp_path / "raced-observations.json"
    collected = False
    witness = {"files": [], "directories": [], "entries": []}

    def collect_then_change(root, case, destination):
        nonlocal collected
        row = qualification._collect_case(root, case, destination)
        collected = True
        return row

    def require_unchanged(_expected):
        if collected:
            raise qualification.QualificationError(
                "executed checkout changed during collection"
            )

    monkeypatch.setattr(qualification, "_checkout_source_witness", lambda: witness)
    monkeypatch.setattr(qualification, "_require_checkout_witness", require_unchanged)

    with pytest.raises(
        qualification.QualificationError,
        match="executed checkout changed during collection",
    ):
        qualification.collect(
            manifest_path,
            output_path,
            case_collector=collect_then_change,
        )

    history = qualification._run_history_rows(
        tmp_path / "runs.jsonl", "dataset-development",
    )
    assert [row["event"] for row in history] == ["started", "failed_or_interrupted"]
    assert not output_path.exists()


def test_real_legacy_control_emission_is_validated_outside_task_metrics(tmp_path):
    manifest_path, manifest = _manifest(tmp_path)
    control = next(case for case in manifest["cases"] if case["category"] == "control")
    prompt_path = tmp_path / control["prompt_ref"]["path"]
    prompt_path.write_text(
        "Why do payment retries use bounded exponential backoff and idempotency keys?\n",
        encoding="utf-8",
    )
    control["prompt_ref"] = _ref(tmp_path, prompt_path)
    control["prompt_sha256"] = qualification.sha256(prompt_path)
    label_path = tmp_path / control["label_ref"]["path"]
    label = qualification.load_json(label_path)
    label["control_assertions"] = [{
        "assertion_id": "legacy-control.emitted",
        "arm": "candidate",
        "field": "emitted_revision_ids",
        "operator": "contains",
        "value": "revision-0",
        "control_kind": "legacy",
    }]
    _write(label_path, label)
    control["label_ref"] = _ref(tmp_path, label_path)
    _write(manifest_path, manifest)
    observations_path = tmp_path / "legacy-control-observations.json"

    observations = qualification.collect(manifest_path, observations_path)
    observed = next(
        row for row in observations["cases"] if row["case_id"] == control["case_id"]
    )
    projection, reasons = qualification.build_projection(
        manifest_path, observations_path,
    )

    assert observed["arms"]["candidate"]["emitted_revision_ids"] == ["revision-0"]
    assert observed["arms"]["candidate"]["task_route_selected"] is False
    assert observed["arms"]["candidate"]["origin"] is None
    assert "qualification_observation_contradiction" not in reasons
    assert "regression_control_observations_missing" not in reasons
    assert control["case_id"] not in projection["unknown_case_ids"]
    assert projection["legacy_regression_ids"] == []
    assert all(
        row["case_id"] != control["case_id"]
        for row in projection["relevance_observations"]
    )


def test_untrusted_case_and_repository_ids_cannot_escape_collection_roots(tmp_path):
    manifest_path, manifest = _manifest(tmp_path)
    escaped = tmp_path.parent / f"escaped-{tmp_path.name}"
    case = manifest["cases"][0]
    case["case_id"] = str(escaped)
    label_path = tmp_path / case["label_ref"]["path"]
    label = qualification.load_json(label_path)
    label["case_id"] = str(escaped)
    _write(label_path, label)
    case["label_ref"] = _ref(tmp_path, label_path)
    _write(manifest_path, manifest)
    destinations = []

    def recording_collector(root, selected_case, destination):
        destinations.append(destination)
        return qualification._collect_case(root, selected_case, destination)

    qualification.collect(
        manifest_path,
        tmp_path / "contained-observations.json",
        case_collector=recording_collector,
    )

    assert destinations[0].name == "case-0000"
    assert all(path.name.startswith("case-") for path in destinations)
    assert not escaped.exists()

    manifest = qualification.load_json(manifest_path)
    manifest["cases"][0]["repo_id"] = str(escaped / "repo")
    _write(manifest_path, manifest)
    with pytest.raises(qualification.QualificationError, match="relative identifier"):
        qualification.validate_manifest(manifest_path)


def test_collection_rechecks_every_source_and_preserves_failed_attempt(tmp_path):
    manifest_path, manifest = _manifest(tmp_path)
    first_prompt = tmp_path / manifest["cases"][0]["prompt_ref"]["path"]
    called = False

    def mutate_after_collection(root, case, destination):
        nonlocal called
        row = qualification._collect_case(root, case, destination)
        if not called:
            called = True
            first_prompt.write_text("changed during measurement\n", encoding="utf-8")
        return row

    with pytest.raises(qualification.QualificationError, match="identity mismatch"):
        qualification.collect(
            manifest_path,
            tmp_path / "mixed-observations.json",
            case_collector=mutate_after_collection,
        )

    history = qualification._run_history_rows(
        tmp_path / "runs.jsonl", "dataset-development",
    )
    assert [row["event"] for row in history] == ["started", "failed_or_interrupted"]
    assert not (tmp_path / "mixed-observations.json").exists()


def test_reserved_packet_requires_review_and_explicit_opening(tmp_path):
    manifest_path, _ = _manifest(tmp_path, origin="qualification")
    history = tmp_path / "exposure.jsonl"

    with pytest.raises(qualification.QualificationError, match="explicit opening"):
        qualification.collect(manifest_path, tmp_path / "observations.json")
    assert qualification.exposure_state(history, "dataset-qualification") == "draft"

    _attach_review(manifest_path)
    assert qualification.exposure_state(
        history, "dataset-qualification"
    ) == "frozen_unopened"


def test_opening_intent_is_consumed_even_when_collection_fails(tmp_path):
    manifest_path, _ = _manifest(tmp_path, origin="qualification")
    _attach_review(manifest_path)

    def interrupt(*_args):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        qualification.collect(
            manifest_path,
            tmp_path / "observations.json",
            open_qualification=True,
            case_collector=interrupt,
        )
    assert qualification.exposure_state(
        tmp_path / "exposure.jsonl", "dataset-qualification"
    ) == "consumed"
    with pytest.raises(qualification.QualificationError, match="unopened"):
        qualification.collect(
            manifest_path,
            tmp_path / "retry.json",
            open_qualification=True,
            case_collector=interrupt,
        )


def test_opened_packet_cannot_be_resealed_and_illegal_history_is_rejected(tmp_path):
    history = tmp_path / "exposure.jsonl"
    dataset_id = "dataset-state-machine"
    for event in ("draft", "frozen_unopened", "opening_intent", "consumed"):
        qualification.append_exposure(history, dataset_id, event)

    with pytest.raises(qualification.QualificationError, match="never be resealed"):
        qualification.append_exposure(history, dataset_id, "draft")

    rows = qualification._history_rows(history, dataset_id)
    forged = {
        "dataset_id": dataset_id,
        "event": "draft",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "previous_hash": rows[-1]["record_hash"],
        "details": {},
    }
    forged["record_hash"] = qualification.digest(forged)
    history.write_text(
        history.read_text(encoding="utf-8") + qualification.canonical(forged) + "\n",
        encoding="utf-8",
    )
    with pytest.raises(qualification.QualificationError, match="illegal exposure transition"):
        qualification.exposure_state(history, dataset_id)


def test_rejected_human_reviews_require_explicit_accepting_adjudication(tmp_path):
    manifest_path, _ = _manifest(tmp_path, origin="qualification")
    review_path = _attach_review(manifest_path)
    review = qualification.load_json(review_path)
    for record in review["records"]:
        record["reviewers"][0]["verdict"] = "reject"
    _write(review_path, review)
    manifest = qualification.load_json(manifest_path)
    manifest["review_attestation_ref"] = _ref(tmp_path, review_path)
    _write(manifest_path, manifest)

    reasons = qualification.validate_review_attestation(
        review_path,
        manifest_path,
        qualification.digest(qualification.build_review_packet(manifest_path)),
    )
    report = qualification.build_report(manifest_path)

    assert any(reason.endswith(":adjudication_missing") for reason in reasons)
    assert report["packet"]["review_status"] == "pending"
    assert report["packet"]["prepared"] is False

def test_report_only_is_read_only_and_does_not_open_reserved_packet(tmp_path):
    manifest_path, _ = _manifest(tmp_path, origin="qualification")
    _attach_review(manifest_path)
    history = tmp_path / "exposure.jsonl"
    before = history.read_bytes()

    report = qualification.build_report(manifest_path)

    assert report["packet"]["prepared"] is True
    assert report["qualification"]["accepted"] is False
    assert history.read_bytes() == before


def test_legacy_or_unversioned_artifact_cannot_satisfy_qualified_validator(tmp_path):
    legacy = _write(tmp_path / "legacy.json", {
        "schema_version": 1,
        "artifact_kind": "qualification",
        "provenance": {"mode": "measured", "synthetic": False, "stub": False},
        "measurements": {},
    })

    result = qualification.validate_qualified_artifact(legacy)

    assert result["status"] == "inconclusive"
    assert result["reasons"] == ["qualified_schema_invalid"]


def test_qualified_artifact_recomputes_references_and_rejects_later_mutation(tmp_path):
    manifest_path, _ = _manifest(tmp_path, origin="qualification")
    _attach_review(manifest_path)
    observations = _synthetic_observations(manifest_path)
    artifact_path = tmp_path / "qualified-evidence.json"

    qualification.build_qualified_artifact(manifest_path, observations, artifact_path)
    result = qualification.validate_qualified_artifact(artifact_path)

    assert result["status"] == "pass"
    assert result["measurements"]["unknown_case_ids"] == []
    assert len(result["measurements"]["relevance_observations"]) == 104
    assert len(result["measurements"]["regression_control_case_ids"]) == 12

    manifest = qualification.load_json(manifest_path)
    rationale = tmp_path / manifest["cases"][0]["rationale_ref"]["path"]
    rationale.write_text("mutated after review\n", encoding="utf-8")
    changed = qualification.validate_qualified_artifact(artifact_path)
    assert changed["status"] == "inconclusive"
    assert "qualified_reference_invalid" in changed["reasons"]


def test_qualified_validator_does_not_reread_validated_exposure_history(
    tmp_path, monkeypatch,
):
    manifest_path, _ = _manifest(tmp_path, origin="qualification")
    _attach_review(manifest_path)
    observations = _synthetic_observations(manifest_path)
    artifact_path = tmp_path / "qualified-evidence.json"
    qualification.build_qualified_artifact(manifest_path, observations, artifact_path)

    def unexpected_second_read(*_args, **_kwargs):
        raise AssertionError("exposure history was read twice")

    monkeypatch.setattr(qualification, "exposure_state", unexpected_second_read)

    assert qualification.validate_qualified_artifact(artifact_path)["status"] == "pass"


def test_qualified_artifact_rejects_failed_then_completed_attempt(tmp_path):
    manifest_path, _ = _manifest(tmp_path, origin="qualification")
    _attach_review(manifest_path)
    observations = _synthetic_observations(manifest_path)
    artifact_path = tmp_path / "qualified-evidence.json"
    qualification.build_qualified_artifact(manifest_path, observations, artifact_path)
    manifest = qualification.load_json(manifest_path)
    history = tmp_path / manifest["run_history_path"]
    rows = qualification._run_history_rows(history, manifest["dataset_id"])
    failed = {
        "dataset_id": manifest["dataset_id"],
        "event": "failed_or_interrupted",
        "attempt_id": rows[-1]["attempt_id"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "previous_hash": rows[0]["record_hash"],
        "details": {"error": "forged earlier terminal"},
    }
    failed["record_hash"] = qualification.digest(failed)
    completed = {
        "dataset_id": manifest["dataset_id"],
        "event": "completed",
        "attempt_id": rows[-1]["attempt_id"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "previous_hash": failed["record_hash"],
        "details": {"output_sha256": qualification.sha256(observations)},
    }
    completed["record_hash"] = qualification.digest(completed)
    history.write_text(
        "\n".join(qualification.canonical(row) for row in [
            rows[0], failed, completed,
        ]) + "\n",
        encoding="utf-8",
    )
    artifact = qualification.load_json(artifact_path)
    artifact["references"]["run_history"]["sha256"] = qualification.sha256(history)
    _write(artifact_path, artifact)

    result = qualification.validate_qualified_artifact(artifact_path)

    assert result["status"] == "inconclusive"
    assert "qualified_reference_invalid" in result["reasons"]
    assert "run history transition is invalid" in result["reasons"]


def test_qualified_artifact_cannot_be_relabelled_for_another_source(tmp_path):
    manifest_path, _ = _manifest(tmp_path, origin="qualification")
    _attach_review(manifest_path)
    observations = _synthetic_observations(manifest_path)
    artifact_path = tmp_path / "qualified-evidence.json"
    qualification.build_qualified_artifact(manifest_path, observations, artifact_path)
    artifact = qualification.load_json(artifact_path)
    campaign = _campaign(manifest_path)
    relabelled = {"head": "d" * 40, "diff_sha256": "e" * 64}
    artifact["provenance"]["integration_source"] = relabelled
    artifact["provenance"]["candidate_source_sha256"] = "e" * 64
    campaign["integration_source"] = relabelled
    campaign["candidate_source_sha256"] = "e" * 64
    _write(artifact_path, artifact)

    result = qualification.validate_qualified_artifact(
        artifact_path, campaign_manifest=campaign,
    )

    assert result["status"] == "inconclusive"
    assert "qualified_provenance_invalid" in result["reasons"]


def test_contradictory_emission_summaries_remain_unknown(tmp_path):
    manifest_path, _ = _manifest(tmp_path, origin="qualification")
    _attach_review(manifest_path)
    observations_path = _synthetic_observations(manifest_path)
    observations = qualification.load_json(observations_path)
    positive = next(row for row in observations["cases"] if row["category"] == "positive")
    candidate = positive["arms"]["candidate"]
    retained_ids = list(candidate["emitted_revision_ids"])
    candidate.update({
        "supported": False,
        "origin": "legacy",
        "task_route_selected": False,
        "router_context_in_payload": False,
    })
    _write(observations_path, observations)
    run_history = tmp_path / "runs.jsonl"
    run_history.unlink()
    attempt_id = "dataset-qualification:attempt:1"
    qualification.append_run_history(
        run_history, "dataset-qualification", "started", attempt_id,
    )
    qualification.append_run_history(
        run_history,
        "dataset-qualification",
        "completed",
        attempt_id,
        output_sha256=qualification.sha256(observations_path),
    )

    projection, reasons = qualification.build_projection(
        manifest_path, observations_path,
    )
    projected = next(
        row for row in projection["relevance_observations"]
        if row["case_id"] == positive["case_id"]
    )

    assert retained_ids
    assert positive["case_id"] in projection["unknown_case_ids"]
    assert projected["candidate_revisions"] == []
    assert "qualification_observation_contradiction" in reasons
    artifact_path = tmp_path / "contradictory-qualified.json"
    qualification.build_qualified_artifact(
        manifest_path, observations_path, artifact_path,
    )
    assert qualification.validate_qualified_artifact(artifact_path)["status"] \
        == "inconclusive"


def test_contract07_schema_two_consumes_the_shared_qualified_validator(tmp_path):
    manifest_path, _ = _manifest(tmp_path, origin="qualification")
    _attach_review(manifest_path)
    observations = _synthetic_observations(manifest_path)
    artifact_path = tmp_path / "qualified-evidence.json"
    artifact = qualification.build_qualified_artifact(
        manifest_path, observations, artifact_path,
    )
    campaign = _campaign(manifest_path)
    document = {
        "schema_version": 1,
        "evidence_kind": "qualification",
        "integration_source": campaign["integration_source"],
        "candidate_source_sha256": campaign["candidate_source_sha256"],
        "measured_at": "2026-09-20T12:00:00+00:00",
        "checks": [{
            "check_id": "qualification.reviewed_measurements_v2",
            "evidence_class": "measured_live_prerequisite",
            "protocol": "qualification_review_v2",
            "artifact_sha256": qualification.sha256(artifact_path),
            "artifact_path": artifact_path.name,
            "validator_sha256": artifact["provenance"]["producer_sha256"],
        }],
    }

    status, reasons, metrics = pilot_readiness._qualification_status(
        document, campaign, tmp_path,
    )

    assert status == "pass"
    assert reasons == []
    assert metrics["fresh_qualification"] is True
    assert metrics["full_precision_pooled"] == 1.0
    assert metrics["paired_coverage_gain_equal_task"] == 1.0


@pytest.mark.parametrize(("field", "value"), [
    ("snapshot_id", "changed"),
    ("validator_sha256", "f" * 64),
    ("required_checks", ["forged-check"]),
])
def test_campaign_schema_two_requires_complete_core_identity(tmp_path, field, value):
    manifest_path, _ = _manifest(tmp_path)
    campaign = _campaign(manifest_path)
    campaign_path = _write(tmp_path / "campaign.json", campaign)
    for kind in pilot_readiness.GATE_KINDS:
        _write(tmp_path / f"{kind}.json", {})
        campaign["evidence"][kind]["sha256"] = qualification.sha256(
            tmp_path / f"{kind}.json"
        )
    _write(campaign_path, campaign)

    loaded = pilot_readiness.load_manifest(campaign_path)
    assert loaded["schema_version"] == 2

    campaign["tasks"][0][field] = value
    _write(campaign_path, campaign)
    with pytest.raises(pilot_readiness.PilotError, match="differ"):
        pilot_readiness.load_manifest(campaign_path)


@pytest.mark.parametrize(("field", "value"), [
    ("validator_sha256", "f" * 64),
    ("required_checks", ["forged-check"]),
])
def test_qualified_artifact_rejects_campaign_outcome_substitution(
    tmp_path, field, value,
):
    manifest_path, _ = _manifest(tmp_path, origin="qualification")
    _attach_review(manifest_path)
    observations = _synthetic_observations(manifest_path)
    artifact_path = tmp_path / "qualified-evidence.json"
    qualification.build_qualified_artifact(manifest_path, observations, artifact_path)
    campaign = _campaign(manifest_path)
    campaign["tasks"][0][field] = value

    result = qualification.validate_qualified_artifact(
        artifact_path, campaign_manifest=campaign,
    )

    assert result["status"] == "inconclusive"
    assert "campaign_pilot_task_core_mismatch" in result["reasons"]
