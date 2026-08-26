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
  no receipt line, no disposition. It reads the store and reports what a real pass would do,
  with one honest exception: a candidate that duplicates a decision still `pending_approval`
  previews as `duplicates` (it never reaches `_settle_write_statuses`, which is what turns that
  case into `already_pending` on a real pass) - the preview undercounts that one field rather
  than running the write-adjacent recheck just to report it.

The receipt of a pass is LOGGED, never spooled (ruling R34): `.reconcile_<slug>.jsonl`, the
`.retrieval_<slug>.jsonl` precedent. Bookkeeping in the evidence spool is bookkeeping that
retention eventually reports as lost evidence - see `_log_receipt`.

ORDER MATTERS, and it is a state machine rather than a sequence of hopeful writes. Every held
candidate carries its phase in its own `candidate.json` (`spool.CANDIDATE_STATES`), and each
transition is durable before the next one starts:

1. write the complete manifest in `held` state;
2. move every named event into the held directory;
3. VERIFY the manifest's event set against what is now there - a missing source with no held
   target marks the pass incomplete and stops this candidate;
4. flip the manifest to `materializing`;
5. write through the ordinary store or lifecycle path;
6. re-read the store and persist `pending_review` or `settled` from what it OBSERVES, never
   from the write's own return value;
7. after a review, record the compact evidence summary on the decision;
8. persist `reviewed` with the disposition;
9. only then remove the held directory.

Hold FIRST, materialize second. The reverse order (the shipped one until this task) left a
window nothing on disk covered: a crash after the store write and before the hold left a
decision whose evidence was still in `pending/`, connected to it by nothing. Now a crash
anywhere leaves the evidence either wholly pending - so the next pass simply aggregates it
again, under the same deterministic id - or wholly held behind a manifest that names both what
it claims and how far it got. `_resume_holds` carries each of those forward, re-classifying a
held candidate's own events against the CURRENT store so a replay recognizes the decision the
interrupted pass already created instead of writing it twice.

The window is closed, but its recovery stays: a store that dedups a restatement onto an entry
still awaiting review, and legacy holds from the shipped order, both still produce a candidate
that duplicates a decision nobody has reviewed. Settling that would file a `dismissed` receipt
against a live review and delete the only evidence for it, so it is HELD against that decision
instead (`_settle_write_statuses`) and its disposition becomes whatever the review turns out
to be.

On the way out the same rule reads backwards: the summary is durable before the manifest says
`reviewed`, and the manifest says `reviewed` before a single raw event is deleted. Any other
order loses the evidence and the receipt for it together, permanently.
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
        # Binary, and it stays empty: nothing is ever written to or read from this file -
        # the flock on its descriptor is the whole content. (Also why the package-wide
        # pin-your-encoding invariant does not apply: there is no text here.)
        handle = open(path, "ab")       # noqa: SIM115 - closed on every path below
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


def _human_ratified(entry: dict | None) -> bool:
    """True when a developer explicitly approved THIS decision's live content.

    `approved_by == "human"` is written only by `store._apply_approval`'s approve/edit paths, so
    it is the one signal that survives approving a brand-new pending entry - which blesses
    revision 1 in place and advances nothing a revision test could see. A live
    `proposed_revision` disqualifies it: the wording on offer is not the wording that was
    ratified.

    TWO readers, deliberately shared: `_dispositions` judges a hold that reached review, and
    `_settle_write_statuses` judges a duplicate whose target is no longer awaiting one. Spelling
    it twice is how a resumed candidate came to file `dismissed` against a decision a human had
    just approved.
    """
    return bool(entry and entry.get("approved_by") == "human"
                and not entry.get("proposed_revision"))


