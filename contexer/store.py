import contextlib
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import tomllib
import uuid
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl                       # POSIX advisory file locks (macOS/Linux)
except ImportError:                    # pragma: no cover - non-POSIX fallback
    fcntl = None

STORE_DIR = Path.home() / ".contexer"
MAX_ENTRIES = 500
GLOBAL_SLUG = "_global"           # reserved slug for cross-repo decisions
_UNFILTERED_DISPLAY = 10          # entries shown when no query/type filter applied
_FILTERED_DISPLAY = 25            # entries shown when a filter is active


# Directories that must never be treated as a repo. A poisoned .current_repo pointing at
# a tool's config dir (e.g. ~/.claude) would otherwise slug into its own store file and
# silently swallow decisions made in the real project. Guarded on both read and write.
def _config_dirs() -> set[str]:
    home = Path.home()
    return {str(home), str(home / ".claude"), str(home / ".cursor"),
            str(home / ".codex"), str(home / ".gemini"),
            str(home / ".contexer"), str(home / ".config")}


def _is_sane_repo(path: str) -> bool:
    """A usable repo path: non-empty, absolute, and not a tool config / home directory."""
    if not path:
        return False
    p = path.strip()
    if not p or not os.path.isabs(p):
        return False
    return os.path.normpath(p) not in _config_dirs()


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


def _current_repo_path() -> str:
    path = STORE_DIR / ".current_repo"
    if path.exists():
        val = path.read_text().strip()
        return val if _is_sane_repo(val) else ""
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
    STORE_DIR.mkdir(mode=0o700, exist_ok=True)
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
    STORE_DIR.mkdir(mode=0o700, exist_ok=True)
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


def update_global_decision(content: str, session_id: str, subtype: str = "") -> tuple[bool, str | None]:
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
        entry = _new_decision_entry(content, session_id, subtype, status="approved")
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
        decisions = [d for d in decisions if pat.search(d.get("content", ""))]

    display_limit = limit if limit > 0 else (_FILTERED_DISPLAY if is_filtered else _UNFILTERED_DISPLAY)
    lines = ["# Global context (applies to all repos)\n"]

    if decisions:
        total = len(decisions)
        shown = _keep_top(decisions, display_limit)
        filter_note = f" — showing {len(shown)} of {total}" if total > display_limit else ""
        lines.append(f"## Global decisions{filter_note}")
        for d in shown:
            subtype_tag = f" [{d['subtype']}]" if d.get("subtype") else ""
            lines.append(f"- [{d['timestamp'][:10]}]{subtype_tag}{_recur_suffix(d)} {d['content']}")
    elif is_filtered:
        lines.append("No matching global decisions found.")

    return "\n".join(lines)


_PUNCT_RE = re.compile(r"[^\w\s]")

def _tokenize(text: str) -> set[str]:
    """Lowercase, strip punctuation, split on whitespace. Fixes comma-attached tokens."""
    return set(_PUNCT_RE.sub("", text.lower()).split())


def _query_pattern(query: str) -> "re.Pattern":
    """Case-insensitive search pattern for a user query. A leading `\\b` is added only
    when the query starts with a word char — prepending it before `.`, `@`, `#` would
    never fire, so queries like `.env` / `@auth` / `#deploy` silently matched nothing."""
    q = query.lower()
    prefix = r"\b" if q[:1].isalnum() else ""
    return re.compile(prefix + re.escape(q), re.IGNORECASE)


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
        if len(tokens & other) / hi > 0.7:
            return entry
    return None


def _is_storable(content: str) -> bool:
    """Content needs at least one real token to be a storable decision. Punctuation-
    or whitespace-only content is rejected — this preserves the pre-refactor behavior
    where empty-token content was treated as non-novel and never stored."""
    return bool(_tokenize(content))


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
        key=lambda x: (x.get("occurrence_count", 1), x.get("timestamp", "")),
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


