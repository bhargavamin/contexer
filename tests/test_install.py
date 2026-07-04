"""Tests for contexer CLI install/uninstall commands."""
import json
import sys
from pathlib import Path

import pytest

from contexer.adapters import claude
from contexer.cli import install, uninstall


@pytest.fixture
def clean_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


@pytest.fixture
def installed_home(clean_home):
    install()
    return clean_home


# ── install ───────────────────────────────────────────────────────────────────

class TestInstall:
    def test_creates_claude_json_with_mcp_entry(self, installed_home):
        claude = json.loads((installed_home / ".claude.json").read_text())
        entry = claude["mcpServers"]["contexer"]
        assert entry["type"] == "stdio"
        assert "contexer" in entry["command"]

    def test_creates_settings_json(self, installed_home):
        assert (installed_home / ".claude" / "settings.json").exists()

    def test_session_start_hook_registered(self, installed_home):
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        cmds = [h["command"] for grp in settings["hooks"]["SessionStart"]
                for h in grp["hooks"] if "command" in h]
        assert any("get_session_start_context" in c for c in cmds)

    def test_pre_compact_hook_registered(self, installed_home):
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        cmds = [h["command"] for grp in settings["hooks"]["PreCompact"]
                for h in grp["hooks"] if "command" in h]
        assert any("compaction starting" in c for c in cmds)

    def test_pre_compact_flushes_memory(self, installed_home):
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        cmds = [h["command"] for grp in settings["hooks"]["PreCompact"]
                for h in grp["hooks"] if "command" in h]
        # PreCompact must both flush memory and still emit the reminder.
        assert any("sync_memory" in c and "compaction starting" in c for c in cmds)

    def test_session_start_flushes_memory(self, installed_home):
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        cmds = [h["command"] for grp in settings["hooks"]["SessionStart"]
                for h in grp["hooks"] if "command" in h]
        assert any("sync_memory" in c for c in cmds)

    def test_session_end_hook_registered(self, installed_home):
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        cmds = [h["command"] for grp in settings["hooks"]["SessionEnd"]
                for h in grp["hooks"] if "command" in h]
        assert any("sync_memory" in c for c in cmds)

    def test_post_compact_hook_not_registered(self, installed_home):
        # PostCompact cannot inject context (no additionalContext; systemMessage is
        # user-facing only), so a PostCompact hook was pure visible noise on /compact.
        # SessionStart(source="compact") owns the silent reload — no PostCompact hook.
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        assert not settings["hooks"].get("PostCompact"), \
            "PostCompact hook must not be wired (SessionStart source=compact reloads silently)"

    def test_user_prompt_submit_anchor_registered(self, installed_home):
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        ups = settings["hooks"]["UserPromptSubmit"]
        cmds = [h["command"] for grp in ups for h in grp["hooks"] if "command" in h]
        assert any(".current_repo" in c for c in cmds)

    def test_user_prompt_submit_bootstrap_registered(self, installed_home):
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        ups = settings["hooks"]["UserPromptSubmit"]
        cmds = [h["command"] for grp in ups for h in grp["hooks"] if "command" in h]
        assert any("get_bootstrap_context_prompt" in c for c in cmds)

    def test_constraint_and_rationale_command_hooks_registered(self, installed_home):
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        ups = settings["hooks"]["UserPromptSubmit"]
        cmds = [h["command"] for grp in ups for h in grp["hooks"] if "command" in h]
        assert any("claude.capture_constraint" in c for c in cmds)
        assert any("claude.rationale" in c for c in cmds)
        # capture hooks are command hooks now, never mcp_tool
        assert not any(h.get("type") == "mcp_tool" for grp in ups for h in grp["hooks"])

    def test_permissions_added(self, installed_home):
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        allow = settings["permissions"]["allow"]
        for p in ["mcp__contexer__update_context",
                  "mcp__contexer__get_context", "mcp__contexer__bootstrap_context"]:
            assert p in allow

    def test_store_dir_created(self, installed_home):
        assert (installed_home / ".contexer").is_dir()

    def test_install_is_idempotent(self, installed_home):
        install()  # second install

        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        ups = settings["hooks"]["UserPromptSubmit"]
        cmds = [h.get("command", "") for grp in ups for h in grp["hooks"]]
        assert sum("claude.capture_constraint" in c for c in cmds) == 1, \
            "capture_constraint hook must not be duplicated"
        allow = settings["permissions"]["allow"]
        assert allow.count("mcp__contexer__update_context") == 1

    def test_install_preserves_existing_settings(self, clean_home):
        settings_path = clean_home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(json.dumps({"effortLevel": "xhigh", "agentPushNotifEnabled": True}))

        install()

        settings = json.loads(settings_path.read_text())
        assert settings["effortLevel"] == "xhigh"
        assert settings["agentPushNotifEnabled"] is True
        assert "hooks" in settings

    def test_no_stop_hook_installed(self, installed_home):
        # The Stop hook was removed: end-of-turn prompting is replaced by the deterministic
        # PostToolUse flag + next-prompt anchor reminder.
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        stop_groups = settings["hooks"].get("Stop", [])
        cmds = [h.get("command", "") for grp in stop_groups for h in grp.get("hooks", [])]
        assert not any(".pending_capture" in c for c in cmds), \
            "Contexer must not install a Stop hook"

    def test_post_tool_use_flag_hook_still_registered(self, installed_home):
        # The deterministic write/edit signal must survive Stop-hook removal.
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        cmds = [h["command"] for grp in settings["hooks"]["PostToolUse"]
                for h in grp["hooks"] if "command" in h]
        assert any(".pending_capture" in c for c in cmds)

    def test_install_removes_preexisting_contexer_stop_hook(self, clean_home):
        # A user upgrading from a version that installed the Stop hook: reinstall must strip it.
        settings_path = clean_home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(json.dumps({"hooks": {"Stop": [
            {"hooks": [{"type": "command",
                        "command": "rm -f $HOME/.contexer/.pending_capture; echo '{}'"}]}]}}))

        install()

        settings = json.loads(settings_path.read_text())
        stop_groups = settings["hooks"].get("Stop", [])
        cmds = [h.get("command", "") for grp in stop_groups for h in grp.get("hooks", [])]
        assert not any(".pending_capture" in c for c in cmds), \
            "reinstall must remove a previously-installed Contexer Stop hook"

    def test_install_preserves_foreign_stop_hook(self, clean_home):
        # A user's own Stop hook (not Contexer's) must be left untouched.
        settings_path = clean_home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(json.dumps({"hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": "./my-own-stop.sh"}]}]}}))

        install()

        settings = json.loads(settings_path.read_text())
        cmds = [h.get("command", "") for grp in settings["hooks"]["Stop"]
                for h in grp.get("hooks", [])]
        assert "./my-own-stop.sh" in cmds


