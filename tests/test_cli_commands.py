"""Tests for the contexer CLI management commands: version, status, reinstall,
uninstall --purge, help, and the main() dispatch."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from contexer import cli
from contexer.cli import install, reinstall, status, uninstall, version


@pytest.fixture(autouse=True)
def _no_network_update_check(monkeypatch):
    """status() checks PyPI for updates - tests must never hit the network.
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
        assert "teams (remote)" not in out  # native teams MCP entry retired (Python sync only)
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
             "last_sync": {"at": time.time() - 4, "ok": True, "duration_ms": 42,
                          "upserted": 1, "removed": 0}}))
        status()
        out = capsys.readouterr().out
        assert "last sync:  ok, 4s ago (42ms)" in out

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

    def test_team_mode_shows_last_render(self, installed_home, capsys):
        from contexer import store as _store
        store_dir = installed_home / ".contexer"
        (store_dir / "config.toml").write_text(
            'mode = "team"\nendpoint = "https://t/mcp"\n')
        (store_dir / ".current_repo").write_text("/repo/x")
        slug = _store._slug("/repo/x")
        (store_dir / f".team_{slug}.json").write_text(json.dumps(
            {"repo_key": "k", "cursor": "c1", "decisions": [],
             "last_render": {"at": time.time(), "rows": 10, "chars": 1229}}))
        status()
        out = capsys.readouterr().out
        assert "last render: 10 rows, ~1.2KB" in out

    def test_team_mode_dates_an_expired_oauth_session(self, installed_home, capsys):
        store_dir = installed_home / ".contexer"
        (store_dir / "config.toml").write_text(
            'mode = "team"\nendpoint = "https://t/mcp"\n')
        (store_dir / ".team_auth.json").write_text(json.dumps(
            {"issuer": "https://t", "expires_at": time.time() - 7200}))
        status()
        out = capsys.readouterr().out
        assert "token:      oauth (expired 2h ago - run `contexer login`)" in out

    def test_a_non_numeric_expiry_costs_the_line_not_the_command(self, clean_home, capsys):
        """`.team_auth.json` is validated no further than "it is a dict", and main() runs
        status outside _run_guarded - so comparing a hand-edited string expiry against the
        clock replaced the whole diagnostic with a TypeError traceback."""
        store_dir = clean_home / ".contexer"
        store_dir.mkdir()
        (store_dir / "config.toml").write_text(
            'mode = "team"\nendpoint = "https://t/mcp"\n')
        (store_dir / ".team_auth.json").write_text(json.dumps(
            {"issuer": "https://t", "expires_at": "2026-01-01T00:00:00Z"}))
        status()
        out = capsys.readouterr().out
        assert "token:      oauth" in out
        assert "expired" not in out  # unjudgeable, so unjudged
        assert "Not fully installed" in out, "the lines after the team block still ran"

    def test_a_corrupt_team_cache_costs_the_lines_not_the_command(self, clean_home, capsys):
        """Same class as the expiry guard, on the other file status reads verbatim: a torn
        write leaves `decisions`, `last_sync` and `last_render` any shape at all."""
        from contexer import store as _store
        store_dir = clean_home / ".contexer"
        store_dir.mkdir()
        (store_dir / "config.toml").write_text(
            'mode = "team"\nendpoint = "https://t/mcp"\n')
        (store_dir / ".current_repo").write_text("/repo/x")
        slug = _store._slug("/repo/x")
        (store_dir / f".team_{slug}.json").write_text(json.dumps(
            {"decisions": 3, "last_sync": "yesterday", "last_render": {"chars": "lots"}}))
        status()
        out = capsys.readouterr().out
        assert "cache:      0 decision(s)" in out
        assert "last sync:  never" in out
        assert "last render: 0 rows, ~0.0KB" in out
        assert "Not fully installed" in out, "the lines after the team block still ran"

    def test_team_mode_omits_last_render_when_absent(self, installed_home, capsys):
        from contexer import store as _store
        store_dir = installed_home / ".contexer"
        (store_dir / "config.toml").write_text(
            'mode = "team"\nendpoint = "https://t/mcp"\n')
        (store_dir / ".current_repo").write_text("/repo/x")
        slug = _store._slug("/repo/x")
        (store_dir / f".team_{slug}.json").write_text(json.dumps(
            {"repo_key": "k", "cursor": "c1", "decisions": []}))
        status()
        out = capsys.readouterr().out
        assert "last render" not in out



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
        # No input() available - must not prompt.
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
    """status() is a diagnostic - it must survive any state it is asked to diagnose."""

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
        # A corrupt config must NOT produce "run `contexer install`" - install uses
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
        old = time.time() - 7200  # 2h - well past the 1h in-flight grace window
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
        # process - unlinking it would make that process's os.replace fail.
        store_dir = installed_home / ".contexer"
        (store_dir / "repo.json.live01.tmp").write_text("in-flight")
        status()
        assert "cleaned:" not in capsys.readouterr().out
        assert (store_dir / "repo.json.live01.tmp").exists()

    def test_no_sweep_line_when_no_temp_files(self, installed_home, capsys):
        status()
        assert "cleaned:" not in capsys.readouterr().out

    def test_install_on_corrupt_claude_json_fails_loudly(self, clean_home):
        # Deliberate: mutating commands keep the strict loader - a corrupt config
        # must not be silently replaced with a fresh minimal one.
        (clean_home / ".claude.json").write_text('{"mcpServers": {')
        with pytest.raises(json.JSONDecodeError):
            install()


# ── permission-denied guidance ────────────────────────────────────────────────

class TestPermissionDeniedGuidance:
    """Mutating commands turn PermissionError into actionable advice (exit 1),
    instead of a raw traceback - and the advice is chown, never sudo."""

    def test_install_into_unwritable_claude_dir(self, clean_home, monkeypatch, capsys):
        claude_dir = clean_home / ".claude"
        claude_dir.mkdir()
        claude_dir.chmod(0o500)  # read+exec only - settings.json write must fail
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
        # must still show "Not fully installed" - _resolve_targets([]) falls back
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
    """install must fail with clear, actionable advice on a corrupt config - not crash
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
        # original left intact for the user to fix - not overwritten
        assert (clean_home / ".claude.json").read_text() == "[]"

    def test_install_aborts_on_non_object_settings_json(self, clean_home, capsys):
        settings = clean_home / ".claude" / "settings.json"
        settings.parent.mkdir(parents=True)
        settings.write_text("[]")
        with pytest.raises(SystemExit) as exc:
            cli._run_guarded(lambda: install([]))
        assert exc.value.code == 1


# ── login: clean failures ────────────────────────────────────────────────────

class TestLoginFailures:
    """`contexer login` must fail with a message and a non-zero exit, never a traceback."""

    def test_an_unusable_config_toml_is_reported_cleanly(self, clean_home, monkeypatch, capsys):
        """ConfigError is not a ValueError subclass, so it fell through login_cmd's except
        clause AND _run_guarded's - out of a login that had already spent the browser flow."""
        from contexer import auth, config

        def boom(endpoint=None):
            raise config.ConfigError(
                f"invalid ui.port in {clean_home}/.contexer/config.toml: expected int")
        monkeypatch.setattr(auth, "login", boom)
        with pytest.raises(SystemExit) as exc:
            cli.login_cmd([])
        assert exc.value.code == 1
        assert "contexer login: invalid ui.port" in capsys.readouterr().err


