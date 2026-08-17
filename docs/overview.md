# Contexer — architecture overview

A snapshot of what Contexer does and how it is built, as of **v0.26.1**.

This is the engineering-facing companion to [how it works](how-it-works.md): that document explains
Contexer to someone using it, this one explains it to someone changing it. For the authoritative,
file-by-file contract — including the reasoning behind individual thresholds and rejected
alternatives — see `CLAUDE.md` at the repo root.

**One line:** an MCP server plus a hook layer that captures engineering decisions as they are made,
stores them locally per repo, and re-injects only the relevant ones into future AI sessions — so
context survives across sessions, compactions, and tools.

## The core loop

```
AI nominates  →  server filters (novelty)  →  local store  →  router re-injects on demand
```

The filter is **not** an LLM call: >70% token overlap with an existing decision is a duplicate,
silently discarded and counted as a recurrence. Storage caps at 500 entries per repo; display caps
separately (10 for overview calls, 25 for filtered queries).

## Storage — git-like, revisions preserved

`~/.contexer/<repo_slug>.json`, one file per repo, plus `_global.json` for cross-repo rules. Writes
are atomic (unique temp file + `os.replace`, mode `0600`) so readers never see a torn file; corrupt
files read as empty rather than crashing.

A **decision** is the logical engineering choice; a **revision** is one immutable version of it.
Storage preserves every revision forever; replay exposes only the current approved one — that
separation is what lets history accumulate without ever growing the prompt or leaking stale values
into a session. Revisions are embedded inline (`revisions[]` plus a `current_revision_id` pointer —
precedence is the pointer, **never** timestamps), with `content`/`confidence` kept as a synced
HEAD-cache so the many read sites stay O(1). Legacy entries migrate transparently and idempotently
on first load.

Four subtypes (`architecture` / `constraint` / `pattern` / `convention`) × four statuses
(`approved` / `suggested` / `pending_approval` / `ignored`).

**Repo resolution** (`store._resolve_repo`) has strict precedence:

1. an explicit, sane `repo_path` argument
2. the repo bound to this MCP server process (captured once from its own startup cwd)
3. the shared `~/.contexer/.current_repo` pointer, as a last resort

Every step is sanity-checked, so a poisoned pointer can never select a config-dir store file. Hooks
additionally fall back to their own cwd, because hosts run hooks with cwd set to the project and
non-git projects are first-class stores keyed by absolute path.

## Five capture paths

| Path | Trigger | Model involved? |
|---|---|---|
| `update_context` | AI calls it mid-session | yes |
| Constraint auto-capture | prompt says "always X" / "never Y" | **no** — regex |
| Convention mining | bootstrap scan of the repo | **no** — AST + config parsing |
| Memory sync | Claude's memory-tool files | **no** — deterministic import |
| Plan capture | `ExitPlanMode` approval | yes, stored *provisional* |

The non-model paths carry most of the weight. Constraint capture stores a clean standalone directive
as **trusted**; a directive carrying a conversation-local reference ("make *this* a rule") lands
`pending_approval` with an in-band ack that explicitly forbids self-approval. Restatements are
routed onto the existing entry — a superset becomes a suggested update, a terse restatement records
a recurrence — rather than accumulating near-duplicates.

Memory sync is a *coexistence* path, not a competitor for capture: rather than fight an authority
contest that hook-injected reminders always lose against another tool's system-prompt memory
workflow, Contexer becomes a deterministic sync target.

## Retrieval V1 — BM25 topic router

`get_context_for_prompt` is backed by a disposable BM25 index sidecar, written only at the tail of
`_save` (every writer already holds the store lock) and read read-only per prompt. It indexes
`approved`/`suggested` entries only, so a stale index can never advertise a decision `get_context`
would refuse to return. A missing, corrupt, or wrong-version index falls back verbatim to the
pre-V1 keyword lookup — never rebuilt inline.

