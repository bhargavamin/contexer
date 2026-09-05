"""Bootstrap's evidence contract, exception-only UX and next-session trust boundary."""

import asyncio
import copy
import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from contexer import bootstrap, guard_engine, policy, revisions, server, share_policy, store


@pytest.fixture
def project(tmp_repo):
    root = Path(tmp_repo)
    root.mkdir()
    (root / "pyproject.toml").write_text(
        '[project]\nname = "ledger"\nrequires-python = ">=3.12"\n'
        '[tool.ruff]\nline-length = 100\n', encoding="utf-8")
    (root / "billing.py").write_text(
        'def charge(db, amount):\n    with db.transaction():\n'
        '        db.insert_charge(amount)\n        db.insert_outbox("email")\n', encoding="utf-8")
    return root


def ref(root, file, line, end=None, role="documentation"):
    text = (root / file).read_text(encoding="utf-8").splitlines()
    return {"file": file, "line": line, "end_line": end or line,
            "quote": "\n".join(text[line - 1:end or line]), "role": role}


def finding(root, scan, *, assessment="supported"):
    candidate = scan["candidates"][0]
    return {"candidate_id": candidate["candidate_id"], "content": candidate["content"],
            "kind": "inferred", "subtype": "architecture", "scope": "billing service",
            "assessment": assessment, "reason": "Documentation compared with transaction implementation",
            "sources": [ref(root, candidate["source_file"], candidate["source_line"]),
                        ref(root, "billing.py", 2, 4, "implementation")]}


def scan_rule(root, rule="Use a transactional outbox for billing email."):
    (root / "ARCHITECTURE.md").write_text("# Decisions\n- " + rule + "\n", encoding="utf-8")
    return bootstrap.run(str(root), "test")


def finish(root, scan, rows):
    return bootstrap.run(str(root), "test", snapshot_id=scan["snapshot_id"], findings=rows, finish=True)


def test_facts_saved_without_approval_or_completed_analysis(project):
    scan = bootstrap.run(str(project), "test")
    entries = store.load(str(project))["entries"]
    assert any(">=3.12" in e["content"] for e in entries)
    assert all(e["status"] == "suggested" and e["created_by"] == "ai" for e in entries)
    assert all(revisions.current_revision(e)["approved_at"] is None for e in entries)
    assert not store.get_pending_decisions(str(project))
    assert scan["stage"] == "interpreting"
    assert "incomplete" in bootstrap.directive(str(project))
    assert "fact-confirmation" not in bootstrap.GUIDE  # only conflict questions are required


def test_grounded_rule_used_next_session_but_never_project_policy(project):
    scan = scan_rule(project)
    result = finish(project, scan, [finding(project, scan)])
    assert result["stage"] == "reported_complete"
    entry = next(e for e in store.load(str(project))["entries"] if e.get("bootstrap", {}).get("kind") == "inferred")
    assert entry["status"] == "suggested"
    assert "source_files" not in entry
    assert "approved_by" not in entry
    ctx = store.session_start_payload(str(project))["context"]
    assert "transactional outbox" in ctx
    assert "not human-approved policy" in ctx
    assert "Project rules - apply to ALL" not in ctx
    assert "call bootstrap_context now" not in ctx
    assert "Evidence billing.py:2" in store.get_context(str(project), query="outbox")
    assert not store.get_pending_decisions(str(project))


def test_conflict_only_is_pending_and_shows_both_sources(project):
    scan = scan_rule(project, "Always send billing email synchronously.")
    row = finding(project, scan, assessment="contradicted")
    row["question"] = "Keep the outbox implementation or restore synchronous delivery?"
    result = finish(project, scan, [row])
    pending = store.get_pending_decisions(str(project))
    assert len(pending) == 1
    assert result["outcomes"][0]["question"] == row["question"]
    detail = store.get_context(str(project), query="synchronously")
    assert "UNRESOLVED" in detail and "db.insert_outbox" in detail
    assert "Always send billing" not in store.session_start_payload(str(project))["context"]


@pytest.mark.parametrize("conflicted", [False, True])
def test_user_correction_versions_original_and_rescan_cannot_undo(project, conflicted):
    scan = scan_rule(project)
    row = finding(project, scan, assessment="contradicted" if conflicted else "supported")
    if conflicted:
        row["question"] = "Which delivery mechanism should govern?"
    result = finish(project, scan, [row])
    did = result["outcomes"][0]["id"]
    before = copy.deepcopy(revisions.current_revision(store.entry_by_id(store.load(str(project))["entries"], did)))
    ok, _ = store.approve_decision(str(project), did, "edit", "Use an outbox for all billing notifications.")
    assert ok
    entry = store.entry_by_id(store.load(str(project))["entries"], did)
    assert entry["revisions"][0] == before
    assert entry["revision"] == 2
    assert entry["approved_by"] == "human"
    assert revisions.current_revision(entry)["source"] == "human"
    fresh = bootstrap.run(str(project), "again")
    again = finish(project, fresh, [finding(project, fresh)])
    assert again["outcomes"][0]["outcome"] == "protected"
    assert store.entry_by_id(store.load(str(project))["entries"], did)["revision"] == 2
    assert store.approve_decision(str(project), did, "edit", "Use an outbox for billing only.")[0]
    assert store.entry_by_id(store.load(str(project))["entries"], did)["revision"] == 3


