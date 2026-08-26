# Evidence-Based Decision Capture and Policy Evaluation

Status: Proposed implementation plan, revised after code validation

Companion plan: [Contexer Teams implementation](../../contexer-teams/docs/evidence-capture-policy-enforcement-plan.md)

Revision note: the validated plan uses one atomic JSON file per raw event instead of a shared
growing ledger file, avoids locks on editor hook paths, does not restore Stop hooks, models host
capabilities explicitly, preserves pending-decision retrieval, defers event kinds that have no
emitter, and gives lifecycle proposals a separate review lane from content revisions.

## Summary

Contexer should stop treating a single successful hook or an agent's willingness to take notes as
the unit of capture. Hooks, prompts, edited files, diffs, test results, agent conclusions, repeated
statements, and end-of-session reconciliation should each contribute evidence to a decision
candidate. A candidate remains provisional until the existing review flow approves it.

The same work should introduce a first-class policy-evaluation API. The API consumes a declared
intent, operation, affected files, and the actual operation artifact, then returns an explicit
`allow`, `warn`, or `block` verdict. Evaluation remains separate from enforcement. Local adapters
such as `guard_staged` may enforce deterministic results, while Contexer Teams owns canonical team
review and managed organization enforcement.

This plan preserves Contexer's current product constraints:

- local-first and inspectable storage;
- no background service;
- no vector database;
- no requirement that an AI model call a tool correctly;
- no automatic trust for AI-inferred architecture or constraints;
- immutable decision revisions;
- opt-in team sync;
- fail-open local commit protection when Contexer itself is unavailable.

## Outcomes

When complete, Contexer will:

1. Retain bounded evidence from multiple session signals even when one hook is missed.
2. Aggregate related evidence into explainable, deduplicated decision candidates.
3. Reconcile candidates at session boundaries without trusting them automatically.
4. Preserve the full revision and retirement history of decisions.
5. Evaluate proposed operations through one reusable API instead of embedding policy in prompts.
6. Let Git, file-write, shell, and future adapters consume the same evaluation result.
7. Sync approved revisions and lifecycle changes to Contexer Teams without sending raw private
   session evidence by default.

## Non-goals

- Guaranteeing that every engineering decision can be inferred from activity.
- Making an LLM the authority that approves or retires a decision.
- Blocking arbitrary shell or file operations before host-specific adapters exist.
- Replacing the existing decision store or revision model.
- Sending raw prompts, model responses, diffs, or source code to Teams automatically.
- Building generic agent governance in the first release.
- Making local Git hooks tamper-proof. Organization guarantees belong in required CI and service
  authorization boundaries.

## Existing foundations

The implementation should extend these existing seams:

| Concern | Existing implementation | Planned use |
| --- | --- | --- |
| Atomic local writes | `store._atomic_write` | One-file-per-event evidence spool |
| Decision-store serialization | `store._store_lock` | Decision mutations only; never an editor-hook evidence dependency |
| Decision capture | `store.update_decision_with_meta` | Materialize reviewed candidates through the current write path |
| Review | `review_pending` and `approve_decision` | Human ratification of inferred candidates |
| Immutable history | `revisions.py` and `current_revision_id` | Stable policy revision identity |
| Retirement | `<slug>.deleted.json` tombstone sidecar | Retain inactive decisions and lifecycle metadata |
| File signals | `record_edited_file` and host PostToolUse hooks | Emit normalized evidence events |
| Policy lookup | `guard_engine.decisions_for_files` | Resolve applicable approved decisions |
| Commit checks | `guard_engine.guard_staged` | First adapter over the common evaluator |
| Team transport | `remote.py` | Capability-negotiated revision and lifecycle sync |

## Target architecture

```text
Host events and explicit MCP calls
              |
              v
       normalized evidence ledger
              |
              v
       deterministic aggregation
              |
              v
        provisional candidates
              |
              v
       existing human review flow
              |
              v
       approved decision revisions
          |                 |
          v                 v
  context retrieval   policy evaluation
                            |
                            v
                  host enforcement adapter
```

Capture and policy evaluation share approved decision revisions, but they must remain separate
modules. Capture answers, "Should this become or update a decision?" Evaluation answers, "Does this
specific operation comply with the currently approved decisions?"

## Workstream A: Evidence ledger

### A1. Add `contexer/evidence.py`

The module owns a small versioned event schema and bounded sidecar storage. It must not import the
MCP server or host adapters.

Suggested event shape:

```python
{
    "schema_version": 1,
    "event_id": "uuid",
    "session_id": "host-session-id",
    "repo_key": "canonical-or-local-repo-key",
    "kind": "file_changed",
    "occurred_at": "UTC ISO-8601",
    "source": "codex_post_tool_use",
    "summary": "Authentication middleware changed",
    "files": ["src/auth.py"],
    "content_hash": "sha256-or-null",
    "attributes": {},
}
```

Initial `kind` vocabulary:

