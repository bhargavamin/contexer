"""Tests for C-auth: contexer/auth.py (zero-paste OAuth) + RemoteStore token resolution.

Network (_request) and the interactive browser leg (_await_code) are faked/omitted; the
OAuth mechanics (PKCE, DCR, code/refresh exchange, token resolution) are fully covered.
"""
import base64
import hashlib
import io
import json
import subprocess
import threading
import time
import urllib.error
from pathlib import Path

import pytest

from contexer import auth, config, store

TEAM = config.Profile(mode="team", endpoint="http://localhost:8080/mcp", token=None)


@pytest.fixture
def creds_env(tmp_path, monkeypatch):
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    monkeypatch.setattr(config, "CONFIG_PATH", tmp_path / ".contexer" / "config.toml")  # login self-configures
    return tmp_path


# ── PKCE / helpers ────────────────────────────────────────────────────────────────

def test_pkce_challenge_is_s256_of_verifier():
    verifier, challenge = auth._pkce()
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    assert 43 <= len(verifier) <= 128
    assert "=" not in challenge


def test_issuer_from_endpoint_strips_path():
    assert auth._issuer_from_endpoint("http://localhost:8080/mcp") == "http://localhost:8080"
    assert auth._issuer_from_endpoint("https://mcp.contexer.ai/mcp") == "https://mcp.contexer.ai"
    assert auth._issuer_from_endpoint("https://x.example") == "https://x.example"


def test_free_port_returns_bindable_int():
    port = auth._free_port()
    assert isinstance(port, int) and 1024 <= port <= 65535


# ── loopback result page ─────────────────────────────────────────────────────────

def test_result_page_success_has_next_steps():
    page = auth._result_page(True, "Login complete", "You're signed in.").decode()
    assert "Login complete" in page
    assert "contexer pull" in page and "contexer share" in page


def test_result_page_error_suggests_retry():
    page = auth._result_page(False, "Login failed", "nope").decode()
    assert "contexer login" in page
    assert "contexer pull" not in page


def test_result_page_has_brand_mark():
    page = auth._result_page(True, "Login complete", "ok").decode()
    assert "<svg" in page and "Contexer Teams" in page


def test_result_page_escapes_html():
    page = auth._result_page(False, "Login failed", "<script>alert(1)</script>").decode()
    assert "<script>" not in page
    assert "&lt;script&gt;" in page


def test_callback_outcome_success():
    code, err, page = auth._callback_outcome({"code": ["abc"], "state": ["s1"]}, "s1")
    assert code == "abc" and err is None
    assert b"Login complete" in page


def test_callback_outcome_oauth_error():
    code, err, page = auth._callback_outcome(
        {"error": ["access_denied"], "error_description": ["user declined"], "state": ["s1"]}, "s1")
    assert code is None
    assert "access_denied" in err and "user declined" in err
    assert b"Login failed" in page and b"user declined" in page


def test_callback_outcome_error_description_escaped():
    _, _, page = auth._callback_outcome(
        {"error": ["access_denied"], "error_description": ["<img onerror=x>"],
         "state": ["s1"]}, "s1")
    assert b"<img" not in page
    assert b"&lt;img" in page


def test_callback_outcome_state_mismatch_keeps_listening():
    # Wrong state = not our redirect (stray, stale, or malicious). Neither code nor error:
    # the caller must keep waiting instead of letting the request abort the login.
    code, err, page = auth._callback_outcome({"code": ["abc"], "state": ["evil"]}, "s1")
    assert code is None and err is None
    assert b"state mismatch" in page


def test_callback_outcome_error_without_state_is_ignored():
    # An OAuth error redirect echoes our state (RFC 6749); one without it could come from
    # any local process poking the loopback port — it must not terminate the flow.
    code, err, page = auth._callback_outcome(
        {"error": ["access_denied"], "error_description": ["spoofed"]}, "s1")
    assert code is None and err is None
    assert b"state mismatch" in page


def test_callback_outcome_missing_code():
    code, err, page = auth._callback_outcome({"state": ["s1"]}, "s1")
    assert code is None
    assert "No authorization code" in err
    assert b"Login failed" in page


# ── creds storage ────────────────────────────────────────────────────────────────

def test_creds_roundtrip_and_0600(creds_env):
    assert auth._load_creds() is None
    auth._save_creds({"issuer": "http://localhost:8080", "access_token": "a"})
    assert auth._load_creds()["access_token"] == "a"
    assert auth._creds_path().stat().st_mode & 0o777 == 0o600


def test_load_creds_corrupt_returns_none(creds_env):
    path = auth._creds_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json")
    assert auth._load_creds() is None


# ── HTTP + OAuth requests ─────────────────────────────────────────────────────────

