"""Tests for `contexer ui` — the console subcommand — and the repo-store count in `status`."""
import json
import os
import signal
import socket
import subprocess
import sys
import types
import webbrowser

import pytest

from contexer import cli, updates
from contexer.ui import daemon


@pytest.fixture(autouse=True)
def ui_home(tmp_path, monkeypatch):
    """A throwaway HOME with the statefile and log inside it — never the real ~/.contexer.

    Also pins the daemon's configured-port lookup and the PyPI check: `contexer ui` and
    `contexer status` must not read the developer's real config or touch the network."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(daemon, "STATE_PATH", tmp_path / ".contexer" / "ui.json")
    monkeypatch.setattr(daemon, "LOG_PATH", tmp_path / ".contexer" / "ui.log")
    monkeypatch.setattr(daemon, "_configured_port", lambda: daemon.DEFAULT_PORT)
    monkeypatch.setattr(updates, "refresh", lambda force=False: {})
    return tmp_path


@pytest.fixture(autouse=True)
def occupied_ports(monkeypatch):
    """Ports `daemon.port_occupied` should report as taken — none, unless a test binds one.

    The real probe opens a TCP connection to the FIXED default port, so without this the
    default-port tests passed or failed depending on whether the developer happened to be
    running a real console on this machine: `contexer ui` reported the port busy and exited 1
    in eight tests that never asked about a foreign listener. A feature must not break its own
    suite by being in use. The probe itself is covered against a purpose-bound port in
    tests/test_ui_daemon.py."""
    taken: set[int] = set()
    monkeypatch.setattr(daemon, "port_occupied", lambda port: port in taken)
    return taken


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
def opened(monkeypatch):
    """Record browser launches instead of opening one."""
    urls = []
    monkeypatch.setattr(webbrowser, "open", lambda url: urls.append(url) or True)
    return urls


@pytest.fixture
def kills(monkeypatch):
    """Record signals instead of sending them — a recorded pid may exist on this machine."""
    sent = []
    monkeypatch.setattr(os, "kill", lambda pid, sig: sent.append((pid, sig)))
    return sent


@pytest.fixture
def listener(occupied_ports):
    """A bound, listening loopback socket standing in for whatever holds the port."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    occupied_ports.add(sock.getsockname()[1])
    yield sock
    sock.close()


def a_state(**overrides) -> daemon.UiState:
    fields = {"pid": os.getpid(), "port": daemon.DEFAULT_PORT, "token": "tok-abc",
              "started_at": "2026-07-31T00:00:00Z", "version": daemon.current_version()}
    return daemon.UiState(**{**fields, **overrides})


# ── default / --open ────────────────────────────────────────────────────────

class TestUiStart:
    def test_prints_the_url_and_the_log_location(self, spawns, capsys):
        cli.ui_cmd([])
        out = capsys.readouterr().out
        assert f"Console: http://127.0.0.1:{daemon.DEFAULT_PORT}/?p=" in out
        assert str(daemon.LOG_PATH) in out

    def test_spawns_a_detached_daemon_on_the_configured_port(self, spawns):
        cli.ui_cmd([])
        assert spawns[0]["argv"] == [sys.executable, "-P", "-m", "contexer.ui.server",
                                    "--port", str(daemon.DEFAULT_PORT)]
        assert spawns[0]["start_new_session"] is True

    def test_the_url_never_carries_the_console_token(self, spawns, capsys):
        cli.ui_cmd([])
        token = daemon.read_state().token
        assert token not in capsys.readouterr().out

    def test_warm_start_reuses_the_running_daemon(self, monkeypatch, spawns, capsys):
        monkeypatch.setattr(daemon, "probe", lambda port, token: True)
        daemon.write_state(a_state())
        cli.ui_cmd([])
        assert spawns == []
        assert f"?p={daemon.pairing_code('tok-abc')}" in capsys.readouterr().out

    def test_open_launches_the_printed_url(self, spawns, opened, capsys):
        cli.ui_cmd(["--open"])
        printed = capsys.readouterr().out.split("Console: ")[1].splitlines()[0]
        assert opened == [printed]

    def test_no_browser_without_open(self, spawns, opened, capsys):
        cli.ui_cmd([])
        assert opened == []

    def test_reports_a_spawn_failure_with_the_log_path(self, monkeypatch, capsys):
        monkeypatch.setattr(daemon, "ensure_running", lambda port=None: None)
        with pytest.raises(SystemExit) as exc:
            cli.ui_cmd([])
        assert exc.value.code == 1
        assert str(daemon.LOG_PATH) in capsys.readouterr().err


