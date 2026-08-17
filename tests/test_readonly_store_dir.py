"""Regression tests for #152 - an unwritable ~/.contexer must never abort a hook.

Codex's managed sandbox can leave the workspace writable while ~/.contexer is not. The
installed SessionStart hook then raised PermissionError on a best-effort bookkeeping write
(the `.current_repo` anchor) and Contexer injected nothing at all - losing the whole stored
context over a pointer file it never needed in order to *read* that context.

The rule under test: inability to write optional bookkeeping never prevents SessionStart
from rendering context.
"""
import errno
import json
from pathlib import Path


from contexer import store
from contexer.adapters import cursor, gemini

# A realistic host SessionStart/BeforeAgent payload. Hosts always send a session id, and
# several guarded code paths only execute when one is present - a bare "{}" silently
# skips them, which is how the gemini marker writes stayed unguarded through review.
RAW = json.dumps({"session_id": "sess-abc", "prompt": "why did we choose that?"})


def _deny_store_writes(monkeypatch):
    """Make every *write* under STORE_DIR raise PermissionError; reads keep working.

    Faithful to the sandbox failure mode and - unlike chmod - unaffected by the test
    process running as root. Called explicitly (not as a fixture) so a test can seed the
    store first and then have the sandbox close around it, exactly as a session does.
    """
    def guarded(name):
        real = getattr(Path, name)

        def deny(self, *args, **kwargs):
            if self == store.STORE_DIR or store.STORE_DIR in self.parents:
                raise PermissionError(errno.EPERM, "Operation not permitted", str(self))
            return real(self, *args, **kwargs)
        return deny

    for name in ("write_text", "write_bytes", "touch", "unlink", "mkdir"):
        monkeypatch.setattr(Path, name, guarded(name))


class TestAnchorRepo:
    def test_writes_the_pointer(self, tmp_repo):
        assert store.anchor_repo(tmp_repo) is True
        assert (store.STORE_DIR / ".current_repo").read_text() == tmp_repo

    def test_rejects_a_config_dir(self, tmp_repo):
        # The sanity gate is unchanged: a poisoned pointer would slug into its own
        # store file and swallow decisions made in the real project.
        assert store.anchor_repo(str(Path.home() / ".claude")) is False
        assert not (store.STORE_DIR / ".current_repo").exists()

    def test_rejects_an_empty_repo(self, tmp_repo):
        assert store.anchor_repo("") is False

    def test_returns_false_instead_of_raising_when_unwritable(self, tmp_repo, monkeypatch):
        _deny_store_writes(monkeypatch)
        assert store.anchor_repo(tmp_repo) is False  # must not raise PermissionError

    def test_survives_a_non_utf8_repo_path(self, tmp_repo):
        # Why the except is `Exception` and not `OSError`: a path carrying non-UTF-8
        # filesystem bytes (surfaced by Python as a surrogate escape - routine on Linux)
        # makes write_text raise UnicodeEncodeError, which is a ValueError, NOT an
        # OSError. Narrowing the catch would reproduce #152 on a different trigger.
        store.STORE_DIR.mkdir(parents=True, exist_ok=True)
        assert store.anchor_repo("/home/dev/proj\udcff") is False

    def test_survives_a_missing_home_directory(self, tmp_repo, monkeypatch):
        # Same reasoning, second trigger: _is_sane_repo consults Path.home(), which
        # raises RuntimeError (not OSError) when the home dir cannot be determined.
        def no_home():
            raise RuntimeError("Could not determine home directory")

        monkeypatch.setattr(Path, "home", staticmethod(no_home))
        assert store.anchor_repo(tmp_repo) is False


class TestSessionStartUnderReadOnlyStoreDir:
    def test_renders_the_same_context_as_a_writable_store_dir(self, populated_repo, monkeypatch):
        # The headline regression, stated as parity: decisions are on disk and readable,
        # only the bookkeeping write fails - so the session must get byte-identical
        # context. Before the fix this raised PermissionError and injected nothing.
        expected = store.get_session_start_context(populated_repo)
        _deny_store_writes(monkeypatch)
        assert store.get_session_start_context(populated_repo) == expected
        assert "decision" in json.dumps(expected)  # guards against asserting {} == {}

    def test_resume_without_context_still_injects_mining_instructions(self, tmp_repo, monkeypatch):
        # This branch writes the .resume_mining flag; the flag only silences a duplicate
        # bootstrap offer, so failing to write it must not cost the session the branch's
        # entire reason for existing.
        _deny_store_writes(monkeypatch)
        payload = store.session_start_payload(tmp_repo, source="resume")
        assert payload["context"]

    def test_bare_session_start_does_not_raise(self, tmp_repo, monkeypatch):
        _deny_store_writes(monkeypatch)
        store.get_session_start_context(tmp_repo)  # no stored context - must not raise

    def test_gemini_session_start_still_injects(self, populated_repo, monkeypatch):
        # gemini.session_start wraps everything in a blanket except that degrades to an
        # empty injection - so ANY unguarded write inside it silently costs the whole
        # context. The session id in RAW is load-bearing: without it _session_marker
        # returns None and the marker unlink never executes, so a `raw="{}"` version of
        # this test passes against code that is still broken for every real session.
        expected = gemini.session_start(populated_repo, RAW)
        _deny_store_writes(monkeypatch)
        assert gemini.session_start(populated_repo, RAW) == expected

    def test_gemini_before_agent_still_injects(self, populated_repo, monkeypatch):
        # Same for the per-prompt hook, with the post-compaction reload flag armed so the
        # reload branch's flag-consume actually runs. Losing this one costs the session
        # its rehydrated pre-compaction context, the review nudge, and the rationale.
        store.STORE_DIR.mkdir(parents=True, exist_ok=True)
        arm = (store.STORE_DIR / gemini._PENDING_RELOAD).touch

        # First call drains the fire-once state (the pending-review nudge fires once per
        # armed flag) so both compared runs start from the same steady state.
        arm()
        gemini.before_agent(populated_repo, RAW)
        arm()
        expected = gemini.before_agent(populated_repo, RAW)
        assert "Contexer" in expected, "expected a real injection to compare against"

        arm()  # re-arm for the sandboxed run
        _deny_store_writes(monkeypatch)
        assert gemini.before_agent(populated_repo, RAW) == expected

    def test_cursor_session_start_still_injects(self, populated_repo, monkeypatch):
        # Same shape for Cursor: its except returns the bare nudge, dropping the rules.
        monkeypatch.setattr(cursor, "_ensure_rule_file", lambda repo: None)
        expected = cursor.session_start(populated_repo, RAW)
        _deny_store_writes(monkeypatch)
        assert cursor.session_start(populated_repo, RAW) == expected

    def test_pointer_read_survives_an_unreadable_store_dir(self, tmp_repo, monkeypatch):
        real = Path.read_text

        def deny(self, *args, **kwargs):
            if self.name == ".current_repo":
                raise PermissionError(errno.EPERM, "Operation not permitted", str(self))
            return real(self, *args, **kwargs)

        store.anchor_repo(tmp_repo)
        monkeypatch.setattr(Path, "read_text", deny)
        assert store._current_repo_path() == ""  # degrades to "no repo", never raises
