"""Tests for core store.py logic — filtering, storage, context output, and bootstrap scan."""
import json
import tempfile
from pathlib import Path

import pytest

import store


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def tmp_repo(tmp_path, monkeypatch):
    """Redirects STORE_DIR to a temp path and returns a fake repo path."""
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    return str(tmp_path / "repo")


@pytest.fixture
def populated_repo(tmp_repo):
    """A repo with one task and two decisions pre-loaded."""
    store.capture_task(tmp_repo, "implement authentication flow for the API", "sess-1")
    store.update_decision(tmp_repo, "decided to use JWT instead of sessions — stateless, easier to scale", "sess-1")
    store.update_decision(tmp_repo, "constraint: never store plaintext passwords, always use bcrypt", "sess-1")
    return tmp_repo


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
        for i in range(store.MAX_ENTRIES + 5):
            store.update_decision(tmp_repo, f"unique decision number {i} about the system architecture approach", f"sess-{i}")
        data = store._load(tmp_repo)
        assert len(data["entries"]) <= store.MAX_ENTRIES


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

    def test_shows_only_last_10_decisions(self, tmp_repo):
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
        # get_context surfaces only the last 10
        assert result.count("- [") == 10


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

    def test_empty_repo_directive_includes_repo_path(self, tmp_repo):
        # Directive includes repo path so Claude knows which repo is being bootstrapped
        result = store.get_session_start_context(tmp_repo)
        assert tmp_repo in result["hookSpecificOutput"]["additionalContext"]

    def test_empty_repo_directive_includes_bootstrap_instruction(self, tmp_repo):
        result = store.get_session_start_context(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "bootstrap_context" in ctx
        assert "STOP" in ctx

    def test_populated_repo_injects_pointer_not_full_dump(self, populated_repo):
        # JIT: SessionStart injects a count pointer, not the full context
        result = store.get_session_start_context(populated_repo)
        assert "stored" in result["systemMessage"]
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
        assert "no stored context" in ctx.lower()
        assert "update_context" in ctx

    def test_populated_repo_returns_empty_dict(self, populated_repo):
        result = store.get_bootstrap_context_prompt(populated_repo)
        assert result == {}

    def test_directive_tells_claude_to_call_bootstrap_tool(self, tmp_repo):
        # The hook emits a STOP directive — inferred facts are fetched via the MCP tool, not inline
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "uv.lock").write_text("")
        result = store.get_bootstrap_context_prompt(tmp_repo)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        assert "bootstrap_context" in ctx
        assert "STOP" in ctx

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
    """Flatten gap question text for assertion convenience."""
    return [g["question"] for g in result["gaps"]]


def _gap_assumptions(result: dict) -> list[str]:
    return [g["assumption"] for g in result["gaps"]]


