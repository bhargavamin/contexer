"""Claude Code integration adapter."""
import json
from pathlib import Path

from contexer import store

NAME = "claude"


def is_present(home: Path) -> bool:
    # Claude's config may be a directory (~/.claude) or a standalone file (~/.claude.json).
    return (home / ".claude").exists() or (home / ".claude.json").exists()


def format_session_start(payload: dict) -> dict:
    """Neutral payload -> Claude SessionStart hook output. Empty context => status only."""
    if not payload.get("context"):
        return {"systemMessage": payload["status"]} if payload.get("status") else {}
    return {
        "systemMessage": payload["status"],
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": payload["context"],
        },
    }


def format_bootstrap_prompt(payload: dict) -> dict:
    """Neutral payload -> Claude UserPromptSubmit output. Empty context => no-op {}."""
    if not payload.get("context"):
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": payload["context"],
        }
    }


def format_post_compact(payload: dict) -> dict:
    """Neutral payload -> Claude PostCompact output (injected via systemMessage)."""
    parts = [p for p in (payload.get("status"), payload.get("context")) if p]
    return {"systemMessage": "\n".join(parts)} if parts else {}


def capture_task(repo_path: str, raw: str) -> str:
    """UserPromptSubmit (once): store the first prompt as the task. Silent."""
    try:
        repo = store._resolve_repo(repo_path)
        if repo:
            store.capture_task(repo, store.prompt_from_hook_stdin(raw),
                               store.session_from_hook_stdin(raw))
    except Exception:
        pass
    return "{}"


def capture_constraint(repo_path: str, raw: str) -> str:
    """UserPromptSubmit (every prompt): auto-store 'always/never/from now on' directives."""
    try:
        repo = store._resolve_repo(repo_path)
        if not repo:
            return "{}"
        entry_id, content = store.capture_user_constraint(
            repo, store.prompt_from_hook_stdin(raw), store.session_from_hook_stdin(raw))
        if entry_id is None:
            return "{}"
        msg = (f"Auto-stored as constraint: '{content}'. "
               "Acknowledge this briefly to the user — e.g. 'Stored as a constraint in Contexer.'")
        return json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": msg}})
    except Exception:
        return "{}"


def rationale(repo_path: str, raw: str) -> str:
    """UserPromptSubmit (every prompt): inject matching decisions for rationale questions."""
    try:
        repo = store._resolve_repo(repo_path)
        if not repo:
            return "{}"
        ctx = store.get_context_for_prompt(repo, store.prompt_from_hook_stdin(raw))
        if not ctx:
            return "{}"
        return json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": ctx}})
    except Exception:
        return "{}"
