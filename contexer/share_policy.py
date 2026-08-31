"""Schema-v1 storage and pure rules for remembered automatic decision proposals.

This module owns only local policy state.  It performs no network I/O and never takes the
decision-store lock.  Policy, intent, receipt, attention, and drainer state each use an
independent sidecar lock so an editor capture cannot wait behind a store scan or uploader.

Durable readers are deliberately strict.  Missing means empty/disabled; malformed never does.
A write based on a malformed queue would turn developer work into an empty queue plus the new
record, so every read-modify-write path refuses and returns or raises an opaque diagnostic id.
"""
from __future__ import annotations

import contextlib
import json
import re
import secrets
import string
import threading
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

from contexer import decision_observability, revisions, sidecars, store


SCHEMA_VERSION = 1
POLICY_MODE = "propose_approved"
OUTBOX_CAP = store.MAX_ENTRIES
ATTENTION_CAP = store.MAX_ENTRIES
RECEIPT_LOG_CAP = 2_000

RECEIPT_STATES = frozenset({
    "queued", "submitted", "already_pending", "unchanged", "attention", "baseline",
})
TERMINAL_RECEIPT_STATES = frozenset({
    "submitted", "already_pending", "unchanged", "attention", "baseline",
})
ERROR_CODES = frozenset({
    "unsupported_protocol", "account_mismatch", "policy_mismatch", "repo_mismatch",
    "team_mismatch", "not_member", "not_authorized", "ineligible_revision",
    "global_decision", "baseline_revision", "duplicate_receipt", "corrupt_queue",
    "stale_head", "stale_intent", "lock_busy", "rate_limited", "quota_exceeded",
    "trial_expired", "transient_error", "transport_error", "validation_error",
})
ERROR_CLASSES = frozenset({
    "authorization", "capability", "conflict", "lock", "rate_limit", "transport",
    "validation",
})

_FINGERPRINT_RE = re.compile(r"acctfp_v1_[A-Za-z0-9_-]{12,64}\Z")
_DIAGNOSTIC_RE = re.compile(r"diag_[A-Z0-9]{16}\Z")
_DIAGNOSTIC_ALPHABET = string.ascii_uppercase + string.digits
_MAX_TOKEN = 256
_MAX_ENDPOINT = 2_048
_MAX_REPO_PATH = 4_096
_MAX_RECORD_BYTES = 16_384
_MAX_POLICY_BYTES = 262_144

_POLICY_FIELDS = frozenset({
    "schema_version", "mode", "policy_generation", "repo_key", "repo_slug", "endpoint",
    "account_fingerprint", "team_id", "team_name_at_confirmation", "enabled_at",
    "include_existing", "baseline_revision_ids", "paused_reason",
})
_INTENT_FIELDS = frozenset({
    "schema_version", "idempotency_key", "policy_generation", "decision_id", "revision_id",
    "repo_path", "repo_key", "endpoint", "account_fingerprint", "team_id", "queued_at",
    "attempts", "last_error_code", "last_error_class", "diagnostic_id",
})
_RECEIPT_FIELDS = frozenset({
    "schema_version", "policy_generation", "endpoint", "account_fingerprint", "repo_key",
    "team_id", "decision_id", "revision_id", "state", "candidate_id", "reason",
    "recorded_at",
})
_ATTENTION_FIELDS = frozenset({
    "schema_version", "idempotency_key", "policy_generation", "decision_id", "revision_id",
    "repo_path", "repo_key", "endpoint", "account_fingerprint", "team_id", "reason",
    "moved_at", "last_error_code", "last_error_class", "diagnostic_id",
})


class SidecarDataError(RuntimeError):
    """A durable proposal sidecar could not be trusted."""

    def __init__(self, kind: str, diagnostic_id: str):
        self.kind = kind
        self.diagnostic_id = diagnostic_id
        super().__init__(f"{kind} sidecar is malformed; refused write ({diagnostic_id})")


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reason_code: str
    decision_id: str = ""
    revision_id: str = ""


@dataclass(frozen=True)
class OperationOutcome:
    result: str
    reason_code: str
    diagnostic_id: str | None = None


