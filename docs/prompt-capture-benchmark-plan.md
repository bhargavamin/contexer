# Dedicated prompt-capture benchmark

Status: implemented on `codex/capture-environment-revisions`; retained as the design and
adversarial-maintenance record. The runnable contract is in
[`benchmarks/prompt_capture/README.md`](../benchmarks/prompt_capture/README.md).
Baseline inspected: `57d1c1a`, following PR #304, on main base `7289a7e`.

## Objective and scope

Measure whether trusted host prompts produce the right durable decision or review candidate,
with faithful wording, correct target identity, preserved history, and appropriate guidance.
Measure detection, persistence, and host guidance separately so a successful parser cannot hide
a failed store transition, and a recorded candidate cannot be mistaken for a delivered question.

V1 covers deterministic prompt capture: existing prescriptive directives, factual environment
scope, environment reactivation statements, recurrence, review transitions, and host adapters.
Use the user's original n8n and Neuraverse examples as regression seeds, together with unrelated
services, company-specific environment names, and negative controls.

The benchmark is local, synthetic, deterministic, and model-free. It does not establish general
natural-language understanding, real-editor display, or that an assistant actually asked the user
a question. General multi-fact extraction and pronoun resolution across conversational turns need
separate product contracts; list them as out of scope rather than assigning guessed gold labels.
The applicability/relevance R01-R18 corpus and its ordinary-task-trigger gap remain independently
versioned. Existing focused store/adapter tests remain useful regressions.

## Files and execution surfaces

- `benchmarks/prompt_capture/cases.json`: versioned scenarios, actions, and independently written
  expected outcomes. Store the narrowly allowed gap registry in the same fixture.
- `benchmarks/prompt_capture/run.py`: fixture validation, isolated execution, observation,
  assertion scoring, and JSON/text reports. Use plain functions; no new production interface.
- `benchmarks/prompt_capture/README.md`: scope, commands, metric denominators, gap policy, and
  how to add an independently labeled case.
- `tests/test_prompt_capture_benchmark.py`: corpus assertions and harness integrity tests.
  This deliberately avoids the `test_bench_` prefix, which the repository marks `slow` and
  excludes from the main correctness matrix.

Drive `store.capture_user_constraint_with_meta` for store replays, the public functions in
`prompt_capture.py` for diagnostic classifier observations, and `approve_decision` for explicit
review actions. Adapter cases exercise Claude capture, Codex's configured shared Claude entry,
Cursor capture, and Gemini `before_agent`, including the real evidence wrapper.
Do not stub capture, proposal selection, persistence, or approval in scored scenarios.
The runner must execute without pytest; pytest is only a consumer of its report.

## Scenario matrix

Start with approximately 60-80 authored scenarios across these families. Final counts come from
the reviewed fixture; fixture validation pins a per-family minimum and required seed IDs so
dropping difficult cases cannot silently improve the score. Transformed variants and host replays
have separate counts and do not inflate the authored-scenario total.

| Family | Cases and required outcome |
| --- | --- |
| P01 Directives | Clean always/never/from-now-on directives retain their existing approved behavior; deictic and bare can-only statements require review. |
| P02 Scope facts | Original typo-bearing n8n statement, alternate services, aliases, explicit environment markers, structured names, and mercury/venus placement produce pending candidates with faithful casing. |
| P03 Non-environment controls | Weak used/needed relationships between pages, features, or components produce no deployment candidate; changing both labels to explicit environments changes the expected classification. |
| P04 Assertion boundaries | Questions, reported claims, hedges, negation, withdrawn claims, and trailing corrections are rejected as asserted facts; unrelated task sentences do not contaminate a captured fact. |
| P05 Input boundaries | Empty/malformed payloads, fenced code, quoted/log/system text, lengths at and beyond documented bounds, punctuation, and whitespace have explicit acceptance or rejection expectations. |
| P06 Lifecycle extraction | Original mixed PR request, varied company labels, retirement/reactivation synonyms, now-is/is-now ordering, and unrelated questions preserve exactly the lifecycle clause. |
| P07 Target selection | One approved subject match yields a proposal on that ID; unrelated retired Redis components, suggested entries, insufficient overlap, and multiple eligible targets never receive an arbitrary correction. Test equal and unequal target scores and reverse store order. |
| P08 Proposal protection | Existing human/plan/ai/scan proposals remain intact when a different unconfirmed fact arrives; the refusal is truthful. Identical repeated proposals do not create versions. |
| P09 Review and history | Capture leaves HEAD and approval metadata unchanged; explicit approve/edit advances the intended decision once and preserves the prior revision. Skip/dismiss and edited n8n casing are checked. |
| P10 Repetition | Replay unmatched pending candidates, pending proposals, and approved corrections in the same and a new session; assert intended identity/cardinality and recurrence, not just a successful return. |
| P11 Inactive state | Distinguish an active rule saying an environment was retired from an ignored or tombstoned decision. A factual prompt never restores or human-approves an inactive decision; full reconsideration semantics remain owned by lifecycle tests. |
| P12 Host and failure behavior | Persisted results agree across supported capture paths; acknowledgments match each host's capability. Exercise store contention, write failure, and spool failure without false success or unintended state changes. |

