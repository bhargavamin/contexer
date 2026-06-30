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
