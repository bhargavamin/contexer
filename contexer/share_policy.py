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
import os
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

from contexer import decision_observability, remote, repo_key, revisions, share, sidecars, store
from contexer.config import Profile, load_profile


SCHEMA_VERSION = 1
POLICY_MODE = "propose_approved"
OUTBOX_CAP = store.MAX_ENTRIES
ATTENTION_CAP = store.MAX_ENTRIES
RECEIPT_LOG_CAP = 2_000
_DRAINER_LEASE_SECONDS = 15 * 60

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


@dataclass(frozen=True)
class ScanOutcome:
    result: str
    reason_code: str
    scanned: int = 0
    queued: int = 0
    skipped: int = 0


def _diagnostic_id() -> str:
    return "diag_" + "".join(secrets.choice(_DIAGNOSTIC_ALPHABET) for _ in range(16))


def _data_error(kind: str) -> SidecarDataError:
    return SidecarDataError(kind, _diagnostic_id())


def _emit(operation: str, outcome: OperationOutcome, started_ns: int, *,
          intent: dict | None = None, queue_depth: int | None = None,
          candidate_id: str | None = None, replayed: bool | None = None) -> None:
    error_class = {
        "lock_busy": "lock",
        "corrupt_queue": "validation",
        "validation_error": "validation",
        "account_mismatch": "capability",
        "unsupported_protocol": "capability",
        "policy_mismatch": "validation",
        "repo_mismatch": "validation",
        "team_mismatch": "authorization",
        "not_member": "authorization",
        "not_authorized": "authorization",
        "stale_head": "conflict",
        "stale_intent": "conflict",
        "rate_limited": "rate_limit",
        "quota_exceeded": "rate_limit",
        "trial_expired": "authorization",
        "transient_error": "transport",
        "transport_error": "transport",
    }.get(outcome.reason_code, "none")
    decision_observability.emit_decision_operation(
        operation,
        result=outcome.result,
        reason_code=outcome.reason_code,
        error_class=error_class,
        started_ns=started_ns,
        diagnostic_id=outcome.diagnostic_id,
        team_id=(intent or {}).get("team_id"),
        decision_id=(intent or {}).get("decision_id"),
        policy_generation=(intent or {}).get("policy_generation"),
        idempotency_key=(intent or {}).get("idempotency_key"),
        candidate_id=candidate_id,
        attempt=(intent or {}).get("attempts"),
        queue_depth=queue_depth,
        replayed=replayed,
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
_DETACHED_DRAINER_GUARD = threading.Lock()
_DETACHED_DRAINER_RUNNING = False


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


def _read_drainer_claim(path: Path) -> dict | None:
    try:
        if path.stat().st_size > 1_024:
            return {"invalid": True, "renewed_at": path.stat().st_mtime}
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError):
        return {"invalid": True, "renewed_at": path.stat().st_mtime}
    if not raw.strip():
        return None
    try:
        claim = json.loads(raw)
    except json.JSONDecodeError:
        return {"invalid": True, "renewed_at": path.stat().st_mtime}
    if (not isinstance(claim, dict) or set(claim) != {"owner", "renewed_at"}
            or not isinstance(claim.get("owner"), str)
            or not isinstance(claim.get("renewed_at"), (int, float))
            or isinstance(claim.get("renewed_at"), bool)):
        return {"invalid": True, "renewed_at": path.stat().st_mtime}
    try:
        canonical_owner = str(uuid.UUID(claim["owner"]))
    except (ValueError, AttributeError):
        return {"invalid": True, "renewed_at": path.stat().st_mtime}
    if claim["owner"].lower() != canonical_owner:
        return {"invalid": True, "renewed_at": path.stat().st_mtime}
    if float(claim["renewed_at"]) > time.time() + 60:
        return {"invalid": True, "renewed_at": path.stat().st_mtime}
    return claim


def _write_drainer_claim(path: Path, claim: dict | None) -> None:
    """Rewrite the lease inode while its short coordination flock is held."""
    store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    with open(path, "r+", encoding="utf-8") as handle:
        handle.seek(0)
        handle.truncate()
        if claim is not None:
            handle.write(json.dumps(claim, separators=(",", ":")))
        handle.flush()
        os.fsync(handle.fileno())


def _active_drainer_claim(claim: dict | None, now: float) -> bool:
    if not claim:
        return False
    renewed = claim.get("renewed_at")
    if not isinstance(renewed, (int, float)) or isinstance(renewed, bool):
        return False
    return now - float(renewed) < _DRAINER_LEASE_SECONDS


