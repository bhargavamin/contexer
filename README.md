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
  <em>Capture decisions. Review changes. Enforce selected rules.</em>
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
  <a href="#features">Features</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#optional-commit-checks">Commit checks</a> ·
  <a href="#browse-and-manage-your-decisions">Console</a> ·
  <a href="#personal-cloud-and-teams">Cloud &amp; Teams</a> ·
  <a href="docs/benchmark.md">Benchmark</a> ·
  <a href="docs/faq.md">FAQ</a> ·
  <a href="#documentation">Docs</a> ·
  <a href="https://discord.gg/Fk6JSaW4p">Discord</a>
</p>

<p align="center">
  <sub><strong>New:</strong> <a href="https://contexer.ai/teams">Contexer Personal Cloud &amp; Teams</a>: sync decisions across machines, share a team decision layer.</sub>
</p>

---

# The decision and enforcement layer for AI coding agents

Capture engineering decisions and their reasoning, review changes, and bring the right guidance into **Claude Code, Cursor, Codex, and Gemini CLI**. Share approved decisions through optional Contexer Teams and enforce selected rules with optional commit checks.

Contexer keeps a reviewable, versioned record of what was decided, why, and whether it has been approved. The same local decision store works across supported assistants, so your engineering guidance is not tied to one agent's conversation history. Review, revision history, team approval, and explicitly enabled checks turn recorded decisions into guidance you can manage and verify.

### A simple example

You tell your assistant:

> "Always use our shared API client for outgoing requests. It handles authentication and retries; separate implementations caused duplicate requests."

Contexer can save both the rule and its reason. In a later session, your assistant can retrieve that decision instead of guessing why the shared client exists. If you link the decision to source files, Contexer can flag when those files change so you know to recheck the advice.

Clear directives can be captured automatically. For an important decision made during a discussion, say **"store that decision"**. Capture beyond explicit directives is best-effort.

## See Contexer in action

Watch how to install Contexer and capture your first decisions.

