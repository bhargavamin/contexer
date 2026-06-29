"""OpenAI Codex CLI integration adapter.

Codex's hook system is JSON and uses the *same schema and event names* as Claude Code
(SessionStart / PostToolUse / PreCompact / PostCompact / UserPromptSubmit, with the same
hookSpecificOutput / additionalContext / systemMessage output). So this adapter reaches
near-full Claude parity and reuses Claude's runtime entrypoints verbatim — the hook command
strings call store.get_*/claude.capture_* directly, exactly as the Claude hooks do.

Two things differ from the Claude adapter:
  - the MCP server is registered in ~/.codex/config.toml (TOML), not JSON;
  - hooks live in a separate ~/.codex/hooks.json (JSON, same shape as Claude's settings.json
    `hooks` block).
There is no permissions.allow — Codex approves MCP tools interactively (like Cursor).
"""
import shutil
import sys
import tomllib
from pathlib import Path

from contexer.adapters import base
from contexer.store import _atomic_write

NAME = "codex"


def is_present(home: Path) -> bool:
    return (home / ".codex").exists()


# ── TOML config.toml helpers (surgical: touch only our [mcp_servers.contexer] stanza) ──────

def _stanza_bounds(lines: list[str]) -> tuple[int, int] | None:
    """Line range [start, end) of the [mcp_servers.contexer] table (incl. any sub-tables),
    or None if absent. The block ends at the next table header that is not ours."""
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "[mcp_servers.contexer]":
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        s = lines[j].lstrip()
        if s.startswith("[") and not s.startswith("[mcp_servers.contexer.") \
                and not s.startswith("[mcp_servers.contexer]"):
            end = j
            break
    return start, end


def _set_contexer_stanza(text: str, cmd: str) -> str:
    """Insert or replace just the [mcp_servers.contexer] stanza, leaving the rest of the
    config byte-for-byte intact."""
    escaped = cmd.replace("\\", "\\\\").replace('"', '\\"')
    stanza = f'[mcp_servers.contexer]\ncommand = "{escaped}"\n'
    lines = text.splitlines(keepends=True)
    bounds = _stanza_bounds(lines)
    if bounds:
        start, end = bounds
        return "".join(lines[:start] + [stanza] + lines[end:])
    body = text.rstrip("\n")
    return stanza if not body else body + "\n\n" + stanza


def _remove_contexer_stanza(text: str) -> str:
    """Drop the [mcp_servers.contexer] stanza; return text unchanged if it isn't present."""
    lines = text.splitlines(keepends=True)
    bounds = _stanza_bounds(lines)
    if not bounds:
        return text
    start, end = bounds
    result = "".join(lines[:start] + lines[end:]).rstrip("\n")
    return result + "\n" if result else ""


def _read_config_command(home: Path):
    """The registered contexer MCP command from config.toml, or None. Tolerant — a missing
    or unparseable config reads as 'not registered' rather than crashing diagnostics."""
    path = home / ".codex" / "config.toml"
    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text())
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
        return None
    servers = data.get("mcp_servers", {})
    entry = servers.get("contexer") if isinstance(servers, dict) else None
    return entry.get("command") if isinstance(entry, dict) else None


# ── install / uninstall / status ───────────────────────────────────────────────────────────

