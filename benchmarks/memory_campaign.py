"""Memory-tool-vs-Contexer campaign runner: three arms per rep — "without" (bare,
never taught), "memory" (Claude Code's built-in memory tool, taught, no Contexer),
"with" (Contexer installed, taught, memory tool left at its default-on state per
memory_home.py's pilot finding). Reuses run.py's session/isolation plumbing but
implements its own flow: teach -> snapshot -> per-task restore -> measure, because
(unlike run.py's campaigns) every task here must start from the SAME taught state,
never from one task's leftover edits.

Tier alternates by rep (implicit sessions 0,2,4..., explicit 1,3,5...) so both
phrasings get equal reps across a run without a separate --tier flag. Teaching runs
ONE session per scripted prompt (rows are phase="teach", distinguished by
`prompt_index`): a joined multi-prompt blob exceeds contexer's
`store._MAX_DIRECTIVE_LEN` (300 chars), so the taught rule would be rejected by the
constraint-capture path and the capture-rate stat would measure the harness's own
prompt joining rather than either product. Teaching rows carry the same token
fields as measured rows: teaching has a token cost too, and the report shows it.

Isolation is asymmetric and that asymmetry is disclosed (MEMORY_PILOT.md): the
memory tool CANNOT be turned off, so the "with" arm counts the memory files each
of its sessions wrote (`memory_leak_files`) and then deletes them before the next
session and before the post-teaching snapshot. Within-session writes cannot help
the session that made them; deleting kills the two real vectors — cross-session
leakage, and contexer's own SessionStart `sync_memory` ingesting the opponent's
captures. `contaminated` is then a genuine tripwire: it flags memory files that
were present when a measured session STARTED (i.e. the sweep failed)."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from benchmarks import score
from benchmarks.fixtures.generate import build_webapi
from benchmarks.memory_home import memory_dir, memory_files
from benchmarks.otel import OtelReceiver
from benchmarks.run import (_MANAGED_SETTINGS, _append, _condition_b_setup, _fresh,
                             _run_session, _session_env, _telemetry_check, _tool_calls)

TEACHING_FILE = Path(__file__).resolve().parent / "teaching.json"
TASKS_FILE = Path(__file__).resolve().parent / "memory_tasks.json"
_SRC = Path(__file__).resolve().parent.parent  # this contexer checkout: uv's --project root
_COPY_IGNORE = shutil.ignore_patterns("tmp_pack_*", "tmp_idx_*", "tmp_rev_*", "tmp_mtimes_*")
# The ONE request-data-logging matcher in this campaign: it is both the pattern the
# enforcement task arms the commit guard with and the pattern cont-log is scored by,
# so the demonstration and the statistic can never disagree about what a violation is.
# `(\.\w+)*` covers the idiomatic `logger.info(...)` / `log.debug(...)` spellings —
# without it the pattern only ever matched a bare `log(...)`, which would have made
# the armed guard silently unfirable on the very line the enf-commit prompt asks for.
# ponytail: known ceiling — it also matches a log line that merely says the word
# "request" while logging no request data. Tightening needs an AST check, not a
# second regex; the same pattern must keep governing both sites.
# \b anchors the match to a real `log`-prefixed identifier: without it the
# pattern fires inside `catalog.get(request)`, `blog.render(request)`,
# `dialog(payload)` — words that merely contain "log".
_ENF_REGEX = r"\blog\w*(\.\w+)*\(.*(payload|request)"


def _base_row(task_id, kind, arm, rep, tier, phase, model) -> dict:
    return {"task_id": task_id, "kind": kind, "chain": "", "step": 0,
            "condition": arm, "arm": arm, "rep": rep, "model": model,
            "tier": tier, "phase": phase, "ts": time.time(), "prompt_index": 0,
            "tokens_in": 0, "tokens_out": 0, "tokens_cache_read": 0, "tokens_cache_write": 0,
            "tokens_total": 0, "cost_usd": 0.0, "turns": 0, "duration_ms": 0, "tool_calls": 0,
            "violations": 0, "rationale": 0.0, "success": False, "result_snippet": "",
            "otel_tokens_total": 0, "otel_cost_usd": 0.0, "telemetry_ok": None, "error": "",
            "capture": {}, "sup_result": "", "contaminated": False,
            "memory_leak_files": 0, "enf_outcome": "", "enf_detail": ""}


def _sweep_memory(home: Path, work: Path) -> int:
    """Count the memory files present, then delete the memory directory. Called
    after every "with"-arm session (see module docstring). Returns the count, which
    is recorded rather than discarded: how much the memory tool wrote alongside
    Contexer is an honest stat, and it is the only remaining evidence that the
    opponent's mechanism could not be disabled."""
    n = len(memory_files(home, work))
    shutil.rmtree(memory_dir(home, work), ignore_errors=True)
    return n


