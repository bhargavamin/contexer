"""Explicit share: push a local decision up to the Teams cloud context (C4).

Path B, the write counterpart to team_context.pull. Sharing is an EXPLICIT verb - never
auto-shares on capture. v1 syncs to your PERSONAL cloud context (push_decision auto-approves
it); a team `shared_candidate` awaits a team-scoped push endpoint (future Track A).

NOTE: the profile -> RemoteStore -> canonical_repo_key(git origin) boilerplate is duplicated
with team_context.pull; DRY into one helper once C4 and C5 are both merged.

Outbox: a failed push (cloud unreachable, or auth rejected) must not lose the user's intent
to share. `share()` queues the payload in a durable, GLOBAL outbox file (one file for every
repo - entries carry their own repo key) instead of dropping it, and `drain_outbox` retries
the queue FIFO. The outbox is drained from two places: at the start of every `share()` call
(so queued items go out before the new one) and from `team_context.refresh` (the SessionStart
seam every adapter already funnels through), so a queued share retries automatically the next
time the user starts a session - no manual retry required.

Shared-marker sidecar: a second GLOBAL file (`.shared.jsonl`, separate from the outbox) records
which decisions have already been successfully pushed, namespaced by endpoint so switching
endpoints (e.g. local -> prod) never shows a stale/false "already shared" marker - the team
cache had exactly this endpoint-contamination bug once (a prod cursor confusing a local pull);
this sidecar is scoped from the start to avoid repeating it. Purely cosmetic (the `contexer
share` picker's `✓ shared` hint) - see `_mark_shared`/`shared_map`.
"""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
import json
import threading
import time
import uuid
import weakref
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from contexer import share_status, sidecars, store
from contexer.config import Profile, load_profile
from contexer.remote import (
    DecisionReconciliationPreview,
    RemoteAuthError,
    RemoteRateLimitError,
    RemoteStore,
    RemoteStoreError,
    RemoteTeam,
    RemoteUnavailableError,
    TeamSubmissionResult,
    _reconciliation_wire_body,
    awith_local_fallback,
    with_local_fallback,
)
from contexer.repo_key import canonical_repo_key

# THE definition of "this refusal is transient", and the only one. A transient refusal keeps a
# confirmed write queued so a later retry can land it; anything else is terminal, and the drain
# moves that entry to `attention`. `RemoteRateLimitError` is what makes the rate-limit arm
# structural - the three copies of `"rate" in str(exc) and "limit" in str(exc)` that used to
# decide this lived here, in the retry-versus-drop path for a write the developer had already
# confirmed. See remote.py.
#
# The value is the (queued, could-not-queue) outcome pair, so ONE table serves both decision
# sites: the drain asks only "is this transient?" through _RETRYABLE_REFUSALS, while
# submit_reconciliation needs the pair too. Those two used to enumerate the same three classes
# separately, so adding a fourth transient class to the tuple would have left
# submit_reconciliation reporting it as terminal - "Nothing was queued" - for a confirmed write.
# Every key is a direct sibling under RemoteStoreError, so first-match lookup is unambiguous.
_TRANSIENT_REFUSALS: dict[type[RemoteStoreError], tuple[str, str]] = {
    RemoteUnavailableError: (
        share_status.UNREACHABLE_QUEUED, share_status.UNREACHABLE_NOT_QUEUED),
    RemoteAuthError: (
        share_status.UNREACHABLE_QUEUED, share_status.UNREACHABLE_NOT_QUEUED),
    RemoteRateLimitError: (
        share_status.RATE_LIMITED_QUEUED, share_status.RATE_LIMITED_NOT_QUEUED),
}
_RETRYABLE_REFUSALS = tuple(_TRANSIENT_REFUSALS)


def _transient_outcomes(exc: BaseException) -> tuple[str, str] | None:
    """The (queued, could-not-queue) outcomes for a transient refusal, else None for a terminal
    one. Derived from `_TRANSIENT_REFUSALS`, so the transient set has exactly one home."""
    for cls, outcomes in _TRANSIENT_REFUSALS.items():
        if isinstance(exc, cls):
            return outcomes
    return None

# Outbox cap: push_decision is idempotent on decision_id server-side, so a queued entry is
# never dropped for age or attempt count in v1 - retrying is always safe. This count is the
# only bound, so a long-offline stretch can't grow ~/.contexer/.outbox.json without limit.
_OUTBOX_CAP = 50
_OUTBOX_LOCK_SLUG = ".outbox"
_OUTBOX_LOCAL_LOCK = threading.Lock()
_OUTBOX_LOCK_DEPTH = contextvars.ContextVar("contexer_outbox_lock_depth", default=0)
_OUTBOX_ASYNC_LOCKS: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()
_OUTBOX_ASYNC_LOCKS_GUARD = threading.Lock()
# How long an async waiter sleeps between attempts at the outbox lock. Small enough that the
# wait is not noticeable next to the network push it precedes, large enough that a contended
# wait is not a spin.
_OUTBOX_LOCK_POLL_SECONDS = 0.02


@dataclass
class ReconciliationPlan:
    decision: dict
    repo_key: str | None
    target: RemoteTeam
    remote: RemoteStore
    preview: DecisionReconciliationPreview | None
    atomic: bool
    idempotency_key: str
    redact_on: bool | None = None

# Max decisions per push_decisions batch. MUST stay <= contexer-teams' PUSH_DECISIONS_MAX (50):
# the server rejects a larger array, so bulk shares / drains chunk into calls of this size. One
# call per chunk (not per decision) is what stops a bulk share from trickling into the web UI one
# row at a time. Kept in sync with the cloud like _WIRE_SOURCES below.
_BATCH_SIZE = 50

# The cloud's push_decision validates `source` against this exact CLOSED allowlist (a z.enum)
# and rejects anything else with a hard -32602 that silently poisons the outbox forever. Kept
# in sync with contexer-teams' PUSH_DECISION_SOURCES: the five canonical sources plus `plan`
# (OSS provisional provenance), which the cloud accepts and stores as-is since contexer-teams#91.
_WIRE_SOURCES = frozenset({"ai", "human", "scan", "bootstrap", "memory", "plan"})


def _wire_source(source: str | None) -> str | None:
    """Coerce a local decision's `source` onto the cloud's accepted taxonomy.

    The cloud's push_decision accepts only _WIRE_SOURCES and rejects anything else with a hard
    -32602 that silently poisons the outbox forever. `plan` (a provisional maturity marker that
    leaks from `created_by` into a revision's `source`) is accepted and PRESERVED end-to-end, so
    the provisional signal reaches the cloud intact. Any *other* off-taxonomy *string* degrades
    to "ai" - a safe accepted value - so an unknown source can never brick the outbox. `None`
    passes through unchanged: push_decision OMITS source when it is None (the cloud stores NULL =
    unknown provenance), so None must not be fabricated into a false "ai" provenance."""
    if source is None or source in _WIRE_SOURCES:
        return source
    return "ai"


def _outbox_path():
    # Computed at call time (not module import time) so tests that monkeypatch
    # store.STORE_DIR see the redirected path, like every other store-adjacent file.
    return store.STORE_DIR / sidecars.filename("outbox")


def _reconcile_outbox_path():
    return store.STORE_DIR / sidecars.filename("reconcile_outbox")


def _read_reconcile_outbox() -> tuple[list[dict], str | None]:
    path = _reconcile_outbox_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], None
    except (OSError, UnicodeDecodeError) as exc:
        return [], f"{type(exc).__name__}: {exc}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [], f"{type(exc).__name__}: {exc}"
    if not isinstance(data, list):
        return [], f"not a reconciliation outbox list (got {type(data).__name__})"
    return data, None


def _load_reconcile_outbox() -> list[dict]:
    return _read_reconcile_outbox()[0]


def _save_reconcile_outbox(entries: list[dict]) -> None:
    store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    store.atomic_write(
        _reconcile_outbox_path(), json.dumps(entries, indent=2, ensure_ascii=False))


def _enqueue_reconciliation(operation: dict) -> None:
    """Persist one CONFIRMED atomic operation. Dedupe by local decision+team: a later explicit
    reconciliation supersedes an older queued intent, while its idempotency key stays stable for
    every automatic retry of this exact row."""
    with outbox_lock():
        key = (operation.get("decision_id"), operation.get("team_id"))
        loaded, error = _read_reconcile_outbox()
        if error is not None:
            raise RuntimeError(f"cannot read reconciliation retry queue: {error}")
        entries = [e for e in loaded if (e.get("decision_id"), e.get("team_id")) != key]
        if len(entries) == len(loaded) and len(entries) >= _OUTBOX_CAP:
            raise RuntimeError("reconciliation retry queue is full")
        entries.append(operation)
        _save_reconcile_outbox(entries)


