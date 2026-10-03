import json

import pytest

from contexer import cli, console_api, lifecycle, server, share_policy, store
from contexer.ui import api


OLD = "ghp_" + "A" * 36
NEW = "PERSONAL_ERASURE_PAYLOAD_987654321"
PROPOSAL = "PROPOSED_ERASURE_PAYLOAD_123456789"


def seeded(repo):
    _, did = store.update_decision(repo, f"Use the private credential {OLD} for upstream calls.",
                                   "s1", "architecture", created_by="human", title=f"Private {OLD}")
    store.edit_decision(repo, did, content=f"Use the replacement credential {NEW} for upstream calls.", source="human")
    store.update_decision(repo, f"Use the proposed credential {PROPOSAL} for upstream calls.",
                          "s2", "architecture", replace_id=did, title=f"Proposed {PROPOSAL}")
    return did


@pytest.mark.parametrize("retired", [False, True])
def test_erasure_removes_every_copy_but_keeps_safe_history(tmp_repo, retired):
    did = seeded(tmp_repo)
    _, other = store.update_decision(tmp_repo, "Keep database migrations reversible.", "s", "convention", created_by="human")
    if retired:
        assert store.delete_decision(tmp_repo, did)[0]
    slug = store.repo_slug(tmp_repo)
    # Copies outside the live record: old history, proposals, raw evidence and queued sends.
    store.atomic_write(store.sidecar_path("outbox"), json.dumps([
        {"decision_id": did, "content": OLD}, {"decision_id": other, "content": "unrelated"}]))
    store.atomic_write(store.sidecar_path("working_set", slug=slug, session="s"),
                       json.dumps({"rows": [{"id": did, "body": NEW}], "unrelated": 17}))
    spool = store.store_dir() / "evidence" / slug / "pending"
    spool.mkdir(parents=True)
    (spool / "event.json").write_text(json.dumps({"text": f"User mentioned {OLD}, {NEW}, {PROPOSAL}"}))
    (spool / "unrelated.json").write_text('{"text":"Keep a useful unrelated observation."}')
    store.atomic_write(store.sidecar_path("reconcile_log", slug=slug),
                       json.dumps({"excerpt": NEW}) + "\n" + json.dumps({"excerpt": PROPOSAL}) + "\n")
    ok, message = lifecycle.erase_decision(tmp_repo, did[:8], confirm=True, actor="cli")
    assert ok, message
    for path in store.store_dir().rglob("*"):
        if path.is_file():
            raw = path.read_bytes()
            for marker in (OLD, NEW, PROPOSAL):
                assert marker.encode() not in raw, path.name
    assert store.entry_by_id(store.load(tmp_repo)["entries"], other)
    assert (spool / "unrelated.json").read_text() == '{"text":"Keep a useful unrelated observation."}'
    audit = store.read_deleted(tmp_repo)[0]["entries"][-1]
    assert set(audit) == {"type", "id", "timestamp", "deleted_at", "deleted_by", "reason", "content_digests"}
    assert audit["id"] == did and audit["deleted_by"] == "cli" and audit["reason"] == "erased"
    assert not lifecycle.restore_decision(tmp_repo, did)[0]
    row = next(r for r in console_api.list_tombstones(tmp_repo)["tombstones"] if r["id"] == did)
    assert row["status"] == "erased" and row["content"] == ""


def test_confirmation_and_team_copy_refusal(tmp_repo):
    did = seeded(tmp_repo)
    before = store._store_path(tmp_repo).read_bytes()
    assert not lifecycle.erase_decision(tmp_repo, did)[0]
    store.atomic_write(store.sidecar_path("shared_markers"),
                       json.dumps({"endpoint": "https://team.example", "id": did, "at": "now"}) + "\n")
    ok, message = lifecycle.erase_decision(tmp_repo, did, confirm=True)
    assert not ok and "team copy" in message
    assert store._store_path(tmp_repo).read_bytes() == before


@pytest.mark.parametrize("payload", ["password=Ab1cd2", "postgres://me:abc@localhost/db", "a@b.org"])
def test_short_credentials_and_personal_addresses_in_copied_evidence_are_erased(tmp_repo, payload):
    _, did = store.update_decision(tmp_repo, f"Use the private setting {payload} for upstream calls.",
                                   "s", "architecture", created_by="human")
    path = store.store_dir() / "copied-evidence.jsonl"
    secret = "Ab1cd2" if payload.startswith("password=") else "abc" if payload.startswith("postgres:") else payload
    path.write_text(json.dumps({"excerpt": f"Observed {secret} in a longer evidence record."}) + "\n")
    assert lifecycle.erase_decision(tmp_repo, did, confirm=True)[0]
    assert secret not in path.read_text()


def test_corrupt_store_and_tombstones_are_preserved(tmp_repo):
    did = seeded(tmp_repo)
    store._deleted_path(tmp_repo).write_text("{bad")
    before = store._store_path(tmp_repo).read_bytes()
    assert not lifecycle.erase_decision(tmp_repo, did, confirm=True)[0]
    assert store._store_path(tmp_repo).read_bytes() == before
    assert store._deleted_path(tmp_repo).read_text() == "{bad"


