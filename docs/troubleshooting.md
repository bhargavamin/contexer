# Troubleshooting

Start every diagnosis with:

```bash
contexer status
```

It reports the installed version, binary path, MCP registration, hook state, and store summary. It is built to survive corrupt files: a corrupt **config** file gets an explicit WARNING; a corrupt **store** file is simply excluded from the entry counts. Then find your symptom below.

---

## `contexer` doesn't show up in `/mcp`

1. `contexer status` — does it say `MCP server: registered`?
   - **NOT registered** → run `contexer install`, then restart your AI assistant.
   - **registered** but still missing in `/mcp` → restart your AI assistant. The MCP server is spawned once at session start; config changes are not picked up mid-session.
2. Check the binary path printed by `status` exists: `ls -l ~/.local/bin/contexer`. If missing, `uv tool install contexer` first.

## `Permission denied` during `contexer install` / `uninstall` / `reinstall`

Contexer writes only to assistant config files and `~/.contexer/` in your home directory — being an administrator is not required, and **sudo is not the fix**. A permission error usually means a previous sudo run left those files owned by root. Restore ownership and re-run without sudo:

```bash
sudo chown -R "$USER" ~/.claude.json ~/.claude ~/.cursor ~/.codex ~/.gemini ~/.contexer
contexer install
```

If your home directory itself is read-only (some managed/corporate setups), sudo won't help either — the files must be writable by the user running the assistant; talk to whoever manages the machine.

## `status` says `hooks: missing or partial`

Run `contexer install` (it is idempotent — re-running never duplicates hooks), then restart your AI assistant.

## `status` prints `WARNING: … is not valid JSON`

One of the assistant JSON config files named in the warning is corrupt — usually a hand-edit gone wrong. **Fix or remove the file before running `contexer install`**: install deliberately refuses to overwrite a corrupt config. Restore from a backup, fix the JSON, or — if you accept losing its contents — delete the file and re-run `contexer install`.

## I upgraded but the version didn't change

```bash
contexer version            # what's actually installed
uv tool upgrade contexer    # or, if a release is minutes old:
uv tool install --reinstall --refresh contexer
contexer reinstall          # re-sync hooks if they changed
```

Then restart your AI assistant — a running session keeps the old server process.

`contexer status` checks PyPI (2-second timeout, silent when offline) and prints an
`update:` line when a newer version exists. Set `CONTEXER_NO_UPDATE_CHECK=1` to skip
the check entirely (airgapped machines).

## Decisions aren't being stored

- Say *"store that as a constraint"* (or convention/architecture/pattern) to save explicitly.
- Content too similar to an existing entry (>70% token overlap) is silently rejected as a duplicate. Rephrase to include what specifically changed.
- Your **first prompt** of a session is captured as the current *task*. If it is phrased as a clear directive (*"always X"*, *"never Y"*), it is **also** auto-saved as a constraint — but a rule phrased indirectly may slip past the detector, so follow up with *"store that as a constraint"* to be sure.

## A stored decision isn't appearing at session start

Only `constraint` and `convention` decisions are pre-loaded at session start; `architecture` and `pattern` decisions are fetched on demand. A decision added mid-session shows up at the *next* session start.

## A repo's context disappeared / store file looks broken

Each repo's store is plain JSON at `~/.contexer/<repo_slug>.json`. A corrupt store file is treated as empty by the server (it never crashes a session) and is excluded from `contexer status` counts. Writes are atomic, so corruption should only result from external causes (disk issues, manual edits). If a file is corrupt, fix the JSON by hand or delete it and re-bootstrap with `/bootstrap`.

The `repo stores` line counts **repo stores only**. The same directory also holds the global rules (`_global.json`), the console statefile (`ui.json`), tombstone sidecars (`<repo_slug>.deleted.json`), and dot-prefixed caches — earlier versions counted those too, which inflated both the store count and `entries total`.

