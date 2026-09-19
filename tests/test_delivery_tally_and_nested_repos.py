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

class TestDeliveryTallyReachesTheRealRouter:
    """Restored after review of 3b09c3c: a span-edit while rewriting the concurrency test
    silently deleted this class, and the suite went 5710 -> 5709 without anyone noticing.

    This must assert the tally DIRECTLY after get_context_for_prompt. The compaction test
    below does fail today if the router stops recording, but only indirectly - it goes
    through the session ledger, so it would stop catching a router regression the moment the
    ledger write and the tally write were separated. The production integration deserves its
    own assertion rather than a side effect of someone else's.
    """

    def test_a_strong_injection_lands_in_the_durable_tally(self, tmp_repo):
        ok, entry_id = store.update_decision(
            tmp_repo,
            "Use Postgres with pgbouncer for the decision store connection pooling because "
            "per-request connections exhausted the server under load",
            "seed-session", "architecture", created_by="human")
        assert ok
        store.ensure_retrieval_index(tmp_repo)

        store.get_context_for_prompt(
            tmp_repo, "why did we choose postgres with pgbouncer for pooling?", "sess-live")

        rows = working_set.read_delivery_tally(tmp_repo)
        assert f"personal:{entry_id}" in rows, rows
        assert rows[f"personal:{entry_id}"]["renders"] == 1


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


class TestDroppedDeliveriesAreNotSilent:
    """Human review of 8c78ab9 (finding 1): exhausting the lock-retry budget on a decision's
    FIRST delivery is indistinguishable from 'never delivered' unless the drop is recorded
    somewhere - which is exactly the case the two-week measurement exists to detect."""

    def test_a_dropped_first_delivery_is_recorded_as_a_gap(self, tmp_repo):
        import threading

        held = threading.Event()
        release = threading.Event()

        def hold_the_lock():
            with store.store_lock(working_set._delivery_lock_slug(tmp_repo)):
                held.set()
                release.wait(timeout=5)

        holder = threading.Thread(target=hold_the_lock)
        holder.start()
        assert held.wait(timeout=5)
        try:
            result = working_set.record_delivery_tally(
                tmp_repo, "first-ever-session",
                [{"scope": "personal", "id": "never-seen-before", "fingerprint": "f"}])
        finally:
            release.set()
            holder.join(timeout=5)

        assert result is False
        assert working_set.read_delivery_tally(tmp_repo) == {}, (
            "the row must not appear silently - if it did, the gap counter would be lying")
        assert working_set.delivery_gap_count(tmp_repo) == 1

    def test_gap_count_accumulates_across_drops_and_repos_stay_separate(self, tmp_repo, tmp_path):
        working_set.record_delivery_gap(tmp_repo, 3)
        working_set.record_delivery_gap(tmp_repo, 2)
        assert working_set.delivery_gap_count(tmp_repo) == 5

        other_repo = str(tmp_path / "other")
        assert working_set.delivery_gap_count(other_repo) == 0

    def test_zero_or_negative_gaps_are_not_recorded(self, tmp_repo):
        working_set.record_delivery_gap(tmp_repo, 0)
        working_set.record_delivery_gap(tmp_repo, -3)
        assert working_set.delivery_gap_count(tmp_repo) == 0

    def test_missing_gap_file_reads_as_zero_not_an_error(self, tmp_repo):
        assert working_set.delivery_gap_count(tmp_repo) == 0

    def test_a_successful_delivery_records_no_gap(self, tmp_repo):
        working_set.record_delivery_tally(
            tmp_repo, "s1", [{"scope": "personal", "id": "d", "fingerprint": "f"}])
        assert working_set.delivery_gap_count(tmp_repo) == 0


class TestLongSessionIdsStillSuppress:
    """Human review of 8c78ab9 (finding 3): the writer stored `session_id` verbatim while the
    reader normalized anything over FIELD_MAX back to "" - so a long id compared unequal to
    itself on the very next call, and one session rendering twice was counted as two."""

    def test_a_257_char_session_id_still_suppresses_a_repeat(self, tmp_repo):
        long_session = "s" * 257
        for _ in range(2):
            working_set.record_delivery_tally(
                tmp_repo, long_session,
                [{"scope": "personal", "id": "d", "fingerprint": "f"}])

        assert working_set.read_delivery_tally(tmp_repo)["personal:d"]["renders"] == 1

    def test_the_stored_session_field_never_exceeds_field_max(self, tmp_repo):
        working_set.record_delivery_tally(
            tmp_repo, "s" * 5000, [{"scope": "personal", "id": "d", "fingerprint": "f"}])

        row = working_set.read_delivery_tally(tmp_repo)["personal:d"]
        assert len(row["session"]) <= working_set.FIELD_MAX


