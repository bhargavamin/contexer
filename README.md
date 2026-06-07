# Contexer

A lightweight MCP server that captures developer decisions during Claude Code sessions and surfaces them in future sessions — so context is never lost between restarts.

## The problem

AI coding sessions start blind. CLAUDE.md files decay. When Claude Code works autonomously, the reasoning behind decisions isn't captured anywhere. The next session has no idea what changed or why, and you waste the first few minutes re-explaining context before doing real work.

## How it works

Every session follows the same automatic flow:

```
You open Claude Code
  └─▶ SessionStart hook: injects project rules (all conventions + constraints) directly
      PLUS a count pointer for architecture/pattern decisions (JIT)
      OR: injects STOP directive if no context exists (triggers bootstrap)

You type your message
  └─▶ Anchor hook: writes git root to ~/.contexer/.current_repo (every prompt)
  └─▶ Bootstrap hook (once): if no context, injects directive to run bootstrap first
  └─▶ Capture hook (once): stores your first message as the task description
  └─▶ Rationale hook (every prompt): if the prompt asks "why / reason / rationale /
      decided", auto-fetches keyword-matching decisions and injects them as context

Claude works on your task
  └─▶ Claude calls get_context when it needs architecture/pattern context (JIT)
  └─▶ Claude calls update_context when it makes a significant decision

Context window nears limit
  └─▶ PreCompact hook: reminds Claude to call update_context before compaction

Compaction happens
  └─▶ PostCompact hook: reloads full stored context into Claude's working memory

Next session: repeat from the top — but now with history
```

**It is Claude — not you — who calls `update_context`.** You work normally. Claude nominates decisions; the server filters before storing. If Claude misses something important, say: **"store that decision"**.

## The five tools

| Tool | Triggered by | What it does |
|---|---|---|
| `capture_context` | `UserPromptSubmit` hook (once per session) | Stores the task description |
| `update_context` | Claude Code, mid-task | Nominates a decision; server filters before storing |
| `get_context` | Claude Code, JIT when task requires it | Loads stored decisions — optionally filtered by keyword or subtype |
| `get_context_for_prompt` | `UserPromptSubmit` hook (every prompt) | Detects "why/reason/rationale/decision" questions; auto-injects matching decisions as context; silent no-op for all other prompts |
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

**Plugin (recommended):**

```
/plugin marketplace add bhargavamin/contexer
/plugin install contexer@contexer
/reload-plugins
```

**Manual fallback:**

```bash
git clone git@github.com:bhargavamin/contexer.git ~/tools/contexer
bash ~/tools/contexer/scripts/install.sh
```

## Storing constraints from user instructions

**`capture_context` only stores tasks — it never creates decisions or constraints.**

The `UserPromptSubmit` hook calls `capture_context` with your first prompt and stores it as `type=task`. Even if your prompt is an imperative instruction ("always update the README before committing"), it is stored as a task, not a constraint. The novelty filter is not even applied to tasks.

For an instruction to become a stored constraint or convention, Claude must explicitly call `update_context` with `subtype=constraint` (or `convention`). This happens automatically when Claude recognises a significant decision during a task — but it requires Claude to complete a turn without interruption.

**If you type an instruction and it isn't stored as a constraint:**

- Say **"store that as a constraint"** — Claude will call `update_context` with the right subtype immediately
- Or let the full turn complete before interrupting — Claude calls `update_context` at the end of a turn

**Subtypes and when they are stored:**

| Subtype | Examples | When Claude stores it |
|---|---|---|
| `constraint` | "never commit untested code", "always update docs before committing" | Rule that must always apply |
| `convention` | "use uv not pip", "conventional commit format" | Agreed team/project standard |
| `architecture` | "chose FastMCP over low-level API" | Structural or framework decision |
| `pattern` | "use plain dicts as function boundaries" | Recurring implementation approach |

## What happens if a decision is missed

Nothing breaks — you just lose that piece of context for future sessions.

- Say **"store that decision"** and Claude will call `update_context` immediately
- Call `get_context` mid-session to see what's been captured so far
- Inspect the file directly: `cat ~/.contexer/<repo_slug>.json`

## License

MIT
