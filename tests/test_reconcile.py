"""Tests for contexer/reconcile.py — evidence in, decisions PENDING REVIEW out.

Two properties carry this module and are asserted from several angles rather than once:

* **Idempotency.** A second pass over the same evidence proposes nothing and holds nothing
  twice. The mechanism under test is the HOLD — a materialized candidate's events are moved
  into `held/<candidate-id>/`, so they never reach the aggregator again — NOT the store's
  novelty filter, so the assertions are on the receipt and on the spool, not merely on the
  store staying the same size.
* **`dry_run` writes nothing anywhere.** Asserted on bytes: every file under STORE_DIR,
  spool included, is compared before and after.

Everything reconciliation proposes is unreviewed by construction, so the retire path is
asserted the other way round: the proposal lands in the separate lifecycle lane and the
decision itself — content, revisions, status — is untouched until a human retires it.

A settled candidate leaves no raw evidence behind at all: its held files are deleted and the
disposition lives on in the DECISION's own `evidence_summary` history, which is what most of
the disposition assertions below read.
"""
import json
import uuid
from pathlib import Path

import pytest

from contexer import candidates, evidence, lifecycle, reconcile, spool, store
from contexer.adapters import claude, codex, cursor, gemini

SESSION = "sess-1"


def _emit(repo, kind, summary="", session_id=SESSION, files=None, attributes=None):
    result = evidence.emit_hook_event(repo, kind, session_id=session_id, source="test",
                                      summary=summary, files=files, attributes=attributes)
    assert result["status"] == "stored", result
    return result


def _spool_state(repo):
    """Every file the spool holds, by path and content — what a dry run must not change."""
    root = spool._repo_dir(repo)
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file()} if root.exists() else {}


def _store_bytes(repo):
    path = store.STORE_DIR / f"{store.repo_slug(repo)}.json"
    return path.read_bytes() if path.exists() else b""


def _held(repo):
    return spool.held_candidates(repo)


def _summaries(repo, entry_id):
    """The `evidence_summary` history reconciliation attached to one decision."""
    entry = next((e for e in store.load(repo)["entries"] if e["id"] == entry_id), None)
    return (entry or {}).get("evidence_summary", [])


def _dispositions_of(repo):
    """Every disposition recorded on every decision in the store, `[(entry_id, kind), ...]`."""
    return [(e["id"], s["disposition"]) for e in store.load(repo)["entries"]
            for s in e.get("evidence_summary", [])]


def _pending(repo):
    return [e for e in spool.list_pending_evidence(repo)
            if e["kind"] in (candidates.SEED_KINDS | candidates.SUPPORT_KINDS)]


# The stored decision the update/retire/duplicate candidates below are measured against.
# Token overlap (|A∩B| / |smaller| over retrieval.index_tokens) is what classifies them, so the
# four texts are pinned by an explicit overlap assertion in the first test of each class rather
# than left to read as arbitrary prose.
STORED = "Postgres backs the decision store; connection pooling is handled by pgbouncer."
UPDATES = "Postgres backs the decision store, and every migration must run through alembic in CI now."
RETIRES = "Stop using Postgres for the decision store; pooling is gone."
DUPLICATES = "Postgres backs the decision store; connection pooling handled by pgbouncer."
UNRELATED = "Always run the linter before pushing a branch to origin."


def _only_held(repo):
    """The one held candidate's `(candidate_id, meta)` — its dir IS the pending record."""
    ((candidate_id, meta),) = _held(repo).items()
    return candidate_id, meta


def _boom(*_a, **_k):
    raise RuntimeError("boom")


def test_the_fixture_texts_sit_in_the_bands_that_classify_them():
    """The four texts above are not arbitrary prose: each one is chosen for its token overlap
    with STORED, and that is what decides its candidate kind. Pinned here so a reworded fixture
    fails loudly instead of silently re-testing the wrong branch."""
    from contexer import retrieval

    def overlap(left, right):
        a, b = set(retrieval.index_tokens(left)), set(retrieval.index_tokens(right))
        return len(a & b) / min(len(a), len(b))

    assert overlap(STORED, DUPLICATES) > 0.7                     # duplicate
    assert 0.5 < overlap(STORED, RETIRES) <= 0.7                 # retire (plus a negation)
    assert 0.3 < overlap(STORED, UPDATES) <= 0.5                 # update
    assert overlap(STORED, UNRELATED) == 0.0                     # new


def _stored_decision(repo, content=STORED):
    """One approved decision to classify candidates against (human capture => born approved)."""
    ok, entry_id = store.update_decision(repo, content, SESSION, "architecture",
                                         created_by="human")
    assert ok and entry_id
    entry = next(e for e in store.load(repo)["entries"] if e["id"] == entry_id)
    assert store.entry_status(entry) == "approved"
    return entry_id


# ── end to end ───────────────────────────────────────────────────────────────

