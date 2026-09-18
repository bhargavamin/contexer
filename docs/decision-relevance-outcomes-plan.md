# Better decision relevance and outcome measurement

Status: proposed implementation plan, not an approved or implemented architecture.

Prepared 2026-09-17 against fetched `origin/main` at `5676247`. The primary checkout is older and contains unrelated work; source inspection used the fetched revision without changing that checkout. Source locations below refer to this revision, not necessarily the current working-tree files.

## Goal and user requirements

Get the correct, current decision to the agent before it makes the relevant choice, and show whether that guidance was useful using evidence rather than retrieval counts.

The user prioritizes whatever is effective and achievable without nagging or obstructing in-session work; visibility of impact is essential. Therefore:

- Start with local coding-agent sessions, where retrieval and delivery can be improved in this repository. Treat existing cached team guidance as a separately attributed source, not as a reason to expand the first release into the Teams backend.
- Make visibility an early deliverable, not a final dashboard project.
- Add no mandatory feedback, review interruptions, session-end questionnaires, blocking checks, or extra per-turn notifications.
- Keep existing approval, sharing, and enforcement boundaries unchanged.
- Do not describe this as building another memory tool: the unit of value is a correctly applied engineering decision.

## 1. What already exists, and the actual gaps

| Current implementation | Consequence for this plan |
|---|---|
| `contexer/retrieval.py:160`, `prompt_rank`, already combines separate title and content BM25 scores. | Do not propose title search as a new feature or replace the lexical ranker before measuring its failures. |
| `contexer/store.py:5977`, `_get_context_for_prompt`, still gates on rationale/project language, questions, or artifacts. Plain imperative tasks without those signals return early. | Measure trigger misses separately from ranking misses; broader task discovery needs a controlled experiment. |
| Explicit `get_context` can fall back to BM25 relevance, but display truncation then calls `_keep_top` (`store.py:1105`), which selects by recurrence and recency. | A cap can discard the best match. Fix query-result selection separately from storage eviction, which uses the same helper. |
| The prompt path prioritizes explicit file anchors and caps full-content output at three decisions (`store.py:6086`). | Common files can crowd out more relevant guidance. Candidate recall and visible-output relevance need separate measurements. |
| Working-set dedup stores decision IDs (`store.py:5387`), not delivered revision identities. | Test changed decisions in long sessions; an already-seen ID must not hide a newly applicable revision. |
| `_retrieval_log` and `log_followup_if_matching` (`store.py:5506`, `5739`) track pointers and a recent topic-matching lookup. | A lookup is engagement, not compliance or success. The repo-wide time-window association is not sufficient task attribution. |
| Host hook session IDs and MCP process identity are not universally identical (`server.py:11`; adapter hook input). | Do not join events merely because their repo and timestamps match. Concurrent sessions need exact identity or an explicit unknown state. |
| `guard_engine.rank_applicable` is a separate content-only change-ranking path. The historical applicability harness exercises it or file matching, not the complete agent prompt-delivery path. | Old PR relevance scores are diagnostic evidence, not the baseline for agent-session improvements. Keep the two evaluations separate. |
| `benchmarks/memory_campaign.py:180` already scores rationale, compliance, superseded choices, and enforcement on isolated tasks. | Reuse its isolation and task-scoring approach; add baseline-versus-candidate retrieval comparisons instead of recreating a benchmark platform. |
| Evidence collection exists, but host declarations mark test results/diffs unavailable and conclusions model-reported. Cursor cannot inject per-prompt context through its prompt hook. | Do not promise automatic observation of every outcome or equal delivery coverage across hosts. |
| Pending decisions are intentionally searchable and can be injected with a pending label; prompt and explicit lookup rendering are not identical. | Preserve authority labels and test bootstrap/conflict caveat parity. Do not mistake relevance for approval or silently ban all pending guidance. |
| `policy_api.evaluate_operation` and `policy.evaluate_policies` already return structured verdicts, evaluation status, matched revision IDs, and unchecked gaps. | Reuse existing check results where they actually occurred. An `allow` verdict alone is not proof of compliance; passing-policy coverage needs an explicit receipt contract. |
| Claude/Codex recall notices already expose guidance; their token-savings figure uses a fixed benchmark-derived multiplier (`adapters/claude.py:191–198`). Console sessions currently group captured decisions (`console_api.py:510`). | Extend visibility without representing estimated savings as measured task impact or captured decisions as applied decisions. |

