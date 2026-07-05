"""Tests for the Codex adapter install/uninstall."""
import json
import sys
import tomllib
from pathlib import Path

import pytest

from contexer.adapters import codex


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _config(home: Path) -> str:
    return (home / ".codex" / "config.toml").read_text()


def _hooks(home: Path) -> dict:
    return json.loads((home / ".codex" / "hooks.json").read_text())


class TestCodexInstall:
    def test_registers_mcp_in_config_toml(self, home):
        codex.install(home)
        data = tomllib.loads(_config(home))
        assert "contexer" in data["mcp_servers"]["contexer"]["command"]

    def test_config_toml_is_valid_toml(self, home):
        codex.install(home)
        tomllib.loads(_config(home))  # must not raise

    def test_session_start_hook_loads_context(self, home):
        codex.install(home)
        cmds = [g["hooks"][0]["command"] for g in _hooks(home)["hooks"]["SessionStart"]]
        assert any("get_session_start_context" in c for c in cmds)

    def test_full_hook_event_set_wired(self, home):
        codex.install(home)
        hooks = _hooks(home)["hooks"]
        assert set(hooks) >= {"SessionStart", "PostToolUse", "PreCompact",
                              "PostCompact", "UserPromptSubmit"}

    def test_user_prompt_submit_capture_hooks(self, home):
        codex.install(home)
        cmds = [h["command"] for g in _hooks(home)["hooks"]["UserPromptSubmit"]
                for h in g["hooks"]]
        joined = "\n".join(cmds)
        for marker in ("get_bootstrap_context_prompt",
                       "claude.capture_constraint", "claude.rationale", ".pending_capture"):
            assert marker in joined

    # ── T2: team sync ────────────────────────────────────────────────────────────

    def test_session_start_pulls_team(self, home):
        codex.install(home)
        cmds = [g["hooks"][0]["command"] for g in _hooks(home)["hooks"]["SessionStart"]]
        assert any("pull_team" in c for c in cmds)  # team cache refreshed at session start

    def test_user_prompt_submit_wires_team_poll(self, home):
        codex.install(home)
        cmds = [h["command"] for g in _hooks(home)["hooks"]["UserPromptSubmit"]
                for h in g["hooks"]]
        assert any("claude.team_poll" in c for c in cmds)  # per-prompt delta injection

    def test_team_poll_wired_once(self, home):
        codex.install(home)
        codex.install(home)
        cmds = [h["command"] for g in _hooks(home)["hooks"]["UserPromptSubmit"]
                for h in g["hooks"]]
        assert sum("claude.team_poll" in c for c in cmds) == 1

    def test_session_start_pull_team_wired_once(self, home):
        codex.install(home)
        codex.install(home)
        assert len(_hooks(home)["hooks"]["SessionStart"]) == 1  # not duplicated on reinstall

    def test_migrates_stale_session_start_to_add_team_pull(self, home):
        # An older install: SessionStart loads context but has NO team pull. Reinstall must
        # replace it so team context refreshes at session start.
        hooks_path = home / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        hooks_path.write_text(json.dumps({"hooks": {"SessionStart": [
            {"hooks": [{"type": "command",
                        "command": 'py -c "store.get_session_start_context(repo)" "$REPO"'}]}]}}))
        codex.install(home)
        ss = _hooks(home)["hooks"]["SessionStart"]
        cmds = [g["hooks"][0]["command"] for g in ss]
        assert len(ss) == 1  # replaced in place, not duplicated
        assert any("pull_team" in c for c in cmds)
        assert any("get_session_start_context" in c for c in cmds)

    def test_post_tool_use_matches_write_edit(self, home):
        codex.install(home)
        put = _hooks(home)["hooks"]["PostToolUse"]
        assert any(g.get("matcher") == "Write|Edit" for g in put)

    def test_no_stop_hook_installed(self, home):
        codex.install(home)
        stop = _hooks(home)["hooks"].get("Stop", [])
        cmds = [h.get("command", "") for g in stop for h in g.get("hooks", [])]
        assert not any(".pending_capture" in c for c in cmds)

    def test_install_removes_preexisting_contexer_stop_hook(self, home):
        hooks_path = home / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        hooks_path.write_text(json.dumps({"hooks": {"Stop": [
            {"hooks": [{"type": "command",
                        "command": "rm -f $HOME/.contexer/.pending_capture; echo '{}'"}]}]}}))
        codex.install(home)
        stop = _hooks(home)["hooks"].get("Stop", [])
        cmds = [h.get("command", "") for g in stop for h in g.get("hooks", [])]
        assert not any(".pending_capture" in c for c in cmds)

    def test_install_preserves_foreign_stop_hook(self, home):
        hooks_path = home / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        hooks_path.write_text(json.dumps({"hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": "./mine.sh"}]}]}}))
        codex.install(home)
        cmds = [h.get("command", "") for g in _hooks(home)["hooks"]["Stop"]
                for h in g.get("hooks", [])]
        assert "./mine.sh" in cmds

    def test_uses_current_python(self, home):
        codex.install(home)
        cmds = [g["hooks"][0]["command"] for g in _hooks(home)["hooks"]["SessionStart"]]
        assert any(sys.executable in c for c in cmds)

    def test_install_idempotent(self, home):
        codex.install(home)
        codex.install(home)
        cmds = [h["command"] for g in _hooks(home)["hooks"]["UserPromptSubmit"]
                for h in g["hooks"]]
        assert sum("claude.capture_constraint" in c for c in cmds) == 1
        # the stanza must appear exactly once too
        assert _config(home).count("[mcp_servers.contexer]") == 1

    def test_does_not_pre_approve_tools(self, home):
        log = codex.install(home)
        assert any("approve" in line.lower() for line in log)

    def test_preserves_existing_config_toml_byte_for_byte(self, home):
        cfg = home / ".codex" / "config.toml"
        cfg.parent.mkdir(parents=True)
        original = (
            "# my codex config\n"
            "model = \"gpt-5\"\n\n"
            "[mcp_servers.other]\n"
            "command = \"/usr/bin/other\"\n\n"
            "[mcp_servers.other.env]\n"
            "SECRET = \"hunter2\"\n"
        )
        cfg.write_text(original)
        codex.install(home)
        text = _config(home)
        # everything the user had is still present, untouched
        assert original in text
        assert "[mcp_servers.contexer]" in text
        # parses and both servers are visible
        data = tomllib.loads(text)
        assert data["mcp_servers"]["other"]["env"]["SECRET"] == "hunter2"
        assert "contexer" in data["mcp_servers"]["contexer"]["command"]

    def test_replaces_existing_contexer_stanza_in_place(self, home):
        cfg = home / ".codex" / "config.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("[mcp_servers.contexer]\ncommand = \"/old/path\"\n\n[other]\nx = 1\n")
        codex.install(home)
        text = _config(home)
        assert text.count("[mcp_servers.contexer]") == 1
        assert "/old/path" not in text
        assert tomllib.loads(text)["other"]["x"] == 1

    def test_refuses_to_touch_invalid_config_toml(self, home):
        cfg = home / ".codex" / "config.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("this is = = not valid toml [[[")
        log = codex.install(home)
        assert cfg.read_text() == "this is = = not valid toml [[["  # untouched
        assert any("not valid TOML" in line for line in log)


class TestCodexUninstall:
    def test_removes_mcp_stanza_only(self, home):
        cfg = home / ".codex" / "config.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("[mcp_servers.other]\ncommand = \"x\"\n")
        codex.install(home)
        codex.uninstall(home)
        text = _config(home)
        assert "[mcp_servers.contexer]" not in text
        assert tomllib.loads(text)["mcp_servers"]["other"]["command"] == "x"

    def test_removes_contexer_hooks_keeps_user_hooks(self, home):
        hooks_path = home / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        hooks_path.write_text(json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "./mine.sh"}]}]}}))
        codex.install(home)
        codex.uninstall(home)
        hooks = _hooks(home)["hooks"]
        assert hooks["Stop"][0]["hooks"][0]["command"] == "./mine.sh"
        assert not hooks.get("SessionStart")

    def test_uninstall_idempotent(self, home):
        codex.install(home)
        codex.uninstall(home)
        codex.uninstall(home)  # must not raise

    def test_uninstall_removes_team_poll(self, home):
        codex.install(home)
        codex.uninstall(home)
        ups = _hooks(home)["hooks"].get("UserPromptSubmit", [])
        cmds = [h.get("command", "") for g in ups for h in g.get("hooks", [])]
        assert not any("claude.team_poll" in c for c in cmds)


class TestCodexStatus:
    def test_is_installed_true_after_install(self, home):
        codex.install(home)
        assert codex.is_installed(home) is True

    def test_is_installed_false_when_absent(self, home):
        assert codex.is_installed(home) is False

    def test_status_lines_report_registered(self, home):
        codex.install(home)
        lines = "\n".join(codex.status_lines(home))
        assert "[codex]" in lines
        assert "registered" in lines
        assert "installed" in lines

    def test_status_tolerates_corrupt_files(self, home):
        d = home / ".codex"
        d.mkdir(parents=True)
        (d / "config.toml").write_text("not = = valid [[[")
        (d / "hooks.json").write_text("{ not json")
        # must not raise and must read as not-installed
        assert codex.is_installed(home) is False
        assert codex.status_lines(home)  # returns lines, no crash

    def test_is_present(self, home):
        assert codex.is_present(home) is False
        (home / ".codex").mkdir()
        assert codex.is_present(home) is True
