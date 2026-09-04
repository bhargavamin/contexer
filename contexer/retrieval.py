"""Pure lexical retrieval primitives: tokenization, topic tagging, BM25 scoring, artifacts.

This is the scoring half of Retrieval V1. It has no notion of a repo, a store file, or an
entry's lifecycle - it turns text into tokens/topics/artifacts and ranks an already-built
index against query terms.

Deliberately NOT here, and why:

* the index sidecar I/O (``store._index_path`` / ``_read_retrieval_index`` /
  ``_write_retrieval_index`` / ``ensure_retrieval_index``) is file I/O, and the test suite
  monkeypatches those names *through the store module*, which only works while the calls
  resolve in store's namespace;
* ``store._build_retrieval_index`` assembles docs from a loaded store: it reads entry status
  and current content, and calls into ``guard_engine``/``conflicts`` (function-level imports,
  to dodge the load-order cycle). That is store-shaped work, so it stays with the I/O.

The dependency stays one-way: this module never imports store. Callers that need a private
name here reach it qualified (``retrieval._TOPIC_ALIASES``) rather than through an alias
copied onto ``store``; a leaf does not re-export another leaf's internals.
"""

import re

_QUERY_STOP_WORDS = frozenset({
    "why", "was", "the", "did", "we", "for", "what", "how", "is", "are",
    "can", "does", "this", "that", "it", "to", "of", "in", "a", "an",
    "and", "or", "but", "not", "with", "at", "by", "from", "reason",
    "rationale", "decision", "decided", "chose", "choice", "about", "have",
    "has", "been", "would", "could", "should", "will", "tell", "explain",
    "know", "me", "you", "do", "our", "my", "your", "them", "they",
    "implement", "implemented", "implementation", "use", "using", "used",
    "build", "built", "create", "created", "add", "added", "make", "made",
    "just", "here", "there", "when", "then", "than", "also", "get",
    "into", "which", "who", "where", "its",
})

# ── Retrieval V1: topic router (lexical BM25 index + working set + injection ladder) ──
#
# Topic → alias words. A decision (or prompt) is tagged with a topic when its lowercase
# tokens hit >=1 alias. Derived only — never stored on the entry (the index sidecar owns
# topics). Each topic's own bare name IS a member of its alias set (a question naming the
# topic word directly — "what is the auth feature doing?" — must still tag as that topic),
# but pruned words like bare "session" stay deliberately excluded — see below.
_TOPIC_ALIASES: dict[str, frozenset] = {
    "db": frozenset({"db", "postgres", "postgresql", "mysql", "sqlite", "sql", "migration",
                     "migrations", "schema", "query", "orm", "database", "redis", "mongo"}),
    "api": frozenset({"api", "endpoint", "endpoints", "rest", "route", "routes", "request",
                      "response", "http", "graphql"}),
    # Bare "session"/"sessions" deliberately absent: in agent-tooling repos those
    # words overwhelmingly mean agent sessions, not auth sessions — they mis-tagged
    # documentation questions as auth (observed live 2026-07-15). Genuine auth-session
    # phrasing is caught by _AUTH_SESSION_RE below instead.
    "auth": frozenset({"auth", "jwt", "oauth", "login", "token", "tokens"}),
    "frontend": frozenset({"frontend", "react", "component", "components", "css", "ui", "dom"}),
    "deploy": frozenset({"deploy", "docker", "kubernetes", "k8s", "ci", "terraform", "helm",
                         "release"}),
    "testing": frozenset({"testing", "pytest", "test", "tests", "fixture", "fixtures", "mock",
                          "coverage"}),
    "config": frozenset({"config", "toml", "yaml", "env", "settings"}),
    "perf": frozenset({"perf", "cache", "latency", "optimize"}),
    "security": frozenset({"security", "secret", "vulnerability", "sanitize", "injection"}),
}

