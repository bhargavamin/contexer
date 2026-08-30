"""Tests for worktree store-key canonicalization (canonical_store_key) and the
pre-fix stray-store migration (migrate_worktree_strays).

All linked worktrees of a repo must share the main worktree's store file; submodules,
separate-git-dir layouts, main repos, and non-git dirs must keep their own keys with
zero subprocess cost.
"""
import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from contexer import store


def _git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _make_repo(base: Path) -> str:
    base.mkdir(parents=True, exist_ok=True)
    _git("init", cwd=base)
    _git("config", "user.email", "test@example.com", cwd=base)
    _git("config", "user.name", "Test", cwd=base)
    (base / "f.txt").write_text("x")
    _git("add", ".", cwd=base)
    _git("commit", "-m", "init", cwd=base)
    return str(base)


@pytest.fixture(autouse=True)
def _fresh_canon_cache():
    """Isolate the module-level canonicalization cache between tests."""
    store._CANON_CACHE.clear()
    yield
    store._CANON_CACHE.clear()


@pytest.fixture
def store_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    return tmp_path / ".contexer"


@pytest.fixture
def wt_repo(tmp_path):
    """(main_path, worktree_path) — a real repo with one linked worktree.

    Paths are realpath'd: on macOS tmp dirs live under a /var → /private/var symlink
    and git reports resolved paths, so the fixture must hand out what git will say."""
    root = Path(os.path.realpath(tmp_path))
    main = _make_repo(root / "main")
    wt = str(root / "wt")
    _git("worktree", "add", wt, cwd=main)
    return main, wt


class TestCanonicalStoreKey:
    def test_worktree_collapses_to_main(self, wt_repo, store_dir):
        main, wt = wt_repo
        assert store.canonical_store_key(wt) == main
        assert store.repo_slug(wt) == store.repo_slug(main)
        assert store._legacy_slug(wt) == store._legacy_slug(main)
        assert store._store_path(wt) == store._store_path(main)
        # Same slug → same lock sidecar name → one cross-process critical section.
        assert (store.STORE_DIR / f"{store.repo_slug(wt)}.lock") == \
               (store.STORE_DIR / f"{store.repo_slug(main)}.lock")

    def test_global_slug_guard_with_cwd_in_worktree(self, wt_repo, monkeypatch):
        # _slug("") is used for global-store contexts; without the empty-path guard,
        # os.path.join("", ".git") stats .git relative to CWD and the GLOBAL key
        # would collapse into the repo store whenever cwd is a worktree.
        _main, wt = wt_repo
        monkeypatch.chdir(wt)
        assert store.canonical_store_key("") == ""
        expected = f"-{hashlib.sha1(b'').hexdigest()[:8]}"
        assert store.repo_slug("") == expected

    def test_submodule_gitfile_no_collapse_no_subprocess(self, tmp_path, monkeypatch):
        d = tmp_path / "sub"
        d.mkdir()
        (d / ".git").write_text("gitdir: /some/where/.git/modules/x\n")
        calls = []
        monkeypatch.setattr(store.subprocess, "run", lambda *a, **k: calls.append(a))
        assert store.canonical_store_key(str(d)) == str(d)
        assert calls == []

    def test_separate_git_dir_no_collapse(self, tmp_path, monkeypatch):
        root = Path(os.path.realpath(tmp_path))
        repo = root / "repo"
        backup_git = root / "backup" / ".git"
        repo.mkdir()
        (root / "backup").mkdir()
        subprocess.run(
            ["git", "init", f"--separate-git-dir={backup_git}", str(repo)],
            check=True, capture_output=True, text=True,
        )
        calls = []
        monkeypatch.setattr(store.subprocess, "run", lambda *a, **k: calls.append(a))
        # gitdir is .../backup/.git (no /worktrees/) — must NOT mis-key to <tmp>/backup.
        assert store.canonical_store_key(str(repo)) == str(repo)
        assert calls == []

    def test_main_repo_and_non_git_dir_unchanged_no_subprocess(self, tmp_path, monkeypatch):
        root = Path(os.path.realpath(tmp_path))
        main = _make_repo(root / "main")
        plain = root / "plain"
        plain.mkdir()
        calls = []
        monkeypatch.setattr(store.subprocess, "run", lambda *a, **k: calls.append(a))
        assert store.canonical_store_key(main) == main            # .git is a directory
        assert store.canonical_store_key(str(plain)) == str(plain)  # no .git at all
        assert calls == []

    def test_failure_is_transparent_and_not_cached(self, wt_repo):
        main, wt = wt_repo
        gitfile = Path(wt) / ".git"
        gitdir = Path(gitfile.read_text().split(":", 1)[1].strip())
        commondir = gitdir / "commondir"
        original = commondir.read_text()
        # Transient/corrupt worktree metadata → uncollapsed, fail-soft behavior.
        commondir.write_text("")
        assert store.canonical_store_key(wt) == wt
        # Failure must not be cached: same call now succeeds and collapses.
        commondir.write_text(original)
        assert store.canonical_store_key(wt) == main

    def test_worktree_derivation_never_spawns_subprocess(self, wt_repo, monkeypatch):
        main, wt = wt_repo
        monkeypatch.setattr(store.subprocess, "run",
                            lambda *a, **k: pytest.fail("canonicalization spawned subprocess"))
        assert store.canonical_store_key(wt) == main

    def test_insane_root_never_selected(self, wt_repo, monkeypatch):
        main, wt = wt_repo
        real = store.is_sane_repo
        monkeypatch.setattr(store, "is_sane_repo",
                            lambda p: False if p == main else real(p))
        assert store.canonical_store_key(wt) == wt

    def test_worktree_path_reuse_by_different_repo_not_stale_cached(self, tmp_path):
        # A worktree path removed and later reused by a DIFFERENT repo's worktree in
        # the same long-lived process must re-resolve — a stale cache hit here would
        # route the replacement repo into the former repo's store.
        root = Path(os.path.realpath(tmp_path))
        repo_a = _make_repo(root / "repo_a")
        repo_b = _make_repo(root / "repo_b")
        p = str(root / "shared_wt")
        _git("worktree", "add", p, cwd=repo_a)
        assert store.canonical_store_key(p) == repo_a
        _git("worktree", "remove", "--force", p, cwd=repo_a)
        _git("worktree", "add", p, cwd=repo_b)
        assert store.canonical_store_key(p) == repo_b

    def test_team_context_cache_path_collapses(self, wt_repo, store_dir):
        from contexer import team_context
        main, wt = wt_repo
        assert team_context._cache_path(wt) == team_context._cache_path(main)