def _dispositions(held: dict, entries: list, retired_ids: set) -> dict:
    """The disposition each held candidate has earned since the last pass,
    `{candidate_id: "approved"|"dismissed"}`.

    The INPUT is the held directories and their `candidate.json` bookkeeping. This judges only
    the candidates that have REACHED review (`pending_review`, and `settled` for one whose
    finalize was interrupted): a candidate still `held` or `materializing` has no review to
    have earned anything yet and belongs to `_resume_holds`, a `reviewed` one has already
    earned its disposition, and an unknown state is never guessed at at all.

    The rules are lazy on purpose: nothing hooks `approve_decision` or `retire_decision`, so a
    candidate learns its decision's fate the next time reconciliation runs. In the order they
    are tried:

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
    * otherwise it is a brand-new pending entry - or a duplicate held against one, which is the
      same question - and only a real ratification stamp (`approved_by == "human"`, written by
      `_apply_approval`'s approve/edit paths) counts - approving one blesses revision 1 IN
      PLACE, so the revision rule above cannot see it.

    Anything else stays held, which is also what keeps its evidence exempt from retention until
    somebody actually reviews it. A candidate whose meta records no `entry_id` is left held
    too: there is nothing to judge it against, and guessing is what
    `spool.evidence_diagnostics`' `held_unattributed` counter exists to surface instead.
    """
    by_id = {str(e.get("id") or ""): e for e in entries}
    flips = {}
    for candidate_id, meta in sorted(held.items()):
        state = meta.get("state")
        if state == "settled":
            # Settled before this pass - a duplicate, an update the store applied in place, or a
            # disposition whose finalize was interrupted - and only the finalize that removes
            # the directory is missing. The recorded disposition is what it was decided to be,
            # never what the rules below would infer: without this, a crash between the hold and
            # the finalize left a duplicate carrying an `entry_id` and no `lane`, and the last
            # rule below read its target's `approved_by == "human"` as an APPROVAL of a
            # candidate that was dismissed on arrival - the fabricated-approval class this whole
            # function guards.
            #
            # Tested against the spool's own vocabulary, not merely `!= "pending"`: a
            # hand-edited `candidate.json` would otherwise reach `finalize_candidate_evidence`
            # with a disposition it rejects, and that ValueError aborts the WHOLE pass - on
            # every run, for good.
            settled = str(meta.get("status") or "")
            if settled in spool.DISPOSITIONS:
                flips[candidate_id] = settled
            continue
        if state != "pending_review":
            # `held`/`materializing` belong to `_resume_holds`, `reviewed` has already earned
            # its disposition, and a missing, unreadable or unknown state is never guessed at.
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
        elif _human_ratified(entry):
            status = "approved"
        else:
            continue
        flips[candidate_id] = status
    return flips


def _lifecycle_awaiting_review(repo_path: str, target: str) -> bool:
    """True when `target` is a live decision that already carries a retirement proposal.

    The one question `lifecycle.propose_lifecycle`'s refusal cannot answer: it returns the same
    `ok: False` whether the decision is gone, is no longer live, or is holding a proposal that
    outranks this one - and only the last of those means a review is pending. Asked of the
    STORE rather than of the message, so it stays true however that message is worded.

    One extra read, on the refusal path only, which is rare and never on the fast path.
    """
    if not target:
        return False
    entries = [e for e in store.load(repo_path).get("entries", []) if isinstance(e, dict)]
    entry = store.entry_by_id([e for e in entries if e.get("type") == "decision"], target)
    return bool(entry and entry.get("proposed_lifecycle"))


def _manifest(candidate_id: str, candidate: dict, event_ids: list, basis: dict,
              created_at: str = "") -> dict:
    """The complete `candidate.json` for one candidate, in `held` state.

    Written before a single event moves and before the store is touched at all, so it carries
    everything a resumed pass needs to finish the work without re-aggregating: what the
    candidate PROPOSES (`kind`, `target_decision_id`), the revision that proposal was formed
    against (`basis_revision_id`), the events it claims, and the decision text itself.

    `lane` and `revision_id` are deliberately ABSENT rather than null: absence is how the
    content lane has always been spelled here, and `_dispositions` reads a stored
    `revision_id` as "this materialized as a proposal" - a null one would be a third meaning
    for a field with two.
    """
    now = datetime.now(timezone.utc).isoformat()
    target = str(candidate.get("target_decision_id") or "") or None
    return {
        "schema_version": spool.MANIFEST_VERSION,
        "candidate_id": candidate_id,
        "state": "held",
        "status": "pending",
        "kind": candidate.get("kind") or "",
        "target_decision_id": target,
        "basis_revision_id": basis.get(target or "") or None,
        "event_ids": list(event_ids),
        "entry_id": "",
        "candidate": {
            "title": candidate.get("title") or "",
            "content": candidate.get("content") or "",
            "subtype": candidate.get("subtype") or "",
            "source_files": list(candidate.get("source_files") or []),
            "possible_source_files": list(candidate.get("possible_source_files") or []),
            "score": candidate.get("score") or 0,
        },
        "created_at": created_at or now,
        "updated_at": now,
    }


