"""Remote sync client: contexer's own MCP client to the Teams server.

Path B: contexer stays the local-first brain. When the user is authenticated to a Teams
plan, this RemoteStore is an ADDITIVE layer that pushes local decisions up and pulls team
context down. It is a Streamable-HTTP MCP client to the Teams ``/mcp`` endpoint (Bearer
auth), distinct from Claude Code's own MCP client. store.py / local capture is untouched.

Every network failure is raised as a typed ``RemoteStoreError`` so callers (the offline /
auth-failure degradation, C8) can catch it and fall back to local-only without a traceback
ever reaching the agent.
"""
from __future__ import annotations

import asyncio
import inspect
import re
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from contexer.config import Profile

T = TypeVar("T")

# push_decision returns a text message "Saved decision <id> to your personal context."
_SAVED_ID_RE = re.compile(r"Saved decision (\S+)")
_DEFAULT_TIMEOUT = 10.0


class RemoteStoreError(Exception):
    """Base for any RemoteStore failure. Callers catch this to degrade to local-only."""


class RemoteAuthError(RemoteStoreError):
    """The Teams endpoint rejected the token (HTTP 401/403)."""


class RemoteUnavailableError(RemoteStoreError):
    """The Teams endpoint was unreachable (network error, timeout, 5xx)."""


@dataclass(frozen=True)
class RemoteDecision:
    """One row from the Teams merged context. ``scope`` is provenance (personal|team)."""

    id: str
    type: str
    content: str
    rationale: str | None
    repo: str | None
    agent: str | None
    scope: str


@dataclass(frozen=True)
class RemoteContext:
    """A get_context response: decisions plus incremental-pull tombstones and the cursor."""

    decisions: list[RemoteDecision]
    deleted: list[str]
    cursor: str | None


