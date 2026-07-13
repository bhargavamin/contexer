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
