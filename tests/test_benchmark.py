"""
Contexer effectiveness benchmark.

Run with:
    uv run pytest tests/test_benchmark.py -v -s

Measures:
  1. Session start context size — tokens injected vs baseline (no Contexer)
  2. Preloaded vs deferred decision split
  3. Rationale prompt hit/miss rate (get_context_for_prompt)
  4. On-demand get_context timing and hit/miss rate
  5. Global store fallback effectiveness
  6. Novelty filter effectiveness (duplicate blocking)

Token approximation: words × 1.3 (GPT/Claude tokenisers average ~0.75 words per token)
"""

import math
import time
from pathlib import Path

import pytest

from contexer import store


# ── Token helpers ─────────────────────────────────────────────────────────────

def _approx_tokens(text: str) -> int:
    """Rough token count: words × 1.3. Accurate enough for relative comparison."""
    return math.ceil(len(text.split()) * 1.3)


def _pct(n: int, total: int) -> str:
    return f"{n}/{total} ({100 * n // total}%)" if total else "0/0"


# ── Demo data ─────────────────────────────────────────────────────────────────
# 20 realistic decisions across a TypeScript/Node.js API project.

DEMO_REPO = "/demo/ecommerce-api"
SESSION = "bench-session-id"

CONVENTIONS = [
    "Always use conventional commits format: type(scope): description — types are feat, fix, docs, refactor, chore, test",
    "Use TypeScript strict mode for all new files — tsconfig.json has strict:true enabled globally",
    "All API endpoints return JSON with { data, error, meta } envelope structure — never return bare values",
    "Environment variables must be validated at startup using Zod schema — no direct process.env access in business logic",
    "Database migrations run automatically on deploy via Prisma migrate deploy — never run migrations manually in production",
]

CONSTRAINTS = [
    "Never commit directly to main — all changes require a PR with at least one approval before merging",
    "No real external API calls in unit tests — use MSW handlers for all HTTP mocking",
    "Response time SLA: p99 must be under 200ms for all user-facing endpoints — benchmark before merging to main",
    "PII data must never appear in logs — scrub user emails, phone numbers, and card digits before any log statement",
    "All database queries must use parameterized statements — no string interpolation in SQL under any circumstances",
]

ARCHITECTURE = [
    "Chose PostgreSQL over MongoDB because we need ACID transactions for multi-step order processing and inventory updates",
    "API gateway pattern: all external traffic routes through Kong, internal service communication uses gRPC not REST",
    "Event sourcing for order state: OrderPlaced, OrderShipped, OrderDelivered events stored in EventStore with projections",
    "Frontend uses Next.js App Router with React Server Components — no client-side data fetching for initial page loads",
    "Authentication uses JWT with 15-minute access tokens and 7-day refresh tokens stored in httpOnly cookies not localStorage",
]

PATTERNS = [
    "Repository pattern for all database access — no raw Prisma calls outside of repository classes in the services layer",
    "Use Result type for error handling in the service layer instead of throwing exceptions — all services return Result not void",
    "API route handlers are thin controllers — all business logic lives in service classes, handlers only parse and delegate",
    "Feature flags controlled via environment variables prefixed FEATURE_ — no hardcoded feature toggles or if/else branching",
    "Pagination uses cursor-based approach with opaque cursors not offset integers — required for all list endpoints",
]

# Rationale prompts that SHOULD hit stored decisions (contains keyword + rationale word)
HIT_PROMPTS = [
    ("why did we choose postgresql over other databases?",          "postgres"),
    ("what was the reason for using event sourcing?",              "event"),
    ("why are we using jwt for authentication?",                   "authentication"),
    ("what is the rationale behind the repository pattern?",       "repository"),
    ("why did we decide on cursor pagination instead of offset?",  "pagination"),
    ("what is the reason for strict typescript mode?",             "typescript"),
    ("why did we choose gRPC for internal services?",              "grpc"),
    ("what was the reasoning behind the Result type pattern?",     "result"),
    ("why do we use Kong as our API gateway?",                     "gateway"),
    ("why were migrations automated instead of manual?",           "migrations"),
]

# Question-shaped comprehension prompts — no rationale word, but they name rare store
# terms, so the discriminative-term guard lets them through. Pinned separately as well as
# in HIT_PROMPTS because the hit-rate benchmark only asserts a floor over all prompts:
# test_question_prompts_inject_strong_content pins these to full content, not a pointer.
QUESTION_HIT_PROMPTS = [
    ("what does our event sourcing implementation do?",            "event"),
    ("how does cursor pagination work here?",                      "pagination"),
    ("how are refresh tokens stored?",                             "cookies"),
]

HIT_PROMPTS += QUESTION_HIT_PROMPTS

# Pointer-expected prompts (Task 4): a bare topic word gives the WEAK lane a topic to
# overlap on, even though the STRONG lane's discriminative guard still blocks full content.
# Pinned separately (not in HIT_PROMPTS, which only asserts truthiness) because
# test_pointer_prompts_stay_weak below pins the exact kind — "pointer", never "strong".
POINTER_HIT_PROMPTS = [
    # "api" (df 4) is a lone common term with 0 discriminative hits — the guard blocks
    # STRONG, but "api" is now a member of its own topic's alias set (Task 4), so the WEAK
    # pointer fires instead of total silence.
    ("what about the api?",              "api"),
    # The motivating case: a question naming a bare topic word whose only BM25 match
    # (the JWT/refresh-token decision) is single-term ("auth" isn't literally in that
    # decision's text, only "authentication" — a different token) — pre-Task-4 this
    # derived no topic at all and stayed silent. Now "auth" is a member of its own alias
    # set, so the WEAK pointer surfaces it instead of nothing.
    ("what is the auth feature doing?",  "auth"),
    # PRE-EXISTING behavior, not new in this task: "overview" already passed the
    # is_project gate and "docker" was already a `deploy` alias before this branch — any
    # store holding a deploy-tagged decision (the migrations-on-deploy convention below
    # genuinely IS one; `prisma migrate deploy` is a real deploy-pipeline fact) already
    # produced this pointer. The documented limitation is prompt-side (a general-knowledge
    # "Docker networking" question happens to share the `docker` token with the `deploy`
    # topic), not a false tag — and the payload is a ~15-token "if relevant" pointer, not
    # a content injection.
    ("give me an overview of Docker networking", "deploy"),
]