def _run_and_record(row: dict, work: Path, home: Path, prompt: str, claude_cmd: str,
                    model: str, rx: OtelReceiver) -> dict:
    """Runs one session and fills the run.py-shaped token/cost fields onto `row`.
    Returns the raw session result (for callers that also need res["result"])."""
    rx.reset()
    # _tool_calls counts every tool_use ever written into this HOME's transcripts.
    # run.py gets away with the raw count because each of its runs owns a fresh
    # HOME; here one HOME carries the arm's teaching sessions and is restored from
    # a snapshot that contains them, so the raw count would add teaching's tool
    # calls to every measured row of the taught arms and none to `without`.
    calls_before = _tool_calls(home)
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
               duration_ms=res.get("duration_ms", 0),
               tool_calls=max(0, _tool_calls(home) - calls_before))
    row["tokens_total"] = (row["tokens_in"] + row["tokens_out"] +
                           row["tokens_cache_read"] + row["tokens_cache_write"])
    row["result_snippet"] = str(res.get("result", ""))[:300]
    # Same 1.5s OTel flush wait run.py takes. Skipping it does not merely zero the
    # corroboration field: a PARTIAL export lands a nonzero total below tolerance,
    # which _telemetry_check reports as telemetry_ok=False — a fake disagreement.
    time.sleep(1.5)
    _telemetry_check(row, rx.snapshot())
    return res


def _find_never_log_id(work: Path, home: Path) -> tuple[str | None, str | None]:
    """Looks up the approved "never log request data" decision id in the isolated
    store, via a child `uv run python -c` (contexer's venv, not this process's).
    Returns (id, None) on success, (None, None) for the genuine "not captured/
    approved" outcome (the probe's own deliberate exit 3), or (None, error) for
    any OTHER exit — a uv resolution failure or corrupt store must not collapse
    into the same message as a real no-capture result (Important 4)."""
    code = ("from contexer import store\n"
            f"entries = store._load({str(work)!r})['entries']\n"
            "hit = next((e['id'] for e in entries "
            "if 'log request data' in store._current_content(e) "
            "and store._entry_status(e) == 'approved'), None)\n"
            "import sys\n"
            "sys.exit(3) if hit is None else print(hit)\n")
    proc = subprocess.run(["uv", "run", "python", "-c", code], cwd=_SRC,
                          env=_session_env(home, 0), capture_output=True, text=True)
    if proc.returncode == 0:
        return proc.stdout.strip(), None
    if proc.returncode == 3:
        return None, None
    return None, f"probe failed rc={proc.returncode}: {proc.stderr[-300:]}"


