# Contexer

A lightweight MCP server that captures developer decisions during Claude Code sessions and surfaces them in future sessions — so context is never lost between restarts.

## The problem

AI coding sessions start blind. CLAUDE.md files decay. When Claude Code works autonomously, the reasoning behind decisions isn't captured anywhere. The next session has no idea what changed or why, and you waste the first few minutes re-explaining context before doing real work.

## How it works

Every session follows the same automatic flow:

```
You open Claude Code
  └─▶ SessionStart hook: injects count pointer ("N decisions stored — call get_context")
      OR: injects STOP directive if no context exists (triggers bootstrap)

You type your first message
  └─▶ Anchor hook: writes git root to ~/.contexer/.current_repo (every prompt)
  └─▶ Bootstrap hook (once): if no context, injects directive to run bootstrap first
  └─▶ Capture hook (once): stores your first message as the task description

Claude works on your task
  └─▶ Claude calls get_context when it needs project context (JIT — not pre-loaded)
  └─▶ Claude calls update_context when it makes a significant decision

Context window nears limit
  └─▶ PreCompact hook: reminds Claude to call update_context before compaction

Compaction happens
  └─▶ PostCompact hook: reloads full stored context into Claude's working memory

Next session: repeat from the top — but now with history
```

**It is Claude — not you — who calls `update_context`.** You work normally. Claude nominates decisions; the server filters before storing. If Claude misses something important, say: **"store that decision"**.

## The four tools

| Tool | Triggered by | What it does |
|---|---|---|
| `capture_context` | `UserPromptSubmit` hook (once per session) | Stores the task description |
| `update_context` | Claude Code, mid-task | Nominates a decision; server filters before storing |
| `get_context` | Claude Code, JIT when task requires it | Loads stored decisions — optionally filtered by keyword or subtype |
| `bootstrap_context` | Claude Code, first session with no context | Scans the repo stack and returns inferred facts + gap questions |

## The filter

`update_context` does not store everything Claude sends. It applies one gate and silently discards failures:

**Novelty:** content that overlaps >70% with any existing stored decision (token overlap) is rejected as a duplicate. Novel content is always stored — `update_context` is only called for significant decisions, so if it passes the novelty check, it is worth keeping.

If filtered, the content is silently discarded. No noise, no logs.

## Storage

Context is stored at `~/.contexer/<repo_slug>.json` — one file per repo, capped at 500 entries. Each entry has `id`, `type` (`task` | `decision`), `subtype` (`architecture` | `constraint` | `pattern` | `convention`), `content`, `session_id`, and `timestamp`. No cloud, no database, no external dependencies.

Use `entry_type` when calling `get_context` to retrieve filtered views:
```
get_context(entry_type="constraint")   → up to 25 constraints
get_context(query="postgres")          → up to 25 decisions matching "postgres"
get_context()                          → latest 10 decisions (overview)
```

## Install

→ See **[docs/install.md](docs/install.md)** for full install steps, verification, and uninstall.

**Quick start (plugin — recommended):**

```
/plugin install contexer@contexer
```

Restart Claude Code, then `/mcp` — `contexer` should appear as connected.

**Manual fallback:**

```bash
git clone https://github.com/bhargavamin/contexer.git ~/tools/contexer
bash ~/tools/contexer/install.sh
```

## What happens if a decision is missed

Nothing breaks — you just lose that piece of context for future sessions.

- Say **"store that decision"** and Claude will call `update_context` immediately
- Call `get_context` mid-session to see what's been captured so far
- Inspect the file directly: `cat ~/.contexer/<repo_slug>.json`

## License

MIT
