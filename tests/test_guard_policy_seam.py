"""The seam between the commit-time guard and the pure policy evaluator.

`tests/test_guard_engine.py` is the guard's SPECIFICATION and is deliberately left
byte-for-byte untouched by the refactor that made `guard_staged` a git adapter over
`policy.evaluate_policies` — if a row of it had to change, the refactor changed behaviour.
This file pins what that suite cannot: that the routing actually happens (so a future edit
cannot quietly re-inline the judging), the one piece of NEW behaviour the routing bought
(a dead armed rule is surfaced instead of passing as clean), and the two parity rows the
existing suite covers only below `guard_staged`.

Parity matrix — where each scenario is pinned. Rows already covered are NOT duplicated:

    clean run ................. test_guard_engine TestUncheckedIsReported::
                               test_clean_run_omits_the_key_entirely
    Tier-1 advisory pairing ... test_guard_engine TestGuardStaged::
                               test_source_files_pair_surfaces_advisory
    armed regex blocks ........ test_guard_engine TestGuardStagedViolations::
                               test_regex_violation_surfaces
    armed secret blocks ....... HERE (the existing suite pinned the secret check only
                               below guard_staged, never end to end through it)
    over-cap file unchecked ... test_guard_engine TestUncheckedIsReported::
                               test_over_cap_file_is_reported_not_silently_passed
    budget overrun reported,
      not blocking ............ test_guard_engine TestUncheckedIsReported::
                               test_running_out_of_time_does_not_block_the_commit
    CONTEXER_GUARD=0 .......... test_guard_engine TestGuardStaged::
                               test_env_var_zero_skips_before_any_work
    dead armed rule ........... HERE (new behaviour)
"""
from contexer import cli, guard_engine, policy, store
from tests.conftest import _git, _seed_entry, _write

# `repo` and `git_repo` are conftest fixtures, so pytest resolves them by name with no
# import here at all — which is what retired the re-binding this file used to carry.


def _arm_raw(repo_dir, entry, check):
    """Write a guard_check straight onto a stored entry, bypassing `arm_guard`'s
    validation — which is the only way a rule that cannot run exists at all. A store is a
    JSON file a human (or a botched migration) can edit."""
    data = store.load(str(repo_dir))
    store.entry_by_id(data["entries"], entry["id"])["guard_check"] = check
    store.save(str(repo_dir), data)


# ── the routing itself ────────────────────────────────────────────────────────

class TestGuardStagedRoutesThroughTheEvaluator:
    """The guard must not keep a second copy of "what counts as a violation". A future
    refactor that re-inlines the judging would still pass every behavioural test in the
    guard suite, so the call itself is pinned."""

    def test_evaluate_policies_is_called_with_the_policies_and_the_staged_bytes(
            self, repo, monkeypatch):
        entry = _seed_entry(repo, "Never commit TODO markers", title="No TODOs")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO",
                                message="no TODOs")
        _write(repo, "a.py", "# TODO fix this\n")
        _git(repo, "add", "a.py")

        seen = []
        real = policy.evaluate_policies

        def spy(policies, request, unchecked=None):
            seen.append((policies, request))
            return real(policies, request, unchecked)
        monkeypatch.setattr(policy, "evaluate_policies", spy)

        result = guard_engine.guard_staged(str(repo))
        assert len(result["violations"]) == 1

        assert len(seen) == 1, "one armed rule against one staged file is one evaluation"
        policies, request = seen[0]
        assert [p["decision_id"] for p in policies] == [entry["id"]]
        assert policies[0]["kind"] == "armed"
        assert policies[0]["title"] == "No TODOs"
        assert policies[0]["rule"]["pattern"] == "TODO"
        assert policies[0]["matched_files"] == ["a.py"]
        # The staged bytes, as an artifact — the adapter reads git, the evaluator judges
        # content and never learns what a repository is.
        assert request["artifact"] == {"kind": "file_content",
                                       "content": "# TODO fix this\n"}

    def test_a_file_no_rule_selects_is_never_offered_to_the_evaluator(
            self, repo, monkeypatch):
        entry = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), entry["id"], "regex", pattern="TODO",
                                paths="src/*.py")
        _write(repo, "data.json", "# TODO fix this\n")
        _git(repo, "add", "data.json")

        seen = []
        monkeypatch.setattr(policy, "evaluate_policies",
                            lambda p, r, u=None: seen.append(p) or
                            {"matches": [], "unchecked": []})
        assert guard_engine.guard_staged(str(repo))["violations"] == []
        assert seen == []


# ── parity row the existing suite covers only below guard_staged ──────────────

class TestSecretCheckBlocksEndToEnd:
    def test_staged_aws_key_surfaces_a_violation(self, repo):
        entry = _seed_entry(repo, "Never commit secrets", title="No secrets")
        guard_engine.arm_guard(str(repo), entry["id"], "secret")
        _write(repo, "conf.py", "line one\nkey = 'AKIAIOSFODNN7EXAMPLE'\n")
        _git(repo, "add", "conf.py")

        result = guard_engine.guard_staged(str(repo))
        assert [(v["path"], v["line"], v["decision_id"])
                for v in result["violations"]] == [("conf.py", 2, entry["id"])]
        assert "unchecked" not in result


# ── new behaviour: a dead armed rule is surfaced, never silently dead ─────────