@contextlib.contextmanager
def proposal_drainer_lock(*, blocking: bool = False):
    """Claim uploader ownership without holding a file lock across network work.

    The durable lease is written while a short non-blocking flock is held, then the flock is
    released before yielding.  Other processes observe the unexpired opaque claim and refuse to
    upload.  A crashed worker self-heals after the bounded lease window.  This is deliberately a
    lease context rather than a held sidecar lock: preview and submission must run with no store or
    sidecar lock held.
    """
    path = proposal_drainer_lock_path()
    owner = str(uuid.uuid4())
    now = time.time()
    with _sidecar_lock(path, blocking=blocking):
        if _active_drainer_claim(_read_drainer_claim(path), now):
            raise BlockingIOError("proposal drainer already claimed")
        _write_drainer_claim(path, {"owner": owner, "renewed_at": now})
    try:
        yield owner
    finally:
        try:
            with _sidecar_lock(path, blocking=True):
                claim = _read_drainer_claim(path)
                if isinstance(claim, dict) and claim.get("owner") == owner:
                    _write_drainer_claim(path, None)
        except (OSError, BlockingIOError):
            pass


def _renew_drainer_claim(owner: str) -> bool:
    path = proposal_drainer_lock_path()
    try:
        with _sidecar_lock(path, blocking=False):
            claim = _read_drainer_claim(path)
            if not isinstance(claim, dict) or claim.get("owner") != owner:
                return False
            _write_drainer_claim(path, {"owner": owner, "renewed_at": time.time()})
            return True
    except (OSError, BlockingIOError):
        return False


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
    receipt = index.get(receipt_key(probe))
    if receipt is not None and receipt.get("state") in TERMINAL_RECEIPT_STATES:
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
    normalized = None
    queue_depth = None
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
            queue_depth = len(folded)
            if any(intent_key(row) == intent_key(normalized) for row in folded):
                outcome = OperationOutcome("no_op", "duplicate_receipt")
            elif len(folded) >= OUTBOX_CAP:
                outcome = OperationOutcome("failure", "validation_error", _diagnostic_id())
            else:
                folded.append(normalized)
                _write_json_list(proposal_outbox_path(), folded)
                queue_depth = len(folded)
                outcome = OperationOutcome("queued", "none")
    except BlockingIOError:
        outcome = OperationOutcome("no_op", "lock_busy")
    except SidecarDataError as exc:
        outcome = OperationOutcome("failure", "corrupt_queue", exc.diagnostic_id)
    except OSError:
        outcome = OperationOutcome("failure", "validation_error", _diagnostic_id())
    _emit("enqueue", outcome, started, intent=normalized, queue_depth=queue_depth)
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


def _record_intent_retry(intent: dict, reason: str, error_class: str,
                         diagnostic_id: str) -> None:
    """Persist one closed retry reason without copying transport or SDK error text."""
    if reason not in ERROR_CODES or error_class not in ERROR_CLASSES:
        raise ValueError("invalid persisted retry")
    key = (intent_key(intent), intent["idempotency_key"])
    with _sidecar_lock(proposal_outbox_lock_path()):
        current = read_outbox()
        updated = []
        for row in current:
            if (intent_key(row), row["idempotency_key"]) != key:
                updated.append(row)
                continue
            updated.append(parse_intent({
                **row,
                "attempts": row["attempts"] + 1,
                "last_error_code": reason,
                "last_error_class": error_class,
                "diagnostic_id": diagnostic_id,
            }))
        if updated != current:
            _write_json_list(proposal_outbox_path(), updated)


def _pause_policy(repo_path: str, generation: str, reason: str) -> bool:
    """CAS-like pause of the policy generation that produced an unsafe destination intent."""
    if reason not in ERROR_CODES:
        raise ValueError("invalid pause reason")
    with _sidecar_lock(policy_lock_path(repo_path)):
        policy = load_policy(repo_path)
        if policy is None or policy["policy_generation"] != generation:
            return False
        if policy.get("paused_reason") == reason:
            return True
        normalized = parse_policy({**policy, "paused_reason": reason})
        store.atomic_write(policy_path(repo_path), json.dumps(normalized, indent=2))
        return True


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


def append_receipt(receipt: dict, *, blocking: bool = True) -> None:
    """Append and compact under one receipt lock so a racing record cannot be lost."""
    normalized = parse_receipt(receipt)
    with _sidecar_lock(proposal_receipts_lock_path(), blocking=blocking):
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