# BM25 tuning (Robertson/Sparck-Jones defaults — corpus is <=500 short jargon sentences).
_BM25_K1 = 1.5
_BM25_B = 0.75
_TITLE_BM25_WEIGHT = 2.0

# Artifact extraction: signal-rich tokens pulled from a paste even when the prose is empty.
_ARTIFACT_PATH_RE = re.compile(r"[\w./-]+\.(?:py|ts|js|go|rs|sh|md|toml|yaml|json)\b")
_ARTIFACT_DOTTED_RE = re.compile(r"\b[a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*)+\b")
_ARTIFACT_EXC_RE = re.compile(r"\b[A-Z]\w*(?:Error|Exception)\b")
# Two+ path segments required: a lone slash in prose ("light/dark", "read/write",
# "either/or") is not a route, but "/api/users/{id}" is.
_ARTIFACT_ROUTE_RE = re.compile(r"/[a-z][\w{}-]*(?:/[\w{}-]+)+")

# Compound auth-session phrasing ("invalidate all user sessions") carries no
# surviving auth alias token; this phrase check restores the tag without letting
# bare agent-session vocabulary ("SessionStart runs each session") mean auth.
_AUTH_SESSION_RE = re.compile(r"\b(?:user|login|auth|authenticated) sessions?\b")


def index_tokens(text: str) -> list[str]:
    """Lowercase, punctuation-stripped, alnum tokens of length >=3, minus stop words.
    The single tokenization used by both the index and the BM25 query side (distinct from
    the novelty filter's set-based `store._tokenize`)."""
    toks = re.findall(r"[a-z0-9]+", (text or "").lower())
    return [t for t in toks if len(t) >= 3 and t not in _QUERY_STOP_WORDS]


def derive_topics(content: str) -> list[str]:
    """Sorted topics with >=1 alias hit in `content`. Derived, never persisted."""
    low = (content or "").lower()
    toks = set(re.findall(r"[a-z0-9]+", low))
    topics = {t for t, aliases in _TOPIC_ALIASES.items() if toks & aliases}
    if "auth" not in topics and _AUTH_SESSION_RE.search(low):
        topics.add("auth")
    return sorted(topics)


