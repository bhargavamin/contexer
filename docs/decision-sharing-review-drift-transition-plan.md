# Decision sharing, review, and drift-transition implementation plan

Status: Phase 0 baseline reconciliation in progress
Originally drafted: 2026-08-28
Refreshed: 2026-08-30
Canonical progress ledger: `docs/decision-sharing-review-drift-transition-progress.md`
Repositories:

- `contexer`: `/Users/bhargavamin/repos/personal/contexer`
- `contexer-teams`: `/Users/bhargavamin/repos/personal/contexer-teams`

## 0. Phase 0 reconciliation and authoritative baselines

Phase A is paused until the Phase 0 gate in the progress ledger is complete. All implementation
work must use clean worktrees created from the latest fetched `origin/main`; the normal checkouts
contain user-owned dirty and untracked files and are not implementation surfaces.

Fetched baselines on 2026-08-30:

| Repository | `origin/main` | Baseline meaning |
| --- | --- | --- |
| Contexer | `d03a173b540317bf11b7424bccd2d5f3909c6f4e` | Includes PR #274's read-only `contexer share --help` and unknown-option validation before repository, profile, outbox, or network work. |
| Contexer Teams | `adea69403c21e5cd7f9536765a336817a0549bb1` | Includes PR #194 and therefore completed issue #191 promotion-time lifecycle-divergence notification backfill. |

Teams PR #194 is baseline behavior, not work for this plan. Preserve its team-row serialization,
source/team lock ordering, authority revalidation, transactional promotion/invite/backfill rollback,
content-free notification payloads, `(user_id, divergence_id)` deduplication, and chunked fan-out.
PR #193 is also baseline: an application revision and its migrator must come from the same checkout,
manual deployment selectors must be immutable revision tags, production promotes resolved image
digests, and MCP database failures return safe diagnostic ids rather than SQL or bound data.

Phase 0 deliverables are the refreshed tracked plan, its single Contexer progress ledger, and a
Teams-side pointer pinned to the immutable Contexer documentation commit. The Teams repository must
not contain a second progress ledger. After both documentation changes pass review, Phase A resumes
at A1; Phase B and production behavior remain blocked until the Phase A contract and invariant gate
is complete.

## 1. Problem to solve

Three user-visible problems are currently separate steps of one incomplete workflow:

1. A developer repeatedly asks an agent to share every local decision. The only lasting opt-out,
   `skip_confirm`, is a broad confirmation bypass for personal-cloud pushes; it is not a scoped
   instruction to propose approved decisions to a particular team.
2. Team leads receive candidates, but the review queue encourages blind approval. Update candidates
   show the old and new text, but not the associated Check drift, affected pull request, rationale,
   enforcement consequences, or a concise explanation of what approval will do.
3. Code can intentionally move ahead of the currently approved team decision. Check correctly
   reports drift against the old authority, but the developer must manually reconcile the local
   replacement, share it, wait for approval, resolve the finding, and request a new Check.

These are not three independent features. They are one **decision transition**:

```text
approved team decision D1
        |
        | code intentionally changes and Check records drift against D1
        v
local approved revision D2 is proposed to the team as an update of D1
        |
        | D1 remains authoritative; the drift remains real
        v
lead reviews one transition package: D1 -> D2 + rationale + files + Check/PR impact
        |
        | one approval transaction
        v
D2 becomes approved, D1 is superseded, the old drift gets a durable disposition,
and Check is queued to evaluate the code against D2
```

The goal is to remove repetitive mechanics without weakening team governance.

## 2. Required product behavior

### 2.1 Developer experience

After one explicit setup, a developer can say, in effect:

> For this repository, propose each locally approved decision revision to Team X.

From then on:

- Contexer does not ask which decisions to share after every session.
- Only a new revision whose current revision carries explicit human-approval provenance is eligible;
  an implicitly approved scan/bootstrap/AI/plan revision is not enough.
- Pending, suggested, ignored, global, or unchanged revisions are not automatically proposed in V1.
- Secret redaction is still applied at egress.
- A failed upload stays in a durable queue and is visible in status output.
- Changing account, endpoint, repository identity, or target team pauses the policy instead of
  sending to an ambiguous destination.

Enabling the policy must not silently send historical decisions. By default, activation records the
current revisions as a baseline and applies only to later approved revisions. An explicit
`--include-existing` option may preview and queue the existing set.

### 2.2 Lead experience

The review page separates:

- **Decision transitions**: update candidates that replace an approved decision. These require
  individual review and cannot be included in Select all or bulk approval.
- **Routine additions**: initial candidates that do not replace team authority. Existing batch
  review may remain for these.
- **Higher-risk transitions**: a transition replacing a blocking decision, containing risky text,
  or associated with high-severity drift. These remain individual and show the consequence of each
  approval option before the lead acts.

A transition card must answer, without making the lead reconstruct it manually:

