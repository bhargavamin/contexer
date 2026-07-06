"""Tests for the C5 team-context cache + merge (contexer/team_context.py).

The team cache is separate from the local store: pulled TEAM decisions never enter
store.py entries[], they live in ~/.contexer/.team_{slug}.json and are merged at read
time. RemoteStore is faked here so no network is touched.
"""
import json

import pytest

import contexer.remote as remote
from contexer import config, store, team_context
from contexer.remote import RemoteAuthError, RemoteContext, RemoteDecision, RemoteUnavailableError

TEAM_PROFILE = config.Profile(mode="team", endpoint="https://t/mcp", token="tok")


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
    """tmp_repo isolates STORE_DIR; give the repo a git origin so a canonical key resolves."""
    monkeypatch.setattr(store, "_git", lambda repo, *a: "git@github.com:a/b.git")
    remote.reset_degradation_warnings()
    return tmp_repo


def _fake_rs(monkeypatch, *, ctx=None, exc=None):
    fake = _FakeRS(ctx=ctx, exc=exc)
    monkeypatch.setattr(team_context.RemoteStore, "from_profile", staticmethod(lambda p, **kw: fake))
    return fake


# ── pull ─────────────────────────────────────────────────────────────────────────

def test_pull_local_mode_is_noop(team_env, monkeypatch):
    monkeypatch.setattr(team_context.RemoteStore, "from_profile", staticmethod(lambda p: None))
    assert team_context.pull(team_env, profile=config.Profile()) == (0, 0)
    assert not team_context._cache_path(team_env).exists()


def test_pull_no_git_remote_is_noop(team_env, monkeypatch):
    monkeypatch.setattr(store, "_git", lambda repo, *a: None)  # no origin
    _fake_rs(monkeypatch, ctx=RemoteContext([_rd("t1", "x")], [], "c1"))
    assert team_context.pull(team_env, profile=TEAM_PROFILE) == (0, 0)


def test_pull_caches_team_rows_only(team_env, monkeypatch):
    ctx = RemoteContext(
        decisions=[_rd("t1", "team rule", "team"), _rd("p1", "personal mirror", "personal")],
        deleted=[], cursor="2026-01-01T00:00:00Z")
    fake = _fake_rs(monkeypatch, ctx=ctx)
    up, rm = team_context.pull(team_env, profile=TEAM_PROFILE)
    assert (up, rm) == (1, 0)
    cache = json.loads(team_context._cache_path(team_env).read_text())
    assert [d["id"] for d in cache["decisions"]] == ["t1"]  # personal row NOT cached
    assert cache["cursor"] == "2026-01-01T00:00:00Z"
    assert cache["repo_key"] == "github.com/a/b"
    assert fake.calls == [("github.com/a/b", None)]  # first pull: no cursor


def test_pull_incremental_upserts_and_deletes(team_env, monkeypatch):
    team_context._save_cache(team_env, {
        "repo_key": "github.com/a/b", "cursor": "c0",
        "decisions": [
            {"id": "t1", "type": "architecture", "content": "old", "rationale": None,
             "repo": None, "agent": None, "scope": "team"},
            {"id": "t2", "type": "constraint", "content": "keep", "rationale": None,
             "repo": None, "agent": None, "scope": "team"},
        ]})
    ctx = RemoteContext(decisions=[_rd("t1", "updated", "team")], deleted=["t2"], cursor="c1")
    fake = _fake_rs(monkeypatch, ctx=ctx)
    up, rm = team_context.pull(team_env, profile=TEAM_PROFILE)
    assert (up, rm) == (1, 1)
    cache = json.loads(team_context._cache_path(team_env).read_text())
    by_id = {d["id"]: d for d in cache["decisions"]}
    assert set(by_id) == {"t1"}
    assert by_id["t1"]["content"] == "updated"
    assert cache["cursor"] == "c1"
    assert fake.calls == [("github.com/a/b", "c0")]  # incremental: prior cursor sent


def test_pull_degraded_keeps_existing_cache(team_env, monkeypatch, capsys):
    team_context._save_cache(team_env, {
        "repo_key": "github.com/a/b", "cursor": "c0",
        "decisions": [{"id": "t1", "type": "architecture", "content": "keep", "rationale": None,
                       "repo": None, "agent": None, "scope": "team"}]})
    _fake_rs(monkeypatch, exc=RemoteUnavailableError("down"))
    assert team_context.pull(team_env, profile=TEAM_PROFILE) == (0, 0)
    cache = json.loads(team_context._cache_path(team_env).read_text())
    assert [d["id"] for d in cache["decisions"]] == ["t1"]  # untouched
    assert "unreachable" in capsys.readouterr().err.lower()  # C8 warned once


