# Contexer

Contexer is a lightweight MCP server for Claude Code that automatically captures decisions made during coding sessions and surfaces them at the start of every future session — so Claude never starts blind.

## The problem

Every Claude Code session starts with no memory of the previous one. CLAUDE.md files require manual maintenance and go stale. When Claude works autonomously, the reasoning behind decisions disappears when the session ends. Teams end up re-explaining the same constraints, conventions, and architecture choices every time.

Contexer solves this by capturing decisions as they happen — silently, automatically, in the background — and replaying them as project rules at session start.

---

## Quick start

Install takes under two minutes. See **[docs/install.md](docs/install.md)** for full steps, verification, and uninstall.

**Plugin (recommended):**

```
/plugin marketplace add bhargavamin/contexer
/plugin install contexer@contexer
/reload-plugins
```

**Manual:**

```bash
git clone git@github.com:bhargavamin/contexer.git ~/tools/contexer
bash ~/tools/contexer/scripts/install.sh
```

After install, **open a new Claude Code session in any repo.** If no context exists, Claude will automatically run a bootstrap to capture your first decisions. If context exists, all constraints and conventions are injected at session start.

---

## How it works

You work normally. Contexer runs in the background via Claude Code hooks.

```
Session opens
  └─▶ All conventions + constraints injected as project rules
      Architecture/pattern decisions available on demand (JIT)
      First session with no context → bootstrap runs automatically

You type a prompt
  └─▶ Your first message is stored as the current task description
  └─▶ "Why/reason/rationale/decided" questions auto-fetch matching decisions

Claude works
  └─▶ Calls get_context when it needs architecture or pattern context
  └─▶ Calls update_context when it makes a significant decision

Context window nears limit
  └─▶ Claude is reminded to save any unsaved decisions before compaction
  └─▶ After compaction, full context is reloaded automatically
```

**You never call any tool directly.** Claude handles all tool calls. If Claude misses something, say *"store that decision"* and it will call `update_context` immediately.

---

## Decision types

Every stored decision has a `subtype` that controls when and how it is surfaced.

| Subtype | What it captures | Injected at session start? |
|---|---|---|
| `constraint` | Rules that must always apply — "never commit untested code" | Yes — always |
| `convention` | Team or project standards — "use uv not pip", "conventional commits" | Yes — always |
| `architecture` | Structural decisions — "chose FastMCP over low-level mcp.Server" | No — fetched on demand |
| `pattern` | Recurring implementation approaches — "plain dicts as function boundaries" | No — fetched on demand |

Constraints and conventions are injected directly at every session start because they apply to every task. Architecture and pattern decisions are large and task-specific — Claude fetches them just-in-time when the task requires them.

---

## Managing decisions

All operations use natural language. Claude translates them into the right tool call.

### Store a decision

```
"store that as a constraint"
"save this as a convention: always use uv not pip"
"remember this architecture decision"
```

Claude calls `update_context` with the content and the appropriate subtype. The server applies a novelty filter before storing — content with more than 70% token overlap with an existing decision is silently discarded as a duplicate.

> **Note:** Your first prompt each session is automatically stored as the *task description* — not as a decision or constraint. If you open a session with an instruction like *"always update docs before committing"*, it is captured as the task, not stored as a constraint. To store it as a constraint, either complete the turn (Claude will call `update_context` at the end) or explicitly say *"store that as a constraint"*.

### Query decisions

```
"show me all constraints"
"what decisions did we make about postgres?"
"show everything stored for this repo"
```

| Example call | What it returns |
|---|---|
| `get_context()` | Latest 10 decisions — overview |
| `get_context(entry_type="constraint")` | Up to 25 constraints |
| `get_context(query="postgres")` | Up to 25 decisions matching "postgres" |
| `get_context(limit=50)` | Up to 50 decisions |

### Update a decision

```
"update the uv decision — we switched back to pip"
"correct the constraint about commit format"
```

Claude calls `update_context` with the revised content. The old entry is not removed — a new entry is added alongside it. If the revised content is too similar to the original (>70% token overlap), it will be filtered as a duplicate. Rephrase it to include what changed.

### Remove a decision

```
"delete the postgres decision"
"remove all outdated constraints"
```

Claude reads `~/.contexer/<repo_slug>.json` and removes the matching entry directly. The file is plain JSON — you can also edit or prune it manually at any time.

---

## Tools reference

| Tool | Triggered by | What it does |
|---|---|---|
| `capture_context` | `UserPromptSubmit` hook — once per session | Stores the first prompt as the task description |
| `update_context` | Claude, mid-task | Nominates a decision; server filters before storing |
| `get_context` | Claude, on demand | Returns stored decisions — filtered by keyword or subtype |
| `get_context_for_prompt` | `UserPromptSubmit` hook — every prompt | Detects rationale questions and auto-injects matching decisions |
| `bootstrap_context` | Claude, first session with no context | Scans repo stack for inferable decisions; surfaces gap questions |

---

## Storage

Decisions are stored locally at `~/.contexer/<repo_slug>.json` — one file per repo, capped at 500 entries. No cloud, no database, no accounts.

Each entry contains:

| Field | Values |
|---|---|
| `id` | UUID |
| `type` | `task` or `decision` |
| `subtype` | `architecture` \| `constraint` \| `pattern` \| `convention` |
| `content` | The full decision text |
| `session_id` | UUID for the session that created it |
| `timestamp` | ISO 8601 UTC |

Inspect the store at any time:

```bash
cat ~/.contexer/<repo_slug>.json
```

---

## Troubleshooting

**Claude isn't storing decisions automatically.**
Claude calls `update_context` at the end of significant decisions, not continuously. If something specific was missed, say *"store that decision"* and Claude will call it immediately.

**A decision was stored but later ignored.**
Constraints and conventions are injected at session start. If you added a new constraint mid-session, it will appear from the next session onward.

**A decision is outdated or wrong.**
Say *"delete the X decision"* or edit `~/.contexer/<repo_slug>.json` directly and remove the entry.

**The novelty filter rejected a new decision.**
If the content is too similar to an existing one (>70% token overlap), it is silently discarded. Rephrase it to be more specific, or remove the old entry first.

**No context was injected at session start.**
If no decisions are stored for the repo, the bootstrap flow runs instead. Complete bootstrap once and all future sessions will have context.

---

## License

MIT
