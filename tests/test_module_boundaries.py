"""Mechanical enforcement of the three module-boundary rules (CLAUDE.md, "Module boundaries").

These are not style checks. Each rule was written because the codebase had drifted in a way that
NOTHING failed on: a facade answered correctly while the seam eroded, a private name grew a second
reader and became an interface nobody had declared, and a leaf's regex was read through an alias
copied onto store. All three are invisible to a normal test run, which is why they are asserted
here as properties of the tree rather than left to review.

Deliberately a scan and not a docs statement: the four extractions before these rules were done to
one stated rule and landed at four different depths, so a rule that is only written down demonstrably
does not hold.
"""
import ast
import pathlib

from contexer import store

SRC = pathlib.Path(store.__file__).parent
STORE_PY = SRC / "store.py"
REPO = SRC.parent
# Every tree that calls into the store, not just the package. `benchmarks/` is here because
# leaving it out is what let a dead `store._current_content` call survive this whole file:
# it is a caller of the store like any other, and it is not under contexer/.
CALLER_ROOTS = ("contexer", "benchmarks")

# Leaf modules store.py may call. A leaf never imports store back except through the module
# object (see guard_engine.py's load-order docstring). Kept for the Rule 3 alias scan below,
# which needs the set of modules store.py is allowed to reach for.
#
# It is deliberately NOT the gate for the module-object rule any more. That check read
# `if path.stem not in LEAVES: continue`, so a module absent from this hand-kept list was skipped
# and passed unconditionally - and four were absent (`config`, `repo_key`, `share_status`,
# `sidecars`), two of them added by the very changes that wrote "a pure leaf" into CLAUDE.md.
# The rule applies to ANY module that reaches the store, so the check now finds its own subjects
# and there is no list left to drift. This is the same failure test_sidecars.py exists to
# prevent: "Four separate declaration bugs reached review before that test existed."
LEAVES = frozenset({
    "revisions", "reconciliation", "review", "retrieval", "redact", "miner",
    "conflicts", "guard_engine", "anchors", "console_api", "scope_audit", "memory_sync",
    "sidecars", "share_status",
})


def _py_files(roots=("contexer",)):
    """Every .py under the named repo-relative roots, plus server.py at the repo root.

    Keyed by the FULL path by every caller below, never by `path.name`: `contexer/server.py`
    and `contexer/ui/server.py` share a basename, so a basename key silently merges two
    readers into one and hides exactly the second-reader case Rule 2 exists to catch.
    """
    for root in roots:
        base = REPO / root
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            yield path, ast.parse(path.read_text(encoding="utf-8"))
    shim = REPO / "server.py"
    if shim.exists():
        yield shim, ast.parse(shim.read_text(encoding="utf-8"))


def _store_aliases(tree):
    """The local names actually bound to the store MODULE in this file.

    Derived from the imports rather than assumed, because assuming is wrong in both
    directions. `benchmarks/score.py` imports the module as `_store` and then uses a local
    variable literally named `store` to hold a Path, so a hardcoded {"store", "_store"} reads
    `store.read_text()` as a store attribute. Imports inside function bodies count: cli.py
    imports the store lazily per command.
    """
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "contexer":
            names |= {a.asname or a.name for a in node.names if a.name == "store"}
        if isinstance(node, ast.Import):
            names |= {a.asname or a.name.split(".")[-1] for a in node.names
                      if a.name in ("contexer.store",)}
    return names


def _store_attr_reads(tree):
    """Names read off the store module object, however that module is spelled locally."""
    aliases = _store_aliases(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name) \
                and node.value.id in aliases:
            yield node.attr, node.lineno
        if isinstance(node, ast.ImportFrom) and node.module == "contexer.store":
            for alias in node.names:
                yield alias.name, node.lineno


def _docstring_nodes(tree):
    """Every string Constant that is a module/class/function docstring, i.e. documentation."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) \
                    and isinstance(first.value.value, str):
                out.add(id(first.value))
    return out


_FMT_PLACEHOLDER = "_fmt_"


def _string_source(node, docstrings):
    """The full text of a string node, or None if it is not a candidate code string.

    Handles the f-string case, which is why the first version of this check missed the very
    bug it was written for. `benchmarks/memory_campaign.py` builds its probe as an implicit
    concatenation with one f-string in the middle, so Python parses it as a JoinedStr of
    fragments rather than one Constant: no single fragment both parses and holds a call, and
    a per-Constant scan therefore sees nothing. Interpolations become a bare name so the
    reassembled text still parses (`store.load(_fmt_)` rather than `store.load()`).
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return None if id(node) in docstrings else node.value
    if isinstance(node, ast.JoinedStr):
        parts = []
        for piece in node.values:
            if isinstance(piece, ast.Constant) and isinstance(piece.value, str):
                parts.append(piece.value)
            else:
                parts.append(_FMT_PLACEHOLDER)
        return "".join(parts)
    return None


def _store_reads_in_strings(tree):
    """`store.<name>` written inside a string literal that is EXECUTABLE CODE.

    Not a stylistic nicety: `benchmarks/memory_campaign.py` builds a probe as a source
    string and runs it in a child interpreter, so a rename reaches it only if someone looks
    inside literals. A dead call there raised AttributeError on every campaign run while
    every other check in this file passed.

    Detects code rather than words, because a plain substring match cannot tell a call from
    a sentence. Two filters, each earned by a false positive on the first attempt: docstrings
    are skipped outright (documentation legitimately discusses `store._read_global`), and the
    remainder must PARSE as Python and contain a Call. Parsing alone is not enough, since the
    filename `"store.py"` is itself a valid attribute expression and appears as a literal in
    this very file. Consequence, accepted and stated: a code string that only reads a constant
    (`store.MAX_SOURCE_FILES`) with no call in it is not seen.
    """
    docstrings = _docstring_nodes(tree)
    # A code string runs in a CHILD interpreter with its own imports, so the binding is
    # whatever that string itself imports - not this file's aliases.
    for node in ast.walk(tree):
        text = _string_source(node, docstrings)
        if text is None or "store." not in text:
            continue
        try:
            inner = ast.parse(text)
        except SyntaxError:
            continue
        if not any(isinstance(n, ast.Call) for n in ast.walk(inner)):
            continue
        inner_aliases = _store_aliases(inner)
        for n in ast.walk(inner):
            if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) \
                    and n.value.id in inner_aliases:
                yield n.attr, node.lineno


