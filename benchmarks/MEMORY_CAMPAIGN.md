# Memory Tool vs Contexer: Live Campaign Runbook

The ordered commands to execute the memory-vs-Contexer benchmark campaign, written
so a future session can run it cold. The harness itself (`benchmarks/memory_campaign.py`,
`benchmarks/memory_tasks.json`, `benchmarks/teaching.json`, `benchmarks/memory_home.py`,
`benchmarks/validate.py`, `benchmarks/report.py`) already exists; this document is
only the sequence to run it and the checklist to publish what it produces.

## COST WARNING: read before running steps 2 or 3

**All live benchmark spend in this repo is frozen.** Steps 2 and 3 below call
`claude` with a real API key and spend real tokens. Before running either one:

1. Compute the expected session count and cost for the exact run you are about
   to make (see the worked estimate under each step).
2. Quote that exact expected cost to the developer in the same turn.
3. Get fresh, explicit developer approval for that specific run. Approval from
   a previous campaign does not carry over. A standing "yes" is not fresh consent.

Step 1 (the stub pipeline check) spends no tokens and needs no approval. Do
not skip straight to step 2 or 3 "to save time": the whole point of step 1 is
to catch a broken harness before it burns approved budget.

## Prerequisites

### Pilot findings (Task 1)

`benchmarks/MEMORY_PILOT.md` recorded the pilot run against `claude` version
`2.1.226`. The load-bearing findings for this runbook:

- **The memory tool is default-on.** With a completely empty throwaway `HOME`
  (no `settings.json` at all), a session still writes memory files under
  `<home>/.claude/projects/<slug>/memory/`. No configuration is needed to
  activate it.
- **There is no disable key.** No `settings.json` key, documented or
  undocumented, turns the memory tool off while leaving everything else on.
  `--bare` disables it, but also disables hooks, plugins, and keychain auth,
  which would contaminate the comparison by turning off things the campaign
  needs left on.
- **Consequence: the `with` arm sweeps, it does not disable.**
  `write_home_settings(memory_enabled=False)` cannot stop the memory tool from
  writing, so the runner does not call it at all (calling it after
  `contexer install` used to overwrite the installed hooks). Instead, after
  every `with`-arm session the harness records how many memory files that
  session wrote (`memory_leak_files`) and deletes the memory directory, before
  the next session and before the post-teaching snapshot. That prevents both
  cross-session leakage and Contexer's own `sync_memory` importing the
  opponent's captures. `contaminated` is then measured **before** each `with`
  session (anything present survived a sweep) and after each `memory` session
  (a `.contexer` store where Contexer was never installed).
  `benchmarks/validate.py` fails the whole campaign
  (`_check_memory_isolation`) if any measured, non-enforcement row came back
  contaminated. A contaminated row is not downweighted or footnoted, it is a
  validator failure that must be fixed (usually by re-running), not published
  around.
- **Residual, disclosed:** because `contaminated` is read before the `with`
  session runs, a memory file that session writes *and then reads back within
  the same session* is invisible to it (the sweep deletes it afterwards, so no
  later session sees it). Publication should state that the `with` arm's
  isolation is cross-session, not within-session; `memory_leak_files` is the
  per-session upper bound on how much that could ever have been.

### Pre-registered: what carries the headline

- **The POOLED cell is the headline claim.** `tier` is derived from rep parity,
  so a 16-rep campaign puts 8 reps in each per-tier cell. Every headline table
  (`sup-current`, `cont-log`) therefore renders a third column, `pooled
  (headline)`, combining both phrasing tiers with its own Wilson interval.
  Public claims cite the pooled cell. The per-tier cells are the
  **phrasing-sensitivity check**: they exist to show whether capture and recall
  survive an implicit phrasing as well as an explicit one, and they are cited
  as such, never as the headline number.
- **Teaching runs one session per scripted prompt.** Joining a session's
  prompts into one invocation pushed them past `store._MAX_DIRECTIVE_LEN` (300
  chars), so Contexer's constraint-capture path rejected them outright and the
  taught rule never landed as an approved decision. The capture-rate stat would
  have measured the harness's own prompt joining. Rows are one per prompt,
  distinguished by `prompt_index`.

### Frozen-scripts rule

`teaching.json` and `memory_tasks.json` are the fixed instrument the campaign
measures against. `run_memory_campaign` writes their sha256 into
`campaign.json` as soon as a run starts (`teaching_frozen_sha`,
`memory_tasks_sha`). Once a live campaign has used a given version of these
files:

- Do not edit `teaching.json` or `memory_tasks.json` and re-run into the same
  `--out` directory. The recorded hash in that campaign's `campaign.json` is
  what makes the run auditable: a silent edit after the fact breaks the
  guarantee that every row in `runs.jsonl` was measured against the content
  the hash names.
