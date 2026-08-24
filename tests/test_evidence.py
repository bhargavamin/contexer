"""Tests for contexer/evidence.py — the evidence-ledger schema, validator, and sidecar.

`validate_event` is a pure function: it returns `(normalized_copy, [])` or `(None, errors)`,
collects EVERY structural error rather than the first, never raises, and never mutates its
input. Golden fixtures under `tests/fixtures/evidence/` carry the cases; the two iteration
tests below pick up any fixture added later with no new test code.

The storage half (from `── sidecar storage ──` down) asserts the properties a host hook
depends on: an append never raises, a busy lock or an I/O failure is RECORDED loss rather
than silence, and an unreadable ledger is reported instead of replaced.
"""
import fcntl
import json
import uuid
from pathlib import Path

import pytest

from contexer import evidence, store

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


# ── sidecar storage ──────────────────────────────────────────────────────────
#
# `tmp_repo` (conftest) points store.STORE_DIR at a temp dir; evidence.py reads that name off
# the store MODULE at call time, so the patch is seen here without evidence knowing about it.

def _appended(repo, n=1, **overrides):
    """`n` distinct events appended to `repo`, returning their ids in append order."""
    ids = []
    for i in range(n):
        event = _event(event_id=str(uuid.uuid4()),
                       occurred_at=f"2026-08-24T10:{i:02d}:00+00:00", **overrides)
        assert evidence.append_evidence(repo, event)["status"] == "stored"
        ids.append(event["event_id"])
    return ids


def _seed(repo, **overrides):
    """Write a ledger straight to disk — the only way to stage checkpoints, which Task 5 owns."""
    evidence._sidecar_path(repo).parent.mkdir(mode=0o700, exist_ok=True)
    ledger = {**evidence._empty_ledger(repo), **overrides}
    evidence._sidecar_path(repo).write_text(json.dumps(ledger), encoding="utf-8")


def _ledger(repo):
    return json.loads(evidence._sidecar_path(repo).read_text(encoding="utf-8"))


def test_append_stores_a_normalized_event_in_a_0600_sidecar(tmp_repo):
    token = "github_pat_" + "A1b2C3d4E5" * 4
    result = evidence.append_evidence(tmp_repo, _event(
        occurred_at="2026-08-24T12:00:00+02:00", summary=f"ran with {token}"))
    assert result == {"status": "stored", "errors": []}

    path = evidence._sidecar_path(tmp_repo)
    assert path.stat().st_mode & 0o777 == 0o600
    ledger = _ledger(tmp_repo)
    assert set(ledger) == {"schema_version", "repo_path", "events",
                           "candidate_checkpoints", "compacted_through", "evicted_total"}
    assert (ledger["schema_version"], ledger["candidate_checkpoints"],
            ledger["compacted_through"], ledger["evicted_total"]) == (1, {}, None, 0)
    stored, = ledger["events"]
    # Task 1's normalization is what landed on disk, not the caller's raw event.
    assert stored["occurred_at"] == "2026-08-24T10:00:00+00:00"
    assert token not in stored["summary"] and "[REDACTED:" in stored["summary"]


def test_missing_sidecar_initializes_fresh(tmp_repo):
    assert not evidence._sidecar_path(tmp_repo).exists()
    _appended(tmp_repo)
    assert len(_ledger(tmp_repo)["events"]) == 1


def test_invalid_event_is_rejected_without_a_file_or_a_gap(tmp_repo):
    """A schema rejection is a caller bug, not lost evidence — so no gap marker."""
    result = evidence.append_evidence(tmp_repo, _event(kind="nope"))
    assert result["status"] == "rejected_invalid"
    assert any("kind must be one of" in e for e in result["errors"]), result
    assert not evidence._sidecar_path(tmp_repo).exists()
    assert not evidence._gap_path(tmp_repo).exists()


def test_busy_lock_drops_the_event_and_records_the_gap(tmp_repo):
    _appended(tmp_repo)
    evidence._lock_path(tmp_repo).touch()
    with open(evidence._lock_path(tmp_repo), "wb") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = evidence.append_evidence(tmp_repo, _event(event_id=str(uuid.uuid4())))
    assert result == {"status": "dropped_busy", "errors": []}
    assert len(_ledger(tmp_repo)["events"]) == 1
    gap = json.loads(evidence._gap_path(tmp_repo).read_text())
    assert gap["drops"] == 1 and gap["last_reason"] == "busy"


@pytest.mark.parametrize("garbage", [b"{not json at all", b"[]", b'{"events": "not a list"}'],
                         ids=["unparseable", "not-an-object", "wrong-shape"])
def test_corrupt_sidecar_is_reported_never_replaced(tmp_repo, garbage):
    path = evidence._sidecar_path(tmp_repo)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_bytes(garbage)
    result = evidence.append_evidence(tmp_repo, _event())
    assert result["status"] == "dropped_error" and result["errors"]
    assert path.read_bytes() == garbage
    assert json.loads(evidence._gap_path(tmp_repo).read_text())["last_reason"] == "error"


