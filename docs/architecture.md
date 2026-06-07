# Architecture

Internal reference for how Contexer works — hooks, tools, filtering logic, and storage layout.

---

## Session flow

```
You open Claude Code
  └─▶ SessionStart hook: injects project rules (all conventions + constraints) directly
      PLUS a count pointer for architecture/pattern decisions (JIT)
      OR: injects STOP directive if no context exists (triggers bootstrap)

You type your message
  └─▶ Anchor hook (command, every prompt): writes git root to ~/.contexer/.current_repo
  └─▶ Bootstrap hook (mcp_tool, once): if no context, injects directive to run bootstrap first
  └─▶ Capture hook (mcp_tool, once): stores first prompt as the task description via capture_context
  └─▶ Rationale hook (mcp_tool, every prompt): if prompt contains "why / reason / rationale /
      decided", calls get_context_for_prompt to auto-inject keyword-matching decisions

Claude works on your task
  └─▶ Claude calls get_context when it needs architecture/pattern context (JIT)
  └─▶ Claude calls update_context when it makes a significant decision

Context window nears limit
  └─▶ PreCompact hook: systemMessage reminds Claude to call update_context before compaction

Compaction happens
  └─▶ PostCompact hook: reloads full stored context into Claude's working memory via systemMessage

Next session: repeat from the top — but now with history
```

---

## Hook configuration

All hooks live in `~/.claude/settings.json` (global — applies to every repo).

| Event | Type | Fires | Purpose |
|---|---|---|---|
| `SessionStart` | `command` | Once at session open | Injects conventions/constraints; defers architecture/patterns |
| `UserPromptSubmit` (anchor) | `command` | Every prompt | Writes `git rev-parse --show-toplevel` to `~/.contexer/.current_repo` |
| `UserPromptSubmit` (bootstrap) | `mcp_tool` | Once, if no context | Calls `get_bootstrap_context_prompt` to inject STOP directive |
| `UserPromptSubmit` (capture) | `mcp_tool` | Once per session | Calls `capture_context` with the first user prompt |
| `UserPromptSubmit` (rationale) | `mcp_tool` | Every prompt | Calls `get_context_for_prompt` with the prompt text |
| `PreCompact` | `command` | Before compaction | Outputs `systemMessage` reminding Claude to save decisions |
| `PostCompact` | `command` | After compaction | Outputs `systemMessage` with full stored context |

**Important:** `${prompt}` substitution only works in `mcp_tool`-type hooks — it is treated as a literal string in `command`-type hooks. The anchor hook is `command`-type specifically because it does not need the prompt text; it only needs to write the git root.

The anchor hook runs first (as a `command` hook) before any `mcp_tool` hooks fire, ensuring `~/.contexer/.current_repo` is set before `capture_context` or `get_context_for_prompt` resolve `repo_path=""`.

---

## The five MCP tools

| Tool | Caller | What it does |
|---|---|---|
| `capture_context` | `UserPromptSubmit` hook (once) | Calls `capture_task()` — stores as `type=task`. Never stores as a decision or constraint regardless of content. |
| `update_context` | Claude Code, mid-task | Calls `update_decision()` — applies novelty filter, stores as `type=decision` with optional subtype. |
| `get_context` | Claude Code, JIT | Returns stored decisions optionally filtered by keyword (`query`) or subtype (`entry_type`). Display capped at 10 (unfiltered) or 25 (filtered). |
| `get_context_for_prompt` | `UserPromptSubmit` hook (every prompt) | Detects rationale keywords; extracts content keywords; calls `get_context` with each keyword; injects first match as `additionalContext`. Silent no-op for non-rationale prompts. |
| `bootstrap_context` | Claude Code, first session | Calls `bootstrap_scan()` — scans repo for inferable decisions and returns gap questions. |

---

## Novelty filter

`update_context` applies one gate before storing:

**Token overlap check:** the content is tokenised by whitespace and lowercased. If any existing `decision` entry shares more than 70% token overlap (by Jaccard-style ratio: `overlap / max(len_a, len_b)`), the content is rejected as a duplicate.

Tasks bypass this filter entirely — `capture_task()` does not call `_passes_filter()`.

If filtered, the content is silently discarded. No log, no error, no return value change visible to Claude (it receives `"Filtered — did not meet storage criteria."`).

---

## `capture_context` vs `update_context`

These are the two write paths and they are completely separate:

| | `capture_context` | `update_context` |
|---|---|---|
| **Caller** | `UserPromptSubmit` hook (automatic) | Claude (explicit tool call) |
| **Stores as** | `type=task` | `type=decision` |
| **Subtype** | None | `architecture` \| `constraint` \| `pattern` \| `convention` |
| **Novelty filter** | No | Yes |
| **Frequency** | Once per session | As many times as needed |

This means: if a user opens a session with an imperative instruction as their first prompt ("always update docs before committing"), it is captured as a `task`, not a `constraint`. For it to become a `constraint`, Claude must explicitly call `update_context` with `subtype=constraint`.

---

## Storage layout

```
~/.contexer/
  .current_repo          # last git root written by anchor hook
  <repo_slug>.json       # one file per repo
```

The slug is the repo path with the leading `/` stripped and all non-alphanumeric characters (except `-` and `_`) replaced by `_`.

Example: `/Users/alice/projects/my-api` → `Users_alice_projects_my-api.json`

Each file is a JSON object:

```json
{
  "repo_path": "/Users/alice/projects/my-api",
  "entries": [
    {
      "id": "uuid",
      "type": "task",
      "content": "...",
      "session_id": "uuid",
      "timestamp": "2026-06-07T10:00:00+00:00"
    },
    {
      "id": "uuid",
      "type": "decision",
      "subtype": "constraint",
      "content": "...",
      "session_id": "uuid",
      "timestamp": "2026-06-07T10:05:00+00:00"
    }
  ]
}
```

Entries are capped at 500. Display is separately capped: 10 for unfiltered overview calls, 25 for filtered calls. Both caps can be overridden with the `limit` parameter on `get_context`.

---

## Source files

```
server.py    — MCP server entry point; defines the five tools using FastMCP
store.py     — All read/write and filtering logic; no shared mutable state
```

`SESSION_ID` is a UUID generated once at process start and shared across all tool calls in the session. It is stored on each entry so decisions can be grouped by session if needed.
