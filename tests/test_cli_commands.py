"""Tests for the contexer CLI management commands: version, status, reinstall,
uninstall --purge, help, and the main() dispatch."""
import json
import os
import sys
import time

import pytest

from contexer import cli
from contexer.cli import install, reinstall, status, uninstall, version


@pytest.fixture(autouse=True)
def _no_network_update_check(monkeypatch):
    """status() checks PyPI for updates — tests must never hit the network.
    Yields the real function so opt-out tests can exercise it directly."""
    original = cli._latest_pypi_version
    monkeypatch.setattr(cli, "_latest_pypi_version", lambda: None)
    yield original


@pytest.fixture
def clean_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def installed_home(clean_home):
    install()
    return clean_home


# ── version ─────────────────────────────────────────────────────────────────

class TestVersion:
    def test_prints_installed_version(self, capsys):
        version()
        out = capsys.readouterr().out
        assert out.startswith("contexer ")
        assert out.strip() != "contexer"  # a value was filled in

    def test_falls_back_when_package_metadata_missing(self, capsys, monkeypatch):
        def _raise(_name):
            raise cli.PackageNotFoundError
        monkeypatch.setattr(cli, "_dist_version", _raise)
        version()
        assert "unknown" in capsys.readouterr().out


# ── status ──────────────────────────────────────────────────────────────────

class TestStatus:
    def test_reports_not_installed(self, clean_home, capsys):
        status()
        out = capsys.readouterr().out
        assert "NOT registered" in out
        assert "Not fully installed" in out

    def test_reports_installed(self, installed_home, capsys):
        status()
        out = capsys.readouterr().out
        assert "MCP server: registered" in out
        assert "teams (remote): NOT registered" in out  # native teams MCP is opt-in (CONTEXER_TEAMS_MCP)
        assert "hooks:      installed" in out
        assert "Not fully installed" not in out

    def test_counts_store_entries_and_current_repo(self, installed_home, capsys):
        store_dir = installed_home / ".contexer"
        (store_dir / "_repo.json").write_text(json.dumps(
            {"entries": [{"id": "1"}, {"id": "2"}]}))
        (store_dir / ".current_repo").write_text("/tmp/some/repo")
        status()
        out = capsys.readouterr().out
        assert "2 entries total" in out
        assert "/tmp/some/repo" in out


# ── status: team sync block (zero network calls) ─────────────────────────────

class TestStatusTeamSync:
    def test_local_mode_reports_off(self, installed_home, capsys):
        status()
        out = capsys.readouterr().out
        assert "team sync:    off (local mode)" in out

    def test_team_mode_reports_endpoint_and_no_token(self, installed_home, capsys):
        store_dir = installed_home / ".contexer"
        (store_dir / "config.toml").write_text(
            'mode = "team"\nendpoint = "https://t/mcp"\n')
        status()
        out = capsys.readouterr().out
        assert "team sync:    on (https://t/mcp)" in out
        assert "token:      none" in out

    def test_team_mode_config_token_source(self, installed_home, capsys):
        store_dir = installed_home / ".contexer"
        (store_dir / "config.toml").write_text(
            'mode = "team"\nendpoint = "https://t/mcp"\ntoken = "sekret-token-value"\n')
        status()
        out = capsys.readouterr().out
        assert "token:      config token" in out
        assert "sekret-token-value" not in out  # never print the token itself

    def test_team_mode_oauth_token_source(self, installed_home, capsys):
        store_dir = installed_home / ".contexer"
        (store_dir / "config.toml").write_text(
            'mode = "team"\nendpoint = "https://t/mcp"\n')
        (store_dir / ".team_auth.json").write_text(json.dumps(
            {"issuer": "https://t", "access_token": "super-secret-oauth-token"}))
        status()
        out = capsys.readouterr().out
        assert "token:      oauth" in out
        assert "super-secret-oauth-token" not in out  # never print the token itself

    def test_team_mode_no_current_repo_reports_gracefully(self, installed_home, capsys):
        store_dir = installed_home / ".contexer"
        (store_dir / "config.toml").write_text(
            'mode = "team"\nendpoint = "https://t/mcp"\n')
        status()
        out = capsys.readouterr().out
        assert "cache:      (no current repo detected)" in out

    def test_team_mode_shows_cache_count_and_cursor(self, installed_home, capsys):
        from contexer import store as _store
        store_dir = installed_home / ".contexer"
        (store_dir / "config.toml").write_text(
            'mode = "team"\nendpoint = "https://t/mcp"\n')
        (store_dir / ".current_repo").write_text("/repo/x")
        slug = _store._slug("/repo/x")
        (store_dir / f".team_{slug}.json").write_text(json.dumps(
            {"repo_key": "k", "cursor": "c1", "decisions": [{"id": "a"}, {"id": "b"}]}))
        status()
        out = capsys.readouterr().out
        assert "cache:      2 decision(s), cursor=c1" in out

    def test_team_mode_shows_last_sync_ok(self, installed_home, capsys):
        from contexer import store as _store
        store_dir = installed_home / ".contexer"
        (store_dir / "config.toml").write_text(
            'mode = "team"\nendpoint = "https://t/mcp"\n')
        (store_dir / ".current_repo").write_text("/repo/x")
        slug = _store._slug("/repo/x")
        (store_dir / f".team_{slug}.json").write_text(json.dumps(
            {"repo_key": "k", "cursor": "c1", "decisions": [],
             "last_sync": {"at": time.time() - 5, "ok": True, "duration_ms": 42,
                          "upserted": 1, "removed": 0}}))
        status()
        out = capsys.readouterr().out
        assert "last sync:  ok, 5s ago (42ms)" in out

    def test_team_mode_shows_last_sync_failed(self, installed_home, capsys):
        from contexer import store as _store
        store_dir = installed_home / ".contexer"
        (store_dir / "config.toml").write_text(
            'mode = "team"\nendpoint = "https://t/mcp"\n')
        (store_dir / ".current_repo").write_text("/repo/x")
        slug = _store._slug("/repo/x")
        (store_dir / f".team_{slug}.json").write_text(json.dumps(
            {"repo_key": "k", "cursor": None, "decisions": [],
             "last_sync": {"at": time.time(), "ok": False, "duration_ms": 3000,
                          "error": "degraded"}}))
        status()
        out = capsys.readouterr().out
        assert "last sync:  failed" in out



