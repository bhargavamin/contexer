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

- **`server.py`** — MCP server entry point. Defines the three tools (`capture_context`, `update_context`, `get_context`) using `FastMCP`. Generates a `SESSION_ID` (UUID) at process start shared across all tool calls in a session. Delegates all logic to `store.py`.
- **`store.py`** — All read/write and filtering logic. The filtering in `_passes_filter` is the core intelligence: content passes if it matches any one of four criteria (decision signal, pattern signal, constraint signal, or novelty). Novelty is a token-overlap check against existing entries (>70% overlap = duplicate). Storage is capped at `MAX_ENTRIES = 50` per repo.
- **`requirements.txt`** — Kept for reference; `pyproject.toml` is the authoritative dependency spec managed by `uv`.

## Storage

Context is stored at `~/.contexer/<repo_slug>.json` — one file per repo. The slug is the repo path with non-alphanumeric characters replaced by underscores. Each file holds a flat list of entries, each with `id`, `type` (`task` | `decision`), `content`, `session_id`, and `timestamp`.

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

`.claude/settings.json` wires up two hooks automatically:

- **`SessionStart`** — calls `get_context` to load stored decisions into the session before the first message
- **`UserPromptSubmit` (once)** — calls `capture_context` with the first user prompt as the task description

**During a session**, call `update_context` whenever you make a significant decision, establish a pattern, or document a constraint. Pass the full reasoning, not just the conclusion. The server filters — if it doesn't meet the criteria it will be silently discarded, so err on the side of calling it.

## Design constraints

- **Silent operation is essential.** Tools must not produce noise — `update_context` silently discards filtered content without logging.
- **No abstraction beyond what exists.** The three-file structure is intentional. Do not add classes, config files, or layers unless the spec changes.
- **`update_context` is called by Claude Code, not the developer.** Claude Code nominates content; the server filters. The filtering criteria are heuristic keyword signals, not an LLM call.
- **Git hooks and CLI commits are out of scope.** The MCP tool call path is the only capture mechanism.
