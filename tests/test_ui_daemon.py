"""Tests for the console daemon lifecycle: statefile, token race, liveness, pairing codes."""
import ast
import json
import os
import signal
import socket
import subprocess
import sys
import textwrap
import threading
import time
import types
from pathlib import Path

import pytest

from contexer.ui import daemon

# The module-level imports daemon.py is allowed to make. Reaching contexer.store from here costs
# a measured 134ms on the SessionStart hook path, so this is a budget, not a preference.
ALLOWED_IMPORTS = {
    "os", "sys", "json", "socket", "subprocess", "pathlib", "secrets", "hmac", "hashlib",
    "base64", "time", "signal", "errno", "dataclasses", "typing", "contextlib",
}


@pytest.fixture(autouse=True)
def ui_paths(tmp_path, monkeypatch):
    """Redirect the statefile and log into a temp dir — never the real ~/.contexer."""
    monkeypatch.setattr(daemon, "STATE_PATH", tmp_path / ".contexer" / "ui.json")
    monkeypatch.setattr(daemon, "LOG_PATH", tmp_path / ".contexer" / "ui.log")
    return tmp_path / ".contexer"


@pytest.fixture
def spawns(monkeypatch):
    """Record spawns instead of starting a real daemon. Returns the list of Popen calls."""
    calls = []

    class FakePopen:
        def __init__(self, argv, **kwargs):
            calls.append({"argv": argv, **kwargs})
            self.pid = 4242

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    return calls


@pytest.fixture
def kills(monkeypatch):
    """Record signals instead of sending them — a recorded pid may exist on this machine."""
    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    return sent


def a_state(**overrides) -> daemon.UiState:
    fields = {"pid": os.getpid(), "port": 31415, "token": "tok-abc",
              "started_at": "2026-07-31T00:00:00Z", "version": "1.2.3"}
    return daemon.UiState(**{**fields, **overrides})


def raises(exc):
    def fail(*args, **kwargs):
        raise exc
    return fail


# --- statefile -------------------------------------------------------------------------

def test_state_round_trip_and_owner_only_mode():
    state = a_state()
    daemon.write_state(state)
    assert daemon.read_state() == state
    assert (daemon.STATE_PATH.stat().st_mode & 0o777) == 0o600  # holds the console token


def test_write_state_leaves_no_temp_file(ui_paths):
    daemon.write_state(a_state())
    daemon.write_state(a_state(pid=999))
    assert [p.name for p in ui_paths.iterdir()] == ["ui.json"]


def test_read_state_absent_is_none():
    assert daemon.read_state() is None


@pytest.mark.parametrize("body", [
    "",
    "not json",
    "[]",
    "null",
    '{"pid": 1}',
    '{"pid":1,"port":31415,"token":"","started_at":"x","version":"1"}',
    '{"pid":1,"port":0,"token":"t","started_at":"x","version":"1"}',
])
def test_read_state_corrupt_or_incomplete_is_none(body, ui_paths):
    ui_paths.mkdir(parents=True)
    daemon.STATE_PATH.write_text(body)
    assert daemon.read_state() is None


def test_clear_state_is_idempotent():
    daemon.clear_state()
    daemon.write_state(a_state())
    daemon.clear_state()
    assert not daemon.STATE_PATH.exists()


def test_clear_state_scoped_to_a_pid_drops_its_own_claim():
    daemon.write_state(a_state(pid=os.getpid()))
    daemon.clear_state(pid=os.getpid())
    assert not daemon.STATE_PATH.exists()


def test_clear_state_scoped_to_a_pid_leaves_a_replacements_claim_alone():
    """The upgrade path: the replacement only waits for the port, so a SIGTERMed daemon can reach
    its own cleanup after the new statefile has been written. It used to delete it."""
    daemon.write_state(a_state(pid=os.getpid() + 1))
    daemon.clear_state(pid=os.getpid())
    assert daemon.read_state() is not None


def test_clear_state_scoped_to_a_pid_still_drops_an_unparseable_file(ui_paths):
    ui_paths.mkdir(parents=True)
    daemon.STATE_PATH.write_text("{not json")
    daemon.clear_state(pid=os.getpid())
    assert not daemon.STATE_PATH.exists()


# --- token minting race ----------------------------------------------------------------

def test_claim_state_mints_once():
    first, minted = daemon._claim_state(31415, "1.2.3", None)
    second, minted_again = daemon._claim_state(31415, "1.2.3", None)
    assert minted and not minted_again
    assert second.token == first.token  # the O_EXCL winner's token is the only one


