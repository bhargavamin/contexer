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
        assert rows["personal:dec-a"]["n"] == 1

    def test_tally_outlives_the_session_ledger(self, tmp_repo):
        """The whole point: the session sidecar is SESSION-lived, this is not."""
        working_set.record_deliveries(
            tmp_repo, "sess-1", [{"scope": "personal", "id": "dec-a", "fingerprint": "fp1"}])
        working_set.path(tmp_repo, "sess-1").unlink()     # session ends

        assert working_set.records(tmp_repo, "sess-1") == []
        assert working_set.read_delivery_tally(tmp_repo)["personal:dec-a"]["n"] == 1

    def test_separate_sessions_accumulate(self, tmp_repo):
        for session in ("s1", "s2", "s3"):
            working_set.record_deliveries(
                tmp_repo, session,
                [{"scope": "personal", "id": "dec-a", "fingerprint": "fp1"}])

        row = working_set.read_delivery_tally(tmp_repo)["personal:dec-a"]
        assert row["n"] == 3
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
        assert rows["personal:hot"]["n"] == 400

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
            '{"v":1,"rows":{"personal:ok":{"n":2},"bad":{"n":"x"},"zero":{"n":0}}}',
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


class TestDeliveryTallyReachesTheRealRouter:
    def test_a_strong_injection_lands_in_the_durable_tally(self, tmp_repo):
        """End to end through get_context_for_prompt, not just the ledger API - the tally is
        worthless if the production delivery path does not reach it."""
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
        assert rows[f"personal:{entry_id}"]["n"] == 1
