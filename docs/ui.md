# The local console

Contexer stores what your agents get told in plain JSON under `~/.contexer/`. The console is a
small web UI over that store — every repo on the machine in one place, with a repo switcher —
so you can read it, fix it, and approve what is waiting without opening a file or memorising a
CLI flag.

It is a loopback-only, single-user tool: it binds `127.0.0.1` and is reachable only from your own
machine. Nothing about serving it goes near the network. The exceptions are deliberate and few: the
Team view can trigger a team pull or share, and the Config view can start a Teams login or log out —
each one the same path the CLI already uses, and none of them a way to read or set a credential.

```bash
contexer ui --open
```

## Starting and stopping

`contexer ui` starts the console daemon if it is not already running and prints where to find it:

```
Console: http://127.0.0.1:31415/?p=K3QZ7YB4NMXA
  log:          /Users/you/.contexer/ui.log
```

Open that URL and you land on the current repo's dashboard — or on the Config view if this machine
has no stores yet. The daemon keeps running in the background after the command returns; it exits
on its own once you stop using it (see [Idle timeout](#idle-timeout-and-restarts)).

| Flag | What it does |
|---|---|
| *(none)* | Start the console if needed, print the URL and the log path |
| `--open` | Same, and open the URL in your browser |
| `--status` | Report whether the console is running, on which port, with the current URL — starts nothing |
| `--stop` | Terminate the running console (`Console stopped.` / `Console was not running.`) |
| `--port N` | Use port `N` for this invocation instead of `[ui] port` (1-65535) |
| `--foreground` | Run the console in this terminal instead of detaching it; Ctrl-C stops it. Prints no URL — for watching output or debugging a startup failure |
| `--reset-token` | Stop the console, discard its credential, and start again with a fresh one. Any open tab and any previously printed URL stop working |

`--stop` is checked first, then `--status`, then `--foreground`; the rest apply to a normal start.

`contexer ui --status` is laid out like `contexer status`:

```
contexer console 0.26.1
  state:        running (pid 41207, started 2026-07-31T08:12:04Z)
  port:         31415
  url:          http://127.0.0.1:31415/?p=K3QZ7YB4NMXA
  statefile:    /Users/you/.contexer/ui.json
  log:          /Users/you/.contexer/ui.log
```

If something else already holds the port, `contexer ui` says so and exits rather than guessing a
different one — the URL is meant to be stable, so the port is fixed, never scanned. Pick another
with `--port N` or `[ui] port`.

## Autostart at session start

Autostart is **opt-in**. Add it to `~/.contexer/config.toml`:

```toml
[ui]
autostart = true
```

With it on, every session start appends the console to the status line your assistant already
shows you:

```
Contexer: 4 constraints, 8 conventions loaded. | console http://127.0.0.1:31415/?p=K3QZ7YB4NMXA#/store/myrepo-1a2b3c4d
```

The link deep-links to the repo you just opened. The daemon is started only if it is not already
running, and the whole thing is wrapped: if the console cannot start, the session gets its context
anyway and the line simply has no URL.

With it off — the default — nothing changes at all. No URL, no background process, no statefile
read; session start is byte-identical to a build without the console. That is deliberate:
installing an open-source tool should not imply a listening socket. This page is the only place
the feature is announced, which is why it is worth reading before you turn it on.

**Only Claude Code and Codex actually show you that line.** Cursor and Gemini CLI discard the
human-facing status line entirely — their hook contracts take injected context and nothing else —
so under those two, autostart still starts the daemon but you will never see a URL. Run
`contexer ui --open` instead. This is the hosts' hook model, not something Contexer can work
around.

## Configuration

All console settings live in the `[ui]` table of `~/.contexer/config.toml`. Every key is optional.

| Key | Type | Default | Meaning |
|---|---|---|---|
| `autostart` | bool | `false` | Start the console and print its URL at session start |
| `port` | int | `31415` | Fixed loopback port. Never scanned, so a printed URL survives a restart |
| `idle_timeout_minutes` | int | `60` | Minutes without a user-driven request before the daemon exits. Minimum 1 |

```toml
[ui]
autostart = true
port = 31500
idle_timeout_minutes = 240
```

A very large `idle_timeout_minutes` effectively means "never exit". There is no upper bound when
you write the file by hand; the Config view in the browser accepts 1-1440 (24 hours) and
1024-65535 for the port, because those are the values worth clicking to.

An invalid value is an error, not a silent fallback: `contexer ui` refuses to start and names the
key. Session-start autostart is the exception — a broken config costs you the console, never your
context.

If you hand-edit `config.toml`, keep `[ui]` **last**. TOML binds every key after a table header to
that table, so a top-level setting such as `redact_secrets` written below `[ui]` becomes
`ui.redact_secrets` and is ignored.

Changing `port` or `idle_timeout_minutes` takes effect when the daemon next starts. Restart it with
`contexer ui --stop && contexer ui`.

## The views

The sidebar switches between repos; the seven views below all apply to whichever repo is selected.
Routes are in the URL fragment (`#/store/<slug>/decisions/<id>`), so the back button and
bookmarking work.

| View | What is in it |
|---|---|
| **Dashboard** | Repo name and path, when the store was last touched, row count. Stat cards for decisions, what needs you, global rules, and cached team rows. Subtype and status breakdowns as bar rows, the review queue with inline approve/reject, and a recent-activity timeline |
| **Decisions** | Everything stored for the repo. Search (press `/`) plus subtype and status filters; the detail pane shows the content, rationale, subtype/status/confidence chips, recurrence, who created it, share state, and the full revision timeline `v1..vN` with each revision's source. Edit and delete live here |
| **Review** | What has not reached an agent yet: pending decisions, and proposed updates to already-approved ones rendered as before/after diffs. Approve or reject each one. Rejecting maps to the store's `ignore` state, so it is never surfaced again |
| **Global rules** | The rules in `~/.contexer/_global.json` that apply to every repo on the machine. Add and delete; constraints and conventions only, same as the CLI |
| **Team** | Team decisions cached on this machine, read-only, with sync staleness, a "Pull now" button, and a picker for what to share upward. The console never writes team decisions directly. A pull that fails because the Teams session is dead offers to fix it — see [Signing in and out of Teams](#signing-in-and-out-of-teams). In local mode it offers to log in and connect this machine |
| **Deleted** | Tombstoned decisions, with restore |
| **Config** | The `[ui]` settings, the capture toggles (`redact_secrets`, `skip_confirm`), the team connection, and the Teams session row — its state, expiry and Log in / Log out buttons. No token is ever shown, and none can be typed in |

Every action is a request followed by a refetch — there is no client-side copy of your store to
drift out of date. While the tab is visible and you are not typing, editing, or confirming
something, the view refreshes every 10 seconds.

A store whose file cannot be parsed says "store unreadable" rather than pretending it has no
decisions. That notice lives on the Dashboard, which is where the repo switcher points; the
Decisions, Team and Deleted views of an unreadable store refuse to load instead, because the
repo path they would need is inside the file that will not parse. The same is true of a store
file naming a path Contexer will not treat as a repo (your home directory, `/`, a config
directory): it is listed, and deliberately not readable.

## Editing, deleting, and restoring

**Editing appends a revision.** Nothing is overwritten in place: an edit becomes `vN+1`, the
previous text stays in the revision timeline with its source, and the decision keeps its status
(approved stays approved, pending stays pending). Because history is kept, edits are not
confirmed. If an agent session writes to the same decision while you have the editor open, your
save is refused rather than clobbering that write: the console tells you it changed underneath you
and reloads the current version.

**Deleting writes a tombstone.** The entry moves out of the live store into
`~/.contexer/<slug>.deleted.json`, appears in the Deleted view, and can be restored. Deletes are
confirmed.

A tombstone also blocks re-capture. Contexer can re-derive a decision from sources it re-reads
every session — a memory file, a repo scan, a mined conversation — so without the tombstone a
decision you deleted would simply come back next session and the delete would look broken. Memory
sync, bootstrap scanning and mining, and ordinary captures all check the tombstones first and
silently skip anything you deleted. Restoring the decision lifts that block.

One exception, by design: stating the rule again yourself re-captures it. A directive you type in
a session ("always X", "never Y") is an explicit instruction, so it is not filtered against the
tombstones the way a re-derived decision is.

Three limits worth knowing:

- **Tombstones are capped at 500 per repo**, oldest deletions dropped first, because every capture
  compares against the whole list while holding the store lock. Past 500 deletions in one repo the
  most ancient tombstones stop being restorable — the same bargain the live store already makes at
  its own 500-entry cap.
- **Restore is refused when the store is full**, rather than silently evicting some other decision
  to make room. Delete something first.
- **If the tombstone file itself cannot be parsed**, the Deleted view says so instead of showing an
  empty list, and deleting is refused so a corrupt file is not overwritten with a fresh one. Until
  it is fixed or removed, deleted decisions in that repo can be re-captured again.

## Signing in and out of Teams

The Config view has a **Teams session** row. It reports which of five states this machine is in,
when the credential expires, and one sentence about what that means:

| State | What it means |
|---|---|
| `signed in` | Stored OAuth credentials match the configured endpoint's issuer and are unexpired |
| `session expired` | They match but are past expiry. Logging in again refreshes them |
| `refresh rejected` | Expired *and* the refresh grant was refused. Refresh tokens are single-use and rotate, so a stale one is dead — a new login is the only fix |
| `static token only` | There are no matching OAuth credentials, so a static `token` from `config.toml` is what would be used |
| `signed out` | No usable credential at all |

`static token only` exists because of a real failure: a session that had expired days earlier fell
back to a long-dead static token, and every error downstream said "bad token" — indistinguishable
from a token that was simply wrong. The row now names which credential is in play before anything
fails.

**Log in** starts the same flow as `contexer login`: the daemon runs it as a tracked subprocess,
your browser opens, and the row says `waiting for your browser…` until it finishes. The console
polls about once a second and then reports what actually happened. A login started somewhere else —
another tab, or a terminal — is not an error here: only one runs at a time, so the console attaches
to the one already in flight and waits for it. A successful login writes `mode` and `endpoint` into
`config.toml` itself, which is why the Team view can offer it in local mode.

**Log out** is `contexer logout`, and it is confirmed: it removes this machine's stored credentials
for that endpoint. Your local decisions are untouched.

**A pull that fails on a dead session offers the fix.** "Pull now" on the Team view used to have
one answer for every failure — a message and nothing to act on, even when a single click was the
cure. A pull that fails *because the session is unusable* now renders a panel with the reason and a
**Log in** button, and after a successful login it re-runs that same pull and reports its real
result.

A pull that fails for any other reason keeps the plain message it always had. Being told to log in
again when the endpoint is unreachable, or when the repo has no git remote to key team context on,
would send you after the wrong problem.

## Security model

The store is fed to your agents as authoritative policy, so a write here is a change to how every
future session in that repo behaves. That is the surface worth defending, and the defences are
deliberately boring:

- **Loopback only.** The daemon binds `127.0.0.1`. There is no setting to expose it, and no CORS
  headers, so nothing off-machine can reach it or read a response.
- **Every route is authenticated**, including the health check. The credential is a long-lived
  token in `~/.contexer/ui.json`, owner-readable only (mode 0600). It is never printed.
- **The URL carries a pairing code, not the token.** `?p=…` is a short-lived code derived from the
  token over a 10-minute window (the previous window is still accepted, so a link printed at 09:59
  works at 10:01). The browser exchanges it once for a session cookie. This matters because the
  session-start line is written into your assistant's transcript on disk — and transcripts get
  synced and shared. Whatever leaks there is dead within 20 minutes.
  A code is accepted **only** on the exchange itself; it does not open the API or the assets, so a
  leaked line cannot be replayed against an endpoint directly. Be clear-eyed about what it still
  is, though: exchanging a code is how you log in, so a code that is still inside its window is
  worth a session — reads and writes both — to whoever holds it and can reach your loopback. The
  protection here is that the window is short and the long-lived token is never printed, not that
  the code is somehow read-only.
- **`Host` and `Origin` are validated.** Without that check, a website that re-resolves its own
  name to `127.0.0.1` could read your whole decision store from your browser (DNS rebinding).
- **Writes need more than a cookie.** Every mutating request must also carry a header the page can
  only obtain same-origin, and an `Origin` the daemon recognises, so no other website can make your
  browser approve, edit, or delete a decision — a cookie on its own is refused.
- **Repos are addressed by slug**, resolved against a listing of `~/.contexer/`. The daemon never
  accepts a filesystem path from a request, so no crafted request can make it read or write
  somewhere else.
- **The log is deliberately thin.** `~/.contexer/ui.log` records methods, paths, statuses, and
  error incident ids — never decision content. Anything that looks like a credential (a pairing
  code, a token, a cookie value) is redacted on the way in, including on the error paths that echo
  a malformed request back, and each line is length-capped. Tracebacks go to the log and never into
  a response body; the browser gets an incident id to quote instead.
- **No credential is ever readable, and none can be typed in.** The console can *start* a login and
  can log out — the two operations the CLI already offers — but it cannot see or set a token.
  `/api/config` carries no token, refresh token or secret: the session row is built from a state
  name, an expiry, an issuer, a scope and one human sentence, and `token_set` is a bare boolean.
  A `PUT /api/config` writes only console and capture settings and refuses credential keys, so
  neither the page nor anything reaching the API can install a credential of its own choosing.
  Nor can it aim the login: the login endpoint takes no endpoint (or any other field) from the
  request — a caller-supplied one would point the OAuth flow at somebody else's IdP and store a
  token for it — it uses the configured profile endpoint. Both operations are mutations, so they
  need the same header and `Origin` as every other write, and they count against the same rate
  limit.

  Be clear about what this does mean: whoever holds a console session can end your Teams session,
  or start a login that lands in your browser. Those are recoverable and visible, which is why they
  are allowed; nothing here exposes the credential itself.

What this does **not** protect against, stated plainly: any process running as you on this machine
can read `~/.contexer/ui.json` and therefore everything the console can see — though it could
equally just read `~/.contexer/*.json` directly. This is a single-user localhost tool and it is not
hardened for a machine where you share an account, or for a multi-user box where others can read
your home directory. Leave autostart off in that situation, and start the console only when you
want it.

## Idle timeout and restarts

The daemon exits after `idle_timeout_minutes` with no user-driven request, and comes back on the
next session start (with autostart on) or the next `contexer ui`. Restarting it is cheap, so the
default is to be self-cleaning rather than permanently resident.

Background refreshes from an open tab are marked as polls and do **not** count as activity — a tab
you forgot about in another window cannot keep the daemon alive forever. When the daemon does go
away underneath it, the tab shows a disconnected banner with a Retry button; start the console
again and hit Retry.

The token outlives the process, so an open tab keeps working across a restart, and a URL you
bookmarked keeps resolving to the same port. Only `contexer ui --reset-token` invalidates it.
Upgrading Contexer replaces a running daemon automatically the next time one is needed, so you
never end up with a new wheel serving an old UI.

## Caveats

**Saving settings from the browser rewrites `config.toml` and loses your comments.** Contexer's
TOML support is read-only, and the file is re-serialised from its parsed values, so hand-written
comments and ordering do not survive a save from the Config view. The previous file is copied to
`config.toml.bak` first (both files are owner-only, mode 0600). If you keep comments in that file,
edit it by hand and treat the Config view as read-only.

Note the backup only buys you one save: it is overwritten each time, so a second save replaces it
with the already-stripped version. Recover your comments after the first save, or not at all.

**"Pull now" tells you when a sync failed.** A pull that could not reach the endpoint, or that was
never attempted, says so rather than reporting "0 updated, 0 removed" as though nothing had changed
upstream. It uses the same short timeout as a session-start sync, so a slow connection can report a
failure that a retry resolves. When the failure is the Teams session itself, the reason comes with a
Log in button that then re-runs the pull — see
[Signing in and out of Teams](#signing-in-and-out-of-teams).

**Cursor and Gemini CLI never show the session-start URL.** See
[Autostart](#autostart-at-session-start).

**The console never writes team decisions.** Sharing upward and pulling down are available; team
content itself is governed on the team side.

**There is no dark/light toggle.** The console is dark only.

## When something goes wrong

Startup problems, port conflicts, expired links, and how to disable the console completely are all
in [troubleshooting](troubleshooting.md). The daemon's own output is in `~/.contexer/ui.log`.
