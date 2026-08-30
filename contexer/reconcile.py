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
  human moves a decision out of active context;
* a developer's restatement of an INACTIVE decision lands as a `proposed_reconsideration` on
  that same decision - the reconsideration lane, which restores nothing: only an explicit
  `lifecycle.reconsider_decision` by a human brings one back. Routed to the lifecycle owner and
  never through the content path, because the store's dedup is status-blind and would absorb
  the restatement onto the dead entry with no receipt anywhere (OUTSTANDING-ISSUES item 7).

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
it claims and how far it got. `_recoverable_holds` carries each interrupted hold forward,
re-classifying its own events against the CURRENT store so a replay recognizes the decision
the interrupted pass already created instead of writing it twice. A deliberate attention
deferral instead resumes the exact proposal and lifecycle basis frozen before earlier admitted
batches changed the store.

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

from contexer import candidates, evidence, lifecycle, repo_key, sidecars, spool, store

# Kinds a candidate can be built out of. The rest of `evidence.EVENT_KINDS` is bookkeeping
# ABOUT candidates (`policy_evaluation`, `session_reconcile`), which never groups into one, so
# a spooled one must never make the next pass look like it has work to do.
_EVIDENCE_KINDS = candidates.SEED_KINDS | candidates.SUPPORT_KINDS

# Measured attention bounds, not storage bounds. Before admission control, 20 distinct
# directives produced 20 review rows in ~69ms and the realistic 1,000-event corpus produced
# 100 rows in ~1.32s. Five writes keep one checkpoint's materialization work small; ten total
# pending items matches the review surface's existing overview scale without dropping a byte.
# Excess candidates are held durably in `deferred_attention`, so these constants can be tuned
# from the benchmark without changing capture or retention limits.
PENDING_REVIEW_CEILING = 10
MATERIALIZATION_ALLOWANCE = 5

# Tail cap on the reconciliation log, matching `store._RETRIEVAL_LOG_CAP` - the same kind of
# record, kept for the same reason, so it is bounded the same way.
_RECEIPT_LOG_CAP = 200

# Candidate kinds that materialize into the LIFECYCLE lane rather than the content one. Both
# spell the same transition - "this decision should stop being live" - and `replace` differs
# only by naming what takes over, which rides along as `replacement_decision_id` (and makes the
# eventual lifecycle record read "superseded" instead of "retired").
_LIFECYCLE_KINDS = frozenset({"retire", "replace"})

# Reconsideration refusals that mean THE RECORD MOVED, not that there is nothing to ask. A
# candidate meeting one of these is replayable: it stays held, unsettled and unproposed, and a
# later pass re-forms it against whatever the decision is then. Every other refusal is terminal
# for this candidate or means a review is already pending, and each is answered separately -
# reading one `ok: False` as one fact is what filed a disposition for a review that never
# happened (see `_lifecycle_awaiting_review`).
_STALE_REFUSALS = frozenset({"stale_basis", "stale_state"})

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


def _receipt(dry_run: bool, host: str = "") -> dict:
    return {"events_observed": 0, "proposed": 0, "lifecycle_proposed": 0, "reconsidered": 0,
            "already_pending": 0, "duplicates": 0, "insufficient": 0, "deferred": 0,
            "incomplete": False, "lock_unavailable": False,
            "skipped": False, "dry_run": bool(dry_run),
            "coverage": evidence.host_coverage(host)}


def _recoverage(receipt: dict, state: str, dropped: int = 0) -> None:
    """Restate the coverage block for the status this pass actually reached.

    Rebuilt rather than mutated so the vocabulary and the never-upgrade rule stay in
    `evidence.host_coverage`: the host name round-trips through it (an unknown one is already
    `manual`, which resolves to `manual` again) and the drop count only ever accumulates."""
    block = receipt["coverage"]
    receipt["coverage"] = evidence.host_coverage(
        block["host"], reconciliation=state,
        dropped_events=block["dropped_events"] + dropped)


@contextmanager
def _reconcile_lock(repo_path: str):
    """Yield True to the one pass that holds this repo's reconcile lock, False to any pass
    that finds it already held. NEVER waits.

    Two concurrent passes over one spool can record a disposition that never happened: each
    snapshots the live decisions before classifying, so a store write landing between B's
    snapshot and B's classification changes the `kind`/`target_decision_id` B computes for the
    SAME events, hence their `candidate_id` - and B then holds events A already moved, sees
    them `missing`, and files an `evidence_summary` under its own entry describing a
    disposition nobody made. EVERY host now reconciles at session start (the shared
    `store._local_session_start_payload` path), and Claude and Gemini add their own compaction
    and session-end checkpoints on top, so two passes on one repo is ordinary, not exotic - and
    the non-blocking skip is what keeps a second session start from waiting on the first.

    THIS DOES NOT VIOLATE THE SPOOL'S "no locks anywhere" RULE. That rule governs EVIDENCE
    WRITES FROM EDITOR HOOKS (`spool.append_evidence`), which must never wait behind another
    writer. Reconciliation is neither an editor hook nor an evidence write: it is the scanning
    consumer, it already takes the store lock to write, and skipping it costs nothing because
    the next checkpoint picks the work up. Do not "fix" this back out.

    `flock` and not a lock FILE's existence: a crashed pass releases it with its fd, where a
    stale marker would wedge reconciliation on this repo for good. Failing to open or acquire
    a trustworthy lock fails CLOSED for this pass (`None`): the evidence stays untouched and
    the receipt is incomplete. The attention ceiling is a hard invariant, so running unlocked
    is worse than deferring work to a checkpoint whose filesystem can serialize it.
    """
    path = store.STORE_DIR / sidecars.filename("reconcile_lock", slug=store.repo_slug(repo_path))
    try:
        store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        # Binary, and it stays empty: nothing is ever written to or read from this file -
        # the flock on its descriptor is the whole content. (Also why the package-wide
        # pin-your-encoding invariant does not apply: there is no text here.)
        handle = open(path, "ab")       # noqa: SIM115 - closed on every path below
    except OSError:
        yield None
        return
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:             # held by another pass: skip, never wait
        handle.close()
        yield False
        return
    except OSError:                     # no trustworthy flock: retain work for a later pass
        handle.close()
        yield None
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


