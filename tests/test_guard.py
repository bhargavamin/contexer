"""Tests for the commit-time guard's Task-1 plumbing (staged-file reading and
path-matching helpers) and Task-2 Tier-1 advisory engine (pairing, throttle,
dismissals) in store.py."""
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


@pytest.fixture
def repo(git_repo, monkeypatch):
    """`git_repo` with STORE_DIR redirected to a sibling temp dir — for Task-2 tests
    that read/write the store or the guard's sidecar files, not just git plumbing.
    Same pattern as test_store.py's tmp_repo / session_repo_preferred_over_pointer."""
    monkeypatch.setattr(store, "STORE_DIR", git_repo.parent / ".contexer")
    return git_repo


def _seed_entry(repo, content, *, subtype="architecture", created_by="human",
                 status="approved", source_files=None, global_store=False,
                 title="", session_id="test-session"):
    """Build a decision entry via the real entry constructor (so revisions/
    current_revision_id/status/source all come out shaped exactly like production
    data) and append it directly to the (repo or global) store — bypassing the
    novelty filter, which is irrelevant to the guard engine's own tests."""
    entry = store._new_decision_entry(content, session_id, subtype,
                                       created_by=created_by, status=status, title=title)
    if source_files is not None:
        entry["source_files"] = source_files
    if global_store:
        data = store._load_global()
        data["entries"].append(entry)
        store._save_global(data)
    else:
        data = store._load(str(repo))
        data["entries"].append(entry)
        store._save(str(repo), data)
    return entry


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


# ── Task 2: Tier-1 advisory engine — pairing, throttle, dismissals ──────────


# ── _guard_trusted ────────────────────────────────────────────────────────────

class TestGuardTrusted:
    def test_human_approved_is_trusted(self, repo):
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="human",
                             status="approved")
        assert store._guard_trusted(entry) is True

    def test_scan_approved_is_trusted(self, repo):
        entry = _seed_entry(repo, "Functions use snake_case naming (98% of 412)",
                             created_by="scan", status="approved")
        assert store._guard_trusted(entry) is True

    def test_bootstrap_approved_is_trusted(self, repo):
        entry = _seed_entry(repo, "Stack: Python, FastMCP, stdlib only",
                             created_by="bootstrap", status="approved")
        assert store._guard_trusted(entry) is True

    def test_ai_created_not_trusted(self, repo):
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="ai",
                             status="approved")
        assert store._guard_trusted(entry) is False

    def test_memory_imported_not_trusted(self, repo):
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="memory",
                             status="approved")
        assert store._guard_trusted(entry) is False

    def test_pending_approval_not_trusted(self, repo):
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="human",
                             status="pending_approval")
        assert store._guard_trusted(entry) is False

    def test_suggested_not_trusted(self, repo):
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="human",
                             status="suggested")
        assert store._guard_trusted(entry) is False

    def test_ignored_not_trusted(self, repo):
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="human",
                             status="ignored")
        assert store._guard_trusted(entry) is False


# ── _guard_hash ───────────────────────────────────────────────────────────────

class TestGuardHash:
    def test_is_12_char_hex(self):
        h = store._guard_hash("dec-1", "a/b.py")
        assert len(h) == 12
        int(h, 16)  # raises if not hex

    def test_deterministic(self):
        assert store._guard_hash("dec-1", "a/b.py") == store._guard_hash("dec-1", "a/b.py")

    def test_differs_by_decision(self):
        assert store._guard_hash("dec-1", "a/b.py") != store._guard_hash("dec-2", "a/b.py")

    def test_differs_by_path(self):
        assert store._guard_hash("dec-1", "a/b.py") != store._guard_hash("dec-1", "c/d.py")


# ── dismiss_guard / _dismissed_guard ─────────────────────────────────────────

