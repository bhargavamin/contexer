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

from contexer import guard_engine, revisions, store
from tests.conftest import _git, _seed_entry, _write


# ── local helpers ───────────────────────────────────────────────────────────
#
# `git_repo`, `repo`, `_write`, `_git` and `_seed_entry` live in tests/conftest.py:
# tests/test_guard_policy_seam.py needs the same five, and reaching into this file for
# them meant importing a fixture under another name and re-binding it purely to stop
# ruff reading every `repo` parameter as a redefinition. Shared fixtures belong in
# conftest; the two helpers with no second reader stay here.

# "café/módulo.py" — built from escapes on purpose: a pasted glyph is invisible
# in a diff and easy to mangle. Any path outside ASCII is C-quoted by
# `git diff --cached --name-only` without `-z`.
_NON_ASCII_REL = "caf\u00e9/m\u00f3dulo.py"


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
    """Returns `(text, reason)`. `reason` is None only when the text IS the staged
    content; every other case names why there is none, so a caller can never mistake
    "could not read" for "nothing here" (see the function's own docstring for the two
    bugs that collapse caused)."""

    def test_returns_index_not_working_tree_content(self, git_repo):
        _write(git_repo, "f.py", "committed\n")
        _git(git_repo, "add", "f.py")
        # edit working tree WITHOUT staging — index still holds the old content
        _write(git_repo, "f.py", "working-tree-only\n")

        text, reason, fingerprint = guard_engine._staged_content(str(git_repo), "f.py")
        assert (text, reason) == ("committed\n", None) and fingerprint

    def test_binary_null_byte_skipped(self, git_repo):
        _write(git_repo, "bin.dat", b"\x00\x01\x02binarydata")
        _git(git_repo, "add", "bin.dat")

        text, reason, fingerprint = guard_engine._staged_content(str(git_repo), "bin.dat")
        assert (text, reason) == ("", "binary")
        # git READ it; only scannability was in question, so it still has an identity the
        # throttle can compare. None here is what made such a pair re-advise forever.
        assert fingerprint

    def test_oversize_file_skipped(self, git_repo, monkeypatch):
        # The cap is monkeypatched DOWN rather than exercised at its real 2MB value:
        # this pins the size branch, not the constant, and writing 2MB through git on
        # every run buys nothing. The real value's justification is measured in the
        # constant's own comment.
        monkeypatch.setattr(guard_engine, "_GUARD_MAX_FILE_BYTES", 64)
        _write(git_repo, "big.txt", b"x" * 65)
        _git(git_repo, "add", "big.txt")

        text, reason, fingerprint = guard_engine._staged_content(str(git_repo), "big.txt")
        assert (text, reason) == ("", "too-large")
        assert fingerprint, "an over-cap file was still read, so it has a fingerprint"

    def test_a_file_exactly_at_the_cap_is_still_read(self, git_repo, monkeypatch):
        # The comparison is `>`, not `>=`: an exactly-cap-sized file must be scanned,
        # or the boundary silently costs one file's worth of coverage.
        monkeypatch.setattr(guard_engine, "_GUARD_MAX_FILE_BYTES", 64)
        _write(git_repo, "exact.txt", b"y" * 64)
        _git(git_repo, "add", "exact.txt")

        text, reason, _fp = guard_engine._staged_content(str(git_repo), "exact.txt")
        assert reason is None and len(text) == 64

    def test_an_empty_staged_file_is_read_not_an_error(self, git_repo):
        # The state the old bare-"" return could not express at all: genuinely empty
        # content is a successful read, so `reason` must stay None.
        _write(git_repo, "empty.py", "")
        _git(git_repo, "add", "empty.py")

        assert guard_engine._staged_content(str(git_repo), "empty.py")[:2] == ("", None)

    def test_missing_path_fails_soft(self, git_repo):
        # The one case with NO fingerprint: nothing was read, so nothing is known.
        assert guard_engine._staged_content(str(git_repo), "nope.py") == ("", "unreadable", None)

    def test_non_repo_fails_soft(self, tmp_path):
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()
        assert guard_engine._staged_content(str(not_a_repo), "f.py") == ("", "unreadable", None)


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

    # ── issue #180: explicit human approval as its own trust discriminator ──

    def test_ai_created_explicitly_approved_by_human_is_trusted(self, repo):
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="ai",
                             status="approved", approved_by="human")
        assert guard_engine._guard_trusted(entry) is True

    def test_memory_imported_explicitly_approved_by_human_is_trusted(self, repo):
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="memory",
                             status="approved", approved_by="human")
        assert guard_engine._guard_trusted(entry) is True

    def test_ai_created_without_approved_by_stays_untrusted(self, repo):
        # Regression pin: approved status alone (no approved_by) must NOT be
        # enough for an ai-sourced entry — this is the pre-#180 behavior and
        # must still hold for entries no human ever explicitly ratified.
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="ai",
                             status="approved")
        assert "approved_by" not in entry
        assert guard_engine._guard_trusted(entry) is False

    def test_approved_by_human_without_approved_status_stays_untrusted(self, repo):
        # The status gate is still checked FIRST — approved_by alone can't
        # short-circuit a pending/suggested/ignored entry.
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="ai",
                             status="pending_approval", approved_by="human")
        assert guard_engine._guard_trusted(entry) is False

    def test_approved_by_non_human_value_stays_untrusted(self, repo):
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="ai",
                             status="approved", approved_by="ai")
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
        path = store.STORE_DIR / f".guard_dismissed_{store.repo_slug(str(repo))}.json"
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

    def test_explicitly_human_approved_ai_capture_pairs_as_emitted(self, repo):
        """Issue #180: an ai-sourced entry the developer explicitly approved must
        pair like any other trusted decision, even though its revision `source`
        stays 'ai'."""
        entry = _seed_entry(repo, "Decided to use JWT for auth", created_by="ai",
                             status="approved", approved_by="human",
                             source_files=["auth/jwt.py"])
        pairs = guard_engine._guard_pairs(str(repo), ["auth/jwt.py"])
        assert len(pairs) == 1
        assert pairs[0]["decision_id"] == entry["id"]
        assert pairs[0]["emitted"] is True
        assert pairs[0]["reason"] == "source_files match"

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

    def test_recent_edit_candidates_never_pair_or_promote_on_plain_approval(self, repo):
        """A recent-edit sidecar is proximity, not scope. It pairs nothing while pending,
        and approving only the decision text expires the guess without anchoring it.

        created_by="plan" (not the "ai" default) so the entry both lands pending_approval
        (constraint subtype forces approval_required for plan too, same as ai) AND is a
        _guard_trusted-eligible source once approved — an "ai"-sourced entry stays
        guard-untrusted forever regardless of approval, which would make this test unable
        to observe the "pairs after approval" half of the invariant."""
        store.record_edited_file(str(repo), "auth/jwt.py")
        stored, eid = store.update_decision(
            str(repo), "Decided to use JWT for auth", "sess-1", "constraint",
            created_by="plan")
        assert stored
        entry = store.entry_by_id(store.load(str(repo))["entries"], eid)
        assert entry["anchor_candidates"] == ["auth/jwt.py"]
        assert "source_files" not in entry
        assert entry["status"] == "pending_approval"

        # Pending: candidates carry zero pairing signal — not even an untrusted candidate.
        pairs = guard_engine._guard_pairs(str(repo), ["auth/jwt.py"])
        assert pairs == []

        ok, _msg = store.approve_decision(str(repo), eid, "approve")
        assert ok
        entry = store.entry_by_id(store.load(str(repo))["entries"], eid)
        assert not entry.get("source_files")
        assert "anchor_candidates" not in entry
        assert guard_engine._guard_pairs(str(repo), ["auth/jwt.py"]) == []

    def test_explicit_file_selection_promotes_and_pairs(self, repo):
        store.record_edited_file(str(repo), "auth/jwt.py")
        stored, eid = store.update_decision(
            str(repo), "Decided to use JWT for auth", "sess-1", "constraint",
            created_by="plan")
        assert stored

        ok, _msg = store.approve_decision(
            str(repo), eid, "approve", source_files=["auth/jwt.py"])
        assert ok
        entry = store.entry_by_id(store.load(str(repo))["entries"], eid)
        assert entry["source_files"] == ["auth/jwt.py"]
        assert "anchor_candidates" not in entry

        pairs = guard_engine._guard_pairs(str(repo), ["auth/jwt.py"])
        assert len(pairs) == 1
        assert pairs[0]["decision_id"] == eid
        assert pairs[0]["reason"] == "source_files match"
        assert pairs[0]["emitted"] is True


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
        store.save(str(repo), self._legacy_data(created_by="human", rev_source=None))
        entry = store.load(str(repo))["entries"][0]
        # No storage rewrite: the falsy source persists exactly as stored.
        assert entry["revisions"][0]["source"] is None
        assert guard_engine._guard_trusted(entry) is True
        pairs = guard_engine._guard_pairs(str(repo), ["auth/jwt.py"])
        assert len(pairs) == 1
        assert pairs[0]["emitted"] is True

    def test_ai_created_stays_untrusted_after_load(self, repo):
        store.save(str(repo), self._legacy_data(created_by="ai", rev_source=None))
        entry = store.load(str(repo))["entries"][0]
        assert entry["revisions"][0]["source"] is None
        assert guard_engine._guard_trusted(entry) is False
        pairs = guard_engine._guard_pairs(str(repo), ["auth/jwt.py"])
        assert len(pairs) == 1
        assert pairs[0]["emitted"] is False
        assert pairs[0]["reason"] == "rejected: untrusted provenance"

    def test_falsy_created_by_also_stays_untrusted(self, repo):
        store.save(str(repo), self._legacy_data(created_by="", rev_source=None))
        entry = store.load(str(repo))["entries"][0]
        assert guard_engine._guard_trusted(entry) is False

    def test_legacy_source_stays_none_through_load_and_share_projection(self, repo):
        """Regression pin for the binding ruling: `_load` must never fabricate a
        provenance value onto a legacy revision's falsy `source`, and the share wire
        projection built from that loaded entry must still carry `source: None` —
        `share._wire_source` relies on this to pass None through as honest unknown
        provenance rather than coercing it to a false "ai"."""
        store.save(str(repo), self._legacy_data(created_by="human", rev_source=None))
        entry = store.load(str(repo))["entries"][0]
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

    def test_constraint_capture_to_advisory_end_to_end(self, repo):
        """The full flow loop, finally guard-visible (issue #175, review fix I3): a deictic
        user constraint captured in the HOOK process (pending_approval, created_by="human" —
        guard-TRUSTED once approved, so the highest-value candidate carrier there is)
        accrues the repo's edited files as candidates, pairs NOTHING while pending, and
        surfaces a real advisory through guard_staged once the developer approves it."""
        store.record_edited_file(str(repo), "auth/jwt.py")
        eid, _content, status = store.capture_user_constraint(
            str(repo),
            "I'm not going to accept any performance degradation so ensure you clarify "
            "and ensure this feature is actual improvement",
            "s1")
        assert status == "pending_approval"
        assert store.entry_by_id(store.load(str(repo))["entries"], eid)[
            "anchor_candidates"] == ["auth/jwt.py"]

        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        assert guard_engine.guard_staged(str(repo))["advisories"] == []

        ok, _msg = store.approve_decision(
            str(repo), eid, "approve", source_files=["auth/jwt.py"])
        assert ok
        advisories = guard_engine.guard_staged(str(repo))["advisories"]
        assert len(advisories) == 1
        assert advisories[0]["decision_id"] == eid
        assert advisories[0]["reason"] == "source_files match"

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
        (store.STORE_DIR / f".guard_dismissed_{store.repo_slug(str(repo))}.json").write_text("{{{")
        _write(repo, "auth/jwt.py", "token = 1\n")
        _git(repo, "add", "auth/jwt.py")
        result = guard_engine.guard_staged(str(repo))
        assert len(result["advisories"]) == 1

    def test_corrupt_advised_sidecar_fails_soft(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        store.STORE_DIR.mkdir(parents=True, exist_ok=True)
        (store.STORE_DIR / f".guard_advised_{store.repo_slug(str(repo))}.json").write_text("{{{")
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
        entry = next(e for e in store.load(str(repo))["entries"] if e["id"] == eid)
        assert entry["status"] == "pending_approval"
        assert "source_files" not in entry

        ok, msg = store.approve_decision(str(repo), eid, "approve",
                                         source_files=["auth/jwt.py"])
        assert ok, msg
        entry = next(e for e in store.load(str(repo))["entries"] if e["id"] == eid)
        assert entry["status"] == "approved"
        assert entry["source_files"] == ["auth/jwt.py"]
        assert entry["anchor_commit"]

        _write(repo, "auth/jwt.py", "token = 1  # rewritten\n")
        _git(repo, "add", "auth/jwt.py")
        result = guard_engine.guard_staged(str(repo))
        assert len(result["advisories"]) == 1
        assert result["advisories"][0]["decision_id"] == eid
        assert result["advisories"][0]["reason"] == "source_files match"

    def test_approve_ai_captured_pairs_in_guard_staged_180(self, repo):
        """Issue #180: an ai-captured decision (the dominant capture path) that a
        developer EXPLICITLY approves must clear the guard's provenance gate via
        `approved_by == "human"`, even though its revision `source` stays 'ai' —
        never itself a member of `_GUARD_TRUSTED_SOURCES`. Before #180 this entry
        stayed guard-inert forever; only `plan`-sourced captures gained
        guard-visible anchors at approval time."""
        _write(repo, "auth/jwt.py", "token = 0\n")
        _git(repo, "add", "auth/jwt.py")
        _commit(repo, "init")

        # created_by left at its default ("ai"); subtype="constraint" forces
        # pending_approval regardless of created_by.
        stored, eid = store.update_decision(str(repo), "Always use JWT for session auth, "
                                             "never plain cookies", "s1", "constraint")
        assert stored
        entry = next(e for e in store.load(str(repo))["entries"] if e["id"] == eid)
        assert entry["status"] == "pending_approval"
        assert entry["created_by"] == "ai"
        assert "source_files" not in entry

        ok, msg = store.approve_decision(str(repo), eid, "approve",
                                         source_files=["auth/jwt.py"])
        assert ok, msg
        entry = next(e for e in store.load(str(repo))["entries"] if e["id"] == eid)
        assert entry["status"] == "approved"
        assert entry["approved_by"] == "human"
        assert entry["source_files"] == ["auth/jwt.py"]
        assert revisions.current_revision(entry)["source"] == "ai"
        assert guard_engine._guard_trusted(entry) is True

        _write(repo, "auth/jwt.py", "token = 1  # rewritten\n")
        _git(repo, "add", "auth/jwt.py")
        result = guard_engine.guard_staged(str(repo))
        assert len(result["advisories"]) == 1
        assert result["advisories"][0]["decision_id"] == eid
        assert result["advisories"][0]["reason"] == "source_files match"

    def test_explicit_approve_of_ai_capture_pairs_in_guard_staged_180(self, repo):
        """An explicit per-id approval is what earns guard trust for an ai-sourced capture.
        This used to be pinned on the bulk path; bulk approval has since been removed
        precisely because clearing a list wholesale is NOT the deliberate gesture the guard
        trust model assumes."""
        _write(repo, "auth/jwt.py", "token = 0\n")
        _git(repo, "add", "auth/jwt.py")
        _commit(repo, "init")

        stored, eid = store.update_decision(str(repo), "Always use JWT for session auth, "
                                             "never plain cookies", "s1", "constraint")
        assert stored
        ok, _msg = store.approve_decision(str(repo), eid, "approve")
        assert ok

        entry = next(e for e in store.load(str(repo))["entries"] if e["id"] == eid)
        assert entry["status"] == "approved"
        assert entry["approved_by"] == "human"
        assert guard_engine._guard_trusted(entry) is True


# ── issue #180 audit: every real auto-approval path never sets approved_by ────

class TestAutoApprovalNeverSetsApprovedByHuman:
    """Pins the invariant `_guard_trusted`'s `approved_by == "human"` clause
    relies on: every path that lands an entry in status='approved' WITHOUT a
    genuine `_apply_approval` ratification must never itself set `approved_by`.
    One case per real auto-approval route found in the #180 audit."""

    def test_memory_import_never_sets_approved_by(self, repo):
        status = store.upsert_memory_decision(
            str(repo), "Use bcrypt for password hashing", "s1", "convention", "mem-1")
        assert status == "created"
        entry = next(e for e in store.load(str(repo))["entries"]
                     if e.get("memory_key") == "mem-1")
        assert entry["status"] == "approved"
        assert entry["created_by"] == "memory"
        assert "approved_by" not in entry
        assert guard_engine._guard_trusted(entry) is False

    def test_scan_fact_pattern_ai_capture_never_sets_approved_by(self, repo):
        # _classify_level's Level-1 auto route matches _SCAN_FACT_PATTERNS
        # regardless of created_by, so a plain ai capture whose content happens
        # to start with a scan-fact prefix is born approved without any human
        # ever looking at it.
        stored, eid = store.update_decision(str(repo), "Package manager: uv", "s1",
                                             "architecture")
        assert stored
        entry = next(e for e in store.load(str(repo))["entries"] if e["id"] == eid)
        assert entry["status"] == "approved"
        assert entry["created_by"] == "ai"
        assert "approved_by" not in entry
        assert guard_engine._guard_trusted(entry) is False

    def test_legacy_migration_never_sets_approved_by(self, repo):
        legacy = {
            "id": "legacy-1", "type": "decision", "subtype": "architecture",
            "content": "Decided to use JWT for auth",
            "session_id": "s1", "session_ids": ["s1"],
            "timestamp": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "revision": 1,
            # status/created_by deliberately absent — pre-provenance entry.
        }
        store.save(str(repo), {"entries": [legacy]})
        entry = store.load(str(repo))["entries"][0]
        assert entry["status"] == "approved"
        assert entry["created_by"] == "ai"
        assert "approved_by" not in entry
        assert guard_engine._guard_trusted(entry) is False

    def test_global_ai_capture_never_sets_approved_by(self, repo):
        # update_global_decision's MCP path (update_global_context) leaves
        # created_by at its "ai" default and stores status="approved" directly —
        # no _apply_approval involvement, so approved_by is never set.
        ok, eid = store.update_global_decision("Never log raw request bodies", "s1",
                                                "constraint")
        assert ok
        entry = next(e for e in store.load_global()["entries"] if e["id"] == eid)
        assert entry["status"] == "approved"
        assert entry["created_by"] == "ai"
        assert "approved_by" not in entry
        assert guard_engine._guard_trusted(entry) is False


# ── Greptile P1: approved_by invalidated when a non-human revision goes live ──

class TestApprovedByStampInvalidatedByNonHumanRevision:
    """`approved_by` is an ENTRY-level stamp, but pattern/convention trivial updates via
    `update_context(replace_id=...)` (and memory-sync refreshes) apply IN PLACE as a new
    current revision (`revisions.append_revision`) - before this fix the entry kept its 'human' stamp
    while the live content became unreviewed AI/tool text, so the guard trusted (and advised
    with) content the developer never actually saw. `revisions.append_revision` now pops `approved_by`
    whenever the new revision's `source` isn't 'human'; a genuine ratification site
    (`_apply_approval` promoting a Suggested Update) restamps AFTER appending."""

    def test_trivial_ai_update_invalidates_stamp_the_regression(self, repo):
        """THE Greptile scenario, end-to-end: a human-approved ai-captured pattern decision,
        corrected in place by another AI turn, must stop being guard-trusted and stop
        pairing as an advisory."""
        entry = _seed_entry(repo, "Log files live under logs/<date>.log", created_by="ai",
                             subtype="pattern", status="approved", approved_by="human",
                             source_files=["logging/setup.py"])
        eid = entry["id"]
        assert guard_engine._guard_trusted(entry) is True

        ok, rid = store.update_decision(
            str(repo), "Log files live under var/log/<date>.log", "s2", "pattern",
            replace_id=eid, created_by="ai")
        assert ok and rid == eid

        updated = store.entry_by_id(store.load(str(repo))["entries"], eid)
        assert updated["revision"] == 2
        assert "approved_by" not in updated
        assert guard_engine._guard_trusted(updated) is False

        _write(repo, "logging/setup.py", "# rewritten\n")
        _git(repo, "add", "logging/setup.py")
        result = guard_engine.guard_staged(str(repo))
        assert result["advisories"] == []

    def test_developer_approving_the_change_restores_trust(self, repo):
        """A significant (architecture/constraint) AI change to a human-approved entry
        lands as a Suggested Update instead of applying silently — the live entry (and its
        stamp) stays untouched until the developer actually approves it, at which point
        trust is restored on the new content."""
        entry = _seed_entry(repo, "Rollback endpoint is /api/v1/rollback", created_by="ai",
                             subtype="architecture", status="approved", approved_by="human",
                             source_files=["api/rollback.py"])
        eid = entry["id"]

        ok, rid = store.update_decision(
            str(repo), "Rollback endpoint is /api/v2/rollback", "s2", "architecture",
            replace_id=eid, created_by="ai")
        assert ok and rid == eid
        pending = store.entry_by_id(store.load(str(repo))["entries"], eid)
        assert pending.get("proposed_revision") is not None
        # Unreviewed proposal: the live content (and its stamp) is untouched so far.
        assert pending["revision"] == 1
        assert pending.get("approved_by") == "human"
        assert guard_engine._guard_trusted(pending) is True

        ok, msg = store.approve_decision(str(repo), eid, "approve")
        assert ok, msg
        approved = store.entry_by_id(store.load(str(repo))["entries"], eid)
        assert approved["revision"] == 2
        assert approved["content"] == "Rollback endpoint is /api/v2/rollback"
        assert approved["approved_by"] == "human"
        assert guard_engine._guard_trusted(approved) is True

    def test_suggested_update_promotion_ordering_pin(self, repo):
        """Ordering pin: `_apply_approval` stamps `approved_by` AFTER `_promote_proposal`
        (which calls `revisions.append_revision` with the proposal's own source - 'ai' by default,
        NOT 'human'). If a future change stamped BEFORE promoting again, the chokepoint's
        invalidation would immediately erase the stamp the approval action just set, and
        this assertion would catch it. The promoted revision's own `source` field stays
        'ai' (provenance of the content is unchanged); only `approved_by` reflects the
        human ratification."""
        entry = _seed_entry(repo, "Rollback endpoint is /api/v1/rollback", created_by="ai",
                             subtype="architecture", status="approved", approved_by="human",
                             source_files=["api/rollback.py"])
        eid = entry["id"]
        store.update_decision(str(repo), "Rollback endpoint is /api/v2/rollback", "s2",
                              "architecture", replace_id=eid, created_by="ai")

        ok, msg = store.approve_decision(str(repo), eid, "approve")
        assert ok, msg
        approved = store.entry_by_id(store.load(str(repo))["entries"], eid)
        assert revisions.current_revision(approved)["source"] == "ai"
        assert approved["approved_by"] == "human"
        assert guard_engine._guard_trusted(approved) is True

    def test_memory_sync_in_place_update_invalidates_stamp(self, repo):
        """A memory-imported fact refreshed in place (source='memory') is tool-written, not
        human-reviewed — the same invalidation must apply."""
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="memory",
                             status="approved", approved_by="human",
                             source_files=["auth/hash.py"])
        eid = entry["id"]
        data = store.load(str(repo))
        stored = store.entry_by_id(data["entries"], eid)
        stored["memory_key"] = "mem-1"
        store.save(str(repo), data)
        assert guard_engine._guard_trusted(stored) is True

        status = store.upsert_memory_decision(
            str(repo), "Use argon2 for password hashing", "s2", "convention", "mem-1")
        assert status == "updated"

        updated = store.entry_by_id(store.load(str(repo))["entries"], eid)
        assert updated["revision"] == 2
        assert "approved_by" not in updated
        assert guard_engine._guard_trusted(updated) is False


