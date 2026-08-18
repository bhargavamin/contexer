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

    def test_before_agent_deictic_directive_acks_pending(self, home, tmp_path):
        # decision ceb955f5: deictic directives are stored pending_approval, not trusted.
        repo = str(tmp_path / "repo")
        raw = json.dumps({
            "session_id": "s1",
            "prompt": "I'm not going to accept any performance degradation so ensure you "
                      "clarify and ensure this feature is actual improvement",
        })
        out = json.loads(gemini.before_agent(repo, raw))
        context = out["hookSpecificOutput"]["additionalContext"]
        assert "pending" in context.lower()
        assert "Auto-stored as constraint" not in context

    def test_write_flag_injects_reminder_when_no_compress(self, home, tmp_path):
        repo = str(tmp_path / "repo")
        raw = json.dumps({"session_id": "s1", "prompt": "continue"})
        gemini.after_write(repo, raw)
        out = json.loads(gemini.before_agent(repo, raw))
        context = out["hookSpecificOutput"]["additionalContext"]
        assert "wrote or edited files" in context
        assert not (store.STORE_DIR / ".gemini_pending_capture").exists()

    def test_after_write_records_edited_file(self, home, tmp_path):
        # issue #175 Task 2: the same edited-files signal Claude/Codex record via
        # PostToolUse - Gemini records it from AfterTool(write_file|replace) instead.
        repo = str(tmp_path / "repo")
        raw = json.dumps({
            "session_id": "s1",
            "tool_input": {"file_path": str(tmp_path / "repo" / "src" / "a.py")},
        })
        gemini.after_write(repo, raw)
        assert store._read_edited_files(repo) == ["src/a.py"]

    def test_after_write_fail_soft_on_missing_tool_input(self, home, tmp_path):
        repo = str(tmp_path / "repo")
        raw = json.dumps({"session_id": "s1", "prompt": "continue"})
        out = json.loads(gemini.after_write(repo, raw))  # must not raise
        assert "hookSpecificOutput" in out
        assert store._read_edited_files(repo) == []

    def test_after_write_fail_soft_on_garbage_stdin(self, home, tmp_path):
        repo = str(tmp_path / "repo")
        out = json.loads(gemini.after_write(repo, "not json"))  # must not raise
        assert "hookSpecificOutput" in out

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

    def test_compress_flag_rehydrates_working_set(self, home, tmp_path):
        # session_id threading (Retrieval V1 compact-reload parity): a reload after
        # compression rehydrates the CONTENT of decisions the router already surfaced this
        # session, not just the general rules - mirroring Claude's SessionStart(compact).
        repo = str(tmp_path / "repo")
        store.update_decision(
            repo, "JWT refresh tokens expire after fifteen minutes and live in httpOnly cookies",
            "s1", "architecture")
        prompt = "why do jwt refresh tokens expire in httpOnly cookies?"
        store.get_context_for_prompt(repo, prompt, "s1")  # populates the working set
        raw = json.dumps({"session_id": "s1", "prompt": prompt})
        gemini.after_write(repo, raw)
        gemini.pre_compress(repo, raw)
        out = json.loads(gemini.before_agent(repo, raw))
        context = out["hookSpecificOutput"]["additionalContext"]
        assert "Rehydrated working context" in context

    def test_pending_review_flag_injects_nudge(self, home, tmp_path):
        repo = str(tmp_path / "repo")
        raw = json.dumps({"session_id": "s1", "prompt": "continue"})
        store.update_decision(repo, "Never deploy on Fridays", "s1", "constraint")  # sets per-repo flag
        out = json.loads(gemini.before_agent(repo, raw))
        context = out["hookSpecificOutput"]["additionalContext"]
        assert "pending your review" in context
        assert not store._pending_review_flag(repo).exists()  # consumed

    def test_no_pending_review_flag_no_nudge(self, home, tmp_path):
        repo = str(tmp_path / "repo")
        raw = json.dumps({"session_id": "s2", "prompt": "continue"})
        out = json.loads(gemini.before_agent(repo, raw))
        context = out.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "pending your review" not in context

    def test_reload_still_fires_review_nudge(self, home, tmp_path):
        # Greptile #3: a reload reloads get_context (which EXCLUDES pending decisions), so the
        # review nudge must still fire - not be silently swallowed by the reload branch.
        repo = str(tmp_path / "repo")
        raw = json.dumps({"session_id": "s1", "prompt": "continue"})
        store.update_decision(repo, "Never deploy on Fridays", "s1", "constraint")
        gemini.pre_compress(repo, raw)  # sets the reload flag
        out = json.loads(gemini.before_agent(repo, raw))
        context = out["hookSpecificOutput"]["additionalContext"]
        assert "pending your review" in context  # fires despite the reload
        assert not store._pending_review_flag(repo).exists()

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


