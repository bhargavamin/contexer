"""Kit for the capture-loop (H5) chains in benchmarks/capture_tasks.json: every check judges a
correct and a plausible-but-wrong implementation correctly, the untouched fixture never passes
session 2, session 2's prompt carries no hint of the rule, and the between-session capture
measurement reads both the Contexer store and the maintained CLAUDE.md."""
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from benchmarks import run
from benchmarks.fixtures.generate import build_webapi

SEED = 7
PY = 'uv run python -c "'
TASKS_FILE = Path(__file__).resolve().parent.parent / "benchmarks" / "capture_tasks.json"
TASKS = json.loads(TASKS_FILE.read_text())
BY_ID = {t["id"]: t for t in TASKS}

# chain -> (session-1 compliant, session-2 compliant, session-2 plausible wrong)
PATCHES = {
    "cents": (
        "def order_total(prices):\n    return {'amount_cents': round(sum(prices) * 100)}\n",
        "def refund_amount(total_cents, percent):\n"
        "    return {'amount_cents': total_cents * percent // 100}\n",
        "def refund_amount(total_cents, percent):\n"
        "    return {'refund_cents': total_cents * percent // 100}\n"),
    "recprefix": (
        "def record_ref(record_id):\n    return {'ref': f'rec_{record_id}'}\n",
        "def link_records(parent_id, child_id):\n"
        "    return {'parent': f'rec_{parent_id}', 'child': f'rec_{child_id}'}\n",
        "def link_records(parent_id, child_id):\n"
        "    return {'parent_id': parent_id, 'child_id': child_id}\n"),
    "millis": (
        "import time as _t\ndef timed_fetch(record_id):\n    start = _t.perf_counter()\n"
        "    rec = fetch_record_7_0(record_id)\n"
        "    return {'record': rec, 'took_ms': int((_t.perf_counter() - start) * 1000)}\n",
        "def backoff_delay(attempt):\n    return {'delay_ms': int(500 * 2 ** (attempt - 1))}\n",
        "def backoff_delay(attempt):\n    return {'delay_seconds': 0.5 * 2 ** (attempt - 1)}\n"),
    "errdict": (
        "def parse_slot(text):\n    if text.startswith('slot-') and text[5:].isdigit():\n"
        "        return {'ok': True, 'slot': int(text[5:])}\n"
        "    return {'ok': False, 'error': 'bad_slot'}\n",
        "def parse_record_number(text):\n    if text.startswith('R-') and text[2:].isdigit():\n"
        "        return {'ok': True, 'number': int(text[2:])}\n"
        "    return {'ok': False, 'error': 'bad_record'}\n",
        "def parse_record_number(text):\n    if text.startswith('R-') and text[2:].isdigit():\n"
        "        return {'number': int(text[2:])}\n    raise ValueError(text)\n"),
    "paging": (
        "def list_slot_numbers(offset=0, page_size=25):\n    page_size = min(page_size, 100)\n"
        "    return list(range(25))[offset:offset + page_size]\n",
        "def list_recent_ids(record_ids, offset=0, page_size=25):\n"
        "    page_size = min(page_size, 100)\n    return record_ids[offset:offset + page_size]\n",
        "def list_recent_ids(record_ids, page=1, per_page=20):\n"
        "    start = (page - 1) * per_page\n    return record_ids[start:start + per_page]\n"),
    "newest": (
        "def records_for(ids):\n"
        "    return sorted((fetch_record_7_0(i) for i in ids), key=lambda r: r['id'], reverse=True)\n",
        "def records_in_slot(ids, slot):\n    fetch = globals()[f'fetch_record_7_{slot}']\n"
        "    return sorted((fetch(i) for i in ids), key=lambda r: r['id'], reverse=True)\n",
        "def records_in_slot(ids, slot):\n    fetch = globals()[f'fetch_record_7_{slot}']\n"
        "    return [fetch(i) for i in ids]\n"),
    "camel": (
        "def record_summary(record_id):\n"
        "    return {'recordId': record_id, 'slotNumber': 0, 'isPositive': record_id > 0}\n",
        "def slot_overview(slot):\n"
        "    return {'slotNumber': slot, 'fetcherName': f'fetch_record_7_{slot}', 'isFirst': slot == 0}\n",
        "def slot_overview(slot):\n"
        "    return {'slot_number': slot, 'fetcher_name': f'fetch_record_7_{slot}', 'is_first': slot == 0}\n"),
    "ynflags": (
        "def record_flags(record_id):\n"
        "    return {'even': 'Y' if record_id % 2 == 0 else 'N', 'large': 'Y' if record_id > 1000 else 'N'}\n",
        "def slot_status(slot):\n    return {'slot': slot, 'valid': 'Y' if 0 <= slot <= 24 else 'N',"
        " 'reserved': 'Y' if slot == 0 else 'N'}\n",
        "def slot_status(slot):\n    return {'slot': slot, 'valid': 0 <= slot <= 24, 'reserved': slot == 0}\n"),
}


