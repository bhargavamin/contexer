# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) and other coding agents when working with code in this repository. It holds what every session needs; detail loads on demand from [`docs/architecture.md`](docs/architecture.md), [`contexer/adapters/CLAUDE.md`](contexer/adapters/CLAUDE.md) and [`benchmarks/CLAUDE.md`](benchmarks/CLAUDE.md). Keep this file an index: new module detail goes in the reference or the module docstring (`tests/test_claude_md.py` caps its size).

## Working in this repo

**During a session**, call `update_context` whenever you make a significant decision, establish a pattern, or document a constraint. This is mandatory, not optional. Call it proactively within the same turn a decision is made, rather than waiting for the next prompt's reminder.

Call `update_context` for any of these:
- A technology, library, or approach chosen over alternatives (subtype: `architecture`)
- A file structure, naming convention, or code organization pattern established (subtype: `pattern`)
- A rule stated by the user or inferred from their preferences: "always X", "never Y" (subtype: `constraint` or `convention`)
- A build, deploy, or tooling convention established (subtype: `convention`)
- Any decision that would surprise a future Claude session if it wasn't stored
- A synthesized understanding of how a subsystem works, produced while scanning the codebase to answer a question (subtype: `architecture`) - capture it in the same turn; the session may end with the answer.

Pass the full reasoning, not just the conclusion. Pass `subtype` so decisions are retrievable by type. The server's novelty filter discards duplicates silently, so err on the side of calling it.

**Retrieving context JIT**: before reading files for any question about architecture, design decisions, rationale, constraints, patterns, or conventions, use a relevant `[Contexer: auto-fetched ...]` block when one is present. If that block does not answer the question, call Contexer's `get_context` with concise subject keywords; another memory, graph, or search tool is not a substitute. Multiword keyword queries fall back to the same content-and-title ranker as prompt retrieval when no literal phrase matches. Fall back to reading files only when Contexer context is missing or the question is about current code state (exact syntax, current values). Use `query` for keyword search or `entry_type` to retrieve a specific subtype. Use `limit` to override the display cap. When results are truncated, the output includes a `"showing N of M"` note so you know more exist.

## Commands

Use Conventional Commits for all commit messages: `type(scope): concise description`
(scope optional). This convention does not itself authorize committing or publishing changes.

```bash
# Install dependencies
uv sync

# Run the server (stdio transport - for manual testing)
uv run python server.py

# Smoke-test the server responds to MCP initialize
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}' | uv run python server.py

# Test the commit-time guard against currently staged changes
uv run contexer guard --explain

# Preview the assisted anchor backfill (read-only)
uv run contexer guard anchors --list
```

## Prompt-capture benchmark maintenance

All agents working on prompt capture must read the runnable
[benchmark guide](benchmarks/prompt_capture/README.md). This is separate from the
applicability/relevance benchmark: retrieval scores do not validate capture correctness.
`AGENTS.md` points here; keep this maintenance contract authoritative rather than duplicating it
in host-specific instructions.

The benchmark is implemented; its authoritative usage and maintenance guide is
[`benchmarks/prompt_capture/README.md`](benchmarks/prompt_capture/README.md). Missing fixture,
runner, or report output is a failed check, never a silent success.

Run the dedicated benchmark before and after changes affecting prompt
classification/extraction or sanitization, capture target selection, proposal/approval/history
transitions, recurrence, evidence/failure recovery, or host capture/guidance adapters. Run it
again after resolving a rebase or merge that touches those paths, and after changing its fixtures,
runner, or assertions. Verified commands:

```bash
uv run python benchmarks/prompt_capture/run.py --format json
uv run pytest tests/test_prompt_capture_benchmark.py --no-cov
```

Maintain coverage in the same change as relevant behavior: add a regression for each newly
discovered capture bug, neighboring positive/negative cases, and alternate service/environment
names where relevant. Reuse sufficient existing coverage with an explicit explanation rather
than adding duplicates. Update independently justified expectations, fixture versions, gap
registrations, and documentation when the intended contract changes; never rewrite gold labels
just to match a failing implementation. Remove fixed strict xfails; new gaps require the plan's
exact assertion-level evidence and review, never a blanket xfail or weakened safety assertion.
In the handoff/PR, report commands and outcomes, known gaps versus unexpected failures, and
coverage added or why existing cases suffice. Report unavailable checks explicitly. CI integration
and the verified run/update instructions are part of benchmark implementation acceptance.