def _acquire_outbox_lock(*, blocking: bool = True) -> contextlib.ExitStack:
    """Take the process-local mutex AND the cross-process file lock; return what releases both.

    Raises `BlockingIOError` when `blocking=False` and either is already held, leaving nothing
    acquired. Split out of `outbox_lock` so the reentrancy depth ContextVar is set by the CALLER,
    in the caller's own context: the async path needs to acquire without blocking its loop, and a
    token set anywhere else than the awaiting coroutine's own context would be invisible to it -
    a nested `_enqueue` would then read depth 0 and try to take the non-reentrant `store_lock` a
    second time, deadlocking against itself."""
    stack = contextlib.ExitStack()
    try:
        if not _OUTBOX_LOCAL_LOCK.acquire(blocking=blocking):
            raise BlockingIOError("the outbox lock is held by another thread")
        stack.callback(_OUTBOX_LOCAL_LOCK.release)
        stack.enter_context(store.store_lock(_OUTBOX_LOCK_SLUG, blocking=blocking))
    except BaseException:
        stack.close()
        raise
    return stack


@contextlib.contextmanager
def outbox_lock():
    """Serialize an account transition against outbox writers and drains."""
    depth = _OUTBOX_LOCK_DEPTH.get()
    if depth:
        token = _OUTBOX_LOCK_DEPTH.set(depth + 1)
        try:
            yield
        finally:
            _OUTBOX_LOCK_DEPTH.reset(token)
        return
    with _acquire_outbox_lock():
        token = _OUTBOX_LOCK_DEPTH.set(1)
        try:
            yield
        finally:
            _OUTBOX_LOCK_DEPTH.reset(token)


def _async_outbox_loop_lock() -> asyncio.Lock:
    loop = asyncio.get_running_loop()
    with _OUTBOX_ASYNC_LOCKS_GUARD:
        lock = _OUTBOX_ASYNC_LOCKS.get(loop)
        if lock is None:
            lock = asyncio.Lock()
            _OUTBOX_ASYNC_LOCKS[loop] = lock
        return lock


async def _await_outbox_lock() -> contextlib.ExitStack:
    """Wait for the outbox lock without blocking the event loop, one poll at a time.

    Every wait is an `await`, so the caller's deadline can cancel it (`server.share_decision`
    wraps the share in `asyncio.wait_for`) and every other tool on the loop keeps running while
    this one waits. Unbounded on purpose: the semantics are the blocking lock's - wait until you
    get it - and the bound belongs to the caller's own timeout, as it already does.

    Accepted trade: a poller can lose repeatedly to a blocking waiter that is queued in the
    kernel. Both holders here take the lock for one short file rewrite, so the wait is bounded in
    practice by the other side finishing, not by fairness."""
    while True:
        try:
            return _acquire_outbox_lock(blocking=False)
        except BlockingIOError:
            await asyncio.sleep(_OUTBOX_LOCK_POLL_SECONDS)


@contextlib.asynccontextmanager
async def async_outbox_lock():
    """Async task-safe wrapper for the same outbox critical section.

    Held across an `await`, so it is entered without ever blocking the loop (see
    `_await_outbox_lock`). RELEASE stays synchronous: `flock(LOCK_UN)` never waits, and awaiting
    in the `finally` would let a cancellation arriving at that moment skip the release and strand
    the lock for every other process."""
    depth = _OUTBOX_LOCK_DEPTH.get()
    if depth:
        token = _OUTBOX_LOCK_DEPTH.set(depth + 1)
        try:
            yield
        finally:
            _OUTBOX_LOCK_DEPTH.reset(token)
        return
    async with _async_outbox_loop_lock():
        stack = await _await_outbox_lock()
        try:
            # Set in THIS coroutine's context, so a nested sync `outbox_lock()` (every
            # `_enqueue` on this path takes one) sees the depth and re-enters instead of
            # deadlocking on the lock this frame already holds.
            token = _OUTBOX_LOCK_DEPTH.set(1)
            try:
                yield
            finally:
                _OUTBOX_LOCK_DEPTH.reset(token)
        finally:
            stack.close()


def _read_outbox() -> tuple[list[dict], str | None]:
    """(queued entries, read error) from ONE read of the outbox.

    The degrade-but-report split `store._read_global` carries, for the same reason: `_load_outbox`
    answers "no queued shares" for a missing file AND for one that exists but cannot be parsed,
    and `error` is the ONLY thing that tells those apart. Every RENDER path wants the fail-soft
    view. A SAFETY gate does not: `discard_outbox` reporting "the queue is clear" off an
    unreadable file would leave the previous account's entries on disk for the post-login drain
    to find once the transient cleared - which is precisely the egress it exists to prevent."""
    path = _outbox_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return [], None
    except (OSError, UnicodeDecodeError) as exc:
        return [], f"{type(exc).__name__}: {exc}"
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return [], f"{type(exc).__name__}: {exc}"
    if not isinstance(data, list):
        return [], f"not an outbox list (got {type(data).__name__})"
    return data, None


def _load_outbox() -> list[dict]:
    """Read the outbox; a missing or corrupt file reads as empty, never raises. The read every
    drain/enqueue path wants. A caller that must tell empty from unreadable uses `_read_outbox`."""
    return _read_outbox()[0]


def _save_outbox(entries: list[dict]) -> None:
    store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    store.atomic_write(_outbox_path(), json.dumps(entries, indent=2, ensure_ascii=False))


def discard_outbox() -> tuple[int, int]:
    """Locked wrapper for dropping every queued share."""
    with outbox_lock():
        discarded, remaining = _discard_outbox_unlocked()
        r_discarded, r_remaining = _discard_reconcile_outbox_unlocked()
        if remaining < 0 or r_remaining < 0:
            return discarded + r_discarded, -1
        return discarded + r_discarded, remaining + r_remaining


def _discard_reconcile_outbox_unlocked() -> tuple[int, int]:
    """Clear confirmed team writes on account switch with the same fail-closed contract."""
    entries, error = _read_reconcile_outbox()
    if error is None and not entries:
        return 0, 0
    queued = len(entries)
    try:
        _reconcile_outbox_path().unlink()
    except OSError:
        try:
            _save_reconcile_outbox([])
        except Exception:
            pass
    after, after_error = _read_reconcile_outbox()
    if after_error is None and not after:
        return queued, 0
    if after_error is not None:
        return 0, -1
    return queued - len(after), len(after)


def _discard_outbox_unlocked() -> tuple[int, int]:
    """Drop every queued share; return (discarded, still_queued).

    Called by `auth.login` when a new account signs in (issue #232). A queued entry carries no
    account identity - `_payload` stores the decision, its repo key and provenance, and nothing
    about who was signed in when it was queued - so after a switch the drain that `login` itself
    triggers (`cli._post_login_sync` -> `team_context.refresh`, which drains) would push the
    PREVIOUS account's queued decisions up as the NEW account's rows. That is an outward,
    hard-to-undo write into the wrong team's context.

    Discarding loses share intent, which is real: a same-account re-login drops whatever was
    waiting. It is still the right side of the trade, and the asymmetry is the same one
    `forget_shared_markers` documents - a discarded queue is visible (login SAYS how many went)
    and recoverable by re-running `contexer share`, while an upload into the wrong account is
    silent and cannot be taken back. Sizing the loss is why the discarded count is returned
    rather than a bool.

    `still_queued` is the second half, and it is NOT decoration: this is the one cleanup in the
    family whose failure is not benign. A stranded pull cache renders a stale row; a stranded
    OUTBOX egresses to the wrong account, which is exactly what this function exists to stop -
    so silently swallowing an unlink error (as the first cut did) left the hole it was closing
    wide open, and the caller must be able to tell. Removal is therefore attempted twice, by
    different syscalls: `unlink`, then a truncate to `[]` through the module's normal atomic
    writer, since a file that resists one can still yield to the other. The queue is re-read
    afterwards rather than inferred, so the count reported is measured, not assumed.

    A non-zero `still_queued` means the caller MUST NOT let a drain run this session
    (`auth.login` warns and skips the post-login sync). Both attempts failing usually means the
    store dir itself is unwritable, which nothing here can repair - the honest move is to say so
    and stop, not to proceed as though the queue were clear. `-1` is that same verdict when the
    file is there but unreadable: the count is unknown, the danger is not.

    Every decision here reads `_read_outbox`, never `_load_outbox`. The fail-soft view answers
    "no queued shares" for a file that merely failed to PARSE, so gating on it would skip the
    removal AND report the queue clear, leaving the entries for the post-login drain to find
    once the transient cleared. Existence, not parseability, is what makes a queue dangerous.

    Deliberately NOT called from `logout`: nothing drains without credentials, so `login` is the
    only chokepoint an entry can egress through, and clearing at logout as well would discard
    queues that were never in danger."""
    entries, error = _read_outbox()
    if error is None and not entries:
        return 0, 0            # genuinely nothing queued - the overwhelmingly common case
    queued = len(entries)      # 0 when unreadable; the message under-reports, the gate does not
    path = _outbox_path()
    try:
        path.unlink()
    except OSError:
        # Second attempt down a different syscall path (temp file + os.replace). An emptied
        # outbox is as safe as an absent one - both read back as no queued shares.
        try:
            _save_outbox([])
        except Exception:
            pass
    after, after_error = _read_outbox()
    if after_error is None and not after:
        return queued, 0
    if after_error is not None:
        return 0, -1           # still there and unreadable: unknown count, known danger
    return queued - len(after), len(after)