def _materialize(repo_path: str, candidate: dict, sessions: dict, dry_run: bool,
                 receipt: dict, writes: dict, basis: dict, *,
                 candidate_id: str = "", prior: dict | None = None) -> None:
    """Route ONE candidate: count it, recommend it, or hold-then-store it.

    HOLD FIRST, then materialize (transitions 1-5). The manifest lands in `held` state, the
    events move behind it, the move is VERIFIED, the manifest flips to `materializing`, and
    only then does the store see anything. A crash at any of those points leaves evidence that
    is either wholly in `pending/` or wholly held under a manifest naming what it claims -
    never a decision whose evidence nothing on disk connects it to.

    A hold that cannot account for its events stops the candidate here. Storing a decision
    whose evidence was never verified as moved is what leaves the two to be reconciled by
    guesswork later; the events stay where they are and the next pass tries again.

    A `writes` entry is the candidate's observed bookkeeping - `event_ids`, the `status` it
    settles at (`pending` = awaiting review, anything else = settled in this same run), the
    `entry_id` it belongs to, and the lane/revision the disposition rules read. `_commit_writes`
    persists it onto the manifest as transition 6.

    `candidate_id`/`prior` are the resume path (`_resume_holds`): the identity of a hold is the
    directory it already occupies, never the id a re-classification would mint, and `prior`
    keeps the moment the candidate was first claimed.
    """
    kind = candidate.get("kind")
    candidate_id = candidate_id or candidate["candidate_id"]
    event_ids = [str(s.get("event_id") or "") for s in candidate.get("signals") or []]

    if kind == "insufficient":
        # No hold: more evidence may still arrive for the same statement, and a
        # dismissal here would consume the events that would have completed it.
        receipt["insufficient"] += 1
        return
    if dry_run:
        # Counted and dropped BEFORE the first write of any kind - a dry run creates no
        # manifest, moves no event and takes no lock. This is the only guard the preview
        # needs on this path now that everything below it writes.
        receipt["lifecycle_proposed" if kind in _LIFECYCLE_KINDS
                else "duplicates" if kind == "duplicate" else "proposed"] += 1
        return
    if not _hold(repo_path, candidate_id, event_ids,
                 meta=_manifest(candidate_id, candidate, event_ids, basis,
                                created_at=str((prior or {}).get("created_at") or ""))):
        receipt["incomplete"] = True
        return
    # Transition 4. Its return is deliberately not checked: a manifest still reading `held`
    # replays through exactly the same path as one reading `materializing`, so a flip that did
    # not land costs nothing, while transition 6 below writes to the same file and DOES report.
    spool.update_candidate_state(repo_path, candidate_id, "materializing")

    if kind in _LIFECYCLE_KINDS:
        target = str(candidate.get("target_decision_id") or "")
        result = lifecycle.propose_lifecycle(
            repo_path, target, "retire",
            f"inferred from session evidence: {candidate.get('title') or ''}".strip(),
            source="ai", replacement_id=candidate.get("replacement_decision_id"))
        if not result["ok"]:
            if _lifecycle_awaiting_review(repo_path, target):
                # Refused because a retirement proposal for this target ALREADY SITS on it -
                # most often the one this very candidate attached before an interrupted pass,
                # since a replay proposes what it proposed the first time. A retirement is
                # awaiting review, so the evidence is held against it and `_dispositions`
                # settles it on the outcome - approved when the target is genuinely retired,
                # dismissed when the proposal dies. Reading the refusal as "nothing awaits
                # review" filed a `dismissed` receipt against a LIVE proposal and deleted the
                # only evidence for it. Counted as `already_pending` rather than as a new
                # proposal: this pass created nothing, and nothing was thrown away.
                receipt["already_pending"] += 1
                writes[candidate_id] = {"event_ids": event_ids, "kind": kind,
                                        "status": "pending", "entry_id": target,
                                        "lane": "lifecycle"}
                return
            # Genuinely refused - the target is gone or is no longer live. Nothing awaits
            # review, so the events are settled rather than re-aggregated into the same refusal
            # on every future pass. Deliberately uncounted: no receipt line claims a proposal
            # that does not exist, and inventing a "refused" counter for a case the developer
            # cannot act on would be noise.
            writes[candidate_id] = {"event_ids": event_ids, "kind": kind,
                                    "status": "dismissed", "entry_id": target}
            return
        receipt["lifecycle_proposed"] += 1
        # `lane` is what `_dispositions` reads (off the held dir's `candidate.json`) to tell an
        # approved retirement (the target is tombstoned with a retirement record) from a
        # dismissed one, without ever inferring either. Deliberately NO `revision_id`: a
        # revision advance is not an approval signal for this lane (ruling R25), so storing one
        # would only invite a reader to use it.
        writes[candidate_id] = {"event_ids": event_ids, "kind": kind, "status": "pending",
                                "entry_id": target, "lane": "lifecycle"}
        return
    if kind == "duplicate":
        # The store already holds this decision, so there is nothing to review - provided that
        # decision is itself settled. Then the events ARE settled too, so they are held and
        # finalized in this SAME run (red-team mitigation 2), with the summary attached to the
        # decision they matched. Leaving them in `pending/` would re-aggregate this duplicate
        # at every checkpoint forever and permanently defeat
        # the fast path. The recorded status is `pending` rather than `dismissed` because only
        # a re-read of the store can tell whether the decision this duplicates is settled or is
        # ITSELF still awaiting review - `_settle_write_statuses` decides, and corrects the
        # receipt's duplicate count when it turns out not to be a settled duplicate after all.
        receipt["duplicates"] += 1
        writes[candidate_id] = {"event_ids": event_ids, "kind": kind, "status": "pending",
                                "entry_id": str(candidate.get("target_decision_id") or "")}
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
        writes[candidate_id] = {"event_ids": event_ids, "kind": kind, "status": "pending",
                                "entry_id": str(entry_id)}
        return
    # The store's own filter rejected it - an existing decision already says this. That is a
    # duplicate, not an error, and it settles the events exactly like a matched one.
    receipt["duplicates"] += 1
    writes[candidate_id] = {"event_ids": event_ids, "kind": kind, "status": "dismissed",
                            "entry_id": ""}


