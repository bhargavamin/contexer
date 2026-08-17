"""The marker routing the PR gate's `-m "not slow and not perf"` depends on.

Guards the actual risks: a harness file renamed out of the `test_bench_` prefix
silently rejoins the gate (+45s), or a non-harness file drifts into it and stops
being verified on pull requests; and a `perf` test running under coverage, where
its wall-clock number is ~5x inflated and the assertion becomes a coin flip.
"""
import types
from pathlib import Path

from tests import conftest


class _Item:
    def __init__(self, name, markers=()):
        self.path = Path("/tests") / name
        self.markers = list(markers)

    def add_marker(self, marker):
        self.markers.append(marker.name)

    def get_closest_marker(self, name):
        return name if name in self.markers else None


def _config(*, covering):
    """The two attributes the hook reads off pytest-cov's registered options."""
    return types.SimpleNamespace(
        option=types.SimpleNamespace(
            cov_source=["contexer"] if covering else None, no_cov=not covering))


def test_only_bench_files_are_slow():
    names = [p.name for p in Path(__file__).parent.glob("test_*.py")]
    assert "test_bench_runner.py" in names  # the glob actually found the suite

    items = [_Item(n) for n in names]
    conftest.pytest_collection_modifyitems(_config(covering=False), items)

    slow = {i.path.name for i in items if i.markers == ["slow"]}
    assert slow == {n for n in names if n.startswith("test_bench_")}
    assert all(i.markers in ([], ["slow"]) for i in items)  # never double-marked


def test_perf_tests_are_skipped_under_coverage():
    item = _Item("test_store.py", markers=["perf"])
    conftest.pytest_collection_modifyitems(_config(covering=True), [item])
    assert "skip" in item.markers


def test_perf_tests_run_when_coverage_is_off():
    """--no-cov is how the real numbers get measured; the skip must not apply there."""
    item = _Item("test_store.py", markers=["perf"])
    conftest.pytest_collection_modifyitems(_config(covering=False), [item])
    assert item.markers == ["perf"]


def test_coverage_does_not_skip_non_perf_tests():
    item = _Item("test_store.py")
    conftest.pytest_collection_modifyitems(_config(covering=True), [item])
    assert item.markers == []
