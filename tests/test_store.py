"""Tests for core store.py logic — filtering, storage, context output, and bootstrap scan."""
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from contexer import store



# ── repo resolution: sanity guard + session-repo binding ──────────────────────

class TestRepoResolution:
    def test_config_dirs_rejected(self):
        home = Path.home()
        for bad in (str(home), str(home / ".claude"), str(home / ".cursor"),
                    str(home / ".codex"), str(home / ".gemini"),
                    str(home / ".contexer")):
            assert store._is_sane_repo(bad) is False, bad

    def test_real_repo_accepted(self, tmp_path):
        assert store._is_sane_repo(str(tmp_path / "myproject")) is True

    def test_relative_and_empty_rejected(self):
        assert store._is_sane_repo("") is False
        assert store._is_sane_repo("relative/path") is False

    def test_explicit_repo_wins(self, tmp_repo):
        assert store._resolve_repo(tmp_repo) == tmp_repo

    def test_explicit_config_dir_never_honored(self, tmp_path, monkeypatch):
        # A caller passing ~/.claude must NOT resolve to it — falls back to safe sources.
        monkeypatch.setattr(store, "_SESSION_REPO", "")
        monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
        assert store._resolve_repo(str(Path.home() / ".claude")) == ""

    def test_session_repo_preferred_over_pointer(self, tmp_path, monkeypatch):
        # The clobber scenario: pointer poisoned to ~/.claude, but the server is bound to
        # its own cwd repo — decisions must resolve to the real project, not the config dir.
        monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
        store.STORE_DIR.mkdir()
        (store.STORE_DIR / ".current_repo").write_text(str(Path.home() / ".claude"))
        monkeypatch.setattr(store, "_SESSION_REPO", str(tmp_path / "realproject"))
        assert store._resolve_repo("") == str(tmp_path / "realproject")

    def test_poisoned_pointer_read_returns_empty(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
        store.STORE_DIR.mkdir()
        (store.STORE_DIR / ".current_repo").write_text(str(Path.home() / ".cursor"))
        assert store._current_repo_path() == ""

    def test_set_session_repo_rejects_config_dir(self, monkeypatch):
        monkeypatch.setattr(store, "_SESSION_REPO", "")
        store.set_session_repo(str(Path.home() / ".claude"))
        assert store._SESSION_REPO == ""
        store.set_session_repo("")  # reset


# ── _overlap_ratio ────────────────────────────────────────────────────────────
# The novelty filter's duplicate metric, extracted for reuse by team-context dedup.
# These pin it to the exact behavior of the old inline `len(a & b) / max(|a|, |b|)`.

class TestOverlapRatio:
    def test_identical_sets_is_one(self):
        assert store._overlap_ratio({"a", "b", "c"}, {"a", "b", "c"}) == 1.0

    def test_disjoint_sets_is_zero(self):
        assert store._overlap_ratio({"a", "b"}, {"c", "d"}) == 0.0

    def test_empty_side_is_zero(self):
        assert store._overlap_ratio(set(), {"a"}) == 0.0
        assert store._overlap_ratio({"a"}, set()) == 0.0

    def test_ratio_divides_by_larger_set(self):
        # |{a,b}| shared / max(2, 4) = 2 / 4
        assert store._overlap_ratio({"a", "b"}, {"a", "b", "c", "d"}) == 0.5

    def test_matches_old_inline_formula(self):
        # The pre-extraction expression, computed independently for a spread of pairs.
        pairs = [
            ({"x", "y", "z"}, {"x", "y"}),
            ({"one", "two", "three", "four"}, {"three", "four", "five"}),
            ({"solo"}, {"solo"}),
            ({"a", "b", "c", "d", "e"}, {"a"}),
        ]
        for a, b in pairs:
            hi = a if len(a) > len(b) else b
            expected = len(a & b) / len(hi)
            assert store._overlap_ratio(a, b) == expected


# ── _is_novel ─────────────────────────────────────────────────────────────────

class TestIsNovel:
    def test_novel_against_empty(self):
        assert store._is_novel("use postgres for persistence", []) is True

    def test_exact_duplicate_rejected(self):
        existing = [{"content": "use postgres for persistence"}]
        assert store._is_novel("use postgres for persistence", existing) is False

    def test_high_overlap_rejected(self):
        existing = [{"content": "decided to use postgres for primary persistence layer"}]
        assert store._is_novel("decided to use postgres for primary persistence layer today", existing) is False

    def test_different_content_passes(self):
        existing = [{"content": "use postgres for persistence"}]
        assert store._is_novel("authentication uses JWT tokens for stateless sessions", existing) is True


# ── _passes_filter ────────────────────────────────────────────────────────────

class TestPassesFilter:
    def test_novel_content_passes(self):
        assert store._passes_filter("decided to use FastMCP over raw server API", []) is True

    def test_novel_content_without_signals_passes(self):
        # novelty is the gate — update_context is only called for significant content
        assert store._passes_filter("fastmcp handles routing and tool listing automatically", []) is True

    def test_duplicate_rejected(self):
        existing = [{"type": "decision", "content": "fastmcp handles routing and tool listing automatically"}]
        assert store._passes_filter("fastmcp handles routing and tool listing automatically", existing) is False

    def test_duplicate_with_signals_still_rejected(self):
        # signal keywords do NOT override the novelty veto
        existing = [{"type": "decision", "content": "decided to use jwt instead of sessions for stateless auth"}]
        assert store._passes_filter("decided to use jwt instead of sessions for stateless auth", existing) is False

    def test_novelty_ignores_task_entries(self):
        # task entries must NOT trigger the duplicate veto for decisions
        tasks = [{"type": "task", "content": "add jwt authentication to the api endpoints"}]
        assert store._passes_filter("decided to use jwt for authentication — stateless and scalable", tasks) is True


# ── update_decision ───────────────────────────────────────────────────────────

class TestUpdateDecision:
    def test_stores_decision(self, tmp_repo):
        stored, entry_id = store.update_decision(
            tmp_repo, "decided to use postgres over sqlite — needs concurrent writes", "sess-1"
        )
        assert stored is True
        assert entry_id is not None

    def test_filters_duplicate(self, populated_repo):
        stored, _ = store.update_decision(
            populated_repo, "decided to use JWT instead of sessions — stateless, easier to scale", "sess-2"
        )
        assert stored is False

    def test_subtype_stored_in_entry(self, tmp_repo):
        store.update_decision(tmp_repo, "constraint: never store plaintext passwords in any log", "s1", subtype="constraint")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        assert entry["subtype"] == "constraint"

    def test_subtype_defaults_to_empty_string(self, tmp_repo):
        store.update_decision(tmp_repo, "decided to use postgres for primary storage layer", "s1")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        assert entry["subtype"] == ""

    def test_cap_enforced(self, tmp_repo):
        distinct = [
            "decided to use postgresql for primary relational data storage layer",
            "chose redis for session caching and distributed rate limit tracking",
            "selected graphql for the client-facing api query interface",
            "picked typescript strict mode for all frontend component definitions",
            "went with docker compose for reproducible local development environments",
            "decided on github actions for continuous integration and deployment pipeline",
            "chose fastapi over flask for the async python backend service",
            "selected terraform for infrastructure provisioning and state management",
            "went with elasticsearch for full text search and log aggregation",
            "decided to use celery with rabbitmq for async background task processing",
            "chose prisma orm for type-safe database access in nodejs services",
            "selected nextjs app router for the customer-facing web application",
            "decided to use stripe webhooks for payment event processing integration",
            "went with datadog apm for application performance monitoring and tracing",
            "chose sentry for error tracking and on-call alerting in production",
        ]
        for i, content in enumerate(distinct):
            store.update_decision(tmp_repo, content, f"sess-{i}")
        data = store._load(tmp_repo)
        assert len(data["entries"]) <= store.MAX_ENTRIES
        assert len(data["entries"]) == 15  # well under cap, all stored


# ── get_context ───────────────────────────────────────────────────────────────

class TestGetContext:
    def test_empty_repo_message(self, tmp_repo):
        result = store.get_context(tmp_repo)
        assert "No context stored" in result

    def test_includes_decisions(self, populated_repo):
        result = store.get_context(populated_repo)
        assert "JWT" in result
        assert "bcrypt" in result

    def test_query_filter_returns_matching_decisions(self, populated_repo):
        result = store.get_context(populated_repo, query="JWT")
        assert "JWT" in result
        assert "bcrypt" not in result

    def test_query_filter_case_insensitive(self, populated_repo):
        result = store.get_context(populated_repo, query="jwt")
        assert "JWT" in result

    def test_query_filter_no_match_returns_message(self, populated_repo):
        result = store.get_context(populated_repo, query="nonexistent_xyz_keyword")
        assert "No matching" in result

    def test_entry_type_filter_returns_matching_subtype(self, tmp_repo):
        store.update_decision(tmp_repo, "constraint: always use bcrypt for passwords", "s1", subtype="constraint")
        store.update_decision(tmp_repo, "architecture: layered domain model with services", "s1", subtype="architecture")
        result = store.get_context(tmp_repo, entry_type="constraint")
        assert "bcrypt" in result
        assert "layered" not in result

    def test_entry_type_filter_no_match_returns_message(self, populated_repo):
        # populated_repo entries have no subtype — filter should return no match
        result = store.get_context(populated_repo, entry_type="architecture")
        assert "No matching" in result

    def test_unfiltered_shows_last_10_by_default(self, tmp_repo):
        topics = [
            "decided to use postgres for primary storage instead of sqlite",
            "chose jwt over sessions because stateless auth scales horizontally",
            "constraint: bcrypt for passwords, never md5 or sha1 by design",
            "convention: all api handlers return typed response objects",
            "decided docker is the deployment target, not bare metal",
            "pattern: migrations run on startup in dev, manual in prod",
            "constraint: no third-party auth providers, self-hosted only",
            "decided graphql over rest for the client api layer",
            "convention: snake_case for db columns, camelCase for json",
            "constraint: all secrets via environment variables, never hardcoded",
            "decided redis for session cache, not in-process memory",
            "pattern: service layer always validates input before hitting db",
            "chose ruff over flake8 for speed and single-tool simplicity",
            "decided to use uv instead of pip for reproducible builds",
            "constraint: no orm, raw sql with typed wrappers only",
        ]
        for i, content in enumerate(topics):
            store.update_decision(tmp_repo, content, f"s{i}")
        result = store.get_context(tmp_repo)
        assert result.count("- [") == store._UNFILTERED_DISPLAY

    def test_filtered_shows_up_to_25_by_default(self, tmp_repo):
        constraints = [
            "constraint: bcrypt for password hashing, never md5 or sha1",
            "constraint: api responses must be typed using pydantic models",
            "constraint: database migrations run manually in production only",
            "constraint: all secrets stored in environment variables",
            "constraint: input validation at every api boundary before service layer",
            "constraint: no direct database access from route handlers",
            "constraint: logging must not include personally identifiable information",
            "constraint: external api calls wrapped in retry logic with backoff",
            "constraint: test coverage required for all repository and service functions",
            "constraint: feature flags for all new functionality in production",
            "constraint: database connection pooling configured per environment",
            "constraint: authentication tokens expire after 24 hours maximum",
            "constraint: rate limiting applied to all public endpoints at gateway",
            "constraint: background jobs must be idempotent for safe re-execution",
            "constraint: no business logic in database migration scripts",
            "constraint: ssl certificates renewed automatically via scheduled job",
            "constraint: docker images built from minimal base images for security",
            "constraint: all configuration loaded at startup not at request time",
            "constraint: json responses include api version header on all routes",
            "constraint: database indexes reviewed for every new query pattern",
            "constraint: error messages never expose internal stack traces to clients",
            "constraint: health check endpoints excluded from authentication",
            "constraint: all file uploads scanned for malware before processing",
            "constraint: pagination required for all list endpoints returning collections",
            "constraint: cache invalidation strategy documented per cached resource",
            "constraint: database transactions wrap all multi-step write operations",
            "constraint: api deprecation notices sent 90 days before removal",
            "constraint: audit log written for all writes on sensitive user data",
            "constraint: memory limits set on all containerized service deployments",
            "constraint: zero trust networking enforced between internal microservices",
        ]
        for i, content in enumerate(constraints):
            store.update_decision(tmp_repo, content, f"s{i}", subtype="constraint")
        result = store.get_context(tmp_repo, entry_type="constraint")
        assert result.count("- [") == store._FILTERED_DISPLAY

    def test_limit_param_overrides_auto(self, tmp_repo):
        distinct = [
            "decided to use postgresql for primary relational data storage",
            "chose redis for distributed session caching and rate limits",
            "selected graphql for the client-facing api query layer",
            "picked typescript strict mode for all frontend components",
            "went with docker compose for local development environments",
            "decided on github actions for continuous integration pipeline",
            "chose fastapi for the async python backend service layer",
            "selected terraform for infrastructure provisioning management",
            "went with elasticsearch for full text search functionality",
            "decided celery with rabbitmq for async background processing",
            "chose prisma for type-safe database access in node services",
            "selected nextjs app router for customer-facing web application",
            "decided to use stripe webhooks for payment event processing",
            "went with datadog for application performance monitoring",
            "chose sentry for error tracking and production alerting",
            "decided to use s3 for file storage and static asset serving",
            "selected cloudfront as cdn for static asset delivery globally",
            "went with route53 for dns management and health checks",
            "chose aurora rds for the managed relational database service",
            "decided to use ecr for container image storage and deployment",
        ]
        for i, content in enumerate(distinct):
            store.update_decision(tmp_repo, content, f"s{i}")
        result = store.get_context(tmp_repo, limit=5)
        assert result.count("- [") == 5

    def test_overflow_note_shown_when_results_truncated(self, tmp_repo):
        constraints = [
            "constraint: bcrypt for password hashing, never md5 or sha1",
            "constraint: api responses must be typed using pydantic models",
            "constraint: database migrations run manually in production only",
            "constraint: all secrets stored in environment variables",
            "constraint: input validation at every api boundary before service layer",
            "constraint: no direct database access from route handlers",
            "constraint: logging must not include personally identifiable information",
            "constraint: external api calls wrapped in retry logic with backoff",
            "constraint: test coverage required for all repository and service functions",
            "constraint: feature flags for all new functionality in production",
            "constraint: database connection pooling configured per environment",
            "constraint: authentication tokens expire after 24 hours maximum",
            "constraint: rate limiting applied to all public endpoints at gateway",
            "constraint: background jobs must be idempotent for safe re-execution",
            "constraint: no business logic in database migration scripts",
            "constraint: ssl certificates renewed automatically via scheduled job",
            "constraint: docker images built from minimal base images for security",
            "constraint: all configuration loaded at startup not at request time",
            "constraint: json responses include api version header on all routes",
            "constraint: database indexes reviewed for every new query pattern",
            "constraint: error messages never expose internal stack traces to clients",
            "constraint: health check endpoints excluded from authentication",
            "constraint: all file uploads scanned for malware before processing",
            "constraint: pagination required for all list endpoints returning collections",
            "constraint: cache invalidation strategy documented per cached resource",
            "constraint: database transactions wrap all multi-step write operations",
            "constraint: api deprecation notices sent 90 days before removal",
            "constraint: audit log written for all writes on sensitive user data",
            "constraint: memory limits set on all containerized service deployments",
            "constraint: zero trust networking enforced between internal microservices",
        ]
        for i, content in enumerate(constraints):
            store.update_decision(tmp_repo, content, f"s{i}", subtype="constraint")
        result = store.get_context(tmp_repo, entry_type="constraint")
        assert "showing" in result and "of 30" in result


# ── _resolve_repo ─────────────────────────────────────────────────────────────

class TestResolveRepo:
    def test_explicit_path_returned_unchanged(self, tmp_repo):
        assert store._resolve_repo(tmp_repo) == tmp_repo

    def test_empty_string_falls_back_to_current_repo_file(self, tmp_repo, monkeypatch):
        monkeypatch.setattr(store, "STORE_DIR", Path(tmp_repo).parent / ".contexer")
        store.STORE_DIR.mkdir(exist_ok=True)
        (store.STORE_DIR / ".current_repo").write_text(tmp_repo)
        assert store._resolve_repo("") == tmp_repo

    def test_empty_string_with_no_file_returns_empty(self, tmp_repo, monkeypatch):
        monkeypatch.setattr(store, "STORE_DIR", Path(tmp_repo).parent / ".contexer_empty")
        assert store._resolve_repo("") == ""

    def test_nonempty_path_bypasses_file(self, tmp_repo, monkeypatch):
        monkeypatch.setattr(store, "STORE_DIR", Path(tmp_repo).parent / ".contexer")
        store.STORE_DIR.mkdir(exist_ok=True)
        (store.STORE_DIR / ".current_repo").write_text("/some/other/repo")
        assert store._resolve_repo(tmp_repo) == tmp_repo


# ── get_session_start_context ─────────────────────────────────────────────────

class TestGetSessionStartContext:
    def test_empty_repo_offers_bootstrap(self, tmp_repo):
        result = store.get_session_start_context(tmp_repo)
        assert "bootstrap" in result["hookSpecificOutput"]["additionalContext"].lower()
        assert "no context stored" in result["systemMessage"].lower()

    def test_empty_repo_directive_stops_and_waits(self, tmp_repo):
        # Bootstrap offer must pause Claude — it waits for yes/full/no before doing anything
        result = store.get_session_start_context(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "CRITICAL" in ctx or "stop completely" in ctx.lower()
        assert "yes" in ctx.lower()
        assert "skip" in ctx.lower()
        # Hard constraint: Claude must not call bootstrap_context before hearing yes
        assert "do not" in ctx.lower() or "don't" in ctx.lower()

    def test_empty_repo_directive_includes_repo_path(self, tmp_repo):
        result = store.get_session_start_context(tmp_repo)
        assert tmp_repo in result["hookSpecificOutput"]["additionalContext"]

    def test_empty_repo_directive_includes_bootstrap_instruction(self, tmp_repo):
        result = store.get_session_start_context(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "bootstrap_context" in ctx

    def test_populated_repo_injects_pointer_not_full_dump(self, populated_repo):
        # JIT: SessionStart injects a count pointer, not the full context
        result = store.get_session_start_context(populated_repo)
        assert "on demand" in result["systemMessage"]
        assert "get_context" in result["hookSpecificOutput"]["additionalContext"]
        # Must NOT contain the decision content — Claude fetches that on demand
        assert "JWT" not in result["hookSpecificOutput"]["additionalContext"]

    def test_output_is_valid_hook_json(self, populated_repo):
        result = store.get_session_start_context(populated_repo)
        assert "systemMessage" in result
        assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"


# ── get_bootstrap_context_prompt ─────────────────────────────────────────────

class TestGetBootstrapContextPrompt:
    def test_empty_repo_returns_hook_output(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        result = store.get_bootstrap_context_prompt(tmp_repo)
        assert "hookSpecificOutput" in result
        assert result["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"

    def test_empty_repo_context_contains_instruction(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        result = store.get_bootstrap_context_prompt(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "no project context" in ctx.lower()
        assert "update_context" in ctx

    def test_populated_repo_returns_empty_dict(self, populated_repo):
        result = store.get_bootstrap_context_prompt(populated_repo)
        assert result == {}

    def test_directive_tells_claude_to_call_bootstrap_tool(self, tmp_repo):
        # Opt-in: Claude asks first, calls bootstrap_context only after user says yes
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "uv.lock").write_text("")
        result = store.get_bootstrap_context_prompt(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "bootstrap_context" in ctx
        assert "CRITICAL" in ctx or "stop completely" in ctx.lower()
        assert "yes" in ctx.lower()
        assert "skip" in ctx.lower()
        assert "do not" in ctx.lower() or "don't" in ctx.lower()

    def test_directive_includes_repo_path(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        result = store.get_bootstrap_context_prompt(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert tmp_repo in ctx

    def test_directive_includes_update_context_instruction(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        result = store.get_bootstrap_context_prompt(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "update_context" in ctx


# ── bootstrap_scan ────────────────────────────────────────────────────────────

def _gap_questions(result: dict) -> list[str]:
    return [g["question"] for g in result["gaps"]]


class TestBootstrapScan:
    # ── gap structure ──────────────────────────────────────────────────────────

    def test_gaps_are_dicts_with_required_keys(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        result = store.bootstrap_scan(tmp_repo, insight="high")
        for gap in result["gaps"]:
            assert "assumption" in gap
            assert "question" in gap
            assert "hint" in gap

    def test_always_asks_primary_purpose(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        result = store.bootstrap_scan(tmp_repo, insight="high")
        assert any("what does this repo do" in q.lower() for q in _gap_questions(result))

    def test_team_context_asked_when_architecture_signals_present(self, tmp_repo):
        # Team conventions gap is conditional — only when architecture signals suggest collaboration
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        src = Path(tmp_repo) / "src"
        (src / "api").mkdir(parents=True)
        (src / "services").mkdir(parents=True)
        (src / "models").mkdir(parents=True)
        result = store.bootstrap_scan(tmp_repo, insight="high")
        assert any("branch" in q.lower() or "team" in q.lower() or "pr" in q.lower() for q in _gap_questions(result))

    def test_exclusions_asked_when_dep_choices_exist(self, tmp_repo):
        # Exclusions gap only fires when dep tree has meaningful choices (>5 deps or ORM detected)
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "api"\nrequires-python = ">=3.12"\n'
            'dependencies = ["sqlalchemy>=2.0","httpx","pydantic","redis","celery","stripe"]\n'
        )
        result = store.bootstrap_scan(tmp_repo, insight="high")
        assert any("exclusion" in q.lower() or "intentionally" in q.lower() for q in _gap_questions(result))

    def test_constraints_asked_when_production_signals_present(self, tmp_repo):
        # Constraints gap only fires when production signals exist (auth, cloud, infra, container)
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "api"\nrequires-python = ">=3.12"\ndependencies = ["boto3>=1.0"]\n'
        )
        result = store.bootstrap_scan(tmp_repo, insight="high")
        assert any("constraint" in q.lower() for q in _gap_questions(result))

    def test_purpose_assumption_derived_from_readme(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "README.md").write_text("# MyApp\nA payment processing service for e-commerce.\n")
        result = store.bootstrap_scan(tmp_repo, insight="high")
        purpose_gap = next(g for g in result["gaps"] if "what does this repo do" in g["question"].lower())
        assert "payment" in purpose_gap["assumption"].lower()

    def test_purpose_assumption_inferred_from_name(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text('[project]\nname = "my-api-service"\nrequires-python = ">=3.12"\n')
        result = store.bootstrap_scan(tmp_repo, insight="high")
        purpose_gap = next(g for g in result["gaps"] if "what does this repo do" in g["question"].lower())
        assert "api" in purpose_gap["assumption"].lower() or "service" in purpose_gap["assumption"].lower()

    def test_max_10_questions(self, tmp_repo):
        # Even with all gaps triggered, total questions must not exceed 10
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "app"\nrequires-python = ">=3.12"\ndependencies = ["boto3>=1.0","stripe>=2.0"]\n'
        )
        result = store.bootstrap_scan(tmp_repo, insight="high")
        assert len(result["gaps"]) <= 10

    # ── language / tooling detection ──────────────────────────────────────────

    def test_detects_python_uv(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nrequires-python = ">=3.12"\n'
        )
        (Path(tmp_repo) / "uv.lock").write_text("")

        result = store.bootstrap_scan(tmp_repo, insight="high")
        inferred = " ".join(result["inferred"]).lower()
        assert "python" in inferred
        assert "uv" in inferred

    def test_detects_node_typescript_react(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        pkg = {
            "name": "my-app",
            "engines": {"node": ">=20"},
            "dependencies": {"react": "^18.0.0"},
            "devDependencies": {"typescript": "^5.0.0"},
        }
        (Path(tmp_repo) / "package.json").write_text(json.dumps(pkg))

        result = store.bootstrap_scan(tmp_repo, insight="high")
        inferred = " ".join(result["inferred"]).lower()
        assert "node" in inferred
        assert "typescript" in inferred
        assert "react" in inferred

    def test_detects_github_actions(self, tmp_repo):
        wf_dir = Path(tmp_repo) / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text("on: [push]")

        result = store.bootstrap_scan(tmp_repo, insight="high")
        assert any("github actions" in i.lower() for i in result["inferred"])

    # ── deployment detection ───────────────────────────────────────────────────

    def test_detects_dockerfile_suppresses_deployment_gap(self, tmp_repo):
        # When Dockerfile present, deployment target is known — no need to ask
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "Dockerfile").write_text("FROM python:3.12-slim\n")

        result = store.bootstrap_scan(tmp_repo, insight="high")
        assert any("dockerfile" in i.lower() or "container" in i.lower() for i in result["inferred"])
        assert not any("where does this run" in g["question"].lower() for g in result["gaps"])

    def test_no_dockerfile_adds_deployment_gap(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        # pyproject.toml marks this as a real code repo — not a simple/docs repo
        (Path(tmp_repo) / "pyproject.toml").write_text('[project]\nname = "api"\n')
        result = store.bootstrap_scan(tmp_repo, insight="high")
        env_gap = next(g for g in result["gaps"] if "where does this run" in g["question"].lower())
        assert "no container" in env_gap["assumption"].lower() or "deployment target" in env_gap["assumption"].lower()

    # ── data layer detection ───────────────────────────────────────────────────

    def test_detects_postgres_dep(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "api"\nrequires-python = ">=3.12"\ndependencies = ["psycopg[binary]>=3.0"]\n'
        )
        result = store.bootstrap_scan(tmp_repo, insight="high")
        assert any("postgresql" in i.lower() for i in result["inferred"])

    def test_detects_redis_dep(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        pkg = {"name": "app", "dependencies": {"redis": "^4.0.0"}}
        (Path(tmp_repo) / "package.json").write_text(json.dumps(pkg))

        result = store.bootstrap_scan(tmp_repo, insight="high")
        assert any("redis" in i.lower() for i in result["inferred"])

    def test_detects_orm_dep(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "api"\nrequires-python = ">=3.12"\ndependencies = ["sqlalchemy>=2.0"]\n'
        )
        result = store.bootstrap_scan(tmp_repo, insight="high")
        assert any("sqlalchemy" in i.lower() for i in result["inferred"])

    # ── auth and security-sensitive detection ──────────────────────────────────

    def test_detects_jwt_dep_in_inferred(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "api"\nrequires-python = ">=3.12"\ndependencies = ["python-jose[cryptography]>=3.3"]\n'
        )
        result = store.bootstrap_scan(tmp_repo, insight="high")
        assert any("jwt" in i.lower() or "auth" in i.lower() for i in result["inferred"])

    def test_security_sensitive_deps_add_compliance_gap(self, tmp_repo):
        # Auth or payment deps trigger a compliance question
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "api"\nrequires-python = ">=3.12"\ndependencies = ["stripe>=2.0"]\n'
        )
        result = store.bootstrap_scan(tmp_repo, insight="high")
        assert any("compliance" in q.lower() or "security" in q.lower() for q in _gap_questions(result))

    # ── cloud and integration detection ───────────────────────────────────────

    def test_detects_aws_sdk(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "api"\nrequires-python = ">=3.12"\ndependencies = ["boto3>=1.0"]\n'
        )
        result = store.bootstrap_scan(tmp_repo, insight="high")
        assert any("aws" in i.lower() for i in result["inferred"])

    def test_detects_stripe_integration(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        pkg = {"name": "app", "dependencies": {"stripe": "^14.0.0"}}
        (Path(tmp_repo) / "package.json").write_text(json.dumps(pkg))

        result = store.bootstrap_scan(tmp_repo, insight="high")
        assert any("stripe" in i.lower() for i in result["inferred"])

    def test_detects_openai_integration(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "api"\nrequires-python = ">=3.12"\ndependencies = ["openai>=1.0"]\n'
        )
        result = store.bootstrap_scan(tmp_repo, insight="high")
        assert any("openai" in i.lower() for i in result["inferred"])

    # ── monorepo detection ─────────────────────────────────────────────────────

    def test_detects_nx_monorepo(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "nx.json").write_text("{}")

        result = store.bootstrap_scan(tmp_repo, insight="high")
        assert any("monorepo" in i.lower() for i in result["inferred"])

    # ── novelty veto ───────────────────────────────────────────────────────────

    def test_skips_inferred_already_in_decisions(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "uv.lock").write_text("")
        data = store._load(tmp_repo)
        data["entries"].append({
            "id": "seed", "type": "decision", "content": "Package manager: uv",
            "session_id": "seed", "timestamp": "2026-01-01T00:00:00+00:00",
        })
        store._save(tmp_repo, data)

        result = store.bootstrap_scan(tmp_repo, insight="high")
        assert not any("uv" in i.lower() for i in result["inferred"])

    # ── is_simple_repo detection ───────────────────────────────────────────────

    def test_empty_repo_is_simple(self, tmp_repo):
        """No code config + no inferred stack → docs-only → is_simple_repo suppresses infra questions."""
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        result = store.bootstrap_scan(tmp_repo, insight="high")
        # Simple repo: only the purpose gap should appear, no tests/CI/deploy gaps
        questions = [g["question"].lower() for g in result["gaps"]]
        assert not any("automated testing" in q for q in questions)
        assert not any("build or deploy" in q for q in questions)
        assert not any("where does this run" in q for q in questions)

    def test_readme_portfolio_keyword_suppresses_infra_questions(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "README.md").write_text(
            "# Interview Submissions\nThis is a portfolio of job interview submissions.\n"
        )
        result = store.bootstrap_scan(tmp_repo, insight="high")
        questions = [g["question"].lower() for g in result["gaps"]]
        assert not any("automated testing" in q for q in questions)
        assert not any("where does this run" in q for q in questions)

    def test_readme_tutorial_keyword_suppresses_infra_questions(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "README.md").write_text("# Tutorial\nA demo project for learning.\n")
        result = store.bootstrap_scan(tmp_repo, insight="high")
        questions = [g["question"].lower() for g in result["gaps"]]
        assert not any("automated testing" in q for q in questions)

    def test_claude_md_portfolio_keyword_suppresses_infra_questions(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "CLAUDE.md").write_text(
            "This is a portfolio project showcasing interview submissions.\n"
        )
        result = store.bootstrap_scan(tmp_repo, insight="high")
        questions = [g["question"].lower() for g in result["gaps"]]
        assert not any("automated testing" in q for q in questions)
        assert not any("where does this run" in q for q in questions)

    def test_docs_dir_with_portfolio_keyword_suppresses_infra_questions(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        docs = Path(tmp_repo) / "docs"
        docs.mkdir()
        (docs / "overview.md").write_text(
            "This is a learning exercise and kata for practicing algorithms.\n"
        )
        result = store.bootstrap_scan(tmp_repo, insight="high")
        questions = [g["question"].lower() for g in result["gaps"]]
        assert not any("automated testing" in q for q in questions)

    def test_code_repo_without_portfolio_keywords_gets_infra_questions(self, tmp_repo):
        """A real code repo (with pyproject.toml, no simple-repo keywords) gets all relevant gaps."""
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text('[project]\nname = "api"\n')
        result = store.bootstrap_scan(tmp_repo, insight="high")
        questions = [g["question"].lower() for g in result["gaps"]]
        assert any("where does this run" in q for q in questions)
        assert any("automated testing" in q for q in questions)

    def test_claude_md_provides_readme_summary_fallback(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "CLAUDE.md").write_text(
            "# Project\nThis service handles payment processing for e-commerce.\n"
        )
        result = store.bootstrap_scan(tmp_repo, insight="high")
        purpose_gap = next(g for g in result["gaps"] if "what does this repo do" in g["question"].lower())
        assert "payment" in purpose_gap["assumption"].lower()

    # ── mined-suppression (bootstrap redesign) ─────────────────────────────────

    def test_mined_test_convention_suppresses_tests_gap(self, tmp_repo):
        # pyproject.toml (real code repo) + no test config -> tests gap would normally
        # fire, but a mined test convention makes asking redundant.
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text('[project]\nname = "api"\n')
        mined = [{"content": "Tests use plain pytest asserts (94% of 61 test functions)",
                  "subtype": "convention", "tier": "high"}]
        result = store.bootstrap_scan(tmp_repo, insight="high", mined=mined)
        assert not any("automated testing" in q.lower() for q in _gap_questions(result))

    def test_mined_test_layout_alone_does_not_suppress_tests_gap(self, tmp_repo):
        # Layout-only evidence (ad-hoc test files) doesn't prove testing is in scope —
        # the question must still be asked (Greptile #114).
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text('[project]\nname = "api"\n')
        mined = [{"content": "Tests live in tests/ (3 test files)",
                  "subtype": "convention", "tier": "high"}]
        result = store.bootstrap_scan(tmp_repo, insight="high", mined=mined)
        assert any("automated testing" in q.lower() for q in _gap_questions(result))

    def test_mined_ci_convention_suppresses_ci_gap(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text('[project]\nname = "api"\n')
        mined = [{"content": "CI runs: pytest, ruff check (from ci.yml)",
                  "subtype": "convention", "tier": "high"}]
        result = store.bootstrap_scan(tmp_repo, insight="high", mined=mined)
        assert not any("build or deploy" in q.lower() for q in _gap_questions(result))

    def test_mined_three_or_more_suppresses_team_conventions_gap(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        src = Path(tmp_repo) / "src"
        (src / "api").mkdir(parents=True)
        (src / "services").mkdir(parents=True)
        (src / "models").mkdir(parents=True)
        mined = [
            {"content": f"Functions use style {i} ({90 + i}% of 100 functions)",
             "subtype": "convention", "tier": "high"}
            for i in range(3)
        ]
        result = store.bootstrap_scan(tmp_repo, insight="high", mined=mined)
        assert not any(
            "branch" in q.lower() or "team" in q.lower() or "pr" in q.lower()
            for q in _gap_questions(result)
        )

    def test_mined_config_facts_do_not_suppress_team_conventions_gap(self, tmp_repo):
        # Config-encoded facts (line length, hook ids) say nothing about branching,
        # PR flow, or ownership — the team question must survive (Greptile #114).
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        src = Path(tmp_repo) / "src"
        (src / "api").mkdir(parents=True)
        (src / "services").mkdir(parents=True)
        (src / "models").mkdir(parents=True)
        mined = [
            {"content": "Line length 100 enforced by ruff (pyproject.toml)",
             "subtype": "convention", "tier": "high"},
            {"content": "Pre-commit hooks run: ruff, trailing-whitespace (.pre-commit-config.yaml)",
             "subtype": "convention", "tier": "high"},
            {"content": "Mypy strict mode required (pyproject.toml)",
             "subtype": "convention", "tier": "high"},
        ]
        result = store.bootstrap_scan(tmp_repo, insight="high", mined=mined)
        assert any(
            "branch" in q.lower() or "team" in q.lower() or "pr" in q.lower()
            for q in _gap_questions(result)
        )

    def test_mined_none_behaves_like_today(self, tmp_repo):
        # mined=None (every existing direct caller) must match mined=[] exactly - no
        # suppression, identical gap set.
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text('[project]\nname = "api"\n')
        with_none = store.bootstrap_scan(tmp_repo, insight="high", mined=None)
        with_empty = store.bootstrap_scan(tmp_repo, insight="high", mined=[])
        assert _gap_questions(with_none) == _gap_questions(with_empty)
        assert any("automated testing" in q.lower() for q in _gap_questions(with_none))


# ── bootstrap_apply (bootstrap redesign — core wiring) ───────────────────────

SESSION_ID_BA = "test-ba-session"


def _snake_file(n_snake: int, n_bad: int = 0) -> str:
    """A Python module of plain functions - mirrors test_miner.py's `_funcs` helper so
    the naming-convention stat lands on the same tier boundaries exercised there."""
    lines = [f"def fn_snake_{i}():\n    pass\n" for i in range(n_snake)]
    lines += [f"def fnBad{i}():\n    pass\n" for i in range(n_bad)]
    return "\n".join(lines)


class TestBootstrapApply:
    def test_consolidated_stack_entry_not_per_fact(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "widgets-api"\nrequires-python = ">=3.12"\n'
            'dependencies = ["fastapi", "sqlalchemy", "boto3", "stripe", "redis"]\n'
        )
        result = store.bootstrap_apply(tmp_repo, SESSION_ID_BA)
        decisions = [e for e in store._load(tmp_repo)["entries"] if e["type"] == "decision"]
        stack_entries = [d for d in decisions if d["content"].startswith("Stack: ")]
        assert len(stack_entries) == 1
        inferred_facts = set(result["inferred"])
        assert not any(d["content"] in inferred_facts for d in decisions)

    def test_high_tier_mined_stored_approved_scan(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "mod.py").write_text(_snake_file(n_snake=25))
        result = store.bootstrap_apply(tmp_repo, SESSION_ID_BA)
        entry = next(
            e for e in store._load(tmp_repo)["entries"]
            if e["type"] == "decision" and "snake_case" in e["content"]
        )
        assert entry["status"] == "approved"
        assert entry["created_by"] == "scan"
        assert entry["subtype"] == "convention"
        assert "%" in entry["content"]
        assert result["stored"] >= 1

    def test_medium_tier_mined_stored_pending_approval(self, tmp_repo):
        # NOT 'suggested': suggested entries inject at session start and never surface
        # in review_pending — a 60-89% signal must wait for the developer instead.
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "mod.py").write_text(_snake_file(n_snake=14, n_bad=6))
        result = store.bootstrap_apply(tmp_repo, SESSION_ID_BA)
        entry = next(
            e for e in store._load(tmp_repo)["entries"]
            if e["type"] == "decision" and "snake_case" in e["content"]
        )
        assert entry["status"] == "pending_approval"
        assert result["pending"] >= 1

    def test_medium_tier_surfaces_in_review_and_arms_nudge(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "mod.py").write_text(_snake_file(n_snake=14, n_bad=6))
        store.bootstrap_apply(tmp_repo, SESSION_ID_BA)
        pending = store.get_pending_decisions(tmp_repo)
        assert any("snake_case" in e["content"] for e in pending)
        assert store._pending_review_flag(tmp_repo).exists()

    def test_single_save_for_batch(self, tmp_repo, monkeypatch):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "widgets-api"\ndependencies = ["fastapi", "boto3"]\n'
        )
        (Path(tmp_repo) / "mod.py").write_text(_snake_file(n_snake=25))
        calls = []
        real_save = store._save

        def counting_save(repo_path, data):
            calls.append(1)
            return real_save(repo_path, data)

        monkeypatch.setattr(store, "_save", counting_save)
        store.bootstrap_apply(tmp_repo, SESSION_ID_BA)
        assert len(calls) == 1

    def test_second_call_is_noop(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "widgets-api"\ndependencies = ["fastapi", "boto3"]\n'
        )
        (Path(tmp_repo) / "mod.py").write_text(_snake_file(n_snake=25))
        store.bootstrap_apply(tmp_repo, SESSION_ID_BA)
        before = {e["id"]: e.get("occurrence_count", 1) for e in store._load(tmp_repo)["entries"]}

        result2 = store.bootstrap_apply(tmp_repo, SESSION_ID_BA)

        after = {e["id"]: e.get("occurrence_count", 1) for e in store._load(tmp_repo)["entries"]}
        assert result2["stored"] == 0
        assert result2["pending"] == 0
        assert result2["skipped"] > 0
        assert before == after

    def test_empty_repo_no_crash(self, tmp_repo):
        result = store.bootstrap_apply(tmp_repo, SESSION_ID_BA)
        assert result["stored"] == 0
        assert result["pending"] == 0
        assert result["skipped"] == 0

    def test_return_shape(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        result = store.bootstrap_apply(tmp_repo, SESSION_ID_BA)
        for key in ("inferred", "gaps", "insight", "insight_source", "decisive",
                    "stored", "pending", "skipped"):
            assert key in result

    def test_never_stores_constraint_from_mining(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "widgets-api"\n'
            'dependencies = ["fastapi", "sqlalchemy", "boto3", "stripe", "redis"]\n\n'
            '[tool.ruff]\nline-length = 100\n\n[tool.mypy]\nstrict = true\n'
        )
        (Path(tmp_repo) / "mod.py").write_text(_snake_file(n_snake=25))
        store.bootstrap_apply(tmp_repo, SESSION_ID_BA)
        data = store._load(tmp_repo)
        assert not any(
            e["type"] == "decision" and e.get("created_by") == "scan" and e.get("subtype") == "constraint"
            for e in data["entries"]
        )

    def test_stored_counts_reflect_post_trim_survivors(self, tmp_repo, monkeypatch):
        # Near MAX_ENTRIES, _keep_top can evict freshly-appended bootstrap entries
        # (pin_last protects only the final one). The returned counts must reflect
        # what actually survived, never what was appended (Greptile #114 P1).
        monkeypatch.setattr(store, "MAX_ENTRIES", 3)
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "widgets-api"\ndependencies = ["fastapi", "boto3"]\n'
        )
        (Path(tmp_repo) / "mod.py").write_text(_snake_file(n_snake=25))
        for i, f in enumerate(["filler decision alpha topic", "filler decision bravo topic",
                               "filler decision charlie topic"]):
            store.update_decision(tmp_repo, f, f"seed-{i}")

        result = store.bootstrap_apply(tmp_repo, SESSION_ID_BA)

        data = store._load(tmp_repo)
        surviving_scan = sum(1 for e in data["entries"]
                             if e["type"] == "decision" and e.get("created_by") == "scan"
                             and e.get("status") == "approved")
        surviving_pending = sum(1 for e in data["entries"]
                                if e["type"] == "decision" and e.get("created_by") == "scan"
                                and e.get("status") == "pending_approval")
        assert result["stored"] == surviving_scan
        assert result["pending"] == surviving_pending
        assert len(data["entries"]) <= store.MAX_ENTRIES

    def test_max_entries_respected(self, tmp_repo, monkeypatch):
        monkeypatch.setattr(store, "MAX_ENTRIES", 5)
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "widgets-api"\ndependencies = ["fastapi", "boto3"]\n'
        )
        (Path(tmp_repo) / "mod.py").write_text(_snake_file(n_snake=25))
        fillers = ["filler decision alpha topic", "filler decision bravo topic",
                   "filler decision charlie topic", "filler decision delta topic"]
        for i, f in enumerate(fillers):
            store.update_decision(tmp_repo, f, f"seed-{i}")

        store.bootstrap_apply(tmp_repo, SESSION_ID_BA)

        data = store._load(tmp_repo)
        assert len(data["entries"]) <= store.MAX_ENTRIES


# ── session start subtype breakdown (v0.4.0) ─────────────────────────────────

SESSION_ID_SS = "test-ss-session"

class TestSessionStartBreakdown:
    def test_constraints_and_conventions_shown_separately(self, tmp_repo):
        store.update_decision(tmp_repo, "Never commit untested code", SESSION_ID_SS, "constraint")
        store.update_decision(tmp_repo, "Always use uv not pip", SESSION_ID_SS, "convention")
        result = store.get_session_start_context(tmp_repo)
        msg = result["systemMessage"]
        assert "pending" in msg  # constraint is pending -> count-only notice
        assert "convention" in msg  # convention (suggested) loads as a project rule

    def test_arch_shown_as_on_demand(self, tmp_repo):
        store.update_decision(tmp_repo, "Use PostgreSQL for persistence", SESSION_ID_SS, "architecture")
        result = store.get_session_start_context(tmp_repo)
        msg = result["systemMessage"]
        assert "on demand" in msg
        assert "arch" in msg.lower() or "pattern" in msg.lower()

    def test_only_constraints_no_conventions(self, tmp_repo):
        store.update_decision(tmp_repo, "Never commit untested code", SESSION_ID_SS, "constraint")
        result = store.get_session_start_context(tmp_repo)
        msg = result["systemMessage"]
        assert "pending" in msg  # the lone pending constraint surfaces as a count

    def test_mixed_pre_loaded_and_deferred(self, tmp_repo):
        store.update_decision(tmp_repo, "Never commit secrets", SESSION_ID_SS, "constraint")
        store.update_decision(tmp_repo, "Use uv", SESSION_ID_SS, "convention")
        store.update_decision(tmp_repo, "Chose REST over GraphQL", SESSION_ID_SS, "architecture")
        result = store.get_session_start_context(tmp_repo)
        msg = result["systemMessage"]
        assert "on demand" in msg  # arch deferred
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "constraint" in ctx or "convention" in ctx  # pre-loaded rules shown


# ── project-context overview fallback (v0.4.0) ───────────────────────────────

class TestProjectContextOverviewFallback:
    def test_purpose_question_triggers_overview(self, tmp_repo):
        store.update_decision(tmp_repo, "This API serves internal dashboards", SESSION_ID_SS, "architecture")
        result = store.get_context_for_prompt(tmp_repo, "what is the purpose of this repo?")
        assert result != "", "purpose question should trigger context injection"
        assert "project context" in result.lower() or "auto-fetched" in result.lower()

    def test_domain_keyword_in_purpose_question_uses_query_not_overview(self, tmp_repo):
        store.update_decision(tmp_repo, "Use PostgreSQL for persistence", SESSION_ID_SS, "architecture")
        result = store.get_context_for_prompt(tmp_repo, "what is the purpose of the postgres schema?")
        # "postgres" is a domain keyword — should search, never dump the full overview.
        # (Retrieval V1: BM25 tokenizes exactly, so "postgres" no longer substring-matches
        # "PostgreSQL"; it routes via the shared `db` topic — a decision hit or a topic
        # pointer, but never the "[Contexer: project context]" overview.)
        if result:
            assert "project context" not in result.lower()
            assert "postgres" in result.lower() or "db" in result.lower()

    def test_scope_without_domain_keywords_triggers_overview(self, tmp_repo):
        store.update_decision(tmp_repo, "Handles payment processing for e-commerce", SESSION_ID_SS, "architecture")
        result = store.get_context_for_prompt(tmp_repo, "what is the scope of this project?")
        assert result != ""

    def test_non_rationale_prompt_returns_empty(self, tmp_repo):
        store.update_decision(tmp_repo, "Use PostgreSQL", SESSION_ID_SS, "architecture")
        result = store.get_context_for_prompt(tmp_repo, "fix the login bug")
        assert result == ""


# ── _is_prescriptive_constraint ───────────────────────────────────────────────

class TestIsPrescriptiveConstraint:
    def test_always_before_verb_is_constraint(self):
        is_c, subtype = store._is_prescriptive_constraint(
            "ensure that the terraform you write is as per best practice and always store state on s3 bucket secured"
        )
        assert is_c is True
        assert subtype == "constraint"

    def test_never_keyword_yields_constraint_subtype(self):
        is_c, subtype = store._is_prescriptive_constraint("never commit untested code to main")
        assert is_c is True
        assert subtype == "constraint"

    def test_always_standalone_at_start_is_constraint(self):
        is_c, subtype = store._is_prescriptive_constraint("always use conventional commits")
        assert is_c is True
        assert subtype == "constraint"

    def test_from_now_on_alone_is_convention(self):
        is_c, subtype = store._is_prescriptive_constraint("from now on use uv not pip for dependency management")
        assert is_c is True
        assert subtype == "convention"

    def test_going_forward_alone_is_convention(self):
        is_c, subtype = store._is_prescriptive_constraint("going forward use semantic versioning for all releases")
        assert is_c is True
        assert subtype == "convention"

    def test_henceforth_alone_is_convention(self):
        is_c, subtype = store._is_prescriptive_constraint("henceforth all pull requests require two approvals")
        assert is_c is True
        assert subtype == "convention"

    def test_from_now_on_with_always_is_constraint(self):
        # combined with "always" → constraint wins
        is_c, subtype = store._is_prescriptive_constraint("from now on always run tests before committing")
        assert is_c is True
        assert subtype == "constraint"

    def test_must_always_is_constraint(self):
        is_c, subtype = store._is_prescriptive_constraint("terraform must always store state on s3 with locking")
        assert is_c is True
        assert subtype == "constraint"

    def test_should_never_is_constraint(self):
        is_c, subtype = store._is_prescriptive_constraint("api keys should never be hardcoded in source files")
        assert is_c is True
        assert subtype == "constraint"

    def test_at_all_times_is_constraint(self):
        is_c, subtype = store._is_prescriptive_constraint("encryption must be enabled at all times for s3 buckets")
        assert is_c is True
        assert subtype == "constraint"

    def test_every_time_is_constraint(self):
        is_c, subtype = store._is_prescriptive_constraint("every time you deploy run the smoke tests first")
        assert is_c is True
        assert subtype == "constraint"

    def test_no_exceptions_is_constraint(self):
        is_c, subtype = store._is_prescriptive_constraint("all endpoints require authentication no exceptions")
        assert is_c is True
        assert subtype == "constraint"

    def test_as_a_rule_is_constraint(self):
        is_c, subtype = store._is_prescriptive_constraint("as a rule all database migrations must be reversible")
        assert is_c is True
        assert subtype == "constraint"

    def test_typo_allways_detected(self):
        is_c, _ = store._is_prescriptive_constraint("allways use https not http for external api calls")
        assert is_c is True

    def test_typo_alwyas_detected(self):
        is_c, _ = store._is_prescriptive_constraint("alwyas ensure the tests pass before merging the pr")
        assert is_c is True

    def test_question_prompt_excluded(self):
        is_c, _ = store._is_prescriptive_constraint("should we always use s3 for state storage?")
        assert is_c is False

    def test_personal_i_always_excluded(self):
        is_c, _ = store._is_prescriptive_constraint("I always get this error when running the server")
        assert is_c is False

    def test_personal_we_always_excluded(self):
        is_c, _ = store._is_prescriptive_constraint("we always use this pattern in the codebase")
        assert is_c is False

    def test_personal_it_always_excluded(self):
        # "it always worked" is descriptive — not a directive
        is_c, _ = store._is_prescriptive_constraint("it always worked before the last deployment")
        assert is_c is False

    def test_it_should_always_is_prescriptive(self):
        # "should" sits between "it" and "always" so the personal descriptor does not strip it
        is_c, subtype = store._is_prescriptive_constraint("it should always return 200 for the health endpoint")
        assert is_c is True
        assert subtype == "constraint"

    def test_no_trigger_words_excluded(self):
        is_c, _ = store._is_prescriptive_constraint("write tests for the authentication module")
        assert is_c is False

    def test_both_never_and_always_yields_constraint(self):
        is_c, subtype = store._is_prescriptive_constraint(
            "always encrypt sensitive data and never log passwords"
        )
        assert is_c is True
        assert subtype == "constraint"

    def test_ensure_you_is_detected(self):
        is_c, _ = store._is_prescriptive_constraint("ensure you never commit without running tests")
        assert is_c is True

    def test_ensure_that_you_is_detected(self):
        is_c, _ = store._is_prescriptive_constraint(
            "ensure that you are not revealing architecture in documentation"
        )
        assert is_c is True

    def test_make_sure_you_is_detected(self):
        is_c, _ = store._is_prescriptive_constraint("make sure you always write tests before committing")
        assert is_c is True

    def test_ensure_task_instruction_not_detected(self):
        is_c, _ = store._is_prescriptive_constraint("ensure the tests pass before the release")
        assert is_c is False

    def test_make_sure_task_instruction_not_detected(self):
        is_c, _ = store._is_prescriptive_constraint("make sure the API is RESTful")
        assert is_c is False

    def test_love_always_irony_excluded(self):
        is_c, _ = store._is_prescriptive_constraint("love always use pip")
        assert is_c is False

    def test_slash_s_sarcasm_excluded(self):
        is_c, _ = store._is_prescriptive_constraint("always use pip /s")
        assert is_c is False

    def test_yeah_right_irony_excluded(self):
        is_c, _ = store._is_prescriptive_constraint("yeah right never push to main directly")
        assert is_c is False

    def test_oh_sure_irony_excluded(self):
        is_c, _ = store._is_prescriptive_constraint("oh sure always commit broken code")
        assert is_c is False

    def test_genuine_always_still_detected(self):
        # Sarcasm exclusion should not affect real directives
        is_c, _ = store._is_prescriptive_constraint("always use uv not pip")
        assert is_c is True

    # ── broadened: prohibitions + rule-framing ────────────────────────────────
    def test_dont_prohibition_is_constraint(self):
        is_c, subtype = store._is_prescriptive_constraint("don't use pip, use uv instead")
        assert is_c is True
        assert subtype == "constraint"

    def test_do_not_prohibition_is_constraint(self):
        is_c, subtype = store._is_prescriptive_constraint("do not commit directly to main")
        assert is_c is True
        assert subtype == "constraint"

    def test_avoid_prohibition_is_constraint(self):
        is_c, subtype = store._is_prescriptive_constraint("avoid global mutable state in services")
        assert is_c is True
        assert subtype == "constraint"

    def test_no_longer_is_constraint(self):
        is_c, subtype = store._is_prescriptive_constraint("no longer support python 3.11 in this repo")
        assert is_c is True
        assert subtype == "constraint"

    def test_create_a_rule_framing_detected(self):
        is_c, _ = store._is_prescriptive_constraint("create a rule to not commit to main without review")
        assert is_c is True

    def test_make_a_rule_framing_detected(self):
        is_c, _ = store._is_prescriptive_constraint("make a rule that all PRs need two approvals")
        assert is_c is True

    def test_rule_colon_prefix_detected(self):
        is_c, _ = store._is_prescriptive_constraint("rule: every endpoint must be authenticated")
        assert is_c is True

    def test_stop_doing_is_constraint(self):
        is_c, _ = store._is_prescriptive_constraint("stop using deprecated requests, switch to httpx")
        assert is_c is True

    # ── soft prose must stay non-constraint ───────────────────────────────────
    def test_dont_worry_not_constraint(self):
        is_c, _ = store._is_prescriptive_constraint("don't worry about the tests for now")
        assert is_c is False

    def test_i_dont_know_not_constraint(self):
        is_c, _ = store._is_prescriptive_constraint("I don't know why the build is failing")
        assert is_c is False

    def test_dont_hesitate_not_constraint(self):
        is_c, _ = store._is_prescriptive_constraint("don't hesitate to refactor as you see fit")
        assert is_c is False


class TestConstraintNoiseGuards:
    """Regression: the constraint hook must not store pasted blobs or system text
    that merely contain a directive word (the source of store crowding)."""

    def test_long_pasted_task_is_not_a_constraint(self):
        blob = ("I want you to create another PR to update the readme to improve it. "
                "Add a section that says every Claude Code session starts fresh, and "
                "always replays decisions before Claude types. " * 4)
        assert store._is_prescriptive_constraint(blob)[0] is False

    def test_system_task_notification_is_not_a_constraint(self):
        text = "<task-notification>\n<task-id>abc</task-id> agent must always finish</task-notification>"
        assert store._is_prescriptive_constraint(text)[0] is False

    def test_contexer_injected_text_is_not_a_constraint(self):
        assert store._is_prescriptive_constraint("[Contexer: auto-fetched] always use uv")[0] is False

    def test_fenced_code_dump_is_not_a_constraint(self):
        text = "I got this issue now ```\nError: you must always set repo_path\n```"
        assert store._is_prescriptive_constraint(text)[0] is False

    def test_short_genuine_directive_still_captured(self):
        # the guards must not break real directive capture
        assert store._is_prescriptive_constraint("always use conventional commits") == (True, "constraint")
        assert store._is_prescriptive_constraint("never push without bumping the version")[0] is True


class TestSanitizeDirective:
    def test_profanity_stripped(self):
        result = store._sanitize_directive("always fucking use uv not pip")
        assert "fucking" not in result.lower()
        assert "uv" in result
        assert "pip" in result

    def test_frustration_opener_stripped(self):
        result = store._sanitize_directive("what the hell, always add tests dammit")
        assert "hell" not in result.lower()
        assert "always add tests" in result.lower()

    def test_excessive_exclamation_normalised(self):
        result = store._sanitize_directive("never commit broken code!!!!")
        assert "!!!!" not in result
        assert "never" in result.lower()

    def test_all_caps_normalised(self):
        result = store._sanitize_directive("ALWAYS USE UV NOT PIP")
        assert result == result[0].upper() + result[1:]
        assert "uv" in result.lower() or "UV" in result  # content preserved

    def test_clean_input_unchanged(self):
        text = "Always use uv not pip"
        assert store._sanitize_directive(text) == text

    def test_profanity_in_middle_stripped(self):
        result = store._sanitize_directive("never commit shit code again")
        assert "shit" not in result.lower()
        assert "never" in result.lower()
        assert "code" in result.lower()

    def test_trailing_hence_stripped(self):
        result = store._sanitize_directive("always use pip hence this would work")
        assert "hence" not in result.lower()
        assert result.lower().startswith("always use pip")

    def test_trailing_so_that_stripped(self):
        result = store._sanitize_directive("never commit without tests so that it stays clean")
        assert "so that" not in result.lower()
        assert "never commit without tests" in result.lower()

    def test_trailing_because_of_this_stripped(self):
        result = store._sanitize_directive("always use uv not pip because of this")
        assert "because" not in result.lower()
        assert "always use uv not pip" in result.lower()

    def test_clean_directive_unchanged(self):
        result = store._sanitize_directive("Always use uv not pip")
        assert result == "Always use uv not pip"


# ── capture_user_constraint ───────────────────────────────────────────────────

class TestCaptureUserConstraint:
    def test_stores_always_directive_as_constraint(self, tmp_repo):
        entry_id, content, status = store.capture_user_constraint(
            tmp_repo,
            "ensure terraform always stores state on s3 with dynamodb locking",
            "sess-1",
        )
        assert entry_id is not None
        assert content
        assert status == "approved"
        data = store._load(tmp_repo)
        decisions = [e for e in data["entries"] if e["type"] == "decision"]
        assert len(decisions) == 1
        assert decisions[0]["subtype"] == "constraint"

    def test_stores_never_directive_as_constraint(self, tmp_repo):
        entry_id, content, status = store.capture_user_constraint(
            tmp_repo, "never push secrets or credentials to the repository", "sess-1"
        )
        assert entry_id is not None
        data = store._load(tmp_repo)
        decisions = [e for e in data["entries"] if e["type"] == "decision"]
        assert decisions[0]["subtype"] == "constraint"

    def test_non_directive_prompt_returns_none(self, tmp_repo):
        entry_id, content, status = store.capture_user_constraint(
            tmp_repo, "write tests for the authentication module", "sess-1"
        )
        assert entry_id is None
        assert content is None
        assert status is None
        data = store._load(tmp_repo)
        assert data["entries"] == []

    def test_question_prompt_returns_none(self, tmp_repo):
        entry_id, content, status = store.capture_user_constraint(
            tmp_repo, "should we always use s3 for terraform state?", "sess-1"
        )
        assert entry_id is None

    def test_personal_descriptive_returns_none(self, tmp_repo):
        entry_id, content, status = store.capture_user_constraint(
            tmp_repo, "I always get a permission error when running this", "sess-1"
        )
        assert entry_id is None

    def test_duplicate_directive_silently_discarded(self, tmp_repo):
        store.capture_user_constraint(
            tmp_repo, "always store terraform state on s3 with dynamodb lock table", "sess-1"
        )
        entry_id, content, status = store.capture_user_constraint(
            tmp_repo, "always store terraform state on s3 with dynamodb lock table", "sess-2"
        )
        assert entry_id is None
        data = store._load(tmp_repo)
        decisions = [e for e in data["entries"] if e["type"] == "decision"]
        assert len(decisions) == 1

    def test_stored_as_decision_type(self, tmp_repo):
        store.capture_user_constraint(
            tmp_repo, "never commit without running tests first", "sess-1"
        )
        data = store._load(tmp_repo)
        entry = data["entries"][0]
        assert entry["type"] == "decision"
        assert entry["session_id"] == "sess-1"

    def test_long_prompt_is_rejected_not_stored(self, tmp_repo):
        # A long pasted blob that merely contains 'always' is not a clean directive —
        # it must not be stored (previously it was truncated to 600c and kept, which
        # crowded the store with pasted prompts).
        long_prompt = "always " + "x" * 700
        entry_id, content, status = store.capture_user_constraint(tmp_repo, long_prompt, "sess-1")
        assert entry_id is None
        assert store._load(tmp_repo)["entries"] == []


# ── Atomic save & corruption recovery ─────────────────────────────────────────

class TestAtomicSave:
    def test_save_leaves_no_temp_files(self, tmp_repo):
        store.update_decision(tmp_repo, "decided to use redis for caching hot reads", "sess-1")
        assert not list(store.STORE_DIR.glob("*.tmp")), "atomic write must clean up its temp file"

    def test_saved_file_is_owner_only(self, tmp_repo):
        store.update_decision(tmp_repo, "decided to use redis for caching hot reads", "sess-1")
        mode = store._store_path(tmp_repo).stat().st_mode & 0o777
        assert mode == 0o600

    def test_save_replaces_existing_file_atomically(self, tmp_repo):
        store._save(tmp_repo, {"repo_path": tmp_repo, "entries": []})
        before = store._store_path(tmp_repo).read_text()
        store._save(tmp_repo, {"repo_path": tmp_repo, "entries": [
            {"id": "1", "type": "decision", "subtype": "", "content": "c",
             "session_id": "s", "timestamp": "t"}]})
        after = json.loads(store._store_path(tmp_repo).read_text())
        assert json.loads(before)["entries"] == []
        assert len(after["entries"]) == 1

    def test_failed_write_cleans_up_temp_and_preserves_old_file(self, tmp_repo, monkeypatch):
        store._save(tmp_repo, {"repo_path": tmp_repo, "entries": []})
        before = store._store_path(tmp_repo).read_text()

        def boom(src, dst):
            raise OSError("disk full")
        monkeypatch.setattr(store.os, "replace", boom)
        with pytest.raises(OSError):
            store._save(tmp_repo, {"repo_path": tmp_repo, "entries": [
                {"id": "1", "type": "decision", "subtype": "", "content": "c",
                 "session_id": "s", "timestamp": "t"}]})
        assert not list(store.STORE_DIR.glob("*.tmp")), "temp file must be removed on failure"
        assert store._store_path(tmp_repo).read_text() == before, "old store must be untouched"

    def test_non_ascii_content_round_trips(self, tmp_repo):
        # Write side pins UTF-8 (ensure_ascii=False emits raw bytes); read side must
        # pin UTF-8 too — a locale-default read would corrupt this on non-UTF-8 systems.
        content = "décision: use café-naming → emoji ✓ 日本語"
        store._save(tmp_repo, {"repo_path": tmp_repo, "entries": [
            {"id": "1", "type": "decision", "subtype": "", "content": content,
             "session_id": "s", "timestamp": "t"}]})
        data = store._load(tmp_repo)
        assert data["entries"][0]["content"] == content


class TestCorruptionRecovery:
    def _corrupt(self, path: Path) -> None:
        path.write_text('{"repo_path": "/x", "entries": [{"id": "1", "type": "dec')  # truncated

    def test_load_recovers_from_truncated_json(self, tmp_repo):
        self._corrupt(store._store_path(tmp_repo))
        data = store._load(tmp_repo)
        assert data == {"repo_path": tmp_repo, "entries": []}

    def test_load_global_recovers_from_truncated_json(self, tmp_repo):
        self._corrupt(store._global_path())
        data = store._load_global()
        assert data == {"repo_path": store.GLOBAL_SLUG, "entries": []}

    def test_get_context_on_corrupt_store_is_graceful(self, tmp_repo):
        self._corrupt(store._store_path(tmp_repo))
        result = store.get_context(tmp_repo)
        assert "No context stored" in result

    def test_capture_after_corruption_rewrites_valid_store(self, tmp_repo):
        self._corrupt(store._store_path(tmp_repo))
        ok, _ = store.update_decision(
            tmp_repo, "decided to use JWT instead of sessions — stateless auth", "sess-1")
        assert ok
        data = json.loads(store._store_path(tmp_repo).read_text())  # valid JSON again
        assert len(data["entries"]) == 1


class TestSessionFromHookStdin:
    def test_extracts_session_id(self):
        from contexer import store
        assert store.session_from_hook_stdin('{"session_id": "abc-123"}') == "abc-123"

    def test_missing_session_id_returns_empty(self):
        from contexer import store
        assert store.session_from_hook_stdin('{"prompt": "hi"}') == ""

    def test_malformed_stdin_returns_empty(self):
        from contexer import store
        assert store.session_from_hook_stdin("not json") == ""


class TestSessionStartPayload:
    def test_empty_repo_payload_has_bootstrap_context(self, tmp_repo):
        from contexer import store
        p = store.session_start_payload(tmp_repo)
        assert "bootstrap" in p["context"].lower()
        assert "no context stored" in p["status"].lower()

    def test_populated_repo_payload_pointer(self, populated_repo):
        from contexer import store
        p = store.session_start_payload(populated_repo)
        assert "get_context" in p["context"]
        assert "on demand" in p["status"]

    def test_resume_with_decisions_has_status_no_context(self, populated_repo):
        from contexer import store
        p = store.session_start_payload(populated_repo, source="resume")
        assert p["context"] == ""
        assert "resumed" in p["status"].lower()

    def test_get_session_start_context_envelope_unchanged(self, tmp_repo):
        # Back-compat: the Claude dict shape is preserved exactly.
        from contexer import store
        result = store.get_session_start_context(tmp_repo)
        assert "no context stored" in result["systemMessage"].lower()
        assert "bootstrap" in result["hookSpecificOutput"]["additionalContext"].lower()
        assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"


class TestBootstrapPromptPayload:
    def test_decisions_present_payload_empty(self, populated_repo):
        from contexer import store
        p = store.bootstrap_prompt_payload(populated_repo, "anything")
        assert p == {"status": "", "context": ""}

    def test_empty_repo_payload_has_context(self, tmp_repo):
        from contexer import store
        p = store.bootstrap_prompt_payload(tmp_repo, "add a feature")
        assert p["status"] == ""
        assert p["context"] != ""


class TestPostCompactPayload:
    def test_empty_repo_reoffers_bootstrap(self, tmp_repo):
        from contexer import store
        p = store.post_compact_payload(tmp_repo)
        assert "bootstrap" in p["context"].lower()

    def test_populated_repo_reloads_context(self, populated_repo):
        from contexer import store
        p = store.post_compact_payload(populated_repo)
        assert "reloaded after compaction" in p["status"].lower()
        assert p["context"] != ""

    def test_session_id_rehydrates_working_set(self, tmp_repo):
        # Codex/Gemini compact-reload parity: session_id threads through to
        # _rehydrate_working_set so this session's pre-compaction router state survives
        # replay, same as Claude's SessionStart(compact) path.
        from contexer import store
        _seed_rv1(tmp_repo, RV1_CORPUS)
        sid = "postcompact-sess"
        store.get_context_for_prompt(
            tmp_repo, "why do jwt refresh tokens expire in httpOnly cookies?", sid)
        p = store.post_compact_payload(tmp_repo, sid)
        assert "Rehydrated working context" in p["context"]

    def test_no_session_id_omits_rehydration(self, populated_repo):
        from contexer import store
        p = store.post_compact_payload(populated_repo)
        assert "Rehydrated working context" not in p["context"]

    def test_get_post_compact_context_threads_session_id(self, tmp_repo):
        # Codex's entrypoint (claude.format_post_compact envelope) must pass session_id
        # through to post_compact_payload, not silently drop it.
        from contexer import store
        _seed_rv1(tmp_repo, RV1_CORPUS)
        sid = "codex-postcompact-sess"
        store.get_context_for_prompt(
            tmp_repo, "why do jwt refresh tokens expire in httpOnly cookies?", sid)
        out = store.get_post_compact_context(tmp_repo, sid)
        assert "Rehydrated working context" in out["systemMessage"]


# ── review regression: corruption recovery, slug injectivity, query matching ──

class TestNonDictStoreRecovery:
    """A store file holding valid JSON that is not an object must read as empty,
    not crash every tool call for the repo (review finding C3)."""

    @pytest.mark.parametrize("payload", ["[]", "null", '"x"', "42"])
    def test_non_dict_file_reads_as_empty(self, tmp_repo, payload):
        store._store_path(tmp_repo).write_text(payload, encoding="utf-8")
        data = store._load(tmp_repo)
        assert isinstance(data, dict)
        assert data["entries"] == []

    def test_non_dict_file_does_not_break_tool_calls(self, tmp_repo):
        store._store_path(tmp_repo).write_text("[]", encoding="utf-8")
        # Any of these would raise TypeError/AttributeError on a list/None payload.
        assert store.get_context(tmp_repo) == "No context stored for this repository."
        ok, _ = store.update_decision(tmp_repo, "use postgres over mysql for jsonb support", "s1")
        assert ok is True

    def test_entries_wrong_type_reads_as_empty(self, tmp_repo):
        store._store_path(tmp_repo).write_text('{"entries": "oops"}', encoding="utf-8")
        data = store._load(tmp_repo)
        assert data["entries"] == []

    @pytest.mark.parametrize("payload", ["[]", "null"])
    def test_global_non_dict_file_reads_as_empty(self, isolated_store_dir, payload):
        store._global_path().write_text(payload, encoding="utf-8")
        data = store._load_global()
        assert isinstance(data, dict)
        assert data["entries"] == []


@pytest.fixture
def isolated_store_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    return tmp_path


class TestSlugInjectivity:
    """Paths differing only by a char that maps to '_' must not share a store
    file (review finding H3)."""

    def test_dot_vs_underscore_paths_do_not_collide(self):
        assert store._slug("/home/u/my.repo") != store._slug("/home/u/my_repo")

    def test_space_vs_underscore_paths_do_not_collide(self):
        assert store._slug("/home/u/my repo") != store._slug("/home/u/my_repo")

    def test_colliding_repos_keep_separate_stores(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
        store.update_decision("/home/u/my.repo", "decision A for the dotted repo here", "s1")
        store.update_decision("/home/u/my_repo", "decision B for the underscore repo here", "s2")
        a = store.get_context("/home/u/my.repo")
        b = store.get_context("/home/u/my_repo")
        assert "Decision A" in a and "Decision B" not in a
        assert "Decision B" in b and "Decision A" not in b


class TestQueryNonWordPrefix:
    """A query starting with a non-word char must still match (review finding H5)."""

    @pytest.mark.parametrize("query,content", [
        (".env", "store secrets in the .env file, never in git"),
        ("@auth", "the @auth decorator guards every admin route"),
        ("#deploy", "tag #deploy in the message to trigger a release"),
    ])
    def test_non_word_prefix_query_matches(self, tmp_repo, query, content):
        store.update_decision(tmp_repo, content, "s1", "convention")
        out = store.get_context(tmp_repo, query=query)
        assert "No matching" not in out
        assert query in out


class TestConcurrentWriteIntegrity:
    """Concurrent writers to one repo's store must not lose each other's appends
    (review finding H2: lost-update race, ~47% loss measured without a lock)."""

    def test_no_lost_updates_under_concurrency(self, tmp_repo):
        import threading
        n_threads, per_thread = 8, 25
        total = n_threads * per_thread
        barrier = threading.Barrier(n_threads)

        def worker(t):
            barrier.wait()  # start together to maximize contention
            for i in range(per_thread):
                store.update_decision(
                    tmp_repo, f"distinct decision t{t} i{i} alpha{t}beta{i}gamma", f"sess-{t}")

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(n_threads)]
        for th in threads:
            th.start()
        for th in threads:
            th.join()

        data = store._load(tmp_repo)
        decisions = [e for e in data["entries"] if e["type"] == "decision"]
        assert len(decisions) == total, f"lost updates: kept {len(decisions)} of {total}"


# ── _classify_level ────────────────────────────────────────────────────────────

class TestClassifyLevel:
    def test_scan_created_is_auto(self):
        assert store._classify_level("Package manager: uv", "convention", "scan") == "auto"

    def test_human_created_is_auto(self):
        assert store._classify_level("Always use uv not pip", "constraint", "human") == "auto"

    def test_constraint_subtype_is_approval_required(self):
        assert store._classify_level("Never commit plaintext secrets", "constraint", "ai") == "approval_required"

    def test_bootstrap_convention_is_auto(self):
        assert store._classify_level("Test framework: pytest", "convention", "bootstrap") == "auto"

    def test_bootstrap_pattern_is_auto(self):
        assert store._classify_level("Route handlers in src/routes/", "pattern", "bootstrap") == "auto"

    def test_bootstrap_architecture_is_suggested(self):
        assert store._classify_level("Use PostgreSQL for storage", "architecture", "bootstrap") == "suggested"

    def test_scan_fact_pattern_match_is_auto(self):
        assert store._classify_level("CI/CD: GitHub Actions (3 workflow files)", "", "ai") == "auto"
        assert store._classify_level("Package manager: pnpm", "convention", "ai") == "auto"
        assert store._classify_level("Python project \"myapp\", requires-python >=3.11", "", "ai") == "auto"

    def test_l3_instead_of_signal(self):
        assert store._classify_level("We use RabbitMQ instead of Kafka for messaging", "architecture", "ai") == "approval_required"

    def test_l3_intentionally_signal(self):
        assert store._classify_level("Intentionally uses polling over webhooks for simplicity", "architecture", "ai") == "approval_required"

    def test_l3_standardize_on_signal(self):
        assert store._classify_level("We standardize on PostgreSQL for all transactional storage", "architecture", "ai") == "approval_required"

    def test_l3_prohibited_signal(self):
        assert store._classify_level("Use of Lambda is prohibited in our stack", "architecture", "ai") == "approval_required"

    def test_l3_mandatory_signal(self):
        assert store._classify_level("Helm is mandatory for all Kubernetes deployments", "architecture", "ai") == "approval_required"

    def test_l3_team_owns_signal(self):
        assert store._classify_level("Platform team owns all Terraform modules", "architecture", "ai") == "approval_required"

    def test_l3_all_services_must_signal(self):
        assert store._classify_level("All services must emit OpenTelemetry traces", "architecture", "ai") == "approval_required"

    def test_ai_architecture_without_l3_signals_is_suggested(self):
        assert store._classify_level("Use PostgreSQL for the main data store", "architecture", "ai") == "suggested"

    def test_ai_convention_is_suggested(self):
        assert store._classify_level("Conventional commits for all PRs", "convention", "ai") == "suggested"

    def test_ai_pattern_is_suggested(self):
        assert store._classify_level("Route handlers delegate to service layer", "pattern", "ai") == "suggested"

    def test_memory_convention_is_suggested(self):
        assert store._classify_level("Always use uv not pip", "convention", "memory") == "suggested"


# ── _compute_confidence ────────────────────────────────────────────────────────

class TestComputeConfidence:
    def test_new_ai_entry_has_base_score(self):
        entry = {"status": "suggested", "created_by": "ai", "occurrence_count": 1, "session_ids": ["s1"]}
        score, factors = store._compute_confidence(entry)
        assert score == 30
        assert factors == []

    def test_bootstrap_entry_gets_repo_bonus(self):
        entry = {"status": "approved", "created_by": "bootstrap", "occurrence_count": 1, "session_ids": ["s1"]}
        score, factors = store._compute_confidence(entry)
        assert score == 45
        assert "Observed in repository" in factors

    def test_scan_entry_gets_repo_bonus(self):
        entry = {"status": "approved", "created_by": "scan", "occurrence_count": 1, "session_ids": ["s1"]}
        score, factors = store._compute_confidence(entry)
        assert score == 45

    def test_human_entry_gets_stated_bonus(self):
        entry = {"status": "approved", "created_by": "human", "occurrence_count": 1, "session_ids": ["s1"]}
        score, factors = store._compute_confidence(entry)
        assert score == 50
        assert "Stated by developer" in factors

    def test_human_approval_adds_large_bonus(self):
        entry = {"status": "approved", "created_by": "ai", "approved_by": "human",
                 "occurrence_count": 1, "session_ids": ["s1"]}
        score, factors = store._compute_confidence(entry)
        assert score == 70
        assert "Approved by developer" in factors

    def test_multiple_sessions_adds_bonus(self):
        entry = {"status": "suggested", "created_by": "ai", "occurrence_count": 3,
                 "session_ids": ["s1", "s2", "s3"]}
        score, factors = store._compute_confidence(entry)
        assert score == 50  # 30 base + 20 for >=3 occurrences
        assert any("Referenced in" in f for f in factors)

    def test_score_capped_at_100(self):
        entry = {"status": "approved", "created_by": "bootstrap", "approved_by": "human",
                 "occurrence_count": 5, "session_ids": ["s1", "s2", "s3", "s4", "s5"],
                 "memory_key": "some-key"}
        score, _ = store._compute_confidence(entry)
        assert score == 100

    def test_memory_key_adds_small_bonus(self):
        base_entry = {"status": "approved", "created_by": "ai", "occurrence_count": 1, "session_ids": ["s1"]}
        score_without, _ = store._compute_confidence(base_entry)
        memory_entry = {**base_entry, "memory_key": "some/file.md##section"}
        score_with, factors = store._compute_confidence(memory_entry)
        assert score_with == score_without + 5
        assert "Persisted to memory tool" in factors


# ── _new_decision_entry with new fields ───────────────────────────────────────

class TestNewDecisionEntryFields:
    def test_status_and_created_by_stored(self, tmp_repo):
        store.update_decision(tmp_repo, "Use pytest for all unit tests", "s1", subtype="convention")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        assert "status" in entry
        assert "created_by" in entry
        assert "confidence" in entry

    def test_constraint_subtype_stored_as_pending(self, tmp_repo):
        store.update_decision(tmp_repo, "Never expose internal stack traces to clients", "s1", subtype="constraint")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        assert entry["status"] == "pending_approval"
        assert entry["created_by"] == "ai"

    def test_convention_subtype_stored_as_suggested(self, tmp_repo):
        store.update_decision(tmp_repo, "Use conventional commits for all pull requests", "s1", subtype="convention")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        assert entry["status"] == "suggested"

    def test_l3_content_stored_as_pending(self, tmp_repo):
        store.update_decision(tmp_repo, "We use RabbitMQ instead of Kafka by design", "s1", subtype="architecture")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        assert entry["status"] == "pending_approval"

    def test_scan_created_by_stored_as_approved(self, tmp_repo):
        store.update_decision(tmp_repo, "CI/CD: GitHub Actions (2 workflow files)", "s1",
                              subtype="convention", created_by="scan")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        assert entry["status"] == "approved"
        assert entry["created_by"] == "scan"

    def test_human_created_by_stored_as_approved(self, tmp_repo):
        store.update_decision(tmp_repo, "Always write tests before committing code", "s1",
                              subtype="constraint", created_by="human")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        assert entry["status"] == "approved"
        assert entry["created_by"] == "human"

    def test_backward_compat_entry_without_status_treated_as_approved(self, tmp_repo):
        # Simulate an old entry written before the status field existed
        store.STORE_DIR.mkdir(exist_ok=True)
        path = store._store_path(tmp_repo)
        old_entry = {
            "id": "abc123",
            "type": "decision",
            "subtype": "convention",
            "content": "Old decision without status field",
            "session_id": "s1",
            "session_ids": ["s1"],
            "timestamp": "2024-01-01T00:00:00+00:00",
            "occurrence_count": 1,
        }
        import json
        path.write_text(json.dumps({"repo_path": tmp_repo, "entries": [old_entry]}))
        # get_context should still show it (treated as approved)
        result = store.get_context(tmp_repo)
        assert "Old decision without status field" in result
        assert store._entry_status(old_entry) == "approved"


# ── update_decision replace_id ────────────────────────────────────────────────

class TestUpdateDecisionReplaceId:
    """replace_id on a TRIVIAL category (pattern/convention) updates in place as a new
    revision; the significant-category path (architecture/constraint) is covered by
    TestSuggestedUpdate."""

    def test_replace_id_updates_content_in_place(self, tmp_repo):
        _, eid = store.update_decision(tmp_repo, "Name test files test_*.py by convention", "s1",
                                       "convention")
        ok, _ = store.update_decision(tmp_repo, "Name test files *_test.py by convention", "s1",
                                      "convention", replace_id=eid)
        assert ok is True
        data = store._load(tmp_repo)
        entries = [e for e in data["entries"] if e["type"] == "decision"]
        assert len(entries) == 1
        assert entries[0]["content"] == "Name test files *_test.py by convention"
        assert entries[0]["id"] == eid

    def test_replace_id_bypasses_similarity_filter(self, tmp_repo):
        _, eid = store.update_decision(tmp_repo, "Group modules by feature folder", "s1", "pattern")
        # Near-duplicate would normally be filtered; replace_id forces the update.
        ok, _ = store.update_decision(tmp_repo, "Group modules by feature folder v2", "s1",
                                      "pattern", replace_id=eid)
        assert ok is True

    def test_replace_id_snapshots_prior_revision(self, tmp_repo):
        _, eid = store.update_decision(tmp_repo, "Lint runs in the pre-commit hook", "s1",
                                       "convention")
        store.update_decision(tmp_repo, "Lint runs in CI and the pre-commit hook", "s1",
                              "convention", replace_id=eid)
        entry = next(e for e in store._load(tmp_repo)["entries"] if e.get("id") == eid)
        # current revision bumped, prior value preserved in history (never overwritten away).
        # revisions[] now holds ALL versions (incl current); current_revision_id is the pointer.
        assert entry["revision"] == 2
        assert entry["content"] == "Lint runs in CI and the pre-commit hook"
        assert len(entry["revisions"]) == 2
        v1, v2 = entry["revisions"]
        assert v1["version_number"] == 1
        assert v1["content"] == "Lint runs in the pre-commit hook"
        assert v2["version_number"] == 2
        assert entry["current_revision_id"] == v2["revision_id"]
        # current revision content == decision HEAD-cache
        assert store._current_content(entry) == entry["content"]

    def test_replace_id_accumulates_multiple_revisions(self, tmp_repo):
        _, eid = store.update_decision(tmp_repo, "Deploy via script v1", "s1", "convention")
        store.update_decision(tmp_repo, "Deploy via script v2", "s1", "convention", replace_id=eid)
        store.update_decision(tmp_repo, "Deploy via script v3", "s1", "convention", replace_id=eid)
        entry = next(e for e in store._load(tmp_repo)["entries"] if e.get("id") == eid)
        assert entry["revision"] == 3
        # all three versions retained, in order; current pointer is v3
        assert [r["content"] for r in entry["revisions"]] == [
            "Deploy via script v1", "Deploy via script v2", "Deploy via script v3",
        ]
        assert [r["version_number"] for r in entry["revisions"]] == [1, 2, 3]
        assert store._current_content(entry) == "Deploy via script v3"

    def test_new_entry_has_revision_one_and_updated_at(self, tmp_repo):
        _, eid = store.update_decision(tmp_repo, "Use uv for dependency management here", "s1",
                                       "convention")
        entry = next(e for e in store._load(tmp_repo)["entries"] if e.get("id") == eid)
        assert entry["revision"] == 1
        assert entry["updated_at"] == entry["timestamp"]
        # A new decision is born with exactly one revision, pointed at by current_revision_id.
        assert len(entry["revisions"]) == 1
        rev = entry["revisions"][0]
        assert rev["version_number"] == 1
        assert rev["decision_id"] == eid
        assert rev["source"] == "ai"  # update_decision default created_by
        assert entry["current_revision_id"] == rev["revision_id"]

    def test_replace_id_not_found_falls_through_to_normal_store(self, tmp_repo):
        ok, new_id = store.update_decision(tmp_repo, "Use Redis for caching layer here", "s1",
                                           "convention", replace_id="nonexistent-id")
        assert ok is True
        assert new_id is not None

    def test_replace_id_updates_subtype_when_provided(self, tmp_repo):
        # convention -> pattern: both trivial categories, so it applies in place.
        _, eid = store.update_decision(tmp_repo, "Group helpers in a utils module", "s1", "convention")
        store.update_decision(tmp_repo, "Group helpers in a utils package", "s1",
                              "pattern", replace_id=eid)
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e.get("id") == eid)
        assert entry["subtype"] == "pattern"


class TestSuggestedUpdate:
    """Significant changes (architecture/constraint) become a Suggested Update that
    preserves the live decision until the developer approves - never overwritten."""

    def _approved(self, repo: str, content: str, subtype: str = "architecture") -> str:
        """Create a decision and force it to approved/live so a change becomes a proposal."""
        store.update_decision(repo, content, "s1", subtype)
        data = store._load(repo)
        entry = next(e for e in data["entries"] if e.get("type") == "decision")
        entry["status"] = "approved"
        store._save(repo, data)
        return entry["id"]

    def test_significant_change_creates_proposal_not_overwrite(self, tmp_repo):
        eid = self._approved(tmp_repo, "Rollback endpoint is /api/v1/rollback")
        ok, rid = store.update_decision(tmp_repo, "Rollback endpoint is /api/v2/rollback", "s2",
                                        "architecture", replace_id=eid)
        assert ok is True and rid == eid
        entry = next(e for e in store._load(tmp_repo)["entries"] if e.get("id") == eid)
        # live decision is UNCHANGED; the change is parked as a proposal awaiting approval
        assert entry["content"] == "Rollback endpoint is /api/v1/rollback"
        assert entry["revision"] == 1
        assert entry["proposed_revision"]["content"] == "Rollback endpoint is /api/v2/rollback"
        assert "confidence" in entry["proposed_revision"]

    def test_constraint_change_is_significant(self, tmp_repo):
        eid = self._approved(tmp_repo, "Never log secrets to stdout", "constraint")
        store.update_decision(tmp_repo, "Never log secrets or tokens to stdout", "s2",
                              "constraint", replace_id=eid)
        entry = next(e for e in store._load(tmp_repo)["entries"] if e.get("id") == eid)
        assert entry.get("proposed_revision") is not None

    def test_recategorising_constraint_is_significant(self, tmp_repo):
        # Downgrading a constraint to a convention still changes a constraint → approval.
        eid = self._approved(tmp_repo, "Always pin dependency versions", "constraint")
        store.update_decision(tmp_repo, "Prefer pinning dependency versions", "s2",
                              "convention", replace_id=eid)
        entry = next(e for e in store._load(tmp_repo)["entries"] if e.get("id") == eid)
        assert entry.get("proposed_revision") is not None

    def test_human_stated_change_applies_without_approval(self, tmp_repo):
        eid = self._approved(tmp_repo, "Use postgres for primary storage")
        store.update_decision(tmp_repo, "Use sqlite for primary storage", "s2",
                              "architecture", created_by="human", replace_id=eid)
        entry = next(e for e in store._load(tmp_repo)["entries"] if e.get("id") == eid)
        assert entry["content"] == "Use sqlite for primary storage"
        assert entry["revision"] == 2
        assert "proposed_revision" not in entry

    def test_approve_promotes_proposal_to_new_revision(self, tmp_repo):
        eid = self._approved(tmp_repo, "Rollback endpoint is /api/v1/rollback")
        store.update_decision(tmp_repo, "Rollback endpoint is /api/v2/rollback", "s2",
                              "architecture", replace_id=eid)
        ok, msg = store.approve_decision(tmp_repo, eid, "approve")
        assert ok is True
        entry = next(e for e in store._load(tmp_repo)["entries"] if e.get("id") == eid)
        assert entry["content"] == "Rollback endpoint is /api/v2/rollback"
        assert entry["revision"] == 2
        assert entry["revisions"][0]["content"] == "Rollback endpoint is /api/v1/rollback"
        assert "proposed_revision" not in entry
        assert entry["approved_by"] == "human"

    def test_dismiss_keeps_current_revision(self, tmp_repo):
        eid = self._approved(tmp_repo, "Use postgres for primary storage")
        store.update_decision(tmp_repo, "Use sqlite for primary storage", "s2",
                              "architecture", replace_id=eid)
        ok, msg = store.approve_decision(tmp_repo, eid, "dismiss")
        assert ok is True
        entry = next(e for e in store._load(tmp_repo)["entries"] if e.get("id") == eid)
        assert entry["content"] == "Use postgres for primary storage"
        assert entry["revision"] == 1
        assert "proposed_revision" not in entry

    def test_skip_keeps_proposal_pending(self, tmp_repo):
        eid = self._approved(tmp_repo, "Use postgres for primary storage")
        store.update_decision(tmp_repo, "Use sqlite for primary storage", "s2",
                              "architecture", replace_id=eid)
        store.approve_decision(tmp_repo, eid, "skip")
        entry = next(e for e in store._load(tmp_repo)["entries"] if e.get("id") == eid)
        assert entry.get("proposed_revision") is not None

    def test_edit_promotes_with_corrected_content(self, tmp_repo):
        eid = self._approved(tmp_repo, "Use postgres for primary storage")
        store.update_decision(tmp_repo, "Use sqlite for primary storage", "s2",
                              "architecture", replace_id=eid)
        store.approve_decision(tmp_repo, eid, "edit",
                               content="Use sqlite for local primary storage")
        entry = next(e for e in store._load(tmp_repo)["entries"] if e.get("id") == eid)
        assert entry["content"] == "Use sqlite for local primary storage"
        assert entry["revision"] == 2
        assert "proposed_revision" not in entry

    def test_proposal_surfaces_in_pending_and_prompt(self, tmp_repo):
        eid = self._approved(tmp_repo, "Rollback endpoint is /api/v1/rollback")
        store.update_decision(tmp_repo, "Rollback endpoint is /api/v2/rollback", "s2",
                              "architecture", replace_id=eid)
        pending_ids = [e["id"] for e in store.get_pending_decisions(tmp_repo)]
        assert eid in pending_ids
        prompt = store.get_pending_approval_prompt(tmp_repo, eid)
        assert "pending review" in prompt
        assert "/api/v2/rollback" in prompt
        assert "Dismiss" in prompt

    def test_blank_content_rejected_on_replace_id_path(self, tmp_repo):
        eid = self._approved(tmp_repo, "Use postgres for primary storage")
        ok, rid = store.update_decision(tmp_repo, "   ", "s2", "convention", replace_id=eid)
        assert ok is False
        entry = next(e for e in store._load(tmp_repo)["entries"] if e.get("id") == eid)
        assert entry["content"] == "Use postgres for primary storage"

    def test_identical_content_is_noop_on_replace_id(self, tmp_repo):
        eid = self._approved(tmp_repo, "Use postgres for primary storage")
        ok, rid = store.update_decision(tmp_repo, "Use postgres for primary storage", "s2",
                                        "architecture", replace_id=eid)
        assert ok is True and rid == eid
        entry = next(e for e in store._load(tmp_repo)["entries"] if e.get("id") == eid)
        assert entry["revision"] == 1
        assert "proposed_revision" not in entry
        # no-op: still exactly the original single revision, no new version appended
        assert len(entry["revisions"]) == 1

    def test_proposal_not_attached_to_pending_approval_base(self, tmp_repo):
        # A pending_approval base has not been approved yet; attaching a proposal
        # would let approve_decision silently bless never-reviewed content.
        # Use an L3-signal content to guarantee pending_approval status.
        store.update_decision(tmp_repo,
                              "Use Kafka instead of RabbitMQ for event streaming", "s1",
                              "architecture")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e.get("type") == "decision")
        assert entry.get("status") == "pending_approval", (
            f"Expected pending_approval but got {entry.get('status')!r}")
        eid = entry["id"]
        ok, rid = store.update_decision(tmp_repo,
                                        "Use RabbitMQ instead of Kafka for event streaming", "s2",
                                        "architecture", replace_id=eid)
        assert ok is True
        entry = next(e for e in store._load(tmp_repo)["entries"] if e.get("id") == eid)
        assert "proposed_revision" not in entry

    def test_identical_proposal_not_duplicated(self, tmp_repo):
        eid = self._approved(tmp_repo, "Rollback endpoint is /api/v1/rollback")
        store.update_decision(tmp_repo, "Rollback endpoint is /api/v2/rollback", "s2",
                              "architecture", replace_id=eid)
        # Second call with identical proposed content must not overwrite the proposal.
        ok, rid = store.update_decision(tmp_repo, "Rollback endpoint is /api/v2/rollback", "s3",
                                        "architecture", replace_id=eid)
        assert ok is True
        entry = next(e for e in store._load(tmp_repo)["entries"] if e.get("id") == eid)
        # Proposal should still exist and be from s2 (not silently replaced by s3).
        assert entry["proposed_revision"]["session_id"] == "s2"

    def test_approve_merges_proposing_session_into_session_ids(self, tmp_repo):
        eid = self._approved(tmp_repo, "Rollback endpoint is /api/v1/rollback")
        store.update_decision(tmp_repo, "Rollback endpoint is /api/v2/rollback", "s2",
                              "architecture", replace_id=eid)
        store.approve_decision(tmp_repo, eid, "approve")
        entry = next(e for e in store._load(tmp_repo)["entries"] if e.get("id") == eid)
        assert "s2" in entry.get("session_ids", [])
        assert entry.get("occurrence_count", 1) >= 2


# ── Decision / Revision model (Git-like: storage preserves history, replay = current) ──

class TestRevisionModel:
    """The refactored model: revisions are immutable first-class objects, the decision
    carries an explicit current_revision_id pointer, replay exposes only the current one."""

    def test_new_decision_has_first_revision_object(self, tmp_repo):
        _, eid = store.update_decision(tmp_repo, "Use feature-folder module layout", "s1", "pattern")
        entry = next(e for e in store._load(tmp_repo)["entries"] if e.get("id") == eid)
        rev = store._current_revision(entry)
        assert rev is not None
        # full revision object per the forward-ready schema
        for field in ("revision_id", "decision_id", "version_number", "content",
                      "confidence_score", "evidence", "created_at", "approved_at", "source"):
            assert field in rev, field
        assert rev["decision_id"] == eid
        assert rev["version_number"] == 1
        assert entry["current_revision_id"] == rev["revision_id"]

    def test_approval_moves_pointer_and_preserves_history(self, tmp_repo):
        # Build an approved architecture decision, then propose + approve a change.
        store.update_decision(tmp_repo, "Rollback endpoint is /api/v1/rollback", "s1", "architecture")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        entry["status"] = "approved"
        store._save(tmp_repo, data)
        eid = entry["id"]
        v1_rev_id = entry["current_revision_id"]

        store.update_decision(tmp_repo, "Rollback endpoint is /api/v2/rollback", "s2",
                              "architecture", replace_id=eid)
        store.approve_decision(tmp_repo, eid, "approve")

        entry = next(e for e in store._load(tmp_repo)["entries"] if e.get("id") == eid)
        # pointer advanced; both revisions preserved; precedence is the pointer, not time
        assert entry["current_revision_id"] != v1_rev_id
        assert len(entry["revisions"]) == 2
        assert store._current_content(entry) == "Rollback endpoint is /api/v2/rollback"
        assert any(r["revision_id"] == v1_rev_id and r["content"] == "Rollback endpoint is /api/v1/rollback"
                   for r in entry["revisions"])

    def test_replay_exposes_only_current_revision(self, tmp_repo):
        _, eid = store.update_decision(tmp_repo, "Deploy with blue-green v1", "s1", "convention")
        store.update_decision(tmp_repo, "Deploy with blue-green v2", "s1", "convention", replace_id=eid)
        out = store.get_context(tmp_repo)
        assert "v2" in out
        assert "v1" not in out  # historical revision never reaches replay

    def test_pending_update_does_not_change_current_revision(self, tmp_repo):
        store.update_decision(tmp_repo, "Primary store is postgres", "s1", "architecture")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        entry["status"] = "approved"
        store._save(tmp_repo, data)
        eid = entry["id"]
        before = entry["current_revision_id"]
        store.update_decision(tmp_repo, "Primary store is mysql", "s2", "architecture", replace_id=eid)
        entry = next(e for e in store._load(tmp_repo)["entries"] if e.get("id") == eid)
        # proposal parked; current revision unchanged until approval
        assert entry["current_revision_id"] == before
        assert store._current_content(entry) == "Primary store is postgres"
        assert entry.get("proposed_revision")

    def test_legacy_entry_migrates_on_load(self, tmp_repo):
        # Hand-write a pre-refactor store file (old shape: content + revision + historical
        # snapshots, no current_revision_id / no revision objects).
        store.STORE_DIR.mkdir(parents=True, exist_ok=True)
        legacy = {
            "repo_path": tmp_repo,
            "entries": [{
                "id": "legacy-1",
                "type": "decision",
                "subtype": "architecture",
                "content": "Rollback endpoint is /api/v3",
                "session_id": "s1",
                "session_ids": ["s1"],
                "timestamp": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-03-01T00:00:00+00:00",
                "revision": 3,
                "status": "approved",
                "created_by": "ai",
                "confidence": 70,
                "revisions": [
                    {"revision": 1, "content": "Rollback endpoint is /api/v1",
                     "subtype": "architecture", "status": "approved",
                     "timestamp": "2026-01-01T00:00:00+00:00", "replaced_at": "2026-02-01T00:00:00+00:00"},
                    {"revision": 2, "content": "Rollback endpoint is /api/v2",
                     "subtype": "architecture", "status": "approved",
                     "timestamp": "2026-02-01T00:00:00+00:00", "replaced_at": "2026-03-01T00:00:00+00:00"},
                ],
            }],
        }
        store._save(tmp_repo, legacy)

        data = store._load(tmp_repo)
        entry = data["entries"][0]
        # migrated: full revision objects incl current, pointer set, current content preserved
        assert entry.get("current_revision_id")
        assert len(entry["revisions"]) == 3
        assert [r["version_number"] for r in entry["revisions"]] == [1, 2, 3]
        assert all("revision_id" in r for r in entry["revisions"])
        assert store._current_content(entry) == "Rollback endpoint is /api/v3"
        # replay still shows only the current value
        out = store.get_context(tmp_repo)
        assert "/api/v3" in out and "/api/v1" not in out and "/api/v2" not in out

    def test_migration_is_idempotent(self, tmp_repo):
        _, eid = store.update_decision(tmp_repo, "Use uv for deps", "s1", "convention")
        first = store._load(tmp_repo)["entries"][0]
        rid = first["current_revision_id"]
        # a second load must not rebuild revisions or change the pointer
        second = store._load(tmp_repo)["entries"][0]
        assert second["current_revision_id"] == rid
        assert len(second["revisions"]) == 1


# ── approve_decision ──────────────────────────────────────────────────────────

class TestApproveDecision:
    def _store_pending(self, repo: str, content: str = "We use RabbitMQ instead of Kafka") -> str:
        """Store a decision that will land as pending_approval and return its id."""
        store.update_decision(repo, content, "s1", subtype="architecture")
        data = store._load(repo)
        entry = next(
            e for e in data["entries"]
            if e.get("type") == "decision" and e.get("status") == "pending_approval"
        )
        return entry["id"]

    def _store_active(self, repo: str, content: str, subtype: str = "convention") -> str:
        """Store a decision that lands ALREADY trusted (approved/suggested) and return its id."""
        store.update_decision(repo, content, "s1", subtype=subtype, created_by="human")
        data = store._load(repo)
        entry = next(e for e in data["entries"] if e.get("type") == "decision")
        assert entry["status"] in ("approved", "suggested")
        return entry["id"]

    def test_approve_action_sets_approved_status(self, tmp_repo):
        eid = self._store_pending(tmp_repo)
        ok, msg = store.approve_decision(tmp_repo, eid, "approve")
        assert ok is True
        assert "approved" in msg.lower() or "trusted" in msg.lower()
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e.get("id") == eid)
        assert entry["status"] == "approved"
        assert entry.get("approved_by") == "human"
        assert "approved_at" in entry

    def test_ignore_action_sets_ignored_status(self, tmp_repo):
        eid = self._store_pending(tmp_repo)
        ok, msg = store.approve_decision(tmp_repo, eid, "ignore")
        assert ok is True
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e.get("id") == eid)
        assert entry["status"] == "ignored"

    def test_edit_action_updates_content_and_approves(self, tmp_repo):
        eid = self._store_pending(tmp_repo)
        ok, msg = store.approve_decision(tmp_repo, eid, "edit",
                                         content="We use Kafka instead of RabbitMQ for throughput")
        assert ok is True
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e.get("id") == eid)
        assert entry["status"] == "approved"
        assert "Kafka" in entry["content"]
        assert entry.get("approved_by") == "human"

    def test_approve_boosts_confidence(self, tmp_repo):
        eid = self._store_pending(tmp_repo)
        data_before = store._load(tmp_repo)
        conf_before = next(e for e in data_before["entries"] if e.get("id") == eid)["confidence"]
        store.approve_decision(tmp_repo, eid, "approve")
        data_after = store._load(tmp_repo)
        conf_after = next(e for e in data_after["entries"] if e.get("id") == eid)["confidence"]
        assert conf_after > conf_before

    def test_invalid_action_returns_false(self, tmp_repo):
        eid = self._store_pending(tmp_repo)
        ok, msg = store.approve_decision(tmp_repo, eid, "accept")
        assert ok is False
        assert "Invalid" in msg

    def test_edit_without_content_returns_false(self, tmp_repo):
        eid = self._store_pending(tmp_repo)
        ok, msg = store.approve_decision(tmp_repo, eid, "edit", content="")
        assert ok is False

    def test_unknown_entry_id_returns_false(self, tmp_repo):
        ok, msg = store.approve_decision(tmp_repo, "nonexistent-id-abc", "approve")
        assert ok is False
        assert "not found" in msg

    def test_approved_decisions_appear_in_context_without_pending_tag(self, tmp_repo):
        eid = self._store_pending(tmp_repo)
        store.approve_decision(tmp_repo, eid, "approve")
        result = store.get_context(tmp_repo)
        assert "[pending]" not in result
        assert "RabbitMQ" in result

    def test_ignored_decisions_excluded_from_context(self, tmp_repo):
        eid = self._store_pending(tmp_repo)
        store.approve_decision(tmp_repo, eid, "ignore")
        result = store.get_context(tmp_repo)
        assert "RabbitMQ" not in result

    # ── ignore on an ALREADY-trusted (approved/suggested) decision — Finding 129 ──────

    def test_ignore_action_retires_an_approved_decision(self, tmp_repo):
        eid = self._store_active(tmp_repo, "Use snake_case for Python module names")
        ok, msg = store.approve_decision(tmp_repo, eid, "ignore")
        assert ok is True
        assert "retired" in msg.lower()
        entry = next(e for e in store._load(tmp_repo)["entries"] if e["id"] == eid)
        assert entry["status"] == "ignored"
        assert entry["revisions"], "full revision history is kept, not wiped"

    def test_ignore_action_retires_a_suggested_decision(self, tmp_repo):
        # created_by="ai" + non-constraint, no L3 signal -> lands "suggested" (still active/
        # injected), distinct from "approved" and from "pending_approval".
        store.update_decision(tmp_repo, "Group route handlers under api/routes/", "s1",
                              subtype="pattern", created_by="ai")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        assert entry["status"] == "suggested"
        ok, _msg = store.approve_decision(tmp_repo, entry["id"], "ignore")
        assert ok is True
        entry = next(e for e in store._load(tmp_repo)["entries"] if e["id"] == entry["id"])
        assert entry["status"] == "ignored"

    def test_ignored_active_decision_excluded_from_session_start_injection(self, tmp_repo):
        eid = self._store_active(tmp_repo, "Always use uv for dependency management")
        before = store.get_session_start_context(tmp_repo)
        assert "uv" in before["hookSpecificOutput"]["additionalContext"]
        store.approve_decision(tmp_repo, eid, "ignore")
        after = store.get_session_start_context(tmp_repo)
        # The lone decision is now ignored -> nothing left to inject at all.
        assert "hookSpecificOutput" not in after
        assert "uv" not in json.dumps(after)

    def test_approve_action_on_approved_decision_rejected(self, tmp_repo):
        eid = self._store_active(tmp_repo, "Use snake_case for Python module names")
        ok, msg = store.approve_decision(tmp_repo, eid, "approve")
        assert ok is False
        assert "already approved" in msg.lower()
        entry = next(e for e in store._load(tmp_repo)["entries"] if e["id"] == eid)
        assert entry["status"] == "approved", "no re-approval mutation"

    def test_edit_action_on_approved_decision_rejected(self, tmp_repo):
        eid = self._store_active(tmp_repo, "Use snake_case for Python module names")
        ok, msg = store.approve_decision(tmp_repo, eid, "edit", content="Use camelCase instead")
        assert ok is False
        entry = next(e for e in store._load(tmp_repo)["entries"] if e["id"] == eid)
        assert entry["content"] == "Use snake_case for Python module names", "untouched"

    def test_dismiss_and_skip_on_approved_decision_rejected(self, tmp_repo):
        eid = self._store_active(tmp_repo, "Use snake_case for Python module names")
        for action in ("dismiss", "skip"):
            ok, _msg = store.approve_decision(tmp_repo, eid, action)
            assert ok is False, f"{action} should stay pending-only"
        entry = next(e for e in store._load(tmp_repo)["entries"] if e["id"] == eid)
        assert entry["status"] == "approved"

    def test_ignore_on_already_ignored_decision_rejected(self, tmp_repo):
        eid = self._store_active(tmp_repo, "Use snake_case for Python module names")
        ok1, _ = store.approve_decision(tmp_repo, eid, "ignore")
        assert ok1 is True
        ok2, msg2 = store.approve_decision(tmp_repo, eid, "ignore")
        assert ok2 is False
        assert "already ignored" in msg2.lower()

    def test_pending_flow_unaffected_by_active_gating(self, tmp_repo):
        # Regression: the new status gate must not touch the pending_approval branch.
        eid = self._store_pending(tmp_repo)
        ok, msg = store.approve_decision(tmp_repo, eid, "approve")
        assert ok is True
        entry = next(e for e in store._load(tmp_repo)["entries"] if e["id"] == eid)
        assert entry["status"] == "approved"


# ── get_pending_decisions ──────────────────────────────────────────────────────

class TestGetPendingDecisions:
    def test_returns_pending_entries(self, tmp_repo):
        store.update_decision(tmp_repo, "We use RabbitMQ instead of Kafka", "s1", subtype="architecture")
        pending = store.get_pending_decisions(tmp_repo)
        assert len(pending) == 1
        assert "RabbitMQ" in pending[0]["content"]

    def test_excludes_approved_entries(self, tmp_repo):
        store.update_decision(tmp_repo, "Use pytest for testing", "s1", subtype="convention")
        pending = store.get_pending_decisions(tmp_repo)
        assert len(pending) == 0

    def test_excludes_constraint_after_approval(self, tmp_repo):
        store.update_decision(tmp_repo, "Never create public S3 buckets", "s1", subtype="constraint")
        data = store._load(tmp_repo)
        eid = next(e for e in data["entries"] if e["type"] == "decision")["id"]
        store.approve_decision(tmp_repo, eid, "approve")
        pending = store.get_pending_decisions(tmp_repo)
        assert len(pending) == 0

    def test_empty_repo_returns_empty_list(self, tmp_repo):
        assert store.get_pending_decisions(tmp_repo) == []


# ── get_pending_approval_prompt ────────────────────────────────────────────────

class TestGetPendingApprovalPrompt:
    def test_returns_formatted_prompt_for_pending_entry(self, tmp_repo):
        store.update_decision(tmp_repo, "We deploy only to AWS — all other clouds prohibited", "s1",
                              subtype="architecture")
        data = store._load(tmp_repo)
        eid = next(e for e in data["entries"] if e["type"] == "decision")["id"]
        prompt = store.get_pending_approval_prompt(tmp_repo, eid)
        assert "pending review" in prompt
        assert "AWS" in prompt
        assert "Confidence:" in prompt
        assert "approve_decision" in prompt
        assert "[Y] Approve" in prompt
        assert "[N] Ignore" in prompt

    def test_returns_empty_for_approved_entry(self, tmp_repo):
        store.update_decision(tmp_repo, "Use pytest for unit tests", "s1", subtype="convention")
        data = store._load(tmp_repo)
        eid = next(e for e in data["entries"] if e["type"] == "decision")["id"]
        assert store.get_pending_approval_prompt(tmp_repo, eid) == ""

    def test_returns_empty_for_none_id(self, tmp_repo):
        assert store.get_pending_approval_prompt(tmp_repo, None) == ""

    def test_returns_empty_for_unknown_id(self, tmp_repo):
        assert store.get_pending_approval_prompt(tmp_repo, "nonexistent") == ""


# ── get_context with status tags ──────────────────────────────────────────────

class TestGetContextStatusTags:
    def test_suggested_entries_show_suggested_tag(self, tmp_repo):
        store.update_decision(tmp_repo, "Use Postgres for the main data store", "s1", subtype="architecture")
        result = store.get_context(tmp_repo)
        assert "[suggested]" in result

    def test_pending_entries_show_pending_tag(self, tmp_repo):
        store.update_decision(tmp_repo, "We use RabbitMQ instead of Kafka", "s1", subtype="architecture")
        result = store.get_context(tmp_repo)
        assert "[pending]" in result

    def test_approved_entries_show_no_status_tag(self, tmp_repo):
        store.update_decision(tmp_repo, "Package manager: uv", "s1", subtype="convention", created_by="scan")
        result = store.get_context(tmp_repo)
        assert "[suggested]" not in result
        assert "[pending]" not in result

    def test_ignored_entries_excluded_from_context(self, tmp_repo):
        store.update_decision(tmp_repo, "We use RabbitMQ instead of Kafka", "s1", subtype="architecture")
        data = store._load(tmp_repo)
        eid = next(e for e in data["entries"] if e["type"] == "decision")["id"]
        store.approve_decision(tmp_repo, eid, "ignore")
        result = store.get_context(tmp_repo)
        assert "RabbitMQ" not in result

    def test_active_only_excludes_pending(self, tmp_repo):
        # The _active_only flag is used by session injection — not the MCP tool.
        # Pending decisions are excluded from active-only queries.
        store.update_decision(tmp_repo, "We use RabbitMQ instead of Kafka", "s1", subtype="architecture")
        result_full = store.get_context(tmp_repo)
        result_active = store.get_context(tmp_repo, _active_only=True)
        assert "RabbitMQ" in result_full   # visible in full mode
        assert "RabbitMQ" not in result_active  # excluded in active-only

    def test_active_only_includes_suggested(self, tmp_repo):
        store.update_decision(tmp_repo, "Use Postgres for the main data store", "s1", subtype="architecture")
        result = store.get_context(tmp_repo, _active_only=True)
        assert "Postgres" in result


# ── session_start with pending decisions ──────────────────────────────────────

SESSION_ID_CONF = "test-confidence-session"


class TestSessionStartWithPending:
    def test_pending_constraint_appears_in_status(self, tmp_repo):
        store.update_decision(tmp_repo, "Never create public S3 buckets anywhere", SESSION_ID_CONF, "constraint")
        result = store.get_session_start_context(tmp_repo)
        msg = result["systemMessage"]
        assert "pending review" in msg  # count-only pending notice
        assert "pending" in msg.lower()

    def test_pending_decisions_excluded_from_project_rules_injection(self, tmp_repo):
        store.update_decision(tmp_repo, "Never create public S3 buckets anywhere", SESSION_ID_CONF, "constraint")
        result = store.get_session_start_context(tmp_repo)
        ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        # Pending constraints must NOT appear as trusted rules
        assert "Never create public S3 buckets" not in ctx.split("## Project rules")[1] if "## Project rules" in ctx else True

    def test_approved_constraint_appears_in_project_rules(self, tmp_repo):
        store.update_decision(tmp_repo, "Never create public S3 buckets anywhere", SESSION_ID_CONF, "constraint")
        data = store._load(tmp_repo)
        eid = next(e for e in data["entries"] if e["type"] == "decision")["id"]
        store.approve_decision(tmp_repo, eid, "approve")
        result = store.get_session_start_context(tmp_repo)
        msg = result["systemMessage"]
        # After approval, constraint shows up in loaded count
        assert "constraint" in msg
        assert "pending" not in msg.lower()

    def test_suggested_convention_injected_with_tag(self, tmp_repo):
        store.update_decision(tmp_repo, "Use conventional commits for all pull requests",
                              SESSION_ID_CONF, "convention")
        result = store.get_session_start_context(tmp_repo)
        ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "conventional commits" in ctx
        assert "[suggested]" in ctx

    def test_pending_review_reminder_in_context(self, tmp_repo):
        store.update_decision(tmp_repo, "We standardize on PostgreSQL for all relational data",
                              SESSION_ID_CONF, "architecture")
        result = store.get_session_start_context(tmp_repo)
        ctx = result.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "pending" in ctx.lower()
        assert "contexer review" in ctx or "approve_decision" in ctx


# ── capture_user_constraint with new fields ────────────────────────────────────

class TestCaptureUserConstraintFields:
    def test_human_constraint_stored_as_approved(self, tmp_repo):
        store.capture_user_constraint(tmp_repo, "always use uv not pip", "s1")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        assert entry["status"] == "approved"
        assert entry["created_by"] == "human"


# ── deictic constraint scope (decision ceb955f5) ───────────────────────────────
# A prescriptive directive that leans on a conversation-local pronoun (this/that/these/
# those/it/here) is a strong signal of session-scoped intent, not a standing rule.
# It is still stored (never dropped) but as pending_approval, not auto-trusted.

class TestDeicticConstraintScope:
    @pytest.mark.parametrize("prompt", [
        # Three live misfires from 2026-07-15.
        "I'm not going to accept any performance degradation so ensure you clarify "
        "and ensure this feature is actual improvement",
        'It could be "Dogfood for you agents so that they stop overdoing, repeating '
        'and waisting time and tokens."',
        "It could be beautiful nudges for you ai agents to stop wondering and "
        "overthinking same issue. something likethat",
        # Greptile #125 P1: unresolved mid-directive pronoun must not be trusted.
        "always apply it before deployment",
        # Greptile #125 P2 counter-case: mid-directive here is conversation-local.
        "the pattern used here must always be followed",
    ])
    def test_deictic_directive_stored_pending_not_trusted(self, tmp_repo, prompt):
        entry_id, content, status = store.capture_user_constraint(tmp_repo, prompt, "s1")
        assert entry_id is not None, f"must still be stored: {prompt!r}"
        assert status == "pending_approval"
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["id"] == entry_id)
        assert entry["status"] == "pending_approval"
        assert entry["created_by"] == "human"  # provenance unchanged by the deictic check

    @pytest.mark.parametrize("prompt", [
        "always use uv not pip",
        "never log request data",
        "from now on use conventional commits for every commit",
        "never commit code that fails lint",
        "make it a rule to run tests before pushing",
        "always ensure that migrations are reversible",
        "always use uv for this repo",
        # Greptile #125 P2: trailing here scopes the rule to the repo — durable.
        "always use uv here",
        "never push directly to main here.",
    ])
    def test_clean_directive_remains_trusted(self, tmp_repo, prompt):
        entry_id, content, status = store.capture_user_constraint(tmp_repo, prompt, "s1")
        assert entry_id is not None, f"must be stored trusted: {prompt!r}"
        assert status == "approved"
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["id"] == entry_id)
        assert entry["status"] == "approved"

    def test_it_inside_word_does_not_trigger_pending(self, tmp_repo):
        # "git" contains "it" as a substring but not as a standalone word.
        entry_id, content, status = store.capture_user_constraint(
            tmp_repo, "always run git fetch before rebasing", "s1")
        assert entry_id is not None
        assert status == "approved"

    def test_deictic_capture_does_not_arm_pending_review_flag(self, tmp_repo):
        # Fix 5: the in-band ack already notifies; a second deferred nudge would double up.
        store.capture_user_constraint(
            tmp_repo,
            "I'm not going to accept any performance degradation so ensure you clarify "
            "and ensure this feature is actual improvement",
            "s1",
        )
        assert not store._pending_review_flag(tmp_repo).exists()

    def test_deictic_pending_surfaces_in_review_pending(self, tmp_repo):
        entry_id, content, status = store.capture_user_constraint(
            tmp_repo,
            "I'm not going to accept any performance degradation so ensure you clarify "
            "and ensure this feature is actual improvement",
            "s1",
        )
        pending = store.get_pending_decisions(tmp_repo)
        assert any(e["id"] == entry_id for e in pending)


class TestDeicticCleanRestatementPromotion:
    """Fix 2: a clean (non-deictic) restatement that overlaps its own pending twin promotes
    it to approved in place, instead of being silently dropped as a duplicate."""

    _DEICTIC = "always validate this feature before shipping"
    _CLEAN = "always validate the feature before shipping"

    def test_pending_then_clean_restatement_promotes_to_approved(self, tmp_repo):
        eid, _, status = store.capture_user_constraint(tmp_repo, self._DEICTIC, "s1")
        assert status == "pending_approval"

        eid2, content2, status2 = store.capture_user_constraint(tmp_repo, self._CLEAN, "s2")
        assert eid2 == eid, "promotion amends the same decision, not a new one"
        assert status2 == "promoted"
        assert "the feature" in content2.lower()

        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["id"] == eid)
        assert entry["status"] == "approved"
        assert "the feature" in entry["content"].lower()
        assert entry["occurrence_count"] == 2

    def test_promoted_entry_injected_at_session_start(self, tmp_repo):
        store.capture_user_constraint(tmp_repo, self._DEICTIC, "s1")
        store.capture_user_constraint(tmp_repo, self._CLEAN, "s2")
        payload = store.session_start_payload(tmp_repo)
        assert "the feature" in payload["context"].lower()

    def test_deictic_restatement_of_pending_twin_stays_silent_noop(self, tmp_repo):
        eid, _, _ = store.capture_user_constraint(tmp_repo, self._DEICTIC, "s1")
        eid2, content2, status2 = store.capture_user_constraint(tmp_repo, self._DEICTIC, "s2")
        assert (eid2, content2, status2) == (None, None, None)
        entry = next(e for e in store._load(tmp_repo)["entries"] if e["id"] == eid)
        assert entry["status"] == "pending_approval"
        assert entry["occurrence_count"] == 1

    def test_clean_restatement_of_already_approved_entry_stays_silent_noop(self, tmp_repo):
        store.capture_user_constraint(tmp_repo, self._DEICTIC, "s1")
        store.capture_user_constraint(tmp_repo, self._CLEAN, "s2")  # promotes -> approved
        eid3, content3, status3 = store.capture_user_constraint(tmp_repo, self._CLEAN, "s3")
        assert (eid3, content3, status3) == (None, None, None)
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e["type"] == "decision")
        assert entry["occurrence_count"] == 2, "no further bump on an ordinary duplicate"


class TestDeicticIgnoredTombstoneDoesNotBlock:
    """Fix 3: an entry the developer ignored via review must not block a re-typed rule."""

    _DEICTIC = "always validate this feature before shipping"
    _CLEAN = "always validate the feature before shipping"

    def test_reretype_after_ignore_stores_fresh_as_approved(self, tmp_repo):
        eid, _, status = store.capture_user_constraint(tmp_repo, self._DEICTIC, "s1")
        assert status == "pending_approval"
        store.approve_decision(tmp_repo, eid, "ignore")
        data = store._load(tmp_repo)
        assert next(e for e in data["entries"] if e["id"] == eid)["status"] == "ignored"

        eid2, content2, status2 = store.capture_user_constraint(tmp_repo, self._CLEAN, "s2")
        assert eid2 is not None and eid2 != eid, "re-typed rule lands as a fresh entry"
        assert status2 == "approved"
        data = store._load(tmp_repo)
        fresh = next(e for e in data["entries"] if e["id"] == eid2)
        assert fresh["status"] == "approved"
        ignored = next(e for e in data["entries"] if e["id"] == eid)
        assert ignored["status"] == "ignored", "the tombstone itself is untouched"


class TestContainmentCapture:
    """Containment-aware routing in capture_user_constraint: a superset restatement of a
    stored rule (high |∩|/min, low |∩|/max) consolidates onto the existing entry instead
    of accumulating. _overlap_ratio/_find_match are untouched — bootstrap idempotence,
    memory sync, and team dedup depend on the max-denominator metric as-is."""

    # The real user-reported scenario, verbatim (typo kept on purpose).
    _SEED_LONG = "Always ensure you commit changes on approval"
    _SEED_SHORT = "Always commit automatically"
    _SUPERSET = "Always commit automatically after approvals and ensure you double cfonirm"

    def test_real_three_constraint_scenario_becomes_suggested_update(self, tmp_repo):
        eid1, _, st1 = store.capture_user_constraint(tmp_repo, self._SEED_LONG, "s1")
        eid2, _, st2 = store.capture_user_constraint(tmp_repo, self._SEED_SHORT, "s1")
        assert st1 == st2 == "approved"

        near: list = []
        eid3, content3, status3 = store.capture_user_constraint(
            tmp_repo, self._SUPERSET, "s2", near)
        assert status3 == "revision_proposed"
        assert eid3 == eid2, "routed onto the contained entry, not stored as a third"
        assert near == [], "no near-miss note on a containment hit"

        data = store._load(tmp_repo)
        decisions = [e for e in data["entries"] if e["type"] == "decision"]
        assert len(decisions) == 2, "NO new overlapping entry"
        target = next(e for e in decisions if e["id"] == eid2)
        prop = target.get("proposed_revision")
        assert prop and "cfonirm" in prop["content"]
        assert target["content"] == self._SEED_SHORT, "trusted revision never replaced silently"
        assert target["status"] == "approved"

        ack = store.constraint_ack(content3, status3, eid3, near)
        assert "suggested update" in ack.lower()
        assert eid2[:8] in ack
        assert "do not approve it yourself" in ack.lower()

    def test_approving_the_proposal_promotes_to_new_revision(self, tmp_repo):
        store.capture_user_constraint(tmp_repo, self._SEED_LONG, "s1")
        eid2, _, _ = store.capture_user_constraint(tmp_repo, self._SEED_SHORT, "s1")
        store.capture_user_constraint(tmp_repo, self._SUPERSET, "s2")
        ok, msg = store.approve_decision(tmp_repo, eid2, "approve")
        assert ok
        entry = next(e for e in store._load(tmp_repo)["entries"] if e["id"] == eid2)
        assert entry["revision"] == 2
        assert "cfonirm" in entry["content"]

    def test_superset_capture_is_idempotent(self, tmp_repo):
        store.capture_user_constraint(tmp_repo, self._SEED_LONG, "s1")
        eid2, _, _ = store.capture_user_constraint(tmp_repo, self._SEED_SHORT, "s1")
        store.capture_user_constraint(tmp_repo, self._SUPERSET, "s2")
        eid3b, _, status3b = store.capture_user_constraint(tmp_repo, self._SUPERSET, "s3")
        # Repeat lands on the same entry; the identical proposal is not re-attached.
        assert (eid3b, status3b) == (eid2, "revision_proposed")
        data = store._load(tmp_repo)
        assert len([e for e in data["entries"] if e["type"] == "decision"]) == 2

    def test_pending_twin_clean_superset_promotes_with_new_content(self, tmp_repo):
        eid, _, st = store.capture_user_constraint(
            tmp_repo, "always validate this feature before shipping", "s1")
        assert st == "pending_approval"
        eid2, content2, status2 = store.capture_user_constraint(
            tmp_repo,
            "always validate the feature before shipping and run the full smoke suite",
            "s2")
        assert eid2 == eid
        assert status2 == "promoted"
        entry = next(e for e in store._load(tmp_repo)["entries"] if e["id"] == eid)
        assert entry["status"] == "approved"
        assert "smoke suite" in entry["content"]
        assert entry["revision"] == 2, "promotion appends a revision (history preserved)"

    def test_pending_twin_deictic_superset_amends_in_place_stays_pending(self, tmp_repo):
        eid, _, _ = store.capture_user_constraint(
            tmp_repo, "always validate this feature before shipping", "s1")
        eid2, content2, status2 = store.capture_user_constraint(
            tmp_repo,
            "always validate this feature before shipping and update it in the changelog",
            "s2")
        assert eid2 == eid
        assert status2 == "pending_approval"
        entry = next(e for e in store._load(tmp_repo)["entries"] if e["id"] == eid)
        assert entry["status"] == "pending_approval"
        assert "changelog" in entry["content"]
        assert entry["revision"] == 1, "pre-approval amend rewrites v1, no new revision"

    def test_pending_twin_shorter_clean_promotes_keeping_fuller_content(self, tmp_repo):
        eid, _, st = store.capture_user_constraint(
            tmp_repo,
            "always validate this feature before shipping to production environments", "s1")
        assert st == "pending_approval"
        eid2, content2, status2 = store.capture_user_constraint(
            tmp_repo, "always validate before shipping", "s2")
        assert eid2 == eid
        assert status2 == "promoted"
        entry = next(e for e in store._load(tmp_repo)["entries"] if e["id"] == eid)
        assert entry["status"] == "approved"
        assert "production environments" in entry["content"], "fuller content kept"

    def test_shorter_clean_restatement_of_approved_is_recurrence(self, tmp_repo):
        eid, _, st = store.capture_user_constraint(
            tmp_repo, "Always run the full integration suite before merging to main", "s1")
        assert st == "approved"
        result = store.capture_user_constraint(
            tmp_repo, "Always run the full integration suite", "s2")
        assert result == (None, None, None), "silent no-op like today's dup case"
        data = store._load(tmp_repo)
        decisions = [e for e in data["entries"] if e["type"] == "decision"]
        assert len(decisions) == 1, "recurrence of the existing rule, not a new entry"
        assert decisions[0]["occurrence_count"] == 2

    def test_synonym_phrasings_remain_two_entries(self, tmp_repo):
        # Pinned as the documented limitation: dedup is lexical, so the same rule in
        # different words stays separate — surfaced via the near-miss ack, merged manually.
        eid1, _, _ = store.capture_user_constraint(tmp_repo, self._SEED_LONG, "s1")
        eid2, _, _ = store.capture_user_constraint(tmp_repo, self._SEED_SHORT, "s1")
        assert eid1 is not None and eid2 is not None and eid1 != eid2
        data = store._load(tmp_repo)
        assert len([e for e in data["entries"] if e["type"] == "decision"]) == 2

    def test_near_miss_listed_in_ack_for_developer_confirmed_consolidation(self, tmp_repo):
        eid1, _, _ = store.capture_user_constraint(tmp_repo, self._SEED_LONG, "s1")
        near: list = []
        eid2, content2, status2 = store.capture_user_constraint(
            tmp_repo, self._SEED_SHORT, "s1", near)
        assert status2 == "approved"
        assert len(near) == 1 and eid1[:8] in near[0]
        ack = store.constraint_ack(content2, status2, eid2, near)
        assert eid1[:8] in ack
        assert "confirms" in ack.lower(), "consolidation gated on developer confirmation"
        assert "never merge on your own" in ack.lower()

    def test_containment_picks_best_match_not_first_hit(self, tmp_repo):
        # A short generic rule ("Always commit") is contained (ratio 1.0) in every longer
        # directive that mentions committing. First-hit-wins would route to whichever
        # happens to iterate first; the fix picks the closest overall match instead.
        data = store._load(tmp_repo)
        short = store._new_decision_entry("Always commit", "seed", "constraint",
                                          created_by="human", status="approved")
        longer = store._new_decision_entry("Always commit automatically", "seed", "constraint",
                                           created_by="human", status="approved")
        data["entries"].extend([short, longer])  # short iterates first
        store._save(tmp_repo, data)

        eid, _content, status = store.capture_user_constraint(
            tmp_repo, "Always commit automatically after approvals", "s1")
        assert status == "revision_proposed"
        assert eid == longer["id"], "routes to the closer match (higher overlap ratio)"
        assert eid != short["id"]

    def test_different_superset_does_not_clobber_pending_proposal(self, tmp_repo):
        store.capture_user_constraint(tmp_repo, self._SEED_LONG, "s1")
        eid2, _, _ = store.capture_user_constraint(tmp_repo, self._SEED_SHORT, "s1")
        store.capture_user_constraint(tmp_repo, self._SUPERSET, "s2")
        original_prop = next(e for e in store._load(tmp_repo)["entries"]
                             if e["id"] == eid2)["proposed_revision"]
        assert original_prop is not None

        different_superset = "Always commit automatically after every merge to keep history clean"
        eid3, content3, status3 = store.capture_user_constraint(tmp_repo, different_superset, "s3")
        assert eid3 == eid2, "still routes onto the same contained entry"
        assert status3 == "revision_already_pending"

        target = next(e for e in store._load(tmp_repo)["entries"] if e["id"] == eid2)
        assert target["proposed_revision"] == original_prop, "original proposal untouched"

        ack = store.constraint_ack(content3, status3, eid3)
        assert eid2[:8] in ack
        assert "already has a suggested update" in ack.lower()
        assert "not stored" in ack.lower()
        assert "contexer review" in ack.lower()
        assert content3.lower() in ack.lower(), "new phrasing surfaced so the developer can fold it in"

    def test_identical_superset_recapture_stays_idempotent_with_pending_proposal(self, tmp_repo):
        # Companion to test_superset_capture_is_idempotent: pins that a byte-identical
        # re-capture of the SAME superset never triggers the new clobber-guard status.
        store.capture_user_constraint(tmp_repo, self._SEED_LONG, "s1")
        eid2, _, _ = store.capture_user_constraint(tmp_repo, self._SEED_SHORT, "s1")
        store.capture_user_constraint(tmp_repo, self._SUPERSET, "s2")
        eid3, _content, status3 = store.capture_user_constraint(tmp_repo, self._SUPERSET, "s3")
        assert (eid3, status3) == (eid2, "revision_proposed")


class TestReviewPendingAndSharePreview:
    """Direct coverage for the on-demand review list and the cloud-push dry-run preview."""

    def test_format_pending_review_lists_id_content_actions(self, tmp_repo):
        store.update_decision(tmp_repo, "Never deploy on Fridays", "s1", "constraint")
        out = store.format_pending_review(tmp_repo)
        eid = store.get_pending_decisions(tmp_repo)[0]["id"][:8]
        assert "pending your review" in out
        assert "Never deploy on Fridays" in out
        assert "approve_decision" in out
        assert eid in out

    def test_format_pending_review_empty(self, tmp_repo):
        assert store.format_pending_review(tmp_repo) == "Nothing pending review."

    def test_format_pending_review_shows_title_and_quoted_body_when_distinct(self, tmp_repo):
        # Non-proposed branch routes through _title_and_body: title leads the bullet line,
        # and the (now-quoted) body only appears on its own line when it isn't a dup of the title.
        store.update_decision(
            tmp_repo, "Never deploy database migrations on Fridays before a long weekend",
            "s1", "constraint", title="No Friday migrations")
        out = store.format_pending_review(tmp_repo)
        lines = out.splitlines()
        head = next(l for l in lines if l.startswith("- "))
        assert "No Friday migrations" in head
        assert "Never deploy database migrations" not in head  # body not on the bullet line
        idx = lines.index(head)
        assert lines[idx + 1] == '    "Never deploy database migrations on Fridays before a long weekend"'

    def test_format_pending_review_short_untitled_dedups_no_quoted_line(self, tmp_repo):
        # Finding #1 applied to review_pending: short untitled content must not repeat as
        # both the bullet-line title and a quoted body line underneath.
        store.update_decision(tmp_repo, "Never deploy on Fridays", "s1", "constraint")
        out = store.format_pending_review(tmp_repo)
        lines = out.splitlines()
        head = next(l for l in lines if l.startswith("- "))
        assert "Never deploy on Fridays" in head
        # the very next line is the action line, not a quoted repeat of the content
        idx = lines.index(head)
        assert lines[idx + 1].strip().startswith("approve_decision(")


class TestPendingReviewFlag:
    """update_decision drops a PER-REPO .pending_review flag ONLY when it creates a state that
    awaits the developer (a new pending_approval entry, or a freshly-attached proposed_revision),
    so the next-prompt consumer can nudge mid-session. Auto/suggested captures and no-ops must not."""

    def test_new_pending_constraint_drops_flag(self, tmp_repo):
        store.update_decision(tmp_repo, "Never call the API without a request-id", "s", "constraint")
        assert store._pending_review_flag(tmp_repo).exists()

    def test_suggested_architecture_no_flag(self, tmp_repo):
        store.update_decision(tmp_repo, "Use Redis for caching", "s", "architecture")
        assert not store._pending_review_flag(tmp_repo).exists()

    def test_human_auto_decision_no_flag(self, tmp_repo):
        store.update_decision(tmp_repo, "Always use uv, not pip", "s", "convention", created_by="human")
        assert not store._pending_review_flag(tmp_repo).exists()

    def test_significant_update_drops_flag(self, tmp_repo):
        _ok, base = store.update_decision(tmp_repo, "Use Postgres for storage", "s",
                                          "architecture", created_by="human")
        assert not store._pending_review_flag(tmp_repo).exists()
        store.update_decision(tmp_repo, "Use MySQL for storage", "s", "architecture", replace_id=base)
        assert store._pending_review_flag(tmp_repo).exists()

    def test_noop_update_no_flag(self, tmp_repo):
        _ok, base = store.update_decision(tmp_repo, "Use Postgres for storage", "s",
                                          "architecture", created_by="human")
        store.update_decision(tmp_repo, "Use Postgres for storage", "s", "architecture", replace_id=base)
        assert not store._pending_review_flag(tmp_repo).exists()

    # ── pending_review_nudge (verified, per-repo consumer) ──────────────────────────
    def test_nudge_fires_and_clears_when_pending(self, tmp_repo):
        store.update_decision(tmp_repo, "Never deploy on Fridays", "s", "constraint")
        nudge = store.pending_review_nudge(tmp_repo)
        assert nudge and "pending your review" in nudge
        assert not store._pending_review_flag(tmp_repo).exists()  # fired once, cleared

    def test_nudge_none_after_approve(self, tmp_repo):
        # Greptile #1: flag set, then decision approved before the next prompt -> no false nudge.
        _ok, eid = store.update_decision(tmp_repo, "Never deploy on Fridays", "s", "constraint")
        store.approve_decision(tmp_repo, eid, "approve")
        assert store.pending_review_nudge(tmp_repo) is None
        assert not store._pending_review_flag(tmp_repo).exists()

    def test_nudge_none_without_flag(self, tmp_repo):
        assert store.pending_review_nudge(tmp_repo) is None

    def test_nudge_is_repo_scoped(self, tmp_repo, tmp_path):
        # Greptile #2: a pending decision in repo A must not nudge a session in repo B.
        repo_b = str(tmp_path / "repo_b")
        store.update_decision(tmp_repo, "Never deploy on Fridays", "s", "constraint")
        assert store.pending_review_nudge(repo_b) is None            # B sees nothing
        assert store._pending_review_flag(tmp_repo).exists()         # A's flag untouched
        assert store.pending_review_nudge(tmp_repo)                  # A still nudges

    def test_approve_decisions_batch_single_transaction(self, tmp_repo):
        # Batched approve: per-id results (failures surfaced), one load+save, no per-id rewrite.
        store.update_decision(tmp_repo, "Never log PII", "s", "constraint")
        store.update_decision(tmp_repo, "Never disable TLS verification", "s", "constraint")
        ids = [d["id"] for d in store.get_pending_decisions(tmp_repo)]
        results = store.approve_decisions(tmp_repo, ids + ["bogus"], "approve")
        assert [ok for _i, ok, _m in results] == [True, True, False]
        assert store.get_pending_decisions(tmp_repo) == []  # both real ones cleared atomically

    def test_session_start_escalates_growing_backlog(self, tmp_repo):
        data = store._load(tmp_repo)
        for i in range(store._BACKLOG_ESCALATE + 2):
            data["entries"].append(
                store._new_decision_entry(f"Constraint {i} distinct text {i}", "s",
                                          "constraint", status="pending_approval"))
        store._save(tmp_repo, data)
        result = store.get_session_start_context(tmp_repo)
        assert "piling up" in result["systemMessage"]
        assert 'entry_id="all"' in result["hookSpecificOutput"]["additionalContext"]

    def test_approve_decision_resolves_8char_prefix(self, tmp_repo):
        # review_pending shows 8-char ids in its approve_decision(...) instructions; approving
        # by that short id must resolve (Greptile: exact-only match returned 'not found').
        _ok, eid = store.update_decision(tmp_repo, "Never deploy on Fridays", "s", "constraint")
        ok, msg = store.approve_decision(tmp_repo, eid[:8], "approve")
        assert ok, msg
        assert not any(e["id"] == eid for e in store.get_pending_decisions(tmp_repo))

    def test_format_pending_review_caps_large_backlog(self, tmp_repo):
        # #1: a big backlog must not dump every decision — cap like get_context, point overflow
        # to `contexer review`. Build entries directly to bypass the novelty filter.
        data = store._load(tmp_repo)
        for i in range(30):
            data["entries"].append(
                store._new_decision_entry(f"Decision number {i} unique text {i}", "s",
                                          "constraint", status="pending_approval"))
        store._save(tmp_repo, data)
        out = store.format_pending_review(tmp_repo)
        assert f"showing {store._FILTERED_DISPLAY} of 30" in out
        assert "contexer review" in out
        # only the shown items are listed (each is a '- <id> …' bullet)
        assert sum(1 for ln in out.splitlines() if ln.startswith("- ")) == store._FILTERED_DISPLAY

    def test_format_share_preview_shows_content_endpoint_and_confirm(self, tmp_repo):
        store.update_decision(tmp_repo, "Use Redis for caching", "s1", "architecture")
        eid = store._load(tmp_repo)["entries"][0]["id"]
        out = store.format_share_preview(tmp_repo, eid)
        assert "Use Redis for caching" in out
        assert "PERSONAL cloud" in out
        assert "confirm=true" in out
        assert "skip_confirm" in out

    def test_format_share_preview_nothing_to_share(self, tmp_repo):
        assert "Nothing to share" in store.format_share_preview(tmp_repo, "no-such-id")

    def test_format_shareable_list(self, tmp_repo):
        store.update_decision(tmp_repo, "Use Postgres for primary storage", "s1", "architecture")
        out = store.format_shareable_list(tmp_repo)
        assert "available to share" in out
        assert "Use Postgres for primary storage" in out
        assert "share_decision" in out

    def test_format_shareable_list_empty(self, tmp_repo):
        assert store.format_shareable_list(tmp_repo) == "No decisions available to share."

    def test_format_share_preview_multi_lists_all_selected(self, tmp_repo):
        store.update_decision(tmp_repo, "Use Redis for caching", "s1", "architecture")
        store.update_decision(tmp_repo, "Store blobs in object storage", "s1", "architecture")
        ids = [e["id"][:8] for e in store._load(tmp_repo)["entries"]]
        out = store.format_share_preview(tmp_repo, ",".join(ids))
        assert "2 decisions" in out
        assert "Use Redis for caching" in out and "Store blobs in object storage" in out


# ── insight-detection caching (_cached_insight) ───────────────────────────────

class TestInsightCache:
    """_cached_insight wraps _detect_insight with a per-repo TTL cache file so repeated
    SessionStart/bootstrap-fallback/bootstrap_scan calls don't re-run ~6 git subprocesses."""

    @pytest.fixture
    def git_repo(self, tmp_path, monkeypatch):
        """Real git repo with global/system git config isolated; returns its path."""
        monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
        repo = tmp_path / "gitrepo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(
            ["git", "-c", "user.email=me@test.local", "-c", "user.name=T",
             "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-q", "-m", "c"],
            cwd=repo, check=True)
        subprocess.run(["git", "config", "user.email", "me@test.local"], cwd=repo, check=True)
        return str(repo)

    def _counting_git(self, monkeypatch):
        # Wraps the real store._git so call sites still get real answers, just counted.
        real_git = store._git
        calls = {"n": 0}

        def counting(repo_path, *args):
            calls["n"] += 1
            return real_git(repo_path, *args)

        monkeypatch.setattr(store, "_git", counting)
        return calls

    def test_cache_hit_skips_git(self, git_repo, monkeypatch):
        calls = self._counting_git(monkeypatch)
        first = store._cached_insight(git_repo)
        assert calls["n"] > 2  # first call is a real detection (~6 git calls)
        calls["n"] = 0
        second = store._cached_insight(git_repo)
        # cache hit = exactly the 2 cheap validation calls (user.email + HEAD),
        # never a full re-detection
        assert calls["n"] == 2
        assert second == first

    def test_changed_email_invalidates_cache(self, git_repo, monkeypatch):
        store._cached_insight(git_repo)  # warm the cache
        subprocess.run(["git", "config", "user.email", "someone-else@test.local"],
                       cwd=git_repo, check=True)
        seen = {}
        real_detect = store._detect_insight

        def spying_detect(repo_path):
            seen["called"] = True
            return real_detect(repo_path)

        monkeypatch.setattr(store, "_detect_insight", spying_detect)
        store._cached_insight(git_repo)
        assert seen.get("called")  # identity changed — cache must not be trusted

    def test_changed_head_invalidates_cache(self, git_repo, monkeypatch):
        store._cached_insight(git_repo)  # warm the cache
        subprocess.run(
            ["git", "-c", "user.email=me@test.local", "-c", "user.name=T",
             "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-q", "-m", "c2"],
            cwd=git_repo, check=True)
        seen = {}
        real_detect = store._detect_insight

        def spying_detect(repo_path):
            seen["called"] = True
            return real_detect(repo_path)

        monkeypatch.setattr(store, "_detect_insight", spying_detect)
        store._cached_insight(git_repo)
        assert seen.get("called")  # history moved — cache must not be trusted

    def test_expired_cache_redetects(self, git_repo, monkeypatch):
        path = store._insight_cache_path(git_repo)
        store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        stale_ts = time.time() - store._INSIGHT_CACHE_TTL - 1
        path.write_text(json.dumps({"level": "low", "decisive": False, "ts": stale_ts}))
        calls = self._counting_git(monkeypatch)
        store._cached_insight(git_repo)
        assert calls["n"] > 0  # expired entry — falls through to a fresh _detect_insight
        refreshed = json.loads(path.read_text())
        assert refreshed["ts"] > stale_ts

    def test_corrupt_cache_fails_soft(self, git_repo):
        path = store._insight_cache_path(git_repo)
        store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        path.write_bytes(b"\xff\x00not json")
        level, decisive = store._cached_insight(git_repo)
        assert level in store._INSIGHT_ORDER
        assert isinstance(decisive, bool)

    def test_cache_is_per_repo_slug(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
        repo_a, repo_b = str(tmp_path / "repo_a"), str(tmp_path / "repo_b")
        store._cached_insight(repo_a)
        store._cached_insight(repo_b)
        path_a, path_b = store._insight_cache_path(repo_a), store._insight_cache_path(repo_b)
        assert path_a != path_b
        assert path_a.exists() and path_b.exists()

    def test_bootstrap_scan_uses_cache(self, tmp_repo, monkeypatch):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        path = store._insight_cache_path(tmp_repo)
        # The stored key must match what _insight_cache_key returns at read time or
        # the cache is (rightly) distrusted. Derive it — `git config user.email`
        # falls back to global config even outside a repo, so it isn't simply None.
        email, head = store._insight_cache_key(tmp_repo)
        path.write_text(json.dumps({"level": "high", "decisive": True, "ts": time.time(),
                                    "email": email, "head": head}))

        def fail_if_called(repo_path):
            raise AssertionError("_detect_insight must not run when a fresh cache exists")

        monkeypatch.setattr(store, "_detect_insight", fail_if_called)
        result = store.bootstrap_scan(tmp_repo, "")
        assert result["insight"] == "high"
        assert result["insight_source"] == "auto"
        assert result["decisive"] is True


# ── Retrieval V1 (Part A): topic router — index, BM25, working set, log ────────

RV1_SESSION = "rv1-session"


def _seed_rv1(repo, items):
    """items: list of (content, subtype). Returns {content: decision_id}."""
    ids = {}
    for content, subtype in items:
        _, eid = store.update_decision(repo, content, RV1_SESSION, subtype)
        ids[content] = eid
    return ids


# A store spanning the topic facets, used by the BM25 router tests.
RV1_CORPUS = [
    ("Postgres migrations run through Alembic; the session layer catches OperationalError and retries on the pool", "architecture"),
    ("REST endpoints under the orders route return a JSON response envelope", "pattern"),
    ("JWT refresh tokens expire after fifteen minutes and live in httpOnly cookies", "architecture"),
    ("React components use CSS modules for styling across the dashboard", "pattern"),
    ("Docker images are built in CI and released through Helm charts", "convention"),
    ("Pytest fixtures live in conftest and mocks use the responses library", "convention"),
    ("Settings load from a TOML file validated at startup", "convention"),
    ("An in-process cache trims latency on the hot product listing path", "architecture"),
    ("Secrets are injected from the vault and inputs sanitized against injection", "constraint"),
    ("The service is organized as thin controllers delegating to a domain layer", "architecture"),
]


class TestDeriveTopics:
    def test_single_alias_hit(self):
        assert store._derive_topics("we migrated the postgres schema for orders") == ["db"]

    def test_multi_topic_sorted(self):
        topics = store._derive_topics("the react component calls a rest endpoint")
        assert topics == ["api", "frontend"]  # sorted, both facets present

    def test_no_alias_returns_empty(self):
        assert store._derive_topics("a plain sentence about widgets and gadgets") == []

    def test_case_insensitive(self):
        assert store._derive_topics("Using JWT for LOGIN flows") == ["auth"]

    def test_session_words_do_not_mean_auth(self):
        # "session" means agent sessions in this domain — it mis-tagged docs
        # questions as auth when it lived in the auth alias set.
        assert store._derive_topics("SessionStart hooks run each session") == []

    def test_compound_auth_session_phrases_still_mean_auth(self):
        # Greptile #123: genuine auth-session text carries no surviving alias
        # token; the compound-phrase check restores the tag.
        assert store._derive_topics("invalidate all user sessions on password change") == ["auth"]
        assert store._derive_topics("login sessions expire after thirty minutes") == ["auth"]


class TestRetrievalIndex:
    def test_written_by_save_on_update_decision(self, tmp_repo):
        store.update_decision(tmp_repo, "We chose postgres for the orders schema", RV1_SESSION, "architecture")
        idx = store._read_retrieval_index(tmp_repo)
        assert idx is not None
        assert idx["v"] == 1 and idx["n_docs"] == 1
        (doc,) = idx["docs"].values()
        assert "db" in doc["topics"]
        assert "postgres" in doc["tf"]

    def test_reflects_current_content_after_revision(self, tmp_repo):
        store.update_decision(tmp_repo, "Use redis for caching the product feed", RV1_SESSION, "architecture")
        data = store._load(tmp_repo)
        entry = data["entries"][0]
        store._append_revision(entry, "Use memcached for caching the product feed now", source="human")
        store._save(tmp_repo, data)
        (doc,) = store._read_retrieval_index(tmp_repo)["docs"].values()
        assert "memcached" in doc["tf"]      # current content indexed
        assert "redis" not in doc["tf"]      # superseded content dropped

    def test_missing_returns_none_and_creates_no_file(self, tmp_repo):
        assert store._read_retrieval_index(tmp_repo) is None
        assert not store._index_path(tmp_repo).exists()

    def test_corrupt_returns_none(self, tmp_repo):
        store.update_decision(tmp_repo, "Use postgres for storage layer", RV1_SESSION, "architecture")
        store._index_path(tmp_repo).write_text("{ not json")
        assert store._read_retrieval_index(tmp_repo) is None

    def test_wrong_version_returns_none(self, tmp_repo):
        store.update_decision(tmp_repo, "Use postgres for storage layer", RV1_SESSION, "architecture")
        p = store._index_path(tmp_repo)
        payload = json.loads(p.read_text())
        payload["v"] = 2
        p.write_text(json.dumps(payload))
        assert store._read_retrieval_index(tmp_repo) is None

    def test_indexes_only_decisions(self, tmp_repo):
        store.update_decision(tmp_repo, "Use postgres for storage layer", RV1_SESSION, "architecture")
        data = store._load(tmp_repo)
        data["entries"].append({"type": "task", "id": "task-1", "content": "a task not a decision"})
        store._save(tmp_repo, data)
        idx = store._read_retrieval_index(tmp_repo)
        assert idx["n_docs"] == 1
        assert "task-1" not in idx["docs"]


class TestBM25Router:
    def _id_by(self, ids, needle):
        return next(v for k, v in ids.items() if needle in k)

    def test_strong_match_injects_content_with_header(self, tmp_repo):
        _seed_rv1(tmp_repo, RV1_CORPUS)
        result = store.get_context_for_prompt(tmp_repo, "why do jwt refresh tokens live in cookies?")
        assert result.startswith("[Contexer: auto-fetched for this question]")
        assert "JWT" in result and "cookies" in result

    def test_bm25_two_term_beats_one_term_noise(self, tmp_repo):
        ids = _seed_rv1(tmp_repo, RV1_CORPUS + [
            ("orders are archived nightly to cold storage", "convention"),
            ("orders list uses cursor pagination with opaque cursors", "pattern"),
        ])
        idx = store._read_retrieval_index(tmp_repo)
        ranked = store._bm25_rank(["orders", "pagination"], idx)
        top_id = ranked[0][0]
        assert top_id == self._id_by(ids, "cursor pagination")   # 2 term hits wins
        assert ranked[0][2] == 2

    def test_relative_threshold_weak_second_not_injected(self, tmp_repo):
        _seed_rv1(tmp_repo, RV1_CORPUS)
        # "docker helm ci" hits the deploy doc on 3 terms; "layer" is a lone weak term
        # shared by other docs — it must not be injected as content alongside the strong hit.
        result = store.get_context_for_prompt(tmp_repo, "why did we choose docker helm ci for the layer?")
        assert "Docker images" in result
        assert "domain layer" not in result          # weak single-term doc excluded
        assert result.count("\n- ") + result.count("]\n- ") >= 0  # structural sanity

    def test_strong_cap_is_three(self, tmp_repo):
        _seed_rv1(tmp_repo, [
            ("caching alpha reduces the checkout latency budget", "architecture"),
            ("caching beta improves the profile latency budget", "architecture"),
            ("caching gamma speeds the search latency budget", "architecture"),
            ("caching delta trims the report latency budget", "architecture"),
        ])
        result = store.get_context_for_prompt(tmp_repo, "why does caching latency budget matter?")
        assert result.startswith("[Contexer: auto-fetched for this question]")
        assert result.count("\n- [") == 3           # capped at 3 rendered decisions

    def test_artifact_extraction_routes_paste_to_db(self, tmp_repo):
        _seed_rv1(tmp_repo, RV1_CORPUS)
        prompt = "fix this bug\n\nTraceback: app/db/session.py raised OperationalError in the pool"
        result = store.get_context_for_prompt(tmp_repo, prompt)
        assert "OperationalError" in result          # routed via artifacts, prose named no topic
        assert "Alembic" in result

    def test_rationale_boost_preserves_single_keyword_hit(self, tmp_repo):
        _seed_rv1(tmp_repo, RV1_CORPUS)
        # "alembic" hits exactly one doc with a single term — the rationale boost keeps it.
        result = store.get_context_for_prompt(tmp_repo, "why alembic?")
        assert result != ""
        assert "Alembic" in result

    def test_weak_topic_overlap_returns_pointer(self, tmp_repo):
        _seed_rv1(tmp_repo, RV1_CORPUS)
        # "why the schema design?" — "schema" is a db alias but matches no doc token exactly,
        # so no strong content; the db topic still overlaps stored docs → a pointer.
        result = store.get_context_for_prompt(tmp_repo, "why the schema design here?")
        assert result.startswith("[Contexer] Related stored decisions:")
        assert "db(" in result

    def test_miss_returns_empty(self, tmp_repo):
        _seed_rv1(tmp_repo, RV1_CORPUS)
        assert store.get_context_for_prompt(tmp_repo, "refactor the helper function") == ""
        assert store.get_context_for_prompt(tmp_repo, "why do birds fly south?") == ""


class TestRenderPromptDecisions:
    """_render_prompt_decisions feeds the BM25 strong-match auto-injection path — it must
    render the SAME two-line shape as get_context (title-bearing bullet line, then a
    `    `-indented content line), not the old title-less single line."""

    def test_two_line_shape_with_title_on_bullet_content_on_next(self, tmp_repo):
        _, id1 = store.update_decision(
            tmp_repo, "Use postgres for the storage layer", RV1_SESSION,
            "architecture", title="Postgres storage layer",
        )
        _, id2 = store.update_decision(
            tmp_repo, "JWT refresh tokens live in httpOnly cookies", RV1_SESSION,
            "architecture", title="JWT refresh in cookies",
        )
        rendered = store._render_prompt_decisions(tmp_repo, [id1, id2])
        lines = rendered.splitlines()
        assert len(lines) == 4   # 2 decisions x (bullet line + indented content line)

        assert lines[0].startswith("- [")
        assert "Postgres storage layer" in lines[0]
        assert "Use postgres for the storage layer" not in lines[0]   # content not on bullet
        assert lines[1] == "    Use postgres for the storage layer"

        assert lines[2].startswith("- [")
        assert "JWT refresh in cookies" in lines[2]
        assert lines[3] == "    JWT refresh tokens live in httpOnly cookies"

    def test_derives_title_when_none_stored_short_content_dedups_to_one_line(self, tmp_repo):
        # No explicit title -> falls back to _derive_title(content), same as get_context.
        # Content is short (<=100 chars) so the derived title IS the content verbatim —
        # showing it again on an indented line would just repeat it, so there's no 2nd line.
        _, eid = store.update_decision(
            tmp_repo, "Settings load from a TOML file validated at startup", RV1_SESSION,
            "convention",
        )
        rendered = store._render_prompt_decisions(tmp_repo, [eid])
        lines = rendered.splitlines()
        assert len(lines) == 1
        assert lines[0].startswith("- [")
        assert "Settings load from a TOML file validated at startup" in lines[0]

    def test_derives_title_when_none_stored_long_content_keeps_two_lines(self, tmp_repo):
        # Long content (>100 chars) derives a truncated title distinct from the full body,
        # so the body still gets its own indented line.
        long_content = ("Settings load from a TOML file validated at startup against a strict "
                        "schema before anything else in the app is allowed to run.")
        _, eid = store.update_decision(tmp_repo, long_content, RV1_SESSION, "convention")
        rendered = store._render_prompt_decisions(tmp_repo, [eid])
        lines = rendered.splitlines()
        assert len(lines) == 2
        assert lines[0].startswith("- [")
        assert long_content not in lines[0]
        assert lines[1] == f"    {long_content}"


class TestContextForPromptMeta:
    """get_context_for_prompt_with_meta hands back structured data instead of a caller
    (claude.rationale) having to scrape the rendered text."""

    def test_strong_hit_kind_and_count(self, tmp_repo):
        _seed_rv1(tmp_repo, RV1_CORPUS)
        text, meta = store.get_context_for_prompt_with_meta(
            tmp_repo, "why do jwt refresh tokens live in cookies?")
        assert meta["kind"] == "strong"
        assert meta["count"] == 1
        assert "auth" in meta["topics"]

    def test_pointer_kind_and_topics(self, tmp_repo):
        _seed_rv1(tmp_repo, RV1_CORPUS)
        text, meta = store.get_context_for_prompt_with_meta(
            tmp_repo, "why the schema design here?")
        assert meta["kind"] == "pointer"
        assert "db" in meta["topics"]
        assert meta["count"] >= 1

    def test_miss_kind_empty(self, tmp_repo):
        _seed_rv1(tmp_repo, RV1_CORPUS)
        text, meta = store.get_context_for_prompt_with_meta(tmp_repo, "refactor the helper function")
        assert text == ""
        assert meta == {"kind": "", "count": 0, "topics": []}

    @pytest.mark.parametrize("prompt", [
        "why do jwt refresh tokens live in cookies?",
        "why the schema design here?",
        "refactor the helper function",
        "why did we choose docker helm ci for the layer?",
    ])
    def test_drift_pin_matches_public_api(self, tmp_repo, prompt):
        # get_context_for_prompt and get_context_for_prompt_with_meta must never diverge —
        # both are thin wrappers over the same private implementation.
        _seed_rv1(tmp_repo, RV1_CORPUS)
        assert store.get_context_for_prompt(tmp_repo, prompt) == \
            store.get_context_for_prompt_with_meta(tmp_repo, prompt)[0]


class TestWorkingSet:
    def test_second_identical_prompt_not_reinjected(self, tmp_repo):
        _seed_rv1(tmp_repo, RV1_CORPUS)
        sid = "sess-ws"
        first = store.get_context_for_prompt(tmp_repo, "why do jwt refresh tokens expire in httpOnly cookies?", sid)
        assert first.startswith("[Contexer: auto-fetched for this question]")
        second = store.get_context_for_prompt(tmp_repo, "why do jwt refresh tokens expire in httpOnly cookies?", sid)
        assert "auto-fetched" not in second          # already in the working set → not re-injected

    def test_different_session_reinjects(self, tmp_repo):
        _seed_rv1(tmp_repo, RV1_CORPUS)
        store.get_context_for_prompt(tmp_repo, "why do jwt refresh tokens expire in httpOnly cookies?", "sess-a")
        other = store.get_context_for_prompt(tmp_repo, "why do jwt refresh tokens expire in httpOnly cookies?", "sess-b")
        assert other.startswith("[Contexer: auto-fetched for this question]")

    def test_empty_session_no_dedup_no_file(self, tmp_repo):
        _seed_rv1(tmp_repo, RV1_CORPUS)
        a = store.get_context_for_prompt(tmp_repo, "why do jwt refresh tokens expire in httpOnly cookies?")
        b = store.get_context_for_prompt(tmp_repo, "why do jwt refresh tokens expire in httpOnly cookies?")
        assert a == b and a.startswith("[Contexer: auto-fetched for this question]")
        assert store._ws_path(tmp_repo, "").exists() is False

    def test_working_set_ids_public_helper(self, tmp_repo):
        ids = _seed_rv1(tmp_repo, RV1_CORPUS)
        sid = "sess-helper"
        store.get_context_for_prompt(tmp_repo, "why do jwt refresh tokens expire in httpOnly cookies?", sid)
        jwt_id = next(v for k, v in ids.items() if "JWT" in k)
        assert jwt_id in store.working_set_ids(tmp_repo, sid)
        assert store.working_set_ids(tmp_repo, "") == []


class TestLegacyFallback:
    def test_rationale_hit_byte_identical_to_legacy(self, tmp_repo, monkeypatch):
        store.update_decision(tmp_repo, "We chose postgres over mongo for ACID transactions", RV1_SESSION, "architecture")
        monkeypatch.setattr(store, "_read_retrieval_index", lambda repo: None)
        result = store.get_context_for_prompt(tmp_repo, "why did we choose postgres?")
        expected = "[Contexer: auto-fetched for this question]\n" + store.get_context(tmp_repo, query="postgres")
        assert result == expected

    def test_miss_byte_identical_to_legacy(self, tmp_repo, monkeypatch):
        store.update_decision(tmp_repo, "We chose postgres over mongo", RV1_SESSION, "architecture")
        monkeypatch.setattr(store, "_read_retrieval_index", lambda repo: None)
        assert store.get_context_for_prompt(tmp_repo, "add a new endpoint here") == ""


class TestRetrievalLog:
    def test_pointer_event_appended(self, tmp_repo):
        _seed_rv1(tmp_repo, RV1_CORPUS)
        store.get_context_for_prompt(tmp_repo, "why the schema design here?", "sess-log")
        path = store.STORE_DIR / f".retrieval_{store._slug(tmp_repo)}.jsonl"
        assert path.exists()
        events = [json.loads(l) for l in path.read_text().splitlines() if l]
        assert events[-1]["e"] == "pointer"
        assert "db" in events[-1]["topics"]
        assert events[-1]["sid"] == "sess-log"

    def test_log_cap_enforced(self, tmp_repo):
        for i in range(store._RETRIEVAL_LOG_CAP + 25):
            store._retrieval_log(tmp_repo, {"e": "pointer", "topics": ["db"], "sid": "s", "ts": i})
        path = store.STORE_DIR / f".retrieval_{store._slug(tmp_repo)}.jsonl"
        lines = [l for l in path.read_text().splitlines() if l]
        assert len(lines) == store._RETRIEVAL_LOG_CAP
        assert json.loads(lines[-1])["ts"] == store._RETRIEVAL_LOG_CAP + 24  # tail kept

    def test_corrupt_log_ignored(self, tmp_repo):
        path = store.STORE_DIR / f".retrieval_{store._slug(tmp_repo)}.jsonl"
        store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        path.write_text("not\x00valid json line\n")
        # append still succeeds (fail-soft rewrite), never raises
        store._retrieval_log(tmp_repo, {"e": "pointer", "topics": ["api"], "sid": "s", "ts": 1})
        assert path.exists()


def _ignore(repo, eid):
    data = store._load(repo)
    for e in data["entries"]:
        if e["id"] == eid:
            e["status"] = "ignored"
    store._save(repo, data)


# ── Retrieval V1 review findings: indexed path must dominate legacy ─────────────

class TestIndexStatusFilter:
    def test_ignored_not_indexed_pending_is(self, tmp_repo):
        _, ig = store.update_decision(tmp_repo, "Use postgres for the ledger storage layer",
                                      RV1_SESSION, "architecture")
        _ignore(tmp_repo, ig)
        # a constraint is born pending_approval — retrievable via get_context (tagged
        # [pending]), so it MUST be indexed; only ignored is excluded (Greptile #117).
        store.update_decision(tmp_repo, "Never log postgres credentials in plaintext",
                              RV1_SESSION, "constraint")
        idx = store._read_retrieval_index(tmp_repo)
        assert idx["n_docs"] == 1
        assert all(d["status"] == "pending_approval" for d in idx["docs"].values())

    def test_pending_only_match_injects_tagged(self, tmp_repo):
        # Dominance parity: legacy surfaces a pending-only rationale match via
        # get_context, so the indexed path must inject it too (with the pending tag).
        store.update_decision(tmp_repo, "Chose Kafka over RabbitMQ for the event backbone ordering",
                              RV1_SESSION, "constraint")
        result = store.get_context_for_prompt(tmp_repo, "why did we choose kafka for the event backbone?")
        assert "Kafka over RabbitMQ" in result
        assert "[pending]" in result

    def test_approved_wins_over_ignored(self, tmp_repo):
        _, ig = store.update_decision(tmp_repo, "JWT tokens are validated in middleware auth checks",
                                      RV1_SESSION, "architecture")
        _ignore(tmp_repo, ig)
        store.update_decision(tmp_repo,
                              "We rejected long-lived jwt refresh cookies for middleware security reasons",
                              RV1_SESSION, "architecture", created_by="human")
        result = store.get_context_for_prompt(tmp_repo, "why do we use jwt tokens in middleware?")
        assert "rejected long-lived" in result                 # approved content injected
        assert "validated in middleware auth checks" not in result   # ignored never surfaces


class TestBM25PrefixAndDigits:
    def test_prefix_query_matches_indexed_token(self, tmp_repo):
        store.update_decision(tmp_repo, "Use PostgreSQL as the primary database with SQLAlchemy",
                              RV1_SESSION, "architecture", created_by="human")
        result = store.get_context_for_prompt(tmp_repo, "why did we pick postgres for storage?")
        assert "PostgreSQL" in result       # 'postgres' prefix-expanded to 'postgresql'

    def test_digit_bearing_term_reaches_ranker(self, tmp_repo):
        store.update_decision(tmp_repo, "We deploy the service on k8s clusters through helm charts",
                              RV1_SESSION, "architecture", created_by="human")
        result = store.get_context_for_prompt(tmp_repo, "why did we pick k8s for this?")
        assert "k8s" in result              # digit-bearing term survives tokenization

    def test_bm25_prefix_expansion_aggregates_df(self, tmp_repo):
        _seed_rv1(tmp_repo, RV1_CORPUS)
        idx = store._read_retrieval_index(tmp_repo)
        # 'postgres' is absent as an exact token but 'postgres'-prefixed tokens exist
        ranked = store._bm25_rank(["postgres"], idx)
        assert ranked and ranked[0][2] == 1


class TestProjectRelaxation:
    def test_project_single_keyword_injects_content(self, tmp_repo):
        store.update_decision(tmp_repo, "Use PostgreSQL as the primary database with SQLAlchemy",
                              RV1_SESSION, "architecture", created_by="human")
        result = store.get_context_for_prompt(tmp_repo, "what is the goal for the database layer?")
        assert result.startswith("[Contexer: auto-fetched for this question]")
        assert "PostgreSQL" in result


class TestArtifactRouteGate:
    def test_prose_slashes_are_not_routes(self):
        for prose in ("light/dark mode", "read/write splitting", "either/or choice"):
            assert store._ARTIFACT_ROUTE_RE.findall(prose) == [], prose
            assert store._extract_artifacts(prose) == [], prose

    def test_real_route_matches(self):
        assert store._ARTIFACT_ROUTE_RE.findall("GET /api/users/{id} returns json") == ["/api/users/{id}"]
        arts = store._extract_artifacts("GET /api/users/{id} returns json")
        assert "users" in arts and "api" in arts

    def test_artifact_only_prompt_no_global_leak(self, tmp_repo):
        # An index must exist (a repo decision) so the BM25 path — not legacy — runs.
        store.update_decision(tmp_repo, "The cart service owns checkout totals",
                              RV1_SESSION, "architecture", created_by="human")
        store.update_global_decision("Always rename modules using git mv to preserve history",
                                     RV1_SESSION, "convention")
        # not rationale, not project — only an artifact (utils.py). Legacy was silent here.
        assert store.get_context_for_prompt(tmp_repo, "please rename utils.py for me") == ""


class TestTopicAliasRetry:
    def test_bare_topic_query_falls_back_to_aliases(self, tmp_repo):
        _seed_rv1(tmp_repo, RV1_CORPUS)
        # No decision literally contains 'db', but the pointer nudge suggests query='db'.
        result = store.get_context(tmp_repo, query="db")
        assert "Alembic" in result                      # postgres/migration alias hit
        assert "No matching decisions" not in result

    def test_literal_match_unchanged_when_present(self, tmp_repo):
        _seed_rv1(tmp_repo, RV1_CORPUS)
        result = store.get_context(tmp_repo, query="jwt")
        assert "JWT refresh tokens" in result

    def test_no_result_query_logs_no_followup(self, tmp_repo):
        _seed_rv1(tmp_repo, RV1_CORPUS)
        # Arm a fresh pointer for the db topic.
        store.get_context_for_prompt(tmp_repo, "why the schema design here?", "sess-fu")
        path = store.STORE_DIR / f".retrieval_{store._slug(tmp_repo)}.jsonl"
        before = path.read_text().count('"followup"')
        # A get_context that found nothing must not log a follow-through, even in-window.
        store.log_followup_if_matching(tmp_repo, "db", found=False)
        assert path.read_text().count('"followup"') == before
        # A genuine hit does log one.
        store.log_followup_if_matching(tmp_repo, "db", found=True)
        assert path.read_text().count('"followup"') == before + 1


class TestWsPathSanitized:
    def test_slashed_session_id_stays_in_store_dir(self, tmp_repo):
        sid = "proj/abc123"
        p = store._ws_path(tmp_repo, sid)
        assert p.parent == store.STORE_DIR          # no nested path escape
        _seed_rv1(tmp_repo, RV1_CORPUS)
        first = store.get_context_for_prompt(tmp_repo, "why do jwt refresh tokens expire in httpOnly cookies?", sid)
        assert first.startswith("[Contexer: auto-fetched for this question]")
        assert p.exists()                           # ws file created directly in STORE_DIR
        second = store.get_context_for_prompt(tmp_repo, "why do jwt refresh tokens expire in httpOnly cookies?", sid)
        assert "auto-fetched" not in second         # dedup works across calls with that id

    def test_shared_32char_prefix_does_not_collide(self, tmp_repo):
        # Greptile #117: ids sharing the first 32 chars must not share a working set.
        base = "project-alpha-2026-07-15-morning-run"   # >32 chars
        a, b = base + "-A", base + "-B"
        assert store._ws_path(tmp_repo, a) != store._ws_path(tmp_repo, b)
        _seed_rv1(tmp_repo, RV1_CORPUS)
        first = store.get_context_for_prompt(tmp_repo, "why do jwt refresh tokens expire in httpOnly cookies?", a)
        assert "auto-fetched" in first
        other = store.get_context_for_prompt(tmp_repo, "why do jwt refresh tokens expire in httpOnly cookies?", b)
        assert "auto-fetched" in other              # session B is not poisoned by A's working set


# Two distinct docs (below the 70% novelty threshold) covering every hit class.
DOMINANCE_CORPUS = [
    ("We chose PostgreSQL as the primary database for ACID order processing across the data layer", "architecture"),
    ("The session catches OperationalError from app/db/session.py and retries on the pool", "architecture"),
]


class TestIndexDominatesLegacy:
    """Pins the primary (indexed) path to never inject less than its legacy fallback."""

    @pytest.mark.parametrize("prompt,needle,is_miss", [
        ("why did we choose postgresql for storage?", "PostgreSQL", False),   # rationale exact-token
        ("why did we pick postgres here?", "PostgreSQL", False),              # rationale prefix-form
        ("what is the goal for the database layer?", "PostgreSQL", False),    # project single keyword
        ("Traceback:\napp/db/session.py raised OperationalError", "OperationalError", False),  # artifact paste
        ("refactor the helper function", "", True),                          # task/miss
        ("why do birds fly south?", "", True),                               # rationale word, no domain match
    ])
    def test_indexed_at_least_as_good_as_legacy(self, tmp_repo, prompt, needle, is_miss):
        for content, subtype in DOMINANCE_CORPUS:
            store.update_decision(tmp_repo, content, RV1_SESSION, subtype, created_by="human")
        indexed = store.get_context_for_prompt(tmp_repo, prompt)
        store._index_path(tmp_repo).unlink(missing_ok=True)   # force the legacy path
        legacy = store.get_context_for_prompt(tmp_repo, prompt)
        if is_miss:
            assert indexed == "" and legacy == ""
        else:
            if needle in legacy:                              # never worse than fallback
                assert needle in indexed
            assert needle in indexed                          # and the indexed path actually recalls it


# ── Retrieval V1 (Part B): session-start integration ────────────────────────────────

# Distinct-enough-to-avoid-novelty-dedup filler so a store can clear the 20-decision gate
# for the standing-map tests without colliding with RV1_CORPUS content.
RV1_EXTRA = [
    ("The billing service uses stripe webhooks for payment reconciliation", "architecture"),
    ("Search indexing runs nightly through a cron job in the etl pipeline", "convention"),
    ("Feature flags are managed through launchdarkly for gradual rollout", "pattern"),
    ("Error monitoring reports crash telemetry to sentry", "convention"),
    ("The notification worker retries failed emails three times", "pattern"),
    ("User uploads are stored in an s3 bucket with versioning enabled", "architecture"),
    ("Rate limiting caps external calls at 100 requests per minute", "constraint"),
    ("The admin dashboard uses server side rendering for faster loads", "pattern"),
    ("Background jobs run through a sidekiq style queue worker", "architecture"),
    ("Analytics events are batched before being sent to the warehouse", "convention"),
    ("The mobile app syncs offline changes using a conflict free merge", "pattern"),
    ("Internationalization strings are loaded from json locale files", "convention"),
]


class TestStandingTopicMap:
    def test_map_appears_at_20_plus_decisions(self, tmp_repo):
        assert len(RV1_CORPUS) + len(RV1_EXTRA) >= 20
        _seed_rv1(tmp_repo, RV1_CORPUS + RV1_EXTRA)
        result = store.get_session_start_context(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Stored decisions by topic:" in ctx
        assert "get_context(query=" in ctx

    def test_map_absent_below_20(self, tmp_repo):
        _seed_rv1(tmp_repo, RV1_CORPUS)  # 10 decisions — below the gate
        result = store.get_session_start_context(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Stored decisions by topic:" not in ctx

    def test_map_absent_when_index_missing(self, tmp_repo):
        _seed_rv1(tmp_repo, RV1_CORPUS + RV1_EXTRA)
        store._index_path(tmp_repo).unlink()  # simulate a missing index sidecar
        result = store.get_session_start_context(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Stored decisions by topic:" not in ctx


class TestCompactRehydration:
    def test_rehydrates_capped_and_active_only(self, tmp_repo):
        items = RV1_CORPUS + RV1_EXTRA  # 22 mutually-distinct decisions, nothing deduped
        ids_by_content = _seed_rv1(tmp_repo, items)
        ids = [ids_by_content[c] for c, _ in items]
        assert all(ids)  # sanity: no novelty-filter dedup collapsed any of these
        sid = "sess-compact"
        # Ignore one of the 10 most-recently-injected ids — it must never be rehydrated
        # even though it's in the working set.
        ignored_id = ids[13]  # RV1_EXTRA[3] — "...crash telemetry to sentry", within the last 10
        store.approve_decision(tmp_repo, ignored_id, "ignore")
        store._ws_add(tmp_repo, sid, ids)  # simulate all 22 injected earlier this session

        result = store.get_session_start_context(tmp_repo, "compact", sid)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "## Rehydrated working context" in ctx
        section = ctx.split("## Rehydrated working context:")[1]
        assert section.count("\n- [") <= 10
        assert "sentry" not in section  # ids[13]'s content — excluded (ignored, not active)

    def test_no_session_id_no_rehydration(self, tmp_repo):
        store.update_decision(tmp_repo, "Use postgres for storage", RV1_SESSION, "architecture")
        result = store.get_session_start_context(tmp_repo, "compact")
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Rehydrated working context" not in ctx

    def test_no_working_set_no_rehydration(self, tmp_repo):
        store.update_decision(tmp_repo, "Use postgres for storage", RV1_SESSION, "architecture")
        result = store.get_session_start_context(tmp_repo, "compact", "sess-empty-ws")
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "Rehydrated working context" not in ctx

    def test_rehydrate_working_set_shows_title_via_helper(self, tmp_repo):
        # _rehydrate_working_set must route through _title_and_body: the bullet line
        # leads with the title, and the content only repeats on an indented line when
        # it's not a duplicate of the title.
        # created_by="human" -> auto-approved (see _classify_level), so the entry is
        # deterministically "active" and passes _rehydrate_working_set's status filter.
        _, eid = store.update_decision(
            tmp_repo, "Use postgres for storage, not sqlite", RV1_SESSION,
            "architecture", created_by="human", title="Postgres over sqlite")
        store._ws_add(tmp_repo, "sess-title", [eid])
        rendered = store._rehydrate_working_set(tmp_repo, "sess-title")
        lines = rendered.splitlines()
        head = next(l for l in lines if l.startswith("- ["))
        assert "Postgres over sqlite" in head
        assert "Use postgres for storage, not sqlite" not in head  # not on the bullet
        idx = lines.index(head)
        assert lines[idx + 1] == "    Use postgres for storage, not sqlite"


class TestWorkingSetGC:
    def test_gc_removes_stale_and_keeps_fresh(self, tmp_repo):
        store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        slug = store._slug(tmp_repo)
        stale_ws = store.STORE_DIR / f".ws_{slug}_stale.json"
        fresh_ws = store.STORE_DIR / f".ws_{slug}_fresh.json"
        stale_log = store.STORE_DIR / f".retrieval_{slug}.jsonl"
        stale_ws.write_text('{"injected": [], "ts": 0}')
        fresh_ws.write_text('{"injected": [], "ts": 0}')
        stale_log.write_text('{"e": "pointer"}\n')
        old = time.time() - store._WS_GC_AGE_SECONDS - 3600
        os.utime(stale_ws, (old, old))
        os.utime(stale_log, (old, old))
        # fresh_ws keeps the mtime from the write above (just now)

        store._gc_stale_session_files()

        assert not stale_ws.exists()
        assert not stale_log.exists()
        assert fresh_ws.exists()

    def test_gc_runs_at_non_resume_session_start(self, tmp_repo):
        store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        stale_ws = store.STORE_DIR / f".ws_{store._slug(tmp_repo)}_old.json"
        stale_ws.write_text('{"injected": [], "ts": 0}')
        old = time.time() - store._WS_GC_AGE_SECONDS - 3600
        os.utime(stale_ws, (old, old))
        store.get_session_start_context(tmp_repo)  # non-resume start
        assert not stale_ws.exists()

    def test_gc_skipped_on_resume(self, tmp_repo):
        store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        stale_ws = store.STORE_DIR / f".ws_{store._slug(tmp_repo)}_old.json"
        stale_ws.write_text('{"injected": [], "ts": 0}')
        old = time.time() - store._WS_GC_AGE_SECONDS - 3600
        os.utime(stale_ws, (old, old))
        store.update_decision(tmp_repo, "Use postgres for storage", RV1_SESSION, "architecture")
        store.get_session_start_context(tmp_repo, "resume")
        assert stale_ws.exists()  # resume takes the early-return path — GC never runs


class TestRationaleSessionIdPlumbing:
    def test_rationale_passes_session_id_dedups_second_prompt(self, tmp_repo):
        from contexer.adapters import claude
        _seed_rv1(tmp_repo, RV1_CORPUS)
        raw = json.dumps({
            "prompt": "why do jwt refresh tokens expire in httpOnly cookies?",
            "session_id": "claude-sess-1",
        })
        first = json.loads(claude.rationale(tmp_repo, raw))
        assert "additionalContext" in first["hookSpecificOutput"]
        assert "auto-fetched" in first["hookSpecificOutput"]["additionalContext"]
        second = json.loads(claude.rationale(tmp_repo, raw))  # same prompt, same session
        second_ctx = second.get("hookSpecificOutput", {}).get("additionalContext", "")
        assert "auto-fetched" not in second_ctx  # already in the working set -> not re-injected

    def test_injection_is_observable(self, tmp_repo):
        # The developer sees WHAT was recalled (systemMessage, user-facing); a routine
        # small injection stays silent about cost. The model is told the fetch already
        # happened so it doesn't re-call get_context.
        from contexer.adapters import claude
        _seed_rv1(tmp_repo, RV1_CORPUS)
        raw = json.dumps({
            "prompt": "why do jwt refresh tokens expire in httpOnly cookies?",
            "session_id": "claude-sess-obs",
        })
        out = json.loads(claude.rationale(tmp_repo, raw))
        msg = out["systemMessage"]
        assert msg.startswith("Contexer: recalled 1 decision")
        assert "tokens" not in msg  # small injection -> cost note suppressed
        ctx = out["hookSpecificOutput"]["additionalContext"]
        assert ctx.startswith("[Contexer: auto-fetched for this question]")
        assert "no get_context call needed" in ctx

    def test_large_injection_flags_cost(self, tmp_repo):
        # Cost-on-exception: only an injection above _COST_NOTE_TOKENS carries the estimate.
        from contexer.adapters import claude
        long_tail = " because " + " ".join(f"reason{i} pooling latency" for i in range(80))
        store.update_decision(
            tmp_repo, "Chose postgres for the orders database" + long_tail,
            RV1_SESSION, "architecture", created_by="scan")
        raw = json.dumps({
            "prompt": "why did we choose postgres for the orders database?",
            "session_id": "claude-sess-big",
        })
        out = json.loads(claude.rationale(tmp_repo, raw))
        msg = out["systemMessage"]
        assert msg.startswith("Contexer: recalled 1 decision")
        assert "· ~" in msg and "tokens" in msg

    def test_no_injection_no_system_message(self, tmp_repo):
        from contexer.adapters import claude
        _seed_rv1(tmp_repo, RV1_CORPUS)
        raw = json.dumps({"prompt": "hello there", "session_id": "claude-sess-quiet"})
        assert claude.rationale(tmp_repo, raw) == "{}"

    def test_different_session_id_reinjects(self, tmp_repo):
        from contexer.adapters import claude
        _seed_rv1(tmp_repo, RV1_CORPUS)
        raw_a = json.dumps({
            "prompt": "why do jwt refresh tokens expire in httpOnly cookies?",
            "session_id": "claude-sess-a",
        })
        raw_b = json.dumps({
            "prompt": "why do jwt refresh tokens expire in httpOnly cookies?",
            "session_id": "claude-sess-b",
        })
        claude.rationale(tmp_repo, raw_a)
        second = json.loads(claude.rationale(tmp_repo, raw_b))
        assert "additionalContext" in second["hookSpecificOutput"]


class TestFollowThroughLog:
    def test_followup_logged_on_matching_query(self, tmp_repo):
        _seed_rv1(tmp_repo, RV1_CORPUS)
        store.get_context_for_prompt(tmp_repo, "why the schema design here?", "sess-follow")
        path = store.STORE_DIR / f".retrieval_{store._slug(tmp_repo)}.jsonl"
        assert json.loads(path.read_text().splitlines()[-1])["e"] == "pointer"

        store.log_followup_if_matching(tmp_repo, "db")

        events = [json.loads(l) for l in path.read_text().splitlines() if l]
        assert events[-1]["e"] == "followup"
        assert events[-1]["query"] == "db"

    def test_no_followup_when_topic_does_not_match(self, tmp_repo):
        _seed_rv1(tmp_repo, RV1_CORPUS)
        store.get_context_for_prompt(tmp_repo, "why the schema design here?", "sess-follow2")
        path = store.STORE_DIR / f".retrieval_{store._slug(tmp_repo)}.jsonl"
        before = path.read_text()

        store.log_followup_if_matching(tmp_repo, "frontend")

        assert path.read_text() == before  # no matching topic -> nothing appended

    def test_no_pointer_no_followup_no_crash(self, tmp_repo):
        store.log_followup_if_matching(tmp_repo, "db")  # nothing logged yet, must not raise
        path = store.STORE_DIR / f".retrieval_{store._slug(tmp_repo)}.jsonl"
        assert not path.exists()


# ── Title helpers (Task 1) ─────────────────────────────────────────────────────

class TestTitleHelpers:
    def test_normalize_flattens_and_strips(self):
        assert store._normalize_title("  hello\n  world \t") == "hello world"

    def test_normalize_truncates_with_ellipsis(self):
        long = "x" * 150
        out = store._normalize_title(long)
        assert len(out) == store.MAX_TITLE_LEN
        assert out.endswith("…")

    def test_normalize_empty(self):
        assert store._normalize_title("   \n ") == ""

    def test_derive_verbatim_when_short(self):
        c = "Never commit spec or plan files to git."
        assert store._derive_title(c) == c  # <= 100 -> whole content, no ellipsis

    def test_derive_first_sentence_when_long(self):
        c = ("Native contexer-teams entry removed. Team sync is the Python path; "
             "kept a legacy janitor pop; login now refreshes status.")
        out = store._derive_title(c)
        assert out.startswith("Native contexer-teams entry removed.")
        assert len(out) <= store.MAX_TITLE_LEN

    def test_derive_truncates_long_first_sentence(self):
        c = "A " + "very " * 60 + "long first sentence with no early period"
        out = store._derive_title(c)
        assert len(out) == store.MAX_TITLE_LEN and out.endswith("…")

    def test_derive_empty(self):
        assert store._derive_title("") == ""


# ── _title_and_body: the shared render primitive (findings #1-#3) ──────────────

class TestTitleAndBody:
    """_title_and_body(entry) -> (title, body). body is None whenever it would just repeat
    the title on an indented second line — the dedup fix for finding #1."""

    def test_short_untitled_dedups_body_to_none(self):
        # No authored title, content <=100 chars -> derived title IS the content ->
        # body is None so callers don't print the same text twice.
        c = "Never commit spec or plan files to git."
        e = store._new_decision_entry(c, "s1", "architecture")
        title, body = store._title_and_body(e)
        assert title == c
        assert body is None

    def test_long_untitled_keeps_full_body(self):
        # No authored title, content >100 chars -> derived title is a truncated first
        # sentence, distinct from the full body -> body is the full content.
        c = ("Native contexer-teams entry removed. Team sync is the Python path; "
             "kept a legacy janitor pop; login now refreshes status.")
        e = store._new_decision_entry(c, "s1", "architecture")
        title, body = store._title_and_body(e)
        assert title == store._derive_title(c)
        assert title != c
        assert body == c

    def test_authored_title_distinct_from_content_keeps_body(self):
        # Authored title differs from content (even short content) -> body still renders,
        # since it is NOT a repeat of the title.
        c = "Use Postgres for the queue."
        e = store._new_decision_entry(c, "s1", "architecture", title="Queue backend: Postgres")
        title, body = store._title_and_body(e)
        assert title == "Queue backend: Postgres"
        assert body == c

    def test_content_param_overrides_current_revision(self):
        # Explicit `content` (e.g. a proposed_revision's content) is used instead of the
        # entry's current revision.
        e = store._new_decision_entry("Original short body.", "s1", "architecture")
        title, body = store._title_and_body(e, content="A different candidate body.")
        assert title == e["title"]  # authored/derived title on the entry itself is unchanged
        assert body == "A different candidate body."


# ── Title on entry + first revision (Task 2) ───────────────────────────────────

class TestTitleOnEntry:
    def test_authored_title_wins(self):
        e = store._new_decision_entry("some long body " * 10, "s1", "architecture",
                                       title="Short authored title")
        assert e["title"] == "Short authored title"
        assert store._current_revision(e)["title"] == "Short authored title"

    def test_derived_when_omitted(self):
        e = store._new_decision_entry("Use Postgres for the queue.", "s1", "architecture")
        assert e["title"] == "Use Postgres for the queue."  # short -> verbatim
        assert store._current_revision(e)["title"] == "Use Postgres for the queue."

    def test_sync_cache_mirrors_revision_title(self):
        e = store._new_decision_entry("Body.", "s1", "architecture", title="T1")
        store._current_revision(e)["title"] = "T2"
        store._sync_decision_cache(e)
        assert e["title"] == "T2"


# ── Title through update paths (Task 3) ───────────────────────────────────────

class TestTitleRevision:
    def test_revision_rederives_title_when_omitted(self, tmp_repo):
        _, eid = store.update_decision(tmp_repo, "Original short body.", "s1",
                                       subtype="architecture", created_by="human", title="Original title")
        # revise with new content, no title -> re-derive from new content
        store.update_decision(tmp_repo, "Brand new short body.", "s1",
                              subtype="architecture", created_by="human", replace_id=eid[:8])
        d = next(e for e in store._load(tmp_repo)["entries"] if e["id"] == eid)
        assert d["title"] == "Brand new short body."   # re-derived, not carried forward
        assert d["revision"] == 2

    def test_revision_uses_authored_title(self, tmp_repo):
        _, eid = store.update_decision(tmp_repo, "Body one.", "s1", subtype="architecture", created_by="human")
        store.update_decision(tmp_repo, "Body two.", "s1", subtype="architecture",
                              created_by="human", replace_id=eid[:8], title="Explicit new title")
        d = next(e for e in store._load(tmp_repo)["entries"] if e["id"] == eid)
        assert d["title"] == "Explicit new title"

    def test_gated_proposal_carries_title_through_approval(self, tmp_repo):
        # An AI-authored change to an architecture/constraint decision routes through the
        # approval gate (Suggested Update) instead of revising immediately - the title must
        # survive capture -> proposal -> promoted revision.
        store.update_decision(tmp_repo, "Rollback endpoint is /api/v1/rollback", "s1", "architecture")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e.get("type") == "decision")
        eid = entry["id"]
        entry["status"] = "approved"
        store._save(tmp_repo, data)

        # default created_by="ai" -> significant change on an architecture decision is gated.
        ok, rid = store.update_decision(
            tmp_repo, "Rollback endpoint is /api/v2/rollback", "s2",
            subtype="architecture", replace_id=eid, title="Gated new title")
        assert ok is True and rid == eid

        gated = next(e for e in store._load(tmp_repo)["entries"] if e["id"] == eid)
        # still gated: live revision untouched, but the pending proposal carries the title.
        assert gated["revision"] == 1
        assert gated["proposed_revision"]["title"] == "Gated new title"

        ok, _msg = store.approve_decision(tmp_repo, eid, "approve")
        assert ok is True
        promoted = next(e for e in store._load(tmp_repo)["entries"] if e["id"] == eid)
        assert promoted["revision"] == 2
        assert promoted["title"] == "Gated new title"
        assert store._current_revision(promoted)["title"] == "Gated new title"

    def test_gated_proposal_title_rederived_when_edited_at_approval(self, tmp_repo):
        # If the lead edits the content while approving a Suggested Update, the proposal's
        # title (derived for the ORIGINAL proposed content) must not be carried onto the
        # edited content - it should re-derive instead of going stale.
        store.update_decision(tmp_repo, "Rollback endpoint is /api/v1/rollback", "s1", "architecture")
        data = store._load(tmp_repo)
        entry = next(e for e in data["entries"] if e.get("type") == "decision")
        eid = entry["id"]
        entry["status"] = "approved"
        store._save(tmp_repo, data)

        store.update_decision(
            tmp_repo, "Rollback endpoint is /api/v2/rollback", "s2",
            subtype="architecture", replace_id=eid, title="Gated new title")

        ok, _msg = store.approve_decision(
            tmp_repo, eid, "edit", content="Rollback endpoint is /api/v3/rollback with retries")
        assert ok is True
        promoted = next(e for e in store._load(tmp_repo)["entries"] if e["id"] == eid)
        assert promoted["revision"] == 2
        assert promoted["title"] == "Rollback endpoint is /api/v3/rollback with retries"
        assert promoted["title"] != "Gated new title"


class TestTitleBackfill:
    def test_legacy_entry_gets_title_on_load(self, tmp_repo, monkeypatch):
        # Write a store file with a revision-model entry that predates `title`.
        store.update_decision(tmp_repo, "Legacy decision body kept verbatim.", "s1",
                              subtype="architecture")
        data = store._load(tmp_repo)
        for e in data["entries"]:
            e.pop("title", None)
            for r in e.get("revisions", []):
                r.pop("title", None)
        data["schema_version"] = 2  # simulate an older store
        store._save(tmp_repo, data)
        # Next load must backfill.
        reloaded = store._load(tmp_repo)
        e = reloaded["entries"][0]
        assert e["title"] == "Legacy decision body kept verbatim."
        assert store._current_revision(e)["title"] == "Legacy decision body kept verbatim."
        assert reloaded.get("schema_version") == 3


class TestServerTitleParam:
    def test_update_context_forwards_title(self, tmp_repo, monkeypatch):
        from contexer import server
        seen = {}
        def fake_update(repo, content, sid, subtype="", created_by="ai", replace_id="", title=""):
            seen.update(title=title, content=content); return True, "id123"
        monkeypatch.setattr(server.store, "update_decision", fake_update)
        monkeypatch.setattr(server.store, "_resolve_repo", lambda p: tmp_repo)
        server.update_context("body", subtype="architecture", title="My Title")
        assert seen["title"] == "My Title"

    def test_update_global_context_forwards_title(self, monkeypatch):
        from contexer import server
        seen = {}
        def fake_update_global(content, sid, subtype="", title=""):
            seen.update(title=title, content=content); return True, "id456"
        monkeypatch.setattr(server.store, "update_global_decision", fake_update_global)
        server.update_global_context("body", subtype="constraint", title="My Global Title")
        assert seen["title"] == "My Global Title"


class TestTitleDisplay:
    def test_get_context_leads_with_title(self, tmp_repo):
        long_body = ("Adopt the outbox pattern for share retries. " + "detail " * 30)
        store.update_decision(tmp_repo, long_body, "s1", subtype="architecture",
                              title="Adopt outbox for share retries")
        out = store.get_context(tmp_repo)
        lines = out.splitlines()
        head = next(l for l in lines if "Adopt outbox for share retries" in l)
        # title on the bullet line; full body on the following indented line
        idx = lines.index(head)
        assert lines[idx].lstrip().startswith("- [")
        assert lines[idx + 1].startswith("    ") and "outbox pattern" in lines[idx + 1]

    def test_get_context_short_untitled_decision_renders_one_line(self, tmp_repo):
        # Finding #1: an untitled decision whose content is <=100 chars must NOT print the
        # content twice (bullet-line title + indented content line). One line only.
        short_body = "Never store plaintext passwords, always use bcrypt"
        store.update_decision(tmp_repo, short_body, "s1", subtype="constraint")
        out = store.get_context(tmp_repo)
        lines = out.splitlines()
        head = next(l for l in lines if short_body in l)
        idx = lines.index(head)
        assert lines[idx].lstrip().startswith("- [")
        # no follow-up indented duplicate of the same content
        assert idx + 1 >= len(lines) or not lines[idx + 1].startswith("    ")

    def test_get_context_long_untitled_decision_still_shows_two_lines(self, tmp_repo):
        # A long untitled decision still gets a derived (truncated) title on the bullet
        # line and the FULL content on the indented line below it.
        long_body = ("We rejected sharding the primary database this quarter because the "
                     "write volume does not yet justify the operational complexity it adds.")
        store.update_decision(tmp_repo, long_body, "s1", subtype="architecture")
        out = store.get_context(tmp_repo)
        lines = out.splitlines()
        head = next(l for l in lines if l.lstrip().startswith("- [") and "sharding" in l)
        idx = lines.index(head)
        assert long_body not in lines[idx]          # bullet line has the truncated title, not the full body
        assert lines[idx + 1] == f"    {long_body}"  # full body on the next, indented line
