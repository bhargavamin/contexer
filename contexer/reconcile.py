"""Turn unconsumed evidence into decisions a developer REVIEWS.

The one coordinator in the evidence pipeline: it reads the spool (`spool`), scores what it
finds (`candidates`, pure), and materializes the result through the store's ordinary capture
path. Nothing here approves, retires, or trusts anything, and every decision it creates is
REVIEWABLE by construction:

* a new candidate lands `pending_approval` - `force_pending`, never the `suggested` tier an
  ai capture would otherwise get, because `suggested` injects at session start yet never
  appears in `review_pending`: trusted without ever having been offered for review;
* an update lands as a `proposed_revision` on its target, HEAD unmoved until approval;
* a retirement or replacement lands as a `proposed_lifecycle` on its target - a proposal in the
  separate lifecycle lane, which retires nothing: only an explicit `lifecycle.retire_decision` by a
  human moves a decision out of active context.

The same discipline governs the way back out: a candidate is only ever settled `approved`
against real review evidence - a human ratification stamp, a content proposal whose revision
actually advanced, or a decision genuinely retired into the tombstone sidecar - never inferred
from an entry merely having stopped being pending, and never from a revision advance the
candidate's own lane cannot be caused by.

Above store rather than beside it: `store.py` never imports this module, and store-owned
helpers are read through the store MODULE OBJECT at call time - the load-order discipline
`guard_engine.py` documents, so a value a test patches on `contexer.store` is seen here.

Two properties are load-bearing, and both come from the deterministic candidate id:

* **Idempotency.** A second pass over the same events proposes nothing. The mechanism is the
  HOLD, not the store's novelty filter: a materialized candidate's events are moved out of
  `pending/` into `held/<candidate-id>/`, so they never reach the aggregator again, and the
  directory itself is the record that this candidate is already awaiting review. The novelty
  filter is only the backstop for a hold that failed to complete.
* **`dry_run` writes NOTHING anywhere** - no store write, no hold, no finalize, no retention,
  no receipt line, no disposition. It reads the store and reports what a real pass would do.

The receipt of a pass is LOGGED, never spooled (ruling R34): `.reconcile_<slug>.jsonl`, the
`.retrieval_<slug>.jsonl` precedent. Bookkeeping in the evidence spool is bookkeeping that
retention eventually reports as lost evidence - see `_log_receipt`.

ORDER MATTERS on the way in (revised plan B3 step 8): materialize FIRST, then move. A crash
between the two leaves a candidate whose decision exists and whose events are split across
`pending/` and `held/`; the next pass finishes the move from the candidate's own recorded
`event_ids` (`_finish_interrupted_holds`) rather than re-aggregating the remainder into a
second candidate under a different id.
"""

import fcntl
import json
from contextlib import contextmanager
from datetime import datetime, timezone

from contexer import candidates, lifecycle, spool, store

# Kinds a candidate can be built out of. The rest of `evidence.EVENT_KINDS` is bookkeeping
# ABOUT candidates (`policy_evaluation`, `session_reconcile`), which never groups into one, so
# a spooled one must never make the next pass look like it has work to do.
_EVIDENCE_KINDS = candidates.SEED_KINDS | candidates.SUPPORT_KINDS

# Tail cap on the reconciliation log, matching `store._RETRIEVAL_LOG_CAP` - the same kind of
# record, kept for the same reason, so it is bounded the same way.
_RECEIPT_LOG_CAP = 200

# Candidate kinds that materialize into the LIFECYCLE lane rather than the content one. Both
# spell the same transition - "this decision should stop being live" - and `replace` differs
# only by naming what takes over, which rides along as `replacement_decision_id` (and makes the
# eventual lifecycle record read "superseded" instead of "retired").
_LIFECYCLE_KINDS = frozenset({"retire", "replace"})

# Lifecycle record kinds that mean "this decision was retired". DERIVED, never respelled:
# `lifecycle.py` owns the vocabulary it writes, and a local copy here would keep passing its
# own tests while a rename there silently emptied this set. `restored` is excluded at the
# source - a restored decision is back in the live store, so it is not in the tombstone list
# `_retired_ids` reads at all.
_RETIRED_KINDS = lifecycle.RETIRED_KINDS