class TestRuleOneFacadeIsBackCompatOnly:
    """`store`'s lazy __getattr__ re-exports names that were public on store before an
    extraction moved them. It is a compatibility shim, not the surface production code uses."""

    def test_no_module_reaches_a_re_exported_name_through_store(self):
        exported = store._GUARD_EXPORTS | store._CONFLICT_EXPORTS | store._CONSOLE_EXPORTS
        offenders = []
        for path, tree in _py_files(CALLER_ROOTS):
            if path == STORE_PY:
                continue
            reads = list(_store_attr_reads(tree)) + list(_store_reads_in_strings(tree))
            for name, line in reads:
                if name in exported:
                    rel = path.relative_to(REPO)
                    offenders.append(f"{rel}:{line} store.{name} (import the owner)")
        assert offenders == [], offenders

    def test_every_re_exported_name_still_resolves(self):
        # The shim's whole purpose. If it stops resolving it is broken, not merely unused.
        for name in store._GUARD_EXPORTS | store._CONFLICT_EXPORTS | store._CONSOLE_EXPORTS:
            assert getattr(store, name) is not None, name


class TestRuleTwoTwoReadersMakeAnInterface:
    """One module reading a private store name is coupling. Two is an undeclared interface:
    it is depended upon from outside, so it must be public or belong to its own leaf."""

    def test_no_private_store_name_has_two_reader_modules(self):
        readers = {}
        for path, tree in _py_files(CALLER_ROOTS):
            if path == STORE_PY:
                continue
            rel = str(path.relative_to(REPO))
            reads = list(_store_attr_reads(tree)) + list(_store_reads_in_strings(tree))
            for name, _line in reads:
                if name.startswith("_"):
                    readers.setdefault(name, set()).add(rel)
        shared = {n: sorted(m) for n, m in readers.items() if len(m) > 1}
        assert shared == {}, shared

    def test_every_store_name_any_caller_reads_actually_exists(self):
        """Rule 2 promotes names, and a promotion is a RENAME: the old spelling stops
        resolving. Nothing else in the suite checks that every reader followed, and a reader
        inside a string literal or outside contexer/ is reached by neither the type checker
        nor an import. This is the check that would have caught the dead probe call."""
        missing = []
        for path, tree in _py_files(CALLER_ROOTS):
            if path == STORE_PY:
                continue
            reads = list(_store_attr_reads(tree)) + list(_store_reads_in_strings(tree))
            for name, line in reads:
                if not hasattr(store, name):
                    missing.append(f"{path.relative_to(REPO)}:{line} store.{name}")
        assert missing == [], missing


class TestRuleThreeNoLeafReExportsAnotherLeaf:
    """store.py used to carry 28 assignments of the form `_x = <leaf>.x`, so callers reached a
    leaf's functions and compiled regexes under a private store name. That is what made
    `guard_engine -> store alias -> retrieval private` a three-hop read of one pattern."""

    def test_store_does_not_alias_a_leaf_name(self):
        # Any contexer module store imports, not just LEAVES, and AnnAssign as well as
        # Assign: restricting either one lets the same shape back in under a new spelling.
        tree = ast.parse(STORE_PY.read_text(encoding="utf-8"))
        owners = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "contexer":
                owners |= {a.asname or a.name for a in node.names}
        offenders = []
        for node in tree.body:
            target = node.value if isinstance(node, (ast.Assign, ast.AnnAssign)) else None
            if isinstance(target, ast.Attribute) and isinstance(target.value, ast.Name) \
                    and target.value.id in (owners | LEAVES):
                offenders.append(f"store.py:{node.lineno} {ast.unparse(node)}")
        assert offenders == [], offenders

    def test_no_module_imports_store_with_from_imports(self):
        # Any module that reaches the store must import the MODULE OBJECT, so a value patched on
        # contexer.store is still seen at call time and store.py never needs it at import time.
        #
        # Scoped to every caller tree rather than to a hand-kept list of leaves. The list version
        # skipped any module not named in it, which made this check silently inert for four
        # modules - including two that CLAUDE.md calls leaves. A check whose subjects are declared
        # by hand fails open exactly when a new module arrives, which is when it is needed most.
        offenders = []
        for path, tree in _py_files(CALLER_ROOTS):
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module == "contexer.store":
                    names = ", ".join(a.name for a in node.names)
                    offenders.append(
                        f"{path}:{node.lineno} from contexer.store import {names}")
        assert offenders == [], offenders

    def test_every_package_module_is_covered_by_the_module_object_check(self):
        # The companion to the check above: prove its reach rather than assume it. If _py_files
        # ever stops finding a module, this fails instead of the check quietly passing.
        scanned = {path.stem for path, _ in _py_files(CALLER_ROOTS)}
        on_disk = {p.stem for p in (SRC).glob("*.py") if p.stem != "__init__"}
        missing = sorted(on_disk - scanned)
        assert missing == [], f"not scanned by the module-object check: {missing}"
