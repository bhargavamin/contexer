import pytest

from contexer import revisions, server, store


def test_capture_indexes_task_vocabulary_separately(tmp_repo):
    result = server.update_context("Keep execution synchronous; no threads or asyncio.",
                                   subtype="convention", repo_path=tmp_repo,
                                   applies_when=["slow upstream reads", "making fetches faster"])
    assert "Stored" in result
    entry = store.load(tmp_repo)["entries"][0]
    assert entry["applies_when"] == ["slow upstream reads", "making fetches faster"]
    assert revisions.current_revision(entry)["applies_when"] == entry["applies_when"]
    index = store._read_retrieval_index(tmp_repo)
    assert "upstream" in index["applies_df"] and "upstream" not in index["df"]
    assert "synchronous" in store.get_context(tmp_repo, query="slow upstream")
    assert "synchronous" in store.get_context_for_prompt(tmp_repo, "Why are slow upstream reads still synchronous?")


def test_one_trigger_word_cannot_inject_a_decision(tmp_repo):
    store.update_decision(tmp_repo, "Use os.replace to publish durable state.", "s", "architecture",
                          applies_when=["writing JSON records"])
    assert "os.replace" not in store.get_context_for_prompt(tmp_repo, "Why does JSON matter here?")


def test_scattered_trigger_words_cannot_inject_a_decision(tmp_repo):
    store.update_decision(tmp_repo, "Keep execution synchronous.", "s", "convention",
                          applies_when=["slow upstream", "writing records"])
    assert "Keep execution synchronous." not in store.get_context_for_prompt(
        tmp_repo, "Why are slow records an issue?")


@pytest.mark.parametrize("task_id", ["adopt-k3-atomic", "adopt-k4-append", "adopt-k5-no-threads"])
@pytest.mark.parametrize("rep", [0, 1, 2])
def test_authored_phrases_retrieve_adoption_decisions(tmp_repo, task_id, rep):
    import json
    from pathlib import Path
    from benchmarks import seeding
    from benchmarks.applicability import adoption_triggers

    tasks = json.loads((Path(__file__).parents[1] / "benchmarks/adoption_tasks.json").read_text())
    task = next(t for t in adoption_triggers.enrich(tasks) if t["id"] == task_id)
    items = seeding.seed_items(task, 0, rep)
    scope = {}
    exec(compile(seeding.seed_script(tmp_repo, items), "<adoption-seed>", "exec"), scope)
    needed = scope["seeded_ids"][task["needed_decision"]]
    text = store.get_context_for_prompt(tmp_repo, task["prompt"].replace("{seed}", "0"))
    entry = store.entry_by_id(store.load(tmp_repo)["entries"], needed)
    assert needed[:8] in text
    assert revisions.current_content(entry) in text


def test_applicability_update_respects_constraint_review(tmp_repo):
    _, did = store.update_decision(tmp_repo, "Never use threads in this service.", "s", "constraint",
                                   created_by="human", applies_when=["slow upstream reads"])
    store.update_decision(tmp_repo, "Never use threads in this service.", "s2", "constraint",
                          replace_id=did, applies_when=["parallel data fetches"])
    entry = store.load(tmp_repo)["entries"][0]
    assert entry["applies_when"] == ["slow upstream reads"]
    assert entry["proposed_revision"]["applies_when"] == ["parallel data fetches"]
    assert store.approve_decision(tmp_repo, did, "approve")[0]
    entry = store.load(tmp_repo)["entries"][0]
    assert entry["applies_when"] == ["parallel data fetches"]
    assert entry["revisions"][0]["applies_when"] == ["slow upstream reads"]


@pytest.mark.parametrize("bad", ["everything", ["everything"], ["too " + "long" * 30], ["read JSON"] * 9, [7]])
def test_invalid_trigger_terms_never_write(tmp_repo, bad):
    with pytest.raises(ValueError, match="applies_when"):
        store.update_decision(tmp_repo, "Use atomic publication.", "s", applies_when=bad)
    assert store.load(tmp_repo)["entries"] == []


