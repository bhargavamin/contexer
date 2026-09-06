"""Authority heads must not change because a freshness projection changed."""

import copy

import pytest

from contexer import bootstrap, store
from tests import test_bootstrap_inferred as fixtures

project = fixtures.project


def test_refresh_then_exact_revert_does_not_reject_inflight_report(project):
    scan = fixtures.scan_rule(project)
    row = fixtures.finding(project, scan)
    receipt = fixtures.finish(project, scan, [row])
    before = store.load(str(project))
    path = project / "billing.py"
    original = path.read_bytes()
    path.write_text("send_synchronously()\n")
    store.session_start_payload(str(project))
    changed = store.load(str(project))
    assert any(e.get("bootstrap_withheld") for e in changed["entries"])
    assert changed["bootstrap_scan"]["snapshot_id"] == receipt["snapshot_id"]
    assert bootstrap._heads(changed["entries"]) == bootstrap._heads(before["entries"])
    path.write_bytes(original)
    result = fixtures.finish(project, receipt, [row])
    assert result["stage"] == "reported_complete"
    assert not any(e.get("bootstrap_withheld") for e in store.load(str(project))["entries"])


def test_loaded_legacy_status_and_explicit_approved_have_identical_heads(project):
    bootstrap.run(str(project), "test")
    data = store.load(str(project))
    data["entries"][0].pop("status")
    store.save(str(project), data)
    loaded = store.load_for_update(str(project))
    assert "status" not in loaded["entries"][0]
    explicit = copy.deepcopy(loaded["entries"])
    explicit[0]["status"] = "approved"
    assert bootstrap._heads(loaded["entries"]) == bootstrap._heads(explicit)


@pytest.mark.parametrize("action", ["approve", "edit", "ignore"])
def test_human_action_still_invalidates_analysis_basis(project, action):
    scan = fixtures.scan_rule(project)
    entry_id = store.load(str(project))["entries"][0]["id"]
    assert store.approve_decision(str(project), entry_id, action, "Use Python 3.13.")[0]
    with pytest.raises(ValueError, match="decision changed"):
        fixtures.finish(project, scan, [fixtures.finding(project, scan)])
