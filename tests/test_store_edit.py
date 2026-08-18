"""Tests for edit_decision - the console's explicit-edit write path."""
import pytest

from contexer import store

SESSION = "test-edit-session"

APPROVED = "Use Redis for hot-read caching because Postgres round-trips dominated latency"
PENDING = "Never commit generated lockfiles to the repository"


def _approved(repo: str, content: str = APPROVED, **kwargs) -> dict:
    """An approved decision (created_by='human' classifies as auto)."""
    ok, entry_id = store.update_decision(repo, content, SESSION, created_by="human", **kwargs)
    assert ok
    return _entry(repo, entry_id)


def _pending(repo: str, content: str = PENDING) -> dict:
    """A brand-new pending_approval decision (an AI-captured constraint)."""
    ok, entry_id = store.update_decision(repo, content, SESSION, subtype="constraint")
    assert ok
    entry = _entry(repo, entry_id)
    assert entry["status"] == "pending_approval"
    return entry


def _entry(repo: str, entry_id: str) -> dict:
    return next(e for e in store._load(repo)["entries"] if e["id"] == entry_id)


# ── the happy path ────────────────────────────────────────────────────────────

class TestRevisionAppend:
    def test_content_edit_appends_a_revision_with_the_given_source(self, tmp_repo):
        before = _approved(tmp_repo)
        ok, msg, entry = store.edit_decision(
            tmp_repo, before["id"], content="Use Memcached for hot-read caching instead")
        assert ok
        assert before["id"][:8] in msg
        assert "revision 2" in msg

        after = _entry(tmp_repo, before["id"])
        assert entry is not None and entry["id"] == before["id"]
        assert len(after["revisions"]) == 2
        assert after["revisions"][0]["content"] == before["content"]
        assert after["revisions"][-1]["source"] == "ui"
        assert after["revisions"][-1]["version_number"] == 2
        assert after["current_revision_id"] == after["revisions"][-1]["revision_id"]
        assert after["content"] == "Use Memcached for hot-read caching instead"
        assert after["revision"] == 2

    def test_source_is_overridable(self, tmp_repo):
        entry = _approved(tmp_repo)
        store.edit_decision(tmp_repo, entry["id"], content="Use Memcached instead",
                            source="human")
        assert _entry(tmp_repo, entry["id"])["revisions"][-1]["source"] == "human"

    def test_returns_the_updated_entry(self, tmp_repo):
        entry = _approved(tmp_repo)
        ok, _msg, payload = store.edit_decision(tmp_repo, entry["id"], title="Cache hot reads")
        assert ok
        assert payload is not None
        assert payload["id"] == entry["id"]
        assert payload["title"] == "Cache hot reads"

    def test_short_id_accepted(self, tmp_repo):
        entry = _approved(tmp_repo)
        ok, _msg, _p = store.edit_decision(tmp_repo, entry["id"][:8], title="Cache hot reads")
        assert ok
        assert _entry(tmp_repo, entry["id"])["title"] == "Cache hot reads"

    def test_updated_at_moves_but_timestamp_does_not(self, tmp_repo):
        entry = _approved(tmp_repo)
        store.edit_decision(tmp_repo, entry["id"], content="Use Memcached instead")
        after = _entry(tmp_repo, entry["id"])
        assert after["timestamp"] == entry["timestamp"]
        assert after["updated_at"] >= entry["updated_at"]

    def test_edit_is_visible_in_get_context(self, tmp_repo):
        entry = _approved(tmp_repo)
        store.edit_decision(tmp_repo, entry["id"], content="Use Memcached for hot-read caching")
        rendered = store.get_context(tmp_repo)
        assert "Memcached" in rendered
        assert "Redis" not in rendered


# ── title handling ────────────────────────────────────────────────────────────

