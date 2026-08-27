"""Team-context cache + merge (C5).

Path B: the local store stays the base; TEAM context pulled from the Teams backend is
cached SEPARATELY (never written into store.py entries[]) and merged in at read time.
Keeping remote rows out of the local store means they are never subject to the local
novelty filter, `_keep_top` eviction, or the revision engine, and tombstone (`deleted[]`)
reconciliation is a trivial set operation.

Cache file: ~/.contexer/.team_{slug}.json = {repo_key, cursor, decisions[]}.
Only `scope == "team"` rows are cached (the local store already holds the user's personal
decisions; fresh-clone restore of the personal cloud mirror is a later follow-up).
"""
from __future__ import annotations

import json
import subprocess
import sys
import time

from contexer import config, revisions, share, sidecars, store
from contexer.remote import RemoteAuthError, RemoteDecision, RemoteStore, with_local_fallback
from contexer.repo_key import canonical_repo_key

# Max team rows rendered into a single get_context (mirrors the local filtered display).
_TEAM_DISPLAY = 25

# Min seconds between mid-session delta polls (C7) — keeps UserPromptSubmit cheap. This is
# the healthy-cloud cadence; consecutive sync failures back this off exponentially (see
# _poll_interval), up to _POLL_MAX_INTERVAL, so a down cloud isn't hammered every prompt.
_POLL_MIN_INTERVAL = 15

# Ceiling for the backoff interval (15 min) — a down cloud never gets polled less often
# than this, but also never floods a prompting session with retries forever.
_POLL_MAX_INTERVAL = 900

# Cap the in-cache sync log (last N batches). A consumer further behind than this misses the
# proactive "just approved" banner for old batches only — those rows are still in the cache
# (get_context / SessionStart render them), so this is a UX degradation, never data loss.
_SYNC_LOG_CAP = 50

# Fields persisted per cached team decision (the get_context wire projection).
_ROW_FIELDS = ("id", "type", "title", "content", "rationale", "repo", "agent", "scope",
               "local_decision_id", "team_id", "team_name", "reconciliation")

# How stale last_ok_at must be before format_team_section tags the header as possibly
# stale (the "quietly dead refresher" failure mode: rows keep rendering with no signal).
_STALE_AFTER = 24 * 3600


def _cache_path(repo_path: str):
    return store.STORE_DIR / sidecars.filename("team_cache", slug=store.repo_slug(repo_path))


def _empty_cache() -> dict:
    return {"repo_key": None, "cursor": None, "decisions": []}


def _load_cache(repo_path: str) -> dict:
    path = _cache_path(repo_path)
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("decisions"), list):
            return data
    return _empty_cache()


def _save_cache(repo_path: str, data: dict) -> None:
    store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    store.atomic_write(_cache_path(repo_path), json.dumps(data, indent=2, ensure_ascii=False))


# The credentials file lives in the same `.team_*` namespace as the caches but is NOT one:
# `clear_caches` runs from `auth.login`, which has just written it.
_CREDS_FILE = sidecars.filename("team_creds")


def clear_caches() -> int:
    """Drop every cached team pull on this machine; return how many files were removed.

    Called when the logged-in account changes (issue #232). Nothing on disk records WHICH
    Teams account a cache belongs to - the pull cache holds only `repo_key`/`cursor`/rows, and
    the access token is opaque rather than a JWT - so there is no identity to compare against
    and no way to re-key. Discarding is what is actually available.

    Discarding is also load-bearing rather than tidy: `_sync` is a cursor-based DELTA. It
    upserts what the server returns and removes only what the server explicitly reports as
    deleted, and a different account never reports the previous account's rows as deleted
    because it never had them. So without this they persist indefinitely and keep rendering at
    session start as current team context. Resetting the cursor alone would not do it - a full
    re-pull still only ADDS.

    The per-consumer delta-poll markers (`.team_seen_<slug>_<consumer>.json`) go with them, and
    that pairing is not optional: they hold a high-water `seq` into the CACHE's own sync log,
    whose counter restarts at 0 with the cache. A marker left at 40 beside a rebuilt log would
    silently suppress every batch until the new log passed 40. `_read_seen` self-heals a
    MISSING marker (caught-up, injects nothing that once), which is exactly the right landing
    state here - SessionStart renders the fresh backlog.

    Fail-soft per file: a cache that will not delete is left behind rather than raising into a
    login that has already succeeded."""
    try:
        paths = sorted(store.STORE_DIR.glob(".team_*.json"))
    except OSError:
        return 0
    removed = 0
    for path in paths:
        # Ask the declaration, not a literal. Renaming the credentials file in auth.py used to
        # make login delete the token it had just written, because this exclusion spelled the
        # old name; anything the declaration calls durable is now skipped for the same reason.
        if sidecars.lifetime_for(path.name) is None:
            continue
        try:
            path.unlink()
            removed += 1
        except OSError:
            pass
    return removed


