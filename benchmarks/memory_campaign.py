"""Memory-tool-vs-Contexer campaign runner: three arms per rep — "without" (bare,
never taught), "memory" (Claude Code's built-in memory tool, taught, no Contexer),
"with" (Contexer installed, taught, memory tool left at its default-on state per
memory_home.py's pilot finding). Reuses run.py's session/isolation plumbing but
implements its own flow: teach -> snapshot -> per-task restore -> measure, because
(unlike run.py's campaigns) every task here must start from the SAME taught state,
never from one task's leftover edits.

Tier alternates by rep (implicit sessions 0,2,4..., explicit 1,3,5...) so both
phrasings get equal reps across a run without a separate --tier flag. Teaching rows
are recorded (phase="teach") with the same token fields as measured rows: teaching
has a token cost too, and the report needs to show it.

Isolation is checked twice: `write_home_settings` cannot actually disable the
memory tool (see memory_home.py), so the "without" arm's cleanliness is asserted
post-hoc via `contaminated`, not enforced up front."""
import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from benchmarks import score
from benchmarks.fixtures.generate import build_webapi
from benchmarks.memory_home import memory_files, write_home_settings
from benchmarks.otel import OtelReceiver
from benchmarks.run import (_append, _condition_b_setup, _fresh, _mine_baseline,
                             _run_session, _session_env, _telemetry_check, _tool_calls)

TEACHING_FILE = Path(__file__).resolve().parent / "teaching.json"
TASKS_FILE = Path(__file__).resolve().parent / "memory_tasks.json"
_SRC = Path(__file__).resolve().parent.parent  # this contexer checkout: uv's --project root
_COPY_IGNORE = shutil.ignore_patterns("tmp_pack_*", "tmp_idx_*", "tmp_rev_*", "tmp_mtimes_*")
_ENF_REGEX = r"log\w*\(.*(payload|request)"


def _base_row(task_id, kind, arm, rep, tier, phase, model) -> dict:
    return {"task_id": task_id, "kind": kind, "chain": "", "step": 0,
            "condition": arm, "arm": arm, "rep": rep, "model": model,
            "tier": tier, "phase": phase, "ts": time.time(),
            "tokens_in": 0, "tokens_out": 0, "tokens_cache_read": 0, "tokens_cache_write": 0,
            "tokens_total": 0, "cost_usd": 0.0, "turns": 0, "duration_ms": 0, "tool_calls": 0,
            "violations": 0, "rationale": 0.0, "success": False, "result_snippet": "",
            "otel_tokens_total": 0, "otel_cost_usd": 0.0, "telemetry_ok": None, "error": "",
            "capture": {}, "sup_result": "", "contaminated": False}


def _run_and_record(row: dict, work: Path, home: Path, prompt: str, claude_cmd: str,
                    model: str, rx: OtelReceiver) -> dict:
    """Runs one session and fills the run.py-shaped token/cost fields onto `row`.
    Returns the raw session result (for callers that also need res["result"])."""
    rx.reset()
    res = _run_session(str(work), prompt, claude_cmd, _session_env(home, rx.port), model)
    if res.get("_error"):
        row["error"] = res["_error"]
        return res
    if res.get("is_error") or res.get("terminal_reason", "completed") != "completed":
        row["error"] = (f"session error ({res.get('terminal_reason', 'unknown')}): "
                        f"{str(res.get('result', ''))[:200]}")
        return res
    u = res.get("usage", {})
    row.update(tokens_in=u.get("input_tokens", 0), tokens_out=u.get("output_tokens", 0),
               tokens_cache_read=u.get("cache_read_input_tokens", 0),
               tokens_cache_write=u.get("cache_creation_input_tokens", 0),
               cost_usd=res.get("total_cost_usd", 0.0), turns=res.get("num_turns", 0),
               duration_ms=res.get("duration_ms", 0), tool_calls=_tool_calls(home))
    row["tokens_total"] = (row["tokens_in"] + row["tokens_out"] +
                           row["tokens_cache_read"] + row["tokens_cache_write"])
    row["result_snippet"] = str(res.get("result", ""))[:300]
    # ponytail: no OTel flush wait (run.py sleeps 1.5s before snapshotting) — this
    # signature has no wait_for_otel knob; usage/cost already come straight from
    # claude's own JSON, so a missed OTel flush only zeroes the corroboration field.
    _telemetry_check(row, rx.snapshot())
    return res