def test_claim_state_reuses_a_supplied_token():
    state, minted = daemon._claim_state(31415, "1.2.3", "carried-over")
    assert minted and state.token == "carried-over"


def test_claim_state_gives_up_rather_than_minting_a_rival_token(ui_paths):
    ui_paths.mkdir(parents=True)
    daemon.STATE_PATH.write_text("")  # the race winner created it but has not filled it in
    assert daemon._claim_state(31415, "1.2.3", None) is None


def test_claim_state_is_owner_only():
    daemon._claim_state(31415, "1.2.3", None)
    assert (daemon.STATE_PATH.stat().st_mode & 0o777) == 0o600


# --- liveness --------------------------------------------------------------------------

def test_is_alive_false_when_the_process_is_gone(monkeypatch):
    monkeypatch.setattr(os, "kill", raises(ProcessLookupError()))
    monkeypatch.setattr(daemon, "probe", lambda port, token: pytest.fail("probed a dead pid"))
    assert not daemon.is_alive(a_state())


def test_is_alive_defers_to_the_probe(monkeypatch):
    monkeypatch.setattr(daemon, "probe", lambda port, token: True)
    assert daemon.is_alive(a_state())
    monkeypatch.setattr(daemon, "probe", lambda port, token: False)
    assert not daemon.is_alive(a_state())


def test_is_alive_lets_the_probe_settle_a_recycled_pid(monkeypatch):
    """os.kill EPERM means some other user's process holds that pid — only the token decides."""
    monkeypatch.setattr(os, "kill", raises(PermissionError()))
    monkeypatch.setattr(daemon, "probe", lambda port, token: True)
    assert daemon.is_alive(a_state())


def test_is_alive_probes_when_no_pid_was_recorded(monkeypatch):
    probed = []
    monkeypatch.setattr(daemon, "probe", lambda port, token: probed.append(port) or True)
    assert daemon.is_alive(a_state(pid=0))
    assert probed == [31415]


def test_probe_timeout_stays_within_the_hook_budget():
    assert daemon.PROBE_TIMEOUT <= 0.1


def test_probe_rejects_a_closed_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    assert not daemon.probe(port, "tok-abc")