def _diagnostic_id() -> str:
    return "diag_" + "".join(secrets.choice(_DIAGNOSTIC_ALPHABET) for _ in range(16))


def _data_error(kind: str) -> SidecarDataError:
    return SidecarDataError(kind, _diagnostic_id())


def _emit(operation: str, outcome: OperationOutcome, started_ns: int) -> None:
    error_class = {
        "lock_busy": "lock",
        "corrupt_queue": "validation",
        "validation_error": "validation",
        "account_mismatch": "capability",
        "repo_mismatch": "validation",
        "team_mismatch": "authorization",
    }.get(outcome.reason_code, "none")
    decision_observability.emit_decision_operation(
        operation,
        result=outcome.result,
        reason_code=outcome.reason_code,
        error_class=error_class,
        started_ns=started_ns,
        diagnostic_id=outcome.diagnostic_id,
    )


def policy_path(repo_path: str) -> Path:
    return store.STORE_DIR / sidecars.filename("share_policy", slug=store.repo_slug(repo_path))


def proposal_outbox_path() -> Path:
    return store.STORE_DIR / sidecars.filename("proposal_outbox")


def proposal_receipts_path() -> Path:
    return store.STORE_DIR / sidecars.filename("proposal_receipts")


def proposal_attention_path() -> Path:
    return store.STORE_DIR / sidecars.filename("proposal_attention")


def policy_lock_path(repo_path: str) -> Path:
    return store.STORE_DIR / sidecars.filename(
        "share_policy_lock", slug=store.repo_slug(repo_path))


def proposal_outbox_lock_path() -> Path:
    return store.STORE_DIR / sidecars.filename("proposal_outbox_lock")


def proposal_drainer_lock_path() -> Path:
    return store.STORE_DIR / sidecars.filename("proposal_drainer_lock")


def proposal_receipts_lock_path() -> Path:
    return store.STORE_DIR / sidecars.filename("proposal_receipts_lock")


def proposal_attention_lock_path() -> Path:
    return store.STORE_DIR / sidecars.filename("proposal_attention_lock")


_LOCAL_LOCKS: dict[str, threading.Lock] = {}
_LOCAL_LOCKS_GUARD = threading.Lock()


def _local_lock(path: Path) -> threading.Lock:
    key = str(path)
    with _LOCAL_LOCKS_GUARD:
        return _LOCAL_LOCKS.setdefault(key, threading.Lock())


@contextlib.contextmanager
def _sidecar_lock(path: Path, *, blocking: bool = True):
    """Take one process-local mutex and its matching cross-process flock."""
    local = _local_lock(path)
    if not local.acquire(blocking=blocking):
        raise BlockingIOError("proposal sidecar lock busy")
    handle = None
    try:
        if store.fcntl is not None:
            store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
            handle = open(path, "a+b")
            try:
                store.fcntl.flock(
                    handle.fileno(),
                    store.fcntl.LOCK_EX | (0 if blocking else store.fcntl.LOCK_NB),
                )
            except BaseException:
                handle.close()
                handle = None
                raise
        yield
    finally:
        if handle is not None:
            try:
                store.fcntl.flock(handle.fileno(), store.fcntl.LOCK_UN)
            finally:
                handle.close()
        local.release()


def proposal_drainer_lock(*, blocking: bool = False):
    """Return the dedicated uploader lock; automatic callers use it non-blocking."""
    return _sidecar_lock(proposal_drainer_lock_path(), blocking=blocking)


def _text(value: object, field: str, *, maximum: int = _MAX_TOKEN) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"invalid {field}")
    return value


def _nullable_text(value: object, field: str, *, maximum: int = _MAX_TOKEN) -> str | None:
    if value is None:
        return None
    return _text(value, field, maximum=maximum)


def _timestamp(value: object, field: str) -> str:
    text = _text(value, field, maximum=64)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"invalid {field}") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"invalid {field}")
    return text


def _schema(record: object, kind: str) -> dict:
    if not isinstance(record, dict) or record.get("schema_version") != SCHEMA_VERSION:
        raise ValueError(f"invalid {kind} schema")
    return dict(record)