def _enf_commit_setup(work: Path, home: Path) -> tuple[bool, str]:
    """Arms the guard on the taught rule and installs the pre-commit hook, so the
    "with" arm's enforcement task can actually be blocked. `--project` points uv
    at this checkout's venv while `cwd=work` is what makes `contexer guard` (which
    resolves its target repo from cwd) operate on the fixture repo, not this one.
    Returns (ok, error) — error is "" on success. Both the arm and the hook-install
    subprocess results are checked (Critical 3): a silent arm/install failure would
    otherwise run the enforcement task with NO guard and score success=False
    indistinguishably from a genuine model failure."""
    decision_id, probe_err = _find_never_log_id(work, home)
    if probe_err:
        return False, f"enf setup: {probe_err}"
    if not decision_id:
        return False, "enf setup: taught rule not captured/approved"
    env = _session_env(home, 0)
    arm = subprocess.run(["uv", "run", "--project", str(_SRC), "contexer", "guard", "arm",
                         decision_id, "--regex", _ENF_REGEX, "--flags", "i"],
                        cwd=work, env=env, capture_output=True, text=True)
    if arm.returncode != 0:
        return False, f"enf setup: guard arm failed rc={arm.returncode}: {arm.stderr[-300:]}"
    hook = subprocess.run(["uv", "run", "--project", str(_SRC), "contexer", "guard", "--install-hook"],
                          cwd=work, env=env, capture_output=True, text=True)
    if hook.returncode != 0:
        return False, f"enf setup: guard install-hook failed rc={hook.returncode}: {hook.stderr[-300:]}"
    return True, ""


def _score_task(row: dict, task: dict, work: Path, arm: str, changed: dict,
                check_cmd: str, chk, res: dict) -> None:
    scorer = task["scorer"]
    if scorer == "sup_current":
        row["sup_result"] = score.sup_current_score(res.get("result", ""))
        row["success"] = row["sup_result"] == "pass"
    elif scorer == "rationale":
        row["rationale"] = score.rationale_score(res.get("result", ""), task["gold"])
        row["success"] = row["rationale"] >= 1.0
    elif scorer == "violations":
        # cont-log tests the TAUGHT rule ("never log request data"), so it is scored
        # against what this session actually wrote — not against the fixture's
        # snake_case/type-hint baseline, which the teaching script never mentions.
        # `_ENF_REGEX` is the single request-logging matcher in this module; the
        # enforcement task's armed guard uses the very same pattern.
        # `changed` is snapshotted by the caller BEFORE check_cmd runs: `uv run
        # pytest` leaves uv.lock + tests/__pycache__/*.pyc untracked in the
        # fixture, which would make this dict non-empty for a session that
        # edited nothing — the exact false "compliant" the guard below prevents.
        if not changed:
            # A session that edits nothing trivially violates nothing. Without this
            # it would score violations=0 + green pytest = "compliant".
            row["error"] = "cont-log: no files changed"
            row["success"] = False
            return
        row["violations"] = sum(1 for text in changed.values()
                                if re.search(_ENF_REGEX, text, re.I))
        row["success"] = row["violations"] == 0 and (not check_cmd or chk.returncode == 0)
    elif scorer == "enforcement":
        msgs = subprocess.run(["git", "-C", str(work), "log", "--format=%s"],
                              capture_output=True, text=True).stdout
        committed = "debug logging" in msgs
        # "blocked" is only honest if a violating edit actually reached the index and
        # the pre-commit hook stopped it there. A model that simply declined to write
        # the log line is a different (and non-demonstrating) outcome.
        staged = subprocess.run(["git", "-C", str(work), "diff", "--cached"],
                                capture_output=True, text=True).stdout
        if committed:
            row["enf_outcome"] = "committed"
        elif not re.search(_ENF_REGEX, staged, re.I):
            row["enf_outcome"] = "no violating change attempted"
        else:
            # A staged violation that never became a commit is NOT self-evidently
            # "blocked" — the session may simply never have run `git commit`. The
            # only honest way to label it is to attempt the commit ourselves and
            # read the hook's verdict, so the demonstration rests on an observed
            # rejection rather than on an absence.
            att = subprocess.run(["git", "-C", str(work), "commit", "-m", "debug logging"],
                                 capture_output=True, text=True, timeout=120)
            row["enf_outcome"] = ("blocked" if att.returncode != 0
                                  else "committed on retry (guard did not block)")
            row["enf_detail"] = (att.stderr or att.stdout)[-300:]
        row["success"] = arm == "with" and row["enf_outcome"] == "blocked"


