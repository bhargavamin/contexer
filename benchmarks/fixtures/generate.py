"""Synthetic fixture repos for the A/B benchmark. Seeded and generated so the
code cannot exist in any model's training data (leakage guard)."""
import subprocess
from pathlib import Path

_PYPROJECT = """[project]
name = "svc-{seed}-webapi"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["fastapi", "sqlalchemy", "pytest"]

[tool.ruff]
line-length = 100

[tool.pytest.ini_options]
addopts = "-q"
"""


def _sh(*args: str, cwd: Path) -> None:
    subprocess.run(args, cwd=cwd, check=True, capture_output=True)


def _commit(dest: Path, msg: str, *git_args: str) -> None:
    _sh("git", "-c", "user.email=fixture@bench.local", "-c", "user.name=Bench",
        "-c", "commit.gpgsign=false", "commit", *git_args, "-q", "-m", msg, cwd=dest)


def build_webapi(dest: Path, seed: int = 0) -> Path:
    app, tests = dest / "app", dest / "tests"
    app.mkdir(parents=True)
    tests.mkdir()
    (dest / "pyproject.toml").write_text(_PYPROJECT.format(seed=seed))
    (app / "__init__.py").write_text("")

    core = ["import json", "import os", "", ""]
    for i in range(25):
        core += [
            f"def fetch_record_{seed}_{i}(record_id: int) -> dict:",
            f'    """Fetches record {i} for service {seed}."""',
            f'    return {{"id": record_id, "slot": {i}}}',
            "", "",
        ]
    (app / f"svc_{seed}_core.py").write_text("\n".join(core))

    body = "\n".join(
        f"def test_fetch_{seed}_{i}():\n    assert {i} + 1 == {i + 1}\n" for i in range(25))
    for n in range(3):
        (tests / f"test_svc_{n}.py").write_text(body)

    _sh("git", "init", "-q", cwd=dest)
    # Repo-local identity so a commit made INSIDE the fixture (by a benchmarked
    # session, or by the enforcement task verifying the guard) succeeds without a
    # ~/.gitconfig. The throwaway HOME has none, and git's hostname fallback fails
    # on any host without an FQDN — which would look like a guard block.
    _sh("git", "config", "user.email", "fixture@bench.local", cwd=dest)
    _sh("git", "config", "user.name", "Bench", cwd=dest)
    _sh("git", "config", "commit.gpgsign", "false", cwd=dest)
    # No background maintenance in fixture repos: an auto-detached `git gc` after a
    # commit races the runner's copytree of this repo (transient pack files vanish
    # mid-copy -> shutil.Error).
    _sh("git", "config", "gc.auto", "0", cwd=dest)
    _sh("git", "config", "gc.autoDetach", "false", cwd=dest)
    for i in range(22):
        _commit(dest, f"feat: add capability {seed}-{i}", "--allow-empty")
    _sh("git", "add", "-A", cwd=dest)
    _commit(dest, "feat: service scaffold")
    return dest