def test_a_gap_survives_a_later_successful_append(tmp_repo):
    """Only `compact_evidence` acknowledges a gap: the loss it records already happened."""
    _seed(tmp_repo, schema_version=99)          # unreadable to this version -> one drop
    assert evidence.append_evidence(tmp_repo, _event())["status"] == "dropped_error"
    evidence._sidecar_path(tmp_repo).unlink()
    _appended(tmp_repo)
    assert json.loads(evidence._gap_path(tmp_repo).read_text())["drops"] == 1


def test_append_never_raises_when_the_store_dir_cannot_be_created(tmp_repo, monkeypatch):
    blocked = evidence._sidecar_path(tmp_repo).parent.parent / "blocked"
    blocked.write_text("a file, so mkdir underneath it cannot succeed")
    monkeypatch.setattr(store, "STORE_DIR", blocked / ".contexer")
    assert evidence.append_evidence(tmp_repo, _event())["status"] == "dropped_error"


# ── bounds ───────────────────────────────────────────────────────────────────

def test_event_cap_evicts_oldest_first_and_counts_the_loss(tmp_repo, monkeypatch):
    monkeypatch.setattr(evidence, "_MAX_EVENTS", 3)
    ids = _appended(tmp_repo, 5)
    ledger = _ledger(tmp_repo)
    assert [e["event_id"] for e in ledger["events"]] == ids[2:]
    assert ledger["evicted_total"] == 2


def test_eviction_skips_events_a_pending_checkpoint_references(tmp_repo, monkeypatch):
    monkeypatch.setattr(evidence, "_MAX_EVENTS", 2)
    ids = _appended(tmp_repo, 2)
    _seed(tmp_repo, events=_ledger(tmp_repo)["events"],
          candidate_checkpoints={"c1": {"event_ids": [ids[0]], "status": "pending"}})
    ids += _appended(tmp_repo, 1)
    kept = [e["event_id"] for e in _ledger(tmp_repo)["events"]]
    assert kept == [ids[0], ids[2]]             # the pinned oldest stayed; ids[1] went


def test_a_fully_pinned_ledger_grows_rather_than_losing_evidence(tmp_repo, monkeypatch):
    """Eviction has nothing it may take, so the cap yields. A candidate still under review
    keeps the evidence it will be judged on, whatever the ledger costs meanwhile."""
    ids = _appended(tmp_repo, 2)
    _seed(tmp_repo, events=_ledger(tmp_repo)["events"],
          candidate_checkpoints={"c1": {"event_ids": ids, "status": "pending"}})
    monkeypatch.setattr(evidence, "_MAX_EVENTS", 1)
    ids += _appended(tmp_repo, 1)
    ledger = _ledger(tmp_repo)
    assert [e["event_id"] for e in ledger["events"]] == ids[:2]
    assert ledger["evicted_total"] == 1          # the one unpinned event, then it stopped


def test_byte_cap_is_enforced(tmp_repo, monkeypatch):
    monkeypatch.setattr(evidence, "_MAX_SIDECAR_BYTES", 2500)
    _appended(tmp_repo, 6, attributes={"note": "x" * 400})
    path = evidence._sidecar_path(tmp_repo)
    assert path.stat().st_size <= 2500
    assert _ledger(tmp_repo)["evicted_total"] > 0


# ── reads ────────────────────────────────────────────────────────────────────

def test_list_session_evidence_filters_by_session_oldest_first(tmp_repo):
    mine = _appended(tmp_repo, 2, session_id="sess-1")
    _appended(tmp_repo, 1, session_id="sess-2")
    assert [e["event_id"] for e in evidence.list_session_evidence(tmp_repo, "sess-1")] == mine


@pytest.mark.parametrize("corrupt", [True, False], ids=["corrupt", "missing"])
def test_list_session_evidence_survives_an_unusable_sidecar(tmp_repo, corrupt):
    if corrupt:
        evidence._sidecar_path(tmp_repo).parent.mkdir(mode=0o700, exist_ok=True)
        evidence._sidecar_path(tmp_repo).write_text("<html>nope")
    assert evidence.list_session_evidence(tmp_repo, "sess-1") == []


def test_diagnostics_tells_a_missing_sidecar_from_a_corrupt_one(tmp_repo):
    absent = evidence.evidence_diagnostics(tmp_repo)
    assert (absent["events"], absent["bytes"], absent["gap"], absent["readable"]) \
        == (0, 0, None, True)

    evidence._sidecar_path(tmp_repo).parent.mkdir(mode=0o700, exist_ok=True)
    evidence._sidecar_path(tmp_repo).write_text("<html>nope")
    broken = evidence.evidence_diagnostics(tmp_repo)
    assert broken["readable"] is False and broken["events"] == 0 and broken["bytes"] > 0