Ranking combines BM25 with a fixed topic-alias table (db/api/auth/frontend/deploy/testing/config/
perf/security) and artifact extraction (file paths, dotted modules, `*Error`, route-shaped strings —
double-weighted), driving a three-rung **injection ladder**:

1. **STRONG** match → full decision content, capped at 3
2. **WEAK** topic overlap → a short pointer line
3. rationale/project prompts only → overview fallback, then the global store

Anything else stays silent by design. A per-session working set prevents re-injecting what has
already been surfaced this session, and on a `compact` source the pre-compaction working set is
rehydrated — otherwise that router state would be lost on replay.

Retrieval is **observable, not spooky**: the rationale hook emits a short user-facing line naming
what was recalled, with a benchmark-derived token-savings estimate on larger injections.

## Bootstrap and convention mining

`bootstrap_context` is insight-aware. It infers the developer's familiarity with the repo from git
signals (commit authorship by `user.email`, first-commit authorship, fresh-clone reflog entries),
TTL-cached for 24h and invalidated immediately on a changed identity or a re-cloned/rewound repo. It
then asks only the gap questions that insight level warrants, suppressing anything the miner already
measured. The offer renders as an interactive picker, armed once per session and deliberately not
resurrected after `/compact` — compaction continues the session in which the developer already
answered.

A first prompt that is a **question about the repo** is always answered directly; the setup menu is
reserved for a first prompt that is a **task**.

`miner.py` is stdlib-only and deterministic. It measures conventions with the evidence embedded in
the sentence itself — *"Functions use snake_case naming (98% of 412 functions across 37 files)"* —
from configs (ruff, mypy, pytest, pre-commit, prettier/eslint/tsconfig, CI), AST source statistics,
test layout, and commit-message convention. Thresholds are the verification story:

- ≥90% dominance with ≥20 samples → tier high, born `approved`
- 60–89% → tier medium, `pending_approval`
- below either bar → **not emitted at all**

Silence over noise. JS/TS/Go/Rust *source* statistics are deliberately out of scope; those languages'
conventions are captured via configs, which is the verified path.

## Team sync

The local store is a capture client, cache, and outbox; the heavy query side (timeline, diff, audit,
shared semantic search) lives upstream in the teams MCP. The embedded JSON maps 1:1 onto the upstream
`decisions` + `revisions` tables, so no schema migration is needed to add those.

Flow: `contexer login` → `share` / `pull` / a per-prompt delta poll.

**Sharing is confirmed-by-default.** A preview shows exactly what would leave the machine (a pure
local read) and requires `confirm=true` to send; a developer can opt out via config.

**The poll is non-blocking by design** — the prompt path never performs network I/O. A detached
refresher runs at most once per 15s and its batch surfaces on the *following* prompt. The trade-off
is one prompt of extra staleness, in exchange for a hard guarantee that a slow or hanging cloud can
never stall prompt submission. Per-consumer high-water marks mean a Claude and a Codex session on
the same repo each receive every synced batch exactly once, independently.

## Secrets — egress-only redaction

`redact.py` scrubs **only where a decision actually leaves the machine**: `remote._wire_args` (the
last mile every push funnels through — the hard guarantee) and `store._share_projection` (so the
confirm-preview and durable outbox show exactly what will be sent), surfaced by a
`(N secrets redacted before sending)` banner.

Capture is deliberately **not** redacted. The local store is a verbatim record, and silently
rewriting captured decisions would corrupt the product's core value — scrubbing `0600` local files
also buys little against a reader who already has `~/.aws` and `.env`.

Detection is balanced: high-confidence provider token shapes, PEM private-key blocks, JWTs, Bearer
tokens, and connection-string passwords, plus a keyword-gated catch-all that fires only on
secret-*looking* values (a prose gate keeps it from mauling ordinary decision text like
`auth: required`). No entropy heuristic — it would false-positive on SHAs, UUIDs, and base64.
Default-on, deterministic, idempotent, never raises.

## Host integration

