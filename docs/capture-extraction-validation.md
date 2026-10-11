# Capture extraction: review gate and validation

Ambiguous mixed instructions retain their full candidate as `pending_approval` instead of automatically approving an extracted fragment. For example, `Always hash passwords. Write the hash with argon2id.` previously became approved `Always hash passwords`, losing the algorithm requirement. It now retains both sentences for human review.

The owner approved this contract before implementation. The same preservation boundary covers proposed removals in both implicit extraction branches. Explicit task-scope separation remains unchanged; plain work items still create no capture. No schema, global authority, model dependency, or review tool was added. Existing approvals are not silently rewritten or demoted, and a clean developer restatement can still resolve a pending candidate.

## Validation identity

Reviewed against `24b3d2fff8f2f258e1dda64c8a9e3af72c09ae43`, also the remote `main` tip when this PR was prepared. Test environment: macOS ARM64, Python 3.12.10, uv 0.11.28, pytest 9.0.3, xdist 3.8.0, pytest-cov 7.1.0, coverage 7.14.1, MCP 1.29.0, Ruff 0.15.4.

Tests ran with disposable child `HOME` and `CONTEXER_NO_UPDATE_CHECK=1`; capture probes also used isolated stores. All 797 real-store file contents and metadata matched before/after the approved fix run. The original implementation author's uncommitted worktree was unchanged. Frozen #390 task files, labels, B1/B2 runs and scores were neither inspected nor changed.

The code and fixtures below are byte-identical to the validated patch. Subsequent PR preparation adds documentation only.

| File | SHA256 |
|---|---|
| `contexer/store.py` | `b186b9c38459e03eede7f8bff8a80c2c29c0b219e974607dce78d45a82c563d1` |
| `tests/test_task_scope_capture.py` | `049da2571f0755a1a409042049ccbcfe8907cb99731ec75848e86b58bbbeaeb4` |
| `tests/test_store.py` | `c4b349ad89b5479193152bf6b78ab70fef022228eef2b12e7ecc0d5fc21cd504` |
| `benchmarks/prompt_capture/cases.json` | `38f21e58c2e32126d953998b0aa64f405f444ee7a4d6ade053f29520d31bec11` |

## Results

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

The 53 supplied and 47 reviewer prompts produced zero new approvals versus baseline; 13 extra exclusion/punctuation probes also produced zero new approvals. The 11 supplied mixed cases yielded 10 full pending candidates and one pre-existing noncapture, with zero approved task-contaminated candidates. Every stored reviewer candidate retained its expected material.

Claude, Codex, Cursor and Gemini probes retained the algorithm and sentence-boundary examples in full, pending, with human provenance. The new human pending candidate was excluded from active context and ineligible for team proposals. MCP remainder comparison found zero differences across 177 permitted input rows; this is not a claim of 177 complete MCP write comparisons. Direct MCP checks still reject caller-supplied human authorship, and AI capture remains pending and team-ineligible.

## Known limits

A semicolon-joined rule and task can still remain approved when the one-action parser removes nothing. The numeric timeout example and supplied money/refund example remain uncaptured under pre-existing trigger/action rules. Existing attribution false approvals, AI global decisions, historical shortened approvals and match/containment deduplication behavior are not redesigned here. Over-limit candidates keep the existing refusal behavior.

This is an independent source-level adversarial review informed by supplied findings, not a blind or prospective effectiveness evaluation. The local internal archive retains raw runs and earlier blocked findings; this committed report provides the reviewable results without publishing real-store inventories.
