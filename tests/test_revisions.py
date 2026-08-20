"""Unit tests for the pure decision-revision lifecycle."""

from contexer import revisions, store


class TestTitleHelpers:
    def test_normalize_flattens_strips_and_handles_empty(self):
        assert revisions.normalize_title("  hello\n  world \t") == "hello world"
        assert revisions.normalize_title("   \n ") == ""

    def test_normalize_truncates_with_ellipsis(self):
        result = revisions.normalize_title("x" * 150)
        assert len(result) == revisions.MAX_TITLE_LEN
        assert result.endswith("…")

    def test_derive_verbatim_when_short_and_empty(self):
        content = "Never commit spec or plan files to git."
        assert revisions.derive_title(content) == content
        assert revisions.derive_title("") == ""

    def test_derive_uses_first_sentence_and_caps_long_sentence(self):
        content = ("Native contexer-teams entry removed. Team sync is the Python path; "
                   "kept a legacy janitor pop; login now refreshes status.")
        assert revisions.derive_title(content) == "Native contexer-teams entry removed."

        long = revisions.derive_title("A " + "very " * 60 + "long first sentence")
        assert len(long) == revisions.MAX_TITLE_LEN
        assert long.endswith("…")

    def test_content_normalization_matches_store_compatibility_alias(self):
        assert revisions.normalize_content("  use   postgres ") == "Use postgres"
        assert revisions.normalize_content("") == ""
        assert store._normalize_content is revisions.normalize_content


class TestComputeConfidence:
    def test_new_ai_entry_has_base_score(self):
        entry = {"created_by": "ai", "occurrence_count": 1, "session_ids": ["s1"]}
        assert revisions.compute_confidence(entry) == (30, [])

    def test_repository_and_human_sources_add_their_bonus(self):
        for source in ("bootstrap", "scan"):
            score, factors = revisions.compute_confidence({"created_by": source})
            assert score == 45
            assert factors == ["Observed in repository"]

        score, factors = revisions.compute_confidence({"created_by": "human"})
        assert score == 50
        assert factors == ["Stated by developer"]

    def test_approval_occurrences_sessions_and_memory_accumulate(self):
        entry = {
            "created_by": "bootstrap",
            "approved_by": "human",
            "occurrence_count": 5,
            "session_ids": ["s1", "s2", "s3", "s4", "s5"],
            "memory_key": "fact.md##one",
        }
        score, factors = revisions.compute_confidence(entry)
        assert score == 100
        assert factors == [
            "Approved by developer", "Observed in repository",
            "Referenced in 5 sessions", "Persisted to memory tool",
        ]

    def test_two_occurrences_and_independent_session_branches(self):
        assert revisions.compute_confidence({
            "occurrence_count": 2, "session_ids": ["s1"],
        }) == (40, ["Mentioned multiple times"])
        assert revisions.compute_confidence({
            "occurrence_count": 1, "session_ids": ["s1", "s2"],
        }) == (35, ["Seen in multiple sessions"])
        assert revisions.compute_confidence({
            "occurrence_count": 2, "session_ids": ["s1", "s2", "s3"],
        }) == (50, ["Mentioned multiple times", "Confirmed across multiple sessions"])


def _revision(revision_id="r1", version=1, content="one", **extra):
    return {
        "revision_id": revision_id,
        "decision_id": "d1",
        "version_number": version,
        "content": content,
        "title": extra.pop("title", ""),
        "confidence_score": extra.pop("confidence_score", 30),
        "evidence": extra.pop("evidence", []),
        "created_at": extra.pop("created_at", "2026-01-01T00:00:00+00:00"),
        "approved_at": extra.pop("approved_at", None),
        "source": extra.pop("source", "ai"),
        **extra,
    }


class TestRevisionLifecycle:
    def test_new_revision_normalizes_by_default_and_can_preserve_legacy_bytes(self):
        evidence = ["observed"]
        revision = revisions.new_revision(
            "d1", 2, "  use   postgres ", "ai", evidence=evidence,
            created_at="created", approved_at="approved", title="Database")
        evidence.append("later mutation")
        assert revision["content"] == "Use postgres"
        assert revision["evidence"] == ["observed"]
        assert revision["created_at"] == "created"
        assert revision["approved_at"] == "approved"
        assert revision["title"] == "Database"
        assert revision["revision_id"]

        legacy = revisions.new_revision("d1", 1, "  original Bytes", "ai", normalize=False)
        assert legacy["content"] == "  original Bytes"
        assert legacy["created_at"]

    def test_current_revision_honors_pointer_then_falls_back(self):
        first = _revision()
        second = _revision("r2", 2, "two")
        entry = {"revisions": [first, second], "current_revision_id": "r1"}
        assert revisions.current_revision(entry) is first
        entry["current_revision_id"] = "missing"
        assert revisions.current_revision(entry) is second
        assert revisions.current_revision({}) is None

    def test_current_content_uses_revision_then_legacy_cache(self):
        assert revisions.current_content({"revisions": [_revision(content="head")]}) == "head"
        assert revisions.current_content({"content": "legacy"}) == "legacy"

    def test_sync_cache_mirrors_head_and_clears_empty_evidence(self):
        entry = {
            "confidence": 7,
            "confidence_factors": ["stale"],
            "revisions": [_revision(content="body", title="", confidence_score=55)],
        }
        revisions.sync_decision_cache(entry)
        assert entry["content"] == "body"
        assert entry["title"] == "body"
        assert entry["revision"] == 1
        assert entry["confidence"] == 55
        assert "confidence_factors" not in entry

        entry["revisions"][0]["evidence"] = ["fresh"]
        revisions.sync_decision_cache(entry)
        assert entry["confidence_factors"] == ["fresh"]
        untouched = {"content": "legacy"}
        revisions.sync_decision_cache(untouched)
        assert untouched == {"content": "legacy"}

    def test_append_advances_head_and_invalidates_nonhuman_approval_before_scoring(self):
        entry = {
            "id": "d1", "created_by": "ai", "approved_by": "human",
            "revisions": [_revision()], "current_revision_id": "r1",
        }
        appended = revisions.append_revision(entry, "two", "ai")
        assert appended["version_number"] == 2
        assert appended["confidence_score"] == 30
        assert "Approved by developer" not in appended["evidence"]
        assert "approved_by" not in entry
        assert entry["current_revision_id"] == appended["revision_id"]
        assert entry["content"] == "Two"
        assert entry["title"] == "two"
        assert entry["updated_at"] == appended["created_at"]

    def test_append_preserves_human_approval_and_handles_first_revision(self):
        entry = {"id": "d1", "created_by": "ai", "approved_by": "human"}
        appended = revisions.append_revision(
            entry, "first", "human", approved_at="approved", title=" Authored title ")
        assert appended["version_number"] == 1
        assert appended["approved_at"] == "approved"
        assert appended["confidence_score"] == 70
        assert appended["title"] == "Authored title"
        assert entry["approved_by"] == "human"

    def test_store_private_names_remain_compatibility_aliases(self):
        assert store._normalize_title is revisions.normalize_title
        assert store._derive_title is revisions.derive_title
        assert store._compute_confidence is revisions.compute_confidence
        assert store._new_revision is revisions.new_revision
        assert store._current_revision is revisions.current_revision
        assert store._current_content is revisions.current_content
        assert store._sync_decision_cache is revisions.sync_decision_cache
        assert store._append_revision is revisions.append_revision
