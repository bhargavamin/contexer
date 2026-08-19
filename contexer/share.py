"""Explicit share: push a local decision up to the Teams cloud context (C4).

Path B, the write counterpart to team_context.pull. Sharing is an EXPLICIT verb — never
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

import json
import time
from datetime import datetime, timezone

from contexer import store
from contexer.config import Profile, load_profile
from contexer.remote import RemoteStore, awith_local_fallback, with_local_fallback
from contexer.repo_key import canonical_repo_key

# Outbox cap: push_decision is idempotent on decision_id server-side, so a queued entry is
# never dropped for age or attempt count in v1 - retrying is always safe. This count is the
# only bound, so a long-offline stretch can't grow ~/.contexer/.outbox.json without limit.
_OUTBOX_CAP = 50

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
    to "ai" — a safe accepted value — so an unknown source can never brick the outbox. `None`
    passes through unchanged: push_decision OMITS source when it is None (the cloud stores NULL =
    unknown provenance), so None must not be fabricated into a false "ai" provenance."""
    if source is None or source in _WIRE_SOURCES:
        return source
    return "ai"


def _outbox_path():
    # Computed at call time (not module import time) so tests that monkeypatch
    # store.STORE_DIR see the redirected path, like every other store-adjacent file.
    return store.STORE_DIR / ".outbox.json"


def _load_outbox() -> list[dict]:
    """Read the outbox; a missing or corrupt file reads as empty, never raises."""
    path = _outbox_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            data = None
        if isinstance(data, list):
            return data
    return []


def _save_outbox(entries: list[dict]) -> None:
    store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    store._atomic_write(_outbox_path(), json.dumps(entries, indent=2, ensure_ascii=False))


def discard_outbox() -> int:
    """Drop every queued share; return how many were discarded. Fail-soft.

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
    silent and cannot be taken back. Sizing the loss is also why this returns a count rather
    than a bool.

    Deliberately NOT called from `logout`: nothing drains without credentials, so `login` is the
    only chokepoint an entry can egress through, and clearing at logout as well would discard
    queues that were never in danger."""
    queued = len(_load_outbox())
    try:
        _outbox_path().unlink()
    except OSError:   # includes FileNotFoundError - nothing queued
        return 0
    return queued


def _enqueue(payload: dict) -> None:
    """Queue a failed push. Dedupes by decision_id (re-sharing the same decision while
    offline replaces the queued entry - fresh content wins) and caps at _OUTBOX_CAP,
    dropping the oldest entries beyond that."""
    entries = [e for e in _load_outbox() if e.get("decision_id") != payload.get("decision_id")]
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
# `_store_lock` is NOT reentrant (a second acquire in the same process blocks on its own
# lock), so these two must stay strictly sequential, never nested. See _mark_shared.
_SHARED_LOCK_SLUG = ".shared"


