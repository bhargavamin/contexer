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
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from benchmarks import score, seeding
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


def _load_tasks(task_ids, tasks_file: Path = None):
    tasks = json.loads((tasks_file or TASKS_FILE).read_text())
    if task_ids is None:
        # Paraphrase variants (prompt-sensitivity probes) never run by default —
        # they'd double-count their base task in campaign aggregates.
        picked = [t for t in tasks if not t.get("variant_of")]
    else:
        picked = [t for t in tasks if t["id"] in task_ids]
    return sorted(picked, key=lambda t: (t["chain"], t["step"]))


_SEEDED_MARK = "BENCH_SEEDED_IDS="


def _condition_b_setup(repo: str, home: Path, seed_decision: str, source: Path = None,
                       seed_decisions: list = None) -> list:
    """contexer install + bootstrap + optional decision seed, in a child process
    whose HOME is the isolated one (store paths must resolve inside it). `source`
    is the contexer checkout `uv run` installs from (its cwd resolves the pyproject
    / venv that provides the `contexer` console script and the `contexer` package
    imported below) — this is what lets an A/B campaign compare two contexer
    versions; it defaults to this harness's own repo root, so callers that don't
    pass it see no behavior change. Returns the stored ids of `seed_decisions`, in order."""
    env = _session_env(home, otel_port=0)
    src = source or Path(__file__).resolve().parent.parent
    subprocess.run(["uv", "run", "contexer", "install"], env=env, check=True,
                   capture_output=True, cwd=src)
    # Use the stable tool surface so campaigns can also target pre-redesign versions.
    code = (f"from contexer import server, store\n"
            f"server.bootstrap_context(repo_path={repo!r})\n")
    if seed_decision:
        code += (f"store.update_decision({repo!r}, {seed_decision!r}, 'bench-seed', "
                 "'constraint', created_by='human')\n")
    if seed_decisions:
        # Seeded in list order through the agent capture path plus approval; the generated
        # script runs inside the SOURCE checkout (an older one for `with_prev`), so it imports
        # only `contexer`. See `seeding.seed_script` for why a refused seed fails setup.
        code += seeding.seed_script(repo, seed_decisions)
        code += f"print({_SEEDED_MARK!r} + __import__('json').dumps(seeded_ids))\n"
    try:
        proc = subprocess.run(["uv", "run", "python", "-c", code], env=env, check=True,
                              capture_output=True, cwd=src)
    except subprocess.CalledProcessError as exc:
        # The row records this message: name the refused seed (or the real exception), not
        # the CalledProcessError repr, which only repeats the whole generated script.
        tail = (exc.stderr or b"").decode(errors="replace").strip().splitlines()[-3:]
        raise RuntimeError("contexer seed setup failed: " + " | ".join(tail)) from exc
    for line in (getattr(proc, "stdout", None) or b"").decode(errors="replace").splitlines():
        if line.startswith(_SEEDED_MARK):
            return json.loads(line[len(_SEEDED_MARK):])
    return []


def _condition_c_setup(work: Path, seed_decision: str,
                       filenames: tuple = ("CLAUDE.md",), seed_decisions: list = None) -> None:
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
    # Every seed the contexer arm stores, so both arms carry the same knowledge.
    seeds = ([seed_decision] if seed_decision else []) + [
        item["content"] for item in seed_decisions or []]

    def _is_rule(text: str) -> bool:
        return text.lower().startswith(("never", "always", "don't", "do not"))

    rule_seeds = [s for s in seeds if _is_rule(s)]
    decision_seeds = [s for s in seeds if not _is_rule(s)]

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
    def _section(heading: str, items: list) -> list:
        return [heading, ""] + [f"- {s}" for s in items] + [""] if items else []

    conventions = ["## Code style", ""] + [f"- {c}" for c in convs] + [""]
    testing = ["## Testing", "",
               "- Run `uv run pytest tests/ -q` before finishing any task.", ""]

    if set(filenames) == {"CLAUDE.md", "AGENTS.md"}:
        claude_lines = overview + _section("## Key decisions", decision_seeds)
        agents_lines = (["# AGENTS.md", "",
                         "Guidance for AI coding agents working in this repository.", ""]
                        + conventions + testing + _section("## Rules", rule_seeds))
        (work / "CLAUDE.md").write_text("\n".join(claude_lines) + "\n")
        (work / "AGENTS.md").write_text("\n".join(agents_lines) + "\n")
        return
    body = overview + _section("## Key decisions", seeds) + conventions + testing
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

