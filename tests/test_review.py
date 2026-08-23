"""Unit tests for the pure proposal-slot policy."""

from contexer import review


class TestProposalTrustOrder:
    def test_proposal_slot_trust_order(self):
        def outranks(new: str, existing: str) -> bool:
            return review.outranks_proposal(new, {"source": existing})

        assert outranks("human", "ai") and outranks("human", "scan") and outranks("plan", "ai")
        assert not (outranks("ai", "human") or outranks("scan", "ai")
                    or outranks("plan", "human"))
        assert not (outranks("ai", "ai") or outranks("human", "human")), "equal trust refuses"
        assert not outranks("mystery", "scan"), "an unknown source never displaces"
        assert outranks("scan", "mystery"), "an unknown source is itself displaceable"

    def test_store_compatibility_alias(self):
        assert review.outranks_proposal is review.outranks_proposal
        assert review.claim_proposal_slot is review.claim_proposal_slot
        assert review.refusal_ack is review.refusal_ack
        assert review.build_proposal is review.build_proposal
        assert review.PROPOSAL_TRUST is review.PROPOSAL_TRUST


class TestClaimProposalSlot:
    def test_empty_slot_is_claimed(self):
        entry = {"id": "d1"}
        assert review.claim_proposal_slot(entry, "ai", "NOW") is True
        assert "superseded_proposals" not in entry, "nothing was displaced"

    def test_lower_trust_never_displaces_and_leaves_the_slot_intact(self):
        entry = {"id": "d1", "proposed_revision": {"source": "human", "content": "keep me"}}
        assert review.claim_proposal_slot(entry, "ai", "NOW") is False
        assert entry["proposed_revision"]["content"] == "keep me"
        assert "superseded_proposals" not in entry, "a refusal archives nothing"

    def test_higher_trust_displaces_and_archives(self):
        entry = {
            "id": "d1",
            "proposed_revision": {"source": "ai", "content": "guess"},
            "conflict_memo": "refers to the ai proposal",
        }
        assert review.claim_proposal_slot(entry, "human", "2026-01-01T00:00:00Z") is True
        assert "proposed_revision" not in entry
        assert "conflict_memo" not in entry, "the memo's referent was replaced"
        archived = entry["superseded_proposals"]
        assert [p["content"] for p in archived] == ["guess"], "displaced, not discarded"
        assert archived[0]["superseded_at"] == "2026-01-01T00:00:00Z"

    def test_equal_trust_claims_the_slot(self):
        # Unlike store._route_containment's refusal: the same automated source retrying is
        # correcting its own proposal, so refusing would silently drop that correction.
        entry = {"id": "d1", "proposed_revision": {"source": "ai", "content": "first guess"}}
        assert review.claim_proposal_slot(entry, "ai", "NOW") is True
        assert entry["superseded_proposals"][0]["content"] == "first guess"


class TestRefusalAck:
    def test_names_the_decision_the_sitting_source_and_its_title(self):
        entry = {
            "id": "abcdef1234567890",
            "proposed_revision": {"source": "human", "title": "Use Postgres"},
        }
        ack = review.refusal_ack(entry)
        assert "abcdef12" in ack and "abcdef1234567890" not in ack, "short id only"
        assert "human" in ack and "Use Postgres" in ack
        assert "NOT stored" in ack and "refused, not queued" in ack
        assert "Do NOT retry" in ack, "self-approval proofing is part of the contract"

    def test_survives_a_missing_proposal(self):
        assert "unknown" in review.refusal_ack({"id": "d1"})


class TestBuildProposal:
    _TARGET = {"id": "d1", "subtype": "constraint", "occurrence_count": 2,
               "session_ids": ["s1"], "memory_key": None}

    def test_carries_provenance_and_normalizes_content(self):
        prop = review.build_proposal(
            self._TARGET, "  never   commit secrets ", "", "s2", "NOW")
        assert prop["content"] == "Never commit secrets"
        assert prop["subtype"] == "constraint", "falls back to the target's subtype"
        assert (prop["session_id"], prop["source"], prop["created_at"]) == ("s2", "ai", "NOW")
        assert prop["confidence"] > 0 and prop["confidence_factors"]

    def test_title_is_derived_when_absent_and_normalized_when_given(self):
        derived = review.build_proposal(self._TARGET, "Never commit secrets", "", "s2", "NOW")
        assert derived["title"] == "Never commit secrets"

        given = review.build_proposal(
            self._TARGET, "Never commit secrets", "", "s2", "NOW", title="  Secret  policy ")
        assert given["title"] == "Secret policy"

    def test_source_files_are_stashed_only_when_present(self):
        bare = review.build_proposal(self._TARGET, "content", "", "s2", "NOW")
        assert "source_files" not in bare, "an empty list must not stash a dead anchor"

        anchored = review.build_proposal(
            self._TARGET, "content", "", "s2", "NOW", source_files=["a.py"])
        assert anchored["source_files"] == ["a.py"]

    def test_the_target_is_never_mutated(self):
        target = dict(self._TARGET)
        before = dict(target)
        review.build_proposal(target, "content", "", "s2", "NOW")
        assert target == before, "a proposal waits for approval; it changes nothing"
