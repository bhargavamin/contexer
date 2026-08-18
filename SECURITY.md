# Security Policy

## Reporting a vulnerability

Please report security issues **privately** - do not open a public issue, PR, or Discord
message for anything exploitable.

Use either channel:

- **GitHub private advisory (preferred):** open a report at
  <https://github.com/bhargavamin/contexer/security/advisories/new>.
- **Email:** `devops.techpro@gmail.com` with `SECURITY` in the subject.

Please include:

- the version (`contexer --version`) and OS,
- a description of the issue and its impact,
- steps to reproduce or a proof of concept,
- any suggested fix, if you have one.

You'll get an acknowledgement within **7 days**. This is a personal open-source project
maintained in spare time, so please allow reasonable time for a fix before any public
disclosure - coordinated disclosure is appreciated, and you'll be credited (unless you'd
rather not be).

## Supported versions

Only the **latest release** on [PyPI](https://pypi.org/project/contexer/) receives security
fixes. Fixes ship as a new patch release; upgrade with `uv tool upgrade contexer`, then
`contexer reinstall`.

## What's in scope

Contexer is a local tool. Understanding what it is sharpens what counts as a vulnerability:

- It runs as a **local MCP server** (stdio - no listening port) plus shell/editor hooks.
- Decisions are stored as **plain JSON under `~/.contexer/`**, written atomically with `0600`
  permissions. Stored content is **data, never executed** - Contexer does not `eval` or run
  anything from the store.
- The **only** network call is an optional version check against PyPI during `contexer status`
  (disable with `CONTEXER_NO_UPDATE_CHECK=1`). No code or decision content leaves your machine.

Reports we're particularly interested in:

- a path by which a tool call or hook **writes outside `~/.contexer/`** or to an unintended
  store file (repo-resolution / path-handling bugs),
- store content that can cause **code execution** in the host editor or the server,
- a way for one repo's hooks/session to **read or corrupt another repo's** store,
- incorrect file permissions or a **torn/leaked write** exposing data to other local users,
- injection through hook command strings written into `~/.claude/settings.json` or
  `~/.cursor/hooks.json` during install.

## Out of scope

- Issues requiring an attacker who **already has** local access to your account or can run
  arbitrary code as you (Contexer's threat model assumes your own machine and account are trusted).
- The contents the **AI agent** chooses to store or how it behaves - Contexer stores and replays
  text; it doesn't control the model.
- Vulnerabilities in **upstream dependencies** (report those upstream; we'll bump once a fix is
  released).
- Anything depending on a user **manually editing** their own store/config files into a bad state.
