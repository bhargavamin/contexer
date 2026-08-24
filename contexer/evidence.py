"""Evidence-ledger event schema, pure validation, and the per-repo sidecar that stores it.

An evidence event is one observed fact about a session — a directive the developer stated, a
file that changed, a conclusion an agent reached — recorded so a later policy pass can decide
what deserves to become a decision. `validate_event` is a pure function and the ONLY schema
gate: storage never re-asserts flatness or caps, it validates once and writes what it gets.

A leaf module: it imports `redact` (itself a leaf) and reaches `store` through the MODULE
OBJECT at call time (`store.STORE_DIR`, never `from contexer.store import ...`) — the same
load-order discipline `guard_engine.py` documents, so store.py never needs this module at
import time and a test patching `contexer.store.STORE_DIR` is seen here.

The schema is FROZEN per version: an unknown top-level key is an error rather than being
preserved, and a `schema_version` other than `SCHEMA_VERSION` is rejected in both directions
(a forward version means a newer writer whose semantics this reader cannot assume).
"""
import contextlib
import json
import math
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from contexer import redact, store

try:
    import fcntl
except ImportError:                    # pragma: no cover - non-POSIX
    fcntl = None

SCHEMA_VERSION = 1

EVENT_KINDS = frozenset({
    "user_directive", "agent_conclusion", "file_changed", "diff_observed",
    "test_result", "decision_repeated", "policy_evaluation",
    "candidate_disposition", "session_reconcile",
})

# Measured implementation constants, not product promises: bounds that keep one event cheap
# to append and to read back. Exceeding a bound is either an error (the event size) or a
# recorded, bounded loss (summary/files/attribute values) — never a silent truncation.
_MAX_EVENT_BYTES = 8192
_MAX_SUMMARY_CHARS = 500
_MAX_FILES = 20
_MAX_PATH_CHARS = 300
_MAX_ATTRS = 20
_MAX_ATTR_KEY_CHARS = 64
_MAX_ATTR_VALUE_CHARS = 500
# session_id / repo_key / source caps, in that order of appearance below.
_MAX_SESSION_ID_CHARS = 128
_MAX_REPO_KEY_CHARS = 300
_MAX_SOURCE_CHARS = 64

_KEYS = frozenset({
    "schema_version", "event_id", "session_id", "repo_key", "kind", "occurred_at",
    "source", "summary", "files", "content_hash", "attributes",
})

_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:[\\/]")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _required_text(event: Mapping, key: str, cap: int, errors: list[str]) -> str:
    """A required non-empty str field, at most `cap` chars. Appends its own error, if any."""
    value = event.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{key} must be a non-empty string")
    elif len(value) > cap:
        errors.append(f"{key} exceeds {cap} characters ({len(value)})")
    else:
        return value
    return ""


def _normalized_time(event: Mapping, errors: list[str]) -> str:
    """`occurred_at` as UTC ISO-8601 with an explicit offset. Naive timestamps are rejected:
    an event whose zone is guessed cannot be ordered against one from another host."""
    value = event.get("occurred_at")
    if not isinstance(value, str):
        errors.append("occurred_at must be an ISO-8601 string")
        return ""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        errors.append(f"occurred_at is not ISO-8601 parseable: {value!r}")
        return ""
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        errors.append("occurred_at must be timezone-aware")
        return ""
    return parsed.astimezone(timezone.utc).isoformat()


def _normalized_files(event: Mapping, errors: list[str]) -> tuple[list[str], int]:
    """The repo-relative file list plus its ORIGINAL length (the caller records the loss when
    the list is truncated). Absolute and parent-escaping paths are rejected, not rewritten."""
    value = event.get("files", [])
    if not isinstance(value, list):
        errors.append("files must be a list of strings")
        return [], 0
    for i, path in enumerate(value):
        if not isinstance(path, str) or not path.strip():
            errors.append(f"files[{i}] must be a non-empty string")
        elif len(path) > _MAX_PATH_CHARS:
            errors.append(f"files[{i}] exceeds {_MAX_PATH_CHARS} characters ({len(path)})")
        elif path.startswith("/") or _WINDOWS_DRIVE.match(path):
            errors.append(f"files[{i}] must be repo-relative, got {path!r}")
        elif ".." in path.replace("\\", "/").split("/"):
            errors.append(f"files[{i}] must not contain a '..' segment, got {path!r}")
    return value[:_MAX_FILES], len(value)


