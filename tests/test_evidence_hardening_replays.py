"""The evidence-capture hardening replay corpus (hardening Task 01).

One deterministic corpus that does two jobs at once: it REPRODUCES the seven outstanding
issues so a later task cannot claim a fix it did not make, and it FREEZES the safety
properties those fixes must not break.

Every scenario lives as a JSON document under `tests/fixtures/evidence_hardening/`, with
fixed event ids (uuid5, never uuid4) and fixed timestamps, so a golden can never depend on
the wall clock, the filesystem's listing order, a locale or a timezone. `_load` is the
test-local loader the brief asks for - it returns validated events, the read-only decision
projection and the expected output - and it is deliberately NOT a production abstraction:
nothing under `contexer/` imports it, and nothing should.

Reading the corpus:

* scenarios 1-9 and 18 are PURE aggregation replays, checked against a golden projection;
* scenarios 10-13 are crash windows and concurrency, which are sequences rather than
  documents, so the fixture supplies the corpus and the replay drives the spool directly;
* scenarios 14-15 are the two 1,000-statement corpora, generated from a spec so the fixture
  stays readable while staying just as fixed;
* scenario 16 is the four Teams server shapes a lifecycle push can meet;
* scenario 17 is a damaged spool.

The gap tests carry `pytest.mark.xfail(strict=True)` naming their outstanding issue and the
exact failure observed today. A later task REMOVES its marker rather than adding a second
test beside it. Outstanding issue 4 (aggregation cost) is deliberately not among them: it is
a performance ceiling, and a wall-clock xfail would be flaky in both directions, so it is
represented by the `perf`-marked baselines at the bottom of this file and gated for real by
Task 06.
"""
import builtins
import io
import json
import os
import random
import shutil
import statistics
import time
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from contexer import (
    candidates,
    cli,
    evidence,
    guard_engine,
    lifecycle,
    policy,
    reconcile,
    remote,
    revisions,
    spool,
    store,
)
from contexer.adapters import claude, cursor, gemini

FIXTURES = Path(__file__).parent / "fixtures" / "evidence_hardening"

# The generated-client rule the brief pins the corpus to, and the decision it restates.
RULE = ("Do not edit src/generated/client.ts directly. "
        "Change openapi/schema.yaml and regenerate.")
STORED = ("Do not edit the generated client directly. "
          "Change the openapi schema and regenerate.")
GENERATED = "src/generated/client.ts"

# The projection a golden is written against: everything a reviewer would look at, and
# nothing that varies between runs.
_PROJECT = ("kind", "title", "subtype", "score", "target_decision_id", "source_files",
            "uncertainties")

_T0 = datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc)
_GEN_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL,
                            "https://contexer.dev/tests/evidence-hardening")


# ── the fixture loader (test-local, by the brief's instruction) ──────────────────

def _validated(raw: dict) -> dict:
    """One fixture event through the REAL schema gate.

    A corpus built out of hand-written dicts that the validator would reject is a corpus of
    fictions, so every event is normalized here exactly as `spool.append_evidence` would
    normalize it on the way in.
    """
    normalized, errors = evidence.validate_event(raw)
    assert not errors, f"fixture event is not schema-valid: {errors}"
    return normalized


def _generated(spec: dict) -> list[dict]:
    """The 1,000-statement corpora, expanded from their spec.

    Deterministic in exactly the way a literal fixture is: the id is a uuid5 over the
    template and the index, and the timestamp is `_T0` plus that many seconds.
    """
    template, count, kind = spec["template"], spec["count"], spec["kind"]
    return [_validated({
        "schema_version": evidence.SCHEMA_VERSION,
        "event_id": str(uuid.uuid5(_GEN_NAMESPACE, f"{template}#{i}")),
        "session_id": "sess-a", "repo_key": "/repo", "kind": kind,
        "occurred_at": (_T0 + timedelta(seconds=i)).isoformat(),
        "source": "replay", "summary": template.format(i=i), "files": [],
        "content_hash": None, "attributes": {},
    }) for i in range(count)]


def _load(name: str) -> dict:
    """One replay scenario: normalized events, the decision projection, and the golden.

    Kept test-local on purpose. A production fixture abstraction would be a second reader of
    the evidence schema, and the whole value of this corpus is that it goes through the one
    gate every host already goes through.
    """
    doc = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    doc["events"] = (_generated(doc["generate"]) if doc.get("generate")
                     else [_validated(e) for e in doc.get("events", [])])
    doc.setdefault("decisions", [])
    return doc


def _project(candidate: dict) -> dict:
    return {key: candidate[key] for key in _PROJECT}


def _aggregate(doc: dict) -> list[dict]:
    result = candidates.aggregate_candidates(doc["events"], doc["decisions"])
    return [_project(c) for c in result["candidates"]]


def _scenarios() -> list[str]:
    return sorted(p.stem for p in FIXTURES.glob("*.json"))


# ── the corpus itself ────────────────────────────────────────────────────────────

def test_the_corpus_covers_every_listed_scenario():
    """Eighteen scenarios, numbered by the brief's own list. A missing file is a hole in the
    corpus rather than a quietly smaller test run."""
    assert len(_scenarios()) == 18
    assert [name[:2] for name in _scenarios()] == [f"{i:02d}" for i in range(1, 19)]


def test_every_fixture_event_is_schema_valid_and_deterministic():
    """`_load` asserts validity per event; this asserts the OTHER half - no wall clock, no
    random ids, so the same file always replays to the same golden."""
    for name in _scenarios():
        doc = _load(name)
        for event in doc["events"]:
            assert uuid.UUID(event["event_id"]).version == 5, name
            assert event["occurred_at"].endswith("+00:00"), name
        assert _load(name)["events"] == doc["events"], name


@pytest.mark.parametrize("name", [
    "01-directive-then-edit",
    "03-unrelated-edit-before-directive",
    "04-directive-duplicates-live-decision",
    "05-directive-repeated-in-a-second-session",
    "06-agent-conclusion-with-rationale",
    "07-ignored-decision-restated-by-a-directive",
    "08-tombstoned-decision-restated-by-a-directive",
    # 10-13 and 17 drive the spool procedurally further down, but their goldens are asserted
    # here too: an unasserted golden in the corpus that is supposed to BE the source of truth
    # rots silently, and these are the corpora those replays are built on.
    "10-crash-after-hold-before-store-mutation",
    "11-crash-after-store-mutation-before-hold",
    "12-crash-after-summary-before-evidence-deletion",
    "13-two-simultaneous-reconciliations",
    "17-corrupt-pending-event-and-held-manifest",
    "18-wrong-session-and-wrong-repo-evidence",
])
def test_pure_aggregation_replay_matches_its_golden(name):
    doc = _load(name)
    assert _aggregate(doc) == doc["expected"], doc["summary"]


def test_every_fixture_golden_is_either_asserted_or_absent():
    """The other half of the rule above. A fixture carries a golden only if some test reads
    it, so the corpus can never accumulate a stale expectation nobody checks. The two gap
    fixtures are the exception in both directions: 02 carries the DESIRED output (its xfail
    reads it), and 09 carries none at all, because the shape Task 04 lands on is that task's
    design call and pre-judging it here would be a golden written by the wrong author."""
    asserted = set(
        test_pure_aggregation_replay_matches_its_golden.pytestmark[0].args[1])
    for name in _scenarios():
        doc = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
        if doc.get("expected") is not None:
            assert name in asserted or name == "02-edit-then-directive", name


def test_the_directive_and_its_edit_become_one_candidate():
    """Scenario 1 spelled out, because it is the shape every later scenario is measured
    against: the directive seeds the candidate, the edit that follows anchors it."""
    ((candidate,),) = (_aggregate(_load("01-directive-then-edit")),)
    assert candidate["kind"] == "new"
    assert candidate["source_files"] == [GENERATED]
    assert candidate["score"] == 60          # 50 directive + 10 files changed


def test_an_unrelated_edit_before_the_directive_never_anchors_it():
    """Scenario 3, and the guard rail on the scenario-2 fix: making a BACKWARD edit
    corroborate a directive must not make EVERY backward edit corroborate it. A README typo
    a minute earlier is not evidence for the generated-client rule, and an anchor is
    guard-and-staleness input, so a wrong one is worse than none."""
    directive, leftover = _aggregate(_load("03-unrelated-edit-before-directive"))
    assert directive["kind"] == "new" and directive["source_files"] == []
    assert leftover["kind"] == "insufficient" and leftover["source_files"] == ["README.md"]


def test_a_restated_inactive_decision_opens_a_reconsideration_on_that_decision():
    """Scenarios 7 and 8 at the AGGREGATOR. A restatement of an ignored or retired decision is
    neither a duplicate of it nor a fresh decision beside it: it names the ORIGINAL decision
    and the revision the question is asked against, which is what lets one continuous identity
    carry the revisions, the retirement record and the reconsideration receipts together.

    This asserted `new` with a note until Task 04's lane existed - the aggregator's half of
    outstanding issue 7 was correct as far as it went, and stopped one step short of a lane
    anything downstream could route."""
    for name, dead_id, state in (
            ("07-ignored-decision-restated-by-a-directive", "dec-ignored", "ignored"),
            ("08-tombstoned-decision-restated-by-a-directive", "dec-retired", "retired")):
        doc = _load(name)
        (candidate,) = candidates.aggregate_candidates(doc["events"],
                                                       doc["decisions"])["candidates"]
        assert candidate["kind"] == "reconsider", name
        assert candidate["target_decision_id"] == dead_id
        assert candidate["target_state"] == state
        assert candidate["basis_revision_id"] == f"rev-{dead_id}"
        assert any(dead_id in note for note in candidate["uncertainties"]), name


def test_agent_only_evidence_cannot_reopen_an_inactive_decision():
    """Scenario 9: the SAME inactive decision as scenarios 7 and 8, named only by an agent.

    This is scenario 7's event with `kind` flipped to `agent_conclusion`, which is the only
    way the scenario means anything - a conclusion whose wording does not restate the ignored
    decision leaves the `decisions` array inert and measures nothing.

    The property, and the one Task 04's lane is designed on: only a developer saying it out
    loud reopens an inactive decision. Task 04 answered the open question this test left with
    `new` and NO note naming the decision - a pointer at the inactive decision would be the
    reopening itself, written in prose by nobody - so the assertion below is unchanged and the
    xfail marker it carried is gone.
    """
    doc = _load("09-inactive-decision-mentioned-by-a-conclusion")
    (candidate,) = _aggregate(doc)
    reopens = ((candidate["kind"] in ("new", "reconsider"))
               and any("dec-ignored" in note for note in candidate["uncertainties"]))
    assert not reopens, (
        f"agent-only evidence reopened dec-ignored: {candidate['kind']} at "
        f"{candidate['score']} - {candidate['uncertainties']}")
    assert candidate["target_decision_id"] is None


def test_a_human_directive_is_what_reopens_an_inactive_decision():
    """The passing side of the distinction above, so it is measured on both ends rather than
    asserted on one. The identical restatement carried by a `user_directive` scores 50 and
    opens a reconsideration of the ignored decision - which is correct and must stay."""
    (directive,) = _aggregate(_load("07-ignored-decision-restated-by-a-directive"))
    (conclusion,) = _aggregate(_load("09-inactive-decision-mentioned-by-a-conclusion"))
    assert directive["score"] == 50 and conclusion["score"] == 25
    assert any("dec-ignored" in note for note in directive["uncertainties"])


def test_evidence_from_another_session_never_corroborates_the_directive():
    """Scenario 18, first half. `_attach_target` gates on the session before anything else,
    so an edit made in a different session lands in its own leftover set instead of anchoring
    somebody else's rule."""
    directive, leftover = _aggregate(_load("18-wrong-session-and-wrong-repo-evidence"))
    assert directive["source_files"] == []
    assert "session sess-b" in " ".join(leftover["uncertainties"])


def test_another_repositorys_evidence_never_enters_this_repos_candidates(tmp_repo, tmp_path):
    """Scenario 18, second half. Both repos hold real evidence, and reconciling repo A must
    produce a decision built ONLY from repo A's events.

    The isolation comes from the per-repo spool DIRECTORY, and from nothing else - see the
    documented limitation below. So the test seeds both spools and follows the events through
    to the decision, rather than asserting that a file written to one directory is in it.
    """
    doc = _load("18-wrong-session-and-wrong-repo-evidence")
    other_repo = str(tmp_path / "other-repo")
    ours = doc["events"][0]
    theirs = doc["foreign_repo_event"]
    assert spool.append_evidence(tmp_repo, ours)["status"] == "stored"
    assert spool.append_evidence(other_repo, theirs)["status"] == "stored"

    assert reconcile.reconcile_session(tmp_repo)["proposed"] == 1

    ((_candidate_id, meta),) = spool.held_candidates(tmp_repo).items()
    assert meta["event_ids"] == [ours["event_id"]]
    assert theirs["event_id"] not in meta["event_ids"]
    assert [e["event_id"] for e in spool.list_pending_evidence(other_repo)] \
        == [theirs["event_id"]], "reconciling repo A consumed repo B's evidence"


