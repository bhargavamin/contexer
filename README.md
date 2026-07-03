<p align="center">
  <a href="https://contexer.ai">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
      <source media="(prefers-color-scheme: light)" srcset="assets/logo-light.svg">
      <img alt="Contexer" src="assets/logo-light.svg" height="60">
    </picture>
  </a>
</p>

<p align="center">
  <em>The engineering decision layer for AI coding agents.</em>
</p>

<p align="center">
  <a href="https://pypi.org/project/contexer/"><img src="https://img.shields.io/pypi/v/contexer" alt="PyPI version"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.12%2B-blue" alt="Python 3.12+"></a>
  <a href="https://discord.gg/Fk6JSaW4p"><img src="https://img.shields.io/badge/Discord-Join-5865F2?logo=discord&logoColor=white" alt="Discord"></a>
</p>

<p align="center">
  <a href="#the-problem">The problem</a> ·
  <a href="#the-solution">The solution</a> ·
  <a href="#how-is-contexer-different">How is Contexer different?</a> ·
  <a href="#use-cases">Use cases</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#cost">Cost</a> ·
  <a href="docs/install.md">Docs</a> ·
  <a href="https://discord.gg/Fk6JSaW4p">Discord</a>
</p>

---

# Contexer

## The Engineering Decision Layer for AI Coding Agents

Contexer captures architecture decisions, constraints, conventions, and engineering patterns while you work with AI coding assistants.

**One decision layer, every agent.** The same engineering knowledge follows you across **Claude Code, Cursor, Codex, and Gemini CLI** — capture a decision in one, and every other agent already knows it. No single-vendor lock-in.

<!-- TODO: add a 15s asciinema/GIF of the aha moment here (session 1 teaches → session 2 already knows). A demo at the very top converts better than any prose below. -->

### See it in one exchange

**Session 1** — you correct the agent once:

> **You:** Mock at the service boundary, not the DB layer.
> _Contexer stores it as a convention._

**Session 2, next day, fresh context** — before you type a word:

> _Contexer injects:_ `[convention] Mock at the service boundary, not the DB layer.`
> The agent gets it right the first time. The re-explanation tax is gone.

---

## The Problem

AI coding assistants are excellent at writing code. They are not good at remembering **why** engineering decisions were made. Every new AI session starts from zero.

You established "mock at the service boundary, not the DB layer" in session one. Session two, the agent is back to mocking the DB. You correct it. Session three, same thing. Every session pays the re-explanation tax.

Engineering teams make decisions every day: database choices, deployment ownership, security constraints, observability standards, messaging tradeoffs. Most of these are discussed in AI conversations, pull requests, Slack, meetings, and code reviews. They rarely make it into documentation or survive into the next session.

---

## The Solution

Contexer captures important engineering decisions as they happen.

Instead of storing conversations, it stores knowledge like:

**Architecture Decisions**

> "We use PostgreSQL for transactional workloads."

**Constraints**

> "Public S3 buckets are prohibited."

**Conventions**

> "Terraform modules live under `infrastructure/modules`."

**Patterns**

> "Every service must emit OpenTelemetry traces."

Future AI sessions automatically receive the relevant engineering knowledge before generating code.

---

## How is Contexer Different?

Contexer is **not** a replacement for README.md, CLAUDE.md, AGENTS.md, Cursor Memories, or other AI memory systems. Each serves a different purpose.

| Tool | Purpose | Best For |
|---|---|---|
| README.md | Human documentation | Project overview, setup instructions, architecture documentation |
| CLAUDE.md / AGENTS.md | Static AI instructions | Coding guidelines, workflows, commands, project rules |
| Contexer | Living engineering knowledge | Capturing, approving, versioning and replaying engineering decisions across AI coding sessions |

### Engineering decisions, not conversations

