# The Contexer Benchmark

We ran over 600 real AI coding sessions measuring what Contexer costs and what it returns, against every common alternative. The **548 sessions that back the findings on this page ship as raw rows in `benchmarks/artifacts/`** — every number below can be recomputed from them. (Sessions from early discarded campaigns — harness bugs, network failures — were excluded from all claims and their rows are not published.) Everything here was scored by code, re-checked by an independent validator, and challenged by an adversarial review before publication.

**Three words, defined once:**

- A **session** is one conversation with an AI coding assistant, start to finish.
- **Tokens** are the units AI providers bill you for. Fewer tokens for the same result = less money and less waiting.
- A **memory setup** is whatever you use to give the AI project knowledge: nothing, a hand-written instructions file (CLAUDE.md or AGENTS.md), or Contexer.

## What it costs

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

## The fine print

For readers who work with tokens, turns, and medians. Numbers are Sonnet 5 unless stated; Opus 4.8 replicated the accuracy and compliance results.

1. **Rationale recall:** with the decision stored, "why did we choose Postgres over MySQL?" is answered correctly in 1 turn / ~33k tokens; without memory: 6 turns / ~165k tokens and no answer. 16 runs per condition, stable under three question rewordings.
2. **Rule compliance:** a seeded rule ("never log request data") was followed 8/8 by every memory condition; bare sessions violated it most of the time (Opus: 0/8 compliant). Contexer was the cheapest compliant setup (~68k tokens vs ~250k for the same rule via CLAUDE.md).
3. **A complete CLAUDE.md ties Contexer on static recall** — identical accuracy and compliance at equal cost. Contexer's edge is that nobody has to write or maintain the file. Layering Contexer on an existing CLAUDE.md caused no harm and no single-shot gain.
4. **AGENTS.md is honored but expensive** — the file is found and read rather than auto-loaded (131k vs 33k tokens on the same question). Small sample (n=3), directional.
5. **Overhead when nothing needs remembering — measured on v0.20.0:** 30 sessions, bare vs Contexer, five editing tasks where no stored decision is relevant. What would have falsified "the overhead is fixed": Contexer costing meaningfully more than bare. Result: **+2.6% median tokens (+7.9% cost)** — down from the +12–17% measured through v0.19.0; improved, not eliminated. The spread matters more than the median: injected conventions change the agent's *behavior* in both directions. Where knowledge replaced discovery it got cheaper than bare (−27% on one task); where the repo's standards applied, it did compliance work bare agents skip — running the full test suite, enforcing the line-length limit, deduplicating test data — at +43–47% on two tasks (every Contexer run above every bare run there). Accuracy and compliance were identical on both arms, and the independent validator confirmed the extremes are task-driven, not noise. Also still true: an earlier "gets cheaper the more sessions you chain" effect did not hold up at a proper sample size. We publish what disappears under scrutiny, not just what survives it.
6. **v0.20.0 vs v0.19.0 (retrieval engine A/B):** 48 interleaved sessions, identical everything except the installed Contexer release. Accuracy and compliance identical; v0.20.0 used **−11.7% median tokens (−8% cost)** overall, −12% to −14.5% on cross-session chain tasks (every v0.20.0 run cheaper than every v0.19.0 run in those cells), and added **no overhead** on the editing task with nothing to retrieve. Wrinkle: 2 of 4 v0.20.0 compliance runs spent extra tokens flagging a conflict with the stored rule before implementing — costlier, arguably better. **Paraphrase check:** all six reworded variants of the recall tasks score identically on both engines, so no engine conclusion hinges on prompt wording — and under one rewording the v0.19.0 engine failed to retrieve at all and spent 170k tokens exploring while v0.20.0 injected normally at 33k. Retrieval that survives rewording is precisely what v0.20.0's retrieval work added. (Attribution note: the A/B compares the two releases, whose entire code difference is the retrieval feature set — engine, session integration, recall notice — so deltas belong to that work as a whole, not provably to the BM25 ranker alone. Variant cells are single sessions: directional.)
7. **Scope:** personal, single-developer sessions on synthetic repos with a pinned model. Team mode has not been benchmarked; this page makes no claims about it.

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
```

The harness lives in `benchmarks/` (runner, scorers, validator, fixture generator, task definitions). Campaign artifacts — one JSONL row per session plus validator output — are in `benchmarks/artifacts/`. Provenance note: `contexer_sources` paths recorded in the engine-A/B campaign metadata (`campaign6-retrieval-v1`, `campaign8-paraphrase`) are machine-local checkout paths; they correspond to the git tags `v0.19.0` and `v0.20.0` — check out those tags to reproduce the arms.
