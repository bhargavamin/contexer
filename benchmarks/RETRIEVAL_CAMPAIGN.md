# Decision-dependent retrieval campaign: runbook

Does Contexer change what an agent builds when a stored decision changes the right answer?
Earlier A/B pairs (C02) could not show this: their task did not depend on any stored
decision, so both arms produced the same correct code. This campaign uses tasks that cannot
pass without the decision.

The harness is the existing A/B runner (`benchmarks/run.py`) with a separate task file
(`benchmarks/retrieval_tasks.json`), plus a free offline preview
(`benchmarks/replay_delivery.py`). Run every command from the current Contexer checkout.

## COST WARNING

Steps 4 and 5 call `claude` with a real API key and spend real tokens. Before either:
compute the session count and cost for the exact run, quote it to the developer, and get
fresh, explicit approval for that run. Steps 1-3 are free and must pass first.

## What is measured

**Task set.** Four tasks share one store of 19 decisions modelled on a real project store,
not written to make a check pass: their lengths (a short rule up to a multi-paragraph
codebase summary), wording, mix of anchored and unanchored decisions, and mix of approved
and capture-time "suggested" decisions follow the spread of a real store. Twelve are anchored
to files, most to the fixture's only module, `app/svc_{seed}_core.py`, which is far more
than the three full-content slots the prompt hook fills. Each task needs a different
decision (`needed_decision`, an index into `seed_decisions`):

| Task | Needs | Check passes only if |
| --- | --- | --- |
| `retr-batch` | batch results as `{'items', 'failed'}` | `fetch_records_batch([1, 0, -3, 2])` puts exactly `0` and `-3` in `failed`, and `items` holds record dicts with ids `1` and `2` (as a list, or a dict keyed by id) |
| `retr-cache` | `lru_cache(maxsize=256)` | every cache in the module reports `maxsize == 256`, and repeating `fetch_record_{seed}_0(5)` hits one |
| `retr-audit` | epoch-millisecond `at_ms` | `record_audit_entry(...)['at_ms']` is a current int in ms |
| `retr-errors` | `RecordInputError(ValueError)` | id `0` raises `RecordInputError`; id `1` still returns |

**Every agent writes decisions differently.** Each needed decision exists in three author
styles: `terse` (a one-sentence rule, as a developer dictates it), `narrative` (an AI capture
with incident history, issue numbers and a because-clause) and `plan` (a numbered plan where
the rule is one point among several). Rep `r` shows each decision in style
`(r + position) % 3`, so one store always mixes styles while each needed decision appears in
every style once per three reps; each row records the needed decision's `needed_style`.

**Was the decision actually delivered?** Success says the code matched the decision, not that
the agent saw it. On the Contexer arms each row also records, from the session transcript,
`needed_delivery` (`full` when the decision's whole text reached the agent through a hook or a
tool result, `named` when only its id did, `none` otherwise; the agent's own messages never
count) and `contexer_lookups` (the agent's calls to Contexer's `get_context` tools). The
`claudemd` arm's file sits in the system prompt, which the transcript doesn't record, so those
fields are absent there rather than a false `none`. `rationale` carries no signal on these
tasks: they have no gold facts, so it is always 1.0. Use
rep counts in multiples of three. The check for a task only tests what every style of its
decision states.

**Seeding is the agent path.** Decisions are stored with `update_context` as the AI author
(title, anchors, capture-quality gate and novelty filter all applied); the ones the store
queues for review are then approved one by one, and the ones it makes active on capture stay
"suggested". A decision the measured version refuses fails setup loudly with the store's own
message (`benchmarks/seeding.py`).

