"""The evidence spool: one atomic JSON file per event, no locks anywhere.

Storage for the evidence ledger, laid out as a spool rather than a sidecar document:

    STORE_DIR/evidence/<repo-slug>/
    ├── pending/<utc-stamp>-<event-id>.json          raw events awaiting reconciliation
    ├── held/<candidate-id>/<utc-stamp>-<event-id>.json   events behind an unsettled candidate
    ├── quarantine/                                  malformed events, isolated not fatal
    ├── .orphan_receipts.json                        terminal receipts for evidence whose
    │                                                decision no longer exists
    └── .gap                                         at least one event was lost

Why per-event files: a host hook appends on every prompt and every tool use, and the one
property that keeps that affordable is that **the cost of event N does not depend on events
1..N-1**. `append_evidence` writes exactly one file - it never lists, reads, parses, counts
or rewrites the spool, and it takes NO lock of any kind. A uuid event id in the filename is
what removes writer contention: two concurrent writers cannot name the same target, so there
is nothing to serialize and nothing to lose.

Retention, orphan sweeps and quarantine moves are the opposite kind of work - they scan - so
they run ONLY from reconciliation or maintenance (`run_retention`), never from an editor hook.

A leaf module: it imports `evidence` for the one schema gate (`validate_event` - validation
lives there and is never reimplemented here) and reaches `store` through the MODULE OBJECT at
call time (`store.STORE_DIR`, `store.repo_slug`, `store.load`), the load-order discipline
`guard_engine.py` documents, so store.py never needs this module at import time and a test
patching `contexer.store.STORE_DIR` is seen here.

`.gap` is an honesty marker, not a queue: it records what left the spool without being
consumed, and nothing here ever clears it - a successful run does not un-lose an event, so
only explicit maintenance may acknowledge one. It keeps TWO counts, because "we failed to
record this" (`drops`) and "this aged out unconsumed" (`expired`) are different news for a
developer and only the first is a bug (see `_bump_gap`).

`.orphan_receipts.json` records a DIFFERENT fact from either of those, which is why it is its
own file rather than a third `.gap` counter: an orphan's events were acknowledged, spooled,
aggregated and held against a decision, and only then did that decision cease to exist. Calling
that a spool loss would claim capture failed when it plainly did not (see
`record_orphan_receipt`).
"""
import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from contexer import evidence, store

# Measured implementation constants, not product promises. 1000 pending events at the 8KB
# per-event ceiling validation already enforces is ~8MB of spool a reconciliation pass can
# afford to read; 30 days is longer than any session anyone reconciles. The temp-file age is
# the only one that is about crashes rather than volume: an hour is far beyond how long an
# `os.replace` can legitimately be in flight, so anything older is debris.
#
# BEFORE RAISING `_MAX_PENDING_EVENTS`, read `candidates._merge_target`: reading 8MB is the
# cheap half. Grouping is still O(N^2) in DISTINCT statements that share a word, and
# reconciliation now runs on the SessionStart path EVERY host traverses. Measured at 1000
# (aggregation alone, three-run median after warm-up): ~5.7ms for a realistic corpus (100
# statements plus corroborating file changes), ~106ms if every event is its own distinct
# statement. Both are ~25x the pre-indexing figures, which is headroom rather than a licence:
# the growth in the ceiling case is still quadratic.
_MAX_PENDING_EVENTS = 1000
_MAX_PENDING_AGE_DAYS = 30
_MAX_TEMP_AGE_SECONDS = 3600
# How often the session-start maintenance pass may scan. 24h, mirroring every other periodic
# sweep keyed off a mtime stamp (`store._MINER_VERIFY_TTL`, `anchors._ANCHOR_VERIFY_TTL`).
_MAINTENANCE_TTL = 86400

_DIR_MODE = 0o700
# Every spool file is created by `tempfile.mkstemp`, which is 0600 umask-independently, and
# `os.replace` preserves the mode - so 0600 is never re-asserted after the rename.
_TEMP_PREFIX = "tmp-"
_META_NAME = "candidate.json"

# The terminal-receipt ledger for orphaned evidence, and its bound. 200 rows matches
# `reconcile._RECEIPT_LOG_CAP` rather than `store.MAX_EVIDENCE_SUMMARIES`=50: those 50 rows
# compete for one decision's own history, while these compete only with each other, and each
# one is the ONLY surviving record of a candidate whose decision is gone. Truncation is
# RECORDED (`dropped`), the rule `_anchor_sources` and `_bump_gap` already follow, so a ledger
# that lost its oldest rows says so instead of reading as a complete account.
_ORPHAN_RECEIPTS_NAME = ".orphan_receipts.json"
MAX_ORPHAN_RECEIPTS = 200

# The one reason this ledger is ever written, stated once. Both callers file the same fact -
# the sweep and reconciliation's own finalize reach it from different directions - so it is a
# constant here rather than a parameter two call sites would eventually word differently.
ORPHAN_REASON = ("the owning decision exists in neither the live store nor the tombstone "
                 "sidecar")

# What `finalize_candidate_evidence` will settle a candidate as, and the whole vocabulary:
# these are the only two dispositions anything writes. `edited` and `ignored` were declared
# here alongside them with no writer anywhere, which made a closed vocabulary read as exercised
# code and gave `reconcile._dispositions` two statuses it could accept off a hand-edited
# `candidate.json` and never produce. Adding one back when a human-review path actually
# distinguishes "the developer rewrote it" from "the developer approved it" is one word.
DISPOSITIONS = frozenset({"approved", "dismissed"})

