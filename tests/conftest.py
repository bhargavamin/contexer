"""Shared pytest fixtures for the contexer test suite."""
import os
import subprocess
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
    budget was the wrong fix - it would have to reach past the live-scan's ~7.7ms to be
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


@pytest.fixture(autouse=True)
def no_detached_proposal_uploader(monkeypatch):
    """Unit tests opt into the real daemon seam explicitly; none may outlive temp path patches."""
    from contexer import share_policy
    monkeypatch.setattr(share_policy, "start_detached_drainer", lambda *_args, **_kwargs: True)


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

# Families a test has no business creating in the real dir, whatever repo they are keyed to.
# The evidence spool, its maintenance stamp and the reconcile log/lock arrived with the
# evidence-capture work; `.evidence_*` is the retired single-sidecar spelling (plus its
# `.evidence_lock_*`), kept here so a stale checkout that still writes one is caught too.
# The `_` in `.reconcile_` is load-bearing: `.reconcile-outbox.json` is the share retry
# queue, which a live session may legitimately write alongside the suite.
_FORBIDDEN_PREFIXES = (".evidence_", ".reconcile_", ".spool_maintained_")
_SPOOL_DIR = "evidence"


def _spool_dirs(root: Path) -> list[str]:
    """Every DIRECTORY under `~/.contexer/evidence`, relative to the store dir.

    Directories, not files: once the developer's real dir holds a spool at all, a top-level
    `evidence` name is in the baseline forever and the check would be blind to everything
    added inside it. A per-repo spool dir (or a `held/<candidate>` batch) appearing during
    the run is a test reaching the real store; a live PostToolUse hook appending an event
    into a spool that already exists only adds FILES, so it can never fail the run - the
    same "never flag what a live session legitimately writes" line `_FORBIDDEN` draws."""
    try:
        return [str(p.relative_to(root.parent)) for p in root.rglob("*") if p.is_dir()]
    except OSError:
        return []


def _leaked(real_store_dir: Path) -> list[str]:
    if not real_store_dir.is_dir():
        return []
    found = []
    for entry in real_store_dir.iterdir():
        name = entry.name
        if name == _SPOOL_DIR and entry.is_dir():
            found.append(name)
            found.extend(_spool_dirs(entry))
        elif (name in _FORBIDDEN or name.endswith(".deleted.json")
                or name.startswith(_FORBIDDEN_PREFIXES)):
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
        f"tests leaked into the real store dir {real}: {new}. Patch store.STORE_DIR (the "
        "`tmp_repo` fixture) before anything that spools evidence, reconciles, or takes a "
        "store-dir lock, and patch HOME (not just store.STORE_DIR or the port) before "
        "anything that can spawn a daemon, a subprocess, or resolve paths from Path.home()."
    )
    assert _real_config_bytes(real) == config_before, (
        f"tests rewrote the real {real / 'config.toml'}. Patch config.CONFIG_PATH before "
        "anything that can call config.write_settings()."
    )


@pytest.fixture(scope="session", autouse=True)
def no_real_repo_settings_writes():
    """Fail the run if a test rewrote this checkout's own `.claude/settings.json`.

    `claude.install()`/`uninstall()` run `clean_legacy_repo_settings` against
    `store.git_root(os.getcwd())` - the PROCESS cwd's git root, not the injected HOME - so
    injecting HOME alone does NOT contain an installer-driving test. Run from a checkout whose
    `.claude/settings.json` carries legacy Contexer hook markers, such a test silently rewrites
    the developer's real file.

    Individual fixtures contain this structurally (`monkeypatch.chdir` into a temp dir, whose
    git root has no `.claude/settings.json` of ours). This is the backstop for the NEXT
    installer-driving test that forgets: it costs nothing, needs no ordering against other
    fixtures, and names the remedy. Same shape and reasoning as `no_real_store_writes` above,
    which watches `~/.contexer` and cannot see this file (issue #185).

    Keyed off `__file__`, not `os.getcwd()` - this is the one settings file that is definitely
    at risk, and a test that chdirs cannot move the target out from under the assertion."""
    real = Path(__file__).resolve().parent.parent / ".claude" / "settings.json"
    before = real.read_bytes() if real.is_file() else None
    yield
    after = real.read_bytes() if real.is_file() else None
    assert after == before, (
        f"tests rewrote the real {real}. An installer-driving test must pin the process cwd "
        "(monkeypatch.chdir into a temp dir) as well as HOME - clean_legacy_repo_settings "
        "resolves its target from os.getcwd(), not from the injected home."
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
    monkeypatch.setattr(store, "run_git", lambda repo, *a: FakeTeamsServer.ORIGIN)
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


# ── the commit-time guard's shared fixtures ─────────────────────────────────
#
# Here rather than in tests/test_guard_engine.py because two files need them, and that
# file is the guard's frozen behavioural specification: reaching into it from
# test_guard_policy_seam.py meant importing a fixture under another name and re-binding
# it purely so ruff would stop reading every `repo` parameter as a redefinition. A
# module that defines its own `repo`/`git_repo` still shadows these, so nothing outside
# the guard suites changes.


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """A real throwaway git repo, isolated from the developer's global/system git
    config so commits succeed deterministically regardless of the host machine's
    setup (mirrors the git_repo fixture pattern in test_store.py's TestInsightCache)."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "guard@test.local"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Guard Test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=repo, check=True)
    return repo


@pytest.fixture
def repo(git_repo, monkeypatch):
    """`git_repo` with STORE_DIR redirected to a sibling temp dir - for tests
    that read/write the store or the guard's sidecar files, not just git plumbing.
    Same pattern as test_store.py's tmp_repo / session_repo_preferred_over_pointer."""
    monkeypatch.setattr(store, "STORE_DIR", git_repo.parent / ".contexer")
    return git_repo


def _write(repo, relpath, content):
    path = repo / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content)
    return path


def _git(repo, *args, check=True):
    subprocess.run(["git", "-C", str(repo), *args], check=check,
                   capture_output=True)


def _seed_entry(repo, content, *, subtype="architecture", created_by="human",
                status="approved", source_files=None, global_store=False,
                title="", session_id="test-session", approved_by=None):
    """Build a decision entry via the real entry constructor (so revisions/
    current_revision_id/status/source all come out shaped exactly like production
    data) and append it directly to the (repo or global) store - bypassing the
    novelty filter, which is irrelevant to the guard engine's own tests."""
    entry = store._new_decision_entry(content, session_id, subtype,
                                      created_by=created_by, status=status, title=title)
    if source_files is not None:
        entry["source_files"] = source_files
    if approved_by is not None:
        entry["approved_by"] = approved_by
    if global_store:
        data = store.load_global()
        data["entries"].append(entry)
        store.save_global(data)
    else:
        data = store.load(str(repo))
        data["entries"].append(entry)
        store.save(str(repo), data)
    return entry


@pytest.fixture(autouse=True)
def _no_real_ssh_config(monkeypatch):
    """Repo-key ssh-alias resolution is identity in tests: never consult the dev
    machine's real ~/.ssh/config (an alias there would skew every key-deriving test).
    Alias-specific tests override this stub; _ssh_hostname unit tests bypass it."""
    from contexer import repo_key
    monkeypatch.setattr(repo_key, "_resolve_ssh_host", lambda h: h)
