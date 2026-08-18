"""Zero-paste OAuth for Contexer Teams (C-auth).

`contexer login` runs a browser OAuth 2.1 Authorization Code + PKCE flow against the Teams
Authorization Server (public DCR client, loopback redirect) and stores tokens in
~/.contexer/.team_auth.json (chmod 600). `resolve_token(profile)` is the seam RemoteStore
consumes: a stored access token (refreshed when expired), else the static config.toml token,
else None. Authenticates the contexer PYTHON process itself (distinct from Claude Code's own
MCP client). Stdlib only - no extra dependencies.
"""
from __future__ import annotations

import base64
import collections
import hashlib
import html
import json
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from contexer import config, store
from contexer.config import Profile, default_endpoint

# Endpoint default for the auth flow, kept next to the code that uses it so the login path
# does not have to reach into config.py for a value it needs on every call. Points at the
# dev stack, which is where logins are exercised most.
AUTH_DEFAULT_ENDPOINT = "https://mcp.dev.contexer.ai/mcp"

# Refresh a little before the token actually expires, to avoid a race at the boundary.
_EXPIRY_SKEW = 60
_HTTP_TIMEOUT = 30

# Creds-file key recording that the LAST refresh grant was rejected. Non-secret, and its
# ABSENCE means "unknown" so a creds file written before this existed still reads correctly.
_REFRESH_FAILED_AT = "refresh_failed_at"


def _creds_path():
    return store.STORE_DIR / ".team_auth.json"


def _load_creds() -> dict | None:
    path = _creds_path()
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            return None
        if isinstance(data, dict):
            return data
    return None


def _save_creds(creds: dict) -> None:
    store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    # Atomic write (unique temp + os.replace); mkstemp yields 0o600, so the creds file
    # is never torn or world-readable even mid-write — critical when a refresher process
    # and the foreground process persist rotated tokens concurrently.
    store._atomic_write(_creds_path(), json.dumps(creds, indent=2))


def _pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge) for PKCE S256."""
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _issuer_from_endpoint(endpoint: str) -> str:
    """The AS issuer (scheme://host[:port]) from an MCP endpoint URL — drops the path."""
    parts = urllib.parse.urlsplit(endpoint)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))


def _validate_endpoint(endpoint: str) -> str:
    """Reject anything that isn't a plain http(s) URL before it reaches OAuth discovery
    or gets persisted to config.toml."""
    parts = urllib.parse.urlsplit(endpoint)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError(
            f"invalid Teams endpoint {endpoint!r}: expected an http(s) URL "
            f"like {config.DEFAULT_ENDPOINT_PROD}"
        )
    return endpoint


def _request(url: str, *, data: dict | None = None, form: bool = False) -> dict:
    """GET (data=None) or POST a JSON/form body; return the parsed JSON response."""
    headers = {"Accept": "application/json"}
    body = None
    if data is not None:
        if form:
            body = urllib.parse.urlencode(data).encode()
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=headers,
                                 method="POST" if data is not None else "GET")
    with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT) as resp:
        return json.loads(resp.read().decode())


def _discover(issuer: str) -> dict:
    return _request(issuer.rstrip("/") + "/.well-known/oauth-authorization-server")


def _register(registration_endpoint: str, redirect_uri: str) -> str:
    """Dynamic Client Registration of a public PKCE client; returns the client_id."""
    resp = _request(registration_endpoint, data={
        "client_name": "contexer-cli",
        "redirect_uris": [redirect_uri],
        "token_endpoint_auth_method": "none",  # public client — PKCE, no secret
        "grant_types": ["authorization_code", "refresh_token"],
        "response_types": ["code"],
    })
    return resp["client_id"]


def _exchange_code(token_endpoint: str, client_id: str, code: str,
                   verifier: str, redirect_uri: str) -> dict:
    return _request(token_endpoint, form=True, data={
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": verifier,
    })


def _refresh(token_endpoint: str, client_id: str, refresh_token: str) -> dict:
    return _request(token_endpoint, form=True, data={
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    })