- What approved decision is being replaced?
- What changed between the old and proposed decision?
- Why is the change proposed?
- Which repository and source files does it govern?
- Which recent Check drift and pull request does it relate to?
- Is the old decision advisory or blocking?
- What remains authoritative before approval?
- What exactly happens after approval, including whether Guard remains blocking and whether a
  re-check is queued?

### 2.3 Check behavior

While D2 is pending:

- D1 remains the only authority.
- A finding against D1 remains `drifts`; do not add `transition_pending` to the finding verdict
  vocabulary.
- Scores, suppression, and Guard conclusions remain unchanged.
- The private Teams dashboard may derive and display **Decision update pending review** when an exact
  pending candidate supersedes the finding's decision in the same team and repository.
- A blocking D1 remains blocking. A pending candidate must never turn a red gate green.

After D2 is approved:

- D2 is approved and D1 is tombstoned in the same database transaction.
- The predecessor's drift receives an append-only `decision_updated` resolution that references D2.
- Historical Check rows and findings remain immutable.
- A new Check is queued for the current affected pull-request head. Compliance is determined only by
  that new run; approval itself must not claim the code complies.
- If the replacement is advisory, stale Guard failures caused only by D1 can be reconcluded after the
  transaction, and the full Check is still queued.
- If the lead explicitly preserves blocking enforcement, the old red run must not be turned green
  before D2 is evaluated. The re-check replaces that result.

If the lead rejects or the author withdraws D2, D1 remains active and the derived pending-transition
badge disappears. The original drift remains unresolved.

## 3. Invariants that implementation must preserve

1. **Pending is not policy.** A `shared_candidate` never enters MCP context, Check context, policy
   evaluation, suppression, scoring, or enforcement.
2. **Exact matching only.** A pending transition matches a drift only when
   `candidate.supersedesDecisionId == finding.decisionId`, both belong to the same team, and their
   canonical repository scopes are compatible. Do not use titles, embeddings, or an LLM to infer the
   link.
3. **Old authority stays live until approval.** No optimistic supersession and no temporary removal
   while a candidate waits.
4. **Approval is atomic in the database.** Candidate approval, predecessor tombstone, enforcement
   choice, transition resolution, and transition audit either all commit or none commit.
5. **Network work is after commit.** GitHub check updates, notifications, email, and runner launches
   are best-effort follow-up work and must not hold a database transaction open.
6. **Capture and editor hooks do no network I/O.** They may append a small durable intent under a
   dedicated lock. Upload runs in a one-shot background drainer and lifecycle fallbacks.
7. **No main store lock for proposal sidecars.** Schema-version-1 policy, proposal-intent, receipt,
   and attention sidecars each use independent dedicated lock slugs. No proposal-sidecar path may
   acquire, use, or wait on the main decision-store lock.
8. **At-least-once transport, effectively-once submission.** The local queue may retry, while the
   existing server idempotency key and a local revision receipt prevent duplicate candidates.
9. **No automatic team approval in V1.** Remembering a developer's proposal preference must not
   become a remembered lead approval.
10. **No historical rewrite.** Old Check findings continue to describe what was judged at that time;
    later resolution and replacement links explain what happened next.
11. **Tenant fences at every read and write.** Candidate, predecessor, finding, check, replacement,
    team, and actor must be verified server-side even when the UI already supplied them.
12. **Existing manual workflows continue to work.** `contexer share`, `contexer reconcile`, normal
    candidate approval, and the three current finding resolutions remain backward compatible.

## 4. Existing code to reuse

### 4.1 Contexer

- `contexer/store.py`
  - `_share_projection` already provides revision identity, status, source files, provenance, and
    egress redaction.
  - `approve_decision` is the local human-ratification seam.
  - `get_shareable_all` currently includes all non-ignored statuses; the automatic policy must use a
    stricter eligibility helper rather than this broad manual-sharing list.
- `contexer/share.py`
  - `prepare_reconciliation` and `submit_reconciliation` already implement team target selection,
    authoritative preview, CAS heads, idempotency, atomic server submission, and a confirmed
    reconciliation outbox.
  - Reuse the wire body and refusal taxonomy, but do not force an automatic producer to create a
    network-derived preview before it can record its local intent.
- `contexer/cli.py`
  - PR #274 made `share_cmd` help and option validation strictly read-only and earlier than repo,
    profile, outbox, or network work. New policy commands must preserve the same parse-before-effects
    boundary.
- `contexer/remote.py`
  - `get_capabilities`, `list_teams`, `preview_decision_reconciliation`, and
    `submit_team_decision` are the required remote primitives.
- `contexer/adapters/claude.py` and sibling adapters
  - SessionStart, PreCompact, SessionEnd where supported, and UserPromptSubmit fallback already form
    the deterministic lifecycle surface. Do not restore a Stop hook.
- `contexer/sidecars.py`
  - Every new durable file and lock must be declared here so cleanup behavior is explicit and tested.

### 4.2 Contexer Teams