def _normalized_attributes(event: Mapping, errors: list[str]) -> dict:
    """A FLAT dict of scalars: nesting would make the ledger a document store, and the policy
    pass reads attributes as decision inputs, so str values are scrubbed and clipped here."""
    value = event.get("attributes", {})
    if not isinstance(value, dict):
        errors.append("attributes must be a flat dict")
        return {}
    # `files_total` is not counted: normalization ADDS it, so counting it would make
    # re-validating a normalized event that used all 20 caller slots an error — the
    # no-op-on-replay property this whole function promises.
    counted = sum(1 for k in value if k != "files_total")
    if counted > _MAX_ATTRS:
        errors.append(f"attributes has more than {_MAX_ATTRS} keys ({counted})")
    out = {}
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > _MAX_ATTR_KEY_CHARS:
            errors.append(f"attributes key {key!r} must be a string of at most "
                          f"{_MAX_ATTR_KEY_CHARS} characters")
            continue
        if isinstance(item, str):
            out[key] = _clip(redact.scrub_text(item), _MAX_ATTR_VALUE_CHARS)
        elif isinstance(item, float) and not math.isfinite(item):
            # `json.dumps` writes NaN/Infinity happily and no JSON reader accepts them back,
            # so an unchecked one would be a ledger line that never parses again.
            errors.append(f"attributes[{key!r}] value must be a finite number, got {item!r}")
        elif isinstance(item, (int, float, bool)):
            out[key] = item
        else:
            errors.append(f"attributes[{key!r}] value must be str, int, float or bool")
    return out


def _clip(text: str, cap: int) -> str:
    """Clip to `cap` characters INCLUDING the ellipsis, so re-validating a clipped value is a
    no-op (the whole normalization is idempotent, which is what makes replay safe)."""
    if len(text) <= cap:
        return text
    return text[:cap - 1] + "…"


def validate_event(event: Mapping) -> tuple[dict | None, list[str]]:
    """Return `(normalized_copy, [])` for a structurally valid event, else `(None, errors)`.

    Collects EVERY structural error, never raises on any input, and never mutates `event`.
    Normalization is bounded and lossy-but-recorded: `summary` and str attribute values are
    scrubbed then clipped, `files` is truncated with the original count kept in
    `attributes["files_total"]`, `occurred_at` becomes UTC, and the optional fields default.
    Normalizing an already-normalized event is a no-op.
    """
    if not isinstance(event, Mapping):
        return None, [f"event must be a mapping, got {type(event).__name__}"]

    errors: list[str] = []
    for key in sorted(str(k) for k in event if k not in _KEYS):
        errors.append(f"unknown top-level key: {key!r}")

    version = event.get("schema_version")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}, got {version!r}")

    event_id = event.get("event_id")
    try:
        uuid.UUID(event_id)
    except (ValueError, AttributeError, TypeError):
        errors.append(f"event_id must be a UUID string, got {event_id!r}")

    session_id = _required_text(event, "session_id", _MAX_SESSION_ID_CHARS, errors)
    repo_key = _required_text(event, "repo_key", _MAX_REPO_KEY_CHARS, errors)
    source = _required_text(event, "source", _MAX_SOURCE_CHARS, errors)

    kind = event.get("kind")
    # The isinstance guard is the never-raises half: `kind not in EVENT_KINDS` hashes its
    # left side, so an unhashable value (a JSON list or object) raised TypeError out of a
    # function whose whole contract is to return its errors instead.
    if not isinstance(kind, str) or kind not in EVENT_KINDS:
        errors.append(f"kind must be one of {sorted(EVENT_KINDS)}, got {kind!r}")

    occurred_at = _normalized_time(event, errors)

    summary = event.get("summary", "")
    if not isinstance(summary, str):
        errors.append("summary must be a string")
        summary = ""

    content_hash = event.get("content_hash")
    if content_hash is not None and (not isinstance(content_hash, str)
                                     or not _SHA256.fullmatch(content_hash)):
        errors.append("content_hash must be a 64-character lowercase hex string or null")

    files, files_total = _normalized_files(event, errors)
    attributes = _normalized_attributes(event, errors)
    if files_total > len(files):
        attributes["files_total"] = files_total

    if errors:
        return None, errors

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid.UUID(event_id)),
        "session_id": session_id,
        "repo_key": repo_key,
        "kind": kind,
        "occurred_at": occurred_at,
        "source": source,
        "summary": _clip(redact.scrub_text(summary), _MAX_SUMMARY_CHARS),
        "files": files,
        "content_hash": content_hash,
        "attributes": attributes,
    }
    size = len(json.dumps(normalized).encode("utf-8"))
    if size > _MAX_EVENT_BYTES:
        return None, [f"event exceeds {_MAX_EVENT_BYTES} bytes ({size})"]
    return normalized, []


