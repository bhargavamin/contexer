"""`no_real_store_writes` protects the developer's real `~/.contexer` from the suite. These
tests point its helper at a FIXTURE directory instead - the one way to prove the guard bites
without doing the very thing it exists to forbid.

Written because the guard went blind: it named a fixed list of console artefacts and a
tmp_path marker, so the evidence spool, the reconcile log and the `.spool_maintained_` stamp
- three families added by the evidence-capture work, all keyed to the REAL repo slug - matched
nothing and would have been reported as a clean run."""
from tests.conftest import _leaked


def _dir(tmp_path, *names):
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if name.endswith("/"):
            path.mkdir(parents=True, exist_ok=True)
        else:
            path.write_text("{}", encoding="utf-8")
    return tmp_path


def test_missing_dir_is_not_a_leak(tmp_path):
    assert _leaked(tmp_path / "nope") == []


def test_the_pre_existing_families_still_report(tmp_path):
    real = _dir(tmp_path, "ui.log", "myrepo.deleted.json",
                ".insight_private_tmp_pytest_of_me-1234")
    assert _leaked(real) == [".insight_private_tmp_pytest_of_me-1234", "myrepo.deleted.json",
                             "ui.log"]


def test_the_evidence_capture_families_report(tmp_path):
    # Exactly the shapes found in the developer's real store dir after this branch's work.
    slug = "Users_me_repos_contexer-8596da46"
    real = _dir(tmp_path,
                f"evidence/{slug}/pending/20260825T122345261234Z-2e44fa3b.json",
                f".evidence_{slug}.json",              # retired single-sidecar spelling
                f".evidence_lock_{slug}",              # and its lock
                f".reconcile_{slug}.jsonl",
                f".reconcile_{slug}.lock",
                f".spool_maintained_{slug}")
    assert _leaked(real) == [
        ".evidence_Users_me_repos_contexer-8596da46.json",
        ".evidence_lock_Users_me_repos_contexer-8596da46",
        ".reconcile_Users_me_repos_contexer-8596da46.jsonl",
        ".reconcile_Users_me_repos_contexer-8596da46.lock",
        ".spool_maintained_Users_me_repos_contexer-8596da46",
        "evidence",
        f"evidence/{slug}",
        f"evidence/{slug}/pending",
    ]


def test_a_spool_appearing_inside_an_existing_evidence_dir_is_a_new_name(tmp_path):
    # The before/after diff is the guard's whole shape, so a family that already exists in the
    # real dir must not go silent: a NEW per-repo spool has to show up as a name nobody saw
    # before it, even though `evidence` itself is in the baseline.
    real = _dir(tmp_path, "evidence/repo_a/pending/e1.json")
    before = set(_leaked(real))
    _dir(tmp_path, "evidence/repo_b/pending/e2.json", "evidence/repo_a/held/abcd1234/x.json")
    assert [n for n in _leaked(real) if n not in before] == [
        "evidence/repo_a/held", "evidence/repo_a/held/abcd1234",
        "evidence/repo_b", "evidence/repo_b/pending"]


def test_a_live_hook_appending_to_an_existing_spool_is_not_a_leak(tmp_path):
    # The deliberate blind spot, pinned so nobody "fixes" it into a flaky run: the developer's
    # own SessionStart/PostToolUse hooks spool events into the real dir while the suite runs.
    # Files inside an existing spool are theirs; only new directories accuse a test.
    real = _dir(tmp_path, "evidence/repo_a/pending/e1.json")
    before = set(_leaked(real))
    _dir(tmp_path, "evidence/repo_a/pending/e2.json")
    assert [n for n in _leaked(real) if n not in before] == []


def test_the_share_retry_queue_is_not_mistaken_for_a_reconcile_log(tmp_path):
    # `.reconcile-outbox.json` (hyphen) is the share outbox a live session may write; only
    # the slug-keyed `.reconcile_<slug>.*` (underscore) belongs to the test suite's families.
    assert _leaked(_dir(tmp_path, ".reconcile-outbox.json")) == []
