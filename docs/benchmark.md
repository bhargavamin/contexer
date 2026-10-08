# The Contexer Benchmark

We ran 1,790 published AI coding sessions (plus discarded smoke and failed runs) measuring what Contexer costs and what it returns, against every common alternative. **Every session that backs a claim on this page ships as a raw row in `benchmarks/artifacts/`**, so every number below can be recomputed. (Sessions from discarded campaigns, such as harness bugs or network failures, are excluded from all claims and not published.) Everything was scored by code, not by an AI judge, and re-checked by an independent validator.

There are two studies. **The adoption benchmark (October 2026)** is the current one: it asks whether a team should use Contexer instead of a rules file. **The earlier study (July 2026)** measured an older release against having no memory at all; it is kept below for the record.

**Words used on this page:**

- A **session** is one conversation with an AI coding assistant, start to finish.
- A **rule** (or decision) is something your team decided that the code alone doesn't tell you: "we never use threads in this service", "always write files atomically".
- **Tokens** are the units AI providers bill for. Fewer tokens for the same result means less money and less waiting.

## How the results have evolved

**Same tasks, two releases (directly comparable).** The October tasks were run on the previous build and again on v0.50.1, with the static-file results unchanged:

| Contexer on the October tasks | v0.49.x (Oct 3) | v0.50.1 (Oct 8) | Best static file |
|---|---|---|---|
| Tasks right, 34 rules | 105 / 114 | **108 / 114** | 111 (indexed docs) |
| Tasks right, 150 rules | 94 / 114 | **109 / 114** | 108 (both) |
| Contradicting rules asked, 34 rules | 12 / 12 | 12 / 12 | 9 / 12 |
| Contradicting rules asked, 150 rules | not run on fixed tasks | **12 / 12** | 7 / 12 |
| Cost per session, 150 rules | $0.054 | $0.076 | $0.103 |

The 150-rule gain came mainly from fixing startup context that was over the host's inline limit and was being cut to a short preview, so most rules never reached the agent. The fix also explains why cost per session rose: the agent now actually reads that context.

**Two studies, different questions (not directly comparable).** Model, tasks and scoring differ, so read this as how the question moved, not as a trend line:

| | July 2026 (v0.20.0) | October 2026 (v0.50.1) |
|---|---|---|
| Model | Claude Sonnet 5 | Claude Sonnet 5.5 |
| Question | Do stored decisions beat having none? | Should a team use Contexer instead of a rules file? |
| Compared against | Nothing stored; a complete hand-written CLAUDE.md | No rules; a full CLAUDE.md; one-file-per-rule docs; at 34 and 150 rules |
| Against nothing stored | 6× fewer tokens, right answers instead of guesses | 109 vs 66 tasks right (150 rules) |
| Against a complete CLAUDE.md | Tie on cost and accuracy (a small set of stored decisions) | Tie on accuracy; 26–30% cheaper at 150 rules |
| New in October | | Contradicting rules: 12/12 asked vs 7/12; capture during work measured (6/24) |

The July headline, "a complete CLAUDE.md ties Contexer", still holds for small rulebooks. What October adds is scale: as the rulebook grows, a rules file's cost grows with it, while Contexer's grows much less, and contradictions between rules become something only Contexer flags.

## The adoption benchmark (October 2026, Contexer v0.50.1)

### What we tested

We gave an AI coding agent (Claude Sonnet 5.5) **38 small coding tasks** in a test repository. Most tasks only come out right if the agent knows a team rule it cannot work out from the code. Each task ran **3 times**, with the rules written in a different style each time (short, a long story, a step-by-step plan): **114 runs per approach**. Code decides each result: did the change work, and did it follow the rule?

We compared four ways of giving the agent the team's rules:

| Approach | What the agent gets |
|---|---|
| **No rules** | Just the code |
| **Full CLAUDE.md** | Every rule in one file, loaded in every session |
| **Indexed docs** | One file per rule, plus a list of titles the agent can open |
| **Contexer** | Rules stored in Contexer, which hands the agent the relevant ones at session start and with each prompt |

We ran it with **34 rules** (a small team) and **150 rules** (a bigger, older project). The tasks cover eight situations: knowledge visible in the code, in the repo's docs, nowhere in the repo, code that points the wrong way, an obvious approach the team rejected, tasks that need no rule (to catch harm from noise), a rule replaced by a newer one, and two current rules that contradict each other.

### Result 1: written-down rules matter a lot