class TestTitle:
    def test_title_only_edit_does_not_wipe_content(self, tmp_repo):
        entry = _approved(tmp_repo)
        ok, _msg, _p = store.edit_decision(tmp_repo, entry["id"], title="Cache hot reads in Redis")
        assert ok
        after = _entry(tmp_repo, entry["id"])
        assert after["content"] == entry["content"]
        assert after["revisions"][-1]["content"] == entry["content"]
        assert after["title"] == "Cache hot reads in Redis"

    def test_title_is_normalized(self, tmp_repo):
        entry = _approved(tmp_repo)
        store.edit_decision(tmp_repo, entry["id"], title="  Cache   hot\nreads  ")
        assert _entry(tmp_repo, entry["id"])["title"] == "Cache hot reads"

    def test_content_edit_without_a_title_rederives_it(self, tmp_repo):
        entry = _approved(tmp_repo)
        store.edit_decision(tmp_repo, entry["id"], content="Use Memcached for hot-read caching")
        after = _entry(tmp_repo, entry["id"])
        assert after["title"] == "Use Memcached for hot-read caching"

    def test_subtype_only_edit_keeps_the_existing_title(self, tmp_repo):
        entry = _approved(tmp_repo)
        store.edit_decision(tmp_repo, entry["id"], title="Cache hot reads")
        store.edit_decision(tmp_repo, entry["id"], subtype="pattern")
        after = _entry(tmp_repo, entry["id"])
        assert after["title"] == "Cache hot reads"
        assert after["content"] == entry["content"]


# ── status preservation ───────────────────────────────────────────────────────

class TestStatusPreserved:
    def test_approved_stays_approved(self, tmp_repo):
        entry = _approved(tmp_repo)
        store.edit_decision(tmp_repo, entry["id"], content="Use Memcached for hot-read caching")
        after = _entry(tmp_repo, entry["id"])
        assert after["status"] == "approved"
        # An edit never invents an approver - it only preserves what was already there.
        assert after.get("approved_by") == entry.get("approved_by")
        assert after["revisions"][-1]["approved_at"] is not None
        assert store.get_pending_decisions(tmp_repo) == []

    def test_pending_stays_pending(self, tmp_repo):
        entry = _pending(tmp_repo)
        store.edit_decision(tmp_repo, entry["id"],
                            content="Never commit generated lockfiles to any branch")
        after = _entry(tmp_repo, entry["id"])
        assert after["status"] == "pending_approval"
        assert "approved_by" not in after
        assert after["revisions"][-1]["approved_at"] is None
        assert [e["id"] for e in store.get_pending_decisions(tmp_repo)] == [entry["id"]]

    def test_editing_a_pending_decision_leaves_it_approvable(self, tmp_repo):
        entry = _pending(tmp_repo)
        store.edit_decision(tmp_repo, entry["id"],
                            content="Never commit generated lockfiles to any branch")
        ok, msg = store.approve_decision(tmp_repo, entry["id"], "approve")
        assert ok, msg
        after = _entry(tmp_repo, entry["id"])
        assert after["status"] == "approved"
        assert "any branch" in after["content"]

    def test_ignored_stays_ignored(self, tmp_repo):
        entry = _approved(tmp_repo)
        store.approve_decision(tmp_repo, entry["id"], "ignore")
        store.edit_decision(tmp_repo, entry["id"], content="Use Memcached for hot-read caching")
        assert _entry(tmp_repo, entry["id"])["status"] == "ignored"


# ── recurrence metadata is not touched ────────────────────────────────────────

class TestRecurrenceUntouched:
    def test_occurrence_count_and_sessions_unchanged(self, tmp_repo):
        entry = _approved(tmp_repo)
        store.update_decision(tmp_repo, APPROVED, "sess-2")     # a restatement -> recurrence
        before = _entry(tmp_repo, entry["id"])
        assert before["occurrence_count"] == 2

        store.edit_decision(tmp_repo, entry["id"], content="Use Memcached for hot-read caching")

        after = _entry(tmp_repo, entry["id"])
        assert after["occurrence_count"] == before["occurrence_count"]
        assert after["session_ids"] == before["session_ids"]