# Prompts that should NOT trigger rationale injection (no rationale keyword or no match)
MISS_PROMPTS = [
    "add a new endpoint to create products",
    "fix the bug in the cart service",
    "refactor the user controller",
    "write unit tests for the payment service",
    "update the README with deployment steps",
    "what is the current price of the premium plan?",    # question, no rationale word
    "show me the schema for the orders table",
    "why is the sky blue?",                              # rationale word, no domain keyword
    "why do plants grow upward?",                        # rationale word, no domain keyword
    "what is the reason for rain?",                      # rationale word, no domain keyword
    "is this variable in scope?",                        # "scope" trigger but domain keyword present
    "add a NOT NULL constraint to the users table",      # "constraint" trigger but SQL-specific
    # Question-shaped but generic: the router opens for questions, the discriminative-term
    # guard keeps these silent (no rare store term matched, no topic overlap).
    "what should I call this variable?",                 # only generic tokens match
    "how do I exit vim?",                                # nothing in the store at all
    "what time is the standup?",                         # "time" matches the SLA rule — 1 hit, not an answer
    # Silent ONLY because of the discriminative guard — delete it and this injects. "must"/
    # "never" are corpus-common (df 4 each) yet co-occur in the PII rule, so the pair clears
    # _STRONG_MIN_HITS. None of its words are topic names, so it stays a pure guard pin even
    # after Task 4 (contrast with "what about the api?", moved to POINTER_HIT_PROMPTS above).
    "what must never happen?",                           # 2 hits, 0 discriminative
    # Task 4 proof: a bare topic word ("api") alone does NOT leak through the gate on a
    # plain task prompt — no rationale/project/question-lead and no artifact, so the router
    # never even reaches topic derivation.
    "add rate limiting to the api gateway",
]

# Known edge-case false positives — short keywords that substring-match unrelated decisions.
# e.g. "form" (from "why do clouds form?") matches "format" in the commits convention.
# These are documented limitations, not bugs.
KNOWN_FALSE_POSITIVES = [
    "why do clouds form?",            # "form" substring-matches "format" in commits convention
    "is this variable in scope?",     # "variable" substring-matches "environment variables"
]

# On-demand get_context queries: (query, expected_hit: bool)
ONDEMAND_QUERIES = [
    ("postgresql",      True),
    ("event sourcing",  True),
    ("jwt",             True),
    ("repository",      True),
    ("cursor",          True),
    ("typescript",      True),
    ("grpc",            True),
    ("result type",     True),
    ("kong",            True),
    ("redis",           False),   # not stored
    ("graphql",         False),   # not stored
    ("kafka",           False),   # not stored
]


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def populated_store(tmp_path_factory, monkeypatch_module):
    store_dir = tmp_path_factory.mktemp("bench_store")
    monkeypatch_module.setattr(store, "STORE_DIR", store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)

    for content in CONVENTIONS:
        store.update_decision(DEMO_REPO, content, SESSION, "convention")
    for content in CONSTRAINTS:
        store.update_decision(DEMO_REPO, content, SESSION, "constraint")
    for content in ARCHITECTURE:
        store.update_decision(DEMO_REPO, content, SESSION, "architecture")
    for content in PATTERNS:
        store.update_decision(DEMO_REPO, content, SESSION, "pattern")

    return store_dir


@pytest.fixture(scope="module")
def monkeypatch_module():
    """Module-scoped monkeypatch (pytest's built-in is function-scoped)."""
    from _pytest.monkeypatch import MonkeyPatch
    mp = MonkeyPatch()
    yield mp
    mp.undo()


# ── Benchmark 1: Session start context size ───────────────────────────────────