def _basis_row(entry: dict, retired: bool) -> dict:
    """What one snapshot knows about the record a proposal is formed against: the revision, and
    the state. Kept as one row rather than two parallel maps because both answer for the same
    decision at the same instant, and a caller that read one from this snapshot and the other
    from a later one is the mismatch the reconsideration lane's expectations exist to refuse."""
    return {"revision_id": str(entry.get("current_revision_id") or ""),
            "state": lifecycle.inactive_state(entry, retired=retired)}


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


def _dispositions(held: dict, entries: list, retired_ids: set, tombstones: list = ()) -> dict:
    """The disposition each held candidate has earned since the last pass,
    `{candidate_id: "approved"|"dismissed"}`.

    The INPUT is the held directories and their `candidate.json` bookkeeping. This judges only
    the candidates that have REACHED review (`pending_review`, and `settled` for one whose
    finalize was interrupted): a candidate still `held` or `materializing` has no review to
    have earned anything yet and belongs to `_recoverable_holds`, a `reviewed` one has already
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
    # Kept SEPARATE from `by_id` rather than merged into it: every other lane's rules read a
    # missing entry as "gone, dismissed", and a tombstone is not gone. Only the reconsideration
    # branch looks here, because its target is inactive by definition.
    tombstoned = {str(e.get("id") or ""): e for e in tombstones}
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
            # `held`/`materializing` belong to `_recoverable_holds`, `reviewed` has earned
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
        if meta.get("lane") == "reconsideration":
            # FIRST, before the retirement test below: this lane's target is normally IN the
            # tombstone sidecar carrying a retirement record, which every rule under here reads
            # as "the decision left" - the opposite of what a reconsideration is asking.
            target = entry if entry is not None else tombstoned.get(entry_id)
            basis = str(meta.get("basis_revision_id") or "")
            sitting = (target or {}).get("proposed_reconsideration") or {}
            answer = _reconsideration_answer(target, candidate_id, basis,
                                             str(meta.get("created_at") or ""))
            if target is None:
                status = "dismissed"                    # evicted: nothing left to answer
            elif str(sitting.get("candidate_id") or "") == candidate_id:
                # THIS candidate's own question is what is on the table. Tested by candidate
                # id rather than by the slot merely being occupied: a slot filled by a LATER
                # restatement says nothing about a candidate the developer already answered.
                continue
            elif answer == "dismissed":
                # The RECEIPT first, always. A `restored` record is written by three different
                # acts and says nothing about which; a receipt naming this candidate, or the
                # one that held the slot at this basis, is unambiguous - so reading the record
                # first filed `approved` over an explicit dismissal.
                status = "dismissed"
            elif answer == "approved" and _restored_on_basis(target, basis):
                status = "approved"
            else:
                # No durable answer yet - it landed between the two writes, or the question is
                # displaced and the sitting one is still unanswered. Held, which is what keeps
                # its evidence exempt from retention until something can speak for it.
                continue
            flips[candidate_id] = status
            continue
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


def _reconsideration_awaiting_review(repo_path: str, target: str) -> bool:
    """True when `target` is an inactive decision that already carries a reconsideration.

    The reconsideration lane's twin of `_lifecycle_awaiting_review`, and it exists for the
    same reason: `propose_reconsideration` returns the same `ok: False` whether the decision
    is gone, is live again, or is holding a question somebody has yet to answer, and only the
    last of those means a review is pending. Asked of the STORE, so it stays true however that
    message is worded. Reads both halves of the lane - an ignored decision is in the live
    store, a retired one is in the tombstone sidecar.
    """
    if not target:
        return False
    return any(str(e.get("id") or "") == target
               for e in lifecycle.pending_reconsiderations(repo_path))


def _restored_on_basis(entry: dict | None, basis: str) -> bool:
    """True when this decision carries a COMPLETED `restored` record for the revision the
    reconsideration was judged against.

    HALF of the approval signal, never the whole of it. This record shape is exactly what
    `lifecycle.restore_decision` writes - the `restore_decision` MCP tool, `contexer restore`,
    and this lane's own restore all produce the same `restored` at the same revision - so it
    cannot say WHICH act produced it. The receipt below is what says that, and `_dispositions`
    requires both: an answer naming this candidate, and a completed restoration to go with it.
    Reading this record alone recorded `approved` for a question the developer had explicitly
    dismissed, which is the fabricated-approval class the whole function guards.
    """
    return bool(entry) and any(
        isinstance(record, dict) and record.get("kind") == "restored"
        and str(record.get("revision_id") or "") == str(basis or "")
        for record in (entry.get("lifecycle") or []))


def _answered_after(occurred_at, held_since) -> bool:
    """Whether a receipt was written at or after the moment a candidate was held.

    The TIME half of the shared-receipt rule, and the whole of what keeps it honest. A
    dismissal does not advance the decision's revision, so a receipt sits at its basis forever
    - and a basis match alone therefore let one answer settle every FUTURE candidate at that
    revision, including ones opened after it and answered the opposite way. A receipt can only
    be an answer to a question that already existed when it was written.

    Both stamps come from `datetime.now(timezone.utc).isoformat()` on one machine, so they are
    aware and directly comparable; they are PARSED rather than string-compared because
    `isoformat()` omits the microseconds when they are zero, and a correctness gate should not
    rest on that shape being uniform. Unparseable or missing on either side returns False, so
    the candidate stays held - the module's standing "cannot judge it, do not guess" rule, and
    the safe direction for a clock that stepped backwards between the two writes.
    """
    try:
        return (datetime.fromisoformat(str(occurred_at or ""))
                >= datetime.fromisoformat(str(held_since or "")))
    except (TypeError, ValueError):
        return False


def _reconsideration_answer(entry: dict | None, candidate_id: str, basis: str,
                            held_since: str = "") -> str:
    """The developer's recorded answer to this candidate's question - `"approved"`,
    `"dismissed"`, or `""` for none yet.

    Two ways a receipt speaks for a candidate, and the second is what stops a displaced one
    being stranded. An entry has ONE reconsideration slot, so a second directive restating the
    same decision while the first question still sits is HELD rather than proposed - and when
    the developer answers, the receipt names only the candidate that held the slot. The held
    one asked the SAME question about the SAME decision at the SAME revision and was answered
    by the same act, so a receipt at a matching `basis_revision_id` settles it too. Without
    that it stays held for good with no receipt anywhere, and invisible to
    `evidence_diagnostics`' `held_unattributed` because it does name an entry.

    That "same act" argument holds ONLY for a candidate the act could have been about, which is
    why the shared branch is gated on `_answered_after`: `held_since` is the candidate's own
    manifest `created_at`, stamped at hold time and carried across resumes, and a receipt older
    than that answered a question this candidate had not asked yet.

    A candidate's own receipt always wins over a same-basis one, and among shared ones the
    latest that qualifies wins - so a question the developer eventually restored settles the
    candidates displaced by it as approved, not off some earlier dismissal. A receipt at a
    DIFFERENT basis answers a different question and never speaks here at all.
    """
    own = shared = ""
    for row in (entry or {}).get("reconsideration_history") or []:
        if not isinstance(row, dict):
            continue
        disposition = str(row.get("disposition") or "")
        if disposition not in spool.DISPOSITIONS:
            continue
        if str(row.get("candidate_id") or "") == candidate_id:
            own = disposition
        elif (basis and str(row.get("basis_revision_id") or "") == str(basis)
                and _answered_after(row.get("occurred_at"), held_since)):
            shared = disposition
    return own or shared


def _link_rows(signals) -> list[dict]:
    """A candidate's signal rows as the manifest keeps them: relation, certainty, reason.

    The event id and the weight are deliberately dropped. The weight is a ranking input the
    candidate's own `score` already summarizes, and the events themselves are held beside this
    file until the candidate settles, so re-listing their ids here would be a second, divergent
    account of `event_ids`.
    """
    return [{"relation": str(row.get("relation") or ""),
             "certainty": str(row.get("certainty") or ""),
             "reason": str(row.get("reason") or "")}
            for row in (signals or []) if isinstance(row, dict)]


def _manifest(candidate_id: str, candidate: dict, event_ids: list, basis: dict,
              created_at: str = "", state: str = "held") -> dict:
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
        "state": state,
        "status": "pending",
        "kind": candidate.get("kind") or "",
        "target_decision_id": target,
        "replacement_decision_id": candidate.get("replacement_decision_id") or None,
        "basis_revision_id": (basis.get(target or "") or {}).get("revision_id") or None,
        "basis_target_state": (basis.get(target or "") or {}).get("state") or None,
        "event_ids": list(event_ids),
        "entry_id": "",
        "candidate": {
            "title": candidate.get("title") or "",
            "content": candidate.get("content") or "",
            "subtype": candidate.get("subtype") or "",
            "source_files": list(candidate.get("source_files") or []),
            "possible_source_files": list(candidate.get("possible_source_files") or []),
            "score": candidate.get("score") or 0,
            "first_observed_at": candidate.get("first_observed_at") or "",
            "security_significant": bool(candidate.get("security_significant")),
            # The typed link rows, TRIMMED to what a review surface renders (Task 03's
            # relation/certainty plus the reason). The relationship type exists only in the
            # aggregator's output, so a manifest that dropped it left the human review unable
            # to say what was OBSERVED and what was merely inferred - which is the whole point
            # of the typing. Additive: a manifest written before this key reads back as no
            # rows and renders nothing, never an empty claim.
            "signals": _link_rows(candidate.get("signals")),
            "uncertain_signals": _link_rows(candidate.get("uncertain_signals")),
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

    `candidate_id`/`prior` are the resume path (`_recoverable_holds`): a hold's identity is the
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
                else "reconsidered" if kind == "reconsider"
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
    if kind == "reconsider":
        # ROUTED ONLY to the lifecycle owner, never through the content capture path: the
        # target is not live, so `update_decision_with_meta` would meet the store's
        # status-blind dedup and absorb the restatement with no receipt anywhere - which is the
        # defect this lane exists to close (OUTSTANDING-ISSUES item 7). Nothing is restored
        # here; a question is attached to the inactive decision and only a human answers it.
        target = str(candidate.get("target_decision_id") or "")
        judged = basis.get(target) or {}
        result = lifecycle.propose_reconsideration(
            repo_path, target,
            content=candidate.get("content") or "",
            title=candidate.get("title") or "",
            candidate_id=candidate_id,
            # The record this candidate was CLASSIFIED against, checked again under the store
            # lock before anything attaches. Aggregation and the filesystem work in front of it
            # deliberately hold no store lock, so the decision can move in between - and the
            # proposal used to bind to whatever was current at attach time while the manifest
            # stayed keyed to the earlier basis. The developer's answer then landed at the new
            # revision, the held evidence went on waiting for one at the old, and nothing could
            # ever settle it.
            expected_basis_revision_id=str(judged.get("revision_id") or ""),
            expected_target_state=str(judged.get("state") or ""),
            # CONFIRMED files only. The candidate's `possible_source_files` stop here, as they
            # do on every other lane: an uncertain path must never become a restoration anchor
            # (runbook invariant 6), and the manifest already carries them for the report.
            source_files=candidate.get("source_files") or [],
            source="ai")
        if not result["ok"]:
            if result.get("reason") in _STALE_REFUSALS:
                # The record moved between this pass's snapshot and the attach, so this
                # candidate was formed against something that is no longer there. Nothing is
                # settled, nothing is deleted and no `writes` record is made: the manifest
                # stays `materializing`, which is the phase `_recoverable_holds` re-classifies
                # against the CURRENT store under this same candidate id. So the question is
                # asked once, at the new basis, on a later pass - never bound to a revision or
                # a state its own evidence was never judged against.
                receipt["incomplete"] = True
                return
            if _reconsideration_awaiting_review(repo_path, target):
                # Refused because a reconsideration for this target already sits on it - most
                # often the one this very candidate attached before an interrupted pass. The
                # question awaits review, so the evidence is held against it exactly as the
                # retirement lane holds its own.
                receipt["already_pending"] += 1
                writes[candidate_id] = {"event_ids": event_ids, "kind": kind,
                                        "status": "pending", "entry_id": target,
                                        "lane": "reconsideration"}
                return
            # The target is gone, or is live again and has nothing to reconsider. Settled
            # rather than re-aggregated into the same refusal on every future pass; the
            # receipt still lands on the decision, which is what keeps the outcome durable.
            writes[candidate_id] = {"event_ids": event_ids, "kind": kind,
                                    "status": "dismissed", "entry_id": target}
            return
        receipt["reconsidered"] += 1
        # `lane` is what `_dispositions` reads to settle this hold on a completed `restored`
        # record or a durable dismissal receipt. Deliberately NO `revision_id`: restoration is
        # a MOVE, so a revision advance is not an approval signal here either (ruling R25).
        writes[candidate_id] = {"event_ids": event_ids, "kind": kind, "status": "pending",
                                "entry_id": target, "lane": "reconsideration"}
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
    stored, entry_id, meta = store.update_decision_with_meta(
        repo_path,
        candidate.get("content") or "",
        _write_session(event_ids, sessions),
        candidate.get("subtype") or "",
        created_by="ai",
        replace_id=str(candidate.get("target_decision_id") or "") if kind == "update" else "",
        title=candidate.get("title") or "",
        force_pending=True,
        # The candidate's CONFIRMED files, and never `possible_source_files`. Passed rather
        # than left to the store's recently-edited-files accrual because that window
        # (`_EDITED_FILES_WINDOW`) is the same 1800 seconds as the aggregator's
        # `_PROXIMITY_SECONDS`, fed by the same PostToolUse call, so every path the aggregator
        # judged `temporal_backward` was landing on the decision as an anchor guess anyway -
        # the outcome runbook invariant 6 forbids, arrived at by a route the aggregator cannot
        # see. Here the answer comes from the evidence itself: an empty list means this
        # candidate's evidence named no file, which is a truer statement than "something was
        # edited nearby". Still only a GUESS awaiting a human approval, exactly as before.
        anchor_candidates=list(candidate.get("source_files") or []),
        anchor_candidates_confirmed=True,
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
    #
    # `inactive_match` is the store REPORTING that the decision it matched is ignored or
    # tombstoned. Only agent-shaped evidence reaches here that way (a developer's restatement
    # is classified `reconsider` and never touches this path at all), and the receipt is what
    # keeps it from vanishing: without the id the summary has nowhere to go and the hold
    # accumulates as `held_unattributed` for good.
    inactive = (meta or {}).get("inactive_match") or {}
    receipt["duplicates"] += 1
    writes[candidate_id] = {"event_ids": event_ids, "kind": kind, "status": "dismissed",
                            "entry_id": str(inactive.get("entry_id") or "")}


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
    # Any `lane` at all means a non-content record: the content lane is the one spelled by the
    # ABSENCE of the key, so testing for that covers every lane added since without a second
    # name to keep in step.
    pending_ids = {cid for cid, record in writes.items()
                   if record.get("status") == "pending" and not record.get("lane")}
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


def _deferred_candidate(candidate_id: str, meta: dict, events: list[dict]) -> dict | None:
    """Rebuild the exact atomic proposal frozen in a deferred manifest.

    Deferral is an admission decision, not a crash. Reclassifying its evidence against a
    store that now contains earlier admitted batches can silently turn unrelated `new`
    proposals into `update` holds on those batches. The manifest deliberately preserves the
    proposal as it was classified; the held event bodies supply only the event ids/session
    data that the bounded manifest does not duplicate.
    """
    payload = meta.get("candidate")
    event_ids = meta.get("event_ids")
    if not isinstance(payload, dict) or not isinstance(event_ids, list):
        return None
    by_id = {str(event.get("event_id") or ""): event for event in events}
    links = payload.get("signals")
    if (not isinstance(links, list) or len(links) != len(event_ids)
            or set(by_id) != {str(event_id) for event_id in event_ids}):
        return None
    signals = []
    for event_id, link in zip(event_ids, links, strict=True):
        if not isinstance(link, dict):
            return None
        signals.append({**by_id[str(event_id)], **link})
    return {
        "candidate_id": candidate_id,
        "kind": str(meta.get("kind") or ""),
        "target_decision_id": meta.get("target_decision_id"),
        "replacement_decision_id": meta.get("replacement_decision_id"),
        "title": payload.get("title") or "",
        "content": payload.get("content") or "",
        "subtype": payload.get("subtype") or "",
        "source_files": list(payload.get("source_files") or []),
        "possible_source_files": list(payload.get("possible_source_files") or []),
        "score": payload.get("score") or 0,
        "first_observed_at": payload.get("first_observed_at") or "",
        "security_significant": bool(payload.get("security_significant")),
        "signals": signals,
        "uncertain_signals": list(payload.get("uncertain_signals") or []),
    }


def _recoverable_holds(repo_path: str, held: dict, projection: list, sessions: dict,
                       receipt: dict, *, include_deferred: bool) -> tuple[dict, list[dict]]:
    """Return interrupted/deferred candidates for the shared admission queue.

    Nothing materializes here. Recovery used to run before new aggregation and could therefore
    exceed a newly introduced ceiling before admission had a chance to count it. Returning one
    queue lets interrupted, deferred and new candidates share the same deterministic priority
    and the same capacity reservation.

    One rule per phase, and each one is the recovery for a crash at a numbered transition:

    * `reviewed` (crash at 9) - the disposition and its summary are both durable, so only the
      raw cleanup is left. It is finished WITHOUT re-recording anything.
    * `held` / `materializing` (crash at 2-5) are re-classified against the CURRENT store and
      queued under the existing directory id. A decision the earlier pass already created
      comes back as a duplicate; one that never landed comes back as new.
    * `deferred_attention`, when capacity exists, resumes the exact atomic candidate and
      lifecycle basis frozen in its manifest; deferral is admission state, not a crash retry.
    * a hold holding no events at all is DISCARDED - it can never be materialized and it would
      otherwise occupy its candidate id for good (`spool.discard_empty_hold`).
    * anything else - a manifest that is missing, unreadable or carries an unknown state, and
      any candidate already awaiting review - is left exactly as it is, for `_dispositions` and
      the diagnostics counters to speak about.

    The identity of a resumed candidate is the DIRECTORY it already occupies, never the id its
    re-classification would mint: kind and target are part of that id, and both can legitimately
    change between the crash and the replay.
    """
    standing, queued = {}, []
    for candidate_id, meta in sorted(held.items()):
        state = meta.get("state")
        if state == "reviewed":
            spool.finalize_candidate_evidence(repo_path, candidate_id,
                                              str(meta.get("status") or "dismissed"))
            continue
        recoverable = state in ("held", "materializing") \
            or (include_deferred and state == "deferred_attention")
        if not recoverable:
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
        if state == "deferred_attention":
            frozen = _deferred_candidate(candidate_id, meta, events)
            resumed = [frozen] if frozen is not None else []
        else:
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
            candidate = dict(resumed[0])
            candidate["candidate_id"] = candidate_id
            row = {"candidate": candidate, "prior": meta, "existing": True}
            if state == "deferred_attention":
                target = str(meta.get("target_decision_id") or "")
                row["basis"] = ({target: {
                    "revision_id": str(meta.get("basis_revision_id") or ""),
                    "state": str(meta.get("basis_target_state") or ""),
                }} if target else {})
            queued.append(row)
        else:
            receipt["incomplete"] = True
        standing[candidate_id] = meta
    return standing, queued


def _requires_attention(candidate: dict) -> bool:
    """Whether materializing this candidate can open a developer review item.

    `duplicate` is terminal bookkeeping and `insufficient` writes nothing, so both continue at
    zero capacity. Every other candidate is conservatively budgeted: a target may already hold
    a proposal and turn the write into `already_pending`, but deferring that evidence is safer
    than guessing and admitting one row past the ceiling.
    """
    return candidate.get("kind") not in ("duplicate", "insufficient")


def _candidate_event_key(candidate: dict) -> tuple[str, ...]:
    """Stable identity of the raw evidence group, independent of re-classification."""
    return tuple(sorted(str(row.get("event_id") or "")
                        for row in (candidate.get("signals") or [])
                        if isinstance(row, dict)))


def _defer_candidate(repo_path: str, candidate: dict, basis: dict, receipt: dict) -> None:
    """Durably hold one not-yet-admitted candidate without touching the decision store."""
    candidate_id = str(candidate.get("candidate_id") or "")
    event_ids = [str(row.get("event_id") or "")
                 for row in (candidate.get("signals") or []) if isinstance(row, dict)]
    manifest = _manifest(candidate_id, candidate, event_ids, basis,
                         state="deferred_attention")
    if not _hold(repo_path, candidate_id, event_ids, meta=manifest):
        receipt["incomplete"] = True


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
      this candidate was attributed to it, so `store.record_evidence_summary` has no entry to
      write to and no retry could ever succeed. The disposition is filed in the spool's own
      orphan ledger instead (`spool.record_orphan_receipt`) and only THEN is the raw evidence
      deleted - the identical summary-before-delete order, against the only durable record
      still available. It used to delete with no receipt anywhere, which is what invariants 3
      and 4 forbid; a ledger write that fails now leaves the hold exactly where it is.

    The manifest reaches `reviewed` BETWEEN the summary and the delete, which is what makes the
    cleanup replayable on its own: a crash at the delete leaves a hold whose disposition and
    summary are both durable, so the next pass finishes the removal without asking the
    disposition rules anything and without filing a second receipt. A manifest that refuses the
    update stops the delete - `reviewed` is the record that the receipt IS durable, and deleting
    the evidence without it would leave the next pass judging the candidate over again.
    """
    if not entry_id:
        return
    if entry_id in filable:
        if (entry_id, candidate_id) not in recorded:
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
    elif not spool.record_orphan_receipt(repo_path, candidate_id, entry_id, disposition):
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
    path = store.STORE_DIR / sidecars.filename("reconcile_log", slug=store.repo_slug(repo_path))
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
        if acquired is None:
            receipt["incomplete"] = True
            receipt["lock_unavailable"] = True
            return receipt
        if acquired is False:
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
    routing_issues = []
    pending = spool.list_pending_evidence(
        repo_path, session_id, route_invalid=False, issues=routing_issues)
    observed_keys = {str(event.get("repo_key") or "") for event in pending}
    comparisons = repo_key.compare_evidence_repo_identities(repo_path, observed_keys)
    identity_work = any(
        not comparisons[observed]["matches"]
        for observed in observed_keys
    )
    return bool(spool.held_candidates(repo_path) or routing_issues or identity_work
                or [e for e in pending if e.get("kind") in _EVIDENCE_KINDS])


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
    * `basis` - per decision, the `revision_id` a proposal is formed against (stamped on the
      manifest at hold time, so a resumed pass can tell what the candidate was judged against
      after HEAD moves) plus the `state` it was judged in. Both travel together because both
      are one snapshot's answer about one decision, and the reconsideration lane hands both to
      `lifecycle.propose_reconsideration` so the attach under the store lock refuses outright
      if either moved in between.
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
        # Tombstones included: a reconsideration is formed against a RETIRED decision's own
        # revision, and a basis map that only knew live decisions stamped None on exactly the
        # manifests whose disposition rule reads it.
        # `retired` is decided by WHICH FILE the entry came out of rather than by its
        # `deleted_at` stamp, so a legacy tombstone written before that stamp existed still
        # reads as retired instead of as an ignored decision that is somehow not in the store.
        "basis": {**{str(e.get("id") or ""): _basis_row(e, False) for e in decisions},
                  **{str(e.get("id") or ""): _basis_row(e, True) for e in tombstones}},
    }


