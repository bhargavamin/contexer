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
import os
import subprocess
import sys
import time

from contexer import config, share, store
from contexer.remote import RemoteDecision, RemoteStore, with_local_fallback
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

# Fields persisted per cached team decision (the get_context wire projection).
_ROW_FIELDS = ("id", "type", "content", "rationale", "repo", "agent", "scope")

# How stale last_ok_at must be before format_team_section tags the header as possibly
# stale (the "quietly dead refresher" failure mode: rows keep rendering with no signal).
_STALE_AFTER = 24 * 3600


def _cache_path(repo_path: str):
    return store.STORE_DIR / f".team_{store._slug(repo_path)}.json"


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
    store._atomic_write(_cache_path(repo_path), json.dumps(data, indent=2, ensure_ascii=False))


def _row_to_dict(rd: RemoteDecision) -> dict:
    return {
        "id": rd.id,
        "type": rd.type,
        "content": rd.content,
        "rationale": rd.rationale,
        "repo": rd.repo,
        "agent": rd.agent,
        "scope": rd.scope,
    }


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
    key = canonical_repo_key(store._git(repo_path, "remote", "get-url", "origin"))
    if key is None:
        return None  # no git remote - nothing to sync on, no cache file

    cache = _load_cache(repo_path)
    start = time.time()
    ctx = with_local_fallback(
        lambda: remote.get_context(repo=key, updated_since=cache.get("cursor")),
        default=None,
        action="pull team context",
    )
    duration_ms = int((time.time() - start) * 1000)
    if ctx is None:
        # Degraded (cloud unreachable / auth rejected): record the attempt but leave the
        # decisions/cursor exactly as loaded - a transient outage must never wipe the cache.
        _save_cache(repo_path, {
            **cache,
            "last_sync": {"at": start, "ok": False, "duration_ms": duration_ms,
                         "error": "degraded",
                         "consecutive_failures": _consecutive_failures(cache) + 1},
        })
        return None  # degraded — leave the existing cache in place

    by_id: dict[str, dict] = {d["id"]: d for d in cache.get("decisions", [])}
    new_rows: list[dict] = []
    for rd in ctx.decisions:
        if rd.scope != "team":
            continue  # local store already holds personal; cache team rows only
        row = _row_to_dict(rd)
        if by_id.get(rd.id) == row:
            # Unchanged re-send: the live server's updatedSince filter is INCLUSIVE, so
            # rows stamped exactly at the cursor come back on every delta fetch. Treating
            # them as new would re-inject the same decisions every poll window.
            continue
        by_id[rd.id] = row
        new_rows.append(row)
    removed: list[str] = []
    for dead in ctx.deleted:
        if by_id.pop(dead, None) is not None:
            removed.append(dead)

    _save_cache(repo_path, {
        **cache,  # preserve extra keys (e.g. last_poll_at) across a sync
        "repo_key": key,
        "cursor": ctx.cursor or cache.get("cursor"),  # null cursor (empty pull) keeps prior
        "decisions": list(by_id.values()),
        "last_sync": {"at": start, "ok": True, "duration_ms": duration_ms,
                     "upserted": len(new_rows), "removed": len(removed),
                     "consecutive_failures": 0},
        "last_ok_at": start,
    })
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


def _pending_path(repo_path: str):
    return store.STORE_DIR / f".team_pending_{store._slug(repo_path)}.json"


