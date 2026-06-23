# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

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
- **`contexer/adapters/`** — one module per AI-assistant target. `base.py` holds shared config-file helpers (`_load`/`_save`/marker checks). `claude.py`, `cursor.py`, and `codex.py` each own that tool's MCP registration, hook wiring, install/uninstall/status, and the formatters that turn `store.py`'s neutral payloads (`session_start_payload`, `bootstrap_prompt_payload`, `post_compact_payload`) into that tool's hook-output JSON. (Claude's installed hook commands call the back-compat `store.get_*` wrappers, which delegate to those payload builders + `claude.py`'s formatters; the Cursor adapter calls the payload builders directly.) `__init__.py` is the registry (`detect()` / `select()`). Add a tool = add a module here; `store.py` never changes. Cursor parity is capped by its platform: it injects context only at `sessionStart`, so per-prompt rationale injection and post-edit reminders degrade to a session-start nudge, and `PreCompact`/`PostCompact` are dropped. Codex, by contrast, reaches near-full Claude parity: its `~/.codex/hooks.json` uses the *same* JSON schema and event names as Claude (`SessionStart`/`PostToolUse`/`PreCompact`/`PostCompact`/`UserPromptSubmit`, same `hookSpecificOutput`/`additionalContext`/`systemMessage`), so `codex.py` reuses Claude's runtime entrypoints verbatim (the hook command strings call `store.get_*` and `claude.capture_*` directly) and defines no formatters of its own. It differs from Claude only in plumbing: the MCP server is registered in `~/.codex/config.toml` (TOML), edited surgically so only the `[mcp_servers.contexer]` stanza changes (the rest of the user's config — plugins, marketplaces, projects, secrets — stays byte-for-byte intact); hooks live in a separate `~/.codex/hooks.json`; and there is no `permissions.allow` (Codex approves MCP tools interactively, like Cursor).
- **`contexer/memory_sync.py`** — imports Claude Code's memory-tool facts (`~/.claude/projects/<slug>/memory/*.md`) into the store, so decisions a session persisted to the file-based memory tool (instead of calling `update_context`) still reach Contexer. This is a *coexistence* path, not a competitor for capture: rather than fight an authority contest hook-injected reminders always lose against another tool's system-prompt memory workflow, Contexer becomes a deterministic sync target. No model in the loop — parses each fact's YAML frontmatter (`description`/`type`/`originSessionId`) + body, splits multi-section docs on `##` headings into one entry each (never a raw blob), assigns a subtype by keyword rules (ordered `convention` > `constraint` > `pattern` > `architecture`, with a frontmatter-`type` backstop — `feedback`→convention, `project`→architecture — and an architecture default, so *every* import is queryable via `get_context(entry_type=...)`), and upserts the whole dir in one load+save via `store.upsert_memory_batch`, keyed on a stable `memory_key` (source file + `##` section; repeated headings are disambiguated so they don't collide) so a fact reworded on disk updates its entry *in place* rather than silently dropping the edit (<30% change) or accumulating a near-duplicate (>30% change). First-time creation still passes the >70% novelty filter, so a memory fact that merely restates an existing decision is recorded as a recurrence, not double-stored across the two systems. Provenance: `session_id` = the fact's `originSessionId`. Fail-soft per file — a malformed memory file is skipped, never raised. The module is neutral (takes a dir path); `adapters/claude.py` owns the Claude-specific `_memory_dir(repo_path)` slug derivation (`re.sub(r"[^a-zA-Z0-9]", "-", repo_path)` — the one fragile coupling point, marked in code: if Claude Code changes its project-dir encoding, sync fails *safe* (finds nothing, never wrong data) but can go quietly dead) and `sync_memory(repo_path)`, which skips the whole import when a content fingerprint (`~/.contexer/.memory_synced_<slug>`) is unchanged.
- **`contexer/cli.py`** — the `contexer` console script: bare invocation runs the MCP server; subcommands `install [--target claude|cursor|codex|all]` / `uninstall [--purge]` / `reinstall` / `status` / `version` / `help` manage tool configs. `install` auto-detects present tools (`~/.claude` → Claude Code, `~/.cursor` → Cursor, `~/.codex` → Codex) and falls back to Claude Code when none is detected; `--target` overrides. Delegates all per-tool logic to `contexer/adapters/`. Also copies the packaged `/bootstrap` command (`contexer/bootstrap_command.md`, the canonical copy — there is no repo-level `.claude/commands/` duplicate) to `~/.claude/commands/bootstrap.md`; the write and the uninstall-removal are guarded by a `managed by contexer` marker so a user's own `bootstrap.md` is never touched.
- **`server.py`** (repo root) — back-compat shim importing `contexer.server`; keeps `uv run python server.py` working.
- **`requirements.txt`** — Kept for reference; `pyproject.toml` is the authoritative dependency spec managed by `uv`.

