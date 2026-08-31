"""Tests for the contexer/console_api.py extraction: the module BOUNDARY, not the projections.

The projections themselves already have coverage, and it deliberately stayed where it was: the
14 test blocks that call these reads (test_global's TestUnreadableGlobalStore, test_store_delete's
TestUnreadableSidecar / TestSlugAddressing / TestPollCost, test_store's TestNonDictStoreRecovery /
TestConsoleSourceFiles, test_overlap_report, test_ui_api, test_ui_session_start) are each testing
store-level behaviour (sidecar corruption, slug addressing, poll cost) and merely OBSERVE it
through a console read. Moving them here would have separated a global-store corruption test from
the global-store test file. They also reach the reads as `store.<name>`, which is what keeps
store.py's `_CONSOLE_EXPORTS` facade exercised rather than an untested compatibility promise.

That is a statement about the TESTS only. The facade is back-compat, not the surface production
code uses: every module under contexer/ imports the owner, pinned mechanically below, because a
production caller drifting onto the facade breaks nothing visible and so would never be noticed.

What had no coverage at all is the seam this extraction created, so that is what these pin: the
facade resolves, it resolves to the OWNER's object, `dir()` matches what `__getattr__` answers,
the module imports first without a cycle, nothing was left behind as a second definition in
store.py, and store-owned helpers are looked up at CALL time (the whole reason the module object
is imported instead of `from`-imports).
"""
import ast
import pathlib
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

from contexer import console_api, store
from tests.seams import redirect_store_dir


# Every name that moved out of store.py, public and private. A name reappearing as a top-level
# def in store.py would be a SECOND definition of a projection the console reads through the
# facade, silently divergent, since the facade would keep answering with console_api's.
MOVED_NAMES = frozenset({
    "_read_store", "_inspect_store_file", "_repo_name", "_console_factors", "_console_summary",
    "_console_proposed", "_console_proposal", "_console_share_state", "_resolve_store",
    "list_stores", "resolve_store_slug", "resolve_store", "store_summary", "dashboard_summary",
    "list_decisions", "get_decision_detail", "list_tombstones", "list_global_rules",
    "delete_global_rule", "team_snapshot",
})


# Public names that console_api owns but store deliberately does NOT re-export.
NOT_RE_EXPORTED = frozenset({"team_snapshot"})


class TestFacadeIsBackCompatOnly:
    """The facade exists so a name that was public on `store` before an extraction still
    resolves there. It is not the surface production code uses: every caller in contexer/
    imports the owner. So it is frozen, and a name no longer reached through it is dropped."""

    def test_a_name_with_no_facade_consumer_is_not_re_exported(self):
        for name in NOT_RE_EXPORTED:
            assert hasattr(console_api, name), name       # the owner still has it
            assert name not in store._CONSOLE_EXPORTS     # store does not advertise it
            with pytest.raises(AttributeError):
                getattr(store, name)

    def test_production_code_reaches_the_owner_not_the_facade(self):
        # Mechanical form of the rule: no module under contexer/ may address a moved read
        # through `store.`. Written as a scan because the failure is invisible otherwise -
        # the facade answers correctly, so nothing breaks and the seam quietly erodes.
        watched = MOVED_NAMES | NOT_RE_EXPORTED
        offenders = []
        for path in pathlib.Path(store.__file__).parent.rglob("*.py"):
            if path.name in ("store.py", "console_api.py"):
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                # AST, not grep: the rule is about code, and several modules discuss these
                # names in prose while correctly never calling them.
                if (isinstance(node, ast.Attribute) and node.attr in watched
                        and isinstance(node.value, ast.Name) and node.value.id == "store"):
                    offenders.append(f"{path.name}:{node.lineno} store.{node.attr}")
        assert offenders == [], offenders


