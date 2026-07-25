"""Tests for the RemoteStore sync client (contexer/remote.py).

Unit-level: the network seam `remote._acall_tool` is monkeypatched so these tests assert
the wire serialization (snake->camel, None-omitted), result parsing, and error mapping
without touching a real Teams server. The real transport is covered by the opt-in
integration test documented in the manual steps.
"""
import asyncio
import inspect
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


def _aseam(body):
    """Wrap a sync body as the async `_acall_tool` seam (the network boundary the sync
    shims and the async core both funnel through). Calling `body` raises → the coroutine
    raises on await, exactly like the real transport."""
    async def _acall(endpoint, token, name, arguments, timeout):
        return body(endpoint, token, name, arguments, timeout)
    return _acall


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


def test_from_profile_default_timeout():
    rs = RemoteStore.from_profile(Profile(mode="team", endpoint="https://t/mcp", token="tok"))
    assert rs._timeout == remote._DEFAULT_TIMEOUT


def test_from_profile_accepts_timeout_override():
    rs = RemoteStore.from_profile(
        Profile(mode="team", endpoint="https://t/mcp", token="tok"), timeout=3.0)
    assert rs._timeout == 3.0


# ── push_decision serialization + id parsing ─────────────────────────────────────

def test_push_decision_serializes_full_wire_shape(monkeypatch):
    captured = {}

    async def fake_call(endpoint, token, name, arguments, timeout):
        captured.update(endpoint=endpoint, token=token, name=name, args=arguments, timeout=timeout)
        return _result(content=[_text("Saved decision dec-42 to your personal context.")])

    monkeypatch.setattr(remote, "_acall_tool", fake_call)
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
        remote, "_acall_tool",
        lambda e, t, n, args, to: captured.update(args=args)
        or _result(content=[_text("Saved decision x to your personal context.")]),
    )
    RemoteStore("https://t/mcp", "tok").push_decision(type="constraint", content="c", repo=None)
    assert captured["args"] == {"type": "constraint", "content": "c"}


def test_push_decision_threads_title_to_wire(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        remote, "_acall_tool",
        lambda e, t, n, args, to: captured.update(args=args)
        or _result(content=[_text("Saved decision x to your personal context.")]),
    )
    RemoteStore("https://t/mcp", "tok").push_decision(
        type="constraint", content="c", repo=None, title="A short heading")
    assert captured["args"]["title"] == "A short heading"


def test_push_decision_returns_empty_when_no_id_in_message(monkeypatch):
    monkeypatch.setattr(remote, "_acall_tool", lambda *a, **k: _result(content=[_text("ok")]))
    did = RemoteStore("https://t/mcp", "tok").push_decision(type="convention", content="c", repo=None)
    assert did == ""


# ── push_decisions (batch) serialization + parsing ───────────────────────────────

def test_push_decisions_serializes_batch_and_parses_ids(monkeypatch):
    captured = {}
    monkeypatch.setattr(remote, "_acall_tool", _aseam(
        lambda e, t, n, args, to: captured.update(name=n, args=args) or _result(structured={
            "results": [{"decisionId": "local-1", "id": "srv-a"},
                        {"decisionId": "local-2", "id": "srv-b"}],
            "skipped": [],
        })))
    saved, skipped = RemoteStore("https://t/mcp", "tok").push_decisions([
        {"type": "architecture", "content": "a", "decision_id": "local-1"},
        {"type": "constraint", "content": "b", "repo": "r", "decision_id": "local-2"},
    ])
    assert captured["name"] == "push_decisions"
    # kwargs are serialized to the wire shape (decision_id -> decisionId, None omitted) inside one call.
    assert captured["args"] == {"decisions": [
        {"type": "architecture", "content": "a", "decisionId": "local-1"},
        {"type": "constraint", "content": "b", "repo": "r", "decisionId": "local-2"},
    ]}
    assert saved == ["srv-a", "srv-b"]
    assert skipped == []


def test_push_decisions_batch_threads_title_per_row(monkeypatch):
    # The batch path spreads **kw into _wire_args per row — a "title" key in the kwargs
    # dict must flow onto the wire like any other optional.
    captured = {}
    monkeypatch.setattr(remote, "_acall_tool", _aseam(
        lambda e, t, n, args, to: captured.update(args=args) or _result(structured={
            "results": [{"decisionId": "local-1", "id": "srv-a"}], "skipped": [],
        })))
    RemoteStore("https://t/mcp", "tok").push_decisions([
        {"type": "architecture", "content": "a", "decision_id": "local-1", "title": "Row title"},
    ])
    assert captured["args"]["decisions"][0]["title"] == "Row title"


