"""`contexer review --json`: the machine-readable review queue the Claude Code mod reads.

The interactive `contexer review` stays the one complete review surface. The JSON mode exists
so an in-session surface (the Claude Code mod's band and pane) can list what waits on the
developer and act on one item per call, through the same store functions the terminal loop
uses. These tests pin the projection's shape (a mod reads it, so a renamed key breaks it
silently), which actions each kind of item offers, and that every refusal comes back as JSON
with a non-zero exit instead of a traceback or a prompt.
"""
import json

import pytest

from contexer import cli, conflicts, console_api, lifecycle, review, share_policy, store
from tests.conftest import _seed_entry

STANDING = "Use Postgres for the decision store; SQLite won't handle concurrent sessions"
UPDATE = "Switch to DynamoDB for the decision store; Postgres is superseded"
RULE = "Never delete database rows for an organisation deleted in Clerk; warn and count instead"


def _entry(repo: str, eid: str) -> dict:
    return next(e for e in store.load(repo)["entries"] if e.get("id") == eid)


def _approved(repo: str) -> str:
    return _seed_entry(repo, STANDING)["id"]


def _pending(repo: str, content: str = RULE) -> str:
    ok, eid = store.update_decision(repo, content, "s1", "constraint", created_by="ai")
    assert ok and store.entry_status(_entry(repo, eid)) == "pending_approval"
    return eid


PREFIX = "Prefix version strings with a lowercase v, as in v1.4.0, so they match git tags."
BARE = ("Publish versions as bare semantic versions such as 1.4.0; the package index rejects a "
        "leading letter.")


def _conflicting_pair(repo: str, left_status: str = "approved",
                      right_status: str = "approved", right_by: str = "human") -> tuple[str, str]:
    """Two current decisions that prescribe incompatible version formats (conflicts.current_pairs)."""
    left = _seed_entry(repo, PREFIX, subtype="convention", title="Prefix versions with v",
                       status=left_status)
    right = _seed_entry(repo, BARE, subtype="convention", title="Publish bare semantic versions",
                        status=right_status, created_by=right_by)
    return left["id"], right["id"]


def _with_update(repo: str, eid: str) -> None:
    ok, rid = store.update_decision(repo, UPDATE, "s2", "architecture", replace_id=eid)
    assert ok and rid == eid and _entry(repo, eid).get("proposed_revision")


@pytest.fixture
def in_repo(tmp_repo, monkeypatch):
    monkeypatch.setattr(store, "git_root", lambda _: tmp_repo)
    return tmp_repo


def _run(capsys, *argv) -> tuple[int, dict]:
    code = 0
    try:
        cli.dispatch(["review", "--json", *argv])
    except SystemExit as exc:
        code = exc.code or 0
    return code, json.loads(capsys.readouterr().out)


class TestQueueProjection:
    def test_an_empty_store_is_an_empty_queue(self, tmp_repo):
        queue = console_api.review_queue(tmp_repo)
        assert queue == {"protocol": console_api.REVIEW_PROTOCOL, "repo": tmp_repo,
                         "count": 0, "items": [], "conflicts": []}

    def test_a_new_pending_decision_offers_approve_edit_ignore(self, tmp_repo):
        eid = _pending(tmp_repo)
        (item,) = console_api.review_queue(tmp_repo)["items"]
        assert item["id"] == eid and item["kind"] == "new"
        assert item["content"] == _entry(tmp_repo, eid)["content"]
        assert item["title"] and item["subtype"] == "constraint"
        assert item["created_by"] == "ai" and item["timestamp"]
        assert item["origin"] == "captured by the assistant", "the shared provenance wording"
        assert item["actions"] == ["approve", "edit", "ignore"]
        assert "proposed" not in item

    def test_a_suggested_update_shows_both_versions_and_offers_dismiss(self, tmp_repo):
        eid = _approved(tmp_repo)
        _with_update(tmp_repo, eid)
        (item,) = console_api.review_queue(tmp_repo)["items"]
        assert item["kind"] == "update"
        assert item["content"] == STANDING
        assert item["proposed"]["content"] == _entry(tmp_repo, eid)["proposed_revision"]["content"]
        assert item["actions"] == ["approve", "edit", "dismiss"]

    def test_a_retirement_is_listed_but_left_to_the_terminal_review(self, tmp_repo):
        eid = _approved(tmp_repo)
        assert lifecycle.propose_lifecycle(tmp_repo, eid, "retire", "superseded",
                                           source="ai")["ok"]
        (item,) = console_api.review_queue(tmp_repo)["items"]
        assert item["kind"] == "retirement" and item["actions"] == []

    def test_a_retirement_carries_its_proposed_reason(self, tmp_repo):
        eid = _approved(tmp_repo)
        assert lifecycle.propose_lifecycle(tmp_repo, eid, "retire", "superseded by DynamoDB",
                                           source="ai")["ok"]
        (item,) = console_api.review_queue(tmp_repo)["items"]
        assert item["retirement"] == {"reason": "superseded by DynamoDB", "replacement_id": None}

    def test_approval_names_the_files_it_would_anchor(self, tmp_repo):
        ok, eid, _ = store.update_decision_with_meta(
            tmp_repo, RULE, "s1", "constraint", created_by="ai",
            anchor_candidates=["src/orgs/delete.py"], anchor_candidates_confirmed=True)
        assert ok
        (item,) = console_api.review_queue(tmp_repo)["items"]
        assert item["id"] == eid and item["anchors"] == ["src/orgs/delete.py"]

    def test_no_confirmed_files_means_no_anchors(self, tmp_repo):
        _pending(tmp_repo)
        (item,) = console_api.review_queue(tmp_repo)["items"]
        assert item["anchors"] == []

    def test_a_suggested_decision_is_not_a_review_item(self, tmp_repo):
        # `suggested` is an ACTIVE status (AI-captured, already replayed), not a backlog: the
        # pane must not turn deliberately unratified context into a nag.
        ok, eid = store.update_decision(tmp_repo, "Prefer small focused modules over large files",
                                        "s1", "pattern", created_by="ai")
        assert ok and store.entry_status(_entry(tmp_repo, eid)) == "suggested"
        assert console_api.review_queue(tmp_repo)["items"] == []

    def test_count_matches_the_items(self, tmp_repo):
        _pending(tmp_repo)
        _pending(tmp_repo, "Always run the formatter before pushing a branch to origin")
        queue = console_api.review_queue(tmp_repo)
        assert queue["count"] == len(queue["items"]) == 2


