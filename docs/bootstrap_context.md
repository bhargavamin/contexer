# Bootstrap Context

Bootstrap initialises a context store for a repo that has no stored decisions. It scans the repo statically, produces a list of detected facts and assumptions, and guides Claude through confirming each one — storing every answer in the context store so future sessions start with a rich baseline.

---

## How it triggers

Bootstrap runs automatically when a session starts in a repo with no stored context. There are two trigger points, in order:

### 1. SessionStart hook

When Claude Code starts a session, the `SessionStart` hook calls `get_session_start_context`. If no decisions exist for the repo, it injects a bootstrap directive into `additionalContext` before Claude responds to anything.

```
Session starts
  └─▶ SessionStart hook fires
      └─▶ No context found → injects bootstrap directive
          └─▶ Claude confirms items one at a time on the user's first message
```

### 2. UserPromptSubmit once-hook (fallback)

If the `SessionStart` hook was skipped (non-interactive session, hook failure), the `UserPromptSubmit` hook fires on the user's first message. It calls `get_bootstrap_context_prompt` — same logic, same output, same effect.

Once either hook stores at least one decision, both return no-op on every subsequent call (empty dict).

---

## What Claude receives

Both hooks inject an 8-line imperative directive into `additionalContext`:

```
STOP — do not answer the user's request yet.
Repo: <repo_path>. No stored context — complete a quick bootstrap first.
Do this now, before anything else:
  1. Call the bootstrap_context MCP tool with repo_path='' to get items to confirm.
  2. Take the first item. State it to the user, ask 'Correct? yes / no / [correction]'. Wait for reply.
  3. Call update_context to store the confirmed fact. Then move to item 2.
  4. Repeat until all items are done. Then — and only then — address the user's original request.
Start by calling bootstrap_context now.
```

The repo path is embedded in line 2 so Claude can confirm which repo is being bootstrapped. It still passes `repo_path=''` to the MCP tool — auto-detect via `.current_repo` handles resolution.

The directive does **not** embed the item list. Claude calls `bootstrap_context` to get structured JSON, which it can reliably iterate. Embedding items as a pipe-separated inline string caused Claude to skip bootstrap — it had nothing concrete to act on and treated `additionalContext` as a soft hint rather than a hard stop.

---

## What gets scanned

`bootstrap_scan` reads known config file paths only — it never walks the full tree.

| Source | What it infers |
|---|---|
| `pyproject.toml` | Python runtime, package manager, test framework, key deps |
| `package.json` | Node.js project name, framework (express, next, react...), test framework |
| `Cargo.toml` | Rust project |
| `go.mod` | Go module |
| `.github/workflows/` | CI present |
| `Dockerfile` / `docker-compose.yml` | Containerised |
| `ruff.toml`, `.eslintrc*`, `mypy.ini`, `.prettierrc*` | Linting / style enforcement |
| `terraform/`, `k8s/`, `helm/` | Infrastructure / deployment |

**Detected facts** are stated directly (e.g. `Framework: express`).

**Gap questions** are generated only when a signal is missing or ambiguous. Every question is conditional — nothing is hardcoded or asked unconditionally (except purpose, which can never be inferred from code).

| Gap | Asked when |
|---|---|
| Purpose | Always — code can't tell you *why* a repo exists |
| Tests | No test framework detected |
| CI/CD | No CI config found |
| Deployment | No Dockerfile or infra config found |
| Cloud deploy | Cloud SDK present but no deploy config |
| Compliance | Auth or payment deps detected |
| Team conventions | Architecture signals suggest collaboration |
| Exclusions | Dep tree has enough choices to warrant it (>5 deps or ORM present) |
| Constraints | Production signals present (auth, cloud, infra, container) |

**Hints are stack-aware.** A Python repo gets pytest examples; a Node repo gets Jest/Vitest examples; a Rust repo gets cargo test examples. No cross-stack noise.

---

## Manual invocation: `/bootstrap`

Type `/bootstrap` at any time to trigger a guided setup on demand — even if context already exists.

```
/bootstrap
```

Claude will:
1. Call `bootstrap_context` MCP tool with `repo_path=""`
2. Present each detected and assumed item one at a time
3. Ask `Correct? yes / no / [your correction]` — wait for reply before moving on
4. Call `update_context` after each answer to store the confirmed fact
5. Report how many items were stored when done

Use this when:
- You want to seed context for a repo that already has some entries (safe — `update_context` deduplicates)
- The automatic bootstrap was interrupted mid-way
- You want to re-confirm assumptions after major changes to the stack

---

## Resetting bootstrap

To trigger automatic bootstrap again for a repo, clear its context store:

```bash
# find the store file
ls ~/.contexer/

# clear it (slug = repo path with non-alphanumeric chars replaced by _)
echo '{"entries":[]}' > ~/.contexer/<repo_slug>.json
```

The next session start will detect no decisions and re-run bootstrap automatically.

---

## Hook chain (technical)

```
UserPromptSubmit hooks fire in order:
  1. command (every prompt) — writes git root to ~/.contexer/.current_repo
  2. command (once)         — get_bootstrap_context_prompt → injects directive if no context
  3. mcp_tool (once)        — capture_context stores the user's first prompt as a task entry
```

The anchor hook (step 1) ensures `.current_repo` always points to the current session's repo before any MCP tool resolves `repo_path=""`. This prevents cross-repo contamination when multiple sessions run concurrently.

The every-prompt update_context reminder was removed — it added tokens on every turn regardless of whether a decision was possible. Claude is briefed once (via SessionStart or the STOP directive) and calls `update_context` based on that standing instruction.

---

## Implementation

| File | Function | Role |
|---|---|---|
| `store.py` | `bootstrap_scan(repo_path)` | Static scanner — returns `inferred` list and `gaps` list |
| `store.py` | `_build_bootstrap_context(repo_path, hook_event)` | Formats scan output into 3-line directive |
| `store.py` | `get_session_start_context(repo_path)` | SessionStart hook handler — bootstrap or load context |
| `store.py` | `get_bootstrap_context_prompt(repo_path)` | UserPromptSubmit hook handler — bootstrap fallback |
| `server.py` | `bootstrap_context(repo_path)` | MCP tool — exposes raw scan JSON for `/bootstrap` command |
| `.claude/commands/bootstrap.md` | — | Slash command definition for manual invocation |