class TestMigrateWorktreeStrays:
    MAIN_CONTENT = "decided to use JWT tokens instead of server sessions because stateless auth scales"
    UNIQUE_CONTENT = "Use Redis for caching hot lookups because sub-millisecond latency matters here"

    def _make_stray(self, tmp_path, wt: str, contents: list[str]) -> Path:
        """Build a realistic pre-fix stray store keyed under the worktree's raw slug."""
        fake = str(Path(os.path.realpath(tmp_path)) / "fake_source")
        Path(fake).mkdir(exist_ok=True)
        for i, content in enumerate(contents):
            store.update_decision(fake, content, f"stray-sess-{i}")
        stray = store.STORE_DIR / f"{store._raw_slug(wt)}.json"
        os.replace(store._store_path(fake), stray)
        return stray

    def test_merge_unique_skip_duplicate_rename_idempotent(self, wt_repo, store_dir, tmp_path):
        main, wt = wt_repo
        store.update_decision(main, self.MAIN_CONTENT, "main-sess")
        stray = self._make_stray(tmp_path, wt, [self.UNIQUE_CONTENT, self.MAIN_CONTENT])
        stray_entries = json.loads(stray.read_text())["entries"]
        unique_id = next(e["id"] for e in stray_entries
                         if e["content"].lower() == self.UNIQUE_CONTENT.lower())

        merged = store.migrate_worktree_strays(main)
        assert merged == 1

        data = json.loads(store._store_path(main).read_text())
        contents = [e["content"].lower() for e in data["entries"]]
        # Unique entry merged, id preserved.
        assert self.UNIQUE_CONTENT.lower() in contents
        assert unique_id in {e["id"] for e in data["entries"]}
        # Duplicate not double-stored, and no occurrence bump on the main twin.
        main_twins = [e for e in data["entries"]
                      if e["content"].lower() == self.MAIN_CONTENT.lower()]
        assert len(main_twins) == 1
        assert main_twins[0].get("occurrence_count", 1) == 1
        # Stray renamed, never deleted.
        assert not stray.exists()
        assert stray.with_suffix(".json.migrated").exists()
        # Idempotent re-run.
        assert store.migrate_worktree_strays(main) == 0
        data2 = json.loads(store._store_path(main).read_text())
        assert len(data2["entries"]) == len(data["entries"])

    def test_first_session_renders_migrated_context_not_bootstrap_offer(
            self, wt_repo, store_dir, tmp_path):
        # Migration must run BEFORE the session-start store read: the first
        # post-upgrade session over an empty canonical store renders the recovered
        # rules, not the "no context stored" bootstrap offer.
        main, wt = wt_repo
        rule = "Always run database migrations through alembic scripts never raw SQL"
        stray = self._make_stray(tmp_path, wt, [rule])
        data = json.loads(stray.read_text())
        for e in data["entries"]:
            e["status"] = "approved"
            e["subtype"] = "constraint"
        stray.write_text(json.dumps(data))
        assert not store._store_path(main).exists()  # canonical store starts empty

        payload = store.session_start_payload(main)

        assert "alembic" in payload["context"].lower()
        assert "no context stored" not in payload["status"]
        assert not stray.exists()
        assert stray.with_suffix(".json.migrated").exists()

    def test_prefix_collision_repo_untouched(self, tmp_path, store_dir):
        root = Path(os.path.realpath(tmp_path))
        proj = _make_repo(root / "proj")
        teams = _make_repo(root / "proj-teams")
        _git("worktree", "add", str(root / "proj-wt"), cwd=proj)

        store.update_decision(proj, self.MAIN_CONTENT, "s1")
        store.update_decision(teams, self.UNIQUE_CONTENT, "s2")
        teams_store = store._store_path(teams)
        before = teams_store.read_bytes()

        store.migrate_worktree_strays(proj)

        # The unrelated prefix-sharing repo's store is never read into proj, never
        # modified, and never renamed.
        assert teams_store.exists()
        assert teams_store.read_bytes() == before
        assert not teams_store.with_suffix(".json.migrated").exists()
        proj_data = json.loads(store._store_path(proj).read_text())
        assert self.UNIQUE_CONTENT.lower() not in [e["content"].lower()
                                                   for e in proj_data["entries"]]
