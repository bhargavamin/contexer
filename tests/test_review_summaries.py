"""Review summaries: a short Simplified Technical English version of a decision, for the
developer reviewing it.

The full content stays what the model reads and what an approval signs; the summary is a
reading aid that review surfaces lead with. These tests pin the rules the design settled on:
the deterministic shape check, that short content is its own summary, that a long model
capture must carry one (bounced otherwise), that every edit drops the old summary unless a
new one comes with it, that the model-facing injection never contains it, and that the review
queue, its basis, the terminal review, `review --json summarize` and export carry it.
"""
import json

import pytest

from contexer import cli, console_api, export, revisions, server, store

LONG = (
    "Read the store directory only through store.store_dir() and store.sidecar_path(). "
    "Production never touches the STORE_DIR constant, and no module joins a sidecar name onto a "
    "directory by hand: store.sidecar_path(kind, **fields) is the single builder. "
    "store.ensure_store_dir() owns the mode=0o700 creation. Reasoning: the directory was spelled "
    "out at 38 hand-written call sites, so a test that redirected HOME still leaked writes into "
    "the real store. Enforced by tests/test_store_dir_seam.py, which fails on any new path."
)
SUMMARY = ("Find the store folder only through two helper functions. "
           "This keeps test redirects and file permissions correct in one place.")
SHORT = "Always use uv for Python installs."


def _entry(repo: str, eid: str) -> dict:
    return next(e for e in store.load(repo)["entries"] if e.get("id") == eid)


@pytest.fixture
def mcp_repo(tmp_repo, monkeypatch):
    monkeypatch.setattr(store, "resolve_repo_verbose", lambda p: (tmp_repo, "argument"))
    monkeypatch.setattr(store, "resolve_repo", lambda p: tmp_repo)
    return tmp_repo


class TestShapeCheck:
    def test_a_short_plain_summary_passes(self):
        assert revisions.summary_problem(SUMMARY) is None

    @pytest.mark.parametrize("text, why", [
        ("", "empty"),
        ("One. Two. Three. Four. Five. Six.", "6 sentences"),
        (" ".join(["word"] * 21) + ".", "21 words"),
        ("Short. " * 3 + "x" * 600, "characters"),
    ])
    def test_a_summary_that_does_not_fit_names_why(self, text, why):
        assert why in revisions.summary_problem(text)

    def test_set_summary_normalizes_or_drops_the_key(self):
        record = {"summary": "old"}
        revisions.set_summary(record, "  Two  spaces.\n")
        assert record == {"summary": "Two spaces."}
        revisions.set_summary(record, "   ")
        assert record == {}

    def test_short_content_is_its_own_summary(self):
        assert not revisions.needs_summary(SHORT)
        assert revisions.review_summary({"content": SHORT}) == SHORT

    def test_long_content_without_a_summary_has_none_to_show(self):
        assert revisions.needs_summary(LONG)
        assert revisions.review_summary({"content": LONG}) is None


