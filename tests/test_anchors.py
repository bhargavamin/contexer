"""Tests for contexer/anchors.py: review-gated anchor decay (issue #174 Task 2).

Uses a real throwaway git repo (borrowed pattern: test_guard_engine.py's `git_repo` /
`repo` fixtures) since rename detection genuinely shells out to git. `store` is imported
directly for the entry-construction/approval helpers the tests need (_new_decision_entry,
approve_decision, _load/_save, ...)."""
import os
import subprocess

import pytest

from contexer import anchors, store


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
    monkeypatch.setattr(store, "STORE_DIR", git_repo.parent / ".contexer")
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
        entry["anchor_commit"] = store._git(str(repo), "rev-parse", "HEAD") or ""
    data = store._load(str(repo))
    data["entries"].append(entry)
    store._save(str(repo), data)
    return entry


def _reload(repo):
    return store._load(str(repo))["entries"][-1]


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
        assert entry.get("proposed_revision") is not None
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
        assert reloaded["anchor_commit"] == store._git(str(repo), "rev-parse", "HEAD")

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
                return store._git(repo_path, *args)
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
    def test_all_files_gone_attaches_rule_shaped_proposal_and_arms_nudge(self, repo, monkeypatch):
        _write(repo, "gone.py")
        _git(repo, "add", "gone.py")
        _commit(repo)
        content = "Use gone.py to configure the thing."
        _seed_entry(repo, content, source_files=["gone.py"])
        os.remove(str(repo / "gone.py"))

        monkeypatch.setattr(anchors, "_run_git", lambda *a, **k: None)  # no rename found
        result = anchors.verify_anchors(str(repo), force=True)
        assert result == {"reanchored": 0, "proposed": 1}

        reloaded = _reload(repo)
        prop = reloaded.get("proposed_revision")
        assert prop is not None
        # Rule-shaped: approving must yield a sane live revision, so the proposal starts
        # with the original rule text, not a status memo.
        assert prop["content"].startswith(content)
        assert "anchored files no longer exist: gone.py" in prop["content"]
        assert "confirm whether this decision still applies" in prop["content"]
        assert reloaded["status"] == "approved"       # current revision stays trusted
        # nudge armed
        assert store._pending_review_flag(str(repo)).exists()

    def test_approving_proposal_yields_sane_content(self, repo, monkeypatch):
        _write(repo, "gone.py")
        _git(repo, "add", "gone.py")
        _commit(repo)
        content = "Use gone.py to configure the thing."
        entry = _seed_entry(repo, content, source_files=["gone.py"])
        os.remove(str(repo / "gone.py"))
        monkeypatch.setattr(anchors, "_run_git", lambda *a, **k: None)
        anchors.verify_anchors(str(repo), force=True)

        store.approve_decision(str(repo), entry["id"], "approve")
        reloaded = _reload(repo)
        assert reloaded.get("proposed_revision") is None
        assert reloaded["content"].startswith(content)
        assert "anchored files no longer exist" in reloaded["content"]
        assert len(reloaded["revisions"]) == 2

    def test_dismissing_proposal_preserves_entry(self, repo, monkeypatch):
        _write(repo, "gone.py")
        _git(repo, "add", "gone.py")
        _commit(repo)
        content = "Use gone.py to configure the thing."
        entry = _seed_entry(repo, content, source_files=["gone.py"])
        os.remove(str(repo / "gone.py"))
        monkeypatch.setattr(anchors, "_run_git", lambda *a, **k: None)
        anchors.verify_anchors(str(repo), force=True)

        store.approve_decision(str(repo), entry["id"], "dismiss")
        reloaded = _reload(repo)
        assert reloaded.get("proposed_revision") is None
        assert reloaded["content"] == content          # unchanged
        assert len(reloaded["revisions"]) == 1          # no new revision appended

    def test_existing_proposal_entry_is_skipped(self, repo, monkeypatch):
        _write(repo, "gone.py")
        _git(repo, "add", "gone.py")
        _commit(repo)
        entry = _seed_entry(repo, "Use gone.py.", source_files=["gone.py"])
        os.remove(str(repo / "gone.py"))
        data = store._load(str(repo))
        target = next(e for e in data["entries"] if e["id"] == entry["id"])
        target["proposed_revision"] = store._build_proposal(
            target, "some unrelated suggested edit", "", "", "2026-01-01T00:00:00+00:00")
        store._save(str(repo), data)

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
        data = store._load(str(repo))
        by_id = {e["id"]: e for e in data["entries"]}
        assert by_id[pending["id"]].get("proposed_revision") is None
        assert by_id[ignored["id"]].get("proposed_revision") is None
        assert by_id[pending["id"]]["source_files"] == ["gone.py"]
        assert by_id[ignored["id"]]["source_files"] == ["gone.py"]


# ── budget ────────────────────────────────────────────────────────────────────

class TestBudget:
    def test_budget_exhaustion_stops_cleanly(self, repo, monkeypatch):
        # 6 entries each needing a rename check (2 git calls each = 12 needed), budget=10.
        _git(repo, "commit", "--allow-empty", "-q", "-m", "seed")
        for i in range(6):
            _seed_entry(repo, f"Decision about missing_{i}.py",
                        source_files=[f"missing_{i}.py"])

        calls = []

        def counting_git(repo_path, *args):
            calls.append(args)
            return None   # never finds a rename -> every entry is a total loss when exhausted

        monkeypatch.setattr(anchors, "_run_git", counting_git)
        result = anchors.verify_anchors(str(repo), force=True)   # must not raise
        assert len(calls) <= anchors._ANCHOR_GIT_BUDGET
        assert isinstance(result, dict)
        # whatever got decided (total-loss proposals for whichever entries were reached)
        # was actually persisted — a budget stop is clean, not a torn/half-written state.
        data = store._load(str(repo))
        assert len(data["entries"]) == 6


# ── fail-soft ────────────────────────────────────────────────────────────────

class TestFailSoft:
    def test_corrupt_store_never_raises(self, repo, monkeypatch):
        def boom(repo_path):
            raise ValueError("corrupt store")
        monkeypatch.setattr(store, "_load", boom)
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
        monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
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
        data = store._load(str(repo))
        entry = next(e for e in data["entries"] if e["subtype"] == "constraint")
        assert entry["source_files"] == ["new.py"]