def test_probe_rejects_a_foreign_socket_without_hanging():
    """A listener that never answers HTTP is not our daemon — and must not stall the hook."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    started = time.perf_counter()
    try:
        assert not daemon.probe(listener.getsockname()[1], "tok-abc")
    finally:
        listener.close()
    assert time.perf_counter() - started < 1.0


def test_probe_accepts_our_own_daemon(monkeypatch):
    monkeypatch.setattr(daemon, "PROBE_TIMEOUT", 5.0)  # thread scheduling, not the real budget
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    seen = {}

    def serve():
        conn, _ = listener.accept()
        seen["request"] = conn.recv(512)
        conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Length: 0\r\n\r\n")
        conn.close()

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        assert daemon.probe(listener.getsockname()[1], "tok-abc")
    finally:
        thread.join(timeout=5)
        listener.close()
    assert b"GET /healthz" in seen["request"]
    assert b"X-Contexer-Token: tok-abc" in seen["request"]  # /healthz is not unauthenticated


def test_port_occupied_sees_a_listener_and_nothing_else():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen(1)
    try:
        assert daemon.port_occupied(port)
    finally:
        listener.close()
    assert not daemon.port_occupied(port)


def test_await_port_free_returns_at_once_on_a_free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    started = time.perf_counter()
    assert daemon.await_port_free(port)
    assert time.perf_counter() - started < 0.5


def test_await_port_free_waits_for_a_closing_listener():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    threading.Timer(0.2, listener.close).start()
    started = time.perf_counter()
    assert daemon.await_port_free(listener.getsockname()[1])
    assert time.perf_counter() - started >= 0.05


def test_await_port_free_gives_up_rather_than_blocking_the_hook():
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    # Deep backlog: nobody accepts here, and a full accept queue drops the SYN, which looks
    # exactly like a free port. A real daemon accepts, which is the case that matters.
    listener.listen(64)
    try:
        started = time.perf_counter()
        assert not daemon.await_port_free(listener.getsockname()[1], timeout_s=0.2)
        assert time.perf_counter() - started < 2.0
    finally:
        listener.close()


def test_probe_rejects_a_non_200_answer(monkeypatch):
    monkeypatch.setattr(daemon, "PROBE_TIMEOUT", 5.0)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)

    def serve():
        conn, _ = listener.accept()
        conn.recv(512)
        conn.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
        conn.close()

    thread = threading.Thread(target=serve)
    thread.start()
    try:
        assert not daemon.probe(listener.getsockname()[1], "wrong-token")
    finally:
        thread.join(timeout=5)
        listener.close()


# --- ensure_running --------------------------------------------------------------------

def test_ensure_running_warm_does_not_spawn(monkeypatch, spawns):
    monkeypatch.setattr(daemon, "current_version", lambda: "1.2.3")
    monkeypatch.setattr(daemon, "probe", lambda port, token: True)
    daemon.write_state(a_state())
    assert daemon.ensure_running() == (31415, "tok-abc")
    assert spawns == []


def test_ensure_running_cold_spawns_detached(monkeypatch, spawns):
    monkeypatch.setattr(daemon, "current_version", lambda: "1.2.3")
    result = daemon.ensure_running(9999)
    assert result is not None
    port, token = result
    assert port == 9999 and len(token) > 20
    call = spawns[0]
    # `-P`: `-m` prepends the process cwd to sys.path, so a daemon started from a
    # checked-out contexer repo would import that source tree, not the installed package.
    assert call["argv"] == [sys.executable, "-P", "-m", "contexer.ui.server", "--port", "9999"]
    assert call["start_new_session"] is True
    assert call["stdin"] is subprocess.DEVNULL
    assert call["stdout"] is call["stderr"]  # both tail into ui.log
    state = daemon.read_state()
    assert (state.pid, state.port, state.version) == (4242, 9999, "1.2.3")
    assert state.token == token


def test_ensure_running_spawns_the_patched_target(monkeypatch, spawns):
    monkeypatch.setattr(daemon, "SPAWN_TARGET", "tests.fake_server")
    daemon.ensure_running(9999)
    assert spawns[0]["argv"][3] == "tests.fake_server"


def test_ensure_running_never_waits_on_the_child(monkeypatch):
    """Waiting on the daemon would pin the session to the daemon's whole lifetime."""
    class Waitless:
        def __init__(self, argv, **kwargs):
            self.pid = 4242

        def __getattr__(self, name):
            pytest.fail(f"ensure_running touched Popen.{name}")

    monkeypatch.setattr(subprocess, "Popen", Waitless)
    assert daemon.ensure_running(9999) is not None


def test_ensure_running_stale_statefile_self_heals(monkeypatch, spawns):
    monkeypatch.setattr(daemon, "current_version", lambda: "1.2.3")
    monkeypatch.setattr(daemon, "probe", lambda port, token: False)  # nothing on the port
    daemon.write_state(a_state())
    assert daemon.ensure_running() == (31415, "tok-abc")  # the token survives the restart
    assert len(spawns) == 1
    assert daemon.read_state().pid == 4242


def test_ensure_running_version_skew_terminates_the_old_daemon(monkeypatch, spawns, kills):
    monkeypatch.setattr(daemon, "current_version", lambda: "9.9.9")
    daemon.write_state(a_state(pid=777, version="1.2.3"))
    assert daemon.ensure_running() == (31415, "tok-abc")
    assert (777, signal.SIGTERM) in kills
    assert len(spawns) == 1
    assert daemon.read_state().version == "9.9.9"


def test_ensure_running_waits_for_the_old_port_before_spawning(monkeypatch, spawns, kills):
    """SIGTERM is asynchronous. Spawning straight after it launched the replacement into
    EADDRINUSE, and by the time it probed, the daemon it collided with had closed its socket —
    so the child exited 1 and the URL the caller printed was dead."""
    order = []
    monkeypatch.setattr(daemon, "current_version", lambda: "9.9.9")
    monkeypatch.setattr(daemon, "await_port_free",
                        lambda port, *args: order.append(("wait", port)) or True)
    monkeypatch.setattr(daemon, "_spawn", lambda port: order.append(("spawn", port)) or 4242)
    daemon.write_state(a_state(pid=777, version="1.2.3"))
    assert daemon.ensure_running() == (31415, "tok-abc")
    assert order == [("wait", 31415), ("spawn", 31415)]


