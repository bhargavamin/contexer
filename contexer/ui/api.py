"""JSON API for the local console - the one module that knows both HTTP and the store.

`server.py` owns transport: binding, the auth guard, headers, static files, the watchdog.
Everything here turns an already-authenticated request into a status code plus a JSON-able
payload, by calling PUBLIC functions in `contexer.console_api` / `store` / `config` /
`share` / `team_context`. It never opens, parses or writes a store file: when the shape the
console needs does not exist yet, the fix is a new public read in `console_api.py`, the module
that owns every projection this file renders, not a file read here.

Field names are the console's contract (`assets/console.js` codes against them), so a rename
here silently blanks a pane there.
"""
from urllib.parse import unquote

from contexer import auth, config, console_api, share, share_status, store, team_context
from contexer.ui import daemon

# Mirrored in console.js as maxlength attributes; enforced here because the browser is not
# the only caller (curl with the persisted token is a supported path).
MAX_TITLE = 100
MAX_CONTENT = 8000

# Bounds on a list request, so one URL can't ask the daemon to serialize the whole store.
MAX_LIMIT = 1000
MAX_OFFSET = 10 ** 6
MAX_QUERY = 200
MAX_SHARE_IDS = 200

# Bounds on the `file=` filter (Task 4 of #174): a generous cap on how many files one commit
# or one filter box plausibly names, and a generous per-path length - well past MAX_QUERY
# because a real repo-relative path (nested monorepo packages) can run longer than a search
# phrase.
MAX_FILES = 50
MAX_FILE_LEN = 300

# Sanity cap on a short single-token field (a subtype, an id). The vocabulary itself is
# validated by the store; this only keeps a megabyte of junk out of an error message.
MAX_WORD = 64

# The global store accepts constraint/convention only. `store.update_global_decision` is the
# authority and rejects anything else; this is duplicated ONLY to answer with a precise 400
# instead of the store's shared "not stored" signal (which also means "duplicate").
GLOBAL_SUBTYPES = ("constraint", "convention")

# Credential states a login actually fixes, so a failed pull is worth flagging as auth-shaped.
# `static_only` is deliberately absent: from here a rejected static token and an unreachable
# endpoint look identical, and telling someone to log in while their network is down is its own
# wrong turn. `logged_in` is absent because the credential is fine - the failure is elsewhere.
LOGIN_FIXES = ("expired", "refresh_failed", "none")

# Written into every revision this surface creates, so the revision timeline shows which
# changes a human made in the console vs. what an agent captured.
SOURCE = "ui"


class ApiError(Exception):
    """A response the console can render: a status plus `{error, ...extra}`."""

    def __init__(self, status: int, message: str, **extra: object):
        super().__init__(message)
        self.status = status
        self.message = message
        self.extra = extra


def dispatch(method: str, path: str, query: dict, body: object) -> tuple[int, object]:
    """Route one API request. Returns (status, payload); raises ApiError for a rejection.

    `path` arrives raw. Splitting on "/" BEFORE percent-decoding each segment is deliberate:
    an encoded separator (`%2f`) then stays inside one segment instead of forging a new one,
    so it reaches `resolve_store_slug`, which rejects it."""
    parts = [unquote(p) for p in path.split("/") if p]
    if not parts or parts[0] != "api":
        raise ApiError(404, "no such endpoint")
    try:
        return _route(method, parts[1:], query, body)
    except config.ConfigError as exc:
        raise ApiError(400, str(exc)) from exc


