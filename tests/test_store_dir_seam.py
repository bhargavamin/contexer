"""The store directory has one reader and one builder, and this is what holds it.

`contexer/sidecars.py` owns what a sidecar is CALLED and how long it lives. `store` owns
WHERE it lives: `store.store_dir()` reads the directory, `store.sidecar_path()` joins a
declared name onto it, and `store.ensure_store_dir()` creates it. Neither module knows the
other's half, which is what let sidecars stay a pure leaf with no package imports.

Kept out of test_sidecars.py deliberately: that file's subject is the DECLARATION, and one
file owning both grew a second reason to change.

None of this is tidiness. A module that builds the path by hand still works, so the erosion
is invisible - the same reason the sweep's hand-kept glob list drifted from the declaration
and hid four bugs. It is also not hypothetical: `share_policy.py` and `adapters/base.py`
landed on main written in the old style days after the convention did, and these checks are
what caught them.

The scans reuse `tests/test_module_boundaries.py`'s helpers rather than re-walking the tree.
That is load-bearing, not politeness: `_store_aliases` derives the local name bound to the
store module from the imports, and three modules spell it `_store`. A hand-written check
matching only `store.STORE_DIR` was blind to all of them, including `adapters/base.py`,
which held exactly that line.
"""
import ast
import os
import pathlib
import re

import pytest

from contexer import sidecars, store
from tests.conftest import redirect_store_dir
from tests.test_module_boundaries import _store_attr_reads

REPO = pathlib.Path(sidecars.__file__).parent.parent
SRC = REPO / "contexer"

# A join is legal only inside the builder itself. Three exceptions, declared rather than
# inferred, and all the same one: `contexer status` is a DIAGNOSTIC that derives its own
# `store_dir` from the home it was asked to inspect and must report on THAT directory, not on
# the process-wide one the seam returns. Routing them through `store.sidecar_path` would
# silently make status describe a different directory from the one it prints. They still take
# the NAME from the declaration, so the f-string and glob checks below still cover them.
JOIN_ALLOWED = {("store.py", "sidecar_path"),
                ("cli.py", "_read_team_creds"), ("cli.py", "_read_team_cache"),
                ("cli.py", "status")}


def _sources():
    """Every module that could reach the store: the package, the root shim, benchmarks.

    `benchmarks/` is included for the reason test_module_boundaries.py includes it: leaving it
    out is what let a dead `store._current_content` call survive.
    """
    paths = sorted(SRC.rglob("*.py")) + [REPO / "server.py"]
    bench = REPO / "benchmarks"
    paths += sorted(bench.rglob("*.py")) if bench.exists() else []
    for path in paths:
        if path.exists():
            yield path, ast.parse(path.read_text(encoding="utf-8"))


def _enclosing_function(tree, lineno):
    """The innermost function containing `lineno`, or "<module>" for a top-level statement.

    The first version of the join check walked `FunctionDef` bodies only, so a join written at
    module scope was invisible - which is precisely the shape `adapters/gemini.py` had before
    the conversion.
    """
    best = ("<module>", -1)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            end = getattr(node, "end_lineno", node.lineno)
            if node.lineno <= lineno <= end and node.lineno > best[1]:
                best = (node.name, node.lineno)
    return best[0]


