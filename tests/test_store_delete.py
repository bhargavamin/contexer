"""Tests for the tombstone-sidecar delete/restore path, the resurrection guard, and the
console's corrupt-vs-empty contract (load_diagnostics, deleted_diagnostics, slug addressing)."""
import contextlib
import json
import types
from pathlib import Path

import pytest

from contexer import memory_sync, store

SESSION = "test-delete-session"

CACHE_DECISION = "Use Redis for hot-read caching because Postgres round-trips dominated latency"
QUEUE_DECISION = "Route background jobs through Celery workers rather than in-process threads"
SEARCH_DECISION = "Serve full-text search from OpenSearch, keeping the primary database free"


def _store_one(repo: str, content: str, **kwargs) -> str:
    """Store an approved decision (created_by='human' classifies as auto) and return its id."""
    ok, entry_id = store.update_decision(repo, content, SESSION, created_by="human", **kwargs)
    assert ok, f"fixture decision was filtered: {content}"
    return entry_id


def _live_ids(repo: str) -> list[str]:
    return [e["id"] for e in store._load(repo)["entries"] if e["type"] == "decision"]


def _snake_file(n_snake: int) -> str:
    """A Python module of snake_case functions — enough for the miner to measure a
    naming convention at the high tier (mirrors test_store.py's helper)."""
    return "\n".join(f"def fn_snake_{i}():\n    pass\n" for i in range(n_snake))


@pytest.fixture
def tracker(monkeypatch):
    """Records every `_atomic_write` together with the `_store_lock` depth in force at the
    time, so a test can prove which files a write path touched, in what order, and that they
    all happened inside ONE critical section."""
    state = types.SimpleNamespace(sections=0, depth=0, writes=[])
    real_lock, real_write = store._store_lock, store._atomic_write

    @contextlib.contextmanager
    def tracked_lock(slug):
        state.sections += 1
        state.depth += 1
        try:
            with real_lock(slug):
                yield
        finally:
            state.depth -= 1

    def tracked_write(path, text):
        state.writes.append((path.name, state.depth))
        real_write(path, text)

    monkeypatch.setattr(store, "_store_lock", tracked_lock)
    monkeypatch.setattr(store, "_atomic_write", tracked_write)
    return state


@pytest.fixture
def read_counts(monkeypatch):
    """Counts `Path.read_text` calls per file name. The console polls these projections every
    10 seconds over a store that is routinely a few hundred KB, so a second parse of the same
    file is a real cost, not a style point."""
    counts: dict[str, int] = {}
    real_read_text = Path.read_text

    def counting(self, *args, **kwargs):
        counts[self.name] = counts.get(self.name, 0) + 1
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", counting)
    return counts


# ── sidecar shape and file semantics ──────────────────────────────────────────

