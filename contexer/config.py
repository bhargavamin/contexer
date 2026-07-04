"""Profile loader for ~/.contexer/config.toml.

Loader only — this does NOT wire mode/endpoint/token into the live server or
store paths (a later ticket does). With no config file (or absent keys) the
profile is pure-local: mode 'local', endpoint/token None, so existing behavior
is completely unchanged.
"""
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Mode = Literal["local", "team"]

CONFIG_PATH = Path.home() / ".contexer" / "config.toml"


@dataclass(frozen=True)
class Profile:
    """Resolved go-live profile. Pure-local by default."""

    mode: Mode = "local"
    endpoint: str | None = None
    token: str | None = None


def load_profile(path: Path | None = None) -> Profile:
    """Load the profile from config.toml, or the pure-local default if absent.

    Absent file / absent keys => Profile('local', None, None). A malformed file
    or an invalid value raises ConfigError with a clear message.
    """
    config_path = CONFIG_PATH if path is None else path

    if not config_path.exists():
        return Profile()

    try:
        data = tomllib.loads(config_path.read_text())
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"failed to parse {config_path}: {exc}") from exc

    mode = data.get("mode", "local")
    if mode not in ("local", "team"):
        raise ConfigError(f"invalid mode {mode!r} in {config_path}: expected 'local' or 'team'")

    endpoint = _opt_str(data, "endpoint", config_path)
    token = _opt_str(data, "token", config_path)

    return Profile(mode=mode, endpoint=endpoint, token=token)


def write_team_profile(endpoint: str, path: Path | None = None) -> None:
    """Persist a team profile to config.toml (mode='team' + endpoint), preserving any
    existing token. Creates the file/dir if absent — so `contexer login` self-configures and
    the user never hand-edits config.toml."""
    config_path = CONFIG_PATH if path is None else path
    existing = load_profile(config_path)
    lines = ['mode = "team"', f'endpoint = "{endpoint}"']
    if existing.token:
        lines.append(f'token = "{existing.token}"')
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("\n".join(lines) + "\n")


def _opt_str(data: dict, key: str, config_path: Path) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"invalid {key} in {config_path}: expected string, got {type(value).__name__}")
    return value


class ConfigError(Exception):
    """Raised when config.toml exists but cannot be parsed or is invalid."""
