"""Cross-store scope audit: find decisions that were saved into the WRONG repo's store.

The failure this exists for was observed in the wild. One session captured nine decisions
in an afternoon; five landed in the project it was actually working in and four landed in an
unrelated project's store, where they now read as that project's engineering decisions
forever. Retrieval is already hard-scoped per repo, so nothing "leaked" at read time — the
records are physically in the wrong file, written there by `store._resolve_repo` picking a
repo the session was not in.

The fingerprint is deterministic and needs no heuristics: **one session id appearing in more
than one repo store**. A session is bound to one project; decisions from it landing in two
stores means at least one of those stores is wrong. That is exactly how the original case was
found, and it is cheap to check — every entry already carries `session_id`/`session_ids`.

Read-only by design, and it stays that way. Moving an entry between stores rewrites where a
decision is recorded as having been made, and this module cannot tell WHICH store is the
wrong one — only that two disagree. The repo already ratified the rule for this class of
change in the anchor backfill (CLAUDE.md's informed-signature rule): a human sees the records
and signs off per item before anything is written. So this reports, names the files, and
stops. `contexer scope-audit` is the surface.

A leaf module: imports `store` for the store directory and slug helpers only, and nothing
imports it back.
"""

import json
from pathlib import Path

from contexer import store

# A session that legitimately spans repos is possible (a developer explicitly asking for a
# capture against another project passes repo_path, and `repo_source` will say "argument").
# Two stores is the smallest number that can disagree, so that is the bar; the report carries
# each entry's stamped provenance so a deliberate cross-repo write is distinguishable from a
# misrouted one at a glance.
_MIN_STORES = 2


_MAX_ENTRIES_SHOWN = 10       # per store, per session — every human surface here caps


def _text(value: object) -> str:
    """A string, whatever the store actually held. Entries are read RAW here — never through
    `_load` — so a field can be a JSON null, a number, or a list, and every one of those
    reaches a sort key or a slice further down."""
    return value if isinstance(value, str) else ""


def _is_memory_import(entry: dict) -> bool:
    """Whether this entry came from `memory_sync`, which makes its session id meaningless
    as a write-site signal.

    Two reasons it must not participate. The hard one: an unattributed memory fact is stored
    under the LITERAL constant `"memory-sync"` (`memory_sync.py`'s `fact["origin"] or
    "memory-sync"`), which is not a session at all — every repo that ever imported one
    carries the same id, so it flags every such pair of repos, forever. The principled one:
    even a fact WITH a real `originSessionId` records where the FACT came from, not which
    repo a writer was aiming at — the importer runs per repo, so a session id shared across
    two memory dirs is ordinary, not evidence of a misroute.

    Detected three ways because these entries are read raw, without `_load`'s migration: the
    provenance field, the `memory_key` every import carries, and the sentinel id itself — an
    entry predating any one of them is still caught by the others."""
    return (entry.get("created_by") == "memory"
            or bool(entry.get("memory_key"))
            or entry.get("session_id") == "memory-sync")