# ── --port ──────────────────────────────────────────────────────────────────

class TestUiPort:
    def test_flag_overrides_the_configured_port(self, spawns, capsys):
        cli.ui_cmd(["--port", "45678"])
        assert spawns[0]["argv"][-1] == "45678"
        assert "http://127.0.0.1:45678/?p=" in capsys.readouterr().out

    def test_config_supplies_the_port_when_the_flag_is_absent(self, ui_home, spawns, capsys):
        (ui_home / ".contexer").mkdir(parents=True, exist_ok=True)
        (ui_home / ".contexer" / "config.toml").write_text("[ui]\nport = 45999\n")
        cli.ui_cmd([])
        assert "http://127.0.0.1:45999/?p=" in capsys.readouterr().out

    @pytest.mark.parametrize("argv", [["--port"], ["--port", "nope"], ["--port", "0"],
                                      ["--port", "70000"], ["--port", "-1"]])
    def test_rejects_a_port_that_is_not_a_port(self, argv, spawns, capsys):
        with pytest.raises(SystemExit) as exc:
            cli.ui_cmd(argv)
        assert exc.value.code == 1
        assert "--port requires a port number" in capsys.readouterr().err
        assert spawns == []

    def test_refuses_when_a_console_is_already_running_on_another_port(self, monkeypatch,
                                                                      spawns, capsys):
        """`ensure_running` short-circuits on a live daemon and returns ITS port, so the flag
        was dropped and the URL printed named a port nothing was ever bound on."""
        monkeypatch.setattr(daemon, "probe", lambda p, t: True)
        daemon.write_state(a_state())  # alive on the default port
        with pytest.raises(SystemExit) as exc:
            cli.ui_cmd(["--port", "45678"])
        assert exc.value.code == 1
        captured = capsys.readouterr()
        assert f"already running on port {daemon.DEFAULT_PORT}" in captured.err
        assert "contexer ui --stop && contexer ui --port 45678" in captured.err
        assert "Console: http" not in captured.out, "never print a URL nothing answers on"
        assert spawns == []

    def test_a_dead_daemon_on_another_port_does_not_block_the_flag(self, monkeypatch,
                                                                   spawns, capsys):
        """Only a LIVE incumbent is a reason to refuse — a stale statefile holds no port."""
        monkeypatch.setattr(daemon, "probe", lambda p, t: False)
        daemon.write_state(a_state())
        cli.ui_cmd(["--port", "45678"])
        assert spawns[0]["argv"][-1] == "45678"
        assert "http://127.0.0.1:45678/?p=" in capsys.readouterr().out

    def test_a_changed_config_port_still_prints_the_running_console(self, ui_home, monkeypatch,
                                                                    spawns, capsys):
        """`[ui] port` applies at the next start (docs/ui.md) and the URL printed here is a live
        one, so this is not the same lie: only an explicit `--port` is a request about THIS
        invocation, which must be honoured or refused."""
        (ui_home / ".contexer").mkdir(parents=True, exist_ok=True)
        (ui_home / ".contexer" / "config.toml").write_text("[ui]\nport = 45999\n")
        monkeypatch.setattr(daemon, "probe", lambda p, t: True)
        daemon.write_state(a_state())
        cli.ui_cmd([])
        assert f"http://127.0.0.1:{daemon.DEFAULT_PORT}/?p=" in capsys.readouterr().out
        assert spawns == []

    def test_reports_a_malformed_config_instead_of_a_traceback(self, ui_home, capsys):
        (ui_home / ".contexer").mkdir(parents=True, exist_ok=True)
        (ui_home / ".contexer" / "config.toml").write_text('[ui]\nport = "nine"\n')
        with pytest.raises(SystemExit) as exc:
            cli.ui_cmd([])
        assert exc.value.code == 1
        assert "contexer ui: invalid ui.port" in capsys.readouterr().err


