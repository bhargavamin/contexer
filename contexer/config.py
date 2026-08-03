"""Profile loader for ~/.contexer/config.toml.

Loader only — this does NOT wire mode/endpoint/token into the live server or
store paths (a later ticket does). With no config file (or absent keys) the
profile is pure-local: mode 'local', endpoint/token None, so existing behavior
is completely unchanged.
"""
import os
import tempfile
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
    lines.extend(_preserved_ui_lines(config_path))  # `contexer login` must not reset [ui]
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text("\n".join(lines) + "\n")
    config_path.chmod(0o600)  # may hold a bearer token — owner-only, like .team_auth.json


# ── the [ui] table (local console) ───────────────────────────────────────────────

@dataclass(frozen=True)
class UiSettings:
    """Resolved `[ui]` settings for the local console. Absent config => these defaults.

    autostart is opt-in: installing an OSS tool must not imply a background listener.
    The port is fixed rather than scanned so a printed console URL survives a restart.
    """

    port: int = 31415
    autostart: bool = False
    idle_timeout_minutes: int = 60


# The only keys write_settings() accepts. `mode`, `endpoint` and `token` are deliberately
# absent: they are wiring and credentials, owned by `contexer login` / `logout` /
# write_team_profile. A browser-driven request reaches write_settings(), so this tuple is a
# security boundary, not a convenience.
SETTABLE_KEYS = ("redact_secrets", "skip_confirm", "autostart", "port", "idle_timeout_minutes")


def load_ui_settings(path: Path | None = None) -> UiSettings:
    """Load the `[ui]` table from config.toml, or the defaults if it is absent.

    Same contract as load_profile: absent file / absent table / absent keys => UiSettings().
    A malformed file or an invalid value raises ConfigError with a clear message.
    """
    config_path = CONFIG_PATH if path is None else path

    if not config_path.exists():
        return UiSettings()

    try:
        data = tomllib.loads(config_path.read_text())
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"failed to parse {config_path}: {exc}") from exc

    table = data.get("ui", {})
    if not isinstance(table, dict):
        raise ConfigError(
            f"invalid [ui] in {config_path}: expected a table, got {type(table).__name__}")

    defaults = UiSettings()
    return UiSettings(
        port=_int_value(table.get("port", defaults.port), "ui.port", config_path,
                        minimum=1, maximum=65535),
        autostart=_bool_value(table.get("autostart", defaults.autostart),
                              "ui.autostart", config_path),
        idle_timeout_minutes=_int_value(
            table.get("idle_timeout_minutes", defaults.idle_timeout_minutes),
            "ui.idle_timeout_minutes", config_path, minimum=1),
    )


def write_settings(path: Path | None = None, /, **allowlisted: object) -> None:
    """Rewrite config.toml with the given settings applied, preserving everything else.

    Read-modify-write over the resolved Profile + `[ui]` table: a key that is not passed
    keeps its current value, so a save from the console can never drop the teams token.
    Only SETTABLE_KEYS are accepted; anything else — including `token`/`endpoint`/`mode` —
    raises ConfigError. `path` is positional-only so that splatting an untrusted request
    body cannot redirect the write to an arbitrary file.

    The previous file is copied to `<name>.bak` first: tomllib is read-only and the rewrite
    is hand-serialized from parsed values, so HAND-WRITTEN COMMENTS DO NOT SURVIVE a save.
    The backup is the mitigation.
    """
    config_path = CONFIG_PATH if path is None else path
    unknown = sorted(set(allowlisted) - set(SETTABLE_KEYS))
    if unknown:
        raise ConfigError(
            f"cannot write {', '.join(unknown)}: settable keys are {', '.join(SETTABLE_KEYS)}")

    profile = load_profile(config_path)
    ui = load_ui_settings(config_path)
    caller = "write_settings()"
    # Validate before touching the file — a rejected value must not leave a half-written
    # config, and every value written here has to survive load_profile/load_ui_settings.
    skip_confirm = _bool_value(allowlisted.get("skip_confirm", profile.skip_confirm),
                               "skip_confirm", caller)
    redact_secrets = _bool_value(allowlisted.get("redact_secrets", profile.redact_secrets),
                                 "redact_secrets", caller)
    merged = UiSettings(
        port=_int_value(allowlisted.get("port", ui.port), "ui.port", caller,
                        minimum=1, maximum=65535),
        autostart=_bool_value(allowlisted.get("autostart", ui.autostart), "ui.autostart", caller),
        idle_timeout_minutes=_int_value(
            allowlisted.get("idle_timeout_minutes", ui.idle_timeout_minutes),
            "ui.idle_timeout_minutes", caller, minimum=1),
    )

    lines = []
    if profile.mode != "local":
        lines.append(f"mode = {_toml_str(profile.mode)}")
    if profile.endpoint:
        lines.append(f"endpoint = {_toml_str(profile.endpoint)}")
    if profile.token:
        lines.append(f"token = {_toml_str(profile.token)}")
    if skip_confirm:
        lines.append("skip_confirm = true")
    if not redact_secrets:
        lines.append("redact_secrets = false")
    lines.extend(_ui_lines(merged))

    config_path.parent.mkdir(parents=True, exist_ok=True)
    if config_path.exists():
        _atomic_write_private(config_path.with_name(config_path.name + ".bak"),
                              config_path.read_bytes())
    _atomic_write_private(config_path, ("\n".join(lines) + "\n").encode())


