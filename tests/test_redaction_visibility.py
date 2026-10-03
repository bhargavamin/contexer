import asyncio
from dataclasses import replace

import pytest

from contexer import cli, config, share, share_policy, share_status, store
from tests.test_share import TEAM, _afake, _fake


OFF = replace(TEAM, redact_secrets=False, skip_confirm=True)


def test_preview_warns_even_with_zero_detected_secrets(tmp_repo):
    store.update_decision(tmp_repo, "Use PostgreSQL for the database", "s", "convention")
    preview = store.format_share_preview(tmp_repo, profile=OFF)
    assert "redaction is OFF" in preview
    assert "Confirm with the developer" in preview


def test_preview_resolves_disabled_profile_when_caller_omits_it(tmp_repo, monkeypatch):
    monkeypatch.setattr(config, "load_profile", lambda: OFF)
    store.update_decision(tmp_repo, "Use PostgreSQL for the database", "s", "convention")
    assert "redaction is OFF" in store.format_share_preview(tmp_repo)


def test_mcp_optout_cannot_skip_preview(tmp_repo, monkeypatch):
    store.update_decision(tmp_repo, "Use PostgreSQL for the database", "s", "convention")
    fake = _afake(monkeypatch)
    result = asyncio.run(share.share_decision_flow(tmp_repo, "", confirm=False, profile=OFF, timeout=10))
    assert "redaction is OFF" in result
    assert not fake.calls and not fake.batches
    result = asyncio.run(share.share_decision_flow(tmp_repo, "", confirm=True, profile=OFF, timeout=10))
    assert "redaction is OFF" in result
    assert len(fake.calls) == 1


def test_background_reconciliations_do_not_send_with_redaction_off(tmp_repo, monkeypatch):
    fake = _fake(monkeypatch)
    share._save_reconcile_outbox([{"operation_id": "queued", "decision": {"content": "token=secret"}}])
    before = share._read_reconcile_outbox()
    assert share._drain_reconciliation_outbox_unlocked(OFF) == 0
    assert share._read_reconcile_outbox() == before
    assert not fake.calls and not fake.batches


def test_reconcile_failure_also_warns(tmp_repo):
    result = share.reconcile(tmp_repo, "missing", profile=OFF)
    assert result.redaction_disabled
    assert "redaction is OFF" in share_status.describe(result)


@pytest.mark.parametrize("scope", ["one", "all", "ids", "global"])
def test_every_sync_result_warns(tmp_repo, monkeypatch, scope):
    _, did = store.update_decision(tmp_repo, "Use PostgreSQL for the database", "s", "convention")
    store.update_global_decision("Always run tests before commits", "s", "constraint")
    _fake(monkeypatch)
    calls = {"one": lambda: share.share(tmp_repo, did, profile=OFF),
             "all": lambda: share.share_all(tmp_repo, profile=OFF),
             "ids": lambda: share.share_ids(tmp_repo, [did], profile=OFF),
             "global": lambda: share.share_global(profile=OFF)}
    status = calls[scope]()
    assert status.redaction_disabled
    assert "redaction is OFF" in share_status.describe(status)


@pytest.mark.parametrize("args", [["--yes", "--all"], ["--yes", "--global"], ["--all"]])
def test_cli_requires_confirmation_despite_bypasses(tmp_repo, monkeypatch, capsys, args):
    store.update_decision(tmp_repo, "Use PostgreSQL for the database", "s", "convention")
    store.update_global_decision("Always run tests before commits", "s", "constraint")
    monkeypatch.setattr(config, "load_profile", lambda: OFF)
    monkeypatch.setattr(store, "git_root", lambda path: tmp_repo)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    fake = _fake(monkeypatch)
    cli.share_cmd(args)
    output = capsys.readouterr().out
    assert "redaction is OFF" in output and "Cancelled" in output
    assert not fake.calls and not fake.batches