def _measure_task(task: dict, work: Path, home: Path, arm: str, tier: str, rep: int,
                  claude_cmd: str, seed: int, model: str, rx: OtelReceiver,
                  capture: dict) -> dict:
    row = _base_row(task["id"], task["kind"], arm, rep, tier, "measure", model)
    row["capture"] = capture
    if arm == "with":
        # PRE-session, deliberately: every with-arm session is swept clean afterwards
        # (`_sweep_memory`), so anything here survived the sweep or the snapshot and
        # is genuine cross-session leakage — the tripwire the spec asks for. Counting
        # post-session instead would flag every row, since the tool cannot be disabled.
        row["contaminated"] = bool(memory_files(home, work))
    try:
        if task["scorer"] == "enforcement" and arm == "with":
            ok, err = _enf_commit_setup(work, home)
            if not ok:
                row["error"] = err
                return row  # skip the session entirely; success stays False

        prompt = task["prompt"].replace("{seed}", str(seed))
        base_sha = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                                  capture_output=True, text=True).stdout.strip() or "HEAD"
        res = _run_and_record(row, work, home, prompt, claude_cmd, model, rx)
        if row["error"]:
            return row
        # Snapshot what the SESSION changed before check_cmd runs — check_cmd
        # (`uv run pytest`) creates untracked build artifacts of its own. run.py
        # scores before check_cmd for the same reason.
        changed = score.changed_files(str(work), base_sha)
        check_cmd = task["check_cmd"].replace("{seed}", str(seed))
        chk = None
        if check_cmd:
            chk = subprocess.run(check_cmd, shell=True, cwd=work, capture_output=True,
                                 timeout=600, env=_session_env(home, 0))
        _score_task(row, task, work, arm, changed, check_cmd, chk, res)
    except Exception as exc:  # a failed run is a data point, never a crash (Critical 2)
        row["error"] = repr(exc)
    finally:
        if arm == "with":
            row["memory_leak_files"] = _sweep_memory(home, work)
        elif arm == "memory":
            # Post-run, always: the memory arm's contamination is contexer state
            # appearing where contexer was never installed, which can only be
            # created DURING the session.
            c = score.capture_stats(home, work)
            row["contaminated"] = (home / ".contexer").exists() and c["contexer_entries"] > 0
    return row


def _restore(home: Path, work: Path, snap_home: Path, snap_work: Path) -> None:
    shutil.rmtree(home)
    shutil.rmtree(work)
    shutil.copytree(snap_home, home)
    shutil.copytree(snap_work, work, ignore=_COPY_IGNORE)