# ── dedup / containment routing is deliberately bypassed ──────────────────────

class TestNoProposalRouting:
    def test_edit_of_a_constraint_applies_directly(self, tmp_repo):
        # A human edit must never be rerouted into a Suggested Update against itself, which
        # is what update_decision's approval gate would do to an approved constraint.
        entry = _approved(tmp_repo, "Always pin dependencies to exact versions",
                          subtype="constraint")
        store.edit_decision(tmp_repo, entry["id"],
                            content="Always pin runtime dependencies to exact versions")
        after = _entry(tmp_repo, entry["id"])
        assert "proposed_revision" not in after
        assert after["content"] == "Always pin runtime dependencies to exact versions"
        assert after["revision"] == 2
        assert store.get_pending_decisions(tmp_repo) == []

    def test_near_identical_edit_still_creates_a_revision(self, tmp_repo):
        # >70% token overlap with the previous content - a dedup-routed path would drop it.
        entry = _approved(tmp_repo)
        store.edit_decision(tmp_repo, entry["id"], content=APPROVED + " under load")
        after = _entry(tmp_repo, entry["id"])
        assert after["revision"] == 2
        assert after["content"].endswith("under load")

    def test_edit_does_not_discard_an_unreviewed_proposal(self, tmp_repo):
        entry = _approved(tmp_repo, "Always pin dependencies to exact versions",
                          subtype="constraint")
        store.update_decision(tmp_repo, "Always pin dependencies to exact minor versions",
                              "sess-2", subtype="constraint", replace_id=entry["id"])
        assert _entry(tmp_repo, entry["id"]).get("proposed_revision")

        store.edit_decision(tmp_repo, entry["id"], title="Pin dependencies")

        assert _entry(tmp_repo, entry["id"]).get("proposed_revision"), \
            "an edit must not silently drop a Suggested Update awaiting review"


# ── subtype ───────────────────────────────────────────────────────────────────

class TestSubtype:
    @pytest.mark.parametrize("subtype", sorted(store._SUBTYPES))
    def test_every_vocabulary_member_is_accepted(self, tmp_repo, subtype):
        entry = _approved(tmp_repo)
        ok, _msg, _p = store.edit_decision(tmp_repo, entry["id"], subtype=subtype)
        assert ok
        assert _entry(tmp_repo, entry["id"])["subtype"] == subtype

    @pytest.mark.parametrize("subtype", ["Architecture", "decision", "constraints", "x"])
    def test_off_vocabulary_subtype_is_rejected(self, tmp_repo, subtype):
        entry = _approved(tmp_repo, subtype="architecture")
        ok, msg, payload = store.edit_decision(tmp_repo, entry["id"], subtype=subtype)
        assert ok is False
        assert "Invalid subtype" in msg
        assert payload is None
        assert _entry(tmp_repo, entry["id"])["subtype"] == "architecture"

    def test_rejected_subtype_writes_nothing(self, tmp_repo):
        entry = _approved(tmp_repo)
        before = store._store_path(tmp_repo).read_text(encoding="utf-8")
        store.edit_decision(tmp_repo, entry["id"], content="Use Memcached instead", subtype="nope")
        assert store._store_path(tmp_repo).read_text(encoding="utf-8") == before

    @pytest.mark.parametrize("subtype", ["", "   ", "\t"])
    def test_a_blank_subtype_means_leave_it_alone(self, tmp_repo, subtype):
        # Capture is permissive, so an unsubtyped legacy entry carries "" - and the console
        # posts the field on every save. Rejecting "" made those decisions uneditable.
        entry = _approved(tmp_repo, subtype="architecture")
        ok, msg, payload = store.edit_decision(tmp_repo, entry["id"], title="Cache hot reads",
                                               subtype=subtype)
        assert ok, msg
        assert payload is not None
        after = _entry(tmp_repo, entry["id"])
        assert after["subtype"] == "architecture"
        assert after["title"] == "Cache hot reads"

    def test_a_legacy_unsubtyped_decision_stays_editable(self, tmp_repo):
        entry = _approved(tmp_repo, subtype="")
        assert entry["subtype"] == ""
        ok, msg, _p = store.edit_decision(tmp_repo, entry["id"], subtype="",
                                          content="Use Memcached for hot-read caching")
        assert ok, msg
        after = _entry(tmp_repo, entry["id"])
        assert after["subtype"] == ""
        assert after["content"] == "Use Memcached for hot-read caching"
        assert after["revision"] == 2

    def test_a_blank_subtype_on_its_own_changes_nothing(self, tmp_repo):
        entry = _approved(tmp_repo, subtype="architecture")
        ok, msg, payload = store.edit_decision(tmp_repo, entry["id"], subtype="")
        assert ok is False
        assert "Nothing to change" in msg
        assert payload is None
        assert _entry(tmp_repo, entry["id"])["revision"] == 1

    def test_subtype_change_is_findable_by_entry_type(self, tmp_repo):
        entry = _approved(tmp_repo, subtype="architecture")
        store.edit_decision(tmp_repo, entry["id"], subtype="pattern")
        assert "Redis" in store.get_context(tmp_repo, entry_type="pattern")
        assert "Redis" not in store.get_context(tmp_repo, entry_type="architecture")


