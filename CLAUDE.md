# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dependencies
uv sync

# Run the server (stdio transport - for manual testing)
uv run python server.py

# Smoke-test the server responds to MCP initialize
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}' | uv run python server.py

# Test the commit-time guard against currently staged changes (see Commit-time guard below)
uv run contexer guard --explain

# Preview the assisted anchor backfill (read-only; see Anchor accrual below)
uv run contexer guard anchors --list
```

## Architecture

A small package (`contexer/`), intentionally minimal:

- **`contexer/revisions.py`** — pure decision-revision lifecycle and derived metadata: normalization, title derivation, confidence scoring, immutable revision creation, HEAD resolution, cache synchronization, and append transitions. Full detail: `docs/internal/architecture/revisions.md` (local only).
  It mutates an already-loaded decision only; persistence, migration, approval, and locking remain in `store.py`.
  Callers reach these functions on the owner (`revisions.current_content(entry)`), never through a private alias on `store`: the six aliases `store.py` carried at extraction time (`_derive_title`, `_new_revision`, `_current_revision`, `_current_content`, `_sync_decision_cache`, `_append_revision`) plus three more (`_normalize_title`, `_normalize_content`, `_compute_confidence`) are gone, per the third module-boundary rule below.
- **`contexer/reconciliation.py`** — pure team-reconciliation state transitions: proposal-slot protection, remote-head deduplication, convergence clearing, and approved/dismissed outcome receipts. It mutates an already-loaded decision only; persistence and public compatibility entry points remain in `store.py`, and the dependency stays one-way (`store.py` imports this leaf, never the reverse). Full detail: `docs/internal/architecture/reconciliation.md` (local only).
- **`contexer/review.py`** — pure proposal-slot policy: the trust order for the single unreviewed `proposed_revision` slot (`PROPOSAL_TRUST`/`outranks_proposal`), the displace-and-archive claim (`claim_proposal_slot`), the model-facing refusal ack, and proposal construction (`build_proposal`). Same one-way rule as the two leaves above. Full detail: `docs/internal/architecture/review.md` (local only).
- **`contexer/retrieval.py`** — pure lexical retrieval primitives: `index_tokens` (the one index/query tokenization, distinct from the novelty filter's `store._tokenize`), `derive_topics` + `_TOPIC_ALIASES`, `bm25_rank` + BM25 tuning, and `extract_artifacts` + the artifact regexes. Same one-way rule. Full detail: `docs/internal/architecture/retrieval.md` (local only).
- **`contexer/server.py`** — MCP server entry point. Defines tools (`capture_user_constraint`, `update_context`, `approve_decision`, `resolve_conflict`, `review_pending`, `get_context`, `get_context_for_prompt`, `bootstrap_context`, `list_shareable`, `share_decision`, `update_global_context`, `get_global_context`) using `FastMCP`. Full detail: `docs/internal/architecture/server.md` (local only).
- **`contexer/store.py`** — All read/write and filtering logic. `_passes_filter` is the core gate: content is stored only if it is novel (token-overlap check - >70% overlap with existing decisions = duplicate). Full detail: `docs/internal/architecture/store.md` (local only).
- **`contexer/guard_engine.py`** — the commit-time guard engine (staged-file plumbing, Tier-1 advisory pairing, Tier-2 armed blocking rules), extracted out of `store.py`. `store.py` stays the public facade: it re-exports the five public entrypoints (`guard_staged`, `guard_candidates`, `arm_guard`, `disarm_guard`, `dismiss_guard`) *lazily*, via a PEP 562 module `__getattr__`, not an eager import. Full detail: `docs/internal/architecture/guard_engine.md` (local only).
- **`contexer/anchors.py`** — anchor lifecycle verification (Task 2 of #174), extracted out of `store.py` (user directive: don't crowd store.py with a second verification family) so `store.py` carries only the session-start call site (`anchors.verify_anchors`, wired in immediately after `verify_scan_conventions`, same re-read-on-change convention: a rename correction or a fresh retirement proposal makes the session re-`load` so it renders the verified state). Full detail: `docs/internal/architecture/anchors.md` (local only).
- **`contexer/conflicts.py`** — conflict rendering + resolution memos (issue #193), extracted out of store.py on the same directive that produced `anchors.py` (one module per cohesive concern; `store.py` stays a thin call-site facade). Owns `_CONFLICT_GUIDE`, `_conflict_pair_key`, `_has_open_conflict`, `_conflict_view`, and `record_conflict_memo`. Full detail: `docs/internal/architecture/conflicts.md` (local only).
- **`contexer/console_api.py`** — local-console read projections, every shape the `contexer ui` console renders for one repo, one store, or the global rules (`list_stores`, `store_summary`, `list_decisions`, `list_global_rules`, `team_snapshot`, `overlap_report`, plus the `_console_*` row/diff projections). Extracted out of store.py on the same directive that produced `anchors.py` and `conflicts.py`, and **the boundary was measured rather than chosen**. Full detail: `docs/internal/architecture/console_api.md` (local only).
- **`contexer/scope_audit.py`** — cross-store scope audit: finds decisions saved into the WRONG repo's store (see "Wrong-store writes" under Storage). The fingerprint needs no heuristic - **one session id appearing in more than one repo store**. Full detail: `docs/internal/architecture/scope_audit.md` (local only).
- **`contexer/miner.py`** — deterministic convention mining, stdlib only (`ast`/`tomllib`/`re`/`configparser`/`subprocess`). `mine_conventions(repo_path) -> list[dict]` measures conventions with whole-repo evidence embedded in the sentence itself ("Functions use snake_case naming (98% of 412 functions across 37 files)"), covering config-encoded rules, AST source statistics, test conventions, and commit-message style. Full detail: `docs/internal/architecture/miner.md` (local only).
- **`contexer/redact.py`** — deterministic secret redaction, stdlib only (`re`). `scrub(text) -> (redacted_text, count)` (plus `scrub_text` / `count_secrets` convenience wrappers) replaces each detected secret with `[REDACTED:<kind>]`. Full detail: `docs/internal/architecture/redact.md` (local only).
- **`contexer/adapters/`** — one module per AI-assistant target. `base.py` holds shared config-file helpers (`_load`/`_save`/marker checks). Full detail: `docs/internal/architecture/adapters.md` (local only).
- **`contexer/memory_sync.py`** — imports Claude Code's memory-tool facts (`~/.claude/projects/<slug>/memory/*.md`) into the store, so decisions a session persisted to the file-based memory tool (instead of calling `update_context`) still reach Contexer. This is a *coexistence* path, not a competitor for capture: rather than fight an authority contest hook-injected reminders always lose against another tool's system-prompt memory workflow, Contexer becomes a deterministic sync target. Full detail: `docs/internal/architecture/memory_sync.md` (local only).
- **`contexer/share_status.py`** — what a share or a reconciliation DID, as data, plus the one place it is put into words. A pure leaf: stdlib only, imports nothing of ours. Full detail (including the reconciliation-status refactor writeup): `docs/internal/architecture/share_status.md` (local only).
- **`contexer/cli.py`** — the `contexer` console script: bare invocation runs the MCP server; subcommands `install [--target claude|cursor|codex|gemini|all]` / `uninstall [--purge]` / `reinstall` / `status` / `scope-audit` (read-only wrong-store report - see `scope_audit.py`) / `version` / `help` manage tool configs. `install` auto-detects present tools (`~/.claude` → Claude Code, `~/.cursor` → Cursor, `~/.codex` → Codex, `~/.gemini` → Gemini CLI) and falls back to Claude Code when none is detected; `--target` overrides. Full detail: `docs/internal/architecture/cli.md` (local only).
- **`server.py`** (repo root) - back-compat shim importing `contexer.server`; keeps `uv run python server.py` working.
- **No `requirements.txt`.** `pyproject.toml` is the single dependency spec (managed by `uv`). A "kept for reference" `requirements.txt` used to sit here carrying its own copy of the `mcp>=1.9.4,<2` bound; nothing consumed it (not CI, not `scripts/`, not `CONTRIBUTING.md`), so it could only ever drift out of agreement with the real bound - where the upper bound is load-bearing, since mcp 2.0.0 removed `mcp.server.fastmcp` and an unbounded resolve installs a server that cannot start. Do not re-add one; `uv sync` is the install path.

## Module boundaries

Four modules were extracted out of `store.py` to one stated rule, and they landed at four different depths.
Two of the four were leaks rather than seams.
So the rule was restated as three clauses.
The clauses are enforced in `tests/test_module_boundaries.py` rather than left to review, because each one describes a drift that NOTHING fails on: a facade keeps answering correctly while the seam behind it erodes, a private name quietly acquires a second reader, and an alias makes a leaf's compiled regex reachable under a store name.
A rule that is only written down demonstrably does not hold here; that is what the four depths are evidence of.
The test also pins one PRE-EXISTING invariant these rules depend on, so it is four checks rather than three: a module reaching the store must import the module OBJECT, never `from contexer.store import X`, or a value a test patches on `contexer.store` is never seen at the call site.

Three enforced rules (`tests/test_module_boundaries.py`): **Rule 1** — production code always imports the owner, never the `store` facade. **Rule 2** — a `store` name read by a second module becomes public. **Rule 3** — no module copies another module's names onto itself (reach them qualified on the owner instead).

Full rules, the pre-rule measurement, and worked examples: `docs/internal/architecture/module-boundaries.md` (local only).

## Storage

Context is stored at `~/.contexer/<repo_slug>.json` - one file per repo, plus `_global.json` for cross-repo decisions. The slug is the repo path with non-alphanumeric characters replaced by underscores.

Linked git worktrees share the main worktree's store: `repo_slug` canonicalizes the STORE KEY ONLY via `_canonical_store_key` (a `.git` *file* whose `gitdir:` contains `/worktrees/` → one `git rev-parse --path-format=absolute --show-toplevel --git-common-dir` call, success-only cached, entirely fail-soft), so every slug-keyed artifact (store, lock, sidecars, flags, team cache) collapses while staleness/insight/miner still see the physical worktree path.

Decisions use an embedded Git-like revision model (each entry owns immutable `revisions[]` with a `current_revision_id` pointer; storage keeps history, replay exposes only current). Repo resolution precedence: explicit `repo_path` argument > the MCP server process's bound `_SESSION_REPO` > the shared `~/.contexer/.current_repo` pointer, each guarded against selecting a config-dir store. Wrong-store writes (a decision landing in the wrong repo's store) are diagnosable via `_resolve_repo_verbose`/`scope_audit.py` but not yet prevented.

Full detail: `docs/internal/architecture/storage.md` (local only).

## MCP integration

`contexer install` registers the server in `~/.claude.json` under `mcpServers`, pointing at the installed console script:

```json
{
  "contexer": {
    "type": "stdio",
    "command": "/Users/<you>/.local/bin/contexer"
  }
}
```

(A from-source dev install via `scripts/install.sh` wires `uv run --directory <clone> python server.py` instead.)

For Cursor, the equivalents are `~/.cursor/mcp.json` (MCP server registration) and `~/.cursor/hooks.json` (hook wiring), managed by `contexer/adapters/cursor.py`. The MCP server entry in `mcp.json` uses the same `contexer` command; hooks use Cursor's `sessionStart` and `beforeSubmitPrompt` event names.

For Codex, the MCP server is registered in `~/.codex/config.toml` (TOML) as `[mcp_servers.contexer]` with `command = "<contexer-bin>"`, and hooks live in `~/.codex/hooks.json` (JSON, same schema and event names as Claude's `settings.json` `hooks` block), both managed by `contexer/adapters/codex.py`. The `config.toml` edit is surgical text manipulation (validated with `tomllib` before writing) so only the contexer stanza is touched - `codex.py` adds no TOML-writer dependency.

For Gemini CLI, both the MCP server and managed hooks live in `~/.gemini/settings.json`, managed by `contexer/adapters/gemini.py`. Installation preserves unrelated keys, servers, and hook groups.

## Session behaviour (hooks)

`~/.claude/settings.json` wires up hooks globally (applies to every repo). Full detail for every hook event below: `docs/internal/architecture/hooks.md` (local only).

- **`SessionStart`** — first runs `claude.sync_memory` to import any memory-tool facts, then injects all *approved* conventions, patterns, and constraints as project rules (constraint-subtype rules render in full, convention/pattern rules render title-only to stay within the host's `additionalContext` size limit), a count pointer for deferred architecture decisions, and a count-only pointer for decisions awaiting review; injects a bootstrap offer if no context exists.
- **`PostToolUse` (Write|Edit, silent)** — fires after every `Write` or `Edit` tool call, running `claude.post_write` (replacing the old shell-only `touch .pending_capture`). Two jobs in one hook: it records the edited file into a **per-repo** sidecar via `store.record_edited_file` (`~/.contexer/.edited_<slug>.json`, a list of `{path, mtime}` records canonicalized through `guard_engine._guard_relpath`, re-stamped in place on a repeat edit, capped at `_EDITED_FILES_CAP`=50 evicting the oldest by mtime) - the **flow** half of anchor accrual (see Commit-time guard below); and it touches `~/.contexer/.pending_capture` exactly as before.
- **`.pending_review` (deterministic mid-session nudge).** Unlike `.pending_capture` (written by a hook), the **per-repo** `~/.contexer/.pending_review_<slug>` flag is written by the **store**: `store.update_decision` touches it at the two spots that create a state awaiting the developer - a new `pending_approval` entry (gated on status) and a freshly-attached `proposed_revision` - after `save`, inside the store lock (`store._touch_pending_review(repo_path)`).
- **No `Stop` hook.** An end-of-turn `Stop` hook that prompted the model (via `additionalContext`) to capture/review decisions was evaluated and removed: it added latency and tokens and depended on model behavior, while delivering nothing the deterministic `PostToolUse` flag + next-prompt anchor reminder doesn't already deliver at a non-interrupting moment. `claude.install`/`codex.install` actively strip a previously-installed Contexer `Stop` hook (matched on `.pending_capture`) while leaving any foreign `Stop` hook intact; the `Stop` marker stays in the uninstall tables for this cleanup.
- **`UserPromptSubmit` (anchor, command, every prompt)** — writes the git root to `~/.contexer/.current_repo`, but only when actually inside a git work tree (no `|| pwd` fallback - that could poison the pointer with a non-repo dir). Also checks for `~/.contexer/.pending_capture`: if present, deletes it and injects `additionalContext` reminding Claude to call `update_context` for decisions from the previous turn.
- **Bookkeeping writes are best-effort everywhere (#152).** `~/.contexer/.current_repo`, `.pending_capture`, `.pending_review`, and `.resume_mining` are *bookkeeping*, not context: the pointer is only the last resort in `resolve_repo`, and the flags merely schedule a reminder. A sandboxed host can leave the workspace writable while `~/.contexer` is not (Codex's managed sandbox), and an unguarded write there used to raise `PermissionError` and abort `SessionStart` - Contexer injected nothing at all over a file it never needed in order to *read* context.
- **`UserPromptSubmit` (bootstrap, command, once)** — calls `store.get_bootstrap_context_prompt` on the first prompt; injects the bootstrap offer if no context exists (fallback for when SessionStart is skipped).
- **`UserPromptSubmit` (constraint, command, every prompt)** — calls `claude.capture_constraint` with the prompt text. Detects prescriptive directives (`always X`, `never Y`, `from now on Z`) and stores them as `decision` entries automatically - no Claude involvement needed.
- **`UserPromptSubmit` (rationale, command, every prompt)** — calls `claude.rationale` with the prompt text; auto-injects matching decisions when the prompt contains rationale keywords (why, reason, rationale, decided, etc.). Silent no-op for all other prompts.
- **`UserPromptSubmit` (team poll, command, every prompt)** — calls `claude.team_poll(repo, raw, consumer)` (C7 delta-poll; `consumer` defaults to `"claude"`, Codex passes `"codex"` - Codex reuses this exact function). **Non-blocking by design: the prompt path never performs network I/O.** Mirrors the SessionStart deferral: a newly-approved `architecture`-typed row is never dumped into the "just approved" banner as full content - it's rolled into a single count-only line pointing at `get_context(entry_type="architecture")`, while non-architecture rows still render in full immediately.
- **`PreCompact`** — runs `claude.sync_memory` to flush memory-tool facts before the context window collapses, then injects a systemMessage reminding Claude to call `update_context` for any unsaved decisions.
- **No `PostCompact` hook.** After compaction Claude Code fires `SessionStart` again with `source="compact"`, and that event silently re-injects the full context via `additionalContext` (the normal `session_start_payload` path) - so post-compaction reload is already owned, seamlessly, exactly like a fresh session start. A `PostCompact` hook was tried and removed: `PostCompact` supports no `additionalContext` and its `systemMessage` is user-facing only (the model never sees it), so it reloaded nothing and merely dumped the entire stored context into the transcript as visible noise on every `/compact`.
- **`SessionEnd`** — runs `claude.sync_memory` to flush memory-tool facts on clean exit. The earlier decision to reject `SessionEnd` hooks was about *capture that needs the model to act* (a `SessionEnd` hook can't make Claude call a tool); this is a deterministic file import that needs no model, so it is valid here.
- **Legacy repo-settings cleanup (upgrade hygiene).** The pre-CLI from-source installer (June 2026) wrote hooks into the *repo's* `.claude/settings.json` - including an `mcp_tool` hook calling the since-removed `capture_context` tool ("Unknown tool: capture_context" on every prompt) and a dead-clone `uv run --directory` SessionStart hook (a second, contradictory "no context stored yet" startup message next to the real global one).

**During a session**, call `update_context` whenever you make a significant decision, establish a pattern, or document a constraint. This is mandatory - not optional. After you write or edit files, the next user prompt will inject a reminder via `additionalContext`, but you should call it proactively within the same turn, not wait for the next prompt.

Call `update_context` for any of these:
- A technology, library, or approach chosen over alternatives (subtype: `architecture`)
- A file structure, naming convention, or code organization pattern established (subtype: `pattern`)
- A rule stated by the user or inferred from their preferences: "always X", "never Y" (subtype: `constraint` or `convention`)
- A build, deploy, or tooling convention established (subtype: `convention`)
- Any decision that would surprise a future Claude session if it wasn't stored
- A synthesized understanding of how a subsystem works, produced while scanning the codebase to answer a question (subtype: `architecture`) - capture it in the same turn; the session may end with the answer.

Pass the full reasoning, not just the conclusion. Pass `subtype` so decisions are retrievable by type. The server's novelty filter discards duplicates silently, so err on the side of calling it.

**Retrieving context JIT**: call `get_context` **before reading files** for any question about architecture, design decisions, rationale, constraints, patterns, or conventions. Fall back to reading files only when context is missing or the question is about current code state (exact syntax, current values). Use `query` for keyword search or `entry_type` to retrieve a specific subtype: `get_context(entry_type="constraint")` returns only constraints (up to 25). Use `limit` to override the display cap. When results are truncated, the output includes a `"showing N of M"` note so you know more exist.

## Commit-time guard (`contexer guard`)

Full detail for every paragraph below: `docs/internal/architecture/commit-time-guard.md` (local only).

A git `pre-commit` hook - install with `contexer guard --install-hook` (never wired automatically by `contexer install`) - that checks staged files against stored decisions when you commit. Two tiers, both implemented in `contexer/guard_engine.py` and dispatched from `cli.py`'s `guard()`/`_guard_*` family: **Tier 1 (advisory, non-blocking, always exit 0)** pairs a staged file against an `approved` decision whose provenance is trusted (`_guard_trusted`; `_GUARD_TRUSTED_SOURCES = {human, scan, bootstrap, plan}` - never `ai`/`memory`, so an unreviewed guess never nags at commit time). **Tier 2 (blocking, exit 1)** fires only for a decision explicitly armed via `arm_guard` with a machine-checkable regex or secret check.

**Approval-time anchoring.** `approve_decision(repo_path, entry_id, action, content="", *, source_files=None)` (in `store.py` - approval machinery, not guard engine) takes a keyword-only `source_files`: when non-empty and the resolved action is `approve` or `edit`, it anchors the entry via `_anchor_sources` (`source_files` + `anchor_commit` = current HEAD) once the approval actually applies - a Suggested Update's own stashed `source_files` anchors through `_promote_proposal` instead, so `approve_decision` skips the redundant re-anchor call in that case.

**Anchor accrual (issue #175).** An unanchored decision is not invisible to Tier 1 - `_guard_pairs` pairs on `source_files` **or** on a path/module artifact mined from the decision's own content - but it is invisible to **staleness** (`_staleness_note` needs `source_files` + `anchor_commit`), and it can only ever pair on whatever files it happens to name in its own prose. Anchoring after the fact is what closes both gaps: it makes the pairing explicit rather than incidental (reason `source_files match` instead of `path artifact ...`, and files the text never mentions become pairable) and it turns on staleness tracking for that decision.

- **Stock: assisted backfill.** `guard_engine.anchor_candidates_for_backfill(repo_path)` is read-only: for every decision that is BOTH `_guard_trusted` AND currently unanchored, it mines the decision's OWN content for path/module-shaped artifacts (the same `_guard_content_artifacts` extraction Tier-1 pairing uses), maps any dotted module onto its file spelling(s), and keeps only candidates that actually `exist` in the working tree right now (no rename detection in v1 - a renamed file yields no candidate for that artifact rather than guessing).

- **Flow: capture-time candidates.** `store.update_decision` no longer requires the model to name `source_files` for a new capture to eventually anchor. When a captured entry lands `pending_approval` without `source_files` and isn't scan/bootstrap/memory-sourced, the repo's recently-edited files (`store._read_edited_files`, fed by `record_edited_file`) are stashed on the entry as `anchor_candidates` - never read by the guard's pairing engine, so a candidate can never pair as a Tier-1 advisory before a human blesses it on approval.

**The informed-signature rule, stated once:** no anchor ever becomes guard input without a human having seen the file(s) it names at the moment they signed off - a backfill candidate is shown and ratified per-decision in the CLI loop before `apply_backfill_anchors` writes it, and a capture-time candidate is rendered as `would anchor:` in the same review surface (`format_pending_review`) the developer is already reading before they call `approve_decision`. Neither path ever anchors a `pending_approval` decision on its own; a candidate sits inert until that human signature arrives.

**Anchor truncation is recorded, not silent, and the field pair has ONE writer.** `store.set_source_files(entry, files, total=None)` is the only writer of `source_files` + `source_files_total`, and `store.clear_source_files(entry)` the only remover; `total` is the number of paths the list was DERIVED FROM, not a "did I truncate" flag, and is stored only when it exceeds the list, so the pair can never claim a truncation that did not happen.

**`source_files` on the wire (issue #174 Task 5).** `remote._WIRE_SOURCE_FILES` gates whether anchors egress at all. It shipped `False` because the contexer-teams push schema is server-controlled and an unknown field poisons the outbox with permanent validation failures (this happened for real with `source="plan"`: -32602 on every retry, 192 attempts); it is now `True`, contexer-teams having accepted the field on both `push_decision` and `push_decisions` (server commit `e1a2189`, snake_case on the wire - note the same schema spells `decisionId` in camelCase, so the field name is not guessable).

## Design constraints

- **Silent operation is essential.** Tools must not produce noise - `update_context` silently discards filtered (non-novel) content without logging. This governs *capture and filtering* noise specifically; it does not forbid every user-facing message. Two deliberate, developer-ratified exceptions: the rationale hook's recall notice (above) - a short, structured line naming what Contexer fetched, so retrieval is observable rather than spooky - and `capture_lint`'s bounce message, a model-facing corrective ack (like `constraint_ack`) that tells the calling model to restate a narrative-shaped capture in the same turn; silently discarding those would lose the decision instead of fixing its shape.
- **Secrets never egress (capture stays faithful).** `redact.py` scrubs only where a decision leaves the machine - `remote._wire_args` (the wire guarantee) and `store._share_projection` (preview/outbox parity), surfaced by the `(N secrets redacted before sending)` preview banner. Capture is deliberately **not** redacted: the local store is a verbatim record, and silently rewriting captured decisions would corrupt the product's core value (scrubbing `0600` local files also buys little against a reader who already has `~/.aws`/`.env`). Redaction is default-on (`config.redact_secrets`), deterministic, idempotent, and fail-soft (a config error keeps it ON). Do not add an outbound path that bypasses `_wire_args` without extending redaction to it.
- **No abstraction beyond what exists.** The module structure (`server.py` / `store.py` / `cli.py` / `adapters/`) is intentional. Do not add classes, config files, or layers unless the spec changes. **Adding support for a NEW AI-ASSISTANT TARGET = one new module in `adapters/`; no other files change** - that clause is about tool targets specifically, and is the one case where a whole feature genuinely fits in a single new file because `adapters/__init__.py`'s registry is the only seam it needs. It is **not** a rule that every change must land in one file. A behaviour that is inherently cross-cutting touches every module that owns a piece of it, and confining it to one would mean either reaching into another module's files or duplicating its logic - both worse than the spread. Two shipped examples: the account-switch cache invalidation of #232 has its trigger in `auth.py`, its pull caches in `team_context.py` and its marker log in `share.py`, because each module owns the file it deletes; and `share --global` (#239) needs a selector in `store.py` (which owns `_global.json`) and a push path in `share.py` (which owns the wire and outbox). The test that separates the two cases is ownership, not file count: if each touched module is doing something only it can do, the change is correctly shaped. Adding a *layer* - an interface with one implementation, a config file for a constant, a factory for one product - is what this constraint actually forbids, at any file count.
- **`update_context` is called by Claude Code, not the developer.** Claude Code nominates content; the server filters. The filtering criterion is novelty - >70% token overlap with any existing decision is rejected as a duplicate, not an LLM call.
- **Git hooks and CLI commits are out of scope for capture.** The MCP tool call path is the only capture mechanism - the commit-time guard's git hook (see above) reads and checks already-stored, already-approved decisions; it never captures a new one.
