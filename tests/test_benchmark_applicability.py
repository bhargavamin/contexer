"""Contract tests for the private applicability benchmark's clean-worktree inputs."""

import importlib.util
from pathlib import Path


_RUN_PATH = Path(__file__).parents[1] / "benchmarks" / "applicability" / "run.py"
_SPEC = importlib.util.spec_from_file_location("applicability_benchmark", _RUN_PATH)
assert _SPEC is not None and _SPEC.loader is not None
_RUN = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_RUN)


def test_absolute_private_input_stays_absolute(tmp_path):
    private_input = tmp_path / "ground_truth.json"
    assert _RUN._input_path(str(private_input)) == private_input


def test_relative_input_resolves_beside_benchmark():
    assert _RUN._input_path("ground_truth.json") == _RUN.HERE / "ground_truth.json"


def test_frozen_corpus_floor_uses_basename_for_external_input(tmp_path):
    assert _RUN._corpus_floor(tmp_path / "ground_truth.json") == 20
    assert _RUN._corpus_floor(tmp_path / "ground_truth_holdout.json") == 10
