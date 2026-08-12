"""Tests for contexer/miner.py — deterministic convention mining."""
import ast
import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path

import pytest

from contexer import miner


# ── helpers ───────────────────────────────────────────────────────────────────

def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _contents(items: list[dict]) -> list[str]:
    return [i["content"] for i in items]


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    """Real git repo with global/system git config isolated; returns its path."""
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    repo = tmp_path / "gitrepo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


def _commit(repo: Path, message: str) -> None:
    subprocess.run(
        ["git", "-c", "user.email=t@test.local", "-c", "user.name=T",
         "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-q", "-m", message],
        cwd=repo, check=True)


# ── config-file conventions ───────────────────────────────────────────────────

class TestConfigConventions:
    def test_ruff_line_length_and_quote_style(self, tmp_path):
        _write(tmp_path / "pyproject.toml", textwrap.dedent("""\
            [tool.ruff]
            line-length = 100

            [tool.ruff.format]
            quote-style = "single"
        """))
        items = miner._config_conventions(tmp_path)
        contents = _contents(items)
        assert any("Line length 100" in c for c in contents)
        assert any("single" in c for c in contents)
        assert all(i["tier"] == "high" for i in items)

    def test_ruff_lint_select_detected(self, tmp_path):
        # Without this the repo's own blocking `ruff check` gate mined nothing at all:
        # _emit_ruff read only line-length and quote-style, so a session was never told
        # linting was enforced.
        _write(tmp_path / "pyproject.toml", textwrap.dedent("""\
            [tool.ruff.lint]
            select = ["E4", "E7", "E9", "F"]
        """))
        items = miner._config_conventions(tmp_path)
        hit = next(c for c in _contents(items) if "Ruff lint enforced" in c)
        assert "E4, E7, E9, F" in hit
        assert all(i["tier"] == "high" for i in items)

    def test_ruff_legacy_top_level_select_detected(self, tmp_path):
        # ruff moved `select` under [lint] in 0.2; configs on the old spelling are still
        # declaring the same rule, so both are read.
        _write(tmp_path / "pyproject.toml", '[tool.ruff]\nselect = ["E", "F"]\n')
        assert any("Ruff lint enforced" in c
                   for c in _contents(miner._config_conventions(tmp_path)))

    def test_ruff_lint_select_truncated_when_long(self, tmp_path):
        rules = [f"R{i}" for i in range(12)]
        _write(tmp_path / "pyproject.toml",
               f"[tool.ruff.lint]\nselect = {rules!r}\n".replace("'", '"'))
        hit = next(c for c in _contents(miner._config_conventions(tmp_path))
                   if "Ruff lint enforced" in c)
        assert "R7" in hit and "R8" not in hit and "…" in hit

    def test_empty_modern_select_beats_a_legacy_top_level_one(self, tmp_path):
        """`lint.select` wins in ruff even when EMPTY — verified with ruff 0.15.4: this
        exact config reports "All checks passed!" on a file with two violations, while
        the legacy key alone finds both. Reading the modern key by presence rather than
        truthiness is what keeps us from advertising rules nothing enforces."""
        _write(tmp_path / "pyproject.toml", textwrap.dedent("""\
            [tool.ruff]
            select = ["E", "F"]

            [tool.ruff.lint]
            select = []
        """))
        assert not any("Ruff lint enforced" in c
                       for c in _contents(miner._config_conventions(tmp_path)))

    def test_non_empty_modern_select_wins_over_legacy(self, tmp_path):
        _write(tmp_path / "pyproject.toml", textwrap.dedent("""\
            [tool.ruff]
            select = ["ALL"]

            [tool.ruff.lint]
            select = ["E4"]
        """))
        hit = next(c for c in _contents(miner._config_conventions(tmp_path))
                   if "Ruff lint enforced" in c)
        assert "E4" in hit and "ALL" not in hit

    def test_ruff_without_select_emits_no_lint_convention(self, tmp_path):
        # Silence over noise: a [tool.ruff] table that selects nothing declares nothing.
        _write(tmp_path / "pyproject.toml", "[tool.ruff]\ntarget-version = \"py312\"\n")
        assert not any("Ruff lint enforced" in c
                       for c in _contents(miner._config_conventions(tmp_path)))

    def test_mypy_strict_detected(self, tmp_path):
        _write(tmp_path / "pyproject.toml", "[tool.mypy]\nstrict = true\n")
        items = miner._config_conventions(tmp_path)
        assert any("Mypy strict mode" in c for c in _contents(items))

    def test_pytest_cov_fail_under_detected(self, tmp_path):
        _write(tmp_path / "pyproject.toml", textwrap.dedent("""\
            [tool.pytest.ini_options]
            addopts = "--cov=contexer --cov-fail-under=85"
        """))
        items = miner._config_conventions(tmp_path)
        assert any("stay ≥85%" in c for c in _contents(items))

    def test_editorconfig_indent_detected(self, tmp_path):
        _write(tmp_path / ".editorconfig", textwrap.dedent("""\
            root = true

            [*]
            indent_style = space
            indent_size = 4
            max_line_length = 100
        """))
        items = miner._config_conventions(tmp_path)
        contents = _contents(items)
        assert any("space" in c and "size 4" in c for c in contents)
        assert any("Max line length 100" in c for c in contents)

    def test_precommit_ids_extracted_no_yaml_parsing(self, tmp_path):
        _write(tmp_path / ".pre-commit-config.yaml", textwrap.dedent("""\
            repos:
              - repo: https://github.com/psf/black
                hooks:
                  - id: black
              - repo: https://github.com/pycqa/flake8
                hooks:
                  - id: flake8
                  - id: mypy
        """))
        items = miner._config_conventions(tmp_path)
        matches = [c for c in _contents(items) if "Pre-commit hooks run" in c]
        assert len(matches) == 1
        assert "black" in matches[0]
        assert "flake8" in matches[0]
        assert "mypy" in matches[0]

    def test_prettier_json_parsed(self, tmp_path):
        _write(tmp_path / ".prettierrc.json",
               json.dumps({"semi": False, "singleQuote": True, "printWidth": 100}))
        items = miner._config_conventions(tmp_path)
        matches = [c for c in _contents(items) if "Prettier config" in c]
        assert len(matches) == 1
        assert "single quotes" in matches[0]
        assert "print width 100" in matches[0]

    def test_eslint_extends(self, tmp_path):
        _write(tmp_path / ".eslintrc.json", json.dumps({"extends": ["airbnb", "prettier"]}))
        items = miner._config_conventions(tmp_path)
        matches = [c for c in _contents(items) if "ESLint extends" in c]
        assert len(matches) == 1
        assert "airbnb" in matches[0] and "prettier" in matches[0]

    def test_tsconfig_strict_with_jsonc_comments(self, tmp_path):
        _write(tmp_path / "tsconfig.json", textwrap.dedent("""\
            {
              // line comment about strictness
              "compilerOptions": {
                /* block
                   comment */
                "strict": true
              }
            }
        """))
        items = miner._config_conventions(tmp_path)
        assert any("TypeScript strict mode" in c for c in _contents(items))

    def test_ci_run_lines_extracted_deduped_capped(self, tmp_path):
        wf_dir = tmp_path / ".github" / "workflows"
        _write(wf_dir / "a.yml", textwrap.dedent("""\
            jobs:
              build:
                steps:
                  - run: echo one
                  - run: |
                      echo should-not-appear
                  - run: echo two
                  - run: echo three
        """))
        _write(wf_dir / "b.yml", textwrap.dedent("""\
            jobs:
              build:
                steps:
                  - run: echo one
                  - run: echo four
                  - run: echo five
                  - run: echo six
        """))
        items = miner._config_conventions(tmp_path)
        matches = [i for i in items if i["content"].startswith("CI runs:")]
        assert len(matches) == 1
        content = matches[0]["content"]
        assert matches[0]["subtype"] == "pattern"
        assert "should-not-appear" not in content
        assert content.count("echo") <= 5
        assert "from a.yml" in content

    def test_absent_configs_produce_no_items(self, tmp_path):
        assert miner._config_conventions(tmp_path) == []

    def test_malformed_toml_no_raise_other_detectors_still_emit(self, tmp_path):
        _write(tmp_path / "pyproject.toml", "this is not [ valid toml =")
        _write(tmp_path / ".prettierrc.json", json.dumps({"semi": True}))
        items = miner._config_conventions(tmp_path)
        assert any("Prettier config" in c for c in _contents(items))

    def test_malformed_json_no_raise_other_detectors_still_emit(self, tmp_path):
        _write(tmp_path / ".eslintrc.json", "{not valid json")
        _write(tmp_path / "pyproject.toml", "[tool.mypy]\nstrict = true\n")
        items = miner._config_conventions(tmp_path)
        assert any("Mypy strict mode" in c for c in _contents(items))

    def test_own_repo_coverage_floor_stays_minable(self):
        # Self-referential pin: the ≥85% floor must live in
        # [tool.pytest.ini_options].addopts because that key is the ONLY
        # place _config_conventions can mine it from. Moving the flag to the
        # CI command line keeps enforcement but silently deletes the repo's
        # own mined "coverage ≥85%" convention — which is exactly what a
        # hygiene PR did once. If this test fails, put the flag back in
        # addopts and use --no-cov for subset runs instead.
        repo_root = Path(__file__).resolve().parents[1]
        items = miner._config_conventions(repo_root)
        assert any(
            "--cov-fail-under" in i["content"] and "85" in i["content"]
            for i in items
        ), (
            "Coverage floor not minable from pyproject addopts, or its 85% "
            "threshold changed without updating this pin — see the comment "
            "above addopts in pyproject.toml"
        )