def _settle_write_statuses(repo_path: str, writes: dict, receipt: dict) -> None:
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

    A DUPLICATE record is the same question asked the other way round, and it is what closes the
    materialize-before-hold crash window: a duplicate of a SETTLED decision is settled itself and
    is dismissed here exactly as it always was, but a duplicate of a decision that is still
    `pending_approval` is evidence FOR a review that has not happened yet - most often the very
    evidence that decision was materialized from, re-read after a crash before its hold existed.
    Dismissing it would delete that evidence and file a receipt saying it was settled, against a
    decision nobody has looked at. It stays held instead, and `_dispositions` settles it on the
    review itself. The receipt is corrected here too: it was counted as a duplicate at
    materialize time, and it is not one.
    """
    pending_ids = {cid for cid, record in writes.items()
                   if record.get("status") == "pending" and record.get("lane") != "lifecycle"}
    if not pending_ids:
        return
    by_id = {str(e.get("id") or ""): e for e in store.load(repo_path).get("entries", [])
             if isinstance(e, dict)}
    for candidate_id in sorted(pending_ids):
        record = writes[candidate_id]
        entry = by_id.get(str(record.get("entry_id") or ""))
        awaiting_review = entry is not None and store.entry_status(entry) == "pending_approval"
        if record.get("kind") == "duplicate":
            # Deliberately NOT the `proposed_revision` test below: a duplicate never proposed
            # that revision, so settling on somebody else's proposal would record an outcome
            # its own evidence never earned - and a target that is approved but carries a
            # proposal would be left held with no rule able to settle it at all.
            if awaiting_review:
                receipt["duplicates"] -= 1
                receipt["already_pending"] += 1
            else:
                # The review already happened, which a RESUMED candidate meets routinely: it
                # duplicates the decision the interrupted pass created, and the developer may
                # have approved that decision in the meantime. Defaulting to `dismissed` filed
                # the opposite of what happened onto an `approved_by: human` decision and then
                # deleted the evidence, so the same ratification signal `_dispositions` uses
                # decides here too.
                record["status"] = "approved" if _human_ratified(entry) else "dismissed"
        elif entry is not None and entry.get("proposed_revision"):
            record["revision_id"] = entry.get("current_revision_id") or ""
        elif not awaiting_review:
            record["status"] = "dismissed"


def _finish_interrupted_holds(repo_path: str, events: list, held: dict, dry_run: bool,
                              receipt: dict) -> list:
    """Drop every event an existing candidate already claims, finishing a half-done hold.

    A crash partway through transition 2 leaves a candidate whose manifest names every event it
    claims and whose events are split across `pending/` and `held/`. Aggregating the remainder
    would mint a DIFFERENT candidate id - the seed is the sorted event ids - and propose the
    same decision a second time, so the leftovers are recognized by the held candidate's own
    recorded `event_ids` and moved into the directory that already claims them. This runs
    BEFORE any resume, so a resumed candidate reads a hold that is whole again.

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


