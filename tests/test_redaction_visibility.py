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


def test_async_outbox_drain_also_pauses_with_redaction_off(tmp_repo, monkeypatch):
    """The in-loop MCP share drains the outbox first; it must hold queued rows like the sync drain."""
    fake = _afake(monkeypatch)
    share._enqueue({"id": "queued", "content": "password=secret-value", "repo": "r"})
    before = share._load_outbox()
    assert asyncio.run(share._adrain_outbox_unlocked(OFF)) == 0
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
    assert calls == []
    api._share(tmp_repo, {"ids": [did], "confirm": True, "confirmation_digest": result["confirmation_digest"]})
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


def test_console_refuses_changed_unredacted_payload(tmp_repo, monkeypatch):
    from contexer.ui import api
    _, did = store.update_decision(tmp_repo, "Use PostgreSQL for the database", "s", "convention")
    monkeypatch.setattr(config, "load_profile", lambda: OFF)
    fake = _fake(monkeypatch)
    _, preview = api._share(tmp_repo, {"ids": [did]})
    store.update_decision(tmp_repo, "Use SQLite for the database", "s", "convention", replace_id=did)
    _, result = api._share(tmp_repo, {"ids": [did], "confirm": True,
                                     "confirmation_digest": preview["confirmation_digest"]})
    assert not result["ok"] and "changed" in result["error"]
    assert not fake.calls and not fake.batches
    _, fresh = api._share(tmp_repo, {"ids": [did]})
    assert fresh["confirmation_digest"] != preview["confirmation_digest"]
    _, result = api._share(tmp_repo, {"ids": [did], "confirm": True,
                                     "confirmation_digest": fresh["confirmation_digest"]})
    assert result["ok"] and fake.batches


@pytest.mark.parametrize("batch", [False, True])
def test_remote_success_survives_outbox_cleanup_failure(tmp_repo, monkeypatch, batch):
    _, did = store.update_decision(tmp_repo, "Use PostgreSQL for the database", "s", "convention")
    share._enqueue({"decision_id": did, "content": "old body", "repo": "r"})
    fake = _fake(monkeypatch)
    monkeypatch.setattr(share, "_save_outbox", lambda rows: (_ for _ in ()).throw(OSError("disk failed")))
    result = share.share_ids(tmp_repo, [did], profile=OFF) if batch else share.share(tmp_repo, did, profile=OFF)
    assert share_status.is_ok(result) and result.sent == 1
    assert result.retry_cleanup_failed == 1
    assert "older payloads may remain queued" in share_status.describe(result)
    assert fake.calls or fake.batches


def test_unredacted_reconciliation_reports_paused_retry():
    result = share_status.ReconcileStatus(share_status.UNREACHABLE_QUEUED, redaction_disabled=True)
    text = share_status.describe(result)
    assert "retry paused until" in text and "for automatic retry" not in text


def test_legacy_preview_confirmation_is_stable_without_migration_write(tmp_repo, monkeypatch):
    import json
    from contexer.ui import api
    entry = store._new_decision_entry("Use PostgreSQL for the database", "s", "convention")
    for key in ("revisions", "current_revision_id"):
        entry.pop(key, None)
    path = store._store_path(tmp_repo)
    path.write_text(json.dumps({"entries": [entry]}), encoding="utf-8")
    before = path.read_bytes()
    monkeypatch.setattr(config, "load_profile", lambda: OFF)
    fake = _fake(monkeypatch)
    _, preview = api._share(tmp_repo, {"ids": [entry["id"]]})
    assert path.read_bytes() == before
    _, result = api._share(tmp_repo, {"ids": [entry["id"]], "confirm": True,
                                     "confirmation_digest": preview["confirmation_digest"]})
    assert result["ok"] and fake.batches
    assert path.read_bytes() == before


def test_confirmed_share_reuses_checked_repository_destination(tmp_repo, monkeypatch):
    _, did = store.update_decision(tmp_repo, "Use PostgreSQL for the database", "s", "convention")
    monkeypatch.setattr(config, "load_profile", lambda: OFF)
    original = store.run_git
    reads = []
    def origin(path, *args):
        if args == ("remote", "get-url", "origin"):
            reads.append(args)
            return "git@github.com:owner/first.git" if len(reads) == 1 else "git@github.com:owner/changed.git"
        return original(path, *args)
    monkeypatch.setattr(store, "run_git", origin)
    digest = share.selection_digest(tmp_repo, [did], profile=OFF)
    reads.clear()
    fake = _fake(monkeypatch)
    result = share.share_ids(tmp_repo, [did], profile=OFF, expected_digest=digest)
    assert share_status.is_ok(result) and len(reads) == 1
    assert fake.batches[0][0]["repo"] == "github.com/owner/first"


def test_confirmation_digest_rejects_same_endpoint_account_change(tmp_repo, monkeypatch):
    from contexer import auth
    profile = config.Profile(mode="team", endpoint="https://team.example/mcp", token="first-account", redact_secrets=False)
    monkeypatch.setattr(share, "_resolve_ids", lambda *a, **kw: ([{"id": "d", "content": "secret"}], []))
    monkeypatch.setattr(store, "run_git", lambda *a: "git@github.com:org/repo.git")
    monkeypatch.setattr(auth, "_load_creds", lambda: None)
    monkeypatch.setattr(share, "_drain_outbox_unlocked", lambda *a: None)
    digest = share.selection_digest(tmp_repo, ["d"], profile=profile)
    changed = config.Profile(mode="team", endpoint=profile.endpoint, token="second-account", redact_secrets=False)
    monkeypatch.setattr(share.RemoteStore, "from_profile", lambda *a, **kw: pytest.fail("must refuse before network"))
    with pytest.raises(ValueError, match="changed"):
        share.share_ids(tmp_repo, ["d"], profile=changed, expected_digest=digest)


def test_oauth_account_change_invalidates_preview_without_network(tmp_repo, monkeypatch):
    from contexer import auth
    creds = {"issuer": "https://team.example", "access_token": "account-one", "client_id": "one"}
    monkeypatch.setattr(auth, "_load_creds", lambda: creds)
    profile = config.Profile(mode="team", endpoint="https://team.example/mcp", redact_secrets=False)
    first = auth.confirmation_binding(profile)
    creds["access_token"] = "account-two"
    assert auth.confirmation_binding(profile) != first
    assert "account-one" not in first