[![Watch: Install Contexer and capture your first decisions](https://www.loom.com/v1/videos/4d4c22353c5c4a1aacc7a95dab8cb698/thumbnail)](https://www.loom.com/share/4d4c22353c5c4a1aacc7a95dab8cb698)

## Quick start

Requires **Python 3.12+** and **[uv](https://docs.astral.sh/uv/getting-started/installation/)**.

```bash
uv tool install contexer   # 1. install
contexer install           # 2. wire into your AI assistants
```

`contexer install` auto-detects supported assistants and configures the ones it finds. Restart your assistant, open your project, and follow Contexer's first-run setup guidance to build its starting context.

Then try telling your assistant:

```text
Save this as a convention: always use uv, not pip, in this project.
```

Decisions are stored locally in plain JSON under `~/.contexer/`. The local features do not require a cloud account; cross-machine sync and team sharing are optional.

Details: **[installation & verification](docs/install.md)** · **[per-tool integration notes](docs/integrations.md)**

---

## Features

| Feature | What it does for you |
|---|---|
| **Decisions with reasons** | Saves what you chose and why: architecture, constraints, conventions, and reusable patterns. |
| **Relevant reminders** | Loads standing rules at session start and retrieves matching decisions using your question or referenced files. Avoids repeatedly injecting decisions already shown in the session. |
| **Decisions across assistants** | Makes the same local project decisions available to Claude Code, Cursor, Codex, and Gemini CLI. Automatic delivery varies by assistant. |
| **Project and global rules** | Keeps project-specific choices separate from preferences you want to reuse across repositories. |
| **Repository-derived context** | Reads configuration and uses source-backed analysis to build starting knowledge. Observations and inferences are labeled, not treated as human-approved rules. |
| **Review and history** | Lets you review pending decisions and proposed updates, while preserving earlier revisions and their origins. |
| **Outdated-advice warnings** | Flags changes to linked code, follows detected file renames, and rechecks measured conventions. A warning asks you to verify the decision; it does not prove it is wrong. |
| **Optional commit checks** | Reminds you of relevant approved rules. Explicitly armed regex or secret checks can block a commit. |
| **Local web console** | Lets you browse, search, edit, review, delete, and restore decisions across your projects. |
| **Claude memory import** | Imports Claude Code's file-based memory into the decision store, updating existing entries when the source changes. |

Keep your `CLAUDE.md`, `AGENTS.md`, `GEMINI.md`, or `.cursor/rules` files for stable instructions. Contexer adds searchable decisions, revision history, review, and file-linked context alongside them.

---

## How it works

1. **Capture.** Contexer detects clear directives, imports supported memory files, and gives your assistant tools to save decisions with their reasoning.
2. **Reuse.** Standing rules load when a session starts. Matching decisions can be retrieved as you ask questions or reference files, without loading the entire store.
3. **Review and maintain.** Inspect pending items, correct an entry, or update a decision when your approach changes. Earlier revisions remain available.

Ask your assistant in plain English:

```text
What decisions did we make about Postgres?
Store that decision, including why we rejected the other option.
Store that globally: use conventional commits across my projects.
```

Deep dive: **[how it works](docs/how-it-works.md)** · **[day-to-day usage & CLI](docs/usage.md)**

### Review and trust

```bash
contexer review
```

Not all captured knowledge has the same status. Clear user directives can be approved automatically. Repository observations and AI-inferred starting context are usable but explicitly non-authoritative; confidence or repeated scans do not count as human approval. Some other AI-inferred knowledge is available as a labeled suggestion; items marked **pending approval** wait for your review instead of becoming standing rules.

Proposed changes to an existing decision do not silently replace its current revision. When a proposed update conflicts with the standing decision, Contexer can show both versions, clearly labeling the unreviewed one, so you can choose what is correct.

Use the review command or local console to inspect the reasoning and evidence, approve or edit pending items, and dismiss advice you do not want.

---

## Optional commit checks

`contexer guard` adds an opt-in Git pre-commit hook with two levels of checking:

```bash
contexer guard --install-hook   # wires .git/hooks/pre-commit for this repo (opt-in, not run by `install`)
```

**Reminders by default.** A relevant, trusted approved decision linked to or mentioning a staged file surfaces before the commit, which still goes through. This is a reminder to check the rule, not automatic detection of a violation.

**Blocking only when you enable it.** Arm an approved decision with a machine-checkable rule (`contexer guard arm <id> --regex '<pattern>'` or `--check secret`). A detected violation can then fail the commit. Only explicitly armed checks can block; the guard does not interpret arbitrary architectural rules as executable tests.

GUI commit flows (VS Code, Cursor, and similar) run the hook but only surface a blocked commit; the warning-only reminders are terminal output a graphical commit panel won't show you.

The guard fails open (any internal error, or a run over budget, skips checks rather than blocking your commit) and can be bypassed per-commit with `CONTEXER_GUARD=0 git commit …`, same as any pre-commit hook is bypassed with `--no-verify`. It's a local nudge, not a substitute for CI: your pipeline's checks are the backstop that can't be skipped from a developer's machine.

Details: **[mechanism](docs/how-it-works.md#commit-time-guard)** · **[CLI reference](docs/usage.md#commit-time-guard)**

---

## Browse and manage your decisions

```bash
contexer ui --open
```

A local web console shows what Contexer has stored across your projects, not just the repository you are working in.

<p align="center">
  <img src="assets/console-dashboard.png" alt="Contexer console per-repo dashboard: stored decisions by subtype and status, review queue, global rules, cached team context, and recent decisions" width="900">
</p>

- **Inspect and edit.** Search decisions, read their reasoning and revision history, and correct their content without losing earlier versions.
- **Review proposed changes.** See pending decisions and before/after diffs for proposed updates.
- **Delete and restore.** Remove unwanted decisions while retaining the option to restore them. Deletion markers prevent the same imported entry from immediately reappearing.
- **Switch context.** Browse per-repo decisions, global rules, cached team context, deleted entries, and settings.

It binds `127.0.0.1`, every route is authenticated, and the server itself never reaches the network. It is also **off until you ask for it**: `contexer ui` starts it on demand, and the printed link is short-lived on purpose. Set `[ui] autostart = true` and every session start hands you the URL for the repo you just opened.

Details, the `[ui]` settings, and the security model: **[the local console](docs/ui.md)**

---

## Personal Cloud and Teams

The open-source local store works for one developer across supported assistants. Optional hosted services extend where that knowledge is available:

| Option | What it is for |
|---|---|
| **Local (open source)** | Keep decisions on your machine and reuse them across your AI assistants. No cloud account required. |
| **Personal Cloud** | Sync your own decisions across your own machines. Personal sync does not share them with teammates. |
| **Teams (early access)** | Share decisions through a team review and approval workflow, so teammates' assistants can use shared context. |

### See personal and team context in action

Watch how Contexer handles your personal decisions and shared team context.

[![Watch: Contexer personal and team context](https://www.loom.com/v1/videos/849265aafaea4c39a31c5c5a15856fb2/thumbnail)](https://www.loom.com/share/849265aafaea4c39a31c5c5a15856fb2)

Learn more: **[Personal Cloud & Teams](https://contexer.ai/teams)** · **[connection and sharing guide](docs/usage.md#connecting-to-a-team-contexer-teams)**

## Limitations

- **Capture is not exhaustive.** Contexer does not save every useful conclusion from every conversation. Explicitly ask to store important decisions and review what was captured.
- **Assistant integrations differ.** Cursor's hooks limit per-prompt context injection; Gemini CLI restores context on the next turn after compression. See the [integration notes](docs/integrations.md).
- **Code changes are signals, not verdicts.** A changed file can make a decision worth reviewing, but Contexer cannot guarantee that all stored advice is current or correct.
- **Reminders are not enforcement.** Agents can still ignore context, and local commit hooks can be bypassed. Keep tests and CI checks for guarantees you depend on.
- **Sharing is optional.** The local store is per-developer. Cross-machine sync and team context require the corresponding hosted service.

Full details: **[published limitations](docs/usage.md#limitations-read-this--we-publish-them-on-purpose)**.

## Benchmarks

The published benchmark compares live agent sessions with and without Contexer, measuring token usage, cost, answer quality, and adherence to stored rules.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/benchmark-dark.svg">
    <img alt="Published Contexer benchmark results comparing token usage, turns, rule adherence, and session cost" src="assets/benchmark-light.svg" width="1000">
  </picture>
</p>

These results describe the measured tasks, not a guarantee for every project. A complete, up-to-date `CLAUDE.md` ties Contexer on cost in the published comparison. The report includes methodology, deterministic scoring, raw session data, and negative findings.

**[Read the benchmark →](docs/benchmark.md)**

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
