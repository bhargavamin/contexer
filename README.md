<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="assets/logo-light.svg">
  <img alt="Contexer" src="assets/logo-dark.svg" height="60">
</picture>

[![PyPI version](https://img.shields.io/pypi/v/contexer)](https://pypi.org/project/contexer/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![Discord](https://img.shields.io/badge/Discord-Join-5865F2?logo=discord&logoColor=white)](https://discord.gg/Fk6JSaW4p)

**[contexer.ai](https://contexer.ai)** · [Discord](https://discord.gg/Fk6WSaW4p) · [Docs](docs/install.md)

Contexer is an MCP server for Claude Code that **automatically remembers every decision made during a session and replays them at the start of every future session** — so Claude never starts blind again.

---

## The problem

Every Claude Code session starts completely fresh. There is no memory of what was decided yesterday, last week, or five minutes ago in a different window.

This means you repeat yourself — constantly:

- *"We use uv, not pip"* — every session
- *"Always write tests before committing"* — every session
- *"We chose Postgres over MongoDB because of transactions"* — every session

CLAUDE.md files help but require manual maintenance and go stale. When Claude works autonomously, the reasoning behind its choices vanishes when the session ends. Teams waste the first 10–15 minutes of every session re-establishing context that Claude already had yesterday.

---

## What Contexer does

Contexer runs silently in the background and fixes this in three ways:

**1 — It captures decisions as you work.**
When Claude makes a meaningful choice — picks a library, establishes a pattern, agrees to a rule — Contexer stores it automatically. You never manually copy decisions into a file. If Claude misses something, say *"store that as a constraint"* and it is saved immediately.

**2 — It replays your context at every session start.**
The next time you open this repo, before you type a single character, Claude already knows your rules. The session start message tells you exactly what was loaded:

```
Contexer: 5 constraints, 4 conventions loaded. 11 arch/patterns will be loaded on demand.
```

Constraints and conventions are injected immediately. Architecture and pattern decisions are fetched silently when the task requires them — not before.

**3 — It answers "why" and "what" questions from stored history.**
Ask *"what is the purpose of this repo?"*, *"what's planned?"*, or *"why did we choose Postgres?"* and Contexer automatically fetches the stored answer. Claude doesn't need to re-read files or ask you again.

---

## Quick start

Install takes under two minutes. See **[docs/install.md](docs/install.md)** for full steps, verification, and uninstall.

**Step 1 — install the package:**

```bash
uv tool install contexer
```

**Step 2 — wire it into Claude Code:**

```bash
sudo contexer install
```

**Step 3 — restart Claude Code** and open any git repo. On your first message, Claude will offer a quick bootstrap setup — answer yes to capture initial decisions, or skip to start immediately.

---

## CLI reference

| Command | Description |
|---|---|
| `sudo contexer install` | Connect Contexer to Claude Code |
| `contexer status` | Show installation health — connection status, store size, current repo |
| `contexer reinstall` | Re-sync connection (use after Claude Code updates) |
| `contexer uninstall` | Disconnect from Claude Code; context store is kept |
| `contexer uninstall --purge` | Remove everything including the context store (`~/.contexer/`) |
| `contexer version` | Print installed version |

---

## How a session looks after Contexer is set up

**Session 1 (first time in a repo):**

Claude offers bootstrap setup. You answer 2–3 questions:
- *"What does this repo do?"* → stored as an architecture decision
- *"Is there a CI pipeline?"* → stored as a convention
- *"Any constraints?"* → stored as a constraint

As you work, Claude stores further decisions automatically.

**Every session after that:**

Before your first message, Claude reads:
```
Contexer: 5 constraints, 4 conventions loaded. 11 arch/patterns will be loaded on demand.
```

Claude already knows:
- "Never commit without tests"
- "Use uv, not pip"
- "Conventional commits format required"
- "We chose REST over GraphQL for the external API"

You skip the re-introduction entirely. Claude starts from where you left off.

---

## Decision types

Every stored decision has a type that controls when it is surfaced.

| Type | What it captures | Surfaced at session start? |
|---|---|---|
| `constraint` | Rules that must always apply — "never commit untested code" | Yes — always |
| `convention` | Team or project standards — "use uv not pip", "conventional commits" | Yes — always |
| `architecture` | Structural decisions — "chose REST over GraphQL for this service" | No — fetched when relevant |
| `pattern` | Recurring implementation approaches — "always validate at the boundary" | No — fetched when relevant |

Constraints and conventions apply to every task, so they load immediately. Architecture and pattern decisions are fetched on demand when the task requires them — this keeps the session start lean regardless of how many decisions you have stored.

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

### Store a global decision

Some conventions apply across every repo — commit style, branch naming, code review process. Store these once and they appear in every session automatically:

```
"store that globally as a convention"
"save this as a global constraint: always use conventional commits"
```

Only `constraint` and `convention` subtypes can be stored globally. Architecture and pattern decisions are always repo-specific.

To see what is stored globally:

```
"show all global conventions"
"show global constraints"
```

### Query decisions

```
"show me all constraints"
"what decisions did we make about postgres?"
"show everything stored for this repo"
```

### Update or remove a decision

```
"update the uv decision — we switched back to pip"
"delete the postgres decision"
"remove all outdated constraints"
```

You can also edit the store file directly — it is plain JSON at `~/.contexer/`.

---

## Performance — no impact on response speed

Contexer does not slow down Claude's response generation. Here is exactly what happens and when:

**All context processing runs before Claude begins generating a response — never during it.** The moment you submit a prompt, Contexer processes context in the background. By the time Claude starts writing, the relevant decisions are already in context. Nothing is added on top of Claude's response time.

**Store lookups are sub-millisecond.** Measured on real data:

| Operation | Time |
|---|---|
| Lookup with a hit (decision found) | 0.3–0.5ms |
| Lookup with a miss (no match) | ~0ms |
| Session start context load | ~1ms |

These are local operations, independent of model. They hold the same whether you have 10 or 500 decisions stored.

**Token cost is front-loaded and fixed.** You pay once at session start; every subsequent prompt in the session is free.

| Pre-loaded rules | Tokens at session start |
|---|---|
| 5 | ~125 |
| 10 | ~250 |
| 25 | ~625 |

Cost per rule is flat at **~26 tokens**, regardless of store size. ~250 tokens is under 0.5% of a typical Claude Code session — less than a one-sentence follow-up question.

**Context injection only fires when relevant.** On prompts that don't relate to stored decisions, Contexer skips silently — no store read, no tokens added.

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
This happens the first time you open a repo. Claude will offer bootstrap setup — answer yes to capture your initial decisions. Once done, all future sessions will have context.

---

## Contributing

Bug reports, fixes, and documentation improvements are welcome. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for development setup, code style, commit format, and the PR process.

Questions or ideas? Join the community on [Discord](https://discord.gg/Fk6JSaW4p).

---

## License

MIT — see [LICENSE](LICENSE) for full terms.

The Contexer name and logo are trademarks of Contexer.ai. The MIT license does not grant rights to use the Contexer name, logo, or brand in any way that implies official affiliation.
