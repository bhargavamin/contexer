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

Approving through the MCP tool (`approve_decision`) also accepts `source_files`: a list of repo-relative files that decision describes. Passing it anchors the decision to those files and the current commit — the same anchor the commit-time guard and staleness checks below rely on — so a decision can become guard-pairable the moment you approve it, not just when it was first captured. It only applies to a single decision id at a time (bulk approvals via `"all"` or a comma-list don't accept it).

Prefer to see them all at once? `contexer ui` opens a local web console with the same review queue — plus every stored decision, its revision history, global rules, and cached team context, for every repo on the machine. Proposed changes are shown as before/after diffs, and you can edit, delete, and restore decisions there too. It is loopback-only and starts on demand; see **[the local console](ui.md)**.

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
| `contexer review` | Review decisions awaiting approval: approve, edit, skip, or dismiss each. Also surfaces possibly-overlapping constraint/convention rules — keep the best one and retire the rest with `approve_decision(id, action="ignore")`, which works on already-approved rules too (Contexer itself never merges or deletes) |
| `contexer ui [--open]` | Start the local web console over every store on this machine and print its URL (`--open` also opens the browser). See [the local console](ui.md) |
| `contexer ui --status\|--stop\|--port N\|--foreground\|--reset-token` | Report on, stop, re-port, run in the foreground, or re-credential the console |
| `contexer share` | Show a numbered list of shareable decisions and multi-select which to push (e.g. `1,3` or `all`) |
| `contexer share <id[,id2…]> [--yes]` | Push the given decision(s) to your personal cloud. Previews what would leave your machine and confirms first; `--yes` skips the prompt. Set `skip_confirm = true` in `~/.contexer/config.toml` to always skip it |
| `contexer share --all [--yes]` | Push every non-ignored decision (previews the list and confirms first) |
| `contexer guard [path…] [--explain]` | Check staged files against approved decisions at commit time — see [commit-time guard](#commit-time-guard) |
| `contexer guard anchors [--list]` | Assisted anchor backfill for trusted decisions that predate anchoring — see [anchor backfill](#anchor-backfill) |
| `contexer status` | Show connection status, store size, current repo; warns about corrupt config files, cleans stale temp files, and notifies when a newer version is on PyPI |
| `contexer reinstall` | Re-sync after an AI assistant update |
| `contexer uninstall` | Disconnect; context store is kept |
| `contexer uninstall --purge` | Remove everything including `~/.contexer/` |
| `contexer version` | Print installed version |
| `contexer help` | Show all commands and flags |

## Commit-time guard

`contexer guard` checks staged files against your approved decisions when you commit — reminders for what it finds, hard blocks only for the rules you explicitly arm.

### Install the hook

```bash
contexer guard --install-hook     # wires .git/hooks/pre-commit for the current repo
contexer guard --uninstall-hook   # removes it (or just contexer's block from a shared hook)
```

Not run automatically by `contexer install` — it's opt-in per repo. `--install-hook` embeds the resolved binary's absolute path, appends to an existing foreign hook (byte-preserving it), and is idempotent (a second run no-ops). It refuses, with guidance, when:

- **`core.hooksPath` is set** (husky, `.githooks`, etc.) — hooks for the repo live outside `.git/hooks`, so it prints the block to add to that hook manager's script instead.
- **the existing `.git/hooks/pre-commit` was generated by the [pre-commit framework](https://pre-commit.com)** — appending would be wiped by the next `pre-commit install`. Add this to `.pre-commit-config.yaml` instead (the repo also ships a root-level `.pre-commit-hooks.yaml`, so `id: contexer-guard` works without copying anything):

  ```yaml
  - repo: local
    hooks:
      - id: contexer-guard
        name: contexer guard
        entry: contexer guard
        language: system
        verbose: true
        pass_filenames: false
        always_run: true
  ```

### Advisory tier (default)

```bash
contexer guard [path…] [--explain]
```

Pairs staged files against approved decisions with a human, scan, bootstrap, or approved-plan origin — never an unreviewed AI guess. (A decision stored before Contexer tracked origin is judged by who originally created it, so older trusted decisions still qualify.) A pair fires when the staged file is one of the decision's recorded source files, or a file/module path mentioned in the decision's content matches the staged path (an exact path, a dotted module, or a `/`-bounded multi-segment suffix — a bare filename never matches on its own). Advisories print and exit 0; up to 5 print per run (more are reported as suppressed), and a pair stops repeating once you dismiss it, or automatically once its file's staged content stops changing. `--explain` lists every candidate pair for the staged files, including rejected ones, with the reason.

```bash
contexer guard --dismiss <hash|n>
```

Permanently suppress one advisory pair. The `hash` printed on an advisory line dismisses it directly, no prompt; a numeric index re-lists current candidates and asks `[y/N]` before dismissing.

Skip the guard for one commit: `CONTEXER_GUARD=0 git commit …`.

### Blocking tier (opt-in)

```bash
contexer guard arm <id> --regex '<pattern>' [--flags i] [--paths <glob>] [--message <hint>]
contexer guard arm <id> --check secret
contexer guard disarm <id>
contexer guard list
```

Arms an *already-approved* decision as a machine-checkable rule; arming an unapproved decision is refused. `--regex` must compile (only the `i` flag is accepted); `--check secret` matches Contexer's own high-confidence secret patterns and takes no pattern of your own. `--paths` restricts the rule to a glob (default: every staged file); `--message` is shown alongside a violation. A violated rule prints the file, line, and decision, and exits 1 — the commit is blocked until you fix the file, disarm the rule (`guard disarm <id>`), or bypass with `CONTEXER_GUARD=0`. `guard list` shows every currently armed rule, repo and global. A decision later marked `ignored` stops firing automatically, without needing an explicit disarm.

### If it fails

The guard fails open by design: any internal error, or a run over its internal time budget, prints one line to stderr and exits 0 rather than blocking your commit. `git commit --no-verify` skips it entirely, same as any pre-commit hook — it's a local nudge, not a substitute for your CI/PR gates.

### Anchor backfill

The advisory tier can only pair a staged file against a decision that's *anchored* to it (`source_files`). New decisions pick up an anchor automatically as you capture and approve them, but older, already-trusted decisions in your store predate that and sit permanently invisible to Tier 1 until something anchors them after the fact.

```bash
contexer guard anchors --list   # preview candidates, read-only, no prompts
contexer guard anchors          # interactive: review and ratify per decision
```

`--list` scans every trusted, currently-unanchored decision, mines its own content for file paths and module names it mentions, and prints the ones that still exist in your working tree — nothing is written. It doubles as the non-interactive surface: run it in CI, a script, or from an agent, since the interactive loop below refuses outright when stdin isn't a TTY.

Without `--list`, and with a TTY attached, `contexer guard anchors` walks the same candidate list one decision at a time:

```
[Y] anchor all shown  [E] edit list (comma-separated)  [S] skip  [Q] quit
```

`Y` accepts the candidates as shown, `E` lets you type your own comma-separated file list instead (validated against the working tree the same way — a file that doesn't exist is dropped, not silently accepted), `S` skips that decision for this run, `Q` stops early. Every accepted selection across the whole run is written in a single batch at the end (or on quit) — one save, not one per decision. Nothing is anchored twice: a decision that's already anchored by the time the batch writes (approved, or backfilled, by a concurrent session) is left alone.

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
- **Deduplication is lexical, not semantic.** Duplicates are detected by token overlap, with no understanding of meaning — the same rule phrased with different words ("commit on approval" vs "commit automatically") can accumulate as separate entries. Containment restatements (re-typing a rule with extra words, or a terse version of it) are consolidated automatically; synonym phrasings are only flagged in the capture ack and surface in review for you to merge manually.
- **Cursor parity is partial.** Cursor's hooks can't inject per-prompt context or restore after compaction; Cursor steering rides on the session-start nudge plus an always-apply rule file. See [integrations](integrations.md).
- **Gemini compression is deferred.** Gemini CLI restores stored context on the next turn after compression, not immediately.
- **Contexer steers, it doesn't enforce.** Agents are told your rules before writing code; your CI and PR gates still verify. The one opt-in exception is the [commit-time guard](#commit-time-guard)'s blocking tier: a decision you explicitly `guard arm` fails the commit it violates — but only that decision, only on the machine where it was armed, and `git commit --no-verify` still skips it, so it's a local nudge, not a replacement for CI.
