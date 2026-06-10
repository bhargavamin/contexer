# Troubleshooting

Start every diagnosis with:

```bash
contexer status
```

It reports the installed version, binary path, MCP registration, hook state, and store summary. It is built to survive corrupt files: a corrupt **config** file gets an explicit WARNING; a corrupt **store** file is simply excluded from the entry counts. Then find your symptom below.

---

## `contexer` doesn't show up in `/mcp`

1. `contexer status` — does it say `MCP server: registered`?
   - **NOT registered** → run `contexer install`, restart Claude Code.
   - **registered** but still missing in `/mcp` → restart Claude Code. The MCP server is spawned once at session start; config changes are not picked up mid-session.
2. Check the binary path printed by `status` exists: `ls -l ~/.local/bin/contexer`. If missing, `uv tool install contexer` first.

## `Permission denied` during `contexer install` / `uninstall` / `reinstall`

contexer writes only to files in your own home directory (`~/.claude.json`, `~/.claude/settings.json`, `~/.contexer/`) — being an administrator is not required, and **sudo is not the fix**. A permission error here almost always means a previous run *with* sudo left those files owned by root (Claude Code itself then can't update its own config either). Restore ownership and re-run without sudo:

```bash
sudo chown -R "$USER" ~/.claude.json ~/.claude ~/.contexer
contexer install
```

If your home directory itself is read-only (some managed/corporate setups), sudo won't help either — the files must be writable by the user Claude Code runs as; talk to whoever manages the machine.

## `status` says `hooks: missing or partial`

Run `contexer install` (it is idempotent — re-running never duplicates hooks), then restart Claude Code.

## `status` prints `WARNING: … is not valid JSON`

Your `~/.claude.json` or `~/.claude/settings.json` is corrupt — usually a hand-edit gone wrong. **Fix or remove the file before running `contexer install`**: install deliberately refuses to overwrite a corrupt config (it fails loudly rather than destroy whatever else was in it). Restore from a backup, fix the JSON, or — if you accept losing its contents — delete the file and re-run `contexer install`.

## I upgraded but the version didn't change

```bash
contexer version            # what's actually installed
uv tool upgrade contexer    # or, if a release is minutes old:
uv tool install --reinstall --refresh contexer
contexer reinstall          # re-sync hooks if they changed
```

Then restart Claude Code — a running session keeps the old server process.

## Decisions aren't being stored

- Say *"store that as a constraint"* (or convention/architecture/pattern) to save explicitly.
- Content too similar to an existing entry (>70% token overlap) is silently rejected as a duplicate. Rephrase to include what specifically changed.
- Your **first prompt** of a session is captured as the current *task*. If it is phrased as a clear directive (*"always X"*, *"never Y"*), it is **also** auto-saved as a constraint — but a rule phrased indirectly may slip past the detector, so follow up with *"store that as a constraint"* to be sure.

## A stored decision isn't appearing at session start

Only `constraint` and `convention` decisions are pre-loaded at session start; `architecture` and `pattern` decisions are fetched on demand. A decision added mid-session shows up at the *next* session start.

## A repo's context disappeared / store file looks broken

Each repo's store is plain JSON at `~/.contexer/<repo_slug>.json`. A corrupt store file is treated as empty by the server (it never crashes a session) and is excluded from `contexer status` counts. Writes are atomic, so corruption should only result from external causes (disk issues, manual edits). If a file is corrupt, fix the JSON by hand or delete it and re-bootstrap with `/bootstrap`.

## `*.tmp` files in `~/.contexer/`

Leftovers from a hard-crashed write. Harmless — they are never read. `contexer status` cleans up any older than an hour.

## Hooks fire but nothing seems to happen

That's usually correct behavior: Contexer is silent by design. To verify it's actually working, check `ls ~/.contexer/` for a `.json` file named after your repo after a session in which a decision was made.

---

Still stuck? [Open a bug report](https://github.com/bhargavamin/contexer/issues/new?template=bug_report.md) with the output of `contexer status` and `contexer version`, or ask on [Discord](https://discord.gg/Fk6JSaW4p).
