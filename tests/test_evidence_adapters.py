"""Host adapters emitting evidence events in SHADOW MODE (plan Workstream A4 / PR 3).

Shadow mode means one thing above all: nothing a host already did changes. Every test here
therefore asserts the hook's pre-existing visible output alongside the new event, and the
failure cases (the store raising, the spool unwritable, garbage stdin) assert that output
and nothing else.

The other property under test is that the four hosts emit ONE schema rather than four:
`file_changed` from Claude/Codex and from Gemini must differ only in `source` and the host's
own session id, and Cursor — whose hooks cannot observe an edit — must emit no `file_changed`
at all, so an absent event there means "Cursor could not see it" rather than "nothing happened".
"""
import json as _json
from pathlib import Path

import pytest

from contexer import evidence, spool, store
from contexer.adapters import claude, codex, cursor, gemini

# The keys that legitimately differ between two hosts observing the same edit.
_PER_HOST = {"event_id", "occurred_at", "session_id", "source"}


def _boom(*_a, **_k):
    raise RuntimeError("boom")


def _write_payload(repo: str, session_id: str, rel: str = "src/a.py") -> str:
    return _json.dumps({"session_id": session_id,
                        "tool_input": {"file_path": str(Path(repo) / rel)}})


class TestFileChangedIsOneSchemaAcrossHosts:
    """The plan's "same normalized file-change schema" adapter test: two hosts, one edit,
    one shape. A divergence here is what makes a later policy pass host-specific."""

    def test_claude_and_gemini_events_differ_only_by_host(self, tmp_repo):
        claude.post_write(tmp_repo, _write_payload(tmp_repo, "s-claude"))
        gemini.after_write(tmp_repo, _write_payload(tmp_repo, "s-gemini"))

        (c,) = spool.list_pending_evidence(tmp_repo, "s-claude")
        (g,) = spool.list_pending_evidence(tmp_repo, "s-gemini")
        assert c.keys() == g.keys()
        assert {k: v for k, v in c.items() if k not in _PER_HOST} == \
               {k: v for k, v in g.items() if k not in _PER_HOST}
        assert c["kind"] == g["kind"] == "file_changed"
        assert c["files"] == g["files"] == ["src/a.py"]
        assert (c["source"], g["source"]) == ("post_tool_use", "gemini_after_tool")

    def test_the_event_names_the_path_the_sidecar_recorded(self, tmp_repo):
        # Identity agreement: the event carries record_edited_file's OWN return, so an
        # absolute host path and the repo-relative sidecar entry can never disagree.
        claude.post_write(tmp_repo, _write_payload(tmp_repo, "s1"))
        (event,) = spool.list_pending_evidence(tmp_repo, "s1")
        assert event["files"] == store._read_edited_files(tmp_repo) == ["src/a.py"]
        assert event["repo_key"] == tmp_repo

    def test_a_path_outside_the_repo_records_nothing_either_way(self, tmp_repo):
        # record_edited_file drops it (it could never pair against a staged path), so there
        # is no recorded path to key an event on — and validation would reject "../" anyway.
        raw = _json.dumps({"session_id": "s1", "tool_input": {"file_path": "../outside.py"}})
        assert claude.post_write(tmp_repo, raw) == "{}"
        assert store._read_edited_files(tmp_repo) == []
        assert spool.evidence_diagnostics(tmp_repo)["pending"] == 0

    def test_missing_session_id_still_emits_under_unknown(self, tmp_repo):
        raw = _json.dumps({"tool_input": {"file_path": "a.py"}})
        assert claude.post_write(tmp_repo, raw) == "{}"
        (event,) = spool.list_pending_evidence(tmp_repo, "unknown")
        assert event["files"] == ["a.py"]


class TestCodexSharesClaudesEntrypoint:
    """Codex reuses claude.post_write VERBATIM (its installed hook command names it), so the
    emission lives there once. A second copy in codex.py would be a second schema to drift."""

    def test_codex_defines_no_second_write_entrypoint(self):
        assert not hasattr(codex, "post_write")
        assert not hasattr(codex, "capture_constraint")
        source = Path(codex.__file__).read_text(encoding="utf-8")
        assert "claude.post_write" in source
        assert "emit_hook_event" not in source    # no second, drifting emission

    def test_the_shared_entrypoint_emits(self, tmp_repo):
        claude.post_write(tmp_repo, _write_payload(tmp_repo, "codex-session"))
        (event,) = spool.list_pending_evidence(tmp_repo, "codex-session")
        # Host-neutral source (controller ruling R9): the entrypoint cannot tell which host
        # is calling it, so it must not claim to.
        assert event["source"] == "post_tool_use"