def install(home: Path) -> list[str]:
    """Wire the Codex MCP server (config.toml) + hooks (hooks.json). Returns log lines."""
    log: list[str] = []
    python = sys.executable
    contexer_bin = shutil.which("contexer") or "contexer"

    def _py(code: str) -> str:
        return (
            f'REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd) && '
            f'"{python}" -c "{code}" "$REPO"'
        )

    ss_code = (
        "from contexer import store; import json,sys; "
        "repo=sys.argv[1]; store.STORE_DIR.mkdir(exist_ok=True); "
        "store._is_sane_repo(repo) and (store.STORE_DIR/'.current_repo').write_text(repo); "
        "print(json.dumps(store.get_session_start_context(repo, store.source_from_hook_stdin(sys.stdin.read()))))"
    )
    boot_code = (
        "from contexer import store; import json,sys; "
        "result=store.get_bootstrap_context_prompt(sys.argv[1], store.prompt_from_hook_stdin(sys.stdin.read())); "
        "print(json.dumps(result))"
    )
    post_code = (
        "from contexer import store; import json,sys; "
        "print(json.dumps(store.get_post_compact_context(sys.argv[1])))"
    )
    anchor_cmd = (
        "REPO=$(git rev-parse --show-toplevel 2>/dev/null || true); "
        "if [ -n \"$REPO\" ]; then printf '%s' \"$REPO\" > ~/.contexer/.current_repo; fi; "
        "FLAG=\"$HOME/.contexer/.pending_capture\"; "
        "if [ -f \"$FLAG\" ]; then "
        "rm -f \"$FLAG\"; "
        "echo '{\"hookSpecificOutput\": {\"hookEventName\": \"UserPromptSubmit\", "
        "\"additionalContext\": \"Contexer: you wrote or edited files last turn "
        "— call update_context for: (1) any NEW architecture/pattern/constraint/convention decisions; "
        "(2) any EXISTING approach you applied again (same or similar content is fine — "
        "the server deduplicates and tracks repetition without storing a duplicate). "
        "If update_context appears as a deferred tool, first call: "
        "ToolSearch(query=\\\"select:mcp__contexer__update_context\\\")\"}}'; "
        "else echo '{}'; fi"
    )
    cap_task = ('REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd) && '
                f'"{python}" -c "from contexer.adapters import claude; import sys; '
                'print(claude.capture_task(sys.argv[1], sys.stdin.read()))" "$REPO"')
    cap_con = ('REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd) && '
               f'"{python}" -c "from contexer.adapters import claude; import sys; '
               'print(claude.capture_constraint(sys.argv[1], sys.stdin.read()))" "$REPO"')
    cap_rat = ('REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd) && '
               f'"{python}" -c "from contexer.adapters import claude; import sys; '
               'print(claude.rationale(sys.argv[1], sys.stdin.read()))" "$REPO"')

    # MCP server (~/.codex/config.toml) — surgical text edit so the user's plugins,
    # marketplaces, projects, other mcp_servers, and secrets stay byte-for-byte intact.
    config_path = home / ".codex" / "config.toml"
    existing = config_path.read_text() if config_path.exists() else ""
    new_text = _set_contexer_stanza(existing, contexer_bin)
    try:
        tomllib.loads(new_text)
    except tomllib.TOMLDecodeError:
        log.append("  ! ~/.codex/config.toml is not valid TOML — left untouched (fix it, then re-run)")
    else:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(config_path, new_text)
        log.append("  ✓ MCP server registered in ~/.codex/config.toml")

    # Hooks (~/.codex/hooks.json) — same JSON schema and event names as Claude Code.
    hooks_path = home / ".codex" / "hooks.json"
    cfg = base._load(hooks_path)
    hooks = cfg.setdefault("hooks", {})

    ss = hooks.setdefault("SessionStart", [])
    if not base._in_groups(ss, "get_session_start_context"):
        ss.insert(0, {"hooks": [{"type": "command",
            "statusMessage": "Loading session context...", "command": _py(ss_code)}]})

    put = hooks.setdefault("PostToolUse", [])
    if not base._in_groups(put, ".pending_capture"):
        put.append({"matcher": "Write|Edit", "hooks": [{"type": "command",
            "command": "touch ~/.contexer/.pending_capture && echo '{}'"}]})

    st = hooks.setdefault("Stop", [])
    if not base._in_groups(st, ".pending_capture"):
        stop_reminder = (
            "Contexer: you wrote or edited files this turn "
            "\\u2014 call update_context NOW for any architecture/pattern/constraint/convention "
            "decisions before finishing this turn. "
            "If update_context is a deferred tool, first call: "
            "ToolSearch(query=\\\"select:mcp__contexer__update_context\\\")"
        )
        st.append({"hooks": [{"type": "command",
            "command": (
                "FLAG=\"$HOME/.contexer/.pending_capture\"; "
                "if [ -f \"$FLAG\" ]; then "
                "rm -f \"$FLAG\"; "
                f"echo '{{\"hookSpecificOutput\": {{\"hookEventName\": \"Stop\", "
                f"\"additionalContext\": \"{stop_reminder}\"}}}}'; "
                "else echo '{}'; fi"
            )}]})

    pc = hooks.setdefault("PreCompact", [])
    if not base._in_groups(pc, "compaction starting"):
        pc.append({"hooks": [{"type": "command",
            "statusMessage": "Saving decisions before compact...",
            "command": "echo '{\"systemMessage\": \"Contexer: context compaction starting — call update_context for any decisions not yet stored\"}'"}]})

    poc = hooks.setdefault("PostCompact", [])
    if not base._in_groups(poc, "get_post_compact_context"):
        poc.append({"hooks": [{"type": "command",
            "statusMessage": "Reloading context after compact...", "command": _py(post_code)}]})

    ups = hooks.setdefault("UserPromptSubmit", [])
    if not base._in_groups(ups, ".pending_capture"):
        ups.insert(0, {"hooks": [{"type": "command",
            "statusMessage": "Anchoring repo context...", "command": anchor_cmd}]})
    # `once` mirrors Claude. If Codex ignores it these degrade gracefully: the bootstrap
    # offer is a silent {} once context exists, and capture_task just tracks the latest prompt.
    if not base._in_groups(ups, "get_bootstrap_context_prompt"):
        ups.append({"hooks": [{"type": "command", "once": True,
            "statusMessage": "Checking bootstrap context...", "command": _py(boot_code)}]})
    if not base._in_groups(ups, "claude.capture_task"):
        ups.append({"hooks": [{"type": "command", "once": True,
            "statusMessage": "Capturing task...", "command": cap_task}]})
    if not base._in_groups(ups, "claude.capture_constraint"):
        ups.append({"hooks": [{"type": "command",
            "statusMessage": "Checking for constraint directives...", "command": cap_con}]})
    if not base._in_groups(ups, "claude.rationale"):
        ups.append({"hooks": [{"type": "command",
            "statusMessage": "Checking for relevant decisions...", "command": cap_rat}]})

    base._save(hooks_path, cfg)
    log.append("  ✓ Hooks registered in ~/.codex/hooks.json")
    log.append("  ℹ Codex will ask once to approve Contexer's MCP tools — approve to allow.")
    return log