- `packages/db/src/decisions.ts`
  - `submitTeamDecisionAtomic` already creates an update candidate with
    `supersedesDecisionId` and prevents a second pending candidate for the same source/team.
  - `setDecisionState` already approves the candidate and tombstones the predecessor in one
    transaction.
  - `listUnclusteredCandidates` already returns the predecessor title and content.
- `packages/db/src/ci-resolutions.ts`
  - Resolutions are already append-only, team-owned, and separate from OSS-synced decision state.
- `packages/db/src/ci-jobs.ts`
  - `requestManagedCheckRerun`, `canRerunManagedCheck`, and the durable managed-job row are the
    re-check seam.
- `apps/web/lib/guard-reconclude.ts`
  - `reconcludeDemotedGuardRuns` already removes stale red Guard conclusions after a gate is lowered.
- `apps/web/components/ReviewQueue.tsx`
  - The UI already recognizes update candidates and offers old/new comparison, but bulk selection
    currently includes those rows and the transition context is incomplete.
- `apps/web/app/actions.ts`
  - `approveDecisionAction`, `resolveFindingAction`, and `rerunCheckWithHigherCapAction` establish the
    authorization, rate-limit, audit, notification, and revalidation patterns.
- `packages/db/src/decision-divergence.ts`, `packages/db/src/teams.ts`,
  `packages/db/src/invites.ts`, and `packages/db/src/notifications.ts`
  - PR #194 completed issue #191. Transition work must preserve source-row and sorted team-row
    serialization, team-before-invite lock order, in-transaction role/backfill rollback, tenant and
    current-lead fencing, content-free payloads, idempotent dedupe, and bounded notification chunks.
- `.github/workflows/deploy.yml`, `.github/workflows/deploy-prod.yml`, and
  `apps/mcp-server/src/tools.ts`
  - PR #193 established same-revision application/migrator deployment, immutable tag and digest
    promotion, and safe MCP database-error diagnostics. New migrations must travel through that
    release contract.

## 5. Target design

### 5.1 A remembered proposal policy, not a global confirmation bypass

Add a per-repository, single-target policy in Contexer:

```json
{
  "schema_version": 1,
  "mode": "propose_approved",
  "policy_generation": "uuid",
  "repo_key": "github.com/org/repo",
  "repo_slug": "...",
  "endpoint": "https://mcp.contexer.ai/mcp",
  "account_fingerprint": "stable-pseudonymous-id",
  "team_id": "uuid",
  "team_name_at_confirmation": "Platform",
  "enabled_at": "2026-08-28T12:00:00Z",
  "include_existing": false,
  "baseline_revision_ids": ["..."],
  "paused_reason": null
}
```

V1 modes:

- `manual` (default, represented by no active policy)
- `propose_approved`

V1 deliberately supports one target team per repository. Fan-out to multiple teams is a separate
explicit feature; it must not emerge accidentally from repeated configuration.

The policy is standing authorization to **propose**, not to approve. Creating or changing it is an
outward-action decision and requires a preview followed by explicit confirmation. `skip_confirm`
does not bypass this first-time policy confirmation.

### 5.2 Account binding

The current local token is opaque and the current caches cannot identify which Teams account they
belong to. Do not key an automatic outward policy by endpoint alone.

Extend Teams `get_capabilities` with a stable, opaque pseudonymous `accountFingerprint` bound to the
authenticated account. Advertise `automaticDecisionProposal.version = 1` so a new Contexer client
can fail closed against an older server. Existing clients ignore the additive response. Token
rotation for the same account must preserve the fingerprint; an account change must change it.

Contexer stores the endpoint, fingerprint, canonical repo key, immutable target team id, and unique
policy generation in the policy and every queued intent. Before every send it re-fetches
capabilities and team membership. A mismatch pauses the policy and moves the intent to attention.
Disable/re-enable creates a new generation, so old intents never cross an account or destination
change without explicit include-existing/migration consent. Manual reconciliation remains available.

### 5.3 Eligibility and receipts

Introduce one pure eligibility function; do not reuse `_shareable_entries`:

```text
eligible when:
  entry is a decision
  current status == approved
  current revision has explicit human approval provenance
  current revision has a non-empty revision_id
  decision belongs to the configured repository (not the global store)
  revision is newer than the activation baseline
  no terminal or successful receipt exists for
    (endpoint, account fingerprint, repo key, team id, decision id, revision id)
```

`suggested` is excluded in V1 even though it is retrievable. It has not passed the same explicit
human-ratification seam as `approved`. A suggested item can become eligible after a human approves
or edits it into an approved revision.

Receipts use these states:

- `queued`
- `submitted` with candidate id
- `already_pending` with candidate id
- `unchanged`
- `attention` with a typed terminal reason
- `baseline` for revisions present when the policy was enabled without `--include-existing`

Rejected or withdrawn candidates are not automatically re-proposed at the same revision. A new
local revision is required.

