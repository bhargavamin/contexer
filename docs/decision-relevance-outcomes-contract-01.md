# Implementation contract 01: regression baseline and measurement definitions

Status: implemented and verified on 2026-09-18.
Date: 2026-09-18.
Parent: [Decision relevance and outcomes plan](decision-relevance-outcomes-plan.md), implementation step 1.
Reviewed reference: `5676247f358dc6a0c099d8eb8eb94623bad04bb0`.
Implementation base: `67d1a4be534fa95919b82ae07d4286a492ed6158`.

## 1. Purpose

Make the next relevance changes testable before changing how Contexer behaves.
Deliver a small, repeatable answer to: **Which decision should this task receive, what did Contexer actually supply, and what can we honestly conclude?**

This contract establishes synthetic regression cases and a local developer report. It does not deliver the user-facing Decision impact view or prove that real agents produce better work. Those require later contracts.

“Must” requirements below are acceptance criteria for this implementation slice, not permission to implement subsequent slices.

## 2. Scope and non-goals

In scope:

- Eighteen deterministic scenario families, including the three reproduced gaps.
- Frozen synthetic decision/revision snapshots and explicit applicability labels.
- A small benchmark-only runner and machine-readable report with a readable summary.
- Definitions and tests distinguishing selection, emission, lookup, checked outcomes, and unknowns.
- Contract checks for authority labels, host delivery limits, isolation, and unchanged behavior.

Out of scope:

- Fixing query truncation, revision dedup, trigger gates, ranking, thresholds, or output budgets.
- Production instrumentation, new sidecars, schema migrations, telemetry settings, dashboards, or MCP tools.
- New notices, feedback prompts, blocking checks, approval flows, or agent reporting calls.
- Real user prompts/transcripts, private historical corpora, live Teams calls, or remote exports.
- Paid model runs, model judging, performance claims, causal impact claims, or token-savings claims.

Existing capture, approval, enforcement, sharing, storage eviction, and silent-operation behavior must remain unchanged. The historical applicability corpora and frozen ranking constants are not tuning inputs for this work.

## 3. Baseline and implementation starting point

The previous review reproduced these behaviors at the reference revision:

1. An ordinary imperative can return no guidance while a rationale question about the same decision succeeds.
2. A session that received a decision can miss a later approved revision; a fresh session receives it.
3. Explicit BM25 query results can lose relevance order when recurrence-based display truncation applies.

These are **known gaps**, not desired behavior. Before implementation, record the actual base commit and inspect changes since the reference. Re-run these probes in an isolated store. If a gap was already fixed, keep its desired assertion as a normal passing regression test and report the difference; do not restore the bug or blindly retain an expected failure.

Do not implement against or overwrite the primary checkout's unrelated modifications. Choose an appropriate clean implementation branch/worktree when implementation is authorized. Do not fetch a changing branch during a test run; a report identifies the exact code it exercised.

## 4. Deliverables and ownership

Use ordinary functions and existing test fixtures; no generic evaluation framework or production abstraction.

| Deliverable | Proposed location | Responsibility |
|---|---|---|
| Synthetic scenario definitions | `benchmarks/applicability/relevance_cases.json` | Versioned inputs, stable case/decision/revision IDs, applicability labels, reference observations, and desired expectations. |
| Local runner and report projection | `benchmarks/applicability/relevance_baseline.py` | Validate fixtures, isolate execution, call existing owners, collect observable results, calculate scoped metrics, render JSON or text. |
| Contract and golden tests | `tests/test_relevance_baseline.py` | Exercise cases, report validation, unknown handling, mutation checks, and isolation. |
| Minimal test support, if needed | `tests/conftest.py` | Reuse or narrowly extend existing isolation helpers; do not duplicate their real-store leak safeguards. |
| Usage and limitations | This document or existing benchmark documentation | Invocation, known gaps, metric meaning, and how to update a reviewed expectation. |

No changes to `contexer/` are expected. If a required observation needs production instrumentation, record it as unknown and defer the instrumentation. Tests can inspect isolated state through the existing owner; do not introduce store aliases or have production code import benchmark helpers.

