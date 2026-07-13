"""Tests for the independent benchmark-validation layer.

Synthetic runs.jsonl fixtures are built row-by-row so each test isolates exactly
one integrity or anomaly path.
"""
import json
import subprocess
import sys
from pathlib import Path

from benchmarks.validate import validate, render_validation

REPO_ROOT = Path(__file__).resolve().parent.parent


def _row(**over):
    """A clean, self-consistent non-errored row; override any field via kwargs."""
    r = {
        "task_id": "t1", "kind": "convention", "chain": "", "step": 0,
        "condition": "without", "rep": 0, "model": "m1", "ts": 1000.0,
        "tokens_in": 100, "tokens_out": 200, "tokens_cache_read": 0,
        "tokens_cache_write": 0, "tokens_total": 300, "cost_usd": 0.01,
        "turns": 3, "duration_ms": 1000, "tool_calls": 2, "violations": 0,
        "rationale": 0.0, "success": True, "result_snippet": "did the work",
        "otel_tokens_total": 300, "otel_cost_usd": 0.01, "telemetry_ok": True,
        "error": "",
    }
    r.update(over)
    # keep the token identity intact unless the test deliberately breaks it
    if "tokens_total" not in over:
        r["tokens_total"] = (r["tokens_in"] + r["tokens_out"]
                             + r["tokens_cache_read"] + r["tokens_cache_write"])
    return r


def _write(tmp_path, rows, reps=1, model="m1"):
    (tmp_path / "runs.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n")
    (tmp_path / "campaign.json").write_text(json.dumps(
        {"model": model, "reps": reps, "seed": 0, "started_at": "now"}))
    return tmp_path


def _two_task_clean():
    """t1 (convention) + t2 (rationale), both conditions, numerically tied so no
    paired-direction warning fires; rationale differs so the both-extreme check
    stays quiet."""
    rows = []
    for cond in ("without", "with"):
        rows.append(_row(task_id="t1", kind="convention", condition=cond))
    rows.append(_row(task_id="t2", kind="rationale", condition="without", rationale=0.0))
    rows.append(_row(task_id="t2", kind="rationale", condition="with", rationale=1.0))
    return rows


def test_clean_campaign_ok(tmp_path):
    _write(tmp_path, _two_task_clean(), reps=1)
    v = validate(tmp_path)
    assert v["ok"] is True
    assert v["failures"] == []
    assert v["warnings"] == []


def test_token_sum_mismatch_fails(tmp_path):
    rows = _two_task_clean()
    rows[0]["tokens_total"] = 999  # break the identity
    _write(tmp_path, rows, reps=1)
    v = validate(tmp_path)
    assert v["ok"] is False
    assert any("tokens_total" in f for f in v["failures"])


def test_mixed_model_fails(tmp_path):
    rows = _two_task_clean()
    rows[1]["model"] = "m2"  # a second, contradictory model
    _write(tmp_path, rows, reps=1, model="m1")
    v = validate(tmp_path)
    assert v["ok"] is False
    assert any("model" in f for f in v["failures"])


def test_missing_cell_warns(tmp_path):
    # t1 both conditions, t2 only "without" -> (t2, with) cell is empty.
    rows = [
        _row(task_id="t1", condition="without"),
        _row(task_id="t1", condition="with"),
        _row(task_id="t2", condition="without"),
    ]
    _write(tmp_path, rows, reps=1)
    v = validate(tmp_path)
    assert v["ok"] is True  # short cell is a warning, not a failure
    assert any("cell" in w and "t2" in w for w in v["warnings"])


def test_zero_token_clean_row_warns(tmp_path):
    rows = _two_task_clean()
    # a non-errored row that reported zero tokens (parts still sum to zero -> schema ok)
    rows.append(_row(task_id="t3", kind="rationale", condition="without",
                     tokens_in=0, tokens_out=0, tokens_cache_read=0,
                     tokens_cache_write=0))
    rows.append(_row(task_id="t3", kind="rationale", condition="with", rationale=1.0))
    _write(tmp_path, rows, reps=1)
    v = validate(tmp_path)
    assert v["ok"] is True
    assert any("tokens_total == 0" in w for w in v["warnings"])


def test_both_zero_rationale_warns(tmp_path):
    rows = [
        _row(task_id="rat", kind="rationale", condition="without", rationale=0.0),
        _row(task_id="rat", kind="rationale", condition="with", rationale=0.0),
    ]
    _write(tmp_path, rows, reps=1)
    v = validate(tmp_path)
    assert any("rationale" in w and "injection" in w for w in v["warnings"])


