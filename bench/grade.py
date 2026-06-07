#!/usr/bin/env python3
"""Blind LLM-judge grader.

Reads results/runs.jsonl, scores each candidate against its rubric with `claude -p --json-schema`,
and writes results/graded.jsonl. The judge is BLIND to which arm produced the output (the arm field
is never shown). Runs a panel of N judges per candidate and aggregates (median scores, majority
conflict flag). The judge runs in the same clean isolation as the cold arm (no Contexer).

Pipeline: run_bench.py -> runs.jsonl -> grade.py -> graded.jsonl -> analyze.py
"""
import argparse
import json
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BENCH_DIR / "results"

SCHEMA = {
    "type": "object",
    "properties": {
        "decision_adherence": {"type": "number"},
        "task_correctness": {"type": "number"},
        "conflict_with_stored": {"type": "boolean"},
        "notes": {"type": "string"},
    },
    "required": ["decision_adherence", "task_correctness", "conflict_with_stored", "notes"],
    "additionalProperties": False,
}


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def build_prompt(rec: dict) -> str:
    decisions = "\n".join(f"  - {d}" for d in rec["seed_decisions"]) or "  (none)"
    return f"""You are grading a coding assistant's output. Be strict and objective. You do NOT know \
which system produced this output; judge only what is shown.

## Project decisions on record (ground truth the assistant was expected to honor)
{decisions}

## Task given to the assistant
{rec['prompt']}

## Grading rubric
{rec['rubric']}

## The assistant's final message
{rec['result'][:4000]}

## The diff it produced (if any)
{rec['diff'][:4000] or '(no file changes)'}

Score each dimension from 0.0 to 1.0:
- decision_adherence: did the output follow the decisions on record above?
- task_correctness: did it correctly accomplish the task?
- conflict_with_stored: true if the output actively contradicts a decision on record.
Return JSON only."""


def judge_once(prompt: str, model: str, cwd: str) -> dict | None:
    cmd = ["claude", "-p", "--output-format", "json", "--json-schema", json.dumps(SCHEMA),
           "--model", model, "--setting-sources", "project", "--strict-mcp-config",
           "--mcp-config", '{"mcpServers":{}}', "--permission-mode", "acceptEdits"]
    proc = subprocess.run(cmd, input=prompt, cwd=cwd, capture_output=True, text=True)
    try:
        res = json.loads(proc.stdout.strip())
    except Exception:
        return None
    if res.get("is_error"):
        return None
    out = res.get("structured_output")
    if out is None:
        try:
            out = json.loads(res.get("result", ""))
        except Exception:
            return None
    return out


def aggregate(grades: list) -> dict:
    if not grades:
        return {"decision_adherence": None, "task_correctness": None,
                "conflict_with_stored": None, "n": 0}
    da = [g["decision_adherence"] for g in grades]
    tc = [g["task_correctness"] for g in grades]
    cf = [bool(g["conflict_with_stored"]) for g in grades]
    return {
        "decision_adherence": round(statistics.median(da), 3),
        "task_correctness": round(statistics.median(tc), 3),
        "conflict_with_stored": sum(cf) > len(cf) / 2,  # majority
        "n": len(grades),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=str(RESULTS_DIR / "runs.jsonl"))
    ap.add_argument("--out", default=str(RESULTS_DIR / "graded.jsonl"))
    ap.add_argument("--model", default="claude-haiku-4-5")
    ap.add_argument("--panel", type=int, default=1, help="judges per candidate (use 3 for real runs)")
    args = ap.parse_args()

    records = [json.loads(line) for line in Path(args.inp).read_text().splitlines() if line.strip()]
    cwd = tempfile.mkdtemp(prefix="contexer-judge-")
    log(f"Grading {len(records)} run(s), panel={args.panel}, judge={args.model}")

    with Path(args.out).open("w") as f:
        for i, rec in enumerate(records, 1):
            if rec.get("is_error"):
                rec["grades"] = []
                rec["grade_agg"] = aggregate([])
                f.write(json.dumps(rec) + "\n")
                log(f"[{i}/{len(records)}] {rec['task_id']}/{rec['arm']} — run errored, skipped grading")
                continue
            prompt = build_prompt(rec)
            grades = [g for g in (judge_once(prompt, args.model, cwd) for _ in range(args.panel)) if g]
            rec["grades"] = grades
            rec["grade_agg"] = aggregate(grades)
            f.write(json.dumps(rec) + "\n")
            f.flush()
            agg = rec["grade_agg"]
            log(f"[{i}/{len(records)}] {rec['task_id']}/{rec['arm']} — "
                f"adherence={agg['decision_adherence']} correct={agg['task_correctness']} "
                f"conflict={agg['conflict_with_stored']}")
    log(f"\nWrote graded results to {args.out}")


if __name__ == "__main__":
    main()