class TestBootstrapScan:
    # ── gap structure ──────────────────────────────────────────────────────────

    def test_gaps_are_dicts_with_required_keys(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        result = store.bootstrap_scan(tmp_repo)
        for gap in result["gaps"]:
            assert "assumption" in gap
            assert "question" in gap
            assert "hint" in gap

    def test_always_asks_primary_purpose(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        result = store.bootstrap_scan(tmp_repo)
        assert any("what does this repo do" in q.lower() for q in _gap_questions(result))

    def test_team_context_asked_when_architecture_signals_present(self, tmp_repo):
        # Team conventions gap is conditional — only when architecture signals suggest collaboration
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        src = Path(tmp_repo) / "src"
        (src / "api").mkdir(parents=True)
        (src / "services").mkdir(parents=True)
        (src / "models").mkdir(parents=True)
        result = store.bootstrap_scan(tmp_repo)
        assert any("branch" in q.lower() or "team" in q.lower() or "pr" in q.lower() for q in _gap_questions(result))

    def test_exclusions_asked_when_dep_choices_exist(self, tmp_repo):
        # Exclusions gap only fires when dep tree has meaningful choices (>5 deps or ORM detected)
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "api"\nrequires-python = ">=3.12"\n'
            'dependencies = ["sqlalchemy>=2.0","httpx","pydantic","redis","celery","stripe"]\n'
        )
        result = store.bootstrap_scan(tmp_repo)
        assert any("exclusion" in q.lower() or "intentionally" in q.lower() for q in _gap_questions(result))

    def test_constraints_asked_when_production_signals_present(self, tmp_repo):
        # Constraints gap only fires when production signals exist (auth, cloud, infra, container)
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "api"\nrequires-python = ">=3.12"\ndependencies = ["boto3>=1.0"]\n'
        )
        result = store.bootstrap_scan(tmp_repo)
        assert any("constraint" in q.lower() for q in _gap_questions(result))

    def test_purpose_assumption_derived_from_readme(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "README.md").write_text("# MyApp\nA payment processing service for e-commerce.\n")
        result = store.bootstrap_scan(tmp_repo)
        purpose_gap = next(g for g in result["gaps"] if "what does this repo do" in g["question"].lower())
        assert "payment" in purpose_gap["assumption"].lower()

    def test_purpose_assumption_inferred_from_name(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text('[project]\nname = "my-api-service"\nrequires-python = ">=3.12"\n')
        result = store.bootstrap_scan(tmp_repo)
        purpose_gap = next(g for g in result["gaps"] if "what does this repo do" in g["question"].lower())
        assert "api" in purpose_gap["assumption"].lower() or "service" in purpose_gap["assumption"].lower()

    def test_max_10_questions(self, tmp_repo):
        # Even with all gaps triggered, total questions must not exceed 10
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "app"\nrequires-python = ">=3.12"\ndependencies = ["boto3>=1.0","stripe>=2.0"]\n'
        )
        result = store.bootstrap_scan(tmp_repo)
        assert len(result["gaps"]) <= 10

    # ── language / tooling detection ──────────────────────────────────────────

    def test_detects_python_uv(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "myapp"\nrequires-python = ">=3.12"\n'
        )
        (Path(tmp_repo) / "uv.lock").write_text("")

        result = store.bootstrap_scan(tmp_repo)
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

        result = store.bootstrap_scan(tmp_repo)
        inferred = " ".join(result["inferred"]).lower()
        assert "node" in inferred
        assert "typescript" in inferred
        assert "react" in inferred

    def test_detects_github_actions(self, tmp_repo):
        wf_dir = Path(tmp_repo) / ".github" / "workflows"
        wf_dir.mkdir(parents=True)
        (wf_dir / "ci.yml").write_text("on: [push]")

        result = store.bootstrap_scan(tmp_repo)
        assert any("github actions" in i.lower() for i in result["inferred"])

    # ── deployment detection ───────────────────────────────────────────────────

    def test_detects_dockerfile_suppresses_deployment_gap(self, tmp_repo):
        # When Dockerfile present, deployment target is known — no need to ask
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "Dockerfile").write_text("FROM python:3.12-slim\n")

        result = store.bootstrap_scan(tmp_repo)
        assert any("dockerfile" in i.lower() or "container" in i.lower() for i in result["inferred"])
        assert not any("where does this run" in g["question"].lower() for g in result["gaps"])

    def test_no_dockerfile_adds_deployment_gap(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        result = store.bootstrap_scan(tmp_repo)
        env_gap = next(g for g in result["gaps"] if "where does this run" in g["question"].lower())
        assert "no container" in env_gap["assumption"].lower() or "deployment target" in env_gap["assumption"].lower()

    # ── data layer detection ───────────────────────────────────────────────────

    def test_detects_postgres_dep(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "api"\nrequires-python = ">=3.12"\ndependencies = ["psycopg[binary]>=3.0"]\n'
        )
        result = store.bootstrap_scan(tmp_repo)
        assert any("postgresql" in i.lower() for i in result["inferred"])

    def test_detects_redis_dep(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        pkg = {"name": "app", "dependencies": {"redis": "^4.0.0"}}
        (Path(tmp_repo) / "package.json").write_text(json.dumps(pkg))

        result = store.bootstrap_scan(tmp_repo)
        assert any("redis" in i.lower() for i in result["inferred"])

    def test_detects_orm_dep(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "api"\nrequires-python = ">=3.12"\ndependencies = ["sqlalchemy>=2.0"]\n'
        )
        result = store.bootstrap_scan(tmp_repo)
        assert any("sqlalchemy" in i.lower() for i in result["inferred"])

    # ── auth and security-sensitive detection ──────────────────────────────────

    def test_detects_jwt_dep_in_inferred(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "api"\nrequires-python = ">=3.12"\ndependencies = ["python-jose[cryptography]>=3.3"]\n'
        )
        result = store.bootstrap_scan(tmp_repo)
        assert any("jwt" in i.lower() or "auth" in i.lower() for i in result["inferred"])

    def test_security_sensitive_deps_add_compliance_gap(self, tmp_repo):
        # Auth or payment deps trigger a compliance question
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "api"\nrequires-python = ">=3.12"\ndependencies = ["stripe>=2.0"]\n'
        )
        result = store.bootstrap_scan(tmp_repo)
        assert any("compliance" in q.lower() or "security" in q.lower() for q in _gap_questions(result))

    # ── cloud and integration detection ───────────────────────────────────────

    def test_detects_aws_sdk(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "api"\nrequires-python = ">=3.12"\ndependencies = ["boto3>=1.0"]\n'
        )
        result = store.bootstrap_scan(tmp_repo)
        assert any("aws" in i.lower() for i in result["inferred"])

    def test_detects_stripe_integration(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        pkg = {"name": "app", "dependencies": {"stripe": "^14.0.0"}}
        (Path(tmp_repo) / "package.json").write_text(json.dumps(pkg))

        result = store.bootstrap_scan(tmp_repo)
        assert any("stripe" in i.lower() for i in result["inferred"])

    def test_detects_openai_integration(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "api"\nrequires-python = ">=3.12"\ndependencies = ["openai>=1.0"]\n'
        )
        result = store.bootstrap_scan(tmp_repo)
        assert any("openai" in i.lower() for i in result["inferred"])

    # ── monorepo detection ─────────────────────────────────────────────────────

    def test_detects_nx_monorepo(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "nx.json").write_text("{}")

        result = store.bootstrap_scan(tmp_repo)
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

        result = store.bootstrap_scan(tmp_repo)
        assert not any("uv" in i.lower() for i in result["inferred"])
