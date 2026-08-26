"""Tests for contexer/candidates.py - deterministic grouping and scoring of evidence.

Two properties carry this module and are asserted first: the same event set in ANY input
order produces byte-identical output, and every `_SCORES` weight is explained by a test that
says what it buys at the `_MIN_CANDIDATE_SCORE` bar (the plan requires the numbers to be
inspectable, not merely present).

Summaries are written with the overlap arithmetic in mind: `retrieval.index_tokens` drops
stop words and tokens under 3 chars, so `_SEED`'s eight tokens are
{migrations, must, run, before, deploying, postgres, schema, updates} and every fixture below
is measured against that set.
"""
import json
import random
import uuid

from contexer import candidates, evidence

# 8 tokens: migrations must run before deploying postgres schema updates.
_SEED = "migrations must run before deploying postgres schema updates"
# 6 tokens each, 4 shared with _SEED -> 4/6 = 0.667: above the contradiction/retire bar (0.5),
# below the merge/duplicate bar (0.7).
_PARTIAL = "run migrations before postgres upgrades manually"
_PARTIAL_NEGATED = "never run migrations before postgres upgrades"
# 7 tokens, 6 shared with _SEED -> 6/7 = 0.857: a negating seed ABOVE the ordinary merge bar.
_NEGATED_ABOVE_MERGE = "never run migrations before deploying postgres schema"
_UNRELATED = "graphql resolvers batch loader caching layer"


def _ev(kind, summary="", *, session="s1", at="2026-08-24T10:00:00+00:00", files=None,
        attributes=None, event_id=None):
    """One evidence event. The default id is a uuid5 of the content, so an event keeps the
    same id however the test happens to build it - shuffling a list must not renumber it."""
    body = f"{kind}|{summary}|{session}|{at}|{files}|{attributes}"
    return {
        "schema_version": evidence.SCHEMA_VERSION,
        "event_id": event_id or str(uuid.uuid5(uuid.NAMESPACE_DNS, body)),
        "session_id": session,
        "repo_key": "/repo",
        "kind": kind,
        "occurred_at": at,
        "source": "test",
        "summary": summary,
        "files": files or [],
        "content_hash": None,
        "attributes": attributes or {},
    }


def _decision(did, content, *, status="approved", tombstoned=False):
    return {"id": did, "status": status, "tombstoned": tombstoned, "title": content[:60],
            "content": content, "subtype": "architecture", "source_files": [],
            "current_revision_id": f"rev-{did}"}


def _only(result):
    assert len(result["candidates"]) == 1, result["candidates"]
    return result["candidates"][0]


# ── the seam with Task 1 ─────────────────────────────────────────────────────────

def test_builder_events_are_real_validated_evidence_events():
    """The aggregator reads what `evidence.validate_event` emits - if the builder above drifts
    from that shape, every assertion below is testing a fiction."""
    normalized, errors = evidence.validate_event(
        _ev("file_changed", "touched the store", files=["contexer/store.py"]))
    assert errors == []
    assert normalized["kind"] == "file_changed"


# ── determinism ──────────────────────────────────────────────────────────────────

def test_same_events_in_any_order_produce_byte_identical_output():
    events = [
        _ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00", files=["db/migrate.py"]),
        _ev("file_changed", "migration touched", at="2026-08-24T10:01:00+00:00",
            files=["db/migrate.py"]),
        _ev("test_result", "suite green", at="2026-08-24T10:02:00+00:00",
            files=["db/migrate.py"], attributes={"status": "passed"}),
        _ev("agent_conclusion", _UNRELATED, at="2026-08-24T10:03:00+00:00"),
        _ev("policy_evaluation", "noise", at="2026-08-24T10:04:00+00:00"),
        _ev("file_changed", "unrelated edit", session="s2",
            at="2026-08-24T10:05:00+00:00", files=["web/app.ts"]),
    ]
    # dec-1 and dec-2 are the SAME content, so they tie on overlap: which one a candidate
    # targets must come from the id tie-break, never from where they sat in the list.
    decisions = [_decision("dec-0", _UNRELATED), _decision("dec-1", _PARTIAL),
                 _decision("dec-2", _PARTIAL)]
    baseline = candidates.aggregate_candidates(events, decisions)
    assert baseline["candidates"][0]["target_decision_id"] == "dec-1"
    shuffled, shuffled_decisions = list(events), list(decisions)
    for seed in range(5):
        random.Random(seed).shuffle(shuffled)
        random.Random(seed + 100).shuffle(shuffled_decisions)
        assert json.dumps(candidates.aggregate_candidates(shuffled, shuffled_decisions)) == \
            json.dumps(baseline)


