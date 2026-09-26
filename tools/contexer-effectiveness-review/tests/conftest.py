import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))
OLD_DATE = "2020-01-01T00:00:00"


def run_script(name, args=(), stdin="", env=None, check=False):
    out = subprocess.run([sys.executable, str(SCRIPTS / name), *args], input=stdin,
                         capture_output=True, text=True, env=env, timeout=60)
    if check:
        assert out.returncode == 0, out.stdout + out.stderr
    return out


def git(repo, *args, env=None):
    full = dict(os.environ, **(env or {}))
    out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, env=full)
    assert out.returncode == 0, out.stderr
    return out.stdout.strip()


def make_repo(path, email="me@example.com"):
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", email)
    git(path, "config", "user.name", "Me")
    git(path, "config", "commit.gpgsign", "false")
    git(path, "config", "core.hooksPath", str(path / ".disabled-hooks"))
    (path / "a.txt").write_text("a\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "initial", env={"GIT_COMMITTER_DATE": OLD_DATE,
                                                "GIT_AUTHOR_DATE": OLD_DATE})
    return path


def commit(repo, name, msg, env=None):
    (repo / name).write_text(name + "\n")
    git(repo, "add", name)
    git(repo, "commit", "-qm", msg, env=env)
    return git(repo, "rev-parse", "HEAD")


class Transcript:
    """Writes host-shaped transcript lines."""

    def __init__(self, path, host):
        self.path, self.host, self.n = path, host, 0
        path.write_text("")

    def _write(self, rec):
        with open(self.path, "a") as f:
            f.write(json.dumps(rec) + "\n")

    def call(self, name, args, namespace=None, result=None):
        self.n += 1
        cid = f"c{self.n}"
        if self.host == "cursor":
            self._write({"role": "assistant", "message": {"content": [
                {"type": "tool_use", "name": name, "input": args}]}})
        elif self.host == "claude":
            self._write({"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": cid, "name": name, "input": args}]}})
            if result is not None:
                self._write({"type": "user", "message": {"content": [
                    {"type": "tool_result", "tool_use_id": cid, "content": result}]}})
        else:
            payload = {"type": "function_call", "name": name, "arguments": json.dumps(args),
                       "call_id": cid}
            if namespace:
                payload["namespace"] = namespace
            self._write({"type": "response_item", "payload": payload})
            if result is not None:
                self._write({"type": "response_item", "payload": {
                    "type": "function_call_output", "call_id": cid, "output": result}})

    def shell(self, command):
        name = {"cursor": "Shell", "claude": "Bash", "codex": "exec_command"}[self.host]
        self.call(name, {"command": command})

    def user(self, text):
        if self.host == "claude":
            self._write({"type": "user", "message": {"role": "user", "content": text}})
        elif self.host == "cursor":
            self._write({"role": "user", "message": {"content": [{"type": "text", "text": text}]}})
        else:
            self._write({"type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}})

    def raw(self, rec):
        self._write(rec)


@pytest.fixture
def home(tmp_path, monkeypatch):
    data = tmp_path / "data"
    env = {k: v for k, v in os.environ.items()
           if not re.search(r'KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL|AUTH', k, re.I)}
    env['CONTEXER_USAGE_HOME'] = str(data)
    return data, env


@pytest.fixture
def repo(tmp_path):
    return make_repo(tmp_path / "repo")


def payload(host, session, transcript_path, repo, **extra):
    if host == "cursor":
        data = {"conversation_id": session, "status": "completed", "loop_count": 0,
                "workspace_roots": [str(repo)], "cursor_version": "3.0",
                "hook_event_name": "stop"}
    else:
        data = {"session_id": session, "cwd": str(repo), "stop_hook_active": False,
                "hook_event_name": "Stop"}
    if transcript_path:
        data["transcript_path"] = str(transcript_path)
    data.update(extra)
    return json.dumps(data)


def stop(host, env, data):
    out = run_script("stop_hook.py", ["--host", host], stdin=data, env=env)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def fired(result):
    return bool(result.get("followup_message") or result.get("reason"))


def token_of(result):
    msg = result.get("followup_message") or result.get("reason")
    return msg.split("token=")[1].split()[0]


def pending_of(home_dir, result):
    return json.loads((home_dir / "pending" / f"{token_of(result)}.json").read_text())


def settle():
    time.sleep(1.1)
