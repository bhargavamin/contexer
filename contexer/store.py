import contextlib
import hashlib
import json
import os
import re
import subprocess
import tempfile
import textwrap
import time
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path

from contexer import redact          # pure stdlib leaf (no cycle): secret redaction

try:
    import fcntl                       # POSIX advisory file locks (macOS/Linux)
except ImportError:                    # pragma: no cover - non-POSIX fallback
    fcntl = None

STORE_DIR = Path.home() / ".contexer"
MAX_ENTRIES = 500
MAX_TITLE_LEN = 100
_SCHEMA_VERSION = 3               # bumped when the on-disk entry shape changes; gates migration
GLOBAL_SLUG = "_global"           # reserved slug for cross-repo decisions
_UNFILTERED_DISPLAY = 10          # entries shown when no query/type filter applied
_FILTERED_DISPLAY = 25            # entries shown when a filter is active
_BACKLOG_ESCALATE = 10            # pending-review count at which surfacing tone firms up


# Directories that must never be treated as a repo. A poisoned .current_repo pointing at
# a tool's config dir (e.g. ~/.claude) would otherwise slug into its own store file and
# silently swallow decisions made in the real project. Guarded on both read and write.
def _config_dirs() -> set[str]:
    home = Path.home()
    return {str(home), str(home / ".claude"), str(home / ".cursor"),
            str(home / ".codex"), str(home / ".gemini"),
            str(home / ".contexer"), str(home / ".config")}


def _is_sane_repo(path: str) -> bool:
    """A usable repo path: non-empty, absolute, and not a tool config / home directory
    or the filesystem root."""
    if not path:
        return False
    p = path.strip()
    if not p or not os.path.isabs(p):
        return False
    norm = os.path.normpath(p)
    # normpath keeps a POSIX-special leading "//", so test for emptiness after
    # stripping separators rather than comparing to a single "/".
    if not norm.strip(os.path.sep):
        return False
    return norm not in _config_dirs()


def _git_root(start: str) -> str:
    """git toplevel for `start`, or "" if it isn't inside a git work tree. Never raises."""
    try:
        out = subprocess.run(
            ["git", "-C", start, "rev-parse", "--show-toplevel"],
            capture_output=True, text=True, timeout=2,
        )
        if out.returncode == 0:
            return out.stdout.strip()
    except Exception:
        pass
    return ""


# Repo bound to the running MCP server process, captured once at startup from its own cwd
# (set by server.main via set_session_repo). Each host session spawns its own server with
# cwd = that session's project, so this is immune to the shared .current_repo being
# clobbered by a different tool or session. "" outside a running server (tests, hooks).
_SESSION_REPO = ""


def set_session_repo(path: str) -> None:
    """Bind the current MCP-server process to a repo (its startup cwd's git root)."""
    global _SESSION_REPO
    _SESSION_REPO = path if _is_sane_repo(path) else ""


def anchor_repo(repo_path: str) -> bool:
    """Best-effort write of the shared ~/.contexer/.current_repo pointer. Never raises.

    Every adapter's session-start / per-prompt hook anchors the pointer here. It is
    pure bookkeeping: `_resolve_repo` consults it only as a LAST resort (an explicit
    repo_path and the per-process session binding both outrank it), so failing to
    write it must never abort a hook. Under a sandboxed host the workspace can be
    writable while `~/.contexer` is not (Codex's managed sandbox) — the write then
    raises PermissionError, and before #152 that aborted SessionStart entirely, so
    Contexer injected no rules or decisions at all over a file it did not need.
    Sanity-checked first (never poison the pointer with a home/config dir).
    Returns True only when the pointer was actually written."""
    try:
        if not _is_sane_repo(repo_path):
            return False
        STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        # encoding pinned (never the locale default) so the pointer round-trips
        # identically to _current_repo_path's read, on any host locale.
        (STORE_DIR / ".current_repo").write_text(repo_path, encoding="utf-8")
        return True
    except Exception:
        # Deliberately broad, and the sanity check is inside it: this runs on every
        # adapter's hook path, where "never crash the host" outranks precision. OSError
        # is the expected failure, but _is_sane_repo consults Path.home(), which raises
        # RuntimeError with no HOME (the contract cursor._anchor_current_repo already
        # had), and a repo path carrying non-UTF-8 filesystem bytes — surfaced as a
        # surrogate escape, routine on Linux — makes write_text raise UnicodeEncodeError,
        # a ValueError. Narrowing to OSError would reproduce #152 on those triggers.
        return False


def _current_repo_path() -> str:
    path = STORE_DIR / ".current_repo"
    try:
        if path.exists():
            val = path.read_text(encoding="utf-8").strip()
            return val if _is_sane_repo(val) else ""
    except Exception:
        # Broad for the same reasons as anchor_repo, its write-side twin: OSError is the
        # expected failure (unreadable pointer under a sandbox), but the shell hooks write
        # this file with `printf` — raw bytes, no encoding contract — so a non-UTF-8
        # pointer raises UnicodeDecodeError (a ValueError), and _is_sane_repo can raise
        # RuntimeError via Path.home(). This sits in _resolve_repo, on EVERY store call:
        # anything escaping here crashes the host far from the file that caused it.
        pass
    return ""


def _resolve_repo(repo_path: str) -> str:
    # Precedence: an explicit caller argument always wins; then the repo bound to this
    # server process (cwd-derived, per-session — cannot be cross-contaminated); then the
    # shared .current_repo pointer as a last resort. Each is sanity-checked.
    if _is_sane_repo(repo_path):
        return repo_path
    if repo_path:  # caller passed something non-sane (e.g. ~/.claude) — never honor it
        return _SESSION_REPO or _current_repo_path()
    if _SESSION_REPO:
        return _SESSION_REPO
    return _current_repo_path()

def _legacy_slug(repo_path: str) -> str:
    # Pre-injective scheme: kept literal `_`/`-`, so `/a/my.repo`, `/a/my_repo`, and
    # `/a/my repo` all collapsed to the same file. Retained only to migrate old stores.
    return re.sub(r"[^a-zA-Z0-9_-]", "_", repo_path.strip("/"))


def _slug(repo_path: str) -> str:
    # Append a short path hash so the slug is injective: paths that map to the same
    # readable base (a `.`/space vs a literal `_`) no longer share one store file.
    digest = hashlib.sha1(repo_path.encode("utf-8")).hexdigest()[:8]
    return f"{_legacy_slug(repo_path)}-{digest}"


def _store_path(repo_path: str) -> Path:
    # Best-effort create: a reader only needs the path, and on a host where ~/.contexer
    # can be neither created nor written (#152) raising here would crash the hook that
    # merely wanted to LOAD context. Writers still surface the failure at their own write.
    try:
        STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        pass
    path = STORE_DIR / f"{_slug(repo_path)}.json"
    # Back-compat: migrate a pre-hash store file to the new name on first access so an
    # upgrade never silently orphans existing context. os.replace is atomic; if a
    # colliding repo already claimed the legacy file, the loser just starts fresh.
    if not path.exists():
        legacy = STORE_DIR / f"{_legacy_slug(repo_path)}.json"
        if legacy.exists():
            try:
                os.replace(legacy, path)
            except OSError:
                return legacy if legacy.exists() else path
    return path


def _load(repo_path: str) -> dict:
    path = _store_path(repo_path)
    if path.exists():
        try:
            # encoding pinned to match _atomic_write — never the locale default
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            # Treat a corrupted or unreadable file as empty — recovers from concurrent-write races.
            data = None
        # Valid-but-non-object JSON ([], null, 42) parses fine but would crash every
        # downstream data["entries"] access — treat the same as corruption.
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            # Transparently upgrade legacy entries to the revision model so every reader
            # sees the normalized shape. Idempotent + in-memory; persisted on next _save.
            _migrate_entries(data)
            return data
    return {"repo_path": repo_path, "entries": []}


def _atomic_write(path: Path, text: str) -> None:
    """Write via a unique temp file + os.replace so readers never see a torn file.

    mkstemp creates the temp file with mode 0o600 (umask-independent), so the store
    is never readable by others — not even between creation and the final rename.
    Deliberate trade-offs: no fsync (atomic, not power-loss durable — acceptable for
    a context cache), and if `path` is a symlink it is replaced by a regular file."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)  # no-op after a successful replace


def _save(repo_path: str, data: dict) -> None:
    path = _store_path(repo_path)
    _atomic_write(path, json.dumps(data, indent=2, ensure_ascii=False))
    # The retrieval index is a disposable sidecar maintained ONLY here — every store
    # writer already holds the store lock, so per-prompt readers never rebuild it.
    _write_retrieval_index(repo_path, data)


@contextlib.contextmanager
def _store_lock(slug: str):
    """Serialize a load→mutate→save critical section for one store across processes.

    Atomic writes prevent a *torn* file, but two sessions writing the same store
    concurrently still race: both read, both append, the second overwrites the first
    (lost update). An exclusive advisory lock on a per-store `.lock` sidecar makes the
    read-modify-write atomic so concurrent writers serialize instead of clobbering.
    Best-effort: if locks are unavailable (non-POSIX), degrade to no serialization
    rather than fail the write."""
    if fcntl is None:                  # pragma: no cover - non-POSIX fallback
        yield
        return
    STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    lock_path = STORE_DIR / f"{slug}.lock"
    f = open(lock_path, "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        finally:
            f.close()


# ── Global store ───────────────────────────────────────────────────────────────

def _global_path() -> Path:
    # Best-effort create, exactly as _store_path: readers only need the path, and on a
    # host where ~/.contexer can be neither created nor written (#152) raising here would
    # crash a hook that merely wanted to load global rules. Writers still surface it.
    try:
        STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    except OSError:
        pass
    return STORE_DIR / f"{GLOBAL_SLUG}.json"


def _load_global() -> dict:
    path = _global_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("entries"), list):
            return data
    return {"repo_path": GLOBAL_SLUG, "entries": []}


def _save_global(data: dict) -> None:
    _atomic_write(_global_path(), json.dumps(data, indent=2, ensure_ascii=False))


def update_global_decision(content: str, session_id: str, subtype: str = "", title: str = "") -> tuple[bool, str | None]:
    """Store a cross-cutting decision in the global store.
    Only constraint and convention subtypes are accepted — architecture and pattern
    decisions are always repo-specific.
    """
    if subtype and subtype not in ("constraint", "convention"):
        return False, None
    subtype = subtype or "convention"
    if not _is_storable(content):
        return False, None
    with _store_lock(GLOBAL_SLUG):
        data = _load_global()
        decisions_only = [e for e in data["entries"] if e["type"] == "decision"]
        match = _find_match(content, decisions_only)
        if match is not None:
            # Mirror the repo path: a restated global rule records a recurrence (×N
            # confidence + eviction protection) instead of being silently dropped.
            _record_recurrence(match, session_id)
            _save_global(data)
            return False, None
        entry = _new_decision_entry(content, session_id, subtype, status="approved", title=title)
        data["entries"].append(entry)
        data["entries"] = _keep_top(data["entries"], MAX_ENTRIES, pin_last=True)
        _save_global(data)
        return True, entry["id"]


def get_global_decisions(entry_type: str = "") -> list:
    """Returns all decisions from the global store, optionally filtered by subtype."""
    data = _load_global()
    decisions = [e for e in data.get("entries", []) if e["type"] == "decision"]
    if entry_type:
        decisions = [d for d in decisions if d.get("subtype") == entry_type]
    return decisions


def get_global_context(query: str = "", entry_type: str = "", limit: int = 0) -> str:
    """Formatted output of global store decisions."""
    data = _load_global()
    entries = data.get("entries", [])
    if not entries:
        return "No global context stored. Use update_global_context to add cross-cutting conventions and constraints."

    decisions = [e for e in entries if e["type"] == "decision"]
    is_filtered = bool(query or entry_type)

    if entry_type:
        decisions = [d for d in decisions if d.get("subtype") == entry_type]
    if query:
        pat = _query_pattern(query)
        decisions = [d for d in decisions if _matches_query(pat, d)]

    display_limit = limit if limit > 0 else (_FILTERED_DISPLAY if is_filtered else _UNFILTERED_DISPLAY)
    lines = ["# Global context (applies to all repos)\n"]

    if decisions:
        total = len(decisions)
        shown = _keep_top(decisions, display_limit)
        filter_note = f" — showing {len(shown)} of {total}" if total > display_limit else ""
        lines.append(f"## Global decisions{filter_note}")
        for d in shown:
            subtype_tag = f" [{d['subtype']}]" if d.get("subtype") else ""
            title, body = _title_and_body(d)
            lines.append(f"- [{d['timestamp'][:10]}]{subtype_tag}{_recur_suffix(d)} {title}")
            if body is not None:
                lines.append(f"    {body}")
    elif is_filtered:
        lines.append("No matching global decisions found.")

    return "\n".join(lines)


_PUNCT_RE = re.compile(r"[^\w\s]")

def _tokenize(text: str) -> set[str]:
    """Lowercase, strip punctuation, split on whitespace. Fixes comma-attached tokens."""
    return set(_PUNCT_RE.sub("", text.lower()).split())


def _overlap_ratio(a: set[str], b: set[str]) -> float:
    """Token overlap of two token sets: |a∩b| / max(|a|, |b|). This is the metric the
    novelty filter thresholds at 0.7 to reject duplicates; team-context dedup reuses it so
    both sites judge "same rule" identically. 0.0 when either side is empty."""
    if not a or not b:
        return 0.0
    hi = len(a) if len(a) > len(b) else len(b)
    return len(a & b) / hi


def _query_pattern(query: str) -> "re.Pattern":
    """Case-insensitive search pattern for a user query. A leading `\\b` is added only
    when the query starts with a word char — prepending it before `.`, `@`, `#` would
    never fire, so queries like `.env` / `@auth` / `#deploy` silently matched nothing."""
    q = query.lower()
    prefix = r"\b" if q[:1].isalnum() else ""
    return re.compile(prefix + re.escape(q), re.IGNORECASE)


def _matches_query(pat: "re.Pattern", row: dict) -> bool:
    """Whether a decision row matches a query, searching the TITLE as well as the content.

    Decisions render title-led, so the heading is often the part a developer remembers - a
    query that hits only the title must not silently drop the row (an authored title can be
    wholly different words from the body). Mirrors the web app's search rule."""
    return bool(pat.search(row.get("content", "")) or pat.search(row.get("title") or ""))


def _find_match(content: str, existing: list) -> dict | None:
    """Returns the first existing entry with >70% token overlap, or None.

    Cheap size pre-filter before the set intersection: the ratio |A∩B|/max(|A|,|B|)
    can exceed 0.7 only if min(|A|,|B|)/max(|A|,|B|) does (since |A∩B| ≤ min). So a
    length check skips the expensive intersection for most candidates — this is what
    keeps write latency flat as the store fills toward MAX_ENTRIES."""
    if not existing:
        return None
    tokens = _tokenize(content)
    if not tokens:
        return None
    n = len(tokens)
    for entry in existing:
        other = _tokenize(entry.get("content", ""))
        if not other:
            continue
        m = len(other)
        hi = n if n > m else m
        lo = m if n > m else n
        if lo <= 0.7 * hi:                       # overlap can't clear the bar — skip intersection
            continue
        if _overlap_ratio(tokens, other) > 0.7:
            return entry
    return None


def _containment_ratio(a: set[str], b: set[str]) -> float:
    """|a∩b| / min(|a|,|b|) — 1.0 when the smaller token set is fully inside the larger.
    Complements _overlap_ratio (max-denominator), which a superset restatement evades:
    "Always commit automatically" inside "Always commit automatically after approvals …"
    scores 0.30 on max but 1.0 on min. Used ONLY for capture_user_constraint routing —
    bootstrap idempotence, memory sync, and team dedup depend on the max-denominator
    metric exactly as-is, so _overlap_ratio/_find_match stay untouched."""
    if not a or not b:
        return 0.0
    lo = len(a) if len(a) < len(b) else len(b)
    return len(a & b) / lo


def _find_containment(content: str, existing: list) -> dict | None:
    """Best-match entry whose containment with `content` exceeds 0.7, or None.

    First-hit-wins was wrong here: a short generic rule ("Always commit") contained in
    many longer directives scores 1.0 containment against ALL of them, so whichever one
    happened to iterate first "won" regardless of which is actually closest. Instead,
    every candidate above the threshold is scored and the best one picked: highest
    containment ratio first, ties broken by highest _overlap_ratio (closest overall
    content — the max-denominator metric penalizes a short match more than a near-equal-
    length one), remaining ties keep earliest iteration order (strict '>' comparisons
    never displace an earlier equally-good candidate)."""
    tokens = _tokenize(content)
    if not tokens:
        return None
    best = None
    best_containment = 0.0
    best_overlap = 0.0
    for entry in existing:
        other = _tokenize(entry.get("content", ""))
        if not other:
            continue
        containment = _containment_ratio(tokens, other)
        if containment <= 0.7:
            continue
        overlap = _overlap_ratio(tokens, other)
        if best is None or containment > best_containment or (
                containment == best_containment and overlap > best_overlap):
            best = entry
            best_containment = containment
            best_overlap = overlap
    return best


_NEAR_MISS_FLOOR = 0.25
_NEAR_MISS_CAP = 3


def _near_misses(content: str, existing: list) -> list[str]:
    """Stored rules in the lexical grey zone (0.25 ≤ _overlap_ratio < 0.7) — the synonym-
    phrasing band token dedup cannot resolve ("commit on approval" vs "commit
    automatically"). Rendered as `short-id "preview"` items for the capture ack, top
    _NEAR_MISS_CAP by overlap; consolidation is the developer's call, never automatic."""
    tokens = _tokenize(content)
    if not tokens:
        return []
    scored = []
    for entry in existing:
        ratio = _overlap_ratio(tokens, _tokenize(entry.get("content", "")))
        if _NEAR_MISS_FLOOR <= ratio < 0.7:
            scored.append((ratio, entry))
    scored.sort(key=lambda t: t[0], reverse=True)
    out = []
    for _ratio, e in scored[:_NEAR_MISS_CAP]:
        preview = e.get("title") or _derive_title(_current_content(e))
        out.append(f'{(e.get("id") or "")[:8]} "{preview}"')
    return out


def _is_storable(content: str) -> bool:
    """Content needs at least one real token to be a storable decision. Punctuation-
    or whitespace-only content is rejected — this preserves the pre-refactor behavior
    where empty-token content was treated as non-novel and never stored."""
    return bool(_tokenize(content))


def _normalize_title(title: str) -> str:
    """Collapse a title to a single stripped line, capped at MAX_TITLE_LEN (adds an
    ellipsis when it has to cut)."""
    one_line = " ".join(title.split())
    if len(one_line) <= MAX_TITLE_LEN:
        return one_line
    return one_line[:MAX_TITLE_LEN - 1].rstrip() + "…"


def _derive_title(content: str) -> str:
    """Deterministic fallback title from content: verbatim when the whole thing is short,
    otherwise the first sentence/line, capped at MAX_TITLE_LEN."""
    one_line = " ".join(content.split())
    if not one_line:
        return ""
    if len(one_line) <= MAX_TITLE_LEN:
        return one_line
    first_line = content.strip().splitlines()[0]
    first_sentence = re.split(r"(?<=[.!?])\s", first_line, maxsplit=1)[0]
    return _normalize_title(first_sentence)


def _title_and_body(entry: dict, content: str | None = None) -> tuple[str, str | None]:
    """Rendering primitive: (title, body) for a decision. `title` is the entry's title
    (or derived from content). `body` is the content to show on the indented second line,
    or None when it would merely repeat the title (a short decision whose derived title IS
    its content) — callers skip the duplicate. Single content source: the current revision."""
    body = _current_content(entry) if content is None else content
    title = entry.get("title") or _derive_title(body)
    collapsed = " ".join(body.split())
    return title, (body if collapsed and collapsed != title else None)


def _is_novel(content: str, existing: list) -> bool:
    if not _is_storable(content):
        return False
    return _find_match(content, existing) is None


def _passes_filter(content: str, existing: list) -> bool:
    # Novelty is a prerequisite veto — duplicates are rejected regardless of signal keywords.
    # Novel content always passes: update_context is only called for significant decisions.
    decisions_only = [e for e in existing if e["type"] == "decision"]
    return _is_novel(content, decisions_only)


def _session_set(match: dict) -> set[str]:
    """Distinct sessions that have hit this entry. Reconstructs from the legacy
    single `session_id` for entries written before `session_ids` existed."""
    sessions = set(match.get("session_ids") or [])
    legacy = match.get("session_id")
    if legacy:
        sessions.add(legacy)
    return sessions


def _record_recurrence(match: dict, session_id: str = "") -> None:
    """Record another near-duplicate hit on a matched entry: bump occurrence_count and
    track the distinct session that produced it.

    The count drives display ranking, eviction protection, and the ×N confidence marker
    — it does NOT change the entry's subtype. A decision's category (architecture,
    pattern, constraint, convention) is a semantic judgment made when it is captured,
    never inferred from how often the same text recurs. Recurrence measures repetition,
    not reuse-across-different-problems, so it cannot tell a genuine pattern from a
    one-off decision that simply got restated."""
    match["occurrence_count"] = match.get("occurrence_count", 1) + 1
    sessions = _session_set(match)
    if session_id:
        sessions.add(session_id)
    match["session_ids"] = sorted(sessions)


def _keep_top(items: list, limit: int, pin_last: bool = False) -> list:
    """Keep the `limit` most-entrenched items, returned in chronological order.

    Ranking is by occurrence_count (how often the decision recurred), with recency
    as the tiebreak — so a proven, frequently-rediscovered decision survives both
    storage eviction at MAX_ENTRIES and display truncation, instead of being dropped
    just for being old or not the most recent. Items at or below the cap are returned
    unchanged, so behaviour is identical until a cap is actually exceeded.

    pin_last guarantees the final item survives, used by storage callers that append
    then cap: without it a fresh count-1 entry could be evicted by older high-count
    entries while the caller still reports the write as stored."""
    if len(items) <= limit:
        return items
    pinned = [items[-1]] if (pin_last and limit >= 1) else []
    pool = items[:-1] if pinned else items
    ranked = sorted(
        pool,
        key=lambda x: (x.get("occurrence_count", 1), x.get("updated_at") or x.get("timestamp", "")),
        reverse=True,
    )
    kept = ranked[: limit - len(pinned)] + pinned
    kept.sort(key=lambda x: x.get("timestamp", ""))  # restore append/chronological order
    return kept


def _recur_suffix(d: dict) -> str:
    """' ×N' confidence marker for a decision seen more than once; '' for a one-off."""
    count = d.get("occurrence_count", 1)
    return f" ×{count}" if count > 1 else ""


# ── Confidence levels and classification ───────────────────────────────────────

# Patterns that identify bootstrap-scan-generated facts (Level 1 — auto approved).
# These match the exact output formats that bootstrap_scan produces; AI decisions
# that happen to start with the same prefix are still treated as Level 1.
_SCAN_FACT_PATTERNS = re.compile(
    r"^(?:"
    r"Python project|Node\.js project|Go module|Go version|Rust project"
    r"|Package manager:|Test framework:|Linting(?:/formatting)?:|Formatting:|Type checking:"
    r"|CI/CD:|Containerized|Local dev:|Infrastructure as code:|Deployment:"
    r"|Monorepo:|Data store(?:s)?:|ORM / query builder:|Auth:|Cloud:|Payments:"
    r"|Email:|Messaging:|AI:|Task queue:|Search:|Architecture:"
    r")",
    re.IGNORECASE,
)

# Content signals that indicate a Level 3 (approval-required) engineering decision:
# intentional alternatives, org-level mandates, ownership, and explicit prohibitions.
_L3_CONTENT_SIGNALS = re.compile(
    r"\b(?:"
    r"instead\s+of|rather\s+than"                      # competing alternatives
    r"|intentionally|deliberately|by\s+design"          # conscious choices
    r"|prohibit(?:ed)?|forbidden|banned"                # prohibitions
    r"|mandatory|mandated"                              # mandates
    r"|standardize(?:d)?\s+on|standardizing\s+on"      # org-level standards
    r"|we\s+(?:standardize|require|adopted)"            # "we standardize/require/adopted X"
    r"|must\s+(?:use|not\s+use|never|always)"          # explicit musts
    r"|only\s+(?:aws|gcp|azure|postgres|mysql|kafka|rabbitmq|redis|mongodb|s3|lambda)\b"  # tech lock-in
    r"|(?:team|platform|service)\s+owns"               # ownership
    r"|all\s+services\s+must"                          # cross-cutting mandate
    r"|deploy\s+only\s+to"                             # deployment constraint
    r")\b",
    re.IGNORECASE,
)

# Confidence threshold for injecting 'suggested' decisions at session start.
_SUGGESTED_INJECT_THRESHOLD = 0


def _entry_status(entry: dict) -> str:
    """Returns the effective status of an entry, defaulting to 'approved' for old entries."""
    return entry.get("status", "approved")