def _shared_path():
    # Computed at call time (not module import time), same convention as _outbox_path -
    # tests that monkeypatch store.STORE_DIR see the redirected path.
    return store.STORE_DIR / ".shared.jsonl"


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
    unavailable (non-POSIX) `_store_lock` is a no-op and the append still can't clobber a
    peer append - only the rare compaction overlap stays exposed, which is cosmetic."""
    store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    path = _shared_path()
    blob = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in records)
    with store._store_lock(_SHARED_LOCK_SLUG):
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
    the window - cosmetic, since the decision merely re-shows as unshared."""
    path = _shared_path()
    try:
        if not path.exists() or len(path.read_text(encoding="utf-8").splitlines()) <= _SHARED_LOG_MAX_LINES:
            return
        with store._store_lock(_SHARED_LOCK_SLUG):
            folded = _load_shared()["endpoints"]
            lines = [{"endpoint": ep, "id": did, "at": at}
                     for ep, bucket in folded.items() for did, at in bucket.items()]
            store._atomic_write(
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
    only exists on disk. Lock-free (this file's existing convention - no file locking
    added here): a concurrent `_enqueue` between our initial `_load_outbox()` at the top
    of `drain_outbox` and this point writes straight to disk, so that payload is invisible
    to our in-memory `entries` and would otherwise be silently overwritten by this drain's
    final save. Re-reading here and keeping any entry we neither sent nor already carry in
    `tail` shrinks the loss window from "the whole drain" down to the handful of lines
    between this re-read and the write that follows it - effectively zero, not perfect
    serialization, which is the deliberate tradeoff for staying lock-free. Disk-only
    entries are appended after `tail` since they were enqueued after this drain started,
    so FIFO order is preserved."""
    disk_entries = _load_outbox()
    tail_ids = {e.get("decision_id") for e in tail}
    extra = [d for d in disk_entries
             if d.get("decision_id") not in sent_ids and d.get("decision_id") not in tail_ids]
    return tail + extra


def _entry_push_kwargs(entry: dict) -> dict:
    """push_decision kwargs for one outbox entry. Shared by drain_outbox + adrain_outbox so
    the two drains serialize an entry identically (source coerced through _wire_source).

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


def _finish_share(dec: dict, key, server_id, endpoint: str | None = None) -> str:
    """Turn one push outcome into the user-facing status (shared by share + share_async).

    On failure (server_id is None: cloud unreachable OR auth rejected) enqueue the decision
    so a later drain retries it - either way a queued retry can succeed later, so both
    degradations queue rather than losing the share. On success, mark the decision shared
    (endpoint-scoped, cosmetic - never lets a marker failure affect the returned status) and
    return the honest personal-scope message (teammates don't see it until team promotion
    ships)."""
    if server_id is None:
        try:
            _enqueue(_payload(dec, key))
        except Exception:
            # Even queueing can fail (disk full, temp-dir perms). Never raise - and the
            # message must not promise a retry that was never recorded.
            return ("Share failed (see the warning above for why). Your local decision is unchanged.")
        return ("Share failed (see the warning above for why). Queued - it will retry "
                "automatically at the next session start.")
    _mark_shared([dec.get("id")], endpoint)
    return (f"Synced decision to your personal cloud context (server id={server_id}) - "
            "teammates won't see this until team promotion ships.")


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


def _prepend_unknown(status: str, missing: list) -> str:
    """Prefix a share status with a note about unknown ids (empty missing -> unchanged)."""
    if not missing:
        return status
    unknown = ", ".join(str(m)[:8] for m in missing)
    return f"Skipped {len(missing)} unknown id(s): {unknown}.\n{status}"


def _no_match_status(missing: list) -> str:
    unknown = ", ".join(str(m)[:8] for m in missing)
    return f"Nothing to share: no matching local decision (unknown id(s): {unknown})."


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


def _queue_rest_status(decs: list[dict], start: int, key, sent: int, total: int) -> str:
    """A chunk the cloud stopped accepting (unreachable, auth, OR a refusal like a rate limit - the
    stderr warning above names which): queue decs[start:] (this chunk + everything after) and
    return the status. Mirrors share_all's original disk-error handling."""
    queued = 0
    try:
        for rest in decs[start:]:
            _enqueue(_payload(rest, key))
            queued += 1
    except Exception:
        return (f"Shared {sent} of {total} decision(s), then the cloud stopped accepting them "
                f"(see the warning above for why). Queued {queued} of the remaining {total - sent} "
                "for retry before the outbox write failed - run `contexer share --all` again to "
                "queue the rest; your local decisions are unchanged.")
    return (f"Shared {sent} of {total} decision(s). The rest are queued and will retry "
            "automatically at the next session start (see the warning above for why the cloud "
            "stopped accepting them).")


def _split_skips(skipped: list) -> tuple[set, int]:
    """Partition server skips into (retryable_decision_ids, permanent_invalid_count). TRANSIENT
    'quota_exceeded' rows are kept queued to drain once space frees; PERMANENT ones (invalid type /
    content - the server per-row-rejected them) can never sync, so they are dropped, not retried."""
    retry = {s["decision_id"] for s in skipped if s.get("reason") == "quota_exceeded"}
    invalid = len(skipped) - len(retry)
    return retry, invalid


def _batch_success_status(sent: int, at_capacity: int, invalid: int, lost: int) -> str:
    """Status when no chunk hit a transport failure: what synced, what was queued at capacity, what
    the server rejected as invalid (dropped), and what was genuinely lost (queued but un-writeable)."""
    msg = f"Synced {sent} decision(s) to your personal cloud context"
    if at_capacity:
        msg += (f"; {at_capacity} could not be stored (context at capacity) and were queued - "
                "delete some decisions to sync them")
    if invalid:
        msg += f"; {invalid} were rejected by the server (unsupported type or content) and skipped"
    if lost:
        msg += f"; {lost} at capacity could NOT be queued (outbox write failed) and are unsaved"
    return msg + " - teammates won't see these until team promotion ships."


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


def _push_batch(remote: RemoteStore, decs: list[dict], key, endpoint: str | None = None) -> str:
    """Sync batch push of shareable projections (share_all / share_ids). Stops at the first failed
    chunk (queues it + the rest); re-queues TRANSIENT capacity skips; drops PERMANENT invalid ones."""
    total = len(decs)
    sent = at_capacity = invalid = lost = 0
    for start in range(0, total, _BATCH_SIZE):
        chunk = decs[start:start + _BATCH_SIZE]
        res = with_local_fallback(
            lambda chunk=chunk: remote.push_decisions([_dec_push_kwargs(d, key) for d in chunk]),
            default=None, action="share decisions")
        if res is None:
            return _queue_rest_status(decs, start, key, sent, total)
        _saved, skipped = res
        _mark_batch_saved(chunk, skipped, endpoint)
        sent += len(chunk) - len(skipped)
        retry, inv = _split_skips(skipped)
        invalid += inv
        if retry:
            requeued, dropped = _requeue_skipped(chunk, key, retry)
            at_capacity += requeued
            lost += dropped
    return _batch_success_status(sent, at_capacity, invalid, lost)


async def _apush_batch(remote: RemoteStore, decs: list[dict], key, endpoint: str | None = None) -> str:
    """Async twin of :func:`_push_batch` (awaits apush_decisions so a wedged chunk is cancellable).
    Mirrors it line-for-line except the awaited push; shares every outbox/status helper."""
    total = len(decs)
    sent = at_capacity = invalid = lost = 0
    for start in range(0, total, _BATCH_SIZE):
        chunk = decs[start:start + _BATCH_SIZE]
        res = await awith_local_fallback(
            lambda chunk=chunk: remote.apush_decisions([_dec_push_kwargs(d, key) for d in chunk]),
            default=None, action="share decisions")
        if res is None:
            return _queue_rest_status(decs, start, key, sent, total)
        _saved, skipped = res
        _mark_batch_saved(chunk, skipped, endpoint)
        sent += len(chunk) - len(skipped)
        retry, inv = _split_skips(skipped)
        invalid += inv
        if retry:
            requeued, dropped = _requeue_skipped(chunk, key, retry)
            at_capacity += requeued
            lost += dropped
    return _batch_success_status(sent, at_capacity, invalid, lost)


def drain_outbox(profile: Profile | None = None) -> int:
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


async def adrain_outbox(profile: Profile | None = None) -> int:
    """Async twin of :func:`drain_outbox` (awaits apush_decision so a wedged retry is
    cancellable). Identical FIFO / stop-at-first-failure / reconcile semantics — the only
    difference is the awaited push; every other line is the shared local outbox logic."""
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
    it back off this same row). Also carries `source_files` (issue #174 Task 5) the same way —
    stored in the outbox regardless of the current wire gate, so `_entry_push_kwargs` +
    `remote._wire_args` decide at DRAIN time whether it actually egresses."""
    return {
        "decision_id": dec["id"], "type": dec["type"], "content": dec["content"],
        "repo": key, "rationale": None, "confidence": dec["confidence"],
        "evidence": dec["evidence"], "source": _wire_source(dec["source"]),
        "title": dec.get("title"), "source_files": dec.get("source_files"),
        "queued_at": time.time(), "attempts": 0,
    }


def share_all(repo_path: str, *, profile: Profile | None = None) -> str:
    """Push every non-ignored local decision to your team cloud context, oldest first.

    Same local-first contract as share(): never raises for cloud problems. Stops at the
    FIRST failed push (the cloud is likely down - drain_outbox semantics) and queues the
    failed decision plus everything after it in the outbox, so no share intent is lost."""
    profile = profile or load_profile()
    try:
        drain_outbox(profile)  # queued shares go out first, so ordering is preserved
    except Exception:
        pass
    decs = store.get_shareable_all(repo_path)
    if not decs:
        return "Nothing to share: no local decisions."
    remote = RemoteStore.from_profile(profile)
    if remote is None:
        return ("Not in team mode. Set mode='team' + endpoint + token in "
                "~/.contexer/config.toml to share.")
    key = canonical_repo_key(store._git(repo_path, "remote", "get-url", "origin"))
    return _push_batch(remote, decs, key, profile.endpoint)


def share_global(*, profile: Profile | None = None) -> str:
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
        drain_outbox(profile)  # queued shares go out first, so ordering is preserved
    except Exception:
        pass
    decs = store.get_shareable_global()
    if not decs:
        return "Nothing to share: no global rules."
    remote = RemoteStore.from_profile(profile)
    if remote is None:
        return ("Not in team mode. Set mode='team' + endpoint + token in "
                "~/.contexer/config.toml to share.")
    return _push_batch(remote, decs, None, profile.endpoint)


def share(repo_path: str, decision_id: str = "", *, profile: Profile | None = None) -> str:
    """Push one local decision to your team cloud context; return a human-readable status.

    Local-first: never raises for cloud problems — returns a message and leaves the local
    decision untouched. `decision_id` selects the decision (full id / 8-char prefix); omit
    to share the most recent. `profile` defaults to load_profile()."""
    profile = profile or load_profile()
    try:
        drain_outbox(profile)  # queued shares go out first, so ordering is preserved
    except Exception:
        pass  # a broken drain (e.g. disk error saving the outbox) must not block this share
    dec = store.get_shareable(repo_path, decision_id)
    if dec is None:
        return "Nothing to share: no matching local decision."
    remote = RemoteStore.from_profile(profile)
    if remote is None:
        return ("Not in team mode. Set mode='team' + endpoint + token in "
                "~/.contexer/config.toml to share.")
    key = canonical_repo_key(store._git(repo_path, "remote", "get-url", "origin"))
    server_id = with_local_fallback(
        lambda: remote.push_decision(**_dec_push_kwargs(dec, key)),
        default=None, action="share decision")
    return _finish_share(dec, key, server_id, profile.endpoint)


def share_ids(repo_path: str, decision_ids: list, *, profile: Profile | None = None) -> str:
    """Share a selection of decisions (a multi-pick) in ONE batched call, returning a combined
    status. An empty list shares the most recent (delegates to share('')). Outbox + local-first
    guarantees are preserved (a failed chunk is queued); unknown/typo'd ids are REPORTED, not
    silently dropped; the loaded profile is threaded through to avoid re-reading config.toml."""
    profile = profile or load_profile()
    if not decision_ids:
        return share(repo_path, "", profile=profile)
    try:
        drain_outbox(profile)  # queued shares go out first, so ordering is preserved
    except Exception:
        pass
    projs, missing = _resolve_ids(repo_path, decision_ids)
    if not projs:
        return _no_match_status(missing)
    remote = RemoteStore.from_profile(profile)
    if remote is None:
        return ("Not in team mode. Set mode='team' + endpoint + token in "
                "~/.contexer/config.toml to share.")
    key = canonical_repo_key(store._git(repo_path, "remote", "get-url", "origin"))
    return _prepend_unknown(_push_batch(remote, projs, key, profile.endpoint), missing)


# ── async share path (#108) ────────────────────────────────────────────────────────
# The in-loop server.share_decision tool awaits these so a wedged push is CANCELLABLE at
# the tool's deadline (no leaked worker thread / open socket). They mirror share / share_ids
# above line-for-line except the push is awaited, and reuse the same local helpers
# (_finish_share, _dec_push_kwargs, _payload, _enqueue) so no logic drifts between the paths.

async def share_async(repo_path: str, decision_id: str = "", *,
                      profile: Profile | None = None) -> str:
    """Async twin of :func:`share`. Same local-first contract: never raises for cloud
    problems, leaves the local decision untouched, queues on failure."""
    profile = profile or load_profile()
    try:
        await adrain_outbox(profile)  # queued shares go out first, so ordering is preserved
    except Exception:
        pass  # a broken drain (e.g. disk error) must not block this share
    dec = store.get_shareable(repo_path, decision_id)
    if dec is None:
        return "Nothing to share: no matching local decision."
    remote = RemoteStore.from_profile(profile)
    if remote is None:
        return ("Not in team mode. Set mode='team' + endpoint + token in "
                "~/.contexer/config.toml to share.")
    key = canonical_repo_key(store._git(repo_path, "remote", "get-url", "origin"))
    server_id = await awith_local_fallback(
        lambda: remote.apush_decision(**_dec_push_kwargs(dec, key)),
        default=None, action="share decision")
    return _finish_share(dec, key, server_id, profile.endpoint)


async def share_ids_async(repo_path: str, decision_ids: list, *,
                          profile: Profile | None = None) -> str:
    """Async twin of :func:`share_ids`: one batched (awaited) push per _BATCH_SIZE, unknown ids
    reported, capacity-skipped rows re-queued. An empty list shares the most recent."""
    profile = profile or load_profile()
    if not decision_ids:
        return await share_async(repo_path, "", profile=profile)
    try:
        await adrain_outbox(profile)  # queued shares go out first, so ordering is preserved
    except Exception:
        pass
    projs, missing = _resolve_ids(repo_path, decision_ids)
    if not projs:
        return _no_match_status(missing)
    remote = RemoteStore.from_profile(profile)
    if remote is None:
        return ("Not in team mode. Set mode='team' + endpoint + token in "
                "~/.contexer/config.toml to share.")
    key = canonical_repo_key(store._git(repo_path, "remote", "get-url", "origin"))
    return _prepend_unknown(await _apush_batch(remote, projs, key, profile.endpoint), missing)


def enqueue_ids_for_retry(repo_path: str, decision_ids: list) -> int:
    """Queue the given decisions (by id) into the outbox so a later drain retries them.

    Called when an in-loop share is CANCELLED by its deadline (server.share_decision timeout)
    before it could push or queue them itself: cancellation bypasses share_async's own
    enqueue-on-failure, so without this the tool's "the outbox retries it" message would be an
    empty promise. Idempotent — `_enqueue` dedups by decision_id and a re-push is idempotent
    server-side, so queuing a decision that may already have been sent is safe. An empty list
    queues the most recent shareable (matching `share_async('')`). Missing ids are skipped.
    Returns the count queued."""
    ids = decision_ids or [""]  # "" -> most recent, matching share_async("")
    key = canonical_repo_key(store._git(repo_path, "remote", "get-url", "origin"))
    queued = 0
    for did in ids:
        dec = store.get_shareable(repo_path, did)
        if dec is not None:
            _enqueue(_payload(dec, key))
            queued += 1
    return queued
