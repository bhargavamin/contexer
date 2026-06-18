"""Import Claude Code memory-tool facts into the Contexer store.

Coexistence, not competition: when a session persists decisions to the file-based
memory tool (``~/.claude/projects/<slug>/memory/*.md``) instead of calling
``update_context``, those decisions never reach Contexer and the next session starts
blind. This module reads those memory files and turns each fact into a *subtyped*
Contexer decision so it is retrievable by ``get_context(entry_type=...)``.

Everything here is deterministic — no model in the loop. The memory files are already
structured (YAML frontmatter + markdown body), so a fact's summary (``description``)
and category (``type`` + keyword rules) are lifted directly rather than summarized.
A multi-section doc is split on ``##`` headings so it becomes several coherent
entries, never one raw blob. Re-imports are silent no-ops via the store's >70%
novelty filter; whole-dir re-runs are skipped by a content fingerprint (see
``claude.sync_memory``). Every path is fail-soft: a malformed file is skipped, never
raised — a bad memory file must never break a session start.

This module is neutral: it takes a directory path. The Claude-specific knowledge of
*where* that directory lives is the adapter's (``adapters/claude.py``)."""
import hashlib
import re
from pathlib import Path

from contexer import store

# ── Subtype classification ──────────────────────────────────────────────────────
# Checked in order; first match wins. Convention is first so tooling/format
# directives ("use uv not pip3") win over the constraint reading of "do not".
# Best-effort by design: an imperfect-but-present subtype keeps a fact queryable,
# which is the whole point of importing it. fm_type is the backstop, default arch.
_CONVENTION = re.compile(
    r"\b(use\s+\S+\s+not|not\s+pip|conventional commit|commit (format|message|convention)|"
    r"naming|prefix|suffix|lint(er)?|formatter|toolchain|always use|"
    r"(file|directory|folder) structure|package management|dependenc(y|ies)|"
    r"uv|pip3?|npm|yarn|format)\b", re.I)
_CONSTRAINT = re.compile(
    r"\b(never|always|must not|must|do not|don't|avoid|required|prohibited|"
    r"mandatory|ensure|only|out[- ]of[- ]scope|out of scope|forbidden)\b", re.I)
_PATTERN = re.compile(r"\b(pattern|organi[sz]e|layout|module|scaffold|skeleton)\b", re.I)
_ARCHITECTURE = re.compile(
    r"\b(architecture|design|chose|chosen|decided|decision|instead of|library|"
    r"framework|trade[- ]?off|approach|stack|protocol|structure)\b", re.I)

_FM_TYPE_SUBTYPE = {"feedback": "convention", "project": "architecture"}


def _classify(text: str, fm_type: str) -> str:
    if _CONVENTION.search(text):
        return "convention"
    if _CONSTRAINT.search(text):
        return "constraint"
    if _PATTERN.search(text):
        return "pattern"
    if _ARCHITECTURE.search(text):
        return "architecture"
    return _FM_TYPE_SUBTYPE.get(fm_type.lower(), "architecture")


# ── Frontmatter / body parsing ──────────────────────────────────────────────────
_DESC_RE = re.compile(r"^\s*description:\s*(.*)$")
_TYPE_RE = re.compile(r"^\s*type:\s*(.*)$")          # `node_type:` has chars before `type:` — no false match
_ORIGIN_RE = re.compile(r"^\s*originSessionId:\s*(.*)$")
_NAME_RE = re.compile(r"^\s*name:\s*(.*)$")


def _unquote(s: str) -> str:
    s = s.strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in "\"'":
        return s[1:-1]
    return s


def _parse_fact(text: str) -> dict:
    """Split a memory ``.md`` into {name, description, fm_type, origin, body}.

    Tolerant: a file without frontmatter yields empty metadata and the whole text
    as body, so even a plain note is still importable."""
    name = description = fm_type = origin = ""
    body = text
    if text.startswith("---"):
        lines = text.splitlines()
        end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
        if end is not None:
            for ln in lines[1:end]:
                if m := _NAME_RE.match(ln):
                    name = _unquote(m.group(1))
                elif m := _DESC_RE.match(ln):
                    description = _unquote(m.group(1))
                elif m := _TYPE_RE.match(ln):
                    fm_type = _unquote(m.group(1))
                elif m := _ORIGIN_RE.match(ln):
                    origin = _unquote(m.group(1))
            body = "\n".join(lines[end + 1:]).strip()
    return {"name": name, "description": description, "fm_type": fm_type,
            "origin": origin, "body": body}


def _tag(name: str) -> str:
    return f"[memory:{name}] " if name else "[memory] "


def _build_entries(fact: dict, source_id: str) -> list[tuple[str, str, str]]:
    """Turn one parsed fact into (content, subtype, memory_key) triples.

    Multi-section docs (>=2 ``##`` headings) split into one entry per section so a
    big spec becomes several subtyped decisions, not a blob. Atomic facts become a
    single entry led by the human-written ``description``. Each content is tagged
    ``[memory:<name>]`` for provenance; ``memory_key`` (``source_id`` for an atomic
    fact, ``source_id#<heading>`` per section) is the stable identity the store
    upserts on, so a fact reworded on disk updates in place instead of duplicating."""
    name, desc, fm_type, body = fact["name"], fact["description"], fact["fm_type"], fact["body"]
    matches = list(re.finditer(r"^##\s+.*$", body, re.M))

    if len(matches) >= 2:
        entries = []
        for i, m in enumerate(matches):
            start = m.start()
            stop = matches[i + 1].start() if i + 1 < len(matches) else len(body)
            section = body[start:stop].strip()
            heading = m.group(0).lstrip("# ").strip()
            content = f"{_tag(name)}{heading}: {section}"
            entries.append((content, _classify(heading + " " + section, fm_type),
                            f"{source_id}#{heading}"))
        return entries

    lead = desc + "\n" if desc else ""
    meaningful = (lead + body).strip()
    if not meaningful:                       # nothing but (or not even) a provenance tag
        return []
    return [(f"{_tag(name)}{meaningful}", _classify(desc + " " + body, fm_type), source_id)]


# ── Public API ──────────────────────────────────────────────────────────────────
def dir_fingerprint(memory_dir: Path) -> str:
    """Cheap change signature over a memory dir: (name, mtime, size) of every .md.
    Lets the caller skip the whole import when nothing changed."""
    parts = []
    for p in sorted(memory_dir.glob("*.md")):
        try:
            st = p.stat()
            parts.append(f"{p.name}:{int(st.st_mtime)}:{st.st_size}")
        except OSError:
            continue
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def import_dir(memory_dir: Path, repo_path: str) -> int:
    """Import every fact file in ``memory_dir`` into the store for ``repo_path``.

    Returns the count of newly-*created* entries (in-place updates and dedup-skips
    don't count). Skips ``MEMORY.md`` (just a link index). Fail-soft per file.
    Each entry is keyed by ``memory_key`` so a reworded fact updates in place
    (see ``store.upsert_memory_decision``)."""
    stored = 0
    for path in sorted(memory_dir.glob("*.md")):
        if path.name == "MEMORY.md":
            continue
        try:
            fact = _parse_fact(path.read_text(encoding="utf-8"))
            sid = fact["origin"] or "memory-sync"
            for content, subtype, key in _build_entries(fact, path.name):
                if store.upsert_memory_decision(repo_path, content, sid, subtype, key) == "created":
                    stored += 1
        except Exception:
            continue
    return stored