class RemoteStore:
    """MCP client to the Teams sync endpoint. Construct directly or via ``from_profile``."""

    def __init__(self, endpoint: str, token: str, *, timeout: float = _DEFAULT_TIMEOUT,
                 profile: "Profile | None" = None) -> None:
        self._endpoint = endpoint
        self._token = token
        self._timeout = timeout
        # Carried only when built via from_profile — the reactive 401 refresh needs it to
        # re-resolve a fresh token. Direct construction (tests) leaves it None → no reactive path.
        self._profile = profile

    @classmethod
    def from_profile(cls, profile: Profile, *, timeout: float = _DEFAULT_TIMEOUT) -> "RemoteStore | None":
        """Build a RemoteStore for a team profile, or None when sync is not configured
        (local mode, or a missing endpoint/token). None = the caller stays local-only.

        `timeout` (seconds) overrides the default transport timeout - callers on a tighter
        latency budget (e.g. the SessionStart pull) pass a shorter one."""
        if profile.mode != "team" or not profile.endpoint:
            return None
        from contexer import auth
        token = auth.resolve_token(profile)  # OAuth (login) token, refreshed; else static config token
        if not token:
            return None
        return cls(profile.endpoint, token, timeout=timeout, profile=profile)

    # ── async-native core (#108) ─────────────────────────────────────────────────
    # The network path is async at its heart so an in-loop caller (server.share_decision)
    # can AWAIT it and therefore CANCEL it: on a deadline the awaitable is cancelled and the
    # cancellation propagates into httpx's async context managers, closing the socket. The
    # sync push_decision/get_context below are thin asyncio.run(...) shims over this same
    # core for off-loop callers (CLI, hooks — separate processes, no running loop), so there
    # is exactly ONE copy of the wire-serialization + error-mapping + refresh logic.

    async def apush_decision(self, *, type: str, content: str, repo: str | None,
                             rationale: str | None = None, agent: str | None = None,
                             confidence: int | None = None, evidence: list[str] | None = None,
                             source: str | None = None, decision_id: str | None = None) -> str:
        """Async core of :meth:`push_decision`. Awaits the transport (cancellable)."""
        args: dict = {"type": type, "content": content}
        if repo is not None:
            args["repo"] = repo
        if rationale is not None:
            args["rationale"] = rationale
        if agent is not None:
            args["agent"] = agent
        if confidence is not None:
            args["confidence"] = confidence
        if evidence is not None:
            args["evidence"] = evidence
        if source is not None:
            args["source"] = source
        if decision_id is not None:
            args["decisionId"] = decision_id
        result = await self._ainvoke("push_decision", args)
        text = _first_text(getattr(result, "content", None))
        match = _SAVED_ID_RE.search(text) if text else None
        return match.group(1) if match else ""

    async def aget_context(self, repo: str | None = None,
                           updated_since: str | None = None) -> RemoteContext:
        """Async core of :meth:`get_context`. Awaits the transport (cancellable)."""
        args: dict = {}
        if repo is not None:
            args["repo"] = repo
        if updated_since is not None:
            args["updatedSince"] = updated_since
        result = await self._ainvoke("get_context", args)
        structured = getattr(result, "structuredContent", None) or {}
        rows = structured.get("result") or []
        decisions = [
            RemoteDecision(
                id=str(row.get("id", "")),
                type=row.get("type", ""),
                content=row.get("content", ""),
                rationale=row.get("rationale"),
                repo=row.get("repo"),
                agent=row.get("agent"),
                scope=row.get("scope", ""),
            )
            for row in rows
        ]
        return RemoteContext(
            decisions=decisions,
            deleted=list(structured.get("deleted") or []),
            cursor=structured.get("cursor"),
        )

    def push_decision(self, *, type: str, content: str, repo: str | None,
                      rationale: str | None = None, agent: str | None = None,
                      confidence: int | None = None, evidence: list[str] | None = None,
                      source: str | None = None, decision_id: str | None = None) -> str:
        """Push one local decision to the caller's personal Teams context (sync shim).

        Returns the server decision id (best-effort; ``""`` if the response carries none).
        Idempotent on ``decision_id`` server-side. Raises RemoteStoreError on failure. For
        off-loop callers only — an in-loop caller must await :meth:`apush_decision` instead
        (asyncio.run cannot run inside a running event loop)."""
        return asyncio.run(self.apush_decision(
            type=type, content=content, repo=repo, rationale=rationale, agent=agent,
            confidence=confidence, evidence=evidence, source=source, decision_id=decision_id))

    def get_context(self, repo: str | None = None,
                    updated_since: str | None = None) -> RemoteContext:
        """Pull merged team context (sync shim over :meth:`aget_context`).

        ``updated_since`` (an ISO cursor, typically a prior ``RemoteContext.cursor``) fetches
        only changes after it and populates ``deleted`` (tombstoned ids to remove locally).
        Raises RemoteStoreError on failure. Off-loop callers only."""
        return asyncio.run(self.aget_context(repo=repo, updated_since=updated_since))

    async def _ainvoke(self, name: str, arguments: dict, *, _refreshed: bool = False):
        """Call one Teams tool, mapping any transport failure to a typed RemoteStoreError.

        On a transport 401/403 (an expired/rejected bearer), attempt exactly ONE token
        refresh + retry: the access token may have expired mid-session (the token is frozen
        at construction). This is bounded — `_refreshed` gates it so we never loop, and it
        only fires for the transport-auth path, not for a server-side authz/scope denial
        (which a refresh can't fix). If refresh yields no *new* token, the original auth
        error surfaces (→ with_local_fallback's "run contexer login").

        ``except Exception`` deliberately does NOT catch ``asyncio.CancelledError`` (a
        BaseException): a cancelled push must propagate and tear the transport down, not be
        reclassified as an unreachable endpoint. The refresh is a rare, blocking network call
        (auth), so it runs off the loop via ``to_thread`` to keep the loop responsive."""
        try:
            # In production `_acall_tool` is a coroutine → this awaits the real async transport
            # (cancellable). Unit tests patch this seam with a plain sync fake returning a
            # CallToolResult; tolerate that by only awaiting an actual awaitable, so tests need
            # no coroutine wrapper while prod keeps its cancellable await.
            result = _acall_tool(self._endpoint, self._token, name, arguments, self._timeout)
            if inspect.isawaitable(result):
                result = await result
        except RemoteStoreError:
            raise
        except Exception as exc:  # network / transport / anyio group -> typed, catchable
            err = _classify(exc)
            if (isinstance(err, RemoteAuthError) and not _refreshed
                    and await asyncio.to_thread(self._refresh_token)):
                return await self._ainvoke(name, arguments, _refreshed=True)
            raise err from exc
        if getattr(result, "isError", False):
            message = _first_text(getattr(result, "content", None)) or f"{name} failed"
            raise _classify_tool_error(message)
        return result

    def _refresh_token(self) -> bool:
        """Reactively refresh the bearer after a 401. Returns True (and swaps self._token)
        only when a genuinely *new* token was obtained — so the caller retries at most once
        and a stale/unchanged token doesn't trigger a pointless retry. Never raises."""
        if self._profile is None:
            return False
        from contexer import auth
        new = auth.refresh_now(self._profile)
        if new and new != self._token:
            self._token = new
            return True
        return False


# ── Offline / auth-failure degradation (C8) ──────────────────────────────────────
# contexer is local-first: the cloud being unreachable or rejecting the token must NEVER
# block local capture or local reads. Callers (C4 share, C5 pull, C7 poll) run their
# RemoteStore calls through with_local_fallback so a cloud failure degrades to local-only
# with a single, clear warning instead of a traceback reaching the agent or a hook.

_WARNED: set[str] = set()


def reset_degradation_warnings() -> None:
    """Clear the process-wide warn-once state (a fresh session; test isolation)."""
    _WARNED.clear()


def warn_once(message: str, *, key: str) -> None:
    """Print `message` to stderr at most once per process per `key` (no per-call spam).

    Uses stderr prints (contexer's convention — it has no logging module) so the warning
    is visible to the developer without ever being injected into the agent's context."""
    if key in _WARNED:
        return
    _WARNED.add(key)
    print(message, file=sys.stderr)