def _resume_holds(repo_path: str, held: dict, projection: list, sessions: dict,
                  receipt: dict, writes: dict, basis: dict) -> dict:
    """Carry every interrupted candidate to its next durable phase. Returns the holds that
    still stand, so the aggregation loop never counts a directory this pass just discarded.

    One rule per phase, and each one is the recovery for a crash at a numbered transition:

    * `reviewed` (crash at 9) - the disposition and its summary are both durable, so only the
      raw cleanup is left. It is finished WITHOUT re-recording anything.
    * `held` / `materializing` (crash at 2-5) - the events are held and no review exists yet.
      The HELD events are re-classified against the CURRENT store and materialized through the
      ordinary path, which is what makes the replay idempotent rather than merely retried: a
      decision the interrupted pass already created comes back as a `duplicate` of itself and
      is held against its own review, and one that never landed comes back as `new`. Asking the
      aggregator is also the only inspection that stays honest as the store moves underneath a
      stuck candidate.
    * a hold holding no events at all is DISCARDED - it can never be materialized and it would
      otherwise occupy its candidate id for good (`spool.discard_empty_hold`).
    * anything else - a manifest that is missing, unreadable or carries an unknown state, and
      any candidate already awaiting review - is left exactly as it is, for `_dispositions` and
      the diagnostics counters to speak about.

    The identity of a resumed candidate is the DIRECTORY it already occupies, never the id its
    re-classification would mint: kind and target are part of that id, and both can legitimately
    change between the crash and the replay.
    """
    standing = {}
    for candidate_id, meta in sorted(held.items()):
        state = meta.get("state")
        if state == "reviewed":
            spool.finalize_candidate_evidence(repo_path, candidate_id,
                                              str(meta.get("status") or "dismissed"))
            continue
        if state not in ("held", "materializing"):
            if not meta and spool.discard_empty_hold(repo_path, candidate_id):
                # A directory whose manifest write never landed (transition 1) and which holds
                # no events either: nothing can ever attribute it, and leaving it would occupy
                # its candidate id, so the same evidence would read as already pending forever.
                # A missing manifest with events in it is a different case entirely and is
                # refused by `discard_empty_hold` itself.
                continue
            standing[candidate_id] = meta
            continue
        events = spool.held_events(repo_path, candidate_id)
        if not events and spool.discard_empty_hold(repo_path, candidate_id):
            continue
        resumed = (candidates.aggregate_candidates(events, projection)["candidates"]
                   if events else [])
        if len(resumed) == 1:
            # The events a hold claims were ONE candidate when it was written. Anything else
            # is a corpus this pass cannot attribute to this directory, so it is reported
            # rather than split, merged or guessed at.
            #
            # ponytail: a resume that comes back `insufficient` - the seed event was lost and
            # what survives no longer clears the review bar - is counted and left held, so it
            # is re-read on every pass and shows up as `held_unattributed` until a human acts.
            # Accepted over the alternatives: settling it would delete evidence nobody reviewed,
            # and retention deliberately never touches a held directory.
            sessions.update({str(e.get("event_id") or ""): str(e.get("session_id") or "")
                             for e in events})
            _materialize(repo_path, resumed[0], sessions, False, receipt, writes, basis,
                         candidate_id=candidate_id, prior=meta)
        else:
            receipt["incomplete"] = True
        standing[candidate_id] = meta
    return standing


