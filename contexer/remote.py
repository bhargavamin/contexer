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

from contexer import redact
from contexer.config import Profile

T = TypeVar("T")


def _redaction_enabled() -> bool:
    """Whether outbound secret redaction is on — config.redaction_enabled() is the one
    implementation (default True); this stays as the remote-side patch point its tests target.
    Import inside the try, and function-local even though `Profile` is imported at module top:
    _wire_args is the last-mile wire guarantee, so ANY failure here — an unresolvable name
    included — must degrade to redaction-ON, never raise and never fail at module load."""
    try:
        from contexer.config import redaction_enabled
        return redaction_enabled()
    except Exception:
        return True

# push_decision returns a text message "Saved decision <id> to your personal context."
_SAVED_ID_RE = re.compile(r"Saved decision (\S+)")
_DEFAULT_TIMEOUT = 10.0

# GATE (issue #174 Task 5, developer-ruled): the contexer-teams `push_decision`/`push_decisions`
# schema is server-controlled, and an unknown/rejected field can poison the outbox with permanent
# validation failures — this happened for real with `source="plan"` (-32602 on every retry, 192
# attempts, before the server accepted the value). `source_files` therefore stayed LOCAL until the
# server accepted it: the share projection/preview/outbox carried it, but `_wire_args` omitted it
# from the actual push payload.
#
# OPENED: contexer-teams accepts `source_files` on both `push_decision` and `push_decisions`
# (server commit e1a2189, on main since 2026-08-09; validated by `INPUT_LIMITS.sourceFiles`, max
# 10 paths x 300 chars). Verified end to end against a live server before flipping - same client,
# same decision, pushed with the gate off and on: off stored NULL, on stored both paths and
# rendered them in the dashboard's Files section, with no -32602 on either the single or batch
# tool. Kept as a constant rather than inlined so a stale endpoint that rejects the field is a
# one-line rollback - still NOT a user-facing config flag, since a config toggle could be flipped
# on against an old server and reintroduce the poisoning failure mode.
_WIRE_SOURCE_FILES = True

# Wire bounds for `source_files`, mirroring contexer-teams `INPUT_LIMITS.sourceFiles`. The server
# commit justifies rejecting over-bounds input with "the client caps at the same numbers" - true
# for the COUNT (store.MAX_SOURCE_FILES, enforced at every anchor write) and false for the
# LENGTH: nothing on the capture side bounds a path at 300 chars, and the egress scrub can even
# LENGTHEN one ([REDACTED:kind] is longer than some values it replaces). The singular
# `push_decision` rejects over-bounds at its zod boundary, i.e. -32602, i.e. exactly the permanent
# outbox poisoning the gate above exists to prevent (the batch tool loosens x8/x4 and drops
# per-row, so only the singular path is exposed). Asymmetric by a mile: dropping a path costs one
# piece of metadata, sending it wedges the queue forever - so clamp here rather than trust the
# capture side to have been bounded.
_WIRE_SOURCE_FILES_MAX_ITEMS = 10
_WIRE_SOURCE_FILES_MAX_LEN = 300

# GATE (plan E1/E2): the same shape as `_WIRE_SOURCE_FILES` above, and it ships CLOSED for the
# same reason that one did — `source_files` stayed local until the server had accepted the field
# and a human had verified it end to end against a live endpoint.
#
# Two DIFFERENT risks gate lifecycle egress, and neither one covers the other:
#   * does this server support it at all — answered at runtime by capability discovery
#     (`decisionLifecycle`, below). An old server never advertises, so it never receives.
#   * do we know the field SPELLING the advertising server expects — answered only by a human
#     who has read the server's schema. Nothing in this repo can answer it: the contexer-teams
#     push schema is server-controlled and not vendored here, and the existing wire already
#     mixes conventions (`decisionId` camelCase beside `source_files` snake_case), so the name
#     is not derivable from the ones already on the wire.
# Getting the second one wrong is not a failed test, it is a permanently stuck outbox row — so
# everything this gate covers is UNVERIFIED and stays off the wire. Everything else in the
# negotiation is live: discovery parses, the projection bounds, the outbox carries.
#
# PRE-FLIP CHECKLIST — all five, in order, before this becomes True. Stated in full here rather
# than as "confirm it the way `_WIRE_SOURCE_FILES` was confirmed", because this gate covers a
# strictly BIGGER set of unknowns than that one did (three guessed names and two closed
# vocabularies, against one name there) and a maintainer who greps `_WIRE_LIFECYCLE` must be
# able to decide safely from this comment alone:
#   1. The record-list field name. Guessed `lifecycle` (see `_wire_args`), by analogy with
#      `source_files`, the most recently added field.
#   2. The revision-identity field name. Guessed `revision_id` — and this is the WEAKEST of the
#      three, because the counter-evidence is in this very file: `asubmit_team_decision` spells
#      the same conceptual field `"revisionId"` (grep it, ~line 665). If only one name gets
#      confirmed, confirm this one.
#   3. The record's OWN key names: `event_id` / `kind` / `occurred_at` / `actor` / `reason` /
#      `revision_id` / `replacement_decision_id` (see `bound_lifecycle`), guessed by mirroring
#      the local `lifecycle.lifecycle_record` shape.
#   4. Both CLOSED VOCABULARIES against the server's real enums — `_WIRE_LIFECYCLE_KINDS` and
#      `_WIRE_LIFECYCLE_ACTORS`. A value the server's enum rejects is a -32602 exactly like a
#      bad field name; `source="plan"` was a rejected VALUE, not a rejected key.
#   5. The server's own `INPUT_LIMITS` for event count, reason length and id length. The
#      numbers below were invented by analogy with `INPUT_LIMITS.sourceFiles` (10 x 300) and
#      match nothing anyone has read.
# Then repeat the identical live validation `source_files` got: same client, same decision,
# pushed with this gate off and then on against a real endpoint, confirming no -32602 on EITHER
# the singular `push_decision` or the batch `push_decisions`, and that the data actually lands
# and renders rather than storing NULL.
#
# Flipping this is a one-line change, and deliberately NOT a config toggle — a toggle can be
# flipped on against a server that rejects the field, which is the failure mode itself.
_WIRE_LIFECYCLE = False

