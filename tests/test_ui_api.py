"""Tests for the console's JSON API, over a real server on an ephemeral loopback port.

The table-driven contract tests are the point of this file: the frontend
(`contexer/ui/assets/console.js`) is written against these exact field names, so a rename
here would silently blank a pane there rather than fail loudly.
"""
import http.client
import json
import re
import subprocess
import threading
import time
from pathlib import Path

import pytest

from contexer import config, store, team_context
from contexer.ui import api, daemon, server

TOKEN = "console-token-for-tests"

SUMMARY_KEYS = {"id", "title", "content", "subtype", "status", "created_by", "timestamp",
                "updated_at", "revision", "occurrence_count", "confidence", "has_proposal",
                "source_files"}
PROPOSED_KEYS = {"content", "title", "subtype", "source", "created_at", "confidence",
                 "confidence_factors"}
PROPOSAL_KEYS = {"id", "title", "subtype", "status", "revision", "current", "proposed"}
STALENESS_KEYS = {"last_ok_at", "age_seconds", "stale"}
STORE_ROW_KEYS = {"slug", "repo_path", "name", "decisions", "pending", "tombstoned", "mtime",
                  "is_current", "ok", "error"}
REVISION_KEYS = {"version_number", "content", "title", "source", "created_at", "approved_at",
                 "confidence_score", "is_current"}


@pytest.fixture
def console(tmp_path, monkeypatch):
    """A console daemon on an ephemeral port over an isolated ~/.contexer. Never the real one."""
    home = tmp_path / ".contexer"
    home.mkdir()
    monkeypatch.setattr(store, "STORE_DIR", home)
    monkeypatch.setattr(config, "CONFIG_PATH", home / "config.toml")
    monkeypatch.setattr(daemon, "STATE_PATH", home / "ui.json")
    monkeypatch.setattr(daemon, "LOG_PATH", home / "ui.log")
    srv = server.ConsoleServer(0, TOKEN)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.01},
                              daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        thread.join(timeout=5)
        srv.server_close()


@pytest.fixture
def repo(console, tmp_path):
    """A seeded store: one approved decision carrying a Suggested Update, one pending, one
    plain approved. Returns the slug and the ids the tests address."""
    path = str(tmp_path / "widgets")
    _ok, approved = store.update_decision(
        path, "Use Postgres for the decision store, not SQLite", "s1",
        subtype="architecture", created_by="human")
    _ok, pending = store.update_decision(
        path, "Never ship a migration without a rollback plan", "s1", subtype="constraint")
    _ok, plain = store.update_decision(
        path, "Name test files test_<module>.py", "s1",
        subtype="convention", created_by="human")
    # A near-duplicate of an ai-authored architecture change against an approved decision is
    # what attaches a proposed_revision; without replace_id it would only bump recurrence.
    store.update_decision(path, "Use Postgres for the decision store, not MySQL", "s2",
                          subtype="architecture", replace_id=approved[:8])
    detail = store.get_decision_detail(path, approved)
    assert detail["proposed_revision"] is not None, "fixture failed to attach a proposal"
    assert store.get_decision_detail(path, pending)["status"] == "pending_approval"
    return {"path": path, "slug": store.repo_slug(path), "approved": approved,
            "pending": pending, "plain": plain}


class Reply:
    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self.body = body

    @property
    def data(self):
        return json.loads(self.body) if self.body else None


def call(srv, method, path, *, body=None, raw=None, cookie=None, token=None, origin=None,
         poll=False, extra=None):
    headers = {"Host": f"127.0.0.1:{srv.port}"}
    if cookie is not None:
        headers["Cookie"] = f"ctx_ui={cookie}"
    if token is not None:
        headers["X-Contexer-Token"] = token
    if origin is not None:
        headers["Origin"] = origin
    if poll:
        headers["X-Contexer-Poll"] = "1"
    headers.update(extra or {})
    payload = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    if payload is not None:
        headers["Content-Type"] = "application/json"
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    try:
        conn.request(method, path, body=payload, headers=headers)
        res = conn.getresponse()
        return Reply(res.status, dict(res.getheaders()), res.read())
    finally:
        conn.close()


def read(srv, path, **kwargs):
    kwargs.setdefault("token", srv.token)
    return call(srv, "GET", path, **kwargs)


def write(srv, method, path, **kwargs):
    kwargs.setdefault("cookie", srv.token)
    kwargs.setdefault("token", srv.csrf)
    kwargs.setdefault("origin", f"http://127.0.0.1:{srv.port}")
    return call(srv, method, path, **kwargs)


def ok(srv, path, **kwargs):
    reply = read(srv, path, **kwargs)
    assert reply.status == 200, (path, reply.status, reply.body)
    return reply.data


# --- the contract ----------------------------------------------------------------------

def _contract(slug: str, entry_id: str) -> dict:
    """Every key `console.js` reads off a GET response, per api-contract.md."""
    return {
        "/healthz": {"ok", "version", "pid", "started_at", "csrf", "port"},
        f"/api/store/{slug}": {"slug", "repo_path", "name", "is_current", "ok", "error",
                               "mtime", "counts", "tombstones", "subtype_mix", "status_mix",
                               "recent", "pending", "proposals", "staleness", "health"},
        f"/api/store/{slug}/decisions": {"total", "limit", "offset", "ok", "error",
                                         "decisions"},
        f"/api/store/{slug}/decisions/{entry_id}": {
            "id", "title", "content", "subtype", "status", "created_by", "timestamp",
            "updated_at", "revision", "occurrence_count", "session_count", "memory_key",
            "approved_at", "approved_by", "rationale", "confidence", "revisions",
            "proposed_revision", "share", "source_files"},
        f"/api/store/{slug}/deleted": {"ok", "error", "tombstones"},
        "/api/global": {"ok", "error", "rules"},
        f"/api/team/{slug}": {"slug", "repo_key", "mode", "enabled", "counts", "staleness",
                              "last_sync", "decisions", "shareable"},
        "/api/config": {"ui", "profile", "login", "version", "store_dir", "config_path",
                        "stores"},
    }


def test_every_get_endpoint_carries_every_contracted_key(console, repo):
    for path, keys in _contract(repo["slug"], repo["approved"]).items():
        payload = ok(console, path)
        missing = keys - set(payload)
        assert not missing, f"{path} is missing {sorted(missing)}"


def test_the_nested_contract_shapes_are_complete(console, repo):
    dashboard = ok(console, f"/api/store/{repo['slug']}")
    assert {"decisions", "pending", "proposed_updates", "global", "team",
            "tombstoned"} <= set(dashboard["counts"])
    assert {"ok", "error", "count"} <= set(dashboard["tombstones"])
    assert STALENESS_KEYS <= set(dashboard["staleness"])
    assert {"ok", "error"} <= set(dashboard["health"])
    assert {"subtype", "count"} <= set(dashboard["subtype_mix"][0])
    assert {"status", "count"} <= set(dashboard["status_mix"][0])
    assert SUMMARY_KEYS <= set(dashboard["recent"][0])
    assert SUMMARY_KEYS | {"confidence_factors"} <= set(dashboard["pending"][0])

    proposal = dashboard["proposals"][0]
    assert PROPOSAL_KEYS <= set(proposal)
    assert {"content", "title", "version_number"} <= set(proposal["current"])
    assert PROPOSED_KEYS <= set(proposal["proposed"])

    listing = ok(console, f"/api/store/{repo['slug']}/decisions")
    assert SUMMARY_KEYS <= set(listing["decisions"][0])

    detail = ok(console, f"/api/store/{repo['slug']}/decisions/{repo['approved']}")
    assert {"score", "factors"} <= set(detail["confidence"])
    assert REVISION_KEYS <= set(detail["revisions"][0])
    assert PROPOSED_KEYS <= set(detail["proposed_revision"])
    assert {"shared", "shared_at", "endpoint", "queued"} <= set(detail["share"])

    rows = ok(console, "/api/stores")
    assert STORE_ROW_KEYS <= set(rows[0])

    team = ok(console, f"/api/team/{repo['slug']}")
    assert {"decisions"} <= set(team["counts"])
    assert {"at", "ok", "duration_ms", "consecutive_failures", "upserted", "removed",
            "error"} <= set(team["last_sync"])
    assert {"id", "title", "content", "subtype", "status", "confidence", "shared",
            "shared_at", "redacted"} <= set(team["shareable"][0])

    settings = ok(console, "/api/config")
    assert {"autostart", "port", "idle_timeout_minutes"} <= set(settings["ui"])
    assert {"mode", "endpoint", "redact_secrets", "skip_confirm",
            "token_set"} <= set(settings["profile"])
    assert {"logged_in", "issuer", "expires_at", "scope"} <= set(settings["login"])


def test_global_rules_carry_their_contracted_keys(console):
    write(console, "POST", "/api/global",
          body={"content": "Never commit generated files", "subtype": "constraint"})
    rule = ok(console, "/api/global")["rules"][0]
    assert {"id", "title", "content", "subtype", "created_by", "timestamp", "updated_at",
            "revision", "confidence"} <= set(rule)


def test_tombstones_carry_their_contracted_keys(console, repo):
    write(console, "DELETE", f"/api/store/{repo['slug']}/decisions/{repo['plain']}")
    tombstone = ok(console, f"/api/store/{repo['slug']}/deleted")["tombstones"][0]
    assert SUMMARY_KEYS | {"deleted_at", "deleted_by"} <= set(tombstone)


# --- the assets' side of the contract --------------------------------------------------

ASSETS = Path(api.__file__).parent / "assets"