class TestFacade:
    """store.py re-exports the public reads lazily (PEP 562 `__getattr__`), so every pre-existing
    `store.<name>` caller (the test suite, and anything outside this repo importing the published
    package) kept working without an edit."""

    def test_every_export_resolves_to_the_owner_module_object(self):
        # `is`, not just truthiness: a stale copy assigned into store's namespace would still
        # pass a callable check while drifting from the module that actually owns the read.
        for name in store._CONSOLE_EXPORTS:
            assert getattr(store, name) is getattr(console_api, name), name

    def test_no_export_names_a_function_that_does_not_exist(self):
        # Guards the other direction: a typo or a later rename in console_api.py would leave the
        # facade advertising a name that raises AttributeError only when someone finally calls it.
        for name in store._CONSOLE_EXPORTS:
            assert callable(getattr(console_api, name, None)), name

    def test_dir_lists_every_lazily_resolved_export(self):
        # The invariant __dir__'s own comment states: whatever __getattr__ answers, dir() lists.
        listed = set(dir(store))
        assert store._CONSOLE_EXPORTS <= listed
        assert store._GUARD_EXPORTS <= listed
        assert store._CONFLICT_EXPORTS <= listed

    def test_an_unknown_attribute_still_raises(self):
        # The facade must not turn store into a namespace that answers anything.
        try:
            store.not_a_console_read
        except AttributeError:
            return
        raise AssertionError("store.__getattr__ answered an unknown name")