def test_pull_auth_failure_degrades(team_env, monkeypatch, capsys):
    _fake_rs(monkeypatch, exc=RemoteAuthError("401"))
    assert team_context.pull(team_env, profile=TEAM_PROFILE) == (0, 0)
    assert "contexer login --team" in capsys.readouterr().err


def test_pull_null_cursor_preserves_prior_cursor(team_env, monkeypatch):
    team_context._save_cache(team_env, {"repo_key": "github.com/a/b", "cursor": "c0", "decisions": []})
    _fake_rs(monkeypatch, ctx=RemoteContext(decisions=[], deleted=[], cursor=None))
    team_context.pull(team_env, profile=TEAM_PROFILE)
    cache = json.loads(team_context._cache_path(team_env).read_text())
    assert cache["cursor"] == "c0"  # empty pull doesn't wipe the cursor


# ── last_sync telemetry ────────────────────────────────────────────────────────────

def test_last_sync_recorded_on_success(team_env, monkeypatch):
    ctx = RemoteContext(decisions=[_rd("t1", "team rule", "team")], deleted=[], cursor="c1")
    _fake_rs(monkeypatch, ctx=ctx)
    team_context.pull(team_env, profile=TEAM_PROFILE)
    cache = json.loads(team_context._cache_path(team_env).read_text())
    last_sync = cache["last_sync"]
    assert last_sync["ok"] is True
    assert isinstance(last_sync["at"], float)
    assert isinstance(last_sync["duration_ms"], int)
    assert last_sync["upserted"] == 1
    assert last_sync["removed"] == 0


def test_last_sync_recorded_on_degraded(team_env, monkeypatch):
    _fake_rs(monkeypatch, exc=RemoteUnavailableError("down"))
    team_context.pull(team_env, profile=TEAM_PROFILE)
    cache = json.loads(team_context._cache_path(team_env).read_text())
    last_sync = cache["last_sync"]
    assert last_sync["ok"] is False
    assert last_sync["error"] == "degraded"
    assert isinstance(last_sync["duration_ms"], int)
    assert cache["decisions"] == []  # degraded path only writes telemetry, never decisions


def test_last_sync_degraded_preserves_existing_decisions(team_env, monkeypatch):
    team_context._save_cache(team_env, {
        "repo_key": "github.com/a/b", "cursor": "c0",
        "decisions": [{"id": "t1", "type": "architecture", "content": "keep", "rationale": None,
                       "repo": None, "agent": None, "scope": "team"}]})
    _fake_rs(monkeypatch, exc=RemoteAuthError("401"))
    team_context.pull(team_env, profile=TEAM_PROFILE)
    cache = json.loads(team_context._cache_path(team_env).read_text())
    assert [d["id"] for d in cache["decisions"]] == ["t1"]  # untouched by the failed attempt
    assert cache["last_sync"]["ok"] is False


def test_last_sync_no_cache_file_for_local_mode(team_env, monkeypatch):
    monkeypatch.setattr(team_context.RemoteStore, "from_profile", staticmethod(lambda p, **kw: None))
    team_context.pull(team_env, profile=config.Profile())
    assert not team_context._cache_path(team_env).exists()


def test_last_sync_no_cache_file_for_no_origin_repo(team_env, monkeypatch):
    monkeypatch.setattr(store, "_git", lambda repo, *a: None)  # no origin
    _fake_rs(monkeypatch, ctx=RemoteContext([_rd("t1", "x")], [], "c1"))
    team_context.pull(team_env, profile=TEAM_PROFILE)
    assert not team_context._cache_path(team_env).exists()


def test_last_sync_at_is_sync_start_not_end_of_write(team_env, monkeypatch):
    # `at` must equal the clock reading taken BEFORE the network call, not a later one
    # taken while computing duration_ms or serialising/writing the cache. Feed a fixed
    # sequence of clock readings: 1000.0 (start), 1005.0 (right after the network call,
    # used only for duration_ms). If the code ever reads the clock a THIRD time to stamp
    # `at` (the bug: end-of-write instead of start-of-sync), that call drains this
    # iterator and raises StopIteration, failing the test loudly rather than silently
    # accepting a later timestamp.
    times = iter([1000.0, 1005.0])
    monkeypatch.setattr(team_context.time, "time", lambda: next(times))
    ctx = RemoteContext(decisions=[_rd("t1", "team rule", "team")], deleted=[], cursor="c1")
    _fake_rs(monkeypatch, ctx=ctx)
    team_context.pull(team_env, profile=TEAM_PROFILE)
    cache = json.loads(team_context._cache_path(team_env).read_text())
    last_sync = cache["last_sync"]
    assert last_sync["at"] == 1000.0  # the start reading, not the post-network one
    assert last_sync["duration_ms"] == 5000


