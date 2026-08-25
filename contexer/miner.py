"""Deterministic convention mining over a repo's source and config.

Measures conventions with whole-repo evidence — every emitted sentence carries
its own proof (a percentage + sample count, or the config file it came from).
No model in the loop: everything here is ast/regex/config parsing, never an
LLM guess. This module is a leaf: it is imported by store.py and must NEVER
import contexer.store, so it carries its own tiny `_git` helper (a twin of
store.run_git at line ~1520) rather than share store.py's.

Mining is an enhancement, not a gate: `mine_conventions` never raises — any
unexpected failure returns whatever was gathered before the failure (or [])."""
import ast
import configparser
import json
import os
import re
import subprocess
import tomllib
from pathlib import Path

# ── caps (safety against pathological trees, not tuning knobs) ──────────────
_MAX_PY_FILES = 400
_MAX_FILE_BYTES = 200_000
_MAX_DIRS = 2000
_SKIP_DIRS = frozenset({
    ".git", "node_modules", ".venv", "venv", "dist", "build",
    "__pycache__", "target", ".next", ".tox",
})

_MIN_SAMPLES = 20
_HIGH = 0.90
_MEDIUM = 0.60

_SNAKE = re.compile(r"^_?[a-z][a-z0-9_]*$")
_PASCAL = re.compile(r"^_?[A-Z][a-zA-Z0-9]*$")
_UPPER = re.compile(r"^_?[A-Z][A-Z0-9_]*$")
_DUNDER = re.compile(r"^__.*__$")
_EXC_BASE = re.compile(r"\w*(Error|Exception)$")
_CONVENTIONAL_COMMIT = re.compile(
    r"^(feat|fix|chore|docs|refactor|test|ci|build|perf|style)(\(.+\))?!?:", re.I)
_RUN_LINE = re.compile(r"^\s*(?:-\s+)?run:\s*(.+)$")
_PRECOMMIT_ID = re.compile(r"^\s*-\s*id:\s*(\S+)", re.M)


def _git(repo_path: str, *args: str) -> str | None:
    """Local twin of store.run_git - kept local so miner.py never imports
    contexer.store (mining must stay a leaf dependency)."""
    try:
        out = subprocess.run(
            ["git", "-C", repo_path, *args],
            capture_output=True, text=True, timeout=5,
        )
    except Exception:
        return None
    return out.stdout.strip() if out.returncode == 0 else None


def _tier(ratio: float) -> str | None:
    if ratio >= _HIGH:
        return "high"
    if ratio >= _MEDIUM:
        return "medium"
    return None


def _pct_tier(matched: int, total: int) -> tuple[int, str] | None:
    """Rounded percent + tier for a percentage-based stat, or None if below
    the sample floor or below the medium threshold."""
    if total < _MIN_SAMPLES:
        return None
    tier = _tier(matched / total)
    if tier is None:
        return None
    return round(matched / total * 100), tier


def _walk_files(root: Path, suffix: str):
    """Bounded, skip-dir-aware file walk. Caps directories visited and never
    descends into _SKIP_DIRS. Errors reading a dir (permissions, etc.) are
    swallowed by os.walk itself (onerror=None)."""
    dirs_seen = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirs_seen += 1
        if dirs_seen > _MAX_DIRS:
            break
        # Dot-dirs (.claude worktrees, .idea, .mypy_cache, …) are tool state, not
        # project source — mining them poisons the statistics with copies.
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS and not d.startswith(".")]
        for name in filenames:
            if name.endswith(suffix):
                yield Path(dirpath) / name


def _is_test_path(path: Path) -> bool:
    name = path.name
    if name.startswith("test_") or name.endswith("_test.py"):
        return True
    return "tests" in path.parts[:-1]


# ── 1. config-file conventions (all tier="high" — a config file is 100% evidence) ──

