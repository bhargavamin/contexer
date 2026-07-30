"""Adapter contract for one AI-assistant integration target.

An adapter is a *module* (duck-typed, no class needed) that exposes:

  NAME: str                       # "claude" | "cursor" | "codex" | "gemini"
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
    # Strict load for mutating paths (install/uninstall): unparseable JSON raises
    # JSONDecodeError, and valid-but-non-object JSON ([], null, 42) raises ValueError —
    # both surface as a clean abort (see cli._run_guarded) instead of an AttributeError
    # mid-install that could leave the config half-written.
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a JSON object")
    return data


def _load_safe(path: Path) -> dict:
    """Tolerant load for diagnostics: a malformed or non-object JSON file reads as
    empty instead of crashing. Mutating paths (install/uninstall) keep the strict
    _load so a corrupt config fails loudly rather than being silently clobbered."""
    try:
        data = _load(path)
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _is_corrupt(path: Path) -> bool:
    """True when the file exists but is not a JSON object — status() uses this to
    give honest advice (a corrupt config must be fixed, not re-installed over)."""
    if not path.exists():
        return False
    try:
        _load(path)          # raises on unparseable JSON or a non-object payload
        return False
    except (json.JSONDecodeError, OSError, UnicodeDecodeError, ValueError):
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
    """True when any hook in `groups` carries `marker`.

    Matches against `str(hook_dict)` — the dict's *repr* — so it sees every field
    (command, statusMessage, matcher, type) in one shot. The catch: a repr can escape
    quotes. Precisely, repr delimits with `'` unless the string contains `'` and no `"`,
    and escapes only the delimiter. So a `"`-bearing marker is always safe, while a
    `'`-bearing marker breaks exactly when the command ALSO contains `"` — which every
    real hook command does, being `py -c "..."`. That is the trap: `'codex'` matches
    fine in a toy string and never matches in production, so a migration gate keyed on
    it silently re-fires on every install forever. Don't reason it through per marker —
    use `_in_commands` for any marker containing a quote."""
    return any(marker in str(h) for grp in groups for h in _hooks_of(grp))


def _in_commands(groups: list, marker: str) -> bool:
    """True when any hook's `command` string carries `marker`, matched against the raw
    value rather than a repr. Quote-safe, unlike `_in_groups` — use it whenever the
    marker contains `'` or `"` (e.g. a quoted argument that identifies one call site).

    `str(… or "")` rather than a plain `.get("command", "")`: the default only applies
    when the key is ABSENT, so a hand-edited config with `"command": null` (or a list, or
    a number) would raise TypeError mid-install. `_hooks_of` is explicitly documented as
    tolerating hand-edited shapes; this must not be the one place that isn't."""
    return any(marker in str(h.get("command") or "")
               for grp in groups for h in _hooks_of(grp) if isinstance(h, dict))


def _filter_groups(groups: list, markers: list) -> list:
    return [
        grp for grp in groups
        if not any(marker in str(h) for marker in markers for h in _hooks_of(grp))
    ]


def _strip_stale(groups: list, ident_markers: list, current_cmd: str) -> list:
    """Drop any group that is a Contexer hook of this identity (a command containing
    one of ident_markers) but whose command differs from current_cmd — i.e. a stale
    version from an older install: different phrasing, a from-source `uv run
    --directory <clone>` path, or a pre-sentinel command. Keeps the current group and
    every non-Contexer group, so reinstall converges instead of stacking duplicates."""
    out = []
    for grp in groups:
        cmds = [h.get("command", "") for h in _hooks_of(grp)]
        is_this_hook = any(m in c for m in ident_markers for c in cmds)
        if is_this_hook and current_cmd not in cmds:
            continue
        out.append(grp)
    return out