def test_last_sync_at_is_sync_start_on_degraded_path(team_env, monkeypatch):
    times = iter([2000.0, 2003.0])
    monkeypatch.setattr(team_context.time, "time", lambda: next(times))
    _fake_rs(monkeypatch, exc=RemoteUnavailableError("down"))
    team_context.pull(team_env, profile=TEAM_PROFILE)
    cache = json.loads(team_context._cache_path(team_env).read_text())
    last_sync = cache["last_sync"]
    assert last_sync["at"] == 2000.0
    assert last_sync["duration_ms"] == 3000


# ── format_team_section ──────────────────────────────────────────────────────────

def test_format_team_section_empty_when_no_cache(tmp_repo):
    assert team_context.format_team_section(tmp_repo) == ""


def test_format_team_section_renders_scope_and_type(tmp_repo):
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": [
        {"id": "t1aaaaaa", "type": "architecture", "content": "Use Postgres", "rationale": None,
         "repo": None, "agent": None, "scope": "team"}]})
    out = team_context.format_team_section(tmp_repo)
    assert "## Team context" in out
    assert "[scope=team]" in out
    assert "[architecture]" in out
    assert "Use Postgres" in out
    assert "(id=t1aaaaaa)" in out


def test_format_team_section_filters_by_type_and_query(tmp_repo):
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": [
        {"id": "a", "type": "architecture", "content": "Use Postgres", "rationale": None,
         "repo": None, "agent": None, "scope": "team"},
        {"id": "b", "type": "constraint", "content": "Never log secrets", "rationale": None,
         "repo": None, "agent": None, "scope": "team"}]})
    arch = team_context.format_team_section(tmp_repo, entry_type="architecture")
    assert "Postgres" in arch and "secrets" not in arch
    q = team_context.format_team_section(tmp_repo, query="secrets")
    assert "secrets" in q and "Postgres" not in q


def test_load_cache_tolerates_corrupt_file(tmp_repo):
    path = team_context._cache_path(tmp_repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json")
    assert team_context._load_cache(tmp_repo) == {"repo_key": None, "cursor": None, "decisions": []}


# ── store.get_context integration ────────────────────────────────────────────────

def test_get_context_appends_team_section(tmp_repo):
    store.update_decision(tmp_repo, "local decision about auth tokens", "sess-1")
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": [
        {"id": "t1", "type": "architecture", "content": "Team: use Postgres", "rationale": None,
         "repo": None, "agent": None, "scope": "team"}]})
    out = store.get_context(tmp_repo)
    assert "## Decisions and context" in out
    assert "decision about auth tokens" in out  # content is normalized (first char capitalized)
    assert "## Team context" in out
    assert "Team: use Postgres" in out


def test_get_context_unchanged_when_no_team_cache(tmp_repo):
    store.update_decision(tmp_repo, "local only decision", "sess-1")
    out = store.get_context(tmp_repo)
    assert "## Team context" not in out


def test_get_context_fresh_clone_shows_team_only(tmp_repo):
    # No local entries, but team cache present — the "fresh clone, no bootstrap" path.
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": [
        {"id": "t1", "type": "architecture", "content": "Team rule X", "rationale": None,
         "repo": None, "agent": None, "scope": "team"}]})
    out = store.get_context(tmp_repo)
    assert out != "No context stored for this repository."
    assert "## Team context" in out
    assert "Team rule X" in out


def test_get_context_no_local_no_team_is_empty(tmp_repo):
    assert store.get_context(tmp_repo) == "No context stored for this repository."


# ── CLI + adapter wiring ─────────────────────────────────────────────────────────

def test_cli_pull_prints_summary(monkeypatch, capsys):
    from contexer import cli
    monkeypatch.setattr(store, "_git_root", lambda p: "/repo")
    monkeypatch.setattr(team_context, "pull", lambda repo: (3, 1))
    cli.pull([])
    out = capsys.readouterr().out
    assert "3" in out and "1" in out


def test_cli_pull_no_repo_errors(monkeypatch):
    from contexer import cli
    monkeypatch.setattr(store, "_git_root", lambda p: "")
    monkeypatch.setattr(store, "_resolve_repo", lambda p: "")
    with pytest.raises(SystemExit):
        cli.pull([])


