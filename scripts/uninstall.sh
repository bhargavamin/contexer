#!/usr/bin/env bash
# Contexer uninstaller - removes MCP server + hooks from your AI assistant(s).
# Context store (~/.contexer/) is preserved unless you pass --purge.
#
# Usage:
#   bash scripts/uninstall.sh                  # auto-detect supported assistants
#   bash scripts/uninstall.sh --target cursor  # Cursor only
#   bash scripts/uninstall.sh --purge          # also delete ~/.contexer/
#
# Delegates to `contexer uninstall` (contexer/adapters/*) - single source of truth.
set -euo pipefail

echo "Uninstalling Contexer..."
echo ""

CONTEXER_BIN="$(command -v contexer || true)"
if [[ -z "$CONTEXER_BIN" ]]; then
    CONTEXER_BIN="$(uv tool dir 2>/dev/null)/../bin/contexer"
fi
if [[ ! -x "$CONTEXER_BIN" ]]; then
    echo "Error: contexer binary not found. Nothing to delegate to."
    echo "If you installed from source, the tool may already be removed."
    exit 1
fi

exec "$CONTEXER_BIN" uninstall "$@"