class TestEndToEnd:
    """Directive + corroborating file change -> one decision pending review -> approved ->
    the candidate learns its fate on the next pass -> finalized, summary in the decision's
    own history, raw evidence gone."""

    def test_directive_becomes_a_pending_decision_and_settles(self, tmp_repo):
        _emit(tmp_repo, "user_directive", "Always pin the auth middleware to one provider.",
              files=["src/auth.py"])
        _emit(tmp_repo, "file_changed", "auth middleware changed", files=["src/auth.py"])

        receipt = reconcile.reconcile_session(tmp_repo)
        assert receipt["events_observed"] == 2
        assert receipt["proposed"] == 1
        assert receipt["insufficient"] == 0
        assert receipt["incomplete"] is False

        (pending,) = store.get_pending_decisions(tmp_repo)
        assert store.entry_status(pending) == "pending_approval"
        assert "auth middleware" in pending["content"]
        assert pending["id"][:8] in store.format_pending_review(tmp_repo)

        # One held candidate, naming the entry and both events it moved out of pending.
        candidate_id, meta = _only_held(tmp_repo)
        assert meta["entry_id"] == pending["id"]
        assert len(meta["event_ids"]) == 2
        assert len(spool._event_files(spool._held_dir(tmp_repo, candidate_id))) == 2
        assert _pending(tmp_repo) == []

        # The developer approves. Nothing hooks approve_decision: the next pass is what
        # notices, which is exactly what "lazy disposition" means.
        assert store.approve_decision(tmp_repo, pending["id"], "approve")[0] is True
        assert list(_held(tmp_repo)) == [candidate_id]

        second = reconcile.reconcile_session(tmp_repo)
        assert second["proposed"] == 0
        # Settled: the held directory and every raw event in it are gone, and the disposition
        # survives as history ON THE DECISION rather than as another event in the spool.
        assert _held(tmp_repo) == {}
        assert not spool._held_dir(tmp_repo, candidate_id).exists()
        (summary,) = _summaries(tmp_repo, pending["id"])
        assert summary["candidate_id"] == candidate_id
        assert summary["disposition"] == "approved"
        assert sorted(summary["event_ids"]) == sorted(meta["event_ids"])
        assert [e["kind"] for e in spool.list_pending_evidence(tmp_repo)] \
            == ["session_reconcile", "session_reconcile"]

    def test_evidence_summary_is_additive_and_leaves_old_stores_readable(self, tmp_repo):
        """The only store-schema change this pipeline makes. A decision written before the key
        existed must load and render exactly as it did — so the key is only ever ADDED."""
        ok, entry_id = store.update_decision(tmp_repo, UNRELATED, SESSION, "convention",
                                             created_by="human")
        assert ok
        before = next(e for e in store.load(tmp_repo)["entries"] if e["id"] == entry_id)
        assert "evidence_summary" not in before

        assert store.record_evidence_summary(
            tmp_repo, entry_id, {"candidate_id": "c1", "disposition": "approved",
                                 "event_ids": ["e1"], "occurred_at": "2026-08-24T10:00:00+00:00"})
        after = next(e for e in store.load(tmp_repo)["entries"] if e["id"] == entry_id)
        assert {k: v for k, v in after.items() if k != "evidence_summary"} == before
        assert after["evidence_summary"][0]["disposition"] == "approved"
        assert store.get_context(tmp_repo)                      # renders unchanged

    def test_a_summary_for_a_decision_that_is_gone_is_reported_not_raised(self, tmp_repo):
        assert store.record_evidence_summary(tmp_repo, "no-such-id", {"disposition": "x"}) \
            is False

    def test_the_entry_records_the_session_the_evidence_came_from(self, tmp_repo):
        # Provenance, not bookkeeping: the decision came out of that host session, so that is
        # what it records — never the id of whichever process happened to reconcile.
        _emit(tmp_repo, "user_directive", UNRELATED, session_id="host-session-9")
        reconcile.reconcile_session(tmp_repo)
        (entry,) = store.load(tmp_repo)["entries"]
        assert entry["session_id"] == "host-session-9"

    def test_an_ignored_decision_settles_the_candidate_as_dismissed(self, tmp_repo):
        _emit(tmp_repo, "user_directive", UNRELATED)
        reconcile.reconcile_session(tmp_repo)
        (pending,) = store.get_pending_decisions(tmp_repo)
        store.approve_decision(tmp_repo, pending["id"], "ignore")

        reconcile.reconcile_session(tmp_repo)
        # A dismissal settles just as surely as an approval: the hold is released, the events
        # are gone, and the decision keeps the record of why.
        assert _held(tmp_repo) == {}
        assert _pending(tmp_repo) == []
        assert _dispositions_of(tmp_repo) == [(pending["id"], "dismissed")]


class TestInferredDecisionsAreAlwaysReviewable:
    """The whole pipeline's safety property: a decision NOBODY stated must never come to rest
    anywhere the developer will not be shown it. `suggested` is exactly such a place — it
    injects at session start yet never appears in `review_pending` — so evidence-derived
    captures are forced to `pending_approval`, bootstrap's medium-tier precedent."""

    # An agent conclusion with rationale, non-prescriptive, no L3 content signal, no tooling
    # word: everything about it says `suggested` to the store's own classifier.
    CONCLUSION = ("The router reads its sidecar index before ranking, because rebuilding "
                  "inline would cost a prompt several milliseconds.")

    def test_the_store_would_otherwise_file_this_as_suggested(self, tmp_repo):
        # The defect this pins, stated as the store sees it — without this assertion the test
        # below would keep passing if `force_pending` quietly stopped mattering.
        assert store._classify_level(self.CONCLUSION, "architecture", "ai") == "suggested"

    def test_an_agent_conclusion_lands_pending_and_stays_there_until_approved(self, tmp_repo):
        _emit(tmp_repo, "agent_conclusion", self.CONCLUSION)
        assert reconcile.reconcile_session(tmp_repo)["proposed"] == 1

        (entry,) = store.load(tmp_repo)["entries"]
        assert store.entry_status(entry) == "pending_approval"
        assert entry["subtype"] == "architecture"
        assert entry["id"][:8] in store.format_pending_review(tmp_repo)
        assert store.get_pending_decisions(tmp_repo) == [entry]

        # Reconciling again does NOT settle it: nothing has reviewed it.
        reconcile.reconcile_session(tmp_repo)
        assert len(_held(tmp_repo)) == 1

        store.approve_decision(tmp_repo, entry["id"], "approve")
        reconcile.reconcile_session(tmp_repo)
        assert _held(tmp_repo) == {}                 # settled and finalized
        assert _dispositions_of(tmp_repo) == [(entry["id"], "approved")]

    def test_a_candidate_is_never_approved_without_review_evidence(self, tmp_repo):
        # An entry can stop being pending without anyone reviewing it. Reading that as
        # approval is how an inferred decision would launder itself into a reviewed one, so
        # the flip requires `approved_by == "human"` — what only approve/edit stamps.
        _emit(tmp_repo, "agent_conclusion", self.CONCLUSION)
        reconcile.reconcile_session(tmp_repo)
        data = store.load(tmp_repo)
        data["entries"][0]["status"] = "approved"    # no approved_by: nobody looked at it
        store.save(tmp_repo, data)

        reconcile.reconcile_session(tmp_repo)
        assert len(_held(tmp_repo)) == 1
        assert _dispositions_of(tmp_repo) == []


# ── the four candidate kinds ─────────────────────────────────────────────────