def _refresh_rejected(exc: BaseException) -> bool:
    """True only when the AS actively REJECTED the refresh grant.

    Proof of rejection is an answer from the token endpoint refusing the grant — an HTTP 4xx
    (`invalid_grant` and friends, RFC 6749 §5.2). Everything else — DNS failure, timeout,
    connection refused, a 5xx, a body that isn't JSON — means the endpoint was never reached
    or never decided, and leaves the refresh token's validity UNKNOWN. Persisting "unknown" as
    "rejected" is what turns a VPN outage into days of "log in again" on every surface, so the
    two are separated here exactly as `remote._classify` separates RemoteAuthError from
    RemoteUnavailableError. Note HTTPError SUBCLASSES URLError: an `except URLError` reading
    would swallow the one case that does prove rejection."""
    return isinstance(exc, urllib.error.HTTPError) and 400 <= exc.code < 500


def _free_port() -> int:
    """An OS-assigned free port on the loopback interface (for the redirect listener)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _creds_match(creds: dict | None, profile: Profile) -> bool:
    """True when stored creds belong to this profile's endpoint issuer."""
    return bool(
        creds and profile.endpoint
        and creds.get("issuer") == _issuer_from_endpoint(profile.endpoint)
    )


def _locked_refresh(profile: Profile) -> str | None:
    """Refresh the access token under a cross-process lock, double-checked.

    The refresh token is SINGLE-USE and rotates: two processes refreshing with the same
    token trips the server's compromise detection and revokes the whole token family. So
    the read-check-refresh-write is serialized with an advisory lock, and — after acquiring
    it — we re-read the creds: if another process already refreshed while we waited, we use
    that fresh access token instead of spending our (now-stale) refresh token again.

    Returns the usable access token, or ``profile.token`` (static/None) when refresh is
    impossible or fails. Never raises."""
    if store.fcntl is None:
        # No POSIX advisory lock available (non-POSIX runtime): store._store_lock would yield
        # WITHOUT serializing, so two processes could double-spend the single-use refresh token
        # and trip the server's replay detection → token-family revocation. Refreshing a
        # single-use secret UNSERIALIZED is worse than not refreshing: degrade to the static/None
        # token (the caller then surfaces the re-login warning) rather than risk the credentials.
        return profile.token
    with store._store_lock(".team_auth"):
        creds = _load_creds()
        if not _creds_match(creds, profile):
            return profile.token
        # Double-check: a concurrent process may have refreshed while we held for the lock.
        if creds.get("expires_at", 0) > time.time() + _EXPIRY_SKEW:
            return creds.get("access_token")
        refresh_token = creds.get("refresh_token")
        if not refresh_token:
            return profile.token  # expired, nothing to refresh with — skip the doomed network call
        try:
            tok = _refresh(creds["token_endpoint"], creds["client_id"], refresh_token)
        except Exception as exc:
            # Record the rejection where the refresh actually happens: the refresh token is
            # single-use, so a later reader (auth_state) cannot re-attempt one just to find out
            # why the session is dead. Without this marker an expired session and a rejected
            # renewal are indistinguishable, and the user is told "authentication failed" for
            # days when the truth is "log in again". Only a REJECTION earns the marker — it is
            # persistent, so an offline machine that recorded one would keep demanding a new
            # login long after the network came back.
            if _refresh_rejected(exc):
                creds[_REFRESH_FAILED_AT] = time.time()
                _save_creds(creds)
            return profile.token  # refresh failed — degrade to the static token (or None)
        creds["access_token"] = tok.get("access_token", creds.get("access_token"))
        if tok.get("refresh_token"):
            creds["refresh_token"] = tok["refresh_token"]  # persist the rotated (single-use) token
        creds["expires_at"] = time.time() + tok.get("expires_in", 3600)
        creds.pop(_REFRESH_FAILED_AT, None)
        _save_creds(creds)
        return creds["access_token"]


