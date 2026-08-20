"""Unit tests for the pure team-reconciliation state transitions."""

from contexer import reconciliation


def _proposal(head="h1"):
    return {
        "content": "team wording",
        "team_reconciliation": {"team_id": "t1", "team_head": head},
    }


def test_proposal_origin_accepts_mapping_and_rejects_unrelated_values():
    assert reconciliation.proposal_origin(_proposal()) == {
        "team_id": "t1", "team_head": "h1"}
    assert reconciliation.proposal_origin({"team_reconciliation": "bad"}) is None
    assert reconciliation.proposal_origin(None) is None


def test_attach_proposal_adds_a_new_team_head():
    entry = {}
    accepted, changed = reconciliation.attach_proposal(
        entry, _proposal(), current_content="local", normalized_content="team", team_head="h1")
    assert (accepted, changed) == (True, True)
    assert entry["proposed_revision"] == _proposal()


def test_attach_proposal_replays_same_pending_head_but_preserves_other_slots():
    entry = {"proposed_revision": _proposal()}
    assert reconciliation.attach_proposal(
        entry, _proposal(), current_content="local", normalized_content="team",
        team_head="h1") == (True, False)
    assert reconciliation.attach_proposal(
        entry, _proposal("h2"), current_content="local", normalized_content="team",
        team_head="h2") == (False, False)

    unrelated = {"proposed_revision": {"content": "local suggestion"}}
    assert reconciliation.attach_proposal(
        unrelated, _proposal(), current_content="local", normalized_content="team",
        team_head="h1") == (False, False)


def test_attach_proposal_consumes_completed_heads_and_matching_content():
    for outcome in ("approved", "dismissed"):
        entry = {"last_team_reconciliation": {"team_head": "h1", "outcome": outcome}}
        assert reconciliation.attach_proposal(
            entry, _proposal(), current_content="local edit", normalized_content="team",
            team_head="h1") == (True, False)

    entry = {"last_team_reconciliation": {"team_head": "h1", "outcome": "in_sync"}}
    assert reconciliation.attach_proposal(
        entry, _proposal(), current_content="team", normalized_content="team",
        team_head="h1") == (True, False)


def test_clear_proposal_records_convergence_and_rejects_stale_or_local_proposals():
    entry = {"proposed_revision": _proposal(), "conflict_memo": {"choice": "update"}}
    assert reconciliation.clear_proposal(entry, team_head="h1", at="now")
    assert "proposed_revision" not in entry
    assert "conflict_memo" not in entry
    assert entry["last_team_reconciliation"] == {
        "team_id": "t1", "team_head": "h1", "outcome": "in_sync", "at": "now"}

    stale = {"proposed_revision": _proposal("h2")}
    assert not reconciliation.clear_proposal(stale, team_head="h1", at="now")
    assert stale["proposed_revision"] == _proposal("h2")
    assert not reconciliation.clear_proposal(
        {"proposed_revision": {"content": "local"}}, team_head="", at="now")


def test_record_outcome_only_stamps_team_proposals():
    entry = {}
    assert reconciliation.record_outcome(
        entry, _proposal(), outcome="approved", at="now")
    assert entry["last_team_reconciliation"] == {
        "team_id": "t1", "team_head": "h1", "outcome": "approved", "at": "now"}

    untouched = {}
    assert not reconciliation.record_outcome(
        untouched, {"content": "local"}, outcome="dismissed", at="now")
    assert untouched == {}