def test_repo_key_is_not_a_guard_and_the_corpus_says_so(tmp_repo):
    """The limitation behind the isolation above, stated out loud rather than left implied.

    `spool.append_evidence` keys the spool by its `repo_path` ARGUMENT and never reads the
    event's own `repo_key`; the field appears nowhere in `candidates.py` or `reconcile.py`
    either. So an event carrying another repo's `repo_key` that lands in this repo's
    `pending/` - through a worktree or slug resolution change, say - is aggregated and
    proposed into this repo's store like any other.

    Asserted as the current TRUTH, not as an xfail: nobody has ruled that `repo_key` should
    become a guard, and inventing an xfail nobody owns would be a requirement written by a
    test. If a later task does add the check, this test is what tells them the corpus assumed
    the old behaviour.
    """
    foreign = _load("18-wrong-session-and-wrong-repo-evidence")["foreign_repo_event"]
    assert foreign["repo_key"] != tmp_repo
    spool.append_evidence(tmp_repo, foreign)

    assert reconcile.reconcile_session(tmp_repo)["proposed"] == 1
    assert len(store.load(tmp_repo)["entries"]) == 1


# ── the reproduced gaps ──────────────────────────────────────────────────────────

def test_an_edit_before_its_directive_corroborates_it():
    """Outstanding issue 6, fixed by Task 03's two-pass grouping plus the STRUCTURAL link.

    The directive names `src/generated/client.ts` in its own text, so the regeneration recorded
    a minute earlier is linked by the rule's own words rather than by which of the two the
    developer happened to do first. Scenario 3 is the guard rail on the other side: an edit
    with no such link still anchors nothing.
    """
    doc = _load("02-edit-then-directive")
    assert _aggregate(doc) == doc["expected"], doc["summary"]


def test_a_repeated_prohibition_corroborates_itself_rather_than_contradicting():
    """Scenario 5's real-world shape, which the fixture deliberately avoids.

    Scenario 5 uses an affirmative sentence so it can measure the repetition signal at all;
    this is the same replay with the corpus's own prohibition, which is how a developer
    actually repeats a rule ("never commit directly to main"). It matters beyond scenario 5:
    outstanding issue 3's fix re-emits a deduped restatement as corroboration, and for every
    prohibition-shaped rule that corroboration would subtract 30 instead of adding.
    """
    doc = _load("05-directive-repeated-in-a-second-session")
    first, second = doc["events"]
    prohibition = [first | {"summary": RULE}, second | {"summary": RULE}]

    (candidate,) = [_project(c) for c in
                    candidates.aggregate_candidates(prohibition, [])["candidates"]]

    assert candidate["kind"] == "new", candidate["uncertainties"]
    assert candidate["score"] == 65, "the repetition subtracted instead of corroborating"


def test_a_repeated_directive_leaves_an_evidence_trail(tmp_repo):
    """Outstanding issue 3, fixed by Task 03's recurrence meta.

    The store still stores nothing the second time - a restated rule is not a second decision -
    but it now RECORDS the repetition and says so, so the wrapper spools the event that used to
    vanish. What the second event becomes downstream is asserted in
    `test_a_repeated_directive_records_history_without_a_second_decision`.
    """
    first = evidence.capture_directive(tmp_repo, "always use conventional commits",
                                       "sess-a", "replay")
    assert first[0] is not None
    evidence.capture_directive(tmp_repo, "always use conventional commits",
                               "sess-b", "replay")
    assert len(spool.list_pending_evidence(tmp_repo)) == 2


def test_a_repeated_directive_records_history_without_a_second_decision(tmp_repo):
    """The other half of the same fix, and the property that keeps it from becoming spam.

    A repetition is recurrence HISTORY on the decision the developer already has: the count and
    the distinct sessions move, the bounded history gains a row naming how the match was made,
    and nothing else does - no second entry, no status change, no new pending review. The
    spooled event settles against that same decision as a duplicate rather than opening one.
    """
    evidence.capture_directive(tmp_repo, "always use conventional commits", "sess-a", "replay")
    reconcile.reconcile_session(tmp_repo)
    (entry,) = store.load(tmp_repo)["entries"]
    before = store.entry_status(entry)

    entry_id, content, _status = evidence.capture_directive(
        tmp_repo, "always use conventional commits", "sess-b", "claude_user_prompt")
    assert (entry_id, content) == (None, None), "a restatement must not store a second rule"

    (again,) = store.load(tmp_repo)["entries"]
    assert again["id"] == entry["id"] and store.entry_status(again) == before
    assert again["occurrence_count"] == 2 and again["session_ids"] == ["sess-a", "sess-b"]
    ((row,),) = (again["recurrences"],)
    assert (row["session_id"], row["match_kind"], row["source"], row["count"]) \
        == ("sess-b", "overlap", "claude_user_prompt", 1)
    assert row["overlap"] == 1.0

    receipt = reconcile.reconcile_session(tmp_repo)

    assert receipt["proposed"] == 0 and receipt["duplicates"] == 1
    assert len(store.load(tmp_repo)["entries"]) == 1


def test_repeating_a_rule_inside_one_session_bumps_a_count_not_a_row(tmp_repo):
    """The bound that makes the history readable: a developer who restates a rule six times in
    one session must not evict five other sessions from a 20-row window."""
    for _ in range(6):
        evidence.capture_directive(tmp_repo, "always use conventional commits",
                                   "sess-a", "replay")
    ((row,),) = (store.load(tmp_repo)["entries"][0]["recurrences"],)
    assert (row["session_id"], row["count"]) == ("sess-a", 5)


def test_a_recurrence_from_another_session_shows_in_review_without_approving_anything(
        tmp_repo, monkeypatch):
    """Corroboration is visible to the developer reviewing the decision, and is exactly that -
    visible. Repetition never approves, and never arms a policy."""
    with monkeypatch.context() as no_edits:
        no_edits.setattr(store, "_read_edited_files", lambda *_a, **_k: [])
        evidence.capture_directive(tmp_repo, "always tag this release before pushing",
                                   "sess-a", "replay")
        evidence.capture_directive(tmp_repo, "always tag this release before pushing",
                                   "sess-b", "replay")

    (entry,) = store.load(tmp_repo)["entries"]
    assert store.entry_status(entry) == "pending_approval", "deictic: still awaiting review"
    assert not entry.get("approved_by")
    rows = dict(cli._review_metadata(tmp_repo, entry, cli._review_git_budget()))
    assert rows["Seen"] == "2 times across 2 sessions"


def test_some_production_module_emits_an_agent_conclusion():
    """Outstanding issue 2, closed: `record_agent_conclusion` is a real MCP tool, so the kind
    is reachable in a live session instead of existing only in the schema and the scoring
    table. The xfail marker it carried is gone."""
    owners = {"evidence.py", "candidates.py"}
    emitters = [path.name for path in sorted(Path(store.__file__).parent.rglob("*.py"))
                if path.name not in owners
                and "agent_conclusion" in path.read_text(encoding="utf-8")]
    assert emitters, "no module outside the schema and the scoring table names the kind"


# ── scenario 6 through the real emitter (outstanding issue 2) ───────────────────

CONCLUSION = "The generated client is rebuilt from openapi/schema.yaml by the codegen step"
WHY = "A hand edit is overwritten on the next build."


def test_a_bare_agent_conclusion_stays_below_the_review_bar(tmp_repo):
    """The emitter is a door, not a promotion. An unexplained conclusion scores 15 against a
    bar of 25, so it is recorded, kept, and proposed to nobody."""
    assert evidence.record_agent_conclusion(tmp_repo, CONCLUSION, session_id="sess-a")[0]

    receipt = reconcile.reconcile_session(tmp_repo)

    assert (receipt["proposed"], receipt["insufficient"]) == (0, 1)
    assert store.load(tmp_repo)["entries"] == []


def test_an_explained_conclusion_is_proposed_for_review_and_nothing_more(tmp_repo):
    """Scenario 6's shape, produced by the production emitter rather than a fixture: it
    reaches the bar on its own, and reaching the bar means PENDING - never trusted, never
    approved, never anchored."""
    ok, message = evidence.record_agent_conclusion(tmp_repo, CONCLUSION, rationale=WHY,
                                                   session_id="sess-a")
    assert ok and "review" in message

    receipt = reconcile.reconcile_session(tmp_repo)

    assert receipt["proposed"] == 1
    (entry,) = store.load(tmp_repo)["entries"]
    assert store.entry_status(entry) == "pending_approval"
    assert not entry.get("approved_by")
    assert not entry.get("source_files"), "a reported conclusion never anchors a file"
    assert not entry.get("guard_check"), "and never arms a policy"


def test_a_bare_conclusion_plus_an_observed_edit_reaches_the_bar(tmp_repo):
    """The half the coverage block is honest about: what the agent SAYS is 15, and what a
    hook actually OBSERVED is the other 10. Structural evidence is what promotes it."""
    evidence.record_agent_conclusion(tmp_repo, CONCLUSION, files=[GENERATED],
                                     session_id="sess-a")
    evidence.emit_hook_event(tmp_repo, "file_changed", session_id="sess-a",
                             source="post_tool_use", files=[GENERATED])

    receipt = reconcile.reconcile_session(tmp_repo)

    assert (receipt["proposed"], receipt["insufficient"]) == (1, 0)
    (entry,) = store.load(tmp_repo)["entries"]
    assert store.entry_status(entry) == "pending_approval"


def test_a_capture_the_lint_bounced_cannot_re_enter_as_evidence(tmp_repo):
    """The bypass this door would otherwise open. `capture_lint` guards the two write tools;
    reconciliation materializes through `store.update_decision`, which is NOT linted - so a
    bounced narrative re-submitted here would land pending review in exactly the shape the
    lint exists to reshape. It is refused at the emitter, and the bounce names THIS tool."""
    narrative = ("Investigated the flaky pairing test for two hours today. "
                 + "Then I read the guard engine and traced every caller. " * 8)
    assert store.capture_lint(narrative, created_by="ai"), "precondition: update_context bounces it"

    ok, message = evidence.record_agent_conclusion(tmp_repo, narrative, session_id="sess-a")

    assert not ok
    assert "record_agent_conclusion" in message and "update_context" not in message
    assert spool.list_pending_evidence(tmp_repo) == []
    assert reconcile.reconcile_session(tmp_repo)["proposed"] == 0
    assert store.load(tmp_repo)["entries"] == []


def test_the_coverage_block_on_a_pass_says_what_the_host_could_see(tmp_repo):
    """A receipt reports what was found beside what could be found at all. Cursor observes no
    edit, so a pass on a Cursor session says so rather than reporting a quiet session."""
    evidence.record_agent_conclusion(tmp_repo, CONCLUSION, rationale=WHY, session_id="sess-a")

    receipt = reconcile.reconcile_session(tmp_repo, host="cursor")

    assert receipt["coverage"] == {
        "host": "cursor", "user_directives": "captured", "file_changes": "unavailable",
        "assistant_conclusions": "model_reported", "test_results": "unavailable",
        "diffs": "unavailable", "reconciliation": "complete", "dropped_events": 0}
    assert "file changes unavailable" in reconcile.format_receipt(receipt)


@pytest.mark.parametrize("host", ["codex", "cursor", "gemini"])
def test_every_host_reconciles_at_session_start(tmp_repo, host):
    """Outstanding issue 1, closed. Every host's SessionStart traverses the shared store-side
    payload, which reconciles before it reads the store - so a directive left in the spool by
    a session that crashed becomes a decision awaiting review at the NEXT session start,
    whichever host opens it. The xfail marker these three carried is gone.

    Each arm calls what the installed hook calls and nothing else, so a host wired only in
    prose would still fail here.
    """
    evidence.emit_hook_event(tmp_repo, "user_directive", session_id="sess-a",
                             source="replay", summary=RULE)
    raw = json.dumps({"session_id": "sess-a", "source": "startup",
                      "workspace_roots": [tmp_repo]})
    if host == "codex":
        # Codex's installed SessionStart hook calls exactly this, and nothing else.
        store.get_session_start_context(tmp_repo, "startup", "sess-a", "codex")
    elif host == "cursor":
        cursor.session_start(tmp_repo, raw)
    else:
        gemini.session_start(tmp_repo, raw)
    assert [store.entry_status(e) for e in store.load(tmp_repo)["entries"]] \
        == ["pending_approval"]


def test_claude_does_reconcile_at_session_start(tmp_repo):
    """The other side of the parametrized test above, so "every host" is measured against a
    host that already passed rather than against an assumption."""
    evidence.emit_hook_event(tmp_repo, "user_directive", session_id="sess-a",
                             source="replay", summary=RULE)
    claude.sync_memory(tmp_repo)
    assert [store.entry_status(e) for e in store.load(tmp_repo)["entries"]] \
        == ["pending_approval"]


# ── shared session-start reconciliation (outstanding issue 1) ────────────────────

