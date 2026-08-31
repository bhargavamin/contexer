"""D6 host lifecycle checkpoints for automatic proposal recovery."""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from contexer import share_policy, store
from contexer.adapters import base, claude, codex, cursor, gemini


def _commands(groups):
    return [
        hook.get("command", "")
        for group in groups if isinstance(group, dict)
        for hook in group.get("hooks", []) if isinstance(hook, dict)
    ]


def test_shared_fallback_skips_unconfigured_repo_without_scanning(tmp_repo, monkeypatch):
    monkeypatch.setattr(base._store, "hook_cwd_repo", lambda _repo: tmp_repo)
    monkeypatch.setattr(
        share_policy, "scan_and_enqueue",
        lambda _repo: pytest.fail("policy-disabled prompt must not import/run the scanner"),
    )
    monkeypatch.setattr(
        base, "_start_detached_proposal_drainer",
        lambda: pytest.fail("policy-disabled prompt must not start the drainer"),
    )

    assert base._scan_automatic_proposals(tmp_repo) is None


def test_shared_fallback_runs_bounded_scanner_for_configured_repo(tmp_repo, monkeypatch):
    monkeypatch.setattr(base._store, "hook_cwd_repo", lambda _repo: tmp_repo)
    policy = share_policy.policy_path(tmp_repo)
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("configured", encoding="utf-8")
    seen = []
    starts = []
    expected = share_policy.ScanOutcome("success", "none", scanned=3)
    monkeypatch.setattr(
        share_policy, "scan_and_enqueue",
        lambda repo, **kwargs: seen.append((repo, kwargs)) or expected)
    monkeypatch.setattr(
        base, "_start_detached_proposal_drainer", lambda: starts.append(True) or True)

    assert base._scan_automatic_proposals(tmp_repo) == expected
    assert seen == [(tmp_repo, {"start_worker": False})]
    assert starts == [True]


def test_shared_fallback_is_fail_soft(tmp_repo, monkeypatch):
    monkeypatch.setattr(base._store, "hook_cwd_repo", lambda _repo: tmp_repo)
    policy = share_policy.policy_path(tmp_repo)
    policy.parent.mkdir(parents=True, exist_ok=True)
    policy.write_text("configured", encoding="utf-8")
    monkeypatch.setattr(
        share_policy, "scan_and_enqueue",
        lambda _repo, **_kwargs: (
            _ for _ in ()).throw(RuntimeError("SENTINEL_INTERNAL_FAILURE")),
    )

    assert base._scan_automatic_proposals(tmp_repo) is None


def test_detached_drainer_uses_fixed_closed_process(monkeypatch):
    calls = []
    sink = type("Sink", (), {"closed": False, "close": lambda self: setattr(
        self, "closed", True)})()
    monkeypatch.setattr(base, "_open_detached_drainer_diagnostics", lambda: sink)
    monkeypatch.setattr(
        base.subprocess, "Popen", lambda *args, **kwargs: calls.append((args, kwargs)))

    assert base._start_detached_proposal_drainer() is True

    args, kwargs = calls[0]
    assert args[0] == [
        sys.executable,
        "-P",
        "-c",
        "try:\n"
        " from contexer import share_policy as _s\n"
        " _s.run_detached_drainer()\n"
        "except BaseException:\n"
        " pass",
    ]
    assert kwargs == {
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": sink,
        "close_fds": True,
        "start_new_session": True,
    }
    assert sink.closed is True


def test_detached_drainer_diagnostics_sink_is_private_bounded_and_not_a_symlink(
        tmp_repo):
    path = share_policy.proposal_diagnostics_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * (base._DRAINER_DIAGNOSTICS_MAX_BYTES + 1))
    path.chmod(0o644)

    sink = base._open_detached_drainer_diagnostics()
    sink.close()

    assert path.stat().st_size == 0
    assert path.stat().st_mode & 0o777 == 0o600

    if hasattr(os, "O_NOFOLLOW"):
        path.unlink()
        victim = path.parent / "must-not-be-opened"
        victim.write_text("safe", encoding="utf-8")
        path.symlink_to(victim)
        with pytest.raises(OSError):
            base._open_detached_drainer_diagnostics()
        assert victim.read_text(encoding="utf-8") == "safe"