# ── sidecar storage ──────────────────────────────────────────────────────────────
#
# One ledger per repo, `STORE_DIR/.evidence_<slug>.json`, keyed by the worktree-canonical
# slug every other sidecar uses. Measured implementation constants, not product promises:
# 1000 events at the 8KB per-event ceiling is comfortably under the byte cap, and 2MB is a
# file a session-start read can afford. Both are enforced after the append, oldest first.
_MAX_EVENTS = 1000
_MAX_SIDECAR_BYTES = 2 * 1024 * 1024


def _sidecar_path(repo_path: str) -> Path:
    return store.STORE_DIR / f".evidence_{store.repo_slug(repo_path)}.json"


def _lock_path(repo_path: str) -> Path:
    return store.STORE_DIR / f".evidence_lock_{store.repo_slug(repo_path)}"


def _gap_path(repo_path: str) -> Path:
    return store.STORE_DIR / f".evidence_gap_{store.repo_slug(repo_path)}"


def _empty_ledger(repo_path: str) -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "repo_path": repo_path,
        "events": [],
        # candidate_id -> {"event_ids": [...], "status": "pending"|"approved"|"dismissed"}.
        # Task 5 writes these; storage only respects them during eviction and compaction.
        "candidate_checkpoints": {},
        "compacted_through": None,
        "evicted_total": 0,
    }