class TestUserDirectiveEmission:
    def test_directive_prompt_emits_one_event(self, tmp_repo):
        raw = _json.dumps({"prompt": "always use conventional commits", "session_id": "s1"})
        out = _json.loads(claude.capture_constraint(tmp_repo, raw))
        assert "additionalContext" in out["hookSpecificOutput"]        # unchanged ack
        (event,) = spool.list_pending_evidence(tmp_repo, "s1")
        assert event["kind"] == "user_directive"
        assert event["source"] == "claude_prompt"
        assert "conventional commits" in event["summary"]
        assert event["files"] == [] and event["attributes"] == {}

    def test_plain_prompt_emits_nothing(self, tmp_repo):
        raw = _json.dumps({"prompt": "please add a button", "session_id": "s1"})
        assert claude.capture_constraint(tmp_repo, raw) == "{}"
        assert spool.evidence_diagnostics(tmp_repo)["pending"] == 0

    def test_gemini_prompt_path_emits_with_its_own_source(self, tmp_repo):
        raw = _json.dumps({"prompt": "always use conventional commits", "session_id": "s1"})
        out = _json.loads(gemini.before_agent(tmp_repo, raw))
        assert "constraint" in out["hookSpecificOutput"]["additionalContext"].lower()
        kinds = [(e["kind"], e["source"])
                 for e in spool.list_pending_evidence(tmp_repo, "s1")]
        assert kinds == [("user_directive", "gemini_prompt")]

    def test_cursor_emits_user_directive_and_nothing_else(self, tmp_repo):
        raw = _json.dumps({"prompt": "always use conventional commits", "session_id": "s1",
                           "workspace_roots": [tmp_repo]})
        assert _json.loads(cursor.capture_constraint("", raw)) == {"continue": True}
        events = spool.list_pending_evidence(tmp_repo, "s1")
        assert [(e["kind"], e["source"]) for e in events] == [("user_directive", "cursor_prompt")]

    def test_cursor_never_emits_a_file_change(self):
        # Cursor's hooks cannot observe an edit (#175 leaves recording out of scope), so the
        # kind must be absent from the module rather than emitted with a guessed path.
        # Checked as a LITERAL, not as a word: the prose above says "file_changed" too, and
        # the invariant is that cursor.py emits only through capture_directive.
        source = Path(cursor.__file__).read_text(encoding="utf-8")
        assert '"file_changed"' not in source and "'file_changed'" not in source
        assert "emit_hook_event" not in source


class TestStoreFailureIsRecordedNotSwallowed:
    """The loss case the spool exists for: capture raised, so no entry proves the directive
    — the event is still written, flagged `unverified`, and the hook behaves exactly as before."""

    def test_claude_hook_output_unchanged_and_event_flagged(self, tmp_repo, monkeypatch):
        monkeypatch.setattr(store, "capture_user_constraint", _boom)
        raw = _json.dumps({"prompt": "always use conventional commits", "session_id": "s1"})
        assert claude.capture_constraint(tmp_repo, raw) == "{}"   # pre-existing: swallowed
        (event,) = spool.list_pending_evidence(tmp_repo, "s1")
        assert event["kind"] == "user_directive"
        assert event["attributes"] == {"unverified": True}
        assert store.load(tmp_repo)["entries"] == []               # nothing was stored

    def test_cursor_hook_output_unchanged_and_event_flagged(self, tmp_repo, monkeypatch):
        monkeypatch.setattr(store, "capture_user_constraint", _boom)
        raw = _json.dumps({"prompt": "always use conventional commits", "session_id": "s1",
                           "workspace_roots": [tmp_repo]})
        assert _json.loads(cursor.capture_constraint("", raw)) == {"continue": True}
        (event,) = spool.list_pending_evidence(tmp_repo, "s1")
        assert event["attributes"] == {"unverified": True}

    def test_a_non_directive_prompt_records_nothing_when_the_store_fails(self, tmp_repo,
                                                                        monkeypatch):
        # Gated on the store's OWN detector: a failure while handling an ordinary prompt is
        # not evidence of a directive, and guessing would poison the spool on every crash.
        monkeypatch.setattr(store, "capture_user_constraint", _boom)
        raw = _json.dumps({"prompt": "please add a button", "session_id": "s1"})
        assert claude.capture_constraint(tmp_repo, raw) == "{}"
        assert spool.evidence_diagnostics(tmp_repo)["pending"] == 0

    def test_a_failing_detector_does_not_mask_the_original_error(self, tmp_repo, monkeypatch):
        # capture_directive re-raises what the store raised; a second failure while RECORDING
        # the loss must not replace it, or the caller's error path sees the wrong exception.
        monkeypatch.setattr(store, "capture_user_constraint", _boom)
        monkeypatch.setattr(store, "is_prescriptive_directive",
                            lambda *_a, **_k: 1 / 0)
        with pytest.raises(RuntimeError, match="boom"):
            evidence.capture_directive(tmp_repo, "always squash", "s1", "claude_prompt")


