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
  <a href="https://github.com/bhargavamin/contexer/stargazers"><img src="https://img.shields.io/github/stars/bhargavamin/contexer?style=flat&logo=github" alt="GitHub stars"></a>
  <a href="https://discord.gg/Fk6JSaW4p"><img src="https://img.shields.io/badge/Discord-Join-5865F2?logo=discord&logoColor=white" alt="Discord"></a>
</p>

<p align="center">
  <a href="#the-developer-problem-re-teaching-your-agent-every-session">For developers</a> ·
  <a href="#the-leadership-problem-your-standards-never-reach-the-code">For engineering leaders</a> ·
  <a href="#what-contexer-gives-you-that-a-md-file-cant">Why not just CLAUDE.md?</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="docs/benchmark.md">Benchmark</a> ·
  <a href="#documentation">Docs</a> ·
  <a href="https://discord.gg/Fk6JSaW4p">Discord</a>
</p>

<p align="center">
  <strong>🚀 Now live — <a href="https://contexer.ai/teams">Contexer Personal Cloud &amp; Teams</a></strong><br>
  <sub>Sync your engineering decisions across every machine, and share a team decision layer that every agent on your team reads from.</sub>
</p>

---

# Stop your AI agents from overthinking, repeating, and burning tokens on what you already decided.

You explained it Monday. Again Wednesday. It's Friday, and your AI is asking again — or quietly re-deriving the answer from scratch, six turns at a time. **And if you're the diligent one who maintains a CLAUDE.md:** you updated it this morning; the afternoon session reworked an entire feature; the file is already out of date.

Every AI coding session starts from zero, and the files you write to fix that go stale the moment an agent ships something new. Contexer is the fix: a **decision layer** that captures your engineering decisions as you work, keeps them current as they change, and hands them to **every agent** — Claude Code, Cursor, Codex, Gemini CLI — **before it starts reasoning**. The settled answer arrives first; the agent stops wondering, stops re-exploring — and just builds.

**Session 1, Tuesday** — the agent proposes `LIMIT/OFFSET` pagination for a listing endpoint. You correct it:

> **You:** No offset pagination — that collapsed for us at 10M rows. Cursor-based on `(created_at, id)`, always, and the composite index is mandatory.
> _Contexer stores the convention — including the reason._

**Session 3, three weeks later** — fresh context, different feature, maybe a different tool. You ask for a new listing endpoint, and before the agent writes a line:

> _Contexer injects:_ `[convention] Pagination is cursor-based on (created_at, id) with the composite index — offset pagination collapsed at 10M rows.`
> The agent ships cursor pagination on the first try. And when a teammate's session asks *"why don't we use OFFSET here?"* — it gets the real answer, with the incident behind it, not a guess from training data.

Every claim about Contexer is **measured, not claimed** — hundreds of live agent sessions, deterministic scoring, independent validation, negative findings published. **[Read the benchmark →](docs/benchmark.md)**

---

## The developer problem: re-teaching your agent every session

You know these moments:

- **"I've told it this three times."** You corrected the agent's mocking strategy Monday. It's Wednesday and the agent is mocking the DB layer again. → Contexer injects your conventions at session start, every session. Correct once, done forever.
- **"Why did we pick this?"** You ask about a decision made three weeks ago and the agent confidently guesses wrong from training data. → Ask "why did we choose Postgres?" and Contexer hands the agent the *actual stored decision* — with the reasoning — before it answers.
- **"Cursor doesn't know what Claude knows."** You switch tools and start re-teaching. → One decision layer, four agents. Capture in any of them; all of them know it.
- **"I maintain four rule files now."** CLAUDE.md for Claude, AGENTS.md for Codex, `.cursor/rules` for Cursor, GEMINI.md for Gemini — times every repo. You wrote them in January; the codebase moved on in February; now they quietly teach every agent things that are no longer true. → Zero files to maintain. Decisions are captured when they're made and updated when they change, in one store every tool reads.
- **"My context just got compacted."** Long session, context compression eats the decisions you fetched. → Contexer restores them automatically.

No workflow change. No prompt discipline. You code; directives like *"always use uv, never pip"* are captured automatically, and *"store that decision"* catches anything else. Everything stays in plain JSON on your machine.

## The leadership problem: your standards never reach the code

If you run an engineering team, your real problem isn't that AI writes bad code — it's that **AI writes code without knowing what your organization has decided**:

- **Standards that live in a wiki don't reach the code.** Your security rule — *"never log request data"*, *"no plaintext secrets"* — is documented, and violated, because no agent reads the wiki at the moment it writes the line. Contexer puts the rule in front of every agent, in every session, *before generation* — the leftmost shift there is.
- **Every senior engineer is a walking decision archive** — and those decisions leave in AI chat scrollback, Slack threads, and resignations. Contexer turns them into a versioned, human-approved, auditable asset: who decided what, when, and why.
- **Onboarding costs weeks of "ask the person who knows".** A new engineer's agent starts with the repo's full decision history from day one.
- **Multi-agent sprawl is already here.** Your team runs Claude Code *and* Cursor *and* Codex. Contexer is the one layer that keeps them all consistent — no single-vendor lock-in on your engineering knowledge.
- **Governance, not vibes.** Decisions are approved by humans, versioned with full history, and reviewable (`contexer review`). AI proposes; your engineers ratify. And Contexer steers rather than enforces — your CI and PR gates still verify; agents just stop violating the rules in the first place.

