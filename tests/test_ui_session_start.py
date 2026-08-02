"""The console URL on the session-start line — the one seam all four adapters share.

Two properties matter here and they pull in opposite directions:

1. With `[ui] autostart` off (the default), every payload is BYTE-IDENTICAL to a build with
   no console at all. That guard is what makes the feature safe to ship default-off.
2. With it on, the URL reaches the developer through `status` and NEVER the model through
   `context`.
"""
import os
import subprocess

import pytest

from contexer import store
from contexer.adapters import claude, cursor, gemini
from contexer.ui import daemon

# The exact status lines this build produced before the console existed. Literals, not
# recomputed from the code under test: a regression guard that derives its own expectation
# cannot fail.
NO_CONTEXT = "Contexer: no context stored — setup offer on next prompt"
RESUME_EMPTY = "Contexer: resumed with no stored context — mining this conversation for decisions"
POPULATED = ("Contexer: 1 architecture decision will be loaded on demand. 1 decision pending "
             "review — say 'review pending' or run `contexer review`.")
RESUME_POPULATED = "Contexer: session resumed — 2 decisions already loaded in conversation"


@pytest.fixture(autouse=True)
def ui_paths(tmp_path, monkeypatch):
    """Console statefile and log inside tmp_path — never the real ~/.contexer."""
    monkeypatch.setattr(daemon, "STATE_PATH", tmp_path / ".contexer" / "ui.json")
    monkeypatch.setattr(daemon, "LOG_PATH", tmp_path / ".contexer" / "ui.log")


@pytest.fixture(autouse=True)
def spawns(monkeypatch):
    """Record spawns instead of starting a real daemon."""
    calls = []

    class FakePopen:
        def __init__(self, argv, **kwargs):
            calls.append({"argv": argv, **kwargs})
            self.pid = os.getpid()  # so the recorded pid passes the daemon's liveness check

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    return calls


@pytest.fixture
def autostart(tmp_repo):
    """Opt this store's home into `[ui] autostart`, the way a developer would."""
    store.STORE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    (store.STORE_DIR / "config.toml").write_text("[ui]\nautostart = true\n")
    return tmp_repo


def seed(repo: str) -> None:
    store.update_decision(
        repo, "decided to use JWT instead of sessions - stateless, easier to scale", "s1")
    store.update_decision(
        repo, "constraint: never store plaintext passwords, always use bcrypt", "s1")


# ── autostart off: byte-identical ───────────────────────────────────────────

class TestAutostartOffIsUnchanged:
    def test_new_session_with_no_decisions(self, tmp_repo):
        assert store.session_start_payload(tmp_repo)["status"] == NO_CONTEXT

    def test_new_session_with_decisions(self, tmp_repo):
        seed(tmp_repo)
        assert store.session_start_payload(tmp_repo)["status"] == POPULATED

    def test_resume_with_no_decisions(self, tmp_repo):
        assert store.session_start_payload(tmp_repo, "resume")["status"] == RESUME_EMPTY

    def test_resume_with_decisions(self, tmp_repo):
        seed(tmp_repo)
        assert store.session_start_payload(tmp_repo, "resume")["status"] == RESUME_POPULATED

    def test_compact(self, tmp_repo):
        seed(tmp_repo)
        assert store.session_start_payload(tmp_repo, "compact")["status"] == POPULATED

    def test_compact_after_the_offer_stays_completely_silent(self, tmp_repo):
        store.session_start_payload(tmp_repo)  # arms the bootstrap offer
        assert store.session_start_payload(tmp_repo, "compact") == {"status": "", "context": ""}

    def test_the_daemon_is_never_contacted(self, monkeypatch, tmp_repo, spawns):
        """Off means off: no statefile read, no port probe, no spawn on the hook path."""
        monkeypatch.setattr(daemon, "ensure_running",
                            lambda port=None: pytest.fail("started a console while off"))
        seed(tmp_repo)
        store.session_start_payload(tmp_repo)
        assert spawns == []
        assert not daemon.STATE_PATH.exists()

    def test_an_unrelated_ui_key_does_not_turn_it_on(self, tmp_repo):
        store.STORE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        (store.STORE_DIR / "config.toml").write_text("[ui]\nport = 45678\n")
        assert store.session_start_payload(tmp_repo)["status"] == NO_CONTEXT


# ── autostart on: the URL, on the human channel only ────────────────────────

