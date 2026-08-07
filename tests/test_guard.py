"""Tests for the commit-time guard's Task-1 plumbing: staged-file reading and
path-matching helpers in store.py. All helpers are fail-soft (git failure -> empty
result, never raise) and pure/path-oriented where noted."""
import os
import subprocess

import pytest

from contexer import store


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """A real throwaway git repo, isolated from the developer's global/system git
    config so commits succeed deterministically regardless of the host machine's
    setup (mirrors the git_repo fixture pattern in test_store.py's TestInsightCache)."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "guard@test.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Guard Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    return repo


def _write(repo, relpath, content):
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    return path


def _git(repo, *args, check=True):
    subprocess.run(["git", "-C", str(repo), *args], check=check,
                    capture_output=True)


def _commit(repo, message="init"):
    _git(repo, "commit", "-q", "-m", message)


# ── _staged_files ────────────────────────────────────────────────────────────

class TestStagedFiles:
    def test_added_modified_and_renamed_edited_included(self, git_repo):
        # baseline committed file, to be modified
        _write(git_repo, "mod.py", "line1\nline2\n")
        # baseline committed file, to be renamed + edited
        _write(git_repo, "old.py", "a\nb\nc\nd\ne\n")
        _git(git_repo, "add", "mod.py", "old.py")
        _commit(git_repo)

        # added
        _write(git_repo, "new_file.py", "print(1)\n")
        _git(git_repo, "add", "new_file.py")

        # modified
        _write(git_repo, "mod.py", "line1\nline2\nline3\n")
        _git(git_repo, "add", "mod.py")

        # renamed then edited
        _git(git_repo, "mv", "old.py", "renamed.py")
        _write(git_repo, "renamed.py", "a\nb\nc\nd\nCHANGED\n")
        _git(git_repo, "add", "renamed.py")

        staged = store._staged_files(str(git_repo))
        assert set(staged) == {"new_file.py", "mod.py", "renamed.py"}

    def test_deleted_files_excluded(self, git_repo):
        _write(git_repo, "gone.py", "bye\n")
        _git(git_repo, "add", "gone.py")
        _commit(git_repo)

        _git(git_repo, "rm", "-q", "gone.py")
        staged = store._staged_files(str(git_repo))
        assert "gone.py" not in staged

    def test_no_staged_changes_is_empty(self, git_repo):
        assert store._staged_files(str(git_repo)) == []

    def test_non_repo_fails_soft(self, tmp_path):
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()
        assert store._staged_files(str(not_a_repo)) == []


# ── _staged_content ──────────────────────────────────────────────────────────

class TestStagedContent:
    def test_returns_index_not_working_tree_content(self, git_repo):
        _write(git_repo, "f.py", "committed\n")
        _git(git_repo, "add", "f.py")
        # edit working tree WITHOUT staging — index still holds the old content
        _write(git_repo, "f.py", "working-tree-only\n")

        assert store._staged_content(str(git_repo), "f.py") == "committed\n"

    def test_binary_null_byte_skipped(self, git_repo):
        _write(git_repo, "bin.dat", b"\x00\x01\x02binarydata")
        _git(git_repo, "add", "bin.dat")

        assert store._staged_content(str(git_repo), "bin.dat") == ""

    def test_oversize_file_skipped(self, git_repo):
        big = ("x" * (store._GUARD_MAX_FILE_BYTES + 1)).encode()
        _write(git_repo, "big.txt", big)
        _git(git_repo, "add", "big.txt")

        assert store._staged_content(str(git_repo), "big.txt") == ""

    def test_missing_path_fails_soft(self, git_repo):
        assert store._staged_content(str(git_repo), "nope.py") == ""

    def test_non_repo_fails_soft(self, tmp_path):
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()
        assert store._staged_content(str(not_a_repo), "f.py") == ""


# ── _merge_in_progress ───────────────────────────────────────────────────────

class TestMergeInProgress:
    def test_false_normally(self, git_repo):
        _write(git_repo, "a.txt", "1\n")
        _git(git_repo, "add", "a.txt")
        _commit(git_repo)
        assert store._merge_in_progress(str(git_repo)) is False

    def test_true_with_merge_head_present(self, git_repo):
        _write(git_repo, "a.txt", "line1\n")
        _git(git_repo, "add", "a.txt")
        _commit(git_repo, "base")

        _git(git_repo, "checkout", "-q", "-b", "other")
        _write(git_repo, "a.txt", "line1\nfrom-other\n")
        _git(git_repo, "add", "a.txt")
        _commit(git_repo, "other change")

        _git(git_repo, "checkout", "-q", "-")
        _write(git_repo, "a.txt", "line1\nfrom-main\n")
        _git(git_repo, "add", "a.txt")
        _commit(git_repo, "main change")

        # conflicting merge — leaves MERGE_HEAD without completing
        _git(git_repo, "merge", "other", check=False)

        assert store._merge_in_progress(str(git_repo)) is True

    def test_non_repo_fails_soft(self, tmp_path):
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()
        assert store._merge_in_progress(str(not_a_repo)) is False


# ── _guard_relpath ───────────────────────────────────────────────────────────

class TestGuardRelpath:
    def test_relative_spelling(self, git_repo):
        _write(git_repo, "src/a.py", "x\n")
        assert store._guard_relpath(str(git_repo), "src/a.py") == "src/a.py"

    def test_absolute_spelling_matches_relative(self, git_repo):
        _write(git_repo, "src/a.py", "x\n")
        rel = store._guard_relpath(str(git_repo), "src/a.py")
        abs_spelling = str(git_repo / "src" / "a.py")
        absolute = store._guard_relpath(str(git_repo), abs_spelling)
        assert rel == absolute == "src/a.py"

    def test_nonexistent_file_still_canonicalizes(self, git_repo):
        # guard scans staged paths before they necessarily exist on disk in every
        # caller's mental model — canonicalization must not require existence.
        assert store._guard_relpath(str(git_repo), "src/does_not_exist.py") == "src/does_not_exist.py"

    def test_root_level_file(self, git_repo):
        _write(git_repo, "top.py", "x\n")
        assert store._guard_relpath(str(git_repo), "top.py") == "top.py"

    def test_failure_returns_empty_string(self):
        # a None path can't be resolved — must fail soft, never raise
        assert store._guard_relpath("/tmp/somewhere", None) == ""


# ── _pathlike_artifact ───────────────────────────────────────────────────────

class TestPathlikeArtifact:
    @pytest.mark.parametrize("artifact", [
        "contexer/store.py",
        "store.py",
        "contexer.store",
        "a/b/c.py",
        "config.toml",
    ])
    def test_pathlike_accepted(self, artifact):
        assert store._pathlike_artifact(artifact) is True

    @pytest.mark.parametrize("artifact", [
        "FooError",
        "/api/users",
        "bareword",
        "SomeClass",
        "",
    ])
    def test_non_pathlike_rejected(self, artifact):
        assert store._pathlike_artifact(artifact) is False


# ── _artifact_path_match ─────────────────────────────────────────────────────

class TestArtifactPathMatch:
    def test_exact_relpath_equality(self):
        assert store._artifact_path_match("contexer/store.py", "contexer/store.py") is True

    def test_exact_bare_name_equality_at_root(self):
        assert store._artifact_path_match("store.py", "store.py") is True

    def test_dotted_module_maps_to_py_file(self):
        assert store._artifact_path_match("contexer.store", "contexer/store.py") is True

    def test_dotted_module_maps_to_package_init(self):
        assert store._artifact_path_match("contexer.store", "contexer/store/__init__.py") is True

    def test_dotted_module_no_match_wrong_file(self):
        assert store._artifact_path_match("contexer.store", "contexer/other.py") is False

    def test_multisegment_suffix_match_at_boundary(self):
        assert store._artifact_path_match("a/utils.py", "x/a/utils.py") is True

    def test_multisegment_suffix_requires_path_boundary(self):
        # "za/utils.py" ends with "a/utils.py" as raw characters but NOT at a "/"
        # boundary — must not match.
        assert store._artifact_path_match("a/utils.py", "za/utils.py") is False

    def test_bare_basename_never_matches_nested_file(self):
        assert store._artifact_path_match("utils.py", "a/utils.py") is False

    def test_bare_basename_never_matches_nested_file_config(self):
        assert store._artifact_path_match("config.json", "a/config.json") is False

    def test_symbol_artifact_never_matches(self):
        assert store._artifact_path_match("FooError", "contexer/foo.py") is False

    def test_route_shaped_artifact_never_matches(self):
        assert store._artifact_path_match("/api/users", "api/users.py") is False

    def test_unrelated_paths_no_match(self):
        assert store._artifact_path_match("contexer/store.py", "contexer/miner.py") is False

    def test_empty_inputs_fail_soft(self):
        assert store._artifact_path_match("", "contexer/store.py") is False
        assert store._artifact_path_match("contexer/store.py", "") is False