class TestSidecar:
    def test_sidecar_created_with_expected_shape(self, tmp_repo):
        entry_id = _store_one(tmp_repo, CACHE_DECISION)
        ok, msg = store.delete_decision(tmp_repo, entry_id, actor="ui")
        assert ok
        assert entry_id[:8] in msg

        sidecar = store._deleted_path(tmp_repo)
        assert sidecar.exists()
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        assert set(data) == {"repo_path", "entries"}
        assert data["repo_path"] == tmp_repo
        assert len(data["entries"]) == 1
        tomb = data["entries"][0]
        assert tomb["id"] == entry_id
        assert tomb["content"] == store._normalize_content(CACHE_DECISION)
        assert tomb["deleted_by"] == "ui"
        assert tomb["deleted_at"].endswith("+00:00")

    def test_sidecar_file_is_owner_only(self, tmp_repo):
        store.delete_decision(tmp_repo, _store_one(tmp_repo, CACHE_DECISION))
        mode = store._deleted_path(tmp_repo).stat().st_mode & 0o777
        assert mode == 0o600

    def test_sidecar_name_is_derived_from_the_slug(self, tmp_repo):
        assert store._deleted_path(tmp_repo).name == f"{store._slug(tmp_repo)}.deleted.json"

    def test_entry_leaves_the_live_store_file(self, tmp_repo):
        keep = _store_one(tmp_repo, QUEUE_DECISION)
        gone = _store_one(tmp_repo, CACHE_DECISION)
        store.delete_decision(tmp_repo, gone)
        assert _live_ids(tmp_repo) == [keep]

    def test_live_store_carries_no_tombstone_fields(self, tmp_repo):
        # The whole point of the sidecar: `_load` runs per prompt, so tombstones must never
        # accumulate in the file it parses.
        for content in (CACHE_DECISION, QUEUE_DECISION):
            store.delete_decision(tmp_repo, _store_one(tmp_repo, content))
        raw = store._store_path(tmp_repo).read_text(encoding="utf-8")
        assert "deleted_at" not in raw
        assert "deleted_by" not in raw
        assert store._load(tmp_repo)["entries"] == []

    def test_two_deletes_append_to_one_sidecar(self, tmp_repo):
        first = _store_one(tmp_repo, CACHE_DECISION)
        second = _store_one(tmp_repo, QUEUE_DECISION)
        store.delete_decision(tmp_repo, first)
        store.delete_decision(tmp_repo, second)
        assert [e["id"] for e in store.list_deleted(tmp_repo)] == [first, second]

    def test_unknown_id_is_a_noop(self, tmp_repo):
        _store_one(tmp_repo, CACHE_DECISION)
        ok, msg = store.delete_decision(tmp_repo, "nope-not-an-id")
        assert ok is False
        assert "not found" in msg
        assert not store._deleted_path(tmp_repo).exists()
        assert len(_live_ids(tmp_repo)) == 1

    def test_empty_id_is_a_noop(self, tmp_repo):
        _store_one(tmp_repo, CACHE_DECISION)
        ok, _ = store.delete_decision(tmp_repo, "")
        assert ok is False
        assert len(_live_ids(tmp_repo)) == 1

    def test_short_id_prefix_accepted(self, tmp_repo):
        entry_id = _store_one(tmp_repo, CACHE_DECISION)
        ok, _ = store.delete_decision(tmp_repo, entry_id[:8])
        assert ok
        assert _live_ids(tmp_repo) == []

    def test_actor_defaults_to_ui(self, tmp_repo):
        store.delete_decision(tmp_repo, _store_one(tmp_repo, CACHE_DECISION))
        assert store.list_deleted(tmp_repo)[0]["deleted_by"] == "ui"

    def test_corrupt_sidecar_degrades_to_empty_on_the_capture_path(self, tmp_repo):
        # `list_deleted` and the resurrection guard still degrade (they must never crash a
        # session); `deleted_diagnostics` is what tells "empty" from "unreadable".
        store._deleted_path(tmp_repo).write_text('{"entries": [{"id": "1"')
        assert store.list_deleted(tmp_repo) == []
        assert store.deleted_diagnostics(tmp_repo)["ok"] is False

    def test_a_non_decision_entry_cannot_be_tombstoned(self, tmp_repo):
        # delete_decision searched ALL entries, so an entry of another type was reachable by id
        # through the console's write surface.
        _store_one(tmp_repo, CACHE_DECISION)
        data = store._load(tmp_repo)
        data["entries"].append({"id": "ctx-0000000000001", "type": "context",
                                "content": "scratch note from a session",
                                "timestamp": "2026-01-01T00:00:00+00:00"})
        store._save(tmp_repo, data)

        ok, msg = store.delete_decision(tmp_repo, "ctx-0000000000001")

        assert ok is False
        assert "not found" in msg
        assert any(e["id"] == "ctx-0000000000001" for e in store._load(tmp_repo)["entries"])
        assert store.list_deleted(tmp_repo) == []

    def test_the_sidecar_is_capped_so_the_capture_guard_stays_bounded(self, tmp_repo,
                                                                     monkeypatch):
        monkeypatch.setattr(store, "MAX_TOMBSTONES", 2)
        ids = [_store_one(tmp_repo, c) for c in (CACHE_DECISION, QUEUE_DECISION,
                                                SEARCH_DECISION)]
        for entry_id in ids:
            store.delete_decision(tmp_repo, entry_id)
        # Oldest deletion evicted; the two most recent stay restorable.
        assert [e["id"] for e in store.list_deleted(tmp_repo)] == ids[1:]


# ── an unreadable sidecar is reported, never silently overwritten ──────────────

class TestUnreadableSidecar:
    def _tombstone_then_truncate(self, repo: str) -> tuple[str, str]:
        """One real tombstone on disk, then the file truncated so it will not parse. Returns
        (tombstoned id, the exact bytes now on disk)."""
        entry_id = _store_one(repo, CACHE_DECISION)
        store.delete_decision(repo, entry_id)
        sidecar = store._deleted_path(repo)
        broken = sidecar.read_text(encoding="utf-8")[:-3]
        sidecar.write_text(broken, encoding="utf-8")
        return entry_id, broken

    def test_a_delete_never_overwrites_a_sidecar_it_could_not_parse(self, tmp_repo):
        first, broken = self._tombstone_then_truncate(tmp_repo)
        second = _store_one(tmp_repo, QUEUE_DECISION)

        ok, msg = store.delete_decision(tmp_repo, second)

        assert ok is False
        assert "unreadable" in msg
        assert store._deleted_path(tmp_repo).read_text(encoding="utf-8") == broken, \
            "the delete rewrote the sidecar, destroying every tombstone already in it"
        assert first in broken, "the original tombstone must still be recoverable by hand"
        assert _live_ids(tmp_repo) == [second], "a refused delete must not touch the live store"
        assert "deleted_at" not in store._store_path(tmp_repo).read_text(encoding="utf-8")

    def test_the_deleted_view_says_unreadable_instead_of_empty(self, tmp_repo):
        self._tombstone_then_truncate(tmp_repo)

        view = store.list_tombstones(tmp_repo)

        assert view["ok"] is False
        assert "JSONDecodeError" in view["error"]
        assert view["tombstones"] == []

    def test_the_dashboard_reports_the_sidecar_separately_from_the_store(self, tmp_repo):
        self._tombstone_then_truncate(tmp_repo)
        summary = store.dashboard_summary(tmp_repo)
        assert summary["ok"] is True, "the live store is fine; only the sidecar is broken"
        assert summary["tombstones"]["ok"] is False
        assert "JSONDecodeError" in summary["tombstones"]["error"]
        assert summary["tombstones"]["count"] == 0

    def test_capture_deliberately_fails_open(self, tmp_repo):
        """Documented trade-off (see `_is_tombstoned`): failing closed would block EVERY
        capture in the repo — including decisions that were never deleted — on one corrupt
        file. The condition is surfaced instead of hidden."""
        self._tombstone_then_truncate(tmp_repo)
        stored, _entry_id = store.update_decision(tmp_repo, CACHE_DECISION, "sess-2",
                                                  created_by="human")
        assert stored is True
        assert store.deleted_diagnostics(tmp_repo)["ok"] is False


