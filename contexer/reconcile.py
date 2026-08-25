"""Turn unconsumed evidence into decisions a developer REVIEWS.

The one coordinator in the evidence pipeline: it reads the ledger (`evidence`), scores what
it finds (`candidates`, pure), and materializes the result through the store's ordinary
capture path. Nothing here approves, retires, or trusts anything, and every decision it
creates is REVIEWABLE by construction:

* a new candidate lands `pending_approval` — `force_pending`, never the `suggested` tier an
  ai capture would otherwise get, because `suggested` injects at session start yet never
  appears in `review_pending`: trusted without ever having been offered for review;
* an update lands as a `proposed_revision` on its target, HEAD unmoved until approval;
* a retirement or replacement lands as a `proposed_lifecycle` on its target — a proposal in the
  separate lifecycle lane, which retires nothing: only an explicit `lifecycle.retire_decision` by a
  human moves a decision out of active context.

The same discipline governs the way back out: a checkpoint is only ever marked `approved`
against real review evidence — a human ratification stamp, a content proposal whose revision
actually advanced, or a decision genuinely retired into the tombstone sidecar — never inferred
from an entry merely having stopped being pending, and never from a revision advance the
checkpoint's own lane cannot be caused by.

Above store rather than beside it: `store.py` never imports this module, and store-owned
helpers are read through the store MODULE OBJECT at call time — the load-order discipline
`guard_engine.py` documents, so a value a test patches on `contexer.store` is seen here.

Two properties are load-bearing, and both come from the deterministic candidate id:

* **Idempotency.** A second pass over the same events proposes nothing. The mechanism is the
  checkpoint, not the store's novelty filter: every materialized candidate records the event
  ids it consumed, and a consumed event never reaches the aggregator again. The novelty filter
  is only the backstop for a checkpoint that failed to write.
* **`dry_run` writes NOTHING anywhere** — no store write, no checkpoint, no compaction, no
  receipt event, no disposition flip. It reads the store and reports what a real pass would do.
"""

from contexer import candidates, evidence, lifecycle, store

# Kinds a candidate can be built out of. The rest of `evidence.EVENT_KINDS` is bookkeeping
# ABOUT candidates (`session_reconcile`, `candidate_disposition`, `policy_evaluation`), which
# never groups into one — and filtering it out here is what stops this function's OWN receipt
# event from making the next pass look like it has work to do, forever.
_EVIDENCE_KINDS = candidates.SEED_KINDS | candidates.SUPPORT_KINDS

# Candidate kinds that materialize into the LIFECYCLE lane rather than the content one. Both
# spell the same transition — "this decision should stop being live" — and `replace` differs
# only by naming what takes over, which rides along as `replacement_decision_id` (and makes the
# eventual lifecycle record read "superseded" instead of "retired").
_LIFECYCLE_KINDS = frozenset({"retire", "replace"})

# Lifecycle record kinds that mean "this decision was retired" (`lifecycle.lifecycle_record`).
# `restored` is deliberately absent: a restored decision is back in the live store, so it is
# not in the tombstone list `_retired_ids` reads at all.
_RETIRED_KINDS = frozenset({"retired", "superseded"})

# The session stamped on a materialized decision when its evidence names none. Real evidence
# carries the session that produced it, and that is what the entry records — this is only the
# fallback spelling for an event whose session was already "unknown".
_FALLBACK_SESSION = "reconcile"


def _receipt(dry_run: bool) -> dict:
    return {"events_observed": 0, "proposed": 0, "lifecycle_proposed": 0, "already_pending": 0,
            "duplicates": 0, "insufficient": 0, "incomplete": False, "dry_run": bool(dry_run)}


def _projected(entry: dict, tombstoned: bool) -> dict:
    """One store entry as the read-only projection `candidates.aggregate_candidates` expects."""
    return {
        "id": str(entry.get("id") or ""),
        "status": store.entry_status(entry),
        "tombstoned": tombstoned,
        "title": entry.get("title") or "",
        "content": entry.get("content") or "",
        "subtype": entry.get("subtype") or "",
        "source_files": entry.get("source_files") or [],
        "current_revision_id": entry.get("current_revision_id") or "",
    }


