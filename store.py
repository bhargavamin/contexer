import json
import re
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path

STORE_DIR = Path.home() / ".contexer"
MAX_ENTRIES = 50

_DECISION_SIGNALS = [
    "decided", "decision", "chose", "approach", "instead of",
    "rather than", "went with", "will use", "should use", "opted",
]
_PATTERN_SIGNALS = [
    "pattern", "convention", "always", "never", "standard",
    "consistent", "going forward", "from now on", "practice",
]
_CONSTRAINT_SIGNALS = [
    "constraint", "tradeoff", "trade-off", "limitation", "cannot",
    "avoid", "requirement", "must not", "intentionally", "by design",
]


def _slug(repo_path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", repo_path.strip("/"))


def _store_path(repo_path: str) -> Path:
    STORE_DIR.mkdir(exist_ok=True)
    return STORE_DIR / f"{_slug(repo_path)}.json"


def _load(repo_path: str) -> dict:
    path = _store_path(repo_path)
    if path.exists():
        return json.loads(path.read_text())
    return {"repo_path": repo_path, "entries": []}


def _save(repo_path: str, data: dict) -> None:
    _store_path(repo_path).write_text(json.dumps(data, indent=2))


def _is_novel(content: str, existing: list) -> bool:
    if not existing:
        return True
    tokens = set(content.lower().split())
    if not tokens:
        return False
    for entry in existing:
        other = set(entry.get("content", "").lower().split())
        if not other:
            continue
        overlap = len(tokens & other) / max(len(tokens), len(other))
        if overlap > 0.7:
            return False
    return True


def _passes_filter(content: str, existing: list) -> bool:
    # Novelty is a prerequisite veto — duplicates are rejected regardless of signal keywords.
    # Novel content always passes: update_context is only called for significant decisions.
    decisions_only = [e for e in existing if e["type"] == "decision"]
    return _is_novel(content, decisions_only)


_QUESTION_STARTS = {
    "what", "how", "why", "when", "where", "who", "which",
    "is", "are", "can", "does", "do", "will", "would", "could", "should",
}

def _is_task(content: str) -> bool:
    stripped = content.strip()
    words = stripped.lower().split()
    if len(words) < 5:
        return False
    if stripped.endswith("?") and len(words) < 20:
        return False
    if words[0] in _QUESTION_STARTS and len(words) < 12:
        return False
    return True


def capture_task(repo_path: str, description: str, session_id: str) -> str | None:
    if not _is_task(description):
        return None
    data = _load(repo_path)
    # keep only decisions — one task slot is enough for "last task" context
    data["entries"] = [e for e in data["entries"] if e["type"] != "task"]
    entry = {
        "id": str(uuid.uuid4()),
        "type": "task",
        "content": description,
        "session_id": session_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    data["entries"].append(entry)
    data["entries"] = data["entries"][-MAX_ENTRIES:]
    _save(repo_path, data)
    return entry["id"]


def update_decision(repo_path: str, content: str, session_id: str) -> tuple[bool, str | None]:
    data = _load(repo_path)
    if not _passes_filter(content, data["entries"]):
        return False, None
    entry = {
        "id": str(uuid.uuid4()),
        "type": "decision",
        "content": content,
        "session_id": session_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    data["entries"].append(entry)
    data["entries"] = data["entries"][-MAX_ENTRIES:]
    _save(repo_path, data)
    return True, entry["id"]


def get_session_start_context(repo_path: str) -> dict:
    data = _load(repo_path)
    decisions = [e for e in data.get("entries", []) if e["type"] == "decision"]
    if decisions:
        return {
            "systemMessage": f"Contexer: {len(decisions)} decision(s) loaded",
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": get_context(repo_path),
            },
        }
    return {
        "systemMessage": "Contexer: no context stored yet — offer to bootstrap",
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": (
                "No context stored for this repo yet. "
                "Ask the user: 'No stored context found. I can scan this repo to build an initial "
                "baseline of decisions and constraints — should I?' "
                "Wait for their confirmation before calling bootstrap_context."
            ),
        },
    }


def get_context(repo_path: str) -> str:
    data = _load(repo_path)
    entries = data.get("entries", [])
    if not entries:
        return "No context stored for this repository."

    lines = [f"# Context for {repo_path}\n"]

    tasks = [e for e in entries if e["type"] == "task"]
    if tasks:
        last = tasks[-1]
        lines.append(f"## Last task ({last['timestamp'][:10]})")
        lines.append(last["content"])
        lines.append("")

    decisions = [e for e in entries if e["type"] == "decision"]
    if decisions:
        lines.append("## Decisions and context")
        for d in decisions[-10:]:
            lines.append(f"- [{d['timestamp'][:10]}] {d['content']}")
        lines.append("")

    return "\n".join(lines)


