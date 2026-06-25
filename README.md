<p align="center">
  <a href="https://contexer.ai">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="assets/logo-dark.svg">
      <source media="(prefers-color-scheme: light)" srcset="assets/logo-horizontal-light.svg">
      <img alt="Contexer" src="assets/logo-horizontal-light.svg" height="60">
    </picture>
  </a>
</p>

<p align="center">
  <em>Your sessions are disposable. Your decisions aren't.</em>
</p>

<p align="center">
  <a href="https://pypi.org/project/contexer/"><img src="https://img.shields.io/pypi/v/contexer" alt="PyPI version"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.12%2B-blue" alt="Python 3.12+"></a>
  <a href="https://discord.gg/Fk6JSaW4p"><img src="https://img.shields.io/badge/Discord-Join-5865F2?logo=discord&logoColor=white" alt="Discord"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#the-problem">The problem</a> ·
  <a href="#what-changes">What changes</a> ·
  <a href="#vs-claudemd-agentsmd-cursorrules">vs. CLAUDE.md</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#cost">Cost</a> ·
  <a href="docs/install.md">Docs</a> ·
  <a href="https://discord.gg/Fk6JSaW4p">Discord</a>
</p>

---

Every session you've ever had made a decision.
Contexer kept it.
Your next session starts with all of them.

**Close the session. Keep the decisions.**

---

## The problem

A session with 40 exchanges, six file reads, and three architecture decisions costs thousands of tokens to maintain.
You keep it running anyway — because starting fresh means re-explaining the stack, the constraints, and the patterns the agent already broke once.

You established "mock at the service boundary, not the DB layer" in session one.
Session two, the agent is back to mocking the DB.
You correct it.
Session three, same thing.

Every session pays the re-explanation tax.
Every mistake the agent makes because it forgot costs correction turns.
Sessions run long. Budgets overrun. And still, nothing is ever retained.

**Contexer stores your decisions as they happen and replays them at the start of your next session.**
The agent never forgets. You never say it again.

Works with Claude Code, Cursor, Codex, and Gemini CLI.

---

## Quick start