The August applicability analysis also found hub-file false positives and top-k recall limits. Its historical reconstruction does not fully rewind authority/lifecycle state. Preserve its frozen corpora and constants; collect a fresh, snapshot-based dataset for new tuning.

Three isolated synthetic-store probes confirmed the ordinary-task gate miss, same-session suppression after a revision update, and relevance loss at an explicit `limit=1`. These are demonstrated failure cases, not estimates of how often users encounter them. See verification below.

## 2. Measurement contract: separate relevance, delivery, and results

For an independently labeled task, record the stages below. A later stage is not implied by an earlier one.

| Stage | What can honestly be claimed | Failure category |
|---|---|---|
| Knowledge available | A relevant decision revision existed before the task, with known authority and applicability. | Missing capture or unavailable knowledge |
| Candidate found | Retrieval considered the relevant decision. | Trigger or candidate-generation miss |
| Guidance selected | The decision survived ranking, scope checks, and the output budget. | Ranking, scope, or truncation miss |
| Guidance emitted | The adapter emitted supported context, or an explicit tool response returned it. | Delivery unsupported, failed, or unknown |
| Agent referenced it | A matching lookup or explicit reference can be attributed to this task. | Follow-through unknown; not proof of application |
| Result checked | A task-specific test or human review assessed the artifact against the decision. | Applied, violated, appropriately clarified, or unknown |

Do not label emitted context as read or understood. Do not label an agent's claim as independent verification. Do not label silence, a successful commit, or zero detected violations as success.

### Minimum event fields

Extend the existing retrieval diagnostics through the current owners, with a versioned schema:

- Event ID and type, monotonic sequence/time, repository identity, physical checkout/worktree identity, host/version, retrieval variant and configuration fingerprint.
- Host session identity and MCP session identity where available; exact request/task correlation and its provenance. The benchmark supplies explicit task IDs; everyday sessions may have only request/session identity.
- Decision ID, exact rendered revision ID, source scope, status/authority label, and open-proposal identity when a conflict is shown. Obtain these from the same loaded state used for rendering, not an earlier ranking snapshot.
- Trigger category; match signals; score/rank; selected tier; exclusion reason; configured budget; estimated versus measured context size; elapsed time.
- Emission status with host capability, and outcome evidence type/source, artifact revision, validator version, and result when available.
- Missing attribution, dropped diagnostics, unsupported capabilities, and incomplete checks as explicit coverage gaps.

Keep selection metadata structured; do not recover identities by parsing rendered prose. Preserve distinguishable reasons such as `gate_closed`, `no_candidate`, `out_of_scope`, `already_delivered_revision`, `budget_limited`, `host_unsupported`, and `error`.

Distinguish standing-title, standing-full, prompt-full, pointer, explicit-tool-result, and cached-team delivery. A startup title is not the full rationale. The current working set is prompt-injection dedup state, not a complete exposure ledger; do not reuse its counts as total delivery. Selection or working-set insertion before adapter output is not confirmed emission, and even adapter output is not proof the host consumed it.

The existing pointer-followup heuristic can remain a legacy engagement metric, labeled as such. It must not become a task-success metric. Where a safe host-to-MCP identity bridge is unavailable, report unattributed events rather than guessing. No repo-wide “current task” pointer.

### Privacy, reliability, and cost

- Detailed pilot diagnostics are explicitly enabled and local-only. No raw prompts, conversations, diffs, secrets, or full decision bodies in routine metric events; use bounded metadata and local artifact references. Diagnostic export is a separate explicit action with preview/redaction.
- Respect the existing sidecar lifecycle in `sidecars.py`; enforce both age and byte/row caps. Document deletion/export behavior before enabling pilot collection.
- Optional diagnostics must not block a prompt, take the decision-store lock, scan the evidence spool, run Git, rebuild an index, call a model, or use the network on the prompt path.
- Use bounded, concurrency-safe writes owned by the store. Under contention, fail soft and expose incomplete coverage rather than corrupting events or fabricating complete measurement.
- Retrieval logs are observations, not decision evidence that can approve, anchor, share, retire, or arm a rule. Do not feed usage counts into authority or confidence.

