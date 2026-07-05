"""Tests for C4 share: store.get_shareable + contexer/share.py.

RemoteStore is faked (monkeypatched from_profile) so no network is touched. The team
profile is passed explicitly to share() to avoid reading a real config.toml.
"""
import pytest

import contexer.remote as remote
from contexer import config, share, store
from contexer.remote import RemoteAuthError, RemoteUnavailableError

TEAM = config.Profile(mode="team", endpoint="https://t/mcp", token="tok")


class _FakeRS:
    def __init__(self, ret="srv-1", exc=None):
        self.ret, self.exc, self.calls = ret, exc, []

    def push_decision(self, **kw):
        self.calls.append(kw)
        if self.exc is not None:
            raise self.exc
        return self.ret


def _fake(monkeypatch, **kw):
    fake = _FakeRS(**kw)
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: fake))
    remote.reset_degradation_warnings()
    return fake


# ── store.get_shareable ──────────────────────────────────────────────────────────

def test_get_shareable_none_when_empty(tmp_repo):
    assert store.get_shareable(tmp_repo) is None


def test_get_shareable_latest_by_default(tmp_repo):
    _, id1 = store.update_decision(tmp_repo, "first decision here", "s1", subtype="architecture")
    _, id2 = store.update_decision(tmp_repo, "second newer decision", "s1", subtype="constraint")
    dec = store.get_shareable(tmp_repo)
    assert dec["id"] == id2
    assert dec["type"] == "constraint"


def test_get_shareable_by_id_prefix(tmp_repo):
    _, id1 = store.update_decision(tmp_repo, "alpha decision content", "s1", subtype="architecture")
    store.update_decision(tmp_repo, "beta decision content", "s1", subtype="constraint")
    dec = store.get_shareable(tmp_repo, id1[:8])
    assert dec["id"] == id1
    assert dec["type"] == "architecture"


def test_get_shareable_not_found(tmp_repo):
    store.update_decision(tmp_repo, "some decision content", "s1", subtype="architecture")
    assert store.get_shareable(tmp_repo, "no-such-id") is None


def test_get_shareable_excludes_ignored(tmp_repo):
    _, id1 = store.update_decision(tmp_repo, "keep this decision here", "s1", subtype="architecture")
    _, id2 = store.update_decision(tmp_repo, "ignore this one please now", "s1", subtype="constraint")
    store.approve_decision(tmp_repo, id2, "ignore")
    assert store.get_shareable(tmp_repo)["id"] == id1  # latest non-ignored


def test_get_shareable_carries_provenance(tmp_repo):
    _, id1 = store.update_decision(tmp_repo, "decision with provenance data", "s1", subtype="architecture")
    dec = store.get_shareable(tmp_repo, id1)
    assert dec["source"] == "ai"
    assert isinstance(dec["confidence"], int)
    assert dec["evidence"] is None or isinstance(dec["evidence"], list)


# ── share.share ──────────────────────────────────────────────────────────────────

def test_share_nothing_to_share(tmp_repo):
    assert "nothing to share" in share.share(tmp_repo, profile=TEAM).lower()


def test_share_local_mode_message(tmp_repo, monkeypatch):
    store.update_decision(tmp_repo, "a decision to maybe share", "s1", subtype="architecture")
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: None))
    assert "team mode" in share.share(tmp_repo, profile=config.Profile()).lower()


def test_share_happy_path_wire_args(tmp_repo, monkeypatch):
    _, did = store.update_decision(tmp_repo, "use postgres for storage", "s1", subtype="architecture")
    monkeypatch.setattr(store, "_git", lambda repo, *a: "git@github.com:a/b.git")
    fake = _fake(monkeypatch, ret="srv-9")
    msg = share.share(tmp_repo, profile=TEAM)
    assert "srv-9" in msg
    assert len(fake.calls) == 1
    kw = fake.calls[0]
    assert kw["type"] == "architecture"
    assert "postgres" in kw["content"].lower()
    assert kw["repo"] == "github.com/a/b"
    assert kw["decision_id"] == did  # local id -> idempotent re-share
    assert kw["source"] == "ai"


def test_share_no_git_origin_pushes_repo_none(tmp_repo, monkeypatch):
    store.update_decision(tmp_repo, "decision without a remote origin", "s1", subtype="constraint")
    monkeypatch.setattr(store, "_git", lambda repo, *a: None)
    fake = _fake(monkeypatch, ret="srv-x")
    assert "srv-x" in share.share(tmp_repo, profile=TEAM)
    assert fake.calls[0]["repo"] is None


def test_share_by_id_prefix(tmp_repo, monkeypatch):
    _, id1 = store.update_decision(tmp_repo, "alpha shareable decision", "s1", subtype="architecture")
    store.update_decision(tmp_repo, "beta newer decision here", "s1", subtype="constraint")
    monkeypatch.setattr(store, "_git", lambda repo, *a: None)
    fake = _fake(monkeypatch, ret="srv-a")
    share.share(tmp_repo, id1[:8], profile=TEAM)
    assert fake.calls[0]["decision_id"] == id1  # shared the requested one, not the latest


