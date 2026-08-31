"""Tests for contexer/sidecars.py: the lifetime declaration, and the sweep that reads it.

These pin the SAFETY DIRECTION rather than the file list. The declaration is meant to change
as kinds are added; what must not change is that an undeclared or durable name is never
deleted, because the two errors are not symmetric. Failing to sweep a cache costs disk;
sweeping a durable file costs a queued share, a credential, an explicit guard dismissal, or a
decision store.
"""
import os
import time

import pytest

from contexer import sidecars, store


class TestClassification:
    @pytest.mark.parametrize("name", [
        "Users_me_proj-abc123.json",      # a repo store
        "_global.json",                   # the global store
        "Users_me_proj.deleted.json",     # tombstones
        "Users_me_proj.json.migrated",    # a folded worktree stray
        "repo-1.lock",
        ".current_repo", ".outbox.json", ".reconcile-outbox.json", ".shared.jsonl",
        ".team_auth.json",                # credentials
        ".guard_dismissed_x.json",        # explicit human dismissals
        ".pending_review_x",              # only a NEW pending decision re-arms it
        ".outbox.lock", ".shared.lock", ".team_auth.lock",   # dotted lock slugs
        ".reconcile_x.lock",              # evidence consumer lock
        ".team_share_policy_repo-1.json",
        ".team-proposal-outbox.json", ".team-proposal-receipts.jsonl",
        ".team-proposal-attention.json",
        ".team_share_policy_repo-1.lock", ".team-proposal-outbox.lock",
        ".team-proposal-drainer.lock", ".team-proposal-receipts.lock",
        ".team-proposal-attention.lock",
        "ui.json", "ui.log",             # console daemon owns these
    ])
    def test_durable_names_are_never_sweepable(self, name):
        assert sidecars.lifetime_for(name) is None

    @pytest.mark.parametrize("name", [
        ".ws_x_y.json", ".retrieval_x.jsonl", ".bootstrap_offered_x", ".edited_x.json",
        ".resume_mining", ".gemini_first_prompt_x",
        ".gemini_pending_capture", ".gemini_pending_reload",
        ".team_pending_x.json", ".reconcile_x.jsonl",
    ])
    def test_session_bookkeeping_expires(self, name):
        assert sidecars.lifetime_for(name) == sidecars.SESSION

    @pytest.mark.parametrize("name", [
        ".team_x.json", ".team_seen_x_claude.json", ".insight_x", ".anchor_verify_x",
        ".miner_verify_x", ".memory_synced_x", ".guard_advised_x.json",
        ".retrieval_index_x.json",        # rebuildable; the card called it so
    ])
    def test_cold_repo_caches_expire_later(self, name):
        assert sidecars.lifetime_for(name) == sidecars.COLD_REPO

    @pytest.mark.parametrize("name", [".something_brand_new", "config.toml", "ui.log", "x"])
    def test_an_undeclared_name_is_never_swept(self, name):
        # Deletion is opt-in. A kind nobody declared is a kind nobody reasoned about.
        assert sidecars.lifetime_for(name) is None

    def test_a_leading_dot_is_not_matched_by_the_store_globs(self):
        # fnmatch does NOT exempt a leading dot the way shell globbing does, so a bare
        # `*.json` for the decision stores also matched `.ws_*.json` / `.team_*.json` and
        # silently made every sidecar durable. This is that regression, pinned.
        assert sidecars.lifetime_for(".ws_a_b.json") == sidecars.SESSION
        assert sidecars.lifetime_for(".team_a.json") == sidecars.COLD_REPO
        assert sidecars.lifetime_for("plain_store.json") is None

    def test_credentials_win_over_the_team_cache_glob(self):
        # `.team_auth.json` also matches the sweepable `.team_*.json`; the durable listing is
        # consulted first, so ordering inside KINDS can never decide this.
        assert sidecars.lifetime_for(".team_auth.json") is None
        assert sidecars.lifetime_for(".team_x.json") == sidecars.COLD_REPO

    def test_every_declared_kind_states_a_reason(self):
        # A kind whose reason cannot be written down probably has the wrong lifetime.
        for kind in sidecars.KINDS:
            assert kind.why.strip(), kind.name

    def test_every_template_classifies_back_to_its_own_kind(self):
        """The round trip. This is what makes name-versus-glob drift unrepresentable.

        Both stage-1 bugs were exactly that drift: a bare `*.json` glob for the decision
        stores also swallowed every dotted sidecar, and the declaration said
        `.reconcile_outbox.json` while share.py writes `.reconcile-outbox.json`. Deriving the
        glob from the template fixes the first; this test catches the second, because a
        template that does not match its own kind's glob cannot survive it.
        """
        for kind in sidecars.KINDS:
            rendered = kind.template.format(slug="Users_me_proj", session="abc123",
                                            consumer="claude")
            assert sidecars.lifetime_for(rendered) == kind.lifetime, (kind.name, rendered)

    def test_a_template_field_is_required_not_silently_blank(self):
        with pytest.raises(KeyError):
            sidecars.filename("insight")                      # missing slug
        with pytest.raises(KeyError):
            sidecars.filename("no_such_kind", slug="x")

    # Every kind, with the arguments needed to render it and the builder that produces it.
    # `None` for the builder means "no function in the package builds this name", which must
    # be a DELIBERATE, listed exception rather than an omission.
    PRODUCERS = {
        "store":            ("store._store_path", {"slug": None}),
        "deleted":          ("store._deleted_path", {"slug": None}),
        "migrated":         (None, {"slug": None}),          # built by migrate_worktree_strays inline
        "lock":             (None, {"slug": None}),          # store_lock builds it from a slug arg
        "dotted_lock":      (None, {"slug": None}),          # same, for the three dotted slugs
        "repo_pointer":     (None, {}),   # anchor_repo writes it; no path getter exists
        "pending_review":   ("store._pending_review_flag", {"slug": None}),
        "outbox":           ("share._outbox_path", {}),
        "reconcile_outbox": ("share._reconcile_outbox_path", {}),
        "shared_markers":   ("share._shared_path", {}),
        "team_creds":       ("auth._creds_path", {}),
        "guard_dismissed":  ("guard_engine._guard_dismissed_path", {"slug": None}),
        "reconcile_lock":   (None, {"slug": None}),       # reconcile builds through filename
        "share_policy":     ("share_policy.policy_path", {"slug": None}),
        "proposal_outbox":  ("share_policy.proposal_outbox_path", {}),
        "proposal_receipts": ("share_policy.proposal_receipts_path", {}),
        "proposal_attention": ("share_policy.proposal_attention_path", {}),
        "share_policy_lock": ("share_policy.policy_lock_path", {"slug": None}),
        "proposal_outbox_lock": ("share_policy.proposal_outbox_lock_path", {}),
        "proposal_drainer_lock": ("share_policy.proposal_drainer_lock_path", {}),
        "proposal_receipts_lock": ("share_policy.proposal_receipts_lock_path", {}),
        "proposal_attention_lock": ("share_policy.proposal_attention_lock_path", {}),
        "console_state":    (None, {}),                      # ui/daemon.py keeps its own literal
        "console_log":      (None, {}),                      # (import allowlist; see sidecars.py)
        "working_set":      (None, {"slug": None, "session": "abc"}),   # _ws_path hashes the id
        "retrieval_log":    (None, {"slug": None}),          # built inline in two places
        "reconcile_log":    (None, {"slug": None}),          # reconcile builds through filename
        "bootstrap_offered": ("store._offer_flag", {"slug": None}),
        "edited_files":     ("store._edited_files_path", {"slug": None}),
        "resume_mining":    (None, {}),                      # built inline at two session-start sites
        "pending_capture":  (None, {}),                      # adapters/claude.py touches it
        "gemini_capture":   (None, {}),                      # adapters/gemini.py marker helper
        "gemini_reload":    (None, {}),
        "gemini_first":     (None, {"slug": None}),   # gemini._session_marker hashes the key
        "team_pending":     (None, {"slug": None}),          # legacy, dropped on first poll
        "team_cache":       ("team_context._cache_path", {"slug": None}),
        "team_seen":        ("team_context._seen_path", {"slug": None, "consumer": "claude"}),
        "memory_synced":    (None, {"slug": None}),          # adapters/claude.py sync_memory
        "insight":          ("store._insight_cache_path", {"slug": None}),
        "anchor_verify":    ("anchors._anchor_verify_stamp_path", {"slug": None}),
        "miner_verify":     ("store._miner_verify_stamp_path", {"slug": None}),
        "guard_advised":    ("guard_engine._guard_advised_path", {"slug": None}),
        "retrieval_index":  ("store._index_path", {"slug": None}),
    }

    def test_every_declared_kind_is_listed_here(self):
        """Direction one: the declaration may not grow a kind this test does not know about.

        Without this, a new row is unverified and its glob may not match the name the code
        writes. That is not hypothetical: `.reconcile-outbox.json` was declared with an
        underscore, and the dotted lock names were not declared at all. Both were invisible,
        because an unrecognised name is durable and durable was the right answer anyway.
        """
        declared = {k.name for k in sidecars.KINDS}
        assert declared == set(self.PRODUCERS), {
            "declared but unlisted": sorted(declared - set(self.PRODUCERS)),
            "listed but not declared": sorted(set(self.PRODUCERS) - declared),
        }

    def test_the_declared_name_matches_what_the_code_actually_writes(self, tmp_repo):
        """Direction two: for every kind with a builder, the builder's name equals the
        template's. This is what catches a declaration that has drifted from the code."""
        from contexer import anchors, auth, guard_engine, share, share_policy, team_context
        mods = {"store": store, "share": share, "auth": auth, "anchors": anchors,
                "guard_engine": guard_engine, "team_context": team_context,
                "share_policy": share_policy}
        slug = store.repo_slug(tmp_repo)
        checked = 0
        for kind, (producer, fields) in self.PRODUCERS.items():
            if producer is None:
                continue
            mod_name, fn_name = producer.split(".")
            fn = getattr(mods[mod_name], fn_name)
            extra = {k: v for k, v in fields.items() if k != "slug" and v is not None}
            built = fn(tmp_repo, *extra.values()) if "slug" in fields else fn()
            rendered = sidecars.filename(
                kind, **{k: (slug if v is None else v) for k, v in fields.items()})
            assert built.name == rendered, (kind, built.name, rendered)
            checked += 1
        assert checked >= 15, checked

    def test_no_module_spells_a_declared_sidecar_name_by_hand(self):
        """Direction three: no literal anywhere may duplicate a declared name.

        The completeness claim for this change was wrong twice, both times because the search
        was wrong rather than the code: a grep for `STORE_DIR / "` missed `auth.py`'s
        single-quoted build, and missed names held in a module constant. This scans for the
        rendered names themselves, so the spelling of the surrounding code cannot hide one.
        """
        import pathlib as _p
        root = _p.Path(sidecars.__file__).parent
        allowed = {
            # ui/daemon.py cannot import the declaration: its module-level imports are an
            # enforced allowlist, because importing contexer.store on the SessionStart path
            # costs a measured 134ms against a ~0.3ms budget.
            ("daemon.py", "ui.json"), ("daemon.py", "ui.log"),
            # Installed shell hooks embed the path for a shell to expand, not for Python.
            ("claude.py", ".current_repo"), ("claude.py", ".pending_capture"),
            ("codex.py", ".current_repo"), ("codex.py", ".pending_capture"),
        }
        literals = {k.template for k in sidecars.KINDS if "{" not in k.template}
        offenders = []
        for path in sorted(root.rglob("*.py")):
            if path.name == "sidecars.py":
                continue
            text = path.read_text(encoding="utf-8")
            for lit in literals:
                for quote in ('"', "'"):
                    if f"{quote}{lit}{quote}" in text and (path.name, lit) not in allowed:
                        offenders.append(f"{path.name}: {lit}")
        assert offenders == [], sorted(set(offenders))

    def test_order_between_two_sweepable_kinds_is_load_bearing(self):
        """`.team_pending_x.json` matches both `.team_pending_*.json` (SESSION) and
        `.team_*.json` (COLD_REPO). SESSION wins only because it is declared first, so this
        pins the ORDER rather than just the value: reordering the rows fails here instead of
        quietly making the file live four times longer."""
        names = [k.name for k in sidecars.KINDS]
        assert names.index("team_pending") < names.index("team_cache")
        assert sidecars.lifetime_for(".team_pending_x.json") == sidecars.SESSION


