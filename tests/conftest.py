"""Shared pytest fixtures for the contexer test suite."""
import types
from pathlib import Path

import pytest

from contexer import remote, store


def pytest_collection_modifyitems(config, items):
    """Mark the benchmark-harness tests so CI can deselect them (`-m "not slow"`), and skip
    `perf` tests whenever coverage is measuring, because a wall-clock number taken under a
    tracer is not the number the assertion is about.

    ponytail: one marker, not a tier taxonomy — the harness files are the only
    slow ones (~45s vs ~30s for everything else combined). Split further only if
    a second slow area appears.

    The perf skip is the root-cause fix for a real intermittent failure, not tidiness.
    `addopts` turns coverage on for every bare `pytest` run, and coverage's tracer inflates
    the measured path ~5x: `test_index_lookup_meets_latency_budget` measures ~0.9ms p50
    uninstrumented (the figure its docstring pins) but ~4.7ms against a 5.0ms budget under
    `--cov`, so whether it passes came down to machine load at that moment. Raising the
    budget was the wrong fix — it would have to reach past the live-scan's ~7.7ms to be
    safe, which is precisely the regression the assertion exists to catch. The marker
    already declares these "meaningful only on fixed hardware, never in CI" and CI already
    deselects them; this closes the one configuration that ran them anyway. Applied to the
    marker rather than to the one test that flaked, so all 7 are covered. Get the real
    numbers with `uv run pytest -m perf --no-cov`.
    """
    covering = bool(getattr(config.option, "cov_source", None)) and not getattr(
        config.option, "no_cov", False)
    skip_perf = pytest.mark.skip(reason="perf timings are meaningless under coverage; "
                                        "run `pytest -m perf --no-cov`")
    for item in items:
        if item.path.name.startswith("test_bench_"):
            item.add_marker(pytest.mark.slow)
        if covering and item.get_closest_marker("perf"):
            item.add_marker(skip_perf)


@pytest.fixture
def tmp_repo(tmp_path, monkeypatch):
    """Redirects STORE_DIR to a temp path and returns a fake repo path."""
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    return str(tmp_path / "repo")


# Artefacts a test must never create in the developer's real ~/.contexer. Redirecting
# store.STORE_DIR is not sufficient on its own: contexer/ui/daemon.py resolves its paths
# from Path.home() at import time, so a test that patches the port (or only STORE_DIR)
# but not HOME spawns a REAL console daemon against the real store. That happened twice
# while this feature was being built, once leaving a daemon listening for 15 minutes.
# The check is deliberately narrow — it names only files a test has no business creating,
# so the live SessionStart/PostToolUse hooks writing .pending_capture or a team cache
# alongside the suite can never fail the run.
_FORBIDDEN = ("ui.json", "ui.log")
_TEMP_MARKERS = ("private_tmp", "var_folders", "pytest", "tmp_")


def _leaked(real_store_dir: Path) -> list[str]:
    if not real_store_dir.is_dir():
        return []
    found = []
    for entry in real_store_dir.iterdir():
        name = entry.name
        if name in _FORBIDDEN or name.endswith(".deleted.json"):
            found.append(name)
        elif any(marker in name for marker in _TEMP_MARKERS):
            found.append(name)  # a store keyed to a tmp_path escaped into the real dir
    return sorted(found)


def _real_config_bytes(real_store_dir: Path) -> bytes | None:
    path = real_store_dir / "config.toml"
    return path.read_bytes() if path.is_file() else None