## 3. User-visible impact without interruptions

Add an on-demand **Decision impact** view to the existing console, with the same projection available from existing status/diagnostic CLI surfaces. Do not start a separate service or introduce another dashboard system.

First deliverable: show existing engagement and coverage honestly, then enrich the same view as exact traces and checked outcomes become available.

The view should answer:

1. Which decision and revision were surfaced, and why?
2. Was the full decision emitted, only a pointer returned, or delivery unsupported?
3. Is there independent evidence that the resulting work followed it?
4. Which relevant decisions were missed in labeled tasks, and at which stage?
5. What is unknown because the host or task has no outcome evidence?

Use labels such as **Emitted**, **Looked up**, **Verified compliant**, **Violation observed**, **Clarification appropriate**, and **Outcome unknown**. Never display “mistakes prevented” or “hours saved” without evidence supporting that counterfactual.

A detail view can link a decision revision to an existing test receipt or reviewed artifact when available. Reuse results from already-invoked policy checks rather than adding another evaluator. A receipt must identify the checked policies/revisions, artifact identity, evaluator version, completeness and unchecked gaps, including passes rather than only violations. A check validates only its stated condition, not the whole task. No artifact, incomplete evaluation, advisory match, or empty policy set cannot establish verified compliance. It should not create or run checks without user authorization. Optional “not relevant to this task” feedback belongs here, not in an interrupting prompt; it does not ignore or retire the decision globally.

No new notification stream. Preserve existing recall-notice behavior for existing routes, but newly handled ordinary-task prompts must not add user-facing recall notices: emit model context and make its trace inspectable on demand. Simply reusing the existing notice on more prompts would still increase interruption. Do not add mandatory agent reporting calls. Clearly label the existing token-savings number as a benchmark-derived estimate, separate from measured token usage and controlled comparisons; any change to that ratified notice is its own reviewed change.

## 4. Establish a trustworthy baseline before tuning

Start with 12–20 deterministic golden scenarios covering the three reproduced failures, existing successful routes, negative prompts, revision/authority changes, and host output. This small suite is sufficient to assess narrow correctness fixes; it is not proof of improved completed-task outcomes.

Before broad retrieval changes, grow a fresh dataset toward approximately 120 task scenarios, initially split into 60 development, 30 validation, and 30 sealed test scenarios. These are collection targets, not a claim of statistical power or a prerequisite for every small fix. Use at least three consenting repositories and group splits by task/decision family so paraphrases of the same task cannot leak across splits. Add a repository-held-out slice where practical.

Each scenario freezes the pre-task checkout, decision revisions, authority/lifecycle state, pending proposals, global rules, cached team context if present, host capabilities, model/agent version, and standing session context. Never reconstruct a trustworthy baseline from today's mutable store alone. Exclude historically uncertain labels or report them as a separate diagnostic slice.

Required scenario types:

- Ordinary imperatives without paths: “Add payment retries.”
- Questions and explicit file requests that already work.
- Explicit multiword lookup with a small result cap and a frequently repeated but less relevant competitor.
- Synonyms and paraphrases, including vocabulary not in topic aliases.
- Hub files, incidental path mentions, and unrelated changes in the same file.
- Multiple applicable decisions, including tasks requiring more than three.
- Global process rules already supplied at session start.
- Revised, retired, withheld, pending, and conflicting decisions.
- Explicit exceptions and unknown scope; e.g. retries allowed only with an idempotency key.
- No relevant decision, missing captured knowledge, and requests outside the project.
- Concurrent sessions, task switches, compaction, missing indexes, and restricted filesystem access.

For each task, a reviewer labels which exact decisions apply, whether background context is sufficient, the required action or appropriate clarification, and an independent success check. A second reviewer adjudicates ambiguous and safety-critical cases without seeing retrieval predictions. Model suggestions can assist labeling but are not the sole authority.

Freeze the metric definitions, split hashes, validators, and thresholds before tuning. Keep historical corpora unchanged. Repeated benchmark runs must not write into the developer's real store or leak decisions between arms.

## 5. Relevance changes, one measured experiment at a time

### A. Narrow correctness fixes — first candidates

