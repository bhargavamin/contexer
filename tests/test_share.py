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
