from contexer import store
from contexer.adapters import claude


def test_missing_guidance_requires_one_focused_lookup(tmp_repo):
    store.update_decision(tmp_repo, "Count failed batches once per batch.", "s", "architecture")
    text = store.get_context_for_prompt(tmp_repo, "Why count failed batches?")
    assert "you must make one focused lookup" in text
    assert "before reading or editing files" in text


def test_named_pointer_requests_missing_guidance_without_new_notice(tmp_repo):
    _, did = store.update_decision(tmp_repo, "Keep long-term invoices auditable.", "s", "architecture",
                                   source_files=["app/core.py"], title="Keep invoices auditable")
    raw = '{"prompt":"Add deterministic tokenizer stemming to app/core.py"}'
    def monkey_lookup(*args, **kwargs):
        return store.get_context_for_prompt_with_meta(
            tmp_repo, "Add deterministic tokenizer stemming to app/core.py", "lookup-guidance")
    # The existing pointer notice remains the same; only the model's guidance changes.
    from unittest.mock import patch
    with patch.object(store, "resolve_repo", return_value=tmp_repo):
        payload = claude._recall_payload(tmp_repo, raw, prompt_lookup=monkey_lookup)
    text = payload["hookSpecificOutput"]["additionalContext"]
    assert did[:8] in text and "before editing" in text
    assert "Add query=<task subject>" in text
    assert payload["systemMessage"] == "Contexer: pointed at related decisions (app/core.py, core.py)"


def test_plain_unrelated_prompt_stays_silent(tmp_repo):
    assert store.get_context_for_prompt(tmp_repo, "hello") == ""