def _warn_degrade(exc: RemoteStoreError, action: str) -> None:
    """Emit the warn-once line matching a RemoteStoreError's category. Shared by the sync and
    async fallback wrappers so their degradation messages can never drift apart."""
    if isinstance(exc, RemoteAuthError):
        warn_once(
            f"Contexer: Teams authentication failed while trying to {action} - "
            "continuing local-only (run 'contexer login --team' or check your token).",
            key="degrade:auth",
        )
    elif isinstance(exc, RemoteUnavailableError):
        warn_once(
            f"Contexer: Teams endpoint unreachable while trying to {action} - "
            "continuing local-only.",
            key="degrade:unreachable",
        )
    else:
        # The cloud was reached and answered, it just refused or failed the request (e.g. a
        # validation error) - distinct from the transport-level "unreachable" case above, so the
        # warning doesn't misreport a reachable-but-failing cloud as a network outage.
        warn_once(
            f"Contexer: Teams request failed while trying to {action} - "
            "continuing local-only.",
            key="degrade:request",
        )


def with_local_fallback(op: Callable[[], T], *, default: T, action: str) -> T:
    """Run a RemoteStore operation, degrading to local-only on any RemoteStoreError.

    Returns `default` (and warns once per failure category) when the cloud is unreachable
    or rejects the token — a cloud failure MUST NEVER block local capture or local reads,
    so no RemoteStoreError bubbles out. Non-RemoteStoreError exceptions (real bugs) are NOT
    swallowed; they propagate. `action` is a short phrase ("pull team context") used in the
    warning so the developer knows what degraded."""
    try:
        return op()
    except RemoteStoreError as exc:
        _warn_degrade(exc, action)
        return default


async def awith_local_fallback(op: Callable[[], Awaitable[T]], *, default: T, action: str) -> T:
    """Async twin of :func:`with_local_fallback` for in-loop callers (server.share_decision).

    ``op`` is a zero-arg callable returning an awaitable; it is ``await``-ed so a wedged push
    stays cancellable. Same local-first contract and identical warn-once messages (via the
    shared ``_warn_degrade``). A CancelledError is a BaseException, so an outer deadline
    cancels straight through this wrapper — it is not swallowed as a degradation."""
    try:
        return await op()
    except RemoteStoreError as exc:
        _warn_degrade(exc, action)
        return default


def _first_text(content) -> str:
    """First text payload from an MCP content list (an SDK content item or a plain dict)."""
    for item in content or []:
        text = getattr(item, "text", None)
        if text is None and isinstance(item, dict):
            text = item.get("text")
        if text:
            return text
    return ""


def _extract_status(exc: BaseException) -> int | None:
    """HTTP status from an httpx-style error, recursing into ExceptionGroup children."""
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status, int):
        return status
    for sub in getattr(exc, "exceptions", ()) or ():
        found = _extract_status(sub)
        if found is not None:
            return found
    return None


def _classify(exc: BaseException) -> RemoteStoreError:
    """Map a raw transport exception to a typed RemoteStoreError."""
    status = _extract_status(exc)
    if status in (401, 403):
        return RemoteAuthError(f"Teams rejected the token (HTTP {status}).")
    return RemoteUnavailableError(f"Teams endpoint unreachable: {exc}")


# A tool call that returns isError with one of these signals is an AUTHORIZATION failure
# (insufficient scope, forbidden, permission denied) - the cloud was reached and answered, it
# just refused the action. It must NOT be reported as "endpoint unreachable": the remedy is to
# re-authenticate, so it is raised as RemoteAuthError and surfaced via the auth degradation path.
# The scope arms are phrase-level (not a bare `\bscope\b`) so a validation error that merely
# mentions a "scope" parameter (e.g. "Value for 'scope' parameter must be a string") is not
# misclassified as an auth failure - only genuine authorization denials match.
_AUTHZ_ERROR_RE = re.compile(
    r"\b(?:forbidden|unauthori[sz]ed|permission|not allowed"
    r"|lacks the .{0,40} scope|insufficient[ _-]scope|scope required)\b",
    re.I,
)


def _classify_tool_error(message: str) -> RemoteStoreError:
    """Map a server-returned tool error (an isError result's text) to a typed RemoteStoreError.

    An insufficient-scope / permission message is an authorization failure (RemoteAuthError), not
    a transport outage - so with_local_fallback tells the user to re-authenticate instead of
    misreporting a reachable-but-refusing cloud as unreachable."""
    if _AUTHZ_ERROR_RE.search(message):
        return RemoteAuthError(message)
    return RemoteStoreError(message)


async def _acall_tool(endpoint: str, token: str, name: str, arguments: dict, timeout: float):  # pragma: no cover - real network I/O, exercised by the opt-in integration test
    """Network seam: open a Streamable-HTTP MCP session, call one tool, return the result.

    The single async boundary the whole client funnels through (both the async core and the
    sync asyncio.run shims). Awaiting it is what makes a wedged call cancellable — cancellation
    unwinds these ``async with`` blocks, closing the transport."""
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"}
    async with streamablehttp_client(endpoint, headers=headers, timeout=timeout) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return await session.call_tool(name, arguments)
