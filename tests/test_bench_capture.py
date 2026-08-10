import json, re
from benchmarks.score import capture_stats


def test_counts_both_systems(tmp_path):
    repo = tmp_path / "r"; repo.mkdir()
    mem = tmp_path / ".claude" / "projects" / re.sub(r"[^a-zA-Z0-9]", "-", str(repo)) / "memory"
    mem.mkdir(parents=True); (mem / "fact.md").write_text("x"); (mem / "MEMORY.md").write_text("i")
    slug = re.sub(r"[^a-zA-Z0-9]", "_", str(repo))
    ctx = tmp_path / ".contexer"; ctx.mkdir()
    (ctx / f"{slug}.json").write_text(json.dumps({"entries": [
        {"type": "decision"}, {"type": "decision"}, {"type": "task"}]}))
    assert capture_stats(tmp_path, repo) == {"memory_files": 1, "contexer_entries": 2}


def test_fail_soft_on_missing_and_corrupt(tmp_path):
    repo = tmp_path / "r"; repo.mkdir()
    assert capture_stats(tmp_path, repo) == {"memory_files": 0, "contexer_entries": 0}
    ctx = tmp_path / ".contexer"; ctx.mkdir()
    (ctx / (re.sub(r"[^a-zA-Z0-9]", "_", str(repo)) + ".json")).write_text("{not json")
    assert capture_stats(tmp_path, repo)["contexer_entries"] == 0
