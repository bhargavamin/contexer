"""Contract 04's complete, explicitly requested pilot workflow over isolated fixtures."""

from pathlib import Path

import pytest

from contexer import decision_impact, policy_api, server, store, working_set
from tests.test_policy_api import _arm, _seed


def _enable(repo):
    target = store.store_dir() / "config.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        "[diagnostics]\n"
        "decision_impact = true\n\n"
        "[policy]\n"
        f'artifact_read_roots = ["{repo}"]\n')


def _fixture(repo, *, pattern="FORBIDDEN"):
    _enable(repo)
    path = Path(repo) / "src" / "app.py"
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = _seed(
        repo, "Never include the FORBIDDEN marker in the application file",
        title="No forbidden marker", source_files=["src/app.py"])
    _arm(repo, entry["id"], pattern=pattern, paths="src/app.py")
    return entry, path


def _receipt(text: str, marker: str) -> str:
    return text.rsplit(marker, 1)[1].split("]", 1)[0].strip()


def _evaluation_receipt(text: str) -> str:
    return text.rsplit("decision_impact_receipt:", 1)[1].splitlines()[0].strip()


@pytest.mark.parametrize(
    ("content", "expected_result", "expected_verdict"),
    [("ready\n", "satisfied", "allow"),
     ("FORBIDDEN\n", "violated", "block")],
)
def test_complete_requested_workflow_reports_a_specific_checked_condition(
        tmp_repo, content, expected_result, expected_verdict):
    entry, path = _fixture(tmp_repo)
    calls = []

    calls.append("lookup")
    guidance = server.get_context(tmp_repo, query="FORBIDDEN")
    guidance_id = _receipt(guidance, "[Contexer decision-impact receipt:")

    path.write_text(content)
    calls.append("evaluation")
    evaluation = server.evaluate_policy(
        tmp_repo, operation="commit", artifact_path="src/app.py",
        guidance_refs=[guidance_id])
    assert f"verdict: {expected_verdict}" in evaluation
    evaluation_id = _evaluation_receipt(evaluation)

    calls.append("report")
    report = server.get_decision_impact(tmp_repo, receipt_id=evaluation_id)
    assert f"decision {entry['id']}" in report
    assert f"result={expected_result}" in report
    assert "evidence=verified" in report
    assert f"caller-linked to {guidance_id}" in report
    assert "not proof that Contexer improved" in report
    assert calls == ["lookup", "evaluation", "report"]


def test_unsupported_condition_is_reported_unverified_without_entering_matcher(
        tmp_repo, monkeypatch):
    _entry, path = _fixture(tmp_repo, pattern="(a+)+$")
    guidance = server.get_context(tmp_repo, query="FORBIDDEN")
    guidance_id = _receipt(guidance, "[Contexer decision-impact receipt:")
    path.write_text("a" * 30 + "!\n")

    from contexer import policy
    original = policy.rule_matches

    def guarded(rule, content):
        if rule.get("pattern") == "(a+)+$":
            pytest.fail("unsupported regex reached the live matcher")
        return original(rule, content)

    monkeypatch.setattr(policy, "rule_matches", guarded)
    evaluation = server.evaluate_policy(
        tmp_repo, operation="commit", artifact_path="src/app.py",
        guidance_refs=[guidance_id])
    assert "evaluation_status: partial" in evaluation
    evaluation_id = _evaluation_receipt(evaluation)
    report = server.get_decision_impact(tmp_repo, receipt_id=evaluation_id)
    assert "result=unchecked" in report and "evidence=unverified" in report


def test_edit_or_lookup_alone_never_runs_an_evaluation_or_report(tmp_repo, monkeypatch):
    _entry, path = _fixture(tmp_repo)
    monkeypatch.setattr(
        policy_api, "evaluate_operation",
        lambda *a, **k: pytest.fail("an edit or lookup automatically ran a check"))
    monkeypatch.setattr(
        decision_impact, "report",
        lambda *a, **k: pytest.fail("an edit or lookup automatically opened a report"))

    path.write_text("FORBIDDEN\n")
    guidance = server.get_context(tmp_repo, query="FORBIDDEN")
    assert "No forbidden marker" in guidance


def test_indexed_prompt_records_only_the_final_rendered_guidance(tmp_repo, monkeypatch):
    _fixture(tmp_repo)
    seen = []
    monkeypatch.setattr(
        decision_impact, "append",
        lambda repo, envelope, **_kw: seen.append(envelope) or "receipt")

    rendered = store.get_context_for_prompt(
        tmp_repo, "why is the FORBIDDEN marker prohibited?", "impact-session", host="codex")

    assert rendered.startswith("[Contexer: auto-fetched for this question]")
    assert "decision-impact receipt" not in rendered
    assert len(seen) == 1 and seen[0]["kind"] == "guidance"
    assert seen[0]["route"] == "indexed_prompt"
    assert seen[0]["host"] == "codex"
    assert seen[0]["decisions"][0]["revision_id"] != "legacy"


def test_selected_guidance_skipped_by_rendering_gets_no_impact_credit(tmp_repo, monkeypatch):
    _fixture(tmp_repo)
    monkeypatch.setattr(
        store, "_render_prompt_decisions_with_records", lambda *a, **k: ("", []))
    monkeypatch.setattr(
        decision_impact, "append",
        lambda *a, **k: pytest.fail("render-skipped guidance was recorded as prepared"))

    assert store.get_context_for_prompt(
        tmp_repo, "why is the FORBIDDEN marker prohibited?", "skipped-session") == ""
    assert working_set.records(tmp_repo, "skipped-session") == []


def test_ledger_failure_cannot_remove_guidance_or_change_a_check_verdict(tmp_repo, monkeypatch):
    _entry, path = _fixture(tmp_repo)
    monkeypatch.setattr(
        decision_impact, "append",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("disk unavailable")))

    guidance = server.get_context(tmp_repo, query="FORBIDDEN")
    assert "No forbidden marker" in guidance
    assert "decision-impact receipt" not in guidance

    path.write_text("FORBIDDEN\n")
    evaluation = server.evaluate_policy(
        tmp_repo, operation="commit", artifact_path="src/app.py")
    assert "verdict: block" in evaluation
    assert "decision_impact_receipt" not in evaluation
