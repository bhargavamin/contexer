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
  <em>The decision and enforcement layer for AI coding agents.</em>
</p>

<p align="center">
  <a href="https://pypi.org/project/contexer/"><img src="https://img.shields.io/pypi/v/contexer" alt="PyPI version"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-MIT-blue.svg" alt="License: MIT"></a>
  <a href="https://www.python.org/"><img src="https://img.shields.io/badge/python-3.12%2B-blue" alt="Python 3.12+"></a>
  <a href="https://github.com/bhargavamin/contexer/stargazers"><img src="https://img.shields.io/github/stars/bhargavamin/contexer?style=flat&logo=github" alt="GitHub stars"></a>
  <a href="https://discord.gg/Fk6JSaW4p"><img src="https://img.shields.io/badge/Discord-Join-5865F2?logo=discord&logoColor=white" alt="Discord"></a>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#enforcement--guardrails">Enforcement</a> ·
  <a href="#see-every-decision-with-its-evidence">Console</a> ·
  <a href="docs/faq.md">FAQ</a> ·
  <a href="docs/benchmark.md">Benchmark</a> ·
  <a href="#documentation">Docs</a> ·
  <a href="https://discord.gg/Fk6JSaW4p">Discord</a>
</p>

<p align="center">
  <sub><strong>New:</strong> <a href="https://contexer.ai/teams">Contexer Personal Cloud &amp; Teams</a>: sync decisions across machines, share a team decision layer.</sub>
</p>

---

# Train your AI agents to work exactly how you want. And enforce it.

If you work with AI agents every day, you already know these:

- *"My agent ignores my instructions the moment the context window fills up, and I'm back to re-explaining what it must never do."*
- *"I switched to a new agent and every rule and decision I'd built up stayed behind with the old one."*
- *"I run a whole stack of AI tools, and I still can't drive what they decide, let alone enforce it."*
- *"The answer is in there somewhere, buried under a thousand lines of session slop."*
- *"I have no idea what my team's agents are being told, or why they built it that way."*

Contexer is the **decision and enforcement layer**: it captures engineering decisions as you work and keeps them current as your architecture evolves. It **selectively injects the relevant decision, constraint, or convention the moment the agent needs it**. At commit time, staged changes that violate an approved decision get flagged, or blocked outright for rules you arm. The settled answer arrives before the agent starts reasoning; it stops re-exploring and just builds.

Questions about capture accuracy, privacy, supported agents, instruction files, or the difference
between open source and Teams? **[Read the public FAQ →](docs/faq.md)**

## What it solves

