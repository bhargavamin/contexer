"""Tests for contexer CLI install/uninstall commands."""
import json
import sys
from pathlib import Path

import pytest

from contexer import config
from contexer.adapters import claude
from contexer.cli import install, uninstall


@pytest.fixture(autouse=True)
def isolated_config(tmp_path, monkeypatch):
    """Never read the developer's real ~/.contexer/config.toml during install tests."""
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / "isolated-config.toml")
    return config.CONFIG_PATH


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

    def test_session_start_upgraded_to_thread_session_id(self, clean_home):
        # Pre-retrieval-V1 hook: has source_from_hook_stdin AND sync_memory but no
        # session-id threading — must still be replaced on reinstall.
        settings_path = clean_home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps({"hooks": {
            "SessionStart": [{"hooks": [{"type": "command",
                "command": "py -c '_c.sync_memory(repo); store.get_session_start_context("
                           "repo, store.source_from_hook_stdin(x))'"}]}],
        }}))
        install()
        groups = json.loads(settings_path.read_text())["hooks"]["SessionStart"]
        cmds = [h["command"] for grp in groups for h in grp["hooks"]]
        assert any("session_from_hook_stdin" in c for c in cmds)
        assert len(groups) == 1


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
    # The native contexer-teams remote-MCP entry is retired: team sync is the Python client
    # path (`contexer login` + pull/share/poll). Install never writes the entry and strips
    # any legacy leftover from an older build.
    def test_never_registers(self, clean_home):
        install()
        servers = json.loads((clean_home / ".claude.json").read_text())["mcpServers"]
        assert "contexer-teams" not in servers

    def test_retired_env_var_never_registers(self, clean_home, monkeypatch):
        # CONTEXER_TEAMS_MCP is dead — setting it must NOT resurrect the native entry.
        monkeypatch.setenv("CONTEXER_TEAMS_MCP", "1")
        install()
        servers = json.loads((clean_home / ".claude.json").read_text())["mcpServers"]
        assert "contexer-teams" not in servers

    def test_install_strips_legacy_entry(self, clean_home):
        # A leftover entry from an older default-on build is removed on the next install;
        # the local stdio entry and unrelated servers survive.
        cfg = clean_home / ".claude.json"
        cfg.write_text(json.dumps({"mcpServers": {
            "contexer-teams": {"type": "http", "url": "https://mcp.contexer.ai/mcp"},
            "other": {"type": "stdio", "command": "x"},
        }}))
        install()
        servers = json.loads(cfg.read_text())["mcpServers"]
        assert "contexer-teams" not in servers
        assert servers["contexer"]["type"] == "stdio"
        assert servers["other"] == {"type": "stdio", "command": "x"}

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


class TestTeamsUninstall:
    def test_uninstall_removes_legacy_contexer_teams(self, clean_home):
        install()
        cfg = clean_home / ".claude.json"
        data = json.loads(cfg.read_text())
        data["mcpServers"]["contexer-teams"] = {"type": "http", "url": "https://mcp.contexer.ai/mcp"}
        cfg.write_text(json.dumps(data))
        uninstall()
        servers = json.loads(cfg.read_text()).get("mcpServers", {})
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
    def test_status_has_no_teams_line(self, clean_home):
        # The native teams entry is retired — status no longer reports it at all.
        joined = "\n".join(claude.status_lines(clean_home))
        assert "teams (remote)" not in joined
        assert "[claude]" in joined


