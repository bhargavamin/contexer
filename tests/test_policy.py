"""Tests for contexer/policy.py - request validation, policy selection, and result shapes.

Two properties carry this module. Selection is a GATE: a decision that has not been approved,
or that no human ever vouched for, must not appear in the set at all - so every status and
every provenance gets its own test rather than one representative case. And
`policy_set_version` must name a set independently of how the caller happened to hold it,
because a verdict is only tied to its policies if the same set always hashes the same.

Every bound in the module has a test here saying what it buys; a constant nobody can explain
is a constant nobody can change.
"""
import ast
import itertools
import pathlib
import re

import pytest

from contexer import guard_engine, policy, redact


def _rev(rev_id="rev-1", source="human"):
    return {"revision_id": rev_id, "source": source}


def _entry(did="d1", *, status="approved", source="human", created_by="",
           approved_by=None, source_files=None, guard_check=None, revisions=None,
           title="Title", tombstoned=None):
    """One store decision entry, as `select_policies` receives it from a caller that loaded it.

    `revisions=None` builds a single current revision; pass a list to exercise the pointer.
    """
    entry = {"id": did, "status": status, "title": title,
             "current_revision_id": "rev-1",
             "revisions": revisions if revisions is not None else [_rev(source=source)]}
    if created_by:
        entry["created_by"] = created_by
    if approved_by is not None:
        entry["approved_by"] = approved_by
    if source_files is not None:
        entry["source_files"] = source_files
    if guard_check is not None:
        entry["guard_check"] = guard_check
    if tombstoned is not None:
        entry["tombstoned"] = tombstoned
    return entry


def _armed(paths="", pattern="TODO"):
    """The shape `guard_engine.arm_guard` writes onto an entry."""
    return {"type": "regex", "pattern": pattern, "flags": "", "paths": paths,
            "message": "", "armed_at": "2026-08-24T10:00:00+00:00"}


def _diff(content):
    """The artifact a commit-shaped request carries - the bytes the evaluator judges."""
    return {"kind": "diff", "content": content}


def _broken(did="d1"):
    """A selected policy whose `rule` is not a mapping at all - the shape a hand-corrupted
    store produces, and the one thing that makes the evaluator break mid-set."""
    return {"decision_id": did, "revision_id": "r1", "kind": "armed", "title": "",
            "rule": "not a mapping", "matched_files": []}


def _request(**over):
    base = {"operation": "commit", "files": [], "repo_key": "/repo"}
    base.update(over)
    return base


# ── vocabularies ─────────────────────────────────────────────────────────────────

class TestVocabularies:
    def test_the_five_vocabularies_are_exactly_the_plan_s(self):
        assert policy.VERDICTS == ("allow", "warn", "block")
        assert policy.EVALUATION_STATUSES == ("complete", "partial", "error")
        assert policy.BASES == ("deterministic", "semantic", "mixed")
        assert policy.OPERATIONS == ("read_files", "write_files", "shell", "commit",
                                     "merge", "deploy", "api_request")
        assert policy.ARTIFACT_KINDS == ("diff", "file_content", "command", "request",
                                         "deployment")

    def test_trusted_sources_mirror_the_guard_s_four(self):
        # Restated, not imported (policy.py is a leaf). `ai` and `memory` are absent by
        # design: neither has had a human in the loop.
        assert policy.TRUSTED_SOURCES == frozenset({"human", "scan", "bootstrap", "plan"})


# ── request validation ───────────────────────────────────────────────────────────

