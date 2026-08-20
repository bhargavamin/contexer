"""Tests for the C5 team-context cache + merge (contexer/team_context.py).

The team cache is separate from the local store: pulled TEAM decisions never enter
store.py entries[], they live in ~/.contexer/.team_{slug}.json and are merged at read
time. RemoteStore is faked here so no network is touched.
"""
import json
import time

import pytest

import contexer.remote as remote
from contexer import config, store, team_context
from contexer.remote import RemoteAuthError, RemoteContext, RemoteDecision, RemoteUnavailableError

TEAM_PROFILE = config.Profile(mode="team", endpoint="https://t/mcp", token="tok")


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
    """tmp_repo isolates STORE_DIR; give the repo a git origin so a canonical key resolves."""
    monkeypatch.setattr(store, "_git", lambda repo, *a: "git@github.com:a/b.git")
    remote.reset_degradation_warnings()
    return tmp_repo


def _fake_rs(monkeypatch, *, ctx=None, exc=None):
    fake = _FakeRS(ctx=ctx, exc=exc)
    monkeypatch.setattr(team_context.RemoteStore, "from_profile", staticmethod(lambda p, **kw: fake))
    return fake


# ── title round-trips through _ROW_FIELDS / _row_to_dict (Decision Titles v2, Task 5) ──

def test_row_to_dict_carries_title():
    rd = _rd("t1", "Use Postgres for the queue.", title="Queue backend: Postgres")
    row = team_context._row_to_dict(rd)
    assert row["title"] == "Queue backend: Postgres"
    assert set(team_context._ROW_FIELDS) == set(row.keys())


def test_row_to_dict_title_none_when_rd_has_none():
    rd = _rd("t1", "no authored title here")
    row = team_context._row_to_dict(rd)
    assert row["title"] is None


def test_pull_cache_round_trips_title(team_env, monkeypatch):
    ctx = RemoteContext(
        decisions=[_rd("t1", "team rule", "team", title="Team rule heading")],
        deleted=[], cursor="c1")
    _fake_rs(monkeypatch, ctx=ctx)
    team_context.pull(team_env, profile=TEAM_PROFILE)
    cache = json.loads(team_context._cache_path(team_env).read_text())
    assert cache["decisions"][0]["title"] == "Team rule heading"


def test_pull_team_ahead_attaches_local_proposal_without_overwriting_standing_revision(
        team_env, monkeypatch):
    local_id = _seed_local(team_env, "keep the original local wording", subtype="constraint")
    store.approve_decision(team_env, local_id, "approve")
    rd = RemoteDecision(
        id="team-1", type="constraint", title="Lead wording", content="use the lead wording",
        rationale=None, repo="github.com/a/b", agent=None, scope="team",
        local_decision_id=local_id, team_id="t-1", team_name="Platform",
        reconciliation={"state": "team_ahead", "personalHead": "p1", "teamHead": "th2"})
    _fake_rs(monkeypatch, ctx=RemoteContext([rd], [], "c1"))

    assert team_context.pull(team_env, profile=TEAM_PROFILE) == (1, 0)
    entry = next(e for e in store._load(team_env)["entries"] if e["id"] == local_id)
    assert entry["content"].casefold() == "keep the original local wording"
    assert entry["proposed_revision"]["content"].casefold() == "use the lead wording"
    assert entry["proposed_revision"]["team_reconciliation"]["team_head"] == "th2"


def test_pull_in_sync_clears_only_team_created_proposal(team_env, monkeypatch):
    local_id = _seed_local(team_env, "keep the original local wording", subtype="constraint")
    store.approve_decision(team_env, local_id, "approve")
    assert store.attach_team_reconciliation_proposal(
        team_env, local_id, content="use approved lead wording", team_id="t-1",
        team_name="Platform", team_head="th2")
    rd = RemoteDecision(
        id="team-1", type="constraint", title=None, content="use approved lead wording",
        rationale=None, repo="github.com/a/b", agent=None, scope="team",
        local_decision_id=local_id, team_id="t-1", team_name="Platform",
        reconciliation={"state": "in_sync", "personalHead": "p2", "teamHead": "th2"})
    _fake_rs(monkeypatch, ctx=RemoteContext([rd], [], "c2"))

    team_context.pull(team_env, profile=TEAM_PROFILE)
    entry = next(e for e in store._load(team_env)["entries"] if e["id"] == local_id)
    assert "proposed_revision" not in entry
    assert entry["last_team_reconciliation"]["outcome"] == "in_sync"


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


def test_sync_drops_row_with_mismatched_repo(team_env, monkeypatch):
    # Defense-in-depth: even though we queried repo="github.com/a/b", a server-side
    # scoping bug could return a row tagged with a different repo. The client must
    # never trust that over the key it asked for.
    good = _rd("t1", "team rule for this repo")  # repo="github.com/a/b" (matches key)
    bad = RemoteDecision(id="t2", type="architecture", title=None,
                         content="leaked from another repo", rationale=None,
                         repo="github.com/other/repo", agent=None, scope="team")
    ctx = RemoteContext(decisions=[good, bad], deleted=[], cursor="c1")
    _fake_rs(monkeypatch, ctx=ctx)
    up, rm = team_context.pull(team_env, profile=TEAM_PROFILE)
    assert up == 1
    cache = json.loads(team_context._cache_path(team_env).read_text())
    assert [d["id"] for d in cache["decisions"]] == ["t1"]


def test_sync_keeps_row_with_no_repo_tag(team_env, monkeypatch):
    # A legitimate row can carry repo=None — must not be rejected as a mismatch.
    rd = RemoteDecision(id="t1", type="architecture", title=None, content="no repo tag",
                        rationale=None, repo=None, agent=None, scope="team")
    ctx = RemoteContext(decisions=[rd], deleted=[], cursor="c1")
    _fake_rs(monkeypatch, ctx=ctx)
    up, rm = team_context.pull(team_env, profile=TEAM_PROFILE)
    assert up == 1
    cache = json.loads(team_context._cache_path(team_env).read_text())
    assert [d["id"] for d in cache["decisions"]] == ["t1"]


