"""Tests for contexer.updates: the update-delivery fact, state and policy.

Two invariants matter more than any single assertion here and are each pinned by their own
test: the notice path never performs network I/O (constraint 1, it runs before every prompt),
and every failure degrades to silence rather than to an exception (constraint 4, a hook that
raises costs the developer something real in exchange for news that was never urgent).
"""
import json
import subprocess
import time

import pytest

from contexer import store, updates
from tests.conftest import redirect_store_dir
from contexer.adapters import claude, codex, cursor, gemini


class _Ns:
    """A stand-in module namespace. Stubs are rebound on `updates` rather than on the shared
    `urllib` / `subprocess` modules: patching an attribute of those patches them for the whole
    interpreter, which once broke every `subprocess.run` in the suite."""


def _stub_urlopen(monkeypatch, handler):
    ns = _Ns()
    ns.request = _Ns()
    ns.request.urlopen = handler
    monkeypatch.setattr(updates, "urllib", ns)


def _stub_popen(monkeypatch, handler):
    ns = _Ns()
    ns.Popen = handler
    ns.DEVNULL = subprocess.DEVNULL
    monkeypatch.setattr(updates, "subprocess", ns)


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Every test gets its own store dir, and no test may reach the network or fork.

    Autouse rather than opt-in: a single test that forgets would write the developer's real
    `~/.contexer/.update_check.json` and suppress their next genuine update notice.
    """
    redirect_store_dir(monkeypatch, tmp_path / ".contexer")
    (tmp_path / ".contexer").mkdir(parents=True)
    # conftest redirects state_path suite-wide and disables the check; this module is the one
    # that actually exercises it, so both are put back under this test's own tmp dir.
    monkeypatch.setattr(updates, "state_path",
                        lambda: tmp_path / ".contexer" / ".update_check.json")
    monkeypatch.delenv("CONTEXER_NO_UPDATE_CHECK", raising=False)

    def _no_network(*a, **k):
        raise AssertionError("network I/O attempted")

    def _no_fork(*a, **k):
        raise AssertionError("process spawn attempted")

    _stub_urlopen(monkeypatch, _no_network)
    _stub_popen(monkeypatch, _no_fork)
    return tmp_path


def _state(**over):
    base = {"latest": "9.9.9", "floor": None, "checked_at": time.time()}
    base.update(over)
    return base


def _pin_installed(monkeypatch, version="1.0.0"):
    monkeypatch.setattr(updates, "installed_version", lambda: version)


def _notice():
    """The text `deliver` yields for a render that always succeeds.

    Production paths all pass a real renderer and want its result; tests want the string.
    `updates.notice()` used to exist for exactly this and was deleted: once the CLI backstop
    moved to `deliver`, it had no production caller left and duplicated `deliver`'s body.
    """
    return updates.deliver(lambda text: text)


# ── version parsing ───────────────────────────────────────────────────────────────────────

class TestVersionTuple:
    def test_plain_numeric(self):
        assert updates.version_tuple("0.41.0") == (0, 41, 0)
        assert updates.version_tuple("1.2") == (1, 2)

    @pytest.mark.parametrize("bad", ["0.42.0rc1", "0.42.0+dev", "unknown", "", None, "0.5.x"])
    def test_anything_not_plain_numeric_is_none(self, bad):
        """A dev or pre-release build must never be told to 'upgrade' to a release it is
        already ahead of, so an unparseable version disables the comparison entirely."""
        assert updates.version_tuple(bad) is None


# ── the floor signal ──────────────────────────────────────────────────────────────────────

class TestFloorFromProjectUrls:
    @pytest.mark.parametrize("label", [
        "Minimum supported version", "minimum-supported-version",
        "Minimum_Supported_Version", "MinimumSupportedVersion",
    ])
    def test_label_matched_normalized(self, label):
        """The key is a human-written display label, so matching is normalized. A floor that
        silently fails to parse because of a hyphen is worse than no floor at all."""
        urls = {label: "https://github.com/o/r/releases/tag/v0.40.0"}
        assert updates._floor_from_project_urls(urls) == "0.40.0"

    def test_tag_without_v_prefix(self):
        assert updates._floor_from_project_urls(
            {"Minimum supported version": "https://x/releases/tag/0.40.0"}) == "0.40.0"

    def test_absent_key_means_no_floor(self):
        assert updates._floor_from_project_urls({"Homepage": "https://x"}) is None

    @pytest.mark.parametrize("bad", [
        {"Minimum supported version": "not-a-url"},
        {"Minimum supported version": "https://x/releases/tag/vNEXT"},
        {"Minimum supported version": 7},
        None, [], "string",
    ])
    def test_malformed_reads_as_no_floor(self, bad):
        """A metadata typo must never be able to tell every user their install is
        unsupported, so anything unparseable degrades to 'no floor'."""
        assert updates._floor_from_project_urls(bad) is None


# ── fetching ──────────────────────────────────────────────────────────────────────────────

class _Resp:
    def __init__(self, payload):
        self._payload = json.dumps(payload).encode()

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _serve(monkeypatch, payload):
    _stub_urlopen(monkeypatch, lambda *a, **k: _Resp(payload))


class TestFetch:
    def test_returns_latest_and_floor(self, monkeypatch):
        _serve(monkeypatch, {"info": {"version": "0.43.0", "project_urls": {
            "Minimum supported version": "https://x/releases/tag/v0.40.0"}}})
        assert updates.fetch() == {"latest": "0.43.0", "floor": "0.40.0"}

    def test_no_project_urls_is_no_floor(self, monkeypatch):
        _serve(monkeypatch, {"info": {"version": "0.43.0"}})
        assert updates.fetch() == {"latest": "0.43.0", "floor": None}

    def test_unparseable_latest_is_a_failure(self, monkeypatch):
        _serve(monkeypatch, {"info": {"version": "not-a-version"}})
        assert updates.fetch() is None

    def test_network_error_returns_none(self, monkeypatch):
        def _boom(*a, **k):
            raise OSError("no route to host")
        _stub_urlopen(monkeypatch, _boom)
        assert updates.fetch() is None

    def test_opt_out_returns_before_any_io(self, monkeypatch):
        monkeypatch.setenv("CONTEXER_NO_UPDATE_CHECK", "1")
        assert updates.fetch() is None  # the autouse urlopen would have raised



# ── state ─────────────────────────────────────────────────────────────────────────────────

class TestState:
    def test_roundtrip(self):
        assert updates.write_state({"latest": "1.2.3"}) is True
        assert updates.read_state() == {"latest": "1.2.3"}

    def test_absent_reads_empty(self):
        assert updates.read_state() == {}

    def test_corrupt_reads_empty(self):
        updates.state_path().write_text("{not json", encoding="utf-8")
        assert updates.read_state() == {}

    def test_non_dict_reads_empty(self):
        updates.state_path().write_text("[1, 2]", encoding="utf-8")
        assert updates.read_state() == {}

    def test_unwritable_reports_false_without_raising(self, monkeypatch):
        """A read-only ~/.contexer makes the notice repeat, which is the mild failure. It
        must never break the hook that was merely trying to be helpful."""
        monkeypatch.setattr(store, "atomic_write", lambda *a: (_ for _ in ()).throw(OSError))
        assert updates.write_state({"a": 1}) is False


class TestStaleness:
    def test_missing_state_is_stale(self):
        assert updates.is_stale({}) is True

    def test_fresh_is_not_stale(self):
        assert updates.is_stale({"checked_at": time.time()}) is False

    def test_expired_is_stale(self):
        assert updates.is_stale({"checked_at": time.time() - updates.TTL_SECONDS - 1}) is True

    def test_future_timestamp_is_stale(self):
        """A restored machine or a VM snapshot can leave a timestamp in the future, which
        would otherwise pin the state as fresh forever."""
        assert updates.is_stale({"checked_at": time.time() + 10_000}) is True

    def test_garbage_timestamp_is_stale(self):
        assert updates.is_stale({"checked_at": "yesterday"}) is True


class TestRefresh:
    def test_fresh_state_is_not_refetched(self, monkeypatch):
        updates.write_state(_state(latest="1.0.0"))
        monkeypatch.setattr(updates, "fetch",
                            lambda: pytest.fail("fetched despite fresh state"))
        assert updates.refresh()["latest"] == "1.0.0"

    def test_force_refetches_a_fresh_state(self, monkeypatch):
        updates.write_state(_state(latest="1.0.0"))
        monkeypatch.setattr(updates, "fetch", lambda: {"latest": "2.0.0", "floor": None})
        assert updates.refresh(force=True)["latest"] == "2.0.0"

    def test_failed_fetch_still_advances_checked_at(self, monkeypatch):
        """Without this an offline machine re-spawns a doomed refresher on every prompt
        forever. One attempt per TTL, offline or not."""
        updates.write_state({"latest": "1.0.0", "checked_at": 0})
        monkeypatch.setattr(updates, "fetch", lambda: None)
        state = updates.refresh()
        assert state["latest"] == "1.0.0"      # the last known answer is kept
        assert not updates.is_stale(state)     # but we will not retry until the TTL is up

    def test_withdrawn_floor_disappears(self, monkeypatch):
        """A floor removed upstream must clear here too, or a machine stays permanently
        breached against a rule that no longer exists."""
        updates.write_state({"latest": "1.0.0", "floor": "0.9.0", "checked_at": 0})
        monkeypatch.setattr(updates, "fetch", lambda: {"latest": "1.0.0", "floor": None})
        assert updates.refresh()["floor"] is None

    def test_opt_out_does_nothing(self, monkeypatch):
        monkeypatch.setenv("CONTEXER_NO_UPDATE_CHECK", "1")
        assert updates.refresh(force=True) == {}
        assert updates.read_state() == {}


class TestSpawnRefresh:
    def test_fresh_state_does_not_spawn(self):
        updates.write_state(_state())
        updates.spawn_refresh()  # the autouse Popen would have raised

    def test_opt_out_does_not_spawn(self, monkeypatch):
        monkeypatch.setenv("CONTEXER_NO_UPDATE_CHECK", "1")
        updates.spawn_refresh()

    def test_stale_state_spawns_detached(self, monkeypatch):
        calls = []
        _stub_popen(monkeypatch, lambda *a, **k: calls.append((a, k)))
        updates.spawn_refresh()
        (argv,), kwargs = calls[0]
        assert argv[1:] == ["-m", "contexer.updates"]
        assert kwargs["start_new_session"] is True

    def test_spawn_failure_is_swallowed(self, monkeypatch):
        def _boom(*a, **k):
            raise OSError("fork failed")
        _stub_popen(monkeypatch, _boom)
        updates.spawn_refresh()


# ── the policy ────────────────────────────────────────────────────────────────────────────

class TestNotice:
    def test_routine_notice_fires_once_per_release(self, monkeypatch):
        _pin_installed(monkeypatch, "1.0.0")
        updates.write_state(_state(latest="2.0.0"))
        assert "2.0.0 is available" in _notice()
        assert _notice() is None

    def test_a_newer_release_fires_again_once_the_window_has_passed(self, monkeypatch):
        """It fires again for a NEW release, but not before MIN_NOTICE_INTERVAL. The
        held-back case is pinned separately below."""
        _pin_installed(monkeypatch, "1.0.0")
        updates.write_state(_state(latest="2.0.0"))
        _notice()
        updates.write_state({**updates.read_state(), "latest": "3.0.0",
                             "notified_at": time.time() - updates.MIN_NOTICE_INTERVAL - 1})
        assert "3.0.0 is available" in _notice()

    def test_a_second_release_inside_the_window_is_held_back(self, monkeypatch):
        """The rule that makes this bearable at 48 releases in 90 days. Without it a developer
        sees roughly 16 update lines a month, which is the nagging the constraint forbids."""
        _pin_installed(monkeypatch, "1.0.0")
        updates.write_state(_state(latest="2.0.0"))
        assert _notice() is not None
        updates.write_state({**updates.read_state(), "latest": "3.0.0"})
        assert _notice() is None

    def test_a_held_back_notice_is_not_lost(self, monkeypatch):
        """Suppressed is not consumed. It fires once the window passes, naming whatever is
        latest by then, so the developer is at most one window behind the news."""
        _pin_installed(monkeypatch, "1.0.0")
        updates.write_state(_state(latest="2.0.0"))
        _notice()
        state = updates.read_state()
        state["latest"] = "3.0.0"
        state["notified_at"] = time.time() - updates.MIN_NOTICE_INTERVAL - 1
        updates.write_state(state)
        assert "3.0.0 is available" in _notice()

    def test_a_floor_breach_ignores_the_window(self, monkeypatch):
        """A floor is declared only when running an older build is a real risk. That is worth
        interrupting for."""
        _pin_installed(monkeypatch, "1.0.0")
        updates.write_state(_state(latest="2.0.0"))
        _notice()                                   # spends the window
        updates.write_state({**updates.read_state(), "floor": "1.5.0"})
        assert "below the minimum supported version" in _notice()

    def test_silent_when_current(self, monkeypatch):
        _pin_installed(monkeypatch, "2.0.0")
        updates.write_state(_state(latest="2.0.0"))
        assert _notice() is None

    def test_silent_when_installed_is_ahead(self, monkeypatch):
        """A local build ahead of PyPI must never be told to downgrade."""
        _pin_installed(monkeypatch, "3.0.0")
        updates.write_state(_state(latest="2.0.0"))
        assert _notice() is None

    def test_silent_when_installed_version_unknown(self, monkeypatch):
        _pin_installed(monkeypatch, None)
        updates.write_state(_state(latest="2.0.0"))
        assert _notice() is None

    def test_silent_with_no_state(self, monkeypatch):
        _pin_installed(monkeypatch, "1.0.0")
        assert _notice() is None

    def test_opt_out_silences_even_a_ready_state(self, monkeypatch):
        _pin_installed(monkeypatch, "1.0.0")
        updates.write_state(_state(latest="2.0.0"))
        monkeypatch.setenv("CONTEXER_NO_UPDATE_CHECK", "1")
        assert _notice() is None

    def test_breach_outranks_the_routine_notice(self, monkeypatch):
        _pin_installed(monkeypatch, "1.0.0")
        updates.write_state(_state(latest="2.0.0", floor="1.5.0"))
        assert "below the minimum supported version 1.5.0" in _notice()

    def test_breach_suppresses_the_routine_notice_afterwards(self, monkeypatch):
        """'Replaces' has to hold across turns. A breach followed next prompt by a routine
        'an update is available' delivers the same instruction twice in a weaker voice."""
        _pin_installed(monkeypatch, "1.0.0")
        updates.write_state(_state(latest="2.0.0", floor="1.5.0"))
        _notice()
        assert _notice() is None

    def test_a_new_floor_fires_again(self, monkeypatch):
        _pin_installed(monkeypatch, "1.0.0")
        updates.write_state(_state(latest="2.0.0", floor="1.5.0"))
        _notice()
        updates.write_state({**updates.read_state(), "floor": "1.8.0"})
        assert "1.8.0" in _notice()

    def test_no_breach_when_at_or_above_the_floor(self, monkeypatch):
        _pin_installed(monkeypatch, "1.5.0")
        updates.write_state(_state(latest="1.5.0", floor="1.5.0"))
        assert _notice() is None

    def test_unparseable_floor_is_ignored(self, monkeypatch):
        _pin_installed(monkeypatch, "1.0.0")
        updates.write_state(_state(latest="1.0.0", floor="whenever"))
        assert _notice() is None


class TestDeliver:
    """`deliver` is where the seam and the state meet, and the ORDER is the contract."""

    def test_renders_and_records(self, monkeypatch):
        _pin_installed(monkeypatch, "1.0.0")
        updates.write_state(_state(latest="2.0.0"))
        assert updates.deliver(lambda text: {"shown": text})["shown"].startswith("Contexer 2.0.0")
        assert updates.deliver(lambda text: {"shown": text}) is None   # consumed

    def test_a_host_with_no_channel_does_not_burn_the_notice(self, monkeypatch):
        """The finding this ordering exists for. Consuming before rendering would leave a
        Cursor or Gemini developer never told, and nothing for the backstop to say."""
        _pin_installed(monkeypatch, "1.0.0")
        updates.write_state(_state(latest="2.0.0"))
        assert updates.deliver(lambda _text: None) is None
        assert updates.read_state().get("notified") is None   # still owed
        assert "2.0.0 is available" in _notice()       # and still deliverable

    def test_silent_when_nothing_is_due(self, monkeypatch):
        _pin_installed(monkeypatch, "1.0.0")
        updates.write_state(_state(latest="1.0.0"))
        assert updates.deliver(lambda _t: pytest.fail("rendered with nothing due")) is None

    def test_opt_out_never_renders(self, monkeypatch):
        _pin_installed(monkeypatch, "1.0.0")
        updates.write_state(_state(latest="2.0.0"))
        monkeypatch.setenv("CONTEXER_NO_UPDATE_CHECK", "1")
        assert updates.deliver(lambda _t: pytest.fail("rendered despite opt-out")) is None

    def test_does_not_spawn_its_own_refresh(self):
        """The caller owns the refresh, because a hook may yield to something better this
        prompt and never reach deliver at all. The autouse Popen stub would raise."""
        updates.deliver(lambda _t: None)


# ── install-method detection ──────────────────────────────────────────────────────────────

class _Dist:
    """A stand-in for importlib.metadata's Distribution.

    `direct_url.json` is written to a real file and resolved through `files` /
    `locate_file`, mirroring how the code actually finds it: a stub that just returned the
    string would pass even if the lookup were broken."""

    def __init__(self, location, direct_url=None, tmp_path=None):
        self._location = location
        self._entry = None
        if direct_url is not None:
            written = tmp_path / "direct_url.json"
            written.write_text(direct_url, encoding="utf-8")
            self._entry = written

    def locate_file(self, entry):
        return self._entry if entry is self._entry else self._location

    @property
    def files(self):
        return [self._entry] if self._entry is not None else []


def _dist(monkeypatch, dist):
    monkeypatch.setattr(updates, "distribution", lambda _: dist)


class TestInstallMethod:
    def test_source_install_detected_via_direct_url(self, monkeypatch, tmp_path):
        """The case this detection exists for: `uv tool install --from <clone>` is
        indistinguishable from a PyPI install by path shape, and upgrading it would replace a
        developer's own build with a release."""
        _dist(monkeypatch, _Dist("/home/me/.local/share/uv/tools/contexer/lib/site-packages",
                                 json.dumps({"url": "file:///home/me/src/contexer"}), tmp_path))
        assert updates.install_method() == (updates.UV_TOOL_SOURCE, "/home/me/src/contexer")

    def test_uv_tool_install_from_pypi(self, monkeypatch, tmp_path):
        _dist(monkeypatch, _Dist("/home/me/.local/share/uv/tools/contexer/lib/site-packages",
                                 json.dumps({"url": "https://files.pythonhosted.org/x.whl"}),
                                 tmp_path))
        assert updates.install_method()[0] == updates.UV_TOOL

    def test_uv_tool_without_direct_url(self, monkeypatch):
        _dist(monkeypatch, _Dist("/home/me/.local/share/uv/tools/contexer/lib/site-packages"))
        assert updates.install_method()[0] == updates.UV_TOOL

    def test_plain_site_packages_is_pip(self, monkeypatch):
        _dist(monkeypatch, _Dist("/usr/lib/python3.12/site-packages"))
        assert updates.install_method() == (updates.PIP, "/usr/lib/python3.12/site-packages")

    def test_malformed_direct_url_falls_through(self, monkeypatch, tmp_path):
        _dist(monkeypatch, _Dist("/usr/lib/python3.12/site-packages", "{not json", tmp_path))
        assert updates.install_method()[0] == updates.PIP

    def test_uninstallable_introspection_is_unknown(self, monkeypatch):
        def _boom(_):
            raise ValueError("no dist")
        monkeypatch.setattr(updates, "distribution", _boom)
        assert updates.install_method() == (updates.UNKNOWN, "")

    def test_only_a_uv_tool_pypi_install_may_be_run_for_you(self):
        """One cascade, in one place. Splitting "which command" from "may we run it" let the
        two disagree about the same install."""
        assert updates.upgrade_plan(updates.UV_TOOL) == (True, "uv tool upgrade contexer", "")
        for method in (updates.UV_TOOL_SOURCE, updates.PIP, updates.UNKNOWN):
            runnable, command, why_not = updates.upgrade_plan(method, "/some/path")
            assert runnable is False, method
            assert command and why_not, method

    def test_source_plan_points_at_the_clone_it_came_from(self):
        runnable, command, why_not = updates.upgrade_plan(updates.UV_TOOL_SOURCE, "/home/me/src")
        assert command == "bash /home/me/src/scripts/install.sh"
        assert "not from PyPI" in why_not