def _enqueue(payload: dict) -> None:
    """Locked wrapper for queueing one failed push."""
    with outbox_lock():
        _enqueue_unlocked(payload)


def _enqueue_unlocked(payload: dict) -> None:
    """Queue a failed push. Dedupes by decision_id (re-sharing the same decision while
    offline replaces the queued entry - fresh content wins) and caps at _OUTBOX_CAP,
    dropping the oldest entries beyond that.

    Reads through `_read_outbox` and REFUSES on a read error rather than writing. This used to
    read through the fail-soft `_load_outbox`, which answers "empty" for a file it cannot parse,
    so queueing one share against a damaged outbox saved "empty plus the new row" over the top
    and every share already waiting in it was gone, silently. `_read_outbox`'s own docstring
    states the rule this now follows - the fail-soft view is for RENDER paths, never for one that
    decides an action - and `_enqueue_reconciliation` already raises for the identical failure on
    the other queue in this module.

    Raising is the right shape here because every caller already handles it and already reports
    it honestly: `_finish_share` returns "Your local decision is unchanged" instead of promising a
    retry, `_requeue_skipped` counts the row as lost, `_queue_rest_status` says the queue stopped,
    and `server.share_decision`'s timeout path is explicitly best-effort. The honest message
    already existed; this failure mode simply could never reach it."""
    loaded, error = _read_outbox()
    if error is not None:
        raise RuntimeError(f"cannot read the share retry queue: {error}")
    entries = [e for e in loaded if e.get("decision_id") != payload.get("decision_id")]
    entries.append(payload)
    if len(entries) > _OUTBOX_CAP:
        entries = entries[-_OUTBOX_CAP:]
    _save_outbox(entries)


# ── shared-marker sidecar (separate GLOBAL file from the outbox above) ─────────────
# Records successful pushes so the picker can show "✓ shared". Namespaced by endpoint:
# {"endpoints": {"<endpoint>": {"<decision_id>": "<iso8601>"}}}. Same call-time-path /
# fail-soft-read / atomic-write conventions as the outbox helpers above - a marker is
# purely cosmetic and must NEVER break or block a push.

# The marker sidecar is an APPEND-ONLY log, not a rewritten document. A marker is monotonic
# (a decision pushed to an endpoint stays pushed), so recording one never needs to read what
# is already there - which removes the read-modify-write entirely and with it the lost-update
# race, on EVERY platform rather than only where POSIX advisory locks exist. Each line is one
# self-contained {"endpoint", "id", "at"} record; a reader folds the log into a map, last
# write winning per (endpoint, id).
_SHARED_LOG_MAX_LINES = 2000  # compaction threshold, so the log can't grow without bound
# Lock slug shared by _append_shared and _compact_shared - the two must never overlap.
# `store_lock` is NOT reentrant (a second acquire in the same process blocks on its own
# lock), so these two must stay strictly sequential, never nested. See _mark_shared.
_SHARED_LOCK_SLUG = ".shared"


def _shared_path():
    # Computed at call time (not module import time), same convention as _outbox_path -
    # tests that monkeypatch store.STORE_DIR see the redirected path.
    return store.STORE_DIR / sidecars.filename("shared_markers")


def forget_shared_markers() -> bool:
    """Delete the `✓ shared` marker log; True if one was there. Fail-soft.

    Called when the logged-in account changes (issue #232). The log is namespaced by ENDPOINT
    alone, and two accounts normally sit behind the same endpoint, so markers written under one
    read as "already shared" under the other. Namespacing by account instead is the real fix and
    needs an account identity that does not exist on disk yet; until then the marker is dropped.

    Dropping rather than keeping is the right direction because the two errors are not
    symmetric: a false "already shared" invites a developer to skip a decision the new account
    genuinely does not have, while a missing marker only costs a re-push, which is idempotent on
    `decisionId`. `share --all` ignores this file entirely either way."""
    try:
        _shared_path().unlink()
        return True
    except OSError:   # includes FileNotFoundError - nothing to forget
        return False


def _load_shared() -> dict:
    """Fold the append-only log into {endpoint: {decision_id: iso8601}}. A missing file, a
    corrupt file, or an unparseable line is skipped - never raises."""
    path = _shared_path()
    out: dict[str, dict[str, str]] = {}
    if not path.exists():
        return {"endpoints": out}
    try:
        raw = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return {"endpoints": out}
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # a torn/garbled line loses one marker, never the whole log
        if isinstance(rec, dict) and rec.get("endpoint") and rec.get("id"):
            out.setdefault(str(rec["endpoint"]), {})[str(rec["id"])] = str(rec.get("at") or "")
    return {"endpoints": out}


def _append_shared(records: list[dict]) -> None:
    """Append records in ONE write call. Concurrent appends interleave by line rather than
    clobbering each other, so the append itself needs no lock to keep both writers' markers.

    The lock is taken anyway, for ONE case the append-only shape can't cover: compaction
    replaces the file wholesale, so an append landing between compaction's fold and its
    atomic rename would write to an inode about to be discarded. Holding the same lock as
    `_compact_shared` makes append and compaction mutually exclusive. Where locks are
    unavailable (non-POSIX) `store_lock` is a no-op and the append still can't clobber a
    peer append - only the rare compaction overlap stays exposed, which is cosmetic."""
    store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    path = _shared_path()
    blob = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    with store.store_lock(_SHARED_LOCK_SLUG):
        # Heal a missing trailing newline first: appending straight onto a torn/partial last
        # line (a half-written record, or a hand-edited file) would fuse it with our first
        # record and lose BOTH. Starting on a fresh line costs one byte and confines the
        # damage to the pre-existing partial line, which the reader already skips.
        try:
            needs_nl = path.exists() and path.stat().st_size > 0 and \
                path.read_bytes()[-1:] != b"\n"
        except OSError:
            needs_nl = False
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(("\n" if needs_nl else "") + blob)