def test_sync_purges_stale_cache_on_later_mismatched_repo(team_env, monkeypatch):
    # A row cached correctly for this repo on one sync later comes back under the SAME id
    # but tagged with a different repo (e.g. re-scoped/corrected server-side). The stale
    # cached copy must be dropped outright, not merely left untouched - otherwise it keeps
    # rendering forever since no explicit deletion tombstone will ever arrive for it.
    good = _rd("t1", "team rule for this repo")
    _fake_rs(monkeypatch, ctx=RemoteContext(decisions=[good], deleted=[], cursor="c1"))
    team_context.pull(team_env, profile=TEAM_PROFILE)
    cache = json.loads(team_context._cache_path(team_env).read_text())
    assert [d["id"] for d in cache["decisions"]] == ["t1"]

    mismatched = RemoteDecision(id="t1", type="architecture", title=None,
                                content="reassigned elsewhere", rationale=None,
                                repo="github.com/other/repo", agent=None, scope="team")
    _fake_rs(monkeypatch, ctx=RemoteContext(decisions=[mismatched], deleted=[], cursor="c2"))
    up, rm = team_context.pull(team_env, profile=TEAM_PROFILE)
    assert (up, rm) == (0, 1)
    cache = json.loads(team_context._cache_path(team_env).read_text())
    assert cache["decisions"] == []


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
    err = capsys.readouterr().err
    assert "contexer login" in err and "--team" not in err  # a flag that never existed


def test_an_auth_rejection_is_recorded_as_auth_not_a_generic_degradation(team_env, monkeypatch):
    """`remote` classifies the failure, but `with_local_fallback` returns only its default, so
    the class was discarded and every degradation landed as "degraded". A token the server has
    REVOKED still looks unexpired locally, so with the type thrown away nothing downstream could
    tell an auth failure from an outage — which is how one reads as the other and sends the
    developer off to check their network."""
    _fake_rs(monkeypatch, exc=RemoteAuthError("401"))
    team_context.pull(team_env, profile=TEAM_PROFILE)
    cache = json.loads(team_context._cache_path(team_env).read_text())
    assert cache["last_sync"]["error"] == "auth"
    assert cache["last_sync"]["ok"] is False


def test_an_unreachable_endpoint_stays_a_generic_degradation(team_env, monkeypatch):
    """The other half of the contract: only a rejection is labelled `auth`, so nothing can offer
    a pointless login when the network is what broke."""
    _fake_rs(monkeypatch, exc=RemoteUnavailableError("connection refused"))
    team_context.pull(team_env, profile=TEAM_PROFILE)
    cache = json.loads(team_context._cache_path(team_env).read_text())
    assert cache["last_sync"]["error"] == "degraded"


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


# ── consecutive_failures + last_ok_at (backoff / staleness telemetry) ────────────

def test_success_sets_last_ok_at_and_resets_failures(team_env, monkeypatch):
    team_context._save_cache(team_env, {
        "repo_key": "github.com/a/b", "cursor": "c0", "decisions": [],
        "last_sync": {"at": 1, "ok": False, "duration_ms": 1, "error": "degraded",
                     "consecutive_failures": 3}})
    _fake_rs(monkeypatch, ctx=RemoteContext([_rd("t1", "x")], [], "c1"))
    team_context.pull(team_env, profile=TEAM_PROFILE)
    cache = json.loads(team_context._cache_path(team_env).read_text())
    assert cache["last_sync"]["consecutive_failures"] == 0
    assert isinstance(cache["last_ok_at"], float)


def test_degraded_increments_consecutive_failures(team_env, monkeypatch):
    team_context._save_cache(team_env, {
        "repo_key": "github.com/a/b", "cursor": "c0", "decisions": [],
        "last_sync": {"at": 1, "ok": True, "duration_ms": 1, "consecutive_failures": 0}})
    _fake_rs(monkeypatch, exc=RemoteUnavailableError("down"))
    team_context.pull(team_env, profile=TEAM_PROFILE)
    cache = json.loads(team_context._cache_path(team_env).read_text())
    assert cache["last_sync"]["consecutive_failures"] == 1
    _fake_rs(monkeypatch, exc=RemoteUnavailableError("down"))
    team_context.pull(team_env, profile=TEAM_PROFILE)
    cache = json.loads(team_context._cache_path(team_env).read_text())
    assert cache["last_sync"]["consecutive_failures"] == 2


def test_degraded_first_failure_from_absent_counter(team_env, monkeypatch):
    _fake_rs(monkeypatch, exc=RemoteUnavailableError("down"))
    team_context.pull(team_env, profile=TEAM_PROFILE)
    cache = json.loads(team_context._cache_path(team_env).read_text())
    assert cache["last_sync"]["consecutive_failures"] == 1


def test_consecutive_failures_reads_fail_soft():
    assert team_context._consecutive_failures({}) == 0
    assert team_context._consecutive_failures({"last_sync": "not a dict"}) == 0
    assert team_context._consecutive_failures({"last_sync": {"consecutive_failures": "x"}}) == 0
    assert team_context._consecutive_failures({"last_sync": {"consecutive_failures": -1}}) == 0
    assert team_context._consecutive_failures({"last_sync": {"consecutive_failures": 4}}) == 4


def test_poll_interval_backoff_doubles_and_caps():
    assert team_context._poll_interval({}) == team_context._POLL_MIN_INTERVAL
    assert team_context._poll_interval(
        {"last_sync": {"consecutive_failures": 1}}) == team_context._POLL_MIN_INTERVAL * 2
    assert team_context._poll_interval(
        {"last_sync": {"consecutive_failures": 2}}) == team_context._POLL_MIN_INTERVAL * 4
    # Enough failures to blow past the ceiling — must clamp, never grow unbounded.
    assert team_context._poll_interval(
        {"last_sync": {"consecutive_failures": 20}}) == team_context._POLL_MAX_INTERVAL


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


