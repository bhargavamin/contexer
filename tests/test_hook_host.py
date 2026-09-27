"""Exercise installed commands across the Cursor/Claude application boundary."""
import io
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from contexer import hook_host
from contexer.adapters import claude, cursor


@pytest.mark.parametrize("payload", [
    {"cursor_version": "3.21.18", "model": "claude-sonnet"},
    {"cursor_version": "3.21.18", "model": "gpt"},
    {"cursor_version": ""},
    {"conversation_id": "c", "generation_id": "g", "workspace_roots": []},
])
def test_cursor_envelopes_skip_before_launch(payload, monkeypatch, capsys):
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode())))
    monkeypatch.setattr(hook_host.subprocess, "run", lambda *a, **k: pytest.fail("hook launched"))
    hook_host.run_claude()
    assert capsys.readouterr().out == "{}\n"


@pytest.mark.parametrize("raw", [
    b'{"session_id":"s","model":"claude-sonnet","prompt":"cursor_version"}',
    b'{"workspace_roots": ["/repo"]}', b'{}', b'[]', b'null', b'broken', b'\xff', b'',
])
def test_unknown_or_claude_envelope_preserves_bytes_and_exit(raw, monkeypatch):
    monkeypatch.setattr(sys, "stdin", SimpleNamespace(buffer=io.BytesIO(raw)))
    monkeypatch.setattr(sys, "argv", ["-c", "trusted command"])
    monkeypatch.setenv("TERM_PROGRAM", "vscode")  # Claude Code in Cursor's terminal
    calls = []
    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=7)
    monkeypatch.setattr(hook_host.subprocess, "run", run)
    with pytest.raises(SystemExit) as exc:
        hook_host.run_claude()
    assert exc.value.code == 7
    assert calls == [("trusted command", {"shell": True, "input": raw})]