def _preserved_ui_lines(config_path: Path) -> list[str]:
    """The `[ui]` table to carry into a rewritten config.toml.

    Preserving `[ui]` RIDES ALONG with a credential write, so it must never be able to abort
    one: `contexer login` used to complete the entire browser flow, save the tokens, and then
    die here on a hand-edited `port = "31500"` — leaving mode/endpoint unwritten and team sync
    off with nothing pointing at why. A table that will not validate is therefore copied
    through VERBATIM rather than dropped or silently "corrected": login is not the place to
    rewrite a value the user typed, and the loaders that actually read `[ui]` still reject it,
    where the error means something.
    """
    try:
        return _ui_lines(load_ui_settings(config_path))
    except ConfigError:
        return _raw_ui_lines(config_path)


def _raw_ui_lines(config_path: Path) -> list[str]:
    """The file's `[ui]` header and everything after it, unparsed.

    TOML binds every key after a table header to that table, so the header to EOF IS the
    table — and the file has already parsed as TOML (load_profile would have raised first),
    so copying that tail through cannot produce something unreadable."""
    lines = config_path.read_text().splitlines()
    for index, line in enumerate(lines):
        if line.strip().startswith("[ui]"):
            return ["", *lines[index:]]
    return []


def _ui_lines(ui: UiSettings) -> list[str]:
    """The `[ui]` table as non-default keys only, or nothing at all when it is all defaults.

    Callers must append this LAST: TOML binds every key after a table header to that table."""
    defaults = UiSettings()
    keys = []
    if ui.port != defaults.port:
        keys.append(f"port = {ui.port}")
    if ui.autostart:
        keys.append("autostart = true")
    if ui.idle_timeout_minutes != defaults.idle_timeout_minutes:
        keys.append(f"idle_timeout_minutes = {ui.idle_timeout_minutes}")
    return ["", "[ui]", *keys] if keys else []


def _atomic_write_private(path: Path, data: bytes) -> None:
    """Write via a unique temp file + os.replace, owner-only from the first byte.

    mkstemp creates the temp file 0600 regardless of umask, so a file that may hold a
    bearer token is never even briefly group/world-readable (write_text + chmod is).
    Duplicates store._atomic_write deliberately: config.py has to stay at the bottom of
    the import graph — store imports config, so importing store here would cycle."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f"{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)  # no-op after a successful replace


def _bool_value(value: object, key: str, where: Path | str) -> bool:
    if not isinstance(value, bool):
        raise ConfigError(
            f"invalid {key} in {where}: expected true/false, got {type(value).__name__}")
    return value


def _int_value(value: object, key: str, where: Path | str, *,
               minimum: int, maximum: int | None = None) -> int:
    # bool is an int subclass, so without the first check `port = true` becomes port 1.
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(
            f"invalid {key} in {where}: expected an integer, got {type(value).__name__}")
    if value < minimum or (maximum is not None and value > maximum):
        bound = f"{minimum}-{maximum}" if maximum is not None else f">= {minimum}"
        raise ConfigError(f"invalid {key} in {where}: expected {bound}, got {value}")
    return value


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
