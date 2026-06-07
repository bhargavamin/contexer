#!/usr/bin/env python3
"""Contexer A/B benchmark orchestrator (scripts/ install mode, version-selectable).

Toggles Contexer with the REAL installer — `<contexer-dir>/scripts/install.sh` /
`uninstall.sh` — so the warm arm reflects exactly what a user gets. `--contexer-dir` lets you point
at any checkout/worktree (e.g. a pre-JIT commit) to A/B test Contexer versions themselves.

Headless `claude -p` only authenticates against the real ~/.claude here (subscription token in the
Keychain; a sandboxed HOME returns "Not logged in"), so the install touches the real global config.
The suite BACKS UP and RESTORES ~/.claude.json, ~/.claude/settings.json and ~/.contexer/ around the
run, and groups by install state:
  uninstall -> run cold+paste ;  install -> run warm ;  uninstall ;  restore.

Arms: cold ("No memory"), paste ("Paste rules by hand"), warm ("Contexer").
Store seeding is written directly as JSON (version-agnostic across store.py revisions).
Pipeline: run_bench.py -> results/runs.jsonl -> grade.py -> analyze.py
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BENCH_DIR / "results"
sys.path.insert(0, str(BENCH_DIR))
from corpus import CORPUS  # noqa: E402
import price_table  # noqa: E402

CONTEXER_TOOLS = ("get_context", "capture_context", "update_context", "bootstrap_context")
HOME = Path.home()
STORE_DIR = HOME / ".contexer"
GLOBAL_PATHS = [HOME / ".claude.json", HOME / ".claude" / "settings.json", STORE_DIR / ".current_repo"]
CONTEXER_DIR = BENCH_DIR.parent  # overridden by --contexer-dir


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _slug(repo: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "_", repo.strip("/"))


def _store_path(repo: str) -> Path:
    STORE_DIR.mkdir(exist_ok=True)
    return STORE_DIR / f"{_slug(repo)}.json"


def seed_store(repo: str, seed_decisions: list) -> None:
    """Write the warm store directly (no dependence on a specific store.py version)."""
    entries = [{
        "id": str(uuid.uuid4()), "type": "decision", "subtype": d.get("subtype", ""),
        "content": d["content"], "session_id": "bench-seed",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    } for d in seed_decisions]
    _store_path(repo).write_text(json.dumps({"repo_path": repo, "entries": entries}, indent=2))
    (STORE_DIR / ".current_repo").write_text(repo)


def backup_global() -> dict:
    return {str(p): (p.read_text() if p.exists() else None) for p in GLOBAL_PATHS}


def restore_global(snap: dict) -> None:
    for path_str, content in snap.items():
        p = Path(path_str)
        if content is None:
            p.unlink(missing_ok=True)
        else:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content)
    log("  restored ~/.claude.json, ~/.claude/settings.json, ~/.contexer/.current_repo")


HOOK_EVENTS = ("SessionStart", "UserPromptSubmit", "PreCompact", "PostCompact")


def strip_contexer_from_config() -> None:
    """Clear the slate so the version-under-test's install.sh writes FRESH hooks. We remove the
    Contexer MCP entry and DROP the four hook-event keys entirely (SessionStart, UserPromptSubmit,
    PreCompact, PostCompact). Dropping the whole keys (not just contexer entries) is required
    because b732052's install.sh skips an event if ANY hook for it already exists — and your global
    config has other hooks (e.g. caveman's SessionStart). restore_global() puts everything back."""
    cj = HOME / ".claude.json"
    if cj.exists():
        d = json.loads(cj.read_text())
        if d.get("mcpServers", {}).pop("contexer", None) is not None:
            cj.write_text(json.dumps(d, indent=2))
    sj = HOME / ".claude" / "settings.json"
    if sj.exists():
        d = json.loads(sj.read_text())
        hooks = d.get("hooks", {})
        for ev in HOOK_EVENTS:
            hooks.pop(ev, None)
        allow = d.get("permissions", {}).get("allow", [])
        d.setdefault("permissions", {})["allow"] = [p for p in allow if "contexer" not in p]
        sj.write_text(json.dumps(d, indent=2))


def run_script(name: str, fatal: bool = True) -> None:
    # stdin=DEVNULL so an interactive `read` (older uninstall.sh) gets EOF and exits instead of
    # blocking forever on inherited stdin.
    r = subprocess.run(["bash", str(CONTEXER_DIR / "scripts" / name)],
                       stdin=subprocess.DEVNULL, capture_output=True, text=True)
    if r.returncode != 0:
        # Older uninstall.sh is interactive (set -e + `read`) and fails headless; that's fine —
        # restore_global() overwrites the global config and is the real cleanup.
        log(f"  WARN: scripts/{name} exited {r.returncode} (cleanup falls back to config restore)")
        if fatal:
            raise subprocess.CalledProcessError(r.returncode, name, r.stdout, r.stderr)


def find_transcript(session_id: str) -> Path | None:
    root = HOME / ".claude" / "projects"
    if not session_id or not root.exists():
        return None
    hits = list(root.rglob(f"{session_id}.jsonl"))
    return hits[0] if hits else None


def count_contexer_tool_calls(transcript: Path | None) -> dict:
    counts = {t: 0 for t in CONTEXER_TOOLS}
    if not transcript or not transcript.exists():
        return counts
    for line in transcript.read_text().splitlines():
        try:
            obj = json.loads(line)
        except Exception:
            continue
        msg = obj.get("message", {})
        content = msg.get("content", []) if isinstance(msg, dict) else []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    for t in CONTEXER_TOOLS:
                        if block.get("name", "").endswith(t):
                            counts[t] += 1
    return counts


def run_arm(task: dict, arm: str, rep: int, model: str) -> dict:
    work = Path(tempfile.mkdtemp(prefix="contexer-bench-"))
    project = work / "project"
    project.mkdir(parents=True)
    for rel, content in task["project_files"].items():
        p = project / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    project = project.resolve()  # match `git rev-parse --show-toplevel`
    subprocess.run(["git", "init", "-q"], cwd=str(project), check=True)
    subprocess.run(["git", "add", "-A"], cwd=str(project), check=True)
    subprocess.run(["git", "-c", "user.email=b@b", "-c", "user.name=b", "commit", "-q", "-m", "seed"],
                   cwd=str(project), check=True)

    repo = str(project)
    slug_file = _store_path(repo)
    sent_prompt = task["prompt"]
    if arm == "warm":
        seed_store(repo, task["seed_decisions"])
    else:
        slug_file.unlink(missing_ok=True)
        if arm == "paste":
            decisions = "\n".join(f"- {d['content']}" for d in task["seed_decisions"])
            sent_prompt = f"Follow these project decisions:\n{decisions}\n\n{task['prompt']}"

    cmd = ["claude", "-p", sent_prompt, "--output-format", "json", "--model", model,
           "--add-dir", str(project), "--permission-mode", "acceptEdits"]
    if arm != "warm":  # clean baseline; warm uses the real global install
        cmd += ["--setting-sources", "project", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']

    proc = subprocess.run(cmd, cwd=str(project), capture_output=True, text=True)
    try:
        res = json.loads(proc.stdout.strip())
    except Exception:
        res = {"is_error": True, "result": f"PARSE_FAIL: {proc.stdout[:150]} / {proc.stderr[:150]}",
               "usage": {}, "num_turns": 0, "duration_api_ms": 0, "session_id": "", "total_cost_usd": 0}

    diff = subprocess.run(["git", "diff"], cwd=str(project), capture_output=True, text=True).stdout
    usage = res.get("usage", {}) or {}
    rec = {
        "task_id": task["id"], "kind": task["kind"], "arm": arm, "rep": rep, "model": model,
        "contexer_dir": str(CONTEXER_DIR),
        "is_error": res.get("is_error", True), "result": res.get("result", ""), "diff": diff,
        "prompt": task["prompt"], "rubric": task["rubric"],
        "seed_decisions": [d["content"] for d in task["seed_decisions"]],
        "num_turns": res.get("num_turns", 0), "duration_api_ms": res.get("duration_api_ms", 0),
        "session_id": res.get("session_id", ""),
        "usage": {k: usage.get(k, 0) for k in
                  ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")},
        "reported_cost_usd": res.get("total_cost_usd", 0),
        "derived_cost_usd": round(price_table.cost_usd(model, usage), 6),
        "contexer_tool_calls": count_contexer_tool_calls(find_transcript(res.get("session_id", ""))),
    }
    slug_file.unlink(missing_ok=True)
    shutil.rmtree(work, ignore_errors=True)
    return rec


def main() -> None:
    global CONTEXER_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--tasks", default="")
    ap.add_argument("--arms", default="cold,paste,warm")
    ap.add_argument("--reps", type=int, default=1)
    ap.add_argument("--model", default="claude-haiku-4-5")
    ap.add_argument("--out", default=str(RESULTS_DIR / "runs.jsonl"))
    ap.add_argument("--contexer-dir", default=str(BENCH_DIR.parent),
                    help="checkout/worktree of Contexer to install from (default: this repo)")
    ap.add_argument("--no-pull", action="store_true")
    args = ap.parse_args()
    CONTEXER_DIR = Path(args.contexer_dir).resolve()

    RESULTS_DIR.mkdir(exist_ok=True)
    if not price_table.is_known(args.model):
        log(f"WARNING: model '{args.model}' not in price_table — derived cost will be 0.")
    log(f"Contexer install source: {CONTEXER_DIR}")

    if not args.no_pull:
        r = subprocess.run(["git", "-C", str(CONTEXER_DIR), "pull", "--ff-only"], capture_output=True, text=True)
        out = (r.stdout or r.stderr).strip()
        log("git pull: " + (out.splitlines()[-1] if out else "(no output)"))

    want = set(filter(None, args.tasks.split(",")))
    tasks = [t for t in CORPUS if not want or t["id"] in want]
    arms = [a for a in args.arms.split(",") if a in ("cold", "paste", "warm")]
    clean_arms = [a for a in arms if a in ("cold", "paste")]

    log("Backing up your real Contexer config (restored at the end) ...")
    snap = backup_global()
    out_path = Path(args.out)
    written = 0
    try:
        with out_path.open("w") as f:
            if clean_arms:
                log("scripts/uninstall.sh + clear config (clean state for cold/paste) ...")
                run_script("uninstall.sh", fatal=False)
                strip_contexer_from_config()
                for task in tasks:
                    for arm in clean_arms:
                        for rep in range(args.reps):
                            log(f"[{arm}] {task['id']} rep{rep} ...")
                            rec = run_arm(task, arm, rep, args.model)
                            f.write(json.dumps(rec) + "\n"); f.flush(); written += 1
                            log(f"     err={rec['is_error']} cost=${rec['derived_cost_usd']:.4f}")
            if "warm" in arms:
                log("clear any existing Contexer config, then scripts/install.sh (fresh) ...")
                run_script("uninstall.sh", fatal=False)
                strip_contexer_from_config()  # ensure install.sh writes the version-under-test fresh
                run_script("install.sh")
                for task in tasks:
                    for rep in range(args.reps):
                        log(f"[warm] {task['id']} rep{rep} ...")
                        rec = run_arm(task, "warm", rep, args.model)
                        f.write(json.dumps(rec) + "\n"); f.flush(); written += 1
                        log(f"     err={rec['is_error']} cost=${rec['derived_cost_usd']:.4f} "
                            f"ctx_calls={sum(rec['contexer_tool_calls'].values())}")
                log("scripts/uninstall.sh ...")
                run_script("uninstall.sh", fatal=False)
        log(f"\nWrote {written} run(s) to {out_path}")
    finally:
        log("Restoring your real Contexer config ...")
        restore_global(snap)


if __name__ == "__main__":
    main()
