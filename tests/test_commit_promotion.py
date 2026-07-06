"""Tests for commit-time promotion (store.promote_on_commit).

A git commit is the strongest "this decision survived" signal, so it promotes provisional
(suggested) decisions to approved. Matching is evidence-based (changed files + message
keywords), never blanket - so these tests pin the strong/weak/none boundary, the seed-only
first run, and the invariants (pending_approval never auto-promoted, idempotent no-op)."""
import subprocess

import pytest

from contexer import store


def _git(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], capture_output=True, check=True)


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """A real git repo with STORE_DIR redirected; returns the repo path as str."""
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("init")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "init")
    return str(repo)


def _commit(repo, path, body, message):
    import os
    full = os.path.join(repo, path)
    os.makedirs(os.path.dirname(full), exist_ok=True) if os.path.dirname(path) else None
    with open(full, "w") as f:
        f.write(body)
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", message)


def _status(repo, entry_id):
    for e in store._load(repo)["entries"]:
        if e["id"] == entry_id:
            return store._entry_status(e)
    raise KeyError(entry_id)


def test_first_call_seeds_only_no_promotion(git_repo):
    _, eid = store.update_decision(git_repo, "core logic lives in contexer/ledger.py", "s1",
                                   "architecture", created_by="plan")
    # First observation must not promote against unknown history.
    assert store.promote_on_commit(git_repo) == {}
    assert _status(git_repo, eid) == "suggested"


def test_strong_file_match_promotes_to_approved(git_repo):
    _, eid = store.update_decision(git_repo, "Adopt event-sourcing; core logic in contexer/ledger.py",
                                   "s1", "architecture", created_by="plan")
    store.promote_on_commit(git_repo)  # seed
    _commit(git_repo, "contexer/ledger.py", "# es", "feat: add ledger")
    res = store.promote_on_commit(git_repo)
    assert eid in res["promoted"]
    assert _status(git_repo, eid) == "approved"
    rev = store._current_revision(store._load(git_repo)["entries"][0])
    assert any("Validated by commit" in f for f in rev["evidence"])


def test_weak_message_match_stays_suggested_with_evidence(git_repo):
    _, eid = store.update_decision(git_repo, "The queue uses exponential backoff for retries",
                                   "s1", "architecture", created_by="plan")
    store.promote_on_commit(git_repo)  # seed
    _commit(git_repo, "worker.py", "x", "fix: exponential backoff for queue retries")
    res = store.promote_on_commit(git_repo)
    assert eid in res["validated"]
    assert eid not in res["promoted"]
    assert _status(git_repo, eid) == "suggested"


def test_no_match_left_untouched(git_repo):
    _, eid = store.update_decision(git_repo, "UI copy should use sentence case everywhere",
                                   "s1", "convention", created_by="plan")
    store.promote_on_commit(git_repo)  # seed
    _commit(git_repo, "contexer/ledger.py", "# es", "feat: unrelated ledger change")
    res = store.promote_on_commit(git_repo)
    assert eid not in res.get("promoted", []) and eid not in res.get("validated", [])
    assert _status(git_repo, eid) == "suggested"


def test_pending_approval_never_auto_promoted(git_repo):
    # A constraint captured by AI is pending_approval - the explicit human gate must survive
    # a commit that references it by file.
    _, eid = store.update_decision(git_repo, "never edit contexer/ledger.py without review",
                                   "s1", "constraint", created_by="ai")
    assert _status(git_repo, eid) == "pending_approval"
    store.promote_on_commit(git_repo)  # seed
    _commit(git_repo, "contexer/ledger.py", "# es", "feat: touch ledger")
    store.promote_on_commit(git_repo)
    assert _status(git_repo, eid) == "pending_approval"


def test_idempotent_noop_on_same_head(git_repo):
    store.update_decision(git_repo, "core logic in contexer/ledger.py", "s1", "architecture",
                          created_by="plan")
    store.promote_on_commit(git_repo)  # seed
    _commit(git_repo, "contexer/ledger.py", "# es", "feat: ledger")
    assert store.promote_on_commit(git_repo)  # promotes
    assert store.promote_on_commit(git_repo) == {}  # HEAD unchanged -> no-op


