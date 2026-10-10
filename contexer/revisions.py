"""Pure decision-revision lifecycle and derived metadata.

The local store owns persistence, migration, approval, and locking.  This module owns the
in-memory revision model so those mechanics can evolve without growing the store facade.
"""

import re
import uuid
from datetime import datetime, timezone

MAX_TITLE_LEN = 100

# The review summary: a short Simplified Technical English (ASD-STE100) version of a decision,
# for the human reviewing it. The model never reads it; the full content stays what is injected
# and what an approval signs. Checked deterministically: the controlled STE dictionary is not
# machine-checkable, but "short sentences, few of them" is, and that is the failure that matters
# (a summary as long as the text it summarizes).
MAX_SUMMARY_SENTENCES = 5
MAX_SUMMARY_SENTENCE_WORDS = 20
MAX_SUMMARY_CHARS = 600
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


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


def normalize_summary(summary: str) -> str:
    """Collapse a summary to single-spaced text; sentences, not lines, are its units."""
    return " ".join((summary or "").split())


def set_summary(record: dict, summary: str) -> None:
    """Store `summary` (normalized) on a revision, proposal or decision cache, or drop the key
    when it normalizes to nothing: a record never keeps an empty summary."""
    flat = normalize_summary(summary)
    if flat:
        record["summary"] = flat
    else:
        record.pop("summary", None)


def summary_problem(text: str) -> str | None:
    """Why `text` does not read as a review summary, or None when it does: one to
    MAX_SUMMARY_SENTENCES sentences, each at most MAX_SUMMARY_SENTENCE_WORDS words, at most
    MAX_SUMMARY_CHARS characters in all."""
    flat = normalize_summary(text)
    if not flat:
        return "it is empty"
    if len(flat) > MAX_SUMMARY_CHARS:
        return f"it is {len(flat)} characters; keep it under {MAX_SUMMARY_CHARS}"
    sentences = [s for s in _SENTENCE_END.split(flat) if s]
    if len(sentences) > MAX_SUMMARY_SENTENCES:
        return f"it has {len(sentences)} sentences; use at most {MAX_SUMMARY_SENTENCES}"
    longest = max(len(s.split()) for s in sentences)
    if longest > MAX_SUMMARY_SENTENCE_WORDS:
        return (f"a sentence has {longest} words; keep each sentence to "
                f"{MAX_SUMMARY_SENTENCE_WORDS} words or fewer")
    return None


def needs_summary(content: str) -> bool:
    """Whether content is too long to be its own review summary. Short content (a one-line
    convention, a prompt directive) already reads as one, so it gets none stored."""
    return summary_problem(content) is not None


def review_summary(record: dict) -> str | None:
    """What a review shows in place of the full text: the stored summary, else the content
    itself when it is short enough to be its own, else None (the review shows the full text).
    `record` is a decision, a revision or a proposal."""
    stored = normalize_summary(record.get("summary") or "")
    if stored:
        return stored
    text = record.get("content", "")
    return normalize_summary(text) if text and not needs_summary(text) else None


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


def normalize_applies_when(value: list[str] | None) -> list[str] | None:
    """Bound task phrases so author-supplied applicability cannot become a word dump."""
    if value is None:
        return None
    if not isinstance(value, list) or len(value) > 8:
        raise ValueError("applies_when must contain at most eight short task phrases")
    phrases = []
    for phrase in value:
        if not isinstance(phrase, str):
            raise ValueError("applies_when phrases must be strings")
        clean = " ".join(phrase.split())
        from contexer import retrieval
        subject = clean
        for artifact in retrieval.raw_path_artifacts(clean):
            subject = subject.replace(artifact, " ")
        tokens = set(retrieval.index_tokens(subject))
        if not clean or len(clean) > 100 or len(tokens) < 2:
            raise ValueError("applies_when needs specific task phrases of 2+ words, at most 100 characters")
        if tokens <= retrieval._GENERIC_TASK_WORDS:
            raise ValueError("applies_when phrases need a word specific to the situation, "
                             f"not only generic task words: {clean!r}")
        if clean not in phrases:
            phrases.append(clean)
    return phrases


def new_revision(decision_id: str, version_number: int, content: str, source: str,
                 confidence_score: int = 0, evidence: list | None = None,
                 approved_at: str | None = None, created_at: str | None = None,
                 normalize: bool = True, title: str = "",
                 applies_when: list[str] | None = None, summary: str = "") -> dict:
    """Build one immutable revision object. A review `summary` is kept only when given: a
    revision never inherits another revision's summary, since it may no longer describe it."""
    now = datetime.now(timezone.utc).isoformat()
    revision = {
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
    if applies_when is not None:
        revision["applies_when"] = normalize_applies_when(applies_when)
    set_summary(revision, summary)
    return revision


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
    set_summary(entry, revision.get("summary") or "")
    if "applies_when" in revision:
        entry["applies_when"] = list(revision["applies_when"])
    else:
        entry.pop("applies_when", None)
    entry["revision"] = revision.get("version_number", 1)
    entry["confidence"] = revision.get("confidence_score", entry.get("confidence", 0))
    evidence = revision.get("evidence") or []
    if evidence:
        entry["confidence_factors"] = evidence
    else:
        entry.pop("confidence_factors", None)


def append_revision(entry: dict, content: str, source: str,
                    approved_at: str | None = None, title: str = "",
                    normalize: bool = True, applies_when: list[str] | None = None,
                    summary: str = "") -> dict:
    """Append a revision, advance HEAD, invalidate stale approval, and sync its cache.

    `normalize=False` preserves already-collapsed case-sensitive factual content. The new
    revision carries only the `summary` given here, never the previous revision's.
    """
    if applies_when is None:
        applies_when = (current_revision(entry) or entry).get("applies_when")
    revisions = entry.setdefault("revisions", [])
    next_version = (revisions[-1]["version_number"] + 1) if revisions else 1
    if source != "human":
        entry.pop("approved_by", None)
    score, factors = compute_confidence(entry)
    effective_title = normalize_title(title) or derive_title(content)
    revision = new_revision(
        entry.get("id", ""), next_version, content,
        source=source, confidence_score=score, evidence=factors,
        approved_at=approved_at, title=effective_title, normalize=normalize,
        applies_when=applies_when, summary=summary,
    )
    revisions.append(revision)
    entry["current_revision_id"] = revision["revision_id"]
    entry["updated_at"] = revision["created_at"]
    sync_decision_cache(entry)
    return revision
