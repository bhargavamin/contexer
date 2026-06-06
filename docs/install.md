# Installing Contexer

Install once; it activates automatically in every git repo you open.

**Prerequisite:** [uv](https://docs.astral.sh/uv/getting-started/installation/) must be on your `PATH`.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## Install (plugin — recommended)

Run these three commands in any Claude Code session:

```
/plugin marketplace add bhargavamin/contexer
/plugin install contexer@contexer
/reload-plugins
```

Verify the server is connected:

```
/mcp
```

`contexer` should appear as **connected**.

**What this installs:**

| Config | What changes |
|---|---|
| MCP server | Registered globally via the plugin system |
| 6 hooks | SessionStart, PreCompact, PostCompact, 3× UserPromptSubmit |

No files in `~/.claude.json` or `~/.claude/settings.json` are hand-edited.

---

## First session

Open Claude Code in any git repo. On your first message, Contexer will:

1. Detect that no context exists for the repo
2. Inject a STOP directive asking Claude to run bootstrap first
3. Claude calls `bootstrap_context` — scans your stack (pyproject.toml, package.json, etc.)
4. Claude presents each inferred fact to you one at a time: `Correct? yes / no / [correction]`
5. Confirmed facts are stored; corrections are stored with your wording
6. Once bootstrap is done, Claude answers your original question

If you want to skip bootstrap and come back to it: type `skip` when Claude presents the first item.

To trigger bootstrap manually at any time:

```
/bootstrap
```

---

## Verify it's working

After the first session in a repo, check the store:

```bash
ls ~/.contexer/
cat ~/.contexer/<repo_slug>.json
```

The slug is your repo path with non-alphanumeric characters replaced by underscores.

---

## Update

```
/plugin update contexer@contexer
/reload-plugins
```

---

## Uninstall

```
/plugin uninstall contexer
```

This removes the MCP server registration and all hooks. Your context store (`~/.contexer/`) is not deleted. To also remove stored context:

```bash
rm -rf ~/.contexer/
```

---

## Manual install (fallback)

If you prefer not to use the plugin system:

```bash
git clone git@github.com:bhargavamin/contexer.git ~/tools/contexer
bash ~/tools/contexer/scripts/install.sh
```

The script writes the MCP server entry and all 6 hooks into `~/.claude.json` and `~/.claude/settings.json` directly. It is idempotent — safe to re-run after updates or if you move the repo.

To uninstall:

```bash
bash ~/tools/contexer/scripts/uninstall.sh
```

---

## How sessions work after install

```
Session opens
  └─▶ SessionStart hook: injects count pointer (N decisions stored) or STOP directive (0 stored)

You send first message
  └─▶ Anchor hook: writes git root to ~/.contexer/.current_repo
  └─▶ Bootstrap hook (once): checks if context exists; if not, injects bootstrap directive
  └─▶ Capture hook (once): calls capture_context with your first message as task description

Claude works on your task
  └─▶ Claude calls update_context when it makes significant decisions (you say nothing)

Context window nears limit
  └─▶ PreCompact hook: injects reminder to call update_context before compaction

Compaction happens
  └─▶ PostCompact hook: reloads full stored context into Claude's working memory

Next session: repeat from top, but with history
```

**You do not need to do anything during a session.** Claude captures decisions automatically. If Claude misses something important, say: *"store that decision"*.

---

## Something not working?

→ See **[docs/troubleshooting.md](troubleshooting.md)** for the four most common failure modes with exact fix steps.
