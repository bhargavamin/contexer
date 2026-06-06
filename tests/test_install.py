"""Tests for install.sh and uninstall.sh — validates JSON output against expected schema."""
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).parent.parent


def _run_installer(script: str, home: Path) -> subprocess.CompletedProcess:
    env = {"HOME": str(home), "PATH": subprocess.os.environ["PATH"]}
    return subprocess.run(
        ["bash", str(REPO_ROOT / script), str(REPO_ROOT)],
        capture_output=True, text=True, env=env,
    )


@pytest.fixture
def clean_home(tmp_path):
    """A temporary HOME with no pre-existing Claude config."""
    return tmp_path


@pytest.fixture
def installed_home(clean_home):
    """A HOME that has already had install.sh run successfully."""
    result = _run_installer("install.sh", clean_home)
    assert result.returncode == 0, result.stderr
    return clean_home


# ── install.sh ────────────────────────────────────────────────────────────────

class TestInstall:
    def test_creates_claude_json_with_mcp_entry(self, installed_home):
        claude = json.loads((installed_home / ".claude.json").read_text())
        entry = claude["mcpServers"]["contexer"]
        assert entry["type"] == "stdio"
        assert entry["command"] == "uv"
        assert str(REPO_ROOT) in entry["args"]

    def test_creates_settings_json(self, installed_home):
        assert (installed_home / ".claude" / "settings.json").exists()

    def test_session_start_hook_registered(self, installed_home):
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        ss_groups = settings["hooks"]["SessionStart"]
        cmds = [h["command"] for grp in ss_groups for h in grp["hooks"] if "command" in h]
        assert any("get_session_start_context" in c for c in cmds)

    def test_pre_compact_hook_registered(self, installed_home):
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        pc_groups = settings["hooks"]["PreCompact"]
        cmds = [h["command"] for grp in pc_groups for h in grp["hooks"] if "command" in h]
        assert any("compaction starting" in c for c in cmds)

    def test_post_compact_hook_registered(self, installed_home):
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        poc_groups = settings["hooks"]["PostCompact"]
        cmds = [h["command"] for grp in poc_groups for h in grp["hooks"] if "command" in h]
        assert any("reloaded after compaction" in c for c in cmds)

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

    def test_capture_context_mcp_tool_registered(self, installed_home):
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        ups = settings["hooks"]["UserPromptSubmit"]
        mcp_hooks = [
            h for grp in ups for h in grp["hooks"]
            if h.get("type") == "mcp_tool" and h.get("tool") == "capture_context"
        ]
        assert len(mcp_hooks) == 1
        assert mcp_hooks[0]["server"] == "contexer"
        assert mcp_hooks[0]["input"]["description"] == "${prompt}"

    def test_permissions_added(self, installed_home):
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        allow = settings["permissions"]["allow"]
        for p in ["mcp__contexer__capture_context", "mcp__contexer__update_context",
                  "mcp__contexer__get_context", "mcp__contexer__bootstrap_context"]:
            assert p in allow

    def test_store_dir_created(self, installed_home):
        assert (installed_home / ".contexer").is_dir()

    def test_install_is_idempotent(self, installed_home):
        # Run install a second time — should not duplicate any entries
        result = _run_installer("install.sh", installed_home)
        assert result.returncode == 0

        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        ups = settings["hooks"]["UserPromptSubmit"]
        mcp_hooks = [
            h for grp in ups for h in grp["hooks"]
            if h.get("type") == "mcp_tool" and h.get("tool") == "capture_context"
        ]
        assert len(mcp_hooks) == 1, "capture_context hook must not be duplicated"

        allow = settings["permissions"]["allow"]
        assert allow.count("mcp__contexer__capture_context") == 1

    def test_install_preserves_existing_settings(self, clean_home):
        # Pre-populate settings with an unrelated key
        settings_path = clean_home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(json.dumps({"effortLevel": "xhigh", "agentPushNotifEnabled": True}))

        result = _run_installer("install.sh", clean_home)
        assert result.returncode == 0

        settings = json.loads(settings_path.read_text())
        assert settings["effortLevel"] == "xhigh"
        assert settings["agentPushNotifEnabled"] is True
        assert "hooks" in settings

    def test_post_compact_cmd_contains_repo_dir(self, installed_home):
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        poc_groups = settings["hooks"]["PostCompact"]
        cmds = [h["command"] for grp in poc_groups for h in grp["hooks"] if "command" in h]
        assert any(str(REPO_ROOT) in c for c in cmds)


# ── uninstall.sh ──────────────────────────────────────────────────────────────

class TestUninstall:
    def test_removes_mcp_entry(self, installed_home):
        result = _run_installer("uninstall.sh", installed_home)
        assert result.returncode == 0
        claude = json.loads((installed_home / ".claude.json").read_text())
        assert "contexer" not in claude.get("mcpServers", {})

    def test_removes_session_start_hook(self, installed_home):
        _run_installer("uninstall.sh", installed_home)
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        ss = settings.get("hooks", {}).get("SessionStart", [])
        cmds = [h.get("command", "") for grp in ss for h in grp.get("hooks", [])]
        assert not any("get_session_start_context" in c for c in cmds)

    def test_removes_permissions(self, installed_home):
        _run_installer("uninstall.sh", installed_home)
        settings = json.loads((installed_home / ".claude" / "settings.json").read_text())
        allow = settings.get("permissions", {}).get("allow", [])
        assert not any("contexer" in p for p in allow)

    def test_preserves_store_directory(self, installed_home):
        (installed_home / ".contexer" / "test.json").write_text("{}")
        _run_installer("uninstall.sh", installed_home)
        assert (installed_home / ".contexer" / "test.json").exists()

    def test_preserves_non_contexer_settings(self, clean_home):
        settings_path = clean_home / ".claude" / "settings.json"
        settings_path.parent.mkdir(parents=True)
        settings_path.write_text(json.dumps({"effortLevel": "xhigh"}))

        _run_installer("install.sh", clean_home)
        _run_installer("uninstall.sh", clean_home)

        settings = json.loads(settings_path.read_text())
        assert settings.get("effortLevel") == "xhigh"

    def test_uninstall_is_idempotent(self, installed_home):
        _run_installer("uninstall.sh", installed_home)
        result = _run_installer("uninstall.sh", installed_home)
        assert result.returncode == 0