class TestApplicability:
    def test_items_carry_their_applicability(self, tmp_repo):
        eid = _pending(tmp_repo)
        data = store.load(tmp_repo)
        next(e for e in data["entries"] if e["id"] == eid)["applies_when"] = ["deleting organisations"]
        store.save(tmp_repo, data)
        (item,) = console_api.review_queue(tmp_repo)["items"]
        assert item["applies_when"] == ["deleting organisations"]

    def test_an_update_carries_current_and_proposed_applicability(self, tmp_repo):
        eid = _approved(tmp_repo)
        _with_update(tmp_repo, eid)
        data = store.load(tmp_repo)
        entry = next(e for e in data["entries"] if e["id"] == eid)
        entry["applies_when"] = ["the decision store"]
        entry["proposed_revision"]["applies_when"] = ["the decision store", "team sync"]
        store.save(tmp_repo, data)
        (item,) = console_api.review_queue(tmp_repo)["items"]
        assert item["applies_when"] == ["the decision store"]
        assert item["proposed"]["applies_when"] == ["the decision store", "team sync"]


class TestConflicts:
    def test_an_update_that_differs_is_flagged_as_a_conflict(self, tmp_repo):
        eid = _approved(tmp_repo)
        _with_update(tmp_repo, eid)
        (item,) = console_api.review_queue(tmp_repo)["items"]
        assert item["conflict"] is True and item["pick"] is None

    def test_a_proposal_without_applicability_inherits_rather_than_clears_it(self, tmp_repo):
        eid = _approved(tmp_repo)
        _with_update(tmp_repo, eid)
        data = store.load(tmp_repo)
        entry = next(e for e in data["entries"] if e["id"] == eid)
        entry["applies_when"] = ["the decision store"]
        entry["proposed_revision"].pop("applies_when", None)
        store.save(tmp_repo, data)
        (item,) = console_api.review_queue(tmp_repo)["items"]
        assert item["proposed"]["applies_when"] is None, "inherits on approval, not 'always'"

    def test_an_unknown_memo_choice_is_no_pick(self, tmp_repo):
        eid = _approved(tmp_repo)
        _with_update(tmp_repo, eid)
        assert conflicts.record_conflict_memo(tmp_repo, eid, "standing")[0]
        data = store.load(tmp_repo)
        next(e for e in data["entries"] if e["id"] == eid)["conflict_memo"]["choice"] = "maybe"
        store.save(tmp_repo, data)
        (item,) = console_api.review_queue(tmp_repo)["items"]
        assert item["pick"] is None

    def test_an_earlier_pick_is_reported_as_data(self, tmp_repo):
        # The pane words it for its own buttons, so it gets the pick, not a sentence that
        # names `approve`/`dismiss`.
        eid = _approved(tmp_repo)
        _with_update(tmp_repo, eid)
        assert conflicts.record_conflict_memo(tmp_repo, eid, "update")[0]
        (item,) = console_api.review_queue(tmp_repo)["items"]
        assert item["pick"] == "update"

    def test_contradicting_current_decisions_are_listed_with_both_sides(self, tmp_repo):
        left, right = _conflicting_pair(tmp_repo)
        queue = console_api.review_queue(tmp_repo)
        (pair,) = queue["conflicts"]
        assert {d["id"] for d in pair["decisions"]} == {left, right}
        assert pair["actions"] == review.item_actions("current_conflict") == ["keep"]
        assert pair["reason"] == conflicts.CURRENT_PAIR_REASON
        assert all(side["can_keep"] for side in pair["decisions"])
        assert queue["items"] == [], "a current conflict is not a pending decision"

    def test_a_side_with_a_pending_update_shows_it(self, tmp_repo):
        left, right = _conflicting_pair(tmp_repo)
        data = store.load(tmp_repo)
        side = next(e for e in data["entries"] if e.get("id") == left)
        side["proposed_revision"] = {"content": PREFIX + " Tags stay annotated.", "source": "ai"}
        store.save(tmp_repo, data)
        (pair,) = console_api.review_queue(tmp_repo)["conflicts"]
        sides = {d["id"]: d for d in pair["decisions"]}
        assert sides[left]["proposed"]["content"].endswith("Tags stay annotated.")
        assert "proposed" not in sides[right]

    def test_only_an_approved_side_can_be_kept(self, tmp_repo):
        left, right = _conflicting_pair(tmp_repo, right_status="suggested")
        (pair,) = console_api.current_conflicts(tmp_repo)
        keepable = {side["id"]: side["can_keep"] for side in pair["decisions"]}
        assert keepable == {left: True, right: False}

    def test_an_automatically_approved_side_cannot_be_kept(self, tmp_repo):
        # A scan fact is born approved with no human act; it must not replace a ratified rule.
        human, scan = _conflicting_pair(tmp_repo, right_by="scan")
        (pair,) = console_api.current_conflicts(tmp_repo)
        assert {s["id"]: s["can_keep"] for s in pair["decisions"]} == {human: True, scan: False}

    def test_a_pair_with_no_approved_side_offers_no_action(self, tmp_repo):
        _conflicting_pair(tmp_repo, left_status="suggested", right_status="suggested")
        (pair,) = console_api.current_conflicts(tmp_repo)
        assert pair["actions"] == []

    def test_no_conflicts_is_an_empty_list(self, tmp_repo):
        assert console_api.review_queue(tmp_repo)["conflicts"] == []