Preserve relevance order when truncating BM25-ranked explicit lookup results. Keep storage eviction and unfiltered listing behavior unchanged; do not globally rewrite `_keep_top`. Add tests for limits, ties, authority filtering, and existing literal/alias/file routes before deciding whether those routes also need ranking changes.

Make prompt dedup aware of the rendered revision/proposal identity rather than only decision ID. Same-revision repetition stays suppressed; changed guidance and compaction need explicit handling. Test lifecycle changes, legacy working-set migration, and failure recovery. Retired or changed instructions already in an agent's context cannot be erased by changing a sidecar, so correction behavior must be explicit and tested, not counted as automatically solved.

Review these as independent, small changes against golden scenarios before requiring a large live campaign. Do not claim their user-level outcome benefit from unit tests alone.

### B. Ordinary-task retrieval — first broader experiment

Separate cheap candidate discovery from the decision to inject content. Evaluate cached lexical/title candidates for meaningful task requests even when they are not questions and contain no path. This is not permission to inject on every prompt: acknowledgements and unrelated requests should remain silent, and no-match is a valid outcome.

Keep the current ranker, thresholds, standing rules, and output budget for the first experiment. Determine whether the trigger alone recovers missed tasks without increasing irrelevant full-content guidance. This isolates the change's cause.

### C. Better choice among file and lexical matches

Next evaluate ranking that combines an explicit anchor, title/body relevance, actual request intent, and source scope without treating an anchor to a common file as automatic proof of task relevance. Preserve broad candidate discovery. A low-ranked mechanical candidate remains available to explicit lookup rather than being erased.

Distinguish candidate retention from output size: strong results may carry full content; weaker results may get a bounded pointer; the rest remain searchable. Report recall at each stage, not only the candidate union. Standing process rules belong in the standing-context measurement, not falsely counted as prompt retrieval misses.

Only after trigger and ranking changes are measured, compare the fixed three-item cap with a bounded token/character budget and relevance threshold. Include all headings and conflict text in the budget; mark estimates as estimates. Never silently clip an exception or conflict explanation to fit. Keep zero-result behavior and a hard upper bound. Do not equate a broad candidate union with visible coverage: the current strong-result path returns before weaker pointers.

### D. Explicit conditions — contingent, not a v1 dependency

If remaining errors demonstrably concern conditions/exceptions, pilot optional applicability fields through the existing revision and review flow: supported include/exclude paths, operation type, and human-readable conditions. Preserve provenance, backward compatibility, and unknown conditions.

Only mechanically supported scope can act as a deterministic exclusion. AI interpretation of “unless idempotent” cannot silently suppress an approved rule or authorize an operation. Unknown semantic scope gets a qualified pointer or appropriate clarification, not a guessed hard verdict.

Do not add embeddings, an LLM reranker, a vector database, or a new model-facing tool as the starting solution. Reconsider them only if a labeled residual-error analysis shows a benefit that simpler changes do not provide, with separate latency/privacy/cost approval.

## 6. Prove the effect on completed work

Extend the existing benchmark plumbing with two primary arms:

- **Baseline:** the pinned current retrieval behavior.
- **Candidate:** one proposed retrieval change, with the same decision snapshot and standing context.

Use fresh isolated sessions/checkouts per task and arm, identical model/agent configurations, balanced randomized order, repeated runs, and the same independent validators. A starting sealed campaign of 30 tasks × 2 arms × 3 repetitions is 180 agent sessions; estimate and approve the cost before executing it. Repetitions do not count as independent new tasks.

A small diagnostic **oracle-guidance** arm can provide the reviewer-selected decisions directly. It estimates whether a failure is due to retrieval/delivery or to the agent's execution. It is a ceiling diagnostic, not a shipped feature. A static instruction-file comparison is optional later; it is not needed to establish whether this retrieval change improves on today's product.

Primary task outcome: the requested change is functionally correct **and** satisfies every applicable decision that has a predefined validator. Appropriate clarification on an unresolved contradiction can be correct; doing nothing on an implementable task cannot. Passing unrelated tests or mentioning a rule does not establish compliance.