def test_non_git_dir_is_safe_noop(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    assert store.promote_on_commit(str(tmp_path / "not-a-repo")) == {}


def test_substring_filename_does_not_falsely_promote(git_repo):
    # Changing store.py must NOT strong-promote a decision that only names test_store.py:
    # `base in low` substring matching would falsely fire (store.py is inside test_store.py).
    _, eid = store.update_decision(git_repo, "unit tests live in tests/test_store.py",
                                   "s1", "pattern", created_by="plan")
    store.promote_on_commit(git_repo)  # seed
    _commit(git_repo, "store.py", "x", "chore: bump deps")
    res = store.promote_on_commit(git_repo)
    assert eid not in res.get("promoted", [])
    assert _status(git_repo, eid) == "suggested"


def test_datastore_does_not_match_store(git_repo):
    # datastore.py contains "store.py" as a substring but is a different file - no promotion.
    _, eid = store.update_decision(git_repo, "persistence goes through datastore.py",
                                   "s1", "architecture", created_by="plan")
    store.promote_on_commit(git_repo)  # seed
    _commit(git_repo, "store.py", "x", "chore: bump deps")
    res = store.promote_on_commit(git_repo)
    assert eid not in res.get("promoted", [])
    assert _status(git_repo, eid) == "suggested"


def test_genuine_basename_reference_still_promotes(git_repo):
    # A decision naming store.py as a whole token IS a strong match when store.py changes.
    _, eid = store.update_decision(git_repo, "all persistence lives in store.py",
                                   "s1", "architecture", created_by="plan")
    store.promote_on_commit(git_repo)  # seed
    _commit(git_repo, "store.py", "x", "chore: rework persistence")
    res = store.promote_on_commit(git_repo)
    assert eid in res["promoted"]
    assert _status(git_repo, eid) == "approved"


def test_subdir_path_still_matches(git_repo):
    # A subdir path reference still matches its changed file (boundary check keeps full paths).
    _, eid = store.update_decision(git_repo, "core logic lives in contexer/store.py",
                                   "s1", "architecture", created_by="plan")
    store.promote_on_commit(git_repo)  # seed
    _commit(git_repo, "contexer/store.py", "# core", "feat: store")
    res = store.promote_on_commit(git_repo)
    assert eid in res["promoted"]
    assert _status(git_repo, eid) == "approved"


def test_valid_empty_diff_range_uses_full_range_messages(git_repo):
    # A net-zero range (file added then deleted) is a VALID range with an empty diff, not an
    # invalid one: weak matching must see the full-range messages, not just the tip commit's.
    import os
    _, eid = store.update_decision(git_repo, "the queue uses exponential backoff for retries",
                                   "s1", "architecture", created_by="plan")
    store.promote_on_commit(git_repo)  # seed at current HEAD
    _commit(git_repo, "foo.txt", "x", "fix: exponential backoff for queue retries")
    os.remove(os.path.join(git_repo, "foo.txt"))
    _git(git_repo, "add", "-A")
    _git(git_repo, "commit", "-m", "chore: cleanup unused")
    res = store.promote_on_commit(git_repo)
    assert eid in res["validated"]  # matched via the earlier commit's message, not the tip
    assert _status(git_repo, eid) == "suggested"


def test_commit_credit_does_not_survive_new_revision(git_repo):
    # The +25 commit-validation credit is scoped to the validated revision: a later revision
    # that rewrote the content must not inherit it.
    _, eid = store.update_decision(git_repo, "core logic in contexer/ledger.py", "s1",
                                   "architecture", created_by="plan")
    store.promote_on_commit(git_repo)  # seed
    _commit(git_repo, "contexer/ledger.py", "# es", "feat: ledger")
    store.promote_on_commit(git_repo)  # strong-promote
    entry = store._load(git_repo)["entries"][0]
    validated_rev = store._current_revision(entry)
    assert any("Validated by commit" in f for f in validated_rev["evidence"])
    base_score = validated_rev["confidence_score"]
    # Rewrite the content into a new revision.
    store._append_revision(entry, "core logic now split across modules", source="ai")
    new_rev = store._current_revision(entry)
    assert not any("Validated by commit" in f for f in new_rev["evidence"])
    assert new_rev["confidence_score"] == base_score - 25
    # The prior validated revision keeps its credit in history.
    assert any("Validated by commit" in f for f in validated_rev["evidence"])


def test_cursor_hook_promotes_on_commit(git_repo):
    """Cursor rides commit-promotion on its beforeSubmitPrompt hook (capture_constraint)."""
    from contexer.adapters import cursor
    _, eid = store.update_decision(git_repo, "core logic lives in contexer/ledger.py", "s1",
                                   "architecture", created_by="plan")
    cursor.capture_constraint(git_repo, "{}")  # seed
    _commit(git_repo, "contexer/ledger.py", "# es", "feat: ledger")
    cursor.capture_constraint(git_repo, "{}")  # promotes
    assert _status(git_repo, eid) == "approved"
