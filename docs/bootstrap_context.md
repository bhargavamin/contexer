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
- Questions are targeted to actual gaps, not generic
- Fits the existing architecture: one new tool, no new files

## Resolved design decisions

| Question | Decision |
|---|---|
| Auto-trigger or explicit? | Auto-trigger on SessionStart when no context exists, but ask for user confirmation before scanning |
| How to ask gap questions? | Short and quick — presented as a batch, focused on patterns, use case, and constraints |
| Store inferred decisions automatically? | Show to user for confirmation first, then store confirmed ones via `update_context` |

## Design

### New tool: `bootstrap_context(repo_path)`

Added to `server.py`. Returns JSON:

```json
{
  "inferred": [
    "Python 3.12, uv as package manager (pyproject.toml)",
    "pytest for testing (pyproject.toml dev deps)",
    "GitHub Actions CI on push to main (.github/workflows/ci.yml)"
  ],
  "gaps": [
    "What is this repo's primary purpose? (1-2 sentences)",
    "What is the deployment target? (container, serverless, VPS, not yet decided...)",
    "Are there intentional technology exclusions or patterns to always follow?",
    "Are there performance, scale, or compliance constraints that shape technical decisions?"
  ],
  "existing_context_files": ["CLAUDE.md", "README.md", "pyproject.toml"]
}
```

Claude's job after receiving this:
1. Show `inferred` list to user and ask for confirmation
2. Store confirmed items via `update_context`
3. Present `gaps` as a quick-fire batch (not one at a time)
4. Store each answer via `update_context`

### Auto-trigger via SessionStart hook

`get_session_start_context` (in `store.py`) handles both cases:

- **Context exists:** inject decisions into session as before
- **No context:** inject a prompt telling Claude to offer bootstrapping, including the detected repo_path so Claude knows exactly what to pass to `bootstrap_context`

The hook detects empty context and tells Claude:
> "No context stored for repo '/path/to/repo'. Ask the user: 'No stored context found. I can scan this repo to build an initial baseline of decisions and constraints — should I?' If confirmed, call bootstrap_context with repo_path='/path/to/repo'."

The SessionStart hook also writes the detected repo path to `~/.contexer/.current_repo`. All four MCP tools accept `repo_path=""` and auto-detect from this file — so `capture_context` (called from the UserPromptSubmit hook) requires no hardcoded path.

### What the scanner reads

**Tier 1 — infer directly:**
- `pyproject.toml` / `package.json` / `Cargo.toml` / `go.mod` → runtime, package manager, key deps
- `.github/workflows/` / `.gitlab-ci.yml` → CI/CD system
- `ruff.toml` / `.eslintrc*` / `mypy.ini` / `.prettierrc*` → linting and style enforcement
- `Dockerfile` / `docker-compose.yml` → containerization
- `pytest.ini` / `jest.config.*` / `vitest.config.*` → test framework
- `*.tf` / `terraform/` / `k8s/` / `helm/` → infra and deployment

**Tier 2 — absence becomes a gap question:**
- No test config → "What is the testing approach?"
- No Dockerfile/k8s/terraform → "What is the deployment target?"

**Always ask (cannot be inferred):**
- Primary purpose
- Intentional technology exclusions / patterns
- Performance, scale, compliance constraints

### Gap question principles

Questions must be short and answerable in 1-2 sentences. Focus on:
- **Use case:** what is this for?
- **Patterns:** what should always/never be done?
- **Constraints:** what shapes technical decisions?

Present all gap questions at once as a quick-fire batch, not an extended interview.

### Implementation

`store.py` — two new functions:
- `bootstrap_scan(repo_path) -> dict` — static file scanner, returns `inferred`, `gaps`, `existing_context_files`
- `get_session_start_context(repo_path) -> dict` — used by SessionStart hook; returns the full hook JSON output including bootstrap prompt when no context exists

`server.py` — new tool:
```python
@mcp.tool()
def bootstrap_context(repo_path: str) -> str:
    """Scans a repo for inferable decisions and gap questions. Present inferred
    items to the user for confirmation, store confirmed ones via update_context,
    then ask the gap questions and store each answer."""
    return json.dumps(store.bootstrap_scan(repo_path), indent=2)
```

### Usage flow

```
New session — no context stored for this repo
    │
    ↓
SessionStart hook fires → get_session_start_context detects empty context
    └─▶ injects prompt: "No context found. Should I bootstrap?"
    │
    ↓
Claude asks user for confirmation
    │
    ↓
User confirms → Claude calls bootstrap_context("/path/to/repo")
    │
    ↓
Server returns: inferred decisions + gap questions
    │
    ↓
Claude shows inferred list → user confirms → Claude calls update_context for each
    │
    ↓
Claude presents gap questions as a quick batch
    │
    ↓
User answers → Claude calls update_context for each answer
    │
    ↓
Repo now has a rich decision baseline for all future sessions
```

### Edge cases

- **Repo already has context:** `bootstrap_scan` uses `_is_novel` against existing decisions — safe to run again, duplicates are skipped
- **Large repos:** scanner only reads known config file paths by name — O(1), never walks the full tree
- **Monorepos:** scan at root by default; pass a subdirectory path for per-service context

## Constraints preserved

- No new files — `bootstrap_scan` and `get_session_start_context` are in `store.py`; new tool is in `server.py`
- No LLM calls in the server — all reasoning stays in Claude
- Silent operation unchanged — bootstrap is always explicit (requires user confirmation)
