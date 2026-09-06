"""Executable gates for incremental capture and independently owned applicability."""

import copy
import json
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from contexer import bootstrap, revisions, server, share, store
from tests import test_bootstrap_inferred as fixtures
from tests.test_bootstrap_inferred import finding, finish, scan_rule
from tests.test_bootstrap_recovery import capture, entry

project = fixtures.project


def refresh(project):
    return bootstrap.refresh_for_session(str(project), store.load(str(project)))


def inventory_fixture(project):
    path = project / "pyproject.toml"
    path.write_text(path.read_text().replace('[tool.ruff]', 'dependencies = ["httpx"]\n[tool.ruff]'))
    scan = scan_rule(project)
    row = finding(project, scan)
    second = {k: v for k, v in row.items() if k != "candidate_id"}
    second.update(topic="billing-transaction", content="Billing writes the charge and outbox in one transaction.")
    receipt = finish(project, scan, [row, second])
    human = store.build_inferred_entry("Keep billing records for seven years.", "test", "constraint", "pending_approval")
    data = store.load(str(project))
    data["entries"].append(human)
    store.save(str(project), data)
    assert store.approve_decision(str(project), human["id"], "approve")[0]
    scan = bootstrap.run(str(project), "new-policy-basis")
    return scan, receipt


def assess(project, scan, delta):
    return bootstrap.run(str(project), "assess", snapshot_id=scan["snapshot_id"], findings=[],
                         assessed_delta=delta["id"], finish=True)


def test_exact_two_caveats_survive_refresh_then_assessment_clears_durably(project):
    scan, _ = inventory_fixture(project)
    (project / "new_worker.py").write_text("BACKEND = 'smtp'\n")
    view = refresh(project)
    again = refresh(project)
    assert view == again
    assert len(view["entries"]) == 6
    assert sum(bool(e.get("bootstrap_withheld")) for e in view["entries"]) == 0
    assert sum("inventory_unassessed" in bootstrap._unchecked(e) for e in view["entries"]) == 2
    assert sum(store.entry_status(e) != "ignored" for e in view["entries"]) == 6
    assert view["bootstrap_scan"]["snapshot_id"] == scan["snapshot_id"]
    delta = view["bootstrap_scan"]["inventory_delta"]
    receipt = assess(project, scan, delta)
    assert not receipt["inventory_delta"]
    for _ in range(2):
        view = refresh(project)
        assert not any(e.get("bootstrap_unchecked") for e in view["entries"])
        assert not view["bootstrap_scan"].get("refresh_needed")
    assert not bootstrap.directive(str(project))


def test_old_delta_cannot_clear_second_change_and_human_basis_is_bound(project):
    scan, _ = inventory_fixture(project)
    (project / "extra.py").write_text("VALUE = 1\n")
    first = refresh(project)["bootstrap_scan"]["inventory_delta"]
    (project / "extra.py").write_text("VALUE = 2\n")
    second = refresh(project)["bootstrap_scan"]["inventory_delta"]
    assert first["id"] != second["id"]
    before = store.load(str(project))
    with pytest.raises(ValueError, match="Inventory delta changed"):
        assess(project, scan, first)
    assert store.load(str(project)) == before
    did = before["entries"][0]["id"]
    store.approve_decision(str(project), did, "edit", "Python 3.14 is now required.")
    with pytest.raises(ValueError, match="decision changed"):
        assess(project, scan, second)


def test_budget_and_inventory_have_independent_clear_owners(project, monkeypatch):
    did = capture(project)
    (project / "new.py").write_text("VALUE = 1\n")
    view = refresh(project)
    scan = view["bootstrap_scan"]
    monkeypatch.setattr(bootstrap, "MAX_FOCUSED_BYTES", 0)
    bootstrap._refresh_entries(view["entries"], {**scan, "files": {}})
    old = store.entry_by_id(view["entries"], did)
    assert set(bootstrap._unchecked(old)) == {"citation_budget", "inventory_unassessed"}
    monkeypatch.setattr(bootstrap, "MAX_FOCUSED_BYTES", 2_000_000)
    bootstrap._refresh_entries(view["entries"], scan)
    assert set(bootstrap._unchecked(old)) == {"inventory_unassessed"}
    assert not old.get("bootstrap_withheld")