| Kind | Meaning | Default sensitivity |
| --- | --- | --- |
| `user_directive` | Explicit prescriptive user statement | Redacted text allowed locally |
| `agent_conclusion` | Agent reports a synthesized decision or rationale | Summary only |
| `file_changed` | A governed or session file changed | Paths and hashes |
| `diff_observed` | A diff was available for reconciliation | Hash and bounded statistics |
| `test_result` | A relevant validation passed or failed | Command class and status |
| `decision_repeated` | Existing decision was restated | Decision and revision IDs |
| `policy_evaluation` | An operation was checked | IDs, verdict, coverage |
| `session_reconcile` | Session reconciliation ran | Counts and completion state |

As shipped, `evidence.EVENT_KINDS` holds exactly those eight and three of them are RESERVED, meaning
schema-valid with no emitter anywhere. `diff_observed` and `test_result` are reserved because their
consumer shipped first: `candidates.SUPPORT_KINDS` scores both, so the day an adapter emits one the
validator must already accept it. `decision_repeated` is reserved because a restated rule is emitted
as an ordinary `user_directive` instead, which the aggregator can match onto the decision it
duplicates and settle as a `duplicate`, whereas a lone `decision_repeated` would score 15, land
`insufficient` and sit in `pending/` forever. `session_reconcile` is reserved because the pass
receipt was moved out of the spool entirely and into `.reconcile_<slug>.jsonl`: a receipt spooled
into `pending/` was never held, so it aged out through retention and made `contexer status` report
lost events on a repository that lost none.

`candidate_disposition` was REMOVED on the opposite test. A settled candidate's disposition lives in
the decision's own `evidence_summary` history, so that kind has neither a producer nor a reader, and
a spooled file claiming it is schema drift the validator should catch.

Do not store full source contents or full model transcripts in the ledger. The first release should
store bounded summaries, repo-relative paths, hashes, counts, decision IDs, and small redacted
snippets only when needed to render a candidate.

### A2. Per-event JSON spool and durability

Use one small JSON file per raw event. The evidence sidecar is a directory tree, not one growing
JSON document:

```text
~/.contexer/evidence/<repo-slug>/
├── pending/
│   ├── <utc-stamp>-<event-id>.json
│   └── <utc-stamp>-<event-id>.json
├── held/
│   └── <candidate-id>/
│       ├── <utc-stamp>-<event-id>.json
│       └── <utc-stamp>-<event-id>.json
├── quarantine/
└── .gap
```

`pending/` contains raw events awaiting reconciliation. `held/<candidate-id>/` retains the events
that support a candidate until that candidate receives a final disposition. `quarantine/` isolates
malformed events without making the rest of the spool unreadable. `.gap` means at least one event
could not be recorded or an overflow forced evidence loss.

Hook write requirements:

- derive the repo slug through the same hook-cwd resolution path used by the reader;
- create the repository and spool directories with mode `0700`;
- validate, bound, and redact the event before persistence;
- cap one serialized event at 8 KiB in V1;
- give every event a UUID and a filename that cannot collide;
- write a temporary file with mode `0600`, then `os.replace` it into `pending/` on the same
  filesystem;
- never acquire `_store_lock` or any shared evidence lock;
- never read, count, parse, or rewrite older events from the hook;
- never scan Git, aggregate candidates, or call a model from the hook;
- on any write failure, best-effort touch `.gap` and return without breaking the host operation.

Unique target filenames remove writer contention: concurrent editor windows never mutate the same
file. The cost of capturing event N is independent of events 1 through N-1. A crash exposes either
one complete event or no event, and one corrupt event cannot corrupt the rest of the spool.

Retention runs during reconciliation or explicit maintenance, never during an editor hook. Start
with measured bounds of approximately 1,000 pending events, 30 days pending age, and 8 KiB per
event. Retain held events while their candidate awaits review. After approval, edit, dismissal, or
ignore, preserve a compact evidence summary and event IDs in decision history, then remove the raw
held files. If retention drops events, set `.gap` and report the dropped count.

### A3. Evidence ingestion API

Add internal functions:

```python
append_evidence(repo_path: str, event: Mapping) -> EvidenceWriteResult
list_pending_evidence(repo_path: str, session_id: str = "") -> list[EvidenceFile]
hold_candidate_evidence(repo_path: str, candidate_id: str,
                        event_ids: Sequence[str]) -> EvidenceMoveResult
finalize_candidate_evidence(repo_path: str, candidate_id: str,
                            disposition: str) -> EvidenceCompactionResult
evidence_diagnostics(repo_path: str) -> dict
```

`append_evidence` should be fail-soft for host hooks. A failed evidence write must not break the
developer's editor operation. A failed write leaves `.gap` so loss is visible. The result should
distinguish `stored`, `dropped_error`, and `rejected_invalid` so tests and status reporting do not
treat every loss as the same failure.

`append_evidence` writes exactly one file and does not list the spool directory. Reconciliation is
the only normal reader. Invalid files move to `quarantine/`; one invalid event never makes the
whole repository evidence history unreadable.

### A4. Upgrade host adapters

Update Claude, Codex, Cursor, and Gemini adapters to emit the events each host can actually observe
through one CLI or Python entry point. Host-specific scripts should not construct storage objects
themselves.

The existing `.pending_capture` flags remain during the compatibility period. An event append also
arms the flag where that host already supports it, and reconciliation consumes both. Remove the
flags only after host-specific recovery tests pass.