def test_path_title_is_only_a_pointer_on_unrelated_task(tmp_repo):
    _, unrelated = store.update_decision(tmp_repo, "Public functions keep one-line docstrings.", "s", "convention",
                                          title="Docstrings for functions in app/core.py")
    _, needed = store.update_decision(tmp_repo, "Count failed batches once per batch.", "s", "architecture",
                                      title="Count batch failures once")
    text = store.get_context_for_prompt(tmp_repo, "Fix failed batch counting in app/core.py")
    assert needed[:8] in text
    assert "Public functions keep" not in text
    assert unrelated[:8] not in text or "not shown" in text


def test_irrelevant_anchors_leave_space_for_unanchored_task_rule(tmp_repo):
    for content in ["Use compact serialization for payload digests.", "Read decisions through the facade.", "Back off network retries exponentially."]:
        store.update_decision(tmp_repo, content, "s", "architecture", source_files=["app/core.py"], title=content.split()[0]+" subject")
    _, needed = store.update_decision(tmp_repo, "Tokenizer stemming stays deterministic across platforms.", "s", "constraint", created_by="human")
    text = store.get_context_for_prompt(tmp_repo, "Add deterministic tokenizer stemming to app/core.py")
    assert needed[:8] in text and "Tokenizer stemming stays" in text
    assert "Read decisions through the facade." not in text
    assert "not shown" in text


@pytest.mark.parametrize("phrase", ["read it", "UI in Go", "use S3 db", "editing config.py", "add to JSON"])
def test_unmatchable_phrases_are_refused(tmp_repo, phrase):
    with pytest.raises(ValueError, match="applies_when"):
        store.update_decision(tmp_repo, "Keep execution synchronous.", "s", applies_when=[phrase])


@pytest.mark.parametrize("phrase", ["fix test", "new feature", "code changes", "any task"])
def test_generic_task_phrases_are_refused(tmp_repo, phrase):
    """Such a phrase is a subset of most task prompts and would take the top full slot on all of them."""
    with pytest.raises(ValueError, match="applies_when"):
        store.update_decision(tmp_repo, "Keep execution synchronous.", "s", applies_when=[phrase])


def test_explicit_lookup_matches_one_term_but_not_two_mixed_phrases(tmp_repo):
    store.update_decision(tmp_repo, "Keep execution synchronous.", "s", "architecture",
                          applies_when=["slow upstream", "writing records"])
    assert "Keep execution synchronous." in store.get_context(tmp_repo, query="upstream")
    assert "Keep execution synchronous." not in store.get_context(tmp_repo, query="slow records")


def test_applicability_only_update_preserves_title_and_is_visible_to_review(tmp_repo):
    from contexer import review_impact
    _, did = store.update_decision(tmp_repo, "Keep execution synchronous.", "s", "constraint",
                                   created_by="human", title="No parallel execution")
    store.update_decision(tmp_repo, "Keep execution synchronous.", "s", "constraint", replace_id=did,
                          applies_when=["slow upstream reads"])
    entry = store.load(tmp_repo)["entries"][0]
    assert entry["proposed_revision"]["title"] == "No parallel execution"
    lines = review_impact.impact_lines(review_impact.review_impact(tmp_repo, entry))
    assert "Proposed applicability: slow upstream reads" in lines
    assert "slow upstream reads" in store.format_pending_review(tmp_repo)
    store.approve_decision(tmp_repo, did, "edit", content="Keep synchronous database transactions.")
    assert store.load(tmp_repo)["entries"][0]["applies_when"] == []


def test_global_anchor_uses_cached_index_in_prompt_hook(tmp_repo, monkeypatch):
    _, did = store.update_global_decision("Keep invoice numbering stable in app/invoices.py because audit trails need it.", "s", "constraint")
    data = store.load_global()
    data["entries"][0]["source_files"] = ["app/invoices.py"]
    store.save_global(data)
    store.session_start_payload(tmp_repo)
    def no_inline_index(*args, **kwargs):
        raise AssertionError("per-prompt index rebuild")
    monkeypatch.setattr(store, "_build_retrieval_index", no_inline_index)
    text = store.get_context_for_prompt(tmp_repo, "Why is invoice numbering stable in app/invoices.py?")
    assert "invoice numbering stable" in text
