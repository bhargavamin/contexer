"""Pure state transitions for team decision reconciliation.

Persistence and revision construction stay in :mod:`contexer.store`.  This module owns
the reconciliation-specific policy so new outcomes and head rules do not keep growing
the store facade.
"""

from collections.abc import Mapping


def proposal_origin(proposal: Mapping | None) -> dict | None:
    """Return a pull-created proposal's reconciliation metadata, if present."""
    if not isinstance(proposal, Mapping):
        return None
    origin = proposal.get("team_reconciliation")
    return dict(origin) if isinstance(origin, Mapping) else None


def attach_proposal(entry: dict, proposal: dict, *, current_content: str,
                    normalized_content: str, team_head: str) -> tuple[bool, bool]:
    """Attach ``proposal`` when its team head has not already been handled.

    Returns ``(accepted, changed)``.  An idempotent replay is accepted without a
    mutation, while a conflicting proposal already occupying the single review slot is
    rejected.  Approved and dismissed heads are receipts, so later metadata changes for
    the same remote head cannot re-open review.
    """
    existing = entry.get("proposed_revision")
    if existing:
        origin = proposal_origin(existing) or {}
        return bool(team_head and origin.get("team_head") == team_head), False

    last = entry.get("last_team_reconciliation")
    if isinstance(last, Mapping) and team_head and last.get("team_head") == team_head:
        if last.get("outcome") in {"approved", "dismissed"}:
            return True, False
        if current_content == normalized_content:
            return True, False

    entry["proposed_revision"] = proposal
    return True, True


def clear_proposal(entry: dict, *, team_head: str, at: str) -> bool:
    """Clear a pull-created proposal after convergence, rejecting stale deltas."""
    origin = proposal_origin(entry.get("proposed_revision"))
    if origin is None:
        return False
    if team_head and origin.get("team_head") not in {"", team_head}:
        return False

    entry.pop("proposed_revision", None)
    entry.pop("conflict_memo", None)
    entry["last_team_reconciliation"] = {**origin, "outcome": "in_sync", "at": at}
    return True


def record_outcome(entry: dict, proposal: Mapping | None, *, outcome: str, at: str) -> bool:
    """Record that a pull-created proposal head was consumed by local review."""
    origin = proposal_origin(proposal)
    if origin is None:
        return False
    entry["last_team_reconciliation"] = {**origin, "outcome": outcome, "at": at}
    return True
