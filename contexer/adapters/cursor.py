"""Cursor integration adapter."""
from pathlib import Path

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
