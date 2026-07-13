"""Deterministic scorers for the A/B benchmark. No LLM judge — a violation is a
measurable contradiction of a high-tier mined convention in the changed code."""
import ast
import re
import subprocess
from pathlib import Path

_SNAKE = re.compile(r"^_?[a-z][a-z0-9_]*$")


def rationale_score(answer: str, gold: list[str]) -> float:
    if not gold:
        return 1.0
    low = answer.lower()
    return sum(1 for g in gold if g.lower() in low) / len(gold)


def changed_files(repo: str) -> dict[str, str]:
    root = Path(repo)
    tracked = subprocess.run(["git", "-C", repo, "diff", "--name-only", "HEAD"],
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
        if not rel.endswith(".py"):
            continue
        try:
            tree = ast.parse(text)
        except SyntaxError:
            violations += 1
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if "snake" in checks and not node.name.startswith("__") and not _SNAKE.match(node.name):
                    violations += 1
                args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
                if "hints" in checks and node.returns is None and not any(a.annotation for a in args):
                    violations += 1
    return violations