def _badge_keys() -> set:
    """The keys `console.js`'s badges map actually writes."""
    block = ASSETS.joinpath("console.js").read_text().split("const badges = {", 1)[1]
    return set(re.findall(r"^\s+(\w+):", block.split("};", 1)[0], re.MULTILINE))


def test_every_nav_badge_in_the_markup_is_one_the_console_fills():
    """A `data-count` the badges map never writes is dead markup: an element that can only
    ever stay hidden, telling the next reader that a count exists."""
    markup = ASSETS.joinpath("index.html").read_text()
    assert _badge_keys() == {"decisions", "pending", "deleted", "global", "team"}
    assert set(re.findall(r'data-count="([^"]+)"', markup)) == _badge_keys()


def test_everything_toggled_through_the_hidden_attribute_is_actually_hideable():
    """The console shipped with a permanently visible "Disconnected from the Contexer daemon."
    banner over a dashboard that was polling successfully every 10 seconds.

    `hidden` is enforced only by the UA stylesheet, and ANY author `display` rule outranks a UA
    rule whatever its specificity. `.banner` and `.toast` are `display: flex` components toggled
    exclusively through `element.hidden`, so they were painted from first load and no amount of
    `setDisconnected(false)` could put them away - what the user read was the placeholder text
    sitting in index.html. Only a real browser shows this: a DOM shim sees `hidden === true` and
    reports success, which is exactly what happened during development. So the guard lives here,
    on the stylesheet, where it can be checked without one."""
    css = ASSETS.joinpath("console.css").read_text()
    markup = ASSETS.joinpath("index.html").read_text()
    script = ASSETS.joinpath("console.js").read_text()

    override = re.search(r"\[hidden\]\s*\{[^}]*display:\s*none\s*!important", css)
    assert override, "console.css must neutralise `display` for [hidden] elements"

    # Every element the script hides must carry the attribute in the markup, and every element
    # that carries it must be one the script controls - otherwise it is hidden forever.
    toggled = set(re.findall(r"\b(\w+)\.hidden\s*=", script))
    assert {"banner", "toastEl"} <= toggled, toggled
    hidden_ids = set(re.findall(r'id="([^"]+)"(?=[^>]*\shidden(?:\s|>))', markup))
    assert {"banner", "toast"} <= hidden_ids, hidden_ids


def test_the_consoles_page_size_is_inside_the_apis_paging_bound():
    """The list view asks for one fixed page; a limit over MAX_LIMIT would 400 every load."""
    script = ASSETS.joinpath("console.js").read_text()
    limit = int(re.search(r"const PAGE_LIMIT = (\d+);", script).group(1))
    assert 0 < limit <= api.MAX_LIMIT


# --- healthz ---------------------------------------------------------------------------

def test_healthz_reports_this_process_and_the_in_memory_csrf(console):
    import os

    payload = ok(console, "/healthz")
    assert payload["ok"] is True
    assert payload["pid"] == os.getpid()
    assert payload["port"] == console.port
    assert payload["csrf"] == console.csrf and payload["csrf"] != console.token


# --- /api/stores -----------------------------------------------------------------------

def test_stores_lists_one_row_per_repo_store(console, repo, tmp_path):
    other = str(tmp_path / "gadgets")
    store.update_decision(other, "Ship the CLI as a single wheel", "s1", created_by="human")
    rows = ok(console, "/api/stores")
    assert {r["repo_path"] for r in rows} == {repo["path"], other}
    row = next(r for r in rows if r["repo_path"] == repo["path"])
    assert row["slug"] == repo["slug"] and row["name"] == "widgets"
    assert row["decisions"] == 3 and row["pending"] == 2  # one pending + one proposed update
    assert row["ok"] is True and row["error"] is None and row["mtime"] > 0


def test_stores_marks_the_current_repo(console, repo):
    (store.STORE_DIR / ".current_repo").write_text(repo["path"])
    rows = ok(console, "/api/stores")
    assert [r["is_current"] for r in rows if r["repo_path"] == repo["path"]] == [True]


def test_stores_ignores_the_sidecars_that_share_the_directory(console, repo):
    write(console, "DELETE", f"/api/store/{repo['slug']}/decisions/{repo['plain']}")
    store.update_global_decision("Never log secrets", "s1", "constraint")
    daemon.write_state(daemon.UiState(pid=1, port=1, token="t", started_at="x", version="y"))
    assert [r["slug"] for r in ok(console, "/api/stores")] == [repo["slug"]]


# --- dashboard -------------------------------------------------------------------------

def test_the_dashboard_counts_split_pending_from_proposed_updates(console, repo):
    store.update_global_decision("Never log secrets", "s1", "constraint")
    counts = ok(console, f"/api/store/{repo['slug']}")["counts"]
    assert counts == {"decisions": 3, "pending": 1, "proposed_updates": 1, "global": 1,
                      "team": 0, "tombstoned": 0}


def test_the_dashboard_mixes_add_up_to_the_decision_count(console, repo):
    dashboard = ok(console, f"/api/store/{repo['slug']}")
    assert sum(r["count"] for r in dashboard["subtype_mix"]) == 3
    assert sum(r["count"] for r in dashboard["status_mix"]) == 3
    assert {r["subtype"] for r in dashboard["subtype_mix"]} == {"architecture", "constraint",
                                                               "convention"}


def test_the_dashboard_lists_what_needs_review(console, repo):
    dashboard = ok(console, f"/api/store/{repo['slug']}")
    assert [d["id"] for d in dashboard["pending"]] == [repo["pending"]]
    assert [p["id"] for p in dashboard["proposals"]] == [repo["approved"]]
    assert dashboard["proposals"][0]["proposed"]["content"].endswith("not MySQL")


def test_an_unknown_slug_is_a_404(console):
    assert read(console, "/api/store/does-not-exist").status == 404
    assert read(console, "/api/store/does-not-exist/decisions").status == 404
    assert read(console, "/api/team/does-not-exist").status == 404
    assert read(console, "/api/store/does-not-exist/sessions").status == 404
    assert read(console, "/api/store/does-not-exist/sessions/s1").status == 404


# --- decision list ---------------------------------------------------------------------

def test_the_decision_list_is_newest_change_first(console, repo):
    listing = ok(console, f"/api/store/{repo['slug']}/decisions")
    assert listing["total"] == 3 and len(listing["decisions"]) == 3
    stamps = [d["updated_at"] for d in listing["decisions"]]
    assert stamps == sorted(stamps, reverse=True)


@pytest.mark.parametrize("query,expected", [
    ("?q=postgres", 1),
    ("?q=POSTGRES", 1),
    ("?q=nothing-matches-this", 0),
    ("?subtype=architecture", 1),
    ("?subtype=convention", 1),
    ("?status=pending_approval", 1),
    ("?status=approved", 2),
    ("?subtype=architecture&status=pending_approval", 0),
])
def test_the_decision_list_filters(console, repo, query, expected):
    listing = ok(console, f"/api/store/{repo['slug']}/decisions{query}")
    assert listing["total"] == expected == len(listing["decisions"])


def test_the_decision_list_pages(console, repo):
    page = ok(console, f"/api/store/{repo['slug']}/decisions?limit=2&offset=1")
    assert page["total"] == 3 and page["limit"] == 2 and page["offset"] == 1
    assert len(page["decisions"]) == 2


@pytest.mark.parametrize("query", ["?limit=abc", "?offset=-1", "?limit=100000"])
def test_a_bad_paging_parameter_is_a_400(console, repo, query):
    assert read(console, f"/api/store/{repo['slug']}/decisions{query}").status == 400


# --- file filter (Task 4 of #174) -------------------------------------------------------

@pytest.fixture
def files_repo(console, tmp_path):
    """One decision anchored to a file, one not - for the file= filter tests below."""
    path = str(tmp_path / "files-widgets")
    _ok, jwt = store.update_decision(
        path, "Decided to use JWT for stateless auth tokens", "s1",
        subtype="architecture", created_by="human", source_files=["auth/jwt.py"])
    _ok, plain = store.update_decision(
        path, "Use pytest for unit tests", "s1", subtype="convention", created_by="human")
    return {"path": path, "slug": store.repo_slug(path), "jwt": jwt, "plain": plain}


def test_the_file_filter_matches(console, files_repo):
    listing = ok(console, f"/api/store/{files_repo['slug']}/decisions?file=auth/jwt.py")
    assert listing["total"] == 1
    assert listing["decisions"][0]["id"] == files_repo["jwt"]
    assert listing["decisions"][0]["source_files"] == ["auth/jwt.py"]


def test_the_file_filter_excludes_non_matching(console, files_repo):
    listing = ok(console, f"/api/store/{files_repo['slug']}/decisions?file=unrelated/file.py")
    assert listing["total"] == 0
    assert listing["decisions"] == []
    assert listing["ok"] is True


def test_an_unknown_file_is_an_empty_result_not_an_error(console, files_repo):
    reply = read(console, f"/api/store/{files_repo['slug']}/decisions?file=nope.py")
    assert reply.status == 200
    assert reply.data["total"] == 0


def test_the_file_filter_accepts_a_repeated_param(console, files_repo):
    listing = ok(console, f"/api/store/{files_repo['slug']}/decisions"
                          "?file=unrelated/file.py&file=auth/jwt.py")
    assert listing["total"] == 1


def test_the_file_filter_accepts_a_comma_separated_value(console, files_repo):
    listing = ok(console, f"/api/store/{files_repo['slug']}/decisions"
                          "?file=unrelated/file.py,auth/jwt.py")
    assert listing["total"] == 1


def test_the_file_filter_combines_with_subtype(console, files_repo):
    listing = ok(console, f"/api/store/{files_repo['slug']}/decisions"
                          "?file=auth/jwt.py&subtype=convention")
    assert listing["total"] == 0


