"""Tests for contexer/share_status.py - the share/reconcile outcome types and their renderer.

A pure leaf: no store, no repo, no network, no fixtures. That is the point of extracting it.
`share.py` sat at 76% coverage against 96-99% for its neighbours precisely because its newest
code returned English sentences, and a sentence is expensive to assert.

The load-bearing test here is `test_every_outcome_renders`: an outcome constant with no arm in
`describe` falls through to the fallback sentence, which is a silent defect - the caller still
gets a string, so nothing raises and nothing looks wrong.
"""
import pytest

from contexer import share_status
from contexer.share_status import ReconcileStatus, ShareStatus, describe

# The two families, listed by hand. Mirrors the `test_sidecars.py` precedent: a declaration is
# only checkable against a second, independent list. `test_no_outcome_is_missing_from_this_file`
# is what keeps the two in agreement, so a new constant cannot slip past the checks below.
SHARE_OUTCOMES = [
    share_status.NOT_TEAM_MODE, share_status.NOTHING_TO_SHARE, share_status.NO_MATCH,
    share_status.SYNCED, share_status.QUEUED, share_status.NOT_QUEUED,
    share_status.BATCH_DONE, share_status.BATCH_INTERRUPTED, share_status.BATCH_STRANDED,
]
RECONCILE_OUTCOMES = [
    share_status.NO_LOCAL_MATCH, share_status.NOT_LOGGED_IN, share_status.NO_TEAMS,
    share_status.TEAM_AMBIGUOUS, share_status.TEAM_UNKNOWN, share_status.TEAM_CHOICE_REQUIRED,
    share_status.NO_TEAM_DISCOVERY, share_status.TEAM_LIST_FAILED,
    share_status.CAPABILITIES_FAILED, share_status.NO_REVISION_ID, share_status.PREVIEW_FAILED,
    share_status.SUBMITTED, share_status.HEADS_CHANGED, share_status.NEEDS_REBASE,
    share_status.UNCHANGED, share_status.ALREADY_PENDING, share_status.QUOTA_EXCEEDED,
    share_status.SERVICE_REFUSED, share_status.UNKNOWN_RESULT, share_status.SUBMISSION_REFUSED,
    share_status.UNREACHABLE_QUEUED, share_status.UNREACHABLE_NOT_QUEUED,
    share_status.RATE_LIMITED_QUEUED, share_status.RATE_LIMITED_NOT_QUEUED,
    share_status.COMPAT_SYNC_FAILED, share_status.COMPAT_SUBMIT_FAILED,
]


def _declared_outcomes() -> set:
    """Every public outcome token the module defines, found rather than listed."""
    return {v for k, v in vars(share_status).items()
            if k.isupper() and not k.startswith("_") and isinstance(v, str)}


def test_no_outcome_is_missing_from_this_file():
    assert _declared_outcomes() == set(SHARE_OUTCOMES) | set(RECONCILE_OUTCOMES)


@pytest.mark.parametrize("outcome", SHARE_OUTCOMES)
def test_every_share_outcome_renders(outcome):
    text = describe(ShareStatus(outcome))
    assert text and "unrecognised" not in text


@pytest.mark.parametrize("outcome", RECONCILE_OUTCOMES)
def test_every_reconcile_outcome_renders(outcome):
    text = describe(ReconcileStatus(outcome, team_name="Platform"))
    assert text and "unrecognised" not in text


def test_an_unknown_outcome_says_so_rather_than_raising():
    """A renderer that crashed on an unexpected token would take down a share that SUCCEEDED,
    so the fallback is deliberate. It names the token instead of inventing a result."""
    assert "unrecognised" in describe(ShareStatus("who_knows"))
    assert "unrecognised" in describe(ReconcileStatus("who_knows"))
    assert "Nothing was treated as submitted" in describe(ReconcileStatus(""))


# ── the counts, which is the whole point ──────────────────────────────────────

