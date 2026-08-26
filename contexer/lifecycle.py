"""Decision lifecycle: the `proposed_lifecycle` review lane, retirement, and restoration
(plan C1 + C2).

A decision's one `proposed_revision` slot answers "this decision should READ differently".
Retirement is a different state transition - "this decision should stop being live" - so
sharing that slot would let either proposal silently drop the other. This module owns the
second, independent slot: who may take it, when it goes stale, and the two transitions that
actually move a decision between the live store and the tombstone sidecar.

Extracted out of store.py on the same directive that produced `anchors.py`, `conflicts.py`
and `console_api.py` (one module per cohesive concern; store.py stays a thin call-site
facade). store.py keeps only the seams: `get_pending_decisions` counts this lane,
`format_pending_review` calls `review_lines`, `delete_decision` is a call site into
`tombstone_entry`, and `restore_decision` stays reachable as `store.restore_decision` through
store.py's lazy PEP 562 `__getattr__` - back-compat only, because that name WAS public on
store before this extraction. Nothing else enters that facade: every production caller
imports this module (`anchors`, `reconcile`, `cli`, `server`, `ui/api`), which is what keeps
the boundary visible.

Store-owned helpers (`store_lock`, `load`, `save`, `repo_slug`, `entry_by_id`,
`entry_status`, `load_deleted`, `read_deleted`, `touch_pending_review`, `MAX_ENTRIES`, and
the tombstone sidecar's `_deleted_path` / `_save_deleted` / `_keep_recent_tombstones` plus
the review-render `clip_body`) are read through the `store` module OBJECT, not
`from`-imported - the identical load-order discipline `guard_engine.py` documents at its own
top: they are looked up at call time, so anything a test monkeypatches on `contexer.store` is
still seen here, and store.py never needs this module at import time. The four private names
above have exactly one reader module - this one - which is the coupling the boundary rules
allow; `touch_pending_review` and `read_deleted` were promoted to public precisely because
this module made them a SECOND reader, and two readers are an undeclared interface.

Two rules carry the lane and neither is negotiable:

* **Nothing here retires anything on its own.** `propose_lifecycle` attaches a question and
  arms the review nudge; the decision keeps rendering, unchanged, until a human calls
  `retire_decision`. That is also the approval path for an ai/scan proposal - a human calling
  it IS the explicit human action, so the recorded actor is "human" either way.
* **A proposal is bound to the revision it was judged against.** If HEAD moves,
  `retire_decision` refuses rather than applying a verdict passed on text nobody read in this
  form; `dismiss_lifecycle` stays available, since dropping a proposal needs no basis.
"""

import uuid
from datetime import datetime, timezone

from contexer import revisions
from contexer import store          # module object, not `from`-imports: see docstring above

LIFECYCLE_ACTIONS = ("retire",)
# The four answers a developer may give a reconsideration proposal. `skip` writes nothing at
# all - it is here so the surfaces have one vocabulary rather than two.
RECONSIDER_ACTIONS = ("restore", "restore_edit", "skip", "dismiss")
_RECONSIDER_SLOT = "proposed_reconsideration"
# The dismissal receipts kept on one decision. Bounded like every other accumulating list in
# this store; the count is what a reviewer needs ("asked before, three times"), not the tail.
_MAX_RECONSIDER_HISTORY = 20
# V1 has one action. A replacement is not a second action but a field on this one: retiring a
# decision *because another supersedes it* is still a retirement, recorded as lifecycle kind
# "superseded". "replacement_linked" stays reserved vocabulary with no writer.
_ACTIVE_STATUSES = ("approved", "suggested")
CONSOLE_DELETE_REASON = "deleted via console"

# THE kind vocabulary for a COMPLETED lifecycle record, owned here because this module is the
# only writer of one (`lifecycle_record`, which rejects anything else). Readers derive rather
# than respell: `reconcile._RETIRED_KINDS` is `RETIRED_KINDS`, so a rename here cannot leave a
# reader matching on a spelling nothing writes any more while its own tests still pass.
# `restored` is a record but not a retirement - the decision is back in the live store.
RETIRED_KINDS = frozenset({"retired", "superseded"})
RECORD_KINDS = RETIRED_KINDS | {"restored"}