Use `retrieval` for pure ranking, `revisions` for revision transitions, `store` for persistence/retrieval, and the relevant adapter for host output. Do not reconstruct production retrieval in the harness or substitute canned success responses for the code being tested.

## 5. Scenario contract

Each case must contain:

- Stable ID, family, description, and fixture schema version.
- Synthetic repository inputs and repo/global/cached-team scope when relevant; no real absolute paths.
- Exact decision IDs, revision IDs, content/title, authority/status, anchors, pending proposals, and fixed dates needed by the case.
- Input prompt or lookup arguments, host/surface, explicit test session/request IDs, initial working set, and ordered actions for multi-step cases.
- Reviewer-authored applicable decision/revision sets and minimum useful delivery tier. Applicability must not be generated by the ranker under test.
- Desired assertions, forbidden outputs/authority claims, and an optional separately labeled reference observation with its commit and known-gap key.
- Outcome expectation: `unknown` for retrieval-only cases; a synthetic checked result only for measurement-contract tests explicitly supplying evidence.

Every case starts from a fresh snapshot. Multi-step state exists only within that case. Fixed clocks and controlled revision IDs must make reruns comparable. Use existing store APIs or owner functions where practical; seed malformed/legacy state directly only when that state is the object of the test.

### Required matrix

These are 18 scenario families; host/status variants may produce more than 18 pytest cases. Except for the first three reference probes, this table specifies coverage requirements, not previously verified results.

| ID | Scenario | Required desired assertion |
|---|---|---|
| R01 | “Add payment retries” paired with the equivalent rationale question | Both should supply the required payment decision on a supported prompt surface. Report the current imperative miss independently from the successful control; broader imperative delivery remains a later fix. |
| R02 | Retrieve revision A, approve B, ask again in the same session and a fresh session | The new revision must not be treated as already delivered solely because A's decision ID was seen. Track the current miss without changing dedup here. |
| R03 | Multiword explicit lookup; best BM25 match versus high-recurrence incidental match; uncapped and `limit=1` | Query cap should retain the relevant winner; expose current cap loss separately from candidate discovery. |
| R04 | Rationale/comprehension question with a strong title match | Existing successful title/body retrieval remains useful; do not match only whole rendered prose. |
| R05 | Explicit file request with a governing anchor | The governing decision reaches the supported delivery surface with its correct authority label. |
| R06 | Weak topic match/pointer followed by explicit lookup | A pointer is not full guidance; a lookup is not checked application. |
| R07 | Acknowledgement and unrelated request | No relevant decision is labeled; irrelevant full-context output counts as a false positive, not helpful coverage. |
| R08 | Same decision and revision requested twice in one session | Existing full-content repetition suppression remains distinguishable from a miss. Prior exposure is recorded, not fabricated as a second emission. |
| R09 | Two sessions with overlapping topic and close timestamps | Working-set effects stay session-scoped; the report never joins a lookup or outcome to the other task by time/topic alone. |
| R10 | Compaction after prompt delivery | Restored current guidance is distinguishable from ordinary repeat injection; Gemini's deferred restore is exercised at its supported event. |
| R11 | Pending decision as the only match | Useful pending content remains explicitly pending, never counted as settled policy or a violation solely because it is pending. |
| R12 | Ignored entry and bootstrap-withheld entry | Neither is represented as active authoritative guidance; use actual lifecycle fields, not an invented retired status. |
| R13 | Standing decision plus an open proposed update | Both identities and their different authority are preserved when rendered; an unreviewed proposal is not recorded as an approved revision. |
| R14 | Startup constraint full body, convention title, and deferred architecture | Distinguish full guidance, title-only exposure, and deferred content; do not count all captured decisions as fully delivered. Include an existing global standing rule. |
| R15 | Same request through Claude, Codex, Gemini, and Cursor surfaces | Use actual adapter output. Cursor's unsupported per-prompt delivery is reported as unsupported, not an empty successful delivery or a new workaround. |
| R16 | Absent/malformed retrieval index and failed optional bookkeeping write | Existing fail-soft behavior is preserved; prompt routing does not rebuild the index. Missing observation is not reported as no applicable knowledge. |
| R17 | More than three applicable decisions and a common-file incidental match | Candidate coverage, selected/full-output coverage, and cap loss remain distinct; labels do not assume every file match is relevant. No cap/ranking change here. |
| R18 | Synthetic measurement evidence: pass, violation, partial/error, unrelated pass, model claim, missing identity | Only attributable, sufficiently scoped independent checks establish a checked result. Unknowns, unsupported joins, and failed checks remain visible. |