class TestNonFiniteTimestampsAreRejected:
    """Human review of 8c78ab9 (finding 4): `_valid_stamp` rejected NaN but accepted
    +/-Infinity, which JSON also accepts as a bare token. An infinite `last` sorts newest
    forever and would evict every genuinely recent row once the tally hit capacity."""

    def test_positive_infinity_is_rejected(self, tmp_repo):
        store.ensure_store_dir()
        working_set.delivery_path(tmp_repo).write_text(
            '{"v":2,"rows":{"personal:x":{"renders":1,"first":1.0,"last":Infinity,'
            '"session":""}}}', encoding="utf-8")
        assert working_set.read_delivery_tally(tmp_repo) == {}

    def test_negative_infinity_is_rejected(self, tmp_repo):
        store.ensure_store_dir()
        working_set.delivery_path(tmp_repo).write_text(
            '{"v":2,"rows":{"personal:x":{"renders":1,"first":-Infinity,"last":2.0,'
            '"session":""}}}', encoding="utf-8")
        assert working_set.read_delivery_tally(tmp_repo) == {}

    def test_negative_timestamp_is_rejected(self, tmp_repo):
        store.ensure_store_dir()
        working_set.delivery_path(tmp_repo).write_text(
            '{"v":2,"rows":{"personal:x":{"renders":1,"first":-5.0,"last":2.0,'
            '"session":""}}}', encoding="utf-8")
        assert working_set.read_delivery_tally(tmp_repo) == {}

    def test_ordinary_timestamps_still_pass(self, tmp_repo):
        working_set.record_delivery_tally(
            tmp_repo, "s1", [{"scope": "personal", "id": "d", "fingerprint": "f"}])
        assert "personal:d" in working_set.read_delivery_tally(tmp_repo)


class TestExistingNestedCitationsAreWithheld:
    """Human review of 8c78ab9 (finding 2): `_paths` stops a FRESH scan from descending into
    a nested checkout, but an entry already captured from one - before the exclusion existed
    - kept citing that path. `_refresh_entries` falls back to reading a citation directly off
    disk when the file is absent from `scan["files"]`, which every excluded path now is; if
    the file is unchanged, that fallback reported "current" forever. Prevention alone leaves
    the two-week measurement's STARTING denominator corrupt with the three duplicate pairs
    the PR body names - this closes the gap for entries that already exist."""

    RULE = "# Contributing\n\n- **No premature abstractions.**\n"

    def _seed_nested_citation(self, tmp_repo, nested_rel: str) -> dict:
        entry = store.build_inferred_entry(
            "No premature abstractions.", "s0", "convention", "suggested")
        entry["bootstrap"] = {
            "key": "doc:legacy-nested", "kind": "inferred", "assessment": "supported",
            "scope": "project", "sample": "repo",
            "sources": [{"file": nested_rel, "sha256": bootstrap._digest(self.RULE),
                        "line": 3, "end_line": 3, "quote": "No premature abstractions."}],
        }
        data = store.load(tmp_repo)
        data["entries"].append(entry)
        store.save(tmp_repo, data)
        return entry

    def test_an_unchanged_nested_citation_is_withheld_on_refresh(self, tmp_repo):
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        Path(tmp_repo, "CONTRIBUTING.md").write_text(self.RULE)
        wt = Path(tmp_repo, ".claude", "worktrees", "agent-x")
        wt.mkdir(parents=True)
        (wt / "CONTRIBUTING.md").write_text(self.RULE)
        (wt / ".git").write_text("gitdir: /elsewhere/.git/worktrees/agent\n")

        nested_rel = ".claude/worktrees/agent-x/CONTRIBUTING.md"
        scan1 = bootstrap.run(tmp_repo, "test")
        assert nested_rel not in scan1["files"], "sanity: a FRESH scan must exclude it"

        entry = self._seed_nested_citation(tmp_repo, nested_rel)
        scan2 = bootstrap.run(tmp_repo, "test2")
        bootstrap._refresh_entries(store.load(tmp_repo)["entries"], scan2)
        refreshed = next(e for e in store.load(tmp_repo)["entries"] if e["id"] == entry["id"])

        assert refreshed.get("bootstrap_withheld"), "must not stay 'current' forever"
        assert store.entry_status(refreshed) == "ignored", (
            "withheld AI guidance must exit every active filter, not just carry a flag")

    def test_an_ordinary_citation_is_unaffected(self, tmp_repo):
        """The fix must not withhold something that was never nested."""
        Path(tmp_repo).mkdir(parents=True, exist_ok=True)
        Path(tmp_repo, "CONTRIBUTING.md").write_text(self.RULE)
        entry = self._seed_nested_citation(tmp_repo, "CONTRIBUTING.md")

        scan = bootstrap.run(tmp_repo, "test")
        bootstrap._refresh_entries(store.load(tmp_repo)["entries"], scan)
        refreshed = next(e for e in store.load(tmp_repo)["entries"] if e["id"] == entry["id"])

        assert not refreshed.get("bootstrap_withheld")
        assert store.entry_status(refreshed) == "suggested"

    def test_citation_predicate_only_checks_ancestor_directories(self, tmp_repo):
        root = Path(tmp_repo)
        plain = root / "docs" / "guide.md"
        plain.parent.mkdir(parents=True, exist_ok=True)
        plain.write_text("# Guide\n")
        assert bootstrap._citation_under_nested_checkout(root, plain) is False

        nested = root / ".claude" / "worktrees" / "x" / "README.md"
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_text("# X\n")
        (nested.parent / ".git").write_text("gitdir: elsewhere\n")
        assert bootstrap._citation_under_nested_checkout(root, nested) is True

    def test_citation_outside_root_entirely_is_not_flagged(self, tmp_repo, tmp_path):
        outside = tmp_path / "elsewhere" / "file.md"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_text("# Elsewhere\n")
        assert bootstrap._citation_under_nested_checkout(Path(tmp_repo), outside) is False