def _spooled_rule(repo: str, session: str = "sess-crashed") -> None:
    """One directive left in the spool by a session that ended without a checkpoint."""
    evidence.emit_hook_event(repo, "user_directive", session_id=session,
                             source="replay", summary=RULE)


def _reconcile_lock_path(repo: str) -> Path:
    return store.STORE_DIR / f".reconcile_{store.repo_slug(repo)}.lock"


def test_evidence_left_by_a_crashed_session_reconciles_at_the_next_session_start(tmp_repo):
    """The whole of outstanding issue 1, stated as the user-visible outcome: session A emits a
    directive and never reaches a checkpoint, session B opens and the developer is asked about
    it. Session B passes a DIFFERENT session id, which is exactly why the shared call scopes
    itself to `""` - a pass scoped to session B would skip session A's evidence forever."""
    _spooled_rule(tmp_repo, "sess-a")
    payload = store.session_start_payload(tmp_repo, "startup", "sess-b", "codex")
    (entry,) = store.load(tmp_repo)["entries"]
    assert store.entry_status(entry) == "pending_approval"
    assert spool.list_pending_evidence(tmp_repo) == []
    # The COUNT reaches this session's own status line, which is only true because the store
    # is read after the pass rather than before it.
    assert "1 decision pending review" in payload["status"]
    # A count, never the candidate's text: the startup payload must not carry raw evidence.
    assert RULE not in payload["context"] and RULE not in payload["status"]


def test_the_newly_proposed_count_comes_from_the_post_reconcile_store(tmp_repo):
    """Sharper than the line above: with a decision already stored, the pending count has to
    move by exactly the number of proposals this pass made. A store read taken BEFORE the pass
    would report the old count and defer the whole queue by one session."""
    store.update_decision(tmp_repo, "Use Postgres for the decision store, not SQLite.",
                          "sess-0", "architecture", created_by="human")
    before = store.session_start_payload(tmp_repo, "startup", "sess-a", "gemini")["status"]
    assert "pending review" not in before

    _spooled_rule(tmp_repo)
    after = store.session_start_payload(tmp_repo, "startup", "sess-b", "gemini")["status"]
    assert "1 decision pending review" in after


@pytest.mark.parametrize("source", ["resume", "compact"])
def test_resume_and_compact_do_not_miss_older_evidence(tmp_repo, source):
    """Both replay paths return early or render differently, and the reconcile call sits AHEAD
    of every one of those branches on purpose: a resumed session's spool holds exactly what a
    fresh one's does, and skipping it would strand that evidence until the next cold start."""
    store.update_decision(tmp_repo, "Use Postgres for the decision store, not SQLite.",
                          "sess-0", "architecture", created_by="human")
    _spooled_rule(tmp_repo)
    store.session_start_payload(tmp_repo, source, "sess-b", "claude")
    assert sorted(store.entry_status(e) for e in store.load(tmp_repo)["entries"]) \
        == ["approved", "pending_approval"]


def test_claude_reaching_reconciliation_twice_produces_one_proposal_and_one_summary(tmp_repo):
    """Claude's SessionStart runs `sync_memory` and THEN the shared payload, so it reconciles
    twice. The duplicate is kept harmless rather than suppressed by a marker: the first pass
    moves the events into the hold, so the second finds no work at all.

    Asserted on the outcome the brief names - one proposal, one held candidate, and one
    evidence summary once the developer approves - rather than on a call count, because a
    second pass that DID run and correctly did nothing is equally acceptable.
    """
    _spooled_rule(tmp_repo)
    claude.sync_memory(tmp_repo)
    payload = store.session_start_payload(tmp_repo, "startup", "sess-b", "claude")

    (entry,) = store.load(tmp_repo)["entries"]
    assert store.entry_status(entry) == "pending_approval"
    assert "1 decision pending review" in payload["status"]
    assert len(spool.held_candidates(tmp_repo)) == 1

    assert store.approve_decision(tmp_repo, entry["id"], "approve")[0]
    reconcile.reconcile_session(tmp_repo)
    (settled,) = store.load(tmp_repo)["entries"]
    summaries = settled.get("evidence_summary") or []
    assert [row["disposition"] for row in summaries] == ["approved"]


def test_gemini_keeps_its_precompress_and_sessionend_checkpoints_idempotent(tmp_repo):
    """Gemini gains SessionStart and KEEPS the two it had. Running all three over one piece of
    evidence must still leave exactly one proposal - the safety checkpoints are a net for a
    session that never starts again, not a second producer."""
    _spooled_rule(tmp_repo)
    raw = json.dumps({"session_id": "sess-a", "source": "startup"})
    gemini.session_start(tmp_repo, raw)
    gemini.pre_compress(tmp_repo, raw)
    gemini.session_end(tmp_repo, raw)
    assert [store.entry_status(e) for e in store.load(tmp_repo)["entries"]] \
        == ["pending_approval"]
    assert len(spool.held_candidates(tmp_repo)) == 1


def test_a_concurrent_session_start_skips_its_pass_without_waiting(tmp_repo):
    """Two sessions opening on one repo at once: the second finds the reconcile flock held,
    skips, and renders its context anyway. Pinned with the lock genuinely held by another file
    descriptor, and with a wall-clock bound, because the failure this guards against is a
    session start BLOCKING on another one rather than one proposing twice."""
    import fcntl
    _spooled_rule(tmp_repo)
    store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    with open(_reconcile_lock_path(tmp_repo), "ab") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        start = time.perf_counter()
        payload = store.session_start_payload(tmp_repo, "startup", "sess-b", "cursor")
        elapsed = time.perf_counter() - start
    assert elapsed < 5.0, "a session start must never wait on another pass's lock"
    assert store.load(tmp_repo)["entries"] == []
    assert spool.list_pending_evidence(tmp_repo) != []
    # Skipped is not incomplete: another pass holding the lock is the design working, and the
    # next checkpoint picks the work up, so the developer is told nothing.
    assert "reconciliation was incomplete" not in payload["status"]


def test_an_incomplete_pass_does_not_break_session_start_and_stays_visible(tmp_repo,
                                                                          monkeypatch):
    """Fail-soft, but never silent. A pass that could not account for its evidence leaves the
    session start intact - rules, counts and all - and says so in the status line, because
    acknowledged evidence that went unaccounted for is exactly what runbook invariant 3
    requires to remain visible."""
    store.update_decision(tmp_repo, "Always run migrations before deploying.", "sess-0",
                          "constraint", created_by="human")
    _spooled_rule(tmp_repo)
    with monkeypatch.context() as patch:
        patch.setattr(spool, "hold_candidate_evidence",
                      lambda *a, **k: {"status": "ok", "missing": ["gone"], "held": []})
        payload = store.session_start_payload(tmp_repo, "startup", "sess-b", "gemini")
    assert "Evidence reconciliation was incomplete" in payload["status"]
    assert "contexer reconcile-session" in payload["status"]
    assert "Always run migrations before deploying." in payload["context"]


def test_the_incomplete_note_survives_the_deliberately_silent_compact_path(tmp_repo,
                                                                          monkeypatch):
    """The fifth return path of `_local_session_start_payload`, and the only one that returns
    an empty status by design: a compaction continuing a session whose developer already
    dismissed the setup offer.

    It is reachable exactly when reconciliation stored no decision, which is what a failed pass
    looks like from here, so it is the branch where dropping the diagnostic would hide it in
    the very case it exists for. The context stays empty either way: the silence that path
    protects is about not re-opening a dismissed picker, not about suppressing a loss report.
    """
    # An empty repo takes the no-decisions branch, which is what arms the offer.
    store.session_start_payload(tmp_repo, "startup", "sess-a", "claude")
    _spooled_rule(tmp_repo)
    with monkeypatch.context() as patch:
        patch.setattr(spool, "hold_candidate_evidence",
                      lambda *a, **k: {"status": "ok", "missing": ["gone"], "held": []})
        payload = store._local_session_start_payload(tmp_repo, "compact", "sess-b", "claude")
    assert payload["context"] == ""
    assert payload["status"].startswith("Evidence reconciliation was incomplete")


