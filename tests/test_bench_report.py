"""A/B report: medians, deltas, chain curves, telemetry agreement, model guard."""
import json
from pathlib import Path

import pytest

from benchmarks.report import render


def _row(condition, tokens=10000, violations=3, chain="", step=0, model="m1",
         telemetry_ok=True, error="", kind="convention", rationale=1.0):
    return {"task_id": "t", "kind": kind, "chain": chain, "step": step,
            "condition": condition, "rep": 0, "model": model,
            "tokens_in": tokens, "tokens_out": 0, "tokens_cache_read": 0,
            "tokens_cache_write": 0, "tokens_total": tokens, "cost_usd": 0.01,
            "turns": 3, "duration_ms": 1000, "tool_calls": 5, "violations": violations,
            "rationale": rationale, "success": True, "otel_tokens_total": tokens,
            "otel_cost_usd": 0.01, "telemetry_ok": telemetry_ok, "error": error}


def _write(tmp_path, rows):
    p = tmp_path / "runs.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return p


class TestRender:
    def test_medians_delta_and_telemetry_line(self, tmp_path):
        rows = [_row("without", 9000, 3), _row("without", 11000, 5),
                _row("with", 4000, 0), _row("with", 6000, 1, telemetry_ok=None)]
        md = render(_write(tmp_path, rows))
        assert "| tokens_total | 10000 | 5000 | -5000 | -50.0% |" in md
        assert "telemetry agreement: 3/3" in md  # None rows aren't "checked"
        assert "m1" in md

    def test_chain_section(self, tmp_path):
        rows = [_row("without", 8000, 2, chain="orders", step=1),
                _row("without", 8000, 2, chain="orders", step=2),
                _row("with", 8000, 1, chain="orders", step=1),
                _row("with", 4000, 0, chain="orders", step=2)]
        md = render(_write(tmp_path, rows))
        assert "## Chain: orders" in md
        assert "| 2 | 8000 | 2 | 4000 | 0 |" in md  # step | A tokens | A viol | B tokens | B viol

    def test_error_rows_counted_not_aggregated(self, tmp_path):
        rows = [_row("without"), {**_row("with"), "error": "timeout"}]
        md = render(_write(tmp_path, rows))
        assert "1 errored run(s) excluded" in md

    def test_mixed_models_refused(self, tmp_path):
        rows = [_row("without", model="m1"), _row("with", model="m2")]
        with pytest.raises(ValueError, match="[Mm]ixed model"):
            render(_write(tmp_path, rows))


class TestThreeConditions:
    def test_main_table_has_column_per_condition_and_both_deltas(self, tmp_path):
        rows = [_row("without", 10000), _row("claudemd", 8000), _row("with", 5000)]
        md = render(_write(tmp_path, rows))
        assert ("| metric | without | claudemd | with | Δ (with−without) | "
                "Δ% (with−without) | Δ (with−claudemd) | Δ% (with−claudemd) |") in md
        assert "| tokens_total | 10000 | 8000 | 5000 | -5000 | -50.0% | -3000 | -37.5% |" in md
        assert "1 without / 1 claudemd / 1 with" in md

    def test_unknown_condition_appended_gracefully(self, tmp_path):
        rows = [_row("without", 10000), _row("with", 5000), _row("mystery", 7000)]
        md = render(_write(tmp_path, rows))
        assert ("| metric | without | with | mystery | Δ (with−without) | "
                "Δ% (with−without) |") in md
        assert "| tokens_total | 10000 | 5000 | 7000 | -5000 | -50.0% |" in md

    def test_chain_section_column_pair_per_condition(self, tmp_path):
        rows = [_row("without", 8000, 2, chain="orders", step=1),
                _row("claudemd", 6000, 1, chain="orders", step=1),
                _row("with", 4000, 0, chain="orders", step=1)]
        md = render(_write(tmp_path, rows))
        assert ("| step | without tokens | without violations | claudemd tokens | "
                "claudemd violations | with tokens | with violations |") in md
        assert "| 1 | 8000 | 2 | 6000 | 1 | 4000 | 0 |" in md

    def test_claudemd_with_column_and_delta_vs_claudemd(self, tmp_path):
        rows = [_row("without", 10000), _row("claudemd", 8000),
                _row("with", 5000), _row("claudemd_with", 6000)]
        md = render(_write(tmp_path, rows))
        assert ("| metric | without | claudemd | with | claudemd_with | "
                "Δ (with−without) | Δ% (with−without) | "
                "Δ (with−claudemd) | Δ% (with−claudemd) | "
                "Δ (claudemd_with−claudemd) | Δ% (claudemd_with−claudemd) |") in md
        assert ("| tokens_total | 10000 | 8000 | 5000 | 6000 | -5000 | -50.0% "
                "| -3000 | -37.5% | -2000 | -25.0% |") in md

    def test_rationale_zero_footnote(self, tmp_path):
        rows = [_row("without", kind="rationale", rationale=0.0),
                _row("claudemd", kind="rationale", rationale=1.0),
                _row("with", kind="rationale", rationale=1.0)]
        md = render(_write(tmp_path, rows))
        assert "rationale 0.0 can mean the information was unavailable" in md

    def test_no_footnote_when_no_condition_scores_zero(self, tmp_path):
        rows = [_row("without", kind="rationale", rationale=0.5),
                _row("with", kind="rationale", rationale=1.0)]
        md = render(_write(tmp_path, rows))
        assert "rationale 0.0 can mean" not in md
