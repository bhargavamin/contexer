"""Fail-soft local diagnostics for automatic decision-proposal operations.

The proposal workflow runs in editor hooks and must never wait for or depend on telemetry.
Records therefore go to a bounded in-process queue and a daemon writer delivers JSONL to stderr.
There is deliberately no network exporter. The public emitter accepts only closed-vocabulary
outcomes and never accepts exceptions, tokens, endpoints, account fingerprints, repository
identities, or decision prose, keeping sensitive values out of both span attributes and logs.
"""
from __future__ import annotations

import json
import queue
import re
import secrets
import sys
import threading
import time
import uuid
from collections.abc import Iterable

_FIXED_MESSAGE = "decision operation completed"
_MAX_PENDING_RECORD_SETS = 1024
_DIAGNOSTIC_RE = re.compile(r"diag_[A-Z0-9]{16}\Z")

_OPERATIONS = {
    "capabilityRead": (
        "decision_proposal.capability_read",
        "decisionProposalCapabilityRead",
    ),
    "policyChange": (
        "decision_proposal.policy_change",
        "decisionProposalPolicyChange",
    ),
    "scan": (
        "decision_proposal.scan",
        "decisionProposalScan",
    ),
    "enqueue": (
        "decision_proposal.enqueue",
        "decisionProposalEnqueue",
    ),
    "drain": (
        "decision_proposal.drain",
        "decisionProposalDrain",
    ),
}
_RESULTS = {
    "success", "queued", "skipped", "no_op", "refused", "failure",
    "submitted", "already_pending", "unchanged", "attention", "conflict", "retry",
}
_REASON_CODES = {
    "none",
    "unsupported_protocol",
    "account_mismatch",
    "policy_mismatch",
    "repo_mismatch",
    "team_mismatch",
    "not_member",
    "not_authorized",
    "ineligible_revision",
    "global_decision",
    "baseline_revision",
    "duplicate_receipt",
    "corrupt_queue",
    "lock_busy",
    "policy_disabled",
    "cancelled",
    "rate_limited",
    "quota_exceeded",
    "trial_expired",
    "stale_head",
    "stale_intent",
    "transient_error",
    "transport_error",
    "validation_error",
}
_ERROR_CLASSES = {
    "none",
    "authorization",
    "capability",
    "conflict",
    "lock",
    "rate_limit",
    "transport",
    "validation",
    "telemetry",
}

_PENDING: queue.Queue[str] = queue.Queue(maxsize=_MAX_PENDING_RECORD_SETS)
_WORKER_LOCK = threading.Lock()
_WORKER: threading.Thread | None = None


def _write_pending() -> None:
    while True:
        encoded = _PENDING.get()
        try:
            # Resolve stderr at delivery time so pytest/host redirection is honored. A broken or
            # closed diagnostics sink must not affect the functional operation that emitted it.
            sys.stderr.write(encoded)
            sys.stderr.flush()
        except Exception:
            pass
        finally:
            _PENDING.task_done()


def _ensure_worker() -> None:
    global _WORKER
    if _WORKER is not None:
        return
    with _WORKER_LOCK:
        if _WORKER is None:
            _WORKER = threading.Thread(
                target=_write_pending,
                name="contexer-decision-observability",
                daemon=True,
            )
            _WORKER.start()


def _enqueue_records(records: Iterable[dict]) -> None:
    """Enqueue a complete span/log pair without waiting for sink delivery."""
    encoded = "".join(
        json.dumps(record, separators=(",", ":"), sort_keys=True) + "\n"
        for record in records
    )
    _ensure_worker()
    _PENDING.put_nowait(encoded)


def flush_pending(timeout: float = 1.0) -> bool:
    """Boundedly flush already-enqueued records before a detached worker process exits.

    Normal callers remain enqueue-and-return. This shutdown seam is for the independent uploader
    process, whose private stderr is a regular local file; sink failures still call ``task_done``
    and telemetry can never change the proposal outcome.
    """
    if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout < 0:
        return False
    deadline = time.monotonic() + float(timeout)
    try:
        with _PENDING.all_tasks_done:
            while _PENDING.unfinished_tasks:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                _PENDING.all_tasks_done.wait(remaining)
        return True
    except Exception:
        return False