- If the teaching script or task set genuinely needs to change, start a new
  campaign under a new `--out` directory (a new campaign id). The old
  campaign's artifacts stay valid for what they measured; they are not
  retroactively rewritten.

### One model per campaign

`memory_campaign.py`'s `--model` flag defaults to `""`. Running without
`--model` prints a warning to stderr and lets rows record whatever model the
`claude` binary defaults to. Always pass `--model` explicitly for any run
whose output you intend to keep: both `benchmarks/report.py` and
`benchmarks/validate.py` raise or fail on a campaign that mixes models, and an
unpinned run is the easiest way to end up with one by accident (a CLI default
model change between sessions).

## Steps

### Step 0: prerequisites and approval

Confirm `benchmarks/MEMORY_PILOT.md` exists and has been read (it has, as of
this writing). Confirm `teaching.json` and `memory_tasks.json` are in the
state you intend to measure, since step 2 or 3 freezes their hash the moment
either runs. Do not proceed to step 2 or 3 without the cost quote and fresh
developer approval described above.

### Step 1: free stub-only pipeline check (no tokens)

Runs the harness against stub sessions. Confirms the campaign plumbing,
scoring, and isolation logic work before any real API call happens. Free,
needs no approval, run it first every time:

```bash
uv run pytest tests/test_bench_*.py -q --no-cov
```

Deliberately the whole `test_bench_*` family, not a `memory_*` glob: the
campaign's scorer (`test_bench_sup_scorer.py`, `test_bench_score.py`), its
report renderer (`test_bench_report.py`, `test_bench_wilson.py`), its frozen
teaching script (`test_bench_teaching.py`), and its capture stat
(`test_bench_capture.py`) all sit outside that glob, and step 3 runs every one
of those paths. A narrower selection can pass while the reporting path is
broken — exactly the failure step 1 exists to catch before money is spent.
All of them must pass before spending a single token in step 2.

### Step 2: smoke campaign (1 rep, real API, all four tasks)

One rep covers a single tier (implicit or explicit, chosen by `rep % 2`) and
all three arms. Teaching runs one session per scripted prompt, and the implicit
and explicit tiers both have 4 prompts in teaching session 1 plus 1 in session
2, so per rep the session count is: `without` = 4 measure sessions (no
teaching), `memory` = 5 teach + 4 measure = 9, `with` = 5 teach + 4 measure =
9. One rep is 22 sessions total.

**Cost quote to give the developer before running:** 22 sessions at this
repo's last observed per-session range of roughly $0.04-$0.12 (`docs/benchmark.md`'s
cost table) is a ballpark of $0.9-$2.7. Teach sessions are single short prompts
and should land at the low end. Recompute against current model pricing before
quoting; this is an estimation method, not a cached number to reuse blindly.

Once approved:

```bash
uv run python -m benchmarks.memory_campaign --reps 1 --model claude-sonnet-5 \
  --out benchmarks/artifacts/memcamp-smoke
uv run python -m benchmarks.validate benchmarks/artifacts/memcamp-smoke
```

If validation fails here, stop. Do not proceed to the full campaign on a
harness that is failing its own smoke test.

### Step 3: full campaign (16 reps, 8 per tier)

16 reps alternate implicit/explicit by `rep % 2`, giving 8 reps per tier and a
pooled headline cell of n=16 per arm per headline task.
Session count: 16 reps x 22 sessions/rep = 352 sessions.

**Cost quote to give the developer before running:** 352 sessions at the same
$0.04-$0.12 per-session range is a ballpark of $14-$42. As with step 2,
recompute before quoting rather than reusing this figure verbatim; teach
sessions and the enforcement task's guard setup can shift the real per-session
cost away from the plain measure-task baseline this range was drawn from.

Once approved:

```bash
uv run python -m benchmarks.memory_campaign --reps 16 --model claude-sonnet-5 \
  --out benchmarks/artifacts/memcamp1
uv run python -m benchmarks.report benchmarks/artifacts/memcamp1/runs.jsonl
uv run python -m benchmarks.validate benchmarks/artifacts/memcamp1
```

Run the report before the validator: the report is what you read to decide
whether the campaign is worth publishing, the validator is what proves the
numbers in it are real.

## Reading the results

- **Headline cells carry Wilson intervals, not bare success rates.** The
  `sup-current` and `cont-log` tables render each arm/tier cell as
  `k/n (lo-hi)`, plus a `pooled (headline)` cell over both tiers. A claim about
  which arm performed better must not exceed what the interval supports:
  overlapping intervals mean "no distinguishable difference at this sample
  size," not "roughly equal, call it a tie in our favor." Read the interval,
  not just the point estimate, and cite the pooled cell (see the
  pre-registration above) rather than whichever tier looks better.