def test_ensure_running_warm_never_waits_on_a_port(monkeypatch, spawns):
    """The wait belongs to the skew path only: this runs on every SessionStart hook."""
    monkeypatch.setattr(daemon, "current_version", lambda: "1.2.3")
    monkeypatch.setattr(daemon, "probe", lambda port, token: True)
    monkeypatch.setattr(daemon, "await_port_free",
                        lambda *args, **kwargs: pytest.fail("waited on the warm path"))
    daemon.write_state(a_state())
    assert daemon.ensure_running() == (31415, "tok-abc")


def test_ensure_running_does_not_wait_when_the_old_pid_is_already_gone(monkeypatch, spawns):
    monkeypatch.setattr(daemon, "current_version", lambda: "9.9.9")
    monkeypatch.setattr(os, "kill", raises(ProcessLookupError()))
    monkeypatch.setattr(daemon, "await_port_free",
                        lambda *args, **kwargs: pytest.fail("waited on a dead pid"))
    daemon.write_state(a_state(pid=777, version="1.2.3"))
    assert daemon.ensure_running() is not None


def test_ensure_running_version_skew_does_not_probe(monkeypatch, spawns, kills):
    """An upgraded wheel means respawn, whatever the old daemon still reports."""
    monkeypatch.setattr(daemon, "current_version", lambda: "9.9.9")
    monkeypatch.setattr(daemon, "probe", lambda p, t: pytest.fail("probed on version skew"))
    daemon.write_state(a_state(version="1.2.3"))
    assert daemon.ensure_running() is not None


def test_ensure_running_returns_none_when_the_spawn_fails(monkeypatch):
    monkeypatch.setattr(subprocess, "Popen", raises(OSError("no fork for you")))
    assert daemon.ensure_running(9999) is None
    log = daemon.LOG_PATH.read_text()
    assert "ensure_running failed" in log and "OSError" in log


def test_ensure_running_returns_none_while_another_process_is_mid_mint(ui_paths):
    """A zero-byte statefile is what the O_EXCL winner leaves for the few microseconds before it
    fills the file in, so within the grace window it is treated as somebody else's claim."""
    ui_paths.mkdir(parents=True)
    daemon.STATE_PATH.write_text("")
    assert daemon.ensure_running(9999) is None


def test_ensure_running_heals_a_corrupt_statefile(monkeypatch, spawns, ui_paths):
    """`{not json` used to disable the console for good: read_state() returned None, so the
    clear_state() inside `if state is not None` was skipped, _claim_state spun 20x on the same
    garbage, and ensure_running answered None forever with nothing written to ui.log."""
    monkeypatch.setattr(daemon, "current_version", lambda: "1.2.3")
    ui_paths.mkdir(parents=True)
    daemon.STATE_PATH.write_text("{not json")
    result = daemon.ensure_running(9999)
    assert result is not None and result[0] == 9999
    assert len(spawns) == 1
    state = daemon.read_state()
    assert state is not None and state.pid == 4242 and state.token == result[1]


@pytest.mark.parametrize("body", ["", "{\"pid\": 1}", "\x00\x00"])
def test_ensure_running_heals_a_stale_unparseable_statefile(monkeypatch, spawns, ui_paths, body):
    """Once the mint grace has passed, anything that does not parse is wreckage — including the
    zero-byte file a process that died between creating and filling it left behind."""
    monkeypatch.setattr(daemon, "current_version", lambda: "1.2.3")
    ui_paths.mkdir(parents=True)
    daemon.STATE_PATH.write_text(body)
    stale = time.time() - daemon.MINT_GRACE_SECONDS - 5
    os.utime(daemon.STATE_PATH, (stale, stale))
    assert daemon.ensure_running(9999) is not None
    assert len(spawns) == 1
    assert daemon.read_state() is not None


def test_ensure_running_leaves_a_live_daemons_statefile_alone(monkeypatch, spawns):
    """The other half of the heal: a file that parses and answers a probe is never re-minted."""
    monkeypatch.setattr(daemon, "current_version", lambda: "1.2.3")
    monkeypatch.setattr(daemon, "probe", lambda port, token: True)
    daemon.write_state(a_state())
    before = daemon.STATE_PATH.read_text()
    assert daemon.ensure_running() == (31415, "tok-abc")
    assert daemon.STATE_PATH.read_text() == before
    assert spawns == []


# --- ensure_running with real daemons --------------------------------------------------

def an_unused_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def until(predicate, message: str, timeout_s: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.05)
    pytest.fail(message)