def _compact_shared() -> None:
    """Rewrite the log as one record per (endpoint, id) once it grows past the threshold.
    Rare, and mutually exclusive with `_append_shared` via the shared lock, so no marker is
    lost to an append racing the rewrite. Best-effort: if locks are unavailable the rewrite
    still can't corrupt the log (it is an atomic replace), only drop a marker written inside
    the window - cosmetic, since the decision merely re-shows as unshared.

    Reads through the fail-soft `_load_shared` on purpose, which is the one place in this
    module that is allowed to (contrast `_enqueue_unlocked`, which must RAISE): this is
    best-effort maintenance whose docstring already promises never to surface to the caller,
    and its payload is a display hint, not queued work. Flagged as a possible second
    `_enqueue`-shaped case during review, so the reasoning is recorded here rather than
    re-derived."""
    path = _shared_path()
    try:
        if not path.exists() or len(path.read_text(encoding="utf-8").splitlines()) <= _SHARED_LOG_MAX_LINES:
            return
        with store.store_lock(_SHARED_LOCK_SLUG):
            folded = _load_shared()["endpoints"]
            lines = [{"endpoint": ep, "id": did, "at": at}
                     for ep, bucket in folded.items() for did, at in bucket.items()]
            store.atomic_write(
                path, "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in lines))
    except Exception:
        pass  # cosmetic maintenance - never surface to the caller


def _mark_shared(ids, endpoint: str | None) -> None:
    """Record successful pushes of `ids` to `endpoint`, namespaced so switching endpoints
    never shows a stale/false marker for a decision only ever pushed elsewhere. Fail-soft:
    a marker is cosmetic (the picker's "✓ shared" hint) and must NEVER break or block a
    push, so a missing endpoint, an empty `ids`, or any read/write problem is a silent no-op."""
    clean_ids = [str(i) for i in ids if i]
    if not endpoint or not clean_ids:
        return
    try:
        # Append-only: no read, so two concurrent shares (or a share racing an outbox drain)
        # can't clobber each other's markers - on every platform, not just where POSIX
        # advisory locks are available. See the log's design note above.
        # The two calls below are SEQUENTIAL, never nested: both take _SHARED_LOCK_SLUG and
        # the lock is not reentrant, so wrapping one in the other would self-deadlock.
        now = datetime.now(timezone.utc).isoformat()
        _append_shared([{"endpoint": endpoint, "id": did, "at": now} for did in clean_ids])
        _compact_shared()
    except Exception:
        pass  # cosmetic marker - never let a write problem surface to the caller


def shared_map(endpoint: str | None) -> dict[str, str]:
    """decision_id -> iso8601 timestamp of decisions already pushed to `endpoint` (empty
    dict if never pushed there, endpoint is falsy, or the sidecar can't be read)."""
    if not endpoint:
        return {}
    try:
        return dict(_load_shared()["endpoints"].get(endpoint, {}))
    except Exception:
        return {}


def _reconcile_with_disk(tail: list[dict], sent_ids: set) -> list[dict]:
    """Re-read the outbox immediately before the final save and fold in anything that
    only exists on disk. The normal POSIX path serializes outbox writers with `outbox_lock`,
    so this is mostly the non-POSIX fallback: when advisory locking is unavailable, a
    concurrent `_enqueue` between our initial `_load_outbox()` at the top of `drain_outbox`
    and this point writes straight to disk, so that payload is invisible to our in-memory
    `entries` and would otherwise be silently overwritten by this drain's final save.
    Disk-only entries are appended after `tail` since they were enqueued after this drain
    started, so FIFO order is preserved."""
    disk_entries = _load_outbox()
    tail_ids = {e.get("decision_id") for e in tail}
    extra = [d for d in disk_entries
             if d.get("decision_id") not in sent_ids and d.get("decision_id") not in tail_ids]
    return tail + extra


def _entry_push_kwargs(entry: dict) -> dict:
    """push_decision kwargs for one outbox entry. Shared by both drains (`_drain_outbox_unlocked`
    and `_adrain_outbox_unlocked`) so they serialize an entry identically (source coerced through
    _wire_source).

    `source_files` (issue #174 Task 5) is read back off the queued entry unconditionally - the
    wire gate `remote._wire_args`/`_WIRE_SOURCE_FILES` is read at CALL time, i.e. AT DRAIN, not
    at the time this entry was queued. So an entry queued before the gate opened drains with its
    files intact, no re-queue or schema migration needed; conversely a rollback before this entry
    drains means it won't egress the field, exactly as if it had never been gated on."""
    return dict(
        type=entry.get("type"), content=entry.get("content"), repo=entry.get("repo"),
        rationale=entry.get("rationale"), confidence=entry.get("confidence"),
        evidence=entry.get("evidence"), source=_wire_source(entry.get("source")),
        decision_id=entry.get("decision_id"), title=entry.get("title"),
        source_files=entry.get("source_files"))


def _dec_push_kwargs(dec: dict, key) -> dict:
    """push_decision kwargs for one shareable decision. Shared by share / share_all /
    share_async so every share path puts the same decision on the wire identically.

    `source_files` passes through from the projection; whether it actually reaches the wire is
    decided later, at `remote._wire_args` time, by `remote._WIRE_SOURCE_FILES`."""
    return dict(
        type=dec["type"], content=dec["content"], repo=key,
        confidence=dec["confidence"], evidence=dec["evidence"],
        source=_wire_source(dec["source"]), decision_id=dec["id"], title=dec.get("title"),
        source_files=dec.get("source_files"))


def _finish_share(dec: dict, key, server_id,
                  endpoint: str | None = None) -> share_status.ShareStatus:
    """Turn one push outcome into a :class:`share_status.ShareStatus` (share + share_async).

    On failure (server_id is None: cloud unreachable OR auth rejected) enqueue the decision
    so a later drain retries it - either way a queued retry can succeed later, so both
    degradations queue rather than losing the share. On success, mark the decision shared
    (endpoint-scoped, cosmetic - never lets a marker failure affect the returned status)."""
    if server_id is None:
        try:
            _enqueue(_payload(dec, key))
        except Exception:
            # Even queueing can fail (disk full, temp-dir perms). Never raise - and the outcome
            # must not claim a retry that was never recorded, hence NOT_QUEUED, not QUEUED.
            return share_status.ShareStatus(share_status.NOT_QUEUED, lost=1, total=1)
        return share_status.ShareStatus(share_status.QUEUED, queued=1, total=1)
    _mark_shared([dec.get("id")], endpoint)
    return share_status.ShareStatus(
        share_status.SYNCED, sent=1, total=1, server_id=str(server_id))


# ── batch push (share_all / share_ids / drain, both twins) ───────────────────────────
# One push_decisions call per _BATCH_SIZE decisions instead of one push_decision per row, so a
# bulk share lands together instead of trickling into the web UI. The sync (_push_batch) and async
# (_apush_batch) forms mirror each other line-for-line except the awaited push, and share every
# outbox/status helper below so no logic drifts between them.

def _resolve_ids(repo_path: str, decision_ids: list) -> tuple[list[dict], list]:
    """Resolve a multi-pick to (projections, missing_ids). An unknown id is collected in `missing`
    so it can be REPORTED rather than silently dropped."""
    projs: list[dict] = []
    missing: list = []
    for did in decision_ids:
        proj = store.get_shareable(repo_path, str(did))
        if proj is None:
            missing.append(did)
        else:
            projs.append(proj)
    return projs, missing


def _requeue_skipped(chunk: list[dict], key, skipped_ids: list) -> tuple[int, int]:
    """Re-queue the capacity-skipped rows of a chunk; return (requeued, lost). A failed _enqueue
    (e.g. disk full) is COUNTED as lost, not swallowed - that row is neither stored remotely nor
    queued, so its share intent is genuinely gone and the status must say so."""
    skipped = set(skipped_ids)
    requeued = lost = 0
    for dec in chunk:
        if dec["id"] in skipped:
            try:
                _enqueue(_payload(dec, key))
                requeued += 1
            except Exception:
                lost += 1
    return requeued, lost


@dataclass
class _BatchCounts:
    """The running per-decision counts for one batch push, carried as ONE object.

    Both twins accumulate into this, and both of a batch's exits render from it, so no exit can
    report a subset of the fields. A loose parameter list is exactly what let `lost` be dropped:
    it was computed in the loop and then five of the six counts were forwarded to the early
    return, so a decision that was neither stored NOR queued reported `lost=0` and the sentence
    said "the rest are queued". Passing the object whole makes that unforgettable rather than
    merely fixed once."""

    sent: int = 0
    at_capacity: int = 0
    invalid: int = 0
    lost: int = 0

    def status(self, outcome: str, total: int, **extra) -> share_status.ShareStatus:
        return share_status.ShareStatus(
            outcome, sent=self.sent, at_capacity=self.at_capacity, invalid=self.invalid,
            lost=self.lost, total=total, **extra)


def _queue_rest_status(decs: list[dict], start: int, key, counts: _BatchCounts,
                       total: int) -> share_status.ShareStatus:
    """A chunk the cloud stopped accepting (unreachable, auth, OR a refusal like a rate limit - the
    stderr warning above names which): queue decs[start:] (this chunk + everything after) and
    report what that achieved. Mirrors share_all's original disk-error handling.

    The two outcomes differ in exactly the way a caller cares about: BATCH_INTERRUPTED means
    every unsent decision is safely queued, BATCH_STRANDED means the queue write itself failed
    partway and `lost` decisions are recorded nowhere.

    What this call is responsible for comes from `start`, NOT from `counts.sent`. It owns
    `decs[start:]` and nothing else, and `sent` can trail `start` whenever an earlier chunk had a
    row the server skipped: deriving from `sent` then double-counts those rows, which
    `_requeue_skipped` has already queued and counted under `at_capacity`. As prose that was a
    vague overstatement; as a number a caller acts on it would be a false `lost`.

    A failure here ADDS to whatever was already lost in earlier chunks rather than replacing it,
    so the total stays a true partition of `total`. The rendered sentence still summarises (it
    points at the stderr warning for the reason); the FIELDS are the complete account."""
    handled = total - start          # what this call is responsible for queueing
    queued = 0
    try:
        for rest in decs[start:]:
            _enqueue(_payload(rest, key))
            queued += 1
    except Exception:
        counts.lost += handled - queued
        return counts.status(share_status.BATCH_STRANDED, total, queued=queued)
    return counts.status(share_status.BATCH_INTERRUPTED, total, queued=queued)


def _split_skips(skipped: list) -> tuple[set, int]:
    """Partition server skips into (retryable_decision_ids, permanent_invalid_count). TRANSIENT
    'quota_exceeded' rows are kept queued to drain once space frees; PERMANENT ones (invalid type /
    content - the server per-row-rejected them) can never sync, so they are dropped, not retried."""
    retry = {s["decision_id"] for s in skipped if s.get("reason") == "quota_exceeded"}
    invalid = len(skipped) - len(retry)
    return retry, invalid


def _drain_mark(chunk: list[dict], res: tuple[list[str], list[dict]], sent_ids: set,
                endpoint: str | None = None) -> int:
    """Mark a successfully-drained chunk: saved AND permanently-invalid entries -> sent_ids (dropped
    from the outbox on the final reconcile - invalid ones can never sync, so stop retrying them);
    only TRANSIENT capacity skips stay queued. Separately records the shared-marker sidecar for
    only the entries genuinely saved by the server (neither transient-retry NOR permanently-invalid
    - `skipped` as a whole, not just `retry`). Returns the count genuinely saved."""
    _saved, skipped = res
    retry, _invalid = _split_skips(skipped)
    skipped_ids = {s.get("decision_id") for s in skipped}
    for e in chunk:
        if e.get("decision_id") not in retry:
            sent_ids.add(e.get("decision_id"))
    _mark_shared([e.get("decision_id") for e in chunk if e.get("decision_id") not in skipped_ids], endpoint)
    return len(chunk) - len(skipped)


def _mark_batch_saved(chunk: list[dict], skipped: list[dict], endpoint: str | None) -> None:
    """Mark only the chunk rows the server actually SAVED - i.e. NOT present in `skipped` at
    all (transient capacity skip or permanent invalid alike are excluded; a re-queued capacity
    skip may still sync later, but it hasn't yet, so it must not show as shared)."""
    skipped_ids = {s.get("decision_id") for s in skipped}
    _mark_shared([d["id"] for d in chunk if d["id"] not in skipped_ids], endpoint)


def _push_batch(remote: RemoteStore, decs: list[dict], key,
                endpoint: str | None = None) -> share_status.ShareStatus:
    """Sync batch push of shareable projections (share_all / share_ids). Stops at the first failed
    chunk (queues it + the rest); re-queues TRANSIENT capacity skips; drops PERMANENT invalid ones."""
    total = len(decs)
    counts = _BatchCounts()
    for start in range(0, total, _BATCH_SIZE):
        chunk = decs[start:start + _BATCH_SIZE]
        res = with_local_fallback(
            lambda chunk=chunk: remote.push_decisions([_dec_push_kwargs(d, key) for d in chunk]),
            default=None, action="share decisions")
        if res is None:
            return _queue_rest_status(decs, start, key, counts, total)
        _saved, skipped = res
        _mark_batch_saved(chunk, skipped, endpoint)
        counts.sent += len(chunk) - len(skipped)
        retry, inv = _split_skips(skipped)
        counts.invalid += inv
        if retry:
            requeued, dropped = _requeue_skipped(chunk, key, retry)
            counts.at_capacity += requeued
            counts.lost += dropped
    return counts.status(share_status.BATCH_DONE, total)


async def _apush_batch(remote: RemoteStore, decs: list[dict], key,
                       endpoint: str | None = None) -> share_status.ShareStatus:
    """Async twin of :func:`_push_batch` (awaits apush_decisions so a wedged chunk is cancellable).
    Mirrors it line-for-line except the awaited push; shares every outbox/status helper."""
    total = len(decs)
    counts = _BatchCounts()
    for start in range(0, total, _BATCH_SIZE):
        chunk = decs[start:start + _BATCH_SIZE]
        res = await awith_local_fallback(
            lambda chunk=chunk: remote.apush_decisions([_dec_push_kwargs(d, key) for d in chunk]),
            default=None, action="share decisions")
        if res is None:
            return _queue_rest_status(decs, start, key, counts, total)
        _saved, skipped = res
        _mark_batch_saved(chunk, skipped, endpoint)
        counts.sent += len(chunk) - len(skipped)
        retry, inv = _split_skips(skipped)
        counts.invalid += inv
        if retry:
            requeued, dropped = _requeue_skipped(chunk, key, retry)
            counts.at_capacity += requeued
            counts.lost += dropped
    return counts.status(share_status.BATCH_DONE, total)


def drain_outbox(profile: Profile | None = None) -> int:
    """Locked wrapper for retrying queued pushes."""
    with outbox_lock():
        profile = profile or load_profile()
        return (_drain_outbox_unlocked(profile)
                + _drain_reconciliation_outbox_unlocked(profile))


def _drain_outbox_unlocked(profile: Profile | None = None) -> int:
    """Retry every queued push, FIFO, and return how many succeeded.

    No-op (0) when the outbox is empty or team sync isn't configured. Stops at the FIRST
    failure and leaves that entry plus everything after it queued: the cloud is likely
    still down, so hammering the rest would be pointless (with_local_fallback's warn-once
    already keeps stderr quiet). Entries are never dropped for age/attempts here - see
    _OUTBOX_CAP."""
    entries = _load_outbox()
    if not entries:
        return 0
    profile = profile or load_profile()
    remote = RemoteStore.from_profile(profile)
    if remote is None:
        return 0
    sent = 0
    sent_ids: set = set()
    for start in range(0, len(entries), _BATCH_SIZE):
        chunk = entries[start:start + _BATCH_SIZE]
        res = with_local_fallback(
            lambda chunk=chunk: remote.push_decisions([_entry_push_kwargs(e) for e in chunk]),
            default=None, action="drain queued share")
        if res is None:
            for entry in chunk:
                entry["attempts"] = entry.get("attempts", 0) + 1
            _save_outbox(_reconcile_with_disk(entries[start:], sent_ids))
            return sent
        sent += _drain_mark(chunk, res, sent_ids, profile.endpoint)
    _save_outbox(_reconcile_with_disk([], sent_ids))
    return sent


def _drain_reconciliation_outbox_unlocked(profile: Profile) -> int:
    """Retry confirmed atomic writes verbatim; stale heads become attention, never retries."""
    entries, error = _read_reconcile_outbox()
    if error is not None:
        return 0
    if not entries:
        return 0
    remote = RemoteStore.from_profile(profile)
    if remote is None:
        return 0
    try:
        protocol = remote.get_capabilities().decision_reconciliation
    except RemoteStoreError as exc:
        if _unsupported_capability_error(exc):
            for entry in entries:
                if entry.get("stage") != "attention":
                    entry.update(stage="attention", reason="unsupported_protocol")
            _save_reconcile_outbox(entries)
        return 0
    if not protocol or protocol.version < 1 or not protocol.atomic_submit:
        for entry in entries:
            if entry.get("stage") != "attention":
                entry.update(stage="attention", reason="unsupported_protocol")
        _save_reconcile_outbox(entries)
        return 0

    kept: list[dict] = []
    sent = 0
    for index, entry in enumerate(entries):
        if entry.get("stage") == "attention":
            kept.append(entry)
            continue
        try:
            result = _call_atomic_submission(remote, entry)
        except RemoteStoreError as exc:
            entry["attempts"] = entry.get("attempts", 0) + 1
            if not isinstance(exc, _RETRYABLE_REFUSALS):
                entry.update(stage="attention", reason=str(exc))
                kept.append(entry)
                continue
            kept.extend([entry, *entries[index + 1:]])
            _save_reconcile_outbox(kept)
            return sent
        if result.status in {"heads_changed", "needs_rebase"}:
            entry.update(stage="attention", reason=result.status,
                         observed_personal_head=result.personal_head,
                         observed_team_head=result.team_head)
            kept.append(entry)
            continue
        if result.status == "rate_limited":
            entry["attempts"] = entry.get("attempts", 0) + 1
            kept.extend([entry, *entries[index + 1:]])
            _save_reconcile_outbox(kept)
            return sent
        if result.status not in {"submitted", "unchanged", "already_pending"}:
            entry.update(stage="attention", reason=result.status or "unknown_result")
            kept.append(entry)
            continue
        _mark_shared([entry.get("decision_id")], profile.endpoint)
        sent += 1
    _save_reconcile_outbox(kept)
    return sent


async def _adrain_outbox_unlocked(profile: Profile | None = None) -> int:
    """Async twin of :func:`_drain_outbox_unlocked` (awaits apush_decisions so a wedged retry is
    cancellable). Identical FIFO / stop-at-first-failure / reconcile semantics - the only
    difference is the awaited push; every other line is the shared local outbox logic.

    Drains the SHARE outbox only, exactly like its sync twin. The reconciliation retry queue is
    drained by `drain_outbox`, the public locked wrapper, which `team_context.refresh` calls at
    session start. An async wrapper pairing this with an async-native reconciliation drain existed
    and was deleted: nothing but its own tests ever called it, so the 68-line copy of that drain
    only ever drifted from the sync original it duplicated."""
    entries = _load_outbox()
    if not entries:
        return 0
    profile = profile or load_profile()
    remote = RemoteStore.from_profile(profile)
    if remote is None:
        return 0
    sent = 0
    sent_ids: set = set()
    for start in range(0, len(entries), _BATCH_SIZE):
        chunk = entries[start:start + _BATCH_SIZE]
        res = await awith_local_fallback(
            lambda chunk=chunk: remote.apush_decisions([_entry_push_kwargs(e) for e in chunk]),
            default=None, action="drain queued share")
        if res is None:
            for entry in chunk:
                entry["attempts"] = entry.get("attempts", 0) + 1
            _save_outbox(_reconcile_with_disk(entries[start:], sent_ids))
            return sent
        sent += _drain_mark(chunk, res, sent_ids, profile.endpoint)
    _save_outbox(_reconcile_with_disk([], sent_ids))
    return sent


def _payload(dec: dict, key) -> dict:
    """Outbox entry for one wire-projected decision (same shape share() enqueues). Carries
    title so a queued offline share still sends it once drained (_entry_push_kwargs reads
    it back off this same row). Also carries `source_files` (issue #174 Task 5) the same way -
    stored in the outbox regardless of the current wire gate, so `_entry_push_kwargs` +
    `remote._wire_args` decide at DRAIN time whether it actually egresses."""
    return {
        "decision_id": dec["id"], "type": dec["type"], "content": dec["content"],
        "repo": key, "rationale": None, "confidence": dec["confidence"],
        "evidence": dec["evidence"], "source": _wire_source(dec["source"]),
        "title": dec.get("title"), "source_files": dec.get("source_files"),
        "queued_at": time.time(), "attempts": 0,
    }


def share_all(repo_path: str, *, profile: Profile | None = None) -> share_status.ShareStatus:
    with outbox_lock():
        return _share_all_unlocked(repo_path, profile=profile)


def _share_all_unlocked(repo_path: str, *,
                        profile: Profile | None = None) -> share_status.ShareStatus:
    """Push every non-ignored local decision to your team cloud context, oldest first.

    Same local-first contract as share(): never raises for cloud problems. Stops at the
    FIRST failed push (the cloud is likely down - drain_outbox semantics) and queues the
    failed decision plus everything after it in the outbox, so no share intent is lost."""
    profile = profile or load_profile()
    try:
        _drain_outbox_unlocked(profile)  # queued shares go out first, so ordering is preserved
    except Exception:
        pass
    decs = store.get_shareable_all(repo_path)
    if not decs:
        return share_status.ShareStatus(share_status.NOTHING_TO_SHARE)
    remote = RemoteStore.from_profile(profile)
    if remote is None:
        return share_status.ShareStatus(share_status.NOT_TEAM_MODE)
    key = canonical_repo_key(store.run_git(repo_path, "remote", "get-url", "origin"))
    return _push_batch(remote, decs, key, profile.endpoint)


def share_global(*, profile: Profile | None = None) -> share_status.ShareStatus:
    with outbox_lock():
        return _share_global_unlocked(profile=profile)


def _share_global_unlocked(*,
                           profile: Profile | None = None) -> share_status.ShareStatus:
    """Push every global rule (`~/.contexer/_global.json`) to your team cloud context (#239).

    Global rules apply to every repo, so they go up with `repo=None`: `remote._wire_args` omits
    the field entirely, and the server stores an unbound row (`repo IS NULL`) that serves the
    team's repos plus globals. That is also why this cannot be a flag on `share_all` - that path
    derives its key from `canonical_repo_key(git remote get-url origin)`, and there is no repo
    here to derive one from; passing a fake path to reach the global store would bind these rows
    to one arbitrary repo.

    Same local-first contract as `share_all`: never raises for cloud problems, stops at the
    first failed chunk and queues it plus everything after it, so no share intent is lost.
    Idempotent on `decisionId`, so re-running upserts rather than duplicating - which is also
    how an edited global rule already in the cloud gets corrected."""
    profile = profile or load_profile()
    try:
        _drain_outbox_unlocked(profile)  # queued shares go out first, so ordering is preserved
    except Exception:
        pass
    decs = store.get_shareable_global()
    if not decs:
        return share_status.ShareStatus(share_status.NOTHING_TO_SHARE, scope="global")
    remote = RemoteStore.from_profile(profile)
    if remote is None:
        return share_status.ShareStatus(share_status.NOT_TEAM_MODE)
    return _push_batch(remote, decs, None, profile.endpoint)


def share(repo_path: str, decision_id: str = "", *,
          profile: Profile | None = None) -> share_status.ShareStatus:
    with outbox_lock():
        return _share_unlocked(repo_path, decision_id, profile=profile)


def _share_unlocked(repo_path: str, decision_id: str = "", *,
                    profile: Profile | None = None) -> share_status.ShareStatus:
    """Push one local decision to your team cloud context; return a human-readable status.

    Local-first: never raises for cloud problems - returns a message and leaves the local
    decision untouched. `decision_id` selects the decision (full id / 8-char prefix); omit
    to share the most recent. `profile` defaults to load_profile()."""
    profile = profile or load_profile()
    try:
        _drain_outbox_unlocked(profile)  # queued shares go out first, so ordering is preserved
    except Exception:
        pass  # a broken drain (e.g. disk error saving the outbox) must not block this share
    dec = store.get_shareable(repo_path, decision_id)
    if dec is None:
        return share_status.ShareStatus(share_status.NO_MATCH)
    remote = RemoteStore.from_profile(profile)
    if remote is None:
        return share_status.ShareStatus(share_status.NOT_TEAM_MODE)
    key = canonical_repo_key(store.run_git(repo_path, "remote", "get-url", "origin"))
    server_id = with_local_fallback(
        lambda: remote.push_decision(**_dec_push_kwargs(dec, key)),
        default=None, action="share decision")
    return _finish_share(dec, key, server_id, profile.endpoint)


def share_ids(repo_path: str, decision_ids: list, *,
              profile: Profile | None = None) -> share_status.ShareStatus:
    with outbox_lock():
        return _share_ids_unlocked(repo_path, decision_ids, profile=profile)


def reconcile(repo_path: str, decision_id: str, team: str = "", *,
              profile: Profile | None = None) -> share_status.ReconcileStatus:
    """Prepare and submit one reconciliation without an interactive confirmation.

    The CLI calls :func:`prepare_reconciliation` itself so it can show the authoritative server
    preview before asking. This convenience entry point remains useful to API callers and tests.
    """
    plan, why = prepare_reconciliation(repo_path, decision_id, team, profile=profile)
    if plan is None:
        return why
    return submit_reconciliation(plan, profile=profile)


def _team_rows(teams: list[RemoteTeam]) -> tuple[str, ...]:
    return tuple(f"{t.name} ({t.id})" for t in teams)


def _select_team(teams: list[RemoteTeam],
                 requested: str) -> tuple[RemoteTeam | None, share_status.ReconcileStatus | None]:
    """(team, None) on a resolved target, (None, why) otherwise.

    The 2-tuple is the house shape for "a value, or the reason there isn't one"
    (`store._read_outbox`, `store.resolve_repo_verbose`). It replaces `RemoteTeam | str`, where a
    `str` MEANT failure and every caller had to test the type to find out."""
    if not teams:
        return None, share_status.ReconcileStatus(share_status.NO_TEAMS)
    if requested:
        matches = ([t for t in teams if t.id == requested]
                   or [t for t in teams if t.name.casefold() == requested.casefold()])
        if len(matches) == 1:
            return matches[0], None
        if len(matches) > 1:
            return None, share_status.ReconcileStatus(
                share_status.TEAM_AMBIGUOUS, detail=requested)
        return None, share_status.ReconcileStatus(
            share_status.TEAM_UNKNOWN, detail=requested, teams=_team_rows(teams))
    if len(teams) == 1:
        return teams[0], None
    return None, share_status.ReconcileStatus(
        share_status.TEAM_CHOICE_REQUIRED, teams=_team_rows(teams))


def _atomic_decision_kwargs(dec: dict, key: str | None, *,
                            redact_on: bool | None = None) -> dict:
    """Nested reconciliation payload, serialized before preview/submission/outbox persistence.

    This deliberately uses the remote wire serializer up front, not just inside RemoteStore, so a
    confirmed `.reconcile-outbox.json` entry stores the same redacted/bounded decision body the
    user previewed and the server will receive.
    """
    return _reconciliation_wire_body(
        type=dec["type"], content=dec["content"], repo=key,
        confidence=dec["confidence"], evidence=dec["evidence"],
        source=_wire_source(dec["source"]), title=dec.get("title"),
        source_files=dec.get("source_files"), redact_on=redact_on)


def _unsupported_capability_error(exc: RemoteStoreError) -> bool:
    message = str(exc).casefold()
    return any(marker in message for marker in (
        "unknown tool", "tool not found", "method not found", "-32601", "get_capabilities failed"))


def _resolve_reconciliation_team(
        remote: RemoteStore,
        requested: str) -> tuple[RemoteTeam | None, share_status.ReconcileStatus | None]:
    try:
        teams = remote.list_teams()
    except RemoteStoreError as exc:
        if requested and _unsupported_capability_error(exc):
            return RemoteTeam(requested, requested, "member"), None
        if _unsupported_capability_error(exc):
            return None, share_status.ReconcileStatus(share_status.NO_TEAM_DISCOVERY)
        return None, share_status.ReconcileStatus(share_status.TEAM_LIST_FAILED)
    return _select_team(teams, requested)


def prepare_reconciliation(
        repo_path: str, decision_id: str, team: str = "", *,
        profile: Profile | None = None,
) -> tuple[ReconciliationPlan | None, share_status.ReconcileStatus | None]:
    """Resolve a target and fetch the authoritative preview without changing remote state.

    Returns `(plan, None)` when a submission can go ahead, `(None, why)` otherwise. It used to
    return `ReconciliationPlan | str`, so `str` MEANT failure and `cli.reconcile_cmd` had to run
    `isinstance(prepared, str)` to discover which it had been given."""
    profile = profile or load_profile()
    dec = store.get_shareable(repo_path, decision_id, redact_on=profile.redact_secrets)
    if dec is None:
        return None, share_status.ReconcileStatus(share_status.NO_LOCAL_MATCH)
    remote = RemoteStore.from_profile(profile)
    if remote is None:
        return None, share_status.ReconcileStatus(share_status.NOT_LOGGED_IN)

    key = canonical_repo_key(store.run_git(repo_path, "remote", "get-url", "origin"))
    try:
        capabilities = remote.get_capabilities()
    except RemoteStoreError as exc:
        if _unsupported_capability_error(exc):
            target, why = _resolve_reconciliation_team(remote, team)
            if target is None:
                return None, why
            return ReconciliationPlan(
                dec, key, target, remote, None, False, str(uuid.uuid4()),
                profile.redact_secrets), None
        return None, share_status.ReconcileStatus(
            share_status.CAPABILITIES_FAILED, detail=str(exc))

    protocol = capabilities.decision_reconciliation
    atomic = bool(protocol and protocol.version >= 1
                  and protocol.atomic_submit and protocol.preview)
    target, why = _resolve_reconciliation_team(remote, team)
    if target is None:
        return None, why
    if not atomic:
        return ReconciliationPlan(
            dec, key, target, remote, None, False, str(uuid.uuid4()),
            profile.redact_secrets), None
    if not dec.get("revision_id"):
        return None, share_status.ReconcileStatus(share_status.NO_REVISION_ID)
    try:
        preview = remote.preview_decision_reconciliation(
            dec["id"], target.id, **_atomic_decision_kwargs(
                dec, key, redact_on=profile.redact_secrets))
    except RemoteStoreError as exc:
        return None, share_status.ReconcileStatus(
            share_status.PREVIEW_FAILED, detail=str(exc))
    return ReconciliationPlan(
        dec, key, target, remote, preview, True, str(uuid.uuid4()),
        profile.redact_secrets), None


def format_reconciliation_preview(plan: ReconciliationPlan) -> str:
    if not plan.atomic or plan.preview is None:
        return (f"Target: {plan.target.name}\nCompatibility mode: this server does not support "
                "atomic preview and submission. Personal sync will happen before team review.")
    preview = plan.preview
    lines = [f"Target: {preview.team.name}",
             f"Server preview: {preview.operation or 'submit'} ({preview.state or 'ready'})"]
    for field in preview.fields:
        before = json.dumps(field.before, ensure_ascii=False)
        after = json.dumps(field.after, ensure_ascii=False)
        lines.append(f"  {field.field}: {before} -> {after}")
    if preview.pending_candidate_id:
        lines.append(f"Pending candidate: {preview.pending_candidate_id}")
    lines.append("The currently approved team version remains active until a lead approves this.")
    return "\n".join(lines)


def _reconciliation_operation(plan: ReconciliationPlan) -> dict:
    assert plan.preview is not None
    return {
        "operation": "submit_team_decision",
        "idempotency_key": plan.idempotency_key,
        "decision_id": plan.decision["id"],
        "revision_id": plan.decision["revision_id"],
        "team_id": plan.target.id,
        "team_name": plan.target.name,
        "expected_personal_head": plan.preview.personal_head,
        "expected_team_head": plan.preview.team_head,
        "payload": _atomic_decision_kwargs(
            plan.decision, plan.repo_key, redact_on=plan.redact_on),
        "queued_at": time.time(),
        "attempts": 0,
        "stage": "confirmed",
    }


def _call_atomic_submission(remote: RemoteStore, operation: dict) -> TeamSubmissionResult:
    payload = operation.get("payload") or operation.get("decision") or {}
    return remote.submit_team_decision(
        operation["decision_id"], operation["revision_id"], operation["team_id"],
        expected_personal_head=operation.get("expected_personal_head"),
        expected_team_head=operation.get("expected_team_head"),
        idempotency_key=operation["idempotency_key"], **payload)


# Statuses the service returns for a refusal it will not reconsider. Named here rather than
# inline so the set is one thing a reader can check against the server's own taxonomy.
_TERMINAL_SUBMISSION_REFUSALS = frozenset({
    "not_member", "not_authored_by_caller", "invalid_team", "trial_expired",
    "unsupported_protocol"})

_SUBMISSION_OUTCOMES = {
    "heads_changed": share_status.HEADS_CHANGED,
    "needs_rebase": share_status.NEEDS_REBASE,
    "unchanged": share_status.UNCHANGED,
    "already_pending": share_status.ALREADY_PENDING,
    "quota_exceeded": share_status.QUOTA_EXCEEDED,
}


def _submission_status(result: TeamSubmissionResult,
                       team_name: str) -> share_status.ReconcileStatus:
    """Map one server answer onto an outcome, carrying the server's own status either way.

    `server_status` is kept even when the outcome names it, so a caller can log or branch on what
    the service actually said without re-reading a sentence."""
    def status(outcome: str) -> share_status.ReconcileStatus:
        return share_status.ReconcileStatus(
            outcome, team_name=team_name, candidate_id=result.candidate_id or "",
            server_status=result.status or "",
            noun="update" if result.kind == "update" else "decision",
            replayed=bool(result.replayed))

    mapped = _SUBMISSION_OUTCOMES.get(result.status or "")
    if mapped:
        return status(mapped)
    if result.status in _TERMINAL_SUBMISSION_REFUSALS:
        return status(share_status.SERVICE_REFUSED)
    if result.status != "submitted":
        return status(share_status.UNKNOWN_RESULT)
    return status(share_status.SUBMITTED)


def _queue_confirmed(operation: dict, queued: str,
                     stranded: str) -> share_status.ReconcileStatus:
    """Queue a CONFIRMED operation for automatic retry and say which of the two things happened.

    Both callers below reach this after the service declined to take the write now. The
    distinction is the whole point: `queued` means a later drain resubmits it with the same
    idempotency key, `stranded` means the queue write itself failed and nothing will resend it."""
    try:
        _enqueue_reconciliation(operation)
    except Exception:
        return share_status.ReconcileStatus(stranded)
    return share_status.ReconcileStatus(queued)


def submit_reconciliation(plan: ReconciliationPlan, *,
                          profile: Profile | None = None) -> share_status.ReconcileStatus:
    """Submit a previously previewed plan. Only confirmed atomic operations may be queued."""
    profile = profile or load_profile()
    dec, target = plan.decision, plan.target
    if plan.atomic:
        operation = _reconciliation_operation(plan)
        try:
            result = _call_atomic_submission(plan.remote, operation)
        except RemoteStoreError as exc:
            outcomes = _transient_outcomes(exc)
            if outcomes is None:                     # terminal: do not queue, do not retry
                return share_status.ReconcileStatus(
                    share_status.SUBMISSION_REFUSED, detail=str(exc))
            # Transient, so the confirmed write is kept for retry. `detail` is carried on every
            # arm; the rate-limit sentences ignore it, and recording the reason costs nothing.
            return replace(_queue_confirmed(operation, *outcomes), detail=str(exc))
        if result.status == "rate_limited":
            return _queue_confirmed(operation, share_status.RATE_LIMITED_QUEUED,
                                    share_status.RATE_LIMITED_NOT_QUEUED)
        if result.status in {"submitted", "unchanged", "already_pending"}:
            _mark_shared([dec.get("id")], profile.endpoint)
        return _submission_status(result, target.name)

    # Old-server compatibility path: preserve the Phase-1 two-step contract, but never put an
    # unsupported atomic operation into the durable reconciliation queue.
    server_id = with_local_fallback(
        lambda: plan.remote.push_decision(**_dec_push_kwargs(dec, plan.repo_key)),
        default=None, action="sync decision before team submission")
    if server_id is None:
        return share_status.ReconcileStatus(
            share_status.COMPAT_SYNC_FAILED,
            share=_finish_share(dec, plan.repo_key, None, profile.endpoint))

    submitted = with_local_fallback(
        lambda: plan.remote.submit_decision_to_team(dec["id"], target.id),
        default=None, action="submit decision for team review")
    _mark_shared([dec.get("id")], profile.endpoint)
    if submitted is None:
        return share_status.ReconcileStatus(
            share_status.COMPAT_SUBMIT_FAILED, team_name=target.name)

    # Deliberately the SAME outcome the atomic path returns: the two branches produced two copies
    # of one sentence, identical whenever a candidate id was present, which it always is here.
    return share_status.ReconcileStatus(
        share_status.SUBMITTED, team_name=submitted.team.name,
        candidate_id=submitted.candidate_id or "",
        noun="update" if submitted.kind == "update" else "decision")


def _share_ids_unlocked(repo_path: str, decision_ids: list, *,
                        profile: Profile | None = None) -> share_status.ShareStatus:
    """Share a selection of decisions (a multi-pick) in ONE batched call, returning a combined
    status. An empty list shares the most recent (delegates to share('')). Outbox + local-first
    guarantees are preserved (a failed chunk is queued); unknown/typo'd ids are REPORTED, not
    silently dropped; the loaded profile is threaded through to avoid re-reading config.toml."""
    profile = profile or load_profile()
    if not decision_ids:
        return share(repo_path, "", profile=profile)
    try:
        _drain_outbox_unlocked(profile)  # queued shares go out first, so ordering is preserved
    except Exception:
        pass
    projs, missing = _resolve_ids(repo_path, decision_ids)
    if not projs:
        return share_status.ShareStatus(
            share_status.NO_MATCH, unknown_ids=tuple(str(m) for m in missing))
    remote = RemoteStore.from_profile(profile)
    if remote is None:
        return share_status.ShareStatus(share_status.NOT_TEAM_MODE)
    key = canonical_repo_key(store.run_git(repo_path, "remote", "get-url", "origin"))
    return share_status.with_unknown(
        _push_batch(remote, projs, key, profile.endpoint), missing)


# ── async share path (#108) ────────────────────────────────────────────────────────
# The in-loop server.share_decision tool awaits these so a wedged push is CANCELLABLE at
# the tool's deadline (no leaked worker thread / open socket). They mirror share / share_ids
# above line-for-line except the push is awaited, and reuse the same local helpers
# (_finish_share, _dec_push_kwargs, _payload, _enqueue) so no logic drifts between the paths.

async def share_async(repo_path: str, decision_id: str = "", *,
                      profile: Profile | None = None) -> share_status.ShareStatus:
    async with async_outbox_lock():
        return await _share_async_unlocked(repo_path, decision_id, profile=profile)


async def _share_async_unlocked(repo_path: str, decision_id: str = "", *,
                                profile: Profile | None = None) -> share_status.ShareStatus:
    """Async twin of :func:`share`. Same local-first contract: never raises for cloud
    problems, leaves the local decision untouched, queues on failure."""
    profile = profile or load_profile()
    try:
        await _adrain_outbox_unlocked(profile)  # queued shares go out first, so ordering is preserved
    except Exception:
        pass  # a broken drain (e.g. disk error) must not block this share
    dec = store.get_shareable(repo_path, decision_id)
    if dec is None:
        return share_status.ShareStatus(share_status.NO_MATCH)
    remote = RemoteStore.from_profile(profile)
    if remote is None:
        return share_status.ShareStatus(share_status.NOT_TEAM_MODE)
    key = canonical_repo_key(store.run_git(repo_path, "remote", "get-url", "origin"))
    try:
        server_id = await awith_local_fallback(
            lambda: remote.apush_decision(**_dec_push_kwargs(dec, key)),
            default=None, action="share decision")
    except asyncio.CancelledError:
        # Best-effort on the way out: a queue refusal (see _enqueue_unlocked) must not replace
        # the CancelledError, or a cancelled share stops looking cancelled to its caller.
        with contextlib.suppress(Exception):
            _enqueue_unlocked(_payload(dec, key))
        raise
    return _finish_share(dec, key, server_id, profile.endpoint)


async def share_ids_async(repo_path: str, decision_ids: list, *,
                          profile: Profile | None = None) -> share_status.ShareStatus:
    async with async_outbox_lock():
        return await _share_ids_async_unlocked(repo_path, decision_ids, profile=profile)


async def _share_ids_async_unlocked(repo_path: str, decision_ids: list, *,
                                    profile: Profile | None = None) -> share_status.ShareStatus:
    """Async twin of :func:`share_ids`: one batched (awaited) push per _BATCH_SIZE, unknown ids
    reported, capacity-skipped rows re-queued. An empty list shares the most recent."""
    profile = profile or load_profile()
    if not decision_ids:
        return await share_async(repo_path, "", profile=profile)
    try:
        await _adrain_outbox_unlocked(profile)  # queued shares go out first, so ordering is preserved
    except Exception:
        pass
    projs, missing = _resolve_ids(repo_path, decision_ids)
    if not projs:
        return share_status.ShareStatus(
            share_status.NO_MATCH, unknown_ids=tuple(str(m) for m in missing))
    remote = RemoteStore.from_profile(profile)
    if remote is None:
        return share_status.ShareStatus(share_status.NOT_TEAM_MODE)
    key = canonical_repo_key(store.run_git(repo_path, "remote", "get-url", "origin"))
    try:
        status = await _apush_batch(remote, projs, key, profile.endpoint)
    except asyncio.CancelledError:
        # Same reason as share_async's single-decision path above: cancellation must win.
        with contextlib.suppress(Exception):
            for dec in projs:
                _enqueue_unlocked(_payload(dec, key))
        raise
    return share_status.with_unknown(status, missing)


def enqueue_ids_for_retry(repo_path: str, decision_ids: list) -> int:
    """Queue the given decisions (by id) into the outbox so a later drain retries them.

    Called when an in-loop share is CANCELLED by its deadline (server.share_decision timeout)
    before it could push or queue them itself: cancellation bypasses share_async's own
    enqueue-on-failure, so without this the tool's "the outbox retries it" message would be an
    empty promise. Idempotent - `_enqueue` dedups by decision_id and a re-push is idempotent
    server-side, so queuing a decision that may already have been sent is safe. An empty list
    queues the most recent shareable (matching `share_async('')`). Missing ids are skipped.
    Returns the count queued."""
    with outbox_lock():
        ids = decision_ids or [""]  # "" -> most recent, matching share_async("")
        key = canonical_repo_key(store.run_git(repo_path, "remote", "get-url", "origin"))
        queued = 0
        for did in ids:
            dec = store.get_shareable(repo_path, did)
            if dec is not None:
                _enqueue_unlocked(_payload(dec, key))
                queued += 1
        return queued
