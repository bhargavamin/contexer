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
