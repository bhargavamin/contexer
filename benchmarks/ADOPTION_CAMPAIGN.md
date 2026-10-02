# Adoption campaign: does Contexer change what agents build?

A task set for judging Contexer the way a team evaluating it would: on tasks where the needed
knowledge lives in the code, in the docs, nowhere in the repository, or where the code points
the wrong way, compared against the repository alone and against ordinary documentation. This
is the first slice (7 tasks); the campaign grows from it.

**Cost warning.** Live runs call `claude` with a real API key. Run the free steps first, then
quote the session count and cost, get explicit approval, and run one session before any batch.

## Task file

`benchmarks/adoption_tasks.json`. Every task shares one store of realistic decisions (in a
recorded random order, `store_order_seed`), and adds:

| Field | Meaning |
| --- | --- |
| `class` | where the needed knowledge lives: K1 code, K2 repo docs, K3 nowhere in the repo, K4 code points the wrong way, K5 an obvious approach was rejected, K6 no decision needed |
| `tag` | for K3-K5: `convention` (an arbitrary team rule) or `judgment` (a choice a capable engineer would reasonably get wrong without the decision) |
| `plausible_wrong` | the decision-ignorant implementation a reasonable engineer would write |
| `check_cmd` | **adherence**: followed the needed decision (empty when no decision bears on the task) |
| `functional_cmd` | **functional**: the change works, independent of any decision |
| `forbids_action` | the decision forbids something, so adherence alone can pass an untouched fixture |
| `fixture_files` | per-task overlay on the fixture repo (write, append, or replace-once), committed before setup |
| `secondary_decisions` | decisions that also apply to the task but are not graded (an agent may follow them at some cost); checks must not penalise following them |

A row's `success` requires every check the task has. Rows also record `adherence` and
`functional` separately, and `check_output` names which check failed.

`tests/test_bench_adoption_kit.py` proves, for every task, that each check judges four patches
correctly (compliant and working; working but non-compliant; compliant but broken; neither),
that the untouched fixture never succeeds, and that a session which follows the decision but
breaks the code is not scored as a success.

## Conditions

| Condition | What the agent has |
| --- | --- |
| `without` | the repository only |
| `claudemd_full` | every decision in CLAUDE.md, titled (the same arm as `claudemd`, under the evaluation plan's name; run one or the other) |
| `docs_indexed` | one record per decision under `docs/decisions/`, with a CLAUDE.md index of titles |
| `with` | Contexer |

## Steady state

`--steady-state` completes Contexer's bootstrap during setup, so sessions start as in a
repository whose one-time setup is done, and setup fails if the bootstrap prompt would still
fire. Without it, every session is a first install. Report the two separately.

## Free steps

```bash
uv run --frozen python benchmarks/replay_delivery.py tasks \
  --tasks-file benchmarks/adoption_tasks.json --steady-state
uv run pytest tests/test_bench_adoption_kit.py -q --no-cov
```

## A live run

```bash
uv run python -m benchmarks.run --tasks-file benchmarks/adoption_tasks.json --steady-state \
  --conditions without,claudemd_full,docs_indexed,with \
  --model claude-sonnet-5-5 --reps 3 --out benchmarks/artifacts/<new-dir>
```

Three reps show each needed decision once in each author style. Use a new `--out` directory per
task-file version.
