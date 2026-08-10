"""Tests for the memory-campaign-specific validator checks: contamination
isolation and tier-coverage balance. Both are no-ops on legacy rows lacking
the `arm` field (Task 6 introduced arm/tier/phase/contaminated)."""
from benchmarks.validate import _check_memory_isolation, _check_tier_coverage


def _row(**kw):
    base = {"task_id": "sup-current", "kind": "supersession", "arm": "with",
            "tier": "implicit", "phase": "measure", "contaminated": False, "error": ""}
    return base | kw


def test_contaminated_row_is_failure():
    fails = []
    _check_memory_isolation([_row(contaminated=True)], fails)
    assert fails and "contaminated" in fails[0]


def test_legacy_rows_without_arm_are_ignored():
    fails = []
    _check_memory_isolation([{"task_id": "rat-storage", "kind": "rationale", "error": ""}], fails)
    assert fails == []


def test_tier_imbalance_warns():
    warns = []
    rows = [_row(tier="implicit"), _row(tier="implicit")]
    _check_tier_coverage(rows, warns)
    assert warns and "tier" in warns[0].lower()


def test_enforcement_rows_exempt_from_contamination_check():
    fails = []
    _check_memory_isolation(
        [_row(task_id="enf-commit", kind="enforcement", contaminated=True)], fails)
    assert fails == []


def test_teach_rows_exempt_from_contamination_check():
    fails = []
    _check_memory_isolation([_row(phase="teach", contaminated=True)], fails)
    assert fails == []
