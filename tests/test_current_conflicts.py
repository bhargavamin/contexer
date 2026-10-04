import pytest

from contexer import conflicts, store


def pair():
    return [store._new_decision_entry("Prefix version strings with a lowercase v, as in v1.4.0, so they match git tags.", "s", "convention", status="suggested", title="Prefix versions with v"),
            store._new_decision_entry("Publish versions as bare semantic versions such as 1.4.0; the package index rejects a leading letter.", "s", "convention", status="suggested", title="Publish bare semantic versions")]


@pytest.mark.parametrize("count", [34, 150])
def test_startup_groups_conflicts_independent_of_store_size(tmp_repo, count):
    left, right = pair()
    others = [store._new_decision_entry(f"Rule {i} about unrelated subsystem {i}.", "s", "convention", status="suggested") for i in range(count - 2)]
    store.save(tmp_repo, {"entries": [left, *others, right]})
    context = store.session_start_payload(tmp_repo)["context"]
    group = context.split("## Conflicting current decisions:", 1)[1].split("## Project rules", 1)[0]
    assert left["id"][:8] in group and right["id"][:8] in group
    assert "CONFLICT" in group and "Ask the developer" in group
    assert len(conflicts.current_pairs(store.load(tmp_repo)["entries"])) == 1


def test_prompt_renderer_pulls_the_other_side(tmp_repo):
    left, right = pair()
    store.save(tmp_repo, {"entries": [left, right]})
    text, receipts = store._render_prompt_decisions_with_records(tmp_repo, [left["id"]])
    assert left["id"][:8] in text and right["id"][:8] in text
    assert "CONFLICT" in text
    assert {r["id"] for r in receipts} == {left["id"], right["id"]}


@pytest.mark.parametrize("second", ["Prefix version strings with a lowercase v to match tags.",
                                    "We rejected bare semantic versions; prefixing is required.",
                                    "Publish API documentation with version examples."])
def test_non_conflicting_near_duplicates_have_no_marker(second):
    left = pair()[0]
    right = store._new_decision_entry(second, "s", "convention", status="suggested")
    assert conflicts.current_pairs([left, right]) == []


def test_inactive_or_disjoint_scope_rules_are_not_conflicts():
    left, right = pair()
    right["status"] = "ignored"
    assert not conflicts.current_pairs([left, right])
    right["status"] = "suggested"
    left["source_files"], right["source_files"] = ["cli.py"], ["api.py"]
    assert not conflicts.current_pairs([left, right])


def test_directory_scope_overlaps_nested_file_scope():
    left, right = pair()
    left["source_files"], right["source_files"] = ["cli/"], ["cli/version.py"]
    assert len(conflicts.current_pairs([left, right])) == 1


@pytest.mark.parametrize("filename", ["adoption_tasks.json", "adoption_tasks_150.json"])
def test_real_adoption_store_has_exactly_one_conflict_pair(tmp_repo, filename):
    import json
    from pathlib import Path
    from benchmarks import seeding

    tasks = json.loads((Path(__file__).parents[1] / "benchmarks" / filename).read_text())
    task = next(t for t in tasks if t["id"] == "adopt-k7c-version")
    entries = [store._new_decision_entry(item["content"], "s", item["subtype"],
               title=item.get("title", ""), status="suggested")
               for item in seeding.seed_items(task, 0, 0)]
    pairs = conflicts.current_pairs(entries)
    assert len(pairs) == 1
    assert {d["title"] for d in pairs[0]} == {"Prefix versions with v", "Publish bare semantic versions"}
    store.save(tmp_repo, {"entries": entries})
    payload = store.session_start_payload(tmp_repo)
    context = payload["context"]
    group = context.split("## Conflicting current decisions:", 1)[1].split("## Project rules", 1)[0]
    assert all(d["id"][:8] in group and d["content"] in group for d in pairs[0])
    assert context.count("CONFLICT:") == 1



def test_pending_update_is_visible_but_untrusted_in_pair(tmp_repo):
    left, right = pair()
    left["proposed_revision"] = {"content": "Use bare semantic versions for every release.", "source": "ai"}
    store.save(tmp_repo, {"entries": [left, right]})
    text = store.session_start_payload(tmp_repo)["context"]
    assert "[Pending update — not approved] Use bare semantic versions" in text
    assert "Prefix version strings with a lowercase v" in text


def test_mixed_compatible_prescriptions_are_not_false_conflicts():
    left, right = pair()
    left["content"] = "Publish versions with a lowercase v for Git tags; use bare semantic versions for the package index."
    left["revisions"][0]["content"] = left["content"]
    assert conflicts.current_pairs([left, right]) == []


@pytest.mark.parametrize("mixed", [
    "Prefix versions with a lowercase v for package releases; use bare semantic versions in sample docs.",
    "Prefix package-release versions with a lowercase v but use bare versions in sample docs.",
])
def test_mixed_rule_retains_conflict_for_the_same_output(tmp_repo, mixed):
    left, right = pair()
    left["content"] = mixed
    left["revisions"][0]["content"] = mixed
    assert len(conflicts.current_pairs([left, right])) == 1
    store.save(tmp_repo, {"entries": [left, right]})
    startup = store.session_start_payload(tmp_repo)["context"]
    prompt, receipts = store._render_prompt_decisions_with_records(tmp_repo, [left["id"]])
    for text in (startup, prompt):
        assert "CONFLICT:" in text
        assert left["id"][:8] in text and right["id"][:8] in text
    assert {row["id"] for row in receipts} == {left["id"], right["id"]}


def test_single_output_rules_do_not_conflict_across_outputs():
    left, right = pair()
    left["revisions"][0]["content"] = "Publish versions with a lowercase v for Git tags."
    assert conflicts.current_pairs([left, right]) == []


def test_negated_alternative_does_not_change_bare_classification():
    left, right = pair()
    right["content"] = "Publish bare semantic versions; do not use versions with v."
    right["revisions"][0]["content"] = right["content"]
    assert len(conflicts.current_pairs([left, right])) == 1


def test_malformed_legacy_anchors_do_not_crash():
    left, right = pair()
    left["source_files"] = [{"path": "cli.py"}]
    assert len(conflicts.current_pairs([left, right])) == 1


def test_shared_pair_member_is_rendered_once(tmp_repo):
    left, right = pair()
    third = store._new_decision_entry("Use bare semantic versions to match the package index.", "s", "convention", status="suggested")
    store.save(tmp_repo, {"entries": [left, right, third]})
    context = store.session_start_payload(tmp_repo)["context"]
    assert context.count("CONFLICT:") == 1
    assert context.count(left["content"]) == 1


@pytest.mark.parametrize("choice,expected", [("update", "update was picked"), ("standing", "update was declined")])
def test_pair_retains_valid_pending_resolution_memo(tmp_repo, choice, expected):
    left, right = pair()
    left["proposed_revision"] = {"content": "Use bare semantic versions for every release.", "source": "ai"}
    left["conflict_memo"] = {"pair": conflicts._conflict_pair_key(left), "choice": choice,
                             "created_at": "2026-10-03T12:00:00+00:00"}
    store.save(tmp_repo, {"entries": [left, right]})
    text = store.session_start_payload(tmp_repo)["context"]
    assert expected in text
    assert "Pending update — not approved" in text
