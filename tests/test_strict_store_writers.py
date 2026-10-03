"""A damaged store remains recoverable through every mutation surface."""
import ast
from pathlib import Path

import pytest

from contexer import anchors, conflicts, guard_engine, lifecycle, server, store


WRITERS = {
    "mcp capture": lambda r, i: server.update_context("Use Redis for the cache because latency matters.", repo_path=r),
    "directive": lambda r, i: store.capture_user_constraint(r, "Always use Redis for the cache", "s"),
    "capture": lambda r, i: store.update_decision(r, "Use Redis for the cache", "s"),
    "approval": lambda r, i: store.approve_decision(r, i, "approve"),
    "edit": lambda r, i: store.edit_decision(r, i, content="Use Redis for the cache"),
    "delete": lambda r, i: store.delete_decision(r, i),
    "retire": lambda r, i: lifecycle.retire_decision(r, i, "obsolete"),
    "restore": lambda r, i: lifecycle.restore_decision(r, i),
    "lifecycle proposal": lambda r, i: lifecycle.propose_lifecycle(r, i, "retire", "obsolete", source="ai"),
    "lifecycle dismissal": lambda r, i: lifecycle.dismiss_lifecycle(r, i),
    "anchors": lambda r, i: store.apply_backfill_anchors(r, {i: ["app.py"]}),
    "anchor verification": lambda r, i: anchors.verify_anchors(r, force=True),
    "memory": lambda r, i: store.upsert_memory_decision(r, "Use Redis for the cache", "s", "convention", "k"),
    "memory batch": lambda r, i: store.upsert_memory_batch(r, [("Use Redis for the cache", "s", "convention", "k")]),
    "team proposal": lambda r, i: store.attach_team_reconciliation_proposal(r, i, content="Use Redis"),
    "team convergence": lambda r, i: store.clear_team_reconciliation_proposal(r, i),
    "evidence": lambda r, i: store.record_evidence_summary(r, i, {"candidate_id": "c"}),
    "conflict memo": lambda r, i: conflicts.record_conflict_memo(r, i, "standing"),
    "arm": lambda r, i: guard_engine.arm_guard(r, i, "secret"),
    "disarm": lambda r, i: guard_engine.disarm_guard(r, i),
    "scan verification": lambda r, i: store.verify_scan_conventions(r, force=True),
}


@pytest.mark.parametrize("writer", WRITERS, ids=list(WRITERS))
@pytest.mark.parametrize("damaged", [b'{"entries": [', b'[]', b'{"entries": "oops"}', b'{"entries": ["oops"]}', b'\xff'])
def test_mutations_preserve_damaged_bytes(tmp_repo, writer, damaged):
    _, entry_id = store.update_decision(tmp_repo, "Use Postgres for the database", "s", "convention")
    # Restoration must reach the live-store read even when the decision is retired.
    if writer == "restore":
        store.delete_decision(tmp_repo, entry_id)
    path = store._store_path(tmp_repo)
    path.write_bytes(damaged)
    before = {p: p.read_bytes() for p in store.store_dir().rglob("*") if p.is_file()}
    if writer == "anchor verification":
        # The session-start verifier is deliberately opportunistic and fail-soft.
        WRITERS[writer](tmp_repo, entry_id)
    else:
        with pytest.raises(ValueError, match="refusing write"):
            WRITERS[writer](tmp_repo, entry_id)
    assert path.read_bytes() == damaged
    for p, content in before.items():
        assert p.read_bytes() == content


def test_rendering_keeps_degrading(tmp_repo):
    store._store_path(tmp_repo).write_bytes(b'{"entries": [')
    assert store.load(tmp_repo)["entries"] == []
    assert "No context stored" in store.get_context(tmp_repo)
    assert isinstance(store.session_start_payload(tmp_repo, "startup", "s"), dict)
    assert isinstance(store.get_context_for_prompt(tmp_repo, "Why use Postgres?", "s"), str)


@pytest.mark.parametrize("operation", [guard_engine.arm_guard, guard_engine.disarm_guard])
def test_global_management_preserves_corruption(tmp_repo, operation):
    path = store._global_path()
    path.write_bytes(b'{"entries": [')
    with pytest.raises(ValueError, match="refusing write"):
        if operation is guard_engine.arm_guard:
            operation(tmp_repo, "12345678", "secret")
        else:
            operation(tmp_repo, "12345678")
    assert path.read_bytes() == b'{"entries": ['


def test_direct_store_writers_never_use_render_reader():
    failures = []
    for path in (Path(__file__).parents[1] / "contexer").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)]
            names = {n.func.id if isinstance(n.func, ast.Name) else n.func.attr
                     for n in calls if isinstance(n.func, (ast.Name, ast.Attribute))}
            if names & {"save", "save_global", "_save_deleted"} and names & {"load", "load_global"}:
                # Bootstrap reads global context solely for comparison; it writes the repo.
                if path.name == "bootstrap.py" and node.name == "run" and "load" not in names:
                    continue
                failures.append(f"{path.name}:{node.name}")
    assert not failures, failures