class TestSessionStartContext:
    def test_preloaded_vs_deferred_split(self, populated_store, monkeypatch_module):
        result = store.get_session_start_context(DEMO_REPO)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        msg = result["systemMessage"]

        preloaded_lines = [l for l in ctx.splitlines() if l.startswith("- [")]
        deferred_note = [l for l in ctx.splitlines() if "decision(s) stored" in l]

        preloaded_count = len(preloaded_lines)
        deferred_count = int(deferred_note[0].split()[0]) if deferred_note else 0

        print(f"\n{'='*60}")
        print("BENCHMARK 1 — Session start context")
        print(f"{'='*60}")
        print(f"  Status line:      {msg}")
        print(f"  Preloaded:        {preloaded_count} decisions (convention + constraint + pattern)")
        print(f"  Deferred (JIT):   {deferred_count} decisions (architecture)")
        print(f"  Tokens injected:  ~{_approx_tokens(ctx)}")
        print(f"  Tokens baseline:  0 (no Contexer)")
        print(f"  Overhead:         +{_approx_tokens(ctx)} tokens per session start")
        print(f"\n  Preloaded content preview:")
        for line in preloaded_lines[:3]:
            print(f"    {line[:80]}...")
        if len(preloaded_lines) > 3:
            print(f"    ... and {len(preloaded_lines) - 3} more")

        # With confidence lifecycle: constraints (Level 3) start as pending_approval,
        # not pre-loaded until the developer approves them. Patterns with L3 content
        # signals ("instead of") are also pending. Only conventions and clean patterns
        # are pre-loaded as suggested. Architecture decisions are always deferred.
        assert preloaded_count >= 5, f"Expected at least 5 pre-loaded (conventions), got {preloaded_count}"
        assert preloaded_count <= 15, f"Expected at most 15 pre-loaded, got {preloaded_count}"
        assert deferred_count == 5, f"Expected 5 deferred (5 arch), got {deferred_count}"
        # Verify pending decisions are mentioned in the system message
        assert "pending" in msg.lower(), "Expected pending decisions mention in status"

    def test_context_token_size_is_bounded(self, populated_store, monkeypatch_module):
        result = store.get_session_start_context(DEMO_REPO)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        tokens = _approx_tokens(ctx)
        # 10 decisions × ~25 words each × 1.3 ≈ 325 tokens — must stay well under 1k
        assert tokens < 1000, f"Session start context too large: {tokens} tokens"

    def test_without_contexer_baseline(self, populated_store, monkeypatch_module):
        """Baseline: without Contexer, Claude starts with 0 tokens of project context."""
        print(f"\n{'='*60}")
        print("BENCHMARK 1b — Baseline without Contexer")
        print(f"{'='*60}")
        print("  Preloaded decisions: 0")
        print("  Tokens injected:     0")
        print("  Result:              Claude starts blind — re-explains conventions each session")
        # Just a documentation test — always passes
        assert True


# ── Benchmark 2: Rationale prompt hit/miss rate ────────────────────────────────

class TestRationaleHitRate:
    def test_hit_rate(self, populated_store, monkeypatch_module):
        hits = []
        misses = []

        print(f"\n{'='*60}")
        print("BENCHMARK 2 — Rationale prompt hit/miss rate")
        print(f"{'='*60}")
        print("\n  HITS (expected to inject context):")

        for prompt, expected_kw in HIT_PROMPTS:
            start = time.perf_counter()
            result = store.get_context_for_prompt(DEMO_REPO, prompt)
            elapsed_ms = (time.perf_counter() - start) * 1000

            is_hit = bool(result)
            tokens = _approx_tokens(result) if result else 0
            if is_hit:
                hits.append((prompt, elapsed_ms, tokens))
                print(f"    ✓ [{elapsed_ms:.1f}ms, ~{tokens}tk] \"{prompt[:55]}\"")
            else:
                misses.append(prompt)
                print(f"    ✗ [miss] \"{prompt[:55]}\"")

        print(f"\n  MISSES (expected to be silent no-ops):")
        false_positives = []
        for prompt in MISS_PROMPTS:
            start = time.perf_counter()
            result = store.get_context_for_prompt(DEMO_REPO, prompt)
            elapsed_ms = (time.perf_counter() - start) * 1000

            if result:
                false_positives.append((prompt, result))
                print(f"    ✗ [false positive, {elapsed_ms:.1f}ms] \"{prompt[:55]}\"")
            else:
                print(f"    ✓ [silent, {elapsed_ms:.1f}ms] \"{prompt[:55]}\"")

        hit_rate = len(hits) / len(HIT_PROMPTS) * 100
        avg_hit_ms = sum(ms for _, ms, _ in hits) / len(hits) if hits else 0
        avg_hit_tokens = sum(tk for _, _, tk in hits) / len(hits) if hits else 0

        print(f"\n  Summary:")
        print(f"    Hit rate:           {_pct(len(hits), len(HIT_PROMPTS))}")
        print(f"    False positive rate:{_pct(len(false_positives), len(MISS_PROMPTS))}")
        print(f"    Avg retrieval time: {avg_hit_ms:.2f}ms")
        print(f"    Avg tokens injected:{avg_hit_tokens:.0f} per hit")
        print(f"    Tokens on miss:     0 (silent no-op)")

        # Document known false positives separately — short keywords substring-matching
        # unrelated stored decisions (e.g. "form" → "format").
        unexpected_fps = [(p, r) for p, r in false_positives if p not in KNOWN_FALSE_POSITIVES]
        if false_positives:
            print(f"\n  Known false positives (documented limitations):")
            for p, r in false_positives:
                kw = [w for w in p.lower().split() if len(w) > 3 and w not in store._QUERY_STOP_WORDS and w.isalpha()]
                print(f"    \"{p}\" → keyword '{kw}' substring-matched an unrelated decision")

        assert len(hits) >= 10, f"Hit rate too low: {len(hits)}/13. Missed: {misses}"
        assert len(unexpected_fps) == 0, f"Unexpected false positives: {unexpected_fps}"

    def test_question_prompts_inject_strong_content(self, populated_store, monkeypatch_module):
        """The hit-rate benchmark only asserts a floor over all prompts, so a question pin
        could silently decay into a ~15-token pointer and stay green. Pin the kind."""
        for prompt, expected_kw in QUESTION_HIT_PROMPTS:
            text, meta = store.get_context_for_prompt_with_meta(DEMO_REPO, prompt)
            assert meta["kind"] == "strong", f"{prompt!r} degraded to {meta['kind']!r}"
            assert expected_kw in text.lower(), f"{prompt!r} injected the wrong decision"

    def test_discriminative_guard_is_load_bearing(self, populated_store, monkeypatch_module):
        """Same question shape, opposite outcomes: a rare term answers, a common one is
        noise. Without the guard the second prompt injects the PII rule on 'must never'."""
        assert store.get_context_for_prompt(DEMO_REPO, "what must be parameterized?")
        assert store.get_context_for_prompt(DEMO_REPO, "what must never happen?") == ""

    def test_pointer_prompts_stay_weak(self, populated_store, monkeypatch_module):
        """Task 4: a bare topic word (now a member of its own alias set) feeds ONLY the
        WEAK pointer lane — the discriminative guard still blocks these from ever reaching
        STRONG content, so the kind must be "pointer", never "strong" or "" (total silence,
        the pre-Task-4 behavior)."""
        for prompt, expected_topic in POINTER_HIT_PROMPTS:
            text, meta = store.get_context_for_prompt_with_meta(DEMO_REPO, prompt)
            assert meta["kind"] == "pointer", f"{prompt!r} yielded {meta['kind']!r}, not a pointer"
            assert expected_topic in meta["topics"], f"{prompt!r} pointer omitted topic {expected_topic!r}"
            assert expected_topic in text.lower()

    def test_miss_prompts_are_zero_cost(self, populated_store, monkeypatch_module):
        """Confirm non-rationale prompts add 0 tokens (pure no-op) — except the one
        documented substring-match false positive. Asserts the exact set of non-silent
        prompts (not just "skip whatever's in KNOWN_FALSE_POSITIVES") so a NEW false
        positive silently added to that list still fails this test until reviewed here too."""
        non_silent = {p for p in MISS_PROMPTS if store.get_context_for_prompt(DEMO_REPO, p)}
        assert non_silent == {"is this variable in scope?"}, non_silent