def _row_to_dict(rd: RemoteDecision) -> dict:
    return {
        "id": rd.id,
        "type": rd.type,
        "title": rd.title,
        "content": rd.content,
        "rationale": rd.rationale,
        "repo": rd.repo,
        "agent": rd.agent,
        "scope": rd.scope,
        "local_decision_id": rd.local_decision_id,
        "team_id": rd.team_id,
        "team_name": rd.team_name,
        "reconciliation": rd.reconciliation,
    }


def _apply_reconciliation_metadata(repo_path: str, row: dict) -> None:
    """Turn author-scoped team-ahead metadata into local review state, fail-soft."""
    local_id = row.get("local_decision_id")
    rec = row.get("reconciliation")
    if not local_id or not isinstance(rec, dict):
        return
    state = rec.get("state")
    try:
        if state == "team_ahead":
            store.attach_team_reconciliation_proposal(
                repo_path, local_id, content=row.get("content", ""),
                title=row.get("title") or "", team_id=row.get("team_id") or "",
                team_name=row.get("team_name") or "", team_head=rec.get("teamHead") or "")
        elif state == "in_sync":
            store.clear_team_reconciliation_proposal(
                repo_path, local_id, team_head=rec.get("teamHead") or "")
    except (OSError, ValueError, TypeError):
        pass


def _sync(repo_path: str, profile: config.Profile,
         *, timeout: float | None = None) -> tuple[list[dict], list[str]] | None:
    """Fetch team context incrementally and merge it into the cache.

    Returns (new_or_updated_rows, removed_ids) on success, or None (no-op) when not in team
    mode, when the repo has no git origin, or when the cloud is unreachable / rejects the
    token (degrades via with_local_fallback, leaving any existing cache untouched).

    Observability: every ATTEMPTED sync (i.e. we got past the mode/origin checks and
    actually tried the network) records its outcome as a `last_sync` cache key - {"at",
    "ok", "duration_ms", "consecutive_failures"} plus either {"upserted", "removed"} on
    success or {"error"} on failure - so `contexer status` can show when sync last ran and
    why it degraded, without itself touching the network. `at` is stamped once, from
    `start` (captured before the network call), so it means "when the sync attempt began"
    for both the success and degraded paths - not end-of-write, which would drift by
    however long the cache write itself takes. `consecutive_failures` resets to 0 on
    success and increments on failure, driving the poll backoff (see _poll_interval). A
    successful sync also stamps a top-level `last_ok_at` (epoch float) - the freshness
    signal `format_team_section` uses to tag its header when the cache has gone stale.

    `timeout` (seconds) overrides RemoteStore's default transport timeout - used by the
    SessionStart `refresh` seam to bound how long a slow cloud can stall a session start.
    None (the default) keeps RemoteStore.from_profile's own default (10.0s) for callers
    that don't care (poll, poll_nonblocking, the CLI `pull` command)."""
    kwargs = {} if timeout is None else {"timeout": timeout}
    remote = RemoteStore.from_profile(profile, **kwargs)
    if remote is None:
        return None  # local mode / not configured - no cache file for a local-only repo
    key = canonical_repo_key(store.run_git(repo_path, "remote", "get-url", "origin"))
    if key is None:
        return None  # no git remote - nothing to sync on, no cache file

    cache = _load_cache(repo_path)
    start = time.time()
    # `remote` already classifies the failure (RemoteAuthError vs unreachable), but
    # with_local_fallback returns only `default`, so that classification used to be thrown away
    # and every degradation was recorded as "degraded". A token the server has REVOKED still
    # looks unexpired locally, so with the type discarded there was no evidence anywhere that
    # the cause was authentication - which is how an auth failure reads as an outage and sends
    # the developer to check their network. Recording the kind here keeps with_local_fallback's
    # contract exactly as it was (it still warns once and still returns None) and gives
    # `contexer pull` / the console's Pull button something honest to act on.
    failure: dict = {}

    def _attempt():
        try:
            return remote.get_context(repo=key, updated_since=cache.get("cursor"))
        except RemoteAuthError:
            failure["kind"] = "auth"
            raise

    ctx = with_local_fallback(_attempt, default=None, action="pull team context")
    duration_ms = int((time.time() - start) * 1000)
    if ctx is None:
        # Degraded (cloud unreachable / auth rejected): record the attempt but leave the
        # decisions/cursor exactly as loaded - a transient outage must never wipe the cache.
        _save_cache(repo_path, {
            **cache,
            "last_sync": {"at": start, "ok": False, "duration_ms": duration_ms,
                         "error": failure.get("kind", "degraded"),
                         "consecutive_failures": _consecutive_failures(cache) + 1},
        })
        return None  # degraded — leave the existing cache in place

    by_id: dict[str, dict] = {d["id"]: d for d in cache.get("decisions", [])}
    new_rows: list[dict] = []
    removed: list[str] = []
    for rd in ctx.decisions:
        if rd.scope != "team":
            continue  # local store already holds personal; cache team rows only
        if rd.repo and rd.repo != key:
            # Defense-in-depth: never trust a row's own repo tag over the key we queried
            # for; only reject when repo is present AND mismatched (a valid row can
            # legitimately carry repo=None). Also drop any STALE copy already cached
            # under this id - a row whose scoping was corrected/moved server-side must
            # stop rendering here even though no explicit deletion tombstone arrived
            # for it; leaving the old value in `by_id` would keep serving it forever.
            if by_id.pop(rd.id, None) is not None:
                removed.append(rd.id)
            continue
        row = _row_to_dict(rd)
        if by_id.get(rd.id) == row:
            # Unchanged re-send: the live server's updatedSince filter is INCLUSIVE, so
            # rows stamped exactly at the cursor come back on every delta fetch. Treating
            # them as new would re-inject the same decisions every poll window. It also
            # must not re-apply reconciliation metadata: a user may have approved the
            # previous team-created Suggested Update since the row was cached locally.
            continue
        _apply_reconciliation_metadata(repo_path, row)
        by_id[rd.id] = row
        new_rows.append(row)
    for dead in ctx.deleted:
        if by_id.pop(dead, None) is not None:
            removed.append(dead)

    saved = {
        **cache,  # preserve extra keys (last_poll_at, seq, sync_log) across a sync
        "repo_key": key,
        "cursor": ctx.cursor or cache.get("cursor"),  # null cursor (empty pull) keeps prior
        "decisions": list(by_id.values()),
        "last_sync": {"at": start, "ok": True, "duration_ms": duration_ms,
                     "upserted": len(new_rows), "removed": len(removed),
                     "consecutive_failures": 0},
        "last_ok_at": start,
    }
    if new_rows:
        # Record this batch in the per-repo sync log under a fresh monotonic seq. Per-consumer
        # high-water markers (.team_seen_<slug>_<consumer>.json) read this log, so EACH consumer
        # (a Claude and a Codex session on the same repo) is shown every batch exactly once,
        # independently of the others. seq is a stored counter, never wall-clock, so ordering
        # is deterministic and immune to clock skew.
        seq = int(cache.get("seq", 0)) + 1
        prior = cache.get("sync_log")
        log = list(prior) if isinstance(prior, list) else []
        log.append({"seq": seq, "ids": [r["id"] for r in new_rows]})
        saved["seq"] = seq
        saved["sync_log"] = log[-_SYNC_LOG_CAP:]  # drop oldest; very stale consumers miss old banners only
    _save_cache(repo_path, saved)
    return (new_rows, removed)


