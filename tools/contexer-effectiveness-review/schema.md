# Judgment schema (contexer-effectiveness/v3)

`log_usage.py` rejects anything that does not match. All lists are required; use `[]` when
empty. Unknown fields are rejected. Every free-text field is at most 200 characters (`verdict.evidence` and
`llm_did_better` at most 300). Decision ids are the 8-character hex prefix.

The following example is synthetic; replace every fact and id with evidence from the session.

```json
{
  "task": {
    "summary": "Correct a cache expiry comparison",
    "category": "bugfix",
    "difficulty": "moderate",
    "question_type": "code_behavior",
    "knowledge_location": "in_code",
    "scope": "single_file",
    "task_id": "cache-expiry-example"
  },
  "key_facts": [
    {
      "fact": "The expiry check compared seconds with milliseconds",
      "source": "code",
      "code_would_reveal": "current_code_would_reveal",
      "contexer_ids": [],
      "fact_id": "fact-1",
      "knowledge_category": "implementation_fact",
      "action_change": "no_change",
      "alternative_sources": [
        "current_code"
      ],
      "could_current_code_reveal": "yes",
      "could_repo_docs_reveal": "uncertain",
      "could_git_history_reveal": "uncertain",
      "could_pr_history_reveal": "uncertain",
      "could_external_docs_reveal": "uncertain"
    }
  ],
  "contexer_items": [
    {
      "id": "a1b2c3d4",
      "surfaced_by": "autofetch",
      "relevance": "irrelevant",
      "created_this_session": false,
      "note": "An API naming rule did not affect this comparison"
    },
    {
      "id": "c0ffee01",
      "surfaced_by": "other_tool",
      "relevance": "redundant",
      "created_this_session": true,
      "note": "Recorded the unit convention after the fix"
    }
  ],
  "gaps": [
    {
      "kind": "noise",
      "need": "Retrieval returned an unrelated API naming rule"
    }
  ],
  "captures": [
    {
      "id": "c0ffee01",
      "kind": "convention"
    }
  ],
  "feature_ratings": [
    {
      "feature": "autofetch",
      "verdict": "not_useful",
      "note": "The naming rule changed no step"
    },
    {
      "feature": "get_context",
      "verdict": "not_useful",
      "note": "No stored rule explained the unit mismatch"
    },
    {
      "feature": "update_context",
      "verdict": "not_useful",
      "note": "Saved a future convention after solving the task"
    }
  ],
  "improvements": [
    {
      "kind": "improve_feature",
      "area": "autofetch",
      "idea": "Reduce unrelated API naming matches for expiry questions",
      "evidence": "The retrieved naming rule did not apply to the expiry comparison"
    }
  ],
  "verdict": {
    "contexer_effect": "neutral",
    "without_contexer": "same",
    "confidence": "high",
    "evidence": "Reading the comparison and its test revealed the unit mismatch"
  },
  "llm_did_better": "The implementation and test supplied the deciding fact",
  "opportunity": {
    "non_code_context_opportunity": "no",
    "opportunity_type": [],
    "opportunity_reason": "The correct unit comparison was directly visible in the implementation.",
    "required_fact_location": [
      "current_code"
    ],
    "relevant_knowledge_existed": "uncertain",
    "knowledge_surfaced": "no",
    "knowledge_evidence": "Only an unrelated naming rule was surfaced.",
    "could_current_code_reveal": "yes",
    "could_repo_docs_reveal": "uncertain",
    "could_git_history_reveal": "uncertain",
    "could_pr_history_reveal": "uncertain",
    "could_external_docs_reveal": "uncertain"
  },
  "action": {
    "contexer_used": "yes",
    "action_change": "no_change",
    "alternative_sources": [
      "current_code"
    ],
    "impact_class": "uncertain",
    "severity": "unknown",
    "action_before": "Inspect the expiry comparison.",
    "action_after": "Inspect the expiry comparison.",
    "evidence": "Contexer did not alter the engineering action.",
    "material_fact_ids": []
  }
}
```

## Fields