class TestCapture:
    def test_a_long_model_capture_without_a_summary_is_bounced(self, mcp_repo):
        out = server.update_context(LONG, subtype="architecture")
        assert out.startswith("Not stored.") and "summary" in out
        assert store.load(mcp_repo)["entries"] == []

    def test_an_oversized_summary_is_bounced_with_the_reason(self, mcp_repo):
        out = server.update_context(LONG, subtype="architecture",
                                    summary="One. Two. Three. Four. Five. Six.")
        assert out.startswith("Not stored.") and "6 sentences" in out

    def test_a_long_capture_with_a_summary_stores_both(self, mcp_repo):
        out = server.update_context(LONG, subtype="architecture", summary=SUMMARY)
        assert "id=" in out
        (entry,) = store.load(mcp_repo)["entries"]
        assert entry["summary"] == SUMMARY
        assert revisions.current_revision(entry)["summary"] == SUMMARY
        assert revisions.current_content(entry).startswith("Read the store directory")

    def _stored(self, repo: str) -> str:
        server.update_context(LONG, subtype="convention", summary=SUMMARY)
        (entry,) = store.load(repo)["entries"]
        return entry["id"]

    def test_a_title_only_correction_of_long_text_needs_no_summary(self, mcp_repo):
        eid = self._stored(mcp_repo)
        out = server.update_context(LONG, subtype="convention", replace_id=eid[:8],
                                    title="Use the store directory helpers")
        assert not out.startswith("Not stored."), out
        assert _entry(mcp_repo, eid)["summary"] == SUMMARY

    def test_a_stale_note_reverify_of_long_text_needs_no_summary(self, mcp_repo, monkeypatch):
        eid = self._stored(mcp_repo)
        monkeypatch.setattr(store, "_anchor_sources", lambda *a, **k: None)
        out = server.update_context(LONG, subtype="convention", replace_id=eid,
                                    source_files=["contexer/store.py"])
        assert not out.startswith("Not stored."), out
        assert _entry(mcp_repo, eid)["summary"] == SUMMARY

    def test_a_correction_still_checks_a_sent_summary_and_new_text(self, mcp_repo):
        eid = self._stored(mcp_repo)
        out = server.update_context(LONG, subtype="convention", replace_id=eid,
                                    summary="One. Two. Three. Four. Five. Six.")
        assert out.startswith("Not stored.") and "6 sentences" in out
        out = server.update_context(LONG + " Also for global rules.", subtype="convention",
                                    replace_id=eid)
        assert out.startswith("Not stored.") and "summary" in out

    def test_a_reworded_long_correction_without_a_summary_stores_nothing(self, mcp_repo):
        eid = self._stored(mcp_repo)
        before = _entry(mcp_repo, eid)
        out = server.update_context(LONG + " Also for global rules.", subtype="convention",
                                    replace_id=eid)
        assert out.startswith("Not stored.") and "summary" in out
        assert _entry(mcp_repo, eid) == before

    def test_the_writer_refuses_changed_long_text_under_the_lock(self, mcp_repo):
        # The decision is made against the text the lock holds, not a read taken before it.
        eid = self._stored(mcp_repo)
        before = _entry(mcp_repo, eid)
        stored, _, meta = store.update_decision_with_meta(
            mcp_repo, LONG + " Also for global rules.", "s2", "convention",
            replace_id=eid, require_summary=True)
        assert stored and meta["refusal_ack"] == store.summary_missing_notice()
        assert _entry(mcp_repo, eid) == before

    def test_a_non_mcp_caller_still_stores_changed_long_text_without_a_summary(self, mcp_repo):
        # Only the MCP surface passes require_summary; reconcile and other internal writers
        # have no model turn to restate in, so their correction is stored, summary-less.
        eid = self._stored(mcp_repo)
        reworded = LONG + " Also for global rules."
        stored, entry_id, meta = store.update_decision_with_meta(
            mcp_repo, reworded, "s2", "convention", created_by="ai", replace_id=eid)
        assert (stored, entry_id, meta) == (True, eid, {})
        entry = _entry(mcp_repo, eid)
        landed = entry.get("proposed_revision") or revisions.current_revision(entry)
        assert landed["content"] == revisions.normalize_content(reworded)
        assert not landed.get("summary")

    def test_an_unknown_replace_id_holds_long_text_to_the_new_capture_rule(self, mcp_repo):
        # No decision matches, so the write falls through to a new capture: it needs a summary.
        out = server.update_context(LONG, subtype="architecture", replace_id="deadbeef")
        assert out == store.summary_missing_notice()
        assert store.load(mcp_repo)["entries"] == []

    def test_an_applies_when_only_correction_keeps_the_summary(self, mcp_repo):
        eid = self._stored(mcp_repo)
        out = server.update_context(LONG, subtype="convention", replace_id=eid,
                                    applies_when=["store directory paths"])
        assert not out.startswith("Not stored."), out
        entry = _entry(mcp_repo, eid)
        assert len(entry["revisions"]) == 2
        assert revisions.current_revision(entry)["summary"] == SUMMARY
        assert entry["summary"] == SUMMARY

    def test_an_applies_when_only_suggested_update_keeps_the_summary(self, mcp_repo):
        ok, eid = store.update_decision(mcp_repo, LONG, "s1", "architecture",
                                        created_by="human", summary=SUMMARY)
        assert ok
        out = server.update_context(LONG, subtype="architecture", replace_id=eid,
                                    applies_when=["store directory paths"])
        assert not out.startswith("Not stored."), out
        entry = _entry(mcp_repo, eid)
        assert entry["proposed_revision"]["applies_when"] == ["store directory paths"]
        assert entry["proposed_revision"]["summary"] == SUMMARY

    def test_short_content_needs_no_summary(self, mcp_repo):
        assert "id=" in server.update_context(SHORT, subtype="convention")

    def test_the_model_never_reads_the_summary(self, mcp_repo):
        server.update_context(LONG, subtype="convention", summary=SUMMARY)
        assert "helper functions" not in store.get_context(mcp_repo)
        assert "helper functions" not in store.get_context(mcp_repo, query="store directory")