Capability matrix for the first release:

| Signal or checkpoint | Claude | Codex | Gemini | Cursor |
| --- | --- | --- | --- | --- |
| User prompt and explicit directive | Yes | Yes | Yes | Yes |
| Edited-file event | Yes (`PostToolUse`) | Yes (`PostToolUse`) | Yes (`AfterTool`) | No |
| Assistant conclusion | Model-reported | Model-reported | Model-reported | Model-reported |
| Pre-compaction checkpoint | `PreCompact` | `PreCompact` | `PreCompress` | No |
| Clean session-end checkpoint | `SessionEnd` | No | `SessionEnd` | No |
| Session-start recovery | Yes | Yes | Yes | Yes |
| Next-prompt fallback | Yes | Yes | `BeforeAgent` equivalent | Prompt-side capture only |
| Next-session fallback | Yes | Yes | Yes | Yes |

Cursor is prompt-signal-only until Contexer owns a reliable post-edit hook there. Its
`beforeSubmitPrompt` hook can perform capture side effects but cannot inject per-prompt context.
Do not claim edited-file or lifecycle-hook parity for Cursor.

Session-start recovery is the one row that is true on every host without exception, and it is the
only reconciliation checkpoint Codex and Cursor have. `store._local_session_start_payload` calls
`reconcile.reconcile_session` from the shared store-side path every host traverses, so the guarantee
does not depend on any adapter having wired its own checkpoint. Gemini keeps its `PreCompress` and
`SessionEnd` calls beside it because they answer a different failure, and idempotency is what makes
keeping both free.

Assistant conclusions are `model_reported` on every host, including Cursor, and that is a distinct
state from `captured`. No host hands a hook the assistant's own response, so nothing observes a
conclusion. `evidence.record_agent_conclusion` and its MCP tool of the same name are agent-invoked
by design, and no Stop hook or transcript scan was added to pretend otherwise.

Coverage is reported per host by `evidence.host_coverage`, whose vocabulary is `captured`,
`model_reported`, `unavailable`, `manual` and `error`. Reporting is DOWNGRADE-ONLY: an
out-of-vocabulary static value becomes `error`, a missing field makes the pass `partial`, an unknown
host becomes `manual`, and no caller state is ever improved. Capability is never rendered as a
count, so an unavailable signal cannot read as captured-zero.

`diff_observed` and `test_result` are reserved schema kinds in Phase 1, not promised signals. They
remain absent until a commit-time diff emitter and a bounded PostToolUse/AfterTool command-result
emitter are implemented with their own privacy and size limits.

## Workstream B: Candidate aggregation

### B1. Add `contexer/candidates.py`

This must be a pure leaf module. Inputs are normalized evidence and a read-only projection of
existing decisions. Outputs are candidate objects and diagnostics. It performs no file or store
writes.

Suggested candidate shape:

```python
{
    "candidate_id": "uuid",
    "kind": "new|update|replace|retire|reconsider|duplicate|insufficient",
    "title": "Retain retired decisions as history",
    "content": "...",
    "subtype": "architecture",
    "target_decision_id": None,
    "target_state": None,
    "basis_revision_id": None,
    "replacement_decision_id": None,
    "source_files": ["contexer/store.py"],
    "possible_source_files": [],
    "score": 78,
    "signals": [
        {"event_id": "...", "weight": 30, "relation": "explicit",
         "certainty": "confirmed", "reason": "explicit user directive"}
    ],
    "uncertain_signals": [],
    "uncertainties": [],
}
```

Three additions to the sketch above are load-bearing rather than cosmetic.

`reconsider` is a seventh kind, added because a restatement of a retired or ignored decision is a
question about that decision's identity rather than a new one. It carries `target_state`
(`retired` or `ignored`) and `basis_revision_id`, the target's `current_revision_id` at proposal
time, and only that kind binds its identity to the basis: for every other kind the target's revision
is read at materialization time, where the proposal actually lands. Only an explicit `user_directive`
may open one, so agent-only evidence produces no reconsideration and no note naming any decision.

Every signal row carries a typed `relation` and a `certainty` beside its weight, from two closed
vocabularies. `RELATIONS` is `explicit`, `structural`, `causal_forward`, `temporal_backward`,
`repetition`, `contradiction`, `validation` and `unrelated`; `CERTAINTIES` is `confirmed`,
`supporting` and `uncertain`. Weight alone could never say WHY an event is in a candidate, and a
reviewer reading a queue needs the link rather than a number. Rows of weight 0 are kept for the same
reason: dropping them would make a corroborating event that scored nothing indistinguishable from
one that was never seen.

`possible_source_files` holds paths reached only through an UNCERTAIN link, and it is kept separate
from `source_files` for the whole length of the pipeline. Nothing may promote one into an anchor, a
policy rule's scope, `anchor_candidates` or Teams. Its one reader outside the aggregator and its own
manifest is the review render, which labels such paths as files that will NOT be anchored, with the
reason. That separation is enforced by a structural test rather than by convention.

### B2. Deterministic scoring first

