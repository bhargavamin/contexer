"""Tests for C7 delta-poll injection: team_context.poll + claude.team_poll."""
import json
import time

import pytest

import contexer.remote as remote
from contexer import config, store, team_context
from contexer.remote import RemoteContext, RemoteDecision, RemoteUnavailableError

TEAM = config.Profile(mode="team", endpoint="https://t/mcp", token="tok")


def _rd(id, content, scope="team", type="architecture", title=None):
    return RemoteDecision(id=id, type=type, title=title, content=content, rationale=None,
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
    monkeypatch.setattr(store, "run_git", lambda repo, *a: "git@github.com:a/b.git")
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
    monkeypatch.setattr(store, "resolve_repo", lambda p: "/repo")
    # Non-architecture type: architecture rows are deferred to a count-only pointer
    # (see test_team_context.py::test_team_poll_defers_architecture_shows_rest).
    monkeypatch.setattr(team_context, "poll_nonblocking",
                        lambda repo, consumer="claude": [{"content": "Use Postgres", "type": "constraint"}])
    data = json.loads(claude.team_poll("/repo", ""))
    assert "Use Postgres" in data["hookSpecificOutput"]["additionalContext"]
    assert data["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


def test_team_poll_empty_when_nothing_new(monkeypatch):
    from contexer.adapters import claude
    monkeypatch.setattr(store, "resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(team_context, "poll_nonblocking", lambda repo, consumer="claude": [])
    assert claude.team_poll("/repo", "") == "{}"


def test_team_poll_no_repo(monkeypatch):
    from contexer.adapters import claude
    monkeypatch.setattr(store, "resolve_repo", lambda p: "")
    assert claude.team_poll("", "") == "{}"


def test_team_poll_swallows_errors(monkeypatch):
    from contexer.adapters import claude
    monkeypatch.setattr(store, "resolve_repo", lambda p: "/repo")

    def boom(repo, consumer="claude"):
        raise RuntimeError("boom")

    monkeypatch.setattr(team_context, "poll_nonblocking", boom)
    assert claude.team_poll("/repo", "") == "{}"


# ── poll_nonblocking (background refresh; per-consumer sync-log delivery) ─────────

def _no_spawn(monkeypatch):
    spawned = []
    monkeypatch.setattr(team_context, "_spawn_refresh", lambda repo: spawned.append(repo))
    return spawned


def _seed_log(repo, *, seq, sync_log, decisions, last_poll_at=None):
    """Write a cache with a per-repo sync log (as _sync would leave it). Throttled by default
    (last_poll_at=now) so a test can assert delivery without a background spawn interfering."""
    team_context._save_cache(repo, {
        "repo_key": "github.com/a/b", "cursor": "c1",
        "last_poll_at": time.time() if last_poll_at is None else last_poll_at,
        "seq": seq, "sync_log": sync_log, "decisions": decisions})


def test_two_consumers_each_get_new_rows_once(team_env, monkeypatch):
    _no_spawn(monkeypatch)
    _seed_log(team_env, seq=1, sync_log=[{"seq": 1, "ids": ["t1"]}],
              decisions=[{"id": "t1", "content": "team rule", "type": "constraint"}])
    # Both consumers were caught up at seq 0 before the seq-1 batch was logged mid-session.
    team_context._write_seen(team_env, "claude", 0)
    team_context._write_seen(team_env, "codex", 0)
    a = team_context.poll_nonblocking(team_env, "claude", profile=TEAM)
    b = team_context.poll_nonblocking(team_env, "codex", profile=TEAM)
    assert [r["id"] for r in a] == ["t1"]
    assert [r["id"] for r in b] == ["t1"]  # codex NOT starved by claude's earlier poll
    # a second poll by each consumer sees nothing new (its own marker advanced)
    assert team_context.poll_nonblocking(team_env, "claude", profile=TEAM) == []
    assert team_context.poll_nonblocking(team_env, "codex", profile=TEAM) == []


def test_new_consumer_starts_caught_up(team_env, monkeypatch):
    _no_spawn(monkeypatch)
    # SessionStart already rendered these rows; a brand-new consumer (no marker yet) must NOT
    # re-inject the backlog — it catches up to the current log head instead.
    _seed_log(team_env, seq=2, sync_log=[{"seq": 1, "ids": ["t1"]}, {"seq": 2, "ids": ["t2"]}],
              decisions=[{"id": "t1", "content": "a"}, {"id": "t2", "content": "b"}])
    assert team_context.poll_nonblocking(team_env, "codex", profile=TEAM) == []  # caught up
    assert json.loads(team_context._seen_path(team_env, "codex").read_text())["seq"] == 2


def test_marker_files_are_per_consumer(team_env, monkeypatch):
    _no_spawn(monkeypatch)
    _seed_log(team_env, seq=2, sync_log=[{"seq": 2, "ids": ["t1"]}],
              decisions=[{"id": "t1", "content": "x"}])
    team_context._write_seen(team_env, "claude", 1)  # claude caught up at seq 1
    team_context.poll_nonblocking(team_env, "claude", profile=TEAM)
    assert json.loads(team_context._seen_path(team_env, "claude").read_text())["seq"] == 2
    assert not team_context._seen_path(team_env, "codex").exists()  # untouched by claude's poll


def test_default_consumer_is_claude(team_env, monkeypatch):
    _no_spawn(monkeypatch)
    _seed_log(team_env, seq=1, sync_log=[{"seq": 1, "ids": ["t1"]}],
              decisions=[{"id": "t1", "content": "x"}])
    team_context._write_seen(team_env, "claude", 0)
    got = team_context.poll_nonblocking(team_env, profile=TEAM)  # no consumer arg
    assert [r["id"] for r in got] == ["t1"]  # used the claude marker (the default)


def test_sync_appends_batch_to_log(team_env, monkeypatch):
    _fake_rs(monkeypatch, ctx=RemoteContext([_rd("t1", "a"), _rd("t2", "b")], [], "c1"))
    team_context.poll(team_env, profile=TEAM)  # the blocking poll runs _sync
    cache = team_context._load_cache(team_env)
    assert cache["seq"] == 1
    assert cache["sync_log"] == [{"seq": 1, "ids": ["t1", "t2"]}]


def test_sync_log_is_capped_dropping_oldest(team_env, monkeypatch):
    seeded = [{"seq": i, "ids": [f"x{i}"]} for i in range(1, team_context._SYNC_LOG_CAP + 1)]
    _seed_log(team_env, seq=team_context._SYNC_LOG_CAP, sync_log=seeded, decisions=[],
              last_poll_at=0)
    _fake_rs(monkeypatch, ctx=RemoteContext([_rd("new", "z")], [], "c2"))
    team_context.poll(team_env, profile=TEAM)
    log = team_context._load_cache(team_env)["sync_log"]
    assert len(log) == team_context._SYNC_LOG_CAP
    assert log[0]["seq"] == 2  # seq 1 dropped off the front
    assert log[-1] == {"seq": team_context._SYNC_LOG_CAP + 1, "ids": ["new"]}


def test_consumer_behind_cap_gets_current_log_only(team_env, monkeypatch):
    _no_spawn(monkeypatch)
    # log covers seq 3..5 (1,2 dropped by the cap); a brand-new consumer (marker 0) still gets
    # every batch REMAINING in the log — the dropped ones stay reachable via get_context.
    _seed_log(team_env, seq=5,
              sync_log=[{"seq": 3, "ids": ["t3"]}, {"seq": 4, "ids": ["t4"]},
                        {"seq": 5, "ids": ["t5"]}],
              decisions=[{"id": "t3", "content": "3"}, {"id": "t4", "content": "4"},
                         {"id": "t5", "content": "5"}])
    team_context._write_seen(team_env, "codex", 2)  # further behind than the log's oldest (3)
    got = team_context.poll_nonblocking(team_env, "codex", profile=TEAM)
    assert [r["id"] for r in got] == ["t3", "t4", "t5"]  # gets every batch still in the log


def test_deleted_id_in_log_is_skipped(team_env, monkeypatch):
    _no_spawn(monkeypatch)
    # log references t_gone but it's no longer in decisions (deleted since it was logged).
    _seed_log(team_env, seq=1, sync_log=[{"seq": 1, "ids": ["t_gone", "t1"]}],
              decisions=[{"id": "t1", "content": "kept"}])
    team_context._write_seen(team_env, "claude", 0)
    got = team_context.poll_nonblocking(team_env, "claude", profile=TEAM)
    assert [r["id"] for r in got] == ["t1"]  # missing row skipped, not a KeyError


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


def test_legacy_pending_file_deleted_on_poll(team_env, monkeypatch):
    _no_spawn(monkeypatch)
    team_context.store.STORE_DIR.mkdir(exist_ok=True)
    legacy = (team_context.store.STORE_DIR
              / f".team_pending_{team_context.store.repo_slug(team_env)}.json")
    legacy.write_text(json.dumps([{"id": "old", "content": "stale"}]))
    _seed_log(team_env, seq=0, sync_log=[], decisions=[])
    assert team_context.poll_nonblocking(team_env, "claude", profile=TEAM) == []
    assert not legacy.exists()  # old parked file cleaned up, never injected


def test_corrupt_marker_degrades_to_empty(team_env, monkeypatch):
    _no_spawn(monkeypatch)
    _seed_log(team_env, seq=1, sync_log=[{"seq": 1, "ids": ["t1"]}],
              decisions=[{"id": "t1", "content": "x"}])
    team_context.store.STORE_DIR.mkdir(exist_ok=True)
    team_context._seen_path(team_env, "claude").write_text("{not json")
    # corrupt marker: no crash, no re-spam of the whole log — empty injection this once
    assert team_context.poll_nonblocking(team_env, "claude", profile=TEAM) == []
    # self-healed to caught-up (seq 1); a follow-up poll with no new batch is still empty
    assert team_context.poll_nonblocking(team_env, "claude", profile=TEAM) == []
    assert json.loads(team_context._seen_path(team_env, "claude").read_text())["seq"] == 1


def test_corrupt_sync_log_degrades_to_empty(team_env, monkeypatch):
    _no_spawn(monkeypatch)
    team_context._save_cache(team_env, {
        "repo_key": "github.com/a/b", "cursor": "c1", "last_poll_at": time.time(),
        "seq": 1, "sync_log": "not a list", "decisions": [{"id": "t1", "content": "x"}]})
    assert team_context.poll_nonblocking(team_env, "claude", profile=TEAM) == []  # never raises


def test_malformed_log_entries_are_skipped(team_env, monkeypatch):
    _no_spawn(monkeypatch)
    # A hand-mangled log: a non-dict entry and one with a non-int seq sit beside a good batch.
    _seed_log(team_env, seq=3,
              sync_log=["junk", {"seq": "x", "ids": ["bad"]}, {"seq": 3, "ids": ["t3"]}],
              decisions=[{"id": "t3", "content": "kept"}])
    team_context._write_seen(team_env, "claude", 0)
    got = team_context.poll_nonblocking(team_env, "claude", profile=TEAM)
    assert [r["id"] for r in got] == ["t3"]  # only the well-formed batch survives, no crash


def test_refresh_worker_appends_team_rows_to_log(team_env, monkeypatch):
    monkeypatch.setattr(config, "load_profile", lambda path=None: TEAM)
    _fake_rs(monkeypatch, ctx=RemoteContext(
        [_rd("t1", "team rule"), _rd("p1", "mine", "personal")], [], "c1"))
    team_context._refresh_worker(team_env)
    cache = team_context._load_cache(team_env)
    assert cache["sync_log"] == [{"seq": 1, "ids": ["t1"]}]  # team rows only
    assert cache["cursor"] == "c1"  # cache advanced too


def test_refresh_worker_degraded_leaves_no_log(team_env, monkeypatch):
    monkeypatch.setattr(config, "load_profile", lambda path=None: TEAM)
    _fake_rs(monkeypatch, exc=RemoteUnavailableError("down"))
    team_context._refresh_worker(team_env)
    assert team_context._load_cache(team_env).get("sync_log", []) == []


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
