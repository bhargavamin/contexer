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

STORE_DIR = Path.home() / ".contexer"
MAX_ENTRIES = 500
GLOBAL_SLUG = "_global"           # reserved slug for cross-repo decisions
_UNFILTERED_DISPLAY = 10          # entries shown when no query/type filter applied
_FILTERED_DISPLAY = 25            # entries shown when a filter is active


def _current_repo_path() -> str:
    path = STORE_DIR / ".current_repo"
    if path.exists():
        return path.read_text().strip()
    return ""


def _resolve_repo(repo_path: str) -> str:
    if repo_path:
        return repo_path
    return _current_repo_path()

def _slug(repo_path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", repo_path.strip("/"))


def _store_path(repo_path: str) -> Path:
    STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    return STORE_DIR / f"{_slug(repo_path)}.json"


def _load(repo_path: str) -> dict:
    path = _store_path(repo_path)
    if path.exists():
        try:
            # encoding pinned to match _atomic_write — never the locale default
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            # Treat a corrupted or unreadable file as empty — recovers from concurrent-write races.
            return {"repo_path": repo_path, "entries": []}
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


# ── Global store ───────────────────────────────────────────────────────────────

def _global_path() -> Path:
    STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    return STORE_DIR / f"{GLOBAL_SLUG}.json"


def _load_global() -> dict:
    path = _global_path()
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return {"repo_path": GLOBAL_SLUG, "entries": []}
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
    data = _load_global()
    if not _passes_filter(content, data["entries"]):
        return False, None
    entry = {
        "id": str(uuid.uuid4()),
        "type": "decision",
        "subtype": subtype,
        "content": content,
        "session_id": session_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    data["entries"].append(entry)
    data["entries"] = data["entries"][-MAX_ENTRIES:]
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
        pat = re.compile(r"\b" + re.escape(query.lower()), re.IGNORECASE)
        decisions = [d for d in decisions if pat.search(d.get("content", ""))]

    display_limit = limit if limit > 0 else (_FILTERED_DISPLAY if is_filtered else _UNFILTERED_DISPLAY)
    lines = ["# Global context (applies to all repos)\n"]

    if decisions:
        total = len(decisions)
        shown = decisions[-display_limit:]
        filter_note = f" — showing {len(shown)} of {total}" if total > display_limit else ""
        lines.append(f"## Global decisions{filter_note}")
        for d in shown:
            subtype_tag = f" [{d['subtype']}]" if d.get("subtype") else ""
            lines.append(f"- [{d['timestamp'][:10]}]{subtype_tag} {d['content']}")
    elif is_filtered:
        lines.append("No matching global decisions found.")

    return "\n".join(lines)


_PUNCT_RE = re.compile(r"[^\w\s]")

def _tokenize(text: str) -> set[str]:
    """Lowercase, strip punctuation, split on whitespace. Fixes comma-attached tokens."""
    return set(_PUNCT_RE.sub("", text.lower()).split())


def _is_novel(content: str, existing: list) -> bool:
    if not existing:
        return True
    tokens = _tokenize(content)
    if not tokens:
        return False
    for entry in existing:
        other = _tokenize(entry.get("content", ""))
        if not other:
            continue
        overlap = len(tokens & other) / max(len(tokens), len(other))
        if overlap > 0.7:
            return False
    return True


def _passes_filter(content: str, existing: list) -> bool:
    # Novelty is a prerequisite veto — duplicates are rejected regardless of signal keywords.
    # Novel content always passes: update_context is only called for significant decisions.
    decisions_only = [e for e in existing if e["type"] == "decision"]
    return _is_novel(content, decisions_only)


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
    r")\b",
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
_TRAILING_FILLER = re.compile(
    r"\s*(?:hence|so\s+(?:that|it|we|this)|because\s+(?:of\s+)?(?:this|that|it)|"
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
_CONVENTION_SIGNALS = re.compile(
    r"\b(?:from\s+now\s+on|going\s+forward|henceforth)\b",
    re.IGNORECASE,
)

# Personal-descriptive patterns — these describe existing habits, not directives.
# "I always get this error", "we never did that", "it always worked before" are descriptive.
# NOTE: "it should always" is NOT caught here because "should" sits between "it" and "always".
_PERSONAL_DESCRIPTOR = re.compile(
    r"\b(i|we|it)\s+(have\s+|has\s+|did\s+|does\s+)?(always|never)\b",
    re.IGNORECASE,
)


def _is_prescriptive_constraint(text: str) -> tuple[bool, str]:
    """Returns (is_constraint, subtype). Detects user-stated directives.
    Excludes descriptive first-person/it uses ('I always get this error', 'it always worked')
    and ironic/sarcastic statements ('love always use pip', 'yeah right /s')."""
    if text.strip().endswith("?"):
        return False, ""
    if _SARCASM_EXCLUDES.search(text.strip()):
        return False, ""
    if not _CONSTRAINT_TRIGGER.search(text):
        return False, ""
    # Strip descriptive personal instances; if nothing remains, it was purely descriptive
    cleaned = _PERSONAL_DESCRIPTOR.sub("", text)
    if not _CONSTRAINT_TRIGGER.search(cleaned):
        return False, ""
    # Pure forward-looking practice signals (no always/never) → convention
    # Everything else (mandatory requirements, prohibitions) → constraint
    is_soft = bool(_CONVENTION_SIGNALS.search(cleaned))
    has_hard = bool(re.search(r"\b(?:al+w(?:ay|ya)s|never|must|should)\b", cleaned, re.IGNORECASE))
    subtype = "convention" if (is_soft and not has_hard) else "constraint"
    return True, subtype


def capture_user_constraint(repo_path: str, prompt: str, session_id: str) -> tuple[str, str] | tuple[None, None]:
    """Called on every UserPromptSubmit. Detects prescriptive 'always/never/from now on' directives
    and stores them as decisions. Returns (entry_id, sanitized_content) if stored, (None, None) otherwise."""
    is_constraint, subtype = _is_prescriptive_constraint(prompt)
    if not is_constraint:
        return None, None
    content = _sanitize_directive(prompt.strip())[:600]
    data = _load(repo_path)
    if not _passes_filter(content, data["entries"]):
        return None, None
    entry = {
        "id": str(uuid.uuid4()),
        "type": "decision",
        "subtype": subtype,
        "content": content,
        "session_id": session_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    data["entries"].append(entry)
    data["entries"] = data["entries"][-MAX_ENTRIES:]
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
    data["entries"] = data["entries"][-MAX_ENTRIES:]
    _save(repo_path, data)
    return entry["id"]


def update_decision(repo_path: str, content: str, session_id: str, subtype: str = "") -> tuple[bool, str | None]:
    data = _load(repo_path)
    if not _passes_filter(content, data["entries"]):
        return False, None
    entry = {
        "id": str(uuid.uuid4()),
        "type": "decision",
        "subtype": subtype,
        "content": content,
        "session_id": session_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    data["entries"].append(entry)
    data["entries"] = data["entries"][-MAX_ENTRIES:]
    _save(repo_path, data)
    return True, entry["id"]


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

    if decisive and level == "high":
        # commits by this user found — don't ask how well they know their own repo
        offer = [
            "  \"Contexer: no project context stored for this repo."
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
            "  \"Contexer: no project context stored for this repo."
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
            "  \"Contexer: no project context stored for this repo."
            " How well do you know this repo?",
            "   · quick — I wrote or maintain it (1 question: what does this repo do?)",
            "   · full — I wrote or maintain it (guided setup, a few questions)",
            "   · some — I work with it but didn't build it",
            "   · scan — first time seeing it: scan code and docs, no questions",
            "   · skip — not now\"",
            *suggestion,
        ]
        replies = "quick / full / some / scan / skip"

    # A newcomer question ("what is this repo doing?") is itself low-insight evidence —
    # don't answer it with a menu whose first option mirrors the question back. This check
    # must come FIRST: placed after the menu it loses to "response must be ONLY the offer".
    # Decisive-high keeps the menu: commits by this user outweigh one curious question.
    newbie_exception = [] if (decisive and level == "high") else [
        "STEP 0 — read the user's message before anything else: if it is asking what this"
        " repo or code is or does ('what is this repo doing?', 'explain this codebase',"
        " 'tell me about this repo'), their question already signals they're new here."
        " In that case your ENTIRE response is ONLY this confirmation — NOT the menu below:",
        "  \"Contexer: you're asking what this repo does, so I'll assume you're new here —"
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


def get_session_start_context(repo_path: str, source: str = "") -> dict:
    data = _load(repo_path)
    decisions = [e for e in data.get("entries", []) if e["type"] == "decision"]
    global_rules = get_global_decisions()  # always constraint or convention
    resume_flag = STORE_DIR / ".resume_mining"

    if source == "resume":
        if decisions:
            # The conversation already contains the original session-start injection —
            # re-injecting would duplicate ~1k tokens for nothing.
            return {"systemMessage":
                    f"Contexer: session resumed — {_pl(len(decisions), 'decision')} already loaded in conversation"}
        # Fresh install mid-conversation: the transcript is the best decision source
        # there will ever be. Mine it in the first turn instead of offering a menu —
        # no human round-trip, so decisions are banked even if the session dies early.
        STORE_DIR.mkdir(exist_ok=True)
        resume_flag.write_text(repo_path)  # suppresses the UserPromptSubmit menu fallback
        sys_parts = []
        if global_rules:
            sys_parts.append("## Global rules (apply to ALL repos):")
            sys_parts.extend(f"- [{d.get('subtype', '')}] {d['content']}" for d in global_rules)
            sys_parts.append("")
        sys_parts.extend(_build_resume_mining_context(repo_path))
        return {
            "systemMessage": "Contexer: resumed with no stored context — mining this conversation for decisions",
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "\n".join(sys_parts),
            },
        }

    resume_flag.unlink(missing_ok=True)  # stale flag from a resume that never got a prompt

    if not decisions:
        # No repo context — bootstrap. Inject global rules above the STOP directive
        # so Claude follows them even during the bootstrap conversation.
        lines = _build_bootstrap_context(repo_path)
        sys_parts: list[str] = []
        if global_rules:
            sys_parts.append("## Global rules (apply to ALL repos):")
            for d in global_rules:
                sys_parts.append(f"- [{d.get('subtype', '')}] {d['content']}")
            sys_parts.append("")
        sys_parts.extend(lines)
        global_note = f" ({_pl(len(global_rules), 'global rule')} active)" if global_rules else ""
        return {
            "systemMessage": f"Contexer: no context stored{global_note} — setup offer on next prompt",
            "hookSpecificOutput": {
                "hookEventName": "SessionStart",
                "additionalContext": "\n".join(sys_parts),
            },
        }

    count = len(decisions)
    pre_loaded = [d for d in decisions if d.get("subtype") in ("convention", "constraint")]
    deferred_count = count - len(pre_loaded)

    sys_parts = []
    if global_rules:
        sys_parts.append("## Global rules (apply to ALL repos):")
        for d in global_rules:
            sys_parts.append(f"- [{d.get('subtype', '')}] {d['content']}")
    if pre_loaded:
        sys_parts.append("## Project rules — apply to ALL tasks in this repo:")
        for d in pre_loaded:
            sys_parts.append(f"- [{d.get('subtype', '')}] {d['content']}")
    if deferred_count > 0:
        arch_count = sum(1 for d in decisions if d.get("subtype") == "architecture")
        pat_count = sum(1 for d in decisions if d.get("subtype") == "pattern")
        breakdown_parts = []
        if arch_count:
            breakdown_parts.append(f"{arch_count} architecture")
        if pat_count:
            breakdown_parts.append(f"{pat_count} pattern")
        breakdown = f" ({', '.join(breakdown_parts)})" if breakdown_parts else ""
        sys_parts.append(
            f"{deferred_count} decision(s) stored{breakdown}. "
            "Call get_context BEFORE reading files for any question about architecture, "
            "design decisions, rationale, or patterns."
        )

    constraints = [d for d in pre_loaded if d.get("subtype") == "constraint"]
    conventions = [d for d in pre_loaded if d.get("subtype") == "convention"]

    loaded_parts: list[str] = []
    if global_rules:
        loaded_parts.append(_pl(len(global_rules), "global rule"))
    if constraints:
        loaded_parts.append(_pl(len(constraints), "constraint"))
    if conventions:
        loaded_parts.append(_pl(len(conventions), "convention"))

    sentences: list[str] = []
    if loaded_parts:
        sentences.append(f"{', '.join(loaded_parts)} loaded")
    if deferred_count > 0:
        sentences.append(f"{_pl(deferred_count, 'arch/pattern')} will be loaded on demand")

    user_line = f"Contexer: {'. '.join(sentences)}." if sentences else "Contexer: active."

    return {
        "systemMessage": user_line,
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(sys_parts),
        },
    }


_NEWCOMER_QUESTION_RE = re.compile(
    r"\b(what (is|does) (this|the) (repo|repository|codebase|project|code)\b"
    r"|what'?s (this|the) (repo|repository|codebase|project)( about| for| doing)?\b"
    r"|explain (this|the) (repo|repository|codebase|project|code)\b"
    r"|tell me about (this|the) (repo|repository|codebase|project|code)\b"
    r"|how does (this|the) (repo|repository|codebase|project|code) work\b"
    r"|walk me through (this|the) (repo|repository|codebase|project|code)\b"
    r"|overview of (this|the) (repo|repository|codebase|project|code)\b)",
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


def get_bootstrap_context_prompt(repo_path: str, prompt: str = "") -> dict:
    """Fallback for UserPromptSubmit: catches the case where SessionStart bootstrap
    was skipped (e.g. non-interactive session). Returns empty dict when context exists.
    When the first prompt is itself a newcomer question, the menu is replaced with a
    low-insight confirmation — decided deterministically here, not by model judgment,
    because this is the first thing a user sees after installing."""
    data = _load(repo_path)
    decisions = [e for e in data.get("entries", []) if e["type"] == "decision"]
    if decisions:
        return {}
    # Resumed session: SessionStart already injected mining instructions — a menu
    # here would contradict them ("ENTIRE response must be ONLY the offer").
    resume_flag = STORE_DIR / ".resume_mining"
    if resume_flag.exists():
        try:
            flagged = resume_flag.read_text().strip()
        except Exception:
            flagged = ""
        if flagged == repo_path:
            resume_flag.unlink(missing_ok=True)
            return {}
    level, decisive = _detect_insight(repo_path)
    if _is_newcomer_question(prompt) and not (decisive and level == "high"):
        lines = [
            "Contexer OVERRIDE — ignore any earlier bootstrap menu instructions for this turn.",
            "The user's first message asks what this repo is or does. That is low-insight"
            " evidence (matched deterministically). Your ENTIRE response must be ONLY:",
            "  \"Contexer: you're asking what this repo does, so I'll assume you're new here —"
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
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(lines),
        }
    }


def get_post_compact_context(repo_path: str) -> dict:
    """Called by PostCompact hook. Re-injects stored context after compaction, or
    re-offers bootstrap if no context exists — so the offer is not silently lost after compact."""
    data = _load(repo_path)
    decisions = [e for e in data.get("entries", []) if e["type"] == "decision"]
    if not decisions:
        lines = _build_bootstrap_context(repo_path)
        return {"systemMessage": "\n".join(lines)}
    ctx = get_context(repo_path)
    return {"systemMessage": f"Contexer: context reloaded after compaction\n{ctx}"}


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

    # Search repo decisions first (longest keyword = most specific match)
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


def get_context(repo_path: str, query: str = "", entry_type: str = "", limit: int = 0) -> str:
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

    is_filtered = bool(query or entry_type)
    if entry_type:
        decisions = [d for d in decisions if d.get("subtype", "") == entry_type]

    if query:
        pat = re.compile(r"\b" + re.escape(query.lower()), re.IGNORECASE)
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
        shown = decisions[-display_limit:]
        if total > display_limit:
            filter_note += f" — showing {len(shown)} of {total}"
        lines.append(f"## Decisions and context{filter_note}")
        for d in shown:
            subtype_tag = f" [{d['subtype']}]" if d.get("subtype") else ""
            lines.append(f"- [{d['timestamp'][:10]}]{subtype_tag} {d['content']}")
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
