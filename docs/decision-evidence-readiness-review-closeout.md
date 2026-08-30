# Decision evidence readiness review closeout

Status: `closeout_pending`

Created: 2026-08-30

Scope: Contexer PR #268 and Contexer Teams PR #189

Owner: next implementation/release session, with maintainer ratification where identified

## 1. Purpose

This file is the executable closeout checklist for the review of:

- `docs/decision-evidence-readiness-plan.md`
- `docs/decision-evidence-readiness-progress.md`
- Contexer branch `feat/decision-evidence-hardening`
- Contexer Teams branch `feat/decision-evidence-hardening`

The product implementation passed review, but the work must not be described as completely closed
until the durable handoff, maintainer rulings, evaluation wording, and cross-repository record match
the actual repository state.

This closeout does not reopen the deferred organizational ADR/ticket/PR knowledge-layer work or the
separate decision-sharing/review/drift-transition plan.

## 2. Verified state at review closeout

### Contexer

- Pull request: <https://github.com/bhargavamin/contexer/pull/268>
- State: merged at 2026-08-30T13:57:33Z.
- Reviewed head: `a696201f2e38b2f70353404fe4620abe4e3dd372`.
- Merge commit on `main`: `97c479c86aeaf36ab1311298fd4837b8f37bc18e`.
- All current checks are green:
  - Python 3.12: passed in both runs.
  - Python 3.13: passed in both runs.
  - Python 3.14: passed in both runs.
  - Benchmark harness: passed in both runs.
  - Ruff: passed in both runs.
  - Contexer Guard: passed.
- Independent clean Python 3.12 verification at product SHA `071204c` completed with
  `5100 passed, 22 skipped` and 94.02% coverage.
- `a696201` changes only the Python 3.14 spool regression test so that it does not depend on a
  `pathlib` implementation detail; the latest full CI matrix covers that head.

### Contexer Teams

- Pull request: <https://github.com/contexer-ai/contexer-teams/pull/189>
- State: merged at 2026-08-30T11:28:26Z.
- Reviewed feature head: `fde3408cf9a78982233ed7cedc0ebbc666c806ff`.
- Squash merge on `main`: `48b02110610901f9ef06e66ec07b6657805e7fb5`.
- The merged tree matches the reviewed feature tree.
- Verification completed with `1273 passed, 1 skipped`; typecheck, migration, production build,
  Docker CI, and Contexer Guard passed.

## 3. Blocking closeout tasks

Both product PRs merged before this closeout was executed. Do not mark the progress ledger fully
complete until the documentation follow-ups required by C01, all of C02-C04, and the C05
disposition are complete. C05 is a product change only if the maintainer chooses the stronger
behavior; Bhargav Amin selected Option A on 2026-08-30.

### C01 - Put the canonical handoff under version control

Problem:

- The canonical plan and progress ledger currently exist only as untracked files in the Contexer
  main checkout.
- They are absent from the Contexer feature branch and therefore absent from PR #268.
- The Teams-side pointer also remains untracked and is absent from the already-merged PR #189.
- This violates the plan acceptance criterion that progress be reconstructable without chat
  history.

Required actions:

1. Because Contexer PR #268 merged before this checklist was supplied, add these files without
   losing their existing history or content through a documentation/evaluation-metadata-only
   Contexer follow-up PR:
   - `docs/decision-evidence-readiness-plan.md`
   - `docs/decision-evidence-readiness-progress.md`
   - this closeout file
2. Confirm the files appear in the follow-up PR diff and at its head with `git ls-tree`.
3. After the Contexer documentation follow-up merges, open a small documentation-only Contexer
   Teams follow-up PR that
   tracks `docs/decision-evidence-readiness-plan.md` as a pointer.
4. Replace the Teams pointer's local sibling-relative links with stable absolute repository links to
   the merged Contexer documents. Include the final Contexer and Teams SHAs in the pointer so the
   cross-repository contract remains understandable even if a reader cannot follow the link.
5. Do not create a second independent progress ledger in Teams.

Evidence required:

```text
Contexer documentation follow-up PR: <URL>
Contexer plan present in reviewed follow-up tree: <yes + SHA>
Contexer ledger present in reviewed follow-up tree: <yes + SHA>
Closeout file present in reviewed follow-up tree: <yes + SHA>
Teams pointer follow-up PR: <URL>
Teams pointer merged SHA: <SHA>
```

Gate:

- A fresh clone of each repository can discover the plan and final outcome without access to this
  chat or the original local sibling-directory layout.

### C02 - Correct the ledger using additive correction entries

Problem:

The ledger completion record currently says that nothing was pushed, no PR was opened, and nothing
was merged. It also records the pre-Python-3.14-fix Contexer SHA. Those statements are no longer true.

Required actions:

1. Preserve the existing historical entries. Follow the ledger's own rule: add a correction entry
   rather than rewriting prior observations as though they never occurred.