def lifecycle_record(kind: str, *, reason: str, revision_id: str, at: str,
                     replacement_id: str | None = None, actor: str = "human") -> dict:
    """One versioned entry in a decision's `lifecycle` history (plan C1).

    `kind` must be one of `RECORD_KINDS`: this is the one writer, so validating here is what
    makes that vocabulary real rather than a comment. A caller bug raises - the callers are
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
    for a whole run, which is why this half is separated out at all - calling the locking
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
    calls `retire_decision`. A `pending_approval` entry is refused - it is not live yet, and
    `approve_decision(action="ignore")` is the existing way to drop one."""
    if action not in LIFECYCLE_ACTIONS:
        return {"ok": False, "proposal": None,
                "message": f"Unsupported lifecycle action {action!r}. Use: "
                           f"{', '.join(LIFECYCLE_ACTIONS)}."}
    if not (reason or "").strip():
        return {"ok": False, "proposal": None,
                "message": "A lifecycle proposal needs a reason - it becomes permanent history."}
    with store.store_lock(store.repo_slug(repo_path)):
        data = store.load(repo_path)
        entry = store.entry_by_id([e for e in data["entries"] if e.get("type") == "decision"],
                                  entry_id)
        if entry is None:
            return {"ok": False, "proposal": None, "message": f"Decision {entry_id!r} not found."}
        status = store.entry_status(entry)
        if status not in _ACTIVE_STATUSES:
            return {"ok": False, "proposal": None,
                    "message": f"Decision {entry['id'][:8]} is {status}, not live - there is "
                               "nothing to retire."}
        proposal = attach_lifecycle_proposal(entry, action, reason.strip(), source=source,
                                             replacement_id=replacement_id)
        if proposal is None:
            return {"ok": False, "proposal": None,
                    "message": f"Decision {entry['id'][:8]} already carries a developer's "
                               "retirement proposal - it keeps the slot."}
        store.save(repo_path, data)
        store.touch_pending_review(repo_path)   # a lifecycle proposal now awaits review
        return {"ok": True, "proposal": proposal,
                "message": f"Retirement proposed for {entry['id'][:8]} - pending the "
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
        return True, (f"Dismissed the retirement proposal for {entry['id'][:8]} - the decision "
                      "stays live and unchanged.")


def tombstone_entry(repo_path: str, entry_id: str, *, reason: str, replacement_id: str | None,
                    deleted_by: str, stale_guard: bool) -> tuple[bool, str, dict | None]:
    """Move ONE live decision into the tombstone sidecar with a lifecycle record.
    Returns (ok, error message, tombstoned entry) - the caller words its own success message,
    which is what lets `retire_decision` and store's console-facing `delete_decision` share one
    transition while keeping their own vocabulary.

    Both files are written inside ONE lock, sidecar FIRST: a crash between the two writes
    leaves the entry in both places (visible and restorable) rather than in neither.

    Only entries of `type` "decision" are addressable - an id-taking write surface must not be
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
                "the decision says now - dismiss_lifecycle drops the stale proposal, and a fresh "
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

    This is also the APPROVAL path for an ai/scan-proposed retirement - a human calling it IS
    the explicit human action, so the lifecycle actor is "human" either way. A proposal made
    against a superseded revision is refused here rather than applied blind (see
    `lifecycle_proposal_stale`); a direct retirement with no sitting proposal has no staleness
    question to answer."""
    if not (reason or "").strip():
        return False, ("A retirement needs a reason - it is recorded permanently as the "
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
    decision - and unlike a retirement, that one got no tombstone. An action framed as
    non-destructive must not destroy anything.

    The entry comes back with its prior status and its whole `lifecycle` list, one "restored"
    record longer - history accumulates rather than being rewound."""
    with store.store_lock(store.repo_slug(repo_path)):
        return _restore_unlocked(repo_path, entry_id, reason)[:2]


def _restore_unlocked(repo_path: str, entry_id: str, reason: str = "",
                      *, amend=None) -> tuple[bool, str, dict | None]:
    """`restore_decision`'s body with the lock ALREADY HELD, plus an `amend(entry)` hook the
    reconsideration lane uses to settle its proposal in the same write.

    Separated out rather than duplicated because `store.store_lock` is an `flock` with no
    timeout: a second acquisition from the same process would wait on itself forever, so a
    caller that already holds the lock cannot reach `restore_decision` at all. Returns the
    restored entry so the caller can read what it just wrote.

    `amend` never fires on the already-live branch, and deliberately so: the only caller that
    passes one is the reconsideration lane, whose `_locate_inactive` has already healed the
    doubled state before it decides which branch to take, so reaching here with an id that is
    also live means the tombstone is pure residue and there is nothing left to settle. Running
    the hook anyway would file the lane's receipt with no `restored` record beside it, which is
    a receipt naming an approval that never happened.
    """
    graveyard = store.load_deleted(repo_path)
    entry = store.entry_by_id(graveyard["entries"], entry_id)
    if entry is None:
        return False, f"Deleted decision {entry_id!r} not found.", None
    data = store.load(repo_path)
    # Full id, never the caller's prefix: this asks "is THIS entry already live".
    live = store.entry_by_id(data["entries"], entry["id"])
    if live is not None:
        graveyard["repo_path"] = repo_path
        graveyard["entries"] = [e for e in graveyard["entries"] if e is not entry]
        store._save_deleted(repo_path, graveyard)
        return True, (f"{entry['id'][:8]} was already in the live store - dropped the "
                      "leftover tombstone instead of storing a second copy."), live
    if len(data["entries"]) >= store.MAX_ENTRIES:
        return False, (f"Cannot restore {entry['id'][:8]}: the store already holds "
                       f"{store.MAX_ENTRIES} entries, the maximum. Restoring would evict "
                       "another decision with no tombstone - delete one yourself first."), None
    entry.pop("deleted_at", None)
    entry.pop("deleted_by", None)
    entry.setdefault("lifecycle", []).append(lifecycle_record(
        "restored", reason=(reason or "").strip(),
        at=datetime.now(timezone.utc).isoformat(),
        revision_id=entry.get("current_revision_id") or ""))
    if amend is not None:
        amend(entry)
    data["entries"].append(entry)
    store.save(repo_path, data)
    graveyard["repo_path"] = repo_path
    graveyard["entries"] = [e for e in graveyard["entries"] if e is not entry]
    store._save_deleted(repo_path, graveyard)
    return True, f"Restored {entry['id'][:8]}.", entry


def review_lines(entry: dict, eid: str) -> list[str]:
    """The labeled retirement block a lifecycle proposal adds to `store.format_pending_review`:
    who proposed it, why, whether it is stale, and what each answer actually does. Its own
    block, never folded into the content-proposal render - approving a Suggested Update and
    retiring the decision are different answers to different questions."""
    life = entry.get("proposed_lifecycle") or {}
    lines = [f'    retirement proposed (source={life.get("source") or "unknown"}): '
             f'"{store.clip_body(life.get("reason") or "")}"']
    replacement = (life.get("replacement_decision_id") or "")[:8]
    if replacement:
        lines.append(f"    replaced by: {replacement}")
    if lifecycle_proposal_stale(entry):
        lines.append("    STALE - this was proposed against an earlier revision and the decision "
                     "has changed since; retire_decision is refused until it is re-proposed "
                     "against the current version. dismiss_lifecycle still works.")
        lines.append(f'    dismiss_lifecycle(entry_id="{eid}")')
    else:
        lines.append("    retiring moves it out of active context (retrieval, session start and "
                     "the commit-time guard all stop seeing it) and keeps its history; "
                     "dismissing keeps the decision live and unchanged.")
        lines.append(f'    retire_decision(entry_id="{eid}", reason="<the developer\'s reason>")'
                     f' | dismiss_lifecycle(entry_id="{eid}")')
    lines.append("    Ask the developer - never retire a decision on your own judgment.")
    return lines


# ── the reconsideration lane (hardening Task 04) ─────────────────────────────────
#
# A THIRD question, and the reason it gets its own slot rather than borrowing one. The
# `proposed_revision` lane answers "this live decision should READ differently" and the
# `proposed_lifecycle` lane answers "this live decision should stop being live". Neither can
# carry "this decision the developer already switched off should come BACK", because the
# subject of that question is not live at all: an ignored decision sits in the store with an
# inactive status, and a retired one is not in the store at all - it is in the tombstone
# sidecar. Sharing a slot would also let either proposal silently displace the other, the
# exact failure this module was extracted to stop.
#
# The lane exists because a restatement of an inactive decision used to be absorbed by the
# store's status-blind dedup and its evidence destroyed with no receipt anywhere
# (OUTSTANDING-ISSUES item 7). It is now surfaced against the ORIGINAL decision identity, so
# the revisions, the retirement record and the reconsideration receipts stay one continuous
# history rather than becoming a second decision that says the same thing.
#
# Two rules carry it, and they mirror the retirement lane's:
#
# * **Nothing here restores anything on its own.** `propose_reconsideration` attaches a
#   question. The decision stays exactly as inactive as it was until a developer answers.
# * **A proposal is bound to the revision it was judged against.** If the inactive record
#   changes, or is restored by some other route, restoration is REFUSED - `dismiss` stays
#   available, since dropping a question needs no basis.


def attach_reconsideration(entry: dict, *, content: str, title: str, candidate_id: str,
                           source_files=(), source: str = "ai",
                           now: str = "") -> dict | None:
    """Claim the entry's ONE reconsideration slot IN MEMORY, returning the proposal or None
    when a sitting proposal keeps it. Persistence is the caller's, exactly as with
    `attach_lifecycle_proposal`.

    `source_files` are the CONFIRMED ones only. A candidate's merely-possible paths - the ones
    it reached through an uncertain link - are deliberately not carried here at all: an anchor
    is guard and staleness input, so a wrong one is worse than none (runbook invariant 6), and
    the cheapest way to guarantee one never becomes a restoration anchor is for this lane never
    to hold it. That also keeps the aggregator and its ledger the only two modules that name
    that field at all, which is the structural half of the same invariant.
    """
    now = now or datetime.now(timezone.utc).isoformat()
    sitting = entry.get(_RECONSIDER_SLOT)
    if sitting and not (source == "human" and sitting.get("source") != "human"):
        return None
    if sitting:
        entry.setdefault("superseded_reconsiderations", []).append({**sitting,
                                                                   "superseded_at": now})
    proposal = {
        "proposal_id": str(uuid.uuid4()),
        "content": content,
        "title": title,
        "candidate_id": candidate_id,
        "source_files": [f for f in source_files if isinstance(f, str) and f],
        "target_state": "retired" if entry.get("deleted_at") else "ignored",
        "basis_revision_id": entry.get("current_revision_id") or "",
        "source": source,
        "created_at": now,
    }
    entry[_RECONSIDER_SLOT] = proposal
    return proposal


def reconsideration_stale(entry: dict) -> bool:
    """True when the inactive record moved under its own reconsideration proposal.

    Two ways it can, and both mean the developer would be answering about something they were
    not shown: HEAD advanced (the wording changed), or the decision left the state it was
    judged in - restored, retired or un-ignored by some other route while the question sat.
    """
    prop = entry.get(_RECONSIDER_SLOT) or {}
    if not prop:
        return False
    if prop.get("basis_revision_id") != (entry.get("current_revision_id") or ""):
        return True
    now_state = "retired" if entry.get("deleted_at") else \
        ("ignored" if store.entry_status(entry) == "ignored" else "live")
    return now_state != prop.get("target_state")


def _locate_inactive(repo_path: str, entry_id: str) -> tuple:
    """`(entry, data, graveyard)` for an inactive decision, looking in the live store first
    and the tombstone sidecar second. `data`/`graveyard` are the loaded files so the caller
    saves whichever one actually holds the entry; the other stays None.

    Live first because an IGNORED decision never left the store - only a RETIRED one did.

    HEALS the doubled state on the way past, which is the one thing live-first cannot simply
    ignore. A restoration writes the live store before it clears the sidecar (`tombstone_entry`
    mirrors it the other way round, so a crash duplicates rather than drops), so an interrupted
    one leaves the same id in BOTH files. Left alone, this lane then answers every question
    about that decision from the live copy while the tombstone keeps its own
    `proposed_reconsideration` - a review item no action can ever clear, beside a live entry
    still stamped `deleted_at`. The tombstone is unambiguously the stale copy here, so it is
    dropped and the stamp popped, which is the same rule `_restore_unlocked` states for the
    same window and the reason that branch exists at all.
    """
    data = store.load(repo_path)
    entry = store.entry_by_id([e for e in data["entries"] if e.get("type") == "decision"],
                              entry_id)
    if entry is not None:
        stale = store.entry_by_id(store.load_deleted(repo_path)["entries"], entry["id"])
        if stale is not None:
            _drop_stale_tombstone(repo_path, entry, data)
        return entry, data, None
    graveyard = store.load_deleted(repo_path)
    entry = store.entry_by_id([e for e in graveyard["entries"]
                               if e.get("type") == "decision"], entry_id)
    return entry, None, graveyard


def _drop_stale_tombstone(repo_path: str, live: dict, data: dict) -> None:
    """Finish an interrupted restoration: drop the leftover tombstone for a decision that is
    already live, and clear the `deleted_at`/`deleted_by` stamps the crash left behind.

    Sidecar first, so a crash HERE leaves the entry in both places again rather than losing the
    stamp with no tombstone to explain it - the same ordering rule both write paths follow."""
    graveyard = store.load_deleted(repo_path)
    graveyard["repo_path"] = repo_path
    graveyard["entries"] = [e for e in graveyard["entries"]
                            if str(e.get("id") or "") != str(live.get("id") or "")]
    store._save_deleted(repo_path, graveyard)
    live.pop("deleted_at", None)
    live.pop("deleted_by", None)
    store.save(repo_path, data)


def propose_reconsideration(repo_path: str, entry_id: str, *, content: str, title: str,
                            candidate_id: str, source_files=(), source: str = "ai") -> dict:
    """Attach a reconsideration question to an INACTIVE decision. `{"ok", "message",
    "proposal"}`.

    Proposing restores nothing: an ignored decision stays ignored and a retired one stays in
    the tombstone sidecar until a developer answers through `reconsider_decision`. A decision
    that is still LIVE is refused outright - there is nothing to reconsider, and a restatement
    of a live decision is an ordinary duplicate or update in the content lane.
    """
    if not (content or "").strip():
        return {"ok": False, "proposal": None,
                "message": "A reconsideration needs the restated wording it is asking about."}
    with store.store_lock(store.repo_slug(repo_path)):
        entry, data, graveyard = _locate_inactive(repo_path, entry_id)
        if entry is None:
            return {"ok": False, "proposal": None,
                    "message": f"Decision {entry_id!r} not found, live or retired."}
        retired = graveyard is not None
        if not retired and store.entry_status(entry) != "ignored":
            return {"ok": False, "proposal": None,
                    "message": f"Decision {entry['id'][:8]} is live - there is nothing to "
                               "reconsider."}
        proposal = attach_reconsideration(
            entry, content=content.strip(), title=title, candidate_id=candidate_id,
            source_files=source_files, source=source)
        if proposal is None:
            return {"ok": False, "proposal": None,
                    "message": f"Decision {entry['id'][:8]} already carries a developer's "
                               "reconsideration - it keeps the slot."}
        if retired:
            graveyard["repo_path"] = repo_path
            store._save_deleted(repo_path, graveyard)
        else:
            store.save(repo_path, data)
        store.touch_pending_review(repo_path)
        return {"ok": True, "proposal": proposal,
                "message": f"Reconsideration proposed for {entry['id'][:8]} - the decision "
                           "stays inactive until the developer restores it."}


def pending_reconsiderations(repo_path: str) -> list[dict]:
    """Every inactive decision carrying an open reconsideration - ignored ones from the live
    store, retired ones from the tombstone sidecar, in that order.

    A tombstoned entry is handed back as-is, carrying its `deleted_at`, which is what every
    render below reads to say "retired" rather than "ignored". Nothing here mutates.

    A tombstone whose id is ALSO live is skipped: that is the interrupted-restoration residue
    `_locate_inactive` heals, and the live copy is the current one. This read is on the review
    surfaces, which must never offer the stale copy - answering it is what produced a review
    item nothing could clear.
    """
    live = [e for e in store.load(repo_path).get("entries", [])
            if isinstance(e, dict) and e.get("type") == "decision"]
    live_ids = {str(e.get("id") or "") for e in live}
    ignored = [e for e in live if e.get(_RECONSIDER_SLOT)]
    retired = [e for e in store.load_deleted(repo_path).get("entries", [])
               if isinstance(e, dict) and e.get("type") == "decision"
               and e.get(_RECONSIDER_SLOT) and str(e.get("id") or "") not in live_ids]
    return ignored + retired


def _reconsider_receipt(entry: dict, prop: dict, disposition: str, action: str,
                        now: str) -> None:
    """Record what was answered, on the decision itself. The durable half of every outcome:
    once the raw evidence is finalized away, this list is the only place the question and its
    answer survive - and a reviewer meeting the same restatement again needs to see that it
    was asked before."""
    history = entry.get("reconsideration_history")
    history = (history if isinstance(history, list) else []) + [{
        "candidate_id": prop.get("candidate_id") or "",
        "proposal_id": prop.get("proposal_id") or "",
        "disposition": disposition,
        "action": action,
        "basis_revision_id": prop.get("basis_revision_id") or "",
        "content": prop.get("content") or "",
        "occurred_at": now,
    }]
    entry["reconsideration_history"] = history[-_MAX_RECONSIDER_HISTORY:]


def _fail_toward_review(entry: dict) -> None:
    """A decision coming back with no historical ACTIVE status returns PENDING, never trusted.

    An ignored entry records only that it was switched off - nothing anywhere says what it was
    before - so approving it on the way back in would be a guess dressed as a ratification,
    and a guess that arms the commit-time guard. A retired entry normally carries the status it
    held when it was retired, and keeps it; one retired out of an inactive state hits the same
    rule for the same reason.
    """
    if store.entry_status(entry) not in _ACTIVE_STATUSES:
        entry["status"] = "pending_approval"
        entry.pop("approved_by", None)


def reconsider_decision(repo_path: str, entry_id: str, action: str,
                        content: str = "") -> tuple[bool, str]:
    """The developer's answer to a reconsideration. Returns (ok, message).

    * `restore` - the same decision, the same id, the same revision history, back in the live
      store. It returns PENDING when nothing records that it was ever approved (see
      `_fail_toward_review`), so restoring can never fabricate a ratification.
    * `restore_edit` - the same identity, plus the developer's own wording appended as a new
      human-approved revision. This is the way back for a decision whose point still stands
      but whose text does not.
    * `skip` - keep the question pending. Writes nothing.
    * `dismiss` - the decision stays inactive and the question is answered, durably. A LATER
      human directive may raise it again; repeated agent evidence may not.

    Only a human calls this, from `contexer review` or the `reconsider_decision` tool, which
    is why the restored lifecycle record's actor is "human" either way - the same rule
    `retire_decision` follows.
    """
    action = (action or "").strip()
    if action not in RECONSIDER_ACTIONS:
        return False, (f"Unsupported reconsideration action {action!r}. Use: "
                       f"{', '.join(RECONSIDER_ACTIONS)}.")
    if action == "restore_edit" and not (content or "").strip():
        return False, "restore_edit needs the developer's wording for the new revision."
    with store.store_lock(store.repo_slug(repo_path)):
        entry, data, graveyard = _locate_inactive(repo_path, entry_id)
        if entry is None:
            return False, f"Decision {entry_id!r} not found, live or retired."
        prop = entry.get(_RECONSIDER_SLOT)
        if not prop:
            return False, f"Decision {entry['id'][:8]} has no reconsideration to answer."
        eid = entry["id"][:8]
        now = datetime.now(timezone.utc).isoformat()
        if action == "skip":
            return True, f"Left the reconsideration of {eid} pending."
        if action == "dismiss":
            # Read BEFORE the pop: a stale question can sit on a decision that has since come
            # back by another route, and "kept it inactive" would then be a plain untruth about
            # a decision the developer can see in their context.
            still_inactive = graveyard is not None or store.entry_status(entry) == "ignored"
            entry.pop(_RECONSIDER_SLOT, None)
            _reconsider_receipt(entry, prop, "dismissed", action, now)
            if graveyard is not None:
                graveyard["repo_path"] = repo_path
                store._save_deleted(repo_path, graveyard)
            else:
                store.save(repo_path, data)
            head = (f"Kept {eid} inactive." if still_inactive
                    else f"Dropped the reconsideration of {eid}, which is live again already.")
            return True, (f"{head} The question and its answer are recorded on the decision; "
                          "a later directive from you can raise it again.")
        if reconsideration_stale(entry):
            return False, (
                f"Cannot restore {eid}: this reconsideration was raised against an earlier "
                "revision or a different state, and the decision has moved since. Dismiss it "
                "and let a fresh restatement raise the question against what the decision "
                "says now.")

        def _settle(target: dict) -> None:
            target.pop(_RECONSIDER_SLOT, None)
            _fail_toward_review(target)
            if action == "restore_edit":
                revisions.append_revision(target, content.strip(), source="human",
                                          approved_at=now)
                target["status"] = "approved"
                target["approved_by"] = "human"
            _reconsider_receipt(target, prop, "approved", action, now)

        if graveyard is not None:
            ok, message, restored = _restore_unlocked(
                repo_path, entry["id"], "reconsidered after a developer restatement",
                amend=_settle)
            if not ok:
                return False, message
            entry = restored or entry
        else:
            # An IGNORED decision never left the live store, so there is nothing to move: the
            # `restored` record is appended here rather than by `_restore_unlocked`, and it
            # carries the BASIS revision so the disposition rules read one shape for both
            # halves of the lane.
            entry.setdefault("lifecycle", []).append(lifecycle_record(
                "restored", reason="reconsidered after a developer restatement", at=now,
                revision_id=entry.get("current_revision_id") or ""))
            _settle(entry)
            store.save(repo_path, data)
        store.touch_pending_review(repo_path)
        status = store.entry_status(entry)
        tail = (" It is live and approved." if status == "approved"
                else " It is back as PENDING - approve it in review to make it trusted "
                     "context, since nothing recorded that it was ever approved before.")
        return True, f"Restored {eid} with its full history.{tail}"


def reconsideration_review_lines(entry: dict, eid: str) -> list[str]:
    """The reconsideration block `store.format_pending_review` renders under an inactive
    decision: what was restated, where it came from, whether the question was asked before,
    and what each answer does. Its own block for the same reason the retirement block is -
    restoring a decision and rewording a live one are different answers to different
    questions."""
    prop = entry.get(_RECONSIDER_SLOT) or {}
    state = "retired" if entry.get("deleted_at") else "ignored"
    lines = [f"    reconsideration proposed (source={prop.get('source') or 'unknown'}): this "
             f"{state} decision was restated by the developer",
             f'    restated as: "{store.clip_body(prop.get("content") or "")}"']
    # The proposal's confirmed files are NOT listed here: `review_impact.impact_lines` renders
    # them beside the uncertain ones it refuses to anchor, and two spellings of "confirmed
    # files" on one screen is exactly the per-surface interpretation Task 07 removed.
    prior = [row for row in entry.get("reconsideration_history") or []
             if isinstance(row, dict) and row.get("disposition") == "dismissed"]
    if prior:
        lines.append(f"    asked before: dismissed {len(prior)} time(s), most recently "
                     f"{(prior[-1].get('occurred_at') or '')[:10]}")
    if reconsideration_stale(entry):
        lines.append("    STALE - the decision has changed or moved since this was raised; "
                     "restoring is refused until a fresh restatement raises it again. "
                     "Dismissing still works.")
        lines.append(f'    reconsider_decision(entry_id="{eid}", action="dismiss")')
    else:
        lines.append("    restoring brings the SAME decision back with its whole history "
                     "(pending review unless it was approved before); restore_edit appends "
                     "the developer's own wording as a new approved revision; dismissing "
                     "keeps it inactive and records that the question was asked.")
        lines.append(f'    reconsider_decision(entry_id="{eid}", '
                     'action="restore|restore_edit|skip|dismiss")')
    lines.append("    Ask the developer - never restore a decision they switched off on your "
                 "own judgment.")
    return lines
