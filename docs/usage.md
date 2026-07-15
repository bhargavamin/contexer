# Using Contexer day to day

Everything uses natural language. There are no commands to memorize during a session — you talk to your agent, Contexer listens where you told it to.

## Store a decision

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

## Query decisions

```
"show me all constraints"
"what decisions did we make about postgres?"
"show everything stored for this repo"
```

## Review pending decisions

AI-proposed architecture and constraint decisions — and any change to a decision you have already approved — are held for your review instead of being trusted automatically. They are stored, but not replayed into AI sessions until you approve them. Review them whenever you like:

```bash
contexer review
```

For each one you can **approve**, **edit**, **skip** (decide later), or **dismiss**. At session start Contexer reminds you, without blocking, when items are waiting.

## Update or remove

```
"update the uv decision - we switched back to pip"
"delete the postgres decision"
"remove all outdated constraints"
```

The store is plain JSON at `~/.contexer/`. Edit it directly if you prefer.

## CLI reference

| Command | Description |
|---|---|
| `contexer install` | Connect Contexer (auto-detects Claude Code, Cursor, Codex, and/or Gemini CLI) |
| `contexer install --target claude\|cursor\|codex\|gemini\|all` | Install for a specific tool only, or all |
| `contexer review` | Review decisions awaiting approval: approve, edit, skip, or dismiss each. Also surfaces possibly-overlapping constraint/convention rules for manual consolidation (read-only — never merges or deletes) |
| `contexer share` | Show a numbered list of shareable decisions and multi-select which to push (e.g. `1,3` or `all`) |
| `contexer share <id[,id2…]> [--yes]` | Push the given decision(s) to your personal cloud. Previews what would leave your machine and confirms first; `--yes` skips the prompt. Set `skip_confirm = true` in `~/.contexer/config.toml` to always skip it |
| `contexer share --all [--yes]` | Push every non-ignored decision (previews the list and confirms first) |
| `contexer status` | Show connection status, store size, current repo; warns about corrupt config files, cleans stale temp files, and notifies when a newer version is on PyPI |
| `contexer reinstall` | Re-sync after an AI assistant update |
| `contexer uninstall` | Disconnect; context store is kept |
| `contexer uninstall --purge` | Remove everything including `~/.contexer/` |
| `contexer version` | Print installed version |
| `contexer help` | Show all commands and flags |

## Connecting to a team (Contexer Teams)

Once you have access, joining a team is two commands — nothing to hand-edit:

```bash
contexer install     # local setup
contexer login       # opens your browser to sign in; enables personal cloud and team sync
```

`contexer login` signs you in via the browser (OAuth), stores the credential, and configures the team endpoint for you. After that your agent automatically pulls the team's approved decisions into every session, and `contexer share [id]` pushes a local decision up (`contexer share --all` pushes every non-ignored decision, oldest first). `contexer logout` disconnects.

Pointing at a self-hosted or local Teams server (for development): set `CONTEXER_ENV=local` before `contexer login`, or pass `contexer login --endpoint <url>`.

## Troubleshooting

**The agent isn't storing decisions automatically.** Say *"store that decision"* and it is captured immediately.

**A decision was stored but isn't appearing.** Constraints and conventions load at session start. If added mid-session, they appear from the next session onward.

**A decision is outdated or wrong.** Say *"delete the X decision"* or edit the store file directly at `~/.contexer/`.

**A new decision wasn't saved. Looks like a duplicate.** Content too similar to an existing decision is silently skipped. Rephrase to include what specifically changed.

**No context appeared at session start on a new repo.** The agent will offer bootstrap setup. Complete it once and all future sessions will have context.

## Limitations (read this — we publish them on purpose)

What exists today: the **open-source (OSS)** version, **Personal Cloud**, and **Teams** (early access). An **Org tier** (organization-wide governance across many teams) is not built yet — if that's what you need, [reach out](https://contexer.ai).

**OSS (free, local):**

- **Per-user, per-machine.** Your decisions live only in `~/.contexer/` on that machine. They don't follow you to your laptop and don't reach teammates.
- **Soft storage cap.** Up to 500 entries per repo; beyond that, the least-reinforced decisions are evicted. There's no automatic staleness pruning — outdated decisions stay until you remove them.
- **One network call.** `contexer status` checks PyPI for a newer version. Disable with `CONTEXER_NO_UPDATE_CHECK=1`. Nothing else leaves your machine.

**Personal Cloud (available):**

- Syncs **your own** decisions across **your own** machines. It does not share anything with teammates — that's Teams.

**Teams (available, early access):**

- Shared, approved team decisions reach every member's agent. Governance is at the team level; there is no multi-team hierarchy, org-wide policy layer, or SSO yet — that's the unbuilt Org tier above.

**Every tier:**

- **Capture is best-effort.** Only outright directives ("always/never/don't/create a rule") are auto-stored deterministically. Other decisions depend on the agent choosing to call the store tool, and it does miss things. Hence the *"store that decision"* escape hatch.
- **Cursor parity is partial.** Cursor's hooks can't inject per-prompt context or restore after compaction; Cursor steering rides on the session-start nudge plus an always-apply rule file. See [integrations](integrations.md).
- **Gemini compression is deferred.** Gemini CLI restores stored context on the next turn after compression, not immediately.
- **Contexer steers, it doesn't enforce.** Agents are told your rules before writing code; your CI and PR gates still verify.
