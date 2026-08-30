# Decision sharing, review, and drift-transition progress

Canonical plan: `docs/decision-sharing-review-drift-transition-plan.md`
Created: 2026-08-30
Overall status: `phase_a_a2_complete`

This is the single durable cross-repository progress ledger. Contexer Teams carries only an
immutable pointer to the plan and this ledger; it must not grow a second ledger.

## 1. Scope boundary

Phase A remained paused throughout Phase 0. Phase 0 preserved/checkpointed prior work and created
documentation-only commits/PRs; it added no production behavior, schema changes, or Phase B work.
Phase A then completed the A1 contract and A2 executable invariant gates without changing
production behavior or adding migrations. Phase B may now begin with identity and transition reads.

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
`b237bff7f9de6b7621175b82ac79133ffeed9e84`. Opened Contexer PR #275 and Teams PR #195 for review.
Both later appeared on `origin/main` before A1 began: Contexer merge `70f6f44c5ad8adbcea902371e0d053153cc177bb`
and Teams merge `dc1a20f3c7cf567824d995962b123ef34080a365`. This task did not perform either merge.

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
- None. The Teams pointer landed in `dc1a20f` and resolves to the merged immutable Contexer record.

## 9. Phase A resume gate - completed

The first permitted task was **A1 - Freeze behavior with cross-repository contract fixtures**.
Re-review any helper or
fixture from checkpoint `9093e53` against the refreshed contract and current main before reuse.
Create/pin the duplicated Python/TypeScript capability, policy, intent, receipt, transition, and
resolution fixtures first; then execute A2's invariant tests. Do not begin Phase B or production
behavior until A1 and A2 are complete.

## 10. Phase A execution log

### 2026-08-30 - A1 cross-repository contract fixtures - complete

After the Phase 0 documentation PRs landed, A1 started in new clean worktrees from the then-current
fetched `origin/main` refs:

| Repository | Branch | Worktree | A1 start SHA |
| --- | --- | --- | --- |
| Contexer | `codex/decision-sharing-contracts` | `/Users/bhargavamin/repos/personal/contexer-decision-sharing-contracts` | `70f6f44c5ad8adbcea902371e0d053153cc177bb` |
| Contexer Teams | `codex/decision-sharing-contracts` | `/Users/bhargavamin/repos/personal/contexer-teams-decision-sharing-contracts` | `dc1a20f3c7cf567824d995962b123ef34080a365` |

The repositories now own byte-identical copies of
`decision-sharing-transition-contract.v1.json`, pinned at SHA-256
`716eb561a4c45e97bc3d9fc693dfa413894cffac93a2a63184142cab4f70c2cf`. The contract freezes:

- the additive account-bound `automaticDecisionProposal.version = 1` capability and legacy absence;
- current MCP initial/update/already-pending/unchanged/heads-changed results, including team metadata,
  plus the tenant-safe unauthorized MCP error boundary;
- schema-v1 policy, proposal-intent, receipt, and attention records with full account/repo/team/policy
  generation binding and a valid current Contexer repository slug;
- exact non-null canonical repository matching, global automatic exclusion, one scalar team target,
  future-only activation, and typed terminal attention;
- transition enrichment both with and without matching drift, with exact team/repository/predecessor
  lineage and an explicit blocking/advisory choice with no default; and
- the append-only `decision_updated` resolution's predecessor, replacement, and origin provenance.
- a required span and terminal structured-log event for every capability/proposal/transition
  operation; separate Teams span/log naming conventions; exact permitted result/reason pairs;
  closed and bounded action/result/reason/error/enforcement/re-check vocabularies; opaque operational
  correlation ids but no repository correlation; raw-exception recording disabled; local-only
  nonblocking Contexer telemetry with no exporter/in-lock flush; and value-class exclusions for
  credentials, identity/path data, decision/finding prose, raw errors, and personal/team display
  data; and
- typed bounded persisted error code/class plus an independently random, format-pinned opaque
  diagnostic id; no raw `last_error` field.

Verification:

- Contexer: 10 focused contract tests pass; Ruff 0.15.4 passes.
- Contexer Teams: 10 focused contract tests pass; the full monorepo typecheck passes.
- Fixture copies compare byte-for-byte and both pin the same digest.
- `git diff --check` passes in both A1 worktrees.
- Independent review found no remaining Critical, Important, or Minor findings after corrections
  for MCP team metadata, unauthorized error semantics, the attention schema, repo-slug validity,
  null-drift coverage, TypeScript type safety, telemetry leakage, diagnostic-id derivation,
  incomplete string taxonomies, misleading result/reason combinations, and database error classes.