def capture_user_constraint(repo_path: str, prompt: str, session_id: str) -> tuple[str, str] | tuple[None, None]:
    """Called on every UserPromptSubmit. Detects prescriptive 'always/never/from now on' directives
    and stores them as decisions. Returns (entry_id, sanitized_content) if stored, (None, None) otherwise."""
    is_constraint, subtype = _is_prescriptive_constraint(prompt)
    if not is_constraint:
        return None, None
    content = _sanitize_directive(prompt.strip())[:600]
    if not _is_storable(content):
        return None, None
    with _store_lock(_slug(repo_path)):
        data = _load(repo_path)
        decisions_only = [e for e in data["entries"] if e["type"] == "decision"]
        # This hook fires on every prompt; a near-duplicate is a silent no-op (no write).
        if _find_match(content, decisions_only) is not None:
            return None, None
        entry = _new_decision_entry(content, session_id, subtype,
                                    created_by="human", status="approved")
        data["entries"].append(entry)
        data["entries"] = _keep_top(data["entries"], MAX_ENTRIES, pin_last=True)
        _save(repo_path, data)
        return entry["id"], content


_QUESTION_STARTS = {
    "what", "how", "why", "when", "where", "who", "which",
    "is", "are", "can", "does", "do", "will", "would", "could", "should",
}

# Prompts starting with these are answers to questions or acknowledgements, not task descriptions
_ANSWER_STARTS = {
    "no", "yes", "nope", "yep", "yeah", "nah", "ok", "okay",
    "not", "none", "never", "nope", "sure", "correct", "right",
    "nowhere", "nothing", "neither",
}

def _is_task(content: str) -> bool:
    stripped = content.strip()
    words = stripped.lower().split()
    if len(words) < 5:
        return False
    if stripped.endswith("?") and len(words) < 20:
        return False
    if words[0] in _QUESTION_STARTS and len(words) < 12:
        return False
    if words[0] in _ANSWER_STARTS:
        return False
    return True