def _retired_ids(tombstones: list) -> set:
    """Ids of tombstoned decisions that carry a real RETIREMENT record.

    Presence in the sidecar is deliberately not enough. A tombstone written before lifecycle
    history existed records nothing about why the decision left, and only an actual retirement
    is a lifecycle proposal's approval — so the record, not the file, is the signal.
    """
    return {str(e.get("id") or "") for e in tombstones
            if any(isinstance(record, dict) and record.get("kind") in _RETIRED_KINDS
                   for record in (e.get("lifecycle") or []))}


def _dispositions(checkpoints: dict, entries: list, retired_ids: set) -> dict:
    """The status flips a pending checkpoint has earned since the last pass.

    Lazy on purpose: nothing hooks `approve_decision` or `retire_decision`, so a checkpoint
    learns its decision's fate the next time reconciliation runs. The rules, in the order they
    are tried:

    * the target was RETIRED — it is in the tombstone sidecar carrying a retirement record. For
      a lifecycle checkpoint that is precisely the outcome it proposed, so it reads `approved`;
      for a content checkpoint the decision left before anyone reviewed its wording, so it
      reads `dismissed`.
    * the entry is otherwise gone (evicted, or a legacy tombstone with no record) or `ignored`:
      `dismissed`, because nothing was reviewed.
    * LIFECYCLE lane: while the proposal still sits, the review has not happened; once it is
      gone from a still-live decision, it died unapproved — `dismissed`. **A revision advance is
      never an approval signal here** (ruling R25): retirement is a MOVE, so an unrelated
      content edit that happens to advance HEAD would otherwise record a retirement that never
      occurred — the same fabricated-approval class the `approved_by` rule below exists to
      prevent, and it would pollute the very disposition signal this pipeline measures.
    * CONTENT lane, checkpoint carrying a `revision_id` (it materialized as a PROPOSAL): while
      the proposal sits, stay pending. Once it is gone the revision answers what became of it —
      HEAD advanced means promoted (approved), HEAD unchanged means it died unapproved
      (dismissed). Sound here precisely because promotion IS the revision advance, which is what
      makes the same test unsound for a lifecycle proposal.
    * otherwise it is a brand-new pending entry, and only a real ratification stamp
      (`approved_by == "human"`, written by `_apply_approval`'s approve/edit paths) counts —
      approving one blesses revision 1 IN PLACE, so the revision rule above cannot see it.

    Anything else stays pending, which is also what keeps its evidence pinned against eviction
    until somebody actually reviews it.
    """
    by_id = {str(e.get("id") or ""): e for e in entries}
    flips = {}
    for candidate_id, checkpoint in sorted(checkpoints.items()):
        if checkpoint.get("status") != "pending":
            continue
        entry_id = str(checkpoint.get("entry_id") or "")
        # Named `lifecycle_lane`, never `lifecycle`: that name is the imported MODULE here.
        lifecycle_lane = checkpoint.get("lane") == "lifecycle"
        entry = by_id.get(entry_id)
        if entry_id in retired_ids:
            status = "approved" if lifecycle_lane else "dismissed"
        elif entry is None or store.entry_status(entry) == "ignored":
            status = "dismissed"
        elif lifecycle_lane:
            if entry.get("proposed_lifecycle"):
                continue
            status = "dismissed"
        elif checkpoint.get("revision_id"):
            if entry.get("proposed_revision"):
                continue
            status = ("approved"
                      if (entry.get("current_revision_id") or "") != checkpoint["revision_id"]
                      else "dismissed")
        elif entry.get("approved_by") == "human" and not entry.get("proposed_revision"):
            status = "approved"
        else:
            continue
        flips[candidate_id] = {**checkpoint, "status": status}
    return flips


