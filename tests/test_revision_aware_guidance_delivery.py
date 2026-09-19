"""Revision- and effective-view-aware prompt guidance delivery."""

import copy
import hashlib
import json
from pathlib import Path

import pytest

from contexer import conflicts, review, revisions, store, working_set


SESSION = "revision-aware-guidance"


def _approved(repo: str, content: str, *, title: str = "", source_files=None) -> str:
    stored, did = store.update_decision(
        repo, content, SESSION, "architecture", created_by="human", title=title,
        source_files=source_files,
    )
    assert stored and did
    return did


def _approved_direct(repo: str, content: str, *, title: str = "") -> str:
    """Seed distinct fixtures without exercising capture's novelty consolidation."""
    data = store.load(repo)
    entry = store._new_decision_entry(
        content, SESSION, "architecture", created_by="human", title=title)
    data["entries"].append(entry)
    store.save(repo, data)
    return entry["id"]


def _revise(repo: str, did: str, content: str, *, title: str = "") -> str:
    data = store.load(repo)
    entry = store.entry_by_id(data["entries"], did)
    revision = revisions.append_revision(
        entry, content, source="human", approved_at="2026-09-18T12:00:00+00:00",
        title=title,
    )
    entry["status"] = "approved"
    entry["approved_by"] = "human"
    store.save(repo, data)
    return revision["revision_id"]


def _propose(repo: str, did: str, content: str, *, title: str = "") -> None:
    data = store.load(repo)
    entry = store.entry_by_id(data["entries"], did)
    entry["proposed_revision"] = review.build_proposal(
        entry, content, "architecture", SESSION,
        "2026-09-18T10:00:00+00:00", source="human", title=title,
    )
    store.save(repo, data)


def _prompt(repo: str, sid: str = "same-session") -> str:
    return store.get_context_for_prompt(
        repo, "Why use checkout reservation leases for inventory?", sid)


