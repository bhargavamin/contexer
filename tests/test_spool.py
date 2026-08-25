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
import time
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


def test_the_gap_counter_accumulates_across_drops(tmp_repo):
    spool._bump_gap(tmp_repo, "write_error")
    spool._bump_gap(tmp_repo, "retention", 46)
    gap = spool.evidence_diagnostics(tmp_repo)["gap"]
    assert gap["drops"] == 47 and "prior_drops_unknown" not in gap


def test_a_damaged_gap_marker_reports_unreadable_rather_than_no_loss(tmp_repo):
    spool._bump_gap(tmp_repo, "retention", 47)
    spool._gap_path(tmp_repo).write_text("{ truncated", encoding="utf-8")
    assert spool.evidence_diagnostics(tmp_repo)["gap"] == {"unreadable": True}


def test_a_damaged_gap_marker_is_never_rewritten_as_a_fresh_count_of_one(tmp_repo):
    """`.gap` is a cumulative loss ledger, not a resettable alarm: the 47 drops are gone, but
    the fact that an unknown number preceded this one must survive every later bump."""
    spool._bump_gap(tmp_repo, "retention", 47)
    spool._gap_path(tmp_repo).write_text("{ truncated", encoding="utf-8")

    spool._bump_gap(tmp_repo, "write_error")
    gap = spool.evidence_diagnostics(tmp_repo)["gap"]
    assert gap["drops"] == 1 and gap["prior_drops_unknown"] is True

    spool._bump_gap(tmp_repo, "write_error")
    carried = spool.evidence_diagnostics(tmp_repo)["gap"]
    assert carried["drops"] == 2 and carried["prior_drops_unknown"] is True


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


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="root ignores directory permissions")
def test_an_unreadable_spool_reports_unreadable_not_empty(tmp_repo):
    """"Nothing spooled" and "could not read the spool" are different facts, and a caller that
    treats the second as the first re-proposes everything the first would have settled."""
    spool.append_evidence(tmp_repo, _event())
    pending = _pending(tmp_repo)
    pending.chmod(0o000)
    try:
        assert spool.evidence_diagnostics(tmp_repo)["readable"] is False
        assert spool.list_pending_evidence(tmp_repo) == []
    finally:
        pending.chmod(0o700)


def _refuse_held_only(path):
    if path.name == "held":
        raise OSError("cannot stat")
    return True


def test_an_unreadable_spool_reports_zeros_not_the_counts_it_reached(tmp_repo, monkeypatch):
    """`readable: False` beside a real `pending` count describes PART of the spool as though it
    described all of it - the same unreadable-versus-empty collapse the flag exists to prevent,
    one level down. The pending listing here succeeds; only the held one fails."""
    spool.append_evidence(tmp_repo, _event())
    assert spool.evidence_diagnostics(tmp_repo)["pending"] == 1

    monkeypatch.setattr(Path, "is_dir", _refuse_held_only)
    diagnostics = spool.evidence_diagnostics(tmp_repo)
    assert diagnostics["readable"] is False
    assert diagnostics["pending"] == 0 and diagnostics["bytes"] == 0


def test_a_stray_candidate_json_in_pending_is_quarantined_not_invisible(tmp_repo):
    """`candidate.json` is a HELD directory's bookkeeping; in `pending/` the name can never
    legitimately occur. Skipping it by name in every listing left such a file invisible to
    listing, to quarantine and to retention at once, which is how a stray becomes permanent."""
    spool.append_evidence(tmp_repo, _event())
    stray = _pending(tmp_repo) / spool._META_NAME
    stray.write_text("{ not an event", encoding="utf-8")

    assert len(spool.list_pending_evidence(tmp_repo)) == 1     # the real event still returns
    assert not stray.exists()
    assert spool.evidence_diagnostics(tmp_repo)["quarantine"] == 1


def test_missing_spool_reads_as_empty_and_readable(tmp_repo):
    assert spool.list_pending_evidence(tmp_repo) == []
    diagnostics = spool.evidence_diagnostics(tmp_repo)
    assert diagnostics == {"pending": 0, "held": 0, "held_events": 0, "held_unattributed": 0,
                           "quarantine": 0, "bytes": 0, "gap": None, "readable": True}


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
                      "failed": [], "errors": []}
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


