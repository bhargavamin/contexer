"""Deterministic scorers for the A/B benchmark. No LLM judge — a violation is a
measurable contradiction of a high-tier mined convention in the changed code."""
import ast
import json
import re
import subprocess
from pathlib import Path

from benchmarks.memory_home import memory_files
from contexer import store as _store

_SNAKE = re.compile(r"^_?[a-z][a-z0-9_]*$")
# True dunders only (__init__), matching the miner's exclusion — a generated
# __BadName is part of the mined snake_case population and must be scored.
_DUNDER = re.compile(r"^__.*__$")


def _is_test_path(rel: str) -> bool:
    """Mirror the miner's test-file detection: baseline source conventions are mined
    from NON-test files, so test files must not be scored against them."""
    parts = rel.split("/")
    name = parts[-1]
    return name.startswith("test_") or name.endswith("_test.py") or "tests" in parts[:-1]


def rationale_score(answer: str, gold: list) -> float:
    """Fraction of the gold facts the answer contains.

    A gold item is either a string (that substring must appear) or a LIST of
    accepted spellings for the same fact, any one of which counts. The variant
    form exists so a gold fact cannot reward verbatim echoing of one system's
    stored wording: "machine-parseable" and "machine readable" are the same
    fact, and a scorer that only accepts the first measures architecture
    (Contexer injects the sentence verbatim) rather than recall."""
    if not gold:
        return 1.0
    low = answer.lower()
    hits = sum(1 for g in gold
               if any(a.lower() in low for a in ([g] if isinstance(g, str) else g)))
    return hits / len(gold)


def changed_files(repo: str, base: str = "HEAD") -> dict[str, str]:
    """Files changed since `base` (a ref or sha) plus untracked files. Callers that
    can observe the repo's pre-session HEAD should pass it — diffing against a live
    HEAD misses edits the session committed before finishing."""
    root = Path(repo)
    tracked = subprocess.run(["git", "-C", repo, "diff", "--name-only", base],
                             capture_output=True, text=True).stdout.split()
    untracked = subprocess.run(["git", "-C", repo, "ls-files", "--others", "--exclude-standard"],
                               capture_output=True, text=True).stdout.split()
    out = {}
    for rel in {*tracked, *untracked}:
        p = root / rel
        if p.is_file():
            try:
                out[rel] = p.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                pass
    return out


def count_violations(diff_files: dict[str, str], baseline: list[dict]) -> int:
    checks = set()
    for conv in baseline:
        if conv["tier"] != "high":
            continue
        if "snake_case naming" in conv["content"]:
            checks.add("snake")
        if "type hints" in conv["content"]:
            checks.add("hints")
    violations = 0
    for rel, text in diff_files.items():
        if not rel.endswith(".py") or _is_test_path(rel):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            violations += 1
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if "snake" in checks and not _DUNDER.match(node.name) and not _SNAKE.match(node.name):
                    violations += 1
                args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                if "hints" in checks and node.returns is None and not any(a.annotation for a in args):
                    violations += 1
    return violations


def sup_current_score(answer: str, current: str = "dynamodb", superseded: str = "postgres") -> str:
    """Slot-scored supersession check (spec: never free-text NLP). First non-empty
    line names the choice; 'review' rows are counted separately, never dropped."""
    first = next((ln for ln in (answer or "").splitlines() if ln.strip()), "").lower()
    has_cur, has_old = current in first, superseded in first
    if has_cur and not has_old:
        return "pass"
    if has_old and not has_cur:
        return "fail"
    return "review"


def capture_stats(home, repo) -> dict:
    home, repo = Path(home), Path(repo)
    slug = _store.repo_slug(str(repo))
    entries = 0
    store = home / ".contexer" / f"{slug}.json"
    try:
        data = json.loads(store.read_text())
        entries = sum(1 for e in data.get("entries", []) if e.get("type") == "decision")
    except Exception:
        entries = 0
    return {"memory_files": len(memory_files(home, repo)), "contexer_entries": entries}