class TestSweep:
    def _age(self, path, seconds):
        old = time.time() - seconds
        import os
        os.utime(path, (old, old))

    def test_sweeps_cold_and_spares_durable(self, tmp_repo):
        d = store.STORE_DIR
        d.mkdir(parents=True, exist_ok=True)
        durable = [".team_auth.json", ".outbox.json", ".guard_dismissed_x.json",
                   "_global.json", "somerepo-1234.json"]
        cold = [".ws_a_b.json", ".insight_x", ".team_x.json", ".memory_synced_x"]
        for n in durable + cold:
            (d / n).write_text("{}", encoding="utf-8")
            self._age(d / n, 400 * 24 * 3600)      # a year old: cold by any lifetime

        store._gc_stale_session_files()

        for n in durable:
            assert (d / n).exists(), f"swept a durable file: {n}"
        for n in cold:
            assert not (d / n).exists(), f"left a cold file: {n}"

    def test_spares_a_cold_kind_that_is_still_fresh(self, tmp_repo):
        d = store.STORE_DIR
        d.mkdir(parents=True, exist_ok=True)
        fresh = d / ".insight_x"
        fresh.write_text("{}", encoding="utf-8")
        store._gc_stale_session_files()
        assert fresh.exists()

    def test_an_undeclared_file_survives_however_old(self, tmp_repo):
        d = store.STORE_DIR
        d.mkdir(parents=True, exist_ok=True)
        mystery = d / ".some_future_kind_nobody_declared"
        mystery.write_text("x", encoding="utf-8")
        self._age(mystery, 400 * 24 * 3600)
        store._gc_stale_session_files()
        assert mystery.exists()

    def test_one_unstattable_entry_does_not_stop_the_sweep(self, tmp_repo, monkeypatch):
        """The PER-ENTRY guard, which the previous version of this test did not reach: it
        patched the whole directory listing, so it only proved the outer guard existed."""
        d = store.STORE_DIR
        d.mkdir(parents=True, exist_ok=True)
        for n in (".insight_bad", ".insight_good"):
            (d / n).write_text("{}", encoding="utf-8")
            self._age(d / n, 400 * 24 * 3600)
        real_stat = store.Path.stat

        def flaky(self, *a, **k):
            if self.name == ".insight_bad":
                raise OSError("boom")
            return real_stat(self, *a, **k)

        monkeypatch.setattr(store.Path, "stat", flaky)
        store._gc_stale_session_files()
        monkeypatch.undo()      # Path.exists() itself calls stat, so restore before asserting
        left = set(os.listdir(d))
        assert ".insight_bad" in left          # skipped, not crashed on
        assert ".insight_good" not in left     # and the sweep carried on past it

    def test_a_non_oserror_cannot_escape_into_session_start(self, tmp_repo, monkeypatch):
        # The guard is deliberately wider than OSError: lifetime_for is not I/O, so an
        # OSError-only except would let anything else reach the SessionStart hook, which must
        # always render context.
        d = store.STORE_DIR
        d.mkdir(parents=True, exist_ok=True)
        (d / ".insight_x").write_text("{}", encoding="utf-8")
        monkeypatch.setattr(sidecars, "lifetime_for",
                            lambda name: (_ for _ in ()).throw(ValueError("boom")))
        store._gc_stale_session_files()   # must not raise
