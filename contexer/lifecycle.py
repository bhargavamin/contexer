"""Decision lifecycle: the `proposed_lifecycle` review lane, retirement, and restoration
(plan C1 + C2).

A decision's one `proposed_revision` slot answers "this decision should READ differently".
Retirement is a different state transition — "this decision should stop being live" — so
sharing that slot would let either proposal silently drop the other. This module owns the
second, independent slot: who may take it, when it goes stale, and the two transitions that
actually move a decision between the live store and the tombstone sidecar.

Extracted out of store.py on the same directive that produced `anchors.py`, `conflicts.py`
and `console_api.py` (one module per cohesive concern; store.py stays a thin call-site
facade). store.py keeps only the seams: `get_pending_decisions` counts this lane,
`format_pending_review` calls `review_lines`, `delete_decision` is a call site into
`tombstone_entry`, and `restore_decision` stays reachable as `store.restore_decision` through
store.py's lazy PEP 562 `__getattr__` — back-compat only, because that name WAS public on
store before this extraction. Nothing else enters that facade: every production caller
imports this module (`anchors`, `reconcile`, `cli`, `server`, `ui/api`), which is what keeps
the boundary visible.

Store-owned helpers (`store_lock`, `load`, `save`, `repo_slug`, `entry_by_id`,
`entry_status`, `load_deleted`, `read_deleted`, `touch_pending_review`, `MAX_ENTRIES`, and
the tombstone sidecar's `_deleted_path` / `_save_deleted` / `_keep_recent_tombstones` plus
the review-render `_clip_body`) are read through the `store` module OBJECT, not
`from`-imported — the identical load-order discipline `guard_engine.py` documents at its own
top: they are looked up at call time, so anything a test monkeypatches on `contexer.store` is
still seen here, and store.py never needs this module at import time. The four private names
above have exactly one reader module — this one — which is the coupling the boundary rules
allow; `touch_pending_review` and `read_deleted` were promoted to public precisely because
this module made them a SECOND reader, and two readers are an undeclared interface.

Two rules carry the lane and neither is negotiable:

* **Nothing here retires anything on its own.** `propose_lifecycle` attaches a question and
  arms the review nudge; the decision keeps rendering, unchanged, until a human calls
  `retire_decision`. That is also the approval path for an ai/scan proposal — a human calling
  it IS the explicit human action, so the recorded actor is "human" either way.
* **A proposal is bound to the revision it was judged against.** If HEAD moves,
  `retire_decision` refuses rather than applying a verdict passed on text nobody read in this
  form; `dismiss_lifecycle` stays available, since dropping a proposal needs no basis.
"""

import uuid
from datetime import datetime, timezone

from contexer import store          # module object, not `from`-imports: see docstring above

LIFECYCLE_ACTIONS = ("retire",)
# V1 has one action. A replacement is not a second action but a field on this one: retiring a
# decision *because another supersedes it* is still a retirement, recorded as lifecycle kind
# "superseded". "replacement_linked" stays reserved vocabulary with no writer.
_ACTIVE_STATUSES = ("approved", "suggested")
CONSOLE_DELETE_REASON = "deleted via console"

# THE kind vocabulary for a COMPLETED lifecycle record, owned here because this module is the
# only writer of one (`lifecycle_record`, which rejects anything else). Readers derive rather
# than respell: `reconcile._RETIRED_KINDS` is `RETIRED_KINDS`, so a rename here cannot leave a
# reader matching on a spelling nothing writes any more while its own tests still pass.
# `restored` is a record but not a retirement — the decision is back in the live store.
RETIRED_KINDS = frozenset({"retired", "superseded"})
RECORD_KINDS = RETIRED_KINDS | {"restored"}


def lifecycle_record(kind: str, *, reason: str, revision_id: str, at: str,
                     replacement_id: str | None = None, actor: str = "human") -> dict:
    """One versioned entry in a decision's `lifecycle` history (plan C1).

    `kind` must be one of `RECORD_KINDS`: this is the one writer, so validating here is what
    makes that vocabulary real rather than a comment. A caller bug raises — the callers are
    both in this module, and neither is on a hook path.

    `revision_id` is captured AT THE TRANSITION by the caller and never re-derived later: the
    entry-level `approved_by` stamp is popped whenever a non-human revision lands
    (`revisions.append_revision`), so history that trusted entry-level state would misreport
    which version of the decision was actually retired."""
    if kind not in RECORD_KINDS:
        raise ValueError(f"kind must be one of {sorted(RECORD_KINDS)}, got {kind!r}")
    return {
        "event_id": str(uuid.uuid4()),
        "kind": kind,
        "occurred_at": at,
        "actor": actor,
        "reason": reason,
        "revision_id": revision_id,
        "replacement_decision_id": replacement_id or None,
    }


