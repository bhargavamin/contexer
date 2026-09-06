"""Recoverable scan limits and staleness, without recovering rejected policy."""

import copy
import json

import pytest

from contexer import bootstrap, revisions, server, store
from tests import test_bootstrap_inferred as fixtures
from tests.test_bootstrap_inferred import finish, ref

project = fixtures.project


def capture(project, *, source_paths=None):
    scan = bootstrap.run(str(project), "test", source_paths=source_paths)
    row = {"topic": "billing-outbox", "content": "Billing inserts email into the outbox transaction.",
           "scope": "billing", "kind": "observed", "subtype": "architecture",
           "assessment": "supported", "reason": "The same transaction writes both records.",
           "sources": [ref(project, "billing.py", 2, 4, "implementation")]}
    receipt = finish(project, scan, [row])
    return receipt["outcomes"][0]["id"]


def entry(project, did):
    return store.entry_by_id(store.load(str(project))["entries"], did)


@pytest.mark.parametrize("refresh", ["scan", "session"])
def test_exact_revert_recovers_code_topic_without_model_report(project, refresh):
    did = capture(project)
    before = copy.deepcopy(entry(project, did))
    path = project / "billing.py"
    original = path.read_bytes()
    path.write_text("send_synchronously()\n")
    bootstrap.run(str(project), "changed")
    assert entry(project, did)["bootstrap_withheld"]
    path.write_bytes(original)
    if refresh == "scan":
        scan = bootstrap.run(str(project), "restored")
        finish(project, scan, [])  # code topics aren't nominated docs: no forced re-report
    else:
        store.session_start_payload(str(project))
    recovered = entry(project, did)
    assert not recovered.get("bootstrap_withheld")
    assert store.entry_status(recovered) == "suggested"
    assert recovered["revisions"] == before["revisions"]
    assert revisions.compute_confidence(recovered)[0] == 30


@pytest.mark.parametrize("reason", ["historical", "unsupported", "custom"])
def test_matching_fingerprints_do_not_revive_non_stale_withholding(project, reason):
    did = capture(project)
    data = store.load(str(project))
    old = store.entry_by_id(data["entries"], did)
    old["bootstrap_withheld"] = "Not applicable guidance"
    old["bootstrap_withheld_reason"] = reason
    store.save(str(project), data)
    bootstrap.run(str(project), "again")
    assert entry(project, did)["bootstrap_withheld_reason"] == reason


def test_legacy_evidence_withholding_recovers_but_historical_message_does_not(project):
    did = capture(project)
    data = store.load(str(project))
    old = store.entry_by_id(data["entries"], did)
    old["bootstrap_withheld"] = "Evidence changed, disappeared, or is outside the authorized snapshot"
    store.save(str(project), data)
    bootstrap.run(str(project), "again")
    assert not entry(project, did).get("bootstrap_withheld")
    data = store.load(str(project))
    store.entry_by_id(data["entries"], did)["bootstrap_withheld"] = "Source is historical; not current guidance"
    store.save(str(project), data)
    bootstrap.run(str(project), "again")
    assert entry(project, did)["bootstrap_withheld"]


def test_withheld_inference_can_be_permanently_ignored(project):
    did = capture(project)
    path = project / "billing.py"
    original = path.read_bytes()
    path.write_text("send_synchronously()\n")
    bootstrap.run(str(project), "changed")
    assert store.entry_status(entry(project, did)) == "ignored"
    assert entry(project, did)["status"] == "suggested"
    assert store.approve_decision(str(project), did, "ignore")[0]
    path.write_bytes(original)
    scan = bootstrap.run(str(project), "restored")
    finish(project, scan, [])
    assert entry(project, did)["status"] == "ignored"
    assert not bootstrap.directive(str(project))
    assert capture(project) == did
    assert entry(project, did)["status"] == "ignored"


def test_inventory_cap_does_not_withhold_unchanged_citation_and_focus_recovers(project, monkeypatch):
    did = capture(project)
    monkeypatch.setattr(bootstrap, "MAX_FILES", 1)
    (project / "AGENTS.md").write_text("# Overview\nA billing service.\n")
    scan = bootstrap.run(str(project), "budget")
    assert "billing.py" not in scan["files"]
    assert not entry(project, did).get("bootstrap_withheld")
    assert set(bootstrap._unchecked(entry(project, did))) == {"inventory_unassessed"}
    focused = bootstrap.run(str(project), "focus", source_paths=["billing.py"])
    assert set(focused["files"]) == {"billing.py"}
    assert capture(project) == did  # remembered focus allows re-report


def test_omitted_citation_still_detects_actual_changes(project, monkeypatch):
    did = capture(project)
    monkeypatch.setattr(bootstrap, "MAX_FILES", 0)
    (project / "billing.py").write_text("send_synchronously()\n")
    bootstrap.run(str(project), "changed")
    assert entry(project, did)["bootstrap_withheld_reason"] == "evidence"


