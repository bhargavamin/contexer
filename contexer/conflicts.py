"""Conflict rendering and resolution memos (issue #193): what a decision looks like when it
carries BOTH an approved revision and a later, unreviewed update, and how a developer's pick
between the two is recorded.

Extracted out of store.py (same directive that produced `anchors.py`: one module per cohesive
concern, store.py stays a thin call-site facade). store.py keeps only the seams - the four
render loops (`get_context`, `_render_prompt_decisions`, `_local_session_start_payload`,
`_rehydrate_working_set`) call `_conflict_view`/`_has_open_conflict` and append `_CONFLICT_GUIDE`,
`format_pending_review` calls `_conflict_pair_key` for its memo lines, and the two lifecycle
sites (`_promote_proposal`, `_apply_approval`'s dismiss branch) pop `conflict_memo` directly.
`record_conflict_memo` stays reachable as `store.record_conflict_memo` (server.py's
`resolve_conflict` tool calls it there) through store.py's existing lazy PEP 562 `__getattr__`.

Store-owned helpers (`_title_and_body`, `_current_revision`, `_current_content`,
`_normalize_content`, `_store_lock`, `_slug`, `_load`, `_save`, `_entry_by_id`) are read through
the `store` module OBJECT, not `from`-imported - the same load-order discipline `guard_engine.py`
documents at its own top: they're looked up at call time, so anything a test monkeypatches on
`contexer.store` is still seen here, and store.py never needs this module at import time.
"""

import hashlib
from datetime import datetime, timezone

from contexer import store          # module object, not `from`-imports: see docstring above


_CONFLICT_GUIDE = (
    "CONFLICT: a decision above has an approved version AND a later, unreviewed update. Do "
    "NOT settle which one holds by exploring the codebase - the code shows what was built, "
    "not which version the developer now intends; only they know. If it matters for the "
    "current task, ask them, then record their answer with "
    'resolve_conflict(entry_id="<the id shown on the decision>", choice="standing"|"update") '
    "- that records the pick, it approves nothing. A decision already carrying a picked/"
    "declined marker is settled for now: steer by what it renders, don't re-ask. Being ASKED "
    "which version is current is not resolving it - report both, naming the update as the "
    "latest stated direction and flagging it as unreviewed."
)


def _conflict_pair_key(entry: dict) -> str:
    """Identity of one (current revision, proposal) pair - what a resolution memo is bound
    to. `created_at` is part of the key on purpose: `verify_scan_conventions` pops a
    proposal WITHOUT advancing the revision, and a later TTL cycle can rebuild a
    byte-identical one - without the timestamp a dead memo would silently revive. Every
    rebuild path is dedup-guarded, so an identical retry keeps its `created_at` and the
    memo legitimately survives."""
    prop = entry.get("proposed_revision") or {}
    rev_id = (store._current_revision(entry) or {}).get("revision_id", "")
    raw = (f"{rev_id}\n{prop.get('content', '')}\n"
           f"{prop.get('title', '')}\n{prop.get('created_at', '')}")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def _has_open_conflict(entry: dict) -> bool:
    """Whether a proposal renders as a conversational conflict (issue #193). Excludes
    bookkeeping proposals - a scan-sourced convention withdrawal or an anchor retirement
    re-proposes on every 24h TTL cycle after a dismiss, and "only the developer knows" is
    factually backwards for them (the re-scan IS the evidence) - and title-only
    recaptures, whose content is unchanged. Those keep today's flag-only tag."""
    prop = entry.get("proposed_revision") or {}
    if not prop or prop.get("source") == "scan" or prop.get("clear_anchors"):
        return False
    return (store._normalize_content(prop.get("content", ""))
            != store._normalize_content(store._current_content(entry)))