Use deterministic artifact checks where they genuinely express the rule; use blinded human assessment for architectural judgments. Validate against frozen pre-task state and resulting changes so a pre-existing violation in a changed file is not automatically attributed to this task. Scoring should not merely repeat the same enforcement regex and declare the system independently validated. Record validator coverage and unknowns. Keep model self-reports separate. Pilot summaries must not automatically execute user commands, infer intent from arbitrary commits, or claim causal impact from observational correlations.

### Scorecard and proposed release gates

These are planning targets to ratify after the baseline/power pilot and before opening the sealed test set; they are not measured results or tuning knobs to adjust after seeing final scores.

| Metric | Definition / proposed gate |
|---|---|
| Useful-guidance precision | Applicable decision revisions / emitted decision revisions, separated into full content and pointers. Candidate full-content precision must not regress. |
| Decision coverage | Applicable revisions made available through standing context plus task guidance / all labeled applicable available revisions. Track whole-task coverage as well as per-decision recall. Target at least +10 percentage points on ordinary-task coverage. |
| End-to-end success | Functionally correct and decision-compliant tasks / assigned evaluable tasks. Target at least +10 percentage points versus baseline, with a paired uncertainty interval supporting improvement before broad rollout. |
| No-decision false positives | Fraction of labeled no-relevant-decision tasks receiving full-content guidance. Provisional ceiling 5%; report counts and uncertainty, not just a percentage. |
| Wrong-authority guidance | Retired, pending, or inferred content presented as settled policy, including a stale delivered revision after a known change. Zero known violations in safety/regression fixtures; a labeled pending conflict is not itself a violation. |
| Attribution / outcome coverage | Show exact-joined, unattributed, checked, and unknown counts by host. Never drop unknowns silently to improve the success rate; report conservative failure/unknown sensitivity. |
| User burden | Zero new mandatory prompts, blockers, or notifications. Track context size, repeated guidance, latency, and optional “not relevant” feedback separately. |
| Runtime cost | Preserve the existing 500-decision ranker performance gate; proposed added end-to-end warm-hook p95 ≤10 ms and p99 ≤25 ms on a fixed reference machine. Measure cold starts and missing-index fallbacks separately. |

Use task-clustered paired uncertainty estimates and report repository/host/task-type slices; do not treat 90 repetitions as 90 independent tasks. If the dataset is too small to support a decision, keep the feature experimental and collect fresh sealed cases. Do not ship just because retrieval scores improved while completed-task results stayed flat.

## 7. Implementation sequence and ownership

| Step | Deliverable | Existing owners | Exit condition |
|---|---|---|---|
| 1. Small baseline and measurement contract | Add 12–20 golden scenarios; define delivered vs checked vs unknown and minimal trace semantics. | Retrieval/store/adapter tests; existing benchmark helpers | Reproduced failures and existing behavior have deterministic expectations. |
| 2. Narrow correctness fixes | Preserve query relevance under caps; revision-aware prompt dedup, in separate changes. | `store.py`, working-set and query tests | Each regression is fixed without changing storage eviction, authority or unrelated ranking constants. |
| 3. Quiet traces and early visibility | Bounded structured selection/emission records; on-demand Decision impact; reuse actual policy receipts when available. | `store.py`, `sidecars.py`, `server.py`, adapters, `policy_api.py`, `console_api.py`, `ui/api.py`, existing CLI/UI | Users can inspect guidance and honest evidence gaps; disabled/failure/concurrency behavior is safe. |
| 4. Broader-task experiment and outcome harness | Fresh labeled snapshots and independent validators; test ordinary-task discovery against the pinned baseline. | `store.py`, `retrieval.py`, existing applicability and memory-campaign helpers | Offline gates pass; cost approved before real agent runs; no extra notices. |
| 5. Opt-in pilot and release | Run paired outcome evaluation, enrich existing report, document host limits and rollback. | Benchmark helpers, console/status, adapters and docs | Evidence-backed benefit with low burden, adequate outcome coverage, and no authority regressions. |
| 6. Conditional follow-ups | Hub-file ranking, adaptive budget, then explicit conditions only if residual errors justify them. | Existing retrieval owners and review flow | Each change independently improves measured failures. |