def _emit_ruff(cfg: dict, source: str) -> list[dict]:
    items: list[dict] = []
    line_length = cfg.get("line-length")
    if isinstance(line_length, int):
        items.append({"content": f"Line length {line_length} enforced by ruff ({source})",
                      "subtype": "convention", "tier": "high"})
    quote = cfg.get("format", {}).get("quote-style") if isinstance(cfg.get("format"), dict) else None
    if quote:
        items.append({"content": f"Quote style '{quote}' enforced by ruff ({source})",
                      "subtype": "convention", "tier": "high"})
    # Selected rule set. Read from both spellings: ruff moved `select` under [lint] in
    # 0.2, and plenty of configs still carry the top-level key. Without this a repo whose
    # CI blocks on `ruff check` mined nothing about linting at all — the gate existed and
    # no session was ever told, which is the one failure this module is here to prevent.
    #
    # PRESENCE, not truthiness: when both spellings are set, `lint.select` wins in ruff
    # even when it is EMPTY. Verified with ruff 0.15.4 — top-level select = ["E","F"] plus
    # lint.select = [] reports "All checks passed!" on a file with two violations, while
    # the legacy key alone finds both. A `lint.get("select") or cfg.get("select")` would
    # therefore mine "rules selected: E, F" for a repo whose linter enforces nothing:
    # exactly the unverifiable claim this module exists not to make.
    lint = cfg.get("lint") if isinstance(cfg.get("lint"), dict) else {}
    select = lint["select"] if "select" in lint else cfg.get("select")
    if isinstance(select, list):
        rules = [str(r) for r in select if isinstance(r, str)]
        if rules:
            # Truncated: a repo selecting 40 rule groups would otherwise bury the fact
            # that linting is enforced under the list itself.
            shown = ", ".join(rules[:8]) + (", …" if len(rules) > 8 else "")
            items.append({"content": f"Ruff lint enforced, rules selected: {shown} ({source})",
                          "subtype": "convention", "tier": "high"})
    return items