@pytest.fixture(scope="session", autouse=True)
def console_paths_never_resolve_the_real_home(tmp_path_factory):
    """Baseline the three import-time `Path.home()` globals somewhere disposable, for the
    WHOLE session — so undoing a per-test monkeypatch restores a sandbox, not the real one.

    A per-test `monkeypatch.setattr(daemon, "LOG_PATH", tmp_path / ...)` protects only the
    window in which that test is running. The console is a threaded HTTP server whose handler
    threads are daemon threads that shutdown deliberately does NOT join (`daemon_threads` +
    `block_on_close = False`, so a stalled client cannot decide when SIGTERM finishes), and
    EVERY request logs through `server._log` -> `daemon.LOG_PATH`. A handler still in flight
    when the test ends therefore reaches its `send_response` -> `log_request` AFTER monkeypatch
    has restored `LOG_PATH` to `~/.contexer/ui.log`, and writes the real developer's store dir
    from a thread nobody is waiting on — minutes of test time later, under whatever unrelated
    test happens to be running then.

    That is the CI flake this fixture closes: `test_ui_auth.py::
    test_sigterm_clears_the_statefile_with_a_half_sent_request_open` deliberately abandons a
    handler mid-request (that is the property it proves), and its `PUT /api/config 400` access
    line landed in the runner's real `~/.contexer/ui.log`, failing `no_real_store_writes`
    against a random later test. Convicted by instrumenting `_log` to record
    `PYTEST_CURRENT_TEST` whenever the path resolved under the real home.

    Fixing that one test would not close the class: any of the ~300 console requests in
    test_ui_api.py / test_ui_auth.py can be the one still in flight. One baseline is the
    chokepoint — after it, no in-process code path can name the real ui.json / ui.log /
    config.toml no matter which thread runs when.

    `no_real_store_writes` keeps its teeth for what this cannot reach: a SUBPROCESS resolves
    `Path.home()` from its own HOME, so a child spawned with an unpatched env still leaks and
    still fails the run."""
    from contexer import config
    from contexer.ui import daemon

    sandbox = tmp_path_factory.mktemp("home") / ".contexer"
    sandbox.mkdir()
    saved = (daemon.STATE_PATH, daemon.LOG_PATH, config.CONFIG_PATH)
    daemon.STATE_PATH = sandbox / "ui.json"
    daemon.LOG_PATH = sandbox / "ui.log"
    config.CONFIG_PATH = sandbox / "config.toml"
    yield
    daemon.STATE_PATH, daemon.LOG_PATH, config.CONFIG_PATH = saved


@pytest.fixture(scope="session", autouse=True)
def no_real_store_writes():
    """Fail the run if the suite leaked console artefacts into — or rewrote — the real store.

    config.toml is checked by content, not existence: `PUT /api/config` rewrites it in place
    (and drops the developer's comments), so a test that reaches the real path would leave the
    file present but different, which a file-listing check cannot see. It holds the teams
    bearer token, so a silent rewrite is the most expensive mistake available here."""
    real = Path.home() / ".contexer"
    before = set(_leaked(real))
    config_before = _real_config_bytes(real)
    yield
    new = [n for n in _leaked(real) if n not in before]
    assert not new, (
        f"tests leaked into the real store dir {real}: {new}. Patch HOME (not just "
        "store.STORE_DIR or the port) before anything that can spawn a daemon or "
        "resolve paths from Path.home()."
    )
    assert _real_config_bytes(real) == config_before, (
        f"tests rewrote the real {real / 'config.toml'}. Patch config.CONFIG_PATH before "
        "anything that can call config.write_settings()."
    )