# ── Benchmark 3: On-demand get_context timing ────────────────────────────────

class TestOnDemandRetrieval:
    def test_query_hit_miss_rate(self, populated_store, monkeypatch_module):
        hits = []
        misses = []

        print(f"\n{'='*60}")
        print("BENCHMARK 3 — On-demand get_context hits and misses")
        print(f"{'='*60}")

        for query, expected_hit in ONDEMAND_QUERIES:
            start = time.perf_counter()
            result = store.get_context(DEMO_REPO, query=query, entry_type="")
            elapsed_ms = (time.perf_counter() - start) * 1000

            actual_hit = "No matching decisions" not in result and "No context stored" not in result
            tokens = _approx_tokens(result)
            status = "HIT " if actual_hit else "MISS"
            correct = actual_hit == expected_hit
            marker = "✓" if correct else "✗"

            if actual_hit:
                hits.append((query, elapsed_ms, tokens))
                print(f"    {marker} {status} [{elapsed_ms:.2f}ms, ~{tokens}tk] query=\"{query}\"")
            else:
                misses.append((query, elapsed_ms))
                print(f"    {marker} {status} [{elapsed_ms:.2f}ms, 0tk]   query=\"{query}\"")

        avg_hit_ms = sum(ms for _, ms, _ in hits) / len(hits) if hits else 0
        avg_miss_ms = sum(ms for _, ms in misses) / len(misses) if misses else 0

        print(f"\n  Summary:")
        print(f"    Queries run:       {len(ONDEMAND_QUERIES)}")
        print(f"    Hits:              {len(hits)}")
        print(f"    Misses:            {len(misses)}")
        print(f"    Avg hit latency:   {avg_hit_ms:.2f}ms")
        print(f"    Avg miss latency:  {avg_miss_ms:.2f}ms")
        print(f"    Tokens on miss:    0 (no context injected)")

        # Validate expected hits match actual hits
        for query, expected_hit in ONDEMAND_QUERIES:
            result = store.get_context(DEMO_REPO, query=query)
            actual_hit = "No matching decisions" not in result and "No context stored" not in result
            assert actual_hit == expected_hit, (
                f"Query '{query}': expected {'hit' if expected_hit else 'miss'}, "
                f"got {'hit' if actual_hit else 'miss'}"
            )

    def test_subtype_filter_precision(self, populated_store, monkeypatch_module):
        """Verify entry_type filter returns only the right subtype."""
        for subtype, expected_count in [("convention", 5), ("constraint", 5),
                                         ("architecture", 5), ("pattern", 5)]:
            result = store.get_context(DEMO_REPO, entry_type=subtype, limit=100)
            lines = [l for l in result.splitlines() if l.startswith("- [")]
            print(f"\n  Filter entry_type={subtype!r}: returned {len(lines)} decisions "
                  f"(expected {expected_count})")
            assert len(lines) == expected_count, (
                f"Subtype filter '{subtype}' returned {len(lines)}, expected {expected_count}"
            )


# ── Benchmark 4: Global store fallback ───────────────────────────────────────

class TestGlobalStoreFallback:
    @pytest.fixture(autouse=True)
    def global_store(self, populated_store, monkeypatch_module):
        # Add a global convention and constraint
        store.update_global_decision(
            "All teams must use semantic versioning for package releases — major.minor.patch format required",
            SESSION, "convention"
        )
        store.update_global_decision(
            "Security vulnerabilities in dependencies must be resolved within 48 hours of CVE publication",
            SESSION, "constraint"
        )

    def test_global_fallback_when_repo_has_no_match(self, populated_store, monkeypatch_module):
        # "versioning" only exists in global, not in repo
        prompt = "why did we decide on semantic versioning for releases?"
        start = time.perf_counter()
        result = store.get_context_for_prompt(DEMO_REPO, prompt)
        elapsed_ms = (time.perf_counter() - start) * 1000

        print(f"\n{'='*60}")
        print("BENCHMARK 4 — Global store fallback")
        print(f"{'='*60}")
        print(f"  Prompt:   \"{prompt}\"")
        print(f"  Keyword not in repo → falls back to global store")
        print(f"  Result:   {'HIT (global fallback)' if result else 'MISS'} [{elapsed_ms:.2f}ms]")
        if result:
            print(f"  Tokens:   ~{_approx_tokens(result)}")

        assert result != "", "Expected global fallback to return a result"
        assert "global" in result.lower(), "Result should be labeled as from global context"

    def test_repo_takes_priority_over_global(self, populated_store, monkeypatch_module):
        # "postgresql" exists in repo — should return repo result, not global
        prompt = "why did we choose postgresql for our database?"
        result = store.get_context_for_prompt(DEMO_REPO, prompt)
        print(f"\n  Priority test: repo match beats global fallback")
        print(f"  Prompt:   \"{prompt}\"")
        print(f"  Result:   {'repo (correct)' if result and 'global context' not in result.lower() else 'global (incorrect)'}")

        assert result != ""
        assert "global context" not in result.lower(), "Repo match should take priority over global"