def bootstrap_scan(repo_path: str) -> dict:
    root = Path(repo_path)
    data = _load(repo_path)
    existing = [e for e in data.get("entries", []) if e["type"] == "decision"]
    inferred: list[str] = []
    found_files: list[str] = []
    all_deps: set[str] = set()  # normalized dep names across all detected ecosystems

    def _add(fact: str) -> None:
        proxy = [{"content": f} for f in inferred]
        if _is_novel(fact, existing + proxy):
            inferred.append(fact)

    def _gap(assumption: str, question: str, hint: str) -> dict:
        return {"assumption": assumption, "question": question, "hint": hint}

    def _has_dep(*names: str) -> bool:
        # substring match — catches scoped packages (@aws-sdk/client-s3) and extras (psycopg[binary])
        return any(n in dep for n in names for dep in all_deps)

    # --- Python ---
    pyproject_path = root / "pyproject.toml"
    if pyproject_path.exists():
        found_files.append("pyproject.toml")
        try:
            with open(pyproject_path, "rb") as f:
                pyp = tomllib.load(f)
            proj = pyp.get("project", {})
            name, py_req = proj.get("name", ""), proj.get("requires-python", "")
            _add(f"Python project{f' \"{name}\"' if name else ''}{f', requires-python {py_req}' if py_req else ''}")
            tool = pyp.get("tool", {})
            if "pytest" in tool:
                _add("Test framework: pytest")
            if "ruff" in tool:
                _add("Linting/formatting: ruff")
            if "mypy" in tool:
                _add("Type checking: mypy")
            # collect all dep names for signal detection below
            raw: list[str] = list(proj.get("dependencies", []))
            for group in pyp.get("dependency-groups", {}).values():
                raw.extend(d for d in group if isinstance(d, str))
            for extra in proj.get("optional-dependencies", {}).values():
                raw.extend(extra)
            for dep in raw:
                normalized = re.split(r"[>=<!~\[\s;]", dep.strip())[0].lower().replace("_", "-")
                all_deps.add(normalized)
        except Exception:
            pass

    if (root / "uv.lock").exists():
        found_files.append("uv.lock")
        _add("Package manager: uv")

    # --- Node / JS ---
    pkg_json_path = root / "package.json"
    if pkg_json_path.exists():
        found_files.append("package.json")
        try:
            pkg = json.loads(pkg_json_path.read_text())
            name = pkg.get("name", "")
            node_ver = pkg.get("engines", {}).get("node", "")
            parts = [f"Node.js project \"{name}\"" if name else "Node.js project"]
            if node_ver:
                parts.append(f"requires Node {node_ver}")
            _add(", ".join(parts))
            mgr = pkg.get("packageManager", "")
            if mgr:
                _add(f"Package manager: {mgr.split('@')[0]}")
            node_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            all_deps.update(k.lower() for k in node_deps)
            if pkg.get("workspaces"):
                _add("Monorepo: npm/yarn workspaces")
            if "typescript" in node_deps:
                _add("Language: TypeScript")
            for fw in ["next", "nuxt", "remix", "svelte", "react", "vue", "express", "fastify", "hono", "elysia"]:
                if fw in node_deps:
                    _add(f"Framework: {fw}")
                    break
            test_cmd = pkg.get("scripts", {}).get("test", "")
            if "jest" in test_cmd or "jest" in node_deps:
                _add("Test framework: Jest")
            elif "vitest" in test_cmd or "vitest" in node_deps:
                _add("Test framework: Vitest")
        except Exception:
            pass

    # --- Go ---
    if (root / "go.mod").exists():
        found_files.append("go.mod")
        try:
            for line in (root / "go.mod").read_text().splitlines():
                if line.startswith("module "):
                    _add(f"Go module: {line.split()[1]}")
                elif line.startswith("go "):
                    _add(f"Go version: {line.split()[1]}")
                    break
        except Exception:
            pass

    # --- Rust ---
    if (root / "Cargo.toml").exists():
        found_files.append("Cargo.toml")
        try:
            with open(root / "Cargo.toml", "rb") as f:
                c = tomllib.load(f)
            p = c.get("package", {})
            rust_name = f' "{p["name"]}"' if p.get("name") else ""
            rust_edition = f', edition {p["edition"]}' if p.get("edition") else ""
            _add(f"Rust project{rust_name}{rust_edition}")
        except Exception:
            pass

    # --- Monorepo ---
    for mf in ["nx.json", "turbo.json", "lerna.json", "pnpm-workspace.yaml"]:
        if (root / mf).exists():
            found_files.append(mf)
            _add(f"Monorepo: {mf.split('.')[0]} workspace")
            break
    if not any("Monorepo" in i for i in inferred):
        if (root / "packages").is_dir() or (root / "apps").is_dir():
            _add("Monorepo: packages/ or apps/ directory structure")

    # --- Data layer ---
    _DB_MAP = {
        "PostgreSQL": {"psycopg", "psycopg2", "asyncpg", "pg", "postgres", "neon"},
        "MySQL/MariaDB": {"pymysql", "aiomysql", "mysql2", "mysql"},
        "MongoDB": {"pymongo", "motor", "mongodb", "mongoose"},
        "Redis": {"redis", "aioredis", "ioredis"},
        "SQLite": {"aiosqlite", "better-sqlite3"},
    }
    _ORM_DEPS = {"sqlalchemy", "tortoise-orm", "databases", "prisma", "drizzle-orm", "typeorm", "sequelize", "knex", "mikro-orm"}

    detected_db = [label for label, names in _DB_MAP.items() if _has_dep(*names)]
    if detected_db:
        _add(f"Data store(s): {', '.join(detected_db)}")
    detected_orm = next((d for d in _ORM_DEPS if _has_dep(d)), None)
    if detected_orm:
        _add(f"ORM / query builder: {detected_orm}")

    # --- Auth ---
    _AUTH_JWT = {"python-jose", "pyjwt", "jose"}
    _AUTH_FRAMEWORK = {"passlib", "authlib", "passport", "next-auth", "@auth", "clerk", "supabase", "firebase-admin", "google-auth", "python-keycloak"}
    if _has_dep(*_AUTH_JWT):
        _add("Auth: JWT-based (pyjwt / python-jose detected)")
    elif _has_dep(*_AUTH_FRAMEWORK):
        pkg_found = next((d for d in _AUTH_FRAMEWORK if _has_dep(d)), "unknown")
        _add(f"Auth: {pkg_found} detected")

    # --- Cloud SDKs ---
    if _has_dep("boto3", "botocore", "aws-cdk", "@aws-sdk", "aws-lambda"):
        _add("Cloud: AWS SDK present (boto3 / @aws-sdk)")
    if _has_dep("google-cloud", "@google-cloud", "google-auth"):
        _add("Cloud: GCP SDK present")
    if _has_dep("azure-", "@azure"):
        _add("Cloud: Azure SDK present")

    # --- External integrations ---
    _INTEGRATIONS = {
        "stripe": "Payments: Stripe",
        "braintree": "Payments: Braintree",
        "sendgrid": "Email: SendGrid",
        "resend": "Email: Resend",
        "twilio": "Messaging: Twilio",
        "openai": "AI: OpenAI SDK",
        "anthropic": "AI: Anthropic SDK",
        "langchain": "AI: LangChain",
        "celery": "Task queue: Celery",
        "dramatiq": "Task queue: Dramatiq",
        "kafka-python": "Messaging: Kafka",
        "confluent-kafka": "Messaging: Kafka (Confluent)",
        "pika": "Messaging: RabbitMQ",
        "aio-pika": "Messaging: RabbitMQ (async)",
        "elasticsearch-py": "Search: Elasticsearch",
        "typesense": "Search: Typesense",
    }
    for dep, label in _INTEGRATIONS.items():
        if _has_dep(dep):
            _add(label)

    # --- CI/CD ---
    gh_wf = root / ".github" / "workflows"
    if gh_wf.is_dir():
        wfs = list(gh_wf.glob("*.yml")) + list(gh_wf.glob("*.yaml"))
        if wfs:
            found_files.append(".github/workflows/")
            _add(f"CI/CD: GitHub Actions ({len(wfs)} workflow file(s))")
    if (root / ".gitlab-ci.yml").exists():
        found_files.append(".gitlab-ci.yml")
        _add("CI/CD: GitLab CI")

    # --- Docker ---
    if (root / "Dockerfile").exists():
        found_files.append("Dockerfile")
        try:
            first_from = next(
                (l.split()[1] for l in (root / "Dockerfile").read_text().splitlines() if l.startswith("FROM")), None
            )
            _add(f"Containerized — Dockerfile present{f' (base: {first_from})' if first_from else ''}")
        except Exception:
            _add("Containerized — Dockerfile present")
    for compose in ["docker-compose.yml", "docker-compose.yaml"]:
        if (root / compose).exists():
            found_files.append(compose)
            _add("Local dev: docker-compose present")
            break

    # --- Linting / formatting ---
    eslint_files = [".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.cjs",
                    "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs"]
    if any((root / f).exists() for f in eslint_files):
        found_files.append(".eslintrc*")
        _add("Linting: ESLint")
    prettier_files = [".prettierrc", ".prettierrc.json", ".prettierrc.js", ".prettierrc.cjs", "prettier.config.js"]
    if any((root / f).exists() for f in prettier_files):
        found_files.append(".prettierrc*")
        _add("Formatting: Prettier")
    if (root / "ruff.toml").exists():
        found_files.append("ruff.toml")
        _add("Linting/formatting: ruff (ruff.toml)")
    if (root / "pytest.ini").exists():
        found_files.append("pytest.ini")
        _add("Test framework: pytest (pytest.ini)")

    # --- Infrastructure ---
    if list(root.glob("*.tf")) or (root / "terraform").is_dir():
        _add("Infrastructure as code: Terraform")
    if any((root / d).is_dir() for d in ["k8s", "kubernetes", "helm"]):
        _add("Deployment: Kubernetes (manifests or Helm charts present)")

    # --- Architecture signals ---
    src = root / "src"
    if src.is_dir():
        layers = [d for d in ["api", "services", "models", "controllers", "middleware", "handlers", "repositories"]
                  if (src / d).is_dir()]
        if layers:
            layer_str = ", ".join(layers[:3]) + ("..." if len(layers) > 3 else "")
            _add(f"Architecture: layered structure detected (src/{layer_str})")

    # --- Existing AI/doc context ---
    for cf in ["CLAUDE.md", "README.md", ".cursorrules"]:
        if (root / cf).exists():
            found_files.append(cf)

    # --- Gap questions as assumptions (confirm / amend) ---
    gaps: list[dict] = []

    gaps.append(_gap(
        assumption="Purpose not yet documented",
        question="What is this repo's primary purpose?",
        hint="e.g. 'REST API for X', 'CLI tool that does Y', 'data pipeline for Z'",
    ))

    has_deploy = any(f in found_files for f in ["Dockerfile", "docker-compose.yml", "docker-compose.yaml"]) \
        or any(kw in i for i in inferred for kw in ["Kubernetes", "Terraform"])
    if not has_deploy:
        has_cloud = any("Cloud:" in i for i in inferred)
        cloud_label = next((i for i in inferred if "Cloud:" in i), "")
        gaps.append(_gap(
            assumption=f"Cloud deployment ({cloud_label}), no container config found" if has_cloud
                       else "Deployment target not yet decided",
            question=f"Assuming {cloud_label.lower()} deployment — correct?" if has_cloud
                     else "Assuming no deployment target decided yet — correct?",
            hint="Container, serverless, VPS, bare metal, or still TBD?",
        ))

    if not any("test" in i.lower() or "Test" in i for i in inferred):
        gaps.append(_gap(
            assumption="No automated tests yet",
            question="Assuming no automated tests yet — correct?",
            hint="If tests exist: framework, coverage expectations, any test patterns",
        ))

    if not detected_db:
        gaps.append(_gap(
            assumption="No database or persistent storage",
            question="Assuming no database yet — correct?",
            hint="If using one: which (Postgres, MySQL, MongoDB, Redis, SQLite) and ORM/query builder if any",
        ))

    if not any("Auth:" in i for i in inferred):
        gaps.append(_gap(
            assumption="No authentication layer yet",
            question="Assuming no auth layer yet — correct?",
            hint="If auth exists: mechanism (JWT, sessions, OAuth, API keys) and library",
        ))

    # Always ask — highest-value signal that can't be inferred
    gaps.append(_gap(
        assumption="No known intentional exclusions",
        question="Are there intentional technology exclusions or patterns to always follow?",
        hint="e.g. 'we avoid ORMs — raw SQL only', 'always async handlers', 'never store secrets in code'",
    ))
    gaps.append(_gap(
        assumption="No known performance or compliance constraints",
        question="Any performance, scale, or compliance constraints that shape decisions?",
        hint="e.g. latency targets, user scale, GDPR/SOC2, regulated data",
    ))

    return {"inferred": inferred, "gaps": gaps, "existing_context_files": found_files}