def test_candidate_id_is_uuid5_over_the_kind_target_and_sorted_event_ids():
    a = _ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00", files=["db/migrate.py"])
    b = _ev("file_changed", "migration touched", at="2026-08-24T10:01:00+00:00",
            files=["db/migrate.py"])
    got = _only(candidates.aggregate_candidates([a, b], []))
    expected = uuid.uuid5(candidates._CANDIDATE_NAMESPACE, "\n".join(
        ["new", "", ",".join(sorted([a["event_id"], b["event_id"]]))]))
    assert got["candidate_id"] == str(expected)


def test_the_same_events_read_as_a_different_proposal_get_a_different_id():
    """The id names what is PROPOSED, not just what was observed. `held/<candidate-id>/` is the
    storage layer's "already pending" record, and an update and a retirement built from one
    event set are two different proposals that must not share one directory."""
    stored = _decision("dec-1", _SEED)
    seed = _ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00")
    duplicate = _only(candidates.aggregate_candidates([seed], [stored]))
    fresh = _only(candidates.aggregate_candidates([seed], []))
    assert (duplicate["kind"], fresh["kind"]) == ("duplicate", "new")
    assert duplicate["candidate_id"] != fresh["candidate_id"]


def test_candidates_are_sorted_by_score_then_id():
    events = [_ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00"),
              _ev("agent_conclusion", _UNRELATED, at="2026-08-24T10:01:00+00:00")]
    scores = [c["score"] for c in candidates.aggregate_candidates(events, [])["candidates"]]
    assert scores == [50, 15]


def test_equal_scores_break_the_tie_on_candidate_id():
    """The output order is load-bearing, so equal scores must not fall back to the order the
    groups happened to be created in - which is the arrival order of their seeds."""
    events = [_ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00"),
              _ev("user_directive", _UNRELATED, at="2026-08-24T10:01:00+00:00")]
    got = candidates.aggregate_candidates(events, [])["candidates"]
    assert [c["score"] for c in got] == [50, 50]
    ids = [c["candidate_id"] for c in got]
    assert ids == sorted(ids)
    assert ids != sorted(ids, reverse=True), "the two ids must actually differ"


# ── one test per _SCORES entry, explaining its threshold ─────────────────────────

def test_user_directive_alone_clears_the_bar():
    """50 is the only weight that carries a candidate on its own: a developer said it out
    loud, so it reaches review with no corroboration at all."""
    assert candidates._SCORES["user_directive"] == 50
    got = _only(candidates.aggregate_candidates([_ev("user_directive", _SEED)], []))
    assert got["score"] == 50
    assert got["kind"] == "new"


def test_agent_conclusion_with_rationale_exactly_meets_the_bar():
    """25 is set AT `_MIN_CANDIDATE_SCORE`: an agent that explained itself is worth surfacing
    once, and one point less would have needed corroboration to be seen at all."""
    assert candidates._SCORES["agent_conclusion_with_rationale"] == candidates._MIN_CANDIDATE_SCORE
    got = _only(candidates.aggregate_candidates(
        [_ev("agent_conclusion", "Postgres backs the ledger because replicas lag.")], []))
    assert got["score"] == 25
    assert got["kind"] == "new"


def test_bare_agent_conclusion_stays_below_the_bar():
    """15 is deliberately short of 25: an unexplained agent assertion needs a second signal
    (a repeat, a file change, a passing test) before it is worth a developer's time."""
    assert candidates._SCORES["agent_conclusion_bare"] == 15
    got = _only(candidates.aggregate_candidates(
        [_ev("agent_conclusion", "Postgres backs the ledger")], []))
    assert got["score"] == 15
    assert got["kind"] == "insufficient"


def test_repeated_independently_lifts_a_bare_conclusion_over_the_bar():
    """15 is exactly what a bare conclusion needs: the same statement reached again in a
    DIFFERENT session is the second signal 15 alone was missing."""
    assert candidates._SCORES["repeated_independently"] == 15
    events = [_ev("agent_conclusion", "Postgres backs the ledger", session="s1",
                  at="2026-08-24T10:00:00+00:00"),
              _ev("agent_conclusion", "Postgres backs the ledger", session="s2",
                  at="2026-08-24T11:00:00+00:00")]
    got = _only(candidates.aggregate_candidates(events, []))
    assert got["score"] == 30
    assert got["kind"] == "new"


