"""Loopback HTTP transport for the local console: routing, auth guard, static files, watchdog.

This module knows HTTP and nothing else. Decisions, stores and config live behind `api.py`;
processes, tokens and pairing codes behind `daemon.py`.

The store is fed verbatim to agents as authoritative policy, so a successful write here is
persistent prompt injection into every future session in that repo. Reads are ordinary; the
mutation surface is what the layered guard below defends:

  bind 127.0.0.1 only -> Host allowlist (anti-DNS-rebinding) -> a credential on EVERY route
  -> and, for anything that writes, a secret the page cannot read plus a same-origin Origin.

A `?p=` pairing code counts as a credential on the exchange route (`/`) and nowhere else: it
travels in printed URLs and session transcripts, so all it may buy is the session cookie.

Run it with `python -m contexer.ui.server --port N`, or `contexer ui --foreground`.
"""
import collections
import errno
import hmac
import json
import os
import pathlib
import re
import secrets
import signal
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from contexer import auth, config
from contexer.ui import api, daemon

BODY_LIMIT = 64 * 1024          # a decision is 8000 chars; anything past this is not a save
MUTATIONS_PER_MINUTE = 60       # per process, across all clients
RATE_WINDOW = 60.0
WATCHDOG_TICK = 5.0
DEFAULT_IDLE_MINUTES = 60
REQUEST_TIMEOUT = 10.0          # a loopback client cannot honestly need longer for 64KB
# Waiting for a request to START is a completely different situation from waiting for one to
# FINISH, and conflating them broke the console. Browsers pre-open sockets and send nothing on
# them; a preconnect never carries a response, so `Connection: close` never applies to it and it
# sits in the browser's pool. Timing those out at REQUEST_TIMEOUT meant the server FIN'd a pooled
# socket at 10s while the console polls every 10s, so the next poll landed on a just-closed
# socket, got an empty reply, and fetch() rejected - the "Disconnected from the Contexer daemon"
# banner, with nothing in the log because no request was ever parsed. This must stay comfortably
# above any client's polling interval. The half-sent-body defence is unaffected: the tighter
# REQUEST_TIMEOUT is armed once the first byte arrives.
IDLE_TIMEOUT = 300.0
LOG_LINE_LIMIT = 200            # a request line is attacker-controlled and may be 64KB long

# No 'unsafe-inline' anywhere: console.js sets data-driven widths through the CSSOM and every
# handler is addEventListener, precisely so this policy can stay this strict.
CSP = ("default-src 'none'; style-src 'self'; script-src 'self'; "
       "img-src 'self' data:; connect-src 'self'")

COOKIE = "ctx_ui"
TOKEN_HEADER = "X-Contexer-Token"
POLL_HEADER = "X-Contexer-Poll"
MUTATING = ("POST", "PATCH", "PUT", "DELETE")
SERVED = ("GET", *MUTATING)

# Every credential this console has can be written as `<name>=<value>` in a request line:
# the pairing code (`?p=`), the session token (`?token=`, `ctx_ui=`) and the csrf value.
_SECRET_VALUE = re.compile(rf"(?i)\b(p|token|csrf|{COOKIE})=[^\s&;'\"]*")

# Static serving is a fixed allowlist, not a directory: nothing from the request is ever
# joined onto a path, so there is no traversal to defend and no listing to leak.
ASSET_DIR = pathlib.Path(__file__).resolve().parent / "assets"
ASSETS = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/console.css": ("console.css", "text/css; charset=utf-8"),
    "/console.js": ("console.js", "text/javascript; charset=utf-8"),
    # The favicon. Behind the same session gate as everything else: the browser sends the cookie
    # with it, so it resolves once paired and 401s before that, like every other route.
    "/icon.svg": ("icon.svg", "image/svg+xml; charset=utf-8"),
}


class _BodyTooLarge(Exception):
    """Raised instead of reading a body past BODY_LIMIT, so the bytes are never buffered."""


