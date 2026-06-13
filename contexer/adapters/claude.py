"""Claude Code integration adapter."""
from pathlib import Path

NAME = "claude"


def is_present(home: Path) -> bool:
    # Claude's config may be a directory (~/.claude) or a standalone file (~/.claude.json).
    return (home / ".claude").exists() or (home / ".claude.json").exists()