def resolve_token(profile: Profile) -> str | None:
    """The bearer RemoteStore should use for this profile, or None.

    Prefers a stored OAuth token for this endpoint's issuer (refreshing when expired);
    falls back to the static config.toml token, then None. Never raises."""
    creds = _load_creds()
    if _creds_match(creds, profile):
        if creds.get("expires_at", 0) > time.time() + _EXPIRY_SKEW:
            return creds.get("access_token")  # happy path: valid token, no lock, no network
        return _locked_refresh(profile)
    return profile.token


def refresh_now(profile: Profile) -> str | None:
    """Force a refresh (reactive path: called after a 401), single-flight and double-checked.

    Shares `_locked_refresh`, so a concurrent process that already refreshed short-circuits
    us without a second network call. Returns the (possibly newly-refreshed) access token, or
    the static/None fallback. Never raises — the caller decides whether the token changed."""
    return _locked_refresh(profile)


def _state(state: str, message: str, *, issuer: str | None = None,
           expires_at: str | None = None, scope: str | None = None) -> dict:
    """One shape for every auth_state answer, so no branch can omit a key a caller reads."""
    return {"state": state, "issuer": issuer, "expires_at": expires_at, "scope": scope,
            "message": message}


def _iso(epoch: object) -> str | None:
    return (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(epoch))
            if isinstance(epoch, (int, float)) else None)


def auth_state(profile: Profile) -> dict:
    """Which credential this profile would use, and whether it can still work.

    A READ, in the strict sense: it never refreshes. Attempting a refresh to answer the
    question would SPEND a single-use token as the side effect of being asked, so renewing is
    left to `refresh_now` — but the ANSWER must still describe what will happen, not just what
    the clock says. Access tokens are minted with `expires_in` 3600 and `resolve_token` renews
    them transparently, so a session past expiry that still holds a refresh token and carries
    no recorded rejection is `renewable`: nothing is wrong with it and the user has nothing to
    do. `expired` is kept for the session that genuinely cannot renew itself (no refresh
    token), and `refresh_failed` for the one whose rotated refresh token the AS rejected —
    which is the point of the marker `_locked_refresh` writes.

    Never raises and never returns a token, a refresh token, or any other secret: the result
    is serialized straight to a browser."""
    try:
        creds = _load_creds()
        if not _creds_match(creds, profile):
            if profile.token:
                return _state("static_only", "Using the static token from config.toml — no "
                                             "Contexer Teams session is stored here.")
            return _state("none", "Not signed in to Contexer Teams — log in to sync with "
                                  "your team.")
        expires_at = creds.get("expires_at")
        detail = {"issuer": creds.get("issuer"), "expires_at": _iso(expires_at),
                  "scope": creds.get("scope") or None}
        if isinstance(expires_at, (int, float)) and expires_at > time.time() + _EXPIRY_SKEW:
            return _state("logged_in", "Signed in to Contexer Teams.", **detail)
        if creds.get(_REFRESH_FAILED_AT):
            return _state("refresh_failed", "Your Contexer Teams session expired and could "
                                            "not be renewed — log in again.", **detail)
        if creds.get("refresh_token"):
            return _state("renewable", "Signed in to Contexer Teams — the access token is past "
                                       "its expiry and renews itself on the next sync.",
                          **detail)
        return _state("expired", "Your Contexer Teams session has expired — log in again.",
                      **detail)
    except Exception:
        return _state("none", "Contexer Teams sign-in state is unreadable — log in again.")


# ── tracked login job (the console's "Log in" button) ────────────────────────────
# `login` below is minutes-long, opens a browser and binds its own loopback port, so a caller
# that must answer an HTTP request in seconds cannot run it in-process. These three functions
# run it as ONE tracked subprocess instead — killable, capped, and reusing the CLI path verbatim.

# Generous, but a cap: an abandoned flow must not leave a python process and a loopback
# listener alive for the rest of the machine's uptime.
LOGIN_TIMEOUT = 300.0
_MESSAGE_LINES = 2
_MESSAGE_LINE_LIMIT = 200
# The drain keeps only the TAIL, which is all `_failure_message` reads (`lines[-2:]`). An
# unbounded list held every byte a child printed for up to LOGIN_TIMEOUT, in a daemon that
# stays up for days — a chatty or looping child was the whole budget for it.
_MAX_OUTPUT_LINES = 200
# How long the waiter gives the output drain after the child is gone. The browser the child
# launched can inherit its stdout and hold the pipe open, and the job has to settle anyway.
_OUTPUT_DRAIN = 5.0

