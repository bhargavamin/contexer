#!/usr/bin/env bash
# Contexer installer (from source) - builds the CLI from this clone and wires it into
# your AI assistant(s) via the same code path end users run.
#
# Usage:
#   bash scripts/install.sh                 # auto-detect supported assistants
#   bash scripts/install.sh --target cursor # Cursor only
#   bash scripts/install.sh --target all    # all supported tools
#
# This delegates all per-tool wiring to `contexer install` (contexer/adapters/*), so a
# single source of truth supports every target. It (re)builds the `contexer` binary from
# THIS directory - re-run after editing source to pick up changes.
#
# Requires: uv (https://docs.astral.sh/uv/getting-started/installation/)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

if ! command -v uv &>/dev/null; then
    echo "Error: uv is required."
    echo "Install: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

echo "Installing Contexer from source: $REPO_DIR"
echo "→ Building and installing the contexer tool..."
uv tool install --reinstall --from "$REPO_DIR" contexer

# Resolve the freshly-installed binary even if ~/.local/bin isn't on PATH yet.
CONTEXER_BIN="$(command -v contexer || true)"
if [[ -z "$CONTEXER_BIN" ]]; then
    CONTEXER_BIN="$(uv tool dir)/../bin/contexer"
fi
if [[ ! -x "$CONTEXER_BIN" ]]; then
    echo "Error: contexer binary not found after install. Is ~/.local/bin on your PATH?"
    exit 1
fi

echo "→ Wiring into your AI assistant(s)..."
exec "$CONTEXER_BIN" install "$@"
