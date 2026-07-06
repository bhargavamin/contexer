"""Tests for canonical_repo_key — shared byte-for-byte with the TS sibling impl."""
import types

import pytest

from contexer.repo_key import canonical_repo_key

# Each vector is SHARED with the TypeScript implementation; output must be identical.
VECTORS = [
    ("https://github.com/Owner/Repo.git", "github.com/owner/repo"),
    ("https://github.com/owner/repo", "github.com/owner/repo"),
    ("http://github.com/owner/repo/", "github.com/owner/repo"),
    ("git@github.com:owner/repo.git", "github.com/owner/repo"),
    ("git@github.com:Owner/Repo", "github.com/owner/repo"),
    ("ssh://git@github.com/owner/repo.git", "github.com/owner/repo"),
    ("git://github.com/owner/repo.git", "github.com/owner/repo"),
    ("https://user:token@github.com/owner/repo.git", "github.com/owner/repo"),
    ("https://GitHub.com/owner/repo", "github.com/owner/repo"),
    ("https://gitlab.com/group/subgroup/repo.git", "gitlab.com/group/subgroup/repo"),
    # scp shorthand without an explicit user (valid git remote form)
    ("github.com:owner/repo", "github.com/owner/repo"),
    ("github.com:owner/repo.git", "github.com/owner/repo"),
    ("git@github.com/owner/repo", "github.com/owner/repo"),
    ("github.com/owner/repo", "github.com/owner/repo"),
    ("", None),
    (None, None),
    ("   ", None),
]


@pytest.mark.parametrize("remote, expected", VECTORS)
def test_canonical_repo_key(remote, expected):
    assert canonical_repo_key(remote) == expected


# ── SSH host-alias resolution (multi-account remotes must not shard team context) ──

class TestSshAliasResolution:
    @pytest.fixture
    def alias_config(self, monkeypatch):
        """A machine whose ~/.ssh/config maps github.com-work -> github.com."""
        from contexer import repo_key
        monkeypatch.setattr(
            repo_key, "_resolve_ssh_host",
            lambda h: {"github.com-work": "github.com"}.get(h, h))

    def test_scp_alias_resolves_to_real_host(self, alias_config):
        assert canonical_repo_key("git@github.com-work:owner/repo.git") == "github.com/owner/repo"

    def test_ssh_scheme_alias_resolves(self, alias_config):
        assert canonical_repo_key("ssh://git@github.com-work/owner/repo.git") == "github.com/owner/repo"

    def test_unknown_host_kept_verbatim(self, alias_config):
        assert canonical_repo_key("git@example.org:owner/repo.git") == "example.org/owner/repo"

    def test_https_never_consults_ssh(self, monkeypatch):
        from contexer import repo_key
        monkeypatch.setattr(repo_key, "_resolve_ssh_host",
                            lambda h: pytest.fail("https must not resolve ssh aliases"))
        assert canonical_repo_key("https://github.com-work/owner/repo") == "github.com-work/owner/repo"


class TestSshHostnameSeam:
    """The uncached `ssh -G` seam - subprocess faked, never the real ssh."""

    def _fake_run(self, monkeypatch, *, stdout="", returncode=0, exc=None):
        from contexer import repo_key

        def run(cmd, **kw):
            assert cmd[:2] == ["ssh", "-G"]
            if exc is not None:
                raise exc
            return types.SimpleNamespace(returncode=returncode, stdout=stdout)
        monkeypatch.setattr(repo_key.subprocess, "run", run)

    def test_resolves_hostname_line(self, monkeypatch):
        from contexer import repo_key
        self._fake_run(monkeypatch, stdout="user git\nhostname github.com\nport 22\n")
        assert repo_key._ssh_hostname("github.com-work") == "github.com"

    def test_nonzero_exit_keeps_host(self, monkeypatch):
        from contexer import repo_key
        self._fake_run(monkeypatch, returncode=255)
        assert repo_key._ssh_hostname("weird-host") == "weird-host"

    def test_missing_ssh_binary_keeps_host(self, monkeypatch):
        from contexer import repo_key
        self._fake_run(monkeypatch, exc=FileNotFoundError("no ssh"))
        assert repo_key._ssh_hostname("github.com-work") == "github.com-work"

    def test_empty_hostname_keeps_host(self, monkeypatch):
        from contexer import repo_key
        self._fake_run(monkeypatch, stdout="hostname \n")
        assert repo_key._ssh_hostname("h") == "h"

    def test_empty_host_short_circuits(self):
        from contexer import repo_key
        assert repo_key._ssh_hostname("") == ""
