# Implementation contract 04 — decision guidance to a checked result

Status: implementation candidate complete on `codex/decision-relevance-outcomes-contract-04`; explicitly new opt-in pilot, disabled by default and pending review/merge.

Revised 2026-09-19 following the user's direction to deliver meaningful outcome evidence in the first iteration, accessible through the AI agent rather than requiring a console. Supersedes the earlier diagnostics-only Contract 04. No existing post-edit `evaluate_policy` workflow has been confirmed, so this contract defines an explicitly new opt-in pilot rather than claiming an established producer or ordinary-workflow coverage. The follow-up adversarial review at 51f1a1a identified missing workspace authorization, the unsupported workflow-adoption assumption, unbounded regex execution and inaccessible older receipts; this revision specifies corrections for all four. Continues step 3 of the [original plan](decision-relevance-outcomes-plan.md) after Contracts 01–03. Retrieval grounding includes merged Contract 03 at 7289a7e. Pin the actual integration base before implementation and preserve unrelated work.

## 1. Result and release boundary

Deliver one small, complete path:

**Decision revision supplied → resulting file independently read → existing deterministic rule evaluated → evidence available through the agent.**

The first release must demonstrate a passing result, a violating result and honest unknown states. A retrieval log with every outcome permanently unknown is no longer sufficient acceptance. The console is an optional presentation of the same report.

Illustrative user question: “What did Contexer contribute, and did this change follow the decision?”

An evidence-backed answer could say:

> Contexer prepared decision D, revision R, for explicit lookup. Its existing armed rule found no forbidden requests import in the server-read snapshot of src/client.py. That checks this condition on this file, not the whole task. The check was explicitly linked to the lookup; whether the guidance caused the result is unknown.

The positive claim is **Checked condition satisfied**, not “decision fully followed,” “agent obeyed,” “mistake prevented,” or “Contexer improved the result.” A deterministic regex is independent of the agent's self-assessment, but covers only its configured condition. Comparative benefit still requires the later baseline-versus-candidate evaluation.

No new mandatory checks, questionnaires, reminders, reporting calls or enforcement. Run an evaluation only when the user requests that check or explicitly opts into a defined checking workflow. If no suitable check runs, retain **Unverified**; opening the report must never run a check.

## 2. Smallest supported outcome slice

The required vertical slice is **an existing approved, trusted, explicitly armed rule in the bounded regex subset below, evaluated against one actual repository text file through the existing evaluate_policy MCP tool**. Reuse policy.select_policies and policy.evaluate_policies. Do not invent a natural-language validator, automatically arm rules, or expand approval authority to increase coverage.

### Supported first-use pilot and invocation boundary

This is an **explicitly new opt-in policy-checking pilot**, not a claim that normal coding-agent sessions already produce outcome evidence. The current code does not automatically invoke `evaluate_policy` after edits, and this contract does not add that behavior. Commit-time Guard users alone are not the initial cohort while that producer remains deferred.

Build and validate the complete workflow with isolated fixtures: explicit MCP lookup of applicable guidance → resulting repository file → one independently acquired policy evaluation → explicit impact query. Exercise compliant, violating and unverified variants with an existing approved/trusted/armed fixture rule. The fixture may establish correctness of the capability and its claims; it does not establish existing adoption, ordinary-workflow coverage or real-world benefit.

In real use, a check may run only when the user requests that specific check or explicitly opts into a separately defined checking workflow whose trigger, scope and disable path are clear. A file edit, prompt, retrieved decision, enabled diagnostics setting or workspace-read grant is not itself permission to run a check. Do not automatically arm rules, evaluate after every edit or prompt, add background checks, or require agents to make evaluation or reporting calls. An impact query remains an explicit read and never triggers evaluation.

The pilot invocation may replace agent-supplied file text with `artifact_path` and optionally carry `guidance_refs` on the **same requested evaluation call**. Record the exact fixture call sequence and assert one lookup, one requested evaluation and one requested report for the complete demonstration, plus negative cases proving that an edit or lookup alone causes zero evaluations and zero reports. Reviewing this plan, enabling diagnostics or granting a read root authorizes no live check by itself and does not authorize collecting private session transcripts or running a live/paid agent campaign.

