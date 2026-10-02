"""Adoption evaluation, phase 0a slice: benchmarks/adoption_tasks.json, its two checks per
task (adherence: followed the needed decision; functional: the change works), fixture overlays,
steady-state setup and the docs_indexed arm. Runbook: benchmarks/ADOPTION_CAMPAIGN.md."""
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks import run, seeding
from benchmarks.fixtures.generate import apply_overlay, build_webapi

TASKS_FILE = Path(__file__).resolve().parent.parent / "benchmarks" / "adoption_tasks.json"
TASKS = json.loads(TASKS_FILE.read_text())
BY_ID = {t["id"]: t for t in TASKS}
SEED = 7
PY = 'uv run python -c "'

K5_CLASS = "\nclass RecordUnavailable(Exception):\n    pass\n"
K4_GOOD = ("\ndef fetch_records_batch(record_ids):\n    out = {'items': [], 'failed': []}\n"
           "    for r in record_ids:\n        if r < 1:\n            out['failed'].append(r)\n"
           "        else:\n            out['items'].append(fetch_record_7_0(r))\n    return out\n")
K4_COPY = "\ndef fetch_records_batch(record_ids):\n    return fetch_records_legacy(record_ids)\n"
K4_BREAK_LEGACY = "\ndef fetch_records_legacy(record_ids):\n    return []\n"
K5_RETRY = (K5_CLASS + "\ndef fetch_record_7_3(record_id):\n    for _ in range(3):\n        try:\n"
            "            return _upstream_get(record_id, 3)\n        except UpstreamTimeout:\n"
            "            continue\n")