### 5.4 Intent queue and asynchronous delivery

Do not put automatic intents directly into the existing confirmed reconciliation outbox. That
outbox stores a post-preview operation with head tokens; an automatic intent must survive offline
before a preview is possible, and head tokens must be fresh at send time.

Add a separate durable proposal-intent queue. An entry contains only stable local intent:

```json
{
  "schema_version": 1,
  "idempotency_key": "uuid",
  "policy_generation": "uuid",
  "decision_id": "local decision uuid",
  "revision_id": "local revision uuid",
  "repo_path": "/local/path-at-enqueue-time",
  "repo_key": "github.com/org/repo",
  "endpoint": "...",
  "account_fingerprint": "...",
  "team_id": "...",
  "queued_at": "...",
  "attempts": 0,
  "last_error": null
}
```

At drain time:

1. Resolve the current policy and verify its destination binding.
2. Re-read the exact local decision and revision. If the decision is ignored, superseded locally,
   or no longer approved, mark the intent stale and do not send it.
3. Build the redacted projection.
4. Fetch fresh capabilities and reconciliation preview.
5. Require atomic reconciliation support; the automatic path must not use the legacy two-write
   compatibility path.
6. Submit using the queued idempotency key.
7. Persist the typed receipt before removing the queue item.
8. On CAS/head change, refresh preview and retry once. On transient failures, keep the item. On
   membership, account, repo, validation, or policy mismatch, move it to attention and pause when
   appropriate.

Queue append and receipt writes use dedicated locks and atomic replacement/append patterns. No
network request occurs while any sidecar or decision-store lock is held.

After a successful enqueue, start a best-effort detached one-shot drainer. A non-blocking drainer
lock ensures concurrent captures start at most one uploader. This keeps the editor path limited to a
small local append. Deterministic fallbacks scan/enqueue missing eligible revisions and drain queued
work at SessionStart, PreCompact, SessionEnd where supported, and explicit CLI flush. The first
UserPromptSubmit fallback may perform the bounded local scan, but must not wait on network.

### 5.5 Derived transition context in Teams

Do not add a new transition table for the pending state. The current data already contains the
durable transition object:

- candidate `state = shared_candidate`
- candidate `supersedesDecisionId = D1.id`
- candidate `sourceDecisionId` links back to the personal source

Add a DB read that enriches update candidates with:

- predecessor title, content, rationale, source files, enforcement, and enforcement reason
- candidate rationale and source files
- latest matching drift finding, Check id, repo, PR number, commit SHA, severity, explanation, and
  remediation
- count of other recent affected Check findings for the same predecessor/repository

The private Check drill-in should use the same exact-match helper to show a pending-transition badge.
The underlying finding object and verdict do not change.

Repository compatibility rules:

- candidate, predecessor, and finding must carry the same non-null exact canonical repo key
- global predecessor/candidate decisions do not participate in automatic transition matching or
  automatic sharing in V1
- a repo-bound candidate must never match a finding from another repo

### 5.6 Durable resolution after approval

Extend `ci_decision_resolutions` additively:

- allow resolution value `decision_updated`
- add nullable `replacement_decision_id` referencing `decisions.id` with `ON DELETE SET NULL`
- add nullable `origin_check_id` referencing `ci_checks.id` with `ON DELETE SET NULL`
- add nullable `origin_finding_id` referencing `ci_check_findings.id` with `ON DELETE SET NULL`

Add a database check constraint:

```text
resolution == decision_updated  => replacement_decision_id is not null
resolution != decision_updated  => replacement_decision_id is null
```

`decision_updated` is explanatory history only. It does not suppress Check input, flag a live
decision as outdated, change confidence, or affect scoring. Existing readers must continue to give
special behavior only to `false_positive` and `outdated`.

### 5.7 Atomic transition approval

Add a dedicated DB workflow, for example `approveDecisionTransition`, rather than composing public
helpers that each open their own transaction.

Inside one serializable/row-locked transaction:

1. Verify the actor is authorized by the caller and re-fence every row by `teamId` in the query.
2. Lock the candidate and require live `shared_candidate` state.
3. Require a non-null predecessor and lock it.
4. Require the predecessor to be live, `team_approved`, in the same team, and exactly equal to the
   candidate's `supersedesDecisionId`. Unlike the generic current approval helper, a stale transition
   must fail closed rather than approve without a swap.
5. Validate an optional origin finding/check belongs to the same team, references the predecessor,
   has verdict `drifts`, and is repository-compatible.
6. Approve D2 and tombstone D1.
7. Apply the lead's explicit enforcement choice.
8. Insert the `decision_updated` resolution for D1 -> D2.
9. Record an atomic `decision.transition_approve` audit entry containing predecessor, replacement,
   enforcement choice, repo, and origin Check where present.
10. Return the affected predecessor, replacement, enforcement change, and recent Check targets for
    after-commit work.

