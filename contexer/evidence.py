"""Evidence event schema, pure validation, and the host-hook emission surface.

An evidence event is one observed fact about a session - a directive the developer stated, a
file that changed, a conclusion an agent reached - recorded so a later policy pass can decide
what deserves to become a decision. `validate_event` is a pure function and the ONLY schema
gate: storage never re-asserts flatness or caps, it validates once and writes what it gets.

STORAGE LIVES IN `spool.py`, not here. This module is the schema plus what a host adapter
calls; the spool owns every filesystem concern (per-event files, holds, retention, `.gap`).
`emit_hook_event` reaches it through a function-level import, which is what keeps the
dependency one-way at import time - `spool` imports this module for `validate_event`.

A leaf module: it imports `redact` (itself a leaf) and reaches `store` through the MODULE
OBJECT at call time (`store.capture_user_constraint`, never `from contexer.store import ...`)
- the same load-order discipline `guard_engine.py` documents, so store.py never needs this
module at import time and a test patching `contexer.store` is seen here.

The schema is FROZEN per version: an unknown top-level key is an error rather than being
preserved, and a `schema_version` other than `SCHEMA_VERSION` is rejected in both directions
(a forward version means a newer writer whose semantics this reader cannot assume).
"""
import contextlib
import json
import math
import re
import uuid
from collections.abc import Mapping
from datetime import datetime, timezone

from contexer import redact, store

SCHEMA_VERSION = 1

# `candidate_disposition` is deliberately absent: a settled candidate's disposition is
# recorded in the DECISION's own history (`evidence_summary`, written by reconciliation)
# rather than as a synthetic event, so nothing emits that kind any more and a spooled file
# claiming it is a schema drift the validator should catch.
EVENT_KINDS = frozenset({
    "user_directive", "agent_conclusion", "file_changed", "diff_observed",
    "test_result", "decision_repeated", "policy_evaluation", "session_reconcile",
})

# Measured implementation constants, not product promises: bounds that keep one event cheap
# to append and to read back. Exceeding a bound is either an error (the event size) or a
# recorded, bounded loss (summary/files/attribute values) - never a silent truncation.
_MAX_EVENT_BYTES = 8192
_MAX_SUMMARY_CHARS = 500
_MAX_FILES = 20
_MAX_PATH_CHARS = 300
_MAX_ATTRS = 20
_MAX_ATTR_KEY_CHARS = 64
_MAX_ATTR_VALUE_CHARS = 500
# session_id / repo_key / source caps, in that order of appearance below.
_MAX_SESSION_ID_CHARS = 128
_MAX_REPO_KEY_CHARS = 300
_MAX_SOURCE_CHARS = 64

_KEYS = frozenset({
    "schema_version", "event_id", "session_id", "repo_key", "kind", "occurred_at",
    "source", "summary", "files", "content_hash", "attributes",
})

_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:[\\/]")
_SHA256 = re.compile(r"[0-9a-f]{64}")


def _required_text(event: Mapping, key: str, cap: int, errors: list[str]) -> str:
    """A required non-empty str field, at most `cap` chars. Appends its own error, if any."""
    value = event.get(key)
    if not isinstance(value, str) or not value.strip():
        errors.append(f"{key} must be a non-empty string")
    elif len(value) > cap:
        errors.append(f"{key} exceeds {cap} characters ({len(value)})")
    else:
        return value
    return ""


def _normalized_time(event: Mapping, errors: list[str]) -> str:
    """`occurred_at` as UTC ISO-8601 with an explicit offset. Naive timestamps are rejected:
    an event whose zone is guessed cannot be ordered against one from another host."""
    value = event.get("occurred_at")
    if not isinstance(value, str):
        errors.append("occurred_at must be an ISO-8601 string")
        return ""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        errors.append(f"occurred_at is not ISO-8601 parseable: {value!r}")
        return ""
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        errors.append("occurred_at must be timezone-aware")
        return ""
    return parsed.astimezone(timezone.utc).isoformat()