The open-source version is per-developer. **[Contexer Teams](https://contexer.ai)** (early access) makes it organizational: your platform or security team publishes a rule once, and every developer's agent starts with it — every repo, every tool, before the code is written.

---

## What Contexer gives you that a .md file can't

A complete, up-to-date CLAUDE.md is genuinely as token-efficient as Contexer — [we measured it](docs/benchmark.md). But files start incomplete and go stale. This is what you're actually buying:

| | Hand-written file (CLAUDE.md / AGENTS.md) | Contexer |
|---|---|---|
| Who writes it | You, by hand | Written for you: the repo is scanned and measured on first use; decisions are captured as you work |
| How many to maintain | One per tool, per repo — CLAUDE.md, AGENTS.md, `.cursor/rules`, GEMINI.md — and the copies drift apart | Zero files. One store serves every tool and repo |
| Who keeps it current | You. Nobody does — files decay | Every decision is stored the moment it's made, with the reasoning |
| When it's wrong or outdated | Silently misleads every session | Changes need your approval; full version history of every decision is kept |
| "Why did we build it this way?" | Answered only if someone wrote it down that day | The stored decision includes the reasoning, and the AI is handed it before answering |
| Which tools benefit | Only the tool that reads that file | Claude Code, Cursor, Codex, and Gemini CLI — one store, captured once, known everywhere |
| Long sessions | Knowledge the AI dug up is lost when the conversation is compressed | Restored automatically after compression |
| What it costs when nothing is relevant | The whole file is loaded every session regardless | Nothing loads except your always-on rules (~26 tokens each); lookups add milliseconds |
| Your team | Everyone maintains their own copy | One shared, approved source across the team (Teams, early access) |

---

## Quick start

Requires **Python 3.12+** and **[uv](https://docs.astral.sh/uv/getting-started/installation/)**. Under two minutes.

```bash
uv tool install contexer   # 1. install
contexer install           # 2. wire into your AI assistants
```

`contexer install` auto-detects Claude Code, Cursor, Codex, and Gemini CLI, and wires everything it finds. Restart your assistant, open any git repo — on first use, Contexer analyzes the repo and proposes its starting knowledge. It runs silently from there.

Details: **[installation & verification](docs/install.md)** · **[per-tool integration notes](docs/integrations.md)**

---

## How it works

Contexer captures four kinds of engineering knowledge — **constraints** ("never merge untested code"), **conventions** ("uv, not pip"), **architecture decisions** ("REST over GraphQL, here's why"), and **patterns** — and replays them at the right moment: rules load at session start; architecture and rationale are fetched on demand the instant a question needs them.

You drive it in plain English:

```
"save this as a convention: always use uv not pip"
"what decisions did we make about postgres?"
"store that globally: conventional commits everywhere"
```

Trust is explicit: AI-*proposed* decisions are held for your review (`contexer review`) and never reach a session until you approve them. Approved decisions are versioned — history preserved, latest approved revision replayed. Cost is flat and tiny (roughly 26 tokens per rule at session start, nothing on unrelated prompts).

Deep dive: **[how it works](docs/how-it-works.md)** · **[day-to-day usage & CLI](docs/usage.md)**

### Honest limits

Capture beyond outright directives is best-effort (the *"store that decision"* escape hatch exists for a reason); Cursor's hook model limits per-prompt injection there; the OSS store is per-developer, not shared. Full list, published on purpose: **[limitations](docs/usage.md#limitations-read-this--we-publish-them-on-purpose)**.

---

## Documentation

| | |
|---|---|
| **[Installation](docs/install.md)** | Install, verify, update, uninstall |
| **[Integrations](docs/integrations.md)** | Claude Code, Cursor, Codex, Gemini CLI — wiring and parity notes |
| **[How it works](docs/how-it-works.md)** | Capture, bootstrap, session injection, review/versioning, cost, privacy |
| **[Usage & CLI](docs/usage.md)** | Natural-language commands, CLI reference, teams login, troubleshooting, limitations |
| **[Benchmark](docs/benchmark.md)** | Live-session A/B methodology, findings (including negative ones), raw data |

---

## Star history

If Contexer saves you re-explanation time, a star helps others find it. It takes one second.

[![Star History Chart](https://api.star-history.com/svg?repos=bhargavamin/contexer&type=Date)](https://star-history.com/#bhargavamin/contexer&Date)

## Contributing

Bug reports, fixes, and documentation improvements are welcome. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for setup, code style, and the PR process. Questions or ideas? Join the community on [Discord](https://discord.gg/Fk6JSaW4p).

## License

MIT. See [LICENSE](LICENSE) for full terms.

The Contexer name and logo are trademarks of Contexer.ai. The MIT license does not grant rights to use the Contexer name, logo, or brand in any way that implies official affiliation.

---

Contexer is **not a chat memory tool**. It is **the engineering decision layer for AI coding agents** — capturing architecture decisions, constraints, conventions, and patterns so engineering knowledge becomes a shared organizational asset instead of disappearing inside AI conversations.
