"""CLI and MCP contracts for explicit automatic-proposal policy control."""
import asyncio
import inspect
import re
import sys
import threading

import pytest

from contexer import cli, server, share_policy


def _preview(team_id="team-1"):
    return share_policy.PolicyActivationPreview(
        repo_path="/repo",
        policy={
            "endpoint": "https://mcp.contexer.ai/mcp",
            "account_fingerprint": "acctfp_v1_7M4Q2PX9C6N8",
            "repo_key": "github.com/org/repo",
            "team_id": team_id,
            "team_name_at_confirmation": "Platform",
            "baseline_revision_ids": ["revision-1"],
        },
        entries=[{
            "id": "decision-1",
            "type": "decision",
            "current_revision_id": "revision-1",
        }],
        include_existing=False,
        initial_proposal_count=0,
        baseline_count=1,
        redaction_enabled=True,
        replacing_policy=False,
    )


def _patch_cli_repo(monkeypatch):
    from contexer import store

    monkeypatch.setattr(store, "git_root", lambda _path: "/repo")
    monkeypatch.setattr(store, "resolve_repo", lambda _path: "/repo")


def _mcp(**kwargs):
    return asyncio.run(server.manage_share_policy(**kwargs))


def test_cli_enable_requires_visible_confirmation_and_fresh_binding(
        monkeypatch, capsys):
    _patch_cli_repo(monkeypatch)
    previews = []
    preview = _preview()

    def prepare(*_args, **_kwargs):
        previews.append(True)
        return preview, share_policy.OperationOutcome("success", "none")

    activated = []
    monkeypatch.setattr(share_policy, "prepare_policy_activation", prepare)
    monkeypatch.setattr(
        share_policy, "activate_policy",
        lambda value: activated.append(value) or share_policy.ScanOutcome(
            "success", "none"),
    )
    monkeypatch.setattr(share_policy, "policy_status", lambda _repo: {})
    monkeypatch.setattr(share_policy, "format_policy_status", lambda _snapshot: "policy active")
    monkeypatch.setattr("builtins.input", lambda _prompt="": "yes")

    cli.share_policy_cmd(["enable", "--team", "Platform"])

    output = capsys.readouterr().out
    assert "Automatic decision proposal policy preview" in output
    assert "Team approval remains manual" in output
    assert len(previews) == 2
    assert activated == [preview]


def test_cli_enable_reports_durable_intents_when_detached_launch_fails(
        monkeypatch, capsys):
    _patch_cli_repo(monkeypatch)
    preview = _preview()
    monkeypatch.setattr(
        share_policy, "prepare_policy_activation",
        lambda *_args, **_kwargs: (
            preview, share_policy.OperationOutcome("success", "none")),
    )
    monkeypatch.setattr(
        share_policy, "activate_policy",
        lambda _preview: share_policy.ScanOutcome(
            "queued", "validation_error", scanned=1, queued=1),
    )
    monkeypatch.setattr(share_policy, "policy_status", lambda _repo: {})
    monkeypatch.setattr(share_policy, "format_policy_status", lambda _snapshot: "policy active")
    monkeypatch.setattr("builtins.input", lambda _prompt="": "yes")

    cli.share_policy_cmd(["enable", "--team", "Platform", "--include-existing"])

    output = capsys.readouterr().out
    assert "initial intents are durable" in output
    assert "detached uploader process did not start" in output
    assert "flush" in output