def _run_arm(out: Path, td: Path, golden: Path, tasks: list, teaching: list,
            arm: str, tier: str, rep: int, claude_cmd: str, seed: int, model: str,
            rx: OtelReceiver) -> None:
    tag = f"{arm}-{tier}-{rep}"
    work, home = _fresh(td, golden, tag)
    # No write_home_settings call: the pilot proved it writes {} in BOTH arms (no
    # disable key exists), so it bought nothing — and running it AFTER
    # _condition_b_setup overwrote the five hook events `contexer install` had just
    # written into the same file, silently measuring the with arm with Contexer's
    # entire deterministic mechanism switched off. Never reintroduce a post-install
    # settings write here.
    try:
        if arm == "with":
            _condition_b_setup(str(work), home, "")
        # "memory"/"without": nothing — memory is default-on, the bare arm never teaches.
    except Exception as exc:  # a paid multi-hour run must not abort on a flaky install
        row = _base_row(f"setup-{arm}", "setup", arm, rep, tier, "setup", model)
        row["error"] = f"arm setup failed: {exc!r}"
        _append(out, row)
        return

    if arm != "without":
        sessions = sorted((s for s in teaching if s["tier"] == tier), key=lambda s: s["session"])
        for s in sessions:
            # One session PER PROMPT: a joined blob exceeds store._MAX_DIRECTIVE_LEN
            # and the taught rule would never land as an approved constraint.
            for i, prompt in enumerate(s["prompts"]):
                row = _base_row(f"teach-s{s['session']}-p{i}", "teach", arm, rep, tier,
                                "teach", model)
                row["prompt_index"] = i
                try:
                    _run_and_record(row, work, home, prompt, claude_cmd, model, rx)
                except Exception as exc:  # a failed run is a data point, never a crash
                    row["error"] = repr(exc)
                if arm == "with":
                    row["memory_leak_files"] = _sweep_memory(home, work)
                _append(out, row)
    capture = score.capture_stats(home, work)

    snap_home, snap_work = td / f"snap-h-{tag}", td / f"snap-w-{tag}"
    try:
        try:
            shutil.copytree(home, snap_home)
            shutil.copytree(work, snap_work, ignore=_COPY_IGNORE)
        except Exception as exc:
            row = _base_row(f"snapshot-{arm}", "setup", arm, rep, tier, "setup", model)
            row["error"] = f"arm snapshot failed: {exc!r}"
            _append(out, row)
            return
        for task in tasks:
            _restore(home, work, snap_home, snap_work)
            row = _measure_task(task, work, home, arm, tier, rep, claude_cmd, seed, model,
                                rx, capture)
            _append(out, row)
    finally:
        # 3 arms x 16 reps of full .claude transcripts is a lot of disk to hold for
        # a run's whole duration; each arm-rep's snapshots die with its task loop.
        shutil.rmtree(snap_home, ignore_errors=True)
        shutil.rmtree(snap_work, ignore_errors=True)


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
    if out.exists() and out.stat().st_size > 0:
        # _append (below) is pure append-mode and campaign.json is unconditionally
        # overwritten just below — a rerun into an existing --out would silently mix
        # this run's rows in under the OLD run's now-discarded metadata (model, reps,
        # frozen teaching hash), and neither report.py nor validate.py cross-checks
        # row count against campaign.json's declared reps to catch it. Fail loud
        # instead of aggregating two heterogeneous runs as if they were one.
        raise FileExistsError(
            f"{out} already has rows from a previous run. Rerunning into the same "
            f"--out would silently mix runs under mismatched metadata. Pick a fresh "
            f"--out directory, or remove the existing runs.jsonl first.")
    tasks = json.loads(TASKS_FILE.read_text())
    teaching = json.loads(TEACHING_FILE.read_text())
    (out_dir / "campaign.json").write_text(json.dumps({
        "model": model, "seed": seed, "reps": reps, "conditions": list(conditions),
        "started_at": datetime.now(timezone.utc).isoformat(),
        "claude_version": _claude_version(claude_cmd),
        "managed_settings_present": _MANAGED_SETTINGS.exists(),
        "teaching_frozen_sha": _sha256(TEACHING_FILE),
        "memory_tasks_sha": _sha256(TASKS_FILE)}, indent=2))

    rx = OtelReceiver()
    rx.start()
    try:
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            golden = build_webapi(td / "golden", seed=seed)
            for rep in range(reps):
                tier = "implicit" if rep % 2 == 0 else "explicit"
                # An arm's ~9 sessions run consecutively (teach -> snapshot ->
                # measure cannot be interleaved across arms without holding three
                # HOMEs live), so within a rep the last arm always runs latest.
                # Reversing on odd reps keeps that position from being a fixed
                # property of any one arm across the campaign.
                order = conditions if rep % 2 == 0 else list(reversed(conditions))
                for arm in order:
                    _run_arm(out, td, golden, tasks, teaching, arm, tier, rep,
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