def _conflict_view(entry: dict) -> tuple[str, str | None, list[str]]:
    """`_title_and_body` plus the labeled dual-injection lines for the model-facing render
    sites: a pending update is otherwise invisible until approved, so a fresh session
    answers from the stale standing decision.

    Returns (title, body, extra_lines); the extras are 4-space-indented continuations at
    every call site, never `- ` bullets (`_rendered_meta` counts bullets to report how many
    decisions were injected). Both interpolated bodies are whitespace-collapsed so a legacy
    non-normalized body can't fabricate bullets or break the indentation.

    A `conflict_memo` steers this asymmetrically: choice="update" makes the update
    operative but keeps the approved revision as one demoted line, because unreviewed
    content may be hidden by anyone while reviewed content may only be hidden by a review
    action. A memo whose pair key no longer matches is ignored (the dual render re-fires).

    Orphan memos are tolerated: the proposal-death sites other than `_promote_proposal` and
    `_apply_approval`'s dismiss branch leave `conflict_memo` behind, which is inert - a memo
    is only ever read while a proposal is present, and the `created_at` in the pair key stops
    a rebuilt proposal from reviving it."""
    standing_title, standing_body = store._title_and_body(entry)
    if not _has_open_conflict(entry):
        return standing_title, standing_body, []
    prop = entry["proposed_revision"]
    prop_date = (prop.get("created_at") or "")[:10]
    memo = entry.get("conflict_memo") or {}
    if memo.get("pair") == _conflict_pair_key(entry):
        memo_date = (memo.get("created_at") or "")[:10]
        if memo.get("choice") == "update":
            # Proposal-side title from the PROPOSAL, never _title_and_body(entry, content=...)
            # - that reads the standing title and would head the update's body with it.
            title, body = store._title_and_body({"title": prop.get("title")},
                                                content=prop["content"])
            standing = " ".join(store._current_content(entry).split())
            return title, body, [
                f"[this update was picked with the developer on {memo_date} - pending "
                "formal review; not developer-approved]",
                f'Still the approved version on record (superseded by the pick above): "{standing}"',
            ]
        if memo.get("choice") == "standing":
            return standing_title, standing_body, [
                f"[an update proposed {prop_date} was declined with the developer on "
                f"{memo_date} - the update stays pending formal review]"
            ]
    update = " ".join(prop.get("content", "").split())
    return standing_title, standing_body, [
        f'Unreviewed update ({prop_date}, NOT yet approved): "{update}"'
    ]


def memo_steer_line(entry: dict) -> str | None:
    """The one-line steer a still-valid resolution memo adds to a REVIEW surface - shared by
    `store.format_pending_review` and `contexer review`, which render the same sentence with
    their own indentation and leading capital. None when there is no memo, or it is bound to
    a pair that no longer exists (same staleness rule as `_conflict_view`)."""
    memo = entry.get("conflict_memo") or {}
    if not memo or memo.get("pair") != _conflict_pair_key(entry):
        return None
    date = (memo.get("created_at") or "")[:10]
    if memo.get("choice") == "update":
        return (f"the update was picked with the developer on {date}"
                " - approve to formalize (dismiss drops it)")
    return (f"the update was declined with the developer on {date}"
            " - dismiss to formalize (approve applies it instead)")


def record_conflict_memo(repo_path: str, entry_id: str, choice: str,
                         session_id: str = "") -> tuple[bool, str]:
    """Record which side of a rendered conflict (issue #193) the developer picked, so future
    sessions steer by it. Deliberately NOT an approval path: this writes none of `status` /
    `approved_by` / `source_files` / `anchor_commit`, so the commit-time guard can never arm
    off a memo. Errors are distinguishable so a model doesn't retry-loop on the same call.
    Returns (success, message)."""
    pick = choice.strip().lower()
    if pick not in ("standing", "update"):
        return False, f"Invalid choice {choice!r}. Use 'standing' or 'update'."
    # Renders always emit 8-char ids, and prefix resolution is first-match-wins - a shorter
    # prefix could silently record the pick against the wrong decision.
    entry_id = entry_id.strip()
    if len(entry_id) < 8:
        return False, ("entry_id must be at least 8 characters - use the id shown with the "
                       "decision, e.g. (id=6fb28fd9).")
    with store._store_lock(store._slug(repo_path)):
        data = store._load(repo_path)
        entry = store._entry_by_id(data.get("entries", []), entry_id)
        if entry is None:
            return False, f"Decision {entry_id!r} not found."
        if not entry.get("proposed_revision"):
            return False, "That decision has no pending update - there is no conflict to resolve."
        if not _has_open_conflict(entry):
            return False, ("That pending update is bookkeeping or title-only, not a conflict - "
                           "approve or dismiss it via approve_decision instead.")
        entry["conflict_memo"] = {
            "pair": _conflict_pair_key(entry),
            "choice": pick,
            "session_id": session_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        store._save(repo_path, data)
    return True, (
        f"Recorded for future sessions: steer by the {pick} version. This is NOT an approval - "
        "the update is still pending formal review, and a later explicit statement from the "
        "developer outranks this memo."
    )