Linked git worktrees share the main worktree's store — a session opened in a worktree reads and writes the same context as the main checkout. Earlier versions keyed each worktree to its own store file; those stray stores are merged back automatically at the next session start on that repo and left behind as `*.json.migrated` (safe to delete once verified). Submodules and worktrees of a bare repository keep their own stores.

## `*.tmp` files in `~/.contexer/`

Leftovers from a hard-crashed write. Harmless — they are never read. `contexer status` cleans up any older than an hour.

## Hooks fire but nothing seems to happen

That's usually correct behavior: Contexer is silent by design. To verify it's actually working, check `ls ~/.contexer/` for a `.json` file named after your repo after a session in which a decision was made.

## No console URL at session start

Expected unless you asked for one. The [local console](ui.md) is **opt-in** — with no `[ui] autostart` in `~/.contexer/config.toml`, session start behaves exactly as it did before the console existed. Turn it on:

```toml
[ui]
autostart = true
```

**And even with autostart on, Cursor and Gemini CLI never show it.** Both hosts discard the human-facing status line entirely and take only the injected context, so there is nowhere for the URL to appear. Claude Code and Codex show it. Under Cursor or Gemini, run `contexer ui --open` when you want the console.

## `Port 31415 is in use by another process`

Something that isn't the Contexer console holds the port. The port is fixed on purpose — a printed URL has to survive a restart — so Contexer refuses rather than silently moving. Find the occupant, or move the console:

```bash
lsof -nP -iTCP:31415 -sTCP:LISTEN   # who has it
contexer ui --port 31500            # one-off
```

To move it permanently, set `port` in the `[ui]` table of `~/.contexer/config.toml`, then `contexer ui --stop && contexer ui`.

## The console won't start

```bash
contexer ui --status          # running? stale statefile? which port?
cat ~/.contexer/ui.log        # why it failed
```

`ui.log` is the daemon's only output — startup failures, bind errors, and error incident ids all land there (never your decision content, never the console token). `contexer ui --foreground` runs the server in your terminal instead of detaching it, which is the fastest way to watch a failing start.

If `--status` reports a **stale statefile**, nothing is wrong: the recorded daemon is gone and the next `contexer ui` replaces the file. If the console starts but the browser gets a `403`, check the address is `127.0.0.1` or `localhost` — the `Host` header is validated and any other hostname is rejected by design (that check is what stops a website from reading your store through your browser).

## `403` / unauthorized after clicking a session-start link

The `?p=…` in a printed console URL is a **pairing code, not the token**, and it is good for at most 20 minutes. That is deliberate: the session-start line is written into your assistant's transcript on disk, so anything in it has to expire. An old link from an earlier session will be rejected.

```bash
contexer ui --open      # prints (and opens) a fresh link
```

If a link that is definitely fresh still fails, the token may have been rotated out from under it — `contexer ui --reset-token` invalidates every previously printed URL and open tab.

## The console shows "store unreadable"

That repo's `~/.contexer/<repo_slug>.json` exists but cannot be parsed. The console says so rather than showing "0 decisions", because the two mean very different things. Fix the JSON by hand, or delete the file and re-bootstrap with `/bootstrap` — see [A repo's context disappeared](#a-repos-context-disappeared--store-file-looks-broken) above.

## Turning the console off completely

```bash
contexer ui --stop
```

Then remove the `[ui]` table from `~/.contexer/config.toml` (or just `autostart = true` from it). With no `[ui] autostart`, nothing starts the console and nothing prints a URL. To clean up its files as well, delete `~/.contexer/ui.json` and `~/.contexer/ui.log` — neither is needed again, and both are recreated on the next `contexer ui`.

---

Still stuck? [Open a bug report](https://github.com/bhargavamin/contexer/issues/new?template=bug_report.md) with the output of `contexer status` and `contexer version`, or ask on [Discord](https://discord.gg/Fk6JSaW4p).