def _claim_pending(repo_path: str) -> list[dict]:
    """Atomically take the rows the background refresher parked for injection.

    Claim = rename-then-read: the rename is atomic, so a refresher writing concurrently
    lands a fresh pending file for the NEXT prompt instead of being lost mid-consume."""
    path = _pending_path(repo_path)
    claim = path.with_name(path.name + ".claim")
    try:
        os.replace(path, claim)
    except OSError:
        return []  # nothing pending (or a concurrent prompt claimed it first)
    try:
        data = json.loads(claim.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        data = []
    finally:
        try:
            claim.unlink()
        except OSError:
            pass
    return data if isinstance(data, list) else []


def _refresh_worker(repo_path: str) -> None:
    """Background refresher body (runs in a detached process spawned by poll_nonblocking):
    the network sync happens OFF the prompt path. Newly synced team rows are parked in the
    pending file, merged by id with any rows not yet consumed by a prompt."""
    result = _sync(repo_path, config.load_profile())
    if not result or not result[0]:
        return
    path = _pending_path(repo_path)
    existing: list = []
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            existing = []
    if not isinstance(existing, list):
        existing = []
    by_id = {r.get("id"): r for r in existing}
    for row in result[0]:
        by_id[row["id"]] = row
    store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    store._atomic_write(path, json.dumps(list(by_id.values()), indent=2, ensure_ascii=False))


def _spawn_refresh(repo_path: str) -> None:
    """Start a detached background refresher — the hook process never waits on it."""
    subprocess.Popen(  # noqa: S603 - fixed argv, our own interpreter/module
        [sys.executable, "-m", "contexer.team_context", repo_path],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def poll_nonblocking(repo_path: str, *, profile: config.Profile | None = None) -> list[dict]:
    """C7 delta-poll with ZERO network on the prompt path.

    Returns the rows the background refresher synced since the last prompt (one prompt of
    staleness vs the synchronous poll()) and — at most once per `_poll_interval` (base
    _POLL_MIN_INTERVAL, backed off exponentially on consecutive sync failures up to
    _POLL_MAX_INTERVAL) — spawns a detached refresher whose results surface on the NEXT
    prompt. The prompt only ever pays a cache read plus a non-blocking process spawn, so a
    slow-but-healthy cloud (or the full transport timeout) can never stall the user, and a
    down cloud stops being re-spawned every 15s forever."""
    profile = profile or config.load_profile()
    if profile.mode != "team" or not profile.endpoint:
        return []  # not configured — no work, no cache file for a local repo
    new = _claim_pending(repo_path)
    cache = _load_cache(repo_path)
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
# (poll, poll_nonblocking, the CLI `pull` command) is either non-blocking or explicitly
# interactive, so they keep the longer default - this bound is deliberately narrow.
_SESSION_START_TIMEOUT = 3.0


def refresh(repo_path: str) -> tuple[int, int]:
    """SessionStart pull for ANY adapter. Resolves the repo, refreshes the team cache,
    and NEVER raises — a sync hiccup (offline, bad token, anything) must not break session
    start. Returns (upserted, removed); (0, 0) on no-op / not-team / degraded / error.

    Uses a short transport timeout (see `_SESSION_START_TIMEOUT`) so a slow-but-reachable
    cloud degrades to the existing cache (freshness, never correctness) rather than
    stalling session start for the full default timeout."""
    try:
        repo = store._resolve_repo(repo_path)
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


def poll_for_injection(repo_path: str) -> list[dict]:
    """Per-prompt delta poll for ANY adapter. Returns the team rows newly synced since the
    last prompt (empty when throttled / not team mode / degraded). NEVER raises. Adapters
    format these rows for their own host's hook output."""
    try:
        repo = store._resolve_repo(repo_path)
        if not repo:
            return []
        return poll_nonblocking(repo)
    except Exception:
        return []


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


def format_team_section(repo_path: str, query: str = "", entry_type: str = "") -> str:
    """Render the cached team context as a '## Team context (synced)' markdown block, or ''.

    Filtered like `store.get_context` (by `entry_type` and a `query` substring). Each row is
    tagged `[scope=team]` so the reading agent treats it as provenance, not tool routing.

    The header is tagged '(synced N hours/days ago - may be stale)' when the cache's
    `last_ok_at` is older than `_STALE_AFTER` — this is the signal for a quietly-dead
    refresher (rows keep rendering with no indication sync stopped working). A cache with
    no `last_ok_at` at all (old-format cache, pre-dating this field) is treated as unknown
    freshness and left untagged, to avoid false alarms right after an upgrade.

    On a non-empty result, also records `last_render` telemetry (rows rendered + char
    count) into the cache - see `_record_render` - so display-cap/deferral decisions can
    be made from real data instead of guessing.
    """
    cache = _load_cache(repo_path)
    rows = cache.get("decisions", [])
    if entry_type:
        rows = [r for r in rows if r.get("type", "") == entry_type]
    if query:
        pat = store._query_pattern(query)
        rows = [r for r in rows if pat.search(r.get("content", ""))]
    if not rows:
        return ""

    header = "## Team context (synced)"
    last_ok_at = cache.get("last_ok_at")
    if isinstance(last_ok_at, (int, float)):
        age = time.time() - last_ok_at
        if age >= _STALE_AFTER:
            header = f"## Team context (synced {_format_staleness(age)} - may be stale)"

    lines = [header]
    rendered = rows[:_TEAM_DISPLAY]
    for r in rendered:
        scope = r.get("scope", "team")
        type_tag = f" [{r['type']}]" if r.get("type") else ""
        rid = (r.get("id") or "")[:8]
        id_tag = f" (id={rid})" if rid else ""
        lines.append(f"- [scope={scope}]{type_tag} {r.get('content', '')}{id_tag}")
    result = "\n".join(lines)
    _record_render(repo_path, cache, rows=len(rendered), chars=len(result))
    return result


if __name__ == "__main__":  # pragma: no cover - the spawned refresher process entrypoint
    if len(sys.argv) > 1:
        _refresh_worker(sys.argv[1])