def _route(method: str, parts: list[str], query: dict, body: object) -> tuple[int, object]:
    if parts == ["stores"] and method == "GET":
        return 200, console_api.list_stores()

    if parts == ["config"]:
        if method == "GET":
            return 200, _config()
        if method == "PUT":
            return _write_config(body)

    if parts == ["login"] and method == "POST":
        return _login(body)

    if parts == ["login", "status"] and method == "GET":
        return _login_status(query)

    if parts == ["logout"] and method == "POST":
        return _logout(body)

    if parts == ["global"]:
        if method == "GET":
            # The store's dict verbatim, same rule as `<slug>/deleted`: its `ok`/`error` pair is
            # the ONLY thing that tells "no global rules" from "the global file is unreadable",
            # and re-wrapping it in a bare {"rules": ...} threw that signal away.
            return 200, console_api.list_global_rules()
        if method == "POST":
            return _add_global(body)

    if len(parts) == 2 and parts[0] == "global" and method == "DELETE":
        ok, message = console_api.delete_global_rule(parts[1])
        if not ok:
            raise ApiError(404, message)
        return 200, {"message": message}

    if len(parts) == 2 and parts[0] == "team" and method == "GET":
        return 200, _team(parts[1])

    if len(parts) >= 2 and parts[0] == "store":
        return _store_route(method, parts[1], parts[2:], query, body)

    raise ApiError(404, "no such endpoint")


def _store_route(method: str, slug: str, rest: list[str], query: dict,
                 body: object) -> tuple[int, object]:
    if not rest and method == "GET":
        # Addressed BY SLUG, not by repo path: a store file that will not parse has no repo
        # path to summarize, yet it is still a known address and must reach the console's
        # "store unreadable" view instead of a 404 the console renders as a generic error.
        summary = console_api.store_summary(slug)
        if summary is None:
            raise ApiError(404, "no such store")
        return 200, summary

    repo_path = _repo(slug)

    if rest == ["deleted"] and method == "GET":
        # The store's dict verbatim: its `ok`/`error` pair is the ONLY thing that tells
        # "nothing deleted" from "the tombstone sidecar is unreadable", and re-wrapping it
        # in a bare {"tombstones": ...} threw that signal away.
        return 200, console_api.list_tombstones(repo_path)

    if rest == ["decisions"] and method == "GET":
        return 200, console_api.list_decisions(
            repo_path,
            query=_str_param(query, "q")[:MAX_QUERY],
            subtype=_str_param(query, "subtype"),
            status=_str_param(query, "status"),
            # Repeatable (`file=a&file=b`) AND comma-separated (`file=a,b`) both work -
            # _list_param flattens either into one list, so the console's single filter input
            # can just comma-join and a `curl` caller can repeat the param instead.
            files=_list_param(query, "file", MAX_FILES, MAX_FILE_LEN) or None,
            # Absent/0 means MAX_LIMIT, not "no cap": console_api.list_decisions reads `limit <= 0`
            # as unbounded, so forwarding the bare 0 let a `limit`-less URL serialize every
            # row after all - the exact thing MAX_LIMIT is here to prevent.
            limit=_int_param(query, "limit", MAX_LIMIT) or MAX_LIMIT,
            offset=_int_param(query, "offset", MAX_OFFSET),
        )

    if rest == ["pull"] and method == "POST":
        return _pull(repo_path)

    if rest == ["share"] and method == "POST":
        return _share(repo_path, body)

    if len(rest) >= 2 and rest[0] == "decisions":
        return _decision_route(method, repo_path, rest[1], rest[2:], body)

    raise ApiError(404, "no such endpoint")


def _decision_route(method: str, repo_path: str, entry_id: str, rest: list[str],
                    body: object) -> tuple[int, object]:
    if not rest:
        if method == "GET":
            detail = console_api.get_decision_detail(repo_path, entry_id)
            if detail is None:
                raise ApiError(404, "no such decision")
            return 200, detail
        if method == "PATCH":
            return _edit(repo_path, entry_id, body)
        if method == "DELETE":
            return _finish(repo_path, entry_id, *store.delete_decision(repo_path, entry_id,
                                                                       actor=SOURCE))

    if rest == ["approve"] and method == "POST":
        return _approve(repo_path, entry_id, body)

    if rest == ["restore"] and method == "POST":
        return _finish_restore(repo_path, entry_id, *store.restore_decision(repo_path, entry_id))

    raise ApiError(404, "no such endpoint")