def test_batch_done_reports_each_count_separately():
    text = describe(ShareStatus(share_status.BATCH_DONE, sent=4, at_capacity=2, invalid=1,
                                lost=3, total=10))
    assert "Synced 4 decision(s)" in text
    assert "2 could not be stored" in text
    assert "1 were rejected" in text
    assert "3 could NOT be queued" in text


def test_batch_done_mentions_only_the_counts_that_happened():
    text = describe(ShareStatus(share_status.BATCH_DONE, sent=3, total=3))
    assert "capacity" not in text and "rejected" not in text and "unsaved" not in text


def test_lost_is_readable_without_reading_the_sentence():
    """The count a caller could never get at: neither stored remotely nor queued."""
    status = ShareStatus(share_status.BATCH_STRANDED, sent=1, queued=1, lost=3, total=5)
    assert status.lost == 3
    text = describe(status)
    assert "Queued 1 for retry before the outbox write failed" in text
    assert "3 could NOT be queued" in text


@pytest.mark.parametrize("outcome", [share_status.BATCH_DONE,
                                     share_status.BATCH_INTERRUPTED,
                                     share_status.BATCH_STRANDED])
def test_every_batch_outcome_says_when_a_decision_was_lost(outcome):
    """The sentence must not contradict the fields. `_shortfall` used to belong to BATCH_DONE
    alone, so an interrupted batch rendered "the rest are queued" while its own `lost` count said
    a decision was recorded nowhere - the fields were right and the prose was still wrong."""
    text = describe(ShareStatus(outcome, sent=1, queued=1, lost=2, total=4))
    assert "2 could NOT be queued" in text
    assert "unsaved" in text


@pytest.mark.parametrize("outcome", [share_status.BATCH_INTERRUPTED,
                                     share_status.BATCH_STRANDED])
def test_an_interrupted_batch_reports_earlier_capacity_and_invalid_counts(outcome):
    """These are accumulated before the failing chunk and carried through the early return, so
    they have to render too."""
    text = describe(ShareStatus(outcome, sent=1, queued=1, at_capacity=2, invalid=3, total=7))
    assert "2 could not be stored" in text
    assert "3 were rejected" in text


def test_the_lost_clause_does_not_blame_capacity():
    """A lost decision can come from the interrupted path, where the outbox write failed and
    capacity never came into it, so the clause must not say "at capacity"."""
    text = describe(ShareStatus(share_status.BATCH_INTERRUPTED, lost=1, total=1))
    assert "could NOT be queued" in text
    assert "at capacity could NOT" not in text


def test_compat_sync_failure_without_a_share_does_not_borrow_a_false_reason():
    """It rendered the not-in-team-mode sentence, naming a cause nothing had established."""
    text = describe(ReconcileStatus(share_status.COMPAT_SYNC_FAILED))
    assert "was not recorded" in text
    assert "Not in team mode" not in text


def test_nothing_to_share_names_the_store_it_looked_in():
    assert "no local decisions" in describe(ShareStatus(share_status.NOTHING_TO_SHARE))
    assert "no global rules" in describe(
        ShareStatus(share_status.NOTHING_TO_SHARE, scope="global"))


def test_unknown_ids_prefix_every_batch_outcome():
    for outcome in (share_status.BATCH_DONE, share_status.BATCH_INTERRUPTED,
                    share_status.BATCH_STRANDED):
        text = describe(ShareStatus(outcome, unknown_ids=("bad99999", "worse111"), total=2))
        assert text.startswith("Skipped 2 unknown id(s): bad99999, worse111.\n")


def test_unknown_ids_are_clipped_to_eight_characters():
    text = describe(ShareStatus(share_status.NO_MATCH, unknown_ids=("0123456789abcdef",)))
    assert "01234567" in text and "89abcdef" not in text


def test_with_unknown_leaves_a_status_alone_when_nothing_was_unknown():
    original = ShareStatus(share_status.BATCH_DONE, sent=1)
    assert share_status.with_unknown(original, []) is original


