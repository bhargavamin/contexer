"""Tests for the Cursor adapter install/uninstall."""
import json
import sys

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
        assert any("cursor.capture_constraint" in c for c in cmds)

    def test_uses_current_python(self, home):
        cursor.install(home)
        hooks = json.loads((home / ".cursor" / "hooks.json").read_text())
        cmds = [h["command"] for h in hooks["hooks"]["sessionStart"]]
        assert any(sys.executable in c for c in cmds)

    def test_rule_body_mentions_pending_review(self):
        # Cursor can't inject per-prompt context, so the pending-review nudge rides the
        # always-apply .mdc rule (Cursor re-injects it natively every prompt).
        assert "review_pending" in cursor._RULE_BODY

    def test_ensure_rule_file_refreshes_stale_managed_body(self, tmp_path):
        repo = tmp_path / "ws"
        rules = repo / ".cursor" / "rules"
        rules.mkdir(parents=True)
        rule = rules / cursor._RULE_FILENAME
        rule.write_text(f"<!-- {cursor.base._BOOTSTRAP_CMD_MARKER} -->\nstale body\n")  # ours, outdated
        cursor._ensure_rule_file(str(repo))
        assert "review_pending" in rule.read_text()  # refreshed to current body

    def test_install_idempotent(self, home):
        cursor.install(home)
        cursor.install(home)
        hooks = json.loads((home / ".cursor" / "hooks.json").read_text())
        cmds = [h["command"] for h in hooks["hooks"]["beforeSubmitPrompt"]]
        assert sum("cursor.capture_constraint" in c for c in cmds) == 1

    def test_does_not_auto_approve_mcp_tools(self, home):
        # Contexer must not silently pre-approve its own MCP tools — Cursor should still
        # prompt the user on first use.
        log = cursor.install(home)
        assert not (home / ".cursor" / "permissions.json").exists()
        assert any("approve" in line.lower() for line in log)

    def test_preserves_existing_cursor_config(self, home):
        mcp_path = home / ".cursor" / "mcp.json"
        mcp_path.parent.mkdir(parents=True)
        mcp_path.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
        cursor.install(home)
        mcp = json.loads(mcp_path.read_text())
        assert "other" in mcp["mcpServers"]
        assert "contexer" in mcp["mcpServers"]

    def test_removes_exact_retired_teams_server_only(self, home):
        mcp_path = home / ".cursor" / "mcp.json"
        mcp_path.parent.mkdir(parents=True)
        mcp_path.write_text(json.dumps({"mcpServers": {
            "contexer-teams": {"type": "http", "url": "https://legacy.invalid/mcp"},
            "contexer-teams-custom": {"command": "user-owned"},
            "other": {"command": "other"},
        }}))
        cursor.install(home)
        servers = json.loads(mcp_path.read_text())["mcpServers"]
        assert "contexer-teams" not in servers
        assert servers["contexer-teams-custom"] == {"command": "user-owned"}
        assert servers["other"] == {"command": "other"}


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

    def test_removes_exact_retired_teams_server_only(self, home):
        mcp_path = home / ".cursor" / "mcp.json"
        mcp_path.parent.mkdir(parents=True)
        mcp_path.write_text(json.dumps({"mcpServers": {
            "contexer": {"command": "contexer"},
            "contexer-teams": {"type": "http", "url": "https://legacy.invalid/mcp"},
            "contexer-teams-custom": {"command": "user-owned"},
        }}))
        cursor.uninstall(home)
        servers = json.loads(mcp_path.read_text())["mcpServers"]
        assert "contexer" not in servers
        assert "contexer-teams" not in servers
        assert servers["contexer-teams-custom"] == {"command": "user-owned"}