# Query values a login prints (the authorize URL it echoes for a browser that won't open, an
# error redirect it reflects). The failure message is RENDERED IN THE CONSOLE, so this is not
# defensive: an unscrubbed tail can put an OAuth `code=` on screen, and into any screenshot of
# it. A superset of what `ui.server._scrub` redacts — sharing that function would point auth
# at the ui layer, which imports auth.
_QUERY_SECRET = re.compile(
    r"(?i)\b(code|code_verifier|code_challenge|state|client_id|token|access_token"
    r"|refresh_token|p|csrf|ctx_ui)=[^\s&;'\"]*")

_login_lock = threading.Lock()
_login_job: dict | None = None


class LoginJobBusy(RuntimeError):
    """A login subprocess is already running: two concurrent browser flows would race to
    write the creds file, and only one of them could own the rotating refresh token.

    Carries the id of the job in flight (`job_id`), so a caller told "busy" — a second console
    tab, or a console while a terminal login runs — can follow THAT job's real outcome instead
    of inferring success from the session going live and never seeing its failure."""

    def __init__(self, message: str, job_id: str):
        super().__init__(message)
        self.job_id = job_id


def start_login_job() -> str:
    """Start `contexer login` as a tracked subprocess; returns the job id.

    Takes NO endpoint, deliberately: a caller-supplied one would aim the OAuth flow at an
    attacker's IdP and persist the token it handed back, so the endpoint is resolved here from
    config. Single-flight — a second call while one is pending raises LoginJobBusy."""
    global _login_job
    endpoint = _configured_endpoint()
    with _login_lock:
        if _login_job is not None and _login_job["state"] == "pending":
            raise LoginJobBusy("a login is already running", _login_job["id"])
        job = {"id": secrets.token_urlsafe(8), "state": "pending",
               "message": "Waiting for the browser sign-in to finish.",
               "auth_url": None, "proc": _spawn_login(endpoint)}
        _login_job = job
    threading.Thread(target=_await_login, args=(job,), daemon=True).start()
    return job["id"]


def login_job_status(job_id: str) -> dict | None:
    """`{state: pending|ok|failed, message, auth_url}` for a tracked login, or None for an
    unknown id.

    `auth_url` is the consent page the child opened a browser to, None until it prints one.
    It is what makes a login possible where no browser can open — an SSH session, a container,
    WSL — because `webbrowser.open` silently no-ops there and the printed fallback used to go
    nowhere but into a captured pipe. Only meaningful while the job is `pending`: the loopback
    listener that completes the flow dies with the child.

    Only the most recent job is tracked (there is only ever one), so an id from a previous
    one reads as unknown rather than as somebody else's outcome."""
    with _login_lock:
        job = _login_job
        if job is None or job["id"] != job_id:
            return None
        return {"state": job["state"], "message": job["message"],
                "auth_url": job["auth_url"]}


def stop_login_job(reason: str = "Login was cancelled — the console stopped.") -> bool:
    """Kill a pending login subprocess. True when one was killed.

    Called wherever an orphaned browser flow could still write credentials nobody is waiting
    for: the console stopping, and a logout (a flow started minutes earlier completes after
    the unlink and silently restores the session the user just ended). `reason` becomes the
    job's final message because a tab attached to that job is told it verbatim, and "the
    console stopped" is a lie in the logout case."""
    global _login_job
    with _login_lock:
        job = _login_job
        if job is None or job["state"] != "pending":
            return False
        # Resolve it under the lock BEFORE the kill, so the waiter thread sees a settled job
        # and does not overwrite this outcome with its own "the process died" reading.
        job.update(state="failed", message=reason)
    job["proc"].kill()
    return True


def _configured_endpoint() -> str:
    """The endpoint a console-started login should use: the configured profile's, else the
    default. Fail-soft — a malformed config.toml must not be what stops you logging in."""
    try:
        return config.load_profile().endpoint or AUTH_DEFAULT_ENDPOINT
    except Exception:
        return AUTH_DEFAULT_ENDPOINT