# ── python source stats ───────────────────────────────────────────────────────

def _funcs(n_snake: int, n_bad: int = 0, prefix: str = "f") -> str:
    lines = [f"def {prefix}_snake_{i}():\n    pass\n" for i in range(n_snake)]
    lines += [f"def {prefix}Bad{i}():\n    pass\n" for i in range(n_bad)]
    return "\n".join(lines)


class TestPythonSourceStats:
    def test_high_tier_snake_case_naming(self, tmp_path):
        _write(tmp_path / "mod.py", _funcs(n_snake=19, n_bad=1))
        items = miner._python_source_stats(tmp_path)
        matches = [i for i in items if "snake_case" in i["content"]]
        assert len(matches) == 1
        assert matches[0]["tier"] == "high"
        assert matches[0]["subtype"] == "convention"

    def test_medium_tier_mixed_naming(self, tmp_path):
        _write(tmp_path / "mod.py", _funcs(n_snake=14, n_bad=6))
        items = miner._python_source_stats(tmp_path)
        matches = [i for i in items if "snake_case" in i["content"]]
        assert len(matches) == 1
        assert matches[0]["tier"] == "medium"

    def test_below_60_percent_not_emitted(self, tmp_path):
        _write(tmp_path / "mod.py", _funcs(n_snake=5, n_bad=15))
        items = miner._python_source_stats(tmp_path)
        assert not any("snake_case" in i["content"] for i in items)

    def test_below_sample_floor_not_emitted(self, tmp_path):
        _write(tmp_path / "mod.py", _funcs(n_snake=5, n_bad=0))
        items = miner._python_source_stats(tmp_path)
        assert not any("snake_case" in i["content"] for i in items)

    def test_syntax_error_file_skipped_without_killing_pass(self, tmp_path):
        _write(tmp_path / "good.py", _funcs(n_snake=20))
        _write(tmp_path / "broken.py", "def bad(:\n    pass\n")
        items = miner._python_source_stats(tmp_path)
        matches = [i for i in items if "snake_case" in i["content"]]
        assert len(matches) == 1
        assert matches[0]["tier"] == "high"

    def test_non_utf8_bytes_file_ignored(self, tmp_path):
        _write(tmp_path / "good.py", _funcs(n_snake=20))
        (tmp_path / "binary.py").write_bytes(b"\xff\xfe\x00\x01garbage")
        items = miner._python_source_stats(tmp_path)  # must not raise
        matches = [i for i in items if "snake_case" in i["content"]]
        assert len(matches) == 1

    def test_oversized_file_skipped(self, tmp_path):
        _write(tmp_path / "good.py", _funcs(n_snake=20))
        padding = "# padding\n" * 30000  # comfortably over the 200KB cap
        _write(tmp_path / "huge.py", padding + _funcs(n_snake=0, n_bad=50, prefix="huge"))
        items = miner._python_source_stats(tmp_path)
        matches = [i for i in items if "snake_case" in i["content"]]
        assert len(matches) == 1
        assert matches[0]["tier"] == "high"  # huge.py's bad names never counted

    def test_absolute_import_dominance_emitted(self, tmp_path):
        lines = "\n".join(f"from mod{i} import thing{i}" for i in range(25))
        _write(tmp_path / "mod.py", lines)
        items = miner._python_source_stats(tmp_path)
        matches = [i for i in items if "Imports are absolute" in i["content"]]
        assert len(matches) == 1

    def test_docstring_coverage_emitted(self, tmp_path):
        lines = []
        for i in range(19):
            lines.append(f'def pub_fn_{i}():\n    """Doc."""\n    pass\n')
        lines.append("def pub_fn_19():\n    pass\n")  # 19/20 documented = 95%
        _write(tmp_path / "mod.py", "\n".join(lines))
        items = miner._python_source_stats(tmp_path)
        matches = [i for i in items if "docstrings" in i["content"]]
        assert len(matches) == 1
        assert matches[0]["tier"] == "high"

    def test_custom_exception_classes_emitted(self, tmp_path):
        _write(tmp_path / "errors.py", textwrap.dedent("""\
            class FooError(Exception):
                pass

            class BarError(ValueError):
                pass

            class BazException(Exception):
                pass
        """))
        items = miner._python_source_stats(tmp_path)
        matches = [i for i in items if "custom exception classes" in i["content"]]
        assert len(matches) == 1
        assert matches[0]["subtype"] == "pattern"
        assert matches[0]["tier"] == "high"

    def test_zero_bare_except_emitted_over_20_files(self, tmp_path):
        for i in range(20):
            _write(tmp_path / f"m{i}.py", "def f():\n    pass\n")
        items = miner._python_source_stats(tmp_path)
        matches = [i for i in items if "No bare except" in i["content"]]
        assert len(matches) == 1
        assert matches[0]["subtype"] == "pattern"

    def test_bare_except_present_suppresses_negative_convention(self, tmp_path):
        for i in range(19):
            _write(tmp_path / f"m{i}.py", "def f():\n    pass\n")
        _write(tmp_path / "m19.py", "try:\n    pass\nexcept:\n    pass\n")
        items = miner._python_source_stats(tmp_path)
        assert not any("No bare except" in i["content"] for i in items)