class TestUpdateCandidate:

    def test_update_lands_as_a_proposal_and_head_does_not_move(self, tmp_repo):
        entry_id = _stored_decision(tmp_repo)
        before = next(e for e in store.load(tmp_repo)["entries"] if e["id"] == entry_id)
        head, content = before["current_revision_id"], before["content"]

        _emit(tmp_repo, "user_directive", UPDATES)
        receipt = reconcile.reconcile_session(tmp_repo)
        assert receipt["proposed"] == 1

        after = next(e for e in store.load(tmp_repo)["entries"] if e["id"] == entry_id)
        assert after["current_revision_id"] == head      # unreviewed content is never live
        assert after["content"] == content
        assert "alembic" in after["proposed_revision"]["content"]
        assert len(store.load(tmp_repo)["entries"]) == 1  # routed onto the target, not appended
        assert _only_held(tmp_repo)[1]["entry_id"] == entry_id

    def test_an_update_applied_in_place_is_dismissed_not_left_pending(self, tmp_repo):
        # A convention-subtyped correction is trivial to the store: it applies immediately as
        # a new approved revision, with no proposal and nothing to review. The store's return
        # looks identical to the proposal case, so the disposition is settled from what the
        # entry ACTUALLY shows — otherwise this evidence would be held forever waiting on a
        # review nobody will ever be asked for.
        # Both sides convention-subtyped: the store gates on the OLD subtype as well, so a
        # convention correction to an architecture decision would still be a proposal.
        ok, entry_id = store.update_decision(tmp_repo, STORED, SESSION, "convention",
                                             created_by="human")
        assert ok
        _emit(tmp_repo, "user_directive",
              "Postgres backs the decision store, and the commit hook now formats every "
              "migration file.")
        reconcile.reconcile_session(tmp_repo)

        entry = next(e for e in store.load(tmp_repo)["entries"] if e["id"] == entry_id)
        assert "proposed_revision" not in entry            # applied in place
        assert store.get_pending_decisions(tmp_repo) == []  # nothing to review
        assert _held(tmp_repo) == {}                       # dismissed in the same run
        assert _pending(tmp_repo) == []
        assert _dispositions_of(tmp_repo) == [(entry_id, "dismissed")]

    def test_a_second_pass_proposes_nothing_more(self, tmp_repo):
        _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", UPDATES)
        reconcile.reconcile_session(tmp_repo)
        before = _store_bytes(tmp_repo)

        assert reconcile.reconcile_session(tmp_repo)["proposed"] == 0
        assert _store_bytes(tmp_repo) == before

    def test_the_held_meta_records_the_revision_it_was_proposed_against(self, tmp_repo):
        entry_id = _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", UPDATES)
        reconcile.reconcile_session(tmp_repo)

        entry = next(e for e in store.load(tmp_repo)["entries"] if e["id"] == entry_id)
        _candidate_id, meta = _only_held(tmp_repo)
        assert meta["revision_id"] == entry["current_revision_id"]
        assert "lane" not in meta                            # the content lane is the default

    def test_an_approved_proposal_settles_as_approved(self, tmp_repo):
        entry_id = _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", UPDATES)
        reconcile.reconcile_session(tmp_repo)

        store.approve_decision(tmp_repo, entry_id, "approve")   # HEAD advances
        reconcile.reconcile_session(tmp_repo)
        assert _dispositions_of(tmp_repo) == [(entry_id, "approved")]

    def test_a_dismissed_proposal_settles_as_dismissed_rather_than_holding_forever(
            self, tmp_repo):
        # The Task 5 residual. `approved_by == "human"` alone can never become true here — the
        # target was never a pending entry — so this candidate used to sit pending for good,
        # pinning its evidence against eviction and keeping the fast path awake on every pass.
        entry_id = _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", UPDATES)
        reconcile.reconcile_session(tmp_repo)

        store.approve_decision(tmp_repo, entry_id, "dismiss")   # HEAD does NOT advance
        reconcile.reconcile_session(tmp_repo)
        assert _held(tmp_repo) == {}
        assert _dispositions_of(tmp_repo) == [(entry_id, "dismissed")]


class TestRetireCandidate:
    """Phase 3: a retirement candidate materializes as a `proposed_lifecycle` on its target —
    a proposal in the separate lane. The decision stays live and its content is untouched;
    only an explicit human `retire_decision` moves it out."""

    def test_retire_proposes_retirement_without_touching_the_decision(self, tmp_repo):
        entry_id = _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", RETIRES)

        receipt = reconcile.reconcile_session(tmp_repo)
        assert (receipt["proposed"], receipt["lifecycle_proposed"]) == (0, 1)
        entry = next(e for e in store.load(tmp_repo)["entries"] if e["id"] == entry_id)
        proposal = entry["proposed_lifecycle"]
        assert proposal["action"] == "retire"
        assert proposal["source"] == "ai"
        assert "Stop using Postgres" in proposal["reason"]
        assert proposal["basis_revision_id"] == entry["current_revision_id"]
        assert entry["content"] == STORED                     # content lane untouched
        assert entry.get("proposed_revision") is None
        assert store.entry_status(entry) == "approved"        # still live
        assert store._pending_review_flag(tmp_repo).exists()  # nudge armed

    def test_its_evidence_is_held_and_the_next_pass_proposes_nothing(self, tmp_repo):
        entry_id = _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", RETIRES)
        reconcile.reconcile_session(tmp_repo)

        _candidate_id, meta = _only_held(tmp_repo)   # held => its events are out of pending
        assert meta["lane"] == "lifecycle"
        assert meta["entry_id"] == entry_id
        # No revision is recorded: a revision advance is not an approval signal for this lane
        # (ruling R25), so storing one would only invite a reader to use it.
        assert "revision_id" not in meta
        assert _pending(tmp_repo) == []
        second = reconcile.reconcile_session(tmp_repo)
        assert (second["lifecycle_proposed"], second["proposed"]) == (0, 0)

    def test_retiring_the_target_settles_the_candidate_as_approved(self, tmp_repo):
        entry_id = _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", RETIRES)
        reconcile.reconcile_session(tmp_repo)

        assert lifecycle.retire_decision(tmp_repo, entry_id, "the developer said so")[0]
        reconcile.reconcile_session(tmp_repo)
        # The hold is released and the disposition rides along with the decision into the
        # tombstone sidecar, which is the only place that decision still exists.
        assert _held(tmp_repo) == {}
        (tombstone,) = store.load_deleted(tmp_repo)["entries"]
        assert [s["disposition"] for s in tombstone["evidence_summary"]] == ["approved"]

    def test_dismissing_the_proposal_settles_the_candidate_as_dismissed(self, tmp_repo):
        # The Task 5 residual: a proposal that died unapproved must release its evidence rather
        # than sit pending forever — and it must never read as approval.
        entry_id = _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", RETIRES)
        reconcile.reconcile_session(tmp_repo)

        assert lifecycle.dismiss_lifecycle(tmp_repo, entry_id)[0]
        reconcile.reconcile_session(tmp_repo)
        assert _held(tmp_repo) == {}
        assert _dispositions_of(tmp_repo) == [(entry_id, "dismissed")]

    def test_an_unrelated_edit_never_records_a_retirement_that_did_not_happen(self, tmp_repo):
        # Ruling R25. Retirement is a MOVE, so the revision test that settles a CONTENT proposal
        # is unsound here: HEAD advancing says a revision landed, not that anything was retired.
        # Reading it as approval would fabricate exactly the outcome this pipeline measures.
        entry_id = _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", RETIRES)
        reconcile.reconcile_session(tmp_repo)

        # The developer has not answered the retirement — they edited the decision's wording.
        store.edit_decision(tmp_repo, entry_id, content=f"{STORED} Pooling is tuned per service.")
        entry = next(e for e in store.load(tmp_repo)["entries"] if e["id"] == entry_id)
        assert entry["proposed_lifecycle"]                        # still awaiting an answer
        assert entry["current_revision_id"]                       # ...on a HEAD that moved

        reconcile.reconcile_session(tmp_repo)
        assert len(_held(tmp_repo)) == 1                       # still awaiting an answer
        assert _dispositions_of(tmp_repo) == []

    def test_a_dismissal_after_an_unrelated_edit_is_still_a_dismissal(self, tmp_repo):
        # The other half of R25: once the proposal is gone from a still-LIVE decision it died
        # unapproved, whatever its revisions did in the meantime.
        entry_id = _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", RETIRES)
        reconcile.reconcile_session(tmp_repo)
        store.edit_decision(tmp_repo, entry_id, content=f"{STORED} Pooling is tuned per service.")
        assert lifecycle.dismiss_lifecycle(tmp_repo, entry_id)[0]

        reconcile.reconcile_session(tmp_repo)
        assert _dispositions_of(tmp_repo) == [(entry_id, "dismissed")]

    def test_an_ignored_target_is_dismissed_not_read_as_a_retirement(self, tmp_repo):
        # `approve_decision(action="ignore")` leaves the decision in the LIVE store with status
        # ignored — it writes no tombstone and no lifecycle record, so it is not a retirement.
        entry_id = _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", RETIRES)
        reconcile.reconcile_session(tmp_repo)
        assert store.approve_decision(tmp_repo, entry_id, "ignore")[0]
        assert store.list_deleted(tmp_repo) == []

        reconcile.reconcile_session(tmp_repo)
        assert _dispositions_of(tmp_repo) == [(entry_id, "dismissed")]

    def test_a_tombstone_with_no_retirement_record_is_not_an_approval(self, tmp_repo):
        # A tombstone written before lifecycle history existed records nothing about WHY the
        # decision left, so the record — not the file — is what `_retired_ids` reads.
        entry_id = _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", RETIRES)
        reconcile.reconcile_session(tmp_repo)
        assert lifecycle.retire_decision(tmp_repo, entry_id, "gone")[0]
        graveyard = store.load_deleted(tmp_repo)
        graveyard["entries"][0].pop("lifecycle")          # a legacy-shaped tombstone
        store._save_deleted(tmp_repo, graveyard)

        reconcile.reconcile_session(tmp_repo)
        (tombstone,) = store.load_deleted(tmp_repo)["entries"]
        assert [s["disposition"] for s in tombstone["evidence_summary"]] == ["dismissed"]

    def test_a_dry_run_proposes_no_retirement_and_writes_nothing(self, tmp_repo):
        _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", RETIRES)
        before = _store_bytes(tmp_repo)

        receipt = reconcile.reconcile_session(tmp_repo, dry_run=True)
        assert receipt["lifecycle_proposed"] == 1
        assert _store_bytes(tmp_repo) == before
        assert _held(tmp_repo) == {}

    def test_a_refused_proposal_settles_its_events_rather_than_retrying_forever(self, tmp_repo):
        entry_id = _stored_decision(tmp_repo)
        assert lifecycle.propose_lifecycle(tmp_repo, entry_id, "retire", "I said so",
                                       source="human")["ok"]
        _emit(tmp_repo, "user_directive", RETIRES)

        receipt = reconcile.reconcile_session(tmp_repo)
        assert receipt["lifecycle_proposed"] == 0
        entry = next(e for e in store.load(tmp_repo)["entries"] if e["id"] == entry_id)
        assert entry["proposed_lifecycle"]["reason"] == "I said so"   # human keeps the slot
        assert _pending(tmp_repo) == []
        assert _held(tmp_repo) == {}                # settled in the same run, nothing awaited


