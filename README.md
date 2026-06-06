# Contexer

A lightweight MCP server that captures developer intent during Claude Code sessions and injects it into future sessions — so context is never lost between restarts.

## The problem

AI coding sessions start blind. CLAUDE.md files decay. When Claude Code commits autonomously, the reasoning behind decisions isn't captured anywhere. The next session has no idea what changed or why, and you waste the first few minutes re-explaining context before doing real work.

## How it works

Every session follows the same automatic flow:

```
You open Claude Code
    │
    ↓
SessionStart hook fires
    └─▶ reads ~/.contexer/<repo>.json
    └─▶ injects stored decisions into Claude's context
    └─▶ shows "Contexer: N decision(s) loaded" in the UI
    │
    ↓
You type your first message
    │
    ↓
UserPromptSubmit hook fires (once per session)
    └─▶ saves your message as the task description
    │
    ↓
Claude works on the task
    │
    ↓
After each of your messages, a reminder is injected:
    └─▶ "if you made a significant decision this turn, call update_context"
    │
    ↓
Claude calls update_context when it makes decisions
    │
    ↓
Server filters the content — stores only if it:
    ├─▶ describes a decision affecting the repo
    ├─▶ establishes or changes a pattern
    ├─▶ documents a constraint or tradeoff
    └─▶ is meaningfully different from what's already stored
    │
    ↓
Context window fills up → compaction about to happen
    │
    ↓
PreCompact hook fires
    └─▶ shows "Contexer: context compaction starting — call update_context" in the UI
    └─▶ NOTE: PreCompact cannot inject into Claude's context window (Claude Code
        limitation). The UI message is a visual cue only. Manually tell Claude
        to call update_context if you see it.
    │
    ↓
Compaction happens
    │
    ↓
PostCompact hook fires
    └─▶ shows "Contexer: N decision(s) available — run get_context to reload" in UI
    └─▶ NOTE: PostCompact cannot inject context automatically. After compaction,
        ask Claude to "call get_context" to reload stored decisions.
    │
    ↓
Next session: repeat from the top — but now with history
```

**It is Claude — not you — who calls `update_context`.** You work normally. Claude watches what it's doing and invokes the tool when it judges something is worth storing. The reminder injected before every turn keeps that instruction fresh so Claude doesn't forget mid-task.

If Claude misses something important, just tell it: **"store that decision"** and it will call `update_context` immediately.

## The three tools

| Tool | Triggered by | What it does |
|---|---|---|
| `get_context` | `SessionStart` hook | Loads stored decisions into the session |
| `capture_context` | `UserPromptSubmit` hook (first message only) | Saves the task description |
| `update_context` | Claude Code, mid-task | Nominates a decision; server filters before storing |
| `bootstrap_context` | Claude Code, on first session with no context | Scans the repo for inferable decisions and returns gap questions |

## The filter

`update_context` does not store everything Claude sends. It applies one primary gate and silently discards failures:

- **Novelty required:** if the content overlaps >70% with any existing stored decision (token overlap), it is discarded as a duplicate
- **Novel content is stored:** `update_context` is only called for significant decisions — if it passes the novelty check, it is stored

If filtered, the content is silently discarded. No noise, no logs.

## What happens if a decision is missed

Nothing breaks — you just lose that piece of context for future sessions. The next session starts without it, and Claude might ask about something that was already decided, or make a conflicting choice without realising it.

Two things can cause a miss:

1. **Claude doesn't call `update_context`** — it judged something as not significant enough. The decision is never nominated.
2. **Claude calls it but the server filters it** — the content didn't match any of the four criteria. Silently dropped.

**What you can do:**

- Say **"store that decision"** and Claude will call `update_context` immediately
- Ask Claude to call `get_context` mid-session to see what's been captured so far
- Inspect the file directly: `cat ~/.contexer/<repo_slug>.json`

## Install as a plugin (recommended)

Contexer ships as a Claude Code plugin — one install registers the MCP server **and** all four
session hooks together. No hand-editing of `~/.claude.json` or `~/.claude/settings.json`.