# ── uninstall ─────────────────────────────────────────────────────────────────

class TestUninstall:
    def test_removes_mcp_entry(self, installed_home):
        uninstall()
        claude = json.loads((installed_home / ".claude.json").read_text())
        assert "contexer" not in claude.get("mcpServers", {})

    def test_removes_session_start_hook(self, installed_home):
        uninstall()
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        ss = settings.get("hooks", {}).get("SessionStart", [])
        cmds = [h.get("command", "") for grp in ss for h in grp.get("hooks", [])]
        assert not any("get_session_start_context" in c for c in cmds)

    def test_removes_session_end_hook(self, installed_home):
        uninstall()
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        assert "SessionEnd" not in settings.get("hooks", {})

    def test_removes_permissions(self, installed_home):
        uninstall()
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        allow = settings.get("permissions", {}).get("allow", [])
        assert not any("contexer" in p for p in allow)

    def test_preserves_store_directory(self, installed_home):
        (installed_home / ".contexer" / "test.json").write_text("{}")
        uninstall()
        assert (installed_home / ".contexer" / "test.json").exists()

    def test_preserves_non_contexer_settings(self, clean_home):
        settings_path = clean_home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(json.dumps({"effortLevel": "xhigh"}))

        install()
        uninstall()

        settings = json.loads(settings_path.read_text())
        assert settings.get("effortLevel") == "xhigh"

    def test_uninstall_is_idempotent(self, installed_home):
        uninstall()
        uninstall()  # second uninstall should not raise