class TestDuplicateCandidate:

    def test_duplicate_is_held_and_finalized_in_the_same_run(self, tmp_repo):
        """Red-team mitigation 2. A duplicate leaves nothing to review, so if its events stayed
        in `pending/` they would re-aggregate into the same duplicate at every checkpoint
        forever — permanently defeating the fast path. Held AND finalized in one pass."""
        target_id = _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", DUPLICATES)
        before = _store_bytes(tmp_repo)

        receipt = reconcile.reconcile_session(tmp_repo)
        assert (receipt["duplicates"], receipt["proposed"]) == (1, 0)
        assert _pending(tmp_repo) == []             # consumed
        assert _held(tmp_repo) == {}                # and already settled — not left holding
        # The store's decisions are untouched apart from the additive summary on the one this
        # candidate matched, which is where its disposition now lives.
        assert _store_bytes(tmp_repo) != before
        assert _dispositions_of(tmp_repo) == [(target_id, "dismissed")]
        assert store.load(tmp_repo)["entries"][0]["content"] == STORED
        assert reconcile.reconcile_session(tmp_repo)["events_observed"] == 0

    def test_a_candidate_the_store_filter_rejects_counts_as_a_duplicate(self, tmp_repo):
        # The novelty filter is the backstop: the aggregator thought this was new, the store
        # knew better. That is a duplicate, not an error, and it settles the same way.
        _emit(tmp_repo, "user_directive", UNRELATED)
        reconcile.reconcile_session(tmp_repo)
        (entry,) = store.load(tmp_repo)["entries"]
        _emit(tmp_repo, "user_directive", UNRELATED, session_id="sess-2")

        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(reconcile.candidates, "aggregate_candidates",
                          _forced_new(entry["id"]))
            receipt = reconcile.reconcile_session(tmp_repo)
        assert (receipt["duplicates"], receipt["proposed"]) == (1, 0)
        assert len(store.load(tmp_repo)["entries"]) == 1


def _forced_new(existing_id):
    """Force the "aggregator says new, store says duplicate" collision the filter backstops.

    The candidate id is a real uuid because it becomes a directory name: the spool shape-checks
    every id before joining it to a path (mitigation 6), so a made-up string would be rejected
    at the boundary rather than exercising the branch this fixture is for."""
    def fake(events, decisions):
        return {"candidates": [{
            "candidate_id": str(uuid.uuid4()), "kind": "new", "title": "Linter rule",
            "content": UNRELATED, "subtype": "convention", "target_decision_id": None,
            "replacement_decision_id": None, "source_files": [], "score": 50,
            "signals": [{"event_id": e["event_id"], "weight": 50, "reason": "x"}
                        for e in events],
            "uncertainties": [],
        }], "diagnostics": {}}
    return fake