def test_share_degraded_unreachable(tmp_repo, monkeypatch, capsys):
    store.update_decision(tmp_repo, "decision that fails to sync", "s1", subtype="architecture")
    monkeypatch.setattr(store, "_git", lambda repo, *a: "git@github.com:a/b.git")
    _fake(monkeypatch, exc=RemoteUnavailableError("down"))
    msg = share.share(tmp_repo, profile=TEAM)
    assert "fail" in msg.lower()
    assert "unreachable" in capsys.readouterr().err.lower()


def test_share_degraded_auth(tmp_repo, monkeypatch, capsys):
    store.update_decision(tmp_repo, "decision with bad token sync", "s1", subtype="architecture")
    monkeypatch.setattr(store, "_git", lambda repo, *a: None)
    _fake(monkeypatch, exc=RemoteAuthError("401"))
    assert "fail" in share.share(tmp_repo, profile=TEAM).lower()
    assert "contexer login --team" in capsys.readouterr().err


# ── outbox: enqueue on share ──────────────────────────────────────────────────────

def test_share_degraded_enqueues_payload(tmp_repo, monkeypatch):
    _, did = store.update_decision(tmp_repo, "decision that fails to sync", "s1", subtype="architecture")
    monkeypatch.setattr(store, "_git", lambda repo, *a: "git@github.com:a/b.git")
    _fake(monkeypatch, exc=RemoteUnavailableError("down"))
    msg = share.share(tmp_repo, profile=TEAM)
    assert "queued" in msg.lower()
    entries = share._load_outbox()
    assert len(entries) == 1
    entry = entries[0]
    assert entry["decision_id"] == did
    assert entry["type"] == "architecture"
    assert "sync" in entry["content"].lower()
    assert entry["repo"] == "github.com/a/b"
    assert entry["source"] == "ai"
    assert entry["attempts"] == 0
    assert isinstance(entry["queued_at"], float)


def test_share_degraded_auth_also_enqueues(tmp_repo, monkeypatch):
    store.update_decision(tmp_repo, "decision with bad token sync", "s1", subtype="constraint")
    monkeypatch.setattr(store, "_git", lambda repo, *a: None)
    _fake(monkeypatch, exc=RemoteAuthError("401"))
    share.share(tmp_repo, profile=TEAM)
    assert len(share._load_outbox()) == 1  # auth failures enqueue too -- retry after re-login


def test_share_nothing_to_share_does_not_enqueue(tmp_repo):
    share.share(tmp_repo, profile=TEAM)
    assert share._load_outbox() == []


def test_share_local_mode_does_not_enqueue(tmp_repo, monkeypatch):
    store.update_decision(tmp_repo, "a decision to maybe share", "s1", subtype="architecture")
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: None))
    share.share(tmp_repo, profile=config.Profile())
    assert share._load_outbox() == []


def test_share_happy_path_does_not_enqueue(tmp_repo, monkeypatch):
    store.update_decision(tmp_repo, "use postgres for storage", "s1", subtype="architecture")
    monkeypatch.setattr(store, "_git", lambda repo, *a: "git@github.com:a/b.git")
    _fake(monkeypatch, ret="srv-9")
    share.share(tmp_repo, profile=TEAM)
    assert share._load_outbox() == []


# ── outbox: _enqueue dedupe + cap ─────────────────────────────────────────────────

def test_enqueue_dedupe_by_decision_id_replaces(tmp_repo):
    share._enqueue({"decision_id": "d1", "content": "stale content", "attempts": 0})
    share._enqueue({"decision_id": "d1", "content": "fresh content", "attempts": 0})
    entries = share._load_outbox()
    assert len(entries) == 1
    assert entries[0]["content"] == "fresh content"


def test_enqueue_caps_at_50_drops_oldest(tmp_repo):
    for i in range(55):
        share._enqueue({"decision_id": f"d{i}", "content": f"c{i}", "attempts": 0})
    entries = share._load_outbox()
    assert len(entries) == 50
    ids = [e["decision_id"] for e in entries]
    assert "d0" not in ids  # oldest 5 dropped
    assert "d4" not in ids
    assert "d5" in ids  # oldest survivor
    assert "d54" in ids  # newest kept


# ── outbox: drain_outbox ──────────────────────────────────────────────────────────

def test_drain_outbox_noop_when_empty(tmp_repo, monkeypatch):
    fake = _fake(monkeypatch, ret="srv-1")
    assert share.drain_outbox(TEAM) == 0
    assert fake.calls == []


def test_drain_outbox_noop_when_not_configured(tmp_repo, monkeypatch):
    share._enqueue({"decision_id": "d1", "type": "architecture", "content": "c",
                    "repo": None, "rationale": None, "confidence": None,
                    "evidence": None, "source": "ai", "queued_at": 1.0, "attempts": 0})
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: None))
    assert share.drain_outbox(config.Profile()) == 0
    assert len(share._load_outbox()) == 1  # left queued, untouched