class ConsoleServer(ThreadingHTTPServer):
    """Loopback server holding the console's auth material, idle clock and rate budget.

    Binds 127.0.0.1 only - never 0.0.0.0. `port=0` takes an ephemeral port (tests), and
    `self.port` is always the port actually bound, which is what the Host/Origin checks
    compare against."""

    daemon_threads = True
    # Shutdown must never wait on a request thread: a client holding a half-sent body would
    # otherwise decide when SIGTERM finishes, and clear_state() runs after server_close().
    # ThreadingMixIn's join already skips daemon threads; this states the requirement rather
    # than leaving it resting on daemon_threads above.
    block_on_close = False

    def __init__(self, port: int, token: str, *,
                 idle_timeout_minutes: int = DEFAULT_IDLE_MINUTES, clock=time.monotonic,
                 request_timeout: float = REQUEST_TIMEOUT, idle_timeout: float = IDLE_TIMEOUT):
        super().__init__(("127.0.0.1", port), Handler)
        self.token = token
        self.request_timeout = request_timeout
        self.idle_timeout = idle_timeout
        # Minted per daemon start and held in memory ONLY: never persisted, never logged.
        # The session cookie is HttpOnly so the page cannot read it - which is exactly why a
        # separate value has to authorize mutations.
        self.csrf = secrets.token_urlsafe(32)
        self.port = self.server_address[1]
        self.version = daemon.current_version()
        self.started_at = _now_iso()
        self.clock = clock
        self.idle_timeout_seconds = max(idle_timeout_minutes, 1) * 60
        self.last_request = clock()
        self.stopping = threading.Event()
        self.exit_reason = ""
        self._mutations: collections.deque = collections.deque()
        self._lock = threading.Lock()

    def touch(self) -> None:
        self.last_request = self.clock()

    def allow_mutation(self) -> bool:
        """Budget one more write, or refuse. Bounds the damage a runaway script (or a page
        that somehow obtained the csrf value) can do to the store in a loop."""
        now = self.clock()
        with self._lock:
            while self._mutations and now - self._mutations[0] >= RATE_WINDOW:
                self._mutations.popleft()
            if len(self._mutations) >= MUTATIONS_PER_MINUTE:
                return False
            self._mutations.append(now)
            return True

    def stop(self, reason: str) -> None:
        """Ask serve_forever to return. `shutdown()` blocks until the loop exits, so it runs
        on its own thread - calling it from a request thread would deadlock."""
        self.exit_reason = reason
        self.stopping.set()
        threading.Thread(target=self.shutdown, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    server_version = "contexer-console"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def setup(self):
        # Two deadlines, deliberately different (see IDLE_TIMEOUT): a generous one while waiting
        # for a request to begin, so a browser's pooled preconnect is not FIN'd underneath it,
        # and a tight one once bytes are arriving, so a client that declares a Content-Length it
        # never finishes sending cannot hold this thread.
        self.timeout = self.server.idle_timeout
        super().setup()

    def handle_one_request(self):
        super().handle_one_request()
        self.timeout = self.server.idle_timeout  # re-arm for the next request on this socket

    def _arm_request_deadline(self):
        """Tighten the socket deadline now that a request line has been read."""
        try:
            self.connection.settimeout(self.server.request_timeout)
        except OSError:  # already closed by the peer; the read that follows will report it
            pass

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def do_PATCH(self):
        self._handle("PATCH")

    def do_PUT(self):
        self._handle("PUT")

    def do_DELETE(self):
        self._handle("DELETE")

    def do_HEAD(self):
        self._handle("HEAD")

    def do_OPTIONS(self):
        self._handle("OPTIONS")

    def __getattr__(self, name: str):
        """Route unknown verbs (TRACE, PROPFIND, FOO) through the guard as well.

        http.server answers a missing `do_<VERB>` with its own 501, which skips the Host check,
        the credential gate and the security headers - an unauthenticated surface."""
        if name.startswith("do_"):
            return lambda: self._handle(self.command)
        raise AttributeError(name)

    # ── the guard ───────────────────────────────────────────────────────────────

    def _handle(self, method: str) -> None:
        # A request line has been read, so this is a live client, not an idle preconnect: the
        # tight deadline applies from here on (it is what bounds an unfinished body read).
        self._arm_request_deadline()
        try:
            parsed = urllib.parse.urlparse(self.path)
            query = urllib.parse.parse_qs(parsed.query)
        except ValueError:
            # An absolute-form target with a broken IPv6 literal (`GET http://[::1/x`).
            return self._json(400, {"error": "malformed request target"})
        try:
            if not self._host_ok():
                return self._json(403, {"error": "bad Host header"})
            if not self._credentialed(query, parsed.path):
                return self._json(403, {"error": "not authenticated"})
            if method == "GET" and self._pairing_ok(query, parsed.path):
                return self._exchange()
            if method not in SERVED:
                # HEAD and OPTIONS are answered, never served. In particular no CORS preflight
                # is ever satisfied: one Access-Control-Allow-Origin here would undo the Origin
                # check below. Unknown verbs land here too, gated and with the same headers.
                return self._json(405, {"error": "method not allowed"})
            if method in MUTATING:
                if not self._mutation_ok():
                    return self._json(403, {"error": f"{TOKEN_HEADER} and a same-origin "
                                                     "Origin are required to change anything"})
                if not self.server.allow_mutation():
                    return self._json(429, {"error": "too many changes - slow down"})
            # A polling background tab must not keep the daemon alive forever, so its
            # requests deliberately leave the idle clock alone.
            if self.headers.get(POLL_HEADER) != "1":
                self.server.touch()
            if method == "GET" and parsed.path in ASSETS:
                return self._static(parsed.path)
            if method == "GET" and parsed.path == "/healthz":
                return self._json(200, self._healthz())
            status, payload = api.dispatch(method, parsed.path, query, self._read_body())
            self._json(status, payload)
        except _BodyTooLarge:
            self._json(413, {"error": f"request body over {BODY_LIMIT} bytes"})
        except api.ApiError as exc:
            self._json(exc.status, {"error": exc.message, **exc.extra})
        except TimeoutError:
            # The declared Content-Length never arrived. Answering (rather than waiting) is what
            # keeps a stalled client from holding a thread, and the process, indefinitely.
            self.close_connection = True
            self._json(408, {"error": "request timed out"})
        except (BrokenPipeError, ConnectionResetError):
            self.close_connection = True  # the browser navigated away mid-response
        except Exception:
            incident = secrets.token_hex(4)
            _log(f"{method} {parsed.path} 500 incident={incident}\n{traceback.format_exc()}")
            self._json(500, {"error": "internal error", "incident": incident})

    def _host_ok(self) -> bool:
        """Anti-DNS-rebinding. Without this, `evil.com` re-resolving to 127.0.0.1 can read
        the whole store from a victim's browser: the connection really is to loopback, so
        binding alone proves nothing about who is asking."""
        host = (self.headers.get("Host") or "").strip()
        return host in (f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}")

    def _origin_ok(self) -> bool:
        origin = (self.headers.get("Origin") or "").strip()
        return origin in (f"http://127.0.0.1:{self.server.port}",
                          f"http://localhost:{self.server.port}")

    def _token_ok(self) -> bool:
        """Either secret in X-Contexer-Token authenticates. The csrf value is only obtainable
        from an already-authenticated /healthz, so accepting it here grants nothing new, and
        the contract needs a curl caller to work with one header and no handshake."""
        supplied = self.headers.get(TOKEN_HEADER) or ""
        return _same(supplied, self.server.token) or _same(supplied, self.server.csrf)

    def _cookie_ok(self) -> bool:
        for morsel in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = morsel.strip().partition("=")
            if name == COOKIE and _same(value, self.server.token):
                return True
        return False

    def _pairing_ok(self, query: dict, path: str) -> bool:
        """A pairing code authenticates the exchange route and nothing else.

        Codes are short-lived, but they land in transcripts on disk. Confining them to `/` means
        a leaked one can only start a session in a browser - never read the store or write a
        global rule straight from the query string."""
        if path != "/":
            return False
        codes = query.get("p") or []
        return bool(codes) and daemon.verify_pairing_code(self.server.token, codes[0])

    def _credentialed(self, query: dict, path: str) -> bool:
        """Every route needs one of these three. There is no unauthenticated surface at all -
        not /healthz, not the static assets, not an unknown verb."""
        return self._pairing_ok(query, path) or self._token_ok() or self._cookie_ok()

    def _mutation_ok(self) -> bool:
        """A cookie alone must NEVER authorize a write: it rides along on any cross-site
        request. The csrf value has to be read from /healthz by same-origin script (or be
        the persisted token, for curl), and the Origin has to be this console."""
        return self._token_ok() and self._origin_ok()

    # ── responses ───────────────────────────────────────────────────────────────

    def _exchange(self) -> None:
        """Trade a valid pairing code for the session cookie.

        The redirect target carries NO fragment, so the browser keeps the `#/store/<slug>`
        deep link from the printed URL and the console lands on the right repo."""
        self.send_response(302)
        self.send_header("Location", "/")
        self.send_header("Set-Cookie",
                         f"{COOKIE}={self.server.token}; HttpOnly; SameSite=Strict; Path=/")
        self.send_header("Content-Length", "0")
        self._finish_headers()

    def _static(self, path: str) -> None:
        name, content_type = ASSETS[path]
        try:
            body = (ASSET_DIR / name).read_bytes()
        except OSError:
            return self._json(404, {"error": "asset missing"})
        self._respond(200, content_type, body)

    def _healthz(self) -> dict:
        return {"ok": True, "version": self.server.version, "pid": os.getpid(),
                "started_at": self.server.started_at, "csrf": self.server.csrf,
                "port": self.server.port}

    def _read_body(self) -> object:
        raw_length = (self.headers.get("Content-Length") or "0").strip()
        if not raw_length.isdigit():
            raise api.ApiError(400, "invalid Content-Length")
        length = int(raw_length)
        if length > BODY_LIMIT:
            raise _BodyTooLarge
        if length == 0:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise api.ApiError(400, "body is not valid JSON") from exc

    def _json(self, status: int, payload: object) -> None:
        self._respond(status, "application/json", json.dumps(payload).encode("utf-8"))

    def _respond(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self._finish_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _finish_headers(self) -> None:
        # Connection: close keeps a rejected request's unread body from being parsed as the next
        # request on the same socket. send_error() sends its own, so it is not in end_headers.
        self.send_header("Connection", "close")
        self.end_headers()

    def end_headers(self) -> None:
        """The policy headers ride on EVERY response. No CORS headers, ever.

        Overridden rather than called from `_finish_headers` so that the replies http.server
        writes on its own - send_error() for a request line it could not parse - carry them."""
        self.send_header("Content-Security-Policy", CSP)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_error(self, code, message=None, explain=None):
        """Answer a request line we could not parse with a real status line.

        parse_request() rejects a bad version before it records one, so the base class falls back
        to HTTP/0.9 framing: a bare HTML body, no status line, no headers. Someone typing
        `https://` at this port deserves to be told 400."""
        if self.request_version == "HTTP/0.9":
            self.request_version = self.protocol_version
        super().send_error(code, message, explain)

    # ── logging ─────────────────────────────────────────────────────────────────

    def log_request(self, code="-", size="-"):
        # getattr, not self.path: parse_request() calls send_error() - which logs through here -
        # for a bad request version, bad request syntax and a 414, all before `path` exists.
        # The query string is dropped as well as redacted; `?p=` carries a live credential.
        path = getattr(self, "path", "").split("?", 1)[0]
        if not path:
            return  # send_error() already logged the reason; a second bare line adds nothing
        self.log_message("%s %s %s", getattr(self, "command", None) or "-", path, code)

    def log_message(self, fmt, *args):
        # Every logging path funnels into _log, which redacts and truncates: send_error() and
        # log_error() reflect the RAW request line, which is attacker-controlled.
        _log(fmt % args)


def watchdog(server: ConsoleServer, *, clock=time.monotonic, tick: float = WATCHDOG_TICK) -> None:
    """Shut the daemon down once it has been idle for the configured window.

    Self-cleaning: a respawn costs ~2.5ms, so an unused console has no business holding a
    port and a process. `clock`/`tick` are injected by the test, which therefore needs no
    sleep to prove the deadline."""
    while not server.stopping.wait(tick):
        if daemon.idle_deadline_exceeded(server.last_request, server.idle_timeout_seconds,
                                         now=clock()):
            _log(f"idle for {server.idle_timeout_seconds:.0f}s - shutting down")
            server.stop("idle")
            return


def main(argv: list[str] | None = None) -> int:
    """Serve the console in the foreground until idle, SIGTERM, or SIGINT.

    Both `python -m contexer.ui.server --port N` (the daemon spawn) and `contexer ui
    --foreground` land here."""
    args = sys.argv[1:] if argv is None else argv
    port = _port_arg(args)
    state = daemon.read_state()
    token = state.token if state is not None else None
    try:
        server = ConsoleServer(port, token or secrets.token_urlsafe(32),
                               idle_timeout_minutes=_idle_timeout_minutes())
    except OSError as exc:
        return _port_unavailable(exc, port, token)

    # Record this process so `contexer ui --status/--stop` and the SessionStart liveness
    # probe find it. The token is carried over when one exists, so a restart never
    # invalidates an already-open tab.
    daemon.write_state(daemon.UiState(pid=os.getpid(), port=server.port, token=server.token,
                                      started_at=server.started_at, version=server.version))
    signal.signal(signal.SIGTERM, lambda *_: server.stop("SIGTERM"))
    signal.signal(signal.SIGINT, lambda *_: server.stop("SIGINT"))
    threading.Thread(target=watchdog, args=(server,), daemon=True).start()
    _log(f"listening on 127.0.0.1:{server.port}, "
         f"idle timeout {server.idle_timeout_seconds // 60} min")
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        # A login started from the console is our child, holding a browser flow and its own
        # loopback port. Every exit lands here (SIGTERM, SIGINT, idle), so this is the one place
        # that can guarantee none of them outlives the daemon that opened them.
        if auth.stop_login_job():
            _log("killed the login subprocess still running at shutdown")
        # The statefile is a liveness claim; leaving it behind makes the next check lie. Scoped
        # to our own pid: on the upgrade path a replacement daemon has already claimed the file
        # by now, and this process is the corpse.
        daemon.clear_state(pid=os.getpid())
        _log(f"stopped ({server.exit_reason or 'closed'})")
    return 0


def _port_unavailable(exc: OSError, port: int, token: str | None) -> int:
    """Bind-as-mutex: the port IS the lock, so no lockfile and no race by construction.

    Our own daemon already serving is a success (two SessionStart hooks firing together is
    normal); anything else is a foreign process and has to be reported rather than shadowed."""
    if exc.errno == errno.EADDRINUSE and token and daemon.probe(port, token):
        return 0
    # stderr only: `_spawn` points the daemon's stderr at ui.log, so logging it as well would
    # write the same line twice, while `contexer ui --foreground` needs it on the terminal.
    print(f"contexer: cannot bind 127.0.0.1:{port}: {exc}", file=sys.stderr)
    return 1


def _port_arg(args: list[str]) -> int:
    if "--port" in args:
        index = args.index("--port")
        value = args[index + 1] if index + 1 < len(args) else ""
        if not value.isdigit():
            print("contexer.ui.server: --port requires a port number", file=sys.stderr)
            raise SystemExit(2)
        return int(value)
    return _ui_setting("port", daemon.DEFAULT_PORT)


def _idle_timeout_minutes() -> int:
    return _ui_setting("idle_timeout_minutes", DEFAULT_IDLE_MINUTES)


def _ui_setting(name: str, fallback: int) -> int:
    """A `[ui]` setting, fail-soft: a malformed config.toml must cost the setting, not the
    console (the developer has no other window into the daemon when it won't start)."""
    try:
        return int(getattr(config.load_ui_settings(), name))
    except Exception:
        return fallback


def _same(supplied: str, secret: str) -> bool:
    """Constant-time comparison. `errors="replace"` because a header value can carry any
    bytes and a comparison must never raise on the way to returning False."""
    return hmac.compare_digest(supplied.encode("utf-8", "replace"),
                               secret.encode("utf-8", "replace"))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _scrub(message: str) -> str:
    """Redact credentials and bound the length of anything on its way to ui.log.

    The single choke point for it: http.server's own error messages quote the raw request line,
    so `?p=<live pairing code>` reaches the log through send_error(), and that line can be 64KB
    of attacker-chosen bytes."""
    redacted = _SECRET_VALUE.sub(lambda match: f"{match.group(1)}=REDACTED", message)
    return "\n".join(line[:LOG_LINE_LIMIT] for line in redacted.splitlines())


def _log(message: str) -> None:
    """Append one line to ~/.contexer/ui.log. NEVER the token, the csrf secret, or decision
    content - this file gets pasted into bug reports."""
    message = _scrub(message)
    try:
        daemon.LOG_PATH.parent.mkdir(mode=0o700, exist_ok=True)
        with open(daemon.LOG_PATH, "a", encoding="utf-8") as log:
            log.write(f"{_now_iso()} console: {message}\n")
    except OSError:
        pass


if __name__ == "__main__":
    sys.exit(main())