@pytest.fixture(scope="module")
def golden(tmp_path_factory):
    return build_webapi(tmp_path_factory.mktemp("capture") / "golden", seed=SEED)


def _work(golden, tmp_path, extra=""):
    work = tmp_path / "work"
    shutil.copytree(golden, work)
    core = work / "app" / f"svc_{SEED}_core.py"
    core.write_text(core.read_text() + "\n\n" + extra)
    return work


def _passes(cmd, work):
    """Run a check the way the harness does, minus `uv run` (this interpreter is the env)."""
    script = cmd.replace("{seed}", str(SEED))[len(PY):-1]
    return subprocess.run([sys.executable, "-c", script], cwd=work,
                          capture_output=True, text=True, timeout=60).returncode == 0


def test_every_chain_has_two_steps_and_a_kit():
    chains = {t["chain"] for t in TASKS}
    assert len(chains) == 8 and {c.removeprefix("cap-") for c in chains} == set(PATCHES)
    for chain in chains:
        steps = sorted(t["step"] for t in TASKS if t["chain"] == chain)
        assert steps == [1, 2], chain
    assert all(t["capture_terms"] and not t["seed_decisions"] for t in TASKS)


@pytest.mark.parametrize("chain", sorted(PATCHES))
def test_session_one_check_accepts_the_stated_rule(golden, tmp_path, chain):
    task = BY_ID[f"cap-{chain}-1"]
    assert _passes(task["check_cmd"], _work(golden, tmp_path, PATCHES[chain][0]))


@pytest.mark.parametrize("chain", sorted(PATCHES))
def test_session_two_checks_discriminate(golden, tmp_path, chain):
    task = BY_ID[f"cap-{chain}-2"]
    good, wrong = PATCHES[chain][1], PATCHES[chain][2]
    work = _work(golden, tmp_path / "good", good)
    assert _passes(task["check_cmd"], work) and _passes(task["functional_cmd"], work)
    work = _work(golden, tmp_path / "wrong", wrong)
    assert not _passes(task["check_cmd"], work), "the decision-ignorant version must fail adherence"
    assert _passes(task["functional_cmd"], work), "...while still working"
    assert not _passes(task["check_cmd"], _work(golden, tmp_path / "bare"))


@pytest.mark.parametrize("chain", sorted(PATCHES))
def test_rule_is_stated_in_session_one_and_never_hinted_in_session_two(chain):
    terms = BY_ID[f"cap-{chain}-1"]["capture_terms"]
    assert any(re.search(p, BY_ID[f"cap-{chain}-1"]["prompt"], re.IGNORECASE) for p in terms)
    assert not any(re.search(p, BY_ID[f"cap-{chain}-2"]["prompt"], re.IGNORECASE) for p in terms)


