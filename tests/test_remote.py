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


# ── content parsing edge (SDK returns dict content items in some transports) ──────

def test_push_decision_reads_dict_content_item(monkeypatch):
    monkeypatch.setattr(
        remote, "_call_tool",
        lambda *a, **k: _result(content=[{"type": "text", "text": "Saved decision dd-7 to your personal context."}]),
    )
    did = RemoteStore("https://t/mcp", "tok").push_decision(type="constraint", content="c", repo=None)
    assert did == "dd-7"
