# Adoption readiness

*For a team lead deciding whether to adopt Contexer. Written as a press release and FAQ: what holds today first, then the questions you would ask, each answered with its evidence (code, tests, docs or the [benchmark](benchmark.md)) or with the open issue that tracks the gap.*

## Press release

### Ready for a team pilot

**The same answers as a hand-kept `CLAUDE.md`, 30% lower cost per session at 150 rules, and decisions your team can approve, audit, export and erase.**

**October 2026.** Teams adopting AI coding agents quickly accumulate rules the agents must follow, usually in a `CLAUDE.md` or a folder of decision docs that nobody owns, nobody approves and nothing checks. Contexer v0.50.1 keeps those decisions in a local store, hands the agent the relevant ones at session start and with each prompt, and gives the team the controls a shared rulebook needs: human approval, revision history, conflict marking, export and erasure.

In the [adoption benchmark](benchmark.md) (1,242 sessions, scored by code), agents using Contexer got **109 of 114** tasks right with a 150-rule store, against **108** with a full `CLAUDE.md` and **108** with decision docs, at **$0.076** per session against $0.109 and $0.103. When two current rules contradicted each other, the agent asked the developer in **12 of 12** runs, against 7 of 12 with the static files. Whenever the needed rule reached the agent, it followed it.

Contexer fails safe. Hooks never block a session or a commit by failing; a corrupt store keeps sessions running and refuses to be overwritten; data stays on the developer's machine unless someone shares a decision explicitly, and sharing with redaction switched off always warns and asks for confirmation. Every decision keeps its history, can be exported to Markdown or ADR files, and can be erased by a human when it holds something sensitive.

