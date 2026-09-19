"""Delivered-guidance ledger: per-session suppression credit and a durable per-repo tally.

Both are the same fact - "this decision was actually rendered to a session" - recorded at the
same moment under two retention policies. The SESSION ledger answers "has this session already
seen it" and dies with the session; the COLD_REPO tally answers "has this decision done any
work at all" and outlives it. Splitting them across modules would mean two writers of one
observation, so they stay together here.

This module owns the working-set sidecar's schema, validation, bounded persistence, and
most-recent-delivery ordering. Prompt selection and rendering remain in ``store.py``; they
hand this owner structured receipts only after full guidance was actually rendered.

The dependency on :mod:`contexer.store` is through the module object so tests that relocate
the store directory or replace atomic writes are observed at call time. ``store.py`` imports
this owner only inside the functions that need it, avoiding an import-order cycle.
"""

from __future__ import annotations

import hashlib
import math
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
        if (not isinstance(scope, str) or scope not in SCOPES
                or not _valid_string(decision_id)
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


DELIVERY_VERSION = 2


def delivery_path(repo_path: str) -> Path:
    """The canonical durable delivery-tally path for one repo."""
    return store.sidecar_path("delivery_tally", slug=store.repo_slug(repo_path))


def _delivery_lock_slug(repo_path: str) -> str:
    """Own lock, not the store's: this is written from the PROMPT path, and taking the store
    lock there would serialize rendering against every ordinary store write for a counter
    nobody reads synchronously. `<slug>_delivery.lock` still matches the declared `lock` kind,
    so it inherits that kind's DURABLE lifetime rather than becoming an unswept stray."""
    return f"{store.repo_slug(repo_path)}_delivery"


# Bounded, never unbounded: this runs on the per-prompt path, where the standing invariant is
# that optional bookkeeping can never stop a hook rendering context. A blocking flock has no
# timeout, so one stalled holder would hang the prompt for a counter nothing reads
# synchronously. Three quick tries absorb ordinary brief contention; past that the increment is
# dropped, which costs at most one render event and never costs the developer their output.
_TALLY_LOCK_TRIES = 3
_TALLY_LOCK_BACKOFF = 0.005


def _valid_stamp(value: object) -> bool:
    """A finite, non-negative Unix timestamp. Rejects NaN (value != value) AND +/-Infinity
    (math.isfinite) - JSON accepts the bare `Infinity`/`NaN` tokens Python's own json.dumps
    emits for them, so a persisted row can carry either without the file being malformed.
    An infinite `last` would sort newest-forever and evict every genuinely recent row once
    the tally reaches capacity."""
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value) and value >= 0)


def read_delivery_tally(repo_path: str) -> dict:
    """Validated {"<scope>:<id>": {"renders", "first", "last", "session"}}; {} if unusable.

    Every field eviction or a report will later read is validated HERE, because the mutation
    path sorts on `last`: a row that passes this filter without one raised KeyError out of a
    fail-soft helper and into the prompt path.
    """
    try:
        with delivery_path(repo_path).open("rb") as source:
            raw = source.read(MAX_BYTES + 1)
        if len(raw) > MAX_BYTES:
            return {}
        data = json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    if not isinstance(data, dict) or data.get("v") != DELIVERY_VERSION:
        return {}
    rows = data.get("rows")
    if not isinstance(rows, dict):
        return {}
    clean: dict[str, dict] = {}
    for key, row in rows.items():
        if not _valid_string(key) or not isinstance(row, dict):
            continue
        renders = row.get("renders")
        if not isinstance(renders, int) or isinstance(renders, bool) or renders <= 0:
            continue
        if not _valid_stamp(row.get("first")) or not _valid_stamp(row.get("last")):
            continue
        session = row.get("session")
        clean[key] = {"renders": renders, "first": row["first"], "last": row["last"],
                      "session": session if _valid_string(session) else ""}
    return clean


def _session_fingerprint(session_id: str) -> str:
    """Bounded stand-in for a raw session id, same construction as `path()` above.

    The row's `session` field is compared for equality to suppress a consecutive repeat.
    A raw id can exceed FIELD_MAX (256), and the READER's `_valid_string` then discards it
    back to "" - so a long id would compare unequal to itself on the very next call and
    defeat suppression entirely (same session, same decision, renders += 1 forever). Fixed
    at 16 hex chars, always well under FIELD_MAX, so the round trip through the reader is
    lossless."""
    return hashlib.sha1(session_id.encode("utf-8", "replace")).hexdigest()[:16]


def _gap_path(repo_path: str) -> Path:
    return store.sidecar_path("delivery_gaps", slug=store.repo_slug(repo_path))


def record_delivery_gap(repo_path: str, count: int) -> None:
    """Durably note `count` render events that were NOT recorded, because the tally's lock
    was contended past its retry budget. Never raises; never blocks.

    Deliberately lock-free: a single `open(..., "a")` write is a single write(2) syscall for
    a payload this small, and POSIX guarantees that call atomic under O_APPEND (the mode
    Python's text "a" sets), so two concurrent droppers cannot corrupt each other's line -
    no flock needed for an append-only counter nobody reads synchronously.

    A gap is recorded per DROPPED EVENT, not attributed to any one decision: the tally
    cannot know, after losing the race, which specific rows would have been new. Reporting
    it as a total is honest about that limit rather than guessing which decision to blame.
    """
    if count <= 0:
        return
    try:
        store.ensure_store_dir()
        with open(_gap_path(repo_path), "a", encoding="utf-8") as f:
            f.write("x" * count + "\n")
    except OSError:
        pass


