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

from contexer import config, store
from contexer.remote import RemoteDecision, RemoteStore, with_local_fallback
from contexer.repo_key import canonical_repo_key

# Max team rows rendered into a single get_context (mirrors the local filtered display).
_TEAM_DISPLAY = 25

# Min seconds between mid-session delta polls (C7) — keeps UserPromptSubmit cheap.
_POLL_MIN_INTERVAL = 15

# Cap the in-cache sync log (last N batches). A consumer further behind than this misses the
# proactive "just approved" banner for old batches only — those rows are still in the cache
# (get_context / SessionStart render them), so this is a UX degradation, never data loss.
_SYNC_LOG_CAP = 50

# Fields persisted per cached team decision (the get_context wire projection).
_ROW_FIELDS = ("id", "type", "content", "rationale", "repo", "agent", "scope")


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


def _sync(repo_path: str, profile: config.Profile) -> tuple[list[dict], list[str]] | None:
    """Fetch team context incrementally and merge it into the cache.

    Returns (new_or_updated_rows, removed_ids) on success, or None (no-op) when not in team
    mode, when the repo has no git origin, or when the cloud is unreachable / rejects the
    token (degrades via with_local_fallback, leaving any existing cache untouched)."""
    remote = RemoteStore.from_profile(profile)
    if remote is None:
        return None  # local mode / not configured
    key = canonical_repo_key(store._git(repo_path, "remote", "get-url", "origin"))
    if key is None:
        return None  # no git remote — nothing to sync on

    cache = _load_cache(repo_path)
    ctx = with_local_fallback(
        lambda: remote.get_context(repo=key, updated_since=cache.get("cursor")),
        default=None,
        action="pull team context",
    )
    if ctx is None:
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

    saved = {
        **cache,  # preserve extra keys (last_poll_at, seq, sync_log) across a sync
        "repo_key": key,
        "cursor": ctx.cursor or cache.get("cursor"),  # null cursor (empty pull) keeps prior
        "decisions": list(by_id.values()),
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


def pull(repo_path: str, *, profile: config.Profile | None = None) -> tuple[int, int]:
    """Fetch team context and update the local cache (incremental). Returns (upserted,
    removed) counts; `(0, 0)` on any no-op (local mode / no origin / cloud unreachable)."""
    profile = profile or config.load_profile()
    result = _sync(repo_path, profile)
    if result is None:
        return (0, 0)
    new_rows, removed = result
    return (len(new_rows), len(removed))


def poll(repo_path: str, *, profile: config.Profile | None = None) -> list[dict]:
    """Delta-poll for injection (C7): return the team decisions newly synced this poll.

    Throttled to at most once per `_POLL_MIN_INTERVAL` so it never adds perceptible latency
    to a prompt. `[]` when throttled, not in team mode, or degraded — never raises."""
    profile = profile or config.load_profile()
    if profile.mode != "team" or not profile.endpoint:
        return []  # not configured — no work, no cache file for a local repo
    cache = _load_cache(repo_path)
    if time.time() - cache.get("last_poll_at", 0) < _POLL_MIN_INTERVAL:
        return []  # throttled — skip the network round-trip
    result = _sync(repo_path, profile)
    # Stamp the poll time regardless of outcome (don't hammer a down cloud every prompt).
    stamped = _load_cache(repo_path)
    stamped["last_poll_at"] = time.time()
    _save_cache(repo_path, stamped)
    return result[0] if result else []


def _seen_path(repo_path: str, consumer: str):
    return store.STORE_DIR / f".team_seen_{store._slug(repo_path)}_{consumer}.json"


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
    store._atomic_write(_seen_path(repo_path, consumer), json.dumps({"seq": seq}, ensure_ascii=False))


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
    sync log). Best-effort — a missing file (the normal case) is silently ignored."""
    try:
        (store.STORE_DIR / f".team_pending_{store._slug(repo_path)}.json").unlink()
    except OSError:
        pass


def _refresh_worker(repo_path: str) -> None:
    """Background refresher body (runs in a detached process spawned by poll_nonblocking):
    the network sync happens OFF the prompt path. `_sync` appends any newly synced rows to the
    cache's sync_log, from which each consumer's poll picks them up exactly once."""
    _sync(repo_path, config.load_profile())


def _spawn_refresh(repo_path: str) -> None:
    """Start a detached background refresher — the hook process never waits on it."""
    subprocess.Popen(  # noqa: S603 - fixed argv, our own interpreter/module
        [sys.executable, "-m", "contexer.team_context", repo_path],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def poll_nonblocking(repo_path: str, consumer: str = "claude", *,
                     profile: config.Profile | None = None) -> list[dict]:
    """C7 delta-poll with ZERO network on the prompt path, per consumer.

    Returns the team rows synced since THIS consumer last polled (read from the per-repo sync
    log via the consumer's own high-water marker, so a Claude and a Codex session on the same
    repo each receive every batch once) and — at most once per _POLL_MIN_INTERVAL — spawns a
    detached refresher whose results surface on the NEXT prompt. The prompt only ever pays a
    cache read plus a non-blocking process spawn, so a slow-but-healthy cloud (or the full
    transport timeout) can never stall the user. `consumer` defaults to "claude" so the
    original installed Claude hook string keeps working unchanged."""
    profile = profile or config.load_profile()
    if profile.mode != "team" or not profile.endpoint:
        return []  # not configured — no work, no cache file for a local repo
    _drop_legacy_pending(repo_path)  # one-time cleanup of a pre-sync-log parked file
    cache = _load_cache(repo_path)
    new = _collect_unseen(repo_path, cache, consumer)
    if time.time() - cache.get("last_poll_at", 0) >= _POLL_MIN_INTERVAL:
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


def refresh(repo_path: str) -> tuple[int, int]:
    """SessionStart pull for ANY adapter. Resolves the repo, refreshes the team cache,
    and NEVER raises — a sync hiccup (offline, bad token, anything) must not break session
    start. Returns (upserted, removed); (0, 0) on no-op / not-team / degraded / error."""
    try:
        repo = store._resolve_repo(repo_path)
        if not repo:
            return (0, 0)
        return pull(repo)
    except Exception:
        return (0, 0)


def poll_for_injection(repo_path: str, consumer: str = "claude") -> list[dict]:
    """Per-prompt delta poll for ANY adapter. Returns the team rows newly synced since THIS
    consumer last polled (empty when throttled / not team mode / degraded). Each consumer
    ("claude", "codex") keeps its own high-water marker, so concurrent sessions on one repo
    don't steal each other's proactive injection. NEVER raises. Adapters format these rows for
    their own host's hook output. `consumer` defaults to "claude" (the original hook string)."""
    try:
        repo = store._resolve_repo(repo_path)
        if not repo:
            return []
        return poll_nonblocking(repo, consumer)
    except Exception:
        return []


def format_team_section(repo_path: str, query: str = "", entry_type: str = "") -> str:
    """Render the cached team context as a '## Team context (synced)' markdown block, or ''.

    Filtered like `store.get_context` (by `entry_type` and a `query` substring). Each row is
    tagged `[scope=team]` so the reading agent treats it as provenance, not tool routing.
    """
    rows = _load_cache(repo_path).get("decisions", [])
    if entry_type:
        rows = [r for r in rows if r.get("type", "") == entry_type]
    if query:
        pat = store._query_pattern(query)
        rows = [r for r in rows if pat.search(r.get("content", ""))]
    if not rows:
        return ""

    lines = ["## Team context (synced)"]
    for r in rows[:_TEAM_DISPLAY]:
        scope = r.get("scope", "team")
        type_tag = f" [{r['type']}]" if r.get("type") else ""
        rid = (r.get("id") or "")[:8]
        id_tag = f" (id={rid})" if rid else ""
        lines.append(f"- [scope={scope}]{type_tag} {r.get('content', '')}{id_tag}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - the spawned refresher process entrypoint
    if len(sys.argv) > 1:
        _refresh_worker(sys.argv[1])