def test_a_version_upgrade_leaves_a_daemon_that_answers_the_printed_url(tmp_path, monkeypatch):
    """The whole skew sequence with real processes.

    ensure_running SIGTERMed the old daemon and spawned immediately: the child died on
    EADDRINUSE, the corpse's own cleanup deleted the statefile the parent had just written, and
    three seconds later nothing was listening on the URL the hook had already printed."""
    monkeypatch.setenv("HOME", str(tmp_path))  # the children resolve ~/.contexer from this

    # Pin the child's environment explicitly rather than trusting `subprocess.Popen`'s
    # default (a fresh snapshot of `os.environ` at call time) to still carry the HOME
    # override above: this is the one test in the whole suite that spawns a REAL,
    # independent process running contexer.ui.server, which does its own fresh
    # Path.home() resolution — the exact "resolve paths from Path.home()" case the
    # leak-guard in conftest.py warns about. An explicit env= closes any window where
    # os.environ could be read by the child before this monkeypatch is visible to it
    # (e.g. under a slow/contended CI runner), so a real console can never bind against
    # the developer's actual ~/.contexer even transiently.
    pinned_env = dict(os.environ)
    real_popen = subprocess.Popen

    def _pinned_popen(*args, **kwargs):
        kwargs.setdefault("env", pinned_env)
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _pinned_popen)

    port = an_unused_port()
    pids = []
    try:
        first = daemon.ensure_running(port)
        assert first is not None
        until(lambda: daemon.probe(port, first[1]), "the first daemon never came up")
        old = daemon.read_state()
        assert old is not None
        pids.append(old.pid)

        monkeypatch.setattr(daemon, "current_version", lambda: "9.9.9")
        second = daemon.ensure_running(port)
        assert second is not None
        assert second[0] == port
        until(lambda: daemon.probe(port, second[1]), "the replacement never came up")
        pids.append(daemon.read_state().pid)

        time.sleep(1.0)  # the corpse's clear_state() lands somewhere in here
        assert daemon.probe(port, second[1]), "the URL the caller printed went dead"
        state = daemon.read_state()
        assert state is not None, "the corpse deleted the replacement's statefile"
        assert state.pid != old.pid and state.token == second[1]
        assert daemon.is_alive(state)
    finally:
        # The pids collected above are not sufficient on their own: this test spawns REAL
        # daemons, and a statefile read can race the write that records the replacement's pid,
        # so SIGTERMing only what we happened to observe left a daemon listening for its full
        # 60-minute idle timeout after the suite had finished. Re-read the statefile, escalate
        # to SIGKILL, then ASSERT the port is free so a future leak fails this test loudly
        # instead of quietly outliving the run.
        leftover = daemon.read_state()
        if leftover is not None:
            pids.append(leftover.pid)
        for pid in dict.fromkeys(pids):
            try:
                os.kill(pid, signal.SIGTERM)
            except OSError:
                pass
        if daemon.port_occupied(port):
            for pid in dict.fromkeys(pids):
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        until(lambda: not daemon.port_occupied(port),
              f"test leaked a daemon listening on {port}", timeout_s=5.0)


def test_ensure_running_falls_back_to_the_configured_port(monkeypatch, spawns):
    monkeypatch.setattr(daemon, "_configured_port", lambda: 4321)
    assert daemon.ensure_running()[0] == 4321


def test_configured_port_is_the_default_when_config_is_unusable(monkeypatch):
    from contexer import config

    monkeypatch.setattr(config, "load_ui_settings", raises(ValueError("malformed")),
                        raising=False)
    assert daemon._configured_port() == daemon.DEFAULT_PORT


# --- stop / status ---------------------------------------------------------------------

def test_stop_without_a_statefile():
    assert daemon.stop() is False


def test_stop_signals_and_clears(kills):
    daemon.write_state(a_state(pid=777))
    assert daemon.stop() is True
    assert kills == [(777, signal.SIGTERM)]
    assert not daemon.STATE_PATH.exists()


def test_stop_clears_the_statefile_even_when_the_pid_is_gone(monkeypatch):
    monkeypatch.setattr(os, "kill", raises(ProcessLookupError()))
    daemon.write_state(a_state(pid=777))
    assert daemon.stop() is False
    assert not daemon.STATE_PATH.exists()


def test_stop_on_a_daemon_that_was_never_spawned(kills):
    daemon.write_state(a_state(pid=0))  # minted, then the spawner died
    assert daemon.stop() is False
    assert kills == []  # pid 0 would signal the whole process group
    assert not daemon.STATE_PATH.exists()


