# tests/test_bench_teaching.py
import json
from pathlib import Path

def _load():
    return json.loads((Path("benchmarks/teaching.json")).read_text())

def test_two_tiers_two_sessions():
    t = _load()
    assert {(e["session"], e["tier"]) for e in t} == {(1, "implicit"), (1, "explicit"), (2, "implicit"), (2, "explicit")}

def test_session1_teaches_all_four_facts_session2_only_supersedes():
    t = _load()
    for e in t:
        joined = " ".join(e["prompts"]).lower()
        if e["session"] == 1:
            for kw in ("machine-parseable", "postgres", "lru", "never log request data"):
                assert kw in joined, (e["tier"], kw)
            assert "dynamodb" not in joined
        else:
            assert "dynamodb" in joined and "lru" not in joined