def test_one_failed_move_does_not_abandon_the_rest_of_the_batch(tmp_repo, monkeypatch):
    """A transient failure on one event must not leave the events behind it unattempted — that
    would silently shrink the batch to whatever preceded the first bad file."""
    ids = [_event()["event_id"] for _ in range(3)]
    for event_id in ids:
        spool.append_evidence(tmp_repo, _event(event_id=event_id))
    candidate = str(uuid.uuid4())
    real_replace = os.replace

    def second_move_fails(src, dst, *args, **kwargs):
        if ids[1] in str(src):
            raise OSError("transient")
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(spool.os, "replace", second_move_fails)
    result = spool.hold_candidate_evidence(tmp_repo, candidate, ids)

    assert result["moved"] == 2 and result["failed"] == [ids[1]]
    assert result["status"] == "error" and ids[1] in result["errors"][0]
    assert [e["event_id"] for e in spool.list_pending_evidence(tmp_repo)] == [ids[1]]


def test_hold_reports_an_unserializable_meta_instead_of_raising(tmp_repo):
    result = spool.hold_candidate_evidence(tmp_repo, str(uuid.uuid4()), [],
                                           meta={"entry_id": object()})
    assert result["status"] == "error" and result["errors"]


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


def test_the_byte_total_counts_a_held_candidates_own_bookkeeping(tmp_repo):
    """`candidate.json` is real disk the spool is responsible for, so a size report that
    omitted it would understate a repo with many held candidates. It is still not an EVENT,
    which is the one place the meta is filtered out rather than counted."""
    candidate = str(uuid.uuid4())
    spool.hold_candidate_evidence(tmp_repo, candidate, _spool_two(tmp_repo),
                                  meta={"entry_id": "e1"})
    held = spool._held_dir(tmp_repo, candidate)
    assert (held / spool._META_NAME).exists()

    diagnostics = spool.evidence_diagnostics(tmp_repo)
    assert diagnostics["held_events"] == 2
    assert diagnostics["bytes"] == sum(p.stat().st_size for p in held.iterdir())


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


def test_the_count_cap_evicts_by_arrival_not_by_the_events_own_stamp(tmp_repo, monkeypatch):
    """Ruling R29: content must not decide retention. The event that ARRIVED first goes, even
    though its own `occurred_at` puts it last in the listing — a clock-skewed or hand-written
    `2030` stamp would otherwise outlive every honestly-stamped event beside it."""
    monkeypatch.setattr(spool, "_MAX_PENDING_EVENTS", 1)
    spool.append_evidence(tmp_repo, _event(occurred_at="2030-01-01T00:00:00+00:00",
                                           summary="arrived first, stamped last"))
    spool.append_evidence(tmp_repo, _event(occurred_at="2020-01-01T00:00:00+00:00",
                                           summary="arrived second, stamped first"))
    by_stamp = sorted(_pending(tmp_repo).iterdir(), key=lambda p: p.name)
    for path, offset in zip(by_stamp, (1.0, 2.0)):     # 2020 file arrived LAST
        os.utime(path, (time.time() - offset, time.time() - offset))

    assert spool.run_retention(tmp_repo)["dropped_pending"] == 1
    assert [e["summary"] for e in spool.list_pending_evidence(tmp_repo)] == \
        ["arrived second, stamped first"]


def test_identical_mtimes_still_let_the_events_own_stamp_decide(tmp_repo, monkeypatch):
    """The documented residual, named for what it is: at the same mtime the sort is stable, so
    the tie is decided by listing order — filename order — which is the event's own
    `occurred_at`. That is exactly the content influence R29 removes from the distinct-mtime
    case, and it is NOT removed here: the spool has no arrival fact left to decide with, and a
    monotonic arrival counter stamped at write time is the upgrade path. Pinned so the residual
    is a decision somebody made rather than a surprise."""
    monkeypatch.setattr(spool, "_MAX_PENDING_EVENTS", 1)
    spool.append_evidence(tmp_repo, _event(occurred_at=_ago(9), summary="stamped older"))
    spool.append_evidence(tmp_repo, _event(occurred_at=_ago(1), summary="stamped newer"))
    stamped = time.time()
    for path in _pending(tmp_repo).iterdir():
        os.utime(path, (stamped, stamped))

    spool.run_retention(tmp_repo)

    # The event's own stamp picked the victim — the residual, not the desired behaviour.
    assert [e["summary"] for e in spool.list_pending_evidence(tmp_repo)] == ["stamped newer"]


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
    real_stat, seen = Path.stat, set()

    def flaky(self, *args, **kwargs):
        # Fail only the AGE read. The listing stats each file once to classify it, and a file
        # that failed THAT stat would never be listed at all — a different branch entirely.
        if self.suffix == ".json" and self.parent.name == "pending" and self in seen:
            raise OSError("no stat")
        seen.add(self)
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


