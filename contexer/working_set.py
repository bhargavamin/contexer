"""Per-session delivered-guidance ledger and compaction restoration history.

This module owns the working-set sidecar's schema, validation, bounded persistence, and
most-recent-delivery ordering. Prompt selection and rendering remain in ``store.py``; they
hand this owner structured receipts only after full guidance was actually rendered.

The dependency on :mod:`contexer.store` is through the module object so tests that relocate
the store directory or replace atomic writes are observed at call time. ``store.py`` imports
this owner only inside the functions that need it, avoiding an import-order cycle.
"""

from __future__ import annotations

import hashlib
import json
import time
from pathlib import Path

from contexer import store


VERSION = 2
MAX_BYTES = 256 * 1024
FIELD_MAX = 256
SCOPES = frozenset({"personal", "global"})


def path(repo_path: str, session_id: str) -> Path:
    """Return the canonical sidecar path for one host session."""
    safe = hashlib.sha1(session_id.encode("utf-8", "replace")).hexdigest()[:16]
    return store.sidecar_path(
        "working_set", slug=store.repo_slug(repo_path), session=safe)


def _valid_string(value: object) -> bool:
    return isinstance(value, str) and 0 < len(value) <= FIELD_MAX


def _recent_unique_ids(values: list[str]) -> list[str]:
    """Keep each ID's newest position, returning oldest to newest."""
    seen: set[str] = set()
    recent: list[str] = []
    for decision_id in reversed(values):
        if decision_id not in seen:
            seen.add(decision_id)
            recent.append(decision_id)
    recent.reverse()
    return recent[-store.MAX_ENTRIES:]


def read(repo_path: str, session_id: str) -> dict:
    """Return validated ledger state; unknown identities remain restoration hints only."""
    empty = {"injected": [], "records": []}
    if not session_id:
        return empty
    try:
        # Read one byte past the limit instead of checking stat first: an atomic replacement
        # between stat and read must not bypass the memory bound.
        with path(repo_path, session_id).open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return empty
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return empty
    if not isinstance(data, dict):
        return empty

    raw_ids = data.get("injected")
    injected = []
    if isinstance(raw_ids, list):
        injected = _recent_unique_ids([item for item in raw_ids if _valid_string(item)])
    if data.get("v") != VERSION or not isinstance(data.get("records"), list):
        return {"injected": injected, "records": []}

    records: list[dict] = []
    seen: set[tuple[str, str]] = set()
    # Read newest first so duplicate rows cannot resurrect older credit.
    for row in reversed(data["records"][-store.MAX_ENTRIES:]):
        if not isinstance(row, dict):
            continue
        scope, decision_id, fingerprint = (
            row.get("scope"), row.get("id"), row.get("fingerprint"))
        if (scope not in SCOPES or not _valid_string(decision_id)
                or (fingerprint is not None and not _valid_string(fingerprint))):
            continue
        key = (scope, decision_id)
        if key in seen:
            continue
        seen.add(key)
        records.append({
            "scope": scope, "id": decision_id, "fingerprint": fingerprint,
        })
    records.reverse()
    return {"injected": injected, "records": records}


def write(repo_path: str, session_id: str, records: list[dict],
          hints: list[str] | None = None) -> bool:
    """Write a bounded v2 ledger. Return persistence success without blocking output."""
    if not session_id:
        return False
    rows = records[-store.MAX_ENTRIES:]
    hint_ids = _recent_unique_ids((hints or []) + [row["id"] for row in rows])
    try:
        store.ensure_store_dir()
        while True:
            payload = {
                "v": VERSION, "injected": hint_ids, "records": rows, "ts": time.time(),
            }
            raw = json.dumps(payload, separators=(",", ":"))
            if len(raw.encode("utf-8")) <= MAX_BYTES:
                break
            # Compatibility hints carry no suppression credit, so shed them first.
            credited_ids = {row["id"] for row in rows}
            removable = next(
                (index for index, decision_id in enumerate(hint_ids)
                 if decision_id not in credited_ids),
                None,
            )
            if removable is not None:
                hint_ids.pop(removable)
            elif rows:
                rows = rows[1:]
                hint_ids = _recent_unique_ids(hint_ids + [row["id"] for row in rows])
            else:
                return False
        store.atomic_write(path(repo_path, session_id), raw)
        return True
    except OSError:
        return False


def ids(repo_path: str, session_id: str) -> list[str]:
    """Decision IDs observed this session, retained for compatibility/restoration only."""
    return read(repo_path, session_id)["injected"]


def records(repo_path: str, session_id: str) -> list[dict]:
    """Structured delivery records that may carry current-window suppression credit."""
    return read(repo_path, session_id)["records"]


def has_credit(rows: list[dict], scope: str, decision_id: str,
               fingerprint: str | None) -> bool:
    """Whether the exact scoped guidance identity was delivered in this context window."""
    return bool(fingerprint) and any(
        row["scope"] == scope and row["id"] == decision_id
        and row.get("fingerprint") == fingerprint
        for row in rows
    )


def record_deliveries(repo_path: str, session_id: str, delivered: list[dict]) -> bool:
    """MRU-upsert actual full-render receipts, one row per scoped decision."""
    if not session_id or not delivered:
        return False
    state = read(repo_path, session_id)
    rows = list(state["records"])
    for row in delivered:
        if (row.get("scope") not in SCOPES
                or not _valid_string(row.get("id"))
                or not _valid_string(row.get("fingerprint"))):
            continue
        rows = [old for old in rows
                if (old["scope"], old["id"]) != (row["scope"], row["id"])]
        rows.append({
            "scope": row["scope"], "id": row["id"],
            "fingerprint": row["fingerprint"],
        })
    return write(repo_path, session_id, rows, state["injected"])


def add_hints(repo_path: str, session_id: str, decision_ids: list[str]) -> None:
    """Record ID-only history without inventing full-guidance suppression credit."""
    if not session_id or not decision_ids:
        return
    state = read(repo_path, session_id)
    merged = state["injected"] + [item for item in decision_ids if _valid_string(item)]
    write(repo_path, session_id, state["records"], merged)


def restoration_history(state: dict) -> list[str | dict]:
    """Combine legacy hints and v2 receipts in their persisted chronological order.

    Every v2 write constructs ``injected`` from historical hints followed by actual receipt
    rows. Consequently, a hint without any scoped row predates the structured rows. Keeping
    those hints first prevents an upgraded legacy ledger from displacing its newest actual
    deliveries at the bounded compaction replay boundary.
    """
    rows = list(state.get("records") or [])
    row_ids = {row["id"] for row in rows}
    legacy_hints = [decision_id for decision_id in state.get("injected") or []
                    if decision_id not in row_ids]
    return [*legacy_hints, *rows]
