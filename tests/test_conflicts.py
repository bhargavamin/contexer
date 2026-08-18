"""Tests for contexer/conflicts.py: labeled dual injection + resolution memos (issue #193).

The seams stay in store.py - the render loops (`get_context`, `_render_prompt_decisions`,
`session_start_payload`, `_rehydrate_working_set`, `format_pending_review`) are what a session
actually sees, so these exercise store's public surface and assert on what it renders; `store`
is imported directly for the entry-construction helpers the fixtures need (borrowed pattern:
test_anchors.py)."""
import threading
import time
from datetime import datetime, timezone

from contexer import store


CONFLICT_STANDING = "Use Postgres for the decision store; SQLite won't handle concurrent sessions"
CONFLICT_UPDATE = "Switch to DynamoDB for the decision store; Postgres is superseded"


def _entry(repo: str, eid: str) -> dict:
    return next(e for e in store._load(repo)["entries"] if e.get("id") == eid)


def _conflicted(repo: str, standing: str = CONFLICT_STANDING, update: str = CONFLICT_UPDATE,
                subtype: str = "architecture") -> str:
    """An approved decision carrying an ai-sourced Suggested Update - the shape #193 renders."""
    store.update_decision(repo, standing, "s1", subtype)
    data = store._load(repo)
    entry = next(e for e in data["entries"]
                 if e.get("type") == "decision" and e["content"].startswith(standing[:20]))
    entry["status"] = "approved"
    store._save(repo, data)
    eid = entry["id"]
    ok, rid = store.update_decision(repo, update, "s2", subtype, replace_id=eid)
    assert ok and rid == eid and _entry(repo, eid).get("proposed_revision")
    return eid


def _poke_proposal(repo: str, eid: str, **fields) -> None:
    data = store._load(repo)
    entry = next(e for e in data["entries"] if e.get("id") == eid)
    entry["proposed_revision"].update(fields)
    store._save(repo, data)


