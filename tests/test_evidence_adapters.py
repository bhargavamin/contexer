"""Host adapters emitting evidence events in SHADOW MODE (plan Workstream A4 / PR 3).

Shadow mode means one thing above all: nothing a host already did changes. Every test here
therefore asserts the hook's pre-existing visible output alongside the new event, and the
failure cases (the store raising, the spool unwritable, garbage stdin) assert that output
and nothing else.

The other property under test is that the four hosts emit ONE schema rather than four:
`file_changed` from Claude/Codex and from Gemini must differ only in `source` and the host's
own session id, and Cursor - whose hooks cannot observe an edit - must emit no `file_changed`
at all, so an absent event there means "Cursor could not see it" rather than "nothing happened".
"""
import json as _json
import threading
from pathlib import Path

import pytest

from contexer import evidence, share_policy, spool, store
from tests.conftest import redirect_store_dir
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
        # is no recorded path to key an event on - and validation would reject "../" anyway.
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

    def test_verified_host_prompt_enqueues_after_capture_returns(self, tmp_repo, monkeypatch):
        policy = share_policy.build_policy(
            repo_path=tmp_repo,
            repo_key="github.com/org/repo",
            endpoint="https://mcp.contexer.ai/mcp",
            account_fingerprint="acctfp_v1_7M4Q2PX9C6N8",
            team_id="40000000-0000-4000-8000-000000000001",
            team_name="Platform",
            entries=[],
            include_existing=True,
        )
        share_policy.save_policy(tmp_repo, policy)
        monkeypatch.setattr(store, "run_git", lambda *_args: "https://github.com/org/repo.git")
        raw = _json.dumps({"prompt": "always use conventional commits", "session_id": "s1"})

        out = _json.loads(claude.capture_constraint(tmp_repo, raw))

        assert "additionalContext" in out["hookSpecificOutput"]
        (intent,) = share_policy.read_outbox()
        assert intent["decision_id"] == store.load(tmp_repo)["entries"][0]["id"]
        assert share_policy.read_receipts()[0]["state"] == "queued"

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
    - the event is still written, flagged `unverified`, and the hook behaves exactly as before.

    The seam these patch is `capture_user_constraint_with_meta`, which is what
    `evidence.capture_directive` calls: the 3-tuple `capture_user_constraint` is now a thin
    wrapper over it (the house `_with_meta` pattern), so patching the wrapper would leave the
    real call path untouched and test nothing.
    """

    def test_claude_hook_output_unchanged_and_event_flagged(self, tmp_repo, monkeypatch):
        monkeypatch.setattr(store, "capture_user_constraint_with_meta", _boom)
        raw = _json.dumps({"prompt": "always use conventional commits", "session_id": "s1"})
        assert claude.capture_constraint(tmp_repo, raw) == "{}"   # pre-existing: swallowed
        (event,) = spool.list_pending_evidence(tmp_repo, "s1")
        assert event["kind"] == "user_directive"
        assert event["attributes"] == {"unverified": True}
        assert store.load(tmp_repo)["entries"] == []               # nothing was stored

    def test_cursor_hook_output_unchanged_and_event_flagged(self, tmp_repo, monkeypatch):
        monkeypatch.setattr(store, "capture_user_constraint_with_meta", _boom)
        raw = _json.dumps({"prompt": "always use conventional commits", "session_id": "s1",
                           "workspace_roots": [tmp_repo]})
        assert _json.loads(cursor.capture_constraint("", raw)) == {"continue": True}
        (event,) = spool.list_pending_evidence(tmp_repo, "s1")
        assert event["attributes"] == {"unverified": True}

    def test_a_non_directive_prompt_records_nothing_when_the_store_fails(self, tmp_repo,
                                                                        monkeypatch):
        # Gated on the store's OWN detector: a failure while handling an ordinary prompt is
        # not evidence of a directive, and guessing would poison the spool on every crash.
        monkeypatch.setattr(store, "capture_user_constraint_with_meta", _boom)
        raw = _json.dumps({"prompt": "please add a button", "session_id": "s1"})
        assert claude.capture_constraint(tmp_repo, raw) == "{}"
        assert spool.evidence_diagnostics(tmp_repo)["pending"] == 0

    def test_a_failing_detector_does_not_mask_the_original_error(self, tmp_repo, monkeypatch):
        # capture_directive re-raises what the store raised; a second failure while RECORDING
        # the loss must not replace it, or the caller's error path sees the wrong exception.
        monkeypatch.setattr(store, "capture_user_constraint_with_meta", _boom)
        monkeypatch.setattr(store, "is_prescriptive_directive",
                            lambda *_a, **_k: 1 / 0)
        with pytest.raises(RuntimeError, match="boom"):
            evidence.capture_directive(tmp_repo, "always squash", "s1", "claude_prompt")


class TestPromptHooksNeverWaitForTheDecisionStore:
    @pytest.mark.parametrize("host", ["claude", "codex", "cursor", "gemini"])
    def test_busy_store_spools_unverified_directive_without_ack_or_prompt_stall(
            self, tmp_repo, host):
        acquired = threading.Event()
        release = threading.Event()

        def hold_store():
            with store.store_lock(store.repo_slug(tmp_repo)):
                acquired.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=hold_store)
        holder.start()
        assert acquired.wait(timeout=1)

        raw = _json.dumps({
            "prompt": "always use conventional commits",
            "session_id": f"busy-{host}",
            "workspace_roots": [tmp_repo],
        })
        result: dict[str, str] = {}
        failed: list[BaseException] = []

        def invoke():
            try:
                if host in {"claude", "codex"}:  # Codex installs this exact shared entrypoint.
                    result["output"] = claude.capture_constraint(tmp_repo, raw)
                elif host == "cursor":
                    result["output"] = cursor.capture_constraint("", raw)
                else:
                    result["output"] = gemini.before_agent(tmp_repo, raw)
            except BaseException as exc:  # the assertion reports an escaping hook failure
                failed.append(exc)

        prompt = threading.Thread(target=invoke)
        prompt.start()
        prompt.join(timeout=0.5)
        completed_while_locked = not prompt.is_alive()
        release.set()
        holder.join(timeout=1)
        prompt.join(timeout=1)

        assert completed_while_locked, "prompt hook waited for the decision-store lock"
        assert failed == []
        assert store.load(tmp_repo)["entries"] == []
        (event,) = spool.list_pending_evidence(tmp_repo, f"busy-{host}")
        assert event["kind"] == "user_directive"
        assert event["attributes"] == {"unverified": True, "store_busy": True}
        assert "Auto-stored as constraint" not in result["output"]


class TestPromptHooksNeverRunGit:
    @pytest.mark.parametrize("host", ["claude", "codex", "cursor", "gemini"])
    def test_cache_cold_linked_worktree_uses_one_shared_identity_without_subprocess(
            self, tmp_path, monkeypatch, host):
        redirect_store_dir(monkeypatch, tmp_path / ".contexer")
        main = tmp_path / "main"
        gitdir = main / ".git" / "worktrees" / "wt"
        gitdir.mkdir(parents=True)
        (gitdir / "commondir").write_text("../..\n", encoding="utf-8")
        worktree = tmp_path / "wt"
        nested = worktree / "src" / "deep"
        nested.mkdir(parents=True)
        (worktree / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")

        calls = []

        def forbidden(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("prompt hook spawned a subprocess")

        monkeypatch.setattr(store.subprocess, "run", forbidden)
        raw = _json.dumps({
            "prompt": "always use conventional commits",
            "session_id": f"nogit-{host}",
            "cwd": str(nested),
            "workspace_roots": [str(nested)],
        })
        if host in {"claude", "codex"}:  # Codex installs this exact shared entrypoint.
            claude.capture_constraint(str(nested), raw)
        elif host == "cursor":
            cursor.capture_constraint("", raw)
        else:
            gemini.before_agent(str(nested), raw)

        assert calls == []
        assert store.current_repo_path() == str(worktree)
        assert store.repo_slug(str(worktree)) == store.repo_slug(str(main))
        assert len(store.load(str(main))["entries"]) == 1
        (event,) = spool.list_pending_evidence(str(main), f"nogit-{host}")
        assert event["repo_key"] == str(worktree)

    @pytest.mark.parametrize("host", ["claude", "gemini"])
    @pytest.mark.parametrize("prompt", [
        "Why did we choose SQLite for durable storage?",
        "What is the purpose?",
    ])
    def test_anchored_retrieval_and_overview_skip_staleness_git(
            self, tmp_repo, monkeypatch, host, prompt):
        ok, _ = store.update_decision(
            tmp_repo, "Use SQLite for durable storage because local writes must be atomic.",
            "setup", created_by="user")
        assert ok
        data = store.load(tmp_repo)
        data["entries"][0]["source_files"] = ["contexer/store.py"]
        data["entries"][0]["anchor_commit"] = "a" * 40
        store.save(tmp_repo, data)

        calls = []

        def forbidden(*args, **kwargs):
            calls.append((args, kwargs))
            raise AssertionError("prompt retrieval ran Git")

        monkeypatch.setattr(store, "run_git", forbidden)
        raw = _json.dumps({"prompt": prompt, "session_id": f"retrieve-{host}",
                           "cwd": tmp_repo, "workspace_roots": [tmp_repo]})
        output = (claude.rationale(tmp_repo, raw) if host == "claude"
                  else gemini.before_agent(tmp_repo, raw))

        assert "SQLite" in output
        assert calls == []


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


class TestCaptureCoverage:
    """Each adapter's static capability map, pinned exactly.

    These maps are the ONLY place a host says what it can observe, and every surface that
    reports coverage renders them, so a quiet edit here would change what Contexer tells a
    developer about its own blind spots. Written out per host rather than derived, because
    deriving them from the code they describe would make the test agree with any drift.
    """

    EXPECTED = {
        "claude": {"user_directives": "captured", "file_changes": "captured",
                   "assistant_conclusions": "model_reported",
                   "test_results": "unavailable", "diffs": "unavailable"},
        "codex": {"user_directives": "captured", "file_changes": "captured",
                  "assistant_conclusions": "model_reported",
                  "test_results": "unavailable", "diffs": "unavailable"},
        "gemini": {"user_directives": "captured", "file_changes": "captured",
                   "assistant_conclusions": "model_reported",
                   "test_results": "unavailable", "diffs": "unavailable"},
        "cursor": {"user_directives": "captured", "file_changes": "unavailable",
                   "assistant_conclusions": "model_reported",
                   "test_results": "unavailable", "diffs": "unavailable"},
    }

    @pytest.mark.parametrize("module", [claude, codex, cursor, gemini])
    def test_the_map_is_exactly_what_this_host_can_observe(self, module):
        assert module.EVIDENCE_COVERAGE == self.EXPECTED[module.NAME]

    def test_codex_matches_claude_because_it_runs_claudes_entrypoints(self):
        # Restated rather than aliased (module-boundary rule 3), so this parity check is what
        # actually holds the two together - the shape policy/guard_engine trust already has.
        assert codex.EVIDENCE_COVERAGE == claude.EVIDENCE_COVERAGE

    def test_cursor_claims_neither_file_nor_assistant_capture(self):
        # Cursor's hooks see one payload, the prompt. An absent file event there means "could
        # not observe", which is why the map says unavailable instead of reporting zero.
        assert cursor.EVIDENCE_COVERAGE["file_changes"] == "unavailable"
        assert cursor.EVIDENCE_COVERAGE["assistant_conclusions"] != "captured"
        assert not hasattr(cursor, "post_write") and not hasattr(cursor, "after_write")

    @pytest.mark.parametrize("module", [claude, codex, cursor, gemini])
    def test_no_host_claims_a_reserved_kind_or_an_unknown_state(self, module):
        # `test_result` and `diff_observed` are schema-valid with no emitter anywhere, so no
        # host may advertise them until one exists.
        assert module.EVIDENCE_COVERAGE["test_results"] == "unavailable"
        assert module.EVIDENCE_COVERAGE["diffs"] == "unavailable"
        assert set(module.EVIDENCE_COVERAGE) == set(evidence.COVERAGE_FIELDS)
        assert set(module.EVIDENCE_COVERAGE.values()) <= evidence.CAPTURE_STATES

    @pytest.mark.parametrize("module", [claude, codex, cursor, gemini])
    def test_no_host_claims_to_capture_the_assistants_own_words(self, module):
        # No host hands a hook the model's response, and this is the surface that would lie
        # about it first. `record_agent_conclusion` is agent-reported, always.
        assert module.EVIDENCE_COVERAGE["assistant_conclusions"] == "model_reported"
