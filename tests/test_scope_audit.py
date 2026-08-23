"""Cross-store scope audit: decisions saved into the wrong repo's store."""
import json

import pytest

from contexer import scope_audit, store


@pytest.fixture
def store_dir(tmp_path, monkeypatch):
    d = tmp_path / ".contexer"
    d.mkdir()
    monkeypatch.setattr(store, "STORE_DIR", d)
    return d


def _write_store(store_dir, repo, entries):
    """Write a per-repo store file the way _save would name it."""
    path = store_dir / f"{store.repo_slug(repo)}.json"
    path.write_text(json.dumps({"repo_path": repo, "entries": entries}))
    return path


def _decision(eid, session_id, ts="2026-08-03T12:00:00+00:00", **extra):
    e = {"type": "decision", "id": eid, "session_id": session_id, "timestamp": ts,
         "content": f"content for {eid}", "title": f"title for {eid}"}
    e.update(extra)
    return e


class TestAuditSessions:
    def test_clean_when_each_session_stays_in_one_store(self, store_dir):
        _write_store(store_dir, "/repo/a", [_decision("a1", "sess-a")])
        _write_store(store_dir, "/repo/b", [_decision("b1", "sess-b")])
        assert scope_audit.audit_sessions() == []

    def test_flags_a_session_split_across_two_stores(self, store_dir):
        _write_store(store_dir, "/repo/right", [_decision("r1", "sess-1")])
        _write_store(store_dir, "/repo/wrong", [_decision("w1", "sess-1")])

        (row,) = scope_audit.audit_sessions()
        assert row["session_id"] == "sess-1"
        assert sorted(s["repo"] for s in row["stores"]) == ["/repo/right", "/repo/wrong"]

    def test_reports_every_store_a_session_touched(self, store_dir):
        # The real case scattered one session across three projects, not two.
        for repo in ("/repo/a", "/repo/b", "/repo/c"):
            _write_store(store_dir, repo, [_decision(f"{repo[-1]}1", "sess-1")])
        (row,) = scope_audit.audit_sessions()
        assert len(row["stores"]) == 3

    def test_global_store_never_participates(self, store_dir):
        # Global decisions are cross-repo BY DESIGN — they are supposed to apply everywhere.
        _write_store(store_dir, "/repo/a", [_decision("a1", "sess-1")])
        (store_dir / f"{store.GLOBAL_SLUG}.json").write_text(
            json.dumps({"entries": [_decision("g1", "sess-1")]}))
        assert scope_audit.audit_sessions() == []

    def test_sidecars_are_not_read_as_stores(self, store_dir):
        _write_store(store_dir, "/repo/a", [_decision("a1", "sess-1")])
        (store_dir / f".retrieval_index_{store.repo_slug('/repo/a')}.json").write_text(
            json.dumps({"v": 2, "docs": {"x": {}}}))
        (store_dir / ".team_something.json").write_text(
            json.dumps({"entries": [_decision("t1", "sess-1")]}))
        assert scope_audit.audit_sessions() == []

    def test_tombstone_sidecar_is_not_a_second_store(self, store_dir):
        # `<slug>.deleted.json` carries the SAME shape as a real store, so a hand-rolled
        # "skip dotfiles and _global" filter reads a repo's deleted decisions as a second
        # store FOR THE SAME REPO — reporting one repo twice and telling the developer to
        # re-capture a decision they deliberately deleted.
        _write_store(store_dir, "/repo/a", [_decision("a1", "sess-1")])
        (store_dir / f"{store.repo_slug('/repo/a')}.deleted.json").write_text(
            json.dumps({"repo_path": "/repo/a", "entries": [_decision("a2", "sess-1")]}))
        assert scope_audit.audit_sessions() == []

    def test_console_statefile_is_not_a_store(self, store_dir):
        _write_store(store_dir, "/repo/a", [_decision("a1", "sess-1")])
        (store_dir / "ui.json").write_text(
            json.dumps({"entries": [_decision("u1", "sess-1")]}))
        assert scope_audit.audit_sessions() == []

    def test_malformed_entry_costs_its_title_not_the_audit(self, store_dir):
        # Entries here are RAW json — never run through _load's migration — so revision-model
        # helpers can meet shapes a live store never hands them. That must not raise out of a
        # read-only report.
        _write_store(store_dir, "/repo/a",
                     [_decision("a1", "sess-1", title="", revisions=["oops"])])
        _write_store(store_dir, "/repo/b", [_decision("b1", "sess-1")])

        (row,) = scope_audit.audit_sessions()
        assert len(row["stores"]) == 2
        scope_audit.format_audit([row])              # renders without raising

    def test_memory_imports_never_participate(self, store_dir):
        # memory_sync stores an unattributed fact under the LITERAL id "memory-sync", so every
        # repo that ever imported one shares it — a guaranteed false pair. And even a real
        # originSessionId records where the FACT came from, not which repo a writer targeted.
        for repo, eid in (("/repo/a", "a1"), ("/repo/b", "b1")):
            _write_store(store_dir, repo, [_decision(eid, "memory-sync", created_by="memory",
                                                     memory_key="notes.md#rule")])
        assert scope_audit.audit_sessions() == []

    def test_memory_import_detected_without_the_provenance_field(self, store_dir):
        # Read raw, without _load's migration — an older entry may carry only one marker.
        _write_store(store_dir, "/repo/a", [_decision("a1", "real-sid", memory_key="n.md#x")])
        _write_store(store_dir, "/repo/b", [_decision("b1", "real-sid", memory_key="n.md#x")])
        assert scope_audit.audit_sessions() == []

    def test_a_real_session_alongside_memory_imports_is_still_flagged(self, store_dir):
        _write_store(store_dir, "/repo/a", [
            _decision("m1", "memory-sync", created_by="memory", memory_key="n.md#x"),
            _decision("a1", "sess-1"),
        ])
        _write_store(store_dir, "/repo/b", [_decision("b1", "sess-1")])

        (row,) = scope_audit.audit_sessions()
        assert row["session_id"] == "sess-1"
        assert [e["id"] for s in row["stores"] for e in s["entries"]] == ["a1", "b1"]

    def test_recurrence_from_another_session_is_not_evidence(self, store_dir):
        # session_ids accumulates every session that has TOUCHED an entry; a recurrence
        # recorded from a second session is normal and must not read as a misrouted write.
        _write_store(store_dir, "/repo/a",
                     [_decision("a1", "sess-1", session_ids=["sess-1", "sess-2"])])
        _write_store(store_dir, "/repo/b", [_decision("b1", "sess-2")])
        assert scope_audit.audit_sessions() == []

    def test_tasks_and_entries_without_a_session_are_skipped(self, store_dir):
        _write_store(store_dir, "/repo/a", [
            {"type": "task", "id": "t1", "session_id": "sess-1"},
            _decision("a1", ""),
        ])
        _write_store(store_dir, "/repo/b", [_decision("b1", "sess-1")])
        assert scope_audit.audit_sessions() == []

    def test_corrupt_store_is_skipped_not_raised(self, store_dir):
        _write_store(store_dir, "/repo/a", [_decision("a1", "sess-1")])
        _write_store(store_dir, "/repo/b", [_decision("b1", "sess-1")])
        (store_dir / "broken.json").write_text("{ not json")
        (store_dir / "wrongshape.json").write_text(json.dumps({"entries": "nope"}))

        (row,) = scope_audit.audit_sessions()
        assert len(row["stores"]) == 2

    def test_entries_carry_stamped_provenance(self, store_dir):
        _write_store(store_dir, "/repo/a", [_decision("a1", "sess-1", repo_source="session")])
        _write_store(store_dir, "/repo/b", [_decision("b1", "sess-1", repo_source="pointer")])

        (row,) = scope_audit.audit_sessions()
        sources = {e["repo_source"] for s in row["stores"] for e in s["entries"]}
        assert sources == {"session", "pointer"}

    def test_unhashable_session_id_does_not_raise(self, store_dir):
        # A raw store can hold anything here; an unhashable value used as a dict key would
        # terminate the whole read-only audit on one malformed entry.
        _write_store(store_dir, "/repo/a", [_decision("a1", ["not", "a", "string"])])
        _write_store(store_dir, "/repo/b", [_decision("b1", "sess-1")])
        assert scope_audit.audit_sessions() == []

    def test_numeric_session_id_does_not_reach_the_report(self, store_dir):
        # An int IS hashable, so it survives the grouping and only breaks later, in the
        # report's slicing — coerced away at the same point as the unhashable case.
        _write_store(store_dir, "/repo/a", [_decision("a1", 12345)])
        _write_store(store_dir, "/repo/b", [_decision("b1", 12345)])
        assert scope_audit.audit_sessions() == []
        scope_audit.format_audit(scope_audit.audit_sessions())

    def _stray_worktree_pair(self, store_dir, monkeypatch):
        """A pre-fix stray worktree store sitting beside its canonical file.

        Written under EXPLICIT filenames, not via `_slug`: `_slug` itself canonicalizes
        through `_canonical_store_key`, so patching that helper would collapse both writes
        onto one filename and the fixture would silently test nothing. A real pre-fix stray
        has its own slug precisely because it was written before that canonicalization.
        """
        monkeypatch.setattr(store, "_canonical_store_key",
                            lambda p: "/repo/main" if p in ("/repo/main", "/repo/wt") else p)
        (store_dir / "canonical.json").write_text(json.dumps(
            {"repo_path": "/repo/main", "entries": [_decision("m1", "sess-1")]}))
        (store_dir / "stray.json").write_text(json.dumps(
            {"repo_path": "/repo/wt", "entries": [_decision("w1", "sess-1")]}))

    def test_two_store_files_for_one_repo_are_one_store(self, store_dir, monkeypatch):
        # Both files describe ONE repo, so a session writing to both is correctly scoped —
        # reporting it would send the developer to retire records that are fine.
        self._stray_worktree_pair(store_dir, monkeypatch)
        assert scope_audit.audit_sessions() == []

    def test_merged_repo_still_pairs_with_a_genuinely_different_one(self, store_dir, monkeypatch):
        self._stray_worktree_pair(store_dir, monkeypatch)
        (store_dir / "other.json").write_text(json.dumps(
            {"repo_path": "/repo/other", "entries": [_decision("o1", "sess-1")]}))

        (row,) = scope_audit.audit_sessions()
        assert len(row["stores"]) == 2                       # merged pair + the real other
        merged = next(s for s in row["stores"] if len(s["paths"]) == 2)
        assert [e["id"] for e in merged["entries"]] == ["m1", "w1"]

    def test_a_repo_that_no_longer_exists_is_labelled(self, store_dir):
        # A stray from a REMOVED worktree cannot be merged with its main worktree — the .git
        # file is gone and `git worktree list` no longer enumerates it, so nothing on disk
        # records the link. The row is labelled rather than guessed at.
        _write_store(store_dir, "/repo/gone-worktree", [_decision("g1", "sess-1")])
        _write_store(store_dir, str(store_dir), [_decision("h1", "sess-1")])

        rows = scope_audit.audit_sessions()
        by_repo = {s["repo"]: s for r in rows for s in r["stores"]}
        assert by_repo["/repo/gone-worktree"]["missing"] is True
        assert by_repo[str(store_dir)]["missing"] is False       # this one is on disk
        assert "path no longer exists" in scope_audit.format_audit(rows)

    def test_unstattable_path_is_not_accused_of_being_gone(self, store_dir, monkeypatch):
        monkeypatch.setattr(scope_audit.os.path, "exists",
                            lambda p: (_ for _ in ()).throw(OSError("dead mount")))
        _write_store(store_dir, "/repo/a", [_decision("a1", "sess-1")])
        _write_store(store_dir, "/repo/b", [_decision("b1", "sess-1")])

        rows = scope_audit.audit_sessions()
        assert all(s["missing"] is False for r in rows for s in r["stores"])

    def test_store_with_an_unreadable_repo_path_is_never_merged_blindly(self, store_dir):
        for name, eid in (("one.json", "a1"), ("two.json", "b1")):
            (store_dir / name).write_text(
                json.dumps({"entries": [_decision(eid, "sess-1")]}))   # no repo_path
        (row,) = scope_audit.audit_sessions()
        assert len(row["stores"]) == 2

    def test_null_timestamp_does_not_raise(self, store_dir):
        # `.get("timestamp", "")` still returns None for a key present with a JSON null, and
        # None then blows up the sort and the max() below it. Entries are read raw.
        _write_store(store_dir, "/repo/a", [_decision("a1", "sess-1", timestamp=None)])
        _write_store(store_dir, "/repo/b", [_decision("b1", "sess-1")])

        (row,) = scope_audit.audit_sessions()
        assert len(row["stores"]) == 2
        scope_audit.format_audit([row])              # slicing survives it too

    def test_non_string_fields_do_not_raise(self, store_dir):
        _write_store(store_dir, "/repo/a", [_decision("a1", "sess-1", id=42, title=["x"])])
        _write_store(store_dir, "/repo/b", [_decision("b1", "sess-1")])
        scope_audit.format_audit(scope_audit.audit_sessions())

    def test_missing_store_dir_is_empty_not_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "STORE_DIR", tmp_path / "does-not-exist")
        assert scope_audit.audit_sessions() == []


