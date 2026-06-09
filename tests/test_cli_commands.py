"""Tests for the contexer CLI management commands: version, status, reinstall,
uninstall --purge, help, and the main() dispatch."""
import json
import os
import sys
import time

import pytest

from contexer import cli
from contexer.cli import install, reinstall, status, uninstall, version


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
        assert "registered" in out and "NOT registered" not in out
        assert "hooks:        installed" in out
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
        uninstall(purge=True)
        assert not (installed_home / ".contexer").exists()

    def test_purge_when_store_absent(self, installed_home, capsys):
        import shutil
        shutil.rmtree(installed_home / ".contexer")
        uninstall(purge=True)  # must not raise
        assert "No store to purge" in capsys.readouterr().out

    def test_default_preserves_store(self, installed_home):
        uninstall()  # purge defaults to False
        assert (installed_home / ".contexer").exists()


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
        _run_main(monkeypatch, "uninstall", "--purge")
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
        assert "hooks:        installed" in capsys.readouterr().out

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