def _normalized_files(event: Mapping, errors: list[str]) -> tuple[list[str], int]:
    """The repo-relative file list plus its ORIGINAL length (the caller records the loss when
    the list is truncated). Absolute and parent-escaping paths are rejected, not rewritten."""
    value = event.get("files", [])
    if not isinstance(value, list):
        errors.append("files must be a list of strings")
        return [], 0
    for i, path in enumerate(value):
        if not isinstance(path, str) or not path.strip():
            errors.append(f"files[{i}] must be a non-empty string")
        elif len(path) > _MAX_PATH_CHARS:
            errors.append(f"files[{i}] exceeds {_MAX_PATH_CHARS} characters ({len(path)})")
        elif path.startswith("/") or _WINDOWS_DRIVE.match(path):
            errors.append(f"files[{i}] must be repo-relative, got {path!r}")
        elif ".." in path.replace("\\", "/").split("/"):
            errors.append(f"files[{i}] must not contain a '..' segment, got {path!r}")
    return value[:_MAX_FILES], len(value)


def _normalized_attributes(event: Mapping, errors: list[str]) -> dict:
    """A FLAT dict of scalars: nesting would make the ledger a document store, and the policy
    pass reads attributes as decision inputs, so str values are scrubbed and clipped here."""
    value = event.get("attributes", {})
    if not isinstance(value, dict):
        errors.append("attributes must be a flat dict")
        return {}
    # `files_total` is not counted: normalization ADDS it, so counting it would make
    # re-validating a normalized event that used all 20 caller slots an error - the
    # no-op-on-replay property this whole function promises.
    counted = sum(1 for k in value if k != "files_total")
    if counted > _MAX_ATTRS:
        errors.append(f"attributes has more than {_MAX_ATTRS} keys ({counted})")
    out = {}
    for key, item in value.items():
        if not isinstance(key, str) or len(key) > _MAX_ATTR_KEY_CHARS:
            errors.append(f"attributes key {key!r} must be a string of at most "
                          f"{_MAX_ATTR_KEY_CHARS} characters")
            continue
        if isinstance(item, str):
            out[key] = _clip(redact.scrub_text(item), _MAX_ATTR_VALUE_CHARS)
        elif isinstance(item, float) and not math.isfinite(item):
            # `json.dumps` writes NaN/Infinity happily and no JSON reader accepts them back,
            # so an unchecked one would be a ledger line that never parses again.
            errors.append(f"attributes[{key!r}] value must be a finite number, got {item!r}")
        elif isinstance(item, (int, float, bool)):
            out[key] = item
        else:
            errors.append(f"attributes[{key!r}] value must be str, int, float or bool")
    return out


def _clip(text: str, cap: int) -> str:
    """Clip to `cap` characters INCLUDING the ellipsis, so re-validating a clipped value is a
    no-op (the whole normalization is idempotent, which is what makes replay safe)."""
    if len(text) <= cap:
        return text
    return text[:cap - 1] + "…"


def validate_event(event: Mapping) -> tuple[dict | None, list[str]]:
    """Return `(normalized_copy, [])` for a structurally valid event, else `(None, errors)`.

    Collects EVERY structural error, never raises on any input, and never mutates `event`.
    Normalization is bounded and lossy-but-recorded: `summary` and str attribute values are
    scrubbed then clipped, `files` is truncated with the original count kept in
    `attributes["files_total"]`, `occurred_at` becomes UTC, and the optional fields default.
    Normalizing an already-normalized event is a no-op.
    """
    if not isinstance(event, Mapping):
        return None, [f"event must be a mapping, got {type(event).__name__}"]

    errors: list[str] = []
    for key in sorted(str(k) for k in event if k not in _KEYS):
        errors.append(f"unknown top-level key: {key!r}")

    version = event.get("schema_version")
    if isinstance(version, bool) or version != SCHEMA_VERSION:
        errors.append(f"schema_version must be {SCHEMA_VERSION}, got {version!r}")

    event_id = event.get("event_id")
    try:
        uuid.UUID(event_id)
    except (ValueError, AttributeError, TypeError):
        errors.append(f"event_id must be a UUID string, got {event_id!r}")

    session_id = _required_text(event, "session_id", _MAX_SESSION_ID_CHARS, errors)
    repo_key = _required_text(event, "repo_key", _MAX_REPO_KEY_CHARS, errors)
    source = _required_text(event, "source", _MAX_SOURCE_CHARS, errors)

    kind = event.get("kind")
    # The isinstance guard is the never-raises half: `kind not in EVENT_KINDS` hashes its
    # left side, so an unhashable value (a JSON list or object) raised TypeError out of a
    # function whose whole contract is to return its errors instead.
    if not isinstance(kind, str) or kind not in EVENT_KINDS:
        errors.append(f"kind must be one of {sorted(EVENT_KINDS)}, got {kind!r}")

    occurred_at = _normalized_time(event, errors)

    summary = event.get("summary", "")
    if not isinstance(summary, str):
        errors.append("summary must be a string")
        summary = ""

    content_hash = event.get("content_hash")
    if content_hash is not None and (not isinstance(content_hash, str)
                                     or not _SHA256.fullmatch(content_hash)):
        errors.append("content_hash must be a 64-character lowercase hex string or null")

    files, files_total = _normalized_files(event, errors)
    attributes = _normalized_attributes(event, errors)
    if files_total > len(files):
        attributes["files_total"] = files_total

    if errors:
        return None, errors

    normalized = {
        "schema_version": SCHEMA_VERSION,
        "event_id": str(uuid.UUID(event_id)),
        "session_id": session_id,
        "repo_key": repo_key,
        "kind": kind,
        "occurred_at": occurred_at,
        "source": source,
        "summary": _clip(redact.scrub_text(summary), _MAX_SUMMARY_CHARS),
        "files": files,
        "content_hash": content_hash,
        "attributes": attributes,
    }
    size = len(json.dumps(normalized).encode("utf-8"))
    if size > _MAX_EVENT_BYTES:
        return None, [f"event exceeds {_MAX_EVENT_BYTES} bytes ({size})"]
    return normalized, []


