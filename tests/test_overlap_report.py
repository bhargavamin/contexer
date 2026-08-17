"""Tests for the store-hygiene overlap report: store.overlap_report clustering and
its rendering inside the `contexer review` flow. Read-only surface - never merges
or deletes; review's pending-item handling itself is untouched."""
import builtins

import pytest

from contexer import store
from contexer.cli import review

# The real-world trio that motivated the feature. Pairwise (max-based) overlap:
# a-b 0.286, a-c 0.400, b-c 0.300; containment (|∩|/min): a-b 0.667, a-c 0.571,
# b-c 1.000. So a-c links via pairwise >= 0.35 and b-c via containment >= 0.7,
# and union-find transitivity lands all three in ONE cluster.
TRIO = [
    "Always ensure you commit changes on approval",
    "Always commit automatically",
    "Always commit automatically after approvals and ensure you double cfonirm",
]


def _add(repo, content, subtype="constraint"):
    stored, entry_id = store.update_decision(repo, content, "s1", subtype=subtype)
    assert stored, f"fixture rule was filtered as a duplicate: {content!r}"
    return entry_id


class TestOverlapReport:
    def test_trio_lands_in_one_cluster(self, tmp_repo):
        for rule in TRIO:
            _add(tmp_repo, rule)
        clusters = store.overlap_report(tmp_repo)
        assert len(clusters) == 1
        contents = {d["content"] for d in clusters[0]}
        assert contents == set(TRIO)

    def test_cluster_dict_shape(self, tmp_repo):
        ids = [_add(tmp_repo, rule) for rule in TRIO]
        (cluster,) = store.overlap_report(tmp_repo)
        for d in cluster:
            assert set(d) == {"id", "subtype", "status", "content"}
            assert len(d["id"]) == 8
            assert d["subtype"] == "constraint"
            assert d["status"] in ("approved", "suggested", "pending_approval")
        assert {d["id"] for d in cluster} == {i[:8] for i in ids}

    def test_unrelated_rules_not_clustered(self, tmp_repo):
        _add(tmp_repo, "Never store plaintext passwords in application logs")
        _add(tmp_repo, "Use tabs for indentation across the Go services", subtype="convention")
        _add(tmp_repo, "Database migrations require an explicit rollback script")
        assert store.overlap_report(tmp_repo) == []

    def test_ignored_entries_excluded(self, tmp_repo):
        ids = [_add(tmp_repo, rule) for rule in TRIO]
        ok, _ = store.approve_decision(tmp_repo, ids[1], "ignore")
        assert ok
        clusters = store.overlap_report(tmp_repo)
        # With the middle rule ignored, only the a-c pairwise link remains.
        assert len(clusters) == 1
        assert {d["content"] for d in clusters[0]} == {TRIO[0], TRIO[2]}
        assert all(d["status"] != "ignored" for d in clusters[0])

    def test_non_rule_subtypes_excluded(self, tmp_repo):
        # Same overlapping texts, but as architecture decisions - out of scope.
        for rule in TRIO:
            _add(tmp_repo, rule, subtype="architecture")
        assert store.overlap_report(tmp_repo) == []

    def test_empty_store(self, tmp_repo):
        assert store.overlap_report(tmp_repo) == []

    def test_fail_soft_on_error(self, tmp_repo, monkeypatch):
        def _boom(_repo):
            raise RuntimeError("disk on fire")
        monkeypatch.setattr(store, "_load", _boom)
        assert store.overlap_report(tmp_repo) == []

    def test_pure_read_no_writes(self, tmp_repo):
        for rule in TRIO:
            _add(tmp_repo, rule)
        path = store._store_path(tmp_repo)
        before = path.read_bytes()
        store.overlap_report(tmp_repo)
        assert path.read_bytes() == before

    def test_retiring_approved_duplicates_makes_cluster_disappear_on_rerun(self, tmp_repo):
        # Finding 129: ignore now works on APPROVED entries too, so the executable
        # workflow (keep the best, ignore the rest) actually clears the cluster.
        ids = [_add(tmp_repo, rule) for rule in TRIO]
        for eid in ids:
            ok, _ = store.approve_decision(tmp_repo, eid, "approve")
            assert ok
        assert len(store.overlap_report(tmp_repo)) == 1

        for eid in ids[1:]:  # keep the first, retire the other two
            ok, msg = store.approve_decision(tmp_repo, eid, "ignore")
            assert ok, msg
        assert store.overlap_report(tmp_repo) == [], "cluster gone once duplicates are retired"


class TestReviewOverlapSection:
    @pytest.fixture
    def cli_repo(self, tmp_repo, monkeypatch):
        monkeypatch.setattr(store, "_git_root", lambda _cwd: tmp_repo)
        return tmp_repo

    def test_clusters_shown_after_pending_queue(self, cli_repo, monkeypatch, capsys):
        # AI-captured constraints land as pending_approval, so they fill the review
        # queue AND (being active) feed the overlap report. Skip every queue item.
        for rule in TRIO:
            _add(cli_repo, rule)
        monkeypatch.setattr(builtins, "input", lambda *_: "S")
        review()
        out = capsys.readouterr().out
        assert "3 decision(s) pending approval" in out
        assert "Possibly overlapping rules (1 cluster(s)):" in out
        assert out.index("Review complete") < out.index("Possibly overlapping rules")
        assert "Cluster 1 (3 rules):" in out
        for rule in TRIO:
            assert rule in out
        assert "never merges or deletes" in out
        assert 'action="ignore"' in out
        assert "ignore now works on approved rules too" in out

    def test_clusters_shown_immediately_when_nothing_pending(self, cli_repo, capsys):
        for eid in [_add(cli_repo, rule) for rule in TRIO]:
            ok, _ = store.approve_decision(cli_repo, eid, "approve")
            assert ok
        review()  # no pending items -> no input() prompts
        out = capsys.readouterr().out
        assert "No decisions pending approval." in out
        assert "Possibly overlapping rules (1 cluster(s)):" in out
        assert "approved" in out  # cluster rows carry the entries' status

    def test_silent_when_no_clusters(self, cli_repo, capsys):
        eid = _add(cli_repo, "Never store plaintext passwords in application logs")
        ok, _ = store.approve_decision(cli_repo, eid, "approve")
        assert ok
        review()
        out = capsys.readouterr().out
        assert "No decisions pending approval." in out
        assert "Possibly overlapping" not in out

    def test_silent_on_empty_store(self, cli_repo, capsys):
        review()
        out = capsys.readouterr().out
        assert "No decisions pending approval." in out
        assert "Possibly overlapping" not in out

    def test_outside_git_repo_exits(self, tmp_repo, monkeypatch, capsys):
        monkeypatch.setattr(store, "_git_root", lambda _cwd: "")
        with pytest.raises(SystemExit):
            review()
        assert "Not inside a git repository." in capsys.readouterr().err
