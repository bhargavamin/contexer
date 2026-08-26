"""Adversarial capture evaluations and rollout gates (hardening Task 09).

This file runs the frozen replay corpus as a PRODUCT evaluation rather than rebuilding it.
`tests/test_evidence_hardening_replays.py` is still the corpus and still owns the goldens;
everything here reads it through that module's own loader, so there is exactly one definition
of what a scenario is.

Three jobs, in order:

1. **A labelled directive corpus.** Recall on explicit user directives is a hard threshold
   (100 percent). Precision has no invented target: the measured number is reported and every
   false positive is FROZEN BY NAME in `_KNOWN_DIRECTIVE_FALSE_POSITIVES`, so a regression
   that adds a new one fails here and the ones that exist today are visible to a developer
   instead of buried in an aggregate.
2. **The adversarial cases the brief lists that nothing already covers.** Each brief case was
   searched for first; the ones already pinned elsewhere are cited in `task-09-report.md` and
   deliberately NOT duplicated here. What is left is in this file.
3. **The metrics report.** One `perf`-marked writer produces the machine-readable JSON and the
   short Markdown summary. It is perf-marked because it takes wall-clock medians, and this
   repository's convention is that timing lives in the perf tier - so `-m perf --no-cov` is
   what produces the artifact, and the default tier stays free of wall-clock assertions.

The hard thresholds themselves are asserted in the DEFAULT tier (below), never only inside the
report: a threshold that is checked only when someone remembers to run `-m perf` is not a gate.
"""
import json
import os
import subprocess
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from contexer import (
    candidates,
    evidence,
    guard_engine,
    policy_api,
    reconcile,
    spool,
    store,
)
from tests.test_evidence_hardening_replays import (
    GENERATED,
    _load,
    _median_ms,
    _realistic_corpus,
    _scenarios,
    _validated,
)

# Artifacts land under the packet's own directory, which `.superpowers/sdd/.gitignore` ignores
# wholesale (`*`). `tests/artifacts/` is NOT ignored by this repository's `.gitignore`, and the
# brief's first choice was conditional on the established ignored location - this is it.
ARTIFACTS = (Path(__file__).resolve().parent.parent / ".superpowers" / "sdd"
             / "evidence-capture-policy-evaluation-hardening" / "eval-artifacts")

_T0 = datetime(2026, 8, 1, 10, 0, 0, tzinfo=timezone.utc)
_NS = uuid.uuid5(uuid.NAMESPACE_URL, "https://contexer.dev/tests/evidence-hardening/evals")


def _armed(repo: str) -> list:
    """Every armed rule the guard would actually run, both stores - the same pair
    `guard_engine.guard_staged` gathers, so "nothing is armed" means what the guard means."""
    return (guard_engine._armed_rules(store.load(repo).get("entries") or [])
            + guard_engine._armed_rules(store.load_global().get("entries") or []))


def _event(name: str, kind: str, summary: str, *, session: str = "sess-a",
           files=None, offset: int = 0, repo_key: str = "/repo") -> dict:
    """One schema-valid event, deterministic the way the corpus is: uuid5 id, fixed clock."""
    return _validated({
        "schema_version": evidence.SCHEMA_VERSION,
        "event_id": str(uuid.uuid5(_NS, name)),
        "session_id": session, "repo_key": repo_key, "kind": kind,
        "occurred_at": (_T0 + timedelta(seconds=offset)).isoformat(),
        "source": "replay", "summary": summary, "files": list(files or []),
        "content_hash": None, "attributes": {},
    })


# ── 1. the labelled directive corpus ─────────────────────────────────────────────
#
# The brief's first adversarial addition: prescriptive text contained inside code, logs,
# quoted documentation, or tool output. The positives are the recall threshold; the negatives
# are text that MENTIONS a rule without the developer stating one.

# Explicit user directives. Every one must be detected - the brief's 100 percent recall bar.
# Shapes outside `store._CONSTRAINT_TRIGGER`'s documented vocabulary are deliberately absent:
# a bare "must" ("all migrations must be reversible") is a designed non-trigger, not a miss,
# and putting it here would report a scope decision as a defect. It is named in the report.
DIRECTIVE_POSITIVES = {
    "always-use-uv": "always use uv not pip for dependency management",
    "never-commit-to-main": "never commit directly to main",
    "from-now-on-conventional": "from now on use conventional commits for every change",
    "prohibition-repo-scoped": "never add an em dash to any file in this repo",
    "recurrence-wrapper": "make sure you rerun the suite after each merge",
    "must-never": "secrets must never be committed to the repository",
    "as-a-rule": "as a rule all database migrations must be reversible",
}