# ── handlers ────────────────────────────────────────────────────────────────────

def _approve(repo_path: str, entry_id: str, body: object) -> tuple[int, object]:
    """Approve or reject. "reject" is the console's word for the store's `ignore` action -
    the store vocabulary (approve/ignore/edit/skip/dismiss) stays unchanged."""
    action = _body(body, "action").get("action", "approve")
    if action == "reject":
        action = "ignore"
    if action not in ("approve", "ignore"):
        raise ApiError(400, "action must be 'approve' or 'reject'")
    return _finish(repo_path, entry_id, *store.approve_decision(repo_path, entry_id, action))


def _edit(repo_path: str, entry_id: str, body: object) -> tuple[int, object]:
    payload = _body(body, "content", "title", "subtype", "if_version")
    content = _text(payload, "content", MAX_CONTENT)
    title = _text(payload, "title", MAX_TITLE)
    subtype = _text(payload, "subtype", MAX_WORD)
    if_version = payload.get("if_version")
    if if_version is not None and (isinstance(if_version, bool) or not isinstance(if_version, int)):
        raise ApiError(400, "if_version must be an integer")
    # `subtype: ""` is forwarded, not rejected: the store reads a blank subtype as "leave it
    # alone" so a legacy entry that carries no subtype stays editable. Only a MISSING field is
    # "nothing to change" here; a body of nothing but `subtype: ""` is the store's own refusal.
    if content is None and title is None and subtype is None:
        raise ApiError(400, "nothing to change - pass content, title, or subtype")

    # `source="human"`, not SOURCE - same reasoning as `_add_global`: an edit arriving here was
    # typed by a developer, and "ui" names the surface, not the author. Only this call site
    # changes; `edit_decision`'s own "ui" default still covers a direct (non-console) caller,
    # which by definition is not a developer at a form.
    ok, message, extra = store.edit_decision(repo_path, entry_id, content=content, title=title,
                                             subtype=subtype, source="human",
                                             if_version=if_version)
    if not ok and message == store.EDIT_CONFLICT:
        # A live MCP session wrote this decision between the console's read and this save.
        raise ApiError(409, message, current_version=(extra or {}).get("current_version"))
    return _finish(repo_path, entry_id, ok, message)


def _pull(repo_path: str) -> tuple[int, object]:
    """Refresh the team cache. Degrades to a renderable `{error}` rather than a stacktrace:
    being offline or unauthenticated is an ordinary state for this button.

    `team_context.pull` is the seam here - the one `contexer pull` uses, at the full transport
    timeout - NOT `team_context.refresh`. `refresh` bounds the transport to ~3s because
    SessionStart runs before the assistant can answer; against a cold-start endpoint replying
    in 4-8s this button failed on every click (counting a consecutive failure each time) while
    the same pull succeeded in a terminal on the same machine. `refresh` also drains the share
    outbox, and a button labelled Pull must not push: the drain still runs at SessionStart and
    ahead of every explicit share (`share.share_ids`), so a read costs nothing by omitting it.

    The trade is that `pull` can raise where `refresh` never did. The cloud's own failures are
    already swallowed into the degraded stamp below, so what is left is local (disk, git) and
    must still reach the console as text, never as a 500 - same rule as `_share`.

    `pull` answers `(0, 0)` for a clean no-op, a refused connection and a rejected token alike,
    so its return value cannot tell success from failure - reporting "Pulled - 0 updated, 0
    removed." over an unreachable endpoint is the lie this avoids. The sync's own `last_sync`
    stamp is the evidence: a NEW stamp carrying `ok: false` is a failed attempt, and an
    UNCHANGED stamp means no attempt was made at all (nothing to key on), which is not a
    success either."""
    profile = config.load_profile()
    if profile.mode != "team" or not profile.endpoint:
        return 200, {"error": "Not connected to a team - run `contexer login` first."}
    before = console_api.team_snapshot(repo_path)["last_sync"]
    try:
        # The already-loaded profile, so the mode check above, the sync, and the message below
        # all describe ONE reading of config.toml.
        upserted, removed = team_context.pull(repo_path, profile=profile)
    except Exception as exc:
        return 200, {"error": f"Pull failed - {exc}"}
    after = console_api.team_snapshot(repo_path)["last_sync"]
    if after != before and after.get("ok") is False:
        failures = after.get("consecutive_failures") or 0
        streak = f" {failures} consecutive failures." if failures > 1 else ""
        return 200, _pull_failure(f"Pull failed - could not sync with {profile.endpoint}. "
                                  f"The cached rows are unchanged.{streak}", profile,
                                  rejected=after.get("error") == "auth")
    if after == before and not upserted and not removed:
        return 200, _pull_failure("Pull did not run - no sync was attempted. Team context is "
                                  "keyed on this repo's git remote, so a checkout with no "
                                  "origin has nothing to sync against.", profile,
                                  attempted=False)
    return 200, {"message": f"Pulled - {upserted} updated, {removed} removed."}