def test_repeated_independently_is_awarded_once_per_distinct_extra_session():
    events = [_ev("agent_conclusion", "Postgres backs the ledger", session="s1",
                  at="2026-08-24T10:00:00+00:00"),
              _ev("agent_conclusion", "Postgres backs the ledger", session="s2",
                  at="2026-08-24T11:00:00+00:00"),
              _ev("agent_conclusion", "Postgres backs the ledger", session="s2",
                  at="2026-08-24T12:00:00+00:00"),
              _ev("agent_conclusion", "Postgres backs the ledger", session="s3",
                  at="2026-08-24T13:00:00+00:00")]
    got = _only(candidates.aggregate_candidates(events, []))
    assert got["score"] == 15 + 15 + 0 + 15
    reasons = [s["reason"] for s in got["signals"]]
    assert reasons.count("repeated independently in another session") == 2
    assert reasons.count("restated in the same session") == 1


def test_files_changed_corroborates_but_cannot_carry_a_candidate():
    """10 lifts an explained conclusion clear of the bar without ever reaching it alone - a
    file change is evidence that something happened, never evidence of what was decided."""
    assert candidates._SCORES["files_changed"] == 10
    events = [_ev("agent_conclusion", "The ledger writes atomically. Torn reads are the risk.",
                  at="2026-08-24T10:00:00+00:00", files=["contexer/evidence.py"]),
              _ev("file_changed", "sidecar write", at="2026-08-24T10:01:00+00:00",
                  files=["contexer/evidence.py"]),
              _ev("file_changed", "same file again", at="2026-08-24T10:02:00+00:00",
                  files=["contexer/evidence.py"])]
    got = _only(candidates.aggregate_candidates(events, []))
    assert got["score"] == 35, "counted once per group, not once per event"
    assert [s["reason"] for s in got["signals"]][-1] == "files already counted"


def test_tests_validate_only_counts_a_passing_run():
    """10 for a green run, 0 for anything else: a failing test is evidence the behavior is
    NOT settled, so it must not raise the candidate's rank."""
    assert candidates._SCORES["tests_validate"] == 10
    conclusion = _ev("agent_conclusion", "Postgres backs the ledger",
                     at="2026-08-24T10:00:00+00:00")
    passed = _ev("test_result", "green", at="2026-08-24T10:01:00+00:00",
                 attributes={"status": "passed"})
    failed = _ev("test_result", "red", at="2026-08-24T10:01:00+00:00",
                 attributes={"status": "failed"})
    assert _only(candidates.aggregate_candidates([conclusion, passed], []))["score"] == 25
    assert _only(candidates.aggregate_candidates([conclusion, failed], []))["score"] == 15


def test_contradiction_penalty_drops_a_directive_below_the_bar_and_is_recorded():
    """-30 is sized to sink even a user directive (50 -> 20): when a later statement negates
    the group, the aggregator must not keep ranking it as settled. The disagreement is
    surfaced as an uncertainty rather than hidden in a separate group."""
    assert candidates._SCORES["contradiction"] == -30
    events = [_ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00"),
              _ev("user_directive", _PARTIAL_NEGATED, at="2026-08-24T11:00:00+00:00")]
    result = candidates.aggregate_candidates(events, [])
    got = _only(result)
    assert got["score"] == 20
    assert got["kind"] == "insufficient"
    assert any("contradicted by a later statement" in u for u in got["uncertainties"])
    assert result["diagnostics"]["merged_duplicates"] == 1, \
        "the negating seed merged on the lowered 0.5 bar (0.667 overlap), not its own group"


