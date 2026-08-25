"""Tests for contexer/reconcile.py — evidence in, decisions PENDING REVIEW out.

Two properties carry this module and are asserted from several angles rather than once:

* **Idempotency.** A second pass over the same evidence proposes nothing and writes no second
  checkpoint. The mechanism under test is the checkpoint (a consumed event never reaches the
  aggregator again), NOT the store's novelty filter — so the assertions are on the receipt and
  on the ledger, not merely on the store staying the same size.
* **`dry_run` writes nothing anywhere.** Asserted on bytes: the store file, the ledger and the
  gap marker are compared before and after.

Everything reconciliation proposes is unreviewed by construction, so the retire path is
asserted the other way round: the proposal lands in the separate lifecycle lane and the
decision itself — content, revisions, status — is untouched until a human retires it.
"""
import json
from pathlib import Path

import pytest

from contexer import candidates, evidence, lifecycle, reconcile, store
from contexer.adapters import claude, codex, cursor, gemini

SESSION = "sess-1"


def _emit(repo, kind, summary="", session_id=SESSION, files=None, attributes=None):
    result = evidence.emit_hook_event(repo, kind, session_id=session_id, source="test",
                                      summary=summary, files=files, attributes=attributes)
    assert result["status"] == "stored", result
    return result


def _ledger_bytes(repo):
    path = store.STORE_DIR / f".evidence_{store.repo_slug(repo)}.json"
    return path.read_bytes() if path.exists() else b""


def _store_bytes(repo):
    path = store.STORE_DIR / f"{store.repo_slug(repo)}.json"
    return path.read_bytes() if path.exists() else b""


def _checkpoints(repo):
    return evidence.candidate_checkpoints(repo)