# (task, label, code appended to app/svc_7_core.py, adherence passes, functional passes).
# Every implementation task has the four cases the plan requires; adherence None means the
# task has no needed decision. A broken-but-compliant patch must never count as a success.
KIT = [
    ("adopt-k1-pattern", "compliant",
     '\ndef fetch_record_7_25(record_id: int) -> dict:\n    """Fetches record 25 for service 7."""\n'
     '    return {"id": record_id, "slot": 25}\n', True, True),
    # A decision-aware agent may also apply decision 10 (validate ids as fetchers are touched)
    # to tasks it isn't graded on; doing so must never cost a pass.
    ("adopt-k1-pattern", "compliant-plus-id-validation",
     "\nclass RecordInputError(ValueError):\n    pass\n"
     "def fetch_record_7_25(record_id: int) -> dict:\n"
     '    """Fetches record 25 for service 7."""\n    if record_id < 1:\n'
     "        raise RecordInputError(record_id)\n"
     '    return {"id": record_id, "slot": 25}\n', True, True),
    ("adopt-k1-pattern", "works-but-off-pattern",
     '\ndef fetch_record_7_25(record_id):\n    return {"id": record_id, "slot": 25}\n', False, True),
    ("adopt-k1-pattern", "on-pattern-but-broken",
     '\ndef fetch_record_7_25(record_id: int) -> dict:\n    """Fetches record 25 for service 7."""\n'
     '    return {"id": record_id, "slot": 24}\n', True, False),
    ("adopt-k1-pattern", "off-pattern-and-broken",
     '\ndef fetch_record_7_25(record_id):\n    return {"id": record_id, "slot": 24}\n', False, False),

    ("adopt-k2-errors", "compliant",
     "\nclass RecordInputError(ValueError):\n    pass\n_orig = fetch_record_7_1\n"
     "def fetch_record_7_1(record_id: int) -> dict:\n    if record_id < 1:\n"
     "        raise RecordInputError(record_id)\n    return _orig(record_id)\n", True, True),
    ("adopt-k2-errors", "plain-valueerror",
     "\n_orig = fetch_record_7_1\ndef fetch_record_7_1(record_id):\n    if record_id < 1:\n"
     "        raise ValueError(record_id)\n    return _orig(record_id)\n", False, True),
    ("adopt-k2-errors", "rejects-every-id",
     "\nclass RecordInputError(ValueError):\n    pass\ndef fetch_record_7_1(record_id):\n"
     "    raise RecordInputError(record_id)\n", True, False),
    ("adopt-k2-errors", "returns-none",
     "\n_orig = fetch_record_7_1\ndef fetch_record_7_1(record_id):\n    if record_id < 1:\n"
     "        return None\n    return _orig(record_id)\n", False, False),

    ("adopt-k3-cache", "compliant",
     "\nimport functools\nfetch_record_7_0 = functools.lru_cache(maxsize=256)(fetch_record_7_0)\n",
     True, True),
    ("adopt-k3-cache", "compliant-plus-id-validation",
     "\nimport functools\nclass RecordInputError(ValueError):\n    pass\n"
     "@functools.lru_cache(maxsize=256)\ndef fetch_record_7_0(record_id: int) -> dict:\n"
     '    """Fetches record 0 for service 7."""\n    if record_id < 1:\n'
     "        raise RecordInputError(record_id)\n"
     "    return _load_record(record_id, 0)\n", True, True),
    ("adopt-k3-cache", "bounded-helper-returning-copies",
     "\nimport functools\n@functools.lru_cache(maxsize=256)\ndef _cached_0(record_id):\n"
     "    return _load_record(record_id, 0)\ndef fetch_record_7_0(record_id):\n"
     "    return dict(_cached_0(record_id))\n", True, True),
    ("adopt-k3-cache", "unbounded-helper-returning-copies",
     "\nimport functools\n@functools.lru_cache(maxsize=None)\ndef _cached_0(record_id):\n"
     "    return _load_record(record_id, 0)\ndef fetch_record_7_0(record_id):\n"
     "    return dict(_cached_0(record_id))\n", False, True),
    ("adopt-k3-cache", "unbounded-lru",
     "\nimport functools\nfetch_record_7_0 = functools.lru_cache(maxsize=None)(fetch_record_7_0)\n",
     False, True),
    ("adopt-k3-cache", "hand-rolled-dict",
     "\n_cache = {}\n_orig = fetch_record_7_0\ndef fetch_record_7_0(record_id):\n"
     "    if record_id not in _cache:\n        _cache[record_id] = _orig(record_id)\n"
     "    return _cache[record_id]\n", False, True),
    # A correct cache that rebuilds a fresh dict per call (no shared mutable record, no
    # cache_info): the load count is the only sign of caching, and it must pass functional.
    ("adopt-k3-cache", "hand-rolled-fresh-dicts",
     "\n_rows = {}\ndef fetch_record_7_0(record_id):\n    if record_id not in _rows:\n"
     "        rec = _load_record(record_id, 0)\n        _rows[record_id] = (rec['id'], rec['slot'])\n"
     "    rid, slot = _rows[record_id]\n    return {'id': rid, 'slot': slot}\n", False, True),
    ("adopt-k3-cache", "bounded-but-wrong-function",
     "\nimport functools\nfetch_record_7_1 = functools.lru_cache(maxsize=256)(fetch_record_7_1)\n",
     True, False),
    ("adopt-k3-cache", "uncached-and-wrong",
     "\ndef fetch_record_7_0(record_id):\n    return {'id': record_id + 1, 'slot': 0}\n", False, False),

    ("adopt-k3-audit", "compliant",
     "\nimport time as _t\ndef record_audit_entry(record_id, action):\n"
     "    return {'record_id': record_id, 'action': action, 'at_ms': int(_t.time() * 1000)}\n",
     True, True),
    ("adopt-k3-audit", "iso-timestamp",
     "\nimport datetime as _dt\ndef record_audit_entry(record_id, action):\n"
     "    return {'record_id': record_id, 'action': action,\n"
     "            'timestamp': _dt.datetime.now(_dt.timezone.utc).isoformat()}\n", False, True),
    ("adopt-k3-audit", "at-ms-but-no-record",
     "\nimport time as _t\ndef record_audit_entry(record_id, action):\n"
     "    return {'action': action, 'at_ms': int(_t.time() * 1000), 'source': 'svc'}\n", True, False),
    ("adopt-k3-audit", "action-only",
     "\ndef record_audit_entry(record_id, action):\n    return {'action': action}\n", False, False),

    ("adopt-k4-batch-legacy", "compliant", K4_GOOD, True, True),
    ("adopt-k4-batch-legacy", "copies-legacy", K4_COPY, False, True),
    ("adopt-k4-batch-legacy", "compliant-but-breaks-legacy", K4_GOOD + K4_BREAK_LEGACY, True, False),
    ("adopt-k4-batch-legacy", "copies-and-breaks-legacy", K4_COPY + K4_BREAK_LEGACY, False, False),

    ("adopt-k5-no-retry", "compliant",
     K5_CLASS + "\ndef fetch_record_7_3(record_id: int) -> dict:\n    try:\n"
     "        return _upstream_get(record_id, 3)\n    except UpstreamTimeout as exc:\n"
     "        raise RecordUnavailable(record_id) from exc\n", True, True),
    ("adopt-k5-no-retry", "compliant-plus-id-validation",
     K5_CLASS + "\nclass RecordInputError(ValueError):\n    pass\n"
     "def fetch_record_7_3(record_id: int) -> dict:\n    if record_id < 1:\n"
     "        raise RecordInputError(record_id)\n    try:\n"
     "        return _upstream_get(record_id, 3)\n    except UpstreamTimeout as exc:\n"
     "        raise RecordUnavailable(record_id) from exc\n", True, True),
    ("adopt-k5-no-retry", "retries-then-raises",
     K5_RETRY + "    raise RecordUnavailable(record_id)\n", False, True),
    ("adopt-k5-no-retry", "one-call-but-wrong-record",
     K5_CLASS + "\ndef fetch_record_7_3(record_id):\n    try:\n        _upstream_get(record_id, 3)\n"
     "        return {'id': record_id, 'slot': 0}\n    except UpstreamTimeout as exc:\n"
     "        raise RecordUnavailable(record_id) from exc\n", True, False),
    ("adopt-k5-no-retry", "retries-then-swallows",
     K5_RETRY + "    return None\n", False, False),

    ("adopt-k6-slots", "correct",
     "\nimport re as _re\ndef list_slots():\n"
     "    return sorted(int(n.rsplit('_', 1)[1]) for n in list(globals())\n"
     "                  if _re.fullmatch(r'fetch_record_7_\\d+', n))\n", None, True),
    ("adopt-k6-slots", "off-by-one", "\ndef list_slots():\n    return list(range(24))\n", None, False),
]


