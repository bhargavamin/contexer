"""End-to-end tests — exercises the full hook sequence from install through context recall.

Covers: install/uninstall, reinstall idempotency, session start states, bootstrap offer,
constraint capture, decision storage, context retrieval, rationale injection, task capture,
and bootstrap scan. Uses tmp_path + monkeypatch to isolate all filesystem side effects.
"""
import json
import subprocess
import sys
from pathlib import Path

import pytest

from contexer import store
from contexer import cli


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_home(tmp_path, monkeypatch):
    """Redirects Path.home() to a temp dir so install() never touches real settings."""
    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".claude").mkdir()
    return tmp_path


@pytest.fixture
def tmp_repo(tmp_path, monkeypatch):
    """Redirects STORE_DIR to a temp path and returns a fake repo path."""
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    return str(tmp_path / "myrepo")


SESSION = "e2e-session"


def _settings(tmp_home: Path) -> dict:
    p = tmp_home / ".claude" / "settings.json"
    return json.loads(p.read_text()) if p.exists() else {}


def _in_groups(groups: list, marker: str) -> bool:
    return any(marker in str(h) for grp in groups for h in grp.get("hooks", []))


def _has_mcp_tool(groups: list, tool: str) -> bool:
    return any(
        any(h.get("type") == "mcp_tool" and h.get("tool") == tool
            for h in grp.get("hooks", []))
        for grp in groups
    )


# ── 1. Install — hook and permission registration ─────────────────────────────

class TestInstall:
    def test_mcp_server_registered(self, tmp_home):
        cli.install()
        claude = json.loads((tmp_home / ".claude.json").read_text())
        assert "contexer" in claude.get("mcpServers", {})

    def test_session_start_hook_registered(self, tmp_home):
        cli.install()
        hooks = _settings(tmp_home).get("hooks", {})
        ss = hooks.get("SessionStart", [])
        assert _in_groups(ss, "get_session_start_context")

    def test_post_tool_use_hook_registered(self, tmp_home):
        cli.install()
        hooks = _settings(tmp_home).get("hooks", {})
        put = hooks.get("PostToolUse", [])
        assert _in_groups(put, ".pending_capture")

    def test_anchor_hook_has_pending_capture_check(self, tmp_home):
        cli.install()
        hooks = _settings(tmp_home).get("hooks", {})
        ups = hooks.get("UserPromptSubmit", [])
        anchor_cmds = [h.get("command", "") for g in ups for h in g.get("hooks", [])
                       if ".current_repo" in str(h)]
        assert any(".pending_capture" in c for c in anchor_cmds)

    def test_capture_user_constraint_hook_registered(self, tmp_home):
        cli.install()
        ups = _settings(tmp_home).get("hooks", {}).get("UserPromptSubmit", [])
        assert _has_mcp_tool(ups, "capture_user_constraint")

    def test_capture_user_constraint_hook_receives_prompt(self, tmp_home):
        cli.install()
        ups = _settings(tmp_home).get("hooks", {}).get("UserPromptSubmit", [])
        hooks = [h for g in ups for h in g.get("hooks", [])
                 if h.get("tool") == "capture_user_constraint"]
        assert any(h.get("input", {}).get("prompt") == "${prompt}" for h in hooks)

    def test_capture_context_hook_registered_once(self, tmp_home):
        cli.install()
        ups = _settings(tmp_home).get("hooks", {}).get("UserPromptSubmit", [])
        hooks = [h for g in ups for h in g.get("hooks", [])
                 if h.get("tool") == "capture_context"]
        assert hooks
        assert any(h.get("once") for h in hooks)

    def test_get_context_for_prompt_hook_registered(self, tmp_home):
        cli.install()
        ups = _settings(tmp_home).get("hooks", {}).get("UserPromptSubmit", [])
        assert _has_mcp_tool(ups, "get_context_for_prompt")

    def test_bootstrap_hook_registered(self, tmp_home):
        cli.install()
        hooks = _settings(tmp_home).get("hooks", {})
        ups = hooks.get("UserPromptSubmit", [])
        assert _in_groups(ups, "get_bootstrap_context_prompt")

    def test_pre_compact_hook_registered(self, tmp_home):
        cli.install()
        hooks = _settings(tmp_home).get("hooks", {})
        pc = hooks.get("PreCompact", [])
        assert _in_groups(pc, "compaction starting")

    def test_post_compact_hook_registered(self, tmp_home):
        cli.install()
        hooks = _settings(tmp_home).get("hooks", {})
        poc = hooks.get("PostCompact", [])
        assert _in_groups(poc, "reloaded after compaction")

    @pytest.mark.parametrize("perm", [
        "mcp__contexer__capture_context",
        "mcp__contexer__update_context",
        "mcp__contexer__get_context",
        "mcp__contexer__bootstrap_context",
        "mcp__contexer__get_context_for_prompt",
        "mcp__contexer__update_global_context",
        "mcp__contexer__get_global_context",
        "mcp__contexer__capture_user_constraint",
    ])
    def test_permission_registered(self, tmp_home, perm):
        cli.install()
        allow = _settings(tmp_home).get("permissions", {}).get("allow", [])
        assert perm in allow

    def test_contexer_store_dir_created(self, tmp_home):
        cli.install()
        assert (tmp_home / ".contexer").is_dir()


