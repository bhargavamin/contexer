# Capture campaign: does a decision made in one session reach the next?

The adoption campaign ([`ADOPTION_CAMPAIGN.md`](ADOPTION_CAMPAIGN.md)) seeds every decision before
the session starts. This campaign seeds nothing. In session 1 the user states a team rule, with
its reason, while asking for a task; in session 2 a fresh agent gets a different task that needs
the rule and no hint. Whatever session 1 recorded is all session 2 has. It measures the half of
Contexer that seeding skips: capture, and capture followed by reuse.

**Cost warning.** Live runs call `claude` with a real API key. Run the free kit first, then quote
the session count and cost, get approval, and run one chain before the batch.

## Task file

`benchmarks/capture_tasks.json`: 8 chains (`cap-<name>`), each with two steps sharing one
repository and HOME per condition and rep. Each rule is an arbitrary team choice an agent can't
infer from the code (an `amount_cents` key, `rec_` id prefixes, `_ms` duration keys, error dicts
instead of exceptions, `page_size` defaulting to 25 capped at 100, newest-first ordering,
camelCase keys, `'Y'`/`'N'` flags).

| Field | Meaning |
| --- | --- |
| `chain`, `step` | the chain and its session (1 states the rule, 2 needs it) |
| `check_cmd` | adherence: the code follows the rule |
| `functional_cmd` | session 2 only: the change works, rule or not |
| `capture_terms` | patterns that identify the rule in whatever the agent stored, since its wording is the agent's own |
| `plausible_wrong` | what a capable agent writes without the rule |

## Conditions

| Condition | What carries the rule from session 1 to session 2 |
| --- | --- |
| `without` | nothing |
| `claudemd_maintained` | a CLAUDE.md that starts with an empty `## Decisions` section and asks the agent to record decisions there |
| `with` | Contexer, empty store, no approval simulated between sessions |

`claudemd_maintained` is a deliberately maintained decision log, a stronger baseline than an
ordinary CLAUDE.md: every session-1 agent is told to record each decision with its reason. Report
it under that name, never as "CLAUDE.md" in general. The arms are not symmetric in one way:
Contexer can also capture a rule straight from the prompt, while the CLAUDE.md arm depends on the
agent making the edit. Nothing Contexer captures is approved between sessions, and session 2 gets
only what Contexer chooses to show.

Between the sessions the harness puts the code back where session 1 started and keeps only the
arm's memory (the maintained CLAUDE.md, or Contexer's store in HOME). Session 1's code applies the
rule, so leaving it would let session 2 copy the rule from the module and measure nothing. This
models a rule agreed in discussion before the code exists, or applied in another repository.

Capture is judged by patterns over the stored text, so a faithful paraphrase that matches none
of them counts as not captured. The patterns were broadened after a blind review, and the kit
pins the reviewer's paraphrases; when a capture is reported missing, read the stored text before
blaming the arm. Matching ignores the run's own repository path and chain name, which hook
output repeats (a work directory named after the cents chain once matched the cents rule).

## What a row records

- Session 1: `success` (it applied the rule it was told), and between sessions `captured`
  (the rule is in the store, or in CLAUDE.md's decisions section), plus for Contexer
  `capture_status`, `decisions_stored` and `decisions_pending`.
- Session 2: `success`, `adherence`, `functional`, and for Contexer `needed_delivery` (the rule
  reached the agent) and `contexer_lookups`.

Report each chain's drop-off separately: not captured, captured but pending, captured but not
delivered, delivered but not applied. Each points to a different fix.

## Report

```bash
uv run python benchmarks/capture_report.py benchmarks/artifacts/<dir>
uv run python benchmarks/validate.py benchmarks/artifacts/<dir>
```

The report gives each arm's capture and session-2 success and Contexer's drop-off by stage. The
validator fails a campaign whose capture rows lack their measurements or whose chains are missing
a step (expected steps come from the task file). Session-2 delivery is read only from session 2's
own transcript: chain steps share a HOME, and session 1's capture acknowledgement repeats the
rule. A step that errors stops its chain, so a broken revert can't contaminate session 2.

## Free steps

```bash
uv run pytest tests/test_bench_capture_kit.py -q --no-cov
```

The kit proves every session-2 check fails a plausible decision-ignorant implementation and the
untouched fixture, passes a correct one, and that no session-2 prompt hints at its rule.

## A live run

```bash
uv run python -m benchmarks.run --tasks-file benchmarks/capture_tasks.json --steady-state \
  --conditions without,claudemd_maintained,with \
  --model claude-sonnet-5-5 --reps 3 --out benchmarks/artifacts/<new-dir>
```

8 chains x 2 sessions x 3 arms x 3 reps = 144 sessions. Use a new `--out` directory per task-file
version.
