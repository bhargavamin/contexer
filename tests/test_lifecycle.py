"""Tests for the decision-lifecycle lane (plan C1 + C2): `proposed_lifecycle`, retirement,
restoration, and the lifecycle history a tombstone carries.

Three properties carry this feature, and each is asserted from several angles:

* **The two proposal lanes are independent.** A content correction and a retirement are
  different questions about the same decision. Neither may displace the other, and resolving
  one must leave the other untouched — the whole reason the plan refused to reuse the single
  `proposed_revision` slot.
* **Nothing is retired without a human.** A proposal changes nothing a session can see; only
  `retire_decision` moves a decision out of active context, and a proposal judged against a
  revision that has since moved is refused rather than applied blind.
* **Retiring hides, it never erases.** Every active surface stops seeing the decision at once
  (the Phase 3 exit gate, asserted surface by surface), while the tombstone keeps the full
  revision history, the lifecycle record, and any proposal nobody ever ruled on.

`store` is exercised through its public surface, with entries built by the real capture path
(borrowed pattern: test_conflicts.py).
"""
import ast
import json
import pathlib
import subprocess
import sys

import pytest

from contexer import (
    cli,
    conflicts,
    console_api,
    guard_engine,
    lifecycle,
    revisions,
    scope_audit,
    server,
    store,
)


STANDING = "Use Postgres for the decision store; SQLite won't handle concurrent sessions"
UPDATE = "Switch to DynamoDB for the decision store; Postgres is superseded"
OTHER = "Always run the formatter before pushing a branch to origin"


def _entry(repo: str, eid: str) -> dict:
    return next(e for e in store.load(repo)["entries"] if e.get("id") == eid)


def _approved(repo: str, content: str = STANDING, subtype: str = "architecture",
              session: str = "s1") -> str:
    """One live, approved decision — the only shape a lifecycle proposal may attach to."""
    ok, eid = store.update_decision(repo, content, session, subtype)
    assert ok and eid
    data = store.load(repo)
    next(e for e in data["entries"] if e["id"] == eid)["status"] = "approved"
    store.save(repo, data)
    return eid


def _with_update(repo: str, eid: str, update: str = UPDATE) -> None:
    ok, rid = store.update_decision(repo, update, "s2", "architecture", replace_id=eid)
    assert ok and rid == eid and _entry(repo, eid).get("proposed_revision")


def _answer(monkeypatch, keys: list) -> None:
    """Feed `contexer review` one keypress (or reason) per prompt, in order."""
    answers = iter(keys)
    monkeypatch.setattr("builtins.input", lambda _="": next(answers))


def _propose(repo: str, eid: str, reason: str = "superseded by the new store",
             source: str = "ai", replacement_id=None) -> dict:
    result = lifecycle.propose_lifecycle(repo, eid, "retire", reason, source=source,
                                         replacement_id=replacement_id)
    assert result["ok"], result["message"]
    return result["proposal"]


# ── the slot itself ───────────────────────────────────────────────────────────

class TestSlot:
    def test_the_proposal_records_what_it_was_judged_against(self, tmp_repo):
        eid = _approved(tmp_repo)
        proposal = _propose(tmp_repo, eid, "the service it describes was deleted")

        assert proposal["action"] == "retire"
        assert proposal["source"] == "ai"
        assert proposal["reason"] == "the service it describes was deleted"
        assert proposal["replacement_decision_id"] is None
        assert proposal["basis_revision_id"] == _entry(tmp_repo, eid)["current_revision_id"]
        assert proposal["proposal_id"] and proposal["created_at"]

    def test_proposing_arms_the_pending_review_nudge(self, tmp_repo):
        eid = _approved(tmp_repo)
        store._pending_review_flag(tmp_repo).unlink(missing_ok=True)
        _propose(tmp_repo, eid)
        assert store._pending_review_flag(tmp_repo).exists()
        assert store.pending_review_nudge(tmp_repo) is not None

    def test_a_pending_decision_has_nothing_to_retire(self, tmp_repo):
        # `approve_decision(action="ignore")` is the existing way to drop one, and a decision
        # nobody ever trusted is not a decision anyone needs to retire.
        ok, eid = store.update_decision(tmp_repo, STANDING, "s1", "constraint", created_by="ai")
        assert ok and store.entry_status(_entry(tmp_repo, eid)) == "pending_approval"
        result = lifecycle.propose_lifecycle(tmp_repo, eid, "retire", "no", source="ai")
        assert not result["ok"] and "not live" in result["message"]
        assert _entry(tmp_repo, eid).get("proposed_lifecycle") is None

    def test_an_unsupported_action_and_an_empty_reason_are_both_refused(self, tmp_repo):
        eid = _approved(tmp_repo)
        assert not lifecycle.propose_lifecycle(tmp_repo, eid, "archive", "why", source="ai")["ok"]
        assert not lifecycle.propose_lifecycle(tmp_repo, eid, "retire", "   ", source="ai")["ok"]
        assert _entry(tmp_repo, eid).get("proposed_lifecycle") is None

    def test_a_missing_decision_is_reported_not_created(self, tmp_repo):
        _approved(tmp_repo)
        result = lifecycle.propose_lifecycle(tmp_repo, "nosuchid", "retire", "why", source="ai")
        assert not result["ok"] and "not found" in result["message"]

    def test_dismissing_drops_the_proposal_and_keeps_the_decision(self, tmp_repo):
        eid = _approved(tmp_repo)
        _propose(tmp_repo, eid)
        ok, message = lifecycle.dismiss_lifecycle(tmp_repo, eid)
        assert ok and eid[:8] in message
        entry = _entry(tmp_repo, eid)
        assert entry.get("proposed_lifecycle") is None
        assert entry["content"] == revisions.normalize_content(STANDING)
        assert len(entry["revisions"]) == 1
        assert store.entry_status(entry) == "approved"

    def test_dismissing_nothing_says_so(self, tmp_repo):
        eid = _approved(tmp_repo)
        ok, message = lifecycle.dismiss_lifecycle(tmp_repo, eid)
        assert not ok and "no retirement proposal" in message


