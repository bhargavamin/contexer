<a href="https://contexer.ai">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
    <source media="(prefers-color-scheme: light)" srcset="assets/logo-horizontal-light.svg">
    <img alt="Contexer" src="assets/logo-horizontal-light.svg" height="60">
  </picture>
</a>

[![PyPI version](https://img.shields.io/pypi/v/contexer)](https://pypi.org/project/contexer/)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12%2B-blue)](https://www.python.org/)
[![Discord](https://img.shields.io/badge/Discord-Join-5865F2?logo=discord&logoColor=white)](https://discord.gg/Fk6JSaW4p)

**[contexer.ai](https://contexer.ai)** · [Discord](https://discord.gg/Fk6JSaW4p) · [Docs](docs/install.md)

---

Every Claude Code session starts fresh. No memory of what was decided last week. No knowledge of the constraints your team spent months establishing. No recollection of the architecture choices that took three PRs to get right.

The result: developers re-explain the same rules every session. Claude re-introduces patterns already rejected. Work gets redone. Sessions run long. Budgets overrun.

**Contexer fixes this by capturing decisions as they happen and replaying them before Claude types a single character in your next session.**

---

## What changes

**Before Contexer:** You establish "mock at the service boundary, not the DB layer" in session one. Session two, Claude is back to mocking the DB. You correct it. Session three, same thing. Every session pays the re-explanation tax, and every mistake Claude makes because it forgot costs correction turns that run sessions long.

**After Contexer:** That rule is stored once as a constraint. Every future session starts with it already injected. Claude never forgets it. You never say it again.

The impact compounds across a team. Shared constraints mean every engineer's agent follows the same rules, enforces the same quality standards, and stays within the same architectural boundaries — without anyone managing it manually.

---

## vs. `CLAUDE.md`, `AGENTS.md`, `.cursorrules`

`CLAUDE.md`, `AGENTS.md`, and `.cursorrules` are worth having. They work well for stable project context — setup notes, architecture overviews, things you know before you start working.

The gap they don't cover is decisions made during development.

You can't write a `CLAUDE.md` entry for a constraint you haven't established yet. The rules that matter most — the ones that emerge from real work, real mistakes, real conversations — get established mid-session and disappear when it ends. `CLAUDE.md` captures what you remember to write down. Contexer captures what actually happened.

| | `CLAUDE.md` / `AGENTS.md` / `.cursorrules` | Contexer |
|---|---|---|
| **Source** | Written manually, when you think of it | Captured automatically as decisions are made |
| **Freshness** | As current as the last time someone edited the file | Updated continuously; novelty filter prevents drift |
| **Token cost** | Entire file on every prompt | Only constraints/conventions at session start; architecture fetched when relevant |
| **New repo** | Blank — you start from scratch | Bootstrap scans git history and code to infer initial decisions |
| **Scope** | One file, one repo | Per-repo decisions + global rules that follow you across every repo |

Today Contexer is a personal decision store — private by default, per-user, per-machine. Team sharing is next.

**Use both.** `CLAUDE.md` is the right place for onboarding context and stable project notes. Contexer is where the decisions made *during development* live — automatically, without discipline to maintain a file.

---

## Quick start

Install takes under two minutes.

```bash
# Step 1 — install
uv tool install contexer

# Step 2 — wire into Claude Code
contexer install
```

Step 3 — restart Claude Code and open any git repo. Contexer runs silently from here.

See **[docs/install.md](docs/install.md)** for verification, update, and uninstall steps.

---

## How it works

You work normally. Contexer captures decisions in the background.

- **Session start** — constraints and conventions inject automatically before you type anything
- **As you work** — Claude captures significant decisions; no manual intervention needed
- **"Why" questions** — if you ask about rationale or past decisions, Contexer fetches them automatically
- **Context window limit** — decisions are saved before compaction and restored after, so nothing is lost

If Claude misses something, say *"store that decision"* and it is captured immediately.

**First time in a repo** — Claude includes a brief offer at the top of its first response. The offer adapts to how well you know the repo, judged from its git history:

- The repo has commits from you → pick **quick** (one question) or **full** (guided setup).
- No commits from your git email (e.g. a freshly cloned project) → Contexer suggests **scan**: it reads the code and docs instead of asking questions you can't answer.
- Can't tell → it simply asks how well you know the repo.

**Resumed sessions** (`--resume` / `--continue`) don't repeat any of this — the context is already in the conversation. If you installed Contexer mid-project, resuming an old session makes Claude mine that conversation for decisions already made and store them, no questions asked.

---

## Decision types

Not all context is equal. Contexer distinguishes between what must always apply and what is only relevant sometimes — and only loads what the current task actually needs.

| Type | What it captures | Loaded at session start |
|---|---|---|
| `constraint` | Rules that must always apply — "never merge untested code" | Yes — always |
| `convention` | Team or project standards — "use uv not pip", "conventional commits" | Yes — always |
| `architecture` | Structural decisions — "chose REST over GraphQL" | No — fetched when relevant |
| `pattern` | Recurring implementation approaches | No — fetched when relevant |

Constraints and conventions load every session because they apply to every task. Architecture and pattern decisions cost zero tokens at session start — they are retrieved only when the work requires them.

---

## Why this saves money

Contexer has a fixed, predictable cost: ~26 tokens per rule at every session start, paid only for constraints and conventions. Architecture and pattern decisions cost nothing at session start.

The saving comes from what that replaces.

| Without Contexer | With Contexer |
|---|---|
| 200–500 tokens re-explaining rules per session through back-and-forth | ~26 tokens per rule at session start, flat and predictable |
| Claude re-introduces rejected patterns, correction turns follow | Pattern stored once, correction never needed |
| Developer reworks output that didn't follow established decisions | Decisions enforced from session start |
| Long sessions from accumulated re-explanation and mistakes | Sessions stay focused, context stays clean |

```
10 constraints/conventions stored  →  ~260 tokens at every session start
20 constraints/conventions stored  →  ~520 tokens at every session start
```

The trade: unpredictable, recurring re-explanation cost replaced by a small, flat, predictable session-start cost. A single avoided correction turn saves more than a full week of that overhead.

**The ROI is in eliminated rework across sessions, not token compression within one.**

---

## Managing decisions

Everything uses natural language.

### Store a decision

```
"store that as a constraint"
"save this as a convention: always use uv not pip"
"remember this architecture decision"
```

Global decisions apply across all repos — use them for commit style, branch naming, or anything that travels with you:

```
"store that globally as a convention"
"save this as a global constraint: always use conventional commits"
```

Only `constraint` and `convention` types can be stored globally. Architecture and pattern decisions are always repo-specific.

### Query decisions

```
"show me all constraints"
"what decisions did we make about postgres?"
"show everything stored for this repo"
```

### Update or remove

```
"update the uv decision — we switched back to pip"
"delete the postgres decision"
"remove all outdated constraints"
```

The store is plain JSON at `~/.contexer/` — edit it directly if you prefer.

---

## Why it stays lightweight

Contexer is a single Python MCP server with a plain JSON store. No background worker. No vector database. No port listening. No infrastructure to maintain.

This is intentional. Every piece of complexity added to a memory system is a piece of complexity that can fail, drift, or accumulate noise. Contexer stores only what matters — decisions — and keeps everything inspectable, auditable, and greppable.

---

## Token cost reference

Context processing runs before Claude generates a response, not during it. It adds nothing to response time.

**Session start — pre-loaded rules (constraints + conventions):**

| Pre-loaded rules | Tokens at session start |
|---|---|
| 5 | ~125 |
| 10 | ~250 |
| 25 | ~625 |

~26 tokens per rule, fixed regardless of total store size. Architecture and pattern decisions cost 0 tokens at session start — fetched on demand only.

Token cost is paid once at session start. Every subsequent prompt within that session adds nothing — the rules are already in context.

**Retrieval latency:**

| Operation | Time |
|---|---|
| Hit (decision found) | 0.3–0.5ms |
| Miss (no match) | ~0ms |
| Session start load | ~1ms |

On prompts unrelated to stored decisions, Contexer skips entirely — no read, no tokens.

---

## CLI reference

| Command | Description |
|---|---|
| `contexer install` | Connect Contexer to Claude Code |
| `contexer status` | Show connection status, store size, current repo; warns about corrupt config files, cleans stale temp files, and notifies when a newer version is on PyPI |
| `contexer reinstall` | Re-sync after a Claude Code update |
| `contexer uninstall` | Disconnect; context store is kept |
| `contexer uninstall --purge` | Remove everything including `~/.contexer/` |
| `contexer version` | Print installed version |
| `contexer help` | Show all commands and flags |

---

## Troubleshooting

**Claude isn't storing decisions automatically.** Say *"store that decision"* and it is captured immediately.

**A decision was stored but isn't appearing.** Constraints and conventions load at session start. If added mid-session, they appear from the next session onward.

**A decision is outdated or wrong.** Say *"delete the X decision"* or edit the store file directly at `~/.contexer/`.

**A new decision wasn't saved — looks like a duplicate.** Content too similar to an existing decision is silently skipped. Rephrase to include what specifically changed.

**No context appeared at session start on a new repo.** Claude will offer bootstrap setup. Complete it once and all future sessions will have context.

---

## Contributing

Bug reports, fixes, and documentation improvements are welcome. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for setup, code style, and the PR process.

Questions or ideas? Join the community on [Discord](https://discord.gg/Fk6JSaW4p).

---

## License

MIT — see [LICENSE](LICENSE) for full terms.

The Contexer name and logo are trademarks of Contexer.ai. The MIT license does not grant rights to use the Contexer name, logo, or brand in any way that implies official affiliation.