def _pull_failure(error: str, profile: config.Profile, *, attempted: bool = True,
                  rejected: bool = False) -> dict:
    """A failed pull, tagged `auth` when the credential is what explains it.

    The flag is the whole point: with it the console can offer "Log in again" instead of
    pattern-matching error strings, which is how an expired session spent three days looking
    like a bad token.

    Two independent pieces of evidence, because either alone leaves a hole. The LOCAL state
    (`auth_state`) catches an expired or unrefreshable session before the network is even
    consulted. `rejected` carries the SERVER's verdict - `team_context` now records
    `last_sync.error == "auth"` when the endpoint refused the credential - and that is the only
    thing that can see a token which is unexpired locally but revoked upstream, the case where
    `auth_state` honestly reports `logged_in` and would otherwise blame the network.

    `attempted=False` means no sync was even tried, which has two causes: no credential to try
    with, or no git remote to key team context on. Only the first is worth a login, and `not
    profile.token` is what tells them apart - with no static fallback, an unusable session
    means the token resolved to None and the sync could not start; with one, the cause is the
    missing remote, and sending that user to a login screen is the wrong turn."""
    state = auth.auth_state(profile)
    local_says_auth = state["state"] in LOGIN_FIXES and not (not attempted and profile.token)
    if not (local_says_auth or rejected):
        return {"error": error}
    detail = state["message"] if local_says_auth else (
        "The endpoint rejected this machine's credential - log in again.")
    return {"error": f"{error} {detail}", "auth": True, "state": state["state"]}


def _login(body: object) -> tuple[int, object]:
    """Start the browser login flow as a tracked subprocess.

    The body must be EMPTY - not "an endpoint is ignored" but "no field is accepted": an
    endpoint from a request would point the OAuth flow at an attacker's IdP and persist the
    token it returned, so the endpoint comes from config and a body carrying one is refused
    loudly rather than silently dropped."""
    _body(body)
    try:
        return 202, {"job": auth.start_login_job()}
    except auth.LoginJobBusy as exc:
        # The 409 names the job already in flight: without it a second tab can only watch the
        # session go live and never learns that the login it attached to actually failed.
        raise ApiError(409, str(exc), job=exc.job_id) from exc


def _login_status(query: dict) -> tuple[int, object]:
    status = auth.login_job_status(_str_param(query, "job")[:MAX_WORD])
    if status is None:
        raise ApiError(404, "no such login job")
    return 200, status


