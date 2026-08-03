"""Tests for the `[ui]` table and the settings writer (~/.contexer/config.toml)."""
import pytest

from contexer import config
from contexer.config import ConfigError, UiSettings, load_profile, load_ui_settings, write_settings


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    """A temp config path. CONFIG_PATH is monkeypatched too, so a bug that ignores the
    explicit path argument still cannot reach the real ~/.contexer/config.toml."""
    path = tmp_path / ".contexer" / "config.toml"
    monkeypatch.setattr(config, "CONFIG_PATH", path)
    return path


def bak(cfg):
    return cfg.with_name(cfg.name + ".bak")


# ── load_ui_settings ─────────────────────────────────────────────────────────────

def test_absent_file_yields_defaults(cfg):
    assert not cfg.exists()
    assert load_ui_settings(cfg) == UiSettings(port=31415, autostart=False,
                                               idle_timeout_minutes=60)


def test_absent_ui_table_yields_defaults(cfg):
    cfg.parent.mkdir(parents=True)
    cfg.write_text('mode = "team"\nendpoint = "http://x/mcp"\n')
    assert load_ui_settings(cfg) == UiSettings()  # autostart stays off without opt-in


def test_ui_table_parsed(cfg):
    cfg.parent.mkdir(parents=True)
    cfg.write_text("[ui]\nport = 8123\nautostart = true\nidle_timeout_minutes = 5\n")
    assert load_ui_settings(cfg) == UiSettings(port=8123, autostart=True,
                                               idle_timeout_minutes=5)


def test_partial_ui_table_keeps_other_defaults(cfg):
    cfg.parent.mkdir(parents=True)
    cfg.write_text("[ui]\nautostart = true\n")
    settings = load_ui_settings(cfg)
    assert settings.autostart is True
    assert (settings.port, settings.idle_timeout_minutes) == (31415, 60)


def test_ui_settings_coexist_with_profile(cfg):
    cfg.parent.mkdir(parents=True)
    cfg.write_text('mode = "team"\ntoken = "t"\n\n[ui]\nport = 40000\n')
    assert load_profile(cfg).token == "t"       # the new table does not disturb the profile
    assert load_ui_settings(cfg).port == 40000


def test_malformed_toml_raises(cfg):
    cfg.parent.mkdir(parents=True)
    cfg.write_text("[ui]\nport = = 1")
    with pytest.raises(ConfigError):
        load_ui_settings(cfg)


def test_ui_not_a_table_raises(cfg):
    cfg.parent.mkdir(parents=True)
    cfg.write_text("ui = 5\n")
    with pytest.raises(ConfigError):
        load_ui_settings(cfg)


@pytest.mark.parametrize("body", [
    'port = "8080"',            # string, not integer
    "port = true",              # bool is an int subclass — must not become port 1
    "port = 0",
    "port = 65536",
    "port = 1.5",
    'autostart = "yes"',
    "autostart = 1",
    "idle_timeout_minutes = 0",
    "idle_timeout_minutes = -1",
    'idle_timeout_minutes = "60"',
])
def test_malformed_ui_value_raises(cfg, body):
    cfg.parent.mkdir(parents=True)
    cfg.write_text(f"[ui]\n{body}\n")
    with pytest.raises(ConfigError):
        load_ui_settings(cfg)


def test_port_bounds_are_inclusive(cfg):
    cfg.parent.mkdir(parents=True)
    cfg.write_text("[ui]\nport = 65535\n")
    assert load_ui_settings(cfg).port == 65535


# ── write_settings: round-trip ───────────────────────────────────────────────────

def test_write_settings_creates_config_when_absent(cfg):
    write_settings(cfg, autostart=True, port=40100)
    assert load_ui_settings(cfg) == UiSettings(port=40100, autostart=True,
                                               idle_timeout_minutes=60)
    assert not bak(cfg).exists()  # nothing to back up


def test_write_settings_round_trips_every_field(cfg):
    write_settings(cfg, port=40200, autostart=True, idle_timeout_minutes=15,
                   skip_confirm=True, redact_secrets=False)
    assert load_ui_settings(cfg) == UiSettings(port=40200, autostart=True,
                                              idle_timeout_minutes=15)
    profile = load_profile(cfg)
    assert profile.skip_confirm is True
    assert profile.redact_secrets is False


def test_write_settings_defaults_omit_the_ui_table(cfg):
    write_settings(cfg, port=31415, autostart=False, idle_timeout_minutes=60)
    assert "[ui]" not in cfg.read_text()          # defaults are expressed by absence
    assert load_ui_settings(cfg) == UiSettings()  # and still round-trip


def test_write_settings_is_incremental(cfg):
    write_settings(cfg, port=40300)
    write_settings(cfg, autostart=True)
    assert load_ui_settings(cfg) == UiSettings(port=40300, autostart=True,
                                               idle_timeout_minutes=60)


def test_write_settings_is_owner_only(cfg):
    cfg.parent.mkdir(parents=True)
    cfg.write_text('token = "secret"\n')
    write_settings(cfg, autostart=True)
    assert (cfg.stat().st_mode & 0o777) == 0o600      # holds a bearer token
    assert (bak(cfg).stat().st_mode & 0o777) == 0o600  # so does the copy


# ── write_settings: preservation ─────────────────────────────────────────────────

