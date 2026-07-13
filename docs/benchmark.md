# The Contexer Benchmark

*540 live Claude Code sessions across two models (Sonnet 5, Opus 4.8) and six memory conditions, scored deterministically, recomputed by an independent validator, and challenged by an adversarial review before publication. Raw per-session data ships in `benchmarks/artifacts/`; every number below can be re-derived from it.*

## What we measured

The same AI agent performs the same tasks on the same codebase under different memory conditions — no memory at all, a hand-written CLAUDE.md, an AGENTS.md, both files, contexer, and contexer on top of CLAUDE.md — and we measure accuracy, tokens, cost, turns, and rule compliance, with code doing all the scoring.

## Findings

**1. Recorded decisions turn unanswerable questions into one-turn answers.** Asked "why did we choose Postgres over MySQL?" when the decision existed only in memory: with contexer the agent answered correctly in 1 turn at ~33k tokens (~$0.04); without memory it explored for 6 turns and ~165k tokens (~$0.11) and correctly concluded it couldn't know. Same pattern on both models, 16 runs per condition, stable under three rewordings of the question.

**2. Stored rules get followed.** A seeded compliance rule ("never log request data") was respected 8/8 runs on both models by every memory condition, while bare sessions violated it most of the time (Opus: 0/8 compliant). On Sonnet, contexer was also the cheapest way to comply (~68k tokens vs ~250k for the same rule in CLAUDE.md), because injected rules skip the exploration phase.

**3. A complete, hand-maintained CLAUDE.md ties contexer on static recall.** Given the same knowledge, accuracy and compliance were identical (both models, 16 runs per cell). Contexer's value is therefore not out-recalling a perfect file — it's that the file never has to be written: bootstrap mines evidence-backed conventions from the repo automatically in seconds, and decisions made mid-session are captured without anyone editing docs. Layering contexer on an existing CLAUDE.md caused no harm and no single-shot gain.

**4. AGENTS.md knowledge is honored, but at 2–4× the token cost of CLAUDE.md** (131k vs 33k on the same question) — consistent with the file being found and read rather than auto-loaded. Directional result: the campaign stopped early, one observation per cell.

**5. What we could not confirm — published because dropping failed claims is how benchmarks lie:** an early "cross-session compounding" effect (contexer getting cheaper across chained sessions) disappeared at higher sample size with proper interleaving; and contexer's median session costs **+12–17% more tokens** across all tasks — the injected context is overhead on tasks that never touch memory. The savings are task-shaped, not universal.

**6. Scope:** all numbers cover personal, single-developer sessions on synthetic repos with a pinned model. The team-mode benchmark (shared decisions across developers via the remote store) is designed but has not yet run — numbers on this page make no claims about team mode.

## The harness approach

- **Isolation:** every session runs in a throwaway `HOME` with a fresh copy of the fixture repo; sessions receive an environment allowlist, never the developer's real environment; contexer is installed via its real installer so real hooks are exercised.
- **No training-data leakage:** the fixture codebase is synthetic and seeded — it cannot exist in any model's training data.
- **No ordering confounds:** conditions alternate in time (never one block after another) and every row carries a timestamp; a validator warns if any condition ran as a contiguous block.
- **Deterministic scoring:** answers must contain facts from the stored knowledge; written code is checked by AST inspection against measured conventions; tasks pass or fail by their own test commands. No LLM judge.
- **Verification chain:** an independent validator recomputes every statistic from raw rows with separate code and hunts anomalies (zero-token "successes", error-rate asymmetries, non-interleaved runs); failed sessions are recorded as errored rows and excluded from aggregates, never silently zeroed. An adversarial review pass attempts to refute each claim before anything is published — its objections shaped the conditions above (the CLAUDE.md arm exists because the review demanded a fair competitor).
- **Prompt-sensitivity probes:** key prompts run in reworded variants with identical stored knowledge and scoring, so conclusions can't hinge on one phrasing.

## Reproduce it

```bash
# free end-to-end pipeline check (stub sessions, no tokens)
uv run pytest tests/test_bench_*.py -q --no-cov

# a live campaign (spends real API tokens — start small)
uv run python -m benchmarks.run --reps 1 --tasks rat-storage,conv-endpoint \
  --model claude-sonnet-5 --out benchmarks/artifacts/mine
uv run python -m benchmarks.report benchmarks/artifacts/mine/runs.jsonl
uv run python -m benchmarks.validate benchmarks/artifacts/mine
```

The harness lives in `benchmarks/` (runner, scorers, validator, fixture generator, task definitions). Campaign artifacts — one JSONL row per session plus validator output — are in `benchmarks/artifacts/`.