Real-world usefulness is an evaluation criterion for the opt-in pilot, not a prerequisite for implementation and not something fixture success can prove. After correctness and safety gates pass, assess whether consenting users find the explicit workflow useful, whether its supported-rule coverage is adequate and whether its burden is acceptable. Report those observations separately from deterministic checked-condition results and do not infer that Contexer improved a task without a later comparative evaluation.

Include:

- Same-snapshot receipts for ordinary explicit get_context results, so MCP-first usage is observable, plus bounded indexed-prompt observations.
- Actual per-condition pass/fail/unchecked observations, bound to exact decision revision, rule configuration and artifact.
- A bounded repository-file input option on evaluate_policy, so an already-requested evaluation can read the file independently rather than trusting an agent-supplied copy.
- Exact receipt references where available, distinct from object-level associations and unknown task attribution.
- A read-only get_decision_impact MCP tool, local CLI projection and small optional console view over one shared report.
- Opt-in local collection, bounded storage, privacy, failure behavior, adversarial tests and rollback.

Defer:

- General test/CI ingestion, semantic architectural checks, automated test execution, cross-file behavioral validators, human-review ingestion and LLM judges.
- Commit-time Guard receipt production and every-host/surface coverage. Guard's enforcement and deliberately different selector semantics remain unchanged.
- Broader retrieval/ranking/budget changes, a large dashboard, remote diagnostics, Teams PR checks and paid campaigns.
- Working-set optimization: retain separate later step 3b. The [judge experiment](decision-judge-experiment-contract-01.md) remains separate.

Installations without a suitable existing rule or an executed check get guidance history but no fabricated checked outcome. Document this first-release coverage limitation. Existing caller-supplied artifacts remain usable but cannot establish actual repository state.

## 3. Code-grounded boundaries

- store._get_context_for_prompt and _render_prompt_decisions_with_records provide indexed selection and same-snapshot scoped fingerprints. Extend structured observations, not prose parsing or after-the-fact reloads. Explicit get_context also needs rendered identities from its real formatting boundary, preserving ordering, caps, conflicts and authority.
- contexer.server.evaluate_policy currently accepts caller-supplied artifact text. policy_api.evaluate_operation validates, selects and evaluates that text. The new file-input mode closes this specific artifact-provenance gap.
- policy.evaluate_policies returns aggregate verdict/status, matches, unchecked gaps and a policy-set version. These are not sufficient explicit passing receipts. Observe actual checks inside the evaluation loop; never infer a pass by subtracting violations from selected policies.
- policy_api uses the existing approved/trusted selector. Guard's explicitly armed selector has different trust semantics by design. Do not unify selectors for reporting.
- Adapter return/serialization proves preparation, not successful transport or agent consumption. MCP process identity is not universally a conversation boundary. Working-set and legacy followup logs cannot supply exact task attribution.

Keep pure evaluation in policy.py, artifact coordination in policy_api.py, retrieval/rendering in store.py, transport in server.py/adapters, projection in console_api.py, lifecycle in sidecars.py and settings in config.py. A cohesive diagnostics owner may validate/store receipts through store-owned public path/atomic-write interfaces; no telemetry framework, event bus, new daemon or store facade aliases.

## 4. Evidence contract

### A. Guidance observations

One bounded envelope per participating retrieval invocation contains:

- Version, opaque event ID, timestamp, producer version, canonical repository and separate physical-checkout identities.
- Route/host and separately namespaced host-session/request and MCP-process identities with provenance; missing stays unknown. No prompt text or prompt digest.
- Scoped decision ID, persisted revision or explicit legacy-unknown provenance, effective-view fingerprint, authority, visible proposal identity and actual full/excerpt/title/pointer tier, from the same rendered snapshot.
- Categorical selection/exclusion reason, rank where observed, configured cap and output bytes. Estimates stay distinguished from measured tokens.
- Selected, rendered and prepared as separate stages. Only final payload inclusion earns prepared status. Logging failure cannot replace a successfully constructed product result.