def test_with_unknown_records_the_ids_as_strings():
    attached = share_status.with_unknown(ShareStatus(share_status.BATCH_DONE), ["a", 7])
    assert attached.unknown_ids == ("a", "7")
    assert attached.sent == 0      # nothing else is disturbed


# ── reconciliation detail ─────────────────────────────────────────────────────

def test_submitted_renders_the_same_sentence_for_both_server_generations():
    """The atomic path and the old two-step path built this sentence separately and identically.
    One outcome now serves both, which is why the compat branch could be deleted."""
    atomic = ReconcileStatus(share_status.SUBMITTED, team_name="Platform",
                             candidate_id="cand-1", noun="update")
    compat = ReconcileStatus(share_status.SUBMITTED, team_name="Platform",
                             candidate_id="cand-1", noun="update")
    assert describe(atomic) == describe(compat)
    assert "Submitted update cand-1 to Platform for lead review." in describe(atomic)


def test_submitted_marks_an_idempotent_replay():
    text = describe(ReconcileStatus(share_status.SUBMITTED, team_name="P", candidate_id="c",
                                    replayed=True))
    assert "confirmed from an idempotent retry" in text


def test_team_choices_are_listed_from_the_rows_not_rebuilt():
    text = describe(ReconcileStatus(share_status.TEAM_CHOICE_REQUIRED,
                                    teams=("Platform (t-1)", "Security (t-2)")))
    assert "Platform (t-1), Security (t-2)" in text


def test_the_service_reason_reaches_the_reader():
    for outcome in (share_status.CAPABILITIES_FAILED, share_status.PREVIEW_FAILED,
                    share_status.SUBMISSION_REFUSED, share_status.UNREACHABLE_QUEUED):
        assert "boom" in describe(ReconcileStatus(outcome, detail="boom"))


def test_compat_sync_failure_delegates_to_the_share_it_degraded_into():
    inner = ShareStatus(share_status.NOT_QUEUED, lost=1, total=1)
    text = describe(ReconcileStatus(share_status.COMPAT_SYNC_FAILED, share=inner))
    assert text == describe(inner)


def test_compat_sync_failure_without_a_share_does_not_crash():
    assert describe(ReconcileStatus(share_status.COMPAT_SYNC_FAILED))


# ── is_ok ─────────────────────────────────────────────────────────────────────

def test_every_ok_outcome_is_a_declared_outcome():
    """`_OK_OUTCOMES` is a third hand-kept enumeration of the same tokens. Nothing checked it, so
    a typo would silently make an outcome not-ok forever."""
    assert share_status._OK_OUTCOMES <= _declared_outcomes()


def test_is_ok_covers_the_outcomes_that_put_the_decision_somewhere():
    assert share_status.is_ok(ShareStatus(share_status.SYNCED))
    assert share_status.is_ok(ShareStatus(share_status.QUEUED))
    assert share_status.is_ok(ReconcileStatus(share_status.SUBMITTED))
    assert share_status.is_ok(ReconcileStatus(share_status.RATE_LIMITED_QUEUED))


def test_is_ok_rejects_the_outcomes_that_stored_nothing():
    for outcome in (share_status.NOT_TEAM_MODE, share_status.NO_MATCH,
                    share_status.NOTHING_TO_SHARE, share_status.NOT_QUEUED):
        assert not share_status.is_ok(ShareStatus(outcome))
    for outcome in (share_status.HEADS_CHANGED, share_status.SUBMISSION_REFUSED,
                    share_status.RATE_LIMITED_NOT_QUEUED):
        assert not share_status.is_ok(ReconcileStatus(outcome))


def test_a_batch_with_losses_is_still_ok_and_the_counts_carry_the_shortfall():
    """`is_ok` is not "nothing went wrong". The batch ran; `lost` says what it cost."""
    status = ShareStatus(share_status.BATCH_DONE, sent=8, lost=2, total=10)
    assert share_status.is_ok(status) and status.lost == 2


def test_statuses_are_frozen_so_a_caller_cannot_rewrite_a_result():
    with pytest.raises(Exception):
        ShareStatus(share_status.SYNCED).sent = 99
