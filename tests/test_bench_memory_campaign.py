import json
import stat
from pathlib import Path

import benchmarks.memory_campaign as mc
from benchmarks.memory_campaign import run_memory_campaign

STUB = """#!/bin/bash
echo '{"result": "DynamoDB\\nscaling", "usage": {"input_tokens": 10, "output_tokens": 5}, "num_turns": 1, "total_cost_usd": 0.001, "duration_ms": 5}'
"""

def _stub(tmp_path):
    p = tmp_path / "claude"
    p.write_text(STUB)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)

def test_rerun_into_existing_out_dir_fails_loud(tmp_path):
    """Greptile P1: _append is pure append-mode and campaign.json is unconditionally
    overwritten, so a second run into the same --out would silently mix this run's
    rows in under the new run's (mismatched) metadata. Must refuse instead."""
    import pytest
    out_dir = tmp_path / "out"
    first = run_memory_campaign(out_dir, reps=1, claude_cmd=_stub(tmp_path),
                                model="stub", conditions=("without",))
    rows_after_first = first.read_text().splitlines()
    assert rows_after_first  # sanity: the first run actually wrote something

    with pytest.raises(FileExistsError):
        run_memory_campaign(out_dir, reps=1, claude_cmd=_stub(tmp_path),
                            model="stub", conditions=("without",))

    # the guard must fire before any row is appended or campaign.json overwritten
    assert first.read_text().splitlines() == rows_after_first


def test_zero_byte_existing_file_also_blocks(tmp_path):
    """Greptile follow-up: the guard is an atomic O_EXCL claim, not a size check —
    a check-then-write (e.g. `exists() and st_size > 0`) leaves a race window where
    two concurrent campaigns can both pass before either writes. A pre-existing
    EMPTY file must block too, since a size-based check would let it through and
    silently reintroduce that race."""
    import pytest
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    (out_dir / "runs.jsonl").touch()  # zero bytes — would pass an st_size>0 check

    with pytest.raises(FileExistsError):
        run_memory_campaign(out_dir, reps=1, claude_cmd=_stub(tmp_path),
                            model="stub", conditions=("without",))
    assert not (out_dir / "campaign.json").exists()  # never got that far


def test_campaign_writes_rows_with_new_fields(tmp_path):
    out = run_memory_campaign(tmp_path / "out", reps=1, claude_cmd=_stub(tmp_path),
                              model="stub", conditions=("without", "memory", "with"))
    rows = [json.loads(ln) for ln in out.read_text().splitlines()]
    arms = {r["arm"] for r in rows}
    assert arms == {"without", "memory", "with"}
    assert any(r["phase"] == "teach" for r in rows)       # teaching rows recorded
    assert all(r["phase"] == "measure" or r["arm"] != "without" for r in rows)  # bare never teaches
    sup = [r for r in rows if r["task_id"] == "sup-current" and r["phase"] == "measure"]
    assert all(r["sup_result"] in ("pass", "fail", "review") for r in sup)
    assert all("tier" in r and "capture" in r and "contaminated" in r for r in rows)


def test_teaching_runs_one_session_per_prompt(tmp_path):
    """Critical 4: joined session prompts exceed store._MAX_DIRECTIVE_LEN (300), so
    the taught rule would never land as an approved constraint. One session per
    prompt, each row distinguishable by prompt_index."""
    from contexer import store
    teaching = json.loads(Path("benchmarks/teaching.json").read_text())
    for s in teaching:  # the property the split relies on
        assert all(len(p) < store._MAX_DIRECTIVE_LEN for p in s["prompts"])

    out = run_memory_campaign(tmp_path / "out", reps=1, claude_cmd=_stub(tmp_path),
                              model="stub", conditions=("memory",))
    rows = [json.loads(ln) for ln in out.read_text().splitlines()]
    teach = [r for r in rows if r["phase"] == "teach"]
    n_prompts = sum(len(s["prompts"]) for s in teaching if s["tier"] == "implicit")
    assert len(teach) == n_prompts
    assert len({r["task_id"] for r in teach}) == n_prompts
    assert {r["prompt_index"] for r in teach} == {0, 1, 2, 3}


class _Chk:
    def __init__(self, rc=0):
        self.returncode = rc


