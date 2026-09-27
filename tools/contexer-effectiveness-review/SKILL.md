---
name: contexer-effectiveness-review
description: Records evidence of whether Contexer helped a coding session, separating script-observed facts from the agent's judgment. Use when a stop-hook prompt says "Contexer effectiveness review due", or when the user asks to log, review, or rate how Contexer was used in this session.
---

# Contexer effectiveness review

Primary question: when non-code engineering intent matters, does Contexer supply a fact
that materially changes the action and could not reliably be recovered from the current
repository and good decision documentation? Separate how often this opportunity occurs
from success when it occurs. A record that says Contexer did not help is exactly as
valuable as one that says it did.

Each record has three parts, produced by different means:

| Part | Produced by | Trust |
|---|---|---|
| `observed` | `stop_hook.py`, from git and the transcript | measured |
| `judgment` | you, now, following [schema.md](schema.md) | self-assessed |
| outcomes | `outcomes.py`, later, from GitHub and git | independently observed status; not causal proof |

## Steps

Resolve script paths relative to this skill directory, not the current repository.

1. Take `token`, `pending` and `logger` from the review prompt. Without a prompt (the user asked
   directly), create them first:
   `uv run --no-project python scripts/stop_hook.py --manual --host <cursor|claude|codex> --session <id> --transcript <path> --repo <repo>`
2. Read the pending file. It holds the commits, how they were attributed to this session, the
   Contexer calls the script saw, and which facts the transcript could show (`null` means not
   visible, not zero). Never edit it: the logger rejects a pending file whose digest changed.
3. Review only the pending segment, from `segment.from_offset` through `segment.to_offset`
   when present. A retried token still refers to its original evidence; exclude later work.
   Write the judgment JSON defined in
   [schema.md](schema.md):
   - the task and what kind of problem it was: knowledge needed, where the answer lived, scope;
   - a stable `task.task_id`, reused for later segments of this same task;
   - whether a non-code-context opportunity existed, regardless of what Contexer returned;
   - the facts the outcome depended on, their categories and separately assessed recovery
     from current code, repository docs, Git history, PR history and external docs;
   - whether relevant Contexer knowledge existed and surfaced; a failed search does not prove
     it was never stored, so use `uncertain` without evidence;
   - whether Contexer was used and changed an action: no change, minor, material, prevented
     wrong action, or uncertain; identify before/after actions and link material fact ids;
   - constrained impact class and severity, with uncertainty when evidence is insufficient;
   - every Contexer decision that was surfaced, and how relevant it was;
   - a rating for every Contexer feature that appeared, including ones that did nothing useful;
   - what was missing or wrong;
   - improvement ideas this session motivated: new features, changes to existing ones, or
     features to reduce or remove;
   - the verdict.
4. Validate against the observed facts, then log (writes under `~/.contexer-usage/`; request
   permission if sandboxed):
   `uv run --no-project python <logger> --check --token <token> < judgment.json`, then
   `uv run --no-project python <logger> --token <token> < judgment.json`.
   On exit 2, fix the listed problems and resubmit. Never drop the record. The logger rejects
   ids that the transcript shows were never surfaced, a `not_used` verdict when Contexer was
   called, self-credit for this session's own captures, notes over 200 characters and anything
   that looks like a credential.
5. Tell the user in two sentences: the verdict and the most useful gap. Then stop; do not resume
   other work in this turn.

## Validity rules

- **Facts before judgment.** List `key_facts` and `contexer_items` first, then pick the verdict
  from them, never the other way round.
- **Default to no effect.** When unsure, use `neutral`, `unknown`, `low`. `decisive` means the
  outcome would have been wrong or blocked without it; `helpful` means it saved real steps.
- **Name the counterfactual.** Assess a capable agent with good decision docs and Git/PR
  access, not an artificially weak baseline. Use the expanded `code_would_reveal` label and
  five separate recovery flags. Code suggesting intent is not proof of that intent.
- **Opportunity is independent of use.** Framework, file layout and visible behavior alone
  are not non-code opportunities. Rationale, exceptions, migration intent and authority may
  be. A no-opportunity or uncertain record is valid; do not manufacture an opportunity.
- **Calls are not impact.** A call or repeated fact is not material help. `helpful` can describe
  convenience, but conditional success requires a prior fact linked to a material action.
  Assess severity separately from confidence. Do not claim time, token or dollar savings.
- **No pretend independent review.** Do not fill secondary assessments yourself. A separate
  reviewer first seals required facts without the primary verdict or causal claim; attribution
  is revealed only afterward. Follow the optional process in [README.md](README.md).