def test_budget_unknown_is_labeled_not_withheld_and_focus_rechecks(project, monkeypatch):
    did = capture(project)
    monkeypatch.setattr(bootstrap, "MAX_FILES", 0)
    monkeypatch.setattr(bootstrap, "MAX_FOCUSED_BYTES", 1)
    bootstrap.run(str(project), "budget")
    old = entry(project, did)
    assert not old.get("bootstrap_withheld")
    assert old["bootstrap_unchecked"]
    assert "Freshness unverified" in " ".join(bootstrap.render(old))
    monkeypatch.setattr(bootstrap, "MAX_FOCUSED_BYTES", 2_000_000)
    monkeypatch.setattr(bootstrap, "MAX_FILES", 160)
    bootstrap.run(str(project), "focus", source_paths=["billing.py"])
    assert "citation_budget" not in bootstrap._unchecked(entry(project, did))
    assert "inventory_unassessed" in bootstrap._unchecked(entry(project, did))


def test_large_file_focused_capture_uses_full_hash_not_just_excerpt(project):
    path = project / "billing.py"
    path.write_text(path.read_text() + "# unrelated implementation context\n" * 4000)
    assert path.stat().st_size > bootstrap.MAX_FILE_BYTES
    assert "billing.py" not in bootstrap.run(str(project), "normal")["files"]
    did = capture(project, source_paths=["billing.py"])
    old = entry(project, did)
    assert not old.get("bootstrap_withheld")
    path.write_text(path.read_text() + "send_synchronously()\n")
    store.session_start_payload(str(project))
    assert entry(project, did)["bootstrap_withheld"]
    # Exact quote still exists, but changes elsewhere in the file require re-analysis.
    assert old["bootstrap"]["sources"][0]["quote"] in path.read_text()


@pytest.mark.parametrize("sources", [["../outside.py"], ["/tmp/outside.py"], ["billing.txt"],
                                     [".git/config.py"], [""]])
def test_focus_cannot_expand_repository_authorization(project, sources):
    before = store.load(str(project))
    with pytest.raises(ValueError, match="Focused sources"):
        bootstrap.run(str(project), "bad", source_paths=sources)
    assert store.load(str(project)) == before


def test_focus_symlink_is_omitted_not_read(project, tmp_path):
    outside = tmp_path / "outside.py"
    outside.write_text("sensitive = True\n")
    (project / "linked.py").symlink_to(outside)
    scan = bootstrap.run(str(project), "bad", source_paths=["linked.py"])
    assert "linked.py" not in scan["files"]
    assert any("linked.py" in note for note in scan["omitted"])


def test_focus_change_invalidates_old_report_and_preview_does_not_save_focus(project):
    old = bootstrap.run(str(project), "normal")
    preview = bootstrap.run(str(project), "preview", apply=False, source_paths=["billing.py"])
    assert preview["source_paths"] == ["billing.py"]
    assert store.load(str(project))["bootstrap_scan"]["source_paths"] == []
    bootstrap.run(str(project), "focus", source_paths=["billing.py"])
    with pytest.raises(ValueError, match="superseded"):
        finish(project, old, [])
    assert bootstrap.run(str(project), "clear", source_paths=[])["source_paths"] == []


def test_server_exposes_focused_scan_without_familiarity_parameter(project):
    import inspect
    signature = inspect.signature(server.bootstrap_context)
    assert "insight" not in signature.parameters
    assert "source_paths" in signature.parameters
    result = json.loads(server.bootstrap_context(repo_path=str(project), source_paths=["billing.py"]))
    assert result["source_paths"] == ["billing.py"]


def test_freshness_rechecks_omitted_evidence_in_current_checkout(project, tmp_path, monkeypatch):
    did = capture(project)
    other = tmp_path / "other-checkout"
    other.mkdir()
    (other / "billing.py").write_text("send_synchronously()\n")
    monkeypatch.setattr(bootstrap, "MAX_FILES", 0)
    view = bootstrap.freshness_view(str(other), store.load(str(project)))
    assert store.entry_by_id(view["entries"], did)["bootstrap_withheld"]
    assert not entry(project, did).get("bootstrap_withheld")  # projection is read-only


def test_unknown_budget_cannot_clear_existing_staleness(project, monkeypatch):
    did = capture(project)
    original = (project / "billing.py").read_bytes()
    (project / "billing.py").write_text("send_synchronously()\n")
    bootstrap.run(str(project), "changed")
    (project / "billing.py").write_bytes(original)
    monkeypatch.setattr(bootstrap, "MAX_FILES", 0)
    monkeypatch.setattr(bootstrap, "MAX_FOCUSED_BYTES", 1)
    bootstrap.run(str(project), "unknown")
    assert entry(project, did)["bootstrap_withheld"]
    assert entry(project, did)["bootstrap_unchecked"]


def test_focus_has_explicit_file_and_byte_limits(project):
    with pytest.raises(ValueError, match="at most 20"):
        bootstrap.run(str(project), "too-many", source_paths=[f"file{i}.py" for i in range(21)])
    (project / "large.py").write_text("#" * (bootstrap.MAX_FOCUSED_BYTES + 1))
    scan = bootstrap.run(str(project), "too-large", source_paths=["large.py"])
    assert "large.py" not in scan["files"]
    assert any("large.py" in note and "oversized" in note for note in scan["omitted"])


def test_reports_cannot_change_focused_inventory(project):
    scan = bootstrap.run(str(project), "test")
    with pytest.raises(ValueError, match="separate scan"):
        bootstrap.run(str(project), "report", snapshot_id=scan["snapshot_id"], findings=[],
                      finish=True, source_paths=["billing.py"])