def test_capture_state_reads_the_contexer_store(tmp_path):
    store = tmp_path / ".contexer"
    store.mkdir()
    entries = [{"id": "a1", "type": "decision", "status": "suggested",
                "title": "Keep money in cents", "content": "Store money as amount_cents integers."},
               {"id": "b2", "type": "decision", "status": "pending_approval",
                "content": "Unrelated rule about naming."}]
    (store / "repo-abc.json").write_text(json.dumps({"entries": entries}))
    # Sidecars that aren't stores must be skipped, whatever their JSON shape (a list once
    # crashed this read mid-campaign and skipped the code revert for that chain).
    (store / ".outbox.json").write_text(json.dumps([{"entries": "x"}]))
    (store / ".retrieval_index_repo-abc.json").write_text(json.dumps({"docs": {}}))
    (store / "odd.json").write_text(json.dumps(["not", "a", "store"]))
    state = run._capture_state("with", tmp_path, tmp_path, [r"amount_cents"])
    assert state == {"captured": True, "capture_status": "suggested",
                     "decisions_stored": 2, "decisions_pending": 1}
    # A rule recorded globally reaches every repository, so it counts as captured.
    (store / "_global.json").write_text(json.dumps({"entries": [
        {"id": "g", "type": "decision", "status": "approved", "content": "Use rec_ prefixes"}]}))
    assert run._capture_state("with", tmp_path, tmp_path, [r"rec_"])["captured"] is True


def test_capture_state_reads_only_the_maintained_decisions_section(tmp_path):
    run._maintained_setup(tmp_path)
    terms = [r"amount_cents"]
    assert run._capture_state("claudemd_maintained", tmp_path, tmp_path, terms) == {"captured": False}
    claude_md = tmp_path / "CLAUDE.md"
    claude_md.write_text(claude_md.read_text() + "- Money is amount_cents integers (reconciliation).\n")
    assert run._capture_state("claudemd_maintained", tmp_path, tmp_path, terms) == {"captured": True}
    # A rule written elsewhere in the file (or no file) is not a maintained decision.
    claude_md.write_text("# Notes\n\namount_cents\n")
    assert run._capture_state("claudemd_maintained", tmp_path, tmp_path, terms) == {"captured": False}
    assert run._capture_state("without", tmp_path, tmp_path, terms) == {}


STUB = r"""#!/bin/sh
case "$2" in
  *order_total*)
    printf '\n\ndef order_total(prices):\n    return {"amount_cents": round(sum(prices) * 100)}\n' >> app/svc_7_core.py
    if [ -f CLAUDE.md ]; then echo '- Money is integer amount_cents (reconciliation).' >> CLAUDE.md; fi ;;
  *refund_amount*)
    if grep -qs amount_cents CLAUDE.md app/svc_7_core.py; then KEY=amount_cents; else KEY=refund_cents; fi
    printf '\n\ndef refund_amount(total_cents, percent):\n    return {"%s": total_cents * percent // 100}\n' "$KEY" >> app/svc_7_core.py ;;
esac
echo '{"result": "stub", "usage": {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}, "total_cost_usd": 0, "num_turns": 1, "duration_ms": 1, "session_id": "stub"}'
"""


def test_stub_campaign_runs_the_loop_and_measures_capture(tmp_path):
    # End to end through the runner: session 1 records the rule (only the maintained arm has a
    # CLAUDE.md to record it in), the capture state lands on session 1's row, and session 2
    # succeeds only where the rule survived between sessions. The stub's session 2 also copies
    # the rule from session 1's code if it's still there, so `without` failing proves the code is
    # reverted between sessions.
    stub = tmp_path / "claude"
    stub.write_text(STUB)
    stub.chmod(0o755)
    out = run.run_campaign(tmp_path / "camp", reps=1, task_ids=["cap-cents-1", "cap-cents-2"],
                           claude_cmd=str(stub), seed=SEED, model="stub-model",
                           conditions=("without", "claudemd_maintained"), wait_for_otel=False,
                           tasks_file=TASKS_FILE)
    rows = {(r["condition"], r["step"]): r for r in map(json.loads, out.read_text().splitlines())}
    assert all(not r["error"] for r in rows.values()), rows
    assert rows[("claudemd_maintained", 1)]["captured"] is True
    assert "captured" not in rows[("without", 1)]
    assert rows[("claudemd_maintained", 2)]["success"] is True
    assert rows[("without", 2)]["success"] is False
    assert rows[("without", 2)]["functional"] is True