class TestTrustOrder:
    def test_an_ai_proposal_never_displaces_a_developers(self, tmp_repo):
        eid = _approved(tmp_repo)
        _propose(tmp_repo, eid, "I want this gone", source="human")
        result = lifecycle.propose_lifecycle(tmp_repo, eid, "retire", "inferred", source="ai")

        assert not result["ok"] and "developer's retirement proposal" in result["message"]
        entry = _entry(tmp_repo, eid)
        assert entry["proposed_lifecycle"]["reason"] == "I want this gone"
        assert "superseded_lifecycle" not in entry

    def test_a_developers_proposal_displaces_an_automated_one_and_archives_it(self, tmp_repo):
        eid = _approved(tmp_repo)
        _propose(tmp_repo, eid, "inferred from evidence", source="ai")
        _propose(tmp_repo, eid, "I want this gone", source="human")

        entry = _entry(tmp_repo, eid)
        assert entry["proposed_lifecycle"]["reason"] == "I want this gone"
        # Displaced, not discarded — the same archival shape `superseded_proposals` has.
        (archived,) = entry["superseded_lifecycle"]
        assert archived["reason"] == "inferred from evidence"
        assert archived["superseded_at"]

    def test_two_automated_proposals_keep_the_first(self, tmp_repo):
        # ai and scan are both machine guesses at the same transition; ranking them against
        # each other would let one quietly overwrite the other's reason.
        eid = _approved(tmp_repo)
        _propose(tmp_repo, eid, "first guess", source="scan")
        assert not lifecycle.propose_lifecycle(tmp_repo, eid, "retire", "second guess",
                                           source="ai")["ok"]
        assert _entry(tmp_repo, eid)["proposed_lifecycle"]["reason"] == "first guess"


class TestLanesCoexist:
    def test_both_proposals_sit_on_one_entry(self, tmp_repo):
        eid = _approved(tmp_repo)
        _with_update(tmp_repo, eid)
        _propose(tmp_repo, eid)

        entry = _entry(tmp_repo, eid)
        assert entry["proposed_revision"]["content"].startswith("Switch to DynamoDB")
        assert entry["proposed_lifecycle"]["action"] == "retire"
        # One decision, listed once, however many lanes are pending on it.
        assert [e["id"] for e in store.get_pending_decisions(tmp_repo)] == [eid]

    def test_approving_the_update_leaves_the_retirement_proposal(self, tmp_repo):
        eid = _approved(tmp_repo)
        _with_update(tmp_repo, eid)
        proposal = _propose(tmp_repo, eid)

        assert store.approve_decision(tmp_repo, eid, "approve")[0]
        entry = _entry(tmp_repo, eid)
        assert entry.get("proposed_revision") is None
        assert entry["proposed_lifecycle"]["proposal_id"] == proposal["proposal_id"]

    def test_dismissing_the_retirement_leaves_the_update(self, tmp_repo):
        eid = _approved(tmp_repo)
        _with_update(tmp_repo, eid)
        _propose(tmp_repo, eid)

        assert lifecycle.dismiss_lifecycle(tmp_repo, eid)[0]
        entry = _entry(tmp_repo, eid)
        assert entry.get("proposed_lifecycle") is None
        assert entry["proposed_revision"]["content"].startswith("Switch to DynamoDB")

    def test_dismissing_the_update_leaves_the_retirement(self, tmp_repo):
        eid = _approved(tmp_repo)
        _with_update(tmp_repo, eid)
        _propose(tmp_repo, eid)

        assert store.approve_decision(tmp_repo, eid, "dismiss")[0]
        entry = _entry(tmp_repo, eid)
        assert entry.get("proposed_revision") is None
        assert entry["proposed_lifecycle"]["action"] == "retire"


