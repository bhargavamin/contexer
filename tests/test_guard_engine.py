"""Tests for the commit-time guard's Task-1 plumbing (staged-file reading and
path-matching helpers), Task-2 Tier-1 advisory engine (pairing, throttle,
dismissals), and Task-3 Tier-2 armed rules (arm/disarm, regex + secret checks,
blocking violations) in contexer/guard_engine.py. `store` is still imported
directly for the store-owned pieces the guard engine reads through it
(STORE_DIR, _load, _save, ...) and for the five public entrypoints it
re-exports at its own bottom for backward compatibility."""
import copy
import os
import subprocess
import sys
import time

import pytest

from contexer import store, guard_engine


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


# "café/módulo.py" — built from escapes on purpose: a pasted glyph is invisible
# in a diff and easy to mangle. Any path outside ASCII is C-quoted by
# `git diff --cached --name-only` without `-z`.
_NON_ASCII_REL = "caf\u00e9/m\u00f3dulo.py"


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

        staged = guard_engine._staged_files(str(git_repo))
        assert set(staged) == {"new_file.py", "mod.py", "renamed.py"}

    def test_deleted_files_excluded(self, git_repo):
        _write(git_repo, "gone.py", "bye\n")
        _git(git_repo, "add", "gone.py")
        _commit(git_repo)

        _git(git_repo, "rm", "-q", "gone.py")
        staged = guard_engine._staged_files(str(git_repo))
        assert "gone.py" not in staged

    def test_no_staged_changes_is_empty(self, git_repo):
        assert guard_engine._staged_files(str(git_repo)) == []

    def test_non_repo_fails_soft(self, tmp_path):
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()
        assert guard_engine._staged_files(str(not_a_repo)) == []

    def test_non_ascii_path_is_returned_unquoted(self, git_repo):
        """`--name-only` C-quotes any path outside ASCII (\"caf\\303\\251/...\"),
        and the quoted spelling survives canonicalization only to make every
        later `git show :<path>` fail — silently skipping the file. `-z` turns
        quoting off entirely."""
        relpath = _NON_ASCII_REL
        _write(git_repo, relpath, "x = 1\n")
        _git(git_repo, "add", relpath)
        assert guard_engine._staged_files(str(git_repo)) == [relpath]

    def test_paths_with_spaces_and_quotes_survive(self, git_repo):
        relpath = 'a dir/we"ird file.py'
        _write(git_repo, relpath, "x = 1\n")
        _git(git_repo, "add", relpath)
        assert guard_engine._staged_files(str(git_repo)) == [relpath]


# ── _staged_content ──────────────────────────────────────────────────────────

class TestStagedContent:
    def test_returns_index_not_working_tree_content(self, git_repo):
        _write(git_repo, "f.py", "committed\n")
        _git(git_repo, "add", "f.py")
        # edit working tree WITHOUT staging — index still holds the old content
        _write(git_repo, "f.py", "working-tree-only\n")

        assert guard_engine._staged_content(str(git_repo), "f.py") == "committed\n"

    def test_binary_null_byte_skipped(self, git_repo):
        _write(git_repo, "bin.dat", b"\x00\x01\x02binarydata")
        _git(git_repo, "add", "bin.dat")

        assert guard_engine._staged_content(str(git_repo), "bin.dat") == ""

    def test_oversize_file_skipped(self, git_repo):
        big = ("x" * (guard_engine._GUARD_MAX_FILE_BYTES + 1)).encode()
        _write(git_repo, "big.txt", big)
        _git(git_repo, "add", "big.txt")

        assert guard_engine._staged_content(str(git_repo), "big.txt") == ""

    def test_missing_path_fails_soft(self, git_repo):
        assert guard_engine._staged_content(str(git_repo), "nope.py") == ""

    def test_non_repo_fails_soft(self, tmp_path):
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()
        assert guard_engine._staged_content(str(not_a_repo), "f.py") == ""


# ── _merge_in_progress ───────────────────────────────────────────────────────

class TestMergeInProgress:
    def test_false_normally(self, git_repo):
        _write(git_repo, "a.txt", "1\n")
        _git(git_repo, "add", "a.txt")
        _commit(git_repo)
        assert guard_engine._merge_in_progress(str(git_repo)) is False

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

        assert guard_engine._merge_in_progress(str(git_repo)) is True

    def test_non_repo_fails_soft(self, tmp_path):
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()
        assert guard_engine._merge_in_progress(str(not_a_repo)) is False


# ── _guard_relpath ───────────────────────────────────────────────────────────

class TestGuardRelpath:
    def test_relative_spelling(self, git_repo):
        _write(git_repo, "src/a.py", "x\n")
        assert guard_engine._guard_relpath(str(git_repo), "src/a.py") == "src/a.py"

    def test_absolute_spelling_matches_relative(self, git_repo):
        _write(git_repo, "src/a.py", "x\n")
        rel = guard_engine._guard_relpath(str(git_repo), "src/a.py")
        abs_spelling = str(git_repo / "src" / "a.py")
        absolute = guard_engine._guard_relpath(str(git_repo), abs_spelling)
        assert rel == absolute == "src/a.py"

    def test_nonexistent_file_still_canonicalizes(self, git_repo):
        # guard scans staged paths before they necessarily exist on disk in every
        # caller's mental model — canonicalization must not require existence.
        assert guard_engine._guard_relpath(str(git_repo), "src/does_not_exist.py") == "src/does_not_exist.py"

    def test_root_level_file(self, git_repo):
        _write(git_repo, "top.py", "x\n")
        assert guard_engine._guard_relpath(str(git_repo), "top.py") == "top.py"

    def test_failure_returns_empty_string(self):
        # a None path can't be resolved — must fail soft, never raise
        assert guard_engine._guard_relpath("/tmp/somewhere", None) == ""


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
        assert guard_engine._pathlike_artifact(artifact) is True

    @pytest.mark.parametrize("artifact", [
        "FooError",
        "/api/users",
        "bareword",
        "SomeClass",
        "",
    ])
    def test_non_pathlike_rejected(self, artifact):
        assert guard_engine._pathlike_artifact(artifact) is False


# ── _artifact_path_match ─────────────────────────────────────────────────────

class TestArtifactPathMatch:
    def test_exact_relpath_equality(self):
        assert guard_engine._artifact_path_match("contexer/store.py", "contexer/store.py") is True

    def test_exact_bare_name_equality_at_root(self):
        assert guard_engine._artifact_path_match("store.py", "store.py") is True

    def test_dotted_module_maps_to_py_file(self):
        assert guard_engine._artifact_path_match("contexer.store", "contexer/store.py") is True

    def test_dotted_module_maps_to_package_init(self):
        assert guard_engine._artifact_path_match("contexer.store", "contexer/store/__init__.py") is True

    def test_dotted_module_no_match_wrong_file(self):
        assert guard_engine._artifact_path_match("contexer.store", "contexer/other.py") is False

    def test_multisegment_suffix_match_at_boundary(self):
        assert guard_engine._artifact_path_match("a/utils.py", "x/a/utils.py") is True

    def test_multisegment_suffix_requires_path_boundary(self):
        # "za/utils.py" ends with "a/utils.py" as raw characters but NOT at a "/"
        # boundary — must not match.
        assert guard_engine._artifact_path_match("a/utils.py", "za/utils.py") is False

    def test_bare_basename_never_matches_nested_file(self):
        assert guard_engine._artifact_path_match("utils.py", "a/utils.py") is False

    def test_bare_basename_never_matches_nested_file_config(self):
        assert guard_engine._artifact_path_match("config.json", "a/config.json") is False

    def test_symbol_artifact_never_matches(self):
        assert guard_engine._artifact_path_match("FooError", "contexer/foo.py") is False

    def test_route_shaped_artifact_never_matches(self):
        assert guard_engine._artifact_path_match("/api/users", "api/users.py") is False

    def test_unrelated_paths_no_match(self):
        assert guard_engine._artifact_path_match("contexer/store.py", "contexer/miner.py") is False

    def test_empty_inputs_fail_soft(self):
        assert guard_engine._artifact_path_match("", "contexer/store.py") is False
        assert guard_engine._artifact_path_match("contexer/store.py", "") is False