# The transition phases one candidate moves through, in order. `state` is WHERE a candidate
# stands, `status` is the disposition it settles at (`pending` until one is known) - the field
# that existed before this machine did, kept because every reader of a hold already speaks it.
#
# * `held`          - the manifest exists and its named events are being or have been moved;
# * `materializing` - the store write may have started and must be replayed idempotently;
# * `pending_review`- a concrete entry or lifecycle proposal is awaiting a developer;
# * `settled`       - no review remains and a disposition is known;
# * `reviewed`      - the disposition and its durable summary are both recorded, leaving only
#                     the raw cleanup.
#
# An UNRECOGNIZED state is never guessed at: the candidate reads as incomplete, is counted in
# `evidence_diagnostics`, and is neither swept nor resumed nor deleted.
CANDIDATE_STATES = ("held", "materializing", "pending_review", "settled", "reviewed")
MANIFEST_VERSION = 1

# Mitigation 6: every id that becomes a path component is shape-checked BEFORE the join, so
# no `..`, no separator and no absolute path can ever reach one. Deliberately a whitelist of
# the two shapes this layer actually mints (a uuid, or bare hex) rather than a blocklist.
_ID_SHAPE = re.compile(r"[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}|[0-9a-f]{8,64}", re.I)
_EVENT_ID_IN_NAME = re.compile(r"-([0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12})\.json\Z", re.I)


# ── layout ───────────────────────────────────────────────────────────────────────

def _evidence_root() -> Path:
    return store.STORE_DIR / "evidence"


def _repo_dir(repo_path: str, slug: str = "") -> Path:
    # `store.repo_slug` and nothing else: the readers resolve the same way, so a linked
    # worktree and its main worktree address one spool (the identity-agreement rule). An
    # explicit `slug` is for the one reader that has no repo path to resolve - see
    # `spool_slugs`.
    return _evidence_root() / (slug or store.repo_slug(repo_path))


def _pending_dir(repo_path: str, slug: str = "") -> Path:
    return _repo_dir(repo_path, slug) / "pending"


def _held_root(repo_path: str, slug: str = "") -> Path:
    return _repo_dir(repo_path, slug) / "held"


def _held_dir(repo_path: str, candidate_id: str) -> Path:
    return _held_root(repo_path) / _checked_id(candidate_id, "candidate_id")


def _quarantine_dir(repo_path: str, slug: str = "") -> Path:
    return _repo_dir(repo_path, slug) / "quarantine"


def _gap_path(repo_path: str, slug: str = "") -> Path:
    return _repo_dir(repo_path, slug) / ".gap"


def _orphan_receipts_path(repo_path: str, slug: str = "") -> Path:
    return _repo_dir(repo_path, slug) / _ORPHAN_RECEIPTS_NAME


def spool_slugs() -> list[str]:
    """Every repo slug that has a spool directory, sorted.

    A slug is not reversible into a repo path, which is exactly why this exists: a repo whose
    evidence never produced a store entry (every candidate insufficient, or duplicates before
    any entry existed) has no store file for `contexer status` to find it by, so its pending
    count and its `.gap` were invisible in the one surface meant to report them. The slug is
    what the developer is shown for such a repo - less readable than a path, and the only
    honest thing on offer."""
    try:
        return sorted(p.name for p in _evidence_root().iterdir() if p.is_dir())
    except OSError:
        return []


def _checked_id(value: object, label: str) -> str:
    """`value` if it is a safe path component, else ValueError.

    Raising is right here and does not weaken the never-raises contract elsewhere: only the
    reconciliation-side entry points take an id, and a malformed one is a caller bug - the
    hook-facing `append_evidence` never names a path component at all.
    """
    if not isinstance(value, str) or not _ID_SHAPE.fullmatch(value):
        raise ValueError(f"{label} must be a uuid or hex id, got {value!r}")
    return value


def _ensure_dir(path: Path) -> Path:
    """`mkdir -p` at 0700 on every level, including the ones `Path.mkdir(parents=True)` would
    create at the default mode - the spool holds verbatim prompt text, so 0700 is the point."""
    chain = [path]
    while chain[-1] != store.STORE_DIR and chain[-1].parent != chain[-1]:
        chain.append(chain[-1].parent)
    for directory in reversed(chain):
        directory.mkdir(mode=_DIR_MODE, exist_ok=True)
    return path


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _stamp(occurred_at: str) -> str:
    """The sortable filename prefix. Lexical order over this spelling IS chronological order,
    which is what lets every listing sort by name instead of stat-ing each file."""
    try:
        moment = datetime.fromisoformat(occurred_at).astimezone(timezone.utc)
    except (TypeError, ValueError):     # validation already guarantees this parses
        moment = _now()
    return moment.strftime("%Y%m%dT%H%M%S%fZ")


def _event_files(directory: Path) -> list[Path]:
    """Every candidate event file in `directory`, oldest first by filename.

    Temp files are skipped rather than read, because a rename may be in flight. EVERYTHING
    else is included, even a name this layer would never mint: a listing that silently
    ignored junk would leave it invisible to quarantine AND to retention, which is how a
    stray file becomes permanent. `candidate.json` is not special-cased here for that reason
    - in `pending/` or `quarantine/` the name can never legitimately occur, so it should be
    read, fail validation, and be quarantined like any other malformed file. Two callers list
    a HELD directory, where the name IS legitimate: `evidence_diagnostics` filters it out of
    the event count itself, and `finalize_candidate_evidence` needs no filter because
    `_EVENT_ID_IN_NAME` cannot match it.

    An ABSENT directory is an empty listing; any other listing failure is raised. The two are
    not the same fact, and a caller that must tell "nothing spooled" from "could not read the
    spool" - `evidence_diagnostics` - can only do so if this does not collapse them.
    """
    try:
        entries = list(directory.iterdir())
    except (FileNotFoundError, NotADirectoryError):
        return []
    return sorted((p for p in entries
                   if p.is_file() and not p.name.startswith(_TEMP_PREFIX)),
                  key=lambda p: p.name)