| | AI Memory | Contexer |
|---|---|---|
| **What it stores** | Conversations | Engineering decisions |
| **Scope** | Personal | Project decisions (org-wide rules in Contexer Teams) |
| **Persistence** | Session history | Cross-session |
| **History** | n/a | Versioned - full history preserved, latest approved replayed |
| **Focus** | Chat focused | Human approved |
| **Awareness** | n/a | Architecture aware |
| **Reach** | Single tool | Works across repositories |
| **Agents** | One assistant | Shared across AI coding agents |
| **Governance** | No governance | Human-approved decisions |

> AI assistants remember conversations. Contexer remembers what your engineering organization decided.

> [!NOTE]
> **Contexer doesn't replace your documentation. It complements it.**
>
> README.md, CLAUDE.md and AGENTS.md remain the canonical documentation and instruction files. Contexer continuously captures engineering knowledge that can later be promoted into permanent documentation, Architecture Decision Records (ADRs), README.md or CLAUDE.md after review.

---

## Use Cases

### Never Explain Your Architecture Twice

Your AI already knows your architecture decisions.

### Keep Every AI Coding Assistant Consistent

Claude, Cursor, Codex and Gemini all start with the same engineering knowledge.

### Build Shared Team Knowledge

Capture engineering decisions once. Reuse them across the entire team.

### Onboard Engineers Faster

New developers instantly understand historical architecture decisions.

### Preserve Tribal Knowledge

Engineering decisions no longer disappear inside AI conversations.

---

## Quick start

