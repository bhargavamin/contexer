"""Local-console read projections (issue: store.py modularization): every shape the
`contexer ui` console renders for one repo, one store, or the global rules.

Extracted out of store.py (same directive that produced `anchors.py` and `conflicts.py`: one
module per cohesive concern, store.py stays a thin call-site facade). The boundary was not
guessed: `contexer/ui/api.py` is the only production caller and it reaches for exactly the
names below and nothing else, and the rest of store.py called into this cluster exactly ONCE
(`file_mtime`, which is store-owned and stayed there). So this is a read/query layer for a
web UI that had no business living inside the capture store.

That one-caller property is load-bearing, so `overlap_report` did NOT come along even though it
sat inside the same line range in store.py: it has no console consumer at all (its only caller
is `contexer review`'s terminal output, via `cli._print_overlap_section`), it carries its own
thresholds, and it reads store internals rather than projecting an entry for display. Adjacency
in a file is not a boundary. Anything added here must have a console consumer, or the claim
above stops being checkable.

The console must never open a store file itself (the same one-write-path rule the MCP surface
follows), so every shape it renders is assembled here. Everything is a PURE READ except
`delete_global_rule`: no lock, no network, and no `load` side effects beyond the ones `load`
already has. When the shape the console needs does not exist yet, the fix is a new public read
HERE, not a file read in `ui/api.py`.

What deliberately did NOT move, and why: `load_diagnostics` is the third of a family with
`global_diagnostics` and `deleted_diagnostics` (both store-owned, and `ui/api.py` calls
`global_diagnostics` directly), so splitting one off would scatter the trio; `store_files` /
`_is_repo_store_file` enumerate which files in STORE_DIR are repo stores, which is store-level
knowledge `scope_audit.py` also depends on by documented contract; `file_mtime` is read by
`store.verify_scan_conventions` and `anchors.py` as well as here; and `overlap_report` is the
terminal-only read described above.

Store-owned helpers are read through the `store` module OBJECT, not `from`-imported, the same
load-order discipline `guard_engine.py` documents at its own top: they are looked up at call
time, so anything a test monkeypatches on `contexer.store` (`STORE_DIR`, by the `tmp_repo`
fixture in conftest.py; `GLOBAL_SLUG`, by tests/test_ui_cli.py) is still seen here, and
store.py never needs this module at
import time. The public entrypoints stay reachable as `store.<name>` through store.py's lazy
PEP 562 `__getattr__`, so no existing caller had to change.
"""

import json
import os
import time
from pathlib import Path

from contexer import review_impact  # the shared review block; reads store, never console_api
from contexer import revisions      # pure stdlib leaf (no cycle): revision lifecycle
from contexer import store          # module object, not `from`-imports: see docstring above


_CONSOLE_RECENT = 10          # rows in the dashboard's RECENT timeline

# Reported for a store file that names no usable repo path — either it does not parse (so no
# path could be read out of it) or the path it claims is one `is_sane_repo` rejects. The
# console renders it as "store unreadable", never as "no decisions".
_NO_REPO_PATH = "store file names no usable repo_path"


def _read_store(repo_path: str) -> tuple[dict, str | None, float | None]:
    """(store data, parse error, mtime) from ONE read of this repo's store file.

    The console's poll path wants all three, and `load_diagnostics` + `load` + `file_mtime`
    parsed the same file TWICE and stat'd it again — every 10 seconds, over a store that is
    routinely a few hundred KB. Data degrades to an empty store exactly like `load` (revision
    migration included, so the console projections still see the normalized shape) and `error`
    is what keeps "unreadable" distinct from "empty". A missing file is a genuinely empty store,
    so it reports no error."""
    path = store._store_path(repo_path)
    empty = {"repo_path": repo_path, "entries": []}
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return empty, None, None
    except (OSError, UnicodeDecodeError) as exc:
        return empty, f"{type(exc).__name__}: {exc}", store.file_mtime(path)
    mtime = store.file_mtime(path)
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        return empty, f"{type(exc).__name__}: {exc}", mtime
    problem = store._entries_error(data.get("entries") if isinstance(data, dict) else None)
    if problem:
        return empty, f"not a store object ({problem})", mtime
    store._migrate_entries(data)
    return data, None, mtime