def _materialize(repo_path: str, candidate: dict, sessions: dict, dry_run: bool,
                 receipt: dict, writes: dict) -> None:
    """Route ONE candidate: count it, recommend it, or store it. Fills `receipt`/`writes`."""
    kind = candidate.get("kind")
    candidate_id = candidate["candidate_id"]
    event_ids = [str(s.get("event_id") or "") for s in candidate.get("signals") or []]

    if kind == "insufficient":
        # No checkpoint: more evidence may still arrive for the same statement, and a
        # dismissal here would consume the events that would have completed it.
        receipt["insufficient"] += 1
        return
    if kind in _LIFECYCLE_KINDS:
        if dry_run:
            receipt["lifecycle_proposed"] += 1
            return
        target = str(candidate.get("target_decision_id") or "")
        result = lifecycle.propose_lifecycle(
            repo_path, target, "retire",
            f"inferred from session evidence: {candidate.get('title') or ''}".strip(),
            source="ai", replacement_id=candidate.get("replacement_decision_id"))
        if not result["ok"]:
            # Refused — the target is gone, no longer live, or a developer's own retirement
            # proposal holds the slot. Nothing awaits review, so the events are settled rather
            # than re-aggregated into the same refusal on every future pass. Deliberately
            # uncounted: no receipt line claims a proposal that does not exist, and inventing
            # a "refused" counter for a case the developer cannot act on would be noise.
            writes[candidate_id] = {"event_ids": event_ids, "status": "dismissed",
                                    "entry_id": target}
            return
        receipt["lifecycle_proposed"] += 1
        # `lane` is what `_dispositions` reads to tell an approved retirement (the target is
        # tombstoned with a retirement record) from a dismissed one, without ever inferring
        # either. Deliberately NO `revision_id`: a revision advance is not an approval signal
        # for this lane (ruling R25), so storing one would only invite a reader to use it.
        writes[candidate_id] = {"event_ids": event_ids, "status": "pending", "entry_id": target,
                                "lane": "lifecycle"}
        return
    if kind == "duplicate":
        # The store already holds this decision, so there is nothing to review — but the
        # events ARE settled, and a checkpoint is what stops them resurfacing every pass.
        # (No dry_run guard: `_reconcile` discards `writes` wholesale on a dry run, which is
        # the single gate — a second one here would be a second place to get it wrong.)
        receipt["duplicates"] += 1
        writes[candidate_id] = {"event_ids": event_ids, "status": "dismissed",
                                "entry_id": str(candidate.get("target_decision_id") or "")}
        return
    if dry_run:
        receipt["proposed"] += 1
        return

    # The ordinary capture path, deliberately: novelty filtering, revision construction,
    # capacity limits and the pending-review flow all apply, and `replace_id` lands an update
    # in the existing trust-ordered proposal slot without moving HEAD. `force_pending` applies
    # to a brand-new entry only: an INFERRED decision must never rest in the `suggested` tier,
    # which injects at session start but never appears in `review_pending`.
    stored, entry_id, _meta = store.update_decision_with_meta(
        repo_path,
        candidate.get("content") or "",
        _write_session(event_ids, sessions),
        candidate.get("subtype") or "",
        created_by="ai",
        replace_id=str(candidate.get("target_decision_id") or "") if kind == "update" else "",
        title=candidate.get("title") or "",
        force_pending=True,
    )
    if stored and entry_id:
        # Counted as proposed even in the one case where the store accepted the call but kept
        # a higher-trust proposal in the entry's single slot (`meta["refusal_ack"]`, #202): the
        # decision IS awaiting review, just not on this candidate's wording, and re-proposing
        # it every pass would be the worse answer. The receipt has no truer slot for it.
        # The checkpoint STATUS, though, is settled from what the store actually did rather
        # than from this optimism — see `_settle_write_statuses`.
        receipt["proposed"] += 1
        writes[candidate_id] = {"event_ids": event_ids, "status": "pending",
                                "entry_id": str(entry_id)}
        return
    # The store's own filter rejected it — an existing decision already says this. That is a
    # duplicate, not an error, and it settles the events exactly like a matched one.
    receipt["duplicates"] += 1
    writes[candidate_id] = {"event_ids": event_ids, "status": "dismissed", "entry_id": ""}