Instrument explicit get_context as well as indexed prompt retrieval. When collection is enabled and persistence succeeds, an explicit MCP retrieval may append one machine-readable opaque receipt reference to its existing result. This is the only required retrieval-output addition: no extra-call instruction or user-facing hook notice. Disabled/failed persistence adds no usable reference. Limit detailed decision/candidate rows to 32 and mark truncation; omitted identities cannot acquire evidence through a shared envelope.

For eligible armed guidance, include the normalized rule digest from the already-loaded snapshot. Rule identity is distinct from decision revision because a check configuration can change without text changing. Unknown persisted revision or ambiguous source scope cannot become an exact join.

Prompt exits distinguish gate_closed, no_candidate, already_delivered_revision, budget_limited, render_skipped, branch_uninstrumented and error where observed. A closed gate does not establish that no relevant decision exists. Do not rerank, reload excluded entries or recompute whole-store fingerprints for explanations.

### B. Independently acquired file

Extend evaluate_policy and its policy_api owner with optional artifact_path, one repository-relative file, as an alternative to existing caller-supplied content. This changes the input of an already requested evaluation, not the number or timing of checks.

**Authorize the root before resolving the file.** Add a developer/operator-owned [policy] artifact_read_roots list to the existing config owner, default empty and capped at 16 exact absolute physical project roots, each at most 300 characters. These are explicit file-read grants, separate from diagnostics consent. A root is enrolled only by the developer/operator or an agent acting on their explicit request; no model-facing evaluation/report parameter, guidance receipt, repository marker or discovery heuristic grants access. Preserve the list through every config writer and validate its bounds/types. Store canonical physical paths at explicit enrollment; refuse a hand-edited symlink/noncanonical grant rather than silently following it to a different root. Refuse filesystem root, the user's home or its ancestors, and the existing protected config directories; never expand an entry to a parent or infer sibling worktrees. Non-Git projects can be explicitly enrolled too.

Resolve the requested physical project against these exact grants before loading an artifact. An omitted repo_path may use the process's startup-bound physical project only if it matches a grant; otherwise refuse file mode with workspace_not_authorized. Never use .current_repo fallback for source reads. An explicit repo_path selects among grants, it cannot create one; an invalid or unmatched argument is an error, not permission to fall through to a different workspace. Store-key canonicalization may share decision/history storage between worktrees but cannot broaden the granted physical read root. Each physical worktree requires its own grant.

Use bounded config reads on each file-mode invocation so revocation applies to the next invocation; malformed/unavailable grants fail closed. Bind the granted root to a directory handle during acquisition and validate subsequent path components relative to it, rejecting symlinks/special files and resolution/open races. Do not use an earlier string-prefix test followed by an unconstrained open. If the platform cannot enforce the confined open, report file mode unsupported rather than weakening the boundary. This restricts the new capability only: do not change legacy repository resolution for decision retrieval or caller-supplied evaluation.

- Validate mutually exclusive modes. File mode derives file_content and the checked-file list from the validated path; reject conflicting artifact/files arguments rather than letting the caller disguise applicability.
- Resolve against the authorized physical checkout. Reject absolute/escaping paths, .git metadata, symlinks and special files. No globbing, directory scans, Git, subprocess or network.
- File mode has a stricter first-release cap of 64 KiB, never greater than policy.MAX_ARTIFACT_BYTES; read at most that cap plus an oversize sentinel from one handle. Also cap each decoded line at 8,192 characters before evaluation. Binary, invalid decoding, oversized or unreadable input is unchecked/error, not empty content. Detect in-read mutation through file identity/stat checks; instability cannot earn verification.
- Compute versioned SHA-256 over the exact acquired bytes and evaluate their lossless decoded text. Persist only a bounded relative-path reference, digest, byte count, artifact kind and acquisition provenance, never bytes/snippets. Do not reread a different version for its digest.
- Bind evidence to that snapshot, not HEAD, branch or mtime. A later edit leaves historical evidence valid but does not establish the current file. Reading the impact report never rereads or rechecks the artifact.

Caller-supplied text retains its existing advisory behavior and is labeled **Caller-supplied artifact — repository state unverified**. A claimed path/digest cannot upgrade provenance. The new input mode remains available when history collection is disabled; evaluation is an explicit operation, while recording is optional.

