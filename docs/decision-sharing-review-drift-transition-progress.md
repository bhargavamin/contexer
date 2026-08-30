# Decision sharing, review, and drift-transition progress

Canonical plan: `docs/decision-sharing-review-drift-transition-plan.md`
Created: 2026-08-30
Overall status: `phase_0_complete`

This is the single durable cross-repository progress ledger. Contexer Teams carries only an
immutable pointer to the plan and this ledger; it must not grow a second ledger.

## 1. Scope boundary

Phase A remained paused throughout Phase 0. Phase 0 preserved/checkpointed prior work and created
documentation-only commits/PRs; it added no production behavior, schema changes, or Phase B work.
With every Phase 0 gate complete and this status set to `phase_0_complete`, Phase A may now resume
at A1.

## 2. Fetched baselines

Both remotes were fetched on 2026-08-30 immediately before the worktrees were created.

| Repository | Fetched `origin/main` | Baseline |
| --- | --- | --- |
| Contexer | `d03a173b540317bf11b7424bccd2d5f3909c6f4e` | PR #274 merged; share help/unknown-option paths are read-only before repo/profile/outbox/network work. |
| Contexer Teams | `adea69403c21e5cd7f9536765a336817a0549bb1` | PR #194 merged; issue #191 promotion-time divergence backfill is complete baseline behavior. |

Known minimum baselines supplied by the maintainer matched the fetched refs exactly; neither main
advanced during Phase 0 setup.

Additional completed baseline records:

- Teams PR #193, merge `d765ac64764aa3e2e8f3c0602c83315d0ab0702d`: same-revision
  application/migrator deployment, immutable manual tags, digest-pinned promotion, and safe MCP
  database-error diagnostics.
- Teams PR #194, reviewed head `318529640b9b5c6d8419edff0fe2647db10f7d72`, merge
  `adea69403c21e5cd7f9536765a336817a0549bb1`: team-row serialization, source/team lock ordering,
  transactional promotion/invite/backfill rollback, content-free notifications, deduplication,
  and bounded fan-out. Verification recorded by the PR: 1,293 passed, 1 skipped; typecheck, build,
  Docker, and Guard green.
- Contexer PR #274, reviewed head `4b882a1882198207e7b3af9bb575a207876ee6d5`, merge
  `d03a173b540317bf11b7424bccd2d5f3909c6f4e`: read-only share help/argument validation. Verification
  recorded by the PR: 5,103 passed, 22 skipped; Ruff and Guard green.

## 3. Worktrees and preserved user state

Fresh implementation worktrees:

| Repository | Branch | Worktree | Start SHA |
| --- | --- | --- | --- |
| Contexer | `codex/decision-sharing-transition` | `/Users/bhargavamin/repos/personal/contexer-decision-sharing-transition` | `d03a173b540317bf11b7424bccd2d5f3909c6f4e` |
| Contexer Teams | `feat/decision-sharing-transition` | `/Users/bhargavamin/repos/personal/contexer-teams-decision-sharing-transition` | `adea69403c21e5cd7f9536765a336817a0549bb1` |

The normal main checkouts were read-only throughout Phase 0. Their pre-existing state remains
user-owned and outside both documentation commits:

- Contexer main: modified `uv.lock`; untracked applicability benchmark inputs; untracked readiness
  plan/progress/closeout shadow copies; untracked transition-plan shadow copy.
- Teams main: untracked local `.claude` settings/skills, `.superpowers`, local planning documents,
  `graphify-out`, and `skills-lock.json`.

Later cleanup requirement: after the Contexer record is approved and merged, compare the stale
normal-checkout shadow documents with the merged files and request explicit maintainer approval
before deleting, replacing, or repointing any of them. Do not clean them automatically.

## 4. Existing Phase A provenance and reconciliation

### Contexer

The only discovered uncommitted sharing-related work was in:

- worktree: `/Users/bhargavamin/repos/personal/contexer-sustainable-engineering`
- branch: `feat/share-persistence`
- original branch/base SHA: `8f1f58de46f84c6e80081787a637b4fa97ae5bda`
- original state: uncommitted extraction of explicit-share persistence plus a mechanical split of
  `tests/test_share.py`; no automatic-proposal capability fixture, policy/intent/receipt schema,
  or transition invariant test was present
- preservation checkpoint: `9093e5329a43877c036b241d756fe41803a4177e`
  (`chore: checkpoint Phase A share persistence split`)
- checkpoint validation: `git diff --check` passed; focused share modules `131 passed`

Selective replay review:

- A cherry-pick onto current `d03a173` was attempted only to expose compatibility conflicts, then
  aborted cleanly without committing or rewriting either branch.
- The checkpoint replaces large portions of current `share.py` and `test_share.py` from an August 1
  base. Current main now includes evidence/lifecycle/reconciliation ownership and PR #274's CLI
  invariants that the checkpoint predates.
- No checkpoint hunk is required to express the refreshed Phase A contract, and forcing the
  mechanical extraction would risk deleting later behavior. Therefore no Phase A code commit was
  replayed. The checkpoint remains intact as provenance/reference; Phase A will re-review and copy
  only demonstrably valid fixtures/helpers after A1 defines the refreshed contract.

### Contexer Teams