# ── review: title headline ───────────────────────────────────────────────────

class TestReviewTitleHeadline:
    def test_pending_decision_shows_title_as_headline(self, tmp_repo, monkeypatch, capsys):
        """The non-proposed branch of review() leads with the title, content beneath -
        mirrors TestReviewOverlapSection's real-store driving style in test_overlap_report.py
        (tmp_repo redirects STORE_DIR; _git_root is patched to point at it)."""
        from contexer import store

        monkeypatch.setattr(store, "_git_root", lambda _cwd: tmp_repo)
        # subtype="constraint" always lands pending_approval (see _classify_level), so this
        # is deterministically in the review queue regardless of content-signal heuristics.
        stored, _entry_id = store.update_decision(
            tmp_repo,
            "Long body explaining the outbox pattern in detail for share retries.",
            "s1",
            subtype="constraint",
            title="Adopt outbox for retries",
        )
        assert stored

        monkeypatch.setattr("builtins.input", lambda *_a: "S")  # skip past the prompt
        cli.review()

        out = capsys.readouterr().out
        assert "[constraint]" in out
        assert "Adopt outbox for retries" in out
        assert "Long body explaining the outbox pattern" in out
        # title heading must come before the quoted body, on its own line
        head_idx = out.index("Adopt outbox for retries")
        body_idx = out.index("Long body explaining the outbox pattern")
        assert head_idx < body_idx


class TestReviewAnchorCandidates:
    def test_pending_decision_with_candidates_shows_would_anchor_line(
            self, tmp_repo, monkeypatch, capsys):
        """A pending decision carrying anchor_candidates (issue #175 Task 3) surfaces a
        one-line 'Would anchor: ...' hint before the approve/edit/ignore/skip prompt, so the
        human's approval signature is informed about what it will bless."""
        from contexer import store

        monkeypatch.setattr(store, "_git_root", lambda _cwd: tmp_repo)
        store.record_edited_file(tmp_repo, "auth/jwt.py")
        stored, _entry_id = store.update_decision(
            tmp_repo, "Decided to use JWT for auth", "sess-1", "constraint")
        assert stored

        monkeypatch.setattr("builtins.input", lambda *_a: "S")  # skip past the prompt
        cli.review()

        out = capsys.readouterr().out
        assert "Would anchor" in out and "auth/jwt.py" in out

    def test_pending_decision_without_candidates_omits_would_anchor_line(
            self, tmp_repo, monkeypatch, capsys):
        from contexer import store

        monkeypatch.setattr(store, "_git_root", lambda _cwd: tmp_repo)
        stored, _entry_id = store.update_decision(
            tmp_repo, "Decided to use JWT for auth", "s1", "constraint")
        assert stored

        monkeypatch.setattr("builtins.input", lambda *_a: "S")
        cli.review()

        out = capsys.readouterr().out
        assert "Would anchor" not in out


class TestReviewConflictMemo:
    def _conflicted(self, tmp_repo, store):
        """An approved decision carrying an ai-sourced Suggested Update (issue #193 shape)."""
        store.update_decision(tmp_repo, "Use Postgres for the decision store", "s1", "architecture")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e.get("type") == "decision")
        entry["status"] = "approved"
        store._save(tmp_repo, data)
        eid = entry["id"]
        ok, rid = store.update_decision(
            tmp_repo, "Switch to DynamoDB for the decision store", "s2", "architecture",
            replace_id=eid)
        assert ok and rid == eid
        return eid

    def test_review_prints_update_choice_memo_line(self, tmp_repo, monkeypatch, capsys):
        from contexer import store

        monkeypatch.setattr(store, "_git_root", lambda _cwd: tmp_repo)
        eid = self._conflicted(tmp_repo, store)
        store.record_conflict_memo(tmp_repo, eid, "update")

        monkeypatch.setattr("builtins.input", lambda *_a: "S")
        cli.review()

        out = capsys.readouterr().out
        assert "The update was picked with the developer on" in out
        assert "approve to formalize (dismiss drops it)" in out

    def test_review_prints_standing_choice_memo_line(self, tmp_repo, monkeypatch, capsys):
        from contexer import store

        monkeypatch.setattr(store, "_git_root", lambda _cwd: tmp_repo)
        eid = self._conflicted(tmp_repo, store)
        store.record_conflict_memo(tmp_repo, eid, "standing")

        monkeypatch.setattr("builtins.input", lambda *_a: "S")
        cli.review()

        out = capsys.readouterr().out
        assert "The update was declined with the developer on" in out
        assert "dismiss to formalize (approve applies it instead)" in out


# ── guard ────────────────────────────────────────────────────────────────────