def build_intent(policy: dict, repo_path: str, entry: dict, *,
                 now: str | None = None, idempotency_key: str | None = None) -> dict:
    """Build the stable local intent shape; decision prose never enters this sidecar."""
    policy = parse_policy(policy)
    revision = revisions.current_revision(entry)
    return parse_intent({
        "schema_version": SCHEMA_VERSION,
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
        "policy_generation": policy["policy_generation"],
        "decision_id": entry.get("id"),
        "revision_id": (revision or {}).get("revision_id"),
        "repo_path": store.canonical_store_key(repo_path),
        "repo_key": policy["repo_key"],
        "endpoint": policy["endpoint"],
        "account_fingerprint": policy["account_fingerprint"],
        "team_id": policy["team_id"],
        "queued_at": now or datetime.now(timezone.utc).isoformat(),
        "attempts": 0,
        "last_error_code": None,
        "last_error_class": None,
        "diagnostic_id": None,
    })


def queued_receipt(intent: dict) -> dict:
    """The local receipt paired with a durable queued intent."""
    intent = parse_intent(intent)
    return parse_receipt({
        "schema_version": SCHEMA_VERSION,
        "policy_generation": intent["policy_generation"],
        "endpoint": intent["endpoint"],
        "account_fingerprint": intent["account_fingerprint"],
        "repo_key": intent["repo_key"],
        "team_id": intent["team_id"],
        "decision_id": intent["decision_id"],
        "revision_id": intent["revision_id"],
        "state": "queued",
        "candidate_id": None,
        "reason": None,
        "recorded_at": intent["queued_at"],
    })


def _emit_scan(outcome: ScanOutcome, started_ns: int) -> None:
    _emit(
        "scan",
        OperationOutcome(outcome.result, outcome.reason_code),
        started_ns,
    )


def _finish_scan(outcome: ScanOutcome, started_ns: int) -> ScanOutcome:
    _emit_scan(outcome, started_ns)
    if outcome.result == "queued" and outcome.queued:
        # Thread creation is the only prompt-path work here. Capability discovery, preview, and
        # submission happen in the daemon after this committed local append has returned.
        start_detached_drainer()
    return outcome