def test_adapter_pull_team_swallows_errors(monkeypatch):
    from contexer.adapters import claude

    def boom(repo):
        raise RuntimeError("boom")

    monkeypatch.setattr(store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(team_context, "pull", boom)
    assert claude.pull_team("/repo") == (0, 0)  # delegates to the fail-soft refresh() seam


def test_adapter_pull_team_returns_counts(monkeypatch):
    from contexer.adapters import claude
    monkeypatch.setattr(store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(team_context, "pull", lambda repo, **kw: (2, 0))
    assert claude.pull_team("/repo") == (2, 0)


# ── Option A seam: shared session-start rendering ────────────────────────────────

def _seed_team(repo, content="Team rule X", rid="t1aaaaaa", rtype="architecture"):
    team_context._save_cache(repo, {"repo_key": "k", "cursor": None, "decisions": [
        {"id": rid, "type": rtype, "content": content, "rationale": None,
         "repo": None, "agent": None, "scope": "team"}]})


def test_session_start_payload_appends_team_section(tmp_repo):
    store.update_decision(tmp_repo, "local constraint never log secrets", "s1", subtype="constraint")
    _seed_team(tmp_repo, "Team deploy via CI only")
    ctx = store.session_start_payload(tmp_repo)["context"]
    assert "## Team context" in ctx
    assert "Team deploy via CI only" in ctx


def test_session_start_payload_no_team_when_cache_absent(tmp_repo):
    store.update_decision(tmp_repo, "local constraint x", "s1", subtype="constraint")
    ctx = store.session_start_payload(tmp_repo)["context"]
    assert "## Team context" not in ctx  # local-only session start is unchanged


def test_session_start_payload_status_suffix_when_team_synced(tmp_repo):
    store.update_decision(tmp_repo, "local constraint never log secrets", "s1", subtype="constraint")
    _seed_team(tmp_repo, "Team deploy via CI only")
    payload = store.session_start_payload(tmp_repo)
    assert payload["status"].endswith(" | team: 1 synced")


def test_session_start_payload_no_status_suffix_without_team(tmp_repo):
    store.update_decision(tmp_repo, "local constraint x", "s1", subtype="constraint")
    payload = store.session_start_payload(tmp_repo)
    assert "| team:" not in payload["status"]


def test_session_start_payload_status_suffix_caps_at_display_limit(tmp_repo):
    # format_team_section only ever renders _TEAM_DISPLAY (25) rows, so a cache holding
    # more than that must not claim a synced count the model never actually received.
    store.update_decision(tmp_repo, "local constraint never log secrets", "s1", subtype="constraint")
    decisions = [{"id": f"t{i}", "type": "architecture", "content": f"rule {i}",
                  "rationale": None, "repo": None, "agent": None, "scope": "team"}
                 for i in range(30)]
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": decisions})
    payload = store.session_start_payload(tmp_repo)
    assert payload["status"].endswith(" | team: 30 synced (25 shown)")


def test_session_start_payload_status_suffix_exact_cap_no_shown_note(tmp_repo):
    store.update_decision(tmp_repo, "local constraint never log secrets", "s1", subtype="constraint")
    decisions = [{"id": f"t{i}", "type": "architecture", "content": f"rule {i}",
                  "rationale": None, "repo": None, "agent": None, "scope": "team"}
                 for i in range(25)]
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": decisions})
    payload = store.session_start_payload(tmp_repo)
    assert payload["status"].endswith(" | team: 25 synced")
    assert "shown" not in payload["status"]


def test_session_start_payload_fresh_clone_shows_team(tmp_repo):
    # No local decisions, but a team cache exists — a fresh clone should still see team.
    _seed_team(tmp_repo, "Team rule survives fresh clone")
    ctx = store.session_start_payload(tmp_repo)["context"]
    assert "## Team context" in ctx
    assert "Team rule survives fresh clone" in ctx


def test_get_session_start_context_envelope_includes_team(tmp_repo):
    # Every adapter renders team at session start through this ONE builder.
    _seed_team(tmp_repo, "Team via Claude envelope")
    blob = json.dumps(store.get_session_start_context(tmp_repo))
    assert "Team via Claude envelope" in blob


def test_session_start_payload_resume_with_decisions_suppresses_team(tmp_repo):
    # Resume + local decisions: local context is deliberately "" (decisions already in the
    # reloaded conversation, alongside the team block injected at the original start). Team
    # must NOT be re-appended here — that would duplicate it; deltas surface via the poll.
    store.update_decision(tmp_repo, "local decision present on resume", "s1", subtype="constraint")
    _seed_team(tmp_repo, "Team rule should not double on resume")
    payload = store.session_start_payload(tmp_repo, source="resume")
    assert payload["context"] == ""
    assert "## Team context" not in payload["context"]


def test_session_start_payload_resume_fresh_clone_shows_team(tmp_repo):
    # Resume with NO local decisions (fresh clone): local mining context is non-empty, so
    # team still surfaces — the resume-suppression only applies to the empty-context path.
    _seed_team(tmp_repo, "Team rule on fresh resume")
    ctx = store.session_start_payload(tmp_repo, source="resume")["context"]
    assert "## Team context" in ctx
    assert "Team rule on fresh resume" in ctx


# ── Option A seam: neutral refresh / poll_for_injection ──────────────────────────

def test_refresh_delegates_to_pull(monkeypatch):
    monkeypatch.setattr(store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(team_context, "pull", lambda repo, **kw: (2, 1))
    assert team_context.refresh("/x") == (2, 1)


def test_refresh_passes_short_timeout_to_pull(monkeypatch):
    captured = {}
    monkeypatch.setattr(store, "_resolve_repo", lambda p: "/repo")

    def fake_pull(repo, *, profile=None, timeout=None):
        captured["timeout"] = timeout
        return (0, 0)

    monkeypatch.setattr(team_context, "pull", fake_pull)
    team_context.refresh("/x")
    assert captured["timeout"] == team_context._SESSION_START_TIMEOUT == 3.0


def test_refresh_timeout_reaches_remote_store_construction(team_env, monkeypatch):
    # End-to-end through pull -> _sync -> RemoteStore.from_profile, with only RemoteStore
    # itself faked (real _sync/pull/refresh code runs) - proves the seam is fully wired.
    captured = {}

    def fake_from_profile(profile, **kw):
        captured.update(kw)
        return _FakeRS(ctx=RemoteContext(decisions=[], deleted=[], cursor=None))

    monkeypatch.setattr(team_context.RemoteStore, "from_profile", staticmethod(fake_from_profile))
    monkeypatch.setattr(team_context.config, "load_profile", lambda: TEAM_PROFILE)
    monkeypatch.setattr(store, "_resolve_repo", lambda p: team_env)
    team_context.refresh(team_env)
    assert captured["timeout"] == 3.0


def test_poll_keeps_default_timeout(team_env, monkeypatch):
    # poll() must NOT inherit the SessionStart short timeout - only refresh() does.
    captured = {}

    def fake_from_profile(profile, **kw):
        captured.update(kw)
        return _FakeRS(ctx=RemoteContext(decisions=[], deleted=[], cursor=None))

    monkeypatch.setattr(team_context.RemoteStore, "from_profile", staticmethod(fake_from_profile))
    team_context.poll(team_env, profile=TEAM_PROFILE)
    assert "timeout" not in captured  # no override - RemoteStore.from_profile's own default applies


def test_refresh_empty_repo_is_noop(monkeypatch):
    monkeypatch.setattr(store, "_resolve_repo", lambda p: "")
    assert team_context.refresh("/x") == (0, 0)


def test_refresh_never_raises(monkeypatch):
    monkeypatch.setattr(store, "_resolve_repo", lambda p: "/repo")

    def boom(repo):
        raise RuntimeError("boom")

    monkeypatch.setattr(team_context, "pull", boom)
    assert team_context.refresh("/x") == (0, 0)


def test_poll_for_injection_delegates(monkeypatch):
    monkeypatch.setattr(store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(team_context, "poll_nonblocking", lambda repo: [{"id": "t1", "content": "c"}])
    assert team_context.poll_for_injection("/x") == [{"id": "t1", "content": "c"}]


def test_poll_for_injection_empty_repo_is_noop(monkeypatch):
    monkeypatch.setattr(store, "_resolve_repo", lambda p: "")
    assert team_context.poll_for_injection("/x") == []


def test_poll_for_injection_never_raises(monkeypatch):
    monkeypatch.setattr(store, "_resolve_repo", lambda p: "/repo")

    def boom(repo):
        raise RuntimeError("boom")

    monkeypatch.setattr(team_context, "poll_nonblocking", boom)
    assert team_context.poll_for_injection("/x") == []


# ── No double-inject: session start renders once, poll injects deltas only ────────

def test_team_poll_empty_when_no_new(monkeypatch):
    from contexer.adapters import claude
    monkeypatch.setattr(team_context, "poll_for_injection", lambda rp: [])
    assert claude.team_poll("/repo", "{}") == "{}"


def test_team_poll_injects_new_rows(monkeypatch):
    from contexer.adapters import claude
    monkeypatch.setattr(team_context, "poll_for_injection",
                        lambda rp: [{"id": "t1", "content": "New team rule", "type": "constraint"}])
    out = claude.team_poll("/repo", "{}")
    assert "New team rule" in out
    assert "just approved" in out.lower()