# ── Greptile P1 #2: confidence recomputed AFTER stamp invalidation, not before ─────────

class TestConfidenceRecomputedAfterStampInvalidation:
    """`revisions.append_revision` used to snapshot confidence (`_compute_confidence`) BEFORE
    popping `approved_by` — so a non-human revision replacing human-approved content still
    carried the ~40-point approval bonus and the "Approved by developer" evidence factor on
    the freshly-created revision (and the resynced head cache), even though `approved_by`
    itself was gone from the entry a moment later. The fix pops the stamp first, then
    computes confidence from the now-unstamped entry."""

    def test_ai_inplace_update_strips_confidence_bonus_and_factor(self, repo):
        entry = _seed_entry(repo, "Log files live under logs/<date>.log", created_by="ai",
                             subtype="pattern", status="approved", approved_by="human",
                             source_files=["logging/setup.py"])
        eid = entry["id"]

        ok, rid = store.update_decision(
            str(repo), "Log files live under var/log/<date>.log", "s2", "pattern",
            replace_id=eid, created_by="ai")
        assert ok and rid == eid

        updated = store.entry_by_id(store.load(str(repo))["entries"], eid)
        cur = revisions.current_revision(updated)
        assert "approved_by" not in updated
        assert "Approved by developer" not in cur["evidence"]
        assert "Approved by developer" not in updated.get("confidence_factors", [])
        # created_by="ai" contributes no factor either, so nothing but the base score remains.
        assert cur["confidence_score"] == 30
        assert updated["confidence"] == 30

    def test_memory_sync_inplace_update_strips_confidence_bonus_and_factor(self, repo):
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="memory",
                             status="approved", approved_by="human",
                             source_files=["auth/hash.py"])
        eid = entry["id"]
        data = store.load(str(repo))
        stored = store.entry_by_id(data["entries"], eid)
        stored["memory_key"] = "mem-1"
        store.save(str(repo), data)

        status = store.upsert_memory_decision(
            str(repo), "Use argon2 for password hashing", "s2", "convention", "mem-1")
        assert status == "updated"

        updated = store.entry_by_id(store.load(str(repo))["entries"], eid)
        cur = revisions.current_revision(updated)
        assert "approved_by" not in updated
        assert "Approved by developer" not in cur["evidence"]
        assert "Approved by developer" not in updated.get("confidence_factors", [])

    def test_approval_path_still_yields_bonus_and_factor(self, repo):
        """Pin: the legitimate pending->approved blessing (no stamp invalidation involved)
        must keep computing the approval bonus + factor on the newly-blessed revision."""
        entry = _seed_entry(repo, "Use bcrypt for password hashing", created_by="ai",
                             subtype="convention", status="pending_approval")
        eid = entry["id"]

        ok, msg = store.approve_decision(str(repo), eid, "approve")
        assert ok, msg
        approved = store.entry_by_id(store.load(str(repo))["entries"], eid)
        cur = revisions.current_revision(approved)
        assert approved["approved_by"] == "human"
        assert "Approved by developer" in cur["evidence"]
        assert "Approved by developer" in approved.get("confidence_factors", [])
        assert cur["confidence_score"] >= 40
        assert approved["confidence"] == cur["confidence_score"]

    def test_suggested_update_promotion_still_yields_bonus_and_factor(self, repo):
        """Pin: `_apply_approval`'s Suggested-Update promotion branch (stamp-then-recompute,
        fixed earlier on this branch) still ends with the approval bonus on the promoted
        revision, unaffected by the `revisions.append_revision` reorder."""
        entry = _seed_entry(repo, "Rollback endpoint is /api/v1/rollback", created_by="ai",
                             subtype="architecture", status="approved", approved_by="human",
                             source_files=["api/rollback.py"])
        eid = entry["id"]
        store.update_decision(str(repo), "Rollback endpoint is /api/v2/rollback", "s2",
                              "architecture", replace_id=eid, created_by="ai")

        ok, msg = store.approve_decision(str(repo), eid, "approve")
        assert ok, msg
        approved = store.entry_by_id(store.load(str(repo))["entries"], eid)
        cur = revisions.current_revision(approved)
        assert approved["approved_by"] == "human"
        assert "Approved by developer" in cur["evidence"]
        assert cur["confidence_score"] >= 40


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