class TestGeminiAfterWriteHookCwdFallback:
    """Greptile P1, PR #181: in a non-git project the installed AfterTool hook's shell
    wrapper computes an empty $REPO (`git rev-parse --show-toplevel || true`), and
    after_write used to resolve that via `store._resolve_repo`, which - in this
    hook-invoked process (not the MCP server, so `_SESSION_REPO` is always empty) -
    falls through to the shared `.current_repo` pointer, recording the edit under
    whatever OTHER repo that pointer names (or discarding it). Fixed by falling back to
    the hook's own cwd (`store._hook_cwd_repo`), matching claude.post_write."""

    def test_empty_repo_records_under_hook_cwd_in_non_git_project(self, home, tmp_path, monkeypatch):
        project = tmp_path / "non_git_project"
        (project / "src").mkdir(parents=True)
        monkeypatch.chdir(project)
        raw = json.dumps({
            "session_id": "s1",
            "tool_input": {"file_path": str(project / "src" / "a.py")},
        })
        out = json.loads(gemini.after_write("", raw))
        assert "hookSpecificOutput" in out
        assert store._read_edited_files(str(project)) == ["src/a.py"]

    def test_empty_repo_never_misroutes_to_current_repo_pointer(self, home, tmp_path, monkeypatch):
        other_repo = tmp_path / "other_repo"
        other_repo.mkdir()
        store.anchor_repo(str(other_repo))  # some earlier session pointed .current_repo here
        project = tmp_path / "non_git_project"
        (project / "src").mkdir(parents=True)
        monkeypatch.chdir(project)
        raw = json.dumps({
            "session_id": "s1",
            "tool_input": {"file_path": str(project / "src" / "a.py")},
        })
        gemini.after_write("", raw)
        assert store._read_edited_files(str(other_repo)) == [], \
            "edit must not be recorded under the unrelated .current_repo pointer target"
        assert store._read_edited_files(str(project)) == ["src/a.py"]

    def test_empty_repo_in_home_dir_records_nothing_and_does_not_crash(self, home, monkeypatch):
        # _is_sane_repo rejects the home dir itself: no cwd fallback, no recording, no crash.
        monkeypatch.chdir(home)
        raw = json.dumps({
            "session_id": "s1",
            "tool_input": {"file_path": str(home / "a.py")},
        })
        out = json.loads(gemini.after_write("", raw))
        assert "hookSpecificOutput" in out
        assert store._read_edited_files(str(home)) == []


class TestGeminiBeforeAgentHookCwdFallback:
    """Greptile P1 #2, PR #181, follow-up to 3fde7aa: after_write records the edited-file
    signal under `store._hook_cwd_repo`, but `before_agent` - where CAPTURE actually runs
    (`capture_user_constraint`, plus the pending-review nudge and context payloads) - used
    to resolve its repo via bare `store._resolve_repo`, which in this hook-invoked process
    (not the MCP server, so `_SESSION_REPO` is always empty) falls through to the shared
    `.current_repo` pointer on an empty hook-supplied repo. In a non-git project, if another
    session moved the pointer between hook events, the edit was recorded under the
    cwd-keyed store while capture read anchor candidates from the pointer-keyed store - a
    writer/reader repo-key split, the same shape as the session-id bug covered by
    TestAnchorCandidates.test_hook_written_signal_reaches_a_different_server_session in
    test_store.py, now at the repo-key level. Fixed by resolving through
    `store._hook_cwd_repo` in before_agent (and session_start) too, matching after_write."""

    _DEICTIC_DIRECTIVE = "always validate this feature before shipping"

    def test_before_agent_capture_attaches_candidates_recorded_at_the_same_cwd(
            self, home, tmp_path, monkeypatch):
        project = tmp_path / "non_git_project"
        (project / "src").mkdir(parents=True)
        monkeypatch.chdir(project)

        # Writer: after_write records the edit under the cwd-keyed store.
        write_raw = json.dumps({
            "session_id": "s1",
            "tool_input": {"file_path": str(project / "src" / "a.py")},
        })
        gemini.after_write("", write_raw)
        assert store._read_edited_files(str(project)) == ["src/a.py"]

        # Reader: before_agent-driven capture on the SAME cwd must see that recording.
        prompt_raw = json.dumps({"session_id": "s2", "prompt": self._DEICTIC_DIRECTIVE})
        gemini.before_agent("", prompt_raw)

        entries = [e for e in store._load(str(project))["entries"] if e["type"] == "decision"]
        assert entries, "before_agent must have captured the deictic directive"
        assert entries[0]["status"] == "pending_approval"
        assert entries[0].get("anchor_candidates") == ["src/a.py"], (
            "capture must read anchor candidates from the SAME cwd-keyed store after_write "
            "recorded the edit into - this is the writer/reader agreement regression test"
        )

    def test_pointer_never_hijacks_either_side(self, home, tmp_path, monkeypatch):
        other_repo = tmp_path / "other_repo"
        other_repo.mkdir()
        store.anchor_repo(str(other_repo))  # an earlier session pointed .current_repo here
        project = tmp_path / "non_git_project"
        (project / "src").mkdir(parents=True)
        monkeypatch.chdir(project)

        write_raw = json.dumps({
            "session_id": "s1",
            "tool_input": {"file_path": str(project / "src" / "a.py")},
        })
        gemini.after_write("", write_raw)

        prompt_raw = json.dumps({"session_id": "s2", "prompt": self._DEICTIC_DIRECTIVE})
        gemini.before_agent("", prompt_raw)

        other_decisions = [e for e in store._load(str(other_repo))["entries"]
                            if e["type"] == "decision"]
        assert other_decisions == [], \
            "capture must not land under the unrelated .current_repo pointer target"
        assert store._read_edited_files(str(other_repo)) == []

        project_decisions = [e for e in store._load(str(project))["entries"]
                              if e["type"] == "decision"]
        assert project_decisions and project_decisions[0].get("anchor_candidates") == ["src/a.py"]


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