def bm25_rank(keywords: list[str], index: dict, *, tf_field: str = "tf",
              len_field: str = "len", df_field: str = "df",
              avgdl_field: str = "avgdl") -> list[tuple[str, float, int, int]]:
    """BM25-score every indexed doc against `keywords` (which may repeat — repeats raise
    that term's query weight). Returns (decision_id, score, distinct_term_hits,
    discriminative_hits) sorted by score desc. Terms absent from the corpus contribute
    nothing. A hit is *discriminative* when the matched term is rare in this corpus
    (df <= max(2, n_docs // 20)) — the router's junk guard for question-only prompts."""
    import math
    docs = index.get("docs", {})
    df = index.get(df_field, {})
    n_docs = index.get("n_docs", 0) or 0
    avgdl = index.get(avgdl_field, 0.0) or 0.0
    if not docs or not keywords:
        return []
    # Query-term weights: a repeated keyword (e.g. a double-weighted artifact) counts twice.
    qweight: dict[str, int] = {}
    for kw in keywords:
        qweight[kw] = qweight.get(kw, 0) + 1
    # Resolve each query term to the corpus token(s) it scores against. An exact df hit maps
    # to itself; a term absent from df expands to every indexed token having it as a prefix
    # (restores legacy \b-prefix matching — 'postgres' must match a doc holding only
    # 'postgresql'). Aggregated df is capped at n_docs so idf stays non-negative.
    resolved: dict[str, tuple[list[str], int]] = {}
    for term in qweight:
        if term in df:
            resolved[term] = ([term], df[term])
            continue
        pref = [t for t in df if t.startswith(term)]
        if pref:
            resolved[term] = (pref, min(sum(df[t] for t in pref), n_docs))
    disc_cap = max(2, n_docs // 20)
    ranked: list[tuple[str, float, int, int]] = []
    for did, doc in docs.items():
        tf = doc.get(tf_field, {})
        dl = doc.get(len_field, 0) or 0
        score = 0.0
        hits = 0
        dhits = 0
        for term, w in qweight.items():
            r = resolved.get(term)
            if not r:
                continue
            toks_for, n_t = r
            f = sum(tf.get(t, 0) for t in toks_for)
            if not f:
                continue
            hits += 1
            if n_t <= disc_cap:
                dhits += 1
            idf = math.log(1 + (n_docs - n_t + 0.5) / (n_t + 0.5))
            denom = f + _BM25_K1 * (1 - _BM25_B + _BM25_B * (dl / avgdl if avgdl else 1))
            score += w * idf * (f * (_BM25_K1 + 1) / denom)
        if hits:
            ranked.append((did, score, hits, dhits))
    ranked.sort(key=lambda r: r[1], reverse=True)
    return ranked


def prompt_rank(keywords: list[str], index: dict) -> list[tuple[str, float, int, int, int, int]]:
    """Rank prompt-retrieval candidates across content and their own titles.

    Content remains the broad recall field. A concise authored title contributes a weighted
    BM25 score only to the decision that owns it, and a title-only match can therefore enter
    the candidate set and outrank an incidental body mention. The returned hit counts use the
    conservative maximum across the fields; this never manufactures two distinct lexical hits.
    The fifth and sixth return fields are the content and title distinct-term hit counts, so
    the prompt router can distinguish a true title-only subject from a concise title that
    simply mirrors an already-strong body.
    """
    content = {row[0]: row for row in bm25_rank(keywords, index)}
    titles = {row[0]: row for row in bm25_rank(
        keywords, index, tf_field="title_tf", len_field="title_len",
        df_field="title_df", avgdl_field="title_avgdl",
    )}
    ranked = []
    # Walk the persisted document order rather than a set union. Python's stable sort then
    # gives equal-score candidates deterministic ordering across hook processes.
    candidate_ids = (
        did for did in index.get("docs", {}) if did in content or did in titles
    )
    for did in candidate_ids:
        content_row = content.get(did, (did, 0.0, 0, 0))
        title_row = titles.get(did, (did, 0.0, 0, 0))
        ranked.append((
            did,
            content_row[1] + _TITLE_BM25_WEIGHT * title_row[1],
            max(content_row[2], title_row[2]),
            max(content_row[3], title_row[3]),
            content_row[2],
            title_row[2],
        ))
    ranked.sort(key=lambda row: row[1], reverse=True)
    return ranked


def raw_path_artifacts(text: str) -> list[str]:
    """Path- and module-shaped spans of `text`, matched but NOT segmented.

    The one definition of "what a file path or dotted module looks like", shared by the two
    callers that need it at different granularities: :func:`extract_artifacts` segments these
    spans into BM25 tokens, while ``guard_engine._guard_content_artifacts`` needs the span
    intact so it can compare it against a real staged path. Both used to reach the two
    regexes directly, the guard doing so through an alias copied onto ``store`` - a three-hop
    chain to read a compiled pattern. Exposing the match instead of the patterns keeps the
    interface one function wide and puts this definition in one place."""
    if not text:
        return []
    return _ARTIFACT_PATH_RE.findall(text) + _ARTIFACT_DOTTED_RE.findall(text)


def extract_artifacts(prompt: str) -> list[str]:
    """Signal tokens pulled from a paste: file paths (segmented), dotted module paths,
    CamelCase *Error/*Exception names, and route-shaped strings. Lowercased, len>=3."""
    if not prompt:
        return []
    raw: list[str] = raw_path_artifacts(prompt)
    raw += _ARTIFACT_EXC_RE.findall(prompt)
    raw += _ARTIFACT_ROUTE_RE.findall(prompt)
    out: list[str] = []
    for m in raw:
        for seg in re.split(r"[^a-zA-Z0-9]+", m.lower()):
            if len(seg) >= 3:
                out.append(seg)
    return out