def _spawn_login(endpoint: str):
    """Spawn the CLI login with its output captured. The one seam tests replace — nothing in
    a test may open a browser.

    `-u` is load-bearing: the child's stdout is a pipe, so Python would block-buffer it and
    the authorize URL printed at the START of the flow would not reach us until the END of it
    — minutes after the only moment it is useful."""
    return subprocess.Popen(
        [sys.executable, "-u", "-m", "contexer", "login", "--endpoint", endpoint],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        start_new_session=True, text=True, errors="replace")


def _await_login(job: dict) -> None:
    """Wait out one login subprocess and record its outcome. Own thread: the wait is minutes
    long by design and the daemon has to keep serving."""
    proc = job["proc"]
    lines: collections.deque[str] = collections.deque(maxlen=_MAX_OUTPUT_LINES)
    reader = threading.Thread(target=_read_login_output, args=(job, lines), daemon=True)
    reader.start()
    timed_out = False
    try:
        proc.wait(timeout=LOGIN_TIMEOUT)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()  # reap it: a killed child left unwaited is a zombie for the daemon's life
    reader.join(_OUTPUT_DRAIN)
    with _login_lock:
        if job["state"] != "pending":
            return  # stop_login_job() already resolved it
        if timed_out:
            job.update(state="failed",
                       message=f"Login timed out after {int(LOGIN_TIMEOUT // 60)} minutes "
                               "— nothing was saved.")
        elif proc.returncode == 0:
            job.update(state="ok", message="Signed in to Contexer Teams.")
        else:
            job.update(state="failed", message=_failure_message("".join(lines)))


def _read_login_output(job: dict, lines: "collections.deque[str]") -> None:
    """Drain the child's stdout as it arrives, publishing the authorize URL the moment it is
    printed.

    Streaming, rather than reading the output at exit: on a machine where no browser can open
    that URL is the ONLY way to finish the login, so it is needed WHILE the child sits waiting
    for the callback — a post-mortem of a timed-out flow is exactly too late."""
    proc = job["proc"]
    try:
        for line in proc.stdout:
            lines.append(line)
            url = _authorize_url(line)
            if url:
                with _login_lock:
                    if job["state"] == "pending":
                        job["auth_url"] = url
    finally:
        proc.stdout.close()


def _authorize_url(line: str) -> str | None:
    """The authorize URL in one line of the child's output, or None.

    This is published verbatim — an authorize URL is the address of a consent page, not a
    credential, and redacting its parameters would leave a link that cannot complete a flow.
    So the match is narrow on purpose: the URL must carry the authorize REQUEST's own
    parameters, and one holding a `code=` is refused outright rather than shown, because an
    authorization CODE is a credential and a callback URL echoed into the output must never
    come back out as a login link."""
    match = re.search(r"https?://\S+", line)
    if not match:
        return None
    params = urllib.parse.parse_qs(urllib.parse.urlsplit(match.group(0)).query)
    if "code" in params:
        return None
    if params.get("response_type") == ["code"] and "code_challenge" in params:
        return match.group(0)
    return None


def _failure_message(output: str) -> str:
    """The tail of the child's output as one scrubbed line."""
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    tail = " ".join(line[:_MESSAGE_LINE_LIMIT] for line in lines[-_MESSAGE_LINES:])
    scrubbed = _QUERY_SECRET.sub(lambda match: f"{match.group(1)}=REDACTED", tail)
    return f"Login failed — {scrubbed}" if scrubbed else "Login failed."


# Static CSS for the loopback result tab (kept out of the f-string so `{}` stays literal).
_PAGE_CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
       font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", sans-serif;
       background: #f8fafc; color: #0f172a; }
.card { background: #fff; border: 1px solid #e2e8f0; border-radius: 16px; padding: 48px 56px;
        text-align: center; box-shadow: 0 10px 30px rgba(2, 6, 23, .08); max-width: 26rem; margin: 16px; }