def emit_decision_operation(
    operation: str,
    *,
    result: str,
    reason_code: str,
    error_class: str,
    started_ns: int | None = None,
    diagnostic_id: str | None = None,
    team_id: str | None = None,
    decision_id: str | None = None,
    candidate_id: str | None = None,
    policy_generation: str | None = None,
    idempotency_key: str | None = None,
    attempt: int | None = None,
    queue_depth: int | None = None,
    replayed: bool | None = None,
) -> None:
    """Best-effort enqueue of one correlated span and terminal structured-log event.

    The API is intentionally incapable of receiving raw failures or request values. Invalid
    caller-provided vocabulary collapses to a fixed validation failure instead of being copied
    into diagnostics. Queue saturation, thread creation failure, serialization failure, and a
    broken stderr sink are all swallowed: telemetry is never part of the operation's outcome.
    """
    try:
        span_name, action = _OPERATIONS[operation]
        if result not in _RESULTS or reason_code not in _REASON_CODES or error_class not in _ERROR_CLASSES:
            result = "failure"
            reason_code = "validation_error"
            error_class = "telemetry"

        trace_id = secrets.token_hex(16)
        span_id = secrets.token_hex(8)
        if diagnostic_id is not None and not _DIAGNOSTIC_RE.fullmatch(diagnostic_id):
            diagnostic_id = None
        if result == "failure" and diagnostic_id is None:
            diagnostic_id = f"diag_{secrets.token_hex(8).upper()}"
        ended_ns = time.monotonic_ns()
        duration_ms = (
            max(0, ended_ns - started_ns) / 1_000_000
            if type(started_ns) is int else 0.0
        )

        span_attributes = {
            "contexer.result": result,
            "contexer.reason_code": reason_code,
            "contexer.error_class": error_class,
        }
        log_fields = {
            "action": action,
            "result": result,
            "reasonCode": reason_code,
            "errorClass": error_class,
        }
        if diagnostic_id is not None:
            span_attributes["contexer.diagnostic_id"] = diagnostic_id
            log_fields["diagnosticId"] = diagnostic_id

        # Correlation values are emitted only when they are canonical opaque UUIDs. The
        # persisted schema is intentionally more permissive for compatibility, so copying an
        # arbitrary identifier here could turn attacker-controlled prose into telemetry.
        correlations = (
            ("team_id", "teamId", team_id),
            ("decision_id", "decisionId", decision_id),
            ("candidate_id", "candidateId", candidate_id),
            ("policy_generation", "policyGeneration", policy_generation),
            ("idempotency_key", "idempotencyKey", idempotency_key),
        )
        for span_field, log_field, value in correlations:
            if not isinstance(value, str):
                continue
            try:
                canonical = str(uuid.UUID(value))
            except (ValueError, AttributeError):
                continue
            if value.lower() != canonical:
                continue
            span_attributes[f"contexer.{span_field}"] = canonical
            log_fields[log_field] = canonical
        for span_field, log_field, value in (
            ("attempt", "attempt", attempt),
            ("queue_depth", "queueDepth", queue_depth),
        ):
            if type(value) is int and 0 <= value <= 1_000_000:
                span_attributes[f"contexer.{span_field}"] = value
                log_fields[log_field] = value
        if type(replayed) is bool:
            span_attributes["contexer.replayed"] = replayed
            log_fields["replayed"] = replayed

        _enqueue_records((
            {
                "recordType": "span",
                "traceId": trace_id,
                "spanId": span_id,
                "name": span_name,
                "durationMs": duration_ms,
                "attributes": span_attributes,
                "events": [],
            },
            {
                "recordType": "log",
                "traceId": trace_id,
                "spanId": span_id,
                "message": _FIXED_MESSAGE,
                "fields": log_fields,
            },
        ))
    except Exception:
        pass
