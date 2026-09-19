"""Bounded local evidence that guidance was prepared and a condition was checked.

This is diagnostics, never decision evidence. Records cannot approve, arm, anchor, share,
retire, or change confidence. Writers are opt-in and fail soft; readers are explicit and never
run a check. The decision store is intentionally not used as the ledger: this module owns one
small per-repository sidecar and its independent lock.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import hmac
import importlib.metadata
import json
import math
import os
import re
import secrets
import time
import uuid
from pathlib import Path

from contexer import config, redact, store


VERSION = 1
MAX_RECORDS = 256
MAX_BYTES = 256 * 1024
MAX_RECORD_BYTES = 16 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
_PROJECTION_HEADROOM = 16 * 1024
MAX_PAGE_ROWS = 50
MAX_CURSOR_BYTES = 1024
RETENTION_SECONDS = 7 * 24 * 3600
_ID = re.compile(r"^[0-9a-f]{32}$")
_SECRET = re.compile(r"^[0-9a-f]{64}$")
_TOP_KEYS = frozenset({
    "kind", "producer_version", "repository", "checkout", "route", "host",
    "session", "request", "process", "stage", "reason", "decisions", "truncated",
    "artifact", "evaluator", "policy_set_version", "conditions", "guidance_refs",
    "linkage_gaps", "coverage",
})
_KINDS = frozenset({"guidance", "evaluation"})


def path(repo_path: str) -> Path:
    return store.sidecar_path("decision_impact", slug=store.repo_slug(repo_path))


def lock_path(repo_path: str) -> Path:
    return store.sidecar_path("decision_impact_lock", slug=store.repo_slug(repo_path))


def collection_enabled() -> bool:
    return config.decision_impact_enabled(store.store_dir() / "config.toml")


def _version() -> str:
    try:
        return importlib.metadata.version("contexer")
    except Exception:
        return "unknown"


def identity(namespace: str, value: str) -> str:
    """A stable namespaced local identifier. This is not an anonymization guarantee."""
    if not value:
        return ""
    digest = hashlib.sha256(f"decision-impact-v1:{namespace}:{value}".encode()).hexdigest()
    return f"sha256:{digest}"


def guidance_envelope(repo_path: str, physical_repo: str, *, route: str,
                      rows: list[dict], stage: str = "prepared", reason: str = "",
                      host: str = "", session_id: str = "", request_id: str = "",
                      process_id: str = "", truncated: bool = False,
                      configured_cap: int = 0, output_bytes: int = 0) -> dict:
    """Build the allowlisted, content-free observation written after final rendering."""
    return {
        "kind": "guidance",
        "producer_version": _version(),
        "repository": identity("repository", store.canonical_store_key(repo_path)),
        "checkout": identity("checkout", physical_repo),
        "route": route,
        "host": host,
        "session": identity("session", session_id),
        "request": identity("request", request_id),
        "process": identity("process", process_id),
        "stage": stage,
        "reason": reason,
        "decisions": rows[:32],
        "truncated": truncated or len(rows) > 32,
        "coverage": {
            "selected": min(len(rows), 32),
            "rendered": min(len(rows), 32),
            "prepared": min(len(rows), 32),
            "configured_cap": max(0, configured_cap),
            "output_bytes": max(0, output_bytes),
        },
    }


def evaluation_envelope(repo_path: str, physical_repo: str, *, artifact: dict,
                        policy_set_version: str, conditions: list[dict],
                        guidance_refs: list[dict] | None = None,
                        linkage_gaps: list[str] | None = None,
                        coverage: str = "complete") -> dict:
    return {
        "kind": "evaluation",
        "producer_version": _version(),
        "repository": identity("repository", store.canonical_store_key(repo_path)),
        "checkout": identity("checkout", physical_repo),
        "artifact": artifact,
        "evaluator": "bounded_file_v1",
        "policy_set_version": policy_set_version,
        "conditions": conditions[:32],
        "guidance_refs": list(guidance_refs or [])[:8],
        "linkage_gaps": list(linkage_gaps or [])[:8],
        "coverage": coverage,
        "truncated": len(conditions) > 32,
    }


def _empty() -> dict:
    return {
        "v": VERSION,
        "epoch": uuid.uuid4().hex,
        "cursor_secret": secrets.token_hex(32),
        "next_sequence": 1,
        "pruned_through": 0,
        "records": [],
    }


def _bounded(value: object, depth: int = 0) -> bool:
    if depth > 5:
        return False
    if value is None or isinstance(value, (bool, int, float)):
        return True
    if isinstance(value, str):
        return len(value) <= 512
    if isinstance(value, list):
        return len(value) <= 32 and all(_bounded(item, depth + 1) for item in value)
    if isinstance(value, dict):
        return len(value) <= 64 and all(
            isinstance(key, str) and len(key) <= 64 and _bounded(item, depth + 1)
            for key, item in value.items())
    return False


def _choice(*values):
    return lambda value: isinstance(value, str) and value in values


def _pattern(pattern: str):
    compiled = re.compile(pattern)
    return lambda value: isinstance(value, str) and compiled.fullmatch(value) is not None


def _count(value: object) -> bool:
    return type(value) is int and 0 <= value <= 2**31 - 1


def _fields(value: object, schema: dict, optional: tuple[str, ...] = ()) -> bool:
    return (isinstance(value, dict) and set(value) <= schema.keys()
            and schema.keys() - set(optional) <= value.keys()
            and all(schema[key](item) for key, item in value.items()))


def _rows(value: object, validator, limit: int = 32) -> bool:
    return isinstance(value, list) and len(value) <= limit and all(validator(row) for row in value)


def _relative_path(value: object) -> bool:
    return (isinstance(value, str) and 0 < len(value) <= 300
            and not value.startswith(("/", "\\")) and not re.match(r"[A-Za-z]:", value)
            and ".." not in value.replace("\\", "/").split("/")
            and not any(ord(char) < 32 for char in value))


_TOKEN = _pattern(r"[A-Za-z0-9_.:-]{1,128}")
_DIGEST = _pattern(r"sha256:[0-9a-f]{64}")
_AUTHORITY = _choice("human_approved", "trusted_approved", "approved_untrusted",
                     "approved", "suggested", "pending_approval", "ignored")
_SCOPE = _choice("personal", "global")
def _files(value: object) -> bool:
    return _rows(value, _relative_path)


_GUIDANCE_SCHEMA = {
    "scope": _SCOPE, "id": _TOKEN, "revision_id": _TOKEN,
    "fingerprint": _pattern(r"(?:guidance-v[0-9]+|sha256):[0-9a-f]{64}"),
    "authority": _AUTHORITY, "proposal_id": lambda value: value == "" or _DIGEST(value),
    "tier": _choice("full", "excerpt", "title", "pointer"),
    "reason": _choice("files", "query", "entry_type", "overview", "prompt_selected",
                      "source_files match"),
    "rank": lambda value: value is None or _count(value), "files": _files,
    "rule_digest": lambda value: value == "" or _DIGEST(value),
}
_CONDITION_SCHEMA = {
    "decision_id": lambda value: value == "" or _TOKEN(value),
    "revision_id": lambda value: value == "" or _TOKEN(value),
    "scope": _SCOPE, "authority": _AUTHORITY, "rule_digest": _DIGEST,
    "rule_type": _choice("regex", "secret", "unknown", ""),
    "profile": _choice("bounded_file_v1"), "files": _files,
    "identity_complete": lambda value: type(value) is bool,
    "result": _choice("satisfied", "violated", "unchecked", "error"),
    "gap": _choice("", "budget", "omitted", "unsupported-check", "unattributable",
                   "bad-pattern", "evaluator-error"),
    "applicable_units": _count, "evaluated_units": _count, "match_count": _count,
    "complete": lambda value: type(value) is bool,
    "verified": lambda value: type(value) is bool,
}
_ARTIFACT_SCHEMA = {
    "path": _relative_path, "digest": _DIGEST,
    "bytes": lambda value: _count(value) and value <= 64 * 1024,
    "kind": _choice("file_content"), "provenance": _choice("authorized_server_read"),
}
_REFERENCE_SCHEMA = {
    "receipt_id": _pattern(r"[0-9a-f]{32}"), "attribution": _choice("caller_linked"),
}
_COVERAGE_SCHEMA = {key: _count for key in (
    "selected", "rendered", "prepared", "configured_cap", "output_bytes")}


def _valid_condition(row: object) -> bool:
    if not _fields(row, _CONDITION_SCHEMA, ("match_count",)):
        return False
    if row["verified"]:
        return (row["complete"] and row["identity_complete"]
                and row["result"] in ("satisfied", "violated") and row["gap"] == ""
                and row["applicable_units"] > 0
                and row["applicable_units"] == row["evaluated_units"]
                and bool(row["decision_id"]) and row["revision_id"] not in ("", "legacy")
                and row["authority"] in ("trusted_approved", "human_approved"))
    return True


def _valid_record_shape(record: dict) -> bool:
    """Closed schemas reject unexpected content at ingestion and when loading history."""
    schema = {
        "kind": _choice("guidance", "evaluation"),
        "producer_version": _pattern(r"[A-Za-z0-9_.+-]{1,64}"),
        "repository": _DIGEST, "checkout": _DIGEST,
        "truncated": lambda value: type(value) is bool,
    }
    if record.get("kind") == "guidance":
        schema.update({
            "route": _choice("explicit_lookup", "indexed_prompt"),
            "host": _choice("", "mcp", "claude", "codex", "gemini"),
            **{key: lambda value: value == "" or _DIGEST(value)
               for key in ("session", "request", "process")},
            "stage": _choice("prepared"),
            "reason": _choice("", "gate_closed", "no_candidate", "already_delivered_revision",
                              "budget_limited", "render_skipped", "branch_uninstrumented", "error"),
            "decisions": lambda value: _rows(value, lambda row: _fields(
                row, _GUIDANCE_SCHEMA, ("rank", "proposal_id", "rule_digest"))),
            "coverage": lambda value: _fields(value, _COVERAGE_SCHEMA),
        })
    else:
        schema.update({
            "artifact": lambda value: _fields(value, _ARTIFACT_SCHEMA),
            "evaluator": _choice("bounded_file_v1"), "policy_set_version": _DIGEST,
            "conditions": lambda value: _rows(value, _valid_condition),
            "guidance_refs": lambda value: _rows(
                value, lambda row: _fields(row, _REFERENCE_SCHEMA), 8),
            "linkage_gaps": lambda value: _rows(value, _choice(
                "invalid_reference", "not_retained_or_unknown", "identity_mismatch",
                "reference_limit", "history_unavailable"), 8),
            "coverage": _choice("complete", "partial", "error", "no_applicable_conditions"),
        })
    content = {key: value for key, value in record.items()
               if key not in ("receipt_id", "sequence", "created_at")}
    return _fields(content, schema)


def _valid_record(record: object, *, stored: bool = False) -> bool:
    if not isinstance(record, dict) or not _bounded(record):
        return False
    allowed = _TOP_KEYS | ({"receipt_id", "sequence", "created_at"} if stored else set())
    if (set(record) - allowed or record.get("kind") not in _KINDS
            or not _valid_record_shape(record)):
        return False
    if stored:
        if not _ID.fullmatch(str(record.get("receipt_id") or "")):
            return False
        if (not isinstance(record.get("sequence"), int)
                or isinstance(record["sequence"], bool) or record["sequence"] < 1):
            return False
        if (not isinstance(record.get("created_at"), (int, float))
                or isinstance(record["created_at"], bool)
                or not math.isfinite(record["created_at"]) or record["created_at"] < 0):
            return False
    return len(json.dumps(record, separators=(",", ":")).encode()) <= MAX_RECORD_BYTES


def _valid_state(data: object) -> bool:
    if not isinstance(data, dict) or data.get("v") != VERSION:
        return False
    if set(data) != {"v", "epoch", "cursor_secret", "next_sequence",
                     "pruned_through", "records"}:
        return False
    if not _ID.fullmatch(str(data.get("epoch") or "")):
        return False
    secret = data.get("cursor_secret")
    if not isinstance(secret, str) or not _SECRET.fullmatch(secret):
        return False
    if (not isinstance(data.get("next_sequence"), int)
            or isinstance(data["next_sequence"], bool) or data["next_sequence"] < 1):
        return False
    if (not isinstance(data.get("pruned_through"), int)
            or isinstance(data["pruned_through"], bool) or data["pruned_through"] < 0):
        return False
    records = data.get("records")
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        return False
    if not all(_valid_record(record, stored=True) for record in records):
        return False
    sequences = [record["sequence"] for record in records]
    return (sequences == sorted(set(sequences))
            and all(sequence > data["pruned_through"] for sequence in sequences)
            and data["next_sequence"] > data["pruned_through"]
            and (not sequences or data["next_sequence"] > sequences[-1]))


def _load(repo_path: str) -> tuple[dict | None, str]:
    target = path(repo_path)
    if not target.exists():
        return _empty(), "missing"
    try:
        with target.open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return None, "oversized"
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None, "unreadable"
    return (data, "ok") if _valid_state(data) else (None, "invalid")


def _serialized(state: dict) -> str | None:
    raw = json.dumps(state, separators=(",", ":"), sort_keys=True)
    return raw if len(raw.encode()) <= MAX_BYTES else None


def append(repo_path: str, envelope: dict, *, now: float | None = None) -> str:
    """Append one observation when enabled. Return its opaque id, or ``""`` on any failure."""
    if not collection_enabled() or not _valid_record(envelope):
        return ""
    stamp = time.time() if now is None else now
    try:
        store.ensure_store_dir()
        fd = os.open(lock_path(repo_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            state, _status = _load(repo_path)
            if state is None:
                return ""
            records = list(state["records"])
            expired = [record["sequence"] for record in records
                       if stamp - float(record["created_at"]) > RETENTION_SECONDS]
            if expired:
                cutoff = max(expired)
                records = [record for record in records if record["sequence"] > cutoff]
                state["pruned_through"] = max(state["pruned_through"], cutoff)
            receipt_id = uuid.uuid4().hex
            record = {
                **envelope,
                "receipt_id": receipt_id,
                "sequence": state["next_sequence"],
                "created_at": stamp,
            }
            if not _valid_record(record, stored=True):
                return ""
            state["next_sequence"] += 1
            records.append(record)
            while len(records) > MAX_RECORDS:
                removed = records.pop(0)
                state["pruned_through"] = max(
                    state["pruned_through"], removed["sequence"])
            state["records"] = records
            raw = _serialized(state)
            while raw is None and records:
                removed = records.pop(0)
                state["pruned_through"] = max(
                    state["pruned_through"], removed["sequence"])
                state["records"] = records
                raw = _serialized(state)
            if raw is None or not any(r["receipt_id"] == receipt_id for r in records):
                return ""
            store.atomic_write(path(repo_path), raw)
            return receipt_id
        finally:
            os.close(fd)
    except (BlockingIOError, OSError, ValueError, TypeError):
        return ""


def _cursor_encode(state: dict, payload: dict) -> str:
    body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    sig = hmac.new(bytes.fromhex(state["cursor_secret"]), body, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(body + sig).decode().rstrip("=")


def _cursor_decode(state: dict, cursor: str) -> dict | None:
    if not isinstance(cursor, str) or not cursor or len(cursor) > MAX_CURSOR_BYTES:
        return None
    try:
        raw = base64.urlsafe_b64decode(cursor + "=" * (-len(cursor) % 4))
        body, supplied = raw[:-32], raw[-32:]
        expected = hmac.new(bytes.fromhex(state["cursor_secret"]), body, hashlib.sha256).digest()
        if not hmac.compare_digest(supplied, expected):
            return None
        payload = json.loads(body.decode())
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _record_matches(record: dict, files: tuple[str, ...]) -> bool:
    if not files:
        return True
    artifact = record.get("artifact") or {}
    if artifact.get("path") in files:
        return True
    return any(set(row.get("files") or []) & set(files)
               for row in record.get("decisions") or [] if isinstance(row, dict))


def report(repo_path: str, *, files: list[str] | None = None, limit: int = 10,
           receipt_id: str = "", cursor: str = "", now: float | None = None) -> dict:
    """Read one retained receipt or a stable page. Never writes, prunes, or evaluates."""
    if receipt_id and cursor:
        return {"status": "invalid_request", "error": "receipt_id and cursor are exclusive"}
    bounded_files = tuple(files or [])
    if (len(bounded_files) > 32 or any(
            not isinstance(item, str) or not item or len(item) > 300 or item.startswith("/")
            or ".." in Path(item).parts for item in bounded_files)):
        return {"status": "invalid_request", "error": "invalid file filter"}
    page_limit = min(max(limit if isinstance(limit, int) else 10, 1), MAX_PAGE_ROWS)
    state, status = _load(repo_path)
    if state is None:
        return {"status": "history_unavailable", "reason": status, "records": []}
    stamp = time.time() if now is None else now
    retained = [record for record in state["records"]
                if stamp - float(record["created_at"]) <= RETENTION_SECONDS]
    expired_hidden = len(state["records"]) - len(retained)
    if receipt_id:
        if not _ID.fullmatch(receipt_id):
            return {"status": "invalid_request", "error": "invalid receipt_id"}
        record = next((row for row in retained if row["receipt_id"] == receipt_id), None)
        return ({"status": "ok", "records": [record], "next_cursor": "",
                 "coverage": {"expired_hidden": expired_hidden}}
                if record else {"status": "not_retained_or_unknown", "records": []})

    repo_identity = identity("repository", store.canonical_store_key(repo_path))
    high = max((record["sequence"] for record in retained), default=0)
    before = high + 1
    retained_after = stamp - RETENTION_SECONDS
    if cursor:
        payload = _cursor_decode(state, cursor)
        expected_files = list(bounded_files)
        if (payload is None or payload.get("v") != VERSION
                or payload.get("repository") != repo_identity
                or payload.get("epoch") != state["epoch"]
                or payload.get("files") != expected_files
                or not isinstance(payload.get("high"), int)
                or not isinstance(payload.get("last"), int)
                or not isinstance(payload.get("pruned"), int)
                or not isinstance(payload.get("retained_after"), (int, float))
                or isinstance(payload.get("retained_after"), bool)
                or not math.isfinite(payload["retained_after"])):
            return {"status": "invalid_cursor", "records": []}
        high, before = payload["high"], payload["last"]
        retained_after = float(payload["retained_after"])
        # The first page binds the retention watermark. If it advances while a caller is
        # traversing that snapshot, some not-yet-visited sequence may have disappeared. A
        # filter makes it impossible to prove that the removed row would not have matched,
        # so restart explicitly instead of silently presenting a shortened history.
        if state["pruned_through"] > payload["pruned"]:
            return {"status": "history_changed", "restart_required": True, "records": []}
        current_cutoff = stamp - RETENTION_SECONDS
        if any(record["sequence"] <= high and record["sequence"] < before
               and float(record["created_at"]) >= retained_after
               and float(record["created_at"]) < current_cutoff
               for record in state["records"]):
            return {"status": "history_changed", "restart_required": True, "records": []}

    candidates = [record for record in reversed(retained)
                  if record["sequence"] <= high and record["sequence"] < before
                  and _record_matches(record, bounded_files)]
    rows: list[dict] = []
    for record in candidates:
        trial = {"status": "ok", "records": [*rows, record]}
        if len(json.dumps(trial, separators=(",", ":")).encode()) > (
                MAX_RESPONSE_BYTES - _PROJECTION_HEADROOM):
            break
        if len(rows) >= page_limit:
            break
        rows.append(record)
    remaining = candidates[len(rows):]
    next_cursor = ""
    if remaining and rows:
        next_cursor = _cursor_encode(state, {
            "v": VERSION,
            "repository": repo_identity,
            "files": list(bounded_files),
            "epoch": state["epoch"],
            "high": high,
            "last": rows[-1]["sequence"],
            "pruned": state["pruned_through"],
            "retained_after": retained_after,
        })
    return {
        "status": "ok",
        "records": rows,
        "next_cursor": next_cursor,
        "coverage": {
            "collection_enabled": collection_enabled(),
            "expired_hidden": expired_hidden,
            "pruned_through": state["pruned_through"],
        },
    }


def validate_guidance_refs(repo_path: str, physical_repo: str, refs: list,
                           conditions: list[dict]) -> tuple[list[dict], list[str]]:
    """Join only exact retained guidance identities; bad links never change a verdict."""
    linked: list[dict] = []
    gaps: list[str] = []
    expected_repo = identity("repository", store.canonical_store_key(repo_path))
    expected_checkout = identity("checkout", physical_repo)
    for ref in refs[:8]:
        if not isinstance(ref, str) or not _ID.fullmatch(ref):
            gaps.append("invalid_reference")
            continue
        found = report(repo_path, receipt_id=ref)
        records = found.get("records") or []
        if found.get("status") != "ok" or not records:
            gaps.append("not_retained_or_unknown")
            continue
        record = records[0]
        if (record.get("kind") != "guidance" or record.get("repository") != expected_repo
                or record.get("checkout") != expected_checkout or record.get("truncated")):
            gaps.append("identity_mismatch")
            continue
        guidance = record.get("decisions") or []
        matched = any(
            row.get("id") == condition.get("decision_id")
            and row.get("revision_id") == condition.get("revision_id")
            and row.get("rule_digest") == condition.get("rule_digest")
            and row.get("scope") == condition.get("scope")
            and row.get("revision_id") not in ("", "legacy")
            and condition.get("verified") is True
            for row in guidance for condition in conditions
            if isinstance(row, dict) and isinstance(condition, dict))
        if not matched:
            gaps.append("identity_mismatch")
            continue
        linked.append({"receipt_id": ref, "attribution": "caller_linked"})
    if len(refs) > 8:
        gaps.append("reference_limit")
    return linked, gaps


def clear(repo_path: str) -> bool:
    """Explicit repository-only reset. Callers own confirmation; report reads never call it."""
    try:
        store.ensure_store_dir()
        fd = os.open(lock_path(repo_path), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            if not path(repo_path).exists():
                return True
            raw = _serialized(_empty())
            if raw is None:
                return False
            store.atomic_write(path(repo_path), raw)
            return True
        finally:
            os.close(fd)
    except OSError:
        return False


def format_report(result: dict) -> str:
    """Bounded agent/CLI text projection. It never upgrades a condition into task success."""
    status = result.get("status") or "history_unavailable"
    if status != "ok":
        return f"Decision impact: {status.replace('_', ' ')}."
    records = result.get("records") or []
    if not records:
        enabled = (result.get("coverage") or {}).get("collection_enabled")
        suffix = "collection is enabled" if enabled else "collection is off"
        return f"Decision impact: no retained observations ({suffix})."
    lines = [f"Decision impact: {len(records)} retained observation(s)."]
    for record in records:
        rid = record.get("receipt_id", "")
        if record.get("kind") == "guidance":
            rows = record.get("decisions") or []
            lines.append(f"- receipt {rid}: guidance prepared; "
                         f"{len(rows)} decision revision(s); delivery/consumption unconfirmed")
            for row in rows:
                lines.append(
                    "  - decision " + str(row.get("id") or "unknown")
                    + " revision " + str(row.get("revision_id") or "unknown")
                    + "; scope=" + str(row.get("scope") or "unknown")
                    + "; tier=" + str(row.get("tier") or "unknown")
                    + "; reason=" + str(row.get("reason") or "unknown"))
        else:
            conditions = record.get("conditions") or []
            counts = {
                "satisfied": sum(1 for row in conditions
                                 if row.get("result") == "satisfied"
                                 and row.get("verified") is True),
                "violated": sum(1 for row in conditions
                                if row.get("result") == "violated"
                                and row.get("verified") is True),
                "unchecked": sum(1 for row in conditions
                                 if row.get("result") == "unchecked"
                                 or (row.get("result") in ("satisfied", "violated")
                                     and row.get("verified") is not True)),
                "error": sum(1 for row in conditions if row.get("result") == "error"),
            }
            artifact = record.get("artifact") or {}
            lines.append(
                f"- receipt {rid}: checked {artifact.get('path') or '(unknown artifact)'} "
                f"({artifact.get('digest') or 'unknown snapshot'}): "
                f"{counts['satisfied']} condition(s) satisfied, "
                f"{counts['violated']} violation(s), "
                f"{counts['unchecked'] + counts['error']} unverified")
            if not conditions:
                lines.append("  - no applicable condition was verified")
            for row in conditions:
                lines.append(
                    "  - decision " + str(row.get("decision_id") or "unknown")
                    + " revision " + str(row.get("revision_id") or "unknown")
                    + "; condition=" + str(row.get("rule_digest") or "unknown")
                    + "; result=" + str(row.get("result") or "unknown")
                    + "; completeness="
                    + ("complete" if row.get("complete") is True else "incomplete")
                    + "; evidence="
                    + ("verified" if row.get("verified") is True else "unverified"))
            refs = record.get("guidance_refs") or []
            if refs:
                lines.append("  attribution: caller-linked to "
                             + ", ".join(str(ref.get("receipt_id") or "unknown")
                                         for ref in refs))
            else:
                lines.append("  attribution: object-level association; task link unknown")
            gaps = record.get("linkage_gaps") or []
            if gaps:
                lines.append("  linkage gaps: " + ", ".join(map(str, gaps)))
    if result.get("next_cursor"):
        lines.append("next_cursor: " + str(result["next_cursor"]))
    lines.append("These are checks of specific conditions and snapshots, not proof that "
                 "Contexer improved or caused the task outcome.")
    return redact.scrub_text("\n".join(lines))


def scrubbed_report(result: object) -> object:
    """Redact string leaves for JSON/UI projection without mutating the ledger result."""
    if isinstance(result, dict):
        return {key: scrubbed_report(value) for key, value in result.items()}
    if isinstance(result, list):
        return [scrubbed_report(value) for value in result]
    return redact.scrub_text(result) if isinstance(result, str) else result