- **No self-credit.** Decisions stored in this same session are marked `created_this_session`
  and cannot support a `helpful` or `decisive` verdict.
- **Surfaced is not used.** A decision that was injected but did not change what you did is
  `redundant` or `irrelevant`, not `helpful`.
- **Count what went wrong.** Stale, wrong, noisy or missing context goes in `gaps`; a case where
  code reading answered faster or better goes in `llm_did_better`.
- **Classify the problem, not the answer.** Set `question_type`, `knowledge_location` and
  `scope` from what the task needed. This is what later shows which problems Contexer suits
  (for example unwritten rationale) and which the LLM handles alone (for example local code
  behaviour).
- **Rate every feature you met.** A feature that ran but did nothing is `not_useful`; one that cost
  attention or tokens is `noise`. Silence is not approval.
- **Ideas need a trigger.** Each improvement cites the moment in this session that prompted it.
  Generic wishlists are left out.
- **Only this session.** Cite decision ids you actually saw here. Do not reconstruct what a
  tool probably returned.
- **Stay local.** Notes are at most 200 characters and never paste decision bodies, secrets or
  customer data.

## Reports

- `uv run --no-project python scripts/outcomes.py` records PR state, CI result and reverts (including reverts of
  a squash or merge commit) whenever they change. A failed PR lookup is `unknown`, not `no_pr`.
- `uv run --no-project python scripts/summarize.py [--repo-key KEY] [--host HOST] [--kind hook|manual]` prints opportunity, conditional success, fact recovery, the benefit funnel,
  severity and feature burden with explicit denominators and unknowns. It ranks a problem kind only once it has 5 records, skips
  unreadable records and counts duplicates once.

Previously absent PRs are checked again on later outcome runs. Failed fetches or git queries
produce unknown revert status. Branch-based PR matching requires the reviewed commits;
branch reuse and PR-command-only evidence remain unverified without commit membership. Reports separate unknown tool counts from observed zero counts.

## Known limits

- Judgments are self-assessed, and automatic reviews sample commit or PR-command segments; manual reviews must be analyzed separately; there is no
  run without Contexer. Credited shares are the agent's opinion, not a measured effect.
- Each commit carries an `attribution`: `transcript` (this session ran a commit-creating git
  command), `subagent` (it started a subagent), `unverified` (it ran shell commands, such as a
  release script, but no visible git command), or `git_only` (no transcript; 15-minute
  lookback). Sessions that made no shell or subagent call are never credited. Filter out
  `unverified` when two sessions may have committed in the same repo at once.
- Unlogged automatic reviews are reoffered with the same token on the next normal stop, until
  logged or the seven-day pending retention expires. New activity remains queued behind them.
  A matching SHA in a visible Git command result can establish a temporary committer identity;
  a command string alone cannot.
- Commits made in an aborted turn, or in a continuation another hook forced, are reviewed on the
  next normal turn. Failed git queries and timeouts preserve the evidence for the next stop.
  Each git call has a 4-second limit; the hook's git work has a shared 7-second budget.
- Literal shell arguments in Codex wrappers are command-intent evidence, not proof that the
  inner command ran successfully. PR-command triggers likewise do not prove a PR was created.
  Dynamic shell expressions and arbitrary wrapper APIs cannot be reconstructed reliably.
- Codex wrappers hide inner tool counts and result provenance. Their total Contexer count and
  exhaustive result ids are null; directly observed calls remain a lower bound. Missing
  transcripts also produce null measurements. Do not turn unknown counts into zero.
- Cursor transcripts omit tool results and injected context, so ids and auto-fetch counts are
  `null` there and ids cannot be cross-checked.
- Cursor also runs Claude Code hooks; the Claude hook ignores Cursor payloads so each turn is
  reviewed once.
- Transcript replacement is detected from the consumed bytes, including replacements of equal
  or greater length. A replacement can replay old command evidence; exact event continuity
  across host compaction is not guaranteed.
- Observer-visible captures from earlier reviews in this session still count as self-captures.
  Unknown fields and duplicate feature ratings are rejected; positively observed features must
  be rated. Hidden captures on hosts without result provenance remain self-reported.
- Queries, titles and decision bodies are not copied from tool arguments into measurements.
  Known credential shapes in commit/remote metadata are redacted. Unrecognized secrets and
  short pasted decision bodies in judgment text cannot be detected reliably.

## Tests

`uv run --no-project --with pytest pytest -c pytest.ini tests -q --tb=short -p no:cacheprovider` from this
directory. Tests use isolated usage homes and throwaway repositories, including the full
hook → logger → outcomes → summary flow for all three hosts.

Lint: `uvx ruff@0.15.4 check scripts tests`.
