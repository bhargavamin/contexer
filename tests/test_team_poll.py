"""Tests for C7 delta-poll injection: team_context.poll + claude.team_poll."""
import json
import time

import pytest

import contexer.remote as remote
from contexer import config, store, team_context
from contexer.remote import RemoteContext, RemoteDecision, RemoteUnavailableError

TEAM = config.Profile(mode="team", endpoint="https://t/mcp", token="tok")


def _rd(id, content, scope="team", type="architecture"):
    return RemoteDecision(id=id, type=type, content=content, rationale=None,
                          repo="github.com/a/b", agent=None, scope=scope)


class _FakeRS:
    def __init__(self, ctx=None, exc=None):
        self._ctx, self._exc = ctx, exc
        self.calls = []

    def get_context(self, repo=None, updated_since=None):
        self.calls.append((repo, updated_since))
        if self._exc is not None:
            raise self._exc
        return self._ctx


@pytest.fixture
def team_env(tmp_repo, monkeypatch):
    monkeypatch.setattr(store, "_git", lambda repo, *a: "git@github.com:a/b.git")
    remote.reset_degradation_warnings()
    return tmp_repo


def _fake_rs(monkeypatch, *, ctx=None, exc=None):
    fake = _FakeRS(ctx=ctx, exc=exc)
    monkeypatch.setattr(team_context.RemoteStore, "from_profile", staticmethod(lambda p: fake))
    return fake


# ── poll ─────────────────────────────────────────────────────────────────────────

def test_poll_returns_new_team_rows_only(team_env, monkeypatch):
    _fake_rs(monkeypatch, ctx=RemoteContext(
        [_rd("t1", "team rule"), _rd("p1", "personal mirror", "personal")], [], "c1"))
    new = team_context.poll(team_env, profile=TEAM)
    assert [d["id"] for d in new] == ["t1"]  # personal row excluded


def test_poll_throttled_skips_network(team_env, monkeypatch):
    team_context._save_cache(team_env, {"repo_key": "github.com/a/b", "cursor": "c0",
                                        "decisions": [], "last_poll_at": time.time()})
    fake = _fake_rs(monkeypatch, ctx=RemoteContext([_rd("t1", "x")], [], "c1"))
    assert team_context.poll(team_env, profile=TEAM) == []
    assert fake.calls == []  # throttled — no round-trip


def test_poll_after_window_polls_again(team_env, monkeypatch):
    team_context._save_cache(team_env, {"repo_key": "github.com/a/b", "cursor": "c0",
                                        "decisions": [], "last_poll_at": time.time() - 100})
    _fake_rs(monkeypatch, ctx=RemoteContext([_rd("t2", "fresh rule")], [], "c2"))
    assert [d["content"] for d in team_context.poll(team_env, profile=TEAM)] == ["fresh rule"]


def test_poll_stamps_last_poll_at(team_env, monkeypatch):
    _fake_rs(monkeypatch, ctx=RemoteContext([_rd("t1", "x")], [], "c1"))
    team_context.poll(team_env, profile=TEAM)
    assert team_context._load_cache(team_env).get("last_poll_at", 0) > 0


def test_poll_local_mode_no_file(tmp_repo):
    assert team_context.poll(tmp_repo, profile=config.Profile()) == []
    assert not team_context._cache_path(tmp_repo).exists()  # no cache for a local repo


def test_poll_degraded_returns_empty(team_env, monkeypatch, capsys):
    _fake_rs(monkeypatch, exc=RemoteUnavailableError("down"))
    assert team_context.poll(team_env, profile=TEAM) == []
    assert "unreachable" in capsys.readouterr().err.lower()


def test_pull_contract_intact_after_refactor(team_env, monkeypatch):
    _fake_rs(monkeypatch, ctx=RemoteContext([_rd("t1", "x"), _rd("t2", "y")], ["gone"], "c1"))
    assert team_context.pull(team_env, profile=TEAM) == (2, 0)  # still (int, int)


# ── adapter team_poll ─────────────────────────────────────────────────────────────