# ── Task 2: Tier-1 advisory engine — pairing, throttle, dismissals ──────────


# ── _guard_trusted ────────────────────────────────────────────────────────────

class TestGuardTrusted:
    def test_human_approved_is_trusted(self, repo):
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="human",
                             status="approved")
        assert guard_engine._guard_trusted(entry) is True

    def test_scan_approved_is_trusted(self, repo):
        entry = _seed_entry(repo, "Functions use snake_case naming (98% of 412)",
                             created_by="scan", status="approved")
        assert guard_engine._guard_trusted(entry) is True

    def test_bootstrap_approved_is_trusted(self, repo):
        entry = _seed_entry(repo, "Stack: Python, FastMCP, stdlib only",
                             created_by="bootstrap", status="approved")
        assert guard_engine._guard_trusted(entry) is True

    def test_plan_approved_is_trusted(self, repo):
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="plan",
                             status="approved")
        assert guard_engine._guard_trusted(entry) is True

    def test_plan_pending_approval_not_trusted(self, repo):
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="plan",
                             status="pending_approval")
        assert guard_engine._guard_trusted(entry) is False

    def test_plan_suggested_not_trusted(self, repo):
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="plan",
                             status="suggested")
        assert guard_engine._guard_trusted(entry) is False

    def test_ai_created_not_trusted(self, repo):
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="ai",
                             status="approved")
        assert guard_engine._guard_trusted(entry) is False

    def test_memory_imported_not_trusted(self, repo):
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="memory",
                             status="approved")
        assert guard_engine._guard_trusted(entry) is False

    def test_pending_approval_not_trusted(self, repo):
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="human",
                             status="pending_approval")
        assert guard_engine._guard_trusted(entry) is False

    def test_suggested_not_trusted(self, repo):
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="human",
                             status="suggested")
        assert guard_engine._guard_trusted(entry) is False

    def test_ignored_not_trusted(self, repo):
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="human",
                             status="ignored")
        assert guard_engine._guard_trusted(entry) is False


# ── _guard_hash ───────────────────────────────────────────────────────────────

class TestGuardHash:
    def test_is_12_char_hex(self):
        h = guard_engine._guard_hash("dec-1", "a/b.py")
        assert len(h) == 12
        int(h, 16)  # raises if not hex

    def test_deterministic(self):
        assert guard_engine._guard_hash("dec-1", "a/b.py") == guard_engine._guard_hash("dec-1", "a/b.py")

    def test_differs_by_decision(self):
        assert guard_engine._guard_hash("dec-1", "a/b.py") != guard_engine._guard_hash("dec-2", "a/b.py")

    def test_differs_by_path(self):
        assert guard_engine._guard_hash("dec-1", "a/b.py") != guard_engine._guard_hash("dec-1", "c/d.py")


# ── dismiss_guard / _dismissed_guard ─────────────────────────────────────────

class TestDismissGuard:
    def test_dismiss_then_dismissed_contains_hash(self, repo):
        guard_engine.dismiss_guard(str(repo), "dec-1", "a/b.py")
        expected = guard_engine._guard_hash("dec-1", guard_engine._guard_relpath(str(repo), "a/b.py"))
        assert expected in guard_engine._dismissed_guard(str(repo))

    def test_dismiss_idempotent(self, repo):
        guard_engine.dismiss_guard(str(repo), "dec-1", "a/b.py")
        guard_engine.dismiss_guard(str(repo), "dec-1", "a/b.py")
        assert len(guard_engine._dismissed_guard(str(repo))) == 1

    def test_abs_and_rel_spelling_dismiss_same_pair(self, repo):
        _write(repo, "a/b.py", "x\n")
        rel = "a/b.py"
        absolute = str(repo / "a" / "b.py")
        guard_engine.dismiss_guard(str(repo), "dec-1", absolute)
        dismissed = guard_engine._dismissed_guard(str(repo))
        expected = guard_engine._guard_hash("dec-1", rel)
        assert expected in dismissed
        assert len(dismissed) == 1

    def test_undismissed_hash_absent(self, repo):
        guard_engine.dismiss_guard(str(repo), "dec-1", "a/b.py")
        other = guard_engine._guard_hash("dec-2", "a/b.py")
        assert other not in guard_engine._dismissed_guard(str(repo))

    def test_no_sidecar_reads_empty(self, repo):
        assert guard_engine._dismissed_guard(str(repo)) == set()

    def test_corrupt_sidecar_fails_soft(self, repo):
        store.STORE_DIR.mkdir(parents=True, exist_ok=True)
        path = store.STORE_DIR / f".guard_dismissed_{store._slug(str(repo))}.json"
        path.write_text("not json{{{")
        assert guard_engine._dismissed_guard(str(repo)) == set()

    def test_dismiss_can_raise_on_bad_store_dir(self, repo, monkeypatch):
        # dismiss_guard is the management path — deliberately NOT fail-soft, unlike
        # the run path. Point STORE_DIR at a location that can't be created (a file,
        # not a dir, in its place) and confirm the error propagates.
        blocker = repo.parent / "not_a_dir"
        blocker.write_text("blocked")
        monkeypatch.setattr(store, "STORE_DIR", blocker / "sub")
        with pytest.raises(OSError):
            guard_engine.dismiss_guard(str(repo), "dec-1", "a/b.py")


# ── _guard_pairs ──────────────────────────────────────────────────────────────