class TestNonObjectTombstone:
    """A sidecar whose `entries` list holds a non-dict item: it parses as JSON, so the list
    check alone let it through and `_find_match` then called `.get()` on a string — an
    AttributeError out of the MCP capture tool with the store lock held."""

    BROKEN = '{"entries": ["oops"]}'

    def test_capture_still_fails_open(self, tmp_repo):
        store._deleted_path(tmp_repo).write_text(self.BROKEN, encoding="utf-8")

        stored, entry_id = store.update_decision(tmp_repo, CACHE_DECISION, SESSION,
                                                 created_by="human")

        assert stored is True, "`_is_tombstoned` promises to fail OPEN on an unreadable sidecar"
        assert entry_id in _live_ids(tmp_repo)
        assert store.deleted_diagnostics(tmp_repo)["ok"] is False

    def test_the_deleted_view_says_unreadable_instead_of_empty(self, tmp_repo):
        store._deleted_path(tmp_repo).write_text(self.BROKEN, encoding="utf-8")

        view = store.list_tombstones(tmp_repo)

        assert view["ok"] is False
        assert "non-object" in view["error"]
        assert view["tombstones"] == []

    def test_a_delete_refuses_to_overwrite_it(self, tmp_repo):
        entry_id = _store_one(tmp_repo, QUEUE_DECISION)
        store._deleted_path(tmp_repo).write_text(self.BROKEN, encoding="utf-8")

        ok, msg = store.delete_decision(tmp_repo, entry_id)

        assert ok is False
        assert "unreadable" in msg
        assert store._deleted_path(tmp_repo).read_text(encoding="utf-8") == self.BROKEN
        assert _live_ids(tmp_repo) == [entry_id]


class TestDeletedDiagnostics:
    def test_a_readable_sidecar_is_ok(self, tmp_repo):
        store.delete_decision(tmp_repo, _store_one(tmp_repo, CACHE_DECISION))
        assert store.deleted_diagnostics(tmp_repo) == {"ok": True, "error": None}

    def test_a_missing_sidecar_is_ok_not_corrupt(self, tmp_repo):
        assert not store._deleted_path(tmp_repo).exists()
        assert store.deleted_diagnostics(tmp_repo) == {"ok": True, "error": None}

    def test_a_truncated_sidecar_reports_the_parse_error(self, tmp_repo):
        store._deleted_path(tmp_repo).write_text('{"entries": [{"id"', encoding="utf-8")
        diag = store.deleted_diagnostics(tmp_repo)
        assert diag["ok"] is False
        assert "JSONDecodeError" in diag["error"]

    def test_a_non_object_sidecar_is_not_ok(self, tmp_repo):
        store._deleted_path(tmp_repo).write_text("[]", encoding="utf-8")
        assert store.deleted_diagnostics(tmp_repo)["ok"] is False

    def test_undecodable_bytes_are_not_ok(self, tmp_repo):
        store._deleted_path(tmp_repo).write_bytes(b'{"entries": [], "x": "\xff\xfe"}')
        assert "UnicodeDecodeError" in store.deleted_diagnostics(tmp_repo)["error"]

    def test_an_empty_graveyard_is_ok_and_says_so(self, tmp_repo):
        _store_one(tmp_repo, CACHE_DECISION)
        assert store.list_tombstones(tmp_repo) == {"ok": True, "error": None, "tombstones": []}


# ── the deleted entry is gone from every live read ────────────────────────────

