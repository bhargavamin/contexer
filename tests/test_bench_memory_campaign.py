import json, os, stat
from pathlib import Path
from benchmarks.memory_campaign import run_memory_campaign

STUB = """#!/bin/bash
echo '{"result": "DynamoDB\\nscaling", "usage": {"input_tokens": 10, "output_tokens": 5}, "num_turns": 1, "total_cost_usd": 0.001, "duration_ms": 5}'
"""

def _stub(tmp_path):
    p = tmp_path / "claude"; p.write_text(STUB); p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)

def test_campaign_writes_rows_with_new_fields(tmp_path):
    out = run_memory_campaign(tmp_path / "out", reps=1, claude_cmd=_stub(tmp_path),
                              model="stub", conditions=("without", "memory", "with"))
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    arms = {r["arm"] for r in rows}
    assert arms == {"without", "memory", "with"}
    assert any(r["phase"] == "teach" for r in rows)       # teaching rows recorded
    assert all(r["phase"] == "measure" or r["arm"] != "without" for r in rows)  # bare never teaches
    sup = [r for r in rows if r["task_id"] == "sup-current" and r["phase"] == "measure"]
    assert all(r["sup_result"] in ("pass", "fail", "review") for r in sup)
    assert all("tier" in r and "capture" in r and "contaminated" in r for r in rows)