def _recorded_summaries(entries: list) -> set:
    """`(entry_id, candidate_id)` for every disposition already filed on a decision's history.

    What makes `_finalize` IDEMPOTENT. The summary is written before the held events are
    deleted, so a failure between the two leaves a candidate that still holds its evidence and
    still settles to the same disposition on the next pass - and re-recording it would file the
    same receipt twice on the same decision.
    """
    pairs = set()
    for entry in entries:
        entry_id = str(entry.get("id") or "")
        for row in entry.get("evidence_summary") or []:
            if isinstance(row, dict) and row.get("candidate_id"):
                pairs.add((entry_id, str(row["candidate_id"])))
    return pairs


def _finalize(repo_path: str, candidate_id: str, disposition: str, entry_id: str,
              filable: set, event_ids: list, recorded: set, receipt: dict) -> None:
    """Settle one candidate: preserve the summary on its decision, record the outcome on its
    manifest, and only THEN delete the raw held events (transitions 7, 8 and 9).

    The summary is the whole point of finalizing rather than deleting - once the events are
    gone, the decision's own `evidence_summary` history is the only place the disposition lives.
    So the durable order is the one written here. The other way round - delete, then record -
    one transient store failure lost BOTH the raw evidence and its summary, and a `False` return
    (the store saying it filed nothing) was dropped on the floor entirely. Now a summary that did
    not land leaves the held directory exactly where it is, marks the pass `incomplete` the way
    every other partial result here does, and the next pass settles the candidate again from the
    status recorded on its own hold (`_dispositions`' first rule). One entry's failure still
    never abandons the rest of the batch mid-loop.

    TWO shapes of "no entry to file against" arrive here, and they are NOT the same fact - the
    caller passes the raw `entry_id` plus `filable` precisely so this can tell them apart:

    * NO `entry_id` AT ALL. The events were never attributed to any decision: the store's own
      novelty filter matched the capture onto an existing entry as a recurrence (returning no
      id) rather than letting the aggregator classify it a duplicate - including, by a known
      and accepted gap, onto a RETIRED or IGNORED decision, since `_find_match` and
      `_is_tombstoned` are status-blind. Deleting here would destroy acknowledged evidence with
      NO receipt anywhere, which is the one thing runbook invariant 3 forbids, so the hold is
      left exactly as it is and surfaces through `evidence_diagnostics`' `held_unattributed`.
      The cost is a held directory nothing will settle until a human or a later lane
      (OUTSTANDING-ISSUES, the reconsideration work) does; that is strictly better than the
      silent loss it replaces, and it is the same answer this module gives every other
      candidate it cannot judge.
    * an `entry_id` that no longer RESOLVES (`not in filable`). The decision was evicted after
      this candidate was attributed to it, so there is nowhere to file the receipt and no write
      that could ever succeed - withholding the delete would retry it on every pass forever,
      and `spool._sweep_orphan_holds` would remove the hold anyway. The delete proceeds.

    The manifest reaches `reviewed` BETWEEN the summary and the delete, which is what makes the
    cleanup replayable on its own: a crash at the delete leaves a hold whose disposition and
    summary are both durable, so the next pass finishes the removal without asking the
    disposition rules anything and without filing a second receipt. A manifest that refuses the
    update stops the delete - `reviewed` is the record that the receipt IS durable, and deleting
    the evidence without it would leave the next pass judging the candidate over again.
    """
    if not entry_id:
        return
    if entry_id in filable and (entry_id, candidate_id) not in recorded:
        summary = {"candidate_id": candidate_id, "disposition": disposition,
                   "event_ids": list(event_ids),
                   "occurred_at": datetime.now(timezone.utc).isoformat()}
        try:
            landed = store.record_evidence_summary(repo_path, entry_id, summary)
        except Exception:
            landed = False
        if not landed:
            receipt["incomplete"] = True
            return
    # `status` only: the manifest already records which decision this was about, and nothing
    # here has learned anything truer about it.
    if not spool.update_candidate_state(repo_path, candidate_id, "reviewed",
                                        status=disposition):
        receipt["incomplete"] = True
        return
    spool.finalize_candidate_evidence(repo_path, candidate_id, disposition)


