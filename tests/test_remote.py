"""Tests for the RemoteStore sync client (contexer/remote.py).

Unit-level: the network seam `remote._call_tool` is monkeypatched so these tests assert
the wire serialization (snake->camel, None-omitted), result parsing, and error mapping
without touching a real Teams server. The real transport is covered by the opt-in
integration test documented in the manual steps.
"""
import types

import pytest

from contexer import remote
from contexer.config import Profile
from contexer.remote import (
    RemoteAuthError,
    RemoteContext,
    RemoteDecision,
    RemoteStore,
    RemoteStoreError,
    RemoteUnavailableError,
)


def _result(*, content=None, structured=None, is_error=False):
    """A fake CallToolResult — RemoteStore only reads these three attributes."""
    return types.SimpleNamespace(content=content or [], structuredContent=structured, isError=is_error)


def _text(s):
    return types.SimpleNamespace(type="text", text=s)


def _http_error(status):
    """An httpx-style error carrying `.response.status_code`."""
    exc = Exception(f"HTTP {status}")
    exc.response = types.SimpleNamespace(status_code=status)
    return exc


# ── from_profile ────────────────────────────────────────────────────────────────

def test_from_profile_team_builds_store():
    rs = RemoteStore.from_profile(Profile(mode="team", endpoint="https://t/mcp", token="tok"))
    assert isinstance(rs, RemoteStore)


def test_from_profile_local_returns_none():
    assert RemoteStore.from_profile(Profile()) is None


def test_from_profile_team_missing_token_returns_none():
    assert RemoteStore.from_profile(Profile(mode="team", endpoint="https://t/mcp", token=None)) is None


def test_from_profile_team_missing_endpoint_returns_none():
    assert RemoteStore.from_profile(Profile(mode="team", endpoint=None, token="tok")) is None


# ── push_decision serialization + id parsing ─────────────────────────────────────

def test_push_decision_serializes_full_wire_shape(monkeypatch):
    captured = {}

    def fake_call(endpoint, token, name, arguments, timeout):
        captured.update(endpoint=endpoint, token=token, name=name, args=arguments, timeout=timeout)
        return _result(content=[_text("Saved decision dec-42 to your personal context.")])

    monkeypatch.setattr(remote, "_call_tool", fake_call)
    rs = RemoteStore("https://t/mcp", "tok", timeout=7.0)
    did = rs.push_decision(
        type="architecture", content="use X", repo="github.com/a/b",
        rationale="why", agent="claude", confidence=80,
        evidence=["e1", "e2"], source="ai", decision_id="local-1",
    )
    assert captured["name"] == "push_decision"
    assert captured["args"] == {
        "type": "architecture", "content": "use X", "repo": "github.com/a/b",
        "rationale": "why", "agent": "claude", "confidence": 80,
        "evidence": ["e1", "e2"], "source": "ai", "decisionId": "local-1",
    }
    assert captured["endpoint"] == "https://t/mcp"
    assert captured["token"] == "tok"
    assert captured["timeout"] == 7.0
    assert did == "dec-42"


def test_push_decision_omits_none_optionals(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        remote, "_call_tool",
        lambda e, t, n, args, to: captured.update(args=args)
        or _result(content=[_text("Saved decision x to your personal context.")]),
    )
    RemoteStore("https://t/mcp", "tok").push_decision(type="constraint", content="c", repo=None)
    assert captured["args"] == {"type": "constraint", "content": "c"}


def test_push_decision_returns_empty_when_no_id_in_message(monkeypatch):
    monkeypatch.setattr(remote, "_call_tool", lambda *a, **k: _result(content=[_text("ok")]))
    did = RemoteStore("https://t/mcp", "tok").push_decision(type="convention", content="c", repo=None)
    assert did == ""


# ── get_context serialization + parsing ──────────────────────────────────────────

def test_get_context_parses_structured_content(monkeypatch):
    captured = {}
    structured = {
        "result": [{
            "id": "1", "type": "constraint", "content": "c",
            "rationale": None, "repo": "r", "agent": "a", "scope": "team",
        }],
        "deleted": ["9"],
        "cursor": "2026-01-01T00:00:00Z",
    }

    def fake_call(endpoint, token, name, arguments, timeout):
        captured.update(name=name, args=arguments)
        return _result(structured=structured)

    monkeypatch.setattr(remote, "_call_tool", fake_call)
    ctx = RemoteStore("https://t/mcp", "tok").get_context(
        repo="r", updated_since="2025-12-31T00:00:00Z",
    )
    assert captured["name"] == "get_context"
    assert captured["args"] == {"repo": "r", "updatedSince": "2025-12-31T00:00:00Z"}
    assert isinstance(ctx, RemoteContext)
    assert ctx.deleted == ["9"]
    assert ctx.cursor == "2026-01-01T00:00:00Z"
    assert ctx.decisions == [
        RemoteDecision(id="1", type="constraint", content="c", rationale=None,
                       repo="r", agent="a", scope="team"),
    ]