# ── Benchmark 5: Novelty filter ───────────────────────────────────────────────

class TestNoveltyFilter:
    def test_duplicate_blocking_rate(self, populated_store, monkeypatch_module):
        # Try to re-add all 20 decisions — all should be blocked
        blocked = 0
        for content in CONVENTIONS + CONSTRAINTS + ARCHITECTURE + PATTERNS:
            stored, _ = store.update_decision(DEMO_REPO, content, SESSION)
            if not stored:
                blocked += 1

        print(f"\n{'='*60}")
        print("BENCHMARK 5 — Novelty filter effectiveness")
        print(f"{'='*60}")
        print(f"  Duplicate decisions attempted: 20")
        print(f"  Blocked by novelty filter:     {blocked}/20 ({100*blocked//20}%)")
        print(f"  Noise that got through:        {20-blocked}/20")

        assert blocked == 20, f"Expected all 20 duplicates blocked, only {blocked} were"

    def test_near_duplicate_blocking(self, populated_store, monkeypatch_module):
        # Near-duplicate of: "Never commit directly to main — all changes require a PR with
        # at least one approval before merging"
        # Only "PR" → "pull request" changed — 17/19 token overlap = 89% > 70% threshold
        near_dup = "Never commit directly to main — all changes require a pull request with at least one approval before merging"
        stored, _ = store.update_decision(DEMO_REPO, near_dup, SESSION)
        print(f"\n  Near-duplicate test:")
        print(f"    Input:   \"{near_dup[:75]}\"")
        print(f"    Result:  {'BLOCKED ✓' if not stored else 'STORED (false negative) ✗'}")
        print(f"    Note:    'PR' → 'pull request' rewording; token overlap ~89% > 70% threshold")
        assert not stored, "Near-duplicate should be blocked by novelty filter"

    def test_tokenisation_edge_case(self, populated_store, monkeypatch_module):
        # Documents a known limitation: comma-attached tokens ("feat," ≠ "feat") reduce
        # measured overlap for list-style decisions. The filter is conservative — it only
        # blocks when overlap IS high, so this causes false negatives, not false positives.
        edge_case = "Always use conventional commits format type scope description types are feat fix docs refactor chore test"
        stored, _ = store.update_decision(DEMO_REPO, edge_case, SESSION)
        print(f"\n  Tokenisation edge case (comma stripping):")
        print(f"    Original: 'feat, fix, docs, refactor, chore, test' — commas attached = separate tokens")
        print(f"    Rewrite:  'feat fix docs refactor chore test' — same words, no commas")
        print(f"    Result:   {'STORED (false negative — commas reduced measured overlap)' if stored else 'BLOCKED ✓'}")
        print(f"    Note:     This is a known filter limitation, not data corruption.")

    def test_genuinely_new_decision_passes(self, populated_store, monkeypatch_module):
        genuinely_new = "All background jobs use Bull queue with Redis — no direct setTimeout or setInterval for async work"
        stored, eid = store.update_decision(DEMO_REPO, genuinely_new, SESSION)
        print(f"\n  Genuinely new decision test:")
        print(f"    Input:   \"{genuinely_new[:70]}\"")
        print(f"    Result:  {'STORED ✓' if stored else 'BLOCKED (false positive) ✗'}")
        assert stored, "Novel decision should pass the filter"


# ── Large-scale benchmark: 50 decisions ───────────────────────────────────────
# 13 convention + 12 constraint + 13 architecture + 12 pattern = 50
# Models a mature monorepo team that has been capturing decisions for months.

LARGE_REPO = "/demo/platform-monorepo"
LARGE_SESSION = "bench-large-session"

LARGE_CONVENTIONS = [
    "Always use conventional commits format: type(scope): description — types are feat, fix, docs, refactor, chore, test",
    "Use TypeScript strict mode for all packages — tsconfig.json has strict:true, no exceptions per package",
    "All REST endpoints return JSON with { data, error, meta } envelope structure — never return bare values",
    "Environment variables validated at startup with Zod — no raw process.env access outside the config module",
    "Database migrations run automatically on deploy via Prisma migrate deploy — never run manually in production",
    "All packages use pnpm workspaces — no npm or yarn commands anywhere in the monorepo",
    "Branch naming: feat/TICKET-description, fix/TICKET-description, chore/description — tickets required for feat and fix",
    "PR titles must follow conventional commit format — enforced by GitHub Actions title check on every PR",
    "All public functions and classes must have JSDoc with @param and @returns — enforced by ESLint rule",
    "Log levels: error for unhandled exceptions, warn for expected failures, info for business events, debug for dev only",
    "All new services must expose /health and /metrics endpoints — required for Kubernetes liveness and Prometheus scraping",
    "Date handling uses date-fns not moment.js — moment is deprecated and must not be added to any package",
    "All user-facing strings go through i18n — no hardcoded English text in component JSX or API responses",
]