def scan_and_enqueue(repo_path: str, *, decision_ids: set[str] | None = None,
                     is_global: bool = False) -> ScanOutcome:
    """Bounded, local-only backstop that durably queues every newly eligible revision.

    Every sidecar lock is non-blocking on this path.  Editor/MCP mutations are already
    committed when this runs, so a busy or unavailable proposal lock loses promptness only;
    a later lifecycle scan sees the same revision and retries.  No network primitive is
    imported or called here, and no decision-store lock is acquired.
    """
    started = time.monotonic_ns()
    try:
        if is_global or not store.is_sane_repo(repo_path):
            return _finish_scan(ScanOutcome("skipped", "global_decision"), started)
        try:
            with _sidecar_lock(policy_lock_path(repo_path), blocking=False):
                policy = load_policy(repo_path)
        except BlockingIOError:
            return _finish_scan(ScanOutcome("no_op", "lock_busy"), started)
        if policy is None or policy.get("paused_reason"):
            return _finish_scan(ScanOutcome("skipped", "policy_disabled"), started)

        origin = store.run_git(repo_path, "remote", "get-url", "origin")
        current_repo_key = repo_key.canonical_repo_key(origin)
        if not current_repo_key or current_repo_key != policy["repo_key"]:
            return _finish_scan(ScanOutcome("failure", "validation_error"), started)
        try:
            with _sidecar_lock(proposal_receipts_lock_path(), blocking=False):
                receipts = fold_receipts(read_receipts())
        except BlockingIOError:
            return _finish_scan(ScanOutcome("no_op", "lock_busy"), started)

        entries = store.load(repo_path).get("entries", [])[:store.MAX_ENTRIES]
        selected = None
        if decision_ids is not None:
            selected = set()
            decision_entries = [
                entry for entry in entries
                if isinstance(entry, dict) and entry.get("type") == "decision"
            ]
            for decision_id in decision_ids:
                matched = store.entry_by_id(decision_entries, decision_id)
                if matched is not None:
                    selected.add(matched["id"])
        scanned = queued = skipped = 0
        only_reason = None
        saw_lock_busy = False
        saw_failure = None
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("type") != "decision":
                continue
            if selected is not None and entry.get("id") not in selected:
                continue
            scanned += 1
            candidate = eligibility(
                entry, policy, receipts, repo_key=current_repo_key, is_global=False)
            if not candidate.eligible:
                skipped += 1
                only_reason = (candidate.reason_code if only_reason in (None, candidate.reason_code)
                               else "none")
                continue

            intent = build_intent(policy, repo_path, entry)
            enqueue = enqueue_intent(intent, blocking=False)
            if enqueue.result == "queued":
                queued += 1
            elif enqueue.reason_code == "duplicate_receipt":
                skipped += 1
                existing_receipt = receipts.get(receipt_key(intent))
                if (existing_receipt is not None
                        and existing_receipt.get("state") == "queued"):
                    only_reason = ("duplicate_receipt" if only_reason in (
                        None, "duplicate_receipt") else "none")
                    continue
            elif enqueue.reason_code == "lock_busy":
                saw_lock_busy = True
                skipped += 1
                continue
            else:
                saw_failure = enqueue.reason_code
                skipped += 1
                continue

            # A scanner that finds the queued row but not its receipt heals a crash between
            # those writes. Duplicate queued receipts are harmless and fold to the latest row.
            try:
                receipt = queued_receipt(intent)
                append_receipt(receipt, blocking=False)
                receipts[receipt_key(intent)] = receipt
            except BlockingIOError:
                # The intent is durable already. A later scan/drain heals the receipt without
                # making this committed mutation wait for another process.
                saw_lock_busy = True
            except SidecarDataError:
                saw_failure = "corrupt_queue"
            except (OSError, ValueError, TypeError):
                saw_failure = "validation_error"

        if saw_failure:
            return _finish_scan(
                ScanOutcome("failure", saw_failure, scanned, queued, skipped), started)
        if queued:
            return _finish_scan(
                ScanOutcome("queued", "none", scanned, queued, skipped), started)
        if saw_lock_busy:
            return _finish_scan(
                ScanOutcome("no_op", "lock_busy", scanned, queued, skipped), started)
        if scanned == 1 and only_reason == "duplicate_receipt":
            return _finish_scan(
                ScanOutcome("no_op", only_reason, scanned, queued, skipped), started)
        if scanned == 1 and only_reason not in (None, "none"):
            return _finish_scan(
                ScanOutcome("skipped", only_reason, scanned, queued, skipped), started)
        return _finish_scan(ScanOutcome("success", "none", scanned, queued, skipped), started)
    except SidecarDataError:
        return _finish_scan(ScanOutcome("failure", "corrupt_queue"), started)
    except Exception:
        # This wrapper runs after the functional mutation committed. Any unexpected local
        # filesystem/git/schema failure is diagnostic only and must never change that result.
        return _finish_scan(ScanOutcome("failure", "validation_error"), started)


def enqueue_after_local_mutation(repo_path: str, decision_id: str) -> ScanOutcome:
    """Best-effort prompt path for one committed decision; the full scanner is the backstop."""
    return scan_and_enqueue(repo_path, decision_ids={decision_id})


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


_PAUSING_DRAIN_REASONS = frozenset({
    "account_mismatch", "policy_mismatch", "repo_mismatch", "team_mismatch",
    "not_member", "not_authorized", "trial_expired",
})
_SUCCESSFUL_DRAIN_STATES = frozenset({"submitted", "already_pending", "unchanged"})
_CONFLICT_SUBMISSION_STATES = frozenset({"heads_changed", "needs_rebase", "stale_head"})


def _recorded_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _finish_drain(outcome: OperationOutcome, started_ns: int, intent: dict | None, *,
                  queue_depth: int | None = None, candidate_id: str | None = None,
                  replayed: bool | None = None) -> OperationOutcome:
    _emit(
        "drain", outcome, started_ns, intent=intent, queue_depth=queue_depth,
        candidate_id=candidate_id, replayed=replayed,
    )
    return outcome


def _receipt_for_intent(intent: dict, state: str, *, candidate_id: str | None = None,
                        reason: str | None = None) -> dict:
    return parse_receipt({
        "schema_version": SCHEMA_VERSION,
        "policy_generation": intent["policy_generation"],
        "endpoint": intent["endpoint"],
        "account_fingerprint": intent["account_fingerprint"],
        "repo_key": intent["repo_key"],
        "team_id": intent["team_id"],
        "decision_id": intent["decision_id"],
        "revision_id": intent["revision_id"],
        "state": state,
        "candidate_id": candidate_id,
        "reason": reason,
        "recorded_at": _recorded_at(),
    })


