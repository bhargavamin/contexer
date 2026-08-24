"""Tests for contexer/evidence.py — the evidence-ledger event schema and pure validator.

`validate_event` is a pure function: it returns `(normalized_copy, [])` or `(None, errors)`,
collects EVERY structural error rather than the first, never raises, and never mutates its
input. Golden fixtures under `tests/fixtures/evidence/` carry the cases; the two iteration
tests below pick up any fixture added later with no new test code.
"""
import json
from pathlib import Path

import pytest

from contexer import evidence

FIXTURES = Path(__file__).parent / "fixtures" / "evidence"
VALID = sorted((FIXTURES / "valid").glob("*.json"))
INVALID = sorted((FIXTURES / "invalid").glob("*.json"))

GOOD = {
    "schema_version": 1,
    "event_id": "3f2504e0-4f89-11d3-9a0c-0305e82c3301",
    "session_id": "sess-1",
    "repo_key": "_Users_dev_repo",
    "kind": "file_changed",
    "occurred_at": "2026-08-24T10:00:00+00:00",
    "source": "codex_post_tool_use",
    "summary": "Authentication middleware changed",
    "files": ["src/auth.py"],
    "content_hash": None,
    "attributes": {},
}


def _event(**overrides):
    return {**GOOD, **overrides}


# ── golden fixtures ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_valid_fixture_validates(path):
    raw = json.loads(path.read_text())
    normalized, errors = evidence.validate_event(raw)
    assert errors == []
    assert normalized is not None
    assert normalized["kind"] == raw["kind"]


def test_valid_fixtures_cover_every_kind():
    kinds = {json.loads(p.read_text())["kind"] for p in VALID}
    assert kinds == set(evidence.EVENT_KINDS)


@pytest.mark.parametrize("path", INVALID, ids=lambda p: p.stem)
def test_invalid_fixture_rejected(path):
    case = json.loads(path.read_text())
    normalized, errors = evidence.validate_event(case["event"])
    assert normalized is None
    assert any(case["reason_contains"] in e for e in errors), errors


# ── contract ─────────────────────────────────────────────────────────────────

def test_errors_accumulate():
    normalized, errors = evidence.validate_event(
        _event(schema_version=2, kind="nope", event_id="not-a-uuid"))
    assert normalized is None
    assert len(errors) == 3


@pytest.mark.parametrize("bad", [None, 42, "an event", ["an", "event"], object()])
def test_non_mapping_input_never_raises(bad):
    normalized, errors = evidence.validate_event(bad)
    assert normalized is None
    assert len(errors) == 1


@pytest.mark.parametrize("field", sorted(GOOD))
@pytest.mark.parametrize("value", [[1, 2], {"a": 1}, None, 3.5, b"bytes", object()],
                         ids=["list", "dict", "none", "float", "bytes", "object"])
def test_hostile_field_value_reports_errors_and_never_raises(field, value):
    """The never-raises contract is module-wide, not just for a non-mapping event: a malformed
    value in ANY field must come back as an error. `kind` was the one field that raised — its
    membership test hashes the raw value, so an unhashable one escaped as TypeError."""
    normalized, errors = evidence.validate_event({**GOOD, field: value})
    assert (normalized is None) == bool(errors)


@pytest.mark.parametrize("kind", [["file_changed"], {"kind": "file_changed"}])
def test_unhashable_kind_is_an_error_not_a_crash(kind):
    normalized, errors = evidence.validate_event(_event(kind=kind))
    assert normalized is None
    assert any("kind must be one of" in e for e in errors), errors


def test_input_is_never_mutated():
    raw = _event(files=[f"src/f{i}.py" for i in range(25)], summary="x" * 900,
                 attributes={"a": "y" * 900})
    before = json.dumps(raw, sort_keys=True)
    evidence.validate_event(raw)
    assert json.dumps(raw, sort_keys=True) == before


def test_optional_fields_default():
    normalized, errors = evidence.validate_event(
        {k: v for k, v in GOOD.items()
         if k not in ("summary", "files", "content_hash", "attributes")})
    assert errors == []
    assert normalized["summary"] == ""
    assert normalized["files"] == []
    assert normalized["content_hash"] is None
    assert normalized["attributes"] == {}