def _exact_fields(record: dict, allowed: frozenset[str], kind: str) -> None:
    if set(record) != allowed:
        raise ValueError(f"invalid {kind} fields")


def _endpoint(value: object) -> str:
    endpoint = _text(value, "endpoint", maximum=_MAX_ENDPOINT)
    parsed = urlsplit(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("invalid endpoint")
    return endpoint


def _repo_key(value: object) -> str:
    key = _text(value, "repo_key")
    if (key != key.lower() or key.startswith("/") or key.endswith(("/", ".git"))
            or "://" in key or key.count("/") < 2 or any(char.isspace() for char in key)):
        raise ValueError("invalid repo_key")
    return key


def parse_policy(value: object) -> dict:
    """Validate and normalize one schema-v1 policy without doing I/O."""
    if isinstance(value, (str, bytes, bytearray)):
        value = json.loads(value)
    policy = _schema(value, "policy")
    _exact_fields(policy, _POLICY_FIELDS, "policy")
    if policy.get("mode") != POLICY_MODE:
        raise ValueError("invalid policy mode")
    for field in ("policy_generation", "repo_key", "repo_slug", "account_fingerprint",
                  "team_id", "team_name_at_confirmation"):
        policy[field] = _text(policy.get(field), field)
    policy["repo_key"] = _repo_key(policy["repo_key"])
    policy["endpoint"] = _endpoint(policy.get("endpoint"))
    if not _FINGERPRINT_RE.fullmatch(policy["account_fingerprint"]):
        raise ValueError("invalid account_fingerprint")
    policy["enabled_at"] = _timestamp(policy.get("enabled_at"), "enabled_at")
    if type(policy.get("include_existing")) is not bool:
        raise ValueError("invalid include_existing")
    baseline = policy.get("baseline_revision_ids")
    if not isinstance(baseline, list) or len(baseline) > store.MAX_ENTRIES:
        raise ValueError("invalid baseline_revision_ids")
    baseline = [_text(item, "baseline_revision_id") for item in baseline]
    if len(set(baseline)) != len(baseline):
        raise ValueError("duplicate baseline_revision_ids")
    policy["baseline_revision_ids"] = baseline
    paused = policy.get("paused_reason")
    if paused is not None and paused not in ERROR_CODES:
        raise ValueError("invalid paused_reason")
    return policy


def parse_intent(value: object) -> dict:
    intent = _schema(value, "intent")
    _exact_fields(intent, _INTENT_FIELDS, "intent")
    for field in ("idempotency_key", "policy_generation", "decision_id", "revision_id",
                  "repo_key", "account_fingerprint", "team_id"):
        intent[field] = _text(intent.get(field), field)
    intent["repo_key"] = _repo_key(intent["repo_key"])
    intent["repo_path"] = _text(intent.get("repo_path"), "repo_path", maximum=_MAX_REPO_PATH)
    if not store.is_sane_repo(intent["repo_path"]):
        raise ValueError("invalid repo_path")
    intent["endpoint"] = _endpoint(intent.get("endpoint"))
    if not _FINGERPRINT_RE.fullmatch(intent["account_fingerprint"]):
        raise ValueError("invalid account_fingerprint")
    intent["queued_at"] = _timestamp(intent.get("queued_at"), "queued_at")
    if type(intent.get("attempts")) is not int or not 0 <= intent["attempts"] <= 1_000_000:
        raise ValueError("invalid attempts")
    code = intent.get("last_error_code")
    error_class = intent.get("last_error_class")
    diagnostic = intent.get("diagnostic_id")
    if code is not None and code not in ERROR_CODES:
        raise ValueError("invalid last_error_code")
    if error_class is not None and error_class not in ERROR_CLASSES:
        raise ValueError("invalid last_error_class")
    if diagnostic is not None and not isinstance(diagnostic, str):
        raise ValueError("invalid diagnostic_id")
    if diagnostic is not None and not _DIAGNOSTIC_RE.fullmatch(diagnostic):
        raise ValueError("invalid diagnostic_id")
    if (code is None) != (error_class is None):
        raise ValueError("incomplete persisted error")
    if code is None and diagnostic is not None:
        raise ValueError("diagnostic_id without persisted error")
    return intent


def parse_receipt(value: object) -> dict:
    receipt = _schema(value, "receipt")
    _exact_fields(receipt, _RECEIPT_FIELDS, "receipt")
    for field in ("policy_generation", "endpoint", "account_fingerprint", "repo_key",
                  "team_id", "decision_id", "revision_id"):
        receipt[field] = _text(
            receipt.get(field), field, maximum=_MAX_ENDPOINT if field == "endpoint" else _MAX_TOKEN)
    receipt["repo_key"] = _repo_key(receipt["repo_key"])
    if not _FINGERPRINT_RE.fullmatch(receipt["account_fingerprint"]):
        raise ValueError("invalid account_fingerprint")
    receipt["endpoint"] = _endpoint(receipt["endpoint"])
    if receipt.get("state") not in RECEIPT_STATES:
        raise ValueError("invalid receipt state")
    receipt["candidate_id"] = _nullable_text(receipt.get("candidate_id"), "candidate_id")
    reason = receipt.get("reason")
    if reason is not None and reason not in ERROR_CODES:
        raise ValueError("invalid receipt reason")
    state = receipt["state"]
    if (state in {"submitted", "already_pending"}) != (receipt["candidate_id"] is not None):
        raise ValueError("candidate_id does not match receipt state")
    if (state == "attention") != (reason is not None):
        raise ValueError("reason does not match receipt state")
    receipt["recorded_at"] = _timestamp(receipt.get("recorded_at"), "recorded_at")
    return receipt


def parse_attention(value: object) -> dict:
    item = _schema(value, "attention")
    _exact_fields(item, _ATTENTION_FIELDS, "attention")
    for field in ("idempotency_key", "policy_generation", "decision_id", "revision_id",
                  "repo_key", "account_fingerprint", "team_id"):
        item[field] = _text(item.get(field), field)
    item["repo_key"] = _repo_key(item["repo_key"])
    item["repo_path"] = _text(item.get("repo_path"), "repo_path", maximum=_MAX_REPO_PATH)
    if not store.is_sane_repo(item["repo_path"]):
        raise ValueError("invalid repo_path")
    item["endpoint"] = _endpoint(item.get("endpoint"))
    if not _FINGERPRINT_RE.fullmatch(item["account_fingerprint"]):
        raise ValueError("invalid account_fingerprint")
    if item.get("reason") not in ERROR_CODES or item.get("last_error_code") not in ERROR_CODES:
        raise ValueError("invalid attention reason")
    if item.get("last_error_class") not in ERROR_CLASSES:
        raise ValueError("invalid attention error class")
    if item["reason"] != item["last_error_code"]:
        raise ValueError("attention reason mismatch")
    diagnostic = item.get("diagnostic_id")
    if not isinstance(diagnostic, str) or not _DIAGNOSTIC_RE.fullmatch(diagnostic):
        raise ValueError("invalid diagnostic_id")
    item["moved_at"] = _timestamp(item.get("moved_at"), "moved_at")
    return item


def destination_matches(policy: dict, destination: dict) -> bool:
    """Exact V1 binding match.  No URL, repo, account, or team normalization occurs here."""
    fields = ("policy_generation", "endpoint", "account_fingerprint", "repo_key", "team_id")
    return all(
        isinstance(policy.get(field), str)
        and policy.get(field) != ""
        and policy.get(field) == destination.get(field)
        for field in fields
    )


def receipt_key(record: dict) -> tuple[str, ...]:
    return tuple(str(record.get(field) or "") for field in (
        "endpoint", "account_fingerprint", "repo_key", "team_id", "decision_id", "revision_id",
    ))


def intent_key(record: dict) -> tuple[str, ...]:
    return (str(record.get("policy_generation") or ""), *receipt_key(record))


def fold_intents(records: list[dict]) -> list[dict]:
    """Keep the first stable idempotency key for each destination/revision identity."""
    folded: dict[tuple[str, ...], dict] = {}
    for raw in records:
        record = parse_intent(raw)
        folded.setdefault(intent_key(record), record)
    return list(folded.values())


def fold_receipts(records: list[dict]) -> dict[tuple[str, ...], dict]:
    """Latest receipt for each exact destination/revision identity."""
    folded: dict[tuple[str, ...], dict] = {}
    for raw in records:
        receipt = parse_receipt(raw)
        key = receipt_key(receipt)
        if key in folded:
            del folded[key]
        folded[key] = receipt
    return folded


def activation_baseline(entries: list[dict], *, include_existing: bool) -> list[str]:
    """Current local decision revisions excluded by a future-only activation."""
    if include_existing:
        return []
    result = []
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "decision":
            continue
        revision = revisions.current_revision(entry)
        revision_id = (revision or {}).get("revision_id") or entry.get("current_revision_id")
        if isinstance(revision_id, str) and revision_id and revision_id not in result:
            result.append(revision_id)
    return result[:store.MAX_ENTRIES]


def build_policy(*, repo_path: str, repo_key: str, endpoint: str,
                 account_fingerprint: str, team_id: str, team_name: str,
                 entries: list[dict], include_existing: bool = False,
                 now: str | None = None, policy_generation: str | None = None) -> dict:
    """Build a validated policy after a separate caller has obtained human confirmation."""
    policy = {
        "schema_version": SCHEMA_VERSION,
        "mode": POLICY_MODE,
        "policy_generation": policy_generation or str(uuid.uuid4()),
        "repo_key": repo_key,
        "repo_slug": store.repo_slug(repo_path),
        "endpoint": endpoint,
        "account_fingerprint": account_fingerprint,
        "team_id": team_id,
        "team_name_at_confirmation": team_name,
        "enabled_at": now or datetime.now(timezone.utc).isoformat(),
        "include_existing": include_existing,
        "baseline_revision_ids": activation_baseline(
            entries, include_existing=include_existing),
        "paused_reason": None,
    }
    return parse_policy(policy)


def baseline_receipts(policy: dict, entries: list[dict], *, now: str | None = None) -> list[dict]:
    """Receipt rows that make a future-only activation durable and independently foldable."""
    policy = parse_policy(policy)
    recorded_at = now or policy["enabled_at"]
    decision_by_revision = {}
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "decision":
            continue
        revision = revisions.current_revision(entry)
        revision_id = (revision or {}).get("revision_id") if isinstance(revision, dict) else None
        if revision_id in policy["baseline_revision_ids"] and isinstance(entry.get("id"), str):
            decision_by_revision[revision_id] = entry["id"]
    return [parse_receipt({
        "schema_version": SCHEMA_VERSION,
        "policy_generation": policy["policy_generation"],
        "endpoint": policy["endpoint"],
        "account_fingerprint": policy["account_fingerprint"],
        "repo_key": policy["repo_key"],
        "team_id": policy["team_id"],
        "decision_id": decision_by_revision[revision_id],
        "revision_id": revision_id,
        "state": "baseline",
        "candidate_id": None,
        "reason": None,
        "recorded_at": recorded_at,
    }) for revision_id in policy["baseline_revision_ids"] if revision_id in decision_by_revision]


def eligibility(entry: object, policy: dict, receipts: dict[tuple[str, ...], dict] | list[dict],
                *, repo_key: str | None, is_global: bool) -> Eligibility:
    """Pure strict automatic-sharing eligibility; manual shareability is intentionally broader."""
    if is_global or not repo_key:
        return Eligibility(False, "global_decision")
    if repo_key != policy.get("repo_key"):
        return Eligibility(False, "repo_mismatch")
    if not isinstance(entry, dict) or entry.get("type") != "decision":
        return Eligibility(False, "ineligible_revision")
    decision_id = entry.get("id") if isinstance(entry.get("id"), str) else ""
    revision = revisions.current_revision(entry)
    revision_id = (revision or {}).get("revision_id") if isinstance(revision, dict) else ""
    explicitly_human_approved = (
        entry.get("approved_by") == "human" or (revision or {}).get("source") == "human"
    )
    if (store.entry_status(entry) != "approved" or not explicitly_human_approved
            or not decision_id or not revision_id):
        return Eligibility(False, "ineligible_revision", decision_id, revision_id or "")
    if revision_id in set(policy.get("baseline_revision_ids") or []):
        return Eligibility(False, "baseline_revision", decision_id, revision_id)
    index = fold_receipts(receipts) if isinstance(receipts, list) else receipts
    probe = {**policy, "decision_id": decision_id, "revision_id": revision_id}
    if receipt_key(probe) in index:
        return Eligibility(False, "duplicate_receipt", decision_id, revision_id)
    return Eligibility(True, "none", decision_id, revision_id)


def _read_json_list(path: Path, kind: str, parser, *, cap: int) -> list[dict]:
    try:
        if path.stat().st_size > _MAX_RECORD_BYTES * cap:
            raise _data_error(kind)
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        raise _data_error(kind) from exc
    try:
        value = json.loads(raw)
        if not isinstance(value, list) or len(value) > cap:
            raise ValueError("not a list")
        return [parser(item) for item in value]
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise _data_error(kind) from exc


def load_policy(repo_path: str) -> dict | None:
    path = policy_path(repo_path)
    try:
        if path.stat().st_size > _MAX_POLICY_BYTES:
            raise _data_error("proposal policy")
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as exc:
        raise _data_error("proposal policy") from exc
    try:
        policy = parse_policy(raw)
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise _data_error("proposal policy") from exc
    if policy["repo_slug"] != store.repo_slug(repo_path):
        raise _data_error("proposal policy")
    return policy


def save_policy(repo_path: str, policy: dict) -> None:
    """Strict locked replacement; a corrupt existing policy is never silently healed."""
    normalized = parse_policy(policy)
    if normalized["repo_slug"] != store.repo_slug(repo_path):
        raise ValueError("policy repo_slug does not match repository")
    started = time.monotonic_ns()
    try:
        with _sidecar_lock(policy_lock_path(repo_path)):
            if policy_path(repo_path).exists():
                load_policy(repo_path)
            store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
            store.atomic_write(policy_path(repo_path), json.dumps(normalized, indent=2))
    except SidecarDataError as exc:
        outcome = OperationOutcome("refused", "validation_error", exc.diagnostic_id)
        _emit("policyChange", outcome, started)
        raise
    _emit("policyChange", OperationOutcome("success", "none"), started)


def read_outbox() -> list[dict]:
    return _read_json_list(
        proposal_outbox_path(), "proposal outbox", parse_intent, cap=OUTBOX_CAP)


def read_attention() -> list[dict]:
    return _read_json_list(
        proposal_attention_path(), "proposal attention", parse_attention, cap=ATTENTION_CAP)


def _write_json_list(path: Path, records: list[dict]) -> None:
    store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    store.atomic_write(path, json.dumps(records, indent=2, ensure_ascii=False))


def enqueue_intent(intent: dict, *, blocking: bool = False) -> OperationOutcome:
    """Append one intent locally, returning immediately when the dedicated lock is busy."""
    started = time.monotonic_ns()
    try:
        normalized = parse_intent(intent)
    except (ValueError, TypeError):
        outcome = OperationOutcome("failure", "validation_error", _diagnostic_id())
        _emit("enqueue", outcome, started)
        return outcome
    try:
        with _sidecar_lock(proposal_outbox_lock_path(), blocking=blocking):
            loaded = read_outbox()
            folded = fold_intents(loaded)
            if any(intent_key(row) == intent_key(normalized) for row in folded):
                outcome = OperationOutcome("no_op", "duplicate_receipt")
            elif len(folded) >= OUTBOX_CAP:
                outcome = OperationOutcome("failure", "validation_error", _diagnostic_id())
            else:
                folded.append(normalized)
                _write_json_list(proposal_outbox_path(), folded)
                outcome = OperationOutcome("queued", "none")
    except BlockingIOError:
        outcome = OperationOutcome("no_op", "lock_busy")
    except SidecarDataError as exc:
        outcome = OperationOutcome("failure", "corrupt_queue", exc.diagnostic_id)
    except OSError:
        outcome = OperationOutcome("failure", "validation_error", _diagnostic_id())
    _emit("enqueue", outcome, started)
    return outcome


def remove_intents(completed: list[dict]) -> int:
    """Remove exact rows after delivery, re-reading under lock to preserve racing enqueues."""
    completed_keys = {(intent_key(row), str(row.get("idempotency_key") or "")) for row in completed}
    with _sidecar_lock(proposal_outbox_lock_path()):
        current = read_outbox()
        kept = [row for row in current
                if (intent_key(row), str(row.get("idempotency_key") or "")) not in completed_keys]
        if len(kept) != len(current):
            _write_json_list(proposal_outbox_path(), kept)
        return len(current) - len(kept)


def read_receipts() -> list[dict]:
    path = proposal_receipts_path()
    try:
        if path.stat().st_size > _MAX_RECORD_BYTES * RECEIPT_LOG_CAP:
            raise _data_error("proposal receipts")
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as exc:
        raise _data_error("proposal receipts") from exc
    records = []
    try:
        for line in raw.splitlines():
            if not line.strip():
                continue
            records.append(parse_receipt(json.loads(line)))
            if len(records) > RECEIPT_LOG_CAP:
                raise ValueError("receipt log exceeds cap")
    except (json.JSONDecodeError, ValueError, TypeError) as exc:
        raise _data_error("proposal receipts") from exc
    return records


def append_receipt(receipt: dict) -> None:
    """Append and compact under one receipt lock so a racing record cannot be lost."""
    normalized = parse_receipt(receipt)
    with _sidecar_lock(proposal_receipts_lock_path()):
        records = read_receipts()
        records.append(normalized)
        path = proposal_receipts_path()
        store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        if len(records) > RECEIPT_LOG_CAP:
            records = list(fold_receipts(records).values())[-RECEIPT_LOG_CAP:]
            text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in records)
            store.atomic_write(path, text)
            return
        encoded = json.dumps(normalized, ensure_ascii=False) + "\n"
        if not path.exists() or path.stat().st_size == 0:
            store.atomic_write(path, encoded)
            return
        with open(path, "rb") as handle:
            handle.seek(-1, 2)
            needs_newline = handle.read(1) != b"\n"
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(("\n" if needs_newline else "") + encoded)


