# Integrations: Claude Code, Cursor, Codex, Gemini CLI

One install wires every tool you have; the same decision store serves all of them.

```bash
contexer install            # auto-detects installed tools and wires all of them
contexer install --target claude|cursor|codex|gemini|all   # override
```

Auto-detection: `~/.claude` → Claude Code, `~/.cursor` → Cursor, `~/.codex` → Codex, `~/.gemini` → Gemini CLI.

## Claude Code

Full integration: automatic session-start injection of your rules, per-prompt rationale and constraint capture, post-edit capture reminders, doc-drift advisories on edited files, context restore after compaction, and a `/bootstrap` command. Registered in `~/.claude.json` with hooks in `~/.claude/settings.json`.

## Cursor (1.7+)

```bash
contexer install --target cursor
```

This registers Contexer's MCP server in `~/.cursor/mcp.json` and wires two Cursor hook events in `~/.cursor/hooks.json`:

- `sessionStart`: injects your stored project rules and a usage nudge, and drops a managed always-apply rule at `<repo>/.cursor/rules/contexer.mdc`.
- `beforeSubmitPrompt`: silently captures your task and any "always / never / don't / create a rule" directives.

The managed rule file (marker-guarded, so your own rules are never touched) steers the agent to call Contexer's `get_context` before reading files for architecture/"why" questions, and to save rules via `update_context` rather than writing native `.cursor/rules` files.

The first time Cursor calls a Contexer tool it asks you to approve it. Contexer does not pre-approve its own MCP tools for you.

**Parity note:** Cursor's `beforeSubmitPrompt` hook cannot inject context (only allow/block) and Cursor exposes no usable compaction hook. So Contexer's per-prompt steering on Cursor rides on the session-start nudge plus the always-apply rule file, rather than Claude's per-prompt hooks. Doc-drift advisories, which need a per-prompt injection surface, are not available on Cursor for the same reason. The core value (automatic session-start injection of your stored rules) works identically to Claude Code.

## Codex

```bash
contexer install --target codex
```

This registers Contexer's MCP server in `~/.codex/config.toml` (under `[mcp_servers.contexer]`) and wires hooks in `~/.codex/hooks.json`. The `config.toml` edit is surgical: only the Contexer stanza is added or removed, so your existing servers, plugins, projects, and secrets are left untouched.

Codex's hooks use the same events as Claude Code (`SessionStart`, `PostToolUse`, `PreCompact`, `PostCompact`, `UserPromptSubmit`), so Contexer runs at **full Claude parity** there: automatic session-start injection, per-prompt rationale and constraint capture, post-edit reminders, doc-drift advisories, and context reload after compaction all work.

The first time Codex calls a Contexer tool it asks you to approve it. Contexer does not pre-approve its own MCP tools for you.

## Gemini CLI

```bash
contexer install --target gemini
```

This adds Contexer's MCP server and managed hooks to `~/.gemini/settings.json`, preserving all existing settings, MCP servers, and user hooks. Gemini CLI will ask you to trust the new hooks after installation.

The adapter uses Gemini's native `SessionStart`, `BeforeAgent`, `AfterTool`, `PreCompress`, and `SessionEnd` events. Session rules, first-prompt task capture, deterministic constraint capture, rationale lookup, post-edit reminders, and doc-drift advisories are supported. The `AfterTool` hook matches Gemini's `write_file` and `replace` tools.

**Parity note:** Gemini's `PreCompress` hook is asynchronous and advisory, and Gemini has no `PostCompress` event. Contexer therefore flags the compression and re-injects full context at the next `BeforeAgent` event. This restores context on the next turn, but cannot force Gemini to save an unsaved decision immediately before compression.

## Verification, updates, uninstall

See [install.md](install.md) for verifying an install, updating, and uninstalling.
