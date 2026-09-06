# Bootstrap

Bootstrap builds useful repository context without a setup interview or routine approval
questions. Hooks request bootstrap automatically for a context-less repository, regardless
of Git history or authorship. The host agent performs the analysis through `bootstrap_context`.
Existing or legacy decisions do not count as a completed bootstrap report: those repositories
also receive the automatic request while preserving their standing decisions.

The first-run notice invites: **Ask “Run Contexer bootstrap” to discover this repo's decisions,
rules, and conventions.** This works as a chat instruction across hosts; Claude also installs
`/bootstrap`. Displaying the notice does not count as starting or finishing bootstrap. The
first prompt independently requests automatic discovery once on hosts that support prompt-context
injection (Claude, Codex and Gemini), even when the notice was missed. Cursor relies on session
start because its prompt hook cannot inject context.
Later sessions request unfinished analysis again. Completion stops these requests; hooks cannot
guarantee execution by a host agent that ignores tool instructions or has disabled hooks/MCP.

## What happens

1. Contexer snapshots bounded local Markdown, configuration, implementation and test sources.
   Parsed configuration facts are saved immediately as observed, non-human-approved context.
2. The host agent examines documented rules and relevant implementation/tests. Each reported
   finding includes its scope, reasoning and exact, fingerprinted source excerpts.
3. Contexer validates the report and saves grounded inferences as usable, provisional context.
   Historical documents and unsupported code-only hypotheses do not become active guidance.
4. Only concrete material discrepancies enter clarification. Both sides and one focused question
   are shown; unrelated findings remain usable. Agreement with code is not human approval.
   Conflicting documented prescriptions stay pending together, even if code supports one.
   Exact excerpts containing the counterpart line link them into one evidence-backed choice in `clarifications`;
   ambiguous multi-candidate ranges require explicit `against_candidate_ids`;
   unchanged disputes are not asked again. A human answer is applied explicitly to each affected
   decision, never inferred from which implementation happens to exist.
5. The agent lists what was actually saved, with evidence links and an optional “Anything to change?”
   invitation. No answer is required to continue.

An optional question offers shared Markdown outside the repository. Only user-authorized,
specific paths are read. Links in documents do not authorize external reads. Paths are remembered;
an empty `external_paths` list removes them. Old inferences based on removed sources are withheld.

## Observations, inferences and decisions

- **Observed:** what configuration or inspected code currently contains.
- **Inferred:** a source-backed interpretation, usable in future sessions but not human-approved.
- **Human-confirmed:** an explicit decision or correction, which inferred context cannot replace.

Using SQLite does not establish “never use PostgreSQL.” Production PostgreSQL and SQLite
unit tests are different scopes, not inherently contradictory. Unverified means unknown,
not compliant and not conflicting.

Inferred context is rendered separately from project policy, with its scope and evidence.
It cannot become an enforced rule or qualify for automatic human-approved sharing. Those
existing authority boundaries remain unchanged.

## Corrections and freshness

An explicit conversational correction uses `approve_decision(action="edit", entry_id=..., content=...)`.
It appends a human-directed revision to the same bootstrap decision, including when the first
capture was only inferred. The original text and evidence remain in revision history. Console
content edits have the same semantics. Rescanning cannot undo a human correction or revive a
deliberately ignored/deleted decision.

Source fingerprints include uncommitted changes. Unrelated file edits no longer reject the
whole report: each finding validates its own exact citations against its analysis-time file
fingerprints and current disk. Changed citations are returned in `deferred`; valid peers
commit together with their receipts. Malformed report structure still rejects the batch.

Capture completion and current applicability are separate. Changed, missing, symlinked or
no-longer-authorized evidence withholds the affected inference. Exact evidence restoration or
a fresh grounded report can lift evidence withholding, but cannot revive historical,
unsupported or human-dismissed decisions. Human-approved decisions are never silently withdrawn.

Uncited inventory drift adds an `inventory_unassessed` caveat to model-reported context.
Unchanged source-local parsed facts stay available without that caveat. This is uncertainty,
not proof of contradiction: inferred context remains non-authoritative and visible. Budget
warnings and inventory caveats have separate owners. Rechecking citations clears only citation
warnings, never an unassessed inventory change.

The returned `inventory_delta` includes an exact fingerprint and up to 40 paths to assess
(with bounded coverage explicitly disclosed). After examining its effect on retained inferences
and current human decisions, the agent submits `assessed_delta=<id>` with the current
`snapshot_id`. A second change invalidates the old delta acknowledgment. A matching assessment
clears the inventory caveat durably across session starts; it does not approve decisions or
lift known-invalid citation withholding. Merely opening a new scan does not count as assessment.
An exact return to the assessed inventory also clears that caveat.