class TestAutostartOn:
    def test_status_carries_a_pairing_url_deep_linked_to_this_repo(self, autostart):
        seed(autostart)
        payload = store.session_start_payload(autostart)
        port = daemon.read_state().port
        assert payload["status"].startswith(f"{POPULATED} | console http://127.0.0.1:{port}/?p=")
        assert payload["status"].endswith(f"#/store/{store._slug(autostart)}")

    def test_the_url_never_carries_the_console_token(self, autostart):
        seed(autostart)
        payload = store.session_start_payload(autostart)
        assert daemon.read_state().token not in payload["status"]

    def test_context_never_sees_the_url(self, autostart):
        """`context` goes to the model: a loopback address and a credential have no business
        there, and would be replayed into every later prompt."""
        seed(autostart)
        payload = store.session_start_payload(autostart)
        for needle in ("console", "127.0.0.1", "?p=", "#/store/"):
            assert needle not in payload["context"]

    def test_the_console_is_started_once_and_reused(self, autostart, spawns, monkeypatch):
        seed(autostart)
        first = store.session_start_payload(autostart)["status"]
        monkeypatch.setattr(daemon, "probe", lambda port, token: True)  # now it is up
        second = store.session_start_payload(autostart)["status"]
        assert len(spawns) == 1
        assert first == second  # same token, same pairing window

    def test_the_bootstrap_offer_line_gets_the_url_too(self, autostart):
        assert store.session_start_payload(autostart)["status"].startswith(
            f"{NO_CONTEXT} | console http://127.0.0.1:")

    def test_a_resumed_session_gets_the_url(self, autostart):
        """The resume branch returns early, before the team suffix — it must not miss out."""
        seed(autostart)
        status = store.session_start_payload(autostart, "resume")["status"]
        assert status.startswith(f"{RESUME_POPULATED} | console http://127.0.0.1:")

    def test_the_console_suffix_follows_the_team_suffix(self, monkeypatch, autostart):
        monkeypatch.setattr(store, "_team_section_with_counts",
                            lambda repo: ("## Team context\n- something", 1, 0))
        seed(autostart)
        payload = store.session_start_payload(autostart)
        assert " | team: 1 synced | console http://127.0.0.1:" in payload["status"]
        assert "## Team context" in payload["context"]

    def test_a_silent_start_stays_silent(self, autostart):
        """Compaction after the offer deliberately says nothing; a URL would be pure noise."""
        store.session_start_payload(autostart)
        assert store.session_start_payload(autostart, "compact") == {"status": "", "context": ""}


# ── the deep link only appears when it will actually resolve ────────────────

class TestDeepLinkResolves:
    """A `#/store/<slug>` fragment only resolves once a store FILE exists — the slug is
    matched against `~/.contexer/*.json`. Printing one before that lands the developer on
    "Could not load this view", which reads as a broken console."""

    def test_the_first_session_in_a_new_repo_links_to_the_console_root(self, autostart):
        assert not store._store_path(autostart).exists()

        status = store.session_start_payload(autostart)["status"]

        port = daemon.read_state().port
        assert status.startswith(f"{NO_CONTEXT} | console http://127.0.0.1:{port}/?p=")
        assert "#/store/" not in status

    def test_the_deep_link_appears_once_the_store_file_exists(self, autostart):
        seed(autostart)
        assert store.session_start_payload(autostart)["status"].endswith(
            f"#/store/{store._slug(autostart)}")

    def test_a_session_with_no_repo_never_links_to_the_empty_string_slug(self, autostart):
        """`_slug("")` is sha1 of the empty string — `-da39a3ee`, a slug no store can have."""
        payload = store._with_console_url({"status": "Contexer: something"}, "")

        assert " | console http://127.0.0.1:" in payload["status"]
        assert "#/store/" not in payload["status"]
        assert store._slug("") not in payload["status"]

    def test_every_printed_deep_link_resolves(self, autostart):
        seed(autostart)
        status = store.session_start_payload(autostart)["status"]
        slug = status.rsplit("#/store/", 1)[1]
        assert store.resolve_store_slug(slug) == autostart


# ── failure modes: the console never breaks context injection ───────────────

class TestConsoleFailureIsInvisible:
    def test_a_console_that_will_not_start_leaves_the_line_alone(self, monkeypatch, autostart):
        monkeypatch.setattr(daemon, "ensure_running", lambda port=None: None)
        seed(autostart)
        assert store.session_start_payload(autostart)["status"] == POPULATED

    def test_an_exception_is_swallowed_silently(self, monkeypatch, autostart, capsys):
        def boom(port=None):
            raise RuntimeError("no sockets today")
        monkeypatch.setattr(daemon, "ensure_running", boom)
        seed(autostart)
        payload = store.session_start_payload(autostart)
        assert payload["status"] == POPULATED
        assert capsys.readouterr() == ("", ""), "a hook that prints breaks its host's JSON"

    def test_a_malformed_config_leaves_the_line_alone(self, tmp_repo, capsys):
        store.STORE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
        (store.STORE_DIR / "config.toml").write_text("[ui]\nautostart = 'yes'\n")
        seed(tmp_repo)
        assert store.session_start_payload(tmp_repo)["status"] == POPULATED
        assert capsys.readouterr() == ("", "")


# ── the four adapters ───────────────────────────────────────────────────────

class TestEveryAdapterSeesTheSameSeam:
    """One seam, four hosts. Only the two with a human-facing channel render `status`, and
    neither of the other two leaks it into the model's context."""

    def test_claude_renders_the_url_as_a_system_message_only(self, autostart):
        seed(autostart)
        out = claude.format_session_start(store.session_start_payload(autostart))
        assert " | console http://127.0.0.1:" in out["systemMessage"]
        assert "console" not in out["hookSpecificOutput"]["additionalContext"]

    def test_codex_shares_claudes_formatter(self, autostart):
        """codex.py's SessionStart hook calls store.get_session_start_context, which is
        claude.format_session_start over this same payload."""
        seed(autostart)
        out = store.get_session_start_context(autostart)
        assert " | console http://127.0.0.1:" in out["systemMessage"]
        assert "console" not in out["hookSpecificOutput"]["additionalContext"]

    def test_cursor_drops_the_status_line_entirely(self, autostart):
        seed(autostart)
        out = cursor.format_session_start(store.session_start_payload(autostart))
        assert "console" not in out["additional_context"]
        assert "systemMessage" not in out

    def test_gemini_injects_context_only(self, monkeypatch, autostart, tmp_path):
        seed(autostart)
        monkeypatch.setattr(store, "_resolve_repo", lambda p: autostart)
        out = gemini.session_start(autostart, "{}")
        assert "console" not in out and "127.0.0.1" not in out