# Wire bounds for the lifecycle record list. `reason` is free human prose (scrubbed, then
# truncated rather than dropped — dropping the whole record over a long reason would lose the
# event, and dropping just the reason would lose the why). `kind` and `actor` are CLOSED
# vocabularies checked against these tuples rather than passed through: an unknown enum value is
# the `source="plan"` failure in miniature, and an actor is supposed to be a CATEGORY, so an
# unrecognized one must never egress as free text that could carry a name or an address.
_WIRE_LIFECYCLE_KINDS = ("retired", "restored", "superseded")
_WIRE_LIFECYCLE_ACTORS = ("human", "ai", "scan", "plan", "bootstrap", "memory")
_WIRE_LIFECYCLE_MAX_EVENTS = 20
_WIRE_LIFECYCLE_MAX_REASON = 300
_WIRE_LIFECYCLE_MAX_ID = 200


def bound_source_files(source_files: list[str]) -> list[str]:
    """Apply the wire bounds above. ONE definition, deliberately called from BOTH layers, the
    same two-layer shape redaction already has: `store._share_projection` bounds so the
    confirm-preview and the durable outbox show exactly what will be sent (a path the wire is
    going to drop must not appear in the preview the developer approves), and `_wire_args`
    bounds again as the hard guarantee, because it is the only chokepoint EVERY push funnels
    through - a legacy outbox row queued before this existed, or a direct `push_decision`
    caller, never passed through a projection at all.

    Callers apply this AFTER redaction, so what is measured is the length that actually goes
    on the wire rather than the on-disk one."""
    return [f for f in source_files
            if len(f) <= _WIRE_SOURCE_FILES_MAX_LEN][:_WIRE_SOURCE_FILES_MAX_ITEMS]


def _bounded_token(value) -> str:
    """One lifecycle id or timestamp, or "" when it is missing or implausibly long. These are
    machine-generated tokens, so an over-long one is corrupt data rather than a long name and is
    DROPPED rather than truncated — a truncated id would still LOOK like an id while silently
    pointing at nothing, which is worse than an absent one."""
    text = str(value or "")
    return text if 0 < len(text) <= _WIRE_LIFECYCLE_MAX_ID else ""


def bound_lifecycle(records: list, *, reasons: bool = True,
                    redact_on: bool = False) -> list[dict]:
    """Project a decision's COMPLETED lifecycle history onto the wire shape, whitelist-first.

    ONE definition called from BOTH layers, the same two-layer shape `bound_source_files` and
    redaction already have: `store._share_projection` applies it so the durable outbox carries
    exactly what a later drain will send, and `_wire_args` applies it again as the hard
    guarantee, because it is the only chokepoint every push funnels through — a row queued
    before this existed never passed through a projection at all.

    Whitelist-first is the privacy boundary, not tidiness: this builds a NEW dict out of six
    known keys instead of copying and deleting, so a field added to the local record later (a
    proposal id, an evidence reference, a session id) cannot egress by default. `reason` is the
    only free text and it is scrubbed before being measured, since redaction can lengthen a
    string. `reasons=False` is the `retirementReasons` sub-capability being absent: the events
    still go, without their prose.

    Only records the local lifecycle actually completes are representable — `proposed_lifecycle`
    is a different key on the entry and is never read here or anywhere on the wire path."""
    out: list[dict] = []
    for rec in records or []:
        if not isinstance(rec, dict) or rec.get("kind") not in _WIRE_LIFECYCLE_KINDS:
            continue
        row: dict = {"kind": rec["kind"]}
        for key in ("event_id", "revision_id", "replacement_decision_id", "occurred_at"):
            bounded = _bounded_token(rec.get(key))
            if bounded:
                row[key] = bounded
        if rec.get("actor") in _WIRE_LIFECYCLE_ACTORS:
            row["actor"] = rec["actor"]
        if reasons:
            reason = str(rec.get("reason") or "")
            if redact_on:
                reason = redact.scrub_text(reason)
            if reason:
                row["reason"] = reason[:_WIRE_LIFECYCLE_MAX_REASON]
        out.append(row)
    # The MOST RECENT events, not the first: a decision with a long history is most usefully
    # described by how it ended, and the oldest records are the ones a reader can live without.
    return out[-_WIRE_LIFECYCLE_MAX_EVENTS:]


