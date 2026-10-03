"""Read-only deterministic Markdown and ADR projections of current decisions."""
import hashlib
import re
from pathlib import Path

from contexer import redact, revisions, store


def render(repo_path: str, *, format: str = "md", include_retired: bool = False,
           verbatim: bool = False) -> dict[str, str]:
    """Return stable relative filenames and text; never mutate the decision store."""
    if format not in {"md", "adr"}:
        raise ValueError("Export format must be md or adr")
    data = store.load_for_update(repo_path)
    entries = {e["id"]: e for e in data["entries"] if e.get("type") == "decision"
               and e.get("id") and store.entry_status(e) in
               ({"approved", "suggested", "ignored"} if include_retired else {"approved", "suggested"})}
    if include_retired:
        deleted, error = store.read_deleted(repo_path)
        if error:
            raise ValueError(f"Cannot export unreadable retired decisions: {error}")
        for e in deleted["entries"]:
            if e.get("type") == "decision" and e.get("id"):
                entries.setdefault(e["id"], e)
    ordered = sorted(entries.values(), key=lambda e: (e.get("subtype", ""), e.get("timestamp", ""), e["id"]))

    def text(value: str) -> str:
        return value if verbatim else redact.scrub_text(value)

    def metadata(entry: dict) -> list[str]:
        status = "retired" if entry.get("deleted_at") else store.entry_status(entry)
        lifecycle = entry.get("lifecycle") or []
        replacement = entry.get("superseded_by") or next(
            (r.get("replacement_decision_id") or r.get("replacement_id")
             for r in reversed(lifecycle) if r.get("replacement_decision_id") or r.get("replacement_id")), "")
        if replacement:
            status = "superseded"
        rows = [f"Status: {status}", f"ID: {entry['id']}",
                f"Created: {entry.get('timestamp', '')}",
                f"Updated: {entry.get('updated_at') or entry.get('timestamp', '')}"]
        if entry.get("source_files"):
            rows.append("Applies to files: " + ", ".join(entry["source_files"]))
        if replacement:
            target = filename(str(replacement)) if format == "adr" else f"#decision-{replacement}"
            rows.append(f"Replaced by: [{replacement}]({target})")
        return [text(row) for row in rows]

    def filename(decision_id: str) -> str:
        safe_id = decision_id if re.fullmatch(r"[a-zA-Z0-9-]{1,80}", decision_id) else hashlib.sha256(decision_id.encode()).hexdigest()
        return f"adr-{safe_id}.md"

    if format == "adr":
        documents = {}
        for entry in ordered:
            context = entry.get("rationale") or "Context was not recorded separately."
            consequences = entry.get("consequences") or "Consequences were not recorded separately."
            documents[filename(entry["id"])] = (
                f"# {text(entry.get('title') or revisions.derive_title(revisions.current_content(entry)))}\n\n"
                + "\n\n".join(metadata(entry)) + "\n\n## Context\n\n" + text(context)
                + "\n\n## Decision\n\n" + text(revisions.current_content(entry))
                + "\n\n## Consequences\n\n" + text(consequences) + "\n")
        return documents
    lines = ["# Decisions", ""]
    subtype = None
    for entry in ordered:
        if entry.get("subtype", "") != subtype:
            subtype = entry.get("subtype", "")
            lines.extend([f"## {subtype or 'Unclassified'}", ""])
        lines.extend([f'<a id="decision-{entry["id"]}"></a>',
                      f"### {text(entry.get('title') or revisions.derive_title(revisions.current_content(entry)))}", "",
                      *metadata(entry), "", text(revisions.current_content(entry)), ""])
    return {"decisions.md": "\n".join(lines)}


def write(repo_path: str, out: Path, **options) -> list[Path]:
    documents = render(repo_path, **options)
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    paths = []
    for name, content in documents.items():
        path = out / name
        store.atomic_write(path, content)
        paths.append(path)
    return paths