def test_rescan_is_idempotent_and_does_not_inflate_confidence(project):
    scan = scan_rule(project)
    finish(project, scan, [finding(project, scan)])
    before = store.load(str(project))["entries"]
    fresh = bootstrap.run(str(project), "another-session")
    assert fresh["stage"] == "reported_complete"
    result = finish(project, fresh, [finding(project, fresh)])
    assert result["outcomes"][0]["outcome"] == "unchanged"
    assert store.load(str(project))["entries"] == before


@pytest.mark.parametrize("mutate", ["quote", "line", "missing_source", "md_as_code", "duplicate_conflict"])
def test_fabricated_or_invalid_evidence_is_rejected_atomically(project, mutate):
    scan = scan_rule(project)
    row = finding(project, scan)
    if mutate == "quote":
        row["sources"][1]["quote"] = "db.send_synchronously()"
    elif mutate == "line":
        row["sources"][1]["line"] = 3
    elif mutate == "missing_source":
        row["sources"][1]["file"] = "not-scanned.py"
    elif mutate == "md_as_code":
        row["sources"][0]["role"] = "implementation"
    else:
        row["assessment"], row["question"] = "contradicted", "Which should govern?"
        row["sources"] = [row["sources"][0], row["sources"][0]]
    before = store.load(str(project))
    with pytest.raises(ValueError):
        finish(project, scan, [row])
    assert store.load(str(project)) == before


def test_uncommitted_source_change_rejects_report_and_marks_existing_inference_stale(project):
    scan = scan_rule(project)
    row = finding(project, scan)
    finish(project, scan, [row])
    scan = bootstrap.run(str(project), "again")
    (project / "billing.py").write_text("def charge(db, amount):\n    db.send_email()\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Snapshot changed"):
        finish(project, scan, [row])
    assert "Evidence changed or disappeared" in store.get_context(str(project), query="outbox")


def test_human_edit_during_analysis_rejects_report(project):
    scan = scan_rule(project)
    did = store.load(str(project))["entries"][0]["id"]
    store.approve_decision(str(project), did, "edit", "Python 3.13 or newer is required.")
    with pytest.raises(ValueError, match="decision changed"):
        finish(project, scan, [finding(project, scan)])


def test_finish_requires_accounting_for_every_candidate(project):
    scan = scan_rule(project)
    with pytest.raises(ValueError, match="Unaccounted"):
        finish(project, scan, [])
    assert store.load(str(project))["bootstrap_scan"]["stage"] == "interpreting"


def test_interrupted_batch_resumes_and_is_not_mistaken_for_complete(project):
    scan = scan_rule(project)
    receipt = bootstrap.run(str(project), "test", snapshot_id=scan["snapshot_id"],
                            findings=[finding(project, scan)])
    assert receipt["stage"] == "interpreting"
    assert "incomplete" in store.session_start_payload(str(project))["context"]
    final = bootstrap.run(str(project), "new-session", snapshot_id=receipt["snapshot_id"],
                          findings=[], finish=True)
    assert final["stage"] == "reported_complete"


def test_code_only_fact_needs_no_documentation_or_confirmation(project):
    scan = bootstrap.run(str(project), "test")
    row = {"topic": "billing-outbox", "content": "Billing inserts an email outbox record inside its transaction.",
           "kind": "observed", "subtype": "pattern", "scope": "billing.charge",
           "assessment": "supported", "reason": "Transaction body contains both database writes",
           "sources": [ref(project, "billing.py", 1, 4, "implementation")]}
    result = finish(project, scan, [row])
    assert result["outcomes"][0]["outcome"] == "stored"
    assert not store.get_pending_decisions(str(project))


def test_weak_code_only_hypothesis_is_retained_but_not_usable(project):
    scan = bootstrap.run(str(project), "test")
    row = {"topic": "outbox-policy", "content": "All services probably require outboxes.",
           "kind": "inferred", "subtype": "architecture", "scope": "repository",
           "assessment": "unverified", "reason": "Only billing was inspected",
           "sources": [ref(project, "billing.py", 1, 4, "implementation")]}
    assert finish(project, scan, [row])["outcomes"][0]["outcome"] == "unverified"
    assert "probably" not in store.get_context(str(project))


@pytest.mark.parametrize("setting,assessment", [(100, "supported"), (88, "contradicted")])
def test_deterministic_scalar_comparison_cannot_be_overruled_by_model(project, setting, assessment):
    scan = scan_rule(project, f"Use Ruff line length {setting}.")
    assert scan["candidates"][0]["comparison"]["assessment"] == assessment
    wrong = finding(project, scan, assessment="unverified")
    with pytest.raises(ValueError, match="parsed configuration"):
        finish(project, scan, [wrong])


def test_malformed_configuration_does_not_support_scalar_claim(project):
    (project / "pyproject.toml").write_text('[tool.ruff]\nline-length = 100\nINVALID\n', encoding="utf-8")
    scan = scan_rule(project, "Use Ruff line length 100.")
    assert scan["facts"] == []
    assert scan["candidates"][0]["comparison"]["assessment"] == "unverified"


@pytest.mark.parametrize("rule", ["Require Python 3.12 or newer.", "Use PostgreSQL in production."])
def test_versions_and_environment_words_do_not_create_lexical_conflicts(project, rule):
    scan = scan_rule(project, rule)
    assert scan["candidates"][0]["comparison"]["assessment"] == "unverified"
    assert not store.get_pending_decisions(str(project))


