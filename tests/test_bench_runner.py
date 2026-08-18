"""Runner logic tested against a stub 'claude' binary - no network, no tokens."""
import json
import os
import stat

import pytest

from benchmarks.run import run_campaign, _session_env

STUB = """#!/bin/sh
mkdir -p app 2>/dev/null || true
printf 'def added_by_stub(x: int) -> dict:\\n    return {}\\n' > app/stub_edit.py
cat <<EOF
{"result": "args:[$*] Done. We picked it for transactional integrity.", "usage": {"input_tokens": 1000, "output_tokens": 200, "cache_creation_input_tokens": 50, "cache_read_input_tokens": 3000}, "total_cost_usd": 0.01, "num_turns": 3, "duration_ms": 4200, "modelUsage": {}, "session_id": "stub"}
EOF
"""


STUB_AUTH_FAIL = """#!/bin/sh
cat <<'EOF'
{"result": "Not logged in - Please run /login", "is_error": true, "terminal_reason": "api_error", "usage": {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}, "total_cost_usd": 0, "num_turns": 1, "duration_ms": 40, "session_id": "stub"}
EOF
"""


# Reports what each condition's session actually sees: whether contexer hooks were
# installed in the isolated HOME and whether a CLAUDE.md exists in the work repo.
STUB_PROBE = """#!/bin/sh
CS=no; [ -f "$HOME/.claude/settings.json" ] && CS=yes
CM=no; [ -f CLAUDE.md ] && CM=yes
AM=no; [ -f AGENTS.md ] && AM=yes
cat <<EOF
{"result": "probe settings:$CS claudemd:$CM agents:$AM", "usage": {"input_tokens": 10, "output_tokens": 5, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}, "total_cost_usd": 0.001, "num_turns": 1, "duration_ms": 5, "session_id": "stub"}
EOF
"""


def _make_stub(tmp_path, content):
    p = tmp_path / "claude"
    p.write_text(content)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def _run_stubbed_campaign(*args, **kwargs):
    """Run the benchmark harness against a local test stub."""
    return run_campaign(*args, **kwargs, wait_for_otel=False)


@pytest.fixture
def stub_claude(tmp_path):
    return _make_stub(tmp_path, STUB)


@pytest.fixture
def stub_claude_fail(tmp_path):
    return _make_stub(tmp_path, STUB_AUTH_FAIL)


@pytest.fixture
def stub_claude_probe(tmp_path):
    return _make_stub(tmp_path, STUB_PROBE)


