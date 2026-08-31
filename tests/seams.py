"""Test-side swap points: the one place the suite substitutes a store-owned seam.

Why this exists. `contexer/store.py` gained `store_dir()` so production reads the store
directory in exactly one place. The test suite had the mirror-image problem and kept it: 69
call sites across 34 files each wrote `monkeypatch.setattr(store, "STORE_DIR", ...)` out by
hand, and each independently re-derived the path (`tmp_path / ".contexer"` in 35 of them).
That count is not cosmetic - it is the number that has to change if the mechanism ever does,
and the architecture review named it as the evidence that no swap point existed.

Monkeypatch itself is not the smell. Its automatic restore is exactly why a test may write a
module attribute at all, and replacing it with an unrestored setter would leak state between
tests. What is collected here is the CHOICE of which attribute to write, so the suite states
it once.

The patcher is passed in rather than taken as a fixture, because the sites are not uniform:
most hold pytest's function-scoped `monkeypatch`, `tests/test_benchmark.py` holds a
module-scoped `monkeypatch_module`, and either may appear inside a `monkeypatch.context()`
block. A fixture bound to one of those would not restore at the right moment for the others.
"""
from pathlib import Path

from contexer import store


def redirect_store_dir(patcher, path) -> Path:
    """Point Contexer's store directory at `path` for `patcher`'s lifetime. Returns it.

    `patcher` is any object with pytest's `setattr(obj, name, value)` restore semantics:
    the `monkeypatch` fixture, a module-scoped one, or a `monkeypatch.context()` block.

    The CONSTANT is what gets written, not `store.store_dir` itself, because production
    resolves the directory through that function on every call - so writing the value
    redirects every builder, and a test that separately reads `store.STORE_DIR` still
    agrees with it. Substitute `store.store_dir` directly (not through this helper) only in
    the rarer case where the directory must VARY during a single call.
    """
    target = Path(path)
    patcher.setattr(store, "STORE_DIR", target)
    return target
