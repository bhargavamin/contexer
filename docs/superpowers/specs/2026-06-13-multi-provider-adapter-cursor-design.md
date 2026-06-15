# Multi-provider adapter + Cursor integration — design

**Date:** 2026-06-13
**Status:** Approved for planning
**Base branch:** built on `feat/pattern-promotion` (not `main`). Pattern-promotion only enriched `store.py` internals (occurrence_count ranking, architecture→pattern promotion, inline confidence); the three hook builders still return the same dict shape, so this adapter work is orthogonal — the neutral payload carries whatever (now richer) text the builders produce.
**Topic:** Make Contexer work with AI assistants beyond Claude Code, starting with Cursor, behind a clean per-provider adapter seam.

## Problem

Contexer's storage, novelty filter, retrieval, and bootstrap logic (`store.py`) and its MCP tool definitions (`server.py`) are already provider-agnostic — pure Python plus the open MCP standard. But Contexer only works with Claude Code today, because four thin spots hard-code Claude Code's integration surface:

1. `cli.py install` writes Claude Code's config files — `~/.claude.json` (MCP) and `~/.claude/settings.json` (hooks).
2. The hook-building functions in `store.py` (`get_session_start_context`, `get_bootstrap_context_prompt`, `get_post_compact_context`) return Claude Code's hook-output schema (`hookSpecificOutput.additionalContext`, `systemMessage`).
3. Three hooks use Claude Code's `mcp_tool` hook type (`capture_context`, `capture_user_constraint`, `get_context_for_prompt`) plus the `${prompt}` template variable.
4. Plugin packaging (`.claude-plugin/`, `${CLAUDE_PLUGIN_ROOT}`).

We want Cursor support, and a structure where a third tool (Windsurf, Copilot, Codex) is one new adapter rather than a rewrite.

## Goals

- Cursor users get the behavior that the benchmark proved is the only one that delivers value: **automatic session-start injection of stored rules** (see "Why session-start injection is the load-bearing feature").
- Silent auto-capture on Cursor (task + "always/never" constraint detection) with no agent involvement.
- A clean adapter seam: `store.py` stays provider-neutral; each tool is one adapter module.
- Auto-detecting install that wires whichever tools are present, with an explicit `--target` override.
- No regression to the working Claude Code flow (verified by the existing benchmark harness).

## Non-goals

- Replicating Claude Code features Cursor's platform cannot support (per-prompt context injection, compaction hooks). These degrade gracefully and are documented.
- Auto-writing rule files into user repositories by default (git-noise / intrusion).
- A plugin framework or config-driven adapter registry. Two concrete adapter modules behind one small interface — nothing more.

## Why session-start injection is the load-bearing feature

The benchmark (`bench/`, recorded in `store.py` history and the `jit-recall-regression` memory) found that opt-in retrieval delivers ~0% warm-recall: when the agent must *choose* to call `get_context`, it doesn't, so warm == cold. The value comes from **injecting stored rules into context automatically at session start**. Any provider integration that drops session-start injection repeats this known failure. Cursor's `sessionStart` hook supports exactly this, so the core value is achievable.

## Cursor's hook contract (verified)

From Cursor's hooks docs (Cursor 1.7+):

| Hook | Output schema | Can inject context? |
|---|---|---|
| `sessionStart` | `{ "env": {...}, "additional_context": "string" }` | **Yes** — `additional_context` is added to the conversation's initial system context. Input includes `workspace_roots[]`, `user_email`, `composer_mode`, `transcript_path`. Note: **no `source` field** (no startup/resume/compact discriminator). |
| `beforeSubmitPrompt` | `{ "continue": bool, "user_message": "string?" }` | **No** — can only allow/block + show a message. Input includes `prompt`, `attachments`. |
| `afterFileEdit` | *(no output fields)* | **No** |
| `beforeMCPExecution` | `{ "permission": "allow\|deny\|ask", "user_message?", "agent_message?" }` | No (permission gate only) |

Config file: `~/.cursor/hooks.json` (global) or `<repo>/.cursor/hooks.json` (project):

```json
{ "version": 1, "hooks": { "sessionStart": [{ "command": "...", "type": "command" }] } }
```

MCP registration: `~/.cursor/mcp.json` (global) or `<repo>/.cursor/mcp.json`, same `mcpServers` shape as `~/.claude.json`.

## Behavior parity matrix

