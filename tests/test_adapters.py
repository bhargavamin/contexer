"""Tests for the multi-provider adapter registry."""
import json as _json
from pathlib import Path

import pytest

from contexer import adapters, store


class TestRegistry:
    def test_all_returns_known_adapters(self):
        names = {a.NAME for a in adapters.all_adapters()}
        assert names == {"claude", "cursor"}

    def test_get_by_name(self):
        assert adapters.get("claude").NAME == "claude"
        assert adapters.get("cursor").NAME == "cursor"

    def test_get_unknown_raises(self):
        with pytest.raises(KeyError):
            adapters.get("emacs")


class TestDetect:
    def test_detects_claude_when_dot_claude_present(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".claude").mkdir()
        detected = {a.NAME for a in adapters.detect()}
        assert detected == {"claude"}

    def test_detects_cursor_when_dot_cursor_present(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".cursor").mkdir()
        detected = {a.NAME for a in adapters.detect()}
        assert detected == {"cursor"}

    def test_detects_both(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".cursor").mkdir()
        assert {a.NAME for a in adapters.detect()} == {"claude", "cursor"}

    def test_detect_empty_when_none_present(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert adapters.detect() == []


class TestSelect:
    def test_select_all(self):
        assert {a.NAME for a in adapters.select("all")} == {"claude", "cursor"}

    def test_select_one(self):
        assert [a.NAME for a in adapters.select("cursor")] == ["cursor"]

    def test_select_unknown_raises(self):
        with pytest.raises(KeyError):
            adapters.select("emacs")


from contexer.adapters import claude


class TestClaudeFormatters:
    def test_session_start_with_context(self):
        d = claude.format_session_start({"status": "S", "context": "C"})
        assert d["systemMessage"] == "S"
        assert d["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert d["hookSpecificOutput"]["additionalContext"] == "C"

    def test_session_start_empty_context_omits_injection(self):
        d = claude.format_session_start({"status": "only status", "context": ""})
        assert d == {"systemMessage": "only status"}

    def test_bootstrap_prompt_empty_is_empty_dict(self):
        assert claude.format_bootstrap_prompt({"status": "", "context": ""}) == {}

    def test_bootstrap_prompt_with_context(self):
        d = claude.format_bootstrap_prompt({"status": "", "context": "menu"})
        assert d["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert d["hookSpecificOutput"]["additionalContext"] == "menu"

    def test_post_compact_combines_status_and_context(self):
        d = claude.format_post_compact({"status": "reloaded", "context": "ctx"})
        assert d == {"systemMessage": "reloaded\nctx"}

    def test_post_compact_context_only(self):
        d = claude.format_post_compact({"status": "", "context": "bootstrap lines"})
        assert d == {"systemMessage": "bootstrap lines"}


class TestClaudeCaptureEntrypoints:
    def test_capture_task_stores_and_prints_empty(self, tmp_repo):
        raw = _json.dumps({"prompt": "Refactor the auth module to use JWT", "session_id": "s1"})
        assert claude.capture_task(tmp_repo, raw) == "{}"
        assert "Last task" in store.get_context(tmp_repo)

    def test_capture_task_ignores_question(self, tmp_repo):
        raw = _json.dumps({"prompt": "what is this repo?", "session_id": "s1"})
        assert claude.capture_task(tmp_repo, raw) == "{}"

    def test_capture_constraint_stores_and_acks(self, tmp_repo):
        raw = _json.dumps({"prompt": "always use conventional commits", "session_id": "s1"})
        out = _json.loads(claude.capture_constraint(tmp_repo, raw))
        assert "additionalContext" in out["hookSpecificOutput"]
        assert "constraint" in out["hookSpecificOutput"]["additionalContext"].lower()

    def test_capture_constraint_noop_on_plain_prompt(self, tmp_repo):
        raw = _json.dumps({"prompt": "please add a button", "session_id": "s1"})
        assert claude.capture_constraint(tmp_repo, raw) == "{}"

    def test_rationale_injects_when_decisions_match(self, populated_repo):
        raw = _json.dumps({"prompt": "why did we choose JWT for authentication?"})
        out = _json.loads(claude.rationale(populated_repo, raw))
        assert "additionalContext" in out["hookSpecificOutput"]

    def test_rationale_noop_on_plain_prompt(self, populated_repo):
        raw = _json.dumps({"prompt": "add a test"})
        assert claude.rationale(populated_repo, raw) == "{}"

    def test_entrypoints_never_raise_on_bad_stdin(self, tmp_repo):
        assert claude.capture_task(tmp_repo, "garbage") == "{}"
        assert claude.capture_constraint(tmp_repo, "garbage") == "{}"
        assert claude.rationale(tmp_repo, "garbage") == "{}"
