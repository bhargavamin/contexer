import json
import shutil
import sys
import time
from importlib.metadata import PackageNotFoundError, version as _dist_version
from pathlib import Path

from contexer.store import _atomic_write

USAGE = """contexer — persistent context for Claude Code

Usage: contexer [command]

Commands:
  (no args)     Run the MCP server over stdio (how Claude Code launches it).
  install       Register the MCP server + hooks in your global Claude config.
  uninstall     Remove the MCP server + hooks. Add --purge to also delete the store.
  reinstall     Re-sync config (uninstall + install). Does NOT rebuild the binary.
  status        Show install state: version, binary path, MCP/hooks, store summary.
  version       Print the installed version.
  help          Show this message.

Flags:
  -V, --version   Same as `version`.
  -h, --help      Same as `help`.
  --purge         With `uninstall`: also delete ~/.contexer/ (stored context).

To upgrade the program itself (rebuild the binary):
  uv tool install --reinstall contexer
"""


def _version() -> str:
    try:
        return _dist_version("contexer")
    except PackageNotFoundError:
        return "unknown (not installed as a package)"


def _usage(stream=None) -> None:
    print(USAGE, file=stream or sys.stdout)


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


def _has_mcp_tool(groups: list, tool: str) -> bool:
    return any(
        any(isinstance(h, dict) and h.get("type") == "mcp_tool"
            and h.get("server") == "contexer" and h.get("tool") == tool
            for h in _hooks_of(grp))
        for grp in groups
    )


def install() -> None:
    home = Path.home()
    python = sys.executable

    def _py(code: str) -> str:
        return (
            f'REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd) && '
            f'"{python}" -c "{code}" "$REPO"'
        )

    ss_code = (
        "from contexer import store; import json,sys; "
        "store.STORE_DIR.mkdir(exist_ok=True); "
        "(store.STORE_DIR/'.current_repo').write_text(sys.argv[1]); "
        "print(json.dumps(store.get_session_start_context(sys.argv[1])))"
    )
    boot_code = (
        "from contexer import store; import json,sys; "
        "result=store.get_bootstrap_context_prompt(sys.argv[1]); "
        "print(json.dumps(result))"
    )
    post_code = (
        "from contexer import store; import json,sys; "
        "print(json.dumps(store.get_post_compact_context(sys.argv[1])))"
    )

    anchor_cmd = (
        "REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd) && "
        "printf '%s' \"$REPO\" > ~/.contexer/.current_repo && "
        "FLAG=\"$HOME/.contexer/.pending_capture\" && "
        "if [ -f \"$FLAG\" ]; then "
        "rm -f \"$FLAG\" && "
        "echo '{\"hookSpecificOutput\": {\"hookEventName\": \"UserPromptSubmit\", "
        "\"additionalContext\": \"Contexer: you wrote or edited files last turn "
        "— call update_context for any architecture, pattern, constraint, or convention decisions before responding.\"}}'; "
        "else echo '{}'; fi"
    )

    contexer_bin = shutil.which("contexer") or "contexer"

    # MCP server (~/.claude.json)
    claude_json = home / ".claude.json"
    claude = _load(claude_json)
    claude.setdefault("mcpServers", {})["contexer"] = {
        "type": "stdio",
        "command": contexer_bin,
    }
    _save(claude_json, claude)
    print("  ✓ MCP server registered in ~/.claude.json")

    # Hooks and permissions (~/.claude/settings.json)
    settings_json = home / ".claude" / "settings.json"
    settings = _load(settings_json)
    hooks = settings.setdefault("hooks", {})

    ss = hooks.setdefault("SessionStart", [])
    if not _in_groups(ss, "get_session_start_context"):
        ss.insert(0, {"hooks": [{"type": "command",
            "statusMessage": "Loading session context...",
            "command": _py(ss_code)}]})

    # PostToolUse: set a flag after Write/Edit so next prompt reminds Claude to call update_context
    put = hooks.setdefault("PostToolUse", [])
    if not _in_groups(put, ".pending_capture"):
        put.append({"matcher": "Write|Edit", "hooks": [{"type": "command",
            "command": "touch ~/.contexer/.pending_capture && echo '{}'"}]})

    pc = hooks.setdefault("PreCompact", [])
    if not _in_groups(pc, "compaction starting"):
        pc.append({"hooks": [{"type": "command",
            "statusMessage": "Saving decisions before compact...",
            "command": "echo '{\"systemMessage\": \"Contexer: context compaction starting — call update_context for any decisions not yet stored\"}'"}]})

    poc = hooks.setdefault("PostCompact", [])
    # Migrate: old hook used get_context (no bootstrap offer); replace with get_post_compact_context
    if _in_groups(poc, "reloaded after compaction") and not _in_groups(poc, "get_post_compact_context"):
        hooks["PostCompact"] = _filter_groups(poc, ["reloaded after compaction"])
        poc = hooks["PostCompact"]
    if not _in_groups(poc, "get_post_compact_context"):
        poc.append({"hooks": [{"type": "command",
            "statusMessage": "Reloading context after compact...",
            "command": _py(post_code)}]})

    ups = hooks.setdefault("UserPromptSubmit", [])

    # Replace old anchor hook (without .pending_capture logic) with new one
    if _in_groups(ups, ".current_repo") and not _in_groups(ups, ".pending_capture"):
        ups = _filter_groups(ups, [".current_repo"])
        hooks["UserPromptSubmit"] = ups

    if not _in_groups(ups, ".pending_capture"):
        ups.insert(0, {"hooks": [{"type": "command",
            "statusMessage": "Anchoring repo context...",
            "command": anchor_cmd}]})

    if not _in_groups(ups, "get_bootstrap_context_prompt"):
        ups.append({"hooks": [{"type": "command", "once": True,
            "statusMessage": "Checking bootstrap context...",
            "command": _py(boot_code)}]})

    if not _has_mcp_tool(ups, "capture_context"):
        ups.append({"hooks": [{"type": "mcp_tool", "server": "contexer",
            "tool": "capture_context",
            "input": {"repo_path": "", "description": "${prompt}"},
            "once": True, "statusMessage": "Capturing task..."}]})

    if not _has_mcp_tool(ups, "capture_user_constraint"):
        ups.append({"hooks": [{"type": "mcp_tool", "server": "contexer",
            "tool": "capture_user_constraint",
            "input": {"repo_path": "", "prompt": "${prompt}"},
            "statusMessage": "Checking for constraint directives..."}]})

    if not _has_mcp_tool(ups, "get_context_for_prompt"):
        ups.append({"hooks": [{"type": "mcp_tool", "server": "contexer",
            "tool": "get_context_for_prompt",
            "input": {"repo_path": "", "prompt": "${prompt}"},
            "statusMessage": "Checking for relevant decisions..."}]})

    allow = settings.setdefault("permissions", {}).setdefault("allow", [])
    for p in [
        "mcp__contexer__capture_context", "mcp__contexer__update_context",
        "mcp__contexer__get_context", "mcp__contexer__bootstrap_context",
        "mcp__contexer__get_context_for_prompt",
        "mcp__contexer__update_global_context", "mcp__contexer__get_global_context",
        "mcp__contexer__capture_user_constraint",
    ]:
        if p not in allow:
            allow.append(p)

    (home / ".contexer").mkdir(exist_ok=True)
    _save(settings_json, settings)
    print("  ✓ Hooks and permissions written to ~/.claude/settings.json")
    print()
    print("Done. Restart Claude Code and open any git repo to activate Contexer.")