def test_a_raising_reconciliation_still_renders_the_session_start(tmp_repo, monkeypatch):
    """The call site is unguarded on `reconcile_session`'s never-raises contract, so this pins
    that contract from the session-start side: even an exception raised inside the pass costs
    the session nothing but the coverage note."""
    store.update_decision(tmp_repo, "Always run migrations before deploying.", "sess-0",
                          "constraint", created_by="human")
    _spooled_rule(tmp_repo)
    with monkeypatch.context() as patch:
        patch.setattr(spool, "held_candidates",
                      lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        payload = store.session_start_payload(tmp_repo, "startup", "sess-b", "codex")
    assert "Evidence reconciliation was incomplete" in payload["status"]
    assert "Always run migrations before deploying." in payload["context"]


def test_an_empty_spool_session_start_does_no_reconciliation_work_at_all(tmp_repo,
                                                                        monkeypatch):
    """The Task 06 latency gate for the case that runs on EVERY session start of EVERY repo.

    Not a wall-clock number: the requirement is structural, so it is measured structurally.
    No store read, no store lock, no reconcile lock FILE and no candidate scan happen while
    the spool holds nothing - the pass returns on two directory listings. Counted against the
    real session-start path, so a future call added above `reconcile_session` would be caught
    here too.
    """
    store.update_decision(tmp_repo, "Always run migrations before deploying.", "sess-0",
                          "constraint", created_by="human")
    # `store.load` and `store.store_lock` are used by the REST of session start too, so the
    # counters only count while the pass itself is on the stack.
    counts = {"aggregate": 0, "load": 0, "lock": 0}
    inside = {"pass": False}
    real_pass, real_load, real_lock = (reconcile.reconcile_session, store.load,
                                       store.store_lock)

    def scoped(name, real):
        def counted(*args, **kwargs):
            if inside["pass"]:
                counts[name] += 1
            return real(*args, **kwargs)
        return counted

    def watched(*args, **kwargs):
        inside["pass"] = True
        try:
            return real_pass(*args, **kwargs)
        finally:
            inside["pass"] = False

    with monkeypatch.context() as patch:
        patch.setattr(reconcile, "reconcile_session", watched)
        patch.setattr(reconcile.candidates, "aggregate_candidates",
                      scoped("aggregate", candidates.aggregate_candidates))
        patch.setattr(store, "load", scoped("load", real_load))
        patch.setattr(store, "store_lock", scoped("lock", real_lock))
        payload = store.session_start_payload(tmp_repo, "startup", "sess-b", "codex")

        assert counts == {"aggregate": 0, "load": 0, "lock": 0}
        assert not _reconcile_lock_path(tmp_repo).exists()
        assert "Always run migrations before deploying." in payload["context"]

        # The instrumentation is not vacuous: spool one directive and every counter fires,
        # so a zero above means the fast path skipped the work rather than that this test
        # patched the wrong names.
        _spooled_rule(tmp_repo)
        store.session_start_payload(tmp_repo, "startup", "sess-c", "codex")
    assert counts["aggregate"] and counts["load"] and counts["lock"]
    assert _reconcile_lock_path(tmp_repo).exists()


def test_the_status_line_says_nothing_when_a_pass_completes(tmp_repo):
    """Silent operation. A pass that reconciled everything it was handed adds no sentence at
    all - what it produced is already visible as the pending-review count."""
    _spooled_rule(tmp_repo)
    status = store.session_start_payload(tmp_repo, "startup", "sess-b", "claude")["status"]
    assert "reconciliation" not in status.lower()


def test_a_directive_restating_an_ignored_decision_is_surfaced_for_review(tmp_repo):
    _replay_inactive_twin(tmp_repo, retire=False)


def test_a_directive_restating_a_retired_decision_is_surfaced_for_review(tmp_repo):
    _replay_inactive_twin(tmp_repo, retire=True)


def test_restating_an_ignored_decision_is_never_absorbed_as_a_recurrence(tmp_repo):
    """The half of issue 7 Task 03 owns, and the only half it owns.

    The store's dedup is status-blind, so a restatement of an IGNORED decision used to bump its
    `occurrence_count` - ranking a decision the developer switched off as increasingly
    corroborated, which is the opposite of what happened, and burying the restatement where no
    lane could ever find it. Nothing is stored and nothing is bumped now; the match is REPORTED
    so Task 04's reconsideration lane has something to act on. The lane itself is not built
    here, and the aggregator's own classification (`new`, with the `restates <id>` note) is
    unchanged - see `test_a_restated_inactive_decision_is_classified_as_new_not_matched_back`.
    """
    ok, entry_id = store.update_decision(tmp_repo, RULE, "sess-0", "constraint",
                                         created_by="human")
    assert ok and store.approve_decision(tmp_repo, entry_id, "ignore")[0]

    stored, new_id, meta = store.update_decision_with_meta(tmp_repo, RULE, "sess-1",
                                                           "constraint", created_by="ai")

    assert (stored, new_id) == (False, None)
    assert meta["inactive_match"]["entry_id"] == entry_id
    assert meta["inactive_match"]["status"] == "ignored"
    (entry,) = store.load(tmp_repo)["entries"]
    assert entry["occurrence_count"] == 1, "an ignored decision must not gain corroboration"
    assert entry.get("recurrences") is None
    assert store.entry_status(entry) == "ignored"


def _inactive_twin(repo: str, *, retire: bool) -> str:
    """One stored rule, made inactive the way the two halves of issue 7 describe."""
    # RULE on both sides, deliberately: the defect is the STORE's own status-blind dedup
    # (`_find_match` / `_tombstoned_match`), which runs on a different tokenizer from the
    # aggregator's, so a paraphrase would land as a fresh entry and quietly test nothing.
    ok, entry_id = store.update_decision(repo, RULE, "sess-0", "constraint",
                                         created_by="human")
    assert ok and entry_id
    if retire:
        assert lifecycle.retire_decision(repo, entry_id, "superseded by the codegen step")[0]
    else:
        assert store.approve_decision(repo, entry_id, "ignore")[0]
    return entry_id


def _inactive_entry(repo: str, entry_id: str) -> dict:
    """The decision, wherever it currently lives - live store or tombstone sidecar."""
    for source in (store.load(repo), store.load_deleted(repo)):
        found = next((e for e in source["entries"] if e.get("id") == entry_id), None)
        if found is not None:
            return found
    raise AssertionError(f"{entry_id} is in neither the live store nor the sidecar")


def _replay_inactive_twin(repo: str, *, retire: bool) -> None:
    """Store the rule, make it inactive, then restate it as session evidence.

    The requirement either way: the restatement REACHES the developer, on the original
    decision's own identity, with its raw evidence still held against the question.

    These two carried `xfail(strict=True)` for outstanding issue 7 until the durable-state
    work's fix round, which stopped `reconcile._finalize` deleting evidence it could file no
    receipt against - the evidence survived, but the restatement was still not surfaced: no
    proposal anywhere and a hold naming no decision, counted as `held_unattributed` forever.
    The reconsideration lane closes that half, so this asserts the surfacing rather than the
    weaker "something survived" property it was written as.
    """
    entry_id = _inactive_twin(repo, retire=retire)
    evidence.emit_hook_event(repo, "user_directive", session_id="sess-a", source="replay",
                             summary=RULE)
    receipt = reconcile.reconcile_session(repo)

    assert receipt["reconsidered"] == 1
    # ONE question, on the decision that was already there - never a second decision beside it.
    entry = _inactive_entry(repo, entry_id)
    assert entry["proposed_reconsideration"]["content"] == RULE
    assert len(store.load(repo)["entries"]) + len(store.load_deleted(repo)["entries"]) == 1
    assert store.entry_status(entry) == ("approved" if retire else "ignored")
    assert bool(entry.get("deleted_at")) is retire      # a retired twin stays tombstoned

    # It is reviewable, one id at a time, and its raw evidence is held against the question.
    assert [e["id"] for e in store.get_pending_decisions(repo)] == [entry_id]
    assert f'reconsider_decision(entry_id="{entry_id[:8]}"' in store.format_pending_review(repo)
    ((candidate_id, meta),) = spool.held_candidates(repo).items()
    assert (meta["lane"], meta["entry_id"], meta["state"]) \
        == ("reconsideration", entry_id, "pending_review")
    assert len(_held_event_files(repo, candidate_id)) == 1
    assert spool.evidence_diagnostics(repo)["held_unattributed"] == 0


# ── crash windows and concurrency (scenarios 10-13) ──────────────────────────────

def _spool_events(repo: str, doc: dict) -> list[str]:
    """Put the scenario's corpus into the repo's spool, returning the event ids in order."""
    for event in doc["events"]:
        assert spool.append_evidence(repo, event)["status"] == "stored"
    return [e["event_id"] for e in doc["events"]]


def _held_event_files(repo: str, candidate_id: str) -> list[str]:
    return [p.name for p in spool._event_files(spool._held_dir(repo, candidate_id))
            if p.name != spool._META_NAME]


def _crash(*_args, **_kwargs):
    raise RuntimeError("crash")


def test_a_hold_whose_store_write_never_landed_is_replayed_not_stranded(tmp_repo):
    """Scenario 10 - crash after the hold, before the store mutation.

    The hold exists and names no `entry_id`, because nothing was stored. This is the state the
    hold-first transition order makes ROUTINE rather than exotic, so it is the state recovery
    is measured on: the held events are re-classified against the current store and
    materialized under the SAME candidate id, and the evidence stays put throughout
    (invariants 3 and 4). Before that replay existed such a hold was stranded for good, visible
    only as `held_unattributed`.

    The manifest is deliberately the pre-state-machine shape - no `state`, no `entry_id` - so
    this also pins the legacy migration: `held` is what such a manifest reads as.
    """
    doc = _load("10-crash-after-hold-before-store-mutation")
    event_ids = _spool_events(tmp_repo, doc)
    candidate_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "orphan-hold"))
    spool.hold_candidate_evidence(tmp_repo, candidate_id, event_ids,
                                  meta={"event_ids": event_ids, "status": "pending"})
    assert spool.held_candidates(tmp_repo)[candidate_id]["state"] == "held"
    assert spool.evidence_diagnostics(tmp_repo)["held_unattributed"] == 1

    receipt = reconcile.reconcile_session(tmp_repo)

    assert receipt["proposed"] == 1 and receipt["duplicates"] == 0
    assert len(_held_event_files(tmp_repo, candidate_id)) == len(event_ids)
    ((entry,),) = (store.load(tmp_repo)["entries"],)
    assert store.entry_status(entry) == "pending_approval"
    meta = spool.held_candidates(tmp_repo)[candidate_id]
    assert (meta["state"], meta["entry_id"]) == ("pending_review", entry["id"])
    assert spool.evidence_diagnostics(tmp_repo)["held_unattributed"] == 0

    # And it is not proposed twice: the replay is idempotent under the same id.
    assert reconcile.reconcile_session(tmp_repo)["proposed"] == 0
    assert len(store.load(tmp_repo)["entries"]) == 1


def test_a_decision_stored_before_its_hold_is_held_against_its_own_review(tmp_repo):
    """Scenario 11 - a decision stored with its evidence still pending.

    The transition order can no longer PRODUCE this state: the hold and its manifest land
    before the store is touched, so a crash leaves the evidence either wholly pending or wholly
    held. It is still reachable two other ways - a held directory written by the shipped
    materialize-then-move order, which exists on real machines, and the store's own dedup
    absorbing a restatement onto an entry still awaiting review - so it is constructed
    directly here rather than by crashing a step that no longer runs in that order.

    The requirement is unchanged. The next pass re-aggregates the events and the aggregator
    matches them onto the decision they became; settling that as a duplicate would delete the
    only evidence for a decision nobody has reviewed, so it is HELD against that decision and
    reported as `already_pending`.
    """
    doc = _load("11-crash-after-store-mutation-before-hold")
    _spool_events(tmp_repo, doc)
    reconcile.reconcile_session(tmp_repo)
    (stranded,) = spool.held_candidates(tmp_repo)
    held_dir = spool._held_dir(tmp_repo, stranded)
    for path in list(spool._event_files(held_dir)):
        if path.name != spool._META_NAME:
            path.rename(spool._pending_dir(tmp_repo) / path.name)
    shutil.rmtree(held_dir)                  # nothing on disk connects decision and evidence

    entries = store.load(tmp_repo)["entries"]
    assert [store.entry_status(e) for e in entries] == ["pending_approval"]
    assert spool.held_candidates(tmp_repo) == {}

    receipt = reconcile.reconcile_session(tmp_repo)

    assert receipt["already_pending"] == 1 and receipt["duplicates"] == 0
    assert len(store.load(tmp_repo)["entries"]) == 1, "the same decision was proposed twice"
    ((candidate_id, meta),) = spool.held_candidates(tmp_repo).items()
    assert meta["entry_id"] == entries[0]["id"]
    assert _held_event_files(tmp_repo, candidate_id), "the raw evidence was thrown away"


def test_a_hold_written_under_the_old_scoring_resumes_under_the_new_one(tmp_repo):
    """Task 03 changes what the aggregator makes of a given event set, and `_resume_holds`
    re-scores every interrupted candidate with the CURRENT scorer - so a hold written before
    this change is re-read by rules it was never scored under.

    Scenario 2 is exactly that pair: two events that used to aggregate to TWO candidates and
    now aggregate to one, held under a candidate id neither scorer would mint. The identity of
    a hold is the DIRECTORY it occupies, so the re-classification is materialized there rather
    than under the id it now computes - one decision, one hold, and a second pass changes
    nothing.
    """
    doc = _load("02-edit-then-directive")
    event_ids = _spool_events(tmp_repo, doc)
    candidate_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "pre-task-03-hold"))
    spool.hold_candidate_evidence(tmp_repo, candidate_id, event_ids,
                                  meta={"event_ids": event_ids, "status": "pending"})

    receipt = reconcile.reconcile_session(tmp_repo)

    assert receipt["proposed"] == 1 and receipt["incomplete"] is False
    ((entry,),) = (store.load(tmp_repo)["entries"],)
    assert store.entry_status(entry) == "pending_approval"
    ((held_id, meta),) = spool.held_candidates(tmp_repo).items()
    assert held_id == candidate_id, "a re-scored hold must keep the directory it occupies"
    assert (meta["state"], meta["entry_id"]) == ("pending_review", entry["id"])
    assert len(_held_event_files(tmp_repo, candidate_id)) == len(event_ids)

    assert reconcile.reconcile_session(tmp_repo)["proposed"] == 0
    assert len(store.load(tmp_repo)["entries"]) == 1


def test_a_hold_whose_events_now_classify_as_a_different_kind_resumes_down_the_new_lane(
        tmp_repo):
    """The THIRD resume direction, and the one the other two miss: not a different candidate
    COUNT but a different candidate KIND.

    `_resume_holds` gates on `len(resumed) == 1` and never compares the fresh classification's
    kind against the manifest's, which is deliberate - Task 02's `_candidate_id` docstring says
    kind and target can legitimately change between the crash and the replay. So a hold created
    when the store held nothing, resumed after a decision the same evidence now revises has
    appeared, must materialize as a proposed_revision on that decision, inside the directory it
    already occupies, without minting a second candidate or stranding the first.
    """
    doc = _load("05-directive-repeated-in-a-second-session")
    directive = doc["events"][0]
    assert spool.append_evidence(tmp_repo, directive)["status"] == "stored"
    candidate_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "kind-change-hold"))
    spool.hold_candidate_evidence(tmp_repo, candidate_id, [directive["event_id"]],
                                  meta={"event_ids": [directive["event_id"]],
                                        "status": "pending"})
    # Appears only AFTER the hold: overlap 0.57 puts it in the update band (above
    # `_UPDATE_OVERLAP`, below `_DUPLICATE_OVERLAP`), and the seed states no prohibition, so
    # the same events that classified `new` against an empty store now classify `update`.
    ok, target_id = store.update_decision(
        tmp_repo, "Regenerate src/generated/client.ts by hand once a week.",
        "sess-0", "architecture", created_by="human")
    assert ok and target_id

    receipt = reconcile.reconcile_session(tmp_repo)

    assert receipt["proposed"] == 1 and receipt["incomplete"] is False
    ((entry,),) = (store.load(tmp_repo)["entries"],)
    assert entry["id"] == target_id, "the resume created a second decision"
    assert entry["proposed_revision"]["content"].startswith("Regenerate src/generated")
    assert entry["proposed_revision"]["content"] != entry["content"]
    ((held_id, meta),) = spool.held_candidates(tmp_repo).items()
    assert held_id == candidate_id, "a re-classified hold keeps the directory it occupies"
    assert (meta["kind"], meta["target_decision_id"]) == ("update", target_id)
    assert meta["entry_id"] == target_id
    assert len(_held_event_files(tmp_repo, candidate_id)) == 1


def test_a_hold_that_now_splits_is_reported_rather_than_duplicated(tmp_repo):
    """The other direction of the same risk, and the reason it is safe: when re-aggregation
    does NOT come back as one candidate, the pass says `incomplete` and leaves the hold exactly
    as it was. Nothing is stored, nothing is split across two directories, and nothing is
    deleted - the next pass tries again.
    """
    doc = _load("03-unrelated-edit-before-directive")
    event_ids = _spool_events(tmp_repo, doc)
    candidate_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "pre-task-03-split"))
    spool.hold_candidate_evidence(tmp_repo, candidate_id, event_ids,
                                  meta={"event_ids": event_ids, "status": "pending"})

    receipt = reconcile.reconcile_session(tmp_repo)

    assert receipt["incomplete"] is True and receipt["proposed"] == 0
    assert store.load(tmp_repo)["entries"] == []
    assert list(spool.held_candidates(tmp_repo)) == [candidate_id]
    assert len(_held_event_files(tmp_repo, candidate_id)) == len(event_ids)