class TestStoreDirectorySeam:
    def test_no_module_reads_the_store_directory_constant(self):
        """`store.STORE_DIR` is the VALUE and `store.store_dir()` is the SEAM. A module that
        reads the constant pins the resolution at its own call site, which is what made
        "change how the directory resolves" an edit in every consumer."""
        offenders = []
        for path, tree in _sources():
            if path.name == "store.py":
                continue
            for attr, lineno in _store_attr_reads(tree):
                if attr == "STORE_DIR":
                    offenders.append(f"{path.name}:{lineno}")
        assert offenders == [], sorted(offenders)

    def test_the_constant_is_read_in_exactly_one_place_inside_store(self):
        """Inside store.py the constant is a bare name, so the check above cannot see it.
        Exactly two references may exist: the assignment, and the seam's own return."""
        tree = ast.parse((SRC / "store.py").read_text(encoding="utf-8"))
        stores, loads = [], []
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and node.id == "STORE_DIR":
                (stores if isinstance(node.ctx, ast.Store) else loads).append(node.lineno)
        assert len(stores) == 1, stores
        assert len(loads) == 1, loads
        readers = [f.name for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)
                   and any(isinstance(n, ast.Name) and n.id == "STORE_DIR"
                           and isinstance(n.ctx, ast.Load) for n in ast.walk(f))]
        assert readers == ["store_dir"], readers

    def test_no_module_joins_a_sidecar_name_onto_a_directory_by_hand(self):
        """`dir / sidecars.filename(kind)` is the builder, and it lives in one function.

        Spelled out at every call site instead, the declaration owns the NAME while each
        caller separately owns the PLACE, which is one half of a fact in two files.
        """
        offenders = []
        for path, tree in _sources():
            for node in ast.walk(tree):
                if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)
                        and isinstance(node.right, ast.Call)
                        and isinstance(node.right.func, ast.Attribute)
                        and node.right.func.attr == "filename"):
                    continue
                where = _enclosing_function(tree, node.lineno)
                if (path.name, where) not in JOIN_ALLOWED:
                    offenders.append(f"{path.name}:{where}:{node.lineno}")
        assert offenders == [], sorted(offenders)

    def test_every_allowed_join_still_exists(self):
        """The allowlist may not outlive its entries: a stale exemption is a hole nobody knows
        is open. All four named functions must still be there to claim it."""
        for filename, func_name in JOIN_ALLOWED:
            tree = ast.parse((SRC / filename).read_text(encoding="utf-8"))
            names = {f.name for f in ast.walk(tree) if isinstance(f, ast.FunctionDef)}
            assert func_name in names, (filename, func_name)

    def test_no_module_writes_a_declared_template_as_an_f_string(self):
        """The join check sees `dir / sidecars.filename(kind)` and nothing else, so a name
        spelled straight into an f-string walked past it.

        That was not hypothetical. `cli._read_team_cache` held
        `f".team_{_store.repo_slug(repo_path)}.json"`, a second copy of the declared
        `team_cache` template, invisible to every other check here: the literal scan in
        test_sidecars.py only looks at templates with NO fields, and this one has a field.

        The comparison is on SHAPE, not text. An f-string's constant parts with `{}` per
        interpolation render exactly the template with `{}` per field, so `f".team_{x}.json"`
        and `.team_{slug}.json` collide whatever the expression inside is named.

        A template that STARTS with a field is excluded, the same carve-out `Kind.glob` makes
        for a leading dot. Those are the decision stores and their locks, whose shape reduces
        to `{}.json` - any f-string naming any JSON file matches, and `benchmarks/score.py`
        was reported on the first run. Their real builders are pinned by PRODUCERS.
        """
        shapes = {re.sub(r"\{[a-z_]+\}", "{}", k.template): k.name
                  for k in sidecars.KINDS
                  if "{" in k.template and not k.template.startswith("{")}
        offenders = []
        for path, tree in _sources():
            for node in ast.walk(tree):
                if not isinstance(node, ast.JoinedStr):
                    continue
                shape = "".join(part.value if isinstance(part, ast.Constant) else "{}"
                                for part in node.values)
                if shape in shapes:
                    offenders.append(f"{path.name}:{node.lineno} -> {shapes[shape]}")
        assert offenders == [], sorted(offenders)

    def test_no_module_writes_a_declared_glob_by_hand(self):
        """A glob is a name with a wildcard in it, and the declaration renders it too.

        `team_context._purge_all_caches` swept `.team_*.json` written as a literal, directly
        below a comment reading "Ask the declaration, not a literal." Renaming a kind would
        have left that sweep matching the old shape, silently, which is the exact failure that
        comment was written about.
        """
        globs = {k.glob: k.name for k in sidecars.KINDS if "*" in k.glob}
        offenders = []
        for path, tree in _sources():
            if path.name == "sidecars.py":
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                        and node.value in globs:
                    offenders.append(f"{path.name}:{node.lineno} -> {globs[node.value]}")
        assert offenders == [], sorted(offenders)

    def test_sidecar_path_follows_a_redirected_directory(self, tmp_path, monkeypatch):
        redirect_store_dir(monkeypatch, tmp_path / "elsewhere")
        assert store.sidecar_path("insight", slug="Users_me_proj") == \
            tmp_path / "elsewhere" / ".insight_Users_me_proj"
        assert store.store_dir() == tmp_path / "elsewhere"

    def test_substituting_the_seam_itself_redirects_every_builder(self, tmp_path, monkeypatch):
        """The swap the constant cannot offer: a test needing the directory to VARY during one
        call replaces the reader, not the value."""
        monkeypatch.setattr(store, "store_dir", lambda: tmp_path / "swapped")
        assert store.sidecar_path("outbox") == tmp_path / "swapped" / ".outbox.json"
        assert store.ensure_store_dir() == tmp_path / "swapped"

    def test_an_undeclared_kind_raises_rather_than_rendering_a_name(self):
        """Silence here would put a file nobody declared into the store directory, where
        `lifetime_for` calls it durable and the sweep never touches it again."""
        with pytest.raises(KeyError):
            store.sidecar_path("not_a_kind")
        with pytest.raises(KeyError):
            store.sidecar_path("insight")          # declared, but its field is missing

    def test_ensure_store_dir_creates_it_private_and_is_idempotent(self, tmp_path, monkeypatch):
        target = redirect_store_dir(monkeypatch, tmp_path / "fresh")
        assert store.ensure_store_dir() == target
        assert target.is_dir()
        assert os.stat(target).st_mode & 0o077 == 0, "group/other bits on the store directory"
        assert store.ensure_store_dir() == target   # exist_ok, not a second mkdir

    def test_ensure_store_dir_raises_rather_than_hiding_an_unwritable_home(self, tmp_path,
                                                                          monkeypatch):
        """It is a shared helper on hook paths that already have their own try. Swallowing
        here would tell every one of them the directory is fine when it is not."""
        blocker = tmp_path / "blocker"
        blocker.write_text("not a directory", encoding="utf-8")
        redirect_store_dir(monkeypatch, blocker / "under-a-file")
        with pytest.raises(OSError):
            store.ensure_store_dir()