def _attention_for_intent(intent: dict, reason: str, error_class: str,
                          diagnostic_id: str) -> dict:
    return parse_attention({
        "schema_version": SCHEMA_VERSION,
        "idempotency_key": intent["idempotency_key"],
        "policy_generation": intent["policy_generation"],
        "decision_id": intent["decision_id"],
        "revision_id": intent["revision_id"],
        "repo_path": intent["repo_path"],
        "repo_key": intent["repo_key"],
        "endpoint": intent["endpoint"],
        "account_fingerprint": intent["account_fingerprint"],
        "team_id": intent["team_id"],
        "reason": reason,
        "moved_at": _recorded_at(),
        "last_error_code": reason,
        "last_error_class": error_class,
        "diagnostic_id": diagnostic_id,
    })


def _move_intent_to_attention(intent: dict, reason: str, error_class: str, *,
                              result: str = "attention",
                              owner: str | None = None) -> OperationOutcome:
    """Durably strand a terminal intent; its receipt always precedes queue removal."""
    if owner is not None and not _renew_drainer_claim(owner):
        return OperationOutcome("no_op", "lock_busy")
    diagnostic_id = _diagnostic_id()
    try:
        append_attention(_attention_for_intent(intent, reason, error_class, diagnostic_id))
        if reason in _PAUSING_DRAIN_REASONS:
            _pause_policy(intent["repo_path"], intent["policy_generation"], reason)
        append_receipt(_receipt_for_intent(intent, "attention", reason=reason))
        remove_intents([intent])
    except SidecarDataError as exc:
        return OperationOutcome("failure", "corrupt_queue", exc.diagnostic_id)
    except (OSError, ValueError, TypeError, RuntimeError):
        return OperationOutcome("failure", "validation_error", diagnostic_id)
    return OperationOutcome(result, reason, diagnostic_id)


def _keep_intent_for_retry(intent: dict, reason: str, error_class: str, *,
                           owner: str | None = None) -> OperationOutcome:
    if owner is not None and not _renew_drainer_claim(owner):
        return OperationOutcome("no_op", "lock_busy")
    diagnostic_id = _diagnostic_id()
    try:
        _record_intent_retry(intent, reason, error_class, diagnostic_id)
    except SidecarDataError as exc:
        return OperationOutcome("failure", "corrupt_queue", exc.diagnostic_id)
    except (OSError, ValueError, TypeError):
        return OperationOutcome("failure", "validation_error", diagnostic_id)
    return OperationOutcome("retry", reason, diagnostic_id)


def _current_terminal_receipt(intent: dict) -> dict | None:
    with _sidecar_lock(proposal_receipts_lock_path()):
        receipt = fold_receipts(read_receipts()).get(receipt_key(intent))
    if receipt is not None and receipt.get("state") in TERMINAL_RECEIPT_STATES:
        return receipt
    return None


def _load_bound_policy(intent: dict) -> dict | None:
    with _sidecar_lock(policy_lock_path(intent["repo_path"])):
        policy = load_policy(intent["repo_path"])
    if policy is None or not destination_matches(policy, intent) or policy.get("paused_reason"):
        return None
    return policy


def _fresh_local_projection(intent: dict, policy: dict, redact_on: bool) -> tuple[dict | None, str]:
    """Re-read and strictly bind the current approved revision before any network call."""
    origin = store.run_git(intent["repo_path"], "remote", "get-url", "origin")
    current_repo_key = repo_key.canonical_repo_key(origin)
    if current_repo_key != intent["repo_key"] or current_repo_key != policy["repo_key"]:
        return None, "repo_mismatch"
    entries = store.load(intent["repo_path"]).get("entries", [])[:store.MAX_ENTRIES]
    entry = next((row for row in entries if isinstance(row, dict)
                  and row.get("id") == intent["decision_id"]), None)
    if entry is None:
        return None, "stale_intent"
    eligible = eligibility(
        entry, policy, {}, repo_key=current_repo_key, is_global=False)
    if (not eligible.eligible or eligible.decision_id != intent["decision_id"]
            or eligible.revision_id != intent["revision_id"]):
        return None, "stale_intent"
    decision = store.get_shareable(
        intent["repo_path"], intent["decision_id"], redact_on=redact_on)
    if (decision is None or decision.get("id") != intent["decision_id"]
            or decision.get("revision_id") != intent["revision_id"]
            or decision.get("status") != "approved"):
        return None, "stale_intent"
    return decision, "none"