@pytest.fixture(autouse=True)
def _git_isolated(monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")


class TestSessionEnv:
    def test_allowlist_strips_config_overrides(self, tmp_path, monkeypatch):
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/should/not/leak")
        monkeypatch.setenv("XDG_CONFIG_HOME", "/nor/this")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
        env = _session_env(tmp_path, otel_port=0)
        assert "CLAUDE_CONFIG_DIR" not in env and "XDG_CONFIG_HOME" not in env
        assert env["HOME"] == str(tmp_path)
        assert env["ANTHROPIC_API_KEY"] == "sk-test"

    def test_otel_vars_present_when_port_given(self, tmp_path):
        env = _session_env(tmp_path, otel_port=4318)
        assert env["CLAUDE_CODE_ENABLE_TELEMETRY"] == "1"
        assert env["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://127.0.0.1:4318"
        assert env["OTEL_EXPORTER_OTLP_PROTOCOL"] == "http/json"


class TestRunCampaign:
    def test_stubbed_runs_skip_otel_flush_wait(self, tmp_path, stub_claude, monkeypatch):
        import benchmarks.run as run_mod

        sleeps = []
        monkeypatch.setattr(run_mod.time, "sleep", sleeps.append)

        _run_stubbed_campaign(tmp_path / "stub", reps=1, task_ids=["rat-storage"],
                              claude_cmd=stub_claude, seed=5, conditions=("without",))

        assert sleeps == []

    def test_live_runs_wait_for_otel_flush(self, tmp_path, stub_claude, monkeypatch):
        import benchmarks.run as run_mod

        sleeps = []
        monkeypatch.setattr(run_mod.time, "sleep", sleeps.append)

        run_campaign(tmp_path / "live", reps=1, task_ids=["rat-storage"],
                     claude_cmd=stub_claude, seed=5, conditions=("without",))

        assert sleeps == [1.5]

    def test_rows_cache_fields_and_totals(self, tmp_path, stub_claude):
        out = _run_stubbed_campaign(tmp_path / "a", reps=1, task_ids=["rat-storage", "conv-endpoint"],
                                    claude_cmd=stub_claude, seed=5, model="stub-model")
        rows = [json.loads(line) for line in out.read_text().splitlines()]
        assert len(rows) == 6  # 2 tasks x 3 conditions x 1 rep
        for r in rows:
            assert r["condition"] in ("without", "claudemd", "with") and r["error"] == ""
            assert isinstance(r["ts"], float) and r["ts"] > 0
            assert r["tokens_cache_read"] == 3000 and r["tokens_cache_write"] == 50
            assert r["tokens_total"] == 1000 + 200 + 50 + 3000
            assert r["telemetry_ok"] is None  # stub exports no OTel
        rat = [r for r in rows if r["task_id"] == "rat-storage"]
        assert all(r["rationale"] == 1.0 for r in rat)
        # Non-interactive sessions must be able to write files.
        assert all("--dangerously-skip-permissions" in r["result_snippet"] for r in rat)

    def test_conditions_interleaved_with_ts(self, tmp_path, stub_claude):
        # Rep outermost, condition innermost: conditions alternate in time rather
        # than running as one block per condition (red-team #2).
        out = _run_stubbed_campaign(tmp_path / "i", reps=2,
                                    task_ids=["rat-storage", "conv-endpoint"],
                                    claude_cmd=stub_claude, seed=5,
                                    conditions=("without", "claudemd"))
        rows = [json.loads(line) for line in out.read_text().splitlines()]
        assert [r["condition"] for r in rows] == ["without", "claudemd"] * 4
        # per rep: task1 both conditions, then task2 both conditions
        assert [(r["rep"], r["task_id"], r["condition"]) for r in rows[:4]] == [
            (0, "conv-endpoint", "without"), (0, "conv-endpoint", "claudemd"),
            (0, "rat-storage", "without"), (0, "rat-storage", "claudemd")]
        ts = [r["ts"] for r in rows]
        assert ts == sorted(ts) and len(set(ts)) == len(ts)

    def test_chain_steps_share_repo_and_home(self, tmp_path, stub_claude):
        out = _run_stubbed_campaign(
            tmp_path / "b", reps=1, task_ids=["chain-1-cache", "chain-2-list", "chain-3-audit"],
            claude_cmd=stub_claude, seed=5)
        rows = [json.loads(line) for line in out.read_text().splitlines()]
        chain_rows = [r for r in rows if r["chain"] == "orders"]
        assert sorted({r["step"] for r in chain_rows}) == [1, 2, 3]
        # Steps stay sequential within a condition; conditions cycle within the rep.
        assert [(r["condition"], r["step"]) for r in chain_rows] == [
            ("without", 1), ("without", 2), ("without", 3),
            ("claudemd", 1), ("claudemd", 2), ("claudemd", 3),
            ("with", 1), ("with", 2), ("with", 3)]

    def test_claudemd_condition_writes_claude_md_without_contexer(
            self, tmp_path, stub_claude_probe):
        out = _run_stubbed_campaign(
            tmp_path / "p", reps=1, task_ids=["rat-storage"],
            claude_cmd=stub_claude_probe, seed=5,
            conditions=("without", "claudemd", "with", "claudemd_with"))
        rows = {r["condition"]: r for r in
                (json.loads(line) for line in out.read_text().splitlines())}
        assert "settings:no claudemd:no" in rows["without"]["result_snippet"]
        # the honest competitor: a CLAUDE.md, but NO contexer hooks in its HOME
        assert "settings:no claudemd:yes" in rows["claudemd"]["result_snippet"]
        assert "settings:yes claudemd:no" in rows["with"]["result_snippet"]
        # condition D: contexer layered on top of a pre-existing CLAUDE.md
        assert "settings:yes claudemd:yes" in rows["claudemd_with"]["result_snippet"]

    def test_agentsmd_conditions_write_right_files(self, tmp_path, stub_claude_probe):
        # agentsmd: only AGENTS.md; claudemd_agentsmd: both files, no contexer.
        out = _run_stubbed_campaign(
            tmp_path / "am", reps=1, task_ids=["rat-storage"],
            claude_cmd=stub_claude_probe, seed=5,
            conditions=("agentsmd", "claudemd_agentsmd"))
        rows = {r["condition"]: r for r in
                (json.loads(line) for line in out.read_text().splitlines())}
        assert "settings:no claudemd:no agents:yes" in rows["agentsmd"]["result_snippet"]
        assert "settings:no claudemd:yes agents:yes" in rows["claudemd_agentsmd"]["result_snippet"]

    def test_claudemd_with_condition_gets_both(self, tmp_path, stub_claude_probe):
        # Condition D: contexer layered on a pre-existing CLAUDE.md - the adoption
        # question for repos that already maintain one.
        out = _run_stubbed_campaign(
            tmp_path / "d", reps=1, task_ids=["rat-storage"],
            claude_cmd=stub_claude_probe, seed=5,
            conditions=("claudemd_with",))
        row = json.loads(out.read_text().splitlines()[0])
        assert "settings:yes claudemd:yes" in row["result_snippet"]

    def test_condition_c_setup_content(self, tmp_path):
        from benchmarks.fixtures.generate import build_webapi
        from benchmarks.run import _condition_c_setup
        from contexer import miner
        work = build_webapi(tmp_path / "w", seed=5)
        _condition_c_setup(work, "Chose Postgres over MySQL for transactional integrity")
        text = (work / "CLAUDE.md").read_text()
        # single file carries everything: overview + decision + conventions
        assert "## Key decisions" in text and "## Code style" in text
        assert "- Chose Postgres over MySQL for transactional integrity" in text
        mined = miner.mine_conventions(str(work))
        assert mined  # the fixture must yield measurable conventions
        for conv in mined:
            assert f"- {conv['content']}" in text

    def test_condition_c_setup_realistic_split(self, tmp_path):
        # Both files: CLAUDE.md = project info + decision-shaped seeds;
        # AGENTS.md = conventions + testing + rule-shaped seeds.
        from benchmarks.fixtures.generate import build_webapi
        from benchmarks.run import _condition_c_setup
        from contexer import miner
        work = build_webapi(tmp_path / "w2", seed=5)
        _condition_c_setup(work, "Never log function arguments or record ids",
                           ("CLAUDE.md", "AGENTS.md"))
        claude = (work / "CLAUDE.md").read_text()
        agents = (work / "AGENTS.md").read_text()
        assert "## Architecture" in claude and "Never log" not in claude
        assert "## Rules" in agents and "- Never log function arguments" in agents
        for conv in miner.mine_conventions(str(work)):
            assert f"- {conv['content']}" in agents and conv["content"] not in claude

        work3 = build_webapi(tmp_path / "w3", seed=5)
        _condition_c_setup(work3, "Chose Postgres over MySQL for transactional integrity",
                           ("CLAUDE.md", "AGENTS.md"))
        assert "## Key decisions" in (work3 / "CLAUDE.md").read_text()
        assert "Postgres" not in (work3 / "AGENTS.md").read_text()

    def test_campaign_metadata_written(self, tmp_path, stub_claude):
        out = _run_stubbed_campaign(
            tmp_path / "c", reps=1, task_ids=["rat-storage"],
            claude_cmd=stub_claude, seed=5, model="stub-model")
        meta = json.loads((out.parent / "campaign.json").read_text())
        assert meta["model"] == "stub-model"
        assert isinstance(meta["managed_settings_present"], bool)

    def test_failed_session_recorded_as_errored_row(self, tmp_path, stub_claude_fail):
        # An auth/API failure returns well-formed JSON with is_error - it must land
        # as an errored row (excluded from medians), never a clean zero-token row.
        out = _run_stubbed_campaign(
            tmp_path / "e", reps=1, task_ids=["rat-storage"],
            claude_cmd=stub_claude_fail, seed=5)
        rows = [json.loads(line) for line in out.read_text().splitlines()]
        assert rows and all(r["error"].startswith("session error (api_error)") for r in rows)
        assert all("Please run /login" in r["error"] for r in rows)

    def test_fresh_dirs_are_canonical(self, tmp_path, stub_claude):
        # macOS /var/folders is a symlink to /private/var; the store slug must be
        # computed from the canonical path or session hooks find an empty store.
        from benchmarks.run import _fresh
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        link.symlink_to(real, target_is_directory=True)
        golden = tmp_path / "golden"
        golden.mkdir()
        (golden / "f.txt").write_text("x")
        work, home = _fresh(link, golden, "t")
        assert str(work) == str(work.resolve()) and str(home) == str(home.resolve())
        assert "link" not in work.parts

    def test_home_not_leaked(self, tmp_path, stub_claude):
        real_home = os.environ.get("HOME")
        _run_stubbed_campaign(
            tmp_path / "d", reps=1, task_ids=["rat-storage"], claude_cmd=stub_claude, seed=5)
        assert os.environ.get("HOME") == real_home


class TestCheckIsolation:
    def test_check_cmd_runs_in_isolated_env(self, tmp_path, stub_claude, monkeypatch):
        # success must be judged in the same isolated HOME the session saw - a check
        # that would only pass against the developer's real HOME must fail (P2).
        import benchmarks.run as run_mod
        real_home = os.environ["HOME"]
        tasks = [{"id": "env-probe", "kind": "convention", "chain": "", "step": 0,
                  "prompt": "noop", "gold": [], "seed_decision": "",
                  "check_cmd": f'test "$HOME" != "{real_home}" && test -n "$HOME"'}]
        tf = tmp_path / "tasks.json"
        tf.write_text(json.dumps(tasks))
        monkeypatch.setattr(run_mod, "TASKS_FILE", tf)
        out = _run_stubbed_campaign(
            tmp_path / "o", reps=1, task_ids=["env-probe"],
            claude_cmd=stub_claude, seed=5, conditions=("without",))
        row = json.loads(out.read_text().splitlines()[0])
        assert row["error"] == ""
        assert row["success"] is True  # HOME inside the check is the throwaway one


STUB_VIOLATE_ONCE = """#!/bin/sh
mkdir -p app 2>/dev/null || true
[ -f app/stub_bad.py ] || printf 'def BadCamel(x):\\n    return x\\n' > app/stub_bad.py
cat <<'EOF2'
{"result": "done", "usage": {"input_tokens": 100, "output_tokens": 10, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}, "total_cost_usd": 0.001, "num_turns": 1, "duration_ms": 5, "session_id": "stub"}
EOF2
"""


class TestChainScoringIsolation:
    def test_untracked_step1_file_not_rescored_by_later_steps(self, tmp_path):
        # Step 1 leaves an untracked violating file; the worktree is snapshot-committed
        # between steps, so steps 2-3 must score zero violations for it (P1).
        p = tmp_path / "claude"
        p.write_text(STUB_VIOLATE_ONCE)
        p.chmod(p.stat().st_mode | 0o111)
        out = _run_stubbed_campaign(
            tmp_path / "c", reps=1,
            task_ids=["chain-1-cache", "chain-2-list", "chain-3-audit"],
            claude_cmd=str(p), seed=5, conditions=("without",))
        rows = [json.loads(line) for line in out.read_text().splitlines()]
        by_step = {r["step"]: r for r in rows}
        assert by_step[1]["violations"] >= 1   # the bad file is step 1's work
        assert by_step[2]["violations"] == 0   # not re-counted
        assert by_step[3]["violations"] == 0


class TestContexerSourcesCLI:
    def test_malformed_pair_rejected_before_any_run(self, tmp_path):
        # Greptile #117: a pair without '=' must be an argparse error (exit 2 with a
        # clear message), never a ValueError after the campaign machinery starts.
        import subprocess
        import sys
        proc = subprocess.run(
            [sys.executable, "-m", "benchmarks.run",
             "--tasks", "rat-storage", "--out", str(tmp_path / "out"),
             "--contexer-sources", "contexer_pre_v1"],
            capture_output=True, text=True)
        assert proc.returncode == 2
        assert "is not name=path" in proc.stderr
        assert not (tmp_path / "out").exists()      # nothing was written