With no rules, the agent got **66 of 114** tasks right. With the rules, by any approach, it got **106 to 111**. Writing decisions down and giving them to the agent clearly works.

### Result 2: Contexer matches the static files on accuracy

| Tasks right (of 114) | No rules | Full CLAUDE.md | Indexed docs | Contexer |
|---|---|---|---|---|
| 34 rules | 66 | 106 | 111 | 108 |
| 150 rules | 66 | 108 | 108 | 109 |

A difference of 1 to 3 tasks out of 114 is within noise, so the honest summary is **about equal**, not "Contexer is better". On the previous build (v0.49.x), before fixes that stopped long startup context being cut short by the host, Contexer scored 105 and 94 (the 94 on the original, unfixed conflict tasks); those fixes are why we reran it.

### Result 3: Contexer costs less, and the gap grows with the rulebook

| Cost per session | Full CLAUDE.md | Indexed docs | Contexer |
|---|---|---|---|
| 34 rules | $0.080 | $0.075 | **$0.066** |
| 150 rules | $0.109 | $0.103 | **$0.076** |
| Growth, 34 to 150 rules | +36% | +37% | **+15%** |

A rules file sends every rule in every session, so its cost grows with the rulebook. Contexer sends mainly the rules that look relevant. At 150 rules it is **26 to 30% cheaper**. (No rules at all: $0.045, with far worse results.) Costs are total spend divided by sessions; medians differ by under a cent.

### Result 4: when rules contradict, Contexer asks instead of guessing

Some tasks carry **two current rules that contradict each other**, for example "prefix versions with v" against "publish bare versions". The right behaviour is to stop and ask which applies. A session counts only if its final message names both rules and its code takes neither side.

| Asked before coding (of 12) | Full CLAUDE.md | Indexed docs | Contexer |
|---|---|---|---|
| 34 rules | 5 | 9 | **12** |
| 150 rules | 7 | 7 | **12** |

Contexer marks contradicting rules explicitly, so the agent caught every one. With static files it often implemented one side. On the version conflict, both static approaches asked in 0 of 3 runs at 150 rules. This is Contexer's clearest advantage; each cell is only 12 runs.

### Result 5: when Contexer delivers a rule, the agent follows it

