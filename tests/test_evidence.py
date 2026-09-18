"""Tests for contexer/evidence.py - the evidence event schema and its validator.

`validate_event` is a pure function: it returns `(normalized_copy, [])` or `(None, errors)`,
collects EVERY structural error rather than the first, never raises, and never mutates its
input. Golden fixtures under `tests/fixtures/evidence/` carry the cases; the two iteration
tests below pick up any fixture added later with no new test code.

STORAGE IS TESTED IN tests/test_spool.py, not here: evidence.py owns the schema and the
host-hook emission surface, and every filesystem property (atomic per-event writes, holds,
quarantine, retention, `.gap`) belongs to the module that does the writing. The HOOK emission
surface's own tests live in tests/test_evidence_adapters.py, beside the hosts that call it;
the agent-conclusion emitter and the host-coverage block are tested here, because neither
belongs to any one adapter - the coverage vocabulary and the never-upgrade rule live in this
module while each adapter owns only its own static map.
"""
import json
from pathlib import Path

import pytest

from contexer import evidence, spool

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
    value in ANY field must come back as an error. `kind` was the one field that raised - its
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
    re-validate - otherwise replaying a normalized event is not the no-op above."""
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


# ── the agent-conclusion emitter ─────────────────────────────────────────────
#
# The one production emitter of `agent_conclusion`. Its END-TO-END behaviour (what the
# aggregator does with what it spools, and that a lint-bounced capture cannot re-enter the
# store through it) is pinned in tests/test_evidence_hardening_replays.py; what is here is
# the emitter's own contract: shape, bounds, and an honest report of a failed append.

CONCLUSION = "The codegen step overwrites the client."


def test_a_recorded_conclusion_is_one_agent_reported_event(tmp_repo):
    ok, message = evidence.record_agent_conclusion(
        tmp_repo, CONCLUSION, rationale="It runs on every build.",
        files=["src/a.py"], session_id="sess-1")

    assert ok and "EVIDENCE" in message
    (event,) = spool.list_pending_evidence(tmp_repo, "sess-1")
    assert event["kind"] == "agent_conclusion"
    assert event["source"] == "agent_tool"
    assert event["summary"] == "The codegen step overwrites the client. It runs on every build."
    assert event["files"] == ["src/a.py"]
    assert event["attributes"] == {"reported_by": "agent", "has_rationale": True}


def test_a_conclusion_without_a_rationale_says_so(tmp_repo):
    assert evidence.record_agent_conclusion(tmp_repo, CONCLUSION, session_id="s")[0]
    (event,) = spool.list_pending_evidence(tmp_repo, "s")
    assert event["attributes"]["has_rationale"] is False
    assert event["summary"] == CONCLUSION


@pytest.mark.parametrize("summary,rationale", [("", ""), ("   ", "  "), (None, None)])
def test_a_blank_conclusion_is_refused_rather_than_spooled(tmp_repo, summary, rationale):
    # An event with no statement can never seed a candidate, so spooling one would write a
    # file nothing can ever read.
    ok, message = evidence.record_agent_conclusion(tmp_repo, summary, rationale=rationale)
    assert (ok, message) == (False, "Nothing recorded - a conclusion needs a summary.")
    assert spool.list_pending_evidence(tmp_repo) == []


def test_an_escaping_path_is_reported_as_loss_not_silently_dropped(tmp_repo):
    # The schema's own path rule, reused rather than restated here: nothing is spooled, and
    # the caller is told the conclusion was NOT captured.
    ok, message = evidence.record_agent_conclusion(tmp_repo, CONCLUSION, files=["/etc/passwd"])
    assert not ok
    assert "NOT recorded" in message and "repo-relative" in message
    assert spool.list_pending_evidence(tmp_repo) == []


def test_a_failed_append_reports_the_loss_and_never_claims_capture(tmp_repo, monkeypatch):
    with monkeypatch.context() as broken:
        broken.setattr(spool, "append_evidence",
                       lambda *_a, **_k: {"status": "dropped_error", "errors": ["disk is on fire"]})
        ok, message = evidence.record_agent_conclusion(tmp_repo, CONCLUSION)
    assert not ok
    assert "NOT recorded" in message and "disk is on fire" in message
    assert "state it to the developer" in message


# ── host capture coverage ────────────────────────────────────────────────────

def test_no_host_reports_manual_and_claims_nothing_it_cannot_see():
    block = evidence.host_coverage()
    assert block == {"host": "manual", "user_directives": "unavailable",
                     "file_changes": "unavailable",
                     "assistant_conclusions": "model_reported",
                     "test_results": "unavailable", "diffs": "unavailable",
                     "reconciliation": "complete", "dropped_events": 0}


def test_a_named_host_reports_its_own_adapter_map():
    from contexer.adapters import claude

    block = evidence.host_coverage("claude")
    assert block["host"] == "claude"
    assert {f: block[f] for f in evidence.COVERAGE_FIELDS} == claude.EVIDENCE_COVERAGE


@pytest.mark.parametrize("host", ["nonesuch", "Claude", " "])
def test_an_unknown_host_falls_back_to_manual_rather_than_guessing(host):
    assert evidence.host_coverage(host)["host"] == "manual"


def test_a_static_value_outside_the_vocabulary_degrades_to_error(monkeypatch):
    # A typo in an adapter's map must read as "this was not checked", never as a capture
    # claim - the only direction this block is allowed to move at runtime.
    from contexer.adapters import cursor

    with monkeypatch.context() as typo:
        typo.setattr(cursor, "EVIDENCE_COVERAGE",
                     dict(cursor.EVIDENCE_COVERAGE, user_directives="capturd"))
        assert evidence.host_coverage("cursor")["user_directives"] == "error"


def test_dropped_events_downgrade_the_pass_and_are_counted():
    block = evidence.host_coverage("claude", dropped_events=3)
    assert (block["reconciliation"], block["dropped_events"]) == ("partial", 3)
    # A capability is never downgraded by a drop nobody attributed to it.
    assert block["file_changes"] == "captured"


@pytest.mark.parametrize("state", ["skipped", "error", "partial"])
def test_a_reported_state_is_never_improved_by_the_drop_rule(state):
    assert evidence.host_coverage("claude", reconciliation=state,
                                  dropped_events=2)["reconciliation"] == state


def test_an_unknown_reconciliation_state_reads_as_error():
    assert evidence.host_coverage(reconciliation="finished")["reconciliation"] == "error"


@pytest.mark.parametrize("dropped", [-1, True, "many", None])
def test_a_nonsense_drop_count_reads_as_zero(dropped):
    assert evidence.host_coverage(dropped_events=dropped)["dropped_events"] == 0


def test_rendering_distinguishes_unavailable_from_a_zero_count():
    # The honesty the block exists for: cursor sees no edits at all, which is a different
    # fact from claude having observed none this session. Neither line states a count.
    cursor_line = evidence.format_coverage(evidence.host_coverage("cursor"))
    claude_line = evidence.format_coverage(evidence.host_coverage("claude"))
    assert "file changes unavailable" in cursor_line
    assert "file changes captured" in claude_line
    assert "0 file changes" not in cursor_line
    assert "conclusions agent-reported" in cursor_line and "captured" not in \
        cursor_line.split("conclusions")[1]


def test_rendering_names_the_pass_status_and_its_drops():
    line = evidence.format_coverage(evidence.host_coverage("gemini", dropped_events=1))
    assert line.startswith("gemini: ")
    assert line.endswith("; reconciliation partial, 1 event dropped")


def test_a_caller_that_ran_no_pass_renders_capabilities_alone():
    # The two runtime fields default to complete/0, so a caller with no pass to report on must
    # not render them: printing a default as an outcome is the one lie this block exists to
    # stop. `contexer status` is that caller.
    block = evidence.host_coverage("claude")
    line = evidence.format_coverage(block, pass_status=False)
    assert line == "claude: directives captured, file changes captured, " \
                   "conclusions agent-reported, test results unavailable, diffs unavailable"
    assert "reconciliation" not in line and "dropped" not in line
    assert evidence.format_coverage(block).startswith(line + ";")