class TestConflictDualInjection:
    def test_get_context_renders_both_sides_and_guide(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        out = store.get_context(tmp_repo)
        assert "SQLite won't handle concurrent sessions" in out       # standing
        assert "Unreviewed update" in out and "DynamoDB" in out       # labeled update
        assert "Do NOT settle which one holds by exploring the codebase" in out
        assert "resolve_conflict(" in out
        assert sum(1 for ln in out.splitlines() if ln.startswith("- ")) == 1
        # extras are indented continuations, never bullets (_rendered_meta counts bullets)
        assert not any(ln.strip().startswith("- ") and "Unreviewed update" in ln
                       for ln in out.splitlines())
        assert eid[:8] in out

    def test_render_prompt_decisions_renders_both_sides_and_guide(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        out = store._render_prompt_decisions(tmp_repo, [eid])
        assert "SQLite won't handle concurrent sessions" in out
        assert "Unreviewed update" in out and "DynamoDB" in out
        assert "resolve_conflict(" in out
        assert sum(1 for ln in out.splitlines() if ln.startswith("- ")) == 1

    def test_meta_count_unchanged_with_three_conflicts(self, tmp_repo):
        ids = [
            _conflicted(tmp_repo, "Use Postgres for the decision store", "Switch to DynamoDB"),
            _conflicted(tmp_repo, "Deploy through fly.io regions", "Deploy through render.com"),
            _conflicted(tmp_repo, "Cache sessions in redis", "Cache sessions in memcached"),
        ]
        out = store._render_prompt_decisions(tmp_repo, ids)
        assert store._rendered_meta("strong", out)["count"] == 3

    def test_session_start_project_rules_carry_both_sides(self, tmp_repo):
        # Deliberate pin: an `ai` replace_id proposal on an APPROVED constraint now renders,
        # labeled, inside the trusted project-rules block - the trust call made visible.
        eid = _conflicted(tmp_repo, "Never log secrets to stdout",
                          "Never log secrets or bearer tokens to stdout or stderr", "constraint")
        ctx = store.session_start_payload(tmp_repo)["context"]
        rules = ctx.split("## Project rules")[1]
        assert "Never log secrets to stdout" in rules
        assert "Unreviewed update" in rules and "bearer tokens" in rules
        assert "resolve_conflict(" in rules
        assert f"(id={eid[:8]})" in rules, "the guide tells the model to pass the id shown"

    def test_rehydrated_working_set_carries_both_sides(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        store._ws_add(tmp_repo, "sess-conflict", [eid])
        out = store._rehydrate_working_set(tmp_repo, "sess-conflict")
        assert "SQLite won't handle concurrent sessions" in out
        assert "Unreviewed update" in out and "DynamoDB" in out
        assert "resolve_conflict(" in out
        assert f"(id={eid[:8]})" in out, "the guide tells the model to pass the id shown"

    def test_scan_sourced_proposal_renders_flag_only(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        _poke_proposal(tmp_repo, eid, source="scan")
        out = store.get_context(tmp_repo)
        assert "[update pending approval]" in out       # today's flag-only tag stays
        assert "Unreviewed update" not in out and "DynamoDB" not in out
        assert "resolve_conflict(" not in out

    def test_clear_anchors_proposal_renders_flag_only(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        _poke_proposal(tmp_repo, eid, clear_anchors=True)
        out = store.get_context(tmp_repo)
        assert "[update pending approval]" in out
        assert "Unreviewed update" not in out and "DynamoDB" not in out
        assert "resolve_conflict(" not in out

    def test_title_only_proposal_renders_content_once_no_guide(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        current = store._current_content(_entry(tmp_repo, eid))
        _poke_proposal(tmp_repo, eid, content=current, title="Postgres over SQLite")
        out = store.get_context(tmp_repo)
        assert out.count("SQLite won't handle concurrent sessions") == 1
        assert "Unreviewed update" not in out
        assert "resolve_conflict(" not in out

    def test_guide_rendered_once_for_many_conflicts(self, tmp_repo):
        _conflicted(tmp_repo, "Use Postgres for the decision store", "Switch to DynamoDB")
        _conflicted(tmp_repo, "Deploy through fly.io regions", "Deploy through render.com")
        out = store.get_context(tmp_repo)
        assert out.count("Unreviewed update") == 2
        assert out.count("resolve_conflict(") == 1

    def test_legacy_proposal_without_title_renders_no_none(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        data = store._load(tmp_repo)
        next(e for e in data["entries"] if e.get("id") == eid)["proposed_revision"].pop("title")
        store._save(tmp_repo, data)
        store.record_conflict_memo(tmp_repo, eid, "update")
        out = store.get_context(tmp_repo)
        assert "None" not in out and "DynamoDB" in out


class TestConflictMemo:
    def test_update_choice_makes_proposal_operative_with_demoted_standing(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        ok, msg = store.record_conflict_memo(tmp_repo, eid, "update")
        assert ok and "NOT an approval" in msg and "outranks" in msg
        out = store.get_context(tmp_repo)
        bullet = next(ln for ln in out.splitlines() if ln.startswith("- "))
        assert "DynamoDB" in bullet                       # proposal title heads the bullet
        assert "Postgres for the decision store" not in bullet   # standing title is NOT on it
        assert "this update was picked with the developer on" in out
        assert "in-session" not in out
        assert 'Still the approved version on record (superseded by the pick above): "' in out
        assert "SQLite won't handle concurrent sessions" in out   # demoted, but present

    def test_standing_choice_hides_the_update(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        assert store.record_conflict_memo(tmp_repo, eid, "standing")[0]
        out = store.get_context(tmp_repo)
        assert "SQLite won't handle concurrent sessions" in out
        assert "was declined with the developer on" in out
        assert "DynamoDB" not in out
        assert "in-session" not in out

    def test_new_proposal_content_invalidates_memo(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        store.record_conflict_memo(tmp_repo, eid, "update")
        _poke_proposal(tmp_repo, eid, content="Switch to Cassandra for the decision store")
        out = store.get_context(tmp_repo)
        assert "Unreviewed update" in out and "Cassandra" in out   # dual render re-fires
        assert "picked with the developer" not in out

    def test_identical_reproposal_after_non_revision_pop_does_not_revive_memo(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        store.record_conflict_memo(tmp_repo, eid, "update")
        data = store._load(tmp_repo)
        # A proposal death that does NOT advance the revision (verify_scan_conventions' shape).
        old = next(e for e in data["entries"] if e.get("id") == eid).pop("proposed_revision")
        store._save(tmp_repo, data)
        data = store._load(tmp_repo)
        next(e for e in data["entries"] if e.get("id") == eid)["proposed_revision"] = dict(
            old, created_at=datetime.now(timezone.utc).isoformat())
        store._save(tmp_repo, data)
        out = store.get_context(tmp_repo)
        assert "Unreviewed update" in out
        assert "picked with the developer" not in out

    def test_promote_pops_memo(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        store.record_conflict_memo(tmp_repo, eid, "update")
        assert store.approve_decision(tmp_repo, eid, "approve")[0]
        assert "conflict_memo" not in _entry(tmp_repo, eid)

    def test_dismiss_pops_memo(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        store.record_conflict_memo(tmp_repo, eid, "standing")
        assert store.approve_decision(tmp_repo, eid, "dismiss")[0]
        assert "conflict_memo" not in _entry(tmp_repo, eid)

    def test_orphan_memo_renders_identically_to_plain(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        store.record_conflict_memo(tmp_repo, eid, "update")
        data = store._load(tmp_repo)
        next(e for e in data["entries"] if e.get("id") == eid).pop("proposed_revision")
        store._save(tmp_repo, data)
        with_memo = store.get_context(tmp_repo)
        data = store._load(tmp_repo)
        next(e for e in data["entries"] if e.get("id") == eid).pop("conflict_memo")
        store._save(tmp_repo, data)
        assert with_memo == store.get_context(tmp_repo)

    def test_record_holds_the_store_lock(self, tmp_repo, monkeypatch):
        eid = _conflicted(tmp_repo)
        real_load = store._load

        def slow_load(repo_path):
            data = real_load(repo_path)
            time.sleep(0.15)          # widen the load→save window a lockless writer would lose
            return data

        monkeypatch.setattr(store, "_load", slow_load)
        t = threading.Thread(target=store.record_conflict_memo, args=(tmp_repo, eid, "update"))
        t.start()
        time.sleep(0.03)
        store.update_decision(tmp_repo, "Rate limiting caps external calls at 100 rps",
                              "s3", "constraint")
        t.join()
        entries = real_load(tmp_repo)["entries"]
        assert next(e for e in entries if e.get("id") == eid).get("conflict_memo")
        assert any("Rate limiting caps" in e.get("content", "") for e in entries)

    def test_record_requires_full_length_id(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        ok, msg = store.record_conflict_memo(tmp_repo, eid[:6], "update")
        assert not ok and "at least 8 characters" in msg

    def test_record_normalizes_choice_and_rejects_bad_ones(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        assert store.record_conflict_memo(tmp_repo, eid, "  Update ")[0]
        assert _entry(tmp_repo, eid)["conflict_memo"]["choice"] == "update"
        ok, msg = store.record_conflict_memo(tmp_repo, eid, "approve")
        assert not ok and "Use 'standing' or 'update'" in msg

    def test_record_rejects_scan_sourced_proposal(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        _poke_proposal(tmp_repo, eid, source="scan")
        ok, msg = store.record_conflict_memo(tmp_repo, eid, "update")
        assert not ok and "bookkeeping or title-only" in msg

    def test_errors_are_distinguishable(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        messages = {
            store.record_conflict_memo(tmp_repo, eid, "nonsense")[1],
            store.record_conflict_memo(tmp_repo, "0" * 12, "update")[1],
            store.record_conflict_memo(tmp_repo, eid[:4], "update")[1],
        }
        store.approve_decision(tmp_repo, eid, "dismiss")
        messages.add(store.record_conflict_memo(tmp_repo, eid, "update")[1])
        assert len(messages) == 4

    def test_memo_grants_no_trust(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        before = _entry(tmp_repo, eid)
        snapshot = {k: before.get(k) for k in
                    ("status", "approved_by", "source_files", "anchor_commit")}
        assert store.record_conflict_memo(tmp_repo, eid, "update")[0]
        after = _entry(tmp_repo, eid)
        assert {k: after.get(k) for k in snapshot} == snapshot


class TestConflictRetrieval:
    def test_query_matching_only_the_proposal_finds_the_entry(self, tmp_repo):
        _conflicted(tmp_repo)
        out = store.get_context(tmp_repo, query="dynamodb")
        assert "No matching" not in out
        assert "Unreviewed update" in out and "DynamoDB" in out

    def test_bm25_route_reaches_dual_render_on_update_terms(self, tmp_repo):
        _conflicted(tmp_repo)
        out = store.get_context_for_prompt(tmp_repo, "why are we using dynamodb here?")
        assert "DynamoDB" in out and "Unreviewed update" in out

    def test_query_on_a_non_rendering_proposal_does_not_match(self, tmp_repo):
        # A scan/bookkeeping proposal never renders, so matching its terms would return a
        # decision showing none of the words asked for.
        eid = _conflicted(tmp_repo)
        _poke_proposal(tmp_repo, eid, source="scan")
        out = store.get_context(tmp_repo, query="dynamodb")
        assert "No matching" in out and "DynamoDB" not in out

    def test_bm25_does_not_rank_a_non_rendering_proposal(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        _poke_proposal(tmp_repo, eid, source="scan")   # _save rebuilds the retrieval index
        out = store.get_context_for_prompt(tmp_repo, "why are we using dynamodb here?")
        # The row used to rank on the hidden proposal's terms and inject its STANDING content -
        # an answer about Postgres to a question about DynamoDB.
        assert "SQLite" not in out and "DynamoDB" not in out


class TestConflictReviewLines:
    """The memo lines format_pending_review renders under a Suggested Update."""

    def test_format_pending_review_shows_update_choice_memo_line(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        store.record_conflict_memo(tmp_repo, eid, "update")
        out = store.format_pending_review(tmp_repo)
        assert "the update was picked with the developer on" in out
        assert "approve to formalize (dismiss drops it)" in out

    def test_format_pending_review_shows_standing_choice_memo_line(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        store.record_conflict_memo(tmp_repo, eid, "standing")
        out = store.format_pending_review(tmp_repo)
        assert "the update was declined with the developer on" in out
        assert "dismiss to formalize (approve applies it instead)" in out

    def test_format_pending_review_stale_memo_pair_shows_neither(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        store.record_conflict_memo(tmp_repo, eid, "update")
        _poke_proposal(tmp_repo, eid, content="Switch to Cassandra for the decision store")
        out = store.format_pending_review(tmp_repo)
        assert "picked with the developer" not in out
        assert "declined with the developer" not in out

    def test_format_pending_review_no_memo_shows_neither(self, tmp_repo):
        _conflicted(tmp_repo)
        out = store.format_pending_review(tmp_repo)
        assert "picked with the developer" not in out
        assert "declined with the developer" not in out


class TestConflictProposalDisplaced:
    """Issue #200: a developer restatement displaces a lower-trust proposal. The dual render
    must follow the NEW proposal, and the memo bound to the replaced one must be gone."""

    _STANDING = "Always commit automatically"
    _AI_UPDATE = "Always commit automatically once the CI pipeline is green"
    _HUMAN = "Always commit automatically after approvals and ensure you double cfonirm"

    def test_dual_render_follows_the_replacing_proposal(self, tmp_repo):
        eid = _conflicted(tmp_repo, self._STANDING, self._AI_UPDATE, subtype="constraint")
        assert store.record_conflict_memo(tmp_repo, eid, "update", "s2")[0]
        assert "CI pipeline" in store.get_context(tmp_repo)      # memo steers to the update

        eid2, _content, status = store.capture_user_constraint(tmp_repo, self._HUMAN, "s3")
        assert (eid2, status) == (eid, "revision_proposed")
        out = store.get_context(tmp_repo)
        assert "Unreviewed update" in out and "cfonirm" in out
        assert "CI pipeline" not in out, "the displaced proposal is archived, not rendered"
        assert "picked with the developer" not in out, "the memo's referent is gone"


class TestProposalSlotAtReplaceId:
    """The same trust-ordered slot (#200) at update_decision's replace_id write sites: an ai
    correction there used to clobber whatever Suggested Update was already awaiting review."""

    def test_ai_correction_keeps_a_human_proposal(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        _poke_proposal(tmp_repo, eid, source="human")
        before = dict(_entry(tmp_repo, eid)["proposed_revision"])
        ok, rid = store.update_decision(tmp_repo, "Switch to Cassandra for the decision store",
                                        "s3", "architecture", replace_id=eid)
        assert (ok, rid) == (True, eid), "still returns success, so the pending prompt shows"
        entry = _entry(tmp_repo, eid)
        assert entry["proposed_revision"] == before, "a human proposal is never auto-replaced"
        assert "superseded_proposals" not in entry, "nothing was displaced, nothing archived"

    def test_ai_retitle_keeps_a_human_proposal(self, tmp_repo):
        # The title-only gated branch writes the same slot and needs the same guard.
        eid = _conflicted(tmp_repo)
        _poke_proposal(tmp_repo, eid, source="human")
        before = dict(_entry(tmp_repo, eid)["proposed_revision"])
        ok, rid = store.update_decision(tmp_repo, store._current_content(_entry(tmp_repo, eid)),
                                        "s3", "architecture", replace_id=eid,
                                        title="Postgres over SQLite")
        assert (ok, rid) == (True, eid)
        entry = _entry(tmp_repo, eid)
        assert entry["proposed_revision"] == before
        assert "superseded_proposals" not in entry

    def test_allowed_overwrite_archives_the_displaced_proposal_and_pops_the_memo(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        assert store.record_conflict_memo(tmp_repo, eid, "update")[0]
        displaced = dict(_entry(tmp_repo, eid)["proposed_revision"])
        assert store.update_decision(tmp_repo, "Switch to Cassandra for the decision store",
                                     "s3", "architecture", replace_id=eid) == (True, eid)
        entry = _entry(tmp_repo, eid)
        assert "Cassandra" in entry["proposed_revision"]["content"]
        archived = entry["superseded_proposals"]
        assert [p["content"] for p in archived] == [displaced["content"]]
        assert "superseded_at" in archived[0]
        assert "conflict_memo" not in entry, "it referenced the proposal just replaced"


class TestRefusedCorrectionAck:
    """Issue #202: a refused claim returns success, so the refusal has to be reported in band
    (`meta['refusal_ack']`) or the model is told its correction is pending when it was dropped."""

    def test_content_branch_refusal_acks(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        _poke_proposal(tmp_repo, eid, source="human")
        before = dict(_entry(tmp_repo, eid)["proposed_revision"])
        stored, rid, meta = store.update_decision_with_meta(
            tmp_repo, "Switch to Cassandra for the decision store", "s3", "architecture",
            replace_id=eid)
        assert (stored, rid) == (True, eid)
        ack = meta["refusal_ack"]
        assert eid[:8] in ack and "NOT stored" in ack
        assert "human" in ack and before["title"] in ack
        assert _entry(tmp_repo, eid)["proposed_revision"] == before

    def test_title_only_branch_refusal_acks(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        _poke_proposal(tmp_repo, eid, source="human")
        before = dict(_entry(tmp_repo, eid)["proposed_revision"])
        stored, rid, meta = store.update_decision_with_meta(
            tmp_repo, store._current_content(_entry(tmp_repo, eid)), "s3", "architecture",
            replace_id=eid, title="Postgres over SQLite")
        assert (stored, rid) == (True, eid)
        assert eid[:8] in meta["refusal_ack"] and "NOT stored" in meta["refusal_ack"]
        assert _entry(tmp_repo, eid)["proposed_revision"] == before

    def test_tie_claim_acks_nothing(self, tmp_repo):
        eid = _conflicted(tmp_repo)                     # the sitting proposal is ai-sourced
        stored, rid, meta = store.update_decision_with_meta(
            tmp_repo, "Switch to Cassandra for the decision store", "s3", "architecture",
            replace_id=eid)
        assert (stored, rid, meta) == (True, eid, {})

    def test_empty_slot_acks_nothing(self, tmp_repo):
        store.update_decision(tmp_repo, CONFLICT_STANDING, "s1", "architecture")
        data = store._load(tmp_repo)
        entry = data["entries"][0]
        entry["status"] = "approved"
        store._save(tmp_repo, data)
        stored, rid, meta = store.update_decision_with_meta(
            tmp_repo, CONFLICT_UPDATE, "s2", "architecture", replace_id=entry["id"])
        assert (stored, rid, meta) == (True, entry["id"], {})

    def test_wrapper_still_returns_two_elements_on_the_refusal_path(self, tmp_repo):
        eid = _conflicted(tmp_repo)
        _poke_proposal(tmp_repo, eid, source="human")
        assert store.update_decision(tmp_repo, "Switch to Cassandra for the decision store",
                                     "s3", "architecture", replace_id=eid) == (True, eid)