def _commit_writes(repo_path: str, writes: dict, recorded: set, filable: set, receipt: dict) -> None:
    """Transition 6: persist each materialized candidate's OBSERVED state, then finalize the
    ones with no review left to wait for.

    The state comes from what a re-read of the store saw (`_settle_write_statuses`), never from
    the write's own return value: `update_decision_with_meta` answers identically for a
    brand-new pending entry, an attached proposal and a correction it applied in place, and only
    the first two leave anything for a developer to look at. `pending_review` means somebody has
    to look; `settled` means nobody does, and the disposition rides along as `status` so an
    interrupted finalize is settled from its own hold rather than re-inferred.

    A manifest that could not be updated is NEVER finalized: the phase on disk would still read
    `materializing` while the evidence behind it was deleted. It is reported instead, and the
    next pass settles the candidate with its evidence intact.

    `filable` is the same `_run_pass`-computed set the `flips` loop passes to `_finalize` (live
    decisions and tombstoned ones, tasks excluded on both sides): a decision materialized
    earlier in this very pass can still be evicted by a later write in the same batch, so the
    guard applies here too rather than only on the flips side.
    """
    for candidate_id, record in sorted(writes.items()):
        settled = record["status"] != "pending"
        fields = {key: record[key] for key in ("entry_id", "lane", "revision_id", "status")
                  if key in record}
        if not spool.update_candidate_state(repo_path, candidate_id,
                                            "settled" if settled else "pending_review",
                                            **fields):
            receipt["incomplete"] = True
            continue
        if settled:
            _finalize(repo_path, candidate_id, record["status"],
                      str(record.get("entry_id") or ""), filable,
                      record["event_ids"], recorded, receipt)


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
    """The fast-path gate, then the lock, then the pass.

    Both bail-outs are tested BEFORE the lock is touched, the same unlocked-then-locked shape
    `store.ensure_retrieval_index` uses and for a sharper reason here: this runs at every
    session start on every repo, and taking the lock first would create (and `mkdir` for) a
    lock file on repos that will never have a single evidence event. The listing under the
    lock is the authoritative one - a pass that finished while this one waited its turn may
    have consumed exactly the events read here - so the fast path's read is pure work
    avoidance and is deliberately thrown away.
    """
    if not _has_work(repo_path, session_id):
        return receipt
    if dry_run:
        # NOT lock-guarded, both ways round: a dry run writes nothing, so it cannot cause the
        # fabricated disposition the lock exists to prevent - and taking the lock would itself
        # create a file, breaking the "dry_run writes NOTHING anywhere" invariant, while
        # letting a preview skip (or delay) a real pass. Worst case its report describes a
        # spool that moved underneath it, which is what a preview is.
        return _run_pass(repo_path, session_id, True, receipt)
    with _reconcile_lock(repo_path) as acquired:
        if not acquired:
            receipt["skipped"] = True
            return receipt
        return _run_pass(repo_path, session_id, False, receipt)


def _has_work(repo_path: str, session_id: str) -> bool:
    """Anything to aggregate, or any held candidate whose fate could have changed.

    Two directory listings; the store is not read at all, let alone locked - this is the
    every-session-start cost, and the whole pass is skipped on a `False`.

    No readability gate any more, and none is needed: settling a candidate MOVES its events
    out of `pending/`, so a spool that reads as empty can no longer cause the failure the old
    ledger's gate existed for (re-proposing everything its checkpoints had already settled).
    An unreadable spool is reported where a developer can act on it - `contexer status`, off
    `spool.evidence_diagnostics`' own `readable` flag - rather than costing a pass here.
    """
    return bool(spool.held_candidates(repo_path)
                or [e for e in spool.list_pending_evidence(repo_path, session_id)
                    if e.get("kind") in _EVIDENCE_KINDS])


def _snapshot(repo_path: str) -> dict:
    """Everything a pass classifies and files against, read in one go.

    One helper rather than seven locals because it is read TWICE: once at the top of a pass and
    again after a resume has written the store, since a stale projection there classified new
    matching evidence as `new` instead of as a duplicate of what the resume had just created.

    * `entries`/`tombstones`/`decisions` - `type == "decision"` on BOTH sides, because the store
      holds tasks too and a deleted task is no more a decision to classify against than a live
      one is;
    * `projection` - the read-only view `candidates.aggregate_candidates` takes;
    * `recorded` - dispositions already filed, which is what keeps `_finalize` idempotent;
    * `filable` - what `store.record_evidence_summary` can actually write to; an `entry_id`
      outside it names a decision that is simply gone, which is a receipt with nowhere to go
      rather than a store failure to retry (see `_finalize`);
    * `basis` - the revision a proposal is formed against, stamped on the manifest at hold time
      so a resumed pass can tell what the candidate was judged against after HEAD moves.
    """
    entries = [e for e in store.load(repo_path).get("entries", []) if isinstance(e, dict)]
    tombstones = [e for e in store.load_deleted(repo_path).get("entries", [])
                  if isinstance(e, dict) and e.get("type") == "decision"]
    decisions = [e for e in entries if e.get("type") == "decision"]
    return {
        "entries": entries,
        "tombstones": tombstones,
        "projection": ([_projected(e, False) for e in decisions]
                       + [_projected(e, True) for e in tombstones]),
        "recorded": _recorded_summaries(decisions + tombstones),
        "filable": {str(e.get("id") or "") for e in decisions + tombstones},
        "basis": {str(e.get("id") or ""): str(e.get("current_revision_id") or "")
                  for e in decisions},
    }


