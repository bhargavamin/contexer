"""A/B/C campaign runner: same tasks under three conditions — "without" (bare),
"claudemd" (a static CLAUDE.md carrying the same knowledge, the honest
competitor), and "with" (contexer install + bootstrap + seed). Live sessions go
through the `claude` CLI; tests inject a stub binary via claude_cmd.

Conditions are INTERLEAVED in time (rep outermost, condition innermost) and every
row carries a ``ts`` epoch stamp, so server-side drift / cache warming can never
be confounded with condition (red-team campaign3, challenge #2).

Isolation: every run gets a throwaway HOME (chains share one per condition x rep)
and a fresh copy of the fixture repo; sessions receive an env ALLOWLIST (never
os.environ passthrough) so CLAUDE_CONFIG_DIR / XDG_CONFIG_HOME cannot leak the
developer's real config; the model is pinned per campaign; an embedded OTLP
receiver independently re-measures tokens/cost per run (telemetry_ok)."""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from benchmarks import score
from benchmarks.fixtures.generate import build_webapi
from benchmarks.otel import OtelReceiver

TASKS_FILE = Path(__file__).resolve().parent / "tasks.json"
_ALLOWED_ENV = ("PATH", "ANTHROPIC_API_KEY", "TERM", "LANG", "LC_ALL",
                "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM")
_MANAGED_SETTINGS = Path("/Library/Application Support/ClaudeCode/managed-settings.json")
_TELEMETRY_TOLERANCE = 0.05


def _session_env(home: Path, otel_port: int) -> dict:
    env = {k: os.environ[k] for k in _ALLOWED_ENV if k in os.environ}
    env["HOME"] = str(home)
    if otel_port:
        env.update(CLAUDE_CODE_ENABLE_TELEMETRY="1",
                   OTEL_METRICS_EXPORTER="otlp",
                   OTEL_EXPORTER_OTLP_PROTOCOL="http/json",
                   OTEL_EXPORTER_OTLP_ENDPOINT=f"http://127.0.0.1:{otel_port}",
                   OTEL_METRIC_EXPORT_INTERVAL="1000")
    return env


def _load_tasks(task_ids):
    tasks = json.loads(TASKS_FILE.read_text())
    if task_ids is None:
        # Paraphrase variants (prompt-sensitivity probes) never run by default —
        # they'd double-count their base task in campaign aggregates.
        picked = [t for t in tasks if not t.get("variant_of")]
    else:
        picked = [t for t in tasks if t["id"] in task_ids]
    return sorted(picked, key=lambda t: (t["chain"], t["step"]))


def _condition_b_setup(repo: str, home: Path, seed_decision: str) -> None:
    """contexer install + bootstrap + optional decision seed, in a child process
    whose HOME is the isolated one (store paths must resolve inside it)."""
    env = _session_env(home, otel_port=0)
    subprocess.run(["uv", "run", "contexer", "install"], env=env, check=True,
                   capture_output=True, cwd=Path(__file__).resolve().parent.parent)
    code = f"from contexer import store\nstore.bootstrap_apply({repo!r}, 'bench-seed')\n"
    if seed_decision:
        code += (f"store.update_decision({repo!r}, {seed_decision!r}, 'bench-seed', "
                 "'constraint', created_by='human')\n")
    subprocess.run(["uv", "run", "python", "-c", code], env=env, check=True,
                   capture_output=True, cwd=Path(__file__).resolve().parent.parent)


def _condition_c_setup(work: Path, seed_decision: str,
                       filenames: tuple = ("CLAUDE.md",)) -> None:
    """The honest competitor: NO contexer — static rules file(s) in the work repo
    carrying the same knowledge condition "with" receives. Content mimics how these
    files are commonly written in the wild: CLAUDE.md as project overview + commands
    + key decisions (Anthropic's recommended shape), AGENTS.md as agent working
    conventions + testing + rules (the agents.md standard shape). With BOTH files,
    knowledge splits realistically — rule-shaped seeds ("Never/Always ...") go to
    AGENTS.md, decision-shaped seeds to CLAUDE.md; a single file carries everything
    so single-file conditions stay comparable. For chains the file(s) are written
    once before step 1 and never updated between steps: a static file cannot
    capture mid-session decisions, and that asymmetry IS the thing measured."""
    from contexer import miner
    convs = [c["content"] for c in miner.mine_conventions(str(work))]
    is_rule = bool(seed_decision) and seed_decision.lower().startswith(
        ("never", "always", "don't", "do not"))

    overview = [
        "# Project: record service", "",
        "FastAPI-style record service (Python, managed with uv).", "",
        "## Commands", "", "```bash",
        "uv sync",
        "uv run pytest tests/ -q",
        "```", "",
        "## Architecture", "",
        "- `app/` — service modules",
        "- `tests/` — pytest suite, plain asserts", "",
    ]
    decisions = ["## Key decisions", "", f"- {seed_decision}", ""] if seed_decision else []
    conventions = ["## Code style", ""] + [f"- {c}" for c in convs] + [""]
    testing = ["## Testing", "",
               "- Run `uv run pytest tests/ -q` before finishing any task.", ""]
    rules = ["## Rules", "", f"- {seed_decision}", ""] if seed_decision else []

    if set(filenames) == {"CLAUDE.md", "AGENTS.md"}:
        claude_lines = overview + ([] if is_rule else decisions)
        agents_lines = (["# AGENTS.md", "",
                         "Guidance for AI coding agents working in this repository.", ""]
                        + conventions + testing + (rules if is_rule else []))
        (work / "CLAUDE.md").write_text("\n".join(claude_lines) + "\n")
        (work / "AGENTS.md").write_text("\n".join(agents_lines) + "\n")
        return
    body = overview + decisions + conventions + testing
    text = "\n".join(body) + "\n"
    for name in filenames:
        (work / name).write_text(text)


