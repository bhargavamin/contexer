"""Tests for MCP tool entry points in contexer/server.py.

share_decision is the one MCP tool that reaches the network from inside FastMCP's event
loop. It is async and offloads its blocking (sync) body to a worker thread the loop AWAITS,
so a slow/hung cloud never freezes the whole server. No pytest-asyncio: the async tool is
driven with asyncio.run(...), matching test_remote.py's convention.
"""
import asyncio
import inspect
import threading
import time

import contexer.share as share_mod
from contexer import server


def test_share_decision_is_async_tool():
    # Guards the fix: a regression back to a sync `def` would reintroduce the
    # asyncio.run-in-a-running-loop failure the MCP path hit.
    assert inspect.iscoroutinefunction(server.share_decision)


def test_share_decision_short_circuits_without_repo(monkeypatch):
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "")
    called = {"share": False}
    monkeypatch.setattr(share_mod, "share", lambda *a, **k: called.__setitem__("share", True))
    result = asyncio.run(server.share_decision("d", ""))
    assert result == "Skipped — repo path not detected."
    assert called["share"] is False  # never reaches the network layer


def test_share_decision_offloads_blocking_body_off_the_loop(monkeypatch):
    # The sync share() body must run on a DIFFERENT thread than the event loop, and its
    # return value + args must pass through unchanged.
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo/x")
    seen = {}

    def fake_share(repo, decision_id, **kw):
        seen["thread"] = threading.current_thread()
        seen["args"] = (repo, decision_id)
        return "shared srv-1"

    monkeypatch.setattr(share_mod, "share", fake_share)

    async def driver():
        seen["loop_thread"] = threading.current_thread()
        return await server.share_decision("dec-42", "/repo/x", confirm=True)

    result = asyncio.run(driver())
    assert result == "shared srv-1"
    assert seen["args"] == ("/repo/x", "dec-42")
    assert seen["thread"] is not seen["loop_thread"]


def test_share_decision_does_not_block_the_event_loop(monkeypatch):
    # While the blocking share() runs, the loop must stay free to make progress on other
    # coroutines. If share_decision froze the loop, no tick could land before share finished.
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo")
    order = []

    def slow_share(repo, decision_id, **kw):
        time.sleep(0.2)  # stands in for a slow network round-trip
        order.append("share_done")
        return "ok"

    monkeypatch.setattr(share_mod, "share", slow_share)

    async def ticker():
        for _ in range(5):
            await asyncio.sleep(0.02)
            order.append("tick")

    async def driver():
        await asyncio.gather(server.share_decision("d", "/repo", confirm=True), ticker())

    asyncio.run(driver())
    assert "tick" in order
    assert order.index("tick") < order.index("share_done")


def test_share_decision_times_out_without_hanging(monkeypatch):
    # A wedged share() (transport never returns) must not hang the tool: within the bounded
    # wait it returns a local-first degradation message instead of blocking forever. The
    # worker keeps running in the background (Python can't cancel a blocking thread), which
    # is why the fake sleeps a bounded 0.3s so the test process doesn't hang on exit.
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(server, "_SHARE_TIMEOUT", 0.03)
    started = threading.Event()

    def wedged_share(repo, decision_id, **kw):
        started.set()
        time.sleep(0.3)  # outlives the 0.03s backstop
        return "late"

    monkeypatch.setattr(share_mod, "share", wedged_share)

    result = asyncio.run(server.share_decision("d", "/repo", confirm=True))
    assert started.is_set()          # the offload did start
    assert result != "late"          # but the tool did NOT wait for it
    assert "Saved locally" in result and "did not respond" in result


# ── cloud-push preview gate + review_pending ─────────────────────────────────────
from contexer import config as _config_mod


def test_share_decision_previews_by_default_without_pushing(monkeypatch):
    # confirm=False (default) + skip_confirm off -> preview only, NOTHING is pushed.
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo")
    # Team-configured profile: the preview gate only fires when sharing is actually set up.
    monkeypatch.setattr(_config_mod, "load_profile",
                        lambda *a, **k: _config_mod.Profile(mode="team", endpoint="https://x/mcp"))
    monkeypatch.setattr(server.store, "format_share_preview", lambda r, d, profile=None: "PREVIEW-TEXT")
    pushed = {"n": 0}
    monkeypatch.setattr(share_mod, "share", lambda *a, **k: pushed.__setitem__("n", pushed["n"] + 1))

    result = asyncio.run(server.share_decision("ab12cd34", "/repo"))
    assert result == "PREVIEW-TEXT"
    assert pushed["n"] == 0  # dry run — nothing left the machine


def test_share_decision_skip_confirm_pushes_without_preview(monkeypatch):
    # A developer who set skip_confirm bypasses the preview even with confirm=False.
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(_config_mod, "load_profile",
                        lambda *a, **k: _config_mod.Profile(skip_confirm=True))
    monkeypatch.setattr(share_mod, "share", lambda repo, dec, **k: "pushed")

    result = asyncio.run(server.share_decision("ab12cd34", "/repo"))
    assert result == "pushed"


def test_share_decision_local_mode_skips_preview(monkeypatch):
    # #2: with no team configured, don't preview a push that would no-op — go straight to share().
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(_config_mod, "load_profile", lambda *a, **k: _config_mod.Profile())  # local
    previewed = {"n": 0}
    monkeypatch.setattr(server.store, "format_share_preview",
                        lambda *a, **k: previewed.__setitem__("n", previewed["n"] + 1) or "PREVIEW")
    monkeypatch.setattr(share_mod, "share", lambda repo, dec, **k: "not configured")

    result = asyncio.run(server.share_decision("ab12cd34", "/repo"))
    assert result == "not configured"
    assert previewed["n"] == 0  # never previewed in local mode


def test_review_pending_returns_identified_list(monkeypatch):
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "/repo")
    monkeypatch.setattr(server.store, "format_pending_review", lambda r: "PENDING-LIST")
    assert server.review_pending("/repo") == "PENDING-LIST"


def test_review_pending_no_repo(monkeypatch):
    monkeypatch.setattr(server.store, "_resolve_repo", lambda p: "")
    assert server.review_pending("") == "No repo path detected."
