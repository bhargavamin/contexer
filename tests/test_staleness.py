"""Tests for staleness anchoring — source_files + anchor_commit, checked at injection."""
import os
import subprocess
from pathlib import Path

import pytest

from contexer import store


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Real git repo with one commit, STORE_DIR isolated; returns its path as a str."""
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    path = tmp_path / "gitrepo"
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "auth.py").write_text("def login(): pass\n", encoding="utf-8")
    (path / "other.py").write_text("x = 1\n", encoding="utf-8")
    _commit(path, "initial")
    return str(path)


def _commit(repo, message: str) -> None:
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@test.local", "-c", "user.name=T",
         "-c", "commit.gpgsign=false", "commit", "-q", "-m", message],
        cwd=repo, check=True)


def _touch(repo: str, name: str, body: str) -> None:
    Path(repo, name).write_text(body, encoding="utf-8")
    _commit(repo, f"change {name}")


def _entry(repo: str) -> dict:
    return store._load(repo)["entries"][0]


SUMMARY = "auth flow: login() verifies the token then issues a session cookie"


def test_unchanged_file_renders_no_note(repo):
    stored, eid = store.update_decision(repo, SUMMARY, "s1", "architecture",
                                        source_files=["auth.py"])
    assert stored
    assert _entry(repo)["anchor_commit"]  # HEAD resolved
    assert " [may be stale" not in store.get_context(repo, query="auth")
    assert " [may be stale" not in store._render_prompt_decisions(repo, [eid])


def test_changed_file_renders_note_in_both_sites(repo):
    _, eid = store.update_decision(repo, SUMMARY, "s1", "architecture",
                                   source_files=["auth.py"])
    _touch(repo, "auth.py", "def login(): return 'rewritten'\n")

    out = store.get_context(repo, query="auth")
    assert "[may be stale: auth.py changed since capture]" in out

    rendered = store._render_prompt_decisions(repo, [eid])
    assert "[may be stale: auth.py changed since capture]" in rendered


def test_uncommitted_edit_renders_note(repo):
    """The dominant case: the session is editing the file right now, nothing committed yet."""
    _, eid = store.update_decision(repo, SUMMARY, "s1", "architecture",
                                   source_files=["auth.py"])
    Path(repo, "auth.py").write_text("def login(): return 'edited, uncommitted'\n",
                                     encoding="utf-8")
    assert "[may be stale: auth.py changed since capture]" in store.get_context(repo, query="auth")
    assert "[may be stale: auth.py changed since capture]" in store._render_prompt_decisions(repo, [eid])


def test_note_counts_extra_changed_files(repo):
    store.update_decision(repo, SUMMARY, "s1", "architecture",
                          source_files=["auth.py", "other.py"])
    _touch(repo, "auth.py", "def login(): return 1\n")
    _touch(repo, "other.py", "x = 2\n")
    out = store.get_context(repo, query="auth")
    assert "[may be stale: auth.py changed since capture, +1 more]" in out


def test_unrelated_file_change_is_not_stale(repo):
    store.update_decision(repo, SUMMARY, "s1", "architecture", source_files=["auth.py"])
    _touch(repo, "other.py", "x = 99\n")
    assert " [may be stale" not in store.get_context(repo, query="auth")


def test_bogus_anchor_fails_soft(repo):
    _, eid = store.update_decision(repo, SUMMARY, "s1", "architecture",
                                   source_files=["auth.py"])
    data = store._load(repo)
    data["entries"][0]["anchor_commit"] = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"
    store._save(repo, data)
    assert store._staleness_note(repo, data["entries"][0]) == ""
    assert " [may be stale" not in store.get_context(repo, query="auth")
    assert " [may be stale" not in store._render_prompt_decisions(repo, [eid])


def test_non_git_repo_fails_soft(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    plain = str(tmp_path / "plain")
    os.mkdir(plain)
    store.update_decision(plain, SUMMARY, "s1", "architecture", source_files=["auth.py"])
    entry = _entry(plain)
    assert entry["source_files"] == ["auth.py"]
    assert entry["anchor_commit"] == ""  # no HEAD to resolve — files still stored
    assert " [may be stale" not in store.get_context(plain, query="auth")


def test_no_source_files_leaves_entry_unanchored(repo):
    store.update_decision(repo, SUMMARY, "s1", "architecture")
    entry = _entry(repo)
    assert "source_files" not in entry and "anchor_commit" not in entry
    _touch(repo, "auth.py", "def login(): return 2\n")
    assert " [may be stale" not in store.get_context(repo, query="auth")


def test_source_files_capped_at_ten(repo):
    store.update_decision(repo, SUMMARY, "s1", "architecture",
                          source_files=[f"m{i}.py" for i in range(25)])
    files = _entry(repo)["source_files"]
    assert len(files) == store._MAX_SOURCE_FILES == 10
    assert files[0] == "m0.py"


def test_non_string_and_blank_entries_dropped(repo):
    store.update_decision(repo, SUMMARY, "s1", "architecture",
                          source_files=["auth.py", "", None, 7, "  "])
    assert _entry(repo)["source_files"] == ["auth.py"]


def test_recurrence_does_not_reanchor(repo):
    store.update_decision(repo, SUMMARY, "s1", "architecture", source_files=["auth.py"])
    first = _entry(repo)["anchor_commit"]
    _touch(repo, "auth.py", "def login(): return 3\n")
    stored, _ = store.update_decision(repo, SUMMARY, "s2", "architecture",
                                      source_files=["other.py"])
    assert not stored  # duplicate -> recurrence
    entry = _entry(repo)
    assert entry["source_files"] == ["auth.py"] and entry["anchor_commit"] == first


def test_check_budget_caps_git_calls(repo, monkeypatch):
    calls = []
    monkeypatch.setattr(store, "_git", lambda *a, **kw: calls.append(a) or "")
    entries = [{"id": f"e{i}", "source_files": ["a.py"], "anchor_commit": "abc"}
               for i in range(6)]
    store._staleness_notes(repo, entries)
    assert len(calls) == store._STALENESS_MAX_CHECKS == 3


def test_legacy_entry_without_fields_round_trips(repo):
    store.update_decision(repo, SUMMARY, "s1", "architecture")
    before = store._load(repo)["entries"]
    store._save(repo, {"repo_path": repo, "entries": before})
    after = store._load(repo)["entries"]
    assert after == before
    assert "No matching" not in store.get_context(repo, query="auth")