def test_detached_drainer_survives_short_lived_hook_parent(tmp_path):
    """A child waiting on a post-parent gate proves it is not a daemon thread."""
    fake_root = tmp_path / "fake-package"
    package = fake_root / "contexer"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "share_policy.py").write_text(
        "import os, time\n"
        "from pathlib import Path\n"
        "def run_detached_drainer():\n"
        "    gate = Path(os.environ['CONTEXER_TEST_DRAIN_GATE'])\n"
        "    marker = Path(os.environ['CONTEXER_TEST_DRAIN_MARKER'])\n"
        "    deadline = time.monotonic() + 5\n"
        "    while not gate.exists() and time.monotonic() < deadline:\n"
        "        time.sleep(0.01)\n"
        "    if gate.exists():\n"
        "        marker.write_text('completed', encoding='utf-8')\n",
        encoding="utf-8",
    )
    gate = tmp_path / "release-child"
    marker = tmp_path / "child-completed"
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).parent.parent)
    env["HOME"] = str(tmp_path / "home")
    env["CONTEXER_TEST_DRAIN_GATE"] = str(gate)
    env["CONTEXER_TEST_DRAIN_MARKER"] = str(marker)
    parent = (
        "import os,sys; from contexer.adapters import base; "
        "os.environ['PYTHONPATH']=sys.argv[1]; "
        "raise SystemExit(0 if base._start_detached_proposal_drainer() else 1)"
    )

    completed = subprocess.run(
        [sys.executable, "-P", "-c", parent, str(fake_root)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr
    assert not marker.exists()
    gate.write_text("release", encoding="utf-8")
    deadline = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert marker.read_text(encoding="utf-8") == "completed"


def test_detached_child_persists_secret_safe_terminal_drain_telemetry(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    secret = "SENTINEL_SECRET_MUST_NOT_REACH_DETACHED_DIAGNOSTICS"
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(Path(__file__).parent.parent)
    env["CONTEXER_TEST_SECRET"] = secret
    parent = (
        "from contexer.adapters import base; "
        "raise SystemExit(0 if base._start_detached_proposal_drainer() else 1)"
    )

    completed = subprocess.run(
        [sys.executable, "-P", "-c", parent],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr
    path = home / ".contexer" / ".team-proposal-diagnostics.jsonl"
    deadline = time.monotonic() + 5
    rows = []
    while time.monotonic() < deadline:
        try:
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        except (FileNotFoundError, json.JSONDecodeError):
            rows = []
        if len(rows) >= 2:
            break
        time.sleep(0.01)

    assert len(rows) == 2
    span, log = rows
    assert span["name"] == "decision_proposal.drain"
    assert span["attributes"] == {
        "contexer.error_class": "none",
        "contexer.queue_depth": 0,
        "contexer.reason_code": "none",
        "contexer.result": "no_op",
    }
    assert log["fields"] == {
        "action": "decisionProposalDrain",
        "errorClass": "none",
        "queueDepth": 0,
        "reasonCode": "none",
        "result": "no_op",
    }
    encoded = json.dumps(rows)
    assert secret not in encoded
    assert str(tmp_path) not in encoded
    assert path.stat().st_mode & 0o777 == 0o600


def test_detached_child_converts_unexpected_failure_to_closed_telemetry(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    injector = tmp_path / "injector"
    injector.mkdir()
    secret = "SENTINEL_RAW_EXCEPTION_SECRET"
    path_sentinel = "/SENTINEL/private/repository/path"
    (injector / "sitecustomize.py").write_text(
        "import os\n"
        "from contexer import share_policy\n"
        "def explode(*_args, **_kwargs):\n"
        "    raise RuntimeError(os.environ['CONTEXER_TEST_RAW_FAILURE'])\n"
        "share_policy.drain_once = explode\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(Path(__file__).parent.parent)
    env["CONTEXER_TEST_RAW_FAILURE"] = f"{secret} {path_sentinel}"
    parent = (
        "import os,sys; from contexer.adapters import base; "
        "os.environ['PYTHONPATH']=sys.argv[1]; "
        "raise SystemExit(0 if base._start_detached_proposal_drainer() else 1)"
    )

    completed = subprocess.run(
        [sys.executable, "-P", "-c", parent, str(injector)],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=5,
    )

    assert completed.returncode == 0, completed.stderr
    diagnostics = home / ".contexer" / ".team-proposal-diagnostics.jsonl"
    deadline = time.monotonic() + 5
    rows = []
    while time.monotonic() < deadline:
        try:
            rows = [
                json.loads(line)
                for line in diagnostics.read_text(encoding="utf-8").splitlines()
            ]
        except (FileNotFoundError, json.JSONDecodeError):
            rows = []
        if len(rows) >= 2:
            break
        time.sleep(0.01)

    assert len(rows) == 2
    assert rows[0]["name"] == "decision_proposal.drain"
    assert rows[0]["attributes"]["contexer.result"] == "failure"
    assert rows[0]["attributes"]["contexer.reason_code"] == "validation_error"
    assert rows[1]["fields"]["action"] == "decisionProposalDrain"
    encoded = json.dumps(rows)
    assert secret not in encoded
    assert path_sentinel not in encoded
    assert "RuntimeError" not in encoded


def test_claude_shared_lifecycle_and_prompt_paths_scan_even_without_directive(
        tmp_repo, monkeypatch):
    scans = []
    monkeypatch.setattr(claude, "_scan_automatic_proposals", lambda repo: scans.append(repo))
    monkeypatch.setattr(claude, "_import_memory_facts", lambda _repo: 2)
    monkeypatch.setattr(claude, "_reconcile_evidence", lambda _repo: None)

    assert claude.sync_memory(tmp_repo) == 2

    monkeypatch.setattr(store, "_hook_repo_verbose", lambda *_args: (tmp_repo, "hook-arg"))
    monkeypatch.setattr(store, "anchor_repo", lambda _repo: None)
    monkeypatch.setattr(
        claude.evidence, "capture_directive",
        lambda *_args, **_kwargs: (None, "", ""),
    )
    assert claude.capture_constraint(tmp_repo, "{}") == "{}"
    assert scans == [tmp_repo, tmp_repo]


def test_claude_plugin_routes_every_supported_fallback_through_scanning_entrypoints():
    hooks = json.loads(
        (Path(__file__).parent.parent / "hooks" / "hooks.json").read_text())["hooks"]

    for event in ("SessionStart", "PreCompact", "SessionEnd"):
        assert "sync_memory" in "\n".join(_commands(hooks[event]))
    assert "claude.capture_constraint" in "\n".join(
        _commands(hooks["UserPromptSubmit"]))
    assert "Stop" not in hooks


def test_codex_installs_local_scans_on_supported_lifecycle_events(tmp_path):
    codex.install(tmp_path)
    hooks = json.loads((tmp_path / ".codex" / "hooks.json").read_text())["hooks"]
    session = "\n".join(_commands(hooks["SessionStart"]))
    compact = "\n".join(_commands(hooks["PreCompact"]))
    prompts = "\n".join(_commands(hooks["UserPromptSubmit"]))

    assert "_scan_automatic_proposals" in session
    assert "_scan_automatic_proposals" in compact
    assert "claude.capture_constraint" in prompts
    assert "Stop" not in hooks
    for command in (session, compact):
        assert "RemoteStore" not in command and "drain_once" not in command


def test_codex_reinstall_replaces_old_prompt_only_precompact_hook(tmp_path):
    path = tmp_path / ".codex" / "hooks.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"hooks": {"PreCompact": [{"hooks": [{
        "type": "command",
        "command": "echo 'Contexer: context compaction starting'",
    }]}]}}), encoding="utf-8")

    codex.install(tmp_path)

    hooks = json.loads(path.read_text())["hooks"]["PreCompact"]
    commands = _commands(hooks)
    assert len([command for command in commands if "compaction starting" in command]) == 1
    assert "_scan_automatic_proposals" in commands[0]