class TestGuidanceFingerprint:
    def test_semantic_identity_table(self, tmp_repo):
        did = _approved(
            tmp_repo, "Use checkout reservation leases for inventory consistency",
            title="Checkout reservation leases",
        )
        data = store.load(tmp_repo)
        entry = store.entry_by_id(data["entries"], did)
        original = store._guidance_fingerprint(entry, data)

        for field, value in [
            ("occurrence_count", 99),
            ("timestamp", "2099-01-01T00:00:00+00:00"),
            ("updated_at", "2099-01-02T00:00:00+00:00"),
            ("session_ids", ["other"]),
        ]:
            changed = copy.deepcopy(entry)
            changed[field] = value
            assert store._guidance_fingerprint(changed, data) == original

        for mutate in [
            lambda e: e.__setitem__("title", "Different checkout policy"),
            lambda e: e.__setitem__("status", "suggested"),
            lambda e: e.__setitem__("approved_by", "different-authority"),
            lambda e: e.__setitem__("subtype", "constraint"),
        ]:
            changed = copy.deepcopy(entry)
            mutate(changed)
            assert store._guidance_fingerprint(changed, data) != original

    def test_effective_conflict_view_changes_but_hidden_title_only_proposal_does_not(self,
                                                                                     tmp_repo):
        did = _approved(tmp_repo, "Use checkout leases for inventory consistency")
        data = store.load(tmp_repo)
        entry = store.entry_by_id(data["entries"], did)
        original = store._guidance_fingerprint(entry, data)

        hidden = copy.deepcopy(entry)
        hidden["proposed_revision"] = {
            "content": revisions.current_content(hidden), "title": "Hidden proposed title",
            "created_at": "2026-09-18T00:00:00+00:00", "source": "human",
        }
        assert store._guidance_fingerprint(hidden, data) == original

        visible = copy.deepcopy(entry)
        visible["proposed_revision"] = {
            "content": "Use 45-second checkout leases for inventory consistency",
            "title": "45-second checkout leases",
            "created_at": "2026-09-18T00:00:00+00:00", "source": "human",
        }
        assert store._guidance_fingerprint(visible, data) != original

    def test_proposal_edits_memo_choices_and_removal_change_effective_identity(self, tmp_repo):
        did = _approved(tmp_repo, "Use 30-second checkout leases for inventory consistency")
        data = store.load(tmp_repo)
        entry = store.entry_by_id(data["entries"], did)
        fingerprints = [store._guidance_fingerprint(entry, data)]
        entry["proposed_revision"] = review.build_proposal(
            entry, "Use 45-second checkout leases for inventory consistency",
            "architecture", SESSION, "2026-09-18T10:00:00+00:00", source="human")
        fingerprints.append(store._guidance_fingerprint(entry, data))
        entry["proposed_revision"]["content"] = \
            "Use 60-second checkout leases for inventory consistency"
        fingerprints.append(store._guidance_fingerprint(entry, data))
        pair = conflicts._conflict_pair_key(entry)
        entry["conflict_memo"] = {
            "pair": pair, "choice": "standing", "created_at": "2026-09-18T11:00:00+00:00",
        }
        fingerprints.append(store._guidance_fingerprint(entry, data))
        entry["conflict_memo"]["choice"] = "update"
        fingerprints.append(store._guidance_fingerprint(entry, data))
        entry.pop("proposed_revision")
        fingerprints.append(store._guidance_fingerprint(entry, data))
        assert all(left != right for left, right in zip(fingerprints, fingerprints[1:]))
        assert fingerprints[-1] == fingerprints[0]  # ledger keeps only the latest row

    def test_stable_bootstrap_caveat_participates_but_transient_check_failure_does_not(
        self, tmp_repo
    ):
        entry = store.build_inferred_entry(
            "Observed checkout queue topology", SESSION, "architecture", "suggested")
        data = {"repo_path": tmp_repo, "entries": [entry]}
        entry["bootstrap"] = {
            "kind": "observed", "assessment": "supported", "scope": "checkout queue",
            "sources": [],
        }
        supported = store._guidance_fingerprint(entry, data)
        entry["bootstrap_check_unavailable"] = "temporary filesystem denial"
        assert store._guidance_fingerprint(entry, data) == supported
        entry["bootstrap"]["assessment"] = "contradicted"
        entry["bootstrap"]["question"] = "Which checkout queue is intended?"
        assert store._guidance_fingerprint(entry, data) != supported

    def test_raw_legacy_migration_uuid_is_not_identity_and_never_serializes_provenance(
        self, tmp_repo
    ):
        raw = {
            "repo_path": tmp_repo,
            "entries": [{
                "id": "legacy-one", "type": "decision", "subtype": "architecture",
                "content": "Use checkout reservation leases for inventory consistency",
                "title": "Checkout reservation leases", "status": "approved",
                "created_by": "human", "timestamp": "2026-01-01T00:00:00+00:00",
                "revision": 1,
            }],
        }
        path = store._store_path(tmp_repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw), encoding="utf-8")

        first = store.load(tmp_repo)
        second = store.load(tmp_repo)
        assert first["entries"][0]["current_revision_id"] != second["entries"][0][
            "current_revision_id"]
        assert store._guidance_fingerprint(first["entries"][0], first) == \
            store._guidance_fingerprint(second["entries"][0], second)

        assert store.ensure_retrieval_index(tmp_repo)
        sid = "raw-legacy"
        prompt = "Why use checkout reservation leases for inventory?"
        assert "checkout reservation" in store.get_context_for_prompt(tmp_repo, prompt, sid).lower()
        assert store.get_context_for_prompt(tmp_repo, prompt, sid) == ""

        store.save(tmp_repo, first)
        serialized = json.loads(path.read_text(encoding="utf-8"))
        assert store._GUIDANCE_PROVENANCE_KEY not in serialized
        persisted = store.load(tmp_repo)
        assert store._guidance_fingerprint(persisted["entries"][0], persisted) == \
            store._read_retrieval_index(tmp_repo)["docs"]["legacy-one"]["guidance_fingerprint"]
        assert "Updated context for this decision" in \
            store.get_context_for_prompt(tmp_repo, prompt, sid)
        assert store.get_context_for_prompt(tmp_repo, prompt, sid) == ""

    def test_failed_legacy_save_does_not_promote_temporary_revision_identity(
        self, tmp_repo, monkeypatch
    ):
        raw = {"repo_path": tmp_repo, "entries": [{
            "id": "legacy-failed", "type": "decision", "subtype": "architecture",
            "content": "Use checkout leases", "status": "approved", "created_by": "human",
            "timestamp": "2026-01-01T00:00:00+00:00", "revision": 1,
        }]}
        path = store._store_path(tmp_repo)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw), encoding="utf-8")
        data = store.load(tmp_repo)
        entry = data["entries"][0]
        before = store._guidance_fingerprint(entry, data)

        monkeypatch.setattr(store, "atomic_write", lambda *_args: (_ for _ in ()).throw(
            OSError("denied")))
        with pytest.raises(OSError, match="denied"):
            store.save(tmp_repo, data)
        assert store._guidance_fingerprint(entry, data) == before
        assert store._revision_identity_is_persisted(data, entry) is False