# Conditions that always install contexer regardless of --contexer-sources.
_CONTEXER_CONDITIONS = ("with", "claudemd_with")


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


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


def _needed_delivery(home: Path, content: str, entry_id: str) -> tuple[str, int]:
    """(how the needed decision reached the agent, Contexer lookups the agent made), read
    from the session transcript. Only what the agent RECEIVED counts (hook context and tool
    results, never its own messages, so an echo can't): "full" when the decision's whole
    text arrived, "named" when only its id did (a pointer), else "none". Lookups count the
    agent's calls to Contexer's get_context tools, the way a pointer is followed up."""
    received, lookups = [], 0
    for transcript in (home / ".claude" / "projects").rglob("*.jsonl"):
        for line in transcript.read_text(errors="ignore").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(entry, dict):
                continue
            if entry.get("type") == "assistant":
                content_blocks = (entry.get("message") or {}).get("content") or []
                lookups += sum(1 for block in content_blocks if isinstance(block, dict)
                               and block.get("type") == "tool_use"
                               and "contexer" in str(block.get("name", ""))
                               and "get_context" in str(block.get("name", "")))
            else:
                received.extend(_strings(entry))
    text = " ".join(" ".join(received).split())
    if " ".join(content.split()) in text:
        return "full", lookups
    if entry_id and entry_id[:8] in text:
        return "named", lookups
    return "none", lookups


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
                 conditions: tuple = ("without", "claudemd", "with"),
                 contexer_sources: dict = None, wait_for_otel: bool = True,
                 tasks_file: Path = None) -> Path:
    """`contexer_sources` maps condition name -> contexer checkout path (see
    `_condition_b_setup`), for A/B comparisons across contexer versions. A
    condition present in the map installs contexer from that path even if it
    isn't one of `_CONTEXER_CONDITIONS`. Omitted/empty: unchanged behavior.
    `tasks_file` swaps in another task list (e.g. `retrieval_tasks.json`)."""
    contexer_sources = contexer_sources or {}
    if "with_prev" in conditions and not str(contexer_sources.get("with_prev") or "").strip():
        # Without a source (or with an empty one) it gets neither rules files nor an
        # install: a bare arm the report would still label as a version comparison.
        raise ValueError("condition 'with_prev' needs --contexer-sources with_prev=<checkout>")
    tasks_path = tasks_file or TASKS_FILE
    tasks_sha = hashlib.sha256(tasks_path.read_bytes()).hexdigest()
    out = out_dir / "runs.jsonl"
    meta_path = out_dir / "campaign.json"
    if out.exists() and out.stat().st_size and meta_path.exists():
        # Rows append to runs.jsonl while campaign.json is rewritten: appending another task
        # set's rows would leave mixed results under metadata naming only the new one.
        previous = json.loads(meta_path.read_text()).get("tasks_sha256")
        if previous != tasks_sha and (previous or tasks_path != TASKS_FILE):
            raise ValueError(f"{out_dir} already holds runs from a different task file; "
                             "use a new --out directory")
    out_dir.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps({
        "model": model, "seed": seed, "reps": reps, "conditions": list(conditions),
        "contexer_sources": contexer_sources,
        "tasks_file": str(tasks_path),
        "tasks_sha256": tasks_sha,
        "managed_settings_present": _MANAGED_SETTINGS.exists(),
        "started_at": datetime.now(timezone.utc).isoformat()}, indent=2))
    tasks = _load_tasks(task_ids, tasks_file)
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
                                       claude_cmd, seed, model, rx, contexer_sources, wait_for_otel)
                        _append(out, row)
                        _discard(work, home)
                for chain_tasks in chains.values():
                    # A chain's steps must stay sequential within one condition
                    # (shared repo + HOME: accumulation), so the full chain runs
                    # per condition — but conditions still cycle within the rep.
                    for condition in conditions:
                        work, home = _fresh(td, golden, f"{chain_tasks[0]['chain']}-{condition}-{rep}")
                        for task in chain_tasks:  # steps share repo + HOME: accumulation
                            row = _one_run(task, condition, rep, work, home, baseline,
                                           claude_cmd, seed, model, rx, contexer_sources, wait_for_otel)
                            _append(out, row)
                        _discard(work, home)
    finally:
        rx.stop()
    return out