Initial scoring should be inspectable and deterministic:

| Signal | Example contribution |
| --- | ---: |
| Explicit user directive | +50 |
| Agent conclusion with rationale | +25 |
| Same conclusion repeated independently | +15 |
| Relevant files changed | +10 |
| Tests validate the chosen behavior | +10 |
| Contradictory statement | -30 |
| Only a file changed, with no semantic statement | candidate remains insufficient |

The numbers must live in one constants object and have tests explaining each threshold. They are
ranking inputs, not confidence that a statement is true.

An optional model-assisted summarizer may propose candidate wording after deterministic grouping,
but it must not decide approval, enforcement, or lifecycle changes. Persist the contributing event
IDs so the proposed wording is reviewable.

### B3. Candidate materialization

Add a coordinator in `store.py` or a focused facade module that:

1. reads unconsumed session evidence;
2. loads current live and tombstoned decisions;
3. calls the pure aggregator;
4. maps `new` candidates into the current pending decision model;
5. maps `update` candidates into `proposed_revision` without moving HEAD;
6. maps `retire` and `replace` candidates into the separate `proposed_lifecycle` lane defined in
   C2, never into or through `proposed_revision`, and maps `reconsider` candidates into a third
   `proposed_reconsideration` lane that coexists with both of the others;
7. records candidate-to-event references;
8. moves its supporting files from `pending/` to `held/<candidate-id>/` and only then
   materializes the candidate, which is the REVERSE of the order this plan first specified;
9. emits a reconciliation receipt.

Never call a low-level store mutation that bypasses current similarity filtering, revision
construction, approval invalidation, or capacity limits.

Candidate identity must be deterministic from the candidate kind, target decision ID when any,
and sorted supporting event IDs. Store the supporting event IDs on the pending candidate. If a
crash occurs after candidate materialization but before every file moves to `held/`, the next run
recognizes those event IDs as already referenced, finishes the moves, and does not create a second
candidate.

As shipped, a `reconsider` candidate APPENDS its `basis_revision_id` to that hash, so a question
asked against one revision of a decision is a different question from the same words asked against
the next one. Every pre-existing id keeps its spelling because the component is appended only when
present.

The hold directory is what makes repeated reconciliation a no-op, not the store's novelty filter:
a held candidate's events never reach the aggregator again, and the directory itself is the record
that the candidate awaits review. The novelty filter is only the backstop for a hold that failed to
complete.

**HOLD FIRST, materialize second**, which reverses step 8 as this plan originally wrote it, and the
reversal was earned rather than preferred. Materializing first left a window nothing on disk covered:
a crash after the store write and before the hold left a decision whose evidence was still in
`pending/`, connected to it by nothing. Holding first means a crash anywhere leaves the evidence
either wholly pending, so the next pass aggregates it again under the same deterministic id, or
wholly held behind a manifest that names both what it claims and how far it got.

The order is therefore a state machine rather than a sequence of hopeful writes, with each phase
durable in the candidate's own manifest before the next one starts: write the manifest in `held`,
move every named event, VERIFY the manifest's event set against what is now on disk, flip to
`materializing`, write through the ordinary store or lifecycle path, re-read the store and persist
`pending_review` or `settled` from what it OBSERVES rather than from the write's own return value,
record the evidence summary after a review, persist `reviewed` with the disposition, and only then
remove the held directory.

On the way out the same rule reads backwards: the summary is durable before the manifest says
`reviewed`, and the manifest says `reviewed` before a single raw event is deleted. Any other order
loses the evidence and the receipt for it together, permanently.

A hold that reports a source gone with no target is never finalized: finalizing writes an
`evidence_summary` saying this event set settled, and a hold that cannot account for its events has
not earned that receipt.

### B4. Session reconciliation surface

Add an MCP tool and CLI command:

```text
reconcile_session(repo_path, session_id, dry_run=false)
contexer reconcile-session [--session ID] [--dry-run]
```

The result must distinguish:

- events observed;
- candidates proposed;
- candidates already pending;
- duplicates;
- insufficient groups;
- evaluation incomplete because events, the `.gap` marker, or a diff indicated missing evidence.

Never run it from Stop hooks. Stop fires at every turn end and Contexer deliberately removes its
old Stop hooks because their latency and token cost added no functional value.

Run reconciliation at the host-specific checkpoints in A4: clean session end when supported,
pre-compaction when supported, the next prompt or agent turn, and the next session start as the
final recovery net. Repeated calls must be idempotent on the same event set. Hooks should enqueue
or invoke only bounded deterministic reconciliation; any expensive summarization remains outside
the hook path.

As shipped, the session-start net is a single store-side call rather than four adapter-side ones,
and its cost is gated structurally rather than by wall clock. An empty spool does no store load, no
store lock, no lock-file creation and no candidate scan, so a repository that will never hold an
evidence event pays two directory listings at every session start. The lock is taken only once the
fast path has already found work, which is the unlocked-then-locked shape `ensure_retrieval_index`
uses; a pass that finds it held skips entirely and says so in its receipt, and the next checkpoint
picks the work up.