class FakeTeamsServer:
    """In-memory stand-in for the Teams MCP server (E2E integration tests).

    Implements just enough of push_decision + get_context — idempotency on decisionId,
    scope, `updatedSince` filtering, and a monotonic cursor — to drive the full OSS sync
    path (share/pull/poll) with only the network hop faked.

    `title` is echoed through the same way `content`/`rationale` are: stored on push
    (absent -> None, mirroring the real server's nullable column) and returned by
    get_context. Title is NEVER required — `add_team_decision` (a row injected directly,
    simulating one added before Decision Titles v2 or by an older client) defaults it to
    None, and `get_context`'s projection tolerates a row with no "title" key at all."""

    ORIGIN = "git@github.com:acme/widgets.git"
    REPO_KEY = "github.com/acme/widgets"

    def __init__(self):
        self.rows: dict[str, dict] = {}
        self._seq = 0

    def _tick(self) -> str:
        self._seq += 1
        return f"{self._seq:06d}"  # zero-padded so string compare orders correctly

    def _store(self, args: dict) -> str:
        """Idempotent upsert of one decision; returns the server id."""
        did = args.get("decisionId") or f"srv-{self._tick()}"
        prev = self.rows.get(did, {})
        self.rows[did] = {
            "id": did, "type": args["type"], "title": args.get("title"),
            "content": args["content"],
            "rationale": args.get("rationale"), "repo": args.get("repo"),
            "agent": args.get("agent"),
            "scope": prev.get("scope", "personal"),  # push lands in personal; approval promotes
            "updated_at": self._tick(),
        }
        return did

    def push_decision(self, args: dict) -> str:
        return f"Saved decision {self._store(args)} to your personal context."

    def push_decisions(self, args: dict) -> dict:
        """Batch push: one server transaction, structuredContent {results:[...], skipped:[]}.
        The fake has no quota, so nothing is ever skipped."""
        return {"results": [{"decisionId": item.get("decisionId"), "id": self._store(item)}
                            for item in args.get("decisions", [])],
                "skipped": []}

    def approve_as_team(self, decision_id: str) -> None:
        """Simulate a lead approving a shared decision (personal -> team-approved)."""
        self.rows[decision_id]["scope"] = "team"
        self.rows[decision_id]["updated_at"] = self._tick()

    def add_team_decision(self, content: str, type: str = "architecture",
                          title: str | None = None) -> str:
        """Inject a row directly as already team-approved (bypasses push_decision). `title`
        defaults to None — an older client / pre-Titles-v2 row — so callers proving the
        untitled path don't have to pass anything."""
        did = f"team-{self._tick()}"
        self.rows[did] = {"id": did, "type": type, "title": title, "content": content,
                          "rationale": None, "repo": self.REPO_KEY, "agent": None,
                          "scope": "team", "updated_at": self._tick()}
        return did

    def get_context(self, args: dict) -> dict:
        repo, since = args.get("repo"), args.get("updatedSince")
        matched = [r for r in self.rows.values() if repo is None or r["repo"] == repo]
        rows = matched if since is None else [r for r in matched if r["updated_at"] > since]
        # .get(k) (not r[k]) so a row dict with no "title" key at all — e.g. hand-built by a
        # test predating this field — still projects cleanly as None; title stays optional.
        result = [{k: r.get(k) for k in ("id", "type", "title", "content", "rationale", "repo", "agent", "scope")}
                  for r in rows]
        cursor = max((r["updated_at"] for r in matched), default=None)
        return {"result": result, "deleted": [], "cursor": cursor}


def _fake_transport(server: FakeTeamsServer):
    # Patched over the async `_acall_tool` seam. A sync return is fine: `_ainvoke` only awaits
    # an actual awaitable, so this in-memory fake needs no coroutine wrapper (both the sync
    # asyncio.run shims and the awaited async share path funnel through here).
    def _call(endpoint, token, name, args, timeout):
        if name == "push_decision":
            text = server.push_decision(args)
            return types.SimpleNamespace(
                content=[types.SimpleNamespace(type="text", text=text)],
                structuredContent=None, isError=False)
        if name == "push_decisions":
            return types.SimpleNamespace(content=[], structuredContent=server.push_decisions(args), isError=False)
        if name == "get_context":
            return types.SimpleNamespace(content=[], structuredContent=server.get_context(args), isError=False)
        raise AssertionError(f"unexpected tool {name}")
    return _call


@pytest.fixture
def team_stack(tmp_path, monkeypatch):
    """Hermetic Teams backend: isolates STORE_DIR, pins a git origin, and routes
    RemoteStore's transport to an in-memory FakeTeamsServer. Returns the server."""
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    monkeypatch.setattr(store, "_git", lambda repo, *a: FakeTeamsServer.ORIGIN)
    server = FakeTeamsServer()
    monkeypatch.setattr(remote, "_acall_tool", _fake_transport(server))
    remote.reset_degradation_warnings()
    return server


@pytest.fixture
def populated_repo(tmp_repo):
    """A repo with two decisions pre-loaded."""
    store.update_decision(tmp_repo, "decided to use JWT instead of sessions — stateless, easier to scale", "sess-1")
    store.update_decision(tmp_repo, "constraint: never store plaintext passwords, always use bcrypt", "sess-1")
    return tmp_repo


@pytest.fixture(autouse=True)
def _no_real_ssh_config(monkeypatch):
    """Repo-key ssh-alias resolution is identity in tests: never consult the dev
    machine's real ~/.ssh/config (an alias there would skew every key-deriving test).
    Alias-specific tests override this stub; _ssh_hostname unit tests bypass it."""
    from contexer import repo_key
    monkeypatch.setattr(repo_key, "_resolve_ssh_host", lambda h: h)