class TestVersionAwarePromptDelivery:
    def test_a_then_b_same_session_then_b_suppressed(self, tmp_repo):
        did = _approved(
            tmp_repo, "Use 30-second checkout reservation leases for inventory",
            title="Checkout reservation leases",
        )
        first = _prompt(tmp_repo)
        assert "30-second" in first
        _revise(
            tmp_repo, did, "Use 45-second checkout reservation leases for inventory",
            title="Checkout reservation leases",
        )
        second = _prompt(tmp_repo)
        assert "Updated context for this decision" in second
        assert "45-second" in second
        assert _prompt(tmp_repo) == ""

    def test_a_to_b_to_a_is_a_new_delivery(self, tmp_repo):
        did = _approved(tmp_repo, "Use 30-second checkout reservation leases for inventory")
        assert "30-second" in _prompt(tmp_repo)
        _revise(tmp_repo, did, "Use 45-second checkout reservation leases for inventory")
        assert "45-second" in _prompt(tmp_repo)
        _revise(tmp_repo, did, "Use 30-second checkout reservation leases for inventory")
        replay = _prompt(tmp_repo)
        assert "Updated context for this decision" in replay and "30-second" in replay

    def test_file_anchor_replays_current_revision(self, tmp_repo):
        did = _approved(
            tmp_repo, "checkout/reservations.py uses 30-second inventory leases",
            source_files=["checkout/reservations.py"],
        )
        sid = "anchor-session"
        prompt = "fix checkout/reservations.py lease handling"
        assert "30-second" in store.get_context_for_prompt(tmp_repo, prompt, sid)
        _revise(tmp_repo, did, "checkout/reservations.py uses 45-second inventory leases")
        replay = store.get_context_for_prompt(tmp_repo, prompt, sid)
        assert "Updated context for this decision" in replay and "45-second" in replay

    def test_global_anchor_keeps_scope_when_local_store_has_same_id(self, tmp_repo):
        local = store._new_decision_entry(
            "Local queue uses red widgets", SESSION, "architecture", created_by="human",
            title="Local red widget rule",
        )
        global_entry = store._new_decision_entry(
            "Global checkout file uses 45-second inventory leases", SESSION, "constraint",
            created_by="human", title="Global checkout lease",
        )
        collision_id = "same-opaque-id"
        for entry in (local, global_entry):
            entry["id"] = collision_id
            entry["revisions"][0]["decision_id"] = collision_id
        global_entry["source_files"] = ["checkout/reservations.py"]
        store.save(tmp_repo, {"repo_path": tmp_repo, "entries": [local]})
        store.save_global({"repo_path": store.GLOBAL_SLUG, "entries": [global_entry]})

        sid = "scope-collision"
        output = store.get_context_for_prompt(
            tmp_repo, "fix checkout/reservations.py lease handling", sid)
        assert "Global checkout file uses 45-second inventory leases" in output
        assert "Local queue uses red widgets" not in output
        assert working_set.records(tmp_repo, sid) == [{
            "scope": "global", "id": collision_id,
            "fingerprint": store._guidance_fingerprint(global_entry),
        }]

    def test_proposal_edit_replays_rendered_conflict_view_then_suppresses(self, tmp_repo):
        did = _approved(
            tmp_repo, "Use 30-second checkout reservation leases for inventory",
            title="Checkout reservation leases",
        )
        sid = "proposal-edit"
        assert "30-second" in _prompt(tmp_repo, sid)

        _propose(
            tmp_repo, did, "Use 45-second checkout reservation leases for inventory",
            title="45-second checkout leases",
        )
        proposed = _prompt(tmp_repo, sid)
        assert "Updated context for this decision" in proposed
        assert "Unreviewed update" in proposed and "45-second" in proposed
        assert _prompt(tmp_repo, sid) == ""

        data = store.load(tmp_repo)
        entry = store.entry_by_id(data["entries"], did)
        entry["proposed_revision"]["content"] = \
            "Use 60-second checkout reservation leases for inventory"
        entry["proposed_revision"]["title"] = "60-second checkout leases"
        store.save(tmp_repo, data)

        edited = _prompt(tmp_repo, sid)
        assert "Updated context for this decision" in edited
        assert "Unreviewed update" in edited and "60-second" in edited
        assert _prompt(tmp_repo, sid) == ""

    def test_memo_choices_and_dismissal_replay_actual_authority_view(self, tmp_repo):
        did = _approved(
            tmp_repo, "Use 30-second checkout reservation leases for inventory",
            title="Checkout reservation leases",
        )
        sid = "proposal-choices"
        assert "30-second" in _prompt(tmp_repo, sid)
        _propose(
            tmp_repo, did, "Use 45-second checkout reservation leases for inventory",
            title="45-second checkout leases",
        )
        assert "Unreviewed update" in _prompt(tmp_repo, sid)

        ok, _ = conflicts.record_conflict_memo(tmp_repo, did[:8], "standing", sid)
        assert ok
        standing = _prompt(tmp_repo, sid)
        assert "Updated context for this decision" in standing
        assert "30-second" in standing and "declined with the developer" in standing
        assert "45-second" not in standing
        assert _prompt(tmp_repo, sid) == ""

        ok, _ = conflicts.record_conflict_memo(tmp_repo, did[:8], "update", sid)
        assert ok
        update = _prompt(tmp_repo, sid)
        assert "Updated context for this decision" in update
        assert "45-second" in update
        assert "pending formal review; not developer-approved" in update
        assert "Still the approved version" in update and "30-second" in update
        assert _prompt(tmp_repo, sid) == ""

        ok, _ = store.approve_decision(tmp_repo, did, "dismiss")
        assert ok
        dismissed = _prompt(tmp_repo, sid)
        assert "Updated context for this decision" in dismissed
        assert "30-second" in dismissed and "45-second" not in dismissed
        assert "Unreviewed update" not in dismissed
        assert _prompt(tmp_repo, sid) == ""

    def test_proposal_approval_replays_authoritative_revision_then_suppresses(self, tmp_repo):
        did = _approved(
            tmp_repo, "Use 30-second checkout reservation leases for inventory",
            title="Checkout reservation leases",
        )
        sid = "proposal-approval"
        assert "30-second" in _prompt(tmp_repo, sid)
        _propose(
            tmp_repo, did, "Use 45-second checkout reservation leases for inventory",
            title="45-second checkout leases",
        )
        assert "Unreviewed update" in _prompt(tmp_repo, sid)

        ok, _ = store.approve_decision(tmp_repo, did, "approve")
        assert ok
        approved = _prompt(tmp_repo, sid)
        assert "Updated context for this decision" in approved
        assert "45-second" in approved
        assert "Unreviewed update" not in approved
        assert "pending formal review" not in approved
        assert _prompt(tmp_repo, sid) == ""

    def test_pointer_and_cap_excluded_candidate_receive_no_credit(self, tmp_repo):
        _approved(tmp_repo, "We use PostgreSQL as the primary datastore")
        sid = "pointer-session"
        pointer = store.get_context_for_prompt(tmp_repo, "why the schema design here?", sid)
        assert pointer.startswith("[Contexer] Related stored decisions:")
        assert working_set.records(tmp_repo, sid) == []

        ids = [_approved_direct(
            tmp_repo, f"Caching option {i} controls checkout latency budget behavior",
            title=f"Checkout cache option {i}",
        ) for i in range(4)]
        cap_sid = "cap-session"
        store.get_context_for_prompt(
            tmp_repo, "why does caching checkout latency budget behavior matter?", cap_sid)
        credited = {row["id"] for row in working_set.records(tmp_repo, cap_sid)}
        assert len(credited) == store._STRONG_CAP
        assert any(did not in credited for did in ids)

    def test_selected_but_ignored_before_render_gets_no_credit(self, tmp_repo, monkeypatch):
        did = _approved(tmp_repo, "Use checkout reservation leases for inventory")
        original = store._render_prompt_decisions_with_records

        def disappear(repo_path, ids, **kwargs):
            data = store.load(repo_path)
            store.entry_by_id(data["entries"], did)["status"] = "ignored"
            store.atomic_write(store._store_path(repo_path), json.dumps({
                k: v for k, v in data.items() if k != store._GUIDANCE_PROVENANCE_KEY
            }))
            return original(repo_path, ids, **kwargs)

        monkeypatch.setattr(store, "_render_prompt_decisions_with_records", disappear)
        assert _prompt(tmp_repo, "render-race") == ""
        assert working_set.records(tmp_repo, "render-race") == []

    def test_store_update_between_render_and_record_does_not_credit_new_view(
        self, tmp_repo, monkeypatch
    ):
        did = _approved(tmp_repo, "Use 30-second checkout reservation leases for inventory")
        original = working_set.record_deliveries
        changed = False

        def update_then_record(repo_path, session_id, delivered):
            nonlocal changed
            if not changed:
                changed = True
                _revise(repo_path, did, "Use 45-second checkout reservation leases for inventory")
            return original(repo_path, session_id, delivered)

        monkeypatch.setattr(working_set, "record_deliveries", update_then_record)
        assert "30-second" in _prompt(tmp_repo, "snapshot-race")
        replay = _prompt(tmp_repo, "snapshot-race")
        assert "45-second" in replay