In every run where the needed rule reached the agent (116 runs across both sizes), the task succeeded. **Every Contexer failure was a rule that never arrived.** The persistent case: a task saying "finish as quickly as possible" needs the rule "no threads or async"; the two share no words, so Contexer never connects them, and that task failed in all 6 runs ([#390](https://github.com/bhargavamin/contexer/issues/390)).

### What this does not show (yet)

- **Capturing rules during work.** Above, the rules were put into each store in advance. In a separate test (144 sessions), a rule a developer stated in one session was needed in the next: Contexer recorded it in **6 of 24** runs, while a CLAUDE.md the agent was told to maintain recorded **24 of 24**. Agents rarely save decisions on their own, and prompt capture recognised only some phrasings ([#385](https://github.com/bhargavamin/contexer/issues/385)). This is Contexer's biggest current weakness.
- **Real company repositories.** These are synthetic tasks in one fixture repo; real repos have different rule wording and file layouts.
- **Same-day runs.** The Contexer v0.50.1 rows were run on 2026-10-08; the static-file and no-rules rows on 2026-10-03. Same model, tasks and checks; the Claude Code CLI version was recorded only for the later runs (2.1.292).
- **Team mode** is not benchmarked.

### Raw data

| Directory | What | Sessions |
|---|---|---|
| `adoption-38-phase1` | All four approaches, 34 rules (Contexer row superseded by `adoption-38-0501`) | 456 |
| `adoption-150-phase3` | Full CLAUDE.md, indexed docs, Contexer, 150 rules (Contexer row superseded) | 342 |
| `adoption-38-k7c-fixed` | Contradicting-rule tasks after a fixture fix, 34 rules | 36 |
| `adoption-38-0501`, `adoption-150-0501` | Contexer v0.50.1, both sizes | 228 |
| `adoption-150-k7c-0501` | Contradicting-rule tasks, 150 rules, three approaches | 36 |
| `capture-phase2b` | Capture: rule stated in session 1, needed in session 2 | 144 |

The contradicting-rule tasks were fixed after the first two campaigns (two of them did not really conflict), so every total above uses the fixed tasks: the first two campaigns' rows for those four tasks are replaced by `adoption-38-k7c-fixed` and `adoption-150-k7c-0501`. Each directory has `validation.md` from `benchmarks/validate.py`; all pass. Task definitions: `benchmarks/adoption_tasks.json`, `adoption_tasks_150.json`, `capture_tasks.json`.

## Earlier study (July 2026, Contexer v0.20.0)

Contexer against nothing stored and against a hand-written instructions file, on questions about past decisions and on seeded rules. 548 sessions.

### What it costs

Same repo, same model (Claude Sonnet 5), same questions about past project decisions. The only thing we changed is the memory setup. Each row is the median of the sessions measured for it:

| Memory setup | Sessions measured | Tokens per session | Cost per session | Right answer? |
|---|---|---|---|---|
| Nothing | 24 | 198,864 | $0.116 | **No** — guesses or gives up |
| Hand-written, up-to-date AGENTS.md | 3 | 131,184 | $0.085 | Yes |
| Hand-written, up-to-date CLAUDE.md | 24 | 32,430 | $0.042 | Yes |
| Hand-written, up-to-date CLAUDE.md + AGENTS.md | 3 | 32,445 | $0.043 | Yes |
| **Contexer** (v0.20.0) | 12 | 32,804 | $0.043 | Yes |

Read the last column first: **with no memory, you pay the most and get wrong answers.** The AI burns six times the tokens searching the code for a decision that was never written down, then guesses or admits it can't know.

Then read the cost column: **Contexer costs the same as a perfect CLAUDE.md.** Not cheaper — the same. A complete, up-to-date instructions file is genuinely as token-efficient as Contexer. That is the honest headline, and it leads to the real question: who keeps that file perfect?

*(Fewer sessions were measured for AGENTS.md — treat those two rows as indicative, not final.)*

The cost table compares Contexer to a CLAUDE.md that is **complete and current** — but files start incomplete and go stale, and that difference is what you're actually buying. See **[What Contexer gives you that a .md file can't](../README.md#what-contexer-gives-you-that-a-md-file-cant)** in the README.

### The fine print

For readers who work with tokens, turns, and medians. Numbers are Sonnet 5 unless stated; Opus 4.8 replicated the accuracy and compliance results.

1. **Rationale recall:** with the decision stored, "why did we choose Postgres over MySQL?" is answered correctly in 1 turn / ~33k tokens; without memory: 6 turns / ~165k tokens and no answer. 16 runs per condition, stable under three question rewordings.
2. **Rule compliance:** a seeded rule ("never log request data") was followed 8/8 by every memory condition; bare sessions violated it most of the time (Opus: 0/8 compliant). Contexer was the cheapest compliant setup (~68k tokens vs ~250k for the same rule via CLAUDE.md).
3. **A complete CLAUDE.md ties Contexer on static recall** — identical accuracy and compliance at equal cost. Contexer's edge is that nobody has to write or maintain the file. Layering Contexer on an existing CLAUDE.md caused no harm and no single-shot gain.
4. **AGENTS.md is honored but expensive** — the file is found and read rather than auto-loaded (131k vs 33k tokens on the same question). Small sample (n=3), directional.
5. **Overhead when nothing needs remembering — measured on v0.20.0:** 30 sessions, bare vs Contexer, five editing tasks where no stored decision is relevant. What would have falsified "the overhead is fixed": Contexer costing meaningfully more than bare. Result: **+2.6% median tokens (+7.9% cost)** — down from the +12–17% measured through v0.19.0; improved, not eliminated. The spread matters more than the median: injected conventions change the agent's *behavior* in both directions. Where knowledge replaced discovery it got cheaper than bare (−27% on one task); where the repo's standards applied, it did compliance work bare agents skip — running the full test suite, enforcing the line-length limit, deduplicating test data — at +43–47% on two tasks (every Contexer run above every bare run there). Accuracy and compliance were identical on both arms, and the independent validator confirmed the extremes are task-driven, not noise. Also still true: an earlier "gets cheaper the more sessions you chain" effect did not hold up at a proper sample size. We publish what disappears under scrutiny, not just what survives it.
6. **v0.20.0 vs v0.19.0 (retrieval engine A/B):** 48 interleaved sessions, identical everything except the installed Contexer release. Accuracy and compliance identical; v0.20.0 used **−11.7% median tokens (−8% cost)** overall, −12% to −14.5% on cross-session chain tasks (every v0.20.0 run cheaper than every v0.19.0 run in those cells), and added **no overhead** on the editing task with nothing to retrieve. Wrinkle: 2 of 4 v0.20.0 compliance runs spent extra tokens flagging a conflict with the stored rule before implementing — costlier, arguably better. **Paraphrase check:** all six reworded variants of the recall tasks score identically on both engines, so no engine conclusion hinges on prompt wording — and under one rewording the v0.19.0 engine failed to retrieve at all and spent 170k tokens exploring while v0.20.0 injected normally at 33k. Retrieval that survives rewording is precisely what v0.20.0's retrieval work added. (Attribution note: the A/B compares the two releases, whose entire code difference is the retrieval feature set — engine, session integration, recall notice — so deltas belong to that work as a whole, not provably to the BM25 ranker alone. Variant cells are single sessions: directional.)
7. **Recall-notice savings figure:** the `· ~N tokens saved` estimate in the recall notice is `injected estimate × 3`, from a golden multiplier of 4 — the median without÷with session-token ratio across the 10 recall-event task-cells (rationale + continuity) in campaigns 3, 4-sonnet, 4-opus, and 5 (median 4.13, mean 3.98; per-cell range 1.2×–6.1×). Cells where no recall fires (editing/convention tasks, ratios as low as 0.5×) are excluded because the notice never appears there. Re-derived manually whenever a new campaign lands.
8. **Scope:** personal, single-developer sessions on synthetic repos with a pinned model. Team mode has not been benchmarked; this page makes no claims about it.

## How we measured

- **Isolation:** every session runs in a throwaway `HOME` on a fresh copy of a synthetic fixture repo (which cannot exist in any model's training data), with an environment allowlist. Contexer is installed by its real installer, so real hooks are exercised.
- **No ordering tricks:** conditions alternate in time and every row is timestamped; the validator flags any condition that ran as a contiguous block.
- **Scored by code, not opinion:** answers must contain the stored facts; written code is AST-checked against measured conventions; tasks pass or fail by their own test commands. No LLM judge.
- **Checked twice, then attacked:** an independent validator recomputes every statistic from raw rows and hunts anomalies (zero-token "successes", error asymmetries); failed sessions are recorded and excluded, never zeroed. An adversarial review tries to refute each claim before publication — it's why the CLAUDE.md comparison exists at all.
- **Reworded prompts:** key questions run in paraphrased variants with identical stored knowledge, so no conclusion hinges on one phrasing.

## Reproduce it

```bash
# free end-to-end pipeline check (stub sessions, no tokens)
uv run pytest tests/test_bench_*.py -q --no-cov

# a live campaign (spends real API tokens — start small)
uv run python -m benchmarks.run --reps 1 --tasks rat-storage,conv-endpoint \
  --model claude-sonnet-5 --out benchmarks/artifacts/mine
uv run python -m benchmarks.report benchmarks/artifacts/mine/runs.jsonl
uv run python -m benchmarks.validate benchmarks/artifacts/mine

# A/B two Contexer versions (how fine-print item 6 was produced): each condition
# installs Contexer from its own checkout into the session's isolated HOME
uv run python -m benchmarks.run --reps 4 --tasks rat-storage,rat-errors,cont-logging,conv-endpoint,chain-1-cache,chain-2-list \
  --model claude-sonnet-5 --conditions contexer_pre_v1,contexer_v1 \
  --contexer-sources "contexer_pre_v1=/path/to/old-checkout,contexer_v1=." \
  --out benchmarks/artifacts/my-ab

# decision-dependent tasks (a stored decision changes the right answer); free offline
# delivery preview first — full runbook: benchmarks/RETRIEVAL_CAMPAIGN.md
uv run --frozen python benchmarks/replay_delivery.py tasks

# the adoption benchmark — runbooks: benchmarks/ADOPTION_CAMPAIGN.md, benchmarks/CAPTURE_CAMPAIGN.md
uv run python -m benchmarks.run --tasks-file benchmarks/adoption_tasks.json --steady-state \
  --conditions without,claudemd_full,docs_indexed,with \
  --model claude-sonnet-5-5 --reps 3 --out benchmarks/artifacts/my-adoption
```

The harness lives in `benchmarks/` (runner, scorers, validator, fixture generator, task definitions). Campaign artifacts — one JSONL row per session plus validator output — are in `benchmarks/artifacts/`. Provenance note: `contexer_sources` paths recorded in the engine-A/B campaign metadata (`campaign6-retrieval-v1`, `campaign8-paraphrase`) are machine-local checkout paths; they correspond to the git tags `v0.19.0` and `v0.20.0` — check out those tags to reproduce the arms.