def test_superseded_document_never_becomes_active_guidance(project):
    scan = scan_rule(project)
    doc = project / "ARCHITECTURE.md"
    doc.write_text("Status: superseded\n" + doc.read_text(encoding="utf-8"), encoding="utf-8")
    scan = bootstrap.run(str(project), "again")
    assert scan["candidates"][0]["historical"]
    result = finish(project, scan, [finding(project, scan)])
    assert result["outcomes"][0]["outcome"] == "historical"
    assert "transactional outbox" not in store.get_context(str(project))


def test_external_docs_are_opt_in_remembered_and_never_follow_links(project, tmp_path):
    outside = tmp_path / "shared.md"
    outside.write_text("# Rules\n- Never expose private customer records.\n", encoding="utf-8")
    (project / "README.md").write_text(f"Shared rules: [{outside}]({outside})\n", encoding="utf-8")
    first = bootstrap.run(str(project), "test")
    assert str(outside) not in first["files"]
    assert first["external_docs_question"]
    second = bootstrap.run(str(project), "test", external_paths=[str(outside)])
    assert str(outside) in second["files"]
    assert any(c["source_file"] == str(outside) for c in second["candidates"])
    assert not bootstrap.run(str(project), "next")["external_docs_question"]
    assert str(outside) not in bootstrap.run(str(project), "next", external_paths=[])["files"]


def test_snapshot_ignores_symlinks_generated_docs_and_reports_limits(project, tmp_path, monkeypatch):
    outside = tmp_path / "outside.py"
    outside.write_text("secret = 'private'\n", encoding="utf-8")
    (project / "linked.py").symlink_to(outside)
    (project / "generated.md").write_text("<!-- AUTO-GENERATED: DO NOT EDIT -->\n# Rules\n- Never use this.\n", encoding="utf-8")
    scan = bootstrap.run(str(project), "test", apply=False)
    assert "linked.py" not in scan["files"] and "generated.md" not in scan["files"]
    monkeypatch.setattr(bootstrap, "MAX_FILES", 1)
    assert bootstrap.run(str(project), "test", apply=False)["omitted"]


def test_preview_is_read_only_and_does_not_consume_external_question(project):
    result = bootstrap.run(str(project), "test", apply=False)
    assert result["outcomes"] == []
    assert not store.load(str(project))["entries"]
    assert "bootstrap_scan" not in store.load(str(project))
    assert bootstrap.run(str(project), "test")["external_docs_question"]


def test_deleted_inference_is_not_resurrected(project):
    scan = scan_rule(project)
    did = finish(project, scan, [finding(project, scan)])["outcomes"][0]["id"]
    store.delete_decision(str(project), did)
    scan = bootstrap.run(str(project), "again")
    assert finish(project, scan, [finding(project, scan)])["outcomes"][0]["outcome"] == "protected_deleted"


@pytest.mark.parametrize("commits", [None, 0, 1])
def test_no_git_empty_git_and_own_commit_all_get_nonblocking_bootstrap(project, commits):
    if commits is not None:
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        if commits:
            subprocess.run(["git", "-C", str(project), "-c", "user.name=Test", "-c", "user.email=test@example.invalid",
                            "commit", "--allow-empty", "-qm", "Initialize"], check=True)
    ctx = store.session_start_payload(str(project))["context"]
    assert "call bootstrap_context now" in ctx
    assert "without asking setup" in ctx
    assert "How well do you know" not in ctx


def test_server_exposes_semantic_report_and_safe_failure(project):
    first = json.loads(server.bootstrap_context(str(project)))
    assert first["stage"] == "interpreting"
    failure = json.loads(server.bootstrap_context(str(project), snapshot_id="bad", findings=[], finish=True))
    assert failure["saved"] is False
    assert "error" in failure


def test_corrupt_store_is_never_overwritten(project):
    bootstrap.run(str(project), "test")
    path = store.sidecar_path("store", slug=store.repo_slug(str(project)))
    path.write_text('{broken', encoding="utf-8")
    with pytest.raises(ValueError):
        bootstrap.run(str(project), "test")
    assert path.read_text(encoding="utf-8") == '{broken'


def test_inferred_capture_cannot_enforce_or_be_automatically_shared(project):
    scan = scan_rule(project)
    did = finish(project, scan, [finding(project, scan)])["outcomes"][0]["id"]
    entry = store.entry_by_id(store.load(str(project))["entries"], did)
    assert not policy.is_trusted(entry)
    assert not guard_engine._guard_trusted(entry)
    assert share_policy.eligibility(entry, {"repo_key": "repo"}, [],
                                    repo_key="repo", is_global=False).reason_code == "ineligible_revision"
    with pytest.raises(ValueError, match="only approved"):
        guard_engine.arm_guard(str(project), did, "regex", "email")
    assert not store.entry_by_id(store.load(str(project))["entries"], did).get("guard")


def test_new_file_during_analysis_invalidates_snapshot(project):
    scan = scan_rule(project)
    (project / "direct_email.py").write_text("send_synchronously()\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Snapshot changed"):
        finish(project, scan, [finding(project, scan)])


def test_new_files_after_empty_bootstrap_trigger_next_session_analysis(project):
    scan = bootstrap.run(str(project), "test")
    finish(project, scan, [])
    (project / "AGENTS.md").write_text("# Rules\n- Never use floating point for money.\n", encoding="utf-8")
    assert "call bootstrap_context now" in store.session_start_payload(str(project))["context"]


