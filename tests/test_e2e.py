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

    def test_capture_constraint_command_hook_registered(self, tmp_home):
        cli.install()
        ups = _settings(tmp_home).get("hooks", {}).get("UserPromptSubmit", [])
        assert _in_groups(ups, "claude.capture_constraint")
        # capture is a command hook now, never an mcp_tool
        assert not any(h.get("type") == "mcp_tool" for g in ups for h in g.get("hooks", []))

    def test_rationale_command_hook_registered(self, tmp_home):
        cli.install()
        ups = _settings(tmp_home).get("hooks", {}).get("UserPromptSubmit", [])
        assert _in_groups(ups, "claude.rationale")

    def test_bootstrap_command_installed_globally(self, tmp_home):
        """A project-level command file only works inside that repo — end users
        installing from PyPI must get /bootstrap in ~/.claude/commands/."""
        cli.install()
        cmd = (tmp_home / ".claude" / "commands" / "bootstrap.md").read_text()
        assert "bootstrap_context" in cmd
        assert "managed by contexer" in cmd

    def test_bootstrap_command_install_is_idempotent(self, tmp_home):
        cli.install()
        first = (tmp_home / ".claude" / "commands" / "bootstrap.md").read_text()
        cli.install()
        assert (tmp_home / ".claude" / "commands" / "bootstrap.md").read_text() == first

    def test_foreign_bootstrap_command_not_clobbered(self, tmp_home):
        cmd_path = tmp_home / ".claude" / "commands" / "bootstrap.md"
        cmd_path.parent.mkdir(parents=True)
        cmd_path.write_text("my own custom bootstrap command")
        cli.install()
        assert cmd_path.read_text() == "my own custom bootstrap command"

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

    def test_post_compact_hook_not_registered(self, tmp_home):
        # PostCompact can't inject context; SessionStart(source="compact") reloads
        # silently. A PostCompact hook would only dump visible noise on /compact.
        cli.install()
        hooks = _settings(tmp_home).get("hooks", {})
        assert not hooks.get("PostCompact")

    @pytest.mark.parametrize("perm", [
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
            "capture_constraint": sum(1 for g in ups for h in g.get("hooks", [])
                                      if "claude.capture_constraint" in h.get("command", "")),
            "pending_capture": sum(1 for g in put for h in g.get("hooks", [])
                                   if ".pending_capture" in str(h)),
        }

    def test_no_duplication_after_three_installs(self, tmp_home):
        cli.install()
        cli.install()
        cli.install()
        counts = self._counts(tmp_home)
        assert counts["anchor"] == 1
        assert counts["capture_constraint"] == 1
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
        # Constraints start as pending_approval and surface in the pending notice,
        # not as pre-loaded project rules. Only after approval do they appear as rules.
        store.update_decision(tmp_repo, "Always write tests before committing", SESSION, "constraint")
        result = store.get_session_start_context(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "pending" in ctx.lower()
        assert "constraint" in ctx.lower()

    def test_constraints_injected_eagerly(self, tmp_repo):
        # Constraints require approval before injection; they appear in the pending notice.
        store.update_decision(tmp_repo, "Never commit plaintext secrets", SESSION, "constraint")
        store.update_decision(tmp_repo, "Use FastAPI for HTTP", SESSION, "architecture")
        result = store.get_session_start_context(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "pending" in ctx.lower()
        assert "constraint" in ctx.lower()

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
        """A repo question must be ANSWERED, not met with the menu — so STEP 0 (answer-first)
        must precede the 'response must be ONLY the offer' directive, or it loses to it."""
        text = "\n".join(store._build_bootstrap_context(tmp_repo))
        assert "STEP 0" in text
        assert "Answer it" in text and "do NOT show a setup menu" in text
        assert text.index("STEP 0") < text.index("ENTIRE response must be ONLY the offer block")

    def test_newcomer_question_check_in_low_variant(self, git_repo, tmp_path):
        _git_commit(git_repo, OTHER, 3)
        clone = tmp_path / "clone"
        subprocess.run(["git", "clone", "-q", git_repo, str(clone)], check=True)
        subprocess.run(["git", "config", "user.email", ME], cwd=clone, check=True)
        text = "\n".join(store._build_bootstrap_context(str(clone)))
        assert "STEP 0" in text

    def test_newcomer_check_present_when_high_decisive_with_maintainer_framing(self, git_repo):
        """A repo author asking what the repo does still gets answered — but as a fellow
        maintainer, never quizzed as a newcomer. STEP 0 applies at every insight level."""
        _git_commit(git_repo, ME, 5)
        _set_me(git_repo)
        text = "\n".join(store._build_bootstrap_context(git_repo))
        assert "STEP 0" in text
        assert "fellow maintainer" in text
        assert "I'll assume you're new" in text  # only as the NEGATIVE instruction (do NOT say)
        assert "answer as a newcomer would need" not in text

    def test_offer_includes_repo_name(self, tmp_repo):
        """Offer text must show the repo folder name so misfiring is immediately visible."""
        from pathlib import Path
        name = Path(tmp_repo).name
        text = "\n".join(store._build_bootstrap_context(tmp_repo))
        assert f'"{name}"' in text

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
        "what is repo doing?",          # article-less — the reported miss
        "what does project do",         # article-less
        "What does this project do?",
        "explain this codebase",
        "tell me about this repo",
        "how does this code work",
        "walk me through this codebase please",
        "give me an overview of this project",
        "whats this repo about",
        # summarize variants — the original misfiring report
        "summarize this repo",
        "summarize the codebase",
        "summarize this project",
        "can you summarize this repo?",
        "please summarize the code",
        "summary of this repo",
        "give me a summary of this project",
    ])
    def test_newcomer_questions_match(self, prompt):
        assert store._is_newcomer_question(prompt) is True

    @pytest.mark.parametrize("prompt", [
        "fix the bug in store.py",
        "add a logout endpoint to the api",
        "why did we choose uv over pip?",
        "what is this function doing",  # code-element question, not repo-level
        "refactor the elevator scheduling logic",
        "summarize the changes in the last commit",  # not repo-level
        "",
    ])
    def test_other_prompts_do_not_match(self, prompt):
        assert store._is_newcomer_question(prompt) is False

    def test_newcomer_prompt_overrides_menu(self, tmp_repo):
        result = store.get_bootstrap_context_prompt(tmp_repo, "what is this repo doing?")
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "OVERRIDE" in ctx and "Answer it" in ctx
        assert "update_context(subtype='architecture')" in ctx  # stores findings
        assert "How well do you know this repo?" not in ctx

    def test_articleless_prompt_overrides_menu(self, tmp_repo):
        """'what is repo doing?' (no this/the) was the reported miss — must answer, not menu."""
        result = store.get_bootstrap_context_prompt(tmp_repo, "what is repo doing?")
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "OVERRIDE" in ctx and "Answer it" in ctx
        assert "How well do you know this repo?" not in ctx

    def test_summarize_prompt_overrides_menu(self, tmp_repo):
        """'summarize this repo' was an earlier misfiring report — must answer, not menu."""
        result = store.get_bootstrap_context_prompt(tmp_repo, "summarize this repo")
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "OVERRIDE" in ctx and "Answer it" in ctx
        assert "How well do you know this repo?" not in ctx

    def test_task_prompt_gets_the_menu(self, tmp_repo):
        result = store.get_bootstrap_context_prompt(tmp_repo, "fix the login bug")
        assert "OVERRIDE" not in result["hookSpecificOutput"]["additionalContext"]

    def test_newcomer_prompt_in_own_repo_answers_as_maintainer(self, git_repo):
        """A repo author asking what the repo does is answered (not menued) — as a
        maintainer, not quizzed. The commit signal tunes phrasing, never blocks the answer."""
        _git_commit(git_repo, ME, 5)
        _set_me(git_repo)
        ctx = store.get_bootstrap_context_prompt(git_repo, "what is repo doing?")[
            "hookSpecificOutput"]["additionalContext"]
        assert "OVERRIDE" in ctx and "Answer it" in ctx
        assert "fellow maintainer" in ctx
        assert "How well do you know this repo?" not in ctx

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

    def test_long_pasted_prompt_is_not_captured(self, tmp_repo):
        # A long pasted blob containing a directive word is not a clean directive —
        # it must not be auto-stored as a constraint.
        long_prompt = "always " + "x" * 700
        eid, content = store.capture_user_constraint(tmp_repo, long_prompt, SESSION)
        assert eid is None
        assert store._load(tmp_repo)["entries"] == []


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


# ── 6b. Pattern promotion ─────────────────────────────────────────────────────

class TestPatternPromotion:
    def test_near_duplicate_increments_occurrence_count(self, tmp_repo):
        store.update_decision(tmp_repo, "Use FastAPI for HTTP routing", SESSION, "architecture")
        store.update_decision(tmp_repo, "FastAPI used for HTTP routing", SESSION, "architecture")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        assert entry.get("occurrence_count", 1) == 2

    def test_architecture_not_auto_promoted_on_recurrence(self, tmp_repo):
        """Recurrence bumps the count but must NOT change the subtype: a restated
        architecture decision is still that one decision, not a reusable pattern.
        Category is a semantic judgment made at capture, never inferred from a count."""
        store.update_decision(tmp_repo, "Use FastAPI for HTTP routing", SESSION, "architecture")
        store.update_decision(tmp_repo, "FastAPI used for HTTP routing", SESSION, "architecture")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        assert entry["subtype"] == "architecture"
        assert entry.get("occurrence_count") == 2

    def test_convention_near_duplicate_does_not_promote(self, tmp_repo):
        store.update_decision(tmp_repo, "Use conventional commits for all merges", SESSION, "convention")
        store.update_decision(tmp_repo, "Conventional commits used for all merges", SESSION, "convention")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        assert entry["subtype"] == "convention"

    def test_constraint_near_duplicate_does_not_promote(self, tmp_repo):
        store.update_decision(tmp_repo, "Never commit secrets to the repository", SESSION, "constraint")
        store.update_decision(tmp_repo, "Never commit secrets to the repo", SESSION, "constraint")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        assert entry["subtype"] == "constraint"

    def test_new_entry_has_occurrence_count_one(self, tmp_repo):
        _, eid = store.update_decision(tmp_repo, "Use Postgres for persistence", SESSION, "architecture")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["id"] == eid)
        assert entry.get("occurrence_count") == 1

    def test_legacy_entry_without_field_treated_as_count_one(self, tmp_repo):
        """Entries written before this change lack occurrence_count — must behave as count=1."""
        store.update_decision(tmp_repo, "Use Redis for caching decisions", SESSION, "architecture")
        data = store._load(tmp_repo)
        data["entries"][0].pop("occurrence_count", None)
        store._save(tmp_repo, data)
        store.update_decision(tmp_repo, "Redis used for caching decisions", SESSION, "architecture")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        assert entry.get("occurrence_count") == 2

    def test_explicit_pattern_appears_in_session_start_preload(self, tmp_repo):
        """A decision explicitly captured as a pattern is pre-loaded inline at SessionStart."""
        store.update_decision(tmp_repo, "Validate at the route boundary across all endpoints", SESSION, "pattern")
        result = store.get_session_start_context(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "route boundary" in ctx

    def test_within_session_repeat_does_not_promote(self, tmp_repo):
        """Two near-duplicate calls in one session bump the count but must NOT promote:
        restating a decision in the same conversation is repetition, not reuse."""
        store.update_decision(tmp_repo, "Validate inputs at HTTP boundary with Pydantic", SESSION, "architecture")
        store.update_decision(tmp_repo, "Validate inputs at the HTTP boundary using Pydantic", SESSION, "architecture")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        assert entry["subtype"] == "architecture"
        assert entry.get("occurrence_count") == 2

    def test_bootstrap_scan_produces_pattern_gap_for_web_framework_repo(self, tmp_repo):
        Path(tmp_repo).mkdir()
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "api"\ndependencies = ["fastapi", "boto3", "stripe"]\n'
        )
        result = store.bootstrap_scan(tmp_repo, insight="high")
        pattern_gaps = [g for g in result["gaps"] if g["subtype"] == "pattern"]
        assert pattern_gaps, "high-insight web repo should produce at least one pattern gap"

    def test_distinct_session_ids_tracked(self, tmp_repo):
        """Each hit from a new session is recorded; same session is not double-listed."""
        store.update_decision(tmp_repo, "Use FastAPI for HTTP routing", "s1", "architecture")
        store.update_decision(tmp_repo, "FastAPI used for HTTP routing", "s1", "architecture")
        store.update_decision(tmp_repo, "Using FastAPI for HTTP routing", "s2", "architecture")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        assert set(entry["session_ids"]) == {"s1", "s2"}
        assert entry["occurrence_count"] == 3

    def test_cross_session_rediscovery_does_not_promote(self, tmp_repo):
        """Independent rediscovery across sessions is tracked (count + session_ids) but
        does not change the subtype — recurrence cannot distinguish a reused approach
        from the same fact restated, so it never auto-promotes."""
        store.update_decision(tmp_repo, "Use Postgres for the persistence layer", "s1", "architecture")
        store.update_decision(tmp_repo, "Postgres used for the persistence layer", "s2", "architecture")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        assert entry["subtype"] == "architecture"
        assert set(entry["session_ids"]) == {"s1", "s2"}

    def test_legacy_entry_session_id_seeds_distinct_set(self, tmp_repo):
        """Legacy entries with only `session_id` contribute that id to the distinct set."""
        store.update_decision(tmp_repo, "Use Redis for caching", "s1", "architecture")
        data = store._load(tmp_repo)
        data["entries"][0].pop("session_ids", None)  # simulate pre-change entry
        store._save(tmp_repo, data)
        store.update_decision(tmp_repo, "Redis used for caching", "s2", "architecture")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        assert set(entry["session_ids"]) == {"s1", "s2"}

    def test_recurrence_never_changes_subtype(self, tmp_repo):
        """No subtype is ever flipped by recurrence — a near-duplicate only bumps the
        count on whichever entry it matches first, regardless of that entry's subtype."""
        store.update_decision(tmp_repo, "Always validate requests at the API boundary", SESSION, "constraint")
        store.update_decision(tmp_repo, "Always validate requests at the API boundary please", SESSION, "constraint")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["content"].startswith("Always"))
        assert entry["subtype"] == "constraint"
        assert entry.get("occurrence_count") == 2

    def test_deferred_breakdown_excludes_preloaded_patterns(self, tmp_repo):
        """The 'load on demand' breakdown must count only architecture — patterns are
        pre-loaded inline, so claiming they are deferred would force a wasted get_context."""
        store.update_decision(tmp_repo, "Validate at the route boundary across all endpoints", SESSION, "pattern")
        store.update_decision(tmp_repo, "Use Postgres for persistence", SESSION, "architecture")
        ctx = store.get_session_start_context(tmp_repo)["hookSpecificOutput"]["additionalContext"]
        assert "1 decision(s) stored (1 architecture)" in ctx
        assert "pattern)" not in ctx  # no deferred-pattern claim

    def test_preloaded_patterns_counted_in_summary(self, tmp_repo):
        store.update_decision(tmp_repo, "Validate at the route boundary across all endpoints", SESSION, "pattern")
        summary = store.get_session_start_context(tmp_repo)["systemMessage"]
        assert "1 pattern loaded" in summary

    # ── recurrence counter use-cases (#1 truncation, #2 eviction, #3 inline ×N) ──

    def test_recurrence_marker_shown_in_get_context(self, tmp_repo):
        """A decision seen more than once is annotated with ×N in get_context output."""
        store.update_decision(tmp_repo, "Use FastAPI for HTTP routing", SESSION, "architecture")
        store.update_decision(tmp_repo, "FastAPI used for HTTP routing", SESSION, "architecture")
        out = store.get_context(tmp_repo)
        assert "×2" in out

    def test_no_recurrence_marker_for_one_off(self, tmp_repo):
        store.update_decision(tmp_repo, "Use Postgres for persistence", SESSION, "architecture")
        out = store.get_context(tmp_repo)
        assert "×" not in out

    def test_recurrence_marker_in_session_start_preload(self, tmp_repo):
        # Patterns are pre-loaded inline, so a recurring pattern shows its ×N marker there.
        store.update_decision(tmp_repo, "Validate at the route boundary across all endpoints", SESSION, "pattern")
        store.update_decision(tmp_repo, "Validate at the route boundary for all endpoints", SESSION, "pattern")  # ×2
        ctx = store.get_session_start_context(tmp_repo)["hookSpecificOutput"]["additionalContext"]
        assert "×2" in ctx

    def test_truncation_keeps_high_recurrence_over_recent(self, tmp_repo):
        """When more decisions exist than the display cap, the recurring one survives
        even if older — it is not pushed out by newer one-off decisions."""
        # One recurring decision (count 2), stored first so it is the OLDEST.
        store.update_decision(tmp_repo, "Use FastAPI for HTTP routing", SESSION, "architecture")
        store.update_decision(tmp_repo, "FastAPI used for HTTP routing", SESSION, "architecture")
        # Fill past the unfiltered display cap with newer one-off decisions.
        for i in range(store._UNFILTERED_DISPLAY + 3):
            store.update_decision(tmp_repo, f"Unique one-off decision number {i} stands alone", SESSION, "architecture")
        out = store.get_context(tmp_repo)  # unfiltered → capped at _UNFILTERED_DISPLAY
        assert "FastAPI" in out, "recurring decision should survive truncation despite being oldest"

    def test_eviction_keeps_high_recurrence_at_capacity(self, tmp_repo, monkeypatch):
        """At MAX_ENTRIES, a frequently-recurring decision is retained even when oldest."""
        monkeypatch.setattr(store, "MAX_ENTRIES", 5)
        store.update_decision(tmp_repo, "Use FastAPI for HTTP routing", SESSION, "architecture")
        store.update_decision(tmp_repo, "FastAPI used for HTTP routing", SESSION, "architecture")  # count 2, oldest
        # Mutually-distinct one-off decisions so each is stored separately (no near-dup collapse).
        fillers = [
            "Adopt GraphQL for the public client API surface",
            "Stream domain events through Kafka topics",
            "Persist relational data in CockroachDB clusters",
            "Render the marketing site with Astro islands",
            "Queue background jobs via Celery workers",
            "Cache hot keys inside a Redis sidecar",
            "Ship logs to Loki with Promtail agents",
            "Authenticate users through Auth0 tenants",
        ]
        for f in fillers:
            store.update_decision(tmp_repo, f, SESSION, "architecture")
        data = store._load(tmp_repo)
        assert len(data["entries"]) == 5
        assert any("FastAPI" in e["content"] for e in data["entries"]), "recurring decision must not be evicted"

    def test_keep_top_noop_under_limit(self, tmp_repo):
        """Below the cap, _keep_top returns the list unchanged (same object, same order)."""
        items = [{"timestamp": "2026-01-01", "occurrence_count": 1}]
        assert store._keep_top(items, 5) is items

    # ── review-round fixes ───────────────────────────────────────────────────

    def test_empty_token_content_not_stored(self, tmp_repo):
        """Punctuation/whitespace-only content tokenizes to empty and must be rejected,
        not stored as a blank decision (regression guard for the _find_match refactor)."""
        stored, eid = store.update_decision(tmp_repo, "!!! ...", SESSION, "architecture")
        assert stored is False and eid is None
        data = store._load(tmp_repo)
        assert not [e for e in data["entries"] if e["type"] == "decision"]

    def test_empty_token_global_content_not_stored(self, tmp_repo):
        stored, _ = store.update_global_decision("???", SESSION, "constraint")
        assert stored is False

    def test_pinned_new_entry_survives_eviction_at_capacity(self, tmp_repo, monkeypatch):
        """A freshly written count-1 decision must persist even when the store is full of
        higher-count entries — otherwise update_decision reports success for a lost write."""
        monkeypatch.setattr(store, "MAX_ENTRIES", 3)
        # Fill the cap with recurring (count-2) decisions.
        recurring = [
            ("Adopt GraphQL for the public client API surface", "Adopt GraphQL for the public client API layer"),
            ("Stream domain events through Kafka topics here", "Stream domain events through Kafka topics now"),
            ("Persist relational data in CockroachDB clusters today", "Persist relational data in CockroachDB clusters now"),
        ]
        for a, b in recurring:
            store.update_decision(tmp_repo, a, SESSION, "architecture")
            store.update_decision(tmp_repo, b, SESSION, "architecture")  # → count 2
        stored, eid = store.update_decision(tmp_repo, "Brand new one-off decision lands last here", SESSION, "architecture")
        assert stored is True
        data = store._load(tmp_repo)
        assert any(e["id"] == eid for e in data["entries"]), "pinned new entry must not be evicted"

    def test_constraint_hook_near_dup_does_not_write_or_promote(self, tmp_repo):
        """capture_user_constraint is a silent no-op on a near-duplicate: no disk write,
        and it must never promote an architecture entry from a constraint phrasing."""
        store.update_decision(tmp_repo, "Validate requests at the API boundary with Pydantic", SESSION, "architecture")
        before = (store._store_path(tmp_repo)).stat().st_mtime_ns
        eid, _ = store.capture_user_constraint(tmp_repo, "always validate requests at the API boundary", SESSION)
        assert eid is None
        data = store._load(tmp_repo)
        arch = next(e for e in data["entries"] if "Pydantic" in e["content"])
        assert arch["subtype"] == "architecture", "constraint hook must not promote architecture"
        assert arch.get("occurrence_count", 1) == 1, "constraint hook must not bump the count"
        assert (store._store_path(tmp_repo)).stat().st_mtime_ns == before, "no write on near-dup"


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

    def test_bootstrap_command_removed(self, tmp_home):
        cli.install()
        cli.uninstall()
        assert not (tmp_home / ".claude" / "commands" / "bootstrap.md").exists()

    def test_foreign_bootstrap_command_survives_uninstall(self, tmp_home):
        cmd_path = tmp_home / ".claude" / "commands" / "bootstrap.md"
        cmd_path.parent.mkdir(parents=True)
        cmd_path.write_text("my own custom bootstrap command")
        cli.install()   # leaves the foreign file alone
        cli.uninstall()
        assert cmd_path.read_text() == "my own custom bootstrap command"


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