class TestNothingLeftBehind:
    def test_store_no_longer_defines_the_moved_names(self):
        tree = ast.parse(Path(store.__file__).read_text(encoding="utf-8"))
        defined = {n.name for n in tree.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert not (MOVED_NAMES & defined), sorted(MOVED_NAMES & defined)

    def test_console_api_defines_all_of_them(self):
        tree = ast.parse(Path(console_api.__file__).read_text(encoding="utf-8"))
        defined = {n.name for n in tree.body
                   if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        assert MOVED_NAMES <= defined, sorted(MOVED_NAMES - defined)

    def test_what_deliberately_stayed_is_still_in_store(self):
        # `load_diagnostics` keeps its family (`global_diagnostics`, `deleted_diagnostics`);
        # `store_files`/`_is_repo_store_file` are the store-file enumeration scope_audit.py
        # depends on by documented contract; `file_mtime` is read by verify_scan_conventions
        # and anchors.py as well as by the console. `store_files`/`file_mtime` are public
        # because two extracted modules read them; `_is_repo_store_file` has one reader.
        for name in ("load_diagnostics", "global_diagnostics", "deleted_diagnostics",
                     "store_files", "_is_repo_store_file", "file_mtime"):
            assert name in vars(store), name


class TestNoImportCycle:
    """console_api.py imports the store module OBJECT and store.py resolves it lazily, so neither
    module needs the other to have finished loading. Checked in a FRESH interpreter because an
    already-imported package hides an ordering bug completely."""

    def test_console_api_can_be_the_first_touch_of_the_package(self):
        proc = subprocess.run(
            [sys.executable, "-c",
             "import contexer.console_api as c; print(c.store.GLOBAL_SLUG)"],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "_global"

    def test_store_can_still_be_the_first_touch(self):
        proc = subprocess.run(
            [sys.executable, "-c", "import contexer.store as s; print(s.list_stores.__module__)"],
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        assert proc.stdout.strip() == "contexer.console_api"


class TestCallTimeResolution:
    """The payoff of `from contexer import store` over `from contexer.store import STORE_DIR`:
    every store-owned value is read at call time, so a monkeypatch on `contexer.store` is still
    seen from in here. The whole existing console test suite depends on this through the
    `tmp_repo` fixture, which patches `store.STORE_DIR`; these assert it directly."""

    def test_store_dir_is_read_at_call_time(self, tmp_path, monkeypatch):
        # Deliberately through `resolve_store`, the ONE read that dereferences `store.STORE_DIR`
        # in this module (its by-file-name short-circuit). `list_stores` would pass either way:
        # it reaches the directory via `store.store_files()`, so the lookup happens inside
        # store.py and proves nothing about how THIS module holds the name.
        store_dir = tmp_path / ".contexer"
        store_dir.mkdir()
        (store_dir / "some_repo-deadbeef.json").write_text(
            f'{{"repo_path": "{tmp_path / "repo"}", "entries": []}}', encoding="utf-8")
        redirect_store_dir(monkeypatch, store_dir)
        resolved = console_api.resolve_store("some_repo-deadbeef")
        assert resolved is not None and resolved["ok"], resolved
        assert resolved["repo_path"] == str(tmp_path / "repo")

    def test_store_functions_are_looked_up_at_call_time(self, tmp_repo, monkeypatch):
        store.update_decision(tmp_repo, "Use uv for dependency management", "s1", "convention")
        monkeypatch.setattr(store, "current_repo_path", lambda: tmp_repo)
        assert [r["is_current"] for r in console_api.list_stores()] == [True]
        monkeypatch.setattr(store, "current_repo_path", lambda: "/somewhere/else")
        assert [r["is_current"] for r in console_api.list_stores()] == [False]

    def test_the_global_read_write_pair_honours_a_patched_slug(self, tmp_repo, monkeypatch):
        # Pins the read/write pair end-to-end under a renamed global store, which is the one
        # console read that MUTATES. Deliberately not claimed as a call-time-lookup check:
        # `delete_global_rule`'s only direct use of the constant is the lock name
        # (`store.store_lock(store.GLOBAL_SLUG)`), and the file itself is addressed inside
        # store.py via `_global_path`, so an import-time-bound slug here would take a
        # differently-named lock and still address the right file, invisible to a test.
        monkeypatch.setattr(store, "GLOBAL_SLUG", "_globals")
        store.update_global_decision("Never commit directly to main", "s1", "constraint")
        rules = console_api.list_global_rules()
        assert rules["ok"] and len(rules["rules"]) == 1
        ok, message = console_api.delete_global_rule(rules["rules"][0]["id"])
        assert ok, message
        assert console_api.list_global_rules()["rules"] == []


# ── list_sessions / session_transcript (issue #256) ─────────────────────────────────────

def _seed(repo: str, content: str, session_id: str, subtype: str = "architecture", *,
         status: str = "approved", ts: str | None = None, memory_key: str | None = None,
         created_by: str = "ai") -> str:
    """Build one decision entry with exact control over session_id/status/timestamp -
    `update_decision`'s public path routes through novelty filtering and status
    classification, neither of which these tests want to fight (borrowed pattern:
    test_guard_engine.py's `_entry_at`)."""
    entry = store._new_decision_entry(content, session_id, subtype, created_by=created_by,
                                      status=status, memory_key=memory_key)
    if not session_id:
        del entry["session_id"]
    if ts is not None:
        entry["timestamp"] = ts
        entry["updated_at"] = ts
    data = store.load(repo)
    data["entries"].append(entry)
    store.save(repo, data)
    return entry["id"]


def _seed_conflict(repo: str, session_id: str, standing: str, update: str,
                   subtype: str = "architecture") -> str:
    """An approved decision carrying a real (non-bookkeeping) Suggested Update, originated by
    `session_id` - the shape `has_open_conflict` renders as open (borrowed pattern:
    test_conflicts.py's `_conflicted`)."""
    store.update_decision(repo, standing, session_id, subtype)
    data = store.load(repo)
    entry = next(e for e in data["entries"] if e["content"].startswith(standing[:20]))
    entry["status"] = "approved"
    store.save(repo, data)
    eid = entry["id"]
    ok, rid = store.update_decision(repo, update, session_id, subtype, replace_id=eid)
    assert ok and rid == eid and store.entry_by_id(
        store.load(repo)["entries"], eid).get("proposed_revision")
    return eid


class TestListSessions:
    def test_groups_by_originating_session_only(self, tmp_repo):
        # created by s1; a later session (s2) merely recurs the same content, which adds it
        # to session_ids WITHOUT changing the entry's originating session_id.
        eid = _seed(tmp_repo, "Use Postgres for the decision store", "s1")
        data = store.load(tmp_repo)
        entry = next(e for e in data["entries"] if e["id"] == eid)
        entry["session_ids"] = ["s1", "s2"]
        store.save(tmp_repo, data)

        rows = console_api.list_sessions(tmp_repo)["sessions"]
        assert [r["session_id"] for r in rows] == ["s1"]
        assert rows[0]["count"] == 1

    def test_memory_sync_excluded_and_counted_separately(self, tmp_repo):
        _seed(tmp_repo, "Never store plaintext passwords", "s1", subtype="constraint")
        _seed(tmp_repo, "Imported fact from the memory tool", "memory-sync",
             subtype="convention", memory_key="claude-memory:foo.md#Section",
             created_by="memory")

        result = console_api.list_sessions(tmp_repo)
        assert result["total_decisions"] == 2
        assert result["memory_import_count"] == 1
        assert [r["session_id"] for r in result["sessions"]] == ["s1"]

    def test_null_bucket_present_labeled_and_sorted_last(self, tmp_repo):
        _seed(tmp_repo, "Use uv for dependency management", "s1",
             subtype="convention", ts="2026-08-27T00:00:00+00:00")
        # No session_id at all (predates session attribution) - and deliberately the NEWEST
        # timestamp, to prove the null bucket sorts last regardless of its own activity.
        _seed(tmp_repo, "Legacy decision with no session id", "", subtype="convention",
             ts="2026-08-28T00:00:00+00:00")

        rows = console_api.list_sessions(tmp_repo)["sessions"]
        assert [r["session_id"] for r in rows] == ["s1", None]
        assert rows[-1]["short_id"] == ""

    def test_sessions_sorted_by_last_at_descending(self, tmp_repo):
        _seed(tmp_repo, "Old decision", "s-old", ts="2026-08-01T00:00:00+00:00")
        _seed(tmp_repo, "New decision", "s-new", ts="2026-08-20T00:00:00+00:00")
        _seed(tmp_repo, "Middle decision", "s-mid", ts="2026-08-10T00:00:00+00:00")

        rows = console_api.list_sessions(tmp_repo)["sessions"]
        assert [r["session_id"] for r in rows] == ["s-new", "s-mid", "s-old"]

    def test_first_at_and_last_at_span_the_session(self, tmp_repo):
        _seed(tmp_repo, "First decision", "s1", ts="2026-08-01T00:00:00+00:00")
        _seed(tmp_repo, "Second decision", "s1", ts="2026-08-15T00:00:00+00:00")

        row = console_api.list_sessions(tmp_repo)["sessions"][0]
        assert row["first_at"] == "2026-08-01T00:00:00+00:00"
        assert row["last_at"] == "2026-08-15T00:00:00+00:00"
        assert row["count"] == 2

    def test_open_count_pending_and_conflict_not_bookkeeping(self, tmp_repo):
        _seed(tmp_repo, "Never ship a migration without a rollback plan", "s1",
             subtype="constraint", status="pending_approval")
        _seed_conflict(tmp_repo, "s1",
                      "Use Postgres for the decision store; SQLite won't handle concurrency",
                      "Switch to DynamoDB for the decision store; Postgres is superseded")
        bookkeeping_id = _seed(tmp_repo, "Config is expressed in TOML", "s1",
                               subtype="convention")
        data = store.load(tmp_repo)
        entry = next(e for e in data["entries"] if e["id"] == bookkeeping_id)
        entry["proposed_revision"] = {
            "content": "Config is expressed in YAML", "title": "", "source": "scan",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        store.save(tmp_repo, data)

        row = console_api.list_sessions(tmp_repo)["sessions"][0]
        assert row["count"] == 3
        assert row["open_count"] == 2

    def test_unreadable_store_fails_soft(self, tmp_repo):
        store._store_path(tmp_repo).write_text('{"repo_path": "/x", "entries": [{"id": "1"',
                                                encoding="utf-8")
        assert console_api.list_sessions(tmp_repo) == {
            "sessions": [], "memory_import_count": 0, "total_decisions": 0}


class TestSessionTranscript:
    def test_entries_ascend_by_timestamp_capture_order(self, tmp_repo):
        old_id = _seed(tmp_repo, "Old decision", "s1", ts="2026-08-01T00:00:00+00:00")
        new_id = _seed(tmp_repo, "New decision", "s1", ts="2026-08-10T00:00:00+00:00",
                       status="pending_approval")

        transcript = console_api.session_transcript(tmp_repo, "s1")
        assert [e["id"] for e in transcript["entries"]] == [old_id, new_id]
        assert transcript["count"] == 2
        assert transcript["first_at"] == "2026-08-01T00:00:00+00:00"
        assert transcript["last_at"] == "2026-08-10T00:00:00+00:00"

    def test_open_pending_and_conflict_flags(self, tmp_repo):
        pending_id = _seed(tmp_repo, "Never ship without a rollback plan", "s1",
                           subtype="constraint", status="pending_approval",
                           ts="2026-08-01T00:00:00+00:00")
        conflict_id = _seed_conflict(
            tmp_repo, "s1",
            "Use Postgres for the decision store; SQLite won't handle concurrency",
            "Switch to DynamoDB for the decision store; Postgres is superseded")
        plain_id = _seed(tmp_repo, "Name test files test_<module>.py", "s1",
                         subtype="convention", ts="2026-08-03T00:00:00+00:00")

        transcript = console_api.session_transcript(tmp_repo, "s1")
        rows = {r["id"]: r for r in transcript["entries"]}
        assert rows[pending_id]["pending"] is True
        assert rows[pending_id]["open"] is True
        assert rows[pending_id]["open_conflict"] is False
        assert rows[conflict_id]["open_conflict"] is True
        assert rows[conflict_id]["open"] is True
        assert rows[conflict_id]["pending"] is False
        assert rows[plain_id]["open"] is False
        assert [r["id"] for r in transcript["open"]] == [pending_id, conflict_id]

    def test_bookkeeping_proposal_does_not_count_as_open(self, tmp_repo):
        eid = _seed(tmp_repo, "Config is expressed in TOML", "s1", subtype="convention")
        data = store.load(tmp_repo)
        entry = next(e for e in data["entries"] if e["id"] == eid)
        entry["proposed_revision"] = {
            "content": "Config is expressed in YAML", "title": "", "source": "scan",
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        store.save(tmp_repo, data)

        transcript = console_api.session_transcript(tmp_repo, "s1")
        assert transcript["entries"][0]["open"] is False
        assert transcript["entries"][0]["open_conflict"] is False
        assert transcript["open"] == []

    def test_anchor_commit_is_carried(self, tmp_repo):
        eid = _seed(tmp_repo, "Use uv for dependency management", "s1", subtype="convention")
        data = store.load(tmp_repo)
        entry = next(e for e in data["entries"] if e["id"] == eid)
        entry["anchor_commit"] = "deadbeef"
        store.save(tmp_repo, data)

        transcript = console_api.session_transcript(tmp_repo, "s1")
        assert transcript["entries"][0]["anchor_commit"] == "deadbeef"

    def test_full_and_short_id_addressing(self, tmp_repo):
        sid = "abcdef12-3456-7890-aaaa-bbbbbbbbbbbb"
        _seed(tmp_repo, "A decision", sid)

        full = console_api.session_transcript(tmp_repo, sid)
        short = console_api.session_transcript(tmp_repo, sid[:8])
        assert full is not None and short is not None
        assert full["session_id"] == sid == short["session_id"]
        assert full["short_id"] == sid[:8] == short["short_id"]

    def test_unknown_session_is_none(self, tmp_repo):
        _seed(tmp_repo, "A decision", "s1")
        assert console_api.session_transcript(tmp_repo, "not-a-real-session") is None

    def test_empty_session_id_is_none_not_an_arbitrary_session(self, tmp_repo):
        # Every session id "starts with" "" - the prefix-match fallback must not treat an
        # empty id as a vacuous match for whichever session a set happens to iterate first.
        _seed(tmp_repo, "A decision", "s1")
        assert console_api.session_transcript(tmp_repo, "") is None

    def test_memory_sync_literal_is_none(self, tmp_repo):
        _seed(tmp_repo, "Imported fact", "memory-sync", memory_key="claude-memory:foo.md#S",
             created_by="memory")
        assert console_api.session_transcript(tmp_repo, "memory-sync") is None

    def test_none_addresses_the_null_bucket(self, tmp_repo):
        eid = _seed(tmp_repo, "Legacy decision with no session id", "")
        transcript = console_api.session_transcript(tmp_repo, "none")
        assert transcript is not None
        assert transcript["session_id"] is None
        assert transcript["short_id"] == ""
        assert [e["id"] for e in transcript["entries"]] == [eid]

    def test_none_with_no_null_bucket_entries_is_none(self, tmp_repo):
        _seed(tmp_repo, "A decision", "s1")
        assert console_api.session_transcript(tmp_repo, "none") is None

    def test_unreadable_store_fails_soft(self, tmp_repo):
        store._store_path(tmp_repo).write_text('{"repo_path": "/x", "entries": [{"id": "1"',
                                                encoding="utf-8")
        assert console_api.session_transcript(tmp_repo, "s1") is None


# ── transcript link (issue #261) ──────────────────────────────────────────────────────────

class TestTranscriptLink:
    """The "View full transcript" link's backing reads: existence-gated, fail-soft, and owned
    by console_api (never a raw file open in ui/api.py - see `_claude_transcript_path`'s
    docstring). `_claude_transcript_path` resolves `Path.home()` at CALL time (not a module
    constant), so every test here patches `Path.home` itself - the same pattern
    test_readonly_store_dir.py uses - rather than touching the real `~/.claude`; the
    session-scoped `console_paths_never_resolve_the_real_home` fixture in conftest.py already
    guarantees a leak here would fail the run."""

    def _seed_transcript(self, tmp_path, monkeypatch, repo: str, session_id: str,
                         content: str = "{}\n") -> Path:
        fake_home = tmp_path / "fakehome"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        project_dir = fake_home / ".claude" / "projects" / repo.replace("/", "-")
        project_dir.mkdir(parents=True, exist_ok=True)
        path = project_dir / f"{session_id}.jsonl"
        path.write_text(content, encoding="utf-8")
        return path

    def test_transcript_exists_finds_a_seeded_file(self, tmp_repo, tmp_path, monkeypatch):
        self._seed_transcript(tmp_path, monkeypatch, tmp_repo, "sess-full-123")
        assert console_api.transcript_exists(tmp_repo, "sess-full-123") is True

    def test_transcript_exists_is_false_for_a_missing_file(self, tmp_repo, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
        assert console_api.transcript_exists(tmp_repo, "no-such-session") is False

    def test_transcript_exists_never_resolves_a_short_id(self, tmp_repo, tmp_path, monkeypatch):
        sid = "abcdef12-3456-7890-aaaa-bbbbbbbbbbbb"
        self._seed_transcript(tmp_path, monkeypatch, tmp_repo, sid)
        assert console_api.transcript_exists(tmp_repo, sid) is True
        # The store's full/short-id prefix matching (session_transcript's own addressing) is
        # deliberately NOT reproduced for the real transcript file - Claude Code's own
        # filenames are always the full id, so a short id must fail closed, not resolve.
        assert console_api.transcript_exists(tmp_repo, sid[:8]) is False

    def test_read_transcript_returns_the_raw_content(self, tmp_repo, tmp_path, monkeypatch):
        content = '{"type": "user", "message": {"role": "user", "content": "hi"}}\n'
        self._seed_transcript(tmp_path, monkeypatch, tmp_repo, "sess-1", content=content)
        assert console_api.read_transcript(tmp_repo, "sess-1") == content

    def test_read_transcript_is_none_for_a_missing_file(self, tmp_repo, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
        assert console_api.read_transcript(tmp_repo, "sess-1") is None

    def test_read_transcript_never_resolves_a_short_id(self, tmp_repo, tmp_path, monkeypatch):
        sid = "abcdef12-3456-7890-aaaa-bbbbbbbbbbbb"
        self._seed_transcript(tmp_path, monkeypatch, tmp_repo, sid, content="hello\n")
        assert console_api.read_transcript(tmp_repo, sid) == "hello\n"
        assert console_api.read_transcript(tmp_repo, sid[:8]) is None

    def test_read_transcript_over_the_size_cap_returns_a_pointer_not_the_bytes(
            self, tmp_repo, tmp_path, monkeypatch):
        path = self._seed_transcript(tmp_path, monkeypatch, tmp_repo, "sess-big",
                                     content="x" * 100)
        monkeypatch.setattr(console_api, "_TRANSCRIPT_SIZE_CAP", 10)

        message = console_api.read_transcript(tmp_repo, "sess-big")
        assert message is not None
        assert "x" * 100 not in message
        assert str(path) in message

    def test_session_transcript_carries_transcript_available_true_for_full_and_short_id(
            self, tmp_repo, tmp_path, monkeypatch):
        sid = "abcdef12-3456-7890-aaaa-bbbbbbbbbbbb"
        _seed(tmp_repo, "A decision", sid)
        self._seed_transcript(tmp_path, monkeypatch, tmp_repo, sid)

        # Addressed by full id and by the #256 short-id fallback alike - the flag is computed
        # from the RESOLVED full session id (`target`) either way, never the short one passed in.
        assert console_api.session_transcript(tmp_repo, sid)["transcript_available"] is True
        assert console_api.session_transcript(tmp_repo, sid[:8])["transcript_available"] is True

    def test_session_transcript_carries_transcript_available_false_when_absent(
            self, tmp_repo, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
        _seed(tmp_repo, "A decision", "s1")
        transcript = console_api.session_transcript(tmp_repo, "s1")
        assert transcript["transcript_available"] is False

    def test_session_transcript_null_bucket_transcript_unavailable(
            self, tmp_repo, tmp_path, monkeypatch):
        # The null bucket has no session id at all (`target` is None) - there is nothing to
        # look a transcript up by, so this must never even attempt the filesystem check.
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
        _seed(tmp_repo, "Legacy decision with no session id", "")
        transcript = console_api.session_transcript(tmp_repo, "none")
        assert transcript["transcript_available"] is False

    # --- path-traversal regression (fix round after review) ---------------------------------
    # `session_id` reaches `_claude_transcript_path` from a URL path segment. `ui/api.py`'s
    # `dispatch` splits the raw request path on '/' BEFORE unquoting each segment, so a
    # percent-encoded '/' (`%2F`) survives routing as one segment and only becomes a literal
    # '/' once it lands here - a caller authenticated for repo A could smuggle `../<repo B's
    # slug>/<session>` and read repo B's real transcript, or `../../secret` to read any file
    # under `~/.claude`. These tests call console_api directly with a session_id that ALREADY
    # contains the dangerous character, since that is the actual vulnerable surface regardless
    # of which URL-encoding trick produces it.

    def test_session_id_containing_a_slash_is_rejected(self, tmp_repo, tmp_path, monkeypatch):
        fake_home = tmp_path / "fakehome"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

        # A second repo's real transcript - the file the crafted session_id tries to reach.
        victim_repo = "/some/other/repo"
        victim_dir = fake_home / ".claude" / "projects" / victim_repo.replace("/", "-")
        victim_dir.mkdir(parents=True)
        (victim_dir / "victim-session.jsonl").write_text("SECRET other-repo transcript\n",
                                                          encoding="utf-8")

        traversal_id = "../" + victim_repo.replace("/", "-") + "/victim-session"
        assert console_api._claude_transcript_path(tmp_repo, traversal_id) is None
        assert console_api.transcript_exists(tmp_repo, traversal_id) is False
        assert console_api.read_transcript(tmp_repo, traversal_id) is None

    def test_session_id_containing_a_backslash_is_rejected(self, tmp_repo, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
        assert console_api.read_transcript(tmp_repo, "..\\secret") is None

    def test_session_id_escaping_entirely_outside_projects_is_rejected(
            self, tmp_repo, tmp_path, monkeypatch):
        """The second live PoC from the review: `../../secret` reaching a file entirely
        outside any project directory, e.g. `~/.claude/secret.jsonl`."""
        fake_home = tmp_path / "fakehome"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        claude_dir = fake_home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "secret.jsonl").write_text("SECRET machine-wide file\n", encoding="utf-8")

        assert console_api.read_transcript(tmp_repo, "../../secret") is None

    def test_session_id_of_dotdot_alone_is_rejected_even_without_a_slash(
            self, tmp_repo, tmp_path, monkeypatch):
        """`..` on its own never contains '/', so the slash check alone would not catch it -
        it needs its own explicit rejection. (In this function specifically the trailing
        `.jsonl` suffix would neutralise it into a harmless single filename component anyway,
        but the rejection is explicit rather than relying on that incidental fact.)"""
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
        assert console_api._claude_transcript_path(tmp_repo, "..") is None
        assert console_api.read_transcript(tmp_repo, "..") is None

    def test_session_id_of_single_dot_is_rejected(self, tmp_repo, tmp_path, monkeypatch):
        monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path / "fakehome"))
        assert console_api._claude_transcript_path(tmp_repo, ".") is None

    def test_a_normal_uuid_session_id_still_resolves_after_the_traversal_fix(
            self, tmp_repo, tmp_path, monkeypatch):
        """The fix must not collateral-damage the legitimate case."""
        fake_home = tmp_path / "fakehome"
        monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))
        sid = "abcdef12-3456-7890-aaaa-bbbbbbbbbbbb"
        project_dir = fake_home / ".claude" / "projects" / tmp_repo.replace("/", "-")
        project_dir.mkdir(parents=True)
        (project_dir / f"{sid}.jsonl").write_text("hello\n", encoding="utf-8")

        assert console_api._claude_transcript_path(tmp_repo, sid) is not None
        assert console_api.transcript_exists(tmp_repo, sid) is True
        assert console_api.read_transcript(tmp_repo, sid) == "hello\n"
