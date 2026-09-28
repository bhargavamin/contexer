# Adapters: agent guidance

Loaded automatically by Claude Code when files under `contexer/adapters/` are read. Other agents:
read this before changing an adapter. It holds the host-specific detail relocated from the root
`CLAUDE.md`; the full hook-by-hook behaviour is in
[`docs/architecture.md`](../../docs/architecture.md#session-behaviour-hooks).

## Adapter package

- **`contexer/adapters/`** - one module per AI-assistant target (`claude.py`, `cursor.py`, `codex.py`, `gemini.py`), each owning that tool's MCP registration, hook wiring, install/uninstall/status, provider-specific hook output, a static `EVIDENCE_COVERAGE` declaration, and the host name passed into shared reconciliation. Coverage is honest-first: Cursor has prompt directives but no edit hook, unimplemented test/diff capture stays unavailable, and agent conclusions are model-reported rather than observed. Prompt/editor hooks pass host payload plus process cwd into the shared filesystem resolver: it walks parents for a `.git` marker and canonicalizes linked-worktree store identity without spawning Git. These paths never wait on the decision-store lock, scan the spool, call a model/network, or run Git; prompt retrieval deliberately omits Git-backed staleness decoration while explicit MCP/UI reads retain it. Session-start reconciliation is owned by the shared store path so new adapters inherit crash recovery without duplicating it. `__init__.py` is the registry (`detect()` / `select()`). `base.py` holds shared config-file helpers. Hook migration is owner-aware and filters each entry independently: a current Contexer command retires stale Contexer siblings without deleting foreign commands in the same group, while generic substrings such as `update_context.py` or `uv run --directory` are never sufficient ownership evidence. Exact namespaced legacy identities remain removable. **A real gotcha for anyone touching adapter marker-matching:** matching a hook's identity against `str(hook_dict)` (its Python repr) breaks silently whenever the command contains a double quote (any `python -c "..."` command does), because repr re-escapes inner quotes - a marker check written that way will never recognize the hook it just installed and will re-fire on every install forever. Use the raw-command match helper instead for any marker containing a quote. Gemini stores MCP and hook configuration together in `~/.gemini/settings.json` and uses different event names (`SessionStart`, `BeforeAgent`, `AfterTool`, `PreCompress`, `SessionEnd`); because it has no `PostCompress` equivalent, it sets a flag and re-injects context at the next `BeforeAgent` event instead.

## Host hook registrations

- **Legacy cleanup on upgrade.** Older installs may have left stale hook entries (repo-local settings, hooks pointing at removed tool names) behind; the installer self-heals these on the next install/session start rather than requiring a manual uninstall/reinstall cycle.
- **Claude registrations have an application boundary.** `hook_host.run_claude` is a stdlib-only wrapper around installer-generated commands and the Claude plugin bundle. Cursor imports Claude configuration alongside native hooks, so top-level `cursor_version` or the combined `conversation_id`/`generation_id`/`workspace_roots` envelope returns `{}` before importing adapters, touching flags, or launching the wrapped command. Model names and terminal environment do not select a host. Unknown/legacy envelopes preserve the original command, stdin bytes, cwd, output and exit status. Keep this guard at registration, since Codex reuses Claude handlers directly. Reinstall converges all owned commands, including plan reminders, without altering foreign siblings; a package-only update does not rewrite these registrations. The plugin locates an existing Python 3.12+ with `uv python find --no-project --no-config --system --offline --no-python-downloads` and executes `hook_host.py` directly with `-P`, preserving workspace cwd. The stdlib guard runs before project loading, environment setup/repair or the inner `uv run`; the documented uv/Python prerequisites must already be available. Plan-hook presence uses exact command equality so a foreign "plan approved" marker cannot suppress the managed reminder.

## MCP integration (per host)

`contexer install` registers the server in `~/.claude.json` under `mcpServers`, pointing at the installed console script:

```json
{
  "contexer": {
    "type": "stdio",
    "command": "/Users/<you>/.local/bin/contexer"
  }
}
```

(A from-source dev install via `scripts/install.sh` wires `uv run --directory <clone> python server.py` instead.)

For Cursor, the equivalents are `~/.cursor/mcp.json` (MCP server registration) and `~/.cursor/hooks.json` (hook wiring), managed by `contexer/adapters/cursor.py`. The MCP server entry in `mcp.json` uses the same `contexer` command; hooks use Cursor's `sessionStart` and `beforeSubmitPrompt` event names.

For Codex, the MCP server is registered in `~/.codex/config.toml` (TOML) as `[mcp_servers.contexer]` with `command = "<contexer-bin>"`, and hooks live in `~/.codex/hooks.json` (JSON, same schema and event names as Claude's `settings.json` `hooks` block), both managed by `contexer/adapters/codex.py`. The `config.toml` edit is surgical text manipulation (validated with `tomllib` before writing) so only the contexer stanza is touched.

For Gemini CLI, both the MCP server and managed hooks live in `~/.gemini/settings.json`, managed by `contexer/adapters/gemini.py`. Installation preserves unrelated keys, servers, and hook groups.
