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
from contexer.remote import RemoteStore, _push_arg, with_local_fallback
from contexer.repo_key import canonical_repo_key

# Outbox cap: push_decision is idempotent on decision_id server-side, so a queued entry is
# never dropped for age or attempt count in v1 - retrying is always safe. This count is the
# only bound, so a long-offline stretch can't grow ~/.contexer/.outbox.json without limit.
_OUTBOX_CAP = 50

# Max decisions per push_decisions batch. MUST stay <= contexer-teams' PUSH_DECISIONS_MAX (50):
# the server rejects a larger array, so `share --all` / drain chunk into calls of this size. One
# call per chunk (not per decision) is what stops a bulk share from trickling into the web UI one
# row at a time. Kept in sync with the cloud like _WIRE_SOURCES above.
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


def _outbox_arg(entry: dict) -> dict:
    """Build a push wire-arg from a queued outbox entry (the shape _payload / share() enqueue)."""
    return _push_arg(
        type=entry.get("type"), content=entry.get("content"), repo=entry.get("repo"),
        rationale=entry.get("rationale"), confidence=entry.get("confidence"),
        evidence=entry.get("evidence"), source=_wire_source(entry.get("source")),
        decision_id=entry.get("decision_id"))


def drain_outbox(profile: Profile | None = None) -> int:
    """Retry queued pushes in chunks of _BATCH_SIZE (one network call per chunk), FIFO, and return
    how many succeeded.

    No-op (0) when the outbox is empty or team sync isn't configured. Stops at the FIRST failed
    chunk and leaves that chunk plus everything after it queued: the cloud is likely still down, so
    hammering the rest would be pointless (with_local_fallback's warn-once already keeps stderr
    quiet). A chunk is all-or-nothing (one server transaction), so on failure every entry in it is
    kept and its attempt count bumped. Entries are never dropped for age/attempts here - see
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
            lambda chunk=chunk: remote.push_decisions([_outbox_arg(e) for e in chunk]),
            default=None, action="drain queued share")
        if res is None:
            for e in chunk:
                e["attempts"] = e.get("attempts", 0) + 1
            _save_outbox(_reconcile_with_disk(entries[start:], sent_ids))
            return sent
        _saved_ids, skipped_ids = res
        skipped = set(skipped_ids)
        # Saved entries drop out of the outbox (added to sent_ids); entries the server skipped
        # (personal context at capacity) are NOT marked sent, so _reconcile_with_disk keeps them
        # queued to retry once space frees.
        for e in chunk:
            if e.get("decision_id") not in skipped:
                sent_ids.add(e.get("decision_id"))
        sent += len(chunk) - len(skipped_ids)
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


def _proj_arg(dec: dict, key) -> dict:
    """Build a push wire-arg from a share projection (store._share_projection shape)."""
    return _push_arg(
        type=dec["type"], content=dec["content"], repo=key,
        confidence=dec["confidence"], evidence=dec["evidence"],
        source=_wire_source(dec["source"]), decision_id=dec["id"])


def _push_projections(remote: RemoteStore, projs: list[dict], key) -> str:
    """Push share projections to your PERSONAL cloud in chunks of _BATCH_SIZE - one network call
    per chunk, not per decision - so a bulk share lands as a batch instead of trickling in.

    Local-first: stops at the FIRST failed chunk (cloud likely down) and queues that chunk plus
    everything after it in the outbox, so no share intent is lost. A chunk the server accepts but
    can only partially store (personal context at capacity) has its skipped rows re-queued to drain
    later. Returns a human-readable status. Shared by share_all and share_ids."""
    total = len(projs)
    sent = 0
    at_capacity = 0
    for start in range(0, total, _BATCH_SIZE):
        chunk = projs[start:start + _BATCH_SIZE]
        res = with_local_fallback(
            lambda chunk=chunk: remote.push_decisions([_proj_arg(p, key) for p in chunk]),
            default=None, action="share decisions")
        if res is None:
            queued = 0
            try:
                for rest in projs[start:]:
                    _enqueue(_payload(rest, key))
                    queued += 1
            except Exception:
                # The outbox write itself died mid-queue (e.g. disk error). Report the exact split
                # - some decisions may already be in the outbox - and how to retry the rest.
                return (f"Shared {sent} of {total} decision(s), then the cloud became "
                        f"unreachable or auth was rejected (see the warning above). Queued "
                        f"{queued} of the remaining {total - sent} for retry before the "
                        "outbox write failed - run `contexer share --all` again to queue "
                        "the rest; your local decisions are unchanged.")
            return (f"Shared {sent} of {total} decision(s). The rest are queued and "
                    "will retry automatically at the next session start (cloud "
                    "unreachable or auth rejected - see the warning above).")
        _saved_ids, skipped_ids = res
        sent += len(chunk) - len(skipped_ids)
        if skipped_ids:
            # Server stored what fit; the rest are at capacity. Re-queue only those (best-effort)
            # so valid siblings aren't blocked and the intent isn't lost.
            skipped = set(skipped_ids)
            for p in chunk:
                if p["id"] in skipped:
                    try:
                        _enqueue(_payload(p, key))
                    except Exception:
                        pass
            at_capacity += len(skipped_ids)
    if at_capacity:
        return (f"Synced {sent} decision(s) to your personal cloud context; {at_capacity} could "
                "not be stored (context at capacity) and were queued - delete some decisions to "
                "sync them. Teammates won't see these until team promotion ships.")
    return (f"Synced {sent} decision(s) to your personal cloud context - "
            "teammates won't see these until team promotion ships.")


def share_all(repo_path: str, *, profile: Profile | None = None) -> str:
    """Push every non-ignored local decision to your team cloud context, oldest first, in one
    batched call per _BATCH_SIZE decisions.

    Same local-first contract as share(): never raises for cloud problems. Stops at the FIRST
    failed chunk (the cloud is likely down - drain_outbox semantics) and queues the failed chunk
    plus everything after it in the outbox, so no share intent is lost."""
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
    return _push_projections(remote, decs, key)


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
        lambda: remote.push_decision(
            type=dec["type"], content=dec["content"], repo=key,
            confidence=dec["confidence"], evidence=dec["evidence"],
            source=_wire_source(dec["source"]), decision_id=dec["id"]),
        default=None, action="share decision")
    if server_id is None:
        # Cloud unreachable OR auth rejected - either way a queued retry can succeed
        # later (auth: after the user re-logs-in; unreachable: once the cloud is back),
        # so both degradations enqueue rather than losing the share.
        try:
            _enqueue(_payload(dec, key))
        except Exception:
            # Even queueing can fail (disk full, temp-dir perms). share() never raises -
            # and the message must not promise a retry that was never recorded.
            return ("Share failed: cloud unreachable or auth rejected (see the warning "
                    "above). Your local decision is unchanged.")
        return ("Share failed: cloud unreachable or auth rejected (see the warning above). "
                "Queued - it will retry automatically at the next session start.")
    # Honest about scope: v1 push_decision writes to the CALLER's personal cloud context
    # only (see module docstring) - teammates get nothing until team promotion (Track A)
    # ships, so the success message must not imply the decision is visible to the team yet.
    return (f"Synced decision to your personal cloud context (server id={server_id}) - "
            "teammates won't see this until team promotion ships.")


def share_ids(repo_path: str, decision_ids: list, *, profile: Profile | None = None) -> str:
    """Share a selection of decisions (a multi-pick) in ONE batched call, returning a combined
    status. An empty list shares the most recent (delegates to share('')). Outbox + local-first
    guarantees are preserved (a failed chunk is queued for retry); the loaded profile is threaded
    through to avoid re-reading config.toml per id."""
    profile = profile or load_profile()
    if not decision_ids:
        return share(repo_path, "", profile=profile)
    try:
        drain_outbox(profile)  # queued shares go out first, so ordering is preserved
    except Exception:
        pass
    # Resolve each id, tracking any that don't match a local decision so an unknown id (e.g. a typo
    # in a multi-pick) is REPORTED rather than silently dropped.
    projs, missing = [], []
    for did in decision_ids:
        proj = store.get_shareable(repo_path, str(did))
        (missing if proj is None else projs).append(did if proj is None else proj)
    if not projs:
        unknown = ", ".join(str(m)[:8] for m in missing)
        return f"Nothing to share: no matching local decision (unknown id(s): {unknown})."
    remote = RemoteStore.from_profile(profile)
    if remote is None:
        return ("Not in team mode. Set mode='team' + endpoint + token in "
                "~/.contexer/config.toml to share.")
    key = canonical_repo_key(store._git(repo_path, "remote", "get-url", "origin"))
    status = _push_projections(remote, projs, key)
    if missing:
        unknown = ", ".join(str(m)[:8] for m in missing)
        status = f"Skipped {len(missing)} unknown id(s): {unknown}.\n{status}"
    return status