def test_an_uncertain_path_never_becomes_an_anchor_a_policy_or_a_guard_input(tmp_repo):
    """Runbook invariant 6, measured end to end rather than asserted at the aggregator.

    The README edit precedes the directive with nothing structural to connect them, so it is a
    `possible_source_files` entry and only that. What must NOT happen anywhere downstream: it
    becoming `source_files`, becoming an `anchor_candidates` guess a developer could bless,
    selecting a policy, or pairing at commit time.

    The edited-files sidecar is SEEDED here, and that is the whole point of the test rather
    than setup noise. Review caught this assertion passing vacuously: on Claude and Codex the
    same `post_write` call that emits the `file_changed` event also writes that sidecar, and
    `store._EDITED_FILES_WINDOW` is the same 1800 seconds as `candidates._PROXIMITY_SECONDS`,
    so in production every backward-linked path IS a fresh sidecar entry when reconciliation
    materializes the decision - and the store stamped it on as an anchor guess. A fixture-only
    replay never wrote the sidecar, so the test asserted a property the system did not have.
    Seeding it is what makes the route real.
    """
    doc = _load("03-unrelated-edit-before-directive")
    (directive,) = [c for c in candidates.aggregate_candidates(doc["events"], [])["candidates"]
                    if c["kind"] == "new"]
    assert directive["possible_source_files"] == ["README.md"]

    assert store.record_edited_file(tmp_repo, os.path.join(tmp_repo, "README.md")) == "README.md"
    assert store._read_edited_files(tmp_repo) == ["README.md"], "the route must be live"
    _spool_events(tmp_repo, doc)
    assert reconcile.reconcile_session(tmp_repo)["proposed"] == 1
    (entry,) = store.load(tmp_repo)["entries"]

    assert "README.md" not in (entry.get("source_files") or [])
    assert "README.md" not in (entry.get("anchor_candidates") or [])
    request = {"operation": "commit", "files": ["README.md"], "artifact": ""}
    assert policy.select_policies([entry], request) == []
    assert guard_engine.decisions_for_files(tmp_repo, ["README.md"]) == []


def test_a_decisions_evidence_receipts_are_bounded_and_the_drop_is_recorded(tmp_repo):
    """A repeated directive settles one candidate per repetition, each filing an
    `evidence_summary` receipt on the SAME decision - so this task opened a growth path beside
    a `recurrences` list it had just capped at 20 (review measured 31 rows for 31 repetitions).

    Bounded now, and honestly: the count of what left is stamped on the entry, the way
    `_anchor_sources` stamps `source_files_total` when it truncates an anchor. A receipt is the
    only place a settled candidate's disposition lives once its raw events are deleted, so
    dropping one silently would be the shape runbook invariant 3 exists to prevent.
    """
    cap = store.MAX_EVIDENCE_SUMMARIES
    ok, entry_id = store.update_decision(tmp_repo, RULE, "sess-0", "constraint",
                                         created_by="human")
    assert ok and entry_id

    for i in range(cap + 3):
        assert store.record_evidence_summary(tmp_repo, entry_id, {
            "candidate_id": str(uuid.uuid5(_GEN_NAMESPACE, f"receipt#{i}")),
            "disposition": "approved", "event_ids": [], "occurred_at": _T0.isoformat()})

    (entry,) = store.load(tmp_repo)["entries"]
    rows = entry["evidence_summary"]
    assert len(rows) == cap
    assert entry["evidence_summary_dropped"] == 3
    assert rows[-1]["candidate_id"] == str(uuid.uuid5(_GEN_NAMESPACE, f"receipt#{cap + 2}")), \
        "the cap keeps the most recent receipts"


def test_a_short_receipt_history_records_no_drop_at_all(tmp_repo):
    """The other side of the bound: an ordinary decision must not grow a bookkeeping key it
    never needed. `evidence_summary_dropped` appears only once something actually left."""
    ok, entry_id = store.update_decision(tmp_repo, RULE, "sess-0", "constraint",
                                         created_by="human")
    assert ok and store.record_evidence_summary(tmp_repo, entry_id, {
        "candidate_id": str(uuid.uuid5(_GEN_NAMESPACE, "one")), "disposition": "approved",
        "event_ids": [], "occurred_at": _T0.isoformat()})
    (entry,) = store.load(tmp_repo)["entries"]
    assert len(entry["evidence_summary"]) == 1
    assert "evidence_summary_dropped" not in entry


def test_no_module_outside_the_aggregator_and_its_ledger_reads_a_possible_source_file():
    """The structural half of the same invariant. `possible_source_files` is produced by
    `candidates.py` and carried on the manifest by `reconcile.py`; a further reader is how an
    uncertain path starts being treated as a certain one, so it is caught here rather than in
    review.

    `review_impact.py` is the ONE display exception, added by Task 07 because the brief
    requires the uncertain paths to be SHOWN - the failure this invariant exists to stop is a
    silent one, and a reviewer who is never told a path was considered cannot catch it either.
    Its read is one function (`_possible_files`) whose whole output is rendered under "NOT
    anchored on approval", and the behavioural half of the guard is
    `tests/test_review_impact.py::TestInvariants` - a possible file never reaches
    `files.confirmed`, never survives an approval as `source_files`, and is filtered out of the
    possible list the moment it becomes confirmed. Widen this set again only with the same
    pairing: a render-only reader plus a behavioural test that pins where the path CANNOT go.

    ONE SPELLING, all the way to the edge. `review_impact`'s dict carries the paths under the
    key `possible_source_files`, not a shorter local name, precisely so this scan still sees
    every reader: a rename at the module boundary would have created a second spelling that
    nothing greps for, which is a WEAKER ban than the absolute one this narrowed. The web
    console is served the RENDERED LINES rather than the dict for the same reason - the paths
    reach the browser already labelled "NOT anchored on approval", with no key there for a
    future handler to route into `source_files`, which is what the sweep below checks.
    """
    owners = {"candidates.py", "reconcile.py", "review_impact.py"}
    readers = [path.name for path in sorted(Path(store.__file__).parent.rglob("*.py"))
               if path.name not in owners
               and "possible_source_files" in path.read_text(encoding="utf-8")]
    assert readers == []

    # The non-Python egress the substring scan above cannot reach. Assets are scanned for BOTH
    # the owner spelling and the shorter one a projection might invent, so neither a rename here
    # nor a new console handler can start carrying an uncertain path without failing this.
    assets = sorted((Path(store.__file__).parent / "ui").rglob("*.js"))
    assert assets, "the console assets moved; this sweep is now checking nothing"
    for path in assets:
        text = path.read_text(encoding="utf-8")
        for spelling in ("possible_source_files", "files.possible", '["possible"]'):
            assert spelling not in text, f"{path.name} reads uncertain paths ({spelling})"


def test_raw_held_evidence_survives_a_failed_summary_write(tmp_repo, monkeypatch):
    """Scenario 12 - crash after the review summary, before the raw evidence deletion, and
    the invariant that governs the whole finalize order.

    The summary is recorded on the decision BEFORE the held events are deleted. A failure at
    the delete therefore leaves both the summary and the raw evidence in place, and the retry
    must not file the summary a second time.
    """
    doc = _load("12-crash-after-summary-before-evidence-deletion")
    _spool_events(tmp_repo, doc)
    reconcile.reconcile_session(tmp_repo)
    (entry,) = store.load(tmp_repo)["entries"]
    (candidate_id,) = spool.held_candidates(tmp_repo)
    assert store.approve_decision(tmp_repo, entry["id"], "approve")[0]

    with monkeypatch.context() as crashed:       # see scenario 11 on why not `undo()`
        crashed.setattr(spool, "finalize_candidate_evidence", _crash)
        assert reconcile.reconcile_session(tmp_repo)["incomplete"] is True

    assert _held_event_files(tmp_repo, candidate_id), "evidence deleted before its receipt"
    summaries = store.load(tmp_repo)["entries"][0].get("evidence_summary") or []
    assert [s["disposition"] for s in summaries] == ["approved"]

    reconcile.reconcile_session(tmp_repo)

    assert spool.held_candidates(tmp_repo) == {}
    summaries = store.load(tmp_repo)["entries"][0].get("evidence_summary") or []
    assert [s["disposition"] for s in summaries] == ["approved"], "the receipt was filed twice"


def test_no_raw_evidence_is_removed_when_the_summary_itself_fails(tmp_repo, monkeypatch):
    """The other half of invariant 4, and the one that actually motivated the finalize order:
    a store write that reports it filed nothing must leave the held evidence exactly where it
    is and mark the pass incomplete, rather than deleting the evidence and the receipt at
    once."""
    doc = _load("12-crash-after-summary-before-evidence-deletion")
    _spool_events(tmp_repo, doc)
    reconcile.reconcile_session(tmp_repo)
    (entry,) = store.load(tmp_repo)["entries"]
    (candidate_id,) = spool.held_candidates(tmp_repo)
    assert store.approve_decision(tmp_repo, entry["id"], "approve")[0]

    monkeypatch.setattr(store, "record_evidence_summary", lambda *_a, **_k: False)
    assert reconcile.reconcile_session(tmp_repo)["incomplete"] is True

    assert _held_event_files(tmp_repo, candidate_id)
    assert not (store.load(tmp_repo)["entries"][0].get("evidence_summary") or [])


# ── the reconsideration lane, end to end (outstanding issue 7) ───────────────────

def _one_hold(repo: str) -> tuple:
    ((candidate_id, meta),) = spool.held_candidates(repo).items()
    return candidate_id, meta


def _summaries(repo: str, entry_id: str) -> list:
    return [s["disposition"]
            for s in _inactive_entry(repo, entry_id).get("evidence_summary") or []]


def _restated(repo: str, *, retire: bool) -> str:
    """An inactive twin with a reconsideration proposed and its evidence held."""
    entry_id = _inactive_twin(repo, retire=retire)
    evidence.emit_hook_event(repo, "user_directive", session_id="sess-a", source="replay",
                             summary=RULE)
    assert reconcile.reconcile_session(repo)["reconsidered"] == 1
    return entry_id


@pytest.mark.parametrize("retire", [False, True])
def test_restoring_settles_the_candidate_and_keeps_the_receipt(tmp_repo, retire):
    """The way back out. Approval in this lane is recognized ONLY by a completed `restored`
    record for the basis revision - never by the decision merely being live again - and the
    compact summary lands on that same decision before its raw evidence is removed."""
    entry_id = _restated(tmp_repo, retire=retire)
    candidate_id, _meta = _one_hold(tmp_repo)
    before = _inactive_entry(tmp_repo, entry_id)["revisions"]

    assert lifecycle.reconsider_decision(tmp_repo, entry_id, "restore")[0]
    reconcile.reconcile_session(tmp_repo)

    entry = _inactive_entry(tmp_repo, entry_id)
    assert entry["id"] == entry_id and entry["revisions"] == before
    assert "restored" in [r["kind"] for r in entry["lifecycle"]]
    assert _summaries(tmp_repo, entry_id) == ["approved"]
    assert spool.held_candidates(tmp_repo) == {}
    assert not _held_event_files(tmp_repo, candidate_id)


def test_dismissing_settles_the_candidate_against_a_decision_that_stays_inactive(tmp_repo):
    entry_id = _restated(tmp_repo, retire=False)
    assert lifecycle.reconsider_decision(tmp_repo, entry_id, "dismiss")[0]
    reconcile.reconcile_session(tmp_repo)

    assert store.entry_status(_inactive_entry(tmp_repo, entry_id)) == "ignored"
    assert _summaries(tmp_repo, entry_id) == ["dismissed"]
    assert spool.held_candidates(tmp_repo) == {}


def test_a_dismissal_is_never_filed_as_an_approval(tmp_repo):
    """A `restored` lifecycle record is written by three different acts - this lane's own
    restore, the `restore_decision` tool, and `contexer restore` - and says nothing about
    which. Settling on that record alone recorded `approved` in the durable ledger for a
    question the developer had explicitly DISMISSED, which is the fabricated-approval class
    the disposition rules exist to prevent. The RECEIPT decides now, and it names the
    candidate.

    The developer's own route, not a crash and not hand-built state: they answer the question
    with the other tool, which makes the reconsideration stale, and then dismiss it because
    the review surface says dismissal still works.
    """
    entry_id = _restated(tmp_repo, retire=True)
    assert lifecycle.restore_decision(tmp_repo, entry_id)[0]
    assert lifecycle.reconsider_decision(tmp_repo, entry_id, "dismiss")[0]

    reconcile.reconcile_session(tmp_repo)

    assert _summaries(tmp_repo, entry_id) == ["dismissed"]
    assert spool.held_candidates(tmp_repo) == {}


def test_a_second_restatement_displaced_by_the_first_is_settled_not_stranded(tmp_repo):
    """An entry has ONE reconsideration slot, so a second directive restating the same
    decision while the first question sits is HELD rather than proposed. When the developer
    dismisses the sitting one, the receipt names only that candidate - and the held one used
    to be stuck forever: no slot to wait on, no receipt of its own, and invisible to
    `held_unattributed` because it does name an entry.

    It asked the same question about the same decision at the same revision and was answered
    by the same act, so the same-basis receipt settles it too.
    """
    entry_id = _restated(tmp_repo, retire=False)
    evidence.emit_hook_event(tmp_repo, "user_directive", session_id="sess-b", source="replay",
                             summary=RULE + " This still holds.")
    assert reconcile.reconcile_session(tmp_repo)["already_pending"] == 1
    assert len(spool.held_candidates(tmp_repo)) == 2

    assert lifecycle.reconsider_decision(tmp_repo, entry_id, "dismiss")[0]
    reconcile.reconcile_session(tmp_repo)

    assert _summaries(tmp_repo, entry_id) == ["dismissed", "dismissed"]
    assert spool.held_candidates(tmp_repo) == {}
    assert store.entry_status(_inactive_entry(tmp_repo, entry_id)) == "ignored"