@pytest.mark.parametrize("value", ["../../etc/passwd", "/etc/passwd", "..%2f..%2fetc%2fpasswd"])
def test_the_file_filter_drops_escape_shaped_values_without_error(console, files_repo, value):
    reply = read(console, f"/api/store/{files_repo['slug']}/decisions?file={value}")
    assert reply.status == 200
    assert reply.data["total"] == 0


def test_too_many_file_values_is_a_400(console, files_repo):
    query = "&".join(f"file=f{i}.py" for i in range(api.MAX_FILES + 1))
    assert read(console, f"/api/store/{files_repo['slug']}/decisions?{query}").status == 400


def test_an_overlong_file_value_is_a_400(console, files_repo):
    value = "a" * (api.MAX_FILE_LEN + 1)
    reply = read(console, f"/api/store/{files_repo['slug']}/decisions?file={value}")
    assert reply.status == 400


# --- sessions (issue #256) --------------------------------------------------------------

def test_sessions_route_lists_one_row_per_originating_session(console, repo):
    result = ok(console, f"/api/store/{repo['slug']}/sessions")
    assert set(result) == {"sessions", "memory_import_count", "total_decisions"}
    assert result["total_decisions"] == 3
    assert result["memory_import_count"] == 0
    assert [r["session_id"] for r in result["sessions"]] == ["s1"]
    assert result["sessions"][0]["count"] == 3
    # `pending` plus the `approved` decision's genuine (non-bookkeeping) proposed_revision.
    assert result["sessions"][0]["open_count"] == 2


def test_session_transcript_route_returns_the_originating_entries(console, repo):
    transcript = ok(console, f"/api/store/{repo['slug']}/sessions/s1")
    assert transcript["session_id"] == "s1"
    assert transcript["count"] == 3
    assert {e["id"] for e in transcript["entries"]} == {
        repo["approved"], repo["pending"], repo["plain"]}
    assert {e["id"] for e in transcript["open"]} == {repo["approved"], repo["pending"]}
    conflicted = next(e for e in transcript["entries"] if e["id"] == repo["approved"])
    assert conflicted["open_conflict"] is True and conflicted["pending"] is False
    pending = next(e for e in transcript["entries"] if e["id"] == repo["pending"])
    assert pending["pending"] is True and pending["open_conflict"] is False


def test_session_transcript_route_resolves_a_short_id(console, tmp_path):
    path = str(tmp_path / "sessions-widgets")
    sid = "abcdef12-3456-7890-aaaa-bbbbbbbbbbbb"
    _ok, eid = store.update_decision(path, "Use uv for dependency management", sid,
                                     subtype="convention", created_by="human")
    slug = store.repo_slug(path)

    full = ok(console, f"/api/store/{slug}/sessions/{sid}")
    short = ok(console, f"/api/store/{slug}/sessions/{sid[:8]}")
    assert full["session_id"] == short["session_id"] == sid
    assert full["entries"][0]["id"] == eid


def test_session_transcript_route_null_bucket_via_none(console, tmp_path):
    path = str(tmp_path / "legacy-widgets")
    data = store.load(path)
    entry = store._new_decision_entry("Legacy decision with no session id", "s-legacy",
                                      "convention", created_by="human", status="approved")
    del entry["session_id"]
    data["entries"].append(entry)
    store.save(path, data)
    slug = store.repo_slug(path)

    transcript = ok(console, f"/api/store/{slug}/sessions/none")
    assert transcript["session_id"] is None
    assert transcript["entries"][0]["id"] == entry["id"]


def test_session_transcript_route_unknown_session_is_a_404(console, repo):
    assert read(console, f"/api/store/{repo['slug']}/sessions/not-a-real-session").status == 404


def test_session_transcript_route_memory_sync_literal_is_a_404(console, tmp_path):
    path = str(tmp_path / "memory-widgets")
    data = store.load(path)
    entry = store._new_decision_entry("Imported fact from the memory tool", "memory-sync",
                                      "convention", created_by="memory", status="approved",
                                      memory_key="claude-memory:foo.md#S")
    data["entries"].append(entry)
    store.save(path, data)
    slug = store.repo_slug(path)
    assert read(console, f"/api/store/{slug}/sessions/memory-sync").status == 404


def test_sessions_routes_only_accept_get(console, repo):
    assert write(console, "POST", f"/api/store/{repo['slug']}/sessions", body={}).status == 404
    assert write(console, "POST", f"/api/store/{repo['slug']}/sessions/s1",
                body={}).status == 404


# --- decision detail -------------------------------------------------------------------

def test_the_detail_resolves_a_full_id_and_an_eight_char_prefix(console, repo):
    full = ok(console, f"/api/store/{repo['slug']}/decisions/{repo['approved']}")
    short = ok(console, f"/api/store/{repo['slug']}/decisions/{repo['approved'][:8]}")
    assert full["id"] == short["id"] == repo["approved"]


def test_the_detail_exposes_the_revision_timeline_and_confidence(console, repo):
    detail = ok(console, f"/api/store/{repo['slug']}/decisions/{repo['approved']}")
    assert detail["revision"] == 1 and len(detail["revisions"]) == 1
    assert detail["revisions"][0]["is_current"] is True
    assert detail["revisions"][0]["source"] == "human"
    assert isinstance(detail["confidence"]["score"], int)
    assert detail["confidence"]["factors"]
    assert detail["share"] == {"shared": False, "shared_at": None, "endpoint": None,
                               "queued": False}


def test_the_detail_omits_internal_revision_ids(console, repo):
    body = read(console, f"/api/store/{repo['slug']}/decisions/{repo['approved']}").body
    assert b"revision_id" not in body and b"session_ids" not in body


def test_an_unknown_decision_id_is_a_404(console, repo):
    assert read(console, f"/api/store/{repo['slug']}/decisions/deadbeef").status == 404


# --- approve / reject ------------------------------------------------------------------

def test_approving_a_pending_decision_makes_it_trusted(console, repo):
    reply = write(console, "POST",
                  f"/api/store/{repo['slug']}/decisions/{repo['pending']}/approve",
                  body={"action": "approve"})
    assert reply.status == 200 and reply.data["message"]
    detail = ok(console, f"/api/store/{repo['slug']}/decisions/{repo['pending']}")
    assert detail["status"] == "approved" and detail["approved_by"] == "human"


def test_rejecting_maps_to_the_stores_ignore_state(console, repo):
    write(console, "POST", f"/api/store/{repo['slug']}/decisions/{repo['pending']}/approve",
          body={"action": "reject"})
    detail = ok(console, f"/api/store/{repo['slug']}/decisions/{repo['pending']}")
    assert detail["status"] == "ignored"


def test_approving_a_proposed_update_promotes_it_to_a_new_revision(console, repo):
    write(console, "POST", f"/api/store/{repo['slug']}/decisions/{repo['approved']}/approve",
          body={"action": "approve"})
    detail = ok(console, f"/api/store/{repo['slug']}/decisions/{repo['approved']}")
    assert detail["revision"] == 2 and detail["proposed_revision"] is None
    assert detail["content"].endswith("not MySQL")


def test_approving_an_already_approved_decision_is_a_400_not_a_404(console, repo):
    reply = write(console, "POST",
                  f"/api/store/{repo['slug']}/decisions/{repo['plain']}/approve",
                  body={"action": "approve"})
    assert reply.status == 400 and "already approved" in reply.data["error"]


def test_approving_an_unknown_decision_is_a_404(console, repo):
    reply = write(console, "POST", f"/api/store/{repo['slug']}/decisions/deadbeef/approve",
                  body={"action": "approve"})
    assert reply.status == 404


def test_an_unknown_approve_action_is_a_400(console, repo):
    reply = write(console, "POST",
                  f"/api/store/{repo['slug']}/decisions/{repo['pending']}/approve",
                  body={"action": "promote"})
    assert reply.status == 400


# --- edit ------------------------------------------------------------------------------

def test_an_edit_appends_a_revision_attributed_to_the_developer(console, repo):
    """A console edit is a developer retyping the decision, so the revision's provenance is
    `human` - matching what the Add form stores. It is deliberately not the console's own
    `SOURCE` ("ui"): `share._WIRE_SOURCES` is closed, so `_wire_source` would degrade "ui" to
    "ai" on push and the developer's edit would reach the cloud as AI-authored."""
    reply = write(console, "PATCH", f"/api/store/{repo['slug']}/decisions/{repo['plain']}",
                  body={"content": "Name test files test_<module>.py, one per module",
                        "title": "Test file naming", "if_version": 1})
    assert reply.status == 200
    detail = ok(console, f"/api/store/{repo['slug']}/decisions/{repo['plain']}")
    assert detail["revision"] == 2 and detail["title"] == "Test file naming"
    assert detail["revisions"][-1]["source"] == "human"
    assert detail["status"] == "approved"  # an edit to a trusted decision stays trusted


def test_a_stale_if_version_is_a_409_carrying_the_current_version(console, repo):
    reply = write(console, "PATCH", f"/api/store/{repo['slug']}/decisions/{repo['plain']}",
                  body={"content": "Something else entirely", "if_version": 99})
    assert reply.status == 409
    assert reply.data["error"] == store.EDIT_CONFLICT
    assert reply.data["current_version"] == 1


def test_an_edit_to_an_unknown_decision_is_a_404(console, repo):
    reply = write(console, "PATCH", f"/api/store/{repo['slug']}/decisions/deadbeef",
                  body={"content": "New text"})
    assert reply.status == 404