@pytest.mark.parametrize("legacy", ["Evidence exceeded the recheck budget; use a focused source_paths scan.",
                                    "Legacy evidence freshness needs checking."])
def test_legacy_string_renders_and_clears_only_after_successful_citation_check(project, legacy):
    did = capture(project)
    data = store.load(str(project))
    old = store.entry_by_id(data["entries"], did)
    old["bootstrap_unchecked"] = legacy
    store.save(str(project), data)
    assert legacy in " ".join(bootstrap.render(entry(project, did)))
    unknown = bootstrap.freshness_view(str(project), data, unavailable=True)
    assert bootstrap._unchecked(store.entry_by_id(unknown["entries"], did))
    refresh(project)
    assert not entry(project, did).get("bootstrap_unchecked")


@pytest.mark.parametrize("known_stale", [False, True])
def test_real_store_contention_is_render_only_and_never_revives_known_stale(project, known_stale):
    did = capture(project)
    if known_stale:
        (project / "billing.py").write_text("send_synchronously()\n")
        refresh(project)
    data = store.load(str(project))
    path = store._store_path(str(project))
    before = path.read_bytes()
    with store.store_lock(store.repo_slug(str(project))):
        with ThreadPoolExecutor(max_workers=1) as pool:
            view = pool.submit(bootstrap.refresh_for_session, str(project), data).result(timeout=5)
    assert path.read_bytes() == before
    old = store.entry_by_id(view["entries"], did)
    assert bool(old.get("bootstrap_withheld")) == known_stale
    assert old["bootstrap_check_unavailable"]
    assert "Freshness unverified" in " ".join(bootstrap.render(old))
    assert not entry(project, did).get("bootstrap_check_unavailable")


def test_revoked_external_authorization_is_invalid_even_when_checks_unavailable(project, tmp_path, monkeypatch):
    external = tmp_path / "shared.md"
    external.write_text("# Rules\n- Use a transactional outbox for billing email.\n")
    scan = bootstrap.run(str(project), "test", external_paths=[str(external)])
    row = finding(project, scan)
    did = finish(project, scan, [row])["outcomes"][0]["id"]
    data = store.load(str(project))
    data["bootstrap_scan"]["external_paths"] = []
    monkeypatch.setattr(bootstrap, "_text", lambda *a, **k: pytest.fail("Must not read revoked evidence"))
    view = bootstrap.freshness_view(str(project), data, unavailable=True)
    assert store.entry_by_id(view["entries"], did)["bootstrap_withheld_reason"] == "evidence"


def test_failed_read_is_ephemeral_uncertainty_but_deleted_source_is_invalid(project, monkeypatch):
    did = capture(project)
    monkeypatch.setattr(bootstrap, "MAX_FILES", 0)
    original = bootstrap._text

    def denied(*args, **kwargs):
        raise PermissionError("permission denied")

    monkeypatch.setattr(bootstrap, "_text", denied)
    view = refresh(project)
    old = store.entry_by_id(view["entries"], did)
    assert old.get("bootstrap_check_unavailable")
    assert not old.get("bootstrap_withheld")
    assert not entry(project, did).get("bootstrap_check_unavailable")
    monkeypatch.setattr(bootstrap, "_text", original)
    (project / "billing.py").unlink()
    refresh(project)
    assert entry(project, did)["bootstrap_withheld_reason"] == "evidence"


def test_reworded_rule_explicitly_revises_uuid_and_deleted_row_is_not_replayed(project):
    scan = scan_rule(project)
    did = finish(project, scan, [finding(project, scan)])["outcomes"][0]["id"]
    before = entry(project, did)
    scan = scan_rule(project, "Use a transactional outbox for all billing notifications.")
    assert scan["candidates"][0]["candidate_id"] != before["bootstrap"]["candidate_id"]
    assert not scan["reported_keys"]
    assert any(d["id"] == did and d["withheld"] for d in scan["decisions"])
    row = {**finding(project, scan), "replaces": did}
    receipt = finish(project, scan, [row])
    assert receipt["outcomes"][0]["id"] == did
    assert entry(project, did)["revision"] == before["revision"] + 1
    (project / "ARCHITECTURE.md").write_text("# Overview\nBilling service.\n")
    scan = bootstrap.run(str(project), "deleted")
    assert not scan["reported_keys"]
    for _ in range(2):
        scan = finish(project, scan, [])
        assert not scan["outcomes"]
        assert entry(project, did)["bootstrap_withheld"]