def test_resume_preserves_incomplete_interpretation_instruction(project):
    bootstrap.run(str(project), "test")
    assert "incomplete until that report is saved" in store.session_start_payload(str(project), "resume")["context"]


def test_external_authorization_withdrawal_suppresses_old_inference(project, tmp_path):
    external = tmp_path / "rules.md"
    external.write_text("# Rules\n- Never log customer account numbers.\n", encoding="utf-8")
    scan = bootstrap.run(str(project), "test", external_paths=[str(external)])
    row = finding(project, scan)
    did = finish(project, scan, [row])["outcomes"][0]["id"]
    bootstrap.run(str(project), "again", external_paths=[])
    entry = store.entry_by_id(store.load(str(project))["entries"], did)
    assert entry["bootstrap_withheld"]
    assert "customer account numbers" not in store.get_context(str(project))
    assert len(entry["revisions"]) == 1


def test_changed_inference_withheld_until_grounded_reanalysis(project):
    scan = scan_rule(project)
    row = finding(project, scan)
    did = finish(project, scan, [row])["outcomes"][0]["id"]
    (project / "billing.py").write_text("def charge(db, amount):\n    db.send_email()\n", encoding="utf-8")
    fresh = bootstrap.run(str(project), "again")
    assert store.entry_by_id(store.load(str(project))["entries"], did)["bootstrap_withheld"]
    assert "transactional outbox" not in store.get_context(str(project), _active_only=True)
    row = {**finding(project, fresh), "assessment": "contradicted", "question": "Restore outbox delivery?"}
    row["sources"][1] = ref(project, "billing.py", 1, 2, "implementation")
    finish(project, fresh, [row])
    entry = store.entry_by_id(store.load(str(project))["entries"], did)
    assert entry["status"] == "pending_approval" and not entry.get("bootstrap_withheld")


def test_human_policy_discrepancy_keeps_standing_decision_unchanged(project):
    store.update_decision(str(project), "Always send billing email synchronously.", "human", "constraint", created_by="human")
    standing = copy.deepcopy(store.load(str(project))["entries"][0])
    scan = bootstrap.run(str(project), "test")
    row = {"topic": "billing-policy-discrepancy", "content": standing["content"],
           "kind": "inferred", "subtype": "constraint", "scope": "billing service",
           "assessment": "contradicted", "reason": "Implementation uses an outbox instead",
           "against_decision_id": standing["id"], "question": "Keep outbox or restore synchronous delivery?",
           "sources": [ref(project, "billing.py", 1, 4, "implementation")]}
    result = finish(project, scan, [row])
    assert result["outcomes"][0]["outcome"] == "stored"
    assert store.entry_by_id(store.load(str(project))["entries"], standing["id"]) == standing
    text = store.format_pending_review(str(project))
    assert "Standing decision" in text and "db.insert_outbox" in text


def test_repeat_conflict_does_not_rearm_question(project, monkeypatch):
    scan = scan_rule(project, "Always send billing email synchronously.")
    row = finding(project, scan, assessment="contradicted")
    row["question"] = "Keep outbox or restore synchronous delivery?"
    finish(project, scan, [row])
    fresh = bootstrap.run(str(project), "again")
    calls = []
    monkeypatch.setattr(store, "touch_pending_review", lambda *_: calls.append(1))
    assert finish(project, fresh, [row])["outcomes"][0]["outcome"] == "unchanged"
    assert not calls


def test_full_store_preserves_all_entries_and_reports_failure(project, monkeypatch):
    scan = scan_rule(project)
    before = store.load(str(project))
    monkeypatch.setattr(store, "MAX_ENTRIES", len(before["entries"]))
    with pytest.raises(ValueError, match="full"):
        finish(project, scan, [finding(project, scan)])
    assert store.load(str(project)) == before


def test_unknown_saved_state_is_never_reset_to_empty(project):
    bootstrap.run(str(project), "test")
    data = store.load(str(project))
    data["bootstrap_scan"]["reported"] = "corrupt"
    store.save(str(project), data)
    with pytest.raises(ValueError, match="Malformed bootstrap"):
        bootstrap.run(str(project), "test")
    assert store.load(str(project))["bootstrap_scan"]["reported"] == "corrupt"


@pytest.mark.parametrize("field,value", [
    ("content", ""), ("content", "x" * 1501), ("scope", None), ("reason", ""),
    ("kind", "human"), ("subtype", "policy"), ("assessment", "approved"),
    ("candidate_id", "invented"), ("approved_by", "human"), ("sources", []),
    ("replaces", "missing"), ("against_decision_id", "missing"),
])
def test_report_validation_never_mints_unsupported_trust(project, field, value):
    scan = scan_rule(project)
    row = {**finding(project, scan), field: value}
    before = store.load(str(project))
    with pytest.raises((ValueError, TypeError)):
        finish(project, scan, [row])
    assert store.load(str(project)) == before


def test_console_correction_protects_the_original_identity(project):
    scan = scan_rule(project)
    did = finish(project, scan, [finding(project, scan)])["outcomes"][0]["id"]
    ok, _, edited = store.edit_decision(str(project), did, content="Keep billing notifications in an outbox.")
    assert ok and edited["revision"] == 2 and edited["approved_by"] == "human"
    scan = bootstrap.run(str(project), "again")
    assert finish(project, scan, [finding(project, scan)])["outcomes"][0]["outcome"] == "protected"


