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
import statistics
import time
import types
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from contexer import (
    candidates,
    evidence,
    lifecycle,
    policy,
    reconcile,
    remote,
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


def test_a_restated_inactive_decision_is_classified_as_new_not_matched_back():
    """Scenarios 7 and 8 at the AGGREGATOR, which is already correct: a restatement of an
    ignored or retired decision comes back as `new` carrying a note naming what it restates.
    The defect outstanding issue 7 describes is downstream of here."""
    for name, dead_id in (("07-ignored-decision-restated-by-a-directive", "dec-ignored"),
                          ("08-tombstoned-decision-restated-by-a-directive", "dec-retired")):
        (candidate,) = _aggregate(_load(name))
        assert candidate["kind"] == "new" and candidate["target_decision_id"] is None
        assert any(dead_id in note for note in candidate["uncertainties"]), name


@pytest.mark.xfail(strict=True, reason=(
    "outstanding issue 7, agent-only half, owned by Task 04's reconsideration lane: the "
    "aggregator opens kind=new at score 25 from agent-only evidence against an inactive "
    "decision, carrying 'restates dec-ignored, which was retired or ignored - review it as "
    "new'. A restatement inside an agent_conclusion has two sentences, so `_has_rationale` "
    "scores it 25 - exactly the review bar - and it reopens the ignored decision with no "
    "developer having said anything."))
def test_agent_only_evidence_cannot_reopen_an_inactive_decision():
    """Scenario 9: the SAME inactive decision as scenarios 7 and 8, named only by an agent.

    This is scenario 7's event with `kind` flipped to `agent_conclusion`, which is the only
    way the scenario means anything - a conclusion whose wording does not restate the ignored
    decision leaves the `decisions` array inert and measures nothing.

    The property, and the one Task 04's lane is designed on: only a developer saying it out
    loud reopens an inactive decision. Asserted as "does not reopen" rather than as a golden,
    because whether agent-only evidence should come back `insufficient`, `new` without the
    reconsideration note, or in a lane of its own is Task 04's call.
    """
    doc = _load("09-inactive-decision-mentioned-by-a-conclusion")
    (candidate,) = _aggregate(doc)
    reopens = (candidate["kind"] == "new"
               and any("dec-ignored" in note for note in candidate["uncertainties"]))
    assert not reopens, (
        f"agent-only evidence reopened dec-ignored: {candidate['kind']} at "
        f"{candidate['score']} - {candidate['uncertainties']}")


def test_a_human_directive_is_what_reopens_an_inactive_decision():
    """The passing side of the distinction above, so it is measured on both ends rather than
    asserted on one. The identical restatement carried by a `user_directive` scores 50 and is
    flagged as a reconsideration of the ignored decision - which is correct and must stay."""
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

@pytest.mark.xfail(strict=True, reason=(
    "outstanding issue 6: an edit recorded BEFORE the directive that explains it does not "
    "corroborate it. Today the corpus aggregates to TWO candidates - a fileless `new` at 50 "
    "and an `insufficient` at 10 carrying src/generated/client.ts - where the same pair in "
    "the other order (scenario 1) is ONE candidate at 60 anchored on that file."))
def test_an_edit_before_its_directive_corroborates_it():
    doc = _load("02-edit-then-directive")
    assert _aggregate(doc) == doc["expected"], doc["summary"]


@pytest.mark.xfail(strict=True, reason=(
    "Task 03 carry-forward (ledger D1): a PROHIBITION repeated verbatim in a second session "
    "contradicts itself. `candidates._negates` matches the bare word `not` anywhere in the "
    "summary, and `_score_group` charges any merged seed that negates -30 without asking "
    "whether it negates the GROUP or merely restates the same prohibition - so the pair "
    "scores 50 - 30 = 20 and falls under the 25 review bar, where the identical rule phrased "
    "affirmatively (scenario 5) scores 65."))
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


@pytest.mark.xfail(strict=True, reason=(
    "outstanding issue 3: a directive that dedups against an already-stored rule emits no "
    "evidence at all. `store.capture_user_constraint` returns (None, None, None) on the "
    "restatement and `evidence.capture_directive` gates emission on a stored/updated entry, "
    "so the second call leaves the spool at exactly one event."))
def test_a_repeated_directive_leaves_an_evidence_trail(tmp_repo):
    first = evidence.capture_directive(tmp_repo, "always use conventional commits",
                                       "sess-a", "replay")
    assert first[0] is not None
    evidence.capture_directive(tmp_repo, "always use conventional commits",
                               "sess-b", "replay")
    assert len(spool.list_pending_evidence(tmp_repo)) == 2


@pytest.mark.xfail(strict=True, reason=(
    "outstanding issue 2: `agent_conclusion` has no production emitter. The kind appears "
    "only in evidence.EVENT_KINDS and in candidates' scoring table, so `user_directive` is "
    "the only seed a real session can ever produce."))
def test_some_production_module_emits_an_agent_conclusion():
    owners = {"evidence.py", "candidates.py"}
    emitters = [path.name for path in sorted(Path(store.__file__).parent.rglob("*.py"))
                if path.name not in owners
                and "agent_conclusion" in path.read_text(encoding="utf-8")]
    assert emitters, "no module outside the schema and the scoring table names the kind"


@pytest.mark.parametrize("host", ["codex", "cursor", "gemini"])
@pytest.mark.xfail(strict=True, reason=(
    "outstanding issue 1: only Claude reconciles at session start, via sync_memory. Codex "
    "and Cursor never reconcile at all and Gemini reconciles only at PreCompress/SessionEnd, "
    "so the shared store-side session-start path leaves the spooled directive pending and "
    "the store empty."))
def test_every_host_reconciles_at_session_start(tmp_repo, host):
    evidence.emit_hook_event(tmp_repo, "user_directive", session_id="sess-a",
                             source="replay", summary=RULE)
    raw = json.dumps({"session_id": "sess-a", "source": "startup",
                      "workspace_roots": [tmp_repo]})
    if host == "codex":
        # Codex's installed SessionStart hook calls exactly this, and nothing else.
        store.get_session_start_context(tmp_repo, "startup", "sess-a")
    elif host == "cursor":
        cursor.session_start(tmp_repo, raw)
    else:
        gemini.session_start(tmp_repo, raw)
    assert [store.entry_status(e) for e in store.load(tmp_repo)["entries"]] \
        == ["pending_approval"]


def test_claude_does_reconcile_at_session_start(tmp_repo):
    """The other side of the parametrized xfail above, so "every host" is measured against a
    host that already passes rather than against an assumption."""
    evidence.emit_hook_event(tmp_repo, "user_directive", session_id="sess-a",
                             source="replay", summary=RULE)
    claude.sync_memory(tmp_repo)
    assert [store.entry_status(e) for e in store.load(tmp_repo)["entries"]] \
        == ["pending_approval"]


@pytest.mark.xfail(strict=True, reason=(
    "outstanding issue 7 (ignored twin): the restatement is absorbed as a RECURRENCE of the "
    "ignored entry. The receipt says duplicates=1, occurrence_count goes to 2, the held "
    "directory ends up empty, and no evidence_summary is filed anywhere - the restated "
    "decision is never surfaced for review and its evidence is destroyed with no receipt."))
def test_a_directive_restating_an_ignored_decision_is_surfaced_for_review(tmp_repo):
    _replay_inactive_twin(tmp_repo, retire=False)


@pytest.mark.xfail(strict=True, reason=(
    "outstanding issue 7 (tombstoned twin): identical outcome via the other status-blind "
    "path. `store._is_tombstoned` matches the retired entry, update_decision_with_meta "
    "refuses the write, and reconcile settles the candidate as `dismissed` with entry_id "
    "'' - so the events are deleted and there is no decision to file the receipt against."))
def test_a_directive_restating_a_retired_decision_is_surfaced_for_review(tmp_repo):
    _replay_inactive_twin(tmp_repo, retire=True)


def _replay_inactive_twin(repo: str, *, retire: bool) -> None:
    """Store the rule, make it inactive, then restate it as session evidence.

    The requirement either way: the restatement reaches the developer. Something must be
    reviewable afterwards - a fresh pending decision, a lifecycle proposal on the dead one,
    or at the very least a held candidate still carrying its raw evidence - and a receipt
    must exist. Deliberately not pinned to ONE of those shapes: Task 04 owns the design, and
    this asserts the property rather than pre-judging the lane.
    """
    # RULE on both sides, deliberately: the defect is the STORE's own status-blind dedup
    # (`_find_match` / `_is_tombstoned`), which runs on a different tokenizer from the
    # aggregator's, so a paraphrase would land as a fresh entry and quietly test nothing.
    ok, entry_id = store.update_decision(repo, RULE, "sess-0", "constraint",
                                         created_by="human")
    assert ok and entry_id
    if retire:
        assert lifecycle.retire_decision(repo, entry_id, "superseded by the codegen step")[0]
    else:
        assert store.approve_decision(repo, entry_id, "ignore")[0]

    evidence.emit_hook_event(repo, "user_directive", session_id="sess-a", source="replay",
                             summary=RULE)
    reconcile.reconcile_session(repo)

    live = [e for e in store.load(repo)["entries"] if store.entry_status(e) != "ignored"]
    held = spool.held_candidates(repo)
    summaries = [s for e in store.load(repo)["entries"]
                 for s in e.get("evidence_summary") or []]
    assert live or held or summaries, (
        "the restatement of an inactive decision left nothing to review and no receipt")


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


def test_a_hold_whose_store_write_never_landed_keeps_its_evidence(tmp_repo):
    """Scenario 10 - crash after the hold, before the store mutation.

    The hold exists and names no `entry_id`, because nothing was stored. Invariants 3 and 4:
    the raw evidence is still there, nothing is deleted, and no disposition is invented for a
    candidate nothing can judge. It surfaces through `held_unattributed` instead.
    """
    doc = _load("10-crash-after-hold-before-store-mutation")
    event_ids = _spool_events(tmp_repo, doc)
    candidate_id = str(uuid.uuid5(uuid.NAMESPACE_URL, "orphan-hold"))
    spool.hold_candidate_evidence(tmp_repo, candidate_id, event_ids,
                                  meta={"event_ids": event_ids, "status": "pending"})

    receipt = reconcile.reconcile_session(tmp_repo)

    assert receipt["proposed"] == 0 and receipt["duplicates"] == 0
    assert len(_held_event_files(tmp_repo, candidate_id)) == len(event_ids)
    assert store.load(tmp_repo)["entries"] == []
    # `held_unattributed`, not `held`: the weaker counter is satisfied by an ordinary hold
    # that names an entry, which is exactly the state this scenario is NOT in.
    assert spool.evidence_diagnostics(tmp_repo)["held_unattributed"] == 1


def test_a_decision_stored_before_its_hold_is_held_against_its_own_review(tmp_repo,
                                                                         monkeypatch):
    """Scenario 11 - crash after the store mutation, before the hold.

    The decision exists and every event is still pending, so the next pass re-aggregates them
    and the aggregator matches them onto the decision they just became. Settling that as a
    duplicate would delete the only evidence for a decision nobody has reviewed, so it is
    HELD against that decision instead and reported as `already_pending`.
    """
    doc = _load("11-crash-after-store-mutation-before-hold")
    _spool_events(tmp_repo, doc)
    # A nested context, never `monkeypatch.undo()`: undo is per-INSTANCE, and `tmp_repo`
    # redirects `store.STORE_DIR` through this same instance - so undoing here would point
    # the rest of the test at the developer's real store dir.
    with monkeypatch.context() as crashed:
        crashed.setattr(spool, "hold_candidate_evidence", _crash)
        reconcile.reconcile_session(tmp_repo)

    entries = store.load(tmp_repo)["entries"]
    assert [store.entry_status(e) for e in entries] == ["pending_approval"]

    receipt = reconcile.reconcile_session(tmp_repo)

    assert receipt["already_pending"] == 1 and receipt["duplicates"] == 0
    assert len(store.load(tmp_repo)["entries"]) == 1, "the same decision was proposed twice"
    ((candidate_id, meta),) = spool.held_candidates(tmp_repo).items()
    assert meta["entry_id"] == entries[0]["id"]
    assert _held_event_files(tmp_repo, candidate_id), "the raw evidence was thrown away"


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
    """Scenario 16, and runbook invariant 11. `_WIRE_LIFECYCLE` is opened here because the
    mechanism under test is the NEGOTIATION - the shipped constant is pinned separately
    below, and outstanding issue 5 is why it stays closed until a live validation."""
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


def test_the_lifecycle_wire_gate_ships_closed(monkeypatch):
    """Outstanding issue 5, pinned rather than re-raised as a bug. The field spellings are
    guesses, so an advertising server changes nothing until a human validates them live."""
    assert remote._WIRE_LIFECYCLE is False
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
    assert median < 2000.0


@pytest.mark.perf
def test_baseline_aggregation_of_a_thousand_distinct_statements():
    """Outstanding issue 4's ceiling, measured rather than asserted away: grouping compares
    each seed against every group opened so far, so a corpus of entirely distinct statements
    is O(N^2) token-overlap comparisons."""
    doc = _load("14-thousand-distinct-statements")
    result = candidates.aggregate_candidates(doc["events"], [])
    assert len(result["candidates"]) == doc["expected_candidates"]
    assert result["diagnostics"]["merged_duplicates"] == doc["expected_merged"]
    median = _median_ms(lambda: candidates.aggregate_candidates(doc["events"], []))
    _report("aggregate_candidates over 1,000 distinct statements", median)
    assert median < 30000.0


@pytest.mark.perf
def test_baseline_aggregation_of_a_thousand_boilerplate_statements():
    """The other end of the same axis: 1,000 statements that all restate one another merge
    into a single group on the first comparison, so the pass is linear."""
    doc = _load("15-thousand-boilerplate-statements")
    result = candidates.aggregate_candidates(doc["events"], [])
    assert len(result["candidates"]) == doc["expected_candidates"]
    assert result["diagnostics"]["merged_duplicates"] == doc["expected_merged"]
    median = _median_ms(lambda: candidates.aggregate_candidates(doc["events"], []))
    _report("aggregate_candidates over 1,000 boilerplate statements", median)
    assert median < 30000.0


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