### C. Bounded execution for the new file mode

A byte cap alone does not bound Python regex execution: the reviewed evaluator exceeded a 0.5-second isolated test deadline for (a+)+$ on a 31-byte input. Do not run arbitrary stored regexes in the new file mode and call it bounded.

Use a versioned bounded_file_v1 evaluation profile, set by the deterministic file-input owner, not selectable/bypassable by the model. Its first-release supported regex grammar is one nonempty ASCII literal of at most 128 decoded characters, optionally preceded by ^ and/or followed by $. Literal regex metacharacters must be escaped. Permit backslash escapes only for literal regex punctuation/backslash; reject other escapes, word boundaries, groups, classes, alternation, quantifiers, lookarounds, backreferences and inline flags. Only an empty flags value is supported in this first profile. This deliberately narrow grammar supports rules such as a forbidden literal import statement; it does not claim to understand Python imports semantically.

Validate this grammar with a bounded pure scanner in the policy owner **before** passing a rule to the existing matcher. Continue to use the existing matcher once admitted; do not create a second interpretation of rule semantics. No unsupported pattern reaches compile/search in file mode. Record excluded rules as unsupported-check with a closed profile detail, preserve their standing approval/arming, and leave legacy caller-supplied evaluation and Guard behavior unchanged. Their pre-existing regex runtime limitation is not fixed by this slice.

Cap the selected armed-rule set for file mode at 16. If it exceeds that bound, reject the bounded evaluation with budget coverage gaps rather than silently choosing an apparently successful subset. With at most 64 KiB of input, 8,192 characters per line, 16 rules and 128 literal characters per pattern, the admitted work has bounded input/product size and no regex backtracking constructs. Add a 250 ms soft evaluation deadline checked between admitted rules; remaining rules become budget/unchecked. This is not a hard real-time wall-clock promise: scheduler/filesystem latency can exceed it, but a single unbounded regex search cannot be entered.

Errors, unsupported rules and budget exhaustion never become passes. A completed supported condition may retain its own result while the overall report explicitly shows other conditions unverified. Include profile/grammar version in evaluator identity. Invalid/oversized profile input is rejected before matching; the new capability uses the same safety profile even with diagnostics disabled. Profiling or diagnostics must not add a second check run.

### D. Actual checked-condition receipts

Observe checks once at the existing evaluation boundary. An optional observer must not throw into the evaluator, alter verdicts, change authority or cause a second evaluation.

Required metadata:

- Evaluation ID, producer/evaluator version, canonical repository and physical checkout.
- Artifact identity and acquisition provenance.
- Exact scoped decision ID, persisted approved revision, authority, normalized rule digest, rule type, policy-set version and evaluated file scope.
- Per-condition satisfied, violated, unchecked or error result; applicable/evaluated unit counts; completeness and closed-vocabulary gaps.
- Snapshot provenance, truncation and optional guidance references.

A verified result requires a bounded_file_v1-compatible regex selected through the existing approved/trusted/armed policy path, bytes acquired from an authorized workspace, at least one completed applicable evaluation, known persisted revision and unambiguous scope, and no error/gap/truncation affecting that condition and artifact.

**Checked condition satisfied** requires an actual completed zero-hit check. **Violation observed** requires an observed match. A genuine empty file may satisfy that regex if actually evaluated; it does not prove the implementation is complete. Empty policy sets, zero applicable chunks, omitted content, advisory warnings, parse failures and timeouts are not passes. A violation found during a partial evaluation can remain an observed violation, with partial coverage; the whole artifact cannot become compliant.

Carry personal/global scope through selection without changing eligibility. An ambiguous ID collision is unverified, not joined by bare ID. A later revision or rule change never rewrites an old receipt. No receipt approves, anchors, arms, retires, shares or increases confidence in a decision.

### E. Attribution without invented causality

Add optional guidance_refs to the existing evaluation call, capped at eight bounded opaque references. They identify retained Contexer-minted guidance receipts, not agent-reported outcomes. Validate repository, physical checkout, scoped decision, persisted revision and rule digest before linking. Invalid/expired/truncated references produce a linkage gap without changing the check verdict.

