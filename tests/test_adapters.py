"""Tests for the multi-provider adapter registry."""
import json as _json
from pathlib import Path

import pytest

from contexer import adapters, store
from contexer.adapters import claude, cursor


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

    def test_capture_constraint_deictic_directive_acks_pending(self, tmp_repo):
        # decision ceb955f5: a deictic directive is stored but the ack must say PENDING
        # review, not "stored as a constraint" — it is not auto-trusted.
        raw = _json.dumps({
            "prompt": "I'm not going to accept any performance degradation so ensure you "
                      "clarify and ensure this feature is actual improvement",
            "session_id": "s1",
        })
        out = _json.loads(claude.capture_constraint(tmp_repo, raw))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "pending" in ctx.lower()
        assert "review_pending" in ctx or "contexer review" in ctx
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        assert entry["status"] == "pending_approval"

    def test_rationale_injects_when_decisions_match(self, populated_repo):
        # Targets the fixture's retrievable (suggested) decision. The JWT entry classifies
        # as pending_approval and is deliberately not auto-injected (only approved/suggested
        # decisions reach the index — pending ones surface via review_pending).
        raw = _json.dumps({"prompt": "why did we choose bcrypt for password hashing?"})
        out = _json.loads(claude.rationale(populated_repo, raw))
        assert "additionalContext" in out["hookSpecificOutput"]

    def test_rationale_noop_on_plain_prompt(self, populated_repo):
        raw = _json.dumps({"prompt": "add a test"})
        assert claude.rationale(populated_repo, raw) == "{}"

    def test_rationale_note_shows_tokens_saved(self, tmp_repo):
        # Above the _COST_NOTE_TOKENS gate the notice reports estimated SAVINGS
        # (est × (_SAVED_MULTIPLIER − 1), nearest 10) — never the injected count.
        # Same "constraint:" wording as the populated_repo fixture's retrievable
        # entry, padded long enough that the injected ctx exceeds the gate.
        content = (
            "constraint: never store plaintext passwords, always use bcrypt for "
            "password hashing. Rationale: "
            + "bcrypt has adaptive cost tuning and a battle-tested history. " * 15
        )
        store.update_decision(tmp_repo, content, "sess-1")
        raw = _json.dumps({"prompt": "why did we choose bcrypt for password hashing?"})
        out = _json.loads(claude.rationale(tmp_repo, raw))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        est = max(1, len(ctx) // 4)
        assert est > claude._COST_NOTE_TOKENS  # guard: test data must clear the gate
        saved = int(round(est * (claude._SAVED_MULTIPLIER - 1), -1))
        msg = out["systemMessage"]
        assert msg.endswith(f"· ~{saved} tokens saved"), msg
        assert f"~{est} tokens" not in msg  # injected count no longer shown

    def test_rationale_note_silent_below_gate(self, populated_repo):
        # Short injection (fixture's two one-line decisions) stays under the gate:
        # notice names what was recalled but claims no savings number.
        raw = _json.dumps({"prompt": "why did we choose bcrypt for password hashing?"})
        out = _json.loads(claude.rationale(populated_repo, raw))
        assert "additionalContext" in out["hookSpecificOutput"]
        assert "tokens saved" not in out["systemMessage"]

    def test_entrypoints_never_raise_on_bad_stdin(self, tmp_repo):
        assert claude.capture_constraint(tmp_repo, "garbage") == "{}"
        assert claude.rationale(tmp_repo, "garbage") == "{}"


class TestClaudePostWrite:
    """PostToolUse (Write|Edit) entrypoint (issue #175 Task 2): records the edited file
    into the per-session sidecar AND arms .pending_capture. Replaces the old shell-only
    `touch ~/.contexer/.pending_capture` hook."""

    def test_payload_round_trip_records_edited_file(self, tmp_repo):
        raw = _json.dumps({
            "session_id": "s1",
            "tool_input": {"file_path": str(Path(tmp_repo) / "src" / "a.py")},
        })
        out = claude.post_write(tmp_repo, raw)
        assert out == "{}"
        assert store._read_edited_files(tmp_repo, "s1", clear=False) == ["src/a.py"]

    def test_arms_pending_capture_flag(self, tmp_repo):
        raw = _json.dumps({"session_id": "s1", "tool_input": {"file_path": "a.py"}})
        assert not (store.STORE_DIR / ".pending_capture").exists()
        claude.post_write(tmp_repo, raw)
        assert (store.STORE_DIR / ".pending_capture").exists()

    def test_always_returns_empty_json(self, tmp_repo):
        raw = _json.dumps({"session_id": "s1", "tool_input": {"file_path": "a.py"}})
        assert claude.post_write(tmp_repo, raw) == "{}"

    def test_fail_soft_on_garbage_stdin(self, tmp_repo):
        assert claude.post_write(tmp_repo, "not json") == "{}"
        # Still arms the flag — a malformed payload must not cost the deterministic
        # capture-reminder signal, only the edited-file recording (which has nothing to record).
        assert (store.STORE_DIR / ".pending_capture").exists()

    def test_fail_soft_on_missing_tool_input(self, tmp_repo):
        raw = _json.dumps({"session_id": "s1"})
        assert claude.post_write(tmp_repo, raw) == "{}"
        assert store._read_edited_files(tmp_repo, "s1", clear=False) == []

    def test_fail_soft_on_non_string_file_path(self, tmp_repo):
        raw = _json.dumps({"session_id": "s1", "tool_input": {"file_path": 42}})
        assert claude.post_write(tmp_repo, raw) == "{}"
        assert store._read_edited_files(tmp_repo, "s1", clear=False) == []

    def test_no_session_id_records_nothing_but_still_returns_empty_json(self, tmp_repo):
        raw = _json.dumps({"tool_input": {"file_path": "a.py"}})
        assert claude.post_write(tmp_repo, raw) == "{}"

    def test_pending_capture_write_failure_is_fail_soft(self, tmp_repo, monkeypatch):
        # The flag write is best-effort (#152): an unwritable STORE_DIR must not break the
        # hook's mandatory JSON output, mirroring the shell hook's `touch ... 2>/dev/null`.
        def _boom(self, *a, **k):
            raise OSError("nope")
        monkeypatch.setattr(Path, "mkdir", _boom)
        raw = _json.dumps({"session_id": "s1", "tool_input": {"file_path": "a.py"}})
        assert claude.post_write(tmp_repo, raw) == "{}"

    def test_record_edited_file_failure_is_fail_soft(self, tmp_repo, monkeypatch):
        monkeypatch.setattr(store, "record_edited_file",
                             lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        raw = _json.dumps({"session_id": "s1", "tool_input": {"file_path": "a.py"}})
        assert claude.post_write(tmp_repo, raw) == "{}"


class TestClaudePostWriteRepoResolutionParity:
    """The doc-drift hazard: post_write's shell wrapper must resolve $REPO IDENTICALLY to
    every sibling UserPromptSubmit hook (git-toplevel, never raw cwd) — a mismatch would
    key record_edited_file's write and Task 3's capture-time read under different sidecar
    slugs, silently killing the feature for any project not opened at its git root."""

    def _post_toolusecmd(self, home):
        settings = _json.loads((home / ".claude" / "settings.json").read_text())
        cmds = [h["command"] for grp in settings["hooks"]["PostToolUse"]
                for h in grp.get("hooks", []) if "command" in h]
        return next(c for c in cmds if "claude.post_write" in c)

    def _sibling_prefix(self, home):
        settings = _json.loads((home / ".claude" / "settings.json").read_text())
        cmds = [h["command"] for grp in settings["hooks"]["UserPromptSubmit"]
                for h in grp.get("hooks", []) if "command" in h]
        rationale_cmd = next(c for c in cmds if "claude.rationale" in c)
        # Repo-resolution prefix: everything up to and including the `&&` that starts
        # the python invocation.
        return rationale_cmd.split("&&")[0] + "&&"

    def test_post_write_prefix_matches_sibling_user_prompt_submit_hooks(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        from contexer.cli import install
        install()
        post_write_cmd = self._post_toolusecmd(tmp_path)
        sibling_prefix = self._sibling_prefix(tmp_path)
        assert post_write_cmd.startswith(sibling_prefix), (
            f"post_write's $REPO resolution {post_write_cmd!r} must match the sibling "
            f"UserPromptSubmit hooks' prefix {sibling_prefix!r}"
        )
        assert "show-toplevel" in sibling_prefix  # guard: the prefix we compared against
                                                     # actually uses git-toplevel resolution


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
        raw = _json.dumps({"workspace_roots": [tmp_repo], "session_id": "s1"})
        cursor.session_start("", raw)
        rule = Path(tmp_repo) / ".cursor" / "rules" / "contexer.mdc"
        assert rule.exists()
        body = rule.read_text()
        assert "alwaysApply: true" in body
        assert "managed by contexer" in body
        assert "get_context" in body and "update_context" in body

    def test_session_start_does_not_overwrite_user_rule_file(self, tmp_repo):
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
