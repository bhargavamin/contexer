import json
from pathlib import Path

def test_memory_tasks_schema_and_ids():
    ts = json.loads(Path("benchmarks/memory_tasks.json").read_text())
    assert {t["id"] for t in ts} == {"rat-mem", "sup-current", "cont-log", "enf-commit"}
    for t in ts:
        assert t["seed_decision"] == ""   # organic seeding only
        assert t["scorer"] in {"rationale", "sup_current", "violations", "enforcement"}
    sup = next(t for t in ts if t["id"] == "sup-current")
    assert "first line" in sup["prompt"]
    assert sup["headline"] is True
