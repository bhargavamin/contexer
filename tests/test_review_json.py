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

from contexer import cli, console_api, lifecycle, share_policy, store
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
                         "count": 0, "items": []}

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
