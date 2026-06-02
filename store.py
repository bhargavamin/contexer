import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

STORE_DIR = Path.home() / ".contexer"
MAX_ENTRIES = 50

_DECISION_SIGNALS = [
    "decided", "decision", "chose", "approach", "instead of",
    "rather than", "went with", "will use", "should use", "opted",
]
_PATTERN_SIGNALS = [
    "pattern", "convention", "always", "never", "standard",
    "consistent", "going forward", "from now on", "practice",
]
_CONSTRAINT_SIGNALS = [
    "constraint", "tradeoff", "trade-off", "limitation", "cannot",
    "avoid", "requirement", "must not", "intentionally", "by design",
]


def _slug(repo_path: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", repo_path.strip("/"))


def _store_path(repo_path: str) -> Path:
    STORE_DIR.mkdir(exist_ok=True)
    return STORE_DIR / f"{_slug(repo_path)}.json"


def _load(repo_path: str) -> dict:
    path = _store_path(repo_path)
    if path.exists():
        return json.loads(path.read_text())
    return {"repo_path": repo_path, "entries": []}


def _save(repo_path: str, data: dict) -> None:
    _store_path(repo_path).write_text(json.dumps(data, indent=2))


def _is_novel(content: str, existing: list) -> bool:
    if not existing:
        return True
    tokens = set(content.lower().split())
    if not tokens:
        return False
    for entry in existing:
        other = set(entry.get("content", "").lower().split())
        if not other:
            continue
        overlap = len(tokens & other) / max(len(tokens), len(other))
        if overlap > 0.7:
            return False
    return True


def _passes_filter(content: str, existing: list) -> bool:
    c = content.lower()
    c1 = any(w in c for w in _DECISION_SIGNALS)
    c2 = any(w in c for w in _PATTERN_SIGNALS)
    c3 = any(w in c for w in _CONSTRAINT_SIGNALS)
    c4 = _is_novel(content, existing)
    return c1 or c2 or c3 or c4


def capture_task(repo_path: str, description: str, session_id: str) -> str:
    data = _load(repo_path)
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


def update_decision(repo_path: str, content: str, session_id: str) -> tuple[bool, str | None]:
    data = _load(repo_path)
    if not _passes_filter(content, data["entries"]):
        return False, None
    entry = {
        "id": str(uuid.uuid4()),
        "type": "decision",
        "content": content,
        "session_id": session_id,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    data["entries"].append(entry)
    data["entries"] = data["entries"][-MAX_ENTRIES:]
    _save(repo_path, data)
    return True, entry["id"]


def get_context(repo_path: str) -> str:
    data = _load(repo_path)
    entries = data.get("entries", [])
    if not entries:
        return "No context stored for this repository."

    lines = [f"# Context for {repo_path}\n"]

    tasks = [e for e in entries if e["type"] == "task"]
    if tasks:
        last = tasks[-1]
        lines.append(f"## Last task ({last['timestamp'][:10]})")
        lines.append(last["content"])
        lines.append("")

    decisions = [e for e in entries if e["type"] == "decision"]
    if decisions:
        lines.append("## Decisions and context")
        for d in decisions[-10:]:
            lines.append(f"- [{d['timestamp'][:10]}] {d['content']}")
        lines.append("")

    return "\n".join(lines)