def _logout(body: object) -> tuple[int, object]:
    """Sign out, resolving a login still in flight first.

    A browser flow started minutes earlier finishes AFTER the unlink and rewrites both the
    creds file and config.toml, undoing a logout the user was told had succeeded. The console's
    own polling flag cannot prevent it - it is per-tab, so a second tab or a `contexer login`
    in a terminal is invisible to it, which is why the server has to be the one to resolve the
    race. Killing the job BEFORE the unlink is what closes the window: a child that already
    wrote its credentials still loses them to the unlink that follows.

    The kill is reported, not silent: cancelling somebody's half-finished sign-in is a second
    thing that happened, and a message naming only the sign-out would leave them waiting on a
    browser tab that can no longer complete."""
    _body(body)
    # Name the real reason: stop_login_job's default says the console stopped, which is a
    # different event, and a tab polling that job would be told something that did not happen.
    cancelled = auth.stop_login_job("Login was cancelled - you signed out.")
    message = ("Signed out of Contexer Teams. Team context stays cached until the next pull."
               if auth.logout() else "No Contexer Teams credentials were stored.")
    if cancelled:
        message += " A sign-in that was still running was cancelled."
    return 200, {"message": message}


def _share(repo_path: str, body: object) -> tuple[int, object]:
    ids = _body(body, "ids").get("ids")
    if not isinstance(ids, list) or not ids:
        raise ApiError(400, "ids must be a non-empty list")
    if len(ids) > MAX_SHARE_IDS:
        raise ApiError(400, f"at most {MAX_SHARE_IDS} ids per request")
    for value in ids:
        if not isinstance(value, str) or not value or len(value) > MAX_WORD:
            raise ApiError(400, "every id must be a non-empty string")
    try:
        status = share.share_ids(repo_path, ids)
    except Exception as exc:
        # share_ids already swallows cloud failures (it queues them); anything left is local
        # and must still reach the console as text, never as a 500. `outcome`/`ok` are present
        # here too, so a page branching on them never meets a second response shape.
        return 200, {"error": f"Share failed: {exc}", "outcome": "share_failed", "ok": False}
    # The counts, not only the sentence. `share.py` computed all of these and then rendered them
    # into one English string, so this endpoint shipped prose as a machine result and no console
    # page could tell "3 synced" from "3 unsaved". `message` stays for the pages that render it
    # today; `outcome` is the stable token to branch on.
    return 200, {
        "message": share_status.describe(status),
        "outcome": status.outcome,
        "ok": share_status.is_ok(status),
        "sent": status.sent,
        "queued": status.queued,
        "at_capacity": status.at_capacity,
        "invalid": status.invalid,
        "lost": status.lost,
        "total": status.total,
        "unknown_ids": list(status.unknown_ids),
    }


def _add_global(body: object) -> tuple[int, object]:
    payload = _body(body, "content", "subtype", "title")
    content = _text(payload, "content", MAX_CONTENT, required=True)
    title = _text(payload, "title", MAX_TITLE) or ""
    subtype = _text(payload, "subtype", MAX_WORD) or GLOBAL_SUBTYPES[1]
    if subtype not in GLOBAL_SUBTYPES:
        raise ApiError(400, f"subtype must be one of: {', '.join(GLOBAL_SUBTYPES)}")
    # `created_by="human"`, not SOURCE: a rule reaching this handler was typed by a developer
    # into the Add form, and `created_by` is where that fact has to land - it drives the entry's
    # attribution, revision 1's `source`, and `revisions.compute_confidence`'s "Stated by developer" +20.
    # SOURCE ("ui") stays the session id and is deliberately not reused as the provenance:
    # `share._WIRE_SOURCES` is a closed allowlist (ai | human | scan | bootstrap | memory | plan)
    # mirroring the cloud's push_decision enum, so `_wire_source` degrades "ui" back to "ai" on
    # the way out - a rule born "ui" would reach the cloud indistinguishable from an AI-authored
    # one. `_edit` passes "human" for the same reason.
    stored, _entry_id = store.update_global_decision(content, SOURCE, subtype, title=title,
                                                     created_by="human")
    if not stored:
        # The store signals a duplicate and its REFUSAL to write over an unparseable global
        # file with the same `(False, None)`. Reporting both as "already exists" told a
        # developer whose global rules had become unreadable that their new rule was
        # redundant - while the file holding the old ones is the thing that needs repair.
        health = store.global_diagnostics()
        if not health["ok"]:
            return 200, {"error": f"Not stored - the global rules file is unreadable "
                                  f"({health['error']}), and overwriting it would discard "
                                  "every global rule on this machine. Move it aside, "
                                  "then retry."}
        return 200, {"error": "Not stored - a matching global rule already exists."}
    return 200, {"message": "Global rule added."}