# ── test-file conventions ─────────────────────────────────────────────────────

class TestTestConventions:
    def test_layout_and_naming_detected(self, tmp_path):
        for i in range(3):
            _write(tmp_path / "tests" / f"test_m{i}.py", "def test_x():\n    assert True\n")
        items = miner._test_conventions(tmp_path)
        contents = _contents(items)
        assert any("Tests live in tests/" in c for c in contents)
        assert any("test_*.py naming" in c for c in contents)
        assert all(i["tier"] == "high" for i in items if "Tests" in i["content"] or "test_*.py" in i["content"])

    def test_plain_assert_dominance(self, tmp_path):
        for f in range(3):
            lines = "\n".join(f"def test_fn_{f}_{i}():\n    assert True\n" for i in range(7))
            _write(tmp_path / "tests" / f"test_m{f}.py", lines)
        items = miner._test_conventions(tmp_path)
        matches = [i for i in items if "plain pytest asserts" in i["content"]]
        assert len(matches) == 1

    def test_unittest_testcase_dominance(self, tmp_path):
        for f in range(3):
            methods = "\n".join(
                f"    def test_fn_{f}_{i}(self):\n        self.assertTrue(True)\n" for i in range(7))
            content = "import unittest\n\nclass MyTest(unittest.TestCase):\n" + methods
            _write(tmp_path / "tests" / f"test_m{f}.py", content)
        items = miner._test_conventions(tmp_path)
        matches = [i for i in items if "unittest.TestCase" in i["content"]]
        assert len(matches) == 1

    def test_fixtures_detected(self, tmp_path):
        content = textwrap.dedent("""\
            import pytest

            @pytest.fixture
            def fixture_one():
                return 1

            @pytest.fixture()
            def fixture_two():
                return 2

            @pytest.fixture(scope="module")
            def fixture_three():
                return 3

            def test_uses_fixtures(fixture_one):
                assert fixture_one == 1
        """)
        _write(tmp_path / "tests" / "test_a.py", content)
        _write(tmp_path / "tests" / "test_b.py", "def test_x():\n    assert True\n")
        _write(tmp_path / "tests" / "test_c.py", "def test_y():\n    assert True\n")
        items = miner._test_conventions(tmp_path)
        matches = [i for i in items if "fixtures" in i["content"] and "Pytest" in i["content"]]
        assert len(matches) == 1
        assert "3 fixtures" in matches[0]["content"]
        assert matches[0]["subtype"] == "pattern"

    def test_suffix_test_naming_detected(self, tmp_path):
        for i in range(4):
            _write(tmp_path / "tests" / f"m{i}_test.py", "def test_x():\n    assert True\n")
        contents = _contents(miner._test_conventions(tmp_path))
        assert any("*_test.py naming" in c for c in contents)
        assert not any("test_*.py naming" in c for c in contents)

    def test_fewer_than_three_test_files_yields_nothing(self, tmp_path):
        _write(tmp_path / "tests" / "test_a.py", "def test_x():\n    assert True\n")
        assert miner._test_conventions(tmp_path) == []