`reconcile_session` also takes a `host` argument, threaded from the adapter, because the store
genuinely does not know which host it is running under. An un-reinstalled install reports `manual`,
which claims nothing: the rule is under-report, never over-claim.

The receipt is LOGGED to `.reconcile_<slug>.jsonl`, tail-capped and fail-soft, and never spooled. A
pass that did nothing logs nothing, so a repository with one stuck held candidate does not write a
line per session start forever.

`dry_run` writes NOTHING anywhere: no store write, no hold, no manifest, no state update, no
finalize, no retention, no receipt line, no disposition. Because the hold moved in front of the
store write, the gate is ONE early return in `_materialize`, taken after the candidate is counted
and before the first write of any kind, so every lane is covered by the same check. A future lane
must sit BELOW that return rather than beside it.

## Workstream C: Decision lifecycle

### C1. Keep active state separate from history

Retrieval and policy evaluation deliberately have different trust filters:

- explicit `get_context` and BM25 retrieval continue to include approved, suggested, and
  `pending_approval` decisions, with suggested and pending entries visibly labeled;
- session-start automatic context continues to preload approved and suggested rules, while pending
  entries contribute only to the review count;
- policy evaluation and enforcement use only decisions accepted by the existing Guard trust
  predicate: status `approved` plus trusted provenance or explicit human ratification;
- ignored, deleted, and retired entries participate in neither active retrieval nor policy
  evaluation.

This plan does not change current pending retrieval behavior. Pending and suggested knowledge can
help explain what is under consideration, but neither may produce a policy warning or block unless
the developer explicitly approves it and it passes the Guard trust predicate.

As shipped, everything reconciliation creates is REVIEWABLE by construction. A new candidate lands
`pending_approval` via an explicit `force_pending`, never the `suggested` tier an `ai` capture would
otherwise get, because `suggested` injects at session start yet never appears in `review_pending`,
which is trusted without ever having been offered for review.

**Approval is not arming, and the review surface says so.** Approving a decision makes it retrievable
and lets it pair as a Tier-1 advisory; it never creates a blocking rule. Arming is a separate
explicit `guard arm` gesture, and no path from any review surface reaches it.
`review_impact._armed_rule` REPORTS through `guard_engine._armed_rules`, the same selector the guard
itself runs, so the block can describe an armed rule that already exists and can create none.

What a review surface shows is computed once, in `contexer/review_impact.py`, and rendered from one
list by all three surfaces (`review_pending`, `contexer review`, the local console), so the same
decision cannot read one way in the terminal and another in the console. The block names origin,
the score LABELLED as a review priority rather than a confidence, confirmed evidence grouped by
relationship type, uncertain links in their own section, what would be anchored and what explicitly
would not, host coverage, similar decisions and open conflicts, inactive history and retirement
reason, revision identity with the lane's own staleness verdict, and the policy effect of approving.
The module is a READ: it never approves, arms, anchors or restores anything, which is the whole
point of a block a developer reads before signing.

The tombstone entry should gain a versioned lifecycle record:

```json
{
  "lifecycle": [
    {
      "event_id": "uuid",
      "kind": "retired",
      "occurred_at": "...",
      "actor": "human",
      "reason": "Replaced by centralized authentication",
      "revision_id": "revision-used-at-retirement",
      "replacement_decision_id": "decision-456"
    }
  ]
}
```

Supported lifecycle events in V1:

- `retired`
- `restored`
- `superseded`
- `replacement_linked`

As shipped, that vocabulary has ONE owner, `lifecycle.RECORD_KINDS` and `lifecycle.RETIRED_KINDS`,
enforced by `lifecycle_record` itself, and `reconcile` DERIVES its retired set rather than
respelling it, so a rename cannot leave a reader matching on a spelling nothing writes while its own
tests still pass. `replacement_linked` remains reserved vocabulary with no writer.
`remote._WIRE_LIFECYCLE_KINDS` deliberately stays a SEPARATE list even though the three written
spellings agree today: it is a guess at the server's enum, and coupling it to the local vocabulary
would let a local rename silently change what goes over the wire.

`revision_id` on a record is captured AT the transition and never re-derived, because
`revisions.append_revision` pops the entry-level `approved_by` stamp whenever a non-human revision
lands, so history that trusted entry-level state would misreport which version was retired.

Deletion in the UI should become a clearly named retirement action for normal knowledge hygiene.
Reserve irreversible erasure for an explicit privacy workflow.

### C2. Give lifecycle proposals a separate review lane

A candidate may recommend retirement or replacement, but only an explicit human action can move a
live decision out of active context. Do not reuse the entry's one `proposed_revision` slot. That
slot has an existing trust order and represents alternative decision wording; a retirement
recommendation is a different state transition and must not displace or be displaced by a content
correction.

Suggested representation:

```json
{
  "proposed_lifecycle": {
    "proposal_id": "uuid",
    "action": "retire",
    "reason": "Replaced by centralized authentication",
    "replacement_decision_id": "decision-456",
    "basis_revision_id": "revision-3",
    "source": "ai",
    "created_at": "..."
  }
}
```

Rules:

- at most one `proposed_lifecycle` and one `proposed_revision` may exist on an entry;
- the two lanes may coexist and never displace one another;
- human lifecycle proposals outrank automated lifecycle proposals;
- a lifecycle proposal is bound to `basis_revision_id`;
- moving HEAD makes an unresolved lifecycle proposal stale and requires fresh review;
- approving a content proposal never approves a lifecycle proposal;
- approving retirement archives any unresolved content proposal as unreviewed at retirement;
- anchor-loss withdrawal should migrate to this lane instead of encoding retirement as new
  decision content.

Add review actions or a narrow lifecycle tool such as:

```text
retire_decision(entry_id, reason, replacement_id?)
restore_decision(entry_id, reason?)
```

Do not overload `approve_decision(action="ignore")` forever. Maintain it as a compatibility alias
while the UI and CLI migrate to lifecycle language.

As shipped there are THREE lanes on an entry, not two. `proposed_revision` answers "this should read
differently", `proposed_lifecycle` answers "this should stop being live", and
`proposed_reconsideration` answers "this stopped being live and somebody just restated it". All
three coexist, none displaces another, and resolving one leaves the others exactly where they were.
A reconsideration lives on the live entry for an ignored twin and on the tombstone for a retired one,
and its review actions are `restore`, `restore_edit`, `skip` and `dismiss`, one decision id at a
time, on both the MCP and CLI surfaces plus a `reconsider_decision` tool.

`restore` returns a decision with no historical active status as `pending_approval` rather than
guessing that it was approved, so restoring an ignored twin deliberately costs two answers.

For the lifecycle and reconsideration lanes a revision advance is NEVER an approval signal.
Retirement is a move, so an unrelated content edit that happened to advance HEAD would otherwise
record a retirement that never occurred. A hold in those lanes is settled only on observed review
state: a completed record at the basis, or a durable receipt written at or after the hold itself.

## Workstream D: Policy-evaluation API

### D1. Add a pure evaluator

Create `contexer/policy.py` with no MCP, CLI, subprocess, or filesystem dependencies.

Input:

```python
PolicyEvaluationRequest(
    intent="Modify authentication middleware",
    operation="write_files",
    files=["src/auth.py"],
    artifact=PolicyArtifact(kind="diff", content="..."),
    repo_key="github.com/acme/api",
)
```

Output:

```python
PolicyEvaluationResult(
    verdict="warn",
    evaluation_status="complete",
    basis="deterministic",
    matches=[...],
    unchecked=[],
    policy_set_version="sha256:...",
)
```

Required vocabularies:

```text
verdict: allow | warn | block
evaluation_status: complete | partial | error
basis: deterministic | semantic | mixed
operation: read_files | write_files | shell | commit | merge | deploy | api_request
artifact.kind: diff | file_content | command | request | deployment
```

Rules:

- `allow` means all applicable policies that the selected engine can evaluate passed.
- `warn` means a relevant advisory policy may be violated or evaluation is partial.
- `block` requires an applicable approved policy marked for enforcement and a supported blocking
  engine result.
- `error` is never silently converted to `allow` by the evaluator.
- every match names the exact decision ID and revision ID;
- every omitted or truncated artifact appears in `unchecked`;
- the caller, not the evaluator, chooses fail-open or fail-closed behavior for `partial` and
  `error`, according to the enforcement boundary.

### D2. Separate policy selection from judging

Expose pure steps:

```python
select_policies(decisions, request) -> list[ApplicablePolicy]
evaluate_policies(policies, request) -> PolicyEvaluationResult
policy_set_version(policies) -> str
```

Policy selection reuses file anchors, repo scope, armed guard configuration, and the exact existing
`guard_engine._guard_trusted` semantics: `status == "approved"` plus trusted provenance or explicit
human ratification. Suggested and pending decisions remain retrievable context but do not enter the
policy set. Judging reuses deterministic regex and secret checks from `guard_engine.py`.

Free-form decision prose may generate an advisory warning locally. It must not become a new local
hard block merely because a model interpreted it as one. Existing explicitly armed regex and
secret checks retain their blocking behavior.

### D3. MCP and CLI surface

Add:

```text
evaluate_policy(repo_path, intent, operation, files, artifact_kind, artifact)
contexer policy evaluate --operation commit --diff-file ...
```

Bound every string and list at both the MCP schema and evaluator facade. Redact secrets from logs
and receipts. The structured result is authoritative; human-readable text is a rendering of it.

### D4. Refactor `guard_staged`

Split `guard_staged` into:

1. a Git adapter that resolves the repository and reads staged paths/diff;
2. policy selection;
3. pure deterministic evaluation;
4. existing warning deduplication and rendering;
5. Git-specific exit behavior.

Preserve current behavior during refactoring:

- trusted approved decisions only;
- advisory prose does not block;
- explicitly armed deterministic checks can block;
- timeout or internal error fails open with an honest unchecked message;
- `--no-verify` and `CONTEXER_GUARD=0` remain explicit local bypasses.

## Workstream E: Teams synchronization

### E1. Extend capability discovery

The remote client must discover support before sending new fields:

```json
{
  "decisionLifecycle": {
    "version": 1,
    "revisions": true,
    "tombstones": true,
    "retirementReasons": true
  }
}
```

