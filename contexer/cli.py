import json
import shutil
import sys
from pathlib import Path


def _load(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def _save(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def _in_groups(groups: list, marker: str) -> bool:
    return any(marker in str(h) for grp in groups for h in grp.get("hooks", []))


def _filter_groups(groups: list, markers: list) -> list:
    return [
        grp for grp in groups
        if not any(marker in str(h) for marker in markers for h in grp.get("hooks", []))
    ]


def _has_mcp_tool(groups: list, tool: str) -> bool:
    return any(
        any(h.get("type") == "mcp_tool" and h.get("server") == "contexer"
            and h.get("tool") == tool for h in grp.get("hooks", []))
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
        "ctx=store.get_context(sys.argv[1]); "
        "print(json.dumps({'systemMessage': 'Contexer: context reloaded after compaction\\\\n' + ctx}))"
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
    if not _in_groups(poc, "reloaded after compaction"):
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


def uninstall() -> None:
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
            "PostCompact":      ["reloaded after compaction"],
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

    print()
    print("Uninstall complete. Context store (~/.contexer/) was not removed.")
    print("To delete stored context: rm -rf ~/.contexer/")


def main() -> None:
    args = sys.argv[1:]

    if not args:
        from contexer.server import main as _server
        _server()
        return

    cmd = args[0]
    if cmd == "install":
        install()
    elif cmd == "uninstall":
        uninstall()
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        print("Usage: contexer [install|uninstall]", file=sys.stderr)
        sys.exit(1)
