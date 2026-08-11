"""Throwaway-HOME helpers for the built-in memory tool.

Pilot findings (benchmarks/MEMORY_PILOT.md, claude 2.1.226): the memory tool
is default-on with zero settings.json configuration, and no settings key
(documented or otherwise) disables it. The only lever found (--bare) is a CLI
flag that disables far more than memory, so it does not fit here. There is
therefore no way to actually turn memory off from settings.json, so the
campaign does not call write_home_settings at all: the "with" arm instead
counts and deletes whatever the memory tool wrote after every session
(memory_campaign._sweep_memory, built on memory_files() here). The function
is kept because MEMORY_PILOT.md and MEMORY_CAMPAIGN.md cite it as the lever
that was tried and found inert.
"""
import json
import re
from pathlib import Path


def write_home_settings(home: Path, memory_enabled: bool) -> Path:
    """Write <home>/.claude/settings.json. Always {} : see module docstring,
    there is no known key that disables the memory tool. memory_enabled is
    kept for interface symmetry with the benchmark harness's two arms."""
    p = home / ".claude" / "settings.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({}, indent=2))
    return p


def memory_dir(home: Path, repo: Path) -> Path:
    slug = re.sub(r"[^a-zA-Z0-9]", "-", str(repo))
    return home / ".claude" / "projects" / slug / "memory"


def memory_files(home: Path, repo: Path) -> list[Path]:
    d = memory_dir(home, repo)
    if not d.is_dir():
        return []
    return sorted(p for p in d.glob("*.md") if p.name != "MEMORY.md")
