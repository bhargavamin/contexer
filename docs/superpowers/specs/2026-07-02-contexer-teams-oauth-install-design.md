# Design: zero-config remote OAuth on Contexer install (`contexer-teams`)

**Date:** 2026-07-02
**Status:** approved (design), pending spec review
**Scope:** installer/packaging only. Client-side registration of the remote Contexer
MCP server so the MCP client's *native* OAuth completes on first use. Claude Code CLI only.

## Goal

After `contexer install`, a brand-new user has the remote Contexer MCP server registered
in Claude Code pointing at `https://dev.contexer.ai/mcp`, and the client's **native MCP
OAuth** completes a browser sign-in on first use — no flags, no transport shim, no token
minting or pasting. Once authenticated, the already-running contexer-teams web app can show
that user's context.

This ticket delivers the **registration + OAuth enablement** only. Actually syncing the
local `~/.contexer` store to/from the remote is a **separate flow (later ticket)**.

## Non-goals (explicitly out of scope)

- Changing the local `contexer` stdio server, the hooks, or the `~/.contexer/*.json` store
  in any way. Local remains the source of truth and is read/written exactly as today.
- Bidirectional local↔remote context sync (separate ticket).
- Desktop, Cursor, Codex, Gemini. Claude Code CLI only this iteration (they use the same
  `{type:http,url}` schema, so they are a fast follow, not a rewrite).
- Migrating any hand-added manual remote entries from earlier testing.
- Any change to the remote server (it is a complete OAuth 2.1 AS+RS already).

## Key decisions (locked)

| Decision | Choice | Rationale |
|---|---|---|
| Relationship to local | **Additive** — new second MCP entry | Local store + hooks stay authoritative; remote is a mirror a later sync flow populates. No split-brain. |
| Entry name | **`contexer-teams`** | Distinct from the local `contexer` (stdio) entry; matches the backend project. Constant across environments. |
| Transport | Standard Streamable-HTTP (`type: http`) | Triggers Claude Code's **native** OAuth. No custom transport shim, no `--transport contexer-local`, no static token. |
| Remote purpose this ticket | **Auth-only** | It exists so OAuth runs and the web app can identify the user. No expectation the client calls its tools yet (sync ticket handles that). |
| Registration mechanism | **Direct config write** via `base._load`/`_save` | Same approach as every existing adapter; no dependency on the `claude` binary being on PATH; uniform + easy to make idempotent and to uninstall. |
| Endpoint selection | Prod by default; `CONTEXER_ENV=local` → localhost | HTTPS prod for all normal users; localhost is an explicit developer opt-in, off by default. |
| Clients | Claude Code CLI only | Stated primary target. |
| Migration | None | No detection/replacement of prior manual entries. |

## Config diff written on install (`~/.claude.json`)

The existing local entry is untouched; install adds exactly one sibling key:

```json
"mcpServers": {
  "contexer":       { "type": "stdio", "command": "/Users/<you>/.local/bin/contexer" },
  "contexer-teams": { "type": "http",  "url": "https://dev.contexer.ai/mcp" }
}
```

- `{"type":"http","url":...}` is exactly the shape that makes Claude Code run native OAuth
  (401 → `resource_metadata` discovery → Dynamic Client Registration at `/register` →
  authorization-code + PKCE(S256) in the browser → opaque access + refresh token → silent
  refresh). No secret/token is ever written to config.
- Written at **user scope** (top-level `mcpServers` in `~/.claude.json`), so it applies in
  every repo — matching how the current global install behaves.

## Endpoint selection — one baked-in constant

A single place to change the prod URL. In `contexer/adapters/claude.py` (or a shared
module-level constant):

```python
CONTEXER_TEAMS_PROD  = "https://dev.contexer.ai/mcp"
CONTEXER_TEAMS_LOCAL = "http://localhost:8080/mcp"

def _teams_url() -> str:
    return CONTEXER_TEAMS_LOCAL if os.environ.get("CONTEXER_ENV") == "local" else CONTEXER_TEAMS_PROD
```

- Default → prod HTTPS. `CONTEXER_ENV=local` → localhost:8080 (developer testing against a
  locally-running teams server). A normal user never gets localhost.

## Install / uninstall behaviour

- **Install** (in `claude.py`): `_load(~/.claude.json)` →
  `setdefault("mcpServers", {})["contexer-teams"] = {"type": "http", "url": _teams_url()}` →
  `_save(...)`. Idempotent: overwrites only its own key, leaves `contexer` and all other
  servers/keys intact. Emits a log line (`✓ contexer-teams (remote) registered → <url>`).
- **Uninstall** (in `claude.py`): `mcpServers.pop("contexer-teams", None)`; save only if
  removed. Removes only what install added; the local `contexer` entry is handled by the
  existing uninstall path.
- **Status** (optional, nice-to-have): show whether `contexer-teams` is registered and at
  which URL; do not attempt to report auth state (that lives in the client's keychain).

## First-use UX (what the user sees)

1. `contexer install` → prints that `contexer-teams` was registered at the prod URL.
2. Restart Claude Code.
3. `/mcp` lists **`contexer`** (connected) and **`contexer-teams`** (⚠ needs authentication).
4. User runs `/mcp` → selects `contexer-teams` → **Authenticate** → browser opens → sign in
   with any provider → redirected back → token stored in the OS keychain.
5. Thereafter the token auto-refreshes; the contexer-teams web app shows the user's context
   (once the separate sync flow populates it).

## Server-side interop prerequisites (verify before "done")

The remote server "already works", but these specific client-interop details decide whether
the browser flow completes. They are verification items, not build items:

- **localhost callback:** Claude Code uses a random `http://localhost:<port>/callback` redirect
  by default. The AS must accept the `http://localhost:*` range (or we would have to pin a
  fixed `--callback-port`, which the direct-write approach does not do). Confirm the AS allows
  loopback redirects with variable port.
- **`offline_access`:** must appear in the AS `scopes_supported` for Claude Code to obtain a
  refresh token and refresh silently.
- **DCR:** `/register` must accept repeated public-client registrations
  (`token_endpoint_auth_method: "none"`), since Claude Code re-registers per need.
- **HTTPS:** prod endpoint is HTTPS (satisfied).

## Testing

Unit (pytest, mirroring existing `tests/test_install.py` / `test_cli_commands.py` style):
- Default install writes `contexer-teams` with `type:http` and the **prod** URL.
- `CONTEXER_ENV=local` → the entry's URL is `http://localhost:8080/mcp`.
- The pre-existing local `contexer` stdio entry is present and unchanged after install.
- Re-install is idempotent (one `contexer-teams` key, prod URL, no duplicate).
- Unrelated `mcpServers` keys and top-level keys are preserved.
- Uninstall removes `contexer-teams` and only that (local `contexer` and other servers remain).
- Never writes a token/secret into config.

E2E (manual, documented):
- Fresh `~/.claude.json`; `contexer install`; confirm `/mcp` lists `contexer-teams` needing
  auth; complete the browser sign-in; confirm connected + no token in config.

## Open questions / follow-ups

- **Sync flow (separate ticket):** how the local `~/.contexer` store is pushed to / pulled
  from the remote once authenticated. Out of scope here.
- **Other clients:** Desktop/Cursor use the identical `{type:http,url}` schema — add in a
  follow-up once Claude Code is verified end-to-end. Codex/Gemini remote-HTTP+OAuth support
  is unverified.
- **Callback port hardening:** if the AS cannot allow variable loopback ports, revisit
  pinning `--callback-port` (would require a different registration mechanism).
