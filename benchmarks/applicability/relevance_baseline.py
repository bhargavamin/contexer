#!/usr/bin/env python3
"""Deterministic decision-relevance baseline from contract 01.

This is deliberately benchmark-only.  It drives the existing store, retrieval owners, and
adapter formatters against synthetic stores, then projects only observable evidence into a
versioned report.  It does not add a production event stream or parse rendered prose for IDs
that the fixture did not already control.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from contexer import retrieval, revisions, store  # noqa: E402
from contexer.adapters import claude, cursor, gemini  # noqa: E402

SCHEMA_VERSION = 1
RUNNER_VERSION = "2"
FIXTURE_PATH = Path(__file__).with_name("relevance_cases.json")
STAGES = {"candidate", "selected", "emitted", "looked_up"}
TIERS = {"standing_title", "standing_full", "prompt_full", "pointer", "tool_result", "team_delta"}
STATUSES = {"observed", "absent", "unknown", "unsupported"}
OUTCOME_RESULTS = {
    "verified_compliant", "violation_observed", "clarification_appropriate", "unknown"
}
FULL_TIERS = {"standing_full", "prompt_full", "tool_result", "team_delta"}
DELIVERY_STAGES = {"emitted", "looked_up"}


class FixtureError(ValueError):
    """A fixture cannot be executed without inventing evidence."""


def _git(args: list[str]) -> str:
    try:
        return subprocess.run(
            ["git", "-C", str(ROOT), *args], capture_output=True, text=True, timeout=3,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _fixture_sha256(fixture: dict[str, Any]) -> str:
    """Hash the fixture data that was actually executed, not a default path."""
    encoded = json.dumps(
        fixture, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_fixture(path: Path = FIXTURE_PATH) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureError(f"invalid fixture: {exc}") from exc
    validate_fixture(data)
    return data


def validate_fixture(data: dict[str, Any]) -> None:
    if not isinstance(data, dict) or data.get("schema_version") != SCHEMA_VERSION:
        raise FixtureError("fixture schema_version must be 1")
    cases = data.get("cases")
    if not isinstance(cases, list) or not cases:
        raise FixtureError("fixture cases must be a non-empty list")
    ids = [case.get("case_id") for case in cases if isinstance(case, dict)]
    if (len(ids) != len(cases)
            or any(not isinstance(case_id, str) or not case_id.strip() for case_id in ids)
            or len(ids) != len(set(ids))):
        raise FixtureError("case_id values must be unique non-empty strings")
    required_families = {f"R{i:02d}" for i in range(1, 19)}
    if {case.get("family") for case in cases} != required_families:
        raise FixtureError("fixture must contain exactly scenario families R01-R18")
    decision_ids: set[str] = set()
    revision_ids: set[str] = set()
    for case in cases:
        for field in ("description", "repo_id", "checkout_id", "host", "surface", "session_id"):
            if not isinstance(case.get(field), str) or not case[field]:
                raise FixtureError(f"{case.get('case_id')}: missing {field}")
        if os.path.isabs(case["repo_id"]) or os.path.isabs(case["checkout_id"]):
            raise FixtureError(f"{case['case_id']}: synthetic paths must be relative")
        if not isinstance(case.get("actions"), list) or not case["actions"]:
            raise FixtureError(f"{case['case_id']}: actions must be non-empty")
        if not isinstance(case.get("applicable"), list):
            raise FixtureError(f"{case['case_id']}: applicable must be a list")
        if not isinstance(case.get("desired_assertions"), list):
            raise FixtureError(f"{case['case_id']}: desired_assertions must be a list")
        for item in case.get("decisions", []) + case.get("global_decisions", []):
            for field in ("decision_id", "revision_id", "content", "title", "status", "subtype", "timestamp"):
                if not isinstance(item.get(field), str) or not item[field]:
                    raise FixtureError(f"{case['case_id']}: decision missing {field}")
            pair = (case["case_id"], item["decision_id"])
            if str(pair) in decision_ids:
                raise FixtureError(f"{case['case_id']}: duplicate decision_id")
            decision_ids.add(str(pair))
            revision_pair = (case["case_id"], item["revision_id"])
            if str(revision_pair) in revision_ids:
                raise FixtureError(f"{case['case_id']}: duplicate revision_id")
            revision_ids.add(str(revision_pair))
            if item["status"] not in {"approved", "suggested", "pending_approval", "ignored"}:
                raise FixtureError(f"{case['case_id']}: invalid decision status")
        for label in case["applicable"]:
            if label.get("required_tier") not in TIERS:
                raise FixtureError(f"{case['case_id']}: invalid required tier")
        labels = {
            (label.get("decision_id"), label.get("revision_id")) for label in case["applicable"]
        }
        action_ids = {action.get("action_id") for action in case["actions"]}
        for expected in case.get("delivery_expectations", []):
            if expected.get("action_id") not in action_ids:
                raise FixtureError(f"{case['case_id']}: delivery expectation has unknown action")
            if (expected.get("decision_id"), expected.get("revision_id")) not in labels:
                raise FixtureError(f"{case['case_id']}: delivery expectation is not applicable")
        for assertion in case["desired_assertions"]:
            if not assertion.get("key") or not assertion.get("kind"):
                raise FixtureError(f"{case['case_id']}: assertion requires key and kind")
            if assertion.get("known_gap") and not assertion.get("owner"):
                raise FixtureError(f"{case['case_id']}: known gaps require an owner")


def _entry(item: dict[str, Any]) -> dict[str, Any]:
    revision = {
        "revision_id": item["revision_id"],
        "decision_id": item["decision_id"],
        "version_number": item.get("version_number", 1),
        "content": item["content"],
        "title": item["title"],
        "confidence_score": item.get("confidence", 90),
        "evidence": list(item.get("evidence", [])),
        "created_at": item["timestamp"],
        "approved_at": item["timestamp"] if item["status"] == "approved" else None,
        "source": item.get("source", "human"),
    }
    entry = {
        "id": item["decision_id"],
        "type": "decision",
        "subtype": item["subtype"],
        "status": item["status"],
        "title": item["title"],
        "content": item["content"],
        "timestamp": item["timestamp"],
        "updated_at": item["timestamp"],
        "occurrence_count": item.get("occurrence_count", 1),
        "created_by": item.get("created_by", item.get("source", "human")),
        "current_revision_id": item["revision_id"],
        "revision": item.get("version_number", 1),
        "confidence": item.get("confidence", 90),
        "revisions": [revision],
    }
    for field in ("source_files", "bootstrap_withheld", "withheld_reason"):
        if field in item:
            entry[field] = item[field]
    if item.get("proposal"):
        entry["proposed_revision"] = dict(item["proposal"])
    return entry


def _marker_map(case: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {item["decision_id"]: item for item in case.get("decisions", []) + case.get("global_decisions", [])}


def _action_identity(case: dict[str, Any], action: dict[str, Any]) -> dict[str, str | None]:
    action_id = action["action_id"]
    request_root = action.get("request_id") or case.get("request_id")
    return {
        "action_id": action_id,
        "session_id": action.get("session_id", case["session_id"]),
        "request_id": f"{request_root}:{action_id}" if request_root else None,
        "host": action.get("host", case["host"]),
        "surface": action.get("surface", case["surface"]),
    }


def _runtime_entry(repo: str, item: dict[str, Any]) -> dict[str, Any] | None:
    data = store.load_global() if item.get("scope") == "global" else store.load(repo)
    return next(
        (entry for entry in data.get("entries", []) if entry.get("id") == item["decision_id"]),
        None,
    )


def _runtime_marker_identity(
    repo: str, item: dict[str, Any], marker: str, *, proposal_marker: bool,
) -> tuple[str | None, str | None] | None:
    """Resolve a rendered marker against the state that was actually rendered."""
    entry = _runtime_entry(repo, item)
    if entry is None:
        return None
    current = revisions.current_revision(entry) or {}
    current_text = "\n".join((current.get("title", ""), current.get("content", "")))
    proposal = entry.get("proposed_revision") or {}
    proposal_text = "\n".join((proposal.get("title", ""), proposal.get("content", "")))
    revision_id = current.get("revision_id")
    if proposal_marker and marker in proposal_text:
        proposal_id = proposal.get("proposal_id") or proposal.get("revision_id")
        return revision_id, proposal_id
    if marker in current_text:
        return revision_id, None
    return None


def _json_context(raw: str) -> str:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return ""
    hook = data.get("hookSpecificOutput", {}) if isinstance(data, dict) else {}
    return hook.get("additionalContext") or data.get("additional_context") or ""


def _observations_from_text(
    case: dict[str, Any], action: dict[str, Any], repo: str, text: str, *, stage: str, tier: str,
    status: str = "observed", reason: str = "", evidence_source: str,
) -> list[dict[str, Any]]:
    observations = []
    found = False
    for decision_id, item in _marker_map(case).items():
        marker = item.get("marker") or item["content"]
        item_tier = action.get("tier_by_decision", {}).get(decision_id, tier)
        proposal = item.get("proposal") or {}
        proposal_marker = proposal.get("marker") or proposal.get("content")
        identity = (
            _runtime_marker_identity(repo, item, marker, proposal_marker=False)
            if marker and marker in text else None
        )
        if identity:
            found = True
            observations.append({
                **_action_identity(case, action), "stage": stage,
                "decision_id": decision_id, "revision_id": identity[0],
                "proposal_id": identity[1], "tier": item_tier, "status": status,
                "reason": reason or None, "evidence_source": evidence_source,
                "source_scope": item.get("scope", "repo"),
            })
        proposal_identity = (
            _runtime_marker_identity(repo, item, proposal_marker, proposal_marker=True)
            if proposal_marker and proposal_marker in text else None
        )
        if proposal_identity:
            found = True
            observations.append({
                **_action_identity(case, action), "stage": stage,
                "decision_id": decision_id, "revision_id": proposal_identity[0],
                "proposal_id": proposal_identity[1], "tier": item_tier,
                "status": status, "reason": reason or None,
                "evidence_source": evidence_source, "source_scope": item.get("scope", "repo"),
            })
    if not found:
        observations.append({
            **_action_identity(case, action), "stage": stage, "decision_id": None,
            "revision_id": None, "proposal_id": None, "tier": tier, "status": status,
            "reason": reason or ("gate_closed" if status == "absent" else None),
            "evidence_source": evidence_source, "source_scope": "repo",
        })
    return observations


def classify_outcome(evidence: dict[str, Any]) -> dict[str, Any]:
    """Classify only independently attributable evidence; never infer task success."""
    scope = evidence.get("scope") or "unspecified"
    base = {
        "evidence_id": evidence.get("evidence_id"), "scope": scope,
        "artifact_id": evidence.get("artifact_id"),
        "decision_id": evidence.get("decision_id"),
        "validator": evidence.get("validator"),
        "validator_version": evidence.get("validator_version"),
        "completeness": evidence.get("completeness", "unknown"),
        "evidence_provenance": evidence.get("provenance", "synthetic"),
        "checked_policies": list(evidence.get("checked_policies") or []),
        "checked_criteria": list(evidence.get("checked_criteria") or []),
        "raw_result": evidence.get("raw_result"),
    }
    independent = evidence.get("provenance") == "independent_check"
    validator_identified = bool(evidence.get("validator") and evidence.get("validator_version"))
    scoped = bool(evidence.get("scope") and evidence.get("scope") != "unspecified")
    attributable = bool(evidence.get("artifact_id") and evidence.get("decision_id"))
    complete = evidence.get("completeness") == "complete"
    raw = evidence.get("raw_result")
    checked = evidence.get("decision_id") in (evidence.get("checked_policies") or [])
    criterion = evidence.get("criterion")
    criterion_checked = bool(
        criterion and criterion in (evidence.get("checked_criteria") or [])
    )
    verified = independent and validator_identified and scoped
    if verified and attributable and raw == "violation" and checked:
        result, reason = "violation_observed", "attributable checked violation"
    elif verified and attributable and complete and checked and raw == "allow":
        result, reason = "verified_compliant", "complete deterministic scoped check"
    elif (verified and evidence.get("artifact_id") and complete
          and criterion == "clarification_needed" and criterion_checked and raw == "satisfied"):
        result, reason = "clarification_appropriate", "predefined criterion independently assessed"
    else:
        result, reason = "unknown", evidence.get("unknown_reason") or "insufficient attributable evidence"
    return {**base, "result": result, "reason": reason}


def _action_tier(action: dict[str, Any], meta: dict[str, Any] | None = None) -> str:
    if action.get("tier"):
        return action["tier"]
    if meta and meta.get("kind") == "pointer":
        return "pointer"
    return "prompt_full"


def _run_action(
    case: dict[str, Any], action: dict[str, Any], repo: str, state: dict[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    kind = action["kind"]
    action_id = action["action_id"]
    detail: dict[str, Any] = {"action_id": action_id, "kind": kind}
    if kind == "prompt":
        original = store.atomic_write
        if action.get("fail_working_set_write"):
            def fail_ws(path: Path, text: str) -> None:
                if path.name.startswith(".ws_"):
                    raise OSError("synthetic optional bookkeeping failure")
                original(path, text)
            store.atomic_write = fail_ws
        try:
            text, meta = store.get_context_for_prompt_with_meta(
                repo, action["prompt"], action.get("session_id", case["session_id"])
            )
        finally:
            store.atomic_write = original
        detail.update({"text": text, "meta": meta})
        obs_status = "observed" if text else "absent"
        observations = _observations_from_text(
            case, action, repo, text, stage="emitted", tier=_action_tier(action, meta),
            status=obs_status, evidence_source="store.get_context_for_prompt_with_meta",
        )
        return observations, detail
    if kind == "lookup":
        ranked_calls: list[list[tuple[str, float, int, int, int, int]]] = []
        original_rank = retrieval.prompt_rank

        def traced_rank(*args: Any, **kwargs: Any) -> list[tuple[str, float, int, int, int, int]]:
            ranked = original_rank(*args, **kwargs)
            ranked_calls.append(ranked)
            return ranked

        if action.get("trace_prompt_rank"):
            retrieval.prompt_rank = traced_rank
        try:
            text = store.get_context(
                repo, query=action.get("query", ""), entry_type=action.get("entry_type", ""),
                limit=action.get("limit", 0), files=action.get("files"),
            )
        finally:
            retrieval.prompt_rank = original_rank
        detail.update({
            "text": text,
            "prompt_rank_calls": len(ranked_calls),
            "ranked_ids": [row[0] for row in ranked_calls[0]] if ranked_calls else [],
        })
        observations = _observations_from_text(
            case, action, repo, text, stage="looked_up", tier="tool_result",
            status="observed" if "No matching" not in text and "No context" not in text else "absent",
            evidence_source="store.get_context",
        )
        if ranked_calls:
            items = _marker_map(case)
            for decision_id, *_ranking in ranked_calls[0]:
                item = items.get(decision_id)
                if item is None:
                    continue
                entry = _runtime_entry(repo, item) or {}
                current = revisions.current_revision(entry) or {}
                observations.append({
                    **_action_identity(case, action), "stage": "candidate",
                    "decision_id": decision_id, "revision_id": current.get("revision_id"),
                    "proposal_id": None, "tier": "tool_result", "status": "observed",
                    "reason": None, "evidence_source": "retrieval.prompt_rank",
                    "source_scope": item.get("scope", "repo"),
                })
        return observations, detail
    if kind == "rank":
        index = store._read_retrieval_index(repo)
        if index is None:
            obs = [{**_action_identity(case, action), "stage": "candidate", "decision_id": None,
                    "revision_id": None, "proposal_id": None, "tier": "prompt_full",
                    "status": "unknown", "reason": "observation_unavailable",
                    "evidence_source": "store._read_retrieval_index", "source_scope": "repo"}]
            return obs, {**detail, "ranked_ids": []}
        terms = retrieval.index_tokens(action["prompt"])
        ranked = retrieval.prompt_rank(terms, index)
        ids = [row[0] for row in ranked]
        detail["ranked_ids"] = ids
        items = _marker_map(case)
        obs = []
        for did in ids:
            item = items.get(did)
            entry = _runtime_entry(repo, item) if item else None
            current = revisions.current_revision(entry or {}) or {}
            obs.append({
                **_action_identity(case, action), "stage": "candidate", "decision_id": did,
                "revision_id": current.get("revision_id"), "proposal_id": None,
                "tier": "prompt_full", "status": "observed", "reason": None,
                "evidence_source": "retrieval.prompt_rank", "source_scope": "repo",
            })
        return obs, detail
    if kind == "session_start":
        payload = store.session_start_payload(
            repo, action.get("source", ""), action.get("session_id", case["session_id"])
        )
        text = payload.get("context", "")
        detail.update({"text": text, "status_text": payload.get("status", "")})
        return _observations_from_text(
            case, action, repo, text, stage="emitted", tier=action.get("tier", "standing_full"),
            status="observed" if text else "absent", evidence_source="store.session_start_payload",
        ), detail
    if kind == "compact":
        if action.get("host") == "gemini":
            raw = json.dumps({"session_id": action.get("session_id", case["session_id"]), "prompt": "continue"})
            gemini.pre_compress(repo, raw)
            rendered = gemini.before_agent(repo, raw)
            text = _json_context(rendered)
            source = "gemini.pre_compress+before_agent"
        else:
            payload = store.session_start_payload(
                repo, "compact", action.get("session_id", case["session_id"])
            )
            text = payload.get("context", "")
            source = "store.session_start_payload(compact)"
        detail["text"] = text
        return _observations_from_text(
            case, action, repo, text, stage="emitted", tier="standing_full",
            status="observed" if text else "absent", evidence_source=source,
        ), detail
    if kind == "adapter_prompt":
        raw = json.dumps({
            "prompt": action["prompt"],
            "session_id": action.get("session_id", case["session_id"]),
        })
        host = action["host"]
        if host == "claude":
            rendered, source = claude.rationale(repo, raw), "claude.rationale"
        elif host == "codex":
            rendered, source = claude.rationale(repo, raw), "codex hook's claude.rationale entrypoint"
        elif host == "gemini":
            rendered, source = gemini.before_agent(repo, raw), "gemini.before_agent"
        elif host == "cursor":
            detail["rendered"] = cursor.format_prompt_passthrough()
            observation = {
                **_action_identity(case, action), "stage": "emitted", "decision_id": None,
                "revision_id": None, "proposal_id": None, "tier": "prompt_full",
                "status": "unsupported", "reason": "host_unsupported",
                "evidence_source": "cursor.format_prompt_passthrough", "source_scope": "repo",
            }
            return [observation], detail
        else:
            raise FixtureError(f"{case['case_id']}: unknown host {host}")
        text = _json_context(rendered)
        detail.update({"rendered": json.loads(rendered), "text": text})
        return _observations_from_text(
            case, action, repo, text, stage="emitted", tier=_action_tier(action),
            status="observed" if text else "absent", evidence_source=source,
        ), detail
    if kind == "approve_proposal":
        original_uuid4 = revisions.uuid.uuid4
        if action.get("generated_revision_id"):
            def fixed_uuid4() -> str:
                return action["generated_revision_id"]

            revisions.uuid.uuid4 = fixed_uuid4
        try:
            ok, message = store.approve_decision(repo, action["decision_id"], "approve")
        finally:
            revisions.uuid.uuid4 = original_uuid4
        if not ok:
            raise FixtureError(f"{case['case_id']}: proposal approval failed: {message}")
        entry = next(
            item for item in store.load(repo).get("entries", [])
            if item.get("id") == action["decision_id"]
        )
        current = revisions.current_revision(entry) or {}
        detail.update({
            "message": message,
            "current_revision_id": current.get("revision_id"),
            "current_content": current.get("content"),
            "proposal_id": (entry.get("proposed_revision") or {}).get("proposal_id"),
        })
        return [], detail
    if kind == "remove_index":
        path = store._index_path(repo)
        path.unlink(missing_ok=True)
        detail["index_exists"] = path.exists()
        state["index_missing_before_prompt"] = not path.exists()
        return [], detail
    if kind == "corrupt_index":
        path = store._index_path(repo)
        path.write_text("{not-json", encoding="utf-8")
        detail["index_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        state["corrupt_index_sha256"] = detail["index_sha256"]
        return [], detail
    if kind == "restore_index":
        store.save(repo, store.load(repo))
        detail["exists"] = store._index_path(repo).exists()
        return [], detail
    if kind == "check_index":
        path = store._index_path(repo)
        detail["exists"] = path.exists()
        detail["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None
        return [], detail
    if kind == "outcome_evidence":
        outcomes = [classify_outcome(e) for e in action["evidence"]]
        detail["outcomes"] = outcomes
        state.setdefault("outcomes", []).extend(outcomes)
        return [], detail
    if kind == "record_prior_exposure":
        store._ws_add(repo, action.get("session_id", case["session_id"]), action["decision_ids"])
        detail["working_set"] = store.working_set_ids(
            repo, action.get("session_id", case["session_id"])
        )
        return [], detail
    raise FixtureError(f"{case['case_id']}: unknown action kind {kind}")


def _find_action(details: list[dict[str, Any]], action_id: str) -> dict[str, Any]:
    try:
        return next(item for item in details if item["action_id"] == action_id)
    except StopIteration as exc:
        raise FixtureError(f"unknown action_id in assertion: {action_id}") from exc


def _normalize_detail(value: Any, repo_path: str, repo_id: str) -> Any:
    if isinstance(value, str):
        return value.replace(repo_path, repo_id)
    if isinstance(value, list):
        return [_normalize_detail(item, repo_path, repo_id) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_detail(item, repo_path, repo_id) for key, item in value.items()}
    return value


def _evaluate_assertion(
    assertion: dict[str, Any], observations: list[dict[str, Any]], details: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> tuple[bool, str]:
    kind = assertion["kind"]
    detail = _find_action(details, assertion["action_id"]) if assertion.get("action_id") else {}
    text = detail.get("text", "")
    if kind == "contains_decision":
        matching = [o for o in observations if o["action_id"] == assertion["action_id"]
                    and o.get("decision_id") == assertion["decision_id"]
                    and o["status"] == "observed"
                    and o["stage"] in DELIVERY_STAGES]
        if assertion.get("revision_id"):
            matching = [o for o in matching if o.get("revision_id") == assertion["revision_id"]]
        if assertion.get("proposal_id"):
            matching = [o for o in matching if o.get("proposal_id") == assertion["proposal_id"]]
        if assertion.get("tier"):
            matching = [o for o in matching if o.get("tier") == assertion["tier"]]
        return bool(matching), "controlled marker and identity observed" if matching else "required identity absent"
    if kind == "omits_decision":
        found = any(o["action_id"] == assertion["action_id"]
                    and o.get("decision_id") == assertion["decision_id"]
                    and o["status"] == "observed" and o["stage"] in DELIVERY_STAGES
                    for o in observations)
        return not found, "identity absent" if not found else "forbidden identity observed"
    if kind == "contains_text":
        passed = assertion["text"] in text
        return passed, "text observed" if passed else "required text absent"
    if kind == "omits_text":
        passed = assertion["text"] not in text
        return passed, "text absent" if passed else "forbidden text observed"
    if kind == "action_status":
        matching = [o for o in observations if o["action_id"] == assertion["action_id"]]
        passed = bool(matching) and all(o["status"] == assertion["status"] for o in matching)
        return passed, f"status={matching[0]['status'] if matching else 'missing'}"
    if kind == "rank_order":
        ids = detail.get("ranked_ids", [])
        wanted = assertion["decision_ids"]
        present = [did for did in ids if did in wanted]
        return present == wanted, f"observed order={present}"
    if kind == "prompt_rank_call_count":
        count = detail.get("prompt_rank_calls", 0)
        passed = count >= assertion.get("minimum", 1)
        return passed, f"count={count}"
    if kind == "observation_count":
        matching = [o for o in observations if o["action_id"] == assertion["action_id"]
                    and o["status"] == assertion.get("status", "observed")
                    and (not assertion.get("stage") or o["stage"] == assertion["stage"])
                    and (not assertion.get("decision_only") or o.get("decision_id"))]
        count = len(matching)
        passed = count >= assertion.get("minimum", count) and count <= assertion.get("maximum", count)
        return passed, f"count={count}"
    if kind == "outcome_result":
        match = next((o for o in outcomes if o.get("evidence_id") == assertion["evidence_id"]), None)
        passed = bool(match) and match["result"] == assertion["result"]
        return passed, f"result={match['result'] if match else 'missing'}"
    if kind == "same_text":
        other = _find_action(details, assertion["other_action_id"])
        passed = text == other.get("text", "")
        return passed, "outputs equal" if passed else "outputs differ"
    if kind == "different_text":
        other = _find_action(details, assertion["other_action_id"])
        passed = text != other.get("text", "")
        return passed, "outputs differ" if passed else "outputs equal"
    if kind == "index_unchanged":
        before = _find_action(details, assertion["before_action_id"])
        after = _find_action(details, assertion["after_action_id"])
        passed = before.get("index_sha256") == after.get("sha256")
        return passed, "reader left malformed index untouched" if passed else "index changed"
    if kind == "index_absent":
        passed = not detail.get("exists", True)
        return passed, "index remains absent" if passed else "prompt path rebuilt index"
    raise FixtureError(f"unknown assertion kind {kind}")


def _tier_satisfies(observed: str, required: str) -> bool:
    return observed == required or observed in FULL_TIERS and required in FULL_TIERS


def _metrics(cases: list[dict[str, Any]]) -> dict[str, Any]:
    emitted_full: set[tuple[Any, ...]] = set()
    applicable_full: set[tuple[Any, ...]] = set()
    required_num = required_den = 0
    whole_case_num = whole_case_den = 0
    whole_request_num = whole_request_den = 0
    all_assigned_num = all_assigned_den = 0
    no_decision_full = no_decision_den = 0
    authority_errors = 0
    observation_counts: Counter[str] = Counter()
    stage_counts: Counter[str] = Counter()
    checked_cases = 0
    by_host: dict[str, Counter[str]] = {}
    by_surface: dict[str, Counter[str]] = {}
    by_request: list[dict[str, Any]] = []
    for case in cases:
        expectations = case.get("delivery_expectations", [])
        expected_by_request: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for expected in expectations:
            group = (
                case["case_id"], expected["action_id"], expected.get("session_id"),
                expected.get("request_id"),
            )
            expected_by_request.setdefault(group, []).append(expected)

        delivered_full_by_request: dict[tuple[Any, ...], set[tuple[str, str | None]]] = {}
        unsupported_requests: set[tuple[Any, ...]] = set()
        for obs in case["observations"]:
            observation_counts[obs["status"]] += 1
            stage_counts[obs["stage"]] += 1
            group = (
                case["case_id"], obs["action_id"], obs.get("session_id"), obs.get("request_id"),
            )
            if obs["status"] == "unsupported" and obs["stage"] in DELIVERY_STAGES:
                unsupported_requests.add(group)
            if (obs["stage"] not in DELIVERY_STAGES or obs["status"] != "observed"
                    or not obs.get("decision_id")):
                continue
            identity = (obs["decision_id"], obs.get("revision_id"))
            matching = [
                expected for expected in expected_by_request.get(group, [])
                if (expected["decision_id"], expected.get("revision_id")) == identity
                and expected.get("proposal_id") == obs.get("proposal_id")
            ]
            if obs["tier"] in FULL_TIERS:
                token = (*group, *identity, obs.get("proposal_id"))
                host = obs.get("host", case["host"])
                surface = obs.get("surface", case["surface"])
                new_emission = token not in emitted_full
                emitted_full.add(token)
                delivered_full_by_request.setdefault(group, set()).add(identity)
                if new_emission:
                    by_host.setdefault(host, Counter())["emitted_full"] += 1
                    by_surface.setdefault(surface, Counter())["emitted_full"] += 1
                if matching:
                    new_applicable = token not in applicable_full
                    applicable_full.add(token)
                    if new_applicable:
                        by_host.setdefault(host, Counter())["applicable_emitted_full"] += 1
                        by_surface.setdefault(surface, Counter())["applicable_emitted_full"] += 1
        case_delivered = 0
        case_required = 0
        case_supported = True
        for group, group_expectations in expected_by_request.items():
            observations = [
                obs for obs in case["observations"]
                if (case["case_id"], obs["action_id"], obs.get("session_id"),
                    obs.get("request_id")) == group
                and obs["stage"] in DELIVERY_STAGES and obs["status"] == "observed"
            ]
            delivered = 0
            for expected in group_expectations:
                if any(
                    obs.get("decision_id") == expected["decision_id"]
                    and obs.get("revision_id") == expected.get("revision_id")
                    and _tier_satisfies(obs["tier"], expected["required_tier"])
                    and obs.get("proposal_id") == expected.get("proposal_id")
                    for obs in observations
                ):
                    delivered += 1
            supported = group not in unsupported_requests
            if supported:
                required_num += delivered
                required_den += len(group_expectations)
            case_delivered += delivered
            case_required += len(group_expectations)
            case_supported = case_supported and supported
            if supported:
                whole_request_den += 1
                whole_request_num += int(delivered == len(group_expectations))
            by_request.append({
                "case_id": case["case_id"], "action_id": group[1],
                "session_id": group[2], "request_id": group[3], "supported": supported,
                "numerator": delivered, "denominator": len(group_expectations),
            })

        if expectations:
            all_assigned_den += 1
            all_assigned_num += int(case_delivered == case_required)
            if case_supported:
                whole_case_den += 1
                whole_case_num += int(case_delivered == case_required)
        else:
            delivery_groups = {
                (case["case_id"], obs["action_id"], obs.get("session_id"), obs.get("request_id"))
                for obs in case["observations"] if obs["stage"] in DELIVERY_STAGES
            }
            for group in delivery_groups - unsupported_requests:
                no_decision_den += 1
                no_decision_full += int(bool(delivered_full_by_request.get(group)))
        if any(outcome["result"] != "unknown" for outcome in case["outcomes"]):
            checked_cases += 1
        authority_errors += sum(
            assertion.get("metric") == "authority" and assertion["status"] != "passed"
            for assertion in case.get("assertions", [])
        )
    def ratio(num: int, den: int) -> float | None:
        return num / den if den else None

    return {
        "full_guidance_precision": {
            "numerator": len(applicable_full), "denominator": len(emitted_full),
            "ratio": ratio(len(applicable_full), len(emitted_full)),
            "by_host": {host: {"numerator": counts["applicable_emitted_full"],
                                "denominator": counts["emitted_full"],
                                "ratio": ratio(counts["applicable_emitted_full"], counts["emitted_full"])}
                        for host, counts in sorted(by_host.items())},
            "by_surface": {surface: {"numerator": counts["applicable_emitted_full"],
                                      "denominator": counts["emitted_full"],
                                      "ratio": ratio(counts["applicable_emitted_full"],
                                                     counts["emitted_full"])}
                           for surface, counts in sorted(by_surface.items())},
        },
        "required_guidance_coverage": {
            "numerator": required_num, "denominator": required_den,
            "ratio": ratio(required_num, required_den),
            "by_request": [
                {**row, "ratio": ratio(row["numerator"], row["denominator"])}
                for row in sorted(
                    by_request,
                    key=lambda row: (row["case_id"], row["action_id"], row["session_id"] or ""),
                )
            ],
        },
        "whole_case_coverage": {
            "supported": {"numerator": whole_case_num, "denominator": whole_case_den,
                          "ratio": ratio(whole_case_num, whole_case_den)},
            "all_assigned_conservative": {"numerator": all_assigned_num,
                                           "denominator": all_assigned_den,
                                           "ratio": ratio(all_assigned_num, all_assigned_den)},
        },
        "whole_request_coverage": {
            "numerator": whole_request_num, "denominator": whole_request_den,
            "ratio": ratio(whole_request_num, whole_request_den),
        },
        "no_decision_false_positive_rate": {
            "numerator": no_decision_full, "denominator": no_decision_den,
            "ratio": ratio(no_decision_full, no_decision_den),
        },
        "authority_errors": authority_errors,
        "observation_coverage": dict(sorted(observation_counts.items())),
        "observation_coverage_by_stage": dict(sorted(stage_counts.items())),
        "checked_outcome_coverage": {
            "numerator": checked_cases, "denominator": len(cases),
            "ratio": ratio(checked_cases, len(cases)),
        },
    }


def _delivery_expectations(case: dict[str, Any]) -> list[dict[str, Any]]:
    """Expand reviewer labels onto the exact requests where delivery is required."""
    labels = {
        (item["decision_id"], item["revision_id"]): item for item in case["applicable"]
    }
    raw = case.get("delivery_expectations")
    if raw is None:
        raw = [
            assertion for assertion in case["desired_assertions"]
            if assertion.get("kind") == "contains_decision"
            and not assertion.get("proposal_id")
            and (assertion.get("decision_id"), assertion.get("revision_id")) in labels
        ]
    actions = {action["action_id"]: action for action in case["actions"]}
    expanded: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for item in raw:
        action = actions[item["action_id"]]
        label = labels[(item["decision_id"], item["revision_id"])]
        expectation = {
            **_action_identity(case, action),
            "decision_id": item["decision_id"],
            "revision_id": item["revision_id"],
            "proposal_id": item.get("proposal_id"),
            "required_tier": item.get("required_tier", label["required_tier"]),
            "authority": item.get("authority", label["authority"]),
        }
        key = tuple(expectation.values())
        if key not in seen:
            seen.add(key)
            expanded.append(expectation)
    return expanded


def run_case(case: dict[str, Any]) -> dict[str, Any]:
    old_store = store.STORE_DIR
    with tempfile.TemporaryDirectory(prefix=f"contexer-relevance-{case['family'].lower()}-") as tmp:
        sandbox = Path(tmp)
        repo_path = sandbox / case["repo_id"]
        repo_path.mkdir(parents=True)
        store.STORE_DIR = sandbox / ".contexer"
        try:
            entries = [_entry(item) for item in case.get("decisions", [])]
            store.save(str(repo_path), {"repo_path": str(repo_path), "entries": entries})
            global_entries = [_entry(item) for item in case.get("global_decisions", [])]
            if global_entries:
                store.save_global({"repo_path": "__global__", "entries": global_entries})
            observations: list[dict[str, Any]] = []
            details: list[dict[str, Any]] = []
            state: dict[str, Any] = {}
            for action in case["actions"]:
                obs, detail = _run_action(case, action, str(repo_path), state)
                observations.extend(obs)
                details.append(_normalize_detail(detail, str(repo_path), case["repo_id"]))
            outcomes = state.get("outcomes", [])
            assertions = []
            for assertion in case["desired_assertions"]:
                passed, reason = _evaluate_assertion(assertion, observations, details, outcomes)
                status = "passed" if passed else ("known_gap" if assertion.get("known_gap") else "failed")
                assertions.append({
                    "key": assertion["key"], "status": status, "reason": reason,
                    "known_gap": assertion.get("known_gap"), "owner": assertion.get("owner"),
                    "metric": assertion.get("metric"),
                })
            forbidden = []
            all_text = "\n".join(detail.get("text", "") for detail in details)
            for token in case.get("forbidden_outputs", []):
                forbidden.append({"text": token, "present": token in all_text})
            if any(item["present"] for item in forbidden):
                assertions.append({
                    "key": "forbidden-output", "status": "failed",
                    "reason": "forbidden output observed", "known_gap": None, "owner": None,
                    "metric": None,
                })
            return {
                "case_id": case["case_id"], "family": case["family"],
                "description": case["description"], "host": case["host"],
                "surface": case["surface"], "repo_id": case["repo_id"],
                "checkout_id": case["checkout_id"], "session_id": case["session_id"],
                "request_id": case.get("request_id"), "applicable": case["applicable"],
                "delivery_expectations": _delivery_expectations(case),
                "observations": observations, "outcomes": outcomes,
                "outcome_unknown_reason": None if outcomes else case.get("outcome_unknown_reason", "retrieval_only"),
                "assertions": assertions, "forbidden_outputs": forbidden,
                "reference_observation": case.get("reference_observation"),
                "action_details": details,
            }
        finally:
            store.STORE_DIR = old_store


def build_report(fixture: dict[str, Any] | None = None, *, reverse: bool = False) -> dict[str, Any]:
    if fixture is None:
        fixture = load_fixture()
    else:
        validate_fixture(fixture)
    cases = list(fixture["cases"])
    if reverse:
        cases.reverse()
    results = [run_case(case) for case in cases]
    if reverse:
        results.sort(key=lambda result: result["case_id"])
    else:
        results.sort(key=lambda result: result["case_id"])
    known_gaps = [
        {"case_id": case["case_id"], "key": assertion["key"],
         "gap": assertion["known_gap"], "owner": assertion["owner"]}
        for case in results for assertion in case["assertions"]
        if assertion["status"] == "known_gap"
    ]
    failures = [
        {"case_id": case["case_id"], "key": assertion["key"], "reason": assertion["reason"]}
        for case in results for assertion in case["assertions"] if assertion["status"] == "failed"
    ]
    unsupported = [
        {"case_id": case["case_id"], "action_id": obs["action_id"], "reason": obs["reason"]}
        for case in results for obs in case["observations"] if obs["status"] == "unsupported"
    ]
    return {
        "schema_version": SCHEMA_VERSION,
        "code_revision": _git(["rev-parse", "HEAD"]),
        "dirty": _git(["status", "--porcelain"]) not in ("", "unknown"),
        "fixture_version": fixture["fixture_version"],
        "fixture_sha256": _fixture_sha256(fixture),
        "runner_version": RUNNER_VERSION,
        "runner_sha256": _sha256(Path(__file__)),
        "cases": results,
        "summary": {
            "assigned_cases": len(results), "known_gaps": known_gaps,
            "unexpected_failures": failures, "unsupported_paths": unsupported,
            "metrics": _metrics(results),
        },
    }


def render_text(report: dict[str, Any]) -> str:
    summary = report["summary"]
    lines = [
        "Decision relevance baseline",
        f"code: {report['code_revision']} ({'dirty' if report['dirty'] else 'clean'})",
        f"fixtures: v{report['fixture_version']} {report['fixture_sha256'][:12]}",
        f"cases: {summary['assigned_cases']}",
        f"known gaps: {len(summary['known_gaps'])}",
        f"unexpected failures: {len(summary['unexpected_failures'])}",
        f"unsupported paths: {len(summary['unsupported_paths'])}",
        "",
    ]
    for case in report["cases"]:
        counts = Counter(item["status"] for item in case["assertions"])
        lines.append(
            f"{case['case_id']} {case['family']}: "
            f"{counts['passed']} passed, {counts['known_gap']} known gap, {counts['failed']} failed"
        )
    if summary["known_gaps"]:
        lines.extend(["", "Known gaps:"])
        for gap in summary["known_gaps"]:
            lines.append(f"- {gap['case_id']} {gap['key']}: {gap['gap']} -> {gap['owner']}")
    return "\n".join(lines)


def exit_code(report: dict[str, Any], strict: bool = False) -> int:
    if report["summary"]["unexpected_failures"]:
        return 1
    if strict and report["summary"]["known_gaps"]:
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Run the deterministic relevance baseline. Normal mode exits 0 when only registered "
            "known gaps remain; --strict exits 1 until every desired assertion passes."
        )
    )
    parser.add_argument("--format", choices=("text", "json"), default="text")
    parser.add_argument("--output", type=Path, help="write the report to this explicit path")
    parser.add_argument("--strict", action="store_true", help="fail while any registered known gap remains")
    parser.add_argument("--fixture", type=Path, default=FIXTURE_PATH)
    args = parser.parse_args(argv)
    try:
        fixture = load_fixture(args.fixture)
        report = build_report(fixture)
    except FixtureError as exc:
        print(f"invalid fixture: {exc}", file=sys.stderr)
        return 1
    rendered = json.dumps(report, indent=2, sort_keys=True) if args.format == "json" else render_text(report)
    if args.output:
        args.output.write_text(rendered + "\n", encoding="utf-8")
    else:
        print(rendered)
    return exit_code(report, args.strict)


if __name__ == "__main__":
    raise SystemExit(main())
