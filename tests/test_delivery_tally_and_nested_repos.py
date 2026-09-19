"""Two independent prerequisites for measuring whether a decision does any work.

Both were found by auditing a real store (307 decisions, 156 unratified):

  - bootstrap counted the same convention once per repo copy under `.claude/worktrees/`,
    which corrupts BOTH halves of a delivery ratio: the denominator inflates, and delivery
    credit splits across duplicates so one copy always reads as never used.
  - delivery was recorded per session and discarded, so "has this decision ever been
    rendered" had no durable answer at all.
"""

from pathlib import Path

from contexer import bootstrap, sidecars, store, working_set


# ── #310: the scan must not descend into a nested checkout ────────────────────

def _doc(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


class TestNestedRepoRootsAreNotScanned:
    RULE = "# Contributing\n\n- **No premature abstractions.**\n"

    def test_worktree_copy_of_a_doc_is_not_a_second_source(self, tmp_path):
        _doc(tmp_path / "CONTRIBUTING.md", self.RULE)
        # A linked git worktree: `.git` is a FILE here, not a directory.
        wt = tmp_path / ".claude" / "worktrees" / "agent-a3fe4056"
        _doc(wt / "CONTRIBUTING.md", self.RULE)
        (wt / ".git").write_text("gitdir: /elsewhere/.git/worktrees/agent\n", encoding="utf-8")

        found = [p for p in bootstrap._paths(tmp_path) if p.name == "CONTRIBUTING.md"]

        assert len(found) == 1, [str(p) for p in found]
        assert found[0] == tmp_path / "CONTRIBUTING.md"

    def test_ordinary_clone_nested_in_the_tree_is_also_skipped(self, tmp_path):
        _doc(tmp_path / "README.md", "# Root\n")
        vendored = tmp_path / "third_party" / "somelib"
        _doc(vendored / "README.md", "# Vendored\n")
        (vendored / ".git").mkdir()          # a normal clone: `.git` is a DIRECTORY

        names = {p.relative_to(tmp_path).as_posix() for p in bootstrap._paths(tmp_path)}

        assert "README.md" in names
        assert "third_party/somelib/README.md" not in names

    def test_dot_claude_itself_is_still_scanned(self, tmp_path):
        """The allowlist exists for a reason - only nested CHECKOUTS are excluded."""
        _doc(tmp_path / ".claude" / "guide.md", "# Project guide\n")

        names = {p.relative_to(tmp_path).as_posix() for p in bootstrap._paths(tmp_path)}

        assert ".claude/guide.md" in names

    def test_nested_repo_root_predicate_is_exact(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        assert bootstrap._nested_repo_root(plain) is False
        (plain / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
        assert bootstrap._nested_repo_root(plain) is True


# ── #312: delivery must be durable, and bounded by decisions not by prompts ───

class TestDurableDeliveryTally:
    def test_rendering_a_decision_records_a_durable_count(self, tmp_repo):
        working_set.record_deliveries(
            tmp_repo, "sess-1", [{"scope": "personal", "id": "dec-a", "fingerprint": "fp1"}])

        rows = working_set.read_delivery_tally(tmp_repo)
        assert rows["personal:dec-a"]["renders"] == 1

    def test_tally_outlives_the_session_ledger(self, tmp_repo):
        """The whole point: the session sidecar is SESSION-lived, this is not."""
        working_set.record_deliveries(
            tmp_repo, "sess-1", [{"scope": "personal", "id": "dec-a", "fingerprint": "fp1"}])
        working_set.path(tmp_repo, "sess-1").unlink()     # session ends

        assert working_set.records(tmp_repo, "sess-1") == []
        assert working_set.read_delivery_tally(tmp_repo)["personal:dec-a"]["renders"] == 1

    def test_separate_sessions_accumulate(self, tmp_repo):
        for session in ("s1", "s2", "s3"):
            working_set.record_deliveries(
                tmp_repo, session,
                [{"scope": "personal", "id": "dec-a", "fingerprint": "fp1"}])

        row = working_set.read_delivery_tally(tmp_repo)["personal:dec-a"]
        assert row["renders"] == 3
        assert row["first"] <= row["last"]

    def test_scope_is_part_of_identity(self, tmp_repo):
        working_set.record_deliveries(tmp_repo, "s1", [
            {"scope": "personal", "id": "same-id", "fingerprint": "fp1"},
            {"scope": "global", "id": "same-id", "fingerprint": "fp2"},
        ])

        rows = working_set.read_delivery_tally(tmp_repo)
        assert set(rows) == {"personal:same-id", "global:same-id"}

    def test_never_delivered_decisions_are_simply_absent(self, tmp_repo):
        working_set.record_deliveries(
            tmp_repo, "s1", [{"scope": "personal", "id": "used", "fingerprint": "fp"}])

        rows = working_set.read_delivery_tally(tmp_repo)
        assert "personal:unused" not in rows, "absence is the signal; do not invent a zero row"

    def test_size_is_bounded_by_decisions_not_by_prompt_volume(self, tmp_repo):
        """The reason this is a tally and not an event log: a long measurement window must
        not silently drop its own start the way the tail-capped retrieval_log would."""
        for i in range(400):
            working_set.record_deliveries(
                tmp_repo, f"s{i}",
                [{"scope": "personal", "id": "hot", "fingerprint": f"fp{i}"}])

        rows = working_set.read_delivery_tally(tmp_repo)
        assert len(rows) == 1
        assert rows["personal:hot"]["renders"] == 400

    def test_eviction_drops_the_least_recently_delivered(self, tmp_repo):
        over = store.MAX_ENTRIES + 25
        working_set.record_deliveries(
            tmp_repo, "s0",
            [{"scope": "personal", "id": f"d{i}", "fingerprint": "f"} for i in range(over)])

        rows = working_set.read_delivery_tally(tmp_repo)
        assert len(rows) == store.MAX_ENTRIES
        assert "personal:d0" not in rows            # oldest written, first evicted
        assert f"personal:d{over - 1}" in rows

    def test_malformed_rows_are_dropped_not_trusted(self, tmp_repo):
        store.ensure_store_dir()
        working_set.delivery_path(tmp_repo).write_text(
            '{"v":2,"rows":{'
            '"personal:ok":{"renders":2,"first":1.0,"last":2.0},'
            '"nonnumeric":{"renders":"x","first":1.0,"last":2.0},'
            '"zero":{"renders":0,"first":1.0,"last":2.0},'
            '"no-last":{"renders":2,"first":1.0},'
            '"bad-last":{"renders":2,"first":1.0,"last":"soon"}}}',
            encoding="utf-8")

        rows = working_set.read_delivery_tally(tmp_repo)
        assert set(rows) == {"personal:ok"}

    def test_unreadable_tally_reads_empty_and_never_raises(self, tmp_repo):
        store.ensure_store_dir()
        working_set.delivery_path(tmp_repo).write_text("{not json", encoding="utf-8")
        assert working_set.read_delivery_tally(tmp_repo) == {}

    def test_a_tally_failure_does_not_cost_the_session_its_credit(self, tmp_repo, monkeypatch):
        """Suppression credit is what the developer actually feels next prompt; the tally is
        bookkeeping. Ordering must favour the former."""
        monkeypatch.setattr(working_set, "record_delivery_tally",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
        delivered = [{"scope": "personal", "id": "dec-a", "fingerprint": "fp1"}]

        try:
            working_set.record_deliveries(tmp_repo, "s1", delivered)
        except OSError:
            pass

        assert working_set.has_credit(
            working_set.records(tmp_repo, "s1"), "personal", "dec-a", "fp1")

    def test_tally_survives_the_session_sweep(self, tmp_repo):
        """SESSION (7d) would erase a two-week measurement the moment the developer spends a
        week elsewhere. COLD_REPO (30d) is the declared lifetime, and it is refreshed on use."""
        assert sidecars.lifetime_for(working_set.delivery_path(tmp_repo).name) == sidecars.COLD_REPO
        assert sidecars.lifetime_for(
            working_set.path(tmp_repo, "s1").name) == sidecars.SESSION


class TestDeliveryTallyIntegrity:
    """The three reproductions from review of 8eddeb2, each pinned as a regression."""

    def test_a_corrupt_row_cannot_escape_through_eviction(self, tmp_repo):
        """Review repro: `KeyError: 'last'`. The old reader accepted a positive count with no
        `last`, and eviction sorted on `last` outside the fail-soft boundary - so the poison
        row only detonated once the tally exceeded the cap, which the original test never did.
        """
        store.ensure_store_dir()
        poison = ('{"v":2,"rows":{"personal:rotten":{"renders":9,"first":1.0}}}')
        working_set.delivery_path(tmp_repo).write_text(poison, encoding="utf-8")

        over = store.MAX_ENTRIES + 5
        assert working_set.record_delivery_tally(
            tmp_repo, "s-evict",
            [{"scope": "personal", "id": f"d{i}", "fingerprint": "f"} for i in range(over)])

        rows = working_set.read_delivery_tally(tmp_repo)
        assert len(rows) == store.MAX_ENTRIES
        assert "personal:rotten" not in rows

    def test_one_session_rendering_twice_counts_once(self, tmp_repo):
        """Review repro: `same_session_count 2`. Compaction re-renders under the same session,
        and a failed ledger write lets a later prompt retry it."""
        for fingerprint in ("fp-before-edit", "fp-after-edit"):
            working_set.record_delivery_tally(
                tmp_repo, "one-session",
                [{"scope": "personal", "id": "dec-a", "fingerprint": fingerprint}])

        assert working_set.read_delivery_tally(tmp_repo)["personal:dec-a"]["renders"] == 1

    def test_a_different_session_does_count_again(self, tmp_repo):
        for session in ("s1", "s2"):
            working_set.record_delivery_tally(
                tmp_repo, session,
                [{"scope": "personal", "id": "dec-a", "fingerprint": "fp"}])

        assert working_set.read_delivery_tally(tmp_repo)["personal:dec-a"]["renders"] == 2

    def test_compaction_replay_does_not_inflate_the_count(self, tmp_repo):
        """End to end: _rehydrate_working_set clears credit and re-renders, which is the
        production path that produced the inflated figure."""
        ok, entry_id = store.update_decision(
            tmp_repo,
            "Key the store on the main worktree path, not the linked worktree path, because "
            "rev-parse returns the worktree path and would split one repo across two stores",
            "seed", "architecture", created_by="human")
        assert ok
        store.ensure_retrieval_index(tmp_repo)
        store.get_context_for_prompt(tmp_repo, "why key the store on the main worktree?", "sess-c")

        store._rehydrate_working_set(tmp_repo, "sess-c")

        assert working_set.read_delivery_tally(tmp_repo)[f"personal:{entry_id}"]["renders"] == 1

    def test_concurrent_writers_do_not_lose_each_others_rows(self, tmp_repo):
        """Review repro: only `['personal:decision-1']` survived. Atomic replacement stops a
        torn file, not a lost update - the read-modify-write needs the lock.

        Asserts the guarantee the design actually makes, which is NOT exact counts. The lock
        is non-blocking with a bounded retry (see TestTallyNeverBlocksTheHook), so a writer
        that loses the race three times drops its increment rather than stalling a prompt.
        What must hold is that no writer's ROW is lost and no count is corrupted by an
        interleaved read-modify-write - asserting `renders == writes` would pin a guarantee
        the bounded lock deliberately trades away, which is how this test failed on CI after
        the blocking acquire was replaced.
        """
        import threading

        writes = 20
        start = threading.Barrier(2)
        landed = {}

        def deliver(decision_id: str, session_prefix: str) -> None:
            start.wait()
            ok = 0
            for n in range(writes):
                if working_set.record_delivery_tally(
                        tmp_repo, f"{session_prefix}-{n}",
                        [{"scope": "personal", "id": decision_id, "fingerprint": "f"}]):
                    ok += 1
            landed[decision_id] = ok

        threads = [threading.Thread(target=deliver, args=(f"decision-{i}", f"s{i}"))
                   for i in (1, 2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        rows = working_set.read_delivery_tally(tmp_repo)
        assert set(rows) == {"personal:decision-1", "personal:decision-2"}, rows
        for decision_id, accepted in landed.items():
            row = rows[f"personal:{decision_id}"]
            # Every accepted write is counted exactly once: no increment is silently merged
            # away by the other writer, which is the corruption the lock exists to prevent.
            assert row["renders"] == accepted, (decision_id, row, accepted)
            assert 1 <= row["renders"] <= writes
            assert row["first"] <= row["last"]

class TestTallyNeverBlocksTheHook:
    """Review of 5794acb: a blocking flock has no timeout, so one stalled holder would hang
    the prompt path for a counter nothing reads synchronously."""

    def test_contention_drops_the_increment_instead_of_waiting(self, tmp_repo):
        import threading
        import time as _time

        held = threading.Event()
        release = threading.Event()

        def hold_the_lock() -> None:
            with store.store_lock(working_set._delivery_lock_slug(tmp_repo)):
                held.set()
                release.wait(timeout=30)

        holder = threading.Thread(target=hold_the_lock)
        holder.start()
        assert held.wait(timeout=5)
        try:
            start = _time.perf_counter()
            result = working_set.record_delivery_tally(
                tmp_repo, "blocked", [{"scope": "personal", "id": "d", "fingerprint": "f"}])
            elapsed = _time.perf_counter() - start
        finally:
            release.set()
            holder.join(timeout=5)

        assert result is False, "contention must be reported, not waited out"
        budget = working_set._TALLY_LOCK_TRIES * working_set._TALLY_LOCK_BACKOFF
        assert elapsed < budget + 1.0, f"waited {elapsed:.2f}s; the hook must never block"
        assert working_set.read_delivery_tally(tmp_repo) == {}

    def test_the_lock_is_released_so_the_next_delivery_succeeds(self, tmp_repo):
        """The drop is transient, not sticky - a skipped increment must not poison the file."""
        assert working_set.record_delivery_tally(
            tmp_repo, "s1", [{"scope": "personal", "id": "d", "fingerprint": "f"}])
        assert working_set.read_delivery_tally(tmp_repo)["personal:d"]["renders"] == 1
