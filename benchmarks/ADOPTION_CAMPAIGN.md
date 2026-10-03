# Adoption campaign: does Contexer change what agents build?

A task set for judging Contexer the way a team evaluating it would: on tasks where the needed
knowledge lives in the code, in the docs, nowhere in the repository, or where the code points
the wrong way, compared against the repository alone and against ordinary documentation. The
task set has 38 tasks, at least 4 per class, with 12 tasks (K1 and K6) where no stored decision
should change the result, so harm can be measured there.

**Cost warning.** Live runs call `claude` with a real API key. Run the free steps first, then
quote the session count and cost, get explicit approval, and run one session before any batch.

## Task file

`benchmarks/adoption_tasks.json`. Every task shares one store of realistic decisions (in a
recorded random order, `store_order_seed`), and adds:

| Field | Meaning |
| --- | --- |
| `class` | where the needed knowledge lives: K1 code, K2 repo docs, K3 nowhere in the repo, K4 code points the wrong way, K5 an obvious approach was rejected, K6 no decision needed, K7s a decision superseded by a newer one, K7c two current decisions that collide |
| `tag` | for K3-K5: `convention` (an arbitrary team rule) or `judgment` (a choice a capable engineer would reasonably get wrong without the decision) |
| `plausible_wrong` | the decision-ignorant implementation a reasonable engineer would write |
| `check_cmd` | **adherence**: followed the needed decision (empty when no decision bears on the task) |
| `functional_cmd` | **functional**: the change works, independent of any decision |
| `forbids_action` | the decision forbids something, so adherence alone can pass an untouched fixture |
| `fixture_files` | per-task overlay on the fixture repo (write, append, or replace-once), committed before setup |
| `real_source` | for tasks rewritten from a real decision (this repository's published design constraints), where it came from |
| `conflict_decisions`, `clarification` | K7c only: the two colliding decisions, the terms that name each side, and `side_taken_cmd`, which exits 0 when the code implements either side |
| `secondary_decisions` | decisions that also apply to the task but are not graded (an agent may follow them at some cost); checks must not penalise following them |

A row's `success` requires every check the task has. Rows also record `adherence` and
`functional` separately, and `check_output` names which check failed. A K7c task has neither
check: its correct outcome is to stop and name the conflict, so it succeeds only when the
session's final message names both sides and its code takes neither (`clarified`).

A seed decision may carry `history`, older revisions it superseded, with a `date` on each. The
store gets them as real revisions (Contexer serves the current one); the static arms show every
revision, dated, with the old ones marked superseded.

`tests/test_bench_adoption_kit.py` proves, for every task, that each check judges four patches
correctly (compliant and working; working but non-compliant; compliant but broken; neither),
that the untouched fixture never succeeds, and that a session which follows the decision but
breaks the code is not scored as a success. Each K7c clarification check is validated against
five session outcomes (a valid clarification, vague hesitation, either side implemented, and a
claimed clarification that still implements a side), and the runner's K7c scoring is tested end
to end.

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

## Larger store

`benchmarks/adoption_tasks_150.json` holds the same 38 tasks and checks over a 150-decision
store: the 34 decisions above plus 116 synthetic decisions about other components (gateway,
export and import jobs, sibling services, web app, CI, deploy, warehouse, security, on-call,
docs), in a new recorded order. None is anchored to the task module or touches a graded topic,
but they share its vocabulary, so they compete in retrieval the way a real store's neighbours
do. Every task's needed, secondary and conflict indices point to the same decision text as in
`adoption_tasks.json` (pinned by the kit test). Run it the same way with
`--tasks-file benchmarks/adoption_tasks_150.json`; the `without` arm doesn't read the store, so
its results carry over from a run on the smaller file.

At this size the SessionStart block exceeds Claude Code's inline limit for hook output and
arrives as a short preview (#365); report results with that in mind until it is fixed.