@pytest.fixture(scope="module")
def golden(tmp_path_factory):
    return build_webapi(tmp_path_factory.mktemp("adoption") / "golden", seed=SEED)


def _work(task, golden, tmp_path, extra=""):
    work = tmp_path / "work"
    shutil.copytree(golden, work)
    apply_overlay(work, task["fixture_files"], SEED)
    core = work / "app" / f"svc_{SEED}_core.py"
    core.write_text(core.read_text() + extra)
    return work


def _passes(cmd, work):
    """Run a check the way the harness does, minus `uv run` (this interpreter is the env)."""
    if not cmd:
        return None
    script = cmd.replace("{seed}", str(SEED))[len(PY):-1]
    return subprocess.run([sys.executable, "-c", script], cwd=work,
                          capture_output=True, text=True, timeout=60).returncode == 0


class TestTaskFile:
    def test_every_class_in_the_slice_is_present_once_or_more(self):
        assert {t["class"] for t in TASKS} == {"K1", "K2", "K3", "K4", "K5", "K6"}
        assert len(BY_ID) == len(TASKS)

    def test_shape_matches_the_plan(self):
        for t in TASKS:
            assert t["functional_cmd"].startswith(PY), t["id"]
            assert t["seed_decisions"] == TASKS[0]["seed_decisions"]  # one shared store
            assert t["store_order_seed"] == TASKS[0]["store_order_seed"]
            # Decisions that apply beyond what is graded, recorded so their cost can be read.
            assert all(0 <= i < len(t["seed_decisions"]) and i != t.get("needed_decision")
                       for i in t["secondary_decisions"]), t["id"]
            needs = t["class"] in ("K2", "K3", "K4", "K5")
            assert ("needed_decision" in t) is needs, t["id"]
            if t["class"] == "K6":
                assert t["check_cmd"] == "", t["id"]  # no decision bears on it
            else:
                assert t["check_cmd"].startswith(PY), t["id"]
            if t["class"] in ("K3", "K4", "K5"):
                assert t["tag"] in ("convention", "judgment") and t["plausible_wrong"], t["id"]
            if t["class"] in ("K4", "K5"):
                assert t["tag"] == "judgment", t["id"]

    def test_needed_decisions_come_in_every_author_style(self):
        for t in TASKS:
            if "needed_decision" in t:
                variants = t["seed_decisions"][t["needed_decision"]].get("variants") or []
                assert [v["style"] for v in variants] == ["terse", "narrative", "plan"], t["id"]

    @pytest.mark.parametrize("task", TASKS, ids=list(BY_ID))
    def test_prompt_does_not_carry_the_decision(self, task):
        for token in ("maxsize=256", "at_ms", "RecordInputError", "'items'", "retry", "retries",
                      "partial result"):
            assert token not in task["prompt"], (task["id"], token)


