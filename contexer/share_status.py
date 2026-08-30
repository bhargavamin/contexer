"""What a share or a reconciliation DID, as data, plus the one place it is put into words.

`share.py` knows exactly what happened to every decision it handled: how many the team service
saved, how many it refused as invalid, how many were queued because the context is at capacity,
and how many could not even be queued and are therefore unsaved. It computed all four counts and
then returned one English sentence, so no caller could read any of them. `cli.py` printed the
sentence, `ui/api.py` shipped the same sentence as a JSON field named `message`, and a test could
only match words - which is why `assert "3" in msg` was a normal assertion in this area, and why
`share.py` was the worst-covered module in the package.

So the counts cross the seam instead. `share.py` returns a `ShareStatus` or a `ReconcileStatus`,
and `describe` renders one into English.

**Why the renderer is here and not in each front end.** The two front ends decide how to PRESENT
a result: the CLI prints a sentence, the console returns fields a page can lay out itself. What
they must not do is each keep their own copy of forty-five sentences, which would guarantee
drift. So the default English lives here, once, and a front end that wants something else already
has the fields to build it from. The seam is real rather than hypothetical: two adapters sit at
it today.

**`outcome` is the discriminator** and is a stable machine token, never a display string. Every
other field is evidence for it, so a caller reads `status.lost` rather than looking for the word
"unsaved". A pure leaf: stdlib only, imports nothing from this package, so it is testable with no
store, no repo and no network.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

# ── share outcomes ────────────────────────────────────────────────────────────────
# A push was possible but nothing was configured or found.
NOT_TEAM_MODE = "not_team_mode"          # no endpoint/token: nothing was attempted
NOTHING_TO_SHARE = "nothing_to_share"    # the store held nothing to send (see `scope`)
NO_MATCH = "no_match"                    # the requested id(s) resolved to no local decision
# One decision.
SYNCED = "synced"                        # the service saved it (`server_id`)
QUEUED = "queued"                        # the push failed; it is queued and will retry
NOT_QUEUED = "not_queued"                # the push failed AND the queue write failed: unsaved
# A batch.
BATCH_DONE = "batch_done"                # every chunk reached the service; see the four counts
BATCH_INTERRUPTED = "batch_interrupted"  # a chunk failed; it and the rest are queued
BATCH_STRANDED = "batch_stranded"        # a chunk failed and the queue write failed partway

# ── reconciliation outcomes ───────────────────────────────────────────────────────
# Nothing was submitted; the target could not be resolved.
NO_LOCAL_MATCH = "no_local_match"
NOT_LOGGED_IN = "not_logged_in"
NO_TEAMS = "no_teams"
TEAM_AMBIGUOUS = "team_ambiguous"                  # `detail` is what was asked for
TEAM_UNKNOWN = "team_unknown"                      # `detail` asked for, `teams` available
TEAM_CHOICE_REQUIRED = "team_choice_required"      # `teams` available
NO_TEAM_DISCOVERY = "no_team_discovery"
TEAM_LIST_FAILED = "team_list_failed"
CAPABILITIES_FAILED = "capabilities_failed"        # `detail` is the service's reason
NO_REVISION_ID = "no_revision_id"
PREVIEW_FAILED = "preview_failed"                  # `detail` is the service's reason
# The service answered a submission.
SUBMITTED = "submitted"                            # `candidate_id`, `noun`, `replayed`
HEADS_CHANGED = "heads_changed"
NEEDS_REBASE = "needs_rebase"
UNCHANGED = "unchanged"
ALREADY_PENDING = "already_pending"
QUOTA_EXCEEDED = "quota_exceeded"
SERVICE_REFUSED = "service_refused"                # `server_status` names which refusal
UNKNOWN_RESULT = "unknown_result"                  # `server_status` verbatim, possibly empty
SUBMISSION_REFUSED = "submission_refused"          # raised a terminal error; `detail`
# The confirmed operation could not be sent now. Queued (or not) for automatic retry.
UNREACHABLE_QUEUED = "unreachable_queued"          # `detail` is the transport reason
UNREACHABLE_NOT_QUEUED = "unreachable_not_queued"
RATE_LIMITED_QUEUED = "rate_limited_queued"
RATE_LIMITED_NOT_QUEUED = "rate_limited_not_queued"
# Old-server two-step path (no atomic submit).
COMPAT_SYNC_FAILED = "compat_sync_failed"          # `share` carries the personal-push outcome
COMPAT_SUBMIT_FAILED = "compat_submit_failed"      # personal sync landed, team submit did not

_NOT_TEAM_MODE_TEXT = ("Not in team mode. Set mode='team' + endpoint + token in "
                       "~/.contexer/config.toml to share.")
_RATE_LIMIT_QUEUED_TEXT = "The service rate limit was reached; the confirmed submission is queued."
_RATE_LIMIT_STRANDED_TEXT = ("The service rate limit was reached, and the confirmed operation "
                             "could not be written to the retry queue. Rerun reconcile later.")
_TEAM_VERSION_TEXT = "The currently approved team version remains active until it is approved."


@dataclass(frozen=True)
class ShareStatus:
    """The outcome of one share, one batch share, or one global-rules share.

    Every count is a number of DECISIONS, and they partition what was handled:
    `sent + at_capacity + invalid + contested + lost + queued` is what the operation accounted for.

    - `sent`         the service saved it
    - `at_capacity`  the service had no room, so it was queued and will retry
    - `invalid`      the service rejected it permanently, so it was dropped, never retried
    - `contested`    a lifecycle event id belongs to another decision, so nothing was saved
    - `lost`         it is neither stored remotely NOR queued: its share intent is gone
    - `queued`       it was queued without being offered to the service (a chunk failed first)

    `lifecycle_pending` and `lifecycle_lost` describe the optional lifecycle delta after the
    base decision synced; they do not participate in the base-decision partition above.

    `lost` is the one a caller most needs and could never read before. `unknown_ids` are ids the
    caller asked for that match no local decision; they are reported rather than dropped.
    """

    outcome: str
    sent: int = 0
    queued: int = 0
    at_capacity: int = 0
    invalid: int = 0
    contested: int = 0
    lost: int = 0
    lifecycle_pending: int = 0
    lifecycle_lost: int = 0
    total: int = 0
    unknown_ids: tuple[str, ...] = ()
    server_id: str = ""
    scope: str = "repo"      # "repo" | "global" - which store was asked for


@dataclass(frozen=True)
class ReconcileStatus:
    """The outcome of preparing or submitting one reconciliation.

    `share` is set only on `COMPAT_SYNC_FAILED`, where the old-server path degrades into an
    ordinary personal push and that push's own outcome is the whole story. Carrying it as a field
    keeps one return type per function instead of a union of two result types.
    """

    outcome: str
    team_name: str = ""
    candidate_id: str = ""
    server_status: str = ""
    noun: str = "decision"       # "decision" | "update"
    replayed: bool = False
    detail: str = ""             # the service's own reason, or the team the caller asked for
    teams: tuple[str, ...] = ()  # "Name (id)" rows, for the pick-a-team outcomes
    share: ShareStatus | None = None


def with_unknown(status: ShareStatus, unknown_ids) -> ShareStatus:
    """Attach the ids a caller asked for that matched no local decision.

    The batch helpers do not know what the caller typed, and the caller does not know what the
    batch did, so the two facts are joined here rather than by prefixing a note onto a finished
    sentence (which is how it worked when the sentence WAS the return value)."""
    if not unknown_ids:
        return status
    return replace(status, unknown_ids=tuple(str(i) for i in unknown_ids))


def is_ok(status: ShareStatus | ReconcileStatus) -> bool:
    """True when the operation put something where the caller wanted it.

    Deliberately not "nothing went wrong": `BATCH_DONE` is ok even with `invalid` or `lost`
    rows, because the batch ran and the counts say what happened to each decision. A caller that
    cares about the shortfall reads the counts."""
    return status.outcome in _OK_OUTCOMES


_OK_OUTCOMES = frozenset({
    SYNCED, QUEUED, BATCH_DONE, BATCH_INTERRUPTED, BATCH_STRANDED,
    SUBMITTED, UNCHANGED, ALREADY_PENDING,
    UNREACHABLE_QUEUED, RATE_LIMITED_QUEUED,
})


def describe(status: ShareStatus | ReconcileStatus) -> str:
    """Render one outcome as the sentence a person reads. The only prose in this area."""
    if isinstance(status, ReconcileStatus):
        return _describe_reconcile(status)
    return _describe_share(status)


def _short_ids(unknown_ids: tuple[str, ...]) -> str:
    """Ids as the 8-char prefixes every human-facing surface in this repo renders."""
    return ", ".join(str(i)[:8] for i in unknown_ids)


def _unknown_prefix(unknown_ids: tuple[str, ...]) -> str:
    if not unknown_ids:
        return ""
    return f"Skipped {len(unknown_ids)} unknown id(s): {_short_ids(unknown_ids)}.\n"


def _shortfall(s: ShareStatus) -> str:
    """The clauses naming every decision that did not simply sync.

    Shared by all three batch outcomes. It used to belong to BATCH_DONE alone, so an interrupted
    batch rendered "the rest are queued" while its own `lost` count said otherwise - the fields
    were fixed and the sentence was still wrong. Any outcome that can carry a shortfall now says
    so. The `lost` clause deliberately does NOT say "at capacity" any more: a lost decision can
    also come from the interrupted path, where the outbox write failed and capacity never came
    into it."""
    out = ""
    if s.at_capacity:
        out += (f"; {s.at_capacity} could not be stored (context at capacity) and were "
                "queued - delete some decisions to sync them")
    if s.invalid:
        out += (f"; {s.invalid} were rejected by the server (unsupported type or content) "
                "and skipped")
    if s.contested:
        out += (f"; {s.contested} were refused because a lifecycle event id is already recorded "
                "against another decision, and were skipped - nothing of them was saved")
    if s.lost:
        out += f"; {s.lost} could NOT be queued (outbox write failed) and are unsaved"
    if s.lifecycle_pending:
        out += (f"; {s.lifecycle_pending} lifecycle update(s) were refused and remain queued "
                "until the server protocol changes")
    if s.lifecycle_lost:
        out += (f"; {s.lifecycle_lost} lifecycle update(s) were refused and could NOT be queued "
                "- re-share after the server supports them")
    return out


def _describe_share(s: ShareStatus) -> str:
    if s.outcome == NOT_TEAM_MODE:
        return _NOT_TEAM_MODE_TEXT
    if s.outcome == NOTHING_TO_SHARE:
        what = "global rules" if s.scope == "global" else "local decisions"
        return f"Nothing to share: no {what}."
    if s.outcome == NO_MATCH:
        if s.unknown_ids:
            shown = ", ".join(str(i)[:8] for i in s.unknown_ids)
            return f"Nothing to share: no matching local decision (unknown id(s): {shown})."
        return "Nothing to share: no matching local decision."
    if s.outcome == SYNCED:
        return (f"Synced decision to your personal cloud context (server id={s.server_id})"
                f"{_shortfall(s)} - teammates won't see this until team promotion ships.")
    if s.outcome == QUEUED:
        return ("Share failed (see the warning above for why). Queued - it will retry "
                "automatically at the next session start.")
    if s.outcome == NOT_QUEUED:
        return "Share failed (see the warning above for why). Your local decision is unchanged."
    if s.outcome == BATCH_DONE:
        return _unknown_prefix(s.unknown_ids) + (
            f"Synced {s.sent} decision(s) to your personal cloud context" + _shortfall(s)
            + " - teammates won't see these until team promotion ships.")
    if s.outcome == BATCH_INTERRUPTED:
        return _unknown_prefix(s.unknown_ids) + (
            f"Shared {s.sent} of {s.total} decision(s), and queued {s.queued} to retry "
            "automatically at the next session start (see the warning above for why the cloud "
            "stopped accepting them)" + _shortfall(s) + ".")
    if s.outcome == BATCH_STRANDED:
        return _unknown_prefix(s.unknown_ids) + (
            f"Shared {s.sent} of {s.total} decision(s), then the cloud stopped accepting them "
            f"(see the warning above for why). Queued {s.queued} for retry before the outbox "
            "write failed - run `contexer share --all` again to queue the rest; your local "
            "decisions are unchanged" + _shortfall(s) + ".")
    return f"Share finished with an unrecognised outcome ({s.outcome or 'empty'})."


def _describe_reconcile(s: ReconcileStatus) -> str:
    if s.outcome == NO_LOCAL_MATCH:
        return "Nothing to reconcile: no matching local decision."
    if s.outcome == NOT_LOGGED_IN:
        return ("Not in team mode. Run `contexer login` to connect this machine before "
                "submitting a team update.")
    if s.outcome == NO_TEAMS:
        return "You do not belong to any shared teams."
    if s.outcome == TEAM_AMBIGUOUS:
        return f"Team name {s.detail!r} is ambiguous; pass its id instead."
    if s.outcome == TEAM_UNKNOWN:
        return (f"No shared team matches {s.detail!r}. "
                f"Available: {', '.join(s.teams)}.")
    if s.outcome == TEAM_CHOICE_REQUIRED:
        return f"Choose a team with `--team NAME_OR_ID`. Available: {', '.join(s.teams)}."
    if s.outcome == NO_TEAM_DISCOVERY:
        return ("This team server does not support team discovery. "
                "Run reconcile again with `--team TEAM_ID`.")
    if s.outcome == TEAM_LIST_FAILED:
        return "Could not list shared teams (see the warning above); nothing was submitted."
    if s.outcome == CAPABILITIES_FAILED:
        return (f"Could not discover reconciliation capabilities: {s.detail}. "
                "Nothing was submitted.")
    if s.outcome == NO_REVISION_ID:
        return "This decision has no stable local revision id; nothing was submitted."
    if s.outcome == PREVIEW_FAILED:
        return f"Could not preview reconciliation: {s.detail}. Nothing was submitted."
    if s.outcome == HEADS_CHANGED:
        return ("The personal or team decision changed after the preview. Nothing was submitted; "
                "run reconcile again to review the new heads.")
    if s.outcome == NEEDS_REBASE:
        return ("The team decision moved ahead and this update needs review. Nothing was "
                "submitted; pull and run reconcile again.")
    if s.outcome == UNCHANGED:
        return f"{s.team_name} already has this decision; no candidate was needed."
    if s.outcome == ALREADY_PENDING:
        named = f" as {s.candidate_id}" if s.candidate_id else ""
        return f"This exact update is already pending lead review in {s.team_name}{named}."
    if s.outcome == QUOTA_EXCEEDED:
        return ("The team service is at capacity. Nothing was submitted or queued; free capacity "
                "and run reconcile again for a fresh preview.")
    if s.outcome == SERVICE_REFUSED:
        return (f"The service refused the reconciliation ({s.server_status}). Nothing was "
                "submitted or queued.")
    if s.outcome == UNKNOWN_RESULT:
        return (f"The service returned an unknown reconciliation result "
                f"({s.server_status or 'empty'}). Nothing was treated as submitted or queued.")
    if s.outcome == SUBMISSION_REFUSED:
        return f"The service refused the submission: {s.detail}. Nothing was queued."
    if s.outcome == UNREACHABLE_QUEUED:
        return (f"Could not reach the team service ({s.detail}); the confirmed submission is "
                "queued with the same idempotency key for automatic retry.")
    if s.outcome == UNREACHABLE_NOT_QUEUED:
        return (f"Could not reach the team service ({s.detail}), and the confirmed operation "
                "could not be written to the retry queue. Nothing was submitted; rerun "
                "reconcile when the service is available.")
    if s.outcome == RATE_LIMITED_QUEUED:
        return _RATE_LIMIT_QUEUED_TEXT
    if s.outcome == RATE_LIMITED_NOT_QUEUED:
        return _RATE_LIMIT_STRANDED_TEXT
    if s.outcome == SUBMITTED:
        named = f" {s.candidate_id}" if s.candidate_id else ""
        replay = " (confirmed from an idempotent retry)" if s.replayed else ""
        return (f"Submitted {s.noun}{named} to {s.team_name} for lead review{replay}. "
                + _TEAM_VERSION_TEXT)
    if s.outcome == COMPAT_SYNC_FAILED:
        # No inner status means the reason was not recorded. Say that, rather than borrowing a
        # specific and probably untrue one (this returned the not-in-team-mode text, which names
        # a cause nothing here established).
        if s.share is None:
            return ("Could not sync the decision to personal cloud before team submission, and "
                    "the reason was not recorded. Nothing was submitted.")
        return _describe_share(s.share)
    if s.outcome == COMPAT_SUBMIT_FAILED:
        return (f"Synced the decision to personal cloud, but could not submit it to {s.team_name} "
                "for review. Run the same reconcile command again; the personal sync is "
                "idempotent.")
    return (f"Reconciliation finished with an unrecognised outcome ({s.outcome or 'empty'}). "
            "Nothing was treated as submitted or queued.")