def test_retention_removes_gap_write_debris_too(tmp_repo):
    """`.gap` goes through `store.atomic_write`, whose temp files are named
    `<name>.<random>.tmp` rather than with this module's own `tmp-` prefix - so a
    prefix-matched sweep left an interrupted gap write behind for good. The `.tmp` suffix is
    what the two writers actually share."""
    spool.append_evidence(tmp_repo, _event())
    debris = spool._repo_dir(tmp_repo) / ".gap.abc123.tmp"
    debris.write_text("{ half", encoding="utf-8")
    _age(debris, 1)

    assert spool.run_retention(tmp_repo)["temp_removed"] == 1
    assert not debris.exists()


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


def test_a_held_candidate_with_no_recorded_entry_is_left_alone_but_counted(tmp_repo):
    """Held is exempt while unsettled, and a candidate naming no entry cannot be judged — so it
    is left rather than guessed at. It is also held FOREVER, since no sweep can ever reach it,
    which is why `contexer status` has to be able to see it accruing."""
    candidate = str(uuid.uuid4())
    spool.hold_candidate_evidence(tmp_repo, candidate, _spool_two(tmp_repo))
    assert spool.run_retention(tmp_repo)["finalized_orphans"] == []
    assert list(spool.held_candidates(tmp_repo)) == [candidate]
    assert spool.evidence_diagnostics(tmp_repo)["held_unattributed"] == 1


def test_a_corrupt_candidate_meta_counts_as_unattributed_and_says_so(tmp_repo):
    candidate = str(uuid.uuid4())
    spool.hold_candidate_evidence(tmp_repo, candidate, _spool_two(tmp_repo),
                                  meta={"entry_id": "e1"})
    (spool._held_dir(tmp_repo, candidate) / spool._META_NAME).write_text("{ nope",
                                                                        encoding="utf-8")

    assert spool.held_candidates(tmp_repo) == {candidate: {"unreadable": True}}
    assert spool.evidence_diagnostics(tmp_repo)["held_unattributed"] == 1
    assert spool.run_retention(tmp_repo)["finalized_orphans"] == []


def test_an_attributed_held_candidate_is_not_counted_as_unattributed(tmp_repo):
    spool.hold_candidate_evidence(tmp_repo, str(uuid.uuid4()), _spool_two(tmp_repo),
                                  meta={"entry_id": "e1"})
    assert spool.evidence_diagnostics(tmp_repo)["held_unattributed"] == 0


def test_an_unreadable_store_defers_the_orphan_sweep_rather_than_failing(tmp_repo, monkeypatch):
    """The sweep asks the store who is still live and takes no lock to do it — so a store it
    cannot read costs one deferred sweep, never a held candidate finalized on a guess."""
    candidate = str(uuid.uuid4())
    spool.hold_candidate_evidence(tmp_repo, candidate, _spool_two(tmp_repo),
                                  meta={"entry_id": "who-knows"})

    def unreadable(*_args, **_kwargs):
        raise OSError("store is gone")

    monkeypatch.setattr(store, "load", unreadable)

    assert spool.run_retention(tmp_repo) == {
        "dropped_pending": 0, "dropped_quarantine": 0, "temp_removed": 0,
        "finalized_orphans": [], "errors": []}
    assert list(spool.held_candidates(tmp_repo)) == [candidate]


def test_retention_on_an_absent_spool_is_a_clean_no_op(tmp_repo):
    assert spool.run_retention(tmp_repo) == {
        "dropped_pending": 0, "dropped_quarantine": 0, "temp_removed": 0,
        "finalized_orphans": [], "errors": []}


# ── session-start maintenance (mitigation 1) ─────────────────────────────────────

def test_maintain_spool_runs_retention_once_per_ttl(tmp_repo):
    """The emit-only host's only bound: `run_retention`'s other caller is reconciliation,
    which Codex never reaches and Cursor never has file evidence for."""
    spool.append_evidence(tmp_repo, _event(summary="ancient"))
    _age(next(iter(_pending(tmp_repo).iterdir())), spool._MAX_PENDING_AGE_DAYS + 1)

    assert spool.maintain_spool(tmp_repo)["dropped_pending"] == 1
    assert spool.list_pending_evidence(tmp_repo) == []

    # A second call inside the TTL does no work at all — not even a listing.
    spool.append_evidence(tmp_repo, _event(summary="fresh"))
    _age(next(iter(_pending(tmp_repo).iterdir())), spool._MAX_PENDING_AGE_DAYS + 1)
    assert spool.maintain_spool(tmp_repo) == {}
    assert len(spool.list_pending_evidence(tmp_repo)) == 1