def test_global_decision_is_available_as_a_conflict_basis(project):
    store.update_global_decision("Never deliver email in a background worker.", "human", "constraint")
    standing = store.load_global()["entries"][0]
    scan = bootstrap.run(str(project), "test")
    assert standing["id"] in {e["id"] for e in scan["decisions"]}
    row = {"topic": "global-delivery-conflict", "content": "Billing uses outbox delivery despite the standing global restriction.",
           "kind": "inferred", "subtype": "constraint", "scope": "billing",
           "assessment": "contradicted", "reason": "The transaction inserts an outbox message",
           "against_decision_id": standing["id"], "question": "Is billing an intentional exception?",
           "sources": [ref(project, "billing.py", 1, 4, "implementation")]}
    assert finish(project, scan, [row])["outcomes"][0]["outcome"] == "stored"
    assert store.load_global()["entries"][0] == standing


def test_configuration_excerpt_does_not_mistake_multiline_example_for_live_value(project):
    (project / "pyproject.toml").write_text(
        '[project]\nname = "ledger"\ndescription = """\nline-length = 88\n"""\n'
        '[tool.ruff]\nline-length = 100\n', encoding="utf-8")
    scan = scan_rule(project, "Use Ruff line length 88.")
    fact = scan["facts"][0]
    assert "100" in fact["content"]
    assert "line-length = 100" in fact["sources"][0]["quote"]
    assert scan["candidates"][0]["comparison"]["assessment"] == "contradicted"


def test_comment_only_code_is_not_behavior_evidence(project):
    (project / "notes.py").write_text("# All billing email is synchronous.\n", encoding="utf-8")
    scan = bootstrap.run(str(project), "test")
    row = {"topic": "email", "content": "Billing sends email synchronously.", "kind": "observed",
           "subtype": "pattern", "scope": "billing", "assessment": "supported", "reason": "Comment says so",
           "sources": [ref(project, "notes.py", 1, role="implementation")]}
    with pytest.raises(ValueError, match="Comment-only"):
        finish(project, scan, [row])


def test_model_cannot_change_a_deterministically_parsed_fact(project):
    scan = bootstrap.run(str(project), "test")
    fact = copy.deepcopy(scan["facts"][0])
    fact["content"] = "Python 2.7 is required."
    with pytest.raises(ValueError, match="parsed configuration fact"):
        finish(project, scan, [fact])


def test_identical_rule_in_different_subsystems_keeps_both_scopes(project):
    for scope in ("billing", "inventory"):
        (project / scope).mkdir()
        (project / scope / "AGENTS.md").write_text("# Rules\n- Always validate identifiers before writes.\n", encoding="utf-8")
    scan = bootstrap.run(str(project), "test")
    assert len(scan["candidates"]) == 2
    rows = []
    for candidate in scan["candidates"]:
        row = finding(project, {"candidates": [candidate]})
        row["scope"] = str(Path(candidate["source_file"]).parent)
        rows.append(row)
    result = finish(project, scan, rows)
    assert len({r["id"] for r in result["outcomes"]}) == 2


def test_scalar_conflict_requires_the_actual_configuration_evidence(project):
    scan = scan_rule(project, "Use Ruff line length 88.")
    row = finding(project, scan, assessment="contradicted")
    row["question"] = "Keep configured 100 or adopt documented 88?"
    with pytest.raises(ValueError, match="parsed configuration evidence"):
        finish(project, scan, [row])
    row["sources"][1] = scan["candidates"][0]["comparison"]["sources"][0]
    assert finish(project, scan, [row])["outcomes"][0]["assessment"] == "contradicted"


def test_an_obvious_fact_cannot_carry_an_approval_question(project):
    scan = bootstrap.run(str(project), "test")
    row = {**scan["facts"][0], "question": "Does this project use Python?"}
    with pytest.raises(ValueError, match="Only material conflicts"):
        finish(project, scan, [row])