class TestInsufficientCandidate:

    def test_support_alone_stays_pending_until_a_seed_arrives(self, tmp_repo):
        _emit(tmp_repo, "file_changed", "edited", files=["src/a.py"])
        first = reconcile.reconcile_session(tmp_repo)
        assert (first["insufficient"], first["proposed"]) == (1, 0)
        assert _held(tmp_repo) == {}
        assert len(_pending(tmp_repo)) == 1        # more evidence may still arrive

        _emit(tmp_repo, "user_directive", "Always keep the fixture under src/a.py green.",
              files=["src/a.py"])
        second = reconcile.reconcile_session(tmp_repo)
        assert second["events_observed"] == 2      # the old file event re-aggregated
        assert second["proposed"] == 1
        # The file change is still leftover, and correctly so: a support event that PRECEDES
        # the statement it belongs to corroborates nothing (candidates._attach_target's stated
        # consequence). It stays pending rather than being folded in after the fact.
        assert second["insufficient"] == 1
        _candidate_id, meta = _only_held(tmp_repo)
        assert len(meta["event_ids"]) == 1
        assert [e["kind"] for e in _pending(tmp_repo)] == ["file_changed"]


# ── idempotency, crash recovery, dry run, fast path ──────────────────────────

class TestIdempotency:

    def test_second_pass_proposes_nothing_and_holds_nothing_twice(self, tmp_repo):
        _emit(tmp_repo, "user_directive", UNRELATED)
        assert reconcile.reconcile_session(tmp_repo)["proposed"] == 1
        held, entries = _held(tmp_repo), _store_bytes(tmp_repo)

        second = reconcile.reconcile_session(tmp_repo)
        assert (second["proposed"], second["events_observed"]) == (0, 0)
        assert _held(tmp_repo) == held
        assert _store_bytes(tmp_repo) == entries

    def _forget_the_event_ids(self, repo):
        """Put a held candidate's events back in `pending/` and erase the record that it ever
        claimed them — the one state `_finish_interrupted_holds` cannot recognize."""
        candidate_id, meta = _only_held(repo)
        held_dir = spool._held_dir(repo, candidate_id)
        for path in spool._event_files(held_dir):
            path.rename(spool._pending_dir(repo) / path.name)
        (held_dir / spool._META_NAME).write_text(
            json.dumps({k: v for k, v in meta.items() if k != "event_ids"}), encoding="utf-8")
        return candidate_id

    def test_a_held_candidate_is_counted_as_already_pending(self, tmp_repo):
        # The belt to the held-events braces: the directory exists but its bookkeeping never
        # recorded the event ids, so the events DO reach the aggregator again — and the
        # deterministic candidate id is what stops a second proposal anyway.
        _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", UPDATES)
        reconcile.reconcile_session(tmp_repo)
        candidate_id = self._forget_the_event_ids(tmp_repo)

        receipt = reconcile.reconcile_session(tmp_repo)
        assert (receipt["already_pending"], receipt["proposed"]) == (1, 0)
        assert list(_held(tmp_repo)) == [candidate_id]

    def test_an_unrecorded_hold_is_absorbed_as_a_duplicate_not_proposed_twice(self, tmp_repo):
        """The novelty filter as the backstop. A brand-new candidate whose hold left no record
        re-aggregates against a store that now HOLDS that decision, so the second pass reads it
        as a duplicate of what the first pass created — never as a second decision."""
        _emit(tmp_repo, "user_directive", UNRELATED)
        reconcile.reconcile_session(tmp_repo)
        (created,) = store.get_pending_decisions(tmp_repo)
        self._forget_the_event_ids(tmp_repo)

        receipt = reconcile.reconcile_session(tmp_repo)
        assert (receipt["duplicates"], receipt["proposed"]) == (1, 0)
        assert len(store.load(tmp_repo)["entries"]) == 1
        assert _pending(tmp_repo) == []
        assert (created["id"], "dismissed") in _dispositions_of(tmp_repo)

    def test_two_concurrent_passes_converge_on_one_decision(self, tmp_repo, monkeypatch):
        """Red-team mitigation 4. There is NO lock in the evidence layer, so two reconciles can
        both aggregate the same pending set before either holds anything. Simulated by giving
        the second pass the spool as it looked before the first one wrote: the same events, no
        held candidate to recognize. It must converge — one decision (the store's novelty
        filter absorbs the second), the first pass's hold intact, no event lost or held twice.
        """
        _emit(tmp_repo, "user_directive", UNRELATED)
        pre_hold = spool.list_pending_evidence(tmp_repo)
        reconcile.reconcile_session(tmp_repo)                 # the pass that wins the race
        (created,) = store.get_pending_decisions(tmp_repo)
        winner, meta = _only_held(tmp_repo)
        held_files = [p.name for p in spool._event_files(spool._held_dir(tmp_repo, winner))]

        monkeypatch.setattr(spool, "list_pending_evidence", lambda *_a, **_k: list(pre_hold))
        monkeypatch.setattr(spool, "held_candidates", lambda *_a, **_k: {})
        receipt = reconcile.reconcile_session(tmp_repo)

        assert receipt["proposed"] == 0                      # nothing new was proposed
        assert [e["id"] for e in store.load(tmp_repo)["entries"]] == [created["id"]]
        # Read off the filesystem, not `held_candidates` — it is still patched to the
        # pre-hold view for the length of this test.
        assert [d.name for d in spool._held_root(tmp_repo).iterdir()] == [winner]
        assert [p.name for p in spool._event_files(spool._held_dir(tmp_repo, winner))] \
            == held_files                                    # and no event moved or lost
        assert sorted(meta["event_ids"]) == sorted(e["event_id"] for e in pre_hold)

    def test_a_crash_between_materializing_and_moving_finishes_the_move(self, tmp_repo):
        """Revised plan B3 step 8. Materialize FIRST, then move — so a crash in between leaves
        the decision stored and its events split across `pending/` and `held/`. Re-aggregating
        the leftovers would mint a DIFFERENT candidate id (the seed is the sorted event ids)
        and propose the same decision twice; instead the next pass finishes the move."""
        _emit(tmp_repo, "user_directive", "Always pin the auth middleware to one provider.",
              files=["src/auth.py"])
        _emit(tmp_repo, "file_changed", "auth middleware changed", files=["src/auth.py"])
        assert reconcile.reconcile_session(tmp_repo)["proposed"] == 1
        candidate_id, meta = _only_held(tmp_repo)
        assert len(meta["event_ids"]) == 2

        # Simulate the crash: half the batch never made it out of pending/.
        moved_back = spool._event_files(spool._held_dir(tmp_repo, candidate_id))[0]
        moved_back.rename(spool._pending_dir(tmp_repo) / moved_back.name)
        assert len(_pending(tmp_repo)) == 1

        receipt = reconcile.reconcile_session(tmp_repo)

        assert (receipt["proposed"], receipt["events_observed"]) == (0, 0)
        assert list(_held(tmp_repo)) == [candidate_id]        # one candidate, not two
        assert len(spool._event_files(spool._held_dir(tmp_repo, candidate_id))) == 2
        assert _pending(tmp_repo) == []
        assert len(store.get_pending_decisions(tmp_repo)) == 1   # and one decision, not two