class TestDismissGuard:
    def test_dismiss_then_dismissed_contains_hash(self, repo):
        store.dismiss_guard(str(repo), "dec-1", "a/b.py")
        expected = store._guard_hash("dec-1", store._guard_relpath(str(repo), "a/b.py"))
        assert expected in store._dismissed_guard(str(repo))

    def test_dismiss_idempotent(self, repo):
        store.dismiss_guard(str(repo), "dec-1", "a/b.py")
        store.dismiss_guard(str(repo), "dec-1", "a/b.py")
        assert len(store._dismissed_guard(str(repo))) == 1

    def test_abs_and_rel_spelling_dismiss_same_pair(self, repo):
        _write(repo, "a/b.py", "x\n")
        rel = "a/b.py"
        absolute = str(repo / "a" / "b.py")
        store.dismiss_guard(str(repo), "dec-1", absolute)
        dismissed = store._dismissed_guard(str(repo))
        expected = store._guard_hash("dec-1", rel)
        assert expected in dismissed
        assert len(dismissed) == 1

    def test_undismissed_hash_absent(self, repo):
        store.dismiss_guard(str(repo), "dec-1", "a/b.py")
        other = store._guard_hash("dec-2", "a/b.py")
        assert other not in store._dismissed_guard(str(repo))

    def test_no_sidecar_reads_empty(self, repo):
        assert store._dismissed_guard(str(repo)) == set()

    def test_corrupt_sidecar_fails_soft(self, repo):
        store.STORE_DIR.mkdir(parents=True, exist_ok=True)
        path = store.STORE_DIR / f".guard_dismissed_{store._slug(str(repo))}.json"
        path.write_text("not json{{{")
        assert store._dismissed_guard(str(repo)) == set()

    def test_dismiss_can_raise_on_bad_store_dir(self, repo, monkeypatch):
        # dismiss_guard is the management path — deliberately NOT fail-soft, unlike
        # the run path. Point STORE_DIR at a location that can't be created (a file,
        # not a dir, in its place) and confirm the error propagates.
        blocker = repo.parent / "not_a_dir"
        blocker.write_text("blocked")
        monkeypatch.setattr(store, "STORE_DIR", blocker / "sub")
        with pytest.raises(OSError):
            store.dismiss_guard(str(repo), "dec-1", "a/b.py")


# ── _guard_pairs ──────────────────────────────────────────────────────────────