def test_push_decisions_parses_skipped_capacity_rows(monkeypatch):
    monkeypatch.setattr(remote, "_acall_tool", _aseam(lambda *a: _result(structured={
        "results": [{"decisionId": "local-1", "id": "srv-a"}],
        "skipped": [{"decisionId": "local-2", "reason": "quota_exceeded"}],
    })))
    saved, skipped = RemoteStore("https://t/mcp", "tok").push_decisions([
        {"type": "architecture", "content": "a", "decision_id": "local-1"},
        {"type": "constraint", "content": "b", "decision_id": "local-2"},
    ])
    assert saved == ["srv-a"]
    assert skipped == [{"decision_id": "local-2", "reason": "quota_exceeded"}]


def test_push_decisions_raises_when_submitted_id_unaccounted(monkeypatch):
    # A successful response that omits a submitted decisionId (neither saved nor skipped) must
    # NOT be treated as done - raise so the caller keeps it queued (Greptile remote.py#149).
    monkeypatch.setattr(remote, "_acall_tool", _aseam(lambda *a: _result(structured={
        "results": [{"decisionId": "local-1", "id": "srv-a"}], "skipped": [],
    })))
    with pytest.raises(RemoteStoreError):
        RemoteStore("https://t/mcp", "tok").push_decisions([
            {"type": "architecture", "content": "a", "decision_id": "local-1"},
            {"type": "constraint", "content": "b", "decision_id": "local-2"},  # dropped by server
        ])


def test_push_decisions_missing_structured_raises_when_ids_submitted(monkeypatch):
    # Empty/missing structured content with submitted ids -> nothing accounted -> raise (queued).
    monkeypatch.setattr(remote, "_acall_tool", _aseam(lambda *a: _result(structured=None)))
    with pytest.raises(RemoteStoreError):
        RemoteStore("https://t/mcp", "tok").push_decisions([{"type": "constraint", "content": "c", "decision_id": "d1"}])


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

    async def fake_call(endpoint, token, name, arguments, timeout):
        captured.update(name=name, args=arguments)
        return _result(structured=structured)

    monkeypatch.setattr(remote, "_acall_tool", fake_call)
    ctx = RemoteStore("https://t/mcp", "tok").get_context(
        repo="r", updated_since="2025-12-31T00:00:00Z",
    )
    assert captured["name"] == "get_context"
    assert captured["args"] == {"repo": "r", "updatedSince": "2025-12-31T00:00:00Z"}
    assert isinstance(ctx, RemoteContext)
    assert ctx.deleted == ["9"]
    assert ctx.cursor == "2026-01-01T00:00:00Z"
    assert ctx.decisions == [
        RemoteDecision(id="1", type="constraint", title=None, content="c", rationale=None,
                       repo="r", agent="a", scope="team"),
    ]


# ── RemoteDecision.title parsing (Decision Titles v2, Task 5) ───────────────────

def test_get_context_parses_title_from_row(monkeypatch):
    structured = {
        "result": [{
            "id": "1", "type": "constraint", "title": "A short heading", "content": "c",
            "rationale": None, "repo": "r", "agent": "a", "scope": "team",
        }],
        "deleted": [], "cursor": None,
    }
    monkeypatch.setattr(remote, "_acall_tool", lambda *a, **k: _result(structured=structured))
    ctx = RemoteStore("https://t/mcp", "tok").get_context()
    assert ctx.decisions[0].title == "A short heading"


def test_get_context_title_none_when_row_lacks_key(monkeypatch):
    # Older server / pre-title row: no "title" key in the row at all -> RemoteDecision.title
    # is None (never a KeyError, never "").
    structured = {
        "result": [{
            "id": "1", "type": "constraint", "content": "c",
            "rationale": None, "repo": "r", "agent": "a", "scope": "team",
        }],
        "deleted": [], "cursor": None,
    }
    monkeypatch.setattr(remote, "_acall_tool", lambda *a, **k: _result(structured=structured))
    ctx = RemoteStore("https://t/mcp", "tok").get_context()
    assert ctx.decisions[0].title is None


