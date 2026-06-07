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

After install, open a new Claude Code session in any repo. If no context exists, Claude will run a short bootstrap to capture your first decisions. From that point on, your project rules are injected automatically at every session start.

---

## How it works

You work normally. Contexer runs silently in the background.

- **Session start** — all your constraints and conventions are injected as project rules before you type anything
- **As you work** — Claude captures significant decisions automatically; you never have to do it manually
- **"Why" questions** — if you ask about rationale or past decisions, Contexer auto-fetches the relevant ones
- **Context window limit** — decisions are saved before compaction and restored after, so nothing is lost

**You never call anything directly.** If Claude misses something, say *"store that decision"* and it will be captured immediately.

---

## Decision types

Every stored decision has a type that controls when it is surfaced.

| Type | What it captures | Surfaced at session start? |
|---|---|---|
| `constraint` | Rules that must always apply — "never commit untested code" | Yes — always |
| `convention` | Team or project standards — "use uv not pip", "conventional commits" | Yes — always |
| `architecture` | Structural decisions — "chose REST over GraphQL for this service" | No — fetched when relevant |
| `pattern` | Recurring implementation approaches — "always validate at the boundary" | No — fetched when relevant |

Constraints and conventions apply to every task, so they load immediately. Architecture and pattern decisions are fetched on demand when the task requires them.

---

## Managing decisions

All operations use natural language.

### Store a decision

```
"store that as a constraint"
"save this as a convention: always use uv not pip"
"remember this architecture decision"
```

> **Note:** Your first prompt each session is captured as the current task description, not as a constraint. If you open a session with an instruction like *"always update docs before committing"*, say *"store that as a constraint"* to make sure it is saved correctly.

### Query decisions

```
"show me all constraints"
"what decisions did we make about postgres?"
"show everything stored for this repo"
```

### Update a decision

```
"update the uv decision — we switched back to pip"
"correct the constraint about commit format"
```

The old entry stays alongside the new one. If the revision is too similar to the original, rephrase it to include what changed.

### Remove a decision

```
"delete the postgres decision"
"remove all outdated constraints"
```

You can also edit the store file directly — it is plain JSON at `~/.contexer/`.

---

## Troubleshooting

**Claude isn't storing decisions automatically.**
Say *"store that decision"* and Claude will capture it immediately.

**A decision was stored but isn't appearing.**
Constraints and conventions load at session start. If you added one mid-session, it will appear from the next session onward.

**A decision is outdated or wrong.**
Say *"delete the X decision"* or edit the store file directly.

**A new decision wasn't saved — it looks like a duplicate.**
If the content is too close to an existing decision, it is silently skipped. Rephrase it to include what specifically changed.

**No context appeared at session start.**
This happens the first time you open a repo — bootstrap will run to capture your initial decisions. Complete it once and all future sessions will have context.

---

## License

MIT