class TestRevisions:
    def _approved(self, repo: str) -> str:
        ok, eid = store.update_decision(repo, LONG, "s1", "architecture", created_by="human",
                                        summary=SUMMARY)
        assert ok
        return eid

    def test_a_suggested_update_carries_its_own_summary_and_keeps_the_current_one(self, tmp_repo):
        eid = self._approved(tmp_repo)
        store.update_decision(tmp_repo, LONG + " Also for global rules.", "s2", "architecture",
                              replace_id=eid, summary="Also use the helpers for global rules.")
        entry = _entry(tmp_repo, eid)
        assert entry["summary"] == SUMMARY
        assert entry["proposed_revision"]["summary"] == "Also use the helpers for global rules."

    def test_approving_the_update_promotes_its_summary(self, tmp_repo):
        eid = self._approved(tmp_repo)
        store.update_decision(tmp_repo, LONG + " Also for global rules.", "s2", "architecture",
                              replace_id=eid, summary="Also use the helpers for global rules.")
        assert store.approve_decision(tmp_repo, eid, "approve")[0]
        assert _entry(tmp_repo, eid)["summary"] == "Also use the helpers for global rules."

    def test_an_edit_at_approval_drops_the_proposal_summary(self, tmp_repo):
        eid = self._approved(tmp_repo)
        store.update_decision(tmp_repo, LONG + " Also for global rules.", "s2", "architecture",
                              replace_id=eid, summary="Also use the helpers for global rules.")
        assert store.approve_decision(tmp_repo, eid, "edit", LONG + " Edited by hand.")[0]
        assert "summary" not in _entry(tmp_repo, eid)

    def test_an_edit_at_approval_with_the_proposal_text_keeps_the_summary_sent(self, tmp_repo):
        eid = self._approved(tmp_repo)
        store.update_decision(tmp_repo, LONG + " Also for global rules.", "s2", "architecture",
                              replace_id=eid, summary="Also use the helpers for global rules.")
        prop_content = _entry(tmp_repo, eid)["proposed_revision"]["content"]
        assert store.approve_decision(tmp_repo, eid, "edit", prop_content,
                                      summary="Use the helpers everywhere.")[0]
        assert _entry(tmp_repo, eid)["summary"] == "Use the helpers everywhere."

    def test_an_edit_keeps_only_the_summary_sent_with_it(self, tmp_repo):
        ok, eid = store.update_decision(tmp_repo, LONG, "s1", "constraint", summary=SUMMARY)
        assert store.entry_status(_entry(tmp_repo, eid)) == "pending_approval"
        assert store.approve_decision(tmp_repo, eid, "edit", LONG + " Edited.",
                                      summary="A new plain summary.")[0]
        assert _entry(tmp_repo, eid)["summary"] == "A new plain summary."

    def test_a_trivial_revision_does_not_inherit_the_old_summary(self, tmp_repo):
        ok, eid = store.update_decision(tmp_repo, LONG, "s1", "convention", created_by="human",
                                        summary=SUMMARY)
        store.update_decision(tmp_repo, LONG + " Also global rules.", "s2", "convention",
                              replace_id=eid, created_by="human")
        assert "summary" not in _entry(tmp_repo, eid)

    def test_a_model_cannot_reword_the_summary_of_a_trusted_decision(self, mcp_repo):
        # The developer reviews by the summary, so a model rewording it in place could make a
        # later reversal look harmless on every review surface.
        eid = self._approved(mcp_repo)
        version = _entry(mcp_repo, eid)["revision"]
        server.update_context(LONG, subtype="architecture", replace_id=eid,
                              summary="This rule is old and safe to drop.")
        entry = _entry(mcp_repo, eid)
        assert entry["summary"] == SUMMARY
        assert entry["revision"] == version and not entry.get("proposed_revision")

    @pytest.mark.parametrize("source", ["scan", "bootstrap"])
    def test_no_model_supplied_source_can_reword_a_trusted_summary(self, mcp_repo, source):
        # update_context accepts scan and bootstrap from the model too, so they are no
        # more trusted than ai to reword what the developer reviews by.
        eid = self._approved(mcp_repo)
        version = _entry(mcp_repo, eid)["revision"]
        server.update_context(LONG, subtype="architecture", replace_id=eid, created_by=source,
                              summary="This rule is old and safe to drop.")
        entry = _entry(mcp_repo, eid)
        assert entry["summary"] == SUMMARY
        assert entry["revision"] == version and not entry.get("proposed_revision")

    def test_a_model_cannot_put_a_summary_over_a_short_trusted_decision(self, mcp_repo):
        # Short content is its own summary and stores none; a model summary on top of it
        # would become what review surfaces lead with.
        ok, eid = store.update_decision(mcp_repo, "Never push to main.", "s1", "constraint",
                                        created_by="human")
        assert "summary" not in _entry(mcp_repo, eid)
        server.update_context("Never push to main.", subtype="constraint", replace_id=eid,
                              summary="Pushing to main is allowed now.")
        entry = _entry(mcp_repo, eid)
        assert "summary" not in entry
        assert revisions.review_summary(entry) == "Never push to main."

    def test_an_applies_when_correction_keeps_a_trusted_summary(self, mcp_repo):
        # A pattern update applies as a new approved revision with no review, so a model
        # summary riding on an applies_when change must not replace the trusted one.
        ok, eid = store.update_decision(mcp_repo, LONG, "s1", "pattern", created_by="human",
                                        summary=SUMMARY)
        version = _entry(mcp_repo, eid)["revision"]
        server.update_context(LONG, subtype="pattern", replace_id=eid,
                              applies_when=["when editing sidecar paths"],
                              summary="This rule is old and safe to drop.")
        entry = _entry(mcp_repo, eid)
        assert entry["revision"] != version
        assert entry["applies_when"] == ["when editing sidecar paths"]
        assert entry["summary"] == SUMMARY

    @pytest.mark.parametrize("change", [
        {"applies_when": ["when editing sidecar paths"]},
        {"title": "Reach the store folder only through helpers"},
    ])
    def test_a_gated_suggested_update_keeps_a_trusted_summary(self, mcp_repo, change):
        # A gated applies_when or title change to a trusted constraint goes to review, and the
        # unchanged text carries the proposal summary onto the approved revision, so a model
        # summary riding on it must not replace the trusted one there either.
        ok, eid = store.update_decision(mcp_repo, LONG, "s1", "constraint", created_by="human",
                                        summary=SUMMARY)
        server.update_context(LONG, subtype="constraint", replace_id=eid,
                              summary="This rule is old and safe to drop.", **change)
        entry = _entry(mcp_repo, eid)
        assert entry["proposed_revision"]["summary"] == SUMMARY
        assert store.approve_decision(mcp_repo, eid, "approve")[0]
        entry = _entry(mcp_repo, eid)
        assert len(entry["revisions"]) == 2
        assert revisions.current_revision(entry)["summary"] == SUMMARY

    def test_a_pending_decision_summary_is_corrected_in_place(self, mcp_repo):
        ok, eid = store.update_decision(mcp_repo, LONG, "s1", "constraint", summary=SUMMARY)
        assert store.entry_status(_entry(mcp_repo, eid)) == "pending_approval"
        version = _entry(mcp_repo, eid)["revision"]
        server.update_context(LONG, subtype="constraint", replace_id=eid,
                              summary="Use the two helpers for the store folder.")
        entry = _entry(mcp_repo, eid)
        assert entry["summary"] == "Use the two helpers for the store folder."
        assert entry["revision"] == version

    def test_a_trusted_decision_without_a_summary_gets_one_in_place(self, mcp_repo):
        ok, eid = store.update_decision(mcp_repo, LONG, "s1", "architecture", created_by="human")
        assert "summary" not in _entry(mcp_repo, eid)
        version = _entry(mcp_repo, eid)["revision"]
        server.update_context(LONG, subtype="architecture", replace_id=eid,
                              summary="Use the two helpers for the store folder.")
        entry = _entry(mcp_repo, eid)
        assert entry["summary"] == "Use the two helpers for the store folder."
        assert entry["revision"] == version and not entry.get("proposed_revision")