No uncommitted or committed decision-transition Phase A branch/worktree was found. Existing Teams
worktrees were clean or belonged to completed unrelated work (#191, readiness, lifecycle,
dashboard, and other features). The new implementation branch therefore starts directly from
`adea694` with no replay.

## 5. Maintainer-ratified V1 decisions

Bhargav Amin ratified these decisions on 2026-08-30:

| ID | Ruling |
| --- | --- |
| R01 | Use schema-version-1 policy, proposal-intent, receipt, and attention sidecars with independent dedicated locks. No proposal-sidecar path uses or waits on the main decision-store lock. |
| R02 | Teams exposes account-bound `automaticDecisionProposal` capability version 1 and a stable pseudonymous authenticated-account fingerprint. Policies and intents bind to a unique policy generation so old queued work cannot cross an account/destination change. |
| R03 | Automatic transition matching requires the same non-null exact canonical repository identity. Global decisions are excluded from automatic sharing and automatic transition matching in V1. |
| R04 | Replacing a blocking decision requires an explicit lead choice: keep the replacement blocking or make it advisory. There is no default and no missing-value fallback. |
| R05 | V1 permits one target team per repository, is future-only by default, requires explicit include-existing behavior, never auto-approves a team candidate, and performs no semantic/LLM transition matching. |
| R06 | PRs may be opened or updated for review, but must not be merged without Bhargav Amin's explicit approval. |

## 6. Phase 0 execution log

### 2026-08-30 - Baseline and preservation - complete

Read both repositories' complete authoritative guides; read the current transition plan; read the
completed readiness plan, progress ledger, and closeout from Contexer `origin/main`; read current
Teams architecture/security documentation; inspected PRs #193/#194/#274 and #194's affected
transaction, notification, lock-order, rollback, navigation, and concurrency tests; fetched both
remotes; inventoried all worktrees/branches/statuses; checkpointed the separable Phase A work; and
created fresh implementation worktrees from the fetched refs.

Deviations:

- The preserved Phase A checkpoint was not replayed because its mechanical extraction conflicts
  broadly with current ownership and contains none of A1/A2's feature contracts. This is a
  deliberate selective-replay result, not lost work.

### 2026-08-30 - Documentation reconciliation - complete

Refreshed the plan against current main, recorded this ledger, published Contexer commit
`c620d1e1437d9f777708447beb56e30937101390`, and added the Teams immutable pointer in commit
`b237bff7f9de6b7621175b82ac79133ffeed9e84`. Opened Contexer PR #275 and Teams PR #195 for review;
both remain unmerged under R06.

The independent reviewer reported no Critical or Important findings. Its only Minor finding was
that the phrase `outbox/drainer` could imply one shared lock; the plan now lists independent
`outbox` and `drainer` locks. The reviewer confirmed the six rulings, baselines, Phase A checkpoint,
immutable pointer, no-second-ledger boundary, dirty-checkout preservation, diff checks, and correct
A1 resume point.

## 7. Phase 0 gate

- [x] Both remotes fetched and actual baseline SHAs recorded.
- [x] Both implementation worktrees start exactly at fetched `origin/main`.
- [x] Existing Phase A work is checkpointed, its original base is recorded, and replay disposition
      is explicit.
- [x] #191/#194 is treated as completed baseline behavior.
- [x] All six maintainer rulings are recorded.
- [x] Current #193/#194/#274 and lifecycle/notification/reconciliation seams were checked.
- [x] Refreshed Contexer plan and this ledger are committed and remotely reachable.
- [x] Teams pointer is committed and resolves to immutable Contexer documentation links.
- [x] `git diff --check` passes in both implementation repositories.
- [x] Normal dirty/untracked checkouts are verified unchanged after documentation work.
- [x] Independent review has no unresolved Important finding.
- [x] This ledger records `phase_0_complete` before Phase A resumes.

## 8. Tests, review, deviations, and unresolved items

Tests and review completed:

- Preserved checkpoint: `git diff --check` passed.
- Preserved checkpoint: `uv run pytest tests/test_share.py tests/test_share_persistence.py
  tests/test_share_contract.py tests/test_share_cli.py --no-cov -q` -> `131 passed`.

- Full-range `git diff --check origin/main..HEAD` passed in both documentation worktrees.
- Contexer PR #275: Guard passed and GitHub reported a clean merge state.
- Teams PR #195: Build & test, Guard, and change detection passed; documentation-only Docker jobs
  were skipped as expected; GitHub reported a clean merge state.
- Independent review: no Critical or Important findings; the one Minor wording ambiguity was fixed.

No production test suite was required for the documentation-only Phase 0 commits. If later replay
or implementation changes production code, the owning focused and full gates are mandatory.

Unresolved items:

- None requiring a maintainer ruling in Phase 0.
- The Teams pointer must later be repointed from the reviewed immutable Contexer branch commit to
  the merged canonical Contexer record after explicit approval/merge.

## 9. First Phase A task permitted after the gate

Resume **A1 - Freeze behavior with cross-repository contract fixtures**. Re-review any helper or
fixture from checkpoint `9093e53` against the refreshed contract and current main before reuse.
Create/pin the duplicated Python/TypeScript capability, policy, intent, receipt, transition, and
resolution fixtures first; then execute A2's invariant tests. Do not begin Phase B or production
behavior until A1 and A2 are complete.