An explicit reference means **Caller-linked to this guidance receipt**, not proof of reading, obedience, authorship or causal benefit. The agent may pass it on an evaluation it was already making; normal work never requires it or a separate reporting call.

Without a reference or a demonstrated host request bridge, a checked artifact can be associated with the same scoped decision/revision/rule as **Object-level association; task link unknown**. Repository/time/topic similarity and same-process identity cannot establish a task. Do not add a global current-task pointer or select a convenient pass from another worktree.

This first release can therefore prove a specific file snapshot satisfies a specific condition even when its task linkage is unknown. Explicitly linked fixtures additionally prove the complete guidance-to-check reporting path. Neither proves Contexer caused the result.

## 5. Agent-first report

Add a thin read-only get_decision_impact(repo_path="", files=None, limit=10, receipt_id="", cursor="") MCP tool, with maximum 50 envelope rows per page, a 64-KiB response bound and explicit truncation. File filters are bounded repository-relative metadata selectors, not arbitrary file reads. Resolve/read one repository only. Do not run checks, change settings, clear history, start the UI or count report reads as new activity.

Support both exact bounded receipt lookup and forward traversal of all retained history:

- receipt_id selects one guidance or evaluation envelope by its returned opaque ID, independent of its position among the newest 50. It is a validated identifier lookup within the resolved repository, never a filename/path. Reject combining it with cursor. Return not_retained_or_unknown for an absent ID; without a retained tombstone, do not claim to distinguish expiry from a never-existing ID. An expired record is not resurrected.
- Assign a monotonic per-repository sequence under the existing history lock and a history epoch that changes on clear/reset. Order pages by descending sequence, not wall-clock time; IDs identify receipts, sequence establishes local publication order only, never task causality.
- A bounded opaque cursor binds schema version, repository, normalized filters, history epoch, the first page's high-water sequence and the last returned sequence. Later writes above that watermark do not reorder or repeat the paged snapshot. Bind/validate all fields again; a cursor cannot authorize another repository, arbitrary read offsets or a larger response.
- Include next_cursor when more matching retained envelopes remain. Respect both row and byte caps without cutting an envelope's meaning or silently ending the traversal. Exact lookup must still fit the response bound. All surface adapters reuse these semantics.
- Clearing/resetting invalidates prior cursors. Retention that removes the unvisited range returns an explicit history_changed/restart-required gap, not a false complete history; keep a bounded pruned-through sequence in the history header. Bound cursors to 1 KiB and reject malformed/future/cross-filter values. A page read itself changes no state.

Acceptance must enumerate more than 50 retained same-file events with no duplicates or omissions in stable history, find an old receipt directly, and cover concurrent appends, expiry/eviction, clear, malformed cursors and response-byte boundaries. These bounds keep reads finite without making the retained 256-record history inaccessible.

Why a new tool: the intent is to inspect observed guidance and results, not retrieve instructions or perform another evaluation. Overloading get_context would mix evidence with operative decisions; overloading evaluate_policy risks executing work when the user only requested history. Deterministic code, not the model's arguments, enforces provenance, joins, completeness and permitted claims.

Share one projection with contexer status --impact [--json] and a minimal existing-console panel. CLI reading bypasses status's version/network refresh and release-notice tail. An MCP-only user can request results without a shell/browser once collection is configured.

Separate four dimensions:

1. **Guidance activity:** revision, reason and actual tier prepared; delivery remains unconfirmed.
2. **Checked results:** condition satisfied, violation observed, unchecked/error; artifact snapshot and evaluator.
3. **Attribution:** caller-linked, proven host bridge if available, object-only association or unknown.
4. **Coverage:** off/error, retained window, absent checks, unsupported conditions/hosts, expired receipts and partial history.

Counts must state unit/window and deduplicate check IDs; never label them decisions applied or divide by all prompts/tasks. Current decision titles are current metadata, not historical evidence. Escape/sanitize display strings and apply existing egress redaction; relative paths can also be sensitive. Do not expose artifact bytes, regex text or match snippets through the new report.