class TestMemorySyncMigration:
    """Old installs predate memory-tool sync; reinstall must upgrade them in place."""

    def _write_legacy(self, home):
        settings_path = home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps({"hooks": {
            "SessionStart": [{"hooks": [{"type": "command",
                "command": "py -c 'store.get_session_start_context(repo, store.source_from_hook_stdin(x))'"}]}],
            "PreCompact": [{"hooks": [{"type": "command",
                "command": "echo '{\"systemMessage\": \"Contexer: context compaction starting\"}'"}]}],
        }}))
        return settings_path

    def test_session_start_upgraded_to_flush_memory(self, clean_home):
        path = self._write_legacy(clean_home)
        install()
        cmds = [h["command"] for grp in json.loads(path.read_text())["hooks"]["SessionStart"]
                for h in grp["hooks"]]
        assert any("sync_memory" in c for c in cmds)
        # not duplicated — exactly one SessionStart group
        assert len(json.loads(path.read_text())["hooks"]["SessionStart"]) == 1

    def test_pre_compact_upgraded_to_flush_memory(self, clean_home):
        path = self._write_legacy(clean_home)
        install()
        cmds = [h["command"] for grp in json.loads(path.read_text())["hooks"]["PreCompact"]
                for h in grp["hooks"]]
        assert any("sync_memory" in c for c in cmds)
        assert len(json.loads(path.read_text())["hooks"]["PreCompact"]) == 1


class TestTargetSelection:
    def test_install_target_cursor_only(self, clean_home, monkeypatch):
        import contexer.cli as cli
        monkeypatch.setattr(sys, "argv", ["contexer", "install", "--target", "cursor"])
        cli.main()
        assert (clean_home / ".cursor" / "mcp.json").exists()
        assert not (clean_home / ".claude.json").exists()

    def test_install_target_gemini_only(self, clean_home, monkeypatch):
        import contexer.cli as cli
        monkeypatch.setattr(sys, "argv", ["contexer", "install", "--target", "gemini"])
        cli.main()
        assert (clean_home / ".gemini" / "settings.json").exists()
        assert not (clean_home / ".claude.json").exists()

    def test_install_target_all(self, clean_home, monkeypatch):
        import contexer.cli as cli
        monkeypatch.setattr(sys, "argv", ["contexer", "install", "--target", "all"])
        cli.main()
        assert (clean_home / ".cursor" / "mcp.json").exists()
        assert (clean_home / ".gemini" / "settings.json").exists()
        assert (clean_home / ".claude.json").exists()

    def test_install_autodetects_present_tools(self, clean_home, monkeypatch):
        # Only ~/.cursor present -> only Cursor wired.
        (clean_home / ".cursor").mkdir()
        import contexer.cli as cli
        monkeypatch.setattr(sys, "argv", ["contexer", "install"])
        cli.main()
        assert (clean_home / ".cursor" / "mcp.json").exists()
        assert not (clean_home / ".claude.json").exists()

    def test_install_defaults_to_claude_when_none_detected(self, clean_home, monkeypatch):
        import contexer.cli as cli
        monkeypatch.setattr(sys, "argv", ["contexer", "install"])
        cli.main()
        assert (clean_home / ".claude.json").exists()

    def test_install_unknown_target_exits_1(self, clean_home, monkeypatch, capsys):
        import contexer.cli as cli
        monkeypatch.setattr(sys, "argv", ["contexer", "install", "--target", "emacs"])
        with pytest.raises(SystemExit) as e:
            cli.main()
        assert e.value.code == 1
        assert "unknown target" in capsys.readouterr().err.lower()