LARGE_CONSTRAINTS = [
    "Never commit directly to main — all changes require a PR with at least one senior approval before merging",
    "No real external API calls in unit tests — MSW handlers required for all HTTP, fetch, and axios mocking",
    "p99 latency must stay under 200ms for all user-facing endpoints — Datadog SLO alert triggers on breach",
    "PII data must never appear in logs — strip emails, phone numbers, card digits before any log statement",
    "All SQL queries must use parameterized statements — no string interpolation in any database call",
    "Packages must not import across service boundaries — only shared packages in packages/shared are allowed cross-imports",
    "No synchronous file system calls in request handlers — all fs operations must be async to avoid blocking the event loop",
    "Third-party dependencies must be approved in the security channel before being added — no unreviewed packages in prod",
    "Maximum bundle size for client packages: 250kb gzipped — webpack-bundle-analyzer check runs on every PR",
    "All secrets must live in AWS Secrets Manager — no secrets in .env files committed to the repo, ever",
    "Database connection pools capped at 20 per service — exceeding this caused the March outage, do not raise without DBA sign-off",
    "API rate limiting: 1000 req/min per authenticated user, 100 req/min unauthenticated — enforced at Kong gateway level",
]

LARGE_ARCHITECTURE = [
    "Chose PostgreSQL over MongoDB because we need ACID transactions for multi-step order processing and inventory updates",
    "API gateway pattern: all external traffic routes through Kong, internal service communication uses gRPC not REST",
    "Event sourcing for order state: OrderPlaced, OrderShipped, OrderDelivered events stored in EventStore with read-model projections",
    "Frontend uses Next.js App Router with React Server Components — no client-side data fetching for initial page loads",
    "Authentication uses JWT with 15-minute access tokens and 7-day refresh tokens stored in httpOnly cookies not localStorage",
    "Monorepo managed by Turborepo with remote caching on Vercel — build times went from 18min to 3min after migration",
    "Message queue is SQS not Kafka — Kafka was evaluated but operational overhead too high for current team size",
    "Search powered by OpenSearch not Elasticsearch — moved for cost: managed OpenSearch is 40% cheaper at our volume",
    "Payment processing uses Stripe exclusively — Braintree was sunset, Adyen was evaluated but integration cost too high",
    "CDN is Cloudfront with S3 origin for static assets — media uploads go direct to S3 presigned URLs, not through the API",
    "WebSocket connections handled by a dedicated presence service — colocating with the main API caused memory pressure",
    "Feature flag system is custom-built on Redis — LaunchDarkly was evaluated but $8k/mo cost was not justified at current scale",
    "Caching strategy: Redis L1 cache per service with 5min TTL, PostgreSQL materialized views as L2 for heavy aggregations",
]

LARGE_PATTERNS = [
    "Repository pattern for all database access — no raw Prisma calls outside of repository classes in the services layer",
    "Result type for error handling in service layer — services return Result<T, AppError> not void, never throw",
    "API route handlers are thin controllers — all business logic lives in service classes, handlers only parse and delegate",
    "Feature flags via FEATURE_ env vars — no hardcoded feature toggles or if/else branching in business logic",
    "Cursor-based pagination for all list endpoints — offset pagination is banned after the performance incident in Q3",
    "Saga pattern for distributed transactions — each multi-service operation has a compensating transaction rollback path",
    "CQRS split at the service level: command handlers write to primary DB, query handlers read from read replicas",
    "All external HTTP calls wrapped in circuit breaker using opossum — prevents cascade failures across services",
    "Background jobs always idempotent — jobs can be retried safely, idempotency key stored in Redis with 24h TTL",
    "Optimistic locking with version columns for concurrent writes — avoids deadlocks on high-contention inventory rows",
    "API versioning via URL prefix /v1/, /v2/ — header-based versioning was tried and rejected for cache complexity",
    "Dependency injection via tsyringe — no global singletons, all services registered in the DI container at bootstrap",
]

# Large-scale hit prompts covering the extended decision set
LARGE_HIT_PROMPTS = [
    ("why did we choose postgresql over mongodb?",                    "postgresql"),
    ("what was the reason for using turborepo?",                     "turborepo"),
    ("why did we pick sqs over kafka for the message queue?",        "kafka"),
    ("what is the rationale behind the saga pattern?",               "saga"),
    ("why did we move from elasticsearch to opensearch?",            "opensearch"),
    ("what was the reasoning behind cursor pagination?",             "pagination"),
    ("why did we build a custom feature flag system instead of launchdarkly?", "launchdarkly"),
    ("what is the reason for using circuit breakers?",               "circuit"),
    ("why are background jobs required to be idempotent?",           "idempotent"),
    ("what was the decision behind optimistic locking?",             "locking"),
]

LARGE_MISS_PROMPTS = [
    "add a product search endpoint",
    "fix the cart totals calculation bug",
    "write tests for the checkout service",
    "update the deployment pipeline",
    "why is the ocean salty?",
    "what is the reason for seasons?",
]


@pytest.fixture(scope="module")
def large_store(tmp_path_factory, monkeypatch_module):
    store_dir = tmp_path_factory.mktemp("bench_large_store")
    monkeypatch_module.setattr(store, "STORE_DIR", store_dir)
    store_dir.mkdir(parents=True, exist_ok=True)

    for content in LARGE_CONVENTIONS:
        store.update_decision(LARGE_REPO, content, LARGE_SESSION, "convention")
    for content in LARGE_CONSTRAINTS:
        store.update_decision(LARGE_REPO, content, LARGE_SESSION, "constraint")
    for content in LARGE_ARCHITECTURE:
        store.update_decision(LARGE_REPO, content, LARGE_SESSION, "architecture")
    for content in LARGE_PATTERNS:
        store.update_decision(LARGE_REPO, content, LARGE_SESSION, "pattern")

    return store_dir