def _consecutive_failures(cache: dict) -> int:
    """Fail-soft read of the last_sync consecutive-failure counter. Missing key, missing
    last_sync, or a corrupt/non-int value all read as 0 - a diagnostic counter must never
    itself become a crash source."""
    last_sync = cache.get("last_sync")
    if not isinstance(last_sync, dict):
        return 0
    n = last_sync.get("consecutive_failures", 0)
    return n if isinstance(n, int) and n >= 0 else 0


def _poll_interval(cache: dict) -> float:
    """Backoff interval (seconds) before the next poll/refresher spawn is due. Healthy
    cloud (0 consecutive failures) keeps the base _POLL_MIN_INTERVAL cadence; each
    consecutive failure doubles it, capped at _POLL_MAX_INTERVAL. The first success after
    an outage resets consecutive_failures to 0, snapping the cadence back immediately."""
    return min(_POLL_MIN_INTERVAL * (2 ** _consecutive_failures(cache)), _POLL_MAX_INTERVAL)


def pull(repo_path: str, *, profile: config.Profile | None = None,
        timeout: float | None = None) -> tuple[int, int]:
    """Fetch team context and update the local cache (incremental). Returns (upserted,
    removed) counts; `(0, 0)` on any no-op (local mode / no origin / cloud unreachable).

    `timeout` overrides the transport timeout (see `_sync`); None keeps the default."""
    profile = profile or config.load_profile()
    result = _sync(repo_path, profile, timeout=timeout)
    if result is None:
        return (0, 0)
    new_rows, removed = result
    return (len(new_rows), len(removed))