class TestWorkingSetV2:
    def test_legacy_and_future_rows_are_hints_without_suppression_credit(self, tmp_repo):
        did = _approved(tmp_repo, "Use checkout reservation leases for inventory")
        for sid, payload in [
            ("legacy", {"injected": [did], "ts": 0}),
            ("future", {"v": 999, "injected": [did], "records": [{
                "scope": "personal", "id": did, "fingerprint": "invented",
            }]}),
        ]:
            path = working_set.path(tmp_repo, sid)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload), encoding="utf-8")
            assert working_set.ids(tmp_repo, sid) == [did]
            assert working_set.records(tmp_repo, sid) == []
            assert "checkout reservation" in _prompt(tmp_repo, sid).lower()

    @pytest.mark.parametrize("payload", [
        "not-json",
        json.dumps([]),
        json.dumps({"v": 2, "injected": "wrong", "records": "wrong"}),
        json.dumps({"v": 2, "injected": [], "records": [{
            "scope": "alien", "id": "x", "fingerprint": "y",
        }]}),
    ])
    def test_malformed_rows_never_suppress(self, tmp_repo, payload):
        path = working_set.path(tmp_repo, "malformed")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(payload, encoding="utf-8")
        assert working_set.records(tmp_repo, "malformed") == []

    @pytest.mark.parametrize("scope", [[], {}])
    def test_unhashable_scope_fails_open_to_guidance_delivery(self, tmp_repo, scope):
        did = _approved(tmp_repo, "Use checkout reservation leases for inventory")
        data = store.load(tmp_repo)
        entry = store.entry_by_id(data["entries"], did)
        path = working_set.path(tmp_repo, "unhashable-scope")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({
            "v": 2,
            "injected": [did],
            "records": [{
                "scope": scope,
                "id": did,
                "fingerprint": store._guidance_fingerprint(entry, data),
            }],
        }), encoding="utf-8")

        assert working_set.records(tmp_repo, "unhashable-scope") == []
        assert "checkout reservation leases" in _prompt(
            tmp_repo, "unhashable-scope").lower()

    def test_empty_session_creates_no_file(self, tmp_repo):
        did = _approved(tmp_repo, "Use checkout reservation leases for inventory")
        working_set.record_deliveries(tmp_repo, "", [{
            "scope": "personal", "id": did, "fingerprint": "guidance-v1:x",
        }])
        assert not working_set.path(tmp_repo, "").exists()

    def test_scoped_mru_rows_are_count_and_byte_bounded(self, tmp_repo):
        sid = "bounded"
        deliveries = [{
            "scope": "personal", "id": f"decision-{i:04d}",
            "fingerprint": "guidance-v1:" + (f"{i:064x}" * 4)[:240],
        } for i in range(store.MAX_ENTRIES + 25)]
        assert working_set.record_deliveries(tmp_repo, sid, deliveries)
        path = working_set.path(tmp_repo, sid)
        rows = working_set.records(tmp_repo, sid)
        assert len(rows) <= store.MAX_ENTRIES
        assert path.stat().st_size <= working_set.MAX_BYTES
        assert rows[-1]["id"] == deliveries[-1]["id"]

        # The same opaque id in two stores is two identities, never one dedup row.
        working_set.record_deliveries(tmp_repo, sid, [{
            "scope": "global", "id": rows[-1]["id"],
            "fingerprint": "guidance-v1:global",
        }])
        scoped = {(row["scope"], row["id"]) for row in working_set.records(tmp_repo, sid)}
        assert ("personal", rows[-1]["id"]) in scoped
        assert ("global", rows[-1]["id"]) in scoped

    def test_oversized_sidecar_and_fields_fail_toward_replay(self, tmp_repo):
        sid = "oversized"
        path = working_set.path(tmp_repo, sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(" " * (working_set.MAX_BYTES + 1), encoding="utf-8")
        assert working_set.read(tmp_repo, sid) == {"injected": [], "records": []}
        assert working_set.record_deliveries(tmp_repo, "oversized-field", [{
            "scope": "personal", "id": "x" * (working_set.FIELD_MAX + 1),
            "fingerprint": "guidance-v1:valid",
        }])
        assert working_set.records(tmp_repo, "oversized-field") == []

    def test_mixed_legacy_hints_do_not_displace_newest_actual_delivery(self, tmp_repo):
        ids = [_approved_direct(
            tmp_repo, f"Use checkout reservation lease marker {i} for inventory consistency",
            title=f"Checkout lease marker {i}",
        ) for i in range(store._REHYDRATE_CAP + 2)]
        sid = "mixed-legacy-v2"
        path = working_set.path(tmp_repo, sid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"injected": ids[:-1], "ts": 0}), encoding="utf-8")

        rendered, receipts = store._render_prompt_decisions_with_records(tmp_repo, [ids[-1]])
        assert working_set.record_deliveries(tmp_repo, sid, receipts)
        assert working_set.ids(tmp_repo, sid)[-1] == ids[-1]

        replay = store._rehydrate_working_set(tmp_repo, sid)
        assert rendered in replay
        newest = next(row for row in working_set.records(tmp_repo, sid)
                      if row["id"] == ids[-1])
        assert newest["fingerprint"] == receipts[0]["fingerprint"]