Full-content assertions must verify meaningful content and authority, not just an ID or heading. Avoid hard-coded BM25 float scores and whole-output snapshots when ordered identities, content markers, tiers, and labels express the actual contract.

## 6. Handling known failures without enshrining them

Maintain separate fields for **reference observation** and **desired behavior**. Never silently replace desired behavior with whatever the current implementation returns.

- Run reference-characterization assertions only against the matching pinned reference revision. They establish reproduction, not quality.
- On an implementation branch where R01/R02/R03 still reproduce, use narrowly scoped `pytest.mark.xfail(strict=True, raises=AssertionError)` on the desired assertion, with a stable gap key and owning next contract. Setup/runner errors must still fail normally; split setup validation from the expected assertion where necessary.
- Expected failures are listed individually in the report and excluded from any “all requirements satisfied” claim. Do not blanket-xfail a whole file, adapter, or family.
- Unexpected success requires removing the resolved gap marker and reviewing the observed change. Never update fixture expectations automatically.
- Newly discovered gaps must be reported and triaged, not added to an ever-growing allowlist merely to make CI green.

Contract 02 will address R03 (query-cap selection); the subsequent revision-dedup contract will address R02. R01 belongs to the later ordinary-task experiment. Those changes are not bundled into this baseline PR.

## 7. Measurement vocabulary and benchmark report schema

Use a versioned JSON report (`schema_version: 1`) backed by small dictionaries/functions. This is a **benchmark report format**, not a production event schema or a promise to persist user-session data.

Top-level fields:

- `code_revision`, `dirty`, `fixture_version`, `fixture_sha256`, and `runner_version` (the exact runner revision or content hash). `fixture_sha256` hashes the fixture data actually executed, including caller-supplied in-memory fixtures, rather than an unrelated default path.
- `cases`: per-case results with stable IDs and ordered observations.
- `summary`: counts and metric numerators/denominators, known gaps, unsupported paths, and observation coverage.

Per-case fields:

- `case_id`, `host`, `surface`, synthetic `repo_id`, `checkout_id`, `session_id`, and `request_id` (nullable when unavailable).
- `applicable`: labeled decision/revision identities with required tier and authority, plus action-scoped `delivery_expectations` identifying each request/session where that guidance is required.
- `observations`: action/request/session identity, `stage`, decision/revision/proposal identity when known, `tier`, `status`, `reason`, and `evidence_source`.
- `outcomes`: checked rule/task scope, exact artifact/decision identity, validator identity/version, result, completeness, and evidence provenance; default empty with explicit outcome-unknown reason.
- `assertions`: passed, failed, or known-gap, with specific expectation keys; never just a single success boolean.

Allowed stages are `candidate`, `selected`, `emitted`, and `looked_up`; outcomes are separate observations, not an obligatory next stage. A valid check can exist even when nothing was retrieved, so it cannot by itself prove Contexer caused the result.

Delivery tiers are `standing_title`, `standing_full`, `prompt_full`, `pointer`, `tool_result`, and `team_delta`. Preserve source scope separately from tier. A title or pointer satisfies only a label explicitly requiring that tier, not a full-guidance requirement.