def test_unrelated_edits_between_batches_preserve_progress_and_converge(project):
    scan = scan_rule(project)
    row = finding(project, scan)
    for index in range(5):
        (project / "worker.py").write_text(f"GENERATION = {index}\n")
        scan = bootstrap.run(str(project), "batch", snapshot_id=scan["snapshot_id"], findings=[row])
        assert len([e for e in store.load(str(project))["entries"] if e.get("bootstrap", {}).get("candidate_id")]) == 1
    scan = finish(project, scan, [])
    assert scan["stage"] == "reported_complete"
    delta = scan["inventory_delta"]
    scan = assess(project, scan, delta)
    assert not bootstrap.directive(str(project))
    old = next(e for e in store.load(str(project))["entries"] if e.get("bootstrap", {}).get("candidate_id"))
    assert old["revision"] == 1
    assert revisions.compute_confidence(old)[0] == 30


def test_changed_citation_defers_one_finding_not_valid_peer(project):
    scan = scan_rule(project)
    stale = finding(project, scan)
    good = {**stale, "sources": [stale["sources"][0]], "assessment": "unverified"}
    stale.pop("candidate_id")
    stale.update(topic="billing-transaction", kind="observed")
    (project / "billing.py").write_text("send_synchronously()\n")
    receipt = finish(project, scan, [stale, good])
    assert len(receipt["deferred"]) == 1
    assert len(receipt["outcomes"]) == 1
    assert receipt["stage"] == "reported_complete"
    assert receipt["outcomes"][0]["outcome"] == "stored"


def test_analysis_aba_never_restores_old_token_and_refresh_does_not_advance(project):
    scan = bootstrap.run(str(project), "a")
    path = project / "extra.py"
    path.write_text("VALUE = 1\n")
    middle = bootstrap.run(str(project), "b")
    path.unlink()
    restored = bootstrap.run(str(project), "a-again")
    assert restored["snapshot_id"] != scan["snapshot_id"]
    assert restored["generation"] > middle["generation"] > scan["generation"]
    view = refresh(project)
    assert view["bootstrap_scan"]["generation"] == restored["generation"]
    with pytest.raises(ValueError, match="superseded"):
        finish(project, scan, [])


def test_conflict_group_defers_whole_question_and_recovers_all_members(project):
    scan, rows = fixtures.disputed_rules(project)
    finish(project, scan, rows)
    data = store.load(str(project))
    pending = [e for e in data["entries"] if e["status"] == "pending_approval"]
    pending[0]["bootstrap_withheld"] = "Evidence changed"
    pending[0]["bootstrap_withheld_reason"] = "evidence"
    outcomes = [{"key": e["bootstrap"]["key"], "outcome": "updated", "requires_clarification": True} for e in pending]
    assert not bootstrap._clarifications(data["entries"], outcomes)
    groups = bootstrap._clarification_groups(data["entries"], outcomes)
    assert len(groups) == 1 and groups[0]["state"] == "recheck" and groups[0]["question"] == ""
    assert {e["id"] for e in groups[0]["decisions"]} == {e["id"] for e in pending}
    bootstrap._refresh_entries(data["entries"], data["bootstrap_scan"])
    ready = bootstrap._clarifications(data["entries"], outcomes)
    assert len(ready) == 1 and len(ready[0]["decisions"]) == 2
    assert not any("recheck_worklist" in e for e in data["entries"])


def test_assessment_is_exposed_on_existing_mcp_tool(project):
    scan = bootstrap.run(str(project), "test")
    (project / "extra.py").write_text("VALUE = 1\n")
    delta = refresh(project)["bootstrap_scan"]["inventory_delta"]
    result = json.loads(server.bootstrap_context(str(project), snapshot_id=scan["snapshot_id"],
                                                findings=[], finish=True, assessed_delta=delta["id"]))
    assert result["stage"] == "reported_complete" and not result["inventory_delta"]


