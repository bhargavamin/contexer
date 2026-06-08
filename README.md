<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
  <source media="(prefers-color-scheme: light)" srcset="assets/logo-light.svg">
  <img alt="Contexer" src="assets/logo-dark.svg" height="60">
</picture>

[![PyPI version](https://img.shields.io/pypi/v/contexer)](https://pypi.org/project/contexer/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![Discord](https://img.shields.io/discord/1513284860821639328?label=Discord&logo=discord&logoColor=white)](https://discord.gg/Fk6JSaW4p)

**[contexer.ai](https://contexer.ai)** · [Discord](https://discord.gg/Fk6JSaW4p) · [Docs](docs/install.md)

Contexer is a lightweight MCP server for Claude Code that automatically captures decisions made during coding sessions and surfaces them at the start of every future session — so Claude never starts blind.

## The problem

Every Claude Code session starts with no memory of the previous one. CLAUDE.md files require manual maintenance and go stale. When Claude works autonomously, the reasoning behind decisions disappears when the session ends. Teams end up re-explaining the same constraints, conventions, and architecture choices every time.

Contexer solves this by capturing decisions as they happen — silently, automatically, in the background — and replaying them as project rules at session start.

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
| `sudo contexer install` | Register MCP server and all hooks with Claude Code |
| `contexer status` | Show installation health — MCP registration, hooks, store size, current repo |
| `contexer reinstall` | Re-sync config (uninstall + install — use after Claude Code updates) |
| `contexer uninstall` | Remove MCP registration and hooks; context store is kept |
| `contexer uninstall --purge` | Remove everything including the context store (`~/.contexer/`) |
| `contexer version` | Print installed version |

---

## How it works

You work normally. Contexer runs silently in the background.

- **Session start** — all your constraints and conventions are injected as project rules before you type anything
- **As you work** — Claude captures significant decisions automatically; you never have to do it manually
- **"Why" and project questions** — if you ask about rationale, architecture, approach, purpose, or plans, Contexer auto-fetches the relevant stored decisions
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

## Token cost

Contexer is front-loaded by design. Cost is paid once at session start; everything after is near-zero.

**What gets injected at session start:**

Only `constraint` and `convention` decisions are pre-loaded. `architecture` and `pattern` decisions cost **0 tokens** at session start — they are fetched on demand when relevant.

| Decisions stored | Pre-loaded rules | Tokens at session start |
|---|---|---|
| 10 | 5 | ~125 |
| 20 | 10 | ~250 |
| 50 | 25 | ~625 |

Cost is flat at **~25 tokens per pre-loaded rule**, regardless of store size.

**What that compares to:**

~250 tokens (20 decisions) is 0.3–2.5% of a typical Claude Code session. Without Contexer, Claude spends 200–500 tokens re-establishing each rule through back-and-forth — every session. A single corrected mistake costs 500–2,000 tokens. The tool pays for itself after one avoided repetition.

**Rationale injection — only fires on "why" questions:**

| Event | Tokens added | Latency |
|---|---|---|
| Hit — relevant decision found | ~45–80 | ~0.06ms store lookup |
| Miss — no rationale keyword | 0 | 0.000ms |

**Latency:** Contexer's store operations take 0.03–0.27ms. The perceived delay is the MCP hook call round-trip (~50–200ms), which runs before Claude responds — not added on top of it. Sessions with 500 stored decisions have the same sub-millisecond retrieval as sessions with 10.

> These numbers were measured on Claude Sonnet 4.6. Haiku will process injected tokens faster; Opus will take slightly longer but produce higher-quality responses with the same context. The store lookup times are model-independent — they are pure Python/disk operations.

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
