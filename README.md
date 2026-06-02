# Contexer

A lightweight MCP server that captures developer intent during Claude Code sessions and injects it into future sessions — so context is never lost between restarts.

## The problem

AI coding sessions start blind. CLAUDE.md files decay. When Claude Code commits autonomously, the reasoning behind decisions isn't captured anywhere. The next session has no idea what changed or why, and you waste the first few minutes re-explaining context before doing real work.

## How it works

Contexer runs as a local MCP server alongside Claude Code. It exposes three tools:

| Tool | When it's called | What it does |
|---|---|---|
| `capture_context` | Start of every task | Saves the task description against the repo |
| `update_context` | Mid-task, on significant decisions | Filters and stores decisions, patterns, and constraints |
| `get_context` | Start of every new session | Returns stored context for the current repo |

Claude Code calls these tools automatically. You don't have to do anything manually.

The server filters what goes in — content is only stored if it describes a decision, establishes a pattern, documents a constraint or tradeoff, or is meaningfully different from what's already stored. Everything else is silently discarded.

## Installation

**Requires:** Python 3.12+, [uv](https://github.com/astral-sh/uv)

```bash
git clone https://github.com/bhargavamin/contexer.git
cd contexer
uv sync
```

## Register with Claude Code

Add to `~/.claude.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "contexer": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/contexer", "python", "server.py"]
    }
  }
}
```

## Register with Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "contexer": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/contexer", "python", "server.py"]
    }
  }
}
```

Restart Claude Code / Claude Desktop after editing either config.

## Storage

Context is stored at `~/.contexer/<repo_slug>.json` — one file per repo, capped at 50 entries. No cloud, no database, no external dependencies.

## Verify it's working

After restarting, run `/mcp` in Claude Code — `contexer` should appear as connected. Then ask Claude to call `get_context` with your repo path to confirm the tools are reachable.

## License

MIT
