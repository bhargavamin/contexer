# Installing Contexer

Install once; it activates automatically in every git repo you open.

**Prerequisite:** [uv](https://docs.astral.sh/uv/getting-started/installation/) must be on your `PATH`.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## Install from PyPI (recommended)

```bash
uv tool install contexer
```

Then wire it into Claude Code (registers the MCP server and all hooks):

```bash
bash <(curl -fsSL https://raw.githubusercontent.com/bhargavamin/contexer/main/scripts/install.sh)
```

Or if you have the repo cloned already:

```bash
bash scripts/install.sh
```

Verify the server is connected — open any Claude Code session and run:

```
/mcp
```

`contexer` should appear as **connected**.

---

## Install from source (development)

```bash
git clone git@github.com:bhargavamin/contexer.git ~/tools/contexer
bash ~/tools/contexer/scripts/install.sh
```

The script detects that the `contexer` binary is not available from a PyPI install and wires Claude Code to run the server directly from the cloned directory using `uv run`. This is the right choice when you want to edit the source.

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

```bash
uv tool upgrade contexer
```

No reinstall needed — the existing MCP registration and hooks continue to work.

---

## Uninstall

```bash
bash scripts/uninstall.sh
uv tool uninstall contexer
```

Removes the MCP server registration and all hooks. Your context store (`~/.contexer/`) is not deleted. To also remove stored context:

```bash
rm -rf ~/.contexer/
```

---

## Something not working?

→ See **[docs/troubleshooting.md](troubleshooting.md)** for the most common failure modes with exact fix steps.