class TestStaleHookHealing:
    """Reinstall must remove a from-source hook (the dead `uv run --directory <clone>`
    variant) rather than leaving it alongside the current one (review finding C1)."""

    _LEGACY_POC = (
        'REPO=/clone && uv run --directory /clone python -c '
        '"import store; print(\'Contexer: 3 decision(s) available — run get_context\')" ""'
    )

    def _seed_from_source(self, home):
        settings_path = home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps({"hooks": {
            "PostCompact": [{"hooks": [{"type": "command", "command": self._LEGACY_POC}]}],
        }}))
        return settings_path

    def test_legacy_postcompact_removed_on_install(self, clean_home):
        # Install now strips every Contexer PostCompact hook (the event can't inject
        # context; SessionStart source=compact reloads silently) — the dead from-source
        # hook and the whole PostCompact key are gone, not replaced.
        path = self._seed_from_source(clean_home)
        install()
        poc = json.loads(path.read_text()).get("hooks", {}).get("PostCompact", [])
        cmds = [h.get("command", "") for grp in poc for h in grp.get("hooks", [])]
        assert not any("uv run --directory" in c for c in cmds), \
            "dead from-source PostCompact hook must be removed"
        assert not any("get_post_compact_context" in c for c in cmds), \
            "Contexer PostCompact hook must not be re-added"
        assert poc == []

    def test_legacy_postcompact_removed_on_uninstall(self, clean_home):
        path = self._seed_from_source(clean_home)
        install()
        uninstall()
        poc = json.loads(path.read_text()).get("hooks", {}).get("PostCompact", [])
        cmds = [h.get("command", "") for grp in poc for h in grp.get("hooks", [])]
        assert not any("uv run --directory" in c for c in cmds)

    def test_foreign_postcompact_preserved_on_install(self, clean_home):
        # Stripping our PostCompact hook must never remove a non-Contexer one.
        settings_path = clean_home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        foreign = "echo 'someone elses postcompact hook'"
        settings_path.write_text(json.dumps({"hooks": {
            "PostCompact": [
                {"hooks": [{"type": "command", "command": self._LEGACY_POC}]},
                {"hooks": [{"type": "command", "command": foreign}]},
            ],
        }}))
        install()
        poc = json.loads(settings_path.read_text())["hooks"]["PostCompact"]
        cmds = [h.get("command", "") for grp in poc for h in grp.get("hooks", [])]
        assert cmds == [foreign], "foreign PostCompact hook must survive, ours must go"


class TestRepoPointerNotPoisoned:
    """No hook may fall back to `pwd` for the repo — that writes a non-repo dir into
    the shared .current_repo pointer (review finding H1)."""

    def test_no_pwd_fallback_in_any_hook(self, installed_home):
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        cmds = [h.get("command", "") for event in settings["hooks"].values()
                for grp in event for h in grp.get("hooks", [])]
        assert cmds, "expected hook commands"
        offenders = [c for c in cmds if "|| pwd" in c]
        assert not offenders, f"hooks must not fall back to pwd: {offenders}"

    def test_git_hooks_use_true_fallback(self, installed_home):
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        cmds = [h.get("command", "") for event in settings["hooks"].values()
                for grp in event for h in grp.get("hooks", [])]
        git_cmds = [c for c in cmds if "git rev-parse" in c]
        assert git_cmds
        assert all("|| true" in c for c in git_cmds)