def delivery_gap_count(repo_path: str) -> int:
    """Total render events lost to lock contention and never reflected in the tally.

    A caller drawing on `read_delivery_tally` for "was this decision ever delivered" should
    read this alongside it: a nonzero count means some render events during the measurement
    window are simply missing, not necessarily concentrated on any one row, so absence for
    a specific decision is reliable only to within this many total lost observations.
    """
    try:
        return _gap_path(repo_path).read_text(encoding="utf-8").count("x")
    except OSError:
        return 0


def record_delivery_tally(repo_path: str, session_id: str, delivered: list[dict]) -> bool:
    """Bump the durable per-decision render count. Never raises; never blocks output.

    `renders` counts RENDER EVENTS, deliberately not distinct sessions. Counting sessions
    needs unbounded session identity persisted per decision, and the question this exists to
    answer - "was this decision ever delivered at all" - is a boolean that no counting
    convention changes. `session` holds a bounded fingerprint of the most recent session id
    (see `_session_fingerprint`) and suppresses a consecutive repeat, which removes the two
    ways one session inflated the figure (a compaction re-render, and a retry after the
    session ledger failed to persist credit). Interleaved sessions still produce separate
    renders; that is why this is not `sessions`.

    Keyed by (scope, decision id) rather than appended per event, so the file is bounded by
    the store's entry cap instead of by prompt volume - the tail-capped retrieval_log would
    silently drop the START of a multi-week window, which is exactly what this must not do.

    Locked across read-modify-write, because atomic replacement prevents a torn file but not
    a lost update: two sessions delivering in one repo would otherwise drop each other's
    increments, and undercounting is the one failure that makes a used decision look dead.
    The lock is NON-BLOCKING with a bounded retry - a blocking flock has no timeout, so one
    stalled holder would hang the prompt for a counter nothing reads synchronously, and the
    standing invariant is that optional bookkeeping never stops a hook rendering context.

    Exhausting the retry budget on a decision's FIRST delivery would otherwise be
    indistinguishable from "never delivered" - the exact case the measurement exists to
    detect. `record_delivery_gap` makes that loss visible instead of silent; see
    `delivery_gap_count`.
    """
    valid = [row for row in delivered
             if isinstance(row.get("scope"), str) and row["scope"] in SCOPES
             and _valid_string(row.get("id"))]
    if not valid:
        return False
    fingerprint = _session_fingerprint(session_id or "")
    for attempt in range(_TALLY_LOCK_TRIES):
        try:
            with store.store_lock(_delivery_lock_slug(repo_path), blocking=False):
                rows = read_delivery_tally(repo_path)
                now = time.time()
                changed = False
                for row in valid:
                    key = f"{row['scope']}:{row['id']}"
                    existing = rows.get(key)
                    if existing is None:
                        rows[key] = {"renders": 1, "first": now, "last": now,
                                     "session": fingerprint}
                        changed = True
                    elif existing["session"] != fingerprint:
                        existing["renders"] += 1
                        existing["last"] = now
                        existing["session"] = fingerprint
                        changed = True
                if not changed:
                    return False
                if len(rows) > store.MAX_ENTRIES:
                    # Evict least-recently-delivered first: the rows this exists to surface
                    # are the ones still doing work, and the longest-unseen is safest to drop.
                    rows = dict(sorted(rows.items(),
                                       key=lambda kv: kv[1]["last"])[-store.MAX_ENTRIES:])
                store.ensure_store_dir()
                store.atomic_write(
                    delivery_path(repo_path),
                    json.dumps({"v": DELIVERY_VERSION, "rows": rows, "ts": now},
                               separators=(",", ":")))
                return True
        except BlockingIOError:
            # Another writer holds it. Back off briefly rather than wait - see the constants.
            if attempt + 1 < _TALLY_LOCK_TRIES:
                time.sleep(_TALLY_LOCK_BACKOFF)
        except Exception:
            # Wider than OSError on purpose: bookkeeping on the per-prompt path. A malformed
            # persisted row is filtered by the reader above, but the boundary must hold for
            # whatever the next shape of corruption turns out to be.
            return False
    record_delivery_gap(repo_path, len(valid))
    return False


def record_deliveries(repo_path: str, session_id: str, delivered: list[dict]) -> bool:
    """MRU-upsert actual full-render receipts, one row per scoped decision."""
    if not session_id or not delivered:
        return False
    state = read(repo_path, session_id)
    rows = list(state["records"])
    for row in delivered:
        scope = row.get("scope")
        if (not isinstance(scope, str) or scope not in SCOPES
                or not _valid_string(row.get("id"))
                or not _valid_string(row.get("fingerprint"))):
            continue
        rows = [old for old in rows
                if (old["scope"], old["id"]) != (row["scope"], row["id"])]
        rows.append({
            "scope": row["scope"], "id": row["id"],
            "fingerprint": row["fingerprint"],
        })
    # Durable tally last: a tally failure must never cost the session its suppression credit,
    # which is the half that actually changes what the developer sees on the next prompt.
    written = write(repo_path, session_id, rows, state["injected"])
    record_delivery_tally(repo_path, session_id, delivered)
    return written


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