@pytest.mark.parametrize("state", ["submitted", "already_pending", "unchanged"])
def test_automatic_team_receipts_also_refuse_erasure(tmp_repo, state):
    from tests.test_share_policy import _receipt

    did = seeded(tmp_repo)
    before = store._store_path(tmp_repo).read_bytes()
    share_policy.append_receipt(_receipt(decision_id=did, state=state))
    ok, message = lifecycle.erase_decision(tmp_repo, did, confirm=True)
    assert not ok and "team copy" in message
    assert store._store_path(tmp_repo).read_bytes() == before


def test_failed_cleanup_keeps_decision_retryable(tmp_repo, monkeypatch):
    did = seeded(tmp_repo)
    path = store.sidecar_path("outbox")
    store.atomic_write(path, json.dumps([{"decision_id": did, "content": NEW}]))
    original = store.atomic_write
    def fail(target, text, **kwargs):
        if target == path:
            raise OSError("disk full")
        original(target, text, **kwargs)
    monkeypatch.setattr(store, "atomic_write", fail)
    assert not lifecycle.erase_decision(tmp_repo, did, confirm=True)[0]
    assert store.entry_by_id(store.load(tmp_repo)["entries"], did)
    monkeypatch.setattr(store, "atomic_write", original)
    assert lifecycle.erase_decision(tmp_repo, did, confirm=True)[0]


def test_console_requires_boolean_confirmation(tmp_repo):
    did = seeded(tmp_repo)
    with pytest.raises(api.ApiError, match="Confirm explicitly"):
        api._decision_route("POST", tmp_repo, did, ["erase"], {"confirm": "true"})
    status, payload = api._decision_route("POST", tmp_repo, did, ["erase"], {"confirm": True})
    assert status == 200 and "Erased" in payload["message"]


def test_cli_and_no_mcp_surface(tmp_repo, monkeypatch, capsys):
    did = seeded(tmp_repo)
    monkeypatch.setattr(cli, "_cli_repo", lambda: tmp_repo)
    cli.erase_cmd([did, "--yes"])
    assert "Erased" in capsys.readouterr().out
    assert not hasattr(server, "erase_decision")
    assert "erase" not in server.mcp._tool_manager._tools



def test_erasure_preserves_other_decisions_and_repositories(tmp_repo, tmp_path):
    did = seeded(tmp_repo)
    text = "Keep this independent rule " + OLD
    _, other = store.update_decision(tmp_repo, text, "s", "convention", created_by="human")
    foreign = str(tmp_path / "other-repo")
    store.save(foreign, {"entries": [store._new_decision_entry(text, "s", "convention", status="approved")]})
    foreign_path = store._store_path(foreign)
    before = foreign_path.read_bytes()
    settings = store.store_dir() / "config.toml"
    settings.write_text('token = "' + OLD + '"')
    assert lifecycle.erase_decision(tmp_repo, did, confirm=True)[0]
    assert store.entry_by_id(store.load(tmp_repo)["entries"], other)["content"] == text
    assert foreign_path.read_bytes() == before and OLD in settings.read_text()


def test_erased_rule_is_not_automatically_recaptured(tmp_repo):
    did = seeded(tmp_repo)
    contents = [r["content"] for r in store.load(tmp_repo)["entries"][0]["revisions"]]
    assert lifecycle.erase_decision(tmp_repo, did, confirm=True)[0]
    for content in contents:
        assert not store.update_decision(tmp_repo, content, "next-session", "architecture")[0]


def test_corrupt_receipts_fail_without_uncaught_exception(tmp_repo):
    from contexer import share_policy
    did = seeded(tmp_repo)
    share_policy.proposal_receipts_path().write_text("{broken")
    assert not lifecycle.erase_decision(tmp_repo, did, confirm=True)[0]
    assert store.entry_by_id(store.load(tmp_repo)["entries"], did)


def test_active_evidence_publisher_refuses_erasure(tmp_repo):
    did = seeded(tmp_repo)
    with store.evidence_publication_lock(tmp_repo) as acquired:
        assert acquired
        assert not lifecycle.erase_decision(tmp_repo, did, confirm=True)[0]
    assert store.entry_by_id(store.load(tmp_repo)["entries"], did)


def test_erase_gate_never_waits_for_new_evidence_writer(tmp_repo):
    with store.evidence_publication_lock(tmp_repo, exclusive=True) as acquired:
        assert acquired
        with store.evidence_publication_lock(tmp_repo) as published:
            assert not published



def test_durable_write_failure_preserves_previous_bytes(tmp_path, monkeypatch):
    target = tmp_path / "safe.json"
    target.write_text("previous")
    def fail_sync(fd):
        raise OSError("sync failed")
    monkeypatch.setattr(store.os, "fsync", fail_sync)
    with pytest.raises(OSError, match="sync failed"):
        store.atomic_write(target, "replacement", durable=True)
    assert target.read_text() == "previous"
    assert not list(tmp_path.glob("*.tmp"))
