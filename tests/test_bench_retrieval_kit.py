"""Decision-dependent retrieval kit: benchmarks/retrieval_tasks.json, its seeding in both
arms of benchmarks/run.py, the report section, and the offline delivery preview."""
import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks import replay_delivery, report, run, seeding, validate
from benchmarks.fixtures.generate import build_webapi

TASKS_FILE = Path(__file__).resolve().parent.parent / "benchmarks" / "retrieval_tasks.json"
TASKS = json.loads(TASKS_FILE.read_text())
BY_ID = {t["id"]: t for t in TASKS}
SEED = 7

# (task, label, appended module code, check must pass). A compliant variant follows the
# needed decision; the others are what an agent without it plausibly writes. Every check
# must also fail on the untouched fixture: a check that can pass vacuously or by guessing
# measures nothing, and one that rejects a compliant variant counts a win as a loss.
VARIANTS = [
    ("retr-batch", "compliant",
     "\ndef fetch_records_batch(record_ids):\n    out = {'items': [], 'failed': []}\n"
     "    for r in record_ids:\n        if r < 1:\n            out['failed'].append(r)\n"
     "        else:\n            out['items'].append({'id': r})\n    return out\n", True),
    ("retr-batch", "list-skipping-bad-ids",
     "\ndef fetch_records_batch(record_ids):\n"
     "    return [{'id': r} for r in record_ids if r >= 1]\n", False),
    ("retr-batch", "ids-instead-of-records",
     "\ndef fetch_records_batch(record_ids):\n"
     "    return {'items': [r for r in record_ids if r >= 1],\n"
     "            'failed': [r for r in record_ids if r < 1]}\n", False),
    ("retr-batch", "only-zero-fails",
     "\ndef fetch_records_batch(record_ids):\n"
     "    return {'items': [{'id': r} for r in record_ids if r != 0],\n"
     "            'failed': [r for r in record_ids if r == 0]}\n", False),
    ("retr-cache", "compliant-decorated",
     "\nimport functools\n"
     "fetch_record_7_0 = functools.lru_cache(maxsize=256)(fetch_record_7_0)\n", True),
    ("retr-cache", "compliant-delegated",
     "\nimport functools\n_impl = fetch_record_7_0\n_cached = functools.lru_cache(maxsize=256)(_impl)\n"
     "def fetch_record_7_0(record_id):\n    return _cached(record_id)\n", True),
    ("retr-cache", "unbounded-lru",
     "\nimport functools\n"
     "fetch_record_7_0 = functools.lru_cache(maxsize=None)(fetch_record_7_0)\n", False),
    ("retr-cache", "functools-cache",
     "\nimport functools\nfetch_record_7_0 = functools.cache(fetch_record_7_0)\n", False),
    ("retr-cache", "dict-cache",
     "\n_impl = fetch_record_7_0\n_memo = {}\ndef fetch_record_7_0(record_id):\n"
     "    if record_id not in _memo:\n        _memo[record_id] = _impl(record_id)\n"
     "    return _memo[record_id]\n", False),
    ("retr-audit", "compliant",
     "\nimport time as _t\ndef record_audit_entry(record_id, action):\n"
     "    return {'id': record_id, 'action': action, 'at_ms': int(_t.time() * 1000)}\n", True),
    ("retr-audit", "iso-timestamp",
     "\nimport datetime\ndef record_audit_entry(record_id, action):\n"
     "    return {'id': record_id, 'action': action, "
     "'timestamp': datetime.datetime.now().isoformat()}\n", False),
    ("retr-audit", "epoch-seconds",
     "\nimport time as _t\ndef record_audit_entry(record_id, action):\n"
     "    return {'id': record_id, 'action': action, 'at_ms': int(_t.time())}\n", False),
    ("retr-errors", "compliant",
     "\nclass RecordInputError(ValueError):\n    pass\n_orig = fetch_record_7_1\n"
     "def fetch_record_7_1(record_id):\n    if record_id < 1:\n"
     "        raise RecordInputError(record_id)\n    return _orig(record_id)\n", True),
    ("retr-errors", "plain-valueerror",
     "\n_orig = fetch_record_7_1\ndef fetch_record_7_1(record_id):\n    if record_id < 1:\n"
     "        raise ValueError(record_id)\n    return _orig(record_id)\n", False),
]