class TestFormatAudit:
    def test_clean_report(self):
        assert "No cross-store sessions" in scope_audit.format_audit([])

    def test_report_names_stores_files_and_provenance(self, store_dir):
        _write_store(store_dir, "/repo/right", [_decision("r1", "sess-1")])
        _write_store(store_dir, "/repo/wrong",
                     [_decision("w1", "sess-1", repo_source="pointer")])

        out = scope_audit.format_audit(scope_audit.audit_sessions())
        assert "/repo/right" in out and "/repo/wrong" in out
        assert "[via pointer]" in out
        assert "Nothing was changed" in out          # read-only promise stated to the reader

    def test_report_does_not_assert_a_defect_as_fact(self, store_dir):
        # The id is a WRITE-SESSION id — for MCP captures a per-PROCESS uuid4 — so a
        # deliberate cross-repo capture produces this exact shape with nothing wrong.
        _write_store(store_dir, "/repo/a", [_decision("a1", "sess-1")])
        _write_store(store_dir, "/repo/b", [_decision("b1", "sess-1")])

        out = scope_audit.format_audit(scope_audit.audit_sessions())
        assert "normally belongs" in out and "Check each one" in out

    def test_remediation_does_not_point_at_a_surface_that_cannot_show_them(self, store_dir):
        # `contexer review` lists only decisions PENDING approval in the CURRENT repo; a
        # misrouted decision is normally already approved and in another repo.
        _write_store(store_dir, "/repo/a", [_decision("a1", "sess-1")])
        _write_store(store_dir, "/repo/b", [_decision("b1", "sess-1")])

        out = scope_audit.format_audit(scope_audit.audit_sessions())
        assert 'action="ignore"' in out
        assert "will not show them" in out

    def test_entry_list_is_capped_per_store(self, store_dir):
        many = [_decision(f"a{i}", "sess-1", ts=f"2026-08-{i + 1:02d}T12:00:00+00:00")
                for i in range(scope_audit._MAX_ENTRIES_SHOWN + 5)]
        _write_store(store_dir, "/repo/a", many)
        _write_store(store_dir, "/repo/b", [_decision("b1", "sess-1")])

        out = scope_audit.format_audit(scope_audit.audit_sessions())
        assert f"showing {scope_audit._MAX_ENTRIES_SHOWN} of {len(many)}" in out
        assert "(5 more)" in out
