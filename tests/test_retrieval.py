"""Unit tests for the pure lexical retrieval primitives."""

from contexer import retrieval, store


def _index(docs: dict[str, list[str]]) -> dict:
    """Build a minimal BM25 index payload from {doc_id: [tokens]} - the same shape
    store._build_retrieval_index emits, minus the fields ranking never reads."""
    built: dict[str, dict] = {}
    df: dict[str, int] = {}
    total = 0
    for did, toks in docs.items():
        tf: dict[str, int] = {}
        for t in toks:
            tf[t] = tf.get(t, 0) + 1
        for t in tf:
            df[t] = df.get(t, 0) + 1
        total += len(toks)
        built[did] = {"tf": tf, "len": len(toks)}
    n = len(built)
    return {"v": 2, "n_docs": n, "avgdl": (total / n) if n else 0.0, "df": df, "docs": built}


class TestStoreCompatibilityAliases:
    def test_functions_and_constants_are_the_same_objects(self):
        assert store._index_tokens is retrieval.index_tokens
        assert store._derive_topics is retrieval.derive_topics
        assert store._bm25_rank is retrieval.bm25_rank
        assert store._extract_artifacts is retrieval.extract_artifacts
        assert store._QUERY_STOP_WORDS is retrieval._QUERY_STOP_WORDS
        assert store._TOPIC_ALIASES is retrieval._TOPIC_ALIASES
        assert store._ARTIFACT_PATH_RE is retrieval._ARTIFACT_PATH_RE
        assert store._ARTIFACT_DOTTED_RE is retrieval._ARTIFACT_DOTTED_RE
        assert store._ARTIFACT_ROUTE_RE is retrieval._ARTIFACT_ROUTE_RE

    def test_the_index_sidecar_half_stayed_in_store(self):
        # The suite monkeypatches these THROUGH store; moving them would silently
        # break every such patch, so pin where they live.
        import inspect
        for name in ("_index_path", "_read_retrieval_index", "_write_retrieval_index",
                     "ensure_retrieval_index", "_build_retrieval_index"):
            mod = inspect.getmodule(getattr(store, name))
            assert mod.__name__ == "contexer.store", name


class TestIndexTokens:
    def test_lowercases_splits_and_drops_short_tokens(self):
        assert retrieval.index_tokens("Postgres  MIGRATIONS, o k") == ["postgres", "migrations"]

    def test_drops_stop_words(self):
        assert retrieval.index_tokens("why was the decision about postgres") == ["postgres"]

    def test_empty_and_none_safe(self):
        assert retrieval.index_tokens("") == []
        assert retrieval.index_tokens(None) == []

    def test_keeps_duplicates_because_bm25_weights_them(self):
        assert retrieval.index_tokens("orders orders") == ["orders", "orders"]


class TestDeriveTopics:
    def test_single_alias_hit(self):
        assert retrieval.derive_topics("we migrated the postgres schema for orders") == ["db"]

    def test_multi_topic_sorted(self):
        topics = retrieval.derive_topics("the react component calls a rest endpoint")
        assert topics == ["api", "frontend"]  # sorted, both facets present

    def test_no_alias_returns_empty(self):
        assert retrieval.derive_topics("a plain sentence about widgets and gadgets") == []

    def test_case_insensitive(self):
        assert retrieval.derive_topics("Using JWT for LOGIN flows") == ["auth"]

    def test_session_words_do_not_mean_auth(self):
        # "session" means agent sessions in this domain — it mis-tagged docs
        # questions as auth when it lived in the auth alias set.
        assert retrieval.derive_topics("SessionStart hooks run each session") == []

    def test_compound_auth_session_phrases_still_mean_auth(self):
        # Greptile #123: genuine auth-session text carries no surviving alias
        # token; the compound-phrase check restores the tag.
        assert retrieval.derive_topics("invalidate all user sessions on password change") == ["auth"]
        assert retrieval.derive_topics("login sessions expire after thirty minutes") == ["auth"]