def test_a_receipt_never_settles_a_candidate_held_after_it_was_written(tmp_repo):
    """The TIME half of the shared-receipt rule, in four ordinary passes.

    A dismissal does not advance the decision's revision, so an old receipt sits at that basis
    forever. Matching on the basis alone therefore settled every LATER displaced candidate off
    it - filing `dismissed` for a restatement nobody had answered, and finalizing its evidence
    away, while the sibling question at that same revision went on to be RESTORED. The durable
    ledger then read dismissed / dismissed / approved with a fabricated middle row.

    A receipt can only answer a question that existed when it was written.
    """
    entry_id = _restated(tmp_repo, retire=False)                    # pass 1: Q1
    assert lifecycle.reconsider_decision(tmp_repo, entry_id, "dismiss")[0]
    reconcile.reconcile_session(tmp_repo)
    assert _summaries(tmp_repo, entry_id) == ["dismissed"]

    evidence.emit_hook_event(tmp_repo, "user_directive", session_id="sess-b", source="replay",
                             summary=RULE + " I mean it.")
    assert reconcile.reconcile_session(tmp_repo)["reconsidered"] == 1        # pass 2: Q2
    sitting = _inactive_entry(tmp_repo, entry_id)["proposed_reconsideration"]

    evidence.emit_hook_event(tmp_repo, "user_directive", session_id="sess-c", source="replay",
                             summary=RULE + " Still true.")
    assert reconcile.reconcile_session(tmp_repo)["already_pending"] == 1     # pass 3: displaced
    reconcile.reconcile_session(tmp_repo)                                   # pass 4: the bug

    assert _summaries(tmp_repo, entry_id) == ["dismissed"], "settled off a receipt that predates it"
    assert len(spool.held_candidates(tmp_repo)) == 2
    assert _inactive_entry(tmp_repo, entry_id)["proposed_reconsideration"] == sitting

    # And the answer the developer DOES give settles both, with the truth on every row.
    assert lifecycle.reconsider_decision(tmp_repo, entry_id, "restore")[0]
    reconcile.reconcile_session(tmp_repo)

    assert sorted(_summaries(tmp_repo, entry_id)) == ["approved", "approved", "dismissed"]
    assert spool.held_candidates(tmp_repo) == {}


def test_a_later_question_is_never_settled_by_an_earlier_answer(tmp_repo):
    """The guard on the fix above. A restatement arriving AFTER a dismissal opens a NEW
    question at the same basis, which the brief explicitly grants - and the earlier receipt
    sits at that same basis, so a same-basis rule that ignored which candidate holds the slot
    would settle the new question the moment it was asked."""
    entry_id = _restated(tmp_repo, retire=False)
    assert lifecycle.reconsider_decision(tmp_repo, entry_id, "dismiss")[0]
    reconcile.reconcile_session(tmp_repo)

    evidence.emit_hook_event(tmp_repo, "user_directive", session_id="sess-b", source="replay",
                             summary=RULE + " I mean it.")
    assert reconcile.reconcile_session(tmp_repo)["reconsidered"] == 1
    reconcile.reconcile_session(tmp_repo)

    assert _inactive_entry(tmp_repo, entry_id)["proposed_reconsideration"]
    assert len(spool.held_candidates(tmp_repo)) == 1, "the new question was settled unasked"
    assert _summaries(tmp_repo, entry_id) == ["dismissed"]


@pytest.mark.parametrize("retire", [False, True])
def test_a_pending_reconsideration_is_counted_at_session_start(tmp_repo, retire):
    """It was the only proposal lane invisible there, and the mid-session nudge is fire-once -
    so a question the developer did not act on that one time was never raised again. COUNT
    only, never content, exactly like the other lanes."""
    _restated(tmp_repo, retire=retire)
    context = store.session_start_payload(tmp_repo)["context"]
    assert "1 decision pending your review" in context
    assert RULE not in context          # the count, never the content
    assert store.pending_review_nudge(tmp_repo)


def test_a_skipped_reconsideration_keeps_its_evidence_held(tmp_repo):
    """`skip` is not a disposition. Nothing settles, so nothing is deleted - the same rule
    that keeps a held candidate exempt from retention until somebody actually answers."""
    entry_id = _restated(tmp_repo, retire=False)
    candidate_id, _meta = _one_hold(tmp_repo)
    assert lifecycle.reconsider_decision(tmp_repo, entry_id, "skip")[0]
    reconcile.reconcile_session(tmp_repo)

    assert len(_held_event_files(tmp_repo, candidate_id)) == 1
    assert _summaries(tmp_repo, entry_id) == []


def _during_the_attach_window(mutate):
    """A `propose_reconsideration` that lets `mutate()` change the store first, ONCE.

    The window is real and unlocked on purpose: a pass classifies its evidence against a
    snapshot, then does the filesystem work of holding it, and only then does the attach take
    the store lock. Anything the developer or another session does in between lands here. Once,
    because the replay that follows must meet a STABLE store."""
    real = lifecycle.propose_reconsideration
    fired = []

    def proposer(*args, **kwargs):
        if not fired:
            fired.append(True)
            mutate()
        return real(*args, **kwargs)

    return proposer


def _advance_revision(repo: str, entry_id: str):
    def mutate():
        data = store.load(repo)
        entry = next(e for e in data["entries"] if e["id"] == entry_id)
        revisions.append_revision(entry, RULE + " Regenerate with `make codegen`.", "ai")
        store.save(repo, data)
    return mutate


def _un_ignore(repo: str, entry_id: str):
    """Straight to the store, because no public route brings an IGNORED decision back except
    answering a reconsideration - which is the thing this window is preventing. It stands in
    for any route that might: a hand-edited store, the console, a later lane."""
    def mutate():
        data = store.load(repo)
        next(e for e in data["entries"] if e["id"] == entry_id)["status"] = "approved"
        store.save(repo, data)
    return mutate


def _pass_through_the_window(repo: str, monkeypatch, mutate) -> dict:
    with monkeypatch.context() as window:
        window.setattr(lifecycle, "propose_reconsideration",
                       _during_the_attach_window(mutate))
        return reconcile.reconcile_session(repo)


def test_a_revision_advance_in_the_attach_window_proposes_nothing(tmp_repo, monkeypatch):
    """The defect this closes: the manifest records the revision the evidence was CLASSIFIED
    against, while the attach bound the proposal to whatever was current when it ran. A
    developer's answer then landed at the newer revision, the held candidate went on waiting
    for one at the older, and the two could never meet - the evidence was held forever with no
    receipt anywhere.

    Refusing costs one deferred pass. Attaching cost the evidence."""
    entry_id = _inactive_twin(tmp_repo, retire=False)
    evidence.emit_hook_event(tmp_repo, "user_directive", session_id="sess-a", source="replay",
                             summary=RULE)

    receipt = _pass_through_the_window(tmp_repo, monkeypatch,
                                       _advance_revision(tmp_repo, entry_id))

    assert receipt["reconsidered"] == 0
    assert receipt["incomplete"] is True
    assert not _inactive_entry(tmp_repo, entry_id).get("proposed_reconsideration")
    candidate_id, meta = _one_hold(tmp_repo)
    assert meta["state"] == "materializing"          # replayable, not settled
    assert len(_held_event_files(tmp_repo, candidate_id)) == 1
    assert _summaries(tmp_repo, entry_id) == []


@pytest.mark.parametrize("retire,mutation", [
    (False, "retire"),                               # ignored -> retired
    (True, "restore"),                               # retired -> live
    (False, "un_ignore"),                            # ignored -> live
])
def test_a_state_change_in_the_attach_window_proposes_nothing(tmp_repo, monkeypatch,
                                                              retire, mutation):
    """The other half of the binding. A reconsideration asks "should this come back?", so the
    state it was judged in is as load-bearing as the revision: a question formed about an
    ignored decision says nothing about a retired one, and nothing at all about one that is
    live again. Each refusal leaves the raw evidence exactly where it is."""
    entry_id = _inactive_twin(tmp_repo, retire=retire)
    mutate = {
        "retire": lambda: lifecycle.retire_decision(tmp_repo, entry_id, "superseded"),
        "restore": lambda: lifecycle.restore_decision(tmp_repo, entry_id),
        "un_ignore": _un_ignore(tmp_repo, entry_id),
    }[mutation]
    evidence.emit_hook_event(tmp_repo, "user_directive", session_id="sess-a", source="replay",
                             summary=RULE)

    receipt = _pass_through_the_window(tmp_repo, monkeypatch, mutate)

    assert receipt["reconsidered"] == 0
    assert not _inactive_entry(tmp_repo, entry_id).get("proposed_reconsideration")
    candidate_id, _meta = _one_hold(tmp_repo)
    assert len(spool.held_events(tmp_repo, candidate_id)) == 1
    assert _summaries(tmp_repo, entry_id) == []


def test_the_replay_after_a_stale_refusal_asks_one_question_at_the_new_basis(tmp_repo,
                                                                            monkeypatch):
    """Deferred, not dropped. The next pass re-classifies the HELD events against the store as
    it is NOW, under the same candidate id, and the manifest and the proposal agree on the
    basis - which is what lets the developer's answer settle this candidate."""
    entry_id = _inactive_twin(tmp_repo, retire=False)
    evidence.emit_hook_event(tmp_repo, "user_directive", session_id="sess-a", source="replay",
                             summary=RULE)
    _pass_through_the_window(tmp_repo, monkeypatch, _advance_revision(tmp_repo, entry_id))
    first_id, _meta = _one_hold(tmp_repo)

    assert reconcile.reconcile_session(tmp_repo)["reconsidered"] == 1

    candidate_id, meta = _one_hold(tmp_repo)
    assert candidate_id == first_id
    entry = _inactive_entry(tmp_repo, entry_id)
    proposal = entry["proposed_reconsideration"]
    assert meta["basis_revision_id"] == proposal["basis_revision_id"] \
        == entry["current_revision_id"]
    assert proposal["candidate_id"] == candidate_id
    assert proposal["target_state"] == "ignored"


@pytest.mark.parametrize("action,disposition", [("restore", "approved"),
                                                ("dismiss", "dismissed")])
def test_the_answer_to_a_replayed_question_settles_it_with_exactly_one_summary(
        tmp_repo, monkeypatch, action, disposition):
    """End to end through the refusal: one question, one answer, one receipt, no raw evidence
    left over. Before the binding this candidate could be settled by nothing at all."""
    entry_id = _inactive_twin(tmp_repo, retire=False)
    evidence.emit_hook_event(tmp_repo, "user_directive", session_id="sess-a", source="replay",
                             summary=RULE)
    _pass_through_the_window(tmp_repo, monkeypatch, _advance_revision(tmp_repo, entry_id))
    assert reconcile.reconcile_session(tmp_repo)["reconsidered"] == 1
    candidate_id, _meta = _one_hold(tmp_repo)

    assert lifecycle.reconsider_decision(tmp_repo, entry_id, action)[0]
    reconcile.reconcile_session(tmp_repo)

    assert _summaries(tmp_repo, entry_id) == [disposition]
    assert spool.held_candidates(tmp_repo) == {}
    assert not _held_event_files(tmp_repo, candidate_id)


def test_a_crash_before_the_proposal_replays_into_exactly_one_question(tmp_repo,
                                                                       monkeypatch):
    """Crash boundary 1 - the events are held and the proposal never landed. The replay
    re-classifies the HELD events against the current store under the SAME candidate id, so
    the question is asked once, not twice."""
    entry_id = _inactive_twin(tmp_repo, retire=True)
    evidence.emit_hook_event(tmp_repo, "user_directive", session_id="sess-a", source="replay",
                             summary=RULE)
    with monkeypatch.context() as crashed:
        crashed.setattr(lifecycle, "propose_reconsideration", _crash)
        assert reconcile.reconcile_session(tmp_repo)["incomplete"] is True
    candidate_id, meta = _one_hold(tmp_repo)
    assert meta["state"] == "materializing"
    assert not _inactive_entry(tmp_repo, entry_id).get("proposed_reconsideration")

    assert reconcile.reconcile_session(tmp_repo)["reconsidered"] == 1

    assert list(spool.held_candidates(tmp_repo)) == [candidate_id]
    assert _inactive_entry(tmp_repo, entry_id)["proposed_reconsideration"]["content"] == RULE
    assert len(_held_event_files(tmp_repo, candidate_id)) == 1


