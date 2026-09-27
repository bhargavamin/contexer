import json

from conftest import commit, git, run_script
from test_log_usage import example


def record(i, question_type, effect, key=None, **judgment_changes):
    j = example()
    j["task"]["question_type"] = question_type
    j["task"]["task_id"] = f"task-{i}"
    j["verdict"]["contexer_effect"] = effect
    if effect in ('helpful', 'decisive'):
        j['contexer_items'][0].update(relevance=effect, note='Used the stored rule to choose the fix')
        j['key_facts'][0].update(source='contexer', contexer_ids=[j['contexer_items'][0]['id']])
    j.update(judgment_changes)
    return {"schema": "contexer-effectiveness/v3", "record_id": f"r{i}",
            "record_key": key or f"s{i}|abc{i}|0", "recorded_at": f"2026-09-{10 + i:02d}T00:00:00",
            "host": "claude", "session_id": f"s{i}", "repo": "", "repo_key": "demo-1",
            "trigger": {"attribution": "transcript", "commits": [{"sha": f"abc{i}"}],
                        "pr_create_seen": False},
            "observed": {"contexer_call_count": 1, "contexer_calls": [{"tool": "get_context"}],
                         "exploration_before_first_contexer_call": 2, "autofetch_blocks": None},
            "pr_url": None, "pr_lookup": "none", "judgment": j}


def write_records(data_dir, recs, extra_lines=()):
    path = data_dir / "records" / "demo-1.jsonl"
    path.parent.mkdir(parents=True)
    lines = [json.dumps(r) for r in recs] + list(extra_lines)
    path.write_text("\n".join(lines) + "\n")


def test_small_rows_are_not_ranked_above_large_ones(home):
    data_dir, env = home
    recs = [record(i, "convention", "helpful" if i < 3 else "neutral") for i in range(5)]
    recs.append(record(9, "rationale", "helpful"))
    write_records(data_dir, recs)
    out = run_script("summarize.py", env=env, check=True).stdout
    section = out.split("**Knowledge the task needed**")[1].split("**")[0]
    table, few = section.split("Too few to rank:")
    assert "| convention | 3/5 |" in table and "rationale" not in table
    assert "rationale 1/1" in few


def test_malformed_and_duplicate_records_do_not_break_the_report(home):
    data_dir, env = home
    good = record(1, "convention", "neutral")
    dup = dict(good, record_id="r1b")
    broken = record(2, "convention", "neutral")
    del broken["judgment"]["task"]["scope"]
    write_records(data_dir, [good, dup, broken], extra_lines=["not json"])
    out = run_script("summarize.py", env=env, check=True).stdout
    assert "Skipped 2 unreadable or older-schema record(s) and 1 duplicate(s)" in out
    assert "Records: 1 " in out


def test_revert_of_a_branch_commit_is_detected(repo):
    import outcomes
    sha = commit(repo, "b.txt", "feat: b")
    git(repo, "revert", "--no-edit", "HEAD")
    assert outcomes.reverted(str(repo), [sha[:12]]) == [sha[:12]]
    assert outcomes.reverted("", [sha]) is None


def test_revert_of_a_squash_merge_commit_is_detected(repo, monkeypatch):
    import outcomes
    git(repo, "checkout", "-qb", "feat")
    branch_sha = commit(repo, "f.txt", "feat: f")
    git(repo, "checkout", "-q", "main")
    git(repo, "merge", "--squash", "feat")
    git(repo, "commit", "-qm", "feat: f (#1)")
    squash = git(repo, "rev-parse", "HEAD")
    git(repo, "revert", "--no-edit", "HEAD")
    monkeypatch.setattr(outcomes, "pr_outcome", lambda url, r: ({"pr_state": "merged"}, squash))
    rec = record(1, "convention", "neutral")
    rec.update(repo=str(repo), pr_url="https://example/pr/1", pr_lookup="ok")
    rec["trigger"]["commits"] = [{"sha": branch_sha[:12]}]
    row = outcomes.outcome_for(rec)
    assert row["pr_state"] == "merged" and row["reverted"] == [squash]


def test_outcome_rows_append_only_when_changed(home, repo):
    data_dir, env = home
    rec = record(1, "convention", "neutral")
    rec.update(repo=str(repo), pr_lookup="none")
    write_records(data_dir, [rec])
    first = run_script("outcomes.py", env=env, check=True).stdout
    second = run_script("outcomes.py", env=env, check=True).stdout
    assert "1 outcome(s) changed" in first and "0 outcome(s) changed" in second
    row = json.loads((data_dir / "outcomes.jsonl").read_text().splitlines()[0])
    assert row["pr_state"] == "no_pr" and row["reverted"] == []


def test_failed_pr_lookup_is_unknown_not_no_pr(home, repo):
    data_dir, env = home
    rec = record(1, "convention", "neutral")
    git(repo, "checkout", "-qb", "feat")
    rec.update(repo=str(repo), branch="feat", pr_lookup="error")
    write_records(data_dir, [rec])
    run_script("outcomes.py", env=env, check=True)
    row = json.loads((data_dir / "outcomes.jsonl").read_text().splitlines()[0])
    assert row["pr_state"] == "unknown"