def test_occurred_at_normalized_to_utc():
    normalized, errors = evidence.validate_event(_event(occurred_at="2026-08-24T12:00:00+02:00"))
    assert errors == []
    assert normalized["occurred_at"] == "2026-08-24T10:00:00+00:00"


# ── bounded normalization ────────────────────────────────────────────────────

def test_files_truncated_and_total_recorded():
    normalized, errors = evidence.validate_event(
        _event(files=[f"src/f{i}.py" for i in range(25)]))
    assert errors == []
    assert len(normalized["files"]) == evidence._MAX_FILES
    assert normalized["files"][0] == "src/f0.py"
    assert normalized["attributes"]["files_total"] == 25


def test_files_not_truncated_records_no_total():
    normalized, _ = evidence.validate_event(_event(files=["src/a.py", "src/b.py"]))
    assert "files_total" not in normalized["attributes"]


def test_summary_clipped():
    normalized, errors = evidence.validate_event(_event(summary="x" * 900))
    assert errors == []
    assert len(normalized["summary"]) == evidence._MAX_SUMMARY_CHARS
    assert normalized["summary"].endswith("…")


def test_attribute_string_values_clipped():
    normalized, errors = evidence.validate_event(_event(attributes={"note": "y" * 900}))
    assert errors == []
    assert len(normalized["attributes"]["note"]) == evidence._MAX_ATTR_VALUE_CHARS


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_non_finite_attribute_rejected(bad):
    """json.dumps writes NaN/Infinity and no JSON reader accepts them back, so such an
    attribute would be a ledger line that never parses again."""
    normalized, errors = evidence.validate_event(_event(attributes={"ratio": bad}))
    assert normalized is None
    assert any("finite" in e for e in errors), errors


def test_secrets_scrubbed_from_summary_and_attributes():
    token = "github_pat_" + "A1b2C3d4E5" * 4
    normalized, errors = evidence.validate_event(
        _event(summary=f"pushed with {token}", attributes={"cmd": f"auth {token}"}))
    assert errors == []
    assert token not in normalized["summary"]
    assert token not in normalized["attributes"]["cmd"]
    assert "[REDACTED:" in normalized["summary"]
    assert "[REDACTED:" in normalized["attributes"]["cmd"]


def test_validating_a_normalized_event_is_a_no_op():
    once, errors = evidence.validate_event(
        _event(summary="x" * 900 + " github_pat_" + "A1b2C3d4E5" * 4,
               files=[f"src/f{i}.py" for i in range(25)],
               occurred_at="2026-08-24T12:00:00+02:00",
               attributes={"note": "z" * 900}))
    assert errors == []
    twice, errors = evidence.validate_event(once)
    assert errors == []
    assert twice == once


def test_files_total_does_not_consume_a_caller_attribute_slot():
    """Normalization ADDS `files_total`, so an event using all 20 caller slots must still
    re-validate — otherwise replaying a normalized event is not the no-op above."""
    once, errors = evidence.validate_event(
        _event(files=[f"src/f{i}.py" for i in range(25)],
               attributes={f"k{i}": i for i in range(evidence._MAX_ATTRS)}))
    assert errors == []
    assert len(once["attributes"]) == evidence._MAX_ATTRS + 1
    assert evidence.validate_event(once) == (once, [])


# ── size cap ─────────────────────────────────────────────────────────────────

def test_oversized_event_rejected():
    normalized, errors = evidence.validate_event(
        _event(attributes={f"k{i}": "v" * 500 for i in range(evidence._MAX_ATTRS)}))
    assert normalized is None
    assert any("exceeds" in e for e in errors), errors


def test_large_but_under_cap_event_is_accepted():
    normalized, errors = evidence.validate_event(
        _event(attributes={f"k{i}": "v" * 500 for i in range(10)}))
    assert errors == []
    assert len(json.dumps(normalized).encode()) <= evidence._MAX_EVENT_BYTES