def append_attention(item: dict) -> None:
    normalized = parse_attention(item)
    with _sidecar_lock(proposal_attention_lock_path()):
        records = read_attention()
        key = (intent_key(normalized), normalized["idempotency_key"])
        records = [row for row in records
                   if (intent_key(row), row["idempotency_key"]) != key]
        if len(records) >= ATTENTION_CAP:
            raise RuntimeError("proposal attention queue is full")
        records.append(normalized)
        _write_json_list(proposal_attention_path(), records)


def status_snapshot(policy: dict | None, intents: list[dict], receipts: list[dict],
                    attention: list[dict], *, uploading: bool = False) -> dict:
    """Pure status counts.  A queued local intent is never described as shared."""
    state = "disabled"
    if policy is not None:
        state = "paused" if policy.get("paused_reason") else "active"
    counts = {name: 0 for name in RECEIPT_STATES}
    for receipt in fold_receipts(receipts).values():
        counts[receipt["state"]] += 1
    return {
        "policy": state,
        "paused_reason": (policy or {}).get("paused_reason"),
        "queued": len(fold_intents(intents)),
        "uploading": bool(uploading),
        "pending_lead_review": counts["submitted"] + counts["already_pending"],
        "already_current": counts["unchanged"],
        "attention": len(attention),
        "baseline": counts["baseline"],
    }


def render_status(snapshot: dict) -> str:
    """Concise human status for CLI/MCP surfaces added in D5."""
    parts = [f"policy {snapshot.get('policy', 'disabled')}"]
    if snapshot.get("uploading"):
        parts.append("uploading")
    for key, label in (("queued", "queued"), ("pending_lead_review", "pending lead review"),
                       ("already_current", "already current"), ("attention", "attention")):
        count = snapshot.get(key, 0)
        if isinstance(count, int) and count:
            parts.append(f"{count} {label}")
    return "; ".join(parts)
