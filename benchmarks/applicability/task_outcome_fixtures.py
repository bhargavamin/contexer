"""Executable candidate artifacts for the Contract-06 offline outcome experiment.

These modules are copied into a disposable artifact root and executed by the separate
reviewer-owned validator.  They are deliberately small, deterministic programs: the point is
to test the measurement boundary, not to imitate a production repository.
"""

from __future__ import annotations


IMPLEMENTATIONS: dict[str, str] = {
    "payment.good": '''
REVISION = "v2"

def retry_plan(operation_id, attempts):
    return [
        {"attempt": attempt, "delay": min(2 ** attempt, 8),
         "idempotency_key": operation_id}
        for attempt in range(attempts)
    ]
''',
    "payment.wrong_revision": '''
REVISION = "v1"

def retry_plan(operation_id, attempts):
    return [
        {"attempt": attempt, "delay": min(2 ** attempt, 8),
         "idempotency_key": operation_id}
        for attempt in range(attempts)
    ]
''',
    "payment.unbounded_non_idempotent": '''
REVISION = "v2"

def retry_plan(operation_id, attempts):
    return [
        {"attempt": attempt, "delay": 2 ** (attempt + 4),
         "idempotency_key": f"{operation_id}-{attempt}"}
        for attempt in range(attempts)
    ]
''',
    "payment.baseline": '''
REVISION = "v1"

def retry_plan(operation_id, attempts):
    return []
''',
    "redaction.good": '''
def write_audit(payload):
    return {key: value for key, value in payload.items() if key != "secret"}
''',
    "redaction.leaks_secret": '''
def write_audit(payload):
    return dict(payload)
''',
    "redaction.baseline": '''
def write_audit(payload):
    return None
''',
    "migration.good": '''
def migrate(record, cohort):
    if cohort >= 50:
        return dict(record)
    migrated = dict(record)
    migrated["schema_version"] = 2
    migrated["display_name"] = migrated["legacy_name"]
    return migrated
''',
    "migration.baseline": '''
def migrate(record, cohort):
    return dict(record)
''',
    "cache.good": '''
def invalidate(namespace, key, ttl):
    return {"invalidated": True, "key": f"v2:{namespace}:{key}",
            "expires_in": min(ttl, 300)}
''',
    "cache.unbounded": '''
def invalidate(namespace, key, ttl):
    return {"invalidated": True, "key": f"v2:{namespace}:{key}",
            "expires_in": ttl}
''',
    "cache.baseline": '''
def invalidate(namespace, key, ttl):
    return {"invalidated": False, "key": f"{namespace}:{key}", "expires_in": ttl}
''',
    "auth.good": '''
def refresh(token, revoked):
    if token in revoked:
        raise PermissionError("revoked")
    return f"rotated:{token}"
''',
    "auth.ignores_revocation": '''
def refresh(token, revoked):
    return token
''',
    "auth.baseline": '''
def refresh(token, revoked):
    raise RuntimeError("refresh disabled")
''',
    "logging.good": '''
def log_request(payload):
    return {key: value for key, value in payload.items() if key != "secret"}
''',
    "logging.baseline": '''
def log_request(payload):
    return None
''',
}


MODULE_FILENAMES = {
    "payment_retry": "payment_retry.py",
    "audit_redaction": "audit.py",
    "schema_migration": "migration.py",
    "cache_invalidation": "cache.py",
    "token_refresh": "auth.py",
    "request_logging": "logging.py",
}