def test_team_poll_injects_new_decisions(monkeypatch):
    from contexer.adapters import claude
    monkeypatch.setattr(store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(team_context, "poll_nonblocking", lambda repo: [{"content": "Use Postgres", "type": "architecture"}])
    data = json.loads(claude.team_poll("/repo", ""))
    assert "Use Postgres" in data["hookSpecificOutput"]["additionalContext"]
    assert data["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


def test_team_poll_empty_when_nothing_new(monkeypatch):
    from contexer.adapters import claude
    monkeypatch.setattr(store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(team_context, "poll_nonblocking", lambda repo: [])
    assert claude.team_poll("/repo", "") == "{}"


def test_team_poll_no_repo(monkeypatch):
    from contexer.adapters import claude
    monkeypatch.setattr(store, "_resolve_repo", lambda p: "")
    assert claude.team_poll("", "") == "{}"


def test_team_poll_swallows_errors(monkeypatch):
    from contexer.adapters import claude
    monkeypatch.setattr(store, "_resolve_repo", lambda p: "/repo")

    def boom(repo):
        raise RuntimeError("boom")

    monkeypatch.setattr(team_context, "poll_nonblocking", boom)
    assert claude.team_poll("/repo", "") == "{}"


# ── poll_nonblocking (background refresh; zero network on the prompt path) ────────

def _no_spawn(monkeypatch):
    spawned = []
    monkeypatch.setattr(team_context, "_spawn_refresh", lambda repo: spawned.append(repo))
    return spawned


def test_nonblocking_returns_and_consumes_pending(team_env, monkeypatch):
    spawned = _no_spawn(monkeypatch)
    team_context.store.STORE_DIR.mkdir(exist_ok=True)
    team_context._pending_path(team_env).write_text(
        json.dumps([{"id": "t1", "content": "team rule", "type": "constraint"}]))
    new = team_context.poll_nonblocking(team_env, profile=TEAM)
    assert [d["id"] for d in new] == ["t1"]
    assert not team_context._pending_path(team_env).exists()  # consumed
    assert team_context.poll_nonblocking(team_env, profile=TEAM) == []  # gone next prompt
    assert spawned  # a refresh was scheduled for the next window


def test_nonblocking_spawns_when_due_and_stamps_first(team_env, monkeypatch):
    spawned = _no_spawn(monkeypatch)
    team_context._save_cache(team_env, {"repo_key": "github.com/a/b", "cursor": None,
                                        "decisions": [], "last_poll_at": time.time() - 100})
    assert team_context.poll_nonblocking(team_env, profile=TEAM) == []
    assert spawned == [team_env]
    # stamped before spawn: a second prompt inside the window must not spawn again
    assert team_context.poll_nonblocking(team_env, profile=TEAM) == []
    assert spawned == [team_env]


def test_nonblocking_throttled_no_spawn(team_env, monkeypatch):
    spawned = _no_spawn(monkeypatch)
    team_context._save_cache(team_env, {"repo_key": "github.com/a/b", "cursor": None,
                                        "decisions": [], "last_poll_at": time.time()})
    assert team_context.poll_nonblocking(team_env, profile=TEAM) == []
    assert spawned == []


def test_nonblocking_local_mode_no_work(tmp_repo, monkeypatch):
    spawned = _no_spawn(monkeypatch)
    assert team_context.poll_nonblocking(tmp_repo, profile=config.Profile()) == []
    assert spawned == []
    assert not team_context._cache_path(tmp_repo).exists()


def test_nonblocking_never_does_network(team_env, monkeypatch):
    _no_spawn(monkeypatch)
    fake = _fake_rs(monkeypatch, ctx=RemoteContext([_rd("t9", "x")], [], "c9"))
    team_context.poll_nonblocking(team_env, profile=TEAM)
    assert fake.calls == []  # the prompt path itself must never touch the cloud


def test_nonblocking_corrupt_pending_is_empty(team_env, monkeypatch):
    _no_spawn(monkeypatch)
    team_context.store.STORE_DIR.mkdir(exist_ok=True)
    team_context._pending_path(team_env).write_text("{not json")
    assert team_context.poll_nonblocking(team_env, profile=TEAM) == []
    assert not team_context._pending_path(team_env).exists()  # corrupt file still consumed


def test_refresh_worker_parks_new_rows(team_env, monkeypatch):
    monkeypatch.setattr(config, "load_profile", lambda path=None: TEAM)
    _fake_rs(monkeypatch, ctx=RemoteContext(
        [_rd("t1", "team rule"), _rd("p1", "mine", "personal")], [], "c1"))
    team_context._refresh_worker(team_env)
    pending = json.loads(team_context._pending_path(team_env).read_text())
    assert [r["id"] for r in pending] == ["t1"]  # team rows only, parked for the next prompt
    assert team_context._load_cache(team_env)["cursor"] == "c1"  # cache advanced too


def test_refresh_worker_merges_unconsumed_pending(team_env, monkeypatch):
    monkeypatch.setattr(config, "load_profile", lambda path=None: TEAM)
    team_context.store.STORE_DIR.mkdir(exist_ok=True)
    team_context._pending_path(team_env).write_text(
        json.dumps([{"id": "t0", "content": "earlier, not yet injected"}]))
    _fake_rs(monkeypatch, ctx=RemoteContext([_rd("t1", "newer")], [], "c2"))
    team_context._refresh_worker(team_env)
    pending = {r["id"] for r in json.loads(team_context._pending_path(team_env).read_text())}
    assert pending == {"t0", "t1"}  # nothing lost between prompts


def test_refresh_worker_degraded_leaves_no_pending(team_env, monkeypatch):
    monkeypatch.setattr(config, "load_profile", lambda path=None: TEAM)
    _fake_rs(monkeypatch, exc=RemoteUnavailableError("down"))
    team_context._refresh_worker(team_env)
    assert not team_context._pending_path(team_env).exists()


# ── inclusive-cursor defense (live finding: server re-sends rows stamped == cursor) ──

def test_sync_skips_unchanged_resends(team_env, monkeypatch):
    """The dev server's updatedSince is inclusive: rows at == cursor re-send forever.
    An unchanged row must not count as new (it would re-inject every poll window)."""
    team_context._save_cache(team_env, {"repo_key": "github.com/a/b", "cursor": "c1",
                                        "decisions": [team_context._row_to_dict(_rd("t1", "same rule"))],
                                        "last_poll_at": 0})
    _fake_rs(monkeypatch, ctx=RemoteContext([_rd("t1", "same rule")], [], "c1"))
    assert team_context.poll(team_env, profile=TEAM) == []  # unchanged re-send -> nothing new


def test_sync_still_surfaces_changed_rows(team_env, monkeypatch):
    team_context._save_cache(team_env, {"repo_key": "github.com/a/b", "cursor": "c1",
                                        "decisions": [team_context._row_to_dict(_rd("t1", "old wording"))],
                                        "last_poll_at": 0})
    _fake_rs(monkeypatch, ctx=RemoteContext([_rd("t1", "new wording")], [], "c2"))
    assert [d["content"] for d in team_context.poll(team_env, profile=TEAM)] == ["new wording"]


# ── exponential backoff on consecutive sync failures ──────────────────────────────

def test_poll_throttle_widens_after_failures(team_env, monkeypatch):
    # 2 consecutive failures -> interval = 15 * 2**2 = 60s. 40s since last poll is still
    # inside that window, so it must stay throttled even though it's past the base 15s.
    team_context._save_cache(team_env, {
        "repo_key": "github.com/a/b", "cursor": "c0", "decisions": [],
        "last_poll_at": time.time() - 40,
        "last_sync": {"at": 1, "ok": False, "duration_ms": 1, "consecutive_failures": 2}})
    fake = _fake_rs(monkeypatch, ctx=RemoteContext([_rd("t1", "x")], [], "c1"))
    assert team_context.poll(team_env, profile=TEAM) == []
    assert fake.calls == []  # still backed off — no round-trip


def test_poll_polls_again_once_backoff_window_elapses(team_env, monkeypatch):
    team_context._save_cache(team_env, {
        "repo_key": "github.com/a/b", "cursor": "c0", "decisions": [],
        "last_poll_at": time.time() - 61,
        "last_sync": {"at": 1, "ok": False, "duration_ms": 1, "consecutive_failures": 2}})
    _fake_rs(monkeypatch, ctx=RemoteContext([_rd("t1", "recovered")], [], "c1"))
    assert [d["content"] for d in team_context.poll(team_env, profile=TEAM)] == ["recovered"]


def test_poll_backoff_caps_at_max_interval(team_env, monkeypatch):
    # Enough failures that the raw exponential would be far past 900s - must clamp there,
    # so 901s since last poll is enough to unblock even after a long outage.
    team_context._save_cache(team_env, {
        "repo_key": "github.com/a/b", "cursor": "c0", "decisions": [],
        "last_poll_at": time.time() - 901,
        "last_sync": {"at": 1, "ok": False, "duration_ms": 1, "consecutive_failures": 20}})
    _fake_rs(monkeypatch, ctx=RemoteContext([_rd("t1", "x")], [], "c1"))
    assert [d["content"] for d in team_context.poll(team_env, profile=TEAM)] == ["x"]


def test_poll_healthy_cloud_keeps_15s_cadence(team_env, monkeypatch):
    # consecutive_failures == 0 (healthy) - base interval unchanged, matching pre-backoff
    # behaviour exactly.
    team_context._save_cache(team_env, {
        "repo_key": "github.com/a/b", "cursor": "c0", "decisions": [],
        "last_poll_at": time.time() - 16,
        "last_sync": {"at": 1, "ok": True, "duration_ms": 1, "consecutive_failures": 0}})
    _fake_rs(monkeypatch, ctx=RemoteContext([_rd("t1", "x")], [], "c1"))
    assert [d["content"] for d in team_context.poll(team_env, profile=TEAM)] == ["x"]


def test_nonblocking_spawn_widens_after_failures(team_env, monkeypatch):
    spawned = _no_spawn(monkeypatch)
    team_context._save_cache(team_env, {
        "repo_key": "github.com/a/b", "cursor": None, "decisions": [],
        "last_poll_at": time.time() - 40,
        "last_sync": {"at": 1, "ok": False, "duration_ms": 1, "consecutive_failures": 2}})
    assert team_context.poll_nonblocking(team_env, profile=TEAM) == []
    assert spawned == []  # 40s < 60s backoff window - no spawn yet


def test_nonblocking_spawn_resumes_after_backoff_window(team_env, monkeypatch):
    spawned = _no_spawn(monkeypatch)
    team_context._save_cache(team_env, {
        "repo_key": "github.com/a/b", "cursor": None, "decisions": [],
        "last_poll_at": time.time() - 61,
        "last_sync": {"at": 1, "ok": False, "duration_ms": 1, "consecutive_failures": 2}})
    assert team_context.poll_nonblocking(team_env, profile=TEAM) == []
    assert spawned == [team_env]


def test_first_success_snaps_backoff_back_to_base(team_env, monkeypatch):
    # A successful sync resets consecutive_failures to 0, so the very next poll uses the
    # base 15s cadence again rather than staying widened from the prior outage.
    # consecutive_failures=3 -> current interval is 15*2**3=120s, so last_poll_at must be
    # further back than that for this poll to actually reach _sync (and observe the reset).
    team_context._save_cache(team_env, {
        "repo_key": "github.com/a/b", "cursor": "c0", "decisions": [],
        "last_poll_at": time.time() - 121,
        "last_sync": {"at": 1, "ok": False, "duration_ms": 1, "consecutive_failures": 3}})
    _fake_rs(monkeypatch, ctx=RemoteContext([_rd("t1", "back up")], [], "c1"))
    team_context.poll(team_env, profile=TEAM)
    cache = team_context._load_cache(team_env)
    assert cache["last_sync"]["consecutive_failures"] == 0
    assert team_context._poll_interval(cache) == team_context._POLL_MIN_INTERVAL