## Architecture

A small package (`contexer/`), intentionally minimal. Each module is a cohesive concern; adding a feature means finding the module that owns it, not adding a new layer. One line per owner below; the full description of each (invariants, edge cases, why) is in [`docs/architecture.md`](docs/architecture.md#modules), and the module's own docstring. Read the relevant entry there before changing a module.

- **`store.py`** - local decision store: read/write, novelty matching (`_find_match`), capture-quality gate, retrieval rendering, approval/anchoring, sidecar paths; the public facade other modules call.
- **`revisions.py`** - pure decision-revision lifecycle, derived metadata, HEAD resolution (leaf).
- **`reconciliation.py`** - pure team-reconciliation state transitions (leaf; `store` imports it, never the reverse).
- **`review.py`** - pure proposal-slot policy: trust order, slot claims, proposal construction.
- **`retrieval.py`** - pure lexical retrieval: tokenization, topics, BM25, artifact extraction, the default-off Contract 06 classifier.
- **`permissions.py`** - owner-only directory creation and descriptor-based tightening.
- **`working_set.py`** - bounded per-session delivered-guidance ledger.
- **`prompt_capture.py`** - pure classifiers for factual decisions in prompts; never writes storage.
- **`evidence.py`** - fail-soft evidence events from hooks and agent conclusions; observations only, never active decisions.
- **`spool.py`** - atomic per-repo evidence spool: retention, holds, quarantine, durable receipts.
- **`candidates.py`** - pure deterministic aggregation/scoring of evidence into review candidates.
- **`reconcile.py`** - coordinator draining the spool into review items under per-pass capacity limits.
- **`policy.py`** - pure policy selection/evaluation shared by Guard and policy surfaces (leaf).
- **`policy_api.py`** - read-only facade formatting advisory policy reports for MCP/CLI.
- **`guard_engine.py`** - commit-time guard engine and `decisions_for_files`; re-exported lazily by `store`.
- **`anchors.py`** - anchor verification: rename correction, retirement proposals on loss.
- **`lifecycle.py`** - retirement/restoration and the lifecycle and reconsideration review lanes.
- **`conflicts.py`** - open-conflict rendering, explicit incompatible current version-format pairs, and advisory resolution memos.
- **`review_impact.py`** - read-only approval-impact preview shared by MCP, CLI and console.
- **`decision_impact.py`** - opt-in, diagnostics-only decision-impact pilot sidecar.
- **`console_api.py`** - read projections for the `contexer ui` console (`ui/api.py` -> `console_api` -> `store`).
- **`scope_audit.py`** - read-only audit for decisions saved to the wrong repo's store.
- **`repo_key.py`** - canonical remote-key normalization and evidence repository-identity checks.
- **`bootstrap.py`** / **`repository_discovery.py`** - evidence-backed bootstrap; Markdown nomination as evidence, never authority.
- **`miner.py`** - deterministic, stdlib-only convention mining (legacy scan verification only).
- **`updates.py`** - release-notice fact and policy; never network I/O on the hook path. Declaring a version floor is a manual maintainer act (details in the reference).
- **`export.py`** - read-only deterministic Markdown/ADR projections; redacted by default.
- **`redact.py`** - deterministic egress-only secret redaction; see "Secrets never egress" below.
- **`remote.py`** / **`team_context.py`** / **`share_status.py`** - Teams protocol adapter, team-context cache, typed share outcomes.
- **`share.py`** / **`share_policy.py`** - explicit share to Teams (never automatic on capture); local policy state for remembered automatic proposals (no network, no store lock).
- **`auth.py`** / **`config.py`** - Teams OAuth login and token storage; `~/.contexer/config.toml` profile loader (a leaf).
- **`sidecars.py`** - the one declaration of every file kept in the store directory, and its lifetime; see Storage below.
- **`decision_observability.py`** - fail-soft local diagnostics for proposal operations; no network exporter, closed vocabulary.
- **`hook_host.py`** - stdlib-only host boundary for Claude hook registrations (Cursor envelope guard); see the adapters guide.
- **`memory_sync.py`** - imports Claude Code memory-tool facts into the store.
- **`server.py`** - thin FastMCP tool surface; storage and evaluation stay in owner modules.
- **`cli.py`** - `contexer` console script; subcommands are rows in the `COMMANDS` table.
- **`claude_mod/`** - the Claude Code mod (TypeScript): in-session review band and pane over `contexer review --json`; registered by path, so a package upgrade upgrades it (details in the reference).
- **`adapters/`** - one module per host (Claude, Cursor, Codex, Gemini); see [`contexer/adapters/CLAUDE.md`](contexer/adapters/CLAUDE.md), auto-loaded when you work there.
- **Benchmarks** - Contract 06/07/08 offline boundaries are in [`benchmarks/CLAUDE.md`](benchmarks/CLAUDE.md).
- **`server.py`** (repo root) is a back-compat shim. **No `requirements.txt`**: `pyproject.toml` is the single dependency spec; `mcp` stays below 2.0.

## Module boundaries

`store.py` is decomposed into single-purpose owner modules (`revisions.py`, `reconciliation.py`, `review.py`, `retrieval.py`, `evidence.py`, `spool.py`, `candidates.py`, `reconcile.py`, `policy.py`, `policy_api.py`, `lifecycle.py`, `review_impact.py`, and others), enforced by dynamic caller discovery plus the three rules in `tests/test_module_boundaries.py`, so extraction cannot erode silently as new modules are added:

- **Rule 1 - production code imports the owner, always.** `store.py` keeps a small, frozen back-compat facade (a lazy `__getattr__`) for names that were public on `store` before an extraction moved them; nothing new is added to it, and a name with no consumer left is dropped. Tests should address the owner module too, not the facade, so a test doesn't end up merely pinning the shim.
- **Rule 2 - a `store` name read by a second module is interface, so it becomes public** (unprefixed, no leading underscore) rather than staying a "private" name two modules already depend on. This applies to `store` specifically, since it's the module everything else depends on; it does not extend to other modules' shared private helpers.
- **Rule 3 - no module copies another module's names onto itself.** A caller that needs a name from a leaf module reaches it qualified on the owner (`revisions.current_content(entry)`, `review.build_proposal(...)`), never through a second local alias. A stale alias fails silently (an `AttributeError` inside a fail-soft wrapper just stops working, with nothing raised loudly), which is why this is enforced by a test rather than left to code review.

A module reaching into `store` must always import the module object (`from contexer import store`), never `from contexer.store import X` - a value a test monkeypatches on `contexer.store` is only visible at the call site through the module-object form.

## Storage

Context lives at `~/.contexer/<repo_slug>.json` (one file per repo, plus `_global.json`). Writes are atomic (temp file + `os.replace`, mode `0o600`). A corrupt file reads as empty on RENDER paths only; a write path that could destroy queued data must use a reader that raises on corruption. Every store-owned sidecar path goes through `store.store_dir()` / `store.sidecar_path()` / `sidecars.glob_for()` / `store.ensure_store_dir()` (enforced by `tests/test_store_dir_seam.py`). Linked worktrees share the main worktree's store.

A decision owns immutable **revisions** inline (`revisions[]` + `current_revision_id`); storage keeps every revision, replay shows only the current approved one. Repo targeting, sidecar exemptions, and the full entry/revision schema: [`docs/architecture.md`](docs/architecture.md#storage).

## Session behaviour (hooks)

Hosts run Contexer hooks at `SessionStart` (inject approved context), `PostToolUse` Write/Edit (silent edit tracking), `UserPromptSubmit` (directive capture, rationale retrieval, reminders, team poll), `PreCompact`, and `SessionEnd`. Two standing invariants: there is **no `Stop` hook** (tried and removed), and **bookkeeping writes are best-effort**, so failing to write an optional flag never prevents context injection. Claude SessionStart additionalContext is capped at 8,000 bytes including conservative JSON escaping below the observed inline cutoff: complete approved constraints first, then global rules and useful context, followed by one get_context pointer for omitted blocks. Dropped blocks receive no delivery credit. Other hosts retain their own delivery behavior. Hook-by-hook behaviour: [`docs/architecture.md`](docs/architecture.md#session-behaviour-hooks); host wiring: [`contexer/adapters/CLAUDE.md`](contexer/adapters/CLAUDE.md).

## Commit-time guard (`contexer guard`)

An opt-in git `pre-commit` hook (`contexer guard --install-hook`, never wired by `contexer install`). Tier 1 is advisory and always exits 0; Tier 2 blocks only for decisions explicitly armed via `arm_guard`, with approval re-checked at run time. **Invariant: the run path never blocks a commit on its own failure.** Candidate anchors never become guard input without an explicit human signature, and there is no bulk approval path. Full detail: [`docs/architecture.md`](docs/architecture.md#commit-time-guard-contexer-guard).

## Design constraints

- **Silent operation is essential.** Tools must not produce noise - `update_context` silently discards filtered (non-novel) content without logging. This governs *capture and filtering* noise specifically; it does not forbid every user-facing message. Three deliberate exceptions reach the DEVELOPER: a short structured note naming what the rationale hook fetched (so retrieval is observable, not spooky), the new-release notice, and the Claude Code mod's review band (`contexer/claude_mod/`): one line above the prompt, plus `/contexer-review` as the prompt box's dim suggestion (Tab takes it), only while decisions it can settle wait on the developer, both stopped for the session by Later, never shown to the model. A third deliberate non-silent behaviour reaches the MODEL rather than the developer: the capture-quality bounce, a corrective ack returned through the MCP tool result telling the calling model to restate a narrative-shaped capture in the same turn - silently discarding it would lose the decision instead of fixing its shape.

  The new-release notice is announced at most once per release and never within 24 hours of another routine notice (`updates.MIN_NOTICE_INTERVAL`); see [docs/architecture.md](docs/architecture.md#update-notice-cadence) for the full cadence rules.
- **Secrets never egress; capture stays faithful.** Redaction happens only where a decision leaves the machine (the wire call and the share preview), never on local capture - the local store is meant to be a verbatim record, and silently rewriting captured content would corrupt that. Redaction is default-on, deterministic, idempotent, and fail-soft toward staying on. Do not add an outbound path that bypasses the existing redaction chokepoint.
- **No abstraction beyond what exists.** The module structure (`server.py` / `store.py` / `cli.py` / `adapters/`) is intentional. Do not add classes, config files, or layers unless the spec changes. Adding support for a new AI-assistant target is the one case that genuinely fits in a single new file, because the adapter registry is the only seam it needs - it is not a rule that every change must land in one file. A behaviour that is inherently cross-cutting touches every module that owns a piece of it; the test is ownership, not file count. Adding a *layer* - an interface with one implementation, a config file for a value that never changes, a factory for one product - is what this constraint forbids.
- **`update_context` is called by Claude Code, not the developer.** Claude Code nominates content; the server filters. Novelty matching is deterministic: `_find_match` returns the first candidate with token overlap strictly greater than 0.7, not an LLM judgment. Writers select decision candidates (not tasks) and handle storable-content checks and match outcomes; an active match records recurrence rather than creating a duplicate, while inactive matches follow the reconsideration path.
- **Git hooks and CLI commits are out of scope for decision capture.** The commit-time guard reads and checks already-stored, already-approved decisions; it never creates one. Decision nomination comes from MCP calls or from host-hook evidence reconciled into human review, never from commit enforcement.
- **Never claim complete decision capture.** Coverage is best-effort and host-specific. The guarantee is narrower: acknowledged evidence is held, summarized onto a decision, or counted as a gap, and inferred or inactive evidence-reconciliation candidates cannot become trusted, anchored, or armed without the required human action. Bootstrap is an explicit product exception for retrieval only: grounded observations/inferences may be replayed as labeled non-authoritative context, never as approved policy. Confidence, repeated scans, and silence cannot grant human approval.
- **Thin semantic MCP tools, rich deterministic internals.** The model-facing MCP surface stays small and centered on decisions, review, retrieval, and sharing - not internal storage/sync mechanics. A new tool proposal must answer two questions before it's added: why must the model choose this action directly (versus an existing tool's parameter, or something adapters/local Python already handle), and what invariant stays enforced in deterministic code regardless of what the model passes? Prefer higher-level arguments that reflect user intent over internal storage operations, ids, or enum spellings. See contexer-ai/contexer-teams#178 for the matching decision on the Teams client side, so server/client surface boundaries evolve together.