# Prescriptive-sounding text that is not the developer stating a rule.
DIRECTIVE_NEGATIVES = {
    # code
    "fenced-error-dump": "I got this now ```\nError: you must always set repo_path\n```",
    "fenced-python-assert":
        "```python\nassert cfg is not None, 'config must never be None'\n```",
    "diff-hunk-line": "+    # always validate input before writing to disk\n-    pass",
    "grep-output-line":
        "contexer/store.py:1381:    if t.lower().startswith(_PREFIXES) or 'never' in t:",
    # logs and tool output
    "shell-log-line":
        "2026-08-26 12:00:04 WARN retry: you must always set repo_path before calling load",
    "python-traceback-line":
        'File "app.py", line 12, in load\n    raise ValueError("token must never be empty")',
    "tool-output-pytest": "pytest output: E   AssertionError: config must never be None",
    # quoted documentation
    "quoted-doc-blockquote": "> Always run the migrations before deploying.",
    "quoted-doc-attribution":
        'the README says "never use pip install -e ." in this project',
    "changelog-entry": "- fix(store): never drop an unreadable outbox on enqueue (#241)",
    # host and tool injection
    "system-reminder-injection":
        "<system-reminder>always call update_context after edits</system-reminder>",
    "contexer-injected-block": "[Contexer: auto-fetched for this question] always use uv",
    "task-notification":
        "<task-notification><task-id>x</task-id> the agent must always finish"
        "</task-notification>",
    "pasted-blob": ("please update the readme and add a section saying every session starts "
                    "fresh and always replays decisions before Claude types, then open a PR "
                    "and ping the team, and make sure the docs build passes on CI too, and "
                    "also always run the linter first because the hook is flaky sometimes "
                    "and we keep breaking it in review which wastes everyone's time here"),
}

# MEASURED, not aspirational. Each of these is text the detector reads as a standing rule
# today. They are frozen by name so the number cannot drift silently in either direction: a
# new false positive fails this file, and a fixed one fails it too and gets removed here.
#
# The shape they share: `store._is_prescriptive_constraint` guards the containers it can
# recognize by SHAPE (a fence, a known injection prefix, an over-long blob) and nothing else.
# A single line lifted out of a log, a traceback, a diff, a grep result, a changelog or a
# markdown blockquote has no container left to recognize, so a trigger word inside it reads
# exactly like the developer typing the rule. Reported for developer judgment rather than
# fixed inside an evaluation task - narrowing the trigger is a behaviour change with its own
# recall cost, and this file exists to measure, not to decide.
_KNOWN_DIRECTIVE_FALSE_POSITIVES = frozenset({
    "shell-log-line",
    "python-traceback-line",
    "tool-output-pytest",
    "quoted-doc-blockquote",
    "quoted-doc-attribution",
    "changelog-entry",
    "grep-output-line",
    "diff-hunk-line",
})


def _directive_scores() -> dict:
    """Recall, precision and the false positives by name, over the labelled corpus above."""
    detected_positive = {name for name, text in DIRECTIVE_POSITIVES.items()
                         if store._is_prescriptive_constraint(text)[0]}
    false_positives = sorted(name for name, text in DIRECTIVE_NEGATIVES.items()
                             if store._is_prescriptive_constraint(text)[0])
    hits, wrong = len(detected_positive), len(false_positives)
    return {
        "positives": len(DIRECTIVE_POSITIVES),
        "negatives": len(DIRECTIVE_NEGATIVES),
        "recall": hits / len(DIRECTIVE_POSITIVES),
        "precision": hits / (hits + wrong) if (hits + wrong) else 1.0,
        "missed": sorted(set(DIRECTIVE_POSITIVES) - detected_positive),
        "false_positives": false_positives,
    }


def test_every_explicit_user_directive_is_captured():
    """Acceptance threshold: 100 percent recall for explicit user directives."""
    scores = _directive_scores()
    assert scores["missed"] == [], "an explicit directive was not detected"
    assert scores["recall"] == 1.0


def test_the_false_positive_set_is_exactly_the_one_on_record():
    """Precision has no invented target, so this pins the SET rather than a number. Failing
    in the "too few" direction is just as informative: it means a container the detector could
    not see before is recognized now, and the record here is out of date."""
    assert set(_directive_scores()["false_positives"]) == _KNOWN_DIRECTIVE_FALSE_POSITIVES


@pytest.mark.parametrize("name", sorted(
    set(DIRECTIVE_NEGATIVES) - _KNOWN_DIRECTIVE_FALSE_POSITIVES))
def test_a_recognizable_container_is_never_read_as_a_directive(name):
    """The half that DOES hold: a fenced block, a host injection prefix and an over-long
    pasted blob are all refused, each by its own guard in `_is_prescriptive_constraint`."""
    assert store._is_prescriptive_constraint(DIRECTIVE_NEGATIVES[name])[0] is False


def test_a_false_positive_that_slips_through_is_still_only_one_reviewable_decision(tmp_repo):
    """The blast radius of the row above, measured rather than assumed.

    A log line the detector misreads becomes ONE stored constraint. It does not arm a policy,
    it does not anchor a file, and it cannot block a commit - `arm_guard` is a separate
    explicit gesture (runbook invariant 9). That is what keeps a precision miss a review-queue
    cost rather than an enforcement one.
    """
    text = DIRECTIVE_NEGATIVES["quoted-doc-blockquote"]
    entry_id, _, _ = store.capture_user_constraint(tmp_repo, text, "sess-a")
    assert entry_id, "the corpus row says this one is captured"

    (entry,) = store.load(tmp_repo)["entries"]
    assert entry.get("guard_check") is None
    assert not entry.get("source_files")
    assert _armed(tmp_repo) == []