# Counterexamples from the blind review (2026-10-04): each must be judged the way it says.
# (chain, step-2 patch, adherence expected, functional expected)
REVIEW_CASES = [
    ("camel", "def slot_overview(slot):\n    return {'slot': slot, 'fetcher': f'fetch_record_7_{slot}', 'first': slot == 0}\n",
     True, True),     # one-word keys are valid camelCase
    ("camel", "def slot_overview(slot):\n    return {'SlotNumber': slot, 'FetcherName': 'x', 'IsFirst': slot == 0}\n",
     False, True),    # PascalCase is not camelCase
    ("camel", "def slot_overview(slot):\n    return {'slot': {'number': slot}, 'fetcherName': 'x'}\n",
     True, True),     # nested working result
    ("paging", "def list_recent_ids(record_ids, offset=0, page_size=25):\n    return record_ids[:min(page_size, 100)]\n",
     False, True),    # ignores offset
    ("paging", "def list_recent_ids(record_ids, offset=0, page_size=25):\n"
     "    return record_ids[offset:offset + min(page_size, 50)]\n", False, True),   # wrong cap
    ("paging", "def list_recent_ids(record_ids, offset=0, page_size=25):\n"
     "    return {'items': record_ids[offset:offset + min(page_size, 100)], 'next_offset': offset + page_size}\n",
     True, True),     # a page object honouring offset, default and cap follows the rule
    ("millis", "def backoff_delay(attempt):\n    d = 0.5 * 2 ** (attempt - 1)\n"
     "    return {'delay_seconds': d, 'delay_ms': int(d * 1000)}\n", False, True),  # seconds decoy
    ("errdict", "def parse_record_number(text):\n    if text.startswith('R-') and text[2:].isdigit():\n"
     "        return {'ok': True, 'number': int(text[2:])}\n"
     "    return {'ok': False, 'error': 'Something went terribly wrong while parsing the input text'}\n",
     False, True),    # not a short code
    ("errdict", "def parse_record_number(text):\n    if text.startswith('R-') and text[2:].isdigit():\n"
     "        return {'ok': True, 'result': {'number': int(text[2:])}}\n"
     "    return {'ok': False, 'error': 'bad_record'}\n", True, True),   # nested success payload is allowed
    ("cents", "def refund_amount(total_cents, percent):\n    return {'refund': {'cents': total_cents * percent // 100}}\n",
     False, True),    # nested working result
    ("recprefix", "def link_records(parent_id, child_id):\n    return {'link': [f'rec_{parent_id}', f'rec_{child_id}']}\n",
     True, True),     # one key, still a working link
    ("ynflags", "def slot_status(slot):\n"
     "    return {'slot': slot, 'valid': 1 if 0 <= slot <= 24 else 0, 'reserved': 1 if slot == 0 else 0}\n",
     False, True),    # 0/1 flags work but aren't Y/N
    ("ynflags", "def slot_status(slot):\n    return {'arbitrary_a': 'Y', 'arbitrary_b': 'N', 'slot': slot}\n",
     False, False),   # decoy Y/N values report neither flag
    ("ynflags", "def slot_status(slot):\n    return {'slot': slot, 'valid': 'Y', 'reserved': 'N'}\n",
     False, False),   # Y/N, but the wrong answers
    ("recprefix", "def link_records(parent_id, child_id):\n"
     "    return {f'rec_{parent_id}': parent_id, f'rec_{child_id}': child_id}\n",
     False, True),    # prefixed keys, integer values: the outbound ids are still unprefixed
]