Refactor the transaction-aware mutation internals from `setDecisionState` as needed; do not call
`setDecisionState` from inside another transaction.

Enforcement choices:

- If D1 is advisory, D2 is advisory.
- If D1 is blocking, the UI must require the lead to choose either:
  - **Keep Guard blocking**: D2 becomes blocking with a new enforcement epoch. Do not reconclude the
    old red run green; queue a full Check and keep the gate red until D2 is evaluated.
  - **Make the replacement advisory**: D2 becomes advisory. After commit, stale Guard runs that
    failed only D1 may be reconcluded, and a full Check is still queued.

Do not silently copy blocking status and do not silently demote it. The choice is security-relevant,
individual, and audited.

### 5.8 After-commit re-check

After the transaction commits:

1. Notify the author and team using the existing notification patterns.
2. Revalidate the review queue, decision page, and Check drill-in.
3. For advisory replacement, call `reconcludeDemotedGuardRuns(teamId, predecessorId)`.
4. Treat the origin Check tuple as provenance, then resolve the latest known managed head for that
   affected pull request and queue that head rather than re-running a stale origin commit.
5. Identify the latest managed head for every other affected open pull request, up to the existing
   bounded fan-out cap, and queue those. Leave excess work for a durable/background fan-out
   mechanism rather than an unbounded web request.
6. Call `triggerManagedCheckRunner` for reopened jobs. If launch fails, report **approval committed;
   re-check queued but runner launch failed**. Never invite the lead to approve again.

The UI should distinguish these states:

- `Transition approved; re-check queued`
- `Transition approved; a check is already running`
- `Transition approved; no managed check is available`
- `Transition approved; re-check is queued but runner launch needs retry`

## 6. Repository implementation tasks

### Phase 0 - Baseline reconciliation and durable handoff

1. Fetch both remotes and record the actual `origin/main` SHAs.
2. Inventory all Phase A worktrees, branches, commits, bases, and uncommitted changes without
   touching either dirty normal checkout.
3. Checkpoint cleanly separable uncommitted Phase A work on its existing branch. Review it against
   current main; replay only still-valid Phase A-owned commits into the fresh implementation branch.
4. Refresh this plan against current function/file names and the completed #193/#194/#274 behavior.
5. Create the canonical progress ledger in Contexer and record the six maintainer rulings below.
6. Commit and publish the Contexer documentation first, then add a Teams pointer using absolute
   links pinned to that immutable commit and record both baseline SHAs plus #194 baseline status.
7. Run `git diff --check` in both repositories and obtain independent review with no unresolved
   Important finding before marking Phase 0 complete.

Maintainer-ratified V1 contract:

1. Schema-version-1 policy, proposal-intent, receipt, and attention sidecars use independent
   dedicated locks and never the main decision-store lock.
2. Teams advertises account-bound `automaticDecisionProposal` capability version 1 with a stable
   pseudonymous authenticated-account fingerprint.
3. Transition matching uses exact canonical repository identity; global decisions are excluded
   from automatic sharing and automatic transition matching in V1.
4. Replacing a blocking decision requires an explicit `blocking` or `advisory` lead choice, with no
   default and no missing-value fallback.
5. Each repository has at most one destination team, activation is future-only by default,
   include-existing is explicit, team approval is never automatic, and transition matching is not
   semantic/LLM-driven.
6. PRs may be opened or updated for review, but may not be merged without Bhargav Amin's explicit
   approval.

### Phase A - Cross-repository contracts and tests first

#### A1. Freeze behavior with contract fixtures

Create shared JSON fixtures, duplicated intentionally in each repository's tests, for:

- capability response with `automaticDecisionProposal.version = 1` and account fingerprint
- initial, update, already-pending, unchanged, stale-head, and unauthorized reconciliation outcomes
- policy/intent/receipt schema version 1
- transition candidate enrichment shape
- `decision_updated` resolution shape

Each repository owns and validates its copy. Add a comment naming the other copy and a test that
pins required fields. Do not introduce a package dependency between the Python and TypeScript repos.

#### A2. Write invariant tests before implementation

At minimum, prove:

- pending candidate never changes Check context or Guard conclusion
- stale predecessor cannot be transition-approved
- cross-team/cross-repo candidate cannot match a finding
- approval rollback preserves D1 and leaves D2 pending
- capture/approval returns even when the proposal queue lock is busy or unavailable
- automatic sending refuses an old server without atomic reconciliation and account binding

### Phase B - Contexer Teams: identity and transition reads

#### B1. Add account-bound proposal capability

Files:

- `contexer-teams/apps/mcp-server/src/tools.ts`
- related MCP server tests
- `contexer/remote.py` and `tests/test_remote.py` in the client follow-up

Add an authenticated, stable pseudonymous account fingerprint to `get_capabilities` and advertise
automatic proposal support. Keep the response additive. Never log or return the bearer token.

#### B2. Add transition read model

Files:

