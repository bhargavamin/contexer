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
import time

from contexer import config, store
from contexer.remote import RemoteDecision, RemoteStore, with_local_fallback
from contexer.repo_key import canonical_repo_key

# Max team rows rendered into a single get_context (mirrors the local filtered display).
_TEAM_DISPLAY = 25

# Min seconds between mid-session delta polls (C7) — keeps UserPromptSubmit cheap.
_POLL_MIN_INTERVAL = 15

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
    })
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