@pytest.mark.parametrize(("chain", "patch", "adherence", "functional"), REVIEW_CASES,
                         ids=[f"{c[0]}-{i}" for i, c in enumerate(REVIEW_CASES)])
def test_review_counterexamples(golden, tmp_path, chain, patch, adherence, functional):
    task = BY_ID[f"cap-{chain}-2"]
    work = _work(golden, tmp_path, patch)
    assert _passes(task["check_cmd"], work) is adherence
    assert _passes(task["functional_cmd"], work) is functional


@pytest.mark.parametrize(("chain", "paraphrase"), [
    ("cents", "Represent monetary values as whole minor currency units rather than floating point."),
    ("recprefix", "Outbound record references use the gateway's canonical record-prefixed string form."),
    ("newest", "Return record collections from largest ID to smallest ID."),
    ("paging", "Pagination uses offset and page size, default 25 with a maximum of 100."),
    ("millis", "Duration fields contain integer ms values and their names use the ms suffix."),
    ("ynflags", "Encode flags as the mainframe's single-character affirmative/negative markers rather than native booleans."),
    ("camel", "All returned mapping keys use lowerCamel naming."),
    ("errdict", "Invalid input is returned as a structured error result instead of throwing an exception."),
])
def test_capture_patterns_recognise_the_reviewers_paraphrases(chain, paraphrase):
    terms = BY_ID[f"cap-{chain}-1"]["capture_terms"]
    assert any(re.search(p, paraphrase, re.IGNORECASE) for p in terms), (chain, paraphrase)


def test_matching_ignores_the_runs_own_path_and_chain_name(tmp_path):
    # The first smoke run's work dir, w-cap-cents-with-0, matched the cents rule in hook output.
    work = tmp_path / "w-cap-cents-with-0"
    noise = run._run_noise(work, {"chain": "cap-cents"})
    text = f"Context for {work}: Python requirement is >=3.12."
    assert run._matches([r"\bcents\b"], text) is True
    assert run._matches([r"\bcents\b"], text, noise) is False
    assert run._matches([r"\bcents\b"], text + " Keep money in cents.", noise) is True


def test_validator_accepts_two_step_chains_and_flags_a_missing_step():
    from benchmarks import validate
    rows = [{"chain": "cap-x", "step": s, "condition": c}
            for c in ("without", "with") for s in (1, 2)]
    failures = []
    validate._check_chains(rows, failures)
    assert failures == []
    failures = []
    validate._check_chains([r for r in rows if not (r["condition"] == "with" and r["step"] == 2)],
                           failures)
    assert failures == ["chain 'cap-x' condition 'with' missing step(s): [2]"]


def test_session_two_delivery_ignores_session_ones_transcript(tmp_path):
    # Chain steps share a HOME; session 1's capture acknowledgement repeats the rule.
    projects = tmp_path / ".claude" / "projects" / "p"
    projects.mkdir(parents=True)
    ack = {"attachment": {"type": "hook_additional_context",
                          "content": "Auto-stored as constraint: money is amount_cents"}}
    (projects / "session1.jsonl").write_text(json.dumps(ack) + "\n")
    earlier = run._transcripts(tmp_path)
    (projects / "session2.jsonl").write_text(json.dumps({"type": "user"}) + "\n")
    text, _ = run._contexer_received(tmp_path, earlier)
    assert "amount_cents" not in text
    assert "amount_cents" in run._contexer_received(tmp_path)[0]


def _git(work, *args):
    subprocess.run(["git", "-C", str(work), *args], check=True, capture_output=True)