class TestStaleness:
    def test_a_moved_head_makes_the_proposal_stale(self, tmp_repo):
        eid = _approved(tmp_repo)
        _propose(tmp_repo, eid)
        assert not lifecycle.lifecycle_proposal_stale(_entry(tmp_repo, eid))

        _with_update(tmp_repo, eid)
        assert store.approve_decision(tmp_repo, eid, "approve")[0]   # HEAD advances
        assert lifecycle.lifecycle_proposal_stale(_entry(tmp_repo, eid))

    def test_a_stale_proposal_refuses_retirement_and_says_why(self, tmp_repo):
        eid = _approved(tmp_repo)
        _propose(tmp_repo, eid)
        _with_update(tmp_repo, eid)
        store.approve_decision(tmp_repo, eid, "approve")

        ok, message = lifecycle.retire_decision(tmp_repo, eid, "the developer said so")
        assert not ok
        assert "earlier revision" in message and "dismiss_lifecycle" in message
        assert _entry(tmp_repo, eid) is not None        # still live, still untouched
        assert store.list_deleted(tmp_repo) == []

    def test_a_stale_proposal_can_still_be_dismissed(self, tmp_repo):
        eid = _approved(tmp_repo)
        _propose(tmp_repo, eid)
        _with_update(tmp_repo, eid)
        store.approve_decision(tmp_repo, eid, "approve")

        assert lifecycle.dismiss_lifecycle(tmp_repo, eid)[0]
        assert _entry(tmp_repo, eid).get("proposed_lifecycle") is None

    def test_a_fresh_proposal_against_the_new_revision_retires(self, tmp_repo):
        eid = _approved(tmp_repo)
        _propose(tmp_repo, eid)
        _with_update(tmp_repo, eid)
        store.approve_decision(tmp_repo, eid, "approve")
        lifecycle.dismiss_lifecycle(tmp_repo, eid)
        _propose(tmp_repo, eid, "still want it gone", source="human")

        assert lifecycle.retire_decision(tmp_repo, eid, "re-reviewed and confirmed")[0]
        assert store.load(tmp_repo)["entries"] == []

    def test_a_direct_retirement_has_no_staleness_question(self, tmp_repo):
        # Nobody proposed anything, so there is no basis for HEAD to have moved away from.
        eid = _approved(tmp_repo)
        _with_update(tmp_repo, eid)
        store.approve_decision(tmp_repo, eid, "approve")
        assert lifecycle.retire_decision(tmp_repo, eid, "no longer relevant")[0]

    def test_the_review_render_marks_a_stale_proposal(self, tmp_repo):
        eid = _approved(tmp_repo)
        _propose(tmp_repo, eid)
        fresh = store.format_pending_review(tmp_repo)
        assert "retirement proposed (source=ai)" in fresh
        assert "retire_decision(" in fresh and "STALE" not in fresh

        _with_update(tmp_repo, eid)
        store.approve_decision(tmp_repo, eid, "approve")
        stale = store.format_pending_review(tmp_repo)
        assert "STALE" in stale
        assert "retire_decision(" not in stale        # refused, so never suggested
        assert "dismiss_lifecycle(" in stale


# ── retirement and restoration ────────────────────────────────────────────────