Claude/Codex must be explicitly distinguished despite shared rationale code. Gemini observes only its ordinary retrieval segment actually retained in the final payload. Cursor's automatic prompt injection stays unsupported, while its explicit MCP lookup/evaluation/report path is supported. Startup, compaction and cached team guidance remain explicit uninstrumented coverage, not zero activity. Preserve existing notice frequency/wording and keep estimated token savings separate.

## 6. Consent, persistence and reliability

Retain the existing-config proposal: [diagnostics] decision_impact = false. Explain that opt-in collects bounded local metadata for all repositories used by this installation. Installation, upgrade and report reads do not enable it. Preserve the setting across every config writer, including team login/UI saves; malformed config fails collection off. A bounded read/cache notices changes on the next invocation without a watcher. Disabled mode creates no trace/lock/store solely for reporting.

An agent with filesystem access can update this setting at the user's request. MCP-only deployments need one-time operator configuration of diagnostics and, separately, exact artifact_read_roots for file-mode evaluation. Neither enabling diagnostics nor adding a root implies the other permission. The read-only report cannot change either. Document this limitation rather than requiring the console. Config writers must preserve both tables and retain existing credential-write restrictions; a user-facing settings change must not silently enroll a workspace.

Register one dedicated per-repository history family for versioned guidance/check envelopes and one independent durable lock inode. Use existing store path/private-permission seams. Bounds: 256 records, 256 KiB including the fixed-size pagination header, and seven days per canonical repository; 16 KiB per envelope. All fields/lists are bounded. Preserve essential checked-result metadata first; if it cannot fit, drop or mark incomplete rather than fabricate a complete pass. Evict oldest sequence records so a bounded pruned-through watermark can describe retention gaps; if clock changes make an interior record expire, prune the prefix through that record and disclose the shortened history rather than creating an untracked hole. Evicting guidance can make a link unavailable without invalidating an independently observed check receipt. Validate header/version/sequence integrity on reads; a malformed cursor or ledger cannot reset it silently.

Write once after each participating invocation's result is determined, under a dedicated nonblocking advisory lock with no retries or decision-store lock. Bounded read/prune plus atomic replacement protects successful concurrent writers. Missing is empty; malformed, unreadable, oversized or future-version history is not empty and must remain intact. Record failures cannot change product output, evaluation verdicts or enforcement.

Never persist prompts, conversations, decision text/titles, source, regex text, secrets, arbitrary exceptions or diffs. Retain only allowlisted IDs/digests, closed reasons, rule type, relative artifact references and counts. Hash namespaced session and repository/checkout identifiers; this is not an anonymization guarantee. No evidence-reconciliation input, confidence updates, cloud sync or diagnostic export. Explicit report responses expose only bounded sanitized metadata through the user's existing agent connection.

Prune on successful writes and hide expired records on reads; existing lifecycle maintenance handles cold files. Physical deletion is not guaranteed while the app is not running. Disable stops collection but retains history. Provide confirmed repository-only clear through authenticated console controls or explicit CLI contexer status --impact --clear --confirm. This is a write mode, never implicit in reads or exposed by read-only MCP. Clear only this history under its lock, never decisions, working sets, legacy logs, evidence or lock files. In-flight operations may write afterward.

Coverage is best-effort. Failed writes cannot reliably count themselves; total loss stays unknown, not zero. No retained observations does not mean no Contexer activity. No extra prompt-time model/process/network/Git/index rebuild/spool scan/whole-store load for diagnostics. Actual file reads/checks occur only in the explicitly invoked evaluation. Nonblocking locks do not imply latency-free filesystem I/O; measure bounded costs.

## 7. Implementation and adversarial validation

