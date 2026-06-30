"""Tests for canonical_repo_key — shared byte-for-byte with the TS sibling impl."""
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