class TestContLogScoring:
    """Critical 2: cont-log must test the TAUGHT rule ("never log request data") by
    scanning what the session wrote, not the fixture's naming/type-hint baseline."""
    TASK = {"scorer": "violations", "gold": []}

    def _score(self, tmp_path, changed, chk_rc=0):
        row = mc._base_row("cont-log", "continuity", "with", 0, "implicit", "measure", "m")
        mc._score_task(row, self.TASK, tmp_path, "with", changed,
                       "pytest", _Chk(chk_rc), {})
        return row

    def test_request_logging_is_a_violation(self, tmp_path):
        row = self._score(tmp_path,
                          {"app/core.py": "def f(request):\n    logger.info(request.payload)\n"})
        assert row["violations"] == 1 and row["success"] is False

    def test_clean_edit_passes(self, tmp_path):
        row = self._score(tmp_path,
                          {"app/core.py": "def f(x):\n    log.info('called f in %sms', t)\n"})
        assert row["violations"] == 0 and row["success"] is True

    def test_clean_edit_still_fails_a_red_test_suite(self, tmp_path):
        row = self._score(tmp_path,
                          {"app/core.py": "def f(x):\n    log.info('timing')\n"}, chk_rc=1)
        assert row["success"] is False

    def test_no_op_session_cannot_score_compliant(self, tmp_path):
        row = self._score(tmp_path, {})
        assert row["success"] is False
        assert "cont-log: no files changed" in row["error"]


def _git_repo(tmp_path):
    import subprocess
    r = tmp_path / "repo"
    r.mkdir()
    for cmd in (["init", "-q"], ["config", "user.email", "b@e"], ["config", "user.name", "b"]):
        subprocess.run(["git", "-C", str(r)] + cmd, check=True, capture_output=True)
    return r


class TestEnforcementLabelling:
    """Minor: "blocked" is only honest when a violating edit reached the index."""
    TASK = {"scorer": "enforcement", "gold": []}

    def _outcome(self, work):
        row = mc._base_row("enf-commit", "enforcement", "with", 0, "implicit", "measure", "m")
        mc._score_task(row, self.TASK, work, "with", {}, "", None, {})
        return row["enf_outcome"]

    @staticmethod
    def _stage_violation(r):
        import subprocess
        (r / "core.py").write_text("def f(request):\n    log.debug(request.payload)\n")
        subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)

    @staticmethod
    def _reject_hook(r):
        h = r / ".git" / "hooks" / "pre-commit"
        h.write_text("#!/bin/sh\necho 'contexer: blocked' >&2\nexit 1\n")
        h.chmod(h.stat().st_mode | stat.S_IEXEC)

    def test_staged_violation_reads_as_blocked(self, tmp_path):
        r = _git_repo(tmp_path)
        self._stage_violation(r)
        self._reject_hook(r)
        assert self._outcome(r) == "blocked"

    def test_staged_violation_with_no_hook_is_not_called_blocked(self, tmp_path):
        """The label must come from an observed rejection, not from the absence of a
        commit: a session that stages a violation and simply never runs `git commit`
        would otherwise be published as a successful enforcement demonstration."""
        r = _git_repo(tmp_path)
        self._stage_violation(r)
        assert self._outcome(r) == "committed on retry (guard did not block)"

    def test_declining_to_edit_is_not_blocked(self, tmp_path):
        import subprocess
        r = _git_repo(tmp_path)
        (r / "core.py").write_text("def f(x):\n    return x\n")
        subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
        assert self._outcome(r) == "no violating change attempted"


def _fake_install(repo, home, seed, source=None):
    """Stands in for `contexer install`: writes the one thing that matters here,
    a hooks block in <home>/.claude/settings.json."""
    p = Path(home) / ".claude" / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"hooks": {"SessionStart": [{"hooks": []}]}}))


def test_with_arm_setup_keeps_contexer_hooks(tmp_path, monkeypatch):
    """Critical 1: `contexer install` writes five hook events into
    <home>/.claude/settings.json; any settings write AFTER it destroys them and the
    with arm silently measures Contexer with its mechanism switched off. Asserts
    the invariant where it counts — at every session the with arm actually runs."""
    real_run = mc._run_and_record
    seen = []

    def spy(row, work, home, *a, **kw):
        p = Path(home) / ".claude" / "settings.json"
        seen.append(json.loads(p.read_text()) if p.exists() else {})
        return real_run(row, work, home, *a, **kw)

    monkeypatch.setattr(mc, "_condition_b_setup", _fake_install)
    monkeypatch.setattr(mc, "_run_and_record", spy)
    run_memory_campaign(tmp_path / "out", reps=1, claude_cmd=_stub(tmp_path),
                        model="stub", conditions=("with",))
    assert seen, "the with arm ran no sessions"
    assert all("hooks" in s for s in seen)