def test_a_decision_with_no_subtype_is_still_editable(console, repo):
    """Capture is permissive, so a legacy entry can carry subtype "". The console posts the
    field on every save, and rejecting "" made every such decision permanently uneditable."""
    _stored, legacy = store.update_decision(
        repo["path"], "Keep the wire format stable across minor releases", "s9",
        created_by="human")
    assert ok(console, f"/api/store/{repo['slug']}/decisions/{legacy}")["subtype"] == ""

    reply = write(console, "PATCH", f"/api/store/{repo['slug']}/decisions/{legacy}",
                  body={"content": "Keep the wire format stable across every minor release",
                        "title": "Stable wire format", "subtype": "", "if_version": 1})
    assert reply.status == 200
    detail = ok(console, f"/api/store/{repo['slug']}/decisions/{legacy}")
    assert detail["revision"] == 2 and detail["subtype"] == ""
    assert detail["title"] == "Stable wire format"


def test_an_edit_of_nothing_but_a_blank_subtype_is_still_a_400(console, repo):
    """"" means "leave the subtype alone", so a body carrying only that changes nothing."""
    reply = write(console, "PATCH", f"/api/store/{repo['slug']}/decisions/{repo['plain']}",
                  body={"subtype": ""})
    assert reply.status == 400 and "othing to change" in reply.data["error"]


def test_an_invalid_subtype_is_a_400(console, repo):
    reply = write(console, "PATCH", f"/api/store/{repo['slug']}/decisions/{repo['plain']}",
                  body={"subtype": "vibes"})
    assert reply.status == 400


def test_an_empty_edit_is_a_400(console, repo):
    reply = write(console, "PATCH", f"/api/store/{repo['slug']}/decisions/{repo['plain']}",
                  body={"if_version": 1})
    assert reply.status == 400


@pytest.mark.parametrize("field,length", [("title", api.MAX_TITLE), ("content", api.MAX_CONTENT)])
def test_text_over_the_character_cap_is_a_400(console, repo, field, length):
    at_cap = write(console, "PATCH", f"/api/store/{repo['slug']}/decisions/{repo['plain']}",
                   body={field: "x" * length})
    over_cap = write(console, "PATCH", f"/api/store/{repo['slug']}/decisions/{repo['plain']}",
                     body={field: "x" * (length + 1)})
    assert at_cap.status == 200 and over_cap.status == 400


@pytest.mark.parametrize("body", [{"if_version": "1"}, {"if_version": True},
                                  {"content": 42}, {"title": []}])
def test_a_wrongly_typed_field_is_a_400(console, repo, body):
    reply = write(console, "PATCH", f"/api/store/{repo['slug']}/decisions/{repo['plain']}",
                  body=body)
    assert reply.status == 400


# --- delete / restore ------------------------------------------------------------------

def test_delete_tombstones_and_restore_brings_it_back(console, repo):
    path = f"/api/store/{repo['slug']}/decisions/{repo['plain']}"
    assert write(console, "DELETE", path).status == 200
    assert read(console, path).status == 404
    tombstones = ok(console, f"/api/store/{repo['slug']}/deleted")["tombstones"]
    assert [t["id"] for t in tombstones] == [repo["plain"]]
    assert tombstones[0]["deleted_by"] == "ui" and tombstones[0]["deleted_at"]

    assert write(console, "POST", f"{path}/restore").status == 200
    assert read(console, path).status == 200
    assert ok(console, f"/api/store/{repo['slug']}/deleted")["tombstones"] == []


def test_deleting_an_unknown_decision_is_a_404(console, repo):
    assert write(console, "DELETE",
                 f"/api/store/{repo['slug']}/decisions/deadbeef").status == 404
    assert write(console, "POST",
                 f"/api/store/{repo['slug']}/decisions/deadbeef/restore").status == 404


def test_a_restore_refused_at_capacity_is_a_409_not_a_404(console, repo, monkeypatch):
    """The tombstone is still there and still restorable - reporting "not found" sent the
    developer looking for a decision the store had not lost."""
    path = f"/api/store/{repo['slug']}/decisions/{repo['plain']}"
    assert write(console, "DELETE", path).status == 200
    monkeypatch.setattr(store, "MAX_ENTRIES", 2)  # the two survivors already fill the store

    reply = write(console, "POST", f"{path}/restore")
    assert reply.status == 409 and "maximum" in reply.data["error"]
    tombstones = ok(console, f"/api/store/{repo['slug']}/deleted")["tombstones"]
    assert [t["id"] for t in tombstones] == [repo["plain"]], "the refusal kept the tombstone"


def test_restoring_a_leftover_tombstone_is_an_idempotent_success(console, repo):
    """A delete that crashed between its two writes leaves the entry in both files. Restoring
    then drops the stale tombstone instead of storing a second, unreachable copy."""
    graveyard = store.STORE_DIR / f"{repo['slug']}.deleted.json"
    entry = dict(store.get_decision_detail(repo["path"], repo["plain"]))
    graveyard.write_text(json.dumps({"repo_path": repo["path"], "entries": [
        {"id": repo["plain"], "type": "decision", "subtype": "convention",
         "content": entry["content"], "deleted_at": "2026-07-30T00:00:00+00:00",
         "deleted_by": "ui"}]}))

    reply = write(console, "POST",
                  f"/api/store/{repo['slug']}/decisions/{repo['plain']}/restore")
    assert reply.status == 200 and "already in the live store" in reply.data["message"]
    assert ok(console, f"/api/store/{repo['slug']}/deleted")["tombstones"] == []


def test_a_corrupt_tombstone_sidecar_reads_as_unreadable_not_empty(console, repo):
    """The Deleted view renders `ok: false` as "tombstones unreadable"; without the flag a
    sidecar that still holds tombstones would render as "nothing deleted"."""
    (store.STORE_DIR / f"{repo['slug']}.deleted.json").write_text("{not json at all")
    deleted = ok(console, f"/api/store/{repo['slug']}/deleted")
    assert deleted["ok"] is False and deleted["error"] and deleted["tombstones"] == []

    tombstones = ok(console, f"/api/store/{repo['slug']}")["tombstones"]
    assert tombstones["ok"] is False and tombstones["error"] and tombstones["count"] == 0

    # The store refuses to write over a sidecar it could not parse, and the console must
    # report that refusal rather than claim a delete that never happened.
    reply = write(console, "DELETE", f"/api/store/{repo['slug']}/decisions/{repo['plain']}")
    assert reply.status == 400 and "unreadable" in reply.data["error"]


# --- global ----------------------------------------------------------------------------

def test_a_global_rule_can_be_added_and_deleted(console):
    reply = write(console, "POST", "/api/global",
                  body={"content": "Never commit generated files", "subtype": "constraint",
                        "title": "No generated files"})
    assert reply.status == 200
    rules = ok(console, "/api/global")["rules"]
    assert [r["title"] for r in rules] == ["No generated files"]

    assert write(console, "DELETE", f"/api/global/{rules[0]['id']}").status == 200
    assert ok(console, "/api/global")["rules"] == []


def test_a_global_rule_added_from_the_console_is_attributed_to_the_developer(console):
    """The console's Add form is a human typing a rule - the only other caller of
    `update_global_decision` is the MCP tool, where the agent authors it. Defaulting both
    to `ai` rendered every hand-written rule as "by ai" and cost it the
    "Stated by developer" confidence factor."""
    write(console, "POST", "/api/global",
          body={"content": "Never commit generated files", "subtype": "constraint"})
    assert ok(console, "/api/global")["rules"][0]["created_by"] == "human"


def test_a_duplicate_global_rule_is_reported_not_stored(console):
    body = {"content": "Never commit generated files", "subtype": "constraint"}
    write(console, "POST", "/api/global", body=body)
    reply = write(console, "POST", "/api/global", body=body)
    assert reply.status == 200 and "already exists" in reply.data["error"]
    assert len(ok(console, "/api/global")["rules"]) == 1


@pytest.mark.parametrize("body", [
    {"content": "x", "subtype": "architecture"},
    {"content": "x", "subtype": "pattern"},
    {"subtype": "constraint"},
])
def test_an_invalid_global_rule_is_a_400(console, body):
    assert write(console, "POST", "/api/global", body=body).status == 400


def test_deleting_an_unknown_global_rule_is_a_404(console):
    assert write(console, "DELETE", "/api/global/deadbeef").status == 404


def test_a_corrupt_global_file_reads_as_unreadable_not_empty(console):
    """The Global view renders `ok: false` as "unreadable". Without the pair, a file that still
    holds every cross-repo rule renders as "No global rules" - next to an Add button whose
    write path is the one thing that could have replaced them."""
    (store.STORE_DIR / f"{store.GLOBAL_SLUG}.json").write_text("{not json at all")
    payload = ok(console, "/api/global")
    assert payload["ok"] is False and payload["error"] and payload["rules"] == []

    # The store refuses to write over a file it could not parse, and it signals that refusal
    # the same way it signals a duplicate. Reporting "already exists" told the developer their
    # rule was redundant instead of naming the file that needs repair.
    reply = write(console, "POST", "/api/global",
                  body={"content": "Never commit generated files", "subtype": "constraint"})
    assert reply.status == 200 and "unreadable" in reply.data["error"]
    assert "already exists" not in reply.data["error"]


# --- team ------------------------------------------------------------------------------

def test_team_in_local_mode_reports_the_mode_without_erroring(console, repo):
    payload = ok(console, f"/api/team/{repo['slug']}")
    assert payload["mode"] == "local" and payload["enabled"] is False
    assert payload["decisions"] == [] and payload["counts"]["decisions"] == 0
    # Shareable comes from the local store, so it is populated even offline.
    assert {d["id"] for d in payload["shareable"]} >= {repo["plain"]}
    assert payload["shareable"][0]["shared"] is False


