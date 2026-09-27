"""Host boundary for Claude-only command registrations; intentionally stdlib-only."""
import json
import subprocess
import sys


def run_claude() -> None:
    """Skip Claude hooks imported by Cursor, otherwise preserve their shell/stdin contract.

    Identify the application from its event envelope, never its model or terminal env:
    Claude Code may itself be launched in Cursor's terminal. Unknown/legacy envelopes
    continue to the existing hook. The command is installer-generated argv, not input
    from the event; prompt text is passed only on stdin.
    """
    raw = sys.stdin.buffer.read()
    try:
        payload = json.loads(raw)
    except (ValueError, UnicodeError):
        payload = None
    if isinstance(payload, dict) and (
        "cursor_version" in payload
        or {"conversation_id", "generation_id", "workspace_roots"} <= payload.keys()
    ):
        print("{}")
        return
    result = subprocess.run(sys.argv[1], shell=True, input=raw)
    raise SystemExit(result.returncode)