# ── commit message convention ─────────────────────────────────────────────────

class TestCommitConvention:
    def test_high_tier_conventional_commits(self, git_repo):
        for i in range(20):
            _commit(git_repo, f"feat: change number {i}")
        items = miner._commit_convention(str(git_repo))
        assert len(items) == 1
        assert items[0]["tier"] == "high"
        assert items[0]["subtype"] == "convention"

    def test_medium_tier_mixed_commits(self, git_repo):
        for i in range(14):
            _commit(git_repo, f"fix: bug number {i}")
        for i in range(6):
            _commit(git_repo, f"random commit message {i}")
        items = miner._commit_convention(str(git_repo))
        assert len(items) == 1
        assert items[0]["tier"] == "medium"

    def test_no_git_dir_returns_empty(self, tmp_path):
        assert miner._commit_convention(str(tmp_path)) == []

    def test_monkeypatched_git_none_returns_empty(self, monkeypatch, tmp_path):
        monkeypatch.setattr(miner, "_git", lambda repo, *a: None)
        assert miner._commit_convention(str(tmp_path)) == []


# ── caps / safety ──────────────────────────────────────────────────────────────

class TestMinerCaps:
    def test_never_raises_on_weird_tree(self, tmp_path):
        _write(tmp_path / "mod.py", "def snake_fn():\n    pass\n")
        broken_link = tmp_path / "broken_link.py"
        try:
            os.symlink(str(tmp_path / "does_not_exist.py"), broken_link)
        except OSError:
            pass
        locked_dir = tmp_path / "locked"
        locked_dir.mkdir()
        (locked_dir / "inner.py").write_text("def x():\n    pass\n")
        os.chmod(locked_dir, 0)
        try:
            result = miner.mine_conventions(str(tmp_path))
            assert isinstance(result, list)
        finally:
            os.chmod(locked_dir, stat.S_IRWXU)

    def test_respects_file_cap(self, tmp_path, monkeypatch):
        for i in range(10):
            _write(tmp_path / f"m{i}.py", "def snake_fn():\n    pass\n")
        monkeypatch.setattr(miner, "_MAX_PY_FILES", 3)
        calls = {"n": 0}
        real_parse = ast.parse

        def counting_parse(*a, **kw):
            calls["n"] += 1
            return real_parse(*a, **kw)

        monkeypatch.setattr(miner.ast, "parse", counting_parse)
        miner._python_source_stats(tmp_path)
        assert calls["n"] <= 3

    def test_walk_skips_node_modules_and_venv(self, tmp_path):
        _write(tmp_path / "node_modules" / "skip.py", "def x():\n    pass\n")
        _write(tmp_path / ".venv" / "skip2.py", "def y():\n    pass\n")
        _write(tmp_path / "real.py", "def z():\n    pass\n")
        found = list(miner._walk_files(tmp_path, ".py"))
        names = {p.name for p in found}
        assert "skip.py" not in names
        assert "skip2.py" not in names
        assert "real.py" in names

    def test_walk_skips_dot_dirs(self, tmp_path):
        # Tool-state dirs (.claude worktrees, .idea, …) hold full repo copies that
        # would poison the statistics.
        _write(tmp_path / ".claude" / "worktrees" / "agent-x" / "copy.py", "def a():\n    pass\n")
        _write(tmp_path / ".idea" / "gen.py", "def b():\n    pass\n")
        _write(tmp_path / "real.py", "def z():\n    pass\n")
        names = {p.name for p in miner._walk_files(tmp_path, ".py")}
        assert names == {"real.py"}

    def test_no_item_ever_has_constraint_subtype_and_shape_is_valid(self, tmp_path):
        _write(tmp_path / "pyproject.toml", "[tool.ruff]\nline-length = 88\n")
        for i in range(25):
            _write(tmp_path / f"m{i}.py", f'def snake_fn_{i}():\n    """doc"""\n    pass\n')
        for i in range(5):
            _write(tmp_path / "tests" / f"test_m{i}.py", "def test_x():\n    assert True\n")

        items = miner.mine_conventions(str(tmp_path))
        assert items
        for item in items:
            assert item["subtype"] in ("convention", "pattern")
            assert item["subtype"] != "constraint"
            assert item["tier"] in ("high", "medium")
            assert item["content"]

    def test_empty_dir_returns_empty_list_no_raise(self, tmp_path):
        assert miner.mine_conventions(str(tmp_path)) == []

    def test_nonexistent_path_returns_empty_list_no_raise(self, tmp_path):
        assert miner.mine_conventions(str(tmp_path / "does" / "not" / "exist")) == []