Contexer gives developers one reviewable, versioned decision store across Claude Code, Cursor,
Codex, and Gemini CLI. It brings approved context back into later sessions and can check staged
changes against decisions before they land. The open-source product is local and individual;
**[Contexer Teams](https://contexer.ai/teams)** adds reviewed shared decisions and GitHub workflows.

### Benchmarks

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/benchmark-dark.svg">
    <img alt="Benchmarks, Sonnet 5 medians across 548 published live agent sessions: 6x fewer tokens per session (32,804 vs 198,864 with no memory); right answer in 1 turn vs 6 turns of guessing; 8/8 stored rules followed vs 0/8 bare (Opus); $0.043 per session with right answers vs $0.116 with wrong ones" src="assets/benchmark-light.svg" width="1000">
  </picture>
</p>

The honest part: a complete, up-to-date CLAUDE.md ties Contexer on cost. The difference is that nobody has to write that file or keep it perfect. Every number is recomputable from raw session rows, scored by code (no LLM judge), independently validated, negative findings included. **[Read the benchmark →](docs/benchmark.md)**

---

## Quick start

Requires **Python 3.12+** and **[uv](https://docs.astral.sh/uv/getting-started/installation/)**. Under two minutes.

```bash
uv tool install contexer   # 1. install
contexer install           # 2. wire into your AI assistants
```

`contexer install` auto-detects Claude Code, Cursor, Codex, and Gemini CLI, and wires everything it finds. Restart your assistant and open any git repo. On first use, Contexer analyzes the repo and proposes its starting knowledge. It runs silently from there.

Details: **[installation & verification](docs/install.md)** · **[per-tool integration notes](docs/integrations.md)**

---

## How it works

1. **Capture:** Contexer records engineering decisions and supporting provenance while you work.
2. **Review:** Developers decide which proposed decisions become trusted.
3. **Replay:** Relevant approved context is delivered to supported coding agents in later sessions.
4. **Check:** Optional safeguards compare changes with approved decisions.

Read **[How Contexer works](docs/how-it-works.md)** for the mechanism and
**[the FAQ](docs/faq.md)** for capture limitations, authority, lifecycle, relevance, agent parity,
privacy, and product boundaries. Day-to-day commands live in **[Usage & CLI](docs/usage.md)**.

---

## Enforcement / guardrails

The local Guard is an optional pre-commit safeguard:

```bash
contexer guard --install-hook   # wires .git/hooks/pre-commit for this repo (opt-in, not run by `install`)
```

It is advisory by default. Blocking requires an approved decision that the developer explicitly
arms with a machine-checkable rule. See the **[FAQ](docs/faq.md#what-is-the-local-contexer-guard)**
for behavior and limits, the **[mechanism](docs/how-it-works.md#commit-time-guard)** for design, and
the **[CLI reference](docs/usage.md#commit-time-guard)** for commands.

---

## See every decision, with its evidence

```bash
contexer ui --open
```

A local web console over **every repo on your machine**, not just the one you're in, because "what does my agent actually know?" is a question you shouldn't have to answer by reading JSON.

<p align="center">
  <img src="assets/console-dashboard.png" alt="Contexer console per-repo dashboard: stored decisions by subtype and status, review queue, global rules, cached team context, and recent decisions" width="900">
</p>

- **Read it, then fix it.** Browse and search every stored decision with its full revision history. Edit one; history is kept, nothing is overwritten. Delete one for good: a delete sticks, instead of quietly reappearing next session from a memory file or a mined conversation. Restore it if you change your mind.
- **See the whole review queue at once**, not one terminal prompt at a time: what's pending, and proposed changes to decisions you already approved shown as before/after diffs. The graphical counterpart to `contexer review`.
- **The whole picture.** Per-repo dashboard, global rules, cached team context, deleted decisions, and your settings: one switcher, seven views.

It binds `127.0.0.1`, every route is authenticated, and the server itself never reaches the network. It is also **off until you ask for it**: `contexer ui` starts it on demand, and the printed link is short-lived on purpose. Set `[ui] autostart = true` and every session start hands you the URL for the repo you just opened.

Details, the `[ui]` settings, and the security model: **[the local console](docs/ui.md)**

---

## Documentation

| | |
|---|---|
| **[FAQ](docs/faq.md)** | Product questions, capture limits, authority, privacy, Guards, OSS and Teams |
| **[Installation](docs/install.md)** | Install, verify, update, uninstall |
| **[Integrations](docs/integrations.md)** | Claude Code, Cursor, Codex, Gemini CLI: wiring and parity notes |
| **[How it works](docs/how-it-works.md)** | Capture, bootstrap, session injection, review/versioning, cost, privacy |
| **[Usage & CLI](docs/usage.md)** | Natural-language commands, CLI reference, teams login, troubleshooting, limitations |
| **[Local console](docs/ui.md)** | `contexer ui`, the seven views, `[ui]` settings, security model |
| **[Benchmark](docs/benchmark.md)** | Live-session A/B methodology, findings (including negative ones), raw data |

---

## Contributing

Bug reports, fixes, and documentation improvements are welcome. See **[CONTRIBUTING.md](CONTRIBUTING.md)** for setup, code style, and the PR process. Questions or ideas? Join the community on [Discord](https://discord.gg/Fk6JSaW4p).

## License

MIT. See [LICENSE](LICENSE) for full terms.

The Contexer name and logo are trademarks of Contexer.ai. The MIT license does not grant rights to use the Contexer name, logo, or brand in any way that implies official affiliation.