class TestAbsentFromLiveReads:
    def test_get_context_omits_it(self, tmp_repo):
        entry_id = _store_one(tmp_repo, CACHE_DECISION)
        _store_one(tmp_repo, QUEUE_DECISION)
        store.delete_decision(tmp_repo, entry_id)
        rendered = store.get_context(tmp_repo)
        assert "Redis" not in rendered
        assert "Celery" in rendered

    def test_get_context_on_last_decision_reads_as_empty(self, tmp_repo):
        store.delete_decision(tmp_repo, _store_one(tmp_repo, CACHE_DECISION))
        assert "No context stored" in store.get_context(tmp_repo)

    def test_pending_decision_leaves_the_review_queue(self, tmp_repo):
        ok, entry_id = store.update_decision(
            tmp_repo, "Never commit generated lockfiles to the repository", SESSION,
            subtype="constraint")
        assert ok and store._load(tmp_repo)["entries"][0]["status"] == "pending_approval"
        assert store.get_pending_decisions(tmp_repo)

        store.delete_decision(tmp_repo, entry_id)
        assert store.get_pending_decisions(tmp_repo) == []
        assert store.format_pending_review(tmp_repo) == "Nothing pending review."

    def test_session_start_payload_omits_it(self, tmp_repo):
        entry_id = _store_one(tmp_repo, CACHE_DECISION)
        store.delete_decision(tmp_repo, entry_id)
        payload = store.session_start_payload(tmp_repo, session_id=SESSION)
        assert "Redis" not in payload["context"]

    def test_retrieval_index_drops_it(self, tmp_repo):
        entry_id = _store_one(tmp_repo, CACHE_DECISION)
        _store_one(tmp_repo, QUEUE_DECISION)
        store.delete_decision(tmp_repo, entry_id)
        index = store._read_retrieval_index(tmp_repo)
        assert index is not None
        assert entry_id not in index["docs"]

    def test_overlap_report_omits_it(self, tmp_repo):
        first = _store_one(tmp_repo, "Always run database migrations before deploying",
                           subtype="constraint")
        _store_one(tmp_repo, "Always run database tests in continuous integration",
                   subtype="constraint")
        assert store.overlap_report(tmp_repo)
        store.delete_decision(tmp_repo, first)
        assert store.overlap_report(tmp_repo) == []


# ── one lock, ordered writes ──────────────────────────────────────────────────

class TestLockDiscipline:
    def test_delete_writes_both_files_in_one_locked_section(self, tmp_repo, tracker):
        entry_id = _store_one(tmp_repo, CACHE_DECISION)
        tracker.writes.clear()
        tracker.sections = 0

        assert store.delete_decision(tmp_repo, entry_id)[0]

        assert tracker.sections == 1, "delete must take the store lock exactly once"
        assert tracker.writes, "delete must write something"
        assert all(depth == 1 for _name, depth in tracker.writes), \
            "every file write must happen inside the lock"
        names = [name for name, _depth in tracker.writes]
        assert names.index(store._deleted_path(tmp_repo).name) < \
            names.index(store._store_path(tmp_repo).name), \
            "sidecar must be written first so a crash duplicates rather than loses the entry"

    def test_restore_writes_live_store_first_in_one_locked_section(self, tmp_repo, tracker):
        entry_id = _store_one(tmp_repo, CACHE_DECISION)
        store.delete_decision(tmp_repo, entry_id)
        tracker.writes.clear()
        tracker.sections = 0

        assert store.restore_decision(tmp_repo, entry_id)[0]

        assert tracker.sections == 1
        assert all(depth == 1 for _name, depth in tracker.writes)
        names = [name for name, _depth in tracker.writes]
        assert names.index(store._store_path(tmp_repo).name) < \
            names.index(store._deleted_path(tmp_repo).name), \
            "restore mirrors delete: live store first, so the crash window duplicates"


# ── restore ───────────────────────────────────────────────────────────────────

class TestRestore:
    def test_round_trip_returns_the_entry_unchanged(self, tmp_repo):
        entry_id = _store_one(tmp_repo, CACHE_DECISION)
        before = json.dumps(store._load(tmp_repo)["entries"][0], sort_keys=True)

        store.delete_decision(tmp_repo, entry_id)
        ok, msg = store.restore_decision(tmp_repo, entry_id)
        assert ok
        assert entry_id[:8] in msg

        after = store._load(tmp_repo)["entries"]
        assert len(after) == 1
        assert json.dumps(after[0], sort_keys=True) == before
        assert "deleted_at" not in after[0]
        assert "deleted_by" not in after[0]
        assert store.list_deleted(tmp_repo) == []

    def test_restored_entry_is_readable_again(self, tmp_repo):
        entry_id = _store_one(tmp_repo, CACHE_DECISION)
        store.delete_decision(tmp_repo, entry_id)
        store.restore_decision(tmp_repo, entry_id)
        assert "Redis" in store.get_context(tmp_repo)

    def test_restore_accepts_a_short_id(self, tmp_repo):
        entry_id = _store_one(tmp_repo, CACHE_DECISION)
        store.delete_decision(tmp_repo, entry_id)
        assert store.restore_decision(tmp_repo, entry_id[:8])[0]
        assert _live_ids(tmp_repo) == [entry_id]

    def test_unknown_id_is_a_noop(self, tmp_repo):
        entry_id = _store_one(tmp_repo, CACHE_DECISION)
        store.delete_decision(tmp_repo, entry_id)
        ok, msg = store.restore_decision(tmp_repo, "not-a-real-id")
        assert ok is False
        assert "not found" in msg
        assert len(store.list_deleted(tmp_repo)) == 1
        assert _live_ids(tmp_repo) == []

    def test_restore_with_no_sidecar_is_a_noop(self, tmp_repo):
        ok, _ = store.restore_decision(tmp_repo, "anything")
        assert ok is False
        assert not store._deleted_path(tmp_repo).exists()

    def test_restore_leaves_other_tombstones_alone(self, tmp_repo):
        first = _store_one(tmp_repo, CACHE_DECISION)
        second = _store_one(tmp_repo, QUEUE_DECISION)
        store.delete_decision(tmp_repo, first)
        store.delete_decision(tmp_repo, second)
        store.restore_decision(tmp_repo, first)
        assert [e["id"] for e in store.list_deleted(tmp_repo)] == [second]
        assert _live_ids(tmp_repo) == [first]