def test_a_negating_seed_over_the_normal_merge_bar_is_still_a_contradiction():
    """RATIFIED behaviour, and the case that deviates from the brief's letter: 0.5 is a FLOOR
    for a negating seed, not a ceiling. `_NEGATED_ABOVE_MERGE` clears the ordinary 0.7 merge
    bar (6 of 7 tokens shared), so it would have merged anyway - a near-verbatim reversal is a
    STRONGER contradiction than a loosely-worded one, and scoring it as a plain restatement
    (+0, or worse +15 from another session) would rank a reversed decision as corroborated."""
    events = [_ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00"),
              _ev("user_directive", _NEGATED_ABOVE_MERGE, session="s2",
                  at="2026-08-24T11:00:00+00:00")]
    assert candidates._overlap(_NEGATED_ABOVE_MERGE, _SEED) > candidates._MERGE_OVERLAP
    result = candidates.aggregate_candidates(events, [])
    got = _only(result)
    assert result["diagnostics"]["merged_duplicates"] == 1, "it merges, as it did before"
    assert got["score"] == 20, "50 - 30: the penalty applies on the high-overlap route too"
    assert [s["reason"] for s in got["signals"]][-1] == "contradicts the group's statement"
    assert any("contradicted by a later statement" in u for u in got["uncertainties"])


# ── grouping ─────────────────────────────────────────────────────────────────────

def test_restated_seed_merges_and_an_unrelated_seed_does_not():
    events = [_ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00"),
              _ev("user_directive", "migrations must run before deploying postgres schema",
                  at="2026-08-24T10:01:00+00:00"),
              _ev("agent_conclusion", _UNRELATED, at="2026-08-24T10:02:00+00:00")]
    result = candidates.aggregate_candidates(events, [])
    assert len(result["candidates"]) == 2
    assert result["diagnostics"] == {"groups": 2, "seeds": 3, "support": 0, "ignored_kinds": {},
                                     "insufficient": 1, "merged_duplicates": 1}


def test_support_needs_the_same_session_and_a_shared_file():
    seed = _ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00", files=["db/migrate.py"])
    other_session = _ev("file_changed", "elsewhere", session="s2",
                        at="2026-08-24T10:01:00+00:00", files=["db/migrate.py"])
    other_file = _ev("file_changed", "unrelated file", at="2026-08-24T10:02:00+00:00",
                     files=["web/app.ts"])
    result = candidates.aggregate_candidates([seed, other_session, other_file], [])
    directive = next(c for c in result["candidates"] if c["score"] == 50)
    assert [s["event_id"] for s in directive["signals"]] == [seed["event_id"]]
    assert result["diagnostics"]["insufficient"] == 2, "both support events are leftovers"


def test_a_test_result_attaches_to_a_group_that_has_no_files_yet():
    seed = _ev("agent_conclusion", "Postgres backs the ledger", at="2026-08-24T10:00:00+00:00")
    green = _ev("test_result", "green", at="2026-08-24T10:01:00+00:00",
                attributes={"status": "passed"})
    assert _only(candidates.aggregate_candidates([seed, green], []))["score"] == 25


def test_ignored_kinds_are_counted_and_never_grouped():
    events = [_ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00"),
              _ev("policy_evaluation", "evaluated", at="2026-08-24T10:01:00+00:00"),
              _ev("session_reconcile", "reconciled", at="2026-08-24T10:03:00+00:00"),
              _ev("session_reconcile", "reconciled again", at="2026-08-24T10:04:00+00:00")]
    result = candidates.aggregate_candidates(events, [])
    got = _only(result)
    assert len(got["signals"]) == 1
    assert result["diagnostics"]["ignored_kinds"] == {
        "policy_evaluation": 1, "session_reconcile": 2}


def test_source_files_union_is_capped_at_ten():
    events = [_ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00", files=["f0.py"])]
    # Each edit shares a file with the group so far, so the chain keeps attaching.
    for i in range(12):
        events.append(_ev("file_changed", f"edit {i}", at=f"2026-08-24T10:{i + 1:02d}:00+00:00",
                          files=[f"f{i}.py", f"f{i + 1}.py"]))
    got = _only(candidates.aggregate_candidates(events, []))
    assert got["source_files"] == [f"f{i}.py" for i in range(10)]


def test_a_repeated_path_is_recorded_once():
    events = [_ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00",
                  files=["a.py", "a.py"]),
              _ev("file_changed", "edit", at="2026-08-24T10:01:00+00:00",
                  files=["a.py", "b.py", "b.py"])]
    assert _only(candidates.aggregate_candidates(events, []))["source_files"] == ["a.py", "b.py"]