1. **Pin the base and freeze the first-use pilot.** Record that no existing post-edit `evaluate_policy` workflow was confirmed and do not manufacture a baseline adoption claim. Define the explicit opt-in fixture procedure and its supported rule/input eligibility in an isolated repository with explicit read-root grants and pre-approved/armed rules. Exercise actual MCP lookup → requested post-edit evaluator invocation with file input → requested impact query, without the console. Cover pass, violation and unverified variants; assert the exact expected call sequence and prove that editing or retrieving without an explicit check request runs no evaluation or report.
2. **Freeze receipts and pure claim rules.** Test schemas, snapshot/rule identity, provenance, bounds and joins. Agent-supplied “passed,” wrong revision/rule/file/worktree, unrelated test pass, empty selection and omitted content cannot qualify.
3. **Implement consent, authorization and storage.** Cover preservation of both config tables, separate diagnostics/root permissions, default-empty grants, revocation, startup binding, non-Git projects, separate physical worktree grants and refusal of ungranted broad roots/pointer fallback. Test lifecycle, relocated stores, permissions, expiry, truncation, corruption, disk-full, contention, sequence/epoch updates, concurrent clear/write and missing links. No collection in real user stores during tests.
4. **Observe retrieval.** Start with explicit lookup; add scoped indexed prompt paths. Assert actual rendered identities/tier, final inclusion and unchanged ranking/caps/authority. Preserve Contract 02 relevance and Contract 03 suppression/compaction. Only the enabled successful explicit-MCP receipt footer may change retrieval output.
5. **Add confined file input, bounded execution and evaluator receipts.** Cover unauthorized roots, traversal/symlinks/root replacement/open races, mutation, encoding, binary, empty/oversized/unreadable files and conflicting modes. Read/hash/evaluate identical bytes. Test the admitted grammar against existing matcher semantics, then mutations/unsupported quantifiers, alternation, escapes, flags and malformed rules. Assert the matcher is never called for (a+)+$, oversized lines or over-limit rule sets; exercise soft-deadline exhaustion and prove remaining rules are unchecked. No rerun, selector unification or changes to legacy evaluation/Guard verdicts.
6. **Connect references and agent-first projection.** Validate references on the already-invoked evaluation; expose link strength honestly. Test MCP process reuse, multiple worktrees/sessions, duplicate IDs, stale rules and privacy. Add shared MCP/CLI output and minimal optional UI, exact receipt lookup and bounded pagination. Traverse all 256 same-file records in stable history and verify concurrent append/retention/clear/cursor error behavior.
7. **Prove outcomes and limits end to end.** A safe file yields satisfied, forbidden content yields violated; subsequent edits leave historical-only evidence. A changed decision/rule cannot inherit the prior pass. Repeat with no check, disabled collection and failed persistence. The report must never execute evaluation, ask for feedback or grant authority.
8. **Validate and hand off.** Run baseline, targeted/full tests with coverage, lint, MCP smoke and separate uninstrumented performance comparisons. Record exact source/dependency/interpreter identities, imported paths, fixture hashes, commands and results. Document supported workflow and limits; do not enable collection or claim measured real-world benefit.

Required negative cases also include: selected guidance skipped by rendering; discarded Gemini segment; logger throwing after successful serialization; transient legacy revision ID; expired guidance with retained check; excerpt mislabeled full; changed armed rule at unchanged decision revision; zero applicable chunks; pass in unrelated caller-supplied text; old passing artifact mistakenly presented as current; partial check inflated into whole-task compliance. Mutation tests must fail when provenance/claim gates are removed.

Performance: compare clean base/candidate at 500 decisions, sparse/full 500-record working sets, fresh/unchanged/revised prompts, off/on collection and empty/full/contended history. Report uninstrumented median/p95 and output/receipt bytes. Existing gates stay unchanged; investigate repeatable disabled overhead above 10% or enabled incremental prompt p95 above 2 ms. Measure near-cap bounded_file_v1 checks with the maximum admitted rule count/pattern length, many short lines and worst-case repeated literal prefixes, plus complete paged-history traversal and old receipt lookup. Soft-budget tests are deterministic; any dangerous-regex regression probe runs only in an isolated test subprocess with a hard test timeout and cleanup, never in the live MCP process. These are review tripwires, not real-time guarantees. The existing working-set slowdown remains a separate later fix.