# Which static rules file(s) each condition writes into the work repo.
_FILE_CONDITIONS = {
    "claudemd": ("CLAUDE.md",),
    "agentsmd": ("AGENTS.md",),
    "claudemd_agentsmd": ("CLAUDE.md", "AGENTS.md"),
    "claudemd_with": ("CLAUDE.md",),
}


def _run_session(repo: str, prompt: str, claude_cmd: str, env: dict, model: str) -> dict:
    # Non-interactive sessions can't answer permission prompts; without this flag the
    # model cannot write files and every editing task fails vacuously. Safe here: the
    # session is jailed to a throwaway HOME and a disposable fixture-repo copy.
    cmd = [claude_cmd, "-p", prompt, "--output-format", "json",
           "--dangerously-skip-permissions"]
    if model:
        cmd += ["--model", model]
    t0 = time.perf_counter()
    proc = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True, timeout=1200)
    wall = int((time.perf_counter() - t0) * 1000)
    try:
        data = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"_error": f"unparseable output rc={proc.returncode}: {proc.stderr[-300:]}",
                "duration_ms": wall}
    data.setdefault("duration_ms", wall)
    return data


def _tool_calls(home: Path) -> int:
    calls = 0
    for f in (home / ".claude" / "projects").rglob("*.jsonl"):
        for line in f.read_text(errors="ignore").splitlines():
            if '"tool_use"' in line:
                calls += 1
    return calls


def _telemetry_check(row: dict, snap: dict):
    otel_total = sum(snap["tokens"].values())
    row["otel_tokens_total"] = otel_total
    row["otel_cost_usd"] = round(snap["cost_usd"], 6)
    if otel_total == 0:
        row["telemetry_ok"] = None  # no export received (stub, or telemetry off)
        return
    ref = row["tokens_total"]
    row["telemetry_ok"] = ref > 0 and abs(otel_total - ref) / ref <= _TELEMETRY_TOLERANCE


def run_campaign(out_dir: Path, reps: int = 3, task_ids=None, claude_cmd: str = "claude",
                 seed: int = 0, model: str = "",
                 conditions: tuple = ("without", "claudemd", "with")) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / "runs.jsonl"
    (out_dir / "campaign.json").write_text(json.dumps({
        "model": model, "seed": seed, "reps": reps, "conditions": list(conditions),
        "managed_settings_present": _MANAGED_SETTINGS.exists(),
        "started_at": datetime.now(timezone.utc).isoformat()}, indent=2))
    tasks = _load_tasks(task_ids)
    singles = [t for t in tasks if not t["chain"]]
    chains: dict[str, list] = {}
    for t in tasks:
        if t["chain"]:
            chains.setdefault(t["chain"], []).append(t)

    rx = OtelReceiver()
    rx.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            golden = build_webapi(td / "golden", seed=seed)
            baseline = _mine_baseline(str(golden))
            # Rep outermost, condition INNERMOST: conditions alternate in time so
            # drift / cache warming cannot masquerade as a condition effect.
            for rep in range(reps):
                for task in singles:
                    for condition in conditions:
                        work, home = _fresh(td, golden, f"{task['id']}-{condition}-{rep}")
                        row = _one_run(task, condition, rep, work, home, baseline,
                                       claude_cmd, seed, model, rx)
                        _append(out, row)
                for chain_tasks in chains.values():
                    # A chain's steps must stay sequential within one condition
                    # (shared repo + HOME: accumulation), so the full chain runs
                    # per condition — but conditions still cycle within the rep.
                    for condition in conditions:
                        work, home = _fresh(td, golden, f"{chain_tasks[0]['chain']}-{condition}-{rep}")
                        for task in chain_tasks:  # steps share repo + HOME: accumulation
                            row = _one_run(task, condition, rep, work, home, baseline,
                                           claude_cmd, seed, model, rx)
                            _append(out, row)
    finally:
        rx.stop()
    return out