# ── the adapter seam ──────────────────────────────────────────────────────────────────────

class TestNotifySeam:
    def test_claude_renders_system_message(self):
        """systemMessage is user-facing only, so the notice costs zero tokens and does not
        depend on the model choosing to mention it. Fields, not a finished JSON string, so
        one hook can carry a notice alongside its own output."""
        assert claude.notify("hi") == {"systemMessage": "hi"}

    def test_codex_delegates_to_claude(self):
        assert codex.notify("hi") == claude.notify("hi")

    def test_gemini_renders_system_message(self):
        """Gemini's hook reference lists systemMessage as "Displayed immediately to the user
        in the terminal", and suppressOutput does not suppress it."""
        assert gemini.notify("hi") == {"systemMessage": "hi"}

    def test_cursor_has_no_channel(self):
        """Verified against Cursor's hooks docs, not assumed: no systemMessage and no
        notification API, and `user_message` is shown only when an action is BLOCKED. The
        terminal backstop covers Cursor."""
        assert cursor.notify("hi") is None

    def test_every_adapter_implements_the_seam(self):
        """The seam only answers 'and future tools as well' if it is part of the contract
        every adapter satisfies, rather than something two of them happen to have."""
        from contexer import adapters
        for adapter in adapters.all_adapters():
            assert callable(getattr(adapter, "notify", None)), adapter.NAME

    def test_empty_text_renders_nothing(self):
        assert claude.notify("") is None


