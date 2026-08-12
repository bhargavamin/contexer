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


def test_rule_prompt_is_capturable_and_approved_in_both_tiers():
    """The enf-commit demo arms a guard on the taught rule, which requires it to
    land `approved`. A deictic phrasing ("remember this rule") lands
    `pending_approval` instead, silently killing the demo in that tier."""
    from contexer import store
    for e in _load():
        for p in e["prompts"]:
            if "never log request data" not in p.lower():
                continue
            assert store._is_prescriptive_constraint(p)[0], (e["tier"], p)
            assert not store._is_deictic(p), (e["tier"], p)
