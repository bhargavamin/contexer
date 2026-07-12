"""Tests for global context store — all permutations of global/repo store interaction."""
import json
from pathlib import Path

import pytest

from contexer import store


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Redirect STORE_DIR to tmp_path so tests never touch ~/.contexer/."""
    monkeypatch.setattr(store, "STORE_DIR", tmp_path)
    tmp_path.mkdir(parents=True, exist_ok=True)
    return tmp_path


REPO = "/tmp/myproject"
SESSION = "test-session-id"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _add_global(content: str, subtype: str = "convention") -> str | None:
    _, eid = store.update_global_decision(content, SESSION, subtype)
    return eid


def _add_repo(content: str, subtype: str = "constraint") -> str | None:
    _, eid = store.update_decision(REPO, content, SESSION, subtype)
    return eid


# ── update_global_decision ────────────────────────────────────────────────────

class TestUpdateGlobalDecision:
    def test_stores_convention(self):
        ok, eid = store.update_global_decision("Use uv not pip", SESSION, "convention")
        assert ok is True
        assert eid is not None

    def test_stores_constraint(self):
        ok, eid = store.update_global_decision("Never commit untested code", SESSION, "constraint")
        assert ok is True
        assert eid is not None

    def test_rejects_architecture(self):
        ok, eid = store.update_global_decision("Use REST not GraphQL", SESSION, "architecture")
        assert ok is False
        assert eid is None

    def test_rejects_pattern(self):
        ok, eid = store.update_global_decision("Validate at the boundary", SESSION, "pattern")
        assert ok is False
        assert eid is None

    def test_defaults_to_convention_when_subtype_omitted(self):
        store.update_global_decision("Always use conventional commits", SESSION)
        decisions = store.get_global_decisions()
        assert decisions[0]["subtype"] == "convention"

    def test_novelty_filter_rejects_duplicate(self):
        store.update_global_decision("Use uv not pip for all dependency management", SESSION, "convention")
        ok, _ = store.update_global_decision("Use uv not pip for all dependency management", SESSION, "convention")
        assert ok is False

    def test_novelty_filter_accepts_distinct_content(self):
        store.update_global_decision("Use uv not pip", SESSION, "convention")
        ok, _ = store.update_global_decision("Never commit untested code", SESSION, "constraint")
        assert ok is True

    def test_written_to_global_file_not_repo_file(self, isolated_store):
        _add_global("Use uv not pip", "convention")
        global_file = isolated_store / "_global.json"
        repo_slug = store._slug(REPO)
        repo_file = isolated_store / f"{repo_slug}.json"
        assert global_file.exists()
        assert not repo_file.exists()

    def test_global_file_not_contaminated_in_repo_store(self, isolated_store):
        _add_global("Use uv not pip", "convention")
        _add_repo("Repo-specific constraint", "constraint")
        global_data = json.loads((isolated_store / "_global.json").read_text())
        repo_slug = store._slug(REPO)
        repo_data = json.loads((isolated_store / f"{repo_slug}.json").read_text())
        global_contents = [e["content"] for e in global_data["entries"]]
        repo_contents = [e["content"] for e in repo_data["entries"]]
        assert "Use uv not pip" in global_contents
        assert "Use uv not pip" not in repo_contents
        assert "Repo-specific constraint" in repo_contents
        assert "Repo-specific constraint" not in global_contents


# ── get_global_decisions ──────────────────────────────────────────────────────

class TestGetGlobalDecisions:
    def test_returns_empty_list_when_no_global(self):
        assert store.get_global_decisions() == []

    def test_returns_all_decisions(self):
        _add_global("Use uv not pip", "convention")
        _add_global("Never commit untested code", "constraint")
        decisions = store.get_global_decisions()
        assert len(decisions) == 2

    def test_filters_by_subtype_convention(self):
        _add_global("Use uv not pip", "convention")
        _add_global("Never commit untested code", "constraint")
        conventions = store.get_global_decisions(entry_type="convention")
        assert all(d["subtype"] == "convention" for d in conventions)
        assert len(conventions) == 1

    def test_filters_by_subtype_constraint(self):
        _add_global("Use uv not pip", "convention")
        _add_global("Never commit untested code", "constraint")
        constraints = store.get_global_decisions(entry_type="constraint")
        assert all(d["subtype"] == "constraint" for d in constraints)
        assert len(constraints) == 1

    def test_returns_only_decisions_not_tasks(self):
        # Tasks can't be stored globally, but verify type filter is applied
        data = store._load_global()
        data["entries"].append({"id": "x", "type": "task", "content": "some task",
                                "session_id": SESSION, "timestamp": "2024-01-01T00:00:00+00:00"})
        store._save_global(data)
        decisions = store.get_global_decisions()
        assert not any(d["type"] == "task" for d in decisions)


# ── get_global_context (formatted) ───────────────────────────────────────────

class TestGetGlobalContext:
    def test_empty_store_message(self):
        result = store.get_global_context()
        assert "No global context stored" in result

    def test_shows_stored_decisions(self):
        _add_global("Use uv not pip", "convention")
        result = store.get_global_context()
        assert "Use uv not pip" in result

    def test_header_identifies_global(self):
        _add_global("Use uv not pip", "convention")
        result = store.get_global_context()
        assert "Global context" in result

    def test_query_filter_matches_content(self):
        _add_global("Use uv not pip", "convention")
        _add_global("Never commit untested code", "constraint")
        result = store.get_global_context(query="uv")
        assert "uv not pip" in result
        assert "untested" not in result

    def test_query_filter_no_match(self):
        _add_global("Use uv not pip", "convention")
        result = store.get_global_context(query="postgres")
        assert "No matching" in result

    def test_entry_type_filter(self):
        _add_global("Use uv not pip", "convention")
        _add_global("Never commit untested code", "constraint")
        result = store.get_global_context(entry_type="constraint")
        assert "untested" in result
        assert "uv not pip" not in result

    def test_get_context_repo_does_not_show_global(self):
        _add_global("This is a global rule", "convention")
        _add_repo("This is a repo rule", "constraint")
        repo_result = store.get_context(REPO)
        assert "global rule" not in repo_result
        assert "repo rule" in repo_result


# ── get_session_start_context permutations ────────────────────────────────────

class TestSessionStartPermutations:
    def test_no_global_no_repo_bootstraps(self):
        result = store.get_session_start_context(REPO)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Do NOT" in ctx
        assert "yes" in ctx
        assert "Global rules" not in ctx

    def test_global_only_no_repo_bootstraps_with_global_rules(self):
        _add_global("Always use conventional commits", "convention")
        result = store.get_session_start_context(REPO)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Do NOT" in ctx
        assert "yes" in ctx
        assert "Global rules" in ctx
        assert "conventional commits" in ctx

    def test_global_only_no_repo_status_mentions_global(self):
        _add_global("Always use conventional commits", "convention")
        result = store.get_session_start_context(REPO)
        msg = result["systemMessage"]
        assert "global rule" in msg
        assert "setup" in msg or "no context" in msg.lower()

    def test_no_global_repo_has_decisions_injects_project_rules(self):
        # Constraints start as pending_approval; they appear in the pending notice, not as rules.
        _add_repo("Never commit without tests", "constraint")
        result = store.get_session_start_context(REPO)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "pending" in ctx.lower()
        assert "review_pending" in ctx
        assert "Global rules" not in ctx

    def test_no_global_repo_has_decisions_status_line(self):
        _add_repo("Never commit without tests", "constraint")
        result = store.get_session_start_context(REPO)
        msg = result["systemMessage"]
        assert "pending review" in msg
        assert "global rule" not in msg

    def test_both_global_and_repo_injects_both_sections(self):
        # Global conventions (approved) appear as "Global rules"; repo constraints (pending) appear in pending notice.
        _add_global("Always use conventional commits", "convention")
        _add_repo("Never commit without tests", "constraint")
        result = store.get_session_start_context(REPO)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Global rules" in ctx
        assert "conventional commits" in ctx
        assert "pending" in ctx.lower()
        assert "review_pending" in ctx

    def test_both_global_and_repo_global_comes_first(self):
        # Global rules section should appear before the pending notice.
        _add_global("Always use conventional commits", "convention")
        _add_repo("Never commit without tests", "constraint")
        result = store.get_session_start_context(REPO)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        global_pos = ctx.index("Global rules")
        pending_pos = ctx.lower().index("pending")
        assert global_pos < pending_pos

    def test_both_global_and_repo_status_line_shows_both(self):
        _add_global("Always use conventional commits", "convention")
        _add_repo("Never commit without tests", "constraint")
        result = store.get_session_start_context(REPO)
        msg = result["systemMessage"]
        assert "global rule" in msg
        assert "pending review" in msg  # repo constraint is pending -> count-only

    def test_deferred_decisions_mentioned_in_context(self):
        _add_repo("Chose REST over GraphQL for external API", "architecture")
        result = store.get_session_start_context(REPO)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "architecture" in ctx.lower() or "decision" in ctx.lower()

    def test_deferred_decisions_status_mentions_on_demand(self):
        _add_repo("Chose REST over GraphQL for external API", "architecture")
        result = store.get_session_start_context(REPO)
        msg = result["systemMessage"]
        assert "on demand" in msg or "decision" in msg

    def test_multiple_globals_all_injected(self):
        _add_global("Always use conventional commits", "convention")
        _add_global("Never commit untested code", "constraint")
        _add_repo("Never commit without tests", "constraint")
        result = store.get_session_start_context(REPO)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "conventional commits" in ctx
        assert "untested code" in ctx


# ── get_context_for_prompt — global fallback ──────────────────────────────────

class TestContextForPromptGlobalFallback:
    def test_silent_for_non_rationale_prompt(self):
        _add_global("Use uv not pip", "convention")
        result = store.get_context_for_prompt(REPO, "add a login feature")
        assert result == ""

    def test_silent_when_no_matching_decisions(self):
        _add_global("Use uv not pip", "convention")
        result = store.get_context_for_prompt(REPO, "why did we choose postgres?")
        assert result == ""

    def test_returns_repo_decision_when_match(self):
        _add_repo("We use postgres for the main data store because of JSONB support", "architecture")
        result = store.get_context_for_prompt(REPO, "why did we choose postgres?")
        assert result != ""
        assert "postgres" in result.lower()

    def test_falls_back_to_global_when_repo_empty(self):
        # "management" (10 chars) is the longest keyword — always in top-3, guaranteed to match
        _add_global("Always prefer pipenv for package management across all projects", "convention")
        result = store.get_context_for_prompt(REPO, "why did we choose this package management approach?")
        assert result != ""
        assert "global" in result.lower()

    def test_falls_back_to_global_when_repo_has_no_match(self):
        _add_repo("We use postgres for the main data store", "architecture")
        _add_global("Always prefer pipenv for package management across all projects", "convention")
        # "management" keyword only appears in global, not in repo
        result = store.get_context_for_prompt(REPO, "why did we choose this package management approach?")
        assert result != ""
        assert "global" in result.lower()

    def test_repo_match_takes_priority_over_global(self):
        _add_repo("We use postgres not sqlite for production data because of scale", "architecture")
        _add_global("Always use uv not pip", "convention")
        result = store.get_context_for_prompt(REPO, "why did we decide on postgres?")
        assert result != ""
        # Should mention repo context, not global
        assert "global context" not in result.lower()
        assert "postgres" in result.lower()

    def test_silent_when_no_entries_anywhere(self):
        result = store.get_context_for_prompt(REPO, "why did we use postgres?")
        assert result == ""


class TestGlobalRecurrence:
    """A restated global rule must record a recurrence (bump count + track session),
    mirroring the repo path — not vanish silently (review finding H4)."""

    def test_new_global_entry_has_recurrence_fields(self):
        store.update_global_decision("always sign commits with a gpg key", "s1", "convention")
        entry = store._load_global()["entries"][0]
        assert entry["occurrence_count"] == 1
        assert entry["session_ids"] == ["s1"]

    def test_restated_global_rule_bumps_count_and_session(self):
        store.update_global_decision("always sign every commit with a gpg key", "s1", "convention")
        ok, _ = store.update_global_decision("always sign every commit with a gpg key", "s2", "convention")
        assert ok is False  # duplicate, not stored as new
        entries = store._load_global()["entries"]
        assert len(entries) == 1
        assert entries[0]["occurrence_count"] == 2
        assert set(entries[0]["session_ids"]) == {"s1", "s2"}
