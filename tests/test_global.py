"""Tests for global context store — all permutations of global/repo store interaction."""
import json

import pytest

from contexer import store
from tests.seams import redirect_store_dir


@pytest.fixture(autouse=True)
def isolated_store(tmp_path, monkeypatch):
    """Redirect STORE_DIR to tmp_path so tests never touch ~/.contexer/."""
    redirect_store_dir(monkeypatch, tmp_path)
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


def _truncate_global() -> str:
    """Truncate `_global.json` so it will not parse — an interrupted write, a disk-full
    truncation, a hand-edit typo. Returns the exact bytes now on disk."""
    path = store._global_path()
    broken = path.read_text(encoding="utf-8")[:-3]
    path.write_text(broken, encoding="utf-8")
    return broken


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

    def test_provenance_defaults_to_ai(self):
        store.update_global_decision("Always use conventional commits", SESSION)
        entry = store.get_global_decisions()[0]
        assert entry["created_by"] == "ai"
        assert entry["revisions"][0]["source"] == "ai"

    def test_provenance_is_carried_through_to_the_entry_and_its_revision(self):
        """A rule the developer typed by hand must not read as AI-authored. `created_by`
        also drives `_compute_confidence`'s "Stated by developer" factor, so losing it
        under-scores every hand-written rule."""
        store.update_global_decision("Never commit untested code", SESSION, "constraint",
                                     created_by="human")
        entry = store.get_global_decisions()[0]
        assert entry["created_by"] == "human"
        assert entry["revisions"][0]["source"] == "human"
        assert "Stated by developer" in entry["confidence_factors"]

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
        repo_slug = store.repo_slug(REPO)
        repo_file = isolated_store / f"{repo_slug}.json"
        assert global_file.exists()
        assert not repo_file.exists()

    def test_global_file_not_contaminated_in_repo_store(self, isolated_store):
        _add_global("Use uv not pip", "convention")
        _add_repo("Repo-specific constraint", "constraint")
        global_data = json.loads((isolated_store / "_global.json").read_text())
        repo_slug = store.repo_slug(REPO)
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
        data = store.load_global()
        data["entries"].append({"id": "x", "type": "task", "content": "some task",
                                "session_id": SESSION, "timestamp": "2024-01-01T00:00:00+00:00"})
        store.save_global(data)
        decisions = store.get_global_decisions()
        assert not any(d["type"] == "task" for d in decisions)


# ── an unreadable _global.json is reported, never silently overwritten ────────

class TestUnreadableGlobalStore:
    """`_global.json` holds every cross-repo rule on the machine. A read that degrades a
    corrupt file to "no rules", next to a write that saves that degraded state back, destroys
    the lot — the same hazard `delete_decision` already refuses for the tombstone sidecar."""

    def test_adding_a_rule_never_overwrites_a_file_it_could_not_parse(self):
        _add_global("Never commit untested code to the main branch", "constraint")
        _add_global("Always write conventional commit messages in every repo", "convention")
        broken = _truncate_global()

        assert store.list_global_rules()["ok"] is False, \
            "the view the Add button sits in must not read a corrupt file as empty"
        ok, eid = store.update_global_decision("Always sign every commit with a gpg key",
                                               SESSION, "convention")

        assert ok is False
        assert eid is None
        after = store._global_path().read_text(encoding="utf-8")
        assert after == broken, "the add rewrote _global.json over rules it could not read"
        assert "untested code" in after and "conventional commit" in after, \
            "the original rules must still be recoverable by hand"

    def test_the_global_view_says_unreadable_instead_of_no_rules(self):
        _add_global("Never commit untested code to the main branch", "constraint")
        _truncate_global()

        view = store.list_global_rules()

        assert view["ok"] is False
        assert "JSONDecodeError" in view["error"]
        assert view["rules"] == []

    def test_deleting_a_rule_never_overwrites_a_file_it_could_not_parse(self):
        entry_id = _add_global("Never commit untested code to the main branch", "constraint")
        broken = _truncate_global()

        ok, msg = store.delete_global_rule(entry_id)

        assert ok is False
        assert "unreadable" in msg
        assert store._global_path().read_text(encoding="utf-8") == broken

    def test_a_non_object_entry_is_unreadable_rather_than_a_crash(self):
        # Same shape as the tombstone sidecar's: `entries` was checked for being a list and
        # nothing more, so one string in it reached `entry.get(...)`.
        store._global_path().write_text('{"entries": ["oops"]}', encoding="utf-8")

        assert store.global_diagnostics()["ok"] is False
        assert store.get_global_decisions() == []
        assert store.get_global_context() != ""
        assert store.update_global_decision("Always sign every commit with a gpg key",
                                            SESSION, "convention") == (False, None)

    def test_a_session_read_still_degrades_instead_of_raising(self):
        _add_global("Never commit untested code to the main branch", "constraint")
        _truncate_global()
        assert store.load_global() == {"repo_path": store.GLOBAL_SLUG, "entries": []}
        assert store.get_global_decisions() == []


class TestGlobalDiagnostics:
    def test_a_readable_file_is_ok(self):
        _add_global("Use uv not pip for dependency management", "convention")
        assert store.global_diagnostics() == {"ok": True, "error": None}

    def test_a_missing_file_is_ok_not_corrupt(self):
        assert not store._global_path().exists()
        assert store.global_diagnostics() == {"ok": True, "error": None}

    def test_a_non_object_file_is_not_ok(self):
        store._global_path().write_text("[]", encoding="utf-8")
        assert store.global_diagnostics()["ok"] is False

    def test_undecodable_bytes_are_not_ok(self):
        store._global_path().write_bytes(b'{"entries": [], "x": "\xff\xfe"}')
        assert "UnicodeDecodeError" in store.global_diagnostics()["error"]


class TestListGlobalRules:
    def test_an_empty_store_is_ok_and_says_so(self):
        assert store.list_global_rules() == {"ok": True, "error": None, "rules": []}

    def test_the_rows_carry_the_console_row_shape(self):
        _add_global("Use uv not pip for dependency management", "convention")
        view = store.list_global_rules()
        assert view["ok"] is True
        assert set(view["rules"][0]) == {"id", "title", "content", "subtype", "created_by",
                                         "timestamp", "updated_at", "revision", "confidence"}

    def test_only_decision_entries_are_listed(self):
        _add_global("Use uv not pip for dependency management", "convention")
        data = store.load_global()
        data["entries"].append({"id": "x", "type": "task", "content": "some task",
                                "session_id": SESSION, "timestamp": "2024-01-01T00:00:00+00:00"})
        store.save_global(data)
        assert [r["id"] for r in store.list_global_rules()["rules"]] != ["x"]
        assert len(store.list_global_rules()["rules"]) == 1


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
        entry = store.load_global()["entries"][0]
        assert entry["occurrence_count"] == 1
        assert entry["session_ids"] == ["s1"]

    def test_restated_global_rule_bumps_count_and_session(self):
        store.update_global_decision("always sign every commit with a gpg key", "s1", "convention")
        ok, _ = store.update_global_decision("always sign every commit with a gpg key", "s2", "convention")
        assert ok is False  # duplicate, not stored as new
        entries = store.load_global()["entries"]
        assert len(entries) == 1
        assert entries[0]["occurrence_count"] == 2
        assert set(entries[0]["session_ids"]) == {"s1", "s2"}
