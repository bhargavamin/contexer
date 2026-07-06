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
    staleness vs the synchronous poll()) and — at most once per _POLL_MIN_INTERVAL — spawns
    a detached refresher whose results surface on the NEXT prompt. The prompt only ever pays
    a cache read plus a non-blocking process spawn, so a slow-but-healthy cloud (or the full
    transport timeout) can never stall the user."""
    profile = profile or config.load_profile()
    if profile.mode != "team" or not profile.endpoint:
        return []  # not configured — no work, no cache file for a local repo
    new = _claim_pending(repo_path)
    cache = _load_cache(repo_path)
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


def _local_decisions(repo_path: str) -> list[tuple[str, str]]:
    """(id, current-content) for each live local decision - the base team rows dedup against.

    Only `type == "decision"` entries that are not `ignored`, read through the same current-
    revision accessor replay uses. Fail-soft: any load error returns [] so team-context dedup
    degrades to unmodified rendering (this runs inside session-start hooks and must not raise)."""
    try:
        out: list[tuple[str, str]] = []
        for e in store._load(repo_path).get("entries", []):
            if e.get("type") != "decision" or e.get("status") == "ignored":
                continue
            content = store._current_content(e)
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


def format_team_section(repo_path: str, query: str = "", entry_type: str = "") -> str:
    """Render the cached team context as a '## Team context (synced)' markdown block, or ''.

    Filtered like `store.get_context` (by `entry_type` and a `query` substring). Each row is
    tagged `[scope=team]` so the reading agent treats it as provenance, not tool routing.

    Rows that duplicate a LOCAL decision are deduped so the same rule is not injected twice
    (once local, once team). Token overlap against the local store decides - reusing the very
    metric the novelty filter thresholds at 0.7: >= 0.7 collapses to a one-line "ratifies local
    decision …" pointer (the team's ratification is kept as provenance, not the duplicate text);
    0.5-0.7 renders in full but tagged with the local id so a genuine divergence stays visible;
    below 0.5 renders exactly as before. The _TEAM_DISPLAY cap applies AFTER collapsing (a
    collapsed one-liner still counts as a row). Fail-soft: if the local store can't be read,
    `_local_decisions` yields [] and every row renders unmodified - this never raises."""
    rows = _load_cache(repo_path).get("decisions", [])
    if entry_type:
        rows = [r for r in rows if r.get("type", "") == entry_type]
    if query:
        pat = store._query_pattern(query)
        rows = [r for r in rows if pat.search(r.get("content", ""))]
    if not rows:
        return ""

    # Local decision token sets to dedup team rows against ([] on any load failure).
    local_tokens = [(lid, store._tokenize(c)) for lid, c in _local_decisions(repo_path)]

    lines = ["## Team context (synced)"]
    for r in rows[:_TEAM_DISPLAY]:
        content = r.get("content", "")
        rid = (r.get("id") or "")[:8]
        id_tag = f" (id={rid})" if rid else ""
        type_tag = f" [{r['type']}]" if r.get("type") else ""
        lid, overlap = _best_local_overlap(content, local_tokens)
        if lid and overlap >= 0.7:
            # Same rule already stored locally - collapse to a ratification pointer instead of
            # re-injecting the full duplicate text (keeps the team-approval provenance signal).
            lines.append(f"- [scope=team] ratifies local decision {lid[:8]}{id_tag}")
        elif lid and overlap >= 0.5:
            # Heavy but partial overlap: keep the full row (may be a genuine divergence, e.g. a
            # different directive on the same topic) but tag the related local id for the reader.
            scope = r.get("scope", "team")
            lines.append(f"- [scope={scope}, overlaps local {lid[:8]}]{type_tag} {content}{id_tag}")
        else:
            scope = r.get("scope", "team")
            lines.append(f"- [scope={scope}]{type_tag} {content}{id_tag}")
    return "\n".join(lines)


if __name__ == "__main__":  # pragma: no cover - the spawned refresher process entrypoint
    if len(sys.argv) > 1:
        _refresh_worker(sys.argv[1])