def _classify_level(content: str, subtype: str, created_by: str) -> str:
    """Classify a decision into one of three confidence levels.
    Returns 'auto' | 'suggested' | 'approval_required'.

    Level 1 (auto): scan-detected repo facts, human-stated rules (already confirmed).
    Level 2 (suggested): AI-captured patterns and non-constraining architecture.
    Level 3 (approval_required): all constraints, plus arch decisions with L3 signals.
    """
    if created_by in ("scan", "human"):
        return "auto"
    if created_by == "plan":
        # Plan intent is provisional - validated only after implementation. A plan decision is
        # never born 'approved': constraints still need ratification, everything else is suggested.
        # Reconciliation at the settle checkpoint promotes/revises/drops it.
        return "approval_required" if subtype == "constraint" else "suggested"
    if subtype == "constraint":
        return "approval_required"
    if created_by == "bootstrap" and subtype in ("convention", "pattern"):
        return "auto"
    if _SCAN_FACT_PATTERNS.match(content):
        return "auto"
    if _L3_CONTENT_SIGNALS.search(content):
        return "approval_required"
    return "suggested"


def _level_to_status(level: str) -> str:
    return {"auto": "approved", "suggested": "suggested", "approval_required": "pending_approval"}.get(level, "suggested")


# Categories whose CHANGE is high-stakes enough to warrant developer approval (the plan's
# "only ask for approval when: architecture / constraints / ownership / deployment /
# technology standards change"). Pattern/convention updates apply in place silently.
_SIGNIFICANT_UPDATE_SUBTYPES = frozenset({"architecture", "constraint"})


def _update_needs_approval(subtype: str, created_by: str) -> bool:
    """A change to an existing decision becomes a Suggested Update (needs approval) only
    when it is AI-inferred AND touches a high-stakes category. Human-stated changes and
    trusted sources (scan/bootstrap) apply directly; trivial categories (pattern/
    convention) update in place silently. This is the trivial-vs-significant split."""
    if created_by in ("human", "scan", "bootstrap"):
        return False
    return subtype in _SIGNIFICANT_UPDATE_SUBTYPES


def _compute_confidence(entry: dict) -> tuple[int, list[str]]:
    """Compute a confidence score (0-100) and evidence factors from an entry's metadata.
    Confidence reflects available evidence, NOT AI certainty."""
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


# Prescriptive constraint/convention signals in user prompts.
# al+w(?:ay|ya)s catches "always", "allways" (double-l), "alwyas" (transposition).
_CONSTRAINT_TRIGGER = re.compile(
    r"\b(?:"
    r"al+w(?:ay|ya)s"               # always + common typos: allways, alwyas
    r"|never"                        # never
    r"|must\s+(?:always|never)"      # must always / must never
    r"|should\s+(?:always|never)"    # should always / should never
    r"|at\s+all\s+times"             # at all times
    r"|every\s+time"                 # every time
    r"|each\s+time"                  # each time
    r"|no\s+exceptions?"             # no exception / no exceptions
    r"|without\s+exception"          # without exception
    r"|as\s+a\s+rule"                # as a rule
    r"|make\s+it\s+a\s+rule"        # make it a rule
    r"|from\s+now\s+on"             # from now on
    r"|going\s+forward"              # going forward
    r"|henceforth"                   # henceforth
    r"|ensure\s+(?:you\s+|that\s+you\s+)"       # ensure you / ensure that you
    r"|make\s+sure\s+(?:you\s+|that\s+you\s+)"  # make sure you / make sure that you
    r"|(?:make|create|add|set|establish)\s+(?:a\s+|the\s+)?rule"  # "create a rule …"
    r"|^\s*rule(?=\s*[:\-])"        # "rule: never X" / "rule - …" at the start
    r"|do\s*n['’]?t"                # don't (prohibition)
    r"|do\s+not"                    # do not
    r"|avoid"                       # avoid
    r"|no\s+longer"                 # no longer
    r"|stop\s+\w+ing"              # stop doing / stop using
    r")\b",
    re.IGNORECASE,
)

# Soft conversational prose that contains "don't/do not/avoid" but is NOT a directive:
# "don't worry about the tests", "I don't know why", "don't hesitate to ask". These are
# excluded so the broadened prohibition triggers above don't generate false constraints.
_SOFT_PROSE_EXCLUDE = re.compile(
    r"\b(?:"
    r"do\s*n['’]?t\s+(?:worry|hesitate|bother|forget|mind|know|think|see|want|like|"
    r"have\s+to|need\s+to|get|understand)\b"
    r"|do\s+not\s+(?:worry|hesitate|bother|forget|mind|know|think|see|understand)\b"
    r"|i\s+do\s*n['’]?t\b"     # "I don't ..." — speaking about self, not a rule
    r")",
    re.IGNORECASE,
)

# Profanity and frustration words that carry no directive meaning.
# These are stripped before storing so the rule itself is preserved cleanly.
# Covers both fully-spelled and asterisk/symbol-censored forms (f***, sh**).
_PROFANITY = re.compile(
    r"\b(?:"
    r"f+u+c+k+(?:ing|ed|er|s|face|wit|head)?"   # fuck + variations
    r"|f[\*\#@!]+(?:ing|ed|er|s)?"               # f*** / f**king (censored)
    r"|sh[i1\*\#@!]+t+(?:ty|hole|head)?"         # shit / sh*t
    r"|a+s+s+(?:hole|hat|wipe)?"                 # ass + variations
    r"|b[i1\*]+t+c+h+(?:es|ing|y)?"              # bitch + variations
    r"|d+a+m+(?:n+(?:it)?|m+it)?"               # damn / damnit / dammit
    r"|c+r+a+p+(?:py)?"                          # crap
    r"|wtf|ffs|stfu|omfg"                        # abbreviations
    r")\b",
    re.IGNORECASE,
)

# Common frustration openers: "what the hell,", "oh my god," — stripped before storing.
_FRUSTRATION_OPENER = re.compile(
    r"^(?:what\s+the\s+\w+|oh\s+(?:my\s+)?\w+|for\s+\w+(?:'s)?\s+sake)[,\s!]*",
    re.IGNORECASE,
)


# Trailing filler connectors that add no meaning to a stored rule.
# "always use pip hence this would work" → "always use pip"
# NOTE: \bhence\b (not bare "hence") so "henceforth" is NOT accidentally matched here.
_TRAILING_FILLER = re.compile(
    r"\s*(?:\bhence\b|so\s+(?:that|it|we|this)|because\s+(?:of\s+)?(?:this|that|it)|"
    r"as\s+(?:this|that|it)|that\s+way|which\s+(?:means|would)|"
    r"and\s+(?:it\s+)?(?:should|would|will)\s+work|this\s+(?:should|would|will)\s+(?:work|help))"
    r".*$",
    re.IGNORECASE,
)


def _sanitize_directive(text: str) -> str:
    """Strip profanity, frustration framing, and trailing filler from a directive.
    Preserves only the rule itself. Called before storing any auto-captured constraint."""
    text = _FRUSTRATION_OPENER.sub("", text).strip()
    text = _PROFANITY.sub("", text)
    text = _TRAILING_FILLER.sub("", text)          # strip "hence this would work" etc.
    text = re.sub(r"[!]{2,}", "!", text)           # !!!! → !
    text = re.sub(r"[?]{2,}", "?", text)           # ???? → ?
    # If the whole message is ≥ 70% uppercase letters (shouting), normalise to sentence case.
    alpha = [c for c in text if c.isalpha()]
    if alpha and sum(1 for c in alpha if c.isupper()) / len(alpha) >= 0.7:
        text = text.lower()
    else:
        # Otherwise only normalise isolated all-caps words of 4+ letters (not short acronyms)
        text = re.sub(r"\b([A-Z]{4,})\b", lambda m: m.group(1).capitalize(), text)
    text = re.sub(r"\s{2,}", " ", text).strip().strip("!?,. ")
    return text[0].upper() + text[1:] if text else text

# Sarcasm/irony signals — prompts matching these are not stored as constraints even if
# a directive trigger word is present. "love always use pip", "yeah right never again /s".
_SARCASM_EXCLUDES = re.compile(
    r"(?:"
    r"/s\s*$"                                      # /s at end (explicit sarcasm marker)
    r"|^(?:love|oh\s+sure|yeah\s+right|oh\s+great|sure,?|lol,?|haha,?)\s+"  # ironic openers
    r")",
    re.IGNORECASE,
)

# "forward-looking practice" signals — convention subtype when used alone (without always/never)
# "as a rule" and "make it a rule" are soft-practice signals like "from now on" / "going forward".
_CONVENTION_SIGNALS = re.compile(
    r"\b(?:from\s+now\s+on|going\s+forward|henceforth|as\s+a\s+rule|make\s+it\s+a\s+rule)\b",
    re.IGNORECASE,
)

# Personal-descriptive patterns — these describe existing habits, not directives.
# "I always get this error", "we never did that", "it always worked before" are descriptive.
# NOTE: "it should always" is NOT caught here because "should" sits between "it" and "always".
_PERSONAL_DESCRIPTOR = re.compile(
    r"\b(i|we|it)\s+(have\s+|has\s+|did\s+|does\s+)?(always|never)\b",
    re.IGNORECASE,
)


# A genuine directive is short and standalone ("always use conventional commits").
# Anything longer is a pasted blob (a README, an issue dump, a multi-step task) that
# merely *contains* a directive word — storing the whole thing pollutes the store.
_MAX_DIRECTIVE_LEN = 300
# Hook/tool-injected text is never a user directive. The constraint hook fires on
# whatever lands in UserPromptSubmit, including agent-framework notifications and
# Contexer's own injected context — guard against capturing those as constraints.
_SYSTEM_TEXT_PREFIXES = (
    "<task-notification", "<system-reminder", "<persisted-output",
    "[contexer", "contexer:",
)

# Deictic referents point at an object only this conversation can resolve — a strong
# signal the directive is session-scoped intent, not a standing rule. Still stored
# (never dropped), just not auto-trusted. Narrowly scoped to avoid v1's false positives:
#  - this/these/those, UNLESS immediately scoping the rule to the store itself
#    ("this repo/project/repository/codebase" is a legitimate standing rule).
#  - "it", UNLESS inside the self-resolving idiom "make it a <rule/convention/...>" —
#    an unresolved pronoun ("always apply it before deployment") only this conversation
#    can resolve must not become a trusted rule (fail toward review, not silent trust).
#  - "here", UNLESS trailing — "always use uv here" means "in this repo" (durable,
#    like the this-repo exemption); only a mid-directive here is conversation-local.
# Bare "that" is dropped entirely: relative/complementizer uses ("code that fails",
# "ensure that X") dominate and are never deictic.
_DEICTIC_THIS_THESE_THOSE = re.compile(
    r"\b(?:this|these|those)\b(?!\s+(?:repo|repository|project|codebase)\b)", re.IGNORECASE)
_DEICTIC_IT = re.compile(r"(?<!make\s)\bit\b(?!\s+a\s+\w)", re.IGNORECASE)
_DEICTIC_HERE = re.compile(r"\bhere\b(?!\W*$)", re.IGNORECASE)


def _is_deictic(content: str) -> bool:
    """True if `content` carries a conversation-local referent (see _DEICTIC_* above)."""
    return bool(_DEICTIC_IT.search(content)
                or _DEICTIC_HERE.search(content)
                or _DEICTIC_THIS_THESE_THOSE.search(content))


def _is_prescriptive_constraint(text: str) -> tuple[bool, str]:
    """Returns (is_constraint, subtype). Detects user-stated directives.
    Excludes descriptive first-person/it uses ('I always get this error', 'it always worked')
    and ironic/sarcastic statements ('love always use pip', 'yeah right /s')."""
    t = text.strip()
    # Pasted blobs and tool/system-injected text are never clean user directives.
    if not t or len(t) > _MAX_DIRECTIVE_LEN:
        return False, ""
    if t.lower().startswith(_SYSTEM_TEXT_PREFIXES) or "```" in t:
        return False, ""
    if t.endswith("?"):
        return False, ""
    if _SARCASM_EXCLUDES.search(text.strip()):
        return False, ""
    if not _CONSTRAINT_TRIGGER.search(text):
        return False, ""
    # Strip soft conversational prose ("don't worry", "I don't know"); if a broadened
    # prohibition trigger only matched inside that prose, it was not a directive.
    deprosed = _SOFT_PROSE_EXCLUDE.sub("", text)
    if not _CONSTRAINT_TRIGGER.search(deprosed):
        return False, ""
    # Strip descriptive personal instances; if nothing remains, it was purely descriptive
    cleaned = _PERSONAL_DESCRIPTOR.sub("", deprosed)
    if not _CONSTRAINT_TRIGGER.search(cleaned):
        return False, ""
    # Pure forward-looking practice signals (no always/never) → convention
    # Everything else (mandatory requirements, prohibitions) → constraint
    is_soft = bool(_CONVENTION_SIGNALS.search(cleaned))
    has_hard = bool(re.search(
        r"\b(?:al+w(?:ay|ya)s|never|must|should|do\s*n['’]?t|do\s+not|avoid|no\s+longer|stop)\b",
        cleaned, re.IGNORECASE))
    subtype = "convention" if (is_soft and not has_hard) else "constraint"
    return True, subtype


def capture_user_constraint(
    repo_path: str, prompt: str, session_id: str,
    near_misses: list | None = None,
) -> tuple[str, str, str] | tuple[None, None, None]:
    """Called on every UserPromptSubmit. Detects prescriptive 'always/never/from now on' directives
    and stores them as decisions. A directive carrying a deictic referent (see _is_deictic) is
    stored but NOT auto-trusted — it lands pending_approval so the developer can generalize,
    approve, or discard it via review_pending, since "this feature"/"It ..." only means
    something to the conversation that typed it.

    A clean (non-deictic) restatement that matches — via the standard >70% token-overlap
    gate — a still-pending twin THIS path created earlier is treated as the developer's
    generalization: it promotes the pending entry to approved in place (status "promoted"),
    rather than being silently dropped as a duplicate. Any other match (an already-approved
    entry, or a still-deictic restatement of the pending twin) stays today's silent no-op.
    'ignored' entries are excluded from matching here ONLY — a user re-typing a rule after
    discarding a false positive gets a fresh entry, not a permanently blocked match.

    When _find_match misses, a containment check (|∩|/min > 0.7, see _containment_ratio)
    against the same candidates catches superset/subset restatements the max-denominator
    metric is blind to, and routes them onto the matched entry (Suggested Update,
    promotion, in-place amend, or recurrence — see _route_containment) instead of
    accumulating a new overlapping entry.

    `near_misses`, when a list is passed, is extended in place with grey-zone lexical
    matches (see _near_misses) for a brand-new entry — the caller forwards it to
    constraint_ack so the developer can confirm a consolidation.

    Returns (entry_id, sanitized_content, status) if stored, (None, None, None) otherwise.
    `status` is one of "approved" | "pending_approval" | "promoted" | "revision_proposed" |
    "revision_already_pending" — pass it to constraint_ack() for the matching notice."""
    is_constraint, subtype = _is_prescriptive_constraint(prompt)
    if not is_constraint:
        return None, None, None
    content = _sanitize_directive(prompt.strip())[:600]
    if not _is_storable(content):
        return None, None, None
    deictic = _is_deictic(content)
    status = "pending_approval" if deictic else "approved"
    with _store_lock(_slug(repo_path)):
        data = _load(repo_path)
        # 'ignored' entries never block a re-typed rule from landing fresh (Fix 3).
        decisions_only = [e for e in data["entries"]
                          if e["type"] == "decision" and e.get("status") != "ignored"]
        # This hook fires on every prompt; a near-duplicate is normally a silent no-op (no
        # write) — EXCEPT a clean restatement of this path's own pending twin, which promotes.
        match = _find_match(content, decisions_only)
        if match is not None:
            # A pending entry with created_by="human" can only have been born here: the normal
            # update_decision path always classifies created_by="human" as auto-approved.
            if (not deictic and match.get("status") == "pending_approval"
                    and match.get("created_by") == "human"):
                now = datetime.now(timezone.utc).isoformat()
                _append_revision(match, content, source="human", approved_at=now)
                match["status"] = "approved"
                match["approved_at"] = now
                match["approved_by"] = "human"
                _record_recurrence(match, session_id)
                _save(repo_path, data)
                return match["id"], _current_content(match), "promoted"
            return None, None, None
        # Containment routing: a superset/subset restatement of a stored rule evades the
        # max-denominator metric above — consolidate onto the first contained entry
        # instead of accumulating a new overlapping one.
        hit = _find_containment(content, decisions_only)
        if hit is not None:
            return _route_containment(repo_path, data, hit, content, subtype,
                                      deictic, session_id)
        if near_misses is not None:
            near_misses.extend(_near_misses(content, decisions_only))
        entry = _new_decision_entry(content, session_id, subtype,
                                    created_by="human", status=status)
        data["entries"].append(entry)
        data["entries"] = _keep_top(data["entries"], MAX_ENTRIES, pin_last=True)
        _save(repo_path, data)
        # Deliberately does NOT arm the .pending_review flag: the in-band ack (constraint_ack)
        # already notifies the developer, and the SessionStart pending-count pointer covers
        # persistence — a second nudge from this path would double up.
        return entry["id"], content, status


def _route_containment(repo_path: str, data: dict, hit: dict, content: str, subtype: str,
                       deictic: bool, session_id: str) -> tuple:
    """Route a containment hit from capture_user_constraint onto the matched entry `hit`.
    Called under the store lock; saves and returns the capture 3-tuple. Never creates a
    new entry, and never silently replaces a trusted rule's current revision.

    New content LONGER (the observed bug — superset restatement):
      - pending twin (born on this path) + clean  → promote with the fuller content
      - pending twin + deictic                    → amend v1 in place, stays pending
      - other pending (AI-captured)               → recurrence (base needs review first)
      - approved/suggested, no unresolved proposal → attach a proposed_revision (Suggested
                                                    Update — approval promotes it)
      - approved/suggested, DIFFERENT proposal
        already pending                            → leave the existing proposal untouched,
                                                    return "revision_already_pending" (never
                                                    clobber an unreviewed Suggested Update)
    New content SHORTER (user re-types the terse version):
      - pending twin + clean → promote keeping the fuller existing content
      - otherwise            → recurrence, silent no-op like an ordinary duplicate."""
    now = datetime.now(timezone.utc).isoformat()
    longer = len(_tokenize(content)) > len(_tokenize(hit.get("content", "")))
    pending = _entry_status(hit) == "pending_approval"
    pending_twin = pending and hit.get("created_by") == "human"

    def _recur_silently():
        _record_recurrence(hit, session_id)
        _save(repo_path, data)
        return None, None, None

    if longer:
        if pending_twin:
            if deictic:
                # Pre-approval amend precedent: rewrite v1 in place, stays pending.
                rev = _current_revision(hit)
                if rev is not None:
                    rev["content"] = _normalize_content(content)
                _sync_decision_cache(hit)
                hit["updated_at"] = now
                _record_recurrence(hit, session_id)
                _save(repo_path, data)
                return hit["id"], _current_content(hit), "pending_approval"
            _append_revision(hit, content, source="human", approved_at=now)
            hit["status"] = "approved"
            hit["approved_at"] = now
            hit["approved_by"] = "human"
            _record_recurrence(hit, session_id)
            _save(repo_path, data)
            return hit["id"], _current_content(hit), "promoted"
        if pending:
            return _recur_silently()  # never propose on an unreviewed base
        norm = _normalize_content(content)
        prop = hit.get("proposed_revision")
        if prop and prop.get("content") != norm:
            # A DIFFERENT Suggested Update is already awaiting review on this entry —
            # never clobber it (it would vanish unreviewed). Leave it untouched and
            # surface the new phrasing to the developer instead of silently dropping it.
            return hit["id"], norm, "revision_already_pending"
        if not prop:
            hit["proposed_revision"] = _build_proposal(
                hit, content, subtype, session_id, now, source="human")
            _save(repo_path, data)
        # No .pending_review flag here for the same reason as new captures: the
        # in-band revision_proposed ack already notifies the developer.
        return hit["id"], norm, "revision_proposed"

    if pending_twin and not deictic:
        # Terse clean restatement is the activation gesture: bless revision 1 in place,
        # keeping the fuller stored content (approve_decision precedent).
        cur = _current_revision(hit)
        hit["status"] = "approved"
        hit["approved_at"] = now
        hit["approved_by"] = "human"
        _record_recurrence(hit, session_id)
        if cur is not None:
            cur["approved_at"] = now
            score, factors = _compute_confidence(hit)
            cur["confidence_score"] = score
            cur["evidence"] = factors
        _sync_decision_cache(hit)
        _save(repo_path, data)
        return hit["id"], _current_content(hit), "promoted"
    return _recur_silently()


def constraint_ack(content: str, status: str, entry_id: str = "",
                   near_misses: list | tuple = ()) -> str:
    """Single-sourced ack text for capture_user_constraint, shared by every adapter (Claude,
    Codex via claude.capture_constraint, Gemini, the MCP tool) so wording — and the
    self-approval-proofing on the pending and revision_proposed cases — can never drift
    between hosts. `entry_id` names the touched entry (used by revision_proposed);
    `near_misses` (from capture_user_constraint's out-list) appends a consolidation hint
    for grey-zone lexical matches on a brand-new entry."""
    near_note = ""
    if near_misses:
        near_note = (
            " Possibly related stored rules (lexical near-matches, NOT merged): "
            + "; ".join(near_misses)
            + ". If any of these states the same rule, point it out and offer to "
            "consolidate — only proceed after the developer explicitly confirms; never "
            "merge on your own."
        )
    if status == "pending_approval":
        return (
            f"Stored pending your review: '{content}' — it references something only this "
            "conversation understands (this/it/here), so it is not yet trusted. Briefly tell "
            "the developer it was stored pending review. Do NOT approve it yourself; only the "
            "developer decides (they can run `contexer review`, or ask you to show it via "
            "review_pending)." + near_note
        )
    if status == "promoted":
        return (
            f"Your restated rule replaced the pending one — '{content}' is now active. "
            "Acknowledge this briefly to the user."
        )
    if status == "revision_proposed":
        return (
            f"Recorded a suggested update to existing rule {entry_id[:8]}: '{content}'. "
            "The current rule stays active until it is reviewed. Briefly tell the developer "
            "a suggested update to that rule is pending their review. Do NOT approve it "
            "yourself; only the developer decides (they can run `contexer review`, or ask "
            "you to show it via review_pending)."
        )
    if status == "revision_already_pending":
        return (
            f"Rule {entry_id[:8]} already has a suggested update awaiting review; this new "
            f"phrasing ('{content}') was NOT stored, to avoid clobbering it. Tell the developer "
            "a suggested update is already pending for that rule — they can run `contexer "
            "review` — and mention this new phrasing so they can fold it in if relevant."
        )
    return (
        f"Auto-stored as constraint: '{content}'. "
        "Acknowledge this briefly to the user — e.g. 'Stored as a constraint in Contexer.'"
        + near_note
    )


def _redaction_enabled() -> bool:
    """Whether outbound secret redaction is on. Default True (safety holds unconfigured); opt
    out with redact_secrets=false in config.toml. Fail-soft: any config error keeps redaction ON.
    Governs the EGRESS path only (share projection + wire) — capture is deliberately NOT scrubbed
    so the local store stays a faithful record; redaction happens when a decision LEAVES."""
    try:
        from contexer.config import load_profile
        return load_profile().redact_secrets
    except Exception:
        return True


def _normalize_content(content: str) -> str:
    """Strip whitespace, collapse internal runs, capitalize first character."""
    normalized = " ".join(content.split())
    return normalized[:1].upper() + normalized[1:] if normalized else normalized


# ── Decision / Revision model (Git-like: revisions are immutable commits, the decision
# ── carries an explicit current_revision_id pointer = HEAD). Storage preserves every
# ── revision; replay exposes only the current one. The decision-level content / revision /
# ── confidence / confidence_factors fields are a synced HEAD-cache of the current revision,
# ── kept so the many read sites stay simple and replay is O(1). The revisions are the
# ── source of truth; the cache is always rewritten from them on any change.

def _new_revision(decision_id: str, version_number: int, content: str, source: str,
                  confidence_score: int = 0, evidence: list | None = None,
                  approved_at: str | None = None, created_at: str | None = None,
                  normalize: bool = True, title: str = "") -> dict:
    """Build one immutable revision object. `source` is the provenance
    (ai | human | scan | bootstrap | memory) and maps to the upstream push contract.

    normalize=False preserves content byte-for-byte - used by migration, which must be
    lossless and must never rewrite (e.g. re-capitalize) an existing stored value."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "revision_id": str(uuid.uuid4()),
        "decision_id": decision_id,
        "version_number": version_number,
        "content": _normalize_content(content) if normalize else content,
        "title": title,
        "confidence_score": confidence_score,
        "evidence": list(evidence or []),
        "created_at": created_at or now,
        "approved_at": approved_at,
        "source": source,
    }


def _current_revision(entry: dict) -> dict | None:
    """The active revision an entry points at via current_revision_id; falls back to the
    last revision, then None. This is the only revision replay ever exposes."""
    revs = entry.get("revisions") or []
    cid = entry.get("current_revision_id")
    if cid:
        for r in revs:
            if r.get("revision_id") == cid:
                return r
    return revs[-1] if revs else None


def _current_content(entry: dict) -> str:
    """Content of the entry's current revision (the only value replay should inject)."""
    rev = _current_revision(entry)
    if rev is not None:
        return rev.get("content", "")
    return entry.get("content", "")