# ── a foreign process on the port ───────────────────────────────────────────

class TestUiPortInUse:
    def test_names_the_config_key_to_change(self, listener, spawns, capsys):
        port = listener.getsockname()[1]
        with pytest.raises(SystemExit) as exc:
            cli.ui_cmd(["--port", str(port)])
        assert exc.value.code == 1
        err = capsys.readouterr().err
        assert f"Port {port} is in use by another process" in err
        assert "[ui] port" in err
        assert str(cli._ui_config_path()) in err, "name the file this invocation actually reads"
        assert "--port N" in err
        assert spawns == [], "never spawn a daemon that cannot bind"

    def test_our_own_console_on_that_port_is_not_foreign(self, monkeypatch, listener,
                                                        spawns, capsys):
        """The occupied-port check must not fire on a warm start — that is our own listener."""
        port = listener.getsockname()[1]
        monkeypatch.setattr(daemon, "probe", lambda p, t: True)
        daemon.write_state(a_state(port=port))
        cli.ui_cmd(["--port", str(port)])
        assert f"http://127.0.0.1:{port}/?p=" in capsys.readouterr().out
        assert spawns == []

    def test_waits_out_our_own_dying_daemon_before_blaming_another_process(self, monkeypatch,
                                                                          spawns, capsys):
        """`contexer ui --stop && contexer ui` is the documented way to apply a config change,
        and `--stop` drops the statefile before the SIGTERMed daemon has released the socket —
        so waiting the port out is all that tells our own corpse from a foreign listener."""
        probes = []
        monkeypatch.setattr(daemon, "port_occupied",
                            lambda port: probes.append(port) or len(probes) < 3)
        cli.ui_cmd([])
        assert probes[:3] == [daemon.DEFAULT_PORT] * 3  # polled until the port went quiet
        assert len(spawns) == 1
        assert f"http://127.0.0.1:{daemon.DEFAULT_PORT}/?p=" in capsys.readouterr().out

    def test_the_check_goes_through_the_daemons_probe(self):
        """cli kept its own copy of this probe; two spellings of one question drift apart."""
        assert not hasattr(cli, "_port_occupied")
        assert not hasattr(cli, "_await_port_free")


# ── --status ────────────────────────────────────────────────────────────────

class TestUiStatus:
    def test_not_running(self, capsys):
        cli.ui_cmd(["--status"])
        out = capsys.readouterr().out
        assert "state:        not running" in out
        assert f"port:         {daemon.DEFAULT_PORT}" in out
        assert str(daemon.STATE_PATH) in out and str(daemon.LOG_PATH) in out
        assert "url:" not in out

    def test_running_shows_the_pid_and_a_pairing_url(self, monkeypatch, capsys):
        monkeypatch.setattr(daemon, "probe", lambda port, token: True)
        daemon.write_state(a_state())  # a_state() records a pid that really is alive
        cli.ui_cmd(["--status"])
        out = capsys.readouterr().out
        assert f"state:        running (pid {os.getpid()}, started 2026-07-31T00:00:00Z)" in out
        assert f"url:          http://127.0.0.1:{daemon.DEFAULT_PORT}/?p=" in out
        assert "tok-abc" not in out

    def test_stale_statefile_is_called_out(self, monkeypatch, capsys):
        monkeypatch.setattr(daemon, "probe", lambda port, token: False)
        daemon.write_state(a_state())
        cli.ui_cmd(["--status"])
        assert "stale statefile" in capsys.readouterr().out

    def test_starts_nothing(self, spawns, capsys):
        cli.ui_cmd(["--status"])
        assert spawns == []
        assert not daemon.STATE_PATH.exists()


