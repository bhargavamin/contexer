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

---

## First session

Open Claude Code in any git repo. On your first message, Contexer will:

1. Detect that no context exists for the repo
2. Ask Claude to run a short bootstrap before answering your question
3. Claude scans your stack and presents inferred facts one at a time: `Correct? yes / no / [correction]`
4. Confirmed facts are stored; corrections are stored with your wording
5. Once bootstrap is done, Claude answers your original question

If you want to skip bootstrap and come back to it: type `skip` when Claude presents the first item.

To trigger bootstrap manually at any time:

```
/bootstrap
```

---

## Verify it's working

After the first session in a repo, check that decisions were stored:

```bash
ls ~/.contexer/
```

You should see a `.json` file named after your repo. Each file holds the decisions captured for that repo.

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

This removes the MCP server registration and all hooks. Your stored decisions are not deleted. To also remove them:

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

The script is idempotent — safe to re-run after updates or if you move the repo.

To uninstall:

```bash
bash ~/tools/contexer/scripts/uninstall.sh
```

---

## Something not working?

→ See **[docs/troubleshooting.md](troubleshooting.md)** for the most common failure modes with exact fix steps.
