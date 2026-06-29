"""Shared pytest fixtures for the contexer test suite."""
import pytest

from contexer import store


@pytest.fixture
def tmp_repo(tmp_path, monkeypatch):
    """Redirects STORE_DIR to a temp path and returns a fake repo path."""
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    return str(tmp_path / "repo")


@pytest.fixture
def populated_repo(tmp_repo):
    """A repo with two decisions pre-loaded."""
    store.update_decision(tmp_repo, "decided to use JWT instead of sessions — stateless, easier to scale", "sess-1")
    store.update_decision(tmp_repo, "constraint: never store plaintext passwords, always use bcrypt", "sess-1")
    return tmp_repo