class TestTeamsRegistration:
    def test_not_registered_by_default(self, clean_home, monkeypatch):
        # Path B: the native contexer-teams MCP entry is opt-in only (CONTEXER_TEAMS_MCP).
        monkeypatch.delenv("CONTEXER_TEAMS_MCP", raising=False)
        install()
        servers = json.loads((clean_home / ".claude.json").read_text())["mcpServers"]
        assert "contexer-teams" not in servers

    def test_opt_in_registers_prod(self, clean_home, monkeypatch):
        monkeypatch.delenv("CONTEXER_ENV", raising=False)
        monkeypatch.setenv("CONTEXER_TEAMS_MCP", "1")
        install()
        servers = json.loads((clean_home / ".claude.json").read_text())["mcpServers"]
        assert servers["contexer-teams"] == {"type": "http", "url": "https://mcp.contexer.ai/mcp"}

    def test_opt_in_local_env_registers_localhost(self, clean_home, monkeypatch):
        monkeypatch.setenv("CONTEXER_TEAMS_MCP", "1")
        monkeypatch.setenv("CONTEXER_ENV", "local")
        install()
        servers = json.loads((clean_home / ".claude.json").read_text())["mcpServers"]
        assert servers["contexer-teams"]["url"] == "http://localhost:8080/mcp"

    def test_default_install_strips_stale_entry(self, clean_home, monkeypatch):
        monkeypatch.setenv("CONTEXER_TEAMS_MCP", "1")
        install()  # opt-in: entry present
        assert "contexer-teams" in json.loads((clean_home / ".claude.json").read_text())["mcpServers"]
        monkeypatch.delenv("CONTEXER_TEAMS_MCP", raising=False)
        install()  # plain (default) reinstall drops it
        servers = json.loads((clean_home / ".claude.json").read_text())["mcpServers"]
        assert "contexer-teams" not in servers

    def test_local_stdio_entry_untouched(self, clean_home):
        install()
        servers = json.loads((clean_home / ".claude.json").read_text())["mcpServers"]
        assert servers["contexer"]["type"] == "stdio"
        assert "command" in servers["contexer"]

    def test_preserves_unrelated_servers(self, clean_home):
        cfg = clean_home / ".claude.json"
        cfg.write_text(json.dumps({"mcpServers": {"other": {"type": "stdio", "command": "x"}}}))
        install()
        servers = json.loads(cfg.read_text())["mcpServers"]
        assert servers["other"] == {"type": "stdio", "command": "x"}

    def test_no_token_or_secret_in_entry(self, clean_home, monkeypatch):
        monkeypatch.setenv("CONTEXER_TEAMS_MCP", "1")
        install()
        entry = json.loads((clean_home / ".claude.json").read_text())["mcpServers"]["contexer-teams"]
        assert set(entry.keys()) == {"type", "url"}


class TestTeamsUninstall:
    def test_uninstall_removes_contexer_teams(self, clean_home, monkeypatch):
        monkeypatch.setenv("CONTEXER_TEAMS_MCP", "1")
        install()  # register the opt-in entry
        uninstall()
        servers = json.loads((clean_home / ".claude.json").read_text()).get("mcpServers", {})
        assert "contexer-teams" not in servers

    def test_uninstall_preserves_unrelated_servers(self, clean_home):
        install()
        cfg = clean_home / ".claude.json"
        data = json.loads(cfg.read_text())
        data["mcpServers"]["other"] = {"type": "stdio", "command": "x"}
        cfg.write_text(json.dumps(data))
        uninstall()
        servers = json.loads(cfg.read_text()).get("mcpServers", {})
        assert servers.get("other") == {"type": "stdio", "command": "x"}
        assert "contexer-teams" not in servers
        assert "contexer" not in servers


class TestTeamsStatus:
    def test_status_shows_teams_registered(self, clean_home, monkeypatch):
        monkeypatch.setenv("CONTEXER_TEAMS_MCP", "1")
        install()
        joined = "\n".join(claude.status_lines(clean_home))
        assert "teams (remote)" in joined
        assert "mcp.contexer.ai" in joined

    def test_status_shows_teams_not_registered_on_clean(self, clean_home):
        joined = "\n".join(claude.status_lines(clean_home))
        assert "teams (remote)" in joined
        assert "NOT registered" in joined
