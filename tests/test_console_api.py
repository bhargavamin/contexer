"""Tests for the contexer/console_api.py extraction: the module BOUNDARY, not the projections.

The projections themselves already have coverage, and it deliberately stayed where it was: the
14 test blocks that call these reads (test_global's TestUnreadableGlobalStore, test_store_delete's
TestUnreadableSidecar / TestSlugAddressing / TestPollCost, test_store's TestNonDictStoreRecovery /
TestConsoleSourceFiles, test_overlap_report, test_ui_api, test_ui_session_start) are each testing
store-level behaviour (sidecar corruption, slug addressing, poll cost) and merely OBSERVE it
through a console read. Moving them here would have separated a global-store corruption test from
the global-store test file. They also reach the reads as `store.<name>`, which is what keeps
store.py's `_CONSOLE_EXPORTS` facade exercised rather than an untested compatibility promise.

What had no coverage at all is the seam this extraction created, so that is what these pin: the
facade resolves, it resolves to the OWNER's object, `dir()` matches what `__getattr__` answers,
the module imports first without a cycle, nothing was left behind as a second definition in
store.py, and store-owned helpers are looked up at CALL time (the whole reason the module object
is imported instead of `from`-imports).
"""
import ast
import subprocess
import sys
from pathlib import Path

from contexer import console_api, store


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
        # `_store_files`/`_is_repo_store_file` are the store-file enumeration scope_audit.py
        # depends on by documented contract; `_file_mtime` is read by verify_scan_conventions
        # and anchors.py as well as by the console.
        for name in ("load_diagnostics", "global_diagnostics", "deleted_diagnostics",
                     "_store_files", "_is_repo_store_file", "_file_mtime"):
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
        # it reaches the directory via `store._store_files()`, so the lookup happens inside
        # store.py and proves nothing about how THIS module holds the name.
        store_dir = tmp_path / ".contexer"
        store_dir.mkdir()
        (store_dir / "some_repo-deadbeef.json").write_text(
            f'{{"repo_path": "{tmp_path / "repo"}", "entries": []}}', encoding="utf-8")
        monkeypatch.setattr(store, "STORE_DIR", store_dir)
        resolved = console_api.resolve_store("some_repo-deadbeef")
        assert resolved is not None and resolved["ok"], resolved
        assert resolved["repo_path"] == str(tmp_path / "repo")

    def test_store_functions_are_looked_up_at_call_time(self, tmp_repo, monkeypatch):
        store.update_decision(tmp_repo, "Use uv for dependency management", "s1", "convention")
        monkeypatch.setattr(store, "_current_repo_path", lambda: tmp_repo)
        assert [r["is_current"] for r in console_api.list_stores()] == [True]
        monkeypatch.setattr(store, "_current_repo_path", lambda: "/somewhere/else")
        assert [r["is_current"] for r in console_api.list_stores()] == [False]

    def test_the_global_read_write_pair_honours_a_patched_slug(self, tmp_repo, monkeypatch):
        # Pins the read/write pair end-to-end under a renamed global store, which is the one
        # console read that MUTATES. Deliberately not claimed as a call-time-lookup check:
        # `delete_global_rule`'s only direct use of the constant is the lock name
        # (`store._store_lock(store.GLOBAL_SLUG)`), and the file itself is addressed inside
        # store.py via `_global_path`, so an import-time-bound slug here would take a
        # differently-named lock and still address the right file, invisible to a test.
        monkeypatch.setattr(store, "GLOBAL_SLUG", "_globals")
        store.update_global_decision("Never commit directly to main", "s1", "constraint")
        rules = console_api.list_global_rules()
        assert rules["ok"] and len(rules["rules"]) == 1
        ok, message = console_api.delete_global_rule(rules["rules"][0]["id"])
        assert ok, message
        assert console_api.list_global_rules()["rules"] == []
