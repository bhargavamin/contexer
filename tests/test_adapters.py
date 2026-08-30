"""Tests for the multi-provider adapter registry."""
import json as _json
from pathlib import Path

import pytest

from contexer import adapters, spool, store
from contexer.adapters import base, claude, cursor


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
        data = store.load(tmp_repo)
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
        # An unparseable payload yields no prompt, so it is not evidence of a directive.
        assert spool.evidence_diagnostics(tmp_repo)["pending"] == 0


class TestClaudePostWrite:
    """PostToolUse (Write|Edit) entrypoint (issue #175 Task 2): records the edited file
    into the per-repo sidecar AND arms .pending_capture. Replaces the old shell-only
    `touch ~/.contexer/.pending_capture` hook."""

    def test_payload_round_trip_records_edited_file(self, tmp_repo):
        raw = _json.dumps({
            "session_id": "s1",
            "tool_input": {"file_path": str(Path(tmp_repo) / "src" / "a.py")},
        })
        out = claude.post_write(tmp_repo, raw)
        assert out == "{}"
        assert store._read_edited_files(tmp_repo) == ["src/a.py"]

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
        # …and nothing was recorded, so the evidence spool has nothing to say either.
        assert spool.evidence_diagnostics(tmp_repo)["pending"] == 0

    def test_fail_soft_on_missing_tool_input(self, tmp_repo):
        raw = _json.dumps({"session_id": "s1"})
        assert claude.post_write(tmp_repo, raw) == "{}"
        assert store._read_edited_files(tmp_repo) == []

    def test_fail_soft_on_non_string_file_path(self, tmp_repo):
        raw = _json.dumps({"session_id": "s1", "tool_input": {"file_path": 42}})
        assert claude.post_write(tmp_repo, raw) == "{}"
        assert store._read_edited_files(tmp_repo) == []

    def test_records_even_when_the_payload_carries_no_session_id(self, tmp_repo):
        # The sidecar is keyed per repo, so the host's session id is irrelevant to it (C1):
        # a payload without one must still record, not silently drop the signal.
        raw = _json.dumps({"tool_input": {"file_path": "a.py"}})
        assert claude.post_write(tmp_repo, raw) == "{}"
        assert store._read_edited_files(tmp_repo) == ["a.py"]

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

    def test_record_edited_file_failure_does_not_skip_the_pending_capture_arm(self, tmp_repo, monkeypatch):
        # The two best-effort signals must fail independently: a non-OSError escaping
        # record_edited_file (e.g. from guard_engine) must not also cost the deterministic
        # capture-reminder flag — they are wrapped in separate try/except blocks.
        monkeypatch.setattr(store, "record_edited_file",
                             lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        raw = _json.dumps({"session_id": "s1", "tool_input": {"file_path": "a.py"}})
        assert not (store.STORE_DIR / ".pending_capture").exists()
        assert claude.post_write(tmp_repo, raw) == "{}"
        assert (store.STORE_DIR / ".pending_capture").exists()


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
        # Patching HOME alone isn't enough: cli.install() -> claude.install(home) also
        # runs clean_legacy_repo_settings against store.git_root(os.getcwd()) - the
        # PROCESS cwd's git root, unaffected by HOME — to strip a pre-CLI installer's
        # repo-level hooks. Left unpatched, running this test from a checkout whose
        # <repo>/.claude/settings.json carries legacy Contexer markers would rewrite
        # that real file. chdir(tmp_path) contains it structurally (tmp_path has no
        # .claude/settings.json to touch); the byte-identical check is belt-and-suspenders
        # since no session fixture watches <repo>/.claude/settings.json for this class of leak.
        real_repo = store.git_root(str(Path.cwd()))
        real_settings = Path(real_repo) / ".claude" / "settings.json" if real_repo else None
        before = real_settings.read_bytes() if real_settings and real_settings.is_file() else None

        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        from contexer.cli import install
        install()

        if real_settings is not None:
            after = real_settings.read_bytes() if real_settings.is_file() else None
            assert after == before, (
                f"install() must never touch the real {real_settings} — cwd isolation leaked")

        post_write_cmd = self._post_toolusecmd(tmp_path)
        sibling_prefix = self._sibling_prefix(tmp_path)
        assert post_write_cmd.startswith(sibling_prefix), (
            f"post_write's $REPO resolution {post_write_cmd!r} must match the sibling "
            f"UserPromptSubmit hooks' prefix {sibling_prefix!r}"
        )
        assert sibling_prefix == 'REPO="$PWD" &&'
        assert "git" not in sibling_prefix


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


class TestHookConvergenceSafety:
    """`install` converges every python-carrying hook on its exact current command
    (`base._strip_stale` / `_strip_stale_flat`), unconditionally and on every run. Two
    properties make that safe, and nothing in the suite pinned either one:

      1. It may only ever strip a hook CONTEXER installed. Stripping drops the whole
         GROUP, and some identity markers are generic ("compaction starting",
         "sync_memory"), so a bare marker match ate a user's own hook plus every
         unrelated sibling command beside it - silently, permanently, on a command the
         user ran deliberately and was told succeeded.
      2. It must tolerate a hand-edited config. `"command": null` is a real shape (the
         default in `.get("command", "")` applies only when the key is ABSENT), and an
         unhandled TypeError out of `install` aborts the remaining `--target all`
         adapters mid-run.

    Every case here runs against an isolated temp home with cwd redirected, the fixture
    pattern `test_post_write_prefix_matches_sibling_user_prompt_submit_hooks` documents:
    `claude.install` also cleans <cwd git root>/.claude/settings.json, which HOME alone
    does not contain."""

    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.chdir(tmp_path)

    def _settings(self, home, name=".claude/settings.json"):
        return _json.loads((home / name).read_text())

    def _write(self, home, cfg, name=".claude/settings.json"):
        (home / name).write_text(_json.dumps(cfg, indent=2))

    def _cmds(self, groups):
        return [h.get("command") for grp in groups for h in grp.get("hooks", [])]

    # --- 1. foreign hooks survive -------------------------------------------------

    def test_claude_fresh_install_preserves_foreign_compaction_marker(
            self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        (tmp_path / ".claude").mkdir()
        foreign = {"hooks": [{"type": "command",
                               "command": 'echo "compaction starting, backing up notes"'}]}
        self._write(tmp_path, {"hooks": {"PreCompact": [foreign]}})

        claude.install(tmp_path)

        assert foreign in self._settings(tmp_path)["hooks"]["PreCompact"]

    def test_claude_fresh_install_preserves_foreign_uv_postcompact(
            self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        (tmp_path / ".claude").mkdir()
        foreign = {"hooks": [{"type": "command",
                               "command": "uv run --directory /tmp/my-tool python backup.py"}]}
        self._write(tmp_path, {"hooks": {"PostCompact": [foreign]}})

        claude.install(tmp_path)

        assert foreign in self._settings(tmp_path)["hooks"]["PostCompact"]

    def test_claude_fresh_install_does_not_claim_a_foreign_update_context_filename(
            self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        (tmp_path / ".claude").mkdir()
        foreign = {"hooks": [{"type": "command",
                               "command": ("uv run --directory /tmp/my-tool python "
                                           "update_context.py")}]}
        self._write(tmp_path, {"hooks": {"PostCompact": [foreign]}})

        claude.install(tmp_path)

        assert foreign in self._settings(tmp_path)["hooks"]["PostCompact"]

    def test_claude_fresh_uninstall_preserves_foreign_generic_marker(
            self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        (tmp_path / ".claude").mkdir()
        foreign = {"hooks": [{"type": "command",
                               "command": 'echo "compaction starting, backing up notes"'}]}
        self._write(tmp_path, {"hooks": {"PreCompact": [foreign]}})

        claude.uninstall(tmp_path)

        assert foreign in self._settings(tmp_path)["hooks"]["PreCompact"]

    def test_grouped_convergence_preserves_foreign_sibling_and_matcher(self):
        current = "python -c 'from contexer import store; store.get_session_start_context()'"
        stale = "python -c 'from contexer import store; store.get_session_start_context(old)'"
        foreign = {"type": "command", "command": "echo foreign-owned-command"}
        group = {"matcher": "startup", "hooks": [
            {"type": "command", "command": stale}, foreign]}

        assert base._strip_stale(
            [group], ["get_session_start_context"], current) == [
                {"matcher": "startup", "hooks": [foreign]}]

    def test_grouped_convergence_removes_stale_sibling_beside_current(self):
        current = "python -c 'from contexer import store; store.get_session_start_context()'"
        stale = "python -c 'from contexer import store; store.get_session_start_context(old)'"
        current_hook = {"type": "command", "command": current}
        group = {"matcher": "startup", "hooks": [
            current_hook, {"type": "command", "command": stale}]}

        assert base._strip_stale(
            [group], ["get_session_start_context"], current) == [
                {"matcher": "startup", "hooks": [current_hook]}]

    def test_marker_filter_preserves_foreign_sibling_and_matcher(self):
        foreign = {"type": "command", "command": "echo foreign-owned-command"}
        group = {"matcher": "stop", "hooks": [
            {"type": "command", "command": "touch ~/.contexer/.pending_capture"}, foreign]}

        assert base._filter_groups([group], [".pending_capture"]) == [
            {"matcher": "stop", "hooks": [foreign]}]

    def test_claude_stop_migration_preserves_foreign_same_group_sibling(
            self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        claude.install(tmp_path)
        foreign = {"type": "command", "command": "echo foreign-stop-command"}
        cfg = self._settings(tmp_path)
        cfg["hooks"]["Stop"] = [{"matcher": "stop", "hooks": [
            {"type": "command", "command": "touch ~/.contexer/.pending_capture"}, foreign]}]
        self._write(tmp_path, cfg)

        claude.install(tmp_path)

        assert self._settings(tmp_path)["hooks"]["Stop"] == [
            {"matcher": "stop", "hooks": [foreign]}]

    def test_claude_foreign_marker_bearing_group_survives_install(self, tmp_path, monkeypatch):
        """A user's own PreCompact group whose command merely CONTAINS the generic
        "compaction starting" marker - along with the unrelated backup command beside
        it, which carries no marker at all."""
        self._isolate(tmp_path, monkeypatch)
        claude.install(tmp_path)

        foreign = {"hooks": [
            {"type": "command", "command": 'echo "compaction starting, backing up notes"'},
            {"type": "command", "command": "cp ~/notes.md ~/notes.bak"},
        ]}
        cfg = self._settings(tmp_path)
        cfg["hooks"]["PreCompact"].append(_json.loads(_json.dumps(foreign)))
        self._write(tmp_path, cfg)

        claude.install(tmp_path)
        assert foreign in self._settings(tmp_path)["hooks"]["PreCompact"]

    def test_claude_foreign_sync_memory_hook_survives_install(self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        claude.install(tmp_path)

        foreign = {"hooks": [{"type": "command", "command": "python3 ~/bin/sync_memory.py"}]}
        cfg = self._settings(tmp_path)
        cfg["hooks"]["SessionEnd"].append(_json.loads(_json.dumps(foreign)))
        self._write(tmp_path, cfg)

        claude.install(tmp_path)
        assert foreign in self._settings(tmp_path)["hooks"]["SessionEnd"]

    def test_codex_foreign_marker_bearing_group_survives_install(self, tmp_path, monkeypatch):
        from contexer.adapters import codex
        self._isolate(tmp_path, monkeypatch)
        codex.install(tmp_path)

        foreign = {"hooks": [{"type": "command",
                              "command": "python3 ~/bin/log.py --tag claude.rationale"}]}
        cfg = self._settings(tmp_path, ".codex/hooks.json")
        cfg["hooks"]["UserPromptSubmit"].append(_json.loads(_json.dumps(foreign)))
        self._write(tmp_path, cfg, ".codex/hooks.json")

        codex.install(tmp_path)
        assert foreign in self._settings(tmp_path, ".codex/hooks.json")["hooks"]["UserPromptSubmit"]

    def test_cursor_foreign_marker_bearing_hook_survives_install(self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        cursor.install(tmp_path)

        foreign = {"type": "command", "command": 'echo "cursor.session_start fired"'}
        cfg = self._settings(tmp_path, ".cursor/hooks.json")
        cfg["hooks"]["sessionStart"].append(dict(foreign))
        self._write(tmp_path, cfg, ".cursor/hooks.json")

        cursor.install(tmp_path)
        assert foreign in self._settings(tmp_path, ".cursor/hooks.json")["hooks"]["sessionStart"]

    # --- 2. our own stale hooks still converge -------------------------------------

    def test_claude_stale_own_hooks_still_converge(self, tmp_path, monkeypatch):
        """The property the convergence exists for: a config downgraded to the pre-`-P`
        command self-heals on a plain reinstall, with no duplicate stacked beside it."""
        self._isolate(tmp_path, monkeypatch)
        claude.install(tmp_path)

        cfg = self._settings(tmp_path)
        for groups in cfg["hooks"].values():
            for grp in groups:
                for h in grp.get("hooks", []):
                    if isinstance(h.get("command"), str):
                        h["command"] = h["command"].replace('" -P -c "', '" -c "')
        self._write(tmp_path, cfg)

        claude.install(tmp_path)
        cmds = [c for groups in self._settings(tmp_path)["hooks"].values()
                for c in self._cmds(groups) if isinstance(c, str)]
        assert not [c for c in cmds if '" -c "' in c], "a pre--P command survived reinstall"
        assert len(cmds) == len(set(cmds)), "reinstall stacked a duplicate hook"

    def test_cursor_stale_own_hook_still_converges(self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        cursor.install(tmp_path)

        cfg = self._settings(tmp_path, ".cursor/hooks.json")
        for h in cfg["hooks"]["sessionStart"]:
            h["command"] = h["command"].replace('" -P -c "', '" -c "')
        self._write(tmp_path, cfg, ".cursor/hooks.json")

        cursor.install(tmp_path)
        ss = self._settings(tmp_path, ".cursor/hooks.json")["hooks"]["sessionStart"]
        cmds = [h["command"] for h in ss]
        assert not [c for c in cmds if '" -c "' in c]
        assert len(cmds) == len(set(cmds))

    # --- 3. hand-edited shapes abort nothing ---------------------------------------

    def test_claude_install_tolerates_a_null_command_hook(self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        claude.install(tmp_path)

        odd = {"hooks": [{"type": "command", "command": None}]}
        cfg = self._settings(tmp_path)
        cfg["hooks"]["SessionStart"].append(_json.loads(_json.dumps(odd)))
        self._write(tmp_path, cfg)

        claude.install(tmp_path)          # must not raise
        assert odd in self._settings(tmp_path)["hooks"]["SessionStart"]

    def test_cursor_install_tolerates_null_command_and_non_dict_hooks(self, tmp_path, monkeypatch):
        self._isolate(tmp_path, monkeypatch)
        cursor.install(tmp_path)

        cfg = self._settings(tmp_path, ".cursor/hooks.json")
        cfg["hooks"]["sessionStart"].extend([{"type": "command", "command": None}, "junk"])
        self._write(tmp_path, cfg, ".cursor/hooks.json")

        cursor.install(tmp_path)          # must not raise
        ss = self._settings(tmp_path, ".cursor/hooks.json")["hooks"]["sessionStart"]
        assert {"type": "command", "command": None} in ss and "junk" in ss