def test_get_context_omits_none_args(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        remote, "_acall_tool",
        lambda e, t, n, args, to: captured.update(args=args)
        or _result(structured={"result": [], "deleted": [], "cursor": None}),
    )
    ctx = RemoteStore("https://t/mcp", "tok").get_context()
    assert captured["args"] == {}
    assert ctx.decisions == []
    assert ctx.deleted == []
    assert ctx.cursor is None


def test_get_context_missing_structured_yields_empty(monkeypatch):
    monkeypatch.setattr(remote, "_acall_tool", lambda *a, **k: _result(structured=None))
    ctx = RemoteStore("https://t/mcp", "tok").get_context()
    assert ctx.decisions == []
    assert ctx.deleted == []
    assert ctx.cursor is None


# ── error mapping (feeds C8 degradation) ─────────────────────────────────────────

def test_connection_error_maps_to_unavailable(monkeypatch):
    async def boom(*a, **k):
        raise ConnectionError("connection refused")

    monkeypatch.setattr(remote, "_acall_tool", boom)
    with pytest.raises(RemoteUnavailableError):
        RemoteStore("https://t/mcp", "tok").get_context()


@pytest.mark.parametrize("status", [401, 403])
def test_http_auth_status_maps_to_auth_error(monkeypatch, status):
    monkeypatch.setattr(remote, "_acall_tool", lambda *a, **k: (_ for _ in ()).throw(_http_error(status)))
    with pytest.raises(RemoteAuthError):
        RemoteStore("https://t/mcp", "tok").get_context()


def test_http_500_maps_to_unavailable(monkeypatch):
    monkeypatch.setattr(remote, "_acall_tool", lambda *a, **k: (_ for _ in ()).throw(_http_error(500)))
    with pytest.raises(RemoteUnavailableError):
        RemoteStore("https://t/mcp", "tok").get_context()


def test_exception_group_401_maps_to_auth_error(monkeypatch):
    async def boom(*a, **k):
        raise ExceptionGroup("transport", [_http_error(401)])

    monkeypatch.setattr(remote, "_acall_tool", boom)
    with pytest.raises(RemoteAuthError):
        RemoteStore("https://t/mcp", "tok").push_decision(type="constraint", content="c", repo=None)


def test_tool_error_result_raises(monkeypatch):
    monkeypatch.setattr(
        remote, "_acall_tool",
        lambda *a, **k: _result(content=[_text("invalid input")], is_error=True),
    )
    with pytest.raises(RemoteStoreError):
        RemoteStore("https://t/mcp", "tok").push_decision(type="constraint", content="c", repo=None)


def test_tool_error_with_empty_content_uses_fallback_message(monkeypatch):
    monkeypatch.setattr(remote, "_acall_tool", lambda *a, **k: _result(content=[], is_error=True))
    with pytest.raises(RemoteStoreError, match="get_context failed"):
        RemoteStore("https://t/mcp", "tok").get_context()


def test_invoke_reraises_typed_error_unwrapped(monkeypatch):
    async def boom(*a, **k):
        raise RemoteAuthError("already typed")

    monkeypatch.setattr(remote, "_acall_tool", boom)
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
        remote, "_acall_tool",
        lambda *a, **k: _result(content=[_text(message)], is_error=True),
    )
    with pytest.raises(RemoteAuthError):
        RemoteStore("https://t/mcp", "tok").push_decision(type="constraint", content="c", repo=None)


def test_generic_tool_error_is_not_auth_error(monkeypatch):
    """A non-authorization tool error (e.g. bad input) stays a plain RemoteStoreError so the
    scope-error classifier doesn't over-broaden into every failure."""
    monkeypatch.setattr(
        remote, "_acall_tool",
        lambda *a, **k: _result(content=[_text("invalid input: content too long")], is_error=True),
    )
    with pytest.raises(RemoteStoreError) as exc:
        RemoteStore("https://t/mcp", "tok").push_decision(type="constraint", content="c", repo=None)
    assert not isinstance(exc.value, RemoteAuthError)


