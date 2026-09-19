"""Bounded-cost tripwires for Contract 04; these make no real-time guarantee."""

import json
import time
from pathlib import Path

import pytest

from contexer import decision_impact, policy, policy_api
from tests.test_policy_api import _arm, _grant_file_reads, _seed


def _percentile(samples, fraction):
    ordered = sorted(samples)
    return ordered[min(int(len(ordered) * fraction), len(ordered) - 1)]


@pytest.mark.perf
def test_full_history_append_traversal_and_old_receipt_lookup_remain_bounded(tmp_repo):
    _grant_file_reads(tmp_repo, diagnostics=True)
    envelope = decision_impact.guidance_envelope(
        tmp_repo, tmp_repo, route="explicit_lookup", rows=[{
            "scope": "personal", "id": "d", "revision_id": "r",
            "fingerprint": "guidance-v1:" + "a" * 64, "authority": "approved",
            "tier": "full", "reason": "query", "files": ["src/app.py"],
        }])
    ids = [decision_impact.append(tmp_repo, envelope, now=100 + i)
           for i in range(decision_impact.MAX_RECORDS)]
    assert all(ids)

    append_ms = []
    for i in range(20):
        started = time.perf_counter()
        assert decision_impact.append(tmp_repo, envelope, now=1000 + i)
        append_ms.append((time.perf_counter() - started) * 1000)

    seen = []
    cursor = ""
    started = time.perf_counter()
    while True:
        page = decision_impact.report(tmp_repo, limit=50, cursor=cursor, now=2000)
        assert page["status"] == "ok"
        seen.extend(row["receipt_id"] for row in page["records"])
        cursor = page["next_cursor"]
        if not cursor:
            break
    traversal_ms = (time.perf_counter() - started) * 1000
    started = time.perf_counter()
    exact = decision_impact.report(tmp_repo, receipt_id=seen[-1], now=2000)
    exact_ms = (time.perf_counter() - started) * 1000

    assert len(seen) == len(set(seen)) == decision_impact.MAX_RECORDS
    assert exact["status"] == "ok"
    assert _percentile(append_ms, 0.95) < 25
    assert traversal_ms < 250
    assert exact_ms < 25
    assert len(json.dumps(page).encode()) <= decision_impact.MAX_RESPONSE_BYTES


@pytest.mark.perf
def test_near_cap_bounded_file_profile_completes_without_unsafe_regex_work(tmp_repo):
    _grant_file_reads(tmp_repo)
    target = Path(tmp_repo) / "app.py"
    line = "a" * 120 + "c\n"
    target.write_text(line * (policy.MAX_BOUNDED_FILE_BYTES // len(line)))
    for i in range(policy.MAX_BOUNDED_FILE_RULES):
        entry = _seed(
            tmp_repo, f"Condition {i} forbids a repeated literal", title=f"Rule {i}",
            source_files=["app.py"])
        _arm(tmp_repo, entry["id"], pattern="a" * 120 + "b", paths="app.py")

    started = time.perf_counter()
    result = policy_api.evaluate_operation(
        tmp_repo, operation="commit", artifact_path="app.py")
    elapsed_ms = (time.perf_counter() - started) * 1000

    assert result["verdict"] == "allow"
    assert result["evaluation_status"] == "complete"
    assert elapsed_ms < 1000