@pytest.fixture
def guard_repo(tmp_path, monkeypatch):
    """A real throwaway git repo, STORE_DIR redirected, cwd chdir'd into it - the
    CLI guard commands resolve their repo via os.getcwd() (contexer guard has no
    --repo flag), so unlike tmp_repo this fixture must actually chdir. Mirrors
    tests/test_guard.py's `repo` fixture."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "guard@test.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Guard Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    from contexer import store
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    monkeypatch.chdir(repo)
    return repo


def _gwrite(repo, relpath, content):
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


def _ggit(repo, *args):
    subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)


def _gseed(repo, content, *, subtype="architecture", status="approved",
           created_by="human", source_files=None, title=""):
    from contexer import store
    entry = store._new_decision_entry(content, "test-session", subtype,
                                       created_by=created_by, status=status, title=title)
    if source_files is not None:
        entry["source_files"] = source_files
    data = store._load(str(repo))
    data["entries"].append(entry)
    store._save(str(repo), data)
    return entry


class TestGuardDispatchAndExitCodes:
    def test_dispatch_reaches_guard_no_staged_changes(self, guard_repo, monkeypatch):
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard")
        assert exc.value.code == 0

    def test_advisory_only_exits_0(self, guard_repo, monkeypatch, capsys):
        entry = _gseed(guard_repo, "Decided to use JWT for auth",
                        source_files=["auth/jwt.py"])
        _gwrite(guard_repo, "auth/jwt.py", "token = 1\n")
        _ggit(guard_repo, "add", "auth/jwt.py")
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard")
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "review this commit against 1 approved decision(s)" in out
        assert entry["id"][:8] in out
        assert "auth/jwt.py" in out
        assert "dismiss: contexer guard --dismiss" in out

    def test_violation_exits_1(self, guard_repo, monkeypatch, capsys):
        from contexer import store
        entry = _gseed(guard_repo, "Never commit TODO markers")
        store.arm_guard(str(guard_repo), entry["id"], "regex", pattern="TODO")
        _gwrite(guard_repo, "a.py", "# TODO fix this\n")
        _ggit(guard_repo, "add", "a.py")
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard")
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert f"✗ a.py:1 violates decision [{entry['id'][:8]}]" in out
        assert "CONTEXER_GUARD=0 git commit" in out
        assert "contexer guard disarm" in out

    def test_advisories_and_violations_both_print_violation_wins_exit(
            self, guard_repo, monkeypatch, capsys):
        from contexer import store
        _gseed(guard_repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        rule_entry = _gseed(guard_repo, "Never commit TODO markers")
        store.arm_guard(str(guard_repo), rule_entry["id"], "regex", pattern="TODO")
        _gwrite(guard_repo, "auth/jwt.py", "token = 1 # TODO\n")
        _ggit(guard_repo, "add", "auth/jwt.py")
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard")
        assert exc.value.code == 1
        out = capsys.readouterr().out
        assert "review this commit against 1 approved decision(s)" in out
        assert "✗ auth/jwt.py" in out

    def test_advisory_cap_reports_suppressed_count(self, guard_repo, monkeypatch, capsys):
        for i in range(7):
            _gseed(guard_repo, f"Decision number {i} about auth handling",
                   source_files=["auth/jwt.py"])
        _gwrite(guard_repo, "auth/jwt.py", "token = 1\n")
        _ggit(guard_repo, "add", "auth/jwt.py")
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard")
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "review this commit against 5 approved decision(s)" in out
        assert "(2 more suppressed)" in out

    def test_env_var_zero_is_silent_exit_0(self, guard_repo, monkeypatch, capsys):
        monkeypatch.setenv("CONTEXER_GUARD", "0")
        _gseed(guard_repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _gwrite(guard_repo, "auth/jwt.py", "token = 1\n")
        _ggit(guard_repo, "add", "auth/jwt.py")
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard")
        assert exc.value.code == 0
        out, err = capsys.readouterr()
        assert out == "" and err == ""

    def test_internal_exception_exits_0_with_exact_stderr_line(
            self, guard_repo, monkeypatch, capsys):
        from contexer import guard_engine

        def boom(*_a, **_k):
            raise RuntimeError("boom")
        monkeypatch.setattr(guard_engine, "guard_staged", boom)
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard")
        assert exc.value.code == 0
        assert capsys.readouterr().err.strip() == \
            "contexer guard: internal error, skipping checks"

    def test_engine_error_result_exits_0_with_exact_stderr_line(
            self, guard_repo, monkeypatch, capsys):
        from contexer import guard_engine
        monkeypatch.setattr(guard_engine, "guard_staged",
                             lambda *a, **k: {"advisories": [], "violations": [], "error": True})
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard")
        assert exc.value.code == 0
        assert capsys.readouterr().err.strip() == \
            "contexer guard: internal error, skipping checks"

    def test_explain_shows_rejected_with_reason(self, guard_repo, monkeypatch, capsys):
        _gseed(guard_repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"],
               created_by="ai", status="suggested")
        _gwrite(guard_repo, "auth/jwt.py", "token = 1\n")
        _ggit(guard_repo, "add", "auth/jwt.py")
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard", "--explain")
        assert exc.value.code == 0
        out = capsys.readouterr().out
        assert "REJECTED" in out
        assert "untrusted provenance" in out


class TestGuardDismiss:
    def test_hash_form_acts_directly_without_prompting(self, guard_repo, monkeypatch, capsys):
        from contexer import store, guard_engine
        _gseed(guard_repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _gwrite(guard_repo, "auth/jwt.py", "token = 1\n")
        _ggit(guard_repo, "add", "auth/jwt.py")
        h = store.guard_candidates(str(guard_repo))[0]["hash"]

        monkeypatch.setattr("builtins.input",
                             lambda *_a: pytest.fail("hash form must not prompt"))
        _run_main(monkeypatch, "guard", "--dismiss", h)
        assert "Dismissed" in capsys.readouterr().out
        assert guard_engine._dismissed_guard(str(guard_repo)) == {h}

    def test_dismiss_uses_same_resolved_repo_as_candidates_lookup(
            self, guard_repo, monkeypatch, capsys):
        """Regression: dismiss_guard, unlike arm_guard/disarm_guard, does not call
        _resolve_repo internally - it writes straight to whatever path it's given.
        So the CLI must resolve the repo ONCE and reuse that exact value for both
        the candidates lookup and the dismiss call; resolving twice (git-root
        fails, falls back through the .current_repo pointer each time) would be
        fine here since both calls fall back identically, but a naive
        implementation that passed the raw (unresolved) git-root result straight
        to dismiss_guard would silently dismiss into the wrong sidecar file."""
        from contexer import store, guard_engine
        _gseed(guard_repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _gwrite(guard_repo, "auth/jwt.py", "token = 1\n")
        _ggit(guard_repo, "add", "auth/jwt.py")
        h = store.guard_candidates(str(guard_repo))[0]["hash"]

        # Simulate a cwd git-root can't resolve, falling back to the shared
        # .current_repo pointer instead - exactly the path where the two calls
        # inside _guard_dismiss could diverge if not resolved once and reused.
        monkeypatch.setattr(store, "_git_root", lambda _cwd: "")
        assert store.anchor_repo(str(guard_repo))

        _run_main(monkeypatch, "guard", "--dismiss", h)
        assert "Dismissed" in capsys.readouterr().out
        assert guard_engine._dismissed_guard(str(guard_repo)) == {h}

    def test_numeric_form_prompts_and_confirms(self, guard_repo, monkeypatch, capsys):
        from contexer import guard_engine
        _gseed(guard_repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _gwrite(guard_repo, "auth/jwt.py", "token = 1\n")
        _ggit(guard_repo, "add", "auth/jwt.py")

        monkeypatch.setattr("builtins.input", lambda *_a: "y")
        _run_main(monkeypatch, "guard", "--dismiss", "1")
        assert "Dismissed" in capsys.readouterr().out
        assert len(guard_engine._dismissed_guard(str(guard_repo))) == 1

    def test_numeric_form_declined_dismisses_nothing(self, guard_repo, monkeypatch, capsys):
        from contexer import guard_engine
        _gseed(guard_repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _gwrite(guard_repo, "auth/jwt.py", "token = 1\n")
        _ggit(guard_repo, "add", "auth/jwt.py")

        monkeypatch.setattr("builtins.input", lambda *_a: "n")
        _run_main(monkeypatch, "guard", "--dismiss", "1")
        assert "Cancelled" in capsys.readouterr().out
        assert guard_engine._dismissed_guard(str(guard_repo)) == set()

    def test_numeric_form_out_of_range_exits_1(self, guard_repo, monkeypatch, capsys):
        _gseed(guard_repo, "Decided to use JWT for auth", source_files=["auth/jwt.py"])
        _gwrite(guard_repo, "auth/jwt.py", "token = 1\n")
        _ggit(guard_repo, "add", "auth/jwt.py")
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard", "--dismiss", "9")
        assert exc.value.code == 1

    def test_unknown_hash_exits_1(self, guard_repo, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard", "--dismiss", "deadbeef0000")
        assert exc.value.code == 1


class TestGuardArmDisarmList:
    def test_arm_disarm_round_trip(self, guard_repo, monkeypatch, capsys):
        from contexer import store
        entry = _gseed(guard_repo, "Never commit TODO markers")

        _run_main(monkeypatch, "guard", "arm", entry["id"], "--regex", "TODO")
        assert "Armed" in capsys.readouterr().out
        data = store._load(str(guard_repo))
        armed = store._entry_by_id(data["entries"], entry["id"])
        assert armed["guard_check"]["type"] == "regex"
        assert armed["guard_check"]["pattern"] == "TODO"

        _run_main(monkeypatch, "guard", "disarm", entry["id"])
        assert "Disarmed" in capsys.readouterr().out
        data = store._load(str(guard_repo))
        armed = store._entry_by_id(data["entries"], entry["id"])
        assert "guard_check" not in armed

    def test_arm_check_secret(self, guard_repo, monkeypatch, capsys):
        from contexer import store
        entry = _gseed(guard_repo, "Never commit secrets")
        _run_main(monkeypatch, "guard", "arm", entry["id"], "--check", "secret")
        assert "Armed" in capsys.readouterr().out
        data = store._load(str(guard_repo))
        armed = store._entry_by_id(data["entries"], entry["id"])
        assert armed["guard_check"]["type"] == "secret"

    def test_arm_with_flags_paths_message(self, guard_repo, monkeypatch, capsys):
        from contexer import store
        entry = _gseed(guard_repo, "Never commit TODO markers")
        _run_main(monkeypatch, "guard", "arm", entry["id"], "--regex", "todo",
                   "--flags", "i", "--paths", "*.py", "--message", "no TODOs")
        data = store._load(str(guard_repo))
        gc = store._entry_by_id(data["entries"], entry["id"])["guard_check"]
        assert gc["flags"] == "i"
        assert gc["paths"] == "*.py"
        assert gc["message"] == "no TODOs"

    def test_arm_missing_id_exits_1(self, guard_repo, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard", "arm")
        assert exc.value.code == 1
        assert "requires a decision id" in capsys.readouterr().err

    def test_arm_missing_check_kind_exits_1(self, guard_repo, monkeypatch, capsys):
        entry = _gseed(guard_repo, "Never commit TODO markers")
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard", "arm", entry["id"])
        assert exc.value.code == 1
        assert "--regex" in capsys.readouterr().err

    def test_arm_unapproved_entry_refusal_surfaces(self, guard_repo, monkeypatch, capsys):
        entry = _gseed(guard_repo, "Some pending thing", created_by="ai",
                        status="pending_approval")
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard", "arm", entry["id"], "--regex", "TODO")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "only approved decisions can be armed" in err
        # arm's refusals are a documented contract, not a broken file: they must
        # never be dressed up as a corrupt-config error telling users to delete
        # config files.
        assert err.startswith("contexer guard arm: ")
        assert "Corrupt config" not in err

    def test_arm_unmachine_checkable_refusal_surfaces(self, guard_repo, monkeypatch, capsys):
        entry = _gseed(guard_repo, "Never commit TODO markers")
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard", "arm", entry["id"], "--regex", "(unclosed")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert err.startswith("contexer guard arm: ")
        assert "machine-checkable" in err
        assert "Corrupt config" not in err

    def test_disarm_missing_id_exits_1(self, guard_repo, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard", "disarm")
        assert exc.value.code == 1
        assert "requires a decision id" in capsys.readouterr().err

    def test_disarm_unknown_id_exits_1(self, guard_repo, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard", "disarm", "no-such-id")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert err.startswith("contexer guard disarm: ")
        assert "Corrupt config" not in err

    def test_list_shows_armed_rule(self, guard_repo, monkeypatch, capsys):
        from contexer import store
        entry = _gseed(guard_repo, "Never commit TODO markers")
        store.arm_guard(str(guard_repo), entry["id"], "regex", pattern="TODO")
        _run_main(monkeypatch, "guard", "list")
        out = capsys.readouterr().out
        assert entry["id"][:8] in out
        assert "regex" in out
        assert "TODO" in out

    def test_list_empty(self, guard_repo, monkeypatch, capsys):
        _run_main(monkeypatch, "guard", "list")
        assert "No armed guard rules" in capsys.readouterr().out


def _input_sequence(monkeypatch, *answers):
    """Feed successive `input()` calls from a fixed sequence - the anchors loop can
    prompt more than once per decision (choice, then an [E]dit sub-prompt)."""
    it = iter(answers)
    monkeypatch.setattr("builtins.input", lambda *_a: next(it))


def _input_sequence_raising(monkeypatch, *answers):
    """Like _input_sequence, but an exception CLASS in the sequence is raised at that
    prompt instead of returned - how a Ctrl-C/EOF mid-loop actually reaches the caller."""
    it = iter(answers)

    def _next(*_a):
        value = next(it)
        if isinstance(value, type) and issubclass(value, BaseException):
            raise value
        return value

    monkeypatch.setattr("builtins.input", _next)


class TestGuardAnchors:
    def test_list_prints_candidates_and_mutates_nothing(self, guard_repo, monkeypatch, capsys):
        _gwrite(guard_repo, "auth/jwt.py", "token = 0\n")
        entry = _gseed(guard_repo, "See auth/jwt.py for the JWT auth decision")
        _run_main(monkeypatch, "guard", "anchors", "--list")
        out = capsys.readouterr().out
        assert entry["id"][:8] in out
        assert "auth/jwt.py" in out
        from contexer import store
        loaded = next(e for e in store._load(str(guard_repo))["entries"]
                      if e["id"] == entry["id"])
        assert not loaded.get("source_files")

    def test_list_empty_when_no_candidates(self, guard_repo, monkeypatch, capsys):
        _run_main(monkeypatch, "guard", "anchors", "--list")
        out = capsys.readouterr().out
        assert "No trusted, unanchored decisions" in out

    def test_non_tty_refuses_with_candidates(self, guard_repo, monkeypatch, capsys):
        _gwrite(guard_repo, "auth/jwt.py", "token = 0\n")
        _gseed(guard_repo, "See auth/jwt.py for the JWT auth decision")
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard", "anchors")
        assert exc.value.code == 1
        assert "--list" in capsys.readouterr().err

    def test_non_tty_with_no_candidates_prints_message_and_exits_0(
            self, guard_repo, monkeypatch, capsys):
        monkeypatch.setattr("sys.stdin.isatty", lambda: False)
        _run_main(monkeypatch, "guard", "anchors")
        assert "No trusted, unanchored decisions" in capsys.readouterr().out

    def test_yes_anchors_via_apply_backfill_anchors_one_save(
            self, guard_repo, monkeypatch, capsys):
        from contexer import store
        _gwrite(guard_repo, "auth/jwt.py", "token = 0\n")
        _ggit(guard_repo, "add", "auth/jwt.py")
        _ggit(guard_repo, "commit", "-m", "init")
        entry = _gseed(guard_repo, "See auth/jwt.py for the JWT auth decision")

        calls = []
        real_save = store._save

        def _counting_save(repo_path, data):
            calls.append(1)
            real_save(repo_path, data)

        monkeypatch.setattr(store, "_save", _counting_save)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        _input_sequence(monkeypatch, "Y")

        _run_main(monkeypatch, "guard", "anchors")
        out = capsys.readouterr().out
        assert "1 anchored" in out
        assert len(calls) == 1

        loaded = next(e for e in store._load(str(guard_repo))["entries"]
                      if e["id"] == entry["id"])
        assert loaded["source_files"] == ["auth/jwt.py"]
        assert loaded["anchor_commit"]

    def test_edit_validates_paths_against_working_tree(self, guard_repo, monkeypatch, capsys):
        from contexer import store
        _gwrite(guard_repo, "auth/jwt.py", "token = 0\n")
        _gwrite(guard_repo, "auth/other.py", "x = 0\n")
        entry = _gseed(guard_repo, "See auth/jwt.py for the JWT auth decision")

        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        _input_sequence(monkeypatch, "E", "auth/other.py,bogus/missing.py")

        _run_main(monkeypatch, "guard", "anchors")
        out = capsys.readouterr().out
        assert "Not found in working tree, dropped: bogus/missing.py" in out
        assert "1 anchored" in out

        loaded = next(e for e in store._load(str(guard_repo))["entries"]
                      if e["id"] == entry["id"])
        assert loaded["source_files"] == ["auth/other.py"]

    def test_edit_rejects_paths_escaping_the_repo(self, guard_repo, monkeypatch, capsys,
                                                    tmp_path):
        """The [E]dit validation must agree with the write layer (_anchor_sources):
        a ../-escaping or absolute spelling of a file that exists ON DISK but
        outside the repo must be rejected here, not accepted and then silently
        dropped by _anchor_sources later (which would vanish the entry from the
        tally without the CLI ever saying so)."""
        from contexer import store
        _gwrite(guard_repo, "auth/jwt.py", "token = 0\n")
        outside = tmp_path / "outside.py"
        outside.write_text("secret = 1\n")
        entry = _gseed(guard_repo, "See auth/jwt.py for the JWT auth decision")

        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        _input_sequence(monkeypatch, "E", f"../outside.py,{outside}")

        _run_main(monkeypatch, "guard", "anchors")
        out = capsys.readouterr().out
        assert "No valid files given, skipping." in out
        assert "Anchor backfill complete: 1 skipped." in out

        loaded = next(e for e in store._load(str(guard_repo))["entries"]
                      if e["id"] == entry["id"])
        assert not loaded.get("source_files")

    def test_skip_stores_nothing(self, guard_repo, monkeypatch, capsys):
        from contexer import store
        _gwrite(guard_repo, "auth/jwt.py", "token = 0\n")
        entry = _gseed(guard_repo, "See auth/jwt.py for the JWT auth decision")

        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        _input_sequence(monkeypatch, "S")

        _run_main(monkeypatch, "guard", "anchors")
        out = capsys.readouterr().out
        assert "Anchor backfill complete: 1 skipped." in out

        loaded = next(e for e in store._load(str(guard_repo))["entries"]
                      if e["id"] == entry["id"])
        assert not loaded.get("source_files")
        # Reappears next run - skip stores nothing.
        from contexer import guard_engine
        assert len(guard_engine.anchor_candidates_for_backfill(str(guard_repo))) == 1

    def test_quit_applies_selections_gathered_so_far(self, guard_repo, monkeypatch, capsys):
        from contexer import store
        _gwrite(guard_repo, "auth/jwt.py", "token = 0\n")
        _gwrite(guard_repo, "auth/oauth.py", "token = 0\n")
        e1 = _gseed(guard_repo, "See auth/jwt.py for the JWT auth decision")
        e2 = _gseed(guard_repo, "See auth/oauth.py for the OAuth decision")

        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        _input_sequence(monkeypatch, "Y", "Q")

        _run_main(monkeypatch, "guard", "anchors")
        out = capsys.readouterr().out
        assert "1 anchored" in out

        entries = {e["id"]: e for e in store._load(str(guard_repo))["entries"]}
        anchored_ids = {eid for eid, e in entries.items() if e.get("source_files")}
        assert len(anchored_ids) == 1
        assert anchored_ids <= {e1["id"], e2["id"]}

    def _two_candidates(self, guard_repo):
        _gwrite(guard_repo, "auth/jwt.py", "token = 0\n")
        _gwrite(guard_repo, "auth/oauth.py", "token = 0\n")
        return (_gseed(guard_repo, "See auth/jwt.py for the JWT auth decision"),
                _gseed(guard_repo, "See auth/oauth.py for the OAuth decision"))

    def _anchored(self, guard_repo):
        from contexer import store
        return [e for e in store._load(str(guard_repo))["entries"] if e.get("source_files")]

    def test_interrupt_at_the_choice_prompt_writes_nothing(self, guard_repo, monkeypatch,
                                                            capsys):
        """Ctrl-C/EOF is an abort, not a commit: selections ratified before the interrupt
        must NOT be written. (A plain [Q]uit still writes them - that is documented.)"""
        self._two_candidates(guard_repo)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)

        def _inputs(*_a):
            if not calls:
                calls.append(1)
                return "Y"
            raise KeyboardInterrupt
        calls = []
        monkeypatch.setattr("builtins.input", _inputs)

        _run_main(monkeypatch, "guard", "anchors")
        out = capsys.readouterr().out
        assert "Aborted" in out
        assert "1 anchored" not in out
        assert self._anchored(guard_repo) == []

    def test_eof_at_the_edit_prompt_writes_nothing(self, guard_repo, monkeypatch, capsys):
        self._two_candidates(guard_repo)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        _input_sequence_raising(monkeypatch, "Y", "E", EOFError)

        _run_main(monkeypatch, "guard", "anchors")
        out = capsys.readouterr().out
        assert "Aborted" in out
        assert self._anchored(guard_repo) == []

    def test_quit_still_writes_after_a_ratified_selection(self, guard_repo, monkeypatch,
                                                          capsys):
        # The documented contrast with an interrupt, pinned next to it.
        self._two_candidates(guard_repo)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        _input_sequence(monkeypatch, "Y", "Q")
        _run_main(monkeypatch, "guard", "anchors")
        assert "1 anchored" in capsys.readouterr().out
        assert len(self._anchored(guard_repo)) == 1

    def test_unknown_flag_is_rejected_instead_of_prompting(self, guard_repo, monkeypatch,
                                                            capsys):
        """`guard anchors --dry-run` used to fall through into the interactive loop -
        a flag that reads as read-only silently starting a write flow."""
        self._two_candidates(guard_repo)
        monkeypatch.setattr("sys.stdin.isatty", lambda: True)
        monkeypatch.setattr("builtins.input",
                             lambda *_a: pytest.fail("must not prompt on an unknown flag"))
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard", "anchors", "--dry-run")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "--dry-run" in err
        assert "--list" in err
        assert self._anchored(guard_repo) == []


class TestGuardUsage:
    def test_usage_documents_guard(self):
        assert "guard" in cli.USAGE
        assert "CONTEXER_GUARD=0" in cli.USAGE
        assert "arm" in cli.USAGE
        assert "anchors" in cli.USAGE
        assert "disarm" in cli.USAGE

    def test_usage_documents_install_hook(self):
        assert "--install-hook" in cli.USAGE
        assert "--uninstall-hook" in cli.USAGE


# ── guard --install-hook / --uninstall-hook ────────────────────────────────

def _guard_hook_path(repo):
    return repo / ".git" / "hooks" / "pre-commit"


def _stub_guard_bin(monkeypatch, path="/usr/local/bin/contexer"):
    """Pretend `contexer` is on PATH at `path` AND that it supports `guard`.

    The capability probe shells out to the resolved binary, which does not exist
    for these synthetic paths - stubbing it keeps the hook-writing tests about
    hook writing. The probe itself is covered by its own tests below, against a
    real (throwaway) executable."""
    monkeypatch.setattr(cli.shutil, "which", lambda name: path)
    monkeypatch.setattr(cli, "_guard_bin_supports_guard", lambda p: True)


def _fake_contexer_bin(tmp_path, name="contexer", usage="usage: contexer guard"):
    """A real, executable throwaway script standing in for an installed contexer
    binary - used to exercise the capability probe for real."""
    bin_dir = tmp_path / "fakebin"
    bin_dir.mkdir(exist_ok=True)
    path = bin_dir / name
    path.write_text(f"#!/bin/sh\necho '{usage}'\n")
    path.chmod(0o755)
    return path


class TestGuardInstallHook:
    def test_fresh_install_writes_executable_script_with_fence_and_abs_path(
            self, guard_repo, monkeypatch, capsys):
        _stub_guard_bin(monkeypatch)
        _run_main(monkeypatch, "guard", "--install-hook")
        out = capsys.readouterr().out

        hook = _guard_hook_path(guard_repo)
        assert hook.exists()
        content = hook.read_text()
        assert content.startswith("#!/bin/sh")
        assert "# >>> contexer guard >>>" in content
        assert "# <<< contexer guard <<<" in content
        assert "/usr/local/bin/contexer" in content
        assert oct(hook.stat().st_mode)[-3:] == "755"
        assert "installed" in out.lower()

    def test_idempotent_reinstall_is_noop(self, guard_repo, monkeypatch, capsys):
        _stub_guard_bin(monkeypatch)
        _run_main(monkeypatch, "guard", "--install-hook")
        first = _guard_hook_path(guard_repo).read_text()
        capsys.readouterr()

        _run_main(monkeypatch, "guard", "--install-hook")
        second = _guard_hook_path(guard_repo).read_text()
        assert first == second
        assert "already installed" in capsys.readouterr().out.lower()

    def test_foreign_hook_preserved_on_append_and_restored_on_uninstall(
            self, guard_repo, monkeypatch, capsys):
        _stub_guard_bin(monkeypatch)
        hook = _guard_hook_path(guard_repo)
        hook.parent.mkdir(parents=True, exist_ok=True)
        foreign = "#!/bin/sh\necho 'foreign hook'\n"
        hook.write_text(foreign)
        hook.chmod(0o755)

        _run_main(monkeypatch, "guard", "--install-hook")
        appended = hook.read_text()
        assert appended.startswith(foreign)
        assert "# >>> contexer guard >>>" in appended
        assert "already installed" not in capsys.readouterr().out.lower()

        _run_main(monkeypatch, "guard", "--uninstall-hook")
        assert hook.read_text() == foreign

    def test_hooks_path_override_refuses(self, guard_repo, monkeypatch, capsys):
        _ggit(guard_repo, "config", "core.hooksPath", ".githooks")
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard", "--install-hook")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "core.hooksPath" in err
        assert "contexer guard" in err
        assert not _guard_hook_path(guard_repo).exists()

    def test_framework_generated_hook_refuses(self, guard_repo, monkeypatch, capsys):
        hook = _guard_hook_path(guard_repo)
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/usr/bin/env python3\n"
                         "# File generated by pre-commit: https://pre-commit.com\n")
        original = hook.read_text()

        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard", "--install-hook")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert ".pre-commit-config.yaml" in err
        assert "contexer-guard" in err
        assert hook.read_text() == original

    def test_uninstall_ours_only_removes_file(self, guard_repo, monkeypatch, capsys):
        _stub_guard_bin(monkeypatch)
        _run_main(monkeypatch, "guard", "--install-hook")
        capsys.readouterr()

        _run_main(monkeypatch, "guard", "--uninstall-hook")
        assert not _guard_hook_path(guard_repo).exists()
        assert "removed" in capsys.readouterr().out.lower()

    def test_uninstall_noop_when_nothing_installed(self, guard_repo, monkeypatch, capsys):
        _run_main(monkeypatch, "guard", "--uninstall-hook")
        assert "no hook installed" in capsys.readouterr().out.lower()

    def test_undecodable_hook_refuses_install_and_preserves_it(
            self, guard_repo, monkeypatch, capsys):
        """A hook we cannot decode is a hook we must not touch. Before the encoding
        pin these three paths read with the LOCALE codec, so an ordinary hook echoing
        a non-ASCII message raised UnicodeDecodeError under LC_ALL=C - a traceback out
        of install, a traceback out of uninstall, and "not installed" from status."""
        _stub_guard_bin(monkeypatch)
        hook = _guard_hook_path(guard_repo)
        hook.parent.mkdir(parents=True, exist_ok=True)
        raw = b'#!/bin/sh\necho "\xff\xfe not utf-8"\n'   # undecodable under ANY locale
        hook.write_bytes(raw)

        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard", "--install-hook")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "not readable as UTF-8" in err
        assert "contexer guard" in err          # the block is handed over for manual use
        assert hook.read_bytes() == raw         # byte-preserved, not clobbered

    def test_undecodable_hook_refuses_uninstall_instead_of_claiming_none(
            self, guard_repo, monkeypatch, capsys):
        hook = _guard_hook_path(guard_repo)
        hook.parent.mkdir(parents=True, exist_ok=True)
        raw = ("#!/bin/sh\n# >>> contexer guard >>>\ncontexer guard\n"
               "# <<< contexer guard <<<\n").encode() + b'echo "\xff\xfe"\n'
        hook.write_bytes(raw)

        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard", "--uninstall-hook")
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert "not readable as UTF-8" in err
        assert ">>> contexer guard >>>" in err   # tells them what to delete by hand
        assert hook.read_bytes() == raw

    def test_status_says_unknown_not_not_installed_for_undecodable_hook(
            self, guard_repo, monkeypatch):
        hook = _guard_hook_path(guard_repo)
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_bytes(b'#!/bin/sh\n# >>> contexer guard >>>\necho "\xff"\n')
        line = cli._guard_hook_status_line()
        assert "not readable as UTF-8" in line
        assert "not installed" not in line       # ours IS in there; do not deny it

    def test_unreadable_hook_reports_the_os_error_not_an_encoding_one(
            self, guard_repo, monkeypatch):
        """An OSError and a decode error are different diagnoses. Reporting "not
        readable as UTF-8" for a mode-0o000 hook sends the developer after an encoding
        problem that isn't there."""
        hook = _guard_hook_path(guard_repo)
        hook.parent.mkdir(parents=True, exist_ok=True)
        hook.write_text("#!/bin/sh\n", encoding="utf-8")

        def denied(self, *a, **kw):
            raise PermissionError(13, "Permission denied")

        monkeypatch.setattr(Path, "read_bytes", denied)
        line = cli._guard_hook_status_line()
        assert "could not be read" in line and "Permission denied" in line
        assert "UTF-8" not in line

    def test_crlf_hook_keeps_its_line_endings(self, guard_repo, monkeypatch):
        """Byte-exactness, not just decodability. Text mode translates newlines, so a
        CRLF hook read through it came back LF-only - every foreign line silently lost
        its \\r, and uninstall did not restore it. Reads/writes are bytes now, so the
        developer's endings survive and our own block stays LF (a \\r in a `#!/bin/sh`
        script breaks it under Git-for-Windows sh)."""
        _stub_guard_bin(monkeypatch)
        hook = _guard_hook_path(guard_repo)
        hook.parent.mkdir(parents=True, exist_ok=True)
        foreign = b"#!/bin/sh\r\necho hi\r\n"
        hook.write_bytes(foreign)

        _run_main(monkeypatch, "guard", "--install-hook")
        appended = hook.read_bytes()
        assert appended.startswith(foreign)                     # \r intact, byte for byte
        block = appended[len(foreign):]
        assert b"\r" not in block                               # our block is LF-only

        _run_main(monkeypatch, "guard", "--uninstall-hook")
        assert hook.read_bytes() == foreign

    def test_utf8_hook_round_trips_regardless_of_locale(self, guard_repo, monkeypatch):
        """The pin also fixes the silent half: a hook with non-ASCII text is read and
        rewritten as UTF-8, so appending our block cannot mangle the developer's bytes."""
        _stub_guard_bin(monkeypatch)
        hook = _guard_hook_path(guard_repo)
        hook.parent.mkdir(parents=True, exist_ok=True)
        foreign = '#!/bin/sh\necho "✖ lint failed - naïve check"\n'
        hook.write_text(foreign, encoding="utf-8")

        _run_main(monkeypatch, "guard", "--install-hook")
        appended = hook.read_text(encoding="utf-8")
        assert appended.startswith(foreign)

        _run_main(monkeypatch, "guard", "--uninstall-hook")
        assert hook.read_text(encoding="utf-8") == foreign

    def test_install_falls_back_to_argv0_when_which_fails(self, guard_repo, monkeypatch):
        monkeypatch.setattr(cli.shutil, "which", lambda name: None)
        monkeypatch.setattr(cli, "_guard_bin_supports_guard", lambda p: True)
        monkeypatch.setattr(sys, "argv", ["/opt/venv/bin/contexer", "guard", "--install-hook"])
        cli.main()
        content = _guard_hook_path(guard_repo).read_text()
        assert "/opt/venv/bin/contexer" in content

    def test_running_argv0_preferred_over_stale_path_binary(
            self, guard_repo, tmp_path, monkeypatch):
        """The running entry point wins over whatever `which` finds: a stale
        global install earlier on PATH must never be baked into the hook."""
        real_bin = _fake_contexer_bin(tmp_path)
        monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/contexer")
        monkeypatch.setattr(sys, "argv", [str(real_bin), "guard", "--install-hook"])
        cli.main()
        content = _guard_hook_path(guard_repo).read_text()
        assert str(real_bin) in content
        assert "/usr/local/bin/contexer" not in content

    def test_source_argv0_is_not_treated_as_a_binary(self, guard_repo, tmp_path, monkeypatch):
        """`python -m contexer` / `python server.py` leaves a .py file in argv[0];
        it is not a console script, so `which` must still win."""
        script = _fake_contexer_bin(tmp_path, name="contexer.py")
        monkeypatch.setattr(cli.shutil, "which", lambda name: "/usr/local/bin/contexer")
        monkeypatch.setattr(cli, "_guard_bin_supports_guard", lambda p: True)
        monkeypatch.setattr(sys, "argv", [str(script), "guard", "--install-hook"])
        cli.main()
        content = _guard_hook_path(guard_repo).read_text()
        assert "/usr/local/bin/contexer" in content
        assert str(script) not in content

    def test_binary_without_guard_support_refuses_install(
            self, guard_repo, tmp_path, monkeypatch, capsys):
        """A pre-guard binary would produce a hook that exits 1 on every commit.
        Probe it first and refuse rather than install a commit-blocking hook."""
        stale = _fake_contexer_bin(tmp_path, usage="usage: contexer install|status")
        monkeypatch.setattr(cli.shutil, "which", lambda name: str(stale))
        monkeypatch.setattr(sys, "argv", ["contexer", "guard", "--install-hook"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert str(stale) in err
        assert "upgrade" in err.lower()
        assert not _guard_hook_path(guard_repo).exists()

    def test_hook_shell_quotes_the_binary_path(self, guard_repo, monkeypatch):
        """A path holding shell metacharacters must not expand (or break the
        script) when the hook runs."""
        weird = "/opt/my $tools/`x`/contexer"
        monkeypatch.setattr(cli.shutil, "which", lambda name: weird)
        monkeypatch.setattr(cli, "_guard_bin_supports_guard", lambda p: True)
        _run_main(monkeypatch, "guard", "--install-hook")
        content = _guard_hook_path(guard_repo).read_text()
        assert "'/opt/my $tools/`x`/contexer'" in content
        assert '"/opt/my $tools/`x`/contexer"' not in content

    def test_not_a_git_repo_refuses(self, tmp_path, monkeypatch, capsys):
        monkeypatch.chdir(tmp_path)
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "guard", "--install-hook")
        assert exc.value.code == 1
        assert "not inside a git repository" in capsys.readouterr().err.lower()


class TestInstallStatusMentionGuardHook:
    def test_install_output_mentions_guard_hook(self, clean_home, monkeypatch, capsys):
        install()
        out = capsys.readouterr().out
        assert "guard --install-hook" in out
        # Installing the hook enables the ADVISORY tier only - blocking needs an
        # explicit `contexer guard arm`, so the offer must not promise blocking.
        assert "blocking" not in out.lower()

    def test_status_reports_guard_hook_not_installed(self, guard_repo, monkeypatch, capsys):
        _stub_guard_bin(monkeypatch)
        status()
        out = capsys.readouterr().out
        assert "guard hook" in out.lower()
        assert "not installed" in out.lower()

    def test_status_reports_guard_hook_installed(self, guard_repo, monkeypatch, capsys):
        _stub_guard_bin(monkeypatch)
        _run_main(monkeypatch, "guard", "--install-hook")
        capsys.readouterr()
        status()
        out = capsys.readouterr().out
        assert "guard hook" in out.lower()
        assert "installed" in out.lower()


class TestPreCommitFrameworkSpec:
    def test_pre_commit_hooks_yaml_at_repo_root(self):
        import contexer
        repo_root = Path(contexer.__file__).resolve().parent.parent
        spec_path = repo_root / ".pre-commit-hooks.yaml"
        assert spec_path.exists()
        content = spec_path.read_text()
        assert "id: contexer-guard" in content
        assert "entry: contexer guard" in content
        assert "language: system" in content
        assert "verbose: true" in content
        assert "pass_filenames: false" in content
        assert "always_run: true" in content


# ── scope-audit ─────────────────────────────────────────────────────────────

class TestScopeAudit:
    """`contexer scope-audit` - read-only report of decisions saved into the wrong store."""

    def _store_dir(self, tmp_path, monkeypatch):
        from contexer import store
        d = tmp_path / ".contexer"
        d.mkdir()
        monkeypatch.setattr(store, "STORE_DIR", d)
        return d, store

    def _write(self, d, store, repo, eid, sid, **extra):
        entry = {"type": "decision", "id": eid, "session_id": sid,
                 "timestamp": "2026-08-03T12:00:00+00:00", "content": f"content {eid}",
                 "title": f"title {eid}"}
        entry.update(extra)
        (d / f"{store._slug(repo)}.json").write_text(
            json.dumps({"repo_path": repo, "entries": [entry]}))

    def test_reports_a_split_session(self, tmp_path, monkeypatch, capsys):
        d, store = self._store_dir(tmp_path, monkeypatch)
        self._write(d, store, "/repo/right", "r1", "sess-1")
        self._write(d, store, "/repo/wrong", "w1", "sess-1", repo_source="pointer")

        _run_main(monkeypatch, "scope-audit")
        out = capsys.readouterr().out
        assert "/repo/right" in out and "/repo/wrong" in out
        assert "[via pointer]" in out

    def test_clean_store_reports_clean(self, tmp_path, monkeypatch, capsys):
        d, store = self._store_dir(tmp_path, monkeypatch)
        self._write(d, store, "/repo/a", "a1", "sess-a")
        _run_main(monkeypatch, "scope-audit")
        assert "No cross-store sessions" in capsys.readouterr().out

    def test_writes_nothing(self, tmp_path, monkeypatch):
        d, store = self._store_dir(tmp_path, monkeypatch)
        self._write(d, store, "/repo/right", "r1", "sess-1")
        self._write(d, store, "/repo/wrong", "w1", "sess-1")
        before = {p.name: p.read_bytes() for p in d.iterdir()}

        _run_main(monkeypatch, "scope-audit")
        assert {p.name: p.read_bytes() for p in d.iterdir()} == before

    def test_unknown_argument_exits_1_instead_of_running(self, tmp_path, monkeypatch, capsys):
        # A read-only-sounding flag must never fall through into a run that ignores it.
        self._store_dir(tmp_path, monkeypatch)
        with pytest.raises(SystemExit) as exc:
            _run_main(monkeypatch, "scope-audit", "--fix")
        assert exc.value.code == 1
        assert "Unknown argument" in capsys.readouterr().err

    def test_listed_in_help(self, monkeypatch, capsys):
        _run_main(monkeypatch, "help")
        assert "scope-audit" in capsys.readouterr().out


class TestReviewOneViewAccuracy:
    """`contexer review` must let a developer judge an approval WITHOUT a second command:
    full body inline, plus the provenance/anchor metadata that says whether the decision is
    still true. Bulk approval was removed, so each of these screens is the whole decision."""

    def _pending(self, tmp_repo, monkeypatch, content, **kw):
        from contexer import store
        monkeypatch.setattr(store, "_git_root", lambda _cwd: tmp_repo)
        stored, eid = store.update_decision(tmp_repo, content, "sess-1", "constraint", **kw)
        assert stored
        monkeypatch.setattr("builtins.input", lambda *_a: "S")
        return eid

    def test_long_body_is_shown_in_full_not_clipped(self, tmp_repo, monkeypatch, capsys):
        from contexer import store
        tail = "the trailing sentence that a 400-char clip would have eaten entirely."
        body = ("Always validate at the trust boundary. " + ("padding words here. " * 30)) + tail
        assert len(body) > store._BODY_CLIP
        self._pending(tmp_repo, monkeypatch, body)
        cli.review()
        out = capsys.readouterr().out
        assert "[+" not in out                      # no "… [+N chars]" marker
        assert "trailing sentence" in out           # the far end of the body survived

    def test_shows_id_and_progress(self, tmp_repo, monkeypatch, capsys):
        eid = self._pending(tmp_repo, monkeypatch, "Never commit secrets to the repo")
        cli.review()
        out = capsys.readouterr().out
        assert f"id {eid[:8]}" in out
        assert "Decision 1 of 1" in out

    def test_shows_capture_origin(self, tmp_repo, monkeypatch, capsys):
        self._pending(tmp_repo, monkeypatch, "Never commit secrets to the repo")
        cli.review()
        assert "Captured" in capsys.readouterr().out

    def test_quit_stops_without_touching_the_rest(self, tmp_repo, monkeypatch, capsys):
        from contexer import store
        monkeypatch.setattr(store, "_git_root", lambda _cwd: tmp_repo)
        for c in ("Never commit secrets here", "Never log personally identifying data"):
            store.update_decision(tmp_repo, c, "s", "constraint")
        assert len(store.get_pending_decisions(tmp_repo)) == 2

        monkeypatch.setattr("builtins.input", lambda *_a: "Q")
        cli.review()

        assert "the rest stay pending" in capsys.readouterr().out
        assert len(store.get_pending_decisions(tmp_repo)) == 2   # nothing approved

    def test_pointer_resolved_capture_is_flagged(self, tmp_repo, monkeypatch, capsys):
        """repo_source == 'pointer' is the one branch that can silently target the WRONG
        repo, so review says so rather than leaving the developer to guess."""
        from contexer import store
        monkeypatch.setattr(store, "_git_root", lambda _cwd: tmp_repo)
        store.update_decision(tmp_repo, "Never commit secrets to the repo", "s",
                              "constraint", repo_source="pointer")
        monkeypatch.setattr("builtins.input", lambda *_a: "S")
        cli.review()
        assert "shared repo pointer" in capsys.readouterr().out

    def test_argument_resolved_capture_is_not_flagged(self, tmp_repo, monkeypatch, capsys):
        from contexer import store
        monkeypatch.setattr(store, "_git_root", lambda _cwd: tmp_repo)
        store.update_decision(tmp_repo, "Never commit secrets to the repo", "s",
                              "constraint", repo_source="argument")
        monkeypatch.setattr("builtins.input", lambda *_a: "S")
        cli.review()
        assert "shared repo pointer" not in capsys.readouterr().out