def test_team_projects_the_cached_rows_and_the_last_sync(console, repo):
    team_context._save_cache(repo["path"], {
        "repo_key": "github.com/acme/widgets",
        "cursor": "000004",
        "decisions": [{"id": "t1", "type": "architecture", "title": "Use gRPC internally",
                       "content": "Use gRPC for service-to-service calls", "rationale": "Speed",
                       "repo": "github.com/acme/widgets", "agent": None, "scope": "team"}],
        "last_sync": {"at": 1.0, "ok": True, "duration_ms": 12, "upserted": 1, "removed": 0,
                      "consecutive_failures": 0},
        "last_ok_at": time.time(),
    })
    payload = ok(console, f"/api/team/{repo['slug']}")
    assert payload["repo_key"] == "github.com/acme/widgets"
    assert payload["counts"]["decisions"] == 1
    assert payload["decisions"][0]["scope"] == "team"
    assert payload["last_sync"]["ok"] is True and payload["last_sync"]["upserted"] == 1
    assert payload["staleness"]["stale"] is False


def test_share_state_reflects_the_markers_and_the_outbox(console, repo):
    from contexer import share

    endpoint = "https://mcp.example/mcp"
    config.CONFIG_PATH.write_text(f'mode = "team"\nendpoint = "{endpoint}"\n')
    share._mark_shared([repo["plain"]], endpoint)
    share._enqueue({"decision_id": repo["pending"], "type": "constraint", "content": "x"})

    detail = ok(console, f"/api/store/{repo['slug']}/decisions/{repo['plain']}")
    assert detail["share"]["shared"] is True and detail["share"]["shared_at"]
    assert detail["share"]["endpoint"] == endpoint and detail["share"]["queued"] is False

    queued = ok(console, f"/api/store/{repo['slug']}/decisions/{repo['pending']}")
    assert queued["share"]["shared"] is False and queued["share"]["queued"] is True

    shareable = ok(console, f"/api/team/{repo['slug']}")["shareable"]
    assert [d["shared"] for d in shareable if d["id"] == repo["plain"]] == [True]


def test_a_long_dead_team_cache_reads_as_stale(console, repo):
    team_context._save_cache(repo["path"], {
        "repo_key": "github.com/acme/widgets", "cursor": None, "decisions": [],
        "last_ok_at": time.time() - 2 * team_context._STALE_AFTER,
    })
    staleness = ok(console, f"/api/team/{repo['slug']}")["staleness"]
    assert staleness["stale"] is True and staleness["age_seconds"] > team_context._STALE_AFTER


def test_pull_in_local_mode_degrades_to_a_renderable_error(console, repo):
    reply = write(console, "POST", f"/api/store/{repo['slug']}/pull")
    assert reply.status == 200 and "contexer login" in reply.data["error"]


def test_share_in_local_mode_degrades_to_a_renderable_message(console, repo):
    reply = write(console, "POST", f"/api/store/{repo['slug']}/share",
                  body={"ids": [repo["plain"]]})
    assert reply.status == 200 and "team mode" in reply.data["message"]


@pytest.mark.parametrize("body", [{"ids": []}, {"ids": "abc"}, {"ids": [1]}, {"ids": [""]}, {}])
def test_a_malformed_share_request_is_a_400(console, repo, body):
    assert write(console, "POST", f"/api/store/{repo['slug']}/share", body=body).status == 400


# --- config ----------------------------------------------------------------------------

def test_config_reports_the_token_as_a_boolean_and_never_serializes_it(console):
    config.CONFIG_PATH.write_text('mode = "team"\nendpoint = "https://mcp.example/mcp"\n'
                                  'token = "SUPER-SECRET-BEARER"\n')
    reply = read(console, "/api/config")
    assert reply.status == 200
    assert reply.data["profile"]["token_set"] is True
    assert reply.data["profile"]["mode"] == "team"
    assert b"SUPER-SECRET-BEARER" not in reply.body


def test_config_reports_signed_out_without_credentials(console):
    login = ok(console, "/api/config")["login"]
    # `state` and `message` joined this block so the console can say WHY team sync is failing.
    # The shape is asserted exactly: a stray key here is how a credential would leak.
    assert set(login) == {"state", "logged_in", "issuer", "expires_at", "scope", "message"}
    assert login["state"] == "none"
    assert login["logged_in"] is False
    assert login["issuer"] is None and login["expires_at"] is None and login["scope"] is None
    assert login["message"]


def test_config_tells_an_expired_session_from_a_dead_one(console):
    """The masking bug, at the surface that showed it: signed-out, past-expiry and genuinely
    dead all rendered as one line before `state` existed.

    Past expiry is THREE situations, not one, and only two are worth a browser: a session with a
    refresh token renews itself on the next sync (tokens are minted with expires_in 3600, so a
    healthy session is past expiry every hour); one without has nothing to renew from; one whose
    grant was refused is dead until the developer logs in again."""
    config.CONFIG_PATH.write_text('mode = "team"\nendpoint = "https://mcp.example/mcp"\n')
    base = {"issuer": "https://mcp.example", "client_id": "c",
            "token_endpoint": "https://mcp.example/token", "access_token": "SECRET-ACCESS",
            "expires_at": time.time() - 10, "scope": "sync"}

    api.auth._save_creds({**base, "refresh_token": "SECRET-REFRESH"})
    reply = read(console, "/api/config")
    assert reply.data["login"]["state"] == "renewable", "a renewable session is not a dead one"
    assert reply.data["login"]["issuer"] == "https://mcp.example"
    assert b"SECRET-ACCESS" not in reply.body and b"SECRET-REFRESH" not in reply.body

    api.auth._save_creds(base)  # nothing to renew from
    assert ok(console, "/api/config")["login"]["state"] == "expired"

    api.auth._save_creds({**base, "refresh_token": "SECRET-REFRESH",
                          api.auth._REFRESH_FAILED_AT: time.time()})
    assert ok(console, "/api/config")["login"]["state"] == "refresh_failed", \
        "a recorded rejection outranks a refresh token that is present but refused"


def test_config_points_at_the_paths_in_use(console):
    payload = ok(console, "/api/config")
    assert payload["store_dir"] == str(store.STORE_DIR)
    assert payload["config_path"] == str(config.CONFIG_PATH)
    assert payload["version"] == console.version


def test_writing_config_persists_the_allowlisted_keys(console):
    reply = write(console, "PUT", "/api/config",
                  body={"autostart": True, "idle_timeout_minutes": 15, "port": 40000})
    assert reply.status == 200
    settings = config.load_ui_settings()
    assert settings.autostart is True and settings.idle_timeout_minutes == 15
    assert settings.port == 40000


def test_writing_config_preserves_the_teams_token(console):
    config.CONFIG_PATH.write_text('mode = "team"\nendpoint = "https://mcp.example/mcp"\n'
                                  'token = "SUPER-SECRET-BEARER"\n')
    assert write(console, "PUT", "/api/config", body={"autostart": True}).status == 200
    assert config.load_profile().token == "SUPER-SECRET-BEARER"
    assert config.CONFIG_PATH.with_name("config.toml.bak").exists()


@pytest.mark.parametrize("body", [
    {"token": "stolen"}, {"endpoint": "https://evil"}, {"mode": "team"},
    {"port": "nope"}, {"port": 0}, {"autostart": "yes"}, {"idle_timeout_minutes": 0}, {},
])
def test_a_config_write_outside_the_allowlist_is_a_400(console, body):
    assert write(console, "PUT", "/api/config", body=body).status == 400


def test_a_config_write_cannot_redirect_the_file(console, tmp_path):
    """`path` is positional-only on write_settings, so it is not even a settable key here."""
    victim = tmp_path / "elsewhere.toml"
    assert write(console, "PUT", "/api/config",
                 body={"path": str(victim), "autostart": True}).status == 400
    assert not victim.exists()


# --- login / logout --------------------------------------------------------------------

class FakeProc:
    """Stand-in for the login subprocess. A test must NEVER spawn a real login: it opens a
    browser, binds its own loopback port and blocks with no timeout.

    Mirrors the seam `auth._await_login` actually consumes - `stdout` streamed line by line,
    then `wait(timeout=...)` - NOT `communicate()`, which it stopped calling once the authorize
    URL had to be published while the flow was still pending rather than after it ended. The
    reference implementation is `tests/test_auth.py::FakeProc`; kept in step with it deliberately,
    since a fake that satisfies a seam the code no longer uses tests nothing."""

    def __init__(self, *, returncode=0, output="", block=False, timeout=False):
        self.returncode = returncode
        self.output = output
        self.killed = False
        self.waited = []
        self._timeout = timeout
        self._release = threading.Event()
        if not (block or timeout):
            self._release.set()
        self.stdout = self._stream(output)

    def _stream(self, output):
        for line in output.splitlines(keepends=True):
            yield line
        # The real child prints the authorize URL within a second, then lives on for minutes
        # holding the pipe open while it waits for the browser callback.
        assert self._release.wait(5), "the fake login was never released"

    def wait(self, timeout=None):
        self.waited.append(timeout)
        if self._timeout and not self.killed:
            raise subprocess.TimeoutExpired("contexer login", timeout or 0)
        assert self._release.wait(5), "the fake login was never released"
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9
        self._release.set()

    def finish(self, returncode=0):
        self.returncode = returncode
        self._release.set()


@pytest.fixture
def login(monkeypatch):
    """Tracked login jobs over a fake subprocess. Returns an installer for the next spawn."""
    monkeypatch.setattr(api.auth, "_login_job", None)

    def use(proc):
        monkeypatch.setattr(api.auth, "_spawn_login", lambda endpoint: proc)
        return proc

    use(FakeProc())
    return use