# ── --stop ──────────────────────────────────────────────────────────────────

class TestUiStop:
    def test_nothing_running(self, capsys):
        cli.ui_cmd(["--stop"])
        assert "Console was not running." in capsys.readouterr().out

    def test_signals_and_clears(self, kills, capsys):
        daemon.write_state(a_state(pid=777))
        cli.ui_cmd(["--stop"])
        assert kills == [(777, signal.SIGTERM)]
        assert not daemon.STATE_PATH.exists()
        assert "Console stopped." in capsys.readouterr().out

    def test_starts_nothing(self, spawns, kills):
        daemon.write_state(a_state(pid=777))
        cli.ui_cmd(["--stop"])
        assert spawns == []


# ── --reset-token ───────────────────────────────────────────────────────────

class TestUiResetToken:
    def test_mints_a_fresh_token(self, kills, spawns, capsys):
        daemon.write_state(a_state(pid=777, token="leaked-token"))
        cli.ui_cmd(["--reset-token"])
        assert kills == [(777, signal.SIGTERM)]
        assert daemon.read_state().token != "leaked-token"
        assert len(spawns) == 1

    def test_waits_for_the_old_daemon_to_release_the_port(self, monkeypatch, kills, spawns):
        """Otherwise the replacement loses the bind race and the corpse looks foreign."""
        probes = []
        monkeypatch.setattr(daemon, "port_occupied",
                            lambda port: probes.append(port) or len(probes) < 3)
        daemon.write_state(a_state(pid=777))
        cli.ui_cmd(["--reset-token"])
        assert probes[:3] == [daemon.DEFAULT_PORT] * 3  # polled until the port went quiet
        assert len(spawns) == 1

    def test_does_not_wait_when_nothing_was_running(self, monkeypatch, spawns):
        monkeypatch.setattr(daemon, "await_port_free",
                            lambda port, **kw: pytest.fail("waited on a port nobody held"))
        cli.ui_cmd(["--reset-token"])
        assert len(spawns) == 1


# ── --foreground ────────────────────────────────────────────────────────────

class TestUiForeground:
    @pytest.fixture
    def fake_server(self, monkeypatch):
        """Stand in for contexer.ui.server, which the daemon process owns."""
        from contexer import ui

        calls = []
        module = types.ModuleType("contexer.ui.server")
        module.main = lambda argv=None: calls.append(argv)
        monkeypatch.setitem(sys.modules, "contexer.ui.server", module)
        monkeypatch.setattr(ui, "server", module, raising=False)
        return calls

    def test_delegates_to_the_server_in_this_process(self, fake_server, spawns):
        cli.ui_cmd(["--foreground"])
        assert fake_server == [["--port", str(daemon.DEFAULT_PORT)]]
        assert spawns == [], "--foreground must not detach a second daemon"
        assert not daemon.STATE_PATH.exists(), "the server owns the statefile in this mode"

    def test_passes_the_port_flag_through(self, fake_server, spawns):
        cli.ui_cmd(["--foreground", "--port", "45678"])
        assert fake_server == [["--port", "45678"]]

    @pytest.mark.parametrize("code", [1, 2])
    def test_propagates_the_servers_exit_code(self, monkeypatch, spawns, code):
        """server.main RETURNS the exit code, so a failed bind must not exit 0 — a supervisor
        (or a shell script) reads that as a console that is up and serving."""
        from contexer import ui

        module = types.ModuleType("contexer.ui.server")
        module.main = lambda argv=None: code
        monkeypatch.setitem(sys.modules, "contexer.ui.server", module)
        monkeypatch.setattr(ui, "server", module, raising=False)
        with pytest.raises(SystemExit) as exc:
            cli.ui_cmd(["--foreground"])
        assert exc.value.code == code

    def test_a_clean_run_does_not_exit_nonzero(self, monkeypatch, spawns):
        from contexer import ui

        module = types.ModuleType("contexer.ui.server")
        module.main = lambda argv=None: 0
        monkeypatch.setitem(sys.modules, "contexer.ui.server", module)
        monkeypatch.setattr(ui, "server", module, raising=False)
        cli.ui_cmd(["--foreground"])


