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
import subprocess
from functools import lru_cache

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


def canonical_repo_key(remote: str | None) -> str | None:
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
        if scheme == "ssh://":
            host = _resolve_ssh_host(host)
    else:
        # scp-like: [userinfo@]host:path - host/path split on the first ':'.
        # Covers git@host:owner/repo AND the user-less github.com:owner/repo.
        rest = _strip_userinfo(remote, ":")
        host, _, path = rest.partition(":")
        if path:  # a real scp-form remote (host:path) rides ssh -> resolve aliases
            host = _resolve_ssh_host(host)

    host = host.lower()
    path = path.lstrip("/")
    result = host if path == "" else host + "/" + path

    if result.lower().endswith(".git"):
        result = result[: -len(".git")]
    result = result.rstrip("/")

    return result.lower()


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