def uninstall(purge: bool = False) -> None:
    home = Path.home()
    changed = False

    claude_json = home / ".claude.json"
    if claude_json.exists():
        claude = _load(claude_json)
        removed = claude.get("mcpServers", {}).pop("contexer", None)
        if removed:
            _save(claude_json, claude)
            print("  ✓ MCP server removed from ~/.claude.json")
            changed = True
        else:
            print("  - No MCP server entry found in ~/.claude.json")

    settings_json = home / ".claude" / "settings.json"
    if settings_json.exists():
        settings = _load(settings_json)
        hooks = settings.get("hooks", {})

        event_markers = {
            "SessionStart":     ["get_session_start_context"],
            "PostToolUse":      [".pending_capture"],
            "PreCompact":       ["compaction starting"],
            "PostCompact":      ["reloaded after compaction", "get_post_compact_context"],
            "UserPromptSubmit": [".current_repo", ".pending_capture", "get_bootstrap_context_prompt"],
        }
        for event, markers in event_markers.items():
            before = hooks.get(event, [])
            after = _filter_groups(before, markers)
            if event == "UserPromptSubmit":
                after = [
                    grp for grp in after
                    if not any(
                        h.get("type") == "mcp_tool" and h.get("server") == "contexer"
                        for h in grp.get("hooks", [])
                    )
                ]
            if after != before:
                changed = True
                if after:
                    hooks[event] = after
                else:
                    hooks.pop(event, None)

        allow = settings.get("permissions", {}).get("allow", [])
        cleaned = [p for p in allow if "contexer" not in p]
        if cleaned != allow:
            settings["permissions"]["allow"] = cleaned
            changed = True

        if changed:
            _save(settings_json, settings)
            print("  ✓ Hooks and permissions removed from ~/.claude/settings.json")
        else:
            print("  - No Contexer hooks found in ~/.claude/settings.json")

    store_dir = home / ".contexer"
    print()
    if purge:
        if store_dir.exists():
            shutil.rmtree(store_dir)
            print(f"  ✓ Removed {store_dir} (stored context purged)")
        else:
            print(f"  - No store to purge ({store_dir} absent)")
        print("Uninstall complete.")
    else:
        print("Uninstall complete. Context store (~/.contexer/) was not removed.")
        print("To delete stored context too: contexer uninstall --purge")