class TestRetireAndRestore:
    def test_retiring_tombstones_the_decision_with_its_history(self, tmp_repo):
        eid = _approved(tmp_repo)
        _with_update(tmp_repo, eid)
        store.approve_decision(tmp_repo, eid, "approve")     # two revisions to preserve
        before = _entry(tmp_repo, eid)

        ok, message = lifecycle.retire_decision(tmp_repo, eid, "the service was decommissioned")
        assert ok and "Retired" in message and eid[:8] in message
        assert store.load(tmp_repo)["entries"] == []

        (tomb,) = store.list_deleted(tmp_repo)
        assert tomb["id"] == eid
        assert len(tomb["revisions"]) == 2 == len(before["revisions"])
        assert tomb["current_revision_id"] == before["current_revision_id"]
        (record,) = tomb["lifecycle"]
        assert record["kind"] == "retired"
        assert record["actor"] == "human"
        assert record["reason"] == "the service was decommissioned"
        assert record["revision_id"] == before["current_revision_id"]
        assert record["replacement_decision_id"] is None
        assert record["event_id"] and record["occurred_at"]

    def test_a_named_replacement_records_a_supersession(self, tmp_repo):
        eid = _approved(tmp_repo)
        successor = _approved(tmp_repo, OTHER, subtype="convention", session="s3")

        ok, message = lifecycle.retire_decision(tmp_repo, eid, "the new rule covers it", successor)
        assert ok and "Superseded" in message and successor[:8] in message
        (tomb,) = store.list_deleted(tmp_repo)
        (record,) = tomb["lifecycle"]
        assert record["kind"] == "superseded"
        assert record["replacement_decision_id"] == successor
        assert [e["id"] for e in store.load(tmp_repo)["entries"]] == [successor]

    def test_a_retirement_needs_a_reason(self, tmp_repo):
        eid = _approved(tmp_repo)
        ok, message = lifecycle.retire_decision(tmp_repo, eid, "   ")
        assert not ok and "needs a reason" in message
        assert store.list_deleted(tmp_repo) == []

    def test_the_retirement_satisfies_and_pops_its_own_proposal(self, tmp_repo):
        eid = _approved(tmp_repo)
        _propose(tmp_repo, eid)
        assert lifecycle.retire_decision(tmp_repo, eid, "confirmed with the developer")[0]
        (tomb,) = store.list_deleted(tmp_repo)
        assert "proposed_lifecycle" not in tomb

    def test_an_unreviewed_update_is_archived_never_dropped(self, tmp_repo):
        eid = _approved(tmp_repo)
        _with_update(tmp_repo, eid)
        _propose(tmp_repo, eid)

        assert lifecycle.retire_decision(tmp_repo, eid, "confirmed with the developer")[0]
        (tomb,) = store.list_deleted(tmp_repo)
        assert "proposed_revision" not in tomb
        assert tomb["unreviewed_proposal_at_retirement"]["content"].startswith(
            "Switch to DynamoDB")

    def test_restoring_returns_the_decision_with_its_status_and_revisions(self, tmp_repo):
        eid = _approved(tmp_repo)
        _with_update(tmp_repo, eid)
        store.approve_decision(tmp_repo, eid, "approve")
        before = json.dumps({k: v for k, v in _entry(tmp_repo, eid).items()
                             if k != "lifecycle"}, sort_keys=True)

        lifecycle.retire_decision(tmp_repo, eid, "gone for now")
        ok, message = lifecycle.restore_decision(tmp_repo, eid, "we still need it")
        assert ok and eid[:8] in message

        restored = _entry(tmp_repo, eid)
        assert json.dumps({k: v for k, v in restored.items() if k != "lifecycle"},
                          sort_keys=True) == before
        assert store.entry_status(restored) == "approved"
        assert len(restored["revisions"]) == 2
        assert store.list_deleted(tmp_repo) == []

    def test_restoring_appends_to_the_history_rather_than_rewinding_it(self, tmp_repo):
        eid = _approved(tmp_repo)
        lifecycle.retire_decision(tmp_repo, eid, "gone")
        lifecycle.restore_decision(tmp_repo, eid, "back")
        lifecycle.retire_decision(tmp_repo, eid, "gone again")

        (tomb,) = store.list_deleted(tmp_repo)
        assert [(r["kind"], r["reason"]) for r in tomb["lifecycle"]] == [
            ("retired", "gone"), ("restored", "back"), ("retired", "gone again")]

    def test_restoring_into_a_full_store_is_refused(self, tmp_repo, monkeypatch):
        eid = _approved(tmp_repo)
        lifecycle.retire_decision(tmp_repo, eid, "gone")
        _approved(tmp_repo, OTHER, subtype="convention", session="s3")
        monkeypatch.setattr(store, "MAX_ENTRIES", 1)

        ok, message = lifecycle.restore_decision(tmp_repo, eid)
        assert not ok and "maximum" in message
        assert [t["id"] for t in store.list_deleted(tmp_repo)] == [eid]

    def test_the_console_delete_records_the_same_history(self, tmp_repo):
        # Uniform history however a decision left the live store — the console's wording is
        # deliberately unchanged, only what it records.
        eid = _approved(tmp_repo)
        ok, message = store.delete_decision(tmp_repo, eid)
        assert ok and "Deleted" in message
        (record,) = store.list_deleted(tmp_repo)[0]["lifecycle"]
        assert (record["kind"], record["actor"]) == ("retired", "human")
        assert record["reason"] == "deleted via console"

    def test_the_console_delete_ignores_a_stale_proposal(self, tmp_repo):
        # A developer clicking Delete is not resolving anyone's proposal, so the staleness
        # guard that protects `retire_decision` must not fire here.
        eid = _approved(tmp_repo)
        _propose(tmp_repo, eid)
        _with_update(tmp_repo, eid)
        store.approve_decision(tmp_repo, eid, "approve")
        assert lifecycle.lifecycle_proposal_stale(_entry(tmp_repo, eid))
        assert store.delete_decision(tmp_repo, eid)[0]