def test_test_result_paths_do_not_become_source_files():
    """A test's own path names the test, not what was decided - anchoring a candidate to it
    would point review at the wrong file."""
    events = [_ev("agent_conclusion", "Postgres backs the ledger",
                  at="2026-08-24T10:00:00+00:00"),
              _ev("test_result", "green", at="2026-08-24T10:01:00+00:00",
                  files=["tests/test_store.py"], attributes={"status": "passed"})]
    assert _only(candidates.aggregate_candidates(events, []))["source_files"] == []


def test_a_decision_repeated_event_can_open_a_group_at_the_repetition_weight():
    """A repetition IS the "same conclusion repeated" signal, so it carries that weight even
    when it opens the group - and 15 alone stays below the bar, as it does anywhere else."""
    got = _only(candidates.aggregate_candidates(
        [_ev("decision_repeated", _SEED, attributes={"decision_id": "dec-1"})], []))
    assert got["score"] == candidates._SCORES["repeated_independently"]
    assert got["kind"] == "insufficient"


def test_a_summary_with_no_index_tokens_never_merges_or_matches():
    """Overlap over an empty token set is 0, not a division by zero: an all-stop-word summary
    ("do it") must start its own group and match no decision."""
    events = [_ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00"),
              _ev("user_directive", "do it", at="2026-08-24T10:01:00+00:00")]
    result = candidates.aggregate_candidates(events, [_decision("dec-1", "do it")])
    assert len(result["candidates"]) == 2
    assert all(c["target_decision_id"] is None for c in result["candidates"])


def test_a_null_attributes_field_is_read_as_empty_rather_than_raising():
    """A hand-edited or replayed ledger line can carry `"attributes": null`, and
    `.get("attributes", {})` returns None for a key that EXISTS with a null value."""
    events = [_ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00"),
              _ev("test_result", "green", at="2026-08-24T10:01:00+00:00")]
    events[1]["attributes"] = None
    assert _only(candidates.aggregate_candidates(events, []))["score"] == 50


# ── classification ───────────────────────────────────────────────────────────────

def test_new_when_nothing_stored_resembles_it():
    got = _only(candidates.aggregate_candidates([_ev("user_directive", _SEED)],
                                                [_decision("dec-1", _UNRELATED)]))
    assert (got["kind"], got["target_decision_id"]) == ("new", None)


def test_duplicate_when_a_live_decision_already_says_it():
    got = _only(candidates.aggregate_candidates([_ev("user_directive", _SEED)],
                                                [_decision("dec-1", _SEED)]))
    assert (got["kind"], got["target_decision_id"]) == ("duplicate", "dec-1")


def test_update_on_partial_overlap():
    got = _only(candidates.aggregate_candidates([_ev("user_directive", _PARTIAL)],
                                                [_decision("dec-1", _SEED)]))
    assert (got["kind"], got["target_decision_id"]) == ("update", "dec-1")


def test_update_when_a_decision_repeated_event_names_the_decision():
    events = [_ev("user_directive", _UNRELATED, at="2026-08-24T10:00:00+00:00"),
              _ev("decision_repeated", _UNRELATED, session="s2",
                  at="2026-08-24T11:00:00+00:00", attributes={"decision_id": "dec-1"})]
    got = _only(candidates.aggregate_candidates(events, [_decision("dec-1", _SEED)]))
    assert (got["kind"], got["target_decision_id"]) == ("update", "dec-1")


def test_retire_when_the_seed_negates_a_stored_decision():
    got = _only(candidates.aggregate_candidates([_ev("user_directive", _PARTIAL_NEGATED)],
                                                [_decision("dec-1", _SEED)]))
    assert (got["kind"], got["target_decision_id"]) == ("retire", "dec-1")


def test_insufficient_candidates_are_never_classified_against_a_decision():
    got = _only(candidates.aggregate_candidates(
        [_ev("agent_conclusion", _SEED)], [_decision("dec-1", _SEED)]))
    assert (got["kind"], got["target_decision_id"]) == ("insufficient", None)
    assert any("below the 25 review bar" in u for u in got["uncertainties"])


