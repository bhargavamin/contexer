#!/usr/bin/env bash
# Contexer installer — wires up MCP server and global hooks for Claude Code.
#
# Usage after PyPI install:   bash scripts/install.sh
# Usage after git clone:      bash /path/to/contexer/scripts/install.sh
#
# Requires: uv (https://docs.astral.sh/uv/getting-started/installation/)
set -euo pipefail

# ── Detect install mode ───────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if command -v contexer &>/dev/null && uv tool list 2>/dev/null | grep -q "^contexer"; then
    INSTALL_MODE="pypi"
    TOOL_PYTHON="$(uv tool dir)/contexer/bin/python"
    if [[ ! -x "$TOOL_PYTHON" ]]; then
        echo "Error: uv tool Python not found at $TOOL_PYTHON"
        echo "Try: uv tool install contexer"
        exit 1
    fi
    echo "Installing Contexer (PyPI install detected)"
else
    INSTALL_MODE="dev"
    if ! command -v uv &>/dev/null; then
        echo "Error: uv is required."
        echo "Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
        exit 1
    fi
    echo "Installing Contexer from: $REPO_DIR"
    echo "→ Installing dependencies..."
    uv sync --directory "$REPO_DIR" --quiet
fi

echo "→ Configuring Claude Code..."

if [[ "$INSTALL_MODE" == "pypi" ]]; then
    "$TOOL_PYTHON" - <<PYEOF
import json, sys, os
from pathlib import Path
from contexer import store

HOME = Path.home()
TOOL_PYTHON = sys.executable

def _load(path):
    return json.loads(path.read_text()) if path.exists() else {}

def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))

def _in_groups(groups, marker):
    return any(marker in str(h) for grp in groups for h in grp.get("hooks", []))

def _filter_groups(groups, markers):
    return [
        grp for grp in groups
        if not any(marker in str(h) for marker in markers for h in grp.get("hooks", []))
    ]

def _has_mcp_tool(groups, tool):
    return any(
        any(h.get("type") == "mcp_tool" and h.get("server") == "contexer"
            and h.get("tool") == tool for h in grp.get("hooks", []))
        for grp in groups
    )

def _py(code):
    return (
        f'REPO=\$(git rev-parse --show-toplevel 2>/dev/null || pwd) && '
        f'"{TOOL_PYTHON}" -c "{code}" "\$REPO"'
    )

_ss_code   = "from contexer import store; import json,sys; store.STORE_DIR.mkdir(exist_ok=True); (store.STORE_DIR/'.current_repo').write_text(sys.argv[1]); print(json.dumps(store.get_session_start_context(sys.argv[1])))"
_boot_code = "from contexer import store; import json,sys; result=store.get_bootstrap_context_prompt(sys.argv[1]); print(json.dumps(result))"
_post_code = "from contexer import store; import json,sys; ctx=store.get_context(sys.argv[1]); print(json.dumps({'systemMessage': 'Contexer: context reloaded after compaction\\\\n' + ctx}))"

_anchor_cmd = (
    "REPO=\$(git rev-parse --show-toplevel 2>/dev/null || pwd) && "
    "printf '%s' \"\$REPO\" > ~/.contexer/.current_repo && "
    "FLAG=\"\$HOME/.contexer/.pending_capture\" && "
    "if [ -f \"\$FLAG\" ]; then "
    "rm -f \"\$FLAG\" && "
    "echo '{\"hookSpecificOutput\": {\"hookEventName\": \"UserPromptSubmit\", "
    "\"additionalContext\": \"Contexer: you wrote or edited files last turn "
    "\xe2\x80\x94 call update_context for any architecture, pattern, constraint, or convention decisions before responding.\"}}'; "
    "else echo '{}'; fi"
)

# ── MCP server (~/.claude.json) ───────────────────────────────────────────────
claude_json = HOME / ".claude.json"
claude = _load(claude_json)
claude.setdefault("mcpServers", {})["contexer"] = {
    "type": "stdio", "command": "contexer",
}
_save(claude_json, claude)
print("  ✓ MCP server registered in ~/.claude.json")

# ── Global hooks (~/.claude/settings.json) ────────────────────────────────────
settings_json = HOME / ".claude" / "settings.json"
settings = _load(settings_json)
hooks = settings.setdefault("hooks", {})

ss = hooks.setdefault("SessionStart", [])
if not _in_groups(ss, "get_session_start_context"):
    ss.insert(0, {"hooks": [{"type": "command",
        "statusMessage": "Loading session context...",
        "command": _py(_ss_code)}]})

put = hooks.setdefault("PostToolUse", [])
if not _in_groups(put, ".pending_capture"):
    put.append({"matcher": "Write|Edit", "hooks": [{"type": "command",
        "command": "touch ~/.contexer/.pending_capture && echo '{}'"}]})

