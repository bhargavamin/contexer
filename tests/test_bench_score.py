"""Deterministic A/B scorers: convention violations and rationale accuracy."""
import subprocess

from benchmarks import score


class TestRationaleScore:
    def test_all_gold_present(self):
        assert score.rationale_score("We picked it for transactional integrity.",
                                     ["transactional integrity"]) == 1.0

    def test_case_insensitive_partial(self):
        assert score.rationale_score("Errors have Code And Message Keys.",
                                     ["code and message keys", "machine-parseable"]) == 0.5

    def test_empty_gold(self):
        assert score.rationale_score("anything", []) == 1.0


class TestCountViolations:
    BASELINE = [
        {"content": "Functions use snake_case naming (100% of 25 functions across 1 files)",
         "subtype": "convention", "tier": "high"},
        {"content": "Functions use type hints (100% of 25 functions)",
         "subtype": "convention", "tier": "high"},
    ]

    def test_conforming_code_zero(self):
        files = {"app/new.py": "def fetch_thing(x: int) -> dict:\n    return {}\n"}
        assert score.count_violations(files, self.BASELINE) == 0

    def test_camel_case_violates_naming(self):
        files = {"app/new.py": "def FetchThing(x: int) -> dict:\n    return {}\n"}
        assert score.count_violations(files, self.BASELINE) >= 1

    def test_missing_hints_violates_typing(self):
        files = {"app/new.py": "def fetch_thing(x):\n    return {}\n"}
        assert score.count_violations(files, self.BASELINE) >= 1

    def test_syntax_error_counts_once(self):
        assert score.count_violations({"app/bad.py": "def broken(:\n"}, self.BASELINE) == 1

    def test_non_python_ignored(self):
        assert score.count_violations({"README.md": "# hi"}, self.BASELINE) == 0


class TestChangedFiles:
    def test_detects_modified_and_new(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
        repo = tmp_path / "r"
        repo.mkdir()
        (repo / "a.py").write_text("x = 1\n")
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=T",
                        "-c", "commit.gpgsign=false", "commit", "-q", "-m", "c"],
                       cwd=repo, check=True)
        (repo / "a.py").write_text("x = 2\n")
        (repo / "b.py").write_text("y = 1\n")
        out = score.changed_files(str(repo))
        assert set(out) == {"a.py", "b.py"}
        assert out["a.py"] == "x = 2\n"


class TestGreptileFixes:
    BASELINE = TestCountViolations.BASELINE

    def test_test_files_not_scored_against_source_conventions(self):
        # Baseline conventions are mined from non-test source; pytest files with
        # plain unannotated test functions must not count as violations (P1).
        files = {"tests/test_new.py": "def test_thing():\n    assert True\n",
                 "app/util_test.py": "def test_helper():\n    assert True\n"}
        assert score.count_violations(files, self.BASELINE) == 0

    def test_private_double_underscore_names_are_scored(self):
        # Only true dunders (__init__) are exempt, matching the miner; __BadName
        # is part of the mined snake_case population and must count.
        ok = {"app/a.py": "def __init__(self) -> None:\n    pass\n"}
        bad = {"app/b.py": "def __BadName(x: int) -> int:\n    return x\n"}
        assert score.count_violations(ok, self.BASELINE) == 0
        assert score.count_violations(bad, self.BASELINE) >= 1

    def test_committed_changes_detected_with_base_ref(self, tmp_path, monkeypatch):
        monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
        monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
        repo = tmp_path / "r"
        repo.mkdir()
        (repo / "a.py").write_text("x = 1\n")
        env = ["git", "-c", "user.email=t@t", "-c", "user.name=T", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run([*env, "commit", "-q", "-m", "c1"], cwd=repo, check=True)
        base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                              capture_output=True, text=True).stdout.strip()
        (repo / "a.py").write_text("x = 2\n")
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
        subprocess.run([*env, "commit", "-q", "-m", "session committed"], cwd=repo, check=True)
        # a live-HEAD diff misses the committed edit; the base ref catches it
        assert "a.py" not in score.changed_files(str(repo))
        assert score.changed_files(str(repo), base)["a.py"] == "x = 2\n"
