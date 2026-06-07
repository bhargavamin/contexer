#!/usr/bin/env bash
# Contexer installer — wires up MCP server and global hooks for Claude Code.
# Run from any location: bash /path/to/contexer/scripts/install.sh
# Requires: uv (https://docs.astral.sh/uv/getting-started/installation/)
# Note: install path must not contain spaces.
set -euo pipefail

CONTEXER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "Installing Contexer from: $CONTEXER_DIR"
echo ""

if ! command -v uv &>/dev/null; then
    echo "Error: uv is required."
    echo "Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

echo "→ Installing dependencies..."
uv sync --directory "$CONTEXER_DIR" --quiet

echo "→ Configuring Claude Code..."
uv run --directory "$CONTEXER_DIR" python3 - "$CONTEXER_DIR" <<'PYEOF'
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

# Build command strings with CONTEXER_DIR substituted
_ss_code = (
    f"import sys,json; sys.path.insert(0,'{D}'); "
    "import store; store.STORE_DIR.mkdir(exist_ok=True); "
    "(store.STORE_DIR/'.current_repo').write_text(sys.argv[1]); "
    "print(json.dumps(store.get_session_start_context(sys.argv[1])))"
)
_bootstrap_code = (
    f"import sys,json; sys.path.insert(0,'{D}'); "
    "import store; result=store.get_bootstrap_context_prompt(sys.argv[1]); "
    "print(json.dumps(result))"
)
_post_code = (
    f"import sys,json; sys.path.insert(0,'{D}'); "
    "import store; ctx=store.get_context(sys.argv[1]); "
    "print(json.dumps({'systemMessage': 'Contexer: context reloaded after compaction\\n' + ctx}))"
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
if not _in_groups(ups, ".current_repo"):
    ups.insert(0, {"hooks": [{"type": "command",
        "statusMessage": "Anchoring repo context...",
        "command": "REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd) && printf '%s' \"$REPO\" > ~/.contexer/.current_repo && echo '{}'"}]})

if not _in_groups(ups, "get_bootstrap_context_prompt"):
    ups.append({"hooks": [{"type": "command", "once": True,
        "statusMessage": "Checking bootstrap context...",
        "command": _uv(_bootstrap_code)}]})

if not any(
    any(h.get("type") == "mcp_tool" and h.get("server") == "contexer" and h.get("tool") == "capture_context"
        for h in grp.get("hooks", []))
    for grp in ups
):
    ups.append({"hooks": [{"type": "mcp_tool", "server": "contexer", "tool": "capture_context",
        "input": {"repo_path": "", "description": "${prompt}"},
        "once": True, "statusMessage": "Capturing task..."}]})

if not any(
    any(h.get("type") == "mcp_tool" and h.get("server") == "contexer" and h.get("tool") == "get_context_for_prompt"
        for h in grp.get("hooks", []))
    for grp in ups
):
    ups.append({"hooks": [{"type": "mcp_tool", "server": "contexer", "tool": "get_context_for_prompt",
        "input": {"repo_path": "", "prompt": "${prompt}"},
        "statusMessage": "Checking for relevant decisions..."}]})

# ── Permissions ───────────────────────────────────────────────────────────────
allow = settings.setdefault("permissions", {}).setdefault("allow", [])
for p in ["mcp__contexer__capture_context", "mcp__contexer__update_context",
          "mcp__contexer__get_context", "mcp__contexer__bootstrap_context",
          "mcp__contexer__get_context_for_prompt"]:
    if p not in allow:
        allow.append(p)

# ── Store directory ───────────────────────────────────────────────────────────
(HOME / ".contexer").mkdir(exist_ok=True)

_save(settings_json, settings)
print("  ✓ Hooks and permissions written to ~/.claude/settings.json")
PYEOF

echo ""
echo "Contexer installed."
echo ""
echo "  Next: restart Claude Code (or start a new session) in any git repo."
echo "  First session: bootstrap runs automatically."
echo "  Subsequent sessions: call get_context when you need project context."
echo ""
echo "To uninstall: bash $CONTEXER_DIR/scripts/uninstall.sh"
