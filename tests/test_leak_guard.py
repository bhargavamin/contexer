"""`no_real_store_writes` protects the developer's real `~/.contexer` from the suite. These
tests point its helper at a FIXTURE directory instead - the one way to prove the guard bites
without doing the very thing it exists to forbid.

Written because the guard went blind: it named a fixed list of console artefacts and a
tmp_path marker, so the evidence spool, the reconcile log and the `.spool_maintained_` stamp
- three families added by the evidence-capture work, all keyed to the REAL repo slug - matched
nothing and would have been reported as a clean run."""
import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

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


def test_a_late_handler_stays_sandboxed_after_session_teardown(tmp_path):
    """A finished xdist worker can still write while another worker checks for leaks.

    Release the writer only after ALL session fixtures (including the leak guard) have
    finished. The child's real home is disposable too, so the unfixed fixture can be
    convicted without touching the developer's home. Events make the race deterministic.
    """
    home = tmp_path / "home"
    home.mkdir()
    receipt = tmp_path / "writer.json"
    (tmp_path / "conftest.py").write_text(textwrap.dedent('''
        import json
        import threading
        from pathlib import Path
        from contexer import config as settings
        from contexer.ui import daemon, server

        pytest_plugins = ("tests.conftest",)
        release = threading.Event()
        writer = None

        def pytest_unconfigure(config):
            if writer is not None:
                release.set()
                writer.join(timeout=5)
                Path("writer.json").write_text(json.dumps({
                    "alive": writer.is_alive(),
                    "log": str(daemon.LOG_PATH),
                    "state": str(daemon.STATE_PATH),
                    "config": str(settings.CONFIG_PATH),
                }))
    '''), encoding="utf-8")
    (tmp_path / "test_late.py").write_text(textwrap.dedent('''
        import threading
        import conftest

        def test_handler_remains_in_flight():
            started = threading.Event()
            def late_log():
                started.set()
                if conftest.release.wait(timeout=30):
                    conftest.server._log("delayed handler after session teardown")
            conftest.writer = threading.Thread(target=late_log, daemon=True)
            conftest.writer.start()
            assert started.wait(timeout=5)
    '''), encoding="utf-8")
    root = Path(__file__).resolve().parents[1]
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "test_late.py", "-o", "addopts=",
         "--basetemp", str(tmp_path / "pytest-tmp")],
        cwd=tmp_path, env={**os.environ, "HOME": str(home), "PYTHONPATH": str(root),
                           "PYTEST_ADDOPTS": ""},
        capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stdout + done.stderr
    result = json.loads(receipt.read_text(encoding="utf-8"))
    assert not result["alive"], "the late writer did not finish"
    assert not (home / ".contexer" / "ui.log").exists(), done.stdout + done.stderr
    log = Path(result["log"])
    assert "delayed handler after session teardown" in log.read_text(encoding="utf-8")
    assert log.is_relative_to(tmp_path / "pytest-tmp")
    assert Path(result["state"]).parent == Path(result["config"]).parent == log.parent