class TestDryRun:

    def test_dry_run_writes_nothing_anywhere(self, tmp_repo):
        _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", UNRELATED)
        before = {p.name: p.read_bytes() for p in store.STORE_DIR.iterdir() if p.is_file()}
        spooled = _spool_state(tmp_repo)

        receipt = reconcile.reconcile_session(tmp_repo, dry_run=True)
        assert receipt["dry_run"] is True
        assert receipt["proposed"] == 1                      # what a real pass WOULD do
        assert {p.name: p.read_bytes()
                for p in store.STORE_DIR.iterdir() if p.is_file()} == before
        assert _spool_state(tmp_repo) == spooled             # no hold, no finalize, no receipt
        assert _held(tmp_repo) == {}
        assert store.get_pending_decisions(tmp_repo) == []

    def test_dry_run_does_not_settle_a_reviewed_candidate(self, tmp_repo):
        _emit(tmp_repo, "user_directive", UNRELATED)
        reconcile.reconcile_session(tmp_repo)
        (pending,) = store.get_pending_decisions(tmp_repo)
        store.approve_decision(tmp_repo, pending["id"], "approve")
        before = _spool_state(tmp_repo)

        reconcile.reconcile_session(tmp_repo, dry_run=True)
        assert _spool_state(tmp_repo) == before
        assert len(_held(tmp_repo)) == 1
        assert _dispositions_of(tmp_repo) == []

    def test_dry_run_does_not_finish_an_interrupted_hold(self, tmp_repo):
        # The recovery move is a WRITE like any other. A dry run still excludes the stray event
        # from aggregation (that is what a real pass would do, so the report is honest) but
        # leaves it exactly where it lies.
        _emit(tmp_repo, "user_directive", "Always pin the auth middleware to one provider.",
              files=["src/auth.py"])
        _emit(tmp_repo, "file_changed", "auth middleware changed", files=["src/auth.py"])
        reconcile.reconcile_session(tmp_repo)
        candidate_id, _meta = _only_held(tmp_repo)
        stray = spool._event_files(spool._held_dir(tmp_repo, candidate_id))[0]
        stray.rename(spool._pending_dir(tmp_repo) / stray.name)
        before = _spool_state(tmp_repo)

        receipt = reconcile.reconcile_session(tmp_repo, dry_run=True)
        assert (receipt["proposed"], receipt["events_observed"]) == (0, 0)
        assert _spool_state(tmp_repo) == before


class TestFastPath:

    def test_nothing_to_do_never_reads_the_store(self, tmp_repo, monkeypatch):
        # This runs at every session start, so the quiet case must cost two directory listings
        # — not a store load, and certainly not the store lock other session-start passes hold
        # across whole-repo mining.
        def _no(*_a, **_k):
            raise AssertionError("the store must not be read on the fast path")

        monkeypatch.setattr(store, "load", _no)
        monkeypatch.setattr(store, "store_lock", _no)
        receipt = reconcile.reconcile_session(tmp_repo)
        assert receipt == {"events_observed": 0, "proposed": 0, "lifecycle_proposed": 0,
                           "already_pending": 0, "duplicates": 0, "insufficient": 0,
                           "incomplete": False, "dry_run": False}

    def test_a_still_held_candidate_reads_the_store_but_never_locks_it(self, tmp_repo,
                                                                      monkeypatch):
        # The other branch past the fast path: no pending events, one held candidate whose
        # fate might have changed. It must READ the store (that is the whole question it is
        # asking) and must not write, so the store lock is never taken.
        _emit(tmp_repo, "user_directive", UNRELATED)
        reconcile.reconcile_session(tmp_repo)
        assert len(_held(tmp_repo)) == 1

        loads = []
        real_load = store.load
        monkeypatch.setattr(store, "load", lambda *a, **k: loads.append(1) or real_load(*a, **k))
        monkeypatch.setattr(store, "store_lock",
                            lambda *_a, **_k: pytest.fail("nothing is written on this branch"))
        receipt = reconcile.reconcile_session(tmp_repo)
        assert (receipt["events_observed"], receipt["proposed"]) == (0, 0)
        assert loads                                   # the store WAS read
        assert len(_held(tmp_repo)) == 1

    def test_a_pass_that_did_nothing_spools_no_receipt_event(self, tmp_repo):
        # Otherwise a repo with one still-held candidate spools a `session_reconcile` event at
        # every SessionStart, PreCompact and SessionEnd, forever, filling the spool toward
        # retention with news of having done nothing.
        _emit(tmp_repo, "user_directive", UNRELATED)
        reconcile.reconcile_session(tmp_repo)
        before = _spool_state(tmp_repo)

        reconcile.reconcile_session(tmp_repo)
        reconcile.reconcile_session(tmp_repo)
        assert _spool_state(tmp_repo) == before

    def test_a_pass_that_did_something_does_spool_one(self, tmp_repo):
        _emit(tmp_repo, "user_directive", UNRELATED)
        reconcile.reconcile_session(tmp_repo)
        assert [e["kind"] for e in spool.list_pending_evidence(tmp_repo)] == ["session_reconcile"]

    def test_bookkeeping_events_do_not_keep_the_pass_awake(self, tmp_repo, monkeypatch):
        # A pass spools its own `session_reconcile` receipt, which nothing ever holds. If that
        # counted as evidence, the NEXT pass would always have work to do and the fast path
        # would never fire again on any repo that has reconciled once.
        _emit(tmp_repo, "user_directive", UNRELATED)
        reconcile.reconcile_session(tmp_repo)
        (pending,) = store.get_pending_decisions(tmp_repo)
        store.approve_decision(tmp_repo, pending["id"], "approve")
        reconcile.reconcile_session(tmp_repo)               # settles + finalizes

        assert spool.list_pending_evidence(tmp_repo)        # bookkeeping remains
        assert _held(tmp_repo) == {}
        monkeypatch.setattr(store, "load",
                            lambda *_a, **_k: pytest.fail("fast path should have returned"))
        assert reconcile.reconcile_session(tmp_repo)["events_observed"] == 0


