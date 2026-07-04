"""E2E (I2): live delta-poll + approval flow -- the headline loop.

HERMETIC TEST: a `FakeTeamsServer` (see conftest `team_stack`) stands in for Teams. Proves
that a decision approved team-side surfaces in the NEXT prompt of a live session via the
UserPromptSubmit delta-poll -- exercising the initial pull, an approval, the throttled delta
poll, and the `team_poll` injector, with only the network hop faked.

MANUAL RUNBOOK (real stack):
  Prereqs as in test_e2e_two_clones.py, PLUS `contexer reinstall` so the `team_poll`
  UserPromptSubmit hook is active in Claude Code.
  1. In a team-mode repo (logged in), start a Claude session -> SessionStart pulls team
     context (visible via get_context "## Team context (synced)").
  2. In the web review queue, approve a NEW team decision.
  3. Send the NEXT prompt in the live session -> the hook injects:
     "Team decisions just approved (now in effect): - <decision>".
  4. Send another prompt within ~15s -> no re-injection (throttle). After the window, a
     further approval injects on the next prompt. Confirm no perceptible per-prompt latency.
"""
import json

from contexer import config, store, team_context
from contexer.adapters import claude

TEAM = config.Profile(mode="team", endpoint="http://fake/mcp", token="tok")
REPO = "/work/live-clone"


def test_approve_mid_session_injects_on_next_prompt(team_stack, monkeypatch):
    server = team_stack
    # Session start: an existing team decision, pulled (establishes the cursor).
    server.add_team_decision("use conventional commits")
    assert team_context.pull(REPO, profile=TEAM)[0] == 1

    # A lead approves a NEW decision mid-session.
    server.add_team_decision("never log secrets", type="constraint")

    # Next prompt: the UserPromptSubmit hook injects ONLY the newly-approved decision.
    monkeypatch.setattr(store, "_resolve_repo", lambda p: REPO)
    monkeypatch.setattr("contexer.config.load_profile", lambda: TEAM)
    data = json.loads(claude.team_poll(REPO, ""))
    injected = data["hookSpecificOutput"]["additionalContext"]
    assert "never log secrets" in injected
    assert "use conventional commits" not in injected  # already had it; only NEW surfaces


def test_first_poll_after_pull_returns_nothing(team_stack):
    server = team_stack
    server.add_team_decision("existing rule")
    team_context.pull(REPO, profile=TEAM)  # cursor advances past the existing rule
    assert team_context.poll(REPO, profile=TEAM) == []  # nothing new -> no re-injection


def test_delta_poll_is_throttled(team_stack):
    server = team_stack
    server.add_team_decision("first rule")
    team_context.pull(REPO, profile=TEAM)
    server.add_team_decision("second rule")
    assert [d["content"] for d in team_context.poll(REPO, profile=TEAM)] == ["second rule"]
    server.add_team_decision("third rule")
    assert team_context.poll(REPO, profile=TEAM) == []  # within the throttle window


def test_poll_degrades_when_cloud_unreachable(team_stack, monkeypatch):
    import contexer.remote as remote
    from contexer.remote import RemoteUnavailableError
    monkeypatch.setattr(remote, "_call_tool",
                        lambda *a, **k: (_ for _ in ()).throw(RemoteUnavailableError("down")))
    assert team_context.poll(REPO, profile=TEAM) == []  # no injection, no crash