def test_support_events_with_no_seed_yield_one_insufficient_candidate_per_session():
    events = [_ev("file_changed", "edit", session="s1", at="2026-08-24T10:00:00+00:00",
                  files=["a.py"]),
              _ev("file_changed", "edit", session="s1", at="2026-08-24T10:01:00+00:00",
                  files=["b.py"]),
              _ev("file_changed", "edit", session="s2", at="2026-08-24T10:02:00+00:00",
                  files=["c.py"])]
    result = candidates.aggregate_candidates(events, [])
    assert len(result["candidates"]) == 2
    for got in result["candidates"]:
        assert got["kind"] == "insufficient"
        assert got["content"] == ""
        assert any("no stated decision" in u for u in got["uncertainties"])
    # Which of the two equal-scoring candidates sorts first is decided by the candidate id, so
    # the per-session grouping is asserted as a SET of file lists rather than by position.
    assert sorted(tuple(c["source_files"]) for c in result["candidates"]) == \
        [("a.py", "b.py"), ("c.py",)]


def test_tombstoned_and_ignored_decisions_never_match_but_are_noted():
    for decision in (_decision("dec-1", _SEED, tombstoned=True),
                     _decision("dec-1", _SEED, status="ignored")):
        got = _only(candidates.aggregate_candidates([_ev("user_directive", _SEED)], [decision]))
        assert (got["kind"], got["target_decision_id"]) == ("new", None)
        assert any("retired or ignored" in u for u in got["uncertainties"])


def test_a_live_decision_wins_over_a_retired_one_with_the_same_content():
    got = _only(candidates.aggregate_candidates(
        [_ev("user_directive", _SEED)],
        [_decision("dec-dead", _SEED, tombstoned=True), _decision("dec-live", _SEED)]))
    assert (got["kind"], got["target_decision_id"]) == ("duplicate", "dec-live")


# ── subtype ──────────────────────────────────────────────────────────────────────

def test_subtype_keyword_rules():
    def subtype(kind, summary):
        return _only(candidates.aggregate_candidates(
            [_ev(kind, summary)], []))["subtype"]

    assert subtype("user_directive", "always run the linter before pushing") == "constraint"
    assert subtype("user_directive", "run the linter before pushing") == "convention"
    assert subtype("user_directive", "postgres backs the evidence ledger") == "architecture"
    # Prescriptive shape only counts for a DIRECTIVE: an agent asserting "never" about the
    # code is describing it, not ruling on it.
    assert subtype("user_directive", "the deploy job never runs on forks") == "constraint"
    assert subtype("agent_conclusion", "the deploy job never runs on forks. Twice.") \
        == "convention"


# ── titles ───────────────────────────────────────────────────────────────────────

def test_title_is_the_seeds_first_sentence_clipped_to_a_hundred_chars():
    long_first = "postgres " * 20
    got = _only(candidates.aggregate_candidates(
        [_ev("user_directive", f"{long_first}. dropped tail")], []))
    assert len(got["title"]) == 100
    assert "dropped tail" not in got["title"]
    short = _only(candidates.aggregate_candidates(
        [_ev("user_directive", "Ship it. And explain why later.")], []))
    assert short["title"] == "Ship it."
    assert short["content"] == "Ship it. And explain why later."


# ── support attaching to a seed that names no files ──────────────────────────────

def test_a_file_change_corroborates_a_directive_that_names_no_files():
    """The reviewer's reproduction: a real `user_directive` carries no `files` at all, so a
    rule requiring a shared path could never let anything attach to it - the directive and the
    edit it prompted came back as two candidates, `new(score=50, files=[])` plus a useless
    `insufficient(score=10)`. Same session and inside the proximity window is the signal that
    replaces the file overlap a fileless seed can never have."""
    directive = _ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00")
    edit = _ev("file_changed", "auth rewritten", at="2026-08-24T10:05:00+00:00",
               files=["src/auth.py"])
    result = candidates.aggregate_candidates([directive, edit], [])
    got = _only(result)
    assert got["score"] == 50 + candidates._SCORES["files_changed"]
    assert got["source_files"] == ["src/auth.py"]
    assert result["diagnostics"]["insufficient"] == 0


def test_a_fileless_seed_keeps_taking_edits_for_the_whole_window():
    """The first edit gives the GROUP files, but the SEED still names none - so a second edit
    to an unrelated path in the same window still corroborates the same directive rather than
    opening its own leftover."""
    events = [_ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00"),
              _ev("file_changed", "auth", at="2026-08-24T10:05:00+00:00",
                  files=["src/auth.py"]),
              _ev("file_changed", "session store", at="2026-08-24T10:20:00+00:00",
                  files=["src/session.py"])]
    got = _only(candidates.aggregate_candidates(events, []))
    assert got["source_files"] == ["src/auth.py", "src/session.py"]
    assert got["score"] == 60, "files_changed still counts once per group"