def test_get_context_omits_none_args(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        remote, "_call_tool",
        lambda e, t, n, args, to: captured.update(args=args)
        or _result(structured={"result": [], "deleted": [], "cursor": None}),
    )
    ctx = RemoteStore("https://t/mcp", "tok").get_context()
    assert captured["args"] == {}
    assert ctx.decisions == []
    assert ctx.deleted == []
    assert ctx.cursor is None


def test_get_context_missing_structured_yields_empty(monkeypatch):
    monkeypatch.setattr(remote, "_call_tool", lambda *a, **k: _result(structured=None))
    ctx = RemoteStore("https://t/mcp", "tok").get_context()
    assert ctx.decisions == []
    assert ctx.deleted == []
    assert ctx.cursor is None


# ── error mapping (feeds C8 degradation) ─────────────────────────────────────────

def test_connection_error_maps_to_unavailable(monkeypatch):
    def boom(*a, **k):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(remote, "_call_tool", boom)
    with pytest.raises(RemoteUnavailableError):
        RemoteStore("https://t/mcp", "tok").get_context()


@pytest.mark.parametrize("status", [401, 403])
def test_http_auth_status_maps_to_auth_error(monkeypatch, status):
    monkeypatch.setattr(remote, "_call_tool", lambda *a, **k: (_ for _ in ()).throw(_http_error(status)))
    with pytest.raises(RemoteAuthError):
        RemoteStore("https://t/mcp", "tok").get_context()


def test_http_500_maps_to_unavailable(monkeypatch):
    monkeypatch.setattr(remote, "_call_tool", lambda *a, **k: (_ for _ in ()).throw(_http_error(500)))
    with pytest.raises(RemoteUnavailableError):
        RemoteStore("https://t/mcp", "tok").get_context()


def test_exception_group_401_maps_to_auth_error(monkeypatch):
    def boom(*a, **k):
        raise ExceptionGroup("transport", [_http_error(401)])

    monkeypatch.setattr(remote, "_call_tool", boom)
    with pytest.raises(RemoteAuthError):
        RemoteStore("https://t/mcp", "tok").push_decision(type="constraint", content="c", repo=None)


def test_tool_error_result_raises(monkeypatch):
    monkeypatch.setattr(
        remote, "_call_tool",
        lambda *a, **k: _result(content=[_text("invalid input")], is_error=True),
    )
    with pytest.raises(RemoteStoreError):
        RemoteStore("https://t/mcp", "tok").push_decision(type="constraint", content="c", repo=None)


def test_tool_error_with_empty_content_uses_fallback_message(monkeypatch):
    monkeypatch.setattr(remote, "_call_tool", lambda *a, **k: _result(content=[], is_error=True))
    with pytest.raises(RemoteStoreError, match="get_context failed"):
        RemoteStore("https://t/mcp", "tok").get_context()


def test_invoke_reraises_typed_error_unwrapped(monkeypatch):
    def boom(*a, **k):
        raise RemoteAuthError("already typed")

    monkeypatch.setattr(remote, "_call_tool", boom)
    with pytest.raises(RemoteAuthError, match="already typed"):
        RemoteStore("https://t/mcp", "tok").get_context()


@pytest.mark.parametrize("message", [
    "This token lacks the 'write' scope required for this action.",
    "Forbidden",
    "unauthorized",
    "You do not have permission to do that.",
])
def test_authz_tool_error_maps_to_auth_error(monkeypatch, message):
    """A reachable-but-refusing cloud (insufficient scope / permission) is an auth failure,
    NOT a transport outage — so it must raise RemoteAuthError, never a bare RemoteStoreError."""
    monkeypatch.setattr(
        remote, "_call_tool",
        lambda *a, **k: _result(content=[_text(message)], is_error=True),
    )
    with pytest.raises(RemoteAuthError):
        RemoteStore("https://t/mcp", "tok").push_decision(type="constraint", content="c", repo=None)


def test_generic_tool_error_is_not_auth_error(monkeypatch):
    """A non-authorization tool error (e.g. bad input) stays a plain RemoteStoreError so the
    scope-error classifier doesn't over-broaden into every failure."""
    monkeypatch.setattr(
        remote, "_call_tool",
        lambda *a, **k: _result(content=[_text("invalid input: content too long")], is_error=True),
    )
    with pytest.raises(RemoteStoreError) as exc:
        RemoteStore("https://t/mcp", "tok").push_decision(type="constraint", content="c", repo=None)
    assert not isinstance(exc.value, RemoteAuthError)


def test_scope_mentioning_validation_error_is_not_auth_error(monkeypatch):
    """A validation error that merely mentions a 'scope' parameter must NOT be misclassified as
    an authorization failure - the phrase-level regex only matches genuine auth denials."""
    monkeypatch.setattr(
        remote, "_call_tool",
        lambda *a, **k: _result(
            content=[_text("Value for 'scope' parameter must be a string")], is_error=True),
    )
    with pytest.raises(RemoteStoreError) as exc:
        RemoteStore("https://t/mcp", "tok").push_decision(type="constraint", content="c", repo=None)
    assert not isinstance(exc.value, RemoteAuthError)


