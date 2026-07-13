"""The synthetic fixture generator must produce a mineable, git-real repo."""
import subprocess

import pytest

from benchmarks.fixtures.generate import build_webapi
from contexer import miner


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    return build_webapi(tmp_path / "webapi", seed=7)


class TestBuildWebapi:
    def test_layout(self, repo):
        assert (repo / "pyproject.toml").is_file()
        assert (repo / "app").is_dir()
        assert len(list((repo / "tests").glob("test_*.py"))) >= 3

    def test_pyproject_is_uv_runnable(self, repo):
        # `uv run` refuses a [project] table without `version` — every check_cmd
        # would exit 2 regardless of the model's work (campaign-1 bug).
        import tomllib
        proj = tomllib.loads((repo / "pyproject.toml").read_text())["project"]
        assert "version" in proj
        assert any("pytest" in d for d in proj.get("dependencies", []))

    def test_git_history_is_conventional(self, repo):
        log = subprocess.run(["git", "-C", str(repo), "log", "--format=%s"],
                             capture_output=True, text=True).stdout.splitlines()
        assert len(log) >= 20
        assert sum(1 for s in log if s.startswith(("feat", "fix", "chore"))) / len(log) >= 0.9

    def test_miner_finds_high_tier_conventions(self, repo):
        high = [m for m in miner.mine_conventions(str(repo)) if m["tier"] == "high"]
        assert any("snake_case" in m["content"] for m in high)
        assert any("Line length" in m["content"] for m in high)

    def test_deterministic_for_seed(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
        a = build_webapi(tmp_path / "a", seed=3)
        b = build_webapi(tmp_path / "b", seed=3)
        assert (a / "app" / "svc_3_core.py").read_text() == (b / "app" / "svc_3_core.py").read_text()