def attach_lifecycle_proposal(entry: dict, action: str, reason: str, *, source: str,
                              replacement_id: str | None = None, now: str = "") -> dict | None:
    """Claim the entry's ONE `proposed_lifecycle` slot IN MEMORY, returning the proposal or
    None when a sitting proposal keeps it. Persistence is the caller's (`propose_lifecycle`
    does load/lock/save; `anchors.verify_anchors` already holds the store lock and saves once
    for a whole run, which is why this half is separated out at all — calling the locking
    entrypoint from inside that lock would deadlock on the same flock).

    Within the lane the order is only human-over-automated: `ai` and `scan` are both machine
    guesses at the same transition, and ranking them against each other would let one
    automated proposal quietly overwrite another's reason. A displaced proposal is archived to
    `superseded_lifecycle` (the shape `superseded_proposals` already has), never dropped.

    `proposed_revision` is neither read nor written here: the lanes are independent."""
    now = now or datetime.now(timezone.utc).isoformat()
    sitting = entry.get("proposed_lifecycle")
    if sitting and not (source == "human" and sitting.get("source") != "human"):
        return None
    if sitting:
        entry.setdefault("superseded_lifecycle", []).append({**sitting, "superseded_at": now})
    proposal = {
        "proposal_id": str(uuid.uuid4()),
        "action": action,
        "reason": reason,
        "replacement_decision_id": replacement_id or None,
        # Bound to the revision it was judged against: if HEAD moves the proposal is stale and
        # must be re-reviewed rather than applied to a decision nobody read in this form.
        "basis_revision_id": entry.get("current_revision_id") or "",
        "source": source,
        "created_at": now,
    }
    entry["proposed_lifecycle"] = proposal
    return proposal


def lifecycle_proposal_stale(entry: dict) -> bool:
    """True when the entry's lifecycle proposal was judged against a revision that is no longer
    HEAD. Retirement is refused while stale (the developer must re-propose against what the
    decision says NOW); dismissal stays available, since dropping a proposal needs no basis."""
    prop = entry.get("proposed_lifecycle") or {}
    return bool(prop) and prop.get("basis_revision_id") != (entry.get("current_revision_id") or "")


def propose_lifecycle(repo_path: str, entry_id: str, action: str, reason: str, *,
                      source: str, replacement_id: str | None = None) -> dict:
    """Attach a lifecycle proposal to a LIVE decision. `{"ok", "message", "proposal"}`.

    Proposing is not retiring: the decision keeps rendering exactly as it did until a human
    calls `retire_decision`. A `pending_approval` entry is refused — it is not live yet, and
    `approve_decision(action="ignore")` is the existing way to drop one."""
    if action not in LIFECYCLE_ACTIONS:
        return {"ok": False, "proposal": None,
                "message": f"Unsupported lifecycle action {action!r}. Use: "
                           f"{', '.join(LIFECYCLE_ACTIONS)}."}
    if not (reason or "").strip():
        return {"ok": False, "proposal": None,
                "message": "A lifecycle proposal needs a reason — it becomes permanent history."}
    with store.store_lock(store.repo_slug(repo_path)):
        data = store.load(repo_path)
        entry = store.entry_by_id([e for e in data["entries"] if e.get("type") == "decision"],
                                  entry_id)
        if entry is None:
            return {"ok": False, "proposal": None, "message": f"Decision {entry_id!r} not found."}
        status = store.entry_status(entry)
        if status not in _ACTIVE_STATUSES:
            return {"ok": False, "proposal": None,
                    "message": f"Decision {entry['id'][:8]} is {status}, not live — there is "
                               "nothing to retire."}
        proposal = attach_lifecycle_proposal(entry, action, reason.strip(), source=source,
                                             replacement_id=replacement_id)
        if proposal is None:
            return {"ok": False, "proposal": None,
                    "message": f"Decision {entry['id'][:8]} already carries a developer's "
                               "retirement proposal — it keeps the slot."}
        store.save(repo_path, data)
        store.touch_pending_review(repo_path)   # a lifecycle proposal now awaits review
        return {"ok": True, "proposal": proposal,
                "message": f"Retirement proposed for {entry['id'][:8]} — pending the "
                           "developer's review; the decision stays live until they retire it."}