**task**
- `category`: bugfix, feature, refactor, test, docs, investigation, review, release, config, other
- `difficulty`: trivial, moderate, hard
- `question_type`, the main kind of knowledge the task needed: `rationale` (why it is this way),
  `convention`, `constraint`, `code_behavior` (what the code does now), `how_to`, `none`
- `knowledge_location`, where the answer actually lived: `in_code`, `in_docs`,
  `in_git_history`, `unwritten` (only in people's heads, past chats or reviews), `external`
  (library docs, web), `mixed`
- `scope`: single_file, module, cross_module, cross_repo, no_code
- `task_id`: stable 1–80 character identifier (`A–Z`, `a–z`, digits, `.`, `_`, `-`; starts
  alphanumeric). Reuse across segments of the same task; it is not the session or record id.

These three fields are how the report tells problem kinds apart. Pick them from what the task
really needed, not from what Contexer happened to return.

**key_facts**: the facts the outcome actually depended on, usually one to five.
- `source`: contexer, code, docs, git_history, tool_output, user, model_knowledge, web
- `fact_id`: unique within this record, such as `fact-1`; used by `action.material_fact_ids`.
- `knowledge_category`: rationale, intent, exception, migration_future_state, ownership_boundary,
  business_constraint, rejected_alternative, incident_lesson, authority, lifecycle_supersession,
  implementation_fact, other.
- `action_change`: no_change, minor_change, material_change, prevented_wrong_action, uncertain.
  This describes the fact's Contexer-attributed contribution, not whether the task changed code.
- `alternative_sources`: nonempty list from the source vocabulary below.
- `code_would_reveal`: current_code_would_reveal, current_code_suggests_but_does_not_prove,
  repo_docs_would_reveal, git_or_pr_history_would_reveal, external_docs_required,
  human_or_organizational_knowledge_only, unknown.
- All five `could_*_reveal` flags below are required. Use `uncertain` when not checked.
  The recovery label and flags must agree. Human/organizational-only requires all five to be no;
  a code suggestion cannot be marked reliably code-recoverable.
- `contexer_ids`: decision ids that supplied it, or `[]`; required when `source` is `contexer`.
  Every id must also appear in `contexer_items`.

**contexer_items**: every decision Contexer surfaced in this segment that you noticed,
including auto-fetched blocks and session-start rules you relied on or were steered by.
- `surfaced_by`: session_start, autofetch, get_context, review_pending, other_tool
- `relevance`:
  - `decisive`: without it the outcome would have been wrong or blocked
  - `helpful`: it saved real steps or prevented a detour
  - `redundant`: correct, but you already had it or got it just as fast elsewhere
  - `irrelevant`: unrelated to the task
  - `misleading`: stale or wrong, and it cost time or pointed the wrong way
- `created_this_session`: true when this session stored it; must be true for any id in
  `captures` or in the observer's session capture ids, including earlier reviewed turns.
  Items `surfaced_by` autofetch or get_context must appear in that output in the
  transcript when the host records it.
- `note`: required for decisive/helpful; name the step it changed

**gaps**
- `kind`: missing_decision (never stored), stale_decision (stored but outdated),
  retrieval_miss (stored but not surfaced when needed), noise (irrelevant context crowding the
  task), wrong_capture (stored something incorrect), tool_error
- `need`: what was needed

**captures**: decisions stored this segment; `kind` is decision, constraint, convention,
comprehension, correction.

**verdict**
- `contexer_effect`: decisive, helpful, neutral, harmful, not_used. `decisive`/`helpful`
  needs at least one matching item not created this session; `decisive` also needs a key fact
  linked to a decisive item not created in this session. `not_used` is rejected when the transcript shows Contexer calls or
  auto-fetched blocks; use `neutral` when they changed nothing.
- `without_contexer`: what a session without Contexer would realistically have produced:
  same, slower, worse, failed, unknown
- `confidence`: low, medium, high
- `evidence`: the ids, files or steps the verdict rests on

**llm_did_better**: a sentence when code reading or model knowledge beat Contexer, else `null`.

**feature_ratings**: one row per Contexer feature that appeared in this segment, whether or not
it helped. Features: session_start_rules, autofetch, get_context, update_context,
capture_reminder, prompt_constraint_capture, review_pending, review_nudge, bootstrap, guard,
team_context, console_ui, other.
Duplicate feature rows are rejected. Features the observer directly saw must be rated.
- `verdict`: `useful` (it changed what you did for the better), `not_useful` (appeared, changed
  nothing), `noise` (cost attention, tokens or a detour for nothing), `harmful` (wrong or
  misleading)
- `note`: what it did in this session

**improvements**: ideas this session actually motivated; `[]` when none.
- `kind`: `new_feature`, `improve_feature`, `remove_or_reduce` (a feature that is not worth its cost)
- `area`: one of the feature names above
- `idea`: the change, one sentence
- `evidence`: the moment in this session that prompted it. An idea with no such moment does not
  belong here.

## Observation coverage

The observer version is `observe-v6`; new records use `contexer-effectiveness/v3`.
Valid v2 records remain readable without rewriting them. Their missing v3 assessments remain
unknown; old `code_would_reveal=no` is not mapped to any new recovery flag. Unsupported v1
records remain on disk and are reported as skipped. Schema and observer versions are separate.
Missing transcripts produce unknown (`null`) measurements. Codex `exec`/`js` wrappers can hide
inner calls: their segments have `contexer_calls_complete=false`, a null total call count and
null exhaustive result ids. `contexer_observed_call_count` and `contexer_calls` retain direct
calls only. `captured_ids` and `session_captured_ids` retain positively observed captures;
`capture_ids_complete=false` means absence is not proof that a decision was never captured.
`ids_verified=false` marks judgments whose cited ids cannot all be checked from the transcript.
Tool arguments retain field names only, and known credential shapes in script metadata are redacted.

Observer v5 verifies tool-result ids against the claimed surfacing method. Global retrieval
and capture tools are rated under `get_context` and `update_context`, respectively. Older
observations without per-method ids cannot establish method-specific provenance.

## Opportunity and action assessments

These are agent assessments, never observer measurements. All object fields are required;
use the explicit uncertainty enums instead of omitting a field or using null.

**Shared source vocabulary:** current_code, repo_docs, git_history, pr_history, issue_history,
external_docs, conversation_only, no_reasonable_alternative, uncertain. Lists must be unique;
`uncertain` cannot be combined with known alternatives. These describe plausible recovery,
not necessarily the source that was actually used.

**Recovery flags**, each yes / no / uncertain:
`could_current_code_reveal`, `could_repo_docs_reveal`, `could_git_history_reveal`,
`could_pr_history_reveal`, `could_external_docs_reveal`. “Reveal” means reliable recovery of
what the task needed, not a guess that happened to match. Inspect available evidence; do not
claim to have inspected unavailable history or docs.

**opportunity**
- `non_code_context_opportunity`: yes / no / uncertain. Yes means correct engineering choice
  plausibly needs intent not safely inferred from current code alone. Docs-recoverable intent
  can still be an opportunity; it is not unique Contexer knowledge.
- `opportunity_type`: list of fact categories above except implementation_fact; nonempty for
  yes, empty for no. For uncertain, name candidate categories or use an empty list.
- `opportunity_reason`: evidence for this assessment, at most 200 characters.
- `required_fact_location`: nonempty shared-source list.
- All five recovery flags; opportunity=yes cannot have current-code recovery=yes.
- `relevant_knowledge_existed`: yes / no / uncertain, specifically relevant prior Contexer
  knowledge. A retrieval miss alone does not establish no stored knowledge.
- `knowledge_surfaced`: yes / no / uncertain; yes requires knowledge-existed=yes.
- `knowledge_evidence`: at most 200 characters supporting existence/surfacing or uncertainty.

**action**
- `contexer_used`: yes / no / uncertain, including received injections and captures. No must
  not contradict visible calls, surfaced items or captures. A call can coexist with no_change.
- `action_change`: no_change, minor_change, material_change, prevented_wrong_action, uncertain.
- `alternative_sources`: nonempty shared-source list.
- `impact_class`: convenience, reduced_rediscovery, avoided_rework, avoided_architecture_drift,
  avoided_policy_violation, prevented_potentially_consequential_mistake, uncertain.
  For harmful changes with no avoided impact, use uncertain and explain the harm in evidence.
- `severity`: low, medium, high, unknown. Low is convenience or a small local choice. Medium
  implies likely rework, review churn or architectural inconsistency. High could affect
  production, security/compliance, data integrity, major boundaries or costly rollback.
  These are assessed stakes, not measured damages or dollars. Use unknown when unsupported.
- `action_before`, `action_after`, `evidence`: nonempty text, each at most 200 characters.
- `material_fact_ids`: unique references to key_facts; empty unless action is material or
  prevented-wrong-action. Those actions require at least one linked material Contexer fact
  citing prior surfaced knowledge, contexer_used=yes, and surfaced=yes for an opportunity.
  Same-session captures cannot support material action credit, including neutral verdicts.

An empty feature_ratings list is allowed when no feature appeared; observed features still
must be rated. A yes used assessment requires feature ratings. Missing and invalid are not
synonyms for uncertain: invalid records are rejected, while valid uncertain assessments stay
visible in report denominators.

## Optional secondary review: contexer-secondary/v1

Secondary data is separate from immutable primary records. See the staged commands in
[README.md](README.md). It is not required for every record.

First, submit a blind assessment (synthetic example):

```json
{
  "secondary_reviewer_type": "independent_agent",
  "reviewer_id": "reviewer-2",
  "reviewer_session_id": "separate-review-session",
  "blinding": "blind",
  "secondary_required_facts": [
    {
      "fact": "The temporary compatibility exception remains active",
      "available_from": ["conversation_only"],
      "current_repo_recoverable": "no"
    }
  ],
  "secondary_code_recoverable_judgment": "no",
  "secondary_notes": "Implementation alone does not establish the intended expiry."
}
```

Reviewer type is human or independent_agent. An independent agent must declare a nonempty
session id different from the coding session; a human uses null. Reviewer id is bounded text.
Blinding is blind, partial or unblinded. Required facts is a nonempty list, sources use the
shared vocabulary, and recoverability judgments are yes/no/uncertain. Assess current repo
recoverability with its existing docs; identify code versus docs in available_from.

After sealing that assessment and receiving attributed evidence, submit:

```json
{
  "secondary_helpful_judgment": "yes",
  "secondary_notes": "The prior exception prevented an unsupported removal.",
  "later_outcome_corroborated": "uncertain",
  "later_outcome_evidence": "No independently assessed later outcome was collected."
}
```

Both judgments use yes/no/uncertain. Notes and later evidence are nonempty text of at most
300 characters; fact text and identifiers use 200. Later yes needs concrete independent
follow-up evidence relevant to the claimed benefit, not simply CI/merge/no-revert. If not
available by completion, use uncertain. A later reviewer may prepare another case with new
evidence; prior completed reviews remain immutable and conflicting judgments stay unknown.

The tool records case/record identifiers, source-record digest, chronological seal/reveal/
completion timestamps, declared independence and status: prepared → blind_complete →
attribution_revealed → completed. Only completed, valid, matching-source rows enter reports.
Reviewer identity and real-world independence are declarations, not externally verified facts.
Partial/unblinded reviews appear in coverage and agreement; the benefit funnel accepts only
reviews declaring blind first-stage assessment. Multiple conflicting blind reviews yield
uncertain. Unknown earlier follow-up assessments are not automatically superseded by later yes.

Observer v6 adds positive Git-result commit SHAs and a fixed `segment.to_offset`. These permit
matching temporary committer identities when results are visible and keep retries scoped to the
original segment. A branch-only PR candidate without reviewed commit membership is `unverified`
and maps to unknown outcome status. Missing or opaque result evidence cannot establish an
alternate committer identity; a command string alone is insufficient.