def test_a_crash_at_the_summary_keeps_the_raw_evidence_and_files_it_once(tmp_repo,
                                                                        monkeypatch):
    """Crash boundaries 3 and 4 together, in the order the finalize is written: a summary that
    did not land leaves the held evidence exactly where it is, and a cleanup that crashed
    after a durable summary must not file it a second time."""
    entry_id = _restated(tmp_repo, retire=False)
    candidate_id, _meta = _one_hold(tmp_repo)
    assert lifecycle.reconsider_decision(tmp_repo, entry_id, "restore")[0]

    with monkeypatch.context() as failed:
        failed.setattr(store, "record_evidence_summary", lambda *_a, **_k: False)
        assert reconcile.reconcile_session(tmp_repo)["incomplete"] is True
    assert _held_event_files(tmp_repo, candidate_id)
    assert _summaries(tmp_repo, entry_id) == []

    with monkeypatch.context() as crashed:
        crashed.setattr(spool, "finalize_candidate_evidence", _crash)
        assert reconcile.reconcile_session(tmp_repo)["incomplete"] is True
    assert _held_event_files(tmp_repo, candidate_id), "evidence deleted before its receipt"
    assert _summaries(tmp_repo, entry_id) == ["approved"]

    reconcile.reconcile_session(tmp_repo)

    assert spool.held_candidates(tmp_repo) == {}
    assert _summaries(tmp_repo, entry_id) == ["approved"], "the receipt was filed twice"


def test_a_stale_basis_refuses_restoration_and_the_evidence_stays_held(tmp_repo):
    """A verdict passed on text nobody read in this form is refused, exactly as the retirement
    lane refuses one - and refusing settles nothing, so the evidence is still there for the
    fresh question that replaces it."""
    entry_id = _restated(tmp_repo, retire=False)
    candidate_id, _meta = _one_hold(tmp_repo)
    data = store.load(tmp_repo)
    entry = next(e for e in data["entries"] if e["id"] == entry_id)
    store.revisions.append_revision(entry, RULE + " Also update the docs.", source="human")
    store.save(tmp_repo, data)

    ok, message = lifecycle.reconsider_decision(tmp_repo, entry_id, "restore")
    assert not ok and "Cannot restore" in message
    reconcile.reconcile_session(tmp_repo)

    assert store.entry_status(_inactive_entry(tmp_repo, entry_id)) == "ignored"
    assert len(_held_event_files(tmp_repo, candidate_id)) == 1
    assert _summaries(tmp_repo, entry_id) == []


def test_a_pending_reconsideration_never_injects_and_never_gates_a_commit(tmp_repo):
    """Runbook invariant 5 for this lane. The proposal is unreviewed content on a decision the
    developer switched off, so it must reach neither a session's context nor a blocking policy
    verdict until they explicitly restore it - and restoring an ignored twin returns it
    PENDING, which is still excluded from both."""
    entry_id = _restated(tmp_repo, retire=False)
    entry = _inactive_entry(tmp_repo, entry_id)
    entry["guard_check"] = {"type": "regex", "pattern": "client", "flags": "", "paths": "",
                            "message": "no", "armed_at": "t"}

    request = {"intent": "commit", "operation": "commit", "repo_key": tmp_repo,
               "files": [GENERATED],
               "artifact": {"kind": "diff", "content": "+ import client\n"}}
    assert policy.select_policies([entry], request) == []
    assert RULE not in store.get_context(tmp_repo)
    assert "generated" not in store.get_context(tmp_repo)
    assert RULE not in store.get_context_for_prompt(tmp_repo, "why is the client generated?")
    assert entry_id[:8] not in store.session_start_payload(tmp_repo)

    assert lifecycle.reconsider_decision(tmp_repo, entry_id, "restore")[0]
    restored = _inactive_entry(tmp_repo, entry_id)
    restored["guard_check"] = entry["guard_check"]
    assert store.entry_status(restored) == "pending_approval"
    assert policy.select_policies([restored], request) == []


def test_the_second_simultaneous_reconciliation_does_nothing_and_says_so(tmp_repo):
    """Scenario 13. Two passes over one spool can file a disposition that never happened, so
    the second one SKIPS rather than waiting or racing - and the receipt says which it was,
    because a silent no-op and a completed pass read identically to a caller."""
    doc = _load("13-two-simultaneous-reconciliations")
    _spool_events(tmp_repo, doc)
    inner: dict = {}

    real_aggregate = candidates.aggregate_candidates

    def _reenter(events, decisions):
        # Re-entering from inside the outer pass is what makes this deterministic: a thread
        # race would leave which pass wins to the scheduler.
        if not inner:
            inner.update(reconcile.reconcile_session(tmp_repo))
        return real_aggregate(events, decisions)

    candidates.aggregate_candidates = _reenter
    try:
        outer = reconcile.reconcile_session(tmp_repo)
    finally:
        candidates.aggregate_candidates = real_aggregate

    assert inner["skipped"] is True
    assert inner["proposed"] == 0 and outer["skipped"] is False
    assert len(store.load(tmp_repo)["entries"]) == 1


# ── a damaged spool (scenario 17) ────────────────────────────────────────────────

def test_a_corrupt_pending_event_is_quarantined_and_its_siblings_still_reconcile(tmp_repo):
    """Half of scenario 17. A file that will not parse is MOVED OUT rather than skipped in
    place: skipping would leave it invisible to quarantine and to retention at once, which is
    how a stray file becomes permanent."""
    doc = _load("17-corrupt-pending-event-and-held-manifest")
    _spool_events(tmp_repo, doc)
    pending = spool._pending_dir(tmp_repo)
    (pending / "20260801T100000-broken.json").write_text(doc["corrupt_pending_bytes"],
                                                         encoding="utf-8")

    readable = spool.list_pending_evidence(tmp_repo)

    assert [e["kind"] for e in readable] == ["user_directive"]
    assert spool.evidence_diagnostics(tmp_repo)["quarantine"] == 1
    assert reconcile.reconcile_session(tmp_repo)["proposed"] == 1


def test_a_corrupt_held_manifest_is_reported_rather_than_interpreted(tmp_repo):
    """The other half. An unreadable `candidate.json` names no `entry_id`, so nothing can
    judge that candidate - and guessing is exactly what would fabricate a disposition. It
    stays held, its evidence stays put, and `held_unattributed` is how a developer sees it."""
    doc = _load("17-corrupt-pending-event-and-held-manifest")
    event_ids = _spool_events(tmp_repo, doc)
    candidate_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "corrupt-manifest"))
    spool.hold_candidate_evidence(tmp_repo, candidate_id, event_ids, meta={"status": "pending"})
    (spool._held_dir(tmp_repo, candidate_id) / spool._META_NAME).write_text(
        doc["corrupt_manifest_bytes"], encoding="utf-8")

    assert spool.held_candidates(tmp_repo) == {candidate_id: {"unreadable": True}}
    receipt = reconcile.reconcile_session(tmp_repo)

    assert receipt["proposed"] == 0
    assert _held_event_files(tmp_repo, candidate_id)
    assert spool.evidence_diagnostics(tmp_repo)["held_unattributed"] == 1


# ── the four Teams server shapes (scenario 16) ───────────────────────────────────

def _caps_transport(monkeypatch, advertised):
    """`remote._acall_tool` answering `get_capabilities` with `advertised` and recording every
    push payload. The same seam tests/test_lifecycle_sync.py patches, so these assertions are
    against the real `_wire_args` output rather than a projection."""
    pushes: list[dict] = []

    async def fake(endpoint, token, name, arguments, timeout):
        if name == "get_capabilities":
            return types.SimpleNamespace(content=[], isError=False,
                                         structuredContent={"capabilities": advertised})
        pushes.append(arguments)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="Saved decision srv-1.")],
            structuredContent=None, isError=False)

    monkeypatch.setattr(remote, "_acall_tool", fake)
    return pushes


_RETIRED_RECORD = {"event_id": "ev-1", "kind": "retired", "actor": "human",
                   "occurred_at": "2026-08-01T00:00:00+00:00",
                   "reason": "superseded by the codegen step", "revision_id": "rev-3",
                   "replacement_decision_id": None}


def _push(pushes_for, advertised, monkeypatch):
    pushes = _caps_transport(monkeypatch, advertised)
    remote.RemoteStore("https://t/mcp", "tok").push_decision(
        type="constraint", content=RULE, repo="github.com/acme/widgets",
        decision_id="dec-1", revision_id="rev-3", lifecycle=[_RETIRED_RECORD])
    pushes_for.extend(pushes)
    return pushes


@pytest.mark.parametrize("variant", ["lifecycle_capable", "old", "mis_advertising",
                                     "rolled_back"])
def test_each_teams_server_shape_receives_only_what_it_advertised(variant, monkeypatch):
    """Scenario 16, and runbook invariant 11. `_WIRE_LIFECYCLE` is patched here rather than
    inherited: the mechanism under test is the NEGOTIATION, so the test says what it depends on.
    The shipped value, and its rollback, are pinned separately below."""
    server = next(s for s in _load("16-teams-server-variants")["servers"]
                  if s["variant"] == variant)
    monkeypatch.setattr(remote, "_WIRE_LIFECYCLE", True)

    (args,) = _push([], server["advertised"], monkeypatch)

    assert ("lifecycle" in args) is server["expect_lifecycle_on_wire"]
    assert ("revision_id" in args) is server["expect_revision_id_on_wire"]
    # Whatever the server did or did not advertise, the base decision always lands.
    assert args["content"] == RULE and args["type"] == "constraint"

    if "rolled_back_to" in server:
        (after,) = _push([], server["rolled_back_to"], monkeypatch)
        assert ("lifecycle" in after) is server["expect_lifecycle_after_rollback"]
        assert after["content"] == RULE


def test_the_lifecycle_wire_gate_is_open_after_live_validation(monkeypatch):
    """Outstanding issue 5, closed by Task 08. The field spellings were guesses until the server
    contract was read and proved live against a running migrated endpoint; the gate now ships
    OPEN, and this pins that an advertising server actually receives the deltas."""
    assert remote._WIRE_LIFECYCLE is True
    server = next(s for s in _load("16-teams-server-variants")["servers"]
                  if s["variant"] == "lifecycle_capable")
    (args,) = _push([], server["advertised"], monkeypatch)
    assert "lifecycle" in args and "revision_id" in args


def test_rolling_the_lifecycle_gate_back_still_ships_the_legacy_shape(monkeypatch):
    """The rollback its comment promises is still one line. An advertising server receives the
    pre-feature payload with the constant closed, and the capability probe is not even made."""
    monkeypatch.setattr(remote, "_WIRE_LIFECYCLE", False)
    server = next(s for s in _load("16-teams-server-variants")["servers"]
                  if s["variant"] == "lifecycle_capable")
    (args,) = _push([], server["advertised"], monkeypatch)
    assert "lifecycle" not in args and "revision_id" not in args


# ── the frozen invariants ────────────────────────────────────────────────────────

def test_the_same_events_in_any_order_produce_the_same_ids_and_output():
    """Invariant 1. `candidate_id` is a uuid5 over the kind, the target and the SORTED event
    ids, and events are read in (occurred_at, event_id) order, so input order cannot reach
    the output at all. Asserted across the whole corpus rather than on one fixture."""
    shuffler = random.Random(20260801)
    for name in _scenarios():
        doc = _load(name)
        if not doc["events"]:
            continue
        baseline = candidates.aggregate_candidates(doc["events"], doc["decisions"])
        shuffled = list(doc["events"])
        shuffler.shuffle(shuffled)
        assert json.dumps(candidates.aggregate_candidates(shuffled, doc["decisions"]),
                          sort_keys=True) == json.dumps(baseline, sort_keys=True), name


def test_no_candidate_id_is_random():
    """The other half of invariant 1: a uuid4 anywhere in `candidates.py` would make every id
    change per run, which the shuffle test above cannot see because it runs in one process."""
    for name in _scenarios():
        doc = _load(name)
        for candidate in candidates.aggregate_candidates(doc["events"],
                                                         doc["decisions"])["candidates"]:
            assert uuid.UUID(candidate["candidate_id"]).version == 5, name


@pytest.mark.parametrize("status,armed", [
    ("pending_approval", True), ("suggested", True), ("ignored", True),
    ("approved", False),
])
def test_unratified_content_never_produces_a_blocking_policy_verdict(tmp_repo, status, armed):
    """Invariant 5, at the one place a verdict can block. `select_policies` gates on
    `approved` AND `is_trusted`, so an entry awaiting review, one in the `suggested` tier and
    one the developer ignored are excluded from the set ENTIRELY - they can neither block nor
    warn. The `approved` row is the control: an auto-approved entry no human ratified is
    excluded too, which is what stops the gate reading as a status check alone.
    """
    entry = store._new_decision_entry(
        RULE, "sess-a", "constraint",
        created_by="ai" if not armed else "human", status=status)
    entry["guard_check"] = {"type": "regex", "pattern": "TODO", "flags": "", "paths": "",
                            "message": "no TODOs", "armed_at": "t"}
    entry["source_files"] = [GENERATED]
    request = {"intent": "commit", "operation": "commit", "repo_key": tmp_repo,
               "files": [GENERATED],
               "artifact": {"kind": "diff", "content": "+ # TODO fix this\n"}}

    selected = policy.select_policies([entry], request)
    verdicts = [m["verdict"] for m in policy.evaluate_policies(selected, request)["matches"]]

    assert selected == []
    assert "block" not in verdicts


