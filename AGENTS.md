# AGENTS.md

This file provides guidance to Codex (Codex.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync

# Run the server (stdio transport — for manual testing)
uv run python server.py

# Smoke-test the server responds to MCP initialize
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}' | uv run python server.py
```

## Architecture

A small package (`contexer/`), intentionally minimal:

- **`contexer/server.py`** — MCP server entry point. Defines tools (`capture_context`, `capture_user_constraint`, `update_context`, `get_context`, `get_context_for_prompt`, `bootstrap_context`, `update_global_context`, `get_global_context`) using `FastMCP`. Generates a `SESSION_ID` (UUID) at process start shared across all tool calls in a session. Delegates all logic to `store.py`.
- **`contexer/store.py`** — All read/write and filtering logic. `_passes_filter` is the core gate: content is stored only if it is novel (token-overlap check — >70% overlap with existing decisions = duplicate). Storage is capped at `MAX_ENTRIES = 500` per repo. Display is separately capped: `_UNFILTERED_DISPLAY = 10` for overview calls, `_FILTERED_DISPLAY = 25` for query/type-filtered calls. Bootstrap is insight-aware: `_detect_insight(repo_path)` infers the user's familiarity (`high`/`medium`/`low` + decisive flag) from git signals — commit authorship by `user.email`, first-commit authorship, fresh-clone reflog entries. Detection order is load-bearing: the empty-email check must run before any `--author` query (an empty author pattern matches every commit). `bootstrap_scan(repo_path, insight)` filters gap questions by each gap's `min_insight`; empty `insight` auto-detects. Non-decisive detection makes the offer ask the user directly. Known limitation: commit count is repo-wide, so a few commits in one corner of a monorepo read as insight into the whole repo.
- **`contexer/adapters/`** — one module per AI-assistant target. `base.py` holds shared config-file helpers (`_load`/`_save`/marker checks). `Codex.py` and `cursor.py` each own that tool's MCP registration, hook wiring, install/uninstall/status, and the formatters that turn `store.py`'s neutral payloads (`session_start_payload`, `bootstrap_prompt_payload`, `post_compact_payload`) into that tool's hook-output JSON. (Codex's installed hook commands call the back-compat `store.get_*` wrappers, which delegate to those payload builders + `Codex.py`'s formatters; the Cursor adapter calls the payload builders directly.) `__init__.py` is the registry (`detect()` / `select()`). Add a tool = add a module here; `store.py` never changes. Cursor parity is capped by its platform: it injects context only at `sessionStart`, so per-prompt rationale injection and post-edit reminders degrade to a session-start nudge, and `PreCompact`/`PostCompact` are dropped.
- **`contexer/cli.py`** — the `contexer` console script: bare invocation runs the MCP server; subcommands `install [--target Codex|cursor|all]` / `uninstall [--purge]` / `reinstall` / `status` / `version` / `help` manage tool configs. `install` auto-detects present tools (`~/.Codex` → Codex, `~/.cursor` → Cursor) and falls back to Codex when neither is detected; `--target` overrides. Delegates all per-tool logic to `contexer/adapters/`. Also copies the packaged `/bootstrap` command (`contexer/bootstrap_command.md`, the canonical copy — there is no repo-level `.Codex/commands/` duplicate) to `~/.Codex/commands/bootstrap.md`; the write and the uninstall-removal are guarded by a `managed by contexer` marker so a user's own `bootstrap.md` is never touched.
- **`server.py`** (repo root) — back-compat shim importing `contexer.server`; keeps `uv run python server.py` working.
- **`requirements.txt`** — Kept for reference; `pyproject.toml` is the authoritative dependency spec managed by `uv`.

## Storage

Context is stored at `~/.contexer/<repo_slug>.json` — one file per repo, plus `_global.json` for cross-repo decisions. The slug is the repo path with non-alphanumeric characters replaced by underscores. Each file holds a flat list of entries, each with `id`, `type` (`task` | `decision`), `subtype` (`architecture` | `constraint` | `pattern` | `convention` — decisions only), `content`, `session_id`, and `timestamp`. Writes are atomic (unique temp file + `os.replace`, mode 0o600) so readers never see a torn file; corrupt files are read as empty rather than crashing.

**Which repo a tool call targets** is resolved by `store._resolve_repo(repo_path)` with this precedence: (1) an explicit, sane `repo_path` argument; (2) `_SESSION_REPO` — the repo bound to the running MCP-server process, captured once at startup from its own cwd's git root via `server.main` → `set_session_repo(_git_root(os.getcwd()))`; (3) the shared `~/.contexer/.current_repo` pointer as a last resort. The per-process binding (2) exists because `.current_repo` is a single global file every Codex/Cursor hook overwrites — a different tool or session could clobber it and send decisions to the wrong store file. `_is_sane_repo` rejects the home dir and tool config dirs (`~/.Codex`, `~/.cursor`, `~/.contexer`, `~/.config`) on every read and write, so a poisoned pointer (e.g. from the old `git rev-parse … || pwd` fallback run inside `~/.Codex`) can never select a config-dir store file.

## MCP integration

`contexer install` registers the server in `~/.Codex.json` under `mcpServers`, pointing at the installed console script:

```json
{
  "contexer": {
    "type": "stdio",
    "command": "/Users/<you>/.local/bin/contexer"
  }
}
```

(A from-source dev install via `scripts/install.sh` wires `uv run --directory <clone> python server.py` instead.)

For Cursor, the equivalents are `~/.cursor/mcp.json` (MCP server registration) and `~/.cursor/hooks.json` (hook wiring), managed by `contexer/adapters/cursor.py`. The MCP server entry in `mcp.json` uses the same `contexer` command; hooks use Cursor's `sessionStart` and `beforeSubmitPrompt` event names.

## Session behaviour (hooks)

`~/.Codex/settings.json` wires up hooks globally (applies to every repo):

- **`SessionStart`** — injects all conventions and constraints directly as project rules; injects a count pointer for deferred architecture/pattern decisions (fetched JIT via `get_context`); injects a bootstrap offer if no context exists. Resume-aware via the `source` field read from hook stdin (`source_from_hook_stdin`): on `resume` with context, skips re-injection entirely (the conversation already contains it); on `resume` without context, injects transcript-mining instructions instead of the menu (mine decisions from the visible conversation + store scan facts, all in the first turn — no human round-trip) and writes `~/.contexer/.resume_mining` so the UserPromptSubmit bootstrap fallback stays silent (the flag is consumed on first prompt; non-resume session starts clear stale flags).
- **`PostToolUse` (Write|Edit, silent flag)** — fires after every `Write` or `Edit` tool call. Touches `~/.contexer/.pending_capture` and returns `{}`. Completely silent — no UI output.
- **`UserPromptSubmit` (anchor, command, every prompt)** — writes the git root to `~/.contexer/.current_repo`, but only when actually inside a git work tree (no `|| pwd` fallback — that could poison the pointer with a non-repo dir). Also checks for `~/.contexer/.pending_capture`: if present, deletes it and injects `additionalContext` reminding Codex to call `update_context` for decisions from the previous turn. Silent to the user — only Codex sees the injected context.
- **`UserPromptSubmit` (bootstrap, command, once)** — calls `store.get_bootstrap_context_prompt` on the first prompt; injects the bootstrap offer if no context exists (fallback for when SessionStart is skipped).
- **`UserPromptSubmit` (capture, command, once)** — calls `Codex.capture_task` with the first user prompt as the task description. Stores as `type=task` only — never as a decision or constraint.
- **`UserPromptSubmit` (constraint, command, every prompt)** — calls `Codex.capture_constraint` with the prompt text. Detects prescriptive directives (`always X`, `never Y`, `from now on Z`) and stores them as `decision` entries automatically — no Codex involvement needed. Silent no-op for all other prompts.
- **`UserPromptSubmit` (rationale, command, every prompt)** — calls `Codex.rationale` with the prompt text; auto-injects matching decisions when the prompt contains rationale keywords (why, reason, rationale, decided, etc.). Silent no-op for all other prompts.
- **`PreCompact`** — injects a systemMessage reminding Codex to call `update_context` for any unsaved decisions before the context window is compacted.
- **`PostCompact`** — re-injects the full context via systemMessage so Codex resumes with full awareness after compaction.

**During a session**, call `update_context` whenever you make a significant decision, establish a pattern, or document a constraint. This is mandatory — not optional. After you write or edit files, the next user prompt will inject a reminder via `additionalContext`, but you should call it proactively within the same turn, not wait for the next prompt.

Call `update_context` for any of these:
- A technology, library, or approach chosen over alternatives (subtype: `architecture`)
- A file structure, naming convention, or code organization pattern established (subtype: `pattern`)
- A rule stated by the user or inferred from their preferences: "always X", "never Y" (subtype: `constraint` or `convention`)
- A build, deploy, or tooling convention established (subtype: `convention`)
- Any decision that would surprise a future Codex session if it wasn't stored

Pass the full reasoning, not just the conclusion. Pass `subtype` so decisions are retrievable by type. The server's novelty filter discards duplicates silently, so err on the side of calling it.

**Retrieving context JIT**: call `get_context` **before reading files** for any question about architecture, design decisions, rationale, constraints, patterns, or conventions. Fall back to reading files only when context is missing or the question is about current code state (exact syntax, current values). Use `query` for keyword search or `entry_type` to retrieve a specific subtype: `get_context(entry_type="constraint")` returns only constraints (up to 25). Use `limit` to override the display cap. When results are truncated, the output includes a `"showing N of M"` note so you know more exist.

## Design constraints

- **Silent operation is essential.** Tools must not produce noise — `update_context` silently discards filtered content without logging.
- **No abstraction beyond what exists.** The module structure (`server.py` / `store.py` / `cli.py` / `adapters/`) is intentional. Do not add classes, config files, or layers unless the spec changes. Adding support for a new tool = one new module in `adapters/`; no other files change.
- **`update_context` is called by Codex, not the developer.** Codex nominates content; the server filters. The filtering criterion is novelty — >70% token overlap with any existing decision is rejected as a duplicate, not an LLM call.
- **Git hooks and CLI commits are out of scope.** The MCP tool call path is the only capture mechanism.
