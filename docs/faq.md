# Contexer FAQ

Contexer captures and versions engineering decisions, makes relevant approved context available to
AI coding agents, and gives developers a reviewable record of how that context changes. The optional
local Guard checks staged changes against approved decisions. The optional hosted products add
cross-machine sync; Contexer Teams adds selective team sharing, lead review, GitHub pull-request
checks, and optional merge gates.

Contexer is focused on engineering decisions. It is not a general-purpose AI memory system, a
replacement for Git or documentation, a full code-review platform, an autonomous architect, or a
guarantee that generated code is correct.

## Find your answer

Browse all topics: [Understanding Contexer](#understanding-contexer) ·
[Capturing and reviewing decisions](#capturing-and-reviewing-decisions) ·
[Relevance and agent support](#relevance-and-agent-support) ·
[Guards and pull-request checks](#guards-and-pull-request-checks) ·
[Privacy and team sharing](#privacy-and-team-sharing) ·
[Adoption and product fit](#adoption-and-product-fit)

### I’m evaluating Contexer

- [What is an engineering decision?](#what-is-an-engineering-decision)
- [Why not just let the coding agent read the repository?](#why-not-just-let-the-coding-agent-read-the-repository)
- [How is Contexer different from a general AI memory product?](#how-is-contexer-different-from-a-general-ai-memory-product)
- [When is Contexer probably unnecessary?](#when-is-contexer-probably-unnecessary)

### I’m concerned about capture accuracy and control

- [How are decisions captured?](#how-are-decisions-captured)
- [Can Contexer capture something incorrectly or miss a decision?](#can-contexer-capture-something-incorrectly-or-miss-a-decision)
- [Who decides what becomes authoritative?](#who-decides-what-becomes-authoritative)
- [What happens when a decision changes or is removed?](#what-happens-when-a-decision-changes-or-is-removed)

### I need enforcement and pull-request checks

- [What is the local Contexer Guard?](#what-is-the-local-contexer-guard)
- [What are Contexer Check and Contexer Guard in Teams?](#what-are-contexer-check-and-contexer-guard-in-teams)
- [Can Guard or Check be wrong?](#can-guard-or-check-be-wrong)

### I’m checking privacy and team behavior

- [What stays local?](#what-stays-local)
- [What data is sent to Contexer Teams?](#what-data-is-sent-to-contexer-teams)
- [What is the difference between Contexer OSS and Contexer Teams?](#what-is-the-difference-between-contexer-oss-and-contexer-teams)
- [Why use Teams if developers can manage their own context?](#why-use-teams-if-developers-can-manage-their-own-context)

### I want to get started

- [Which coding agents are supported?](#which-coding-agents-are-supported)
- [How can I try it safely?](#how-can-i-try-it-safely)

## Understanding Contexer

### What is an engineering decision?

**Short answer:** A durable choice or constraint that should influence future implementation.

An engineering decision is a choice or constraint that should influence future implementation.

Examples include:

- “Tenant data must be accessed through the authorization service.”
- “Do not introduce another queue; extend the existing event bus.”
- “This service may bypass the normal gateway because of its latency requirement.”
- “Use the shared Terraform module for production databases.”

Not every fact, message, or code change is a decision. The useful distinction is whether it records
judgment that a future developer or agent may need in order to make the right change.

### Why not just let the coding agent read the repository?

**Short answer:** Often it can; Contexer adds intent, history, and approved constraints that code
alone may not reveal.

For many repositories, that is enough. Coding agents are increasingly good at understanding the
code that exists.

The harder problem is recovering intent that the resulting code does not fully express: why a
constraint exists, whether an exception was approved, which of two conflicting instructions is
current, or whether an earlier decision has been replaced. Contexer complements repository
understanding by preserving that decision history instead of asking each new session to infer it
again.

### Why not use CLAUDE.md, AGENTS.md, Cursor Rules, or GEMINI.md?

**Short answer:** Use instruction files for stable instructions; Contexer adds review, lifecycle,
scope, provenance, and sharing when those are needed.

You should use instruction files for stable instructions. They are simple, visible,
version-controlled, and directly supported by coding agents.

Contexer becomes useful when an instruction needs lifecycle around it: review, provenance, file or
repository scope, revision history, retirement, cross-agent delivery, team approval, or a check
against resulting changes. It complements instruction files rather than replacing them.

Contexer Teams can scan supported instruction files in repositories connected through its GitHub
App. Extracted rules remain a separate, reviewable source: they can inform advisory pull-request
checks, but they cannot block a merge or become an approved team decision merely because a model
extracted them. A lead must deliberately promote a rule into the decision workflow first.

### How is Contexer different from a general AI memory product?

**Short answer:** It preserves governed engineering judgment, not a general memory of everything a
user or agent has discussed.

General memory products usually optimize for recalling information that might be useful later.
Contexer focuses on engineering decisions and records additional meaning around them: trust state,
provenance, revision history, scope, file links, retirement, team review, and possible conflict with
a code change.

The goal is not to remember every conversation. It is to preserve reusable engineering judgment in
a form that developers can inspect and govern.

### How is Contexer different from a code-review tool?

**Short answer:** It checks a change against approved engineering decisions; it does not replace
general code review or defect detection.

Most code-review tools ask whether there is a defect, vulnerability, or quality problem in a change.
Contexer can ask an additional question: does this change appear to conflict with an engineering
decision this developer or team already approved?

A change may compile, pass tests, and still violate a project-specific constraint. Contexer adds that
decision context; it does not replace normal review, tests, linters, static analysis, or security
tools.

### Does Contexer replace documentation or ADRs?

**Short answer:** No. It complements documentation, ADRs, instruction files, and code.

Documentation explains systems, ADRs record significant choices, instruction files give agents
stable instructions, and code remains the implementation.

Contexer focuses on decisions that need to remain active during development and benefit from
relevance, review, revision history, sharing, or verification. These tools can coexist, and a stored
decision can point back to the files or evidence that explain it.

## Capturing and reviewing decisions

### How are decisions captured?

**Short answer:** Through clear developer directives, agent tool calls, and bounded local evidence
aggregation that produces review candidates.

The local product uses three capture paths:

1. Clear directives such as “always X,” “never Y,” or “do not Z” are recognized by deterministic
   prompt handling. A self-contained directive can be stored as trusted local context; an ambiguous
   directive that depends on conversation-local words such as “this” or “that” waits for review.
2. For other decisions, the coding agent calls Contexer’s decision tool when it recognizes a choice,
   constraint, convention, or pattern. This is best-effort. Saying “store that decision” is the
   explicit fallback when the agent misses one.
3. Contexer records a bounded local set of session signals, such as directives, edited files, and
   conclusions the agent reports about how something works. At a later checkpoint it can aggregate
   those signals into a review candidate. No individual signal is a decision, and an aggregated
   candidate is not trusted until a developer approves it.

The third path is a recovery net, not a transcript-comprehension guarantee. Contexer does not see
every agent response, test result, or diff. Cursor also cannot provide the same post-edit evidence as
the other supported hosts. `contexer status` reports the capture coverage available for each
installed integration.

### Can Contexer capture something incorrectly or miss a decision?

**Short answer:** Yes. Capture is best-effort, so Contexer keeps untrusted capture separate from
approved authority.

Yes. A model can misunderstand a conversation, and an agent can fail to call the capture tool.
Evidence aggregation improves recovery when one signal is missed, but it does not make capture
complete or infallible.

Contexer therefore separates capture from authority. Decisions and proposed updates that require
human trust appear in `contexer review` and the local console. A developer can approve, edit, skip,
or dismiss each item. Review also shows its provenance, supporting evidence, proposed file links,
similar stored decisions, and revision history.

Pending items are not automatically injected as settled instructions and cannot block a commit.
Approving a decision makes it trusted context; making it a blocking rule is a separate action.

### Who decides what becomes authoritative?

**Short answer:** The developer controls local authority; a team lead controls team-wide authority.

For local use, the developer controls the local store. Clear instructions the developer states
directly can become trusted immediately; AI-inferred decisions and significant AI-proposed changes
wait for review.

In Contexer Teams, a personal decision does not become team-wide authority merely because someone
shares it. It becomes a candidate. A user with the team’s `lead` role must approve or reject that
candidate in the web application. Team members cannot approve team decisions through an MCP tool,
and the team owner can promote other members to lead.

### What happens when a decision changes or is removed?

**Short answer:** Changes append revisions, proposed changes wait for approval, and deletion keeps a
restorable tombstone instead of erasing history.

Local decisions contain immutable revisions and a pointer to the current revision. A human edit
appends a revision instead of overwriting history. An agent-proposed change to an already trusted
decision waits as a proposed revision while the current approved wording remains active.

Deleting a local decision creates a tombstone. That keeps mined or imported material from silently
recreating it on the next session, and the local console can restore it. If the developer explicitly
states the rule again, that new instruction can be captured again.

Teams also preserves update and lifecycle provenance. A pending team update does not replace the
approved team decision until a lead accepts it. Retiring a personal source does not silently retire
an already approved team copy; Teams records the disagreement for lead attention while the team copy
remains authoritative.

Teams includes an exact decision-transition review workflow that can connect a proposed replacement
to related pull-request drift. When that rollout is enabled, a lead can review the old decision, the
replacement, enforcement impact, and Check provenance together. Approval installs the replacement
and records the transition; the pending proposal itself does not change an existing Check or Guard
result. Availability of this grouped interface depends on the Teams deployment’s rollout flag.

Contexer does not claim to infer every conflict or supersession correctly. Exact lineage and human
review remain the authority boundary.

## Relevance and agent support

### How does Contexer decide which context is relevant?

**Short answer:** It uses repository scope, explicit file links, path and topic matches, and lexical
ranking to select approved context.

Constraints and conventions are treated as standing project rules and are injected at session
start. Architecture decisions and patterns are normally retrieved when the current prompt asks about
design, rationale, or related implementation work.

Local retrieval is primarily deterministic and lexical. It uses repository scope, explicit
`source_files` links, file or module names found in decision text, extracted topics and artifacts,
and BM25 ranking. A decision explicitly linked to a requested file is a stronger signal than merely
mentioning that file. Contexer may return a short pointer instead of full content for weaker matches.

This is not a vector database or a general semantic-memory search. Lexical retrieval can miss a
decision phrased with unrelated synonyms, and duplicate detection can miss semantically equivalent
wording. The review and explicit query tools remain available when automatic relevance is not enough.

### Which coding agents are supported?

**Short answer:** Claude Code, Cursor, Codex, and Gemini CLI are supported, with different levels of
hook coverage.

Contexer currently integrates with:

- Claude Code
- Cursor 1.7 and later
- Codex
- Gemini CLI

Claude Code and Codex have the fullest hook parity. Gemini restores context on the turn after
compression. Cursor supports session-start injection and MCP access, but its hook model cannot inject
new per-prompt context or provide the same compaction and post-edit behavior. See
[Integrations](integrations.md) for the current parity details.

All four integrations can use the same local decision store, so decisions are not permanently tied
to one coding-agent vendor.

## Guards and pull-request checks

### What is the local Contexer Guard?

**Short answer:** An opt-in pre-commit check that is advisory by default and blocks only for
explicitly armed, machine-checkable local rules.

The local Guard is an opt-in pre-commit check over staged files:

```bash
contexer guard --install-hook
```

Its default tier is advisory. It pairs staged files with trusted, approved decisions through explicit
file links or path-shaped references in decision text, prints a bounded reminder, and allows the
commit.

A local commit is blocked only when the developer explicitly arms an approved decision with a
machine-checkable regex or secret check. AI-inferred semantic judgment does not block a local
commit. The Guard rechecks that the decision is still approved whenever it runs.

The local Guard fails open on its own internal error or time budget, and it can be bypassed with
`CONTEXER_GUARD=0` or Git’s `--no-verify`. It is a local safeguard, not a substitute for CI.

### What are Contexer Check and Contexer Guard in Teams?

**Short answer:** Check is an advisory pull-request review; Teams Guard is a separate optional check
that can enforce individual decisions deliberately promoted by a lead.

They are separate GitHub surfaces:

- **Contexer Check** is advisory. It fetches a pull request’s changed files, uses an LLM to compare
  applicable approved team decisions and reviewed instruction-file rules with the diff, posts a
  sticky comment, and stores findings and score history. Its score and advisory findings do not
  block a merge.
- **Contexer Guard** is an optional GitHub check run. A team lead can promote an individual approved
  team decision from advisory to blocking. Drift against a promoted decision, inability to read the
  blocking set, or incomplete evaluation of a promoted decision can fail that check.

A failing Teams Guard check blocks merging only when the repository’s GitHub branch protection or
ruleset requires the check named `Contexer Guard`, and the GitHub App has permission to write check
runs. The aggregate drift score, unpromoted decisions, and rules merely extracted from instruction
files cannot block a merge.

Promotion is a lead policy decision, not an automatic result of a confidence score. Editing the
claim or scope of a blocking decision demotes it back to advisory so changed wording does not retain
an old approval silently.

### Can Guard or Check be wrong?

**Short answer:** Yes. Deterministic rules can be misconfigured, and model-based drift judgments can
produce false positives or false negatives.

Yes. The local blocking checks are deterministic for their configured regex or secret patterns, but
the configuration itself can still be wrong or too broad. Teams Check and the drift findings used by
Teams Guard involve model interpretation and can produce false positives and false negatives.

Teams should observe a decision in advisory mode before promoting it and should use blocking only
when they trust both the decision and the evaluation behavior. A lead can resolve a Check finding as
valid, outdated, or false positive. Marking a blocking decision outdated or false positive demotes
the decision so the disputed judgment no longer continues gating merges.

Contexer Guard does not prove architectural correctness. It reports whether the evidence it could
evaluate appears consistent with decisions it knows about.

## Privacy and team sharing

### What stays local?

**Short answer:** Unshared decisions, raw capture evidence, prompt signals, and local decision
history stay on the developer’s machine.

Without Teams configuration, decisions are stored as owner-readable JSON under `~/.contexer/` and
local use requires no account. The local console runs on `127.0.0.1`, is authenticated, and can
inspect decisions, revisions, review items, deleted decisions, global rules, configuration, and any
locally cached team context.

Local capture evidence, prompt signals, edited-file observations, decision history, and unshared
decisions stay on the machine. Raw session evidence is not uploaded as a transcript. The optional
PyPI version check can be disabled with `CONTEXER_NO_UPDATE_CHECK=1`.

Local-first does not mean “nothing can ever leave the machine.” Logging in, syncing selected
decisions, enabling an automatic proposal policy, pulling team context, and using managed GitHub
checks are explicit remote workflows.

### What data is sent to Contexer Teams?

**Short answer:** Data leaves the machine only through explicit sync, sharing, policy, or managed
check workflows; the exact payload depends on the workflow.

There are three different data flows:

#### Personal sync and team proposals

By default, capture remains local. A manual `contexer share` previews the decision before sending it.
The transmitted decision can include its title and content, type, confidence, bounded evidence and
provenance, repository identity, stable decision/revision identifiers, confirmed source-file paths,
and completed lifecycle events. Egress redaction is enabled by default and applies to these text
fields, but users should still review the preview rather than treating redaction as a guarantee.

Automatic team proposals are also off by default. A developer may explicitly enable one policy for
one repository, authenticated account, and destination team:

```bash
contexer share-policy enable --team NAME_OR_ID
```

The default policy applies only to future approved repository-local revisions; including existing
decisions requires `--include-existing`. The policy never includes global rules and never approves a
team decision. Every submitted item still waits for lead review. Destination or account mismatches
pause the workflow instead of sending to a similarly named or newly selected team.

#### Team context

The client downloads approved team decisions into a separate local cache. Agent reads are then
served from the local personal store plus that cache rather than making a live cloud request on each
prompt. Teams stores account and membership identity, review and audit actors, decision data, and
the metadata needed for synchronization and governance.

#### Managed GitHub checks

Contexer Teams fetches pull-request changes from GitHub and sends the diff, applicable approved
decisions, and reviewed rules to its configured model provider on Contexer’s key. The diff is held in
memory for judging and is not stored in the Teams database, cache, disk, logs, or traces. The job row
stores repository and pull-request pointers. Stored findings can include cited file paths and a short
diff-derived explanation snippet.

When Teams scans repository instruction files, their content is read for rule extraction and the
source files are stored so leads can inspect where proposed rules came from.

Contexer does not generally upload coding-agent prompts, full transcripts, or arbitrary model
inputs/outputs. A decision derived from local evidence may carry bounded evidence summaries when it
is shared, but the raw local evidence ledger is not the share payload. For service-wide data
handling and subprocessors, see the public
[Contexer Privacy Policy](https://contexer.ai/privacy).

### What is the difference between Contexer OSS and Contexer Teams?

**Short answer:** OSS is local and individual; Personal Cloud synchronizes selected personal
decisions; Teams adds reviewed shared authority and managed GitHub workflows.

#### Contexer OSS

The MIT-licensed open-source product is designed primarily for individual, local use. It provides:

- local decision capture and review;
- versioned revisions, deletion tombstones, and restore;
- global and repository-specific decisions;
- deterministic and file-aware retrieval;
- integrations for Claude Code, Cursor, Codex, and Gemini CLI;
- an authenticated local web console; and
- an optional local pre-commit Guard.

The local store is per-user and per-machine unless the user opts into a remote product.

#### Personal Cloud and Contexer Teams

Personal Cloud synchronizes a developer’s selected personal decisions across their machines. It does
not make those decisions team authority.

Contexer Teams adds selective sharing, lead-reviewed team decisions, team history and provenance,
local caches for agent delivery, GitHub App integration, instruction-file rule extraction, managed
pull-request drift checks, finding resolution, audit records, and optional blocking GitHub Guard.
Governance is currently team-level; this is not an organization-wide policy hierarchy or a complete
enterprise governance suite.

### Why use Teams if developers can manage their own context?

**Short answer:** Teams is useful when shared decisions need cross-agent delivery, lead approval,
history, and optional pull-request verification.

Some teams do not need it. Personal context may be sufficient when developers work independently and
important decisions are already clear and current elsewhere.

Shared context becomes more useful when several developers work on the same systems, use different
coding agents, repeatedly correct the same architectural mistake, or need a human approval boundary
before a decision becomes team-wide. Teams turns selected personal context into deliberately shared
context; it does not automatically pool everything developers capture.

## Adoption and product fit

### Is Contexer an enterprise AI-governance platform or general policy engine?

**Short answer:** No. Its current scope is engineering decisions used in AI-assisted software
development.

No. Contexer currently focuses on engineering decisions used in AI-assisted software development.
Some decisions can act as enforceable constraints through local or Teams Guard, but Contexer does not
represent or enforce every organizational policy.

Organization-wide governance across many teams, enterprise identity features, regulatory mapping,
and similar capabilities should not be assumed unless they are documented as available.

### Can Contexer guarantee that AI-generated code is correct?

**Short answer:** No. Contexer checks one source of inconsistency; it cannot prove overall code
correctness.

No. Contexer does not replace automated tests, human review, security scanning, static analysis, CI,
architecture review, or domain expertise.

It adds another question to the development process: “Does this change appear consistent with
engineering decisions we already approved?” That can reduce one class of error, but it cannot prove
overall correctness.

### When is Contexer probably unnecessary?

**Short answer:** It may be unnecessary when the repository is easy to understand and important
decisions are already clear, current, and rarely lost.

Contexer may add little value when one developer works alone, the repository is small and easy to
understand, important decisions already live in clear and current instruction files, repeated
context loss is not a problem, or there is little need for review, sharing, lifecycle, or
verification.

It is most useful where developers repeatedly have to explain decisions that have already been made.

### How can I try it safely?

**Short answer:** Start with the local installation; sharing, hooks, automatic proposals, and
blocking enforcement all require separate explicit setup.

Contexer requires Python 3.12 or later and `uv`:

```bash
uv tool install contexer
contexer install
```

The default experience is local. Team sharing, automatic proposal policies, the local commit hook,
and blocking enforcement all require separate setup or explicit actions. Use `contexer ui --open` to
inspect what is stored, `contexer uninstall` to remove the integrations while keeping local data, or
`contexer uninstall --purge` to remove the integrations and `~/.contexer/` data.

For setup and operational details, see [Installation](install.md),
[How Contexer works](how-it-works.md), [Usage](usage.md), and
[Integrations](integrations.md).