def _remote_error_outcome(intent: dict, exc: remote.RemoteStoreError, *,
                          owner: str) -> OperationOutcome:
    if not share.is_transient_reconciliation_refusal(exc):
        return _move_intent_to_attention(
            intent, "validation_error", "validation", result="failure", owner=owner)
    if isinstance(exc, remote.RemoteRateLimitError):
        return _keep_intent_for_retry(
            intent, "rate_limited", "rate_limit", owner=owner)
    if isinstance(exc, remote.RemoteUnavailableError):
        return _keep_intent_for_retry(
            intent, "transport_error", "transport", owner=owner)
    return _keep_intent_for_retry(
        intent, "transient_error", "transport", owner=owner)


def _submit_with_fresh_preview(
        intent: dict, decision: dict, remote_store: remote.RemoteStore,
        target: remote.RemoteTeam, *, redact_on: bool,
        owner: str) -> tuple[remote.TeamSubmissionResult | None, OperationOutcome | None]:
    for attempt in range(2):
        proposed = share.atomic_decision_kwargs(
            decision, intent["repo_key"], redact_on=redact_on)
        if not _renew_drainer_claim(owner):
            return None, OperationOutcome("no_op", "lock_busy")
        try:
            preview = remote_store.preview_decision_reconciliation(
                intent["decision_id"], intent["team_id"],
                **proposed,
            )
        except remote.RemoteStoreError as exc:
            return None, _remote_error_outcome(intent, exc, owner=owner)
        if not _renew_drainer_claim(owner):
            return None, OperationOutcome("no_op", "lock_busy")
        if preview.team.id != intent["team_id"]:
            return None, _move_intent_to_attention(
                intent, "team_mismatch", "authorization", owner=owner)
        # Preview is a network boundary during which a local review action can supersede or
        # withdraw this revision. Re-read before sending; if projection metadata changed without
        # a revision change, obtain a new preview for that exact body rather than submitting a
        # payload the server never previewed.
        current_policy = _load_bound_policy(intent)
        if current_policy is None:
            return None, _move_intent_to_attention(
                intent, "policy_mismatch", "validation", owner=owner)
        current, local_reason = _fresh_local_projection(
            intent, current_policy, redact_on)
        if current is None:
            return None, _move_intent_to_attention(
                intent, local_reason,
                "validation" if local_reason == "repo_mismatch" else "conflict",
                owner=owner)
        current_proposed = share.atomic_decision_kwargs(
            current, intent["repo_key"], redact_on=redact_on)
        if current_proposed != proposed:
            if attempt == 1:
                return None, _move_intent_to_attention(
                    intent, "stale_intent", "conflict", owner=owner)
            decision = current
            continue
        operation = share.atomic_reconciliation_operation(
            current, intent["repo_key"], target, preview, intent["idempotency_key"],
            redact_on=redact_on,
        )
        if not _renew_drainer_claim(owner):
            return None, OperationOutcome("no_op", "lock_busy")
        if _load_bound_policy(intent) is None:
            return None, _move_intent_to_attention(
                intent, "policy_mismatch", "validation", owner=owner)
        try:
            result = share.call_atomic_submission(remote_store, operation)
        except remote.RemoteStoreError as exc:
            return None, _remote_error_outcome(intent, exc, owner=owner)
        if not _renew_drainer_claim(owner):
            return None, OperationOutcome("no_op", "lock_busy")
        if result.team.id != intent["team_id"]:
            return None, _move_intent_to_attention(
                intent, "team_mismatch", "authorization", owner=owner)
        if result.status not in _CONFLICT_SUBMISSION_STATES:
            return result, None
        if attempt == 1:
            return None, _move_intent_to_attention(
                intent, "stale_head", "conflict", result="conflict", owner=owner)
    raise AssertionError("bounded reconciliation retry exhausted")