def _inspect_store_file(path: Path) -> tuple[str, dict | None, str | None]:
    """(repo_path, parsed store or None, error or None) for one store file.

    Deliberately NOT `load`, which degrades a corrupt store to an empty one - the console
    has to tell "unreadable" from "empty". `repo_path` is resolved even when `entries` is
    malformed, so such a store still reports its own error under its own name. It is NOT
    recoverable when the JSON itself will not parse: the repo path lives inside the file and
    the slug is a hash of it, so an unparseable file resolves with `repo_path` "" and
    addressing it is `_resolve_store`'s job, not this function's. A `repo_path` the file claims
    but `is_sane_repo` rejects reads as absent: a poisoned store file must not redirect a
    console read."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as exc:
        return "", None, f"{type(exc).__name__}: {exc}"
    if not isinstance(raw, dict):
        return "", None, "not a store object (no 'entries' list)"
    claimed = str(raw.get("repo_path") or "")
    repo_path = claimed if store.is_sane_repo(claimed) else ""
    problem = store._entries_error(raw.get("entries"))
    if problem:
        return repo_path, None, f"not a store object ({problem})"
    return repo_path, raw, None


def _repo_name(repo_path: str) -> str:
    return os.path.basename(repo_path.rstrip(os.path.sep))


def _console_factors(entry: dict) -> list[str]:
    rev = revisions.current_revision(entry) or {}
    return list(rev.get("evidence") or entry.get("confidence_factors") or [])


def _console_summary(entry: dict) -> dict:
    """The console's shared per-decision row shape. Internal fields (revision ids, session
    ids, the raw revisions list) stay server-side; a caller that needs them asks for the
    detail projection instead.

    `source_files` is the anchored-files list verbatim (Task 4 of #174) — schema-additive,
    so every existing caller (dashboard, list, detail, tombstones) picks it up for free. No
    staleness flag here on purpose: `_staleness_note` costs a `git diff` subprocess per
    entry, and this projection backs the console's 10-second poll of `list_decisions` /
    `dashboard_summary` — adding a git call per row there would multiply into a poll-time
    subprocess storm. Staleness stays confined to its two existing render sites
    (`get_context`, `_render_prompt_decisions`), both budget-capped and neither on a UI poll."""
    content = revisions.current_content(entry)
    rev = revisions.current_revision(entry) or {}
    return {
        "id": entry.get("id", ""),
        "title": entry.get("title") or revisions.derive_title(content),
        "content": content,
        "subtype": entry.get("subtype", ""),
        "status": store.entry_status(entry),
        "created_by": entry.get("created_by", "ai"),
        "timestamp": entry.get("timestamp"),
        "updated_at": entry.get("updated_at") or entry.get("timestamp"),
        "revision": rev.get("version_number", entry.get("revision", 1)),
        "occurrence_count": entry.get("occurrence_count", 1),
        "confidence": rev.get("confidence_score", entry.get("confidence", 0)),
        "has_proposal": bool(entry.get("proposed_revision")),
        "source_files": list(entry.get("source_files") or []),
    }


def _console_proposed(prop: dict) -> dict:
    """A pending `proposed_revision` as the console renders the "after" side of a diff."""
    return {
        "content": prop.get("content", ""),
        "title": prop.get("title", ""),
        "subtype": prop.get("subtype", ""),
        "source": prop.get("source", ""),
        "created_at": prop.get("created_at"),
        "confidence": prop.get("confidence"),
        "confidence_factors": list(prop.get("confidence_factors") or []),
    }


def _console_proposal(entry: dict) -> dict:
    """A decision carrying a Suggested Update, as a before/after review card."""
    rev = revisions.current_revision(entry) or {}
    version = rev.get("version_number", entry.get("revision", 1))
    return {
        "id": entry.get("id", ""),
        "title": entry.get("title") or revisions.derive_title(revisions.current_content(entry)),
        "subtype": entry.get("subtype", ""),
        "status": store.entry_status(entry),
        "revision": version,
        "current": {"content": revisions.current_content(entry), "title": rev.get("title", ""),
                    "version_number": version},
        "proposed": _console_proposed(entry.get("proposed_revision") or {}),
    }


def _console_share_state(decision_id: str) -> dict:
    """Whether one decision has already been pushed, and to where.

    Cosmetic, exactly like the `contexer share` picker's "✓ shared" hint it reads from, so
    any failure reads as "not shared" rather than surfacing an error."""
    try:
        from contexer import config, share
        endpoint = config.load_profile().endpoint
        shared_at = share.shared_map(endpoint).get(decision_id)
        queued = any(e.get("decision_id") == decision_id for e in share._load_outbox())
        return {"shared": shared_at is not None, "shared_at": shared_at,
                "endpoint": endpoint, "queued": queued}
    except Exception:
        return {"shared": False, "shared_at": None, "endpoint": None, "queued": False}


def list_stores() -> list[dict]:
    """One row per repo store in STORE_DIR, for the console's repo switcher.

    Deliberately cheap — counts only, no global read and no team-cache read: the console
    polls this every 10 seconds, and each of those would add a file read per tick.
    `pending` is "awaiting the developer" in the same sense as `get_pending_decisions`
    (a pending_approval decision OR a live one carrying a Suggested Update), counted from
    the store read already done here rather than a second load. `ok: false` marks a file
    that could not be parsed — a caller must render that as "unreadable", never "empty"."""
    current = store.current_repo_path()
    rows = []
    for path in store.store_files():
        repo_path, data, error = _inspect_store_file(path)
        decisions = [e for e in (data or {}).get("entries", []) if e.get("type") == "decision"]
        rows.append({
            "slug": path.stem,
            "repo_path": repo_path,
            "name": _repo_name(repo_path) or path.stem,
            "decisions": len(decisions),
            "pending": sum(1 for e in decisions if store.entry_status(e) == "pending_approval"
                           or e.get("proposed_revision")),
            "tombstoned": len(store._load_deleted(repo_path)["entries"]) if repo_path else 0,
            "mtime": store.file_mtime(path),
            "is_current": bool(repo_path) and repo_path == current,
            "ok": error is None,
            "error": error,
        })
    return rows


def _resolve_store(slug: str) -> tuple[Path, str, str | None] | None:
    """(file, repo_path, parse error) for the store a console slug names, or None for a slug
    that names no store file at all.

    THE security boundary for the console: a repo path is never accepted from a request, so no
    crafted URL can make the daemon read or write an arbitrary filesystem location.

    Three spellings resolve to the same store - the file's own name, `repo_slug(repo_path)`, and
    `_legacy_slug(repo_path)`. The last one is what keeps a slug STABLE across the pre-hash
    rename: `_store_path` renames `someorg_somerepo.json` to `someorg_somerepo-8539fba8.json`
    on the first `load`, so without it a client's slug stopped resolving the moment anything
    opened that store. Exact spellings are matched before the legacy one, which is not
    injective (`/a/my.repo` and `/a/my_repo` share it) and must never shadow a canonical
    address. The file-name hit short-circuits the directory scan: it is the common case and
    costs one read instead of one per store in STORE_DIR.

    An unparseable file resolves with `repo_path` "" and a non-None error — "known slug,
    unreadable", which a caller must keep distinct from None ("unknown slug")."""
    if not slug or "/" in slug or "\\" in slug or "\0" in slug:
        return None
    direct = store.STORE_DIR / f"{slug}.json"
    if store._is_repo_store_file(direct) and direct.exists():
        repo_path, _data, error = _inspect_store_file(direct)
        return direct, repo_path, error
    legacy_hit = None
    for path in store.store_files():
        repo_path, _data, error = _inspect_store_file(path)
        if not repo_path:
            continue
        if slug == store.repo_slug(repo_path):
            return path, repo_path, error
        if legacy_hit is None and slug == store._legacy_slug(repo_path):
            legacy_hit = (path, repo_path, error)
    return legacy_hit


def resolve_store_slug(slug: str) -> str | None:
    """The repo path a console slug names, or None when the slug names no store OR names one
    whose repo path cannot be recovered. `resolve_store` is the richer answer that tells those
    two Nones apart."""
    resolved = _resolve_store(slug)
    return (resolved[1] or None) if resolved is not None else None


def resolve_store(slug: str) -> dict | None:
    """What a console slug names: {"slug", "repo_path", "ok", "error"} — or None when it names
    no store file in STORE_DIR.

    `repo_path` is "" when it could not be recovered: the file will not parse (the path lives
    inside it and the slug is a hash), or the path it claims is one `is_sane_repo` rejects.
    That is still a KNOWN slug, so it resolves rather than 404ing, and `store_summary` is the
    ready-made degraded payload for it. `ok` is False whenever the file did not parse cleanly,
    including the case where `repo_path` IS usable (a store object with a malformed `entries`)
    — there the repo-path reads still work and report the error themselves."""
    resolved = _resolve_store(slug)
    if resolved is None:
        return None
    _path, repo_path, error = resolved
    return {
        "slug": slug,
        "repo_path": repo_path,
        "ok": bool(repo_path) and error is None,
        "error": error or (None if repo_path else _NO_REPO_PATH),
    }


def store_summary(slug: str) -> dict | None:
    """`dashboard_summary` addressed BY SLUG, with a degraded payload for a store whose file
    cannot be read. None ONLY when the slug names no store file.

    The console deep-links by slug, and an unreadable store has no repo path to hand
    `dashboard_summary` — but it is still addressable, so it must render as "store unreadable",
    never as "no decisions" and never as a 404. The degraded payload therefore carries the SAME
    key set with zeroed repo counts and `ok: false`, so a caller branches on `ok` alone."""
    resolved = _resolve_store(slug)
    if resolved is None:
        return None
    path, repo_path, error = resolved
    if repo_path:
        return {"slug": slug, **dashboard_summary(repo_path)}
    message = error or _NO_REPO_PATH
    return {
        "slug": slug,
        "repo_path": "",
        "name": slug,
        "is_current": False,
        "ok": False,
        "error": message,
        "mtime": store.file_mtime(path),
        "counts": {"decisions": 0, "pending": 0, "proposed_updates": 0,
                   "global": len(store.get_global_decisions()), "team": 0, "tombstoned": 0},
        "subtype_mix": [],
        "status_mix": [],
        "recent": [],
        "pending": [],
        "proposals": [],
        # Unknown rather than empty: the sidecar is named after the repo path this file was
        # supposed to carry, so with no repo path there is nothing to read it from.
        "tombstones": {"ok": False, "error": message, "count": 0},
        "staleness": {"last_ok_at": None, "age_seconds": None, "stale": False},
        "health": {"ok": False, "error": message},
    }


def dashboard_summary(repo_path: str) -> dict:
    """Everything the console's dashboard and review views render for one repo.

    `counts.pending` is pending_approval decisions ONLY and `counts.proposed_updates` the
    ones carrying a Suggested Update, so a caller can add them for a "needs you" total
    without double-counting. `ok`/`error` describe the LIVE store; `tombstones.ok`/`.error`
    describe the sidecar separately, because a corrupt sidecar must not read as "nothing
    deleted". One read of each file — the console polls this every 10 seconds."""
    data, error, mtime = _read_store(repo_path)
    health = {"ok": error is None, "error": error}
    decisions = [e for e in data.get("entries", []) if e.get("type") == "decision"]
    pending = [e for e in decisions if store.entry_status(e) == "pending_approval"]
    proposals = [e for e in decisions if e.get("proposed_revision")]
    team = team_snapshot(repo_path)
    graveyard, tomb_error = store.read_deleted(repo_path)
    tombstoned = graveyard.get("entries", [])

    by_subtype: dict[str, int] = {}
    by_status: dict[str, int] = {}
    for entry in decisions:
        by_subtype[entry.get("subtype") or ""] = by_subtype.get(entry.get("subtype") or "", 0) + 1
        status = store.entry_status(entry)
        by_status[status] = by_status.get(status, 0) + 1
    recent = sorted(decisions, key=lambda e: e.get("updated_at") or e.get("timestamp") or "",
                    reverse=True)[:_CONSOLE_RECENT]
    # The review-impact block, on the SAME categories `review_pending` and `contexer review`
    # render (Task 07). Built once per call and threaded in - the console polls this every 10
    # seconds, so a per-row rebuild would mean one spool listing per pending decision per tick.
    # The store read it would otherwise do is handed the decisions this function already has.
    impact_context = review_impact.review_context(repo_path, decisions)

    return {
        "repo_path": repo_path,
        "name": _repo_name(repo_path),
        "is_current": repo_path == store.current_repo_path(),
        "ok": health["ok"],
        "error": health["error"],
        "mtime": mtime,
        "counts": {
            "decisions": len(decisions),
            "pending": len(pending),
            "proposed_updates": len(proposals),
            "global": len(store.get_global_decisions()),
            "team": len(team["decisions"]),
            "tombstoned": len(tombstoned),
        },
        "tombstones": {"ok": tomb_error is None, "error": tomb_error,
                       "count": len(tombstoned)},
        "subtype_mix": [{"subtype": k, "count": v}
                        for k, v in sorted(by_subtype.items(), key=lambda kv: (-kv[1], kv[0]))],
        "status_mix": [{"status": k, "count": v}
                       for k, v in sorted(by_status.items(), key=lambda kv: (-kv[1], kv[0]))],
        "recent": [_console_summary(e) for e in recent],
        "pending": [{**_console_summary(e), "confidence_factors": _console_factors(e),
                     "impact": review_impact.review_impact(repo_path, e, impact_context)}
                    for e in pending],
        "proposals": [{**_console_proposal(e),
                       "impact": review_impact.review_impact(repo_path, e, impact_context)}
                      for e in proposals],
        "staleness": team["staleness"],
        "health": health,
    }


def list_decisions(repo_path: str, *, query: str = "", subtype: str = "", status: str = "",
                   files: list[str] | None = None, limit: int = 0, offset: int = 0) -> dict:
    """A filtered, paged page of decisions, newest change first.

    `total` is the count BEFORE paging so a caller can render "N matching". `limit <= 0`
    means no cap. Carries the same `ok`/`error` pair as the dashboard: a corrupt store
    returns an empty page with `ok: false`, never a silently empty list. One read of the
    store file — this is on the console's 10-second poll.

    `files`: same read-surface semantics as `get_context(files=...)` — decisions that GOVERN
    the given files (`guard_engine.decisions_for_files`: source_files anchor or a path-like
    content artifact), no trust filter, `ignored` excluded. Scoped to THIS store only (the
    console's file filter lives on the per-repo decisions list, not the global one), so the
    already-loaded `rows` are passed as an override rather than letting the engine reload the
    store AND pull in the global store's entries too. A file that matches nothing yields an
    empty list, never an error — same as a query with no hits."""
    data, error, _mtime = _read_store(repo_path)
    health = {"ok": error is None, "error": error}
    rows = [e for e in data.get("entries", []) if e.get("type") == "decision"]
    if files:
        # Local import — see get_context's identical comment: a module-level `from contexer
        # import guard_engine` here would recreate the store <-> guard_engine load-order cycle
        # guard_engine.py's own docstring describes.
        from contexer import guard_engine
        hit_ids = {h["decision_id"]
                  for h in guard_engine.decisions_for_files(repo_path, files, decisions=rows)}
        rows = [e for e in rows if e.get("id") in hit_ids]
    if subtype:
        rows = [e for e in rows if e.get("subtype") == subtype]
    if status:
        rows = [e for e in rows if store.entry_status(e) == status]
    if query:
        pat = store.query_pattern(query)
        rows = [e for e in rows if store.matches_query(pat, e)]
    rows.sort(key=lambda e: e.get("updated_at") or e.get("timestamp") or "", reverse=True)
    start = max(offset, 0)
    window = rows[start:] if limit <= 0 else rows[start:start + limit]
    return {
        "total": len(rows),
        "limit": limit,
        "offset": start,
        "ok": health["ok"],
        "error": health["error"],
        "decisions": [_console_summary(e) for e in window],
    }


def get_decision_detail(repo_path: str, entry_id: str) -> dict | None:
    """One decision in full — revision timeline, confidence evidence, share state — or None.

    `entry_id` accepts a full UUID or the 8-char prefix, like every other id-taking store
    function. `confidence` widens from the summary's bare score to {score, factors} here.
    `rationale` is not a local store field today (the share wire hardcodes None); it is
    projected as whatever the entry carries, so a row imported with one still shows it."""
    entries = [e for e in store.load(repo_path).get("entries", []) if e.get("type") == "decision"]
    entry = store.entry_by_id(entries, entry_id)
    if entry is None:
        return None
    rev = revisions.current_revision(entry) or {}
    current_revision_id = rev.get("revision_id")
    proposal = entry.get("proposed_revision")
    return {
        **_console_summary(entry),
        "session_count": len(store._session_set(entry)),
        "memory_key": entry.get("memory_key"),
        "approved_at": entry.get("approved_at"),
        "approved_by": entry.get("approved_by"),
        "rationale": entry.get("rationale"),
        "confidence": {"score": rev.get("confidence_score", entry.get("confidence", 0)),
                       "factors": _console_factors(entry)},
        "revisions": [{
            "version_number": r.get("version_number"),
            "content": r.get("content", ""),
            "title": r.get("title", ""),
            "source": r.get("source", ""),
            "created_at": r.get("created_at"),
            "approved_at": r.get("approved_at"),
            "confidence_score": r.get("confidence_score"),
            "is_current": r.get("revision_id") == current_revision_id,
        } for r in entry.get("revisions") or []],
        "proposed_revision": _console_proposed(proposal) if proposal else None,
        "share": _console_share_state(entry.get("id", "")),
    }


def list_tombstones(repo_path: str) -> dict:
    """The console's Deleted view: {"ok", "error", "tombstones"} — tombstoned decisions
    projected newest deletion first.

    Carries the same `ok`/`error` pair as `dashboard_summary` and `list_decisions`, for exactly
    the reason those do: every other read of the sidecar degrades a corrupt file to an empty
    list, so without this the view renders "nothing deleted" over a file that actually still
    holds tombstones it could not parse. One read."""
    data, error = store.read_deleted(repo_path)
    rows = [{**_console_summary(e), "deleted_at": e.get("deleted_at"),
             "deleted_by": e.get("deleted_by", "ui")}
            for e in data.get("entries", [])]
    rows.sort(key=lambda r: r["deleted_at"] or "", reverse=True)
    return {"ok": error is None, "error": error, "tombstones": rows}


def list_global_rules() -> dict:
    """The console's Global view: {"ok", "error", "rules"} — global rules projected for
    display. Global entries are born approved and carry no proposals, so the row is narrower
    than a repo decision's.

    Carries the same `ok`/`error` pair as `list_tombstones`, and for a sharper version of the
    same reason: the session-facing read degrades an unparseable `_global.json` to no rules, so
    without this the view renders "No global rules" over a file that still holds them — next to
    an Add button whose write path is the one thing that would replace them. One read."""
    data, error = store._read_global()
    rows = []
    for entry in data["entries"]:
        if entry.get("type") != "decision":
            continue
        summary = _console_summary(entry)
        rows.append({k: summary[k] for k in (
            "id", "title", "content", "subtype", "created_by", "timestamp", "updated_at",
            "revision", "confidence")})
    return {"ok": error is None, "error": error, "rules": rows}


def delete_global_rule(entry_id: str) -> tuple[bool, str]:
    """Remove a global rule outright. Returns (ok, message).

    No tombstone, unlike `delete_decision`: nothing writes INTO the global store from a
    repo scan, `CLAUDE.md`, or the miner, so there is no resurrection path for a sidecar
    to guard against — it would only add a file that never gets consulted.

    Refuses on an unreadable file for the same reason `delete_decision` does: the degraded read
    is an empty store, so saving it back would discard every rule the file still holds."""
    with store.store_lock(store.GLOBAL_SLUG):
        data, error = store._read_global()
        if error is not None:
            return False, (f"Cannot delete {entry_id!r}: {store._global_path().name} is unreadable "
                           f"({error}), and overwriting it would discard every global rule "
                           "already in it. Move that file aside, then retry.")
        entry = store.entry_by_id(data["entries"], entry_id)
        if entry is None:
            return False, f"Global rule {entry_id!r} not found."
        data["entries"] = [e for e in data["entries"] if e is not entry]
        store.save_global(data)
        return True, f"Deleted global rule {entry['id'][:8]}."


def team_snapshot(repo_path: str) -> dict:
    """The cached team context for one repo: rows, last-sync outcome, and staleness.

    Pure cache read — never the network, so the console's poll costs one file read; a
    caller that wants fresh rows calls `team_context.refresh`. Function-level import for
    the same reason as `_team_section` (team_context imports store, so a module-level
    import here would cycle). Fail-soft on config: a malformed config.toml must cost the
    console its mode line, not the whole view."""
    from contexer import config, team_context
    try:
        profile = config.load_profile()
        mode, endpoint = profile.mode, profile.endpoint
    except Exception:
        mode, endpoint = "local", None
    cache = team_context._load_cache(repo_path) if repo_path else team_context._empty_cache()
    rows = [{k: r.get(k) for k in team_context._ROW_FIELDS} for r in cache.get("decisions", [])]
    last_ok_at = cache.get("last_ok_at")
    age = time.time() - last_ok_at if isinstance(last_ok_at, (int, float)) else None
    last_sync = cache.get("last_sync") if isinstance(cache.get("last_sync"), dict) else {}
    return {
        "repo_key": cache.get("repo_key"),
        "mode": mode,
        "enabled": mode == "team" and bool(endpoint),
        "counts": {"decisions": len(rows)},
        "staleness": {"last_ok_at": last_ok_at, "age_seconds": age,
                      "stale": age is not None and age >= team_context._STALE_AFTER},
        "last_sync": {"at": last_sync.get("at"), "ok": last_sync.get("ok"),
                      "duration_ms": last_sync.get("duration_ms"),
                      "consecutive_failures": last_sync.get("consecutive_failures", 0),
                      "upserted": last_sync.get("upserted"),
                      "removed": last_sync.get("removed"),
                      "error": last_sync.get("error")},
        "decisions": rows,
    }