def test_scope_mentioning_validation_error_is_not_auth_error(monkeypatch):
    """A validation error that merely mentions a 'scope' parameter must NOT be misclassified as
    an authorization failure - the phrase-level regex only matches genuine auth denials."""
    monkeypatch.setattr(
        remote, "_acall_tool",
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
        remote, "_acall_tool",
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


# ── reactive 401 refresh + bounded one-retry ─────────────────────────────────────

def _seq_call(*outcomes):
    """A fake async `_acall_tool` that yields `outcomes` in order (raise if an Exception).

    Records the (endpoint, token, ...) args of each call on `.calls` so tests can assert
    the retry used the refreshed token and how many times the transport was hit."""
    it = iter(outcomes)

    async def fake(*args, **kwargs):
        fake.calls.append(args)
        out = next(it)
        if isinstance(out, Exception):
            raise out
        return out

    fake.calls = []
    return fake


def _team_store():
    """A RemoteStore carrying a Profile (as from_profile builds it) so the reactive path is live."""
    prof = Profile(mode="team", endpoint="https://t/mcp", token="tok")
    return RemoteStore("https://t/mcp", "tok", profile=prof)


def test_401_triggers_one_refresh_and_retry(monkeypatch):
    """An expired bearer mid-session: one refresh + one retry, transparently succeeding."""
    fake = _seq_call(_http_error(401), _result(structured={"result": []}))
    monkeypatch.setattr(remote, "_acall_tool", fake)
    calls = []
    monkeypatch.setattr("contexer.auth.refresh_now", lambda p: calls.append(p) or "new-tok")

    store = _team_store()
    ctx = store.get_context()

    assert isinstance(ctx, RemoteContext)
    assert store._token == "new-tok"        # swapped in
    assert len(calls) == 1                   # exactly one refresh
    assert len(fake.calls) == 2              # original + one retry
    assert fake.calls[1][1] == "new-tok"     # retry used the refreshed token


def test_401_without_profile_does_not_retry(monkeypatch):
    """Direct construction (no Profile) keeps the old behavior: 401 → RemoteAuthError, no refresh."""
    fake = _seq_call(_http_error(401))
    monkeypatch.setattr(remote, "_acall_tool", fake)
    monkeypatch.setattr("contexer.auth.refresh_now", lambda p: pytest.fail("must not refresh without a profile"))
    with pytest.raises(RemoteAuthError):
        RemoteStore("https://t/mcp", "tok").get_context()
    assert len(fake.calls) == 1


def test_401_refresh_yields_no_new_token_surfaces(monkeypatch):
    """If refresh can't produce a *new* token (same/None), surface the auth error — no retry."""
    fake = _seq_call(_http_error(401))
    monkeypatch.setattr(remote, "_acall_tool", fake)
    monkeypatch.setattr("contexer.auth.refresh_now", lambda p: None)
    with pytest.raises(RemoteAuthError):
        _team_store().get_context()
    assert len(fake.calls) == 1              # no retry


def test_401_retry_still_401_surfaces(monkeypatch):
    """A genuinely dead token: refresh returns something new, retry still 401 → surface, no loop."""
    fake = _seq_call(_http_error(401), _http_error(401))
    monkeypatch.setattr(remote, "_acall_tool", fake)
    monkeypatch.setattr("contexer.auth.refresh_now", lambda p: "new-tok")
    with pytest.raises(RemoteAuthError):
        _team_store().get_context()
    assert len(fake.calls) == 2              # exactly one retry, then stop (bounded)


def test_authz_tool_error_is_not_reactively_refreshed(monkeypatch):
    """A server-side authz/scope denial (isError result) is NOT a transport 401 — a refresh can't
    fix it, so refresh_now must never be called for it."""
    monkeypatch.setattr(
        remote, "_acall_tool",
        lambda *a, **k: _result(content=[_text("This token lacks the 'write' scope required.")], is_error=True),
    )
    monkeypatch.setattr("contexer.auth.refresh_now", lambda p: pytest.fail("authz denial must not refresh"))
    with pytest.raises(RemoteAuthError):
        _team_store().push_decision(type="constraint", content="c", repo=None)


# ── content parsing edge (SDK returns dict content items in some transports) ──────

def test_push_decision_reads_dict_content_item(monkeypatch):
    monkeypatch.setattr(
        remote, "_acall_tool",
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


def test_with_local_fallback_refusal_warns_with_reason_not_unreachable(monkeypatch, capsys):
    """A reachable-but-refusing tool call (bad input, rate limit, ...) must warn with the SERVER's
    actual reason - not the misleading 'endpoint unreachable' wording - the cloud answered, it just
    refused the request."""
    monkeypatch.setattr(
        remote, "_acall_tool",
        lambda *a, **k: _result(content=[_text("invalid input: content too long")], is_error=True),
    )
    store = RemoteStore("https://t/mcp", "tok")
    result = remote.with_local_fallback(
        lambda: store.push_decision(type="constraint", content="c", repo=None),
        default=None, action="share decision")
    assert result is None
    err = capsys.readouterr().err
    assert "refused the request" in err
    assert "invalid input: content too long" in err  # the server's actual reason is surfaced
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


# ── #108: async-native core (cancellable push path) ──────────────────────────────
# The network path is async at its core: `_ainvoke` awaits `_acall_tool` directly, and
# `apush_decision`/`aget_context` are its async front doors. The sync `push_decision`/
# `get_context` are thin `asyncio.run(...)` shims over that ONE core (off-loop callers:
# CLI, hooks). The payoff over the old thread-offload: an awaited call is CANCELLABLE, so a
# wedged transport is torn down at the deadline instead of leaking a worker + open socket.

def test_async_core_methods_are_coroutines():
    assert inspect.iscoroutinefunction(RemoteStore.apush_decision)
    assert inspect.iscoroutinefunction(RemoteStore.aget_context)
    assert inspect.iscoroutinefunction(RemoteStore._ainvoke)


def test_apush_decision_awaits_acall_tool_and_parses(monkeypatch):
    captured = {}

    async def fake(endpoint, token, name, arguments, timeout):
        captured.update(name=name, args=arguments, timeout=timeout, token=token)
        return _result(content=[_text("Saved decision dec-99 to your personal context.")])

    monkeypatch.setattr(remote, "_acall_tool", fake)
    rs = RemoteStore("https://t/mcp", "tok", timeout=5.0)
    did = asyncio.run(rs.apush_decision(type="architecture", content="use X", repo="r"))
    assert did == "dec-99"
    assert captured["name"] == "push_decision"
    assert captured["args"] == {"type": "architecture", "content": "use X", "repo": "r"}
    assert captured["timeout"] == 5.0
    assert captured["token"] == "tok"


def test_aget_context_awaits_acall_tool_and_parses(monkeypatch):
    structured = {"result": [{"id": "1", "type": "constraint", "content": "c", "rationale": None,
                              "repo": "r", "agent": "a", "scope": "team"}],
                  "deleted": ["9"], "cursor": "2026-01-01T00:00:00Z"}
    monkeypatch.setattr(remote, "_acall_tool", _aseam(lambda *a: _result(structured=structured)))
    ctx = asyncio.run(RemoteStore("https://t/mcp", "tok").aget_context(repo="r"))
    assert isinstance(ctx, RemoteContext)
    assert ctx.deleted == ["9"]
    assert ctx.decisions[0].content == "c"


def test_sync_push_decision_is_thin_shim_over_async_core(monkeypatch):
    """The sync shim drives the SAME async seam via asyncio.run — one network core, no
    duplicated logic. Off-loop callers (CLI, hooks) keep working unchanged."""
    monkeypatch.setattr(remote, "_acall_tool", _aseam(
        lambda *a: _result(content=[_text("Saved decision d-1 to your personal context.")])))
    did = RemoteStore("https://t/mcp", "tok").push_decision(type="constraint", content="c", repo=None)
    assert did == "d-1"


def test_sync_get_context_is_thin_shim_over_async_core(monkeypatch):
    monkeypatch.setattr(remote, "_acall_tool", _aseam(
        lambda *a: _result(structured={"result": [], "deleted": [], "cursor": None})))
    ctx = RemoteStore("https://t/mcp", "tok").get_context()
    assert ctx.decisions == [] and ctx.cursor is None


def test_async_push_cancellation_tears_down_transport(monkeypatch):
    """THE #108 fix. A wedged push, once its deadline fires, is CANCELLED — and the
    cancellation reaches INTO the transport so its async context managers close the
    connection. Asserts the transport's teardown actually ran (socket reclaimed), not
    merely that the caller returned while a thread ran on."""
    torn_down = {"v": False}

    async def wedged(endpoint, token, name, arguments, timeout):
        try:
            await asyncio.sleep(3600)  # a remote that holds the connection open forever
        except asyncio.CancelledError:
            torn_down["v"] = True      # real httpx would close read/write + session here
            raise

    monkeypatch.setattr(remote, "_acall_tool", wedged)
    rs = RemoteStore("https://t/mcp", "tok")

    async def driver():
        async with asyncio.timeout(0.05):
            await rs.apush_decision(type="constraint", content="c", repo=None)

    with pytest.raises(TimeoutError):
        asyncio.run(driver())
    assert torn_down["v"] is True  # cancellation propagated to the transport — reclaimed, not leaked


def test_ainvoke_does_not_swallow_cancellation(monkeypatch):
    """`_ainvoke`'s broad `except Exception` must NEVER catch a CancelledError (it is a
    BaseException) and misclassify it as an unreachable-endpoint error — that would defeat
    the whole cancellable-transport fix."""
    async def wedged(*a, **k):
        await asyncio.sleep(3600)

    monkeypatch.setattr(remote, "_acall_tool", wedged)
    rs = RemoteStore("https://t/mcp", "tok")

    async def driver():
        task = asyncio.ensure_future(rs._ainvoke("push_decision", {"type": "c", "content": "x"}))
        await asyncio.sleep(0.01)
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(driver())


def test_async_connection_error_maps_to_unavailable(monkeypatch):
    monkeypatch.setattr(remote, "_acall_tool", _aseam(
        lambda *a: (_ for _ in ()).throw(ConnectionError("refused"))))
    with pytest.raises(RemoteUnavailableError):
        asyncio.run(RemoteStore("https://t/mcp", "tok").aget_context())


def test_async_core_surfaces_401_without_refreshing(monkeypatch):
    """#108: the async core does NO reactive refresh — it surfaces the transport 401 as a
    RemoteAuthError so it stays fully cancellable (no uncancellable refresh thread). Reactive
    refresh is the sync shim's job. So `contexer.auth.refresh_now` must never be called here."""
    fake = _seq_call(_http_error(401), _result(structured={"result": []}))
    monkeypatch.setattr(remote, "_acall_tool", fake)
    monkeypatch.setattr("contexer.auth.refresh_now",
                        lambda p: pytest.fail("async core must not refresh — that spawns a thread"))
    store = _team_store()
    with pytest.raises(RemoteAuthError):
        asyncio.run(store.aget_context())
    assert len(fake.calls) == 1  # surfaced immediately, no retry


def test_sync_shim_reactive_refresh_only_for_transport_auth(monkeypatch):
    """The sync shim refreshes on a transport 401 (tagged) but NOT on a server-side authz
    denial (untagged RemoteAuthError from _classify_tool_error) — a refresh can't fix scope."""
    # authz denial: isError result -> RemoteAuthError WITHOUT _transport_auth -> no refresh
    monkeypatch.setattr(remote, "_acall_tool", lambda *a, **k: _result(
        content=[_text("This token lacks the 'write' scope required.")], is_error=True))
    monkeypatch.setattr("contexer.auth.refresh_now",
                        lambda p: pytest.fail("authz denial must not refresh"))
    with pytest.raises(RemoteAuthError):
        _team_store().push_decision(type="constraint", content="c", repo=None)


# ── outbound secret redaction (remote._wire_args — the hard egress guarantee) ──

_WIRE_AWS = "AKIAIOSFODNN7EXAMPLE"
_WIRE_GH = "ghp_" + "b" * 36


def test_wire_args_redacts_content_evidence_rationale():
    args = remote._wire_args(
        type="architecture",
        content=f"deploy with {_WIRE_AWS}",
        evidence=[f"seen token {_WIRE_GH}", "plain evidence"],
        rationale=f"chosen because api_key = \"{_WIRE_AWS}\"",
    )
    assert _WIRE_AWS not in args["content"]
    assert "[REDACTED:aws_key]" in args["content"]
    assert all(_WIRE_GH not in e for e in args["evidence"])
    assert "plain evidence" in args["evidence"]  # non-secret evidence preserved
    assert _WIRE_AWS not in args["rationale"]


def test_wire_args_redacts_legacy_content_bypassing_capture():
    # A decision stored before redaction shipped: raw secret arrives at the wire and
    # must still be scrubbed — this is the only protection for legacy on-disk secrets.
    args = remote._wire_args(type="constraint", content=f"legacy key {_WIRE_AWS}")
    assert _WIRE_AWS not in args["content"]


def test_wire_args_respects_opt_out(monkeypatch):
    monkeypatch.setattr(remote, "_redaction_enabled", lambda: False)
    args = remote._wire_args(type="constraint", content=f"key {_WIRE_AWS}")
    assert _WIRE_AWS in args["content"]  # user opted out of redaction entirely


def test_wire_args_no_evidence_key_when_none():
    args = remote._wire_args(type="constraint", content="plain", evidence=None)
    assert "evidence" not in args  # None optional still omitted after redaction wiring


# ── title on the wire (Decision Titles v2, Task 4) ─────────────────────────────

def test_wire_args_omits_title_when_none():
    args = remote._wire_args(type="constraint", content="plain", title=None)
    assert "title" not in args  # server reads an absent key as NULL


def test_wire_args_includes_title_when_given():
    args = remote._wire_args(type="constraint", content="plain", title="A short heading")
    assert args["title"] == "A short heading"


def test_wire_args_redacts_title_secret():
    # SECURITY: a title is derived from content and can carry the same secrets — this is
    # the last-mile chokepoint, independent of any scrubbing already done upstream (e.g.
    # store._share_projection).
    args = remote._wire_args(type="architecture", content="plain body",
                             title=f"Prod key is {_WIRE_AWS}")
    assert _WIRE_AWS not in args["title"]
    assert "[REDACTED:aws_key]" in args["title"]


def test_wire_args_title_respects_opt_out(monkeypatch):
    monkeypatch.setattr(remote, "_redaction_enabled", lambda: False)
    args = remote._wire_args(type="constraint", content="plain", title=f"key {_WIRE_AWS}")
    assert _WIRE_AWS in args["title"]  # user opted out of redaction entirely


def test_wire_args_redact_param_overrides_config():
    on = remote._wire_args(type="c", content=f"key {_WIRE_AWS}", redact_on=True)
    off = remote._wire_args(type="c", content=f"key {_WIRE_AWS}", redact_on=False)
    assert _WIRE_AWS not in on["content"]
    assert _WIRE_AWS in off["content"]  # caller-resolved flag wins over a config read


def test_wire_honors_store_profile_over_global(monkeypatch):
    # A RemoteStore built from a profile with redaction ON must redact even when the GLOBAL
    # config says OFF — otherwise an explicit opt-in caller silently leaks raw secrets.
    monkeypatch.setattr(remote, "_redaction_enabled", lambda: False)  # global OFF
    captured = {}
    monkeypatch.setattr(remote, "_acall_tool",
                        lambda e, t, n, args, to: captured.update(args=args)
                        or _result(content=[_text("Saved decision d1 to your personal context.")]))
    prof = Profile(mode="team", endpoint="https://t/mcp", token="tok", redact_secrets=True)
    rs = RemoteStore("https://t/mcp", "tok", profile=prof)
    rs.push_decision(type="constraint", content=f"key {_WIRE_AWS}", repo=None)
    assert _WIRE_AWS not in captured["args"]["content"]


def test_batch_honors_store_profile_over_global(monkeypatch):
    monkeypatch.setattr(remote, "_redaction_enabled", lambda: False)  # global OFF
    captured = {}
    monkeypatch.setattr(remote, "_acall_tool",
                        lambda e, t, n, args, to: captured.update(args=args)
                        or _result(structured={"results": [], "skipped": []}))
    prof = Profile(mode="team", endpoint="https://t/mcp", token="tok", redact_secrets=True)
    rs = RemoteStore("https://t/mcp", "tok", profile=prof)
    rs.push_decisions([{"type": "constraint", "content": f"k {_WIRE_AWS}", "repo": "github.com/a/b"}])
    assert _WIRE_AWS not in captured["args"]["decisions"][0]["content"]


def test_batch_push_reads_config_once(monkeypatch):
    # apush_decisions serializes N rows through _wire_args; the redaction flag must be resolved
    # ONCE for the batch, not re-read from config.toml per row.
    calls = {"n": 0}
    real = remote._redaction_enabled
    monkeypatch.setattr(remote, "_redaction_enabled",
                        lambda: (calls.__setitem__("n", calls["n"] + 1) or real()))
    monkeypatch.setattr(remote, "_acall_tool",
                        lambda e, t, n, args, to: _result(structured={"results": [], "skipped": []}))
    rs = RemoteStore("https://t/mcp", "tok")
    rows = [{"type": "constraint", "content": f"c{i} plain", "repo": "github.com/a/b"}
            for i in range(3)]
    rs.push_decisions(rows)
    assert calls["n"] <= 1
