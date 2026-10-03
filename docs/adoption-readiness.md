# Adoption readiness

A ten-minute read for a team lead deciding whether to adopt Contexer. Each item says what holds
today and where the evidence is (code, tests, docs), or names the gap and the issue tracking it.
Benchmark results are summarised qualitatively; the runnable benchmark is
[`benchmarks/ADOPTION_CAMPAIGN.md`](../benchmarks/ADOPTION_CAMPAIGN.md).

Status key: **holds** (evidence below), **partial** (holds with a stated limit), **gap** (not
handled yet; issue linked).

## Does it change what agents build?

| Question | Status | Evidence |
| --- | --- | --- |
| When the right decision reaches the agent, does the agent follow it? | holds | In the adoption benchmark, sessions that received the needed decision (in full or by name) succeeded on decision-dependent tasks; Contexer's losses came from decisions that never reached the agent. |
| Does it beat well-kept documentation? | partial | A full CLAUDE.md and an indexed `docs/decisions/` folder matched or beat Contexer on accuracy at both store sizes tested, because they can't miss a decision. Contexer costs less per session, and the gap widens as the store grows. It leads on conflicting decisions at the smaller size. |
| Does extra context hurt tasks that need no decision? | holds | No arm lost accuracy on tasks the code alone answers or that need no decision, at either store size. |
| Does retrieval find the right decision reliably? | gap | A lexical ranker misses decisions written in different words from the task, and file-anchored decisions crowd the three prompt slots: #359, #358, #349, #351. Agents rarely search on their own after a miss: #361. |

## Operational reliability

| Question | Status | Evidence |
| --- | --- | --- |
| Can Contexer block an agent session or a commit by failing? | holds | Hook output is best-effort and fails soft (CLAUDE.md, "Session behaviour"). The commit guard never blocks on its own failure; only explicitly armed rules can block (CLAUDE.md, "Commit-time guard"). |
| What happens on a corrupt store file? | partial | Reads degrade to an empty store, so sessions keep working (`store.load`, `store.load_diagnostics` tells corrupt from empty). **But the next capture overwrites the corrupt file and loses every earlier decision:** #368. |
| Does session-start context reach the model as stores grow? | gap | Claude Code replaces hook output over about 10KB with a short preview, so on a large store most session-start rules, including approved constraints, never reach the model: #365. |
| Partial installs and version skew? | partial | `contexer reinstall` and `upgrade` re-sync hooks and keep foreign hooks (`adapters/base._is_ours`); a release notice appears at most once per release (`updates.py`). Hooks call the installed package directly, so an uninstalled package shows host hook errors rather than silent loss. No health check reports a half-installed host. |
| Which hosts are supported? | partial | Claude Code, Codex and Gemini CLI get session-start and per-prompt delivery; Cursor gets session-start only (CLAUDE.md, adapters). Only Claude Code was benchmarked. |

## Security, privacy and governance

| Question | Status | Evidence |
| --- | --- | --- |
| Where is data stored, and who can read it? | partial | `~/.contexer/`, one JSON file per repository, written atomically at mode 0o600, so only the user can read the contents (`store.atomic_write`). The directory itself is created by `contexer install` with the default umask (typically 0o755), so other local users can list file names, which include repository paths: #373. |
| What leaves the machine? | partial | Decisions leave only when a developer shares explicitly (`contexer share`) or enables team proposals; sharing is never automatic on capture. Separately, the prompt hook can start a background update check that fetches Contexer's release information from PyPI; it sends no decisions, and `CONTEXER_NO_UPDATE_CHECK` turns it off (`updates.py`). |
| Are secrets redacted when decisions are shared? | partial | Yes by default, at the one egress chokepoint (`redact.py`, `remote._wire_args`, `store._share_projection`), and a broken config keeps it on. But a user can switch it off (`redact_secrets = false` in `~/.contexer/config.toml`, or the console's Config view), and nothing warns when it's off: #372. |
| Are secrets in captured decisions protected locally? | partial | Capture is verbatim by design, so a secret an agent captures is stored in plain text, readable only by the user. There's no way to erase one decision's content short of purging every store: #370. |
| Who can make a decision authoritative? | partial | AI-captured constraints, and decisions worded as firm choices ("instead of", "must never"), stay pending until a human approves them. Other AI captures are active straight away but labelled `[suggested]`, so the agent sees they aren't approved (`store._classify_level`, `approve_decision`, `contexer review`). Approval is one id at a time; there's no bulk approval. |
| Is there an audit trail? | holds | Every decision keeps immutable revisions with author, dates and approvals; retirement and supersession keep history (`revisions.py`, `lifecycle.py`). The console shows the timeline (`contexer ui`). |
| Team credentials? | holds | Browser OAuth (`contexer login`); tokens in `~/.contexer/.team_auth.json` at mode 0o600; `contexer logout` deletes credentials and their caches (`auth.py`). |
| Retention and deletion? | partial | No time-based retention. Retire, ignore and console Delete keep history; `contexer uninstall --purge` deletes everything. Per-decision erasure: #370. |

## Ownership and upkeep

| Question | Status | Evidence |
| --- | --- | --- |
| Who approves, resolves conflicts and retires stale decisions, and with what? | holds | `contexer review` (approve, edit, ignore, retire, overlap consolidation), `contexer retire` and `restore`, the console's lifecycle and reconsideration lanes, conflict memos (`conflicts.py`), and anchor verification that proposes retirement when anchored code disappears (`anchors.py`). Ownership itself is a team choice Contexer doesn't make. |
| Are contradicting decisions flagged? | gap | Two current decisions that contradict each other are only noticed if the agent sees both: #360. |
| How much human time does upkeep take? | gap | Not measured yet; the capture-and-maintenance evaluation and a real-repository pilot will measure review minutes, capture precision and staleness: #363. |
| Onboarding an existing repository? | partial | `bootstrap_context` captures evidence-backed context as non-authoritative observations; nothing becomes policy without approval (CLAUDE.md, "Never claim complete decision capture"). Its effort on a real repository is part of #363. |

## Cost

| Question | Status | Evidence |
| --- | --- | --- |
| Model cost per session | holds | Contexer injects a bounded subset of decisions, so it costs less per session than loading every decision into CLAUDE.md, and the difference grows with store size (adoption benchmark, both store sizes). |
| Human cost | gap | See upkeep above (#363). |

## Exit

| Question | Status | Evidence |
| --- | --- | --- |
| Can a team leave without losing its decisions? | partial | The store is plain JSON, readable without Contexer. There's no export to Markdown or ADR files: #369. |
| Can it be removed cleanly? | holds | `contexer uninstall` removes the MCP server and only Contexer-owned hooks; `--purge` also deletes the store after confirmation (`cli._confirm_purge`). |

## Noise

| Question | Status | Evidence |
| --- | --- | --- |
| Does it inject irrelevant context? | partial | Per-prompt retrieval is gated (rationale, project, question or file-shaped prompts) and capped at three decisions, but short or generic prompts can still pull unrelated decisions, and a file path in the prompt can outweigh the task's subject: #334, #358. |
| Do repeated low-value injections teach people or agents to ignore it? | gap | Not measured; part of the pilot (#363). |

## Before adopting

Set your own bar before a pilot, from your economics rather than Contexer's: for example,
"material gain on tasks that depend on unwritten decisions, no regression on ordinary tasks,
and less than N engineer-hours a month of upkeep". Then measure against your current process
(ADRs, CLAUDE.md, a wiki), not against no documentation.