def _fresh(td: Path, golden: Path, tag: str):
    work, home = td / f"w-{tag}", td / f"h-{tag}"
    shutil.copytree(golden, work)
    home.mkdir()
    # resolve(): macOS TemporaryDirectory lives under /var/folders, a symlink to
    # /private/var. The SessionStart hook slugs the repo via `git rev-parse
    # --show-toplevel`, which returns the CANONICAL path — seeding the store under
    # the symlinked path would target a different slug and inject nothing.
    return work.resolve(), home.resolve()


def _append(out: Path, row: dict):
    with out.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def _mine_baseline(repo: str) -> list[dict]:
    from contexer import miner
    return miner.mine_conventions(repo)


def _one_run(task, condition, rep, work: Path, home: Path, baseline,
             claude_cmd, seed, model, rx: OtelReceiver) -> dict:
    prompt = task["prompt"].replace("{seed}", str(seed))
    check_cmd = task["check_cmd"].replace("{seed}", str(seed))
    row = {"task_id": task["id"], "kind": task["kind"], "chain": task["chain"],
           "step": task["step"], "condition": condition, "rep": rep, "model": model,
           "ts": time.time(),
           "tokens_in": 0, "tokens_out": 0, "tokens_cache_read": 0, "tokens_cache_write": 0,
           "tokens_total": 0, "cost_usd": 0.0, "turns": 0, "duration_ms": 0, "tool_calls": 0,
           "violations": 0, "rationale": 0.0, "success": False, "result_snippet": "",
           "otel_tokens_total": 0, "otel_cost_usd": 0.0, "telemetry_ok": None, "error": ""}
    try:
        # Chains set up their condition once (before step 1); singles on every run.
        # "claudemd_with" (condition D) layers contexer on top of a pre-existing
        # CLAUDE.md — the adoption question for repos that already maintain one.
        if not task["chain"] or task["step"] <= 1:
            files = _FILE_CONDITIONS.get(condition)
            if files:
                _condition_c_setup(work, task["seed_decision"], files)
            if condition in ("with", "claudemd_with"):
                _condition_b_setup(str(work), home, task["seed_decision"])
        rx.reset()
        row["ts"] = time.time()  # stamped when the session starts (post-setup)
        res = _run_session(str(work), prompt, claude_cmd,
                           _session_env(home, rx.port), model)
        if res.get("_error"):
            row["error"] = res["_error"]
            return row
        # A claude-level failure (auth, API error) still returns well-formed JSON with
        # zeroed usage — recording it as a clean row would silently poison the medians.
        if res.get("is_error") or res.get("terminal_reason", "completed") != "completed":
            row["error"] = (f"session error ({res.get('terminal_reason', 'unknown')}): "
                            f"{str(res.get('result', ''))[:200]}")
            return row
        u = res.get("usage", {})
        row.update(tokens_in=u.get("input_tokens", 0), tokens_out=u.get("output_tokens", 0),
                   tokens_cache_read=u.get("cache_read_input_tokens", 0),
                   tokens_cache_write=u.get("cache_creation_input_tokens", 0),
                   cost_usd=res.get("total_cost_usd", 0.0), turns=res.get("num_turns", 0),
                   duration_ms=res.get("duration_ms", 0), tool_calls=_tool_calls(home))
        row["tokens_total"] = (row["tokens_in"] + row["tokens_out"] +
                               row["tokens_cache_read"] + row["tokens_cache_write"])
        time.sleep(1.5 if rx.port else 0)  # let the final OTel export flush
        _telemetry_check(row, rx.snapshot())
        row["violations"] = score.count_violations(score.changed_files(str(work)), baseline)
        row["rationale"] = score.rationale_score(res.get("result", ""), task["gold"])
        row["result_snippet"] = str(res.get("result", ""))[:300]
        if check_cmd:
            chk = subprocess.run(check_cmd, shell=True, cwd=work, capture_output=True, timeout=600)
            row["success"] = chk.returncode == 0
        else:
            row["success"] = True
    except Exception as exc:  # a failed run is a data point, never a crash
        row["error"] = repr(exc)
    return row


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="benchmarks/artifacts/dev")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--tasks", default="")
    ap.add_argument("--claude-cmd", default="claude")
    ap.add_argument("--model", default="")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--conditions", default="without,claudemd,with")
    a = ap.parse_args()
    ids = [s for s in a.tasks.split(",") if s] or None
    conds = tuple(s for s in a.conditions.split(",") if s)
    if not a.model:
        print("WARNING: no --model pinned; the report will flag mixed models.", file=sys.stderr)
    print(run_campaign(Path(a.out), reps=a.reps, task_ids=ids,
                       claude_cmd=a.claude_cmd, seed=a.seed, model=a.model,
                       conditions=conds))