The store also holds constraints (preloaded in full at session start) and conventions, so
it exercises both retrieval fixes: ranking anchored decisions (#341) and not re-delivering
startup rules (#342). Needed decisions are never constraints, so they must be retrieved, not
preloaded. `tests/test_bench_retrieval_kit.py` proves every check fails on the untouched
fixture and on a plausible decision-ignorant implementation, and passes on a compliant one.

**Arms.**

| Condition | What the agent gets |
| --- | --- |
| `without` | nothing |
| `claudemd` | the same decisions, same styles and titles, in `CLAUDE.md` (the honest static competitor) |
| `with_prev` | Contexer at the previous version, via `--contexer-sources` |
| `with` | Contexer from this checkout |

## Step 0: the previous version

```bash
git worktree add ../contexer-prev 1d8701b   # last commit before #347's retrieval fixes
```

Any other baseline works the same way; record which one in the write-up.

## Step 1: offline delivery preview (free)

```bash
uv run --frozen python benchmarks/replay_delivery.py tasks
uv run --frozen --project ../contexer-prev python benchmarks/replay_delivery.py tasks
```

It seeds each task exactly as the `with` arm does (bootstrap, then the decisions, via the
shared `benchmarks/seeding.py`), once per author style, runs SessionStart and the prompt
hook, and reports whether the needed decision arrived in full, only named in a pointer, or
not at all. "In full" means the decision's title and body (as that version's own
`title_and_body` splits them) both appear. Recorded on 2026-10-02 at seed 0 (12 = 4 tasks x
3 styles):

| Contexer | Needed decision in full | Named only | Missing | Startup rules re-sent |
| --- | --- | --- | --- | --- |
| this checkout (#350, #353) | 9/12 (terse 1/4, narrative 4/4, plan 4/4) | 3 (all terse) | 0 | 0 |
| `1d8701b` (before) | 3/12 (1/4 in each style) | 0 | 9 | 12 |

The gap the realistic store exposes: a needed decision written as one short rule loses its
full slot to longer decisions and is only named, while the long narrative and plan versions
arrive in full. The live run shows whether the pointer is enough. (On the earlier
six-short-decision store the same checkout delivered 3/4 in full, with only `retr-cache`
named, #351.)

A seed the measured version refuses to store is reported as `not_stored` and counted as
missing; the live `with` arm fails setup on it rather than running without the decision.

The same script replays a real session's frozen store (for example C02's):

```bash
uv run --frozen --project <checkout> python benchmarks/replay_delivery.py snapshot \
  --snapshot <frozen store .json> --repo <task checkout> --prompt-file <prompt.txt>
```

`with_prev` must have a `--contexer-sources` entry; the runner refuses to start without
one, since the arm would otherwise run bare under a version-comparison label.

## Step 2: harness tests (free)

```bash
uv run pytest tests/test_bench_*.py -q --no-cov
```

All must pass before any spend.

## Step 3: freeze the plan

Before the first paid run, write down and keep with the artifacts:

- the model (`claude-sonnet-5-5` unless decided otherwise), seed, and `with_prev` commit;
- the rep count for step 5, fixed now (see below), never raised after seeing results;
- the headline: pooled success, `with` vs `with_prev`, then `with` vs `without` and `with`
  vs `claudemd`. Per-task cells are diagnostic.

Do not edit `retrieval_tasks.json` after a live run uses it; a changed task set is a new
campaign in a new `--out` directory. `campaign.json` records the file's `tasks_sha256`, so
compare it between the smoke and the full campaign.

## Step 4: smoke (paid)

3 reps (one per author style) x 4 tasks x 4 conditions = 48 sessions. Estimate the
per-session cost from the medians in your own `benchmarks/artifacts/*/runs.jsonl`; the
2026-10-02 smoke on this store cost $3.13 for 48 `claude-sonnet-5-5` sessions (about $0.07 each;
one `claude-sonnet-5` session cost $0.29). Recompute for the model you use from a single
session first (`--tasks retr-batch --conditions with --reps 1`), which also proves setup.

```bash
uv run python -m benchmarks.run --tasks-file benchmarks/retrieval_tasks.json \
  --conditions without,claudemd,with_prev,with \
  --contexer-sources with_prev=../contexer-prev \
  --model claude-sonnet-5-5 --reps 3 --out benchmarks/artifacts/retrieval-smoke
uv run python -m benchmarks.validate benchmarks/artifacts/retrieval-smoke
uv run python -m benchmarks.report benchmarks/artifacts/retrieval-smoke/runs.jsonl
```

Stop if validation fails or any row errored for a harness reason.

## Step 5: full campaign (paid)

Twelve reps (four per style) give n=48 per arm on the pooled headline: 192 sessions,
roughly $13-$20 on `claude-sonnet-5-5` at the smoke's per-session cost (about $56 at the
`claude-sonnet-5` figure). Use the rep count frozen in step 3 and a new `--out`
(`benchmarks/artifacts/retrieval1`). Raising reps after seeing overlapping intervals is
optional stopping; publish an overlap as "no distinguishable difference at this sample".

## Reading the result

The report's "Decision-dependent tasks" section gives success k/n with 95% Wilson
intervals per task and pooled per condition. The pooled interval treats every task x rep row
as independent; the tasks differ in base rate (`retr-cache` is pointer-only by design), so
cite it only when the per-task cells point the same way.

- `with` above `with_prev`: the retrieval fixes changed outcomes, not just delivery.
- `with` above `without`: Contexer's retrieved decisions are used.
- `with` vs `with_prev` on `retr-cache`: both versions fail to deliver it in full, so a
  difference there is about the pointer (#341), not ranking; see #349 for full slots taken
  by low-overlap anchors.
- `claudemd` close to `with`: expected here. Six rules fit easily in a static file;
  Contexer's case against `CLAUDE.md` rests on stores too large to preload, which this
  small set does not test.
- Compare per-task cells with the step 1 preview: a task whose decision was delivered in
  full but still failed points at the agent ignoring context, not at retrieval.

Limits: one synthetic module, four tasks, one model. The fixture is generated per seed so
its code is not in training data, but the decisions' phrasing is ordinary; a model may
guess a convention by luck, which the plausible-implementation tests make unlikely but not
impossible.
