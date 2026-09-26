import json
import re
from pathlib import Path

import pytest

import stop_hook
from conftest import Transcript, commit, payload, run_script, stop, token_of

SCHEMA_MD = Path(__file__).resolve().parent.parent / "schema.md"


def example():
    return json.loads(re.search(r"```json\n(.*?)```", SCHEMA_MD.read_text(), re.S).group(1))


def check(judgment, env, token=None):
    args = ["--check"] + (["--token", token] if token else [])
    return run_script("log_usage.py", args, stdin=json.dumps(judgment), env=env)


@pytest.fixture
def claude_pending(home, repo, tmp_path):
    """A real pending file for a Claude turn with one autofetch id and one get_context id."""
    data_dir, env = home
    t = Transcript(tmp_path / "t.jsonl", "claude")
    stop("claude", env, payload("claude", "L", t.path, repo))
    t.raw({"type": "attachment", "attachment": {"type": "hook_additional_context", "content":
           "[Contexer: auto-fetched for this question] x (id=a1b2c3d4)"}})
    t.call("mcp__contexer__get_context", {"query": "q"}, result="(id=11112222)")
    commit(repo, "b.txt", "fix: b")
    t.shell("git commit -m b")
    return token_of(stop("claude", env, payload("claude", "L", t.path, repo)))


def test_schema_example_is_valid(home):
    assert check(example(), home[1]).returncode == 0


REJECTIONS = {
    "own capture marked as not created this session": lambda j: (
        j["contexer_items"][1].update(created_this_session=False)),
    "id that is not an 8-hex decision id": lambda j: j["contexer_items"][0].update(id="zzz"),
    "note over 200 characters": lambda j: j["contexer_items"][0].update(note="x" * 201),
    "credential in a fact": lambda j: j["key_facts"][0].update(fact="key AKIAABCDEFGHIJKLMNOP"),
    "empty fact": lambda j: j["key_facts"][0].update(fact=" "),
    "contexer fact without ids": lambda j: j["key_facts"][0].update(source="contexer"),
    "decisive item with not_used verdict": lambda j: (
        j['verdict'].update(contexer_effect='not_used'),
        j["contexer_items"][0].update(relevance="decisive", note="changed step")),
    "helpful item without a note": lambda j: j["contexer_items"][0].update(
        relevance="helpful", note=""),
    "empty feature_ratings": lambda j: j.update(feature_ratings=[]),
    "empty key_facts": lambda j: j.update(key_facts=[]),
    "improvement without evidence": lambda j: j["improvements"][0].update(evidence=""),
    "unknown knowledge_location": lambda j: j["task"].update(knowledge_location="wiki"),
    "summary that is not a string": lambda j: j["task"].update(summary={"a": 1}),
    "helpful verdict backed only by own capture": lambda j: (
        j["verdict"].update(contexer_effect="helpful"),
        j["contexer_items"][1].update(relevance="helpful")),
    "decisive verdict without a contexer fact": lambda j: (
        j["verdict"].update(contexer_effect="decisive"),
        j["contexer_items"][0].update(relevance="decisive", note="changed step")),
    "llm_did_better that is not text": lambda j: j.update(llm_did_better=42),
}


@pytest.mark.parametrize("case", sorted(REJECTIONS))
def test_invalid_judgments_are_rejected(home, case):
    j = example()
    REJECTIONS[case](j)
    out = check(j, home[1])
    assert out.returncode == 2, (case, out.stdout)


def test_ids_are_checked_against_observed_output(home, claude_pending):
    _, env = home
    j = example()
    j["verdict"]["contexer_effect"] = "neutral"
    j["contexer_items"][0].update(id="a1b2c3d4", surfaced_by="autofetch")
    assert check(j, env, claude_pending).returncode == 0
    j["contexer_items"][0]["id"] = "deadbeef"
    out = check(j, env, claude_pending)
    assert out.returncode == 2 and "not in any autofetch output" in out.stdout