def _wire_args(*, type: str, content: str, repo: str | None = None,
               rationale: str | None = None, agent: str | None = None,
               confidence: int | None = None, evidence: list[str] | None = None,
               source: str | None = None, decision_id: str | None = None,
               title: str | None = None, source_files: list[str] | None = None,
               redact_on: bool | None = None, revision_id: str | None = None,
               lifecycle: list | None = None,
               lifecycle_caps: "DecisionLifecycleCapabilities | None" = None) -> dict:
    """Serialize one decision onto the push wire shape, OMITTING every unset optional (the server
    reads an absent key as NULL/unset - so None must not be sent as a literal). The single copy of
    wire-serialization, shared by apush_decision (one) and apush_decisions (batch).

    This is also the last-mile secret-redaction chokepoint: every push (single, batch, and
    outbox drain) funnels through here, so scrubbing content/evidence/rationale/title here is the
    hard guarantee that no secret egresses — including legacy on-disk secrets that predate
    capture-time redaction. A title is derived from content, so it can carry the same secrets;
    it is scrubbed independently, same as content/evidence. Idempotent with the capture scrub
    (the [REDACTED] placeholder never re-matches).

    `source_files` passes the `_WIRE_SOURCE_FILES` gate above (now open) and is re-scrubbed here
    for the same idempotent-egress-rule reason as content/evidence/title. The gate is read HERE,
    at call time - not captured by a caller ahead of time - so entries queued to the outbox while
    it was still closed egress their files on the next drain, with no re-queue or migration; a
    rollback likewise takes effect at drain time, not at enqueue time. It is also CLAMPED here to
    the server's own bounds (see `_WIRE_SOURCE_FILES_MAX_*`), for the same reason redaction lives
    at this chokepoint: every push funnels through here, so bounding once here is the guarantee,
    where bounding at each capture site would be a promise several writers have to keep.

    `redact_on` lets a batch caller resolve the on/off flag ONCE and pass it in (avoids re-reading
    config.toml per row); None means resolve it here for a lone call.

    `revision_id` and `lifecycle` (plan E1/E2) egress only when BOTH the `_WIRE_LIFECYCLE`
    constant above is open AND `lifecycle_caps` says this server advertised the matching
    sub-capability, and each sub-capability is honoured on its own — `tombstones` without
    `retirementReasons` sends the events with their prose stripped. Like `source_files`, the
    decision is made HERE, at call time, never captured by a caller: `lifecycle_caps` is
    resolved by the pushing store immediately before the call, so an outbox row queued before
    this client had ever spoken to the server drains under whatever is known AT DRAIN TIME, in
    both directions. `lifecycle_caps=None` means "not supported / not discovered / discovery
    failed", and all three land in the same place, which is the old shape — an unknown server
    is an old server."""
    scrub = _redaction_enabled() if redact_on is None else redact_on
    if scrub:
        content = redact.scrub_text(content)
        if rationale is not None:
            rationale = redact.scrub_text(rationale)
        if evidence is not None:
            evidence = [redact.scrub_text(e) for e in evidence]
        if title is not None:
            title = redact.scrub_text(title)
        if source_files is not None:
            source_files = [redact.scrub_text(f) for f in source_files]
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
    if title is not None:
        args["title"] = title
    if _WIRE_SOURCE_FILES and source_files:
        # Bounded AFTER the scrub above, so a redaction-lengthened path is measured at its real
        # wire length. An all-over-bounds list omits the key entirely rather than sending [],
        # keeping the "omit every unset optional" rule (the server reads absent as unset, and an
        # empty array would CLEAR the column via `excluded.source_files` on re-push).
        bounded = bound_source_files(source_files)
        if bounded:
            args["source_files"] = bounded
    if _WIRE_LIFECYCLE and lifecycle_caps is not None:
        if lifecycle_caps.revisions and revision_id:
            # UNVERIFIED spelling, and the weakest of the three guesses: `asubmit_team_decision`
            # below spells the same conceptual field `"revisionId"` (camelCase) at its own tool
            # top level, so this could as easily be that. Item 2 of the `_WIRE_LIFECYCLE`
            # pre-flip checklist — confirm this one first.
            args["revision_id"] = revision_id
        if lifecycle_caps.tombstones and lifecycle:
            # UNVERIFIED spelling — item 1 of the `_WIRE_LIFECYCLE` pre-flip checklist.
            events = bound_lifecycle(lifecycle, reasons=lifecycle_caps.retirement_reasons,
                                     redact_on=scrub)
            if events:   # an all-unknown-kind list omits the key, never sends []
                args["lifecycle"] = events
    return args