def _settle_write_statuses(repo_path: str, writes: dict) -> None:
    """Downgrade to `dismissed` every checkpoint whose entry is NOT actually awaiting review.

    The store's return says a write happened, not what kind. `update_decision_with_meta`
    answers `(True, entry_id, {})` identically for a brand-new pending entry, an attached
    `proposed_revision`, and a trivial correction it applied in place as a new approved
    revision — and only the first two leave anything for the developer to look at. A
    checkpoint claiming `pending` over the third would pin its evidence forever waiting on a
    review that will never be asked for. One re-read of the store settles it for the whole
    batch, which is also why this is not done per candidate inside the loop.

    The same read stamps `revision_id` on every CONTENT checkpoint that left a proposal behind,
    so a later pass can tell a promoted proposal (HEAD advanced) from a dismissed one (HEAD
    unchanged) — see `_dispositions`. Lifecycle checkpoints are skipped outright: their proposal
    is already attached (`propose_lifecycle` returning ok is what says so), and their lane
    settles on the tombstone, never on a revision.
    """
    pending_ids = {cid for cid, cp in writes.items()
                   if cp.get("status") == "pending" and cp.get("lane") != "lifecycle"}
    if not pending_ids:
        return
    by_id = {str(e.get("id") or ""): e for e in store.load(repo_path).get("entries", [])
             if isinstance(e, dict)}
    for candidate_id in pending_ids:
        checkpoint = writes[candidate_id]
        entry = by_id.get(str(checkpoint.get("entry_id") or ""))
        if entry is not None and entry.get("proposed_revision"):
            checkpoint["revision_id"] = entry.get("current_revision_id") or ""
        elif entry is None or store.entry_status(entry) != "pending_approval":
            checkpoint["status"] = "dismissed"


def _write_session(event_ids: list, sessions: dict) -> str:
    """The session a candidate's evidence came from — the truest provenance for the entry it
    becomes, and the reason the reconciling process's own id is never stamped here."""
    for event_id in event_ids:
        if sessions.get(event_id):
            return sessions[event_id]
    return _FALLBACK_SESSION


