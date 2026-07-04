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
    monkeypatch.setattr(team_context, "poll", lambda repo: [{"content": "Use Postgres", "type": "architecture"}])
    data = json.loads(claude.team_poll("/repo", ""))
    assert "Use Postgres" in data["hookSpecificOutput"]["additionalContext"]
    assert data["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"


def test_team_poll_empty_when_nothing_new(monkeypatch):
    from contexer.adapters import claude
    monkeypatch.setattr(store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(team_context, "poll", lambda repo: [])
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

    monkeypatch.setattr(team_context, "poll", boom)
    assert claude.team_poll("/repo", "") == "{}"