# ── host-hook emission ───────────────────────────────────────────────────────────
#
# What a host adapter calls. An adapter owns its hook's output contract and nothing else,
# so every field a hook cannot know (ids, the clock, the defaults) is filled in here and a
# fourth adapter emits the same normalized event as the first three instead of a fourth
# hand-built dict.


# The `source` an agent-reported conclusion carries: not a hook, and named so a reader of the
# spool can tell it apart from anything an adapter observed.
_CONCLUSION_SOURCE = "agent_tool"

_CONCLUSION_RECEIPT = (
    "Recorded as EVIDENCE, not as a decision. Nothing was stored, approved, trusted or "
    "injected: reconciliation groups it with this session's other evidence, and only the "
    "developer's review can turn it into a decision. Tell them what you concluded - do not "
    "tell them it was saved."
)


def emit_hook_event(repo_path: str, kind: str, *, session_id: str = "", source: str = "",
                    summary: str = "", files=None, attributes=None) -> dict:
    """Build one event out of what a hook knows and spool it, returning
    `spool.append_evidence`'s result. NEVER raises: that call already promises as much, and
    the dict build is wrapped too, so a call site inside a hook cannot fail over the spool.

    `repo_key` is the repo the CALLER already resolved for its existing work - never
    re-resolved here. An event keyed through a different chain than the spool it lands in
    is exactly the writer/reader split this repo has shipped twice (post_write's slug, the
    team-poll session id), and re-resolving inside the emitter would invite a third.

    An absent host session id becomes "unknown" rather than failing validation: an event that
    cannot be grouped by session is still evidence of the thing that happened.
    """
    try:
        # Function-level so the dependency is one-way at import time: `spool` imports this
        # module for `validate_event`, and a top-level import back would couple their load
        # order. Inside the try like everything else here - the never-raises contract covers
        # the import too.
        from contexer import spool

        return spool.append_evidence(repo_path, {
            "schema_version": SCHEMA_VERSION,
            "event_id": str(uuid.uuid4()),
            "session_id": session_id or "unknown",
            "repo_key": repo_path,
            "kind": kind,
            "occurred_at": datetime.now(timezone.utc).isoformat(),
            "source": source,
            "summary": summary,
            "files": list(files) if files else [],
            "attributes": dict(attributes) if attributes else {},
        })
    except Exception as exc:           # broad on purpose: the never-raises contract
        return {"status": "dropped_error", "errors": [f"{type(exc).__name__}: {exc}"]}