def _settled(job_id: str, timeout: float = 3.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        status = api.auth.login_job_status(job_id)
        if status["state"] != "pending":
            return status
        time.sleep(0.005)
    raise AssertionError(f"login job never settled: {api.auth.login_job_status(job_id)}")


def test_starting_a_login_answers_with_a_job_id(console, login):
    reply = write(console, "POST", "/api/login")
    assert reply.status == 202
    assert _settled(reply.data["job"])["state"] == "ok"


@pytest.mark.parametrize("body", [
    {"endpoint": "https://evil.example/mcp"}, {"job": "x"}, {"endpoint": None}, {"": ""},
])
def test_a_login_body_carrying_any_field_is_a_400(console, login, body):
    """`endpoint` is the one that matters: a caller-supplied endpoint would aim the OAuth flow
    at an attacker's IdP and persist the token it handed back. Nothing is accepted, so a
    typo'd field cannot be silently dropped either."""
    reply = write(console, "POST", "/api/login", body=body)
    assert reply.status == 400
    assert api.auth._login_job is None  # refused BEFORE anything was spawned


def test_a_second_login_while_one_runs_is_a_409_naming_the_job_in_flight(console, login):
    """The id is what lets a second tab follow the login it attached to: without it that tab
    can only watch the session go live, and never learns the login actually failed."""
    proc = login(FakeProc(block=True))
    first = write(console, "POST", "/api/login")
    assert first.status == 202
    second = write(console, "POST", "/api/login")
    assert second.status == 409 and "already running" in second.data["error"]
    assert second.data["job"] == first.data["job"]
    assert ok(console, f"/api/login/status?job={second.data['job']}")["state"] == "pending"
    api.auth.stop_login_job()
    assert proc.killed is True
    assert _settled(first.data["job"])["state"] == "failed"


def test_login_status_reports_pending_then_ok(console, login):
    proc = login(FakeProc(block=True))
    job = write(console, "POST", "/api/login").data["job"]
    pending = ok(console, f"/api/login/status?job={job}")
    assert pending["state"] == "pending" and pending["message"]
    proc.finish(0)
    assert _settled(job)["state"] == "ok"
    assert ok(console, f"/api/login/status?job={job}")["state"] == "ok"


def test_login_status_reports_a_failure_without_leaking_the_flows_query_string(console, login):
    login(FakeProc(returncode=1, output=(
        "http://localhost:8080/authorize?client_id=cid-9&state=STATEVALUE\n"
        "contexer login: authorization failed - access_denied\n")))
    job = write(console, "POST", "/api/login").data["job"]
    _settled(job)
    reply = read(console, f"/api/login/status?job={job}")
    assert reply.data["state"] == "failed" and "access_denied" in reply.data["message"]
    assert b"STATEVALUE" not in reply.body and b"cid-9" not in reply.body


def test_a_login_over_the_five_minute_cap_is_reported_as_failed(console, login):
    proc = login(FakeProc(timeout=True))
    job = write(console, "POST", "/api/login").data["job"]
    status = _settled(job)
    assert status["state"] == "failed" and "timed out" in status["message"]
    assert proc.killed is True


def test_an_unknown_login_job_is_a_404(console, login):
    assert read(console, "/api/login/status?job=nope").status == 404
    assert read(console, "/api/login/status").status == 404


def test_logout_deletes_the_stored_credentials(console):
    api.auth._save_creds({"issuer": "https://mcp.example", "access_token": "SECRET-ACCESS"})
    reply = write(console, "POST", "/api/logout")
    assert reply.status == 200 and reply.data["message"]
    assert api.auth._load_creds() is None
    assert b"SECRET-ACCESS" not in reply.body
    assert "cancelled" not in reply.data["message"], "nothing was in flight to cancel"
    again = write(console, "POST", "/api/logout")
    assert again.status == 200 and "No Contexer Teams credentials" in again.data["message"]


def test_logout_cancels_a_login_still_in_flight(console, login):
    """A login started minutes earlier finishes AFTER the logout and rewrites both the creds
    file and config.toml, undoing a logout the user was told had succeeded.

    The console's polling flag cannot prevent this: it is per-tab, so a second tab or a
    `contexer login` in a terminal is invisible to it - the server has to resolve the race."""
    proc = login(FakeProc(block=True))
    api.auth._save_creds({"issuer": "https://mcp.example", "access_token": "SECRET-ACCESS"})
    job = write(console, "POST", "/api/login").data["job"]
    assert ok(console, f"/api/login/status?job={job}")["state"] == "pending"

    reply = write(console, "POST", "/api/logout")
    assert reply.status == 200
    assert proc.killed is True, "the in-flight login outlived the logout"
    assert _settled(job)["state"] == "failed"
    assert "cancelled" in reply.data["message"], "the cancellation must not be silent"
    assert api.auth._load_creds() is None


def test_a_logout_body_carrying_any_field_is_a_400(console):
    assert write(console, "POST", "/api/logout", body={"purge": True}).status == 400


def test_a_login_job_is_killed_when_the_daemon_stops(tmp_path, monkeypatch):
    """No orphaned browser flow may outlive the console that opened it - the OAuth callback
    would otherwise still be able to write credentials nobody is waiting for."""
    home = tmp_path / ".contexer"
    home.mkdir()
    monkeypatch.setattr(store, "STORE_DIR", home)
    monkeypatch.setattr(config, "CONFIG_PATH", home / "config.toml")
    monkeypatch.setattr(daemon, "STATE_PATH", home / "ui.json")
    monkeypatch.setattr(daemon, "LOG_PATH", home / "ui.log")
    monkeypatch.setattr(server.signal, "signal", lambda *a: None)  # main() runs off-thread here
    monkeypatch.setattr(api.auth, "_login_job", None)
    proc = FakeProc(block=True)
    monkeypatch.setattr(api.auth, "_spawn_login", lambda endpoint: proc)

    built = {}
    original = server.ConsoleServer.__init__
    monkeypatch.setattr(server.ConsoleServer, "__init__",
                        lambda self, port, token, **kw: (original(self, port, token, **kw),
                                                         built.setdefault("srv", self))[0])
    thread = threading.Thread(target=server.main, args=(["--port", "0"],), daemon=True)
    thread.start()
    for _ in range(500):
        if "srv" in built:
            break
        time.sleep(0.002)

    job = api.auth.start_login_job()
    assert api.auth.login_job_status(job)["state"] == "pending"
    built["srv"].stop("SIGTERM")
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert proc.killed is True
    assert api.auth.login_job_status(job)["state"] == "failed"


# --- request hygiene -------------------------------------------------------------------

@pytest.mark.parametrize("path,body", [
    ("/api/config", {"autostart": True, "bogus": 1}),
    ("/api/global", {"content": "x", "subtype": "constraint", "oops": 1}),
])
def test_an_unknown_json_key_is_a_400(console, path, body):
    method = "PUT" if path == "/api/config" else "POST"
    reply = write(console, method, path, body=body)
    assert reply.status == 400 and "unknown field" in reply.data["error"]


def test_an_unknown_json_key_on_an_edit_is_a_400(console, repo):
    reply = write(console, "PATCH", f"/api/store/{repo['slug']}/decisions/{repo['plain']}",
                  body={"content": "New text", "status": "approved"})
    assert reply.status == 400 and "unknown field" in reply.data["error"]


def test_a_body_over_the_size_cap_is_a_413(console):
    reply = write(console, "PUT", "/api/config", raw=b"x" * (server.BODY_LIMIT + 1))
    assert reply.status == 413


def test_a_body_at_the_size_cap_is_read(console):
    padding = "y" * (server.BODY_LIMIT - 200)
    reply = write(console, "PUT", "/api/config", body={"autostart": True, "note": padding})
    assert reply.status == 400 and "unknown field" in reply.data["error"]


@pytest.mark.parametrize("raw", [b"not json", b"[]", b'"text"', b"123"])
def test_a_non_object_body_is_a_400(console, raw):
    assert write(console, "PUT", "/api/config", raw=raw).status == 400


def test_mutations_are_rate_limited(console):
    for _ in range(server.MUTATIONS_PER_MINUTE):
        assert write(console, "DELETE", "/api/global/deadbeef").status == 404
    reply = write(console, "DELETE", "/api/global/deadbeef")
    assert reply.status == 429 and reply.data["error"]
    # Reads are never budgeted: a rate-limited console must still be able to show state.
    assert read(console, "/api/stores").status == 200


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/nope"), ("GET", "/api"), ("GET", "/nope"),
    ("POST", "/api/stores"), ("PUT", "/api/global"), ("DELETE", "/api/config"),
])
def test_an_unrouted_request_is_a_404(console, method, path):
    reply = read(console, path) if method == "GET" else write(console, method, path, body={})
    assert reply.status == 404


@pytest.mark.parametrize("method,suffix", [
    ("GET", "/bogus"), ("POST", ""), ("GET", "/decisions/x/bogus"),
    ("POST", "/decisions/x/bogus"), ("DELETE", "/deleted"), ("PUT", "/share"),
])
def test_an_unrouted_store_request_is_a_404(console, repo, method, suffix):
    path = f"/api/store/{repo['slug']}{suffix}"
    reply = read(console, path) if method == "GET" else write(console, method, path, body={})
    assert reply.status == 404


def test_approve_with_no_body_defaults_to_approving(console, repo):
    reply = write(console, "POST",
                  f"/api/store/{repo['slug']}/decisions/{repo['pending']}/approve")
    assert reply.status == 200
    assert ok(console, f"/api/store/{repo['slug']}/decisions/"
                       f"{repo['pending']}")["status"] == "approved"


