#!/bin/bash
set -e

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}!${NC} $1"; }
fail() { echo -e "${RED}✗${NC} $1"; exit 1; }

CONTEXER_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CLAUDE_JSON="$HOME/.claude.json"
DESKTOP_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"

echo ""
echo "Contexer — MCP context engine for Claude Code"
echo "──────────────────────────────────────────────"
echo ""

# ── Prerequisites ─────────────────────────────────────────────────────────────

command -v uv &>/dev/null || fail "uv not found. Install it: https://github.com/astral-sh/uv"
ok "uv found ($(uv --version))"

# ── Dependencies ──────────────────────────────────────────────────────────────

echo "Installing Python dependencies..."
uv sync --directory "$CONTEXER_DIR" --quiet
ok "Dependencies installed"

# ── Claude Code (~/.claude.json) ──────────────────────────────────────────────

python3 - <<PYEOF
import json, sys
path = "$CLAUDE_JSON"
try:
    with open(path) as f:
        config = json.load(f)
except FileNotFoundError:
    config = {}

servers = config.setdefault("mcpServers", {})
if "contexer" in servers:
    print("  already registered in ~/.claude.json — skipping")
else:
    servers["contexer"] = {
        "type": "stdio",
        "command": "uv",
        "args": ["run", "--directory", "$CONTEXER_DIR", "python", "server.py"]
    }
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
    print("  registered in ~/.claude.json")
PYEOF
ok "Claude Code MCP registration done"

# ── Claude Desktop ────────────────────────────────────────────────────────────

python3 - <<PYEOF
import json, os
path = "$DESKTOP_CONFIG"
if not os.path.exists(path):
    print("  Claude Desktop config not found — skipping")
    exit(0)

with open(path) as f:
    config = json.load(f)

servers = config.setdefault("mcpServers", {})
if "contexer" in servers:
    print("  already registered in Claude Desktop config — skipping")
else:
    servers["contexer"] = {
        "command": "uv",
        "args": ["run", "--directory", "$CONTEXER_DIR", "python", "server.py"]
    }
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
    print("  registered in Claude Desktop config")
PYEOF
ok "Claude Desktop MCP registration done"

# ── Per-repo hooks ────────────────────────────────────────────────────────────

echo ""
echo "Set up session hooks for a repo?"
echo "Hooks enable automatic context loading and task capture per session."
read -r -p "Enter repo path (or press Enter to skip): " REPO_PATH

if [ -n "$REPO_PATH" ]; then
    REPO_PATH="${REPO_PATH/#\~/$HOME}"  # expand ~
    if [ ! -d "$REPO_PATH/.git" ]; then
        warn "$REPO_PATH is not a git repo — skipping hooks"
    else
        SETTINGS_DIR="$REPO_PATH/.claude"
        SETTINGS_FILE="$SETTINGS_DIR/settings.json"
        mkdir -p "$SETTINGS_DIR"

        python3 - <<PYEOF
import json, os

path = "$SETTINGS_FILE"
repo = "$REPO_PATH"
ctx = "$CONTEXER_DIR"

try:
    with open(path) as f:
        config = json.load(f)
except FileNotFoundError:
    config = {}

hooks = config.setdefault("hooks", {})

if "SessionStart" in hooks:
    print("  SessionStart hook already exists — skipping")
else:
    hooks["SessionStart"] = [{
        "hooks": [{
            "type": "command",
            "command": f"uv run --directory {ctx} python -c \"import sys,json; sys.path.insert(0,'{ctx}'); import store; print(json.dumps(store.get_session_start_context('{repo}')))\"",
            "statusMessage": "Loading session context..."
        }]
    }]

if "PreCompact" not in hooks:
    hooks["PreCompact"] = [{
        "hooks": [{
            "type": "command",
            "command": "echo '{\"systemMessage\": \"Contexer: context compaction starting — call update_context for any decisions not yet stored\"}'",
            "statusMessage": "Saving decisions before compact..."
        }]
    }]

if "PostCompact" not in hooks:
    hooks["PostCompact"] = [{
        "hooks": [{
            "type": "command",
            "command": f"uv run --directory {ctx} python -c \"import sys,json; sys.path.insert(0,'{ctx}'); import store; data=store._load('{repo}'); entries=data.get('entries',[]); decisions=[e for e in entries if e['type']=='decision']; msg=f'Contexer: {{len(decisions)}} decision(s) available — run get_context to reload' if decisions else 'Contexer: no context stored'; print(json.dumps({{'systemMessage':msg}}))\"",
            "statusMessage": "Reloading context after compact..."
        }]
    }]

if "UserPromptSubmit" in hooks:
    print("  UserPromptSubmit hooks already exist — skipping")
else:
    hooks["UserPromptSubmit"] = [
        {
            "hooks": [{
                "type": "mcp_tool",
                "server": "contexer",
                "tool": "capture_context",
                "input": {"repo_path": repo, "description": "\${prompt}"},
                "once": True,
                "statusMessage": "Capturing task..."
            }]
        },
        {
            "hooks": [{
                "type": "command",
                "command": "echo '{\"hookSpecificOutput\": {\"hookEventName\": \"UserPromptSubmit\", \"additionalContext\": \"Reminder: if you make a significant decision, establish a pattern, or document a constraint this turn, call update_context.\"}}'",
                "statusMessage": "Loading context reminder..."
            }]
        }
    ]

with open(path, "w") as f:
    json.dump(config, f, indent=2)
print(f"  hooks written to {path}")
PYEOF
        ok "Session hooks configured for $REPO_PATH"

        # Add settings.json to .gitignore if not already there
        GITIGNORE="$REPO_PATH/.gitignore"
        if [ -f "$GITIGNORE" ] && grep -q "settings.json" "$GITIGNORE"; then
            true
        elif [ -f "$SETTINGS_DIR/settings.local.json" ]; then
            true  # project uses local.json pattern already
        else
            warn "Consider adding .claude/settings.json to .gitignore if it contains absolute paths"
        fi
    fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
ok "Contexer installed successfully"
echo ""
echo "  Next steps:"
echo "  1. Restart Claude Code and Claude Desktop"
echo "  2. Run /mcp in Claude Code — 'contexer' should appear as connected"
echo "  3. Open a repo session — you should see 'Contexer: N decision(s) loaded'"
echo ""
