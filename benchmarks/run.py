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
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from benchmarks import score, seeding
from benchmarks.fixtures.generate import apply_overlay, build_webapi
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
                       seed_decisions: list = None, steady: bool = False) -> list:
    """contexer install + bootstrap + optional decision seed, in a child process
    whose HOME is the isolated one (store paths must resolve inside it). `source`
    is the contexer checkout `uv run` installs from (its cwd resolves the pyproject
    / venv that provides the `contexer` console script and the `contexer` package
    imported below) — this is what lets an A/B campaign compare two contexer
    versions; it defaults to this harness's own repo root, so callers that don't
    pass it see no behavior change. With `steady`, bootstrap is driven to completion and the
    setup fails unless the bootstrap prompt is silent afterwards (see `seeding.bootstrap_script`).
    Returns the stored ids of `seed_decisions`, in order."""
    env = _session_env(home, otel_port=0)
    src = source or Path(__file__).resolve().parent.parent
    subprocess.run(["uv", "run", "contexer", "install"], env=env, check=True,
                   capture_output=True, cwd=src)
    # Use the stable tool surface so campaigns can also target pre-redesign versions.
    code = "from contexer import server, store\n" + seeding.bootstrap_script(repo, steady)
    if seed_decision:
        code += (f"store.update_decision({repo!r}, {seed_decision!r}, 'bench-seed', "
                 "'constraint', created_by='human')\n")
    if seed_decisions:
        # Seeded in list order through the agent capture path plus approval; the generated
        # script runs inside the SOURCE checkout (an older one for `with_prev`), so it imports
        # only `contexer`. See `seeding.seed_script` for why a refused seed fails setup.
        code += seeding.seed_script(repo, seed_decisions)
        code += f"print({_SEEDED_MARK!r} + __import__('json').dumps(seeded_ids))\n"
    if steady:
        code += seeding.steady_check_script(repo)
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
    # Every seed the contexer arm stores, titled as Contexer shows it, so both arms carry the
    # same knowledge, superseded revisions included (dated and marked).
    seeds = ([seed_decision] if seed_decision else []) + [
        line for item in seed_decisions or [] for line in _static_entries(item)]

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


def _static_entries(item: dict) -> list[str]:
    """A seed as static-file lines: each superseded revision, dated and marked as replaced by the
    current one, then the current revision with its date. A plain seed is one titled line."""
    def titled(rev: dict) -> str:
        return f"{rev['title']}: {rev['content']}" if rev.get("title") else rev["content"]
    current = titled(item)
    if not item.get("history"):
        return [current]
    lines = [f"{titled(old)} (In force from {old.get('date') or 'an earlier date'}; superseded on "
             f"{item.get('date') or 'a later date'} by: {_decision_title(item)}.)"
             for old in item["history"]]
    lines.append(f"{current} (Decided {item.get('date') or 'later'}; replaces the earlier rule "
                 f"\"{_decision_title(item['history'][-1])}\".)")
    return lines


def _clarified(result: str, sides: list, side_taken: bool) -> bool:
    """K7c: the session names both colliding decisions (at least one term from each side in its
    final message) and its code takes neither side."""
    text = result.lower()
    return not side_taken and all(any(term.lower() in text for term in side) for side in sides)


def _decision_title(item: dict) -> str:
    """The title a decision record shows: its own, or its first sentence (capped)."""
    if item.get("title"):
        return item["title"]
    first = re.split(r"(?<=[.!?])\s", item["content"].strip(), maxsplit=1)[0]
    return first[:100]