def dismiss_lifecycle(repo_path: str, entry_id: str) -> tuple[bool, str]:
    """Drop a decision's lifecycle proposal, keeping the decision exactly as it is.

    Dismiss means "not now", not "never ask again": an evidence-driven proposer (anchors.py's
    anchor-loss withdrawal) re-proposes on its next TTL cycle, which is the same semantics its
    `proposed_revision` dismissal had."""
    with store.store_lock(store.repo_slug(repo_path)):
        data = store.load(repo_path)
        entry = store.entry_by_id([e for e in data["entries"] if e.get("type") == "decision"],
                                  entry_id)
        if entry is None:
            return False, f"Decision {entry_id!r} not found."
        if not entry.get("proposed_lifecycle"):
            return False, f"Decision {entry['id'][:8]} has no retirement proposal to dismiss."
        entry.pop("proposed_lifecycle", None)
        store.save(repo_path, data)
        return True, (f"Dismissed the retirement proposal for {entry['id'][:8]} — the decision "
                      "stays live and unchanged.")


def tombstone_entry(repo_path: str, entry_id: str, *, reason: str, replacement_id: str | None,
                    deleted_by: str, stale_guard: bool) -> tuple[bool, str, dict | None]:
    """Move ONE live decision into the tombstone sidecar with a lifecycle record.
    Returns (ok, error message, tombstoned entry) — the caller words its own success message,
    which is what lets `retire_decision` and store's console-facing `delete_decision` share one
    transition while keeping their own vocabulary.

    Both files are written inside ONE lock, sidecar FIRST: a crash between the two writes
    leaves the entry in both places (visible and restorable) rather than in neither.

    Only entries of `type` "decision" are addressable — an id-taking write surface must not be
    able to tombstone some other kind of entry that happens to share the id space.

    Refuses outright when the sidecar cannot be parsed: writing a fresh graveyard over it would
    destroy every tombstone already in it, and un-block every one of those decisions for
    re-capture. A refusal is recoverable; that is not.

    An unresolved `proposed_revision` is ARCHIVED onto the tombstone rather than dropped: it is
    unreviewed content nobody ever ruled on, and a retirement is not a ruling on it."""
    with store.store_lock(store.repo_slug(repo_path)):
        data = store.load(repo_path)
        entry = store.entry_by_id([e for e in data["entries"] if e.get("type") == "decision"],
                                  entry_id)
        if entry is None:
            return False, f"Decision {entry_id!r} not found.", None
        if stale_guard and lifecycle_proposal_stale(entry):
            return False, (
                f"Cannot retire {entry['id'][:8]}: the retirement proposal was made against an "
                "earlier revision and the decision has changed since. Re-review it against what "
                "the decision says now — dismiss_lifecycle drops the stale proposal, and a fresh "
                "one can be proposed against the current revision."), None
        graveyard, error = store.read_deleted(repo_path)
        if error is not None:
            # Verb-neutral: this one message is reached from both `retire_decision` and the
            # console's `delete_decision`, and the alternative (each caller rewording it) is a
            # string edit applied to a message the other caller must never see differently.
            return False, (f"Cannot remove {entry['id'][:8]}: "
                           f"{store._deleted_path(repo_path).name} is unreadable ({error}), and "
                           "overwriting it would discard every tombstone already in it. Move "
                           "that file aside, then retry."), None
        now = datetime.now(timezone.utc).isoformat()
        entry.pop("proposed_lifecycle", None)       # satisfied by this very retirement
        unreviewed = entry.pop("proposed_revision", None)
        if unreviewed:
            entry["unreviewed_proposal_at_retirement"] = unreviewed
        entry.pop("conflict_memo", None)            # the pair it resolved no longer exists
        entry.setdefault("lifecycle", []).append(lifecycle_record(
            "superseded" if replacement_id else "retired", reason=reason, at=now,
            revision_id=entry.get("current_revision_id") or "",
            replacement_id=replacement_id))
        entry["deleted_at"] = now
        entry["deleted_by"] = deleted_by
        graveyard["repo_path"] = repo_path
        graveyard["entries"] = store._keep_recent_tombstones(graveyard["entries"] + [entry])
        store._save_deleted(repo_path, graveyard)
        data["entries"] = [e for e in data["entries"] if e is not entry]
        store.save(repo_path, data)
        return True, "", entry


def retire_decision(repo_path: str, entry_id: str, reason: str,
                    replacement_id: str | None = None) -> tuple[bool, str]:
    """Retire a live decision: it leaves active context for the tombstone sidecar, keeping its
    full revision and lifecycle history. Returns (ok, message).

    This is also the APPROVAL path for an ai/scan-proposed retirement — a human calling it IS
    the explicit human action, so the lifecycle actor is "human" either way. A proposal made
    against a superseded revision is refused here rather than applied blind (see
    `lifecycle_proposal_stale`); a direct retirement with no sitting proposal has no staleness
    question to answer."""
    if not (reason or "").strip():
        return False, ("A retirement needs a reason — it is recorded permanently as the "
                       "decision's lifecycle history.")
    replacement_id = (replacement_id or "").strip() or None
    ok, message, entry = tombstone_entry(
        repo_path, entry_id, reason=reason.strip(), replacement_id=replacement_id,
        deleted_by="human", stale_guard=True)
    if not ok:
        return False, message
    what = "Superseded" if replacement_id else "Retired"
    tail = f" (replaced by {replacement_id[:8]})" if replacement_id else ""
    return True, (f"{what} {entry['id'][:8]}{tail}. It no longer appears in retrieval, session "
                  "context, or the commit-time guard; its history is kept and "
                  "restore_decision brings it back.")