def test_a_ledger_without_a_counter_reads_as_zero_evictions(tmp_repo):
    """Coerced once in the reader, so no caller has to guard a hand-edited counter."""
    evidence._sidecar_path(tmp_repo).parent.mkdir(mode=0o700, exist_ok=True)
    evidence._sidecar_path(tmp_repo).write_text(json.dumps(
        {"schema_version": 1, "events": [], "candidate_checkpoints": {},
         "evicted_total": "lots"}))
    assert evidence.evidence_diagnostics(tmp_repo)["evicted_total"] == 0
    _appended(tmp_repo)                          # and appending over it still works
    assert _ledger(tmp_repo)["evicted_total"] == 0


def test_diagnostics_counts_what_the_ledger_holds(tmp_repo):
    _appended(tmp_repo, 2)
    _seed(tmp_repo, events=_ledger(tmp_repo)["events"], evicted_total=7,
          candidate_checkpoints={"c1": {"event_ids": [], "status": "pending"}})
    diag = evidence.evidence_diagnostics(tmp_repo)
    assert (diag["events"], diag["checkpoints"], diag["evicted_total"], diag["readable"]) \
        == (2, 1, 7, True)


# ── compaction ───────────────────────────────────────────────────────────────

def test_compaction_collapses_a_settled_checkpoint_and_spares_a_pending_one(tmp_repo):
    ids = _appended(tmp_repo, 3)
    _seed(tmp_repo, events=_ledger(tmp_repo)["events"], candidate_checkpoints={
        "cand-1": {"event_ids": ids[:2], "status": "approved"},
        "cand-2": {"event_ids": ids[2:], "status": "pending"},
    })
    evidence._bump_gap(tmp_repo, "busy")

    result = evidence.compact_evidence(tmp_repo)
    assert result["status"] == "ok"
    assert (result["compacted"], result["removed_events"]) == (1, 2)

    ledger = _ledger(tmp_repo)
    survivor, disposition = ledger["events"]
    assert survivor["event_id"] == ids[2]        # the pending checkpoint's event stayed
    assert disposition["kind"] == "candidate_disposition"
    assert "cand-1" in disposition["summary"] and "approved" in disposition["summary"]
    assert disposition["session_id"] == "compaction"
    assert disposition["source"] == "compact_evidence"
    assert evidence.validate_event(disposition) == (disposition, [])
    assert ledger["compacted_through"] == "2026-08-24T10:01:00+00:00"
    assert list(ledger["candidate_checkpoints"]) == ["cand-2"]
    assert not evidence._gap_path(tmp_repo).exists()


def test_compaction_of_a_corrupt_sidecar_reports_and_touches_nothing(tmp_repo):
    path = evidence._sidecar_path(tmp_repo)
    path.parent.mkdir(mode=0o700, exist_ok=True)
    path.write_bytes(b"garbage")
    result = evidence.compact_evidence(tmp_repo)
    assert result["status"] == "error" and result["errors"]
    assert path.read_bytes() == b"garbage"


def test_compaction_never_raises_when_the_store_dir_cannot_be_created(tmp_repo, monkeypatch):
    blocked = evidence._sidecar_path(tmp_repo).parent.parent / "blocked"
    blocked.write_text("a file, so mkdir underneath it cannot succeed")
    monkeypatch.setattr(store, "STORE_DIR", blocked / ".contexer")
    assert evidence.compact_evidence(tmp_repo)["status"] == "error"


def test_compaction_with_nothing_settled_is_a_no_op(tmp_repo):
    ids = _appended(tmp_repo, 1)
    _seed(tmp_repo, events=_ledger(tmp_repo)["events"],
          candidate_checkpoints={"c1": {"event_ids": ids, "status": "pending"}})
    assert evidence.compact_evidence(tmp_repo) == {
        "status": "ok", "compacted": 0, "removed_events": 0, "errors": []}
    assert [e["event_id"] for e in _ledger(tmp_repo)["events"]] == ids


# ── contexer status ──────────────────────────────────────────────────────────

def test_status_reports_evidence_only_for_repos_that_have_it(tmp_repo, capsys):
    from contexer import cli

    assert cli._evidence_status_lines([tmp_repo]) == []
    _appended(tmp_repo, 2)
    evidence._bump_gap(tmp_repo, "busy")
    line, = cli._evidence_status_lines([tmp_repo])
    assert line == f"  evidence:     {tmp_repo}: 2 events, 1 gap (last: busy)"


@pytest.mark.parametrize("content", ["garbage", ""], ids=["corrupt", "truncated"])
def test_status_says_a_sidecar_is_unreadable_rather_than_empty(tmp_repo, content):
    """A zero-byte ledger has no bytes to count, so existence is read off `readable` too —
    otherwise the one sidecar most worth reporting is the one status stays silent about."""
    from contexer import cli

    evidence._sidecar_path(tmp_repo).parent.mkdir(mode=0o700, exist_ok=True)
    evidence._sidecar_path(tmp_repo).write_text(content)
    line, = cli._evidence_status_lines([tmp_repo])
    assert line.endswith("unreadable sidecar")