def test_with_arm_sweeps_memory_and_records_the_count(tmp_path, monkeypatch):
    """Critical 3: memory cannot be disabled, so the with arm deletes what the
    memory tool wrote between sessions (killing cross-session leakage and
    contexer's sync_memory ingesting the opponent's captures) while preserving the
    count as an honest stat."""
    real_run = mc._run_and_record
    started_dirty = []

    def writing_run(row, work, home, *a, **kw):
        started_dirty.append(mc.memory_dir(home, work).exists())
        d = mc.memory_dir(home, work)
        d.mkdir(parents=True, exist_ok=True)
        (d / "fact.md").write_text("---\ndescription: x\n---\nnever log request data")
        return real_run(row, work, home, *a, **kw)

    monkeypatch.setattr(mc, "_condition_b_setup", _fake_install)
    monkeypatch.setattr(mc, "_run_and_record", writing_run)
    out = run_memory_campaign(tmp_path / "out", reps=1, claude_cmd=_stub(tmp_path),
                              model="stub", conditions=("with",))
    rows = [json.loads(ln) for ln in out.read_text().splitlines()]
    assert started_dirty and not any(started_dirty)   # no session inherited memory files
    # every session that ran recorded its one leaked file, and nothing survived
    assert sum(1 for r in rows if r["memory_leak_files"] == 1) == len(started_dirty)
    assert not any(r["contaminated"] for r in rows)


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
    p = tmp_path / "claude_crashy"
    p.write_text(_CRASH_STUB)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)

    out = run_memory_campaign(tmp_path / "out2", reps=1, claude_cmd=str(p),
                              model="stub", conditions=("without", "memory"))
    rows = [json.loads(ln) for ln in out.read_text().splitlines()]
    # without: 4 measured calls; memory: 5 teach (one session per scripted prompt:
    # 4 in teaching session 1 + 1 in session 2) + 4 measured = 13 total (the
    # campaign.json claude_version probe eats one of the stub's 3 healthy calls
    # first, so the exact split is an implementation detail — the property under
    # test is: full completion, some rows crash, some don't, nothing aborts).
    assert len(rows) == 13
    errored = sum(1 for r in rows if r["error"])
    assert 0 < errored < 13


def test_enf_regex_needs_a_word_boundary():
    """`catalog`/`blog`/`dialog` all contain "log"; without \\b the armed guard
    would block ordinary code and cont-log would score false violations."""
    import re
    from benchmarks.memory_campaign import _ENF_REGEX
    for clean in ("catalog.get(request)", "blog.render(request)", "dialog(payload)"):
        assert not re.search(_ENF_REGEX, clean, re.I), clean
    for dirty in ("logger.info(payload)", "log.debug(request.body)", "self.log(payload)"):
        assert re.search(_ENF_REGEX, dirty, re.I), dirty


def test_check_cmd_artifacts_cannot_fake_a_changed_file(tmp_path, monkeypatch):
    """HIGH: check_cmd (`uv run pytest`) leaves uv.lock + __pycache__/*.pyc
    untracked in the fixture. Scoring after it ran made `changed` non-empty for a
    session that edited nothing, so the no-op guard never fired and a do-nothing
    run scored compliant."""
    r = _git_repo(tmp_path)
    (r / "seed.py").write_text("x = 1\n")
    import subprocess
    subprocess.run(["git", "-C", str(r), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(r), "commit", "-qm", "seed"], check=True, capture_output=True)

    monkeypatch.setattr(mc, "_run_and_record", lambda row, *a, **kw: {"result": ""})
    task = {"id": "cont-log", "kind": "continuity", "scorer": "violations", "gold": [],
            "prompt": "p", "check_cmd": "touch build_artifact.py"}
    row = mc._measure_task(task, r, tmp_path / "home", "without", "implicit", 0,
                           "claude", 0, "m", _Rx(), {})
    assert (r / "build_artifact.py").exists(), "check_cmd did not run"
    assert row["success"] is False
    assert "cont-log: no files changed" in row["error"]


class _Rx:
    port = 0
    def reset(self): pass
    def snapshot(self): return {"tokens": {}, "cost_usd": 0.0}


def test_tool_calls_excludes_the_arms_teaching_sessions(tmp_path, monkeypatch):
    """MEDIUM: _tool_calls counts every tool_use ever written into a HOME. One HOME
    carries an arm's teaching transcripts, so the raw count would credit teaching's
    tool calls to every measured row of the taught arms and none to `without`."""
    home = tmp_path / "home"
    proj = home / ".claude" / "projects" / "p"
    proj.mkdir(parents=True)
    (proj / "old.jsonl").write_text('{"type":"tool_use"}\n' * 7)   # teaching's legacy

    def fake_session(work, prompt, cmd, env, model):
        (proj / "new.jsonl").write_text('{"type":"tool_use"}\n' * 2)
        return {"result": "ok", "usage": {}, "num_turns": 1, "total_cost_usd": 0.0,
                "duration_ms": 1}

    monkeypatch.setattr(mc, "_run_session", fake_session)
    monkeypatch.setattr(mc.time, "sleep", lambda s: None)
    row = mc._base_row("t", "k", "with", 0, "implicit", "measure", "m")
    mc._run_and_record(row, tmp_path, home, "p", "claude", "m", _Rx())
    assert row["tool_calls"] == 2