@pytest.mark.parametrize(("task_id", "label", "extra", "adherence", "functional"), KIT,
                         ids=[f"{t}-{label}" for t, label, *_ in KIT])
def test_checks_judge_each_patch(task_id, label, extra, adherence, functional, golden, tmp_path):
    task = BY_ID[task_id]
    work = _work(task, golden, tmp_path, extra)
    assert _passes(task["check_cmd"], work) is adherence, "adherence"
    assert _passes(task["functional_cmd"], work) is functional, "functional"


def test_every_task_has_the_four_kit_cases():
    for task_id, task in BY_ID.items():
        cases = {(a, f) for tid, _, _, a, f in KIT if tid == task_id}
        if task["check_cmd"]:
            assert cases >= {(True, True), (False, True), (True, False), (False, False)}, task_id
        else:
            assert cases == {(None, True), (None, False)}, task_id


@pytest.mark.parametrize("task", TASKS, ids=list(BY_ID))
def test_the_untouched_fixture_never_succeeds(task, golden, tmp_path):
    work = _work(task, golden, tmp_path)
    adherence, functional = _passes(task["check_cmd"], work), _passes(task["functional_cmd"], work)
    assert functional is False or adherence is False
    # Adherence alone may pass an untouched fixture only for a decision that forbids an action
    # (K5: "don't retry"); everywhere else it must measure something the agent did.
    if task["check_cmd"] and not task["forbids_action"]:
        assert adherence is False


class TestOverlay:
    def test_write_append_replace_then_commit(self, golden, tmp_path):
        work = tmp_path / "w"
        shutil.copytree(golden, work)
        apply_overlay(work, [{"path": "README.md", "content": "svc {seed}\n"},
                             {"path": "app/svc_{seed}_core.py", "append": "\n# tail {seed}\n"},
                             {"path": "app/svc_{seed}_core.py", "old": "slot\": 3}",
                              "new": "slot\": 3}  # three"}], SEED)
        core = (work / "app" / f"svc_{SEED}_core.py").read_text()
        assert (work / "README.md").read_text() == f"svc {SEED}\n"
        assert core.endswith(f"\n# tail {SEED}\n") and '"slot": 3}  # three' in core
        status = subprocess.run(["git", "status", "--porcelain"], cwd=work,
                                capture_output=True, text=True).stdout
        assert status == ""  # committed, so a session's diff starts after the overlay

    def test_a_replace_that_does_not_match_exactly_once_fails(self, golden, tmp_path):
        work = tmp_path / "w"
        shutil.copytree(golden, work)
        with pytest.raises(ValueError, match="exactly once"):
            apply_overlay(work, [{"path": "app/svc_{seed}_core.py", "old": "def ", "new": "x"}],
                          SEED)


def test_docs_indexed_holds_every_decision_once(golden, tmp_path):
    work = tmp_path / "w"
    shutil.copytree(golden, work)
    items = seeding.seed_items(TASKS[0], SEED)
    run._docs_indexed_setup(work, items)
    index = (work / "CLAUDE.md").read_text()
    records = sorted((work / "docs" / "decisions").glob("*.md"))
    assert len(records) == len(items)
    for item, record in zip(items, records):
        assert f"](docs/decisions/{record.name})" in index
        assert run._decision_title(item) in index
        assert item["content"] in record.read_text()