# ── decisions_for_files (Task 1 of #174) ──────────────────────────────────────

class TestDecisionsForFiles:
    def test_source_files_hit(self, repo):
        entry = _seed_entry(repo, "Decided to use JWT for auth",
                             source_files=["auth/jwt.py"])
        hits = guard_engine.decisions_for_files(str(repo), ["auth/jwt.py"])
        assert len(hits) == 1
        h = hits[0]
        assert h["decision_id"] == entry["id"]
        assert h["title"]
        assert h["status"] == "approved"
        assert h["scope"] == "personal"
        assert h["files_matched"] == ["auth/jwt.py"]
        assert h["reason"] == "source_files match"

    def test_artifact_hit(self, repo):
        entry = _seed_entry(repo, "The contexer.store module owns all read/write logic")
        hits = guard_engine.decisions_for_files(str(repo), ["contexer/store.py"])
        assert len(hits) == 1
        assert hits[0]["decision_id"] == entry["id"]
        assert hits[0]["files_matched"] == ["contexer/store.py"]
        assert "module artifact" in hits[0]["reason"]

    def test_no_signal_no_hit(self, repo):
        _seed_entry(repo, "We use bcrypt for password hashing", source_files=["auth/hash.py"])
        hits = guard_engine.decisions_for_files(str(repo), ["unrelated/file.py"])
        assert hits == []

    def test_pending_decision_still_hits(self, repo):
        # Unlike _guard_pairs, retrieval has no guard-trust filter — an untrusted
        # (pending/ai) decision still governs the file, it just carries its real
        # status so the caller can render a [pending] tag.
        entry = _seed_entry(repo, "Decided to use JWT for auth", created_by="ai",
                             status="pending_approval", source_files=["auth/jwt.py"])
        hits = guard_engine.decisions_for_files(str(repo), ["auth/jwt.py"])
        assert len(hits) == 1
        assert hits[0]["decision_id"] == entry["id"]
        assert hits[0]["status"] == "pending_approval"

    def test_ignored_decision_excluded(self, repo):
        entry = _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        store.approve_decision(str(repo), entry["id"], "ignore")
        hits = guard_engine.decisions_for_files(str(repo), ["auth/jwt.py"])
        assert hits == []

    def test_global_scope_tagged(self, repo):
        entry = _seed_entry(repo, "The contexer.store module owns all read/write logic",
                             global_store=True)
        hits = guard_engine.decisions_for_files(str(repo), ["contexer/store.py"])
        assert len(hits) == 1
        assert hits[0]["decision_id"] == entry["id"]
        assert hits[0]["scope"] == "global"

    def test_source_files_beats_artifact_reason(self, repo):
        # A decision whose content also mentions an artifact for a DIFFERENT queried
        # file must still report "source_files match" as its overall reason — the
        # strongest signal wins, even though it wasn't the first file matched.
        entry = _seed_entry(
            repo,
            "See contexer/other.py for background; the real decision lives in auth/jwt.py",
            source_files=["auth/jwt.py"],
        )
        hits = guard_engine.decisions_for_files(
            str(repo), ["contexer/other.py", "auth/jwt.py"])
        assert len(hits) == 1
        assert hits[0]["decision_id"] == entry["id"]
        assert hits[0]["reason"] == "source_files match"
        assert set(hits[0]["files_matched"]) == {"contexer/other.py", "auth/jwt.py"}

    def test_dedup_one_hit_per_decision_even_with_multiple_files_matched(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth",
                    source_files=["auth/jwt.py", "auth/session.py"])
        hits = guard_engine.decisions_for_files(
            str(repo), ["auth/jwt.py", "auth/session.py"])
        assert len(hits) == 1
        assert set(hits[0]["files_matched"]) == {"auth/jwt.py", "auth/session.py"}

    def test_reverse_tracing_files_matched(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth",
                    source_files=["auth/jwt.py"])
        hits = guard_engine.decisions_for_files(
            str(repo), ["auth/jwt.py", "unrelated/other.py"])
        assert len(hits) == 1
        assert hits[0]["files_matched"] == ["auth/jwt.py"]
        assert "unrelated/other.py" not in hits[0]["files_matched"]

    def test_absolute_path_input_canonicalized(self, repo):
        entry = _seed_entry(repo, "Decided to use JWT for auth",
                             source_files=["auth/jwt.py"])
        absolute = str(repo / "auth" / "jwt.py")
        hits = guard_engine.decisions_for_files(str(repo), [absolute])
        assert len(hits) == 1
        assert hits[0]["decision_id"] == entry["id"]
        assert hits[0]["files_matched"] == ["auth/jwt.py"]

    def test_escape_dropped(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        hits = guard_engine.decisions_for_files(str(repo), ["../../etc/passwd"])
        assert hits == []

    def test_empty_files_fails_soft(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        assert guard_engine.decisions_for_files(str(repo), []) == []

    def test_garbage_input_fails_soft(self, repo):
        assert guard_engine.decisions_for_files(str(repo), None) == []

    def test_corrupt_store_fails_soft(self, repo):
        store_path = store._store_path(str(repo))
        store_path.write_text("not json{{{")
        assert guard_engine.decisions_for_files(str(repo), ["auth/jwt.py"]) == []

    def test_decisions_override_replaces_loaded_entries(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        override_entry = store._new_decision_entry("Use OAuth for auth", "sess", "architecture",
                                                     created_by="human", status="approved")
        override_entry["source_files"] = ["auth/oauth.py"]
        hits = guard_engine.decisions_for_files(str(repo), ["auth/jwt.py", "auth/oauth.py"],
                                                  decisions=[override_entry])
        assert len(hits) == 1
        assert hits[0]["decision_id"] == override_entry["id"]
        assert hits[0]["scope"] == "personal"

    def test_bare_basename_does_not_pair(self, repo):
        _seed_entry(repo, "See utils.py for the shared helper")
        hits = guard_engine.decisions_for_files(str(repo), ["a/utils.py"])
        assert hits == []


# ── anchor_candidates_for_backfill (Task 1 of #175) ───────────────────────────

class TestTemporalAuthority:
    """temporal_authority + the commit_window tagging on decisions_for_files.

    The failure class this pins (24-PR falsification test, 2026-08-27, and the
    applicability benchmark in benchmarks/applicability/): a decision captured
    DURING the PR it supposedly governs — or after its merge — read back as
    prior authority. 5 of 24 real PRs hit it; filtering to 'prior' removed 7
    false pairings at zero true-positive cost on the same benchmark."""

    W_START = "2026-08-08T10:00:00+00:00"
    W_END = "2026-08-08T20:00:00+00:00"

    def _entry(self, ts, rev_ts=None):
        revs = [{"created_at": rev_ts}] if rev_ts else []
        return {"timestamp": ts, "revisions": revs}

    def test_prior(self):
        e = self._entry("2026-08-01T12:00:00+00:00")
        assert guard_engine.temporal_authority(e, self.W_START, self.W_END) == "prior"

    def test_concurrent_self_capture(self):
        e = self._entry("2026-08-08T15:00:00+00:00")
        assert guard_engine.temporal_authority(e, self.W_START, self.W_END) == "concurrent"

    def test_retroactive(self):
        # the #122 shape: successor decision captured ~1min after the merge it names
        e = self._entry("2026-08-08T20:01:00+00:00")
        assert guard_engine.temporal_authority(e, self.W_START, self.W_END) == "retroactive"

    def test_offsets_compared_as_instants_not_strings(self):
        # window end 21:58+02:00 == 19:58 UTC; a 20:04+00:00 capture is AFTER it.
        # A string comparison says "20:04" < "21:58" -> wrongly concurrent.
        e = self._entry("2026-08-08T20:04:00+00:00")
        assert guard_engine.temporal_authority(
            e, "2026-08-08T12:00:00+02:00", "2026-08-08T21:58:00+02:00") == "retroactive"

    def test_earliest_revision_wins_over_entry_timestamp(self):
        # rewritten entry.timestamp must not launder an old decision into the window
        e = self._entry("2026-08-08T15:00:00+00:00", rev_ts="2026-08-01T09:00:00+00:00")
        assert guard_engine.temporal_authority(e, self.W_START, self.W_END) == "prior"

    def test_no_timestamps_degrades_to_prior(self):
        # strip mutation: bookkeeping damage must not silently demote a decision —
        # 'prior' is the pre-commit_window behavior for every entry.
        assert guard_engine.temporal_authority({}, self.W_START, self.W_END) == "prior"
        assert guard_engine.temporal_authority(
            {"timestamp": "not-a-date", "revisions": []}, self.W_START, self.W_END) == "prior"

    def test_default_call_shape_unchanged(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        hits = guard_engine.decisions_for_files(str(repo), ["auth/jwt.py"])
        assert len(hits) == 1
        assert "authority" not in hits[0]

    def test_commit_window_tags_hits(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        # seeded entry's timestamp is "now"; a window ending before it -> retroactive,
        # a window opening after it would be prior. Use a past window: retroactive.
        hits = guard_engine.decisions_for_files(
            str(repo), ["auth/jwt.py"],
            commit_window=("2020-01-01T00:00:00+00:00", "2020-01-02T00:00:00+00:00"))
        assert len(hits) == 1
        assert hits[0]["authority"] == "retroactive"
        # and a window that hasn't closed yet -> the capture is inside it: concurrent
        hits = guard_engine.decisions_for_files(
            str(repo), ["auth/jwt.py"],
            commit_window=("2020-01-01T00:00:00+00:00", "2099-01-01T00:00:00+00:00"))
        assert hits[0]["authority"] == "concurrent"


class TestRankApplicable:
    """rank_applicable: the tiered BM25-over-change + mechanical union.

    Pins the two rules the 24-PR oracle earned (docs/internal/
    applicability-redteam-2026-08-28.md section 7): a mechanical anchor hit is
    NEVER discarded by ranking (4 of 16 real hits ranked 22-74), and the strong
    tier is led by BM25 with prior-authority required when a window is given."""

    def test_bm25_reaches_decision_with_no_file_signal(self, repo):
        _seed_entry(repo, "Serve argon2id password hashing from the login service "
                          "because bcrypt truncates at 72 bytes")
        tiers = guard_engine.rank_applicable(
            str(repo), [], "switch signup flow to argon2id hashing")
        assert [h["reason"] for h in tiers["strong"]] == ["bm25"]
        assert tiers["strong"][0]["bm25_rank"] == 1
        assert tiers["strong"][0]["files_matched"] == []

    def test_mechanical_hit_with_zero_term_overlap_survives_in_candidates(self, repo):
        anchored = _seed_entry(repo, "Decided to use JWT for auth",
                               source_files=["auth/jwt.py"])
        _seed_entry(repo, "Serve argon2id password hashing from the login service")
        tiers = guard_engine.rank_applicable(
            str(repo), ["auth/jwt.py"], "switch signup flow to argon2id hashing")
        everything = tiers["strong"] + tiers["candidates"]
        kept = [h for h in everything if h["decision_id"] == anchored["id"]]
        assert kept and kept[0]["reason"] == "source_files match"
        assert "bm25_rank" not in kept[0]          # zero overlap: rank-less, sorted last
        assert everything[-1]["decision_id"] == anchored["id"]

    def test_strong_caps_at_three_and_drops_nothing(self, repo):
        ids = {_seed_entry(repo, f"Rely on argon2id hashing variant number "
                                 f"{'unique' * (i + 1)}")["id"] for i in range(5)}
        tiers = guard_engine.rank_applicable(str(repo), [], "argon2id hashing rollout")
        assert len(tiers["strong"]) == 3
        surfaced = {h["decision_id"] for h in tiers["strong"] + tiers["candidates"]}
        assert ids <= surfaced                     # rank, never filter

    def test_empty_change_text_degrades_to_mechanical_only(self, repo):
        _seed_entry(repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        tiers = guard_engine.rank_applicable(str(repo), ["auth/jwt.py"], "")
        assert tiers["strong"] == []
        assert [h["reason"] for h in tiers["candidates"]] == ["source_files match"]

    def test_ignored_decision_invisible_to_both_lanes(self, repo):
        _seed_entry(repo, "Serve argon2id password hashing from the login service",
                    status="ignored", source_files=["auth/hash.py"])
        tiers = guard_engine.rank_applicable(
            str(repo), ["auth/hash.py"], "switch signup flow to argon2id hashing")
        assert tiers == {"strong": [], "candidates": []}

    def test_non_prior_capture_never_leads_strong(self, repo):
        # seeded entry's timestamp is "now"; a past window makes it retroactive.
        _seed_entry(repo, "Serve argon2id password hashing from the login service")
        tiers = guard_engine.rank_applicable(
            str(repo), [], "switch signup flow to argon2id hashing",
            commit_window=("2020-01-01T00:00:00+00:00", "2020-01-02T00:00:00+00:00"))
        assert tiers["strong"] == []
        assert [h["authority"] for h in tiers["candidates"]] == ["retroactive"]

    def test_dual_lane_hit_carries_both_signals(self, repo):
        _seed_entry(repo, "Serve argon2id password hashing from auth/hash.py",
                    source_files=["auth/hash.py"])
        tiers = guard_engine.rank_applicable(
            str(repo), ["auth/hash.py"], "switch signup flow to argon2id hashing")
        assert tiers["strong"][0]["reason"] == "source_files match"
        assert tiers["strong"][0]["files_matched"] == ["auth/hash.py"]
        assert tiers["strong"][0]["bm25_rank"] == 1

    def test_fail_soft_returns_empty_tiers(self, repo, monkeypatch):
        _seed_entry(repo, "Serve argon2id password hashing from the login service")
        monkeypatch.setattr("contexer.retrieval.bm25_rank",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        tiers = guard_engine.rank_applicable(str(repo), [], "argon2id hashing")
        assert tiers == {"strong": [], "candidates": []}

    @staticmethod
    def _entry_at(content, ts):
        e = store._new_decision_entry(content, "s", "architecture",
                                      created_by="human", status="approved")
        e["timestamp"] = ts
        for r in e["revisions"]:
            r["created_at"] = ts
        return e

    def test_skipped_non_prior_rank_is_never_backfilled_from_deeper_ranks(self):
        # Review F1 repro: top ranks all non-prior + one weakly-matching prior doc
        # deeper down. The prior doc must NOT be promoted into strong on the strength
        # of one shared token — strong runs under-full and the doc stays a candidate.
        window = ("2020-06-01T00:00:00+00:00", "2020-06-02T00:00:00+00:00")
        entries = [self._entry_at(
            f"Rely on argon2id hashing for credentials {'filler' * i}",
            "2020-06-01T12:00:00+00:00") for i in range(3)]          # concurrent
        weak_prior = self._entry_at(
            "Ship the billing ledger export nightly with argon2id nowhere near it",
            "2019-01-01T00:00:00+00:00")                              # prior, weak match
        tiers = guard_engine.rank_applicable(
            "/nonexistent", [], "argon2id hashing rollout",
            decisions=entries + [weak_prior], commit_window=window)
        assert tiers["strong"] == []
        cand_ids = [h["decision_id"] for h in tiers["candidates"]]
        assert weak_prior["id"] in cand_ids

    def test_duplicate_decision_id_across_hits_yields_one_row_first_hit_wins(self):
        # Review F2 repro: one id emitting two mechanical hits (repo+global share an
        # id, e.g. the memory-sync sentinel). Pre-fix this produced duplicate rows
        # both cloned from the LAST hit, erasing the first hit's files_matched.
        first = self._entry_at("Decided to use JWT for auth", "2019-01-01T00:00:00+00:00")
        second = dict(self._entry_at("Decided to use JWT for auth elsewhere",
                                     "2019-01-01T00:00:00+00:00"), id=first["id"])
        first["source_files"] = ["auth/jwt.py"]
        second["source_files"] = ["auth/other.py"]
        tiers = guard_engine.rank_applicable(
            "/nonexistent", ["auth/jwt.py", "auth/other.py"], "",
            decisions=[first, second])
        rows = [h for h in tiers["candidates"] if h["decision_id"] == first["id"]]
        assert len(rows) == 1
        assert rows[0]["files_matched"] == ["auth/jwt.py"]


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
        # _artifact_path_spellings does not treat the two as exclusive), now
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
        assert len(result[0]["candidates"]) == store.MAX_SOURCE_FILES

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
        entry = store.load(str(repo))["entries"][0]
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
        loaded = next(e for e in store.load(str(repo))["entries"] if e["id"] == entry["id"])
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
        real_save = store.save

        def _counting_save(repo_path, data):
            calls.append(1)
            real_save(repo_path, data)

        monkeypatch.setattr(store, "save", _counting_save)
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
            next(e for e in store.load(str(repo))["entries"]
                 if e["id"] == already_anchored["id"]))
        fresh = _seed_entry(repo, "See auth/oauth.py for the OAuth decision")

        count = store.apply_backfill_anchors(
            str(repo),
            {already_anchored["id"]: ["auth/oauth.py"],  # attempted re-anchor, must be ignored
             fresh["id"]: ["auth/oauth.py"]})

        assert count == 1  # only the fresh decision counts as newly anchored
        after_entries = {e["id"]: e for e in store.load(str(repo))["entries"]}
        assert after_entries[already_anchored["id"]] == before
        assert after_entries[fresh["id"]]["source_files"] == ["auth/oauth.py"]
        assert after_entries[fresh["id"]]["anchor_commit"]


# ── end-to-end: backfilled decision pairs in guard_staged ────────────────────

class TestAnchorBackfillEndToEnd:
    def test_backfilled_decision_pairs_when_file_staged(self, repo):
        """What backfill actually buys, stated discriminatingly: every backfill candidate is
        mined from the decision's own content by the SAME extraction _guard_pairs uses, so
        the decision ALREADY pairs before backfill — via `path artifact ...`. Backfill turns
        that into a real `source_files` anchor: the pairing reason firms up to `source_files
        match`, and (the real prize) the entry gains the anchor_commit that _staleness_note
        needs. It does NOT add new Tier-1 advisories."""
        _write(repo, "auth/jwt.py", "token = 0\n")
        _git(repo, "add", "auth/jwt.py")
        _commit(repo, "init")

        entry = _seed_entry(repo, "See auth/jwt.py for the JWT-based session auth decision")

        # BEFORE: already an advisory, on the content-artifact signal alone.
        _write(repo, "auth/jwt.py", "token = 1  # rotated\n")
        _git(repo, "add", "auth/jwt.py")
        before = guard_engine.guard_staged(str(repo))
        assert len(before["advisories"]) == 1
        assert before["advisories"][0]["decision_id"] == entry["id"]
        assert before["advisories"][0]["reason"].startswith("path artifact")
        assert "source_files" not in entry and "anchor_commit" not in entry

        candidates = guard_engine.anchor_candidates_for_backfill(str(repo))
        assert len(candidates) == 1
        assert candidates[0]["decision_id"] == entry["id"]
        assert candidates[0]["candidates"] == ["auth/jwt.py"]

        applied = store.apply_backfill_anchors(
            str(repo), {entry["id"]: candidates[0]["candidates"]})
        assert applied == 1

        # AFTER: the SAME single advisory, now on the firmer signal — plus the anchor
        # (source_files + anchor_commit) staleness tracking requires. The staged content is
        # changed first: the throttle is content-keyed, so re-running against the identical
        # blob would surface nothing regardless of the anchor.
        _write(repo, "auth/jwt.py", "token = 2  # rotated again\n")
        _git(repo, "add", "auth/jwt.py")
        result = guard_engine.guard_staged(str(repo))
        assert len(result["advisories"]) == 1
        assert result["advisories"][0]["decision_id"] == entry["id"]
        assert result["advisories"][0]["reason"] == "source_files match"
        anchored = store.entry_by_id(store.load(str(repo))["entries"], entry["id"])
        assert anchored["source_files"] == ["auth/jwt.py"]
        assert anchored["anchor_commit"]

        # A backfilled decision no longer surfaces as a further backfill candidate.
        assert guard_engine.anchor_candidates_for_backfill(str(repo)) == []


# ── Task 3: arm_guard / disarm_guard (management path) ───────────────────────

class TestArmGuard:
    def test_arm_regex_success(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        msg = guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern=r"TODO",
                               message="no TODOs allowed")
        assert isinstance(msg, str) and msg
        data = store.load(str(repo))
        stored = store.entry_by_id(data["entries"], entry["id"])
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
        data = store.load(str(repo))
        stored = store.entry_by_id(data["entries"], entry["id"])
        assert stored["guard_check"]["flags"] == "i"

    def test_arm_secret_success(self, repo):
        entry = _seed_entry(repo, "Never commit secrets")
        guard_engine.arm_guard(str(repo), entry["id"], "secret")
        data = store.load(str(repo))
        stored = store.entry_by_id(data["entries"], entry["id"])
        assert stored["guard_check"]["type"] == "secret"
        assert stored["guard_check"]["pattern"] == ""

    def test_arm_honors_paths_glob(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO", paths="*.py")
        data = store.load(str(repo))
        stored = store.entry_by_id(data["entries"], entry["id"])
        assert stored["guard_check"]["paths"] == "*.py"

    def test_arm_short_id_resolution(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"][:8], "regex", pattern="TODO")
        data = store.load(str(repo))
        stored = store.entry_by_id(data["entries"], entry["id"])
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
        data = store.load_global()
        stored = store.entry_by_id(data["entries"], entry["id"])
        assert stored.get("guard_check")

    def test_arm_repo_entry_preferred_over_global_when_id_collides(self, repo):
        # Extremely unlikely in production (real UUIDs), but pins the documented
        # resolution order: repo store is tried before the global store.
        entry = _seed_entry(repo, "Repo-scoped decision")
        global_data = store.load_global()
        clashing = store._new_decision_entry("Global-scoped decision", "s", "architecture",
                                              created_by="human", status="approved")
        clashing["id"] = entry["id"]
        global_data["entries"].append(clashing)
        store.save_global(global_data)

        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")
        repo_entry = store.entry_by_id(store.load(str(repo))["entries"], entry["id"])
        global_entry = store.entry_by_id(store.load_global()["entries"], entry["id"])
        assert repo_entry.get("guard_check")
        assert not global_entry.get("guard_check")


class TestDisarmGuard:
    def test_disarm_removes_guard_check(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")
        guard_engine.disarm_guard(str(repo), entry["id"])
        data = store.load(str(repo))
        stored = store.entry_by_id(data["entries"], entry["id"])
        assert "guard_check" not in stored

    def test_disarm_global_entry(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers globally", global_store=True)
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")
        guard_engine.disarm_guard(str(repo), entry["id"])
        data = store.load_global()
        stored = store.entry_by_id(data["entries"], entry["id"])
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
        data = store.load(str(repo))
        rules = guard_engine._armed_rules(data["entries"])
        assert [r["id"] for r in rules] == [entry["id"]]

    def test_unarmed_entry_excluded(self, repo):
        _seed_entry(repo, "Never commit TODO markers")
        data = store.load(str(repo))
        assert guard_engine._armed_rules(data["entries"]) == []

    def test_ignored_after_arming_stops_firing_without_disarm(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")
        ok, msg = store.approve_decision(str(repo), entry["id"], "ignore")
        assert ok, msg

        data = store.load(str(repo))
        stored = store.entry_by_id(data["entries"], entry["id"])
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

        data = store.load(str(repo))
        data["entries"] = [
            store._new_decision_entry(_content(i), "perf-session", "architecture",
                                       created_by="human", status="approved")
            for i in range(500)
        ]
        for i, entry in enumerate(data["entries"]):
            entry["source_files"] = [f"other{i}/thing{i}.py"]
        store.save(str(repo), data)
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
        data = store.load(str(repo))
        stored = store.entry_by_id(data["entries"], entry["id"])
        stored["guard_check"] = {"type": "regex", "pattern": "(unclosed", "flags": "",
                                  "paths": "", "message": "", "armed_at": "t"}
        store.save(str(repo), data)
        _write(repo, "a.py", "# TODO fix this\n")
        _git(repo, "add", "a.py")
        result = guard_engine.guard_staged(str(repo))
        assert result["violations"] == []
        assert "error" not in result


# ── Unreadable staged content is reported, never a silent pass ─────────────────

class TestUncheckedIsReported:
    """A staged file an armed rule could not be run against must be REPORTED, not
    skipped in silence. Before this, `_staged_content` returned a bare "" and
    `_guard_violations` did `if not content: continue`, so an over-cap file passed
    as a clean result: on this repo that silently exempted contexer/store.py (376KB)
    and tests/test_store.py (390KB) from every armed rule, including `--check secret`."""

    def _armed(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")
        return entry

    def test_over_cap_file_is_reported_not_silently_passed(self, repo, monkeypatch):
        self._armed(repo)
        monkeypatch.setattr(guard_engine, "_GUARD_MAX_FILE_BYTES", 32)
        _write(repo, "big.py", "# TODO fix this\n" + "x" * 64)
        _git(repo, "add", "big.py")

        result = guard_engine.guard_staged(str(repo))
        # The rule genuinely could not see the TODO, which is the honest outcome of a
        # cap; what must NOT happen is that outcome being indistinguishable from clean.
        assert result["violations"] == []
        assert result["unchecked"] == [{"file": "big.py", "reason": "too-large"}]

    def test_clean_run_omits_the_key_entirely(self, repo):
        self._armed(repo)
        _write(repo, "a.py", "fine\n")
        _git(repo, "add", "a.py")

        result = guard_engine.guard_staged(str(repo))
        # Present only when there is something to report, like total_advisories, so a
        # clean run's dict shape is exactly what it was before this existed.
        assert "unchecked" not in result

    def test_binary_is_not_reported(self, repo):
        self._armed(repo)
        _write(repo, "bin.dat", b"\x00\x01\x02binary")
        _git(repo, "add", "bin.dat")

        result = guard_engine.guard_staged(str(repo))
        # Skipping binary is correct, not a gap: a regex over encoded bytes is
        # meaningless, so reporting it would nag on every commit that stages an image.
        assert "unchecked" not in result

    def test_no_armed_rule_means_nothing_went_unchecked(self, repo, monkeypatch):
        monkeypatch.setattr(guard_engine, "_GUARD_MAX_FILE_BYTES", 32)
        _write(repo, "big.py", "x" * 64)
        _git(repo, "add", "big.py")

        result = guard_engine.guard_staged(str(repo))
        # Accurate rather than a gap: with no rule armed there was no check to skip.
        assert "unchecked" not in result

    def test_a_file_no_armed_rule_selects_is_not_reported(self, repo, monkeypatch):
        """A rule scoped with --paths would never have been run against this file, so
        calling it "not checked" invents a gap and nags on every commit."""
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO",
                                paths="src/*.py")
        monkeypatch.setattr(guard_engine, "_GUARD_MAX_FILE_BYTES", 32)
        _write(repo, "data.json", "x" * 64)
        _git(repo, "add", "data.json")

        assert "unchecked" not in guard_engine.guard_staged(str(repo))

    def test_a_file_the_glob_does_select_is_still_reported(self, repo, monkeypatch):
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO",
                                paths="src/*.py")
        monkeypatch.setattr(guard_engine, "_GUARD_MAX_FILE_BYTES", 32)
        _write(repo, "src/big.py", "x" * 64)
        _git(repo, "add", "src/big.py")

        assert guard_engine.guard_staged(str(repo))["unchecked"] == \
            [{"file": "src/big.py", "reason": "too-large"}]

    def test_scan_budget_exhaustion_is_reported_not_left_to_the_deadline(
            self, repo, monkeypatch):
        """The wall-clock deadline fails OPEN by discarding every violation found so far,
        so a byte budget must stop first and NAME what it did not reach. Otherwise a
        commit of many large-but-textual files loses the violations from its small ones
        too, and reports only "internal error"."""
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")
        # A reserve larger than the whole time budget puts the soft cut-off in the past,
        # so everything after the first selected file is out of time.
        monkeypatch.setattr(guard_engine, "_GUARD_SCAN_RESERVE", 10_000)
        _write(repo, "a.py", "# TODO one\n")
        _write(repo, "b.py", "# TODO two\n")
        _git(repo, "add", "a.py", "b.py")

        result = guard_engine.guard_staged(str(repo))
        # The first file always scans, so the run makes forward progress...
        assert [v["path"] for v in result["violations"]] == ["a.py"]
        # ...and the one it could not afford is reported rather than passing as clean.
        assert result["unchecked"] == [{"file": "b.py", "reason": "budget"}]
        assert "error" not in result

    def test_files_no_rule_selects_do_not_consume_the_scan_budget(self, repo, monkeypatch):
        """Security: a path-scoped rule plus a pile of out-of-scope staged files must not
        starve the file the rule DOES cover. Charging the budget for a file that is then
        scanned against nothing let unrelated bulk (a data dump, a lockfile) push the one
        selected file past the budget, where it was skipped with only a non-blocking
        notice and its violation shipped."""
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO",
                                paths="src/*.py")
        monkeypatch.setattr(guard_engine, "_GUARD_SCAN_RESERVE", 10_000)
        # `data.json` sorts before `src/app.py`, so it is seen first and would have eaten
        # the budget under the old accounting, leaving the covered file unscanned.
        _write(repo, "data.json", "x" * 64)
        _write(repo, "src/app.py", "# TODO fix this\n")
        _git(repo, "add", "data.json", "src/app.py")

        result = guard_engine.guard_staged(str(repo))
        assert [v["path"] for v in result["violations"]] == ["src/app.py"]
        assert "unchecked" not in result

    def test_a_file_no_rule_selects_is_never_read(self, repo, monkeypatch):
        """The same rule, as an efficiency property: `git show` is not spent on a file no
        armed rule would be run against."""
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO",
                                paths="src/*.py")
        _write(repo, "data.json", "{}\n")
        _write(repo, "src/app.py", "clean\n")
        _git(repo, "add", "data.json", "src/app.py")

        seen = []
        real = guard_engine._staged_content

        def spy(repo_arg, path, *a, **k):
            seen.append(path)
            return real(repo_arg, path, *a, **k)
        monkeypatch.setattr(guard_engine, "_staged_content", spy)

        guard_engine._guard_violations(str(repo), ["data.json", "src/app.py"],
                                        deadline=time.time() + 30)
        assert seen == ["src/app.py"]

    def test_several_MB_of_selected_text_is_still_fully_scanned(self, repo):
        """Regression for the coverage band a fixed 4MB byte budget silently gave up. The
        cut-off is now the real resource (time), so 4.5MB of selected text, well past the
        old byte cap and nowhere near the 2s deadline, is scanned to the last file."""
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")
        for i in range(5):
            _write(repo, f"f{i}.py", "x" * 900_000)
        # The violation is in the LAST file, so only a run that got all the way there
        # can find it.
        _write(repo, "f9_last.py", "# TODO fix this\n")
        _git(repo, "add", "-A")

        result = guard_engine.guard_staged(str(repo))
        assert [v["path"] for v in result["violations"]] == ["f9_last.py"]
        assert "unchecked" not in result

    def test_running_out_of_time_does_not_block_the_commit(self, repo, monkeypatch):
        """Deliberate policy, pinned so it stays a choice rather than an accident: a
        selected file the guard ran out of time for is REPORTED, not blocked on. Blocking
        would contradict the ratified "the run path never blocks a commit on its own
        failure" invariant, since running out of budget is the guard's own limitation and
        not a rule violation. The trade-off is real and belongs in the open: a violation
        in a file that went unscanned is not caught, which is why the file is named."""
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO")
        monkeypatch.setattr(guard_engine, "_GUARD_SCAN_RESERVE", 10_000)
        _write(repo, "a_clean.py", "nothing here\n")
        _write(repo, "z_dirty.py", "# TODO fix this\n")
        _git(repo, "add", "a_clean.py", "z_dirty.py")

        result = guard_engine.guard_staged(str(repo))
        assert result["violations"] == []
        assert result["unchecked"] == [{"file": "z_dirty.py", "reason": "budget"}]
        # No error flag: this is a reported gap, not a guard malfunction, so the CLI keeps
        # the violations it did find rather than degrading to "internal error".
        assert "error" not in result

    def test_a_readable_file_alongside_an_over_cap_one_still_blocks(self, repo, monkeypatch):
        self._armed(repo)
        monkeypatch.setattr(guard_engine, "_GUARD_MAX_FILE_BYTES", 32)
        _write(repo, "big.py", "x" * 64)
        _write(repo, "small.py", "# TODO fix this\n")
        _git(repo, "add", "big.py", "small.py")

        result = guard_engine.guard_staged(str(repo))
        # One unreadable file must not cost the whole run: the file that COULD be read
        # is still checked, and its violation still reported.
        assert [v["path"] for v in result["violations"]] == ["small.py"]
        assert result["unchecked"] == [{"file": "big.py", "reason": "too-large"}]


class TestThrottleDoesNotFreezeOnUnreadableContent:
    """Tier-1's throttle re-advises a pair once the file's staged content changes, by
    comparing a stored fingerprint. Two opposite failures are pinned here. Hashing
    `_staged_content`'s old bare "" meant an unreadable file hashed to the empty-string
    sha1 every time, so the stamp always matched and the pair was suppressed FOREVER.
    Withholding a fingerprint from every non-None reason is the mirror-image bug: an
    over-cap or binary file could then never be throttled or stamped, so it re-advised on
    every commit and, since the surfaced list is capped, could crowd out fresh advisories.
    The line is drawn at what git actually READ, not at what the scanner accepted."""

    def _unreadable(self, monkeypatch):
        monkeypatch.setattr(guard_engine, "_staged_content",
                            lambda *_a, **_k: ("", "unreadable", None))

    def test_over_cap_pair_still_throttles_when_unchanged(self, repo, monkeypatch):
        _seed_entry(repo, "Keep the store loader fail-soft", source_files=["big.py"])
        monkeypatch.setattr(guard_engine, "_GUARD_MAX_FILE_BYTES", 32)
        _write(repo, "big.py", "x" * 64)
        _git(repo, "add", "big.py")

        assert len(guard_engine.guard_staged(str(repo))["advisories"]) == 1
        assert guard_engine.guard_staged(str(repo))["advisories"] == [], \
            "an over-cap file was still read, so unchanged content must throttle"

    def test_over_cap_pair_re_advises_when_the_file_changes(self, repo, monkeypatch):
        _seed_entry(repo, "Keep the store loader fail-soft", source_files=["big.py"])
        monkeypatch.setattr(guard_engine, "_GUARD_MAX_FILE_BYTES", 32)
        _write(repo, "big.py", "x" * 64)
        _git(repo, "add", "big.py")
        assert len(guard_engine.guard_staged(str(repo))["advisories"]) == 1

        _write(repo, "big.py", "y" * 64)
        _git(repo, "add", "big.py")
        assert len(guard_engine.guard_staged(str(repo))["advisories"]) == 1

    def test_unreadable_pair_re_advises_instead_of_freezing(self, repo, monkeypatch):
        _seed_entry(repo, "Keep the store loader fail-soft", source_files=["a.py"])
        _write(repo, "a.py", "x\n")
        _git(repo, "add", "a.py")
        self._unreadable(monkeypatch)

        assert len(guard_engine.guard_staged(str(repo))["advisories"]) == 1
        assert len(guard_engine.guard_staged(str(repo))["advisories"]) == 1, \
            "unproven sameness must surface, not suppress"

    def test_no_throttle_stamp_is_written_for_unreadable_content(self, repo, monkeypatch):
        _seed_entry(repo, "Keep the store loader fail-soft", source_files=["a.py"])
        _write(repo, "a.py", "x\n")
        _git(repo, "add", "a.py")
        self._unreadable(monkeypatch)

        guard_engine.guard_staged(str(repo))
        # The stamp asserts "we advised on exactly this content"; with nothing read there
        # is no content to say that about, and writing one created the freeze.
        assert guard_engine._guard_advised(str(repo)) == {}

    def test_over_cap_content_IS_stamped(self, repo, monkeypatch):
        _seed_entry(repo, "Keep the store loader fail-soft", source_files=["big.py"])
        monkeypatch.setattr(guard_engine, "_GUARD_MAX_FILE_BYTES", 32)
        _write(repo, "big.py", "x" * 64)
        _git(repo, "add", "big.py")

        guard_engine.guard_staged(str(repo))
        assert guard_engine._guard_advised(str(repo)) != {}, \
            "a file git read has a comparable identity, so its pair must be stampable"

    def test_readable_content_still_throttles(self, repo):
        _seed_entry(repo, "Keep the store loader fail-soft", source_files=["a.py"])
        _write(repo, "a.py", "x\n")
        _git(repo, "add", "a.py")

        assert len(guard_engine.guard_staged(str(repo))["advisories"]) == 1
        # Unchanged content still suppresses: the fix must not have disabled throttling.
        assert guard_engine.guard_staged(str(repo))["advisories"] == []

    def test_fingerprint_matches_the_pre_change_scheme_for_utf8_content(self, git_repo):
        """On-disk stamps survive the switch from hashing decoded text to hashing raw
        bytes: for valid UTF-8 the two digests are identical, so only a file whose bytes
        are not valid UTF-8 gets one benign re-advise rather than the whole corpus."""
        import hashlib
        _write(git_repo, "f.py", "token = 1\n")
        _git(git_repo, "add", "f.py")

        text, _reason, fingerprint = guard_engine._staged_content(str(git_repo), "f.py")
        assert fingerprint == hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()


# ── Task 3: wire-safety regression ────────────────────────────────────────────

class TestWireSafety:
    def test_share_projection_never_leaks_guard_check(self, repo):
        entry = _seed_entry(repo, "Never commit TODO markers")
        entry["guard_check"] = {"type": "regex", "pattern": "TODO", "flags": "",
                                 "paths": "", "message": "no TODOs", "armed_at": "t"}
        projected = store._share_projection(entry, redact_on=False)
        assert "guard_check" not in projected

    def test_share_projection_source_files_present_but_guard_and_anchor_never_egress(self, repo):
        # issue #174 Task 5: source_files becomes a deliberate projection field, but that must
        # not loosen the whitelist — guard_check/anchor_candidates/anchor_commit still never
        # appear, even on an entry that carries all of them at once.
        entry = _seed_entry(repo, "Use JWT tokens for session auth",
                            source_files=["auth/jwt.py"])
        entry["guard_check"] = {"type": "regex", "pattern": "TODO", "flags": "",
                                 "paths": "", "message": "no TODOs", "armed_at": "t"}
        entry["anchor_candidates"] = ["other/file.py"]
        entry["anchor_commit"] = "deadbeef"
        projected = store._share_projection(entry, redact_on=False)
        assert projected["source_files"] == ["auth/jwt.py"]
        assert "guard_check" not in projected
        assert "anchor_candidates" not in projected
        assert "anchor_commit" not in projected


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