# ── Phase 3 exit gate: a retired decision is invisible to every active surface ──

class TestExclusion:
    """Every assertion here is a BEFORE/AFTER pair, deliberately: an exclusion test that only
    checks the "after" passes just as happily against a decision that never reached the surface
    in the first place, and would then pin nothing at all."""

    def _anchored(self, repo: str) -> str:
        """One live, guard-trusted, anchored constraint — visible to every surface below."""
        eid = _approved(repo, f"{STANDING}; see src/app.py", subtype="constraint")
        data = store.load(repo)
        next(e for e in data["entries"] if e["id"] == eid).update(
            {"source_files": ["src/app.py"], "anchor_commit": "abc123",
             "created_by": "human", "approved_by": "human"})
        store.save(repo, data)
        return eid

    def test_get_context_no_longer_returns_it(self, tmp_repo):
        eid = self._anchored(tmp_repo)
        views = [{}, {"query": "Postgres"}, {"entry_type": "constraint"},
                 {"files": ["src/app.py"]}]
        assert all("SQLite" in store.get_context(tmp_repo, **v) for v in views)

        lifecycle.retire_decision(tmp_repo, eid, "the module was deleted")
        assert not any("SQLite" in store.get_context(tmp_repo, **v) for v in views)

    def test_the_retrieval_index_no_longer_holds_it(self, tmp_repo):
        eid = self._anchored(tmp_repo)
        prompt = "why did we choose Postgres for the decision store?"

        def indexed():
            return (store._read_retrieval_index(tmp_repo) or {}).get("docs", {})

        assert eid in indexed()
        assert "SQLite" in store.get_context_for_prompt(tmp_repo, prompt)

        lifecycle.retire_decision(tmp_repo, eid, "the module was deleted")
        assert eid not in indexed()
        assert "SQLite" not in store.get_context_for_prompt(tmp_repo, prompt)

    def test_session_start_no_longer_injects_it(self, tmp_repo):
        eid = self._anchored(tmp_repo)
        assert "SQLite" in store.session_start_payload(tmp_repo)["context"]

        lifecycle.retire_decision(tmp_repo, eid, "the module was deleted")
        assert "SQLite" not in store.session_start_payload(tmp_repo)["context"]

    def test_the_guard_no_longer_pairs_it(self, tmp_repo):
        eid = self._anchored(tmp_repo)
        assert [d["decision_id"] for d in
                guard_engine.decisions_for_files(tmp_repo, ["src/app.py"])] == [eid]
        assert guard_engine.guard_candidates(tmp_repo, ["src/app.py"])

        lifecycle.retire_decision(tmp_repo, eid, "the module was deleted")
        assert guard_engine.decisions_for_files(tmp_repo, ["src/app.py"]) == []
        assert guard_engine.guard_candidates(tmp_repo, ["src/app.py"], explain=True) == []

    def test_it_is_no_longer_pending_review(self, tmp_repo):
        eid = _approved(tmp_repo)
        _propose(tmp_repo, eid)
        assert [d["id"] for d in store.get_pending_decisions(tmp_repo)] == [eid]

        lifecycle.retire_decision(tmp_repo, eid, "confirmed")
        assert store.get_pending_decisions(tmp_repo) == []
        assert store.format_pending_review(tmp_repo) == "Nothing pending review."

    def test_but_the_history_reads_back_in_full(self, tmp_repo):
        eid = self._anchored(tmp_repo)
        lifecycle.retire_decision(tmp_repo, eid, "the module was deleted")

        (tomb,) = store.list_deleted(tmp_repo)
        assert tomb["id"] == eid
        assert tomb["revisions"] and tomb["lifecycle"][0]["kind"] == "retired"
        assert tomb["source_files"] == ["src/app.py"]     # the anchor is history, not lost
        rows = console_api.list_tombstones(tmp_repo)
        assert rows["ok"] and [r["id"] for r in rows["tombstones"]] == [eid]