Observed stage status is `observed`, `absent`, `unknown`, or `unsupported`. Use `absent` only when the measured boundary was observed and nothing was emitted there. A stage the harness cannot see is `unknown`, not an empty list of candidates. Reasons include `gate_closed`, `already_delivered_revision`, `budget_limited`, `host_unsupported`, `identity_unavailable`, and `observation_unavailable`; use a reason only when fixture-controlled evidence or existing structured output supports it.

Rendered text may support fixture assertions using unique synthetic content markers, but it must not be parsed to invent production revision IDs, attribution, or internal exclusion reasons. A rendered revision is known only when the actual loaded revision state and output jointly establish it. After approval, the generated current revision ID comes from the store and the former proposal identity is absent; an open proposal keeps its identity separate from the standing revision.

No timestamps/topic-overlap joins. Exact test IDs are available because the harness supplies them; this must not be described as solved cross-host production identity. Capture source identity, rather than assuming MCP process and hook session IDs are interchangeable.

### Outcome evidence rules

Allowed result labels are `verified_compliant`, `violation_observed`, `clarification_appropriate`, and `unknown`, always with scope. Synthetic R18 evidence tests these definitions; it is not a real agent outcome.

- A complete deterministic check of a named rule on an attributable artifact can establish compliance with that rule only when the checked-policy identity matches and the validator identity and version are present.
- `allow` with error/partial status, no artifact, or no checked policies establishes no general compliance. A partial evaluation may prove a specific observed violation but not overall cleanliness.
- An unrelated passing test, successful commit, model claim, or lack of violations yields no verified task outcome.
- Unresolved or mismatched identities/coverage remain unknown. Appropriate clarification needs an attributable request artifact, a matching predefined checked criterion, and an identified independent validator, not just an agent saying it asked a question.
- Existing policy result shapes may be used as synthetic inputs. Do not add a production receipt collector or invent passing-policy identities the existing result does not expose.

## 8. Metric definitions

Report counts with ratios; zero denominators are `null`/“not applicable,” never 100%.

- **Full-guidance precision:** applicable delivered full-guidance identities / all full-guidance identities observed at `emitted` or `looked_up`. Candidate and selected observations never receive delivery credit. Dedupe within a request, retain separate repeat-emission counts, and report by host/surface. Pointers/title-only output have their own precision, not pooled credit.
- **Required-guidance coverage:** delivered applicable identities at the required tier / all action-scoped applicable identities for supported requests. Report request/session rows as well as the aggregate, so one successful request cannot hide another request's miss. Count valid prior standing/session exposure only when represented by the corresponding delivery boundary, and never count an obsolete revision or a title substituted for a required full body.
- **Whole-request coverage:** requests receiving every action-scoped applicable identity / supported evaluable requests that require at least one.
- **Whole-case coverage:** cases receiving every required applicable identity / supported, evaluable cases that require at least one. Report unsupported/unknown cases separately and an all-assigned conservative view so excluding them cannot conceal product limits.
- **No-decision false-positive rate:** labeled no-relevant-decision cases with full guidance / all such observed supported cases.
- **Authority errors:** failed reviewer-authored checks of the authority labels actually rendered with the content. Properly labeled pending/proposed content is not an error; a missing `[pending]` or proposal caveat is.
- **Observation coverage:** observed versus unknown/unsupported stage counts. Internal candidate recall stays unavailable unless the harness actually observes it; do not infer it from final output.
- **Checked outcome coverage:** cases with attributable independent checks / assigned cases; retrieval-only scenarios have unknown real task outcomes. No end-to-end success percentage from synthetic R18 evidence.

Do not include “mistakes prevented,” “hours saved,” or estimated recall-note token savings in this report. Performance thresholds and paired model outcome gains belong to later evaluation contracts, not this deterministic sample.

## 9. Isolation, determinism, and behavioral parity

