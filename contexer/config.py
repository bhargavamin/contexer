"""Profile loader for ~/.contexer/config.toml.

Loader only — this does NOT wire mode/endpoint/token into the live server or
store paths (a later ticket does). With no config file (or absent keys) the
profile is pure-local: mode 'local', endpoint/token None, so existing behavior
is completely unchanged.
"""
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

Mode = Literal["local", "team"]

CONFIG_PATH = Path.home() / ".contexer" / "config.toml"

# Teams endpoint defaults — the single source of truth for every consumer (login,
# the opt-in native MCP registration). Code ships ONLY the stable production domain;
# which stack answers it (dev today, prod later) is decided in DNS, so promoting
# infrastructure never needs a client release and never strands an old endpoint in
# users' config.toml files. localhost is the explicit developer opt-in.
DEFAULT_ENDPOINT_PROD = "https://mcp.contexer.ai/mcp"
DEFAULT_ENDPOINT_LOCAL = "http://localhost:8080/mcp"


def default_endpoint() -> str:
    """The Teams endpoint used when none is given: localhost under CONTEXER_ENV=local, else prod."""
    return DEFAULT_ENDPOINT_LOCAL if os.environ.get("CONTEXER_ENV") == "local" else DEFAULT_ENDPOINT_PROD


@dataclass(frozen=True)
class Profile:
    """Resolved go-live profile. Pure-local by default."""

    mode: Mode = "local"
    endpoint: str | None = None
    token: str | None = None
    # Opt-out of the personal-cloud push confirmation. Default False = review-before-push is
    # ON (share_decision previews first). Set `skip_confirm = true` in config.toml to always
    # push without the preview.
    skip_confirm: bool = False
    # Secret redaction on EGRESS. Default True = secrets are scrubbed before any push leaves the
    # machine (share projection/preview + wire); capture is NOT redacted, so the local store stays
    # a faithful record. Set `redact_secrets = false` in config.toml to opt out (power users who
    # accept the risk); default-on so the safety property holds unconfigured.
    redact_secrets: bool = True


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

    skip_confirm = data.get("skip_confirm", False)
    if not isinstance(skip_confirm, bool):
        raise ConfigError(
            f"invalid skip_confirm in {config_path}: expected true/false, "
            f"got {type(skip_confirm).__name__}")

    redact_secrets = data.get("redact_secrets", True)
    if not isinstance(redact_secrets, bool):
        raise ConfigError(
            f"invalid redact_secrets in {config_path}: expected true/false, "
            f"got {type(redact_secrets).__name__}")

    return Profile(mode=mode, endpoint=endpoint, token=token,
                   skip_confirm=skip_confirm, redact_secrets=redact_secrets)


def write_team_profile(endpoint: str, path: Path | None = None) -> None:
    """Persist a team profile to config.toml (mode='team' + endpoint), preserving any
    existing token. Creates the file/dir if absent — so `contexer login` self-configures and
    the user never hand-edits config.toml."""
    config_path = CONFIG_PATH if path is None else path
    existing = load_profile(config_path)
    lines = ['mode = "team"', f"endpoint = {_toml_str(endpoint)}"]
    if existing.token:
        lines.append(f"token = {_toml_str(existing.token)}")
    if existing.skip_confirm:  # preserve the push-confirm opt-out across `contexer login`
        lines.append("skip_confirm = true")
    if not existing.redact_secrets:  # preserve the redaction opt-out across `contexer login`
        lines.append("redact_secrets = false")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("\n".join(lines) + "\n")
    config_path.chmod(0o600)  # may hold a bearer token — owner-only, like .team_auth.json


def _toml_str(value: str) -> str:
    """A TOML basic-string literal — escaped, so a `"` or `\\` in a value can't corrupt the file."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _opt_str(data: dict, key: str, config_path: Path) -> str | None:
    value = data.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ConfigError(f"invalid {key} in {config_path}: expected string, got {type(value).__name__}")
    return value


class ConfigError(Exception):
    """Raised when config.toml exists but cannot be parsed or is invalid."""