class TestListCommand:
    def test_prints_the_queue(self, in_repo, capsys):
        eid = _pending(in_repo)
        code, out = _run(capsys)
        assert code == 0 and out["count"] == 1 and out["items"][0]["id"] == eid

    def test_outside_a_repo_answers_json_and_exits_nonzero(self, monkeypatch, capsys):
        monkeypatch.setattr(store, "git_root", lambda _: None)
        code, out = _run(capsys)
        assert code == 1 and out["ok"] is False and "git repository" in out["message"]


class TestActions:
    def test_approve_approves_and_queues_the_share_policy(self, in_repo, monkeypatch, capsys):
        eid = _pending(in_repo)
        queued = []
        monkeypatch.setattr(share_policy, "enqueue_after_local_mutation",
                            lambda repo, i: queued.append(i))
        code, out = _run(capsys, "approve", eid)
        assert code == 0 and out["ok"] is True and out["queue"]["count"] == 0
        assert store.entry_status(_entry(in_repo, eid)) == "approved"
        assert queued == [eid]

    def test_ignore_ignores(self, in_repo, capsys):
        eid = _pending(in_repo)
        code, out = _run(capsys, "ignore", eid)
        assert code == 0 and out["ok"] is True
        assert store.entry_status(_entry(in_repo, eid)) == "ignored"

    def test_edit_approves_the_developers_wording(self, in_repo, capsys):
        eid = _pending(in_repo)
        wording = "Never delete rows for a deleted Clerk organisation; log a warning instead"
        code, out = _run(capsys, "edit", eid, "--content", wording)
        assert code == 0 and out["ok"] is True
        entry = _entry(in_repo, eid)
        assert store.entry_status(entry) == "approved"
        assert wording.lower() in entry["content"].lower()

    def test_dismiss_drops_a_suggested_update_and_keeps_the_decision(self, in_repo, capsys):
        eid = _approved(in_repo)
        _with_update(in_repo, eid)
        code, out = _run(capsys, "dismiss", eid)
        assert code == 0 and out["ok"] is True
        entry = _entry(in_repo, eid)
        assert not entry.get("proposed_revision") and entry["content"] == STANDING


