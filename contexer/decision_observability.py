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
import secrets
import sys
import threading
import time
from collections.abc import Iterable

_FIXED_MESSAGE = "decision operation completed"
_MAX_PENDING_RECORD_SETS = 1024

_OPERATIONS = {
    "capabilityRead": (
        "decision_proposal.capability_read",
        "decisionProposalCapabilityRead",
    ),
}
_RESULTS = {"success", "refused", "failure"}
_REASON_CODES = {
    "none",
    "unsupported_protocol",
    "not_authorized",
    "rate_limited",
    "transport_error",
    "validation_error",
}
_ERROR_CLASSES = {
    "none",
    "authorization",
    "capability",
    "rate_limit",
    "transport",
    "validation",
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


def emit_decision_operation(
    operation: str,
    *,
    result: str,
    reason_code: str,
    error_class: str,
    started_ns: int | None = None,
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
        diagnostic_id = f"diag_{secrets.token_hex(8)}" if result == "failure" else None
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