# ── title-led rendering (Decision Titles v2, Task 5) ─────────────────────────────
# format_team_section renders title-led exactly like a local decision (store._title_and_body):
# title on the bullet line, content on an indented line below - skipped when it would merely
# repeat the title - and derives a title when the cloud sent none.

def test_format_team_section_renders_title_heading_and_indented_content(tmp_repo):
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": [
        {"id": "t1aaaaaa", "type": "architecture", "title": "Queue backend: Postgres",
         "content": "Use Postgres for the queue, not MySQL.", "rationale": None,
         "repo": None, "agent": None, "scope": "team"}]})
    out = team_context.format_team_section(tmp_repo)
    lines = out.splitlines()
    assert lines[1] == "- [scope=team] [architecture] Queue backend: Postgres (id=t1aaaaaa)"
    assert lines[2] == "    Use Postgres for the queue, not MySQL."


def test_format_team_section_dedups_when_title_equals_content(tmp_repo):
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": [
        {"id": "t1aaaaaa", "type": "architecture", "title": "Use Postgres",
         "content": "Use Postgres", "rationale": None,
         "repo": None, "agent": None, "scope": "team"}]})
    out = team_context.format_team_section(tmp_repo)
    lines = out.splitlines()
    assert len(lines) == 2  # header + one bullet line only, no repeated indented content
    assert out.count("Use Postgres") == 1


def test_format_team_section_derives_title_when_cloud_sent_none(tmp_repo):
    long_content = "Adopt the outbox pattern for share retries. " + "detail " * 30
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": [
        {"id": "t1aaaaaa", "type": "architecture", "content": long_content, "rationale": None,
         "repo": None, "agent": None, "scope": "team"}]})  # no "title" key at all
    out = team_context.format_team_section(tmp_repo)
    derived = store._derive_title(long_content)
    lines = out.splitlines()
    assert lines[1] == f"- [scope=team] [architecture] {derived} (id=t1aaaaaa)"
    # The derived title is the leading sentence of long_content; _title_and_body strips
    # that repeated prefix so the body line doesn't restate the title verbatim.
    remainder = " ".join(long_content.split())[len(derived):].strip()
    assert lines[2] == f"    {remainder}"


def test_format_team_section_derives_title_when_row_title_is_none(tmp_repo):
    # A row with an explicit "title": None (a pre-title-v2 row echoed through get_context)
    # behaves identically to a row missing the key entirely.
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": [
        {"id": "t1aaaaaa", "type": "architecture", "title": None, "content": "Use Postgres",
         "rationale": None, "repo": None, "agent": None, "scope": "team"}]})
    out = team_context.format_team_section(tmp_repo)
    assert "- [scope=team] [architecture] Use Postgres (id=t1aaaaaa)" in out


# ── local-dedup (provenance-preserving) ──────────────────────────────────────────
#
# When a team row says essentially the same thing as a LOCAL decision, injecting both wastes
# tokens and reads as two sources. Overlap (store's novelty metric) decides: >= 0.7 collapses
# to a "ratifies local decision" pointer; 0.5-0.7 renders in full but tagged with the local id;
# below 0.5 renders as before. Comparison degrades to today's behavior on any local-store error.

# 8 distinct tokens; a team row sharing 5 of them is 5/8 = 0.625 overlap (the 0.5-0.7 tier).
_LOCAL8 = "alpha beta gamma delta epsilon zeta eta theta"
_TEAM_PARTIAL_OVERLAP = "alpha beta gamma delta epsilon iota kappa lambda"  # 5/8 = 0.625


def _seed_local(repo, content, session="s1", subtype=""):
    ok, did = store.update_decision(repo, content, session, subtype=subtype)
    assert ok
    return did


def _one_team_row(repo, content, rid="teamaaaa", rtype="architecture", scope="team", title=None):
    team_context._save_cache(repo, {"repo_key": "k", "cursor": None, "decisions": [
        {"id": rid, "type": rtype, "title": title, "content": content, "rationale": None,
         "repo": None, "agent": None, "scope": scope}]})


def test_dedup_collapses_team_row_identical_to_local(tmp_repo):
    lid = _seed_local(tmp_repo, _LOCAL8)
    _one_team_row(tmp_repo, _LOCAL8, rid="teamaaaa")
    out = team_context.format_team_section(tmp_repo)
    assert f"ratifies local decision {lid[:8]}" in out
    assert "(id=teamaaaa)" in out       # team-id provenance is kept
    assert "alpha" not in out           # the duplicate text is NOT re-injected
    assert "[scope=team]" in out


def test_dedup_partial_overlap_renders_full_with_tag(tmp_repo):
    lid = _seed_local(tmp_repo, _LOCAL8)
    _one_team_row(tmp_repo, _TEAM_PARTIAL_OVERLAP, rid="teambbbb")
    out = team_context.format_team_section(tmp_repo)
    assert f"[scope=team, overlaps local {lid[:8]}]" in out
    assert "iota kappa lambda" in out   # full divergent text stays visible
    assert "ratifies" not in out
    assert "(id=teambbbb)" in out


def test_dedup_partial_overlap_preserves_non_team_scope(tmp_repo):
    # The 0.5-0.7 partial-overlap branch must read `scope` from the row itself, not hardcode
    # "team" - a non-"team" scope value must pass through unmodified, matching the else branch.
    _seed_local(tmp_repo, _LOCAL8)
    _one_team_row(tmp_repo, _TEAM_PARTIAL_OVERLAP, rid="teameeee", scope="org")
    out = team_context.format_team_section(tmp_repo)
    assert "[scope=org, overlaps local" in out
    assert "[scope=team, overlaps local" not in out


def test_dedup_unrelated_row_renders_unchanged(tmp_repo):
    _seed_local(tmp_repo, _LOCAL8)
    _one_team_row(tmp_repo, "completely different rule about logging", rid="teamcccc")
    out = team_context.format_team_section(tmp_repo)
    assert "- [scope=team] [architecture] completely different rule about logging (id=teamcccc)" in out
    assert "ratifies" not in out
    assert "overlaps local" not in out