class TestGuardPairs:
    def test_source_files_match(self, repo):
        entry = _seed_entry(repo, "Decided to use JWT for auth",
                             source_files=["auth/jwt.py"])
        pairs = guard_engine._guard_pairs(str(repo), ["auth/jwt.py"])
        assert len(pairs) == 1
        c = pairs[0]
        assert c["decision_id"] == entry["id"]
        assert c["file"] == "auth/jwt.py"
        assert c["scope"] == "personal"
        assert c["reason"] == "source_files match"
        assert c["emitted"] is True
        assert c["hash"] == guard_engine._guard_hash(entry["id"], "auth/jwt.py")

    def test_source_files_are_canonicalized_before_comparison(self, repo):
        """source_files must go through _guard_relpath like every other path the
        guard compares — an absolute or "./"-prefixed anchor still names the same
        staged file."""
        entry = _seed_entry(repo, "Decided to use JWT for auth",
                             source_files=[str(repo / "auth" / "jwt.py"), "./other.py"])
        pairs = guard_engine._guard_pairs(str(repo), ["auth/jwt.py", "other.py"])
        assert {p["file"] for p in pairs} == {"auth/jwt.py", "other.py"}
        assert {p["reason"] for p in pairs} == {"source_files match"}
        assert all(p["decision_id"] == entry["id"] for p in pairs)

    def test_plan_approved_source_files_pairs_as_emitted(self, repo):
        entry = _seed_entry(repo, "Decided to use JWT for auth", created_by="plan",
                             status="approved", source_files=["auth/jwt.py"])
        pairs = guard_engine._guard_pairs(str(repo), ["auth/jwt.py"])
        assert len(pairs) == 1
        assert pairs[0]["decision_id"] == entry["id"]
        assert pairs[0]["emitted"] is True

    def test_plan_pending_approval_never_pairs_as_emitted(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", created_by="plan",
                    status="pending_approval", source_files=["auth/jwt.py"])
        pairs = guard_engine._guard_pairs(str(repo), ["auth/jwt.py"])
        assert len(pairs) == 1
        assert pairs[0]["emitted"] is False
        assert pairs[0]["reason"] == "rejected: untrusted provenance"

    def test_module_artifact_match(self, repo):
        _seed_entry(repo, "The contexer.store module owns all read/write logic")
        pairs = guard_engine._guard_pairs(str(repo), ["contexer/store.py"])
        assert len(pairs) == 1
        assert pairs[0]["emitted"] is True
        assert pairs[0]["reason"] == "module artifact contexer.store"

    def test_suffix_path_artifact_match(self, repo):
        _seed_entry(repo, "See a/utils.py for the shared helper")
        pairs = guard_engine._guard_pairs(str(repo), ["x/a/utils.py"])
        assert len(pairs) == 1
        assert pairs[0]["emitted"] is True
        assert "a/utils.py" in pairs[0]["reason"]

    def test_bare_basename_does_not_pair(self, repo):
        _seed_entry(repo, "See utils.py for the shared helper")
        pairs = guard_engine._guard_pairs(str(repo), ["a/utils.py"])
        assert pairs == []

    def test_no_signal_no_candidate(self, repo):
        _seed_entry(repo, "We use bcrypt for password hashing", source_files=["auth/hash.py"])
        pairs = guard_engine._guard_pairs(str(repo), ["unrelated/file.py"])
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
        pairs = guard_engine._guard_pairs(str(repo), ["auth/jwt.py"])
        assert len(pairs) == 1
        assert pairs[0]["emitted"] is False
        assert pairs[0]["reason"] == "rejected: untrusted provenance"

    def test_global_decision_pairs_via_artifact_only(self, repo):
        entry = _seed_entry(repo, "The contexer.store module owns all read/write logic",
                             global_store=True)
        pairs = guard_engine._guard_pairs(str(repo), ["contexer/store.py"])
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
        pairs = guard_engine._guard_pairs(str(repo), ["auth/jwt.py"])
        assert pairs == []

    def test_decisions_override_replaces_loaded_entries(self, repo):
        # A decision seeded into the real repo store must NOT appear...
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        # ...when an explicit decisions= override is given instead.
        override_entry = store._new_decision_entry("Use OAuth for auth", "sess", "architecture",
                                                     created_by="human", status="approved")
        override_entry["source_files"] = ["auth/oauth.py"]
        pairs = guard_engine._guard_pairs(str(repo), ["auth/jwt.py", "auth/oauth.py"],
                                    decisions=[override_entry])
        assert len(pairs) == 1
        assert pairs[0]["file"] == "auth/oauth.py"
        assert pairs[0]["decision_id"] == override_entry["id"]

    def test_canonicalizes_staged_paths(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        absolute = str(repo / "auth" / "jwt.py")
        pairs = guard_engine._guard_pairs(str(repo), [absolute])
        assert len(pairs) == 1
        assert pairs[0]["file"] == "auth/jwt.py"


# ── legacy revision source back-stamp (guard integration) ─────────────────────

class TestGuardTrustsLegacyRevisionsAtReadTime:
    """A legacy store entry whose revision carries an explicit falsy `source` (predates
    provenance tracking) becomes trust-eligible via `_guard_trusted`'s read-time fallback
    to `created_by` — NOT via any storage rewrite. Binding ruling: the stored `source`
    must stay exactly as persisted (None stays None) because `share.py`'s `_wire_source`
    deliberately preserves `source: None` end-to-end as honest unknown provenance on the
    push wire; back-stamping it in storage would fabricate a false provenance there."""

    def _legacy_data(self, created_by, rev_source):
        return {
            "entries": [{
                "id": "legacy-guard-1", "type": "decision", "subtype": "architecture",
                "content": "Decided to use JWT for auth",
                "session_id": "s1", "session_ids": ["s1"],
                "timestamp": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
                "revision": 1, "status": "approved", "created_by": created_by,
                "source_files": ["auth/jwt.py"],
                "current_revision_id": "rev-legacy-1",
                "revisions": [{
                    "revision_id": "rev-legacy-1", "decision_id": "legacy-guard-1",
                    "version_number": 1, "content": "Decided to use JWT for auth",
                    "title": "Decided to use JWT for auth", "confidence_score": 0,
                    "evidence": [], "created_at": "2026-01-01T00:00:00+00:00",
                    "approved_at": "2026-01-01T00:00:00+00:00", "source": rev_source,
                }],
            }],
        }

    def test_human_created_becomes_trusted_and_pairs_after_load(self, repo):
        store._save(str(repo), self._legacy_data(created_by="human", rev_source=None))
        entry = store._load(str(repo))["entries"][0]
        # No storage rewrite: the falsy source persists exactly as stored.
        assert entry["revisions"][0]["source"] is None
        assert guard_engine._guard_trusted(entry) is True
        pairs = guard_engine._guard_pairs(str(repo), ["auth/jwt.py"])
        assert len(pairs) == 1
        assert pairs[0]["emitted"] is True

    def test_ai_created_stays_untrusted_after_load(self, repo):
        store._save(str(repo), self._legacy_data(created_by="ai", rev_source=None))
        entry = store._load(str(repo))["entries"][0]
        assert entry["revisions"][0]["source"] is None
        assert guard_engine._guard_trusted(entry) is False
        pairs = guard_engine._guard_pairs(str(repo), ["auth/jwt.py"])
        assert len(pairs) == 1
        assert pairs[0]["emitted"] is False
        assert pairs[0]["reason"] == "rejected: untrusted provenance"

    def test_falsy_created_by_also_stays_untrusted(self, repo):
        store._save(str(repo), self._legacy_data(created_by="", rev_source=None))
        entry = store._load(str(repo))["entries"][0]
        assert guard_engine._guard_trusted(entry) is False

    def test_legacy_source_stays_none_through_load_and_share_projection(self, repo):
        """Regression pin for the binding ruling: `_load` must never fabricate a
        provenance value onto a legacy revision's falsy `source`, and the share wire
        projection built from that loaded entry must still carry `source: None` —
        `share._wire_source` relies on this to pass None through as honest unknown
        provenance rather than coercing it to a false "ai"."""
        store._save(str(repo), self._legacy_data(created_by="human", rev_source=None))
        entry = store._load(str(repo))["entries"][0]
        assert entry["revisions"][0]["source"] is None
        projection = store._share_projection(entry)
        assert projection["source"] is None


# ── guard_staged ──────────────────────────────────────────────────────────────

class TestGuardStaged:
    def test_env_var_zero_skips_before_any_work(self, repo, monkeypatch):
        monkeypatch.setenv("CONTEXER_GUARD", "0")
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "x\n")
        _git(repo, "add", "auth/jwt.py")
        result = guard_engine.guard_staged(str(repo))
        assert result == {"advisories": [], "violations": [], "skipped": "env"}

    def test_no_staged_files_is_empty_result(self, repo):
        assert guard_engine.guard_staged(str(repo)) == {"advisories": [], "violations": []}

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

        result = guard_engine.guard_staged(str(repo), paths=["a.txt"])
        assert result["skipped"] == "merge"
        assert result["advisories"] == []
        assert result["violations"] == []

    def test_source_files_pair_surfaces_advisory(self, repo):
        entry = _seed_entry(repo, "Decided to use JWT for auth",
                             source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        result = guard_engine.guard_staged(str(repo))
        assert len(result["advisories"]) == 1
        assert result["advisories"][0]["decision_id"] == entry["id"]
        assert result["violations"] == []

    def test_never_writes_the_store(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        store_path = store._store_path(str(repo))
        before = store_path.read_bytes()
        guard_engine.guard_staged(str(repo))
        after = store_path.read_bytes()
        assert before == after

    def test_dismissed_pair_never_surfaces(self, repo):
        entry = _seed_entry(repo, "Decided to use JWT for auth",
                             source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        guard_engine.dismiss_guard(str(repo), entry["id"], "auth/jwt.py")
        result = guard_engine.guard_staged(str(repo))
        assert result["advisories"] == []

    def test_dismissal_persists_across_content_edits(self, repo):
        entry = _seed_entry(repo, "Decided to use JWT for auth",
                             source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        guard_engine.dismiss_guard(str(repo), entry["id"], "auth/jwt.py")
        _write(repo, "auth/jwt.py", "token = 2 # changed\n")
        _git(repo, "add", "auth/jwt.py")
        result = guard_engine.guard_staged(str(repo))
        assert result["advisories"] == []

    def test_throttle_same_content_silent_second_run(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        first = guard_engine.guard_staged(str(repo))
        assert len(first["advisories"]) == 1
        second = guard_engine.guard_staged(str(repo))
        assert second["advisories"] == []

    def test_throttle_re_advises_after_content_edit(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        first = guard_engine.guard_staged(str(repo))
        assert len(first["advisories"]) == 1

        _write(repo, "auth/jwt.py", "token = 2 # different content entirely\n")
        _git(repo, "add", "auth/jwt.py")
        second = guard_engine.guard_staged(str(repo))
        assert len(second["advisories"]) == 1

    def test_cap_honored_with_total_reported(self, repo):
        for i in range(7):
            _seed_entry(repo, f"Decision number {i} about auth handling",
                        source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        result = guard_engine.guard_staged(str(repo))
        assert len(result["advisories"]) == guard_engine._GUARD_MAX_ADVISORIES
        assert result["total_advisories"] == 7

    def test_capped_pairs_beyond_limit_not_stamped(self, repo):
        # Only the surfaced (capped) advisories are stamped — an uncapped pair must
        # still be free to advise on a later run once earlier ones are dismissed.
        for i in range(7):
            _seed_entry(repo, f"Decision number {i} about auth handling",
                        source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        guard_engine.guard_staged(str(repo))
        advised = guard_engine._guard_advised(str(repo))
        assert len(advised) == guard_engine._GUARD_MAX_ADVISORIES

    def test_corrupt_store_file_fails_soft(self, repo):
        store_path = store._store_path(str(repo))
        store_path.write_text("not json{{{")
        _write(repo, "a.py", "x\n")
        _git(repo, "add", "a.py")
        result = guard_engine.guard_staged(str(repo))
        assert result["advisories"] == []
        assert "violations" in result

    def test_corrupt_dismissed_sidecar_fails_soft(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        store.STORE_DIR.mkdir(parents=True, exist_ok=True)
        (store.STORE_DIR / f".guard_dismissed_{store._slug(str(repo))}.json").write_text("{{{")
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        result = guard_engine.guard_staged(str(repo))
        assert len(result["advisories"]) == 1

    def test_corrupt_advised_sidecar_fails_soft(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        store.STORE_DIR.mkdir(parents=True, exist_ok=True)
        (store.STORE_DIR / f".guard_advised_{store._slug(str(repo))}.json").write_text("{{{")
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        result = guard_engine.guard_staged(str(repo))
        assert len(result["advisories"]) == 1

    def test_internal_exception_degrades_to_error_true(self, repo, monkeypatch):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")

        def _boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(guard_engine, "_guard_pairs", _boom)
        result = guard_engine.guard_staged(str(repo))
        assert result == {"advisories": [], "violations": [], "error": True}

    def test_paths_override_used_instead_of_git_staged(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        # deliberately not `git add`ed — paths= must be honored over real staged state
        result = guard_engine.guard_staged(str(repo), paths=["auth/jwt.py"])
        assert len(result["advisories"]) == 1


class TestApprovalTimeAnchorGuardPairing:
    """End-to-end: this is the reason issue #172 exists — a decision anchored at
    APPROVAL time (not just at capture time) must pair in guard_staged."""

    def test_approve_with_source_files_pairs_in_guard_staged(self, repo):
        _write(repo, "auth/jwt.py", "token = 0\n")
        _git(repo, "add", "auth/jwt.py")
        _commit(repo, "init")

        # created_by="plan" + subtype="constraint": lands pending_approval (needs a human
        # look), but its revision source ("plan") is guard-trusted once approved — unlike
        # "ai", which stays untrusted even after approval (see TestGuardTrusted).
        stored, eid = store.update_decision(str(repo), "Always use JWT for session auth, "
                                             "never plain cookies", "s1", "constraint",
                                             created_by="plan")
        assert stored
        entry = next(e for e in store._load(str(repo))["entries"] if e["id"] == eid)
        assert entry["status"] == "pending_approval"
        assert "source_files" not in entry

        ok, msg = store.approve_decision(str(repo), eid, "approve",
                                         source_files=["auth/jwt.py"])
        assert ok, msg
        entry = next(e for e in store._load(str(repo))["entries"] if e["id"] == eid)
        assert entry["status"] == "approved"
        assert entry["source_files"] == ["auth/jwt.py"]
        assert entry["anchor_commit"]

        _write(repo, "auth/jwt.py", "token = 1  # rewritten\n")
        _git(repo, "add", "auth/jwt.py")
        result = guard_engine.guard_staged(str(repo))
        assert len(result["advisories"]) == 1
        assert result["advisories"][0]["decision_id"] == eid
        assert result["advisories"][0]["reason"] == "source_files match"


# ── guard_candidates ──────────────────────────────────────────────────────────

class TestGuardCandidates:
    def test_explain_false_returns_only_emitted(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _seed_entry(repo, "Decided to use JWT for auth", created_by="ai",
                    status="approved", source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        candidates = guard_engine.guard_candidates(str(repo), explain=False)
        assert len(candidates) == 1
        assert candidates[0]["emitted"] is True

    def test_explain_true_includes_rejected(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", created_by="ai",
                    status="approved", source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        candidates = guard_engine.guard_candidates(str(repo), explain=True)
        assert len(candidates) == 1
        assert candidates[0]["emitted"] is False
        assert candidates[0]["reason"] == "rejected: untrusted provenance"

    def test_explain_true_includes_dismissed_reason(self, repo):
        entry = _seed_entry(repo, "Decided to use JWT for auth",
                             source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        guard_engine.dismiss_guard(str(repo), entry["id"], "auth/jwt.py")
        candidates = guard_engine.guard_candidates(str(repo), explain=True)
        assert len(candidates) == 1
        assert candidates[0]["reason"] == "rejected: dismissed"
        assert candidates[0]["emitted"] is False

    def test_explain_true_includes_throttled_reason(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        guard_engine.guard_staged(str(repo))  # first run advises + stamps
        candidates = guard_engine.guard_candidates(str(repo), explain=True)
        assert len(candidates) == 1
        assert candidates[0]["reason"] == "rejected: throttled (content unchanged)"
        assert candidates[0]["emitted"] is False

    def test_mutates_no_stamps(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        guard_engine.guard_candidates(str(repo), explain=True)
        assert guard_engine._guard_advised(str(repo)) == {}
        # A subsequent guard_staged run must still advise — proof nothing was stamped.
        result = guard_engine.guard_staged(str(repo))
        assert len(result["advisories"]) == 1

    def test_no_staged_is_empty_list(self, repo):
        assert guard_engine.guard_candidates(str(repo)) == []

    def test_corrupt_store_fails_soft(self, repo):
        store_path = store._store_path(str(repo))
        store_path.write_text("not json{{{")
        _write(repo, "a.py", "x\n")
        _git(repo, "add", "a.py")
        assert guard_engine.guard_candidates(str(repo)) == []
        assert guard_engine.guard_candidates(str(repo), explain=True) == []


# ── anchor_candidates_for_backfill (Task 1 of #175) ───────────────────────────

class TestAnchorCandidatesForBackfill:
    def test_trusted_unanchored_content_path_candidate(self, repo):
        _write(repo, "auth/jwt.py", "token = 0\n")
        entry = _seed_entry(repo, "Decided to use auth/jwt.py for JWT-based session auth")
        result = guard_engine.anchor_candidates_for_backfill(str(repo))
        assert len(result) == 1
        assert result[0]["decision_id"] == entry["id"]
        assert result[0]["candidates"] == ["auth/jwt.py"]

    def test_already_anchored_decision_excluded(self, repo):
        _write(repo, "auth/jwt.py", "token = 0\n")
        _seed_entry(repo, "See auth/jwt.py for the JWT auth decision",
                    source_files=["auth/jwt.py"])
        assert guard_engine.anchor_candidates_for_backfill(str(repo)) == []

    @pytest.mark.parametrize("created_by,status", [
        ("ai", "approved"),
        ("memory", "approved"),
        ("human", "pending_approval"),
        ("human", "suggested"),
        ("human", "ignored"),
    ])
    def test_untrusted_or_unapproved_decision_excluded(self, repo, created_by, status):
        _write(repo, "auth/jwt.py", "token = 0\n")
        _seed_entry(repo, "See auth/jwt.py for the JWT auth decision",
                    created_by=created_by, status=status)
        assert guard_engine.anchor_candidates_for_backfill(str(repo)) == []

    def test_nonexistent_candidate_dropped_and_decision_skipped(self, repo):
        # auth/jwt.py is never written to the working tree.
        _seed_entry(repo, "See auth/jwt.py for the JWT auth decision")
        assert guard_engine.anchor_candidates_for_backfill(str(repo)) == []

    def test_dotted_module_maps_to_py_file(self, repo):
        _write(repo, "contexer/store.py", "x = 1\n")
        entry = _seed_entry(repo, "The contexer.store module owns all read/write logic")
        result = guard_engine.anchor_candidates_for_backfill(str(repo))
        assert len(result) == 1
        assert result[0]["decision_id"] == entry["id"]
        assert result[0]["candidates"] == ["contexer/store.py"]

    def test_dotted_module_maps_to_init_variant(self, repo):
        _write(repo, "pkg/mod/__init__.py", "x = 1\n")
        entry = _seed_entry(repo, "The pkg.mod module owns the widget logic")
        result = guard_engine.anchor_candidates_for_backfill(str(repo))
        assert len(result) == 1
        assert result[0]["decision_id"] == entry["id"]
        assert result[0]["candidates"] == ["pkg/mod/__init__.py"]

    def test_two_segment_literal_filename_not_mistaken_for_module(self, repo):
        # "config.yaml" also satisfies the dotted-module shape (two lowercase
        # segments) — it must still resolve to the literal file, not a bogus
        # "config/yaml.py" module guess.
        _write(repo, "config.yaml", "key: value\n")
        entry = _seed_entry(repo, "See config.yaml for the tool's default settings")
        result = guard_engine.anchor_candidates_for_backfill(str(repo))
        assert len(result) == 1
        assert result[0]["decision_id"] == entry["id"]
        assert result[0]["candidates"] == ["config.yaml"]

    def test_literal_and_module_spellings_coexist_as_separate_candidates(self, repo):
        # A literal file named "contexer.store" (no extension) alongside the real
        # module file "contexer/store.py" — both existing spellings of the same
        # dotted artifact surface as separate candidates. Accepted behavior (the
        # literal-first, module-mapping-as-additional-guesses shape in
        # _backfill_candidate_paths does not treat the two as exclusive), now
        # pinned here rather than left implicit.
        _write(repo, "contexer.store", "legacy marker file\n")
        _write(repo, "contexer/store.py", "x = 1\n")
        entry = _seed_entry(repo, "The contexer.store module owns all read/write logic")
        result = guard_engine.anchor_candidates_for_backfill(str(repo))
        assert len(result) == 1
        assert result[0]["decision_id"] == entry["id"]
        assert result[0]["candidates"] == ["contexer.store", "contexer/store.py"]

    def test_dedupes_repeated_artifact(self, repo):
        _write(repo, "auth/jwt.py", "token = 0\n")
        entry = _seed_entry(
            repo, "See auth/jwt.py for JWT auth; auth/jwt.py has the full implementation.")
        result = guard_engine.anchor_candidates_for_backfill(str(repo))
        assert len(result) == 1
        assert result[0]["decision_id"] == entry["id"]
        assert result[0]["candidates"] == ["auth/jwt.py"]

    def test_capped_at_max_source_files(self, repo):
        files = [f"pkg/mod{i}.py" for i in range(15)]
        for f in files:
            _write(repo, f, "x = 1\n")
        content = "Consolidated module map: " + "; ".join(files)
        entry = _seed_entry(repo, content)
        result = guard_engine.anchor_candidates_for_backfill(str(repo))
        assert len(result) == 1
        assert result[0]["decision_id"] == entry["id"]
        assert len(result[0]["candidates"]) == store._MAX_SOURCE_FILES

    def test_zero_candidates_decision_skipped_entirely(self, repo):
        _seed_entry(repo, "We use bcrypt for password hashing")
        assert guard_engine.anchor_candidates_for_backfill(str(repo)) == []

    def test_multiple_decisions_each_reported(self, repo):
        _write(repo, "auth/jwt.py", "x\n")
        _write(repo, "auth/oauth.py", "x\n")
        e1 = _seed_entry(repo, "See auth/jwt.py for the JWT decision")
        e2 = _seed_entry(repo, "See auth/oauth.py for the OAuth decision")
        result = guard_engine.anchor_candidates_for_backfill(str(repo))
        ids = {r["decision_id"] for r in result}
        assert ids == {e1["id"], e2["id"]}

    def test_title_present_in_result(self, repo):
        _write(repo, "auth/jwt.py", "x\n")
        entry = _seed_entry(repo, "See auth/jwt.py for JWT auth decisions and rationale",
                             title="Use JWT for session auth")
        result = guard_engine.anchor_candidates_for_backfill(str(repo))
        assert result[0]["title"] == "Use JWT for session auth"
        assert entry["title"] == "Use JWT for session auth"

    def test_read_only_never_mutates_store(self, repo):
        _write(repo, "auth/jwt.py", "x\n")
        _seed_entry(repo, "See auth/jwt.py for the JWT decision")
        guard_engine.anchor_candidates_for_backfill(str(repo))
        entry = store._load(str(repo))["entries"][0]
        assert not entry.get("source_files")

    def test_corrupt_store_fails_soft(self, repo):
        store_path = store._store_path(str(repo))
        store_path.write_text("not json{{{")
        assert guard_engine.anchor_candidates_for_backfill(str(repo)) == []


# ── store.apply_backfill_anchors ──────────────────────────────────────────────

class TestApplyBackfillAnchors:
    def test_applies_selection_and_stamps_anchor(self, repo):
        _write(repo, "auth/jwt.py", "x\n")
        _git(repo, "add", "auth/jwt.py")
        _commit(repo)
        entry = _seed_entry(repo, "See auth/jwt.py for the JWT decision")
        count = store.apply_backfill_anchors(str(repo), {entry["id"]: ["auth/jwt.py"]})
        assert count == 1
        loaded = next(e for e in store._load(str(repo))["entries"] if e["id"] == entry["id"])
        assert loaded["source_files"] == ["auth/jwt.py"]
        assert loaded["anchor_commit"]

    def test_unknown_decision_id_skipped(self, repo):
        count = store.apply_backfill_anchors(str(repo), {"nonexistent-id": ["auth/jwt.py"]})
        assert count == 0

    def test_empty_selections_no_op(self, repo):
        assert store.apply_backfill_anchors(str(repo), {}) == 0

    def test_one_save_for_whole_batch(self, repo, monkeypatch):
        e1 = _seed_entry(repo, "See auth/jwt.py for the JWT decision")
        e2 = _seed_entry(repo, "See auth/oauth.py for the OAuth decision")
        _write(repo, "auth/jwt.py", "x\n")
        _write(repo, "auth/oauth.py", "x\n")
        calls = []
        real_save = store._save

        def _counting_save(repo_path, data):
            calls.append(1)
            real_save(repo_path, data)

        monkeypatch.setattr(store, "_save", _counting_save)
        count = store.apply_backfill_anchors(
            str(repo), {e1["id"]: ["auth/jwt.py"], e2["id"]: ["auth/oauth.py"]})
        assert count == 2
        assert len(calls) == 1

    def test_already_anchored_entry_is_never_overwritten(self, repo):
        """Write-layer invariant: a decision already anchored by the time this batch
        runs (e.g. a concurrent session anchored it while the developer was mid-loop
        in `guard anchors`) must be left byte-identical — never re-anchored — even
        though it was explicitly selected in this batch. Other selections in the
        SAME batch still apply normally."""
        _write(repo, "auth/jwt.py", "x\n")
        _write(repo, "auth/oauth.py", "x\n")
        _git(repo, "add", "auth/jwt.py", "auth/oauth.py")
        _commit(repo)
        already_anchored = _seed_entry(repo, "See auth/jwt.py for the JWT decision",
                                        source_files=["auth/jwt.py"])
        # No anchor_commit stamped by _seed_entry (it sets source_files directly,
        # bypassing _anchor_sources) — a real pre-existing anchor snapshot to diff
        # against for byte-identity.
        before = copy.deepcopy(
            next(e for e in store._load(str(repo))["entries"]
                 if e["id"] == already_anchored["id"]))
        fresh = _seed_entry(repo, "See auth/oauth.py for the OAuth decision")

        count = store.apply_backfill_anchors(
            str(repo),
            {already_anchored["id"]: ["auth/oauth.py"],  # attempted re-anchor, must be ignored
             fresh["id"]: ["auth/oauth.py"]})

        assert count == 1  # only the fresh decision counts as newly anchored
        after_entries = {e["id"]: e for e in store._load(str(repo))["entries"]}
        assert after_entries[already_anchored["id"]] == before
        assert after_entries[fresh["id"]]["source_files"] == ["auth/oauth.py"]
        assert after_entries[fresh["id"]]["anchor_commit"]


# ── end-to-end: backfilled decision pairs in guard_staged ────────────────────

class TestAnchorBackfillEndToEnd:
    def test_backfilled_decision_pairs_when_file_staged(self, repo):
        _write(repo, "auth/jwt.py", "token = 0\n")
        _git(repo, "add", "auth/jwt.py")
        _commit(repo, "init")

        entry = _seed_entry(repo, "See auth/jwt.py for the JWT-based session auth decision")
        candidates = guard_engine.anchor_candidates_for_backfill(str(repo))
        assert len(candidates) == 1
        assert candidates[0]["decision_id"] == entry["id"]
        assert candidates[0]["candidates"] == ["auth/jwt.py"]

        applied = store.apply_backfill_anchors(
            str(repo), {entry["id"]: candidates[0]["candidates"]})
        assert applied == 1

        _write(repo, "auth/jwt.py", "token = 1  # rotated\n")
        _git(repo, "add", "auth/jwt.py")
        result = guard_engine.guard_staged(str(repo))
        assert len(result["advisories"]) == 1
        assert result["advisories"][0]["decision_id"] == entry["id"]
        assert result["advisories"][0]["reason"] == "source_files match"

        # A backfilled decision no longer surfaces as a further backfill candidate.
        assert guard_engine.anchor_candidates_for_backfill(str(repo)) == []


# ── Task 3: arm_guard / disarm_guard (management path) ───────────────────────

class TestArmGuard:
    def test_arm_regex_success(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        msg = guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern=r"TODO",
                               message="no TODOs allowed")
        assert isinstance(msg, str) and msg
        data = store._load(str(repo))
        stored = store._entry_by_id(data["entries"], entry["id"])
        gc = stored["guard_check"]
        assert gc["type"] == "regex"
        assert gc["pattern"] == "TODO"
        assert gc["message"] == "no TODOs allowed"
        assert gc["flags"] == ""
        assert gc["paths"] == ""
        assert "armed_at" in gc and gc["armed_at"]

    def test_arm_regex_with_i_flag(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern=r"todo", flags="i")
        data = store._load(str(repo))
        stored = store._entry_by_id(data["entries"], entry["id"])
        assert stored["guard_check"]["flags"] == "i"

    def test_arm_secret_success(self, repo):
        entry = _seed_entry(repo, "Never commit secrets")
        guard_engine.arm_guard(str(repo), entry["id"], "secret")
        data = store._load(str(repo))
        stored = store._entry_by_id(data["entries"], entry["id"])
        assert stored["guard_check"]["type"] == "secret"
        assert stored["guard_check"]["pattern"] == ""

    def test_arm_honors_paths_glob(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO", paths="*.py")
        data = store._load(str(repo))
        stored = store._entry_by_id(data["entries"], entry["id"])
        assert stored["guard_check"]["paths"] == "*.py"

    def test_arm_short_id_resolution(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"][:8], "regex", pattern="TODO")
        data = store._load(str(repo))
        stored = store._entry_by_id(data["entries"], entry["id"])
        assert stored.get("guard_check")

    def test_arm_refuses_unknown_id(self, repo):
        with pytest.raises(ValueError):
            guard_engine.arm_guard(str(repo), "no-such-id", "regex", pattern="TODO")

    def test_arm_refuses_unapproved_entry(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers", created_by="ai",
                             status="pending_approval")
        with pytest.raises(ValueError, match="approved"):
            guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")

    def test_arm_refuses_non_machine_checkable_type(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        with pytest.raises(ValueError, match="machine-checkable"):
            guard_engine.arm_guard(str(repo), entry["id"], "prose")

    def test_arm_refuses_secret_with_pattern(self, repo):
        entry = _seed_entry(repo, "Never commit secrets")
        with pytest.raises(ValueError, match="machine-checkable"):
            guard_engine.arm_guard(str(repo), entry["id"], "secret", pattern="AKIA.*")

    def test_arm_refuses_regex_without_pattern(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        with pytest.raises(ValueError, match="machine-checkable"):
            guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="")

    def test_arm_refuses_invalid_regex(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        with pytest.raises(ValueError, match="machine-checkable"):
            guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="(unclosed")

    def test_arm_refuses_unsupported_flags(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        with pytest.raises(ValueError, match="machine-checkable"):
            guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO", flags="m")

    def test_arm_global_entry(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers globally", global_store=True)
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")
        data = store._load_global()
        stored = store._entry_by_id(data["entries"], entry["id"])
        assert stored.get("guard_check")

    def test_arm_repo_entry_preferred_over_global_when_id_collides(self, repo):
        # Extremely unlikely in production (real UUIDs), but pins the documented
        # resolution order: repo store is tried before the global store.
        entry = _seed_entry(repo, "Repo-scoped decision")
        global_data = store._load_global()
        clashing = store._new_decision_entry("Global-scoped decision", "s", "architecture",
                                              created_by="human", status="approved")
        clashing["id"] = entry["id"]
        global_data["entries"].append(clashing)
        store._save_global(global_data)

        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")
        repo_entry = store._entry_by_id(store._load(str(repo))["entries"], entry["id"])
        global_entry = store._entry_by_id(store._load_global()["entries"], entry["id"])
        assert repo_entry.get("guard_check")
        assert not global_entry.get("guard_check")


class TestDisarmGuard:
    def test_disarm_removes_guard_check(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")
        guard_engine.disarm_guard(str(repo), entry["id"])
        data = store._load(str(repo))
        stored = store._entry_by_id(data["entries"], entry["id"])
        assert "guard_check" not in stored

    def test_disarm_global_entry(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers globally", global_store=True)
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")
        guard_engine.disarm_guard(str(repo), entry["id"])
        data = store._load_global()
        stored = store._entry_by_id(data["entries"], entry["id"])
        assert "guard_check" not in stored

    def test_disarm_unknown_id_raises(self, repo):
        with pytest.raises(ValueError):
            guard_engine.disarm_guard(str(repo), "no-such-id")

    def test_disarm_unarmed_entry_is_a_noop(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        msg = guard_engine.disarm_guard(str(repo), entry["id"])
        assert isinstance(msg, str) and msg


# ── Task 3: _armed_rules runtime status re-check ─────────────────────────────

class TestArmedRulesLifecycle:
    def test_armed_approved_entry_is_returned(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")
        data = store._load(str(repo))
        rules = guard_engine._armed_rules(data["entries"])
        assert [r["id"] for r in rules] == [entry["id"]]

    def test_unarmed_entry_excluded(self, repo):
        _seed_entry(repo, "Never commit TODO markers")
        data = store._load(str(repo))
        assert guard_engine._armed_rules(data["entries"]) == []

    def test_ignored_after_arming_stops_firing_without_disarm(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")
        ok, msg = store.approve_decision(str(repo), entry["id"], "ignore")
        assert ok, msg

        data = store._load(str(repo))
        stored = store._entry_by_id(data["entries"], entry["id"])
        # guard_check is still physically present (no disarm happened)...
        assert stored.get("guard_check")
        # ...but the runtime re-check excludes it because status != approved.
        assert guard_engine._armed_rules(data["entries"]) == []

    def test_end_to_end_guard_staged_stops_firing_after_ignore(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")
        _write(repo, "a.py", "# TODO fix this\n")
        _git(repo, "add", "a.py")

        before = guard_engine.guard_staged(str(repo))
        assert len(before["violations"]) == 1

        store.approve_decision(str(repo), entry["id"], "ignore")
        after = guard_engine.guard_staged(str(repo))
        assert after["violations"] == []


# ── Task 3: _rule_violations ──────────────────────────────────────────────────

class TestRuleViolations:
    def test_regex_hit_reports_correct_path_and_line(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers", title="No TODOs")
        entry["guard_check"] = {"type": "regex", "pattern": "TODO", "flags": "",
                                 "paths": "", "message": "no TODOs", "armed_at": "t"}
        content = "line one\nline two\n# TODO fix\nline four\n"
        hits = guard_engine._rule_violations([entry], "a.py", content)
        assert len(hits) == 1
        assert hits[0]["path"] == "a.py"
        assert hits[0]["line"] == 3
        assert hits[0]["decision_id"] == entry["id"]
        assert hits[0]["title"] == "No TODOs"
        assert hits[0]["message"] == "no TODOs"

    def test_regex_no_match_no_violation(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        entry["guard_check"] = {"type": "regex", "pattern": "TODO", "flags": "",
                                 "paths": "", "message": "", "armed_at": "t"}
        assert guard_engine._rule_violations([entry], "a.py", "nothing to see here\n") == []

    def test_regex_case_insensitive_flag_honored(self, repo):
        entry = _seed_entry(repo, "Never commit todo markers")
        entry["guard_check"] = {"type": "regex", "pattern": "todo", "flags": "i",
                                 "paths": "", "message": "", "armed_at": "t"}
        hits = guard_engine._rule_violations([entry], "a.py", "# TODO fix\n")
        assert len(hits) == 1

    def test_paths_glob_filters_out_non_matching_file(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        entry["guard_check"] = {"type": "regex", "pattern": "TODO", "flags": "",
                                 "paths": "*.md", "message": "", "armed_at": "t"}
        hits = guard_engine._rule_violations([entry], "a.py", "# TODO fix\n")
        assert hits == []

    def test_paths_glob_matches_intended_file(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        entry["guard_check"] = {"type": "regex", "pattern": "TODO", "flags": "",
                                 "paths": "*.py", "message": "", "armed_at": "t"}
        hits = guard_engine._rule_violations([entry], "a.py", "# TODO fix\n")
        assert len(hits) == 1

    def test_secret_rule_catches_aws_key(self, repo):
        entry = _seed_entry(repo, "Never commit secrets")
        entry["guard_check"] = {"type": "secret", "pattern": "", "flags": "",
                                 "paths": "", "message": "", "armed_at": "t"}
        content = "line one\nkey = 'AKIAIOSFODNN7EXAMPLE'\nline three\n"
        hits = guard_engine._rule_violations([entry], "a.py", content)
        assert len(hits) == 1
        assert hits[0]["line"] == 2

    def test_secret_rule_catches_pem_block(self, repo):
        entry = _seed_entry(repo, "Never commit secrets")
        entry["guard_check"] = {"type": "secret", "pattern": "", "flags": "",
                                 "paths": "", "message": "", "armed_at": "t"}
        pem = (
            "before\n"
            "-----BEGIN RSA PRIVATE KEY-----\n"
            "MIIEpAIBAAKCAQEA1234567890abcdefG\n"
            "abcdefghijklmnopqrstuvwxyz0123456\n"
            "-----END RSA PRIVATE KEY-----\n"
            "after\n"
        )
        hits = guard_engine._rule_violations([entry], "a.py", pem)
        assert len(hits) == 1

    def test_secret_rule_ignores_generic_prose_password(self, repo):
        entry = _seed_entry(repo, "Never commit secrets")
        entry["guard_check"] = {"type": "secret", "pattern": "", "flags": "",
                                 "paths": "", "message": "", "armed_at": "t"}
        content = 'password = "hunter2-wordy"\n'
        hits = guard_engine._rule_violations([entry], "a.py", content)
        assert hits == []


# ── Task 3: guard_staged integration (violations) ─────────────────────────────

class TestGuardStagedViolations:
    def test_regex_violation_surfaces(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")
        _write(repo, "a.py", "# TODO fix this\n")
        _git(repo, "add", "a.py")
        result = guard_engine.guard_staged(str(repo))
        assert len(result["violations"]) == 1
        assert result["violations"][0]["decision_id"] == entry["id"]

    def test_non_ascii_filename_is_actually_scanned(self, repo):
        """Regression: a C-quoted staged path made `git show :<path>` fail, so
        `_staged_content` returned "" and every armed rule silently skipped the
        file — a secret in it would have sailed through."""
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")
        _write(repo, _NON_ASCII_REL, "# TODO fix this\n")
        _git(repo, "add", _NON_ASCII_REL)
        result = guard_engine.guard_staged(str(repo))
        assert len(result["violations"]) == 1
        assert result["violations"][0]["path"] == _NON_ASCII_REL

    def test_invalid_utf8_filename_is_actually_scanned(self, repo):
        """Regression, one layer below the C-quoting bug: a staged path whose
        bytes are not valid UTF-8 at all (not just non-ASCII). Decoding it with
        errors="replace" (the pre-fix behaviour) collapses the bad byte to
        U+FFFD, a lossy spelling that can never round-trip back through `git
        show :<path>` — _staged_content then returns "" and every armed rule
        silently skips the file, the same silent-bypass class the C-quoting fix
        closed one layer up. errors="surrogateescape" + re-encoding via
        os.fsencode keeps the real bytes addressable end to end.

        Built from raw bytes via os.open on a bytes path — never a pasted
        glyph, and never routed through the str-based _write/_git helpers,
        since a genuinely invalid byte can't round-trip through a Python str
        without surrogateescape already applied. macOS (APFS) rejects
        filenames that aren't valid UTF-8, so this skips there and only
        actually exercises the invalid-byte path on Linux (where CI runs)."""
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")

        raw_name = b"bad_\xffname.py"  # 0xff is never valid as a UTF-8 lead byte
        raw_path = os.fsencode(str(repo)) + b"/" + raw_name
        try:
            fd = os.open(raw_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except (OSError, UnicodeError):
            pytest.skip("filesystem rejects filenames that aren't valid UTF-8")
        try:
            os.write(fd, b"# TODO fix this\n")
        finally:
            os.close(fd)
        subprocess.run(["git", "-C", str(repo), "add", "--", raw_name],
                        check=True, capture_output=True)

        expected_relpath = raw_name.decode("utf-8", "surrogateescape")
        staged = guard_engine._staged_files(str(repo))
        assert staged == [expected_relpath]  # not U+FFFD-mangled

        result = guard_engine.guard_staged(str(repo))
        assert len(result["violations"]) == 1
        assert result["violations"][0]["path"] == expected_relpath

    def test_no_armed_rules_no_violations(self, repo):
        _write(repo, "a.py", "# TODO fix this\n")
        _git(repo, "add", "a.py")
        result = guard_engine.guard_staged(str(repo))
        assert result["violations"] == []

    def test_violations_run_during_merge(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")
        # A genuine merge conflict on b.txt puts the repo into merge-in-progress
        # state (MERGE_HEAD present) without needing to resolve it.
        _write(repo, "b.txt", "line1\n")
        _git(repo, "add", "b.txt")
        _commit(repo, "base")
        _git(repo, "checkout", "-q", "-b", "other")
        _write(repo, "b.txt", "line1\nfrom-other\n")
        _git(repo, "add", "b.txt")
        _commit(repo, "other change")
        _git(repo, "checkout", "-q", "-")
        _write(repo, "b.txt", "line1\nfrom-main\n")
        _git(repo, "add", "b.txt")
        _commit(repo, "main change")
        _git(repo, "merge", "other", check=False)
        assert guard_engine._merge_in_progress(str(repo))

        # A cleanly-staged file unrelated to the conflict, added while the merge
        # is still unresolved: proves violations run against real staged content
        # during a merge (a conflicted path itself has no readable stage-0 blob
        # via `git show :path`, which is a separate, expected limitation).
        _write(repo, "a.py", "# TODO from-main\n")
        _git(repo, "add", "a.py")

        result = guard_engine.guard_staged(str(repo), paths=["a.py"])
        assert result["skipped"] == "merge"
        assert result["advisories"] == []
        assert len(result["violations"]) == 1

    def test_global_armed_rule_fires_in_repo_run(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers globally", global_store=True)
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")
        _write(repo, "a.py", "# TODO fix this\n")
        _git(repo, "add", "a.py")
        result = guard_engine.guard_staged(str(repo))
        assert len(result["violations"]) == 1
        assert result["violations"][0]["decision_id"] == entry["id"]

    def test_never_writes_the_store_with_violations(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")
        _write(repo, "a.py", "# TODO fix this\n")
        _git(repo, "add", "a.py")
        store_path = store._store_path(str(repo))
        before = store_path.read_bytes()
        guard_engine.guard_staged(str(repo))
        after = store_path.read_bytes()
        assert before == after

    def test_budget_overrun_returns_error_open(self, repo, monkeypatch):
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")
        _write(repo, "a.py", "# TODO fix this\n")
        _git(repo, "add", "a.py")

        real_time = time.time
        calls = {"n": 0}

        def _fake_time():
            calls["n"] += 1
            # First call establishes the deadline baseline; every call after
            # jumps far enough forward to blow the whole budget immediately.
            if calls["n"] <= 1:
                return real_time()
            return real_time() + guard_engine._GUARD_TIME_BUDGET + 100

        monkeypatch.setattr(time, "time", _fake_time)
        result = guard_engine.guard_staged(str(repo))
        assert result["error"] is True
        assert result["violations"] == []

    def test_budget_covers_tier_1_pairing_too(self, repo, monkeypatch):
        """The budget is the WHOLE guard_staged call, not just the Tier-2 half:
        with no armed rules at all, an overrun must still be caught (in pairing)
        and fail open rather than run unbounded."""
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")

        real_time = time.time
        calls = {"n": 0}

        def _fake_time():
            calls["n"] += 1
            if calls["n"] <= 1:
                return real_time()
            return real_time() + guard_engine._GUARD_TIME_BUDGET + 100

        monkeypatch.setattr(time, "time", _fake_time)
        result = guard_engine.guard_staged(str(repo))
        assert result == {"advisories": [], "violations": [], "error": True}

    def test_large_repo_completes_well_inside_the_budget(self, repo):
        """Perf regression (functional, not a benchmark — the budget is a hard
        bound the guard fails open on): 500 staged paths against a 500-decision
        store used to run the pairing loop as files x decisions x artifacts,
        measured at ~9s for 1000 staged files. Bound generously so CI variance
        can't flake it."""
        def _content(i):
            # A realistically wordy decision naming a handful of files: ~38
            # path/module artifacts each, which is what the cross product
            # multiplied by.
            return (f"Decision {i}: route traffic through pkg{i}/mod{i}.py rather "
                    f"than pkg{i}.legacy, because the adapter in svc{i}/handler{i}.py "
                    "owns it; see also "
                    + ", ".join(f"lib{i}/part{j}.py" for j in range(15))
                    + f" and contexer{i}.store, tests/test_{i}.py")

        data = store._load(str(repo))
        data["entries"] = [
            store._new_decision_entry(_content(i), "perf-session", "architecture",
                                       created_by="human", status="approved")
            for i in range(500)
        ]
        for i, entry in enumerate(data["entries"]):
            entry["source_files"] = [f"other{i}/thing{i}.py"]
        store._save(str(repo), data)
        staged = [f"src/module_{i}/file_{i}.py" for i in range(500)]

        start = time.time()
        result = guard_engine.guard_staged(str(repo), paths=staged)
        elapsed = time.time() - start
        assert result["advisories"] == []
        assert "error" not in result
        assert elapsed < 2.0, f"guard_staged took {elapsed:.2f}s"

    def test_invalid_armed_regex_pattern_is_skipped_not_raised(self, repo):
        # A pattern that once compiled at arm time but can't be re-derived cleanly
        # (defensive: guard_check written directly, bypassing arm_guard's validation)
        # must never raise the whole guard_staged call.
        entry = _seed_entry(repo, "Weird rule")
        data = store._load(str(repo))
        stored = store._entry_by_id(data["entries"], entry["id"])
        stored["guard_check"] = {"type": "regex", "pattern": "(unclosed", "flags": "",
                                  "paths": "", "message": "", "armed_at": "t"}
        store._save(str(repo), data)
        _write(repo, "a.py", "# TODO fix this\n")
        _git(repo, "add", "a.py")
        result = guard_engine.guard_staged(str(repo))
        assert result["violations"] == []
        assert "error" not in result


# ── Task 3: wire-safety regression ────────────────────────────────────────────

class TestWireSafety:
    def test_share_projection_never_leaks_guard_check(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        entry["guard_check"] = {"type": "regex", "pattern": "TODO", "flags": "",
                                 "paths": "", "message": "no TODOs", "armed_at": "t"}
        projected = store._share_projection(entry, redact_on=False)
        assert "guard_check" not in projected


# ── Task 4: store.py backward-compat re-export ────────────────────────────────

class TestStoreReexportIdentity:
    """store.py re-exports guard_engine's five public entrypoints for backward
    compatibility (any caller still holding `store.guard_staged` etc. must keep
    working, byte-identically, after the extraction). Pinned as object identity,
    not just equal behavior, so a future accidental re-wrap or re-def in either
    module would fail this test immediately."""

    def test_guard_staged_is_the_same_object(self):
        assert store.guard_staged is guard_engine.guard_staged

    def test_guard_candidates_is_the_same_object(self):
        assert store.guard_candidates is guard_engine.guard_candidates

    def test_arm_guard_is_the_same_object(self):
        assert store.arm_guard is guard_engine.arm_guard

    def test_disarm_guard_is_the_same_object(self):
        assert store.disarm_guard is guard_engine.disarm_guard

    def test_dismiss_guard_is_the_same_object(self):
        assert store.dismiss_guard is guard_engine.dismiss_guard


class TestImportOrderRegression:
    """store.py's guard re-export used to be an eager `from contexer.guard_engine
    import ...` at the bottom of the file — a real cycle with guard_engine's own
    top-level `from contexer import store`, which only resolved when store.py
    happened to be the module that started loading first. `import
    contexer.guard_engine` (or `from contexer import guard_engine`) as the very
    first touch of the package used to raise ImportError: cannot import name
    'guard_staged' from partially initialized module 'contexer.guard_engine'.
    A fresh subprocess (pytest has already imported both modules in this
    process, in the safe order, so an in-process check would prove nothing)
    with guard_engine imported BEFORE store is the exact previously-broken
    order; store.py's module `__getattr__` (PEP 562) fixes it by resolving the
    re-export lazily instead of at store.py's own load time."""

    def test_guard_engine_first_import_order_does_not_raise(self):
        probe = (
            "import contexer.guard_engine\n"
            "import contexer.store\n"
            "assert contexer.store.guard_staged is contexer.guard_engine.guard_staged\n"
            "print('OK')\n"
        )
        result = subprocess.run([sys.executable, "-c", probe],
                                 capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "OK"