class TestTombstoneReaders:
    """The tombstone sidecar is read RAW by readers that predate these keys. Additive means
    additive: the new keys must be invisible to them, not merely tolerated."""

    def test_scope_audit_reads_a_tombstone_carrying_the_new_keys(self, tmp_repo):
        eid = _approved(tmp_repo)
        _with_update(tmp_repo, eid)
        _propose(tmp_repo, eid)
        lifecycle.retire_decision(tmp_repo, eid, "confirmed")
        assert store._deleted_path(tmp_repo).exists()   # the file it reads raw is really there

        rows = scope_audit.audit_sessions()
        # A tombstone file must never read as a SECOND store for the same repo — the whole
        # reason `store.store_files()` is the enumeration rather than a local re-glob. It
        # reads tombstones RAW, so the new keys must simply be invisible to it.
        assert rows == []
        assert "No cross-store sessions found" in scope_audit.format_audit(rows)

    def test_list_tombstones_projects_it_without_leaking_the_proposal(self, tmp_repo):
        eid = _approved(tmp_repo)
        _with_update(tmp_repo, eid)
        lifecycle.retire_decision(tmp_repo, eid, "confirmed")

        (row,) = console_api.list_tombstones(tmp_repo)["tombstones"]
        assert row["id"] == eid and row["deleted_by"] == "human"
        assert "unreviewed_proposal_at_retirement" not in row


class TestConflictRendering:
    """A lifecycle proposal is not a `proposed_revision` and must never enter the conflict
    machinery: there are no two versions of the text to choose between."""

    def test_a_lifecycle_proposal_is_not_an_open_conflict(self, tmp_repo):
        eid = _approved(tmp_repo)
        _propose(tmp_repo, eid)
        entry = _entry(tmp_repo, eid)
        assert not conflicts._has_open_conflict(entry)
        assert conflicts._conflict_view(entry)[2] == []
        assert conflicts.memo_steer_line(entry) is None

    def test_get_context_renders_no_conflict_for_it(self, tmp_repo):
        eid = _approved(tmp_repo)
        _propose(tmp_repo, eid, "the service it describes was decommissioned")
        out = store.get_context(tmp_repo)
        assert "SQLite" in out                       # the decision itself still renders
        assert "Unreviewed update" not in out
        assert "decommissioned" not in out           # the reason is a review-surface fact
        assert "resolve_conflict(" not in out

    def test_resolve_conflict_refuses_a_lifecycle_only_entry(self, tmp_repo):
        eid = _approved(tmp_repo)
        _propose(tmp_repo, eid)
        ok, message = conflicts.record_conflict_memo(tmp_repo, eid, "standing")
        assert not ok and "no pending update" in message

    def test_a_conflict_beside_a_retirement_still_renders_as_one(self, tmp_repo):
        eid = _approved(tmp_repo)
        _with_update(tmp_repo, eid)
        _propose(tmp_repo, eid)
        out = store.get_context(tmp_repo)
        assert "Unreviewed update" in out and "DynamoDB" in out


# ── MCP + CLI surfaces ────────────────────────────────────────────────────────

class TestMcpTools:
    @pytest.fixture
    def repo(self, tmp_repo, monkeypatch):
        monkeypatch.setattr(server.store, "resolve_repo", lambda p: tmp_repo)
        return tmp_repo

    @pytest.mark.parametrize("target", ["all", "ALL", "*", " all "])
    def test_bulk_retirement_is_refused(self, repo, target):
        eid = _approved(repo)
        _approved(repo, OTHER, subtype="convention", session="s3")
        assert "one at a time" in server.retire_decision(target, "because")
        assert _entry(repo, eid) is not None            # nothing was touched

    def test_a_comma_list_is_refused_on_every_lifecycle_tool(self, repo):
        first = _approved(repo)
        second = _approved(repo, OTHER, subtype="convention", session="s3")
        pair = f"{first[:8]},{second[:8]}"
        assert "one at a time" in server.retire_decision(pair, "because")
        assert "one at a time" in server.restore_decision(pair)
        assert "one at a time" in server.dismiss_lifecycle(pair)
        assert len(store.load(repo)["entries"]) == 2

    def test_an_empty_id_is_reported_rather_than_matching_everything(self, repo):
        _approved(repo)
        assert server.retire_decision("  ", "because") == "No decision id given."
        assert len(store.load(repo)["entries"]) == 1

    def test_the_round_trip_works_through_the_tools(self, repo):
        eid = _approved(repo)
        _propose(repo, eid)
        assert "Retired" in server.retire_decision(eid[:8], "the developer said so")
        assert store.load(repo)["entries"] == []
        assert "Restored" in server.restore_decision(eid[:8], "changed our mind")
        assert _entry(repo, eid) is not None

    def test_dismiss_keeps_the_decision(self, repo):
        eid = _approved(repo)
        _propose(repo, eid)
        assert "stays live" in server.dismiss_lifecycle(eid[:8])
        assert _entry(repo, eid).get("proposed_lifecycle") is None

    def test_review_pending_surfaces_the_retirement(self, repo):
        eid = _approved(repo)
        _propose(repo, eid, "the service was decommissioned")
        out = server.review_pending()
        assert "retirement proposed (source=ai)" in out
        assert "the service was decommissioned" in out
        assert f'retire_decision(entry_id="{eid[:8]}"' in out
        assert "never retire a decision on your own judgment" in out

    def test_an_already_live_decision_is_not_offered_for_approval(self, repo):
        # It is already approved — `approve_decision` would reject the very action offered.
        eid = _approved(repo)
        _propose(repo, eid)
        assert "approve_decision(" not in server.review_pending()
        assert eid[:8] in server.review_pending()


