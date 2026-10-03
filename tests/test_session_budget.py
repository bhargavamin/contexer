import json

from contexer import store, working_set
from contexer.adapters import claude


def test_budget_keeps_constraints_before_large_title_list():
    context = "## Project rules - apply to ALL tasks in this repo:\n"
    context += "\n".join(f"- [convention] [suggested] Convention {i}: " + "é" * 70 + f" (id={i:08x})" for i in range(150))
    constraint = "- [constraint] Use config at call time (id=aaaaaaaa)\n    Never cache configuration at import time."
    context += "\n" + constraint
    result, dropped = claude.budget_session_context(context)
    assert len(result.encode("utf-8")) <= claude.SESSION_CONTEXT_BYTES
    assert len(json.dumps(result).encode()) <= claude.SESSION_CONTEXT_BYTES
    assert constraint in result
    assert result.index(constraint) < result.index("Convention")
    assert "get_context" in result
    assert "aaaaaaaa" not in dropped
    assert dropped


def test_small_context_is_unchanged():
    assert claude.budget_session_context("Small context") == ("Small context", set())


def test_oversized_decision_is_omitted_whole():
    context = "## Project rules:\n- [constraint] Too big (id=aaaaaaaa)\n    " + "é" * 9000
    result, dropped = claude.budget_session_context(context)
    assert len(result.encode("utf-8")) <= claude.SESSION_CONTEXT_BYTES
    assert "Too big" not in result
    assert dropped == {"aaaaaaaa"}


def test_large_store_credits_only_delivered_rules_and_other_hosts_keep_full_context(tmp_repo):
    entries = [store._new_decision_entry(f"Convention {i}: " + "distinct " * 15, "s", "convention", status="suggested", title=f"Convention {i}: " + "é" * 65) for i in range(150)]
    constraint = store._new_decision_entry("Read configuration at call time because imports otherwise freeze the environment.", "s", "constraint", status="approved", title="Read configuration at call time")
    entries.append(constraint)
    store.save(tmp_repo, {"repo_path": tmp_repo, "entries": entries})
    bounded = store.session_start_payload(tmp_repo, "startup", "claude-s", "claude")
    other = store.session_start_payload(tmp_repo, "startup", "codex-s", "codex")
    assert len(bounded["context"].encode("utf-8")) <= claude.SESSION_CONTEXT_BYTES
    assert len(other["context"].encode("utf-8")) > claude.SESSION_CONTEXT_BYTES
    assert "imports otherwise freeze the environment" in bounded["context"]
    delivered = working_set.records(tmp_repo, "claude-s")
    assert any(r["id"] == constraint["id"] and r["fingerprint"] for r in delivered)
    assert all(r["id"][:8] in bounded["context"] for r in delivered if r["fingerprint"])
    assert "_startup_credit" not in bounded
    envelope = store.get_session_start_context(tmp_repo, "startup", "codex-real", "codex")
    assert len(envelope["hookSpecificOutput"]["additionalContext"].encode()) > claude.SESSION_CONTEXT_BYTES


def test_conflict_pair_is_kept_with_both_sides_and_guide():
    group = ("## Conflicting current decisions:\nCONFLICT: Ask the developer which applies.\n"
             "- [suggested] Prefix versions (id=aaaaaaaa)\n    Use v1.2.3.\n"
             "- [suggested] Bare versions (id=bbbbbbbb)\n    Use 1.2.3.")
    context = "## Project rules:\n" + "\n".join(
        f"- [convention] Unrelated {i}: " + "long text " * 30 for i in range(150))
    result, dropped = claude.budget_session_context(context + "\n" + group)
    assert group in result
    assert not {"aaaaaaaa", "bbbbbbbb"} & dropped
