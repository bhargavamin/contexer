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
import hashlib
import json
import secrets
import socket
import sys
import time
import urllib.parse
import urllib.request

from contexer import config, store
from contexer.config import Profile, default_endpoint

# Refresh a little before the token actually expires, to avoid a race at the boundary.
_EXPIRY_SKEW = 60
_HTTP_TIMEOUT = 30


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
        except Exception:
            return profile.token  # refresh failed — degrade to the static token (or None)
        creds["access_token"] = tok.get("access_token", creds.get("access_token"))
        if tok.get("refresh_token"):
            creds["refresh_token"] = tok["refresh_token"]  # persist the rotated (single-use) token
        creds["expires_at"] = time.time() + tok.get("expires_in", 3600)
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


def _await_code(auth_url: str, port: int, expected_state: str) -> str:  # pragma: no cover - interactive (browser + blocking loopback server)
    """Open the browser to the authorize URL and block until the loopback receives the code."""
    import http.server
    import webbrowser

    result: dict = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            result["code"] = (qs.get("code") or [None])[0]
            result["state"] = (qs.get("state") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Contexer: login complete. You can close this tab.</h1>")

        def log_message(self, *a):
            pass

    server = http.server.HTTPServer(("127.0.0.1", port), Handler)
    print(f"Opening your browser to sign in. If it doesn't open, visit:\n  {auth_url}")
    webbrowser.open(auth_url)
    server.handle_request()  # serve exactly one request (the OAuth redirect)
    server.server_close()
    if result.get("state") != expected_state:
        raise RuntimeError("OAuth state mismatch — login aborted.")
    if not result.get("code"):
        raise RuntimeError("No authorization code received.")
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
    config.write_team_profile(endpoint)  # self-configure: user never hand-edits config.toml
    print("Logged in to Contexer Teams - team sync enabled. `contexer pull` / `contexer share` now use your account.")


def logout() -> bool:
    """Delete stored credentials. Returns True if any were present."""
    path = _creds_path()
    if path.exists():
        path.unlink()
        return True
    return False