def _reconciliation_wire_body(*, type: str, content: str, repo: str | None = None,
                              rationale: str | None = None, agent: str | None = None,
                              confidence: int | None = None,
                              evidence: list[str] | None = None,
                              source: str | None = None, title: str | None = None,
                              source_files: list[str] | None = None,
                              redact_on: bool | None = None) -> dict:
    """Nested decision body for preview/atomic-submit, through the exact same last-mile
    redaction and source-file bounds as personal push. The only shape difference is that the
    stable decision id belongs at the tool top level, never inside `decision`/`proposed`."""
    return _wire_args(
        type=type, content=content, repo=repo, rationale=rationale, agent=agent,
        confidence=confidence, evidence=evidence, source=source, title=title,
        source_files=source_files, redact_on=redact_on)


def _caps_version(raw: dict) -> int:
    """A capability block's advertised version, 0 when it is missing or unparseable."""
    try:
        return int(raw.get("version", 0))
    except (TypeError, ValueError):
        return 0


class RemoteStoreError(Exception):
    """Base for any RemoteStore failure. Callers catch this to degrade to local-only."""


class RemoteAuthError(RemoteStoreError):
    """The Teams endpoint rejected the token (HTTP 401/403).

    ``_transport_auth`` is set True only for a genuine transport 401/403 (by ``_classify``);
    it stays False for a server-side authz/scope denial (``_classify_tool_error``). The sync
    reactive-refresh path refreshes only when it is True — a scope denial a refresh can't fix."""

    _transport_auth = False


class RemoteUnavailableError(RemoteStoreError):
    """The Teams endpoint was unreachable (network error, timeout, 5xx)."""


@dataclass(frozen=True)
class RemoteDecision:
    """One row from the Teams merged context. ``scope`` is provenance (personal|team).

    ``title`` is the cloud's stored heading (``None`` for a row synced before Decision
    Titles v2, or a client that pushed no title) - display-time derivation is the
    consumer's job (team_context.format_team_section / store.title_and_body), never
    re-computed here."""

    id: str
    type: str
    title: str | None
    content: str
    rationale: str | None
    repo: str | None
    agent: str | None
    scope: str
    local_decision_id: str | None = None
    team_id: str | None = None
    team_name: str | None = None
    reconciliation: dict | None = None


@dataclass(frozen=True)
class RemoteContext:
    """A get_context response: decisions plus incremental-pull tombstones and the cursor."""

    decisions: list[RemoteDecision]
    deleted: list[str]
    cursor: str | None


@dataclass(frozen=True)
class RemoteTeam:
    """One shared-team target returned by the Teams MCP server."""

    id: str
    name: str
    role: str


@dataclass(frozen=True)
class TeamShareResult:
    """Structured outcome of submitting a synced decision for team review."""

    status: str
    kind: str
    candidate_id: str
    team: RemoteTeam


@dataclass(frozen=True)
class DecisionReconciliationCapabilities:
    version: int
    atomic_submit: bool
    preview: bool
    three_way_merge: bool


@dataclass(frozen=True)
class DecisionLifecycleCapabilities:
    """What an advertising server accepts of a decision's lifecycle (plan E1).

    The three flags are INDEPENDENT and are read independently at the wire: `revisions` gates
    the immutable revision identity, `tombstones` gates the completed lifecycle records, and
    `retirementReasons` gates only the human prose inside those records. Treating the block as
    all-or-nothing would mean a server that accepts events but not reasons receives neither,
    which is strictly less than it asked for."""

    version: int
    revisions: bool
    tombstones: bool
    retirement_reasons: bool


@dataclass(frozen=True)
class ServerCapabilities:
    decision_reconciliation: DecisionReconciliationCapabilities | None
    # Defaulted so every existing construction site (tests, share.py's fallbacks) keeps working
    # and lands on "not advertised" — which is the same place discovery failure lands.
    decision_lifecycle: DecisionLifecycleCapabilities | None = None


@dataclass(frozen=True)
class ReconciliationField:
    field: str
    before: str | list[str] | None
    after: str | list[str] | None