def _run_pass(repo_path: str, session_id: str, dry_run: bool, receipt: dict) -> dict:
    routing_issues = []
    pending = spool.list_pending_evidence(
        repo_path, session_id, route_invalid=not dry_run, issues=routing_issues)
    accepted = []
    identity_incomplete = bool(routing_issues) and not dry_run
    if identity_incomplete:
        receipt["incomplete"] = True
    observed_keys = {str(event.get("repo_key") or "") for event in pending}
    comparisons = repo_key.compare_evidence_repo_identities(repo_path, observed_keys)
    foreign_routes = []
    # Identity is the consumer chokepoint for EVERY valid event. Candidate eligibility is a
    # later concern: filtering bookkeeping kinds first let foreign policy/session records sit
    # in pending forever without either materializing or receiving a terminal quarantine.
    for event in pending:
        observed = str(event.get("repo_key") or "")
        comparison = comparisons[observed]
        if comparison["matches"]:
            accepted.append(event)
            continue
        if dry_run:
            continue
        foreign_routes.append({
            "event_id": str(event.get("event_id") or ""),
            "observed_key": comparison["observed_key"],
            "expected_key": comparison["expected_key"],
            "reason": comparison["reason"],
        })
    routed_events = spool.quarantine_identity_events(repo_path, foreign_routes)
    for routed in routed_events:
        if not routed:
            identity_incomplete = True
            receipt["incomplete"] = True
    events = [event for event in accepted if event.get("kind") in _EVIDENCE_KINDS]
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
    existing_deferred = sum(1 for meta in held.values()
                            if meta.get("state") == "deferred_attention")
    receipt["deferred"] = existing_deferred
    pending_reviews = len(store.get_pending_decisions(repo_path))
    available = min(MATERIALIZATION_ALLOWANCE,
                    max(0, PENDING_REVIEW_CEILING - pending_reviews))
    queued: list[dict] = []
    if not dry_run:
        # Recovery no longer materializes ahead of admission. At zero capacity intentionally
        # do not even read deferred raw events: a full review queue must make repeated session
        # starts cheap. Interrupted `held`/`materializing` rows are still classified because
        # they may already have written the store and now be terminal duplicates.
        held, queued = _recoverable_holds(
            repo_path, held, snap["projection"], sessions, receipt,
            include_deferred=available > 0)
        flips = _dispositions(held, snap["entries"], _retired_ids(snap["tombstones"]),
                              snap["tombstones"])
    new_candidates = (candidates.aggregate_candidates(events, snap["projection"])["candidates"]
                      if events else [])
    for candidate in new_candidates:
        if candidate["candidate_id"] in held:
            # Already awaiting review under its deterministic id. Belt to the held-events
            # braces: it also covers a candidate whose directory exists but whose bookkeeping
            # never recorded the event ids `_finish_interrupted_holds` matches on.
            receipt["already_pending"] += 1
            continue
        queued.append({"candidate": candidate, "prior": None, "existing": False})

    queued.sort(key=lambda row: candidates.attention_priority(row["candidate"]))
    admitted_event_keys = set()
    for row in queued:
        candidate = row["candidate"]
        row["admitted"] = not _requires_attention(candidate) or available > 0
        if _requires_attention(candidate) and row["admitted"]:
            available -= 1
        if row["admitted"] and not row["existing"]:
            admitted_event_keys.add(_candidate_event_key(candidate))

    # Execute admitted RECOVERY first. The choice of what earned attention was global above,
    # but an interrupted candidate may already have written the store. New pending evidence
    # must therefore be re-classified after these writes, exactly as it was before admission
    # existed, or a restatement beside a resume can remain `new` against a stale snapshot and
    # strand an unattributed hold.
    for row in (item for item in queued if item["existing"] and item["admitted"]):
        candidate = row["candidate"]
        if row["prior"].get("state") == "deferred_attention":
            receipt["deferred"] -= 1
        _materialize(
            repo_path, candidate, sessions, dry_run, receipt, writes,
            row["basis"] if "basis" in row else snap["basis"],
            candidate_id=str(candidate.get("candidate_id") or ""), prior=row["prior"])

    if writes:
        snap = _snapshot(repo_path)
        new_candidates = (candidates.aggregate_candidates(events, snap["projection"])["candidates"]
                          if events else [])

    for candidate in new_candidates:
        admitted = _candidate_event_key(candidate) in admitted_event_keys
        if _requires_attention(candidate) and not admitted:
            if not dry_run:
                _defer_candidate(repo_path, candidate, snap["basis"], receipt)
            receipt["deferred"] += 1
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
    unsafe_pending = (receipt["incomplete"]
                      and bool(spool.list_pending_evidence(repo_path)))
    if identity_incomplete or unsafe_pending:
        # A failed receipt/hold/move must leave its raw source exactly where it was. Retention
        # could otherwise evict that same pending file in this pass, contradicting the failure
        # result. Session-start maintenance remains an independent TTL-gated sweep; this guard
        # is only for the pass that just failed to make its own source durable elsewhere.
        return receipt
    retention = spool.run_retention(repo_path)
    if retention.get("orphans_unreceipted"):
        # An orphaned hold whose terminal receipt could not be written is still holding its raw
        # evidence, so nothing was lost - but this pass did not account for it, and a receipt
        # that reported `complete` over it would be the silence invariant 3 exists against.
        receipt["incomplete"] = True
    # Evidence evicted THIS pass, counted where the eviction happens rather than off the
    # cumulative `.gap` ledger: a coverage block reports the run it belongs to.
    _recoverage(receipt, "partial" if receipt["incomplete"] else "complete",
                retention["dropped_pending"] + retention["dropped_quarantine"])
    if not (writes or flips):
        # Nothing happened, so nothing is recorded. A pass that logged its receipt
        # unconditionally would write one line per SessionStart, PreCompact and SessionEnd
        # forever on any repo holding a single stuck held candidate - news of having done
        # nothing, crowding the tail cap that holds the passes that did something.
        return receipt
    _log_receipt(repo_path, session_id, receipt)
    return receipt