## Storage

Context is stored at `~/.contexer/<repo_slug>.json` — one file per repo, plus `_global.json` for cross-repo decisions. The slug is the repo path with non-alphanumeric characters replaced by underscores. Each file holds a flat list of entries, each with `id`, `type` (`task` | `decision`), `subtype` (`architecture` | `constraint` | `pattern` | `convention` — decisions only), `content`, `session_id`, and `timestamp`. Decisions imported from Claude Code's memory tool additionally carry `memory_key` — a stable identity (source file + `##` section) the importer upserts on, so a reworded memory fact updates its entry in place instead of duplicating. Writes are atomic (unique temp file + `os.replace`, mode 0o600) so readers never see a torn file; corrupt files are read as empty rather than crashing.

**Which repo a tool call targets** is resolved by `store._resolve_repo(repo_path)` with this precedence: (1) an explicit, sane `repo_path` argument; (2) `_SESSION_REPO` — the repo bound to the running MCP-server process, captured once at startup from its own cwd's git root via `server.main` → `set_session_repo(_git_root(os.getcwd()))`; (3) the shared `~/.contexer/.current_repo` pointer as a last resort. The per-process binding (2) exists because `.current_repo` is a single global file every Claude/Cursor hook overwrites — a different tool or session could clobber it and send decisions to the wrong store file. `_is_sane_repo` rejects the home dir and tool config dirs (`~/.claude`, `~/.cursor`, `~/.contexer`, `~/.config`) on every read and write, so a poisoned pointer (e.g. from the old `git rev-parse … || pwd` fallback run inside `~/.claude`) can never select a config-dir store file.

## MCP integration

`contexer install` registers the server in `~/.claude.json` under `mcpServers`, pointing at the installed console script:

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