class TestTaskFile:
    def test_shape_matches_runner_contract(self):
        assert len(TASKS) >= 4
        assert len(BY_ID) == len(TASKS)
        for t in TASKS:
            assert t["kind"] == "retrieval" and t["chain"] == "" and t["step"] == 0
            assert t["gold"] == [] and t["seed_decision"] == ""
            assert t["check_cmd"].startswith('uv run python -c "')
            assert 0 <= t["needed_decision"] < len(t["seed_decisions"])

    def test_tasks_share_one_store(self):
        # One realistic store, many tasks: the decision a task needs competes with the
        # others anchored to the same file.
        assert all(t["seed_decisions"] == TASKS[0]["seed_decisions"] for t in TASKS)
        anchors = {f for s in TASKS[0]["seed_decisions"] for f in s["source_files"]}
        assert anchors == {"app/svc_{seed}_core.py"}
        assert len(TASKS[0]["seed_decisions"]) > 3  # more anchors than full-content slots

    def test_needed_decisions_must_be_retrieved_not_preloaded(self):
        seeds = TASKS[0]["seed_decisions"]
        needed = [t["needed_decision"] for t in TASKS]
        assert len(set(needed)) == len(needed)
        # Constraints are preloaded in full at session start; a needed decision of that
        # type would measure the preload, not retrieval.
        assert all(seeds[i]["subtype"] != "constraint" for i in needed)
        # ...but the store does hold a preloaded constraint, so re-delivery is measurable.
        assert any(s["subtype"] == "constraint" for s in seeds)

    def test_prompts_name_the_file_but_not_the_decision(self):
        for t in TASKS:
            needed = t["seed_decisions"][t["needed_decision"]]["content"]
            assert "app/svc_{seed}_core.py" in t["prompt"]
            for token in ("'items'", "maxsize=256", "at_ms", "RecordInputError"):
                if token in needed:
                    assert token not in t["prompt"], (t["id"], token)

    def test_every_task_has_a_compliant_and_a_rejected_variant(self):
        for task_id in BY_ID:
            outcomes = {passes for tid, _, _, passes in VARIANTS if tid == task_id}
            assert outcomes == {True, False}, task_id


@pytest.fixture(scope="module")
def golden(tmp_path_factory):
    return build_webapi(tmp_path_factory.mktemp("retrieval") / "golden", seed=SEED)


def _check(task, golden, tmp_path, extra):
    work = tmp_path / "work"
    shutil.copytree(golden, work)
    core = work / "app" / f"svc_{SEED}_core.py"
    core.write_text(core.read_text() + extra)
    code = task["check_cmd"].replace("{seed}", str(SEED))
    script = code[len('uv run python -c "'):-1]
    return subprocess.run([sys.executable, "-c", script], cwd=work,
                          capture_output=True, text=True)


@pytest.mark.parametrize("task", TASKS, ids=list(BY_ID))
def test_check_fails_on_the_untouched_fixture(task, golden, tmp_path):
    assert _check(task, golden, tmp_path, "").returncode != 0


@pytest.mark.parametrize(("task_id", "label", "extra", "passes"), VARIANTS,
                         ids=[f"{t}-{label}" for t, label, _, _ in VARIANTS])
def test_check_judges_each_variant(task_id, label, extra, passes, golden, tmp_path):
    proc = _check(BY_ID[task_id], golden, tmp_path, extra)
    assert (proc.returncode == 0) is passes, proc.stderr