class TestConsoleEdit:
    def test_a_content_edit_drops_the_old_summary(self, tmp_repo):
        ok, eid = store.update_decision(tmp_repo, LONG, "s1", "convention", created_by="human",
                                        summary=SUMMARY)
        assert store.edit_decision(tmp_repo, eid, content=LONG + " Edited.")[0]
        assert "summary" not in _entry(tmp_repo, eid)

    def test_a_title_edit_keeps_the_summary(self, tmp_repo):
        ok, eid = store.update_decision(tmp_repo, LONG, "s1", "convention", created_by="human",
                                        summary=SUMMARY)
        assert store.edit_decision(tmp_repo, eid, title="Use the store helpers")[0]
        assert _entry(tmp_repo, eid)["summary"] == SUMMARY

    def test_a_summary_only_edit_needs_no_new_revision(self, tmp_repo):
        ok, eid = store.update_decision(tmp_repo, LONG, "s1", "convention", created_by="human")
        version = _entry(tmp_repo, eid)["revision"]
        assert store.edit_decision(tmp_repo, eid, summary=SUMMARY)[0]
        entry = _entry(tmp_repo, eid)
        assert entry["summary"] == SUMMARY and entry["revision"] == version

    def test_a_console_summary_correction_with_unchanged_text_needs_no_new_revision(self, tmp_repo):
        # The console's Save always posts the content and title alongside the summary.
        ok, eid = store.update_decision(tmp_repo, LONG, "s1", "convention", created_by="human")
        before = _entry(tmp_repo, eid)
        assert store.edit_decision(tmp_repo, eid, content=revisions.current_content(before),
                                   title=before["title"], subtype="", source="human",
                                   if_version=before["revision"], summary=SUMMARY)[0]
        entry = _entry(tmp_repo, eid)
        assert entry["summary"] == SUMMARY
        assert entry["revision"] == before["revision"]
        assert len(entry["revisions"]) == len(before["revisions"])

    def test_an_edit_with_an_unfit_summary_is_refused(self, tmp_repo):
        ok, eid = store.update_decision(tmp_repo, LONG, "s1", "convention", created_by="human")
        ok, message, _ = store.edit_decision(tmp_repo, eid, summary=" ".join(["w"] * 30) + ".")
        assert not ok and "does not fit" in message


