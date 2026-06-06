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

Three files, no more:

- **`server.py`** — MCP server entry point. Defines four tools (`capture_context`, `update_context`, `get_context`, `bootstrap_context`) using `FastMCP`. Generates a `SESSION_ID` (UUID) at process start shared across all tool calls in a session. Delegates all logic to `store.py`.
- **`store.py`** — All read/write and filtering logic. `_passes_filter` is the core gate: content is stored only if it is novel (token-overlap check — >70% overlap with existing decisions = duplicate). Storage is capped at `MAX_ENTRIES = 500` per repo. Display is separately capped: `_UNFILTERED_DISPLAY = 10` for overview calls, `_FILTERED_DISPLAY = 25` for query/type-filtered calls.
- **`requirements.txt`** — Kept for reference; `pyproject.toml` is the authoritative dependency spec managed by `uv`.

## Storage

Context is stored at `~/.contexer/<repo_slug>.json` — one file per repo. The slug is the repo path with non-alphanumeric characters replaced by underscores. Each file holds a flat list of entries, each with `id`, `type` (`task` | `decision`), `subtype` (`architecture` | `constraint` | `pattern` | `convention` — decisions only), `content`, `session_id`, and `timestamp`.

## MCP integration

The server is registered in `~/.claude.json` under `mcpServers`:

```json
{
  "contexer": {
    "type": "stdio",
    "command": "uv",
    "args": ["run", "--directory", "/Users/bhargavamin/repos/personal/contexer", "python", "server.py"]
  }
}
```

## Session behaviour (hooks)

`~/.claude/settings.json` wires up hooks globally (applies to every repo):

- **`SessionStart`** — injects a count pointer (`"N decisions stored — call get_context when relevant"`) if context exists; injects the bootstrap STOP directive if no context exists. Does **not** pre-load decisions — Claude fetches them JIT via `get_context`.
- **`UserPromptSubmit` (anchor, every prompt)** — writes the git root to `~/.contexer/.current_repo` so MCP tools can resolve `repo_path=""` correctly even across concurrent sessions.
- **`UserPromptSubmit` (once)** — fires `get_bootstrap_context_prompt` on the first prompt; injects the bootstrap directive if no context exists (fallback for when SessionStart is skipped).
- **`UserPromptSubmit` (once, mcp_tool)** — calls `capture_context` with the first user prompt as the task description.
- **`PreCompact`** — injects a systemMessage reminding Claude to call `update_context` for any unsaved decisions before the context window is compacted.
- **`PostCompact`** — re-injects the full context via systemMessage so Claude resumes with full awareness after compaction.

**During a session**, call `update_context` whenever you make a significant decision, establish a pattern, or document a constraint. Pass the full reasoning, not just the conclusion. Optionally pass `subtype` (`architecture` | `constraint` | `pattern` | `convention`) to enable filtered retrieval later. The server filters — if content doesn't meet the novelty criteria it will be silently discarded, so err on the side of calling it.

**Retrieving context JIT**: call `get_context` when the task requires project knowledge. Use `query` for keyword search or `entry_type` to retrieve a specific subtype: `get_context(entry_type="constraint")` returns only constraints (up to 25). Use `limit` to override the display cap. When results are truncated, the output includes a `"showing N of M"` note so you know more exist.

## Design constraints

- **Silent operation is essential.** Tools must not produce noise — `update_context` silently discards filtered content without logging.
- **No abstraction beyond what exists.** The three-file structure is intentional. Do not add classes, config files, or layers unless the spec changes.
- **`update_context` is called by Claude Code, not the developer.** Claude Code nominates content; the server filters. The filtering criterion is novelty — >70% token overlap with any existing decision is rejected as a duplicate, not an LLM call.
- **Git hooks and CLI commits are out of scope.** The MCP tool call path is the only capture mechanism.
