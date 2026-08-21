"""Pure proposal-slot policy for the developer review queue.

A live decision carries at most ONE unreviewed ``proposed_revision`` (a Suggested Update).
This module owns who may take that slot, what happens to the proposal it displaces, and how
a refused claim is reported back to the calling model.  Persistence, locking, git anchoring,
and the public approval entry points stay in :mod:`contexer.store`, and the dependency stays
one-way (``store.py`` imports this leaf, never the reverse).

Promotion of a proposal into a revision is deliberately NOT here: it anchors source files,
which shells out to git and mutates the entry, so it stays with the store's I/O.
"""

from contexer import revisions

# Trust order for the single proposal slot (issue #200): a developer restatement is the
# highest-trust signal in the system, a plan-sourced value survived reconciliation, an AI
# guess is inferred, and a scan proposal is bookkeeping that re-proposes on its own TTL.
PROPOSAL_TRUST = {"human": 3, "plan": 2, "ai": 1, "scan": 0}


def outranks_proposal(source: str, prop: dict) -> bool:
    """Whether a new proposal from `source` may displace the unreviewed `prop` already
    holding the entry's one proposal slot. STRICTLY greater only: a human proposal is never
    auto-replaced, and an equal-trust collision keeps the refusal. An unrecognised source
    ranks below every known one — it never displaces, and is itself displaceable."""
    return PROPOSAL_TRUST.get(source, -1) > PROPOSAL_TRUST.get(prop.get("source", ""), -1)


def claim_proposal_slot(entry: dict, source: str, now: str) -> bool:
    """Whether a new `source`-sourced proposal may take the entry's ONE proposal slot,
    archiving whatever DIFFERENT proposal already holds it (issue #200's trust order, which
    the update_decision write sites must honour too — an ai correction there used to clobber
    a human's unreviewed Suggested Update). False = the sitting proposal STRICTLY outranks
    the incoming one and is left untouched; the caller returns success so the flow still
    shows the pending prompt, failing toward review of the higher-trust proposal rather than
    losing it.

    A TIE claims the slot, unlike `store._route_containment`'s refusal: there the two sides
    are separate developer statements, each owed a review, while here they are the same
    automated source retrying — refusing would silently drop a model's own correction of the
    proposal it just wrote. Identical-content dedup belongs BEFORE this call."""
    prop = entry.get("proposed_revision")
    if not prop:
        return True
    if PROPOSAL_TRUST.get(prop.get("source", ""), -1) > PROPOSAL_TRUST.get(source, -1):
        return False
    # Displaced, not discarded — same archival shape as _route_containment/edit_decision.
    entry.setdefault("superseded_proposals", []).append({**prop, "superseded_at": now})
    entry.pop("proposed_revision", None)
    entry.pop("conflict_memo", None)   # it referenced the proposal just replaced
    return True


def refusal_ack(entry: dict) -> str:
    """Model-facing ack for a refused slot claim (issue #202). A refusal returns success to
    the caller (the higher-trust proposal still awaits review), so without this the calling
    model is told its correction is pending when it was dropped — same in-band-ack precedent
    as capture_lint/constraint_ack, where silence loses the information."""
    prop = entry.get("proposed_revision") or {}
    return (
        f"Correction NOT stored: decision {entry.get('id', '')[:8]} already has a "
        f"higher-trust Suggested Update pending review (from {prop.get('source', 'unknown')}: "
        f"'{prop.get('title', '')}'). The one proposal slot keeps the higher-trust version — "
        "your correction was refused, not queued, and will not be reviewed. Do NOT retry this "
        "call and do NOT approve anything yourself. This turn, tell the developer both "
        "versions — the pending update and your refused correction — so they can review with "
        "full context (approve_decision action='edit' can merge them)."
    )


def build_proposal(target: dict, content: str, subtype: str, session_id: str, now: str,
                   source: str = "ai", title: str = "", source_files=None) -> dict:
    """A Suggested Update (pending revision) attached to a live decision: the detected new
    value, its confidence/evidence, and provenance. The live decision is NOT modified - this
    proposal waits for developer approval, at which point it is promoted to a new revision.

    source_files: stashed on the proposal, not applied yet — the live entry's anchor must
    keep describing the CURRENTLY RENDERED content until the proposal is actually promoted
    (see store._promote_proposal); re-anchoring here would clear the stale note while the old,
    still-live text keeps rendering."""
    sessions = sorted({s for s in (*(target.get("session_ids") or []), session_id) if s})
    score, factors = revisions.compute_confidence({
        "created_by": "ai",
        "occurrence_count": target.get("occurrence_count", 1),
        "session_ids": sessions,
        "memory_key": target.get("memory_key"),
    })
    normalized_content = revisions.normalize_content(content)
    proposal = {
        "content": normalized_content,
        "subtype": subtype or target.get("subtype", ""),
        "session_id": session_id,
        "source": source,
        "created_at": now,
        "confidence": score,
        "confidence_factors": factors,
    }
    proposal["title"] = revisions.normalize_title(title) or revisions.derive_title(normalized_content)
    if source_files:
        proposal["source_files"] = source_files
    return proposal