def restore_decision(repo_path: str, entry_id: str, reason: str = "") -> tuple[bool, str]:
    """Move a tombstoned decision back into the live store. Returns (ok, message).

    Write order MIRRORS `tombstone_entry` instead of repeating it: the live store goes first
    here, so the crash window again duplicates the entry rather than dropping it.

    Idempotent against that very window. A retirement that crashed between its two writes
    leaves the entry in BOTH files; appending unconditionally then put the same id in the live
    store twice, and since every id-taking store function resolves only the first match, the
    second copy was unreachable and undeletable. When the id is already live the sidecar copy
    is the stale one, so it is dropped instead of appended.

    Refuses when the live store is at capacity rather than evicting to make room: the old
    `_keep_top(..., pin_last=True)` pinned the RESTORED entry, so it dropped some other
    decision — and unlike a retirement, that one got no tombstone. An action framed as
    non-destructive must not destroy anything.

    The entry comes back with its prior status and its whole `lifecycle` list, one "restored"
    record longer — history accumulates rather than being rewound."""
    with store.store_lock(store.repo_slug(repo_path)):
        graveyard = store.load_deleted(repo_path)
        entry = store.entry_by_id(graveyard["entries"], entry_id)
        if entry is None:
            return False, f"Deleted decision {entry_id!r} not found."
        data = store.load(repo_path)
        # Full id, never the caller's prefix: this asks "is THIS entry already live".
        if store.entry_by_id(data["entries"], entry["id"]) is not None:
            graveyard["repo_path"] = repo_path
            graveyard["entries"] = [e for e in graveyard["entries"] if e is not entry]
            store._save_deleted(repo_path, graveyard)
            return True, (f"{entry['id'][:8]} was already in the live store — dropped the "
                          "leftover tombstone instead of storing a second copy.")
        if len(data["entries"]) >= store.MAX_ENTRIES:
            return False, (f"Cannot restore {entry['id'][:8]}: the store already holds "
                           f"{store.MAX_ENTRIES} entries, the maximum. Restoring would evict "
                           "another decision with no tombstone — delete one yourself first.")
        entry.pop("deleted_at", None)
        entry.pop("deleted_by", None)
        entry.setdefault("lifecycle", []).append(lifecycle_record(
            "restored", reason=(reason or "").strip(),
            at=datetime.now(timezone.utc).isoformat(),
            revision_id=entry.get("current_revision_id") or ""))
        data["entries"].append(entry)
        store.save(repo_path, data)
        graveyard["repo_path"] = repo_path
        graveyard["entries"] = [e for e in graveyard["entries"] if e is not entry]
        store._save_deleted(repo_path, graveyard)
        return True, f"Restored {entry['id'][:8]}."


def review_lines(entry: dict, eid: str) -> list[str]:
    """The labeled retirement block a lifecycle proposal adds to `store.format_pending_review`:
    who proposed it, why, whether it is stale, and what each answer actually does. Its own
    block, never folded into the content-proposal render — approving a Suggested Update and
    retiring the decision are different answers to different questions."""
    life = entry.get("proposed_lifecycle") or {}
    lines = [f'    retirement proposed (source={life.get("source") or "unknown"}): '
             f'"{store._clip_body(life.get("reason") or "")}"']
    replacement = (life.get("replacement_decision_id") or "")[:8]
    if replacement:
        lines.append(f"    replaced by: {replacement}")
    if lifecycle_proposal_stale(entry):
        lines.append("    STALE — this was proposed against an earlier revision and the decision "
                     "has changed since; retire_decision is refused until it is re-proposed "
                     "against the current version. dismiss_lifecycle still works.")
        lines.append(f'    dismiss_lifecycle(entry_id="{eid}")')
    else:
        lines.append("    retiring moves it out of active context (retrieval, session start and "
                     "the commit-time guard all stop seeing it) and keeps its history; "
                     "dismissing keeps the decision live and unchanged.")
        lines.append(f'    retire_decision(entry_id="{eid}", reason="<the developer\'s reason>")'
                     f' | dismiss_lifecycle(entry_id="{eid}")')
    lines.append("    Ask the developer — never retire a decision on your own judgment.")
    return lines