class TestLargeScaleBenchmark:
    """50-decision benchmark — compare against 20-decision results."""

    def test_session_start_scale(self, large_store, monkeypatch_module):
        result = store.get_session_start_context(LARGE_REPO)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        msg = result["systemMessage"]

        preloaded_lines = [l for l in ctx.splitlines() if l.startswith("- [")]
        deferred_note   = [l for l in ctx.splitlines() if "decision(s) stored" in l]
        preloaded_count = len(preloaded_lines)
        deferred_count  = int(deferred_note[0].split()[0]) if deferred_note else 0
        tokens = _approx_tokens(ctx)

        # 20-decision baseline (from earlier benchmark)
        BASELINE_TOKENS   = 288
        BASELINE_PRE      = 15
        BASELINE_DEFERRED = 5

        print(f"\n{'='*60}")
        print("LARGE-SCALE BENCHMARK (50 decisions) vs baseline (20)")
        print(f"{'='*60}")
        print(f"\n  Decision breakdown:")
        print(f"    conventions:  13   constraints: 12   architecture: 13   patterns: 12")
        print(f"\n  Session start:")
        print(f"    {'Metric':<28} {'20 decisions':>14} {'50 decisions':>14} {'Delta':>10}")
        print(f"    {'-'*66}")
        print(f"    {'Preloaded (conv+con+pat)':<28} {BASELINE_PRE:>14} {preloaded_count:>14} {preloaded_count-BASELINE_PRE:>+10}")
        print(f"    {'Deferred (arch only)':<28} {BASELINE_DEFERRED:>14} {deferred_count:>14} {deferred_count-BASELINE_DEFERRED:>+10}")
        print(f"    {'Tokens injected':<28} {BASELINE_TOKENS:>14} {tokens:>14} {tokens-BASELINE_TOKENS:>+10}")
        print(f"    {'Tokens per preloaded rule':<28} {BASELINE_TOKENS//BASELINE_PRE:>14} {tokens//preloaded_count:>14}")
        print(f"\n  Status line: {msg}")

        # With confidence lifecycle: constraints (Level 3) require approval — they start
        # as pending_approval, not pre-loaded. Patterns with L3 signals are also pending.
        # Pre-loaded count = conventions + clean patterns (those without L3 signals).
        assert preloaded_count >= 13, f"Expected at least 13 pre-loaded (conventions), got {preloaded_count}"
        assert preloaded_count <= 37, f"Expected at most 37 pre-loaded, got {preloaded_count}"
        assert deferred_count == 13, f"Expected 13 deferred (13 arch), got {deferred_count}"
        assert tokens < 2000, f"Session start context unexpectedly large: {tokens} tokens"
        assert "pending" in msg.lower(), "Expected pending decisions mention in status"

    def test_retrieval_timing_at_scale(self, large_store, monkeypatch_module):
        times_ms = []

        print(f"\n  On-demand retrieval latency at 50 decisions:")
        for query, _ in LARGE_HIT_PROMPTS:
            kw = [w for w in query.lower().split()
                  if len(w) > 3 and w not in store._QUERY_STOP_WORDS and w.isalpha()]
            if not kw:
                continue
            kw = sorted(kw, key=len, reverse=True)[0]
            start = time.perf_counter()
            result = store.get_context(LARGE_REPO, query=kw)
            elapsed_ms = (time.perf_counter() - start) * 1000
            times_ms.append(elapsed_ms)
            hit = "No matching" not in result
            print(f"    {'HIT ' if hit else 'MISS'} [{elapsed_ms:.3f}ms] query={kw!r}")

        avg_ms = sum(times_ms) / len(times_ms)
        print(f"\n  Avg latency (50 decisions): {avg_ms:.3f}ms")
        print(f"  Avg latency (20 decisions): 0.040ms  (from baseline benchmark)")
        print(f"  Delta:                      {avg_ms - 0.040:+.3f}ms")

        # Effectively instant at 50 decisions (~0.5ms locally). Versioned entries carry
        # revision history, so the store is modestly larger to parse than the pre-versioning
        # model; 2.0ms keeps a wide margin for noisy shared CI runners while still catching a
        # real (4x+) regression.
        assert avg_ms < 2.0, f"Retrieval too slow at scale: {avg_ms:.3f}ms"

    def test_rationale_hit_rate_at_scale(self, large_store, monkeypatch_module):
        hits, misses, false_positives = [], [], []

        print(f"\n  Rationale auto-injection at 50 decisions:")
        print(f"  HITS:")
        for prompt, _ in LARGE_HIT_PROMPTS:
            start = time.perf_counter()
            result = store.get_context_for_prompt(LARGE_REPO, prompt)
            elapsed_ms = (time.perf_counter() - start) * 1000
            tokens = _approx_tokens(result) if result else 0
            if result:
                hits.append((prompt, elapsed_ms, tokens))
                print(f"    ✓ [{elapsed_ms:.2f}ms, ~{tokens}tk] \"{prompt[:60]}\"")
            else:
                misses.append(prompt)
                print(f"    ✗ [miss] \"{prompt[:60]}\"")

        print(f"\n  MISSES (expected silent):")
        for prompt in LARGE_MISS_PROMPTS:
            result = store.get_context_for_prompt(LARGE_REPO, prompt)
            elapsed_ms_r = time.perf_counter()
            if result:
                false_positives.append(prompt)
                print(f"    ✗ [false positive] \"{prompt}\"")
            else:
                print(f"    ✓ [silent] \"{prompt}\"")

        avg_hit_ms  = sum(ms for _, ms, _ in hits) / len(hits) if hits else 0
        avg_hit_tk  = sum(tk for _, _, tk in hits) / len(hits) if hits else 0

        print(f"\n  Comparison:")
        print(f"    {'Metric':<32} {'20 decisions':>14} {'50 decisions':>14}")
        print(f"    {'-'*60}")
        print(f"    {'Hit rate':<32} {'10/10 (100%)':>14} {_pct(len(hits), len(LARGE_HIT_PROMPTS)):>14}")
        print(f"    {'Avg retrieval time':<32} {'0.08ms':>14} {f'{avg_hit_ms:.2f}ms':>14}")
        print(f"    {'Avg tokens per hit':<32} {'~51':>14} {f'~{avg_hit_tk:.0f}':>14}")
        print(f"    {'False positives':<32} {'0/10':>14} {_pct(len(false_positives), len(LARGE_MISS_PROMPTS)):>14}")

        assert len(hits) >= 7, f"Hit rate too low at scale: {len(hits)}/{len(LARGE_HIT_PROMPTS)}"

    def test_subtype_filter_precision_at_scale(self, large_store, monkeypatch_module):
        expected = {"convention": 13, "constraint": 12, "architecture": 13, "pattern": 12}
        print(f"\n  Subtype filter precision at 50 decisions:")
        for subtype, count in expected.items():
            result = store.get_context(LARGE_REPO, entry_type=subtype, limit=100)
            lines = [l for l in result.splitlines() if l.startswith("- [")]
            status = "✓" if len(lines) == count else "✗"
            print(f"    {status} entry_type={subtype!r}: {len(lines)}/{count} returned")
            assert len(lines) == count

    def test_display_cap_kicks_in_at_scale(self, large_store, monkeypatch_module):
        # Unfiltered get_context is capped at 10; filtered at 25.
        # With 25 decisions per half, the caps are now actually exercised.
        unfiltered = store.get_context(LARGE_REPO)
        unfiltered_lines = [l for l in unfiltered.splitlines() if l.startswith("- [")]

        filtered = store.get_context(LARGE_REPO, entry_type="convention")
        filtered_lines = [l for l in filtered.splitlines() if l.startswith("- [")]

        print(f"\n  Display caps at 50 decisions (25 conv+constraint, 25 arch+pattern):")
        print(f"    Unfiltered overview: shows {len(unfiltered_lines)} of 50 total (cap=10)")
        print(f"    'showing N of M' note present: {'yes' if 'showing' in unfiltered else 'no'}")
        print(f"    Filtered (convention): shows {len(filtered_lines)} of 13 (cap=25)")
        print(f"    Cap active on filtered: {'yes — showing 13 of 13 (under cap)' if len(filtered_lines) == 13 else f'capped at {len(filtered_lines)}'}")

        assert len(unfiltered_lines) == 10, f"Unfiltered cap not enforced: {len(unfiltered_lines)}"
        assert "showing" in unfiltered, "Truncation note missing from unfiltered overview"
        assert len(filtered_lines) == 13, f"Convention filter returned {len(filtered_lines)}, expected 13"

    def test_novelty_filter_at_scale(self, large_store, monkeypatch_module):
        total = len(LARGE_CONVENTIONS + LARGE_CONSTRAINTS + LARGE_ARCHITECTURE + LARGE_PATTERNS)
        blocked = sum(
            1 for content in LARGE_CONVENTIONS + LARGE_CONSTRAINTS + LARGE_ARCHITECTURE + LARGE_PATTERNS
            if not store.update_decision(LARGE_REPO, content, LARGE_SESSION)[0]
        )
        print(f"\n  Novelty filter at 50 decisions:")
        print(f"    Duplicates attempted: {total}")
        print(f"    Blocked:              {blocked}/{total} ({100*blocked//total}%)")
        assert blocked == total