**Requires:** [uv](https://github.com/astral-sh/uv) on your `PATH`.

```bash
/plugin marketplace add bhargavamin/contexer
/plugin install contexer@contexer
```

Restart Claude Code, then run `/mcp` — `contexer` should appear as connected. On the first
connection `uv` builds an isolated environment for the server (one-time, needs network); later
sessions reuse it. Update later with `/plugin update contexer@contexer`.

### Private repo / auth

`/plugin marketplace add bhargavamin/contexer` clones the repo over your existing git
credentials. If `git clone https://github.com/bhargavamin/contexer` works in your shell, the
add works (check with `gh auth status`). Each user needs read access to the repo and their own
GitHub auth.

- **HTTPS:** `gh auth login`, macOS Keychain, or `git-credential-store`.
- **SSH:** add via the full URL — `/plugin marketplace add git@github.com:bhargavamin/contexer.git`
  (needs the host in `known_hosts` and your key in `ssh-agent`).
- **Background auto-updates** run at startup without interactive credential prompts, so for a
  private repo set a token in your environment or updates silently fail:
  ```bash
  export GITHUB_TOKEN=ghp_xxxx   # or GH_TOKEN — needs repo read scope
  ```

> Plugins are Claude Code only. For **Claude Desktop**, or to install without the plugin system,
> use the manual steps below (or run `scripts/install.sh`, which automates them and also
> registers Claude Desktop).

## Manual install (fallback)

**Requires:** Python 3.12+, [uv](https://github.com/astral-sh/uv)

```bash
git clone https://github.com/bhargavamin/contexer.git
cd contexer
uv sync
```

### Register with Claude Code

Add to `~/.claude.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "contexer": {
      "type": "stdio",
      "command": "uv",
      "args": ["run", "--directory", "/path/to/contexer", "python", "server.py"]
    }
  }
}
```

Then add the session hooks to `~/.claude/settings.json` (global — fires for every Claude Code session, all repos):

```json
{
  "hooks": {
    "SessionStart": [{
      "hooks": [{
        "type": "command",
        "command": "REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd) && uv run --directory /path/to/contexer python -c \"import sys,json; sys.path.insert(0,'/path/to/contexer'); import store; store.STORE_DIR.mkdir(exist_ok=True); (store.STORE_DIR/'.current_repo').write_text(sys.argv[1]); print(json.dumps(store.get_session_start_context(sys.argv[1])))\" \"$REPO\"",
        "statusMessage": "Loading session context..."
      }]
    }],
    "PreCompact": [{
      "hooks": [{
        "type": "command",
        "command": "echo '{\"systemMessage\": \"Contexer: context compaction starting — call update_context for any decisions not yet stored\"}'",
        "statusMessage": "Saving decisions before compact..."
      }]
    }],
    "PostCompact": [{
      "hooks": [{
        "type": "command",
        "command": "REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd) && uv run --directory /path/to/contexer python -c \"import sys,json; sys.path.insert(0,'/path/to/contexer'); import store; data=store._load(sys.argv[1]); entries=data.get('entries',[]); decisions=[e for e in entries if e['type']=='decision']; msg=f'Contexer: {len(decisions)} decision(s) available — run get_context to reload' if decisions else 'Contexer: no context stored'; print(json.dumps({'systemMessage':msg}))\" \"$REPO\"",
        "statusMessage": "Reloading context after compact..."
      }]
    }],
    "UserPromptSubmit": [
      {
        "hooks": [{
          "type": "mcp_tool",
          "server": "contexer",
          "tool": "capture_context",
          "input": {
            "repo_path": "",
            "description": "${prompt}"
          },
          "once": true,
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
  }
}
```

Replace `/path/to/contexer` with your actual Contexer installation path. The hooks detect the current repo automatically — no per-repo configuration needed.

### Register with Claude Desktop

Add to `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "contexer": {
      "command": "uv",
      "args": ["run", "--directory", "/path/to/contexer", "python", "server.py"]
    }
  }
}
```

Restart Claude Code / Claude Desktop after editing either config.

## Storage

Context is stored at `~/.contexer/<repo_slug>.json` — one file per repo, capped at 50 entries. Only the latest task description is kept (previous ones are replaced on each session start) — all remaining slots go to decisions. No cloud, no database, no external dependencies.

## Verify it's working

After restarting, run `/mcp` in Claude Code — `contexer` should appear as connected. On the next session start you should see `Contexer: N decision(s) loaded` in the UI.

## License

MIT
