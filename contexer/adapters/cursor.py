"""Cursor integration adapter."""
from pathlib import Path

NAME = "cursor"


def is_present(home: Path) -> bool:
    return (home / ".cursor").exists()