class TestValidateRequest:
    def test_a_minimal_request_normalizes_with_defaults(self):
        norm, errors = policy.validate_request({"operation": "commit", "repo_key": "/repo"})
        assert errors == []
        assert norm == {"intent": "", "operation": "commit", "files": [],
                        "artifact": None, "repo_key": "/repo"}

    def test_it_never_mutates_the_caller_s_request(self):
        request = {"operation": "commit", "repo_key": "/repo"}
        policy.validate_request(request)
        assert request == {"operation": "commit", "repo_key": "/repo"}

    def test_every_defect_is_collected_not_just_the_first(self):
        _, errors = policy.validate_request({"operation": "teleport", "repo_key": "",
                                             "files": ["/abs"], "surprise": 1})
        assert len(errors) == 4, errors

    def test_a_non_mapping_request_is_an_error_not_a_crash(self):
        norm, errors = policy.validate_request(["not", "a", "mapping"])
        assert norm is None and errors == ["request must be a mapping, got list"]

    def test_an_unknown_top_level_key_is_rejected_because_the_schema_is_frozen(self):
        # Preserving it would let a caller invent a field every evaluator silently ignores.
        _, errors = policy.validate_request(_request(priority="high"))
        assert errors == ["unknown top-level key: 'priority'"]

    def test_an_operation_outside_the_vocabulary_is_rejected(self):
        _, errors = policy.validate_request(_request(operation="rm_rf"))
        assert "operation must be one of" in errors[0]

    def test_an_unhashable_operation_returns_an_error_rather_than_raising(self):
        _, errors = policy.validate_request(_request(operation=["commit"]))
        assert len(errors) == 1 and "operation must be one of" in errors[0]

    def test_repo_key_is_required_and_must_be_non_empty(self):
        _, errors = policy.validate_request({"operation": "commit", "repo_key": "   "})
        assert errors == ["repo_key must be a non-empty string"]

    def test_repo_key_is_capped_at_300_chars(self):
        assert policy._MAX_REPO_KEY_CHARS == 300
        ok, errors = policy.validate_request(_request(repo_key="/" + "r" * 299))
        assert errors == [] and ok is not None
        _, errors = policy.validate_request(_request(repo_key="/" + "r" * 300))
        assert errors == ["repo_key exceeds 300 characters (301)"]

    def test_intent_is_optional_may_be_empty_and_is_capped_at_300_chars(self):
        assert policy._MAX_INTENT_CHARS == 300
        ok, errors = policy.validate_request(_request(intent=""))
        assert errors == [] and ok["intent"] == ""
        _, errors = policy.validate_request(_request(intent="i" * 301))
        assert errors == ["intent exceeds 300 characters (301)"]

    def test_at_most_100_files_because_a_partial_answer_is_not_an_answer(self):
        # Truncating would answer about 100 of the 101 files while claiming to answer the
        # request, so the bound is an error.
        assert policy._MAX_FILES == 100
        ok, errors = policy.validate_request(_request(files=[f"f{i}.py" for i in range(100)]))
        assert errors == [] and len(ok["files"]) == 100
        _, errors = policy.validate_request(_request(files=[f"f{i}.py" for i in range(101)]))
        assert errors == ["files has more than 100 entries (101)"]

    @pytest.mark.parametrize("path", ["/etc/passwd", "C:\\secrets.txt", "../outside.py",
                                      "src/../../outside.py"])
    def test_absolute_and_escaping_paths_are_rejected_not_rewritten(self, path):
        _, errors = policy.validate_request(_request(files=[path]))
        assert len(errors) == 1 and "files[0]" in errors[0]

    def test_a_path_is_capped_at_300_chars_the_same_as_an_evidence_event_s(self):
        assert policy._MAX_PATH_CHARS == 300
        _, errors = policy.validate_request(_request(files=["p" * 301]))
        assert errors == ["files[0] exceeds 300 characters (301)"]

    def test_files_must_be_a_list(self):
        _, errors = policy.validate_request(_request(files="src/app.py"))
        assert errors == ["files must be a list of strings"]

    def test_a_valid_artifact_survives_normalization(self):
        norm, errors = policy.validate_request(
            _request(artifact={"kind": "diff", "content": "@@ -1 +1 @@"}))
        assert errors == [] and norm["artifact"] == {"kind": "diff", "content": "@@ -1 +1 @@"}

    def test_an_artifact_kind_outside_the_vocabulary_is_rejected(self):
        _, errors = policy.validate_request(_request(artifact={"kind": "email", "content": ""}))
        assert "artifact.kind must be one of" in errors[0]

    def test_an_unknown_artifact_key_is_rejected_too(self):
        _, errors = policy.validate_request(
            _request(artifact={"kind": "diff", "content": "", "encoding": "utf-8"}))
        assert errors == ["unknown artifact key: 'encoding'"]

    def test_an_artifact_over_2_mib_is_an_error_because_half_a_diff_reads_as_clean(self):
        assert policy.MAX_ARTIFACT_BYTES == 2 * 1024 * 1024
        ok, errors = policy.validate_request(
            _request(artifact={"kind": "diff", "content": "x" * policy.MAX_ARTIFACT_BYTES}))
        assert errors == [] and ok is not None
        _, errors = policy.validate_request(
            _request(artifact={"kind": "diff", "content": "x" * (policy.MAX_ARTIFACT_BYTES + 1)}))
        assert errors == ["artifact.content exceeds 2097152 bytes (2097153)"]

    def test_the_artifact_cap_counts_utf8_bytes_not_characters(self):
        # One emoji is 4 bytes: a character cap would let a 4x-oversized artifact through.
        oversize = "🙂" * (policy.MAX_ARTIFACT_BYTES // 4 + 1)
        _, errors = policy.validate_request(_request(artifact={"kind": "diff",
                                                               "content": oversize}))
        assert errors and "exceeds 2097152 bytes" in errors[0]

    def test_a_null_artifact_is_valid_because_read_files_carries_no_payload(self):
        norm, errors = policy.validate_request(_request(operation="read_files", artifact=None))
        assert errors == [] and norm["artifact"] is None


# ── selection: status and trust gates ────────────────────────────────────────────

class TestSelectionStatusGate:
    @pytest.mark.parametrize("status", ["suggested", "pending_approval", "ignored"])
    def test_a_non_approved_decision_never_selects(self, status):
        entries = [_entry(status=status, guard_check=_armed())]
        assert policy.select_policies(entries, _request()) == []

    def test_a_missing_status_reads_as_approved_the_store_s_legacy_semantics(self):
        entry = _entry(guard_check=_armed())
        del entry["status"]
        assert len(policy.select_policies([entry], _request())) == 1

    def test_an_explicitly_empty_status_is_malformed_and_does_not_select(self):
        # `.get("status", "approved")`, not `.get("status") or "approved"` - the guard's own
        # spelling. An empty string is a broken write, not a legacy entry, and reading it as
        # approved would let one enforce policy.
        assert policy.select_policies([_entry(status="", guard_check=_armed())],
                                      _request()) == []

    def test_a_tombstoned_entry_is_defensively_deselected(self):
        entries = [_entry(guard_check=_armed(), tombstoned=True)]
        assert policy.select_policies(entries, _request()) == []


class TestSelectionTrustGate:
    @pytest.mark.parametrize("source", ["human", "scan", "bootstrap", "plan"])
    def test_each_trusted_provenance_selects(self, source):
        entries = [_entry(source=source, guard_check=_armed())]
        assert len(policy.select_policies(entries, _request())) == 1

    @pytest.mark.parametrize("source", ["ai", "memory"])
    def test_an_untrusted_provenance_is_excluded_entirely_not_downgraded(self, source):
        # Excluded from the SET: it must be unable to warn as well as to block.
        entries = [_entry(source=source, guard_check=_armed(),
                          source_files=["src/app.py"])]
        assert policy.select_policies(entries, _request(files=["src/app.py"])) == []

    def test_an_ai_decision_a_human_approved_by_hand_is_trusted(self):
        entries = [_entry(source="ai", approved_by="human", guard_check=_armed())]
        assert len(policy.select_policies(entries, _request())) == 1

    def test_an_auto_approved_entry_no_human_touched_stays_untrusted(self):
        # Born-approved with an `ai` source and no `approved_by`: status alone is not consent.
        entries = [_entry(source="ai", guard_check=_armed())]
        assert policy.select_policies(entries, _request()) == []

    def test_a_falsy_revision_source_falls_back_to_created_by(self):
        # Legacy entries predate provenance; the fallback is read-time only, never a rewrite.
        entries = [_entry(source=None, created_by="human", guard_check=_armed())]
        assert len(policy.select_policies(entries, _request())) == 1

    def test_a_falsy_created_by_too_still_resolves_to_untrusted(self):
        entries = [_entry(source=None, created_by="", guard_check=_armed())]
        assert policy.select_policies(entries, _request()) == []

    def test_an_entry_with_no_revisions_at_all_is_untrusted(self):
        entries = [_entry(revisions=[], guard_check=_armed())]
        assert policy.select_policies(entries, _request()) == []

    def test_is_trusted_reads_the_pointer_not_the_last_revision(self):
        entry = _entry(revisions=[_rev("rev-1", "human"), _rev("rev-2", "ai")])
        assert policy.is_trusted(entry) is True
        entry["current_revision_id"] = "rev-2"
        assert policy.is_trusted(entry) is False

    def test_a_dangling_pointer_falls_back_to_the_last_revision(self):
        entry = _entry(revisions=[_rev("rev-9", "human")])
        assert policy.current_revision(entry)["revision_id"] == "rev-9"


class TestTrustRuleParityWithTheGuard:
    """`policy.is_trusted` RESTATES `guard_engine._guard_trusted` - policy.py is a leaf and
    cannot import the guard, so the rule exists in two files and nothing but this test stops
    them drifting apart. A drift here is not cosmetic: the two would disagree about which
    decisions may enforce, so the same entry could block a commit and be invisible to the
    policy plane, or the reverse.

    Exhaustive rather than representative, because a mirror fails at the CORNER - the falsy
    `source` fallback, the `approved_by` override, the missing-status legacy default - and a
    handful of hand-picked rows is exactly what would still pass while a corner rotted.
    """

    SOURCES = ("human", "scan", "bootstrap", "plan", "ai", "memory", None, "")
    STATUSES = ("approved", "suggested", "pending_approval", "ignored", "__missing__")
    APPROVED_BY = ("human", None)
    CREATED_BY = ("human", "ai", "")

    def _grid(self):
        for source, status, approved_by, created_by in itertools.product(
                self.SOURCES, self.STATUSES, self.APPROVED_BY, self.CREATED_BY):
            entry = _entry(source=source, created_by=created_by, approved_by=approved_by,
                           guard_check=_armed())
            if status == "__missing__":
                del entry["status"]
            else:
                entry["status"] = status
            yield entry, (source, status, approved_by, created_by)

    def test_the_grid_covers_every_combination(self):
        assert len(list(self._grid())) == 8 * 5 * 2 * 3

    def test_is_trusted_agrees_with_the_real_guard_on_every_combination(self):
        mismatches = [row for entry, row in self._grid()
                      if policy.is_trusted(entry) != guard_engine._guard_trusted(entry)]
        assert mismatches == []

    def test_selection_admits_exactly_the_entries_the_guard_trusts(self):
        # The gate, not just the predicate: an entry the guard trusts must reach the policy
        # set, and one it does not must be absent from it entirely rather than downgraded.
        mismatches = [row for entry, row in self._grid()
                      if bool(policy.select_policies([entry], _request()))
                      != guard_engine._guard_trusted(entry)]
        assert mismatches == []

    def test_no_unapproved_row_ever_selects(self):
        selected = [row for entry, row in self._grid()
                    if row[1] not in ("approved", "__missing__")
                    and policy.select_policies([entry], _request())]
        assert selected == []


# ── selection: applicability ─────────────────────────────────────────────────────

class TestArmedApplicability:
    def test_an_armed_rule_applies_to_a_repo_wide_operation_naming_no_files(self):
        entries = [_entry(guard_check=_armed(paths="src/*.py"))]
        [hit] = policy.select_policies(entries, _request(operation="commit", files=[]))
        # A glob-scoped rule still governs a commit: the request names nothing to filter by.
        assert hit["kind"] == "armed" and hit["matched_files"] == []

    def test_an_armed_rule_with_no_glob_selects_every_named_file(self):
        entries = [_entry(guard_check=_armed(paths=""))]
        [hit] = policy.select_policies(entries, _request(files=["a.py", "docs/b.md"]))
        assert hit["matched_files"] == ["a.py", "docs/b.md"]

    def test_a_glob_scoped_rule_matches_only_the_files_it_selects(self):
        entries = [_entry(guard_check=_armed(paths="src/*.py"))]
        [hit] = policy.select_policies(entries, _request(files=["src/a.py", "docs/b.md"]))
        assert hit["matched_files"] == ["src/a.py"]

    def test_a_glob_matching_none_of_the_named_files_does_not_select_as_armed(self):
        entries = [_entry(guard_check=_armed(paths="src/*.py"))]
        assert policy.select_policies(entries, _request(files=["docs/b.md"])) == []

    def test_an_armed_rule_whose_glob_misses_still_selects_as_advisory_via_its_anchors(self):
        # The rule cannot judge these files, but the decision's prose may still be relevant;
        # dropping it entirely would be a silent gap rather than a conservative choice.
        entries = [_entry(guard_check=_armed(paths="src/*.py"), source_files=["docs/b.md"])]
        [hit] = policy.select_policies(entries, _request(files=["docs/b.md"]))
        assert hit["kind"] == "advisory" and hit["rule"] is None

    def test_an_empty_guard_check_is_not_armed(self):
        entries = [_entry(guard_check={}, source_files=["a.py"])]
        [hit] = policy.select_policies(entries, _request(files=["a.py"]))
        assert hit["kind"] == "advisory"

    def test_the_armed_rule_travels_on_the_result_as_a_copy(self):
        rule = _armed(paths="src/*.py")
        [hit] = policy.select_policies([_entry(guard_check=rule)], _request())
        assert hit["rule"] == rule and hit["rule"] is not rule

    def test_rule_selects_takes_the_config_and_uses_fnmatch(self):
        assert policy.rule_selects({"paths": ""}, "anything.py") is True
        assert policy.rule_selects({"paths": "src/*.py"}, "src/a.py") is True
        assert policy.rule_selects({"paths": "src/*.py"}, "docs/a.md") is False


class TestAdvisoryApplicability:
    def test_an_anchor_intersection_selects_as_advisory(self):
        entries = [_entry(source_files=["src/app.py", "src/other.py"])]
        [hit] = policy.select_policies(entries, _request(files=["src/app.py", "z.py"]))
        assert hit["kind"] == "advisory"
        assert hit["rule"] is None and hit["matched_files"] == ["src/app.py"]

    def test_no_intersection_selects_nothing(self):
        entries = [_entry(source_files=["src/app.py"])]
        assert policy.select_policies(entries, _request(files=["z.py"])) == []

    def test_an_unanchored_prose_decision_never_selects_on_a_file_request(self):
        assert policy.select_policies([_entry()], _request(files=["z.py"])) == []

    def test_an_advisory_does_not_select_on_a_repo_wide_request(self):
        # Nothing to intersect: only armed rules speak for a file-less operation.
        entries = [_entry(source_files=["src/app.py"])]
        assert policy.select_policies(entries, _request(files=[])) == []


class TestSelectionShape:
    def test_output_is_ordered_by_decision_id_whatever_the_input_order(self):
        entries = [_entry("d3", guard_check=_armed()), _entry("d1", guard_check=_armed()),
                   _entry("d2", guard_check=_armed())]
        ids = [p["decision_id"] for p in policy.select_policies(entries, _request())]
        assert ids == ["d1", "d2", "d3"]
        assert [p["decision_id"] for p in policy.select_policies(entries[::-1], _request())] == ids

    def test_each_policy_carries_the_documented_keys(self):
        entries = [_entry(guard_check=_armed(), title="Never commit secrets")]
        [hit] = policy.select_policies(entries, _request())
        assert set(hit) == {"decision_id", "revision_id", "kind", "title", "rule",
                            "matched_files"}
        assert hit["revision_id"] == "rev-1" and hit["title"] == "Never commit secrets"

    def test_junk_in_the_decisions_list_is_skipped_not_raised_on(self):
        entries = ["nonsense", None, _entry(guard_check=_armed())]
        assert len(policy.select_policies(entries, _request())) == 1

    def test_no_decisions_selects_nothing(self):
        assert policy.select_policies([], _request()) == []


# ── policy_set_version ───────────────────────────────────────────────────────────

class TestPolicySetVersion:
    def test_it_is_a_sha256_prefixed_hex_digest(self):
        version = policy.policy_set_version([])
        assert version.startswith("sha256:") and len(version) == 71

    def test_the_same_set_hashes_the_same_in_any_order(self):
        entries = [_entry("d1", guard_check=_armed()), _entry("d2", guard_check=_armed("a/*"))]
        forward = policy.select_policies(entries, _request())
        assert policy.policy_set_version(forward) == policy.policy_set_version(forward[::-1])

    def test_it_changes_when_a_decision_s_current_revision_advances(self):
        before = policy.select_policies([_entry(guard_check=_armed())], _request())
        entry = _entry(guard_check=_armed(), revisions=[_rev("rev-2", "human")])
        entry["current_revision_id"] = "rev-2"
        after = policy.select_policies([entry], _request())
        assert policy.policy_set_version(before) != policy.policy_set_version(after)

    def test_it_changes_when_an_armed_rule_changes(self):
        before = policy.select_policies([_entry(guard_check=_armed(pattern="TODO"))], _request())
        after = policy.select_policies([_entry(guard_check=_armed(pattern="FIXME"))], _request())
        assert policy.policy_set_version(before) != policy.policy_set_version(after)

    def test_it_changes_when_a_policy_joins_the_set(self):
        one = policy.select_policies([_entry("d1", guard_check=_armed())], _request())
        two = policy.select_policies([_entry("d1", guard_check=_armed()),
                                      _entry("d2", guard_check=_armed())], _request())
        assert policy.policy_set_version(one) != policy.policy_set_version(two)

    def test_a_rule_bearing_and_a_prose_policy_hash_without_ordering_errors(self):
        # A dict and a None sit in the same column; comparing rows as tuples would raise.
        mixed = policy.select_policies(
            [_entry("d1", guard_check=_armed()), _entry("d2", source_files=["a.py"])],
            _request(files=["a.py"]))
        assert len(mixed) == 2 and policy.policy_set_version(mixed).startswith("sha256:")

    def test_matched_files_do_not_change_the_version(self):
        # The version names the POLICY SET, not the request it was selected for.
        entry = _entry(guard_check=_armed())
        first = policy.select_policies([entry], _request(files=["a.py"]))
        second = policy.select_policies([entry], _request(files=["b.py"]))
        assert policy.policy_set_version(first) == policy.policy_set_version(second)


# ── judging ──────────────────────────────────────────────────────────────────────

class TestValidateCheck:
    """Arm time. Every refusal raises the SAME message, so these tests pin WHAT is refused
    rather than how it is phrased - the phrasing is deliberately uninformative."""

    def test_the_only_two_check_types_are_regex_and_secret(self):
        assert policy.CHECK_TYPES == frozenset({"regex", "secret"})

    def test_a_valid_regex_rule_is_accepted(self):
        assert policy.validate_check("regex", "TODO", "") is None

    def test_a_secret_rule_takes_no_pattern(self):
        assert policy.validate_check("secret", "", "") is None
        # A pattern beside `secret` is nonsensical, not merely redundant: `secret` already
        # means "the high-confidence patterns", so honouring one would be a silent lie.
        with pytest.raises(ValueError):
            policy.validate_check("secret", "AKIA", "")

    @pytest.mark.parametrize("args", [
        ("semantic", "looks risky", ""),   # not machine-checkable at all
        ("regex", "", ""),                 # a regex rule with nothing to match
        ("regex", "TODO", "im"),           # only `i` is accepted
        ("regex", "TODO", "x"),
        ("regex", "([unclosed", ""),       # must actually compile
    ])
    def test_an_unarmable_request_raises(self, args):
        with pytest.raises(ValueError, match="machine-checkable"):
            policy.validate_check(*args)

    def test_the_i_flag_alone_is_accepted(self):
        assert policy.validate_check("regex", "todo", "i") is None


class TestRuleMatches:
    """Run time. `(lines, reason)` - a reason means the rule could NOT be evaluated, which is
    the distinction the guard learned the hard way: "not checked" must never read as "clean"."""

    def test_a_regex_reports_the_exact_line_of_each_hit(self):
        lines, reason = policy.rule_matches({"type": "regex", "pattern": "TODO"},
                                            "one\nTODO here\nthree\nTODO again\n")
        assert (lines, reason) == ([2, 4], None)

    def test_no_match_is_an_empty_list_with_no_reason(self):
        assert policy.rule_matches({"type": "regex", "pattern": "TODO"}, "clean\n") == ([], None)

    def test_the_i_flag_is_the_only_one_honoured_and_it_works(self):
        rule = {"type": "regex", "pattern": "todo", "flags": "i"}
        assert policy.rule_matches(rule, "# TODO fix\n")[0] == [1]
        assert policy.rule_matches({**rule, "flags": ""}, "# TODO fix\n")[0] == []

    @pytest.mark.parametrize("pattern", ["([oops", None, 7])
    def test_an_unparseable_pattern_is_unchecked_not_clean(self, pattern):
        # Defensive - `validate_check` refuses one at arm time - but a store is a JSON file a
        # human can edit, and a rule that silently never fires is the worst of both worlds. A
        # JSON `null` is the non-string case: letting its TypeError escape would take down the
        # caller's whole run over one corrupt rule.
        rule = {"type": "regex", "pattern": pattern}
        assert policy.rule_matches(rule, "x") == ([], "bad-pattern")

    def test_an_absent_pattern_keeps_the_guard_s_long_standing_match_everything(self):
        # Not a design choice being made here, just one being preserved: `validate_check` is
        # what stops an empty rule being armed, and this reads a rule that got past it.
        assert policy.rule_matches({"type": "regex"}, "a\nb\n") == ([1, 2], None)

    def test_an_unknown_check_type_is_unchecked_not_clean(self):
        assert policy.rule_matches({"type": "vibes"}, "x") == ([], "unsupported-check")
        assert policy.rule_matches({}, "x") == ([], "unsupported-check")

    @pytest.mark.parametrize("sample", [
        "key = 'AKIAIOSFODNN7EXAMPLE'",
        "tok = ghp_" + "a" * 36,
        "tok = xoxb-1234567890-abcdefghij",
        "tok = sk_live_" + "0" * 20,
        "tok = AIza" + "b" * 35,
        "jwt = eyJhbGciOiJI.eyJzdWIiOiIx.SflKxwRJSMeKK",
        "url = postgres://user:hunter2@db.example.com/app",
    ])
    def test_the_secret_check_catches_each_provider_shape(self, sample):
        lines, reason = policy.rule_matches({"type": "secret"}, "before\n" + sample + "\nafter\n")
        assert reason is None and lines == [2]

    def test_the_secret_check_spans_lines_because_a_pem_block_does(self):
        pem = ("before\n-----BEGIN RSA PRIVATE KEY-----\n"
               "MIIEpAIBAAKCAQEA1234567890abcdefG\n-----END RSA PRIVATE KEY-----\nafter\n")
        # Matching per line would silently defeat the multi-line pattern entirely.
        assert policy.rule_matches({"type": "secret"}, pem) == ([2], None)

    def test_prose_that_merely_mentions_a_password_is_not_a_secret(self):
        # High-confidence only: a blocking check has zero tolerance for a false positive.
        assert policy.rule_matches({"type": "secret"}, 'password = "hunter2-wordy"\n') == ([], None)

    def test_the_secret_check_reads_redact_s_list_rather_than_a_copy_of_it(self, monkeypatch):
        # The one property that matters: there is no second list here to drift from redact's.
        monkeypatch.setattr(redact, "HIGH_CONFIDENCE_PATTERNS", [re.compile(r"CANARY")])
        assert policy.rule_matches({"type": "secret"}, "a\nCANARY\n")[0] == [2]


class TestMatchCeilings:
    def test_the_ceilings_are_armed_blocks_advisory_warns(self):
        assert policy.MAX_VERDICT == {"armed": "block", "advisory": "warn"}

    def test_an_advisory_may_not_block_and_the_code_says_so_not_just_the_docs(self):
        with pytest.raises(ValueError, match="may not block"):
            policy._match({"kind": "advisory", "decision_id": "d1"}, "block", None)

    def test_an_unrecognised_kind_is_capped_at_warn_not_trusted_to_block(self):
        with pytest.raises(ValueError, match="may not block"):
            policy._match({"kind": "whatever", "decision_id": "d1"}, "block", None)

    def test_an_armed_policy_may_block(self):
        assert policy._match({"kind": "armed"}, "block", 3)["verdict"] == "block"


class TestEvaluatePolicies:
    def test_no_policies_allows_and_is_complete(self):
        result = policy.evaluate_policies([], _request())
        assert result["verdict"] == "allow" and result["evaluation_status"] == "complete"
        assert result["matches"] == [] and result["unchecked"] == []
        assert result["basis"] == "deterministic"

    def test_an_armed_rule_that_matches_blocks(self):
        selected = policy.select_policies([_entry(guard_check=_armed(pattern="TODO"))],
                                          _request())
        result = policy.evaluate_policies(selected, _request(artifact=_diff("a\n# TODO\n")))
        assert result["verdict"] == "block" and result["evaluation_status"] == "complete"
        assert [m["line"] for m in result["matches"]] == [2]

    def test_an_armed_rule_that_does_not_match_allows(self):
        selected = policy.select_policies([_entry(guard_check=_armed(pattern="TODO"))],
                                          _request())
        result = policy.evaluate_policies(selected, _request(artifact=_diff("clean\n")))
        assert result["verdict"] == "allow" and result["matches"] == []

    def test_an_advisory_warns_on_applicability_alone(self):
        selected = policy.select_policies([_entry(source_files=["a.py"])],
                                          _request(files=["a.py"]))
        result = policy.evaluate_policies(selected, _request(files=["a.py"],
                                                             artifact=_diff("anything\n")))
        assert result["verdict"] == "warn"
        assert [m["kind"] for m in result["matches"]] == ["advisory"]
        assert result["matches"][0]["line"] is None

    def test_an_advisory_alongside_a_blocking_rule_yields_the_worst_verdict(self):
        entries = [_entry("d1", guard_check=_armed(pattern="TODO")),
                   _entry("d2", source_files=["a.py"])]
        selected = policy.select_policies(entries, _request(files=["a.py"]))
        result = policy.evaluate_policies(selected, _request(files=["a.py"],
                                                             artifact=_diff("# TODO\n")))
        assert result["verdict"] == "block" and len(result["matches"]) == 2

    def test_every_match_names_both_the_decision_and_the_revision(self):
        # A decision whose text has moved on has not made the same objection, so a match that
        # cannot say which revision spoke cannot be audited.
        selected = policy.select_policies([_entry(guard_check=_armed(pattern="TODO"))],
                                          _request())
        result = policy.evaluate_policies(selected, _request(artifact=_diff("# TODO\n")))
        [match] = result["matches"]
        assert match["decision_id"] == "d1" and match["revision_id"] == "rev-1"
        assert set(match) == {"decision_id", "revision_id", "kind", "title", "message",
                              "verdict", "line"}

    def test_the_rule_s_operator_message_travels_on_the_match(self):
        rule = {**_armed(pattern="TODO"), "message": "no TODOs on main"}
        selected = policy.select_policies([_entry(guard_check=rule)], _request())
        result = policy.evaluate_policies(selected, _request(artifact=_diff("# TODO\n")))
        assert result["matches"][0]["message"] == "no TODOs on main"

    def test_the_result_names_the_policy_set_that_answered(self):
        selected = policy.select_policies([_entry(guard_check=_armed())], _request())
        result = policy.evaluate_policies(selected, _request(artifact=_diff("x")))
        assert result["policy_set_version"] == policy.policy_set_version(selected)

    def test_an_armed_rule_with_no_artifact_is_unchecked_never_clean(self):
        selected = policy.select_policies([_entry(guard_check=_armed(pattern="TODO"))],
                                          _request())
        result = policy.evaluate_policies(selected, _request(artifact=None))
        assert result["evaluation_status"] == "partial"
        assert result["unchecked"] == [{"reason": "omitted", "decision_id": "d1"}]
        assert result["verdict"] == "allow"  # allow + partial, never allow + complete

    def test_an_empty_artifact_is_judged_rather_than_reported_as_omitted(self):
        # An empty file WAS scanned and found nothing; a missing one was never scanned.
        selected = policy.select_policies([_entry(guard_check=_armed(pattern="TODO"))],
                                          _request())
        result = policy.evaluate_policies(selected, _request(artifact=_diff("")))
        assert result["evaluation_status"] == "complete" and result["unchecked"] == []

    def test_an_unrunnable_rule_is_reported_as_unchecked(self):
        selected = policy.select_policies([_entry(guard_check={**_armed(), "pattern": "([bad"})],
                                          _request())
        result = policy.evaluate_policies(selected, _request(artifact=_diff("x")))
        assert result["unchecked"] == [{"reason": "bad-pattern", "decision_id": "d1"}]
        assert result["evaluation_status"] == "partial"

    @pytest.mark.parametrize("reason", ["truncated", "unreadable", "binary", "too-large",
                                        "budget"])
    def test_the_caller_s_own_gaps_are_carried_through_and_make_it_partial(self, reason):
        # The caller owns the bytes, so it owns why they are missing; the evaluator's job is
        # to refuse to call the result complete.
        result = policy.evaluate_policies([], _request(),
                                          unchecked=[{"file": "big.bin", "reason": reason}])
        assert result["unchecked"] == [{"reason": reason, "file": "big.bin"}]
        assert result["evaluation_status"] == "partial"

    def test_a_typo_d_reason_raises_rather_than_becoming_an_unactionable_gap(self):
        with pytest.raises(ValueError, match="reason must be one of"):
            policy.evaluate_policies([], _request(), unchecked=[{"reason": "too_large"}])

    @pytest.mark.parametrize("row", ["big.bin", None, ["big.bin", "unreadable"]])
    def test_a_malformed_gap_row_raises_rather_than_vanishing(self, row):
        # Dropping it would silently turn the caller's bug into a clean verdict: they reported
        # a gap and the result would say `complete`. Same answer as a typo'd reason gets.
        with pytest.raises(ValueError, match="must be a mapping"):
            policy.evaluate_policies([], _request(), unchecked=[row])

    def test_one_malformed_row_raises_even_beside_well_formed_ones(self):
        with pytest.raises(ValueError, match="must be a mapping"):
            policy.evaluate_policies([], _request(),
                                     unchecked=[{"reason": "binary", "file": "a.png"}, "oops"])

    def test_an_internal_failure_is_an_error_and_is_never_laundered_into_complete(self):
        # A rule that is not a mapping at all: the evaluator breaks rather than guessing.
        result = policy.evaluate_policies([_broken("d1")], _request(artifact=_diff("x")))
        assert result["evaluation_status"] == "error"

    def test_an_error_keeps_the_matches_already_found_rather_than_allowing(self):
        good = policy.select_policies([_entry("d1", guard_check=_armed(pattern="TODO"))],
                                      _request())
        broken = [_broken("d2")]
        result = policy.evaluate_policies(good + broken, _request(artifact=_diff("# TODO\n")))
        assert result["evaluation_status"] == "error" and result["verdict"] == "block"

    def test_an_error_lists_the_policy_it_broke_on_and_every_one_after_it(self):
        # `error` says only THAT the run broke. Without these rows a caller reading `unchecked`
        # to learn what it got no answer about sees an empty list - a gap reading as clean.
        after = policy.select_policies([_entry("d3", guard_check=_armed(pattern="TODO"))],
                                       _request())
        result = policy.evaluate_policies([_broken("d2")] + after,
                                          _request(artifact=_diff("# TODO\n")))
        assert result["evaluation_status"] == "error"
        assert result["unchecked"] == [{"reason": "evaluator-error", "decision_id": "d2"},
                                       {"reason": "evaluator-error", "decision_id": "d3"}]
        # d3 never ran, so its rule never matched: it is listed as unjudged, not as clean.
        assert result["matches"] == []

    def test_the_policies_judged_before_the_break_are_not_listed_as_unreached(self):
        good = policy.select_policies([_entry("d1", guard_check=_armed(pattern="nope"))],
                                      _request())
        result = policy.evaluate_policies(good + [_broken("d2")], _request(artifact=_diff("x")))
        assert [r["decision_id"] for r in result["unchecked"]] == ["d2"]

    def test_a_caller_s_own_gaps_survive_alongside_the_unreached_ones(self):
        result = policy.evaluate_policies([_broken("d2")], _request(artifact=_diff("x")),
                                          unchecked=[{"reason": "binary", "file": "a.png"}])
        assert result["unchecked"] == [{"reason": "binary", "file": "a.png"},
                                       {"reason": "evaluator-error", "decision_id": "d2"}]

    def test_evaluator_error_is_in_the_vocabulary_rather_than_overloading_another_reason(self):
        assert "evaluator-error" in policy.UNCHECKED_REASONS

    def test_junk_in_the_policy_list_is_skipped_not_raised_on(self):
        assert policy.evaluate_policies(["nonsense", None], _request())["verdict"] == "allow"


class TestLeafPurity:
    """policy.py is where the ONE deterministic check lives, which only works while every
    caller can reach it. A dependency on the store or the guard would make it unreachable
    from below and push someone into keeping a second copy."""

    def _imports(self):
        tree = ast.parse(pathlib.Path(policy.__file__).read_text(encoding="utf-8"))
        plain, internal = set(), set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                plain |= {a.name.split(".")[0] for a in node.names}
            if isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                if root == "contexer":
                    internal |= {f"{node.module}.{a.name}" for a in node.names}
                else:
                    plain.add(root)
        return plain, internal

    def test_the_only_contexer_dependency_is_redact(self):
        _plain, internal = self._imports()
        assert internal == {"contexer.redact"}

    def test_it_owns_no_clock(self):
        # The caller owns the deadline, the reserve and the exit behaviour; a budget growing
        # back in here is exactly how one implementation becomes two.
        plain, _internal = self._imports()
        assert "time" not in plain and "datetime" not in plain


# ── results ──────────────────────────────────────────────────────────────────────

class TestBuildResult:
    def test_it_returns_the_documented_shape(self):
        result = policy.build_result("warn", "partial", "deterministic",
                                     [{"decision_id": "d1"}], ["big.diff"], "sha256:abc")
        assert result == {"verdict": "warn", "evaluation_status": "partial",
                          "basis": "deterministic", "matches": [{"decision_id": "d1"}],
                          "unchecked": ["big.diff"], "policy_set_version": "sha256:abc"}

    def test_none_lists_normalize_to_empty_lists(self):
        result = policy.build_result("allow", "complete", "mixed", None, None, "sha256:abc")
        assert result["matches"] == [] and result["unchecked"] == []

    @pytest.mark.parametrize("bad", [("nope", "complete", "mixed"),
                                     ("allow", "done", "mixed"),
                                     ("allow", "complete", "vibes")])
    def test_a_value_outside_its_vocabulary_raises_because_a_typo_is_a_wrong_verdict(self, bad):
        with pytest.raises(ValueError):
            policy.build_result(*bad, [], [], "sha256:abc")


class TestWorstVerdict:
    @pytest.mark.parametrize("verdicts,expected", [
        ([], "allow"),
        (["allow"], "allow"),
        (["allow", "warn"], "warn"),
        (["warn", "block", "allow"], "block"),
        (["block", "block"], "block"),
    ])
    def test_block_beats_warn_beats_allow(self, verdicts, expected):
        assert policy.worst_verdict(verdicts) == expected

    def test_an_unknown_verdict_raises_rather_than_being_folded_away(self):
        with pytest.raises(ValueError):
            policy.worst_verdict(["allow", "bl0ck"])
