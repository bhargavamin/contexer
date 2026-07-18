"""E2E (I1): two clones -> same repo-key, share/pull round-trip.

HERMETIC TEST (this file): a `FakeTeamsServer` (see conftest `team_stack`) stands in for the
Teams MCP server. The FULL OSS path runs -- share -> push_decision -> server -> get_context
-> pull -> team-cache merge -> store.get_context display -- with only the network hop faked.
Two "clones" = two different local repo PATHS with the SAME git origin, proving they
converge on one canonical repo key (the cross-language seam this ticket exists to protect).

MANUAL RUNBOOK (real stack) -- run when validating against a live deployment:
  Prereqs: contexer-teams up -- `docker compose up -d`; `pnpm db:migrate`; `pnpm dev:mcp`
  (:8080); `pnpm dev:web` (:3000). `~/.contexer/config.toml` -> mode='team',
  endpoint='http://localhost:8080/mcp'. Run `contexer login` (or paste a `pnpm mint` token).
  1. Clone the SAME repo to two paths:  `git clone <origin> A && git clone <origin> B`.
  2. In A: capture a decision (a Claude session's update_context, or the MCP tool), then
     `contexer share`  ->  "Synced decision to your personal team context (server id=...)".
  3. Team-approve it: in the web review queue, share that decision to a team and have a lead
     approve it (scope becomes 'team' -- the pull-visible path). A personal-only share is a
     cloud mirror, NOT pulled into other clones under Path B v1.
  4. In B (different path, same origin): `contexer pull`  ->  "Pulled 1 team decision(s)."
  5. In B: start a session / call get_context  ->  the decision appears under
     "## Team context (synced)", proving both paths resolved to the same canonical repo key.
  6. Re-run `contexer share` in A  ->  idempotent (same decisionId, no duplicate row).
"""
from contexer import config, share, store, team_context
from contexer.repo_key import canonical_repo_key

TEAM = config.Profile(mode="team", endpoint="http://fake/mcp", token="tok")
CLONE_A = "/work/clone-a"
CLONE_B = "/work/clone-b"  # different path, same git origin (see team_stack)


def test_share_then_other_clone_pulls_it(team_stack):
    server = team_stack
    # Clone A captures a decision locally and shares it up.
    _, did = store.update_decision(CLONE_A, "use postgres for the primary datastore", "sA", subtype="architecture")
    assert "Synced" in share.share(CLONE_A, profile=TEAM)
    assert did in server.rows  # pushed under the local (stable) decision id

    # A lead approves the shared decision (personal -> team-approved).
    server.approve_as_team(did)

    # Clone B (different path, same origin) pulls and now sees it in its merged context.
    upserted, removed = team_context.pull(CLONE_B, profile=TEAM)
    assert (upserted, removed) == (1, 0)
    out = store.get_context(CLONE_B)
    assert "## Team context" in out
    assert "postgres for the primary datastore" in out


def test_both_clones_resolve_same_canonical_key(team_stack):
    key_a = canonical_repo_key(store._git(CLONE_A, "remote", "get-url", "origin"))
    key_b = canonical_repo_key(store._git(CLONE_B, "remote", "get-url", "origin"))
    assert key_a == key_b == team_stack.REPO_KEY


def test_reshare_is_idempotent(team_stack):
    server = team_stack
    store.update_decision(CLONE_A, "always run migrations before deploy", "sA", subtype="constraint")
    share.share(CLONE_A, profile=TEAM)
    share.share(CLONE_A, profile=TEAM)  # re-share the same decision
    assert len(server.rows) == 1  # single row, no duplicate on re-push


def test_share_all_pushes_every_decision_e2e(team_stack):
    server = team_stack
    _, id1 = store.update_decision(CLONE_A, "use postgres for the primary datastore", "sA", subtype="architecture")
    _, id2 = store.update_decision(CLONE_A, "always run migrations before deploy", "sA", subtype="constraint")
    msg = share.share_all(CLONE_A, profile=TEAM)
    assert "2" in msg
    assert set(server.rows) == {id1, id2}
    assert all(r["repo"] == server.REPO_KEY for r in server.rows.values())


def test_share_all_outage_then_recovery_drains_outbox(team_stack, monkeypatch):
    """share --all during an outage queues everything; the next share (cloud back up)
    drains the queue automatically - no share intent lost across the outage."""
    import contexer.remote as remote
    from contexer.remote import RemoteUnavailableError
    server = team_stack
    _, id1 = store.update_decision(CLONE_A, "use postgres for the primary datastore", "sA", subtype="architecture")
    _, id2 = store.update_decision(CLONE_A, "always run migrations before deploy", "sA", subtype="constraint")

    real_transport = remote._acall_tool
    monkeypatch.setattr(remote, "_acall_tool",
                        lambda *a, **k: (_ for _ in ()).throw(RemoteUnavailableError("down")))
    assert "queued" in share.share_all(CLONE_A, profile=TEAM).lower()
    assert server.rows == {}
    assert [e["decision_id"] for e in share._load_outbox()] == [id1, id2]

    monkeypatch.setattr(remote, "_acall_tool", real_transport)  # cloud comes back
    remote.reset_degradation_warnings()
    share.share_all(CLONE_A, profile=TEAM)  # drains the outbox first, then re-pushes
    assert set(server.rows) == {id1, id2}
    assert share._load_outbox() == []


def test_local_capture_survives_cloud_outage(team_stack, monkeypatch):
    # Local-first contract: the cloud being down must never block local capture or reads.
    import contexer.remote as remote
    from contexer.remote import RemoteUnavailableError
    monkeypatch.setattr(remote, "_acall_tool",
                        lambda *a, **k: (_ for _ in ()).throw(RemoteUnavailableError("down")))
    ok, _ = store.update_decision(CLONE_A, "local decision while cloud is down", "sA", subtype="architecture")
    assert ok
    assert "Share failed" in share.share(CLONE_A, profile=TEAM)  # degrades, no crash
    assert "decision while cloud is down" in store.get_context(CLONE_A)  # still readable (normalized)