def _sync_decision_cache(entry: dict) -> None:
    """Mirror the current revision onto the decision-level HEAD-cache fields so the read
    sites (get_context display, replay) stay O(1). Revisions remain the source of truth."""
    rev = _current_revision(entry)
    if rev is None:
        return
    entry["content"] = rev.get("content", "")
    entry["title"] = rev.get("title") or _derive_title(rev.get("content", ""))
    entry["revision"] = rev.get("version_number", 1)
    entry["confidence"] = rev.get("confidence_score", entry.get("confidence", 0))
    evidence = rev.get("evidence") or []
    if evidence:
        entry["confidence_factors"] = evidence
    else:
        entry.pop("confidence_factors", None)


def _append_revision(entry: dict, content: str, source: str,
                     approved_at: str | None = None, title: str = "") -> dict:
    """Create the next revision for a decision, make it current, and resync the cache.
    Confidence is computed from the decision's aggregate evidence at this moment and
    snapshotted onto the revision. `title` wins when given; otherwise it is re-derived
    from `content` via `_normalize_title`/`_derive_title`. Returns the new revision."""
    revs = entry.setdefault("revisions", [])
    next_version = (revs[-1]["version_number"] + 1) if revs else 1
    score, factors = _compute_confidence(entry)
    effective_title = _normalize_title(title) or _derive_title(content)
    rev = _new_revision(
        entry.get("id", ""), next_version, content,
        source=source, confidence_score=score, evidence=factors,
        approved_at=approved_at, title=effective_title,
    )
    revs.append(rev)
    entry["current_revision_id"] = rev["revision_id"]
    entry["updated_at"] = rev["created_at"]
    _sync_decision_cache(entry)
    return rev


def _backfill_titles(entry: dict) -> bool:
    """Set a derived `title` on any revision that lacks one, then re-sync the HEAD cache.
    Idempotent; returns True if anything changed."""
    changed = False
    for rev in entry.get("revisions", []):
        if not rev.get("title"):
            rev["title"] = _derive_title(rev.get("content", ""))
            changed = True
    if changed or not entry.get("title"):
        _sync_decision_cache(entry)
        changed = True
    return changed


def _migrate_decision(entry: dict) -> bool:
    """Transparently upgrade a legacy decision entry to the revision model. Idempotent.
    Builds full revision objects from the legacy historical snapshots (`revisions[]`, which
    held only prior versions) plus the current head (legacy `content`/`revision`), and sets
    `current_revision_id`. Returns True if the entry was changed."""
    if entry.get("type") != "decision":
        return False
    revs = entry.get("revisions")
    already = (
        entry.get("current_revision_id")
        and isinstance(revs, list) and revs
        and all(isinstance(r, dict) and "revision_id" in r for r in revs)
    )
    if already:
        # Heal a divergent HEAD-cache: if the current revision's content drifted from the
        # decision-level content cache (e.g. an earlier buggy migration that re-capitalized
        # the revision), restore the revision to the cached value - that is the value replay
        # has always shown, so it is the original-of-record. No-op when consistent.
        healed = False
        cur = _current_revision(entry)
        cached = entry.get("content")
        if cur is not None and cached is not None and cur.get("content") != cached:
            cur["content"] = cached
            healed = True
        backfilled = _backfill_titles(entry)
        return healed or backfilled

    did = entry.get("id", "")
    created_by = entry.get("created_by", "ai")
    legacy = revs if isinstance(revs, list) else []
    full: list[dict] = []
    for snap in legacy:
        if isinstance(snap, dict) and "revision_id" in snap:
            full.append(snap)
            continue
        full.append(_new_revision(
            did, snap.get("revision", len(full) + 1), snap.get("content", ""),
            source=snap.get("source") or created_by,
            confidence_score=snap.get("confidence", 0),
            evidence=snap.get("evidence") or snap.get("confidence_factors") or [],
            created_at=snap.get("timestamp") or entry.get("timestamp", ""),
            approved_at=snap.get("replaced_at"),
            normalize=False,  # migration is lossless - preserve stored content verbatim
        ))
    current_version = entry.get("revision", (full[-1]["version_number"] + 1) if full else 1)
    if not any(r.get("version_number") == current_version for r in full):
        full.append(_new_revision(
            did, current_version, entry.get("content", ""),
            source=created_by,
            confidence_score=entry.get("confidence", 0),
            evidence=entry.get("confidence_factors") or [],
            created_at=entry.get("updated_at") or entry.get("timestamp", ""),
            approved_at=entry.get("approved_at"),
            normalize=False,  # migration is lossless - preserve stored content verbatim
        ))
    current = next((r for r in full if r.get("version_number") == current_version), full[-1])
    entry["revisions"] = full
    entry["current_revision_id"] = current["revision_id"]
    entry["revision"] = current["version_number"]
    _backfill_titles(entry)
    return True


def _migrate_entries(data: dict) -> None:
    """Migrate every decision entry in a loaded store to the revision model, in place.

    Stamped with `schema_version` so an already-migrated store short-circuits on the next
    load instead of re-scanning every entry on every read/write (the hot path). The stamp
    is set in memory here and persisted by the next _save."""
    if data.get("schema_version") == _SCHEMA_VERSION:
        return
    for entry in data.get("entries", []):
        _migrate_decision(entry)
    data["schema_version"] = _SCHEMA_VERSION


def _new_decision_entry(content: str, session_id: str, subtype: str,
                        memory_key: str | None = None,
                        created_by: str = "ai",
                        status: str = "",
                        title: str = "") -> dict:
    """Build a decision entry with its first revision. Single source of truth for the
    entry schema - both manual capture (`update_decision`) and memory import use this."""
    content = _normalize_content(content)
    effective_title = _normalize_title(title) or _derive_title(content)
    if not status:
        level = _classify_level(content, subtype, created_by)
        status = _level_to_status(level)
    now = datetime.now(timezone.utc).isoformat()
    decision_id = str(uuid.uuid4())
    entry: dict = {
        "id": decision_id,
        "type": "decision",
        "subtype": subtype,
        "content": content,         # HEAD-cache of the current revision (see _sync_decision_cache)
        "title": effective_title,   # HEAD-cache of the current revision title
        "session_id": session_id,
        "session_ids": [session_id],
        "timestamp": now,           # Created At - immutable
        "updated_at": now,          # Updated At - bumped on each revision
        "revision": 1,              # current version_number (cache); revisions[] is canonical
        "current_revision_id": None,
        "occurrence_count": 1,
        "status": status,
        "created_by": created_by,
    }
    if memory_key is not None:
        entry["memory_key"] = memory_key
    # First revision. approved_at is set only when the decision is born trusted (not pending).
    approved_at = now if status in ("approved", "suggested") else None
    score, factors = _compute_confidence(entry)
    rev = _new_revision(decision_id, 1, content, source=created_by,
                        confidence_score=score, evidence=factors,
                        approved_at=approved_at, created_at=now,
                        title=effective_title)
    entry["revisions"] = [rev]
    entry["current_revision_id"] = rev["revision_id"]
    _sync_decision_cache(entry)
    return entry


def _build_proposal(target: dict, content: str, subtype: str, session_id: str, now: str,
                    source: str = "ai", title: str = "") -> dict:
    """A Suggested Update (pending revision) attached to a live decision: the detected new
    value, its confidence/evidence, and provenance. The live decision is NOT modified - this
    proposal waits for developer approval, at which point it is promoted to a new revision."""
    sessions = sorted({s for s in (*(target.get("session_ids") or []), session_id) if s})
    score, factors = _compute_confidence({
        "created_by": "ai",
        "occurrence_count": target.get("occurrence_count", 1),
        "session_ids": sessions,
        "memory_key": target.get("memory_key"),
    })
    normalized_content = _normalize_content(content)
    proposal = {
        "content": normalized_content,
        "subtype": subtype or target.get("subtype", ""),
        "session_id": session_id,
        "source": source,
        "created_at": now,
        "confidence": score,
        "confidence_factors": factors,
    }
    proposal["title"] = _normalize_title(title) or _derive_title(normalized_content)
    return proposal


def _promote_proposal(entry: dict, content: str | None = None) -> None:
    """Approve a pending proposed_revision: append it as a new immutable revision and move
    current_revision_id forward. Prior revisions are preserved (never overwritten). `content`
    (an edited value) overrides the proposal's content when given. The proposal's title carries
    forward only when the promoted content matches the proposal's content unchanged; if an
    edit at approval time changed the content, the title is dropped so _append_revision
    re-derives it from the final content instead of carrying a stale one."""
    prop = entry.get("proposed_revision") or {}
    if prop.get("subtype"):
        entry["subtype"] = prop["subtype"]
    # Merge the proposing session FIRST so the new revision's confidence reflects the
    # session that drove the change (not only the original creation sessions).
    prop_session = prop.get("session_id", "")
    if prop_session:
        sessions = set(entry.get("session_ids") or [])
        sessions.add(prop_session)
        entry["session_ids"] = sorted(sessions)
        entry["occurrence_count"] = entry.get("occurrence_count", 1) + 1
    prop_content = prop.get("content", _current_content(entry))
    new_content = content if content else prop_content
    now = datetime.now(timezone.utc).isoformat()
    carried_title = prop.get("title", "") if new_content == prop_content else ""
    _append_revision(entry, new_content, source=prop.get("source", "human"), approved_at=now,
                     title=carried_title)
    entry.pop("proposed_revision", None)


_PENDING_REVIEW_NUDGE = (
    "Contexer: decision(s) are pending your review. At a natural pause, offer to show them "
    "(call review_pending) and approve via approve_decision (entry_id=all clears the shown "
    "set); they stay inactive until approved."
)


def _pending_review_flag(repo_path: str) -> Path:
    """Per-repo flag path — a pending decision in repo A must never nudge a session in repo B."""
    return STORE_DIR / f".pending_review_{_slug(repo_path)}"


def _offer_flag(repo_path: str) -> Path:
    return STORE_DIR / f".bootstrap_offered_{_slug(repo_path)}"


def _arm_offer(repo_path: str) -> None:
    """Record that the setup offer has gone out for this repo in this session. Fail-soft:
    a flag-write error must degrade to the old always-offer behaviour, never raise."""
    try:
        STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        _offer_flag(repo_path).touch()
    except OSError:
        pass


def _offer_already_made(repo_path: str) -> bool:
    """True once the offer has been emitted for this repo in this session.

    The offer instructs the model to treat a dismissed picker as skip and never re-ask, but
    that promise could not hold: skipping stores no decision, so `if decisions` never trips
    and the whole block was rebuilt on the next UserPromptSubmit and after every /compact —
    re-summoning, since the picker landed, a blocking modal instead of a re-printed menu.
    A non-resume, non-compact session start clears the flag, so a genuinely new session
    still offers exactly once; `compact` deliberately does NOT clear it, because compaction
    continues the same session in which the developer already answered."""
    try:
        return _offer_flag(repo_path).exists()
    except OSError:
        return False


def _touch_pending_review(repo_path: str) -> None:
    """Drop the per-repo .pending_review flag — the next-prompt consumer (pending_review_nudge)
    reads it to nudge the developer to review pending decisions mid-session. Fail-soft: a
    flag-write error must never break capture."""
    try:
        STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        _pending_review_flag(repo_path).touch()
    except OSError:
        pass


def pending_review_nudge(repo_path: str) -> str | None:
    """Per-prompt consumer for the pending-review flag. Returns the one-time nudge text (and
    clears the flag) ONLY when THIS repo has a freshly-set flag AND still has decisions awaiting
    review — so a flag left by an already-approved decision, or by a different repo, never
    produces a false nudge. Returns None otherwise. Fail-soft (never raises)."""
    try:
        repo = _resolve_repo(repo_path)
        if not repo:
            return None
        flag = _pending_review_flag(repo)
        if not flag.exists():
            return None
        flag.unlink(missing_ok=True)  # fire once; a new pending decision re-arms it
        return _PENDING_REVIEW_NUDGE if get_pending_decisions(repo) else None
    except OSError:
        return None


_MAX_SOURCE_FILES = 10
_STALENESS_MAX_CHECKS = 3  # git calls per render; anchored entries beyond this render bare
_GIT_FAST_TIMEOUT = 2      # injection/capture paths must never stall on a slow git


def _anchor_sources(repo_path: str, entry: dict, source_files) -> None:
    """Anchor a NEW entry to the files it describes plus the repo's current HEAD, so a later
    injection can flag it as possibly stale (see _staleness_note). No-op when no usable file
    is given. Fail-soft: an unresolvable HEAD stores an empty anchor, never blocks capture."""
    files = [f for f in (source_files or []) if isinstance(f, str) and f.strip()][:_MAX_SOURCE_FILES]
    if not files:
        return
    entry["source_files"] = files
    entry["anchor_commit"] = _git(repo_path, "rev-parse", "HEAD", timeout=_GIT_FAST_TIMEOUT) or ""


def _staleness_note(repo_path: str, entry: dict) -> str:
    """`""` unless the entry is anchored (source_files + anchor_commit) AND git reports at
    least one of those files changed between the anchor and HEAD. Fail-soft: an unknown
    commit, a non-git repo, or a timeout all render no note (see _git). Never raises."""
    files = [f for f in (entry.get("source_files") or []) if isinstance(f, str)]
    anchor = entry.get("anchor_commit") or ""
    if not files or not anchor:
        return ""
    out = _git(repo_path, "diff", "--name-only", f"{anchor}..HEAD", "--", *files,
               timeout=_GIT_FAST_TIMEOUT)
    changed = out.splitlines() if out else []
    if not changed:
        return ""
    extra = f", +{len(changed) - 1} more" if len(changed) > 1 else ""
    return f" [may be stale: {changed[0]} changed since capture{extra}]"


def _staleness_notes(repo_path: str, entries: list) -> dict:
    """id -> staleness note for the given rendered entries. Perf guard: at most
    _STALENESS_MAX_CHECKS git checks per render call — anchored entries past that budget
    render without a note rather than adding subprocess latency to an injection.

    `repo_path` is used as given — the same already-resolved path the caller loaded the
    entries from, so the git check can never target a different repo than the store read."""
    notes, checked = {}, 0
    for e in entries:
        if checked >= _STALENESS_MAX_CHECKS:
            break
        if not (e.get("source_files") and e.get("anchor_commit")):
            continue
        checked += 1
        note = _staleness_note(repo_path, e)
        if note:
            notes[e.get("id")] = note
    return notes


def update_decision(repo_path: str, content: str, session_id: str, subtype: str = "",
                    created_by: str = "ai", replace_id: str = "", title: str = "", *,
                    source_files: list | None = None) -> tuple[bool, str | None]:
    """Store (or route) one decision. `source_files` anchors a NEWLY CREATED entry to the
    repo-relative files it describes plus the current git HEAD, so later injections can flag
    it as possibly stale; capped at _MAX_SOURCE_FILES. Recurrences, containment routes and
    `replace_id` corrections never gain or overwrite an anchor — only entry creation anchors."""
    content = _normalize_content(content)
    with _store_lock(_slug(repo_path)):
        data = _load(repo_path)
        # Explicit correction: caller knows which entry is wrong and wants to change it.
        # Runs before _is_storable — an explicit correction always writes.
        # Accepts both full UUIDs and the 8-char short IDs shown in get_context output.
        if replace_id:
            target = next(
                (e for e in data["entries"] if e.get("id", "").startswith(replace_id)),
                None,
            )
            if target is not None:
                # Fix: validate content before any mutation - replace_id bypasses the
                # downstream _is_storable check, so guard here to prevent blank content
                # from wiping a trusted decision.
                if not _is_storable(content):
                    return False, None
                # No-op guard - identical content creates no revision. A title-only correction
                # (same content, new title) is still handled, but must respect the SAME approval
                # gate as any change: the title renders as a trusted leading heading, so an AI
                # retitling a trusted architecture/constraint decision could reframe it as trusted
                # context - that goes through review, never applied in place.
                if content == target.get("content", ""):
                    new_title = _normalize_title(title)
                    if not new_title or new_title == target.get("title", ""):
                        return True, target["id"]  # nothing meaningful changed
                    now = datetime.now(timezone.utc).isoformat()
                    st = target.get("subtype", "")
                    gated = (_update_needs_approval(subtype or st, created_by)
                             or _update_needs_approval(st, created_by))
                    # A gated title change to an ALREADY-TRUSTED (approved/suggested) decision
                    # must be reviewed - the title renders as trusted, injected context. A pending
                    # (untrusted) decision is NOT injected, so its title is corrected in place like
                    # the non-gated case; the developer still reviews the base with the fixed title.
                    if gated and _entry_status(target) != "pending_approval":
                        # Attach a Suggested Update carrying the new title (content unchanged) for
                        # review. Dedup on content AND title so a corrected-title retry rebuilds it.
                        existing_prop = target.get("proposed_revision")
                        if (existing_prop and existing_prop.get("content", "") == content
                                and existing_prop.get("title", "") == new_title):
                            return True, target["id"]  # identical title proposal already pending
                        target["proposed_revision"] = _build_proposal(
                            target, content, subtype, session_id, now, title=title)
                        _save(repo_path, data)
                        _touch_pending_review(repo_path)
                        return True, target["id"]
                    # Non-gated (human/scan/bootstrap, or pattern/convention) OR pending/untrusted:
                    # correct the current revision's title in place - no new revision.
                    cur = _current_revision(target)
                    if cur is not None:
                        cur["title"] = new_title
                        target["updated_at"] = now
                        _sync_decision_cache(target)
                        _save(repo_path, data)
                    return True, target["id"]
                now = datetime.now(timezone.utc).isoformat()
                new_subtype = subtype or target.get("subtype", "")
                old_subtype = target.get("subtype", "")
                # Significant change -> Suggested Update: the live (approved) revision is left
                # untouched and a proposal is attached, awaiting developer approval. The
                # decision is only versioned forward once the developer approves. Significance
                # considers BOTH subtypes - re-categorising a constraint to a convention is
                # still a change to a constraint.
                if (_update_needs_approval(new_subtype, created_by)
                        or _update_needs_approval(old_subtype, created_by)):
                    # Fix: don't attach a proposal to an entry that isn't approved yet -
                    # the developer needs to review the base first.
                    if _entry_status(target) == "pending_approval":
                        return True, target["id"]
                    # Fix: don't overwrite an existing proposal if the new content AND its
                    # effective title are identical - the proposal is already pending for the same
                    # change. A same-content retry with a CHANGED title must rebuild it, or approval
                    # would promote the stale proposal title.
                    existing_prop = target.get("proposed_revision")
                    new_prop_title = _normalize_title(title) or _derive_title(content)
                    if (existing_prop and existing_prop.get("content", "") == content
                            and existing_prop.get("title", "") == new_prop_title):
                        return True, target["id"]
                    target["proposed_revision"] = _build_proposal(
                        target, content, subtype, session_id, now, title=title)
                    _save(repo_path, data)
                    _touch_pending_review(repo_path)  # a Suggested Update now awaits review (after save)
                    return True, target["id"]
                # Trivial change (pattern/convention, or any human/scan/bootstrap change) →
                # apply immediately as a new approved revision. History is preserved: the
                # prior revision stays in revisions[]; current_revision_id moves forward.
                if subtype:
                    target["subtype"] = subtype
                _append_revision(target, content, source=created_by, approved_at=now, title=title)
                _save(repo_path, data)
                return True, target["id"]
            # replace_id not found — fall through to normal storage
        if not _is_storable(content):
            return False, None
        decisions_only = [e for e in data["entries"] if e["type"] == "decision"]
        match = _find_match(content, decisions_only)
        if match is not None:
            _record_recurrence(match, session_id)
            _save(repo_path, data)
            return False, None
        entry = _new_decision_entry(content, session_id, subtype, created_by=created_by, title=title)
        _anchor_sources(repo_path, entry, source_files)
        data["entries"].append(entry)
        data["entries"] = _keep_top(data["entries"], MAX_ENTRIES, pin_last=True)
        _save(repo_path, data)
        if _entry_status(entry) == "pending_approval":
            _touch_pending_review(repo_path)  # a brand-new decision awaits review (after save)
        return True, entry["id"]


def approve_decision(repo_path: str, entry_id: str, action: str,
                     content: str = "") -> tuple[bool, str]:
    """Approve, edit, skip, ignore, or dismiss a decision awaiting the developer — or
    retire an already-trusted one.

    Handles three cases:
      - a Suggested Update (the entry carries a `proposed_revision`): approve/edit promotes
        it to a new revision (history preserved), skip keeps it for later, dismiss/ignore
        discards the proposal and keeps the current revision.
      - a brand-new pending_approval decision: approve/edit trusts it, ignore/dismiss
        suppresses it, skip leaves it pending.
      - an ACTIVE decision (already approved/suggested, no pending proposal): only
        'ignore' is legal — deliberately retiring a trusted rule (e.g. consolidating an
        overlap-report cluster) is a legitimate hygiene act. Full revision history is
        kept; only status flips to 'ignored'. approve/edit/dismiss/skip are rejected —
        an already-approved decision can't be re-approved through this path.

    content: the corrected decision text, required when action='edit'
    Returns (success, message).
    """
    if action not in ("approve", "ignore", "edit", "skip", "dismiss"):
        return False, f"Invalid action '{action}'. Use: approve, edit, skip, ignore, or dismiss."
    if action == "edit" and not content.strip():
        return False, "Action 'edit' requires content — provide the corrected decision text."

    with _store_lock(_slug(repo_path)):
        data = _load(repo_path)
        ok, msg, changed = _apply_approval(
            data, entry_id, action, content, datetime.now(timezone.utc).isoformat())
        if changed:
            _save(repo_path, data)
        return ok, msg


def _apply_approval(data: dict, entry_id: str, action: str, content: str,
                    now: str) -> tuple[bool, str, bool]:
    """Apply ONE approval action to `data` in memory — no lock, no load, no save. Returns
    (success, message, changed); `changed` lets the caller save only when something mutated, and
    lets `approve_decisions` batch many actions into a single load+save. Resolves an exact id
    first, then an 8-char prefix (consistent with replace_id / get_shareable)."""
    entry = next((e for e in data["entries"] if e.get("id") == entry_id), None)
    if entry is None and entry_id:
        entry = next((e for e in data["entries"] if e.get("id", "").startswith(entry_id)), None)
    if entry is None:
        return False, f"Decision {entry_id!r} not found.", False

    # Suggested Update flow: the live decision stays trusted; we act on the proposal.
    if entry.get("proposed_revision"):
        if action == "skip":
            return True, "Skipped - the suggested update is kept for later review.", False
        if action in ("dismiss", "ignore"):
            rev = entry.get("revision", 1)
            entry.pop("proposed_revision", None)
            return True, f"Dismissed - kept current revision {rev}.", True
        # approve or edit → promote the proposal to a new revision (history preserved).
        # Set the approval fields FIRST so the new revision's snapshotted confidence
        # reflects the developer approval; _promote_proposal computes + syncs the cache.
        entry["status"] = "approved"
        entry["approved_at"] = now
        entry["approved_by"] = "human"
        _promote_proposal(entry, content if action == "edit" else None)
        stored = _current_content(entry)
        preview = stored[:80] + ("..." if len(stored) > 80 else "")
        verb = "Updated and approved" if action == "edit" else "Approved"
        return True, f"{verb}. Now revision {entry['revision']}: \"{preview}\"", True

    # No proposed_revision: a plain decision entry, gated on its own status.
    status = _entry_status(entry)

    # ACTIVE (already trusted) decision: 'ignore' is the one legal action — deliberately
    # retiring a trusted rule (e.g. consolidating an overlap-report cluster) is a legitimate
    # hygiene act, and it keeps full revision history, just flips status. Every other action
    # (approve/edit/dismiss/skip) stays pending-only: no re-approving an already-approved
    # decision, and no repurposing 'dismiss' (which means "discard a proposal") here.
    if status in ("approved", "suggested"):
        if action == "ignore":
            entry["status"] = "ignored"
            return True, "Ignored. This trusted decision is retired and will not surface again.", True
        return False, (
            f"Decision is already {status} — only 'ignore' acts on an active decision "
            "(to retire it). 'approve', 'edit', 'dismiss', and 'skip' are pending-only."
        ), False

    if status == "ignored":
        return False, "Decision is already ignored — nothing to do.", False

    # pending_approval flow. The decision already has revision 1; approval blesses it in
    # place (no new revision - there is no prior version to preserve yet).
    if action == "skip":
        return True, "Skipped.", False
    if action in ("ignore", "dismiss"):
        entry["status"] = "ignored"
        return True, "Ignored. This decision will not surface again.", True

    cur = _current_revision(entry)
    if action == "edit" and cur is not None:
        cur["content"] = _normalize_content(content)
    entry["status"] = "approved"
    entry["approved_at"] = now
    entry["approved_by"] = "human"
    if cur is not None:
        cur["approved_at"] = now
        score, factors = _compute_confidence(entry)
        cur["confidence_score"] = score
        cur["evidence"] = factors
    _sync_decision_cache(entry)
    stored_content = _current_content(entry)
    preview = stored_content[:80] + ("..." if len(stored_content) > 80 else "")
    verb = "Updated and approved" if action == "edit" else "Approved"
    return True, f"{verb}. This decision is now trusted knowledge: \"{preview}\"", True