@dataclass(frozen=True)
class DecisionReconciliationPreview:
    personal_head: str | None
    team_head: str | None
    pending_candidate_id: str | None
    state: str
    operation: str
    fields: list[ReconciliationField]
    available_actions: list[str]
    team: RemoteTeam


@dataclass(frozen=True)
class TeamSubmissionResult:
    status: str
    kind: str
    personal_head: str | None
    team_head: str | None
    candidate_id: str | None
    revision_id: str
    replayed: bool
    team: RemoteTeam


_UNDISCOVERED = object()   # "we have not asked this server yet", as opposed to "it said no"


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
        # _UNDISCOVERED, or a DecisionLifecycleCapabilities, or None — and None is a REAL answer
        # here ("this server does not do lifecycle"), which is why the sentinel exists at all:
        # without it a not-advertising server would be re-probed on every push.
        self._lifecycle_caps = _UNDISCOVERED

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
    # is exactly ONE copy of the wire-serialization + error-mapping logic. The one thing the
    # shims add is reactive token refresh — kept OFF the async core on purpose so the awaited
    # push never spawns an uncancellable refresh thread (see _run_with_reactive_refresh).

    def _redact_on(self) -> bool:
        """Redaction policy for THIS store: honor the Profile it was built from (an explicit
        opt-in must not be lost to a global opt-out on disk); fall back to the global config
        only for a profile-less store (direct construction / tests)."""
        return self._profile.redact_secrets if self._profile is not None else _redaction_enabled()

    async def apush_decision(self, *, type: str, content: str, repo: str | None,
                             rationale: str | None = None, agent: str | None = None,
                             confidence: int | None = None, evidence: list[str] | None = None,
                             source: str | None = None, decision_id: str | None = None,
                             title: str | None = None,
                             source_files: list[str] | None = None,
                             revision_id: str | None = None,
                             lifecycle: list | None = None) -> str:
        """Async core of :meth:`push_decision`. Awaits the transport (cancellable)."""
        caps = await self._alifecycle_caps(
            [{"lifecycle": lifecycle, "revision_id": revision_id}])
        result = await self._ainvoke("push_decision", _wire_args(
            type=type, content=content, repo=repo, rationale=rationale, agent=agent,
            confidence=confidence, evidence=evidence, source=source, decision_id=decision_id,
            title=title, source_files=source_files, redact_on=self._redact_on(),
            revision_id=revision_id, lifecycle=lifecycle, lifecycle_caps=caps))
        text = _first_text(getattr(result, "content", None))
        match = _SAVED_ID_RE.search(text) if text else None
        return match.group(1) if match else ""

    async def apush_decisions(self, kwargs_list: list[dict]) -> tuple[list[str], list[dict]]:
        """Async core of :meth:`push_decisions`: batch-push many decisions in ONE call, awaiting
        the transport (cancellable). Each item is push_decision KWARGS (built by
        _dec_push_kwargs / _entry_push_kwargs), serialized through the same _wire_args as the
        singular path.

        Returns ``(saved_ids, skipped)``: server ids that committed, and a list of
        ``{"decision_id", "reason"}`` for rows the server did NOT store. Reasons are either
        TRANSIENT (``quota_exceeded`` - at capacity, retry later) or PERMANENT (``invalid_type`` /
        ``invalid_content`` - the decision can't be stored as-is); the caller keeps transient rows
        queued and drops permanent ones. Per-row validation on the server means one bad row is
        skipped, never sinking the batch. Raises RemoteStoreError if the response omits a submitted
        decisionId - an unconfirmed row must not be treated as done, so it stays queued."""
        redact_on = self._redact_on()  # resolved once for the whole batch (honors this store's profile)
        caps = await self._alifecycle_caps(kwargs_list)  # one discovery for the whole batch
        result = await self._ainvoke(
            "push_decisions",
            {"decisions": [_wire_args(**kw, redact_on=redact_on, lifecycle_caps=caps)
                           for kw in kwargs_list]})
        structured = getattr(result, "structuredContent", None) or {}
        results = structured.get("results") or []
        skipped_rows = structured.get("skipped") or []
        saved = [str(r.get("id", "")) for r in results]
        skipped = [{"decision_id": str(r.get("decisionId")), "reason": r.get("reason", "invalid")}
                   for r in skipped_rows if r.get("decisionId")]
        # Every submitted decisionId must be accounted for (saved OR skipped); otherwise the
        # server didn't confirm it and we must NOT drop it - fail so the caller keeps it queued.
        submitted = {kw.get("decision_id") for kw in kwargs_list if kw.get("decision_id")}
        accounted = {r.get("decisionId") for r in results} | {r.get("decisionId") for r in skipped_rows}
        missing = submitted - accounted
        if missing:
            raise RemoteStoreError(
                f"push_decisions response did not account for {len(missing)} submitted decision(s)")
        return saved, skipped

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
                title=row.get("title"),
                content=row.get("content", ""),
                rationale=row.get("rationale"),
                repo=row.get("repo"),
                agent=row.get("agent"),
                scope=row.get("scope", ""),
                local_decision_id=row.get("localDecisionId"),
                team_id=row.get("teamId"),
                team_name=row.get("teamName"),
                reconciliation=(dict(row["reconciliation"])
                                if isinstance(row.get("reconciliation"), dict) else None),
            )
            for row in rows
        ]
        return RemoteContext(
            decisions=decisions,
            deleted=list(structured.get("deleted") or []),
            cursor=structured.get("cursor"),
        )

    async def alist_teams(self) -> list[RemoteTeam]:
        """Return shared teams available as explicit submission targets."""
        result = await self._ainvoke("list_teams", {})
        structured = getattr(result, "structuredContent", None) or {}
        return [
            RemoteTeam(id=str(row.get("id", "")), name=str(row.get("name", "")),
                       role=str(row.get("role", "member")))
            for row in (structured.get("teams") or [])
            if row.get("id")
        ]

    async def aget_capabilities(self) -> ServerCapabilities:
        """Discover optional server protocols before creating durable operations.

        Each block is parsed independently: a server advertising one and not the other must not
        lose the one it does advertise. An absent or non-dict block is None — never a
        default-True shape, since an unknown server is an old server."""
        result = await self._ainvoke("get_capabilities", {})
        structured = getattr(result, "structuredContent", None) or {}
        advertised = structured.get("capabilities") or {}
        raw = advertised.get("decisionReconciliation")
        reconciliation = None
        if isinstance(raw, dict):
            reconciliation = DecisionReconciliationCapabilities(
                version=_caps_version(raw),
                atomic_submit=raw.get("atomicSubmit") is True,
                preview=raw.get("preview") is True,
                three_way_merge=raw.get("threeWayMerge") is True,
            )
        raw = advertised.get("decisionLifecycle")
        lifecycle = None
        if isinstance(raw, dict):
            lifecycle = DecisionLifecycleCapabilities(
                version=_caps_version(raw),
                revisions=raw.get("revisions") is True,
                tombstones=raw.get("tombstones") is True,
                retirement_reasons=raw.get("retirementReasons") is True,
            )
        return ServerCapabilities(decision_reconciliation=reconciliation,
                                  decision_lifecycle=lifecycle)

    async def _alifecycle_caps(self, rows: list[dict]) -> DecisionLifecycleCapabilities | None:
        """The lifecycle capability governing THIS push, discovered lazily and memoized.

        Discovery costs a round trip, so it is skipped entirely unless a row actually carries
        something the capability would gate — the overwhelmingly common push (a live decision
        with no lifecycle history) stays exactly as cheap as it was, and with the gate constant
        closed nothing here ever runs at all.

        Any failure — an old server with no `get_capabilities` tool, a timeout, a rejected
        token — resolves to None, i.e. the old shape. That direction is the whole rule: a push
        must never be upgraded by a discovery attempt that did not actually succeed."""
        if not _WIRE_LIFECYCLE or not any(r.get("lifecycle") or r.get("revision_id")
                                          for r in rows):
            return None
        if self._lifecycle_caps is _UNDISCOVERED:
            try:
                self._lifecycle_caps = (await self.aget_capabilities()).decision_lifecycle
            except RemoteStoreError:
                self._lifecycle_caps = None
        return self._lifecycle_caps

    async def apreview_decision_reconciliation(
            self, decision_id: str, team_id: str, *, type: str, content: str,
            repo: str | None = None, rationale: str | None = None,
            agent: str | None = None, confidence: int | None = None,
            evidence: list[str] | None = None, source: str | None = None,
            title: str | None = None,
            source_files: list[str] | None = None) -> DecisionReconciliationPreview:
        proposed = _reconciliation_wire_body(
            type=type, content=content, repo=repo, rationale=rationale, agent=agent,
            confidence=confidence, evidence=evidence, source=source, title=title,
            source_files=source_files, redact_on=self._redact_on())
        result = await self._ainvoke("preview_decision_reconciliation", {
            "decisionId": decision_id, "teamId": team_id, "proposed": proposed})
        structured = getattr(result, "structuredContent", None) or {}
        team = structured.get("team") or {}
        fields = [ReconciliationField(
            field=str(row.get("field", "")), before=row.get("before"), after=row.get("after"))
            for row in (structured.get("fields") or []) if isinstance(row, dict)]
        return DecisionReconciliationPreview(
            personal_head=structured.get("personalHead"),
            team_head=structured.get("teamHead"),
            pending_candidate_id=structured.get("pendingCandidateId"),
            state=str(structured.get("state", "")),
            operation=str(structured.get("operation", "")),
            fields=fields,
            available_actions=[str(v) for v in (structured.get("availableActions") or [])],
            team=RemoteTeam(id=str(team.get("id", team_id)),
                            name=str(team.get("name", team_id)),
                            role=str(team.get("role", "member"))),
        )

    async def asubmit_team_decision(
            self, decision_id: str, revision_id: str, team_id: str, *,
            expected_personal_head: str | None, expected_team_head: str | None,
            idempotency_key: str, type: str, content: str,
            repo: str | None = None, rationale: str | None = None,
            agent: str | None = None, confidence: int | None = None,
            evidence: list[str] | None = None, source: str | None = None,
            title: str | None = None, source_files: list[str] | None = None,
            include_evidence: bool | None = None) -> TeamSubmissionResult:
        decision = _reconciliation_wire_body(
            type=type, content=content, repo=repo, rationale=rationale, agent=agent,
            confidence=confidence, evidence=evidence, source=source, title=title,
            source_files=source_files, redact_on=self._redact_on())
        args = {
            "decisionId": decision_id,
            "revisionId": revision_id,
            "teamId": team_id,
            "expectedPersonalHead": expected_personal_head,
            "expectedTeamHead": expected_team_head,
            "idempotencyKey": idempotency_key,
            "decision": decision,
        }
        if include_evidence is not None:
            args["includeEvidence"] = include_evidence
        result = await self._ainvoke("submit_team_decision", args)
        structured = getattr(result, "structuredContent", None) or {}
        team = structured.get("team") or {}
        return TeamSubmissionResult(
            status=str(structured.get("status", "")),
            kind=str(structured.get("kind", "")),
            personal_head=structured.get("personalHead"),
            team_head=structured.get("teamHead"),
            candidate_id=structured.get("candidateId"),
            revision_id=str(structured.get("revisionId", revision_id)),
            replayed=structured.get("replayed") is True,
            team=RemoteTeam(id=str(team.get("id", team_id)),
                            name=str(team.get("name", team_id)),
                            role=str(team.get("role", "member"))),
        )

    async def asubmit_decision_to_team(self, decision_id: str, team_id: str,
                                       *, include_evidence: bool | None = None) -> TeamShareResult:
        """Submit an already-synced personal decision to a team for lead review."""
        args = {"decisionId": decision_id, "teamId": team_id}
        if include_evidence is not None:
            args["includeEvidence"] = include_evidence
        result = await self._ainvoke("share_decision", args)
        structured = getattr(result, "structuredContent", None) or {}
        team = structured.get("team") or {}
        return TeamShareResult(
            status=str(structured.get("status", "")),
            kind=str(structured.get("kind", "")),
            candidate_id=str(structured.get("candidateId", "")),
            team=RemoteTeam(id=str(team.get("id", team_id)), name=str(team.get("name", team_id)),
                            role=str(team.get("role", "member"))),
        )

    def push_decision(self, *, type: str, content: str, repo: str | None,
                      rationale: str | None = None, agent: str | None = None,
                      confidence: int | None = None, evidence: list[str] | None = None,
                      source: str | None = None, decision_id: str | None = None,
                      title: str | None = None,
                      source_files: list[str] | None = None,
                      revision_id: str | None = None,
                      lifecycle: list | None = None) -> str:
        """Push one local decision to the caller's personal Teams context (sync shim).

        Returns the server decision id (best-effort; ``""`` if the response carries none).
        Idempotent on ``decision_id`` server-side. Raises RemoteStoreError on failure. For
        off-loop callers only — an in-loop caller must await :meth:`apush_decision` instead
        (asyncio.run cannot run inside a running event loop)."""
        return self._run_with_reactive_refresh(lambda: asyncio.run(self.apush_decision(
            type=type, content=content, repo=repo, rationale=rationale, agent=agent,
            confidence=confidence, evidence=evidence, source=source, decision_id=decision_id,
            title=title, source_files=source_files, revision_id=revision_id,
            lifecycle=lifecycle)))

    def push_decisions(self, kwargs_list: list[dict]) -> tuple[list[str], list[dict]]:
        """Batch-push decisions in ONE call (sync shim over :meth:`apush_decisions`). Off-loop
        callers only. Returns ``(saved_ids, skipped)`` where skipped items are
        ``{"decision_id", "reason"}``; raises RemoteStoreError on a transport/auth failure (the
        caller then re-queues the whole chunk)."""
        return self._run_with_reactive_refresh(lambda: asyncio.run(self.apush_decisions(kwargs_list)))

    def get_context(self, repo: str | None = None,
                    updated_since: str | None = None) -> RemoteContext:
        """Pull merged team context (sync shim over :meth:`aget_context`).

        ``updated_since`` (an ISO cursor, typically a prior ``RemoteContext.cursor``) fetches
        only changes after it and populates ``deleted`` (tombstoned ids to remove locally).
        Raises RemoteStoreError on failure. Off-loop callers only."""
        return self._run_with_reactive_refresh(
            lambda: asyncio.run(self.aget_context(repo=repo, updated_since=updated_since)))

    def list_teams(self) -> list[RemoteTeam]:
        """Synchronous team discovery for CLI callers."""
        return self._run_with_reactive_refresh(lambda: asyncio.run(self.alist_teams()))

    def get_capabilities(self) -> ServerCapabilities:
        return self._run_with_reactive_refresh(lambda: asyncio.run(self.aget_capabilities()))

    def preview_decision_reconciliation(self, decision_id: str, team_id: str,
                                        **decision) -> DecisionReconciliationPreview:
        return self._run_with_reactive_refresh(lambda: asyncio.run(
            self.apreview_decision_reconciliation(decision_id, team_id, **decision)))

    def submit_team_decision(self, decision_id: str, revision_id: str, team_id: str,
                             **kwargs) -> TeamSubmissionResult:
        return self._run_with_reactive_refresh(lambda: asyncio.run(
            self.asubmit_team_decision(decision_id, revision_id, team_id, **kwargs)))

    def submit_decision_to_team(self, decision_id: str, team_id: str,
                                *, include_evidence: bool | None = None) -> TeamShareResult:
        """Synchronous team-candidate submission for CLI callers."""
        return self._run_with_reactive_refresh(lambda: asyncio.run(
            self.asubmit_decision_to_team(decision_id, team_id,
                                          include_evidence=include_evidence)))

    def _run_with_reactive_refresh(self, run: "Callable[[], T]") -> T:
        """Run an off-loop op, and on a transport 401/403 do exactly ONE token refresh + retry.

        The bearer can expire mid-session (it is frozen at construction), so a transport-auth
        failure gets one bounded refresh-and-retry. This lives in the SYNC shim layer — NOT in
        the async core — deliberately (#108): the refresh is a blocking, cross-process-locked
        network call (`auth._locked_refresh`), and doing it here (off the event loop, no running
        loop) means the awaited async push path never spawns an uncancellable worker thread. Only
        a genuine *transport* 401/403 (tagged `_transport_auth`) refreshes — a server-side
        authz/scope denial (which a refresh can't fix) surfaces unchanged."""
        try:
            return run()
        except RemoteAuthError as exc:
            if not getattr(exc, "_transport_auth", False) or not self._refresh_token():
                raise
        return run()  # one retry with the refreshed token; a second 401 here propagates

    async def _ainvoke(self, name: str, arguments: dict):
        """Call one Teams tool, mapping any transport failure to a typed RemoteStoreError.

        Async core — pure and cancellable: it does NO blocking token refresh (that lives in
        the sync shim's :meth:`_run_with_reactive_refresh`), so a cancelled push tears the
        transport down with nothing lingering (#108). A transport 401/403 surfaces as a
        ``RemoteAuthError`` tagged ``_transport_auth`` so the sync shim knows it is
        refresh-eligible; the in-loop async caller instead degrades (→ outbox retry).

        ``except Exception`` deliberately does NOT catch ``asyncio.CancelledError`` (a
        BaseException): a cancelled push must propagate and tear the transport down, not be
        reclassified as an unreachable endpoint."""
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
            raise _classify(exc) from exc
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
        # `contexer login --team` does not exist: login takes --endpoint, and --team was
        # silently ignored, so this line sent people to a command that could not fix them.
        warn_once(
            f"Contexer: Teams authentication failed while trying to {action} - "
            "continuing local-only. Run `contexer login` to sign in again.",
            key="degrade:auth",
        )
    elif isinstance(exc, RemoteUnavailableError):
        warn_once(
            f"Contexer: Teams endpoint unreachable while trying to {action} - "
            "continuing local-only.",
            key="degrade:unreachable",
        )
    else:
        # The cloud was reached and answered, it just REFUSED the request (e.g. rate limit or a
        # validation error) - distinct from the transport-level "unreachable" case above. Surface
        # the server's own reason (`exc`) so the developer sees WHY (e.g. "Rate limit exceeded -
        # retry in 12s") instead of a generic failure that reads like a network outage.
        warn_once(
            f"Contexer: Teams refused the request while trying to {action}: {exc} - "
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
    """Map a raw transport exception to a typed RemoteStoreError.

    A transport 401/403 is tagged ``_transport_auth`` so the sync shim's reactive-refresh
    path can distinguish it from a server-side authz/scope denial (which arrives via
    ``_classify_tool_error`` untagged and must never trigger a token refresh)."""
    status = _extract_status(exc)
    if status in (401, 403):
        err = RemoteAuthError(f"Teams rejected the token (HTTP {status}).")
        err._transport_auth = True
        return err
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
