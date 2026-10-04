import json

import pytest

from contexer import retrieval, store


def anchored(content, *, status="approved", subtype="architecture"):
    entry = store._new_decision_entry(content, "review", subtype, status=status)
    entry["source_files"] = ["src/config.py"]
    return entry


def test_relevant_approved_constraint_keeps_slot_ahead_of_pending_guidance(tmp_repo, monkeypatch):
    approved = anchored("Never expose authentication credentials.", subtype="constraint")
    pending = [anchored(f"Proposed authentication serialization policy {i}.", status="pending_approval")
               for i in range(3)]
    for i, entry in enumerate(pending):
        entry["title"] = f"Pending proposal {i}"
    store.save(tmp_repo, {"entries": [*pending, approved]})
    monkeypatch.setattr(retrieval, "prompt_rank", lambda *_: [
        *( (e["id"], 10.0 - i, 2, 1, 2, 0) for i, e in enumerate(pending)),
        (approved["id"], 6.0, 2, 1, 2, 0),
    ])
    text = store.get_context_for_prompt(tmp_repo, "Fix authentication serialization in src/config.py")
    assert approved["content"] in text
    assert sum(e["content"] in text for e in pending) == 2
    assert text.index(approved["content"]) < text.index(pending[0]["content"])


@pytest.mark.parametrize("index_state", ["missing", "previous-version", "corrupt"])
def test_global_anchor_ranks_without_current_global_index(tmp_repo, index_state):
    store.save(tmp_repo, {"entries": [anchored("Use separate billing transactions.")]})
    global_rule = anchored("Never expose authentication credentials.", subtype="constraint")
    store.save_global({"entries": [global_rule]})
    path = store._index_path(store.GLOBAL_SLUG)
    if index_state == "missing":
        path.unlink()
    elif index_state == "previous-version":
        path.write_text(json.dumps({"v": store._RETRIEVAL_INDEX_VERSION - 1}))
    else:
        path.write_text("{broken")
    before = path.read_bytes() if path.exists() else None
    text = store.get_context_for_prompt(tmp_repo, "Fix authentication credentials in src/config.py")
    assert global_rule["content"] in text
    assert (path.read_bytes() if path.exists() else None) == before


def test_topic_and_overflow_pointers_count_each_identity_once(tmp_repo, monkeypatch):
    entry = anchored("Use database migrations for authentication policy.")
    store.save(tmp_repo, {"entries": [entry]})
    monkeypatch.setattr(retrieval, "prompt_rank", lambda *_: [])
    text, meta = store.get_context_for_prompt_with_meta(
        tmp_repo, "Update database authentication migrations in src/config.py")
    assert meta["kind"] == "pointer"
    assert "anchored to files" in text and "Related stored decisions" in text
    assert meta["count"] == 1


@pytest.mark.parametrize("overflow_scope, expected_count", [("personal", 1), ("global", 2)])
def test_mention_and_overflow_pointers_count_scoped_identities(
        tmp_repo, monkeypatch, overflow_scope, expected_count):
    entry = anchored("Use database migrations for authentication policy.")
    store.save(tmp_repo, {"entries": [entry]})
    store.save_global({"entries": [entry]})
    monkeypatch.setattr(retrieval, "prompt_rank", lambda *_: [])
    monkeypatch.setattr(retrieval, "derive_topics", lambda *_: [])
    monkeypatch.setattr(store, "_prompt_file_hits", lambda *_args, **_kwargs: (
        [{"scope": overflow_scope, "id": entry["id"]}],
        [(entry["id"], entry["title"])], ["src/config.py"]))
    text, meta = store.get_context_for_prompt_with_meta(tmp_repo, "Update src/config.py")
    assert meta["kind"] == "pointer"
    assert "anchored to files" in text and "decisions mention" in text
    assert meta["count"] == expected_count