def _unconsumed(repo):
    return [e for e in evidence.unconsumed_events(repo)
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


def _only_checkpoint_id(repo):
    (candidate_id,) = _checkpoints(repo)
    return candidate_id


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
    the checkpoint learns its fate on the next pass -> compaction collapses it."""

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

        # One checkpoint, pending, naming the entry and both events it consumed.
        (checkpoint,) = _checkpoints(tmp_repo).values()
        assert checkpoint["status"] == "pending"
        assert checkpoint["entry_id"] == pending["id"]
        assert len(checkpoint["event_ids"]) == 2
        assert _unconsumed(tmp_repo) == []

        # The developer approves. Nothing hooks approve_decision: the next pass is what
        # notices, which is exactly what "lazy disposition" means.
        assert store.approve_decision(tmp_repo, pending["id"], "approve")[0] is True
        assert _checkpoints(tmp_repo)[next(iter(_checkpoints(tmp_repo)))]["status"] == "pending"

        second = reconcile.reconcile_session(tmp_repo)
        assert second["proposed"] == 0
        # Settled -> compact_evidence collapsed it into one disposition event and dropped the
        # evidence it consumed.
        assert _checkpoints(tmp_repo) == {}
        kinds = [e["kind"] for e in evidence.unconsumed_events(tmp_repo)]
        assert "candidate_disposition" in kinds
        assert "user_directive" not in kinds

    def test_the_entry_records_the_session_the_evidence_came_from(self, tmp_repo):
        # Provenance, not bookkeeping: the decision came out of that host session, so that is
        # what it records — never the id of whichever process happened to reconcile.
        _emit(tmp_repo, "user_directive", UNRELATED, session_id="host-session-9")
        reconcile.reconcile_session(tmp_repo)
        (entry,) = store.load(tmp_repo)["entries"]
        assert entry["session_id"] == "host-session-9"

    def test_an_ignored_decision_settles_the_checkpoint_as_dismissed(self, tmp_repo):
        _emit(tmp_repo, "user_directive", UNRELATED)
        reconcile.reconcile_session(tmp_repo)
        (pending,) = store.get_pending_decisions(tmp_repo)
        store.approve_decision(tmp_repo, pending["id"], "ignore")

        reconcile.reconcile_session(tmp_repo)
        # Dismissed checkpoints are settled too, so compaction removed it and its events.
        assert _checkpoints(tmp_repo) == {}
        assert _unconsumed(tmp_repo) == []


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
        assert next(iter(_checkpoints(tmp_repo).values()))["status"] == "pending"

        store.approve_decision(tmp_repo, entry["id"], "approve")
        reconcile.reconcile_session(tmp_repo)
        assert _checkpoints(tmp_repo) == {}          # settled, then compacted

    def test_a_checkpoint_is_never_approved_without_review_evidence(self, tmp_repo):
        # An entry can stop being pending without anyone reviewing it. Reading that as
        # approval is how an inferred decision would launder itself into a reviewed one, so
        # the flip requires `approved_by == "human"` — what only approve/edit stamps.
        _emit(tmp_repo, "agent_conclusion", self.CONCLUSION)
        reconcile.reconcile_session(tmp_repo)
        data = store.load(tmp_repo)
        data["entries"][0]["status"] = "approved"    # no approved_by: nobody looked at it
        store.save(tmp_repo, data)

        reconcile.reconcile_session(tmp_repo)
        assert next(iter(_checkpoints(tmp_repo).values()))["status"] == "pending"


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
        assert _checkpoints(tmp_repo)[_only_checkpoint_id(tmp_repo)]["entry_id"] == entry_id

    def test_an_update_applied_in_place_is_dismissed_not_left_pending(self, tmp_repo):
        # A convention-subtyped correction is trivial to the store: it applies immediately as
        # a new approved revision, with no proposal and nothing to review. The store's return
        # looks identical to the proposal case, so the checkpoint status is settled from what
        # the entry ACTUALLY shows — otherwise this evidence would be pinned forever waiting
        # on a review nobody will ever be asked for.
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
        assert _checkpoints(tmp_repo) == {}                # dismissed, so compaction took it
        assert _unconsumed(tmp_repo) == []

    def test_a_second_pass_proposes_nothing_more(self, tmp_repo):
        _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", UPDATES)
        reconcile.reconcile_session(tmp_repo)
        before = _store_bytes(tmp_repo)

        assert reconcile.reconcile_session(tmp_repo)["proposed"] == 0
        assert _store_bytes(tmp_repo) == before

    def test_the_checkpoint_records_the_revision_it_was_proposed_against(self, tmp_repo):
        entry_id = _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", UPDATES)
        reconcile.reconcile_session(tmp_repo)

        entry = next(e for e in store.load(tmp_repo)["entries"] if e["id"] == entry_id)
        checkpoint = _checkpoints(tmp_repo)[_only_checkpoint_id(tmp_repo)]
        assert checkpoint["revision_id"] == entry["current_revision_id"]
        assert "lane" not in checkpoint                      # the content lane is the default

    def test_an_approved_proposal_settles_as_approved(self, tmp_repo):
        entry_id = _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", UPDATES)
        reconcile.reconcile_session(tmp_repo)

        store.approve_decision(tmp_repo, entry_id, "approve")   # HEAD advances
        reconcile.reconcile_session(tmp_repo)
        (disposition,) = [e for e in evidence.unconsumed_events(tmp_repo)
                          if e["kind"] == "candidate_disposition"]
        assert disposition["attributes"]["candidate_status"] == "approved"

    def test_a_dismissed_proposal_settles_as_dismissed_rather_than_pinning_forever(
            self, tmp_repo):
        # The Task 5 residual. `approved_by == "human"` alone can never become true here — the
        # target was never a pending entry — so this checkpoint used to sit `pending` for good,
        # pinning its evidence against eviction and keeping the fast path awake on every pass.
        entry_id = _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", UPDATES)
        reconcile.reconcile_session(tmp_repo)

        store.approve_decision(tmp_repo, entry_id, "dismiss")   # HEAD does NOT advance
        reconcile.reconcile_session(tmp_repo)
        assert _checkpoints(tmp_repo) == {}
        (disposition,) = [e for e in evidence.unconsumed_events(tmp_repo)
                          if e["kind"] == "candidate_disposition"]
        assert disposition["attributes"]["candidate_status"] == "dismissed"


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

    def test_its_evidence_is_pinned_and_the_next_pass_proposes_nothing(self, tmp_repo):
        entry_id = _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", RETIRES)
        reconcile.reconcile_session(tmp_repo)

        (checkpoint,) = _checkpoints(tmp_repo).values()
        assert checkpoint["status"] == "pending"      # pending => its events stay pinned
        assert checkpoint["lane"] == "lifecycle"
        assert checkpoint["entry_id"] == entry_id
        assert _unconsumed(tmp_repo) == []
        second = reconcile.reconcile_session(tmp_repo)
        assert (second["lifecycle_proposed"], second["proposed"]) == (0, 0)

    def test_retiring_the_target_settles_the_checkpoint_as_approved(self, tmp_repo):
        entry_id = _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", RETIRES)
        reconcile.reconcile_session(tmp_repo)

        assert lifecycle.retire_decision(tmp_repo, entry_id, "the developer said so")[0]
        reconcile.reconcile_session(tmp_repo)
        # Settled checkpoints are compacted away, so the proof is that its events are gone
        # and a disposition event records the outcome.
        assert _checkpoints(tmp_repo) == {}
        (disposition,) = [e for e in evidence.unconsumed_events(tmp_repo)
                          if e["kind"] == "candidate_disposition"]
        assert disposition["attributes"]["candidate_status"] == "approved"

    def test_dismissing_the_proposal_settles_the_checkpoint_as_dismissed(self, tmp_repo):
        # The Task 5 residual: a proposal that died unapproved must unpin its evidence rather
        # than sit pending forever — and it must never read as approval.
        entry_id = _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", RETIRES)
        reconcile.reconcile_session(tmp_repo)

        assert lifecycle.dismiss_lifecycle(tmp_repo, entry_id)[0]
        reconcile.reconcile_session(tmp_repo)
        assert _checkpoints(tmp_repo) == {}
        (disposition,) = [e for e in evidence.unconsumed_events(tmp_repo)
                          if e["kind"] == "candidate_disposition"]
        assert disposition["attributes"]["candidate_status"] == "dismissed"

    def test_a_dry_run_proposes_no_retirement_and_writes_nothing(self, tmp_repo):
        _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", RETIRES)
        before = _store_bytes(tmp_repo)

        receipt = reconcile.reconcile_session(tmp_repo, dry_run=True)
        assert receipt["lifecycle_proposed"] == 1
        assert _store_bytes(tmp_repo) == before
        assert _checkpoints(tmp_repo) == {}

    def test_a_refused_proposal_settles_its_events_rather_than_retrying_forever(self, tmp_repo):
        entry_id = _stored_decision(tmp_repo)
        assert lifecycle.propose_lifecycle(tmp_repo, entry_id, "retire", "I said so",
                                       source="human")["ok"]
        _emit(tmp_repo, "user_directive", RETIRES)

        receipt = reconcile.reconcile_session(tmp_repo)
        assert receipt["lifecycle_proposed"] == 0
        entry = next(e for e in store.load(tmp_repo)["entries"] if e["id"] == entry_id)
        assert entry["proposed_lifecycle"]["reason"] == "I said so"   # human keeps the slot
        assert _unconsumed(tmp_repo) == []


class TestDuplicateCandidate:

    def test_duplicate_is_dismissed_and_its_events_consumed(self, tmp_repo):
        _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", DUPLICATES)
        before = _store_bytes(tmp_repo)

        receipt = reconcile.reconcile_session(tmp_repo)
        assert (receipt["duplicates"], receipt["proposed"]) == (1, 0)
        assert _store_bytes(tmp_repo) == before
        # Dismissed -> settled -> compaction already collapsed it; either way the events are
        # gone from the unconsumed set, which is the property that matters.
        assert _unconsumed(tmp_repo) == []
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
    """Force the "aggregator says new, store says duplicate" collision the filter backstops."""
    def fake(events, decisions):
        return {"candidates": [{
            "candidate_id": "forced-new", "kind": "new", "title": "Linter rule",
            "content": UNRELATED, "subtype": "convention", "target_decision_id": None,
            "replacement_decision_id": None, "source_files": [], "score": 50,
            "signals": [{"event_id": e["event_id"], "weight": 50, "reason": "x"}
                        for e in events],
            "uncertainties": [],
        }], "diagnostics": {}}
    return fake


class TestInsufficientCandidate:

    def test_support_alone_stays_unconsumed_until_a_seed_arrives(self, tmp_repo):
        _emit(tmp_repo, "file_changed", "edited", files=["src/a.py"])
        first = reconcile.reconcile_session(tmp_repo)
        assert (first["insufficient"], first["proposed"]) == (1, 0)
        assert _checkpoints(tmp_repo) == {}
        assert len(_unconsumed(tmp_repo)) == 1     # more evidence may still arrive

        _emit(tmp_repo, "user_directive", "Always keep the fixture under src/a.py green.",
              files=["src/a.py"])
        second = reconcile.reconcile_session(tmp_repo)
        assert second["events_observed"] == 2      # the old file event re-aggregated
        assert second["proposed"] == 1
        # The file change is still leftover, and correctly so: a support event that PRECEDES
        # the statement it belongs to corroborates nothing (candidates._attach_target's stated
        # consequence). It stays unconsumed rather than being folded in after the fact.
        assert second["insufficient"] == 1
        (checkpoint,) = _checkpoints(tmp_repo).values()
        assert len(checkpoint["event_ids"]) == 1
        assert [e["kind"] for e in _unconsumed(tmp_repo)] == ["file_changed"]


# ── idempotency, dry run, fast path, unreadable ledger ───────────────────────

class TestIdempotency:

    def test_second_pass_proposes_nothing_and_writes_no_second_checkpoint(self, tmp_repo):
        _emit(tmp_repo, "user_directive", UNRELATED)
        assert reconcile.reconcile_session(tmp_repo)["proposed"] == 1
        checkpoints, entries = _checkpoints(tmp_repo), _store_bytes(tmp_repo)

        second = reconcile.reconcile_session(tmp_repo)
        assert (second["proposed"], second["events_observed"]) == (0, 0)
        assert _checkpoints(tmp_repo) == checkpoints
        assert _store_bytes(tmp_repo) == entries

    def test_a_checkpointed_candidate_is_counted_as_already_pending(self, tmp_repo):
        # The belt to the consumed-events braces: same deterministic candidate id, checkpoint
        # already there, so nothing is proposed even when the events look unconsumed.
        _emit(tmp_repo, "user_directive", UNRELATED)
        reconcile.reconcile_session(tmp_repo)
        (candidate_id, checkpoint) = next(iter(_checkpoints(tmp_repo).items()))
        evidence.record_candidate_checkpoints(
            tmp_repo, {candidate_id: {**checkpoint, "event_ids": []}})   # release the events

        receipt = reconcile.reconcile_session(tmp_repo)
        assert (receipt["already_pending"], receipt["proposed"]) == (1, 0)


class TestDryRun:

    def test_dry_run_writes_nothing_anywhere(self, tmp_repo):
        _stored_decision(tmp_repo)
        _emit(tmp_repo, "user_directive", UNRELATED)
        before = {p.name: p.read_bytes() for p in store.STORE_DIR.iterdir()}

        receipt = reconcile.reconcile_session(tmp_repo, dry_run=True)
        assert receipt["dry_run"] is True
        assert receipt["proposed"] == 1                      # what a real pass WOULD do
        assert {p.name: p.read_bytes() for p in store.STORE_DIR.iterdir()} == before
        assert _checkpoints(tmp_repo) == {}
        assert store.get_pending_decisions(tmp_repo) == []

    def test_dry_run_does_not_flip_a_settled_checkpoint(self, tmp_repo):
        _emit(tmp_repo, "user_directive", UNRELATED)
        reconcile.reconcile_session(tmp_repo)
        (pending,) = store.get_pending_decisions(tmp_repo)
        store.approve_decision(tmp_repo, pending["id"], "approve")
        before = _ledger_bytes(tmp_repo)

        reconcile.reconcile_session(tmp_repo, dry_run=True)
        assert _ledger_bytes(tmp_repo) == before
        assert next(iter(_checkpoints(tmp_repo).values()))["status"] == "pending"


class TestFastPath:

    def test_nothing_to_do_never_reads_the_store(self, tmp_repo, monkeypatch):
        # This runs at every session start, so the quiet case must cost one sidecar read —
        # not a store load, and certainly not the store lock other session-start passes hold
        # across whole-repo mining.
        def _no(*_a, **_k):
            raise AssertionError("the store must not be read on the fast path")

        monkeypatch.setattr(store, "load", _no)
        monkeypatch.setattr(store, "store_lock", _no)
        receipt = reconcile.reconcile_session(tmp_repo)
        assert receipt == {"events_observed": 0, "proposed": 0, "lifecycle_proposed": 0,
                           "already_pending": 0, "duplicates": 0, "insufficient": 0,
                           "incomplete": False, "dry_run": False}

    def test_a_stuck_pending_checkpoint_reads_the_store_but_never_locks_it(self, tmp_repo,
                                                                          monkeypatch):
        # The other branch past the fast path: no events, one checkpoint whose fate might have
        # changed. It must READ the store (that is the whole question it is asking) and must
        # not write, so the store lock is never taken.
        _emit(tmp_repo, "user_directive", UNRELATED)
        reconcile.reconcile_session(tmp_repo)
        assert next(iter(_checkpoints(tmp_repo).values()))["status"] == "pending"

        loads = []
        real_load = store.load
        monkeypatch.setattr(store, "load", lambda *a, **k: loads.append(1) or real_load(*a, **k))
        monkeypatch.setattr(store, "store_lock",
                            lambda *_a, **_k: pytest.fail("nothing is written on this branch"))
        receipt = reconcile.reconcile_session(tmp_repo)
        assert (receipt["events_observed"], receipt["proposed"]) == (0, 0)
        assert loads                                   # the store WAS read
        assert next(iter(_checkpoints(tmp_repo).values()))["status"] == "pending"

    def test_a_pass_that_did_nothing_appends_no_receipt_event(self, tmp_repo):
        # Otherwise a repo with one stuck pending checkpoint appends a `session_reconcile`
        # event at every SessionStart, PreCompact and SessionEnd, forever, filling the ledger
        # toward eviction with news of having done nothing.
        _emit(tmp_repo, "user_directive", UNRELATED)
        reconcile.reconcile_session(tmp_repo)
        before = _ledger_bytes(tmp_repo)

        reconcile.reconcile_session(tmp_repo)
        reconcile.reconcile_session(tmp_repo)
        assert _ledger_bytes(tmp_repo) == before

    def test_a_pass_that_did_something_does_append_one(self, tmp_repo):
        _emit(tmp_repo, "user_directive", UNRELATED)
        reconcile.reconcile_session(tmp_repo)
        assert [e["kind"] for e in evidence.unconsumed_events(tmp_repo)
                if e["kind"] == "session_reconcile"] == ["session_reconcile"]

    def test_bookkeeping_events_do_not_keep_the_pass_awake(self, tmp_repo, monkeypatch):
        # A pass appends its own `session_reconcile` receipt. If that counted as evidence, the
        # NEXT pass would always have work to do and the fast path would never fire again.
        _emit(tmp_repo, "user_directive", UNRELATED)
        reconcile.reconcile_session(tmp_repo)
        (pending,) = store.get_pending_decisions(tmp_repo)
        store.approve_decision(tmp_repo, pending["id"], "approve")
        reconcile.reconcile_session(tmp_repo)               # settles + compacts

        assert [e["kind"] for e in evidence.unconsumed_events(tmp_repo)]  # bookkeeping remains
        monkeypatch.setattr(store, "load",
                            lambda *_a, **_k: pytest.fail("fast path should have returned"))
        assert reconcile.reconcile_session(tmp_repo)["events_observed"] == 0


class TestUnreadableLedger:

    def test_a_corrupt_ledger_reports_incomplete_and_writes_nothing(self, tmp_repo):
        _emit(tmp_repo, "user_directive", UNRELATED)
        path = store.STORE_DIR / f".evidence_{store.repo_slug(tmp_repo)}.json"
        path.write_text("{not json", encoding="utf-8")
        before = {p.name: p.read_bytes() for p in store.STORE_DIR.iterdir()}

        receipt = reconcile.reconcile_session(tmp_repo)
        assert receipt["incomplete"] is True
        assert receipt["proposed"] == 0
        assert {p.name: p.read_bytes() for p in store.STORE_DIR.iterdir()} == before

    def test_a_refused_checkpoint_write_is_reported_not_swallowed(self, tmp_repo, monkeypatch):
        # The decisions are stored but their evidence still reads as unconsumed; the receipt
        # must say so, because the next pass depends on the novelty filter to absorb them.
        _emit(tmp_repo, "user_directive", UNRELATED)
        monkeypatch.setattr(evidence, "record_candidate_checkpoints",
                            lambda *_a, **_k: {"status": "error", "recorded": 0,
                                               "errors": ["busy"]})
        receipt = reconcile.reconcile_session(tmp_repo)
        assert (receipt["proposed"], receipt["incomplete"]) == (1, True)

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
        assert [e["session_id"] for e in _unconsumed(tmp_repo)] == ["sess-b"]


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
        before = {p.name: p.read_bytes() for p in store.STORE_DIR.iterdir()}
        self._run(monkeypatch, tmp_repo, "--dry-run")
        assert "dry run — nothing was written" in capsys.readouterr().out
        assert {p.name: p.read_bytes() for p in store.STORE_DIR.iterdir()} == before

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