Session start checks applicability without waiting for a busy store lock. Contention or a failed
read produces a **render-only freshness caveat**, not new withholding, and does not reactivate an
already-known stale finding. No contention state is written or migrated. If a check succeeds but
saving its bookkeeping fails, known-invalid evidence remains withheld in that session's view.

`snapshot_id` binds a monotonic analysis generation, checkout, bounded file map, authorized
paths, reported observations/receipts and local/global decision heads. Decision heads use raw
status with the legacy `approved` default, not derived freshness withholding. Thus human
corrections invalidate an old report, while SessionStart cannot supersede its analysis token.
Even an A → B → A analysis transition cannot reuse an old token. A second agent with a stale
token must rescan; there is no automatic merge of competing interpretations.

A candidate hash identifies an **observation version**, not a stable decision. Rewording or
superseding a documented rule requires explicit `replaces=<decision UUID>` to revise that same
record. Old observation versions cannot be replayed to recreate superseded decisions.
Vanished/changed observations leave active report replay; their stored decisions and history
remain inspectable, with invalid evidence withheld. Inventory omission alone does not establish
deletion. Pending conflict groups include withheld members: if any member needs checking, the
whole automatic question is deferred and a `recheck_worklist` is derived in the tool response.
The worklist is not another persisted artifact.

Scans still serialize source authorization, snapshot construction and persistence under the
store lock. No full source blobs are retained: current disk must support a quote, and rendering
continues to warn when the captured file/line no longer supports it.

Parsed JSON evidence is located by its object-member path, not by the first matching word.
Escaped keys, duplicate keys (last value wins), and multiline values are handled consistently
with parsing. If a complete excerpt exceeds the evidence budget, automatic capture omits that
fact and leaves it for host inspection rather than attaching an incomplete quote.

## Completion and limits

The first tool call returns `stage="interpreting"`, not “complete.” The host submits findings
using the returned `snapshot_id`, then `finish=true` once every nominated document candidate
is accounted for. Candidate receipts distinguish actual save/protection outcomes from deferred, superseded and
needs-recheck observations; completion does not claim deferred findings were saved as decisions. Each batch
returns the current snapshot ID for the next call. Interrupted
analysis is requested again in a later session; identical completed scans do not create copies
or increase confidence.

`reported_complete` means the host reported completion for the bounded snapshot. It is **not**
a guarantee of exhaustive capture or verified architectural correctness. The deterministic
comparison currently supports an unambiguous root Ruff line-length rule; other semantic
comparisons are the host agent's evidence-backed interpretation. Citation validation proves
the cited text exists, not that every interpretation of it is correct.

Automatic facts currently cover Python requirements/dependencies, Node requirements/dependencies,
and Ruff line length. Unlike the previous bootstrap, this path does **not** run the convention
miner's AST percentages, import/test patterns or commit-style measurements. These require host
inspection/reporting; no equivalent deterministic coverage is claimed. The miner remains for
re-verifying eligible legacy scan records, with an early return before mining when none exist.
There is no automatic promotion of new inferences to human-approved rules.

Budgets: 160 files, normally 100 KB/file, 2 MB total source text, 20 nominated document rules, 40 findings/batch
and 80 findings/scan. Evidence excerpts are at most 20 lines/2000 characters, eight per finding.
For focused recovery, start a new scan with `source_paths=["contexer/store.py"]` (up to 20
repository-relative paths). These files receive priority and may be up to 2 MB each, still
within the total snapshot budget. Focus is remembered; [] clears it. Files larger than 2 MB
remain outside supported citation coverage. Changing focus starts a new interpretation snapshot.
Discovery limits are not proof that old evidence disappeared: prior citations get an independent
bounded fingerprint recheck. If that separate 2 MB budget is exhausted, existing context is
labeled freshness-unverified, not silently withdrawn or declared current. Focused scans can
recheck and re-analyze skipped sources; actual changes, deletion, symlinks or revoked external
authorization still withhold affected inferences.
Dependency/build trees, symlinks, generated documents and unsupported file formats are skipped.
Coverage and omissions are returned explicitly. Existing decision previews are capped; the
agent retrieves full relevant decisions through `get_context`.

Malformed durable state refuses writes. A full store refuses bootstrap admission instead of
evicting existing decisions. Failed report validation commits none of that batch; facts saved
by an earlier successful scan remain saved.

## API

`bootstrap_context(repo_path=..., apply=false)` previews without saving decisions or consuming
the external-document invitation. The obsolete `insight` parameter and familiarity questionnaire
are removed; callers should omit it. No additional model account or background LLM is
required: the connected host agent performs interpretation. A host that does not execute the
requested tool workflow cannot complete semantic bootstrap; Contexer keeps that work incomplete.