def _config() -> dict:
    profile = config.load_profile()
    ui = config.load_ui_settings()
    login = auth.auth_state(profile)
    return {
        "ui": {"autostart": ui.autostart, "port": ui.port,
               "idle_timeout_minutes": ui.idle_timeout_minutes},
        # token_set is a BOOLEAN on purpose: the teams bearer is reachable only through
        # `contexer login` / `logout`, never serialized to a browser.
        "profile": {"mode": profile.mode, "endpoint": profile.endpoint,
                    "redact_secrets": profile.redact_secrets,
                    "skip_confirm": profile.skip_confirm,
                    "token_set": bool(profile.token)},
        # `logged_in` is DERIVED from the state, never a second reading of the creds file: a
        # payload claiming logged_in next to state="refresh_failed" is a contradiction the
        # console would have to arbitrate. Still no token here - auth_state carries no secret.
        "login": {**login, "logged_in": login["state"] == "logged_in"},
        "version": daemon.current_version(),
        "store_dir": str(store.STORE_DIR),
        "config_path": str(config.CONFIG_PATH),
        "stores": len(console_api.list_stores()),
    }


def _write_config(body: object) -> tuple[int, object]:
    payload = _body(body, *config.SETTABLE_KEYS)
    if not payload:
        raise ApiError(400, "nothing to change")
    # No `path=`: write_settings takes it positional-only precisely so that splatting a
    # request body cannot redirect the write to an arbitrary file.
    config.write_settings(**payload)
    return 200, {"message": "Saved."}


def _team(slug: str) -> dict:
    repo_path = _repo(slug)
    snapshot = console_api.team_snapshot(repo_path)
    markers = _share_markers()
    shareable = []
    for row in store.get_shareable_all(repo_path):
        shared_at = markers.get(row["id"])
        shareable.append({
            "id": row["id"],
            "title": row["title"],
            "content": row["content"],
            "subtype": row["type"],
            "status": row["status"],
            "confidence": row["confidence"],
            "shared": shared_at is not None,
            "shared_at": shared_at,
            "redacted": row["redacted"],
        })
    return {"slug": slug, **snapshot, "shareable": shareable}


# ── validation ──────────────────────────────────────────────────────────────────

def _repo(slug: str) -> str:
    """Resolve a slug to a repo path, or refuse. A repo path is NEVER taken from a request.

    404 belongs to a slug that names no store file at all. A KNOWN slug whose file names no
    usable repo path is a 409 instead: it stays addressable (`GET /api/store/<slug>` renders
    it as "store unreadable"), but there is no file to read decisions, tombstones or team rows
    out of, and guessing a path from the slug is precisely what must not happen."""
    resolved = console_api.resolve_store(slug)
    if resolved is None:
        raise ApiError(404, "no such store")
    if not resolved["repo_path"]:
        raise ApiError(409, f"Store unreadable - {resolved['error']}")
    return resolved["repo_path"]


def _finish(repo_path: str, entry_id: str, ok: bool, message: str) -> tuple[int, object]:
    """Turn a store `(ok, message)` into a response, separating "gone" from "refused".

    The existence re-read happens only on the failure path, so the happy path still costs
    one store load."""
    if ok:
        return 200, {"message": message}
    if console_api.get_decision_detail(repo_path, entry_id) is None:
        raise ApiError(404, message)
    raise ApiError(400, message)