def test_write_settings_preserves_the_teams_token(cfg):
    cfg.parent.mkdir(parents=True)
    cfg.write_text('mode = "team"\nendpoint = "http://x/mcp"\ntoken = "keep-me"\n')
    write_settings(cfg, autostart=True)
    profile = load_profile(cfg)
    assert profile.token == "keep-me"  # dropping it would silently log the user out
    assert profile.mode == "team"
    assert profile.endpoint == "http://x/mcp"


def test_write_settings_preserves_untouched_flags(cfg):
    cfg.parent.mkdir(parents=True)
    cfg.write_text("skip_confirm = true\nredact_secrets = false\n")
    write_settings(cfg, port=40400)
    profile = load_profile(cfg)
    assert profile.skip_confirm is True
    assert profile.redact_secrets is False


def test_write_settings_escapes_toml_specials_in_preserved_values(cfg):
    cfg.parent.mkdir(parents=True)
    cfg.write_text('mode = "team"\ntoken = "we\\"ird\\\\tok"\n')
    write_settings(cfg, autostart=True)
    assert load_profile(cfg).token == 'we"ird\\tok'  # rewritten file is still valid TOML


def test_write_team_profile_preserves_the_ui_table(cfg):
    write_settings(cfg, autostart=True, port=40500)
    config.write_team_profile("http://new/mcp", cfg)  # e.g. `contexer login`
    assert load_ui_settings(cfg) == UiSettings(port=40500, autostart=True,
                                               idle_timeout_minutes=60)


@pytest.mark.parametrize("body", [
    'port = "31500"',            # quoted: valid TOML, rejected by _int_value
    "idle_timeout_minutes = 0",
    "autostart = 1",
])
def test_write_team_profile_survives_an_unloadable_ui_table(cfg, body):
    """`contexer login` runs this AFTER the browser flow and the creds file. Aborting on a
    hand-edited [ui] leaves mode/endpoint unwritten — team sync stays off, permanently, and
    every retry re-runs the whole flow and fails identically."""
    cfg.parent.mkdir(parents=True)
    cfg.write_text(f'token = "keep-me"\nskip_confirm = true\n\n[ui]\n{body}\n')
    config.write_team_profile("http://new/mcp", cfg)
    profile = load_profile(cfg)
    assert (profile.mode, profile.endpoint) == ("team", "http://new/mcp")
    assert profile.token == "keep-me" and profile.skip_confirm is True
    assert body in cfg.read_text()  # copied through, not silently rewritten or dropped
    with pytest.raises(ConfigError):
        load_ui_settings(cfg)  # still rejected where the error means something


# ── write_settings: the allowlist is a security boundary ─────────────────────────

def test_unknown_key_raises(cfg):
    with pytest.raises(ConfigError, match="cannot write nope"):
        write_settings(cfg, nope=1)
    assert not cfg.exists()  # rejected before anything is written


@pytest.mark.parametrize("key, value", [
    ("token", "attacker-token"),
    ("endpoint", "http://evil/mcp"),
    ("mode", "team"),
])
def test_credentials_and_wiring_are_unreachable(cfg, key, value):
    cfg.parent.mkdir(parents=True)
    cfg.write_text('mode = "local"\ntoken = "mine"\n')
    with pytest.raises(ConfigError, match=f"cannot write {key}"):
        write_settings(cfg, **{key: value})
    profile = load_profile(cfg)
    assert (profile.mode, profile.endpoint, profile.token) == ("local", None, "mine")


def test_path_cannot_be_hijacked_by_a_request_body(cfg, tmp_path):
    """`path` is positional-only, so splatting untrusted JSON cannot redirect the write."""
    elsewhere = tmp_path / "elsewhere.toml"
    with pytest.raises(ConfigError, match="cannot write path"):
        write_settings(**{"path": str(elsewhere), "autostart": True})
    assert not elsewhere.exists()


@pytest.mark.parametrize("kwargs", [
    {"port": "8080"},
    {"port": True},
    {"port": 0},
    {"port": 65536},
    {"autostart": "yes"},
    {"autostart": 1},
    {"idle_timeout_minutes": 0},
    {"skip_confirm": "true"},
    {"redact_secrets": None},
])
def test_malformed_written_value_raises_and_leaves_the_file_alone(cfg, kwargs):
    cfg.parent.mkdir(parents=True)
    cfg.write_text('token = "mine"\n')
    with pytest.raises(ConfigError):
        write_settings(cfg, **kwargs)
    assert cfg.read_text() == 'token = "mine"\n'


# ── write_settings: the backup, and the comment loss it mitigates ────────────────

def test_backup_holds_the_previous_content(cfg):
    cfg.parent.mkdir(parents=True)
    cfg.write_text('mode = "team"\ntoken = "keep-me"\n')
    write_settings(cfg, autostart=True)
    assert bak(cfg).read_text() == 'mode = "team"\ntoken = "keep-me"\n'
    assert "autostart = true" in cfg.read_text()


def test_comments_are_lost_but_survive_in_the_backup(cfg):
    """Pinned, not aspirational: tomllib is read-only and the rewrite is hand-serialized,
    so comments cannot survive a save from the console. Hence the .bak."""
    original = '# my hand-written note\nmode = "team"  # inline note\ntoken = "keep-me"\n'
    cfg.parent.mkdir(parents=True)
    cfg.write_text(original)
    write_settings(cfg, autostart=True)
    assert "# my hand-written note" not in cfg.read_text()
    assert "# inline note" not in cfg.read_text()
    assert bak(cfg).read_text() == original