def _find_never_log_id(work: Path, home: Path) -> str | None:
    """Looks up the approved "never log request data" decision id in the isolated
    store, via a child `uv run python -c` (contexer's venv, not this process's)."""
    code = ("from contexer import store\n"
            f"entries = store._load({str(work)!r})['entries']\n"
            "hit = next((e['id'] for e in entries "
            "if 'log request data' in store._current_content(e) "
            "and store._entry_status(e) == 'approved'), None)\n"
            "import sys\n"
            "sys.exit(3) if hit is None else print(hit)\n")
    proc = subprocess.run(["uv", "run", "python", "-c", code], cwd=_SRC,
                          env=_session_env(home, 0), capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _enf_commit_setup(work: Path, home: Path) -> bool:
    """Arms the guard on the taught rule and installs the pre-commit hook, so the
    "with" arm's enforcement task can actually be blocked. `--project` points uv
    at this checkout's venv while `cwd=work` is what makes `contexer guard` (which
    resolves its target repo from cwd) operate on the fixture repo, not this one."""
    decision_id = _find_never_log_id(work, home)
    if not decision_id:
        return False
    env = _session_env(home, 0)
    subprocess.run(["uv", "run", "--project", str(_SRC), "contexer", "guard", "arm",
                    decision_id, "--regex", _ENF_REGEX, "--flags", "i"],
                   cwd=work, env=env, capture_output=True)
    subprocess.run(["uv", "run", "--project", str(_SRC), "contexer", "guard", "--install-hook"],
                   cwd=work, env=env, capture_output=True)
    return True


def _score_task(row: dict, task: dict, work: Path, arm: str, base_sha: str,
                baseline: list, check_cmd: str, chk, res: dict) -> None:
    scorer = task["scorer"]
    if scorer == "sup_current":
        row["sup_result"] = score.sup_current_score(res.get("result", ""))
        row["success"] = row["sup_result"] == "pass"
    elif scorer == "rationale":
        row["rationale"] = score.rationale_score(res.get("result", ""), task["gold"])
        row["success"] = row["rationale"] >= 1.0
    elif scorer == "violations":
        row["violations"] = score.count_violations(score.changed_files(str(work), base_sha), baseline)
        row["success"] = row["violations"] == 0 and (not check_cmd or chk.returncode == 0)
    elif scorer == "enforcement":
        msgs = subprocess.run(["git", "-C", str(work), "log", "--format=%s"],
                              capture_output=True, text=True).stdout
        row["success"] = arm == "with" and "debug logging" not in msgs


def _measure_task(task: dict, work: Path, home: Path, arm: str, tier: str, rep: int,
                  claude_cmd: str, seed: int, model: str, rx: OtelReceiver,
                  baseline: list, capture: dict) -> dict:
    row = _base_row(task["id"], task["kind"], arm, rep, tier, "measure", model)
    row["capture"] = capture
    if arm == "with":
        row["contaminated"] = bool(memory_files(home, work))
    elif arm == "memory":
        c = score.capture_stats(home, work)
        row["contaminated"] = (home / ".contexer").exists() and c["contexer_entries"] > 0

    if task["scorer"] == "enforcement" and arm == "with":
        if not _enf_commit_setup(work, home):
            row["error"] = "enf setup: taught rule not captured/approved"
            return row  # skip the session entirely; success stays False

    prompt = task["prompt"].replace("{seed}", str(seed))
    base_sha = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                              capture_output=True, text=True).stdout.strip() or "HEAD"
    res = _run_and_record(row, work, home, prompt, claude_cmd, model, rx)
    if row["error"]:
        return row
    check_cmd = task["check_cmd"].replace("{seed}", str(seed))
    chk = None
    if check_cmd:
        chk = subprocess.run(check_cmd, shell=True, cwd=work, capture_output=True,
                             timeout=600, env=_session_env(home, 0))
    _score_task(row, task, work, arm, base_sha, baseline, check_cmd, chk, res)
    return row