# ── if_version (optimistic concurrency) ───────────────────────────────────────

class TestIfVersion:
    def test_matching_version_applies(self, tmp_repo):
        entry = _approved(tmp_repo)
        ok, _msg, _p = store.edit_decision(tmp_repo, entry["id"],
                                           content="Use Memcached instead", if_version=1)
        assert ok
        assert _entry(tmp_repo, entry["id"])["revision"] == 2

    def test_stale_version_is_a_conflict_and_writes_nothing(self, tmp_repo):
        entry = _approved(tmp_repo)
        store.edit_decision(tmp_repo, entry["id"], content="Use Memcached for hot-read caching")
        before = store._store_path(tmp_repo).read_text(encoding="utf-8")

        ok, msg, payload = store.edit_decision(
            tmp_repo, entry["id"], content="Use DynamoDB instead", if_version=1)

        assert ok is False
        assert msg == store.EDIT_CONFLICT
        assert payload == {"current_version": 2}
        assert store._store_path(tmp_repo).read_text(encoding="utf-8") == before

    def test_conflict_message_is_distinguishable_from_other_failures(self, tmp_repo):
        entry = _approved(tmp_repo)
        _ok, not_found, _p = store.edit_decision(tmp_repo, "no-such-id", title="x")
        _ok2, invalid, _p2 = store.edit_decision(tmp_repo, entry["id"], subtype="bogus")
        assert store.EDIT_CONFLICT not in not_found
        assert store.EDIT_CONFLICT not in invalid

    def test_current_version_reported_after_several_edits(self, tmp_repo):
        entry = _approved(tmp_repo)
        store.edit_decision(tmp_repo, entry["id"], title="One")
        store.edit_decision(tmp_repo, entry["id"], title="Two")
        _ok, _msg, payload = store.edit_decision(tmp_repo, entry["id"], title="Three",
                                                 if_version=1)
        assert payload == {"current_version": 3}

    def test_if_version_checked_before_subtype_is_written(self, tmp_repo):
        entry = _approved(tmp_repo, subtype="architecture")
        store.edit_decision(tmp_repo, entry["id"], title="One")
        store.edit_decision(tmp_repo, entry["id"], subtype="pattern", if_version=1)
        assert _entry(tmp_repo, entry["id"])["subtype"] == "architecture"


# ── failure modes ─────────────────────────────────────────────────────────────

