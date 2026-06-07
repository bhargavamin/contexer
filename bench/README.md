# Contexer A/B benchmark

Measures whether Contexer improves Claude Code usage by running a fixed task corpus through two
arms — **cold** (no Contexer) and **warm** (Contexer active + store pre-seeded) — and comparing
**cost** (exact tokens → $) and **quality** (blind LLM-judge: decision adherence, task correctness,
conflict avoidance, rework).

This is standalone eval tooling. It does not touch the three core files (`server.py`, `store.py`,
`requirements.txt`) and never mutates your global Claude config.

## Requirements

- `uv` and a working `claude` CLI **with headless auth**. Either:
  - you're logged in interactively (keychain) — non-bare `claude -p` reuses it, **or**
  - `export ANTHROPIC_API_KEY=...` (token-first; required if your runs sandbox `HOME`).
- The benchmark runs `claude -p` many times — it spends tokens. See cost note below.

## How it works (real scripts/ install, with backup+restore)

The warm arm is toggled with the **real installer** — `scripts/install.sh` / `scripts/uninstall.sh`
— so it reflects exactly what a user gets, on the latest code. Headless `claude -p` only
authenticates against the real `~/.claude` on this machine (the subscription token is in the macOS
Keychain; a sandboxed `HOME` returns "Not logged in"), so the install touches the **real global
config**. The suite makes this safe by backing up and restoring `~/.claude.json`,
`~/.claude/settings.json`, and `~/.contexer/` around the run, and grouping by install state:

1. back up global config → `scripts/uninstall.sh` (clean) → run all **cold** + **paste** runs
2. `scripts/install.sh` (latest) → run all **warm** runs → `scripts/uninstall.sh`
3. restore global config

Three arms:
- **cold** ("No memory"): Contexer uninstalled; `--setting-sources project --strict-mcp-config
  --mcp-config '{}'` for a clean baseline.
- **paste** ("Paste rules by hand"): same as cold, but the `seed_decisions` are prepended to the prompt.
- **warm** ("Contexer"): Contexer installed via `scripts/install.sh`; `~/.contexer/<slug>.json`
  pre-seeded. Claude must call `get_context` itself to use the decisions (JIT).

`run_bench.py` runs `git pull --ff-only` first (skip with `--no-pull`). It runs **serially** and
mutates your live global config *during* the run (restored at the end via a `finally` block).

The comparisons that matter: **warm vs cold** = does remembered context improve quality;
**warm vs paste** = is automatic recall as reliable/cheap as always-pasting; **cost per correct
result** across all three normalizes cost by whether the rule was actually followed.

The temp repo's path yields a unique store slug, so bench data never collides with your real repos
and is deleted after each run. The shared `~/.contexer/.current_repo` is backed up and restored
around the suite. Run serially (no concurrency) so the shared anchor file doesn't race.

## Run it

```bash
# 1. Dry run — 1 task, all 3 arms, cheap-ish model (~$0.30). Verifies the pipeline end to end.
uv run --directory . python bench/run_bench.py --tasks naming-convention --arms cold,paste,warm --reps 1 --model claude-sonnet-4-6
uv run --directory . python bench/grade.py --panel 1 --model claude-haiku-4-5
uv run --directory . python bench/analyze.py

# 2. Full run — all tasks, 3 arms, 5 reps, on the model you actually ship; 3-judge panel.
uv run --directory . python bench/run_bench.py --reps 5 --model claude-sonnet-4-6
uv run --directory . python bench/grade.py --panel 3 --model claude-haiku-4-5
uv run --directory . python bench/analyze.py
```

Outputs (under `bench/results/`, gitignored):
- `runs.jsonl` — one record per run: usage, derived + reported cost, num_turns, diff, result, contexer tool-call counts.
- `graded.jsonl` — the above plus blind-judge scores.
- `summary.csv` / `summary.md` — per-(task,arm) medians and **warm − cold deltas**.

## Reading the output

- **Δ adherence** (warm − cold) > 0 on *high-signal* tasks ⇒ remembered context changed the answer (Contexer's core value).
- **Δ cost / Δ tokens** ⇒ what Contexer costs; on *neutral* tasks this is pure overhead (memory didn't help).
- **cost_per_success** = median cost ÷ correctness pass-rate — the money metric, normalized for quality.
- **ctx_calls** — how often Claude actually called `get_context` etc. in the warm arm (0 in cold).

## Cost

Token-first: cost is derived locally from `usage` via `price_table.py`, so it's valid on a
subscription (where the CLI's own `$` figure is an estimate) or an API key (real billing). The
dry run reports exact tokens — multiply across your full matrix to project the full-run cost before
committing. Knobs: fewer `--reps`, smaller corpus, judge on `claude-haiku-4-5`, and (for non-latency-
sensitive judging) the Batches API at 50% off.

## Files

| File | Role |
|---|---|
| `corpus.py` | Task corpus (prompt, seed_decisions, project_files, rubric, kind) |
| `price_table.py` | Token → USD (cache write 1.25×, read 0.1×) |
| `run_bench.py` | Orchestrator: per (task, arm, rep) → isolated `claude -p` → record |
| `grade.py` | Blind LLM-judge over `runs.jsonl` |
| `analyze.py` | Aggregate → CSV + markdown with warm−cold deltas |