def poll(repo_path: str, *, profile: config.Profile | None = None) -> list[dict]:
    """Delta-poll for injection (C7): return the team decisions newly synced this poll.

    Throttled to at most once per `_poll_interval` (base `_POLL_MIN_INTERVAL`, backed off
    exponentially on consecutive sync failures - see `_poll_interval`) so it never adds
    perceptible latency to a prompt. `[]` when throttled, not in team mode, or degraded —
    never raises."""
    profile = profile or config.load_profile()
    if profile.mode != "team" or not profile.endpoint:
        return []  # not configured — no work, no cache file for a local repo
    cache = _load_cache(repo_path)
    if time.time() - cache.get("last_poll_at", 0) < _poll_interval(cache):
        return []  # throttled — skip the network round-trip
    result = _sync(repo_path, profile)
    # Stamp the poll time regardless of outcome (don't hammer a down cloud every prompt).
    stamped = _load_cache(repo_path)
    stamped["last_poll_at"] = time.time()
    _save_cache(repo_path, stamped)
    return result[0] if result else []


def _seen_path(repo_path: str, consumer: str):
    return store.STORE_DIR / sidecars.filename("team_seen", slug=store.repo_slug(repo_path),
                                               consumer=consumer)


def _read_seen(repo_path: str, consumer: str) -> int | None:
    """Highest sync-log seq this consumer has already been shown, or None when it has no
    valid marker (never polled, or the marker is corrupt).

    The caller treats None as 'caught up': it stamps the current log head and injects nothing
    that once. A brand-new consumer therefore does NOT re-inject the existing backlog (its own
    SessionStart already rendered it), and a garbled marker self-heals to caught-up instead of
    crashing or re-spamming the whole log."""
    path = _seen_path(repo_path, consumer)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if isinstance(data, dict) and isinstance(data.get("seq"), int):
        return data["seq"]
    return None


def _write_seen(repo_path: str, consumer: str, seq: int) -> None:
    store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    store.atomic_write(_seen_path(repo_path, consumer), json.dumps({"seq": seq}, ensure_ascii=False))


def _collect_unseen(repo_path: str, cache: dict, consumer: str) -> list[dict]:
    """Rows from every sync-log batch newer than this consumer's high-water mark.

    Advances the marker to the newest logged seq and returns the current version of each row
    (deduped by id, resolved from the cache's decisions list; a since-deleted id is skipped).
    Per-consumer marker + per-repo log = each consumer sees every batch exactly once,
    independently of any other consumer. Fail-soft: any corruption yields an empty injection,
    never an exception."""
    try:
        log = cache.get("sync_log")
        if not isinstance(log, list):
            return []
        seen = _read_seen(repo_path, consumer)
        if seen is None:
            # No valid marker (new consumer or corrupt file): catch up to the current log head
            # and inject nothing this once. SessionStart already rendered everything synced so
            # far, so the delta-poll surfaces only batches logged AFTER this point.
            _write_seen(repo_path, consumer, int(cache.get("seq", 0)))
            return []
        unseen_ids: list = []
        high = seen
        for entry in log:
            if not isinstance(entry, dict):
                continue
            seq = entry.get("seq")
            if not isinstance(seq, int) or seq <= seen:
                continue
            unseen_ids.extend(entry.get("ids") or [])
            high = max(high, seq)
        if high > seen:
            _write_seen(repo_path, consumer, high)
        if not unseen_ids:
            return []
        by_id = {d.get("id"): d for d in cache.get("decisions", [])}
        rows: list[dict] = []
        emitted: set = set()
        for rid in unseen_ids:
            if rid in emitted:
                continue
            emitted.add(rid)
            row = by_id.get(rid)
            if row is not None:  # skip an id deleted since it was logged
                rows.append(row)
        return rows
    except Exception:
        return []


def _drop_legacy_pending(repo_path: str) -> None:
    """Remove a parked-pending file left by an older Contexer (delivery is now the per-consumer
    sync log). Called on every poll (every prompt), so once the legacy file is gone — the
    common case — an existence check up front avoids an unlink()-then-catch syscall on every
    single prompt. Still best-effort — a file that vanishes between the check and the unlink
    is silently ignored."""
    path = store.STORE_DIR / sidecars.filename("team_pending", slug=store.repo_slug(repo_path))
    if path.exists():
        try:
            path.unlink()
        except OSError:
            pass


def _refresh_worker(repo_path: str) -> None:
    """Background refresher body (runs in a detached process spawned by poll_nonblocking):
    the network sync happens OFF the prompt path. `_sync` appends any newly synced rows to the
    cache's sync_log, from which each consumer's poll picks them up exactly once."""
    _sync(repo_path, config.load_profile())