# ── 2. Reinstall idempotency ──────────────────────────────────────────────────

class TestReinstallIdempotency:
    def _counts(self, tmp_home: Path) -> dict:
        hooks = _settings(tmp_home).get("hooks", {})
        ups = hooks.get("UserPromptSubmit", [])
        put = hooks.get("PostToolUse", [])
        return {
            "anchor": sum(1 for g in ups for h in g.get("hooks", []) if ".current_repo" in str(h)),
            "capture_user_constraint": sum(1 for g in ups for h in g.get("hooks", [])
                                           if h.get("tool") == "capture_user_constraint"),
            "capture_context": sum(1 for g in ups for h in g.get("hooks", [])
                                   if h.get("tool") == "capture_context"),
            "pending_capture": sum(1 for g in put for h in g.get("hooks", [])
                                   if ".pending_capture" in str(h)),
        }

    def test_no_duplication_after_three_installs(self, tmp_home):
        cli.install()
        cli.install()
        cli.install()
        counts = self._counts(tmp_home)
        assert counts["anchor"] == 1
        assert counts["capture_user_constraint"] == 1
        assert counts["capture_context"] == 1
        assert counts["pending_capture"] == 1

    def test_old_anchor_hook_replaced(self, tmp_home):
        """An existing anchor hook without .pending_capture logic is replaced, not duplicated."""
        # Manually write the old-style anchor hook (no .pending_capture)
        settings_path = tmp_home / ".claude" / "settings.json"
        old = {
            "hooks": {
                "UserPromptSubmit": [{
                    "hooks": [{"type": "command", "statusMessage": "Anchoring repo context...",
                               "command": "REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd) "
                                          "&& printf '%s' \"$REPO\" > ~/.contexer/.current_repo && echo '{}'"}]
                }]
            }
        }
        settings_path.write_text(json.dumps(old))
        cli.install()
        counts = self._counts(tmp_home)
        assert counts["anchor"] == 1


# ── 3. Session start states ───────────────────────────────────────────────────