def test_proximity_attachment_is_bounded_by_the_window():
    """The guard against one directive swallowing every edit in a long session: past
    `_PROXIMITY_SECONDS` from the seed, an unrelated edit is a leftover again."""
    assert candidates._PROXIMITY_SECONDS == 1800
    events = [_ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00"),
              _ev("file_changed", "much later", at="2026-08-24T11:00:00+00:00",
                  files=["src/auth.py"])]
    result = candidates.aggregate_candidates(events, [])
    directive = next(c for c in result["candidates"] if c["score"] == 50)
    assert directive["source_files"] == []
    assert result["diagnostics"]["insufficient"] == 1


def test_the_nearest_preceding_fileless_seed_takes_the_edit():
    """Two fileless directives inside one window: the edit corroborates the one it actually
    followed, not whichever group happens to sit first in the list."""
    events = [_ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00"),
              _ev("user_directive", _UNRELATED, at="2026-08-24T10:01:00+00:00"),
              _ev("file_changed", "resolver edit", at="2026-08-24T10:02:00+00:00",
                  files=["web/resolvers.ts"])]
    result = candidates.aggregate_candidates(events, [])
    by_content = {c["content"]: c for c in result["candidates"]}
    assert by_content[_UNRELATED]["source_files"] == ["web/resolvers.ts"]
    assert by_content[_SEED]["source_files"] == []


def test_proximity_attachment_survives_a_shuffled_input_order():
    """Determinism is what makes `held/<candidate-id>/` the already-pending record, and the
    new fallback picks a group by scanning the list - so it must be pinned against order."""
    events = [_ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00"),
              _ev("user_directive", _UNRELATED, at="2026-08-24T10:01:00+00:00"),
              _ev("file_changed", "resolver edit", at="2026-08-24T10:02:00+00:00",
                  files=["web/resolvers.ts"]),
              _ev("file_changed", "auth edit", at="2026-08-24T10:03:00+00:00",
                  files=["src/auth.py"])]
    baseline = candidates.aggregate_candidates(events, [])
    shuffled = list(events)
    for seed in range(5):
        random.Random(seed).shuffle(shuffled)
        assert json.dumps(candidates.aggregate_candidates(shuffled, [])) == \
            json.dumps(baseline)


# ── typed relationships (hardening Task 03) ──────────────────────────────────────

# The seed NAMES a path in its own text, which is what a real directive does ("do not edit
# src/generated/client.ts directly"), so an edit to that path is structurally linked whichever
# side of the statement it landed on.
_NAMES_A_FILE = "always regenerate src/generated/client.ts from the schema"


def _relations(candidate) -> list[tuple]:
    return [(s["relation"], s["certainty"]) for s in candidate["signals"]]


def test_every_signal_carries_a_relation_and_a_certainty_from_the_closed_vocabulary():
    """The row says WHY the event is here, not just what it was worth. A weight of 0 is
    ambiguous on its own - a corroborating event that scored nothing and one that only
    happened to be nearby read identically before this."""
    events = [_ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00",
                  files=["db/migrate.py"]),
              _ev("file_changed", "migration", at="2026-08-24T10:01:00+00:00",
                  files=["db/migrate.py"]),
              _ev("user_directive", _SEED, session="s2", at="2026-08-24T11:00:00+00:00")]
    got = _only(candidates.aggregate_candidates(events, []))
    # Statements first, then what supported them: the two-pass grouping fills the seeds before
    # it attaches support, so a merged restatement precedes an earlier file change. Both passes
    # read `_ordered`, so the order is fixed - it is just no longer strict wall-clock order.
    assert _relations(got) == [("explicit", "confirmed"), ("repetition", "supporting"),
                               ("structural", "confirmed")]
    for signal in got["signals"]:
        assert signal["relation"] in candidates.RELATIONS
        assert signal["certainty"] in candidates.CERTAINTIES


def test_an_edit_before_a_directive_that_names_its_file_is_structural():
    """Time direction is not evidence; a shared identifier is. The edit precedes the statement
    and still anchors it, because the statement names the file."""
    events = [_ev("file_changed", "regenerated", at="2026-08-24T10:00:00+00:00",
                  files=["src/generated/client.ts"]),
              _ev("user_directive", _NAMES_A_FILE, at="2026-08-24T10:01:00+00:00")]
    got = _only(candidates.aggregate_candidates(events, []))
    assert got["source_files"] == ["src/generated/client.ts"]
    assert got["possible_source_files"] == []
    assert _relations(got)[1] == ("structural", "confirmed")


