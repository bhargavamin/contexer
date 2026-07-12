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
        out = json.loads(gemini.after_write("", raw))
        assert "hookSpecificOutput" in out
        assert "additionalContext" in out["hookSpecificOutput"]

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
        assert "Always use uv" in out["hookSpecificOutput"]["additionalContext"]
        assert (store.STORE_DIR / ".current_repo").read_text() == repo

    def test_before_agent_captures_constraint_and_injects_ack(self, home, tmp_path):
        repo = str(tmp_path / "repo")
        raw = json.dumps({"session_id": "s1", "prompt": "always use conventional commits"})
        out = json.loads(gemini.before_agent(repo, raw))
        context = out["hookSpecificOutput"]["additionalContext"]
        assert "Auto-stored as constraint" in context
        assert "always use conventional commits" in store.get_context(repo).lower()

    def test_write_flag_injects_reminder_when_no_compress(self, home, tmp_path):
        repo = str(tmp_path / "repo")
        raw = json.dumps({"session_id": "s1", "prompt": "continue"})
        gemini.after_write(repo, raw)
        out = json.loads(gemini.before_agent(repo, raw))
        context = out["hookSpecificOutput"]["additionalContext"]
        assert "wrote or edited files" in context
        assert not (store.STORE_DIR / ".gemini_pending_capture").exists()

    def test_compress_flag_reloads_context_without_edit_reminder(self, home, tmp_path):
        repo = str(tmp_path / "repo")
        store.update_decision(repo, "always run tests before committing", "s1", "constraint")
        raw = json.dumps({"session_id": "s1", "prompt": "continue"})
        gemini.after_write(repo, raw)
        gemini.pre_compress(repo, raw)
        out = json.loads(gemini.before_agent(repo, raw))
        context = out["hookSpecificOutput"]["additionalContext"]
        # Reload takes priority; the edit reminder is suppressed (write happened
        # before compression, not on the immediately preceding turn).
        assert "wrote or edited files" not in context
        assert "Always run tests before committing" in context
        assert not (store.STORE_DIR / ".gemini_pending_capture").exists()
        assert not (store.STORE_DIR / ".gemini_pending_reload").exists()

    def test_pending_review_flag_injects_nudge(self, home, tmp_path):
        repo = str(tmp_path / "repo")
        raw = json.dumps({"session_id": "s1", "prompt": "continue"})
        store._touch_pending_review()  # the store drops this when a pending decision is created
        out = json.loads(gemini.before_agent(repo, raw))
        context = out["hookSpecificOutput"]["additionalContext"]
        assert "pending your review" in context
        assert not (store.STORE_DIR / ".pending_review").exists()  # consumed

    def test_no_pending_review_flag_no_nudge(self, home, tmp_path):
        repo = str(tmp_path / "repo")
        raw = json.dumps({"session_id": "s2", "prompt": "continue"})
        out = json.loads(gemini.before_agent(repo, raw))
        context = out.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "pending your review" not in context

    def test_reload_suppresses_review_nudge_and_consumes_flag(self, home, tmp_path):
        # A post-compression reload re-injects the pending count itself, so the separate review
        # nudge is skipped and the flag consumed silently (no double surfacing).
        repo = str(tmp_path / "repo")
        raw = json.dumps({"session_id": "s1", "prompt": "continue"})
        store._touch_pending_review()
        gemini.pre_compress(repo, raw)  # sets the reload flag
        out = json.loads(gemini.before_agent(repo, raw))
        context = out.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "clears the shown set" not in context  # the nudge phrase is suppressed
        assert not (store.STORE_DIR / ".pending_review").exists()  # consumed silently

    def test_clear_does_not_reset_first_prompt_marker(self, home, tmp_path):
        # The first-prompt marker gates the once-per-session bootstrap offer; /clear must
        # not delete it, or bootstrap would be re-offered mid-session.
        repo = str(tmp_path / "repo")
        startup = json.dumps({"session_id": "s1", "source": "startup",
                              "prompt": "build the new feature now"})
        clear = json.dumps({"session_id": "s1", "source": "clear"})
        gemini.session_start(repo, startup)
        gemini.before_agent(repo, startup)        # first prompt sets the marker
        marker = gemini._session_marker(startup)
        assert marker is not None and marker.exists()
        gemini.session_start(repo, clear)         # /clear must NOT delete the marker
        assert marker.exists()

    def test_no_session_id_runs_bootstrap_without_error(self, home, tmp_path):
        repo = str(tmp_path / "repo")
        # No session_id → _session_marker returns None; before_agent must still run safely.
        raw = json.dumps({"prompt": "build the feature"})
        assert isinstance(gemini.before_agent(repo, raw), str)
        assert isinstance(gemini.before_agent(repo, raw), str)

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

    def test_uninstall_does_not_write_when_nothing_to_remove(self, home):
        path = home / ".gemini" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"theme": "dark"}))
        mtime_before = path.stat().st_mtime
        gemini.uninstall(home)
        assert path.stat().st_mtime == mtime_before

    def test_reinstall_updates_stale_python_path(self, home):
        gemini.install(home)
        settings = _settings(home)
        # Simulate a stale install: command still contains the marker so _strip_stale
        # recognises it as a Contexer hook, but the Python path has changed.
        old_cmd = (
            'REPO=$(git rev-parse --show-toplevel 2>/dev/null || true) && '
            '"/old/python" -c "from contexer.adapters import gemini; import sys; '
            'print(gemini.before_agent(sys.argv[1], sys.stdin.read()))" "$REPO"'
        )
        settings["hooks"]["BeforeAgent"][0]["hooks"][0]["command"] = old_cmd
        path = home / ".gemini" / "settings.json"
        path.write_text(json.dumps(settings))
        gemini.install(home)
        cmds = _commands(home, "BeforeAgent")
        assert not any(c == old_cmd for c in cmds), "stale hook should have been replaced"
        assert any("gemini.before_agent" in c for c in cmds)

    def test_status_reports_partial_when_pre_compress_missing(self, home):
        gemini.install(home)
        settings = _settings(home)
        settings["hooks"].pop("PreCompress")
        (home / ".gemini" / "settings.json").write_text(json.dumps(settings))
        assert gemini.is_installed(home) is False
        assert "missing or partial" in "\n".join(gemini.status_lines(home))

    def test_status_tolerates_corrupt_settings(self, home):
        path = home / ".gemini" / "settings.json"
        path.parent.mkdir(parents=True)
        path.write_text("{ not json")
        assert gemini.is_installed(home) is False
