"""Pure decision-revision lifecycle and derived metadata.

The local store owns persistence, migration, approval, and locking.  This module owns the
in-memory revision model so those mechanics can evolve without growing the store facade.
"""

import re
import uuid
from datetime import datetime, timezone

MAX_TITLE_LEN = 100


def normalize_content(content: str) -> str:
    """Strip whitespace, collapse internal runs, and capitalize the first character."""
    normalized = " ".join(content.split())
    return normalized[:1].upper() + normalized[1:] if normalized else normalized


def normalize_title(title: str) -> str:
    """Collapse a title to one stripped line and cap it with an ellipsis."""
    one_line = " ".join(title.split())
    if len(one_line) <= MAX_TITLE_LEN:
        return one_line
    return one_line[:MAX_TITLE_LEN - 1].rstrip() + "…"


def derive_title(content: str) -> str:
    """Derive a deterministic fallback title from decision content."""
    one_line = " ".join(content.split())
    if not one_line:
        return ""
    if len(one_line) <= MAX_TITLE_LEN:
        return one_line
    first_line = content.strip().splitlines()[0]
    first_sentence = re.split(r"(?<=[.!?])\s", first_line, maxsplit=1)[0]
    return normalize_title(first_sentence)


def compute_confidence(entry: dict) -> tuple[int, list[str]]:
    """Compute a confidence score from a decision's aggregate evidence."""
    if entry.get("bootstrap") and entry.get("approved_by") != "human":
        return 30, ["Source-backed bootstrap context; repetition is not independent confirmation"]
    score = 30
    factors: list[str] = []

    if entry.get("approved_by") == "human":
        score += 40
        factors.append("Approved by developer")

    created_by = entry.get("created_by", "ai")
    if created_by in ("scan", "bootstrap"):
        score += 15
        factors.append("Observed in repository")
    elif created_by == "human":
        score += 20
        factors.append("Stated by developer")

    occ = entry.get("occurrence_count", 1)
    if occ >= 3:
        score += 20
        factors.append(f"Referenced in {occ} sessions")
    elif occ >= 2:
        score += 10
        factors.append("Mentioned multiple times")

    sessions = entry.get("session_ids", [])
    if len(sessions) >= 3 and occ < 3:
        score += 10
        factors.append("Confirmed across multiple sessions")
    elif len(sessions) >= 2 and occ < 2:
        score += 5
        factors.append("Seen in multiple sessions")

    if entry.get("memory_key"):
        score += 5
        factors.append("Persisted to memory tool")

    return min(score, 100), factors


def new_revision(decision_id: str, version_number: int, content: str, source: str,
                 confidence_score: int = 0, evidence: list | None = None,
                 approved_at: str | None = None, created_at: str | None = None,
                 normalize: bool = True, title: str = "") -> dict:
    """Build one immutable revision object."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "revision_id": str(uuid.uuid4()),
        "decision_id": decision_id,
        "version_number": version_number,
        "content": normalize_content(content) if normalize else content,
        "title": title,
        "confidence_score": confidence_score,
        "evidence": list(evidence or []),
        "created_at": created_at or now,
        "approved_at": approved_at,
        "source": source,
    }


def current_revision(entry: dict) -> dict | None:
    """Resolve the active revision pointer, falling back to the last revision."""
    revs = entry.get("revisions") or []
    current_id = entry.get("current_revision_id")
    if current_id:
        for revision in revs:
            if revision.get("revision_id") == current_id:
                return revision
    return revs[-1] if revs else None


def current_content(entry: dict) -> str:
    """Return the active revision content, with legacy cache fallback."""
    revision = current_revision(entry)
    if revision is not None:
        return revision.get("content", "")
    return entry.get("content", "")


def sync_decision_cache(entry: dict) -> None:
    """Mirror the active revision onto the decision-level HEAD cache."""
    revision = current_revision(entry)
    if revision is None:
        return
    entry["content"] = revision.get("content", "")
    entry["title"] = revision.get("title") or derive_title(revision.get("content", ""))
    entry["revision"] = revision.get("version_number", 1)
    entry["confidence"] = revision.get("confidence_score", entry.get("confidence", 0))
    evidence = revision.get("evidence") or []
    if evidence:
        entry["confidence_factors"] = evidence
    else:
        entry.pop("confidence_factors", None)


def append_revision(entry: dict, content: str, source: str,
                    approved_at: str | None = None, title: str = "") -> dict:
    """Append a revision, advance HEAD, invalidate stale approval, and sync its cache."""
    revisions = entry.setdefault("revisions", [])
    next_version = (revisions[-1]["version_number"] + 1) if revisions else 1
    if source != "human":
        entry.pop("approved_by", None)
    score, factors = compute_confidence(entry)
    effective_title = normalize_title(title) or derive_title(content)
    revision = new_revision(
        entry.get("id", ""), next_version, content,
        source=source, confidence_score=score, evidence=factors,
        approved_at=approved_at, title=effective_title,
    )
    revisions.append(revision)
    entry["current_revision_id"] = revision["revision_id"]
    entry["updated_at"] = revision["created_at"]
    sync_decision_cache(entry)
    return revision
