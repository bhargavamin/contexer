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

## What Contexer notices without being asked

Alongside the decisions your agent stores explicitly, Contexer keeps a small local record of the signals a session produced: rules you stated outright, files that were edited, and conclusions your agent reported about how something works.
Nothing there is a decision yet.
At the start of your next session Contexer groups those signals, and where they add up to something worth your attention it puts one item in the review queue described above.
Every such item is pending review: never trusted, never replayed into a session, never linked to a file, and never able to block a commit until you approve it.

Your agent can report a conclusion deliberately, which is what the `record_agent_conclusion` tool is for.
Ask for it in plain language:

```
"record what you just worked out about the auth flow"
```

That records the claim as a signal, with your agent's own reasoning attached.
It stores no decision by itself, changes nothing you have already approved, and a bare claim with no reasoning behind it is kept but is not proposed to you at all.

This is honest rather than complete.
Contexer cannot see your agent's answers, so a reported conclusion is exactly that, something your agent chose to report, not something Contexer observed.
It has no way to see test results or diffs at all yet, and on Cursor it sees your prompts but not your file edits.

`contexer status` prints one `coverage:` line per connected tool saying what that tool can actually observe:

```
  coverage:     claude: directives captured, file changes captured, conclusions agent-reported, test results unavailable, diffs unavailable
  coverage:     cursor: directives captured, file changes unavailable, conclusions agent-reported, test results unavailable, diffs unavailable
```

It reports a capability, never a count, on purpose.
"File changes unavailable" and "no file changes happened" are different facts, and a line that showed you a zero for both would hide a missing hook behind a quiet session.

## Query decisions

```
"show me all constraints"
"what decisions did we make about postgres?"
"show everything stored for this repo"
"which decisions apply to src/api/users.py?"
```

Ask which decisions govern a specific file (or files) and Contexer returns only the ones actually about it — linked to that file, or mentioning it in their text — instead of everything stored.

## Review pending decisions

AI-proposed architecture and constraint decisions — and any change to a decision you have already approved — are held for your review instead of being trusted automatically. They are stored, but not replayed into AI sessions until you approve them. Review them whenever you like:

```bash
contexer review
```

For each one you can **approve**, **edit**, **skip** (decide later), or **dismiss**. At session start Contexer reminds you, without blocking, when items are waiting.

Under each decision, Contexer prints what approving it would actually do.
The same block appears in `contexer review`, in the MCP `review_pending` tool, and on the decision's page in the console, so a decision never reads one way in the terminal and another in the browser.
It names where the decision came from, which evidence supports it and how strongly, which files approving it would link (and which files it saw but will deliberately NOT link, with the reason), what this host was able to observe at all, similar decisions already stored, and the decision's own revision history.

**Approving does not turn a decision into a commit block.**
Approval makes a decision trusted context: it is replayed into sessions and it can raise an advisory warning at commit time.
Turning one into a rule that actually stops a commit is a separate, explicit step you take yourself with `contexer guard arm`, and nothing in the review flow does it for you.
The review block says so for each decision, and it will tell you when a rule is already armed.

### Decisions you switched off, restated later

If you retire or ignore a decision and then say the same thing again in a later session, Contexer does not quietly resurrect it and does not file it as a brand-new decision beside the old one.
It asks you about the original.
That question appears in the same review queue with its own choices: **restore**, **restore with edits** (your wording becomes the new version), **skip**, or **dismiss**.

Dismiss means "not this time", not "never ask again", and the queue records that you were asked before, so a later restatement says how many times it has come up.
Only your explicit answer ever brings a decision back.
Restoring a decision that has no earlier approved state returns it as pending review rather than guessing it was approved, so you may be asked to approve it once it is back.