def test_rescan_does_not_clear_unassessed_inventory_warning(project):
    did = capture(project)
    (project / "extra.py").write_text("VALUE = 1\n")
    first = refresh(project)["bootstrap_scan"]["inventory_delta"]["id"]
    scan = bootstrap.run(str(project), "rescan")
    assert scan["inventory_delta"]["id"] == first
    assert entry(project, did)["bootstrap_unchecked"]["inventory_unassessed"]["delta_id"] == first
    assert scan["inventory_delta"]["paths_to_assess"] == ["extra.py"]


def test_persisted_state_has_no_source_blob_or_render_only_worklist(project):
    capture(project)
    before = copy.deepcopy(store.load(str(project)))
    (project / "extra.py").write_text("# sentinel-secret-not-to-retain\n" + "VALUE = 1\n" * 1000)
    after = refresh(project)
    serialized = json.dumps(after)
    assert "sentinel-secret-not-to-retain" not in serialized
    assert "recheck_worklist" not in serialized
    assert len(serialized) - len(json.dumps(before)) < 2000


def test_partial_conflict_report_never_shrinks_group_on_replay(project):
    scan, rows = fixtures.disputed_rules(project)
    # Only the contradicted member cites implementation; its peer cites current docs.
    rows[1]["sources"] = rows[1]["sources"][:1]
    receipt = finish(project, scan, rows)
    original_ids = {e["id"] for e in receipt["clarifications"][0]["decisions"]}
    (project / "billing.py").write_text("send_synchronously()\n")
    refresh(project)
    for _ in range(2):
        receipt = bootstrap.run(str(project), "partial", snapshot_id=receipt["snapshot_id"], findings=[rows[1]])
        assert not receipt["clarifications"]
        assert len(receipt["recheck_worklist"]) == 1
        assert {e["id"] for e in receipt["recheck_worklist"][0]["decisions"]} == original_ids


def test_superseded_observation_cannot_be_recreated_by_repeated_old_reports(project):
    scan, rows = fixtures.disputed_rules(project)
    rows = rows[:2]
    # Exercise explicit observation identity, independently of dispute semantics.
    for row in rows:
        row.update(assessment="unverified", sources=row["sources"][:1])
        row.pop("question", None)
    receipt = bootstrap.run(str(project), "first", snapshot_id=scan["snapshot_id"], findings=[rows[0]])
    did = receipt["outcomes"][0]["id"]
    rows[1]["replaces"] = did
    receipt = bootstrap.run(str(project), "supersede", snapshot_id=receipt["snapshot_id"], findings=[rows[1]])
    for _ in range(2):
        receipt = bootstrap.run(str(project), "late-old", snapshot_id=receipt["snapshot_id"], findings=[rows[0]])
        assert any(o["outcome"] == "superseded" for o in receipt["outcomes"])
        old = entry(project, did)
        assert old["bootstrap"]["candidate_id"] == rows[1]["candidate_id"]
        assert old["revision"] == 2
        assert len([e for e in store.load(str(project))["entries"] if e.get("bootstrap", {}).get("candidate_id")]) == 1


def test_deleted_reported_rule_between_batches_does_not_erase_capture_progress(project):
    scan = scan_rule(project)
    receipt = bootstrap.run(str(project), "first", snapshot_id=scan["snapshot_id"], findings=[finding(project, scan)])
    did = receipt["outcomes"][0]["id"]
    (project / "ARCHITECTURE.md").unlink()
    receipt = finish(project, receipt, [])
    assert receipt["stage"] == "reported_complete"
    assert receipt["candidate_receipts"][scan["candidates"][0]["candidate_id"]] == "needs_recheck"
    assert not receipt["outcomes"]
    assert entry(project, did)["bootstrap_withheld"]


