"""Conflict rendering and resolution memos (issue #193): what a decision looks like when it
carries BOTH an approved revision and a later, unreviewed update, and how a developer's pick
between the two is recorded.

Extracted out of store.py (same directive that produced `anchors.py`: one module per cohesive
concern, store.py stays a thin call-site facade). store.py keeps only the seams — the four
render loops (`get_context`, `_render_prompt_decisions_with_records`, `_local_session_start_payload`,
`_rehydrate_working_set`) call `_conflict_view`/`has_open_conflict` and append `_CONFLICT_GUIDE`,
`format_pending_review` calls `_conflict_pair_key` for its memo lines, and the two lifecycle
sites (`_promote_proposal`, `_apply_approval`'s dismiss branch) pop `conflict_memo` directly.
`record_conflict_memo` stays reachable as `store.record_conflict_memo` (server.py's
`resolve_conflict` tool calls it there) through store.py's existing lazy PEP 562 `__getattr__`.

Store-owned helpers (`title_and_body`, `revisions.current_revision`, `revisions.current_content`,
`revisions.normalize_content`, `store_lock`, `repo_slug`, `load`, `save`, `entry_by_id`) are read through
the `store` module OBJECT, not `from`-imported — the same load-order discipline `guard_engine.py`
documents at its own top: they're looked up at call time, so anything a test monkeypatches on
`contexer.store` is still seen here, and store.py never needs this module at import time.
"""

import hashlib
import re
from datetime import datetime, timezone

from contexer import revisions      # pure stdlib leaf (no cycle): revision lifecycle
from contexer import store          # module object, not `from`-imports: see docstring above


_CONFLICT_GUIDE = (
    "CONFLICT: a decision above has an approved version AND a later, unreviewed update. Do "
    "NOT settle which one holds by exploring the codebase — the code shows what was built, "
    "not which version the developer now intends; only they know. If it matters for the "
    "current task, ask them, then record their answer with "
    'resolve_conflict(entry_id="<the id shown on the decision>", choice="standing"|"update") '
    "— that records the pick, it approves nothing. A decision already carrying a picked/"
    "declined marker is settled for now: steer by what it renders, don't re-ask. Being ASKED "
    "which version is current is not resolving it — report both, naming the update as the "
    "latest stated direction and flagging it as unreviewed."
)


# What `current_pairs` detects, in one place: the guide, the review pane and the retirement
# reason all say it, and a new contradiction class must change exactly this sentence.
CURRENT_PAIR_REASON = "These current decisions prescribe incompatible version formats."

_CURRENT_CONFLICT_GUIDE = (
    f"CONFLICT: {CURRENT_PAIR_REASON[0].lower()}{CURRENT_PAIR_REASON[1:]} "
    "Ask the developer which applies to this task before implementing either. "
    "Their status is unchanged; this marker does not approve or retire a decision."
)


def current_pairs(entries: list[dict]) -> list[tuple[dict, dict]]:
    """Conservative explicit version-format conflicts, never inferred from similarity alone.

    Match an operative prescription, not a historical example or a rationale mentioning
    an alternative. Other contradiction classes stay undetected until they have evidence
    and a specific grammar; lexical overlap is not proof of incompatible guidance.
    """
    from contexer import policy

    prescriptions = []
    for entry in entries:
        if (entry.get("type") != "decision" or store.entry_status(entry) not in {"approved", "suggested"}
                or entry.get("bootstrap") or entry.get("superseded_by")):
            continue
        text = revisions.current_content(entry).lower()
        clauses = [part.strip() for part in re.split(
            r";|\.\s|\b(?:but|and)\s+(?=(?:prefix|publish|use|return)\b)", text)]
        target = r"(?:(?:package[- ]release|package|git[- ]tag|sample[- ]doc) )?"
        prefix_rule = rf"^(?:prefix|publish|use) {target}versions? (?:strings? )?with (?:a )?(?:lowercase )?v\b"
        bare_rule = rf"^(?:publish|use|return) (?:{target}versions? as )?(?:bare|unprefixed) (?:semantic )?{target}versions?\b"
        for clause in clauses:
            for form, pattern in (("prefixed", prefix_rule), ("bare", bare_rule)):
                match = re.match(pattern, clause)
                if not match:
                    continue
                # Only explicit output qualifiers restrict scope. A rationale such as
                # "so they match Git tags" does not narrow a rule for all version strings.
                qualifier = re.search(r"\b(?:for|in|on) (?:the )?([^.;]+)", clause[match.end():])
                output = match.group() + (" " + qualifier.group(1) if qualifier else "")
                scope = ("tags" if re.search(r"git[- ]tags?", output) else
                         "package" if re.search(r"package(?:[- ]release| index)?", output) else
                         "docs" if re.search(r"(?:sample[- ]docs?|documentation|docs)\b", output) else "")
                if not scope and re.search(r"package index (?:rejects|requires|accepts)\b", text):
                    scope = "package"
                prescriptions.append((entry, form, scope))
    pairs = []
    seen = set()
    for left, left_form, left_scope in prescriptions:
        for right, right_form, right_scope in prescriptions:
            if left_form != "prefixed" or right_form != "bare":
                continue
            if left_scope and right_scope and left_scope != right_scope:
                continue
            if left.get("id") == right.get("id"):
                continue
            lfiles, rfiles = {path for path in left.get("source_files") or [] if isinstance(path, str)}, {path for path in right.get("source_files") or [] if isinstance(path, str)}
            if (lfiles and rfiles and not policy.source_anchor_hits(lfiles, rfiles)
                    and not policy.source_anchor_hits(rfiles, lfiles)):
                continue
            key = (left.get("id"), right.get("id"))
            if key not in seen:
                seen.add(key)
                pairs.append((left, right))
    return pairs