def approve_decisions(repo_path: str, entry_ids: list, action: str,
                      content: str = "") -> list[tuple[str, bool, str]]:
    """Apply `action` to several decisions in ONE store transaction — load once, save once —
    so a bulk clear is atomic and O(1) writes, not one whole-file rewrite per id. Returns
    [(entry_id, success, message), ...] so the caller reports accurate per-id results (a stale or
    invalid id fails without faking success). 'edit' is single-only (it needs per-decision
    content) and is rejected here."""
    if action not in ("approve", "ignore", "skip", "dismiss"):
        return [(i, False, f"Bulk action {action!r} not supported (edit is single-only).")
                for i in entry_ids]
    results: list[tuple[str, bool, str]] = []
    with _store_lock(_slug(repo_path)):
        data = _load(repo_path)
        now = datetime.now(timezone.utc).isoformat()
        changed_any = False
        for eid in entry_ids:
            ok, msg, changed = _apply_approval(data, eid, action, content, now)
            changed_any = changed_any or changed
            results.append((eid, ok, msg))
        if changed_any:
            _save(repo_path, data)
    return results


def get_pending_decisions(repo_path: str) -> list[dict]:
    """Returns all decisions awaiting the developer: brand-new pending_approval entries
    AND live decisions carrying a Suggested Update (proposed_revision)."""
    data = _load(repo_path)
    return [
        e for e in data.get("entries", [])
        if e.get("type") == "decision"
        and (_entry_status(e) == "pending_approval" or e.get("proposed_revision"))
    ]


def format_pending_review(repo_path: str) -> str:
    """Render every decision awaiting the developer as an identified list — id + subtype +
    content + the action to take — for the in-session `review_pending` tool (the conversational
    twin of the `contexer review` terminal command). Content IS shown here: this is the
    on-demand surface, pulled only when the developer asks to review, so it is where the detail
    belongs (unlike the deliberately terse SessionStart count)."""
    pending = get_pending_decisions(repo_path)
    if not pending:
        return "Nothing pending review."
    total = len(pending)
    shown = pending[:_FILTERED_DISPLAY]  # cap like get_context, so a big backlog can't flood context
    header = f"{_pl(total, 'decision')} pending your review"
    if total > len(shown):
        header += f" — showing {len(shown)} of {total}; run `contexer review` for the rest"
    lines = [header + ":\n"]
    for d in shown:
        eid = (d.get("id") or "")[:8]
        st = d.get("subtype") or "decision"
        prop = d.get("proposed_revision")
        if prop:
            lines.append(f"- {eid} [{st}] update")
            lines.append(f'    current:  "{_current_content(d)}"')
            lines.append(f'    detected: "{prop.get("content", "")}"')
            lines.append(f'    approve_decision(entry_id="{eid}", action="approve|edit|skip|dismiss")')
        else:
            title, body = _title_and_body(d)
            lines.append(f'- {eid} [{st}] {title}')
            if body is not None:
                lines.append(f'    "{body}"')
            lines.append(f'    approve_decision(entry_id="{eid}", action="approve|edit|ignore")')
    lines.append("\nReview each with the developer before approving. To clear several at once, "
                 'pass comma-separated ids — or approve_decision(entry_id="all", action="approve") '
                 "for the whole list.")
    return "\n".join(lines)


# Overlap-report thresholds. Pairwise is deliberately looser than the 0.7 novelty
# bar: the report surfaces rules the dedup filter let coexist. Containment catches a
# short rule swallowed by a longer restatement (|∩|/min), which the max-based
# pairwise ratio structurally under-scores.
_REPORT_PAIRWISE = 0.35
_REPORT_CONTAINMENT = 0.7


def overlap_report(repo_path: str) -> list[list[dict]]:
    """Clusters of active constraint/convention decisions whose contents overlap enough
    to plausibly say the same thing — surfaced for MANUAL consolidation. Pure read:
    never merges, never deletes, never writes. Fail-soft: returns [] on any error.

    Two rules are linked when token overlap (_overlap_ratio) >= _REPORT_PAIRWISE or
    containment |∩|/min >= _REPORT_CONTAINMENT; clusters are the transitive closure
    (union-find), so A~C plus B~C groups all three even if A and B miss directly.
    Only clusters of size >= 2 are returned."""
    try:
        entries = [
            e for e in _load(repo_path).get("entries", [])
            if e.get("type") == "decision"
            and e.get("subtype") in ("constraint", "convention")
            and _entry_status(e) in ("approved", "suggested", "pending_approval")
        ]
        toks = [_tokenize(_current_content(e)) for e in entries]
        n = len(entries)

        parent = list(range(n))

        def _find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        for i in range(n):
            if not toks[i]:
                continue
            for j in range(i + 1, n):
                if not toks[j]:
                    continue
                inter = len(toks[i] & toks[j])
                lo = min(len(toks[i]), len(toks[j]))
                if (_overlap_ratio(toks[i], toks[j]) >= _REPORT_PAIRWISE
                        or inter / lo >= _REPORT_CONTAINMENT):
                    parent[_find(i)] = _find(j)

        groups: dict[int, list[int]] = {}
        for i in range(n):
            groups.setdefault(_find(i), []).append(i)

        return [
            [{"id": (entries[i].get("id") or "")[:8],
              "subtype": entries[i].get("subtype", ""),
              "status": _entry_status(entries[i]),
              "content": _current_content(entries[i])} for i in members]
            for members in groups.values() if len(members) >= 2
        ]
    except Exception:
        return []


def _share_projection(entry: dict, redact_on: bool | None = None) -> dict:
    """Project a decision entry onto the push wire shape {id, type, title, content, confidence,
    evidence, source}: `type` is the decision subtype; `evidence` is None when empty so
    the push omits it. `redact_on` lets a batch caller (or a preview with an explicit profile)
    resolve the redaction flag ONCE and pass it in; None means resolve it here."""
    if redact_on is None:
        redact_on = _redaction_enabled()
    rev = _current_revision(entry) or {}
    content = _current_content(entry)
    # Derive the title from the PRE-scrub content so a fallback-derived title matches the
    # stored one (entry.get("title") is normally already populated by _sync_decision_cache;
    # this is the safety net for an entry that predates the title HEAD-cache). A derived
    # title inherits content's secrets, so it gets scrubbed independently below — same
    # security requirement as content/evidence, not polish.
    title = entry.get("title") or _derive_title(content)
    evidence = rev.get("evidence") or None
    # Redact at the projection so the confirm-preview and durable outbox show exactly what
    # the wire will send (a legacy on-disk secret shows redacted, not a false raw value).
    # `redacted` counts scrubbed secrets for the preview banner; extra key ignored by the
    # wire/outbox builders (they read named fields), and re-scrubbed idempotently at _wire_args.
    redacted = 0
    if redact_on:
        content, redacted = redact.scrub(content)
        title, n_title = redact.scrub(title)
        redacted += n_title
        if evidence:
            scrubbed = []
            for e in evidence:
                se, ne = redact.scrub(e)
                scrubbed.append(se)
                redacted += ne
            evidence = scrubbed
    return {
        "id": entry.get("id", ""),
        "type": entry.get("subtype", "") or "convention",
        "title": title,
        "content": content,
        "confidence": rev.get("confidence_score"),
        "evidence": evidence,
        "source": rev.get("source"),
        "redacted": redacted,
        # Review state (approved | suggested | pending_approval), surfaced so a share preview
        # can show it and the developer doesn't push a not-yet-reviewed decision by accident.
        # Extra key like `redacted`: the wire builders read named fields, so it never egresses.
        "status": _entry_status(entry),
    }


def _shareable_entries(repo_path: str) -> list[dict]:
    return [e for e in _load(repo_path).get("entries", [])
            if e.get("type") == "decision" and _entry_status(e) != "ignored"]


def get_shareable(repo_path: str, decision_id: str = "",
                  redact_on: bool | None = None) -> dict | None:
    """Return a decision's current shareable fields, or None (C4).

    Resolves `decision_id` (full UUID or 8-char prefix) or, when omitted, the most
    recently updated decision. Ignored decisions are excluded. `redact_on` threads a
    caller-resolved redaction flag (e.g. a preview's explicit profile); None resolves it."""
    if redact_on is None:
        redact_on = _redaction_enabled()
    decisions = _shareable_entries(repo_path)
    if not decisions:
        return None
    if decision_id:
        entry = next((e for e in decisions if e.get("id", "").startswith(decision_id)), None)
    else:
        entry = max(decisions, key=lambda e: e.get("updated_at") or e.get("timestamp", ""))
    if entry is None:
        return None
    return _share_projection(entry, redact_on)


def get_shareable_all(repo_path: str, redact_on: bool | None = None) -> list[dict]:
    """Every non-ignored decision as a push wire projection, oldest first (C4 --all).

    Ordered by creation timestamp so a bulk share pushes decisions in the order they
    were made - the server's updatedSince consumers then see them chronologically.
    The redaction flag is resolved ONCE for the whole batch (not per decision)."""
    if redact_on is None:
        redact_on = _redaction_enabled()
    decisions = sorted(_shareable_entries(repo_path), key=lambda e: e.get("timestamp", ""))
    return [_share_projection(e, redact_on) for e in decisions]


# A personal-cloud push is OUTWARD — the single source of this caution, shared by the MCP
# preview (format_share_preview) and the CLI previews (cli._pick_shareable/_confirm_share) so
# the wording can't drift between the surfaces. Egress IS scrubbed by `redact` first, but the
# scrubber only catches known secret shapes — the developer is the last line of defence.
_SHARE_SECRETS_HINT = "Don't share credentials, API keys, or other secrets"


def _share_item_line(proj: dict, maxlen: int = 0) -> str:
    """One '<id8> [type] title' preview line for a share projection, with the content on an
    indented quoted line below — skipped when it only repeats the title (same dedup rule as
    _title_and_body, including its COLLAPSED-whitespace comparison — a title stays a single
    stripped line while content may carry newlines/runs of spaces, so comparing raw strings
    would show a spurious body line even when the two are the same text), so the preview
    matches exactly what the wire will send. Content truncated to `maxlen` (0 = full). Shared
    by the MCP and CLI push previews so both render identically."""
    full_content = proj.get("content", "")
    title = proj.get("title") or ""
    content = full_content
    if maxlen and len(content) > maxlen:
        content = content[:maxlen] + "…"
    head = f'  {(proj.get("id") or "")[:8]} [{proj.get("type") or "decision"}]'
    if not title:
        return f'{head} "{content}"'
    collapsed = " ".join(full_content.split())
    if title == collapsed:
        return f'{head} {title}'
    return f'{head} {title}\n      "{content}"'


_SHARE_BLOCK_LABEL_WIDTH = 7  # "title: " / "type:  " / "desc:  " / "id:    " all pad to this
_SHARE_BLOCK_MIN_TEXT = 24    # floor for the text column, so a narrow `width` still renders
# Rows per page in the interactive `contexer share` picker. Deliberately its own constant, NOT
# _FILTERED_DISPLAY: that one is a TOKEN BUDGET for agent-facing context (get_context /
# review_pending), while this is how much a human wants to read before choosing. They happened
# to share the value 25; tying them together would let a context-budget tune silently resize
# the picker. Each item renders as a multi-line block, so 10 keeps a page inside one screen.
_SHARE_PAGE = 10


def _share_item_block(proj: dict, index: int | None = None, width: int = 76, *,
                      shared: bool = False) -> str:
    """Labelled, multi-line preview block for the HUMAN terminal share surfaces (the
    `contexer share` picker and push-confirm) — one field per line:

        1. id:    c609aa4c
           type:  architecture
           title: Stack: all-TypeScript pnpm monorepo
           desc:  Stack: all-TypeScript pnpm monorepo, Node >=24, Postgres 16 +
                  Drizzle ORM, Vitest…

    `index` prefixes a `N. ` numbering column (picker); omit it for an unnumbered block
    (push-confirm). `desc:` wraps via `textwrap.wrap(..., width)`, continuation lines
    aligned under the first desc line. Same dedup rule as `_share_item_line`: when the
    title equals the COLLAPSED content (a short decision that IS its own title), `desc:`
    is omitted entirely rather than repeating the title as a second, near-identical line.

    `shared` appends a short `✓ shared` marker to the `id:` line (picker-only — see
    `cli._pick_shareable` / `share.shared_map`; the id line is a handful of fixed-width
    chars, so the marker never threatens the `width` budget the way a wrapped desc could).

    Distinct from `_share_item_line` on purpose: the MCP-facing previews
    (`format_shareable_list`/`format_share_preview`) stay on the compact single/double-line
    form — they're injected into an agent's token-capped context, not read by a human on
    a terminal, so they must not grow this structure."""
    full_content = proj.get("content", "")
    title = proj.get("title") or _derive_title(full_content)
    id8 = (proj.get("id") or "")[:8]
    type_ = proj.get("type") or "decision"

    prefix = f"{index:>3}. " if index is not None else "  "
    pad = " " * len(prefix)
    # `width` is the total column budget for the rendered line, so the indent and the
    # `title: `/`desc:  ` label have to come out of it - wrapping/truncating on the bare
    # `width` would overflow the terminal by exactly that much on every line.
    avail = max(_SHARE_BLOCK_MIN_TEXT, width - len(pad) - _SHARE_BLOCK_LABEL_WIDTH)
    # A title is a single line by construction, so it is TRUNCATED (never wrapped) - a
    # derived title can be a full 100 chars, which would otherwise run past the block.
    title_line = title if len(title) <= avail else title[:avail - 1].rstrip() + "…"
    # Pills on the id line: review state first (so a not-yet-approved decision is obvious at a
    # glance before selecting it), then the already-shared marker. `status` is absent on
    # hand-built projections in older callers/tests, so it degrades to no pill.
    id_line = f"{prefix}id:    {id8}"
    status = proj.get("status") or ""
    if status:
        id_line += f"  [{status}]"
    if shared:
        id_line += "  ✓ shared"
    lines = [
        id_line,
        f"{pad}type:  {type_}",
        f"{pad}title: {title_line}",
    ]
    collapsed = " ".join(full_content.split())
    if collapsed and collapsed != title:
        wrapped = textwrap.wrap(collapsed, avail) or [""]
        lines.append(f"{pad}desc:  {wrapped[0]}")
        cont_indent = pad + " " * _SHARE_BLOCK_LABEL_WIDTH
        lines.extend(f"{cont_indent}{cont}" for cont in wrapped[1:])
    return "\n".join(lines)


def format_shareable_list(repo_path: str) -> str:
    """Numbered/identified list of decisions available to share (id + type + content), so the
    developer can pick which to share when they haven't named one. The agent shows this and the
    developer selects conversationally; capped like get_context so a big store can't flood context."""
    items = get_shareable_all(repo_path)
    if not items:
        return "No decisions available to share."
    total = len(items)
    shown = items[:_FILTERED_DISPLAY]
    header = f"{_pl(total, 'decision')} available to share"
    if total > len(shown):
        header += f" — showing {len(shown)} of {total}, run `contexer share` in a terminal for the rest"
    lines = [header + ". Tell me which to share, then I'll preview and confirm:\n"]
    for it in shown:
        lines.append(_share_item_line(it))
    lines.append('\nShare the selected: share_decision(decision_id="<id>[,<id2>…]") '
                 "— previews first; add confirm=true to send.")
    return "\n".join(lines)


def _resolve_share_projections(repo_path: str, decision_id: str,
                               redact_on: bool | None = None) -> list[dict]:
    """Resolve a possibly comma-separated `decision_id` to shareable projections (order and
    duplicates preserved as given; empty -> the single most-recent decision). `redact_on`
    threads a caller-resolved redaction flag through to every projection."""
    ids = [i.strip() for i in decision_id.split(",") if i.strip()]
    if not ids:
        proj = get_shareable(repo_path, "", redact_on)
        return [proj] if proj else []
    return [p for p in (get_shareable(repo_path, i, redact_on) for i in ids) if p is not None]


def format_share_preview(repo_path: str, decision_id: str = "", profile=None) -> str:
    """Dry-run preview of what a personal-cloud push would send — a pure local read, NO network.
    Safe-by-default gate for share_decision: pushing is an OUTWARD action, so the developer must
    see exactly what would be sent, and to where, before confirming. `decision_id` may be a single
    id or a comma-separated selection; `profile` is passed in to avoid re-reading config.toml."""
    from contexer.config import default_endpoint, load_profile
    prof = profile or load_profile()  # resolved ONCE — governs both endpoint and redaction
    projs = _resolve_share_projections(repo_path, decision_id, prof.redact_secrets)
    if not projs:
        return "Nothing to share — no matching decision found."
    endpoint = prof.endpoint or default_endpoint()
    ids_csv = ",".join((p.get("id") or "")[:8] for p in projs)
    lines = [f"Ready to push {_pl(len(projs), 'decision')} to your PERSONAL cloud ({endpoint}). "
             f"{_SHARE_SECRETS_HINT}:\n"]
    lines += [_share_item_line(p) for p in projs]
    redacted = sum(p.get("redacted", 0) for p in projs)
    if redacted:
        lines.append(f"\n  ({_pl(redacted, 'secret')} redacted before sending)")
    lines += [
        "",
        "Confirm with the developer before sending.",
        f'  • Proceed:  share_decision(decision_id="{ids_csv}", confirm=true)',
        "  • Cancel:   do nothing",
        '  • Stop asking: set `skip_confirm = true` in ~/.contexer/config.toml '
        '(or the developer says "always share without asking").',
    ]
    return "\n".join(lines)


def _format_update_approval(entry: dict) -> str:
    """Approval prompt for a Suggested Update - current revision vs the detected change."""
    prop = entry.get("proposed_revision") or {}
    score = prop.get("confidence", 0)
    factors = prop.get("confidence_factors") or []
    factor_lines = "\n".join(f"  - {f}" for f in factors) if factors else "  - Detected this session"
    eid = entry["id"]
    rev = entry.get("revision", 1)
    return (
        f"Engineering decision update recorded — pending review (id={eid}). "
        f"The current revision stays trusted until approved.\n\n"
        f"Current (revision {rev}):\n  \"{entry.get('content', '')}\"\n\n"
        f"Detected:\n  \"{prop.get('content', '')}\"\n\n"
        f"Confidence: {score}%\n"
        f"Evidence:\n{factor_lines}\n\n"
        f"This does not block your current work. Surface it to the developer for approval at a "
        f"natural point (no need to interrupt), then:\n"
        f"  [Y] Approve - approve_decision(entry_id=\"{eid}\", action=\"approve\")  (revision {rev + 1}, history kept)\n"
        f"  [E] Edit    - approve_decision(entry_id=\"{eid}\", action=\"edit\", content=\"<corrected text>\")\n"
        f"  [S] Skip    - approve_decision(entry_id=\"{eid}\", action=\"skip\")  (keep for later)\n"
        f"  [D] Dismiss - approve_decision(entry_id=\"{eid}\", action=\"dismiss\")  (discard, keep current)\n"
        f"(They can also review later in a terminal with `contexer review`.)"
    )


def get_pending_approval_prompt(repo_path: str, entry_id: str | None) -> str:
    """Generate a formatted approval prompt for a decision awaiting the developer.
    Renders the Suggested Update prompt when a proposal is attached, otherwise the
    new-decision prompt. Returns '' if nothing is pending for this entry."""
    if not entry_id:
        return ""
    data = _load(repo_path)
    entry = next((e for e in data["entries"] if e.get("id") == entry_id), None)
    if not entry:
        return ""
    if entry.get("proposed_revision"):
        return _format_update_approval(entry)
    if _entry_status(entry) != "pending_approval":
        return ""
    score, factors = _compute_confidence(entry)
    factor_lines = "\n".join(f"  - {f}" for f in factors) if factors else "  - Initial capture"
    content = entry.get("content", "")
    eid = entry["id"]
    return (
        f"Engineering decision recorded — pending review (id={eid}), not yet trusted.\n\n"
        f"\"{content}\"\n\n"
        f"Confidence: {score}%\n"
        f"Evidence:\n{factor_lines}\n\n"
        f"This does NOT affect any session until approved, and it does not block your current "
        f"work. Surface it to the developer for approval at a natural point (no need to "
        f"interrupt), then:\n"
        f"  [Y] Approve — approve_decision(entry_id=\"{eid}\", action=\"approve\")\n"
        f"  [E] Edit    — approve_decision(entry_id=\"{eid}\", action=\"edit\", content=\"<corrected text>\")\n"
        f"  [N] Ignore  — approve_decision(entry_id=\"{eid}\", action=\"ignore\")\n"
        f"(They can also review later in a terminal with `contexer review`.)"
    )


def _apply_memory_upsert(entries: list, content: str, session_id: str,
                         subtype: str, memory_key: str) -> str:
    """In-memory upsert of one memory fact into `entries`. No I/O, no cap — the
    caller loads, applies one-or-many, caps, and saves once. Mutates `entries`
    in place; returns 'created' | 'updated' | 'unchanged' | 'skipped'."""
    content = _normalize_content(content)
    if not _is_storable(content):
        return "skipped"
    # 1. Keyed match — the evolving-fact path: same source+section, refresh in place.
    match = next((e for e in entries if e.get("memory_key") == memory_key), None)
    # 2. Migration: adopt a pre-key memory entry whose content is still identical,
    #    so the first import after upgrade stamps a key instead of duplicating. Gated
    #    on the [memory ...] tag so a manual decision with identical text is NOT
    #    silently converted into a memory-managed entry (it falls to the novelty gate).
    if match is None:
        match = next((e for e in entries
                      if not e.get("memory_key") and e["type"] == "decision"
                      and e["content"] == content and e["content"].startswith("[memory")), None)
    if match is not None:
        changed = _current_content(match) != content or match.get("subtype", "") != subtype
        match["memory_key"] = memory_key
        if changed:
            # Append a new approved revision (history preserved, never clobbered). Clear any
            # pending proposal - memory sync is authoritative for memory-managed entries.
            match["subtype"] = subtype
            _append_revision(match, content, source="memory",
                             approved_at=datetime.now(timezone.utc).isoformat())
            match.pop("proposed_revision", None)
        return "updated" if changed else "unchanged"
    # 3. First creation — novelty-gate so a memory fact that merely restates an
    #    existing decision is dropped rather than double-stored. Deliberately does
    #    NOT bump recurrence: a static file re-read on every dir change is not a
    #    genuine reuse signal, and bumping here inflated occurrence_count on each
    #    re-sync (it leaves the dup untouched, so re-imports stay idempotent).
    decisions_only = [e for e in entries if e["type"] == "decision"]
    if _find_match(content, decisions_only) is not None:
        return "skipped"
    entries.append(_new_decision_entry(content, session_id, subtype, memory_key,
                                       created_by="memory", status="approved"))
    return "created"


def upsert_memory_decision(repo_path: str, content: str, session_id: str,
                           subtype: str, memory_key: str) -> str:
    """Store or refresh a single memory-imported decision, keyed by stable identity.
    Convenience wrapper around `_apply_memory_upsert` (load + apply + save). For
    bulk import use `upsert_memory_batch`. Returns the apply status."""
    with _store_lock(_slug(repo_path)):
        data = _load(repo_path)
        status = _apply_memory_upsert(data["entries"], content, session_id, subtype, memory_key)
        if status == "created":
            data["entries"] = _keep_top(data["entries"], MAX_ENTRIES, pin_last=True)
        if status != "skipped":              # 'skipped' leaves the store untouched
            _save(repo_path, data)
        return status


def upsert_memory_batch(repo_path: str, items: list[tuple[str, str, str, str]]) -> int:
    """Upsert many memory facts in one load + one save (keeps SessionStart off the
    O(entries × facts) per-entry rewrite path). `items` are
    (content, session_id, subtype, memory_key). The whole batch is one atomic
    write, so a multi-section file imports all-or-nothing. Returns newly-created
    count."""
    with _store_lock(_slug(repo_path)):
        data = _load(repo_path)
        entries = data["entries"]
        created = touched = 0
        for content, session_id, subtype, memory_key in items:
            status = _apply_memory_upsert(entries, content, session_id, subtype, memory_key)
            touched += status != "skipped"
            created += status == "created"
        if created:
            data["entries"] = _keep_top(entries, MAX_ENTRIES, pin_last=True)
        if touched:
            _save(repo_path, data)
        return created


_INSIGHT_ORDER = {"low": 0, "medium": 1, "high": 2}

_FRESH_CLONE_DAYS = 7


