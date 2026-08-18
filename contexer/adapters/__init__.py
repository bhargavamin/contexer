"""Registry of integration adapters. Add a module here to support a new tool."""
from pathlib import Path

from contexer.adapters import claude, cursor, codex, gemini

_ADAPTERS = {
    claude.NAME: claude,
    cursor.NAME: cursor,
    codex.NAME: codex,
    gemini.NAME: gemini,
}


def all_adapters() -> list:
    return list(_ADAPTERS.values())


def get(name: str):
    return _ADAPTERS[name]  # raises KeyError on unknown - caller maps to a CLI error


def detect(home: Path | None = None) -> list:
    home = home or Path.home()
    return [a for a in _ADAPTERS.values() if a.is_present(home)]


def select(target: str) -> list:
    """target is 'all', or a single adapter name."""
    if target == "all":
        return all_adapters()
    return [get(target)]  # KeyError on unknown