class TestDamagedEvidence:

    def test_one_corrupt_event_is_quarantined_and_its_siblings_still_reconcile(self, tmp_repo):
        """The spool's answer to what the single-file ledger reported as `incomplete`: one bad
        event is isolated as it is met, and it hides nothing beside it."""
        _emit(tmp_repo, "user_directive", UNRELATED)
        _emit(tmp_repo, "user_directive", "Always tag releases with the sprint number.",
              session_id="sess-2")
        spool._event_files(spool._pending_dir(tmp_repo))[0].write_text("{not json",
                                                                      encoding="utf-8")

        receipt = reconcile.reconcile_session(tmp_repo)
        assert (receipt["events_observed"], receipt["proposed"]) == (1, 1)
        assert spool.evidence_diagnostics(tmp_repo)["quarantine"] == 1
        assert len(store.get_pending_decisions(tmp_repo)) == 1

    def test_a_hold_naming_no_decision_is_left_alone_rather_than_judged(self, tmp_repo):
        """A `candidate.json` with no `entry_id` — never written, or unreadable — says nothing
        about which decision the candidate became, so there is nothing to judge it against.
        Dismissing it on that silence would settle a candidate that may still be under review;
        it stays held, and `spool.evidence_diagnostics`' `held_unattributed` is what surfaces
        it accruing."""
        _emit(tmp_repo, "user_directive", UNRELATED)
        reconcile.reconcile_session(tmp_repo)
        candidate_id, _meta = _only_held(tmp_repo)
        (spool._held_dir(tmp_repo, candidate_id) / spool._META_NAME).unlink()
        (pending,) = store.get_pending_decisions(tmp_repo)
        store.approve_decision(tmp_repo, pending["id"], "approve")   # a real disposition…

        reconcile.reconcile_session(tmp_repo)

        assert list(_held(tmp_repo)) == [candidate_id]      # …that nothing can attribute
        assert _dispositions_of(tmp_repo) == []
        assert spool.evidence_diagnostics(tmp_repo)["held_unattributed"] == 1

    def test_a_refused_hold_is_reported_not_swallowed(self, tmp_repo, monkeypatch):
        # The decision is stored but its evidence still reads as pending; the receipt must say
        # so, because the next pass is the one that finishes the move.
        _emit(tmp_repo, "user_directive", UNRELATED)
        monkeypatch.setattr(spool, "hold_candidate_evidence",
                            lambda *_a, **_k: {"status": "error", "moved": 0,
                                               "already_held": 0, "missing": [],
                                               "failed": [], "errors": ["nope"]})
        receipt = reconcile.reconcile_session(tmp_repo)
        assert (receipt["proposed"], receipt["incomplete"]) == (1, True)

    def test_a_failed_summary_write_costs_the_receipt_not_the_batch(self, tmp_repo,
                                                                    monkeypatch):
        # Finalizing already deleted the events, so a store failure here loses only the record
        # of the disposition — it must not abandon the remaining candidates mid-loop.
        _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", DUPLICATES)
        monkeypatch.setattr(store, "record_evidence_summary", _boom)

        receipt = reconcile.reconcile_session(tmp_repo)
        assert (receipt["duplicates"], receipt["incomplete"]) == (1, True)
        assert _held(tmp_repo) == {}                        # still settled
        assert _dispositions_of(tmp_repo) == []

    def test_an_unexpected_failure_never_raises_out(self, tmp_repo, monkeypatch):
        _emit(tmp_repo, "user_directive", UNRELATED)
        monkeypatch.setattr(reconcile.candidates, "aggregate_candidates",
                            lambda *_a, **_k: 1 / 0)
        receipt = reconcile.reconcile_session(tmp_repo)
        assert receipt["incomplete"] is True
        assert receipt["proposed"] == 0


class TestSessionScope:

    def test_a_named_session_reconciles_only_its_own_events(self, tmp_repo):
        _emit(tmp_repo, "user_directive", UNRELATED, session_id="sess-a")
        _emit(tmp_repo, "user_directive", "Always tag releases with the sprint number.",
              session_id="sess-b")
        receipt = reconcile.reconcile_session(tmp_repo, "sess-a")
        assert receipt["events_observed"] == 1
        assert receipt["proposed"] == 1
        assert [e["session_id"] for e in _pending(tmp_repo)] == ["sess-b"]


# ── retention wiring ─────────────────────────────────────────────────────────

class TestRetentionWiring:
    """The spool's two bounds. Reconciliation runs retention on every real pass; a host that
    never reconciles gets it at session start instead (red-team mitigation 1)."""

    def _age_pending(self, repo, days):
        import os
        old = (Path(spool._pending_dir(repo)).stat().st_mtime
               - days * 86400)
        for path in spool._event_files(spool._pending_dir(repo)):
            os.utime(path, (old, old))

    def test_reconciliation_runs_retention(self, tmp_repo):
        _emit(tmp_repo, "user_directive", UNRELATED)
        _emit(tmp_repo, "file_changed", "unrelated edit", files=["src/z.py"])
        self._age_pending(tmp_repo, spool._MAX_PENDING_AGE_DAYS + 1)

        reconcile.reconcile_session(tmp_repo)
        # The aged file event corroborated nothing (`insufficient`), so nothing held it and
        # retention is what finally bounds it.
        assert _pending(tmp_repo) == []
        assert spool.evidence_diagnostics(tmp_repo)["gap"]["drops"] >= 1

    def test_an_emit_only_host_still_gets_retention_at_session_start(self, tmp_repo):
        """Codex reaches no reconciliation entrypoint and Cursor emits directives only, so
        without this their `pending/` would grow for good. The call sits on the store-side
        session-start payload every host traverses."""
        _emit(tmp_repo, "user_directive", UNRELATED)
        self._age_pending(tmp_repo, spool._MAX_PENDING_AGE_DAYS + 1)

        store.session_start_payload(tmp_repo)

        assert spool.list_pending_evidence(tmp_repo) == []
        assert spool.evidence_diagnostics(tmp_repo)["gap"]["drops"] == 1

    def test_session_start_finalizes_a_held_candidate_whose_decision_is_gone(self, tmp_repo):
        """The orphan sweep, end to end. A held candidate's decision can be deleted out of
        band; nothing re-aggregates its events (they are out of `pending/`) and nothing will
        ever finalize them — so the sweep does, which is only possible because reconciliation
        recorded the `entry_id` in the hold's own `candidate.json`."""
        _emit(tmp_repo, "user_directive", UNRELATED)
        reconcile.reconcile_session(tmp_repo)
        candidate_id, meta = _only_held(tmp_repo)
        assert meta["entry_id"]

        data = store.load(tmp_repo)                    # delete the decision out of band
        data["entries"] = []
        store.save(tmp_repo, data)

        assert spool.run_retention(tmp_repo)["finalized_orphans"] == [candidate_id]
        assert _held(tmp_repo) == {}
        assert not spool._held_dir(tmp_repo, candidate_id).exists()

    def test_session_start_retention_is_silent_and_fail_soft(self, tmp_repo, monkeypatch):
        _emit(tmp_repo, "user_directive", UNRELATED)
        monkeypatch.setattr(spool, "run_retention", _boom)
        payload = store.session_start_payload(tmp_repo)     # must not raise
        assert "status" in payload and "context" in payload


# ── hook wiring ──────────────────────────────────────────────────────────────