class TestSpoolFailureNeverReachesTheHost:
    @pytest.fixture(params=["raises", "dropped_error"])
    def broken_spool(self, request, monkeypatch):
        if request.param == "raises":
            monkeypatch.setattr(spool, "append_evidence", _boom)
        else:
            monkeypatch.setattr(spool, "append_evidence",
                                lambda *_a, **_k: {"status": "dropped_error", "errors": ["x"]})
        return request.param

    def test_post_write_keeps_both_existing_signals(self, tmp_repo, broken_spool):
        raw = _json.dumps({"session_id": "s1", "tool_input": {"file_path": "a.py"}})
        assert claude.post_write(tmp_repo, raw) == "{}"
        assert store._read_edited_files(tmp_repo) == ["a.py"]
        assert (store.STORE_DIR / ".pending_capture").exists()

    def test_gemini_after_write_keeps_its_reminder(self, tmp_repo, broken_spool):
        raw = _json.dumps({"session_id": "s1", "tool_input": {"file_path": "a.py"}})
        out = _json.loads(gemini.after_write(tmp_repo, raw))
        assert "update_context" in out["hookSpecificOutput"]["additionalContext"]
        assert store._read_edited_files(tmp_repo) == ["a.py"]

    def test_capture_constraint_still_acks_and_stores(self, tmp_repo, broken_spool):
        raw = _json.dumps({"prompt": "always use conventional commits", "session_id": "s1"})
        out = _json.loads(claude.capture_constraint(tmp_repo, raw))
        assert "additionalContext" in out["hookSpecificOutput"]
        assert [e["type"] for e in store.load(tmp_repo)["entries"]] == ["decision"]

    def test_cursor_still_passes_the_prompt_through(self, tmp_repo, broken_spool):
        raw = _json.dumps({"prompt": "always use conventional commits", "session_id": "s1",
                           "workspace_roots": [tmp_repo]})
        assert _json.loads(cursor.capture_constraint("", raw)) == {"continue": True}


class TestEmitHookEvent:
    def test_fills_what_a_hook_cannot_know(self, tmp_repo):
        result = evidence.emit_hook_event(tmp_repo, "file_changed", source="post_tool_use",
                                          files=["a.py"])
        assert result["status"] == "stored"
        (event,) = spool.list_pending_evidence(tmp_repo, "unknown")
        assert event["schema_version"] == evidence.SCHEMA_VERSION
        assert event["repo_key"] == tmp_repo
        assert event["occurred_at"].endswith("+00:00")

    def test_never_raises_on_a_caller_bug(self, tmp_repo):
        # `files` not iterable: the dict build itself fails, and a hook must still survive it.
        assert evidence.emit_hook_event(tmp_repo, "file_changed", source="s",
                                        files=42)["status"] == "dropped_error"

    def test_an_invalid_kind_is_rejected_not_stored(self, tmp_repo):
        result = evidence.emit_hook_event(tmp_repo, "not_a_kind", source="post_tool_use")
        assert result["status"] == "rejected_invalid"
        assert spool.evidence_diagnostics(tmp_repo)["pending"] == 0