def test_log_failure_is_swallowed(ui_paths, monkeypatch):
    ui_paths.parent.joinpath("blocked").write_text("not a directory")
    monkeypatch.setattr(daemon, "LOG_PATH", ui_paths.parent / "blocked" / "ui.log")
    daemon._log("unwritable")  # a read-only home must not turn into a hook crash


def test_status_not_running(monkeypatch):
    monkeypatch.setattr(daemon, "_configured_port", lambda: 31415)
    got = daemon.status()
    assert got["running"] is False and got["stale"] is False
    assert got["port"] == 31415 and got["url"] is None


def test_status_reports_a_stale_statefile(monkeypatch):
    monkeypatch.setattr(daemon, "probe", lambda port, token: False)
    daemon.write_state(a_state())
    got = daemon.status()
    assert got["running"] is False and got["stale"] is True


def test_status_running_carries_a_url_but_never_the_token(monkeypatch):
    monkeypatch.setattr(daemon, "probe", lambda port, token: True)
    daemon.write_state(a_state())
    got = daemon.status()
    assert got["running"] is True and got["pid"] == os.getpid()
    assert got["url"].startswith("http://127.0.0.1:31415/?p=")
    assert "tok-abc" not in json.dumps(got)


# --- pairing codes ---------------------------------------------------------------------

WINDOW_START = 600 * 1_666  # aligned so +599 stays inside the same window


def test_pairing_code_is_stable_within_a_window():
    assert (daemon.pairing_code("tok", now=WINDOW_START)
            == daemon.pairing_code("tok", now=WINDOW_START + 599))


def test_pairing_code_length_and_alphabet():
    code = daemon.pairing_code("tok", now=1_000_000)
    assert len(code) == 12
    assert set(code) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")


def test_pairing_code_is_token_specific():
    assert daemon.pairing_code("a", now=1_000_000) != daemon.pairing_code("b", now=1_000_000)


def test_verify_accepts_the_current_window():
    assert daemon.verify_pairing_code("tok", daemon.pairing_code("tok", now=1_000_000),
                                      now=1_000_000)


def test_verify_accepts_the_previous_window():
    code = daemon.pairing_code("tok", now=WINDOW_START)
    assert daemon.verify_pairing_code("tok", code, now=WINDOW_START + 600)


def test_verify_accepts_a_code_minted_just_before_the_window_rolled():
    """The human clicks seconds after the URL is printed; a window boundary must not 403."""
    minted_at = 600 * 5_000 + 599
    code = daemon.pairing_code("tok", now=minted_at)
    assert daemon.verify_pairing_code("tok", code, now=minted_at + 2)


def test_verify_rejects_two_windows_old():
    code = daemon.pairing_code("tok", now=WINDOW_START)
    assert not daemon.verify_pairing_code("tok", code, now=WINDOW_START + 1200)


def test_verify_rejects_another_token():
    code = daemon.pairing_code("tok-a", now=1_000_000)
    assert not daemon.verify_pairing_code("tok-b", code, now=1_000_000)


def test_verify_rejects_a_wrong_code_of_the_right_length():
    code = daemon.pairing_code("tok", now=1_000_000)
    wrong = ("A" if code[0] != "A" else "B") + code[1:]
    assert len(wrong) == len(code)  # compare_digest, not a length or prefix check
    assert not daemon.verify_pairing_code("tok", wrong, now=1_000_000)


@pytest.mark.parametrize("code", ["", "SHORT", "ééé", "\ud800", "A" * 4096])
def test_verify_never_raises_on_arbitrary_query_bytes(code):
    assert not daemon.verify_pairing_code("tok", code, now=1_000_000)


def test_verify_uses_the_wall_clock_by_default():
    assert daemon.verify_pairing_code("tok", daemon.pairing_code("tok"))


# --- url + watchdog helper -------------------------------------------------------------

def test_console_url_carries_the_pairing_code_not_the_token():
    url = daemon.console_url(31415, "tok-abc")
    assert url == f"http://127.0.0.1:31415/?p={daemon.pairing_code('tok-abc')}"
    assert "tok-abc" not in url


def test_console_url_deep_links_to_a_store():
    url = daemon.console_url(31415, "tok", "github.com-acme-widgets")
    assert "?p=" in url and url.endswith("#/store/github.com-acme-widgets")