def capture_task(repo_path: str, description: str, session_id: str) -> str | None:
    if not _is_task(description):
        return None
    with _store_lock(_slug(repo_path)):
        data = _load(repo_path)
        # keep only decisions — one task slot is enough for "last task" context
        data["entries"] = [e for e in data["entries"] if e["type"] != "task"]
        entry = {
            "id": str(uuid.uuid4()),
            "type": "task",
            "content": description,
            "session_id": session_id,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        data["entries"].append(entry)
        data["entries"] = _keep_top(data["entries"], MAX_ENTRIES, pin_last=True)
        _save(repo_path, data)
        return entry["id"]


def _normalize_content(content: str) -> str:
    """Strip whitespace, collapse internal runs, capitalize first character."""
    normalized = " ".join(content.split())
    return normalized[:1].upper() + normalized[1:] if normalized else normalized


def _new_decision_entry(content: str, session_id: str, subtype: str,
                        memory_key: str | None = None,
                        created_by: str = "ai",
                        status: str = "") -> dict:
    """Build a decision entry. Single source of truth for the entry schema —
    both manual capture (`update_decision`) and memory import use this."""
    content = _normalize_content(content)
    if not status:
        level = _classify_level(content, subtype, created_by)
        status = _level_to_status(level)
    entry: dict = {
        "id": str(uuid.uuid4()),
        "type": "decision",
        "subtype": subtype,
        "content": content,
        "session_id": session_id,
        "session_ids": [session_id],
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "occurrence_count": 1,
        "status": status,
        "created_by": created_by,
    }
    score, factors = _compute_confidence(entry)
    entry["confidence"] = score
    if factors:
        entry["confidence_factors"] = factors
    if memory_key is not None:
        entry["memory_key"] = memory_key
    return entry


def update_decision(repo_path: str, content: str, session_id: str, subtype: str = "",
                    created_by: str = "ai") -> tuple[bool, str | None]:
    if not _is_storable(content):
        return False, None
    with _store_lock(_slug(repo_path)):
        data = _load(repo_path)
        decisions_only = [e for e in data["entries"] if e["type"] == "decision"]
        match = _find_match(content, decisions_only)
        if match is not None:
            _record_recurrence(match, session_id)
            _save(repo_path, data)
            return False, None
        entry = _new_decision_entry(content, session_id, subtype, created_by=created_by)
        data["entries"].append(entry)
        data["entries"] = _keep_top(data["entries"], MAX_ENTRIES, pin_last=True)
        _save(repo_path, data)
        return True, entry["id"]


def approve_decision(repo_path: str, entry_id: str, action: str,
                     content: str = "") -> tuple[bool, str]:
    """Approve, ignore, or edit a pending decision.

    action: 'approve' | 'ignore' | 'edit'
    content: the corrected decision text, required when action='edit'
    Returns (success, message).
    """
    if action not in ("approve", "ignore", "edit"):
        return False, f"Invalid action '{action}'. Use: approve, ignore, or edit."
    if action == "edit" and not content.strip():
        return False, "Action 'edit' requires content — provide the corrected decision text."

    with _store_lock(_slug(repo_path)):
        data = _load(repo_path)
        entry = next((e for e in data["entries"] if e.get("id") == entry_id), None)
        if entry is None:
            return False, f"Decision {entry_id!r} not found."

        now = datetime.now(timezone.utc).isoformat()

        if action == "ignore":
            entry["status"] = "ignored"
            _save(repo_path, data)
            return True, "Ignored. This decision will not surface again."

        if action == "edit":
            entry["content"] = content.strip()

        entry["status"] = "approved"
        entry["approved_at"] = now
        entry["approved_by"] = "human"
        score, factors = _compute_confidence(entry)
        entry["confidence"] = score
        if factors:
            entry["confidence_factors"] = factors
        _save(repo_path, data)

        stored_content = entry["content"]
        preview = stored_content[:80] + ("..." if len(stored_content) > 80 else "")
        verb = "Updated and approved" if action == "edit" else "Approved"
        return True, f"{verb}. This decision is now trusted knowledge: \"{preview}\""


def get_pending_decisions(repo_path: str) -> list[dict]:
    """Returns all decisions with status 'pending_approval' for the given repo."""
    data = _load(repo_path)
    return [
        e for e in data.get("entries", [])
        if e.get("type") == "decision" and _entry_status(e) == "pending_approval"
    ]


def get_pending_approval_prompt(repo_path: str, entry_id: str | None) -> str:
    """Generate a formatted approval prompt for a pending decision.
    Returns '' if the entry is not in pending_approval state."""
    if not entry_id:
        return ""
    data = _load(repo_path)
    entry = next((e for e in data["entries"] if e.get("id") == entry_id), None)
    if not entry or _entry_status(entry) != "pending_approval":
        return ""
    score, factors = _compute_confidence(entry)
    factor_lines = "\n".join(f"  - {f}" for f in factors) if factors else "  - Initial capture"
    content = entry.get("content", "")
    eid = entry["id"]
    return (
        f"Engineering Decision Detected — pending your approval\n\n"
        f"\"{content}\"\n\n"
        f"Confidence: {score}%\n"
        f"Evidence:\n{factor_lines}\n\n"
        f"IMPORTANT: Show this to the developer and wait for their response:\n\n"
        f"  [Y] Approve — call approve_decision(entry_id=\"{eid}\", action=\"approve\")\n"
        f"  [E] Edit    — call approve_decision(entry_id=\"{eid}\", action=\"edit\","
        f" content=\"<corrected text>\")\n"
        f"  [N] Ignore  — call approve_decision(entry_id=\"{eid}\", action=\"ignore\")\n\n"
        f"Or the developer can run `contexer review` in their terminal to review later."
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
        changed = match["content"] != content or match.get("subtype", "") != subtype
        match["memory_key"] = memory_key
        if changed:
            match["content"] = content
            match["subtype"] = subtype
            match["timestamp"] = datetime.now(timezone.utc).isoformat()
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


def _git(repo_path: str, *args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True, text=True, timeout=5,
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


def _build_bootstrap_context(repo_path: str) -> list[str]:
    level, decisive = _detect_insight(repo_path)
    repo_name = Path(repo_path).name if repo_path else ""
    label = f'"{repo_name}"' if repo_name else "this repo"

    if decisive and level == "high":
        # commits by this user found — don't ask how well they know their own repo
        offer = [
            f"  \"Contexer: no project context stored for {label}."
            " How should I set up context for future sessions?",
            "   · quick — 1 question (what does this repo do?)",
            "   · full — guided setup, a few questions",
            "   · skip — not now",
            "   (reply scan if you're actually new to this repo)\"",
        ]
        replies = "quick / full / skip (or scan)"
    elif decisive and level == "low":
        # state the evidence, never the conclusion — detection can be wrong
        offer = [
            f"  \"Contexer: no project context stored for {label}."
            " No commits from your git email found here, so I'd scan the code and docs"
            " instead of asking questions you may not be able to answer.",
            "   · scan — go ahead (no questions)",
            "   · quick / full — I actually know this repo (quick: 1 question, full: guided setup)",
            "   · skip — not now\"",
        ]
        replies = "scan / quick / full / skip"
    else:
        # ambiguous signals — ask familiarity directly
        suggestion = (
            ["   (a few commits from your git email found — 'some' is likely right)"]
            if level == "medium" else []
        )
        offer = [
            f"  \"Contexer: no project context stored for {label}."
            " How well do you know this repo?",
            "   · quick — I wrote or maintain it (1 question: what does this repo do?)",
            "   · full — I wrote or maintain it (guided setup, a few questions)",
            "   · some — I work with it but didn't build it",
            "   · scan — first time seeing it: scan code and docs, no questions",
            "   · skip — not now\"",
            *suggestion,
        ]
        replies = "quick / full / some / scan / skip"

    # A newcomer question ("what is this repo doing?", "summarize this repo") is itself
    # low-insight evidence — don't answer it with a menu whose first option mirrors the
    # question back. This check must come FIRST: placed after the menu it loses to
    # "response must be ONLY the offer". Decisive-high keeps the menu: commits by this
    # user outweigh one curious question.
    newbie_exception = [] if (decisive and level == "high") else [
        "STEP 0 — read the user's message before anything else: if it is asking what this"
        " repo or code is or does, or asking to summarize it"
        " ('what is this repo doing?', 'explain this codebase', 'tell me about this repo',"
        " 'summarize this codebase', 'give me an overview'), their message already signals"
        " they're new here."
        " In that case your ENTIRE response is ONLY this confirmation — NOT the menu below:",
        f"  \"Contexer: you're asking about {label}, so I'll assume you're new here —"
        " I'll scan the code and docs, store what I find for future sessions, then answer"
        " your question. OK? (or: quick / full / skip if you actually know this repo)\"",
        "Then stop and wait. If they confirm (ok / yes / scan) → follow the scan path below,"
        " then answer their original question. Any other reply → follow that option's path below.",
        "Only when their message is NOT such a question, output the menu below instead:",
    ]

    return [
        f"No project context stored for {repo_path}.",
        "CRITICAL INSTRUCTION — read before writing a single word:",
        *newbie_exception,
        "Your ENTIRE response must be ONLY the offer block below. No task work. No file reads."
        " No acknowledgment of any prior request. No explanation. Just the offer, then stop.",
        *offer,
        "Output the offer. Then stop completely. Do NOT call bootstrap_context yet."
        f" Do NOT start the user's task. Wait for them to reply {replies}.",
        "Once the user replies:",
        "If quick (or yes) → call bootstrap_context with insight='high'. Ask ONLY the first gap question"
        " (purpose). Store the answer with update_context using the gap's subtype. Stop — do not ask more.",
        "If full (guided) → call bootstrap_context with insight='high'. For each inferred item confirm and store"
        " with update_context(subtype='architecture'). For each gap question ask the user one at a time."
        " After each answer, re-evaluate remaining gaps — if the purpose answer reveals a docs-only,"
        " portfolio, personal, or learning repo, skip tests/CI/deploy/compliance/exclusion gaps."
        " Store each answer as a separate update_context call using the gap's subtype."
        " Write each stored entry as a single plain sentence, max 15 words, no em dashes, no filler phrases."
        " Example: 'No CI/CD pipeline.' NOT 'There is no CI/CD pipeline planned or needed for this repo.'",
        "If some (works with the repo but didn't build it) → call bootstrap_context with insight='medium'."
        " Store each inferred fact directly via update_context (subtype='architecture', no confirmation)."
        " Ask the returned gap questions one at a time (purpose and the user's goal) and store each answer."
        " Same sentence style: plain, max 15 words.",
        "If scan (first time seeing this repo) → call bootstrap_context with insight='low'."
        " The user cannot answer questions about this repo's history or conventions — do NOT quiz them."
        " Store each inferred fact directly via update_context using subtype='architecture'"
        " (no confirmation needed: the facts come from the code, the user cannot validate them)."
        " Read the README and any docs/ to determine the repo's purpose and store it."
        " Ask only the single gap question returned (what the user plans to do here) and store the answer."
        " Same sentence style: plain, max 15 words.",
        "If no or skip → proceed with their original request directly, do not mention bootstrap again.",
        "Purpose question — never echo it back: if the user's original message itself asked what"
        " this repo does, do NOT ask them the purpose gap question. Read the README and code,"
        " answer their question with your own summary, then ask 'Did I get that right —"
        " anything to correct?' and store the confirmed summary as the purpose.",
        "For every gap question, lead with its assumption and ask the user to confirm or"
        " correct it — never ask open-ended questions the scan can already half-answer.",
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
        "2. Call bootstrap_context (no insight argument) and store each inferred fact"
        " via update_context(subtype='architecture') — baseline repo facts from the code.",
        "3. Tell the user in one line how many decisions were stored, e.g."
        " 'Contexer: stored 4 decisions from this conversation.'",
        "4. Then continue with the user's request as normal.",
        "If the conversation contains no decisions, store only the scan facts — never invent.",
    ]


def session_start_payload(repo_path: str, source: str = "") -> dict:
    """Provider-neutral session-start content. Returns {"status": str, "context": str}:
    `status` is the short human-facing line, `context` is the text to inject into the
    conversation. Empty `context` means "inject nothing". All filtering/promotion logic
    is unchanged from the original get_session_start_context."""
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
        STORE_DIR.mkdir(exist_ok=True)
        resume_flag.write_text(repo_path)
        sys_parts = []
        if global_rules:
            sys_parts.append("## Global rules (apply to ALL repos):")
            sys_parts.extend(f"- [{d.get('subtype', '')}] {d['content']}" for d in global_rules)
            sys_parts.append("")
        sys_parts.extend(_build_resume_mining_context(repo_path))
        return {
            "status": "Contexer: resumed with no stored context — mining this conversation for decisions",
            "context": "\n".join(sys_parts),
        }

    resume_flag.unlink(missing_ok=True)

    if not decisions:
        lines = _build_bootstrap_context(repo_path)
        sys_parts = []
        if global_rules:
            sys_parts.append("## Global rules (apply to ALL repos):")
            for d in global_rules:
                sys_parts.append(f"- [{d.get('subtype', '')}] {d['content']}")
            sys_parts.append("")
        sys_parts.extend(lines)
        global_note = f" ({_pl(len(global_rules), 'global rule')} active)" if global_rules else ""
        return {
            "status": f"Contexer: no context stored{global_note} — setup offer on next prompt",
            "context": "\n".join(sys_parts),
        }

    # Separate decisions by status for injection and summary.
    # pending_approval and ignored decisions are never auto-injected — they must
    # be explicitly approved before becoming trusted engineering knowledge.
    pending = [d for d in decisions if _entry_status(d) == "pending_approval"]
    trusted = [d for d in decisions if _entry_status(d) in ("approved", "suggested")]
    pre_loaded = [d for d in trusted if d.get("subtype") in ("convention", "constraint", "pattern")]
    deferred_count = len(trusted) - len(pre_loaded)

    sys_parts = []
    if global_rules:
        sys_parts.append("## Global rules (apply to ALL repos):")
        for d in global_rules:
            sys_parts.append(f"- [{d.get('subtype', '')}] {d['content']}")
    if pre_loaded:
        sys_parts.append("## Project rules — apply to ALL tasks in this repo:")
        for d in pre_loaded:
            st = _entry_status(d)
            status_tag = " [suggested]" if st == "suggested" else ""
            sys_parts.append(f"- [{d.get('subtype', '')}]{status_tag}{_recur_suffix(d)} {d['content']}")
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
    if pending:
        pending_by_type: dict[str, int] = {}
        for d in pending:
            st = d.get("subtype") or "decision"
            pending_by_type[st] = pending_by_type.get(st, 0) + 1
        pending_parts = [_pl(cnt, st) for st, cnt in sorted(pending_by_type.items())]
        sys_parts.append(
            f"{', '.join(pending_parts)} pending your approval. "
            "Run `contexer review` in your terminal or call approve_decision() "
            "after reviewing each with the developer."
        )

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
    if pending:
        pending_by_type_str = ", ".join(
            _pl(cnt, st) for st, cnt in sorted(pending_by_type.items())
        )
        sentences.append(f"{pending_by_type_str} pending approval — run `contexer review`")

    status = f"Contexer: {'. '.join(sentences)}." if sentences else "Contexer: active."
    return {"status": status, "context": "\n".join(sys_parts)}


def get_session_start_context(repo_path: str, source: str = "") -> dict:
    """Claude Code SessionStart hook output. Thin envelope over session_start_payload —
    kept for back-compat with installed hooks and the existing test suite."""
    from contexer.adapters import claude
    return claude.format_session_start(session_start_payload(repo_path, source))


_NEWCOMER_QUESTION_RE = re.compile(
    r"\b(what (is|does) (this|the) (repo|repository|codebase|project|code)\b"
    r"|what'?s (this|the) (repo|repository|codebase|project)( about| for| doing)?\b"
    r"|explain (this|the) (repo|repository|codebase|project|code)\b"
    r"|tell me about (this|the) (repo|repository|codebase|project|code)\b"
    r"|how does (this|the) (repo|repository|codebase|project|code) work\b"
    r"|walk me through (this|the) (repo|repository|codebase|project|code)\b"
    r"|overview of (this|the) (repo|repository|codebase|project|code)\b"
    r"|summarize (this|the) (repo|repository|codebase|project|code)\b"
    r"|summary of (this|the) (repo|repository|codebase|project|code)\b)",
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
    level, decisive = _detect_insight(repo_path)
    repo_name = Path(repo_path).name if repo_path else ""
    label = f'"{repo_name}"' if repo_name else "this repo"
    if _is_newcomer_question(prompt) and not (decisive and level == "high"):
        lines = [
            "Contexer OVERRIDE — ignore any earlier bootstrap menu instructions for this turn.",
            "The user's first message asks about or wants to summarize this repo. That is"
            " low-insight evidence (matched deterministically). Your ENTIRE response must be ONLY:",
            f"  \"Contexer: you're asking about {label}, so I'll assume you're new here —"
            " I'll scan the code and docs, store what I find for future sessions, then answer"
            " your question. OK? (or: quick / full / skip if you actually know this repo)\"",
            "Then stop and wait. If they confirm (ok / yes / scan) → call bootstrap_context"
            " with insight='low', store each inferred fact directly via update_context"
            " (subtype='architecture'), read the README and docs for the repo's purpose and"
            " store it, ask the single returned goal question and store the answer, then"
            " answer their original question.",
            "If they reply quick / full / skip instead → follow the session-start bootstrap"
            " instructions for that option.",
        ]
    else:
        lines = _build_bootstrap_context(repo_path)
    return {"status": "", "context": "\n".join(lines)}


def get_bootstrap_context_prompt(repo_path: str, prompt: str = "") -> dict:
    """Claude UserPromptSubmit bootstrap-fallback output. Back-compat envelope."""
    from contexer.adapters import claude
    return claude.format_bootstrap_prompt(bootstrap_prompt_payload(repo_path, prompt))


def post_compact_payload(repo_path: str) -> dict:
    """Neutral PostCompact content. {"status": str, "context": str}."""
    data = _load(repo_path)
    decisions = [e for e in data.get("entries", []) if e["type"] == "decision"]
    if not decisions:
        return {"status": "", "context": "\n".join(_build_bootstrap_context(repo_path))}
    return {"status": "Contexer: context reloaded after compaction", "context": get_context(repo_path)}


def get_post_compact_context(repo_path: str) -> dict:
    """Claude PostCompact output. Back-compat envelope."""
    from contexer.adapters import claude
    return claude.format_post_compact(post_compact_payload(repo_path))


_RATIONALE_WORDS = frozenset({
    "why", "reason", "rationale", "decision", "decided", "chose", "choice",
    "motivation", "intent", "reasoning", "background", "justif",
    # approach/design questions: "what approach did we take?", "what's the architecture?"
    "approach", "architecture", "tradeoff", "tradeoffs", "constraint", "convention",
})

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


def get_context_for_prompt(repo_path: str, prompt: str) -> str:
    """Auto-injected by UserPromptSubmit hook. Returns relevant stored decisions when
    the prompt is a rationale/decision or project-context question. Silent no-op otherwise.
    Searches repo decisions first; falls back to global decisions."""
    words_raw = [w.strip("?,./!;:\"'()[]") for w in prompt.lower().split()]
    word_set = set(words_raw)

    is_rationale = bool(word_set & _RATIONALE_WORDS)
    is_project = bool(word_set & _PROJECT_CONTEXT_WORDS)

    if not is_rationale and not is_project:
        return ""

    # Extract content keywords: alpha-only, length >= 3, not stop words.
    # >= 3 (not > 3) captures short tech terms: jwt, api, sdk, k8s, sql, gcp, aws.
    keywords = [
        w for w in words_raw
        if len(w) >= 3 and w not in _QUERY_STOP_WORDS and w.isalpha()
    ]
    ordered_kws = sorted(set(keywords), key=len, reverse=True)[:3]

    # Search repo decisions first (longest keyword = most specific match).
    # Rationale questions use the default (non-active-only) mode: the AI should see
    # pending decisions too (with [pending] tag) so it can answer "why" questions even
    # for decisions not yet approved. Only session-start injection restricts to active.
    data = _load(repo_path)
    if data.get("entries"):
        for kw in ordered_kws:
            result = get_context(repo_path, query=kw)
            if "No matching decisions" not in result and "No context stored" not in result:
                return f"[Contexer: auto-fetched for this question]\n{result}"

        # Overview fallback: only when the prompt has NO domain-specific keywords beyond
        # the project-context trigger word itself. Generic referential words like "repo",
        # "project", "app" don't count — they just mean "this thing we're discussing."
        # Real domain keywords (e.g. "docker", "react", "postgres") block the overview.
        if is_project:
            non_project_kws = [
                k for k in ordered_kws
                if k not in _PROJECT_CONTEXT_WORDS and k not in _OVERVIEW_GENERIC_WORDS
            ]
            if not non_project_kws:
                result = get_context(repo_path)
                if "No context stored" not in result:
                    return f"[Contexer: project context]\n{result}"

    # Fall back to global decisions when repo search yields nothing
    for kw in ordered_kws:
        result = get_global_context(query=kw)
        if "No matching" not in result and "No global context" not in result:
            return f"[Contexer: auto-fetched from global context]\n{result}"

    return ""


def get_context(repo_path: str, query: str = "", entry_type: str = "", limit: int = 0,
                _active_only: bool = False) -> str:
    """Returns stored context for the given repo.

    _active_only: internal flag — when True, exclude pending_approval and ignored entries
    (used by auto-injection paths so only trusted decisions reach the AI automatically).
    """
    data = _load(repo_path)
    entries = data.get("entries", [])
    if not entries:
        return "No context stored for this repository."

    lines = [f"# Context for {repo_path}\n"]

    if not entry_type:
        tasks = [e for e in entries if e["type"] == "task"]
        if tasks:
            last = tasks[-1]
            lines.append(f"## Last task ({last['timestamp'][:10]})")
            lines.append(last["content"])
            lines.append("")

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
        decisions = [d for d in decisions if pat.search(d.get("content", ""))]

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
        for d in shown:
            subtype_tag = f" [{d['subtype']}]" if d.get("subtype") else ""
            st = _entry_status(d)
            status_tag = " [suggested]" if st == "suggested" else " [pending]" if st == "pending_approval" else ""
            lines.append(f"- [{d['timestamp'][:10]}]{subtype_tag}{status_tag}{_recur_suffix(d)} {d['content']}")
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


def bootstrap_scan(repo_path: str, insight: str = "") -> dict:
    if insight in _INSIGHT_ORDER:
        insight_source, decisive = "user", True
    else:
        insight, decisive = _detect_insight(repo_path)
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

    # Tests — only if no test framework detected AND not a simple/docs repo
    if not sig["has_tests"] and not is_simple:
        gaps.append(_gap(
            assumption="No automated test framework detected",
            question="Is automated testing in scope?",
            hint=_test_hint(),
            subtype="convention",
        ))

    # CI — only if no CI config found AND not a simple/docs repo
    if not sig["has_ci"] and not is_simple:
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

    # Team conventions — only if architecture signals suggest a team wrote this
    has_team_signals = (
        any("Architecture" in i or "layered" in i for i in inferred) or
        len(inferred) > 5
    )
    if has_team_signals and not is_simple:
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

    # Validation placement — only if a web framework is present
    has_web_framework = _has_dep(
        "fastapi", "flask", "django", "express", "hono", "elysia", "fastify",
        "next", "nuxt", "remix", "svelte", "aiohttp", "starlette",
    )
    if has_web_framework and not is_simple:
        gaps.append(_gap(
            assumption="Input validation placement not documented",
            question="Where does input validation live — at the HTTP boundary, in the service layer, or both?",
            hint="e.g. Pydantic models at the route layer only; or service layer validates too; or middleware",
            subtype="pattern",
            min_insight="high",
        ))

    # Error handling — only if production signals exist
    if has_production_signals:
        gaps.append(_gap(
            assumption="Error handling approach not documented",
            question="How are errors surfaced — exceptions bubble up, result types, or error middleware?",
            hint="e.g. raise HTTPException at route layer; Result[T, E] types; global exception handler",
            subtype="pattern",
            min_insight="high",
        ))

    # Interview floor for repo authors: signal-conditional gaps collapse to almost
    # nothing on simple repos (no config to scan), but 'full' is an explicit opt-in
    # to an interview — the author's head holds decisions no scan can reach.
    if user_rank == _INSIGHT_ORDER["high"] and len(gaps) < 4:
        interview = [
            _gap(
                assumption="Non-obvious decisions exist only in the author's head",
                question="What decisions shaped this code that aren't visible in it — libraries chosen over alternatives, approaches rejected, structure?",
                hint="e.g. 'argparse over click to avoid deps'; 'rejected async — overkill here'",
                subtype="architecture",
            ),
            _gap(
                assumption="No coding or workflow conventions captured",
                question="Any conventions future sessions should respect — naming, structure, commit style, how you like code written?",
                hint="e.g. 'single file until it hurts'; 'conventional commits'; 'comments only for why'",
                subtype="convention",
            ),
            _gap(
                assumption="No working rules for Claude captured",
                question="Any rules for how Claude should work in this repo — always do, never touch, check before changing?",
                hint="e.g. 'always run tests before commit'; 'never edit data/'; 'ask before adding deps'",
                subtype="constraint",
            ),
        ]
        gaps.extend(interview[:4 - len(gaps)])

    gaps = [g for g in gaps if user_rank >= _INSIGHT_ORDER[g["min_insight"]]]
    return {
        "inferred": inferred,
        "gaps": gaps,
        "existing_context_files": found_files,
        "insight": insight,
        "insight_source": insight_source,
        "decisive": decisive,
    }