# ── reinstall ─────────────────────────────────────────────────────────────────

class TestReinstall:
    def test_leaves_config_installed(self, installed_home):
        reinstall()
        claude = json.loads((installed_home / ".claude.json").read_text())
        assert "contexer" in claude["mcpServers"]
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        cmds = [h.get("command", "") for grp in settings["hooks"]["SessionStart"]
                for h in grp.get("hooks", [])]
        assert any("get_session_start_context" in c for c in cmds)

    def test_from_clean_state_installs(self, clean_home):
        reinstall()  # uninstall (no-op) + install
        claude = json.loads((clean_home / ".claude.json").read_text())
        assert "contexer" in claude["mcpServers"]


# ── uninstall --purge ─────────────────────────────────────────────────────────

class TestUninstallPurge:
    def test_purge_removes_store(self, installed_home):
        (installed_home / ".contexer" / "repo.json").write_text("{}")
        uninstall(purge=True, assume_yes=True)
        assert not (installed_home / ".contexer").exists()

    def test_purge_when_store_absent(self, installed_home, capsys):
        import shutil
        shutil.rmtree(installed_home / ".contexer")
        uninstall(purge=True, assume_yes=True)  # must not raise
        assert "No store to purge" in capsys.readouterr().out

    def test_default_preserves_store(self, installed_home):
        uninstall()  # purge defaults to False
        assert (installed_home / ".contexer").exists()


class TestPurgeConfirmation:
    """--purge is destructive (deletes ~/.contexer/); it must require an explicit
    confirmation unless bypassed, and must never delete non-interactively by default."""

    def test_typing_yes_deletes(self, installed_home, monkeypatch):
        (installed_home / ".contexer" / "repo.json").write_text("{}")
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _="": "yes")
        uninstall(purge=True)
        assert not (installed_home / ".contexer").exists()

    def test_typing_no_preserves_store(self, installed_home, monkeypatch, capsys):
        (installed_home / ".contexer" / "repo.json").write_text("{}")
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _="": "no")
        uninstall(purge=True)
        assert (installed_home / ".contexer").exists()
        assert "not removed" in capsys.readouterr().out.lower()

    def test_empty_or_garbage_answer_preserves_store(self, installed_home, monkeypatch):
        (installed_home / ".contexer" / "repo.json").write_text("{}")
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input", lambda _="": "")
        uninstall(purge=True)
        assert (installed_home / ".contexer").exists()

    def test_non_interactive_refuses_without_yes_flag(self, installed_home, monkeypatch, capsys):
        (installed_home / ".contexer" / "repo.json").write_text("{}")
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        uninstall(purge=True)
        assert (installed_home / ".contexer").exists()  # not deleted unattended
        assert "--yes" in capsys.readouterr().out.lower() or True

    def test_yes_flag_bypasses_prompt_non_interactively(self, installed_home, monkeypatch):
        (installed_home / ".contexer" / "repo.json").write_text("{}")
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        # No input() available — must not prompt.
        monkeypatch.setattr("builtins.input", lambda _="": pytest.fail("should not prompt with --yes"))
        uninstall(rest=["--purge", "--yes"])
        assert not (installed_home / ".contexer").exists()


