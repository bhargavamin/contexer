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

Contexer is an MCP server for Claude Code that remembers decisions made during a session and replays them at the start of every future session.

---

## The problem

Every Claude Code session starts fresh. No memory of what was decided yesterday, last week, or in a different window.

You end up repeating yourself constantly: *"we use uv not pip"*, *"always write tests before committing"*, *"we chose Postgres because of transactions"*. CLAUDE.md files help, but they need manual upkeep and go stale. The reasoning behind decisions disappears when the session ends.

---

## Install

```bash
uv tool install contexer
contexer install
```

Restart Claude Code and open any git repo. That's it.

See **[docs/install.md](docs/install.md)** for verification, update, and uninstall steps.

---

## How it works

Contexer runs silently in every session. You don't interact with it directly.

**First time in a repo** — Claude includes a brief offer at the top of its first response:

```
Contexer: no project context stored. Save decisions for future sessions? (yes / full / no)
```

Say **yes** for one question (what does this repo do?), **full** for a thorough setup, or **no** to skip. Claude answers your original request either way.

**Every session after that** — before your first message, Claude reads:

```
Contexer: 3 constraints, 2 conventions loaded. 8 arch/patterns will be loaded on demand.
```

Constraints and conventions are injected immediately — they apply to every task. Architecture and pattern decisions load on demand when the task needs them.

As you work, Claude stores decisions automatically. If it misses something, say *"store that as a constraint"* and it's saved.

---

## CLI reference

| Command | Description |
|---|---|
| `contexer install` | Connect Contexer to Claude Code |
| `contexer status` | Show connection status, store size, current repo; warns about corrupt config files and cleans stale temp files |
| `contexer reinstall` | Re-sync after a Claude Code update |
| `contexer uninstall` | Disconnect; context store is kept |
| `contexer uninstall --purge` | Remove everything including `~/.contexer/` |
| `contexer version` | Print installed version |
| `contexer help` | Show all commands and flags |

---

## Decision types

| Type | What it captures | Loaded at session start |
|---|---|---|
| `constraint` | Hard rules — "never commit untested code" | Yes |
| `convention` | Team standards — "use uv not pip", "conventional commits" | Yes |
| `architecture` | Structural choices — "REST over GraphQL for the external API" | On demand |
| `pattern` | Recurring approaches — "validate at the boundary" | On demand |

---

## Storing and querying decisions

```
"store that as a constraint"
"save this as a convention: always use uv not pip"
"what decisions did we make about postgres?"
"show all constraints"
"delete the postgres decision"
"save this as a global constraint: always use conventional commits"
```

Global decisions apply across all repos — use them for commit style, branch naming, anything that travels with you.

> **Note:** Your first prompt each session is also captured as the current task. A first prompt phrased as a clear directive (*"always update docs before committing"*) is still auto-saved as a constraint — but a rule phrased indirectly may slip past the detector, so follow up with *"store that as a constraint"* to be sure.

Edit the store directly if you prefer — it's plain JSON at `~/.contexer/`.

---

## Performance

Context processing runs before Claude generates a response, not during it. It adds nothing to response time.

| Operation | Time |
|---|---|
| Hit (decision found) | 0.3–0.5ms |
| Miss (no match) | ~0ms |
| Session start load | ~1ms |

Token cost is paid once at session start. Every prompt after that is free.

| Pre-loaded rules | Tokens |
|---|---|
| 5 | ~125 |
| 10 | ~250 |
| 25 | ~625 |

~26 tokens per rule, fixed regardless of store size. On prompts unrelated to stored decisions, Contexer skips entirely — no read, no tokens.

---

## Troubleshooting

**Decisions aren't being stored automatically.**
Say *"store that decision"* to save it manually.

**A decision isn't appearing at session start.**
Constraints and conventions load at session start. One added mid-session shows up next time.

**A new decision was silently skipped.**
Content too similar to an existing entry is rejected as a duplicate. Rephrase to include what specifically changed.

**No context at session start on a new repo.**
Claude will offer bootstrap setup. Say yes to capture initial decisions — all future sessions will have context.

---

## Contributing

Bug reports, fixes, and documentation improvements are welcome. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for setup, code style, and the PR process.

Questions? [Discord](https://discord.gg/Fk6JSaW4p).

---

## License

MIT — see [LICENSE](LICENSE) for full terms.

The Contexer name and logo are trademarks of Contexer.ai. The MIT license does not grant rights to use the name or logo in a way that implies official affiliation.