# ── 13. Upgrade from a legacy (pre-CLI) install ───────────────────────────────

# Verbatim shape of what the June-2026 from-source installer wrote into the REPO's
# .claude/settings.json (see git history: bd4d178). Upgrading users reported two
# symptoms from this exact file: "Unknown tool: capture_context" on every prompt
# (the tool was removed in #58) and a second, contradictory "no context stored yet"
# startup message from the dead-clone SessionStart hook.
def _legacy_repo_settings(clone: str, repo: str) -> dict:
    return {
        "hooks": {
            "SessionStart": [{"hooks": [{
                "type": "command",
                "command": (f"uv run --directory {clone} python -c \"import sys,json; "
                            f"sys.path.insert(0,'{clone}'); import store; "
                            f"print(json.dumps(store.get_session_start_context('{repo}')))\""),
                "statusMessage": "Loading session context..."}]}],
            "PreCompact": [{"hooks": [{
                "type": "command",
                "command": ("echo '{\"systemMessage\": \"Contexer: context compaction "
                            "starting — call update_context for any decisions not yet stored\"}'"),
                "statusMessage": "Saving decisions before compact..."}]}],
            "PostCompact": [{"hooks": [{
                "type": "command",
                "command": (f"uv run --directory {clone} python -c \"import sys,json; "
                            f"sys.path.insert(0,'{clone}'); import store; "
                            "msg='Contexer: no context stored'; "
                            "print(json.dumps({'systemMessage':msg}))\""),
                "statusMessage": "Reloading context after compact..."}]}],
            "UserPromptSubmit": [
                {"hooks": [{
                    "type": "mcp_tool", "server": "contexer", "tool": "capture_context",
                    "input": {"repo_path": repo, "description": "${prompt}"},
                    "once": True, "statusMessage": "Capturing task..."}]},
                {"hooks": [{
                    "type": "command",
                    "command": ("echo '{\"hookSpecificOutput\": {\"hookEventName\": "
                                "\"UserPromptSubmit\", \"additionalContext\": \"Reminder: if you "
                                "make a significant decision, establish a pattern, or document a "
                                "constraint this turn, call update_context.\"}}'"),
                    "statusMessage": "Loading context reminder..."}]},
            ],
        },
    }


