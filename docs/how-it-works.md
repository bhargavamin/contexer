# How Contexer works

Contexer is wired in through two mechanisms: **MCP tools** the agent can call (to store and fetch decisions) and **editor hooks** the host runs around your session (to inject context and capture directives). You work normally; most of it is invisible.

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

## What gets captured

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

## Bootstrap: establishing trusted knowledge

When Contexer is used on a repository for the first time, it analyzes the project — measuring real conventions from configs, source statistics, and git history — and proposes a small set of engineering questions only a human can answer.

Examples:

- Should infrastructure always be deployed with Terraform?
- Is PostgreSQL the standard database?
- Are deployments multi-cloud?

Developers confirm or refine the answers. These approved decisions become the initial engineering knowledge for the repository.

The offer is a numbered list of at most four options, asked as an interactive multiple-choice question where the host has one (Claude Code's `AskUserQuestion`) and as plain numbered text elsewhere. Answer with the number or with the keyword. Which options lead adapts to how well you know the repo, judged from its git history:

- The repo has commits from you → **quick** (one question) and **full** (guided setup) lead, with **scan** kept for "I'm actually new to this repo" (scan plus one short question).
- No commits from your git email (e.g. a freshly cloned project) → **scan** leads: it reads the code and docs to propose decisions, and asks one short question — what you plan to do here — instead of quizzing you on a repo you may not know.
- Can't tell → it simply asks how well you know the repo, and its **scan** row ("I didn't build it, or it's my first time") asks up to two short questions: what you plan to do here, and what the repo does.

The questions in guided setup are asked the same way: one at a time, each ending in an option to skip that one. A question leads with the scan's assumption to confirm only where that assumption actually answers it, and offers alternatives only where there are distinct ones to offer — otherwise it is simply asked openly.

**Resumed sessions** (Claude Code's `--resume` / `--continue`) don't repeat any of this. The context is already in the conversation. If you installed Contexer mid-project, resuming an old session makes the agent mine that conversation for decisions already made and store them, no questions asked.

## At session start

At session start, a hook injects your stored constraints and conventions before you type anything. The injected block looks roughly like this:

```
## Project rules - apply to ALL tasks in this repo:
- [convention] Use uv, not pip, for all dependency management
- [constraint] Never commit untested code - CI blocks merges below coverage
2 architecture decision(s) stored. Call get_context before reading files
for questions about architecture, design, or rationale.
```

Ask about a past decision or rationale ("why did we pick REST?") and Contexer fetches the matching entries automatically, before the agent responds — and shows you a one-line receipt of what it recalled ("Contexer: recalled 2 decisions (db)"), so retrieval is observable, never spooky.

## Trust and review

AI-proposed architecture and constraint decisions — and any change to a decision you have already approved — are held for your review instead of being trusted automatically. They are stored, but not replayed into AI sessions until you approve them (`contexer review`).

Approved decisions are versioned: a change never overwrites the previous value — it creates a new revision and the full history is preserved. AI sessions always replay the latest approved revision.

Approving a decision can also anchor it to the files it describes: pass `source_files` when you approve or edit a decision that's awaiting your review — a brand-new decision's first approval, or approving a Suggested Update on an existing one — and Contexer records those paths plus the current commit. That anchor is what lets the commit-time guard below recognize a staged file as one the decision covers, and lets Contexer flag the decision as possibly stale later if those files change without it. Anchoring applies to one decision at a time (bulk approvals don't anchor). There's no separate re-approve action on a decision that's already active (approved/suggested with no pending update) — approve/edit only apply while a decision is still awaiting review, and it's on that transition that an already-anchored entry gets `anchor_commit` refreshed to the current commit, clearing any stale flag.

## Commit-time guard

Contexer can also check your work at the moment it matters most: right before you commit.

```bash
contexer guard --install-hook
```

wires `contexer guard` into `.git/hooks/pre-commit` for the current repo — not run automatically by `contexer install`, this is opt-in. From there, every commit runs the guard against the staged files.

### Two tiers

**Tier 1 — advisory, always exits 0.** The guard pairs each staged file against decisions that are approved *and* trusted-provenance — human-reviewed, measured by a scan, set up at bootstrap, or promoted from an approved plan, never an AI-proposed decision no one has looked at. (A decision stored before Contexer tracked provenance is judged by who originally created it, so older trusted decisions aren't excluded.) A pair fires on either of two signals: the staged file is one the decision was anchored to when it was captured (`source_files`), or a file path or dotted module name inside the decision's own content matches the staged path. That match has to be a real path: an exact relative path, a dotted module mapped onto its file (`contexer.store` → `contexer/store.py`), or a multi-segment suffix at a `/` boundary — a bare filename like `utils.py` never matches on its own, so a decision that happens to mention a common filename doesn't fire on every file in the repo sharing that name. Decisions stored globally (across repos) don't carry `source_files`, so they only pair through the content-match path. This is commit-time visibility, not a gate: advisories print and the commit proceeds.

**Tier 2 — blocking, exits 1.** Nothing blocks a commit unless you explicitly arm it: `contexer guard arm <id> --regex '<pattern>'` or `--check secret` turns an *already-approved* decision into a machine-checkable rule. A staged file matching the regex (or Contexer's own high-confidence secret patterns) fails the commit, printing the file, line, and decision it violates. Arming is deliberate and reviewed the same way approval is — you can't arm a decision nobody has looked at, and a decision later marked `ignored` stops blocking automatically, no separate disarm required.

### Anchoring decisions after the fact

Tier 1 can only pair a staged file against a decision that's anchored to it. A decision approved with `source_files` (above) picks up that anchor the moment it's trusted — but decisions that predate anchoring, or were captured before you named any files, sit permanently invisible to the guard until something anchors them. Two things close that gap, one for the decisions already sitting in your store and one for new ones going forward:

- **The decisions you already have.** `contexer guard anchors --list` looks at every trusted, currently-unanchored decision in your store, reads its own stored text for file and module names it mentions, and shows you the ones that still exist in your working tree — a read-only preview, nothing written. Run `contexer guard anchors` without `--list` (needs a real terminal) to walk that list interactively and choose, per decision, whether to accept the suggested files, type your own, or skip it; your choices are applied in one batch when you're done. See the [CLI reference](usage.md#anchor-backfill) for the full walkthrough.
- **The decisions you capture from here on.** When your agent stores a decision without telling Contexer which files it's about, Contexer quietly notes the files you were actually editing in that session as a *candidate* anchor — not a real one. It sits inert: the guard never pairs against a candidate, only a confirmed anchor. The candidate surfaces the next time you review that decision (`contexer review` prints a `would anchor: …` line under it), so you see exactly what will happen before you approve. The moment you do approve it, the candidate becomes a real anchor, the same way `source_files` passed by hand would.

Either way, the rule is the same: **no anchor ever becomes guard input without you having seen the file(s) it names at the moment you signed off on it.** A backfill candidate is shown and confirmed one decision at a time before anything is written; a capture-time candidate is shown alongside the decision you're already reviewing, before you approve it. A decision still awaiting your review is never anchored on its own — the candidate just waits.

### Keeping it quiet

Two mechanisms keep the advisory tier from becoming noise:

- **A permanent dismissal.** `contexer guard --dismiss <hash>` retires one (decision, file) pair for good. The dismissal is keyed on the decision and the file, never the file's content or a line number, so ordinarily editing the file afterward doesn't resurrect it.
- **A content-keyed throttle.** A pair that already surfaced doesn't surface again on the next commit unless the staged file's content actually changed since. The throttle hashes the whole staged file, so any edit to it counts — but committing it unchanged again never re-triggers the same advisory.

### Fail-open, always

The guard is read-only against your decision store — it never writes or approves anything on its own — and is built so its own failure can never block your commit: a single wall-clock time budget covers the whole check — both the blocking rules and the advisory pairing, from the first staged file to the last decision considered — and any internal error, or a run that exceeds that budget in either half, returns nothing rather than raising, which the CLI turns into a single line on stderr and a clean exit. `CONTEXER_GUARD=0 git commit …` bypasses the guard for one commit; `git commit --no-verify` bypasses any pre-commit hook, guard included, same as it always has. Neither the guard nor any pre-commit hook is a substitute for CI — a developer's own machine is never the enforcement backstop.

Already use the pre-commit framework? Add Contexer's check via `.pre-commit-config.yaml` instead of `--install-hook` — see the [CLI reference](usage.md#commit-time-guard).

## Deduplication

**Deduplication is not an LLM call.** Before storing, Contexer checks token overlap against existing decisions. Over 70% overlap is treated as a duplicate and silently dropped. It's deterministic, costs no tokens, and is why you can "over-call" store without bloating anything.

## Cost

Contexer's cost is fixed and predictable: roughly **26 tokens per rule** at session start, paid only for constraints and conventions. Architecture and pattern decisions cost nothing until something actually needs them.

| Pre-loaded rules | Approx. tokens at session start |
|---|---|
| 5 | ~125 |
| 10 | ~250 |
| 25 | ~625 |

Paid once per session. Every later prompt adds nothing. On prompts unrelated to anything stored, Contexer skips entirely: no read, no tokens. Store lookups run before the response is generated and cost low single-digit milliseconds even at the 500-decision store cap (sub-millisecond at typical store sizes), so they add nothing perceptible to response time.

The point isn't token compression. It's **eliminated rework across sessions**. The recurring, unpredictable cost of re-explaining rules and correcting re-introduced patterns is replaced by a small, flat, session-start cost.

## Privacy

Everything lives as plain JSON in `~/.contexer/` on your own machine. Nothing about your code or decisions leaves your machine. The only network call Contexer makes is an optional version check against PyPI (`CONTEXER_NO_UPDATE_CHECK=1` disables it). Team sync is strictly opt-in and every outward push previews what would leave your machine first.

## Why it stays lightweight

Contexer is a single Python MCP server with a plain JSON store. No background worker. No vector database. No port listening. No infrastructure to maintain.

This is intentional. Every piece of complexity added to a decision store is a piece of complexity that can fail, drift, or accumulate noise. Contexer stores only what matters (engineering decisions) and keeps everything inspectable, auditable, and greppable.
