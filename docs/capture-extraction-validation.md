# Capture extraction: review gate and validation

Ambiguous mixed instructions retain their full candidate as `pending_approval` instead of automatically approving an extracted fragment. For example, `Always hash passwords. Write the hash with argon2id.` previously became approved `Always hash passwords`, losing the algorithm requirement. It now retains both sentences for human review.

The owner approved this contract before implementation. The same preservation boundary covers proposed removals in both implicit extraction branches. Explicit task-scope separation remains unchanged; plain work items still create no capture. No schema, global authority, model dependency, or review tool was added. Existing approvals are not silently rewritten or demoted, and a clean developer restatement can still resolve a pending candidate.

## Validation identity

Reviewed against `24b3d2fff8f2f258e1dda64c8a9e3af72c09ae43`, also the remote `main` tip when this PR was prepared. Test environment: macOS ARM64, Python 3.12.10, uv 0.11.28, pytest 9.0.3, xdist 3.8.0, pytest-cov 7.1.0, coverage 7.14.1, MCP 1.29.0, Ruff 0.15.4.

Tests ran with disposable child `HOME` and `CONTEXER_NO_UPDATE_CHECK=1`; capture probes also used isolated stores. All 797 real-store file contents and metadata matched before/after the approved fix run. The original implementation author's uncommitted worktree was unchanged. Frozen #390 task files, labels, B1/B2 runs and scores were neither inspected nor changed.

The hashes and results in this section describe the initial reviewed patch, committed as `9d7e044a`. The PR review follow-up below records the subsequent sanitizer fix and its validation separately.

| File | SHA256 |
|---|---|
| `contexer/store.py` | `b186b9c38459e03eede7f8bff8a80c2c29c0b219e974607dce78d45a82c563d1` |
| `tests/test_task_scope_capture.py` | `049da2571f0755a1a409042049ccbcfe8907cb99731ec75848e86b58bbbeaeb4` |
| `tests/test_store.py` | `c4b349ad89b5479193152bf6b78ab70fef022228eef2b12e7ecc0d5fc21cd504` |
| `benchmarks/prompt_capture/cases.json` | `38f21e58c2e32126d953998b0aa64f405f444ee7a4d6ade053f29520d31bec11` |

## Initial patch results

| Check | Observed result |
|---|---|
| Eight material-loss regressions, before implementation | 8 failed |
| Default task-scope tests + 21 local review safety cases, final source | 122 passed (101 default + 21 local) |
| Prompt-capture benchmark | 532/532 assertions; 159 cases; fixture 1.15.0; no gaps or unexpected results |
| Related capture/authority suite + 21 local review safety cases | 1,849 passed |
| Initial full suite | **7,726 passed, 1 failed, 31 skipped, 1 xfailed; 92.76% coverage** |
| Exact failing test, fresh serial recheck | 1 passed in 2.91 seconds |
| Full-suite retry | **7,727 passed, 31 skipped, 1 xfailed; 92.76% coverage**, 367.33 seconds |
| Ruff 0.15.4 and `git diff --check` | Passed |
| MCP stdio `initialize`, isolated child home | Passed during PR preparation |

The initial failure and retry are both part of the result. A clean first-run full pass is not claimed.

Commands inside the disposable-home wrapper included:

```sh
uv run --frozen pytest -q -n auto
.venv/bin/python -m pytest 'tests/test_qualification_preparation.py::test_qualified_artifact_rejects_campaign_outcome_substitution[required_checks-value1]' --no-cov -q -n0
.venv/bin/python benchmarks/prompt_capture/run.py --format json --output <report.json>
uvx --offline ruff@0.15.4 check .
git diff --check
```

The related run selected `tests/test_task_scope_capture.py`, `tests/test_prompt_capture_benchmark.py`, `tests/test_server.py`, `tests/test_share_policy.py`, `tests/test_evidence_adapters.py`, `tests/test_evidence.py`, `tests/test_store.py`, plus 21 locally archived review cases, with `--no-cov -q -n0`. Those extra local cases are not added as a duplicate default test module; all eight required material-loss regressions are in the committed task-scope suite.

## PR review follow-up: preserve text through sanitization