def _spawn_refresh(repo_path: str) -> None:
    """Start a detached background refresher — the hook process never waits on it."""
    # argv is fixed and points at our own interpreter and module — no shell, and no part of
    # it is caller- or user-supplied except repo_path, which arrives as its own argv element.
    # `-P` for the same reason the installed hooks carry it: `-m` prepends the process cwd
    # to sys.path, and this child inherits the per-prompt hook's cwd = the project root, so
    # a session in a checked-out contexer repo would import that repo's own contexer/ source
    # instead of the installed package. stderr is DEVNULL here, so the resulting crash would
    # be invisible rather than reported.
    subprocess.Popen(
        [sys.executable, "-P", "-m", "contexer.team_context", repo_path],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def poll_nonblocking(repo_path: str, consumer: str = "claude", *,
                     profile: config.Profile | None = None) -> list[dict]:
    """C7 delta-poll with ZERO network on the prompt path, per consumer.

    Returns the team rows synced since THIS consumer last polled (read from the per-repo sync
    log via the consumer's own high-water marker, so a Claude and a Codex session on the same
    repo each receive every batch once) and — at most once per `_poll_interval` (base
    _POLL_MIN_INTERVAL, backed off exponentially on consecutive sync failures up to
    _POLL_MAX_INTERVAL) — spawns a detached refresher whose results surface on the NEXT
    prompt. The prompt only ever pays a cache read plus a non-blocking process spawn, so a
    slow-but-healthy cloud (or the full transport timeout) can never stall the user, and a
    down cloud stops being re-spawned every 15s forever. `consumer` defaults to "claude" so
    the original installed Claude hook string keeps working unchanged."""
    profile = profile or config.load_profile()
    if profile.mode != "team" or not profile.endpoint:
        return []  # not configured — no work, no cache file for a local repo
    _drop_legacy_pending(repo_path)  # one-time cleanup of a pre-sync-log parked file
    cache = _load_cache(repo_path)
    new = _collect_unseen(repo_path, cache, consumer)
    if time.time() - cache.get("last_poll_at", 0) >= _poll_interval(cache):
        # Stamp BEFORE spawning so even a crashing refresher can't cause a spawn storm.
        cache["last_poll_at"] = time.time()
        _save_cache(repo_path, cache)
        _spawn_refresh(repo_path)
    return new


# ── Neutral adapter seam (Option A) ────────────────────────────────────────────────
# One pull + one poll entrypoint every adapter (Claude, Codex, Cursor, Gemini) calls
# from its hooks. Both resolve the repo and NEVER raise, so an adapter wires team sync
# without re-implementing the fail-soft try/except. RENDERING is unified separately in
# store.session_start_payload (which appends format_team_section for every adapter).


# SessionStart is the one seam where a slow cloud is directly on the user's critical path
# (a hook running before the assistant can respond), so it trades some freshness for a hard
# ceiling: at most ~3s stall instead of the full 10s transport default. Every other caller
# (poll, poll_nonblocking, the CLI `pull` command, the console's Pull button) is either
# non-blocking or explicitly interactive, so they keep the longer default - this bound is
# deliberately narrow, and reusing this seam for an interactive caller reads as an outage
# against any endpoint that answers in more than 3s.
_SESSION_START_TIMEOUT = 3.0


def refresh(repo_path: str) -> tuple[int, int]:
    """SessionStart pull for ANY adapter. Resolves the repo, refreshes the team cache,
    and NEVER raises — a sync hiccup (offline, bad token, anything) must not break session
    start. Returns (upserted, removed); (0, 0) on no-op / not-team / degraded / error.

    Uses a short transport timeout (see `_SESSION_START_TIMEOUT`) so a slow-but-reachable
    cloud degrades to the existing cache (freshness, never correctness) rather than
    stalling session start for the full default timeout."""
    try:
        repo = store.resolve_repo(repo_path)
        if not repo:
            return (0, 0)
        result = pull(repo, timeout=_SESSION_START_TIMEOUT)
    except Exception:
        return (0, 0)
    try:
        # Drain any pushes queued by an earlier offline/auth-failed share (the outbox).
        # SessionStart is the one seam every adapter already funnels through, so this is
        # where a queued share gets retried automatically. Fail-soft: a drain error must
        # never break session start (mirrors the try/except above around pull()).
        share.drain_outbox()
    except Exception:
        pass
    return result


def poll_for_injection(repo_path: str, consumer: str = "claude") -> list[dict]:
    """Per-prompt delta poll for ANY adapter. Returns the team rows newly synced since THIS
    consumer last polled (empty when throttled / not team mode / degraded). Each consumer
    ("claude", "codex") keeps its own high-water marker, so concurrent sessions on one repo
    don't steal each other's proactive injection. NEVER raises. Adapters format these rows for
    their own host's hook output. `consumer` defaults to "claude" (the original hook string)."""
    try:
        repo = store.resolve_repo(repo_path)
        if not repo:
            return []
        return poll_nonblocking(repo, consumer)
    except Exception:
        return []


def _local_decisions(repo_path: str) -> list[tuple[str, str]]:
    """(id, current-content) for each live local decision - the base team rows dedup against.

    Only `type == "decision"` entries that are not `ignored`, read through the same current-
    revision accessor replay uses. Fail-soft: any load error returns [] so team-context dedup
    degrades to unmodified rendering (this runs inside session-start hooks and must not raise)."""
    try:
        out: list[tuple[str, str]] = []
        for e in store.load(repo_path).get("entries", []):
            if e.get("type") != "decision" or e.get("status") == "ignored":
                continue
            content = revisions.current_content(e)
            if content:
                out.append((e.get("id", ""), content))
        return out
    except Exception:
        return []


def _best_local_overlap(content: str, local_tokens: list[tuple[str, set]]) -> tuple[str, float]:
    """Highest token overlap (store's novelty metric) of `content` against the local
    decisions, with the winning local id. ('', 0.0) when there are no local decisions."""
    team_tok = store._tokenize(content)
    best_id, best = "", 0.0
    for lid, ltok in local_tokens:
        r = store._overlap_ratio(team_tok, ltok)
        if r > best:
            best_id, best = lid, r
    return best_id, best


def count_deferred_architecture(repo_path: str, *, cache: dict | None = None,
                                local_tokens: list[tuple[str, set]] | None = None) -> int:
    """Count of cached architecture-typed team rows that `format_team_section(...,
    defer_architecture=True)` would actually defer to the count-only pointer - i.e.
    excluding rows that collapse to the cheap local-ratification line regardless
    (>= 0.7 overlap with a local decision) and so render unaffected by deferral.

    Mirrors the defer-eligibility check inside `format_team_section` exactly, so
    `store.session_start_payload`'s status suffix can report how many synced rows
    actually landed as full/ratified content vs. how many are hidden behind the
    pointer - instead of the raw cache size, which overstates it once deferral
    hides a chunk of the cache behind one summary line. Fail-soft: any load error
    yields 0 (status suffix degrades to the pre-deferral raw count).

    `cache`/`local_tokens`: optional pre-loaded snapshots (see `session_team_section`) so a
    caller that also renders the section via `format_team_section` reads ONE consistent
    snapshot of both the team cache and the local store, rather than each function reloading
    independently - closing the window where a concurrent refresh or local decision update
    between separate reads could desync the count from what was actually rendered."""
    try:
        cache = cache if cache is not None else _load_cache(repo_path)
        arch_rows = [r for r in cache.get("decisions", []) if r.get("type") == "architecture"]
        if not arch_rows:
            return 0
        if local_tokens is None:
            local_tokens = [(lid, store._tokenize(c)) for lid, c in _local_decisions(repo_path)]
        return sum(
            1 for r in arch_rows
            if _best_local_overlap(r.get("content", ""), local_tokens)[1] < 0.7
        )
    except Exception:
        return 0


def _format_staleness(age_seconds: float) -> str:
    """Human staleness suffix for the team-context header: whole hours, switching to whole
    days once the gap reaches 48h (e.g. '30 hours ago', '2 days ago')."""
    if age_seconds >= 48 * 3600:
        days = int(age_seconds // 86400)
        return f"{days} day{'' if days == 1 else 's'} ago"
    hours = int(age_seconds // 3600)
    return f"{hours} hour{'' if hours == 1 else 's'} ago"


def _record_render(repo_path: str, cache: dict, *, rows: int, chars: int) -> None:
    """Best-effort render-size telemetry (measure-don't-guess input for future display-cap
    tuning) — never raises, since this runs inline inside every hook that injects context.
    Only touches a cache file that already exists: a pure-local repo (no team cache) must
    never grow one just because get_context ran through format_team_section.

    `cache` (the snapshot `format_team_section` loaded before rendering) is intentionally
    NOT spread back to disk here: a background refresher (poll_nonblocking) runs in a
    separate process and can complete between that initial load and this write, landing
    fresh `decisions`/`cursor`/`last_ok_at`/`consecutive_failures`. Writing the caller's
    now-stale snapshot would silently clobber that fresh write, so this re-loads the cache
    fresh immediately before saving and sets ONLY `last_render` on that fresh copy."""
    if not _cache_path(repo_path).exists():
        return
    try:
        fresh = _load_cache(repo_path)
        fresh["last_render"] = {"at": time.time(), "rows": rows, "chars": chars}
        _save_cache(repo_path, fresh)
    except Exception:
        pass  # telemetry must never break the render it's measuring


def format_team_section(repo_path: str, query: str = "", entry_type: str = "",
                        *, defer_architecture: bool = False, limit: int = 0,
                        cache: dict | None = None,
                        local_tokens: list[tuple[str, set]] | None = None) -> str:
    """Render the cached team context as a '## Team context (synced)' markdown block, or ''.

    Filtered like `store.get_context` (by `entry_type` and a `query` substring). Each row is
    tagged `[scope=team]` so the reading agent treats it as provenance, not tool routing.

    `defer_architecture` (keyword-only, default False): when True AND no explicit `entry_type`
    was requested, architecture-typed rows are pulled out of the render set (before the
    `_TEAM_DISPLAY` cap is applied) and replaced with a single deferred-count line pointing at
    `get_context(entry_type="architecture")` — mirroring the local store's SessionStart
    deferral of architecture decisions to a count-only pointer. An explicit `entry_type`
    always bypasses deferral: naming a type is a targeted JIT fetch, not a bulk render, so it
    must return full content regardless of `defer_architecture`.

    `limit` (keyword-only, default 0 = use `_TEAM_DISPLAY`): raises the render cap for that
    same targeted JIT fetch, so `get_context(entry_type="architecture")` (the exact call the
    deferred-count pointer above tells the model to make) can actually return MORE than
    `_TEAM_DISPLAY` rows when the cache holds more deferred architecture decisions than that -
    otherwise the pointer's "for full content" promise would silently truncate again. A cache
    still holding more than the effective cap after that renders a "showing N of M" note,
    same shape as `get_context`'s own filtered-display truncation note, so a genuine excess is
    visible rather than silently dropped.

    Each row renders title-led, same rule as a local decision (`store.title_and_body`): the
    row's own `title` when the cloud sent one, else one derived from `content` here (display
    time only - never written back into the cache), with the content line skipped entirely
    when it would merely repeat the title.

    Rows that duplicate a LOCAL decision are deduped so the same rule is not injected twice
    (once local, once team). Token overlap against the local store decides - reusing the very
    metric the novelty filter thresholds at 0.7: >= 0.7 collapses to a one-line "ratifies local
    decision …" pointer (the team's ratification is kept as provenance, not the duplicate text);
    0.5-0.7 renders in full but tagged with the local id so a genuine divergence stays visible;
    below 0.5 renders exactly as before. The _TEAM_DISPLAY cap applies AFTER collapsing (a
    collapsed one-liner still counts as a row). Fail-soft: if the local store can't be read,
    `_local_decisions` yields [] and every row renders unmodified - this never raises.

    The header is tagged '(synced N hours/days ago - may be stale)' when the cache's
    `last_ok_at` is older than `_STALE_AFTER` — this is the signal for a quietly-dead
    refresher (rows keep rendering with no indication sync stopped working). A cache with
    no `last_ok_at` at all (old-format cache, pre-dating this field) is treated as unknown
    freshness and left untagged, to avoid false alarms right after an upgrade.

    On a non-empty result, also records `last_render` telemetry (rows rendered + char
    count) into the cache - see `_record_render` - so display-cap/deferral decisions can
    be made from real data instead of guessing.

    `cache`/`local_tokens` (keyword-only, default None = load fresh): lets a caller that
    also needs `count_deferred_architecture`'s number (see `session_team_section`) pass in
    an already-loaded team-cache snapshot and local-decision token list, so both read the
    same moment in time instead of each independently re-reading the cache/local store - a
    concurrent background refresh or local decision update between two separate reads could
    otherwise desync the counts from what this call actually renders.
    """
    cache = cache if cache is not None else _load_cache(repo_path)
    rows = cache.get("decisions", [])
    if entry_type:
        rows = [r for r in rows if r.get("type", "") == entry_type]
    if query:
        pat = store.query_pattern(query)
        rows = [r for r in rows if store.matches_query(pat, r)]
    if not rows:
        return ""

    # Local decision token sets to dedup team rows against ([] on any load failure).
    if local_tokens is None:
        local_tokens = [(lid, store._tokenize(c)) for lid, c in _local_decisions(repo_path)]

    deferred_count = 0
    if defer_architecture and not entry_type:
        # A row that would collapse to the cheap one-line "ratifies local decision <id>"
        # pointer (>= 0.7 overlap) is already as light as the deferred-count line itself,
        # so deferring it would only throw away the more specific ratification signal for
        # no token savings — only defer architecture rows that would otherwise render in
        # full (i.e. don't already collapse).
        keep, deferred_rows = [], []
        for r in rows:
            if r.get("type") == "architecture":
                _, overlap = _best_local_overlap(r.get("content", ""), local_tokens)
                (deferred_rows if overlap < 0.7 else keep).append(r)
            else:
                keep.append(r)
        if deferred_rows:
            rows = keep
            deferred_count = len(deferred_rows)

    header = "## Team context (synced)"
    last_ok_at = cache.get("last_ok_at")
    if isinstance(last_ok_at, (int, float)):
        age = time.time() - last_ok_at
        if age >= _STALE_AFTER:
            header = f"## Team context (synced {_format_staleness(age)} - may be stale)"

    cap = limit if limit > 0 else _TEAM_DISPLAY
    lines = [header]
    rendered = rows[:cap]
    for r in rendered:
        content = r.get("content", "")
        rid = (r.get("id") or "")[:8]
        id_tag = f" (id={rid})" if rid else ""
        type_tag = f" [{r['type']}]" if r.get("type") else ""
        # Title-led rendering, same rule a local decision uses (store.title_and_body): the
        # cloud's own title when it sent one, else derived from content HERE at display time
        # (never stored back into the cache) - and the content line is skipped entirely when
        # it would merely repeat the title (collapsed-whitespace comparison).
        title, body = store.title_and_body({"title": r.get("title")}, content=content)
        lid, overlap = _best_local_overlap(content, local_tokens)
        if lid and overlap >= 0.7:
            # Same rule already stored locally - collapse to a ratification pointer instead of
            # re-injecting the full duplicate text (keeps the team-approval provenance signal).
            lines.append(f"- [scope=team] ratifies local decision {lid[:8]}{id_tag}")
        elif lid and overlap >= 0.5:
            # Heavy but partial overlap: keep the full row (may be a genuine divergence, e.g. a
            # different directive on the same topic) but tag the related local id for the reader.
            scope = r.get("scope", "team")
            lines.append(f"- [scope={scope}, overlaps local {lid[:8]}]{type_tag} {title}{id_tag}")
            if body is not None:
                lines.append(f"    {body}")
        else:
            scope = r.get("scope", "team")
            lines.append(f"- [scope={scope}]{type_tag} {title}{id_tag}")
            if body is not None:
                lines.append(f"    {body}")
    if deferred_count:
        lines.append(
            f"- {deferred_count} team architecture decision(s) synced but deferred. "
            'Call get_context(entry_type="architecture") for full content.'
        )
    if entry_type and len(rows) > cap:
        # Scoped to an explicit entry_type (a targeted JIT fetch, e.g. the exact
        # get_context(entry_type="architecture") call the deferred-count pointer above tells
        # the model to make) that STILL got truncated beyond the effective cap - surface that
        # rather than silently dropping the excess, same shape as get_context's own
        # filtered-display "showing N of M" note. Deliberately NOT raised for a plain/defer
        # bulk render (no entry_type): session_start_payload's own status suffix already
        # reports that truncation via "(M shown)", so this would only duplicate/conflict.
        lines.append(f"- showing {len(rendered)} of {len(rows)} team rows "
                     f"(pass a larger limit= to get_context for the rest)")
    result = "\n".join(lines)
    _record_render(repo_path, cache, rows=len(rendered), chars=len(result))
    return result


def session_team_section(repo_path: str, *, defer_architecture: bool = True) -> tuple[str, int, int]:
    """One-snapshot bundle for `store.session_start_payload`'s status suffix: the rendered
    team section text, the raw synced-row count, and the deferred-architecture count - all
    three derived from the SAME team-cache and local-decisions snapshot in a single call.

    Before this existed, the caller combined `format_team_section`, a raw cache-length count,
    and `count_deferred_architecture` as three independent calls, each reloading the team
    cache (and, for the last two, the local store) fresh. A background refresher landing
    between those reads - or a local decision changing between the count and the deferred-
    count read - could describe a moment different from what `context` actually rendered,
    and the resulting arithmetic could even go negative. Loading once here and threading the
    same `cache`/`local_tokens` into both `format_team_section` and
    `count_deferred_architecture` closes that window.

    Returns `("", 0, 0)` when there is no team cache (no decisions) - the empty text is the
    caller's existing signal to skip appending a team section at all."""
    cache = _load_cache(repo_path)
    count = len(cache.get("decisions", []))
    if count == 0:
        return "", 0, 0
    local_tokens = [(lid, store._tokenize(c)) for lid, c in _local_decisions(repo_path)]
    text = format_team_section(repo_path, defer_architecture=defer_architecture,
                               cache=cache, local_tokens=local_tokens)
    deferred = count_deferred_architecture(repo_path, cache=cache, local_tokens=local_tokens)
    return text, count, deferred


if __name__ == "__main__":  # pragma: no cover - the spawned refresher process entrypoint
    if len(sys.argv) > 1:
        _refresh_worker(sys.argv[1])