def _git(repo_path: str, *args: str, timeout: int = 5) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _detect_insight(repo_path: str) -> tuple[str, bool]:
    """Infers how much insight the current user has into this repo from git
    signals. Returns (level, decisive) — non-decisive means ask the user.
    Known limitation: commit count is repo-wide, so a few commits in one corner
    of a monorepo read as insight into the whole repo."""
    if not (Path(repo_path) / ".git").exists():
        return "low", False
    email = _git(repo_path, "config", "user.email")
    if not email:
        # must bail before any --author query: an empty author matches every commit
        return "low", False
    head = _git(repo_path, "log", "--oneline", "-n", "1")
    if not head:
        return "high", True  # repo exists but has no commits — user just created it
    roots = (_git(repo_path, "rev-list", "--max-parents=0", "HEAD") or "").splitlines()
    if roots and _git(repo_path, "show", "-s", "--format=%ae", roots[0]) == email:
        return "high", True  # authored the first commit — repo creator
    mine = _git(repo_path, "log", f"--author={email}", "--oneline", "-n", "5") or ""
    count = len(mine.splitlines()) if mine else 0
    if count >= 5:
        return "high", True
    if count >= 1:
        # commit count alone can't separate a drive-by contributor from a regular one
        return "medium", False
    reflog = _git(repo_path, "reflog", "--format=%gs::%gd", "--date=unix")
    if reflog:
        oldest_msg, _, oldest_when = reflog.splitlines()[-1].partition("::")
        stamp = re.search(r"\{(\d+)\}", oldest_when)
        if oldest_msg.startswith("clone:") and stamp and \
                time.time() - int(stamp.group(1)) < _FRESH_CLONE_DAYS * 86400:
            return "low", True  # fresh clone of someone else's history
    return "low", False  # zero commits could also be an email mismatch — ask


_INSIGHT_CACHE_TTL = 24 * 3600  # git signals drift slowly — a day-old read is still trustworthy


def _insight_cache_path(repo_path: str) -> Path:
    return STORE_DIR / f".insight_{_slug(repo_path)}"


def _insight_cache_key(repo_path: str) -> tuple:
    """The cheap invariants a cached insight depends on: user.email and HEAD. Two git
    calls instead of _detect_insight's ~6 — and a changed email or a re-cloned/rewound
    repo invalidates the cache immediately instead of after the TTL."""
    return _git(repo_path, "config", "user.email"), _git(repo_path, "rev-parse", "HEAD")


def _cached_insight(repo_path: str) -> tuple[str, bool]:
    """TTL-cached _detect_insight, validated against the current git identity/HEAD.
    Cache hit = 2 git subprocesses instead of ~6. Fail-soft: any read/parse error
    falls through to a fresh detection; write errors are ignored."""
    path = _insight_cache_path(repo_path)
    key = None
    try:
        cached = json.loads(path.read_text())
        level, decisive, ts = cached["level"], cached["decisive"], cached["ts"]
        if level in _INSIGHT_ORDER and isinstance(decisive, bool) and \
                time.time() - ts < _INSIGHT_CACHE_TTL:
            key = _insight_cache_key(repo_path)
            if [cached.get("email"), cached.get("head")] == list(key):
                return level, decisive
    except (OSError, ValueError, KeyError, TypeError):
        pass  # missing, corrupt, or expired — fall through to a fresh detection
    level, decisive = _detect_insight(repo_path)
    try:
        email, head = key if key is not None else _insight_cache_key(repo_path)
        STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        path.write_text(json.dumps({"level": level, "decisive": decisive,
                                    "ts": time.time(), "email": email, "head": head}))
    except OSError:
        pass
    return level, decisive


def _newcomer_answer_block(label: str, level: str, decisive: bool) -> list[str]:
    """Instructions for a repo question asked as the first prompt: ANSWER it, then store
    findings — never a blocking menu. Insight-tailored using the commit signal Contexer
    already computes: a repo author (their git email authored commits here) is answered as
    a maintainer, not quizzed as a newcomer; a confirmed newcomer gets a scanned overview;
    an unverified guess stays neutral. Every path ends with the question answered and the
    scan stored silently — the capture happens as a side effect, no permission asked."""
    if decisive and level == "high":
        stance = ("You have authored commits in this repo (matched on your git email), so"
                  " answer as a fellow maintainer with a direct, technical summary — do NOT"
                  " say 'I'll assume you're new'.")
    elif decisive and level == "low":
        stance = ("No commits from your git email are here, so answer as a newcomer would"
                  " need: lead with what the README and code actually show.")
    else:
        stance = "Answer from what the README, docs, and code show."
    return [
        f"The user's first message is a question about {label}. Answer it — do NOT show a"
        " setup menu and do NOT ask them to pick an option. In THIS response:",
        "1. Read the README, any docs/, and the key source to understand the repo.",
        f"2. {stance} Answer exactly what they asked, in your own words.",
        "3. Then silently store what you learned: call update_context(subtype='architecture')"
        " for the repo's purpose and any load-bearing facts — each a plain sentence, max 15"
        " words, no em dashes. Do not ask permission to store; this IS the capture.",
        "4. End with exactly one line: \"(Contexer: saved this repo's basics for future"
        " sessions — say 'bootstrap' for guided setup.)\"",
        "Never block the answer behind a confirmation or a menu.",
    ]


# The gap-question ask shape. Charged ONCE, on the bootstrap_context result, and only when
# that result actually carries gaps (server.bootstrap_context attaches it as `how_to_ask`) —
# never in _build_bootstrap_context, which is injected at every context-less session start,
# again at the first UserPromptSubmit, and on post-compact, including the skip and STEP 0
# paths where no gap is ever asked. Single source: `/bootstrap` (bootstrap_command.md) and the
# docs point at this field rather than restating it, so the rule cannot drift between copies.
GAP_ASK_GUIDE = (
    "Ask these gaps ONE question at a time, never batched — each answer can remove later gaps"
    " (a docs-only purpose answer drops the tests/CI/deploy ones). With an interactive"
    " multiple-choice tool (Claude Code: AskUserQuestion), render each gap as one question:"
    " the gap's own `question` is the question text; header = a short topic word (Purpose,"
    " Tests, CI, Deploy, Cloud), max 12 characters; last option = \"Skip this one\"."
    " Offer a \"Correct\" option (label \"Correct\", description = the gap's `assumption`) ONLY"
    " when that assumption actually answers the gap's question — most scan observations do"
    " ('No CI/CD config found in this repo' answers 'Is there a build or deploy pipeline?')."
    " When it does not — the goal gap's assumption is the repo's inferred PURPOSE, which says"
    " nothing about what this user plans to do here — drop that option and ask the question"
    " openly; never present an unrelated statement as the confirming answer."
    " In between, add at most two options ONLY if the gap's `hint` names distinct candidate"
    " answers; a hint that restates the question, or that lists one answer's parts"
    " ('e.g. GDPR, PCI-DSS, SOC2, HIPAA' is a single answer), yields none and the question"
    " stands complete without them. Split candidates on ';' or ',' after dropping the leading"
    " 'e.g.', a few words each. Never more than 4 options — free text arrives through the"
    " tool's own \"Other\" choice. Without such a tool, print those same options numbered and"
    " accept the number or a typed answer."
    " Store each answer with update_context using the gap's `subtype`, as a sentence that"
    " ANSWERS THE QUESTION, never the assumption's own wording — \"Correct\" on 'Is automated"
    " testing in scope?' stores 'No automated testing in scope.', not the observation."
    " A candidate or free-text answer stores that answer; \"Skip this one\" stores nothing and"
    " moves to the next gap. Plain sentences, max 15 words, no em dashes."
)


def _build_bootstrap_context(repo_path: str) -> list[str]:
    level, decisive = _cached_insight(repo_path)
    repo_name = Path(repo_path).name if repo_path else ""
    label = f'"{repo_name}"' if repo_name else "this repo"

    # Every variant is NUMBERED and capped at FOUR options. The cap is the interactive
    # picker's: Claude Code's AskUserQuestion takes at most 4 options (plus its own free-text
    # "Other"), so a 5-row menu could not be rendered as a picker at all. Numbers are purely
    # additive — the keywords stay valid, since a text-mode reply and the picker's "Other"
    # both arrive as words. 'some' is the row that gave way (see the ambiguous variant).
    if decisive and level == "high":
        # commits by this user found — don't ask how well they know their own repo
        offer = [
            f"  Contexer: no project context stored for {label}."
            " How should I set up context for future sessions?",
            "   1. quick — 1 question (what does this repo do?)",
            "   2. full — guided setup, a few questions",
            "   3. skip — not now",
            "   4. scan — I'm actually new to this repo (scan code and docs, 1 short question)",
        ]
        replies = "1-4, or quick / full / skip / scan"
    elif decisive and level == "low":
        # state the evidence, never the conclusion — detection can be wrong
        offer = [
            f"  Contexer: no project context stored for {label}."
            " No commits from your git email found here, so I'd scan the code and docs"
            " instead of asking questions you may not be able to answer.",
            "   1. scan — go ahead (scan code and docs, 1 short question)",
            "   2. quick — I actually know this repo (1 question)",
            "   3. full — I actually know this repo (guided setup)",
            "   4. skip — not now",
        ]
        replies = "1-4, or scan / quick / full / skip"
    else:
        # ambiguous signals — ask familiarity directly. 'some' has no row here: five options
        # exceed the picker cap, and scan covers "didn't build it" without a wrong answer
        # (it asks nothing the user can't answer). Typed, 'some' still maps to medium.
        suggestion = (
            ["   (a few commits from your git email found — if you work with this repo but"
             " didn't build it, reply 'some')"]
            if level == "medium" else []
        )
        offer = [
            f"  Contexer: no project context stored for {label}."
            " How well do you know this repo?",
            "   1. quick — I wrote or maintain it (1 question: what does this repo do?)",
            "   2. full — I wrote or maintain it (guided setup, a few questions)",
            "   3. scan — I didn't build it, or it's my first time: scan code and docs,"
            " then up to 2 short questions",
            "   4. skip — not now",
            *suggestion,
        ]
        replies = ("1-4, or quick / full / scan / skip"
                   " (or 'some' if you work with it but didn't build it)")

    # Option 1 differs per variant, so a bare "yes" / "go ahead" cannot mean a fixed keyword:
    # in the low variant the proposal on the table is scan, and routing that affirmative to
    # quick (insight='high') would start the author interview the low variant exists to avoid.
    first_option = offer[1].split(".", 1)[1].split("—")[0].strip()
    # ...and in the ambiguous variant it cannot mean option 1 either. There the question is
    # "How well do you know this repo?", whose option 1 asserts "I wrote or maintain it" —
    # an authorship claim a bare "yes" never makes, in the one variant that exists precisely
    # because the git signal could not establish authorship. Resolving it to quick would
    # route a newcomer to insight='high', which drops the goal gap they CAN answer and asks
    # only the purpose question they cannot. So: don't guess, ask which one.
    affirmative = (
        f"A bare 'yes' or 'go ahead' means option 1 — here that is {first_option},"
        " not any other mode."
        if decisive else
        "A bare 'yes' or 'go ahead' is NOT an answer here — this question asks how well they"
        " know the repo, and option 1 claims they wrote or maintain it. Never infer authorship"
        " from an affirmative: ask which of the four they mean. If they only say they're new"
        " to the repo, take scan."
    )
    # Who reaches scan differs too. In the low variant the evidence says the user has no
    # commits here, so scan means "don't quiz me" (insight='low', one goal question). In the
    # ambiguous variant scan is also the row a developer picks for "I work with it but didn't
    # build it" — 'some' has no row of its own — and insight='low' would silently drop the
    # purpose gap (min_insight='medium'), losing exactly the answer that user CAN give.
    scan_is_medium = not (decisive and level in {"high", "low"})

    # A question about the repo asked as the first prompt ("what is this repo doing?",
    # "summarize this repo") must be ANSWERED, not met with a menu that mirrors the question
    # back. This check comes FIRST: placed after the menu it loses to "response must be ONLY
    # the offer". It applies at EVERY insight level — a repo author asking what the repo does
    # still wants an answer, just phrased as a maintainer, not quizzed as a newcomer; the
    # commit signal only tunes the phrasing (see _newcomer_answer_block), never whether we
    # answer.
    newbie_exception = [
        "STEP 0 — read the user's message before anything else: if it is asking what this"
        " repo or code is or does, or asking to summarize/explain/give an overview of it"
        " ('what is repo doing?', 'explain this codebase', 'tell me about this repo',"
        " 'summarize this codebase', 'give me an overview'), then do NOT output the menu"
        " below. Instead:",
        *_newcomer_answer_block(label, level, decisive),
        "Only when their message is NOT such a question, output the menu below instead:",
    ]

    return [
        f"No project context stored for {repo_path}.",
        "CRITICAL INSTRUCTION — read before writing a single word:",
        *newbie_exception,
        "Ask the offer as an interactive multiple-choice question if you have a tool for that"
        " (Claude Code: AskUserQuestion) — ONE question, header \"Setup\", one choice per"
        " numbered option below in the same order (label = the keyword, description = the text"
        " after the dash), and no options of your own. Make no other tool call and do no other"
        " work in that turn. The answer comes back to you within the SAME turn, so do not end"
        " the turn on the question — run the matching handler below as soon as it arrives."
        " Without such a tool, print the numbered list verbatim as plain text, end your turn"
        " there, and run the handler when the user's next message answers it.",
        "Your ENTIRE response must be ONLY the offer block below — as one multiple-choice"
        " question, or as the text list. No task work. No file reads. No acknowledgment of any"
        " prior request. No explanation. Just the offer, then stop.",
        *offer,
        "Output the offer and nothing else. Do NOT call bootstrap_context before you have their"
        " answer, and do NOT start the user's task. Their reply is one of — "
        f"{replies}. In the picker that answer lands in this same turn: act on it immediately."
        " In plain text your turn ends with the list and their next message is the answer.",
        "A numeric reply means the option at that position in the offer above; the keyword"
        f" itself means the same thing, typed or picked as free text. {affirmative}"
        " If the user dismisses or cancels the question, treat that as skip and never re-ask.",
        "Once the user replies:",
        "If quick → call bootstrap_context with insight='high'. It scans the codebase and stores"
        " detected facts and measured conventions automatically — do NOT re-store them. Report the"
        " stored/pending counts in one line, e.g. 'Contexer: stored 6, 2 pending review.' Ask ONLY the"
        " first gap question (purpose); store the answer with update_context using the gap's subtype."
        " Stop — do not ask more.",
        "If full (guided) → call bootstrap_context with insight='high'. Detected facts and measured"
        " conventions are stored automatically — do NOT re-store them. Report the stored/pending counts"
        " in one line. Then ask each remaining gap question one at a time, in the shape described"
        " below. After each answer, re-evaluate remaining gaps — if the"
        " purpose answer reveals a docs-only, portfolio, personal, or learning repo, skip"
        " tests/CI/deploy/compliance/exclusion gaps. Store each answer as a separate update_context"
        " call using the gap's subtype. Write each stored entry as a single plain sentence, max 15"
        " words, no em dashes, no filler phrases. Example: 'No CI/CD pipeline.' NOT 'There is no CI/CD"
        " pipeline planned or needed for this repo.' Stop when the gaps are done.",
        "If some (works with the repo but didn't build it) → call bootstrap_context with"
        " insight='medium'. Detected facts and measured conventions are stored automatically — do NOT"
        " re-store them. Report the stored/pending counts in one line. Ask the returned gap questions"
        " one at a time (purpose and the user's goal) and store each answer. Same sentence style:"
        " plain, max 15 words.",
        ("If scan (didn't build it, or first time here) → call bootstrap_context with"
         " insight='medium'. That row covers BOTH a developer who works with this repo without"
         " having built it and a genuine first-timer, so use 'medium', not 'low': it returns only"
         " the two gaps either of them can attempt (what they plan to do here, and what the repo"
         " does). If they say they don't know what the repo does, drop that gap instead of"
         " pressing. Do NOT quiz them on this repo's history or conventions."
         if scan_is_medium else
         "If scan (first time seeing this repo) → call bootstrap_context with insight='low'."
         " The user cannot answer questions about this repo's history or conventions — do NOT"
         " quiz them. Ask only the single gap question returned (what the user plans to do here).")
        + " Detected facts and measured conventions are stored automatically — do NOT re-store"
          " them. Report the stored/pending counts in one line. Store each answer. Same sentence"
          " style: plain, max 15 words.",
        "If no or skip → proceed with their original request directly, do not mention bootstrap again.",
        "After any handler's tool call: if the result shows pending > 0, mention once that"
        " measured-but-unratified conventions await review — say 'run `contexer review` when"
        " convenient' — and never block on it.",
        "Purpose question — never echo it back: if the user's original message itself asked what"
        " this repo does, do NOT ask them the purpose gap question. Read the README and code,"
        " answer their question with your own summary, then ask 'Did I get that right —"
        " anything to correct?' and store the confirmed summary as the purpose.",
        "For every gap question, lead with its assumption and ask the user to confirm or"
        " correct it — never ask open-ended questions the scan can already half-answer."
        " bootstrap_context's result carries a `how_to_ask` field with the exact question shape"
        " whenever it returns gaps; follow it then. It is deliberately NOT repeated here: this"
        " block is injected on every context-less session start, including the skip path, where"
        " gap-asking rules can never be used.",
        "After any path completes, answer the user's original message — never leave it hanging.",
    ]


