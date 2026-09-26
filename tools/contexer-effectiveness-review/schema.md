# Judgment schema (contexer-effectiveness/v2)

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
    "scope": "single_file"
  },
  "key_facts": [
    {"fact": "The expiry check compared seconds with milliseconds", "source": "code",
     "code_would_reveal": "yes_quickly", "contexer_ids": []}
  ],
  "contexer_items": [
    {"id": "a1b2c3d4", "surfaced_by": "autofetch", "relevance": "irrelevant",
     "created_this_session": false, "note": "An API naming rule did not affect this comparison"},
    {"id": "c0ffee01", "surfaced_by": "other_tool", "relevance": "redundant",
     "created_this_session": true, "note": "Recorded the unit convention after the fix"}
  ],
  "gaps": [{"kind": "noise", "need": "Retrieval returned an unrelated API naming rule"}],
  "captures": [{"id": "c0ffee01", "kind": "convention"}],
  "feature_ratings": [
    {"feature": "autofetch", "verdict": "not_useful", "note": "The naming rule changed no step"},
    {"feature": "get_context", "verdict": "not_useful", "note": "No stored rule explained the unit mismatch"},
    {"feature": "update_context", "verdict": "not_useful", "note": "Saved a future convention after solving the task"}
  ],
  "improvements": [
    {"kind": "improve_feature", "area": "autofetch",
     "idea": "Reduce unrelated API naming matches for expiry questions",
     "evidence": "The retrieved naming rule did not apply to the expiry comparison"}
  ],
  "verdict": {
    "contexer_effect": "neutral", "without_contexer": "same", "confidence": "high",
    "evidence": "Reading the comparison and its test revealed the unit mismatch"
  },
  "llm_did_better": "The implementation and test supplied the deciding fact"
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

These three fields are how the report tells problem kinds apart. Pick them from what the task
really needed, not from what Contexer happened to return.

**key_facts**: the facts the outcome actually depended on, usually one to five.
- `source`: contexer, code, docs, git_history, tool_output, user, model_knowledge, web
- `code_would_reveal`: could reading code, docs or git history have given it?
  `yes_quickly` (a read or two), `yes_slowly` (real exploration), `no`, `unknown`
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

The observer version is `observe-v4`; judgment records remain `contexer-effectiveness/v2`.
Missing transcripts produce unknown (`null`) measurements. Codex `exec`/`js` wrappers can hide
inner calls: their segments have `contexer_calls_complete=false`, a null total call count and
null exhaustive result ids. `contexer_observed_call_count` and `contexer_calls` retain direct
calls only. `captured_ids` and `session_captured_ids` retain positively observed captures;
`capture_ids_complete=false` means absence is not proof that a decision was never captured.
`ids_verified=false` marks judgments whose cited ids cannot all be checked from the transcript.
Tool arguments retain field names only, and known credential shapes in script metadata are redacted.