def test_a_tombstoned_entry_never_selects_even_when_armed(tmp_repo):
    """Invariant 5's other inactive shape. A retired decision should never reach the selector
    at all, and it is deselected anyway - one `.get` against the cost of a retired policy
    still blocking commits."""
    entry = store._new_decision_entry(RULE, "sess-a", "constraint", created_by="human",
                                      status="approved")
    entry["approved_by"] = "human"
    entry["tombstoned"] = True
    entry["guard_check"] = {"type": "regex", "pattern": "TODO", "flags": "", "paths": "",
                            "message": "no TODOs", "armed_at": "t"}
    request = {"intent": "commit", "operation": "commit", "repo_key": tmp_repo, "files": [],
               "artifact": {"kind": "diff", "content": "+ # TODO\n"}}
    assert policy.select_policies([entry], request) == []


def test_the_editor_hook_append_never_reads_or_lists_prior_events(tmp_repo, monkeypatch):
    """Invariant: an editor hook writes exactly one file and reads nothing.

    Asserted as SYSCALLS rather than as a timing, because a latency test would pass equally
    well if the spool were read and merely happened to be small - and it is the read, not the
    millisecond, that makes the cost of event N depend on events 1..N-1 and puts a hook on
    the developer's critical path.
    """
    seed = _load("01-directive-then-edit")["events"][0]
    for i in range(50):
        # Fifty prior events, so "reads nothing" is a claim about a spool that HAS something
        # to read. Ids stay uuid5 like the rest of the corpus.
        spool.append_evidence(tmp_repo, seed | {
            "event_id": str(uuid.uuid5(_GEN_NAMESPACE, f"append-filler#{i}"))})
    root = str(spool._repo_dir(tmp_repo))
    reads: list[str] = []

    for module, name in ((io, "open"), (builtins, "open")):
        real = getattr(module, name)

        def watched(path, mode="r", *args, _real=real, **kwargs):
            if root in str(path) and not any(flag in mode for flag in "wxa"):
                reads.append(f"read:{path}")
            return _real(path, mode, *args, **kwargs)
        monkeypatch.setattr(module, name, watched)
    for module, name in ((os, "listdir"), (os, "scandir")):
        real = getattr(module, name)

        def watched_listing(path=".", *args, _real=real, **kwargs):
            if root in str(path):
                reads.append(f"list:{path}")
            return _real(path, *args, **kwargs)
        monkeypatch.setattr(module, name, watched_listing)

    result = spool.append_evidence(tmp_repo, _load("06-agent-conclusion-with-rationale")
                                   ["events"][0])

    assert result["status"] == "stored"
    assert reads == [], f"the append path read the spool: {reads}"


def test_a_second_reconcile_produces_no_second_proposal(tmp_repo):
    """Invariant: replaying the same evidence proposes nothing new. The mechanism is the
    HOLD - the events are moved out of `pending/` and never reach the aggregator again - not
    the store's novelty filter, so the receipt and the spool are what is asserted."""
    _spool_events(tmp_repo, _load("01-directive-then-edit"))
    first = reconcile.reconcile_session(tmp_repo)
    before = json.dumps(store.load(tmp_repo), sort_keys=True)

    second = reconcile.reconcile_session(tmp_repo)

    assert first["proposed"] == 1 and second["proposed"] == 0
    # Not merely "nothing new was proposed": the second pass observed NO events at all,
    # because the first one moved them into the hold. That is the mechanism, and asserting
    # it here is what stops this passing for the wrong reason if the hold ever stops working
    # and the store's novelty filter starts absorbing the replay instead.
    assert second["events_observed"] == 0 and second["duplicates"] == 0
    assert json.dumps(store.load(tmp_repo), sort_keys=True) == before
    assert spool.list_pending_evidence(tmp_repo) == []
    assert len(spool.held_candidates(tmp_repo)) == 1


@pytest.mark.parametrize("field", ["priority", "confidence", "decision_id"])
def test_an_unknown_event_field_fails_validation_rather_than_being_interpreted(field):
    """Invariant: the schema is FROZEN per version. An unknown top-level key is an ERROR, not
    something preserved and read by whatever comes next - a writer that invents a field
    learns at the gate instead of having it silently ignored downstream."""
    raw = dict(_load("01-directive-then-edit")["events"][0])
    raw[field] = "whatever"
    normalized, errors = evidence.validate_event(raw)
    assert normalized is None
    assert errors == [f"unknown top-level key: {field!r}"]


def test_a_forward_schema_version_is_rejected_in_both_directions():
    """The same freeze, the other way: a NEWER writer's event is rejected rather than read
    with this version's semantics."""
    raw = dict(_load("01-directive-then-edit")["events"][0])
    raw["schema_version"] = evidence.SCHEMA_VERSION + 1
    assert evidence.validate_event(raw)[0] is None


def test_cursor_is_prompt_signal_only_rather_than_a_full_host(tmp_repo):
    """Invariant: Cursor's coverage is honest about what its hooks can see.

    Its hooks cannot observe an edit, so it emits `user_directive` and nothing else. The
    absence of a `file_changed` event on Cursor must mean "Cursor could not see it", never
    "nothing happened", which is why the kind is absent from the module rather than emitted
    with a guessed path.
    """
    source = Path(cursor.__file__).read_text(encoding="utf-8")
    assert '"file_changed"' not in source and "'file_changed'" not in source
    assert "emit_hook_event" not in source

    raw = json.dumps({"prompt": "always use conventional commits", "session_id": "sess-a",
                      "workspace_roots": [tmp_repo]})
    cursor.capture_constraint("", raw)
    assert [e["kind"] for e in spool.list_pending_evidence(tmp_repo)] == ["user_directive"]


# ── baseline measurements (outstanding issue 4 and the Task 06 gate) ─────────────
#
# `perf`-marked, so CI deselects them and conftest skips them under coverage: a wall-clock
# number taken under a tracer is not the number the measurement is about. There is no
# default-suite wall-clock assertion anywhere in this file; the loose ceilings below exist
# only to catch an order-of-magnitude move, and Task 06's gate is the real pass/fail.

_RUNS = 3

# Task 06's gate, for both 1,000-event fixtures. Unlike the loose order-of-magnitude ceilings
# beside it this one is a real pass/fail: the shared session-start wiring was not allowed to
# land until aggregation cleared it at the spool's own `_MAX_PENDING_EVENTS` bound, since that
# bound is what a session start now pays in the worst case. Measured here at 106.45ms and
# 8.26ms, so the margin is real rather than nominal.
_AGGREGATION_GATE_MS = 500.0

# The realistic-session row Task 06 states its regression delta from (ledger ruling D6): the
# same aggregation-only measurement, 30.57ms before the indexing, 5.68ms after. The ceiling
# here stays loose on purpose - this row exists to catch an order-of-magnitude move on a
# machine nobody fixed, and the 20 percent no-regression rule was judged against the recorded
# medians rather than against a CI-flaky assertion.
_REALISTIC_CEILING_MS = 2000.0


def _median_ms(call) -> float:
    call()                                     # warm-up, discarded
    samples = []
    for _ in range(_RUNS):
        start = time.perf_counter()
        call()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


def _report(label: str, median: float) -> None:
    print(f"\n  {label}: {median:.2f}ms (median of {_RUNS} after warm-up)")


def _realistic_corpus(seeds: int = 100, total: int = 1000) -> list[dict]:
    """The shape a real session leaves, at the spool's own bound.

    Deliberately the SAME workload `test_benchmark_evidence._fill_to_the_bound` builds, since
    that is the corpus OUTSTANDING-ISSUES item 4 quotes its "~69ms for a realistic session"
    against: 100 distinct statements in 100 distinct sessions, plus 900 file changes each
    carrying a REAL path so they actually attach. Ids are uuid5 like the rest of the corpus.

    Getting this wrong is not a small measurement error. A corpus whose statements all merge
    and whose file events carry no path measures the boilerplate case instead, two orders of
    magnitude off - and Task 06 states its regression delta against this row.
    """
    events = [{"schema_version": evidence.SCHEMA_VERSION,
               "event_id": str(uuid.uuid5(_GEN_NAMESPACE, f"realistic-seed#{i}")),
               "session_id": f"sess-{i}", "repo_key": "/repo", "kind": "user_directive",
               "occurred_at": (_T0 + timedelta(seconds=i)).isoformat(), "source": "replay",
               "summary": f"Decision number {i} concerns subsystem alpha{i} and its "
                          f"owner team{i}.",
               "files": [], "content_hash": None, "attributes": {}}
              for i in range(seeds)]
    events += [{"schema_version": evidence.SCHEMA_VERSION,
                "event_id": str(uuid.uuid5(_GEN_NAMESPACE, f"realistic-edit#{i}")),
                "session_id": f"sess-{i % seeds}", "repo_key": "/repo",
                "kind": "file_changed",
                "occurred_at": (_T0 + timedelta(seconds=seeds + i)).isoformat(),
                "source": "replay", "summary": f"module {i} changed",
                "files": [f"src/module_{i % seeds}.py"], "content_hash": None,
                "attributes": {}}
               for i in range(total - seeds)]
    return [_validated(e) for e in events]


@pytest.mark.perf
def test_baseline_aggregation_of_realistic_evidence():
    """A realistically shaped session at the spool's bound: 100 statements, each in its own
    session, with 900 file changes corroborating them. This is the figure the 1,000-distinct
    ceiling below should be read against, and the row Task 06 states its delta from."""
    events = _realistic_corpus()
    result = candidates.aggregate_candidates(events, [])
    # The corpus is only realistic if the support events actually attached. Asserted, because
    # a silently degenerate corpus is precisely how this row was wrong the first time.
    assert len(result["candidates"]) == 100
    assert result["diagnostics"]["merged_duplicates"] == 0
    assert sum(len(c["source_files"]) for c in result["candidates"]) == 100

    median = _median_ms(lambda: candidates.aggregate_candidates(events, []))
    _report(f"aggregate_candidates over {len(events)} realistic events", median)
    assert median < _REALISTIC_CEILING_MS


@pytest.mark.perf
def test_baseline_aggregation_of_a_thousand_distinct_statements():
    """Outstanding issue 4's ceiling, and Task 06's gate: at most 500ms at the spool's own
    bound. These 1,000 statements are mutually distinct but share four ordinary words, so
    every pair is still counted - what the token postings removed is the price of a pair
    (two tokenizations and a set intersection), not the pair count. 2667.67ms before."""
    doc = _load("14-thousand-distinct-statements")
    result = candidates.aggregate_candidates(doc["events"], [])
    assert len(result["candidates"]) == doc["expected_candidates"]
    assert result["diagnostics"]["merged_duplicates"] == doc["expected_merged"]
    median = _median_ms(lambda: candidates.aggregate_candidates(doc["events"], []))
    _report("aggregate_candidates over 1,000 distinct statements", median)
    assert median < _AGGREGATION_GATE_MS


@pytest.mark.perf
def test_baseline_aggregation_of_a_thousand_boilerplate_statements():
    """The other end of the same axis, under the same 500ms gate: 1,000 statements that all
    restate one another merge into a single group on the first comparison, so the pass is
    linear and the postings never hold more than one group. 8.65ms before."""
    doc = _load("15-thousand-boilerplate-statements")
    result = candidates.aggregate_candidates(doc["events"], [])
    assert len(result["candidates"]) == doc["expected_candidates"]
    assert result["diagnostics"]["merged_duplicates"] == doc["expected_merged"]
    median = _median_ms(lambda: candidates.aggregate_candidates(doc["events"], []))
    _report("aggregate_candidates over 1,000 boilerplate statements", median)
    assert median < _AGGREGATION_GATE_MS


@pytest.mark.perf
def test_baseline_empty_spool_reconciliation(tmp_repo):
    """The cost every session start would pay if reconciliation moved onto the shared path
    (Task 06). Two directory listings and no store read at all - this is the number that
    decides whether outstanding issue 1's fix is affordable for all four hosts."""
    median = _median_ms(lambda: reconcile.reconcile_session(tmp_repo))
    _report("reconcile_session over an empty spool", median)
    assert median < 100.0


@pytest.mark.perf
def test_baseline_one_ordinary_session_start_payload(tmp_repo):
    """The surface that fix would ride on, measured as it is today, so the added cost can be
    stated as a delta rather than as an absolute."""
    for i in range(20):
        store.update_decision(tmp_repo, f"Decision {i} governs subsystem {i} and its owner.",
                              "sess-a", "architecture", created_by="human")
    median = _median_ms(lambda: store.session_start_payload(tmp_repo, "startup", "sess-a"))
    _report("session_start_payload with 20 decisions", median)
    assert median < 2000.0