def test_scope_error_degrades_via_auth_branch(monkeypatch, capsys):
    """End-to-end for Bug 2: a missing-write-scope push, run through with_local_fallback, warns
    the user to re-authenticate — instead of the misleading 'endpoint unreachable' message."""
    monkeypatch.setattr(
        remote, "_call_tool",
        lambda *a, **k: _result(
            content=[_text("This token lacks the 'write' scope required for this action.")],
            is_error=True),
    )
    remote.reset_degradation_warnings()
    store = RemoteStore("https://t/mcp", "tok")
    result = remote.with_local_fallback(
        lambda: store.push_decision(type="constraint", content="c", repo=None),
        default=None, action="share decision")
    assert result is None
    err = capsys.readouterr().err
    assert "authentication failed" in err and "contexer login" in err
    assert "unreachable" not in err


# ── content parsing edge (SDK returns dict content items in some transports) ──────

def test_push_decision_reads_dict_content_item(monkeypatch):
    monkeypatch.setattr(
        remote, "_call_tool",
        lambda *a, **k: _result(content=[{"type": "text", "text": "Saved decision dd-7 to your personal context."}]),
    )
    did = RemoteStore("https://t/mcp", "tok").push_decision(type="constraint", content="c", repo=None)
    assert did == "dd-7"


# ── C8: offline / auth-failure degradation ───────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_degrade_warnings():
    """Isolate the process-wide warn-once state between tests."""
    remote.reset_degradation_warnings()
    yield
    remote.reset_degradation_warnings()


def _raise(exc):
    """A zero-arg op that raises `exc` (used as the `op` passed to with_local_fallback)."""
    def op():
        raise exc
    return op


def test_with_local_fallback_returns_op_result_on_success(capsys):
    result = remote.with_local_fallback(lambda: "ok", default="fallback", action="pull")
    assert result == "ok"
    assert capsys.readouterr().err == ""


def test_with_local_fallback_unreachable_returns_default_and_warns_once(capsys):
    op = _raise(RemoteUnavailableError("down"))
    r1 = remote.with_local_fallback(op, default=[], action="pull team context")
    r2 = remote.with_local_fallback(op, default=[], action="pull team context")
    assert r1 == [] and r2 == []
    err = capsys.readouterr().err
    assert err.count("Contexer:") == 1
    assert "unreachable" in err.lower()
    assert "local-only" in err.lower()


def test_with_local_fallback_auth_returns_default_and_hints_login(capsys):
    result = remote.with_local_fallback(_raise(RemoteAuthError("401")), default=None, action="push")
    assert result is None
    err = capsys.readouterr().err
    assert err.count("Contexer:") == 1
    assert "contexer login --team" in err


def test_with_local_fallback_generic_tool_error_warns_request_failed_not_unreachable(monkeypatch, capsys):
    """A reachable-but-refusing/failing tool call (e.g. bad input) must warn with an honest
    'request failed' message, not the misleading 'endpoint unreachable' wording - the cloud
    answered, it just couldn't complete the request."""
    monkeypatch.setattr(
        remote, "_call_tool",
        lambda *a, **k: _result(content=[_text("invalid input: content too long")], is_error=True),
    )
    store = RemoteStore("https://t/mcp", "tok")
    result = remote.with_local_fallback(
        lambda: store.push_decision(type="constraint", content="c", repo=None),
        default=None, action="share decision")
    assert result is None
    err = capsys.readouterr().err
    assert "request failed" in err
    assert "unreachable" not in err


def test_auth_and_unreachable_each_warn_once(capsys):
    remote.with_local_fallback(_raise(RemoteAuthError("a")), default=None, action="x")
    remote.with_local_fallback(_raise(RemoteUnavailableError("u")), default=None, action="y")
    remote.with_local_fallback(_raise(RemoteAuthError("a")), default=None, action="x")
    remote.with_local_fallback(_raise(RemoteUnavailableError("u")), default=None, action="y")
    assert capsys.readouterr().err.count("Contexer:") == 2  # one per category, not per call


def test_with_local_fallback_does_not_swallow_non_remote_errors():
    with pytest.raises(ValueError):
        remote.with_local_fallback(_raise(ValueError("real bug")), default=None, action="x")


def test_reset_rearms_the_warning(capsys):
    op = _raise(RemoteUnavailableError("down"))
    remote.with_local_fallback(op, default=None, action="x")
    remote.reset_degradation_warnings()
    remote.with_local_fallback(op, default=None, action="x")
    assert capsys.readouterr().err.count("Contexer:") == 2


def test_warn_once_dedups_by_key(capsys):
    remote.warn_once("first", key="k")
    remote.warn_once("second", key="k")
    remote.warn_once("third", key="other")
    err = capsys.readouterr().err
    assert "first" in err
    assert "second" not in err
    assert "third" in err
