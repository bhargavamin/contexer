"""Adapter contract for one AI-assistant integration target.

An adapter is a *module* (duck-typed, no class needed) that exposes:

  NAME: str                       # "claude" | "cursor"
  is_present(home: Path) -> bool  # does this tool look installed for the user?
  install(home: Path) -> list[str]    # wire MCP + hooks; return human-facing log lines
  uninstall(home: Path) -> list[str]  # remove MCP + hooks; return log lines
  status_lines(home: Path) -> list[str]  # diagnostic lines for `contexer status`

Plus hook entrypoints called from the hook command strings, each returning the
JSON string to print on stdout (never raises — hooks must not crash the host).

This module also holds the shared config-file helpers (_load/_save, hook-group
markers, the /bootstrap command text) used by both cli.py and the adapters.
"""
import json
from importlib import resources
from pathlib import Path

from contexer.store import _atomic_write

_BOOTSTRAP_CMD_MARKER = "managed by contexer"


def _bootstrap_command_text() -> str:
    return resources.files("contexer").joinpath("bootstrap_command.md").read_text()


def _load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _load_safe(path: Path) -> dict:
    """Tolerant load for diagnostics: a malformed or non-object JSON file reads as
    empty instead of crashing. Mutating paths (install/uninstall) keep the strict
    _load so a corrupt config fails loudly rather than being silently clobbered."""
    try:
        data = _load(path)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _is_corrupt(path: Path) -> bool:
    """True when the file exists but is not a JSON object — status() uses this to
    give honest advice (a corrupt config must be fixed, not re-installed over)."""
    if not path.exists():
        return False
    try:
        return not isinstance(_load(path), dict)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return True


def _save(path: Path, data: dict) -> None:
    # Atomic for the same reason as the store: a torn ~/.claude.json or
    # settings.json would break all of Claude Code, not just contexer.
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(path, json.dumps(data, indent=2))


def _hooks_of(grp) -> list:
    """Hook list of one group, tolerating hand-edited shapes (non-dict group,
    non-list hooks value) — used by status() on configs it must not crash on."""
    hooks = grp.get("hooks", []) if isinstance(grp, dict) else []
    return hooks if isinstance(hooks, list) else []


def _in_groups(groups: list, marker: str) -> bool:
    return any(marker in str(h) for grp in groups for h in _hooks_of(grp))


def _filter_groups(groups: list, markers: list) -> list:
    return [
        grp for grp in groups
        if not any(marker in str(h) for marker in markers for h in _hooks_of(grp))
    ]