pc = hooks.setdefault("PreCompact", [])
if not _in_groups(pc, "compaction starting"):
    pc.append({"hooks": [{"type": "command",
        "statusMessage": "Saving decisions before compact...",
        "command": 'echo \'{"systemMessage": "Contexer: context compaction starting — call update_context for any decisions not yet stored"}\''}]})

poc = hooks.setdefault("PostCompact", [])
if not _in_groups(poc, "reloaded after compaction"):
    poc.append({"hooks": [{"type": "command",
        "statusMessage": "Reloading context after compact...",
        "command": _py(_post_code)}]})

ups = hooks.setdefault("UserPromptSubmit", [])
if _in_groups(ups, ".current_repo") and not _in_groups(ups, ".pending_capture"):
    ups = _filter_groups(ups, [".current_repo"])
    hooks["UserPromptSubmit"] = ups

if not _in_groups(ups, ".pending_capture"):
    ups.insert(0, {"hooks": [{"type": "command",
        "statusMessage": "Anchoring repo context...",
        "command": _anchor_cmd}]})

if not _in_groups(ups, "get_bootstrap_context_prompt"):
    ups.append({"hooks": [{"type": "command", "once": True,
        "statusMessage": "Checking bootstrap context...",
        "command": _py(_boot_code)}]})

if not _has_mcp_tool(ups, "capture_context"):
    ups.append({"hooks": [{"type": "mcp_tool", "server": "contexer", "tool": "capture_context",
        "input": {"repo_path": "", "description": "\${prompt}"},
        "once": True, "statusMessage": "Capturing task..."}]})

if not _has_mcp_tool(ups, "capture_user_constraint"):
    ups.append({"hooks": [{"type": "mcp_tool", "server": "contexer", "tool": "capture_user_constraint",
        "input": {"repo_path": "", "prompt": "\${prompt}"},
        "statusMessage": "Checking for constraint directives..."}]})

if not _has_mcp_tool(ups, "get_context_for_prompt"):
    ups.append({"hooks": [{"type": "mcp_tool", "server": "contexer", "tool": "get_context_for_prompt",
        "input": {"repo_path": "", "prompt": "\${prompt}"},
        "statusMessage": "Checking for relevant decisions..."}]})

# ── Permissions ───────────────────────────────────────────────────────────────
allow = settings.setdefault("permissions", {}).setdefault("allow", [])
for p in ["mcp__contexer__capture_context", "mcp__contexer__update_context",
          "mcp__contexer__get_context", "mcp__contexer__bootstrap_context",
          "mcp__contexer__get_context_for_prompt",
          "mcp__contexer__update_global_context", "mcp__contexer__get_global_context",
          "mcp__contexer__capture_user_constraint"]:
    if p not in allow:
        allow.append(p)

# ── Store directory ───────────────────────────────────────────────────────────
(HOME / ".contexer").mkdir(exist_ok=True)

_save(settings_json, settings)
print("  ✓ Hooks and permissions written to ~/.claude/settings.json")
PYEOF

else
    # Dev / git-clone install path
    D="$REPO_DIR"
    uv run --directory "$D" python3 - "$D" <<'PYEOF'
import json, sys
from pathlib import Path

D = sys.argv[1]  # Contexer install directory
HOME = Path.home()

def _load(path):
    return json.loads(path.read_text()) if path.exists() else {}

def _save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))

def _in_groups(groups, marker):
    return any(marker in str(h) for grp in groups for h in grp.get("hooks", []))

def _filter_groups(groups, markers):
    return [
        grp for grp in groups
        if not any(marker in str(h) for marker in markers for h in grp.get("hooks", []))
    ]

def _has_mcp_tool(groups, tool):
    return any(
        any(h.get("type") == "mcp_tool" and h.get("server") == "contexer"
            and h.get("tool") == tool for h in grp.get("hooks", []))
        for grp in groups
    )

_ss_code = (
    f"import sys,json; sys.path.insert(0,'{D}'); "
    "from contexer import store; store.STORE_DIR.mkdir(exist_ok=True); "
    "(store.STORE_DIR/'.current_repo').write_text(sys.argv[1]); "
    "print(json.dumps(store.get_session_start_context(sys.argv[1])))"
)
_bootstrap_code = (
    f"import sys,json; sys.path.insert(0,'{D}'); "
    "from contexer import store; result=store.get_bootstrap_context_prompt(sys.argv[1]); "
    "print(json.dumps(result))"
)
_post_code = (
    f"import sys,json; sys.path.insert(0,'{D}'); "
    "from contexer import store; ctx=store.get_context(sys.argv[1]); "
    "print(json.dumps({'systemMessage': 'Contexer: context reloaded after compaction\\n' + ctx}))"
)