- new `contexer-teams/packages/db/src/decision-transitions.ts`, or an equivalently cohesive DB module
- `contexer-teams/packages/db/src/index.ts`
- `contexer-teams/packages/db/src/decisions.ts` only for shared private helpers if necessary
- DB tests using PostgreSQL fixtures

Implement exact pending-transition lookup and an enriched review-queue read. Keep all SQL in
`@contexer/db`. Canonicalize repository keys with the existing helper. Bound recent Check history.

#### B3. Reshape the review queue

Files:

- `contexer-teams/apps/web/app/dashboard/page.tsx`
- `contexer-teams/apps/web/components/ReviewQueue.tsx`
- focused component/action tests

Render transitions before routine additions. Remove update candidate ids from `BulkReviewBar`,
Select all, and bulk action payloads. Show rationale, deterministic field differences, source files,
enforcement impact, and matching Check/PR context. Keep risky text visible while collapsed.

Do not generate a prose summary with an LLM. The candidate, predecessor, and finding already provide
the facts; deterministic labels are auditable and cheaper.

#### B4. Add pending-transition context to private Check views

Files:

- the DB read used by `getCheckWithFindings`
- Check drill-in components/pages
- tests for score/verdict invariance

Add derived metadata beside the finding. Do not modify `ci_check_findings.verdict`, the Check score,
the GitHub conclusion, suppression sets, or blocking-id reads. Do not expose candidate content in a
public GitHub comment in V1.

### Phase C - Contexer Teams: atomic transition approval

#### C1. Add the resolution migration

Files:

- `contexer-teams/packages/db/src/schema.ts`
- generated migration in `contexer-teams/packages/db/migrations/`
- generated snapshot/journal under `packages/db/migrations/meta/`
- `contexer-teams/packages/db/src/ci-resolutions.ts`
- resolution and migration tests

Run the repository's migration generator; do not hand-maintain only the schema. Update
`RESOLUTION_VALUES`, input/result types, current-resolution reads, metrics classifications, comments,
and every exhaustive switch. Confirm that `decision_updated` has no suppression or outdated effect.

#### C2. Add atomic DB workflow

Files:

- `contexer-teams/packages/db/src/decision-transitions.ts`
- `contexer-teams/packages/db/src/decisions.ts` for extracted transaction-aware helpers
- `contexer-teams/packages/db/src/audit.ts`
- `contexer-teams/packages/db/src/index.ts`
- DB concurrency/rollback tests

Implement `approveDecisionTransition` with the transaction described in section 5.7. Add the audit
vocabulary. Preserve the generic `setDecisionState` behavior for routine candidates.

#### C3. Add web action and after-commit processing

Files:

- `contexer-teams/apps/web/app/actions.ts`
- `contexer-teams/apps/web/lib/guard-reconclude.ts` only if a generalized helper is required
- managed Check job helpers/tests

Add `approveDecisionTransitionAction`. Follow require-user, require-lead, validate, rate-limit,
mutate, then after-commit side effects. Return a structured partial-success result so a runner launch
failure is not reported as an approval failure.

Routine `approveDecisionAction` must reject/update-route transition candidates so an old UI or forged
form cannot bypass the enforcement choice and atomic resolution.

#### C4. Update review actions

Transition cards call only the new action. For a blocking predecessor, require an explicit
enforcement selection; do not default a missing value. Reject and withdraw continue using their
existing paths and leave D1 unchanged.

### Phase D - Contexer: remembered proposal policy

#### D1. Add sidecar declarations and pure policy module

Files:

- `contexer/sidecars.py`
- new `contexer/share_policy.py`
- new `tests/test_share_policy.py`
- `tests/test_sidecars.py`

Declare durable policy, proposal-intent, receipt, attention, and lock files. Keep policy parsing,
destination matching, eligibility, baseline creation, queue folding, and status rendering in the new
cohesive module. Malformed durable queues must never be overwritten as empty; move/refuse with a
clear diagnostic.

Suggested sidecar kinds:

- `.team_share_policy_{slug}.json`
- `.team-proposal-outbox.json`
- `.team-proposal-receipts.jsonl`
- `.team-proposal-attention.json`
- dedicated lock slugs for policy, outbox/drainer, receipts, and attention

Use existing `atomic_write` and sidecar naming. Cap queue size and JSONL growth, compact under the
same lock used by append, and preserve records racing a drain.

#### D2. Extend remote capability parsing

Files:

- `contexer/remote.py`
- `tests/test_remote.py`

Parse the new capability and account fingerprint strictly but additively. Manual sharing works when
it is absent. Automatic policy setup/send refuses with a typed unsupported status.

#### D3. Implement automatic intent production

Files:

- `contexer/share_policy.py`
- `contexer/server.py`
- `contexer/cli.py`
- narrowly scoped seams after successful local approval/update

After a human-approved revision is committed locally, evaluate the active policy and append an
intent. Do this after the store mutation returns, not inside its lock. Add a deterministic scanner as
a backstop so a missed wrapper loses promptness, not the decision.

