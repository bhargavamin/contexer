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

from contexer import config, store
from contexer.remote import RemoteDecision, RemoteStore, with_local_fallback
from contexer.repo_key import canonical_repo_key

# Max team rows rendered into a single get_context (mirrors the local filtered display).
_TEAM_DISPLAY = 25

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


def pull(repo_path: str, *, profile: config.Profile | None = None) -> tuple[int, int]:
    """Fetch team context for this repo and update the local team cache (incremental).

    Returns (upserted, removed). No-op `(0, 0)` when not in team mode, when the repo has no
    git origin (nothing to key on), or when the cloud is unreachable / rejects the token
    (degrades to local-only via with_local_fallback, keeping any existing cache untouched).
    """
    profile = profile or config.load_profile()
    remote = RemoteStore.from_profile(profile)
    if remote is None:
        return (0, 0)  # local mode / not configured
    key = canonical_repo_key(store._git(repo_path, "remote", "get-url", "origin"))
    if key is None:
        return (0, 0)  # no git remote — nothing to sync on

    cache = _load_cache(repo_path)
    ctx = with_local_fallback(
        lambda: remote.get_context(repo=key, updated_since=cache.get("cursor")),
        default=None,
        action="pull team context",
    )
    if ctx is None:
        return (0, 0)  # degraded — leave the existing cache in place

    by_id: dict[str, dict] = {d["id"]: d for d in cache.get("decisions", [])}
    upserted = 0
    for rd in ctx.decisions:
        if rd.scope != "team":
            continue  # local store already holds personal; cache team rows only
        by_id[rd.id] = _row_to_dict(rd)
        upserted += 1
    removed = 0
    for dead in ctx.deleted:
        if by_id.pop(dead, None) is not None:
            removed += 1

    _save_cache(repo_path, {
        "repo_key": key,
        "cursor": ctx.cursor or cache.get("cursor"),  # null cursor (empty pull) keeps prior
        "decisions": list(by_id.values()),
    })
    return (upserted, removed)


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
