"""Tests for C-auth: contexer/auth.py (zero-paste OAuth) + RemoteStore token resolution.

Network (_request) and the interactive browser leg (_await_code) are faked/omitted; the
OAuth mechanics (PKCE, DCR, code/refresh exchange, token resolution) are fully covered.
"""
import base64
import hashlib
import time

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
    assert auth._issuer_from_endpoint("https://mcp.dev.contexer.ai/mcp") == "https://mcp.dev.contexer.ai"
    assert auth._issuer_from_endpoint("https://x.example") == "https://x.example"


def test_free_port_returns_bindable_int():
    port = auth._free_port()
    assert isinstance(port, int) and 1024 <= port <= 65535


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


def test_resolve_token_refresh_failure_falls_back(creds_env, monkeypatch):
    auth._save_creds({"issuer": "http://localhost:8080", "client_id": "c",
                      "token_endpoint": "http://localhost:8080/token", "access_token": "old",
                      "refresh_token": "r", "expires_at": time.time() - 10})

    def boom(*a):
        raise RuntimeError("refresh failed")

    monkeypatch.setattr(auth, "_refresh", boom)
    prof = config.Profile(mode="team", endpoint="http://localhost:8080/mcp", token="static")
    assert auth.resolve_token(prof) == "static"


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


def test_default_endpoint_env(monkeypatch):
    monkeypatch.setenv("CONTEXER_ENV", "local")
    assert auth.default_endpoint() == "http://localhost:8080/mcp"
    monkeypatch.delenv("CONTEXER_ENV", raising=False)
    assert auth.default_endpoint() == "https://mcp.dev.contexer.ai/mcp"


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
    cli.login_cmd([])
    assert "endpoint" in captured and captured["endpoint"] is None


def test_cli_login_endpoint_flag(monkeypatch):
    from contexer import cli
    captured = {}
    monkeypatch.setattr(auth, "login", lambda endpoint=None: captured.update(endpoint=endpoint))
    cli.login_cmd(["--endpoint", "http://x/mcp"])
    assert captured["endpoint"] == "http://x/mcp"


def test_cli_logout_dispatches(monkeypatch, capsys):
    from contexer import cli
    monkeypatch.setattr(auth, "logout", lambda: True)
    cli.logout_cmd([])
    assert "logged out" in capsys.readouterr().out.lower()