class TestSessionStart:
    def test_new_repo_returns_bootstrap_offer(self, tmp_repo):
        result = store.get_session_start_context(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Yes or no?" in ctx
        assert "Do NOT" in ctx

    def test_new_repo_no_stop_mandate(self, tmp_repo):
        result = store.get_session_start_context(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "STOP" not in ctx

    def test_new_repo_offers_skip_path(self, tmp_repo):
        result = store.get_session_start_context(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert any(w in ctx for w in ["no", "skip"])

    def test_new_repo_system_message_offers_bootstrap(self, tmp_repo):
        result = store.get_session_start_context(tmp_repo)
        assert "bootstrap" in result["systemMessage"]

    def test_with_decisions_injects_project_rules(self, tmp_repo):
        store.update_decision(tmp_repo, "Always write tests before committing", SESSION, "constraint")
        result = store.get_session_start_context(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Project rules" in ctx
        assert "Always write tests" in ctx

    def test_constraints_injected_eagerly(self, tmp_repo):
        store.update_decision(tmp_repo, "Never commit plaintext secrets", SESSION, "constraint")
        store.update_decision(tmp_repo, "Use FastAPI for HTTP", SESSION, "architecture")
        result = store.get_session_start_context(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Never commit plaintext secrets" in ctx

    def test_architecture_deferred_not_in_session_start(self, tmp_repo):
        store.update_decision(tmp_repo, "Use FastAPI for HTTP", SESSION, "architecture")
        store.update_decision(tmp_repo, "Always write tests", SESSION, "constraint")
        result = store.get_session_start_context(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "FastAPI" not in ctx

    def test_no_bootstrap_offer_when_context_exists(self, tmp_repo):
        store.update_decision(tmp_repo, "Use Postgres for storage", SESSION, "architecture")
        result = store.get_session_start_context(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Yes or no?" not in ctx


# ── 4. Bootstrap offer (once hook) ───────────────────────────────────────────

class TestBootstrapOffer:
    def test_returns_dict(self, tmp_repo):
        result = store.get_bootstrap_context_prompt(tmp_repo)
        assert isinstance(result, dict)

    def test_no_context_returns_opt_in_question(self, tmp_repo):
        result = store.get_bootstrap_context_prompt(tmp_repo)
        ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "Yes or no?" in ctx

    def test_no_context_has_do_not_directive(self, tmp_repo):
        result = store.get_bootstrap_context_prompt(tmp_repo)
        ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "Do NOT" in ctx

    def test_with_context_returns_empty(self, tmp_repo):
        store.update_decision(tmp_repo, "Use Django for the web layer", SESSION, "architecture")
        result = store.get_bootstrap_context_prompt(tmp_repo)
        ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert ctx == ""


# ── 5. Constraint capture ─────────────────────────────────────────────────────

class TestConstraintCapture:
    @pytest.mark.parametrize("prompt,expected_subtype", [
        ("always use type hints in Python", "constraint"),
        ("never commit secrets to the repo", "constraint"),
        ("must always include a docstring for public APIs", "constraint"),
        ("allways run tests before merging", "constraint"),       # typo
        ("from now on use snake_case for all variables", "convention"),
        ("going forward, prefer async functions", "convention"),
    ])
    def test_prescriptive_directive_captured_with_correct_subtype(self, tmp_repo, prompt, expected_subtype):
        eid = store.capture_user_constraint(tmp_repo, prompt, SESSION)
        assert eid is not None, f"Expected capture for: {prompt!r}"
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["id"] == eid)
        assert entry["subtype"] == expected_subtype

    @pytest.mark.parametrize("prompt", [
        "how does the build work?",     # question
        "I always use vim",             # personal descriptor
        "we have never needed CI",      # personal descriptor
        "it always worked fine",        # descriptive
        "fix the login bug",            # task, no trigger
    ])
    def test_non_directive_rejected(self, tmp_repo, prompt):
        eid = store.capture_user_constraint(tmp_repo, prompt, SESSION)
        assert eid is None, f"Expected rejection for: {prompt!r}"

    def test_duplicate_directive_silently_discarded(self, tmp_repo):
        store.capture_user_constraint(tmp_repo, "always use type hints in Python", SESSION)
        eid2 = store.capture_user_constraint(tmp_repo, "always use type hints in Python", SESSION)
        assert eid2 is None

    def test_stored_as_decision_type(self, tmp_repo):
        eid = store.capture_user_constraint(tmp_repo, "never push to main directly", SESSION)
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["id"] == eid)
        assert entry["type"] == "decision"

    def test_long_prompt_truncated_to_600_chars(self, tmp_repo):
        long_prompt = "always " + "x" * 700
        eid = store.capture_user_constraint(tmp_repo, long_prompt, SESSION)
        assert eid is not None
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["id"] == eid)
        assert len(entry["content"]) <= 600


# ── 6. Decision storage — novelty filter ─────────────────────────────────────

class TestDecisionStorage:
    def test_exact_duplicate_rejected(self, tmp_repo):
        store.update_decision(tmp_repo, "Use Pydantic v2 for all data models", SESSION, "architecture")
        stored, _ = store.update_decision(tmp_repo, "Use Pydantic v2 for all data models", SESSION, "architecture")
        assert not stored

    def test_near_duplicate_rejected(self, tmp_repo):
        store.update_decision(tmp_repo, "Use Pydantic v2 for all data models", SESSION, "architecture")
        stored, _ = store.update_decision(tmp_repo, "Pydantic v2 is used for data models", SESSION, "architecture")
        assert not stored

    def test_genuinely_new_decision_stored(self, tmp_repo):
        store.update_decision(tmp_repo, "Use Pydantic v2 for all data models", SESSION, "architecture")
        stored, eid = store.update_decision(tmp_repo, "Use SQLAlchemy for database access", SESSION, "architecture")
        assert stored
        assert eid

    def test_subtype_persisted(self, tmp_repo):
        _, eid = store.update_decision(tmp_repo, "Route handlers in src/routes/", SESSION, "pattern")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["id"] == eid)
        assert entry["subtype"] == "pattern"

    def test_entry_count_correct_after_dedup(self, tmp_repo):
        store.update_decision(tmp_repo, "Use Pydantic v2 for all data models", SESSION, "architecture")
        store.update_decision(tmp_repo, "Pydantic v2 is used for data models", SESSION, "architecture")
        store.update_decision(tmp_repo, "Use SQLAlchemy for database access", SESSION, "architecture")
        data = store._load(tmp_repo)
        assert len(data["entries"]) == 2


# ── 7. Context retrieval ──────────────────────────────────────────────────────

class TestContextRetrieval:
    @pytest.fixture(autouse=True)
    def populate(self, tmp_repo):
        store.update_decision(tmp_repo, "Use FastAPI for HTTP", SESSION, "architecture")
        store.update_decision(tmp_repo, "Always run tests before merging", SESSION, "constraint")
        store.update_decision(tmp_repo, "Use conventional commits", SESSION, "convention")
        store.update_decision(tmp_repo, "Route handlers in src/routes/", SESSION, "pattern")
        self.repo = tmp_repo

    def test_get_context_returns_content(self):
        assert store.get_context(self.repo).strip()

    def test_entry_type_filter_constraint(self):
        ctx = store.get_context(self.repo, entry_type="constraint")
        assert "Always run tests" in ctx
        assert "FastAPI" not in ctx

    def test_entry_type_filter_architecture(self):
        ctx = store.get_context(self.repo, entry_type="architecture")
        assert "FastAPI" in ctx
        assert "Always run tests" not in ctx

    def test_query_filter_matches_keyword(self):
        ctx = store.get_context(self.repo, query="commit")
        assert "conventional commits" in ctx

    def test_query_filter_excludes_non_matches(self):
        ctx = store.get_context(self.repo, query="commit")
        assert "FastAPI" not in ctx

    def test_no_matching_query_returns_no_match_message(self):
        ctx = store.get_context(self.repo, query="kubernetes")
        assert "No matching" in ctx or "No context" in ctx


# ── 8. Rationale injection ────────────────────────────────────────────────────

class TestRationaleInjection:
    @pytest.fixture(autouse=True)
    def populate(self, tmp_repo):
        store.update_decision(tmp_repo, "Chose FastAPI over Flask because of async support", SESSION, "architecture")
        store.update_decision(tmp_repo, "SQLAlchemy selected for ORM layer due to team familiarity", SESSION, "architecture")
        self.repo = tmp_repo

    @pytest.mark.parametrize("prompt", [
        "why did we choose FastAPI?",
        "what was the reason for picking SQLAlchemy?",
        "can you explain the rationale behind the async choice?",
        "what was decided about FastAPI?",
    ])
    def test_rationale_prompt_injects_context(self, prompt):
        result = store.get_context_for_prompt(self.repo, prompt)
        assert result.strip(), f"Expected injection for: {prompt!r}"

    @pytest.mark.parametrize("prompt", [
        "add a health check endpoint",
        "fix the login bug",
        "refactor the user model",
        "update the README",
    ])
    def test_task_prompt_is_silent(self, prompt):
        result = store.get_context_for_prompt(self.repo, prompt)
        assert result == "", f"Expected silence for: {prompt!r}"


# ── 9. Task capture ───────────────────────────────────────────────────────────

class TestTaskCapture:
    def test_real_task_stored(self, tmp_repo):
        eid = store.capture_task(tmp_repo, "Add OAuth2 authentication to the API", SESSION)
        assert eid is not None

    def test_noise_rejected(self, tmp_repo):
        for noise in ["yes", "ok", "sure", "no"]:
            assert store.capture_task(tmp_repo, noise, SESSION) is None

    def test_only_task_type_entries_created(self, tmp_repo):
        store.capture_task(tmp_repo, "Add OAuth2 authentication to the API", SESSION)
        store.capture_task(tmp_repo, "yes", SESSION)
        data = store._load(tmp_repo)
        task_entries = [e for e in data["entries"] if e["type"] == "task"]
        assert len(task_entries) == 1


# ── 10. Bootstrap scan ────────────────────────────────────────────────────────

class TestBootstrapScan:
    def test_returns_dict_with_inferred_and_gaps(self, tmp_repo):
        result = store.bootstrap_scan(tmp_repo)
        assert isinstance(result, dict)
        assert "inferred" in result
        assert "gaps" in result

    def test_inferred_and_gaps_are_lists(self, tmp_repo):
        result = store.bootstrap_scan(tmp_repo)
        assert isinstance(result["inferred"], list)
        assert isinstance(result["gaps"], list)


# ── 11. Uninstall ─────────────────────────────────────────────────────────────

class TestUninstall:
    def test_mcp_server_removed(self, tmp_home):
        cli.install()
        cli.uninstall()
        claude = json.loads((tmp_home / ".claude.json").read_text())
        assert "contexer" not in claude.get("mcpServers", {})

    def test_hooks_removed(self, tmp_home):
        cli.install()
        cli.uninstall()
        hooks = _settings(tmp_home).get("hooks", {})
        assert "SessionStart" not in hooks
        assert "PostToolUse" not in hooks
        assert "UserPromptSubmit" not in hooks
        assert "PreCompact" not in hooks
        assert "PostCompact" not in hooks

    def test_permissions_removed(self, tmp_home):
        cli.install()
        cli.uninstall()
        allow = _settings(tmp_home).get("permissions", {}).get("allow", [])
        assert not any("contexer" in p for p in allow)
