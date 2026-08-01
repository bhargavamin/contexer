"""Shared pytest fixtures for the contexer test suite."""
import types

import pytest

from contexer import remote, store


def pytest_collection_modifyitems(items):
    """Classify each test into one selectable verification tier."""
    for item in items:
        filename = item.path.name
        if filename in {"test_benchmark.py", "test_benchmark_extended.py"}:
            item.add_marker(pytest.mark.accuracy)
        elif filename.startswith("test_bench_"):
            item.add_marker(pytest.mark.harness)
        elif filename.startswith(("test_e2e", "test_team_")):
            item.add_marker(pytest.mark.integration)
        else:
            item.add_marker(pytest.mark.fast)


@pytest.fixture
def tmp_repo(tmp_path, monkeypatch):
    """Redirects STORE_DIR to a temp path and returns a fake repo path."""
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    return str(tmp_path / "repo")


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
