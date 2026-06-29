"""Tests for the multi-provider adapter registry."""
import json as _json
from pathlib import Path

import pytest

from contexer import adapters, store


class TestRegistry:
    def test_all_returns_known_adapters(self):
        names = {a.NAME for a in adapters.all_adapters()}
        assert names == {"claude", "cursor", "codex", "gemini"}

    def test_get_by_name(self):
        assert adapters.get("claude").NAME == "claude"
        assert adapters.get("cursor").NAME == "cursor"
        assert adapters.get("codex").NAME == "codex"
        assert adapters.get("gemini").NAME == "gemini"

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

    def test_detects_codex_when_dot_codex_present(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".codex").mkdir()
        detected = {a.NAME for a in adapters.detect()}
        assert detected == {"codex"}

    def test_detects_gemini_when_dot_gemini_present(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".gemini").mkdir()
        detected = {a.NAME for a in adapters.detect()}
        assert detected == {"gemini"}

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
        assert {a.NAME for a in adapters.select("all")} == {
            "claude", "cursor", "codex", "gemini"
        }

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
        assert claude.capture_constraint(tmp_repo, "garbage") == "{}"
        assert claude.rationale(tmp_repo, "garbage") == "{}"


from contexer.adapters import cursor


class TestCursorFormatters:
    def test_session_start_injects_additional_context_with_nudge(self):
        d = cursor.format_session_start({"status": "ignored on cursor", "context": "RULES"})
        assert d["additional_context"].startswith("RULES")
        assert "get_context" in d["additional_context"]   # behavioral nudge appended
        assert "update_context" in d["additional_context"]
        assert "systemMessage" not in d  # cursor has no systemMessage channel

    def test_session_start_empty_context_still_emits_nudge_only(self):
        # Even with no stored rules, the nudge alone is worth injecting once.
        d = cursor.format_session_start({"status": "", "context": ""})
        assert "get_context" in d["additional_context"]

    def test_prompt_passthrough_continue_true(self):
        assert cursor.format_prompt_passthrough() == {"continue": True}


class TestCursorEntrypoints:
    def test_session_start_writes_current_repo_from_workspace_roots(self, tmp_repo, monkeypatch):
        from contexer import store
        raw = _json.dumps({"workspace_roots": [tmp_repo], "session_id": "s1"})
        out = _json.loads(cursor.session_start("", raw))
        assert "additional_context" in out
        assert (store.STORE_DIR / ".current_repo").read_text() == tmp_repo

    def test_session_start_writes_managed_rule_file(self, tmp_repo):
        from pathlib import Path
        raw = _json.dumps({"workspace_roots": [tmp_repo], "session_id": "s1"})
        cursor.session_start("", raw)
        rule = Path(tmp_repo) / ".cursor" / "rules" / "contexer.mdc"
        assert rule.exists()
        body = rule.read_text()
        assert "alwaysApply: true" in body
        assert "managed by contexer" in body
        assert "get_context" in body and "update_context" in body

    def test_session_start_does_not_overwrite_user_rule_file(self, tmp_repo):
        from pathlib import Path
        rule = Path(tmp_repo) / ".cursor" / "rules" / "contexer.mdc"
        rule.parent.mkdir(parents=True)
        rule.write_text("my own rule, hands off")
        raw = _json.dumps({"workspace_roots": [tmp_repo], "session_id": "s1"})
        cursor.session_start("", raw)
        assert rule.read_text() == "my own rule, hands off"

    def test_capture_anchors_current_repo(self, tmp_repo):
        # Every beforeSubmitPrompt must refresh the pointer so bare get_context({}) resolves.
        from contexer import store
        raw = _json.dumps({"prompt": "always use conventional commits", "session_id": "s1",
                           "workspace_roots": [tmp_repo]})
        cursor.capture_constraint("", raw)
        assert (store.STORE_DIR / ".current_repo").read_text() == tmp_repo

    def test_capture_does_not_anchor_config_dir(self, tmp_repo, monkeypatch):
        from pathlib import Path
        from contexer import store
        store.STORE_DIR.mkdir(parents=True, exist_ok=True)
        (store.STORE_DIR / ".current_repo").write_text(tmp_repo)  # a sane prior value
        raw = _json.dumps({"prompt": "hi", "workspace_roots": [str(Path.home() / ".claude")]})
        cursor.capture_constraint("", raw)
        # config-dir workspace must never overwrite the pointer
        assert (store.STORE_DIR / ".current_repo").read_text() == tmp_repo

    def test_capture_constraint_writes_and_passes_through(self, tmp_repo):
        from contexer import store
        raw = _json.dumps({"prompt": "always use conventional commits", "session_id": "s1"})
        assert _json.loads(cursor.capture_constraint(tmp_repo, raw)) == {"continue": True}
        assert "conventional commits" in store.get_context(tmp_repo, entry_type="convention").lower() \
            or "conventional commits" in store.get_context(tmp_repo, entry_type="constraint").lower()

    def test_entrypoints_never_raise(self, tmp_repo):
        assert _json.loads(cursor.capture_constraint(tmp_repo, "garbage")) == {"continue": True}
        assert _json.loads(cursor.session_start("", "garbage"))  # returns dict, no raise