# ── dispatch + usage ────────────────────────────────────────────────────────

class TestUiDispatch:
    def test_main_routes_ui_and_forwards_the_flags(self, monkeypatch):
        seen = []
        monkeypatch.setattr(cli, "ui_cmd", lambda rest: seen.append(rest))
        cli.dispatch(["ui", "--open", "--port", "45678"])
        assert seen == [["--open", "--port", "45678"]]

    def test_ui_is_guarded_against_a_root_owned_home(self, monkeypatch, capsys):
        def denied(rest):
            raise PermissionError(13, "denied", str(daemon.STATE_PATH))
        monkeypatch.setattr(cli, "ui_cmd", denied)
        with pytest.raises(SystemExit) as exc:
            cli.dispatch(["ui"])
        assert exc.value.code == 1
        assert "never needs sudo" in capsys.readouterr().err

    def test_usage_documents_the_command(self):
        assert "  ui            " in cli.USAGE
        for flag in ("--open", "--stop", "--status", "--port N", "--foreground",
                     "--reset-token"):
            assert flag in cli.USAGE


# ── status(): one repo store per repo ───────────────────────────────────────

class TestRepoStoreCount:
    """`contexer status` used to count every *.json in ~/.contexer as a repo store."""

    def _store_dir(self, home):
        path = home / ".contexer"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def test_sidecars_globals_and_caches_are_not_repo_stores(self, ui_home, capsys):
        store_dir = self._store_dir(ui_home)
        (store_dir / "repo-abc12345.json").write_text(
            json.dumps({"entries": [{"id": "1"}, {"id": "2"}]}))
        (store_dir / "repo-abc12345.deleted.json").write_text(
            json.dumps({"repo_path": "/r", "entries": [{"id": "3"}]}))
        (store_dir / "_global.json").write_text(json.dumps({"entries": [{"id": "4"}]}))
        (store_dir / "ui.json").write_text(json.dumps({"pid": 1, "port": 31415}))
        (store_dir / ".team_repo-abc12345.json").write_text(json.dumps({"decisions": []}))
        (store_dir / ".retrieval_index_repo-abc12345.json").write_text(json.dumps({"docs": {}}))
        cli.status()
        assert "repo stores:  1 (2 entries total)" in capsys.readouterr().out

    def test_a_legacy_underscore_slug_still_counts(self, ui_home, capsys):
        """`_slug` keeps a leading underscore from /_vendor/app, so `_`-prefixed is not a tell."""
        store_dir = self._store_dir(ui_home)
        (store_dir / "_vendor_app-abc12345.json").write_text(
            json.dumps({"entries": [{"id": "1"}]}))
        cli.status()
        assert "repo stores:  1 (1 entries total)" in capsys.readouterr().out

    def test_the_global_store_is_named_by_the_store_constant(self, ui_home, monkeypatch,
                                                             capsys):
        """cli spelled `_global.json` out again; renaming the slug in store.py then silently
        turned the global store into a phantom repo store."""
        from contexer import store

        monkeypatch.setattr(store, "GLOBAL_SLUG", "_globals")
        store_dir = self._store_dir(ui_home)
        (store_dir / "_globals.json").write_text(json.dumps({"entries": [{"id": "g"}]}))
        (store_dir / "repo-abc12345.json").write_text(json.dumps({"entries": [{"id": "1"}]}))
        cli.status()
        assert "repo stores:  1 (1 entries total)" in capsys.readouterr().out

    def test_two_repos_count_as_two(self, ui_home, capsys):
        store_dir = self._store_dir(ui_home)
        for slug in ("a-11111111", "b-22222222"):
            (store_dir / f"{slug}.json").write_text(json.dumps({"entries": [{"id": slug}]}))
            (store_dir / f"{slug}.deleted.json").write_text(json.dumps({"entries": []}))
        cli.status()
        assert "repo stores:  2 (2 entries total)" in capsys.readouterr().out