The scanner compares current eligible revisions to baseline/receipts. It is local-only, bounded by
the store's decision cap, idempotent, and fail-soft. It must not treat the manual broad shareability
list as automatic eligibility.

#### D4. Implement one-shot drainer

Files:

- `contexer/share_policy.py`
- `contexer/share.py` for reuse/extraction of reconciliation wire helpers
- `contexer/share_status.py`
- remote/share/policy tests

The drainer obtains fresh preview/head tokens at send time, requires atomic server support, applies
redaction, and records a receipt before queue removal. Start it detached after enqueue; serialize
workers with a non-blocking dedicated lock. Never run network work under a store or sidecar lock.

Reuse the existing transient-error taxonomy. Preserve confirmed manual reconciliation outbox
semantics unchanged.

#### D5. Add user controls

Files:

- `contexer/cli.py`
- `contexer/server.py`
- CLI/MCP tests and help text

Add:

```text
contexer share-policy show
contexer share-policy enable --team NAME_OR_ID [--include-existing]
contexer share-policy disable
contexer share-policy flush
contexer share-policy attention
contexer share-policy retry <intent-id>
```

MCP equivalents should support agent-driven setup, but `enable` and target changes use a two-call
preview/confirm contract. The preview names the endpoint, account fingerprint suffix, canonical
repository, immutable team id/name, future-only versus include-existing scope, count, and redaction
state. The agent cannot bypass confirmation with `skip_confirm`.

Status output must distinguish queued, uploading, pending lead review, already current, attention,
and policy paused. Never say “shared” when only a local intent exists.

#### D6. Wire lifecycle fallbacks without adding a Stop hook

Files:

- `contexer/adapters/claude.py`
- `contexer/adapters/codex.py`
- `contexer/adapters/gemini.py`
- Cursor adapter only where its supported prompt/session surfaces allow
- install/plugin hook tests

Use SessionStart, PreCompact, SessionEnd where each host supports them, plus a bounded local-only
UserPromptSubmit scan fallback. Do not promise post-write parity on Cursor and do not add network
latency to every prompt. Lifecycle commands may start the detached drainer and return immediately.

### Phase E - Cross-repository integration and rollout

#### E1. End-to-end scenarios

Run against local Teams with two users, one member and one lead:

1. Enable future-only policy; prove old revisions are baseline and a new approved revision becomes a
   candidate without another prompt.
2. Produce Check drift against advisory D1, submit D2, and prove the dashboard annotates but does not
   alter the finding.
3. Approve D2 and prove D1 tombstone + D2 approval + resolution + audit are atomic; prove a re-check
   is queued.
4. Repeat with blocking D1 and both explicit enforcement choices.
5. Reject D2 and prove D1 remains authority and drift remains.
6. Go offline before approval, prove local queue durability, then reconnect and drain once.
7. Re-login as a different account at the same endpoint and prove the policy pauses before egress.
8. Remove team membership and prove no data is sent to another or similarly named team.
9. Race two drainers and two lead approvals; prove one candidate and one committed transition.
10. Use an older Teams server and prove manual reconcile works while automatic policy fails closed.

#### E2. Deployment order

1. Deploy Contexer Teams additive capability, read model, migration, and approval workflow first.
2. Verify old Contexer clients continue to share/reconcile normally.
3. Release the Contexer client with policy disabled by default.
4. Enable the UI transition grouping for all teams after DB/query monitoring is stable.
5. Offer explicit policy setup; do not auto-migrate `skip_confirm` into a share policy.

#### E3. Rollback

- Contexer: disable or remove the local policy; queued intents remain inspectable and manual workflows
  remain available. Do not silently discard queued work.
- Teams UI: turn off transition grouping and fall back to normal candidate rows.
- Teams DB: leave additive resolution columns/value in place during rollback. Old code must tolerate
  the extra rows or be deployed with a compatibility read first; do not attempt a destructive down
  migration during an incident.
- Enforcement: pending candidates never changed gates, so disabling the feature cannot require gate
  repair. Already approved D2 decisions remain ordinary approved decisions.

## 7. Test matrix

### Contexer unit/integration tests

- policy create/show/change/disable and required confirmation
- future-only activation baseline and explicit include-existing
- approved-only eligibility; pending/suggested/ignored/global excluded
- one receipt per destination + decision + revision
- new revision after submitted/rejected/withdrawn receipt becomes eligible
- endpoint/account/repo/team mismatch pauses before wire call
- secret redaction parity between preview and submitted body
- corrupted queue refuses overwrite and surfaces attention
- queue/receipt concurrent writers and compaction race
- busy lock keeps capture/approval responsive
- detached drainer singleton behavior
- transient retry, terminal attention, CAS refresh-once, idempotent replay
- old server/manual compatibility
- no Stop hook and no network in UserPromptSubmit/PostToolUse paths