def _run_pass(repo_path: str, session_id: str, dry_run: bool, receipt: dict) -> dict:
    events = [e for e in spool.list_pending_evidence(repo_path, session_id)
              if e.get("kind") in _EVIDENCE_KINDS]
    held = spool.held_candidates(repo_path)
    if not events and not held:
        # The work the fast path saw is gone: another pass took it while this one waited for
        # the lock. Nothing to do, and nothing to report about it.
        return receipt
    events = _finish_interrupted_holds(repo_path, events, held, dry_run, receipt)
    # ponytail: counted AFTER the recovery strips events an existing candidate already claims,
    # so `events_observed` means "events this pass aggregated", not "files in pending/". The
    # checkpoint era's number was the same thing by a different route (a consumed event never
    # reached the reader at all); recovering a half-done hold is the only case where the two
    # spellings could differ, and reporting the leftovers as observed would double-count the
    # evidence of a candidate that is already awaiting review.
    receipt["events_observed"] = len(events)

    sessions = {str(e.get("event_id") or ""): str(e.get("session_id") or "") for e in events}
    snap = _snapshot(repo_path)

    writes: dict = {}
    flips: dict = {}
    if not dry_run:
        # Interrupted candidates first, and their dispositions off what SURVIVES that: a resume
        # can discard a hold or carry it to a new phase, and both the flips below and the loop's
        # own "already pending" test read `held`.
        held = _resume_holds(repo_path, held, snap["projection"], sessions, receipt, writes,
                             snap["basis"])
        if writes:
            # A resume WROTE the store, so everything below must classify and file against what
            # the store says NOW. Reading a stale projection here cost an acknowledged event:
            # matching evidence arriving in the same pass read as `new` instead of as a
            # duplicate of the decision the resume had just created, the store's own novelty
            # filter then rejected the capture, and the record landed with no `entry_id` to
            # file a receipt against. One re-read closes it; `_finalize` refusing to delete an
            # unattributed hold is the backstop for every other route to the same shape.
            snap = _snapshot(repo_path)
        flips = _dispositions(held, snap["entries"], _retired_ids(snap["tombstones"]))
    for candidate in candidates.aggregate_candidates(events, snap["projection"])["candidates"]:
        if candidate["candidate_id"] in held:
            # Already awaiting review under its deterministic id. Belt to the held-events
            # braces: it also covers a candidate whose directory exists but whose bookkeeping
            # never recorded the event ids `_finish_interrupted_holds` matches on.
            receipt["already_pending"] += 1
            continue
        _materialize(repo_path, candidate, sessions, dry_run, receipt, writes, snap["basis"])

    if dry_run:
        return receipt
    _settle_write_statuses(repo_path, writes, receipt)
    _commit_writes(repo_path, writes, snap["recorded"], snap["filable"], receipt)
    for candidate_id, disposition in sorted(flips.items()):
        meta = held[candidate_id]
        _finalize(repo_path, candidate_id, disposition, str(meta.get("entry_id") or ""),
                  snap["filable"], meta.get("event_ids") or [], snap["recorded"], receipt)
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

    `already_pending` counts both shapes of "this is already waiting on the developer": a
    candidate whose own held directory exists, and a duplicate of a decision that is itself
    still awaiting review (held against it rather than settled - see `_settle_write_statuses`).
    Both mean the same thing to a reader: nothing new to look at, and nothing thrown away.

    One pass at a time per repo (`_reconcile_lock`, taken only once there is work to do):
    finding another pass already running means this one does NOTHING and says so (`skipped`),
    rather than waiting or racing it.

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