| Host | Config | Hook events |
|---|---|---|
| Claude Code | `~/.claude.json` + `~/.claude/settings.json` | SessionStart, UserPromptSubmit ×5, PostToolUse, PreCompact, SessionEnd |
| Codex | `~/.codex/config.toml` + `hooks.json` | same, minus SessionEnd, plus PostCompact |
| Cursor | `~/.cursor/mcp.json` + `hooks.json` | sessionStart, beforeSubmitPrompt |
| Gemini CLI | `~/.gemini/settings.json` | SessionStart, BeforeAgent, AfterTool, PreCompress, SessionEnd |

Adding a host is one new module in `adapters/`; no other file changes.

Several hook decisions are deliberate rather than accidental, and are worth not re-litigating:

- **No `Stop` hook.** End-of-turn prompting added latency and tokens and depended on model behavior,
  delivering nothing the deterministic `PostToolUse` flag plus next-prompt anchor doesn't already
  deliver at a non-interrupting moment.
- **No `PostCompact` hook for Claude.** The event supports no `additionalContext` and its
  `systemMessage` is user-facing only, so it reloaded nothing and merely dumped the full context into
  the transcript as visible noise. `SessionStart(source="compact")` already owns post-compaction
  reload.

Both are *actively stripped* on install if left over from an older version, while foreign hooks of
the same name survive untouched. Compact-reload parity holds across all three injecting hosts.

A meaningful share of the adapter code is **upgrade hygiene**: legacy repo-settings cleanup,
self-retiring no-op stubs for removed hook entrypoints, stale permission pruning. The goal is that a
plain package upgrade self-heals each repo without requiring a reinstall.

## Surfaces

**11 MCP tools** — `update_context` · `get_context` · `get_context_for_prompt` ·
`capture_user_constraint` · `approve_decision` · `review_pending` · `bootstrap_context` ·
`list_shareable` · `share_decision` · `update_global_context` · `get_global_context`

**11 CLI commands** — `install` · `uninstall` · `reinstall` · `status` · `review` · `share` ·
`pull` · `login` · `logout` · `version` · `help`

## Module map

| Module | Role |
|---|---|
| `server.py` | MCP entry point; defines the tools, delegates all logic to `store.py` |
| `store.py` | All read/write, filtering, retrieval, session payloads |
| `miner.py` | Deterministic convention mining (leaf — imports nothing local) |
| `redact.py` | Deterministic secret redaction (leaf) |
| `memory_sync.py` | Imports Claude memory-tool facts into the store |
| `remote.py` / `share.py` / `team_context.py` / `auth.py` | Teams client, outbox, sync cache, login |
| `repo_key.py` / `config.py` | Canonical repo identity; profiles and settings |
| `cli.py` | The `contexer` console script |
| `adapters/` | One module per host, plus shared config helpers in `base.py` |

## Invariants

- **Silence over noise.** Filtered content is discarded without logging. The one deliberate,
  ratified exception is the rationale hook's recall notice.
- **Never blocking.** Pending review is a *notice*, never "wait for the developer." No network I/O
  on the prompt path.
- **Secrets never egress, but capture stays faithful.**
- **No abstraction beyond what exists.** The module structure is intentional — no classes, config
  files, or layers without a spec change.
- **Bookkeeping never costs you context.** Inability to write an optional bookkeeping file
  (`.current_repo`, `.pending_capture`, `.pending_review`, `.resume_mining`) never prevents a hook
  from rendering context. Writes that carry actual decisions still fail loudly.

## Currency of this document

Written against v0.26.1. The tool list, CLI commands, adapter set, and hook events above were read
out of the source; the retrieval, mining, and team-sync sections are a synthesis of `CLAUDE.md` and
should be treated as documented intent rather than independently re-verified behavior. The final
invariant ("bookkeeping never costs you context") lands with
[#153](https://github.com/bhargavamin/contexer/pull/153) — check whether it has merged before
relying on it.