def test_defer_architecture_still_collapses_ratifying_row(tmp_repo):
    # A ratifying architecture row (>= 0.7 overlap) already collapses to a cheap one-liner
    # naming the local decision it ratifies - deferring it would throw away that specific
    # signal for zero token savings, so it must render as usual even with defer_architecture.
    lid = _seed_local(tmp_repo, _LOCAL8)
    _one_team_row(tmp_repo, _LOCAL8, rid="teamaaaa")
    out = team_context.format_team_section(tmp_repo, defer_architecture=True)
    assert f"ratifies local decision {lid[:8]}" in out
    assert "synced but deferred" not in out


def test_defer_architecture_defers_non_ratifying_rows(tmp_repo):
    # Partial overlap (0.5-0.7) and no overlap both still render full content today - with
    # defer_architecture these DO get deferred since they aren't already a cheap collapse.
    _seed_local(tmp_repo, _LOCAL8)
    _one_team_row(tmp_repo, _TEAM_PARTIAL_OVERLAP, rid="teambbbb")
    out = team_context.format_team_section(tmp_repo, defer_architecture=True)
    assert "overlaps local" not in out
    assert "iota kappa lambda" not in out
    assert "1 team architecture decision" in out


def test_dedup_ratifies_variant_ignores_row_title(tmp_repo):
    # The "ratifies" pointer never surfaces content OR title - only the local id - so a
    # title on the collapsed row must not leak into it.
    lid = _seed_local(tmp_repo, _LOCAL8)
    _one_team_row(tmp_repo, _LOCAL8, rid="teamaaaa", title="A distinct given title")
    out = team_context.format_team_section(tmp_repo)
    assert f"ratifies local decision {lid[:8]}" in out
    assert "A distinct given title" not in out


def test_dedup_overlap_variant_renders_title_led(tmp_repo):
    # The 0.5-0.7 partial-overlap branch gets the same title-led treatment as the plain
    # branch: title (or the row's own) on the bullet line, full content indented below.
    lid = _seed_local(tmp_repo, _LOCAL8)
    _one_team_row(tmp_repo, _TEAM_PARTIAL_OVERLAP, rid="teambbbb", title="Partial overlap heading")
    out = team_context.format_team_section(tmp_repo)
    lines = out.splitlines()
    title_line = next(ln for ln in lines if "overlaps local" in ln)
    assert title_line == (
        f"- [scope=team, overlaps local {lid[:8]}] [architecture] "
        f"Partial overlap heading (id=teambbbb)")
    assert f"    {_TEAM_PARTIAL_OVERLAP}" in out


def test_dedup_ignores_ignored_local_decisions(tmp_repo):
    _seed_local(tmp_repo, _LOCAL8)
    # Mark the only local decision ignored - it must not be a dedup target.
    data = store._load(tmp_repo)
    data["entries"][0]["status"] = "ignored"
    store._save(tmp_repo, data)
    _one_team_row(tmp_repo, _LOCAL8, rid="teamdddd")
    out = team_context.format_team_section(tmp_repo)
    assert "ratifies" not in out        # ignored local is invisible to matching
    assert "alpha" in out               # so the team row renders in full


def test_dedup_fail_soft_when_local_store_raises(tmp_repo, monkeypatch):
    _one_team_row(tmp_repo, _LOCAL8, rid="teameeee")

    def boom(_repo):
        raise RuntimeError("corrupt store")

    monkeypatch.setattr(store, "_load", boom)
    out = team_context.format_team_section(tmp_repo)   # must not raise
    assert "- [scope=team] [architecture] " + _LOCAL8 + " (id=teameeee)" in out
    assert "ratifies" not in out


def test_dedup_cap_applies_after_collapse(tmp_repo):
    _seed_local(tmp_repo, _LOCAL8)
    # 30 team rows (> _TEAM_DISPLAY=25); the first is a collapse, the rest unrelated. Collapsed
    # one-liners still count as rows, so exactly _TEAM_DISPLAY rows render.
    rows = [{"id": "collapse0", "type": "architecture", "content": _LOCAL8, "rationale": None,
             "repo": None, "agent": None, "scope": "team"}]
    for i in range(29):
        rows.append({"id": f"row{i:05d}", "type": "architecture",
                     "content": f"unrelated distinct decision number {i}", "rationale": None,
                     "repo": None, "agent": None, "scope": "team"})
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": rows})
    out = team_context.format_team_section(tmp_repo)
    rendered = [ln for ln in out.splitlines() if ln.startswith("- ")]
    assert len(rendered) == team_context._TEAM_DISPLAY
    assert any("ratifies local decision" in ln for ln in rendered)