P10 and P12 require baseline characterization before their exact missing-capture expectations
are locked. Do not assume that all repeats deduplicate, or that factual capture currently has the
same recovery behavior as explicit directives. Safety assertions are defined independently of
those product questions and always execute.

## Fixture and oracle contract

Each scenario contains a stable `case_id`, `family`, provenance/rationale, synthetic repository
and session IDs, optional pre-existing decisions/proposals/tombstones, ordered actions, and
named assertions at each checkpoint. Action types are a small closed set: submit a prompt,
perform an explicit review action, and read a snapshot. Failure injection is declared on the
specific operation it affects. Unknown kinds, missing expectations, duplicate IDs, invalid
review targets, and unsupported fixture versions fail validation before execution.

Expected data is authored in the fixture, never generated by the classifier or copied from
the observed result. Specify exact case-sensitive extracted/persisted text, action status where
the surface returns one, destination identity, entry-count delta, current revision, proposal
content/source, approval status/stamps, and preservation requirements for other entries.
Include meaningful rationale on old rules so replacing a rule with a shorter clause cannot
pass merely because its environment name still appears.

For generated IDs, bind a fixture alias only when exactly one observed new object satisfies
the action's declared cardinality. Check identity relationships and immutable history before
canonicalizing UUIDs/timestamps for the report. Never identify the successful target by finding
the text the test hoped to capture: that could conceal a write to the wrong decision.

Distinguish `no_capture`, `new_pending`, `new_approved_directive`, `proposed_revision`,
`review_conflict`, and `recurrence` as benchmark observations derived from both returned metadata
and actual state changes. A `(None, None, None)` return alone does not distinguish a negative
prompt from a repeated rule. Approval actions are fixture-supplied simulated developer receipts,
never inferred from the submitted factual sentence.

Host reports separately record storage outcome and guidance capability/delivery. Claude/Codex
and Gemini must emit the expected review instruction in their documented payload field when
capture succeeds. Cursor's prompt output is allow/pass-through and cannot inject that instruction;
record direct prompt guidance as unsupported. The managed Cursor rule can be checked as a
separate instruction surface, but it is not proof that the user was asked. Codex shares the
Claude entrypoint and source label today; do not invent a distinct provenance expectation.

## Isolation and reproducibility

Execute every case with a fresh temporary repository and store redirected through the existing
`store.store_dir()` seam, restoring the seam after the case. This keeps the fast runner in one
process while preventing reads or writes to the developer's store; import-time/home-dependent
cases must use a disposable subprocess if added later. Never install hooks or consult live stores.
Disable optional refresh, sharing, and automatic proposal scans; block network/model calls and
unintended subprocess spawning. Keep capture/evidence paths real.
For injected I/O failures, target one write/lock boundary and snapshot all durable surfaces
afterward. Measure store and evidence retention independently; neither implies the other.

Reports include schema/fixture/runner versions, fixture hash, runner hash, code revision, dirty
state plus a digest of relevant working-tree source, and Python version. Keep raw diagnostics
available for a failed case; normalized semantic reports omit temporary paths and canonicalize
nondeterministic fields after identity checks. Two fresh runs and reversed scenario order must
produce the same semantic report. Fixture mutation cannot be hidden by hashing only the default
on-disk fixture.

## Known gaps and expected failures

Planning probes at `57d1c1a` reproduced three classifier gaps:

| Gap | Input | Observed / desired |
| --- | --- | --- |
| `scope-clause-localization-gap` | Can you run the tests? Also, n8n runs only in live and is not needed in staging | Scope returns false. Desired: pending candidate containing only the factual clause. Include an unrelated hedge variant and a task-prefix-without-question variant to measure text contamination. |
| `lifecycle-is-now-order-gap` | Can you run the tests? Also, the staging environment was retired but is now restored | No lifecycle extraction. Desired: extract the lifecycle fact and propose against a unique eligible target. The now-is variant is a passing control. |
| `lifecycle-trailing-retraction-gap` | The staging environment was retired but now restored, but that is wrong | Extracts reactivation despite retraction. Desired: no factual candidate or correction. |