class TestBm25Rank:
    def test_empty_index_or_empty_keywords(self):
        assert retrieval.bm25_rank(["orders"], _index({})) == []
        assert retrieval.bm25_rank([], _index({"d1": ["orders"]})) == []

    def test_only_matching_docs_are_returned_ranked_desc(self):
        idx = _index({
            "d1": ["orders", "orders", "pagination"],
            "d2": ["orders"],
            "d3": ["unrelated"],
        })
        ranked = retrieval.bm25_rank(["orders"], idx)
        assert "d3" not in [r[0] for r in ranked], "a doc with no matching term contributes nothing"
        assert [r[1] for r in ranked] == sorted((r[1] for r in ranked), reverse=True)

    def test_higher_term_frequency_outranks_at_equal_length(self):
        idx = _index({
            "d1": ["orders", "orders", "filler"],
            "d2": ["orders", "filler", "filler"],
        })
        ranked = retrieval.bm25_rank(["orders"], idx)
        assert ranked[0][0] == "d1"

    def test_length_normalization_can_beat_raw_frequency(self):
        # BM25 divides by document length: a terse doc matching once outranks a longer
        # doc matching twice. Worth pinning - it is the behaviour that makes a one-line
        # decision competitive against a paragraph-long one.
        idx = _index({"long": ["orders", "orders", "pagination"], "short": ["orders"]})
        assert retrieval.bm25_rank(["orders"], idx)[0][0] == "short"

    def test_absent_term_expands_by_prefix(self):
        # 'postgres' is absent as an exact token, but 'postgresql' starts with it.
        idx = _index({"d1": ["postgresql", "schema"]})
        ranked = retrieval.bm25_rank(["postgres"], idx)
        assert ranked and ranked[0][0] == "d1"

    def test_unknown_term_scores_nothing(self):
        assert retrieval.bm25_rank(["nonexistent"], _index({"d1": ["orders"]})) == []

    def test_repeated_keyword_raises_that_terms_weight(self):
        idx = _index({"d1": ["orders", "pagination"]})
        once = retrieval.bm25_rank(["orders"], idx)[0][1]
        twice = retrieval.bm25_rank(["orders", "orders"], idx)[0][1]
        assert twice > once
        assert twice == 2 * once, "weight is a linear multiplier on the term's contribution"

    def test_hit_counts_are_distinct_terms_not_occurrences(self):
        idx = _index({"d1": ["orders", "orders", "orders", "pagination"]})
        (_did, _score, hits, _dhits) = retrieval.bm25_rank(["orders", "pagination"], idx)[0]
        assert hits == 2, "two distinct query terms matched"

    def test_discriminative_hits_track_rare_terms(self):
        # disc_cap = max(2, n_docs // 20). With 3 docs the cap is 2, so a term in all
        # three is not discriminative while one in a single doc is.
        idx = _index({
            "d1": ["common", "rare"],
            "d2": ["common"],
            "d3": ["common"],
        })
        (_did, _score, _hits, dhits) = retrieval.bm25_rank(["rare"], idx)[0]
        assert dhits == 1
        common = next(r for r in retrieval.bm25_rank(["common"], idx) if r[0] == "d2")
        assert common[3] == 0, "a term in every doc discriminates nothing"


class TestExtractArtifacts:
    def test_prose_slashes_are_not_routes(self):
        for prose in ("light/dark mode", "read/write splitting", "either/or choice"):
            assert retrieval._ARTIFACT_ROUTE_RE.findall(prose) == [], prose
            assert retrieval.extract_artifacts(prose) == [], prose

    def test_real_route_matches(self):
        assert retrieval._ARTIFACT_ROUTE_RE.findall("GET /api/users/{id} returns json") == ["/api/users/{id}"]
        arts = retrieval.extract_artifacts("GET /api/users/{id} returns json")
        assert "users" in arts and "api" in arts

    def test_file_paths_are_segmented(self):
        arts = retrieval.extract_artifacts("see contexer/store.py for details")
        assert "contexer" in arts and "store" in arts

    def test_dotted_modules_and_exception_names(self):
        assert "settings" in retrieval.extract_artifacts("read config.settings first")
        arts = retrieval.extract_artifacts("it raised OperationalError again")
        assert "operationalerror" in arts, "lowercased"

    def test_empty_prompt(self):
        assert retrieval.extract_artifacts("") == []
        assert retrieval.extract_artifacts(None) == []
