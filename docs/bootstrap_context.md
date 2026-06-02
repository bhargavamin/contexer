# bootstrap_context — Feature Design

## Problem

Contexer builds knowledge incrementally during sessions. For existing projects with established patterns, conventions, and historical decisions, there is no initial seed of context. The first sessions start blind even though the codebase already encodes a lot of implicit decisions.

## Goal

Allow Contexer to initialize context for an existing repo by:
1. Statically scanning the repo for inferable facts (runtime, tooling, CI, etc.)
2. Returning inferred decisions + a targeted question list for what could not be determined
3. Claude stores the inferred decisions and works through the questions with the user

## Approach: Option B — Static scan + LLM-driven interview

**Why not static only (Option A)?**
Captures *what* (runtime version, package manager) but misses *why* (architectural intent, intentional exclusions, deployment constraints). The why is exactly what Contexer is designed to preserve.

**Why not predefined questionnaire (Option C)?**
Generic questions miss project-specific gaps. A question like "do you use Docker?" is useless if the repo already has a Dockerfile.

**Why Option B?**
- Server does cheap static work (no LLM cost)
- Claude is already in the loop — it handles synthesis and questioning
- Questions are targeted to gaps in what was inferred, not generic
- Fits the existing three-tool architecture: one new tool, no new files

## Design

### New tool: `bootstrap_context(repo_path)`

Added to `server.py`. Returns JSON with two sections:

```json
{
  "inferred": [
    "Python 3.12, uv as package manager (pyproject.toml)",
    "pytest for testing (pyproject.toml dev deps)",
    "GitHub Actions CI on push to main (.github/workflows/ci.yml)",
    "ruff for linting (ruff.toml)"
  ],
  "gaps": [
    "No deployment config found — what is the deployment target?",
    "No database/ORM dependency found — is persistence out of scope?",
    "No explicit error handling pattern found — what is the approach?",
    "CLAUDE.md exists but no architectural decisions documented"
  ],
  "existing_context_files": ["CLAUDE.md", "README.md"]
}
```

Claude's job after receiving this:
1. Call `update_context` for each item in `inferred`
2. Present `gaps` as questions to the user (3-4 at a time, not all at once)
3. Store each answer via `update_context`

### What the scanner reads

**Tier 1 — high confidence, infer directly:**
- `pyproject.toml` / `package.json` / `Cargo.toml` / `go.mod` → runtime, package manager, key deps
- `.nvmrc` / `.python-version` → pinned versions
- `.github/workflows/` / `.gitlab-ci.yml` / `Jenkinsfile` → CI/CD system
- `ruff.toml` / `.eslintrc*` / `mypy.ini` / `.prettierrc` → linting and style enforcement
- `Dockerfile` / `docker-compose.yml` → containerization intent
- `pytest.ini` / `jest.config.*` / `vitest.config.*` → test framework
- `CLAUDE.md` / `README.md` → existing documented decisions (note: present but may be stale)

**Tier 2 — absence signals a gap question:**
- No test files or test config → "What is the testing approach?"
- No CI config → "Is there a CI/CD pipeline planned?"
- No Dockerfile but cloud SDK deps present → "What is the deployment target?"
- No linting config → "Is code style enforced by tooling?"

**Tier 3 — always ask (cannot be inferred from files):**
- Why certain architectural choices were made
- Intentional exclusions ("we deliberately do not use X because...")
- Performance or scale constraints
- Security or compliance requirements

### Implementation

`store.py` — new function `bootstrap_scan(repo_path) -> dict`:
- Walks only known config file paths (not all files — O(1) not O(n))
- Returns `{"inferred": [...], "gaps": [...], "existing_context_files": [...]}`
- Reuses `_is_novel` to skip inferred items that would duplicate existing decisions
- Pure static analysis, no subprocess, no LLM call

`server.py` — new tool:
```python
@mcp.tool()
def bootstrap_context(repo_path: str) -> str:
    """Scans a repo for inferable decisions and gaps. Call update_context for
    each inferred item, then ask the user the gap questions and store answers."""
    return json.dumps(store.bootstrap_scan(repo_path))
```

### Usage flow

```
Developer installs Contexer on an existing project
    │
    ↓
Calls bootstrap_context("/path/to/repo")
    │
    ↓
Server returns inferred facts + gap questions
    │
    ↓
Claude stores inferred decisions via update_context (one call per item)
    │
    ↓
Claude presents gap questions to user (3-4 at a time)
    │
    ↓
User answers → Claude stores via update_context
    │
    ↓
Repo starts next session with a rich decision baseline
```

### Edge cases

- **Repo already has Contexer context:** `bootstrap_scan` checks existing decisions via `_is_novel` and skips duplicates — safe to run again
- **Large repos:** scanner reads only known config file paths, never walks the full tree
- **Monorepos:** scan at root by default; caller can pass a subdirectory path for per-service context
- **Missing files:** scanner skips gracefully — absence becomes a gap question, not an error

## Constraints preserved

- No new files — `bootstrap_scan` goes in `store.py`, new tool in `server.py`
- No LLM calls in the server — all reasoning stays in Claude
- Silent operation unchanged — `bootstrap_context` is explicit and opt-in, not automatic
- This adds a fourth tool; the three-tool constraint was for the initial MVP

## Open questions

1. Should `bootstrap_context` be triggered automatically when a repo has no stored context, or always explicit?
2. Should gap questions be batched (all at once) or asked one category at a time?
3. Should inferred decisions be stored automatically or shown to the user for confirmation first?
