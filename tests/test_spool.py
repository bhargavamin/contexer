"""Tests for contexer/spool.py — the per-event evidence spool.

The properties asserted here are the ones the design is FOR, not incidental behaviour:

* a write is one file and nothing else — no listing, no lock, no contention, so two racing
  hook writers both land;
* a write is atomic — a reader sees the whole event or no event, never a torn one;
* one bad file never hides its valid siblings (it is quarantined as it is met);
* loss is RECORDED (`.gap`) rather than silent, whether it came from a failed write or a
  retention drop;
* retention ages a file by its own MTIME, never by the timestamp inside its name — an event
  must not get to decide how long it is kept;
* held events are exempt while their candidate is unsettled, and an id that becomes a path
  component is shape-checked before the join.
"""
import json
import os
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from contexer import spool, store

SPOOL_SOURCE = Path(spool.__file__).read_text(encoding="utf-8")


def _event(**overrides):
    return {
        "schema_version": 1,
        "event_id": str(uuid.uuid4()),
        "session_id": "sess-1",
        "repo_key": "/repo",
        "kind": "file_changed",
        "occurred_at": datetime.now(timezone.utc).isoformat(),
        "source": "test",
        "summary": "something happened",
        "files": ["src/app.py"],
        "attributes": {},
        **overrides,
    }


def _ago(days):
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _pending(repo):
    return spool._pending_dir(repo)


def _age(path, days):
    """Backdate a file's MTIME — the only thing retention is allowed to age it by."""
    old = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
    os.utime(path, (old, old))


# ── the write path ───────────────────────────────────────────────────────────────

def test_each_event_gets_its_own_file(tmp_repo):
    for _ in range(3):
        assert spool.append_evidence(tmp_repo, _event())["status"] == "stored"
    assert len(list(_pending(tmp_repo).iterdir())) == 3


def test_filenames_are_unique_and_sort_chronologically(tmp_repo):
    first = _event(occurred_at="2026-01-01T00:00:00+00:00")
    second = _event(occurred_at="2026-06-01T00:00:00+00:00")
    spool.append_evidence(tmp_repo, second)
    spool.append_evidence(tmp_repo, first)
    names = sorted(p.name for p in _pending(tmp_repo).iterdir())
    assert names[0].startswith("20260101T000000")
    assert names[1].startswith("20260601T000000")
    assert first["event_id"] in names[0] and second["event_id"] in names[1]


def test_modes_are_0700_dirs_and_0600_files(tmp_repo):
    spool.append_evidence(tmp_repo, _event())
    pending = _pending(tmp_repo)
    for directory in (spool._repo_dir(tmp_repo), pending):
        assert directory.stat().st_mode & 0o777 == 0o700, directory
    event_file = next(iter(pending.iterdir()))
    assert event_file.stat().st_mode & 0o777 == 0o600


def test_invalid_event_is_rejected_without_a_gap(tmp_repo):
    result = spool.append_evidence(tmp_repo, {"kind": "nonsense"})
    assert result["status"] == "rejected_invalid" and result["errors"]
    assert spool.evidence_diagnostics(tmp_repo)["gap"] is None


def test_a_failed_rename_leaves_no_partial_file_and_records_the_gap(tmp_repo, monkeypatch):
    """Atomicity: content is written to a temp file and PUBLISHED by the rename, so a rename
    that never happens leaves nothing visible — and the lost event is recorded, not silent."""
    spool.append_evidence(tmp_repo, _event())              # create the dir first
    real_replace = os.replace

    def only_publishing_fails(src, dst, *args, **kwargs):
        if os.path.dirname(dst) == str(_pending(tmp_repo)):
            raise OSError("boom")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(spool.os, "replace", only_publishing_fails)
    result = spool.append_evidence(tmp_repo, _event(summary="never lands"))

    assert result["status"] == "dropped_error"
    assert len(list(_pending(tmp_repo).iterdir())) == 1     # only the first event is visible
    assert list(_pending(tmp_repo).glob(f"{spool._TEMP_PREFIX}*")) == []
    assert spool.evidence_diagnostics(tmp_repo)["gap"]["drops"] == 1


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores directory permissions")
def test_unwritable_spool_records_a_gap(tmp_repo):
    pending = spool._ensure_dir(_pending(tmp_repo))
    pending.chmod(0o500)
    try:
        result = spool.append_evidence(tmp_repo, _event())
        assert result["status"] == "dropped_error"
        gap = spool.evidence_diagnostics(tmp_repo)["gap"]
        assert gap["drops"] == 1 and gap["last_reason"] == "write_error"
    finally:
        pending.chmod(0o700)


