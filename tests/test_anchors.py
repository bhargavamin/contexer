"""Tests for contexer/anchors.py: review-gated anchor decay (issue #174 Task 2).

Uses a real throwaway git repo (borrowed pattern: test_guard_engine.py's `git_repo` /
`repo` fixtures) since rename detection genuinely shells out to git. `store` is imported
directly for the entry-construction/approval helpers the tests need (_new_decision_entry,
approve_decision, _load/_save, ...)."""
import os
import subprocess
import sys

import pytest

from contexer import anchors, lifecycle, review, revisions, store
from tests.seams import redirect_store_dir


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """A real throwaway git repo, isolated from the developer's global/system git config
    (mirrors test_guard_engine.py's git_repo fixture)."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "anchors@test.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Anchors Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    return repo


@pytest.fixture
def repo(git_repo, monkeypatch):
    """`git_repo` with STORE_DIR redirected to a sibling temp dir (mirrors
    test_guard_engine.py's repo fixture)."""
    redirect_store_dir(monkeypatch, git_repo.parent / ".contexer")
    return git_repo


def _write(repo, relpath, content="content\n"):
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _git(repo, *args, check=True):
    subprocess.run(["git", "-C", str(repo), *args], check=check, capture_output=True)


def _commit(repo, message="init"):
    _git(repo, "commit", "-q", "-m", message)


def _seed_entry(repo, content, *, subtype="architecture", created_by="human",
                 status="approved", source_files=None, title="", session_id="test-session"):
    """Build a decision entry via the real entry constructor (revisions/status/
    current_revision_id all shaped exactly like production data), append it to the repo
    store, and stamp source_files/anchor_commit directly — bypassing _anchor_sources
    since tests want to control the exact anchor list without depending on capture flow."""
    entry = store._new_decision_entry(content, session_id, subtype,
                                       created_by=created_by, status=status, title=title)
    if source_files is not None:
        entry["source_files"] = source_files
        entry["anchor_commit"] = store.run_git(str(repo), "rev-parse", "HEAD") or ""
    data = store.load(str(repo))
    data["entries"].append(entry)
    store.save(str(repo), data)
    return entry


def _reload(repo):
    return store.load(str(repo))["entries"][-1]


# ── _parse_rename_target (pure) ──────────────────────────────────────────────

class TestParseRenameTarget:
    def test_single_rename_line_confident(self):
        out = "R100\told.py\tnew.py"
        assert anchors._parse_rename_target(out, "old.py") == "new.py"

    def test_no_rename_line_returns_none(self):
        assert anchors._parse_rename_target("M\told.py", "old.py") is None
        assert anchors._parse_rename_target("", "old.py") is None

    def test_two_distinct_targets_ambiguous(self):
        out = "R100\told.py\tnew_a.py\nR090\told.py\tnew_b.py"
        assert anchors._parse_rename_target(out, "old.py") is None

    def test_same_target_repeated_is_confident(self):
        # Not actually ambiguous — the same single target reported twice collapses to one.
        out = "R100\told.py\tnew.py\nR100\told.py\tnew.py"
        assert anchors._parse_rename_target(out, "old.py") == "new.py"


# ── fast path / TTL ───────────────────────────────────────────────────────────

class TestFastPathAndTTL:
    def test_no_anchored_entries_zero_git_calls(self, repo, monkeypatch):
        _seed_entry(repo, "Use uv for everything.", source_files=None)
        calls = []
        monkeypatch.setattr(anchors, "_run_git", lambda *a, **k: calls.append(a) or None)
        result = anchors.verify_anchors(str(repo), force=True)
        assert result == {"reanchored": 0, "proposed": 0}
        assert calls == []
        # Fast path exits before even the TTL stamp is written.
        assert not anchors._anchor_verify_stamp_path(str(repo)).exists()

    def test_ttl_gate_skips_second_call(self, repo, monkeypatch):
        _write(repo, "a.py")
        _git(repo, "add", "a.py")
        _commit(repo)
        _seed_entry(repo, "Decision about a.py", source_files=["a.py"])

        calls = []
        monkeypatch.setattr(anchors, "_run_git", lambda *a, **k: calls.append(a) or None)
        anchors.verify_anchors(str(repo))          # no force: stamps TTL, does its work
        first_call_count = len(calls)
        anchors.verify_anchors(str(repo))          # inside TTL: must no-op
        assert len(calls) == first_call_count

    def test_force_bypasses_ttl(self, repo, monkeypatch):
        _write(repo, "a.py")
        _git(repo, "add", "a.py")
        _commit(repo)
        _seed_entry(repo, "Decision about a.py", source_files=["a.py"])
        os.remove(str(repo / "a.py"))

        monkeypatch.setattr(anchors, "_run_git", lambda *a, **k: None)  # no rename found
        anchors.verify_anchors(str(repo), force=True)
        entry = _reload(repo)
        assert entry.get("proposed_lifecycle") is not None
        # Second forced call must still run (force bypasses TTL) even though a proposal
        # already exists — but the entry is now skipped as a participant (has a proposal).
        result = anchors.verify_anchors(str(repo), force=True)
        assert result == {"reanchored": 0, "proposed": 0}


# ── rename re-anchor ──────────────────────────────────────────────────────────

class TestRenameReanchor:
    def test_confident_rename_reanchors_in_place_no_proposal(self, repo):
        _write(repo, "old.py", "a\nb\nc\n")
        _git(repo, "add", "old.py")
        _commit(repo)
        entry = _seed_entry(repo, "Decision about old.py", source_files=["old.py"])
        old_anchor_commit = entry["anchor_commit"]

        _git(repo, "mv", "old.py", "new.py")
        _commit(repo, "rename")

        result = anchors.verify_anchors(str(repo), force=True)
        assert result == {"reanchored": 1, "proposed": 0}
        reloaded = _reload(repo)
        assert reloaded["source_files"] == ["new.py"]
        assert reloaded.get("proposed_revision") is None
        assert reloaded["anchor_commit"] != old_anchor_commit
        assert reloaded["anchor_commit"] == store.run_git(str(repo), "rev-parse", "HEAD")

    def test_multi_hop_rename_chain_reanchors_to_final_target(self, repo):
        # Greptile P1 repro: a -> b -> c across two commits. `old.py`'s single
        # most-recent-touch commit only ever resolves to `mid.py`, which no longer exists
        # either (it was renamed on again) — the fix must chase that hop too and land on
        # `new.py`, not fall through to "missing, no rename".
        _write(repo, "old.py", "a\nb\nc\n")
        _git(repo, "add", "old.py")
        _commit(repo)
        entry = _seed_entry(repo, "Decision about old.py", source_files=["old.py"])
        old_anchor_commit = entry["anchor_commit"]

        _git(repo, "mv", "old.py", "mid.py")
        _commit(repo, "rename hop 1")
        _git(repo, "mv", "mid.py", "new.py")
        _commit(repo, "rename hop 2")

        result = anchors.verify_anchors(str(repo), force=True)
        assert result == {"reanchored": 1, "proposed": 0}
        reloaded = _reload(repo)
        assert reloaded["source_files"] == ["new.py"]
        assert reloaded.get("proposed_revision") is None
        assert reloaded["anchor_commit"] != old_anchor_commit
        assert reloaded["anchor_commit"] == store.run_git(str(repo), "rev-parse", "HEAD")

    def test_rename_chain_exceeding_hop_cap_treated_as_missing(self, repo, monkeypatch):
        # A chain longer than _RENAME_CHAIN_MAX must NOT be chased indefinitely — it's
        # treated as a plain miss, same as no rename at all, and (since it's the only
        # anchored file) falls through to the total-loss proposal path.
        monkeypatch.setattr(anchors, "_RENAME_CHAIN_MAX", 2)
        _write(repo, "p0.py", "content\n")
        _git(repo, "add", "p0.py")
        _commit(repo)
        _seed_entry(repo, "Decision about p0.py", source_files=["p0.py"])

        # Three rename hops: p0 -> p1 -> p2 -> p3 — one more than the capped chain length.
        for i in range(3):
            _git(repo, "mv", f"p{i}.py", f"p{i + 1}.py")
            _commit(repo, f"rename hop {i}")

        result = anchors.verify_anchors(str(repo), force=True)
        assert result == {"reanchored": 0, "proposed": 1}
        reloaded = _reload(repo)
        assert reloaded["source_files"] == ["p0.py"]   # untouched — no rename applied
        prop = reloaded.get("proposed_lifecycle")
        assert prop is not None
        assert "p0.py no longer exist" in prop["reason"]

    def test_ambiguity_mid_chain_not_confident(self, repo, monkeypatch):
        # Hop 1 (old.py -> mid.py) is confident; hop 2 (mid.py -> ???) is ambiguous. The
        # whole chain must be rejected — not confident — rather than stopping at the
        # ambiguous hop and treating the last confident hop as good enough.
        _write(repo, "old.py", "a\n")
        _write(repo, "keep.py", "b\n")
        _git(repo, "add", "old.py", "keep.py")
        _commit(repo)
        _seed_entry(repo, "Decision about old.py and keep.py",
                    source_files=["old.py", "keep.py"])
        os.remove(str(repo / "old.py"))

        def fake_git(repo_path, *args):
            if args[0] == "log":
                target = args[-1]
                if target == "old.py":
                    return "commit1"
                if target == "mid.py":
                    return "commit2"
                return None
            if args[0] == "show":
                commit = args[-1]
                if commit == "commit1":
                    return "R100\told.py\tmid.py"
                if commit == "commit2":
                    return "R100\tmid.py\ta.py\nR090\tmid.py\tb.py"   # ambiguous
                return None
            if args[:2] == ("rev-parse", "HEAD"):
                return store.run_git(repo_path, *args)
            return None
        monkeypatch.setattr(anchors, "_run_git", fake_git)

        result = anchors.verify_anchors(str(repo), force=True)
        assert result == {"reanchored": 1, "proposed": 0}   # partial loss: keep.py survives
        reloaded = _reload(repo)
        assert reloaded["source_files"] == ["keep.py"]
        assert reloaded.get("proposed_revision") is None

    def test_budget_exhaustion_mid_chain_leaves_entry_unverified(self, repo, monkeypatch):
        # The budget can run out partway through a rename chain (after hop 1's log+show,
        # before hop 2's log). That must surface as _BudgetExceeded like any other
        # mid-entry exhaustion — the entry left completely untouched, not misclassified.
        _write(repo, "old.py", "a\n")
        _git(repo, "add", "old.py")
        _commit(repo)
        entry = _seed_entry(repo, "Decision about old.py", source_files=["old.py"])
        _git(repo, "mv", "old.py", "mid.py")
        _commit(repo, "rename hop 1")
        _git(repo, "mv", "mid.py", "new.py")
        _commit(repo, "rename hop 2")

        monkeypatch.setattr(anchors, "_ANCHOR_GIT_BUDGET", 2)  # exactly one hop's worth
        result = anchors.verify_anchors(str(repo), force=True)
        assert result == {"reanchored": 0, "proposed": 0}
        reloaded = _reload(repo)
        assert reloaded["source_files"] == ["old.py"]          # untouched
        assert reloaded["anchor_commit"] == entry["anchor_commit"]
        assert reloaded.get("proposed_revision") is None

    def test_ambiguous_rename_treated_as_missing(self, repo, monkeypatch):
        _write(repo, "old.py", "a\n")
        _write(repo, "keep.py", "b\n")
        _git(repo, "add", "old.py", "keep.py")
        _commit(repo)
        _seed_entry(repo, "Decision about old.py and keep.py",
                    source_files=["old.py", "keep.py"])
        os.remove(str(repo / "old.py"))

        # Force ambiguity regardless of real git history: two distinct targets.
        def fake_git(repo_path, *args):
            if args[0] == "log":
                return "deadbeef"
            if args[0] == "show":
                return "R100\told.py\ta.py\nR090\told.py\tb.py"
            if args[:2] == ("rev-parse", "HEAD"):
                return store.run_git(repo_path, *args)
            return None
        monkeypatch.setattr(anchors, "_run_git", fake_git)

        result = anchors.verify_anchors(str(repo), force=True)
        assert result == {"reanchored": 1, "proposed": 0}   # partial loss: keep.py survives
        reloaded = _reload(repo)
        assert reloaded["source_files"] == ["keep.py"]
        assert reloaded.get("proposed_revision") is None


# ── partial loss ──────────────────────────────────────────────────────────────

class TestPartialLoss:
    def test_some_files_missing_refreshes_list_no_proposal(self, repo, monkeypatch):
        _write(repo, "a.py")
        _write(repo, "b.py")
        _git(repo, "add", "a.py", "b.py")
        _commit(repo)
        _seed_entry(repo, "Decision about a.py and b.py", source_files=["a.py", "b.py"])
        os.remove(str(repo / "b.py"))

        monkeypatch.setattr(anchors, "_run_git", lambda *a, **k: None)  # no rename found
        result = anchors.verify_anchors(str(repo), force=True)
        assert result == {"reanchored": 1, "proposed": 0}
        reloaded = _reload(repo)
        assert reloaded["source_files"] == ["a.py"]
        assert reloaded.get("proposed_revision") is None

    def test_all_files_present_is_a_true_noop(self, repo, monkeypatch):
        _write(repo, "a.py")
        _git(repo, "add", "a.py")
        _commit(repo)
        entry = _seed_entry(repo, "Decision about a.py", source_files=["a.py"])
        calls = []
        monkeypatch.setattr(anchors, "_run_git", lambda *a, **k: calls.append(a) or None)

        result = anchors.verify_anchors(str(repo), force=True)
        assert result == {"reanchored": 0, "proposed": 0}
        assert calls == []   # no missing file -> no git call at all for this entry
        reloaded = _reload(repo)
        assert reloaded["source_files"] == ["a.py"]
        assert reloaded["anchor_commit"] == entry["anchor_commit"]


# ── total loss ──────────────────────────────────────────────────────────────

class TestTotalLoss:
    def test_all_files_gone_proposes_retirement_and_arms_nudge(self, repo, monkeypatch):
        _write(repo, "gone.py")
        _git(repo, "add", "gone.py")
        _commit(repo)
        content = "Use gone.py to configure the thing."
        entry = _seed_entry(repo, content, source_files=["gone.py"])
        os.remove(str(repo / "gone.py"))

        monkeypatch.setattr(anchors, "_run_git", lambda *a, **k: None)  # no rename found
        result = anchors.verify_anchors(str(repo), force=True)
        assert result == {"reanchored": 0, "proposed": 1}

        reloaded = _reload(repo)
        prop = reloaded.get("proposed_lifecycle")
        assert prop is not None
        assert prop["action"] == "retire"
        assert prop["source"] == "scan"
        assert prop["reason"] == "anchors withdrawn on re-verification: gone.py no longer exist"
        assert prop["basis_revision_id"] == reloaded["current_revision_id"]
        # A retirement is a state transition, never a rewording: the decision's own content
        # and revision history are untouched, and the content lane stays empty.
        assert reloaded["content"] == content
        assert len(reloaded["revisions"]) == 1
        assert reloaded.get("proposed_revision") is None
        assert reloaded["status"] == "approved"       # still live until a human retires it
        assert store._pending_review_flag(str(repo)).exists()   # nudge armed
        # And it is genuinely awaiting review, not merely stamped on the entry.
        assert [e["id"] for e in store.get_pending_decisions(str(repo))] == [entry["id"]]

    def test_retiring_the_proposal_tombstones_the_decision(self, repo, monkeypatch):
        _write(repo, "gone.py")
        _git(repo, "add", "gone.py")
        _commit(repo)
        entry = _seed_entry(repo, "Use gone.py to configure the thing.",
                            source_files=["gone.py"])
        os.remove(str(repo / "gone.py"))
        monkeypatch.setattr(anchors, "_run_git", lambda *a, **k: None)
        anchors.verify_anchors(str(repo), force=True)

        ok, _msg = lifecycle.retire_decision(str(repo), entry["id"],
                                          "the file it describes is gone")
        assert ok
        assert store.load(str(repo))["entries"] == []
        (tomb,) = store.list_deleted(str(repo))
        assert [r["kind"] for r in tomb["lifecycle"]] == ["retired"]
        # A second forced run has nothing left to participate.
        assert anchors.verify_anchors(str(repo), force=True) == {"reanchored": 0, "proposed": 0}

    def test_dismissing_preserves_entry_and_reproposes_next_cycle(self, repo, monkeypatch):
        _write(repo, "gone.py")
        _git(repo, "add", "gone.py")
        _commit(repo)
        content = "Use gone.py to configure the thing."
        entry = _seed_entry(repo, content, source_files=["gone.py"])
        os.remove(str(repo / "gone.py"))
        monkeypatch.setattr(anchors, "_run_git", lambda *a, **k: None)
        anchors.verify_anchors(str(repo), force=True)

        ok, _msg = lifecycle.dismiss_lifecycle(str(repo), entry["id"])
        assert ok
        reloaded = _reload(repo)
        assert reloaded.get("proposed_lifecycle") is None
        assert reloaded["content"] == content          # unchanged
        assert len(reloaded["revisions"]) == 1          # no new revision appended
        assert reloaded["source_files"] == ["gone.py"]  # dismiss leaves anchors as-is

        # Dismiss means "not now": the entry re-qualifies and the next cycle asks again.
        result = anchors.verify_anchors(str(repo), force=True)
        assert result == {"reanchored": 0, "proposed": 1}
        assert _reload(repo)["proposed_lifecycle"]["action"] == "retire"

    def test_a_developers_own_retirement_proposal_is_never_overwritten(self, repo, monkeypatch):
        # The entry-level participant filter already skips an entry carrying a proposal, so
        # this pins the slot rule itself: even reached directly, a scan proposal must not
        # displace a human one.
        _write(repo, "gone.py")
        _git(repo, "add", "gone.py")
        _commit(repo)
        entry = _seed_entry(repo, "Use gone.py.", source_files=["gone.py"])
        assert lifecycle.propose_lifecycle(str(repo), entry["id"], "retire", "I want this gone",
                                       source="human")["ok"]
        os.remove(str(repo / "gone.py"))

        calls = []
        monkeypatch.setattr(anchors, "_run_git", lambda *a, **k: calls.append(a) or None)
        assert anchors.verify_anchors(str(repo), force=True) == {"reanchored": 0, "proposed": 0}
        assert calls == []
        assert _reload(repo)["proposed_lifecycle"]["reason"] == "I want this gone"

    def test_dedupe_guard_skips_when_content_already_carries_clause(self, repo, monkeypatch):
        # Defensive backstop (belt-and-suspenders alongside the approval-clears-anchors
        # fix): an entry whose CURRENT content already carries a withdrawal clause but
        # whose source_files/anchor_commit were somehow left in place (legacy data, a
        # race, or the pre-fix bug) must never get a second clause stacked onto it.
        _write(repo, "gone.py")
        _git(repo, "add", "gone.py")
        _commit(repo)
        content = ("Use gone.py to configure the thing. (anchors withdrawn on "
                   "re-verification: gone.py no longer exist)")
        _seed_entry(repo, content, source_files=["gone.py"])
        os.remove(str(repo / "gone.py"))

        calls = []
        monkeypatch.setattr(anchors, "_run_git", lambda *a, **k: calls.append(a) or None)
        result = anchors.verify_anchors(str(repo), force=True)
        assert result == {"reanchored": 0, "proposed": 0}
        reloaded = _reload(repo)
        assert reloaded.get("proposed_revision") is None
        assert reloaded.get("proposed_lifecycle") is None
        assert reloaded["content"] == revisions.normalize_content(content)
        assert reloaded["content"].count("anchors withdrawn on re-verification") == 1

    def test_existing_proposal_entry_is_skipped(self, repo, monkeypatch):
        _write(repo, "gone.py")
        _git(repo, "add", "gone.py")
        _commit(repo)
        entry = _seed_entry(repo, "Use gone.py.", source_files=["gone.py"])
        os.remove(str(repo / "gone.py"))
        data = store.load(str(repo))
        target = next(e for e in data["entries"] if e["id"] == entry["id"])
        target["proposed_revision"] = review.build_proposal(
            target, "some unrelated suggested edit", "", "", "2026-01-01T00:00:00+00:00")
        store.save(str(repo), data)

        calls = []
        monkeypatch.setattr(anchors, "_run_git", lambda *a, **k: calls.append(a) or None)
        result = anchors.verify_anchors(str(repo), force=True)
        assert result == {"reanchored": 0, "proposed": 0}
        assert calls == []
        reloaded = _reload(repo)
        assert reloaded["proposed_revision"]["content"] == "Some unrelated suggested edit"

    def test_pending_and_ignored_entries_never_touched(self, repo, monkeypatch):
        _write(repo, "gone.py")
        _git(repo, "add", "gone.py")
        _commit(repo)
        pending = _seed_entry(repo, "Pending decision about gone.py",
                              source_files=["gone.py"], status="pending_approval")
        ignored = _seed_entry(repo, "Ignored decision about gone.py",
                              source_files=["gone.py"], status="ignored")
        os.remove(str(repo / "gone.py"))

        monkeypatch.setattr(anchors, "_run_git", lambda *a, **k: None)
        result = anchors.verify_anchors(str(repo), force=True)
        assert result == {"reanchored": 0, "proposed": 0}
        data = store.load(str(repo))
        by_id = {e["id"]: e for e in data["entries"]}
        assert by_id[pending["id"]].get("proposed_revision") is None
        assert by_id[ignored["id"]].get("proposed_revision") is None
        assert by_id[pending["id"]].get("proposed_lifecycle") is None
        assert by_id[ignored["id"]].get("proposed_lifecycle") is None
        assert by_id[pending["id"]]["source_files"] == ["gone.py"]
        assert by_id[ignored["id"]]["source_files"] == ["gone.py"]


# ── legacy clear_anchors proposals (pre-lifecycle-lane stores) ────────────────

class TestLegacyClearAnchorsProposal:
    """anchors.py no longer CREATES a `clear_anchors` proposed_revision - anchor-loss
    withdrawal moved to the `proposed_lifecycle` lane. A store written before that move can
    still hold one pending, so the promote path stays working: a proposal a developer can
    neither approve nor understand would be worse than the branch it costs."""

    def _legacy_proposal(self, repo, entry, content, *, title=""):
        data = store.load(str(repo))
        target = next(e for e in data["entries"] if e["id"] == entry["id"])
        proposal = review.build_proposal(
            target, f"{content} (anchors withdrawn on re-verification: gone.py no longer "
                    "exist)", "", "", "2026-01-01T00:00:00+00:00", source="scan",
            title=title or target.get("title") or "")
        proposal["clear_anchors"] = True
        target["proposed_revision"] = proposal
        store.save(str(repo), data)

    def test_approving_still_clears_anchors_and_exits_participation(self, repo):
        _write(repo, "gone.py")
        _git(repo, "add", "gone.py")
        _commit(repo)
        content = "Use gone.py to configure the thing."
        entry = _seed_entry(repo, content, source_files=["gone.py"])
        self._legacy_proposal(repo, entry, content)
        os.remove(str(repo / "gone.py"))

        store.approve_decision(str(repo), entry["id"], "approve")
        reloaded = _reload(repo)
        assert reloaded.get("proposed_revision") is None
        assert reloaded["content"].startswith(content)
        assert len(reloaded["revisions"]) == 2
        assert "source_files" not in reloaded          # dropped, not left pointing at gone.py
        assert "anchor_commit" not in reloaded
        # And the entry is out of the participant set, so nothing re-proposes on top of it.
        assert anchors.verify_anchors(str(repo), force=True) == {"reanchored": 0, "proposed": 0}

    def test_approving_pops_stale_anchor_candidates(self, repo):
        # `anchor_candidates` legitimately SURVIVE on an already-approved entry, so a
        # retirement approval can meet a stale guess - and the candidate-blessing branch
        # used to read the freshly-emptied source_files as "nothing anchors this entry" and
        # promote that guess into a REAL anchor.
        _write(repo, "gone.py")
        _write(repo, "unrelated.py")
        _git(repo, "add", "gone.py", "unrelated.py")
        _commit(repo)
        content = "Use gone.py to configure the thing."
        entry = _seed_entry(repo, content, source_files=["gone.py"])
        self._legacy_proposal(repo, entry, content)
        data = store.load(str(repo))
        next(e for e in data["entries"] if e["id"] == entry["id"])["anchor_candidates"] = \
            ["unrelated.py"]
        store.save(str(repo), data)
        os.remove(str(repo / "gone.py"))

        store.approve_decision(str(repo), entry["id"], "approve")
        reloaded = _reload(repo)
        assert "source_files" not in reloaded
        assert "anchor_commit" not in reloaded
        assert "anchor_candidates" not in reloaded

    def test_approving_preserves_a_curated_title(self, repo):
        curated = "Thing configuration lives in gone.py"
        _write(repo, "gone.py")
        _git(repo, "add", "gone.py")
        _commit(repo)
        content = "Use gone.py to configure the thing."
        entry = _seed_entry(repo, content, source_files=["gone.py"], title=curated)
        self._legacy_proposal(repo, entry, content)

        store.approve_decision(str(repo), entry["id"], "approve")
        reloaded = _reload(repo)
        assert reloaded["title"] == curated
        assert revisions.current_revision(reloaded)["title"] == curated


# ── budget ────────────────────────────────────────────────────────────────────

class TestBudget:
    def test_budget_exhaustion_leaves_remaining_entries_unverified(self, repo, monkeypatch):
        # Reviewer's exact repro: 6 entries, each with a REAL git-mv rename (no mocking of
        # git at all — this exercises the true 3-call-per-reanchor cost: log + show +
        # rev-parse). The budget is pinned to 10 HERE rather than read from the module, so
        # this test keeps exercising the exhaustion semantics regardless of what the real
        # constant is set to: 10 covers exactly 3 full re-anchors (9 calls); the 4th
        # entry's rename check spends the 10th call on `log`, then hits exhaustion on
        # `show` and must be left completely untouched — not misclassified as a total
        # loss (the old return-None-on-exhaustion bug) and not partially applied.
        monkeypatch.setattr(anchors, "_ANCHOR_GIT_BUDGET", 10)
        names = [f"old_{i}.py" for i in range(6)]
        for name in names:
            _write(repo, name, f"content {name}\n")
        _git(repo, "add", *names)
        _commit(repo)
        entries = [_seed_entry(repo, f"Decision about old_{i}.py", source_files=[names[i]])
                   for i in range(6)]
        for i, name in enumerate(names):
            _git(repo, "mv", name, f"new_{i}.py")
        _commit(repo, "rename all")

        real_run_git = anchors._run_git
        calls = []

        def counting(repo_path, *args):
            calls.append(args)
            return real_run_git(repo_path, *args)
        monkeypatch.setattr(anchors, "_run_git", counting)

        result = anchors.verify_anchors(str(repo), force=True)   # must not raise
        assert result == {"reanchored": 3, "proposed": 0}
        assert len(calls) <= anchors._ANCHOR_GIT_BUDGET

        data = store.load(str(repo))
        by_id = {e["id"]: e for e in data["entries"]}
        for i in range(3):
            assert by_id[entries[i]["id"]]["source_files"] == [f"new_{i}.py"]
        for i in range(3, 6):
            reloaded = by_id[entries[i]["id"]]
            assert reloaded["source_files"] == [names[i]]      # completely untouched
            assert reloaded.get("proposed_revision") is None

    def test_budget_covers_one_worst_case_entry_so_the_run_progresses(self, repo, monkeypatch):
        # Starvation repro: a single fat entry must never be able to exhaust the budget
        # INSIDE its own file loop — that entry, and every entry after it, would then be
        # skipped on every single run, forever, silently. 8 missing committed files cost
        # 16 git calls (log + show each), well past the old budget of 10; the budget now
        # covers the worst case an entry can present (2 * _MAX_SOURCE_FILES + 1), so this
        # one forced run completes BOTH entries.
        fat = [f"fat_{i}.py" for i in range(8)]
        for name in (*fat, "lean.py"):
            _write(repo, name, f"content {name}\n")
        _git(repo, "add", *fat, "lean.py")
        _commit(repo)
        fat_entry = _seed_entry(repo, "Decision about the fat file set.", source_files=fat)
        lean_entry = _seed_entry(repo, "Decision about lean.py", source_files=["lean.py"])
        _git(repo, "rm", "-q", *fat, "lean.py")
        _commit(repo, "delete all")

        result = anchors.verify_anchors(str(repo), force=True)
        assert result == {"reanchored": 0, "proposed": 2}
        by_id = {e["id"]: e for e in store.load(str(repo))["entries"]}
        assert by_id[fat_entry["id"]].get("proposed_lifecycle") is not None
        assert by_id[lean_entry["id"]].get("proposed_lifecycle") is not None


# ── fail-soft ────────────────────────────────────────────────────────────────

class TestFailSoft:
    def test_corrupt_store_never_raises(self, repo, monkeypatch):
        def boom(repo_path):
            raise ValueError("corrupt store")
        monkeypatch.setattr(store, "load", boom)
        result = anchors.verify_anchors(str(repo), force=True)
        assert result == {"reanchored": 0, "proposed": 0}

    def test_crash_after_stamp_write_leaves_stamp_in_place(self, repo, monkeypatch):
        _write(repo, "a.py")
        _git(repo, "add", "a.py")
        _commit(repo)
        _seed_entry(repo, "Decision about a.py", source_files=["a.py"])
        os.remove(str(repo / "a.py"))

        def boom(repo_path, *args):
            raise RuntimeError("simulated crash mid-verification")
        monkeypatch.setattr(anchors, "_run_git", boom)

        result = anchors.verify_anchors(str(repo), force=True)
        assert result == {"reanchored": 0, "proposed": 0}   # fail-soft, no raise
        assert anchors._anchor_verify_stamp_path(str(repo)).exists()   # stamp written first

        # A subsequent un-forced call must be gated by the TTL stamp that survived the
        # crash — no retry storm.
        calls = []
        monkeypatch.setattr(anchors, "_run_git", lambda *a, **k: calls.append(a) or None)
        anchors.verify_anchors(str(repo))
        assert calls == []

    def test_non_repo_fails_soft(self, tmp_path, monkeypatch):
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()
        redirect_store_dir(monkeypatch, tmp_path / ".contexer")
        _seed_entry(not_a_repo, "Decision about a.py", source_files=["a.py"])
        result = anchors.verify_anchors(str(not_a_repo), force=True)
        assert isinstance(result, dict)


# ── session-start wiring ──────────────────────────────────────────────────────

class TestSessionStartWiring:
    def test_session_start_renders_reanchored_state(self, repo, monkeypatch):
        _write(repo, "old.py", "a\n")
        _git(repo, "add", "old.py")
        _commit(repo)
        _seed_entry(repo, "Decision about old.py", source_files=["old.py"],
                    subtype="constraint")
        _git(repo, "mv", "old.py", "new.py")
        _commit(repo, "rename")

        store.session_start_payload(str(repo))
        data = store.load(str(repo))
        entry = next(e for e in data["entries"] if e["subtype"] == "constraint")
        assert entry["source_files"] == ["new.py"]


# ── import order ─────────────────────────────────────────────────────────────

class TestImportOrderRegression:
    """anchors.py imports `store` at its own top level and store.py calls back into
    `anchors` from the session-start path — the same shape that once broke guard_engine
    (see test_guard_engine.py's TestImportOrderRegression). A fresh subprocess is the only
    honest check: pytest has already imported both modules in this process, in the safe
    order, so an in-process assertion would prove nothing. `import contexer.anchors` as the
    very first touch of the package is the order at risk."""

    def test_anchors_first_import_order_does_not_raise(self):
        probe = (
            "import contexer.anchors\n"
            "import contexer.store\n"
            "assert contexer.anchors.store is contexer.store\n"
            "print('OK')\n"
        )
        result = subprocess.run([sys.executable, "-c", probe],
                                 capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "OK"


class TestAnchorCountHasOneWriter:
    """`source_files` and `source_files_total` are one fact with one writer
    (`store.set_source_files` / `store.clear_source_files`). Before that, `_anchor_sources`
    maintained both while this module shrank the list directly, so a partial anchor loss left
    the truncation count behind and `contexer review` rendered survivors as a truncation that
    never happened."""

    def _anchored(self, repo, count=12):
        files = [f"src/mod{i}.py" for i in range(count)]
        for f in files:
            _write(repo, f, f"# {f}\n")
        _git(repo, "add", "-A")
        _commit(repo, "seed")
        entry = _seed_entry(repo, "Route all writes through the queue.")
        store._anchor_sources(str(repo), entry, files)
        data = store.load(str(repo))
        data["entries"][-1] = entry
        store.save(str(repo), data)
        return entry, files

    def test_partial_loss_drops_the_truncation_count(self, repo):
        entry, _ = self._anchored(repo)
        # Truncation is real here, so the count is right and must survive.
        assert len(entry["source_files"]) == store.MAX_SOURCE_FILES
        assert entry["source_files_total"] == 12

        for f in entry["source_files"][:7]:
            (repo / f).unlink()
        _git(repo, "add", "-A")
        _commit(repo, "drop seven")

        anchors.verify_anchors(str(repo), force=True)
        after = _reload(repo)
        assert len(after["source_files"]) == 3
        # The 3 are survivors. Nothing was truncated to reach them, so no count may remain:
        # cli._review_metadata renders "(first 3 of 12)" off exactly this key.
        assert "source_files_total" not in after

    def test_a_pure_rename_keeps_the_truncation_count(self, repo):
        # The mirror-image bug, caught in review: nothing is lost on a rename, so the
        # derivation count still describes the list and erasing it would destroy a REAL
        # "(first 10 of 12)" record on the first rename the repo ever sees.
        entry, _ = self._anchored(repo)
        old = entry["source_files"][0]
        _git(repo, "mv", old, old.replace("mod", "renamed"))
        _commit(repo, "rename one")

        anchors.verify_anchors(str(repo), force=True)
        after = _reload(repo)
        assert len(after["source_files"]) == store.MAX_SOURCE_FILES
        assert after["source_files_total"] == 12
        assert old not in after["source_files"]

    def test_the_share_preview_stops_claiming_a_truncation_too(self, repo):
        # The defect had TWO render surfaces and the first fix only pinned one. The share
        # projection recomputes its own total via max(wire_total, ...), so it needs its own
        # assertion: an edit there would otherwise regress silently with the suite green.
        entry, _ = self._anchored(repo)
        before = store._share_projection(entry)
        assert before.get("source_files_total") == 12          # real truncation, still told

        for f in entry["source_files"][:7]:
            (repo / f).unlink()
        _git(repo, "add", "-A")
        _commit(repo, "drop seven")
        anchors.verify_anchors(str(repo), force=True)

        after = store._share_projection(_reload(repo))
        assert len(after["source_files"]) == 3
        # <= len(files) is what the render guards on, so this is "say nothing", not "say 3 of 3".
        assert (after.get("source_files_total") or 0) <= len(after["source_files"])

    def test_retiring_an_anchor_leaves_no_orphan_count(self, repo):
        entry = _seed_entry(repo, "Guard the queue.", source_files=["a.py"])
        entry["source_files_total"] = 40
        entry["proposed_revision"] = {
            "content": "Guard the queue. (anchors withdrawn on re-verification: `a.py`)",
            "source": "scan", "clear_anchors": True, "created_at": "2026-01-01T00:00:00+00:00",
        }
        data = store.load(str(repo))
        data["entries"][-1] = entry
        store.save(str(repo), data)

        store._promote_proposal(str(repo), entry)
        assert "source_files" not in entry
        assert "source_files_total" not in entry