def _config_conventions(root: Path) -> list[dict]:
    items: list[dict] = []

    pyproject = root / "pyproject.toml"
    if pyproject.is_file():
        try:
            data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
            tool = data.get("tool", {})
            items += _emit_ruff(tool.get("ruff", {}), "pyproject.toml")
            if tool.get("mypy", {}).get("strict") is True:
                items.append({"content": "Mypy strict mode required (pyproject.toml)",
                              "subtype": "convention", "tier": "high"})
            addopts = tool.get("pytest", {}).get("ini_options", {}).get("addopts", "")
            if isinstance(addopts, str):
                m = re.search(r"--cov-fail-under=(\d+)", addopts)
                if m:
                    items.append({"content": f"Test coverage must stay ≥{m.group(1)}% "
                                             "(pytest --cov-fail-under)",
                                  "subtype": "convention", "tier": "high"})
        except Exception:
            pass

    for fname in (".ruff.toml", "ruff.toml"):
        p = root / fname
        if p.is_file():
            try:
                items += _emit_ruff(tomllib.loads(p.read_text(encoding="utf-8")), fname)
            except Exception:
                pass
            break

    editorconfig = root / ".editorconfig"
    if editorconfig.is_file():
        try:
            # `root = true` (a bare key before any section) breaks strict
            # configparser, which requires a section header first — strip it,
            # we only care about the [*] section anyway.
            text = re.sub(r"(?im)^\s*root\s*=.*$", "", editorconfig.read_text(encoding="utf-8"))
            cp = configparser.ConfigParser()
            cp.read_string(text)
            section = cp["*"] if cp.has_section("*") else None
            if section is not None:
                indent_style = section.get("indent_style")
                if indent_style:
                    size = section.get("indent_size")
                    extra = f", size {size}" if size else ""
                    items.append({"content": f"Indent style '{indent_style}'{extra} (.editorconfig)",
                                  "subtype": "convention", "tier": "high"})
                max_len = section.get("max_line_length")
                if max_len:
                    items.append({"content": f"Max line length {max_len} (.editorconfig)",
                                  "subtype": "convention", "tier": "high"})
        except Exception:
            pass

    precommit = root / ".pre-commit-config.yaml"
    if precommit.is_file():
        try:
            text = precommit.read_text(encoding="utf-8", errors="ignore")
            ids = _PRECOMMIT_ID.findall(text)[:6]
            if ids:
                items.append({"content": f"Pre-commit hooks run: {', '.join(ids)} "
                                         "(.pre-commit-config.yaml)",
                              "subtype": "convention", "tier": "high"})
        except Exception:
            pass

    for fname in (".prettierrc", ".prettierrc.json"):
        p = root / fname
        if p.is_file():
            try:
                cfg = json.loads(p.read_text(encoding="utf-8"))
                parts = []
                if "semi" in cfg:
                    parts.append(f"semicolons {'on' if cfg['semi'] else 'off'}")
                if "singleQuote" in cfg:
                    parts.append("single quotes" if cfg["singleQuote"] else "double quotes")
                if "printWidth" in cfg:
                    parts.append(f"print width {cfg['printWidth']}")
                if parts:
                    items.append({"content": f"Prettier config: {'; '.join(parts)} ({fname})",
                                  "subtype": "convention", "tier": "high"})
            except Exception:
                pass
            break

    eslint = root / ".eslintrc.json"
    if eslint.is_file():
        try:
            cfg = json.loads(eslint.read_text(encoding="utf-8"))
            extends = cfg.get("extends")
            if extends:
                names = extends if isinstance(extends, list) else [extends]
                items.append({"content": f"ESLint extends {', '.join(names)} (.eslintrc.json)",
                              "subtype": "convention", "tier": "high"})
        except Exception:
            pass

    tsconfig = root / "tsconfig.json"
    if tsconfig.is_file():
        try:
            # jsonc: strip comments before json.loads. Naive — a `//` inside a
            # string literal (e.g. a URL) would be mis-stripped; acceptable,
            # documented limitation for a deterministic best-effort scan.
            text = tsconfig.read_text(encoding="utf-8")
            text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
            text = re.sub(r"(?m)//.*$", "", text)
            cfg = json.loads(text)
            if cfg.get("compilerOptions", {}).get("strict") is True:
                items.append({"content": "TypeScript strict mode enabled (tsconfig.json)",
                              "subtype": "convention", "tier": "high"})
        except Exception:
            pass

    workflows_dir = root / ".github" / "workflows"
    if workflows_dir.is_dir():
        try:
            files = sorted(workflows_dir.glob("*.yml")) + sorted(workflows_dir.glob("*.yaml"))
            cmds: list[str] = []
            for wf in files:
                try:
                    text = wf.read_text(encoding="utf-8", errors="ignore")
                except Exception:
                    continue
                for line in text.splitlines():
                    m = _RUN_LINE.match(line)
                    if not m:
                        continue
                    val = m.group(1).strip()
                    if val in ("|", ">"):
                        continue  # block-scalar marker — body lines aren't `run:`-prefixed
                    if val not in cmds:
                        cmds.append(val)
            if cmds and files:
                items.append({"content": f"CI runs: {'; '.join(cmds[:5])} (from {files[0].name})",
                              "subtype": "pattern", "tier": "high"})
        except Exception:
            pass

    return items


# ── 2. python source stats (naming, typing, docstrings, imports, exceptions) ──

def _classname_bases(node: ast.ClassDef) -> list[str]:
    bases = []
    for b in node.bases:
        if isinstance(b, ast.Name):
            bases.append(b.id)
        elif isinstance(b, ast.Attribute):
            bases.append(b.attr)
    return bases


def _annotated(fn) -> bool:
    args = fn.args
    all_args = [*args.posonlyargs, *args.args, *args.kwonlyargs]
    return fn.returns is not None or any(a.annotation is not None for a in all_args)