- **`sup-current` reviews are reported, never counted as losses.** The scorer
  has three verdicts: `pass`, `fail`, and `review` (the answer's first line
  named neither database, or named both). A cell renders as
  `k/n (lo-hi), R review`, where **n = pass + fail only**. A review is "not yet
  scored", not a loss, so folding it into the denominator would be a silent
  failure vote. The bias direction is why this matters: a hedged answer is
  exactly what an arm holding two contradictory statements with no supersession
  structure produces, so counting reviews as failures would flatter Contexer on
  the one task built to measure that difference. Reviews are never dropped
  either, the count is always shown next to the interval.
- **`memory_leak_files` is part of the disclosure, not a defect count.** Each
  `with`-arm row records how many memory files that session wrote before the
  harness swept them. The `with` arm could not disable the opponent's
  mechanism, only delete its artifacts between sessions, and that residual
  asymmetry must be stated in any published claim (see
  `MEMORY_PILOT.md`). A `with`-arm `memory_files` median of 0 in the capture
  table is a consequence of the sweep, not evidence that the memory tool
  captured nothing.
- **`cont-log` scores the taught rule, and a no-op cannot pass.** Success needs
  all three: the session changed at least one file, none of the changed content
  matches the request-data-logging pattern (the same pattern the enforcement
  task arms the guard with), and the test suite is green. A session that edited
  nothing is recorded as `cont-log: no files changed` with `success=False`,
  never as compliance.
- **`enf-commit` is a mechanism demonstration, never an aggregate statistic.**
  The report lists each `with`-arm run as `blocked`, `committed`,
  `committed on retry (guard did not block)`, or
  `no violating change attempted` (or its error), and every other arm as
  `no mechanism`, one line per run, with no
  wilson interval and no median. It demonstrates that the commit-time guard
  can block a violation when armed; it is not evidence about how often Claude
  attempts the violation, and must not be folded into a success-rate table
  alongside `sup-current` or `cont-log`. **`blocked` is an observed rejection,
  never an inference.** A run only earns it if its violating edit reached the
  git index AND the harness then attempted the commit itself and git exited
  non-zero (stderr kept in `enf_detail`). Absence of a commit is not evidence:
  a session can stage the violation and simply never run `git commit`, and a
  commit can fail for reasons that have nothing to do with the guard — which
  is why `fixtures/generate.py` now writes a repo-local git identity, so a
  throwaway HOME with no `.gitconfig` cannot produce a fake block. A model that
  declined to write the log line demonstrates nothing about the guard either,
  and reads as `no violating change attempted`.
- **Capture rate is reported per phrasing tier.** The "Capture rate
  (post-teaching)" table breaks out `memory_files` and `contexer_entries`
  medians separately for the implicit and explicit teaching tiers, per arm.
  Do not collapse the two tiers into one number when citing this table: the
  whole point of alternating tiers by rep is to see whether capture survives
  an implicit ("by the way, remember...") phrasing as well as an explicit one.
- **Validator medians cover measured rows only.** For a memory campaign,
  `validate.py` recomputes its medians over `phase == "measure"` rows excluding
  `kind == "enforcement"`, matching what the report aggregates. Teach rows
  (`success` always False, and their own token cost) and enforcement rows
  (`success` hardcoded True for the `with` arm) would otherwise be mixed into
  the very medians this document publishes. Legacy campaigns are unaffected.
- **A contaminated row is a validator failure, not a footnote.** `validate.py`'s
  `_check_memory_isolation` fails the run if any measured, non-enforcement row
  has `contaminated=True`. Do not report medians from a campaign whose
  validator did not pass. If contamination shows up, the affected cell (not
  just the affected row) is compromised: re-run the campaign rather than
  hand-editing the contaminated rows out of `runs.jsonl`.

## Publication checklist

Follow this order. Do not skip ahead.

1. **The validator is green.** `python -m benchmarks.validate <dir>` exits 0
   with no failures for the campaign you intend to publish. Warnings are worth
   reading (they can point at a real confound) but do not by themselves block
   publication; failures do.
2. **Add new fine-print items to `docs/benchmark.md`**, matching the existing
   numbered fine-print style. Wording must be opponent-precise and
   build-versioned: say "Claude Code's built-in memory tool at version
   2.1.226," never "memory tools" in the plural or unqualified. This campaign
   measures exactly one opponent at one pinned build. A claim about memory
   tools in general (plural, or naming a different assistant's built-in
   memory) requires Phase 2's third-party MCP campaign, not this one.
3. **State the scope-honesty sentence.** This campaign measures
   engineering-decision recall and rule compliance on synthetic repos, not
   general memory quality. Say that plainly near the new fine-print items, the
   same way `docs/benchmark.md`'s existing "Scope" item already scopes the
   Contexer-vs-CLAUDE.md comparison.
4. **Only then touch README or marketing copy.** A README or landing-page
   change referencing this campaign comes last, after the fine-print items and
   scope sentence exist in `docs/benchmark.md`, never before them or in the
   same step.