def test_background_outbox_retains_pending_sends(tmp_repo, monkeypatch):
    fake = _fake(monkeypatch)
    share._enqueue({"id": "queued", "content": "password=secret-value", "repo": "r"})
    before = share._load_outbox()
    assert share.drain_outbox(OFF) == 0
    assert share._load_outbox() == before
    assert not fake.calls and not fake.batches


def test_cli_reconcile_warns_and_confirms_before_remote_preview(tmp_repo, monkeypatch, capsys):
    _, did = store.update_decision(tmp_repo, "Use PostgreSQL for the database", "s", "convention")
    monkeypatch.setattr(config, "load_profile", lambda: OFF)
    monkeypatch.setattr(store, "git_root", lambda path: tmp_repo)
    monkeypatch.setattr("builtins.input", lambda prompt: "n")
    calls = []
    monkeypatch.setattr(share, "prepare_reconciliation", lambda *a, **kw: calls.append(a))
    cli.reconcile_cmd([did, "--yes"])
    output = capsys.readouterr().out
    assert "redaction is OFF" in output and "Cancelled" in output
    assert calls == []


def test_auto_proposals_require_attention_when_redaction_off(monkeypatch):
    outcomes = []
    monkeypatch.setattr(share_policy, "_current_terminal_receipt", lambda intent: None)
    monkeypatch.setattr(share_policy, "_move_intent_to_attention", lambda intent, reason, stage, **kw: outcomes.append(reason) or share_policy.OperationOutcome("attention", reason))
    monkeypatch.setattr(share_policy, "_finish_drain", lambda outcome, *a, **kw: outcome)
    result = share_policy._drain_intent({}, OFF, "owner", queue_depth=1)
    assert result.reason_code == "redaction_disabled"
    assert outcomes == ["redaction_disabled"]


def test_console_requires_preview_before_unredacted_egress(tmp_repo, monkeypatch):
    from contexer.ui import api
    _, did = store.update_decision(tmp_repo, "Use PostgreSQL for the database", "s", "convention")
    monkeypatch.setattr(config, "load_profile", lambda: OFF)
    calls = []
    monkeypatch.setattr(share, "share_ids", lambda *a, **kw: calls.append(a) or share_status.ShareStatus(share_status.SYNCED))
    code, result = api._share(tmp_repo, {"ids": [did]})
    assert code == 200 and result["confirmation_required"]
    assert "redaction is OFF" in result["preview"] and calls == []
    api._share(tmp_repo, {"ids": [did], "confirm": "true"})
    assert calls == []
    api._share(tmp_repo, {"ids": [did], "confirm": True})
    assert len(calls) == 1


def test_redaction_attention_does_not_pause_destination_policy():
    assert "redaction_disabled" not in share_policy._PAUSING_DRAIN_REASONS


def test_unredacted_failure_reports_paused_retry():
    text = share_status.describe(share_status.ShareStatus(share_status.QUEUED, redaction_disabled=True))
    assert "retry is paused" in text and "retry automatically" not in text


def test_unredacted_preview_does_not_offer_ineffective_bypass(tmp_repo):
    store.update_decision(tmp_repo, "Use PostgreSQL for the database", "s", "convention")
    assert "Stop asking" not in store.format_share_preview(tmp_repo, profile=OFF)
    preview = store.format_share_preview(tmp_repo, profile=OFF, purpose="reconcile")
    assert "PERSONAL" not in preview and "share_decision(" not in preview


def test_new_explicit_share_settles_old_queued_base(tmp_repo, monkeypatch):
    _, did = store.update_decision(tmp_repo, "Use PostgreSQL for the database", "s", "convention")
    share._enqueue({"decision_id": did, "content": "old body", "repo": "r"})
    _fake(monkeypatch)
    status = share.share(tmp_repo, did, profile=OFF)
    assert status.outcome == share_status.SYNCED
    assert not any(row.get("decision_id") == did for row in share._load_outbox())