# ── 2. adversarial cases with no existing coverage ───────────────────────────────

def _relations(candidate: dict) -> dict:
    return {row["event_id"]: (row["relation"], row["certainty"])
            for row in candidate["signals"]}


def test_a_directive_naming_one_file_ranks_its_sibling_strictly_lower(tmp_repo):
    """Brief case: a directive names one file while an unrelated NEARBY file changes.

    Scenario 03 covers an unrelated edit in another part of the tree, BEFORE the directive.
    This is the sharper version: the sibling sits in the same directory as the file the
    directive names, and it is edited AFTER, inside the proximity window.

    What actually happens is worth stating plainly rather than wishing away. A `user_directive`
    carries no `files` of its own, so `causal_forward` is the only link an edit after it can
    have - and that link IS an anchor. So the sibling does reach `source_files`. That is a
    measured wrong-file attachment, reported as such, and NOT a breach of runbook invariant 6,
    which is about UNCERTAIN links: the sibling is `supporting`, the named file is `confirmed`
    and structural, and a prior unrelated edit is `uncertain` and never anchors at all. This
    test pins the ordering between those three tiers, which is the property that keeps the
    invariant checkable at all.
    """
    sibling = "src/generated/models.ts"
    events = [
        _event("sib-earlier", "file_changed", "typo in the readme", files=["README.md"],
               offset=0),
        _event("sib-directive", "user_directive",
               f"Do not edit {GENERATED} directly. Change openapi/schema.yaml and regenerate.",
               offset=60),
        _event("sib-named", "file_changed", "regenerated the client", files=[GENERATED],
               offset=90),
        _event("sib-sibling", "file_changed", "regenerated the models", files=[sibling],
               offset=120),
    ]
    got = candidates.aggregate_candidates(events, [])["candidates"]
    (candidate,) = [c for c in got if c["kind"] == "new"]
    seen = _relations(candidate)

    assert seen[events[2]["event_id"]] == ("structural", "confirmed"), "the named path"
    assert seen[events[3]["event_id"]] == ("causal_forward", "supporting"), "the sibling"

    assert candidate["source_files"] == [GENERATED, sibling]
    assert candidate["possible_source_files"] == ["README.md"]
    # The backward link is non-consuming, so the prior edit's own signal row stays on the
    # leftover candidate: it is reported as evidence nothing explained, never as this
    # decision's corroboration.
    (leftover,) = [c for c in got if c["kind"] == "insufficient"]
    assert _relations(leftover)[events[0]["event_id"]] == ("unrelated", "uncertain")

    _spool(tmp_repo, events)
    assert reconcile.reconcile_session(tmp_repo)["proposed"] == 1
    (entry,) = store.load(tmp_repo)["entries"]
    assert "README.md" not in (entry.get("source_files") or [])
    assert "README.md" not in (entry.get("anchor_candidates") or [])
    assert guard_engine.decisions_for_files(tmp_repo, ["README.md"]) == []


def test_two_repos_with_the_same_basename_keep_separate_spools(tmp_repo):
    """Brief case: two repos sharing a basename must not share a spool.

    Scenario 18 proves evidence from another repo never enters this one's candidates. This
    proves the layer underneath: the spool is keyed on `store.repo_slug`, which is the whole
    PATH with non-alphanumerics replaced, so the basename cannot collide two repos into one
    directory in the first place.
    """
    left = os.path.join(tmp_repo, "team-a", "app")
    right = os.path.join(tmp_repo, "team-b", "app")
    assert os.path.basename(left) == os.path.basename(right)

    spool.append_evidence(left, _event("basename-left", "user_directive", "always use uv"))
    spool.append_evidence(right, _event("basename-right", "user_directive",
                                        "always use poetry"))

    assert store.repo_slug(left) != store.repo_slug(right)
    assert [e["summary"] for e in spool.list_pending_evidence(left)] == ["always use uv"]
    assert [e["summary"] for e in spool.list_pending_evidence(right)] == ["always use poetry"]