def test_paired_wins_recomputed(tmp_path):
    # taskA: with(100) < without(200) -> with WIN
    # taskB: with(300) > without(200) -> with LOSS
    rows = [
        _row(task_id="A", condition="without", tokens_in=200, tokens_out=0),
        _row(task_id="A", condition="with", tokens_in=100, tokens_out=0),
        _row(task_id="B", condition="without", tokens_in=200, tokens_out=0),
        _row(task_id="B", condition="with", tokens_in=300, tokens_out=0),
    ]
    _write(tmp_path, rows, reps=1)
    v = validate(tmp_path)
    p = v["recomputed"]["paired"]["with_vs_without"]["tokens_total"]
    assert p["wins"] == 1
    assert p["losses"] == 1
    assert p["ties"] == 0


def test_three_conditions_medians_and_all_pairs(tmp_path):
    # one task, three conditions: medians recomputed per condition and every
    # condition pair gets its own paired win/loss table.
    rows = [
        _row(task_id="A", condition="without", tokens_in=300, tokens_out=0),
        _row(task_id="A", condition="claudemd", tokens_in=200, tokens_out=0),
        _row(task_id="A", condition="with", tokens_in=100, tokens_out=0),
    ]
    _write(tmp_path, rows, reps=1)
    v = validate(tmp_path)
    meds = v["recomputed"]["medians"]
    assert meds["without"]["tokens_total"] == 300
    assert meds["claudemd"]["tokens_total"] == 200
    assert meds["with"]["tokens_total"] == 100
    paired = v["recomputed"]["paired"]
    assert set(paired) == {"with_vs_without", "with_vs_claudemd", "claudemd_vs_without"}
    assert paired["with_vs_claudemd"]["tokens_total"]["wins"] == 1
    assert paired["claudemd_vs_without"]["tokens_total"]["wins"] == 1
    md = render_validation(v)
    assert "| metric | without | claudemd | with |" in md
    assert "Paired win/loss/tie (with vs claudemd" in md


def test_claudemd_with_pair_recomputed(tmp_path):
    # condition D (claudemd_with) is paired against claudemd — the adoption
    # question: contexer's marginal value on an already-documented repo.
    rows = [
        _row(task_id="A", condition="claudemd", tokens_in=200, tokens_out=0),
        _row(task_id="A", condition="claudemd_with", tokens_in=100, tokens_out=0),
    ]
    _write(tmp_path, rows, reps=1)
    v = validate(tmp_path)
    p = v["recomputed"]["paired"]["claudemd_with_vs_claudemd"]["tokens_total"]
    assert p["wins"] == 1 and p["losses"] == 0
    assert list(v["recomputed"]["medians"]) == ["claudemd", "claudemd_with"]


def test_contiguous_condition_block_warns_not_interleaved(tmp_path):
    # campaign3's exact smell: every "without" row earlier than every "with" row.
    rows = [
        _row(task_id="A", condition="without", ts=1.0),
        _row(task_id="B", condition="without", ts=2.0),
        _row(task_id="A", condition="with", ts=3.0),
        _row(task_id="B", condition="with", ts=4.0),
    ]
    _write(tmp_path, rows, reps=1)
    v = validate(tmp_path)
    assert any("not" in w and "interleaved" in w for w in v["warnings"])


def test_interleaved_conditions_do_not_warn(tmp_path):
    rows = [
        _row(task_id="A", condition="without", ts=1.0),
        _row(task_id="A", condition="with", ts=2.0),
        _row(task_id="B", condition="without", ts=3.0),
        _row(task_id="B", condition="with", ts=4.0),
    ]
    _write(tmp_path, rows, reps=1)
    v = validate(tmp_path)
    assert not any("interleav" in w for w in v["warnings"])


def test_missing_ts_skips_interleaving_with_warning(tmp_path):
    rows = _two_task_clean()
    for r in rows:
        r.pop("ts")
    _write(tmp_path, rows, reps=1)
    v = validate(tmp_path)
    assert v["ok"] is True  # old artifacts stay valid
    assert any("ts" in w and "skipped" in w for w in v["warnings"])


def test_cli_exit_code(tmp_path):
    rows = _two_task_clean()
    rows[0]["tokens_total"] = 12345  # guaranteed schema failure
    _write(tmp_path, rows, reps=1)
    proc = subprocess.run(
        [sys.executable, "-m", "benchmarks.validate", str(tmp_path)],
        cwd=REPO_ROOT, capture_output=True, text=True)
    assert proc.returncode == 1
    assert "FAIL" in proc.stdout


def test_render_is_markdown(tmp_path):
    _write(tmp_path, _two_task_clean(), reps=1)
    md = render_validation(validate(tmp_path))
    assert md.startswith("## Independent Validation")
    assert "Recomputed medians" in md