def test_idle_deadline_exceeded_with_an_injected_clock():
    assert not daemon.idle_deadline_exceeded(100.0, 60.0, now=159.9)
    assert daemon.idle_deadline_exceeded(100.0, 60.0, now=160.0)


def test_idle_deadline_uses_monotonic_by_default():
    assert not daemon.idle_deadline_exceeded(time.monotonic(), 60.0)


# --- version ---------------------------------------------------------------------------

def test_current_version_reads_the_dist_info_name():
    version = daemon.current_version()
    assert version and version != "dev" and "/" not in version


def test_current_version_falls_back_outside_an_install(monkeypatch, tmp_path):
    monkeypatch.setattr(daemon, "__file__", str(tmp_path / "pkg" / "ui" / "daemon.py"))
    monkeypatch.setattr(daemon, "sys", types.SimpleNamespace(path=[]))
    assert daemon.current_version() == "dev"


# --- import discipline -----------------------------------------------------------------

def test_module_level_imports_are_within_the_allowlist():
    tree = ast.parse(Path(daemon.__file__).read_text())
    imported = set()
    for node in tree.body:  # module level only; lazy imports inside functions are the escape hatch
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported.add((node.module or "").split(".")[0])
    assert imported <= ALLOWED_IMPORTS, f"disallowed: {imported - ALLOWED_IMPORTS}"


def test_package_init_only_imports_the_daemon():
    from contexer import ui

    tree = ast.parse(Path(ui.__file__).read_text())
    modules = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}
    assert modules == {"contexer.ui.daemon"}  # server/api pull in http.server and the store


def test_importing_the_daemon_does_not_import_the_store():
    """The store costs 134ms. Checked in a fresh interpreter: pytest already imported it here."""
    probe = textwrap.dedent("""
        import sys
        import contexer.ui.daemon
        heavy = [m for m in ("contexer.store", "contexer.config", "contexer.server",
                             "contexer.share", "mcp", "http.client", "tempfile")
                 if m in sys.modules]
        print("HEAVY:" + ",".join(heavy))
    """)
    done = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                          cwd=str(Path(daemon.__file__).parents[2]))
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "HEAVY:", done.stdout


class TestClaimStateWaitsOutARival:
    """`_claim_state` loses to whoever wins O_EXCL and then has to READ what they wrote.

    The wait used to be 20 iterations with no sleep — well under a millisecond of real time, so
    on anything slower than a warm page cache it gave up and returned None, costing the console
    for that whole session. It is a deadline now, so the budget is a DURATION. The fakes below
    are therefore keyed on elapsed time, not on a call count: a count-based fake passes against
    either implementation and proves nothing.
    """

    def test_it_waits_out_a_rival_that_is_slow_to_fill_the_file(self, monkeypatch, tmp_path):
        monkeypatch.setattr(daemon, "STATE_PATH", tmp_path / "ui.json")
        daemon.STATE_PATH.touch()  # the rival created it and has not filled it yet
        rival = daemon.UiState(pid=4242, port=31415, token="rival-token",
                               started_at="2026-01-01T00:00:00Z", version="1.0")
        # 50ms is orders of magnitude past what 20 uninterrupted reads cover.
        ready_at = time.monotonic() + 0.05
        monkeypatch.setattr(daemon, "read_state",
                            lambda: rival if time.monotonic() >= ready_at else None)

        assert daemon._claim_state(31415, "1.0", None) == (rival, False), \
            "gave up and minted a rival token"

    def test_it_gives_up_on_a_file_that_is_never_filled(self, monkeypatch, tmp_path):
        monkeypatch.setattr(daemon, "STATE_PATH", tmp_path / "ui.json")
        monkeypatch.setattr(daemon, "CLAIM_WAIT_SECONDS", 0.01)
        daemon.STATE_PATH.touch()
        monkeypatch.setattr(daemon, "read_state", lambda: None)

        assert daemon._claim_state(31415, "1.0", None) is None

    def test_the_happy_path_never_waits(self, monkeypatch, tmp_path):
        """No statefile => O_EXCL succeeds => the wait is not entered at all."""
        monkeypatch.setattr(daemon, "STATE_PATH", tmp_path / "ui.json")
        monkeypatch.setattr(daemon.time, "sleep",
                            lambda _s: pytest.fail("slept on the uncontended path"))

        state, minted = daemon._claim_state(31415, "1.0", None)
        assert minted and state.token