class TestSeeding:
    def test_seed_items_fill_seed_and_keep_order(self):
        items = seeding.seed_items(TASKS[0], 5)
        assert [i["content"] for i in items] == [
            s["content"].replace("{seed}", "5") for s in TASKS[0]["seed_decisions"]]
        assert all(i["source_files"] == ["app/svc_5_core.py"] for i in items)
        assert seeding.seed_items({"seed_decision": "x"}, 5) == []

    def _seed_script(self, tmp_path, monkeypatch):
        commands = []
        monkeypatch.setattr(run.subprocess, "run", lambda args, **kw: commands.append(args))
        items = seeding.seed_items(TASKS[0], SEED)
        run._condition_b_setup(str(tmp_path), tmp_path / "home", "", seed_decisions=items)
        return items, commands[-1][-1]

    def test_contexer_arm_seeds_every_decision_as_human_with_anchor(self, tmp_path,
                                                                    monkeypatch):
        from contexer import server, store
        items, code = self._seed_script(tmp_path, monkeypatch)
        # The script runs inside the source checkout, which may be an older one without
        # this harness's modules: it must not import from `benchmarks`.
        assert "benchmarks" not in code
        captures = []
        monkeypatch.setattr(server, "bootstrap_context", lambda **kw: None)
        monkeypatch.setattr(store, "update_decision",
                            lambda *a, **kw: captures.append((a, kw)) or (True, "id"))
        exec(compile(code, "<seed>", "exec"), {})
        assert [a[1] for a, _ in captures] == [i["content"] for i in items]
        assert [a[3] for a, _ in captures] == [i["subtype"] for i in items]
        assert all(kw == {"created_by": "human", "source_files": [f"app/svc_{SEED}_core.py"]}
                   for _, kw in captures)

    def test_a_refused_seed_fails_setup_instead_of_dropping_silently(self, tmp_path,
                                                                     monkeypatch):
        from contexer import server, store
        _, code = self._seed_script(tmp_path, monkeypatch)
        monkeypatch.setattr(server, "bootstrap_context", lambda **kw: None)
        monkeypatch.setattr(store, "update_decision", lambda *a, **kw: (False, None))
        with pytest.raises(AssertionError, match="seed not stored"):
            exec(compile(code, "<seed>", "exec"), {})

    def test_static_file_arm_carries_the_same_decisions(self, golden, tmp_path):
        items = seeding.seed_items(TASKS[0], SEED)
        work = tmp_path / "one"
        shutil.copytree(golden, work)
        run._condition_c_setup(work, "", seed_decisions=items)
        text = (work / "CLAUDE.md").read_text()
        assert all(f"- {i['content']}" in text for i in items)

        split = tmp_path / "two"
        shutil.copytree(golden, split)
        run._condition_c_setup(split, "", ("CLAUDE.md", "AGENTS.md"), seed_decisions=items)
        both = (split / "CLAUDE.md").read_text() + (split / "AGENTS.md").read_text()
        assert all(both.count(i["content"]) == 1 for i in items)
        assert "Never log record ids" in (split / "AGENTS.md").read_text()

    @pytest.mark.parametrize("sources", [None, {"with_prev": ""}, {"with_prev": "  "}])
    def test_with_prev_without_a_source_is_refused(self, tmp_path, sources):
        with pytest.raises(ValueError, match="with_prev"):
            run.run_campaign(tmp_path / "c", reps=1, conditions=("with", "with_prev"),
                             contexer_sources=sources, tasks_file=TASKS_FILE,
                             wait_for_otel=False)
        assert not (tmp_path / "c").exists()

    def test_a_refused_seed_names_itself_in_the_row_error(self, tmp_path, monkeypatch):
        def fake_run(args, **kw):
            if args[:3] == ["uv", "run", "python"]:
                raise subprocess.CalledProcessError(
                    1, args, stderr=b"Traceback ...\nAssertionError: seed not stored: Caches")
        monkeypatch.setattr(run.subprocess, "run", fake_run)
        with pytest.raises(RuntimeError, match="seed not stored: Caches"):
            run._condition_b_setup(str(tmp_path), tmp_path / "home", "",
                                   seed_decisions=seeding.seed_items(TASKS[0], SEED))

    def test_an_output_dir_never_mixes_task_files(self, tmp_path):
        out = tmp_path / "camp"
        out.mkdir()
        (out / "runs.jsonl").write_text('{"task_id": "conv-endpoint"}\n')
        (out / "campaign.json").write_text(json.dumps({"tasks_sha256": "0" * 64}))
        with pytest.raises(ValueError, match="different task file"):
            run.run_campaign(out, reps=1, conditions=("without",), tasks_file=TASKS_FILE,
                             wait_for_otel=False)
        # A legacy campaign (no recorded hash) only ever ran tasks.json: refused too.
        (out / "campaign.json").write_text(json.dumps({"model": "m"}))
        with pytest.raises(ValueError, match="different task file"):
            run.run_campaign(out, reps=1, conditions=("without",), tasks_file=TASKS_FILE,
                             wait_for_otel=False)


