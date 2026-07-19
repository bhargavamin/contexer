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
"""
from __future__ import annotations

import json
import time

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


def _enqueue(payload: dict) -> None:
    """Queue a failed push. Dedupes by decision_id (re-sharing the same decision while
    offline replaces the queued entry - fresh content wins) and caps at _OUTBOX_CAP,
    dropping the oldest entries beyond that."""
    entries = [e for e in _load_outbox() if e.get("decision_id") != payload.get("decision_id")]
    entries.append(payload)
    if len(entries) > _OUTBOX_CAP:
        entries = entries[-_OUTBOX_CAP:]
    _save_outbox(entries)


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
    the two drains serialize an entry identically (source coerced through _wire_source)."""
    return dict(
        type=entry.get("type"), content=entry.get("content"), repo=entry.get("repo"),
        rationale=entry.get("rationale"), confidence=entry.get("confidence"),
        evidence=entry.get("evidence"), source=_wire_source(entry.get("source")),
        decision_id=entry.get("decision_id"))


def _dec_push_kwargs(dec: dict, key) -> dict:
    """push_decision kwargs for one shareable decision. Shared by share / share_all /
    share_async so every share path puts the same decision on the wire identically."""
    return dict(
        type=dec["type"], content=dec["content"], repo=key,
        confidence=dec["confidence"], evidence=dec["evidence"],
        source=_wire_source(dec["source"]), decision_id=dec["id"])


def _finish_share(dec: dict, key, server_id) -> str:
    """Turn one push outcome into the user-facing status (shared by share + share_async).

    On failure (server_id is None: cloud unreachable OR auth rejected) enqueue the decision
    so a later drain retries it - either way a queued retry can succeed later, so both
    degradations queue rather than losing the share. On success, return the honest
    personal-scope message (teammates don't see it until team promotion ships)."""
    if server_id is None:
        try:
            _enqueue(_payload(dec, key))
        except Exception:
            # Even queueing can fail (disk full, temp-dir perms). Never raise - and the
            # message must not promise a retry that was never recorded.
            return ("Share failed (see the warning above for why). Your local decision is unchanged.")
        return ("Share failed (see the warning above for why). Queued - it will retry "
                "automatically at the next session start.")
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


def _drain_mark(chunk: list[dict], res: tuple[list[str], list[dict]], sent_ids: set) -> int:
    """Mark a successfully-drained chunk: saved AND permanently-invalid entries -> sent_ids (dropped
    from the outbox on the final reconcile - invalid ones can never sync, so stop retrying them);
    only TRANSIENT capacity skips stay queued. Returns the count genuinely saved."""
    _saved, skipped = res
    retry, _invalid = _split_skips(skipped)
    for e in chunk:
        if e.get("decision_id") not in retry:
            sent_ids.add(e.get("decision_id"))
    return len(chunk) - len(skipped)


def _push_batch(remote: RemoteStore, decs: list[dict], key) -> str:
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
        sent += len(chunk) - len(skipped)
        retry, inv = _split_skips(skipped)
        invalid += inv
        if retry:
            requeued, dropped = _requeue_skipped(chunk, key, retry)
            at_capacity += requeued
            lost += dropped
    return _batch_success_status(sent, at_capacity, invalid, lost)


async def _apush_batch(remote: RemoteStore, decs: list[dict], key) -> str:
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
        sent += _drain_mark(chunk, res, sent_ids)
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
        sent += _drain_mark(chunk, res, sent_ids)
    _save_outbox(_reconcile_with_disk([], sent_ids))
    return sent


def _payload(dec: dict, key) -> dict:
    """Outbox entry for one wire-projected decision (same shape share() enqueues)."""
    return {
        "decision_id": dec["id"], "type": dec["type"], "content": dec["content"],
        "repo": key, "rationale": None, "confidence": dec["confidence"],
        "evidence": dec["evidence"], "source": _wire_source(dec["source"]),
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
    return _push_batch(remote, decs, key)


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
    return _finish_share(dec, key, server_id)


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
    return _prepend_unknown(_push_batch(remote, projs, key), missing)


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
    return _finish_share(dec, key, server_id)


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
    return _prepend_unknown(await _apush_batch(remote, projs, key), missing)


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