class TestReviewQueue:
    def test_items_lead_with_the_summary_or_say_none(self, tmp_repo):
        ok, with_summary = store.update_decision(tmp_repo, LONG, "s1", "constraint",
                                                 summary=SUMMARY)
        ok, without = store.update_decision(tmp_repo, (
            "Retry every Teams push with exponential backoff, capped at five attempts, and "
            "record each failure in the outbox so a later session can resume it. Reasoning: "
            "the endpoint sleeps on a cold start and drops the first request, and a single "
            "attempt lost about one share in ten during the pilot. A sixth attempt never "
            "succeeded in the pilot logs, so five is the cap."), "s1", "constraint")
        assert without
        items = {i["id"]: i for i in console_api.review_queue(tmp_repo)["items"]}
        assert items[with_summary]["summary"] == SUMMARY
        assert items[with_summary]["needs_summary"] is False
        assert items[without]["summary"] is None and items[without]["needs_summary"] is True

    def test_short_content_carries_no_summary_and_needs_none(self, tmp_repo):
        ok, eid = store.update_decision(tmp_repo, SHORT, "s1", "constraint")
        (item,) = console_api.review_queue(tmp_repo)["items"]
        assert item["summary"] is None and item["needs_summary"] is False

    def test_the_basis_covers_the_summary(self, tmp_repo):
        ok, eid = store.update_decision(tmp_repo, LONG, "s1", "constraint", summary=SUMMARY)
        before = console_api.review_basis(_entry(tmp_repo, eid))
        store.set_review_summary(tmp_repo, eid, "A different plain summary.")
        assert console_api.review_basis(_entry(tmp_repo, eid)) != before


