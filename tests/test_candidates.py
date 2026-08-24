"""Tests for contexer/candidates.py — deterministic grouping and scoring of evidence.

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
    same id however the test happens to build it — shuffling a list must not renumber it."""
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
    """The aggregator reads what `evidence.validate_event` emits — if the builder above drifts
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


def test_candidate_id_is_uuid5_over_the_sorted_contributing_event_ids():
    a = _ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00", files=["db/migrate.py"])
    b = _ev("file_changed", "migration touched", at="2026-08-24T10:01:00+00:00",
            files=["db/migrate.py"])
    got = _only(candidates.aggregate_candidates([a, b], []))
    expected = uuid.uuid5(candidates._CANDIDATE_NAMESPACE,
                          ",".join(sorted([a["event_id"], b["event_id"]])))
    assert got["candidate_id"] == str(expected)


def test_candidates_are_sorted_by_score_then_id():
    events = [_ev("user_directive", _SEED, at="2026-08-24T10:00:00+00:00"),
              _ev("agent_conclusion", _UNRELATED, at="2026-08-24T10:01:00+00:00")]
    scores = [c["score"] for c in candidates.aggregate_candidates(events, [])["candidates"]]
    assert scores == [50, 15]


def test_equal_scores_break_the_tie_on_candidate_id():
    """The output order is load-bearing, so equal scores must not fall back to the order the
    groups happened to be created in — which is the arrival order of their seeds."""
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
    """10 lifts an explained conclusion clear of the bar without ever reaching it alone — a
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
    bar (6 of 7 tokens shared), so it would have merged anyway — a near-verbatim reversal is a
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
              _ev("candidate_disposition", "approved", at="2026-08-24T10:02:00+00:00"),
              _ev("session_reconcile", "reconciled", at="2026-08-24T10:03:00+00:00"),
              _ev("session_reconcile", "reconciled again", at="2026-08-24T10:04:00+00:00")]
    result = candidates.aggregate_candidates(events, [])
    got = _only(result)
    assert len(got["signals"]) == 1
    assert result["diagnostics"]["ignored_kinds"] == {
        "policy_evaluation": 1, "candidate_disposition": 1, "session_reconcile": 2}


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
    """A test's own path names the test, not what was decided — anchoring a candidate to it
    would point review at the wrong file."""
    events = [_ev("agent_conclusion", "Postgres backs the ledger",
                  at="2026-08-24T10:00:00+00:00"),
              _ev("test_result", "green", at="2026-08-24T10:01:00+00:00",
                  files=["tests/test_store.py"], attributes={"status": "passed"})]
    assert _only(candidates.aggregate_candidates(events, []))["source_files"] == []


def test_a_decision_repeated_event_can_open_a_group_at_the_repetition_weight():
    """A repetition IS the "same conclusion repeated" signal, so it carries that weight even
    when it opens the group — and 15 alone stays below the bar, as it does anywhere else."""
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
    assert result["candidates"][0]["source_files"] == ["a.py", "b.py"]


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
