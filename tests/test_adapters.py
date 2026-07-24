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


# ── Doc Drift Layer 1 — Task 1.6: Claude entrypoints (post_write / drift) ───────

class TestClaudePostWrite:
    """post_write records edited files into the per-(repo, session) drift sidecar and arms
    the .pending_capture flag. It resolves the repo from its OWN cwd (no repo arg — exactly
    how the installed PostToolUse hook runs) and threads the host session_id."""
    SID = "sess-postwrite-1"

    def _stdin(self, file_path=None, edits=None, session_id=SID):
        data = {
            "session_id": session_id,
            "hook_event_name": "PostToolUse",
            "tool_name": "Write",
            "tool_input": {},
            "cwd": "/whatever",
        }
        if file_path is not None:
            data["tool_input"]["file_path"] = file_path
        if edits is not None:
            data["tool_input"]["edits"] = edits
        return _json.dumps(data)

    def test_records_edited_file_with_session(self, tmp_repo, monkeypatch):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(tmp_repo)
        assert claude.post_write(self._stdin(file_path="src/app.py")) == "{}"
        recorded = store._read_edited_files(tmp_repo, self.SID, clear=False)
        assert "src/app.py" in recorded

    def test_empty_session_records_nothing(self, tmp_repo, monkeypatch):
        # GAP-1 failure mode spelled out: no session_id => record_edited_file writes NO
        # sidecar, so drift would silently never fire.
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        store.STORE_DIR.mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(tmp_repo)
        raw = _json.dumps({"tool_input": {"file_path": "src/app.py"}})
        assert claude.post_write(raw) == "{}"
        assert list(store.STORE_DIR.glob(".edited_*")) == []

    def test_multiedit_records_every_path(self, tmp_repo, monkeypatch):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(tmp_repo)
        raw = self._stdin(file_path="top.py",
                          edits=[{"file_path": "a.py"}, {"file_path": "b.py"}])
        claude.post_write(raw)
        recorded = set(store._read_edited_files(tmp_repo, self.SID, clear=False))
        assert {"top.py", "a.py", "b.py"} <= recorded

    def test_touches_pending_capture(self, tmp_repo, tmp_path, monkeypatch):
        # ~/.contexer/.pending_capture must still be armed (the anchor's capture reminder).
        monkeypatch.setenv("HOME", str(tmp_path))
        (tmp_path / ".contexer").mkdir(parents=True, exist_ok=True)
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        monkeypatch.chdir(tmp_repo)
        claude.post_write(self._stdin(file_path="src/app.py"))
        assert (tmp_path / ".contexer" / ".pending_capture").exists()

    def test_failsoft_on_bad_stdin(self, tmp_repo):
        assert claude.post_write("garbage not json") == "{}"
        assert claude.post_write("") == "{}"


class TestClaudeDrift:
    """drift renders the doc-drift advisory for files edited this session, mirroring the
    rationale entrypoint's additionalContext envelope. Fail-soft, never raises."""
    SID = "sess-drift-1"

    def _seed(self, tmp_repo):
        store.update_decision(
            tmp_repo, "hot counter uses Memcached, not Redis; see cache/redis.py",
            "human-sess", "architecture", created_by="human")
        target = Path(tmp_repo) / "cache" / "redis.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('"""Redis cache client in cache/redis.py."""\n', encoding="utf-8")
        return str(target)

    def _prompt_stdin(self, session_id=SID, prompt="continue the work"):
        return _json.dumps({"session_id": session_id, "hook_event_name": "UserPromptSubmit",
                            "prompt": prompt})

    def test_renders_when_payload_nonempty(self, tmp_repo):
        target = self._seed(tmp_repo)
        store.record_edited_file(tmp_repo, target, self.SID)
        out = _json.loads(claude.drift(tmp_repo, self._prompt_stdin()))
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert "[Contexer] Checked" in ctx
        assert "Memcached" in ctx

    def test_empty_payload_returns_brace(self, tmp_repo):
        # Nothing recorded as edited this session -> silent.
        assert claude.drift(tmp_repo, self._prompt_stdin()) == "{}"

    def test_mirrors_rationale_envelope(self, tmp_repo):
        target = self._seed(tmp_repo)
        store.record_edited_file(tmp_repo, target, self.SID)
        out = _json.loads(claude.drift(tmp_repo, self._prompt_stdin()))
        assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert "additionalContext" in out["hookSpecificOutput"]

    def test_failsoft_on_bad_stdin(self, tmp_repo):
        assert claude.drift(tmp_repo, "garbage") == "{}"


class TestClaudeDriftHandshake:
    """GAP-1 (the whole point of the task): post_write WRITES the per-(repo, session) sidecar
    and drift READS it — they must resolve the SAME session_id AND repo, or post_write's
    write lands where drift never looks and the advisory silently never fires while every
    unit test stays green. This drives both hooks with realistic host stdin fixtures."""
    SID = "handshake-session-9f3a"

    def _seed(self, tmp_repo):
        # An approved, human-sourced (drift-trusted) decision that contradicts the docstring
        # of the file it co-references.
        store.update_decision(
            tmp_repo, "hot counter uses Memcached, not Redis; see cache/redis.py",
            "human-sess", "architecture", created_by="human")
        target = Path(tmp_repo) / "cache" / "redis.py"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text('"""Redis cache client in cache/redis.py."""\n', encoding="utf-8")
        return str(target)

    def test_post_write_then_drift_same_session_renders(self, tmp_repo, monkeypatch):
        target = self._seed(tmp_repo)
        # post_write resolves the repo from its own cwd, exactly as the installed PostToolUse
        # hook runs (no repo arg), so drive it FROM the repo dir.
        monkeypatch.chdir(tmp_repo)
        post_stdin = _json.dumps({
            "session_id": self.SID, "hook_event_name": "PostToolUse", "tool_name": "Write",
            "tool_input": {"file_path": target, "content": "..."}, "cwd": tmp_repo})
        assert claude.post_write(post_stdin) == "{}"
        # SAME session id => the advisory renders through the drift hook.
        same = _json.dumps({"session_id": self.SID, "hook_event_name": "UserPromptSubmit",
                            "prompt": "keep going"})
        out = _json.loads(claude.drift(tmp_repo, same))
        assert "additionalContext" in out["hookSpecificOutput"]
        assert "Memcached" in out["hookSpecificOutput"]["additionalContext"]

    def test_different_session_does_not_render(self, tmp_repo, monkeypatch):
        target = self._seed(tmp_repo)
        monkeypatch.chdir(tmp_repo)
        post_stdin = _json.dumps({
            "session_id": self.SID, "hook_event_name": "PostToolUse", "tool_name": "Write",
            "tool_input": {"file_path": target}, "cwd": tmp_repo})
        claude.post_write(post_stdin)
        # A DIFFERENT session never wrote a sidecar => nothing to surface (isolation holds).
        other = _json.dumps({"session_id": "some-other-session", "prompt": "keep going"})
        assert claude.drift(tmp_repo, other) == "{}"


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