def _python_source_stats(root: Path) -> list[dict]:
    func_names: list[str] = []
    all_funcs: list = []
    class_nodes: list = []
    const_names: list[str] = []
    rel_imports = abs_imports = 0
    exc_class_count = 0
    bare_except_count = 0
    files_parsed = 0

    attempted = 0
    for path in _walk_files(root, ".py"):
        if _is_test_path(path):
            continue
        if attempted >= _MAX_PY_FILES:
            break
        attempted += 1
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            tree = ast.parse(text)
        except Exception:
            continue
        files_parsed += 1

        for node in tree.body:  # module-level only
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        const_names.append(tgt.id)
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                const_names.append(node.target.id)

        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                all_funcs.append(node)
                if not _DUNDER.match(node.name):
                    func_names.append(node.name)
            elif isinstance(node, ast.ClassDef):
                class_nodes.append(node)
                if any(_EXC_BASE.match(b) for b in _classname_bases(node)):
                    exc_class_count += 1
            elif isinstance(node, ast.ImportFrom):
                if node.level and node.level > 0:
                    rel_imports += 1
                else:
                    abs_imports += 1
            elif isinstance(node, ast.ExceptHandler):
                if node.type is None:
                    bare_except_count += 1

    items: list[dict] = []

    if func_names:
        matched = sum(1 for n in func_names if _SNAKE.match(n))
        r = _pct_tier(matched, len(func_names))
        if r:
            pct, tier = r
            items.append({"content": f"Functions use snake_case naming "
                                     f"({pct}% of {len(func_names)} functions across {files_parsed} files)",
                          "subtype": "convention", "tier": tier})

    if class_nodes:
        names = [c.name for c in class_nodes]
        matched = sum(1 for n in names if _PASCAL.match(n))
        r = _pct_tier(matched, len(names))
        if r:
            pct, tier = r
            items.append({"content": f"Classes use PascalCase naming "
                                     f"({pct}% of {len(names)} classes across {files_parsed} files)",
                          "subtype": "convention", "tier": tier})

    if const_names:
        matched = sum(1 for n in const_names if _UPPER.match(n))
        r = _pct_tier(matched, len(const_names))
        if r:
            pct, tier = r
            items.append({"content": f"Module-level constants use UPPER_CASE naming "
                                     f"({pct}% of {len(const_names)} constants)",
                          "subtype": "convention", "tier": tier})

    if all_funcs:
        typed = sum(1 for f in all_funcs if _annotated(f))
        r = _pct_tier(typed, len(all_funcs))
        if r:
            pct, tier = r
            items.append({"content": f"Functions use type hints ({pct}% of {len(all_funcs)} functions)",
                          "subtype": "convention", "tier": tier})

    public = [n for n in (*all_funcs, *class_nodes) if not n.name.startswith("_")]
    if public:
        documented = sum(1 for n in public if ast.get_docstring(n))
        r = _pct_tier(documented, len(public))
        if r:
            pct, tier = r
            items.append({"content": f"Public functions and classes have docstrings "
                                     f"({pct}% of {len(public)})",
                          "subtype": "convention", "tier": tier})

    total_imports = rel_imports + abs_imports
    if total_imports:
        if abs_imports >= rel_imports:
            r = _pct_tier(abs_imports, total_imports)
            label = "absolute"
        else:
            r = _pct_tier(rel_imports, total_imports)
            label = "relative"
        if r:
            pct, tier = r
            items.append({"content": f"Imports are {label} ({pct}% of {total_imports} imports)",
                          "subtype": "convention", "tier": tier})

    if exc_class_count >= 3:
        items.append({"content": f"Errors raised via {exc_class_count} custom exception classes",
                      "subtype": "pattern", "tier": "high"})

    if bare_except_count == 0 and files_parsed >= _MIN_SAMPLES:
        items.append({"content": f"No bare except clauses (0 across {files_parsed} files)",
                      "subtype": "pattern", "tier": "high"})

    return items


# ── 3. test conventions (layout, style, fixtures) ───────────────────────────