def test_gemini_scans_session_prompt_precompress_and_end(tmp_repo, monkeypatch):
    scans = []
    monkeypatch.setattr(base, "_scan_automatic_proposals", lambda repo: scans.append(repo))
    monkeypatch.setattr(store, "hook_repo_from_stdin", lambda *_args: tmp_repo)
    monkeypatch.setattr(gemini, "_anchor", lambda _repo: None)
    monkeypatch.setattr(gemini, "_session_marker", lambda _raw: None)
    monkeypatch.setattr(store, "session_start_payload", lambda *_args: {"context": ""})
    monkeypatch.setattr(store, "pending_review_nudge", lambda _repo: "")
    monkeypatch.setattr(store, "bootstrap_prompt_payload", lambda *_args: {"context": ""})
    monkeypatch.setattr(
        gemini.evidence, "capture_directive",
        lambda *_args, **_kwargs: (None, "", ""),
    )
    monkeypatch.setattr(store, "get_context_for_prompt", lambda *_args: "")
    monkeypatch.setattr(gemini, "_reconcile_evidence", lambda _repo: None)

    assert json.loads(gemini.session_start(tmp_repo, "{}"))["suppressOutput"] is True
    assert json.loads(gemini.before_agent(tmp_repo, "{}"))["suppressOutput"] is True
    assert json.loads(gemini.pre_compress(tmp_repo, "{}"))["suppressOutput"] is True
    assert json.loads(gemini.session_end(tmp_repo, "{}"))["suppressOutput"] is True
    assert scans == [tmp_repo, tmp_repo, tmp_repo, tmp_repo]


