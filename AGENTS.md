# AGENTS.md

**Read `CLAUDE.md` in this repo root — it is the single authoritative guide, and everything
below is only what you need before you open it.** Nothing in it is Claude-specific: it documents
the package layout, the store/revision model, every hook on every host, and the design
constraints that apply to whatever agent is working here.

This file used to be a full second copy of that guide, and the copy went stale. It still
described `capture_context` (an MCP tool that no longer exists), claimed `install --target`
accepts `Codex|cursor|all` (it is `claude|cursor|codex|gemini|all`), pointed at `~/.Codex.json`
for MCP registration, and knew nothing about `miner.py`, `redact.py`, `memory_sync.py`,
`team_context.py`, `remote.py`, `repo_key.py`, the `ui/` package, or the Claude and Gemini
adapters. Two hand-maintained copies of one architecture always diverge — the exact failure this
project exists to fix — so this one is a pointer now.

## Commands

```bash
# Install dependencies
uv sync

# Run the server (stdio transport — for manual testing)
uv run python server.py

# Smoke-test the server responds to MCP initialize
echo '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}' | uv run python server.py

# Run tests. Subset runs (single file / -k / class) MUST pass --no-cov: the ≥85%
# coverage floor in pyproject addopts judges the whole package, so a subset run
# exits 1 on coverage even with every test passing. Do not "fix" this by moving
# the flag to CI — the convention miner reads addopts (see pyproject.toml).
uv run pytest tests/                       # full suite, coverage gate on
uv run pytest tests/test_store.py --no-cov # subset iteration

# Lint — the exact version CI pins
uvx ruff@0.15.4 check .
```

## Contexer usage contract (this repo dogfoods itself)

- Call `update_context` when a decision is made, a convention is established, or you reach a
  synthesized understanding of a subsystem while answering a question. Pass the full reasoning
  and a `subtype` (`architecture` | `constraint` | `pattern` | `convention`). The server filters
  duplicates silently, so err on the side of calling it.
- Call `get_context` **before reading files** for any question about architecture, design
  rationale, constraints, patterns, or conventions.
- Keep your own unratified proposals provisional (`created_by="ai"`) rather than storing them as
  settled fact.

Everything else — module responsibilities, hook behaviour, the storage/revision format, and the
design constraints you must not violate — is in `CLAUDE.md`.
