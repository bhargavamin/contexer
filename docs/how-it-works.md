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

Before editing a file, an assistant can also ask Contexer which of your decisions govern it — decisions linked to that file, or that mention it by name — and get back only those, instead of everything stored. This works for one file or several at once.

## Trust and review

AI-proposed architecture and constraint decisions — and any change to a decision you have already approved — are held for your review instead of being trusted automatically. They are stored, but not replayed into AI sessions until you approve them (`contexer review`).

Approved decisions are versioned: a change never overwrites the previous value — it creates a new revision and the full history is preserved. AI sessions always replay the latest approved revision.

Approving a decision can also link it to the files it describes: pass `source_files` when you approve it, or accept the file suggestion Contexer shows you on the review screen. That link is what makes the commit-time guard's warnings precise, and lets Contexer tell you later if a decision might be outdated because its files changed without it.

## Commit-time guard

Contexer can also check your work at the moment it matters most: right before you commit.

```bash
contexer guard --install-hook
```

wires the check into this repo's commits — not run automatically by `contexer install`, this is opt-in. From there, every commit is checked against your approved decisions — decisions you approved (or that came from a repo scan) count; an idea Contexer captured but you haven't looked at yet stays silent, no matter how confident it sounds.

Most of the time this means a short warning naming the decisions related to what you staged, and the commit goes through. A commit is only ever blocked for a rule you've explicitly turned into a hard check (`contexer guard arm <id> --regex '<pattern>'` or `--check secret`) — nothing else can stop a commit.

### Linking decisions to files

A decision's warnings get sharper once it's linked to the files it's about, and that link is what lets Contexer flag it as possibly outdated when those files change. Links come from two places:

- **A one-time pass** (`contexer guard anchors`) over decisions you approved before this existed — it suggests files from the decision's own text and you confirm, edit, or skip each one.
- **Automatically, going forward.** When you approve a new decision, Contexer proposes the files you were just working on as its link (shown as `would anchor: …` on the review screen), and approving accepts it.

Either way, you always see the files before they're linked — nothing is linked to a decision you haven't reviewed.

If a decision's linked files later disappear, Contexer first checks whether they were simply renamed or moved — if so, it quietly updates the link and moves on. Only when the files are truly gone does it ask you, in your next review, whether the decision still applies. It never removes or changes a decision on its own; you always get the final say.

### Keeping it quiet

- `contexer guard --dismiss <hash>` silences one warning for a specific decision and file, permanently.
- A warning that already fired doesn't repeat on the next commit unless that file's staged content changed again.

### If it fails

Three promises: nothing blocks your commit unless you explicitly armed a rule; if the guard itself breaks or takes too long, it prints one line and your commit goes through anyway; and nothing is sent anywhere — it runs entirely on your machine, the same as everything else in Contexer. `CONTEXER_GUARD=0 git commit …` skips the guard for one commit; `git commit --no-verify` skips it and every other pre-commit hook, same as always. It's a local nudge, not a substitute for CI.

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