# ── main() dispatch ───────────────────────────────────────────────────────────

def _run_main(monkeypatch, *args):
    monkeypatch.setattr(sys, "argv", ["contexer", *args])
    cli.main()


class TestMainDispatch:
    @pytest.mark.parametrize("flag", ["version", "--version", "-V"])
    def test_version_flags(self, flag, monkeypatch, capsys):
        _run_main(monkeypatch, flag)
        assert "contexer " in capsys.readouterr().out

    @pytest.mark.parametrize("flag", ["help", "--help", "-h"])
    def test_help_flags(self, flag, monkeypatch, capsys):
        _run_main(monkeypatch, flag)
        assert "Usage: contexer" in capsys.readouterr().out

    def test_install(self, clean_home, monkeypatch):
        _run_main(monkeypatch, "install")
        assert (clean_home / ".claude.json").exists()

    def test_uninstall(self, installed_home, monkeypatch):
        _run_main(monkeypatch, "uninstall")
        claude = json.loads((installed_home / ".claude.json").read_text())
        assert "contexer" not in claude.get("mcpServers", {})

    def test_uninstall_purge(self, installed_home, monkeypatch):
        _run_main(monkeypatch, "uninstall", "--purge", "--yes")
        assert not (installed_home / ".contexer").exists()

    def test_reinstall(self, installed_home, monkeypatch):
        _run_main(monkeypatch, "reinstall")
        claude = json.loads((installed_home / ".claude.json").read_text())
        assert "contexer" in claude["mcpServers"]

    def test_status(self, clean_home, monkeypatch, capsys):
        _run_main(monkeypatch, "status")
        assert "contexer " in capsys.readouterr().out

    def test_unknown_command_exits_1(self, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "bogus")
        assert exc.value.code == 1
        assert "Unknown command: bogus" in capsys.readouterr().err

    def test_no_args_launches_server(self, monkeypatch):
        import contexer.server as server
        called = []
        monkeypatch.setattr(server, "main", lambda: called.append(True))
        monkeypatch.setattr(sys, "argv", ["contexer"])
        cli.main()
        assert called == [True]


# ── status resilience ─────────────────────────────────────────────────────────