class TestReporting:
    def test_retrieval_is_an_editing_kind_and_versions_pair(self):
        assert "retrieval" in validate.EDITING_KINDS
        assert ("with", "with_prev") in validate.PAIRS
        assert ("with", "with_prev") in report._COMPARISONS

    def test_report_renders_per_task_and_pooled_success(self, tmp_path):
        base = {"kind": "retrieval", "chain": "", "step": 0, "rep": 0, "model": "m",
                "telemetry_ok": None, "error": "", "tokens_total": 1, "cost_usd": 0.0,
                "turns": 1, "tool_calls": 0, "duration_ms": 1, "violations": 0,
                "rationale": 0.0}
        rows = [
            {**base, "task_id": "retr-a", "condition": "with", "success": True},
            {**base, "task_id": "retr-a", "condition": "with_prev", "success": False},
            {**base, "task_id": "retr-b", "condition": "with", "success": True},
            {**base, "task_id": "retr-b", "condition": "with_prev", "success": True},
        ]
        runs = tmp_path / "runs.jsonl"
        runs.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        text = report.render(runs)
        assert "## Decision-dependent tasks" in text
        assert "| retr-a | 0/1" in text and "| **pooled (headline)** | 1/2" in text
        assert "independent" in text  # the pooled cell's clustering caveat
        assert "Δ (with−with_prev)" in text

    def test_errored_runs_are_missing_data_not_a_measured_zero(self, tmp_path):
        base = {"kind": "retrieval", "chain": "", "step": 0, "rep": 0, "model": "m",
                "telemetry_ok": None, "tokens_total": 1, "cost_usd": 0.0, "turns": 1,
                "tool_calls": 0, "duration_ms": 1, "violations": 0, "rationale": 0.0,
                "success": False}
        rows = [
            {**base, "task_id": "retr-a", "condition": "with", "error": "", "success": True},
            {**base, "task_id": "retr-a", "condition": "with_prev", "error": "setup failed"},
            {**base, "task_id": "retr-b", "condition": "with", "error": "", "success": True},
            {**base, "task_id": "retr-b", "condition": "with", "error": "api error", "rep": 1},
            {**base, "task_id": "retr-b", "condition": "with_prev", "error": "", "rep": 0},
        ]
        runs = tmp_path / "runs.jsonl"
        runs.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        table = report.render(runs).split("## Decision-dependent tasks")[1]
        assert "| task | with_prev | with |" in table
        assert "| retr-a | no completed runs (1 errored) | 1/1 (0.21-1.00) |" in table
        assert "| retr-b | 0/1 (0.00-0.79) | 1/1 (0.21-1.00), 1 errored |" in table
        assert "0/0" not in table


class TestDeliveryClassification:
    def test_a_short_rule_shown_as_its_title_counts_as_full(self):
        entry = {"id": "abcd1234ef", "type": "decision",
                 "content": "Keep one-line docstrings.", "title": "Keep one-line docstrings."}
        assert replay_delivery._shown_in_full(entry, "- [convention] Keep one-line docstrings.")

    def test_a_clipped_rule_is_not_full(self):
        entry = {"id": "abcd1234ef", "type": "decision", "title": "Use Postgres",
                 "content": "Use Postgres for the queue, since ordering matters."}
        assert not replay_delivery._shown_in_full(
            entry, "- [convention] Use Postgres (id=abcd1234)")
        assert replay_delivery._shown_in_full(
            entry, "- Use Postgres (id=abcd1234)\n    Use Postgres for the queue, since "
                   "ordering matters.")

    def test_pointer_names_with_or_without_a_title(self):
        line = "[Contexer] 2 more decisions ...: Some title (id=abcd1234); id=ef567890 - call"
        assert replay_delivery._NAMED.findall(line) == ["abcd1234", "ef567890"]