class TestFailureModes:
    def test_unknown_id(self, tmp_repo):
        _approved(tmp_repo)
        ok, msg, payload = store.edit_decision(tmp_repo, "not-an-id", title="x")
        assert ok is False
        assert "not found" in msg
        assert payload is None

    def test_empty_id(self, tmp_repo):
        _approved(tmp_repo)
        ok, _msg, _p = store.edit_decision(tmp_repo, "", title="x")
        assert ok is False

    def test_nothing_passed_is_rejected(self, tmp_repo):
        entry = _approved(tmp_repo)
        ok, msg, payload = store.edit_decision(tmp_repo, entry["id"])
        assert ok is False
        assert "Nothing to change" in msg
        assert payload is None
        assert _entry(tmp_repo, entry["id"])["revision"] == 1

    @pytest.mark.parametrize("content", ["", "   ", "!!! ...", "\n\t"])
    def test_blank_content_never_wipes_a_decision(self, tmp_repo, content):
        entry = _approved(tmp_repo)
        ok, msg, _p = store.edit_decision(tmp_repo, entry["id"], content=content)
        assert ok is False
        assert "at least one word" in msg
        assert _entry(tmp_repo, entry["id"])["content"] == entry["content"]

    def test_edit_on_an_empty_store(self, tmp_repo):
        ok, msg, _p = store.edit_decision(tmp_repo, "abc12345", title="x")
        assert ok is False
        assert "not found" in msg

    def test_edit_of_a_deleted_decision_is_not_found(self, tmp_repo):
        entry = _approved(tmp_repo)
        store.delete_decision(tmp_repo, entry["id"])
        ok, msg, _p = store.edit_decision(tmp_repo, entry["id"], title="x")
        assert ok is False
        assert "not found" in msg


class TestSupersededProposal:
    """A CONTENT edit must not leave a Suggested Update pending against the text it replaced.

    Approving that stale proposal promoted the pre-edit content over the developer's rewrite,
    silently reverting an explicit human edit. `if_version` does not cover it: approving a
    proposal checks no version at all.
    """

    @staticmethod
    def _with_proposal(repo: str) -> dict:
        entry = _approved(repo, "Use Postgres as the primary datastore for all services")
        store.update_decision(
            repo, "Use Postgres as the primary datastore for all services and read replicas",
            "sess-proposer", subtype="architecture", replace_id=entry["id"])
        assert _entry(repo, entry["id"]).get("proposed_revision")
        return entry

    def test_content_edit_supersedes_the_proposal(self, tmp_repo):
        entry = self._with_proposal(tmp_repo)
        ok, msg, _p = store.edit_decision(tmp_repo, entry["id"],
                                          content="Use MySQL - Postgres was rejected on cost")
        assert ok
        assert "superseded" in msg
        assert "proposed_revision" not in _entry(tmp_repo, entry["id"])

    def test_approving_after_a_content_edit_cannot_revert_it(self, tmp_repo):
        entry = self._with_proposal(tmp_repo)
        store.edit_decision(tmp_repo, entry["id"],
                            content="Use MySQL - Postgres was rejected on cost")
        store.approve_decision(tmp_repo, entry["id"], "approve")
        # The whole point: the developer's edit is still what a session would replay.
        assert _entry(tmp_repo, entry["id"])["content"].startswith("Use MySQL")

    def test_the_superseded_proposal_is_kept_for_the_timeline(self, tmp_repo):
        entry = self._with_proposal(tmp_repo)
        store.edit_decision(tmp_repo, entry["id"],
                            content="Use MySQL - Postgres was rejected on cost")
        superseded = _entry(tmp_repo, entry["id"])["superseded_proposals"]
        assert len(superseded) == 1
        assert "read replicas" in superseded[0]["content"]
        assert superseded[0]["superseded_at"]

    def test_a_no_op_content_edit_keeps_the_proposal(self, tmp_repo):
        # Same text back is not a rewrite, so the proposal still reads against live content.
        entry = self._with_proposal(tmp_repo)
        ok, _msg, _p = store.edit_decision(tmp_repo, entry["id"], content=entry["content"])
        assert ok
        assert _entry(tmp_repo, entry["id"]).get("proposed_revision")