def test_gemini_lifecycle_prefers_payload_repo_over_fallback_cwd(tmp_path, monkeypatch):
    payload_repo = str(tmp_path / "payload-repo")
    fallback_repo = str(tmp_path / "wrong-cwd")
    raw = json.dumps({"cwd": payload_repo})
    reconciled = []
    scanned = []
    monkeypatch.setattr(
        store, "hook_repo_from_stdin",
        lambda event, fallback: payload_repo if event == raw else fallback,
    )
    monkeypatch.setattr(gemini, "_session_marker", lambda _raw: None)
    monkeypatch.setattr(gemini, "_reconcile_evidence", lambda repo: reconciled.append(repo))
    monkeypatch.setattr(base, "_scan_automatic_proposals", lambda repo: scanned.append(repo))

    assert json.loads(gemini.pre_compress(fallback_repo, raw))["suppressOutput"] is True
    assert json.loads(gemini.session_end(fallback_repo, raw))["suppressOutput"] is True
    assert reconciled == [payload_repo, payload_repo]
    assert scanned == [payload_repo, payload_repo]


def test_cursor_uses_only_supported_session_and_prompt_fallbacks(tmp_repo, monkeypatch):
    scans = []
    monkeypatch.setattr(base, "_scan_automatic_proposals", lambda repo: scans.append(repo))
    monkeypatch.setattr(cursor, "_repo_from", lambda *_args: tmp_repo)
    monkeypatch.setattr(
        cursor, "_repo_from_verbose", lambda *_args: (tmp_repo, "workspace-root"))
    monkeypatch.setattr(store, "is_sane_repo", lambda _repo: True)
    monkeypatch.setattr(store, "anchor_repo", lambda _repo: None)
    monkeypatch.setattr(cursor, "_ensure_rule_file", lambda _repo: None)
    monkeypatch.setattr(store, "session_start_payload", lambda *_args: {"context": ""})
    monkeypatch.setattr(
        cursor.evidence, "capture_directive",
        lambda *_args, **_kwargs: (None, "", ""),
    )

    assert "additional_context" in json.loads(cursor.session_start(tmp_repo, "{}"))
    assert json.loads(cursor.capture_constraint(tmp_repo, "{}")) == {"continue": True}
    assert scans == [tmp_repo, tmp_repo]
