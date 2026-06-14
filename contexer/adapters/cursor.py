"""Cursor integration adapter."""
import json
from pathlib import Path

from contexer import store

NAME = "cursor"


def is_present(home: Path) -> bool:
    return (home / ".cursor").exists()


# Claude delivers these reminders via per-prompt hooks; Cursor can only inject at
# session start, so the nudge rides in additional_context (see the spec's parity matrix).
_NUDGE = (
    "Contexer is active. Call get_context BEFORE reading files for any question about "
    "architecture, design decisions, rationale, patterns, or constraints. Call "
    "update_context after any significant decision, established pattern, or stated "
    "constraint (the server deduplicates, so err on the side of calling it)."
)


def format_session_start(payload: dict) -> dict:
    """Neutral payload -> Cursor sessionStart output. Cursor has no systemMessage
    channel and cannot inject per-prompt, so the behavioral nudge is appended here."""
    ctx = payload.get("context") or ""
    combined = f"{ctx}\n\n{_NUDGE}" if ctx else _NUDGE
    return {"additional_context": combined}


def format_prompt_passthrough() -> dict:
    """beforeSubmitPrompt output: allow the prompt. Cursor cannot inject context here,
    so capture hooks are write-only side effects that return this pass-through."""
    return {"continue": True}


def _repo_from(raw: str, repo_path: str) -> str:
    """Cursor sessionStart provides workspace_roots[]; fall back to .current_repo."""
    if repo_path:
        return store._resolve_repo(repo_path)
    try:
        roots = json.loads(raw).get("workspace_roots") or []
        if roots:
            return store._resolve_repo(roots[0])
    except Exception:
        pass
    return store._resolve_repo("")


def session_start(repo_path: str, raw: str) -> str:
    """Cursor sessionStart: write .current_repo, inject rules + nudge. Never raises."""
    try:
        repo = _repo_from(raw, repo_path)
        if repo:
            store.STORE_DIR.mkdir(exist_ok=True)
            (store.STORE_DIR / ".current_repo").write_text(repo)
            payload = store.session_start_payload(repo)
        else:
            payload = {"status": "", "context": ""}
        return json.dumps(format_session_start(payload))
    except Exception:
        return json.dumps({"additional_context": _NUDGE})


def capture_task(repo_path: str, raw: str) -> str:
    """beforeSubmitPrompt: store the prompt as the task (write-only).

    v1 note: Cursor's beforeSubmitPrompt has no "once" semantics, so this runs on
    every prompt and store.capture_task replaces the task entry each time — Cursor
    tracks the *latest* prompt-as-task, not just the first. Acceptable for v1.
    """
    try:
        repo = _repo_from(raw, repo_path)
        if repo:
            store.capture_task(repo, store.prompt_from_hook_stdin(raw),
                               store.session_from_hook_stdin(raw))
    except Exception:
        pass
    return json.dumps(format_prompt_passthrough())


def capture_constraint(repo_path: str, raw: str) -> str:
    """beforeSubmitPrompt: auto-store 'always/never' directives (write-only; no ack)."""
    try:
        repo = _repo_from(raw, repo_path)
        if repo:
            store.capture_user_constraint(repo, store.prompt_from_hook_stdin(raw),
                                          store.session_from_hook_stdin(raw))
    except Exception:
        pass
    return json.dumps(format_prompt_passthrough())