class TestCompactionDeliveryBoundary:
    def test_replay_warns_when_inference_evidence_disappeared(self, tmp_repo):
        source = Path(tmp_repo) / "checkout-policy.md"
        source_text = "Checkout reservations use a lease queue."
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text(source_text, encoding="utf-8")
        entry = store.build_inferred_entry(
            "Checkout reservations use a lease queue", SESSION, "architecture", "suggested")
        entry["bootstrap"] = {
            "kind": "inferred",
            "assessment": "supported",
            "scope": "checkout reservations",
            "sources": [{
                "file": source.name,
                "line": 1,
                "quote": source_text,
                "sha256": hashlib.sha256(source_text.encode()).hexdigest(),
            }],
        }
        data = store.load(tmp_repo)
        data["entries"].append(entry)
        store.save(tmp_repo, data)
        _, receipts = store._render_prompt_decisions_with_records(
            tmp_repo, [entry["id"]])
        sid = "missing-inference-evidence"
        assert working_set.record_deliveries(tmp_repo, sid, receipts)

        source.unlink()
        replay = store._rehydrate_working_set(tmp_repo, sid)

        assert "Evidence changed or disappeared" in replay

    def test_claude_compaction_replays_refreshed_applicability_view(
        self, tmp_repo, monkeypatch
    ):
        from contexer import bootstrap

        entry = store.build_inferred_entry(
            "Use the inferred checkout lease queue", SESSION, "architecture", "suggested")
        entry["bootstrap"] = {
            "kind": "inferred", "assessment": "supported",
            "scope": "checkout queue", "sources": [],
        }
        data = store.load(tmp_repo)
        data["entries"].append(entry)
        store.save(tmp_repo, data)
        _, receipts = store._render_prompt_decisions_with_records(
            tmp_repo, [entry["id"]])
        sid = "refresh-before-replay"
        assert working_set.record_deliveries(tmp_repo, sid, receipts)

        def withhold(_repo_path, current):
            refreshed = copy.deepcopy(current)
            target = store.entry_by_id(refreshed["entries"], entry["id"])
            target["bootstrap_withheld"] = "Evidence disappeared"
            target["bootstrap_withheld_reason"] = "evidence"
            return refreshed

        monkeypatch.setattr(bootstrap, "refresh_for_session", withhold)
        payload = store._local_session_start_payload(
            tmp_repo, "compact", sid, "claude")

        assert "Use the inferred checkout lease queue" not in payload["context"]
        row = working_set.records(tmp_repo, sid)[0]
        assert row["id"] == entry["id"] and row["fingerprint"] is None

    def test_only_ten_replayed_rows_keep_credit_and_omitted_can_replay(self, tmp_repo):
        subjects = [
            "alpha beacon", "bravo compass", "charlie delta", "echo forest",
            "golf harbor", "india jungle", "kilo lantern", "mango nectar",
            "orbit prairie", "quartz river", "silver tundra", "violet willow",
        ]
        ids = [_approved_direct(
            tmp_repo, f"Use checkout reservation lease {subject} for inventory consistency",
            title=f"Checkout lease {subject}",
        ) for subject in subjects]
        data = store.load(tmp_repo)
        by_id = {e["id"]: e for e in data["entries"]}
        receipts = [{
            "scope": "personal", "id": did,
            "fingerprint": store._guidance_fingerprint(by_id[did], data),
        } for did in ids]
        sid = "compact-cap"
        assert working_set.record_deliveries(tmp_repo, sid, receipts)
        replay = store._rehydrate_working_set(tmp_repo, sid)
        assert replay.count("\n- [") <= store._REHYDRATE_CAP
        rows = working_set.records(tmp_repo, sid)
        credited = {r["id"] for r in rows if r["fingerprint"]}
        assert credited == set(ids[-store._REHYDRATE_CAP:])
        assert ids[0] not in credited
        omitted = store.get_context_for_prompt(
            tmp_repo, "Why use checkout reservation lease alpha beacon for inventory?", sid)
        assert "alpha beacon" in omitted

    def test_global_only_history_survives_both_empty_store_offer_branches(self, tmp_repo):
        _, gid = store.update_global_decision(
            "Global checkout reservation guidance", SESSION, "constraint", created_by="human")
        rendered, receipts = store._render_prompt_decisions_with_records(tmp_repo, [gid])
        assert "Global checkout" in rendered and receipts[0]["scope"] == "global"

        for offered in (False, True):
            sid = f"global-only-{offered}"
            working_set.record_deliveries(tmp_repo, sid, receipts)
            if offered:
                store._arm_offer(tmp_repo)
            else:
                store._offer_flag(tmp_repo).unlink(missing_ok=True)
            payload = store.post_compact_payload(tmp_repo, sid)
            assert "Global checkout reservation guidance" in payload["context"]
            assert working_set.has_credit(
                working_set.records(tmp_repo, sid), "global", gid,
                receipts[0]["fingerprint"],
            )

        claude_sid = "global-only-claude"
        working_set.record_deliveries(tmp_repo, claude_sid, receipts)
        claude_payload = store._local_session_start_payload(
            tmp_repo, "compact", claude_sid, "claude")
        assert "Global checkout reservation guidance" in claude_payload["context"]

    def test_missing_local_is_not_restored_and_old_credit_is_cleared(self, tmp_repo):
        did = _approved(tmp_repo, "Use checkout reservation leases for inventory")
        data = store.load(tmp_repo)
        entry = store.entry_by_id(data["entries"], did)
        fingerprint = store._guidance_fingerprint(entry, data)
        sid = "deleted-before-compact"
        working_set.record_deliveries(tmp_repo, sid, [{
            "scope": "personal", "id": did, "fingerprint": fingerprint,
        }])
        data["entries"] = []
        store.save(tmp_repo, data)
        payload = store.post_compact_payload(tmp_repo, sid)
        assert "Use checkout reservation" not in payload["context"]
        row = working_set.records(tmp_repo, sid)[0]
        assert row["id"] == did and row["fingerprint"] is None

    def test_ordinary_resume_does_not_reset_credit(self, tmp_repo):
        did = _approved(tmp_repo, "Use checkout reservation leases for inventory")
        data = store.load(tmp_repo)
        entry = store.entry_by_id(data["entries"], did)
        receipt = {"scope": "personal", "id": did,
                   "fingerprint": store._guidance_fingerprint(entry, data)}
        sid = "resume-keeps-credit"
        working_set.record_deliveries(tmp_repo, sid, [receipt])
        store._local_session_start_payload(tmp_repo, "resume", sid, "claude")
        assert working_set.records(tmp_repo, sid) == [receipt]

    def test_denied_compaction_write_returns_replay_and_discloses_stale_credit_behavior(
        self, tmp_repo, monkeypatch
    ):
        did = _approved(tmp_repo, "Use checkout reservation leases for inventory")
        rendered, receipts = store._render_prompt_decisions_with_records(tmp_repo, [did])
        sid = "compact-denied"
        working_set.record_deliveries(tmp_repo, sid, receipts)
        original = store.atomic_write

        def deny_ws(path, text):
            if path == working_set.path(tmp_repo, sid):
                raise OSError("denied")
            return original(path, text)

        monkeypatch.setattr(store, "atomic_write", deny_ws)
        payload = store.post_compact_payload(tmp_repo, sid)
        assert rendered in payload["context"]
        # Explicit residual limitation: the old row may survive when reset/receipt writes fail.
        assert working_set.records(tmp_repo, sid) == receipts