def _reconcile(repo_path: str, session_id: str, dry_run: bool, receipt: dict) -> dict:
    if not evidence.evidence_diagnostics(repo_path)["readable"]:
        # An unreadable ledger is not an empty one: propose nothing rather than re-propose
        # everything the checkpoints it holds have already settled.
        receipt["incomplete"] = True
        return receipt

    checkpoints = evidence.candidate_checkpoints(repo_path)
    events = [e for e in evidence.unconsumed_events(repo_path, session_id)
              if e.get("kind") in _EVIDENCE_KINDS]
    receipt["events_observed"] = len(events)
    if not events and not any(cp.get("status") == "pending" for cp in checkpoints.values()):
        # Fast path: nothing to aggregate and no checkpoint whose fate could have changed.
        # The store is not read at all, let alone locked — this runs at every session start.
        return receipt

    entries = [e for e in store.load(repo_path).get("entries", []) if isinstance(e, dict)]
    # `type == "decision"` on BOTH sides: the store holds tasks too, and a deleted task is no
    # more a decision to classify against than a live one is.
    tombstones = [e for e in store.load_deleted(repo_path).get("entries", [])
                  if isinstance(e, dict) and e.get("type") == "decision"]
    flips = {} if dry_run else _dispositions(checkpoints, entries, _retired_ids(tombstones))

    projection = [_projected(e, False) for e in entries if e.get("type") == "decision"]
    projection += [_projected(e, True) for e in tombstones]  # already decision-filtered above
    sessions = {str(e.get("event_id") or ""): str(e.get("session_id") or "") for e in events}

    writes: dict = {}
    for candidate in candidates.aggregate_candidates(events, projection)["candidates"]:
        if candidate["candidate_id"] in checkpoints:
            # Already settled (or already awaiting review) under its deterministic id. Belt to
            # the consumed-events braces: it also covers a checkpoint written by a pass whose
            # compaction never ran.
            receipt["already_pending"] += 1
            continue
        _materialize(repo_path, candidate, sessions, dry_run, receipt, writes)

    if dry_run:
        return receipt
    _settle_write_statuses(repo_path, writes)
    if evidence.record_candidate_checkpoints(repo_path, {**flips, **writes})["status"] != "ok":
        # The decisions are stored but their evidence still reads as unconsumed. Say so: the
        # next pass re-proposes and the store's novelty filter absorbs it.
        receipt["incomplete"] = True
    compacted = evidence.compact_evidence(repo_path)["compacted"]
    if not (writes or flips or compacted):
        # Nothing happened, so nothing is recorded. A pass that appended its receipt
        # unconditionally would write one event per SessionStart, PreCompact and SessionEnd
        # forever on any repo holding a single stuck pending checkpoint — filling the ledger
        # toward eviction with news of having done nothing.
        return receipt
    evidence.emit_hook_event(
        repo_path, "session_reconcile", session_id=session_id or _FALLBACK_SESSION,
        source="reconcile_session",
        summary=(f"reconciled {receipt['events_observed']} evidence event(s): "
                 f"{receipt['proposed']} proposed, {receipt['duplicates']} duplicate, "
                 f"{receipt['insufficient']} insufficient, "
                 f"{receipt['lifecycle_proposed']} retirement(s) proposed"),
        attributes={"events_observed": receipt["events_observed"],
                    "proposed": receipt["proposed"],
                    "lifecycle_proposed": receipt["lifecycle_proposed"],
                    "already_pending": receipt["already_pending"],
                    "duplicates": receipt["duplicates"],
                    "insufficient": receipt["insufficient"],
                    "incomplete": receipt["incomplete"]})
    return receipt


def reconcile_session(repo_path: str, session_id: str = "", dry_run: bool = False) -> dict:
    """Materialize this repo's unconsumed evidence as decisions pending review.

    `session_id` scopes which events participate (`""` = the whole ledger, which is what a
    worktree-shared sidecar needs). Returns the receipt:

        {"events_observed", "proposed", "lifecycle_proposed", "already_pending", "duplicates",
         "insufficient", "incomplete", "dry_run"}

    NEVER raises: every caller is a host hook or a report surface, and a reconciliation that
    could not finish is a receipt marked `incomplete`, never a broken session start.
    """
    receipt = _receipt(dry_run)
    try:
        return _reconcile(repo_path, session_id, dry_run, receipt)
    except Exception:                  # broad on purpose: the never-raises contract
        receipt["incomplete"] = True
        return receipt


def format_receipt(receipt: dict) -> str:
    """The receipt as human-readable lines — shared by the CLI command and the MCP tool so a
    developer and a model are never told two different stories about one pass."""
    head = "Reconciled evidence" + (" (dry run — nothing was written)" if receipt["dry_run"]
                                    else "")
    lines = [
        f"{head}:",
        f"  evidence events observed: {receipt['events_observed']}",
        f"  proposed for review:      {receipt['proposed']}",
        f"  retirements proposed:     {receipt['lifecycle_proposed']}",
        f"  already pending:          {receipt['already_pending']}",
        f"  duplicates:               {receipt['duplicates']}",
        f"  insufficient evidence:    {receipt['insufficient']}",
    ]
    if receipt["lifecycle_proposed"]:
        lines.append("  (proposals only — nothing was retired. `contexer review` shows each "
                     "one; retiring is an explicit `contexer retire <id>`.)")
    if receipt["incomplete"]:
        lines.append("  incomplete: the evidence ledger could not be fully read or updated.")
    return "\n".join(lines)