def test_drain_outbox_sends_fifo_and_removes_successes(tmp_repo, monkeypatch):
    share._enqueue({"decision_id": "d1", "type": "architecture", "content": "first",
                    "repo": "r", "rationale": None, "confidence": 80,
                    "evidence": None, "source": "ai", "queued_at": 1.0, "attempts": 0})
    share._enqueue({"decision_id": "d2", "type": "constraint", "content": "second",
                    "repo": "r", "rationale": None, "confidence": 90,
                    "evidence": None, "source": "ai", "queued_at": 2.0, "attempts": 0})
    fake = _fake(monkeypatch, ret="srv-ok")
    sent = share.drain_outbox(TEAM)
    assert sent == 2
    assert [c["decision_id"] for c in fake.calls] == ["d1", "d2"]  # FIFO order
    assert share._load_outbox() == []


def test_drain_outbox_stops_at_first_failure_keeps_tail(tmp_repo, monkeypatch):
    share._enqueue({"decision_id": "d1", "type": "architecture", "content": "first",
                    "repo": "r", "rationale": None, "confidence": 80,
                    "evidence": None, "source": "ai", "queued_at": 1.0, "attempts": 0})
    share._enqueue({"decision_id": "d2", "type": "constraint", "content": "second",
                    "repo": "r", "rationale": None, "confidence": 90,
                    "evidence": None, "source": "ai", "queued_at": 2.0, "attempts": 0})
    _fake(monkeypatch, exc=RemoteUnavailableError("down"))
    sent = share.drain_outbox(TEAM)
    assert sent == 0
    remaining = share._load_outbox()
    assert [e["decision_id"] for e in remaining] == ["d1", "d2"]  # kept, in order
    assert remaining[0]["attempts"] == 1  # incremented on the failed attempt


def test_drain_outbox_partial_success_then_failure(tmp_repo, monkeypatch):
    share._enqueue({"decision_id": "d1", "type": "architecture", "content": "first",
                    "repo": "r", "rationale": None, "confidence": 80,
                    "evidence": None, "source": "ai", "queued_at": 1.0, "attempts": 0})
    share._enqueue({"decision_id": "d2", "type": "constraint", "content": "second",
                    "repo": "r", "rationale": None, "confidence": 90,
                    "evidence": None, "source": "ai", "queued_at": 2.0, "attempts": 0})

    class _FlakyRS:
        def __init__(self):
            self.calls = []

        def push_decision(self, **kw):
            self.calls.append(kw)
            if kw["decision_id"] == "d2":
                raise RemoteUnavailableError("down")
            return "srv-1"

    fake = _FlakyRS()
    monkeypatch.setattr(share.RemoteStore, "from_profile", staticmethod(lambda p: fake))
    remote.reset_degradation_warnings()
    sent = share.drain_outbox(TEAM)
    assert sent == 1  # d1 sent, d2 kept
    remaining = share._load_outbox()
    assert [e["decision_id"] for e in remaining] == ["d2"]


def test_load_outbox_corrupt_file_reads_empty(tmp_repo):
    path = share._outbox_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json")
    assert share._load_outbox() == []


def test_share_drains_queued_items_before_new_push(tmp_repo, monkeypatch):
    """A queued item from an earlier offline share is sent first, then the new decision --
    ordering is preserved."""
    share._enqueue({"decision_id": "queued-1", "type": "architecture", "content": "old queued item",
                    "repo": "r", "rationale": None, "confidence": 80,
                    "evidence": None, "source": "ai", "queued_at": 1.0, "attempts": 0})
    _, did = store.update_decision(tmp_repo, "brand new decision to share", "s1", subtype="constraint")
    monkeypatch.setattr(store, "_git", lambda repo, *a: "git@github.com:a/b.git")
    fake = _fake(monkeypatch, ret="srv-ok")
    msg = share.share(tmp_repo, profile=TEAM)
    assert "srv-ok" in msg
    assert [c["decision_id"] for c in fake.calls] == ["queued-1", did]
    assert share._load_outbox() == []  # the queued item drained, only the new push happened


# ── CLI ──────────────────────────────────────────────────────────────────────────

def test_cli_share_prints_result(monkeypatch, capsys):
    from contexer import cli
    monkeypatch.setattr(store, "_git_root", lambda p: "/repo")
    monkeypatch.setattr(share, "share", lambda repo, decision_id="": f"shared {decision_id or 'latest'}")
    cli.share_cmd(["abc123"])
    assert "abc123" in capsys.readouterr().out


def test_cli_share_no_repo_exits(monkeypatch):
    from contexer import cli
    monkeypatch.setattr(store, "_git_root", lambda p: "")
    monkeypatch.setattr(store, "_resolve_repo", lambda p: "")
    with pytest.raises(SystemExit):
        cli.share_cmd([])