class TestMachineOutputStaysMachineReadable:
    def test_a_store_failure_is_a_json_refusal_not_a_traceback(self, in_repo, monkeypatch, capsys):
        eid = _pending(in_repo)

        def broken(*_a, **_k):
            raise RuntimeError("store is unreadable")
        monkeypatch.setattr(store, "approve_decision", broken)
        code, out = _run(capsys, "approve", eid)
        assert code == 1 and out["ok"] is False and "store is unreadable" in out["message"]

    def test_json_output_never_carries_the_release_notice(self, in_repo, monkeypatch, capsys):
        # The mod discards stderr, so a notice printed after --json would be consumed unseen.
        shown = []
        monkeypatch.setattr(cli, "_print_update_backstop", lambda: shown.append(1))
        _run(capsys)
        assert shown == []

    def test_the_interactive_review_still_offers_the_notice(self, in_repo, monkeypatch, capsys):
        shown = []
        monkeypatch.setattr(cli, "_print_update_backstop", lambda: shown.append(1))
        cli.dispatch(["review"])
        assert shown == [1]


class TestKeepOneSideOfAConflict:
    def test_keep_supersedes_the_other_side_and_clears_the_conflict(self, in_repo, capsys):
        left, right = _conflicting_pair(in_repo)
        code, out = _run(capsys, "keep", left, "--over", right)
        assert code == 0 and out["ok"] is True
        assert out["queue"]["conflicts"] == []
        assert left in [e["id"] for e in store.load(in_repo)["entries"]]
        assert right not in [e["id"] for e in store.load(in_repo)["entries"]], "retired"

    def test_keep_records_why_in_the_lifecycle_history(self, in_repo, capsys):
        left, right = _conflicting_pair(in_repo)
        _run(capsys, "keep", right, "--over", left)
        graveyard, _ = store.read_deleted(in_repo)
        retired = next(e for e in graveyard["entries"] if e["id"] == left)
        assert "Publish bare semantic versions" in json.dumps(retired)

    def test_keep_never_supersedes_an_approved_decision_with_an_unratified_one(
            self, in_repo, capsys):
        approved, suggested = _conflicting_pair(in_repo, right_status="suggested")
        code, out = _run(capsys, "keep", suggested, "--over", approved)
        assert code == 1 and out["ok"] is False and "stated or approved" in out["message"]
        assert approved in [e["id"] for e in store.load(in_repo)["entries"]], "nothing retired"

    def test_keep_rechecks_the_pair_at_retirement_time(self, in_repo, capsys):
        # Shown in the pane, then settled elsewhere before the click: refused, not acted on.
        left, right = _conflicting_pair(in_repo)
        assert lifecycle.retire_decision(in_repo, right, "settled in a terminal")[0]
        third = _seed_entry(in_repo, BARE + " Always.", subtype="convention")["id"]
        code, out = _run(capsys, "keep", left, "--over", right)
        assert code == 1 and out["ok"] is False
        assert third in [e["id"] for e in store.load(in_repo)["entries"]]

    def test_keep_refuses_two_decisions_that_do_not_conflict(self, in_repo, capsys):
        a = _seed_entry(in_repo, STANDING)["id"]
        b = _seed_entry(in_repo, "Always run the formatter before pushing a branch")["id"]
        code, out = _run(capsys, "keep", a, "--over", b)
        assert code == 1 and out["ok"] is False
        assert b in [e["id"] for e in store.load(in_repo)["entries"]], "nothing retired"

    def test_keep_needs_the_other_side(self, in_repo, capsys):
        left, _right = _conflicting_pair(in_repo)
        code, out = _run(capsys, "keep", left)
        assert code == 1 and out["ok"] is False


class TestRefusals:
    def test_an_unknown_id_is_refused(self, in_repo, capsys):
        code, out = _run(capsys, "approve", "no-such-id")
        assert code == 1 and out["ok"] is False

    def test_an_action_the_item_does_not_offer_is_refused(self, in_repo, capsys):
        eid = _approved(in_repo)
        assert lifecycle.propose_lifecycle(in_repo, eid, "retire", "superseded",
                                           source="ai")["ok"]
        code, out = _run(capsys, "approve", eid)
        assert code == 1 and out["ok"] is False and "contexer review" in out["message"]
        assert _entry(in_repo, eid).get("proposed_lifecycle"), "nothing changed"

    def test_edit_without_wording_is_refused(self, in_repo, capsys):
        eid = _pending(in_repo)
        code, out = _run(capsys, "edit", eid)
        assert code == 1 and out["ok"] is False
        assert store.entry_status(_entry(in_repo, eid)) == "pending_approval"

    def test_an_unknown_action_is_refused(self, in_repo, capsys):
        eid = _pending(in_repo)
        code, out = _run(capsys, "retire", eid)
        assert code == 1 and out["ok"] is False

    def test_an_action_without_an_id_is_refused(self, in_repo, capsys):
        code, out = _run(capsys, "approve")
        assert code == 1 and out["ok"] is False