class TestDeadArmedRuleIsSurfaced:
    """A rule the evaluator cannot run at all used to be indistinguishable from a rule that
    ran and found nothing: the pre-evaluator judging collapsed both into one `continue`. That
    is the failure a developer is least able to notice — the rule is armed, the commit passes,
    and nothing ever says the check did not happen."""

    def test_unparseable_pattern_is_reported_with_its_decision_id(self, repo):
        entry = _seed_entry(repo, "Weird rule")
        _arm_raw(repo, entry, {"type": "regex", "pattern": "(unclosed", "flags": "",
                               "paths": "", "message": "", "armed_at": "t"})
        _write(repo, "a.py", "# TODO fix this\n")
        _git(repo, "add", "a.py")

        result = guard_engine.guard_staged(str(repo))
        assert result["unchecked"] == [{"decision_id": entry["id"],
                                        "reason": "bad-pattern"}]

    def test_unknown_check_type_is_reported(self, repo):
        entry = _seed_entry(repo, "Rule from a future version")
        _arm_raw(repo, entry, {"type": "semantic", "pattern": "", "flags": "",
                               "paths": "", "message": "", "armed_at": "t"})
        _write(repo, "a.py", "anything\n")
        _git(repo, "add", "a.py")

        result = guard_engine.guard_staged(str(repo))
        assert result["unchecked"] == [{"decision_id": entry["id"],
                                        "reason": "unsupported-check"}]

    def test_a_dead_rule_never_blocks_and_is_not_an_error(self, repo):
        """Ratified invariant: the run path never blocks a commit on the guard's OWN
        limitation. A rule that cannot run is the same class as an exhausted budget —
        named, not enforced."""
        entry = _seed_entry(repo, "Weird rule")
        _arm_raw(repo, entry, {"type": "regex", "pattern": "(unclosed", "flags": "",
                               "paths": "", "message": "", "armed_at": "t"})
        _write(repo, "a.py", "# TODO fix this\n")
        _git(repo, "add", "a.py")

        result = guard_engine.guard_staged(str(repo))
        assert result["violations"] == []      # nothing to block on
        assert "error" not in result           # and not a guard malfunction either

    def test_reported_once_per_run_not_once_per_staged_file(self, repo):
        """A dead rule is a property of the RULE. Reporting it per staged file would print
        the same line ten times on a ten-file commit and crowd out the real gaps."""
        entry = _seed_entry(repo, "Weird rule")
        _arm_raw(repo, entry, {"type": "regex", "pattern": "(unclosed", "flags": "",
                               "paths": "", "message": "", "armed_at": "t"})
        for name in ("a.py", "b.py", "c.py"):
            _write(repo, name, "content\n")
        _git(repo, "add", "a.py", "b.py", "c.py")

        assert guard_engine.guard_staged(str(repo))["unchecked"] == [
            {"decision_id": entry["id"], "reason": "bad-pattern"}]

    def test_a_live_rule_alongside_a_dead_one_still_blocks(self, repo):
        live = _seed_entry(repo, "Never commit TODO markers")
        guard_engine.arm_guard(str(repo), live["id"], "regex", pattern="TODO")
        dead = _seed_entry(repo, "Weird rule")
        _arm_raw(repo, dead, {"type": "regex", "pattern": "(unclosed", "flags": "",
                              "paths": "", "message": "", "armed_at": "t"})
        _write(repo, "a.py", "# TODO fix this\n")
        _git(repo, "add", "a.py")

        result = guard_engine.guard_staged(str(repo))
        assert [v["decision_id"] for v in result["violations"]] == [live["id"]]
        assert result["unchecked"] == [{"decision_id": dead["id"],
                                        "reason": "bad-pattern"}]

    def test_a_dead_global_rule_is_reported_too(self, repo):
        entry = _seed_entry(repo, "Weird global rule", global_store=True)
        data = store.load_global()
        store.entry_by_id(data["entries"], entry["id"])["guard_check"] = {
            "type": "regex", "pattern": "(unclosed", "flags": "", "paths": "",
            "message": "", "armed_at": "t"}
        store.save_global(data)
        _write(repo, "a.py", "content\n")
        _git(repo, "add", "a.py")

        assert guard_engine.guard_staged(str(repo))["unchecked"] == [
            {"decision_id": entry["id"], "reason": "bad-pattern"}]


class TestDeadRuleRendering:
    """The two gap kinds share one list and get their own stderr line: one message cannot
    be true of both a staged file that went unscanned and a rule that never ran."""

    def test_dead_rule_line_names_the_decision_and_the_reason(self, capsys):
        cli._print_guard_unchecked([{"decision_id": "abcdef1234567890",
                                     "reason": "bad-pattern"}])
        err = capsys.readouterr().err
        assert "1 armed rule(s) could not run: abcdef12 (bad-pattern)" in err
        assert "staged file(s)" not in err

    def test_file_gaps_keep_their_own_line_unchanged(self, capsys):
        cli._print_guard_unchecked([{"file": "big.py", "reason": "too-large"},
                                    {"decision_id": "abcdef1234567890",
                                     "reason": "unsupported-check"}])
        err = capsys.readouterr().err
        assert "1 staged file(s) not checked by armed rules: big.py (too-large)" in err
        assert "1 armed rule(s) could not run: abcdef12 (unsupported-check)" in err

    def test_a_row_with_no_name_renders_the_reason_alone(self, capsys):
        """The partition is an else-bucket, so a malformed row lands on the rule line. It
        must not print a literal `None` as though that were a decision id."""
        cli._print_guard_unchecked([{"file": "", "reason": "too-large"}])
        err = capsys.readouterr().err
        assert "(too-large)" in err
        assert "None" not in err

    def test_more_than_the_cap_is_summarized(self, capsys):
        cli._print_guard_unchecked([{"decision_id": f"id{i}", "reason": "bad-pattern"}
                                    for i in range(7)])
        assert "+2 more" in capsys.readouterr().err
