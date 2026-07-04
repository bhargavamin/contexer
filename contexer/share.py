"""Explicit share: push a local decision up to the Teams cloud context (C4).

Path B, the write counterpart to team_context.pull. Sharing is an EXPLICIT verb — never
auto-shares on capture. v1 syncs to your PERSONAL cloud context (push_decision auto-approves
it); a team `shared_candidate` awaits a team-scoped push endpoint (future Track A).

NOTE: the profile -> RemoteStore -> canonical_repo_key(git origin) boilerplate is duplicated
with team_context.pull; DRY into one helper once C4 and C5 are both merged.
"""
from __future__ import annotations

from contexer import store
from contexer.config import Profile, load_profile
from contexer.remote import RemoteStore, with_local_fallback
from contexer.repo_key import canonical_repo_key


def share(repo_path: str, decision_id: str = "", *, profile: Profile | None = None) -> str:
    """Push one local decision to your team cloud context; return a human-readable status.

    Local-first: never raises for cloud problems — returns a message and leaves the local
    decision untouched. `decision_id` selects the decision (full id / 8-char prefix); omit
    to share the most recent. `profile` defaults to load_profile()."""
    dec = store.get_shareable(repo_path, decision_id)
    if dec is None:
        return "Nothing to share: no matching local decision."
    profile = profile or load_profile()
    remote = RemoteStore.from_profile(profile)
    if remote is None:
        return ("Not in team mode. Set mode='team' + endpoint + token in "
                "~/.contexer/config.toml to share.")
    key = canonical_repo_key(store._git(repo_path, "remote", "get-url", "origin"))
    server_id = with_local_fallback(
        lambda: remote.push_decision(
            type=dec["type"], content=dec["content"], repo=key,
            confidence=dec["confidence"], evidence=dec["evidence"],
            source=dec["source"], decision_id=dec["id"]),
        default=None, action="share decision")
    if server_id is None:
        return ("Share failed: cloud unreachable or auth rejected (see the warning above). "
                "Your local decision is unchanged.")
    return f"Synced decision to your personal team context (server id={server_id})."
