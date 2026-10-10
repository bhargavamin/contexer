"""Read-only deterministic Markdown and ADR projections of current decisions."""
import hashlib
import json
import os
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
               {"approved", "suggested"}}
    if include_retired:
        deleted, error = store.read_deleted(repo_path)
        if error:
            raise ValueError(f"Cannot export unreadable retired decisions: {error}")
        for e in deleted["entries"]:
            if e.get("type") == "decision" and e.get("id"):
                entries.setdefault(e["id"], e)
    ordered = sorted(entries.values(), key=lambda e: (e.get("subtype", ""), e.get("timestamp", ""), e["id"]))

    def text(value: str) -> str:
        return value if verbatim else redact.scrub_text(value, strict_assignments=True)

    def metadata(entry: dict) -> list[str]:
        status = "retired" if entry.get("deleted_at") else store.entry_status(entry)
        lifecycle = entry.get("lifecycle") or []
        replacement = entry.get("superseded_by") or next(
            (r.get("replacement_decision_id") or r.get("replacement_id")
             for r in reversed(lifecycle) if r.get("replacement_decision_id") or r.get("replacement_id")), "")
        if not entry.get("deleted_at"):
            replacement = ""
        if replacement:
            status = "superseded"
        rows = [f"Status: {status}", f"ID: {entry['id']}",
                f"Created: {entry.get('timestamp', '')}",
                f"Updated: {entry.get('updated_at') or entry.get('timestamp', '')}"]
        if entry.get("source_files"):
            rows.append("Applies to files: " + ", ".join(entry["source_files"]))
        if replacement:
            target = filename(str(replacement)) if format == "adr" else f"#decision-{replacement}"
            rows.append(f"Replaced by: [{replacement}]({target})" if replacement in entries
                        else f"Replaced by: {replacement} (not included in this export)")
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
                + "\n\n".join(metadata(entry))
                + (f"\n\n## Summary\n\n{text(entry['summary'])}" if entry.get("summary") else "")
                + "\n\n## Context\n\n" + text(context)
                + "\n\n## Decision\n\n" + text(revisions.current_content(entry))
                + "\n\n## Consequences\n\n" + text(consequences) + "\n")
        return documents
    lines = ["# Decisions", ""]
    subtype = None
    for entry in ordered:
        if entry.get("subtype", "") != subtype:
            subtype = entry.get("subtype", "")
            lines.extend([f"## {text(subtype or 'Unclassified')}", ""])
        lines.extend([f'<a id="decision-{entry["id"]}"></a>',
                      f"### {text(entry.get('title') or revisions.derive_title(revisions.current_content(entry)))}", "",
                      *metadata(entry), "",
                      # The review summary, when one is stored, beside (never instead of) the text.
                      *([f"_Summary: {text(entry['summary'])}_", ""] if entry.get("summary") else []),
                      text(revisions.current_content(entry)), ""])
    return {"decisions.md": "\n".join(lines)}


def write(repo_path: str, out: Path, **options) -> list[Path]:
    documents = render(repo_path, **options)
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    if out.is_symlink():
        raise OSError("Export directory must not be a symlink")
    descriptor = os.open(out, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        os.fchmod(descriptor, os.fstat(descriptor).st_mode & 0o7700)
    finally:
        os.close(descriptor)
    manifest = out / ".contexer-export.json"
    if manifest.is_symlink():
        raise ValueError("Export manifest must not be a symlink")
    previous = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else {}
    if not isinstance(previous, dict) or any(not isinstance(name, str) or Path(name).name != name
            or not (name == "decisions.md" or (name.startswith("adr-") and name.endswith(".md")))
            for name in previous) or any(
                not isinstance(values, (str, list)) or not (values if isinstance(values, list) else [values])
                or any(not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value)
                       for value in (values if isinstance(values, list) else [values]))
                for values in previous.values()):
        raise ValueError("Unreadable export manifest; refusing output changes")
    stale = []
    for name in previous.keys() | documents.keys():
        path = out / name
        if path.exists() or path.is_symlink():
            if name not in previous:
                raise ValueError("Existing output is not owned by this export; preserve it first")
            accepted = previous[name] if isinstance(previous[name], list) else [previous[name]]
            if path.is_symlink() or hashlib.sha256(path.read_bytes()).hexdigest() not in accepted:
                raise ValueError("A previous export was edited; preserve it before re-exporting")
            if name not in documents:
                stale.append(path)
    paths = []
    hashes = {name: hashlib.sha256(content.encode()).hexdigest() for name, content in documents.items()}
    pending = {name: (value if isinstance(value, list) else [value]) for name, value in previous.items()}
    for name, digest in hashes.items():
        pending[name] = sorted(set(pending.get(name, []) + [digest]))
    # Publish ownership of old and intended bytes before any document replacement. A crash
    # can then resume without treating our own partially updated output as a user's edit.
    store.atomic_write(manifest, json.dumps(pending, sort_keys=True))
    for name, content in documents.items():
        path = out / name
        store.atomic_write(path, content)
        paths.append(path)
        hashes[name] = hashlib.sha256(content.encode()).hexdigest()
    for path in stale:
        path.unlink()
    store.atomic_write(manifest, json.dumps(hashes, sort_keys=True))
    return paths