def _docs_indexed_setup(work: Path, seed_decisions: list) -> None:
    """The well-organized-docs competitor: one decision record per decision under
    docs/decisions/ (ADR style), and a CLAUDE.md index listing every title and file. The layout
    is mechanical (store order, names from titles), so nothing is placed by hand per task; like
    Contexer, it needs the agent to open the record that applies."""
    records = work / "docs" / "decisions"
    records.mkdir(parents=True, exist_ok=True)
    index = ["# Project: record service", "",
             "FastAPI-style record service (Python, managed with uv). Run `uv run pytest tests/ -q`"
             " before finishing any task.", "",
             "## Decision records", "",
             "Engineering decisions live in `docs/decisions/`, one file each. Read the ones that "
             "apply before changing code:", ""]
    number = 0
    for item in seed_decisions or []:
        # A superseded revision keeps its own record, marked and pointing at its replacement.
        revisions = [(old, "superseded") for old in item.get("history", [])] + [(item, "accepted")]
        names = []
        for revision, status in revisions:
            number += 1
            title = _decision_title(revision)
            slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:50]
            names.append((f"{number:03d}-{slug}.md", title, revision, status))
        for position, (name, title, revision, status) in enumerate(names):
            if status == "superseded":
                status_line = (f"- Status: superseded on {item.get('date') or 'a later date'} by "
                               f"{names[-1][0]}\n- In force from: "
                               f"{revision.get('date') or 'an earlier date'}")
            elif len(names) > 1:
                status_line = (f"- Status: accepted ({item.get('date') or 'later'}), supersedes "
                               f"{names[position - 1][0]}")
            else:
                status_line = "- Status: accepted"
            (records / name).write_text(f"# {title}\n\n{status_line}\n"
                                        f"- Type: {item.get('subtype') or 'decision'}\n\n"
                                        f"{revision['content']}\n")
            index.append(f"- [{title}](docs/decisions/{name})"
                         + (" (superseded)" if status == "superseded" else ""))
    (work / "CLAUDE.md").write_text("\n".join(index) + "\n")


_MAINTAINED_HEADING = "## Decisions"


def _maintained_setup(work: Path) -> None:
    """The capture-loop competitor: no Contexer, and a CLAUDE.md that starts with no decisions
    and asks the agent to keep it current, the way a team that maintains CLAUDE.md works. What
    session 1 writes under the heading is all session 2 gets."""
    (work / "CLAUDE.md").write_text("\n".join([
        "# Project: record service", "",
        "FastAPI-style record service (Python, managed with uv). Run `uv run pytest tests/ -q`"
        " before finishing any task.", "",
        _MAINTAINED_HEADING, "",
        "Record every engineering decision you make or are told here, one bullet each with its"
        " reason, so later sessions follow it.", ""]) + "\n")