# The session stamped on a materialized decision when its evidence names none. Real evidence
# carries the session that produced it, and that is what the entry records - this is only the
# fallback spelling for an event whose session was already "unknown".
_FALLBACK_SESSION = "reconcile"


def _receipt(dry_run: bool) -> dict:
    return {"events_observed": 0, "proposed": 0, "lifecycle_proposed": 0, "already_pending": 0,
            "duplicates": 0, "insufficient": 0, "incomplete": False, "skipped": False,
            "dry_run": bool(dry_run)}


@contextmanager
def _reconcile_lock(repo_path: str):
    """Yield True to the one pass that holds this repo's reconcile lock, False to any pass
    that finds it already held. NEVER waits.

    Two concurrent passes over one spool can record a disposition that never happened: each
    snapshots the live decisions before classifying, so a store write landing between B's
    snapshot and B's classification changes the `kind`/`target_decision_id` B computes for the
    SAME events, hence their `candidate_id` - and B then holds events A already moved, sees
    them `missing`, and files an `evidence_summary` under its own entry describing a
    disposition nobody made. Claude and Gemini both reconcile at SessionStart, PreCompact and
    SessionEnd, so two sessions on one repo is ordinary, not exotic.

    THIS DOES NOT VIOLATE THE SPOOL'S "no locks anywhere" RULE. That rule governs EVIDENCE
    WRITES FROM EDITOR HOOKS (`spool.append_evidence`), which must never wait behind another
    writer. Reconciliation is neither an editor hook nor an evidence write: it is the scanning
    consumer, it already takes the store lock to write, and skipping it costs nothing because
    the next checkpoint picks the work up. Do not "fix" this back out.

    `flock` and not a lock FILE's existence: a crashed pass releases it with its fd, where a
    stale marker would wedge reconciliation on this repo for good. Failing to open the lock at
    all fails OPEN (yield True) - an unwritable STORE_DIR must not silently disable the
    pipeline, and it is the contention case, not the I/O case, this exists to answer.
    """
    path = store.STORE_DIR / f".reconcile_{store.repo_slug(repo_path)}.lock"
    try:
        store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        handle = open(path, "a")        # noqa: SIM115 - closed in the finally below
    except OSError:
        yield True
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:             # held by another pass: skip, never wait
        handle.close()
        yield False
        return
    except OSError:                     # no flock on this filesystem: fail open, as above
        handle.close()
        yield True
        return
    try:
        yield True
    finally:
        handle.close()                  # closing the fd releases the flock