_EVENT_MARKERS = {
    "SessionStart":     ["get_session_start_context"],
    "PostToolUse":      [".pending_capture"],
    "Stop":             [".pending_capture"],
    "PreCompact":       ["compaction starting"],
    "PostCompact":      ["get_post_compact_context"],
    "UserPromptSubmit": [".current_repo", ".pending_capture", "get_bootstrap_context_prompt",
                         "claude.capture_task", "claude.capture_constraint", "claude.rationale"],
}


def uninstall(home: Path) -> list[str]:
    """Remove the Codex MCP server + hooks. Does NOT touch the store (--purge's concern)."""
    log: list[str] = []

    config_path = home / ".codex" / "config.toml"
    if config_path.exists():
        text = config_path.read_text()
        new_text = _remove_contexer_stanza(text)
        if new_text == text:
            log.append("  - No MCP server entry found in ~/.codex/config.toml")
        else:
            try:
                tomllib.loads(new_text)
            except tomllib.TOMLDecodeError:
                log.append("  ! ~/.codex/config.toml is not valid TOML — left untouched")
            else:
                _atomic_write(config_path, new_text)
                log.append("  ✓ MCP server removed from ~/.codex/config.toml")

    hooks_path = home / ".codex" / "hooks.json"
    if hooks_path.exists():
        cfg = base._load(hooks_path)
        hooks = cfg.get("hooks", {})
        changed = False
        for event, markers in _EVENT_MARKERS.items():
            before = hooks.get(event, [])
            after = base._filter_groups(before, markers)
            if after != before:
                changed = True
                if after:
                    hooks[event] = after
                else:
                    hooks.pop(event, None)
        if changed:
            base._save(hooks_path, cfg)
            log.append("  ✓ Hooks removed from ~/.codex/hooks.json")
        else:
            log.append("  - No Contexer hooks found in ~/.codex/hooks.json")

    return log


def _mcp_and_hooks_ok(home: Path) -> tuple:
    """Read the Codex config (tolerant of corruption) and report (mcp_command, hooks_ok).
    Shared by status_lines and is_installed."""
    mcp = _read_config_command(home)
    raw_hooks = base._load_safe(home / ".codex" / "hooks.json").get("hooks", {})
    hooks = raw_hooks if isinstance(raw_hooks, dict) else {}

    def _groups(event: str) -> list:
        v = hooks.get(event, [])
        return v if isinstance(v, list) else []

    hooks_ok = (base._in_groups(_groups("SessionStart"), "get_session_start_context")
                and base._in_groups(_groups("UserPromptSubmit"), "claude.rationale"))
    return mcp, hooks_ok


def status_lines(home: Path) -> list[str]:
    mcp, hooks_ok = _mcp_and_hooks_ok(home)
    return [
        "  [codex]",
        f"    MCP server: {'registered → ' + mcp if mcp else 'NOT registered'}",
        f"    hooks:      {'installed' if hooks_ok else 'missing or partial'}",
    ]


def is_installed(home: Path) -> bool:
    """True when both the MCP server and the core hooks are wired for Codex."""
    mcp, hooks_ok = _mcp_and_hooks_ok(home)
    return bool(mcp) and hooks_ok