def reconcile_session(repo_path: str, session_id: str = "", dry_run: bool = False,
                      host: str = "") -> dict:
    """Materialize this repo's unconsumed evidence as decisions pending review.

    `session_id` scopes which events participate (`""` = the whole spool, which is what a
    worktree-shared spool needs). Returns the receipt:

        {"events_observed", "proposed", "lifecycle_proposed", "reconsidered",
         "already_pending", "duplicates", "insufficient", "deferred", "incomplete",
         "lock_unavailable", "skipped", "dry_run",
         "coverage"}

    `host` names the adapter whose checkpoint called this, for the receipt's `coverage` block
    (what that host can actually observe, plus this pass's own status and drop count). A
    caller with no host - the MCP tool, the CLI - reports `manual` rather than guessing.

    `already_pending` counts both shapes of "this is already waiting on the developer": a
    candidate whose own held directory exists, and a duplicate of a decision that is itself
    still awaiting review (held against it rather than settled - see `_settle_write_statuses`).
    Both mean the same thing to a reader: nothing new to look at, and nothing thrown away.

    One pass at a time per repo (`_reconcile_lock`, taken only once there is work to do):
    finding another pass already running means this one does NOTHING and says so (`skipped`),
    rather than waiting or racing it.

    NEVER raises: every caller is a host hook or a report surface, and a reconciliation that
    could not finish is a receipt marked `incomplete`, never a broken session start.

    COST AT THE CEILING is measured rather than estimated because this runs at every host's
    session start. Before attention admission, a realistic 1,000-event spool (100 statements,
    900 corroborating edits) took 1,317ms and opened 100 review items. With the measured bounds
    above it takes ~881ms to hold all 1,000 events while opening five items, ~129ms to fill the
    remaining five slots on the next checkpoint, and ~25ms at the full ten-item ceiling; no
    deferred raw event is dropped. The executable row lives beside the constants in
    `tests/test_benchmark_evidence.py`.
    """
    receipt = _receipt(dry_run, host)
    try:
        receipt = _reconcile(repo_path, session_id, dry_run, receipt)
    except Exception:                  # broad on purpose: the never-raises contract
        receipt["incomplete"] = True
        _recoverage(receipt, "error")
        return receipt
    if receipt["skipped"]:
        _recoverage(receipt, "skipped")
    elif receipt["incomplete"]:
        _recoverage(receipt, "partial")
    return receipt