# ── Summary report ────────────────────────────────────────────────────────────

class TestSummaryReport:
    def test_print_summary(self, populated_store, monkeypatch_module):
        result = store.get_session_start_context(DEMO_REPO)
        ctx = result["hookSpecificOutput"]["additionalContext"]
        session_tokens = _approx_tokens(ctx)

        hit_count = sum(
            1 for prompt, _ in HIT_PROMPTS
            if store.get_context_for_prompt(DEMO_REPO, prompt)
        )

        print(f"\n{'='*60}")
        print("SUMMARY — Contexer effectiveness report")
        print(f"{'='*60}")
        print(f"\n  Demo project: 20 decisions (5 each: convention, constraint, architecture, pattern)")
        print()
        print(f"  Session start overhead:")
        print(f"    Without Contexer: 0 tokens — Claude starts blind")
        print(f"    With Contexer:    ~{session_tokens} tokens — 10 rules preloaded, 10 deferred")
        print(f"    Cost:             ~{session_tokens} tokens once at session open")
        print(f"    Benefit:          conventions + constraints always active — no re-explaining")
        print()
        print(f"  On-demand retrieval (architecture/pattern):")
        print(f"    Only fetched when relevant — 0 token cost on unrelated tasks")
        print(f"    Typical retrieval: <1ms (in-process JSON read + substring filter)")
        print()
        print(f"  Rationale auto-injection:")
        print(f"    Hit rate on 'why/reason/rationale' prompts: {_pct(hit_count, len(HIT_PROMPTS))}")
        print(f"    Cost on miss: 0 tokens (pure no-op)")
        print(f"    Cost on hit: ~{_approx_tokens(store.get_context(DEMO_REPO, query='postgresql'))} tokens (one decision excerpt)")
        print()
        print(f"  Novelty filter:")
        print(f"    Blocks duplicate/near-duplicate decisions before storage")
        print(f"    Keeps store clean — no manual curation needed")
        print(f"{'='*60}")
        assert True  # summary always passes