class TestHookWiring:
    """Zero hook-command, settings or installer changes: the call rides inside entrypoints the
    installed hooks already invoke. Every one of them is fail-soft."""

    def test_sync_memory_reconciles(self, tmp_repo, monkeypatch):
        monkeypatch.setattr(store, "resolve_repo", lambda p: p or tmp_repo)
        _emit(tmp_repo, "user_directive", UNRELATED)
        assert claude.sync_memory(tmp_repo) == 0            # no memory dir here
        assert len(store.get_pending_decisions(tmp_repo)) == 1

    def test_a_raising_reconcile_never_breaks_sync_memory(self, tmp_repo, monkeypatch):
        monkeypatch.setattr(store, "resolve_repo", lambda p: p or tmp_repo)
        monkeypatch.setattr(claude, "_import_memory_facts", lambda repo: 7)
        monkeypatch.setattr(reconcile, "reconcile_session", _boom)
        assert claude.sync_memory(tmp_repo) == 7

    def test_gemini_checkpoints_reconcile_fail_soft(self, tmp_repo, monkeypatch):
        _emit(tmp_repo, "user_directive", UNRELATED)
        assert gemini.pre_compress(tmp_repo, "{}") == json.dumps({"suppressOutput": True})
        assert len(store.get_pending_decisions(tmp_repo)) == 1

        _emit(tmp_repo, "user_directive", "Always squash before merging to main.",
              session_id="sess-2")
        monkeypatch.setattr(reconcile, "reconcile_session", _boom)
        assert gemini.session_end(tmp_repo, "{}") == json.dumps({"suppressOutput": True})

    @pytest.mark.parametrize("module", [claude, codex, cursor, gemini],
                             ids=lambda m: m.NAME)
    def test_no_host_hook_command_mentions_reconciliation(self, module):
        # The wiring point is a Python entrypoint, so no installed hook needs rewiring — and
        # nothing here may become a per-prompt cost. Checked per FILE: concatenating the four
        # and splitting once only ever inspected the last one's tail.
        source = Path(module.__file__).read_text(encoding="utf-8")
        assert "reconcile_session" not in source.split("def install")[-1]


# ── surfaces ─────────────────────────────────────────────────────────────────

class TestMcpTool:

    def test_tool_returns_the_receipt_and_the_pending_review_notice(self, tmp_repo,
                                                                    monkeypatch):
        from contexer import server

        monkeypatch.setattr(server.store, "resolve_repo", lambda p: tmp_repo)
        _emit(tmp_repo, "user_directive", UNRELATED)
        out = server.reconcile_session(tmp_repo)
        assert "proposed for review:      1" in out
        assert "pending review — not yet trusted" in out

    def test_tool_is_quiet_when_nothing_was_proposed(self, tmp_repo, monkeypatch):
        from contexer import server

        monkeypatch.setattr(server.store, "resolve_repo", lambda p: tmp_repo)
        out = server.reconcile_session(tmp_repo)
        assert "pending review" not in out
        assert "evidence events observed: 0" in out

    def test_tool_without_a_repo(self, monkeypatch):
        from contexer import server

        monkeypatch.setattr(server.store, "resolve_repo", lambda p: "")
        assert server.reconcile_session("") == "Skipped — repo path not detected."


class TestCliCommand:

    def _run(self, monkeypatch, tmp_repo, *args):
        from contexer import cli

        monkeypatch.setattr(cli, "_cli_repo", lambda: tmp_repo)
        monkeypatch.setattr("sys.argv", ["contexer", "reconcile-session", *args])
        cli.main()

    def test_prints_the_receipt(self, tmp_repo, monkeypatch, capsys):
        _emit(tmp_repo, "user_directive", UNRELATED)
        self._run(monkeypatch, tmp_repo)
        out = capsys.readouterr().out
        assert "Reconciled evidence:" in out
        assert "proposed for review:      1" in out
        assert len(store.get_pending_decisions(tmp_repo)) == 1

    def test_dry_run_prints_and_writes_nothing(self, tmp_repo, monkeypatch, capsys):
        _emit(tmp_repo, "user_directive", UNRELATED)
        before = ({p.name: p.read_bytes() for p in store.STORE_DIR.iterdir() if p.is_file()},
                  _spool_state(tmp_repo))
        self._run(monkeypatch, tmp_repo, "--dry-run")
        assert "dry run — nothing was written" in capsys.readouterr().out
        assert ({p.name: p.read_bytes() for p in store.STORE_DIR.iterdir() if p.is_file()},
                _spool_state(tmp_repo)) == before

    def test_session_flag_scopes_the_pass(self, tmp_repo, monkeypatch, capsys):
        _emit(tmp_repo, "user_directive", UNRELATED, session_id="sess-a")
        self._run(monkeypatch, tmp_repo, "--session", "sess-b")
        assert "evidence events observed: 0" in capsys.readouterr().out

    def test_session_without_a_value_exits_1_instead_of_eating_the_next_flag(
            self, tmp_repo, monkeypatch, capsys):
        # `--session --dry-run` used to take the flag as the session VALUE: the pass was
        # scoped to a session that cannot exist AND the dry run was dropped, i.e. a write
        # where the developer asked for none.
        _emit(tmp_repo, "user_directive", UNRELATED)
        with pytest.raises(SystemExit) as exc:
            self._run(monkeypatch, tmp_repo, "--session", "--dry-run")
        assert exc.value.code == 1
        assert "Missing value for --session" in capsys.readouterr().err
        assert store.get_pending_decisions(tmp_repo) == []       # nothing was written

    def test_unknown_argument_exits_1(self, tmp_repo, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc:
            self._run(monkeypatch, tmp_repo, "--force")
        assert exc.value.code == 1
        assert "Unknown argument" in capsys.readouterr().err

    def test_listed_in_help(self, monkeypatch, capsys):
        from contexer import cli

        monkeypatch.setattr("sys.argv", ["contexer", "help"])
        cli.main()
        assert "reconcile-session" in capsys.readouterr().out


class TestReceiptRendering:

    def _receipt(self, **overrides):
        return {"events_observed": 3, "proposed": 0, "lifecycle_proposed": 0,
                "already_pending": 0, "duplicates": 0, "insufficient": 0, "dry_run": False,
                "incomplete": False, **overrides}

    def test_a_proposed_retirement_says_it_retired_nothing(self):
        text = reconcile.format_receipt(self._receipt(lifecycle_proposed=2))
        assert "retirements proposed:     2" in text
        assert "nothing was retired" in text

    def test_no_retirement_says_nothing_about_retiring(self):
        assert "nothing was retired" not in reconcile.format_receipt(self._receipt())

    def test_incomplete_is_stated(self):
        assert "incomplete" in reconcile.format_receipt(self._receipt(incomplete=True))