@pytest.fixture
def installed(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    claude.install(home)
    return home


def commands(home):
    settings = json.loads((home / ".claude/settings.json").read_text())
    return [(event, hook["command"]) for event, groups in settings["hooks"].items()
            for group in groups for hook in group["hooks"]]


def invoke(command, payload, cwd):
    return subprocess.run(command, shell=True, input=json.dumps(payload), text=True,
                          cwd=cwd, env=os.environ.copy(), capture_output=True, timeout=20)


def test_every_installed_claude_hook_is_inert_in_cursor(installed, tmp_path):
    state = installed / ".contexer"
    state.mkdir()
    flag = state / ".pending_capture"
    flag.write_text("keep")
    before = {p.relative_to(installed): p.read_bytes()
              for p in installed.rglob("*") if p.is_file()}
    for event, command in commands(installed):
        result = invoke(command, {"cursor_version": "3.21.18", "model": "claude-sonnet",
                                 "session_id": "cursor-chat", "hook_event_name": event,
                                 "workspace_roots": [str(tmp_path)],
                                 "prompt": "Always use conventional commits"}, tmp_path)
        assert result.returncode == 0, result.stderr
        assert result.stdout == "{}\n", (event, result.stdout)
        assert result.stderr == ""
    assert {p.relative_to(installed): p.read_bytes()
            for p in installed.rglob("*") if p.is_file()} == before


def test_reinstall_migrates_all_owned_hooks_and_preserves_foreign_siblings(installed):
    path = installed / ".claude/settings.json"
    settings = json.loads(path.read_text())
    foreign = {"type": "command", "command": "echo foreign", "timeout": 12}
    for groups in settings["hooks"].values():
        for group in groups:
            for hook in group["hooks"]:
                # The exact previous installer command is the wrapper's last argv.
                hook["command"] = shlex.split(hook["command"])[-1]
            group["hooks"].append(foreign.copy())
    path.write_text(json.dumps(settings))
    claude.install(installed)
    migrated = path.read_bytes()
    assert all("contexer.hook_host" in command or command == "echo foreign"
               for _, command in commands(installed))
    assert migrated.count(b'"echo foreign"') == sum(len(g) for g in settings["hooks"].values())
    claude.install(installed)
    assert path.read_bytes() == migrated
    claude.uninstall(installed)
    remaining = json.loads(path.read_text())["hooks"]
    assert all(h == foreign for groups in remaining.values() for g in groups for h in g["hooks"])


def test_claude_command_passes_prompt_literally_and_preserves_reminders(installed, tmp_path):
    flag = installed / ".contexer/.pending_capture"
    flag.parent.mkdir()
    flag.touch()
    command = next(c for event, c in commands(installed)
                   if event == "UserPromptSubmit" and ".pending_capture" in c)
    result = invoke(command, {"session_id": "claude-chat", "cwd": str(tmp_path),
                              "prompt": "$(touch injected); `touch injected`; 'quoted'"}, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "last turn settled" in result.stdout
    assert not flag.exists()
    assert not (tmp_path / "injected").exists()


@pytest.mark.parametrize("model", ["claude-sonnet", "claude-opus", "gpt"])
def test_native_cursor_still_bootstraps_captures_and_recalls(installed, tmp_path, model):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    cursor.install(installed)
    hooks = json.loads((installed / ".cursor/hooks.json").read_text())["hooks"]
    payload = {"cursor_version": "3.21.18", "model": model, "conversation_id": "c",
               "generation_id": "g", "session_id": "c", "workspace_roots": [str(repo)]}
    started = invoke(hooks["sessionStart"][0]["command"], payload, repo)
    assert started.returncode == 0, started.stderr
    assert "bootstrap" in json.loads(started.stdout)["additional_context"].lower()
    assert "get_context" in (repo / ".cursor/rules/contexer.mdc").read_text()
    payload["prompt"] = "Always use conventional commits"
    captured = invoke(hooks["beforeSubmitPrompt"][0]["command"], payload, repo)
    assert captured.returncode == 0, captured.stderr
    assert json.loads(captured.stdout) == {"continue": True}
    recalled = subprocess.run([sys.executable, "-P", "-c",
                               "from contexer import store; import sys; "
                               "print(store.get_context(sys.argv[1]))", str(repo)],
                              text=True, capture_output=True, check=True)
    assert "conventional commits" in recalled.stdout.lower()


def test_claude_bootstrap_roundtrip_in_throwaway_repo(installed, tmp_path):
    repo = tmp_path / "project"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (repo / "pyproject.toml").write_text('[project]\nname = "sample"\nrequires-python = ">=3.12"\n'
                                        '[tool.ruff]\nline-length = 100\n')
    payload = {"session_id": "claude-chat", "cwd": str(repo), "source": "startup"}
    started = invoke(next(c for e, c in commands(installed) if e == "SessionStart"), payload, repo)
    assert started.returncode == 0, started.stderr
    assert "bootstrap" in started.stdout.lower()
    command = next(c for _, c in commands(installed) if "get_bootstrap_context_prompt" in c)
    offered = invoke(command, payload, repo)
    assert offered.returncode == 0, offered.stderr
    assert "call bootstrap_context now" in offered.stdout
    assert json.loads(invoke(command, payload, repo).stdout) == {}
    # Real scan, report, persistence and recall; only HOME is redirected.
    script = """
from contexer import bootstrap, store
import sys
repo = sys.argv[1]
scan = bootstrap.run(repo, 'claude-chat')
assert not scan['candidates']
done = bootstrap.run(repo, 'claude-chat', snapshot_id=scan['snapshot_id'], findings=[], finish=True)
assert done['stage'] == 'reported_complete'
assert store.load(repo)['bootstrap_scan']['stage'] == 'reported_complete'
assert '100' in store.get_context(repo)
assert bootstrap.directive(repo) == ''
"""
    subprocess.run([sys.executable, "-P", "-c", script, str(repo)], check=True, capture_output=True)
    assert json.loads(invoke(command, payload, repo).stdout) == {}


def test_plugin_guard_preserves_workspace_and_skips_cursor(installed, tmp_path, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    hooks = json.loads((root / "hooks/hooks.json").read_text())["hooks"]
    command = next(h["command"] for g in hooks["UserPromptSubmit"] for h in g["hooks"]
                   if "get_bootstrap_context_prompt" in h["command"])
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(root))
    monkeypatch.setenv("UV_NO_SYNC", "1")
    repo = tmp_path / "plugin-workspace"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    skipped = invoke(command, {"cursor_version": "3.21.18", "model": "claude-sonnet"}, repo)
    assert skipped.returncode == 0, skipped.stderr
    assert skipped.stdout == "{}\n"
    # Missing cwd in stdin exercises PWD, which outer uv --directory would misroute.
    offered = invoke(command, {"session_id": "claude-plugin"}, repo)
    assert offered.returncode == 0, offered.stderr
    assert str(repo) in offered.stdout