class TestRegressionsFromTheSecondReview:
    """Each of these shipped once and was caught by review. They stay pinned."""

    def test_a_failing_refresh_does_not_discard_the_recall_payload(self, monkeypatch):
        """The recall payload is already built when the update work runs. An exception there
        is bookkeeping failing on top of context that was ready to send."""
        monkeypatch.setattr(claude, "_recall_payload",
                            lambda *a: {"systemMessage": "Contexer: recalled 2 decisions"})
        monkeypatch.setattr(updates, "spawn_refresh",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        payload = json.loads(claude.rationale("", ""))
        assert payload["systemMessage"] == "Contexer: recalled 2 decisions"

    def test_a_failing_deliver_does_not_discard_the_recall_payload(self, monkeypatch):
        monkeypatch.setattr(claude, "_recall_payload", lambda *a: {"systemMessage": "recall"})
        monkeypatch.setattr(updates, "deliver",
                            lambda _r: (_ for _ in ()).throw(RuntimeError("boom")))
        assert json.loads(claude.rationale("", ""))["systemMessage"] == "recall"

    def test_a_failing_render_leaves_the_notice_owed(self, monkeypatch):
        """`deliver` records the notice only after the renderer succeeds. A print that raises
        must not consume it, which is the whole reason the CLI backstop goes through deliver
        rather than reading the text and printing it."""
        _pin_installed(monkeypatch, "1.0.0")
        updates.write_state(_state(latest="2.0.0"))

        def _explode(_text):
            raise OSError("broken pipe")

        with pytest.raises(OSError):
            updates.deliver(_explode)
        assert updates.read_state().get("notified") is None
        assert "2.0.0 is available" in _notice()

    def test_every_adapter_annotates_notify_the_same_way(self):
        """codex shipped annotated `-> str | None` while returning Claude's dict."""
        import inspect
        from contexer import adapters
        for adapter in adapters.all_adapters():
            sig = inspect.signature(adapter.notify)
            assert str(sig.return_annotation) == "dict | None", adapter.NAME


class TestCursorKeepsTheStateWarm:
    """Cursor cannot show a notice, but it must still refresh, or the terminal backstop has
    nothing to say on the developer's first `contexer` command."""

    def test_prompt_hook_spawns_the_refresh(self, monkeypatch):
        spawned = []
        _stub_popen(monkeypatch, lambda *a, **k: spawned.append(1))
        monkeypatch.setattr(cursor, "_repo_from_verbose", lambda *a: ("", ""))
        cursor.capture_constraint("", "{}")
        assert spawned == [1]

    def test_it_renders_nothing(self, monkeypatch):
        """Cursor has no channel. The refresh is state only, never output."""
        _pin_installed(monkeypatch, "1.0.0")
        updates.write_state(_state(latest="2.0.0"))
        monkeypatch.setattr(cursor, "_repo_from_verbose", lambda *a: ("", ""))
        assert json.loads(cursor.capture_constraint("", "{}")) == {"continue": True}
        assert "2.0.0 is available" in _notice()   # untouched, still owed

    def test_a_failing_refresh_never_costs_the_capture(self, monkeypatch):
        captured = []
        monkeypatch.setattr(updates, "spawn_refresh",
                            lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(cursor, "_repo_from_verbose", lambda *a: ("/repo", "hook-arg"))
        monkeypatch.setattr(cursor, "_anchor_current_repo", lambda _r: None)
        monkeypatch.setattr(cursor.evidence, "capture_directive",
                            lambda *a, **k: captured.append(1))
        cursor.capture_constraint("", "{}")
        assert captured == [1]


class TestGeminiSessionStartCarriesTheNotice:
    """Gemini takes the notice at SessionStart, the event whose docs name systemMessage."""

    def test_notice_rides_alongside_the_context(self, monkeypatch):
        _pin_installed(monkeypatch, "1.0.0")
        updates.write_state(_state(latest="2.0.0"))
        monkeypatch.setattr(gemini.store, "hook_repo_from_stdin", lambda *a: "/repo")
        monkeypatch.setattr(gemini, "_anchor", lambda _r: None)
        monkeypatch.setattr(gemini.store, "source_from_hook_stdin", lambda _r: "startup")
        monkeypatch.setattr(gemini, "_session_marker", lambda _r: None)
        monkeypatch.setattr(gemini.store, "session_start_payload",
                            lambda *a, **k: {"context": "stored rules"})
        out = json.loads(gemini.session_start("", "{}"))
        assert "2.0.0 is available" in out["systemMessage"]
        # The model still gets its context: suppressOutput does not suppress systemMessage.
        assert out["hookSpecificOutput"]["additionalContext"] == "stored rules"
        assert out["suppressOutput"] is True

    def test_notice_still_delivered_outside_a_repo(self, monkeypatch):
        """Update state is machine-global, so a session in a non-repo directory is still told."""
        _pin_installed(monkeypatch, "1.0.0")
        updates.write_state(_state(latest="2.0.0"))
        monkeypatch.setattr(gemini.store, "hook_repo_from_stdin", lambda *a: "")
        out = json.loads(gemini.session_start("", "{}"))
        assert "2.0.0 is available" in out["systemMessage"]

    def test_silent_when_nothing_is_due(self, monkeypatch):
        _pin_installed(monkeypatch, "1.0.0")
        updates.write_state(_state(latest="1.0.0"))
        monkeypatch.setattr(gemini.store, "hook_repo_from_stdin", lambda *a: "")
        assert json.loads(gemini.session_start("", "{}")) == {"suppressOutput": True}

    def test_a_failing_notice_never_costs_the_context(self, monkeypatch):
        _pin_installed(monkeypatch, "1.0.0")
        monkeypatch.setattr(updates, "deliver",
                            lambda _r: (_ for _ in ()).throw(RuntimeError("boom")))
        monkeypatch.setattr(gemini.store, "hook_repo_from_stdin", lambda *a: "/repo")
        monkeypatch.setattr(gemini, "_anchor", lambda _r: None)
        monkeypatch.setattr(gemini.store, "source_from_hook_stdin", lambda _r: "startup")
        monkeypatch.setattr(gemini, "_session_marker", lambda _r: None)
        monkeypatch.setattr(gemini.store, "session_start_payload",
                            lambda *a, **k: {"context": "stored rules"})
        out = json.loads(gemini.session_start("", "{}"))
        assert out["hookSpecificOutput"]["additionalContext"] == "stored rules"
        assert "systemMessage" not in out


class TestRationaleCarriesTheNotice:
    """The notice rides the existing rationale hook rather than a hook of its own: no extra
    per-prompt process, and one `systemMessage` slot for the whole turn."""

    def test_emits_the_notice_when_recall_has_nothing_to_say(self, monkeypatch):
        _pin_installed(monkeypatch, "1.0.0")
        updates.write_state(_state(latest="2.0.0"))
        monkeypatch.setattr(claude, "_recall_payload", lambda *a: {})
        payload = json.loads(claude.rationale("", ""))
        assert "2.0.0 is available" in payload["systemMessage"]

    def test_recall_wins_and_the_notice_stays_owed(self, monkeypatch):
        """Two Contexer lines in one turn is the noise the silent-operation constraint exists
        to prevent. Recall explains what the developer just asked; a release never does."""
        _pin_installed(monkeypatch, "1.0.0")
        updates.write_state(_state(latest="2.0.0"))
        monkeypatch.setattr(claude, "_recall_payload",
                            lambda *a: {"systemMessage": "Contexer: recalled 2 decisions"})
        payload = json.loads(claude.rationale("", ""))
        assert payload["systemMessage"] == "Contexer: recalled 2 decisions"
        assert "2.0.0 is available" in _notice()   # not consumed by yielding

    def test_emits_empty_json_when_neither_has_anything(self, monkeypatch):
        _pin_installed(monkeypatch, "1.0.0")
        updates.write_state(_state(latest="1.0.0"))
        monkeypatch.setattr(claude, "_recall_payload", lambda *a: {})
        assert claude.rationale("", "") == "{}"

    def test_keeps_the_state_warm_even_when_yielding(self, monkeypatch):
        """A developer who asks rationale questions constantly must still get a refresh."""
        _pin_installed(monkeypatch, "1.0.0")
        spawned = []
        _stub_popen(monkeypatch, lambda *a, **k: spawned.append(1))
        monkeypatch.setattr(claude, "_recall_payload", lambda *a: {"systemMessage": "recall"})
        claude.rationale("", "")
        assert spawned == [1]

    def test_never_raises(self, monkeypatch):
        """A hook that raises costs the developer something real in exchange for news that
        was never urgent."""
        def _boom(*a, **k):
            raise RuntimeError("state exploded")
        monkeypatch.setattr(claude, "_recall_payload", _boom)
        assert claude.rationale("", "") == "{}"