Older servers continue receiving the existing active decision shape.

As shipped, the contract is live and the client gate `remote._WIRE_LIFECYCLE` is OPEN. The exact
spellings, which were guesses when this plan was written, were confirmed against the server
implementation and then driven against a locally running migrated server before the gate moved. They
are snake_case on the wire (`revision_id`, `lifecycle`, and the record's own keys) precisely because
those ARE the local serializer's shape, and renaming one would be a rejection on every retry of a
client outbox row. That the same schema spells `decisionId` in camelCase elsewhere is not a typo on
either side; it is why the field names were never guessable and had to be read.

The capability is advertised as `decisionLifecycle` with `version`, `revisions`, `tombstones` and
`retirementReasons`, and `retirementReasons: false` genuinely strips the prose from the wire rather
than merely hiding it.

An OPTIONAL-PROTOCOL FALLBACK sits on both push paths, and it is what makes opening the gate safe
rather than merely justified. An advertising server that refuses the augmented payload gets ONE
retry with the base decision alone, re-serialized through the same wire path with the capability
cleared so it is byte-identical to what an old server receives. THE RETRY IS THE DISCRIMINATOR:
only authentication, authorization, unreachability and capacity are excluded up front, and
everything else is a candidate until the base push actually succeeds, which is what makes it work
against a server whose rejection names no field. If the legacy retry fails too, the ORIGINAL error is
raised, nothing is marked blocked and the capability is untouched.

A confirmed refusal disables the capability for the life of that store, so no later push re-offers
the refused field, and queues the delta as a durable outbox row carrying the reason and the
capability fingerprint it was refused under. It is re-offered only when the advertised fingerprint
moves, which is the difference between durably pending and a retry storm, and it is never dropped,
never quarantined and never counted as synced. A refusal now costs the history rather than the
decision.

### E2. Outbound privacy boundary

Sync only:

- locally approved decision bodies;
- immutable revision metadata allowed by the existing preview;
- lifecycle event type, reason, actor category, and timestamps;
- source-file anchors already approved for sharing;
- bounded provenance already included in the sync contract.

Do not sync `proposed_revision`, `proposed_lifecycle`, or their supporting evidence. Sync a
lifecycle change only after explicit local approval turns it into a completed lifecycle event.

Do not sync raw evidence ledger events, prompts, agent responses, full diffs, test output, or
unapproved candidates. If future team evidence sharing is added, it must be a separate explicit
verb with its own preview and redaction contract.

### E3. Delta convergence

The client must treat a Teams tombstone or retirement as removal from the active remote cache while
retaining local history. A later restore or replacement arrives as a new lifecycle delta. Store the
remote policy-set cursor independently from the local evidence checkpoint.

## Testing strategy

### Unit tests

- event validation, redaction, 8 KiB bound, and serialization;
- unique filenames, mode `0600`, temporary-write plus atomic-rename behavior;
- one corrupt event is quarantined without hiding valid siblings;
- `.gap` diagnostics and retention-overflow reporting;
- deterministic candidate identity from sorted event IDs;
- crash recovery when only some supporting files reached `held/<candidate-id>/`;
- deterministic grouping and scoring;
- candidate idempotency over repeated reconciliation;
- candidate classification for new, update, replacement, retirement, duplicate, and insufficient;
- lifecycle transition purity;
- policy selection by status, provenance, repo, and source files;
- `allow`, `warn`, `block`, `partial`, and `error` semantics;
- stable policy-set hashing independent of input ordering;
- no block from unapproved or retired decisions;
- exact decision and revision IDs in every result.

### Adapter tests

- Claude, Codex, and Gemini map their write hooks to the same normalized file-change schema;
- Cursor produces prompt/directive evidence but no Contexer-owned file-change event;
- a failed hook loses one signal but other signals still produce a candidate;
- concurrent hook writers create independent event files without locking or lost updates;
- a failed event write returns immediately and leaves a gap diagnostic;
- SessionEnd, pre-compaction, next-prompt, and next-session recovery are tested per host capability;
- installers continue removing old Stop hooks rather than restoring them;
- existing `.pending_capture` behavior remains during migration;
- malformed host payloads do not break the editor.

### Integration tests

- edit files, state a decision, reconcile, approve, and retrieve it in a new session;
- update an approved decision and verify HEAD does not move before approval;
- retire a decision and verify retrieval and evaluation exclude it while history retains it;
- stage a deterministic violation and verify `guard_staged` blocks exactly as before;
- force an evaluator error and verify the local Git adapter fails open and reports unchecked work;
- sync revision and lifecycle deltas to a fake old and new Teams server.

### Benchmarks

- per-event atomic spool-write latency under concurrent host events;
- reconciliation and directory-listing latency at spool bounds;
- prompt-start overhead remains unchanged because evidence is not loaded there;
- candidate precision and recall on labeled session fixtures;
- policy selection and deterministic evaluation under the 500-decision store cap.

## Rollout phases

### Phase 0: Contracts and fixtures

- Freeze event, candidate, evaluation, and lifecycle schemas.
- Add golden JSON fixtures shared with Contexer Teams where wire parity matters.
- Add policy result semantics to documentation before code consumes them.

Exit gate: both repositories parse the same fixtures and reject incompatible versions.

### Phase 1: Shadow evidence capture

- Add the ledger and host event emission.
- Emit only signals currently observable by each host; `test_result` and `diff_observed` remain
  empty until their dedicated emitters ship.
- Keep current capture behavior unchanged.
- Reconcile in dry-run mode and measure missed, duplicate, and noisy candidates.

Exit gate: no prompt-start regression and no host operation fails because evidence capture failed.

### Phase 2: Human-reviewed candidates

- Materialize new and update candidates into the current review flow.
- Add candidate evidence explanations to CLI/UI review.
- Keep retirement recommendations read-only.

Exit gate: every inferred architecture or constraint remains inactive until explicit approval.

### Phase 3: Lifecycle

- Add `proposed_lifecycle`, its independent review and staleness rules, then retire, restore,
  replacement, and history rendering.
- Add lifecycle sync capability negotiation.
- Keep irreversible erasure separate.

Exit gate: a retired decision disappears from active context and policy evaluation but remains
inspectable with reason and revision history.

### Phase 4: Read-only policy API

- Add the pure evaluator, MCP tool, and CLI renderer.
- Run `guard_staged` through the evaluator without changing exit behavior.
- Compare legacy and new results in tests and optional shadow telemetry.

Exit gate: deterministic Guard parity and explicit partial/error coverage.

### Phase 5: Additional enforcement adapters

- Add file-write or shell-command adapters only where the host exposes a reliable pre-operation
  interception point.
- Keep free-form prose advisory locally.
- Document bypass and failure behavior for each adapter.

Exit gate: each adapter proves it is invoked by the operation-performing system, not merely by an
agent instruction.

## Suggested pull-request breakdown

1. Event schemas, golden fixtures, and pure validation.
2. Atomic per-event JSON spool, diagnostics, bounds, holding, and compaction.
3. Host adapters in shadow mode with a capability matrix and no Stop hook.
4. Pure candidate grouping and scoring.
5. Session reconciliation and existing-review integration.
6. Separate lifecycle-proposal lane, lifecycle events, and retire/restore UI and CLI terminology.
7. Policy request/result types and pure policy selection.
8. Deterministic evaluator extraction from `guard_engine.py`.
9. `guard_staged` adapter refactor and parity tests.
10. MCP/CLI `evaluate_policy` surface.
11. Revision and lifecycle sync capability support.
12. Benchmarks, migration cleanup, and removal of compatibility flags.

Each pull request should be independently revertible and keep existing capture and Guard behavior
working.

## Success metrics

- Percentage of labeled major decisions represented by at least one candidate.
- Candidate approval, edit, dismissal, and duplicate rates.
- Major decisions found only during end-of-session reconciliation.
- Sessions with evidence gaps or reconciliation errors.
- Median evidence append and reconciliation latency.
- False-warning rate for applicable-file selection.
- Evaluations marked partial because artifacts were missing or truncated.
- Deterministic Guard parity before and after refactoring.
- Retired decisions incorrectly returned as active: target zero.

## Risks and mitigations

| Risk | Mitigation |
| --- | --- |
| Evidence becomes a second noisy memory store | Bounded spool, held-event compaction, and no prompt injection |
| File edits create false decision candidates | File changes alone remain insufficient |
| Agent-authored summaries fabricate rationale | Preserve source event references and require review |
| Candidate scoring looks like truth confidence | Label it aggregation score and render contributing signals |
| Corrupt evidence blocks development | Quarantine one event; hook writes fail soft; diagnostics make loss visible |
| Evidence hook waits behind store maintenance | Unique per-event files require no decision-store or evidence lock |
| Per-event files grow without bound | Reconciliation owns age/count retention; hooks never scan to enforce it |
| Evaluation error becomes accidental allow | Separate verdict from evaluation status |
| Free-form prose becomes an unsafe local blocker | Block only explicitly armed deterministic checks |
| Retirement destroys institutional history | Move out of active store but retain tombstone and lifecycle |
| Team sync leaks session contents | Sync approved decisions and lifecycle only |
| Two policy engines drift | Shared fixtures and one evaluator contract; adapters only collect artifacts and enforce results |

## Definition of done

- Missing any single hook cannot erase all evidence that a decision occurred.
- No evidence hook waits for any shared lock, reads historical evidence, or performs expensive
  reconciliation inline.
- Every inferred decision is explainable by stored signal references.
- No inferred architecture or constraint becomes active without review.
- Policy evaluation is callable without constructing prompt text.
- The response distinguishes verdict, completeness, basis, and unchecked inputs.
- `guard_staged` uses the common evaluator with no behavior regression.
- Pending decisions preserve their current labeled retrieval behavior but never participate in
  policy evaluation or enforcement.
- Retired decisions remain available in history but never participate in active retrieval or
  enforcement.
- Content and lifecycle proposals have separate slots and explicit conflict rules.
- Revision and lifecycle sync is capability-negotiated and privacy-preserving.
- All supported hosts, store corruption paths, and old/new Teams server combinations have tests.
