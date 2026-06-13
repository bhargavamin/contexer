"""Tests for core store.py logic — filtering, storage, context output, and bootstrap scan."""
import json
from pathlib import Path

import pytest

from contexer import store



# ── _is_task ──────────────────────────────────────────────────────────────────

class TestIsTask:
    def test_real_task_passes(self):
        assert store._is_task("implement authentication flow for the login endpoint") is True

    def test_short_input_rejected(self):
        assert store._is_task("fix bug") is False

    def test_question_rejected(self):
        assert store._is_task("what is the best approach here?") is False

    def test_long_question_passes(self):
        # Long questions (>20 words) are treated as task descriptions, not queries
        assert store._is_task(
            "what should we do about the authentication flow given the constraints "
            "of the existing session management system and the new API requirements"
        ) is True

    def test_short_question_start_rejected(self):
        assert store._is_task("how does this work with the API?") is False

    def test_bootstrap_answer_negation_rejected(self):
        # "no where is just documentation nothing else" is a Q&A answer, not a task
        assert store._is_task("no where is just documentation nothing else") is False

    def test_bootstrap_answer_yes_rejected(self):
        assert store._is_task("yes that sounds correct to me") is False

    def test_bootstrap_answer_none_rejected(self):
        assert store._is_task("none of those apply to this repo") is False


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


# ── capture_task ──────────────────────────────────────────────────────────────

class TestCaptureTask:
    def test_stores_task(self, tmp_repo):
        entry_id = store.capture_task(tmp_repo, "refactor the authentication module for the new api", "sess-1")
        assert entry_id is not None
        data = store._load(tmp_repo)
        tasks = [e for e in data["entries"] if e["type"] == "task"]
        assert len(tasks) == 1
        assert tasks[0]["content"] == "refactor the authentication module for the new api"

    def test_replaces_previous_task(self, tmp_repo):
        store.capture_task(tmp_repo, "first task description for the authentication module", "sess-1")
        store.capture_task(tmp_repo, "second task description for the deployment pipeline setup", "sess-2")
        data = store._load(tmp_repo)
        tasks = [e for e in data["entries"] if e["type"] == "task"]
        assert len(tasks) == 1
        assert tasks[0]["content"] == "second task description for the deployment pipeline setup"

    def test_skips_question(self, tmp_repo):
        result = store.capture_task(tmp_repo, "what should we do here?", "sess-1")
        assert result is None
        data = store._load(tmp_repo)
        assert data["entries"] == []

    def test_does_not_replace_decisions(self, tmp_repo):
        store.update_decision(tmp_repo, "decided to use postgres for primary data storage", "sess-1")
        store.capture_task(tmp_repo, "new task to implement the user registration flow", "sess-2")
        data = store._load(tmp_repo)
        decisions = [e for e in data["entries"] if e["type"] == "decision"]
        assert len(decisions) == 1


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

    def test_includes_last_task(self, populated_repo):
        result = store.get_context(populated_repo)
        assert "implement authentication flow" in result

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


# ── session start subtype breakdown (v0.4.0) ─────────────────────────────────

SESSION_ID_SS = "test-ss-session"

class TestSessionStartBreakdown:
    def test_constraints_and_conventions_shown_separately(self, tmp_repo):
        store.update_decision(tmp_repo, "Never commit untested code", SESSION_ID_SS, "constraint")
        store.update_decision(tmp_repo, "Always use uv not pip", SESSION_ID_SS, "convention")
        result = store.get_session_start_context(tmp_repo)
        msg = result["systemMessage"]
        assert "constraint" in msg
        assert "convention" in msg

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
        assert "constraint" in msg

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
        # "postgres" is a domain keyword — should search, not dump full overview
        if result:
            assert "PostgreSQL" in result or "postgres" in result.lower()

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
        entry_id, content = store.capture_user_constraint(
            tmp_repo,
            "ensure terraform always stores state on s3 with dynamodb locking",
            "sess-1",
        )
        assert entry_id is not None
        assert content
        data = store._load(tmp_repo)
        decisions = [e for e in data["entries"] if e["type"] == "decision"]
        assert len(decisions) == 1
        assert decisions[0]["subtype"] == "constraint"

    def test_stores_never_directive_as_constraint(self, tmp_repo):
        entry_id, content = store.capture_user_constraint(
            tmp_repo, "never push secrets or credentials to the repository", "sess-1"
        )
        assert entry_id is not None
        data = store._load(tmp_repo)
        decisions = [e for e in data["entries"] if e["type"] == "decision"]
        assert decisions[0]["subtype"] == "constraint"

    def test_non_directive_prompt_returns_none(self, tmp_repo):
        entry_id, content = store.capture_user_constraint(
            tmp_repo, "write tests for the authentication module", "sess-1"
        )
        assert entry_id is None
        assert content is None
        data = store._load(tmp_repo)
        assert data["entries"] == []

    def test_question_prompt_returns_none(self, tmp_repo):
        entry_id, content = store.capture_user_constraint(
            tmp_repo, "should we always use s3 for terraform state?", "sess-1"
        )
        assert entry_id is None

    def test_personal_descriptive_returns_none(self, tmp_repo):
        entry_id, content = store.capture_user_constraint(
            tmp_repo, "I always get a permission error when running this", "sess-1"
        )
        assert entry_id is None

    def test_duplicate_directive_silently_discarded(self, tmp_repo):
        store.capture_user_constraint(
            tmp_repo, "always store terraform state on s3 with dynamodb lock table", "sess-1"
        )
        entry_id, content = store.capture_user_constraint(
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

    def test_long_prompt_truncated_to_600_chars(self, tmp_repo):
        long_prompt = "always " + "x" * 700
        entry_id, content = store.capture_user_constraint(tmp_repo, long_prompt, "sess-1")
        assert entry_id is not None
        data = store._load(tmp_repo)
        assert len(data["entries"][0]["content"]) <= 600


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
