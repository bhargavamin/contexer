"""End-to-end tests — exercises the full hook sequence from install through context recall.

Covers: install/uninstall, reinstall idempotency, session start states, bootstrap offer,
constraint capture, decision storage, context retrieval, rationale injection, task capture,
and bootstrap scan. Uses tmp_path + monkeypatch to isolate all filesystem side effects.
"""
import json
import os
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


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """Real git repo with global/system git config isolated; returns its path."""
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    repo = tmp_path / "gitrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return str(repo)


ME = "me@test.local"
OTHER = "other@test.local"


def _git_commit(repo: str, email: str, n: int = 1) -> None:
    for i in range(n):
        (Path(repo) / f"{email.split('@')[0]}-{i}.txt").write_text(f"{email}-{i}")
        subprocess.run(["git", "add", "."], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", f"user.email={email}", "-c", "user.name=T",
             "-c", "commit.gpgsign=false", "commit", "-q", "-m", f"c-{i}"],
            cwd=repo, check=True)


def _set_me(repo: str) -> None:
    subprocess.run(["git", "config", "user.email", ME], cwd=repo, check=True)


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
        assert _in_groups(poc, "get_post_compact_context")

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
        assert "yes" in ctx.lower() and "no" in ctx.lower()
        assert "Do NOT" in ctx

    def test_new_repo_has_stop_mandate(self, tmp_repo):
        """Bootstrap must tell Claude to output only the offer and wait."""
        result = store.get_session_start_context(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "CRITICAL" in ctx or "stop completely" in ctx.lower()

    def test_new_repo_offers_skip_path(self, tmp_repo):
        result = store.get_session_start_context(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert any(w in ctx for w in ["no", "skip"])

    def test_new_repo_system_message_signals_setup(self, tmp_repo):
        result = store.get_session_start_context(tmp_repo)
        assert "setup" in result["systemMessage"] or "no context" in result["systemMessage"].lower()

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
        assert "yes" in ctx.lower() and "no" in ctx.lower()

    def test_no_context_has_do_not_directive(self, tmp_repo):
        result = store.get_bootstrap_context_prompt(tmp_repo)
        ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "Do NOT" in ctx

    def test_with_context_returns_empty(self, tmp_repo):
        store.update_decision(tmp_repo, "Use Django for the web layer", SESSION, "architecture")
        result = store.get_bootstrap_context_prompt(tmp_repo)
        ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert ctx == ""


# ── 4b. PostCompact bootstrap re-trigger ─────────────────────────────────────

class TestPostCompactContext:
    def test_no_context_returns_bootstrap_offer(self, tmp_repo):
        """Bug fix: PostCompact was returning 'No context stored' string, not a bootstrap offer."""
        result = store.get_post_compact_context(tmp_repo)
        sys_msg = result.get("systemMessage", "")
        assert "yes" in sys_msg.lower() and "full" in sys_msg.lower(), \
            "PostCompact should re-offer bootstrap when no context stored"

    def test_no_context_does_not_say_reloaded(self, tmp_repo):
        result = store.get_post_compact_context(tmp_repo)
        sys_msg = result.get("systemMessage", "")
        assert "context reloaded" not in sys_msg

    def test_with_context_returns_reloaded_message(self, tmp_repo):
        store.update_decision(tmp_repo, "Use PostgreSQL for persistence", SESSION, "architecture")
        result = store.get_post_compact_context(tmp_repo)
        sys_msg = result.get("systemMessage", "")
        assert "reloaded after compaction" in sys_msg

    def test_with_context_includes_decisions(self, tmp_repo):
        store.update_decision(tmp_repo, "Use PostgreSQL for the main database", SESSION, "architecture")
        result = store.get_post_compact_context(tmp_repo)
        sys_msg = result.get("systemMessage", "")
        assert "PostgreSQL" in sys_msg

    def test_returns_dict_always(self, tmp_repo):
        result = store.get_post_compact_context(tmp_repo)
        assert isinstance(result, dict)
        assert "systemMessage" in result


# ── 4c. Bootstrap instruction correctness ────────────────────────────────────

class TestBootstrapInstructions:
    def test_always_shown_for_task_prompts(self, tmp_repo):
        """Bootstrap offer must be shown always — even when user sent a task or question."""
        lines = store._build_bootstrap_context(tmp_repo)
        full_text = "\n".join(lines)
        assert "CRITICAL" in full_text, "Bootstrap must use a critical instruction so it isn't skipped for task prompts"

    def test_no_just_continues_skip_rule(self, tmp_repo):
        """'just continues → skip' was the original bug — must be gone."""
        lines = store._build_bootstrap_context(tmp_repo)
        full_text = "\n".join(lines)
        assert "just continues" not in full_text

    def test_stops_and_waits_for_response(self, tmp_repo):
        """Bootstrap must pause after the offer — not proceed with the original task."""
        lines = store._build_bootstrap_context(tmp_repo)
        full_text = "\n".join(lines)
        assert "CRITICAL" in full_text or "stop completely" in full_text.lower()
        assert "wait" in full_text.lower()
        assert "immediately answer" not in full_text

    def test_no_skip_path_for_direct_no(self, tmp_repo):
        lines = store._build_bootstrap_context(tmp_repo)
        full_text = "\n".join(lines)
        assert "no or skip" in full_text

    def test_ambiguous_offer_options_are_parallel_modes(self, tmp_repo):
        """Options must be modes, not yes/no mixed with modes. tmp_repo has no .git → ambiguous variant."""
        full_text = "\n".join(store._build_bootstrap_context(tmp_repo))
        for option in ["· quick —", "· full —", "· some —", "· scan —", "· skip —"]:
            assert option in full_text

    def test_scan_option_maps_to_low_insight(self, tmp_repo):
        full_text = "\n".join(store._build_bootstrap_context(tmp_repo))
        assert "insight='low'" in full_text
        assert "do NOT quiz" in full_text

    def test_some_option_maps_to_medium_insight(self, tmp_repo):
        full_text = "\n".join(store._build_bootstrap_context(tmp_repo))
        assert "insight='medium'" in full_text


# ── 4d. Offer variants by detected insight ────────────────────────────────────

class TestOfferVariants:
    def test_high_decisive_skips_familiarity_and_demotes_scan(self, git_repo):
        _git_commit(git_repo, ME, 5)
        _set_me(git_repo)
        text = "\n".join(store._build_bootstrap_context(git_repo))
        assert "How well do you know this repo?" not in text
        assert "· quick —" in text and "· full —" in text
        assert "reply scan if you're actually new" in text, "override must stay visible"

    def test_low_decisive_states_evidence_not_conclusion(self, git_repo, tmp_path):
        _git_commit(git_repo, OTHER, 3)
        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", git_repo, str(clone)], check=True)
        subprocess.run(["git", "config", "user.email", ME], cwd=clone, check=True)
        text = "\n".join(store._build_bootstrap_context(str(clone)))
        assert "No commits from your git email" in text, "must state the evidence"
        assert "you're new to this repo" not in text, "must not assert a conclusion that may be wrong"
        assert "· scan —" in text and "quick / full" in text, "override must stay visible"

    def test_ambiguous_asks_familiarity(self, tmp_repo):
        text = "\n".join(store._build_bootstrap_context(tmp_repo))
        assert "How well do you know this repo?" in text

    def test_medium_nondecisive_suggests_some(self, git_repo):
        _git_commit(git_repo, OTHER, 1)
        _git_commit(git_repo, ME, 2)
        _set_me(git_repo)
        text = "\n".join(store._build_bootstrap_context(git_repo))
        assert "How well do you know this repo?" in text, "1-4 commits is non-decisive — must still ask"
        assert "'some' is likely right" in text

    def test_newcomer_question_check_comes_before_menu(self, tmp_repo):
        """'what is this repo doing?' is low-insight evidence — the check must precede the
        'response must be ONLY the offer' directive, or it loses to it."""
        text = "\n".join(store._build_bootstrap_context(tmp_repo))
        assert "STEP 0" in text
        assert "assume you're new here" in text
        assert text.index("STEP 0") < text.index("ENTIRE response must be ONLY the offer block")

    def test_newcomer_question_check_in_low_variant(self, git_repo, tmp_path):
        _git_commit(git_repo, OTHER, 3)
        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", git_repo, str(clone)], check=True)
        subprocess.run(["git", "config", "user.email", ME], cwd=clone, check=True)
        text = "\n".join(store._build_bootstrap_context(str(clone)))
        assert "STEP 0" in text

    def test_no_newcomer_check_when_high_decisive(self, git_repo):
        """Commits by this user outweigh one curious question — keep the menu."""
        _git_commit(git_repo, ME, 5)
        _set_me(git_repo)
        text = "\n".join(store._build_bootstrap_context(git_repo))
        assert "STEP 0" not in text

    def test_purpose_question_never_echoed_back(self, tmp_repo):
        """Picking full after asking 'what does this repo do?' must not quiz the user
        with their own question."""
        text = "\n".join(store._build_bootstrap_context(tmp_repo))
        assert "never echo it back" in text
        assert "store the confirmed summary as the purpose" in text


# ── 4f. Deterministic newcomer-question detection ─────────────────────────────

class TestNewcomerQuestionDetection:
    @pytest.mark.parametrize("prompt", [
        "what is this repo doing?",
        "What does this project do?",
        "explain this codebase",
        "tell me about this repo",
        "how does this code work",
        "walk me through this codebase please",
        "give me an overview of this project",
        "whats this repo about",
    ])
    def test_newcomer_questions_match(self, prompt):
        assert store._is_newcomer_question(prompt) is True

    @pytest.mark.parametrize("prompt", [
        "fix the bug in store.py",
        "add a logout endpoint to the api",
        "why did we choose uv over pip?",
        "what is this function doing",  # code-element question, not repo-level
        "refactor the elevator scheduling logic",
        "",
    ])
    def test_other_prompts_do_not_match(self, prompt):
        assert store._is_newcomer_question(prompt) is False

    def test_newcomer_prompt_overrides_menu(self, tmp_repo):
        result = store.get_bootstrap_context_prompt(tmp_repo, "what is this repo doing?")
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "OVERRIDE" in ctx and "assume you're new here" in ctx
        assert "How well do you know this repo?" not in ctx

    def test_task_prompt_gets_the_menu(self, tmp_repo):
        result = store.get_bootstrap_context_prompt(tmp_repo, "fix the login bug")
        assert "OVERRIDE" not in result["hookSpecificOutput"]["additionalContext"]

    def test_newcomer_prompt_in_own_repo_keeps_menu(self, git_repo):
        """Commits by the user outweigh one curious question — even deterministically."""
        _git_commit(git_repo, ME, 5)
        _set_me(git_repo)
        result = store.get_bootstrap_context_prompt(git_repo, "what is this repo doing?")
        assert "OVERRIDE" not in result["hookSpecificOutput"]["additionalContext"]

    def test_existing_context_stays_silent(self, tmp_repo):
        store.update_decision(tmp_repo, "decided to use postgres for primary storage", "s1")
        assert store.get_bootstrap_context_prompt(tmp_repo, "what is this repo doing?") == {}

    @pytest.mark.parametrize("raw,expected", [
        ('{"prompt": "hello"}', "hello"),
        ("", ""),
        ("not json at all", ""),
        ("[1, 2, 3]", ""),
        ('{"no_prompt_key": 1}', ""),
        ('{broken json', ""),
    ])
    def test_prompt_from_hook_stdin_never_crashes(self, raw, expected):
        assert store.prompt_from_hook_stdin(raw) == expected


# ── 4g. Resume-aware session start ────────────────────────────────────────────

class TestResumeSessionStart:
    @pytest.mark.parametrize("raw,expected", [
        ('{"source": "resume"}', "resume"),
        ('{"source": "startup"}', "startup"),
        ("", ""),
        ("garbage", ""),
        ('{"other": 1}', ""),
    ])
    def test_source_from_hook_stdin_is_safe(self, raw, expected):
        assert store.source_from_hook_stdin(raw) == expected

    def test_resume_with_context_skips_reinjection(self, tmp_repo):
        """The resumed conversation already contains the original injection —
        re-injecting duplicates ~1k tokens."""
        store.update_decision(tmp_repo, "decided to use postgres for primary storage", "s1")
        result = store.get_session_start_context(tmp_repo, source="resume")
        assert "hookSpecificOutput" not in result
        assert "resumed" in result["systemMessage"]

    def test_resume_without_context_mines_conversation(self, tmp_repo):
        result = store.get_session_start_context(tmp_repo, source="resume")
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "RESUMED session" in ctx
        assert "update_context" in ctx and "bootstrap_context" in ctx
        assert "never invent" in ctx
        assert "How well do you know this repo?" not in ctx, "no menu on resume — mine instead"

    def test_resume_mining_suppresses_menu_fallback(self, tmp_repo):
        """The UserPromptSubmit once-hook must not inject a contradictory menu after
        the SessionStart resume branch injected mining instructions."""
        store.get_session_start_context(tmp_repo, source="resume")
        assert store.get_bootstrap_context_prompt(tmp_repo, "continue please") == {}
        # flag is consumed — the next call behaves normally again
        result = store.get_bootstrap_context_prompt(tmp_repo, "continue please")
        assert result != {}

    def test_startup_clears_stale_resume_flag(self, tmp_repo):
        store.get_session_start_context(tmp_repo, source="resume")  # writes flag
        store.get_session_start_context(tmp_repo, source="startup")  # must clear it
        result = store.get_bootstrap_context_prompt(tmp_repo, "fix the bug")
        assert result != {}, "stale resume flag must not suppress the bootstrap fallback"

    @pytest.mark.parametrize("source", ["", "startup", "clear", "compact"])
    def test_non_resume_sources_keep_menu_behavior(self, tmp_repo, source):
        result = store.get_session_start_context(tmp_repo, source=source)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "RESUMED session" not in ctx
        assert "CRITICAL INSTRUCTION" in ctx

    def test_resume_includes_global_rules(self, tmp_repo):
        store.update_global_decision("always use conventional commits", "s1", subtype="constraint")
        result = store.get_session_start_context(tmp_repo, source="resume")
        assert "Global rules" in result["hookSpecificOutput"]["additionalContext"]


# ── 4h. Reaction-matrix invariants ────────────────────────────────────────────

class TestReactionMatrix:
    """Every combination of repo state × insight must keep the conversation sane:
    bounded question counts, well-formed gaps, and a handler for every advertised option."""

    def _repo_states(self, tmp_path):
        bare = tmp_path / "bare"; bare.mkdir()
        simple = tmp_path / "simple"; simple.mkdir()
        (simple / "main.py").write_text("print('hi')\n")
        rich = tmp_path / "rich"; rich.mkdir()
        (rich / "pyproject.toml").write_text(
            '[project]\nname = "x"\ndependencies = ["fastapi", "stripe", "boto3", '
            '"httpx", "pydantic", "sqlalchemy"]\n')
        missing = tmp_path / "never-created"
        return [str(bare), str(simple), str(rich), str(missing)]

    def test_gap_invariants_hold_for_all_combinations(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
        for repo in self._repo_states(tmp_path):
            for insight in ["low", "medium", "high", "", "banana"]:
                result = store.bootstrap_scan(repo, insight=insight)
                gaps = result["gaps"]
                label = f"{Path(repo).name} × insight={insight!r}"
                assert 1 <= len(gaps) <= 10, f"{label}: {len(gaps)} gaps"
                assert result["insight"] in {"low", "medium", "high"}, label
                for g in gaps:
                    assert g["question"].rstrip().endswith("?"), f"{label}: {g['question']!r}"
                    assert g["subtype"] in {"architecture", "constraint", "pattern", "convention"}, label
                    assert g["min_insight"] in {"low", "medium", "high"}, label
                    assert g["assumption"] and g["hint"], label

    def test_every_advertised_option_has_a_handler(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
        for repo in self._repo_states(tmp_path):
            text = "\n".join(store._build_bootstrap_context(repo))
            for option in ["quick", "full", "some", "scan"]:
                if f"· {option} —" in text:
                    assert f"If {option}" in text, \
                        f"{Path(repo).name}: option '{option}' advertised without a handler"
            assert "no or skip" in text, f"{Path(repo).name}: skip has no handler"


# ── 4e. Insight detection from git signals ────────────────────────────────────

class TestInsightDetection:
    def test_plain_dir_without_git_is_ambiguous(self, tmp_repo):
        assert store._detect_insight(tmp_repo) == ("low", False)

    def test_no_user_email_is_ambiguous_never_high(self, git_repo):
        """The --author='' trap: empty email must not match every commit."""
        _git_commit(git_repo, OTHER, 6)
        assert store._detect_insight(git_repo) == ("low", False)

    def test_empty_repo_with_email_is_creator(self, git_repo):
        _set_me(git_repo)
        assert store._detect_insight(git_repo) == ("high", True)

    def test_first_commit_author_is_high_regardless_of_count(self, git_repo):
        _git_commit(git_repo, ME, 1)
        _git_commit(git_repo, OTHER, 6)
        _set_me(git_repo)
        assert store._detect_insight(git_repo) == ("high", True)

    def test_five_own_commits_is_high_decisive(self, git_repo):
        _git_commit(git_repo, OTHER, 1)
        _git_commit(git_repo, ME, 5)
        _set_me(git_repo)
        assert store._detect_insight(git_repo) == ("high", True)

    def test_few_commits_is_medium_nondecisive(self, git_repo):
        _git_commit(git_repo, OTHER, 1)
        _git_commit(git_repo, ME, 2)
        _set_me(git_repo)
        assert store._detect_insight(git_repo) == ("medium", False)

    def test_zero_commits_in_local_repo_is_ambiguous(self, git_repo):
        """Could be an email mismatch in the user's own repo — must ask, not conclude."""
        _git_commit(git_repo, OTHER, 3)
        _set_me(git_repo)
        assert store._detect_insight(git_repo) == ("low", False)

    def test_fresh_clone_is_low_decisive(self, git_repo, tmp_path):
        _git_commit(git_repo, OTHER, 3)
        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", git_repo, str(clone)], check=True)
        subprocess.run(["git", "config", "user.email", ME], cwd=clone, check=True)
        assert store._detect_insight(str(clone)) == ("low", True)


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
        eid, content = store.capture_user_constraint(tmp_repo, prompt, SESSION)
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
        eid, content = store.capture_user_constraint(tmp_repo, prompt, SESSION)
        assert eid is None, f"Expected rejection for: {prompt!r}"

    def test_duplicate_directive_silently_discarded(self, tmp_repo):
        store.capture_user_constraint(tmp_repo, "always use type hints in Python", SESSION)
        eid2, content = store.capture_user_constraint(tmp_repo, "always use type hints in Python", SESSION)
        assert eid2 is None

    def test_stored_as_decision_type(self, tmp_repo):
        eid, content = store.capture_user_constraint(tmp_repo, "never push to main directly", SESSION)
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["id"] == eid)
        assert entry["type"] == "decision"

    def test_long_prompt_truncated_to_600_chars(self, tmp_repo):
        long_prompt = "always " + "x" * 700
        eid, content = store.capture_user_constraint(tmp_repo, long_prompt, SESSION)
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

    def test_insight_auto_detected_when_not_given(self, tmp_repo):
        result = store.bootstrap_scan(tmp_repo)
        assert result["insight_source"] == "auto"
        assert result["insight"] == "low" and result["decisive"] is False  # no .git → ambiguous

    def test_invalid_insight_triggers_auto_detect(self, tmp_repo):
        result = store.bootstrap_scan(tmp_repo, insight="banana")
        assert result["insight_source"] == "auto"

    def test_user_supplied_insight_is_decisive(self, tmp_repo):
        result = store.bootstrap_scan(tmp_repo, insight="high")
        assert result["insight_source"] == "user" and result["decisive"] is True

    def test_low_insight_gets_single_goal_question(self, tmp_repo):
        """First-timers can't answer insider questions — only their own goal is askable."""
        result = store.bootstrap_scan(tmp_repo, insight="low")
        assert len(result["gaps"]) == 1
        assert "planning to do" in result["gaps"][0]["question"]

    def test_medium_insight_gets_goal_and_purpose_only(self, tmp_repo):
        result = store.bootstrap_scan(tmp_repo, insight="medium")
        questions = [g["question"] for g in result["gaps"]]
        assert len(questions) == 2
        assert any("planning to do" in q for q in questions)
        assert any("What does this repo do" in q for q in questions)

    def test_high_insight_gets_no_goal_question(self, tmp_repo):
        result = store.bootstrap_scan(tmp_repo, insight="high")
        assert not any("planning to do" in g["question"] for g in result["gaps"])

    def test_low_insight_gets_no_insider_questions(self, tmp_repo):
        """Questions about conventions, compliance, CI, or exclusions assume repo authorship."""
        Path(tmp_repo).mkdir()
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "big-app"\ndependencies = ["fastapi", "sqlalchemy", '
            '"boto3", "stripe", "httpx", "pydantic"]\n'
        )
        high = store.bootstrap_scan(tmp_repo, insight="high")
        low = store.bootstrap_scan(tmp_repo, insight="low")
        assert len(high["gaps"]) > 1, "high insight should get intent questions for a production-signal repo"
        assert len(low["gaps"]) == 1, "low insight must never get insider questions"

    def test_low_insight_still_infers_facts_from_code(self, tmp_repo):
        Path(tmp_repo).mkdir()
        (Path(tmp_repo) / "pyproject.toml").write_text('[project]\nname = "some-lib"\n')
        result = store.bootstrap_scan(tmp_repo, insight="low")
        assert any("some-lib" in f for f in result["inferred"])

    def test_full_on_bare_repo_still_interviews(self, tmp_repo):
        """'full' is explicit opt-in to an interview — a simple repo must not collapse it
        to a single question; the author's head holds decisions no scan can reach."""
        result = store.bootstrap_scan(tmp_repo, insight="high")
        assert len(result["gaps"]) >= 4
        subtypes = {g["subtype"] for g in result["gaps"]}
        assert {"architecture", "convention", "constraint"} <= subtypes

    def test_signal_rich_repo_gets_no_interview_padding(self, tmp_repo):
        Path(tmp_repo).mkdir()
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "big-app"\ndependencies = ["fastapi", "sqlalchemy", '
            '"boto3", "stripe", "httpx", "pydantic"]\n'
        )
        result = store.bootstrap_scan(tmp_repo, insight="high")
        assert len(result["gaps"]) >= 4
        assert not any("aren't visible in it" in g["question"] for g in result["gaps"]), \
            "signal-rich repos have real questions — generic interview padding not needed"

    def test_interview_floor_not_applied_below_high(self, tmp_repo):
        assert len(store.bootstrap_scan(tmp_repo, insight="low")["gaps"]) == 1
        assert len(store.bootstrap_scan(tmp_repo, insight="medium")["gaps"]) == 2


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

    def test_non_contexer_hooks_in_same_event_are_preserved(self, tmp_home):
        """Uninstall must not remove hooks from other tools in the same event bucket."""
        settings_path = tmp_home / ".claude" / "settings.json"
        # Pre-populate with a non-contexer hook in UserPromptSubmit
        existing = {"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": "echo other-tool"}]}
        ]}}
        settings_path.write_text(json.dumps(existing))
        cli.install()
        cli.uninstall()
        hooks = _settings(tmp_home).get("hooks", {})
        ups = hooks.get("UserPromptSubmit", [])
        cmds = [h.get("command", "") for g in ups for h in g.get("hooks", [])]
        assert any("other-tool" in c for c in cmds)

    def test_no_settings_file_is_graceful(self, tmp_home):
        """Uninstall without a prior install should not raise."""
        cli.uninstall()  # no install first — should complete without error


# ── 12. main() dispatch ───────────────────────────────────────────────────────

class TestMainDispatch:
    def test_unknown_command_exits_nonzero(self, tmp_home, monkeypatch, capsys):
        monkeypatch.setattr(sys, "argv", ["contexer", "badcmd"])
        with pytest.raises(SystemExit) as exc:
            cli.main()
        assert exc.value.code == 1
        assert "Unknown command" in capsys.readouterr().err

    def test_install_command_dispatches(self, tmp_home, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["contexer", "install"])
        cli.main()  # should not raise
        assert (tmp_home / ".claude.json").exists()

    def test_uninstall_command_dispatches(self, tmp_home, monkeypatch):
        monkeypatch.setattr(sys, "argv", ["contexer", "install"])
        cli.main()
        monkeypatch.setattr(sys, "argv", ["contexer", "uninstall"])
        cli.main()  # should not raise