def test_load_cache_tolerates_corrupt_file(tmp_repo):
    path = team_context._cache_path(tmp_repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json")
    assert team_context._load_cache(tmp_repo) == {"repo_key": None, "cursor": None, "decisions": []}


# ── staleness tag on the header ───────────────────────────────────────────────────

def _seed_with_last_ok_at(repo, *, last_ok_at):
    data = {"repo_key": "k", "cursor": None,
           "decisions": [{"id": "t1aaaaaa", "type": "architecture", "content": "Use Postgres",
                          "rationale": None, "repo": None, "agent": None, "scope": "team"}]}
    if last_ok_at is not None:
        data["last_ok_at"] = last_ok_at
    team_context._save_cache(repo, data)


def test_fresh_sync_no_stale_tag(tmp_repo):
    _seed_with_last_ok_at(tmp_repo, last_ok_at=time.time())
    out = team_context.format_team_section(tmp_repo)
    assert out.startswith("## Team context (synced)\n")
    assert "stale" not in out


def test_missing_last_ok_at_no_stale_tag(tmp_repo):
    _seed_with_last_ok_at(tmp_repo, last_ok_at=None)  # old-format cache, field absent
    out = team_context.format_team_section(tmp_repo)
    assert out.startswith("## Team context (synced)\n")
    assert "stale" not in out


def test_stale_header_hours_wording(tmp_repo):
    _seed_with_last_ok_at(tmp_repo, last_ok_at=time.time() - 30 * 3600)  # 30h ago
    out = team_context.format_team_section(tmp_repo)
    assert "## Team context (synced 30 hours ago - may be stale)" in out


def test_stale_header_exactly_24h_is_tagged(tmp_repo):
    _seed_with_last_ok_at(tmp_repo, last_ok_at=time.time() - team_context._STALE_AFTER)
    out = team_context.format_team_section(tmp_repo)
    assert "may be stale" in out


def test_fresh_23h_not_tagged(tmp_repo):
    _seed_with_last_ok_at(tmp_repo, last_ok_at=time.time() - 23 * 3600)
    out = team_context.format_team_section(tmp_repo)
    assert "stale" not in out


def test_stale_header_days_wording_at_48h(tmp_repo):
    _seed_with_last_ok_at(tmp_repo, last_ok_at=time.time() - 48 * 3600)
    out = team_context.format_team_section(tmp_repo)
    assert "## Team context (synced 2 days ago - may be stale)" in out


def test_stale_header_days_wording_multi_day(tmp_repo):
    _seed_with_last_ok_at(tmp_repo, last_ok_at=time.time() - 5 * 86400)
    out = team_context.format_team_section(tmp_repo)
    assert "## Team context (synced 5 days ago - may be stale)" in out


def test_stale_tag_only_touches_header_not_rows(tmp_repo):
    _seed_with_last_ok_at(tmp_repo, last_ok_at=time.time() - 72 * 3600)
    out = team_context.format_team_section(tmp_repo)
    lines = out.split("\n")
    assert "may be stale" in lines[0]
    assert "Use Postgres" in lines[1]
    assert "may be stale" not in lines[1]


# ── last_render telemetry ─────────────────────────────────────────────────────────

def test_render_records_rows_and_chars(tmp_repo):
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": [
        {"id": "t1", "type": "architecture", "content": "Use Postgres", "rationale": None,
         "repo": None, "agent": None, "scope": "team"}]})
    out = team_context.format_team_section(tmp_repo)
    cache = json.loads(team_context._cache_path(tmp_repo).read_text())
    last_render = cache["last_render"]
    assert last_render["rows"] == 1
    assert last_render["chars"] == len(out)
    assert isinstance(last_render["at"], float)


def test_render_not_written_when_cache_file_absent(tmp_repo, monkeypatch):
    # No cache file exists at all — format_team_section returns "" (no rows) and must
    # never create one from the render path.
    assert team_context.format_team_section(tmp_repo) == ""
    assert not team_context._cache_path(tmp_repo).exists()


def test_record_render_guard_direct_no_file_created(tmp_repo):
    # Exercise the guard directly (belt and suspenders on top of the format_team_section
    # path above): calling _record_render with no cache file on disk must be a pure no-op.
    team_context._record_render(tmp_repo, {}, rows=5, chars=100)
    assert not team_context._cache_path(tmp_repo).exists()


def test_render_fail_soft_on_write_error(tmp_repo, monkeypatch):
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": [
        {"id": "t1", "type": "architecture", "content": "Use Postgres", "rationale": None,
         "repo": None, "agent": None, "scope": "team"}]})

    def boom(repo, data):
        raise OSError("disk full")

    monkeypatch.setattr(team_context, "_save_cache", boom)
    out = team_context.format_team_section(tmp_repo)  # must not raise
    assert "Use Postgres" in out


def test_record_render_preserves_concurrent_refresher_write(tmp_repo, monkeypatch):
    # format_team_section loads the cache ONCE, up front, and renders from that snapshot.
    # A background refresher (poll_nonblocking, a separate process) can complete and write
    # fresh decisions/cursor/last_ok_at/consecutive_failures in the gap before _record_render
    # does its own save. _record_render must re-read the cache fresh right before saving and
    # touch ONLY last_render — never spread the stale snapshot format_team_section rendered
    # from back over that fresher on-disk write.
    stale = {"repo_key": "k", "cursor": "c0", "decisions": [
        {"id": "t1", "type": "architecture", "content": "stale rule", "rationale": None,
         "repo": None, "agent": None, "scope": "team"}]}
    fresh_on_disk = {"repo_key": "k", "cursor": "c1", "decisions": [
        {"id": "t1", "type": "architecture", "content": "stale rule", "rationale": None,
         "repo": None, "agent": None, "scope": "team"},
        {"id": "t2", "type": "architecture", "content": "fresh rule from refresher",
         "rationale": None, "repo": None, "agent": None, "scope": "team"}],
        "last_ok_at": 12345.0, "consecutive_failures": 0}
    # Seed the cache file with what a concurrent refresher would already have written by
    # the time _record_render re-reads it.
    team_context._save_cache(tmp_repo, fresh_on_disk)

    real_load_cache = team_context._load_cache
    calls = {"n": 0}

    def fake_load_cache(repo_path):
        calls["n"] += 1
        if calls["n"] == 1:
            return stale  # format_team_section's own initial load (pre-refresher snapshot)
        return real_load_cache(repo_path)  # _record_render's re-load: sees the real file

    monkeypatch.setattr(team_context, "_load_cache", fake_load_cache)
    out = team_context.format_team_section(tmp_repo)
    assert "stale rule" in out
    assert "fresh rule from refresher" not in out  # rendered from the stale snapshot

    saved = json.loads(team_context._cache_path(tmp_repo).read_text())
    # The refresher's fresh write survives - not clobbered by the stale snapshot.
    assert saved["cursor"] == "c1"
    assert [d["id"] for d in saved["decisions"]] == ["t1", "t2"]
    assert saved["last_ok_at"] == 12345.0
    assert saved["consecutive_failures"] == 0
    # And the new telemetry landed on top of that fresh copy.
    assert saved["last_render"]["rows"] == 1  # rendered from the stale, single-row snapshot
    assert isinstance(saved["last_render"]["at"], float)


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