def test_two_concurrent_writers_both_land(tmp_repo):
    """No lock anywhere, so this is the property that replaces the old busy-lock behaviour:
    unique filenames mean there is nothing to serialize and nothing to lose."""
    spool._ensure_dir(_pending(tmp_repo))
    start = threading.Barrier(2)

    def write():
        start.wait()
        for _ in range(25):
            assert spool.append_evidence(tmp_repo, _event())["status"] == "stored"

    threads = [threading.Thread(target=write) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(spool.list_pending_evidence(tmp_repo)) == 50


def test_the_spool_takes_no_locks_at_all():
    """Structural, in the house style: the no-lock rule is invisible to a behaviour test the
    moment somebody "fixes" a race by reaching for a lock again."""
    for forbidden in ("fcntl", "flock", "store_lock", "LOCK_EX"):
        assert forbidden not in SPOOL_SOURCE, forbidden


def test_append_never_lists_the_spool(tmp_repo, monkeypatch):
    """The cost of event N must not depend on events 1..N-1, which holds only while the write
    path never enumerates what is already there — so make enumerating fail and write anyway."""
    spool.append_evidence(tmp_repo, _event())

    def boom(*_args, **_kwargs):
        raise AssertionError("append listed the spool")

    monkeypatch.setattr(Path, "iterdir", boom)
    monkeypatch.setattr(Path, "glob", boom)

    assert spool.append_evidence(tmp_repo, _event())["status"] == "stored"


# ── the read path ────────────────────────────────────────────────────────────────

def test_events_come_back_oldest_first(tmp_repo):
    for day in (3, 1, 2):
        spool.append_evidence(tmp_repo, _event(occurred_at=_ago(day), summary=f"day {day}"))
    assert [e["summary"] for e in spool.list_pending_evidence(tmp_repo)] == \
        ["day 3", "day 2", "day 1"]


def test_session_filter(tmp_repo):
    spool.append_evidence(tmp_repo, _event(session_id="a"))
    spool.append_evidence(tmp_repo, _event(session_id="b"))
    assert len(spool.list_pending_evidence(tmp_repo, "a")) == 1
    assert len(spool.list_pending_evidence(tmp_repo)) == 2


def test_a_corrupt_file_is_quarantined_and_siblings_still_return(tmp_repo):
    spool.append_evidence(tmp_repo, _event(summary="good one"))
    spool.append_evidence(tmp_repo, _event(summary="good two"))
    victim = sorted(_pending(tmp_repo).iterdir())[0]
    victim.write_text("{ not json", encoding="utf-8")

    events = spool.list_pending_evidence(tmp_repo)

    assert [e["summary"] for e in events] == ["good two"]
    assert (spool._quarantine_dir(tmp_repo) / victim.name).exists()
    assert not victim.exists()
    diagnostics = spool.evidence_diagnostics(tmp_repo)
    assert diagnostics["pending"] == 1 and diagnostics["quarantine"] == 1


def test_a_schema_invalid_file_is_quarantined_too(tmp_repo):
    spool.append_evidence(tmp_repo, _event())
    victim = next(iter(_pending(tmp_repo).iterdir()))
    victim.write_text(json.dumps({"schema_version": 99}), encoding="utf-8")
    assert spool.list_pending_evidence(tmp_repo) == []
    assert spool.evidence_diagnostics(tmp_repo)["quarantine"] == 1


def test_temp_files_are_never_listed_or_quarantined(tmp_repo):
    spool.append_evidence(tmp_repo, _event())
    stray = _pending(tmp_repo) / f"{spool._TEMP_PREFIX}half-written.tmp"
    stray.write_text("{ partial", encoding="utf-8")
    assert len(spool.list_pending_evidence(tmp_repo)) == 1
    assert stray.exists() and spool.evidence_diagnostics(tmp_repo)["quarantine"] == 0


def test_missing_spool_reads_as_empty_and_readable(tmp_repo):
    assert spool.list_pending_evidence(tmp_repo) == []
    diagnostics = spool.evidence_diagnostics(tmp_repo)
    assert diagnostics == {"pending": 0, "held": 0, "held_events": 0, "quarantine": 0,
                           "bytes": 0, "gap": None, "readable": True}


# ── hold / finalize ──────────────────────────────────────────────────────────────

def _spool_two(repo):
    events = [_event(occurred_at=_ago(2)), _event(occurred_at=_ago(1))]
    for event in events:
        spool.append_evidence(repo, event)
    return [e["event_id"] for e in events]


def test_hold_moves_events_out_of_pending(tmp_repo):
    ids = _spool_two(tmp_repo)
    candidate = str(uuid.uuid4())

    result = spool.hold_candidate_evidence(tmp_repo, candidate, ids,
                                           meta={"entry_id": "abc123", "lane": "content"})

    assert result == {"status": "ok", "moved": 2, "already_held": 0, "missing": [],
                      "errors": []}
    assert spool.list_pending_evidence(tmp_repo) == []
    assert spool.held_candidates(tmp_repo) == {candidate: {"entry_id": "abc123",
                                                           "lane": "content"}}
    diagnostics = spool.evidence_diagnostics(tmp_repo)
    assert diagnostics["held"] == 1 and diagnostics["held_events"] == 2


def test_hold_is_idempotent_when_the_target_already_holds_the_event(tmp_repo):
    """Crash recovery: a run interrupted between two renames re-runs, and a source that is
    gone while the target exists counts as already moved rather than as an error."""
    ids = _spool_two(tmp_repo)
    candidate = str(uuid.uuid4())
    spool.hold_candidate_evidence(tmp_repo, candidate, ids[:1])

    result = spool.hold_candidate_evidence(tmp_repo, candidate, ids)

    assert result["moved"] == 1 and result["already_held"] == 1 and result["missing"] == []
    assert spool.evidence_diagnostics(tmp_repo)["held_events"] == 2


def test_hold_reports_a_missing_event_instead_of_raising(tmp_repo):
    candidate, ghost = str(uuid.uuid4()), str(uuid.uuid4())
    result = spool.hold_candidate_evidence(tmp_repo, candidate, [ghost])
    assert result["missing"] == [ghost] and result["status"] == "ok"


def test_finalize_returns_the_summary_and_removes_the_raw_events(tmp_repo):
    ids = _spool_two(tmp_repo)
    candidate = str(uuid.uuid4())
    spool.hold_candidate_evidence(tmp_repo, candidate, ids, meta={"entry_id": "e1"})

    summary = spool.finalize_candidate_evidence(tmp_repo, candidate, "approved")

    assert summary["candidate_id"] == candidate
    assert summary["disposition"] == "approved"
    assert sorted(summary["event_ids"]) == sorted(ids)
    assert datetime.fromisoformat(summary["occurred_at"]).tzinfo is not None
    assert not spool._held_dir(tmp_repo, candidate).exists()
    assert spool.evidence_diagnostics(tmp_repo)["held"] == 0


def test_finalize_is_idempotent(tmp_repo):
    candidate = str(uuid.uuid4())
    spool.hold_candidate_evidence(tmp_repo, candidate, _spool_two(tmp_repo))
    spool.finalize_candidate_evidence(tmp_repo, candidate, "dismissed")

    again = spool.finalize_candidate_evidence(tmp_repo, candidate, "dismissed")

    assert again["event_ids"] == [] and again["disposition"] == "dismissed"


def test_finalize_rejects_an_unknown_disposition(tmp_repo):
    with pytest.raises(ValueError, match="disposition"):
        spool.finalize_candidate_evidence(tmp_repo, str(uuid.uuid4()), "maybe")


@pytest.mark.parametrize("bad", ["../../escape", "a/b", "", ".", "nope", None])
def test_path_shaped_ids_are_refused_before_any_join(tmp_repo, bad):
    """Mitigation 6: `candidate_id` and every event id are shape-checked BEFORE a path join,
    so no traversal, separator or absolute path can address a file outside the spool."""
    with pytest.raises(ValueError):
        spool.hold_candidate_evidence(tmp_repo, bad, [])
    with pytest.raises(ValueError):
        spool.hold_candidate_evidence(tmp_repo, str(uuid.uuid4()), [bad])
    with pytest.raises(ValueError):
        spool.finalize_candidate_evidence(tmp_repo, bad, "dismissed")


# ── retention ────────────────────────────────────────────────────────────────────

def test_retention_drops_past_the_count_cap_oldest_first(tmp_repo, monkeypatch):
    monkeypatch.setattr(spool, "_MAX_PENDING_EVENTS", 2)
    for day in (5, 4, 3, 2):
        spool.append_evidence(tmp_repo, _event(occurred_at=_ago(day), summary=f"day {day}"))

    report = spool.run_retention(tmp_repo)

    assert report["dropped_pending"] == 2
    assert [e["summary"] for e in spool.list_pending_evidence(tmp_repo)] == ["day 3", "day 2"]
    gap = spool.evidence_diagnostics(tmp_repo)["gap"]
    assert gap["drops"] == 2 and gap["last_reason"] == "retention"


def test_retention_drops_past_the_age_cap(tmp_repo):
    spool.append_evidence(tmp_repo, _event(summary="old"))
    _age(next(iter(_pending(tmp_repo).iterdir())), spool._MAX_PENDING_AGE_DAYS + 1)
    spool.append_evidence(tmp_repo, _event(summary="fresh"))

    report = spool.run_retention(tmp_repo)

    assert report["dropped_pending"] == 1
    assert [e["summary"] for e in spool.list_pending_evidence(tmp_repo)] == ["fresh"]


def test_age_is_the_files_mtime_never_the_stamp_in_its_own_name(tmp_repo):
    """Mitigation 5: the filename stamp comes from the event's own `occurred_at`, so ageing by
    it would let an event decide how long it is kept — a backdated event would evict itself."""
    spool.append_evidence(tmp_repo, _event(occurred_at="2001-01-01T00:00:00+00:00",
                                           summary="ancient name, fresh file"))
    assert next(iter(_pending(tmp_repo).iterdir())).name.startswith("20010101")

    assert spool.run_retention(tmp_repo)["dropped_pending"] == 0
    assert len(spool.list_pending_evidence(tmp_repo)) == 1


def test_a_file_that_cannot_be_stat_ed_is_kept(tmp_repo, monkeypatch):
    """Fail-soft means never dropping evidence over a failure to MEASURE it: this file is old
    enough to evict, and the only reason it survives is that its age could not be read."""
    spool.append_evidence(tmp_repo, _event())
    event_file = next(iter(_pending(tmp_repo).iterdir()))
    _age(event_file, spool._MAX_PENDING_AGE_DAYS + 1)
    real_stat = Path.stat

    def flaky(self, *args, **kwargs):
        if self.suffix == ".json" and self.parent.name == "pending":
            raise OSError("no stat")
        return real_stat(self, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(Path, "stat", flaky)
        assert spool.run_retention(tmp_repo)["dropped_pending"] == 0
    assert len(spool.list_pending_evidence(tmp_repo)) == 1


def test_held_events_are_exempt_from_retention(tmp_repo, monkeypatch):
    monkeypatch.setattr(spool, "_MAX_PENDING_EVENTS", 0)
    store.update_decision(tmp_repo, "use JWTs instead of server sessions — stateless", "s1")
    live_id = store.load(tmp_repo)["entries"][0]["id"]
    ids = _spool_two(tmp_repo)
    candidate = str(uuid.uuid4())
    spool.hold_candidate_evidence(tmp_repo, candidate, ids, meta={"entry_id": live_id})
    for path in spool._held_dir(tmp_repo, candidate).iterdir():
        _age(path, spool._MAX_PENDING_AGE_DAYS + 5)

    report = spool.run_retention(tmp_repo)

    assert report["dropped_pending"] == 0
    assert spool.evidence_diagnostics(tmp_repo)["held_events"] == 2


def test_quarantine_is_capped_and_aged_like_pending(tmp_repo):
    spool.append_evidence(tmp_repo, _event())
    next(iter(_pending(tmp_repo).iterdir())).write_text("{ bad", encoding="utf-8")
    spool.list_pending_evidence(tmp_repo)                  # quarantines it
    quarantined = next(iter(spool._quarantine_dir(tmp_repo).iterdir()))
    _age(quarantined, spool._MAX_PENDING_AGE_DAYS + 1)

    report = spool.run_retention(tmp_repo)

    assert report["dropped_quarantine"] == 1
    assert spool.evidence_diagnostics(tmp_repo)["quarantine"] == 0
    assert spool.evidence_diagnostics(tmp_repo)["gap"]["drops"] == 1


def test_retention_removes_stale_temp_files_but_not_fresh_ones(tmp_repo):
    spool.append_evidence(tmp_repo, _event())
    stale = _pending(tmp_repo) / f"{spool._TEMP_PREFIX}stale.tmp"
    stale.write_text("half", encoding="utf-8")
    _age(stale, 1)
    fresh = _pending(tmp_repo) / f"{spool._TEMP_PREFIX}fresh.tmp"
    fresh.write_text("half", encoding="utf-8")

    report = spool.run_retention(tmp_repo)

    assert report["temp_removed"] == 1
    assert fresh.exists() and not stale.exists()
    assert spool.evidence_diagnostics(tmp_repo)["gap"] is None   # debris is not lost evidence


def test_a_held_candidate_whose_decision_is_gone_is_finalized(tmp_repo):
    store.update_decision(tmp_repo, "use JWTs instead of server sessions — stateless", "s1")
    live_id = store.load(tmp_repo)["entries"][0]["id"]
    live, orphan = str(uuid.uuid4()), str(uuid.uuid4())
    spool.hold_candidate_evidence(tmp_repo, live, _spool_two(tmp_repo),
                                  meta={"entry_id": live_id})
    spool.hold_candidate_evidence(tmp_repo, orphan, _spool_two(tmp_repo),
                                  meta={"entry_id": "no-such-entry"})

    report = spool.run_retention(tmp_repo)

    assert report["finalized_orphans"] == [orphan]
    assert list(spool.held_candidates(tmp_repo)) == [live]


def test_a_held_candidate_with_no_recorded_entry_is_left_alone(tmp_repo):
    """Held is exempt while unsettled, and a candidate naming no entry cannot be judged — so
    it is left rather than guessed at."""
    candidate = str(uuid.uuid4())
    spool.hold_candidate_evidence(tmp_repo, candidate, _spool_two(tmp_repo))
    assert spool.run_retention(tmp_repo)["finalized_orphans"] == []
    assert list(spool.held_candidates(tmp_repo)) == [candidate]


def test_retention_on_an_absent_spool_is_a_clean_no_op(tmp_repo):
    assert spool.run_retention(tmp_repo) == {
        "dropped_pending": 0, "dropped_quarantine": 0, "temp_removed": 0,
        "finalized_orphans": [], "errors": []}
