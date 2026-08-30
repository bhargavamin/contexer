"""Adapter contract for one AI-assistant integration target.

An adapter is a *module* (duck-typed, no class needed) that exposes:

  NAME: str                       # "claude" | "cursor" | "codex" | "gemini"
  is_present(home: Path) -> bool  # does this tool look installed for the user?
  install(home: Path) -> list[str]    # wire MCP + hooks; return human-facing log lines
  uninstall(home: Path) -> list[str]  # remove MCP + hooks; return log lines
  status_lines(home: Path) -> list[str]  # diagnostic lines for `contexer status`
  notify(text: str) -> dict | None       # hook-output fields carrying a user-facing
                                         # notice, or None when this host has no channel
                                         # the developer (not the model) actually sees

Plus hook entrypoints called from the hook command strings, each returning the
JSON string to print on stdout (never raises — hooks must not crash the host).

This module also holds the shared config-file helpers (_load/_save, hook-group
markers, the /bootstrap command text) used by both cli.py and the adapters.
"""
import json
from importlib import resources
from pathlib import Path

from contexer import store as _store   # module object, not a `from`-import: a value
                                       # patched on contexer.store must resolve at
                                       # CALL time (CLAUDE.md, module boundaries).

_BOOTSTRAP_CMD_MARKER = "managed by contexer"


def _bootstrap_command_text() -> str:
    return resources.files("contexer").joinpath("bootstrap_command.md").read_text(encoding="utf-8")


def _load(path: Path) -> dict:
    # Strict load for mutating paths (install/uninstall): unparseable JSON raises
    # JSONDecodeError, and valid-but-non-object JSON ([], null, 42) raises ValueError —
    # both surface as a clean abort (see cli._run_guarded) instead of an AttributeError
    # mid-install that could leave the config half-written.
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
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
    _store.atomic_write(path, json.dumps(data, indent=2))


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


def _has_exact_command(groups: list, command: str) -> bool:
    """True when a grouped hook list contains exactly ``command``.

    Identity markers are deliberately insufficient for install convergence: a foreign
    hook may mention one without being owned by Contexer.  After stale owned entries are
    removed, only the complete generated command proves that the current hook exists.
    """
    return any(_hook_command(h) == command for grp in groups for h in _hooks_of(grp))


def _filter_hooks(groups: list, remove) -> list:
    """Remove selected hook entries while preserving foreign siblings and group metadata."""
    out = []
    for grp in groups:
        hooks = _hooks_of(grp)
        kept = [h for h in hooks if not remove(h)]
        if not hooks or len(kept) == len(hooks):
            out.append(grp)
        elif kept:
            out.append({**grp, "hooks": kept})
    return out


def _filter_groups(groups: list, markers: list) -> list:
    """Remove owned, matching hook entries without claiming marker-bearing foreign hooks."""
    return _filter_hooks(groups, lambda h: (
        any(marker in str(h) for marker in markers) and _owned_hook(h, markers)))


_OWNER_MARKER = "contexer"
# The pre-package installer imported a top-level ``store`` module, so its commands do not name
# the modern package even though these callables unambiguously identify Contexer. Keep this list
# deliberately narrow: generic markers such as ``sync_memory`` and ``compaction starting`` still
# require the package marker and therefore cannot claim a user's unrelated hook.
_LEGACY_STORE_IDENTITIES = frozenset({
    "get_session_start_context",
    "get_post_compact_context",
})
_NAMESPACED_HOOK_IDENTITIES = frozenset({"claude.capture_task"})
_PACKAGE_OWNERSHIP_MARKERS = (
    "contexer-managed-hook",
    "from contexer",
    "import contexer",
    "~/.contexer",
    "$home/.contexer",
    "mcp__contexer__",
)
_LEGACY_HOOK_TEXT = (
    "contexer: context compaction starting",
    "contexer: context reloaded after compaction",
    "contexer: no context stored",
    "contexer: 3 decision(s) available",
)


def _hook_command(hook) -> str:
    """Command of one hook, tolerating hand-edited shapes: a non-dict hook, or an explicit
    `"command": null` (the `str(... or "")` rule `_in_commands` documents - a plain
    `.get("command", "")` defaults only when the key is ABSENT and returns None here,
    which turns the next `marker in cmd` into a TypeError mid-install)."""
    return str(hook.get("command") or "") if isinstance(hook, dict) else ""


def _owned_hook(hook, ident_markers: list) -> bool:
    """Whether one hook entry carries evidence that Contexer, not merely a marker, owns it."""
    text = str(hook)
    lowered = text.casefold()
    if isinstance(hook, dict) and hook.get("server") == "contexer":
        return True
    if any(marker in lowered for marker in _PACKAGE_OWNERSHIP_MARKERS):
        return True
    if any(marker in lowered for marker in _LEGACY_HOOK_TEXT):
        return True
    cmd = _hook_command(hook)
    if "store." in cmd and any(m in _LEGACY_STORE_IDENTITIES for m in ident_markers):
        return True
    if any(marker in text for marker in _NAMESPACED_HOOK_IDENTITIES):
        return True
    return ("reminder: if you make a significant decision" in lowered
            and "call update_context" in lowered)


def _is_ours(cmd: str, ident_markers: list) -> bool:
    """True when `cmd` is a Contexer hook of this identity: it names the package AND
    carries one of `ident_markers`.

    The owner half is what makes convergence safe to run unconditionally. Some identity
    markers are generic English or a generic function name ("compaction starting",
    "sync_memory"), so a bare marker match reads a user's own hook as ours - and since
    stripping drops the whole GROUP, an unrelated sibling command in it goes too,
    silently and permanently. Every Contexer command names the package (an import, the
    `uv run --directory <clone>` path, or the `contexer-managed-hook` sentinel) in every
    shape convergence has to recognize, including the pre-sentinel and from-source ones,
    so this excludes foreign hooks without excusing any of ours."""
    identities = [m for m in ident_markers if m in cmd]
    if not identities:
        return False
    if _OWNER_MARKER in cmd.casefold():
        return True
    return "store." in cmd and any(m in _LEGACY_STORE_IDENTITIES for m in identities)


def _strip_stale(groups: list, ident_markers: list, current_cmd: str) -> list:
    """Drop each Contexer hook of this identity (see `_is_ours`) whose command differs
    from current_cmd - i.e. a stale version from an older install:
    different phrasing, a from-source `uv run --directory <clone>` path, or a
    pre-sentinel command. Filtering is per hook, not per group: group matchers/metadata,
    current Contexer siblings, and foreign sibling commands all survive."""
    out = []
    for grp in groups:
        hooks = _hooks_of(grp)
        kept = [h for h in hooks
                if not (_is_ours(_hook_command(h), ident_markers)
                        and _hook_command(h) != current_cmd)]
        if not hooks or len(kept) == len(hooks):
            out.append(grp)
        elif kept:
            out.append({**grp, "hooks": kept})
    return out


def _strip_stale_flat(hooks: list, marker: str, current_cmd: str) -> None:
    """`_strip_stale` for a FLAT hook list (Cursor's shape: bare `{type, command}` dicts,
    no groups). Same rule, in place: drop our own hooks of this identity unless the
    command is already the current one."""
    hooks[:] = [h for h in hooks
                if not _is_ours(_hook_command(h), [marker])
                or _hook_command(h) == current_cmd]
