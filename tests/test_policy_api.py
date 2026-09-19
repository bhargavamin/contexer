"""Tests for contexer/policy_api.py - the shared facade both general policy surfaces sit on.

`tests/test_policy.py` already pins what the pure evaluator decides. What can only be pinned
HERE is everything the facade adds around it: that a malformed request comes back as errors
instead of an exception at a surface, that both stores participate (a global rule governs
every repo, and losing it would be silent), that the bounds hold for a caller who skipped the
tool and the CLI entirely, and that rendering scrubs while evaluation does not.

That last pair is the one that has to be tested together. Scrubbing on the way IN would leave
a `secret` rule matching `[REDACTED:...]` and finding nothing, and scrubbing nowhere would
print the key. Only asserting BOTH - verdict `block` AND the key absent from the render -
says the boundary is in the right place.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest

from contexer import guard_engine, policy, policy_api, store


AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def _seed(repo, content, *, title="A decision", created_by="human", status="approved",
          source_files=None, global_store=False):
    """One approved, trusted decision in the repo (or global) store, built through the real
    entry constructor so revisions/status/source come out shaped like production data."""
    entry = store._new_decision_entry(content, "sess-policy", "constraint",
                                      created_by=created_by, status=status, title=title)
    if source_files is not None:
        entry["source_files"] = source_files
    if global_store:
        data = store.load_global()
        data["entries"].append(entry)
        store.save_global(data)
    else:
        data = store.load(repo)
        data["entries"].append(entry)
        store.save(repo, data)
    return entry


def _arm(repo, entry_id, check_type="regex", **kw):
    return guard_engine.arm_guard(repo, entry_id, check_type, **kw)


# ── validation ───────────────────────────────────────────────────────────────────

class TestValidationNeverRaisesAtTheSurface:
    """A surface must be able to hand a request over without a try/except: an MCP tool that
    raises is a tool error the model cannot act on, and a CLI that traces back is worse."""

    def test_unknown_operation_returns_errors(self, tmp_repo):
        result = policy_api.evaluate_operation(tmp_repo, operation="rm-rf")
        assert result["errors"] and "operation must be one of" in result["errors"][0]

    def test_a_rejected_request_is_error_status_never_a_clean_allow(self, tmp_repo):
        result = policy_api.evaluate_operation(tmp_repo, operation="rm-rf")
        # The verdict alone must never be read as a pass: nothing was judged at all.
        assert result["evaluation_status"] == "error"
        assert result["matches"] == []

    def test_a_rejected_request_keeps_the_full_answer_shape(self, tmp_repo):
        """A renderer handles ONE shape. A short-circuit that returned a bare error dict would
        make every caller special-case the gate."""
        rejected = policy_api.evaluate_operation(tmp_repo, operation="")
        ok = policy_api.evaluate_operation(tmp_repo, operation="commit")
        assert set(rejected) == set(ok)

    def test_no_repo_is_reported_as_an_error_not_a_traceback(self, tmp_repo, monkeypatch):
        monkeypatch.setattr(store, "resolve_repo", lambda p: "")
        result = policy_api.evaluate_operation("", operation="commit")
        assert result["errors"] == ["repo path not detected"]

    @pytest.mark.parametrize("kwargs", [
        {"operation": "commit", "files": ["/abs/path.py"]},
        {"operation": "commit", "files": ["../escape.py"]},
        {"operation": "commit", "artifact_kind": "not-a-kind", "artifact": "x"},
        {"operation": "commit", "intent": "i" * (policy._MAX_INTENT_CHARS + 1)},
    ])
    def test_every_malformed_request_returns_errors_rather_than_raising(self, tmp_repo, kwargs):
        assert policy_api.evaluate_operation(tmp_repo, **kwargs)["errors"]


class TestBoundsHoldBelowTheSurface:
    """The MCP schema and the CLI flags each bound only their own callers. The facade is the
    chokepoint every caller funnels through, which is why the same bounds are applied again
    here - the `remote.bound_source_files` shape."""

    def test_over_length_file_list_is_bounded_for_a_caller_that_skipped_both_surfaces(
            self, tmp_repo):
        result = policy_api.evaluate_operation(
            tmp_repo, operation="commit", files=[f"f{i}.py" for i in range(policy._MAX_FILES + 1)])
        assert any("files has more than" in e for e in result["errors"])

    def test_over_cap_artifact_is_an_error_never_a_truncated_clean_pass(self, tmp_repo):
        entry = _seed(tmp_repo, "Never commit TODO markers")
        _arm(tmp_repo, entry["id"], pattern="TODO")
        oversize = "TODO\n" + "x" * policy.MAX_ARTIFACT_BYTES
        result = policy_api.evaluate_operation(tmp_repo, operation="commit",
                                                artifact_kind="diff", artifact=oversize)
        assert any("exceeds" in e for e in result["errors"])
        assert result["verdict"] == "allow" and result["evaluation_status"] == "error"

    def test_content_without_a_kind_is_an_error_not_silently_dropped(self, tmp_repo):
        result = policy_api.evaluate_operation(tmp_repo, operation="commit", artifact="diff text")
        assert any("artifact.kind" in e for e in result["errors"])


# ── which decisions participate ──────────────────────────────────────────────────

class TestBothStoresParticipate:
    def test_a_repo_armed_rule_blocks(self, tmp_repo):
        entry = _seed(tmp_repo, "Never commit TODO markers", title="No TODOs")
        _arm(tmp_repo, entry["id"], pattern="TODO")
        result = policy_api.evaluate_operation(tmp_repo, operation="commit",
                                                artifact_kind="diff", artifact="+ # TODO fix\n")
        assert result["verdict"] == "block"
        assert [m["decision_id"] for m in result["matches"]] == [entry["id"]]

    def test_a_global_armed_rule_blocks_in_every_repo(self, tmp_repo):
        """A global rule governs every repo everywhere else in the system (session-start
        injection, the commit-time guard). Loading only the repo store here would drop it
        with nothing to notice."""
        entry = _seed(tmp_repo, "Never commit a private key", title="No keys", global_store=True)
        _arm(tmp_repo, entry["id"], pattern="BEGIN RSA PRIVATE KEY")
        result = policy_api.evaluate_operation(
            tmp_repo, operation="commit", artifact_kind="diff",
            artifact="+-----BEGIN RSA PRIVATE KEY-----\n")
        assert result["verdict"] == "block"
        assert [m["decision_id"] for m in result["matches"]] == [entry["id"]]

    def test_an_advisory_decision_anchored_on_a_named_file_warns(self, tmp_repo):
        entry = _seed(tmp_repo, "Keep the router pure", title="Pure router",
                      source_files=["contexer/policy.py"])
        result = policy_api.evaluate_operation(tmp_repo, operation="commit",
                                                files=["contexer/policy.py"])
        assert result["verdict"] == "warn"
        assert [m["decision_id"] for m in result["matches"]] == [entry["id"]]

    def test_an_unapproved_decision_never_speaks(self, tmp_repo):
        entry = _seed(tmp_repo, "Maybe never commit TODO", status="approved")
        _arm(tmp_repo, entry["id"], pattern="TODO")
        data = store.load(tmp_repo)
        store.entry_by_id(data["entries"], entry["id"])["status"] = "pending_approval"
        store.save(tmp_repo, data)
        result = policy_api.evaluate_operation(tmp_repo, operation="commit",
                                                artifact_kind="diff", artifact="TODO\n")
        assert result["verdict"] == "allow" and result["matches"] == []


class TestGapsAreReportedNeverPassedClean:
    def test_an_armed_rule_with_no_artifact_is_omitted(self, tmp_repo):
        entry = _seed(tmp_repo, "Never commit TODO markers")
        _arm(tmp_repo, entry["id"], pattern="TODO")
        result = policy_api.evaluate_operation(tmp_repo, operation="commit")
        assert result["evaluation_status"] == "partial"
        assert [g["reason"] for g in result["unchecked"]] == ["omitted"]

    def test_a_caller_supplied_gap_travels_beside_the_omitted_policies(self, tmp_repo):
        entry = _seed(tmp_repo, "Never commit TODO markers")
        _arm(tmp_repo, entry["id"], pattern="TODO")
        result = policy_api.evaluate_operation(
            tmp_repo, operation="commit",
            unchecked=[{"reason": "too-large", "file": "huge.diff"}])
        reasons = sorted(g["reason"] for g in result["unchecked"])
        assert reasons == ["omitted", "too-large"]
        assert result["evaluation_status"] == "partial"

    def test_a_malformed_gap_row_still_raises_from_the_evaluator(self, tmp_repo):
        """policy.evaluate_policies raises on a caller's broken gap row on purpose - that is a
        bug in the caller, and swallowing it converts it into a false clean verdict. The
        facade must not soften that into a fail-soft no-op."""
        with pytest.raises(ValueError):
            policy_api.evaluate_operation(tmp_repo, operation="commit",
                                           unchecked=[{"reason": "invented"}])


# ── rendering: the egress boundary ───────────────────────────────────────────────

class TestRedactionIsEgressOnly:
    def test_the_secret_check_fires_on_the_real_bytes_and_the_render_hides_them(self, tmp_repo):
        """The pair that proves the boundary sits in the right place. Scrub on the way IN and
        the rule matches `[REDACTED:...]` and reports clean; scrub nowhere and the key is
        printed back out."""
        entry = _seed(tmp_repo, "Never commit credentials", title="No secrets")
        _arm(tmp_repo, entry["id"], "secret")
        diff = f"+AWS_ACCESS_KEY_ID={AWS_KEY}\n"

        result = policy_api.evaluate_operation(tmp_repo, operation="commit",
                                                artifact_kind="diff", artifact=diff)
        assert result["verdict"] == "block", "the check must see the real bytes"

        rendered = policy_api.format_result(result, diff)
        assert AWS_KEY not in rendered
        assert "REDACTED" in rendered, "the quoted line is rendered, with the key removed"

    def test_the_structured_result_is_never_mutated_by_rendering(self, tmp_repo):
        entry = _seed(tmp_repo, "Never commit credentials")
        _arm(tmp_repo, entry["id"], "secret")
        diff = f"+key={AWS_KEY}\n"
        result = policy_api.evaluate_operation(tmp_repo, operation="commit",
                                                artifact_kind="diff", artifact=diff)
        before = repr(result)
        policy_api.format_result(result, diff)
        assert repr(result) == before

    def test_a_secret_in_a_decision_message_is_scrubbed_too(self, tmp_repo):
        entry = _seed(tmp_repo, "Never commit TODO markers")
        _arm(tmp_repo, entry["id"], pattern="TODO", message=f"rotate {AWS_KEY} first")
        result = policy_api.evaluate_operation(tmp_repo, operation="commit",
                                                artifact_kind="diff", artifact="TODO\n")
        assert AWS_KEY not in policy_api.format_result(result)


class TestRenderNamesWhatAnAnswerMeans:
    def test_verdict_status_and_basis_render_together(self, tmp_repo):
        result = policy_api.evaluate_operation(tmp_repo, operation="commit")
        text = policy_api.format_result(result)
        assert "verdict: allow" in text and "evaluation_status: complete" in text
        assert "basis: deterministic" in text

    def test_a_match_names_both_ids(self, tmp_repo):
        entry = _seed(tmp_repo, "Never commit TODO markers", title="No TODOs")
        _arm(tmp_repo, entry["id"], pattern="TODO")
        result = policy_api.evaluate_operation(tmp_repo, operation="commit",
                                                artifact_kind="diff", artifact="TODO\n")
        text = policy_api.format_result(result)
        assert entry["id"] in text and result["matches"][0]["revision_id"] in text
        assert "No TODOs" in text

    def test_gaps_render_with_their_reason_and_say_they_were_not_judged(self, tmp_repo):
        entry = _seed(tmp_repo, "Never commit TODO markers")
        _arm(tmp_repo, entry["id"], pattern="TODO")
        text = policy_api.format_result(
            policy_api.evaluate_operation(tmp_repo, operation="commit"))
        assert "omitted" in text and "NOT judged" in text

    def test_a_rejected_request_renders_its_errors(self, tmp_repo):
        text = policy_api.format_result(
            policy_api.evaluate_operation(tmp_repo, operation="nope"))
        assert "Not evaluated" in text and "operation must be one of" in text


class TestFacadeStaysAboveTheStore:
    def test_policy_stays_pure(self):
        """policy.py must never gain the store this module exists to hold. A module-boundary
        test enforces the leaf rule; this one names the specific import that would break the
        split, because it is the one a future edit would reach for first."""
        source = (policy.__file__ and open(policy.__file__, encoding="utf-8").read()) or ""
        assert "import store" not in source and "contexer.store" not in source


# ── Contract 04: explicitly requested, server-read file evaluation ──────────────

def _grant_file_reads(repo, *, diagnostics=False):
    Path(repo).mkdir(parents=True, exist_ok=True)
    target = store.store_dir() / "config.toml"
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    if diagnostics:
        lines += ["[diagnostics]", "decision_impact = true", ""]
    lines += ["[policy]", f'artifact_read_roots = ["{repo}"]']
    target.write_text("\n".join(lines) + "\n")


def test_requested_file_mode_reads_the_repository_snapshot_and_reports_satisfied(tmp_repo):
    _grant_file_reads(tmp_repo)
    path = Path(tmp_repo) / "src" / "app.py"
    path.parent.mkdir()
    path.write_text("ready\n")
    entry = _seed(tmp_repo, "Never leave TODO markers", source_files=["src/app.py"])
    _arm(tmp_repo, entry["id"], pattern="TODO", paths="src/app.py")

    result = policy_api.evaluate_operation(
        tmp_repo, operation="commit", artifact_path="src/app.py")

    assert result["verdict"] == "allow" and result["evaluation_status"] == "complete"
    assert result["artifact_provenance"]["provenance"] == "authorized_server_read"
    assert result["artifact_provenance"]["path"] == "src/app.py"
    assert result["artifact_provenance"]["bytes"] == len(b"ready\n")


def test_requested_file_mode_observes_a_violation(tmp_repo):
    _grant_file_reads(tmp_repo)
    path = Path(tmp_repo) / "app.py"
    path.write_text("# TODO\n")
    entry = _seed(tmp_repo, "Never leave TODO markers", source_files=["app.py"])
    _arm(tmp_repo, entry["id"], pattern="TODO", paths="app.py")
    result = policy_api.evaluate_operation(tmp_repo, operation="commit", artifact_path="app.py")
    assert result["verdict"] == "block"


def test_file_mode_with_no_applicable_rule_never_fabricates_a_pass_receipt(tmp_repo):
    _grant_file_reads(tmp_repo, diagnostics=True)
    (Path(tmp_repo) / "app.py").write_text("ready\n")

    result = policy_api.evaluate_operation(
        tmp_repo, operation="commit", artifact_path="app.py")
    receipt = policy_api.decision_impact.report(
        tmp_repo, receipt_id=result["receipt_id"])["records"][0]
    assert receipt["conditions"] == []
    assert receipt["coverage"] == "no_applicable_conditions"
    assert "no applicable condition was verified" in \
        policy_api.decision_impact.format_report({
            "status": "ok", "records": [receipt], "next_cursor": ""})


def test_file_mode_requires_a_separate_exact_workspace_grant(tmp_repo):
    Path(tmp_repo).mkdir(parents=True)
    (Path(tmp_repo) / "app.py").write_text("ready\n")
    result = policy_api.evaluate_operation(tmp_repo, operation="commit", artifact_path="app.py")
    assert result["evaluation_status"] == "error"
    assert result["errors"] == ["workspace_not_authorized"]


def test_file_mode_never_uses_the_shared_repo_pointer_even_when_that_root_is_granted(
        tmp_repo, monkeypatch):
    _grant_file_reads(tmp_repo)
    (Path(tmp_repo) / "app.py").write_text("ready\n")
    monkeypatch.setattr(store, "resolve_repo_verbose", lambda _p: (tmp_repo, "pointer"))
    result = policy_api.evaluate_operation("", operation="commit", artifact_path="app.py")
    assert result["errors"] == ["workspace_not_authorized"]


def test_invalid_explicit_repo_cannot_fall_through_to_a_granted_session_root(
        tmp_repo, monkeypatch):
    _grant_file_reads(tmp_repo)
    (Path(tmp_repo) / "app.py").write_text("ready\n")
    monkeypatch.setattr(store, "resolve_repo_verbose", lambda _p: (tmp_repo, "session"))
    result = policy_api.evaluate_operation(
        "/not/a/repository", operation="commit", artifact_path="app.py")
    assert result["errors"] == ["workspace_not_authorized"]


def test_revoking_the_exact_root_applies_to_the_next_requested_evaluation(tmp_repo):
    _grant_file_reads(tmp_repo)
    (Path(tmp_repo) / "app.py").write_text("ready\n")
    assert policy_api.evaluate_operation(
        tmp_repo, operation="commit", artifact_path="app.py")["errors"] == []
    (store.store_dir() / "config.toml").write_text(
        "[policy]\nartifact_read_roots = []\n")
    assert policy_api.evaluate_operation(
        tmp_repo, operation="commit", artifact_path="app.py")["errors"] == [
            "workspace_not_authorized"]


@pytest.mark.parametrize("artifact_path", ["../outside.py", "/tmp/outside.py", ".git/config"])
def test_file_mode_rejects_escape_and_git_metadata(tmp_repo, artifact_path):
    _grant_file_reads(tmp_repo)
    result = policy_api.evaluate_operation(
        tmp_repo, operation="commit", artifact_path=artifact_path)
    assert result["errors"] == ["artifact_invalid_path"]


@pytest.mark.parametrize("artifact_path", [".GIT/config", ".Git/config", "src/.gIt/index"])
def test_file_mode_rejects_case_aliases_of_git_metadata(tmp_repo, artifact_path):
    assert policy_api.evaluate_operation(
        tmp_repo, operation="commit", artifact_path=artifact_path)["errors"] == [
            "artifact_invalid_path"]


def test_file_mode_rejects_a_symlink_leaf(tmp_repo):
    _grant_file_reads(tmp_repo)
    repo = Path(tmp_repo)
    outside = repo.parent / "outside.py"
    outside.write_text("TODO\n")
    (repo / "link.py").symlink_to(outside)
    result = policy_api.evaluate_operation(
        tmp_repo, operation="commit", artifact_path="link.py")
    assert result["errors"] == ["artifact_unreadable"]


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="FIFO unavailable on this platform")
def test_file_mode_rejects_a_fifo_without_blocking(tmp_repo):
    _grant_file_reads(tmp_repo)
    os.mkfifo(Path(tmp_repo) / "pipe")
    probe = (
        "from contexer import policy_api; "
        "\ntry: policy_api._read_confined_file(__import__('sys').argv[1], 'pipe')"
        "\nexcept ValueError as exc: print(exc)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe, tmp_repo], capture_output=True, text=True,
        timeout=2, check=False)
    assert completed.returncode == 0
    assert completed.stdout.strip() == "artifact_not_regular"


def test_file_mode_refuses_a_platform_without_confined_open_support(tmp_repo, monkeypatch):
    _grant_file_reads(tmp_repo)
    (Path(tmp_repo) / "app.py").write_text("ready\n")
    monkeypatch.setattr(policy_api.os, "O_NOFOLLOW", 0)
    result = policy_api.evaluate_operation(
        tmp_repo, operation="commit", artifact_path="app.py")
    assert result["errors"] == ["artifact_mode_unsupported"]


def test_file_mode_detects_an_in_read_file_mutation(tmp_repo, monkeypatch):
    _grant_file_reads(tmp_repo)
    target = Path(tmp_repo) / "app.py"
    target.write_text("ready\n")
    original = policy_api.os.read
    changed = False

    def mutate_after_read(fd, count):
        nonlocal changed
        chunk = original(fd, count)
        if chunk and not changed:
            changed = True
            target.write_text("changed while reading\n")
        return chunk

    monkeypatch.setattr(policy_api.os, "read", mutate_after_read)
    result = policy_api.evaluate_operation(
        tmp_repo, operation="commit", artifact_path="app.py")
    assert result["errors"] == ["artifact_unstable"]


def test_file_mode_detects_replacement_of_the_granted_root(tmp_repo, monkeypatch):
    _grant_file_reads(tmp_repo)
    target = Path(tmp_repo) / "app.py"
    target.write_text("ready\n")
    moved = Path(tmp_repo + "-moved")
    original = policy_api.os.read
    replaced = False

    def replace_after_read(fd, count):
        nonlocal replaced
        chunk = original(fd, count)
        if chunk and not replaced:
            replaced = True
            Path(tmp_repo).rename(moved)
            Path(tmp_repo).mkdir()
        return chunk

    monkeypatch.setattr(policy_api.os, "read", replace_after_read)
    result = policy_api.evaluate_operation(
        tmp_repo, operation="commit", artifact_path="app.py")
    assert result["errors"] == ["artifact_unstable"]


def test_file_mode_is_mutually_exclusive_with_caller_supplied_content(tmp_repo):
    result = policy_api.evaluate_operation(
        tmp_repo, operation="commit", artifact_path="app.py",
        artifact_kind="file_content", artifact="TODO")
    assert result["errors"] == ["artifact_path conflicts with files/artifact input"]


@pytest.mark.parametrize(("field", "value"), [("flags", []), ("paths", ["*.py"])])
def test_file_mode_reports_malformed_stored_rules_unchecked(tmp_repo, field, value):
    _grant_file_reads(tmp_repo, diagnostics=True)
    (Path(tmp_repo) / "app.py").write_text("ready\n")
    entry = _seed(tmp_repo, "Never leave TODO markers", source_files=["app.py"])
    _arm(tmp_repo, entry["id"], pattern="TODO", paths="app.py")
    data = store.load(tmp_repo)
    store.entry_by_id(data["entries"], entry["id"])["guard_check"][field] = value
    store.save(tmp_repo, data)

    result = policy_api.evaluate_operation(
        tmp_repo, operation="commit", artifact_path="app.py")

    assert result["verdict"] == "allow"
    assert result["evaluation_status"] == "partial"
    receipt = policy_api.decision_impact.report(
        tmp_repo, receipt_id=result["receipt_id"])["records"][0]
    assert receipt["conditions"][0]["result"] == "unchecked"
    assert receipt["conditions"][0]["gap"] == "unsupported-check"
    assert receipt["conditions"][0]["verified"] is False


def test_file_mode_rejects_binary_oversized_and_overlong_line_inputs(tmp_repo):
    _grant_file_reads(tmp_repo)
    cases = {
        "binary.py": b"a\x00b",
        "large.py": b"x" * (policy.MAX_BOUNDED_FILE_BYTES + 1),
        "line.py": b"x" * (policy.MAX_BOUNDED_FILE_LINE_CHARS + 1),
    }
    expected = {
        "binary.py": "artifact_binary",
        "large.py": "artifact_too_large",
        "line.py": "artifact_line_too_long",
    }
    for name, content in cases.items():
        (Path(tmp_repo) / name).write_bytes(content)
        result = policy_api.evaluate_operation(
            tmp_repo, operation="commit", artifact_path=name)
        assert result["errors"] == [expected[name]]


def test_file_mode_records_condition_receipt_only_when_diagnostics_are_enabled(tmp_repo):
    _grant_file_reads(tmp_repo, diagnostics=True)
    (Path(tmp_repo) / "app.py").write_text("ready\n")
    entry = _seed(tmp_repo, "Never leave TODO markers", source_files=["app.py"])
    _arm(tmp_repo, entry["id"], pattern="TODO", paths="app.py")

    result = policy_api.evaluate_operation(tmp_repo, operation="commit", artifact_path="app.py")
    assert result["receipt_id"]
    from contexer import decision_impact
    receipt = decision_impact.report(
        tmp_repo, receipt_id=result["receipt_id"])["records"][0]
    assert receipt["conditions"][0]["result"] == "satisfied"
    assert receipt["conditions"][0]["verified"] is True
    assert "TODO" not in decision_impact.path(tmp_repo).read_text()


def test_exact_guidance_reference_links_without_changing_the_check(tmp_repo):
    _grant_file_reads(tmp_repo, diagnostics=True)
    (Path(tmp_repo) / "app.py").write_text("ready\n")
    entry = _seed(tmp_repo, "Never leave TODO markers", source_files=["app.py"])
    _arm(tmp_repo, entry["id"], pattern="TODO", paths="app.py")
    persisted = store.entry_by_id(store.load(tmp_repo)["entries"], entry["id"])
    revision_id = policy.current_revision(persisted)["revision_id"]
    guidance_id = policy_api.decision_impact.append(
        tmp_repo, policy_api.decision_impact.guidance_envelope(
            tmp_repo, tmp_repo, route="explicit_lookup", rows=[{
                "scope": "personal", "id": entry["id"], "revision_id": revision_id,
                "fingerprint": "guidance-v1:test", "authority": "trusted_approved",
                "tier": "full", "reason": "query", "files": ["app.py"],
                "rule_digest": policy.rule_digest(persisted["guard_check"]),
            }]))

    result = policy_api.evaluate_operation(
        tmp_repo, operation="commit", artifact_path="app.py",
        guidance_refs=[guidance_id])

    assert result["verdict"] == "allow" and result["evaluation_status"] == "complete"
    receipt = policy_api.decision_impact.report(
        tmp_repo, receipt_id=result["receipt_id"])["records"][0]
    assert receipt["guidance_refs"] == [{
        "receipt_id": guidance_id, "attribution": "caller_linked"}]
    assert receipt["linkage_gaps"] == []


def test_wrong_guidance_reference_is_only_a_linkage_gap(tmp_repo):
    _grant_file_reads(tmp_repo, diagnostics=True)
    (Path(tmp_repo) / "app.py").write_text("TODO\n")
    entry = _seed(tmp_repo, "Never leave TODO markers", source_files=["app.py"])
    _arm(tmp_repo, entry["id"], pattern="TODO", paths="app.py")
    wrong = policy_api.decision_impact.append(
        tmp_repo, policy_api.decision_impact.guidance_envelope(
            tmp_repo, tmp_repo, route="explicit_lookup", rows=[{
                "scope": "personal", "id": "different", "revision_id": "different",
                "fingerprint": "guidance-v1:test", "authority": "trusted_approved",
                "tier": "full", "reason": "query", "files": ["app.py"],
                "rule_digest": "sha256:different",
            }]))

    result = policy_api.evaluate_operation(
        tmp_repo, operation="commit", artifact_path="app.py", guidance_refs=[wrong])

    # Attribution metadata can fail; it can never soften or strengthen the actual check.
    assert result["verdict"] == "block" and result["evaluation_status"] == "complete"
    receipt = policy_api.decision_impact.report(
        tmp_repo, receipt_id=result["receipt_id"])["records"][0]
    assert receipt["conditions"][0]["result"] == "violated"
    assert receipt["guidance_refs"] == []
    assert receipt["linkage_gaps"] == ["identity_mismatch"]
