"""Evidence-ledger event schema and pure validation, stdlib + `contexer.redact` only.

An evidence event is one observed fact about a session — a directive the developer stated, a
file that changed, a conclusion an agent reached — recorded so a later policy pass can decide
what deserves to become a decision. This module owns the SHAPE of such an event and nothing
else: `validate_event` is a pure function, so the sidecar storage and the host adapters that
emit events can be tested against a schema that never touches disk.

A leaf module: it imports `redact` (itself a leaf) and nothing else from the package — never
`store`, the MCP server, or an adapter.

The schema is FROZEN per version: an unknown top-level key is an error rather than being
preserved, and a `schema_version` other than `SCHEMA_VERSION` is rejected in both directions
(a forward version means a newer writer whose semantics this reader cannot assume).
"""
import json
import math
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone

from contexer import redact

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