- Reuse the test suite's temporary-store fixtures and leak guards. Each case owns fresh store/index/working-set/global/team-cache state; fixtures never load the developer's real store.
- Isolate adapter config/home-derived paths through test facilities before importing modules that cache them. Do not install hooks, modify user settings, or invoke actual agent binaries.
- Stub release-notice refresh, team refresh, and other unrelated external effects; network access or unexpected subprocess execution must fail the test. Permit explicitly declared local fixture-Git operations only outside the prompt measurements that forbid them.
- Do not patch out the retrieval/adapter behavior being measured. Freeze unrelated due-notice state so user-facing payload assertions are reproducible.
- Report to stdout by default; persist only to an explicit caller-selected output file. No default artifact write into the repository or real Contexer directory.
- Compare two fresh reruns after normalizing declared volatile fields; decision identities, ordering, authority, tiers, counts, and gap classifications must match. Changing case order must not change results.
- Existing runtime behavior and adapter payloads must remain unchanged. Test observations themselves must not add a production log, notification, network request, working-set mutation, or new lock. Ordinary production calls may mutate only their isolated fixture state as they normally do.

## 10. Proposed invocation and verification

The implementation is available through these commands:

```bash
uv run python benchmarks/applicability/relevance_baseline.py --format text
uv run python benchmarks/applicability/relevance_baseline.py --format json
uv run pytest tests/test_relevance_baseline.py --no-cov -q
```

Both report formats must use the same result data. The runner exits 0 when assertions pass or only explicitly registered known gaps remain, and exits 1 for unexpected failures, invalid fixtures, or isolation errors. It must print known-gap counts prominently even on exit 0. `--strict` must exit 1 while any desired requirement remains unmet, including known gaps. `--help` must explain this distinction.

During development run the affected retrieval, working-set, rendering, status-filter, compaction, adapter, and benchmark tests with `--no-cov`. Before merging an implementation PR, run the full required suite and pinned lint:

```bash
uv run pytest tests/
uvx ruff@0.15.4 check .
```

The earlier 119 passing targeted tests belong to the planning review. They do not verify this not-yet-built contract implementation.

## 11. Acceptance checklist and handoff

- [x] All 18 families are represented with reviewed desired assertions and immutable synthetic inputs.
- [x] The three reference gaps reproduce on the implementation base with separately labeled reference observations.
- [x] Known gaps are visible, narrowly scoped, and never reported as desired behavior passing.
- [x] The report identifies exact code/fixture/runner versions and separates supported, absent, unknown, and unsupported observations.
- [x] Perturbing R03's expected winner or R02's expected revision makes the corresponding desired assertion fail; assertions are not merely checking nonempty text.
- [x] Measurement tests reject fabricated success from unrelated passes, model claims, incomplete checks, ambiguous identity, and title-only coverage.
- [x] Report determinism, order independence, invalid-input handling, strict mode, and zero-denominator handling are tested.
- [x] No real-store/config writes, network calls, live models, or new dependencies occur. The implementation-base backport uses the reference v3 title/content index and `prompt_rank` implementation for the already-specified read-only multiword explicit-lookup fallback needed by R03; recurrence-based capped selection remains the registered gap.
- [x] Full tests and pinned lint pass, with explicit expected-failure counts and no weakened coverage gate.
- [x] This handoff lists remaining gaps, verification commands/results, sample report output, and the next narrow fix.

### Implementation handoff

The normal text report currently summarizes:

```text
cases: 18
known gaps: 3
unexpected failures: 0
unsupported paths: 1
```

The three expected failures remain deliberately narrow:

- R01: `ordinary-task-trigger-gap`, owned by the later ordinary-task experiment.
- R02: `revision-id-working-set-dedup`, owned by the revision-dedup contract.
- R03: `query-cap-relevance-loss`, owned next by contract 02, query-cap relevance preservation.

Verification on the implementation base:

```text
uv run pytest tests/test_relevance_baseline.py --no-cov -q
33 passed, 3 xfailed

uv run pytest tests/
3827 passed, 8 skipped, 3 xfailed; coverage 93.69%

uvx ruff@0.15.4 check .
All checks passed!
```

Completion means the baseline and definitions are trustworthy enough to evaluate the next changes. It does **not** mean decision relevance has improved, outcomes are measured in live sessions, or a user-facing impact feature has shipped.
