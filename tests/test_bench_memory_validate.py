"""Tests for the memory-campaign-specific validator checks: contamination
isolation, tier-coverage balance, the required memory column set, and the
median/coverage exclusions. All are no-ops on legacy rows lacking the `arm`
field (Task 6 introduced arm/tier/phase/contaminated)."""
from benchmarks.validate import (MEMORY_KEYS, _check_coverage, _check_memory_isolation,
                                 _check_schema, _check_tier_coverage, _median_rows)


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


# --- Important 4: the memory column set is required, never assumed ---------

def _full_row(**kw):
    """A schema-complete memory row (EXPECTED_KEYS + MEMORY_KEYS)."""
    from benchmarks.validate import EXPECTED_KEYS
    base = {k: 0 for k in EXPECTED_KEYS} | {k: "" for k in MEMORY_KEYS}
    base |= {"task_id": "sup-current", "kind": "supersession", "condition": "with",
             "arm": "with", "phase": "measure", "contaminated": False, "capture": {},
             "error": ""}
    return base | kw


def test_dropped_memory_key_is_a_failure():
    row = _full_row()
    del row["contaminated"]
    fails = []
    _check_schema([row], fails)
    assert any("contaminated" in f for f in fails)


def test_complete_memory_row_passes_schema():
    fails = []
    _check_schema([_full_row()], fails)
    assert fails == []


def test_legacy_row_needs_no_memory_keys():
    from benchmarks.validate import EXPECTED_KEYS
    fails = []
    _check_schema([{k: 0 for k in EXPECTED_KEYS}], fails)
    assert fails == []


# --- Important 3: medians exclude teach and enforcement rows ---------------

def test_medians_skip_teach_and_enforcement_rows():
    rows = [_row(phase="teach"), _row(kind="enforcement", task_id="enf-commit"),
            _row()]
    kept = _median_rows(rows)
    assert kept == [_row()]


def test_medians_keep_every_legacy_row():
    legacy = [{"task_id": "t", "condition": "with", "kind": "convention"},
              {"task_id": "t", "condition": "without", "kind": "convention"}]
    assert _median_rows(legacy) == legacy


# --- Minor: no spurious coverage warnings for teach ids under a bare arm ---

def test_coverage_skips_teach_ids_for_arms_that_never_teach():
    rows = ([_row(task_id="teach-s1-p0", phase="teach", condition="with")]
            + [_row(task_id="sup-current", condition=c) for c in ("with", "without")])
    warns, rec = [], {}
    _check_coverage(rows, 1, warns, rec)
    assert warns == []
    assert "teach-s1-p0|without" not in rec["cell_counts"]