def _pl(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _build_resume_mining_context(repo_path: str) -> list[str]:
    return [
        f"No project context stored for {repo_path}, but this is a RESUMED session —"
        " the conversation above may already contain decisions. Do NOT show any setup"
        " menu and do NOT quiz the user. Instead, in your FIRST response:",
        "1. Review the visible conversation for decisions already made — technology"
        " choices, constraints, conventions, approaches chosen over alternatives."
        " Store each via update_context with the right subtype and the original reasoning.",
        "2. Call bootstrap_context (no insight argument) — repo facts and measured conventions"
        " are stored automatically; do NOT re-store them.",
        "3. Tell the user in one line how many decisions were stored, e.g."
        " 'Contexer: stored 4 decisions from this conversation.'",
        "4. Then continue with the user's request as normal.",
        "If the conversation contains no decisions, store nothing else yourself — the scan"
        " already stored repo facts and conventions automatically; never invent decisions"
        " that weren't actually discussed.",
    ]


def _join_context_sections(*parts: str) -> str:
    """Join non-empty context sections with a blank line between them."""
    return "\n\n".join(p for p in parts if p)


def _hook_cwd_repo(repo_path: str) -> str:
    """Fallback repo identity for hook-invoked payload builders: the hook's own cwd.

    Hosts run hooks with cwd = the project directory, and non-git projects are
    first-class stores (keyed by absolute path - the MCP tools accept any sane dir),
    so an empty git-root from the hook's shell must not erase the session's identity.
    0.16.1 bug: in a non-git dir with an existing store, SessionStart said "no context
    stored - setup offer" while the per-prompt MCP tools (which get the path
    explicitly) found the store fine. Guarded by _is_sane_repo, so a session opened in
    a home/config dir keeps the empty repo_path and falls through to the normal
    resolution chain instead of selecting a junk store. This is NOT the retired
    `|| pwd` shell fallback: nothing here writes the shared .current_repo pointer
    unguarded - callers that anchor the pointer still sanity-check first."""
    if repo_path:
        return repo_path
    try:
        cwd = os.getcwd()
    except OSError:  # cwd unlinked since the process started - hooks must never crash
        return repo_path
    return cwd if _is_sane_repo(cwd) else repo_path


def session_start_payload(repo_path: str, source: str = "", session_id: str = "") -> dict:
    """Provider-neutral session-start content, with the shared TEAM-context section
    appended. Returns {"status": str, "context": str}.

    Option A seam: this ONE place renders team context at session start for EVERY adapter
    (Claude/Codex/Cursor/Gemini) — the local payload plus the C5 team cache, joined. Reads
    the cache only (NO network here); adapters refresh it via team_context.refresh() from
    their SessionStart hook before this runs. `''` team section (local mode / empty cache)
    leaves the local payload untouched.

    Resume exception: when a session is resumed with local decisions already present, the
    local path deliberately injects nothing (context='') because those decisions — and the
    team section injected at the ORIGINAL session start — are already in the reloaded
    conversation. Re-appending team there would duplicate it; freshly-approved team rows
    still surface via the per-prompt delta poll. So team is suppressed on that path too.

    Visibility (Phase 2): when a team section IS appended, the human-facing `status` string
    gets a short ` | team: N synced` suffix so the developer can tell team sync is live
    without reading the (model-facing) `context` blob. The `context` string itself is
    unchanged beyond the existing team-section join. The suffix is cap-aware: `format_team_
    section` renders at most `_team_display_cap()` rows, so when the cache holds more than
    that the suffix adds `(M shown)` rather than claiming a count the model never actually
    received. It is also defer-aware: rows this call's own `defer_architecture=True` hid
    behind the count-only architecture pointer (see `team_context.count_deferred_architecture`)
    are subtracted from the `(M shown)` figure too, so "N synced" alone never implies more
    full/ratified content reached the model than actually did. `text`/`count`/`deferred` all
    come from ONE team_context snapshot (`_team_section_with_counts` ->
    `team_context.session_team_section`), not three independent reloads, so a concurrent
    background refresh or local decision update landing mid-computation can't desync the
    counts from what `team` actually rendered (nor drive `shown` negative).

    session_id (Retrieval V1 Part B): optional, "" preserves every existing caller. Threaded
    through to `_local_session_start_payload` for compact-source working-set rehydration."""
    resolved = _hook_cwd_repo(repo_path)
    if resolved != repo_path and _is_sane_repo(resolved):
        # The cwd fallback engaged (non-git project dir): anchor the shared pointer
        # here, exactly as the installed SessionStart hook does for git repos, so
        # bare MCP calls (no repo_path) in this session resolve to the same store.
        anchor_repo(resolved)
    repo_path = resolved
    payload = _local_session_start_payload(repo_path, source, session_id)
    # text/count/deferred come from ONE team_context snapshot (see _team_section_with_counts)
    # so the status-suffix arithmetic below can never describe a different moment than `team`.
    team, count, deferred = _team_section_with_counts(repo_path)
    if not team or (source == "resume" and not payload.get("context")):
        return payload
    status = payload.get("status", "")
    if count:
        cap = _team_display_cap()
        # `shown` is what actually landed as full/ratified content in `team` this call.
        # Order matters: format_team_section removes deferred rows FIRST, then applies the
        # display cap to whatever remains (see format_team_section's defer split, applied
        # before `rendered = rows[:_TEAM_DISPLAY]`) - so this must subtract deferred rows
        # from `count` BEFORE capping, not after, or a cache with both many rows and many
        # deferred rows understates (or even goes negative on) the real shown count. Only
        # note it in the suffix when it's actually less than the raw synced count -
        # otherwise keep the plain "N synced" the suffix has always shown.
        shown = min(count - deferred, cap)
        status = (f"{status} | team: {count} synced" if shown >= count
                 else f"{status} | team: {count} synced ({shown} shown)")
    return {
        **payload,
        "status": status,
        "context": _join_context_sections(payload.get("context", ""), team),
    }


def _local_session_start_payload(repo_path: str, source: str = "", session_id: str = "") -> dict:
    """Local-only session-start content (no team). Returns {"status": str, "context": str}:
    `status` is the short human-facing line, `context` is the text to inject into the
    conversation. Empty `context` means "inject nothing". All filtering/promotion logic
    is unchanged from the original get_session_start_context.

    session_id (Retrieval V1 Part B): "" preserves every existing caller. On a compact
    source with a session id, the working set built up before compaction is rehydrated
    (content of the most-recently-injected decisions) after the normal rules injection —
    additionalContext re-injection doesn't otherwise know what the pre-compaction router
    already surfaced this session."""
    data = _load(repo_path)
    decisions = [e for e in data.get("entries", []) if e["type"] == "decision"]
    global_rules = get_global_decisions()
    resume_flag = STORE_DIR / ".resume_mining"

    if source == "resume":
        if decisions:
            return {
                "status": f"Contexer: session resumed — {_pl(len(decisions), 'decision')} already loaded in conversation",
                "context": "",
            }
        # Best-effort: the flag only silences a duplicate bootstrap offer on the first
        # prompt. An unwritable ~/.contexer (sandboxed host, #152) must not cost the
        # session its resume-mining instructions — the whole point of this branch.
        try:
            STORE_DIR.mkdir(mode=0o700, exist_ok=True)
            resume_flag.write_text(repo_path)
        except OSError:
            pass
        sys_parts = []
        if global_rules:
            sys_parts.append("## Global rules (apply to ALL repos):")
            for d in global_rules:
                title, body = _title_and_body(d)
                sys_parts.append(f"- [{d.get('subtype', '')}] {title}")
                if body is not None:
                    sys_parts.append(f"    {body}")
            sys_parts.append("")
        sys_parts.extend(_build_resume_mining_context(repo_path))
        return {
            "status": "Contexer: resumed with no stored context — mining this conversation for decisions",
            "context": "\n".join(sys_parts),
        }

    # Best-effort (#152): these are bookkeeping flags, so an unwritable ~/.contexer must
    # not abort session start before it renders any context. Both unlinks share one guard
    # because they fail together or not at all — unlink(missing_ok=True) only raises on a
    # directory-permission problem, which applies equally to each.
    try:
        resume_flag.unlink(missing_ok=True)
        if source != "compact":
            # A new session re-arms the offer; compaction continues the session in which the
            # developer already answered it, so it must not resurrect a dismissed picker.
            _offer_flag(repo_path).unlink(missing_ok=True)
    except OSError:
        pass
    _gc_stale_session_files()

    if not decisions:
        if source == "compact" and _offer_already_made(repo_path):
            return {"status": "", "context": ""}
        _arm_offer(repo_path)
        lines = _build_bootstrap_context(repo_path)
        sys_parts = []
        if global_rules:
            sys_parts.append("## Global rules (apply to ALL repos):")
            for d in global_rules:
                title, body = _title_and_body(d)
                sys_parts.append(f"- [{d.get('subtype', '')}] {title}")
                if body is not None:
                    sys_parts.append(f"    {body}")
            sys_parts.append("")
        sys_parts.extend(lines)
        global_note = f" ({_pl(len(global_rules), 'global rule')} active)" if global_rules else ""
        return {
            "status": f"Contexer: no context stored{global_note} — setup offer on next prompt",
            "context": "\n".join(sys_parts),
        }

    # Separate decisions by status for injection and summary.
    # pending_approval and ignored decisions are never auto-injected - they must
    # be explicitly approved before becoming trusted engineering knowledge.
    pending = [d for d in decisions if _entry_status(d) == "pending_approval"]
    with_proposals = [d for d in decisions
                      if d.get("proposed_revision") and _entry_status(d) != "pending_approval"]
    trusted = [d for d in decisions if _entry_status(d) in ("approved", "suggested")]
    pre_loaded = [d for d in trusted if d.get("subtype") in ("convention", "constraint", "pattern")]
    deferred_count = len(trusted) - len(pre_loaded)

    sys_parts = []
    if global_rules:
        sys_parts.append("## Global rules (apply to ALL repos):")
        for d in global_rules:
            title, body = _title_and_body(d)
            sys_parts.append(f"- [{d.get('subtype', '')}] {title}")
            if body is not None:
                sys_parts.append(f"    {body}")
    if pre_loaded:
        sys_parts.append("## Project rules — apply to ALL tasks in this repo:")
        for d in pre_loaded:
            st = _entry_status(d)
            status_tag = " [suggested]" if st == "suggested" else ""
            update_tag = " [update pending approval]" if d.get("proposed_revision") else ""
            title, body = _title_and_body(d)
            sys_parts.append(f"- [{d.get('subtype', '')}]{status_tag}{update_tag}{_recur_suffix(d)} {title}")
            if body is not None:
                sys_parts.append(f"    {body}")
    if global_rules or pre_loaded:
        sys_parts.append(
            "If the current task conflicts with any of these decisions, "
            "surface the conflict and confirm with the developer before proceeding."
        )
    if deferred_count > 0:
        arch_count = sum(1 for d in trusted if d.get("subtype") == "architecture")
        breakdown = f" ({arch_count} architecture)" if arch_count else ""
        sys_parts.append(
            f"{deferred_count} decision(s) stored{breakdown}. "
            "Call get_context BEFORE reading files for any question about architecture, "
            "design decisions, rationale, or patterns."
        )
    # Count-only, deliberately terse: a startup should not dump every pending decision's
    # content (overwhelming). The identified list is pulled on demand via `review_pending`
    # (in-session) or `contexer review` (terminal); the per-decision content is surfaced at
    # capture time, not here.
    total_pending = len(pending) + len(with_proposals)
    if total_pending:
        notice = (
            f"{_pl(total_pending, 'decision')} pending your review (recorded, not yet "
            "trusted — not listed here to keep startup light). Offer to show them to the "
            "developer when appropriate: call review_pending to list them, then "
            "approve_decision — or they can run `contexer review` in a terminal."
        )
        if total_pending >= _BACKLOG_ESCALATE:
            notice += (" This backlog is growing — proactively offer to clear it this session; "
                       'after the developer reviews, approve_decision(entry_id="all", '
                       'action="approve") clears the lot.')
        sys_parts.append(notice)

    # B1: size-gated standing topic map — a one-line overview once the store is big
    # enough that "call get_context before reading files" alone stops being actionable.
    standing_map = _standing_topic_map(repo_path, decisions)
    if standing_map:
        sys_parts.append(standing_map)

    # B2: compact re-injects the normal rules above; also rehydrate the CONTENT of the
    # working set the router built up pre-compaction, since additionalContext replay
    # otherwise loses which decisions were already surfaced this session.
    if source == "compact" and session_id:
        rehydrated = _rehydrate_working_set(repo_path, session_id)
        if rehydrated:
            sys_parts.append(rehydrated)

    constraints = [d for d in pre_loaded if d.get("subtype") == "constraint"]
    conventions = [d for d in pre_loaded if d.get("subtype") == "convention"]
    patterns = [d for d in pre_loaded if d.get("subtype") == "pattern"]

    loaded_parts = []
    if global_rules:
        loaded_parts.append(_pl(len(global_rules), "global rule"))
    if constraints:
        loaded_parts.append(_pl(len(constraints), "constraint"))
    if conventions:
        loaded_parts.append(_pl(len(conventions), "convention"))
    if patterns:
        loaded_parts.append(_pl(len(patterns), "pattern"))

    sentences = []
    if loaded_parts:
        sentences.append(f"{', '.join(loaded_parts)} loaded")
    if deferred_count > 0:
        sentences.append(f"{_pl(deferred_count, 'architecture decision')} will be loaded on demand")
    if total_pending:
        if total_pending >= _BACKLOG_ESCALATE:
            sentences.append(f"{_pl(total_pending, 'decision')} pending review are piling up "
                             "— worth clearing (say 'review pending')")
        else:
            sentences.append(f"{_pl(total_pending, 'decision')} pending review — say "
                             "'review pending' or run `contexer review`")

    status = f"Contexer: {'. '.join(sentences)}." if sentences else "Contexer: active."
    return {"status": status, "context": "\n".join(sys_parts)}


def get_session_start_context(repo_path: str, source: str = "", session_id: str = "") -> dict:
    """Claude Code SessionStart hook output. Thin envelope over session_start_payload —
    kept for back-compat with installed hooks and the existing test suite.

    session_id (Retrieval V1 Part B): "" preserves every existing caller (Codex/Cursor
    still call this without it)."""
    from contexer.adapters import claude
    return claude.format_session_start(session_start_payload(repo_path, source, session_id))


# The article is OPTIONAL — "what is repo doing?" (no this/the) is just as much a newcomer
# question as "what is THIS repo doing?". The noun list is the gate that keeps code-element
# questions ("what is this function doing") out.
_NEWCOMER_QUESTION_RE = re.compile(
    r"\b(what (is|are|does) (this |the |your )?(repo|repository|codebase|project|code)\b"
    r"|what'?s (this |the |your )?(repo|repository|codebase|project)( about| for| doing)?\b"
    r"|explain (this |the |your )?(repo|repository|codebase|project|code)\b"
    r"|tell me about (this |the |your )?(repo|repository|codebase|project|code)\b"
    r"|how does (this |the |your )?(repo|repository|codebase|project|code) work\b"
    r"|walk me through (this |the |your )?(repo|repository|codebase|project|code)\b"
    r"|overview of (this |the |your )?(repo|repository|codebase|project|code)\b"
    r"|summari[sz]e (this |the |your )?(repo|repository|codebase|project|code)\b"
    r"|summary of (this |the |your )?(repo|repository|codebase|project|code)\b)",
    re.IGNORECASE,
)


def _is_newcomer_question(prompt: str) -> bool:
    return bool(_NEWCOMER_QUESTION_RE.search(prompt or ""))


def prompt_from_hook_stdin(raw: str) -> str:
    """Extracts the user prompt from a UserPromptSubmit hook's stdin JSON. Safe on
    any input — hooks must never crash on malformed stdin."""
    try:
        data = json.loads(raw)
        return data.get("prompt", "") if isinstance(data, dict) else ""
    except Exception:
        return ""


def source_from_hook_stdin(raw: str) -> str:
    """Extracts how the session began (startup|resume|clear|compact) from a
    SessionStart hook's stdin JSON. Safe on any input."""
    try:
        data = json.loads(raw)
        return data.get("source", "") if isinstance(data, dict) else ""
    except Exception:
        return ""


def session_from_hook_stdin(raw: str) -> str:
    """Extracts the host's session id from a hook's stdin JSON (both Claude Code
    and Cursor provide `session_id`). Used by command-type capture hooks so stored
    entries are grouped by session. Safe on any input."""
    try:
        data = json.loads(raw)
        return data.get("session_id", "") if isinstance(data, dict) else ""
    except Exception:
        return ""


def bootstrap_prompt_payload(repo_path: str, prompt: str = "") -> dict:
    """Neutral UserPromptSubmit bootstrap-fallback content. {"status": "", "context": str}.
    Empty context => emit nothing. Logic unchanged from get_bootstrap_context_prompt."""
    repo_path = _hook_cwd_repo(repo_path)
    data = _load(repo_path)
    decisions = [e for e in data.get("entries", []) if e["type"] == "decision"]
    if decisions:
        return {"status": "", "context": ""}
    resume_flag = STORE_DIR / ".resume_mining"
    if resume_flag.exists():
        try:
            flagged = resume_flag.read_text().strip()
        except Exception:
            flagged = ""
        if flagged == repo_path:
            resume_flag.unlink(missing_ok=True)
            return {"status": "", "context": ""}
    level, decisive = _cached_insight(repo_path)
    repo_name = Path(repo_path).name if repo_path else ""
    label = f'"{repo_name}"' if repo_name else "this repo"
    if _is_newcomer_question(prompt):
        # Answer-first at EVERY insight level (deterministic match): a repo question is
        # answered, never met with a menu. The commit signal only tunes the phrasing.
        # Not gated on the offer flag: this path shows no menu, it answers the question.
        lines = [
            "Contexer OVERRIDE — ignore any earlier bootstrap menu instructions for this turn.",
            *_newcomer_answer_block(label, level, decisive),
        ]
    else:
        if _offer_already_made(repo_path):
            # SessionStart (or an earlier prompt) already offered this session. Re-injecting
            # would re-open the picker the developer just dismissed, one modal per prompt.
            return {"status": "", "context": ""}
        _arm_offer(repo_path)
        lines = _build_bootstrap_context(repo_path)
    return {"status": "", "context": "\n".join(lines)}


def get_bootstrap_context_prompt(repo_path: str, prompt: str = "") -> dict:
    """Claude UserPromptSubmit bootstrap-fallback output. Back-compat envelope."""
    from contexer.adapters import claude
    return claude.format_bootstrap_prompt(bootstrap_prompt_payload(repo_path, prompt))


def post_compact_payload(repo_path: str, session_id: str = "") -> dict:
    """Neutral PostCompact content. {"status": str, "context": str}.

    session_id (Retrieval V1 compact-reload parity): "" preserves every existing caller.
    When set, appends the rehydrated content of this session's pre-compaction working set
    (via _rehydrate_working_set — same helper Claude's SessionStart(compact) path uses) so
    Gemini's before_agent reload and Codex's post-compact path get the same rehydration
    Claude gets, instead of losing the router's pre-compaction state on replay."""
    data = _load(repo_path)
    decisions = [e for e in data.get("entries", []) if e["type"] == "decision"]
    if not decisions:
        if _offer_already_made(repo_path):
            return {"status": "", "context": ""}
        _arm_offer(repo_path)
        return {"status": "", "context": "\n".join(_build_bootstrap_context(repo_path))}
    context = get_context(repo_path)
    if session_id:
        rehydrated = _rehydrate_working_set(repo_path, session_id)
        if rehydrated:
            context = f"{context}\n\n{rehydrated}" if context else rehydrated
    return {"status": "Contexer: context reloaded after compaction", "context": context}


def get_post_compact_context(repo_path: str, session_id: str = "") -> dict:
    """Claude/Codex PostCompact output. Back-compat envelope."""
    from contexer.adapters import claude
    return claude.format_post_compact(post_compact_payload(repo_path, session_id))


_RATIONALE_WORDS = frozenset({
    "why", "reason", "rationale", "decision", "decided", "chose", "choice",
    "motivation", "intent", "reasoning", "background", "justif",
    # approach/design questions: "what approach did we take?", "what's the architecture?"
    "approach", "architecture", "tradeoff", "tradeoffs", "constraint", "convention",
})

# Question-shaped prompts: "what does the miner do?", "how does the router pick topics?".
# Deliberately lead-word-only (not a trailing "?") and deliberately without can/does/is —
# "can you add X?" is a task request and must stay silent.
_QUESTION_LEADS = frozenset({"what", "how", "where", "which", "when", "who"})

# Project-context questions: "what is the purpose?", "what is planned?", "what's the goal?"
# "plan" excluded — too ambiguous ("premium plan", "payment plan")
_PROJECT_CONTEXT_WORDS = frozenset({
    "purpose", "goal", "planned", "overview", "scope",
})

_QUERY_STOP_WORDS = frozenset({
    "why", "was", "the", "did", "we", "for", "what", "how", "is", "are",
    "can", "does", "this", "that", "it", "to", "of", "in", "a", "an",
    "and", "or", "but", "not", "with", "at", "by", "from", "reason",
    "rationale", "decision", "decided", "chose", "choice", "about", "have",
    "has", "been", "would", "could", "should", "will", "tell", "explain",
    "know", "me", "you", "do", "our", "my", "your", "them", "they",
    "implement", "implemented", "implementation", "use", "using", "used",
    "build", "built", "create", "created", "add", "added", "make", "made",
    "just", "here", "there", "when", "then", "than", "also", "get",
    "into", "which", "who", "where", "what", "that", "its", "been",
})

# Additional words excluded when deciding if a project-context question is domain-specific.
# "repo" can be a valid search term ("repo pattern") so it stays in the keyword pool for
# rationale searches, but when gating the overview fallback it's treated as generic.
_OVERVIEW_GENERIC_WORDS = frozenset({
    "repo", "codebase", "project", "repository", "app", "service",
})


# ── Retrieval V1: topic router (lexical BM25 index + working set + injection ladder) ──
#
# Topic → alias words. A decision (or prompt) is tagged with a topic when its lowercase
# tokens hit >=1 alias. Derived only — never stored on the entry (the index sidecar owns
# topics). Aliases are the listed words only; the bare topic name is NOT auto-added.
_TOPIC_ALIASES: dict[str, frozenset] = {
    "db": frozenset({"postgres", "postgresql", "mysql", "sqlite", "sql", "migration",
                     "migrations", "schema", "query", "orm", "database", "redis", "mongo"}),
    "api": frozenset({"endpoint", "endpoints", "rest", "route", "routes", "request",
                      "response", "http", "graphql"}),
    # Bare "session"/"sessions" deliberately absent: in agent-tooling repos those
    # words overwhelmingly mean agent sessions, not auth sessions — they mis-tagged
    # documentation questions as auth (observed live 2026-07-15). Genuine auth-session
    # phrasing is caught by _AUTH_SESSION_RE below instead.
    "auth": frozenset({"jwt", "oauth", "login", "token", "tokens"}),
    "frontend": frozenset({"react", "component", "components", "css", "ui", "dom"}),
    "deploy": frozenset({"docker", "kubernetes", "k8s", "ci", "terraform", "helm", "release"}),
    "testing": frozenset({"pytest", "test", "tests", "fixture", "fixtures", "mock", "coverage"}),
    "config": frozenset({"toml", "yaml", "env", "settings"}),
    "perf": frozenset({"cache", "latency", "optimize"}),
    "security": frozenset({"secret", "vulnerability", "sanitize", "injection"}),
}

# BM25 tuning (Robertson/Sparck-Jones defaults — corpus is <=500 short jargon sentences).
_BM25_K1 = 1.5
_BM25_B = 0.75
# Injection ladder — RELATIVE thresholds (never absolute cross-repo scores).
_STRONG_CANDIDATES = 5      # top-k considered for a strong (content) injection
_STRONG_SCORE_FRAC = 0.5    # a candidate is strong only within this fraction of the top score
_STRONG_MIN_HITS = 2        # ...and with at least this many distinct query-term hits
_STRONG_CAP = 3             # never inject more than this many decisions per prompt
_RETRIEVAL_LOG_CAP = 200    # pointer/usage log is tail-capped

# Artifact extraction: signal-rich tokens pulled from a paste even when the prose is empty.
_ARTIFACT_PATH_RE = re.compile(r"[\w./-]+\.(?:py|ts|js|go|rs|md|toml|yaml|json)\b")
_ARTIFACT_DOTTED_RE = re.compile(r"\b[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)+\b")
_ARTIFACT_EXC_RE = re.compile(r"\b[A-Z]\w*(?:Error|Exception)\b")
# Two+ path segments required: a lone slash in prose ("light/dark", "read/write",
# "either/or") is not a route, but "/api/users/{id}" is.
_ARTIFACT_ROUTE_RE = re.compile(r"/[a-z][\w{}-]*(?:/[\w{}-]+)+")


def _index_tokens(text: str) -> list[str]:
    """Lowercase, punctuation-stripped, alnum tokens of length >=3, minus stop words.
    The single tokenization used by both the index and the BM25 query side (distinct from
    the novelty filter's set-based `_tokenize`)."""
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    return [t for t in toks if len(t) >= 3 and t not in _QUERY_STOP_WORDS]


# Compound auth-session phrasing ("invalidate all user sessions") carries no
# surviving auth alias token; this phrase check restores the tag without letting
# bare agent-session vocabulary ("SessionStart runs each session") mean auth.
_AUTH_SESSION_RE = re.compile(r"\b(?:user|login|auth|authenticated) sessions?\b")


def _derive_topics(content: str) -> list[str]:
    """Sorted topics with >=1 alias hit in `content`. Derived, never persisted."""
    low = (content or "").lower()
    toks = set(re.findall(r"[a-z0-9]+", low))
    topics = {t for t, aliases in _TOPIC_ALIASES.items() if toks & aliases}
    if "auth" not in topics and _AUTH_SESSION_RE.search(low):
        topics.add("auth")
    return sorted(topics)


def _index_path(repo_path: str) -> Path:
    return STORE_DIR / f".retrieval_index_{_slug(repo_path)}.json"


def _build_retrieval_index(data: dict) -> dict:
    """Build the BM25 index payload from a loaded store. Indexes only `decision` entries,
    over their CURRENT content."""
    docs: dict[str, dict] = {}
    df: dict[str, int] = {}
    total_len = 0
    for e in data.get("entries", []):
        if e.get("type") != "decision":
            continue
        did = e.get("id")
        if not did:
            continue
        # Index only what get_context can hand back. Ignored decisions are permanently
        # suppressed everywhere, so indexing them lets a top-ranked hit render as
        # nothing and weak pointers advertise unretrievable topics. Pending ones ARE
        # retrievable (get_context renders them with a [pending] tag), so they stay
        # indexed — the legacy fallback surfaces them and the indexed path must too.
        status = _entry_status(e)
        if status == "ignored":
            continue
        content = _current_content(e)
        toks = _index_tokens(content)
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        for t in tf:
            df[t] = df.get(t, 0) + 1
        total_len += len(toks)
        docs[did] = {
            "tf": tf, "len": len(toks), "topics": _derive_topics(content),
            "subtype": e.get("subtype", ""), "status": status,
        }
    n_docs = len(docs)
    avgdl = (total_len / n_docs) if n_docs else 0.0
    return {"v": 1, "n_docs": n_docs, "avgdl": avgdl, "df": df, "docs": docs}


def _write_retrieval_index(repo_path: str, data: dict) -> None:
    """Persist the index sidecar. Fail-soft — a missing index just triggers the legacy
    per-prompt path, never a crash."""
    try:
        STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        _atomic_write(_index_path(repo_path), json.dumps(_build_retrieval_index(data)))
    except OSError:
        pass


def _read_retrieval_index(repo_path: str) -> dict | None:
    """Read the index sidecar. None on missing / corrupt / wrong-version. NEVER rebuilds
    or creates the file (the reader is strictly read-only)."""
    path = _index_path(repo_path)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    if not isinstance(data, dict) or data.get("v") != 1 or not isinstance(data.get("docs"), dict):
        return None
    return data


def _bm25_rank(keywords: list[str], index: dict) -> list[tuple[str, float, int, int]]:
    """BM25-score every indexed doc against `keywords` (which may repeat — repeats raise
    that term's query weight). Returns (decision_id, score, distinct_term_hits,
    discriminative_hits) sorted by score desc. Terms absent from the corpus contribute
    nothing. A hit is *discriminative* when the matched term is rare in this corpus
    (df <= max(2, n_docs // 20)) — the router's junk guard for question-only prompts."""
    import math
    docs = index.get("docs", {})
    df = index.get("df", {})
    n_docs = index.get("n_docs", 0) or 0
    avgdl = index.get("avgdl", 0.0) or 0.0
    if not docs or not keywords:
        return []
    # Query-term weights: a repeated keyword (e.g. a double-weighted artifact) counts twice.
    qweight: dict[str, int] = {}
    for kw in keywords:
        qweight[kw] = qweight.get(kw, 0) + 1
    # Resolve each query term to the corpus token(s) it scores against. An exact df hit maps
    # to itself; a term absent from df expands to every indexed token having it as a prefix
    # (restores legacy \b-prefix matching — 'postgres' must match a doc holding only
    # 'postgresql'). Aggregated df is capped at n_docs so idf stays non-negative.
    resolved: dict[str, tuple[list[str], int]] = {}
    for term in qweight:
        if term in df:
            resolved[term] = ([term], df[term])
            continue
        pref = [t for t in df if t.startswith(term)]
        if pref:
            resolved[term] = (pref, min(sum(df[t] for t in pref), n_docs))
    disc_cap = max(2, n_docs // 20)
    ranked: list[tuple[str, float, int, int]] = []
    for did, doc in docs.items():
        tf = doc.get("tf", {})
        dl = doc.get("len", 0) or 0
        score = 0.0
        hits = 0
        dhits = 0
        for term, w in qweight.items():
            r = resolved.get(term)
            if not r:
                continue
            toks_for, n_t = r
            f = sum(tf.get(t, 0) for t in toks_for)
            if not f:
                continue
            hits += 1
            if n_t <= disc_cap:
                dhits += 1
            idf = math.log(1 + (n_docs - n_t + 0.5) / (n_t + 0.5))
            denom = f + _BM25_K1 * (1 - _BM25_B + _BM25_B * (dl / avgdl if avgdl else 1))
            score += w * idf * (f * (_BM25_K1 + 1) / denom)
        if hits:
            ranked.append((did, score, hits, dhits))
    ranked.sort(key=lambda r: r[1], reverse=True)
    return ranked


def _extract_artifacts(prompt: str) -> list[str]:
    """Signal tokens pulled from a paste: file paths (segmented), dotted module paths,
    CamelCase *Error/*Exception names, and route-shaped strings. Lowercased, len>=3."""
    if not prompt:
        return []
    raw: list[str] = []
    raw += _ARTIFACT_PATH_RE.findall(prompt)
    raw += _ARTIFACT_DOTTED_RE.findall(prompt)
    raw += _ARTIFACT_EXC_RE.findall(prompt)
    raw += _ARTIFACT_ROUTE_RE.findall(prompt)
    out: list[str] = []
    for m in raw:
        for seg in re.split(r"[^a-zA-Z0-9]+", m.lower()):
            if len(seg) >= 3:
                out.append(seg)
    return out


def _ws_path(repo_path: str, session_id: str) -> Path:
    # Hash the session id before embedding: filename-safe for any host-supplied id
    # (no path escape) and collision-free where truncation wasn't (two ids sharing
    # a 32-char prefix must not share a working set).
    safe = hashlib.sha1(session_id.encode("utf-8", "replace")).hexdigest()[:16]
    return STORE_DIR / f".ws_{_slug(repo_path)}_{safe}.json"


def working_set_ids(repo_path: str, session_id: str) -> list[str]:
    """Decision ids already injected this session (fail-soft; [] when no session id)."""
    if not session_id:
        return []
    try:
        data = json.loads(_ws_path(repo_path, session_id).read_text(encoding="utf-8"))
        ids = data.get("injected") if isinstance(data, dict) else None
        return ids if isinstance(ids, list) else []
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return []


def _ws_add(repo_path: str, session_id: str, ids: list[str]) -> None:
    """Record injected ids for this session so they are not re-injected. Skipped (and no
    file created) when session_id is empty — dedup off, still correct."""
    if not session_id or not ids:
        return
    existing = working_set_ids(repo_path, session_id)
    merged = existing + [i for i in ids if i not in existing]
    try:
        STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        _atomic_write(_ws_path(repo_path, session_id),
                      json.dumps({"injected": merged, "ts": time.time()}))
    except OSError:
        pass


def _retrieval_log(repo_path: str, event: dict) -> None:
    """Append one JSON line to the pointer/usage log, tail-capped. Fail-soft."""
    path = STORE_DIR / f".retrieval_{_slug(repo_path)}.jsonl"
    try:
        STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        lines: list[str] = []
        if path.exists():
            lines = path.read_text(encoding="utf-8").splitlines()
        lines.append(json.dumps(event))
        if len(lines) > _RETRIEVAL_LOG_CAP:
            lines = lines[-_RETRIEVAL_LOG_CAP:]
        _atomic_write(path, "\n".join(lines) + "\n")
    except OSError:
        pass


# ── Retrieval V1 Part B: session-start integration (standing map, compact rehydration,
# working-set GC, follow-through logging) ──────────────────────────────────────────────
_STANDING_MAP_MIN_DECISIONS = 20   # below this, a topic map is more noise than signal
_STANDING_MAP_TOP_N = 6            # top-N topics by count shown in the map line
_REHYDRATE_CAP = 10                # most-recently-injected working-set decisions replayed
_WS_GC_AGE_SECONDS = 7 * 24 * 3600  # working-set/log files older than this are stale sessions
_FOLLOWUP_WINDOW_SECONDS = 30 * 60  # a pointer counts as "followed through" within this window


def _standing_topic_map(repo_path: str, decisions: list) -> str:
    """Size-gated one-liner: topic counts from the index sidecar, read-only (never
    rebuilt here). '' below _STANDING_MAP_MIN_DECISIONS stored decisions, or when the
    index isn't readable."""
    if len(decisions) < _STANDING_MAP_MIN_DECISIONS:
        return ""
    index = _read_retrieval_index(repo_path)
    if index is None:
        return ""
    counts: dict[str, int] = {}
    for doc in index.get("docs", {}).values():
        if doc.get("status") not in ("approved", "suggested"):
            continue
        for t in doc.get("topics", []):
            counts[t] = counts.get(t, 0) + 1
    if not counts:
        return ""
    top = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))[:_STANDING_MAP_TOP_N]
    parts = ", ".join(f"{t}({n})" for t, n in top)
    return f"Stored decisions by topic: {parts} — fetch with get_context(query=<topic>)."