Confirm each through a real store/adapter replay before registering its exact failing assertion.
These observations revise the earlier suggestion that only one extraction xfail was needed.
Baseline discovery may reveal additional defects, especially repeated pending captures and
failure recovery. Report them explicitly before adding any new exception.

Each registry entry names exact case/action/assertion IDs, expected failure category, baseline
revision, rationale, and accountable owner/follow-up reference. Use a benchmark-plan anchor as
the initial reference; do not invent an issue number or silently create an external issue.
Version any later owner/issue assignment with the fixture.

Score every assertion before applying exceptions. Pytest parametrizes individual registered
assertions with `xfail(strict=True, raises=AssertionError)`; do not xfail a complete scenario,
shared fixture, or adapter family. Exceptions/crashes are harness errors and always fail.
All other assertions in a gap case still run. Unexpected approval, wrong-target mutation,
history loss, or proposal destruction remain hard failures even when capture recall is a
registered gap. For example, the trailing-retraction capture defect may be acknowledged while
an approved write from the same prompt remains an unconditional failure.

Default runner mode fails unexpected assertions, harness/fixture errors, and unexpected passes
of registered gaps; it tolerates only the exact declared failures. `--strict` additionally
fails on any remaining declared gap. Remove a gap registration when fixing it so an XPASS is
an actionable signal. Reports always show gaps, their denominators, and the affected scenarios.

## Metrics and gates

Report counts and denominators, with null for empty denominators:

- Candidate precision and recall on labeled prompt submissions, including accepted review
  conflicts as surfaced outcomes only when the expected wording and target checks also pass.
- Durable new-capture recall separately, so a refusal asking the user to fold in text cannot
  masquerade as a stored candidate.
- Exact text fidelity, correct target routing, review-required correctness, proposal/history
  preservation, and repeat cardinality; provide per-family results as well as totals.
- Unauthorized activation and wrong-target write counts, both required to be zero.
- Guidance emission coverage by supported host surface; report unsupported delivery separately
  while keeping Cursor in storage denominators.
- Full-scenario pass rate requiring every expected checkpoint, plus passed, known-gap,
  unexpected-failure, and error counts. Include gaps in quality denominators.

Avoid a blended score that allows improved recall to offset corrupting an approved decision.
Report the four host replays separately from authored semantic cases. Do not label this corpus
as production traffic accuracy. Keep a small separately reported challenge subset with new
service names and paraphrases authored before running it; because it is checked in, call it a
challenge set rather than claiming a permanently unseen holdout.
Optional latency measurements use fixed hardware and no coverage; they do not become flaky
wall-clock xfails in ordinary CI.

## Implementation sequence and acceptance

1. **Lock the fixture contract.** Author seed cases, desired checkpoints, negative controls,
   and the proposed gap registry. Review gold expectations before viewing baseline results.
2. **Build the runner and state oracle.** Add isolation, schema validation, reports, direct store
   replays, and explicit review actions. Run all cases and characterize additional discrepancies.
3. **Add host replays and harness adversarial checks.** Validate parity for representative cases
   in every family whose transport matters; count unsupported guidance accurately.
4. **Gate CI and publish the baseline.** Add assertion-level pytest consumers, reviewed gap
   registrations, usage docs, and a JSON report artifact from the Python 3.13 correctness job.
   Follow the authoritative maintenance contract in
   [CLAUDE.md](../CLAUDE.md#prompt-capture-benchmark-maintenance). Replace its planned-only
   status and commands with verified runnable guidance and the benchmark README link in this
   same change, so future agents can discover, run, and maintain the benchmark. The README
   must document fixture/version updates, regression additions, and strict-xfail removal.
   Normal correctness tests run on 3.12/3.13/3.14. Proposed local commands:
   `uv run python benchmarks/prompt_capture/run.py --format json` and
   `uv run pytest tests/test_prompt_capture_benchmark.py --no-cov`.
5. **Fix gaps in follow-up changes.** Each production fix removes its expected failure and
   adds neighboring positive/negative variants. Benchmark construction itself does not change
   extraction or add logic to `store.py`.

Before accepting the baseline, mutate the harness inputs/results to prove that: deleting a
required case or its assertions fails; changing an expected ID detects a wrong target; a proposal
silently replacing HEAD fails; an unexpected auto-approval in a known-gap case still fails;
changing only product casing fails text fidelity; a caught adapter error with no durable write
does not pass as silence; and fixing a registered gap produces XPASS/failure until removed.
Reverse eligible target order and scenario order to expose first-match dependence and state leaks.

Done means a documented reproducible baseline, all required scenario families represented,
all known failures explicit at assertion level, zero concealed safety failures, report/pytest
agreement, real host storage paths covered, verified agent run/update instructions and CI
integration, and existing module-boundary and CI-tier contracts preserved. A higher benchmark
score alone is not acceptance.
