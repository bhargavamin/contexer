# Installing Contexer

Install once; it activates automatically in every git repo you open.

**Requirements:** Python 3.12 or later. [uv](https://docs.astral.sh/uv/getting-started/installation/) must be on your `PATH`.

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

---

## Install from PyPI (recommended)

```bash
uv tool install contexer
contexer install
```

> Do **not** run `contexer install` with `sudo` — it writes to *your* `~/.claude.json` and `~/.claude/settings.json`. Under `sudo` it would target root's home directory instead and Claude Code would never see the config.

That's it. The second command registers the MCP server and all hooks with Claude Code automatically.

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

## Token cost at a glance

Contexer injects stored decisions before Claude responds — not during. Cost is paid once per session.

| Pre-loaded rules | Tokens injected | % of a typical session |
|---|---|---|
| 5 rules | ~125 | ~0.2% |
| 10 rules | ~250 | ~0.3% |
| 25 rules | ~625 | ~0.8% |

Only `constraint` and `convention` decisions are pre-loaded. `architecture` and `pattern` decisions cost 0 tokens at session start and are fetched on demand. Store lookups take 0.03–0.27ms regardless of store size — imperceptible. See the [Benchmark & Token Cost](https://app.notion.com/p/378223d61ba281ccb680f5405afa9f96) page for full numbers.

---

## First session

Open Claude Code in any git repo. On your first message, Claude will ask:

> "Contexer: no project context stored for this repo. How should I set up context for future sessions? (quick / full / scan / skip)"

- **Quick** — one question (what does this repo do?), answer stored, then Claude answers your original question.
- **Full** — guided setup: Claude scans your stack, presents inferred facts one at a time (`Correct? yes / no / [correction]`), asks a few questions about intent and constraints, and stores the answers. Best if you develop or maintain the repo.
- **Scan** — you're seeing this repo for the first time: no quiz. Contexer scans the code and docs, stores what it finds, and asks only what you're planning to do here.
- **Skip** — Claude answers your original question immediately. Bootstrap is skipped.

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
contexer reinstall
```

Run `contexer reinstall` after upgrading — it re-syncs the MCP registration and hooks in case they changed. Then restart Claude Code: the MCP server is spawned once at session start, so a running session keeps the old version until restarted.

> If `uv tool upgrade` doesn't pick up a release published minutes ago, force past the cache: `uv tool install --reinstall --refresh contexer`.

---

## Uninstall

```bash
contexer uninstall
uv tool uninstall contexer
```

Removes the MCP server registration and all hooks. Your context store (`~/.contexer/`) is kept. To also remove stored context:

```bash
contexer uninstall --purge
uv tool uninstall contexer
```

---

## Something not working?

→ See **[docs/troubleshooting.md](troubleshooting.md)** for the most common failure modes with exact fix steps.