def record_agent_conclusion(repo_path: str, summary: str, *, rationale: str = "",
                            files=None, session_id: str = "",
                            source: str = _CONCLUSION_SOURCE) -> tuple[bool, str]:
    """Spool ONE `agent_conclusion` event for a conclusion an agent reached. `(ok, receipt)`.

    The only production emitter of the kind, and deliberately a thin one: it records what an
    agent SAYS it worked out, and nothing downstream treats that as observed. It never writes
    a decision, approves, anchors, retires, restores or arms anything - the event goes to the
    spool, reconciliation groups it, and the developer's review is the only thing that can
    turn it into stored knowledge.

    Two gates in front of the spool, both reusing rules that already exist:

    * a blank conclusion is refused rather than spooled - an event with no statement can never
      seed a candidate, so it would be a file nothing can read;
    * `store.capture_lint` runs on the same text `update_context` would lint. Without it this
      door is a LINT BYPASS: a narrative-shaped or multi-claim capture the write tool bounced
      could be re-submitted here, reach the store through reconciliation's ordinary capture
      path (which is `store.update_decision`, not the linted server tool), and land pending
      review in exactly the shape the lint exists to reshape.

    An append failure REPORTS the loss and returns `False`; it never reads as capture.
    """
    text = " ".join(part.strip() for part in (summary, rationale)
                    if isinstance(part, str) and part.strip())
    if not text:
        return False, "Nothing recorded - a conclusion needs a summary."
    lint = store.capture_lint(text, created_by="ai")
    if lint:
        return False, lint.replace("update_context", "record_agent_conclusion")
    explained = bool(isinstance(rationale, str) and rationale.strip())
    result = emit_hook_event(repo_path, "agent_conclusion", session_id=session_id,
                             source=source, summary=text, files=files,
                             attributes={"reported_by": "agent", "has_rationale": explained})
    if result.get("status") != "stored":
        return False, (f"NOT recorded - the conclusion could not be spooled "
                       f"({'; '.join(result.get('errors') or ['unknown error'])}). It was not "
                       f"captured anywhere: state it to the developer in this turn instead.")
    return True, _CONCLUSION_RECEIPT


# ── host capture coverage ────────────────────────────────────────────────────────
#
# What a host can actually observe, reported honestly. Each adapter owns its own static map
# (`EVIDENCE_COVERAGE`) because the hooks it installs are the only thing that decides the
# answer; this module owns the vocabulary, the manual/unknown fallback, and the rendering.
#
# The invariant is one-directional: a runtime status may DOWNGRADE a field, never raise one.
# An agent invoking `record_agent_conclusion` is `model_reported`, never `captured` - no host
# hands a hook the assistant's own response, so claiming observation would be a lie told in
# the one surface built to say what is actually seen.

COVERAGE_FIELDS = ("user_directives", "file_changes", "assistant_conclusions",
                   "test_results", "diffs")
CAPTURE_STATES = frozenset({"captured", "model_reported", "unavailable", "error"})
RECONCILIATION_STATES = frozenset({"complete", "partial", "skipped", "error"})

# No host adapter in the loop: the MCP tool and the CLI reach this repo from an unknown
# client, so only what the client itself reports is available.
_MANUAL_COVERAGE = {
    "user_directives": "unavailable",
    "file_changes": "unavailable",
    "assistant_conclusions": "model_reported",
    "test_results": "unavailable",
    "diffs": "unavailable",
}

_COVERAGE_LABELS = (
    ("user_directives", "directives"),
    ("file_changes", "file changes"),
    ("assistant_conclusions", "conclusions"),
    ("test_results", "test results"),
    ("diffs", "diffs"),
)
_STATE_WORDS = {"captured": "captured", "model_reported": "agent-reported",
                "unavailable": "unavailable", "error": "error"}


def host_coverage(host: str = "", *, reconciliation: str = "complete",
                  dropped_events: int = 0) -> dict:
    """The coverage block for one host: its static capabilities plus this run's status.

    `host` is an adapter name; anything else (including "") resolves to `manual` rather than
    guessing, since a coverage block naming a host that did not observe anything is worse
    than one that says so. An adapter value outside `CAPTURE_STATES` becomes `error` - a
    typo in a static map must degrade to "this was not checked", never to a capture claim.

    Evidence that left the spool unreconciled makes the pass `partial`: it cannot claim to
    have accounted for everything it was given. That is the only upgrade direction blocked
    here too - a caller-supplied state is never improved.
    """
    static, name = _MANUAL_COVERAGE, "manual"
    if host:
        # Call-time import: the adapters import this module, and an unnamed host must not
        # pay to load all four of them on reconciliation's own fast path.
        from contexer import adapters
        try:
            static, name = adapters.get(host).EVIDENCE_COVERAGE, host
        except (KeyError, AttributeError):
            static, name = _MANUAL_COVERAGE, "manual"
    dropped = dropped_events if isinstance(dropped_events, int) \
        and not isinstance(dropped_events, bool) and dropped_events > 0 else 0
    if reconciliation not in RECONCILIATION_STATES:
        reconciliation = "error"
    if dropped and reconciliation == "complete":
        reconciliation = "partial"
    block = {"host": name}
    for field in COVERAGE_FIELDS:
        value = static.get(field)
        block[field] = value if value in CAPTURE_STATES else "error"
    block["reconciliation"] = reconciliation
    block["dropped_events"] = dropped
    return block