| Contexer behavior | Claude Code | Cursor | Mechanism on Cursor |
|---|---|---|---|
| Inject stored constraints/conventions ("project rules") at session start | ✅ | ✅ **parity** | `sessionStart.additional_context` |
| Inject deferred arch/pattern count pointer | ✅ | ✅ | `sessionStart.additional_context` |
| Bootstrap offer when no context exists | ✅ | ✅ | `sessionStart.additional_context` |
| Write `~/.contexer/.current_repo` for repo resolution | ✅ (anchor hook) | ✅ | `sessionStart` reads `workspace_roots[0]`; also written by `beforeSubmitPrompt` |
| Capture first prompt as task | ✅ | ✅ | `beforeSubmitPrompt` command → `store.capture_task` (write only) |
| Auto-capture "always/never/from now on" constraints | ✅ | ✅ | `beforeSubmitPrompt` command → `store.capture_user_constraint` (silent; no ack injection) |
| Per-prompt rationale auto-injection (`get_context_for_prompt`) | ✅ | ❌ → nudge | Cannot inject per-prompt; covered by a behavioral nudge (below). Constraints already injected at session start, so only the *extra rationale fetch* is lost. |
| Post-edit "call `update_context`" reminder | ✅ | ⚠️ → nudge | `afterFileEdit` can touch `.pending_capture` but cannot deliver a reminder; covered by the behavioral nudge. |
| PreCompact / PostCompact re-injection | ✅ | ❌ dropped | Cursor exposes no compaction hooks. |
| Resume-awareness (skip re-inject on resume) | ✅ (`source` from stdin) | ❌ → safe default | Cursor's `sessionStart` has no `source` field. The adapter always calls the builders with `source=""`, so it re-injects every session start. Re-injecting on resume costs ~1k tokens but is correct; the optimization is Claude-only. |
| `get_context` / `update_context` / `update_global_context` MCP tools | ✅ | ✅ | `~/.cursor/mcp.json` |

**Behavioral nudge:** the two degraded items reduce to one short instruction Claude gets per-prompt — *"Call `get_context` before architecture/rationale questions; call `update_context` after significant decisions."* On Cursor this rides in the same `sessionStart.additional_context` block (zero repo pollution). See "Rules-file (optional)".

## Architecture — the adapter seam

### `store.py` — refactor to neutral payloads (logic unchanged)

The three hook builders currently return Claude-schema dicts. Split responsibility: `store.py` returns a **provider-neutral payload**, the adapter formats it.

Neutral payload shape:

```python
{ "status": "Contexer: ...",   # short human-facing status line (Claude shows it; Cursor has no equivalent → dropped)
  "context": "..." }            # the text to inject into the conversation
```

`get_session_start_context`, `get_bootstrap_context_prompt`, `get_post_compact_context` return this neutral shape. All storage/filter/bootstrap/insight logic is untouched — this is a return-shape change plus moving the schema keys out.

### `contexer/adapters/` — one module per tool

```
contexer/adapters/
  __init__.py     # registry: name -> adapter module; detect() returns installed targets
  base.py         # the adapter contract (documented below; duck-typed, no ABC needed)
  claude.py       # Claude Code adapter (current behavior, refactored out of cli.py + store.py)
  cursor.py       # Cursor adapter (new)
```

Each adapter exposes:

| Function | Responsibility |
|---|---|
| `name` | `"claude"` / `"cursor"` |
| `is_present()` | Detect the tool (`~/.claude` / `~/.cursor` exists) |
| `install()` | Write MCP registration + hook config + permissions for this tool |
| `uninstall(purge)` | Remove this tool's MCP + hooks, guarded by markers |
| `status()` | Per-target diagnostic line |
| `format_session_start(payload)` | neutral payload → this tool's `sessionStart` output JSON |
| `format_prompt_capture(payload)` | neutral payload → this tool's `beforeSubmitPrompt`/`UserPromptSubmit` output JSON |
| `hook_command(fn_name)` | Build the inline `python -c` command string for a given store entry point |