.brand { display: flex; align-items: center; justify-content: center; gap: 7px;
         letter-spacing: .08em; text-transform: uppercase; font-size: 12px; color: #64748b;
         margin: 0 0 20px; }
.brand svg { width: 20px; height: 20px; color: #15170d; }
.badge { width: 64px; height: 64px; border-radius: 50%; display: flex; align-items: center;
         justify-content: center; margin: 0 auto 20px; font-size: 30px; color: #fff; }
h1 { font-size: 22px; margin: 0 0 8px; }
.detail { color: #475569; font-size: 15px; line-height: 1.5; margin: 0; }
.hint { margin: 24px 0 0; font-size: 13px; color: #64748b; }
code { background: #f1f5f9; padding: 2px 6px; border-radius: 6px; font-size: 12.5px; }
@media (prefers-color-scheme: dark) {
  body { background: #0b1220; color: #e2e8f0; }
  .card { background: #111a2e; border-color: #1e293b; box-shadow: none; }
  .detail { color: #94a3b8; }
  code { background: #1e293b; }
  .brand svg { color: #f2f1e6; }
}
"""

# The Contexer mark (apps/web/public/brand/contexer-mark.svg in contexer-teams), inlined so the
# page stays self-contained. stroke=currentColor: .brand css picks the ink/light variant per
# color scheme; the lime accent (#CDEB36) is shared by both brand variants.
_BRAND_MARK = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32" aria-hidden="true">'
    '<path d="M23 8 H13 A6 6 0 0 0 7 14 V18 A6 6 0 0 0 13 24 H23" fill="none"'
    ' stroke="currentColor" stroke-width="3.2" stroke-linecap="round"'
    ' stroke-linejoin="round"/>'
    '<rect x="20.5" y="13.7" width="4.6" height="4.6" rx="1" fill="#CDEB36"/></svg>'
)


def _result_page(ok: bool, title: str, detail: str) -> bytes:
    """Self-contained HTML for the loopback result tab (inline CSS, no external assets).

    `title` and `detail` are HTML-escaped here — `detail` can carry text reflected from the
    callback query string (`error_description`), so escaping is a security requirement, not
    cosmetics. The data-URI favicon suppresses the browser's follow-up /favicon.ico request,
    which would otherwise hit a already-closed (or still-listening) loopback server."""
    icon, color = ("&#10003;", "#16a34a") if ok else ("&#10007;", "#dc2626")
    hint = ("Next: <code>contexer pull</code> &middot; <code>contexer share</code>" if ok
            else "Return to your terminal and run <code>contexer login</code> to try again.")
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        '<link rel="icon" href="data:,"><title>Contexer</title>'
        f"<style>{_PAGE_CSS}</style></head><body>"
        f'<div class="card"><p class="brand">{_BRAND_MARK}<span>Contexer Teams</span></p>'
        f'<div class="badge" style="background:{color}">{icon}</div>'
        f"<h1>{html.escape(title)}</h1>"
        f'<p class="detail">{html.escape(detail)}</p>'
        f'<p class="hint">{hint}</p>'
        "</div></body></html>"
    ).encode()


def _callback_outcome(qs: dict, expected_state: str) -> tuple[str | None, str | None, bytes]:
    """Decide the loopback callback result: (code, error, page).

    Pure (unit-testable): `qs` is the parse_qs dict of the /callback query; `page` is what
    the browser tab shows and always matches the terminal outcome. Three shapes:
    - (code, None, page) — success;
    - (None, error, page) — legitimate flow-terminating failure (denial, missing code);
    - (None, None, page) — state mismatch: NOT our redirect. The AS echoes our `state` on
      both success and error redirects (RFC 6749), so a request without it could come from
      any local process poking the loopback port — the caller must keep listening rather
      than let a stray or malicious request abort (or spoof the reason for) the login."""
    if (qs.get("state") or [None])[0] != expected_state:
        return None, None, _result_page(
            False, "Login failed",
            "Security check failed (state mismatch). This tab is not from the current "
            "login attempt — your terminal is still waiting; use the newest login link.")
    error = (qs.get("error") or [None])[0]
    if error:
        desc = (qs.get("error_description") or [None])[0]
        reason = f"{error}: {desc}" if desc else error
        return None, f"authorization failed — {reason}", _result_page(
            False, "Login failed", f"The authorization server reported: {reason}.")
    code = (qs.get("code") or [None])[0]
    if not code:
        return None, "No authorization code received.", _result_page(
            False, "Login failed", "No authorization code was received from the server.")
    return code, None, _result_page(
        True, "Login complete",
        "You're signed in to Contexer Teams. Close this tab and return to your terminal.")


def _await_code(auth_url: str, port: int, expected_state: str) -> str:  # pragma: no cover - interactive (browser + blocking loopback server)
    """Open the browser to the authorize URL and block until the loopback receives the code."""
    import http.server
    import webbrowser

    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/callback":
                # Stray request (favicon, prefetch, port scan) — don't let it consume the
                # one meaningful request; the outer loop keeps serving until /callback.
                self.send_response(404)
                self.end_headers()
                return
            qs = urllib.parse.parse_qs(parsed.query)
            code, error, page = _callback_outcome(qs, expected_state)
            if code or error:
                # Record the outcome before writing the body: a browser that disconnects
                # mid-write must not leave the loop below serving forever.
                result["code"], result["error"] = code, error
            else:
                # State mismatch: not our redirect — answer it but keep listening.
                print("contexer: ignoring loopback callback with unexpected state",
                      file=sys.stderr)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(page)

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    print(f"Opening your browser to sign in. If it doesn't open, visit:\n  {auth_url}")
    webbrowser.open(auth_url)
    try:
        while not result:
            server.handle_request()  # serve until the OAuth redirect hits /callback
    finally:
        server.server_close()
    if result.get("error"):
        raise RuntimeError(result["error"])
    return result["code"]


def login(endpoint: str | None = None) -> None:
    """Run the interactive browser OAuth flow, persist tokens, and self-configure config.toml.

    `endpoint` defaults to default_endpoint() (prod, or localhost under CONTEXER_ENV=local).
    On success this writes mode='team' + endpoint to config.toml, so the user never hand-edits
    it — team onboarding is just `contexer install` then `contexer login`."""
    endpoint = _validate_endpoint(endpoint or default_endpoint())
    issuer = _issuer_from_endpoint(endpoint)
    meta = _discover(issuer)
    port = _free_port()
    redirect_uri = f"http://127.0.0.1:{port}/callback"
    client_id = _register(meta["registration_endpoint"], redirect_uri)
    verifier, challenge = _pkce()
    state = secrets.token_urlsafe(24)
    auth_url = meta["authorization_endpoint"] + "?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "state": state,
        "scope": "",
    })
    code = _await_code(auth_url, port, state)
    tok = _exchange_code(meta["token_endpoint"], client_id, code, verifier, redirect_uri)
    if not tok.get("access_token"):
        raise RuntimeError("token endpoint returned no access_token — login failed.")
    _save_creds({
        "issuer": issuer,
        "client_id": client_id,
        "token_endpoint": meta["token_endpoint"],
        "access_token": tok.get("access_token"),
        "refresh_token": tok.get("refresh_token"),
        "expires_at": time.time() + tok.get("expires_in", 3600),
        "scope": tok.get("scope", ""),
    })
    # Creds first, config second, deliberately: the authorization code behind these tokens is
    # single-use, so a failure while writing config.toml must not throw away a session that
    # already exists — config.toml is hand-fixable, a spent code is not. That ordering is only
    # safe because write_team_profile can no longer fail on the CONTENT of the old file; it
    # used to abort here on an invalid `[ui]` value, after the creds were saved, leaving team
    # sync off with nothing on screen pointing at why.
    config.write_team_profile(endpoint)  # self-configure: user never hand-edits config.toml
    print("Logged in to Contexer Teams - team sync enabled. `contexer pull` / `contexer share` now use your account.")


def logout() -> bool:
    """Delete stored credentials. Returns True if any were present."""
    path = _creds_path()
    if path.exists():
        path.unlink()
        return True
    return False