def _classify_test_funcs(tree: ast.AST) -> tuple[int, int, int]:
    """(unittest-style test count, plain/pytest-style test count, fixture count)."""
    counts = {"unittest": 0, "plain": 0, "fixture": 0}

    def visit(node, in_testcase):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef):
                visit(child, any("TestCase" in b for b in _classname_bases(child)))
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if child.name.startswith("test_"):
                    counts["unittest" if in_testcase else "plain"] += 1
                for deco in child.decorator_list:
                    d = deco.func if isinstance(deco, ast.Call) else deco
                    name = d.id if isinstance(d, ast.Name) else getattr(d, "attr", "")
                    if name == "fixture":
                        counts["fixture"] += 1
                visit(child, in_testcase)
            else:
                visit(child, in_testcase)

    visit(tree, False)
    return counts["unittest"], counts["plain"], counts["fixture"]


def _test_conventions(root: Path) -> list[dict]:
    items: list[dict] = []

    test_paths: list[Path] = []
    attempted = 0
    for path in _walk_files(root, ".py"):
        if not _is_test_path(path):
            continue
        if attempted >= _MAX_PY_FILES:
            break
        attempted += 1
        test_paths.append(path)

    if len(test_paths) < 3:
        return items

    under_tests_dir = sum(1 for p in test_paths if "tests" in p.parts[:-1])
    layout = "Tests live in tests/" if under_tests_dir * 2 >= len(test_paths) else "Tests colocated with source"
    items.append({"content": f"{layout} ({len(test_paths)} test files)",
                 "subtype": "convention", "tier": "high"})
    prefixed = sum(1 for p in test_paths if p.name.startswith("test_"))
    naming = "test_*.py" if prefixed * 2 >= len(test_paths) else "*_test.py"
    items.append({"content": f"Test files follow {naming} naming ({len(test_paths)} files)",
                 "subtype": "convention", "tier": "high"})

    total_unittest = total_plain = total_fixtures = 0
    for path in test_paths:
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8", errors="ignore"))
        except Exception:
            continue
        u, p, f = _classify_test_funcs(tree)
        total_unittest += u
        total_plain += p
        total_fixtures += f

    total_test_funcs = total_unittest + total_plain
    if total_test_funcs:
        if total_plain >= total_unittest:
            r = _pct_tier(total_plain, total_test_funcs)
            content = f"Tests use plain pytest asserts ({{}}% of {total_test_funcs} test functions)"
        else:
            r = _pct_tier(total_unittest, total_test_funcs)
            content = f"Tests use unittest.TestCase classes ({{}}% of {total_test_funcs} test functions)"
        if r:
            pct, tier = r
            items.append({"content": content.format(pct), "subtype": "convention", "tier": tier})

    if total_fixtures >= 3:
        items.append({"content": f"Pytest fixtures used for test setup ({total_fixtures} fixtures)",
                      "subtype": "pattern", "tier": "high"})

    return items


# ── 4. commit message convention ────────────────────────────────────────────

def _commit_convention(repo_path: str) -> list[dict]:
    out = _git(repo_path, "log", "--format=%s", "-n", "100")
    if not out:
        return []
    subjects = [s for s in out.splitlines() if s.strip()]
    matched = sum(1 for s in subjects if _CONVENTIONAL_COMMIT.match(s))
    r = _pct_tier(matched, len(subjects))
    if not r:
        return []
    pct, tier = r
    return [{"content": f"Commit messages follow Conventional Commits ({pct}% of last {len(subjects)})",
            "subtype": "convention", "tier": tier}]


# ── public API ───────────────────────────────────────────────────────────────

def mine_conventions(repo_path: str) -> list[dict]:
    """Deterministic convention detectors over the repo. Never raises; returns [] on failure.
    Item: {"content": str, "subtype": "convention"|"pattern", "tier": "high"|"medium"}
    """
    items: list[dict] = []
    try:
        root = Path(repo_path)
        if not root.is_dir():
            return []
        for fn, arg in (
            (_config_conventions, root),
            (_python_source_stats, root),
            (_test_conventions, root),
            (_commit_convention, repo_path),
        ):
            try:
                items.extend(fn(arg))
            except Exception:
                continue
    except Exception:
        pass
    return items