def test_cli_pull_names_a_dead_session_instead_of_reporting_zero(monkeypatch, capsys):
    """"Pulled 0 team decision(s)." is the same sentence for "nothing new upstream" and "your
    session died three days ago" — the ambiguity that let an expired login sit unnoticed while
    every sync failed. auth_state is a local read, so naming the cause costs nothing."""
    from contexer import auth, cli
    monkeypatch.setattr(store, "_git_root", lambda p: "/repo")
    monkeypatch.setattr(team_context, "pull", lambda repo: (0, 0))
    monkeypatch.setattr(auth, "auth_state", lambda profile: {
        "state": "refresh_failed", "issuer": "https://mcp.example", "expires_at": None,
        "scope": None, "message": "The Teams session expired and the refresh was rejected."})
    cli.pull([])
    captured = capsys.readouterr()
    assert "Pulled 0 team decision(s)." in captured.out
    assert "refresh was rejected" in captured.err


def test_cli_pull_stays_quiet_when_a_live_session_simply_has_nothing_new(monkeypatch, capsys):
    """A healthy zero-row pull must not grow a scary second line."""
    from contexer import auth, cli
    monkeypatch.setattr(store, "_git_root", lambda p: "/repo")
    monkeypatch.setattr(team_context, "pull", lambda repo: (0, 0))
    monkeypatch.setattr(auth, "auth_state", lambda profile: {
        "state": "logged_in", "issuer": "https://mcp.example", "expires_at": None,
        "scope": None, "message": "Signed in."})
    cli.pull([])
    assert capsys.readouterr().err == ""


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
    # Non-architecture type: architecture-typed team rows are deferred at SessionStart
    # (see test_session_start_payload_defers_team_architecture_shows_rest below).
    _seed_team(tmp_repo, "Team deploy via CI only", rtype="constraint")
    ctx = store.session_start_payload(tmp_repo)["context"]
    assert "## Team context" in ctx
    assert "Team deploy via CI only" in ctx


def test_session_start_payload_no_team_when_cache_absent(tmp_repo):
    store.update_decision(tmp_repo, "local constraint x", "s1", subtype="constraint")
    ctx = store.session_start_payload(tmp_repo)["context"]
    assert "## Team context" not in ctx  # local-only session start is unchanged


def test_session_start_payload_status_suffix_when_team_synced(tmp_repo):
    store.update_decision(tmp_repo, "local constraint never log secrets", "s1", subtype="constraint")
    # Non-architecture type: architecture rows are deferred at SessionStart and excluded
    # from the "shown" count (see test_session_start_payload_status_suffix_notes_deferred
    # below) - this test is about the plain cap-only suffix wording.
    _seed_team(tmp_repo, "Team deploy via CI only", rtype="constraint")
    payload = store.session_start_payload(tmp_repo)
    assert payload["status"].endswith(" | team: 1 synced")


def test_session_start_payload_no_status_suffix_without_team(tmp_repo):
    store.update_decision(tmp_repo, "local constraint x", "s1", subtype="constraint")
    payload = store.session_start_payload(tmp_repo)
    assert "| team:" not in payload["status"]


def test_session_start_payload_status_suffix_caps_at_display_limit(tmp_repo):
    # format_team_section only ever renders _TEAM_DISPLAY (25) rows, so a cache holding
    # more than that must not claim a synced count the model never actually received.
    # Non-architecture type: this test is about the display cap, not deferral (see
    # test_session_start_payload_status_suffix_notes_deferred_architecture below).
    store.update_decision(tmp_repo, "local constraint never log secrets", "s1", subtype="constraint")
    decisions = [{"id": f"t{i}", "type": "constraint", "content": f"rule {i}",
                  "rationale": None, "repo": None, "agent": None, "scope": "team"}
                 for i in range(30)]
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": decisions})
    payload = store.session_start_payload(tmp_repo)
    assert payload["status"].endswith(" | team: 30 synced (25 shown)")


def test_session_start_payload_status_suffix_exact_cap_no_shown_note(tmp_repo):
    # Non-architecture type: this test is about the display cap, not deferral (see
    # test_session_start_payload_status_suffix_notes_deferred_architecture below).
    store.update_decision(tmp_repo, "local constraint never log secrets", "s1", subtype="constraint")
    decisions = [{"id": f"t{i}", "type": "constraint", "content": f"rule {i}",
                  "rationale": None, "repo": None, "agent": None, "scope": "team"}
                 for i in range(25)]
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": decisions})
    payload = store.session_start_payload(tmp_repo)
    assert payload["status"].endswith(" | team: 25 synced")
    assert "shown" not in payload["status"]


def test_session_start_payload_status_suffix_notes_deferred_architecture(tmp_repo):
    # An architecture row deferred to the count-only pointer contributes zero rows of
    # actual content to `context`, so the status suffix's "N synced" alone would overstate
    # what the model received - it must also subtract deferred rows via `(M shown)`,
    # exactly like it already does for rows truncated by the display cap.
    store.update_decision(tmp_repo, "local constraint never log secrets", "s1", subtype="constraint")
    decisions = [
        {"id": "arch1", "type": "architecture", "content": "Team picked Postgres over MySQL",
         "rationale": None, "repo": None, "agent": None, "scope": "team"},
        {"id": "cons1", "type": "constraint", "content": "Team requires 2 reviewers",
         "rationale": None, "repo": None, "agent": None, "scope": "team"},
    ]
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": decisions})
    payload = store.session_start_payload(tmp_repo)
    assert payload["status"].endswith(" | team: 2 synced (1 shown)")