Each step should be a reviewable PR or small series. Do not combine every ranking idea into one experiment. Narrow correctness fixes can ship with proportionate regression validation; broad retrieval expansion needs baseline and outcome evidence. D remains a later schema proposal only if measured errors justify it. Teams PR checks are outside the first slice: use local sessions and existing cached team guidance first, with separately reported host/source coverage.

No new framework or generic telemetry platform is required. Keep persistence and loaded-entry policy in the existing owners, pure ranking in `retrieval.py`, projections in `console_api.py`, and host capabilities in adapters. Any new storage family or configuration option needs its concrete retention, migration, privacy, and disabled-mode contract reviewed with its PR.

## 8. Validation and rollout safeguards

- Unit tests for trigger negatives/positives, ranking explanations, exact revision selection, bounded output, and conservative unknown conditions.
- Integration tests through actual host adapter output and explicit MCP responses, not only a pure ranking function. Include Cursor's unsupported prompt delivery and Gemini's deferred restoration.
- Concurrent-session and worktree tests proving no false task joins or crossed feedback; missing/ambiguous IDs remain unattributed.
- Authority tests for pending, retired, bootstrap-inferred, conflicting, and changed revisions. High relevance or positive outcomes never grant approval or arm checks.
- Mutation-test outcome scorers: a wrong implementation, stale choice, empty edit, irrelevant test pass, and a fabricated agent claim must not score as verified success.
- Diagnostics-disabled, disk-full, malformed sidecar, contention, missing-index and latency tests. Shadow evaluation must not mutate the live working set, suppression, review state, or normal logs.
- Re-run targeted tests with `--no-cov` during development, then the required full suite, pinned Ruff check, and MCP smoke test before implementation PRs.
- Shadow/offline first, then opt-in session pilots on hosts with measurable delivery. Cursor remains an explicitly reported coverage limit rather than gaining a workaround that interrupts users. Validate other hosts independently before claiming parity.
- Roll back the retrieval variant independently of diagnostic history; preserve decision stores and user authority. Any wrong-authority regression, new interruption, or latency-budget violation stops expansion.

## 9. Immediate next action

Detailed first-slice specification: [Implementation contract 01 — regression baseline and measurement definitions](decision-relevance-outcomes-contract-01.md).

Start with the small regression baseline and two narrow correctness fixes, alongside the minimum measurement contract. Deliver quiet, on-demand visibility before expanding ordinary-task retrieval. Keep the first report useful even when every outcome is honestly marked unknown; add verified results only as real evidence becomes available.

This planning task does not authorize running paid agent campaigns, collecting/exporting user session data, changing production retrieval, or modifying Teams services. No production implementation or paid agent campaign was performed. Verification used existing tests and isolated synthetic stores only.

## 10. Code-review and verification record

Inspected the relevant end-to-end paths at [revision 5676247](https://github.com/bhargavamin/contexer/tree/5676247): pure retrieval and indexed selection; explicit lookup; eligibility, rendering and revisions; startup, working sets and compaction; Claude/Codex/Gemini/Cursor adapters; MCP identity; diagnostics and sidecar lifecycle; policy evaluation; console session projections; applicability and outcome benchmark runners/scorers. This is a targeted architecture review, not a claim to have read every repository file.

Existing tests run in an isolated inspection worktree:

- **66 passed:** store BM25 routing, prompt rendering/metadata, working sets, status filters, file routes, follow-through logs, compaction/session plumbing, and guard applicability ranking.
- **53 passed:** `tests/test_retrieval.py`, `tests/test_benchmark_applicability.py`, and `tests/test_bench_memory_campaign.py`. The campaign tests use stub agents, not paid model sessions.
- All subset runs used `--no-cov`. These are targeted checks, not the full release suite.

Isolated synthetic-store probes (no real decision-store writes):

| Probe | Observed current behavior |
|---|---|
| “Add payment retries” versus a rationale question using the same subject | The imperative returns no guidance; the question finds the relevant decision. |
| Approve a changed revision after its prior version was injected | The existing session misses the new text; a fresh session receives it. |
| Multiword query with a best-match decision and a high-recurrence incidental match | Both appear uncapped; `limit=1` retains the incidental match and drops the best-ranked match. |

These findings changed the plan's priority: small demonstrated correctness fixes first, broader task discovery second, and richer ranking/scope changes only after evidence shows the need.