Requires **Python 3.12+** and **[uv](https://docs.astral.sh/uv/getting-started/installation/)**. Under two minutes.

```bash
# Step 1 — install
uv tool install contexer

# Step 2 — wire into your AI assistant
contexer install
```

`contexer install` auto-detects which tools are present (`~/.claude` → Claude Code, `~/.cursor` → Cursor, `~/.codex` → Codex, `~/.gemini` → Gemini CLI) and wires all of them. Pass `--target claude`, `--target cursor`, `--target codex`, `--target gemini`, or `--target all` to override.

Restart your AI assistant and open any git repo. Contexer runs silently from here.

See **[docs/install.md](docs/install.md)** for verification, update, and uninstall steps.

---

## What changes

<table>
<tr>
<td width="50%">

### Without Contexer

You say "always use uv, not pip" in Monday's session.

Tuesday, the agent writes a `pip install` command. You correct it.

Wednesday, same thing.

You are not the bottleneck — the session boundary is.

</td>
<td width="50%">

### With Contexer

That rule is stored once.

Every future session opens with it already injected. The agent follows it without being reminded.

You move faster. You never say it again.

</td>
</tr>
<tr>
<td>

You established "mock at the service boundary, not the DB layer."

Next session: the agent mocks the DB. You correct it.

Session after: same thing. Every session pays the re-explanation tax.

</td>
<td>

Stored once as a constraint. Injected at the start of every future session.

The agent never forgets it.

</td>
</tr>
</table>

The impact compounds across a team. Shared constraints mean every engineer's agent follows the same rules, enforces the same quality standards, and stays within the same architectural limits — without anyone maintaining a file to make it happen.

---

## vs. `CLAUDE.md`, `AGENTS.md`, `.cursorrules`

These files are worth having — they work well for stable project context: setup notes, architecture overviews, things you know before you start.

The gap they don't cover is decisions made *during* development.

You can't write a `CLAUDE.md` entry for a constraint you haven't established yet.
The rules that matter most — the ones that emerge from real work, real mistakes, real conversations — get established mid-session and disappear when it ends.
`CLAUDE.md` captures what you remember to write down. Contexer captures what actually happened.

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

## How it works

Contexer is wired in through two mechanisms: **MCP tools** the agent can call (to store and fetch decisions) and **editor hooks** the host runs around your session (to inject context and capture directives). You work normally; most of it is invisible.

- **Session start** — a hook injects your stored constraints and conventions before you type anything.
- **As you work** — capture is two-track. Directives you state outright ("always X", "never Y", "don't Z", "create a rule…") are auto-stored *deterministically* by a hook. Everything else relies on the agent noticing a decision and calling the store tool — best-effort, and it does miss things. When it does, say *"store that decision"* and it's captured immediately.
- **"Why" questions** — ask about a past decision or rationale and Contexer fetches the matching entries automatically.
- **Context window limit** — Claude Code and Codex save before compaction and restore afterward. Gemini CLI restores stored context on the next turn because its compression hook is advisory. Cursor exposes no usable compaction hook.
- **Claude Code memory tool** — if a session records decisions to Claude Code's built-in memory (`~/.claude/projects/.../memory/`) instead of to Contexer, those facts are imported automatically — at session start, around compaction, and on exit — so they're categorised and available to the next session. Two memory systems, one source of truth.

**Deduplication is not an LLM call.** Before storing, Contexer checks token overlap against existing decisions — >70% overlap is treated as a duplicate and silently dropped. It's deterministic, costs no tokens, and is why you can "over-call" store without bloating anything.

At session start, the injected block looks roughly like this:

```
## Project rules — apply to ALL tasks in this repo:
- [convention] Use uv, not pip, for all dependency management
- [constraint] Never commit untested code — CI blocks merges below coverage
2 architecture decision(s) stored. Call get_context before reading files
for questions about architecture, design, or rationale.
```

**First time in a repo** — the agent includes a brief offer at the top of its first response. The offer adapts to how well you know the repo, judged from its git history:

- The repo has commits from you → pick **quick** (one question) or **full** (guided setup).
- No commits from your git email (e.g. a freshly cloned project) → Contexer suggests **scan**: it reads the code and docs instead of asking questions you can't answer.
- Can't tell → it simply asks how well you know the repo.

**Resumed sessions** (Claude Code's `--resume` / `--continue`) don't repeat any of this — the context is already in the conversation. If you installed Contexer mid-project, resuming an old session makes the agent mine that conversation for decisions already made and store them, no questions asked.

---

## What Contexer stores

**Local. Private. Yours.** Everything lives as plain JSON in `~/.contexer/` on your own machine. Nothing about your code or decisions leaves your machine — the only network call Contexer makes is an optional version check against PyPI, which you can turn off.

Not all context is equal. Contexer distinguishes between what must always apply and what is only relevant sometimes — and only loads what the current task actually needs.

| Type | What it captures | Loaded at session start |
|---|---|---|
| `constraint` | Rules that must always apply — "never merge untested code" | Yes — always |
| `convention` | Team or project standards — "use uv not pip", "conventional commits" | Yes — always |
| `architecture` | Structural decisions — "chose REST over GraphQL" | No — fetched when relevant |
| `pattern` | Recurring implementation approaches | No — fetched when relevant |

Constraints and conventions load every session because they apply to every task. Architecture and pattern decisions cost zero tokens at session start — they are retrieved only when the work requires them.

---

## Cost

Contexer's cost is fixed and predictable: roughly **26 tokens per rule** at session start, paid only for constraints and conventions. Architecture and pattern decisions cost nothing until something actually needs them.

| Pre-loaded rules | Approx. tokens at session start |
|---|---|
| 5 | ~125 |
| 10 | ~250 |
| 25 | ~625 |

Paid once per session. Every later prompt adds nothing. On prompts unrelated to anything stored, Contexer skips entirely: no read, no tokens. Store lookups are sub-millisecond and run before the response is generated, so they add nothing to response time.

The point isn't token compression — it's **eliminated rework across sessions**. The recurring, unpredictable cost of re-explaining rules and correcting re-introduced patterns is replaced by a small, flat, session-start cost.

Shorter sessions. Lower cost. No lost context.

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

## Use with Cursor (1.7+)

```bash
contexer install --target cursor   # or: contexer install (auto-detects ~/.cursor)
```

This registers Contexer's MCP server in `~/.cursor/mcp.json` and wires two Cursor hook events in `~/.cursor/hooks.json`:

- `sessionStart` — injects your stored project rules and a usage nudge, and drops a managed always-apply rule at `<repo>/.cursor/rules/contexer.mdc`.
- `beforeSubmitPrompt` — silently captures your task and any "always / never / don't / create a rule" directives.

The managed rule file (marker-guarded, so your own rules are never touched) steers the agent to call Contexer's `get_context` before reading files for architecture/"why" questions, and to save rules via `update_context` rather than writing native `.cursor/rules` files.

The first time Cursor calls a Contexer tool it asks you to approve it — Contexer does not pre-approve its own MCP tools for you.

**Parity note:** Cursor's `beforeSubmitPrompt` hook cannot inject context (only allow/block) and Cursor exposes no usable compaction hook. So Contexer's per-prompt steering on Cursor rides on the session-start nudge plus the always-apply rule file, rather than Claude's per-prompt hooks. The core value — automatic session-start injection of your stored rules — works identically to Claude Code.

---

## Use with Codex

```bash
contexer install --target codex   # or: contexer install (auto-detects ~/.codex)
```

This registers Contexer's MCP server in `~/.codex/config.toml` (under `[mcp_servers.contexer]`) and wires hooks in `~/.codex/hooks.json`. The `config.toml` edit is surgical — only the contexer stanza is added or removed, so your existing servers, plugins, projects, and secrets are left untouched.

Codex's hooks use the same events as Claude Code (`SessionStart`, `PostToolUse`, `PreCompact`, `PostCompact`, `UserPromptSubmit`), so Contexer runs at **full Claude parity** there: automatic session-start injection, per-prompt rationale and constraint capture, post-edit reminders, and context reload after compaction all work.

The first time Codex calls a Contexer tool it asks you to approve it — Contexer does not pre-approve its own MCP tools for you.

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

This is intentional. Every piece of complexity added to a memory system is a piece of complexity that can fail, drift, or accumulate noise. Contexer stores only what matters — decisions — and keeps everything inspectable, auditable, and greppable.

---

## Limitations

- **Personal, not team.** The store is per-user, per-machine. There's no team sync yet — shared rules don't propagate between developers. (It's on the roadmap.)
- **Cursor parity is partial.** Cursor's `beforeSubmitPrompt` hook can't inject context (only allow/block) and it has no usable compaction hook, so per-prompt rationale injection and compaction save/restore are unavailable there. Cursor steering rides on the session-start nudge plus an always-apply rule file.
- **Gemini compression is deferred.** Gemini CLI has `PreCompress` but no `PostCompress`; Contexer re-injects stored context at the next prompt rather than immediately after compression.
- **Capture is best-effort.** Only outright directives ("always/never/don't/create a rule") are auto-stored deterministically. Other decisions depend on the agent choosing to call the store tool, and it does miss things — hence the *"store that decision"* escape hatch.
- **Soft storage cap.** Up to 500 entries per repo; beyond that, the least-reinforced decisions are evicted. There's no automatic staleness pruning — outdated decisions stay until you remove them.
- **One network call.** `contexer status` checks PyPI for a newer version. Disable with `CONTEXER_NO_UPDATE_CHECK=1`. Nothing else leaves your machine.

---

## CLI reference

| Command | Description |
|---|---|
| `contexer install` | Connect Contexer (auto-detects Claude Code, Cursor, Codex, and/or Gemini CLI) |
| `contexer install --target claude\|cursor\|codex\|gemini\|all` | Install for a specific tool only, or all |
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

**A new decision wasn't saved — looks like a duplicate.** Content too similar to an existing decision is silently skipped. Rephrase to include what specifically changed.

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

MIT — see [LICENSE](LICENSE) for full terms.

The Contexer name and logo are trademarks of Contexer.ai. The MIT license does not grant rights to use the Contexer name, logo, or brand in any way that implies official affiliation.