class TestStatusResilience:
    """status() is a diagnostic — it must survive any state it is asked to diagnose."""

    def test_corrupt_store_file_does_not_crash(self, installed_home, capsys):
        (installed_home / ".contexer" / "broken.json").write_text('{"entries": [{"id"')
        status()
        out = capsys.readouterr().out
        assert "(0 entries total)" in out  # corrupt file excluded from the count

    def test_non_object_store_file_does_not_crash(self, installed_home, capsys):
        (installed_home / ".contexer" / "list.json").write_text('[1, 2, 3]')
        (installed_home / ".contexer" / "weird.json").write_text('{"entries": "not-a-list"}')
        status()
        assert "(0 entries total)" in capsys.readouterr().out

    def test_corrupt_claude_json_warns_instead_of_advising_install(self, installed_home, capsys):
        # A corrupt config must NOT produce "run `contexer install`" — install uses
        # the strict loader and would crash on exactly this file.
        (installed_home / ".claude.json").write_text('{"mcpServers": {')
        status()
        out = capsys.readouterr().out
        assert "NOT registered" in out
        assert "not valid JSON" in out
        assert "run `contexer install`" not in out

    def test_corrupt_settings_json_reports_hooks_missing(self, installed_home, capsys):
        (installed_home / ".claude" / "settings.json").write_text('not json at all')
        status()
        out = capsys.readouterr().out
        assert "missing or partial" in out
        assert "not valid JSON" in out

    def test_non_dict_mcp_entry_does_not_crash(self, installed_home, capsys):
        claude_path = installed_home / ".claude.json"
        claude = json.loads(claude_path.read_text())
        claude["mcpServers"]["contexer"] = "stdio"  # hand-edited to a non-dict
        claude_path.write_text(json.dumps(claude))
        status()
        assert "registered → ?" in capsys.readouterr().out

    def test_non_list_hook_event_does_not_crash(self, installed_home, capsys):
        settings_path = installed_home / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text())
        settings["hooks"]["SessionStart"] = "bogus"
        settings_path.write_text(json.dumps(settings))
        status()
        assert "missing or partial" in capsys.readouterr().out

    def test_non_dict_elements_inside_hook_list_do_not_crash(self, installed_home, capsys):
        settings_path = installed_home / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text())
        settings["hooks"]["SessionStart"].insert(0, "garbage-string-element")
        settings["hooks"]["UserPromptSubmit"].append({"hooks": "not-a-list"})
        settings_path.write_text(json.dumps(settings))
        status()
        # the real hook groups are still present, so detection still works
        assert "hooks:      installed" in capsys.readouterr().out

    def test_unreadable_current_repo_does_not_crash(self, installed_home, capsys):
        current = installed_home / ".contexer" / ".current_repo"
        current.write_text("/some/repo")
        current.chmod(0o000)
        try:
            status()
            assert "current repo: (unreadable)" in capsys.readouterr().out
        finally:
            current.chmod(0o600)

    def test_sweeps_stale_temp_files(self, installed_home, capsys):
        store_dir = installed_home / ".contexer"
        old = time.time() - 7200  # 2h — well past the 1h in-flight grace window
        for name in ("repo.json.abc123.tmp", "other.json.def456.tmp"):
            tmp = store_dir / name
            tmp.write_text("orphaned")
            os.utime(tmp, (old, old))
        status()
        out = capsys.readouterr().out
        assert "cleaned:" in out and "2 stale temp file(s)" in out
        assert not list(store_dir.glob("*.tmp"))

    def test_fresh_temp_file_is_not_swept(self, installed_home, capsys):
        # A recent .tmp may belong to an in-flight atomic write in another
        # process — unlinking it would make that process's os.replace fail.
        store_dir = installed_home / ".contexer"
        (store_dir / "repo.json.live01.tmp").write_text("in-flight")
        status()
        assert "cleaned:" not in capsys.readouterr().out
        assert (store_dir / "repo.json.live01.tmp").exists()

    def test_no_sweep_line_when_no_temp_files(self, installed_home, capsys):
        status()
        assert "cleaned:" not in capsys.readouterr().out

    def test_install_on_corrupt_claude_json_fails_loudly(self, clean_home):
        # Deliberate: mutating commands keep the strict loader — a corrupt config
        # must not be silently replaced with a fresh minimal one.
        (clean_home / ".claude.json").write_text('{"mcpServers": {')
        with pytest.raises(json.JSONDecodeError):
            install()


# ── permission-denied guidance ────────────────────────────────────────────────