def test_maintain_spool_does_nothing_at_all_without_a_spool(tmp_repo):
    # The cheap gate. Every session start on every repo runs this and the overwhelming
    # majority have never emitted an event, so no stamp is written and no state accrues.
    assert spool.maintain_spool(tmp_repo) == {}
    assert not spool._maintenance_stamp(tmp_repo).exists()


def test_an_expired_stamp_lets_maintenance_run_again(tmp_repo):
    spool.append_evidence(tmp_repo, _event())
    spool.maintain_spool(tmp_repo)
    _age(spool._maintenance_stamp(tmp_repo), 2)
    _age(next(iter(_pending(tmp_repo).iterdir())), spool._MAX_PENDING_AGE_DAYS + 1)

    assert spool.maintain_spool(tmp_repo)["dropped_pending"] == 1


def test_maintain_spool_never_raises(tmp_repo, monkeypatch):
    spool.append_evidence(tmp_repo, _event())

    def boom(*_a, **_k):
        raise RuntimeError("boom")

    monkeypatch.setattr(spool, "run_retention", boom)
    assert spool.maintain_spool(tmp_repo) == {}


# ── contexer status ──────────────────────────────────────────────────────────────

def _status_line(repo):
    from contexer import cli

    lines = cli._evidence_status_lines([repo])
    return lines[0] if lines else ""


def test_status_is_silent_for_a_repo_with_no_spool(tmp_repo):
    from contexer import cli

    assert cli._evidence_status_lines([tmp_repo]) == []


def test_status_counts_what_the_spool_holds(tmp_repo):
    spool.append_evidence(tmp_repo, _event())
    spool.hold_candidate_evidence(tmp_repo, str(uuid.uuid4()), _spool_two(tmp_repo),
                                  meta={"entry_id": "e1"})
    assert _status_line(tmp_repo) == f"  evidence:     {tmp_repo}: 1 pending, 1 held (2 events)"


def test_status_reads_the_gap_as_a_cumulative_loss_ledger_not_an_alarm(tmp_repo):
    """Ruling R28: nothing clears `.gap`, so it reports what this spool has LOST — a count and
    a date, never a condition the developer is being asked to resolve."""
    spool.append_evidence(tmp_repo, _event())
    spool._bump_gap(tmp_repo, "write_error", 3)
    rendered = _status_line(tmp_repo).split(f"{tmp_repo}: ", 1)[1]
    assert rendered.startswith("1 pending, 3 events lost, last 20")
    assert "gap" not in rendered          # the word names a hole to fill, not a loss ledger


def test_status_says_when_the_loss_count_is_a_lower_bound(tmp_repo):
    spool.append_evidence(tmp_repo, _event())
    spool._gap_path(tmp_repo).write_text("{ mangled", encoding="utf-8")
    spool._bump_gap(tmp_repo, "write_error")
    assert "earlier losses uncounted" in _status_line(tmp_repo)


def test_status_reports_a_damaged_gap_marker_rather_than_a_number(tmp_repo):
    spool.append_evidence(tmp_repo, _event())
    spool._gap_path(tmp_repo).write_text("{ mangled", encoding="utf-8")
    assert _status_line(tmp_repo).endswith("1 pending, loss ledger unreadable")


def test_status_names_held_candidates_nothing_will_ever_settle(tmp_repo):
    spool.hold_candidate_evidence(tmp_repo, str(uuid.uuid4()), _spool_two(tmp_repo))
    assert "1 unattributed" in _status_line(tmp_repo)


def _refuse_spool_dirs(path):
    if path.name in ("pending", "quarantine", "held"):
        raise OSError("cannot stat")
    return True


def test_status_says_a_spool_is_unreadable_rather_than_empty(tmp_repo, monkeypatch):
    spool.append_evidence(tmp_repo, _event())
    monkeypatch.setattr(Path, "is_dir", _refuse_spool_dirs)
    assert _status_line(tmp_repo).endswith("spool unreadable")