_anchor_cmd = (
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

def _uv(code):
    return (
        f'REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd) && '
        f'uv run --directory {D} python -c "{code}" "$REPO"'
    )

# ── MCP server (~/.claude.json) ───────────────────────────────────────────────
claude_json = HOME / ".claude.json"
claude = _load(claude_json)
claude.setdefault("mcpServers", {})["contexer"] = {
    "type": "stdio", "command": "uv",
    "args": ["run", "--directory", D, "python", "server.py"],
}
_save(claude_json, claude)
print("  ✓ MCP server registered in ~/.claude.json")

# ── Global hooks (~/.claude/settings.json) ────────────────────────────────────
settings_json = HOME / ".claude" / "settings.json"
settings = _load(settings_json)
hooks = settings.setdefault("hooks", {})

ss = hooks.setdefault("SessionStart", [])
if not _in_groups(ss, "get_session_start_context"):
    ss.insert(0, {"hooks": [{"type": "command",
        "statusMessage": "Loading session context...",
        "command": _uv(_ss_code)}]})

put = hooks.setdefault("PostToolUse", [])
if not _in_groups(put, ".pending_capture"):
    put.append({"matcher": "Write|Edit", "hooks": [{"type": "command",
        "command": "touch ~/.contexer/.pending_capture && echo '{}'"}]})

pc = hooks.setdefault("PreCompact", [])
if not _in_groups(pc, "compaction starting"):
    pc.append({"hooks": [{"type": "command",
        "statusMessage": "Saving decisions before compact...",
        "command": 'echo \'{"systemMessage": "Contexer: context compaction starting — call update_context for any decisions not yet stored"}\''}]})

poc = hooks.setdefault("PostCompact", [])
if not _in_groups(poc, "reloaded after compaction"):
    poc.append({"hooks": [{"type": "command",
        "statusMessage": "Reloading context after compact...",
        "command": _uv(_post_code)}]})

ups = hooks.setdefault("UserPromptSubmit", [])
if _in_groups(ups, ".current_repo") and not _in_groups(ups, ".pending_capture"):
    ups = _filter_groups(ups, [".current_repo"])
    hooks["UserPromptSubmit"] = ups

if not _in_groups(ups, ".pending_capture"):
    ups.insert(0, {"hooks": [{"type": "command",
        "statusMessage": "Anchoring repo context...",
        "command": _anchor_cmd}]})

if not _in_groups(ups, "get_bootstrap_context_prompt"):
    ups.append({"hooks": [{"type": "command", "once": True,
        "statusMessage": "Checking bootstrap context...",
        "command": _uv(_bootstrap_code)}]})

if not _has_mcp_tool(ups, "capture_context"):
    ups.append({"hooks": [{"type": "mcp_tool", "server": "contexer", "tool": "capture_context",
        "input": {"repo_path": "", "description": "${prompt}"},
        "once": True, "statusMessage": "Capturing task..."}]})

if not _has_mcp_tool(ups, "capture_user_constraint"):
    ups.append({"hooks": [{"type": "mcp_tool", "server": "contexer", "tool": "capture_user_constraint",
        "input": {"repo_path": "", "prompt": "${prompt}"},
        "statusMessage": "Checking for constraint directives..."}]})

if not _has_mcp_tool(ups, "get_context_for_prompt"):
    ups.append({"hooks": [{"type": "mcp_tool", "server": "contexer", "tool": "get_context_for_prompt",
        "input": {"repo_path": "", "prompt": "${prompt}"},
        "statusMessage": "Checking for relevant decisions..."}]})

# ── Permissions ───────────────────────────────────────────────────────────────
allow = settings.setdefault("permissions", {}).setdefault("allow", [])
for p in ["mcp__contexer__capture_context", "mcp__contexer__update_context",
          "mcp__contexer__get_context", "mcp__contexer__bootstrap_context",
          "mcp__contexer__get_context_for_prompt",
          "mcp__contexer__update_global_context", "mcp__contexer__get_global_context",
          "mcp__contexer__capture_user_constraint"]:
    if p not in allow:
        allow.append(p)

# ── Store directory ───────────────────────────────────────────────────────────
(HOME / ".contexer").mkdir(exist_ok=True)

_save(settings_json, settings)
print("  ✓ Hooks and permissions written to ~/.claude/settings.json")
PYEOF
fi

echo ""
echo "Contexer installed."
echo ""
echo "  Next: restart Claude Code (or start a new session) in any git repo."
echo "  First session: Claude will offer a quick bootstrap setup (opt-in)."
echo "  Subsequent sessions: Claude fetches context as needed."
echo ""
echo "To uninstall: bash $SCRIPT_DIR/uninstall.sh"