def _drain_intent(intent: dict, profile: Profile, owner: str, *,
                  queue_depth: int) -> OperationOutcome:
    started = time.monotonic_ns()
    try:
        terminal = _current_terminal_receipt(intent)
        if terminal is not None:
            remove_intents([intent])
            return _finish_drain(
                OperationOutcome("no_op", "duplicate_receipt"), started, intent,
                queue_depth=queue_depth,
            )
        policy = _load_bound_policy(intent)
        if policy is None:
            outcome = _move_intent_to_attention(
                intent, "policy_mismatch", "validation", owner=owner)
            return _finish_drain(outcome, started, intent, queue_depth=queue_depth)
        if profile.endpoint != intent["endpoint"]:
            outcome = _move_intent_to_attention(
                intent, "policy_mismatch", "validation", owner=owner)
            return _finish_drain(outcome, started, intent, queue_depth=queue_depth)
        decision, local_reason = _fresh_local_projection(
            intent, policy, profile.redact_secrets)
        if decision is None:
            outcome = _move_intent_to_attention(
                intent, local_reason,
                "validation" if local_reason == "repo_mismatch" else "conflict",
                owner=owner)
            return _finish_drain(outcome, started, intent, queue_depth=queue_depth)
        # Pin the credential that will be account-fingerprint checked below. On a transport 401,
        # keep the intent and reconstruct on the next drain; never refresh mid-attempt to a
        # concurrently selected same-endpoint account after validation.
        remote_store = remote.RemoteStore.from_profile(profile, reactive_refresh=False)
        if remote_store is None:
            outcome = _keep_intent_for_retry(
                intent, "transient_error", "transport", owner=owner)
            return _finish_drain(outcome, started, intent, queue_depth=queue_depth)

        if not _renew_drainer_claim(owner):
            return _finish_drain(
                OperationOutcome("no_op", "lock_busy"), started, intent,
                queue_depth=queue_depth,
            )
        try:
            capabilities = remote_store.get_capabilities()
        except remote.RemoteStoreError as exc:
            outcome = _remote_error_outcome(intent, exc, owner=owner)
            return _finish_drain(outcome, started, intent, queue_depth=queue_depth)
        if not _renew_drainer_claim(owner):
            return _finish_drain(
                OperationOutcome("no_op", "lock_busy"), started, intent,
                queue_depth=queue_depth,
            )
        automatic = capabilities.automatic_decision_proposal
        protocol = capabilities.decision_reconciliation
        if (automatic is None or automatic.version != 1 or protocol is None
                or protocol.version < 1 or not protocol.atomic_submit or not protocol.preview):
            return _finish_drain(
                OperationOutcome("refused", "unsupported_protocol"), started, intent,
                queue_depth=queue_depth,
            )
        if capabilities.account_fingerprint != intent["account_fingerprint"]:
            outcome = _move_intent_to_attention(
                intent, "account_mismatch", "capability", owner=owner)
            return _finish_drain(outcome, started, intent, queue_depth=queue_depth)

        if not _renew_drainer_claim(owner):
            return _finish_drain(
                OperationOutcome("no_op", "lock_busy"), started, intent,
                queue_depth=queue_depth,
            )
        try:
            teams = remote_store.list_teams()
        except remote.RemoteStoreError as exc:
            outcome = _remote_error_outcome(intent, exc, owner=owner)
            return _finish_drain(outcome, started, intent, queue_depth=queue_depth)
        if not _renew_drainer_claim(owner):
            return _finish_drain(
                OperationOutcome("no_op", "lock_busy"), started, intent,
                queue_depth=queue_depth,
            )
        target = next((team for team in teams if team.id == intent["team_id"]), None)
        if target is None:
            outcome = _move_intent_to_attention(
                intent, "not_member", "authorization", owner=owner)
            return _finish_drain(outcome, started, intent, queue_depth=queue_depth)

        result, outcome = _submit_with_fresh_preview(
            intent, decision, remote_store, target,
            redact_on=profile.redact_secrets, owner=owner)
        if outcome is not None:
            return _finish_drain(outcome, started, intent, queue_depth=queue_depth)
        assert result is not None
        status = result.status or ""
        if status in _SUCCESSFUL_DRAIN_STATES:
            candidate_id = result.candidate_id if status != "unchanged" else None
            try:
                if not _renew_drainer_claim(owner):
                    return _finish_drain(
                        OperationOutcome("no_op", "lock_busy"), started, intent,
                        queue_depth=queue_depth,
                    )
                append_receipt(_receipt_for_intent(
                    intent, status, candidate_id=candidate_id))
                remove_intents([intent])
            except SidecarDataError as exc:
                outcome = OperationOutcome("failure", "corrupt_queue", exc.diagnostic_id)
            except (OSError, ValueError, TypeError):
                outcome = OperationOutcome("failure", "validation_error", _diagnostic_id())
            else:
                outcome = OperationOutcome(status, "none")
            return _finish_drain(
                outcome, started, intent, queue_depth=queue_depth,
                candidate_id=candidate_id, replayed=result.replayed,
            )
        status_reason = {
            "account_mismatch": ("account_mismatch", "capability", "attention"),
            "policy_mismatch": ("policy_mismatch", "validation", "attention"),
            "repo_mismatch": ("repo_mismatch", "validation", "attention"),
            "team_mismatch": ("team_mismatch", "authorization", "attention"),
            "not_member": ("not_member", "authorization", "attention"),
            "not_authorized": ("not_authorized", "authorization", "attention"),
            "not_authored_by_caller": ("not_authorized", "authorization", "attention"),
            "invalid_team": ("team_mismatch", "authorization", "attention"),
            "quota_exceeded": ("quota_exceeded", "rate_limit", "attention"),
            "trial_expired": ("trial_expired", "authorization", "attention"),
        }.get(status)
        if status_reason is not None:
            reason, error_class, result_name = status_reason
            outcome = _move_intent_to_attention(
                intent, reason, error_class, result=result_name, owner=owner)
        elif status == "rate_limited":
            outcome = _keep_intent_for_retry(
                intent, "rate_limited", "rate_limit", owner=owner)
        elif status == "unsupported_protocol":
            outcome = OperationOutcome("refused", "unsupported_protocol")
        else:
            outcome = _move_intent_to_attention(
                intent, "validation_error", "validation", result="failure", owner=owner)
        return _finish_drain(outcome, started, intent, queue_depth=queue_depth)
    except SidecarDataError as exc:
        return _finish_drain(
            OperationOutcome("failure", "corrupt_queue", exc.diagnostic_id),
            started, intent, queue_depth=queue_depth,
        )
    except Exception:
        return _finish_drain(
            OperationOutcome("failure", "validation_error", _diagnostic_id()),
            started, intent, queue_depth=queue_depth,
        )


