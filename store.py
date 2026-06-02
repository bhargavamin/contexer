import json
import re
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
    import tomllib

    root = Path(repo_path)
    data = _load(repo_path)
    existing = [e for e in data.get("entries", []) if e["type"] == "decision"]
    inferred: list[str] = []
    found_files: list[str] = []

    def _add(fact: str) -> None:
        proxy = [{"content": f} for f in inferred]
        if _is_novel(fact, existing + proxy):
            inferred.append(fact)

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
        except Exception:
            pass

    if (root / "uv.lock").exists():
        found_files.append("uv.lock")
        _add("Package manager: uv")

    # --- Node / JS ---
    pkg_json = root / "package.json"
    if pkg_json.exists():
        found_files.append("package.json")
        try:
            pkg = json.loads(pkg_json.read_text())
            name = pkg.get("name", "")
            node_ver = pkg.get("engines", {}).get("node", "")
            parts = [f"Node.js project \"{name}\"" if name else "Node.js project"]
            if node_ver:
                parts.append(f"requires Node {node_ver}")
            _add(", ".join(parts))
            mgr = pkg.get("packageManager", "")
            if mgr:
                _add(f"Package manager: {mgr.split('@')[0]}")
            all_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            if "typescript" in all_deps:
                _add("Language: TypeScript")
            for fw in ["next", "nuxt", "remix", "svelte", "react", "vue", "express", "fastify"]:
                if fw in all_deps:
                    _add(f"Framework: {fw}")
                    break
            test_cmd = pkg.get("scripts", {}).get("test", "")
            if "jest" in test_cmd or "jest" in all_deps:
                _add("Test framework: Jest")
            elif "vitest" in test_cmd or "vitest" in all_deps:
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
    eslint = [".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.cjs",
              "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs"]
    if any((root / f).exists() for f in eslint):
        found_files.append(".eslintrc*")
        _add("Linting: ESLint")

    prettier = [".prettierrc", ".prettierrc.json", ".prettierrc.js", ".prettierrc.cjs", "prettier.config.js"]
    if any((root / f).exists() for f in prettier):
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

    # --- Existing AI/doc context ---
    for cf in ["CLAUDE.md", "README.md", ".cursorrules"]:
        if (root / cf).exists():
            found_files.append(cf)

    # --- Gap questions (pattern / use case / constraints) ---
    gaps: list[str] = ["What is this repo's primary purpose? (1-2 sentences)"]

    has_deploy = any(f in found_files for f in ["Dockerfile", "docker-compose.yml", "docker-compose.yaml"]) \
        or any("Kubernetes" in i or "Terraform" in i for i in inferred)
    if not has_deploy:
        gaps.append("What is the deployment target? (container, serverless, VPS, not yet decided...)")

    if not any("test" in i.lower() for i in inferred):
        gaps.append("What is the testing approach? (framework, coverage expectations, or 'none yet')")

    gaps.append("Are there intentional technology exclusions or patterns to always follow? (e.g. 'we avoid X because...' or 'always use Y for Z')")
    gaps.append("Are there performance, scale, or compliance constraints that shape technical decisions?")

    return {"inferred": inferred, "gaps": gaps, "existing_context_files": found_files}
