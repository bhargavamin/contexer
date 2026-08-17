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


# ── skip_confirm (personal-cloud push confirmation opt-out) ──────────────────────

def test_skip_confirm_defaults_false(config_path):
    config_path.parent.mkdir(parents=True)
    config_path.write_text('mode = "team"\nendpoint = "http://x/mcp"\n')
    assert load_profile().skip_confirm is False


def test_skip_confirm_true_parsed(config_path):
    config_path.parent.mkdir(parents=True)
    config_path.write_text('mode = "team"\nendpoint = "http://x/mcp"\nskip_confirm = true\n')
    assert load_profile().skip_confirm is True


def test_skip_confirm_invalid_type_raises(config_path):
    config_path.parent.mkdir(parents=True)
    config_path.write_text('skip_confirm = "yes"\n')
    with pytest.raises(ConfigError):
        load_profile()


def test_write_team_profile_preserves_skip_confirm(config_path):
    config_path.parent.mkdir(parents=True)
    config_path.write_text('mode = "team"\nendpoint = "http://x/mcp"\nskip_confirm = true\n')
    config.write_team_profile("http://new/mcp")  # e.g. `contexer login` re-writes config
    assert load_profile().skip_confirm is True


# ── redact_secrets (secret redaction, ON by default) ─────────────────────────

def test_redact_secrets_defaults_true(config_path):
    config_path.parent.mkdir(parents=True)
    config_path.write_text('mode = "team"\nendpoint = "http://x/mcp"\n')
    assert load_profile().redact_secrets is True  # safety property holds with zero config


def test_redact_secrets_absent_file_defaults_true(config_path):
    assert not config_path.exists()
    assert load_profile().redact_secrets is True


def test_redact_secrets_false_parsed(config_path):
    config_path.parent.mkdir(parents=True)
    config_path.write_text('mode = "team"\nendpoint = "http://x/mcp"\nredact_secrets = false\n')
    assert load_profile().redact_secrets is False


def test_redact_secrets_invalid_type_raises(config_path):
    config_path.parent.mkdir(parents=True)
    config_path.write_text('redact_secrets = "no"\n')
    with pytest.raises(ConfigError):
        load_profile()


def test_write_team_profile_preserves_redact_secrets_optout(config_path):
    config_path.parent.mkdir(parents=True)
    config_path.write_text('mode = "team"\nendpoint = "http://x/mcp"\nredact_secrets = false\n')
    config.write_team_profile("http://new/mcp")
    assert load_profile().redact_secrets is False  # opt-out survives `contexer login`


# ── redaction_enabled(): the ONE implementation of the fail-soft default ──────
# store._redaction_enabled and remote._redaction_enabled are delegates, so this
# branch is the only copy of "a broken config can never leak secrets" left. It has
# to be pinned here, or a future edit flipping the fallback to False lands unnoticed.

def test_redaction_enabled_true_with_no_config(config_path):
    assert not config_path.exists()
    assert config.redaction_enabled() is True


def test_redaction_enabled_honors_the_optout(config_path):
    config_path.parent.mkdir(parents=True)
    config_path.write_text('redact_secrets = false\n')
    assert config.redaction_enabled() is False


def test_redaction_enabled_fails_soft_to_on(config_path, monkeypatch):
    """A malformed config raises ConfigError out of load_profile — redaction must still
    report ON, since every caller is an egress path that would otherwise leak or raise."""
    config_path.parent.mkdir(parents=True)
    config_path.write_text('redact_secrets = "not-a-bool"\n')
    with pytest.raises(ConfigError):
        load_profile()                      # the underlying read really does raise
    assert config.redaction_enabled() is True

    def boom(*_a, **_k):
        raise RuntimeError("config subsystem is broken")

    monkeypatch.setattr(config, "load_profile", boom)
    assert config.redaction_enabled() is True   # ANY exception, not just ConfigError


def test_delegates_fail_soft_when_the_symbol_is_unresolvable(config_path, monkeypatch):
    """The delegates import redaction_enabled INSIDE their try. Deleting the name is the
    one way to prove that: an unguarded `from contexer.config import ...` would raise
    ImportError straight out of get_shareable / _wire_args instead of degrading to ON."""
    from contexer import remote, store

    monkeypatch.delattr(config, "redaction_enabled")
    assert store._redaction_enabled() is True
    assert remote._redaction_enabled() is True