def test_too_many_share_ids_is_a_400(console, repo):
    reply = write(console, "POST", f"/api/store/{repo['slug']}/share",
                  body={"ids": ["x"] * (api.MAX_SHARE_IDS + 1)})
    assert reply.status == 400


def test_a_local_share_failure_is_reported_as_text_not_a_500(console, repo, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(api.share, "share_ids", boom)
    reply = write(console, "POST", f"/api/store/{repo['slug']}/share",
                  body={"ids": [repo["plain"]]})
    assert reply.status == 200 and "Share failed" in reply.data["error"]


def test_pull_in_team_mode_reports_what_changed(console, repo, monkeypatch):
    config.CONFIG_PATH.write_text('mode = "team"\nendpoint = "https://mcp.example/mcp"\n')
    monkeypatch.setattr(api.team_context, "pull", lambda path, **kw: (2, 1))
    reply = write(console, "POST", f"/api/store/{repo['slug']}/pull")
    assert reply.status == 200 and reply.data["message"] == "Pulled - 2 updated, 1 removed."


def test_pull_reports_a_degraded_sync_as_a_failure(console, repo, monkeypatch):
    """`pull` answers (0, 0) for a refused connection and for a clean no-op alike, so
    "Pulled - 0 updated, 0 removed." was reporting an outage as a success. The sync's own
    last_sync stamp is the evidence, so the button cannot lie about it."""
    config.CONFIG_PATH.write_text('mode = "team"\nendpoint = "http://127.0.0.1:1/mcp"\n')

    def degraded(path, **kw):
        # Exactly what team_context._sync writes when the cloud is unreachable.
        cache = team_context._load_cache(path)
        team_context._save_cache(path, {**cache, "last_sync": {
            "at": time.time(), "ok": False, "duration_ms": 7, "error": "degraded",
            "consecutive_failures": 3}})
        return (0, 0)

    monkeypatch.setattr(api.team_context, "pull", degraded)
    reply = write(console, "POST", f"/api/store/{repo['slug']}/pull")
    assert reply.status == 200 and "message" not in reply.data
    assert "Pull failed" in reply.data["error"] and "127.0.0.1:1" in reply.data["error"]
    assert "3 consecutive failures" in reply.data["error"]


def test_pull_does_not_claim_success_when_no_sync_was_attempted(console, repo, monkeypatch):
    """No git origin (or an unresolvable repo) means nothing was even tried - reporting
    "Pulled - 0 updated" there is the same lie in a quieter form."""
    config.CONFIG_PATH.write_text('mode = "team"\nendpoint = "https://mcp.example/mcp"\n')
    monkeypatch.setattr(api.team_context, "pull", lambda path, **kw: (0, 0))
    reply = write(console, "POST", f"/api/store/{repo['slug']}/pull")
    assert reply.status == 200 and "message" not in reply.data
    assert "did not run" in reply.data["error"]


def test_pull_reports_a_zero_row_success_as_a_success(console, repo, monkeypatch):
    """A sync that ran and found nothing new is NOT a failure - the stamp says ok."""
    config.CONFIG_PATH.write_text('mode = "team"\nendpoint = "https://mcp.example/mcp"\n')

    def clean(path, **kw):
        cache = team_context._load_cache(path)
        team_context._save_cache(path, {**cache, "last_sync": {
            "at": time.time(), "ok": True, "duration_ms": 9, "upserted": 0, "removed": 0,
            "consecutive_failures": 0}})
        return (0, 0)

    monkeypatch.setattr(api.team_context, "pull", clean)
    reply = write(console, "POST", f"/api/store/{repo['slug']}/pull")
    assert reply.status == 200 and reply.data["message"] == "Pulled - 0 updated, 0 removed."


def test_pull_takes_the_interactive_path_not_the_sessionstart_seam(console, repo, monkeypatch):
    """`refresh` is SessionStart's seam: a hard ~3s transport ceiling plus an outbox drain.

    Against a cold-start endpoint answering in 4-8s the button reported "could not sync" on
    every click and counted a consecutive failure each time, while `contexer pull` in a
    terminal on the same machine succeeded - and a read-shaped button was pushing queued
    shares upstream, which the CLI pull never does."""
    config.CONFIG_PATH.write_text('mode = "team"\nendpoint = "https://mcp.example/mcp"\n')
    seen = {}
    drained = []

    def record(path, profile, *, timeout=None):
        seen["timeout"] = timeout
        return ([], [])

    monkeypatch.setattr(api.team_context, "_sync", record)
    monkeypatch.setattr(api.share, "drain_outbox", lambda *a, **kw: drained.append(1))
    reply = write(console, "POST", f"/api/store/{repo['slug']}/pull")
    assert reply.status == 200
    assert seen["timeout"] is None, "the console keeps the interactive transport default"
    assert drained == [], "a button labelled Pull must not push the share outbox"


def test_a_pull_that_raises_is_reported_as_text_not_a_500(console, repo, monkeypatch):
    """`pull` propagates where `refresh` swallowed. The cloud's own failures are already
    absorbed into the degraded stamp, so what is left is local - and reporting it as "did not
    run" (what the swallowing seam produced) blamed the git remote for a disk fault."""
    config.CONFIG_PATH.write_text('mode = "team"\nendpoint = "https://mcp.example/mcp"\n')

    def boom(path, profile, *, timeout=None):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(api.team_context, "_sync", boom)
    reply = write(console, "POST", f"/api/store/{repo['slug']}/pull")
    assert reply.status == 200 and "message" not in reply.data
    assert reply.data["error"] == "Pull failed - disk on fire"


def _degrade(monkeypatch, error="degraded"):
    """Make the next pull record exactly what team_context._sync writes when the cloud refuses.

    `error` is the classification the sync now records: "auth" when the endpoint rejected the
    credential, "degraded" for anything else. It used to be "degraded" either way, which is what
    made a rejection indistinguishable from an outage everywhere downstream."""
    def degraded(path, **kw):
        cache = team_context._load_cache(path)
        team_context._save_cache(path, {**cache, "last_sync": {
            "at": time.time(), "ok": False, "duration_ms": 4, "error": error,
            "consecutive_failures": 1}})
        return (0, 0)

    monkeypatch.setattr(api.team_context, "pull", degraded)


def test_pull_flags_an_auth_shaped_failure_so_the_console_can_offer_a_login(console, repo,
                                                                           monkeypatch):
    """The live bug: a session expired three days ago, the rotated refresh token was rejected,
    and the pull button could only say "could not sync". `auth: true` is what lets the console
    render "log in again" instead of pattern-matching the error string."""
    config.CONFIG_PATH.write_text('mode = "team"\nendpoint = "https://mcp.example/mcp"\n')
    api.auth._save_creds({"issuer": "https://mcp.example", "client_id": "c",
                          "token_endpoint": "https://mcp.example/token",
                          "access_token": "SECRET-ACCESS", "refresh_token": "SECRET-REFRESH",
                          "expires_at": time.time() - 10,
                          api.auth._REFRESH_FAILED_AT: time.time()})
    _degrade(monkeypatch)
    reply = write(console, "POST", f"/api/store/{repo['slug']}/pull")
    assert reply.status == 200
    assert reply.data["auth"] is True
    assert reply.data["state"] == "refresh_failed"
    assert "log in again" in reply.data["error"]
    assert b"SECRET-ACCESS" not in reply.body


def test_pull_does_not_flag_a_network_failure_as_an_auth_problem(console, repo, monkeypatch):
    """Telling someone to log in again when their network is down is its own wrong turn, so
    a valid session plus a failed sync keeps today's message and carries no `auth` key."""
    config.CONFIG_PATH.write_text('mode = "team"\nendpoint = "https://mcp.example/mcp"\n')
    api.auth._save_creds({"issuer": "https://mcp.example", "client_id": "c",
                          "token_endpoint": "https://mcp.example/token",
                          "access_token": "live", "refresh_token": "r",
                          "expires_at": time.time() + 3600})
    _degrade(monkeypatch)
    reply = write(console, "POST", f"/api/store/{repo['slug']}/pull")
    assert reply.status == 200
    assert "auth" not in reply.data and "state" not in reply.data
    assert reply.data["error"].startswith("Pull failed - could not sync with")


def test_pull_does_not_flag_a_static_token_failure(console, repo, monkeypatch):
    """A configured static token that the cloud rejects is indistinguishable from an outage
    from here - no local evidence, so no actionable panel."""
    config.CONFIG_PATH.write_text('mode = "team"\nendpoint = "https://mcp.example/mcp"\n'
                                  'token = "STATIC-BEARER"\n')
    _degrade(monkeypatch)
    reply = write(console, "POST", f"/api/store/{repo['slug']}/pull")
    assert reply.status == 200 and "auth" not in reply.data


def test_pull_flags_a_server_side_revocation_the_local_state_cannot_see(console, repo,
                                                                       monkeypatch):
    """The masking case the local credential can never explain.

    A token revoked upstream (token-family revocation after a reused rotating refresh token) is
    still unexpired on disk, so `auth_state` honestly reports `logged_in` and would blame the
    network. The sync's own `error: "auth"` - the endpoint's verdict - is the only evidence that
    exists, and acting on it is what stops a dead credential from reading as an outage."""
    config.CONFIG_PATH.write_text('mode = "team"\nendpoint = "https://mcp.example/mcp"\n')
    api.auth._save_creds({"issuer": "https://mcp.example", "client_id": "c",
                          "token_endpoint": "https://mcp.example/token",
                          "access_token": "REVOKED-UPSTREAM", "refresh_token": "r",
                          "expires_at": time.time() + 3600})
    _degrade(monkeypatch, error="auth")
    reply = write(console, "POST", f"/api/store/{repo['slug']}/pull")
    assert reply.status == 200
    assert reply.data["auth"] is True, "a rejected credential must offer a login"
    assert reply.data["state"] == "logged_in"  # locally valid: only the server knew
    assert "log in again" in reply.data["error"]
    assert b"REVOKED-UPSTREAM" not in reply.body


def test_a_pull_that_never_ran_flags_a_missing_credential(console, repo, monkeypatch):
    """With no credential at all RemoteStore is never even built, so "nothing was attempted"
    IS the auth failure - and a login is the fix regardless of the git remote."""
    config.CONFIG_PATH.write_text('mode = "team"\nendpoint = "https://mcp.example/mcp"\n')
    monkeypatch.setattr(api.team_context, "pull", lambda path, **kw: (0, 0))
    reply = write(console, "POST", f"/api/store/{repo['slug']}/pull")
    assert reply.data["auth"] is True and reply.data["state"] == "none"
    assert "did not run" in reply.data["error"]


def test_a_pull_that_never_ran_with_a_usable_token_blames_the_git_remote(console, repo,
                                                                        monkeypatch):
    """A token was available, so the sync could have started: the missing remote is the cause,
    and telling this user to log in would send them after the wrong problem."""
    config.CONFIG_PATH.write_text('mode = "team"\nendpoint = "https://mcp.example/mcp"\n'
                                  'token = "STATIC-BEARER"\n')
    api.auth._save_creds({"issuer": "https://mcp.example", "client_id": "c",
                          "token_endpoint": "https://mcp.example/token",
                          "access_token": "old", "refresh_token": "r",
                          "expires_at": time.time() - 10,
                          api.auth._REFRESH_FAILED_AT: time.time()})
    monkeypatch.setattr(api.team_context, "pull", lambda path, **kw: (0, 0))
    reply = write(console, "POST", f"/api/store/{repo['slug']}/pull")
    assert "auth" not in reply.data
    assert "git remote" in reply.data["error"]


# --- corrupt stores --------------------------------------------------------------------

def test_a_corrupt_store_reads_as_unreadable_not_empty(console, tmp_path):
    path = str(tmp_path / "broken")
    (store.STORE_DIR / f"{store.repo_slug(path)}.json").write_text(
        json.dumps({"repo_path": path, "entries": "not a list"}))
    row = next(r for r in ok(console, "/api/stores") if r["repo_path"] == path)
    assert row["ok"] is False and row["error"]

    dashboard = ok(console, f"/api/store/{store.repo_slug(path)}")
    assert dashboard["ok"] is False and dashboard["health"]["ok"] is False
    assert dashboard["health"]["error"] and dashboard["counts"]["decisions"] == 0

    listing = ok(console, f"/api/store/{store.repo_slug(path)}/decisions")
    assert listing["ok"] is False and listing["decisions"] == []


@pytest.mark.parametrize("body", ["{not json at all", "[]", "null", "42", ""])
def test_an_unparseable_store_is_addressable_as_unreadable_not_a_404(console, body):
    """A 404 here made the console render its generic "could not load this view" error for a
    store that plainly exists. The slug is known, so it resolves - as "store unreadable"."""
    (store.STORE_DIR / "garbage-deadbeef.json").write_text(body)
    row = next(r for r in ok(console, "/api/stores") if r["slug"] == "garbage-deadbeef")
    assert row["ok"] is False and row["repo_path"] == "" and row["decisions"] == 0

    dashboard = ok(console, "/api/store/garbage-deadbeef")
    assert dashboard["ok"] is False and dashboard["error"]
    assert dashboard["repo_path"] == "" and dashboard["counts"]["decisions"] == 0
    assert dashboard["tombstones"]["ok"] is False


def test_an_unreadable_store_keeps_every_key_the_dashboard_renders(console, repo):
    """Same key set as a healthy store, so the console branches on `ok` alone instead of
    blanking a pane on a missing key."""
    (store.STORE_DIR / "garbage-deadbeef.json").write_text("{not json at all")
    healthy = ok(console, f"/api/store/{repo['slug']}")
    degraded = ok(console, "/api/store/garbage-deadbeef")
    assert set(degraded) == set(healthy)
    assert set(degraded["counts"]) == set(healthy["counts"])
    assert set(degraded["tombstones"]) == set(healthy["tombstones"])
    assert set(degraded["staleness"]) == set(healthy["staleness"])
    assert set(degraded["health"]) == set(healthy["health"])


@pytest.mark.parametrize("rest", ["/decisions", "/deleted", "/decisions/deadbeef"])
def test_the_sub_reads_of_an_unreadable_store_are_refused_not_faked(console, rest):
    """With no repo path there is no file to read, and guessing one from the slug is exactly
    what must not happen - so these refuse (409) instead of answering an empty list."""
    (store.STORE_DIR / "garbage-deadbeef.json").write_text("{not json at all")
    reply = read(console, f"/api/store/garbage-deadbeef{rest}")
    assert reply.status == 409 and "unreadable" in reply.data["error"]


@pytest.mark.parametrize("claimed", [
    "~/.claude", "~/.cursor", "~", "/", "relative/path", "",
])
def test_a_store_file_claiming_a_config_directory_is_never_read(console, claimed):
    """A poisoned repo_path must not redirect a console read into a tool's config dir - the
    same `_is_sane_repo` gate the capture paths use, applied at slug resolution. The slug still
    resolves (it names a real file), but every read of it reports unreadable."""
    from pathlib import Path

    repo_path = str(Path(claimed).expanduser()) if claimed.startswith("~") else claimed
    (store.STORE_DIR / "poisoned-deadbeef.json").write_text(
        json.dumps({"repo_path": repo_path, "entries": []}))
    dashboard = ok(console, "/api/store/poisoned-deadbeef")
    assert dashboard["ok"] is False and dashboard["repo_path"] == ""
    assert read(console, "/api/store/poisoned-deadbeef/decisions").status == 409
    assert read(console, "/api/team/poisoned-deadbeef").status == 409
    row = next(r for r in ok(console, "/api/stores") if r["slug"] == "poisoned-deadbeef")
    assert row["repo_path"] == ""


# --- failures --------------------------------------------------------------------------

def test_a_handler_exception_is_a_500_whose_traceback_stays_in_the_log(console, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("kaboom-internal-detail")

    monkeypatch.setattr(api, "dispatch", boom)
    reply = read(console, "/api/stores")
    assert reply.status == 500
    assert reply.data["error"] == "internal error" and reply.data["incident"]
    assert b"kaboom-internal-detail" not in reply.body and b"Traceback" not in reply.body

    log = daemon.LOG_PATH.read_text()
    assert reply.data["incident"] in log
    assert "kaboom-internal-detail" in log and "Traceback" in log


# --- idle watchdog ---------------------------------------------------------------------

def test_a_request_moves_the_idle_clock(console):
    stamps = [500.0, 600.0]
    console.clock = lambda: stamps.pop(0) if stamps else 999.0
    read(console, "/api/stores")
    assert console.last_request == 500.0


def test_a_poll_request_leaves_the_idle_clock_alone(console):
    """One forgotten background tab must not keep the daemon alive forever."""
    stamps = [500.0, 600.0]
    console.clock = lambda: stamps.pop(0) if stamps else 999.0
    read(console, "/api/stores")
    read(console, "/api/stores", poll=True)
    read(console, "/api/stores", poll=True)
    assert console.last_request == 500.0


def test_the_watchdog_stops_the_daemon_once_the_deadline_passes(console):
    console.last_request = 0.0
    server.watchdog(console, clock=lambda: console.idle_timeout_seconds, tick=0)
    assert console.exit_reason == "idle" and console.stopping.is_set()


def test_the_watchdog_leaves_a_busy_daemon_running(console):
    console.last_request = 100.0
    remaining = [100.0 + console.idle_timeout_seconds - 1]

    def clock():
        if remaining:
            return remaining.pop(0)
        console.stopping.set()  # end the loop after the one not-yet-idle check
        return 0.0

    server.watchdog(console, clock=clock, tick=0)
    assert console.exit_reason == ""


def test_the_watchdog_returns_when_the_server_is_already_stopping(console):
    console.stopping.set()
    console.last_request = 0.0
    server.watchdog(console, clock=lambda: 10 ** 9, tick=0)
    assert console.exit_reason == ""


class TestListLimitIsBounded:
    """`MAX_LIMIT` has to bound the DEFAULT response, not just an explicit oversized one.

    store.list_decisions reads `limit <= 0` as unbounded, so forwarding the bare 0 that
    `_int_param` returns for a missing parameter meant a `limit`-less URL serialized every
    matching row - the exact thing the cap exists to prevent.
    """

    def test_a_limit_less_request_is_capped(self, repo):
        _status, payload = api.dispatch(
            "GET", f"/api/store/{repo['slug']}/decisions", {}, None)
        assert payload["limit"] == api.MAX_LIMIT

    def test_an_explicit_zero_is_capped_too(self, repo):
        _status, payload = api.dispatch(
            "GET", f"/api/store/{repo['slug']}/decisions", {"limit": ["0"]}, None)
        assert payload["limit"] == api.MAX_LIMIT

    def test_an_explicit_limit_is_still_honoured(self, repo):
        _status, payload = api.dispatch(
            "GET", f"/api/store/{repo['slug']}/decisions", {"limit": ["2"]}, None)
        assert payload["limit"] == 2
        assert len(payload["decisions"]) == 2
        assert payload["total"] == 3

    def test_over_the_cap_is_still_rejected(self, repo):
        with pytest.raises(api.ApiError) as exc:
            api.dispatch("GET", f"/api/store/{repo['slug']}/decisions",
                         {"limit": [str(api.MAX_LIMIT + 1)]}, None)
        assert exc.value.status == 400
