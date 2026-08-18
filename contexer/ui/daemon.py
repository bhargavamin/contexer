"""Lifecycle of the local console daemon: statefile, token, liveness, spawn, pairing codes.

Module-level imports are confined to a small stdlib set on purpose. `ensure_running` runs on the
SessionStart hook path, where importing `contexer.store` costs a measured 134ms and
`importlib.metadata` a further 28ms - against a whole-check budget of ~0.3ms warm. Anything
heavier than the imports below (config, http.client, tempfile) is imported inside the one
function that needs it, or hand-rolled. A test enforces the allowlist.

This module knows about processes and sockets only. It never reads a store.
"""
import base64
import hashlib
import hmac
import json
import os
import pathlib
import secrets
import signal
import socket
import subprocess
import sys
import time
from dataclasses import asdict, dataclass, replace

DEFAULT_PORT = 31415
SPAWN_TARGET = "contexer.ui.server"  # module-level so tests can spawn something harmless
WINDOW_SECONDS = 600
PROBE_TIMEOUT = 0.1
PORT_SETTLE_SECONDS = 2.0    # only ever spent on the kill-then-respawn path
PORT_SETTLE_TICK = 0.05
MINT_GRACE_SECONDS = 1.0     # how long an empty statefile is assumed to be a live mid-mint
# Only ever spent when another process is mid-mint, i.e. two session starts firing together.
CLAIM_WAIT_SECONDS = 0.25
CLAIM_WAIT_TICK = 0.002

# Path duplicated instead of taken from store.STORE_DIR - see the module docstring.
_STATE_DIR = pathlib.Path.home() / ".contexer"
STATE_PATH = _STATE_DIR / "ui.json"
LOG_PATH = _STATE_DIR / "ui.log"


@dataclass(frozen=True)
class UiState:
    """Contents of ~/.contexer/ui.json. `pid` is 0 between minting and spawn."""

    pid: int
    port: int
    token: str
    started_at: str
    version: str


def current_version() -> str:
    """Version of the wheel this process runs from, or "dev" from a bare source tree.

    Reads the dist-info directory NAME rather than calling importlib.metadata.version(),
    which costs 28ms of import on the hook path (68ms cold) to answer the same question in
    a couple of scandirs."""
    package_root = pathlib.Path(__file__).resolve().parents[2]
    roots = [package_root] + [pathlib.Path(p) for p in sys.path if p.endswith("site-packages")]
    for root in roots:
        for info in root.glob("contexer-*.dist-info"):
            return info.name[len("contexer-"):-len(".dist-info")]
    return "dev"


def read_state() -> UiState | None:
    """Parse the statefile, or None when it is absent, torn, or incomplete."""
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        state = UiState(pid=int(data["pid"]), port=int(data["port"]), token=str(data["token"]),
                        started_at=str(data["started_at"]), version=str(data["version"]))
    except (OSError, ValueError, TypeError, KeyError):
        return None
    return state if state.token and state.port > 0 else None


def write_state(state: UiState) -> None:
    """Replace the statefile atomically, mode 0600.

    Same guarantee as store._atomic_write (readers never see a torn file), hand-rolled because
    `tempfile` pulls in shutil and is outside this module's import budget."""
    STATE_PATH.parent.mkdir(mode=0o700, exist_ok=True)
    tmp = STATE_PATH.with_name(f"{STATE_PATH.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(json.dumps(asdict(state), indent=2))
        os.replace(tmp, STATE_PATH)
    finally:
        tmp.unlink(missing_ok=True)  # no-op after a successful replace


def clear_state(pid: int | None = None) -> None:
    """Drop the statefile, or with `pid` only while it still describes THAT process.

    A daemon that was SIGTERMed for a version upgrade races its own replacement: the replacement
    waits for the port, not for the corpse, so the corpse can reach its cleanup after the new
    statefile has been written. Unlinking it there would leave a live daemon nobody can find."""
    if pid is not None:
        state = read_state()
        if state is not None and state.pid != pid:
            return
    STATE_PATH.unlink(missing_ok=True)


def probe(port: int, token: str) -> bool:
    """True when our daemon answers /healthz on `port`. Never raises.

    Raw socket rather than http.client: one request does not justify the import, and every
    caller is on the hook path. /healthz requires the token, so a 200 identifies our daemon."""
    request = (f"GET /healthz HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
               f"X-Contexer-Token: {token}\r\nConnection: close\r\n\r\n").encode()
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=PROBE_TIMEOUT) as sock:
            sock.sendall(request)
            head = sock.recv(64)
    except OSError:
        return False
    # Status line only, and version-agnostic: http.server answers HTTP/1.0 unless the handler
    # opts into 1.1, and which one it is must not decide whether our own daemon looks dead.
    return head.startswith(b"HTTP/1.") and head.split(b" ")[1:2] == [b"200"]