def _hold(repo_path: str, candidate_id: str, event_ids: list, meta: dict | None = None) -> bool:
    """Move a candidate's events into its hold. True only when every named event is verifiably
    THERE.

    `spool.hold_candidate_evidence` reports rather than raises: a source gone with no target
    lands in `missing`, and its docstring hands the decision to the caller. Deciding is what
    this is - `missing` means those events were never verified as moved (evicted, or already
    claimed by a concurrent pass), so the caller must not go on to record a clean disposition
    over them.
    """
    result = spool.hold_candidate_evidence(repo_path, candidate_id, event_ids, meta=meta)
    return result["status"] == "ok" and not result["missing"]


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
    is a lifecycle proposal's approval - so the record, not the file, is the signal.
    """
    return {str(e.get("id") or "") for e in tombstones
            if any(isinstance(record, dict) and record.get("kind") in _RETIRED_KINDS
                   for record in (e.get("lifecycle") or []))}


def _dispositions(held: dict, entries: list, retired_ids: set) -> dict:
    """The disposition each held candidate has earned since the last pass,
    `{candidate_id: "approved"|"dismissed"}`.

    The INPUT is the held directories and their `candidate.json` bookkeeping; a held directory
    IS the pending state, so there is no status to filter on - a settled candidate's directory
    is gone. The RULES are unchanged, and lazy on purpose: nothing hooks `approve_decision` or
    `retire_decision`, so a candidate learns its decision's fate the next time reconciliation
    runs. In the order they are tried:

    * the target was RETIRED - it is in the tombstone sidecar carrying a retirement record. For
      a lifecycle candidate that is precisely the outcome it proposed, so it reads `approved`;
      for a content candidate the decision left before anyone reviewed its wording, so it
      reads `dismissed`.
    * the entry is otherwise gone (evicted, or a legacy tombstone with no record) or `ignored`:
      `dismissed`, because nothing was reviewed.
    * LIFECYCLE lane: while the proposal still sits, the review has not happened; once it is
      gone from a still-live decision, it died unapproved - `dismissed`. **A revision advance is
      never an approval signal here** (ruling R25): retirement is a MOVE, so an unrelated
      content edit that happens to advance HEAD would otherwise record a retirement that never
      occurred - the same fabricated-approval class the `approved_by` rule below exists to
      prevent, and it would pollute the very disposition signal this pipeline measures.
    * CONTENT lane, meta carrying a `revision_id` (it materialized as a PROPOSAL): while
      the proposal sits, stay held. Once it is gone the revision answers what became of it -
      HEAD advanced means promoted (approved), HEAD unchanged means it died unapproved
      (dismissed). Sound here precisely because promotion IS the revision advance, which is what
      makes the same test unsound for a lifecycle proposal.
    * otherwise it is a brand-new pending entry, and only a real ratification stamp
      (`approved_by == "human"`, written by `_apply_approval`'s approve/edit paths) counts -
      approving one blesses revision 1 IN PLACE, so the revision rule above cannot see it.

    Anything else stays held, which is also what keeps its evidence exempt from retention until
    somebody actually reviews it. A candidate whose meta records no `entry_id` is left held
    too: there is nothing to judge it against, and guessing is what
    `spool.evidence_diagnostics`' `held_unattributed` counter exists to surface instead.
    """
    by_id = {str(e.get("id") or ""): e for e in entries}
    flips = {}
    for candidate_id, meta in sorted(held.items()):
        settled = str(meta.get("status") or "")
        # Tested against the spool's own vocabulary, not merely `!= "pending"`: a hand-edited
        # `candidate.json` would otherwise reach `finalize_candidate_evidence` with a status it
        # rejects, and that ValueError aborts the WHOLE pass - on every run, for good.
        if settled in spool.DISPOSITIONS:
            # This candidate was ALREADY settled when it was held - a duplicate, or an update
            # the store applied in place - and only the finalize that would have removed the
            # directory is missing. Its disposition is what it was decided to be, not what the
            # rules below would infer: without this, a crash between the hold and the finalize
            # left a duplicate carrying an `entry_id` and no `lane`, and the last rule below
            # read its target's `approved_by == "human"` as an APPROVAL of a candidate that was
            # dismissed on arrival - the fabricated-approval class this whole function guards.
            flips[candidate_id] = settled
            continue
        entry_id = str(meta.get("entry_id") or "")
        if not entry_id:
            # ponytail: no `entry_id` means the meta was never written or cannot be read, so
            # nothing can judge this candidate and its directory keeps the candidate id
            # occupied - the same evidence re-aggregates to the same id and reads as
            # `already_pending` forever. Accepted rather than guessed at: the only honest
            # signal is `evidence_diagnostics`' `held_unattributed` count, surfaced by
            # `contexer status`. A real fix needs the hold to be atomic with its meta.
            continue
        # Named `lifecycle_lane`, never `lifecycle`: that name is the imported MODULE here.
        lifecycle_lane = meta.get("lane") == "lifecycle"
        entry = by_id.get(entry_id)
        if entry_id in retired_ids:
            status = "approved" if lifecycle_lane else "dismissed"
        elif entry is None or store.entry_status(entry) == "ignored":
            status = "dismissed"
        elif lifecycle_lane:
            if entry.get("proposed_lifecycle"):
                continue
            status = "dismissed"
        elif meta.get("revision_id"):
            if entry.get("proposed_revision"):
                continue
            status = ("approved"
                      if (entry.get("current_revision_id") or "") != meta["revision_id"]
                      else "dismissed")
        elif entry.get("approved_by") == "human" and not entry.get("proposed_revision"):
            status = "approved"
        else:
            continue
        flips[candidate_id] = status
    return flips


def _materialize(repo_path: str, candidate: dict, sessions: dict, dry_run: bool,
                 receipt: dict, writes: dict) -> None:
    """Route ONE candidate: count it, recommend it, or store it. Fills `receipt`/`writes`.

    A `writes` entry is the candidate's intended bookkeeping - `event_ids`, the `status` it
    settles at (`pending` = awaiting review, anything else = settled in this same run), the
    `entry_id` it belongs to, and the lane/revision the disposition rules read. `_commit_writes`
    is what turns it into a held directory, so nothing here touches the spool.
    """
    kind = candidate.get("kind")
    candidate_id = candidate["candidate_id"]
    event_ids = [str(s.get("event_id") or "") for s in candidate.get("signals") or []]

    if kind == "insufficient":
        # No hold: more evidence may still arrive for the same statement, and a
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
            # Refused - the target is gone, no longer live, or a developer's own retirement
            # proposal holds the slot. Nothing awaits review, so the events are settled rather
            # than re-aggregated into the same refusal on every future pass. Deliberately
            # uncounted: no receipt line claims a proposal that does not exist, and inventing
            # a "refused" counter for a case the developer cannot act on would be noise.
            writes[candidate_id] = {"event_ids": event_ids, "status": "dismissed",
                                    "entry_id": target}
            return
        receipt["lifecycle_proposed"] += 1
        # `lane` is what `_dispositions` reads (off the held dir's `candidate.json`) to tell an
        # approved retirement (the target is tombstoned with a retirement record) from a
        # dismissed one, without ever inferring either. Deliberately NO `revision_id`: a
        # revision advance is not an approval signal for this lane (ruling R25), so storing one
        # would only invite a reader to use it.
        writes[candidate_id] = {"event_ids": event_ids, "status": "pending", "entry_id": target,
                                "lane": "lifecycle"}
        return
    if kind == "duplicate":
        # The store already holds this decision, so there is nothing to review - but the events
        # ARE settled, so they are held and finalized in this SAME run (red-team mitigation 2),
        # with the summary attached to the decision they matched. Leaving them in `pending/`
        # would re-aggregate this duplicate at every checkpoint forever and permanently defeat
        # the fast path. (No dry_run guard needed HERE: this branch only fills `writes`, and
        # `_reconcile` discards that wholesale on a dry run - a second check would be a second
        # place to get it wrong. The lifecycle branch above is the one that must guard itself,
        # because `propose_lifecycle` writes the store DIRECTLY rather than through `writes`,
        # so the wholesale discard cannot reach it. Any future lane that writes outside
        # `writes` inherits that obligation.)
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
        # The recorded STATUS, though, is settled from what the store actually did rather
        # than from this optimism - see `_settle_write_statuses`.
        receipt["proposed"] += 1
        writes[candidate_id] = {"event_ids": event_ids, "status": "pending",
                                "entry_id": str(entry_id)}
        return
    # The store's own filter rejected it - an existing decision already says this. That is a
    # duplicate, not an error, and it settles the events exactly like a matched one.
    receipt["duplicates"] += 1
    writes[candidate_id] = {"event_ids": event_ids, "status": "dismissed", "entry_id": ""}


def _settle_write_statuses(repo_path: str, writes: dict) -> None:
    """Downgrade to `dismissed` every record whose entry is NOT actually awaiting review.

    The store's return says a write happened, not what kind. `update_decision_with_meta`
    answers `(True, entry_id, {})` identically for a brand-new pending entry, an attached
    `proposed_revision`, and a trivial correction it applied in place as a new approved
    revision - and only the first two leave anything for the developer to look at. A record
    claiming `pending` over the third would hold its evidence forever waiting on a review that
    will never be asked for. One re-read of the store settles it for the whole batch, which is
    also why this is not done per candidate inside the loop.

    The same read stamps `revision_id` on every CONTENT record that left a proposal behind,
    so a later pass can tell a promoted proposal (HEAD advanced) from a dismissed one (HEAD
    unchanged) - see `_dispositions`. Lifecycle records are skipped outright: their proposal
    is already attached (`propose_lifecycle` returning ok is what says so), and their lane
    settles on the tombstone, never on a revision.
    """
    pending_ids = {cid for cid, record in writes.items()
                   if record.get("status") == "pending" and record.get("lane") != "lifecycle"}
    if not pending_ids:
        return
    by_id = {str(e.get("id") or ""): e for e in store.load(repo_path).get("entries", [])
             if isinstance(e, dict)}
    for candidate_id in pending_ids:
        record = writes[candidate_id]
        entry = by_id.get(str(record.get("entry_id") or ""))
        if entry is not None and entry.get("proposed_revision"):
            record["revision_id"] = entry.get("current_revision_id") or ""
        elif entry is None or store.entry_status(entry) != "pending_approval":
            record["status"] = "dismissed"


def _finish_interrupted_holds(repo_path: str, events: list, held: dict, dry_run: bool,
                              receipt: dict) -> list:
    """Drop every event an existing candidate already claims, finishing a half-done hold.

    Materializing before moving (B3 step 8) means a crash between the two leaves a candidate
    whose decision exists and whose events are split across `pending/` and `held/`. Aggregating
    the remainder would mint a DIFFERENT candidate id - the seed is the sorted event ids - and
    propose the same decision a second time, so the leftovers are recognized by the held
    candidate's own recorded `event_ids` and moved into the directory that already claims them.

    A dry run excludes them from aggregation exactly the same way (that is what a real pass
    would do) but moves nothing, because a dry run writes nothing anywhere.

    Scoped to the events this pass can SEE. A claimed event whose session this pass filtered
    out is not in its listing at all, so it can neither be aggregated here nor recovered here;
    the pass that does see it finishes the move.
    """
    claimed = {str(event_id): candidate_id
               for candidate_id, meta in sorted(held.items())
               for event_id in (meta.get("event_ids") or [])}
    if not claimed:
        return events
    kept, stray = [], {}
    for event in events:
        candidate_id = claimed.get(str(event.get("event_id") or ""))
        if candidate_id is None:
            kept.append(event)
        else:
            stray.setdefault(candidate_id, []).append(str(event.get("event_id") or ""))
    for candidate_id, event_ids in sorted(stray.items()):
        # No `meta`: the directory already describes this candidate, and rewriting the
        # bookkeeping from a recovery pass could only ever replace it with less.
        if not dry_run and not _hold(repo_path, candidate_id, event_ids):
            receipt["incomplete"] = True
    return kept


def _finalize(repo_path: str, candidate_id: str, disposition: str, entry_id: str,
              receipt: dict) -> None:
    """Settle one candidate: delete its held events and preserve the summary on its decision.

    The summary is the whole point of finalizing rather than deleting - once the raw events are
    gone, the decision's own `evidence_summary` history is where the disposition lives. A
    candidate with no entry (the store's novelty filter rejected it, naming nothing) still
    finalizes: its events are settled either way, there is simply nowhere to file the receipt.
    """
    summary = spool.finalize_candidate_evidence(repo_path, candidate_id, disposition)
    if not entry_id:
        return
    try:
        store.record_evidence_summary(repo_path, entry_id, summary)
    except Exception:
        # The candidate is settled and its events are already gone; only the receipt was lost,
        # and one entry's failure must not abandon the rest of the batch mid-loop.
        receipt["incomplete"] = True


def _commit_writes(repo_path: str, writes: dict, receipt: dict) -> None:
    """Hold every materialized candidate's events, and finalize the ones already settled.

    The record is written to the hold's `candidate.json` WHOLE, `status` included: a candidate
    that is settled on arrival (a duplicate, or an update the store applied in place) is
    finalized on the next line, and if that never happens the recorded status is the only thing
    that tells a resumed pass what this hold already was. See `_dispositions`' first rule.

    An INCOMPLETE hold is never finalized. Finalizing writes an `evidence_summary` onto the
    decision saying this event set was settled as `dismissed`/`approved`, and a hold that
    reported `missing` or `failed` never verified that the events are where the summary claims
    they are - so a clean disposition over them is a receipt for something that did not happen.
    The recorded status stays on the hold, and `_dispositions`' first rule settles it on a
    later pass, when the events can actually be accounted for.
    """
    for candidate_id, record in sorted(writes.items()):
        complete = _hold(repo_path, candidate_id, record["event_ids"], meta=record)
        if not complete:
            # The decision is stored but some of its evidence still reads as pending, or was
            # not found at all. Say so: the next pass finishes the move
            # (`_finish_interrupted_holds`) rather than proposing it again, but this pass did
            # not do everything it set out to.
            receipt["incomplete"] = True
        if record["status"] != "pending" and complete:
            _finalize(repo_path, candidate_id, record["status"],
                      str(record.get("entry_id") or ""), receipt)


def _log_receipt(repo_path: str, session_id: str, receipt: dict) -> None:
    """Append one JSON line to this repo's reconciliation log, tail-capped. Fail-soft.

    A receipt is BOOKKEEPING about the pipeline, not evidence for a decision, and ruling R34
    is that it must not live in the evidence spool: nothing reads the `session_reconcile` kind,
    yet a receipt spooled into `pending/` was never held, so it aged out through retention -
    which counts every drop in `.gap`, making `contexer status` report "N events lost" on a
    repo that lost nothing, in the one surface built to be honest about loss. It also spent the
    pending budget that real evidence needs. Filtering the kind inside retention was rejected:
    retention must never parse event content.

    So it goes where this repo already puts log-only records - the `.retrieval_<slug>.jsonl`
    precedent: never user-facing, tail-capped, and fail-soft including the read-back, since a
    log that picked up non-UTF-8 bytes must not break a session start over a bookkeeping file.
    """
    path = store.STORE_DIR / f".reconcile_{store.repo_slug(repo_path)}.jsonl"
    try:
        store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        lines.append(json.dumps({**receipt, "session_id": session_id or _FALLBACK_SESSION,
                                 "at": datetime.now(timezone.utc).isoformat()}))
        store.atomic_write(path, "\n".join(lines[-_RECEIPT_LOG_CAP:]) + "\n")
    except (OSError, ValueError):       # ValueError covers UnicodeDecodeError and json both
        pass


def _write_session(event_ids: list, sessions: dict) -> str:
    """The session a candidate's evidence came from - the truest provenance for the entry it
    becomes, and the reason the reconciling process's own id is never stamped here."""
    for event_id in event_ids:
        if sessions.get(event_id):
            return sessions[event_id]
    return _FALLBACK_SESSION


def _reconcile(repo_path: str, session_id: str, dry_run: bool, receipt: dict) -> dict:
    # No readability gate any more, and none is needed: settling a candidate MOVES its events
    # out of `pending/`, so a spool that reads as empty can no longer cause the failure the old
    # ledger's gate existed for (re-proposing everything its checkpoints had already settled).
    # An unreadable spool is reported where a developer can act on it - `contexer status`, off
    # `spool.evidence_diagnostics`' own `readable` flag - rather than costing a pass here.
    events = [e for e in spool.list_pending_evidence(repo_path, session_id)
              if e.get("kind") in _EVIDENCE_KINDS]
    held = spool.held_candidates(repo_path)
    if not events and not held:
        # Fast path: nothing to aggregate and no held candidate whose fate could have changed.
        # Two directory listings; the store is not read at all, let alone locked - this runs at
        # every session start.
        return receipt
    events = _finish_interrupted_holds(repo_path, events, held, dry_run, receipt)
    # ponytail: counted AFTER the recovery strips events an existing candidate already claims,
    # so `events_observed` means "events this pass aggregated", not "files in pending/". The
    # checkpoint era's number was the same thing by a different route (a consumed event never
    # reached the reader at all); recovering a half-done hold is the only case where the two
    # spellings could differ, and reporting the leftovers as observed would double-count the
    # evidence of a candidate that is already awaiting review.
    receipt["events_observed"] = len(events)

    entries = [e for e in store.load(repo_path).get("entries", []) if isinstance(e, dict)]
    # `type == "decision"` on BOTH sides: the store holds tasks too, and a deleted task is no
    # more a decision to classify against than a live one is.
    tombstones = [e for e in store.load_deleted(repo_path).get("entries", [])
                  if isinstance(e, dict) and e.get("type") == "decision"]
    flips = {} if dry_run else _dispositions(held, entries, _retired_ids(tombstones))

    projection = [_projected(e, False) for e in entries if e.get("type") == "decision"]
    projection += [_projected(e, True) for e in tombstones]  # already decision-filtered above
    sessions = {str(e.get("event_id") or ""): str(e.get("session_id") or "") for e in events}

    writes: dict = {}
    for candidate in candidates.aggregate_candidates(events, projection)["candidates"]:
        if candidate["candidate_id"] in held:
            # Already awaiting review under its deterministic id. Belt to the held-events
            # braces: it also covers a candidate whose directory exists but whose bookkeeping
            # never recorded the event ids `_finish_interrupted_holds` matches on.
            receipt["already_pending"] += 1
            continue
        _materialize(repo_path, candidate, sessions, dry_run, receipt, writes)

    if dry_run:
        return receipt
    _settle_write_statuses(repo_path, writes)
    _commit_writes(repo_path, writes, receipt)
    for candidate_id, disposition in sorted(flips.items()):
        _finalize(repo_path, candidate_id, disposition,
                  str(held[candidate_id].get("entry_id") or ""), receipt)
    spool.run_retention(repo_path)
    if not (writes or flips):
        # Nothing happened, so nothing is recorded. A pass that logged its receipt
        # unconditionally would write one line per SessionStart, PreCompact and SessionEnd
        # forever on any repo holding a single stuck held candidate - news of having done
        # nothing, crowding the tail cap that holds the passes that did something.
        return receipt
    _log_receipt(repo_path, session_id, receipt)
    return receipt


def reconcile_session(repo_path: str, session_id: str = "", dry_run: bool = False) -> dict:
    """Materialize this repo's unconsumed evidence as decisions pending review.

    `session_id` scopes which events participate (`""` = the whole spool, which is what a
    worktree-shared spool needs). Returns the receipt:

        {"events_observed", "proposed", "lifecycle_proposed", "already_pending", "duplicates",
         "insufficient", "incomplete", "skipped", "dry_run"}

    One pass at a time per repo (`_reconcile_lock`): finding another pass already running
    means this one does NOTHING and says so (`skipped`), rather than waiting or racing it.

    NEVER raises: every caller is a host hook or a report surface, and a reconciliation that
    could not finish is a receipt marked `incomplete`, never a broken session start.
    """
    receipt = _receipt(dry_run)
    try:
        if dry_run:
            # NOT lock-guarded, both ways round: a dry run writes nothing, so it cannot cause
            # the fabricated disposition the lock exists to prevent - and taking the lock would
            # itself create a file, breaking the "dry_run writes NOTHING anywhere" invariant,
            # while letting a preview skip (or delay) a real pass. Worst case its report is a
            # snapshot of a spool that moved underneath it, which is what a preview is.
            return _reconcile(repo_path, session_id, True, receipt)
        with _reconcile_lock(repo_path) as acquired:
            if not acquired:
                receipt["skipped"] = True
                return receipt
            return _reconcile(repo_path, session_id, dry_run, receipt)
    except Exception:                  # broad on purpose: the never-raises contract
        receipt["incomplete"] = True
        return receipt


def format_receipt(receipt: dict) -> str:
    """The receipt as human-readable lines - shared by the CLI command and the MCP tool so a
    developer and a model are never told two different stories about one pass."""
    if receipt.get("skipped"):
        return ("Reconciled evidence: skipped - another reconciliation pass is already "
                "running on this repo. The next checkpoint picks this up.")
    head = "Reconciled evidence" + (" (dry run - nothing was written)" if receipt["dry_run"]
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
        lines.append("  (proposals only - nothing was retired. `contexer review` shows each "
                     "one; retiring is an explicit `contexer retire <id>`.)")
    if receipt["incomplete"]:
        lines.append("  incomplete: the evidence spool could not be fully read or updated.")
    return "\n".join(lines)