- No production module, migration, A2 invariant, or Phase B behavior changed.

The next permitted task was **A2 - Write invariant tests before implementation**. It is now
complete; the Phase B production gate is open.

### 2026-08-30 - A2 executable transition invariants - complete

A2 ran in fresh worktrees from the then-current merged A1 refs:

| Repository | Branch | Worktree | A2 start SHA |
| --- | --- | --- | --- |
| Contexer | `codex/decision-sharing-invariants` | `/Users/bhargavamin/repos/personal/contexer-decision-sharing-invariants` | `2be2c60d152ea98ed2efd2f7f36b42dba81d4ebf` |
| Contexer Teams | `codex/decision-sharing-invariants` | `/Users/bhargavamin/repos/personal/contexer-teams-decision-sharing-invariants` | `0c6a4bd17a05bd144889f179ab96977dc1833241` |

Teams was then fast-forwarded, before A2 commits, to `6baf293d4475bc77c093cbd92c03c80aa3a24ef0`
after Bhargav Amin merged pointer-cleanup PR #197. This task did not merge that PR. Its only change
was to replace the provisional A1 pointer with the merged immutable Contexer A1 record and preserve
the no-automatic-merge boundary.

Both repositories now own byte-identical copies of
`decision-sharing-transition-invariants.v1.json`, pinned at SHA-256
`3c5ee30d6385ee48d15b1b111b784a86e4d296efe2f7b600b56326444a1fe761`. The fixture is an
executable preimplementation oracle bound to the merged A1 contract digest. Future production
tests in Phases B-D must load it; passing the fixture-only reference adapters is not runtime proof.
The executable scenarios prove:

- a pending candidate cannot enter Check/Guard inputs or change verdict, score, conclusion, or
  blocking authority;
- stale-predecessor approval and every injected approval-write failure leave the authoritative
  predecessor and pending replacement unchanged;
- candidate, predecessor, and finding team/repository/lineage mismatches, including every global
  variant, fail closed behind one tenant-safe refusal;
- capture and approval return without blocking, network work, or telemetry flush when each
  dedicated proposal lock is busy or unavailable;
- each independently missing legacy prerequisite preserves manual reconciliation but performs no
  automatic submit; and
- every A1 operation/result/reason and closed telemetry value is exercised through concrete spans,
  Contexer JSON stderr, and the repository's actual Teams Pino JSON-to-OTLP mapping.

Security and observability coverage injects source-bound sentinels for credentials, account
fingerprints, endpoints, repository paths/keys, decision and candidate content/rationale/source
files, finding and resolution prose, thrown exceptions, and person/team display data. It proves
none reach spans, events, stderr/stdout JSON, or OTLP records. Teams stdout uses representative
Pino infrastructure and trace fields; the test derives OTLP body, attributes, severity, timestamp,
and first-class trace correlation with the same mapping as `@contexer/observability`. Preparation
only enqueues payloads and returns; sink delivery is separate, forbidden while any listed store or
sidecar lock is held, and a throwing sink cannot change functional state or outcomes.

Verification:

- Contexer: 10 focused invariant tests pass; Ruff 0.15.4 and `git diff --check` pass.
- Contexer full suite: 5,123 passed, 22 skipped, 94.02% coverage.
- Contexer Teams: 10 focused invariant tests, the full monorepo typecheck, and all 833 non-Postgres
  tests pass; `git diff --check` passes.
- A local `pnpm test` attempt could not authenticate to PostgreSQL because this clean worktree had
  no usable `DATABASE_URL` password (`SCRAM-SERVER-FIRST-MESSAGE`). It reported 845 passing tests
  before 438 PostgreSQL-dependent failures; CI remains the authoritative database-backed gate.
- Fixture copies compare byte-for-byte and pin the same digest.
- Independent security re-review found no remaining Critical, Important, or Minor findings after
  corrections for source-bound candidate/repository sentinel coverage, concrete Pino-to-OTLP
  mapping and trace correlation, and enqueue preparation that performs no synchronous delivery.
- No production module, migration, generated metadata, or Phase B behavior changed.

Checkpoint `9093e5329a43877c036b241d756fe41803a4177e` remains intact and was not replayed: it is a broad
mechanical share-persistence split from an older base and contains none of A1/A2's feature
contracts. The normal dirty/untracked checkouts remained outside both A2 worktrees.

Next permitted task: **B1 - Add account-bound proposal capability**, followed by B2 transition
read-model work. No automatic team approval or production merge authority is implied.