# Which static rules file(s) each condition writes into the work repo.
_FILE_CONDITIONS = {
    "claudemd": ("CLAUDE.md",),
    "claudemd_full": ("CLAUDE.md",),
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


def _blocks(entry: dict) -> list[dict]:
    content = (entry.get("message") or {}).get("content")
    return [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []


def _contexer_received(home: Path) -> tuple[str, int]:
    """(everything Contexer gave the agent, Contexer lookups the agent made), read from the
    session transcript. Only what CONTEXER gave counts: hook context
    (`hook_additional_context` attachments) and results of the agent's own Contexer tool calls,
    so neither an echo nor a file the agent read can. Lookups count the agent's calls to
    Contexer's get_context tools, the way a pointer is followed up."""
    entries = []
    for transcript in (home / ".claude" / "projects").rglob("*.jsonl"):
        for line in transcript.read_text(errors="ignore").splitlines():
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
    contexer_calls = {block.get("id"): str(block.get("name", ""))
                      for entry in entries if entry.get("type") == "assistant"
                      for block in _blocks(entry)
                      if block.get("type") == "tool_use" and "contexer" in str(block.get("name"))}
    lookups = sum("get_context" in name for name in contexer_calls.values())
    received = []
    for entry in entries:
        attachment = entry.get("attachment")
        if isinstance(attachment, dict) and attachment.get("type") == "hook_additional_context":
            received.extend(_strings(attachment))
        received.extend(s for block in _blocks(entry)
                        if block.get("type") == "tool_result"
                        and block.get("tool_use_id") in contexer_calls
                        for s in _strings(block.get("content")))
    return " ".join(" ".join(received).split()), lookups


def _needed_delivery(home: Path, content: str, entry_id: str) -> tuple[str, int]:
    """(how the needed decision reached the agent, Contexer lookups): "full" when the decision's
    whole text arrived, "named" when only its id did (a pointer), else "none"."""
    text, lookups = _contexer_received(home)
    if " ".join(content.split()) in text:
        return "full", lookups
    if entry_id and entry_id[:8] in text:
        return "named", lookups
    return "none", lookups


def _matches(patterns: list, text: str, noise: tuple = ()) -> bool:
    """Whether any pattern appears in `text`, after removing `noise`: the run's own repository
    path and chain name, which hook output repeats and which can contain a rule word
    (`w-cap-cents-with-0` matched the cents rule in the first smoke run)."""
    for word in noise:
        text = text.replace(word, " ")
    return any(re.search(p, text, re.IGNORECASE) for p in patterns)


def _run_noise(work: Path, task: dict) -> tuple:
    return tuple(w for w in (str(work), str(work.resolve()), work.name, task.get("chain", "")) if w)


def _revert_code_keep_memory(work: Path, base_sha: str) -> None:
    """Capture loop, between sessions: put the code back where session 1 started, keeping only
    what an arm uses as memory (CLAUDE.md here; Contexer's store lives in HOME). Session 1's code
    applies the rule, so leaving it would let session 2 copy the rule from the module and the
    `without` arm would pass without anything having been remembered."""
    claude_md = work / "CLAUDE.md"
    kept = claude_md.read_text(errors="replace") if claude_md.exists() else None
    subprocess.run(["git", "-C", str(work), "reset", "-q", "--hard", base_sha],
                   capture_output=True, timeout=60)
    subprocess.run(["git", "-C", str(work), "clean", "-fdq", "--", "app", "tests"],
                   capture_output=True, timeout=60)
    if kept is not None:
        claude_md.write_text(kept)


def _capture_state(condition: str, work: Path, home: Path, patterns: list,
                   noise: tuple = ()) -> dict:
    """Capture loop, between sessions: whether session 1 recorded the rule where session 2 can
    find it, judged by the task's `capture_terms` patterns over what was stored (the rule's
    wording is the agent's own, so there's no fixed text to compare). Contexer: the decisions
    in the isolated store, their status, and how many wait for review; no approval is simulated.
    claudemd_maintained: the CLAUDE.md the session left behind. Read-only."""
    if condition in _CONTEXER_CONDITIONS:
        decisions = []
        for path in (home / ".contexer").glob("*.json"):
            if path.name.startswith("_") or path.name.endswith(".deleted.json"):
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            decisions += [e for e in data.get("entries", [])
                          if isinstance(e, dict) and e.get("type") == "decision"]
        hits = [e for e in decisions
                if _matches(patterns, f"{e.get('title', '')} {e.get('content', '')}", noise)]
        return {"captured": bool(hits),
                "capture_status": hits[0].get("status", "approved") if hits else None,
                "decisions_stored": len(decisions),
                "decisions_pending": sum(e.get("status") == "pending_approval"
                                         for e in decisions)}
    if condition == "claudemd_maintained":
        claude_md = work / "CLAUDE.md"
        text = claude_md.read_text(errors="replace") if claude_md.exists() else ""
        decisions = text.split(_MAINTAINED_HEADING, 1)[1] if _MAINTAINED_HEADING in text else ""
        return {"captured": _matches(patterns, decisions, noise)}
    return {}


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
                 tasks_file: Path = None, steady_state: bool = False) -> Path:
    """`contexer_sources` maps condition name -> contexer checkout path (see
    `_condition_b_setup`), for A/B comparisons across contexer versions. A
    condition present in the map installs contexer from that path even if it
    isn't one of `_CONTEXER_CONDITIONS`. Omitted/empty: unchanged behavior.
    `tasks_file` swaps in another task list (e.g. `retrieval_tasks.json`). `steady_state`
    completes Contexer's bootstrap during setup, so sessions start as in a repository whose
    one-time setup is done."""
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
        previous_meta = json.loads(meta_path.read_text())
        previous = previous_meta.get("tasks_sha256")
        if previous != tasks_sha and (previous or tasks_path != TASKS_FILE):
            raise ValueError(f"{out_dir} already holds runs from a different task file; "
                             "use a new --out directory")
        if previous_meta.get("steady_state", False) != steady_state:
            # Steady-state and first-install sessions measure different things; never mix them.
            raise ValueError(f"{out_dir} already holds runs with steady_state="
                             f"{previous_meta.get('steady_state', False)}; use a new --out directory")
    out_dir.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps({
        "model": model, "seed": seed, "reps": reps, "conditions": list(conditions),
        "contexer_sources": contexer_sources, "steady_state": steady_state,
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
            # Apply every task's fixture overlay once before any paid session, so a broken overlay
            # stops the campaign instead of turning into one errored row per session.
            for task in tasks:
                if task.get("fixture_files"):
                    probe = td / f"overlay-check-{task['id']}"
                    shutil.copytree(golden, probe)
                    apply_overlay(probe, task["fixture_files"], seed)
                    shutil.rmtree(probe, ignore_errors=True)
            # Rep outermost, condition INNERMOST: conditions alternate in time so
            # drift / cache warming cannot masquerade as a condition effect.
            for rep in range(reps):
                for task in singles:
                    for condition in conditions:
                        work, home = _fresh(td, golden, f"{task['id']}-{condition}-{rep}")
                        row = _one_run(task, condition, rep, work, home, baseline,
                                       claude_cmd, seed, model, rx, contexer_sources, wait_for_otel,
                                       steady_state)
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
                                           claude_cmd, seed, model, rx, contexer_sources, wait_for_otel,
                                           steady_state)
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
             wait_for_otel: bool = True, steady_state: bool = False) -> dict:
    prompt = task["prompt"].replace("{seed}", str(seed))
    check_cmd = task["check_cmd"].replace("{seed}", str(seed))
    functional_cmd = (task.get("functional_cmd") or "").replace("{seed}", str(seed))
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
            # Before any setup: bootstrap's freshness check and the static arms' convention
            # miner must both see the task's fixture as the starting repository.
            apply_overlay(work, task.get("fixture_files"), seed)
            seeds = seeding.seed_items(task, seed, rep)
            index = task.get("needed_decision")
            if index is not None:
                # Which author's style the needed decision was written in this rep.
                row["needed_style"] = seeds[index]["style"]
            files = _FILE_CONDITIONS.get(condition)
            if files:
                _condition_c_setup(work, task["seed_decision"], files, seed_decisions=seeds)
            if condition == "claudemd_maintained":
                _maintained_setup(work)
            if condition == "docs_indexed":
                legacy = ([{"content": task["seed_decision"], "subtype": "constraint"}]
                          if task["seed_decision"] else [])
                _docs_indexed_setup(work, legacy + seeds)
            src = (contexer_sources or {}).get(condition)
            if condition in _CONTEXER_CONDITIONS or src:
                ids = _condition_b_setup(str(work), home, task["seed_decision"],
                                         Path(src) if src else None, seed_decisions=seeds,
                                         steady=steady_state)
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
        if task.get("capture_terms") and condition in _CONTEXER_CONDITIONS and task["step"] > 1:
            # Capture loop, session 2: did the rule session 1 recorded reach this agent?
            text, row["contexer_lookups"] = _contexer_received(home)
            row["needed_delivery"] = ("full" if _matches(task["capture_terms"], text,
                                                         _run_noise(work, task)) else "none")
        if needed:
            # Contexer arms only: a static rules file is loaded into the system prompt, which
            # the transcript doesn't record, so "none" there would be a false reading.
            row["needed_delivery"], row["contexer_lookups"] = _needed_delivery(home, *needed)
        # Same isolated env as the session: success must never come from developer-local
        # HOME/uv/python configuration the session couldn't see. A task with a functional
        # check is scored on both (adherence: followed the decision; functional: the change
        # works), and succeeds only when every check it has passes.
        failures = []
        for name, cmd in (("adherence", check_cmd), ("functional", functional_cmd)):
            if not cmd:
                continue
            chk = subprocess.run(cmd, shell=True, cwd=work, capture_output=True,
                                 timeout=600, env=_session_env(home, 0))
            if functional_cmd:
                row[name] = chk.returncode == 0
            if chk.returncode != 0:
                failures.append(f"[{name}] " + ((chk.stdout or b"") + (chk.stderr or b"")).decode(
                    errors="replace")[-1500:])
        row["success"] = not failures
        clarification = task.get("clarification")
        if clarification:
            # K7c: the correct outcome is to stop and name the conflict, never to implement.
            taken_cmd = clarification["side_taken_cmd"].replace("{seed}", str(seed))
            # Exit 1 alone means "no side taken"; 0 (a side) or anything else (the check crashed on
            # code the session wrote) means the session acted instead of stopping.
            taken = subprocess.run(taken_cmd, shell=True, cwd=work, capture_output=True,
                                   timeout=600, env=_session_env(home, 0)).returncode != 1
            row["clarified"] = _clarified(str(res.get("result", "")), clarification["sides"],
                                          taken)
            row["success"] = row["clarified"]
        if failures:
            # The session's folders are discarded after scoring: keep why the check failed.
            row["check_output"] = "\n".join(failures)[-3000:]
        if task.get("capture_terms") and task["step"] == 1:
            row.update(_capture_state(condition, work, home, task["capture_terms"],
                                      _run_noise(work, task)))
            _revert_code_keep_memory(work, base_sha)
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
    ap.add_argument("--steady-state", action="store_true",
                    help="complete Contexer's bootstrap during setup (steady-state sessions)")
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
                       tasks_file=Path(a.tasks_file) if a.tasks_file else None,
                       steady_state=a.steady_state))