class TestRestoreIsIdempotent:
    def _crashed_delete(self, repo: str, entry_id: str) -> None:
        """The crash window `delete_decision` deliberately leaves open: the sidecar is written,
        the live store is not, so the entry sits in BOTH files ("recoverable, visible").

        Its own MonkeyPatch context, NOT the test's: `undo()` on the shared one would also
        revert `tmp_repo`'s STORE_DIR patch and point the rest of the test at the real
        `~/.contexer`."""
        def boom(_repo_path, _data):
            raise OSError("crashed between the two writes")

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(store, "_save", boom)
            with pytest.raises(OSError):
                store.delete_decision(repo, entry_id)

    def test_restoring_an_id_that_is_already_live_does_not_duplicate_it(self, tmp_repo):
        entry_id = _store_one(tmp_repo, CACHE_DECISION)
        self._crashed_delete(tmp_repo, entry_id)
        assert _live_ids(tmp_repo) == [entry_id]
        assert [e["id"] for e in store.list_deleted(tmp_repo)] == [entry_id]

        ok, msg = store.restore_decision(tmp_repo, entry_id)

        assert ok
        assert "already" in msg
        assert _live_ids(tmp_repo) == [entry_id], \
            "restore appended a second copy of a live id — the duplicate is unreachable"
        assert store.list_deleted(tmp_repo) == [], "the stale tombstone must be dropped"

    def test_the_surviving_copy_stays_editable_and_deletable(self, tmp_repo):
        # The whole harm of the duplicate: every id-taking function resolves only the first,
        # so the second could never be edited or deleted again.
        entry_id = _store_one(tmp_repo, CACHE_DECISION)
        self._crashed_delete(tmp_repo, entry_id)
        store.restore_decision(tmp_repo, entry_id)

        assert store.edit_decision(tmp_repo, entry_id, title="Cache hot reads")[0]
        assert store.delete_decision(tmp_repo, entry_id)[0]
        assert _live_ids(tmp_repo) == []


class TestRestoreIntoAFullStore:
    def test_restore_is_refused_rather_than_evicting_an_untombstoned_decision(self, tmp_repo,
                                                                             monkeypatch):
        monkeypatch.setattr(store, "MAX_ENTRIES", 2)
        keep = _store_one(tmp_repo, CACHE_DECISION)
        gone = _store_one(tmp_repo, QUEUE_DECISION)
        store.delete_decision(tmp_repo, gone)
        refill = _store_one(tmp_repo, SEARCH_DECISION)   # store back at capacity

        ok, msg = store.restore_decision(tmp_repo, gone)

        assert ok is False
        assert "maximum" in msg
        assert sorted(_live_ids(tmp_repo)) == sorted([keep, refill]), \
            "a restore evicted an unrelated decision, and it got no tombstone"
        assert [e["id"] for e in store.list_deleted(tmp_repo)] == [gone], \
            "the refused restore must leave the tombstone restorable"

    def test_restore_still_works_one_below_capacity(self, tmp_repo, monkeypatch):
        monkeypatch.setattr(store, "MAX_ENTRIES", 2)
        keep = _store_one(tmp_repo, CACHE_DECISION)
        gone = _store_one(tmp_repo, QUEUE_DECISION)
        store.delete_decision(tmp_repo, gone)

        assert store.restore_decision(tmp_repo, gone)[0]
        assert sorted(_live_ids(tmp_repo)) == sorted([keep, gone])


class TestListDeleted:
    def test_empty_when_nothing_was_deleted(self, tmp_repo):
        _store_one(tmp_repo, CACHE_DECISION)
        assert store.list_deleted(tmp_repo) == []

    def test_rows_carry_the_full_entry(self, tmp_repo):
        entry_id = _store_one(tmp_repo, CACHE_DECISION)
        store.delete_decision(tmp_repo, entry_id, actor="dev")
        row = store.list_deleted(tmp_repo)[0]
        for key in ("id", "type", "subtype", "content", "title", "status", "revisions",
                    "timestamp", "deleted_at", "deleted_by"):
            assert key in row, key
        assert row["deleted_by"] == "dev"


# ── resurrection guard ────────────────────────────────────────────────────────

MEMORY_FACT = """---
name: caching-policy
description: Cache hot reads in Redis with a sixty second time to live
metadata:
  node_type: memory
  type: project
  originSessionId: mem-0001
---

Cache hot reads in Redis with a sixty second time to live.
"""


def _memory_dir(tmp_path: Path, text: str = MEMORY_FACT) -> Path:
    mem = tmp_path / "memory"
    mem.mkdir(exist_ok=True)
    (mem / "caching-policy.md").write_text(text, encoding="utf-8")
    return mem


