"""Tests for the Gemini CLI adapter install, runtime hooks, and uninstall."""
import json
import sys
from pathlib import Path

import pytest

from contexer import store
from contexer.adapters import gemini


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    return tmp_path


def _settings(home: Path) -> dict:
    return json.loads((home / ".gemini" / "settings.json").read_text())


def _commands(home: Path, event: str) -> list[str]:
    return [
        hook["command"]
        for group in _settings(home)["hooks"][event]
        for hook in group["hooks"]
    ]


class TestGeminiInstall:
    def test_registers_mcp_and_preserves_user_settings(self, home):
        path = home / ".gemini" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"theme": "Dracula", "mcpServers": {"mine": {"command": "x"}}}))
        gemini.install(home)
        settings = _settings(home)
        assert settings["theme"] == "Dracula"
        assert settings["mcpServers"]["mine"] == {"command": "x"}
        assert "contexer" in settings["mcpServers"]["contexer"]["command"]

    def test_wires_supported_hook_events(self, home):
        gemini.install(home)
        assert set(_settings(home)["hooks"]) >= {
            "SessionStart", "BeforeAgent", "AfterTool", "PreCompress", "SessionEnd"
        }
        assert "gemini.session_start" in "\n".join(_commands(home, "SessionStart"))
        assert "gemini.before_agent" in "\n".join(_commands(home, "BeforeAgent"))

    def test_write_hook_matches_official_edit_tools(self, home):
        gemini.install(home)
        group = _settings(home)["hooks"]["AfterTool"][0]
        assert group["matcher"] == "write_file|replace"

    def test_hooks_use_current_python_and_suppress_output(self, home):
        gemini.install(home)
        assert sys.executable in _commands(home, "BeforeAgent")[0]
        raw = json.dumps({"session_id": "s1", "prompt": "hello"})
        assert json.loads(gemini.after_write("", raw)) == {"suppressOutput": True}

    def test_install_is_idempotent(self, home):
        gemini.install(home)
        gemini.install(home)
        for event in ("SessionStart", "BeforeAgent", "AfterTool", "PreCompress", "SessionEnd"):
            assert len(_settings(home)["hooks"][event]) == 1


class TestGeminiRuntime:
    def test_session_start_injects_context_and_anchors_repo(self, home, tmp_path):
        repo = str(tmp_path / "repo")
        store.update_decision(repo, "always use uv for dependency management", "s1", "convention")
        raw = json.dumps({"session_id": "s1", "source": "startup"})
        out = json.loads(gemini.session_start(repo, raw))
        assert out["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert "always use uv" in out["hookSpecificOutput"]["additionalContext"]
        assert (store.STORE_DIR / ".current_repo").read_text() == repo

    def test_before_agent_captures_task_only_once_per_session(self, home, tmp_path):
        repo = str(tmp_path / "repo")
        first = json.dumps({
            "session_id": "s1", "prompt": "Implement OAuth login support for the web application"
        })
        second = json.dumps({
            "session_id": "s1", "prompt": "Now add logout support for every authenticated user"
        })
        gemini.before_agent(repo, first)
        gemini.before_agent(repo, second)
        assert "Implement OAuth login support" in store.get_context(repo)
        assert "Now add logout support" not in store.get_context(repo)

    def test_before_agent_captures_constraint_and_injects_ack(self, home, tmp_path):
        repo = str(tmp_path / "repo")
        raw = json.dumps({"session_id": "s1", "prompt": "always use conventional commits"})
        out = json.loads(gemini.before_agent(repo, raw))
        context = out["hookSpecificOutput"]["additionalContext"]
        assert "Auto-stored as constraint" in context
        assert "always use conventional commits" in store.get_context(repo).lower()

    def test_write_and_compress_flags_are_consumed_next_prompt(self, home, tmp_path):
        repo = str(tmp_path / "repo")
        store.update_decision(repo, "always run tests before committing", "s1", "constraint")
        raw = json.dumps({"session_id": "s1", "prompt": "continue"})
        gemini.after_write(repo, raw)
        gemini.pre_compress(repo, raw)
        out = json.loads(gemini.before_agent(repo, raw))
        context = out["hookSpecificOutput"]["additionalContext"]
        assert "wrote or edited files" in context
        assert "always run tests before committing" in context
        assert not (store.STORE_DIR / ".pending_capture").exists()
        assert not (store.STORE_DIR / ".gemini_pending_reload").exists()

    @pytest.mark.parametrize("entry", [
        gemini.session_start, gemini.before_agent, gemini.after_write,
        gemini.pre_compress, gemini.session_end,
    ])
    def test_entrypoints_never_raise_on_bad_stdin(self, home, entry):
        assert isinstance(entry("", "garbage"), str)


class TestGeminiUninstallAndStatus:
    def test_uninstall_removes_only_managed_entries(self, home):
        path = home / ".gemini" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({
            "mcpServers": {"mine": {"command": "x"}},
            "hooks": {"BeforeAgent": [{
                "matcher": "*", "hooks": [{"type": "command", "command": "./mine.sh"}]
            }]},
        }))
        gemini.install(home)
        gemini.uninstall(home)
        settings = _settings(home)
        assert settings["mcpServers"] == {"mine": {"command": "x"}}
        assert settings["hooks"]["BeforeAgent"][0]["hooks"][0]["command"] == "./mine.sh"

    def test_status_and_presence(self, home):
        assert gemini.is_present(home) is False
        assert gemini.is_installed(home) is False
        gemini.install(home)
        assert gemini.is_present(home) is True
        assert gemini.is_installed(home) is True
        assert "installed" in "\n".join(gemini.status_lines(home))

    def test_status_tolerates_corrupt_settings(self, home):
        path = home / ".gemini" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text("{ not json")
        assert gemini.is_installed(home) is False