def find_current_pair(entries: list[dict], first_id: str, second_id: str) -> tuple[dict, dict] | None:
    """The current contradiction between exactly these two decisions, in either order."""
    wanted = {first_id, second_id}
    return next(((left, right) for left, right in current_pairs(entries)
                 if {left.get("id"), right.get("id")} == wanted), None)


def can_keep(entry: dict) -> bool:
    """Whether a side of a contradiction may be kept over the other. Keeping retires the other
    side as superseded by this one, so only a HUMAN-ratified decision may be kept: one the
    developer stated (`created_by="human"`) or approved (`approved_by="human"`). Status alone is
    not enough: scan facts and bootstrap conventions are born `approved` with no human act, and
    such a capture must never replace a decision a developer ratified."""
    return (store.entry_status(entry) == "approved"
            and (entry.get("created_by") == "human" or entry.get("approved_by") == "human"))


def keep_current_side(repo_path: str, kept_id: str, other_id: str) -> tuple[bool, str]:
    """Settle a contradiction the developer chose a side of: retire `other_id` as superseded by
    `kept_id`, recording why. The pair, and that the kept side is human-ratified (`can_keep`), are
    re-checked under the retirement's own lock, so a pair that changed or vanished since it was
    shown is refused rather than acted on. One decision per call; nothing here is reachable without a human pick."""
    from contexer import lifecycle      # function-level: lifecycle reads the store this module renders

    pair = find_current_pair(store.load(repo_path).get("entries", []), kept_id, other_id)
    kept = next((e for e in pair or () if e.get("id") == kept_id), None)
    title = (kept or {}).get("title") or kept_id[:8]

    def still_keepable(entries: list[dict]) -> str | None:
        live = find_current_pair(entries, kept_id, other_id)
        if live is None:
            return "Those two decisions are not a current conflict; nothing was retired."
        if not can_keep(next(e for e in live if e.get("id") == kept_id)):
            return ("Only a decision you stated or approved can be kept over a contradiction. "
                    "Nothing was retired.")
        return None

    return lifecycle.retire_decision(
        repo_path, other_id,
        f"Contradicted {title!r} ({kept_id[:8]}): {CURRENT_PAIR_REASON} The developer kept "
        "that one in the in-session review pane.",
        replacement_id=kept_id, precondition=still_keepable)


def render_current_pair(left: dict, right: dict, *, seen: set | None = None) -> list[str]:
    seen = set() if seen is None else seen
    lines = [] if seen else [_CURRENT_CONFLICT_GUIDE]
    for entry in (left, right):
        if entry.get("id") in seen:
            continue
        seen.add(entry.get("id"))
        status = store.entry_status(entry)
        date = (entry.get("updated_at") or entry.get("timestamp") or "")[:10]
        text = " ".join(revisions.current_content(entry).split())
        lines.append(f"- [{status}, {date}] {entry.get('title') or revisions.derive_title(text)} "
                     f"(id={entry.get('id', '')[:8]})\n    {text}")
        proposal = entry.get("proposed_revision") or {}
        if proposal:
            pending = " ".join(proposal.get("content", "").split())
            lines.append(f"    [Pending update — not approved] {pending}")
            if proposal.get("applies_when") is not None:
                lines.append("    Proposed applicability: " + "; ".join(proposal["applies_when"]))
            steer = memo_steer_line(entry)
            lines.append("    " + (steer or "Review the pending update with the developer before treating it as operative."))
    return lines


def _conflict_pair_key(entry: dict) -> str:
    """Identity of one (current revision, proposal) pair — what a resolution memo is bound
    to. `created_at` is part of the key on purpose: `verify_scan_conventions` pops a
    proposal WITHOUT advancing the revision, and a later TTL cycle can rebuild a
    byte-identical one — without the timestamp a dead memo would silently revive. Every
    rebuild path is dedup-guarded, so an identical retry keeps its `created_at` and the
    memo legitimately survives."""
    prop = entry.get("proposed_revision") or {}
    rev_id = (revisions.current_revision(entry) or {}).get("revision_id", "")
    raw = (f"{rev_id}\n{prop.get('content', '')}\n"
           f"{prop.get('title', '')}\n{prop.get('created_at', '')}")
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def has_open_conflict(entry: dict) -> bool:
    """Whether a proposal renders as a conversational conflict (issue #193). Excludes
    bookkeeping proposals — a scan-sourced convention withdrawal or an anchor retirement
    re-proposes on every 24h TTL cycle after a dismiss, and "only the developer knows" is
    factually backwards for them (the re-scan IS the evidence) — and title-only
    recaptures, whose content is unchanged. Those keep today's flag-only tag."""
    prop = entry.get("proposed_revision") or {}
    if not prop or prop.get("source") == "scan" or prop.get("clear_anchors"):
        return False
    return (revisions.normalize_content(prop.get("content", ""))
            != revisions.normalize_content(revisions.current_content(entry)))