2. Update the current summary and completion record to include:
   - Contexer head `a696201f2e38b2f70353404fe4620abe4e3dd372`.
   - Contexer PR #268 URL and current state.
   - Teams feature head `fde3408cf9a78982233ed7cedc0ebbc666c806ff`.
   - Teams PR #189 merged state and main squash SHA
     `48b02110610901f9ef06e66ec07b6657805e7fb5`.
   - The original Python 3.14 failure, its test-only cause, the `a696201` repair, and both green
     Python 3.14 reruns.
   - The final green Contexer Guard result.
3. Fill the blank baseline/evaluation summary fields near the top of the ledger, or explicitly link
   each field to the authoritative later evaluation row. Do not leave a label with no result.
4. Add the post-Task-07 installation, browser acceptance, notification repair, Python 3.14 test
   repair, PR creation, and Teams merge to the deviations/correction record.
5. Keep `Overall status` as `closeout_pending` until C01-C04 and the required part of C05 are done.
   Change it to `complete` only at the final gate.

Evidence required:

```text
Ledger correction entry date/session: <value>
Correct Contexer final SHA recorded: <yes>
Correct Teams feature and main SHAs recorded: <yes>
PR and merge states recorded: <yes>
Python 3.14 failure/fix/rerun recorded: <yes>
Deviation table populated: <yes>
Blank summary fields resolved: <yes>
```

### C03 - Ratify the five implementation choices

Disposition: complete. Bhargav Amin ratified R08-R12 exactly as proposed on 2026-08-30. The
progress ledger records the named maintainer, date, ruling and task evidence for each choice.

Problem:

Section 10 of the canonical plan says that five choices require maintainer ratification. The code
implements defaults, but the ruling log does not record explicit maintainer approval for all five.

Recommended rulings:

| ID | Question | Implemented and recommended ruling | Required record |
| --- | --- | --- | --- |
| R08 | Review-queue ceiling | Ratify 5 new materializations per reconciliation pass and 10 pending review items per repository. Raw evidence remains durable and deferred. | Maintainer name/date and measured 20/1,000-event evidence. |
| R09 | Missing legacy `repo_key` | Ratify quarantine. Missing or unverifiable identity never fails open into a local decision candidate. | Maintainer name/date and migration/recovery note. |
| R10 | Divergence audience | Ratify an author-only `sourceRetired` context marker plus content-free durable notifications to current team leads. Do not notify ordinary members. | Maintainer name/date and privacy rationale. |
| R11 | Source restoration | Ratify automatic divergence resolution when the source is restored, while retaining divergence and notification audit history. | Maintainer name/date and open/resolve/reopen test evidence. |
| R12 | Natural prompt corpus | Permit retention of the corpus as a labeled adversarial regression corpus. Do not call the current result an independently frozen holdout. | Maintainer name/date and approved storage location. |

Required actions:

1. Ask the maintainer to approve or amend R08-R12 explicitly.
2. Record each ruling in the progress ledger's decision/ruling table.
3. If any ruling is rejected, reopen only the owning task and implement/test the selected alternative.
4. Do not treat merging a PR as implicit ratification of an unresolved choice.

Maintainer response shortcut:

```text
I ratify R08-R12 as proposed in
docs/decision-evidence-readiness-review-closeout.md.
Name: <maintainer>
Date: <date>
```

### C04 - Correct the evaluation claim and protect future holdouts

Problem:

`tests/fixtures/directive_holdout/natural-prompts.json` and the classifier/evaluation changes landed
in the same commit (`d7b4d90`). The corpus is useful, but repository history cannot independently
prove that it was frozen before classifier inspection or tuning.

Required actions:

1. Rename the metric in the plan, ledger, PR description, and review notes from an independent or
   natural holdout to a `labeled natural-prompt regression corpus` unless independent provenance can
   be supplied.
2. Preserve the measured result: recall 1.00 and precision 0.9091. Change only the strength of the
   claim, not the recorded numbers.
3. Record corpus path, row count, labeling owner, date, and SHA-256 in the ledger.
4. Before the next directive-classifier change:
   - create or obtain a new natural-prompt corpus in a separate commit or external immutable
     artifact;
   - label and freeze it before the implementer sees expected classifications;
   - record its hash;
   - run it only after classifier work is complete;
   - never tune on it after results are revealed.
5. Track creation of that genuinely held-out corpus as future evaluation work; it requires no
   production-code change in the closeout follow-up.

Current corpus record after provenance-metadata correction:

- Path: `tests/fixtures/directive_holdout/natural-prompts.json`
- Rows: 24 labeled cases
- Labeling owner: Codex implementation session `01a04fa3-045d-7380-b462-f6562909594f`
- Labeling/record date: 2026-08-30
- SHA-256: `d845566f015c9c361614a0f0a845bf5733c84fa2be22870fb39c2006729933f3`
- Measured result: recall 1.00, precision 0.9091

Gate:

- No project document overstates the evidentiary independence of the current corpus.

### C05 - Disposition for newly promoted leads

Chosen disposition: Option A.
Maintainer/date: Bhargav Amin, 2026-08-30.
Follow-up issue: pending creation.
Reason: preserve the reviewed and merged product behavior while tracking promotion-time backfill as
a focused, content-free, tenant-fenced and deduplicated Teams change.

Observed limitation:

