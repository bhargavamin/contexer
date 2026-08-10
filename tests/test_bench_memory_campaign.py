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


# Self-deleting stub: succeeds its first 2 calls, then unlinks itself (still emits
# valid JSON on the call that deletes it — POSIX doesn't invalidate an open fd on
# unlink) so every LATER invocation hits FileNotFoundError, a real crash a naive
# per-run body would propagate straight out of the campaign loop.
_CRASH_STUB = """#!/bin/bash
d="$(dirname "$0")"
n=$(( $(cat "$d/count" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$d/count"
if [ "$n" -ge 3 ]; then
  rm -- "$0"
fi
echo '{"result": "ok", "usage": {"input_tokens": 1, "output_tokens": 1}, "num_turns": 1, "total_cost_usd": 0.0, "duration_ms": 1}'
"""

def test_campaign_survives_a_crashing_session(tmp_path):
    p = tmp_path / "claude_crashy"; p.write_text(_CRASH_STUB); p.chmod(p.stat().st_mode | stat.S_IEXEC)

    out = run_memory_campaign(tmp_path / "out2", reps=1, claude_cmd=str(p),
                              model="stub", conditions=("without", "memory"))
    rows = [json.loads(l) for l in out.read_text().splitlines()]
    # without: 4 measured calls; memory: 2 teach + 4 measured calls = 10 total (the
    # campaign.json claude_version probe eats one of the stub's 3 healthy calls
    # first, so the exact split is an implementation detail — the property under
    # test is: full completion, some rows crash, some don't, nothing aborts).
    assert len(rows) == 10
    errored = sum(1 for r in rows if r["error"])
    assert 0 < errored < 10