def _fresh(td: Path, golden: Path, tag: str):
    work, home = td / f"w-{tag}", td / f"h-{tag}"
    # ignore transient git-gc pack files: they can vanish between listing and copy
    shutil.copytree(golden, work, ignore=shutil.ignore_patterns(
        "tmp_pack_*", "tmp_idx_*", "tmp_rev_*", "tmp_mtimes_*"))
    home.mkdir()
    # resolve(): macOS TemporaryDirectory lives under /var/folders, a symlink to
    # /private/var. The SessionStart hook slugs the repo via `git rev-parse
    # --show-toplevel`, which returns the CANONICAL path — seeding the store under
    # the symlinked path would target a different slug and inject nothing.
    return work.resolve(), home.resolve()


def _discard(*dirs: Path) -> None:
    """Drop a finished session's repo copy and HOME once its row is written: each holds its
    own venv and transcripts, and keeping every session's until the campaign ends filled the
    disk mid-run."""
    for d in dirs:
        shutil.rmtree(d, ignore_errors=True)


def _append(out: Path, row: dict):
    with out.open("a") as fh:
        fh.write(json.dumps(row) + "\n")


def _mine_baseline(repo: str) -> list[dict]:
    from contexer import miner
    return miner.mine_conventions(repo)


def _one_run(task, condition, rep, work: Path, home: Path, baseline,
             claude_cmd, seed, model, rx: OtelReceiver, contexer_sources: dict = None,
             wait_for_otel: bool = True) -> dict:
    prompt = task["prompt"].replace("{seed}", str(seed))
    check_cmd = task["check_cmd"].replace("{seed}", str(seed))
    row = {"task_id": task["id"], "kind": task["kind"], "chain": task["chain"],
           "step": task["step"], "condition": condition, "rep": rep, "model": model,
           "ts": time.time(),
           "tokens_in": 0, "tokens_out": 0, "tokens_cache_read": 0, "tokens_cache_write": 0,
           "tokens_total": 0, "cost_usd": 0.0, "turns": 0, "duration_ms": 0, "tool_calls": 0,
           "violations": 0, "rationale": 0.0, "success": False, "result_snippet": "",
           "otel_tokens_total": 0, "otel_cost_usd": 0.0, "telemetry_ok": None, "error": ""}
    needed = None  # (content, stored id) of the needed decision, on Contexer arms
    try:
        # Chains set up their condition once (before step 1); singles on every run.
        # "claudemd_with" (condition D) layers contexer on top of a pre-existing
        # CLAUDE.md — the adoption question for repos that already maintain one.
        if not task["chain"] or task["step"] <= 1:
            seeds = seeding.seed_items(task, seed, rep)
            index = task.get("needed_decision")
            if index is not None:
                # Which author's style the needed decision was written in this rep.
                row["needed_style"] = seeds[index]["style"]
            files = _FILE_CONDITIONS.get(condition)
            if files:
                _condition_c_setup(work, task["seed_decision"], files, seed_decisions=seeds)
            src = (contexer_sources or {}).get(condition)
            if condition in _CONTEXER_CONDITIONS or src:
                ids = _condition_b_setup(str(work), home, task["seed_decision"],
                                         Path(src) if src else None, seed_decisions=seeds)
                if index is not None:
                    needed = (seeds[index]["content"], ids[index] if ids else "")
        rx.reset()
        row["ts"] = time.time()  # stamped when the session starts (post-setup)
        # Pre-session HEAD: sessions may commit their edits, which would vanish
        # from a live `git diff HEAD` — score against where the repo started.
        base_sha = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                                  capture_output=True, text=True).stdout.strip() or "HEAD"
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
        if wait_for_otel and rx.port:
            time.sleep(1.5)  # let the final OTel export flush
        _telemetry_check(row, rx.snapshot())
        row["violations"] = score.count_violations(
            score.changed_files(str(work), base_sha), baseline)
        row["rationale"] = score.rationale_score(res.get("result", ""), task["gold"])
        row["result_snippet"] = str(res.get("result", ""))[:300]
        if needed:
            # Contexer arms only: a static rules file is loaded into the system prompt, which
            # the transcript doesn't record, so "none" there would be a false reading.
            row["needed_delivery"], row["contexer_lookups"] = _needed_delivery(home, *needed)
        if check_cmd:
            # Same isolated env as the session: success must never come from
            # developer-local HOME/uv/python configuration the session couldn't see.
            chk = subprocess.run(check_cmd, shell=True, cwd=work, capture_output=True,
                                 timeout=600, env=_session_env(home, 0))
            row["success"] = chk.returncode == 0
            if not row["success"]:
                # The session's folders are discarded after scoring: keep why the check failed.
                row["check_output"] = (chk.stderr or chk.stdout or b"").decode(
                    errors="replace")[-400:]
        else:
            row["success"] = True
        if task["chain"]:
            # Snapshot the worktree so the next step scores ONLY its own work —
            # otherwise an untracked file left by step 1 is re-counted by steps 2-3
            # (chain steps share the worktree by design). Also mirrors real flow:
            # work is committed between sessions.
            subprocess.run(["git", "-C", str(work), "add", "-A"],
                           capture_output=True, timeout=60)
            subprocess.run(["git", "-C", str(work),
                            "-c", "user.email=bench@bench.local", "-c", "user.name=Bench",
                            "-c", "commit.gpgsign=false", "commit", "-q", "--allow-empty",
                            "-m", f"bench: snapshot after {task['id']} step {task['step']}"],
                           capture_output=True, timeout=60)
    except Exception as exc:  # a failed run is a data point, never a crash
        row["error"] = repr(exc)
    return row


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="benchmarks/artifacts/dev")
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--tasks", default="")
    ap.add_argument("--tasks-file", default="",
                    help="task list to run instead of benchmarks/tasks.json "
                         "(e.g. benchmarks/retrieval_tasks.json)")
    ap.add_argument("--claude-cmd", default="claude")
    ap.add_argument("--model", default="")
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--conditions", default="without,claudemd,with")
    ap.add_argument("--contexer-sources", default="",
                    help="condition=path pairs, comma-separated (e.g. "
                         "contexer_pre_v1=/path/a,contexer_v1=/path/b) — selects "
                         "which contexer checkout `uv run contexer install` uses "
                         "for that condition; a condition not listed here falls "
                         "back to this harness's own repo root.")
    a = ap.parse_args()
    ids = [s for s in a.tasks.split(",") if s] or None
    conds = tuple(s for s in a.conditions.split(",") if s)
    sources = {}
    for pair in (s for s in a.contexer_sources.split(",") if s):
        if "=" not in pair:
            ap.error(f"--contexer-sources entry {pair!r} is not name=path")
        name, path = pair.split("=", 1)
        sources[name] = path
    if not a.model:
        print("WARNING: no --model pinned; the report will flag mixed models.", file=sys.stderr)
    print(run_campaign(Path(a.out), reps=a.reps, task_ids=ids,
                       claude_cmd=a.claude_cmd, seed=a.seed, model=a.model,
                       conditions=conds, contexer_sources=sources,
                       tasks_file=Path(a.tasks_file) if a.tasks_file else None))