class TestGuardPairs:
    def test_source_files_match(self, repo):
        entry = _seed_entry(repo, "Decided to use JWT for auth",
                             source_files=["auth/jwt.py"])
        pairs = store._guard_pairs(str(repo), ["auth/jwt.py"])
        assert len(pairs) == 1
        c = pairs[0]
        assert c["decision_id"] == entry["id"]
        assert c["file"] == "auth/jwt.py"
        assert c["scope"] == "personal"
        assert c["reason"] == "source_files match"
        assert c["emitted"] is True
        assert c["hash"] == store._guard_hash(entry["id"], "auth/jwt.py")

    def test_module_artifact_match(self, repo):
        _seed_entry(repo, "The contexer.store module owns all read/write logic")
        pairs = store._guard_pairs(str(repo), ["contexer/store.py"])
        assert len(pairs) == 1
        assert pairs[0]["emitted"] is True
        assert pairs[0]["reason"] == "module artifact contexer.store"

    def test_suffix_path_artifact_match(self, repo):
        _seed_entry(repo, "See a/utils.py for the shared helper")
        pairs = store._guard_pairs(str(repo), ["x/a/utils.py"])
        assert len(pairs) == 1
        assert pairs[0]["emitted"] is True
        assert "a/utils.py" in pairs[0]["reason"]

    def test_bare_basename_does_not_pair(self, repo):
        _seed_entry(repo, "See utils.py for the shared helper")
        pairs = store._guard_pairs(str(repo), ["a/utils.py"])
        assert pairs == []

    def test_no_signal_no_candidate(self, repo):
        _seed_entry(repo, "We use bcrypt for password hashing", source_files=["auth/hash.py"])
        pairs = store._guard_pairs(str(repo), ["unrelated/file.py"])
        assert pairs == []

    @pytest.mark.parametrize("created_by,status", [
        ("ai", "approved"),
        ("memory", "approved"),
        ("human", "pending_approval"),
        ("human", "suggested"),
        ("human", "ignored"),
    ])
    def test_untrusted_provenance_never_pairs_as_emitted(self, repo, created_by, status):
        _seed_entry(repo, "Decided to use JWT for auth", created_by=created_by,
                    status=status, source_files=["auth/jwt.py"])
        pairs = store._guard_pairs(str(repo), ["auth/jwt.py"])
        assert len(pairs) == 1
        assert pairs[0]["emitted"] is False
        assert pairs[0]["reason"] == "rejected: untrusted provenance"

    def test_global_decision_pairs_via_artifact_only(self, repo):
        entry = _seed_entry(repo, "The contexer.store module owns all read/write logic",
                             global_store=True)
        pairs = store._guard_pairs(str(repo), ["contexer/store.py"])
        assert len(pairs) == 1
        assert pairs[0]["decision_id"] == entry["id"]
        assert pairs[0]["scope"] == "global"
        assert pairs[0]["emitted"] is True

    def test_global_decision_source_files_never_consulted(self, repo):
        # Brief: global entries have no source_files — they pair via artifact match
        # only. Even if a global entry somehow carried source_files, guard must not
        # honor them for scope=global.
        _seed_entry(repo, "Unrelated content with no artifacts", global_store=True,
                    source_files=["auth/jwt.py"])
        pairs = store._guard_pairs(str(repo), ["auth/jwt.py"])
        assert pairs == []

    def test_decisions_override_replaces_loaded_entries(self, repo):
        # A decision seeded into the real repo store must NOT appear...
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        # ...when an explicit decisions= override is given instead.
        override_entry = store._new_decision_entry("Use OAuth for auth", "sess", "architecture",
                                                     created_by="human", status="approved")
        override_entry["source_files"] = ["auth/oauth.py"]
        pairs = store._guard_pairs(str(repo), ["auth/jwt.py", "auth/oauth.py"],
                                    decisions=[override_entry])
        assert len(pairs) == 1
        assert pairs[0]["file"] == "auth/oauth.py"
        assert pairs[0]["decision_id"] == override_entry["id"]

    def test_canonicalizes_staged_paths(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        absolute = str(repo / "auth" / "jwt.py")
        pairs = store._guard_pairs(str(repo), [absolute])
        assert len(pairs) == 1
        assert pairs[0]["file"] == "auth/jwt.py"


# ── guard_staged ──────────────────────────────────────────────────────────────

class TestGuardStaged:
    def test_env_var_zero_skips_before_any_work(self, repo, monkeypatch):
        monkeypatch.setenv("CONTEXER_GUARD", "0")
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "x\n")
        _git(repo, "add", "auth/jwt.py")
        result = store.guard_staged(str(repo))
        assert result == {"advisories": [], "violations": [], "skipped": "env"}

    def test_no_staged_files_is_empty_result(self, repo):
        assert store.guard_staged(str(repo)) == {"advisories": [], "violations": []}

    def test_merge_in_progress_skips_but_keeps_structure(self, repo):
        _write(repo, "a.txt", "line1\n")
        _git(repo, "add", "a.txt")
        _commit(repo, "base")
        _git(repo, "checkout", "-q", "-b", "other")
        _write(repo, "a.txt", "line1\nfrom-other\n")
        _git(repo, "add", "a.txt")
        _commit(repo, "other change")
        _git(repo, "checkout", "-q", "-")
        _write(repo, "a.txt", "line1\nfrom-main\n")
        _git(repo, "add", "a.txt")
        _commit(repo, "main change")
        _git(repo, "merge", "other", check=False)

        result = store.guard_staged(str(repo), paths=["a.txt"])
        assert result["skipped"] == "merge"
        assert result["advisories"] == []
        assert result["violations"] == []

    def test_source_files_pair_surfaces_advisory(self, repo):
        entry = _seed_entry(repo, "Decided to use JWT for auth",
                             source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        result = store.guard_staged(str(repo))
        assert len(result["advisories"]) == 1
        assert result["advisories"][0]["decision_id"] == entry["id"]
        assert result["violations"] == []

    def test_never_writes_the_store(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        store_path = store._store_path(str(repo))
        before = store_path.read_bytes()
        store.guard_staged(str(repo))
        after = store_path.read_bytes()
        assert before == after

    def test_dismissed_pair_never_surfaces(self, repo):
        entry = _seed_entry(repo, "Decided to use JWT for auth",
                             source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        store.dismiss_guard(str(repo), entry["id"], "auth/jwt.py")
        result = store.guard_staged(str(repo))
        assert result["advisories"] == []

    def test_dismissal_persists_across_content_edits(self, repo):
        entry = _seed_entry(repo, "Decided to use JWT for auth",
                             source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        store.dismiss_guard(str(repo), entry["id"], "auth/jwt.py")
        _write(repo, "auth/jwt.py", "token = 2 # changed\n")
        _git(repo, "add", "auth/jwt.py")
        result = store.guard_staged(str(repo))
        assert result["advisories"] == []

    def test_throttle_same_content_silent_second_run(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        first = store.guard_staged(str(repo))
        assert len(first["advisories"]) == 1
        second = store.guard_staged(str(repo))
        assert second["advisories"] == []

    def test_throttle_re_advises_after_content_edit(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        first = store.guard_staged(str(repo))
        assert len(first["advisories"]) == 1

        _write(repo, "auth/jwt.py", "token = 2 # different content entirely\n")
        _git(repo, "add", "auth/jwt.py")
        second = store.guard_staged(str(repo))
        assert len(second["advisories"]) == 1

    def test_cap_honored_with_total_reported(self, repo):
        for i in range(7):
            _seed_entry(repo, f"Decision number {i} about auth handling",
                        source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        result = store.guard_staged(str(repo))
        assert len(result["advisories"]) == store._GUARD_MAX_ADVISORIES
        assert result["total_advisories"] == 7

    def test_capped_pairs_beyond_limit_not_stamped(self, repo):
        # Only the surfaced (capped) advisories are stamped — an uncapped pair must
        # still be free to advise on a later run once earlier ones are dismissed.
        for i in range(7):
            _seed_entry(repo, f"Decision number {i} about auth handling",
                        source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        store.guard_staged(str(repo))
        advised = store._guard_advised(str(repo))
        assert len(advised) == store._GUARD_MAX_ADVISORIES

    def test_corrupt_store_file_fails_soft(self, repo):
        store_path = store._store_path(str(repo))
        store_path.write_text("not json{{{")
        _write(repo, "a.py", "x\n")
        _git(repo, "add", "a.py")
        result = store.guard_staged(str(repo))
        assert result["advisories"] == []
        assert "violations" in result

    def test_corrupt_dismissed_sidecar_fails_soft(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        store.STORE_DIR.mkdir(parents=True, exist_ok=True)
        (store.STORE_DIR / f".guard_dismissed_{store._slug(str(repo))}.json").write_text("{{{")
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        result = store.guard_staged(str(repo))
        assert len(result["advisories"]) == 1

    def test_corrupt_advised_sidecar_fails_soft(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        store.STORE_DIR.mkdir(parents=True, exist_ok=True)
        (store.STORE_DIR / f".guard_advised_{store._slug(str(repo))}.json").write_text("{{{")
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        result = store.guard_staged(str(repo))
        assert len(result["advisories"]) == 1

    def test_internal_exception_degrades_to_error_true(self, repo, monkeypatch):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")

        def _boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(store, "_guard_pairs", _boom)
        result = store.guard_staged(str(repo))
        assert result == {"advisories": [], "violations": [], "error": True}

    def test_paths_override_used_instead_of_git_staged(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        # deliberately not `git add`ed — paths= must be honored over real staged state
        result = store.guard_staged(str(repo), paths=["auth/jwt.py"])
        assert len(result["advisories"]) == 1


# ── guard_candidates ──────────────────────────────────────────────────────────

class TestGuardCandidates:
    def test_explain_false_returns_only_emitted(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _seed_entry(repo, "Decided to use JWT for auth", created_by="ai",
                    status="approved", source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        candidates = store.guard_candidates(str(repo), explain=False)
        assert len(candidates) == 1
        assert candidates[0]["emitted"] is True

    def test_explain_true_includes_rejected(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", created_by="ai",
                    status="approved", source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        candidates = store.guard_candidates(str(repo), explain=True)
        assert len(candidates) == 1
        assert candidates[0]["emitted"] is False
        assert candidates[0]["reason"] == "rejected: untrusted provenance"

    def test_explain_true_includes_dismissed_reason(self, repo):
        entry = _seed_entry(repo, "Decided to use JWT for auth",
                             source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        store.dismiss_guard(str(repo), entry["id"], "auth/jwt.py")
        candidates = store.guard_candidates(str(repo), explain=True)
        assert len(candidates) == 1
        assert candidates[0]["reason"] == "rejected: dismissed"
        assert candidates[0]["emitted"] is False

    def test_explain_true_includes_throttled_reason(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        store.guard_staged(str(repo))  # first run advises + stamps
        candidates = store.guard_candidates(str(repo), explain=True)
        assert len(candidates) == 1
        assert candidates[0]["reason"] == "rejected: throttled (content unchanged)"
        assert candidates[0]["emitted"] is False

    def test_mutates_no_stamps(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        store.guard_candidates(str(repo), explain=True)
        assert store._guard_advised(str(repo)) == {}
        # A subsequent guard_staged run must still advise — proof nothing was stamped.
        result = store.guard_staged(str(repo))
        assert len(result["advisories"]) == 1

    def test_no_staged_is_empty_list(self, repo):
        assert store.guard_candidates(str(repo)) == []

    def test_corrupt_store_fails_soft(self, repo):
        store_path = store._store_path(str(repo))
        store_path.write_text("not json{{{")
        _write(repo, "a.py", "x\n")
        _git(repo, "add", "a.py")
        assert store.guard_candidates(str(repo)) == []
        assert store.guard_candidates(str(repo), explain=True) == []
