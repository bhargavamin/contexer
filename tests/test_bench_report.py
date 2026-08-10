"""A/B report: medians, deltas, chain curves, telemetry agreement, model guard."""
import json

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


def _mrow(task_id, arm, tier="implicit", rep=0, phase="measure", success=True,
         tokens=1000, error="", capture=None, model="m1", sup_result=""):
    return {"task_id": task_id, "kind": "x", "arm": arm, "condition": arm, "tier": tier,
            "phase": phase, "rep": rep, "model": model, "tokens_total": tokens,
            "success": success, "error": error, "capture": capture or {},
            "sup_result": sup_result, "contaminated": False}


class TestMemoryCampaignSection:
    def test_memory_section_renders_wilson_and_isolates_enf_commit(self, tmp_path):
        rows = [
            # first measure row per (arm, rep) carries the post-teaching snapshot
            _mrow("sup-current", "with", "implicit", rep=0, success=True,
                 capture={"memory_files": 0, "contexer_entries": 3}),
            _mrow("sup-current", "memory", "implicit", rep=0, success=True,
                 capture={"memory_files": 2, "contexer_entries": 0}),
            _mrow("sup-current", "without", "implicit", rep=0, success=False),
            _mrow("cont-log", "with", "implicit", rep=0, success=True),
            _mrow("teach-s0", "with", "implicit", rep=0, phase="teach", tokens=500),
            _mrow("teach-s1", "memory", "implicit", rep=0, phase="teach", tokens=800),
            _mrow("enf-commit", "with", "implicit", rep=0, success=True),
            _mrow("enf-commit", "memory", "implicit", rep=0, success=False),
            {**_mrow("sup-current", "with", "explicit", rep=2, success=False), "error": "timeout"},
        ]
        md = render(_write(tmp_path, rows))
        assert "Memory-vs-Contexer" in md
        assert "1/1 (" in md  # a rendered wilson-interval cell
        assert "## Capture rate" in md
        assert "| with | implicit | 1 | 0 | 3 |" in md
        assert "| memory | implicit | 1 | 2 | 0 |" in md
        assert "## Mechanism demonstration" in md
        heading_idx = md.index("## Mechanism demonstration")
        assert "enf-commit" not in md[:heading_idx]
        assert "blocked" in md
        assert "no mechanism" in md
        assert "1 errored run(s) excluded" in md

    def test_legacy_campaign_unaffected(self, tmp_path):
        rows = [_row("without", 9000), _row("with", 4000)]
        md = render(_write(tmp_path, rows))
        assert "Memory-vs-Contexer" not in md
        assert "Mechanism demonstration" not in md

    def test_pooled_headline_cell_combines_both_tiers(self, tmp_path):
        """Important 1: tier derives from rep parity, so a 16-rep campaign gives
        n=8 per tier. The pooled cell (n = both tiers) carries the headline."""
        rows = [_mrow("sup-current", "with", "implicit", rep=0, success=True,
                     sup_result="pass"),
                _mrow("sup-current", "with", "explicit", rep=1, success=True,
                     sup_result="pass")]
        md = render(_write(tmp_path, rows))
        assert "| arm | implicit | explicit | pooled (headline) |" in md
        line = next(l for l in md.splitlines() if l.startswith("| with |"))
        assert line.count("1/1 (") == 2      # the two per-tier cells
        assert "2/2 (" in line               # the pooled cell
        assert "pre-registered headline number" in md

    def test_reviews_are_shown_and_left_out_of_the_denominator(self, tmp_path):
        """Important 2: a review is "not yet scored", not a loss — counting it in n
        would flatter Contexer, since hedged answers are what the memory arm
        (holding two contradictory statements) produces."""
        rows = [_mrow("sup-current", "memory", "implicit", rep=0, success=True,
                     sup_result="pass"),
                _mrow("sup-current", "memory", "implicit", rep=2, success=False,
                     sup_result="review")]
        md = render(_write(tmp_path, rows))
        line = next(l for l in md.splitlines() if l.startswith("| memory |"))
        assert "1/1 (" in line and "1 review" in line
        assert "1/2" not in line and "0/1" not in line

    def test_enforcement_outcome_distinguishes_a_declined_edit(self, tmp_path):
        rows = [{**_mrow("enf-commit", "with", success=True),
                 "enf_outcome": "no violating change attempted"}]
        md = render(_write(tmp_path, rows))
        assert "no violating change attempted" in md

    def test_mixed_legacy_and_memory_rows_do_not_crash(self, tmp_path):
        """Minor: render dispatches on any("arm" in r), so the memory renderer must
        tolerate a legacy row that carries no "arm"."""
        rows = [_mrow("sup-current", "with", success=True), _row("without")]
        assert "Memory-vs-Contexer" in render(_write(tmp_path, rows))
