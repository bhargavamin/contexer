"""Tests for the Cursor adapter install/uninstall."""
import json
import sys
from pathlib import Path

import pytest

from contexer.adapters import cursor


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


class TestCursorInstall:
    def test_writes_mcp_json(self, home):
        cursor.install(home)
        mcp = json.loads((home / ".cursor" / "mcp.json").read_text())
        entry = mcp["mcpServers"]["contexer"]
        assert "contexer" in entry["command"]

    def test_writes_hooks_json_version_1(self, home):
        cursor.install(home)
        hooks = json.loads((home / ".cursor" / "hooks.json").read_text())
        assert hooks["version"] == 1

    def test_session_start_hook_calls_adapter(self, home):
        cursor.install(home)
        hooks = json.loads((home / ".cursor" / "hooks.json").read_text())
        cmds = [h["command"] for h in hooks["hooks"]["sessionStart"]]
        assert any("cursor.session_start" in c for c in cmds)

    def test_before_submit_prompt_hooks_registered(self, home):
        cursor.install(home)
        hooks = json.loads((home / ".cursor" / "hooks.json").read_text())
        cmds = [h["command"] for h in hooks["hooks"]["beforeSubmitPrompt"]]
        assert any("cursor.capture_task" in c for c in cmds)
        assert any("cursor.capture_constraint" in c for c in cmds)

    def test_uses_current_python(self, home):
        cursor.install(home)
        hooks = json.loads((home / ".cursor" / "hooks.json").read_text())
        cmds = [h["command"] for h in hooks["hooks"]["sessionStart"]]
        assert any(sys.executable in c for c in cmds)

    def test_install_idempotent(self, home):
        cursor.install(home)
        cursor.install(home)
        hooks = json.loads((home / ".cursor" / "hooks.json").read_text())
        cmds = [h["command"] for h in hooks["hooks"]["beforeSubmitPrompt"]]
        assert sum("cursor.capture_task" in c for c in cmds) == 1

    def test_preserves_existing_cursor_config(self, home):
        mcp_path = home / ".cursor" / "mcp.json"
        mcp_path.parent.mkdir(parents=True)
        mcp_path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
        cursor.install(home)
        mcp = json.loads(mcp_path.read_text())
        assert "other" in mcp["mcpServers"]
        assert "contexer" in mcp["mcpServers"]


class TestCursorUninstall:
    def test_removes_mcp_entry(self, home):
        cursor.install(home)
        cursor.uninstall(home)
        mcp = json.loads((home / ".cursor" / "mcp.json").read_text())
        assert "contexer" not in mcp.get("mcpServers", {})

    def test_removes_contexer_hooks_only(self, home):
        hooks_path = home / ".cursor" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        hooks_path.write_text(json.dumps(
            {"version": 1, "hooks": {"afterFileEdit": [{"command": "./mine.sh"}]}}))
        cursor.install(home)
        cursor.uninstall(home)
        hooks = json.loads(hooks_path.read_text())
        # user's own hook survives; contexer's sessionStart is gone
        assert hooks["hooks"]["afterFileEdit"][0]["command"] == "./mine.sh"
        assert not hooks["hooks"].get("sessionStart")

    def test_uninstall_idempotent(self, home):
        cursor.install(home)
        cursor.uninstall(home)
        cursor.uninstall(home)  # must not raise