def _conflict_view(entry: dict) -> tuple[str, str | None, list[str]]:
    """`title_and_body` plus the labeled dual-injection lines for the model-facing render
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
    `_apply_approval`'s dismiss branch leave `conflict_memo` behind, which is inert — a memo
    is only ever read while a proposal is present, and the `created_at` in the pair key stops
    a rebuilt proposal from reviving it."""
    standing_title, standing_body = store.title_and_body(entry)
    if not has_open_conflict(entry):
        if entry.get("bootstrap"):
            from contexer import bootstrap
            return standing_title, standing_body, bootstrap.render(entry)
        return standing_title, standing_body, []
    prop = entry["proposed_revision"]
    prop_date = (prop.get("created_at") or "")[:10]
    memo = entry.get("conflict_memo") or {}
    if memo.get("pair") == _conflict_pair_key(entry):
        memo_date = (memo.get("created_at") or "")[:10]
        if memo.get("choice") == "update":
            # Proposal-side title from the PROPOSAL, never title_and_body(entry, content=...)
            # — that reads the standing title and would head the update's body with it.
            title, body = store.title_and_body({"title": prop.get("title")},
                                                content=prop["content"])
            standing = " ".join(revisions.current_content(entry).split())
            return title, body, [
                f"[this update was picked with the developer on {memo_date} — pending "
                "formal review; not developer-approved]",
                f'Still the approved version on record (superseded by the pick above): "{standing}"',
            ]
        if memo.get("choice") == "standing":
            return standing_title, standing_body, [
                f"[an update proposed {prop_date} was declined with the developer on "
                f"{memo_date} — the update stays pending formal review]"
            ]
    update = " ".join(prop.get("content", "").split())
    return standing_title, standing_body, [
        f'Unreviewed update ({prop_date}, NOT yet approved): "{update}"'
    ]


def memo_pick(entry: dict) -> str | None:
    """The side ("update" or "standing") a still-valid resolution memo records, else None (no
    memo, or one bound to a pair that no longer exists - the `_conflict_view` staleness rule)."""
    memo = entry.get("conflict_memo") or {}
    if not memo or memo.get("pair") != _conflict_pair_key(entry):
        return None
    # Exactly the two choices `_conflict_view` acts on; anything else is no pick at all.
    return memo.get("choice") if memo.get("choice") in ("update", "standing") else None


def memo_steer_line(entry: dict) -> str | None:
    """The one-line steer a still-valid resolution memo adds to a REVIEW surface — shared by
    `store.format_pending_review` and `contexer review`, which render the same sentence with
    their own indentation and leading capital. None when `memo_pick` finds no valid memo."""
    pick = memo_pick(entry)
    if pick is None:
        return None
    date = ((entry.get("conflict_memo") or {}).get("created_at") or "")[:10]
    if pick == "update":
        return (f"the update was picked with the developer on {date}"
                " — approve to formalize (dismiss drops it)")
    return (f"the update was declined with the developer on {date}"
            " — dismiss to formalize (approve applies it instead)")


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
    # Renders always emit 8-char ids, and prefix resolution is first-match-wins — a shorter
    # prefix could silently record the pick against the wrong decision.
    entry_id = entry_id.strip()
    if len(entry_id) < 8:
        return False, ("entry_id must be at least 8 characters — use the id shown with the "
                       "decision, e.g. (id=6fb28fd9).")
    with store.store_lock(store.repo_slug(repo_path)):
        data = store.load_for_update(repo_path)
        entry = store.entry_by_id(data.get("entries", []), entry_id)
        if entry is None:
            return False, f"Decision {entry_id!r} not found."
        if not entry.get("proposed_revision"):
            return False, "That decision has no pending update — there is no conflict to resolve."
        if not has_open_conflict(entry):
            return False, ("That pending update is bookkeeping or title-only, not a conflict — "
                           "approve or dismiss it via approve_decision instead.")
        entry["conflict_memo"] = {
            "pair": _conflict_pair_key(entry),
            "choice": pick,
            "session_id": session_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        store.save(repo_path, data)
    return True, (
        f"Recorded for future sessions: steer by the {pick} version. This is NOT an approval — "
        "the update is still pending formal review, and a later explicit statement from the "
        "developer outranks this memo."
    )