def _sessions_in(path: Path) -> tuple[str, dict[str, list[dict]]]:
    """(repo path this store claims, {session_id: [entry summaries]}). Fail-soft: an
    unreadable or malformed store contributes nothing rather than raising."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return "", {}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        return "", {}
    repo = data.get("repo_path") or ""
    by_session: dict[str, list[dict]] = {}
    for e in data["entries"]:
        if not isinstance(e, dict) or e.get("type") != "decision":
            continue
        # Both fields: `session_ids` accumulates every session that has TOUCHED the entry
        # (a recurrence from a second session is normal and not evidence of anything), so
        # only the ORIGINATING `session_id` — the session that actually wrote it here —
        # can indicate a misrouted write.
        sid = e.get("session_id") or ""
        if not sid or _is_memory_import(e):
            continue
        # Per-entry guard, not just around the parse: these entries are RAW json, never run
        # through _load's _migrate_entries, so revision-model helpers can meet shapes they
        # were never handed in a live store (`{"revisions": ["oops"]}` raises inside
        # _current_content). A malformed entry must cost its own title, not the whole audit —
        # `_run_guarded` would otherwise surface a traceback from a read-only report.
        try:
            title = e.get("title") or store._derive_title(store._current_content(e))
        except Exception:
            title = e.get("title") or ""
        # Coerced, not just defaulted: `.get(k, "")` still returns None for a key present
        # with a JSON null, and None then blows up the sort/max below and the slicing in
        # format_audit. These entries are raw, so any field can be any type.
        by_session.setdefault(sid, []).append({
            "id": _text(e.get("id")),
            "timestamp": _text(e.get("timestamp")),
            "title": _text(title),
            "repo_source": _text(e.get("repo_source")),
        })
    return repo, by_session


def audit_sessions() -> list[dict]:
    """Sessions whose decisions are split across two or more repo stores, newest first.

    Each row: {"session_id", "stores": [{"repo", "path", "entries": [...]}, ...]}. An empty
    list means no session wrote into more than one store — the clean state."""
    seen: dict[str, list[dict]] = {}
    # `store._store_files()`, never a local re-glob: its predicate already knows what else
    # shares STORE_DIR, and the `<slug>.deleted.json` tombstones are the trap — same
    # {"repo_path", "entries"} shape as a real store, so a hand-rolled "skip dotfiles and
    # _global" filter reads a repo's DELETED decisions as a second store for that same repo.
    for path in store._store_files():
        repo, by_session = _sessions_in(path)
        for sid, entries in by_session.items():
            entries.sort(key=lambda e: e.get("timestamp", ""))
            seen.setdefault(sid, []).append(
                {"repo": repo or path.stem, "path": str(path), "entries": entries})
    rows = [{"session_id": sid, "stores": stores}
            for sid, stores in seen.items() if len(stores) >= _MIN_STORES]
    rows.sort(key=lambda r: max((e.get("timestamp", "")
                                 for s in r["stores"] for e in s["entries"]), default=""),
              reverse=True)
    return rows


def format_audit(rows: list[dict]) -> str:
    """Human-facing report. `rows` comes straight from `audit_sessions`."""
    if not rows:
        return ("No cross-store sessions found — every session's decisions live in a single "
                "repo store.")
    # Reported as a question, not a verdict. The id is a WRITE-SESSION id, and for MCP
    # captures that is `server.SESSION_ID`, a uuid4 minted per server PROCESS — so a
    # developer who deliberately captured into a second repo by naming it, or a host that
    # reuses one server process across workspaces, produces this exact shape with nothing
    # wrong. `[via argument]` is the tell for the deliberate case.
    out = [f"{len(rows)} session(s) wrote decisions into more than one repo store.",
           "A session normally belongs to one project, so this usually means one of the "
           "stores below received a decision it should not have — but a deliberate "
           "cross-repo capture looks the same. Check each one.",
           ""]
    for row in rows:
        out.append(f"session {row['session_id'][:8]} — {len(row['stores'])} stores")
        for st in row["stores"]:
            out.append(f"  {st['repo']}")
            out.append(f"    {st['path']}")
            shown = st["entries"][:_MAX_ENTRIES_SHOWN]
            for e in shown:
                src = f" [via {e['repo_source']}]" if e.get("repo_source") else ""
                out.append(f"    - {e['id'][:8]} {e['timestamp'][:10]} {e['title'][:70]}{src}")
            hidden = len(st["entries"]) - len(shown)
            if hidden:
                out.append(f"    … showing {len(shown)} of {len(st['entries'])} "
                           f"({hidden} more)")
        out.append("")
    out.append("Nothing was changed. For each record that is in the wrong store: re-capture "
               "it in the right repo, then retire the misplaced one with "
               'approve_decision(entry_id="<id>", action="ignore") against THAT repo, or from '
               "the console (`contexer ui`). Note `contexer review` will not show them — it "
               "lists only decisions still pending approval in the current repo, and a "
               "misrouted decision is normally already approved and in another repo.")
    out.append("Entries with no `[via ...]` tag predate the provenance stamp or came from a "
               "host that does not stamp yet; new ones name the signal that chose their store.")
    return "\n".join(out)