class TestSummarizeAction:
    @pytest.fixture
    def in_repo(self, tmp_repo, monkeypatch):
        monkeypatch.setattr(store, "git_root", lambda _: tmp_repo)
        return tmp_repo

    def _run(self, capsys, *argv):
        code = 0
        try:
            cli.dispatch(["review", "--json", *argv])
        except SystemExit as exc:
            code = exc.code or 0
        return code, json.loads(capsys.readouterr().out)

    def test_it_stores_a_summary_and_settles_nothing(self, in_repo, capsys):
        ok, eid = store.update_decision(in_repo, LONG, "s1", "constraint")
        (item,) = console_api.review_queue(in_repo)["items"]
        code, out = self._run(capsys, "summarize", eid, "--summary", SUMMARY,
                              "--expect", item["basis"])
        assert code == 0 and out["ok"] is True
        entry = _entry(in_repo, eid)
        assert entry["summary"] == SUMMARY
        assert store.entry_status(entry) == "pending_approval", "a summary approves nothing"
        assert revisions.current_content(entry).startswith("Read the store directory")

    def test_it_refuses_a_changed_decision(self, in_repo, capsys):
        ok, eid = store.update_decision(in_repo, LONG, "s1", "constraint")
        code, out = self._run(capsys, "summarize", eid, "--summary", SUMMARY,
                              "--expect", "0000000000000000")
        assert code == 1 and "changed since" in out["message"]
        assert "summary" not in _entry(in_repo, eid)

    def test_it_refuses_a_summary_that_does_not_fit(self, in_repo, capsys):
        ok, eid = store.update_decision(in_repo, LONG, "s1", "constraint")
        code, out = self._run(capsys, "summarize", eid, "--summary", LONG)
        assert code == 1 and "does not fit" in out["message"]

    def test_it_summarizes_a_suggested_update(self, in_repo, capsys):
        ok, eid = store.update_decision(in_repo, LONG, "s1", "architecture", created_by="human")
        store.update_decision(in_repo, LONG + " Also global rules.", "s2", "architecture",
                              replace_id=eid, summary="")
        code, out = self._run(capsys, "summarize", eid, "--summary", SUMMARY, "--proposal")
        assert code == 0
        assert _entry(in_repo, eid)["proposed_revision"]["summary"] == SUMMARY


class TestTerminalReview:
    def test_it_leads_with_the_summary_and_prints_the_full_text_on_f(
            self, tmp_repo, monkeypatch, capsys):
        store.update_decision(tmp_repo, LONG, "s1", "constraint", summary=SUMMARY)
        monkeypatch.setattr(store, "git_root", lambda _: tmp_repo)
        answers = iter(["F", "S"])
        monkeypatch.setattr("builtins.input", lambda _="": next(answers))
        cli.review()
        out = " ".join(capsys.readouterr().out.split())   # the terminal wraps long lines
        first_card, after_f = out.split("[F] Full text", 1)
        assert "Find the store folder only through two helper functions" in first_card
        assert "call sites" not in first_card
        assert "call sites, so a test that redirected HOME" in after_f

    def test_an_edit_asks_for_a_summary_of_long_wording(self, tmp_repo, monkeypatch, capsys):
        ok, eid = store.update_decision(tmp_repo, LONG, "s1", "constraint", summary=SUMMARY)
        monkeypatch.setattr(store, "git_root", lambda _: tmp_repo)
        answers = iter(["E", LONG + " Edited.", "Use the helpers. Keep it in one place."])
        monkeypatch.setattr("builtins.input", lambda _="": next(answers))
        cli.review()
        assert _entry(tmp_repo, eid)["summary"] == "Use the helpers. Keep it in one place."

    def test_a_long_decision_without_a_summary_is_tagged(self, tmp_repo, monkeypatch, capsys):
        store.update_decision(tmp_repo, LONG, "s1", "constraint")
        monkeypatch.setattr(store, "git_root", lambda _: tmp_repo)
        monkeypatch.setattr("builtins.input", lambda _="": "S")
        cli.review()
        out = capsys.readouterr().out
        assert "(no summary)" in out and "[F] Full text" not in out


