"""Canonical repo-key derivation.

Shared byte-for-byte with the TypeScript sibling (packages/db/src/repoKey.ts):
the same remote URL must map to the same key in both languages so a decision
pushed from the local Python store and one pushed from the TS teams app collide
on the same repo. Never falls back to a filesystem path - a missing/blank
remote yields None.

SSH host aliases are resolved CLIENT-SIDE before the shared normalization: a
multi-account remote like ``git@github.com-work:owner/repo.git`` (where
``github.com-work`` is a ~/.ssh/config alias) must key as ``github.com/owner/repo``,
or two teammates cloning the same repo different ways silently shard the team
context. Resolution asks ssh itself (``ssh -G``, the same machinery git uses to
connect), so it honors the user's entire ssh config; the server-side TS helper
deliberately has no alias step - aliases are a local concept and only resolved
keys ever go over the wire. Fail-soft: if ssh is missing or errors, the host is
used as written (pre-fix behavior).
"""
import os
import subprocess
from functools import lru_cache

from contexer import store

_SCHEMES = ("https://", "http://", "git://", "ssh://")


@lru_cache(maxsize=64)
def _resolve_ssh_host(host: str) -> str:
    """The real hostname ssh would connect to for ``host`` (alias-resolved), or
    ``host`` unchanged on any failure. Cached per process."""
    return _ssh_hostname(host)


def _ssh_hostname(host: str) -> str:
    """Uncached ``ssh -G`` lookup - the seam unit tests exercise directly."""
    if not host:
        return host
    try:
        out = subprocess.run(["ssh", "-G", host], capture_output=True, text=True, timeout=5)
        if out.returncode == 0:
            for line in out.stdout.splitlines():
                if line.startswith("hostname "):
                    resolved = line.split(" ", 1)[1].strip()
                    return resolved or host
    except (OSError, subprocess.SubprocessError):
        pass
    return host


def _canonical_repo_key(remote: str | None, *, resolve_ssh_aliases: bool) -> str | None:
    """Normalize a git remote URL to "host/owner/repo" (or None).

    None / empty / whitespace-only returns None. Mirrors the TS reference:
    strips scheme + userinfo, parses scp-like ``host:path`` (including the
    user-less ``github.com:owner/repo`` shorthand), drops a trailing ``.git``
    and slash, and lowercases host + final result. Subgroup paths (>2 segments)
    are preserved.
    """
    if remote is None:
        return None
    remote = remote.strip()
    if not remote:
        return None

    lowered = remote.lower()
    scheme = next((s for s in _SCHEMES if lowered.startswith(s)), None)
    if scheme is not None:
        # scheme://[userinfo@]host/path - host/path split on the first '/'.
        rest = _strip_userinfo(remote[len(scheme):], "/")
        host, _, path = rest.partition("/")
        if scheme == "ssh://" and resolve_ssh_aliases:
            host = _resolve_ssh_host(host)
    else:
        # scp-like: [userinfo@]host:path - host/path split on the first ':'.
        # Covers git@host:owner/repo AND the user-less github.com:owner/repo.
        rest = _strip_userinfo(remote, ":")
        host, _, path = rest.partition(":")
        if path and resolve_ssh_aliases:  # a real scp-form remote rides ssh
            host = _resolve_ssh_host(host)

    host = host.lower()
    path = path.lstrip("/")
    result = host if path == "" else host + "/" + path

    if result.lower().endswith(".git"):
        result = result[: -len(".git")]
    result = result.rstrip("/")

    return result.lower()


def canonical_repo_key(remote: str | None) -> str | None:
    """Normalize a trusted git remote, including the checkout's SSH host aliases."""
    return _canonical_repo_key(remote, resolve_ssh_aliases=True)


def _evidence_identity_basis(repo_path: str) -> tuple[str, str | None]:
    """The local and remote identity of one spool, each derived once per consumer pass."""
    expected_local = os.path.realpath(store.canonical_store_key(os.path.realpath(repo_path)))
    expected_remote = canonical_repo_key(
        store.run_git(repo_path, "remote", "get-url", "origin"))
    return expected_local, expected_remote


def _compare_evidence_repo_identity(observed_key: object, expected_local: str,
                                    expected_remote: str | None) -> dict:
    """Compare one observed key against a precomputed spool identity basis."""
    observed = observed_key.strip() if isinstance(observed_key, str) else ""
    if not observed:
        return {"matches": False, "expected_key": expected_local, "observed_key": "",
                "reason": "missing"}

    if os.path.isabs(observed):
        observed_local = os.path.realpath(
            store.canonical_store_key(os.path.realpath(observed)))
        return {
            "matches": observed_local == expected_local,
            "expected_key": expected_local,
            "observed_key": observed,
            "reason": "match_local" if observed_local == expected_local else "mismatch_local",
        }

    # Observed evidence is untrusted spool data. Current emitters stamp absolute local paths;
    # a remote-shaped legacy/foreign value may be normalized syntactically but must never run
    # one `ssh -G` subprocess per attacker-chosen hostname on the session-start path. The
    # checkout's own origin was resolved above and remains the sole trusted alias lookup.
    observed_remote = _canonical_repo_key(observed, resolve_ssh_aliases=False)
    if expected_remote and observed_remote:
        matches = observed_remote == expected_remote
        return {"matches": matches, "expected_key": expected_remote,
                "observed_key": observed,
                "reason": "match_remote" if matches else "mismatch_remote"}
    return {"matches": False, "expected_key": expected_local, "observed_key": observed,
            "reason": "unverifiable"}


def compare_evidence_repo_identities(repo_path: str, observed_keys) -> dict[str, dict]:
    """Compare distinct observed keys while reading the checkout's origin exactly once."""
    observed_keys = list(observed_keys)
    if not observed_keys:
        return {}
    expected_local, expected_remote = _evidence_identity_basis(repo_path)
    return {
        str(observed if isinstance(observed, str) else ""):
        _compare_evidence_repo_identity(observed, expected_local, expected_remote)
        for observed in observed_keys
    }


def compare_evidence_repo_identity(repo_path: str, observed_key: object) -> dict:
    """Compare one event's repository identity with the spool that contains it.

    Current host emitters stamp a local path, so local identities use the store's exact
    linked-worktree collapse and a realpath comparison. A remote-shaped key is accepted only
    when this checkout has a canonical origin key to compare it with. Any other non-empty
    spelling is unverifiable, never assumed equivalent.

    Schema V1 has always required a non-empty ``repo_key``; ``missing`` is therefore an
    invalid legacy event, not a compatibility exemption. The spool consumer records its
    missing-identity receipt and quarantines it before candidate aggregation.
    """
    expected_local, expected_remote = _evidence_identity_basis(repo_path)
    return _compare_evidence_repo_identity(observed_key, expected_local, expected_remote)


def _strip_userinfo(s: str, delim: str) -> str:
    """Drop a leading "user[:secret]@" credential that precedes the host.

    The '@' counts as userinfo only when it appears before ``delim`` (the
    host/path boundary), matching the TS reference exactly. This also removes
    the ``git@`` in ``git@host:...`` / ``ssh://git@host/...``.
    """
    at = s.find("@")
    if at == -1:
        return s
    boundary = s.find(delim)
    if boundary == -1 or at < boundary:
        return s[at + 1:]
    return s