def test_an_edit_before_a_directive_with_no_structural_link_is_only_possible():
    """The uncertain link, and everything it is NOT. The path is displayed as possible, the
    score does not move, and the edit is STILL reported as an unexplained change - a display
    link must not consume the evidence of the gap it failed to explain."""
    events = [_ev("file_changed", "readme typo", at="2026-08-24T10:00:00+00:00",
                  files=["README.md"]),
              _ev("user_directive", _SEED, at="2026-08-24T10:01:00+00:00")]
    result = candidates.aggregate_candidates(events, [])
    directive = next(c for c in result["candidates"] if c["kind"] == "new")
    leftover = next(c for c in result["candidates"] if c["kind"] == "insufficient")

    assert directive["score"] == 50, "an uncertain link is worth nothing"
    assert directive["source_files"] == []
    assert directive["possible_source_files"] == ["README.md"]
    assert _relations(directive) == [("explicit", "confirmed")]
    assert [(s["relation"], s["certainty"]) for s in directive["uncertain_signals"]] \
        == [("temporal_backward", "uncertain")]
    assert leftover["source_files"] == ["README.md"]


def test_an_uncertain_link_never_crosses_a_session_boundary():
    """Cross-session temporal attachment is forbidden outright: another session's edit is not
    even a possible anchor for this session's rule."""
    events = [_ev("file_changed", "readme typo", session="s2",
                  at="2026-08-24T10:00:00+00:00", files=["README.md"]),
              _ev("user_directive", _SEED, at="2026-08-24T10:01:00+00:00")]
    directive = next(c for c in candidates.aggregate_candidates(events, [])["candidates"]
                     if c["kind"] == "new")
    assert directive["possible_source_files"] == [] and directive["uncertain_signals"] == []


def test_a_shared_file_beats_proximity_when_both_groups_qualify():
    """The tie-break in the one case where the two rules disagree: the nearer group would take
    the edit on proximity, the further one owns the file. Structural wins - proximity is a
    guess about what an edit was for, a shared path is a fact about it."""
    owns_the_file = _ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00",
                        files=["db/migrate.py"])
    nearer = _ev("user_directive", _UNRELATED, at="2026-08-24T10:05:00+00:00")
    edit = _ev("file_changed", "migration again", at="2026-08-24T10:06:00+00:00",
               files=["db/migrate.py"])
    by_content = {c["content"]: c for c in candidates.aggregate_candidates(
        [owns_the_file, nearer, edit], [])["candidates"]}
    assert by_content[_SEED]["source_files"] == ["db/migrate.py"]
    assert by_content[_UNRELATED]["source_files"] == []


def test_a_prohibition_restated_is_repetition_not_a_contradiction():
    """Ledger ruling D1. `_negates` matches a bare negation word, so a prohibition repeated
    verbatim used to be charged -30 for contradicting ITSELF and sank below the review bar,
    where the same rule phrased affirmatively was corroborated. Polarity is what decides."""
    prohibition = "never run migrations before deploying postgres schema updates"
    events = [_ev("user_directive", prohibition, at="2026-08-24T10:00:00+00:00"),
              _ev("user_directive", prohibition, session="s2", at="2026-08-24T11:00:00+00:00")]
    got = _only(candidates.aggregate_candidates(events, []))
    assert got["score"] == 65 and got["kind"] == "new"
    assert _relations(got)[1] == ("repetition", "supporting")
    assert got["uncertainties"] == []


def test_a_reversal_of_a_prohibition_is_still_a_contradiction():
    """The other side of the same rule, so polarity is measured in both directions rather than
    asserted in one: dropping the prohibition IS a contradiction of it."""
    events = [_ev("user_directive", "never run migrations before deploying postgres schema",
                  at="2026-08-24T10:00:00+00:00"),
              _ev("user_directive", "migrations must run before deploying postgres schema",
                  session="s2", at="2026-08-24T11:00:00+00:00")]
    got = _only(candidates.aggregate_candidates(events, []))
    assert _relations(got)[1] == ("contradiction", "confirmed")
    assert got["score"] == 20, "a contradiction is never hidden by a high positive score"