class TestCli:
    def _run(self, monkeypatch, tmp_repo, *args):
        monkeypatch.setattr(cli, "_cli_repo", lambda: tmp_repo)
        monkeypatch.setattr(sys, "argv", ["contexer", *args])
        cli.main()

    def test_retire_then_restore_round_trip(self, tmp_repo, monkeypatch, capsys):
        eid = _approved(tmp_repo)
        self._run(monkeypatch, tmp_repo, "retire", eid[:8], "--reason", "decommissioned")
        assert "Retired" in capsys.readouterr().out
        assert store.load(tmp_repo)["entries"] == []

        self._run(monkeypatch, tmp_repo, "restore", eid[:8], "--reason", "needed again")
        assert "Restored" in capsys.readouterr().out
        assert [(r["kind"], r["reason"]) for r in _entry(tmp_repo, eid)["lifecycle"]] == [
            ("retired", "decommissioned"), ("restored", "needed again")]

    def test_replaced_by_records_a_supersession(self, tmp_repo, monkeypatch, capsys):
        eid = _approved(tmp_repo)
        successor = _approved(tmp_repo, OTHER, subtype="convention", session="s3")
        self._run(monkeypatch, tmp_repo, "retire", eid[:8], "--reason", "the new rule covers it",
                  "--replaced-by", successor)
        assert "Superseded" in capsys.readouterr().out
        (record,) = store.list_deleted(tmp_repo)[0]["lifecycle"]
        assert (record["kind"], record["replacement_decision_id"]) == ("superseded", successor)

    @pytest.mark.parametrize("args", [
        ["retire", "abc123"],                                   # no --reason
        ["retire", "abc123", "--reason"],                       # --reason with no value
        ["retire", "a,b", "--reason", "x"],                     # a list, not one id
        ["retire", "--reason", "x"],                            # no id at all
        ["restore", "a", "b"],                                  # two ids
    ])
    def test_bad_invocations_exit_nonzero_and_write_nothing(self, args, tmp_repo, monkeypatch):
        eid = _approved(tmp_repo)
        with pytest.raises(SystemExit) as exc:
            self._run(monkeypatch, tmp_repo, *args)
        assert exc.value.code == 1
        assert _entry(tmp_repo, eid) is not None

    def test_a_flag_is_never_swallowed_as_the_reason(self, tmp_repo, monkeypatch, capsys):
        # `--reason --replaced-by X` used to record the flag itself as permanent history.
        eid = _approved(tmp_repo)
        with pytest.raises(SystemExit):
            self._run(monkeypatch, tmp_repo, "retire", eid[:8], "--reason", "--replaced-by", "x")
        assert "Missing value for --reason" in capsys.readouterr().err

    def test_a_store_refusal_exits_nonzero(self, tmp_repo, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc:
            self._run(monkeypatch, tmp_repo, "retire", "nosuchid", "--reason", "why")
        assert exc.value.code == 1
        assert "not found" in capsys.readouterr().err

    def test_review_offers_retire_and_applies_it(self, tmp_repo, monkeypatch, capsys):
        eid = _approved(tmp_repo)
        _propose(tmp_repo, eid, "the service was decommissioned")
        monkeypatch.setattr(store, "git_root", lambda _: tmp_repo)
        _answer(monkeypatch, ["R", ""])

        cli.review()
        out = capsys.readouterr().out
        assert "retirement proposed" in out
        assert "[R] Retire  [D] Dismiss  [S] Skip  [Q] Quit" in out
        assert "1 retired" in out
        (record,) = store.list_deleted(tmp_repo)[0]["lifecycle"]
        # Blank input takes the proposal's own reason as the default.
        assert record["reason"] == "the service was decommissioned"

    def test_review_dismiss_keeps_the_decision_and_says_so(self, tmp_repo, monkeypatch, capsys):
        eid = _approved(tmp_repo)
        _with_update(tmp_repo, eid)
        _propose(tmp_repo, eid)
        monkeypatch.setattr(store, "git_root", lambda _: tmp_repo)
        _answer(monkeypatch, ["D"])

        cli.review()
        out = capsys.readouterr().out
        assert "also has a suggested update pending" in out      # the update is not lost
        assert "1 dismissed" in out
        entry = _entry(tmp_repo, eid)
        assert entry.get("proposed_lifecycle") is None
        assert entry["proposed_revision"]["content"].startswith("Switch to DynamoDB")

    def test_review_marks_a_stale_proposal(self, tmp_repo, monkeypatch, capsys):
        eid = _approved(tmp_repo)
        _propose(tmp_repo, eid)
        _with_update(tmp_repo, eid)
        store.approve_decision(tmp_repo, eid, "approve")
        monkeypatch.setattr(store, "git_root", lambda _: tmp_repo)
        _answer(monkeypatch, ["S"])

        cli.review()
        assert "STALE" in capsys.readouterr().out


# ── the extraction seam ───────────────────────────────────────────────────────

# Every public name the lifecycle lane owns. A name reappearing as a top-level def in
# store.py would be a SECOND definition of a transition, silently divergent from the one
# every caller actually reaches.
OWNED = frozenset({
    "lifecycle_record", "attach_lifecycle_proposal", "lifecycle_proposal_stale",
    "propose_lifecycle", "dismiss_lifecycle", "tombstone_entry", "retire_decision",
    "restore_decision", "review_lines",
})


class TestExtractionSeam:
    """`lifecycle.py` follows conflicts.py's discipline: it reads store through the MODULE
    OBJECT at call time, store.py needs it only at call time too, and the facade re-exports
    exactly one name — the one that WAS public on store before the extraction."""

    def test_lifecycle_owns_every_name_and_store_defines_none_of_them(self):
        tree = ast.parse(pathlib.Path(store.__file__).read_text(encoding="utf-8"))
        defined = {n.name for n in tree.body if isinstance(n, ast.FunctionDef)}
        for name in OWNED:
            assert hasattr(lifecycle, name), name
            assert name not in defined, f"store.py redefines {name}"

    def test_the_facade_re_exports_only_the_previously_public_name(self):
        assert store._LIFECYCLE_EXPORTS == {"restore_decision"}
        assert store.restore_decision is lifecycle.restore_decision
        assert "restore_decision" in dir(store)
        for name in OWNED - store._LIFECYCLE_EXPORTS:
            with pytest.raises(AttributeError):
                getattr(store, name)

    def test_production_code_reaches_the_owner_not_the_facade(self):
        # Written as a scan because the failure is invisible otherwise: the facade answers
        # correctly, so nothing breaks and the seam quietly erodes.
        offenders = []
        for path in pathlib.Path(store.__file__).parent.rglob("*.py"):
            if path.name in ("store.py", "lifecycle.py"):
                continue
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                # AST, not grep: several modules discuss these names in prose.
                if (isinstance(node, ast.Attribute) and node.attr in OWNED
                        and isinstance(node.value, ast.Name) and node.value.id == "store"):
                    offenders.append(f"{path.name}:{node.lineno} store.{node.attr}")
        assert offenders == [], offenders

    @pytest.mark.parametrize("probe,expected", [
        ("import contexer.lifecycle as m; print(m.store.GLOBAL_SLUG)", "_global"),
        ("import contexer.store as s; print(s.restore_decision.__module__)",
         "contexer.lifecycle"),
    ])
    def test_either_module_can_be_the_first_touch_of_the_package(self, probe, expected):
        # A FRESH interpreter: an already-imported package hides an ordering bug completely.
        proc = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                              timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == expected

    def test_store_owned_names_are_looked_up_at_call_time(self, tmp_repo, monkeypatch):
        # The payoff of `from contexer import store` over `from contexer.store import save`:
        # the `tmp_repo` fixture's STORE_DIR patch is what every test above depends on.
        eid = _approved(tmp_repo)
        calls = []
        real_save = store.save
        monkeypatch.setattr(store, "save", lambda *a, **k: calls.append(1) or real_save(*a, **k))
        _propose(tmp_repo, eid)
        assert calls, "lifecycle bound store.save at import time"