class TestSteadyState:
    def _setup_code(self, tmp_path, monkeypatch, steady):
        commands = []
        monkeypatch.setattr(run.subprocess, "run", lambda args, **kw: commands.append(args))
        repo = tmp_path / "repo"
        run._condition_b_setup(str(repo), tmp_path / "home", "", steady=steady,
                               seed_decisions=seeding.seed_items(BY_ID["adopt-k5-no-retry"], SEED))
        return repo, commands[-1][-1]

    def _exec_in_sandbox(self, code, repo, golden, tmp_path, monkeypatch):
        from contexer import store
        shutil.copytree(golden, repo)
        apply_overlay(repo, BY_ID["adopt-k5-no-retry"]["fixture_files"], SEED)
        monkeypatch.setattr(store, "store_dir", lambda: tmp_path / "store")
        exec(compile(code, "<setup>", "exec"), {})
        return store

    def test_steady_setup_leaves_the_bootstrap_prompt_silent(self, golden, tmp_path, monkeypatch):
        repo, code = self._setup_code(tmp_path, monkeypatch, steady=True)
        monkeypatch.undo()
        store = self._exec_in_sandbox(code, repo, golden, tmp_path, monkeypatch)
        assert store.bootstrap_prompt_payload(str(repo), "Add a function") == {
            "status": "", "context": ""}

    def test_the_steady_check_fails_when_bootstrap_is_unfinished(self, golden, tmp_path,
                                                                 monkeypatch):
        # Mutation check: the same setup without finishing bootstrap must trip the gate.
        repo, code = self._setup_code(tmp_path, monkeypatch, steady=False)
        monkeypatch.undo()
        code += seeding.steady_check_script(str(repo))
        with pytest.raises(AssertionError, match="bootstrap prompt still due"):
            self._exec_in_sandbox(code, repo, golden, tmp_path, monkeypatch)


STUB = """#!/bin/sh
echo '{"result": "stub", "usage": {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}, "total_cost_usd": 0, "num_turns": 1, "duration_ms": 1, "session_id": "stub"}'
"""


def test_stub_campaign_scores_both_checks(tmp_path):
    stub = tmp_path / "claude"
    stub.write_text(STUB)
    stub.chmod(0o755)
    out = run.run_campaign(tmp_path / "camp", reps=1, task_ids=["adopt-k4-batch-legacy"],
                           claude_cmd=str(stub), seed=SEED, model="stub-model",
                           conditions=("without", "docs_indexed"), wait_for_otel=False,
                           tasks_file=TASKS_FILE, steady_state=True)
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r["condition"] for r in rows] == ["without", "docs_indexed"]
    for row in rows:
        assert row["error"] == ""
        # The stub writes no code: fetch_records_batch is missing, so both checks fail.
        assert row["adherence"] is False and row["functional"] is False
        assert row["success"] is False
        assert "[adherence]" in row["check_output"] and "[functional]" in row["check_output"]
    assert json.loads((out.parent / "campaign.json").read_text())["steady_state"] is True


def test_a_session_that_follows_the_decision_but_breaks_the_code_is_not_a_success(tmp_path):
    # The runner's own scoring, end to end: a fake session writes the K4 batch reader exactly as
    # the decision says, but breaks the legacy reader. Adherence passes, functional fails, so
    # the row must not be a success.
    patch = K4_GOOD + K4_BREAK_LEGACY
    stub = tmp_path / "claude"
    stub.write_text(STUB.replace("#!/bin/sh\n", "#!/bin/sh\ncat >> app/svc_7_core.py <<'PATCH'\n"
                                 + patch + "PATCH\n", 1))
    stub.chmod(0o755)
    out = run.run_campaign(tmp_path / "camp", reps=1, task_ids=["adopt-k4-batch-legacy"],
                           claude_cmd=str(stub), seed=SEED, model="stub-model",
                           conditions=("without",), wait_for_otel=False, tasks_file=TASKS_FILE)
    row = json.loads(out.read_text().splitlines()[0])
    assert row["error"] == ""
    assert row["adherence"] is True and row["functional"] is False
    assert row["success"] is False
    assert "[functional]" in row["check_output"] and "[adherence]" not in row["check_output"]


def test_preview_counts_only_tasks_that_need_a_decision():
    from benchmarks import replay_delivery
    report = replay_delivery.replay_tasks(TASKS_FILE, SEED, steady=True)
    s = report["summary"]
    labelled = sum("needed_decision" in t for t in TASKS) * 3
    assert s["tasks"] == labelled and s["unlabelled"] == len(TASKS) * 3 - labelled
    assert set(s["by_style"]) == {"terse", "narrative", "plan"}
    assert s["seeds_not_stored"] == 0 and s["needed_missing"] == 0