def test_request_get_and_post_form(monkeypatch):
    import urllib.request
    captured = {}

    class FakeResp:
        def __init__(self, data):
            self._d = data

        def read(self):
            return self._d

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def fake_urlopen(req, timeout=None):
        captured["method"] = req.method
        captured["body"] = req.data
        return FakeResp(b'{"ok": true}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    assert auth._request("http://x/meta") == {"ok": True}
    assert captured["method"] == "GET"
    assert auth._request("http://x/token", data={"grant_type": "x"}, form=True)["ok"] is True
    assert captured["method"] == "POST"
    assert b"grant_type=x" in captured["body"]
    # JSON POST (DCR /register path)
    assert auth._request("http://x/register", data={"a": 1})["ok"] is True
    assert captured["body"] == b'{"a": 1}'


def test_discover_register_exchange(monkeypatch):
    calls = []

    def fake_request(url, *, data=None, form=False):
        calls.append((url, data, form))
        if "well-known" in url:
            return {"registration_endpoint": "http://x/register",
                    "authorization_endpoint": "http://x/authorize",
                    "token_endpoint": "http://x/token"}
        if url.endswith("/register"):
            return {"client_id": "cid-1"}
        return {"access_token": "at", "refresh_token": "rt", "expires_in": 3600}

    monkeypatch.setattr(auth, "_request", fake_request)
    meta = auth._discover("http://x")
    assert meta["token_endpoint"] == "http://x/token"

    assert auth._register("http://x/register", "http://127.0.0.1:5000/callback") == "cid-1"
    reg = next(c for c in calls if c[0].endswith("/register"))
    assert reg[1]["token_endpoint_auth_method"] == "none"
    assert reg[1]["redirect_uris"] == ["http://127.0.0.1:5000/callback"]

    tok = auth._exchange_code("http://x/token", "cid-1", "code123", "verifier", "http://127.0.0.1:5000/callback")
    assert tok["access_token"] == "at"
    ex = next(c for c in calls if c[0].endswith("/token"))
    assert ex[1]["grant_type"] == "authorization_code"
    assert ex[1]["code_verifier"] == "verifier"
    assert ex[2] is True  # form-encoded


def test_refresh_uses_refresh_grant(monkeypatch):
    calls = []
    monkeypatch.setattr(auth, "_request",
                        lambda url, *, data=None, form=False: calls.append((url, data, form)) or {"access_token": "n"})
    auth._refresh("http://x/token", "cid", "rt")
    assert calls[0][1]["grant_type"] == "refresh_token"
    assert calls[0][1]["refresh_token"] == "rt"
    assert calls[0][2] is True


# ── resolve_token ────────────────────────────────────────────────────────────────

def test_resolve_token_no_creds_falls_back_to_static(creds_env):
    prof = config.Profile(mode="team", endpoint="http://localhost:8080/mcp", token="static")
    assert auth.resolve_token(prof) == "static"


def test_resolve_token_no_creds_no_static_is_none(creds_env):
    assert auth.resolve_token(TEAM) is None


def test_resolve_token_valid_creds(creds_env):
    auth._save_creds({"issuer": "http://localhost:8080", "client_id": "c",
                      "token_endpoint": "http://localhost:8080/token", "access_token": "live",
                      "refresh_token": "r", "expires_at": time.time() + 3600})
    assert auth.resolve_token(TEAM) == "live"


def test_resolve_token_issuer_mismatch_falls_back(creds_env):
    auth._save_creds({"issuer": "http://other", "access_token": "live", "expires_at": time.time() + 3600})
    prof = config.Profile(mode="team", endpoint="http://localhost:8080/mcp", token="static")
    assert auth.resolve_token(prof) == "static"


def test_resolve_token_expired_refreshes(creds_env, monkeypatch):
    auth._save_creds({"issuer": "http://localhost:8080", "client_id": "c",
                      "token_endpoint": "http://localhost:8080/token", "access_token": "old",
                      "refresh_token": "r", "expires_at": time.time() - 10})
    monkeypatch.setattr(auth, "_refresh",
                        lambda te, cid, rt: {"access_token": "new", "refresh_token": "r2", "expires_in": 3600})
    assert auth.resolve_token(TEAM) == "new"
    creds = auth._load_creds()
    assert creds["access_token"] == "new" and creds["refresh_token"] == "r2"
    assert creds["expires_at"] > time.time()


def test_resolve_token_expired_without_refresh_token_skips_network(creds_env, monkeypatch):
    auth._save_creds({"issuer": "http://localhost:8080", "client_id": "c",
                      "token_endpoint": "http://localhost:8080/token", "access_token": "old",
                      "refresh_token": None, "expires_at": time.time() - 10})
    monkeypatch.setattr(auth, "_refresh", lambda *a: pytest.fail("must not attempt a refresh"))
    prof = config.Profile(mode="team", endpoint="http://localhost:8080/mcp", token="static")
    assert auth.resolve_token(prof) == "static"


def test_resolve_token_refresh_failure_falls_back(creds_env, monkeypatch):
    auth._save_creds({"issuer": "http://localhost:8080", "client_id": "c",
                      "token_endpoint": "http://localhost:8080/token", "access_token": "old",
                      "refresh_token": "r", "expires_at": time.time() - 10})

    def boom(*a):
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(auth, "_refresh", boom)
    prof = config.Profile(mode="team", endpoint="http://localhost:8080/mcp", token="static")
    assert auth.resolve_token(prof) == "static"


# ── single-flight refresh (the ~1h family-revocation regression) ────────────────────

def test_refresh_single_flight_only_one_network_refresh(creds_env, monkeypatch):
    """Two concurrent refreshers must NOT both spend the single-use refresh token.

    The advisory lock serializes them; the second re-reads the freshly-rotated creds under
    the lock and short-circuits — so ``_refresh`` (the network POST) fires exactly once."""
    import threading

    auth._save_creds({"issuer": "http://localhost:8080", "client_id": "c",
                      "token_endpoint": "http://localhost:8080/token", "access_token": "old",
                      "refresh_token": "r", "expires_at": time.time() - 10})

    calls = []
    barrier = threading.Barrier(2)

    def fake_refresh(te, cid, rt):
        calls.append(rt)
        return {"access_token": "new", "refresh_token": "r2", "expires_in": 3600}

    monkeypatch.setattr(auth, "_refresh", fake_refresh)

    results = {}

    def worker(key):
        barrier.wait()  # release both threads into _locked_refresh together
        results[key] = auth.resolve_token(TEAM)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(calls) == 1, "refresh token must be spent exactly once (no family revocation)"
    assert results[0] == results[1] == "new"
    assert auth._load_creds()["refresh_token"] == "r2"  # rotated token persisted


def test_refresh_skipped_when_serialization_unavailable(creds_env, monkeypatch):
    """On a non-POSIX runtime (no fcntl → store._store_lock can't actually serialize), refreshing
    the single-use token unserialized risks family revocation. So we must NOT refresh — degrade to
    the static/None token instead (the caller surfaces the re-login warning)."""
    auth._save_creds({"issuer": "http://localhost:8080", "client_id": "c",
                      "token_endpoint": "http://localhost:8080/token", "access_token": "old",
                      "refresh_token": "r", "expires_at": time.time() - 10})
    monkeypatch.setattr(store, "fcntl", None)  # simulate a platform without advisory locks
    monkeypatch.setattr(auth, "_refresh", lambda *a: pytest.fail("must not refresh without a real lock"))
    prof = config.Profile(mode="team", endpoint="http://localhost:8080/mcp", token="static")
    assert auth.resolve_token(prof) == "static"


# ── refresh_now (reactive path seam) ────────────────────────────────────────────────

def test_refresh_now_forces_refresh(creds_env, monkeypatch):
    auth._save_creds({"issuer": "http://localhost:8080", "client_id": "c",
                      "token_endpoint": "http://localhost:8080/token", "access_token": "old",
                      "refresh_token": "r", "expires_at": time.time() - 10})
    monkeypatch.setattr(auth, "_refresh",
                        lambda te, cid, rt: {"access_token": "new", "refresh_token": "r2", "expires_in": 3600})
    assert auth.refresh_now(TEAM) == "new"
    assert auth._load_creds()["refresh_token"] == "r2"


def test_refresh_now_double_check_skips_network_when_already_fresh(creds_env, monkeypatch):
    """If creds are already valid (a concurrent process just refreshed), refresh_now returns
    the current token WITHOUT a network call — this is what prevents the double-spend."""
    auth._save_creds({"issuer": "http://localhost:8080", "client_id": "c",
                      "token_endpoint": "http://localhost:8080/token", "access_token": "fresh",
                      "refresh_token": "r", "expires_at": time.time() + 3600})
    monkeypatch.setattr(auth, "_refresh", lambda *a: pytest.fail("must not hit the network"))
    assert auth.refresh_now(TEAM) == "fresh"


def test_refresh_now_falls_back_on_failure(creds_env, monkeypatch):
    auth._save_creds({"issuer": "http://localhost:8080", "client_id": "c",
                      "token_endpoint": "http://localhost:8080/token", "access_token": "old",
                      "refresh_token": "r", "expires_at": time.time() - 10})

    def boom(*a):
        raise RuntimeError("refresh token revoked")

    monkeypatch.setattr(auth, "_refresh", boom)
    prof = config.Profile(mode="team", endpoint="http://localhost:8080/mcp", token="static")
    assert auth.refresh_now(prof) == "static"


def test_locked_refresh_issuer_mismatch_falls_back(creds_env, monkeypatch):
    """Defensive re-read inside the lock: if the stored creds don't belong to this profile's
    issuer, fall back to the static token without touching the network."""
    auth._save_creds({"issuer": "http://other", "client_id": "c",
                      "token_endpoint": "http://other/token", "access_token": "x",
                      "refresh_token": "r", "expires_at": time.time() - 10})
    monkeypatch.setattr(auth, "_refresh", lambda *a: pytest.fail("must not refresh for a foreign issuer"))
    prof = config.Profile(mode="team", endpoint="http://localhost:8080/mcp", token="static")
    assert auth.refresh_now(prof) == "static"


# ── auth_state (telling an expired session from a bad token) ─────────────────────

def _stored(**overrides) -> dict:
    """Stored creds for the TEAM profile's issuer, valid unless overridden."""
    creds = {"issuer": "http://localhost:8080", "client_id": "c",
             "token_endpoint": "http://localhost:8080/token", "access_token": "SECRET-ACCESS",
             "refresh_token": "SECRET-REFRESH", "expires_at": time.time() + 3600,
             "scope": "sync"}
    creds.update(overrides)
    return creds


def test_auth_state_reports_a_live_session(creds_env):
    auth._save_creds(_stored())
    state = auth.auth_state(TEAM)
    assert state["state"] == "logged_in"
    assert state["issuer"] == "http://localhost:8080"
    assert state["scope"] == "sync"
    assert state["expires_at"].endswith("Z")  # ISO, not an epoch float
    assert state["message"]


def _raises(exc):
    def fail(*a):
        raise exc

    return fail


def _http_error(code: int, body: bytes = b'{"error": "invalid_grant"}'):
    """What `_refresh` raises when the token endpoint ANSWERS with an error status."""
    return urllib.error.HTTPError("http://localhost:8080/token", code, "Bad Request", {},
                                  io.BytesIO(body))


def test_auth_state_does_not_read_a_missing_outcome_as_a_rejection(creds_env):
    """A creds file written before the marker existed carries no outcome, and a missing
    outcome means UNKNOWN — never "the refresh was rejected"."""
    auth._save_creds(_stored(expires_at=time.time() - 10))
    assert auth._REFRESH_FAILED_AT not in auth._load_creds()
    state = auth.auth_state(TEAM)
    assert state["state"] == "renewable"


def test_auth_state_reports_refresh_failed_after_a_rejected_grant(creds_env, monkeypatch):
    """The live bug: creds match, they are expired, and the rotated refresh token is dead.
    An HTTP 4xx from the token endpoint is the AS answering "that grant is invalid" — the one
    thing that proves only a new login can fix this."""
    auth._save_creds(_stored(expires_at=time.time() - 10))
    monkeypatch.setattr(auth, "_refresh", _raises(_http_error(400)))
    assert auth.resolve_token(TEAM) is None  # fell back to the static token: there is none
    assert auth._load_creds()[auth._REFRESH_FAILED_AT]
    state = auth.auth_state(TEAM)
    assert state["state"] == "refresh_failed"
    assert "log in again" in state["message"]


@pytest.mark.parametrize("exc, why", [
    (urllib.error.URLError(OSError("Network is unreachable")), "offline / VPN down / DNS"),
    (_http_error(502, b"bad gateway"), "the AS itself is broken"),
    (TimeoutError("timed out"), "no answer at all"),
])
def test_an_unreachable_token_endpoint_is_not_recorded_as_a_rejection(creds_env, monkeypatch,
                                                                     exc, why):
    """A transport failure says NOTHING about the refresh token, and the marker is persistent:
    recording one here makes a five-minute outage tell the user for days that their session was
    rejected — `contexer status`, `contexer pull` and the console's red badge all at once."""
    auth._save_creds(_stored(expires_at=time.time() - 10))
    monkeypatch.setattr(auth, "_refresh", _raises(exc))
    assert auth.resolve_token(TEAM) is None
    assert auth._REFRESH_FAILED_AT not in auth._load_creds(), why
    state = auth.auth_state(TEAM)
    assert state["state"] == "renewable"
    assert "log in again" not in state["message"]


def test_auth_state_reports_a_renewable_session_rather_than_expired(creds_env, monkeypatch):
    """Access tokens are minted with expires_in 3600 and `resolve_token` spends the refresh
    token transparently on the next call, so "expired — log in again" fired every hour on a
    session with nothing wrong with it."""
    auth._save_creds(_stored(expires_at=time.time() - 10))
    monkeypatch.setattr(auth, "_refresh", lambda *a: pytest.fail("auth_state must not refresh"))
    state = auth.auth_state(TEAM)
    assert state["state"] == "renewable"
    assert "log in again" not in state["message"]


def test_auth_state_reports_expired_without_a_refresh_token(creds_env):
    """Nothing left to renew with — this session really does need the user to act."""
    auth._save_creds(_stored(expires_at=time.time() - 10, refresh_token=None))
    state = auth.auth_state(TEAM)
    assert state["state"] == "expired"
    assert "log in again" in state["message"]


def test_a_recorded_rejection_outranks_a_present_refresh_token(creds_env):
    """The token is still on disk but the AS refused it: renewable would be a lie."""
    auth._save_creds(_stored(expires_at=time.time() - 10, refresh_failed_at=time.time()))
    assert auth.auth_state(TEAM)["state"] == "refresh_failed"


def test_a_successful_refresh_clears_the_recorded_failure(creds_env, monkeypatch):
    auth._save_creds(_stored(expires_at=time.time() - 10, refresh_failed_at=time.time()))
    monkeypatch.setattr(auth, "_refresh",
                        lambda te, cid, rt: {"access_token": "new", "expires_in": 3600})
    assert auth.refresh_now(TEAM) == "new"
    assert auth._REFRESH_FAILED_AT not in auth._load_creds()
    assert auth.auth_state(TEAM)["state"] == "logged_in"


def test_auth_state_never_spends_a_refresh_token(creds_env, monkeypatch):
    """A read is a read: being ASKED about the session must not consume the single-use token
    that renewing it would need."""
    auth._save_creds(_stored(expires_at=time.time() - 10))
    monkeypatch.setattr(auth, "_refresh", lambda *a: pytest.fail("auth_state must not refresh"))
    assert auth.auth_state(TEAM)["state"] == "renewable"
    assert auth._load_creds()["refresh_token"] == "SECRET-REFRESH"


def test_auth_state_reports_static_only_for_a_foreign_issuer(creds_env):
    auth._save_creds(_stored(issuer="http://other"))
    prof = config.Profile(mode="team", endpoint="http://localhost:8080/mcp", token="static")
    state = auth.auth_state(prof)
    assert state["state"] == "static_only"
    assert state["issuer"] is None and state["expires_at"] is None


def test_auth_state_reports_none_without_any_credential(creds_env):
    assert auth.auth_state(TEAM)["state"] == "none"


def test_auth_state_never_returns_a_secret(creds_env):
    auth._save_creds(_stored())
    static = config.Profile(mode="team", endpoint="http://elsewhere/mcp", token="STATIC-BEARER")
    for profile in (TEAM, static):
        blob = json.dumps(auth.auth_state(profile))
        assert "SECRET-ACCESS" not in blob and "SECRET-REFRESH" not in blob
        assert "STATIC-BEARER" not in blob
        assert set(json.loads(blob)) == {"state", "issuer", "expires_at", "scope", "message"}


def test_auth_state_never_raises(creds_env):
    """It answers a browser and a CLI status line, so a broken endpoint (urlsplit raises on
    a torn IPv6 literal) has to cost the detail, not the response."""
    auth._save_creds(_stored())
    prof = config.Profile(mode="team", endpoint="http://[::1", token=None)
    assert auth.auth_state(prof)["state"] == "none"


# ── tracked login job ────────────────────────────────────────────────────────────

AUTH_URL = ("http://localhost:8080/authorize?response_type=code&client_id=CID9"
            "&redirect_uri=http%3A%2F%2F127.0.0.1%3A5555%2Fcallback&code_challenge=CHALLENGE"
            "&code_challenge_method=S256&state=STATEVALUE&scope=")
# What `login()` prints before it opens (or fails to open) a browser.
URL_OUTPUT = f"Opening your browser to sign in. If it doesn't open, visit:\n  {AUTH_URL}\n"


class FakeProc:
    """Stand-in for the login subprocess. No test may spawn a real login: it opens a browser,
    binds a loopback port and blocks with no timeout.

    `stdout` streams `output` line by line and then blocks until the process finishes or is
    killed — like the real child, which prints the authorize URL within a second and then
    lives on for minutes waiting for the callback."""

    def __init__(self, *, returncode=0, output="", block=False, timeout=False):
        self.returncode = returncode
        self.killed = False
        self.waited = []
        self._timeout = timeout
        self._release = threading.Event()
        if not (block or timeout):
            self._release.set()
        self.stdout = self._stream(output)

    def _stream(self, output):
        for line in output.splitlines(keepends=True):
            yield line
        assert self._release.wait(5), "the fake login was never released"

    def wait(self, timeout=None):
        self.waited.append(timeout)
        if self._timeout and not self.killed:
            raise subprocess.TimeoutExpired("contexer login", timeout or 0)
        assert self._release.wait(5), "the fake login was never released"
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._release.set()

    def finish(self, returncode=0):
        self.returncode = returncode
        self._release.set()


@pytest.fixture
def login_job(monkeypatch):
    """Isolate the module-global job slot and make a real spawn impossible."""
    monkeypatch.setattr(auth, "_login_job", None)
    monkeypatch.setattr(auth, "_spawn_login",
                        lambda endpoint: pytest.fail("must never spawn a real login"))

    def use(proc, capture=None):
        def spawn(endpoint):
            if capture is not None:
                capture.append(endpoint)
            return proc

        monkeypatch.setattr(auth, "_spawn_login", spawn)
        return proc

    return use


def _settled(job_id: str, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = auth.login_job_status(job_id)
        if status["state"] != "pending":
            return status
        time.sleep(0.005)
    raise AssertionError(f"login job never settled: {auth.login_job_status(job_id)}")


def _published(job_id: str, timeout: float = 3.0) -> str:
    deadline = time.time() + timeout
    while time.time() < deadline:
        url = auth.login_job_status(job_id)["auth_url"]
        if url:
            return url
        time.sleep(0.005)
    raise AssertionError("the authorize URL was never published")


def test_a_clean_login_job_reports_ok(creds_env, login_job):
    login_job(FakeProc(returncode=0))
    assert _settled(auth.start_login_job()) == {"state": "ok", "auth_url": None,
                                               "message": "Signed in to Contexer Teams."}


def test_a_failed_login_job_reports_the_scrubbed_tail_of_its_output(creds_env, login_job):
    """The message is rendered in the console, so scrubbing is load-bearing: `contexer login`
    prints the authorize URL, and an unscrubbed tail can put an OAuth code on screen."""
    login_job(FakeProc(returncode=1, output=(
        "Opening your browser to sign in. If it doesn't open, visit:\n"
        "  http://localhost:8080/authorize?response_type=code&client_id=CID9&"
        "code_challenge=CHALLENGE&code_challenge_method=S256&state=STATEVALUE&scope=\n"
        "contexer login: authorization failed — access_denied: bad code=AUTHCODE9\n")))
    status = _settled(auth.start_login_job())
    assert status["state"] == "failed"
    assert "access_denied" in status["message"]
    for secret in ("CID9", "CHALLENGE", "STATEVALUE", "AUTHCODE9"):
        assert secret not in status["message"]
    assert "REDACTED" in status["message"]


def test_a_login_failure_message_is_bounded(creds_env, login_job):
    """The tail lands in a UI and a bug report; a child that prints a megabyte must not."""
    login_job(FakeProc(returncode=1, output="x" * 5000 + "\n" + "y" * 5000 + "\n"))
    message = _settled(auth.start_login_job())["message"]
    assert len(message) < 2 * auth._MESSAGE_LINE_LIMIT + 40


def test_a_login_job_that_prints_nothing_still_reports_a_failure(creds_env, login_job):
    login_job(FakeProc(returncode=1, output=""))
    status = _settled(auth.start_login_job())
    assert status["message"] == "Login failed."
    assert status["auth_url"] is None


# ── the authorize URL (logging in where no browser can open) ─────────────────────

def test_a_pending_login_publishes_the_authorize_url(creds_env, login_job):
    """On a headless box `webbrowser.open` no-ops and this URL is the only way to finish the
    login, so it has to reach the caller WHILE the child is still waiting for the callback —
    not in the post-mortem of a flow that already timed out."""
    proc = login_job(FakeProc(block=True, output=URL_OUTPUT))
    job = auth.start_login_job()
    assert _published(job) == AUTH_URL
    assert auth.login_job_status(job)["state"] == "pending"  # published mid-flight, not at exit
    proc.finish()
    assert _settled(job)["state"] == "ok"


def test_the_authorize_url_outlives_a_timeout(creds_env, login_job):
    """The timeout branch used to throw the child's output away wholesale, taking the one
    affordance that would have explained how to finish the login with it."""
    login_job(FakeProc(timeout=True, output=URL_OUTPUT))
    status = _settled(auth.start_login_job())
    assert status["state"] == "failed" and "timed out" in status["message"]
    assert status["auth_url"] == AUTH_URL


def test_the_published_authorize_url_keeps_the_parameters_that_make_it_work(creds_env,
                                                                            login_job):
    """`_failure_message` redacts client_id/state/code_challenge — correctly, it is a rendered
    error string. A URL with those redacted is not a link, so the affordance travels in its
    own field instead of being reconstructed from a scrubbed message."""
    proc = login_job(FakeProc(block=True, output=URL_OUTPUT))
    url = _published(auth.start_login_job())
    assert "client_id=CID9" in url and "state=STATEVALUE" in url
    assert "code_challenge=CHALLENGE" in url and "REDACTED" not in url
    proc.finish()


@pytest.mark.parametrize("line", [
    "callback: http://127.0.0.1:5555/callback?code=AUTHCODE9&state=STATEVALUE",  # a credential
    "posting to http://localhost:8080/token",
    "http://localhost:8080/authorize?response_type=token&code_challenge=X",  # not our flow
    "Opening your browser to sign in. If it doesn't open, visit:",
])
def test_only_an_authorize_request_is_ever_published(line):
    """An authorize URL is the address of a consent page; an authorization code is a secret.
    Nothing that is not the former may be handed to a UI as a login link."""
    assert auth._authorize_url(line) is None


def test_the_authorize_url_is_lifted_out_of_its_line(creds_env):
    assert auth._authorize_url(f"  {AUTH_URL}") == AUTH_URL


def test_only_one_login_job_runs_at_a_time(creds_env, login_job):
    proc = login_job(FakeProc(block=True))
    job = auth.start_login_job()
    assert auth.login_job_status(job)["state"] == "pending"
    with pytest.raises(auth.LoginJobBusy, match="already running") as busy:
        auth.start_login_job()
    # The refusal names the job in flight, so the caller can follow ITS outcome rather than
    # inferring one from the session going live.
    assert busy.value.job_id == job
    assert auth.stop_login_job() is True
    assert proc.killed is True


def test_a_login_job_over_the_cap_is_killed_and_reported_failed(creds_env, login_job):
    proc = login_job(FakeProc(timeout=True))
    status = _settled(auth.start_login_job())
    assert status["state"] == "failed" and "timed out" in status["message"]
    assert proc.killed is True
    assert proc.waited[0] == auth.LOGIN_TIMEOUT == 300.0  # ~5 min, and it is really passed


def test_stopping_a_login_job_wins_over_the_waiter(creds_env, login_job):
    """The kill resolves the job; the waiter thread must not then relabel it "the process
    died" and lose the reason the user is being shown."""
    login_job(FakeProc(block=True))
    job = auth.start_login_job()
    assert auth.stop_login_job() is True
    status = _settled(job)
    assert status["state"] == "failed" and "console stopped" in status["message"]
    assert auth.stop_login_job() is False  # nothing left to kill


def test_stopping_a_login_job_records_the_callers_reason(creds_env, login_job):
    """A logout kills an in-flight login too — for a completely different reason, and the tab
    attached to that job is told the message verbatim."""
    login_job(FakeProc(block=True))
    job = auth.start_login_job()
    assert auth.stop_login_job("Signed out — the login in progress was cancelled.") is True
    assert _settled(job)["message"] == "Signed out — the login in progress was cancelled."


def test_login_job_status_is_none_for_an_unknown_id(creds_env, login_job):
    assert auth.login_job_status("nope") is None
    login_job(FakeProc(returncode=0))
    job = auth.start_login_job()
    _settled(job)
    assert auth.login_job_status(job + "x") is None


def test_a_login_job_takes_no_endpoint_from_its_caller(creds_env, login_job):
    """A caller-supplied endpoint would aim the OAuth flow at an attacker's IdP and persist a
    token for it, so the endpoint is read from config and the signature accepts nothing."""
    import inspect

    assert inspect.signature(auth.start_login_job).parameters == {}
    config.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.CONFIG_PATH.write_text('mode = "team"\nendpoint = "http://localhost:8080/mcp"\n')
    endpoints = []
    login_job(FakeProc(returncode=0), capture=endpoints)
    _settled(auth.start_login_job())
    assert endpoints == ["http://localhost:8080/mcp"]


def test_a_login_job_uses_the_default_endpoint_when_config_is_unusable(creds_env, login_job):
    config.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.CONFIG_PATH.write_text("mode = [unparseable\n")
    endpoints = []
    login_job(FakeProc(returncode=0), capture=endpoints)
    _settled(auth.start_login_job())
    assert endpoints == [config.default_endpoint()]


def test_the_login_job_spawns_the_cli_through_the_module_form(creds_env, monkeypatch):
    """`python -m contexer login` is the contract with cli.py — verified by hand against
    contexer/__main__.py, pinned here so a rename cannot silently break the button."""
    monkeypatch.setattr(auth, "_login_job", None)
    captured = {}

    class FakePopen:
        def __init__(self, argv, **kwargs):
            captured["argv"] = argv
            captured["kwargs"] = kwargs
            self.returncode = 0
            self.stdout = io.StringIO("")

        def wait(self, timeout=None):
            return 0

    monkeypatch.setattr(auth.subprocess, "Popen", FakePopen)
    _settled(auth.start_login_job())
    argv = captured["argv"]
    # `-u`: stdout is a pipe, so without it the authorize URL sits in the child's block buffer
    # until the flow ends — which is minutes after the only moment it is useful.
    assert argv[:5] == [auth.sys.executable, "-u", "-m", "contexer", "login"]
    assert argv[5] == "--endpoint" and argv[6]
    assert captured["kwargs"]["stdin"] is subprocess.DEVNULL  # never prompts on our stdin
    assert captured["kwargs"]["stderr"] is subprocess.STDOUT  # the tail carries the reason


# ── login / logout ───────────────────────────────────────────────────────────────

def _stub_oauth(monkeypatch, *, discover=None):
    monkeypatch.setattr(auth, "_discover", discover or (lambda issuer: {
        "authorization_endpoint": "http://localhost:8080/authorize",
        "token_endpoint": "http://localhost:8080/token",
        "registration_endpoint": "http://localhost:8080/register"}))
    monkeypatch.setattr(auth, "_free_port", lambda: 5555)
    monkeypatch.setattr(auth, "_register", lambda re, ru: "cid-9")
    monkeypatch.setattr(auth, "_await_code", lambda url, port, state: "authcode")
    monkeypatch.setattr(auth, "_exchange_code",
                        lambda te, cid, code, ver, ru: {"access_token": "AT", "refresh_token": "RT", "expires_in": 3600})


def test_login_saves_creds_and_writes_config(creds_env, monkeypatch):
    _stub_oauth(monkeypatch)
    auth.login(endpoint="http://localhost:8080/mcp")
    creds = auth._load_creds()
    assert creds["access_token"] == "AT"
    assert creds["refresh_token"] == "RT"
    assert creds["client_id"] == "cid-9"
    assert creds["issuer"] == "http://localhost:8080"
    assert creds["token_endpoint"] == "http://localhost:8080/token"
    assert creds["expires_at"] > time.time()
    # self-configured: no manual config.toml — login wrote team mode + endpoint
    prof = config.load_profile()
    assert prof.mode == "team"
    assert prof.endpoint == "http://localhost:8080/mcp"


def test_login_self_configures_over_an_unloadable_ui_table(creds_env, monkeypatch):
    """The browser flow is done and the tokens are already on disk by the time config.toml is
    written, so a hand-edited `[ui]` value that aborts that write leaves mode/endpoint unset —
    team sync off forever, and every retry re-runs the whole flow to fail the same way."""
    _stub_oauth(monkeypatch)
    config.CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    config.CONFIG_PATH.write_text('[ui]\nport = "31500"\n')
    auth.login(endpoint="http://localhost:8080/mcp")
    assert auth._load_creds()["access_token"] == "AT"
    profile = config.load_profile()
    assert (profile.mode, profile.endpoint) == ("team", "http://localhost:8080/mcp")


def test_login_defaults_endpoint(creds_env, monkeypatch):
    captured = {}

    def fake_discover(issuer):
        captured["issuer"] = issuer
        return {"authorization_endpoint": "http://x/authorize", "token_endpoint": "http://x/token",
                "registration_endpoint": "http://x/register"}

    monkeypatch.setattr(auth, "default_endpoint", lambda: "http://localhost:8080/mcp")
    _stub_oauth(monkeypatch, discover=fake_discover)
    auth.login()  # no endpoint -> uses default_endpoint()
    assert captured["issuer"] == "http://localhost:8080"
    assert config.load_profile().endpoint == "http://localhost:8080/mcp"


def test_login_fails_loudly_without_access_token(creds_env, monkeypatch):
    _stub_oauth(monkeypatch)
    monkeypatch.setattr(auth, "_exchange_code", lambda *a: {"expires_in": 3600})  # no access_token
    with pytest.raises(RuntimeError, match="no access_token"):
        auth.login(endpoint="http://localhost:8080/mcp")
    assert auth._load_creds() is None  # nothing persisted, no false "logged in"


def test_login_rejects_invalid_endpoint(creds_env, monkeypatch):
    _stub_oauth(monkeypatch)
    for bad in ("mcp.contexer.ai/mcp", "ftp://x/mcp", "https://", "not a url"):
        with pytest.raises(ValueError):
            auth.login(endpoint=bad)
    assert auth._load_creds() is None  # nothing persisted on rejection


def test_logout_deletes_creds(creds_env):
    auth._save_creds({"issuer": "x", "access_token": "a"})
    assert auth.logout() is True
    assert auth._load_creds() is None
    assert auth.logout() is False


# ── RemoteStore.from_profile integration ─────────────────────────────────────────

def test_from_profile_uses_resolve_token(monkeypatch):
    from contexer.remote import RemoteStore
    monkeypatch.setattr("contexer.auth.resolve_token", lambda p: "oauth-tok")
    rs = RemoteStore.from_profile(config.Profile(mode="team", endpoint="http://x/mcp", token=None))
    assert rs is not None and rs._token == "oauth-tok"


def test_from_profile_none_when_no_token(monkeypatch):
    from contexer.remote import RemoteStore
    monkeypatch.setattr("contexer.auth.resolve_token", lambda p: None)
    assert RemoteStore.from_profile(config.Profile(mode="team", endpoint="http://x/mcp", token="t")) is None


# ── CLI ──────────────────────────────────────────────────────────────────────────

def test_cli_login_dispatches(monkeypatch):
    from contexer import cli
    captured = {}
    monkeypatch.setattr(auth, "login", lambda endpoint=None: captured.update(endpoint=endpoint))
    monkeypatch.setattr(cli, "_post_login_sync", lambda: None)  # isolate: no network
    cli.login_cmd([])
    assert "endpoint" in captured and captured["endpoint"] is None


def test_cli_login_endpoint_flag(monkeypatch):
    from contexer import cli
    captured = {}
    monkeypatch.setattr(auth, "login", lambda endpoint=None: captured.update(endpoint=endpoint))
    monkeypatch.setattr(cli, "_post_login_sync", lambda: None)  # isolate: no network
    cli.login_cmd(["--endpoint", "http://x/mcp"])
    assert captured["endpoint"] == "http://x/mcp"


def test_cli_login_triggers_post_login_sync(monkeypatch):
    # After a successful login, status must not be stale — login kicks a team pull.
    from contexer import cli
    called = {}
    monkeypatch.setattr(auth, "login", lambda endpoint=None: None)
    monkeypatch.setattr(cli, "_post_login_sync", lambda: called.setdefault("ran", True))
    cli.login_cmd([])
    assert called.get("ran") is True


def test_post_login_sync_noop_when_no_repo(monkeypatch):
    from contexer import cli, store, team_context
    monkeypatch.setattr(store, "_git_root", lambda cwd: None)
    monkeypatch.setattr(store, "_current_repo_path", lambda: "")
    monkeypatch.setattr(team_context, "refresh",
                        lambda *a, **k: pytest.fail("refresh must not run with no repo"))
    cli._post_login_sync()  # returns cleanly, no refresh


def test_post_login_sync_refreshes_and_reports(monkeypatch, capsys):
    from contexer import cli, store, team_context
    monkeypatch.setattr(store, "_git_root", lambda cwd: "/repo")
    monkeypatch.setattr(store, "_current_repo_path", lambda: "/repo")  # same → deduped
    calls = []
    monkeypatch.setattr(team_context, "refresh", lambda repo: calls.append(repo) or (2, 1))
    cli._post_login_sync()
    out = capsys.readouterr().out
    assert calls == ["/repo"]  # deduped: refreshed once
    assert "Synced 2 team decision(s)" in out and "removed 1" in out


def test_post_login_sync_refreshes_current_repo_when_cwd_differs(monkeypatch, capsys):
    # The reported bug: login run outside the repo `status` displays. Both the cwd repo and
    # the .current_repo pointer must be refreshed so the stale line clears either way.
    from contexer import cli, store, team_context
    monkeypatch.setattr(store, "_git_root", lambda cwd: "/cli-repo")
    monkeypatch.setattr(store, "_current_repo_path", lambda: "/app-repo")
    refreshed = []
    monkeypatch.setattr(team_context, "refresh", lambda repo: refreshed.append(repo) or (1, 0))
    cli._post_login_sync()
    assert refreshed == ["/cli-repo", "/app-repo"]
    assert "Synced 2 team decision(s)" in capsys.readouterr().out


def test_post_login_sync_swallows_errors(monkeypatch):
    from contexer import cli, store, team_context
    monkeypatch.setattr(store, "_git_root", lambda cwd: "/repo")
    monkeypatch.setattr(store, "_current_repo_path", lambda: "")

    def boom(repo):
        raise RuntimeError("network exploded")

    monkeypatch.setattr(team_context, "refresh", boom)  # refresh normally never raises
    cli._post_login_sync()  # the outer guard must still swallow it — login already succeeded


def test_cli_login_endpoint_flag_missing_url(monkeypatch, capsys):
    from contexer import cli
    monkeypatch.setattr(auth, "login", lambda endpoint=None: pytest.fail("login must not run"))
    with pytest.raises(SystemExit):
        cli.login_cmd(["--endpoint"])
    assert "--endpoint requires a URL" in capsys.readouterr().err


def test_cli_login_reports_invalid_endpoint(monkeypatch, capsys):
    from contexer import cli

    def bad_login(endpoint=None):
        raise ValueError("invalid Teams endpoint")

    monkeypatch.setattr(auth, "login", bad_login)
    with pytest.raises(SystemExit):
        cli.login_cmd(["--endpoint", "junk"])
    assert "invalid Teams endpoint" in capsys.readouterr().err


def test_cli_logout_dispatches(monkeypatch, capsys):
    from contexer import cli
    monkeypatch.setattr(auth, "logout", lambda: True)
    cli.logout_cmd([])
    assert "logged out" in capsys.readouterr().out.lower()


def test_the_login_output_drain_is_bounded(creds_env, login_job, monkeypatch):
    """The drain kept EVERY line a child printed, for up to LOGIN_TIMEOUT, in a daemon that
    stays up for days. Asserted on the container itself: the message below is identical either
    way, so only its LENGTH can catch an unbounded collection."""
    captured = {}
    real_reader = auth._read_login_output

    def spy(job, lines):
        captured["lines"] = lines
        return real_reader(job, lines)

    monkeypatch.setattr(auth, "_read_login_output", spy)
    login_job(FakeProc(returncode=1, output="".join(f"line {i}\n" for i in range(5000))))

    _settled(auth.start_login_job())

    assert len(captured["lines"]) <= auth._MAX_OUTPUT_LINES


def test_the_output_cap_keeps_the_lines_the_failure_message_reads(creds_env, login_job):
    """`_failure_message` reads `lines[-2:]`, so the cap has to keep the LAST lines, not the
    first — a plain head-truncation would report startup chatter as the reason a login failed."""
    proc = FakeProc(returncode=1,
                    output="".join(f"noise {i}\n" for i in range(auth._MAX_OUTPUT_LINES * 3))
                           + "second to last\nthe real error\n")

    login_job(proc)
    message = _settled(auth.start_login_job())["message"]

    assert message.endswith("second to last the real error")
    assert "noise 0" not in message


# ── account switch invalidates caches (issue #232) ──────────────────────────────────
# Nothing on disk records WHICH account a cache belongs to, and `_sync` is a delta that never
# learns the previous account's rows should go - so login/logout, the only moments a switch is
# definitely known, must discard rather than try to detect.

def _seed_team_caches(creds_env):
    """The three cache shapes an account switch strands, plus the creds file it must not eat."""
    from contexer import share, team_context
    store.STORE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    team_context._save_cache("/repo/a", {"repo_key": "k", "cursor": "c",
                                         "decisions": [{"id": "old-1"}], "seq": 40})
    team_context._write_seen("/repo/a", "claude", 40)
    share._append_shared([{"endpoint": "https://mcp.contexer.ai/mcp", "id": "old-1", "at": "t"}])
    auth._save_creds({"issuer": "x", "access_token": "a"})


def test_logout_clears_team_caches(creds_env):
    from contexer import share, team_context
    _seed_team_caches(creds_env)
    assert auth.logout() is True
    assert team_context._load_cache("/repo/a")["decisions"] == []
    assert team_context._read_seen("/repo/a", "claude") is None
    assert share.shared_map("https://mcp.contexer.ai/mcp") == {}


def test_login_clears_the_previous_accounts_caches(creds_env, monkeypatch, capsys):
    from contexer import share, team_context
    _seed_team_caches(creds_env)
    _stub_oauth(monkeypatch)
    auth.login(endpoint="http://localhost:8080/mcp")
    assert team_context._load_cache("/repo/a")["decisions"] == []
    assert share.shared_map("https://mcp.contexer.ai/mcp") == {}
    assert "Cleared" in capsys.readouterr().out
    # The creds this very login just wrote live in the same `.team_*` namespace and must survive.
    assert auth._load_creds()["access_token"] == "AT"


def test_seen_marker_never_outlives_its_cache(creds_env):
    """The marker holds a high-water `seq` into the CACHE's own sync log, whose counter restarts
    with the cache. Left at 40 beside a rebuilt log it would suppress every batch until the new
    log passed 40 - so the pairing, not just the cache, is what has to be cleared."""
    from contexer import team_context
    _seed_team_caches(creds_env)
    team_context.clear_caches()
    assert not team_context._seen_path("/repo/a", "claude").exists()


def test_clear_caches_is_fail_soft_on_an_undeletable_file(creds_env, monkeypatch):
    """Login has already succeeded by the time this runs; hygiene must never raise into it."""
    from contexer import team_context
    _seed_team_caches(creds_env)
    monkeypatch.setattr(Path, "unlink", lambda self, **kw: (_ for _ in ()).throw(OSError("busy")))
    assert team_context.clear_caches() == 0
    assert auth._forget_account_caches() == 0


def test_login_discards_the_previous_accounts_queued_shares(creds_env, monkeypatch, capsys):
    """The outbox carries no account identity, and `cli._post_login_sync` drains it seconds
    after login with the credentials just stored - so a surviving queue would push account A's
    decisions up as account B's rows."""
    from contexer import share
    _seed_team_caches(creds_env)
    share._enqueue({"decision_id": "queued-1", "type": "constraint", "content": "old account's",
                    "repo": "github.com/a/b", "queued_at": 0, "attempts": 0})
    _stub_oauth(monkeypatch)
    auth.login(endpoint="http://localhost:8080/mcp")
    assert share._load_outbox() == []
    out = capsys.readouterr().out
    assert "Discarded 1 share(s)" in out          # visible, not a silent drop
    assert "contexer share" in out                # and recoverable


def test_logout_leaves_the_outbox_alone(creds_env):
    """Nothing drains without credentials, so logout discards no queue that was ever at risk."""
    from contexer import share
    _seed_team_caches(creds_env)
    share._enqueue({"decision_id": "queued-1", "type": "constraint", "content": "still mine",
                    "repo": "github.com/a/b", "queued_at": 0, "attempts": 0})
    auth.logout()
    assert [e["decision_id"] for e in share._load_outbox()] == ["queued-1"]


def test_discard_outbox_reports_zero_when_empty(creds_env):
    from contexer import share
    assert share.discard_outbox() == 0
