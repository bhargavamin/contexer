#!/usr/bin/env bash
# Contexer demo environment - reproducible repo + store state for the demo video.
#
# Usage:
#   docs/demo/seed.sh --reset    # fresh demo repo, EMPTY store (record Act 2 scene A live)
#   docs/demo/seed.sh --seeded   # fresh demo repo + pre-seeded decisions (jump to scene B)
#
# The demo repo lands in ~/contexer-demo/taskflow with a demo git identity, so no
# personal paths or emails appear on camera. Only the store files for THIS repo are
# touched - your real ~/.contexer stores are left alone.
set -euo pipefail

MODE="${1:---reset}"
DEMO_DIR="$HOME/contexer-demo/taskflow"

# Locate a python that can import contexer: the installed tool's own interpreter
# (read from the launcher's shebang - the venv python is not next to the launcher),
# falling back to plain python3 (e.g. a dev clone with contexer on the path).
PY="python3"
if command -v contexer >/dev/null 2>&1; then
    SHEBANG_PY="$(head -1 "$(command -v contexer)" | sed 's/^#!//')"
    if [ -x "$SHEBANG_PY" ] && "$SHEBANG_PY" -c "import contexer" 2>/dev/null; then
        PY="$SHEBANG_PY"
    fi
fi
if ! "$PY" -c "import contexer" 2>/dev/null; then
    echo "error: cannot import contexer (install it first: uv tool install contexer)" >&2
    exit 1
fi

# ── demo repo ─────────────────────────────────────────────────────────────────
rm -rf "$DEMO_DIR"
mkdir -p "$DEMO_DIR/app"
cd "$DEMO_DIR"

cat > pyproject.toml <<'PYPROJECT'
[project]
name = "taskflow"
version = "0.1.0"
description = "Tiny task service used for the Contexer demo"
requires-python = ">=3.12"
dependencies = ["fastapi", "uvicorn", "psycopg[binary]"]
PYPROJECT

cat > app/main.py <<'MAIN'
"""TaskFlow - a tiny task service (Contexer demo repo)."""
from fastapi import FastAPI

from app.repository import TaskRepository

app = FastAPI(title="TaskFlow")
repo = TaskRepository()


@app.get("/tasks")
def list_tasks():
    return repo.list()


@app.post("/tasks")
def create_task(title: str):
    return repo.create(title)
MAIN

cat > app/repository.py <<'REPO'
"""Repository layer - services never touch SQL directly (see Contexer pattern)."""


class TaskRepository:
    def __init__(self):
        self._tasks = []

    def list(self):
        return self._tasks

    def create(self, title: str):
        task = {"id": len(self._tasks) + 1, "title": title}
        self._tasks.append(task)
        return task
REPO

printf '# TaskFlow\n\nDemo service for the Contexer video.\n' > README.md

git init -q
git config user.name "Demo Dev"
git config user.email "demo@contexer.ai"
git add -A
git commit -qm "feat: initial task service"

# ── store state for THIS repo only ────────────────────────────────────────────
"$PY" - "$DEMO_DIR" <<'PYSEED'
import sys
from contexer import store

repo = sys.argv[1]
slug = store._slug(repo)
removed = 0
for f in store.STORE_DIR.glob(f"*{slug}*"):
    f.unlink()
    removed += 1
print(f"  store reset for {repo} ({removed} file(s) removed)")
PYSEED

if [ "$MODE" = "--seeded" ]; then
"$PY" - "$DEMO_DIR" <<'PYSEED'
import sys
from contexer import store

repo = sys.argv[1]
store.update_decision(
    repo,
    "Use Postgres for task storage instead of DynamoDB - we need relational integrity "
    "across task/project joins and the team already operates RDS",
    "demo", subtype="architecture", created_by="human")
store.update_decision(
    repo,
    "Never log request bodies - they can contain PII",
    "demo", subtype="constraint", created_by="human")
store.update_decision(
    repo,
    "Use uv for all dependency management, not pip",
    "demo", subtype="convention", created_by="human")
store.update_decision(
    repo,
    "Repository pattern for data access - services never touch SQL directly",
    "demo", subtype="pattern", created_by="human")
print("  seeded 4 decisions (architecture, constraint, convention, pattern)")
PYSEED
fi

# ── recording hygiene warnings ────────────────────────────────────────────────
GLOBAL="$HOME/.contexer/_global.json"
if [ -s "$GLOBAL" ]; then
    echo "  ! $GLOBAL exists - your personal global rules WILL appear in every banner."
    echo "    Move it aside for recording:  mv $GLOBAL $GLOBAL.bak   (restore after)"
fi

echo
echo "Demo repo ready: $DEMO_DIR  (mode: $MODE)"
echo "Next: open it in your agent -  cd $DEMO_DIR && claude"