Requires **Python 3.12+** and **[uv](https://docs.astral.sh/uv/getting-started/installation/)**. Under two minutes.

```bash
# Step 1: install
uv tool install contexer

# Step 2: wire into your AI assistant
contexer install
```

`contexer install` auto-detects which tools are present (`~/.claude` → Claude Code, `~/.cursor` → Cursor, `~/.codex` → Codex, `~/.gemini` → Gemini CLI) and wires all of them. Pass `--target claude`, `--target cursor`, `--target codex`, `--target gemini`, or `--target all` to override.

Restart your AI assistant and open any git repo. Contexer runs silently from here.

See **[docs/install.md](docs/install.md)** for verification, update, and uninstall steps.

---

## How it works

```
1. Developer works with AI
        ↓
2. Contexer detects engineering decisions
        ↓
3. Bootstrap asks authoritative questions when needed
        ↓
4. Approved decisions are stored locally
        ↓
5. Future AI sessions automatically replay relevant engineering knowledge
```

### What gets captured

Contexer continuously captures engineering knowledge as you work:

- **Architecture decisions**: structural choices that shape the system
- **Constraints**: rules that must always apply
- **Conventions**: team or project standards
- **Patterns**: recurring implementation approaches

| Type | What it captures | Loaded at session start |
|---|---|---|
| `constraint` | Rules that must always apply ("never merge untested code") | Yes, always |
| `convention` | Team or project standards ("use uv not pip", "conventional commits") | Yes, always |
| `architecture` | Structural decisions ("chose REST over GraphQL") | No, fetched on demand |
| `pattern` | Recurring implementation approaches | No, fetched on demand |

Constraints and conventions load every session because they apply to every task. Architecture and pattern decisions are fetched on demand when you ask about rationale, design, or past decisions.

Capture is two-track, and you stay in control of both:

- Directives you state outright ("always X", "never Y", "don't Z", "create a rule…") are auto-stored *deterministically* by a hook — no model guesswork, no decision stored behind your back.
- Everything else relies on the agent noticing a decision and calling the store tool. That is best-effort by design; when the agent misses one, say *"store that decision"* and it's captured immediately.

### Bootstrap: establishing trusted knowledge

When Contexer is used on a repository for the first time, it analyzes the project and proposes a small set of engineering questions.

Examples:

- Should infrastructure always be deployed with Terraform?
- Is PostgreSQL the standard database?
- Are deployments multi-cloud?

Developers confirm or refine the answers. These approved decisions become the initial engineering knowledge for the repository.

The offer adapts to how well you know the repo, judged from its git history:

- The repo has commits from you → pick **quick** (one question) or **full** (guided setup).
- No commits from your git email (e.g. a freshly cloned project) → Contexer suggests **scan**: it reads the code and docs to propose decisions instead of asking questions you can't answer.
- Can't tell → it simply asks how well you know the repo.

**Resumed sessions** (Claude Code's `--resume` / `--continue`) don't repeat any of this. The context is already in the conversation. If you installed Contexer mid-project, resuming an old session makes the agent mine that conversation for decisions already made and store them, no questions asked.

### At session start

At session start, a hook injects your stored constraints and conventions before you type anything. The injected block looks roughly like this:

```
## Project rules - apply to ALL tasks in this repo:
- [convention] Use uv, not pip, for all dependency management
- [constraint] Never commit untested code - CI blocks merges below coverage
2 architecture decision(s) stored. Call get_context before reading files
for questions about architecture, design, or rationale.
```

Ask about a past decision or rationale ("why did we pick REST?") and Contexer fetches the matching entries automatically, before the agent responds.

### Under the hood

Contexer is wired in through two mechanisms: **MCP tools** the agent can call (to store and fetch decisions) and **editor hooks** the host runs around your session (to inject context and capture directives). You work normally; most of it is invisible.

**Deduplication is not an LLM call.** Before storing, Contexer checks token overlap against existing decisions. Over 70% overlap is treated as a duplicate and silently dropped. It's deterministic, costs no tokens, and is why you can "over-call" store without bloating anything.

Everything lives as plain JSON in `~/.contexer/` on your own machine. Nothing about your code or decisions leaves your machine. The only network call Contexer makes is an optional version check against PyPI, which you can turn off.

---

## Cost

Contexer's cost is fixed and predictable: roughly **26 tokens per rule** at session start, paid only for constraints and conventions. Architecture and pattern decisions cost nothing until something actually needs them.

| Pre-loaded rules | Approx. tokens at session start |
|---|---|
| 5 | ~125 |
| 10 | ~250 |
| 25 | ~625 |

Paid once per session. Every later prompt adds nothing. On prompts unrelated to anything stored, Contexer skips entirely: no read, no tokens. Store lookups are sub-millisecond and run before the response is generated, so they add nothing to response time.

The point isn't token compression. It's **eliminated rework across sessions**. The recurring, unpredictable cost of re-explaining rules and correcting re-introduced patterns is replaced by a small, flat, session-start cost.

Shorter sessions. Lower cost. No lost engineering knowledge.

---

## Managing decisions

Everything uses natural language.

### Store a decision

```
"store that as a constraint"
"save this as a convention: always use uv not pip"
"remember this architecture decision"
```

Global decisions apply across all repos. Use them for commit style, branch naming, or anything that travels with you:

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

### Review pending decisions

AI-proposed architecture and constraint decisions - and any change to a decision you have already approved - are held for your review instead of being trusted automatically. They are stored, but not replayed into AI sessions until you approve them. Review them whenever you like:

```bash
contexer review
```

For each one you can **approve**, **edit**, **skip** (decide later), or **dismiss**. At session start Contexer reminds you, without blocking, when items are waiting.

Approved decisions are versioned: a change never overwrites the previous value - it creates a new revision and the full history is preserved. AI sessions always replay the latest approved revision.

### Update or remove

```
"update the uv decision - we switched back to pip"
"delete the postgres decision"
"remove all outdated constraints"
```

The store is plain JSON at `~/.contexer/`. Edit it directly if you prefer.

---

## Use with Cursor (1.7+)

```bash
contexer install --target cursor   # or: contexer install (auto-detects ~/.cursor)
```

This registers Contexer's MCP server in `~/.cursor/mcp.json` and wires two Cursor hook events in `~/.cursor/hooks.json`:

- `sessionStart`: injects your stored project rules and a usage nudge, and drops a managed always-apply rule at `<repo>/.cursor/rules/contexer.mdc`.
- `beforeSubmitPrompt`: silently captures your task and any "always / never / don't / create a rule" directives.

The managed rule file (marker-guarded, so your own rules are never touched) steers the agent to call Contexer's `get_context` before reading files for architecture/"why" questions, and to save rules via `update_context` rather than writing native `.cursor/rules` files.

The first time Cursor calls a Contexer tool it asks you to approve it. Contexer does not pre-approve its own MCP tools for you.

**Parity note:** Cursor's `beforeSubmitPrompt` hook cannot inject context (only allow/block) and Cursor exposes no usable compaction hook. So Contexer's per-prompt steering on Cursor rides on the session-start nudge plus the always-apply rule file, rather than Claude's per-prompt hooks. The core value (automatic session-start injection of your stored rules) works identically to Claude Code.

---

## Use with Codex

```bash
contexer install --target codex   # or: contexer install (auto-detects ~/.codex)
```

This registers Contexer's MCP server in `~/.codex/config.toml` (under `[mcp_servers.contexer]`) and wires hooks in `~/.codex/hooks.json`. The `config.toml` edit is surgical: only the contexer stanza is added or removed, so your existing servers, plugins, projects, and secrets are left untouched.

Codex's hooks use the same events as Claude Code (`SessionStart`, `PostToolUse`, `PreCompact`, `PostCompact`, `UserPromptSubmit`), so Contexer runs at **full Claude parity** there: automatic session-start injection, per-prompt rationale and constraint capture, post-edit reminders, and context reload after compaction all work.

The first time Codex calls a Contexer tool it asks you to approve it. Contexer does not pre-approve its own MCP tools for you.

---

## Use with Gemini CLI

```bash
contexer install --target gemini   # or: contexer install (auto-detects ~/.gemini)
```

This adds Contexer's MCP server and managed hooks to `~/.gemini/settings.json`, preserving all existing settings, MCP servers, and user hooks. Gemini CLI will ask you to trust the new hooks after installation.

The adapter uses Gemini's native `SessionStart`, `BeforeAgent`, `AfterTool`, `PreCompress`, and `SessionEnd` events. Session rules, first-prompt task capture, deterministic constraint capture, rationale lookup, and post-edit reminders are supported. The `AfterTool` hook matches Gemini's `write_file` and `replace` tools.

**Parity note:** Gemini's `PreCompress` hook is asynchronous and advisory, and Gemini has no `PostCompress` event. Contexer therefore flags the compression and re-injects full context at the next `BeforeAgent` event. This restores context on the next turn, but cannot force Gemini to save an unsaved decision immediately before compression.

---

## Why it stays lightweight

Contexer is a single Python MCP server with a plain JSON store. No background worker. No vector database. No port listening. No infrastructure to maintain.

This is intentional. Every piece of complexity added to a decision store is a piece of complexity that can fail, drift, or accumulate noise. Contexer stores only what matters (engineering decisions) and keeps everything inspectable, auditable, and greppable.

---

## Contexer Teams (early access)

The open-source Contexer is per-developer. **Contexer Teams** makes your team's engineering and security standards **present in every AI agent** — the same engine, one shared, governed source of truth across the org.

- **Publish once, present everywhere.** Your security or platform team sets a rule — *"no plaintext secrets in code"*, *"all PII encrypted at rest"* — and every developer's AI agent starts with it, in every repo and every tool, *before the code is written*.
- **Standards travel to the point of work.** A team's conventions reach any developer the moment they open that repo in their agent — not buried in a wiki no one reads.
- **Governed and auditable.** Rules are centrally owned, lead-approved, and versioned — who decided what, when, and why. The trail compliance needs and onboarding lives on.

A few people publish the rules; everyone else's agent just consumes them — no team-wide capture discipline required.

> [!NOTE]
> **Contexer steers, it doesn't enforce — and that distinction matters.**
> It reliably *tells* every agent your standards at the moment it writes code — the leftmost shift, fewer violations at the source. It does **not** scan code, block commits, or guarantee compliance; your CI, secret scanners, and PR gates still verify and enforce. Contexer makes agents *aware*; it complements your gates, it doesn't replace them.

→ **[Request early access at contexer.ai](https://contexer.ai)** — design-partner teams.

---

## Limitations

- **Personal, not team.** The open-source store is per-user, per-machine — shared rules don't propagate between developers. Team sync, org-wide rules, and governance live in **Contexer Teams** (early access — see above).
- **Cursor parity is partial.** Cursor's `beforeSubmitPrompt` hook can't inject context (only allow/block) and it has no usable compaction hook, so per-prompt rationale injection and compaction save/restore are unavailable there. Cursor steering rides on the session-start nudge plus an always-apply rule file.
- **Gemini compression is deferred.** Gemini CLI has `PreCompress` but no `PostCompress`; Contexer re-injects stored context at the next prompt rather than immediately after compression.
- **Capture is best-effort.** Only outright directives ("always/never/don't/create a rule") are auto-stored deterministically. Other decisions depend on the agent choosing to call the store tool, and it does miss things. Hence the *"store that decision"* escape hatch.
- **Soft storage cap.** Up to 500 entries per repo; beyond that, the least-reinforced decisions are evicted. There's no automatic staleness pruning. Outdated decisions stay until you remove them.
- **One network call.** `contexer status` checks PyPI for a newer version. Disable with `CONTEXER_NO_UPDATE_CHECK=1`. Nothing else leaves your machine.

---

## CLI reference

| Command | Description |
|---|---|
| `contexer install` | Connect Contexer (auto-detects Claude Code, Cursor, Codex, and/or Gemini CLI) |
| `contexer install --target claude\|cursor\|codex\|gemini\|all` | Install for a specific tool only, or all |
| `contexer review` | Review decisions awaiting approval: approve, edit, skip, or dismiss each |
| `contexer status` | Show connection status, store size, current repo; warns about corrupt config files, cleans stale temp files, and notifies when a newer version is on PyPI |
| `contexer reinstall` | Re-sync after an AI assistant update |
| `contexer uninstall` | Disconnect; context store is kept |
| `contexer uninstall --purge` | Remove everything including `~/.contexer/` |
| `contexer version` | Print installed version |
| `contexer help` | Show all commands and flags |

---

## Troubleshooting

**The agent isn't storing decisions automatically.** Say *"store that decision"* and it is captured immediately.

**A decision was stored but isn't appearing.** Constraints and conventions load at session start. If added mid-session, they appear from the next session onward.

**A decision is outdated or wrong.** Say *"delete the X decision"* or edit the store file directly at `~/.contexer/`.

**A new decision wasn't saved. Looks like a duplicate.** Content too similar to an existing decision is silently skipped. Rephrase to include what specifically changed.

**No context appeared at session start on a new repo.** The agent will offer bootstrap setup. Complete it once and all future sessions will have context.

---

## Star history

If Contexer saves you re-explanation time, a star helps others find it. It takes one second.

[![Star History Chart](https://api.star-history.com/svg?repos=bhargavamin/contexer&type=Date)](https://star-history.com/#bhargavamin/contexer&Date)

---

## Contributing

Bug reports, fixes, and documentation improvements are welcome. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for setup, code style, and the PR process.

Questions or ideas? Join the community on [Discord](https://discord.gg/Fk6JSaW4p).

---

## License

MIT. See [LICENSE](LICENSE) for full terms.

The Contexer name and logo are trademarks of Contexer.ai. The MIT license does not grant rights to use the Contexer name, logo, or brand in any way that implies official affiliation.

---

Contexer is **not a chat memory tool.** It is **the engineering decision layer for AI coding agents**, capturing architecture decisions, constraints, conventions, and patterns so engineering knowledge becomes a shared organizational asset instead of disappearing inside AI conversations.