def test_cli_enable_decline_changes_nothing(monkeypatch, capsys):
    _patch_cli_repo(monkeypatch)
    monkeypatch.setattr(
        share_policy, "prepare_policy_activation",
        lambda *_args, **_kwargs: (
            _preview(), share_policy.OperationOutcome("success", "none")),
    )
    monkeypatch.setattr(
        share_policy, "activate_policy",
        lambda _preview: pytest.fail("declined preview must not activate"),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": "no")

    cli.share_policy_cmd(["enable", "--team", "Platform"])

    assert "Cancelled - the policy was not changed" in capsys.readouterr().out


def test_cli_enable_refuses_when_binding_changes_after_confirmation(monkeypatch, capsys):
    _patch_cli_repo(monkeypatch)
    previews = iter((_preview("team-1"), _preview("team-2")))
    monkeypatch.setattr(
        share_policy, "prepare_policy_activation",
        lambda *_args, **_kwargs: (
            next(previews), share_policy.OperationOutcome("success", "none")),
    )
    monkeypatch.setattr(
        share_policy, "activate_policy",
        lambda _preview: pytest.fail("changed destination must not activate"),
    )
    monkeypatch.setattr("builtins.input", lambda _prompt="": "yes")

    with pytest.raises(SystemExit) as exc:
        cli.share_policy_cmd(["enable", "--team", "Platform"])

    assert exc.value.code == 1
    assert "changed during confirmation" in capsys.readouterr().out


def test_cli_help_says_skip_confirm_cannot_bypass_policy_confirmation(capsys):
    cli.share_policy_cmd(["--help"])
    output = capsys.readouterr().out
    assert "skip_confirm" in output
    assert "does not bypass" in output
    assert "--include-existing" in output


def test_main_dispatches_share_policy(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "share_policy_cmd", lambda args: calls.append(args))
    monkeypatch.setattr(sys, "argv", ["contexer", "share-policy", "show"])

    cli.main()

    assert calls == [["show"]]


def test_mcp_policy_control_is_async_and_runs_sync_work_off_the_event_loop(monkeypatch):
    """The real MCP host invokes tools on its event loop. Policy preparation uses the
    synchronous RemoteStore shim, which owns an ``asyncio.run`` for CLI callers, so executing
    that shim on the MCP loop raises before a preview can render. Keep the established sync
    policy transaction intact, but run it on a worker where it cannot nest event loops or freeze
    unrelated MCP requests.
    """
    assert inspect.iscoroutinefunction(server.manage_share_policy)
    seen = {}

    def sync_policy_control(*args):
        seen["worker_thread"] = threading.current_thread()
        seen["args"] = args
        # Stands in for RemoteStore.get_capabilities/list_teams. This is the exact operation that
        # failed when manage_share_policy was a synchronous MCP tool called on the running loop.
        asyncio.run(asyncio.sleep(0))
        return "policy preview"

    monkeypatch.setattr(server, "_manage_share_policy", sync_policy_control)

    async def driver():
        seen["loop_thread"] = threading.current_thread()
        return await server.manage_share_policy(
            action="enable", repo_path="/repo", team="Platform")

    result = asyncio.run(driver())

    assert result == "policy preview"
    assert seen["worker_thread"] is not seen["loop_thread"]
    assert seen["args"] == ("enable", "/repo", "Platform", False, False, "", "")


def test_mcp_enable_is_two_call_and_never_activates_preview(monkeypatch):
    server._POLICY_CONFIRMATIONS.clear()
    monkeypatch.setattr(server.store, "resolve_repo", lambda _path: "/repo")
    preview = _preview()
    calls = []
    monkeypatch.setattr(
        share_policy, "prepare_policy_activation",
        lambda *_args, **_kwargs: (
            preview, share_policy.OperationOutcome("success", "none")),
    )
    monkeypatch.setattr(
        share_policy, "activate_policy",
        lambda value: calls.append(value) or share_policy.ScanOutcome("success", "none"),
    )
    monkeypatch.setattr(share_policy, "policy_status", lambda _repo: {})
    monkeypatch.setattr(share_policy, "format_policy_status", lambda _snapshot: "policy active")

    first = _mcp(action="enable", repo_path="/repo", team="Platform")

    assert "No policy has been changed yet" in first
    assert "confirm=true" in first
    assert calls == []
    token = re.search(r"confirmation_token=([A-Za-z0-9_-]+)", first).group(1)

    second = _mcp(
        action="enable", repo_path="/repo", team="Platform", confirm=True,
        confirmation_token=token)

    assert calls == [preview]
    assert "policy enabled" in second.lower()
    assert "Team approval remains manual" in second
    assert token not in server._POLICY_CONFIRMATIONS


def test_mcp_cannot_confirm_enable_without_a_prior_single_use_preview(monkeypatch):
    server._POLICY_CONFIRMATIONS.clear()
    monkeypatch.setattr(server.store, "resolve_repo", lambda _path: "/repo")
    monkeypatch.setattr(
        share_policy, "prepare_policy_activation",
        lambda *_args, **_kwargs: pytest.fail("direct confirmation must fail before network"),
    )
    monkeypatch.setattr(
        share_policy, "activate_policy",
        lambda _preview: pytest.fail("direct confirmation must not activate"),
    )

    result = _mcp(
        action="enable", repo_path="/repo", team="Platform", confirm=True)

    assert "single-use preview token" in result
    assert "No policy was changed" in result


def test_mcp_confirmation_is_consumed_and_repreviewed_when_binding_changes(monkeypatch):
    server._POLICY_CONFIRMATIONS.clear()
    monkeypatch.setattr(server.store, "resolve_repo", lambda _path: "/repo")
    previews = iter((_preview("team-1"), _preview("team-2")))
    monkeypatch.setattr(
        share_policy, "prepare_policy_activation",
        lambda *_args, **_kwargs: (
            next(previews), share_policy.OperationOutcome("success", "none")),
    )
    monkeypatch.setattr(
        share_policy, "activate_policy",
        lambda _preview: pytest.fail("changed binding must not activate"),
    )
    first = _mcp(
        action="enable", repo_path="/repo", team="Platform")
    token = re.search(r"confirmation_token=([A-Za-z0-9_-]+)", first).group(1)

    result = _mcp(
        action="enable", repo_path="/repo", team="Platform", confirm=True,
        confirmation_token=token)

    assert "changed since the developer's confirmation" in result
    assert token not in server._POLICY_CONFIRMATIONS
    assert "confirmation_token=" in result


def test_mcp_reports_saved_policy_as_enabled_when_activation_mirror_fails(monkeypatch):
    server._POLICY_CONFIRMATIONS.clear()
    monkeypatch.setattr(server.store, "resolve_repo", lambda _path: "/repo")
    monkeypatch.setattr(
        share_policy, "prepare_policy_activation",
        lambda *_args, **_kwargs: (
            _preview(), share_policy.OperationOutcome("success", "none")),
    )
    monkeypatch.setattr(
        share_policy, "activate_policy",
        lambda _preview: share_policy.ScanOutcome("success", "corrupt_queue"),
    )
    monkeypatch.setattr(
        share_policy, "policy_status",
        lambda _repo: {"policy": "active", "paused_reason": None},
    )
    monkeypatch.setattr(
        share_policy, "format_policy_status",
        lambda _snapshot: "policy active",
    )
    first = _mcp(
        action="enable", repo_path="/repo", team="Platform")
    token = re.search(r"confirmation_token=([A-Za-z0-9_-]+)", first).group(1)

    result = _mcp(
        action="enable", repo_path="/repo", team="Platform", confirm=True,
        confirmation_token=token)

    assert "policy enabled with its authoritative baseline saved" in result
    assert "policy active" in result
    assert "no unsafe action" not in result.lower()


def test_mcp_policy_status_and_retry_preserve_lifecycle_wording(monkeypatch):
    monkeypatch.setattr(server.store, "resolve_repo", lambda _path: "/repo")
    monkeypatch.setattr(
        share_policy, "policy_status",
        lambda _repo: {
            "policy": "active", "paused_reason": None, "queued": 1,
            "uploading": True, "pending_lead_review": 1, "already_current": 1,
            "attention": 1, "baseline": 0, "repo_key": None,
        },
    )
    status = _mcp(action="show", repo_path="/repo")
    assert "queued" in status and "uploading" in status
    assert "pending lead review" in status and "already current" in status
    assert "shared" not in status.lower()

    monkeypatch.setattr(
        share_policy, "retry_attention",
        lambda *_args: share_policy.OperationOutcome("queued", "none"),
    )
    retried = _mcp(
        action="retry", repo_path="/repo", intent_id="intent-1")
    assert "local proposal intent" in retried
    assert "shared" not in retried.lower()


def test_mcp_retry_reports_durable_intent_when_detached_launch_fails(monkeypatch):
    monkeypatch.setattr(server.store, "resolve_repo", lambda _path: "/repo")
    monkeypatch.setattr(
        share_policy,
        "retry_attention",
        lambda *_args: share_policy.OperationOutcome(
            "queued", "validation_error", "diag_4Z7K2N8Q5W1C9M6P"),
    )

    result = _mcp(
        action="retry", repo_path="/repo", intent_id="intent-1")

    assert "Retry queued" in result
    assert "detached uploader process did not start" in result
    assert "durable intent" in result