def _rehydrate_working_set(repo_path: str, session_id: str) -> str:
    """The content of at most the _REHYDRATE_CAP most-recently-injected working-set
    decisions (current content, active statuses only), under a heading. '' when there is
    no session id / working set / nothing still active to show."""
    ids = working_set_ids(repo_path, session_id)
    if not ids:
        return ""
    recent = ids[-_REHYDRATE_CAP:]
    data = _load(repo_path)
    by_id = {e.get("id"): e for e in data.get("entries", []) if e.get("type") == "decision"}
    lines = []
    for did in recent:
        e = by_id.get(did)
        if not e or _entry_status(e) not in ("approved", "suggested"):
            continue
        subtype_tag = f" [{e['subtype']}]" if e.get("subtype") else ""
        title, body = _title_and_body(e)
        lines.append(f"- [{e['timestamp'][:10]}]{subtype_tag} {title}")
        if body is not None:
            lines.append(f"    {body}")
    if not lines:
        return ""
    return "## Rehydrated working context:\n" + "\n".join(lines)


def _gc_stale_session_files() -> None:
    """At non-resume session start: drop working-set dedup files and retrieval logs whose
    session is well over — old enough that dedup/history no longer matters. Fail-soft,
    a quick glob+mtime check; never touches the retrieval index sidecar (owned by A2)."""
    try:
        cutoff = time.time() - _WS_GC_AGE_SECONDS
        # .bootstrap_offered_* is normally cleared by its own repo's next session start;
        # this catches flags for repos that are never opened again, so they don't accumulate.
        for pattern in (".ws_*.json", ".retrieval_*.jsonl", ".bootstrap_offered_*"):
            for p in STORE_DIR.glob(pattern):
                try:
                    if p.stat().st_mtime < cutoff:
                        p.unlink(missing_ok=True)
                except OSError:
                    continue
    except OSError:
        pass


def _recent_pointer_event(repo_path: str) -> dict | None:
    """Most recent 'pointer' log event for this repo within the follow-through window, or
    None. Read-only — never touches the log. Fail-soft."""
    path = STORE_DIR / f".retrieval_{_slug(repo_path)}.jsonl"
    if not path.exists():
        return None
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    cutoff = time.time() - _FOLLOWUP_WINDOW_SECONDS
    for line in reversed(lines):
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("e") != "pointer":
            continue
        # Lines are appended in order — the first pointer found scanning backwards is the
        # most recent; if it's already outside the window, nothing older qualifies either.
        return event if event.get("ts", 0) >= cutoff else None
    return None


def log_followup_if_matching(repo_path: str, query: str, found: bool = True) -> None:
    """Server-side hook for the get_context tool: log-only, never changes the returned
    context. When a 'pointer' nudge was logged for this repo within the follow-through
    window and `query` matches one of its topics, append a 'followup' event — a deterministic
    usage signal for whether pointers actually get chased. Fail-soft.

    `found`: whether the get_context call actually returned decisions. A no-result query is
    not an honest follow-through, so it is never logged."""
    if not query or not found:
        return
    try:
        event = _recent_pointer_event(repo_path)
        if not event:
            return
        topics = set(event.get("topics", []))
        query_topics = set(_derive_topics(query)) | {query.strip().lower()}
        if topics & query_topics:
            _retrieval_log(repo_path, {"e": "followup", "query": query, "ts": time.time()})
    except Exception:
        pass


def _render_prompt_decisions(repo_path: str, ids: list[str]) -> str:
    """Render the given decisions in the same two-line format `get_context` uses: a bullet
    line ending in the title, then a `    `-indented line with the current content. Skips
    ignored / missing entries; empty string when nothing renders."""
    data = _load(repo_path)
    by_id = {e.get("id"): e for e in data.get("entries", []) if e.get("type") == "decision"}
    stale = _staleness_notes(repo_path, [by_id[d] for d in ids
                                         if d in by_id and _entry_status(by_id[d]) != "ignored"])
    lines: list[str] = []
    for did in ids:
        e = by_id.get(did)
        if not e or _entry_status(e) == "ignored":
            continue
        subtype_tag = f" [{e['subtype']}]" if e.get("subtype") else ""
        st = _entry_status(e)
        status_tag = " [suggested]" if st == "suggested" else " [pending]" if st == "pending_approval" else ""
        entry_id = e.get("id", "")[:8]
        id_tag = f" (id={entry_id})" if entry_id else ""
        title, body = _title_and_body(e)
        lines.append(f"- [{e['timestamp'][:10]}]{subtype_tag}{status_tag}{_recur_suffix(e)} "
                     f"{title}{id_tag}{stale.get(did, '')}")
        if body is not None:
            lines.append(f"    {body}")
    return "\n".join(lines)


# Structured classification of an injection, replacing the old startswith/regex scrape of
# the rendered text (claude.rationale used to reverse-engineer this from the string; now the
# router hands it over directly). "" kind means no injection.
_EMPTY_META = {"kind": "", "count": 0, "topics": []}


def _rendered_meta(kind: str, text: str) -> dict:
    """Count of rendered '- ' lines + derived topics, computed from the SAME text that gets
    injected — matches what the old regex/line-count scrape produced, for kinds "strong"/
    "overview"/"global" (they all rendered through the same get_context()-style line format)."""
    count = sum(1 for line in text.splitlines() if line.startswith("- "))
    return {"kind": kind, "count": count, "topics": _derive_topics(text)}


def _global_prompt_lookup(ordered_kws: list[str]) -> tuple[str, dict]:
    """The global-store fallback shared by the legacy and BM25 router paths."""
    for kw in ordered_kws:
        result = get_global_context(query=kw)
        if "No matching" not in result and "No global context" not in result:
            text = f"[Contexer: auto-fetched from global context]\n{result}"
            return text, _rendered_meta("global", text)
    return "", dict(_EMPTY_META)


def _legacy_prompt_context(repo_path: str, ordered_kws: list[str], is_project: bool) -> tuple[str, dict]:
    """Today's exact per-prompt lookup, preserved verbatim for the index-absent path."""
    data = _load(repo_path)
    if data.get("entries"):
        for kw in ordered_kws:
            result = get_context(repo_path, query=kw)
            if "No matching decisions" not in result and "No context stored" not in result:
                text = f"[Contexer: auto-fetched for this question]\n{result}"
                return text, _rendered_meta("strong", text)

        # Overview fallback: only when the prompt has NO domain-specific keywords beyond
        # the project-context trigger word itself. Generic referential words like "repo",
        # "project", "app" don't count — they just mean "this thing we're discussing."
        if is_project:
            non_project_kws = [
                k for k in ordered_kws
                if k not in _PROJECT_CONTEXT_WORDS and k not in _OVERVIEW_GENERIC_WORDS
            ]
            if not non_project_kws:
                result = get_context(repo_path)
                if "No context stored" not in result:
                    text = f"[Contexer: project context]\n{result}"
                    return text, _rendered_meta("overview", text)

    return _global_prompt_lookup(ordered_kws)


def _get_context_for_prompt(repo_path: str, prompt: str, session_id: str = "") -> tuple[str, dict]:
    """Body of get_context_for_prompt, returning (text, meta). meta = {"kind": "strong"|
    "pointer"|"overview"|"global"|"", "count": int, "topics": [...]} — structured data for
    a caller's status line (claude.rationale) instead of scraping the rendered text."""
    words_raw = [w.strip("?,./!;:\"'()[]") for w in prompt.lower().split()]
    word_set = set(words_raw)

    is_rationale = bool(word_set & _RATIONALE_WORDS)
    is_project = bool(word_set & _PROJECT_CONTEXT_WORDS)
    is_question = bool(words_raw) and words_raw[0] in _QUESTION_LEADS

    # Extract content keywords: alpha-only, length >= 3, not stop words.
    # >= 3 (not > 3) captures short tech terms: jwt, api, sdk, k8s, sql, gcp, aws.
    keywords = [
        w for w in words_raw
        if len(w) >= 3 and w not in _QUERY_STOP_WORDS and w.isalpha()
    ]
    ordered_kws = sorted(set(keywords), key=len, reverse=True)[:3]

    # No index (missing / corrupt / wrong version) → today's EXACT legacy behavior,
    # including the rationale/project gate. The reader never rebuilds the index.
    index = _read_retrieval_index(repo_path)
    if index is None:
        if not is_rationale and not is_project:
            return "", dict(_EMPTY_META)
        return _legacy_prompt_context(repo_path, ordered_kws, is_project)

    # BM25 path. The router fires on rationale/project questions, on artifact-bearing
    # prompts (a stack-trace paste is signal-rich even when the prose names no topic),
    # and on question-shaped prompts ("what does the miner do?" — comprehension questions
    # carry no rationale word yet are exactly what stored context answers). A non-question
    # task prompt with no artifact stays silent, exactly like today. Question-only prompts
    # (no rationale/project word) additionally clear a discriminative-term guard below, so
    # a generic-token question can't drag in whatever decision happens to share a word.
    artifacts = _extract_artifacts(prompt)
    if not is_rationale and not is_project and not artifacts and not is_question:
        return "", dict(_EMPTY_META)

    # BM25 query vector: the SAME tokenizer the index uses (not the legacy alpha-only
    # extraction), so digit-bearing terms like k8s / oauth2 reach the ranker. Artifacts
    # stay double-weighted. The legacy `keywords`/`ordered_kws` are kept for gating and the
    # overview/global fallbacks below — only this vector changes.
    art_tokens = _index_tokens(" ".join(artifacts))
    query_terms = _index_tokens(prompt) + art_tokens + art_tokens   # artifacts double-weighted
    ranked = _bm25_rank(query_terms, index)
    ws = set(working_set_ids(repo_path, session_id))
    ranked = [r for r in ranked if r[0] not in ws]

    if ranked:
        top_score = ranked[0][1]
        # Junk guard: a bare question (no rationale/project word) only earns a content
        # injection when the top-ranked doc matched a DISCRIMINATIVE term — one rare in
        # this corpus. Otherwise "what time is the standup?" would inject the p99-latency
        # constraint on the word "time".
        question_only = is_question and not is_rationale and not is_project
        allow_strong = not question_only or ranked[0][3] >= 1
        strong: list[str] = []
        if allow_strong:
            for did, score, hits, _dh in ranked[:_STRONG_CANDIDATES]:
                if score >= _STRONG_SCORE_FRAC * top_score and hits >= _STRONG_MIN_HITS:
                    strong.append(did)
        # Rationale/project boost: a single-keyword "why X?" / "what's the goal for X?" often
        # yields one doc with one hit — relax to hits>=1 on the top candidate so legacy's
        # full-content recall for both prompt classes is preserved. A bare question gets the
        # same relaxation only when it *is* single-keyword (and hence discriminative per the
        # guard above) — with more keywords, one lone hit is noise, not an answer.
        relax = is_rationale or is_project or (question_only and len(set(query_terms)) == 1)
        if not strong and allow_strong and relax and ranked[0][2] >= 1:
            strong = [ranked[0][0]]
        strong = strong[:_STRONG_CAP]
        if strong:
            rendered = _render_prompt_decisions(repo_path, strong)
            if rendered:
                _ws_add(repo_path, session_id, strong)
                # Suffix (not part of the pinned header prefix): without it the model
                # narrates "I'll pull this from Contexer" and re-fetches what it already has.
                text = ("[Contexer: auto-fetched for this question] "
                        f"(already in context — no get_context call needed)\n{rendered}")
                return text, _rendered_meta("strong", text)

    # WEAK: no strong content, but the prompt's topics overlap not-yet-injected docs →
    # a ~15-token pointer instead of full content.
    prompt_topics = set(_derive_topics(prompt + " " + " ".join(artifacts)))
    if prompt_topics:
        counts: dict[str, int] = {}
        for did, doc in index.get("docs", {}).items():
            if did in ws:
                continue
            for t in set(doc.get("topics", [])) & prompt_topics:
                counts[t] = counts.get(t, 0) + 1
        if counts:
            ordered_topics = sorted(counts, key=lambda t: (-counts[t], t))
            parts = ", ".join(f"{t}({counts[t]})" for t in ordered_topics)
            _retrieval_log(repo_path, {"e": "pointer", "topics": sorted(prompt_topics),
                                       "sid": session_id, "ts": time.time()})
            text = (f"[Contexer] Related stored decisions: {parts} — "
                    f"call get_context(query='{ordered_topics[0]}') if relevant.")
            meta = {"kind": "pointer", "count": sum(counts.values()), "topics": ordered_topics}
            return text, meta

    # Overview + global fallbacks run ONLY for rationale/project prompts — legacy was silent
    # on an artifact-only prompt that produced no strong hit and no pointer, so we stay silent.
    if is_rationale or is_project:
        # Overview fallback stays exactly as today (project questions with no domain keyword).
        if is_project:
            non_project_kws = [
                k for k in ordered_kws
                if k not in _PROJECT_CONTEXT_WORDS and k not in _OVERVIEW_GENERIC_WORDS
            ]
            if not non_project_kws:
                data = _load(repo_path)
                if data.get("entries"):
                    result = get_context(repo_path)
                    if "No context stored" not in result:
                        text = f"[Contexer: project context]\n{result}"
                        return text, _rendered_meta("overview", text)
        # Global-store fallback, identical to the legacy tail.
        return _global_prompt_lookup(ordered_kws)
    return "", dict(_EMPTY_META)


def get_context_for_prompt(repo_path: str, prompt: str, session_id: str = "") -> str:
    """Auto-injected by UserPromptSubmit hook. Returns relevant stored decisions when
    the prompt is a rationale/decision or project-context question. Silent no-op otherwise.
    Searches repo decisions first; falls back to global decisions."""
    return _get_context_for_prompt(repo_path, prompt, session_id)[0]


def get_context_for_prompt_with_meta(repo_path: str, prompt: str, session_id: str = "") -> tuple[str, dict]:
    """Same as get_context_for_prompt but also returns structured metadata about the
    injection — {"kind": ..., "count": int, "topics": [...]} — so a caller (claude.rationale)
    can build a status line without scraping the rendered text."""
    return _get_context_for_prompt(repo_path, prompt, session_id)


def _team_section(repo_path: str, query: str, entry_type: str, *,
                  defer_architecture: bool = False, limit: int = 0) -> str:
    """Formatted team-context block from the C5 cache. Function-level import avoids a
    store <-> team_context cycle. '' when there is no team context (local mode / no cache).

    `defer_architecture`: passed through to `team_context.format_team_section` — only
    `session_start_payload` sets this True, mirroring the local bulk-injection deferral of
    architecture decisions. `get_context`'s JIT fetch never sets it, so an explicit
    `entry_type="architecture"` call always returns full content.

    `limit`: passed through as the team-section render cap override, so `get_context`'s
    caller-supplied `limit` can actually raise the ceiling on a targeted `entry_type` fetch
    (the same fetch the deferred-count pointer tells the model to make) past the default
    `_TEAM_DISPLAY` - otherwise a cache holding more deferred rows than that would still
    truncate on the "for full content" follow-up call."""
    from contexer import team_context
    return team_context.format_team_section(repo_path, query, entry_type,
                                             defer_architecture=defer_architecture, limit=limit)


def _team_section_with_counts(repo_path: str) -> tuple[str, int, int]:
    """(text, raw_count, deferred_count) for `session_start_payload`'s status suffix - all
    three derived from ONE team_context snapshot (`team_context.session_team_section`), so a
    concurrent background refresh or local decision update between separate reads can no
    longer desync the rendered `text` from the counts describing it (previously `_team_
    section`, `_team_count`, and `_team_deferred_count` each reloaded state independently).
    Same function-level import as the other `_team_*` helpers, for the same reason (avoids a
    store <-> team_context cycle)."""
    from contexer import team_context
    return team_context.session_team_section(repo_path, defer_architecture=True)


def _team_display_cap() -> int:
    """The row cap `format_team_section` renders (`team_context._TEAM_DISPLAY`), so the
    status suffix can stay honest about what actually landed in context. Same
    function-level import as `_team_section_with_counts`, for the same reason (avoids a
    store <-> team_context cycle)."""
    from contexer import team_context
    return team_context._TEAM_DISPLAY


def get_context(repo_path: str, query: str = "", entry_type: str = "", limit: int = 0,
                _active_only: bool = False) -> str:
    """Returns stored context for the given repo.

    _active_only: internal flag — when True, exclude pending_approval and ignored entries
    (used by auto-injection paths so only trusted decisions reach the AI automatically).

    Team context (pulled by C5 and cached separately) is appended as its own section so
    the agent reads local (personal) and team decisions together, scope-tagged.
    """
    data = _load(repo_path)
    entries = data.get("entries", [])
    # Only forward `limit` to the team section on a targeted entry_type fetch (the same
    # "explicit type = JIT fetch, not a bulk render" rule _team_section's defer bypass
    # already follows) - an unfiltered get_context() must keep the plain _TEAM_DISPLAY cap.
    team_section = _team_section(repo_path, query, entry_type,
                                 limit=(limit if entry_type else 0))
    if not entries and not team_section:
        return "No context stored for this repository."

    lines = [f"# Context for {repo_path}\n"]

    decisions = [e for e in entries if e["type"] == "decision"]
    # Always exclude ignored decisions — they are permanently suppressed.
    decisions = [d for d in decisions if _entry_status(d) != "ignored"]
    if _active_only:
        decisions = [d for d in decisions if _entry_status(d) in ("approved", "suggested")]

    is_filtered = bool(query or entry_type)
    if entry_type:
        decisions = [d for d in decisions if d.get("subtype", "") == entry_type]

    if query:
        pat = _query_pattern(query)
        matched = [d for d in decisions if _matches_query(pat, d)]
        # Topic-alias retry: a literal miss on a bare topic name (the pointer nudge suggests
        # get_context(query='db')) falls back to any of that topic's alias tokens, so the
        # suggested call actually returns the postgres/alembic decisions instead of nothing.
        if not matched and query.lower() in _TOPIC_ALIASES:
            aliases = _TOPIC_ALIASES[query.lower()]
            matched = [
                d for d in decisions
                if set(re.findall(r"[a-z0-9]+", d.get("content", "").lower())) & aliases
            ]
        decisions = matched

    display_limit = limit if limit > 0 else (_FILTERED_DISPLAY if is_filtered else _UNFILTERED_DISPLAY)

    if decisions:
        filter_note = ""
        if is_filtered:
            parts = []
            if query:
                parts.append(f"query='{query}'")
            if entry_type:
                parts.append(f"type='{entry_type}'")
            filter_note = f" (filtered: {', '.join(parts)})"
        total = len(decisions)
        shown = _keep_top(decisions, display_limit)
        if total > display_limit:
            filter_note += f" — showing {len(shown)} of {total}"
        lines.append(f"## Decisions and context{filter_note}")
        stale = _staleness_notes(repo_path, shown)
        for d in shown:
            subtype_tag = f" [{d['subtype']}]" if d.get("subtype") else ""
            st = _entry_status(d)
            status_tag = " [suggested]" if st == "suggested" else " [pending]" if st == "pending_approval" else ""
            update_tag = " [update pending approval]" if d.get("proposed_revision") else ""
            entry_id = d.get("id", "")[:8]
            id_tag = f" (id={entry_id})" if entry_id else ""
            title, body = _title_and_body(d)
            lines.append(f"- [scope=personal] [{d['timestamp'][:10]}]{subtype_tag}{status_tag}"
                         f"{update_tag}{_recur_suffix(d)} {title}{id_tag}{stale.get(d.get('id'), '')}")
            if body is not None:
                lines.append(f"    {body}")
        lines.append(
            "\nIf the current task conflicts with any of these decisions, "
            "surface the conflict and confirm with the developer before proceeding."
        )
        lines.append("")
    elif is_filtered:
        parts = []
        if query:
            parts.append(f"query='{query}'")
        if entry_type:
            parts.append(f"type='{entry_type}'")
        lines.append(f"No matching decisions found ({', '.join(parts)}).")

    if team_section:
        lines.append(team_section)
    return "\n".join(lines)


def _infer_purpose(name: str, readme_summary: str) -> str:
    """Derive a concrete purpose assumption from project name and README first line."""
    if readme_summary:
        return readme_summary
    if not name:
        return "Purpose not yet documented"
    n = name.lower()
    if any(w in n for w in ["api", "server", "service", "backend"]):
        return f"Backend API or service (\"{name}\")"
    if any(w in n for w in ["cli", "tool", "cmd"]):
        return f"CLI tool (\"{name}\")"
    if any(w in n for w in ["bot", "agent"]):
        return f"Bot or agent (\"{name}\")"
    if any(w in n for w in ["worker", "job", "queue", "task"]):
        return f"Background worker or job processor (\"{name}\")"
    if any(w in n for w in ["web", "app", "ui", "front", "dashboard"]):
        return f"Web app or frontend (\"{name}\")"
    if any(w in n for w in ["lib", "sdk", "package", "plugin"]):
        return f"Library or SDK (\"{name}\")"
    return f"\"{name}\" — type not obvious from name alone"