class TestLegacyRepoSettingsCleanup:
    """The pre-CLI from-source installer wrote hooks into <repo>/.claude/settings.json —
    including an mcp_tool hook for the removed capture_context tool ("Unknown tool:
    capture_context" on every prompt) and a dead-clone SessionStart hook (a second,
    contradictory "no context stored yet" startup message next to the real one).
    Upgrades must remove these; everything foreign in the file must survive."""

    _LEGACY_SS = (
        "uv run --directory /old/clone python -c \"import sys,json; import store; "
        "print(json.dumps(store.get_session_start_context('/old/repo')))\""
    )
    _LEGACY_REMINDER = (
        "echo '{\"hookSpecificOutput\": {\"hookEventName\": \"UserPromptSubmit\", "
        "\"additionalContext\": \"Reminder: if you make a significant decision, establish "
        "a pattern, or document a constraint this turn, call update_context.\"}}'"
    )
    _FOREIGN = "echo 'someone elses hook'"

    def _seed_repo(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / ".claude").mkdir(parents=True)
        (repo / ".claude" / "settings.json").write_text(json.dumps({
            "hooks": {
                "SessionStart": [{"hooks": [{"type": "command", "command": self._LEGACY_SS}]}],
                "PreCompact": [{"hooks": [{"type": "command", "command":
                    "echo '{\"systemMessage\": \"Contexer: context compaction starting\"}'"}]}],
                "UserPromptSubmit": [
                    {"hooks": [{"type": "mcp_tool", "server": "contexer",
                                "tool": "capture_context", "once": True,
                                "input": {"repo_path": "/old/repo"}}]},
                    {"hooks": [{"type": "command", "command": self._LEGACY_REMINDER}]},
                    {"hooks": [{"type": "command", "command": self._FOREIGN}]},
                ],
            },
            "enabledPlugins": {"foreign-plugin": True},
        }))
        return repo

    def test_removes_contexer_hooks_preserves_foreign(self, tmp_path):
        repo = self._seed_repo(tmp_path)
        assert claude.clean_legacy_repo_settings(str(repo)) is True
        out = json.loads((repo / ".claude" / "settings.json").read_text())
        hooks = out.get("hooks", {})
        assert "SessionStart" not in hooks
        assert "PreCompact" not in hooks
        remaining = [h for grp in hooks["UserPromptSubmit"] for h in grp["hooks"]]
        assert [h.get("command") for h in remaining] == [self._FOREIGN]
        assert not any(h.get("type") == "mcp_tool" for h in remaining)
        assert out["enabledPlugins"] == {"foreign-plugin": True}

    def test_noop_when_no_settings_file(self, tmp_path):
        assert claude.clean_legacy_repo_settings(str(tmp_path / "repo")) is False

    def test_noop_on_foreign_only_file(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / ".claude").mkdir(parents=True)
        p = repo / ".claude" / "settings.json"
        p.write_text(json.dumps({"hooks": {
            "SessionStart": [{"hooks": [{"type": "command", "command": self._FOREIGN}]}]}}))
        before = p.read_text()
        assert claude.clean_legacy_repo_settings(str(repo)) is False
        assert p.read_text() == before

    def test_corrupt_file_left_alone(self, tmp_path):
        repo = tmp_path / "repo"
        (repo / ".claude").mkdir(parents=True)
        p = repo / ".claude" / "settings.json"
        p.write_text("{not json")
        assert claude.clean_legacy_repo_settings(str(repo)) is False
        assert p.read_text() == "{not json"

    def test_refuses_home_directory(self, clean_home):
        # Dotfiles setups make HOME itself a git repo. ~/.claude/settings.json is the
        # GLOBAL config whose modern hooks legitimately contain the legacy markers —
        # the cleaner must refuse the home dir outright (Greptile P1, PR #96).
        (clean_home / ".claude").mkdir(exist_ok=True)
        p = clean_home / ".claude" / "settings.json"
        p.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": [{
            "type": "command",
            "command": "python -c \"...store.get_session_start_context(...)...\""}]}]}}))
        before = p.read_text()
        assert claude.clean_legacy_repo_settings(str(clean_home)) is False
        assert p.read_text() == before

    def test_sync_memory_self_heals(self, clean_home, tmp_path):
        # A plain package upgrade (no `contexer install` re-run) must heal too:
        # sync_memory runs under every already-installed SessionStart hook and is the
        # seam that strips the legacy repo-level hooks for whatever repo the session opens.
        repo = self._seed_repo(tmp_path)
        claude.sync_memory(str(repo))
        hooks = json.loads((repo / ".claude" / "settings.json").read_text()).get("hooks", {})
        assert "SessionStart" not in hooks
        assert not any(h.get("type") == "mcp_tool"
                       for grp in hooks.get("UserPromptSubmit", []) for h in grp["hooks"])

    def test_install_prunes_stale_capture_context_permission(self, clean_home):
        settings_path = clean_home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps({"permissions": {"allow": [
            "mcp__contexer__capture_context", "mcp__contexer__update_context"]}}))
        install()
        allow = json.loads(settings_path.read_text())["permissions"]["allow"]
        assert "mcp__contexer__capture_context" not in allow
        assert "mcp__contexer__update_context" in allow

    def test_install_strips_legacy_reminder_echo(self, clean_home):
        settings_path = clean_home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": self._LEGACY_REMINDER}]}]}}))
        install()
        ups = json.loads(settings_path.read_text())["hooks"]["UserPromptSubmit"]
        cmds = [h.get("command", "") for grp in ups for h in grp.get("hooks", [])]
        assert not any("Reminder: if you make a significant decision" in c for c in cmds)

    def test_uninstall_strips_legacy_reminder_echo(self, clean_home):
        settings_path = clean_home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": self._LEGACY_REMINDER}]}]}}))
        uninstall()
        hooks = json.loads(settings_path.read_text()).get("hooks", {})
        assert "UserPromptSubmit" not in hooks

    def test_stale_plugin_warning(self, clean_home):
        plug = clean_home / ".claude" / "plugins"
        cache = plug / "cache" / "mp" / "contexer" / "0.1.0"
        (cache / "hooks").mkdir(parents=True)
        (cache / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "mcp_tool", "server": "contexer",
                                             "tool": "capture_context"}]}]}}))
        (plug / "installed_plugins.json").write_text(json.dumps({
            "version": 2, "plugins": {"contexer@mp": [{"installPath": str(cache)}]}}))
        warning = claude._stale_plugin_warning(clean_home)
        assert warning and "capture_context" in warning

    def test_no_plugin_warning_on_clean_home(self, clean_home):
        assert claude._stale_plugin_warning(clean_home) is None