Run the existing relevance baseline (JSON), affected tests with --no-cov, uninstrumented perf tests, full tests/coverage, pinned Ruff 0.15.4, whitespace checks and the documented MCP initialize smoke test. Include policy, policy API, server, config, console/UI/CLI, module-boundary and store-directory seam tests. Follow current CLAUDE.md benchmark-maintenance instructions if adapters change; report a planned/unavailable runner honestly. R02/R03 stay passing, R01 remains deferred. No weakened expected gaps or paid campaigns.

## 8. Acceptance, rollback and later work

- [x] An isolated first-use fixture exercises the complete explicit MCP lookup → requested post-edit evaluator with server-read file → requested impact query workflow and demonstrates satisfied, violated and unverified conditions without opening the console.
- [x] Edits, prompts, lookups, diagnostics enablement and read-root grants trigger zero checks or reports on their own. Real checks run only on a specific user request or a separately explicit opt-in workflow, and reporting remains optional and read-only.
- [x] File reads require separate exact physical-workspace grants; arbitrary repo_path, shared-pointer fallback, invalid/revoked config, symlink escape and ungranted sibling worktrees cannot authorize access.
- [x] The new file mode enforces bounded_file_v1 grammar/input/rule-count limits before matching; unsupported/budget-exhausted checks remain unverified, with unchanged legacy evaluation and Guard behavior.
- [x] Exact revision/rule/artifact provenance and evaluated-unit/completeness evidence support every checked claim. Caller text or model assertions cannot establish actual repository state.
- [x] Caller-linked, object-associated and unknown attribution stay distinct. Prepared is not consumed; condition satisfaction is neither task success nor causal benefit.
- [x] Missing checks remain unverified. No background checks, auto-arming/approval, extra nagging or diagnostic effect on retrieval/enforcement.
- [x] Explicit lookup and supported prompt paths are observable; excluded surfaces and one-time MCP-only configuration limitations are documented.
- [x] Shared read-only reports agree; every retained receipt is accessible by exact ID and stable history can be paged completely. Appends cannot reorder pages; retention/reset gaps and byte truncation are explicit.
- [x] Privacy/retention/failure/clear behavior and preservation of diagnostics plus root-grant settings pass.
- [x] Complete adversarial fixtures, unchanged regression baseline, full coverage, lint, protocol and performance checks pass. A diagnostics-only deliverable cannot satisfy this contract.

Rollback collection by disabling diagnostics; normal retrieval and existing evaluation continue. Independently revoke artifact_read_roots to disable new source reads. Revert optional file-input/report/receipt changes without rewriting decisions, armed checks or working sets, preserving settings/lifecycle declarations where needed for compatibility and cleanup. History reset invalidates cursors. No global session clearing or threshold changes.

Next: separate step 3b scoped working-set membership optimization, with identity/output parity and before/after timings. Then broaden check sources and host coverage, collect fresh ordinary-task retrieval labels and run cost-approved paired completed-task evaluations. Later work asks whether Contexer improves outcomes versus not using it; this iteration already establishes whether an observed file snapshot satisfies a specific decision-backed condition.

## 9. Adversarial-review disposition

| Finding | Required correction and evidence |
|---|---|
| Caller-chosen root is not read authorization | Separate default-empty developer/operator grants; exact physical-root matching before file access; no pointer fallback; negative grant/revocation/worktree/open-race tests. |
| Seeded evaluator call does not establish normal outcome coverage | Treat this as a new opt-in pilot, not an existing-workflow cohort. Isolated fixtures prove correctness and invocation boundaries only; edits/lookups alone run nothing, while a specific request exercises one check. Evaluate usefulness with consenting users after implementation and report it separately from deterministic outcomes. |
| Small inputs can hang an unrestricted regex | New file mode admits only bounded_file_v1 literal patterns and fixed work/input limits; no unsafe pattern enters the matcher; soft-budget exhaustion is unchecked. Legacy regex limitations remain disclosed, not claimed fixed. |
| Old retained evidence cannot be requested | Exact receipt lookup plus repository/filter/epoch-bound keyset pagination; complete traversal and concurrent-retention/reset tests. |

The implementation remains inert until a developer/operator separately enables local diagnostics and grants exact physical read roots. The isolated workflow and acceptance tests establish capability correctness and claim boundaries; they do not establish pilot adoption, real-world usefulness or causal task improvement.