Claude formatter → `{"systemMessage": status, "hookSpecificOutput": {"hookEventName": ..., "additionalContext": context}}`.
Cursor formatter → `{"additional_context": context}` for `sessionStart`; `{"continue": true}` for `beforeSubmitPrompt` (capture is a write side-effect; Cursor can't inject the ack/reminder, so output is a pass-through).

### Capture-path unification (command hooks for both)

Today Claude uses `mcp_tool` hooks for `capture_context`, `capture_user_constraint`, `get_context_for_prompt`. Cursor has no `mcp_tool` hook type. **Both providers move to `command`-type hooks** that run `python -c "from contexer import adapters; print(adapters.<tool>.<entrypoint>(...))"`, calling the store function directly and printing adapter-formatted JSON.

One code path, two formatters. The store entry points already exist (`capture_task`, `capture_user_constraint`, `get_context_for_prompt`) — this is wiring, not new logic.

**Regression risk (accepted):** this changes Claude's currently-working capture path. The existing `install()` hook-migration logic (it already replaces old hook shapes) is reused to migrate `mcp_tool` hooks → command hooks. The benchmark harness must confirm auto-capture and constraint-detection still fire identically on Claude before merge.

### `cli.py` — iterate over selected adapters

- `contexer install` → `adapters.detect()`; wire each present tool. If none detected, default to Claude (back-compat).
- `--target claude|cursor|all` → explicit override.
- `uninstall [--purge]`, `reinstall`, `status` → loop over selected/installed adapters, print a section per target.
- Bare `contexer` (no args) still runs the MCP server — unchanged.

### Repo resolution on Cursor

`store._resolve_repo` reads `~/.contexer/.current_repo`. Cursor's `sessionStart` input provides `workspace_roots[]`; the Cursor `sessionStart` command writes `workspace_roots[0]` to `.current_repo`. The `beforeSubmitPrompt` command also writes it (mirrors Claude's anchor hook). No change to `_resolve_repo`.

### Rules-file — deferred to a future feature

**v1:** the behavioral nudge ships inside `sessionStart.additional_context` only — no file written into user repos. This fully covers the degraded items.

**Future (out of v1 scope):** `contexer install --target cursor --with-rules` writes a Contexer rule into the *current* repo (`.cursor/rules/contexer.mdc` or a marked block in `AGENTS.md`) carrying the same nudge, for durability across long/compacted sessions, guarded by a `managed by contexer` marker. Deferred per review — not built in this iteration; the seam should not preclude adding it later.

## Testing plan

- **Claude regression (gate):** run the `bench/` harness on Claude after the capture-path unification; auto-capture, constraint detection, and session-start injection must match pre-change behavior.
- **Cursor install smoke tests:** `install --target cursor` produces valid `~/.cursor/mcp.json` (correct `mcpServers.contexer`) and `~/.cursor/hooks.json` (correct `version`, `sessionStart`/`beforeSubmitPrompt` command entries).
- **Formatter unit tests:** neutral payload → Claude JSON and → Cursor JSON; assert exact keys (`additional_context` vs `hookSpecificOutput.additionalContext`).
- **sessionStart command output:** invoking the Cursor `sessionStart` entry point on a seeded store emits valid JSON with `additional_context` containing the project rules + pointer + nudge.
- **Capture command output:** `beforeSubmitPrompt` entry point writes the task/constraint to the store and prints valid Cursor pass-through JSON.
- **Idempotent install + clean uninstall** per target; `--target all` and auto-detect paths.
- **Adapter detection:** `detect()` returns the right targets for present/absent `~/.claude` and `~/.cursor`.

## Migration & back-compat

- Existing Claude installs: `reinstall` (or next `install`) migrates `mcp_tool` hooks → command hooks via the existing marker-guarded migration logic. No store changes; `~/.contexer/*.json` untouched.
- Bare `contexer install` with only Claude present behaves as today.

## Risks

1. **Claude capture regression** from the `mcp_tool` → command-hook move. Mitigation: benchmark gate before merge; migration reuses tested marker logic.
2. **Cursor schema drift.** The `additional_context` field and `beforeSubmitPrompt` output are Cursor 1.7+; a future Cursor version could change keys. Mitigation: schema isolated in `cursor.py` formatter; one place to fix.
3. **Cursor minimum version.** Hooks require Cursor 1.7+. `cursor.py install()` documents the requirement; older Cursor still gets MCP registration.
4. **Lost compaction re-injection on Cursor.** Accepted; documented as a known gap.

## Rollout phases (for the implementation plan)

1. Refactor `store.py` hook builders to neutral payloads; add `contexer/adapters/` with the Claude adapter reproducing current behavior exactly (no behavior change, benchmark-verified).
2. Unify capture hooks onto command hooks on the Claude adapter; benchmark gate.
3. Add the Cursor adapter (formatters, install/uninstall/status, hook commands).
4. Wire `cli.py` to detect + iterate adapters; add `--target`.
5. Docs (README + CLAUDE.md) for Cursor install and the parity matrix.

*(Deferred to a future iteration: `--with-rules` rules-file generation — see "Rules-file".)*