class TestCaptureTaskStubs:
    """In-process coverage of the self-retiring capture_task stubs (the E2E class
    TestStaleCaptureTaskHook exercises them through bash, which coverage can't see)."""

    _STALE_CLAUDE = 'python -c "...claude.capture_task(...)..."'
    _STALE_CURSOR = 'python -c "...cursor.capture_task(...)..."'
    _HEALTHY = "echo healthy"

    def test_claude_stub_retires_own_hook(self, clean_home):
        (clean_home / ".claude").mkdir(exist_ok=True)
        p = clean_home / ".claude" / "settings.json"
        p.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": self._STALE_CLAUDE}]},
            {"hooks": [{"type": "command", "command": self._HEALTHY}]}]}}))
        assert claude.capture_task("", "") == "{}"
        ups = json.loads(p.read_text())["hooks"]["UserPromptSubmit"]
        assert [h["command"] for g in ups for h in g["hooks"]] == [self._HEALTHY]

    def test_claude_stub_heals_codex_hooks_too(self, clean_home):
        p = clean_home / ".codex" / "hooks.json"
        p.parent.mkdir()
        p.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": self._STALE_CLAUDE}]}]}}))
        assert claude.capture_task("", "") == "{}"
        assert "capture_task" not in p.read_text()

    def test_cursor_stub_retires_own_hook_and_passes_through(self, clean_home):
        from contexer.adapters import cursor
        p = clean_home / ".cursor" / "hooks.json"
        p.parent.mkdir()
        p.write_text(json.dumps({"hooks": {"beforeSubmitPrompt": [
            {"type": "command", "command": self._STALE_CURSOR},
            {"type": "command", "command": self._HEALTHY}]}}))
        out = json.loads(cursor.capture_task("", ""))
        assert isinstance(out, dict)
        bsp = json.loads(p.read_text())["hooks"]["beforeSubmitPrompt"]
        assert [h["command"] for h in bsp] == [self._HEALTHY]

    def test_stubs_failsoft_without_configs(self, clean_home):
        from contexer.adapters import codex, cursor
        assert claude.capture_task("", "") == "{}"
        assert json.loads(cursor.capture_task("", ""))
        codex.retire_capture_task(clean_home)  # must not raise

    def test_retiring_only_hook_leaves_no_empty_keys(self, clean_home):
        # When the stale hook was the only one, neither a dangling empty event list
        # nor an empty "hooks" key may remain (Greptile, PR #98) — matching the
        # clean_legacy_repo_settings behavior.
        from contexer.adapters import cursor
        (clean_home / ".claude").mkdir(exist_ok=True)
        (clean_home / ".codex").mkdir()
        (clean_home / ".cursor").mkdir()
        claude_p = clean_home / ".claude" / "settings.json"
        codex_p = clean_home / ".codex" / "hooks.json"
        cursor_p = clean_home / ".cursor" / "hooks.json"
        claude_p.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": self._STALE_CLAUDE}]}]}}))
        codex_p.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": self._STALE_CLAUDE}]}]}}))
        cursor_p.write_text(json.dumps({"hooks": {"beforeSubmitPrompt": [
            {"type": "command", "command": self._STALE_CURSOR}]}}))
        claude.capture_task("", "")    # heals claude settings + codex hooks.json
        cursor.capture_task("", "")
        assert json.loads(claude_p.read_text()) == {}
        assert json.loads(codex_p.read_text()) == {}
        assert json.loads(cursor_p.read_text()) == {}

    def test_codex_retire_preserves_foreign_and_handles_corrupt(self, clean_home):
        from contexer.adapters import codex
        p = clean_home / ".codex" / "hooks.json"
        p.parent.mkdir()
        p.write_text("{corrupt")
        codex.retire_capture_task(clean_home)          # corrupt: no raise, no change
        assert p.read_text() == "{corrupt"
        p.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": self._HEALTHY}]}]}}))
        before = p.read_text()
        codex.retire_capture_task(clean_home)          # nothing stale: no churn
        assert p.read_text() == before