def _finish_restore(repo_path: str, entry_id: str, ok: bool,
                    message: str) -> tuple[int, object]:
    """`_finish`'s tombstone-side twin: separates "no such tombstone" from a refusal.

    A restore now fails for two very different reasons - the id names no tombstone (gone: 404)
    and the live store is at capacity, so restoring would evict an untombstoned decision
    (refused: 409). Mapping both to 404 told the developer their tombstone had vanished when it
    is still sitting there, restorable as soon as they delete something. Decided by re-reading
    the sidecar, not by matching the store's wording."""
    if ok:
        return 200, {"message": message}
    if _tombstone_exists(repo_path, entry_id):
        raise ApiError(409, message)
    raise ApiError(404, message)


def _tombstone_exists(repo_path: str, entry_id: str) -> bool:
    """Whether a tombstone the caller's id addresses is still in the sidecar. Accepts a full
    id or the 8-char prefix, the same id vocabulary the store resolves."""
    for row in console_api.list_tombstones(repo_path)["tombstones"]:
        if str(row.get("id") or "").startswith(entry_id):
            return True
    return False


def _body(body: object, *allowed: str) -> dict:
    """The request body as a dict, rejecting unknown keys.

    A silently-ignored typo'd field is worse than a 400: the console would report a save it
    never made."""
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise ApiError(400, "body must be a JSON object")
    unknown = sorted(set(body) - set(allowed))
    if unknown:
        raise ApiError(400, f"unknown field(s): {', '.join(unknown)}")
    return body


def _text(payload: dict, key: str, cap: int, *, required: bool = False) -> str | None:
    value = payload.get(key)
    if value is None:
        if required:
            raise ApiError(400, f"{key} is required")
        return None
    if not isinstance(value, str):
        raise ApiError(400, f"{key} must be a string")
    if len(value) > cap:
        raise ApiError(400, f"{key} is longer than {cap} characters")
    return value


def _str_param(query: dict, key: str) -> str:
    values = query.get(key) or [""]
    return values[0] if isinstance(values[0], str) else ""


def _list_param(query: dict, key: str, max_items: int, max_len: int) -> list[str]:
    """Every value for `key`, accepting BOTH a repeated param (`file=a&file=b`) and a single
    comma-separated one (`file=a,b`) - `parse_qs` already hands back one list entry per
    repeated occurrence, so splitting each entry on "," and flattening covers both
    conventions without forcing a caller to pick one. Blank pieces (a stray leading/trailing
    comma) are dropped. Canonicalization, traversal-escaping and repo-relativity are NOT this
    function's job - `guard_engine.decisions_for_files` (via `_guard_relpath`) already does
    that safely and this stays a thin, path-agnostic string splitter."""
    raw = query.get(key) or []
    out: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            continue
        for piece in value.split(","):
            piece = piece.strip()
            if not piece:
                continue
            if len(piece) > max_len:
                raise ApiError(400, f"{key} value is longer than {max_len} characters")
            out.append(piece)
    if len(out) > max_items:
        raise ApiError(400, f"at most {max_items} {key} values")
    return out


def _int_param(query: dict, key: str, maximum: int) -> int:
    raw = _str_param(query, key)
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError as exc:
        raise ApiError(400, f"{key} must be an integer") from exc
    if value < 0 or value > maximum:
        raise ApiError(400, f"{key} must be between 0 and {maximum}")
    return value


def _share_markers() -> dict:
    """decision_id -> shared-at for the configured endpoint. Cosmetic, so it degrades to
    "nothing shared" rather than failing the view it decorates."""
    try:
        return share.shared_map(config.load_profile().endpoint)
    except Exception:
        return {}