What a pilot should watch, because capture is still the weak point: rules a developer states during a session reach the store in only **6 of 24** runs in the benchmark, against 24 of 24 for a `CLAUDE.md` the agent is told to maintain ([#385](https://github.com/bhargavamin/contexer/issues/385)), so a person should add or approve the rules that matter. Rules worded very differently from the task can be missed ([#390](https://github.com/bhargavamin/contexer/issues/390)). The human time upkeep takes has not been measured yet ([#363](https://github.com/bhargavamin/contexer/issues/363)).

**To start a pilot:** `uv tool install contexer` then `contexer install` ([quick start](../README.md#quick-start)), keep your existing `CLAUDE.md` for stable instructions, and set your adopt / don't-adopt bar before you begin ([last question below](#how-should-we-run-a-pilot)).

## Frequently asked questions

Each answer is marked **holds** (works today, with evidence), **partial** (works, with a stated limit) or **gap** (not handled yet, with the issue tracking it).

### Does it change what agents build?

| Question | Status | Evidence |
| --- | --- | --- |
| When the right decision reaches the agent, does the agent follow it? | holds | Every benchmark run where the needed rule arrived succeeded (116 runs across both store sizes); every Contexer loss was a rule that never arrived ([benchmark](benchmark.md#why-does-contexer-still-get-some-tasks-wrong)). |
| Does it beat well-kept documentation? | partial | It matches them on accuracy: 108 vs 106 (`CLAUDE.md`) and 111 (decision docs) of 114 at 34 rules; 109 vs 108 and 108 at 150 rules. It costs 12–18% less per session at 34 rules and 26–30% less at 150 ([benchmark](benchmark.md#how-much-cheaper-is-it-and-why)). |
| Does it catch contradicting rules? | partial | In the benchmark the agent asked before coding in 12 of 12 runs at both store sizes, against 5–9 of 12 for the static files (small samples: 12 runs per cell). Contexer marks a contradiction explicitly only for incompatible version formats (`conflicts.py`); other kinds aren't detected automatically. |
| Does extra context hurt tasks that need no decision? | holds | On tasks the code alone answers or that need no rule, Contexer got 36 of 36 at both store sizes; a full `CLAUDE.md` got 35 of 36. |
| Does retrieval find the right decision reliably? | partial | Anchored decisions no longer crowd out task-matched ones (#358, #349, fixed). Rules worded differently from the task are still missed (#390), inflected words such as "caching" vs "cache" can miss (#351), and agents rarely search on their own after a miss (#361). |

### Will it get in the way?

| Question | Status | Evidence |
| --- | --- | --- |
| Can Contexer block an agent session or a commit by failing? | holds | Hook output is best-effort and fails soft (CLAUDE.md, "Session behaviour"). The commit guard never blocks on its own failure; only explicitly armed rules can block (CLAUDE.md, "Commit-time guard"). |
| What happens on a corrupt store file? | holds | Session reads degrade to an empty store, so work continues (`store.load`). Every write path reads through a strict reader that refuses a corrupt file instead of overwriting it, so earlier decisions are not lost (`store.load_for_update`, #368). |
| Does session-start context reach the model as stores grow? | holds | Claude Code cuts long session-start context down to a short preview; Contexer keeps its startup context under that limit, approved constraints in full first and a pointer to the rest (#365). A retained live 150-decision session received its needed constraint in full ([receipt](../benchmarks/artifacts/issue365-live/receipt-summary.json)); the 150-rule benchmark rerun on v0.50.1 is the end-to-end check. Other hosts have no inferred Claude cutoff. |
| Partial installs and version skew? | partial | `contexer reinstall` and `upgrade` re-sync hooks and keep foreign hooks (`adapters/base._is_ours`); a release notice appears at most once per release (`updates.py`). Hooks call the installed package directly, so an uninstalled package shows host hook errors rather than silent loss. No health check reports a half-installed host. |
| Does it inject irrelevant context? | partial | Per-prompt retrieval is gated and capped at three full decisions, but short or generic prompts can still pull unrelated ones (#334). Whether repeated low-value injections teach people or agents to ignore it is not measured (#363). |
| Which hosts are supported? | partial | Claude Code, Codex and Gemini CLI get session-start and per-prompt delivery; Cursor gets session-start only (CLAUDE.md, adapters). Only Claude Code was benchmarked. |

### Is our data safe?

| Question | Status | Evidence |
| --- | --- | --- |
| Where is data stored, and who can read it? | holds | `~/.contexer/`, one JSON file per repository, private to the owner (mode 0o600) in a directory created at 0o700; next use removes group and world permissions from an existing directory. |
| What leaves the machine? | partial | Decisions leave only when a developer shares explicitly (`contexer share`) or enables team proposals; sharing is never automatic on capture. Separately, the prompt hook can start a background update check against PyPI that sends no decisions; `CONTEXER_NO_UPDATE_CHECK` turns it off (`updates.py`). |
| Are secrets redacted when decisions are shared? | holds | Yes by default, at the one egress chokepoint (`redact.py`). A user can switch it off (`redact_secrets = false`), but then every share shows a warning and requires confirming the exact preview, `skip_confirm` and `--yes` cannot bypass it, and background sends and automatic proposals pause (#372; [usage](usage.md)). |
| Can we remove a secret that was captured by mistake? | holds | `contexer erase <id>` or the console's **Erase content** removes the decision's text, revisions, proposals, linked evidence and local copies, keeping only a content-free record (id, dates, actor, reason). It is human-only (no agent tool can call it) and refuses shared decisions until the team copy is erased too. Limit: a pending evidence event that only paraphrases the decision without being linked to it is not found (#370; [usage](usage.md#erase-sensitive-content)). |
| Who can make a decision authoritative? | partial | AI-captured constraints, and decisions worded as firm choices ("instead of", "must never"), stay pending until a human approves them. Other AI captures are active straight away but labelled `[suggested]`, so the agent sees they aren't approved (`store._classify_level`, `approve_decision`, `contexer review`). Approval is one id at a time; there is no bulk approval. |
| Is there an audit trail? | holds | Every decision keeps immutable revisions with author, dates and approvals; retirement and supersession keep history (`revisions.py`, `lifecycle.py`). The console shows the timeline (`contexer ui`). |
| Team credentials? | holds | Browser OAuth (`contexer login`); tokens in `~/.contexer/.team_auth.json` at mode 0o600; `contexer logout` deletes credentials and their caches (`auth.py`). |
| Retention and deletion? | partial | No time-based retention. Retire, ignore and console Delete keep history; erase removes one decision's content; `contexer uninstall --purge` deletes everything. |

### Who keeps it up to date, and at what cost?

| Question | Status | Evidence |
| --- | --- | --- |
| Do rules stated during work get captured? | gap | In the benchmark, a rule stated in one session was recorded for the next in 6 of 24 runs, against 24 of 24 for a `CLAUDE.md` the agent was told to maintain. Agents rarely call `update_context` on their own, and prompt capture recognises only some phrasings (#385). |
| Who approves, resolves conflicts and retires stale decisions, and with what? | holds | `contexer review` (approve, edit, ignore, retire, overlap consolidation), `contexer retire` and `restore`, the console's lifecycle and reconsideration lanes, conflict memos (`conflicts.py`), and anchor verification that proposes retirement when anchored code disappears (`anchors.py`). Ownership itself is a team choice Contexer doesn't make. |
| How much human time does upkeep take? | gap | Not measured yet; the real-repository pilot will measure review minutes, capture precision and staleness (#363). |
| Onboarding an existing repository? | partial | `bootstrap_context` captures evidence-backed context as non-authoritative observations; nothing becomes policy without approval (CLAUDE.md, "Never claim complete decision capture"). Its effort on a real repository is part of #363. |
| Model cost per session | holds | 12–18% less than static rule files at 34 rules and 26–30% less at 150, because only the relevant decisions are sent ([benchmark](benchmark.md#how-much-cheaper-is-it-and-why)). |

### Can we leave?

| Question | Status | Evidence |
| --- | --- | --- |
| Can a team leave without losing its decisions? | holds | `contexer export --format md` or `--format adr` writes current decisions with status, dates and file applicability, redacted by default (`--verbatim` for an explicit local copy). Export is read-only and stops without touching earlier output if the store is unreadable (#369; [usage](usage.md)). The store itself is plain JSON. |
| Can it be removed cleanly? | holds | `contexer uninstall` removes the MCP server and only Contexer-owned hooks; `--purge` also deletes the store after confirmation (`cli._confirm_purge`). |

### How should we run a pilot?

Set your own bar before the pilot, from your economics rather than Contexer's: for example, "no loss of accuracy against our current `CLAUDE.md`, contradicting rules flagged, and less than N engineer-hours a month of upkeep". Measure against your current process (ADRs, `CLAUDE.md`, a wiki), not against no documentation. Because capture is the weak point, have a person add or approve the rules that matter at the start, and count how many new rules stated during the pilot reach the store on their own.
