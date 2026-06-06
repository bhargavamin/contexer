#!/usr/bin/env bash
# Contexer uninstaller — removes MCP server and hooks from Claude Code config.
# Context store (~/.contexer/) is preserved. Remove manually if desired.
set -euo pipefail

CONTEXER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
echo "Uninstalling Contexer..."
echo ""

python3 - "$CONTEXER_DIR" <<'PYEOF'
import json, sys
from pathlib import Path

HOME = Path.home()

def _load(path):
    return json.loads(path.read_text()) if path.exists() else {}

def _save(path, data):
    path.write_text(json.dumps(data, indent=2))

def _filter_groups(groups, markers):
    return [
        grp for grp in groups
        if not any(marker in str(h) for marker in markers for h in grp.get("hooks", []))
    ]

# ── MCP server ────────────────────────────────────────────────────────────────
claude_json = HOME / ".claude.json"
if claude_json.exists():
    claude = _load(claude_json)
    removed = claude.get("mcpServers", {}).pop("contexer", None)
    if removed:
        _save(claude_json, claude)
        print("  ✓ MCP server removed from ~/.claude.json")
    else:
        print("  - No MCP server entry found in ~/.claude.json")

# ── Hooks and permissions ──────────────────────────────────────────────────────
settings_json = HOME / ".claude" / "settings.json"
if settings_json.exists():
    settings = _load(settings_json)
    hooks = settings.get("hooks", {})
    changed = False

    _markers = {
        "SessionStart":      ["get_session_start_context"],
        "PreCompact":        ["compaction starting"],
        "PostCompact":       ["reloaded after compaction"],
        "UserPromptSubmit":  [".current_repo", "get_bootstrap_context_prompt"],
    }
    for event, markers in _markers.items():
        before = hooks.get(event, [])
        after = _filter_groups(before, markers)
        # also remove the mcp_tool capture_context hook from UserPromptSubmit
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
        changed = True
        settings["permissions"]["allow"] = cleaned

    if changed:
        _save(settings_json, settings)
        print("  ✓ Hooks and permissions removed from ~/.claude/settings.json")
    else:
        print("  - No Contexer hooks found in ~/.claude/settings.json")

print("")
print("Uninstall complete.")
print("Context store (~/.contexer/) was not removed.")
print("To delete stored context: rm -rf ~/.contexer/")
PYEOF