Run focused tests with `--no-cov`, then the full suite and lint per the repository guide.

### Contexer Teams DB tests

- exact same-team/same-repo pending match
- no title/content fuzzy match
- global versus repo compatibility rules
- no cross-tenant existence leak
- candidate enrichment uses bounded latest drift
- pending candidate absent from Check context and blocking-id sets
- `decision_updated` constraint and replacement FK behavior
- `decision_updated` has no suppression/outdated effect
- atomic approve/supersede/resolution/audit commit
- rollback at every mutation boundary
- stale/rejected/withdrawn candidate refusal
- double-approval race
- explicit blocking preservation versus advisory replacement
- origin check/finding ownership and verdict validation

### Contexer Teams web/worker tests

- update candidates excluded from select-all and bulk action server path
- transition card shows rationale, diff, enforcement, Check/PR context
- missing blocking-enforcement choice rejected server-side
- pending badge does not alter verdict, score, or conclusion
- post-commit rerun outcomes and honest partial-success messages
- advisory replacement reconcludes; blocking preservation does not prematurely green
- notification/audit/revalidation behavior
- public GitHub output does not leak pending candidate text

Run the package tests, typecheck, lint, generated migration checks, then the full monorepo suite using
the commands documented in `contexer-teams/CLAUDE.md`.

## 8. Acceptance criteria mapped to the three reported problems

### Decision share fatigue

- A developer explicitly enables one repo/team policy once.
- The next locally approved revision reaches the team candidate queue without another share prompt.
- No historical, pending, suggested, ignored, global, wrong-account, or wrong-team decision leaves
  the machine unexpectedly.
- Failures remain visible and retryable without blocking capture or editing.

### Blind team approval

- Transition candidates cannot be bulk-approved.
- The lead sees old versus proposed authority, rationale, source files, drift/PR context, provenance,
  risk, and enforcement consequence before acting.
- A blocking transition requires an explicit security choice.
- Routine additions remain batchable so the safety improvement does not create unnecessary fatigue.

### Drift during an intentional decision change

- While D2 is pending, Check continues to show real drift against D1 and Guard behavior is unchanged.
- The private UI explains that a precisely linked decision update is awaiting review.
- One lead action atomically approves D2, supersedes D1, records why the old drift was resolved, and
  queues re-evaluation.
- Historical findings remain available, and only the new Check can declare compliance with D2.

## 9. Explicit non-goals

- Automatically approving team candidates
- Allowing an agent or model to approve on behalf of a lead
- Treating pending text as authority or using it in allow/warn/block evaluation
- Suppressing drift merely because a replacement is pending
- Rewriting historical Check findings or scores
- Guessing transition links from semantic similarity
- Automatically publishing to multiple teams
- Automatically sharing global decisions
- Adding a Stop hook
- Running network uploads from PostToolUse, editor, or every-prompt hooks
- Replacing the separate first-class policy evaluation API; this plan only improves the lifecycle of
  the decisions that such an API may later evaluate

## 10. Metrics and operational signals

Instrument without storing code or prompt bodies:

- share-policy enabled/paused count by version and typed reason
- proposal intents queued, submitted, replayed, attention, and age-to-submission
- per-revision duplicate-candidate prevention
- transition candidates with/without matching drift context
- median transition review time
- individual versus bulk approvals, split by initial/update candidate
- transition approval outcomes and enforcement choices
- time from transition approval to rerun queued and rerun completed
- rerun runner-launch failures and stale-head outcomes
- invariant alarms: any pending candidate returned by CI context, any transition approval without a
  predecessor tombstone, or any `decision_updated` resolution without a replacement

Success is not “more automatic shares.” Success is fewer repetitive developer prompts and fewer
blind lead clicks while preserving the rule that only approved team decisions govern agents and
checks.

## 11. Handover checklist

Before implementation begins:

- [x] Re-read both repository `CLAUDE.md` files.
- [x] Confirm both normal checkouts are preserved and create clean implementation worktrees from
      fetched `origin/main`.
- [x] Create separate implementation branches; Teams still deploys first after Phase A.
- [x] Ratify the sidecar schema, capability shape, repository compatibility rule, and enforcement
      choice UX before writing migrations.
- [ ] Complete the Phase 0 ledger, immutable Teams pointer, diff checks, and independent review.
- [ ] Add invariant tests before implementation.

Before declaring complete:

- [ ] All focused and full suites pass in both repositories.
- [ ] Migration and generated metadata are committed together.
- [ ] Manual share/reconcile and routine approval remain backward compatible.
- [ ] Account-switch, offline, stale-head, concurrent-drain, concurrent-approval, and blocking-gate
      scenarios have been exercised end to end.
- [ ] Documentation in both repositories explains the final behavior and security boundaries.
- [ ] The result has been reviewed by an agent/person who did not implement it, with special focus on
      tenant isolation, unintended egress, gate bypass, and false success messages.