# ── Doc Drift Layer 1 — Task 1.6: post_write migration + drift hook install ─────

class TestDriftHookInstall:
    def _cmds(self, home, event):
        settings = json.loads((home / ".claude" / "settings.json").read_text())
        return [h.get("command", "") for grp in settings["hooks"].get(event, [])
                for h in grp.get("hooks", [])]

    def test_post_write_hook_registered(self, installed_home):
        cmds = self._cmds(installed_home, "PostToolUse")
        assert any("claude.post_write" in c for c in cmds)

    def test_legacy_shell_touch_hook_gone(self, installed_home):
        cmds = self._cmds(installed_home, "PostToolUse")
        # The legacy hook was a bare shell `touch ... && echo`; the migrated hook invokes
        # the Python post_write entrypoint instead.
        assert not any(c.strip().startswith("touch ~/.contexer/.pending_capture")
                       for c in cmds)

    def test_post_write_hook_keeps_pending_capture_marker(self, installed_home):
        # The migrated hook keeps the `.pending_capture` marker so the anchor's flag semantics,
        # the reinstall/migration detection, and the uninstall marker table all key on it.
        cmds = self._cmds(installed_home, "PostToolUse")
        assert any(".pending_capture" in c for c in cmds)

    def test_migration_replaces_legacy_shell_group(self, clean_home):
        settings_path = clean_home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(json.dumps({"hooks": {"PostToolUse": [
            {"matcher": "Write|Edit", "hooks": [{"type": "command",
             "command": "touch ~/.contexer/.pending_capture && echo '{}' "
                        "# contexer-managed-hook"}]}]}}))
        install()
        settings = json.loads(settings_path.read_text())
        cmds = [h.get("command", "") for grp in settings["hooks"]["PostToolUse"]
                for h in grp.get("hooks", [])]
        assert any("claude.post_write" in c for c in cmds)
        # legacy shell group stripped, not left alongside the new one
        assert not any(c.strip().startswith("touch ~/.contexer/.pending_capture")
                       for c in cmds)
        write_groups = [grp for grp in settings["hooks"]["PostToolUse"]
                        if str(grp.get("matcher", "")).startswith("Write")]
        assert len(write_groups) == 1

    def test_migration_replaces_old_one_arg_post_write(self, clean_home):
        # Simulates an install from before this fix: post_write's hook had no `git
        # rev-parse --show-toplevel` prefix and called post_write(sys.stdin.read()) with a
        # single arg. install() must replace it with the $REPO-threading version, and stay
        # idempotent on repeated installs thereafter.
        settings_path = clean_home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        old_cmd = (f'"{sys.executable}" -c "from contexer.adapters import claude; import sys; '
                   'print(claude.post_write(sys.stdin.read()))" '
                   '# contexer-managed-hook .pending_capture')
        settings_path.write_text(json.dumps({"hooks": {"PostToolUse": [
            {"matcher": "Write|Edit|MultiEdit", "hooks": [{"type": "command",
             "command": old_cmd}]}]}}))
        install()
        cmds = self._cmds(clean_home, "PostToolUse")
        post_write_cmds = [c for c in cmds if "claude.post_write" in c]
        assert len(post_write_cmds) == 1
        assert "show-toplevel" in post_write_cmds[0]
        assert "sys.argv[1]" in post_write_cmds[0]
        # Idempotent: a second install doesn't duplicate or re-churn the (now current) hook.
        install()
        cmds2 = self._cmds(clean_home, "PostToolUse")
        assert sum("claude.post_write" in c for c in cmds2) == 1
        assert [c for c in cmds2 if "claude.post_write" in c] == post_write_cmds

    def test_install_idempotent_single_post_write(self, clean_home):
        install()
        install()
        install()
        cmds = self._cmds(clean_home, "PostToolUse")
        assert sum("claude.post_write" in c for c in cmds) == 1

    def test_drift_hook_registered(self, installed_home):
        cmds = self._cmds(installed_home, "UserPromptSubmit")
        assert any("claude.drift" in c for c in cmds)

    def test_install_idempotent_single_drift(self, clean_home):
        install()
        install()
        cmds = self._cmds(clean_home, "UserPromptSubmit")
        assert sum("claude.drift" in c for c in cmds) == 1

    def test_drift_does_not_disturb_other_ups_hooks(self, installed_home):
        # GAP-4: the drift group is ADDED, never replacing anchor/constraint/rationale/
        # team-poll/bootstrap/review-nudge.
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        ups = settings["hooks"]["UserPromptSubmit"]
        for marker in [".current_repo", "claude.capture_constraint", "claude.rationale",
                       "claude.team_poll", "get_bootstrap_context_prompt",
                       "claude.review_nudge"]:
            assert claude._in_groups(ups, marker), f"missing {marker}"
        assert claude._in_groups(ups, "claude.drift")

    def test_post_write_and_drift_resolve_repo_identically(self, installed_home):
        # THE BUG (regression): post_write's installed shell wrapper used to resolve the repo
        # from raw os.getcwd() inside Python, with no `git rev-parse --show-toplevel`
        # normalization, while drift's wrapper (like every sibling UserPromptSubmit hook)
        # computes REPO in the shell first. In a monorepo subdirectory those diverge, so
        # post_write's sidecar write and drift's sidecar read land under different repo slugs
        # and drift silently never fires — no error, no failing unit test, because the
        # pure-Python handshake test drives both functions with a hand-passed repo and can't
        # see this shell-layer divergence. Assert both installed wrappers share the identical
        # repo-resolution shell prefix.
        put_cmds = self._cmds(installed_home, "PostToolUse")
        ups_cmds = self._cmds(installed_home, "UserPromptSubmit")
        post_write_cmd = next(c for c in put_cmds if "claude.post_write" in c)
        drift_cmd = next(c for c in ups_cmds if "claude.drift" in c)
        repo_prefix = "REPO=$(git rev-parse --show-toplevel 2>/dev/null || true) && "
        assert post_write_cmd.startswith(repo_prefix), (
            f"post_write's wrapper does not resolve $REPO via git-toplevel: {post_write_cmd!r}")
        assert drift_cmd.startswith(repo_prefix), (
            f"drift's wrapper does not resolve $REPO via git-toplevel: {drift_cmd!r}")
        # Both must pass $REPO into the python call the same way (positional argv[1]).
        assert '"$REPO"' in post_write_cmd
        assert '"$REPO"' in drift_cmd
        assert "sys.argv[1]" in post_write_cmd
        assert "sys.argv[1]" in drift_cmd

    def test_uninstall_strips_post_write_and_drift(self, installed_home):
        uninstall()
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        hooks = settings.get("hooks", {})
        put_cmds = [h.get("command", "") for grp in hooks.get("PostToolUse", [])
                    for h in grp.get("hooks", [])]
        ups_cmds = [h.get("command", "") for grp in hooks.get("UserPromptSubmit", [])
                    for h in grp.get("hooks", [])]
        assert not any("claude.post_write" in c for c in put_cmds)
        assert not any("claude.drift" in c for c in ups_cmds)

    def test_uninstall_preserves_foreign_hooks(self, clean_home):
        install()
        settings_path = clean_home / ".claude" / "settings.json"
        settings = json.loads(settings_path.read_text())
        settings["hooks"].setdefault("PostToolUse", []).append(
            {"matcher": "Write", "hooks": [{"type": "command", "command": "./my-linter.sh"}]})
        settings["hooks"].setdefault("UserPromptSubmit", []).append(
            {"hooks": [{"type": "command", "command": "./my-own-ups.sh"}]})
        settings_path.write_text(json.dumps(settings))
        uninstall()
        settings = json.loads(settings_path.read_text())
        put_cmds = [h.get("command", "") for grp in settings["hooks"].get("PostToolUse", [])
                    for h in grp.get("hooks", [])]
        ups_cmds = [h.get("command", "") for grp in settings["hooks"].get("UserPromptSubmit", [])
                    for h in grp.get("hooks", [])]
        assert "./my-linter.sh" in put_cmds
        assert "./my-own-ups.sh" in ups_cmds
