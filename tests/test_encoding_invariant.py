"""Repo-wide invariant: every text read/write in the package pins its encoding.

This exists because the rule was established, documented, and then broken by the
next feature that touched a file on disk. `contexer guard --install-hook` read the
developer's `.git/hooks/pre-commit` with the locale codec, so a hook echoing a
non-ASCII message crashed install AND uninstall under LC_ALL=C and made status
report "not installed" for a hook that was. Nothing caught it: the tests all run
under a UTF-8 locale, where the locale default and utf-8 are the same thing.

So the invariant is checked structurally instead. AST, not grep, because the
calls wrap across lines and a `read_text(\n    encoding="utf-8")` must pass.
"""
import ast
from pathlib import Path

PACKAGE = Path(__file__).resolve().parent.parent / "contexer"

# Binary APIs take no encoding, and tomllib/json binary reads are the correct way to
# parse those formats - only text-mode calls are in scope. read_bytes/write_bytes are
# therefore fine, and are in fact the right answer when byte-exactness matters (see
# cli._guard_read_hook, where text mode's newline translation was the second bug).
_TEXT_IO = {"read_text", "write_text"}


def _is_text_mode(node: ast.Call) -> bool:
    """True when an open()/fdopen() call opens in text mode.

    A non-literal mode (a variable) is treated as binary, i.e. skipped: guessing there
    would make this invariant produce false failures, and a checker that cries wolf gets
    deleted. Both the mode's positional slot and the keyword form are read."""
    mode = None
    if len(node.args) >= 2 and isinstance(node.args[1], ast.Constant):
        mode = node.args[1].value
    for kw in node.keywords:
        if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
            mode = kw.value.value
    if mode is None and any(kw.arg == "mode" for kw in node.keywords):
        return False                      # mode given but not a literal
    if mode is None and len(node.args) >= 2:
        return False                      # positional mode, not a literal
    return "b" not in (mode or "r")       # no mode at all == text read


def _unpinned_text_io(source: str, rel: str) -> list[str]:
    tree = ast.parse(source)
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _TEXT_IO:
            name = f"{func.attr}()"
        elif isinstance(func, ast.Name) and func.id == "open":
            # Builtin open only. `webbrowser.open` / `os.open` are attribute calls with
            # the same spelling and take no encoding, so matching attributes here would
            # be pure noise; Path().open() is missed as the price of that.
            if not _is_text_mode(node):
                continue
            name = "open()"
        elif isinstance(func, ast.Attribute) and func.attr == "fdopen":
            if not _is_text_mode(node):
                continue
            name = "os.fdopen()"
        else:
            continue
        if any(kw.arg == "encoding" for kw in node.keywords):
            continue
        bad.append(f"{rel}:{node.lineno}: {name} without encoding=")
    return bad


def test_package_text_io_always_pins_encoding():
    offenders = []
    for path in sorted(PACKAGE.rglob("*.py")):
        rel = path.relative_to(PACKAGE.parent).as_posix()
        offenders += _unpinned_text_io(path.read_text(encoding="utf-8"), rel)
    assert not offenders, (
        "Text IO must pass encoding=\"utf-8\" - the locale default is ASCII under "
        "LC_ALL=C and cp1252 on Windows, so these calls fail on a file that is "
        "perfectly valid UTF-8:\n  " + "\n  ".join(offenders))


def test_the_checker_itself_catches_an_unpinned_call():
    """Guards the guard: a checker that silently matches nothing would pass forever."""
    found = _unpinned_text_io("from pathlib import Path\nPath('x').read_text()\n", "sample.py")
    assert found == ["sample.py:2: read_text() without encoding="]
    assert _unpinned_text_io("Path('x').read_text(encoding='utf-8')\n", "sample.py") == []


def test_the_checker_covers_text_mode_open_and_skips_binary_and_lookalikes():
    """The read_text/write_text pair is not the whole surface: a plain
    `open(p).read()` on a developer-owned file decodes with the locale codec too."""
    caught = _unpinned_text_io(
        "open('x')\n"
        "open('x', 'w')\n"
        "open('x', mode='a')\n"
        "os.fdopen(fd, 'w')\n", "s.py")
    assert caught == [
        "s.py:1: open() without encoding=",
        "s.py:2: open() without encoding=",
        "s.py:3: open() without encoding=",
        "s.py:4: os.fdopen() without encoding=",
    ]
    # Binary modes, pinned calls, a non-literal mode, and the same-named functions that
    # take no encoding at all must every one stay silent.
    assert _unpinned_text_io(
        "open('x', 'rb')\n"
        "open('x', 'w', encoding='utf-8')\n"
        "os.fdopen(fd, 'wb')\n"
        "open('x', mode)\n"
        "webbrowser.open(url)\n"
        "os.open(p, os.O_CREAT)\n"
        "urllib.request.urlopen(req, timeout=2)\n", "s.py") == []