def test_delta_assessment_does_not_reactivate_invalid_evidence(project):
    did = capture(project)
    scan = store.load(str(project))["bootstrap_scan"]
    (project / "billing.py").write_text("send_synchronously()\n")
    view = refresh(project)
    assess(project, scan, view["bootstrap_scan"]["inventory_delta"])
    old = entry(project, did)
    assert not old.get("bootstrap_unchecked")
    assert old["bootstrap_withheld_reason"] == "evidence"
    assert store.entry_status(old) == "ignored"


def test_duplicate_candidate_in_different_scopes_still_rejects_batch(project):
    scan = scan_rule(project)
    row = finding(project, scan)
    before = store.load(str(project))
    with pytest.raises(ValueError, match="Duplicate finding"):
        finish(project, scan, [row, {**row, "scope": "different scope"}])
    assert store.load(str(project)) == before


def test_disappeared_authorized_document_does_not_block_rescan(project, tmp_path):
    external = tmp_path / "shared.md"
    external.write_text("# Rules\n- Use a transactional outbox for billing email.\n")
    scan = bootstrap.run(str(project), "test", external_paths=[str(external)])
    did = finish(project, scan, [finding(project, scan)])["outcomes"][0]["id"]
    external.unlink()
    scan = bootstrap.run(str(project), "rescan")
    assert not scan["candidates"]
    assert entry(project, did)["bootstrap_withheld_reason"] == "evidence"


@pytest.mark.parametrize("field,value", [("generation", -1), ("generation", True),
                                          ("candidate_receipts", {"bad": []}),
                                          ("assessed_inventory", "not-a-fingerprint")])
def test_malformed_new_state_never_gets_overwritten(project, field, value):
    bootstrap.run(str(project), "test")
    data = store.load(str(project))
    data["bootstrap_scan"][field] = value
    store.save(str(project), data)
    before = store._store_path(str(project)).read_bytes()
    with pytest.raises(ValueError, match="Malformed bootstrap"):
        bootstrap.run(str(project), "rescan")
    assert store._store_path(str(project)).read_bytes() == before


def test_max_report_budget_has_bounded_applicability_overhead(project):
    """Size gate is deterministic; timing is diagnostic only, not a flaky CI speed threshold."""
    (project / "billing.py").write_text("\n".join(f"VALUE_{i} = '" + "x" * 65 + "'" for i in range(20)) + "\n")
    scan = bootstrap.run(str(project), "test")
    source = fixtures.ref(project, "billing.py", 1, 20, "implementation")
    rows = [{"topic": f"observation-{i}", "content": f"Billing has observation number {i}.",
             "scope": "billing", "kind": "observed", "subtype": "architecture",
             "assessment": "supported", "reason": "Benchmark source fixture.", "sources": [source] * 8}
            for i in range(80)]
    receipt = bootstrap.run(str(project), "first", snapshot_id=scan["snapshot_id"], findings=rows[:40])
    finish(project, receipt, rows[40:])
    before = store.load(str(project))
    (project / "extra.py").write_text("VALUE = 1\n")
    after = refresh(project)
    sizes = [len(json.dumps(data).encode()) for data in (before, after)]
    elapsed = []
    for data in (before, after):
        start = time.perf_counter()
        for _ in range(5):
            store.save(str(project), data)
        elapsed.append((time.perf_counter() - start) * 1000 / 5)
    assert sizes[1] - sizes[0] < 30_000
    assert sum("inventory_unassessed" in bootstrap._unchecked(e) for e in after["entries"]) == 80
    print({"store_bytes_before_after": sizes, "mean_save_ms_before_after": elapsed,
           "additional_bytes": sizes[1] - sizes[0]})


def test_bootstrap_metadata_and_caveats_do_not_enter_share_projection(project):
    did = capture(project)
    data = store.load(str(project))
    old = store.entry_by_id(data["entries"], did)
    sentinel = "private-bootstrap-metadata-sentinel"
    old["bootstrap"]["reason"] = sentinel
    old["bootstrap_unchecked"] = {"inventory_unassessed": {"delta_id": sentinel, "message": sentinel}}
    store.save(str(project), data)
    assert store.approve_decision(str(project), did, "approve")[0]
    projected = store.get_shareable(str(project), did)
    assert projected and sentinel not in json.dumps(projected)
    assert sentinel not in json.dumps(share._payload(projected, "local-fixture"))