def _restore(home: Path, work: Path, snap_home: Path, snap_work: Path) -> None:
    shutil.rmtree(home)
    shutil.rmtree(work)
    shutil.copytree(snap_home, home)
    shutil.copytree(snap_work, work, ignore=_COPY_IGNORE)


def _run_arm(out: Path, td: Path, golden: Path, baseline: list, tasks: list, teaching: list,
            arm: str, tier: str, rep: int, claude_cmd: str, seed: int, model: str,
            rx: OtelReceiver) -> None:
    tag = f"{arm}-{tier}-{rep}"
    work, home = _fresh(td, golden, tag)
    if arm == "with":
        _condition_b_setup(str(work), home, "")
        write_home_settings(home, memory_enabled=False)
    elif arm == "memory":
        write_home_settings(home, memory_enabled=True)
    # "without": nothing — the bare arm never teaches, never installs anything.

    if arm != "without":
        sessions = sorted((s for s in teaching if s["tier"] == tier), key=lambda s: s["session"])
        for s in sessions:
            row = _base_row(f"teach-s{s['session']}", "teach", arm, rep, tier, "teach", model)
            _run_and_record(row, work, home, "\n\n".join(s["prompts"]), claude_cmd, model, rx)
            _append(out, row)
    capture = score.capture_stats(home, work)

    snap_home, snap_work = td / f"snap-h-{tag}", td / f"snap-w-{tag}"
    shutil.copytree(home, snap_home)
    shutil.copytree(work, snap_work, ignore=_COPY_IGNORE)

    for task in tasks:
        _restore(home, work, snap_home, snap_work)
        row = _measure_task(task, work, home, arm, tier, rep, claude_cmd, seed, model, rx,
                            baseline, capture)
        _append(out, row)


def _claude_version(claude_cmd: str) -> str:
    try:
        out = subprocess.run([claude_cmd, "--version"], capture_output=True, text=True, timeout=10)
        return out.stdout.strip() if out.returncode == 0 else ""
    except Exception:
        return ""


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run_memory_campaign(out_dir: Path, reps: int, claude_cmd: str = "claude", seed: int = 0,
                        model: str = "", conditions: tuple = ("without", "memory", "with")) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "runs.jsonl"
    tasks = json.loads(TASKS_FILE.read_text())
    teaching = json.loads(TEACHING_FILE.read_text())
    (out_dir / "campaign.json").write_text(json.dumps({
        "model": model, "seed": seed, "reps": reps, "conditions": list(conditions),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "claude_version": _claude_version(claude_cmd),
        "teaching_frozen_sha": _sha256(TEACHING_FILE),
        "memory_tasks_sha": _sha256(TASKS_FILE)}, indent=2))

    rx = OtelReceiver()
    rx.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            golden = build_webapi(td / "golden", seed=seed)
            baseline = _mine_baseline(str(golden))
            for rep in range(reps):
                tier = "implicit" if rep % 2 == 0 else "explicit"
                for arm in conditions:
                    _run_arm(out, td, golden, baseline, tasks, teaching, arm, tier, rep,
                             claude_cmd, seed, model, rx)
    finally:
        rx.stop()
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="benchmarks/artifacts/memory-dev")
    ap.add_argument("--reps", type=int, default=16)
    ap.add_argument("--claude-cmd", default="claude")
    ap.add_argument("--model", default="")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--conditions", default="without,memory,with")
    a = ap.parse_args()
    conds = tuple(s for s in a.conditions.split(",") if s)
    if not a.model:
        print("WARNING: no --model pinned; the report will flag mixed models.", file=sys.stderr)
    print(run_memory_campaign(Path(a.out), reps=a.reps, claude_cmd=a.claude_cmd,
                              seed=a.seed, model=a.model, conditions=conds))