Approving through the MCP tool (`approve_decision`) also accepts `source_files`: a list of repo-relative files that decision describes. Passing it links the decision to those files right away, instead of waiting for the one-time [`guard anchors`](#commit-time-guard) pass — see [commit-time guard](#commit-time-guard). It only applies to a single decision id at a time — which is the only kind there is: bulk approval (`"all"`, `"*"`, comma-lists) is refused, so decisions are reviewed and approved one at a time.

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
| `contexer reconcile <id> [--team NAME_OR_ID] [--yes]` | Preview a corrected local decision and submit it to the selected team for lead review |
| `contexer share --all [--yes]` | Push every non-ignored decision in this repo (previews the list and confirms first). Global rules are *not* included - use `--global` |
| `contexer share --global [--yes]` | Push your global rules (`~/.contexer/_global.json`) - the ones that apply to every repo. They go up unbound to any repo, so this is the one `share` that works outside a git repository |
| `contexer guard [path…] [--explain]` | Check staged files against approved decisions at commit time — see [commit-time guard](#commit-time-guard) |
| `contexer guard anchors [--list]` | One-time setup linking existing decisions to the files they're about — see [commit-time guard](#commit-time-guard) |
| `contexer status` | Show connection status, store size, current repo; warns about corrupt config files, cleans stale temp files, and notifies when a newer version is on PyPI |
| `contexer reinstall` | Re-sync after an AI assistant update |
| `contexer uninstall` | Disconnect; context store is kept |
| `contexer uninstall --purge` | Remove everything including `~/.contexer/` |
| `contexer version` | Print installed version |
| `contexer help` | Show all commands and flags |

## Commit-time guard

`contexer guard` checks your staged changes against your saved decisions when you commit — decisions you approved (or that came from a repo scan) count; an idea Contexer captured but you haven't looked at yet stays silent.

### Turn it on or off

```bash
contexer guard --install-hook     # wires it into this repo's git commits
contexer guard --uninstall-hook   # removes it
```

Not run automatically by `contexer install` — it's opt-in per repo. Installing is safe on a repo that already has a pre-commit hook: it appends to it rather than replacing it, and running install twice does nothing the second time.

To skip it for a single commit: `CONTEXER_GUARD=0 git commit …`.

Already use the [pre-commit framework](https://pre-commit.com)? `--install-hook` will tell you to use this instead, so add it to `.pre-commit-config.yaml` yourself:

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

(A repo using a hooks manager like husky gets the same message, pointing you at that manager's config instead.)

### What you'll see

Most of the time, a short warning listing the decisions related to what you staged — the commit still goes through:

```
⚠️ Contexer: review this commit against 2 approved decision(s) before proceeding:
  - [a1b2c3d4] All HTTP goes through lib/apiClient — src/fetchUser.js
```

A commit is only ever **blocked** for a rule you explicitly turned into a hard check (see `arm` below).

### Commands

| Command | What it does |
|---|---|
| `contexer guard [path…] [--explain]` | Run the check by hand; `--explain` shows every decision it considered and why each did or didn't fire |
| `contexer guard --dismiss <hash|n>` | Silence one specific warning for good — the `hash` printed next to a warning, or its number from the last run |
| `contexer guard arm <id> --regex '<pattern>' [--flags i] [--paths <glob>] [--message <hint>]` | Make an approved decision block commits when a staged file matches the pattern |
| `contexer guard arm <id> --check secret` | Same, but block on Contexer's own detection of likely secrets instead of a pattern you write |
| `contexer guard disarm <id>` | Turn a decision back from blocking to warning-only |
| `contexer guard list` | List every decision currently set to block |
| `contexer guard anchors [--list]` | One-time setup linking your existing decisions to the files they're about — see below |

Only an **approved** decision can be armed. A violated rule prints the file, the line, and the decision it broke, and stops the commit — fix the file, run `contexer guard disarm <id>`, or skip it once with `CONTEXER_GUARD=0`.

### Approving decisions

When you approve a decision (`contexer review` or the console), you may see a `would anchor: <files>` line under it. That's Contexer proposing to link the decision to the files you were just working on — approving accepts that link, making the guard's warnings for that decision precise and letting Contexer tell you later if those files changed without the decision being revisited. If the suggested files are wrong, edit or skip that part when you approve.

Read that line rather than skimming it.
When you state a rule and then edit a file shortly afterwards, Contexer links the two on timing alone, because a rule that names no file has nothing else to go on.
That is usually right and sometimes is not: a file you happened to touch next can end up linked to a rule that does not govern it, and once linked it drives that decision's commit-time warnings and its staleness notices.
The review block lists such files under the weaker evidence tier, and files it is genuinely unsure about are listed separately as ones it will NOT link.

Approving is also not arming.
It never turns a decision into something that stops a commit; only `contexer guard arm` does that.

### Linking existing decisions to files (`guard anchors`)

Decisions you approved before this linking existed aren't connected to specific files yet. Run this once to catch them up:

```bash
contexer guard anchors --list   # preview only — nothing changes
contexer guard anchors          # walk through and approve each one
```

`--list` shows what it would link, without writing anything. Without `--list`, it walks your decisions one at a time and lets you accept the suggested files, type your own, or skip — nothing is saved until you finish (or quit, which keeps what you'd already approved; Ctrl-C keeps nothing).

### If it fails

The guard fails open: if anything goes wrong internally, or it takes too long, it prints one line and lets your commit through rather than blocking it. `git commit --no-verify` skips it entirely, like any pre-commit hook — it's a local nudge, not a replacement for your CI or PR checks.

## Connecting to a team (Contexer Teams)

Once you have access, joining a team is two commands — nothing to hand-edit:

```bash
contexer install     # local setup
contexer login       # opens your browser to sign in; enables personal cloud and team sync
```

`contexer login` signs you in via the browser (OAuth), stores the credential, and configures the team endpoint for you.
After that your agent automatically pulls the team's approved decisions into every session.
`contexer share [id]` syncs a local decision to personal cloud.
`contexer share --all` pushes every non-ignored decision in the repo, oldest first.
`contexer share --global` pushes your cross-repo rules.
To put corrected wording in front of a team lead, run `contexer reconcile <id> [--team NAME_OR_ID]`.
Contexer fetches the server-authoritative diff before confirmation.
It then atomically syncs the personal revision and creates the lead-reviewed candidate.
If an outage interrupts a confirmed submission, Contexer retries with the same idempotency key.
Changed server heads stop for a fresh preview instead of being blindly retried.
The currently approved team version remains active until the lead approves the candidate.
Older Teams servers fall back to the compatible personal-sync-then-share flow.

When a future enriched team pull reports that a lead changed your linked decision in the web app,
Contexer stores that wording as a local Suggested Update for review.
It never overwrites your standing local revision.
A later `in_sync` pull clears only that team-created proposal and records the reconciliation outcome.
`contexer logout` disconnects.

Logging in or out clears the locally cached team pulls and `✓ shared` markers, so switching accounts never leaves the previous account's decisions being injected into your sessions.
They repopulate from the account you are signed in as at the next session start.

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

- **Capture is best-effort.** Only outright directives ("always/never/don't/create a rule") are auto-stored deterministically. Other decisions depend on the agent choosing to call the store tool, and it does miss things. Hence the *"store that decision"* escape hatch. Contexer also keeps the session signals described [above](#what-contexer-notices-without-being-asked) and proposes review items from them, but that is a second net, not complete capture: it cannot see your agent's answers, test results or diffs, and what it does propose still waits for you.
- **A directive lifted out of its context can be misread as a rule.** A prescriptive line you paste from a log, a traceback, a diff, a changelog, grep output, or a quoted document can be stored as a standing rule, because once the surrounding block is gone it looks exactly like you typing it. Fenced code, pasted blobs and tool-injected text are refused; a single bare line is not. The cost is one item to dismiss in your review queue: such a decision arms no rule, links no file, and cannot block a commit.
- **Deduplication is lexical, not semantic.** Duplicates are detected by token overlap, with no understanding of meaning — the same rule phrased with different words ("commit on approval" vs "commit automatically") can accumulate as separate entries. Containment restatements (re-typing a rule with extra words, or a terse version of it) are consolidated automatically; synonym phrasings are only flagged in the capture ack and surface in review for you to merge manually.
- **Cursor parity is partial.** Cursor's hooks can't inject per-prompt context or restore after compaction; Cursor steering rides on the session-start nudge plus an always-apply rule file. See [integrations](integrations.md).
- **Gemini compression is deferred.** Gemini CLI restores stored context on the next turn after compression, not immediately.
- **Contexer steers, it doesn't enforce.** Agents are told your rules before writing code; your CI and PR gates still verify. The one opt-in exception is the [commit-time guard](#commit-time-guard): a decision you explicitly `guard arm` blocks the commit it violates — but only that decision, only on the machine where it was armed, and `git commit --no-verify` still skips it, so it's a local nudge, not a replacement for CI.
