"""The `slow` marker routing that the PR gate's `-m "not slow"` depends on.

Guards the actual risk: a harness file renamed out of the `test_bench_` prefix
silently rejoins the gate (+45s), or a non-harness file drifts into it and stops
being verified on pull requests.
"""
from pathlib import Path

from tests import conftest


class _Item:
    def __init__(self, name):
        self.path = Path("/tests") / name
        self.markers = []

    def add_marker(self, marker):
        self.markers.append(marker.name)


def test_only_bench_files_are_slow():
    names = [p.name for p in Path(__file__).parent.glob("test_*.py")]
    assert "test_bench_runner.py" in names  # the glob actually found the suite

    items = [_Item(n) for n in names]
    conftest.pytest_collection_modifyitems(items)

    slow = {i.path.name for i in items if i.markers == ["slow"]}
    assert slow == {n for n in names if n.startswith("test_bench_")}
    assert all(i.markers in ([], ["slow"]) for i in items)  # never double-marked