def _write_json(directory: Path, name: str, payload) -> None:
    """Write one file atomically: temp file in the SAME directory, then `os.replace`.

    Same directory means same filesystem, so the rename is atomic and a reader never sees a
    partial file - it sees the old name or the new one, never a half-written event.
    """
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=_TEMP_PREFIX, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False))
        os.replace(tmp, directory / name)
    finally:
        Path(tmp).unlink(missing_ok=True)   # no-op after a successful replace


# ── the gap marker ───────────────────────────────────────────────────────────────

def _read_gap(repo_path: str, slug: str = "") -> dict | None:
    """The gap marker, `None` if there has never been one, `{"unreadable": True}` if there is
    one that cannot be read.

    The three-way answer is the point (ruling R28): `.gap` is a CUMULATIVE loss ledger, not a
    resettable alarm, so collapsing "no loss ever recorded" into "the record of the loss is
    damaged" would let a reader report a clean spool over a marker that says otherwise - and
    would let the next bump restart the count from one. Same degrade-but-report split
    `store._read_global` carries, and the same one `_event_files` draws for a directory.
    """
    try:
        raw = _gap_path(repo_path, slug).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError:
        return {"unreadable": True}
    try:
        gap = json.loads(raw)
    except ValueError:                  # covers JSON and UnicodeDecodeError both
        return {"unreadable": True}
    return gap if isinstance(gap, dict) else {"unreadable": True}