class TestDeliveryPreview:
    @pytest.fixture(scope="class")
    def preview(self):
        return replay_delivery.replay_tasks(TASKS_FILE, SEED)

    def test_preview_reports_every_task_against_this_checkout(self, preview):
        assert preview["summary"]["tasks"] == len(TASKS)
        assert preview["summary"]["seeds_not_stored"] == 0
        assert Path(preview["contexer"]["path"]) == Path(run.__file__).resolve().parents[1]

    def test_current_contexer_names_every_needed_decision(self, preview):
        # #341: an anchored decision that loses a full slot is still named.
        assert preview["summary"]["needed_missing"] == 0
        assert preview["summary"]["needed_full"] >= 3

    def test_preloaded_constraint_is_never_delivered_twice(self, preview):
        # #342: the constraint shown in full at session start.
        seeds = TASKS[0]["seed_decisions"]
        constraint = next(i for i, s in enumerate(seeds) if s["subtype"] == "constraint")
        for row in preview["tasks"]:
            assert row["seeded_ids"][constraint] in row["startup_full_ids"]
            assert row["seeded_ids"][constraint] not in row["repeated_from_startup"]

    def test_no_startup_rule_is_delivered_twice(self, preview):
        # #350: a short convention shown whole at startup is credited too.
        assert preview["summary"]["repeated_from_startup"] == 0

    def test_snapshot_mode_replays_a_frozen_store(self, golden, tmp_path, monkeypatch):
        from contexer import store
        repo = tmp_path / "repo"
        shutil.copytree(golden, repo)
        repo = repo.resolve()
        monkeypatch.setattr(store, "store_dir", lambda: tmp_path / "seed-store")
        stored, entry_id = store.update_decision(
            str(repo), TASKS[0]["seed_decisions"][2]["content"].replace("{seed}", str(SEED)),
            "s", "architecture", created_by="human",
            source_files=[f"app/svc_{SEED}_core.py"])
        assert stored
        snapshot = tmp_path / "snapshot.json"
        snapshot.write_text(json.dumps(store.load(str(repo))))
        prompt = tmp_path / "prompt.txt"
        prompt.write_text(TASKS[0]["prompt"].replace("{seed}", str(SEED)))
        result = replay_delivery.replay_snapshot(snapshot, repo, prompt, None)
        assert result["prompt_full_ids"] == [entry_id[:8]]
        assert result["repeated_from_startup"] == []


STUB = """#!/bin/sh
CM=no; grep -q "RecordInputError" CLAUDE.md 2>/dev/null && CM=yes
cat <<EOF2
{"result": "claudemd-has-seeds:$CM", "usage": {"input_tokens": 10, "output_tokens": 60, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}, "total_cost_usd": 0.001, "num_turns": 1, "duration_ms": 5, "session_id": "stub"}
EOF2
"""


def test_stub_campaign_runs_the_task_file(tmp_path):
    stub = tmp_path / "claude"
    stub.write_text(STUB)
    stub.chmod(0o755)
    out = run.run_campaign(tmp_path / "camp", reps=1, task_ids=["retr-errors"],
                           claude_cmd=str(stub), seed=SEED, model="stub-model",
                           conditions=("without", "claudemd"), wait_for_otel=False,
                           tasks_file=TASKS_FILE)
    rows = {r["condition"]: r for r in map(json.loads, out.read_text().splitlines())}
    assert set(rows) == {"without", "claudemd"}
    assert all(r["kind"] == "retrieval" and r["error"] == "" for r in rows.values())
    assert rows["claudemd"]["result_snippet"] == "claudemd-has-seeds:yes"
    assert rows["without"]["result_snippet"] == "claudemd-has-seeds:no"
    assert not any(r["success"] for r in rows.values())  # the stub writes no code
    meta = json.loads((out.parent / "campaign.json").read_text())
    assert meta["tasks_file"] == str(TASKS_FILE)
    assert meta["tasks_sha256"] == hashlib.sha256(TASKS_FILE.read_bytes()).hexdigest()
    # Re-running the same task file into the same directory keeps appending.
    run.run_campaign(tmp_path / "camp", reps=1, task_ids=["retr-errors"],
                     claude_cmd=str(stub), seed=SEED, model="stub-model",
                     conditions=("without",), wait_for_otel=False, tasks_file=TASKS_FILE)
    assert len(out.read_text().splitlines()) == 3