def test_session_start_payload_status_suffix_ignores_ratified_architecture(tmp_repo):
    # An architecture row that collapses to a local-ratification pointer (>= 0.7 overlap)
    # is NOT deferred by format_team_section, so it must still count toward "shown".
    local8 = "alpha beta gamma delta epsilon zeta eta theta"
    store.update_decision(tmp_repo, local8, "s1", subtype="architecture")
    decisions = [{"id": "teamaaaa", "type": "architecture", "content": local8,
                  "rationale": None, "repo": None, "agent": None, "scope": "team"}]
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": decisions})
    payload = store.session_start_payload(tmp_repo)
    assert payload["status"].endswith(" | team: 1 synced")
    assert "shown" not in payload["status"]


def test_session_start_payload_status_suffix_combines_cap_and_deferral(tmp_repo):
    # Deferral removes rows BEFORE the display cap is applied (format_team_section splits
    # deferred rows out, then slices [:_TEAM_DISPLAY] on what remains) - the status suffix's
    # "shown" math must subtract deferred rows before capping too, not after, or it
    # under/over-counts whenever both the cap and deferral bind on the same cache.
    store.update_decision(tmp_repo, "local constraint never log secrets", "s1", subtype="constraint")
    decisions = [
        {"id": "arch1", "type": "architecture", "content": "Team picked Postgres over MySQL",
         "rationale": None, "repo": None, "agent": None, "scope": "team"},
        {"id": "arch2", "type": "architecture", "content": "Team picked gRPC over REST",
         "rationale": None, "repo": None, "agent": None, "scope": "team"},
    ] + [
        {"id": f"cons{i}", "type": "constraint", "content": f"rule {i}",
         "rationale": None, "repo": None, "agent": None, "scope": "team"}
        for i in range(28)
    ]
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": decisions})
    payload = store.session_start_payload(tmp_repo)
    # 30 synced, 2 deferred (architecture, no overlap) -> 28 eligible, capped at 25 shown.
    # The naive (wrong) order -- min(30, 25) - 2 = 23 -- must NOT be what's reported.
    assert payload["status"].endswith(" | team: 30 synced (25 shown)")


def test_session_start_payload_status_suffix_shown_never_negative(tmp_repo):
    # All 30 rows are non-ratifying architecture (deferred=30, cap=25). The naive order
    # -- min(30, 25) - 30 = -5 -- would surface a nonsensical negative count.
    decisions = [
        {"id": f"arch{i}", "type": "architecture", "content": f"architecture rule {i}",
         "rationale": None, "repo": None, "agent": None, "scope": "team"}
        for i in range(30)
    ]
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": decisions})
    payload = store.session_start_payload(tmp_repo)
    assert payload["status"].endswith(" | team: 30 synced (0 shown)")


def test_session_start_payload_status_suffix_reads_one_cache_snapshot(tmp_repo, monkeypatch):
    # Before session_team_section existed, the text/count/deferred-count each reloaded the
    # team cache independently (format_team_section, the raw count, and
    # count_deferred_architecture) - a concurrent refresh landing between those reads could
    # desync the status suffix from what `context` actually rendered. Assert the cache is
    # only ever loaded twice for the whole call: once for the shared snapshot
    # (session_team_section) and once more inside _record_render's own documented fresh
    # reload-before-write (which must stay independent - see its docstring) - never once per
    # metric.
    decisions = [
        {"id": f"arch{i}", "type": "architecture", "content": f"architecture rule {i}",
         "rationale": None, "repo": None, "agent": None, "scope": "team"}
        for i in range(5)
    ]
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": decisions})

    calls = []
    real_load_cache = team_context._load_cache

    def counting_load_cache(repo_path):
        calls.append(repo_path)
        return real_load_cache(repo_path)

    monkeypatch.setattr(team_context, "_load_cache", counting_load_cache)
    payload = store.session_start_payload(tmp_repo)
    assert "| team: 5 synced" in payload["status"]
    assert len(calls) == 2


def test_session_start_payload_fresh_clone_shows_team(tmp_repo):
    # No local decisions, but a team cache exists — a fresh clone should still see team.
    # Non-architecture type: architecture is deferred at SessionStart (see below).
    _seed_team(tmp_repo, "Team rule survives fresh clone", rtype="constraint")
    ctx = store.session_start_payload(tmp_repo)["context"]
    assert "## Team context" in ctx
    assert "Team rule survives fresh clone" in ctx


def test_get_session_start_context_envelope_includes_team(tmp_repo):
    # Every adapter renders team at session start through this ONE builder.
    # Non-architecture type: architecture is deferred at SessionStart (see below).
    _seed_team(tmp_repo, "Team via Claude envelope", rtype="constraint")
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
    # Non-architecture type: architecture is deferred at SessionStart (see below).
    _seed_team(tmp_repo, "Team rule on fresh resume", rtype="constraint")
    ctx = store.session_start_payload(tmp_repo, source="resume")["context"]
    assert "## Team context" in ctx
    assert "Team rule on fresh resume" in ctx


def test_session_start_payload_defers_team_architecture_shows_rest(tmp_repo):
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": [
        {"id": "arch0001", "type": "architecture", "content": "Team picked Postgres over MySQL",
         "rationale": None, "repo": None, "agent": None, "scope": "team"},
        {"id": "cons0001", "type": "constraint", "content": "Team requires 2 reviewers",
         "rationale": None, "repo": None, "agent": None, "scope": "team"},
    ]})
    ctx = store.session_start_payload(tmp_repo)["context"]
    assert "Team requires 2 reviewers" in ctx
    assert "Team picked Postgres over MySQL" not in ctx
    assert "1 team architecture decision" in ctx
    assert 'get_context(entry_type="architecture")' in ctx


def test_get_context_entry_type_architecture_bypasses_team_deferral(tmp_repo):
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": [
        {"id": "arch0002", "type": "architecture", "content": "Team picked Postgres over MySQL",
         "rationale": None, "repo": None, "agent": None, "scope": "team"},
        {"id": "cons0002", "type": "constraint", "content": "Team requires 2 reviewers",
         "rationale": None, "repo": None, "agent": None, "scope": "team"},
    ]})
    result = store.get_context(tmp_repo, entry_type="architecture")
    assert "Team picked Postgres over MySQL" in result