def _count(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _bump_gap(repo_path: str, reason: str, drops: int = 1, field: str = "drops") -> None:
    """Record that `drops` events went. Best-effort: failing to record a gap must never be the
    thing that raises into a host hook.

    TWO counters, and which one is bumped is decided by the CALLER's own path, never by
    looking at the events (ruling R29 forbids retention reading content):

    * `drops` is genuine LOSS - a write that failed, a quarantined event that aged out. We
      tried to record this and could not.
    * `expired` is evidence that aged out of `pending/` UNCONSUMED. Kept separate because
      counting it as loss made `contexer status` report "N events lost" on a repo that lost
      nothing, in the one surface built to be honest about loss - a failed write and an
      unconsumed queue are different news, and only the first is a bug in this module.

    WHAT `expired` MEANS CHANGED, and the separation outlived the reason for it. It was
    introduced when Codex and Cursor reached no reconciliation entrypoint at all, so ageing out
    genuinely was the designed end of their queue. Every host now reconciles at session start
    (`store._local_session_start_payload`), so an `expired` count on ANY host is an anomaly: a
    30-day-old event outlived every session start in that month. It is still not a `drops`,
    because nothing here failed to record anything - but it is a symptom to look at rather than
    an explanation to be satisfied by, and `cli._gap_phrase` renders it that way.

    A DAMAGED marker is never overwritten with a fresh count of one. The counts it recorded are
    unrecoverable, so they restart - but `prior_drops_unknown` is stamped and carried forward
    for good, which keeps the ledger honest about being a lower bound instead of quietly
    reporting `drops: 1` over a marker that had said 47.
    """
    try:
        _ensure_dir(_repo_dir(repo_path))
        previous = _read_gap(repo_path) or {}
        marker = {
            "drops": _count(previous.get("drops")),
            "expired": _count(previous.get("expired")),
            "last_at": _now().isoformat(),
            "last_reason": reason,
        }
        marker[field] = _count(marker.get(field)) + drops
        if previous.get("unreadable") or previous.get("prior_drops_unknown"):
            marker["prior_drops_unknown"] = True
        store.atomic_write(_gap_path(repo_path), json.dumps(marker))
    except (OSError, ValueError, TypeError):
        pass


# ── append (the hook path) ───────────────────────────────────────────────────────

def append_evidence(repo_path: str, event: Mapping) -> dict:
    """Spool one event. `{"status": ..., "errors": [...]}`, status `stored` | `dropped_error`
    | `rejected_invalid`.

    NEVER raises - host hooks call this on every prompt and tool use. Writes exactly ONE file
    and reads nothing: no listing, no count, no lock, so cost is independent of how much the
    spool already holds. An I/O failure drops the event and bumps `.gap` (recorded loss); an
    invalid event is rejected WITHOUT a gap bump, since a schema rejection is a caller bug
    rather than evidence going missing.
    """
    normalized, errors = evidence.validate_event(event)
    if normalized is None:
        return {"status": "rejected_invalid", "errors": errors}
    try:
        pending = _ensure_dir(_pending_dir(repo_path))
        _write_json(pending,
                    f"{_stamp(normalized['occurred_at'])}-{normalized['event_id']}.json",
                    normalized)
    except Exception as exc:            # broad on purpose: the never-raises contract
        _bump_gap(repo_path, "write_error")
        return {"status": "dropped_error", "errors": [f"{type(exc).__name__}: {exc}"]}
    return {"status": "stored", "errors": []}


# ── read (the reconciliation path) ───────────────────────────────────────────────

def _quarantine(repo_path: str, path: Path) -> None:
    """Isolate one unreadable file so it never hides its valid siblings again. Best-effort:
    a file that cannot be moved is simply read and rejected again next pass."""
    try:
        os.replace(path, _ensure_dir(_quarantine_dir(repo_path)) / path.name)
    except OSError:
        pass


def _read_event(path: Path) -> dict | None:
    """One spooled event, or None if it is not a valid event any more.

    Re-validating on the way out is deliberate: normalization is idempotent, so this costs
    nothing for a healthy file and catches a hand-edited or schema-drifted one at the only
    point where it could otherwise reach the policy pass as if it were trustworthy.
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    normalized, _errors = evidence.validate_event(raw)
    return normalized


def list_pending_evidence(repo_path: str, session_id: str = "") -> list[dict]:
    """Every unheld event, oldest first; `session_id=""` means every session.

    Malformed files are MOVED to `quarantine/` as they are met, so one bad event can never
    hide the valid ones beside it. Lock-free: atomic writes mean a reader never sees a torn
    file, and a file renamed out from under this listing simply reads as absent.

    Filtered by the event's own `session_id` and never by `repo_key`: a linked worktree
    shares the main worktree's canonical spool while its events carry the physical worktree
    path, so a repo_key filter would hide exactly what reconciliation exists to consume.

    An unreadable spool reads as empty HERE, deliberately: this is the consume path, and it
    is `evidence_diagnostics(...)["readable"]` - not a silent empty list - that a caller asks
    before treating "no events" as "nothing happened".
    """
    events = []
    try:
        paths = _event_files(_pending_dir(repo_path))
    except OSError:
        return []
    for path in paths:
        event = _read_event(path)
        if event is None:
            _quarantine(repo_path, path)
            continue
        if session_id and event.get("session_id") != session_id:
            continue
        events.append(event)
    return events


# ── hold / finalize (the candidate lifecycle) ────────────────────────────────────

def _with_state(meta: dict) -> dict:
    """`meta` with its transition phase resolved. A read-time migration, never a write.

    A manifest written before this state machine existed carries no `state`, and such held
    directories are on real machines from the installed branch - so an absent one is DERIVED
    rather than read as damage: a recorded disposition means the candidate was `settled`, a
    named `entry_id` means it reached review, and anything else is still `held`. The
    disposition test comes first because a settled-on-arrival duplicate carries both.

    An unrecognized EXPLICIT state is the opposite case and is flagged instead: something
    wrote a phase this version does not know, so the candidate is incomplete and nothing may
    resume, sweep or delete it.
    """
    if not meta or meta.get("unreadable"):
        return meta                     # no manifest, or one nothing can read: not migratable
    state = meta.get("state")
    if state is None:
        return {**meta, "state": (
            "settled" if str(meta.get("status") or "") in DISPOSITIONS
            else "pending_review" if str(meta.get("entry_id") or "") else "held")}
    return meta if state in CANDIDATE_STATES else {**meta, "invalid_state": True}


def _read_meta(held: Path) -> dict:
    """A held candidate's bookkeeping. `{}` when it was never written, `{"unreadable": True}`
    when it was written and cannot be read - the same three-way split `_read_gap` draws, and
    for the same reason: neither shape carries an `entry_id`, so the sweep skips both, and
    only the count in `evidence_diagnostics` tells the developer which one it is."""
    try:
        raw = (held / _META_NAME).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return {"unreadable": True}
    try:
        meta = json.loads(raw)
    except ValueError:
        return {"unreadable": True}
    return _with_state(meta) if isinstance(meta, dict) else {"unreadable": True}


def hold_candidate_evidence(repo_path: str, candidate_id: str, event_ids,
                            meta: Mapping | None = None) -> dict:
    """Move the named pending events into `held/<candidate-id>/`, so they stop re-aggregating
    while the candidate awaits review.

    `{"status": "ok"|"error", "moved": N, "already_held": N, "missing": [...], "failed": [...],
    "errors": [...]}`.
    A source that is already gone while the target exists counts as ALREADY MOVED rather than
    an error: that is exactly the state a crash between two `os.replace` calls leaves, and the
    next run finishing the moves is the recovery. A source gone with no target is REPORTED,
    never raised - the event was evicted or never spooled, and the caller decides.

    Each event is attempted INDEPENDENTLY: a transient failure on one move must not leave the
    events behind it unattempted, which would silently shrink the batch to whatever preceded
    the first bad file. A failed id lands in `failed` with its own `errors` line, so a retry
    knows exactly which events still need moving.

    `meta` is written beside the events as `candidate.json`: the candidate's own bookkeeping
    (its `state`, `entry_id`, lane, revision id). It is what makes a held dir self-describing,
    which is what lets `run_retention` tell an unsettled candidate from an orphaned one.

    It is written BEFORE the first move, and that order is the state machine's first two
    transitions: a crash between two `os.replace` calls then leaves a manifest naming every
    event the candidate claims, which is what the next pass finishes the move from. The
    reverse order would leave events in a directory nothing can attribute.
    """
    # Shape-checked BEFORE the try, so a bad id is a raised caller bug rather than one more
    # soft error line - everything inside the try is I/O, which is what degrades.
    _checked_id(candidate_id, "candidate_id")
    ids = [_checked_id(event_id, "event_id") for event_id in event_ids]
    result = {"status": "ok", "moved": 0, "already_held": 0, "missing": [], "failed": [],
              "errors": []}
    try:
        held = _ensure_dir(_held_dir(repo_path, candidate_id))
        if meta is not None:
            _write_json(held, _META_NAME, dict(meta))
    except (OSError, TypeError, ValueError) as exc:   # incl. an unserializable `meta`
        # The one failure that IS fatal to the batch: with no destination (or no bookkeeping
        # to attribute it by) there is nowhere to move anything to.
        result["status"] = "error"
        result["errors"].append(f"{type(exc).__name__}: {exc}")
        return result
    pending = _pending_dir(repo_path)
    for event_id in ids:
        try:
            source = next(iter(sorted(pending.glob(f"*-{event_id}.json"))), None)
            if source is None:
                if next(iter(held.glob(f"*-{event_id}.json")), None) is not None:
                    result["already_held"] += 1
                else:
                    result["missing"].append(event_id)
                continue
            os.replace(source, held / source.name)
            result["moved"] += 1
        except OSError as exc:
            result["status"] = "error"
            result["failed"].append(event_id)
            result["errors"].append(f"{event_id}: {type(exc).__name__}: {exc}")
    return result


def update_candidate_state(repo_path: str, candidate_id: str, state: str, **fields) -> bool:
    """Move one candidate's manifest to `state`, merging `fields`. True when it landed.

    The atomic half of the state machine: a same-directory temp file plus `os.replace` (0600
    from `mkstemp`, preserved by the rename), so a reader sees the old phase or the new one and
    never a half-written manifest.

    A manifest that was never written, cannot be read, or carries an unknown state is REFUSED
    rather than replaced. The phase of a candidate nothing can read is unknown, and writing one
    here would invent it - the caller reports the pass incomplete and the directory keeps every
    file it holds.
    """
    _checked_id(candidate_id, "candidate_id")
    if state not in CANDIDATE_STATES:
        raise ValueError(f"state must be one of {list(CANDIDATE_STATES)}, got {state!r}")
    held = _held_dir(repo_path, candidate_id)
    meta = _read_meta(held)
    if not meta or meta.get("unreadable") or meta.get("invalid_state"):
        return False
    try:
        _write_json(held, _META_NAME,
                    {**meta, **fields, "state": state, "updated_at": _now().isoformat()})
    except (OSError, TypeError, ValueError):
        return False
    return True


def held_events(repo_path: str, candidate_id: str) -> list[dict]:
    """The validated events one held candidate still holds, oldest first.

    What a resumed materialization reads: the events are out of `pending/`, so the hold is the
    only place they exist. `candidate.json` can never match `_EVENT_ID_IN_NAME`, so the name
    needs no special case. An unreadable event is SKIPPED rather than quarantined - moving a
    held file out would break the hold's own account of what it is holding.
    """
    _checked_id(candidate_id, "candidate_id")
    events = []
    for path in _event_files(_held_dir(repo_path, candidate_id)):
        if not _EVENT_ID_IN_NAME.search(path.name):
            continue
        event = _read_event(path)
        if event is not None:
            events.append(event)
    return events


def discard_empty_hold(repo_path: str, candidate_id: str) -> bool:
    """Remove a held directory that holds NO events. True when it went.

    The one removal here that files no receipt, safe for exactly one reason: there is nothing
    to file a receipt about. A hold whose events were claimed by another candidate (two passes
    racing) or whose manifest landed before any event could move keeps its candidate id
    occupied for good otherwise - the same evidence re-aggregates to the same id and reads as
    already pending on every pass, forever.

    Refused the moment one event file is present, and refused when the directory cannot be
    listed: "raw evidence is removed only after its receipt is durable" is decided by what is
    actually in there, never by what the manifest claims.
    """
    _checked_id(candidate_id, "candidate_id")
    held = _held_dir(repo_path, candidate_id)
    try:
        if any(_EVENT_ID_IN_NAME.search(path.name) for path in _event_files(held)):
            return False
    except OSError:
        return False
    shutil.rmtree(held, ignore_errors=True)
    return not held.exists()


def held_candidates(repo_path: str) -> dict:
    """`{candidate_id: meta}` for every held candidate - the "already pending" set, and the
    bookkeeping a later pass needs to settle each one.

    The meta is whatever `_read_meta` answers, so it comes in three shapes: the recorded
    mapping, `{}` when no `candidate.json` was ever written, and `{"unreadable": True}` when
    one was written and cannot be read. Every one of them means the candidate is HELD; only
    the first carries an `entry_id`, and the other two are what `evidence_diagnostics` counts
    as `held_unattributed`."""
    root = _held_root(repo_path)
    try:
        directories = sorted(root.iterdir())
    except OSError:
        return {}
    return {d.name: _read_meta(d) for d in directories
            if d.is_dir() and _ID_SHAPE.fullmatch(d.name)}


def _held_event_ids(held: Path) -> list[str]:
    """The event ids one held directory still holds, oldest first.

    ONE definition, because two terminal writers read it and they must agree: the receipt filed
    before a delete and the summary returned by the delete itself would otherwise be two
    accounts of the same event set. An unreadable directory answers with what is known - the
    empty list - rather than raising, since both callers are already settling."""
    try:
        held_files = _event_files(held)
    except OSError:
        return []
    return [match.group(1) for match in
            (_EVENT_ID_IN_NAME.search(path.name) for path in held_files) if match]


def _read_orphan_receipts(repo_path: str, slug: str = "") -> dict:
    """The orphan receipt ledger: `{}` when there has never been one, the mapping when it
    reads, `{"unreadable": True}` when one exists and cannot be read.

    The same three-way split `_read_gap` draws, and load-bearing for the same reason one level
    up: this is the ONLY record of dispositions whose decision is gone, so a damaged file must
    never read as an empty one - `record_orphan_receipt` refuses to write over it, which is
    what keeps the raw evidence in place instead of trading it for a ledger that just lost
    every row it held."""
    try:
        raw = _orphan_receipts_path(repo_path, slug).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError:
        return {"unreadable": True}
    try:
        ledger = json.loads(raw)
    except ValueError:                  # covers JSON and UnicodeDecodeError both
        return {"unreadable": True}
    return ledger if isinstance(ledger, dict) else {"unreadable": True}


def record_orphan_receipt(repo_path: str, candidate_id: str, entry_id: str,
                          disposition: str) -> bool:
    """File the terminal receipt for a candidate whose owning decision no longer exists. True
    only when that receipt is DURABLE - which is the caller's licence to delete the raw events.

    An orphan is the one settle with nowhere to file its summary: the decision it was reviewed
    as is gone, so `store.record_evidence_summary` has no entry to write to. Deleting anyway
    left an acknowledged event with no record of it anywhere, which runbook invariants 3 and 4
    forbid, so the receipt moves to this ledger instead.

    Deliberately NOT a `.gap` bump. That marker records evidence this module failed to record
    or that aged out unconsumed; this event was recorded, aggregated, held and dispositioned,
    and only its decision went missing. Filing it as a spool loss would claim capture failed
    when the history says the opposite.

    IDEMPOTENT on `candidate_id`: an already-filed candidate returns True without appending, so
    a crash between the receipt and the delete resumes without a second row, and a repeated
    sweep of a hold that resisted removal never grows the ledger.
    """
    _checked_id(candidate_id, "candidate_id")
    if disposition not in DISPOSITIONS:
        raise ValueError(f"disposition must be one of {sorted(DISPOSITIONS)}, "
                         f"got {disposition!r}")
    ledger = _read_orphan_receipts(repo_path)
    if ledger.get("unreadable"):
        return False
    rows = [row for row in ledger.get("receipts") or [] if isinstance(row, dict)]
    if any(str(row.get("candidate_id") or "") == candidate_id for row in rows):
        return True
    rows.append({
        "candidate_id": candidate_id,
        "entry_id": entry_id,
        "disposition": disposition,
        "event_ids": _held_event_ids(_held_dir(repo_path, candidate_id)),
        "occurred_at": _now().isoformat(),
        "reason": ORPHAN_REASON,
    })
    try:
        _write_json(_ensure_dir(_repo_dir(repo_path)), _ORPHAN_RECEIPTS_NAME,
                    {"receipts": rows[-MAX_ORPHAN_RECEIPTS:],
                     "dropped": _count(ledger.get("dropped"))
                     + max(0, len(rows) - MAX_ORPHAN_RECEIPTS)})
    except (OSError, TypeError, ValueError):
        return False
    return True


def finalize_candidate_evidence(repo_path: str, candidate_id: str, disposition: str) -> dict:
    """Settle a candidate: return the compact summary and delete its raw held events.

    `{"candidate_id", "disposition", "event_ids", "occurred_at"}` - the summary is the whole
    point, because the CALLER preserves it in the decision's own history, which is where a
    disposition belongs once the raw evidence is gone. `occurred_at` is when the candidate was
    SETTLED; the events' own times are already inside the decision they became.

    Idempotent: an already-absent directory is a success with no event ids, so a retried
    finalize is a no-op rather than an error.
    """
    _checked_id(candidate_id, "candidate_id")
    if disposition not in DISPOSITIONS:
        raise ValueError(f"disposition must be one of {sorted(DISPOSITIONS)}, "
                         f"got {disposition!r}")
    held = _held_dir(repo_path, candidate_id)
    event_ids = _held_event_ids(held)
    # Best-effort: a directory that resists removal shows up in the diagnostics rather than
    # failing a settle that has already produced its summary.
    shutil.rmtree(held, ignore_errors=True)
    return {"candidate_id": candidate_id, "disposition": disposition,
            "event_ids": event_ids, "occurred_at": _now().isoformat()}


# ── diagnostics ──────────────────────────────────────────────────────────────────

def _total_bytes(paths: list[Path]) -> int:
    total = 0
    for path in paths:
        try:
            total += path.stat().st_size
        except OSError:
            pass
    return total


def evidence_diagnostics(repo_path: str, slug: str = "") -> dict:
    """What the spool holds, for `contexer status`. Lock-free.

    `slug` addresses a spool whose repo path is not known - see `spool_slugs`. It is the only
    reader that takes one, and it never resolves a path when given one.

    `readable` is the honest half: an absent spool reports zeros with `readable=True`, while a
    directory that cannot be listed reports `readable=False` - so a reader is never told "no
    evidence" about a spool that could not be read.

    A failed listing therefore returns ZEROS rather than the counts gathered before it failed:
    a half-count beside `readable: False` is the same unreadable-versus-empty collapse this
    flag exists to prevent, one level down - a reader that only glances at `pending` would be
    told a number that describes part of the spool as though it described all of it.

    `bytes` counts every file the spool holds, `candidate.json` included: it is real disk the
    spool is responsible for, and a size report that quietly omitted its own bookkeeping would
    understate a repo with many held candidates.

    `held_unattributed` is the same honesty applied to the sweep's blind spot: a held candidate
    whose `candidate.json` is missing or unreadable records no `entry_id`, so
    `_sweep_orphan_holds` can never judge it and it is held for good. A caller that forgets to
    pass `meta` therefore accrues held directories nothing will ever clean up - this counter is
    what makes that show up in `contexer status` instead of accumulating silently. A candidate
    still short of review counts here too, and correctly: until it materializes there is no
    decision to attribute it to.

    `held_invalid_state` is the other unjudgeable shape: a manifest carrying a state this
    version does not recognize. Nothing resumes, sweeps or deletes it, so like the count above
    it exists to make a stuck candidate visible rather than silent.
    """
    counts = {"pending": 0, "held": 0, "held_events": 0, "held_unattributed": 0,
              "held_invalid_state": 0, "quarantine": 0, "bytes": 0}
    readable = True
    try:
        for key, directory in (("pending", _pending_dir(repo_path, slug)),
                               ("quarantine", _quarantine_dir(repo_path, slug))):
            if not directory.is_dir():
                continue
            files = _event_files(directory)
            counts[key] = len(files)
            counts["bytes"] += _total_bytes(files)
        root = _held_root(repo_path, slug)
        if root.is_dir():
            for directory in sorted(root.iterdir()):
                if not directory.is_dir():
                    continue
                files = _event_files(directory)
                counts["held"] += 1
                # The meta is the one name that legitimately sits beside the events, so it is
                # excluded from the EVENT count and included in the byte total.
                counts["held_events"] += sum(1 for p in files if p.name != _META_NAME)
                counts["bytes"] += _total_bytes(files)
                meta = _read_meta(directory)
                if not str(meta.get("entry_id") or ""):
                    counts["held_unattributed"] += 1
                if meta.get("invalid_state"):
                    counts["held_invalid_state"] += 1
    except OSError:
        counts = dict.fromkeys(counts, 0)
        readable = False
    return {**counts, "gap": _read_gap(repo_path, slug), "readable": readable}


# ── retention (reconciliation / maintenance only) ────────────────────────────────

def _unlink(path: Path) -> int:
    try:
        path.unlink()
        return 1
    except OSError:
        return 0


def _sweep_events(directory: Path) -> int:
    """Drop events past the age cap, then past the count cap. Returns how many went.

    BOTH caps measure the file's MTIME, never the timestamp in its own filename: the filename
    comes from the event's `occurred_at`, which the event itself supplies, so ordering
    evictions by it would let content decide retention (ruling R29). Concretely, a
    clock-skewed host emitting `occurred_at: 2030-01-01` would sit permanently at the end of
    the filename order and outlive every honestly-stamped event beside it, while a backdated
    one would always be first to go however recently it arrived.

    The trade-off this accepts: eviction order (arrival) and listing order (event time) can
    disagree, so a full spool can drop from the MIDDLE of what a reader would consume. That is
    the right way round - listing order is consume ergonomics, retention is about which
    evidence is genuinely stalest, and only one of the two can be decided by the writer.

    A file that cannot be stat-ed is KEPT by both caps: fail-soft means never dropping
    evidence over a failure to measure it.
    """
    if not directory.is_dir():
        return 0
    cutoff = time.time() - _MAX_PENDING_AGE_DAYS * 86400
    dropped, survivors, dated = 0, 0, []
    for path in _event_files(directory):
        try:
            mtime = path.stat().st_mtime
        except OSError:
            survivors += 1              # unmeasurable, so never a count-cap victim either
            continue
        if mtime < cutoff:
            dropped += _unlink(path)
        else:
            survivors += 1
            dated.append((mtime, path))
    # RESIDUAL, named rather than glossed: the sort is stable, so two files with the SAME mtime
    # keep listing order - which is filename order, which is `occurred_at` order, i.e. the very
    # content influence the distinct-mtime case above removes. At equal mtimes the spool has no
    # arrival fact left to decide with, and a tie-break invented here would be arbitrary; the
    # real fix is a monotonic arrival counter stamped at write time, not a cleverer sort.
    for _mtime, path in sorted(dated, key=lambda item: item[0])[:max(0, survivors
                                                                     - _MAX_PENDING_EVENTS)]:
        dropped += _unlink(path)
    return dropped


def _sweep_temp(root: Path) -> int:
    """Remove temp files left behind by an interrupted write. Anything younger than the age
    bound may be a rename still in flight, so it is left alone.

    Matched on the `.tmp` SUFFIX rather than on `_TEMP_PREFIX`, because two writers leave
    debris here and only one of them uses that prefix: `_write_json` mints `tmp-*.tmp`, while
    `.gap` goes through `store.atomic_write`, which mints `<name>.<random>.tmp` - so a crash
    mid-gap-write left a `.gap.*.tmp` file no sweep would ever remove. The suffix is what both
    actually share, and no event file ends in it (they are all `.json`)."""
    removed = 0
    cutoff = time.time() - _MAX_TEMP_AGE_SECONDS
    for path in sorted(root.rglob("*.tmp")):
        try:
            if not path.is_file() or path.stat().st_mtime >= cutoff:
                continue
        except OSError:
            continue
        removed += _unlink(path)
    return removed


def _sweep_orphan_holds(repo_path: str) -> tuple:
    """Settle every held candidate whose decision exists NOWHERE any more.
    `(finalized_ids, unreceipted_ids)`.

    Its events would otherwise be held forever: nothing re-aggregates them (they are out of
    `pending/`) and nothing will ever finalize them (the decision they were reviewed as is
    gone). Deliberately NO store lock - a stale read costs one deferred sweep, never
    correctness, and a candidate whose meta names no entry is left alone rather than guessed
    at, which is what keeps "held is exempt while unsettled" true.

    A TOMBSTONED decision is not gone, and the distinction is ruling R25's: a retired decision
    is exactly the outcome a lifecycle candidate PROPOSED, so reconciliation settles that hold
    `approved` and writes the summary onto the tombstone. This sweep can only ever say
    `dismissed`, and it writes no summary at all - so treating a tombstoned entry as an orphan
    would race reconciliation for the one disposition the lifecycle lane exists to record and
    destroy it. Left held; the next reconciliation pass settles it properly. Once the tombstone
    itself is evicted, the decision really is gone and the hold is swept then.

    The receipt comes FIRST and the delete only follows a durable one (`record_orphan_receipt`).
    There is no decision left to carry an `evidence_summary`, so that ledger is the whole
    terminal record: without it this sweep destroyed acknowledged evidence and left nothing
    anywhere saying it had existed. A receipt that cannot be written leaves the hold exactly as
    it is and is REPORTED, so the pass says it was incomplete rather than settling silently.
    """
    try:
        live = {str(e.get("id") or "") for e in store.load(repo_path).get("entries", [])
                if isinstance(e, dict)}
        live |= {str(e.get("id") or "") for e in store.load_deleted(repo_path).get("entries", [])
                 if isinstance(e, dict)}
    except Exception:                   # broad on purpose: a sweep never breaks its caller
        return [], []
    finalized, unreceipted = [], []
    for candidate_id, meta in held_candidates(repo_path).items():
        entry_id = str(meta.get("entry_id") or "")
        # An unknown state is as unjudgeable as a missing `entry_id`: this sweep can only ever
        # say `dismissed`, and doing so over a phase nothing here understands would settle a
        # candidate that may be mid-materialization.
        if meta.get("invalid_state") or not entry_id or entry_id in live:
            continue
        if not record_orphan_receipt(repo_path, candidate_id, entry_id, "dismissed"):
            unreceipted.append(candidate_id)
            continue
        finalize_candidate_evidence(repo_path, candidate_id, "dismissed")
        finalized.append(candidate_id)
    return finalized, unreceipted


def run_retention(repo_path: str) -> dict:
    """Bound the spool. `{"dropped_pending", "dropped_quarantine", "temp_removed",
    "finalized_orphans", "orphans_unreceipted", "errors"}`.

    `orphans_unreceipted` names every orphaned hold left standing because its terminal receipt
    could not be written. It is not an error - nothing was lost - but the pass did not finish
    what it set out to do, so a caller reports itself incomplete on it.

    The ONLY caller-facing retention entry point, and it SCANS - so it runs from
    reconciliation or maintenance and never from an editor hook. Held events are exempt while
    their candidate is unsettled.

    The two drops it makes are recorded as DIFFERENT facts (see `_bump_gap`), decided by which
    directory was swept rather than by anything inside the events: an aged-out PENDING event was
    never consumed, while an aged-out QUARANTINED one is evidence that could not be read at all.
    Only the second is this module failing at its job, which is why reporting both as "lost" is
    what made `contexer status` accuse a healthy repo of losing evidence.

    An aged-out PENDING event is still not GOOD news. The queue's designed path is reconciliation
    at the next session start, which every host now reaches, so an event that survived to its
    retention age outlived every session start in that window - an anomaly to look into rather
    than the queue working as intended. See `_bump_gap` for the full note.
    """
    report = {"dropped_pending": 0, "dropped_quarantine": 0, "temp_removed": 0,
              "finalized_orphans": [], "orphans_unreceipted": [], "errors": []}
    try:
        root = _repo_dir(repo_path)
        if not root.is_dir():
            return report
        report["temp_removed"] = _sweep_temp(root)
        report["dropped_pending"] = _sweep_events(_pending_dir(repo_path))
        report["dropped_quarantine"] = _sweep_events(_quarantine_dir(repo_path))
        report["finalized_orphans"], report["orphans_unreceipted"] = \
            _sweep_orphan_holds(repo_path)
    except Exception as exc:            # broad on purpose: a report, not a traceback
        report["errors"].append(f"{type(exc).__name__}: {exc}")
    if report["dropped_pending"]:
        _bump_gap(repo_path, "retention_unconsumed", report["dropped_pending"],
                  field="expired")
    if report["dropped_quarantine"]:
        _bump_gap(repo_path, "retention_quarantine", report["dropped_quarantine"])
    return report


def _maintenance_stamp(repo_path: str) -> Path:
    """Same key as every other periodic-sweep stamp here: `store.repo_slug` over the repo the
    caller already resolved, so writer and reader agree without either re-resolving (see
    `anchors._anchor_verify_stamp_path`)."""
    return store.STORE_DIR / f".spool_maintained_{store.repo_slug(repo_path)}"


def maintain_spool(repo_path: str, force: bool = False) -> dict:
    """Periodic retention and the orphan-hold sweep, on a TTL. The retention report, or `{}`
    when the run was skipped. NEVER raises.

    It was introduced as the retention path for an emit-only host, back when Codex reached no
    reconciliation entrypoint and Cursor emitted directives only. Every host now reconciles at
    session start, and reconciliation runs `run_retention` itself - so this is no longer the
    ONLY thing keeping `pending/` bounded, and it is not redundant either. It is the sweep that
    runs INDEPENDENTLY of whether a pass happened: reconciliation skips on lock contention,
    returns early on an empty spool, and `_sweep_orphan_holds` has no other caller at all.
    Session start is not an editor hook, so the never-scan-in-a-hook rule holds; but it IS every
    session, so the scan is gated twice: nothing happens at all until a spool directory exists,
    and after that at most once per `_MAINTENANCE_TTL`.

    The TTL is not only about cost. `_sweep_orphan_holds` judges a held candidate against the
    LIVE store, where a retired decision is absent - which reconciliation reads as its
    lifecycle candidate being approved and this sweep would settle as dismissed. Reconciliation
    settles its own holds first on every pass it runs, so keeping this sweep to once a day is
    what keeps it the safety net it is meant to be rather than a racing second judge.
    """
    try:
        if not _repo_dir(repo_path).is_dir():
            return {}
        stamp = _maintenance_stamp(repo_path)
        if not force:
            mtime = store.file_mtime(stamp)
            if mtime is not None and time.time() - mtime < _MAINTENANCE_TTL:
                return {}
        try:
            store.STORE_DIR.mkdir(mode=_DIR_MODE, exist_ok=True)
            stamp.touch()
        except OSError:
            pass                        # an unwritable stamp costs a re-scan, never the sweep
        return run_retention(repo_path)
    except Exception:                   # broad on purpose: never breaks a session start
        return {}