[PR #406's review finding](https://github.com/bhargavamin/contexer/pull/406#discussion_r4242872249) was independently reproduced. On the initial commit, `Always hash passwords because this protects users. Write the hash with argon2id.` remained pending but became only `Always hash passwords`. The semantic sanitizer deleted the rationale and algorithm requirement before either the pending entry or its first revision was stored. The initial eight regressions lacked this filler-connector pattern, so their passes did not establish this preservation boundary.

When `ambiguous_extraction` is true, capture now bypasses semantic sanitization and uses only whitespace normalization, initial capitalization and removal of terminal punctuation. Existing length, storage and review gates still apply. Unambiguous directives keep their existing sanitizer. No persisted schema or approval transition changes.

Six new end-to-end hook regressions cover `because this`, `so that`, `hence`, `which means`, reversed sentence ordering and multiple implicit actions. They assert the complete content in both the pending entry and its first revision, human provenance, no approval timestamp and exclusion from active context. A clean directive is a negative control for unchanged sanitization; existing task-scope, plain-task, replay and promotion tests cover adjacent behavior. All eight original material-loss tests now also assert first-revision content.

Seven benchmark cases were added with independently specified outcomes and required IDs; fixture version is now **1.15.1**. No existing gold labels were changed in this follow-up.

| Check | Observed result |
|---|---|
| Existing benchmark before this fix | 532/532 assertions; 159 cases |
| Final related capture/authority suite, seven committed test modules | 1,876 passed; 19.36 seconds |
| Final prompt-capture benchmark | 573/573 assertions; 166 cases; no known gaps or unexpected results; 449.06 ms, p50 2.612 ms, p95 6.268 ms |
| In-memory restoration of unconditional semantic sanitization | 12/573 benchmark assertions failed; all 6 new safety hook cases failed; clean sanitizer control passed; production source hash unchanged |
| First follow-up full suite | **7,773 passed, 2 failed, 31 skipped, 1 xfailed; 92.76% coverage**; 389.24 seconds |
| Both failed nodes, fresh serial recheck | 2 passed; 6.25 seconds |
| Follow-up full retry with checkout frozen | **7,775 passed, 31 skipped, 1 xfailed; 92.76% coverage**; 390.50 seconds |
| MCP stdio initialization, disposable child home | Passed |
| Ruff 0.15.4 and `git diff --check` | Passed |

Test, benchmark and mutation commands ran through the disposable-child-home wrapper. Lock preparation ran directly in the checkout before the full suite:

```sh
.venv/bin/python benchmarks/prompt_capture/run.py --format json --output <report.json>
.venv/bin/python <local-sanitizer-negative-control.py>
.venv/bin/python -m pytest tests/test_task_scope_capture.py tests/test_prompt_capture_benchmark.py tests/test_server.py tests/test_share_policy.py tests/test_evidence_adapters.py tests/test_evidence.py tests/test_store.py --no-cov -q -n0
uv lock --offline
.venv/bin/python -m pytest -q -n auto
```

Before this full run, `uv lock --offline` explicitly aligned the editable package version with `pyproject.toml` (0.49.3 → 0.51.1). The lock SHA256 at full-suite start was `857af469306481ce192fcf756d1242262dd10197e419c15680daf9b6783ba97e`. The lock hash matched at the end of the run. The original initial-run failure remains disclosed below; this follow-up does not claim a pass with the tracked 0.49.3 lock.

The first follow-up full run also failed, for a separate, agent-induced reason: the validation report was edited during execution. Qualification checks hash the complete tracked checkout diff, including documentation. Comparing each failing synthetic manifest's saved candidate patch with the end-of-run patch found exactly one differing file: `docs/capture-extraction-validation.md`. Code, tests, fixtures and the prepared lock matched. Both exact nodes passed with the checkout fixed in a fresh serial process:

```text
tests/test_qualification_preparation.py::test_qualified_validator_does_not_reread_validated_exposure_history
  expected pass, received inconclusive
tests/test_qualification_preparation.py::test_contradictory_emission_summaries_remain_unknown
  QualificationError: candidate source does not match the executed retrieval checkout
2 failed, 7773 passed, 31 skipped, 1 xfailed, 10 warnings in 389.24s
```

For the full retry, all tracked files and unignored untracked inputs were frozen until execution completed; start/end tracked-patch SHA256, untracked-path list and lock SHA256 were identical. No qualification assertions, implementation, or skip rules were changed. The tracked lock was restored afterward and is excluded from the patch. Report completion also occurred after execution; the validated production/test/fixture hashes remain the ones below.

| Follow-up file | SHA256 |
|---|---|
| `contexer/store.py` | `bb25262af9fc39185dc3e62cfc1a9d005169870c47213265b109999e97a828f7` |
| `tests/test_task_scope_capture.py` | `89388d248da68e84f8b3146bb3c830b2c620d7348c032b26e3788404e0ac6de0` |
| `benchmarks/prompt_capture/cases.json` | `b96298942b3aedec2afc29623dd4ad1d1416a34224c6fc752d9b1552763a5aea` |

An exploratory reverse-order prompt, `Write the hash with argon2id because this protects users. Always hash passwords.`, does **not** set `ambiguous_extraction`: the existing work-item classifier excludes its deictic first sentence. It remains approved as `Write the hash with argon2id`. This is a pre-existing classifier admission gap, outside this sanitizer fix; the review gate is not a guarantee that every mixed message is detected. The committed reversed-order regression uses an independently recognized work item followed by the lasting rule and its rationale.

## Initial lock-consistency failure

The first full run failed:

```text
FAILED tests/test_qualification_preparation.py::test_qualified_artifact_rejects_campaign_outcome_substitution[required_checks-value1]
benchmarks.applicability.qualification_preparation.QualificationError:
  dependency lock differs from the executed environment
1 failed, 7726 passed, 31 skipped, 1 xfailed, 10 warnings in 388.70s
```

The failure occurred while building the test's synthetic qualified artifact, before its outcome-substitution assertion. During this validation phase, the checkout's `uv.lock` editable Contexer version changed from 0.49.3 to 0.51.1. The writer and exact causal timing were not instrumented. This is an observed checkout-consistency failure, not a proven external outage or an established extraction regression.

The exact test passed in a fresh isolated serial process. The full retry started with the already-updated lock and kept it unchanged throughout execution; its start/end SHA256 matched. Its output was:

```text
Required test coverage of 85% reached. Total coverage: 92.76%
7727 passed, 31 skipped, 1 xfailed, 10 warnings in 367.33s (0:06:07)
```

The tracked lock was restored afterward and is excluded from this patch. No qualification code, assertions or skip rules were changed. A future clean-checkout run may encounter the existing consistency issue again; CI should confirm the eventual integration. Earlier independent baseline and original-v2 full runs also encountered qualification identity failures and passed exact serial rechecks, while the previous reviewed full run passed. These observations do not prove the precise cause of this failure.

## Semantic and authority controls

Eight regressions cover algorithm selection, indefinite objects, salt, negation, reversed ordering, per-transaction recurrence and a default-bypass prohibition. Neighboring tests cover explicit task scope, plain tasks, multiple implicit actions, pending/approved replay, clean restatement, provenance and exclusion from active context.

Six existing benchmark expectations intentionally change to the owner-approved full-candidate review contract: `P01-rule-rationale-work-item`, `P01-labelled-rule-work-item`, `P01-rule-then-build-task`, `P01-deictic-rule-with-work-item`, `P01-trailing-here-work-item`, and `P01-work-item-question-before-rule`. The existing implicit multi-action store test also expects full pending content. Thirteen reviewer benchmark cases were added; no frozen experiment labels changed.

Process-only negative controls, with production file hashes unchanged:

| Mutation | Benchmark failures | Focused hook failures |
|---|---:|---:|
| Restore shortened text and automatic approval in both implicit branches | 82/532 | 12/12 |
| Keep full text but clear the ambiguity review flag | 30/532 | 12/12 |

The 53 supplied and 47 reviewer prompts produced zero new approvals versus baseline; 13 extra exclusion/punctuation probes also produced zero new approvals. The 11 supplied mixed cases yielded 10 full pending candidates and one pre-existing noncapture, with zero approved task-contaminated candidates. Every stored reviewer candidate in that initial probe set retained its expected material; the later PR finding demonstrates that the probe set did not cover semantic sanitization after filler connectors.

Claude, Codex, Cursor and Gemini probes retained the algorithm and sentence-boundary examples in full, pending, with human provenance. The new human pending candidate was excluded from active context and ineligible for team proposals. MCP remainder comparison found zero differences across 177 permitted input rows; this is not a claim of 177 complete MCP write comparisons. Direct MCP checks still reject caller-supplied human authorship, and AI capture remains pending and team-ineligible.

## Known limits

A semicolon-joined rule and task can still remain approved when the one-action parser removes nothing. The numeric timeout example and supplied money/refund example remain uncaptured under pre-existing trigger/action rules. Existing attribution false approvals, AI global decisions, historical shortened approvals and match/containment deduplication behavior are not redesigned here. Over-limit candidates keep the existing refusal behavior.

This is an independent source-level adversarial review informed by supplied findings, not a blind or prospective effectiveness evaluation. The local internal archive retains raw runs and earlier blocked findings; this committed report provides the reviewable results without publishing real-store inventories.
