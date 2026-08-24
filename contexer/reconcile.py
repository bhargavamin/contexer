"""Turn unconsumed evidence into decisions a developer REVIEWS.

The one coordinator in the evidence pipeline: it reads the ledger (`evidence`), scores what
it finds (`candidates`, pure), and materializes the result through the store's ordinary
capture path. Nothing here approves, retires, or trusts anything — a proposal lands
`pending_approval` (or as a `proposed_revision` on its target) and the existing review flow
is the only gate. Retirement stays a READ-ONLY recommendation in the receipt.

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

from contexer import candidates, evidence, store

# Kinds a candidate can be built out of. The rest of `evidence.EVENT_KINDS` is bookkeeping
# ABOUT candidates (`session_reconcile`, `candidate_disposition`, `policy_evaluation`), which
# never groups into one — and filtering it out here is what stops this function's OWN receipt
# event from making the next pass look like it has work to do, forever.
_EVIDENCE_KINDS = candidates.SEED_KINDS | candidates.SUPPORT_KINDS

# Candidate kinds that stay read-only recommendations (the plan's Phase 2 exit gate). Their
# events stay UNCONSUMED: nothing was decided about them, so a later pass must see them again.
_RECOMMEND_ONLY = frozenset({"retire", "replace"})

# The session stamped on a materialized decision when its evidence names none. Real evidence
# carries the session that produced it, and that is what the entry records — this is only the
# fallback spelling for an event whose session was already "unknown".
_FALLBACK_SESSION = "reconcile"


def _receipt(dry_run: bool) -> dict:
    return {"events_observed": 0, "proposed": 0, "already_pending": 0, "duplicates": 0,
            "insufficient": 0, "retire_recommendations": [], "incomplete": False,
            "dry_run": bool(dry_run)}


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


def _dispositions(checkpoints: dict, entries: list) -> dict:
    """The status flips a pending checkpoint has earned since the last pass.

    Lazy on purpose: nothing hooks `approve_decision`: a checkpoint learns its decision's fate
    the next time reconciliation runs. An entry that is GONE from the live store (deleted, or
    ignored) settles as `dismissed`; one still awaiting the developer — `pending_approval`, or
    carrying an unreviewed `proposed_revision` — stays pending, which is also what keeps its
    evidence pinned against eviction until somebody actually reviews it.
    """
    by_id = {str(e.get("id") or ""): e for e in entries}
    flips = {}
    for candidate_id, checkpoint in sorted(checkpoints.items()):
        if checkpoint.get("status") != "pending":
            continue
        entry = by_id.get(str(checkpoint.get("entry_id") or ""))
        if entry is None or store.entry_status(entry) == "ignored":
            status = "dismissed"
        elif store.entry_status(entry) == "pending_approval" or entry.get("proposed_revision"):
            continue
        else:
            status = "approved"
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
    if kind in _RECOMMEND_ONLY:
        receipt["retire_recommendations"].append({
            "candidate_id": candidate_id,
            "target_decision_id": candidate.get("target_decision_id"),
            "title": candidate.get("title") or "",
        })
        return
    if kind == "duplicate":
        # The store already holds this decision, so there is nothing to review — but the
        # events ARE settled, and a checkpoint is what stops them resurfacing every pass.
        receipt["duplicates"] += 1
        if not dry_run:
            writes[candidate_id] = {"event_ids": event_ids, "status": "dismissed",
                                    "entry_id": str(candidate.get("target_decision_id") or "")}
        return
    if dry_run:
        receipt["proposed"] += 1
        return

    # The ordinary capture path, deliberately: novelty filtering, revision construction,
    # capacity limits and the pending-review flow all apply, and `replace_id` lands an update
    # in the existing trust-ordered proposal slot without moving HEAD.
    stored, entry_id, _meta = store.update_decision_with_meta(
        repo_path,
        candidate.get("content") or "",
        _write_session(event_ids, sessions),
        candidate.get("subtype") or "",
        created_by="ai",
        replace_id=str(candidate.get("target_decision_id") or "") if kind == "update" else "",
        title=candidate.get("title") or "",
    )
    if stored and entry_id:
        # Counted as proposed even in the one case where the store accepted the call but kept
        # a higher-trust proposal in the entry's single slot (`meta["refusal_ack"]`, #202): the
        # decision IS awaiting review, just not on this candidate's wording, and re-proposing
        # it every pass would be the worse answer. The receipt has no truer slot for it.
        receipt["proposed"] += 1
        writes[candidate_id] = {"event_ids": event_ids, "status": "pending",
                                "entry_id": str(entry_id)}
        return
    # The store's own filter rejected it — an existing decision already says this. That is a
    # duplicate, not an error, and it settles the events exactly like a matched one.
    receipt["duplicates"] += 1
    writes[candidate_id] = {"event_ids": event_ids, "status": "dismissed", "entry_id": ""}


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
    tombstones = [e for e in store.load_deleted(repo_path).get("entries", [])
                  if isinstance(e, dict)]
    flips = {} if dry_run else _dispositions(checkpoints, entries)

    projection = [_projected(e, False) for e in entries if e.get("type") == "decision"]
    projection += [_projected(e, True) for e in tombstones]
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
    if evidence.record_candidate_checkpoints(repo_path, {**flips, **writes})["status"] != "ok":
        # The decisions are stored but their evidence still reads as unconsumed. Say so: the
        # next pass re-proposes and the store's novelty filter absorbs it.
        receipt["incomplete"] = True
    evidence.compact_evidence(repo_path)
    evidence.emit_hook_event(
        repo_path, "session_reconcile", session_id=session_id or _FALLBACK_SESSION,
        source="reconcile_session",
        summary=(f"reconciled {receipt['events_observed']} evidence event(s): "
                 f"{receipt['proposed']} proposed, {receipt['duplicates']} duplicate, "
                 f"{receipt['insufficient']} insufficient, "
                 f"{len(receipt['retire_recommendations'])} retirement recommendation(s)"),
        attributes={"events_observed": receipt["events_observed"],
                    "proposed": receipt["proposed"],
                    "already_pending": receipt["already_pending"],
                    "duplicates": receipt["duplicates"],
                    "insufficient": receipt["insufficient"],
                    "retire_recommendations": len(receipt["retire_recommendations"]),
                    "incomplete": receipt["incomplete"]})
    return receipt


def reconcile_session(repo_path: str, session_id: str = "", dry_run: bool = False) -> dict:
    """Materialize this repo's unconsumed evidence as decisions pending review.

    `session_id` scopes which events participate (`""` = the whole ledger, which is what a
    worktree-shared sidecar needs). Returns the receipt:

        {"events_observed", "proposed", "already_pending", "duplicates", "insufficient",
         "retire_recommendations": [{"candidate_id", "target_decision_id", "title"}, ...],
         "incomplete", "dry_run"}

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
        f"  already pending:          {receipt['already_pending']}",
        f"  duplicates:               {receipt['duplicates']}",
        f"  insufficient evidence:    {receipt['insufficient']}",
    ]
    for recommendation in receipt["retire_recommendations"]:
        target = (recommendation.get("target_decision_id") or "")[:8]
        lines.append(f"  retirement suggested for {target}: {recommendation.get('title', '')}")
    if receipt["retire_recommendations"]:
        lines.append("  (retirements are recommendations only — nothing was retired.)")
    if receipt["incomplete"]:
        lines.append("  incomplete: the evidence ledger could not be fully read or updated.")
    return "\n".join(lines)