def test_real_mcp_scan_interpretation_and_retrieval_roundtrip(project, tmp_path):
    """Exercise actual tool schemas and JSON transport, with an isolated child-process store."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    (project / "ARCHITECTURE.md").write_text(
        "# Decisions\n- Use a transactional outbox for billing email.\n", encoding="utf-8")
    command = ("import sys; from pathlib import Path; from contexer import store; "
               "store.STORE_DIR=Path(sys.argv[1]); from contexer import server; server.mcp.run()")

    async def exercise():
        params = StdioServerParameters(command=sys.executable, args=["-c", command, str(tmp_path / "mcp-store")],
                                       cwd=str(project), env={"CONTEXER_NO_UPDATE_CHECK": "1"})
        async with stdio_client(params) as (reader, writer):
            async with ClientSession(reader, writer) as client:
                await client.initialize()
                first = await client.call_tool("bootstrap_context", {"repo_path": str(project)})
                scan = json.loads(first.content[0].text)
                assert scan["stage"] == "interpreting"
                response = await client.call_tool("bootstrap_context", {
                    "repo_path": str(project), "snapshot_id": scan["snapshot_id"],
                    "findings": [finding(project, scan)], "finish": True})
                receipt = json.loads(response.content[0].text)
                assert receipt["stage"] == "reported_complete"
                recall = await client.call_tool("get_context", {"repo_path": str(project), "query": "outbox"})
                assert "Not human-approved policy" in recall.content[0].text
                assert "Evidence billing.py:2" in recall.content[0].text

    asyncio.run(asyncio.wait_for(exercise(), timeout=20))


@pytest.mark.parametrize("source", ["ai", "scan", "bootstrap", "plan"])
def test_generic_capture_cannot_bypass_bootstrap_evidence_or_human_correction(project, source):
    scan = scan_rule(project)
    did = finish(project, scan, [finding(project, scan)])["outcomes"][0]["id"]
    store.approve_decision(str(project), did, "edit", "Use outbox delivery for billing notifications.")
    before = store.entry_by_id(store.load(str(project))["entries"], did)
    _, _, meta = store.update_decision_with_meta(str(project), "Send all billing email synchronously.",
                                                 "ai", "convention", created_by=source, replace_id=did)
    assert "Bootstrap capture unchanged" in meta["refusal_ack"]
    assert store.entry_by_id(store.load(str(project))["entries"], did) == before


def test_repetition_of_an_inference_is_not_independent_confirmation(project):
    bootstrap.run(str(project), "test")
    entry = store.load(str(project))["entries"][0]
    entry["occurrence_count"] = 100
    entry["session_ids"] = ["one", "two", "three"]
    assert revisions.compute_confidence(entry)[0] == 30


def disputed_rules(project):
    (project / "ARCHITECTURE.md").write_text(
        "# Decisions\n- Always send billing email synchronously.\n"
        "- Use a transactional outbox for billing email.\n"
        "- Never log customer email addresses.\n", encoding="utf-8")
    scan = bootstrap.run(str(project), "test")
    rows = [finding(project, {**scan, "candidates": [c]}) for c in scan["candidates"]]
    rows[0].update(assessment="contradicted", question="Should billing use synchronous delivery or an outbox?")
    rows[0]["sources"].append(ref(project, "ARCHITECTURE.md", 3))
    rows[2]["assessment"] = "unverified"
    return scan, rows


@pytest.mark.parametrize("first", [None, 0, 1])
def test_document_dispute_keeps_both_prescriptions_pending_in_any_batch_order(project, first):
    scan, rows = disputed_rules(project)
    if first is not None:
        scan = bootstrap.run(str(project), "test", snapshot_id=scan["snapshot_id"], findings=[rows[first]])
    receipt = finish(project, scan, [r for i, r in enumerate(rows) if i != first])
    entries = store.load(str(project))["entries"]
    pending = [e for e in entries if store.entry_status(e) == "pending_approval"]
    assert len(pending) == 2
    assert len(receipt["clarifications"]) == 1
    assert {d["id"] for d in receipt["clarifications"][0]["decisions"]} == {e["id"] for e in pending}
    assert all(e["bootstrap"]["disputed_by"] for e in pending)
    assert next(e for e in entries if "Never log" in e["content"])["status"] == "suggested"
    assert "Use a transactional outbox" not in store.session_start_payload(str(project))["context"]
    before = copy.deepcopy(entries)
    again = finish(project, receipt, rows)
    assert not again["clarifications"]
    assert store.load(str(project))["entries"] == before


def test_revised_assessment_removes_derived_dispute_links_and_questions(project):
    scan, rows = disputed_rules(project)
    receipt = finish(project, scan, rows)
    # Same evidence, now interpreted as differently scoped prescriptions rather than
    # a dispute. Derived links from the earlier report must not survive re-analysis.
    rows[0].update(assessment="not_comparable", reason="Synchronous requirement applies to another environment")
    rows[0].pop("question")
    receipt = finish(project, receipt, [rows[0]])
    assert not receipt["clarifications"]
    assert not store.get_pending_decisions(str(project))
    assert all(not e.get("bootstrap", {}).get("disputed_by") for e in store.load(str(project))["entries"])


def test_dispute_does_not_demote_an_explicit_human_choice(project):
    scan, rows = disputed_rules(project)
    first = bootstrap.run(str(project), "test", snapshot_id=scan["snapshot_id"], findings=[rows[1]])
    did = first["outcomes"][0]["id"]
    assert store.approve_decision(str(project), did, "approve")[0]
    fresh = bootstrap.run(str(project), "test")
    receipt = finish(project, fresh, rows)
    entry = store.entry_by_id(store.load(str(project))["entries"], did)
    assert entry["approved_by"] == "human" and store.entry_status(entry) != "pending_approval"
    assert len(receipt["clarifications"][0]["decisions"]) == 1


def test_historical_dispute_cannot_block_current_guidance(project):
    scan, rows = disputed_rules(project)
    scan = store.load(str(project))["bootstrap_scan"]
    valid = bootstrap.validate_findings(scan, rows)
    valid[0]["historical"] = True
    linked = bootstrap._link_disputes(scan, valid)
    assert not any(r.get("disputed_by") for r in linked.values())


def test_document_prescription_cannot_be_laundered_as_observed_fact(project):
    scan = scan_rule(project)
    row = finding(project, scan)
    row["kind"] = "observed"
    with pytest.raises(ValueError, match="prescription is inferred"):
        finish(project, scan, [row])


def test_first_run_invitation_is_actionable_but_not_completion(project):
    payload = store.session_start_payload(str(project))
    assert 'ask "Run Contexer bootstrap"' in payload["status"]
    assert "decisions, rules, and conventions" in payload["status"]
    assert "next prompt" not in payload["status"]
    assert "bootstrap_scan" not in store.load(str(project))
    assert "call bootstrap_context now" in store.bootstrap_prompt_payload(str(project))["context"]
    assert not store.bootstrap_prompt_payload(str(project))["context"]
    # Ignoring both deliveries does not acknowledge or complete the work.
    assert "call bootstrap_context now" in store.session_start_payload(str(project))["context"]


def test_completed_bootstrap_stops_first_prompt_fallback(project):
    store.session_start_payload(str(project))
    scan = scan_rule(project)
    finish(project, scan, [finding(project, scan)])
    assert not store.bootstrap_prompt_payload(str(project))["context"]


def test_incomplete_bootstrap_retries_after_notice_and_in_later_session(project):
    store.session_start_payload(str(project))
    scan_rule(project)  # Facts saved, interpretation never submitted.
    assert "call bootstrap_context now" in store.bootstrap_prompt_payload(str(project))["context"]
    assert not store.bootstrap_prompt_payload(str(project))["context"]
    assert "call bootstrap_context now" in store.session_start_payload(str(project))["context"]
    assert "call bootstrap_context now" in store.bootstrap_prompt_payload(str(project))["context"]


def test_legacy_notice_marker_does_not_suppress_first_prompt(project):
    store.ensure_store_dir()
    store.sidecar_path("bootstrap_offered", slug=store.repo_slug(str(project))).touch()
    assert "call bootstrap_context now" in store.bootstrap_prompt_payload(str(project))["context"]


def test_first_prompt_capture_does_not_masquerade_as_bootstrap_completion(project):
    store.session_start_payload(str(project))
    store.update_decision(str(project), "Always use Conventional Commits.", "user", "convention")
    assert "bootstrap_scan" not in store.load(str(project))
    assert "call bootstrap_context now" in store.bootstrap_prompt_payload(str(project))["context"]


@pytest.mark.parametrize("source", ["startup", "resume", "compact"])
def test_legacy_decisions_do_not_prevent_automatic_bootstrap(project, source):
    store.update_decision(str(project), "Always use Conventional Commits.", "user", "convention")
    before = store.load(str(project))["entries"]
    assert "call bootstrap_context now" in store.session_start_payload(str(project), source)["context"]
    assert "call bootstrap_context now" in store.bootstrap_prompt_payload(str(project))["context"]
    assert not store.bootstrap_prompt_payload(str(project))["context"]
    assert store.load(str(project))["entries"] == before


@pytest.mark.parametrize("change", ["modify", "delete", "symlink"])
def test_startup_withholds_stale_inference_before_any_prompt_can_use_it(project, change, tmp_path):
    scan = scan_rule(project)
    did = finish(project, scan, [finding(project, scan)])["outcomes"][0]["id"]
    before = copy.deepcopy(store.entry_by_id(store.load(str(project))["entries"], did))
    path = project / "billing.py"
    if change == "modify":
        path.write_text("send_email_synchronously()\n")
    else:
        path.unlink()
        if change == "symlink":
            target = tmp_path / "outside.py"
            target.write_text("send_email_synchronously()\n")
            path.symlink_to(target)
    payload = store.session_start_payload(str(project))
    assert before["content"] not in payload["context"]
    assert "call bootstrap_context now" in payload["context"]
    assert before["content"] not in store._render_prompt_decisions(str(project), [did])
    entry = store.entry_by_id(store.load(str(project))["entries"], did)
    assert entry["bootstrap_withheld"]
    assert entry["revisions"] == before["revisions"]
    assert entry["status"] == before["status"]  # derived suppression, not human dismissal


def test_startup_freshness_preserves_human_correction(project):
    scan = scan_rule(project)
    did = finish(project, scan, [finding(project, scan)])["outcomes"][0]["id"]
    store.approve_decision(str(project), did, "edit", "Keep using an outbox for billing email.")
    (project / "billing.py").write_text("send_email_synchronously()\n")
    store.session_start_payload(str(project))
    entry = store.entry_by_id(store.load(str(project))["entries"], did)
    assert not entry.get("bootstrap_withheld")
    assert entry["approved_by"] == "human"


@pytest.mark.parametrize("failure", ["lock", "save"])
def test_startup_freshness_fails_closed_without_writable_bookkeeping(project, monkeypatch, failure):
    scan = scan_rule(project)
    did = finish(project, scan, [finding(project, scan)])["outcomes"][0]["id"]
    (project / "billing.py").write_text("send_email_synchronously()\n")
    data = store.load(str(project))

    def denied(*args, **kwargs):
        raise PermissionError("read only")

    monkeypatch.setattr(store, "store_lock" if failure == "lock" else "save", denied)
    view = bootstrap.refresh_for_session(str(project), data)
    assert store.entry_status(store.entry_by_id(view["entries"], did)) == "ignored"
    assert "call bootstrap_context now" in bootstrap.directive(str(project), view)
    assert not store.entry_by_id(data["entries"], did).get("bootstrap_withheld")


def test_json_facts_cite_actual_object_members_not_earlier_decoys(project):
    text = ('{\n  "keywords": ["node", "dependencies"],\n'
            '  "example": {"engines": {"node": ">=16"}, "dependencies": {"fake": "*"}},\n'
            '  "engines": {\n    "node": ">=20"\n  },\n'
            '  "dependencies": {\n    "express": "^5.0.0",\n    "zod": "^4.0.0"\n  }\n}\n')
    (project / "package.json").write_text(text)
    scan = bootstrap.run(str(project), "test")
    facts = {f["topic"]: f for f in scan["facts"]}
    assert facts["node-requirement"]["sources"][0] == ref(project, "package.json", 5, role="config")
    assert facts["node-dependencies"]["sources"][0] == ref(project, "package.json", 7, 10, "config")
    assert "express, zod" in facts["node-dependencies"]["content"]


def test_json_location_matches_escaped_keys_and_last_duplicate_wins(project):
    (project / "package.json").write_text(
        '{\n"engines": {"node": ">=14"},\n"engines": {\n'
        '"node": ">=18",\n"no\\u0064e": ">=22"\n}\n}\n')
    scan = bootstrap.run(str(project), "test")
    fact = next(f for f in scan["facts"] if f["topic"] == "node-requirement")
    assert fact["content"] == "Node requirement is >=22."
    assert fact["sources"][0] == ref(project, "package.json", 5, role="config")


def test_oversized_json_excerpt_omits_fact_instead_of_citing_only_heading(project):
    dependencies = ",\n".join(f'"package-{i}": "1.0.0"' for i in range(30))
    (project / "package.json").write_text('{"dependencies": {\n' + dependencies + '\n}}')
    scan = bootstrap.run(str(project), "test")
    assert not any(f["topic"] == "node-dependencies" for f in scan["facts"])


@pytest.mark.parametrize("replace", [False, True])
def test_scan_serializes_external_authorization_with_concurrent_revocation(project, tmp_path, monkeypatch, replace):
    external = tmp_path / "shared.md"
    external.write_text("# Rules\n- Never log customer emails.\n")
    replacement = tmp_path / "new-shared.md"
    replacement.write_text("# Rules\n- Keep receipt data private.\n")
    requested = [str(replacement)] if replace else []
    bootstrap.run(str(project), "setup", external_paths=[str(external)])
    scanning, revoke_attempted, release = threading.Event(), threading.Event(), threading.Event()
    original_snapshot, original_lock = bootstrap.snapshot, store.store_lock
    local = threading.local()

    def paused_snapshot(*args, **kwargs):
        if getattr(local, "role", "") == "scan":
            scanning.set()
            assert getattr(local, "locked", False), "authorization and snapshot must be read under the lock"
            assert release.wait(5)
        return original_snapshot(*args, **kwargs)

    @contextmanager
    def traced_lock(*args, **kwargs):
        if getattr(local, "role", "") == "revoke":
            revoke_attempted.set()
        with original_lock(*args, **kwargs):
            local.locked = True
            try:
                yield
            finally:
                local.locked = False

    def run(role, **kwargs):
        local.role = role
        return bootstrap.run(str(project), role, **kwargs)

    monkeypatch.setattr(bootstrap, "snapshot", paused_snapshot)
    monkeypatch.setattr(store, "store_lock", traced_lock)
    with ThreadPoolExecutor(max_workers=2) as workers:
        first = workers.submit(run, "scan")
        try:
            assert scanning.wait(5)
            second = workers.submit(run, "revoke", external_paths=requested)
            assert revoke_attempted.wait(5)
        finally:
            release.set()
        first.result(timeout=5)
        second.result(timeout=5)
    saved = store.load(str(project))["bootstrap_scan"]
    assert saved["external_paths"] == requested
    assert str(external) not in saved["files"]


@pytest.mark.parametrize("newline", ["\n", "\r\n", "\r"])
def test_json_locator_uses_logical_source_lines(project, newline):
    text = newline.join(['{', '"engines": {', '"node": ">=20"', '}', '}'])
    (project / "package.json").write_bytes(text.encode())
    scan = bootstrap.run(str(project), "test")
    fact = next(f for f in scan["facts"] if f["topic"] == "node-requirement")
    assert fact["sources"][0]["line"] == 3
    assert fact["sources"][0]["quote"] == '\"node\": \">=20\"'


def test_new_sources_persist_rescan_request_without_withholding_unchanged_facts(project):
    scan = bootstrap.run(str(project), "test")
    finish(project, scan, [])
    before = store.load(str(project))["entries"]
    (project / "new_subsystem.py").write_text("DATABASE_BACKEND = 'postgres'\n")
    payload = store.session_start_payload(str(project))
    assert "call bootstrap_context now" in payload["context"]
    saved = store.load(str(project))
    assert saved["bootstrap_scan"]["refresh_needed"]
    assert saved["entries"] == before
    assert "call bootstrap_context now" in store.bootstrap_prompt_payload(str(project))["context"]
    fresh = bootstrap.run(str(project), "test")
    assert fresh["stage"] == "interpreting"
    finish(project, fresh, [])
    assert not bootstrap.directive(str(project))


def test_valid_final_report_clears_refresh_request_after_transient_source_change(project):
    scan = bootstrap.run(str(project), "test")
    receipt = finish(project, scan, [])
    added = project / "temporary.py"
    added.write_text("ENABLED = True\n")
    store.session_start_payload(str(project))
    added.unlink()
    # The original snapshot is current again; a validated final report can settle it.
    finish(project, receipt, [])
    assert not bootstrap.directive(str(project))