For Codex, the MCP server is registered in `~/.codex/config.toml` (TOML) as `[mcp_servers.contexer]` with `command = "<contexer-bin>"`, and hooks live in `~/.codex/hooks.json` (JSON, same schema and event names as Claude's `settings.json` `hooks` block), both managed by `contexer/adapters/codex.py`. The `config.toml` edit is surgical text manipulation (validated with `tomllib` before writing) so only the contexer stanza is touched — `codex.py` adds no TOML-writer dependency.

## Session behaviour (hooks)

`~/.claude/settings.json` wires up hooks globally (applies to every repo):

- **`SessionStart`** — first runs `claude.sync_memory` to import any memory-tool facts (crash-recovery net: catches facts whose previous session ended without a clean `SessionEnd` flush), then injects all conventions and constraints directly as project rules; injects a count pointer for deferred architecture/pattern decisions (fetched JIT via `get_context`); injects a bootstrap offer if no context exists. Resume-aware via the `source` field read from hook stdin (`source_from_hook_stdin`): on `resume` with context, skips re-injection entirely (the conversation already contains it); on `resume` without context, injects transcript-mining instructions instead of the menu (mine decisions from the visible conversation + store scan facts, all in the first turn — no human round-trip) and writes `~/.contexer/.resume_mining` so the UserPromptSubmit bootstrap fallback stays silent (the flag is consumed on first prompt; non-resume session starts clear stale flags).
- **`PostToolUse` (Write|Edit, silent flag)** — fires after every `Write` or `Edit` tool call. Touches `~/.contexer/.pending_capture` and returns `{}`. Completely silent — no UI output.
- **`UserPromptSubmit` (anchor, command, every prompt)** — writes the git root to `~/.contexer/.current_repo`, but only when actually inside a git work tree (no `|| pwd` fallback — that could poison the pointer with a non-repo dir). Also checks for `~/.contexer/.pending_capture`: if present, deletes it and injects `additionalContext` reminding Claude to call `update_context` for decisions from the previous turn. Silent to the user — only Claude sees the injected context.
- **`UserPromptSubmit` (bootstrap, command, once)** — calls `store.get_bootstrap_context_prompt` on the first prompt; injects the bootstrap offer if no context exists (fallback for when SessionStart is skipped).
- **`UserPromptSubmit` (capture, command, once)** — calls `claude.capture_task` with the first user prompt as the task description. Stores as `type=task` only — never as a decision or constraint.
- **`UserPromptSubmit` (constraint, command, every prompt)** — calls `claude.capture_constraint` with the prompt text. Detects prescriptive directives (`always X`, `never Y`, `from now on Z`) and stores them as `decision` entries automatically — no Claude involvement needed. Silent no-op for all other prompts. To avoid crowding the store, `_is_prescriptive_constraint` rejects anything that isn't a clean standalone directive: pasted blobs over `_MAX_DIRECTIVE_LEN` (300) chars, fenced code dumps, and tool/system-injected text (`<task-notification>`, `<system-reminder>`, Contexer's own injected context) — these merely *contain* directive words and are never genuine user rules.
- **`UserPromptSubmit` (rationale, command, every prompt)** — calls `claude.rationale` with the prompt text; auto-injects matching decisions when the prompt contains rationale keywords (why, reason, rationale, decided, etc.). Silent no-op for all other prompts.
- **`PreCompact`** — runs `claude.sync_memory` to flush memory-tool facts before the context window collapses, then injects a systemMessage reminding Claude to call `update_context` for any unsaved decisions.
- **`PostCompact`** — re-injects the full context via systemMessage so Claude resumes with full awareness after compaction (no own sync — `PreCompact` ran moments before, so its import is already reflected).
- **`SessionEnd`** — runs `claude.sync_memory` to flush memory-tool facts on clean exit. The earlier decision to reject `SessionEnd` hooks was about *capture that needs the model to act* (a `SessionEnd` hook can't make Claude call a tool); this is a deterministic file import that needs no model, so it is valid here. These four triggers (`SessionStart`, `PreCompact`, `PostCompact` injection, `SessionEnd`) are the memory-sync surface; the import itself is idempotent, so running it at several of them is cheap.

**During a session**, call `update_context` whenever you make a significant decision, establish a pattern, or document a constraint. This is mandatory — not optional. After you write or edit files, the next user prompt will inject a reminder via `additionalContext`, but you should call it proactively within the same turn, not wait for the next prompt.

Call `update_context` for any of these:
- A technology, library, or approach chosen over alternatives (subtype: `architecture`)
- A file structure, naming convention, or code organization pattern established (subtype: `pattern`)
- A rule stated by the user or inferred from their preferences: "always X", "never Y" (subtype: `constraint` or `convention`)
- A build, deploy, or tooling convention established (subtype: `convention`)
- Any decision that would surprise a future Claude session if it wasn't stored

Pass the full reasoning, not just the conclusion. Pass `subtype` so decisions are retrievable by type. The server's novelty filter discards duplicates silently, so err on the side of calling it.

**Retrieving context JIT**: call `get_context` **before reading files** for any question about architecture, design decisions, rationale, constraints, patterns, or conventions. Fall back to reading files only when context is missing or the question is about current code state (exact syntax, current values). Use `query` for keyword search or `entry_type` to retrieve a specific subtype: `get_context(entry_type="constraint")` returns only constraints (up to 25). Use `limit` to override the display cap. When results are truncated, the output includes a `"showing N of M"` note so you know more exist.

## Design constraints

- **Silent operation is essential.** Tools must not produce noise — `update_context` silently discards filtered content without logging.
- **No abstraction beyond what exists.** The module structure (`server.py` / `store.py` / `cli.py` / `adapters/`) is intentional. Do not add classes, config files, or layers unless the spec changes. Adding support for a new tool = one new module in `adapters/`; no other files change.
- **`update_context` is called by Claude Code, not the developer.** Claude Code nominates content; the server filters. The filtering criterion is novelty — >70% token overlap with any existing decision is rejected as a duplicate, not an LLM call.
- **Git hooks and CLI commits are out of scope.** The MCP tool call path is the only capture mechanism.