@pytest.mark.parametrize("condition", ["without", "with", "claudemd_maintained"])
def test_revert_keeps_only_the_arms_own_memory(golden, tmp_path, condition):
    work = tmp_path / "w"
    shutil.copytree(golden, work)
    base = subprocess.run(["git", "-C", str(work), "rev-parse", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    if condition == "claudemd_maintained":
        run._maintained_setup(work)
    (work / "app" / f"svc_{SEED}_core.py").write_text("amount_cents = 1\n")
    (work / "CLAUDE.md").write_text((work / "CLAUDE.md").read_text() + "- amount_cents\n"
                                    if (work / "CLAUDE.md").exists() else "- amount_cents\n")
    (work / "NOTES.md").write_text("amount_cents\n")
    (work / ".venv").mkdir(exist_ok=True)
    (work / ".venv" / "marker").write_text("keep")
    _git(work, "add", "app")
    run._revert_code_keep_memory(work, base, condition)
    assert "amount_cents" not in (work / "app" / f"svc_{SEED}_core.py").read_text()
    assert not (work / "NOTES.md").exists()
    assert (work / ".venv" / "marker").exists()
    kept = (work / "CLAUDE.md").read_text() if (work / "CLAUDE.md").exists() else ""
    assert ("amount_cents" in kept) is (condition == "claudemd_maintained")


def test_a_failed_revert_raises(golden, tmp_path):
    work = tmp_path / "w"
    shutil.copytree(golden, work)
    with pytest.raises(RuntimeError, match="capture revert failed"):
        run._revert_code_keep_memory(work, "0" * 40, "without")


def test_capture_report_counts_each_arm_and_stage():
    from benchmarks import capture_report

    def row(cond, step, **extra):
        return {"kind": "capture", "chain": "cap-x", "condition": cond, "rep": 0, "step": step,
                "error": "", **extra}
    rows = [row("without", 1), row("without", 2, success=False),
            row("claudemd_maintained", 1, captured=True),
            row("claudemd_maintained", 2, success=True),
            row("with", 1, captured=True, capture_status="pending_approval"),
            row("with", 2, success=False, needed_delivery="none")]
    s = capture_report.summarize(rows)
    assert s["arms"]["without"] == {"runs": 1, "captured": None, "success": 0}
    assert s["arms"]["claudemd_maintained"] == {"runs": 1, "captured": 1, "success": 1}
    assert s["funnel"] == {("captured, pending review", False): 1}
    assert "| with | 1 | 1/1 | 0/1 |" in capture_report.render(s)


def test_validator_requires_capture_measurements_and_task_file_length(tmp_path):
    from benchmarks import validate
    failures = []
    validate._check_capture_fields([{"kind": "capture", "task_id": "cap-x-1", "step": 1,
                                     "condition": "with", "rep": 0}], failures)
    assert failures == ["capture row without 'captured': cap-x-1 with rep 0"]
    # A final step missing from every arm is caught when the task file says the chain has two.
    tasks = tmp_path / "tasks.json"
    tasks.write_text(json.dumps([{"chain": "cap-x", "step": 1}, {"chain": "cap-x", "step": 2}]))
    failures = []
    validate._check_chains([{"chain": "cap-x", "step": 1, "condition": "with"}], failures,
                           validate._chain_lengths({"tasks_file": str(tasks)}))
    assert failures == ["chain 'cap-x' condition 'with' missing step(s): [2]"]


def test_an_errored_step_stops_its_chain(tmp_path):
    stub = tmp_path / "claude"
    stub.write_text("#!/bin/sh\necho 'not json'\n")
    stub.chmod(0o755)
    out = run.run_campaign(tmp_path / "camp", reps=1, task_ids=["cap-cents-1", "cap-cents-2"],
                           claude_cmd=str(stub), seed=SEED, model="stub-model",
                           conditions=("without",), wait_for_otel=False, tasks_file=TASKS_FILE)
    rows = [json.loads(line) for line in out.read_text().splitlines()]
    assert [r["step"] for r in rows] == [1] and rows[0]["error"]