def bootstrap_scan(repo_path: str, insight: str = "", mined: list | None = None) -> dict:
    """mined: convention/pattern items already measured by miner.mine_conventions (see
    bootstrap_apply). None (all direct callers) behaves exactly like [] — no suppression -
    so this stays backward-compatible for every caller that doesn't pass it."""
    mined = mined or []
    if insight in _INSIGHT_ORDER:
        insight_source, decisive = "user", True
    else:
        insight, decisive = _cached_insight(repo_path)
        insight_source = "auto"
    root = Path(repo_path)
    data = _load(repo_path)
    existing = [e for e in data.get("entries", []) if e["type"] == "decision"]
    inferred: list[str] = []
    found_files: list[str] = []
    all_deps: set[str] = set()

    # signals used only for question generation — not stored as inferred facts
    sig: dict = {
        "project_name": "",
        "readme_summary": "",
        "has_tests": False,
        "has_ci": False,
        "has_container": False,
        "has_infra": False,
        "has_security_sensitive": False,  # auth or payment deps detected
        "cloud_detected": "",             # "AWS" | "GCP" | "Azure" | ""
        "is_simple_repo": False,          # portfolio, docs-only, learning — suppress infra/CI/test gaps
    }

    _SIMPLE_REPO_SIGNALS = frozenset({
        "portfolio", "showcase", "interview", "submission", "assignment", "homework",
        "course", "tutorial", "example", "demo", "learning", "experiment", "practice",
        "challenge", "exercises", "playground", "kata", "advent",
    })

    def _add(fact: str) -> None:
        proxy = [{"content": f} for f in inferred]
        if _is_novel(fact, existing + proxy):
            inferred.append(fact)

    def _gap(assumption: str, question: str, hint: str, subtype: str = "architecture",
             min_insight: str = "high") -> dict:
        return {"assumption": assumption, "question": question, "hint": hint,
                "subtype": subtype, "min_insight": min_insight}

    def _has_dep(*names: str) -> bool:
        return any(n in dep for n in names for dep in all_deps)

    # --- Python ---
    pyproject_path = root / "pyproject.toml"
    if pyproject_path.exists():
        found_files.append("pyproject.toml")
        try:
            with open(pyproject_path, "rb") as f:
                pyp = tomllib.load(f)
            proj = pyp.get("project", {})
            name, py_req = proj.get("name", ""), proj.get("requires-python", "")
            if name:
                sig["project_name"] = name
            _add(f"Python project{f' \"{name}\"' if name else ''}{f', requires-python {py_req}' if py_req else ''}")
            tool = pyp.get("tool", {})
            if "pytest" in tool:
                _add("Test framework: pytest")
                sig["has_tests"] = True
            if "ruff" in tool:
                _add("Linting/formatting: ruff")
            if "mypy" in tool:
                _add("Type checking: mypy")
            raw: list[str] = list(proj.get("dependencies", []))
            for group in pyp.get("dependency-groups", {}).values():
                raw.extend(d for d in group if isinstance(d, str))
            for extra in proj.get("optional-dependencies", {}).values():
                raw.extend(extra)
            for dep in raw:
                normalized = re.split(r"[>=<!~\[\s;]", dep.strip())[0].lower().replace("_", "-")
                all_deps.add(normalized)
        except Exception:
            pass

    if (root / "uv.lock").exists():
        found_files.append("uv.lock")
        _add("Package manager: uv")

    # --- Node / JS ---
    pkg_json_path = root / "package.json"
    if pkg_json_path.exists():
        found_files.append("package.json")
        try:
            pkg = json.loads(pkg_json_path.read_text())
            name = pkg.get("name", "")
            if name and not sig["project_name"]:
                sig["project_name"] = name
            node_ver = pkg.get("engines", {}).get("node", "")
            parts = [f"Node.js project \"{name}\"" if name else "Node.js project"]
            if node_ver:
                parts.append(f"requires Node {node_ver}")
            _add(", ".join(parts))
            mgr = pkg.get("packageManager", "")
            if mgr:
                _add(f"Package manager: {mgr.split('@')[0]}")
            node_deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
            all_deps.update(k.lower() for k in node_deps)
            if pkg.get("workspaces"):
                _add("Monorepo: npm/yarn workspaces")
            if "typescript" in node_deps:
                _add("Language: TypeScript")
            for fw in ["next", "nuxt", "remix", "svelte", "react", "vue", "express", "fastify", "hono", "elysia"]:
                if fw in node_deps:
                    _add(f"Framework: {fw}")
                    break
            test_cmd = pkg.get("scripts", {}).get("test", "")
            if "jest" in test_cmd or "jest" in node_deps:
                _add("Test framework: Jest")
                sig["has_tests"] = True
            elif "vitest" in test_cmd or "vitest" in node_deps:
                _add("Test framework: Vitest")
                sig["has_tests"] = True
        except Exception:
            pass

    # --- Go ---
    if (root / "go.mod").exists():
        found_files.append("go.mod")
        try:
            for line in (root / "go.mod").read_text().splitlines():
                if line.startswith("module "):
                    _add(f"Go module: {line.split()[1]}")
                elif line.startswith("go "):
                    _add(f"Go version: {line.split()[1]}")
                    break
        except Exception:
            pass

    # --- Rust ---
    if (root / "Cargo.toml").exists():
        found_files.append("Cargo.toml")
        try:
            with open(root / "Cargo.toml", "rb") as f:
                c = tomllib.load(f)
            p = c.get("package", {})
            if p.get("name") and not sig["project_name"]:
                sig["project_name"] = p["name"]
            rust_name = f' "{p["name"]}"' if p.get("name") else ""
            rust_edition = f', edition {p["edition"]}' if p.get("edition") else ""
            _add(f"Rust project{rust_name}{rust_edition}")
        except Exception:
            pass

    # --- Monorepo ---
    for mf in ["nx.json", "turbo.json", "lerna.json", "pnpm-workspace.yaml"]:
        if (root / mf).exists():
            found_files.append(mf)
            _add(f"Monorepo: {mf.split('.')[0]} workspace")
            break
    if not any("Monorepo" in i for i in inferred):
        if (root / "packages").is_dir() or (root / "apps").is_dir():
            _add("Monorepo: packages/ or apps/ directory structure")

    # --- Data layer ---
    _DB_MAP = {
        "PostgreSQL": {"psycopg", "psycopg2", "asyncpg", "pg", "postgres", "neon"},
        "MySQL/MariaDB": {"pymysql", "aiomysql", "mysql2", "mysql"},
        "MongoDB": {"pymongo", "motor", "mongodb", "mongoose"},
        "Redis": {"redis", "aioredis", "ioredis"},
        "SQLite": {"aiosqlite", "better-sqlite3"},
    }
    _ORM_DEPS = {"sqlalchemy", "tortoise-orm", "databases", "prisma", "drizzle-orm",
                 "typeorm", "sequelize", "knex", "mikro-orm"}
    detected_db = [label for label, names in _DB_MAP.items() if _has_dep(*names)]
    if detected_db:
        _add(f"Data store(s): {', '.join(detected_db)}")
    detected_orm = next((d for d in _ORM_DEPS if _has_dep(d)), None)
    if detected_orm:
        _add(f"ORM / query builder: {detected_orm}")

    # --- Auth / payments (security-sensitive signals) ---
    _AUTH_JWT = {"python-jose", "pyjwt", "jose"}
    _AUTH_FRAMEWORK = {"passlib", "authlib", "passport", "next-auth", "@auth", "clerk",
                       "supabase", "firebase-admin", "google-auth", "python-keycloak"}
    _PAYMENT_DEPS = {"stripe", "braintree"}
    if _has_dep(*_AUTH_JWT):
        _add("Auth: JWT-based (pyjwt / python-jose detected)")
        sig["has_security_sensitive"] = True
    elif _has_dep(*_AUTH_FRAMEWORK):
        pkg_found = next((d for d in _AUTH_FRAMEWORK if _has_dep(d)), "unknown")
        _add(f"Auth: {pkg_found} detected")
        sig["has_security_sensitive"] = True
    if _has_dep(*_PAYMENT_DEPS):
        sig["has_security_sensitive"] = True

    # --- Cloud SDKs ---
    if _has_dep("boto3", "botocore", "aws-cdk", "@aws-sdk", "aws-lambda"):
        _add("Cloud: AWS SDK present (boto3 / @aws-sdk)")
        sig["cloud_detected"] = sig["cloud_detected"] or "AWS"
    if _has_dep("google-cloud", "@google-cloud", "google-auth"):
        _add("Cloud: GCP SDK present")
        sig["cloud_detected"] = sig["cloud_detected"] or "GCP"
    if _has_dep("azure-", "@azure"):
        _add("Cloud: Azure SDK present")
        sig["cloud_detected"] = sig["cloud_detected"] or "Azure"

    # --- External integrations ---
    _INTEGRATIONS = {
        "stripe": "Payments: Stripe", "braintree": "Payments: Braintree",
        "sendgrid": "Email: SendGrid", "resend": "Email: Resend",
        "twilio": "Messaging: Twilio",
        "openai": "AI: OpenAI SDK", "anthropic": "AI: Anthropic SDK", "langchain": "AI: LangChain",
        "celery": "Task queue: Celery", "dramatiq": "Task queue: Dramatiq",
        "kafka-python": "Messaging: Kafka", "confluent-kafka": "Messaging: Kafka (Confluent)",
        "pika": "Messaging: RabbitMQ", "aio-pika": "Messaging: RabbitMQ (async)",
        "elasticsearch-py": "Search: Elasticsearch", "typesense": "Search: Typesense",
    }
    for dep, label in _INTEGRATIONS.items():
        if _has_dep(dep):
            _add(label)

    # --- CI/CD ---
    gh_wf = root / ".github" / "workflows"
    if gh_wf.is_dir():
        wfs = list(gh_wf.glob("*.yml")) + list(gh_wf.glob("*.yaml"))
        if wfs:
            found_files.append(".github/workflows/")
            _add(f"CI/CD: GitHub Actions ({len(wfs)} workflow file(s))")
            sig["has_ci"] = True
    if (root / ".gitlab-ci.yml").exists():
        found_files.append(".gitlab-ci.yml")
        _add("CI/CD: GitLab CI")
        sig["has_ci"] = True

    # --- Docker ---
    if (root / "Dockerfile").exists():
        found_files.append("Dockerfile")
        try:
            first_from = next(
                (l.split()[1] for l in (root / "Dockerfile").read_text().splitlines() if l.startswith("FROM")), None
            )
            _add(f"Containerized — Dockerfile present{f' (base: {first_from})' if first_from else ''}")
        except Exception:
            _add("Containerized — Dockerfile present")
        sig["has_container"] = True
    for compose in ["docker-compose.yml", "docker-compose.yaml"]:
        if (root / compose).exists():
            found_files.append(compose)
            _add("Local dev: docker-compose present")
            break

    # --- Linting / formatting ---
    eslint_files = [".eslintrc", ".eslintrc.js", ".eslintrc.json", ".eslintrc.cjs",
                    "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs"]
    if any((root / f).exists() for f in eslint_files):
        found_files.append(".eslintrc*")
        _add("Linting: ESLint")
    prettier_files = [".prettierrc", ".prettierrc.json", ".prettierrc.js",
                      ".prettierrc.cjs", "prettier.config.js"]
    if any((root / f).exists() for f in prettier_files):
        found_files.append(".prettierrc*")
        _add("Formatting: Prettier")
    if (root / "ruff.toml").exists():
        found_files.append("ruff.toml")
        _add("Linting/formatting: ruff (ruff.toml)")
    if (root / "pytest.ini").exists():
        found_files.append("pytest.ini")
        _add("Test framework: pytest (pytest.ini)")
        sig["has_tests"] = True

    # --- Infrastructure ---
    if list(root.glob("*.tf")) or (root / "terraform").is_dir():
        _add("Infrastructure as code: Terraform")
        sig["has_infra"] = True
    if any((root / d).is_dir() for d in ["k8s", "kubernetes", "helm"]):
        _add("Deployment: Kubernetes (manifests or Helm charts present)")
        sig["has_infra"] = True

    # --- Architecture signals ---
    src = root / "src"
    if src.is_dir():
        layers = [d for d in ["api", "services", "models", "controllers", "middleware", "handlers", "repositories"]
                  if (src / d).is_dir()]
        if layers:
            layer_str = ", ".join(layers[:3]) + ("..." if len(layers) > 3 else "")
            _add(f"Architecture: layered structure detected (src/{layer_str})")

    # --- README summary (for purpose inference) ---
    readme = root / "README.md"
    if readme.exists():
        found_files.append("README.md")
        try:
            text = readme.read_text(errors="ignore")
            lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
            if lines:
                sig["readme_summary"] = lines[0][:120]
            if any(w in text.lower()[:2000] for w in _SIMPLE_REPO_SIGNALS):
                sig["is_simple_repo"] = True
        except Exception:
            pass
    # CLAUDE.md / .cursorrules / docs/ — read for purpose hints before asking questions
    _CONTEXT_FILES = ["CLAUDE.md", ".cursorrules", ".windsurfrules"]
    for cf in _CONTEXT_FILES:
        cf_path = root / cf
        if cf_path.exists():
            found_files.append(cf)
            try:
                cf_text = cf_path.read_text(errors="ignore")[:3000]
                # Extract first meaningful non-heading line as summary if README had none
                if not sig["readme_summary"]:
                    lines = [l.strip() for l in cf_text.splitlines()
                             if l.strip() and not l.startswith("#") and len(l.strip()) > 20]
                    if lines:
                        sig["readme_summary"] = lines[0][:120]
                if any(w in cf_text.lower() for w in _SIMPLE_REPO_SIGNALS):
                    sig["is_simple_repo"] = True
            except Exception:
                pass

    docs_dir = root / "docs"
    if docs_dir.is_dir():
        found_files.append("docs/")
        # Scan first doc file for purpose hints
        for doc in sorted(docs_dir.glob("*.md"))[:3]:
            try:
                doc_text = doc.read_text(errors="ignore")[:1500]
                if not sig["readme_summary"]:
                    lines = [l.strip() for l in doc_text.splitlines()
                             if l.strip() and not l.startswith("#") and len(l.strip()) > 20]
                    if lines:
                        sig["readme_summary"] = lines[0][:120]
                if any(w in doc_text.lower() for w in _SIMPLE_REPO_SIGNALS):
                    sig["is_simple_repo"] = True
            except Exception:
                pass

    # Repos with no build/package config and no inferred stack facts are docs-only
    has_code_config = any([
        (root / "pyproject.toml").exists(), (root / "package.json").exists(),
        (root / "go.mod").exists(), (root / "Cargo.toml").exists(),
    ])
    if not has_code_config and not inferred:
        sig["is_simple_repo"] = True

    # --- Primary stack detection for stack-aware hints ---
    primary_stack = (
        "python" if any("Python" in i for i in inferred) else
        "node"   if any("Node.js" in i or "TypeScript" in i for i in inferred) else
        "go"     if any("Go module" in i or "Go version" in i for i in inferred) else
        "rust"   if any("Rust" in i for i in inferred) else
        "generic"
    )

    def _test_hint() -> str:
        if primary_stack == "python":
            return "e.g. pytest with fixtures and coverage threshold; no mocking external calls in unit tests"
        if primary_stack == "node":
            return "e.g. Jest or Vitest; 80% coverage threshold; no real HTTP calls in unit tests"
        if primary_stack == "go":
            return "e.g. go test, table-driven tests; benchmarks for hot paths"
        if primary_stack == "rust":
            return "e.g. cargo test; #[cfg(test)] modules; integration tests in tests/"
        return "e.g. unit tests, integration tests, coverage threshold"

    def _exclusions_hint() -> str:
        if primary_stack == "python":
            return "e.g. 'no requests, use httpx'; 'no Flask, FastAPI only'; 'always type-annotate public APIs'"
        if primary_stack == "node":
            return "e.g. 'no CommonJS, ESM only'; 'no lodash, use native'; 'no class-based components'"
        if primary_stack == "go":
            return "e.g. 'no global state'; 'always wrap errors with fmt.Errorf'; 'no init() functions'"
        if primary_stack == "rust":
            return "e.g. 'no unwrap() in production code'; 'async with tokio only'; 'no unsafe blocks'"
        return "e.g. specific libraries to avoid, patterns to always follow, things that must never happen"

    def _constraints_hint() -> str:
        if sig["has_security_sensitive"] and sig["cloud_detected"]:
            return f"e.g. GDPR / PCI-DSS compliance; {sig['cloud_detected']} cost ceiling; latency SLA"
        if sig["has_security_sensitive"]:
            return "e.g. GDPR, PCI-DSS, SOC2, HIPAA; audit logging requirements; data residency"
        if sig["cloud_detected"]:
            return f"e.g. {sig['cloud_detected']} cost ceiling; latency SLA; multi-region requirements"
        return "e.g. <100ms p99 latency; 1M+ concurrent users; GDPR; monthly cost ceiling"

    # --- Intent gaps: conditional on signals, filtered by user insight ---
    gaps: list[dict] = []
    name = sig["project_name"]
    user_rank = _INSIGHT_ORDER[insight]

    # Goal — anyone can answer what *they* plan to do; irrelevant for repo authors
    if user_rank < _INSIGHT_ORDER["high"]:
        gaps.append(_gap(
            assumption=_infer_purpose(name, sig["readme_summary"]),
            question="What are you planning to do with this repo?",
            hint="e.g. evaluating it, learning the codebase, fixing a specific bug, integrating it into another project",
            subtype="architecture",
            min_insight="low",
        ))

    # Purpose — can never be inferred from code; first-timers can't answer it either
    gaps.append(_gap(
        assumption=_infer_purpose(name, sig["readme_summary"]),
        question="What does this repo do and who uses it?",
        hint=(
            f"e.g. what {name} is for and who uses it"
            if name else
            "e.g. 'REST API for internal task management, used by 3 frontend apps'"
        ),
        subtype="architecture",
        min_insight="medium",
    ))

    is_simple = sig["is_simple_repo"]

    # Tests — only if no test framework detected AND not a simple/docs repo AND the
    # miner didn't already measure a test convention (asking would be redundant).
    # Layout-only evidence ("Tests live in tests/") isn't enough — ad-hoc test files
    # don't answer whether testing is in scope. Require a measured style/framework
    # signal (assert-style dominance or fixtures) before skipping the question.
    mined_tests = any("test functions" in m.get("content", "")
                      or "Pytest fixtures" in m.get("content", "") for m in mined)
    if not sig["has_tests"] and not is_simple and not mined_tests:
        gaps.append(_gap(
            assumption="No automated test framework detected",
            question="Is automated testing in scope?",
            hint=_test_hint(),
            subtype="convention",
        ))

    # CI — only if no CI config found AND not a simple/docs repo AND the miner didn't
    # already measure the CI pipeline commands.
    mined_ci = any(m.get("content", "").startswith("CI runs:") for m in mined)
    if not sig["has_ci"] and not is_simple and not mined_ci:
        gaps.append(_gap(
            assumption="No CI/CD config found in this repo",
            question="Is there a build or deploy pipeline, or is one planned?",
            hint="e.g. GitHub Actions, GitLab CI, CircleCI; or: manual deploys, not needed yet",
            subtype="convention",
        ))

    # Deployment — only if no container or infra config AND not a simple/docs repo
    if not sig["has_container"] and not sig["has_infra"] and not is_simple:
        gaps.append(_gap(
            assumption="No container or infra config found — deployment target unclear",
            question="Where does this run, or is it local-only?",
            hint="e.g. containerized VPS, serverless function, internal CLI, local-only tool, not deployed yet",
            subtype="architecture",
        ))

    # Cloud SDK but no deploy config — probably in a separate repo
    if sig["cloud_detected"] and not sig["has_container"] and not sig["has_infra"]:
        gaps.append(_gap(
            assumption=f"{sig['cloud_detected']} SDK detected but no deploy config found here",
            question=f"Is the {sig['cloud_detected']} deploy config in a separate repo?",
            hint="e.g. separate infra repo, serverless framework config, or not yet set up",
            subtype="architecture",
        ))

    # Compliance — only if auth or payment deps detected
    if sig["has_security_sensitive"]:
        gaps.append(_gap(
            assumption="Auth or payment handling detected — compliance requirements unknown",
            question="Any compliance or security requirements given the auth/payment handling?",
            hint="e.g. GDPR, PCI-DSS, SOC2, HIPAA; internal security policy; audit logging; data residency",
            subtype="constraint",
        ))

    # Team conventions — only if architecture signals suggest a team wrote this AND the
    # miner didn't already surface >=3 conventions (the developer corrects those at
    # review instead of dictating team norms upfront).
    has_team_signals = (
        any("Architecture" in i or "layered" in i for i in inferred) or
        len(inferred) > 5
    )
    # Config facts (line length, hook ids) don't answer branching/PR/ownership norms —
    # only measured source conventions ("% of N ...") show how the team actually works.
    mined_source_convs = sum(1 for m in mined if "% of" in m.get("content", ""))
    if has_team_signals and not is_simple and mined_source_convs < 3:
        gaps.append(_gap(
            assumption="Team conventions not captured in config files",
            question="Any branching model, PR process, or unwritten norms beyond what's in config files?",
            hint="e.g. trunk-based vs feature branches; PR review requirements; who owns which area",
            subtype="convention",
        ))

    # Exclusions — only if dep tree suggests architectural choices were made
    has_dep_choices = len(all_deps) > 5 or bool(detected_orm) or len(detected_db) > 0
    if has_dep_choices and not is_simple:
        gaps.append(_gap(
            assumption="No known intentional library exclusions or architectural mandates",
            question="Any libraries or patterns that are intentionally excluded or always required?",
            hint=_exclusions_hint(),
            subtype="constraint",
        ))

    # Constraints — only if production signals exist
    has_production_signals = (
        sig["has_security_sensitive"] or sig["cloud_detected"] or
        sig["has_infra"] or sig["has_container"]
    )
    if has_production_signals:
        gaps.append(_gap(
            assumption="No known performance, scale, or compliance constraints",
            question="Any constraints that shape technical decisions?",
            hint=_constraints_hint(),
            subtype="constraint",
        ))

    # Validation placement and error handling gaps are DELETED (not merely suppressed):
    # bootstrap_apply's mining pass now measures actual error-handling conventions
    # (custom exception classes, bare-except rate) straight from the source, so asking
    # is no longer needed here at all.

    # Interview floor for repo authors: signal-conditional gaps collapse to almost
    # nothing on simple repos (no config to scan), but 'full' is an explicit opt-in
    # to an interview — the author's head holds decisions no scan can reach. Floor
    # dropped from 4 to 3: the generic "conventions" filler below is redundant once
    # the miner has actually measured conventions, so one fewer filler is needed to
    # reach a healthy minimum.
    if user_rank == _INSIGHT_ORDER["high"] and len(gaps) < 3:
        interview = [
            _gap(
                assumption="Non-obvious decisions exist only in the author's head",
                question="What decisions shaped this code that aren't visible in it — libraries chosen over alternatives, approaches rejected, structure?",
                hint="e.g. 'argparse over click to avoid deps'; 'rejected async — overkill here'",
                subtype="architecture",
            ),
        ]
        if not mined:
            # Only ask the generic conventions question when nothing was measured —
            # once mined conventions exist, the developer corrects those at review
            # instead of dictating conventions upfront through this filler.
            interview.append(_gap(
                assumption="No coding or workflow conventions captured",
                question="Any conventions future sessions should respect — naming, structure, commit style, how you like code written?",
                hint="e.g. 'single file until it hurts'; 'conventional commits'; 'comments only for why'",
                subtype="convention",
            ))
        interview.append(_gap(
            assumption="No working rules for Claude captured",
            question="Any rules for how Claude should work in this repo — always do, never touch, check before changing?",
            hint="e.g. 'always run tests before commit'; 'never edit data/'; 'ask before adding deps'",
            subtype="constraint",
        ))
        gaps.extend(interview[:3 - len(gaps)])

    gaps = [g for g in gaps if user_rank >= _INSIGHT_ORDER[g["min_insight"]]]
    return {
        "inferred": inferred,
        "gaps": gaps,
        "existing_context_files": found_files,
        "insight": insight,
        "insight_source": insight_source,
        "decisive": decisive,
    }


def bootstrap_apply(repo_path: str, session_id: str, insight: str = "") -> dict:
    """Scan + mine + persist in one step: bootstrap_scan's read-only preview, made
    idempotent and self-storing. This is the core-wiring entrypoint bootstrap_context
    calls by default (apply=True) so a bootstrap actually writes something instead of
    only ever returning a preview.

    Stores exactly ONE consolidated "Stack: ..." decision for all inferred repo facts
    (never one entry per fact — that would flood the store with ~15 near-useless
    entries for a single scan) plus one decision per measured convention/pattern from
    miner.mine_conventions, tier-gated: high tier is measured strongly enough to be
    born approved (created_by='scan' already classifies auto -> approved via
    _classify_level); medium tier is 'pending_approval' — NOT 'suggested', because
    suggested entries inject at session start (merely tagged) and never surface in
    review_pending, which is the opposite of what a 60-89% signal deserves: held out
    of every session until the developer ratifies it in `contexer review`.
    Mined items are skip-don't-bump on dedup — re-deriving the same measurement on a
    later call is not an independent rediscovery, so occurrence_count is left alone."""
    from contexer import miner              # function-level: mirrors _team_section's
                                              # cycle-avoidance style used elsewhere here.
    with _store_lock(_slug(repo_path)):
        mined = miner.mine_conventions(repo_path)
        result = bootstrap_scan(repo_path, insight, mined=mined)
        data = _load(repo_path)
        decisions = [e for e in data["entries"] if e["type"] == "decision"]

        skipped = 0
        changed = False
        # Ids of entries appended this call, by born status. Counts are derived from
        # the post-trim survivors: near MAX_ENTRIES, _keep_top can evict a fresh entry
        # (pin_last protects only the final one), and reporting an evicted entry as
        # "stored" would be a lie.
        new_approved: list[str] = []
        new_pending: list[str] = []

        # Consolidated stack entry — one sentence for every inferred fact, truncated to
        # 400 chars at a "; " boundary so a dependency-heavy repo can't blow past a
        # sane entry size.
        if result["inferred"]:
            sentence = "Stack: " + "; ".join(result["inferred"])
            if len(sentence) > 400:
                cut = sentence.rfind("; ", 0, 400)
                sentence = sentence[:cut] if cut > 0 else sentence[:400]
            if _find_match(sentence, decisions) is None:
                entry = _new_decision_entry(sentence, session_id, "architecture", created_by="scan")
                data["entries"].append(entry)
                decisions.append(entry)
                new_approved.append(entry["id"])
                changed = True
            else:
                skipped += 1

        # Mined conventions/patterns, one decision each. Appended to both `data["entries"]`
        # and the local `decisions` list so later items in this same batch dedup against
        # earlier ones (e.g. two near-identical mined stats never both get stored).
        for item in mined:
            if _find_match(item["content"], decisions) is not None:
                skipped += 1
                continue
            status = "" if item["tier"] == "high" else "pending_approval"
            entry = _new_decision_entry(item["content"], session_id, item["subtype"],
                                        created_by="scan", status=status)
            data["entries"].append(entry)
            decisions.append(entry)
            changed = True
            if item["tier"] == "high":
                new_approved.append(entry["id"])
            else:
                new_pending.append(entry["id"])

        stored, pending = len(new_approved), len(new_pending)
        if changed:
            data["entries"] = _keep_top(data["entries"], MAX_ENTRIES, pin_last=True)
            surviving = {e["id"] for e in data["entries"]}
            stored = sum(1 for i in new_approved if i in surviving)
            pending = sum(1 for i in new_pending if i in surviving)
            _save(repo_path, data)
            if pending:
                _touch_pending_review(repo_path)  # medium-tier items await review (after save)

    return {**result, "stored": stored, "pending": pending, "skipped": skipped}