def test_a_worktree_and_its_main_checkout_share_one_spool(tmp_path, monkeypatch):
    """Brief case: a worktree and the main checkout share canonical memory.

    `_canonical_store_key` collapses a linked worktree onto its main worktree for every
    slug-keyed artifact, and `spool._repo_dir` is slug-keyed - so evidence appended from a
    worktree session is reconciled by a session in the main checkout rather than stranded in a
    second spool nobody scans. The alternative fails silently, which is why it is pinned here
    rather than inferred from the store-key tests.
    """
    monkeypatch.setattr(store, "STORE_DIR", tmp_path / ".contexer")
    store._CANON_CACHE.clear()
    root = Path(os.path.realpath(tmp_path))
    main = root / "main"
    main.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "e@t.local"),
                 ("config", "user.name", "T"), ("config", "commit.gpgsign", "false")):
        subprocess.run(["git", "-C", str(main), *args], check=True, capture_output=True)
    (main / "f.txt").write_text("x")
    subprocess.run(["git", "-C", str(main), "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(main), "commit", "-qm", "init"], check=True,
                   capture_output=True)
    worktree = str(root / "wt")
    subprocess.run(["git", "-C", str(main), "worktree", "add", "-q", worktree], check=True,
                   capture_output=True)

    try:
        spool.append_evidence(worktree, _event("worktree-directive", "user_directive",
                                               "always run the suite before pushing"))
        assert [e["summary"] for e in spool.list_pending_evidence(str(main))] == [
            "always run the suite before pushing"]
        assert spool._repo_dir(worktree) == spool._repo_dir(str(main))
    finally:
        store._CANON_CACHE.clear()


def test_a_skewed_clock_changes_neither_candidate_identity_nor_its_anchors():
    """Brief case: malformed timestamps and clock skew.

    Malformed stamps are refused by the schema (`tests/fixtures/evidence/invalid/` carries
    both the naive and the unparseable case), so what is left is a VALID but wrong one.

    Two properties, and they pull in opposite directions on purpose. `candidate_id` is a uuid5
    over the SORTED EVENT IDS, so a skewed clock cannot change a candidate's identity - which
    is what stops a skewed host filing a second copy of a candidate already held. And the only
    link that survives the skew is the STRUCTURAL one, which is time-direction-blind by design
    because a shared identifier is proof whichever way the clock ran. A skewed edit that shares
    no identifier falls outside `_PROXIMITY_SECONDS` and attaches nothing at all, in either
    direction: the safe way round, since the failure mode is a dropped link rather than a
    fabricated anchor.
    """
    named = [
        _event("skew-directive", "user_directive",
               f"Do not edit {GENERATED} directly. Change openapi/schema.yaml and regenerate.",
               offset=0),
        _event("skew-edit", "file_changed", "regenerated the client", files=[GENERATED],
               offset=30),
    ]
    far = (_T0 + timedelta(days=3650)).isoformat()
    skewed = [named[0], dict(named[1], occurred_at=far)]

    (straight,) = candidates.aggregate_candidates(named, [])["candidates"]
    (crooked,) = candidates.aggregate_candidates(skewed, [])["candidates"]

    assert straight["candidate_id"] == crooked["candidate_id"], "identity is id-derived"
    assert straight["source_files"] == crooked["source_files"] == [GENERATED]
    assert _relations(crooked)[named[1]["event_id"]] == ("structural", "confirmed")

    unnamed = [named[0], _event("skew-unnamed", "file_changed", "touched an unrelated file",
                                files=["src/other/thing.ts"], offset=0)]
    unnamed[1] = dict(unnamed[1], occurred_at=far)
    got = candidates.aggregate_candidates(unnamed, [])["candidates"]
    (seed,) = [c for c in got if c["kind"] == "new"]
    assert seed["source_files"] == [], "an unnamed far-future edit anchors nothing"
    assert seed["possible_source_files"] == []


def test_reconciliation_survives_a_corrupt_live_store_and_tombstone_sidecar(tmp_repo):
    """Brief case: corrupt live store and tombstone sidecars.

    `.gap` and the held manifest are already covered (`test_spool.py`, scenario 17). These two
    are the sidecars reconciliation reads that nothing pinned: the live store it classifies
    against, and the tombstone sidecar the reconsideration lane looks an inactive decision up
    in. Both read fail-soft as empty, so the pass must complete without losing the evidence -
    an unreadable store is not a licence to delete a pending event.
    """
    _spool(tmp_repo, [_event("corrupt-directive", "user_directive",
                             "never commit a generated file")])
    store._store_path(tmp_repo).parent.mkdir(parents=True, exist_ok=True)
    store._store_path(tmp_repo).write_text("{ not json", encoding="utf-8")
    store._deleted_path(tmp_repo).write_text("{ also not json", encoding="utf-8")

    receipt = reconcile.reconcile_session(tmp_repo)

    assert receipt["incomplete"] is False
    assert receipt["proposed"] == 1
    assert (spool.evidence_diagnostics(tmp_repo)["gap"] or {}).get("drops", 0) == 0
    entries = store.load(tmp_repo)["entries"]
    assert [e["status"] for e in entries] == ["pending_approval"]


def test_an_armed_old_revision_judges_the_approved_content_not_the_pending_one(tmp_repo):
    """Brief case: an armed old revision while a new content proposal is pending.

    The rule stays on the APPROVED revision. A pending `proposed_revision` that would widen it
    changes nothing until a human approves - runbook invariant 5 (pending content never
    participates in a blocking verdict) meeting invariant 9 (approving knowledge does not arm
    a policy) on the same entry. `test_review_impact.py` pins how this RENDERS; this pins what
    the evaluator actually answers.
    """
    ok, entry_id = store.update_decision(tmp_repo, "Never hardcode the legacy API token.",
                                         "sess-0", "constraint", created_by="human")
    assert ok
    data = store.load(tmp_repo)
    data["entries"][0]["status"] = "approved"
    store.save(tmp_repo, data)
    guard_engine.arm_guard(tmp_repo, entry_id, "regex", pattern=r"LEGACY_TOKEN")
    assert store.update_decision(tmp_repo, "Never hardcode the legacy or the vendor token.",
                                 "sess-1", "constraint", replace_id=entry_id)[0]

    entry = store.entry_by_id(store.load(tmp_repo)["entries"], entry_id)
    assert entry["proposed_revision"], "the update is pending, not applied"

    old = policy_api.evaluate_operation(tmp_repo, operation="commit", files=["src/app.py"],
                                        artifact_kind="diff", artifact="LEGACY_TOKEN = 1")
    new = policy_api.evaluate_operation(tmp_repo, operation="commit", files=["src/app.py"],
                                        artifact_kind="diff", artifact="VENDOR_TOKEN = 1")

    assert old["verdict"] == "block", "the armed approved revision still enforces"
    assert new["verdict"] != "block", "the pending revision widens nothing yet"


def test_approving_a_knowledge_decision_arms_no_policy(tmp_repo):
    """Brief case: an approved knowledge decision with no armed policy (invariant 9).

    `test_review_impact.py` pins that the review RENDER never arms one. This pins the
    transition itself from the evaluator's side: the same operation is non-blocking before and
    after approval, and only a separate explicit `arm_guard` changes that.
    """
    ok, entry_id, _ = store.update_decision_with_meta(
        tmp_repo, "Never hardcode the legacy API token.", "sess-0", "constraint",
        created_by="ai", force_pending=True)
    assert ok
    request = dict(operation="commit", files=["src/app.py"], artifact_kind="diff",
                   artifact="LEGACY_TOKEN = 1")

    assert policy_api.evaluate_operation(tmp_repo, **request)["verdict"] != "block"
    store.approve_decision(tmp_repo, entry_id, "approve")
    assert store.entry_by_id(store.load(tmp_repo)["entries"], entry_id)["status"] == "approved"

    assert _armed(tmp_repo) == []
    assert policy_api.evaluate_operation(tmp_repo, **request)["verdict"] != "block"

    guard_engine.arm_guard(tmp_repo, entry_id, "regex", pattern=r"LEGACY_TOKEN")
    assert policy_api.evaluate_operation(tmp_repo, **request)["verdict"] == "block"


# ── 3. rollout stages ────────────────────────────────────────────────────────────
#
# The seven stages are documented in `task-09-report.md` with the disable mechanism for each.
# Six of the seven are already pinned by an existing test, cited there. The one that was not
# is the property the whole list depends on: turning a stage OFF must not destroy evidence or
# rewrite decision history.

def test_turning_the_guard_off_destroys_no_evidence_and_no_history(tmp_repo, monkeypatch):
    """Rollout stage 6's disable mechanism, measured on what it must NOT touch.

    `CONTEXER_GUARD=0` short-circuits before any work, and `disarm_guard` drops the rule. Both
    leave the spool, the decision and its revision history exactly as they were - the brief's
    "independently disableable without deleting evidence or changing decision history".
    """
    _spool(tmp_repo, [_event("rollback-directive", "user_directive",
                             "never commit a generated file")])
    assert reconcile.reconcile_session(tmp_repo)["proposed"] == 1
    (entry_id,) = [e["id"] for e in store.load(tmp_repo)["entries"]]
    store.approve_decision(tmp_repo, entry_id, "approve")
    guard_engine.arm_guard(tmp_repo, entry_id, "regex", pattern=r"generated")

    def _snapshot() -> tuple:
        entry = store.entry_by_id(store.load(tmp_repo)["entries"], entry_id)
        held = {cid: sorted(e["event_id"] for e in spool.held_events(tmp_repo, cid))
                for cid in spool.held_candidates(tmp_repo)}
        return (entry["revisions"], entry["current_revision_id"], entry["status"],
                entry.get("evidence_summary"), held,
                spool.evidence_diagnostics(tmp_repo)["gap"])

    before = _snapshot()
    assert before[4], "the candidate's evidence is held, so there is something to lose"

    with monkeypatch.context() as patched:
        patched.setenv("CONTEXER_GUARD", "0")
        assert guard_engine.guard_staged(tmp_repo)["skipped"] == "env"
    assert _snapshot() == before

    guard_engine.disarm_guard(tmp_repo, entry_id)
    after = _snapshot()
    assert after[:4] == before[:4] and after[4:] == before[4:]
    assert _armed(tmp_repo) == []


# ── the metrics ──────────────────────────────────────────────────────────────────
#
# Each metric is a function so the default-tier threshold tests and the perf-tier report read
# the SAME number rather than two implementations of it.

_LABELLED = ["01-directive-then-edit", "03-unrelated-edit-before-directive",
             "04-directive-duplicates-live-decision",
             "05-directive-repeated-in-a-second-session",
             "06-agent-conclusion-with-rationale",
             "07-ignored-decision-restated-by-a-directive",
             "08-tombstoned-decision-restated-by-a-directive",
             "18-wrong-session-and-wrong-repo-evidence"]


def _key(candidate: dict) -> tuple:
    return (candidate["kind"], candidate["title"], candidate["target_decision_id"])


def _candidate_quality() -> dict:
    """Recall, precision and wrong-file attachment over the labelled golden scenarios."""
    expected = produced = matched = files = wrong = uncertain_promoted = 0
    for name in _LABELLED:
        doc = _load(name)
        got = candidates.aggregate_candidates(doc["events"], doc["decisions"])["candidates"]
        want = {_key(c): c for c in doc["expected"]}
        expected += len(want)
        produced += len(got)
        for candidate in got:
            golden = want.get(_key(candidate))
            if golden is None:
                continue
            matched += 1
            files += len(candidate["source_files"])
            wrong += len([f for f in candidate["source_files"]
                          if f not in golden["source_files"]])
            uncertain_promoted += len(set(candidate.get("possible_source_files", []))
                                      & set(candidate["source_files"]))
    return {
        "scenarios": len(_LABELLED),
        "expected_candidates": expected, "produced_candidates": produced,
        "recall": matched / expected if expected else 1.0,
        "precision": matched / produced if produced else 1.0,
        "attached_files": files,
        "wrong_file_attachment_rate": wrong / files if files else 0.0,
        "uncertain_file_promotion_rate": (uncertain_promoted / files) if files else 0.0,
    }


def _adversarial_file_attachment() -> dict:
    """The wrong-file rate the goldens cannot show, on the input built to produce it.

    The labelled scenarios carry three anchored files between them, all of them right, so
    their wrong-file rate is 0.0 over a small denominator. That number on its own would read
    as "wrong files never attach", which is not what the system guarantees: a `user_directive`
    names no files, so any edit inside `_PROXIMITY_SECONDS` after it attaches by
    `causal_forward` and IS an anchor. The sibling case is measured separately and reported
    beside the golden rate rather than averaged into it - two corpora, two numbers.
    """
    sibling = "src/generated/models.ts"
    events = [
        _event("sib-directive", "user_directive",
               f"Do not edit {GENERATED} directly. Change openapi/schema.yaml and regenerate.",
               offset=60),
        _event("sib-named", "file_changed", "regenerated the client", files=[GENERATED],
               offset=90),
        _event("sib-sibling", "file_changed", "regenerated the models", files=[sibling],
               offset=120),
    ]
    (candidate,) = [c for c in candidates.aggregate_candidates(events, [])["candidates"]
                    if c["kind"] == "new"]
    anchored = candidate["source_files"]
    return {
        "case": "directive names one file, a sibling changes inside the window",
        "anchored": anchored,
        "intended": [GENERATED],
        "wrong_file_attachment_rate": (
            len([f for f in anchored if f != GENERATED]) / len(anchored)) if anchored else 0.0,
        "certainty_of_the_wrong_one": dict(_relations(candidate))[
            events[2]["event_id"]][1],
    }


def _agent_only_reconsideration_openings() -> int:
    """Scenario 09: agent-only evidence mentioning an inactive decision. Must open nothing."""
    doc = _load("09-inactive-decision-mentioned-by-a-conclusion")
    got = candidates.aggregate_candidates(doc["events"], doc["decisions"])["candidates"]
    return len([c for c in got if c["kind"] == "reconsider"])


def _review_items_for_a_realistic_session() -> int:
    events = _realistic_corpus()
    return len(candidates.aggregate_candidates(events, [])["candidates"])


def _spool(repo: str, events) -> None:
    for event in events:
        assert spool.append_evidence(repo, event)["status"] == "stored"


def _replay_loss(repo: str) -> dict:
    """Acknowledged-evidence loss and duplicate-proposal rate, measured on a real replay.

    The events are acknowledged (`stored`), reconciled, then reconciled AGAIN. Nothing may be
    lost across the two passes and the second pass may propose nothing: runbook invariants 3
    and 7, expressed as the two rates the brief asks for.
    """
    events = [
        _event("loss-directive", "user_directive",
               f"Do not edit {GENERATED} directly. Change openapi/schema.yaml and regenerate.",
               offset=0),
        _event("loss-edit", "file_changed", "regenerated the client", files=[GENERATED],
               offset=30),
    ]
    _spool(repo, events)
    first = reconcile.reconcile_session(repo)
    second = reconcile.reconcile_session(repo)

    held = {e["event_id"] for cid in spool.held_candidates(repo)
            for e in spool.held_events(repo, cid)}
    pending = {e["event_id"] for e in spool.list_pending_evidence(repo)}
    acknowledged = {e["event_id"] for e in events}
    return {
        "acknowledged": len(acknowledged),
        "recoverable": len(acknowledged & (held | pending)),
        "loss_rate": 1 - len(acknowledged & (held | pending)) / len(acknowledged),
        "first_pass_proposed": first["proposed"],
        "duplicate_proposals": second["proposed"],
        "gap": (spool.evidence_diagnostics(repo)["gap"] or {}),
    }


def _policy_confusion(repo: str) -> dict:
    """False blocks and false allows over labelled operations against one armed rule."""
    ok, entry_id = store.update_decision(repo, "Never hardcode the legacy API token.",
                                         "sess-0", "constraint", created_by="human")
    assert ok
    data = store.load(repo)
    data["entries"][0]["status"] = "approved"
    store.save(repo, data)
    guard_engine.arm_guard(repo, entry_id, "regex", pattern=r"LEGACY_TOKEN",
                           paths="src/*.py")
    # An unratified decision beside it: nothing it says may ever block (invariant 5).
    assert store.update_decision_with_meta(
        repo, "Never call the vendor endpoint from a request handler.", "sess-0",
        "constraint", created_by="ai", force_pending=True)[0]

    labelled = [
        ("armed-pattern-in-scope", "src/app.py", "LEGACY_TOKEN = 1", "block"),
        ("armed-pattern-out-of-scope", "docs/notes.md", "LEGACY_TOKEN = 1", "allow"),
        ("clean-file-in-scope", "src/app.py", "TOKEN = os.environ['T']", "allow"),
        ("pending-decision-subject", "src/app.py", "vendor_endpoint()", "allow"),
        ("no-artifact-at-all", "src/app.py", "", "allow"),
    ]
    false_block, false_allow, rows = 0, 0, {}
    for name, path, artifact, expected in labelled:
        result = policy_api.evaluate_operation(
            repo, operation="commit", files=[path],
            artifact_kind="diff" if artifact else "", artifact=artifact)
        blocked = result["verdict"] == "block"
        rows[name] = result["verdict"]
        if blocked and expected == "allow":
            false_block += 1
        if not blocked and expected == "block":
            false_allow += 1
    return {"operations": len(labelled), "false_block": false_block,
            "false_allow": false_allow, "verdicts": rows}


def _hook_append(repo: str) -> dict:
    """Hook append cost and concurrent-writer behaviour.

    The spool takes no locks, so "concurrent writers" is measured as what it actually
    guarantees: N writers naming N distinct files, all of which survive. A uuid event id is
    what removes the contention, so this is the property, not a timing race.
    """
    writers = 8
    events = [_event(f"concurrent-{i}", "user_directive", f"always rule number {i}",
                     session=f"sess-{i}", offset=i) for i in range(writers)]
    for event in events:
        assert spool.append_evidence(repo, event)["status"] == "stored"
    survived = len({e["event_id"] for e in spool.list_pending_evidence(repo)}
                   & {e["event_id"] for e in events})
    median = _median_ms(lambda: spool.append_evidence(
        repo, _event(f"timed-{uuid.uuid4()}", "user_directive", "always time this")))
    return {"concurrent_writers": writers, "events_survived": survived,
            "append_median_ms": round(median, 3)}


def _teams_lifecycle() -> dict:
    """Retry and duplication counts for the lifecycle wire, read off the Task 08 suite rather
    than re-driven here: those tests own the transport fakes and the contract fixture."""
    return {
        "source": "tests/test_lifecycle_sync.py",
        "retries_per_refusal": 1,
        "duplicate_events_after_retry": 0,
        "evidence": [
            "test_a_refused_lifecycle_payload_still_syncs_the_base_decision",
            "test_a_blocked_delta_stays_durably_pending_in_the_outbox",
            "test_a_pending_delta_is_not_re_offered_while_the_capability_is_unchanged",
            "test_a_byte_identical_resend_after_the_inclusive_cursor_is_not_a_new_row",
            "test_a_delta_refused_DURING_a_drain_survives_the_drain",
        ],
    }


# ── the hard thresholds, asserted in the default tier ────────────────────────────

def test_no_uncertain_file_is_ever_promoted_to_an_anchor():
    assert _candidate_quality()["uncertain_file_promotion_rate"] == 0.0


def test_no_labelled_candidate_attaches_a_file_its_golden_does_not_name():
    assert _candidate_quality()["wrong_file_attachment_rate"] == 0.0


def test_agent_only_evidence_opens_no_reconsideration():
    assert _agent_only_reconsideration_openings() == 0


def test_acknowledged_evidence_survives_a_replay_with_no_duplicate_proposal(tmp_repo):
    loss = _replay_loss(tmp_repo)
    assert loss["loss_rate"] == 0.0
    assert loss["duplicate_proposals"] == 0
    assert loss["gap"].get("drops", 0) == 0


def test_the_policy_evaluator_neither_over_blocks_nor_under_blocks(tmp_repo):
    confusion = _policy_confusion(tmp_repo)
    assert confusion["false_block"] == 0 and confusion["false_allow"] == 0


def test_no_concurrent_hook_write_is_lost(tmp_repo):
    assert _hook_append(tmp_repo)["events_survived"] == 8


def test_an_unavailable_host_signal_is_never_reported_as_captured_zero():
    """The last acceptance threshold: an unobservable lane says so rather than reporting a
    count of zero. `evidence.COVERAGE_FIELDS` are capability words, never numbers."""
    for host in ("claude", "codex", "cursor", "gemini", "nonesuch"):
        block = evidence.host_coverage(host)
        for field in evidence.COVERAGE_FIELDS:
            assert isinstance(block[field], str) and block[field], (host, field)
            assert block[field] != "0"
    assert evidence.host_coverage("cursor")["file_changes"] == "unavailable"


# ── the report ───────────────────────────────────────────────────────────────────

@pytest.mark.perf
def test_the_evaluation_report_is_written(tmp_repo, monkeypatch):
    """Produce the machine-readable JSON and the Markdown summary.

    `perf`-marked because the latency rows are wall-clock medians and this repository keeps
    timing in the perf tier - so `uv run pytest -m perf --no-cov` writes the artifact, and no
    default-tier run depends on a clock. Every threshold above is asserted in the default tier
    too, so this file is a report rather than the gate.
    """
    quality = _candidate_quality()
    directives = _directive_scores()
    loss = _replay_loss(tmp_repo)
    confusion = _policy_confusion(tmp_repo)
    hooks = _hook_append(tmp_repo)

    distinct = _load("14-thousand-distinct-statements")["events"]
    boilerplate = _load("15-thousand-boilerplate-statements")["events"]
    realistic = _realistic_corpus()
    latency = {
        "realistic_ms": round(_median_ms(
            lambda: candidates.aggregate_candidates(realistic, [])), 2),
        "distinct_1000_ms": round(_median_ms(
            lambda: candidates.aggregate_candidates(distinct, [])), 2),
        "boilerplate_1000_ms": round(_median_ms(
            lambda: candidates.aggregate_candidates(boilerplate, [])), 2),
        "empty_spool_reconcile_ms": round(_median_ms(
            lambda: reconcile.reconcile_session(str(Path(tmp_repo) / "empty"))), 2),
        "hook_append_ms": hooks["append_median_ms"],
        "note": "aggregate_candidates alone, median of 3 after warm-up (ledger ruling D6)",
    }

    report = {
        "task": "09-adversarial-evals-and-rollout",
        "corpus_scenarios": len(_scenarios()),
        "candidate_quality": quality,
        "adversarial_file_attachment": _adversarial_file_attachment(),
        "directive_detection": directives,
        "agent_only_reconsiderations": _agent_only_reconsideration_openings(),
        "review_items_per_realistic_session": _review_items_for_a_realistic_session(),
        "evidence_loss": loss,
        "policy_confusion": confusion,
        "hook_append": hooks,
        "teams_lifecycle": _teams_lifecycle(),
        "latency_ms": latency,
    }

    ARTIFACTS.mkdir(parents=True, exist_ok=True)
    (ARTIFACTS / "task-09-evaluation.json").write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (ARTIFACTS / "task-09-evaluation.md").write_text(_markdown(report), encoding="utf-8")

    assert (ARTIFACTS / "task-09-evaluation.json").is_file()
    assert (ARTIFACTS / "task-09-evaluation.md").is_file()


def _markdown(report: dict) -> str:
    quality, directives = report["candidate_quality"], report["directive_detection"]
    loss, confusion = report["evidence_loss"], report["policy_confusion"]
    rows = [
        ("decision-candidate recall (labelled fixtures)", f"{quality['recall']:.2f}"),
        ("proposal precision", f"{quality['precision']:.2f}"),
        ("wrong-file attachment rate (labelled goldens, "
         f"{quality['attached_files']} anchored files)",
         f"{quality['wrong_file_attachment_rate']:.2f}"),
        ("wrong-file attachment rate (adversarial sibling case)",
         f"{report['adversarial_file_attachment']['wrong_file_attachment_rate']:.2f}"),
        ("uncertain-file promotion rate", f"{quality['uncertain_file_promotion_rate']:.2f}"),
        ("evidence acknowledgement-to-receipt loss rate", f"{loss['loss_rate']:.2f}"),
        ("duplicate proposals after replay", str(loss["duplicate_proposals"])),
        ("agent-only reconsiderations opened", str(report["agent_only_reconsiderations"])),
        ("explicit-directive recall", f"{directives['recall']:.2f}"),
        ("directive precision", f"{directives['precision']:.2f}"),
        ("review items per realistic session",
         str(report["review_items_per_realistic_session"])),
        ("policy false blocks", str(confusion["false_block"])),
        ("policy false allows", str(confusion["false_allow"])),
        ("concurrent hook writes surviving",
         f"{report['hook_append']['events_survived']}/"
         f"{report['hook_append']['concurrent_writers']}"),
        ("Teams lifecycle duplicates after retry",
         str(report["teams_lifecycle"]["duplicate_events_after_retry"])),
    ]
    latency = report["latency_ms"]
    lines = ["# Task 09 evaluation", "",
             "Generated by `uv run pytest -m perf --no-cov "
             "tests/test_evidence_hardening_evals.py`.", "",
             "## Capture quality", "", "| Metric | Value |", "| --- | --- |"]
    lines += [f"| {name} | {value} |" for name, value in rows]
    lines += ["", "## Latency", "",
              f"Measured as {latency['note']}.", "",
              "| Workload | Median |", "| --- | --- |"]
    lines += [f"| {key.removesuffix('_ms').replace('_', ' ')} | {latency[key]}ms |"
              for key in ("realistic_ms", "distinct_1000_ms", "boilerplate_1000_ms",
                          "empty_spool_reconcile_ms", "hook_append_ms")]
    lines += ["", "## Directive false positives", "",
              "Precision has no invented target. Every false positive by fixture name:", ""]
    lines += [f"- `{name}`" for name in directives["false_positives"]] or ["- none"]
    lines += ["", "Each is prescriptive text lifted out of a container "
              "(a log line, a traceback, a diff hunk, a grep result, a changelog entry, "
              "a markdown blockquote) that `store._is_prescriptive_constraint` has no shape "
              "left to recognize. A capture that slips through is one reviewable decision: "
              "it arms no policy and anchors no file.", ""]
    return "\n".join(lines)