def port_occupied(port: int) -> bool:
    """True when something accepts a connection on `port`. Says nothing about what it is -
    telling our daemon from a foreign process is `probe`'s job."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=PROBE_TIMEOUT):
            return True
    except OSError:
        return False


def await_port_free(port: int, timeout_s: float = PORT_SETTLE_SECONDS) -> bool:
    """Wait, bounded, for a SIGTERMed daemon to release `port`. True when it is free.

    SIGTERM is asynchronous: without this the replacement loses the bind race against the daemon
    it just killed, dies on EADDRINUSE, and the URL the caller already printed is dead."""
    deadline = time.monotonic() + timeout_s
    while port_occupied(port):
        if time.monotonic() >= deadline:
            return False
        time.sleep(PORT_SETTLE_TICK)
    return True


def is_alive(state: UiState) -> bool:
    """Liveness of the recorded daemon. The pid check is a 0.0004ms early-out; the token probe
    is the authority, so a statefile whose pid never got recorded still resolves correctly."""
    if state.pid > 1:
        try:
            os.kill(state.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            pass  # pid recycled by another user's process; let the probe decide
    return probe(state.port, state.token)


def ensure_running(port: int | None = None) -> tuple[int, str] | None:
    """Return (port, token) of a live console daemon, spawning one if needed, else None.

    `port` says where a NEW daemon binds; a live one always wins, so the port returned can differ
    from the port asked for - one console per machine, one statefile describing it. A caller that
    must honour a requested port checks read_state()/is_alive() itself first (cli.ui_cmd refuses
    rather than print a URL for a port nothing is bound on); moving a live console means stopping
    it, which is the caller's decision to take, not this function's.

    Never raises and never waits on the child: a console failure must cost the caller nothing but
    the console. The warm path does no I/O beyond one read and one probe. The only sleep is the
    upgrade path's wait for the old daemon's port, which a caller cannot be spared - without it
    the URL returned here would be dead."""
    try:
        version = current_version()
        state = read_state()
        if state is not None:
            if state.version != version:
                # A `uv tool upgrade` must not leave the old daemon serving a stale UI.
                if _terminate(state.pid):
                    await_port_free(state.port)
            elif is_alive(state):
                return state.port, state.token
            clear_state()
        elif _stale_statefile():
            # It exists but describes nothing. Left alone, _claim_state spins on it and the
            # console stays dead for good.
            clear_state()
        # The token outlives the daemon so an already-open tab keeps working across restarts.
        claimed = _claim_state(port or _configured_port(), version,
                              state.token if state else None)
        if claimed is None:
            return None
        state, minted = claimed
        if minted:
            state = replace(state, pid=_spawn(state.port))
            write_state(state)
        return state.port, state.token
    except Exception as exc:
        _log(f"ensure_running failed: {exc!r}")
        return None


def stop() -> bool:
    """SIGTERM the recorded daemon and drop the statefile. True when something was signalled."""
    state = read_state()
    if state is None:
        return False
    try:
        return _terminate(state.pid)
    finally:
        clear_state()


def status() -> dict:
    """Console state for `contexer ui --status`. Carries the pairing URL, never the token."""
    state = read_state()
    if state is None:
        return {"running": False, "stale": False, "pid": None, "port": _configured_port(),
                "started_at": None, "version": None, "url": None,
                "state_path": str(STATE_PATH), "log_path": str(LOG_PATH)}
    running = is_alive(state)
    return {"running": running, "stale": not running, "pid": state.pid, "port": state.port,
            "started_at": state.started_at, "version": state.version,
            "url": console_url(state.port, state.token) if running else None,
            "state_path": str(STATE_PATH), "log_path": str(LOG_PATH)}


def pairing_code(token: str, now: float | None = None) -> str:
    """Short-lived credential for the printed URL.

    The session-start line lands in transcripts on disk (~/.claude/projects/**/*.jsonl), which
    get shared and synced, so it must not carry the long-lived token."""
    return _code(token, _window(now))


def verify_pairing_code(token: str, code: str, now: float | None = None) -> bool:
    """Accept the current or previous window - a URL printed at 09:59 is still clickable at
    10:01. Constant-time, and tolerant of arbitrary query-string bytes."""
    window = _window(now)
    supplied = code.encode("utf-8", "replace")  # a real code is base32; any byte is comparable
    return any(hmac.compare_digest(_code(token, w).encode(), supplied)
               for w in (window, window - 1))


def console_url(port: int, token: str, slug: str = "") -> str:
    url = f"http://127.0.0.1:{port}/?p={pairing_code(token)}"
    return f"{url}#/store/{slug}" if slug else url


def idle_deadline_exceeded(last_request: float, timeout_s: float, now: float | None = None) -> bool:
    """Idle-watchdog predicate with an injectable clock, so the watchdog test needs no sleep."""
    return (time.monotonic() if now is None else now) - last_request >= timeout_s


def _window(now: float | None) -> int:
    return int(time.time() if now is None else now) // WINDOW_SECONDS


def _code(token: str, window: int) -> str:
    digest = hmac.new(token.encode(), str(window).encode(), hashlib.sha256).digest()
    return base64.b32encode(digest)[:12].decode()


def _stale_statefile() -> bool:
    """True when the statefile exists but cannot describe any daemon, so re-minting is safe.

    A zero-byte file is the one shape that might be a live race - `_claim_state` creates the file
    before filling it - so it gets a grace window. Anything non-empty that still does not parse is
    wreckage: no writer of ours produces it, and nothing is coming back to finish it."""
    try:
        stat = STATE_PATH.stat()
    except OSError:
        return False
    if read_state() is not None:
        return False
    return stat.st_size > 0 or time.time() - stat.st_mtime > MINT_GRACE_SECONDS


def _claim_state(port: int, version: str, token: str | None) -> tuple[UiState, bool] | None:
    """Create the statefile if absent; returns (state, minted). O_EXCL makes the creator the
    only process that ever mints a token, so two SessionStart hooks firing together cannot
    print two different credentials for one daemon."""
    STATE_PATH.parent.mkdir(mode=0o700, exist_ok=True)
    state = UiState(pid=0, port=port, token=token or secrets.token_urlsafe(32),
                    started_at=_now_iso(), version=version)
    try:
        fd = os.open(STATE_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        # The winner creates the file before filling it, so it can be observed empty for a
        # moment. Wait that out rather than mint a rival token.
        #
        # A bare 20-iteration spin (no sleep) covered well under a millisecond of real time and
        # lost the race it exists to win on any filesystem slower than a warm page cache, which
        # costs the console for the whole session. Bounded by a DEADLINE instead, so the budget
        # is a duration rather than an iteration count, and the tick keeps it off a busy loop.
        # Free in the common path: this branch is only reached when a rival already created the
        # file, and the first read almost always finds it already filled.
        deadline = time.monotonic() + CLAIM_WAIT_SECONDS
        while True:
            existing = read_state()
            if existing is not None:
                return existing, False
            if time.monotonic() >= deadline:
                return None
            time.sleep(CLAIM_WAIT_TICK)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(asdict(state), indent=2))
    return state, True


def _spawn(port: int) -> int:
    """Detach a daemon and return its pid. Deliberately never waited on."""
    LOG_PATH.parent.mkdir(mode=0o700, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as log:
        child = subprocess.Popen(
            [sys.executable, "-m", SPAWN_TARGET, "--port", str(port)],
            start_new_session=True, stdout=log, stderr=log, stdin=subprocess.DEVNULL)
    return child.pid


def _terminate(pid: int) -> bool:
    if pid <= 1:  # 0 = minted but never spawned
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return False
    return True


def _configured_port() -> int:
    """[ui] port from config.toml. Imported lazily (hook path), and fail-soft: a malformed
    config file must cost the console, not the session."""
    try:
        from contexer import config
        return config.load_ui_settings().port
    except Exception:
        return DEFAULT_PORT


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _log(message: str) -> None:
    """Append one line to ui.log. Never the token, never decision content."""
    try:
        LOG_PATH.parent.mkdir(mode=0o700, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as log:
            log.write(f"{_now_iso()} daemon: {message}\n")
    except OSError:
        pass