def drain_once(profile: Profile | None = None) -> list[OperationOutcome]:
    """Drain one stable queue snapshot under a non-blocking durable uploader lease."""
    started = time.monotonic_ns()
    try:
        with proposal_drainer_lock(blocking=False) as owner:
            try:
                with _sidecar_lock(proposal_outbox_lock_path()):
                    intents = fold_intents(read_outbox())
            except SidecarDataError as exc:
                outcome = OperationOutcome("failure", "corrupt_queue", exc.diagnostic_id)
                _finish_drain(outcome, started, None)
                return [outcome]
            if not intents:
                return []
            try:
                current_profile = profile or load_profile()
            except Exception:
                outcome = OperationOutcome("failure", "validation_error", _diagnostic_id())
                _finish_drain(outcome, started, intents[0], queue_depth=len(intents))
                return [outcome]
            outcomes = []
            for intent in intents:
                outcome = _drain_intent(
                    intent, current_profile, owner, queue_depth=len(intents))
                outcomes.append(outcome)
                if (outcome.result in {"retry", "refused"}
                        or outcome.reason_code in _PAUSING_DRAIN_REASONS | {"lock_busy"}):
                    break
            return outcomes
    except BlockingIOError:
        outcome = OperationOutcome("no_op", "lock_busy")
        _finish_drain(outcome, started, None)
        return [outcome]
    except (OSError, SidecarDataError) as exc:
        diagnostic = exc.diagnostic_id if isinstance(exc, SidecarDataError) else _diagnostic_id()
        outcome = OperationOutcome(
            "failure", "corrupt_queue" if isinstance(exc, SidecarDataError) else "validation_error",
            diagnostic,
        )
        _finish_drain(outcome, started, None)
        return [outcome]


def start_detached_drainer(profile: Profile | None = None) -> bool:
    """Start a daemon one-shot worker without performing network I/O on the caller's path."""
    global _DETACHED_DRAINER_RUNNING
    with _DETACHED_DRAINER_GUARD:
        if _DETACHED_DRAINER_RUNNING:
            return False
        _DETACHED_DRAINER_RUNNING = True

    def run() -> None:
        global _DETACHED_DRAINER_RUNNING
        try:
            drain_once(profile)
        finally:
            with _DETACHED_DRAINER_GUARD:
                _DETACHED_DRAINER_RUNNING = False

    try:
        threading.Thread(
            target=run,
            name="contexer-proposal-drainer",
            daemon=True,
        ).start()
    except Exception:
        with _DETACHED_DRAINER_GUARD:
            _DETACHED_DRAINER_RUNNING = False
        return False
    return True


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