class TestExport:
    def test_the_summary_sits_beside_the_full_text(self, tmp_repo):
        ok, eid = store.update_decision(tmp_repo, LONG, "s1", "convention", created_by="human",
                                        summary=SUMMARY)
        text = export.render(tmp_repo)["decisions.md"]
        assert SUMMARY in text and "38 hand-written call sites" in text


class TestApprovalPaths:
    def test_an_mcp_edit_of_long_text_without_a_summary_is_not_edited(self, mcp_repo):
        ok, eid = store.update_decision(mcp_repo, SHORT, "s1", "constraint")
        assert store.entry_status(_entry(mcp_repo, eid)) == "pending_approval"
        out = server.approve_decision(eid, "edit", LONG)
        assert out.startswith("Not edited.") and "summary" in out
        assert revisions.current_content(_entry(mcp_repo, eid)) == SHORT

    def test_an_mcp_edit_with_an_oversized_summary_is_not_edited(self, mcp_repo):
        ok, eid = store.update_decision(mcp_repo, SHORT, "s1", "constraint")
        out = server.approve_decision(eid, "edit", LONG, summary="One. Two. Three. Four. Five. Six.")
        assert out.startswith("Not edited.") and "6 sentences" in out and "Not stored" not in out

    def test_amending_a_pending_draft_keeps_only_the_summary_sent_with_it(self, tmp_repo):
        ok, eid = store.update_decision(tmp_repo, LONG, "s1", "constraint", summary=SUMMARY)
        assert store.entry_status(_entry(tmp_repo, eid)) == "pending_approval"
        store.update_decision(tmp_repo, LONG + " Also for global rules.", "s2", "constraint",
                              replace_id=eid, summary="Also use the helpers for global rules.")
        entry = _entry(tmp_repo, eid)
        assert entry["revision"] == 1 and not entry.get("proposed_revision"), "amended in place"
        assert entry["summary"] == "Also use the helpers for global rules."
        store.update_decision(tmp_repo, LONG + " Also for team rules.", "s3", "constraint",
                              replace_id=eid)
        entry = _entry(tmp_repo, eid)
        assert revisions.current_content(entry).endswith("Also for team rules.")
        assert "summary" not in entry and "summary" not in revisions.current_revision(entry)

    def test_ratifying_a_bootstrap_inference_carries_its_summary(self, tmp_repo):
        ok, eid = store.update_decision(tmp_repo, LONG, "s1", "convention", created_by="human")
        data = store.load(tmp_repo)
        entry = next(e for e in data["entries"] if e.get("id") == eid)
        entry["bootstrap"] = {"kind": "inferred", "assessment": "supported", "sources": []}
        entry["approved_by"] = "ai"
        store.save(tmp_repo, data)
        assert store.set_review_summary(tmp_repo, eid, SUMMARY)[0]
        ok, message = store.approve_decision(tmp_repo, eid, "approve")
        assert ok and "original inference preserved" in message
        entry = _entry(tmp_repo, eid)
        assert entry["approved_by"] == "human" and entry["revision"] == 2
        assert entry["summary"] == SUMMARY
        assert revisions.current_revision(entry)["summary"] == SUMMARY

    def test_a_containment_amend_of_a_pending_directive_drops_its_summary(self, tmp_repo):
        eid, _, status = store.capture_user_constraint(
            tmp_repo, "orders.py can only import payment_store", "s1")
        assert status == "pending_approval"
        assert store.set_review_summary(tmp_repo, eid, "Orders may use the payment store only.")[0]
        eid2, content, status = store.capture_user_constraint(
            tmp_repo, "orders.py can only import payment_store through payment_endpoint with "
            "normalized arguments before outbound execution", "s2")
        assert eid2 == eid and status == "pending_approval" and "payment_endpoint" in content
        entry = _entry(tmp_repo, eid)
        assert "summary" not in entry and "summary" not in revisions.current_revision(entry)