class TestResurrectionGuard:
    def test_memory_sync_import_does_not_resurrect(self, tmp_repo, tmp_path):
        # Through memory_sync.import_dir — the real SessionEnd path, not a direct store call.
        mem = _memory_dir(tmp_path)
        assert memory_sync.import_dir(mem, tmp_repo) == 1
        imported = _live_ids(tmp_repo)
        assert len(imported) == 1

        store.delete_decision(tmp_repo, imported[0])

        assert memory_sync.import_dir(mem, tmp_repo) == 0
        assert _live_ids(tmp_repo) == []
        assert "Redis" not in store.get_context(tmp_repo)

    def test_memory_sync_import_stays_silent(self, tmp_repo, tmp_path, capsys):
        mem = _memory_dir(tmp_path)
        memory_sync.import_dir(mem, tmp_repo)
        store.delete_decision(tmp_repo, _live_ids(tmp_repo)[0])
        capsys.readouterr()

        memory_sync.import_dir(mem, tmp_repo)

        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_memory_sync_import_leaves_the_store_untouched(self, tmp_repo, tmp_path):
        # A skipped import must not rewrite the store at all (the batch path only saves
        # when something was touched), so an unrelated live decision is unaffected.
        mem = _memory_dir(tmp_path)
        memory_sync.import_dir(mem, tmp_repo)
        store.delete_decision(tmp_repo, _live_ids(tmp_repo)[0])
        keep = _store_one(tmp_repo, QUEUE_DECISION)
        before = store._store_path(tmp_repo).read_text(encoding="utf-8")

        memory_sync.import_dir(mem, tmp_repo)

        assert store._store_path(tmp_repo).read_text(encoding="utf-8") == before
        assert _live_ids(tmp_repo) == [keep]

    def test_single_memory_upsert_does_not_resurrect(self, tmp_repo):
        assert store.upsert_memory_decision(
            tmp_repo, CACHE_DECISION, SESSION, "architecture", "src#one") == "created"
        store.delete_decision(tmp_repo, _live_ids(tmp_repo)[0])

        assert store.upsert_memory_decision(
            tmp_repo, CACHE_DECISION, SESSION, "architecture", "src#one") == "skipped"
        assert _live_ids(tmp_repo) == []

    def test_update_decision_does_not_resurrect(self, tmp_repo):
        entry_id = _store_one(tmp_repo, CACHE_DECISION)
        store.delete_decision(tmp_repo, entry_id)

        stored, new_id = store.update_decision(tmp_repo, CACHE_DECISION, "sess-2")
        assert stored is False
        assert new_id is None
        assert _live_ids(tmp_repo) == []

    def test_update_decision_blocks_a_reworded_restatement(self, tmp_repo):
        # The guard uses _find_match, so it catches the same >70% overlap band the novelty
        # filter does — not just byte-identical text.
        entry_id = _store_one(tmp_repo, CACHE_DECISION)
        store.delete_decision(tmp_repo, entry_id)
        reworded = "Use Redis for hot-read caching because Postgres round-trips dominated the latency"
        assert store.update_decision(tmp_repo, reworded, "sess-2") == (False, None)

    def test_update_decision_stays_silent(self, tmp_repo, capsys):
        store.delete_decision(tmp_repo, _store_one(tmp_repo, CACHE_DECISION))
        capsys.readouterr()
        store.update_decision(tmp_repo, CACHE_DECISION, "sess-2")
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == ""

    def test_miner_bootstrap_does_not_resurrect(self, tmp_repo):
        # Through bootstrap_apply, which is the miner's only write path.
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "mod.py").write_text(_snake_file(25), encoding="utf-8")
        store.bootstrap_apply(tmp_repo, SESSION)
        mined = next(e for e in store._load(tmp_repo)["entries"]
                     if "snake_case" in e["content"])

        store.delete_decision(tmp_repo, mined["id"])

        result = store.bootstrap_apply(tmp_repo, SESSION)
        assert result["stored"] == 0
        assert result["skipped"] >= 1
        assert not any("snake_case" in e["content"]
                       for e in store._load(tmp_repo)["entries"])

    def test_miner_bootstrap_does_not_resurrect_the_stack_entry(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        (Path(tmp_repo) / "pyproject.toml").write_text(
            '[project]\nname = "widgets-api"\nrequires-python = ">=3.12"\n'
            'dependencies = ["fastapi", "sqlalchemy", "boto3"]\n', encoding="utf-8")
        store.bootstrap_apply(tmp_repo, SESSION)
        stack = next(e for e in store._load(tmp_repo)["entries"]
                     if e["content"].startswith("Stack: "))

        store.delete_decision(tmp_repo, stack["id"])
        store.bootstrap_apply(tmp_repo, SESSION)

        assert not any(e["content"].startswith("Stack: ")
                       for e in store._load(tmp_repo)["entries"])

    def test_unrelated_content_still_stores(self, tmp_repo):
        store.delete_decision(tmp_repo, _store_one(tmp_repo, CACHE_DECISION))
        stored, entry_id = store.update_decision(tmp_repo, QUEUE_DECISION, "sess-2",
                                                 created_by="human")
        assert stored is True
        assert _live_ids(tmp_repo) == [entry_id]

    def test_restore_lifts_the_guard(self, tmp_repo, tmp_path):
        mem = _memory_dir(tmp_path)
        memory_sync.import_dir(mem, tmp_repo)
        entry_id = _live_ids(tmp_repo)[0]
        store.delete_decision(tmp_repo, entry_id)
        store.restore_decision(tmp_repo, entry_id)

        # The tombstone is gone, so a fresh import is an ordinary keyed no-op, not a block.
        assert memory_sync.import_dir(mem, tmp_repo) == 0
        assert _live_ids(tmp_repo) == [entry_id]

    def test_a_second_repo_is_unaffected(self, tmp_repo, tmp_path):
        other = str(tmp_path / "other-repo")
        store.delete_decision(tmp_repo, _store_one(tmp_repo, CACHE_DECISION))
        stored, _ = store.update_decision(other, CACHE_DECISION, SESSION, created_by="human")
        assert stored is True, "a tombstone in one repo must not filter another repo"

    def test_explicit_user_constraint_is_not_blocked(self, tmp_repo):
        # capture_user_constraint is deliberately NOT guarded: a developer re-typing a rule
        # after deleting it is an explicit act, same reasoning as the 'ignored' exemption.
        prompt = "Always run the linter before every commit"
        entry_id, _, _ = store.capture_user_constraint(tmp_repo, prompt, SESSION)
        assert entry_id
        store.delete_decision(tmp_repo, entry_id)
        again, content, status = store.capture_user_constraint(tmp_repo, prompt, "sess-2")
        assert again and content and status == "approved"


# ── load_diagnostics ──────────────────────────────────────────────────────────

class TestLoadDiagnostics:
    def test_good_store_is_ok(self, tmp_repo):
        _store_one(tmp_repo, CACHE_DECISION)
        assert store.load_diagnostics(tmp_repo) == {"ok": True, "error": None}

    def test_missing_store_is_ok_not_corrupt(self, tmp_repo):
        # An absent file is a genuinely empty store, not a broken one.
        assert not store._store_path(tmp_repo).exists()
        assert store.load_diagnostics(tmp_repo) == {"ok": True, "error": None}

    def test_empty_but_valid_store_is_ok(self, tmp_repo):
        store._save(tmp_repo, {"repo_path": tmp_repo, "entries": []})
        assert store.load_diagnostics(tmp_repo) == {"ok": True, "error": None}

    def test_truncated_store_reports_the_parse_error(self, tmp_repo):
        _store_one(tmp_repo, CACHE_DECISION)
        path = store._store_path(tmp_repo)
        path.write_text(path.read_text(encoding="utf-8")[:120], encoding="utf-8")

        diag = store.load_diagnostics(tmp_repo)
        assert diag["ok"] is False
        assert "JSONDecodeError" in diag["error"]
        # _load still degrades silently — diagnostics is the ONLY thing that tells them apart.
        assert store._load(tmp_repo)["entries"] == []

    def test_non_object_store_is_not_ok(self, tmp_repo):
        store._store_path(tmp_repo).write_text("[]", encoding="utf-8")
        diag = store.load_diagnostics(tmp_repo)
        assert diag["ok"] is False
        assert "entries" in diag["error"]

    def test_missing_entries_key_is_not_ok(self, tmp_repo):
        store._store_path(tmp_repo).write_text('{"repo_path": "/x"}', encoding="utf-8")
        assert store.load_diagnostics(tmp_repo)["ok"] is False

    def test_undecodable_bytes_are_not_ok(self, tmp_repo):
        store._store_path(tmp_repo).write_bytes(b'{"entries": [], "x": "\xff\xfe"}')
        diag = store.load_diagnostics(tmp_repo)
        assert diag["ok"] is False
        assert "UnicodeDecodeError" in diag["error"]


# ── slug addressing: a corrupt store must be reachable, and a slug must stay stable ──

def _truncate_store(repo: str) -> None:
    path = store._store_path(repo)
    path.write_text(path.read_text(encoding="utf-8")[:120], encoding="utf-8")


class TestSlugAddressing:
    def test_a_corrupt_store_is_addressable_and_reports_itself(self, tmp_repo):
        # The reported break: the store LIST showed ok:false, but the per-store lookup 404'd,
        # so clicking that row got a generic error page instead of "store unreadable".
        _store_one(tmp_repo, CACHE_DECISION)
        _truncate_store(tmp_repo)
        slug = store._slug(tmp_repo)

        resolved = store.resolve_store(slug)

        assert resolved is not None, "a known slug must not read as an unknown one"
        assert resolved["ok"] is False
        assert resolved["repo_path"] == ""
        assert "JSONDecodeError" in resolved["error"]

    def test_the_degraded_summary_says_unreadable_not_empty(self, tmp_repo):
        _store_one(tmp_repo, CACHE_DECISION)
        _truncate_store(tmp_repo)

        payload = store.store_summary(store._slug(tmp_repo))

        assert payload is not None
        assert payload["ok"] is False
        assert "JSONDecodeError" in payload["error"]
        assert payload["counts"]["decisions"] == 0
        assert payload["repo_path"] == ""

    def test_the_degraded_summary_carries_the_same_keys_as_a_healthy_one(self, tmp_repo):
        _store_one(tmp_repo, CACHE_DECISION)
        healthy = store.store_summary(store._slug(tmp_repo))
        _truncate_store(tmp_repo)
        degraded = store.store_summary(store._slug(tmp_repo))
        assert set(degraded) == set(healthy), "a caller must be able to branch on ok alone"
        assert set(degraded["counts"]) == set(healthy["counts"])

    def test_a_healthy_summary_is_the_dashboard_plus_the_slug(self, tmp_repo):
        _store_one(tmp_repo, CACHE_DECISION)
        slug = store._slug(tmp_repo)
        payload = store.store_summary(slug)
        assert payload["slug"] == slug
        assert payload["repo_path"] == tmp_repo
        assert payload["counts"]["decisions"] == 1
        assert payload["ok"] is True

    def test_an_unknown_slug_is_none(self, tmp_repo):
        _store_one(tmp_repo, CACHE_DECISION)
        assert store.resolve_store("no-such-store-1234") is None
        assert store.store_summary("no-such-store-1234") is None

    @pytest.mark.parametrize("slug", ["", "..", "a/b", "a\\b", "x\0y"])
    def test_a_traversal_slug_is_refused(self, tmp_repo, slug):
        _store_one(tmp_repo, CACHE_DECISION)
        assert store.resolve_store(slug) is None
        assert store.resolve_store_slug(slug) is None

    def test_the_reserved_files_are_not_addressable(self, tmp_repo):
        _store_one(tmp_repo, CACHE_DECISION)
        store.update_global_decision("Never log secrets", SESSION, "constraint")
        store.delete_decision(tmp_repo, _store_one(tmp_repo, QUEUE_DECISION))
        for slug in ("_global", "ui", f"{store._slug(tmp_repo)}.deleted"):
            assert store.resolve_store(slug) is None, slug

    def test_a_legacy_named_store_keeps_its_slug_across_the_rename(self, tmp_repo):
        # The first `_load` renames `<legacy>.json` to `<legacy>-<hash>.json`, which used to
        # invalidate the slug the client was already holding — every later poll 404'd.
        store.STORE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        legacy = store.STORE_DIR / f"{store._legacy_slug(tmp_repo)}.json"
        legacy.write_text(json.dumps({"repo_path": tmp_repo, "entries": []}), encoding="utf-8")
        slug = legacy.stem
        assert store.resolve_store_slug(slug) == tmp_repo

        store._load(tmp_repo)                      # migrates the file to the hashed name
        assert not legacy.exists()

        assert store.resolve_store_slug(slug) == tmp_repo, \
            "the client's slug stopped resolving the moment the store was opened"
        assert store.store_summary(slug)["repo_path"] == tmp_repo
        assert store.resolve_store_slug(store._slug(tmp_repo)) == tmp_repo


# ── the 10-second poll reads each file once ───────────────────────────────────

class TestPollCost:
    def test_dashboard_summary_reads_each_file_once(self, tmp_repo, read_counts):
        _store_one(tmp_repo, CACHE_DECISION)
        store.delete_decision(tmp_repo, _store_one(tmp_repo, QUEUE_DECISION))
        read_counts.clear()

        store.dashboard_summary(tmp_repo)

        assert read_counts[store._store_path(tmp_repo).name] == 1
        assert read_counts[store._deleted_path(tmp_repo).name] == 1

    def test_list_decisions_reads_the_store_once(self, tmp_repo, read_counts):
        _store_one(tmp_repo, CACHE_DECISION)
        read_counts.clear()

        store.list_decisions(tmp_repo, limit=25)

        assert read_counts[store._store_path(tmp_repo).name] == 1

    def test_list_decisions_files_filter_reads_the_store_once(self, tmp_repo, read_counts):
        # decisions_for_files must be given the already-loaded rows (`decisions=rows`), not
        # left to reload the store itself — otherwise the files filter would silently double
        # the poll's file I/O every time a file filter is active.
        _store_one(tmp_repo, CACHE_DECISION, source_files=["cache/redis.py"])
        read_counts.clear()

        store.list_decisions(tmp_repo, files=["cache/redis.py"])

        assert read_counts[store._store_path(tmp_repo).name] == 1

    def test_collapsing_the_reads_kept_the_corrupt_signal(self, tmp_repo):
        _store_one(tmp_repo, CACHE_DECISION)
        _truncate_store(tmp_repo)
        assert store.dashboard_summary(tmp_repo)["ok"] is False
        assert store.list_decisions(tmp_repo)["ok"] is False
        assert store.list_decisions(tmp_repo)["decisions"] == []

    def test_an_empty_store_still_reads_as_ok(self, tmp_repo):
        assert store.dashboard_summary(tmp_repo)["ok"] is True
        assert store.dashboard_summary(tmp_repo)["mtime"] is None
        assert store.list_decisions(tmp_repo)["ok"] is True