def test_get_context_entry_type_architecture_limit_overrides_team_cap(tmp_repo):
    # 30 non-ratifying architecture rows > _TEAM_DISPLAY (25). This is the exact call the
    # SessionStart deferred-count pointer tells the model to make ("Call
    # get_context(entry_type=\"architecture\") for full content") - it must be able to
    # actually reach every row via limit=, not silently truncate at the generic team cap.
    decisions = [{"id": f"arch{i:05d}", "type": "architecture",
                  "content": f"distinct decision number {i}", "rationale": None,
                  "repo": None, "agent": None, "scope": "team"} for i in range(30)]
    team_context._save_cache(tmp_repo, {"repo_key": "k", "cursor": None, "decisions": decisions})

    default = store.get_context(tmp_repo, entry_type="architecture")
    default_rows = [ln for ln in default.splitlines() if ln.startswith("- [scope=team]")]
    assert len(default_rows) == team_context._TEAM_DISPLAY
    assert "showing 25 of 30 team rows" in default

    full = store.get_context(tmp_repo, entry_type="architecture", limit=30)
    full_rows = [ln for ln in full.splitlines() if ln.startswith("- [scope=team]")]
    assert len(full_rows) == 30
    assert "showing" not in full


# ── Option A seam: neutral refresh / poll_for_injection ──────────────────────────

def test_refresh_delegates_to_pull(monkeypatch):
    monkeypatch.setattr(store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(team_context, "pull", lambda repo, **kw: (2, 1))
    monkeypatch.setattr(team_context.share, "drain_outbox", lambda: 0)
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


def test_refresh_drains_outbox_after_pull(monkeypatch):
    """refresh() is the SessionStart seam every adapter funnels through, so it must
    trigger the outbox drain (fail-soft) after a successful pull."""
    monkeypatch.setattr(store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(team_context, "pull", lambda repo, **kw: (1, 0))
    calls = []
    monkeypatch.setattr(team_context.share, "drain_outbox", lambda: calls.append(1))
    assert team_context.refresh("/x") == (1, 0)
    assert calls == [1]


def test_refresh_drain_failure_is_fail_soft(monkeypatch):
    monkeypatch.setattr(store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(team_context, "pull", lambda repo, **kw: (3, 2))

    def boom():
        raise RuntimeError("drain boom")

    monkeypatch.setattr(team_context.share, "drain_outbox", boom)
    assert team_context.refresh("/x") == (3, 2)  # pull's result still returned


def test_poll_for_injection_delegates(monkeypatch):
    monkeypatch.setattr(store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(team_context, "poll_nonblocking",
                        lambda repo, consumer="claude": [{"id": "t1", "content": "c"}])
    assert team_context.poll_for_injection("/x") == [{"id": "t1", "content": "c"}]


def test_poll_for_injection_threads_consumer(monkeypatch):
    seen = {}
    monkeypatch.setattr(store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(team_context, "poll_nonblocking",
                        lambda repo, consumer="claude": seen.setdefault("consumer", consumer) or [])
    team_context.poll_for_injection("/x", "codex")
    assert seen["consumer"] == "codex"


def test_poll_for_injection_empty_repo_is_noop(monkeypatch):
    monkeypatch.setattr(store, "_resolve_repo", lambda p: "")
    assert team_context.poll_for_injection("/x") == []


def test_poll_for_injection_never_raises(monkeypatch):
    monkeypatch.setattr(store, "_resolve_repo", lambda p: "/repo")

    def boom(repo, consumer="claude"):
        raise RuntimeError("boom")

    monkeypatch.setattr(team_context, "poll_nonblocking", boom)
    assert team_context.poll_for_injection("/x") == []


# ── No double-inject: session start renders once, poll injects deltas only ────────

def test_team_poll_empty_when_no_new(monkeypatch):
    from contexer.adapters import claude
    monkeypatch.setattr(team_context, "poll_for_injection", lambda rp, consumer="claude": [])
    assert claude.team_poll("/repo", "{}") == "{}"


def test_team_poll_injects_new_rows(monkeypatch):
    from contexer.adapters import claude
    monkeypatch.setattr(team_context, "poll_for_injection",
                        lambda rp, consumer="claude": [{"id": "t1", "content": "New team rule", "type": "constraint"}])
    out = claude.team_poll("/repo", "{}")
    assert "New team rule" in out
    assert "just approved" in out.lower()


def test_team_poll_threads_consumer(monkeypatch):
    from contexer.adapters import claude
    seen = {}
    monkeypatch.setattr(team_context, "poll_for_injection",
                        lambda rp, consumer="claude": seen.setdefault("consumer", consumer) or [])
    claude.team_poll("/repo", "{}", "codex")
    assert seen["consumer"] == "codex"


def test_team_poll_defers_architecture_shows_rest(monkeypatch):
    from contexer.adapters import claude
    monkeypatch.setattr(team_context, "poll_for_injection", lambda rp, consumer="claude": [
        {"id": "a1", "content": "Team picked Postgres over MySQL", "type": "architecture"},
        {"id": "c1", "content": "New team rule", "type": "constraint"},
    ])
    out = claude.team_poll("/repo", "{}")
    assert "New team rule" in out
    assert "Team picked Postgres over MySQL" not in out
    assert "1 team architecture decision" in out
    assert "get_context(entry_type=" in out and "architecture" in out


def test_team_poll_all_architecture_still_injects_deferred_line(monkeypatch):
    from contexer.adapters import claude
    monkeypatch.setattr(team_context, "poll_for_injection", lambda rp, consumer="claude": [
        {"id": "a1", "content": "Team picked Postgres over MySQL", "type": "architecture"},
    ])
    out = claude.team_poll("/repo", "{}")
    assert out != "{}"
    assert "Team picked Postgres over MySQL" not in out
    assert "1 team architecture decision" in out