class TestUpgradeFromLegacyInstall:
    """Full upgrading-user journey: legacy repo-level hooks + legacy home hooks →
    upgrade → exactly ONE contexer SessionStart hook remains anywhere, no reference
    to the removed capture_context tool survives, and the REAL installed hook
    command (run through a shell, as Claude Code runs it) emits exactly one
    systemMessage and self-heals the repo file."""

    @pytest.fixture
    def legacy_user(self, tmp_home, monkeypatch):
        """A user machine mid-upgrade: git repo with legacy repo-level hooks, plus a
        legacy (be12ecd-era) home settings.json with the old global hooks."""
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
        repo = tmp_home / "work" / "myproject"
        repo.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        (repo / ".claude").mkdir()
        (repo / ".claude" / "settings.json").write_text(
            json.dumps(_legacy_repo_settings("/dead/clone", str(repo)), indent=2))
        # Legacy home hooks (global era, still pre-CLI): old ss hook, mcp_tool
        # capture hook, reminder echo, stale allow entry.
        home_settings = {
            "hooks": {
                "SessionStart": [{"hooks": [{
                    "type": "command",
                    "command": ("REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd) && "
                                "uv run --directory /dead/clone python -c \"import store; "
                                "print(store.get_session_start_context(''))\" \"$REPO\"")}]}],
                "UserPromptSubmit": [
                    {"hooks": [{"type": "mcp_tool", "server": "contexer",
                                "tool": "capture_context", "once": True}]},
                    {"hooks": [{"type": "command", "command":
                        "echo '... Reminder: if you make a significant decision, establish a "
                        "pattern, or document a constraint this turn, call update_context.'"}]},
                ],
            },
            "permissions": {"allow": ["mcp__contexer__capture_context"]},
        }
        (tmp_home / ".claude" / "settings.json").write_text(json.dumps(home_settings))
        return repo

    def _contexer_session_start_groups(self, tmp_home, repo) -> list:
        """All SessionStart hook groups Claude Code would fire for this repo (home +
        repo-level settings) that belong to contexer."""
        groups = []
        for p in (tmp_home / ".claude" / "settings.json",
                  repo / ".claude" / "settings.json"):
            if p.exists():
                groups += json.loads(p.read_text()).get("hooks", {}).get("SessionStart", [])
        return [g for g in groups if any(
            "contexer" in str(h).lower() or "store.get_session_start_context" in str(h)
            for h in g.get("hooks", []))]

    def test_upgrade_leaves_exactly_one_session_start_hook(self, legacy_user, tmp_home, monkeypatch):
        repo = legacy_user
        monkeypatch.chdir(repo)   # user runs `contexer install` from their project
        cli.install()
        assert len(self._contexer_session_start_groups(tmp_home, repo)) == 1, \
            "exactly one contexer SessionStart hook may remain — one startup message"

    def test_upgrade_removes_every_capture_context_reference(self, legacy_user, tmp_home, monkeypatch):
        repo = legacy_user
        monkeypatch.chdir(repo)
        cli.install()
        for p in (tmp_home / ".claude" / "settings.json",
                  repo / ".claude" / "settings.json"):
            assert "capture_context" not in p.read_text(), \
                f"{p} still references the removed capture_context tool"

    def test_upgrade_cleans_repo_settings_and_logs_it(self, legacy_user, tmp_home, monkeypatch):
        from contexer.adapters import claude as claude_adapter
        repo = legacy_user
        monkeypatch.chdir(repo)
        log = "\n".join(claude_adapter.install(tmp_home))
        assert "Removed legacy Contexer hooks" in log
        hooks = json.loads((repo / ".claude" / "settings.json").read_text()).get("hooks", {})
        assert hooks == {}, "every legacy repo-level hook group was contexer's — all must go"

    def test_uninstall_also_cleans_repo_settings(self, legacy_user, tmp_home, monkeypatch):
        repo = legacy_user
        monkeypatch.chdir(repo)
        cli.install()
        # Re-seed the legacy file to prove uninstall cleans it independently.
        (repo / ".claude" / "settings.json").write_text(
            json.dumps(_legacy_repo_settings("/dead/clone", str(repo))))
        cli.uninstall()
        assert "capture_context" not in (repo / ".claude" / "settings.json").read_text()

    def test_stale_permission_pruned_on_upgrade(self, legacy_user, tmp_home, monkeypatch):
        monkeypatch.chdir(legacy_user)
        cli.install()
        allow = json.loads((tmp_home / ".claude" / "settings.json").read_text())["permissions"]["allow"]
        assert "mcp__contexer__capture_context" not in allow
        assert "mcp__contexer__update_context" in allow

    def _run_installed_session_start_hook(self, tmp_home, repo) -> dict:
        """Execute the installed SessionStart hook command exactly as Claude Code does:
        through a shell, cwd = the repo, stdin = the hook event JSON."""
        settings = json.loads((tmp_home / ".claude" / "settings.json").read_text())
        cmds = [h["command"] for g in settings["hooks"]["SessionStart"]
                for h in g["hooks"] if h.get("type") == "command"]
        assert len(cmds) == 1
        env = dict(os.environ, HOME=str(tmp_home))
        proc = subprocess.run(
            ["bash", "-c", cmds[0]], cwd=repo, env=env, input='{"source": "startup"}',
            capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    def test_real_hook_run_emits_one_message_and_heals_repo(self, legacy_user, tmp_home, monkeypatch):
        """Package-upgrade-only path (user upgrades the package but never re-runs
        `contexer install` from the repo): the modern home hook alone, executed for
        real through bash, must emit exactly one systemMessage AND strip the legacy
        repo-level hooks via the sync_memory self-heal — so the session after next
        is down to one startup message with no reinstall."""
        repo = legacy_user
        monkeypatch.chdir(tmp_home)      # install runs elsewhere: repo file NOT cleaned here
        cli.install()
        assert "capture_context" in (repo / ".claude" / "settings.json").read_text(), \
            "precondition: legacy repo hooks still present before the session"
        out = self._run_installed_session_start_hook(tmp_home, repo)
        assert isinstance(out.get("systemMessage"), str) and out["systemMessage"].count("Contexer:") == 1
        healed = (repo / ".claude" / "settings.json").read_text()
        assert "capture_context" not in healed, "hook run must self-heal the repo settings"
        assert len(self._contexer_session_start_groups(tmp_home, repo)) == 1

    def test_second_session_stays_clean(self, legacy_user, tmp_home, monkeypatch):
        repo = legacy_user
        monkeypatch.chdir(tmp_home)
        cli.install()
        self._run_installed_session_start_hook(tmp_home, repo)   # session 1: heals
        before = (repo / ".claude" / "settings.json").read_text()
        out = self._run_installed_session_start_hook(tmp_home, repo)  # session 2: clean
        assert out["systemMessage"].count("Contexer:") == 1
        assert (repo / ".claude" / "settings.json").read_text() == before, \
            "healing must be idempotent — no churn on an already-clean file"

    def test_home_git_repo_never_cleans_global_settings(self, tmp_home, monkeypatch):
        """Dotfiles-in-home: HOME itself is a git repo, and the user runs
        `contexer install` from ~. The cwd's git root then IS the home dir, and
        ~/.claude/settings.json is the GLOBAL config whose freshly-written modern
        hooks contain the legacy markers — the cleanup must refuse to touch it
        (Greptile P1, PR #96: without the _is_sane_repo guard, install stripped
        the very hooks it had just written)."""
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
        subprocess.run(["git", "init", "-q"], cwd=tmp_home, check=True)
        monkeypatch.chdir(tmp_home)
        cli.install()
        hooks = json.loads((tmp_home / ".claude" / "settings.json").read_text())["hooks"]
        assert "SessionStart" in hooks, "global SessionStart hook must survive install from ~"
        assert "PreCompact" in hooks
        cli.uninstall()   # same guard on the uninstall path — must not raise

    def test_install_log_surfaces_stale_plugin_warning(self, tmp_home):
        from contexer.adapters import claude as claude_adapter
        cache = tmp_home / ".claude" / "plugins" / "cache" / "mp" / "contexer" / "0.1.0"
        (cache / "hooks").mkdir(parents=True)
        (cache / "hooks" / "hooks.json").write_text(json.dumps({"hooks": {
            "UserPromptSubmit": [{"hooks": [{"type": "mcp_tool", "server": "contexer",
                                             "tool": "capture_context"}]}]}}))
        (tmp_home / ".claude" / "plugins" / "installed_plugins.json").write_text(json.dumps({
            "version": 2, "plugins": {"contexer@mp": [{"installPath": str(cache)}]}}))
        log = "\n".join(claude_adapter.install(tmp_home))
        assert "claude plugin update contexer" in log, \
            "install must tell the user their plugin still ships the removed hook"


# ── 15. Non-git project directories ───────────────────────────────────────────

class TestNonGitProjectDir:
    """Stores are keyed by absolute path - .git is NOT required (the MCP tools accept
    any sane directory, and real users have large stores for non-git dirs). Reported
    against 0.16.1: in such a dir, SessionStart said "no context stored - setup offer"
    while the per-prompt MCP tools found the store fine. The hook's git-root resolution
    came up empty and nothing fell back to the hook's own cwd (hosts run hooks with
    cwd = the project dir). These tests run the REAL installed hook commands through
    bash from a non-git project dir."""

    @pytest.fixture
    def non_git_project(self, tmp_home, monkeypatch):
        monkeypatch.setattr(store, "STORE_DIR", tmp_home / ".contexer")
        proj = tmp_home / "work" / "dashboards"
        proj.mkdir(parents=True)   # deliberately NOT a git repo
        store.update_decision(str(proj), "use terraform for dashboard provisioning",
                              "s1", subtype="architecture")
        store.update_decision(str(proj), "never hardcode dynatrace api tokens",
                              "s1", subtype="constraint", created_by="human")
        return proj

    def _hook_cmds(self, tmp_home, event, marker):
        settings = json.loads((tmp_home / ".claude" / "settings.json").read_text())
        return [h["command"] for g in settings["hooks"][event]
                for h in g["hooks"] if marker in h.get("command", "")]

    def _run(self, tmp_home, cwd, command, stdin):
        env = dict(os.environ, HOME=str(tmp_home))
        proc = subprocess.run(["bash", "-c", command], cwd=cwd, env=env, input=stdin,
                              capture_output=True, text=True, timeout=60)
        assert proc.returncode == 0, proc.stderr
        return json.loads(proc.stdout)

    def test_session_start_loads_existing_context_without_git(self, non_git_project, tmp_home):
        cli.install()
        cmds = self._hook_cmds(tmp_home, "SessionStart", "get_session_start_context")
        assert len(cmds) == 1
        out = self._run(tmp_home, non_git_project, cmds[0], '{"source": "startup"}')
        msg = out.get("systemMessage", "")
        assert "no context stored" not in msg, \
            f"false 'no context' banner in a non-git dir with an existing store: {msg}"
        assert "loaded" in msg
        ctx = out["hookSpecificOutput"]["additionalContext"]
        # Constraints are injected eagerly; architecture is deferred behind a count
        # pointer by design - assert one of each.
        assert "never hardcode dynatrace api tokens" in ctx.lower()
        assert "1 architecture" in ctx

    def test_bootstrap_hook_does_not_offer_setup_without_git(self, non_git_project, tmp_home):
        cli.install()
        cmds = self._hook_cmds(tmp_home, "UserPromptSubmit", "get_bootstrap_context_prompt")
        assert len(cmds) == 1
        out = self._run(tmp_home, non_git_project, cmds[0], '{"prompt": "hello"}')
        assert out == {}, f"bootstrap must not offer setup when context exists: {out}"

    def test_per_prompt_hooks_resolve_via_cwd_without_git(self, non_git_project, tmp_home,
                                                          monkeypatch):
        # capture_constraint and rationale run with repo="" in a non-git dir; they must
        # resolve the hook's own cwd, not depend on a (possibly stale) shared pointer.
        from contexer.adapters import claude as claude_adapter
        (tmp_home / ".contexer" / ".current_repo").write_text(str(tmp_home / "other-repo"))
        monkeypatch.chdir(non_git_project)
        out = claude_adapter.rationale(
            "", json.dumps({"prompt": "why terraform for provisioning?"}))
        assert "terraform" in json.loads(out).get(
            "hookSpecificOutput", {}).get("additionalContext", "").lower()

    def test_session_start_fallback_anchors_pointer(self, non_git_project, tmp_home, monkeypatch):
        monkeypatch.chdir(non_git_project)
        store.session_start_payload("")
        assert (tmp_home / ".contexer" / ".current_repo").read_text() == str(non_git_project)

    def test_home_dir_cwd_never_selected(self, tmp_home, monkeypatch):
        # A session opened in the home directory must NOT select a home-dir store -
        # the fallback refuses insane dirs and the normal no-context path applies.
        monkeypatch.setattr(store, "STORE_DIR", tmp_home / ".contexer")
        monkeypatch.chdir(tmp_home)
        payload = store.session_start_payload("")
        assert "no context stored" in payload["status"]
        assert not (tmp_home / ".contexer" / ".current_repo").exists()

    def test_filesystem_root_is_not_a_sane_repo(self):
        assert store._is_sane_repo("/") is False
        assert store._is_sane_repo("//") is False