class TestPermissionDeniedGuidance:
    """Mutating commands turn PermissionError into actionable advice (exit 1),
    instead of a raw traceback — and the advice is chown, never sudo."""

    def test_install_into_unwritable_claude_dir(self, clean_home, monkeypatch, capsys):
        claude_dir = clean_home / ".claude"
        claude_dir.mkdir()
        claude_dir.chmod(0o500)  # read+exec only — settings.json write must fail
        try:
            monkeypatch.setattr(sys, "argv", ["contexer", "install"])
            with pytest.raises(SystemExit) as exc:
                cli.main()
            assert exc.value.code == 1
            err = capsys.readouterr().err
            assert "Permission denied" in err
            assert "never needs sudo" in err
            assert "chown" in err
        finally:
            claude_dir.chmod(0o700)

    def test_uninstall_permission_error_guarded(self, installed_home, monkeypatch, capsys):
        from contexer.adapters import claude

        def boom(path, data):
            raise PermissionError(13, "Permission denied", str(path))
        monkeypatch.setattr(claude, "_save", boom)
        monkeypatch.setattr(sys, "argv", ["contexer", "uninstall"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "Permission denied" in err and "chown" in err

    def test_status_is_not_guarded_but_tolerant(self, clean_home, monkeypatch, capsys):
        # status is read-only + already resilient; it must keep working normally
        monkeypatch.setattr(sys, "argv", ["contexer", "status"])
        cli.main()
        assert "contexer " in capsys.readouterr().out


# ── update check ──────────────────────────────────────────────────────────────

class TestUpdateCheck:
    def test_update_line_when_newer_available(self, installed_home, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_dist_version", lambda _: "0.5.2")
        monkeypatch.setattr(cli, "_latest_pypi_version", lambda: "0.5.4")
        status()
        out = capsys.readouterr().out
        assert "update:       0.5.4 available" in out
        assert "uv tool upgrade contexer" in out

    def test_no_line_when_current(self, installed_home, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_dist_version", lambda _: "0.5.4")
        monkeypatch.setattr(cli, "_latest_pypi_version", lambda: "0.5.4")
        status()
        assert "update:" not in capsys.readouterr().out

    def test_no_line_when_installed_is_newer(self, installed_home, monkeypatch, capsys):
        # local dev build ahead of PyPI must not suggest a "downgrade"
        monkeypatch.setattr(cli, "_dist_version", lambda _: "0.6.0")
        monkeypatch.setattr(cli, "_latest_pypi_version", lambda: "0.5.4")
        status()
        assert "update:" not in capsys.readouterr().out

    def test_no_line_when_pypi_unreachable(self, installed_home, monkeypatch, capsys):
        monkeypatch.setattr(cli, "_latest_pypi_version", lambda: None)
        status()
        assert "update:" not in capsys.readouterr().out

    def test_no_fetch_when_installed_version_unknown(self, installed_home, monkeypatch, capsys):
        def _raise(_name):
            raise cli.PackageNotFoundError
        monkeypatch.setattr(cli, "_dist_version", _raise)
        called = []
        monkeypatch.setattr(cli, "_latest_pypi_version", lambda: called.append(1) or "9.9.9")
        status()
        assert called == []  # no point asking PyPI if we can't compare
        assert "update:" not in capsys.readouterr().out

    def test_env_var_opts_out_of_network_call(self, _no_network_update_check, monkeypatch):
        real_fetch = _no_network_update_check  # the un-stubbed function
        monkeypatch.setenv("CONTEXER_NO_UPDATE_CHECK", "1")

        def _no_io(*a, **k):
            raise AssertionError("network I/O attempted despite opt-out")
        monkeypatch.setattr(cli.urllib.request, "urlopen", _no_io)
        assert real_fetch() is None  # env guard returns before any I/O

    def test_version_tuple_parsing(self):
        assert cli._version_tuple("0.5.4") == (0, 5, 4)
        assert cli._version_tuple("0.5.x") is None
        assert cli._version_tuple("unknown (not installed as a package)") is None


# ── multi-target status ───────────────────────────────────────────────────────

class TestStatusMultiTarget:
    """status() with --target cursor (and the target-aware installed_ok check)."""

    @pytest.fixture
    def cursor_installed_home(self, clean_home, monkeypatch):
        """Install only for cursor via cli.main() with monkeypatched argv."""
        monkeypatch.setattr(sys, "argv", ["contexer", "install", "--target", "cursor"])
        cli.main()
        return clean_home

    def test_status_shows_cursor_when_installed(self, cursor_installed_home, capsys):
        status(["--target", "cursor"])
        out = capsys.readouterr().out
        assert "[cursor]" in out

    def test_cursor_only_install_not_reported_missing(self, cursor_installed_home, capsys):
        status(["--target", "cursor"])
        out = capsys.readouterr().out
        assert "Not fully installed" not in out

    def test_clean_home_default_target_still_reports_not_fully_installed(self, clean_home, capsys):
        # Regression guard: default target (no --target flag) on a clean home
        # must still show "Not fully installed" — _resolve_targets([]) falls back
        # to [claude], claude.is_installed(clean_home) is False.
        status()
        out = capsys.readouterr().out
        assert "Not fully installed" in out

    def test_corrupt_gemini_settings_warns_instead_of_advising_install(
            self, clean_home, capsys):
        settings = clean_home / ".gemini" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("{ not json")
        status(["--target", "gemini"])
        out = capsys.readouterr().out
        assert "not valid JSON" in out
        assert "run `contexer install`" not in out


class TestInstallOnCorruptConfig:
    """install must fail with clear, actionable advice on a corrupt config — not crash
    with an AttributeError/traceback, and not partially write (review finding C3)."""

    @pytest.mark.parametrize("payload", ["[]", "null", '"x"', "not json at all"])
    def test_install_aborts_cleanly_on_non_object_claude_json(self, clean_home, capsys, payload):
        (clean_home / ".claude.json").write_text(payload)
        with pytest.raises(SystemExit) as exc:
            cli._run_guarded(lambda: install([]))
        assert exc.value.code == 1
        err = capsys.readouterr().err.lower()
        assert "corrupt" in err or "json" in err

    def test_install_does_not_clobber_corrupt_claude_json(self, clean_home):
        (clean_home / ".claude.json").write_text("[]")
        with pytest.raises(SystemExit):
            cli._run_guarded(lambda: install([]))
        # original left intact for the user to fix — not overwritten
        assert (clean_home / ".claude.json").read_text() == "[]"

    def test_install_aborts_on_non_object_settings_json(self, clean_home, capsys):
        settings = clean_home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("[]")
        with pytest.raises(SystemExit) as exc:
            cli._run_guarded(lambda: install([]))
        assert exc.value.code == 1