def _read_ledger(repo_path: str) -> tuple[dict, str | None]:
    """(ledger, parse error) from ONE read — the degrade-but-report split `store._read_global`
    carries. A MISSING sidecar is an empty ledger with no error; anything unparseable reports,
    because an appender that took "unreadable" for "empty" would replace the whole ledger with
    the one event it happened to be writing."""
    empty = _empty_ledger(repo_path)
    try:
        raw = _sidecar_path(repo_path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty, None
    except (OSError, UnicodeDecodeError) as exc:
        return empty, f"{type(exc).__name__}: {exc}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return empty, f"{type(exc).__name__}: {exc}"
    if not isinstance(data, dict) or not isinstance(data.get("events"), list) \
            or not isinstance(data.get("candidate_checkpoints"), dict):
        return empty, "not an evidence ledger object"
    if data.get("schema_version") != SCHEMA_VERSION:
        # Same both-directions rule the event schema holds: a ledger written by a newer
        # version is not ours to rewrite.
        return empty, f"schema_version is {data.get('schema_version')!r}, not {SCHEMA_VERSION}"
    # The one place a hand-edited counter is coerced, so no later reader has to guard it.
    if not isinstance(data.get("evicted_total"), int) or data["evicted_total"] is True:
        data["evicted_total"] = 0
    return data, None


def _dump(ledger: dict) -> str:
    return json.dumps(ledger, indent=2, ensure_ascii=False)


def _pinned_event_ids(ledger: dict) -> set:
    """Event ids a PENDING checkpoint still references — Task 5 needs them to judge the
    candidate, so eviction skips them however old they are."""
    return {eid for cp in ledger["candidate_checkpoints"].values()
            if isinstance(cp, dict) and cp.get("status") == "pending"
            for eid in (cp.get("event_ids") or [])}


def _bounded_dump(ledger: dict) -> str:
    """The exact text to write, evicting oldest-first until both caps hold.

    Serializing inside the loop is what makes the byte cap measure the real file rather than
    an estimate — including `evicted_total`, which grows as evictions happen. Eviction is real
    loss, so it is counted rather than silent."""
    text = _dump(ledger)
    if len(ledger["events"]) <= _MAX_EVENTS and len(text.encode("utf-8")) <= _MAX_SIDECAR_BYTES:
        return text
    pinned = _pinned_event_ids(ledger)
    while len(ledger["events"]) > _MAX_EVENTS \
            or len(text.encode("utf-8")) > _MAX_SIDECAR_BYTES:
        victim = next((i for i, e in enumerate(ledger["events"])
                       if e.get("event_id") not in pinned), None)
        if victim is None:
            break              # every remaining event is pinned by a pending checkpoint
        ledger["events"].pop(victim)
        ledger["evicted_total"] += 1
        text = _dump(ledger)
    return text


@contextlib.contextmanager
def _evidence_lock(repo_path: str, *, blocking: bool):
    """Yield True while this process holds the repo's evidence lock, False if a non-blocking
    acquire found it busy.

    A DEDICATED lock, never `store.store_lock`: that one is held across whole-repo mining and
    anchor verification at session start, so an append behind it would stall a host hook for
    seconds. An append would rather drop its event than wait."""
    if fcntl is None:                  # pragma: no cover - non-POSIX
        yield True
        return
    store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    # Binary: only the fd is ever used, nothing is written through this handle.
    handle = open(_lock_path(repo_path), "wb")
    try:
        try:
            fcntl.flock(handle.fileno(),
                        fcntl.LOCK_EX if blocking else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def _bump_gap(repo_path: str, reason: str) -> None:
    """Record that an event was lost. Best-effort — failing to record a gap must never be the
    thing that raises into a host hook. Never cleared by a successful append: a gap means loss
    already happened, and only explicit maintenance (`compact_evidence`) acknowledges it."""
    try:
        store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        gap = _read_gap(repo_path) or {}
        drops = gap.get("drops")
        store.atomic_write(_gap_path(repo_path), json.dumps({
            "drops": (drops if isinstance(drops, int) else 0) + 1,
            "last_at": datetime.now(timezone.utc).isoformat(),
            "last_reason": reason,
        }))
    except (OSError, ValueError, TypeError):
        pass


def _read_gap(repo_path: str) -> dict | None:
    try:
        gap = json.loads(_gap_path(repo_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):      # ValueError covers JSON and UnicodeDecodeError both
        return None
    return gap if isinstance(gap, dict) else None


def append_evidence(repo_path: str, event: Mapping) -> dict:
    """Append one event to the repo's ledger. `{"status": ..., "errors": [...]}` where status
    is `stored` | `dropped_busy` | `dropped_error` | `rejected_invalid`.

    NEVER raises — host hooks call this on every prompt and tool use. A busy lock or an I/O
    failure drops the event and bumps the gap marker (recorded loss); an invalid event is
    rejected WITHOUT a gap bump, since a schema rejection is a caller bug, not lost evidence.
    An unreadable sidecar is never replaced, only reported."""
    normalized, errors = validate_event(event)
    if normalized is None:
        return {"status": "rejected_invalid", "errors": errors}
    try:
        with _evidence_lock(repo_path, blocking=False) as acquired:
            if not acquired:
                _bump_gap(repo_path, "busy")
                return {"status": "dropped_busy", "errors": []}
            ledger, error = _read_ledger(repo_path)
            if error:
                _bump_gap(repo_path, "error")
                return {"status": "dropped_error", "errors": [error]}
            ledger["events"].append(normalized)
            store.atomic_write(_sidecar_path(repo_path), _bounded_dump(ledger))
    except Exception as exc:           # broad on purpose: the never-raises contract
        _bump_gap(repo_path, "error")
        return {"status": "dropped_error", "errors": [f"{type(exc).__name__}: {exc}"]}
    return {"status": "stored", "errors": []}


def list_session_evidence(repo_path: str, session_id: str) -> list[dict]:
    """This session's events, oldest first (append order). Lock-free: atomic writes mean a
    reader never sees a torn file. A missing or corrupt sidecar reads as [] — this is a render
    path, where "nothing to show" and "cannot read" cost the same."""
    ledger, _ = _read_ledger(repo_path)
    return [e for e in ledger["events"]
            if isinstance(e, dict) and e.get("session_id") == session_id]


def _disposition_event(repo_path: str, candidate_id: str, checkpoint: Mapping) -> dict | None:
    """The one synthetic event a compacted checkpoint collapses into. Every field is built
    within its own bound, so validation here is a self-check rather than a real branch — but
    the ledger must never carry an event the validator would refuse."""
    status = checkpoint.get("status")
    count = len(checkpoint.get("event_ids") or [])
    event, _errors = validate_event({
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid.uuid4()),
        "session_id": "compaction",
        "repo_key": _clip(store.repo_slug(repo_path), _MAX_REPO_KEY_CHARS),
        "kind": "candidate_disposition",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "source": "compact_evidence",
        "summary": f"candidate {candidate_id} {status}: {count} evidence event(s) compacted",
        "attributes": {"candidate_id": _clip(str(candidate_id), _MAX_ATTR_VALUE_CHARS),
                       "candidate_status": str(status),
                       "compacted_events": count},
    })
    return event


def compact_evidence(repo_path: str) -> dict:
    """Collapse every settled (`approved`/`dismissed`) checkpoint into one `candidate_disposition`
    event each, dropping the events it consumed. CLI maintenance, so the lock is BLOCKING —
    unlike an append, a compaction that skipped its turn would just be asked for again.

    Clears the gap marker on success: maintenance is the one place that acknowledges loss."""
    try:
        with _evidence_lock(repo_path, blocking=True):
            ledger, error = _read_ledger(repo_path)
            if error:
                return {"status": "error", "compacted": 0, "removed_events": 0,
                        "errors": [error]}
            consumed = {cid: cp for cid, cp in ledger["candidate_checkpoints"].items()
                        if isinstance(cp, dict) and cp.get("status") in ("approved", "dismissed")}
            if not consumed:
                return {"status": "ok", "compacted": 0, "removed_events": 0, "errors": []}
            drop = {eid for cp in consumed.values() for eid in (cp.get("event_ids") or [])}
            removed = [e for e in ledger["events"] if e.get("event_id") in drop]
            ledger["events"] = [e for e in ledger["events"] if e.get("event_id") not in drop]
            for candidate_id, checkpoint in consumed.items():
                del ledger["candidate_checkpoints"][candidate_id]
                disposition = _disposition_event(repo_path, candidate_id, checkpoint)
                if disposition is not None:
                    ledger["events"].append(disposition)
            if removed:
                # ISO-8601 UTC with a fixed offset spelling, so max() is chronological.
                ledger["compacted_through"] = max(e.get("occurred_at") or "" for e in removed)
            store.atomic_write(_sidecar_path(repo_path), _bounded_dump(ledger))
            with contextlib.suppress(OSError):
                _gap_path(repo_path).unlink(missing_ok=True)
    except Exception as exc:           # broad on purpose: a report, not a traceback
        return {"status": "error", "compacted": 0, "removed_events": 0,
                "errors": [f"{type(exc).__name__}: {exc}"]}
    return {"status": "ok", "compacted": len(consumed), "removed_events": len(removed),
            "errors": []}


def evidence_diagnostics(repo_path: str) -> dict:
    """What the ledger holds, for `contexer status`. Lock-free. `readable` is the honest half:
    a missing sidecar reports `events=0, readable=True`, a corrupt one `readable=False`, so a
    reader is never told "no evidence" about a file that could not be parsed."""
    ledger, error = _read_ledger(repo_path)
    try:
        size = _sidecar_path(repo_path).stat().st_size
    except OSError:
        size = 0
    return {
        "events": len(ledger["events"]),
        "bytes": size,
        "checkpoints": len(ledger["candidate_checkpoints"]),
        "evicted_total": ledger["evicted_total"],
        "gap": _read_gap(repo_path),
        "readable": error is None,
    }
