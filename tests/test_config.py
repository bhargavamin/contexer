"""Tests for the go-live profile loader (~/.contexer/config.toml)."""
import pytest

from contexer import config
from contexer.config import ConfigError, Profile, load_profile


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    """Point CONFIG_PATH at a temp file — never touches the real ~/.contexer."""
    path = tmp_path / ".contexer" / "config.toml"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    return path


def test_write_team_profile_creates_config(config_path):
    config.write_team_profile("http://localhost:8080/mcp")
    prof = load_profile()
    assert prof.mode == "team"
    assert prof.endpoint == "http://localhost:8080/mcp"
    assert prof.token is None


def test_write_team_profile_preserves_existing_token(config_path):
    config_path.parent.mkdir(parents=True)
    config_path.write_text('mode = "team"\nendpoint = "http://old/mcp"\ntoken = "keep-me"\n')
    config.write_team_profile("http://new/mcp")
    prof = load_profile()
    assert prof.endpoint == "http://new/mcp"
    assert prof.token == "keep-me"  # a pasted token survives login self-config


def test_write_team_profile_is_owner_only(config_path):
    config.write_team_profile("http://localhost:8080/mcp")
    assert (config_path.stat().st_mode & 0o777) == 0o600  # may hold a bearer token


def test_write_team_profile_escapes_toml_specials(config_path):
    config_path.parent.mkdir(parents=True)
    config_path.write_text('mode = "team"\nendpoint = "http://old/mcp"\ntoken = "we\\"ird\\\\tok"\n')
    config.write_team_profile("http://new/mcp")
    prof = load_profile()  # round-trips: the rewritten file is still valid TOML
    assert prof.token == 'we"ird\\tok'


def test_default_endpoint_env(monkeypatch):
    monkeypatch.setenv("CONTEXER_ENV", "local")
    assert config.default_endpoint() == "http://localhost:8080/mcp"
    monkeypatch.delenv("CONTEXER_ENV", raising=False)
    # The stable production domain — environment selection happens in DNS, not code.
    assert config.default_endpoint() == "https://mcp.contexer.ai/mcp"


def test_absent_file_is_pure_local(config_path):
    assert not config_path.exists()
    profile = load_profile()
    assert profile == Profile(mode="local", endpoint=None, token=None)


def test_absent_keys_default_to_local(config_path):
    config_path.parent.mkdir(parents=True)
    config_path.write_text("")
    assert load_profile() == Profile(mode="local", endpoint=None, token=None)


def test_valid_team_config_parses(config_path):
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        'mode = "team"\n'
        'endpoint = "https://teams.example.com/mcp"\n'
        'token = "secret-123"\n'
    )
    assert load_profile() == Profile(
        mode="team",
        endpoint="https://teams.example.com/mcp",
        token="secret-123",
    )


def test_malformed_toml_raises(config_path):
    config_path.parent.mkdir(parents=True)
    config_path.write_text("mode = = broken")
    with pytest.raises(ConfigError):
        load_profile()


def test_invalid_mode_raises(config_path):
    config_path.parent.mkdir(parents=True)
    config_path.write_text('mode = "remote"')
    with pytest.raises(ConfigError):
        load_profile()


def test_non_string_token_raises(config_path):
    config_path.parent.mkdir(parents=True)
    config_path.write_text("token = 123")
    with pytest.raises(ConfigError):
        load_profile()