def format_receipt(receipt: dict) -> str:
    """The receipt as human-readable lines - shared by the CLI command and the MCP tool so a
    developer and a model are never told two different stories about one pass."""
    if receipt.get("skipped"):
        return ("Reconciled evidence: skipped - another reconciliation pass is already "
                "running on this repo. The next checkpoint picks this up.")
    if receipt.get("lock_unavailable"):
        return ("Reconciled evidence: incomplete - the per-repository lock was unavailable. "
                "No evidence was consumed; retry from a writable local store.")
    coverage = receipt.get("coverage")
    head = "Reconciled evidence" + (" (dry run - nothing was written)" if receipt["dry_run"]
                                    else "")
    lines = [
        f"{head}:",
        f"  evidence events observed: {receipt['events_observed']}",
        f"  proposed for review:      {receipt['proposed']}",
        f"  retirements proposed:     {receipt['lifecycle_proposed']}",
        f"  reconsiderations:         {receipt['reconsidered']}",
        f"  already pending:          {receipt['already_pending']}",
        f"  duplicates:               {receipt['duplicates']}",
        f"  insufficient evidence:    {receipt['insufficient']}",
    ]
    if receipt.get("deferred"):
        lines.append(f"  deferred for attention:   {receipt['deferred']} (evidence retained)")
    if coverage:
        # What could be seen at all, beside what was found: "0 proposed" on a host that
        # cannot observe edits means something different from "0 proposed" on one that can.
        lines.append(f"  capture coverage:         {evidence.format_coverage(coverage)}")
    if receipt["lifecycle_proposed"]:
        lines.append("  (proposals only - nothing was retired. `contexer review` shows each "
                     "one; retiring is an explicit `contexer retire <id>`.)")
    if receipt["reconsidered"]:
        lines.append("  (questions only - nothing was restored. `contexer review` shows each "
                     "inactive decision that was restated; only you can bring one back.)")
    if receipt["incomplete"]:
        lines.append("  incomplete: the evidence spool could not be fully read or updated.")
    return "\n".join(lines)