An open divergence notifies all leads present during reconciliation. A user promoted to lead later
receives the existing item only if another reconciliation touches that source. If no later lifecycle
event occurs, the new lead is not guaranteed to receive the old open item.

Choose and record exactly one disposition:

#### Option A - Accept and track the limitation (recommended for this PR)

- Keep PR #268 and merged Teams PR #189 unchanged.
- State that the current contract covers leads present when the divergence is opened/reconciled.
- Create a Teams follow-up issue for promotion-time backfill.
- The issue must require content-free, tenant-fenced, deduplicated notifications for every currently
  open divergence in the promoted lead's team.

#### Option B - Guarantee attention for every newly promoted lead

- Reopen Teams implementation work in a new PR because PR #189 is already merged.
- In the existing owner-authorized lead-promotion transaction, query open lifecycle divergences for
  the same team and create missing notifications through the existing transactional notification
  seam.
- Preserve the unique `(user_id, divergence_id)` behavior so retries cannot duplicate attention.
- Do not reveal decision content in notification title/body.
- Add PostgreSQL tests for:
  - promotion with one open divergence;
  - promotion with multiple open divergences;
  - repeated promotion/retry idempotency;
  - a member or lead from another tenant;
  - resolved divergences not being backfilled;
  - stale/deleted team decision navigation falling back safely.
- Run Teams typecheck, full PostgreSQL tests, migration check, and production build.

Required record:

```text
Chosen disposition: <A or B>
Maintainer/date: <value>
Follow-up issue or PR: <URL>
Reason: <value>
```

## 4. Final execution order

1. Record the completed R08-R12 ratification.
2. Apply C04's evaluation wording and provenance correction.
3. Update the progress ledger through additive correction entries under C02.
4. Add the canonical plan, corrected ledger, this closeout file and C04's corrected evaluation
   metadata through a documentation/evaluation-metadata-only Contexer follow-up PR under C01.
5. Re-run `git diff --check`, Ruff, and the documentation/fixture-focused tests affected by any
   wording or fixture metadata changes. If production code changes, run the complete owning suite.
6. Wait for every required Contexer follow-up PR check to pass at its documentation head, then merge
   under the maintainer's explicit approval and record its merge SHA.
7. Open and merge the Teams documentation pointer follow-up from C01.
8. Create and record the selected C05 Option A follow-up issue.
9. Run the post-merge verification commands below.
10. Use a final documentation-only Contexer update to record the Teams pointer and C05 issue, then
    change this file and the progress ledger to `complete` only when every final gate is satisfied.

## 5. Post-merge verification

Run from clean checkouts after fetching both remotes:

```bash
git -C /Users/bhargavamin/repos/personal/contexer fetch origin main
git -C /Users/bhargavamin/repos/personal/contexer-teams fetch origin main

git -C /Users/bhargavamin/repos/personal/contexer ls-tree -r --name-only origin/main -- \
  docs/decision-evidence-readiness-plan.md \
  docs/decision-evidence-readiness-progress.md \
  docs/decision-evidence-readiness-review-closeout.md

git -C /Users/bhargavamin/repos/personal/contexer-teams ls-tree -r --name-only origin/main -- \
  docs/decision-evidence-readiness-plan.md
```

Then verify:

- Contexer main contains the reviewed product tree plus `a696201`'s Python 3.14-compatible test.
- Teams main still contains PR #189's reviewed tree.
- The Teams pointer resolves without requiring sibling local repositories.
- The current progress summary and additive correction supersede historical entries that accurately
  said no PR had yet been pushed or merged at the time they were written.
- R08-R12 each have a named maintainer/date.
- The current corpus is described as a regression corpus, not an independently frozen holdout.
- C05 has a recorded issue or PR.

## 6. Final acceptance checklist

- [ ] C01 canonical Contexer documents are tracked and reviewable.
- [ ] C01 Teams pointer is tracked through a follow-up PR and uses a durable link.
- [x] C02 progress ledger matches actual SHAs, PRs, merges, fixes, and deviations.
- [x] C03 R08-R12 are explicitly ratified or their alternatives are implemented and tested.
- [x] C04 current evaluation evidence is labeled accurately.
- [x] C04 future independent-holdout work is recorded.
- [ ] C05 newly promoted lead behavior has an explicit disposition and issue/PR.
- [x] Contexer Python 3.12/3.13/3.14, benchmark, Ruff, and Guard checks pass at `a696201`.
- [x] Teams PR #189 is merged and its full validation was green.
- [x] Contexer PR #268 is merged with explicit approval and its merge SHA is recorded.
- [ ] Implementation worktrees have no uncommitted closeout changes; all pre-existing user-owned
      dirty/untracked files remain preserved.
- [ ] Progress ledger overall status is `complete`.
- [ ] This closeout status is `complete`.

## 7. Completion record

Fill this section only after every unchecked item above is satisfied.

```text
Completed at:
Completed by:
Maintainer ratification:
Final Contexer PR/head/merge SHA:
Final Teams pointer PR/merge SHA:
C05 issue or PR:
Final verification commands/results:
Residual limitations accepted by:
```