def test_session_start_ids_are_checked_too(home, claude_pending):
    _, env = home
    j = example()
    j["verdict"]["contexer_effect"] = "neutral"
    j["contexer_items"][0].update(id="deadbeef", surfaced_by="session_start")
    assert check(j, env, claude_pending).returncode == 2
    j["contexer_items"][0].update(surfaced_by="other_tool")
    assert check(j, env, claude_pending).returncode == 2
    j["contexer_items"][0].update(id="11112222")
    assert check(j, env, claude_pending).returncode == 0


def test_ordinary_words_are_not_mistaken_for_secrets(home):
    j = example()
    j["key_facts"][0]["fact"] = "Rotate token: refresh handling in auth.py"
    j["gaps"][0]["need"] = "secret: scanning now skips fixtures"
    assert check(j, home[1]).returncode == 0


def test_bare_provider_keys_are_rejected(home):
    for key in ("sk_live_" + "a1B2c3D4e5F6g7H8i9", "glpat-" + "abcdefghij1234567890",
                "token=" + "Zx9Qw8Er7Ty6Ui5Op4"):
        j = example()
        j["key_facts"][0]["fact"] = f"used {key}"
        assert check(j, home[1]).returncode == 2, key


def test_two_pr_only_reviews_in_one_session_both_log(home, repo, tmp_path):
    data_dir, env = home
    t = Transcript(tmp_path / "t.jsonl", "cursor")
    stop("cursor", env, payload("cursor", "pp", t.path, repo))
    j = example()
    j["verdict"]["contexer_effect"] = "neutral"
    for i in range(2):
        t.shell(f"gh pr create --title pr{i}")
        tok = token_of(stop("cursor", env, payload("cursor", "pp", t.path, repo)))
        out = run_script("log_usage.py", ["--token", tok], stdin=json.dumps(j), env=env)
        assert out.returncode == 0, out.stdout
    lines = next((data_dir / "records").glob("*.jsonl")).read_text().splitlines()
    assert len(lines) == 2


def test_default_branch_has_no_pr_lookup(repo):
    import log_usage
    assert log_usage.pr_lookup(str(repo), "main") == (None, "no_branch")


def test_not_used_verdict_rejected_when_contexer_was_called(home, claude_pending):
    j = example()
    j['verdict']['contexer_effect'] = 'not_used'
    out = check(j, home[1], claude_pending)
    assert out.returncode == 2 and "not_used" in out.stdout


def test_edited_pending_file_is_rejected(home, claude_pending):
    data_dir, env = home
    path = data_dir / "pending" / f"{claude_pending}.json"
    p = json.loads(path.read_text())
    p["observed"]["contexer_call_count"] = 99
    path.write_text(json.dumps(p))
    j = example()
    j["verdict"]["contexer_effect"] = "neutral"
    out = run_script("log_usage.py", ["--token", claude_pending], stdin=json.dumps(j), env=env)
    assert out.returncode == 2 and "does not match" in out.stdout


def test_log_writes_one_record_and_refuses_a_duplicate(home, claude_pending):
    data_dir, env = home
    j = example()
    j["verdict"]["contexer_effect"] = "neutral"
    j["contexer_items"][0].update(id="a1b2c3d4", surfaced_by="autofetch")
    pending = (data_dir / "pending" / f"{claude_pending}.json").read_text()
    out = run_script("log_usage.py", ["--token", claude_pending], stdin=json.dumps(j), env=env)
    assert out.returncode == 0, out.stdout
    records = list((data_dir / "records").glob("*.jsonl"))
    rec = json.loads(records[0].read_text().splitlines()[0])
    assert rec["schema"] == "contexer-effectiveness/v2" and rec["host"] == "claude"
    assert rec["pr_lookup"] in ("error", "none", "no_branch")
    assert not (data_dir / "pending" / f"{claude_pending}.json").exists()
    state = json.loads((data_dir / "state" / "L.json").read_text())
    (data_dir / "pending" / f"{claude_pending}.json").write_text(pending)
    state["digests"][claude_pending] = stop_hook.digest(json.loads(pending))
    (data_dir / "state" / "L.json").write_text(json.dumps(state))
    again = run_script("log_usage.py", ["--token", claude_pending], stdin=json.dumps(j), env=env)
    assert again.returncode == 2 and "already exists" in again.stdout
