#!/bin/bash
set -e

YELLOW='\033[1;33m'
RED='\033[0;31m'
GREEN='\033[0;32m'
NC='\033[0m'

ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}!${NC} $1"; }

CONTEXER_DIR="$(cd "$(dirname "$0")/.." && pwd)"
CLAUDE_JSON="$HOME/.claude.json"
DESKTOP_CONFIG="$HOME/Library/Application Support/Claude/claude_desktop_config.json"

echo ""
echo "Contexer — Uninstall"
echo "────────────────────"
echo ""

# ── Claude Code (~/.claude.json) ──────────────────────────────────────────────

python3 - <<PYEOF
import json, os
path = "$CLAUDE_JSON"
if not os.path.exists(path):
    print("  ~/.claude.json not found — skipping")
    exit(0)
with open(path) as f:
    config = json.load(f)
servers = config.get("mcpServers", {})
if "contexer" not in servers:
    print("  not registered in ~/.claude.json — skipping")
else:
    del servers["contexer"]
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
    print("  removed from ~/.claude.json")
PYEOF
ok "Claude Code MCP entry removed"

# ── Claude Desktop ────────────────────────────────────────────────────────────

python3 - <<PYEOF
import json, os
path = "$DESKTOP_CONFIG"
if not os.path.exists(path):
    print("  Claude Desktop config not found — skipping")
    exit(0)
with open(path) as f:
    config = json.load(f)
servers = config.get("mcpServers", {})
if "contexer" not in servers:
    print("  not registered in Claude Desktop config — skipping")
else:
    del servers["contexer"]
    with open(path, "w") as f:
        json.dump(config, f, indent=2)
    print("  removed from Claude Desktop config")
PYEOF
ok "Claude Desktop MCP entry removed"

# ── Stored context data ───────────────────────────────────────────────────────

STORE_DIR="$HOME/.contexer"
if [ -d "$STORE_DIR" ]; then
    echo ""
    warn "Stored context found at $STORE_DIR"
    ls "$STORE_DIR"
    echo ""
    read -r -p "Delete all stored context? This cannot be undone. [y/N]: " CONFIRM
    if [[ "$CONFIRM" =~ ^[Yy]$ ]]; then
        rm -rf "$STORE_DIR"
        ok "Deleted $STORE_DIR"
    else
        ok "Kept stored context at $STORE_DIR"
    fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo ""
ok "Contexer uninstalled"
echo ""
echo "  Restart Claude Code and Claude Desktop to apply."
echo "  To remove hooks from individual repos, delete .claude/settings.json"
echo "  in each repo or remove the SessionStart/UserPromptSubmit entries."
echo ""