def format_coverage(block, *, pass_status: bool = True) -> str:
    """One line of coverage. States a capability, never a count: "file changes unavailable"
    and a spool holding zero file events are different facts, and collapsing them is what
    makes a host with no write hook look like a quiet session.

    `pass_status=False` renders the CAPABILITY half alone, for a caller that ran no
    reconciliation pass. The two runtime fields default to `complete`/`0`, so rendering them
    outside a real pass asserts an outcome that never happened - `contexer status` printed
    "reconciliation complete, 0 events dropped" beside a count of unconsumed evidence, and
    printed it for hosts that have no reconciliation checkpoint at all. A pass outcome belongs
    to the receipt of a pass.
    """
    parts = [f"{label} {_STATE_WORDS.get(block.get(field), 'error')}"
             for field, label in _COVERAGE_LABELS]
    line = f"{block.get('host', 'manual')}: " + ", ".join(parts)
    if not pass_status:
        return line
    dropped = block.get("dropped_events", 0)
    return (line + f"; reconciliation {block.get('reconciliation', 'error')}"
            + f", {dropped} event{'' if dropped == 1 else 's'} dropped")


def capture_directive(repo_path: str, prompt: str, session_id: str, source: str,
                      *, near: list | None = None, repo_source: str = "") -> tuple:
    """`store.capture_user_constraint` plus the `user_directive` event for it - the one
    definition every host's per-prompt constraint hook shares.

    Returns and raises EXACTLY what the store's 3-tuple contract does, so no hook's existing
    behaviour changes. Three gates, each honest about what is actually known:

    * the store stored or updated an entry - the event is emitted for it;
    * the store recorded a RECURRENCE (`meta["recurrence"]`, the developer restating a rule the
      store already holds). This used to emit nothing at all, which was outstanding issue 3:
      the second time a rule was stated there was no trace of it anywhere in the ledger. The
      event is an ordinary `user_directive` carrying the sanitized content, so the aggregator
      matches it onto the decision it duplicates and settles it there - which is what keeps a
      repeated rule from becoming a second pending decision. `decision_repeated` stays a valid
      kind with no emitter: it means "some OTHER thing observed a repetition", and inventing it
      here would open a group nothing can settle, since a lone repetition scores below the
      review bar;
    * the store RAISES - the loss this ledger exists to record - and the event is emitted only
      if the store's own detector says the prompt was a directive, marked `unverified` because
      no entry exists to prove it. The detector is reached through its public alias; a second
      copy of "what counts as a directive" would drift from the first.

    `source` is passed into the store call as well as onto the event, so the recurrence history
    row records which host prompt hook restated the rule.
    """
    try:
        entry_id, content, status, meta = store.capture_user_constraint_with_meta(
            repo_path, prompt, session_id, near, repo_source=repo_source, source=source)
    except Exception:
        # Suppressed, not merged into the outer handler: a failure while RECORDING the loss
        # must not replace the exception the caller's own error path is about to see.
        with contextlib.suppress(Exception):
            if store.is_prescriptive_directive(prompt)[0]:
                emit_hook_event(repo_path, "user_directive", session_id=session_id,
                                source=source, summary=prompt,
                                attributes={"unverified": True})
        raise
    repeated = meta.get("recurrence") or {}
    if entry_id is not None:
        emit_hook_event(repo_path, "user_directive", session_id=session_id,
                        source=source, summary=content)
    elif repeated.get("content"):
        emit_hook_event(repo_path, "user_directive", session_id=session_id,
                        source=source, summary=repeated["content"])
    return entry_id, content, status