def version() -> None:
    print(f"contexer {_version()}")


def reinstall() -> None:
    print("Re-syncing Contexer config (uninstall + install)...\n")
    uninstall()
    print()
    install()
    print()
    print("Note: this only re-synced the MCP/hook config. To upgrade the program itself,")
    print("run `uv tool install --reinstall contexer`, then restart Claude Code.")


def status() -> None:
    home = Path.home()
    bin_path = shutil.which("contexer") or "(not on PATH)"

    # status is a diagnostic — it must survive any state it might be asked to
    # diagnose, including corrupt config files and hand-edited entries.
    mcp = _load_safe(home / ".claude.json").get("mcpServers", {}).get("contexer")
    raw_hooks = _load_safe(home / ".claude" / "settings.json").get("hooks", {})
    hooks = raw_hooks if isinstance(raw_hooks, dict) else {}

    def _groups(event: str) -> list:
        v = hooks.get(event, [])
        return v if isinstance(v, list) else []

    hooks_ok = (_in_groups(_groups("SessionStart"), "get_session_start_context")
                and _has_mcp_tool(_groups("UserPromptSubmit"), "get_context_for_prompt"))

    store_dir = home / ".contexer"
    swept = 0
    if store_dir.exists():
        # Sweep temp files leaked by interrupted atomic writes (hard crash between
        # mkstemp and os.replace). Never matched by the *.json glob below. The age
        # gate keeps us from unlinking a temp another process is writing right now —
        # that would make its os.replace fail and lose the save.
        for tmp in store_dir.glob("*.tmp"):
            try:
                if time.time() - tmp.stat().st_mtime < 3600:
                    continue
                tmp.unlink()
                swept += 1
            except OSError:
                pass

    def _entry_count(p: Path) -> int:
        entries = _load_safe(p).get("entries", [])
        return len(entries) if isinstance(entries, list) else 0

    stores = sorted(store_dir.glob("*.json")) if store_dir.exists() else []
    entries = sum(_entry_count(p) for p in stores)
    current = store_dir / ".current_repo"
    mcp_cmd = mcp.get("command", "?") if isinstance(mcp, dict) else "?"

    print(f"contexer {_version()}")
    print(f"  binary:       {bin_path}")
    print(f"  MCP server:   {'registered → ' + mcp_cmd if mcp else 'NOT registered'}")
    print(f"  hooks:        {'installed' if hooks_ok else 'missing or partial'}")
    print(f"  store dir:    {store_dir}{'' if store_dir.exists() else ' (absent)'}")
    print(f"  repo stores:  {len(stores)} ({entries} entries total)")
    if swept:
        print(f"  cleaned:      {swept} stale temp file(s) from interrupted writes")
    if current.exists():
        try:
            print(f"  current repo: {current.read_text().strip()}")
        except OSError:
            print("  current repo: (unreadable)")

    corrupt = [p for p in (home / ".claude.json", home / ".claude" / "settings.json")
               if _is_corrupt(p)]
    if corrupt:
        for p in corrupt:
            print(f"\n  WARNING: {p} exists but is not valid JSON — fix or remove it.")
        print("  (`contexer install` fails loudly on a corrupt file rather than overwrite it.)")
    elif not (mcp and hooks_ok):
        print("\n  Not fully installed — run `contexer install`.")


def main() -> None:
    args = sys.argv[1:]

    if not args:
        from contexer.server import main as _server
        _server()
        return

    cmd, rest = args[0], args[1:]
    if cmd in ("version", "--version", "-V"):
        version()
    elif cmd in ("help", "--help", "-h"):
        _usage()
    elif cmd == "install":
        install()
    elif cmd == "uninstall":
        uninstall(purge="--purge" in rest)
    elif cmd == "reinstall":
        reinstall()
    elif cmd == "status":
        status()
    else:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        _usage(sys.stderr)
        sys.exit(1)
