"""
Extended Contexer benchmark — accuracy and reliability validation.

Run with:
    uv run pytest tests/test_benchmark_extended.py -v -s

Tests:
  1. Statistical reliability  — 100 runs per operation, p50/p95/p99/stddev
  2. Token approximation      — validate word×1.3 vs char÷4 vs known examples
  3. Display cap boundaries   — exactly at cap, one over, one under
  4. Storage at capacity      — 500-entry cap enforcement and filter performance
  5. Realistic prompt noise   — typos, truncation, multi-intent, indirect phrasing
  6. Novelty threshold        — sensitivity sweep 50%–90% with false-neg/pos counts
  7. Decision length          — short (5w), medium (25w), long (80w) effects on filter and tokens
  8. Concurrent session       — atomic-write invariants: no torn JSON under concurrent writers/readers
"""

import json
import math
import statistics
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest

from contexer import store


# ── Helpers ───────────────────────────────────────────────────────────────────

def _tokens_words(text: str) -> int:
    return math.ceil(len(text.split()) * 1.3)

def _tokens_chars(text: str) -> int:
    return math.ceil(len(text) / 4)

def _overlap(a: str, b: str) -> float:
    ta = set(a.lower().split())
    tb = set(b.lower().split())
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / max(len(ta), len(tb))

def _pstats(values: list[float]) -> dict:
    s = sorted(values)
    n = len(s)
    return {
        "mean":   statistics.mean(s),
        "p50":    s[n // 2],
        "p95":    s[int(n * 0.95)],
        "p99":    s[int(n * 0.99)],
        "stddev": statistics.stdev(s) if n > 1 else 0.0,
        "min":    s[0],
        "max":    s[-1],
    }

def _print_stats(label: str, stats: dict) -> None:
    print(f"    {label}:")
    print(f"      mean={stats['mean']:.3f}ms  p50={stats['p50']:.3f}ms  "
          f"p95={stats['p95']:.3f}ms  p99={stats['p99']:.3f}ms  "
          f"stddev={stats['stddev']:.3f}ms  min={stats['min']:.3f}ms  max={stats['max']:.3f}ms")

def _write_direct(repo: str, n: int, subtype: str = "architecture", offset: int = 0) -> None:
    """Write n decisions directly to the store, bypassing the novelty filter.
    Used for cap/capacity tests where we need exact entry counts."""
    data = store._load(repo)
    for i in range(n):
        data["entries"].append({
            "id": str(uuid.uuid4()),
            "type": "decision",
            "subtype": subtype,
            "content": f"Entry {offset+i:05d} subtype={subtype}",
            "session_id": "bench",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    data["entries"] = data["entries"][-store.MAX_ENTRIES:]
    store._save(repo, data)

SESSION = "ext-bench-session"
RUNS    = 100

# 9 genuinely diverse base decisions for reliability / noise / threshold tests
BASE_DECISIONS = [
    ("We use PostgreSQL as the primary database for ACID compliance and transactional reliability", "architecture"),
    ("Authentication uses JWT access tokens with 15-minute expiry and httpOnly refresh cookies",    "architecture"),
    ("All services communicate internally via gRPC and expose REST only at the API gateway",        "architecture"),
    ("Always use conventional commits: feat fix docs refactor chore test as allowed types",         "convention"),
    ("TypeScript strict mode is enabled globally — no any types allowed in production code",        "convention"),
    ("No raw SQL outside repository classes to maintain clean data access boundaries",              "pattern"),
    ("Circuit breaker wraps every external HTTP call to prevent cascade failures across services",  "pattern"),
    ("Never commit untested code to main — CI blocks merge if test coverage drops below threshold", "constraint"),
    ("PII must never appear in logs — strip emails phone numbers and card data before logging",     "constraint"),
]


# ── Base store (module-scoped, created once) ──────────────────────────────────

@pytest.fixture(scope="module")
def base_store_dir(tmp_path_factory):
    """Creates the base store with 9 decisions. Returns (dir, repo).
    Each test class that needs it applies its own function-scoped monkeypatch."""
    d = tmp_path_factory.mktemp("ext_base")
    d.mkdir(parents=True, exist_ok=True)
    repo = "/bench/app"
    original = store.STORE_DIR
    store.STORE_DIR = d
    for content, subtype in BASE_DECISIONS:
        store.update_decision(repo, content, SESSION, subtype)
    store.STORE_DIR = original
    return d, repo


# ═══════════════════════════════════════════════════════════════════════════════
# 1. STATISTICAL RELIABILITY
# ═══════════════════════════════════════════════════════════════════════════════

class TestStatisticalReliability:

    @pytest.fixture(autouse=True)
    def _setup(self, base_store_dir, monkeypatch):
        d, repo = base_store_dir
        monkeypatch.setattr(store, "STORE_DIR", d)
        self.repo = repo

    def test_get_context_latency_distribution(self):
        print(f"\n{'='*60}")
        print(f"BENCHMARK 1 — Statistical reliability ({RUNS} runs each)")
        print(f"{'='*60}")

        times_cold, times_warm = [], []
        for i in range(RUNS):
            t = time.perf_counter()
            store.get_context(self.repo, query="postgresql")
            elapsed = (time.perf_counter() - t) * 1000
            (times_cold if i == 0 else times_warm).append(elapsed)

        warm = _pstats(times_warm)
        print(f"\n  get_context(query='postgresql') — {RUNS} runs")
        print(f"    Cold (run 1):  {times_cold[0]:.3f}ms")
        _print_stats("Warm (runs 2-100)", warm)
        assert warm["p99"] < 5.0, f"p99 too slow: {warm['p99']:.3f}ms"

    def test_rationale_injection_latency_distribution(self):
        prompt = "why did we choose postgresql over other databases?"
        times = [
            (lambda: (t := time.perf_counter(), store.get_context_for_prompt(self.repo, prompt), (time.perf_counter() - t) * 1000)[2])()
            for _ in range(RUNS)
        ]
        stats = _pstats(times)
        print(f"\n  get_context_for_prompt (rationale hit) — {RUNS} runs")
        _print_stats("Stats", stats)
        assert stats["p99"] < 10.0

    def test_miss_is_zero_cost(self):
        times = []
        for _ in range(RUNS):
            t = time.perf_counter()
            result = store.get_context_for_prompt(self.repo, "add a checkout endpoint")
            times.append((time.perf_counter() - t) * 1000)
            assert result == ""
        stats = _pstats(times)
        print(f"\n  get_context_for_prompt (no-op miss) — {RUNS} runs")
        _print_stats("Stats", stats)
        assert stats["mean"] < 1.0, f"No-op too slow: {stats['mean']:.3f}ms"

    def test_timing_variance(self):
        times = []
        for _ in range(RUNS):
            t = time.perf_counter()
            store.get_context(self.repo, query="jwt")
            times.append((time.perf_counter() - t) * 1000)
        stats = _pstats(times)
        cv = stats["stddev"] / stats["mean"]
        print(f"\n  Timing variance — get_context, {RUNS} runs")
        print(f"    mean={stats['mean']:.3f}ms  stddev={stats['stddev']:.3f}ms  CV={cv:.2f}")
        if cv > 2.0:
            print(f"    WARNING: high variance (CV={cv:.2f}) — single-run timing unreliable")


# ═══════════════════════════════════════════════════════════════════════════════
# 2. TOKEN APPROXIMATION ACCURACY
# ═══════════════════════════════════════════════════════════════════════════════

SAMPLES = [
    "use postgresql",
    "always validate input at system boundaries",
    "Authentication uses JWT access tokens with 15-minute expiry and httpOnly refresh cookies",
    "All services communicate internally via gRPC and expose REST only at the API gateway, "
    "no direct service-to-service HTTP calls are allowed under any circumstances",
]

class TestTokenApproximationAccuracy:

    def test_word_vs_char_disagreement(self):
        print(f"\n{'='*60}")
        print("BENCHMARK 2 — Token approximation accuracy")
        print(f"{'='*60}")
        print(f"\n  {'Text (first 50 chars)':<52} {'word×1.3':>10} {'char÷4':>8} {'diff%':>7}")
        print(f"  {'-'*77}")
        errors = []
        for text in SAMPLES:
            w, c = _tokens_words(text), _tokens_chars(text)
            diff = abs(w - c) / max(w, c) * 100
            errors.append(diff)
            print(f"  {text[:52]:<52} {w:>10} {c:>8} {diff:>6.1f}%")
        avg = statistics.mean(errors)
        print(f"\n  Avg disagreement: {avg:.1f}%  — treat all token figures as ±20% estimates")
        assert avg < 40, f"Estimators diverge too much: {avg:.1f}%"

    def test_scales_linearly(self):
        base = "we use postgresql for transactional consistency"
        ratios = [_tokens_words(base * k) / _tokens_words(base) for k in range(2, 5)]
        print(f"\n  Linearity check:")
        for i, r in enumerate(ratios, 2):
            print(f"    {i}× length → {r:.2f}× tokens  (expected ~{i}×)")
            assert abs(r - i) / i < 0.15

    def test_cost_by_length(self):
        cases = [
            ("short (~2w)",   "use postgresql"),
            ("medium (~13w)", "We chose PostgreSQL over MongoDB because we need ACID transactions for order processing"),
            ("long (~60w)",   ("We evaluated PostgreSQL, MongoDB, CockroachDB, and DynamoDB before selecting PostgreSQL. "
                               "The deciding factors were: ACID transaction support for multi-step order processing, "
                               "existing team expertise with SQL, lower operational complexity, and native JSONB support "
                               "for semi-structured product metadata without sacrificing schema integrity guarantees.")),
        ]
        print(f"\n  Token cost by length:")
        print(f"    {'Category':<14} {'Words':>6} {'word×1.3':>10} {'char÷4':>8}")
        for label, text in cases:
            print(f"    {label:<14} {len(text.split()):>6} {_tokens_words(text):>10} {_tokens_chars(text):>8}")


# ═══════════════════════════════════════════════════════════════════════════════
# 3. DISPLAY CAP BOUNDARIES
# ═══════════════════════════════════════════════════════════════════════════════

class TestDisplayCapBoundaries:
    """Each test gets a fresh isolated store via function-scoped monkeypatch."""

    @pytest.fixture(autouse=True)
    def _fresh(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "STORE_DIR", tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.repo = "/bench/cap"

    def _count(self, result: str) -> int:
        return len([l for l in result.splitlines() if l.startswith("- [")])

    def test_unfiltered_exactly_at_cap(self):
        _write_direct(self.repo, 10)
        result = store.get_context(self.repo)
        shown = self._count(result)
        has_note = "showing" in result
        print(f"\n{'='*60}")
        print("BENCHMARK 3 — Display cap boundaries")
        print(f"{'='*60}")
        print(f"\n  Unfiltered cap=10:  10 stored → shows {shown}  truncation note: {'yes' if has_note else 'no'}")
        assert shown == 10
        assert not has_note, "No truncation note at exactly the cap"

    def test_unfiltered_one_over_cap(self):
        _write_direct(self.repo, 11)
        result = store.get_context(self.repo)
        shown = self._count(result)
        has_note = "showing" in result
        print(f"    11 stored → shows {shown}  truncation note: {'yes' if has_note else 'no'}")
        assert shown == 10
        assert has_note
        assert "showing 10 of 11" in result

    def test_filtered_exactly_at_cap(self):
        _write_direct(self.repo, 25, subtype="convention")
        result = store.get_context(self.repo, entry_type="convention")
        shown = self._count(result)
        has_note = "showing" in result
        print(f"\n  Filtered cap=25:    25 conv stored → shows {shown}  note: {'yes' if has_note else 'no'}")
        assert shown == 25
        assert not has_note

    def test_filtered_one_over_cap(self):
        _write_direct(self.repo, 26, subtype="convention")
        result = store.get_context(self.repo, entry_type="convention")
        shown = self._count(result)
        has_note = "showing" in result
        print(f"    26 conv stored → shows {shown}  note: {'yes' if has_note else 'no'}")
        assert shown == 25
        assert has_note
        assert "showing 25 of 26" in result

    def test_limit_override(self):
        _write_direct(self.repo, 30, subtype="pattern")
        result = store.get_context(self.repo, entry_type="pattern", limit=30)
        shown = self._count(result)
        print(f"\n  limit=30 override:  30 stored → shows {shown}")
        assert shown == 30


# ═══════════════════════════════════════════════════════════════════════════════
# 4. STORAGE AT CAPACITY (500 entries)
# ═══════════════════════════════════════════════════════════════════════════════

class TestStorageAtCapacity:

    @pytest.fixture(scope="class")
    def full_store(self, tmp_path_factory):
        d = tmp_path_factory.mktemp("ext_full")
        d.mkdir(parents=True, exist_ok=True)
        repo = "/bench/full"
        original = store.STORE_DIR
        store.STORE_DIR = d
        _write_direct(repo, 505)   # write 505 directly — cap keeps last 500
        store.STORE_DIR = original
        return d, repo

    @pytest.fixture(autouse=True)
    def _setup(self, full_store, monkeypatch):
        d, repo = full_store
        monkeypatch.setattr(store, "STORE_DIR", d)
        self.d, self.repo = d, repo

    def test_cap_enforced(self):
        slug = store._slug(self.repo)
        data = json.loads((self.d / f"{slug}.json").read_text())
        count = len(data["entries"])
        print(f"\n{'='*60}")
        print("BENCHMARK 4 — Storage at capacity (500 entries)")
        print(f"{'='*60}")
        print(f"\n  Wrote 505 directly → stored: {count}  (cap={store.MAX_ENTRIES})")
        assert count == store.MAX_ENTRIES

    def test_oldest_entries_dropped(self):
        slug = store._slug(self.repo)
        data = json.loads((self.d / f"{slug}.json").read_text())
        first = data["entries"][0]["content"]
        last  = data["entries"][-1]["content"]
        print(f"\n  First retained: {first}")
        print(f"  Last retained:  {last}")
        # entries 0–4 dropped; entry 5 should be first
        assert "00004" not in first, "Entry 4 should have been dropped"
        assert "00005" in first or "00006" in first, f"Unexpected first entry: {first}"

    def test_retrieval_latency_at_capacity(self):
        times = []
        for _ in range(50):
            t = time.perf_counter()
            store.get_context(self.repo, query="00300")
            times.append((time.perf_counter() - t) * 1000)
        stats = _pstats(times)
        print(f"\n  Retrieval latency at 500 entries (50 runs):")
        _print_stats("Stats", stats)
        assert stats["p99"] < 50.0

    def test_novelty_filter_write_latency_at_capacity(self):
        times = []
        for i in range(20):
            content = (f"Novel decision {i} about distributed tracing with OpenTelemetry "
                       f"integration for service mesh observability platform {i}")
            t = time.perf_counter()
            store.update_decision(self.repo, content, SESSION, "architecture")
            times.append((time.perf_counter() - t) * 1000)
        stats = _pstats(times)
        print(f"\n  Novelty filter write latency at 500 entries (20 writes):")
        _print_stats("Stats", stats)
        # The size pre-filter in _find_match skips the set intersection for most
        # candidates, so write latency stays flat at capacity (~20ms p99 locally).
        # 100ms leaves ~5x headroom for slower CI runners.
        assert stats["p99"] < 100.0


# ═══════════════════════════════════════════════════════════════════════════════
# 5. REALISTIC PROMPT NOISE
# ═══════════════════════════════════════════════════════════════════════════════
#
# Expected values derived by tracing keyword extraction + substring search
# against the 9 BASE_DECISIONS. Key constraints of the matching algorithm:
#   - Rationale keyword (why/reason/rationale/decided/...) required
#   - Content keywords: isalpha, len > 3, not in _QUERY_STOP_WORDS
#   - Keywords sorted longest-first, top 3 searched
#   - Match = case-insensitive substring of stored decision content
#
# Correctly-predicted hits and misses are marked ✓; documented surprises ↑.

NOISY_PROMPTS = [
    # prompt, expected_hit, note
    ("why did we chooose postgresql over mongodb?",
     True,  "✓ typo in 'chooose' — 'postgresql'(10) is keyword #1, matches decision"),
    ("what was the reason we chose gRPC?",
     True,  "✓ 'chose'=stop word; only 'grpc'(4) extracted, matches decision"),
    ("why postgres?",
     True,  "↑ short prompt but 'postgres'(8) substring-matches 'postgresql'"),
    ("reason for jwt?",
     True,  "✓ 'jwt' now extracted (len>=3 threshold); \bword-boundary match hits JWT decision"),
    ("remind me of the database decision",
     True,  "↑ indirect phrasing; 'database'(8) keyword substring-matches 'primary database'"),
    ("explain why authentication was chosen over alternatives",
     True,  "✓ 'authentication'(14) is clear keyword #1; substring-matches 'Authentication' in JWT decision"),
    ("fix the login bug and explain why we chose jwt",
     True,  "✓ 'jwt' now extracted (len>=3); 'login' not stored but 'jwt' \bmatches JWT decision"),
    ("refactor the db layer, also what was the reason for gRPC?",
     True,  "✓ 'grpc' extracted (? stripped); matches gRPC decision"),
    ("why RSC over CSR for rendering?",
     False, "✓ fixed: \bover no longer matches 'coverage'; 'rendering' not stored → correct miss"),
    ("what is the reason for the repo pattern?",
     True,  "✓ 'repo'(4) substring-matches 'repository'; 'pattern'(7) also matches"),
]

class TestRealisticPromptNoise:

    @pytest.fixture(autouse=True)
    def _setup(self, base_store_dir, monkeypatch):
        d, repo = base_store_dir
        monkeypatch.setattr(store, "STORE_DIR", d)
        self.repo = repo

    def test_noisy_prompt_distribution(self):
        print(f"\n{'='*60}")
        print("BENCHMARK 5 — Realistic prompt noise")
        print(f"{'='*60}")
        print(f"\n  Algorithm constraints:")
        print(f"    - Rationale word required (why/reason/rationale/decided...)")
        print(f"    - Content keyword: isalpha AND len >= 3 AND not stop word (jwt/api/sdk now included)")
        print(f"    - Top 3 keywords by length searched (\\b left-boundary match — no mid-word false positives)")
        print(f"\n  {'Prompt':<55} {'Exp':>5} {'Act':>5} {'OK':>4}  Note")
        print(f"  {'-'*100}")

        correct = 0
        findings = {"true_hit": [], "true_miss": [], "surprise": []}
        for prompt, expected, note in NOISY_PROMPTS:
            result  = store.get_context_for_prompt(self.repo, prompt)
            actual  = bool(result)
            match   = actual == expected
            if match: correct += 1
            marker  = "✓" if match else "✗"
            exp_str = "hit"  if expected else "miss"
            act_str = "hit"  if actual   else "miss"
            print(f"  {marker} {prompt[:55]:<55} {exp_str:>5} {act_str:>5}  {note[:55]}")
            cat = "true_hit" if expected and actual else ("true_miss" if not expected and not actual else "surprise")
            findings[cat].append(prompt)

        accuracy = correct / len(NOISY_PROMPTS) * 100
        print(f"\n  Correct predictions: {correct}/{len(NOISY_PROMPTS)} ({accuracy:.0f}%)")
        print(f"  True hits:   {len(findings['true_hit'])}  |  True misses: {len(findings['true_miss'])}  |  Surprises: {len(findings['surprise'])}")
        print(f"\n  Key findings:")
        print(f"    - Short tech terms ≤3 chars (jwt, db, ui) are NEVER extracted as keywords")
        print(f"    - Indirect phrasing ('remind me', 'database') still hits via substring match")
        print(f"    - Top-3-by-length rule risks excluding the relevant keyword if 3+ longer irrelevant words exist")
        print(f"    - Short common words ('over') produce false positives via substring ('coverage')")

        # All expected values were derived by tracing the algorithm — they should all match
        assert correct == len(NOISY_PROMPTS), (
            f"Prediction mismatch: expected all {len(NOISY_PROMPTS)}, got {correct}. "
            f"This means the algorithm changed."
        )


# ═══════════════════════════════════════════════════════════════════════════════
# 6. NOVELTY THRESHOLD SENSITIVITY
# ═══════════════════════════════════════════════════════════════════════════════

THRESHOLD_PAIRS = [
    # (original, rewrite, label)
    (
        "we use postgresql as the primary database for transactional consistency and reliability",
        "we use postgresql as the primary database for transactional consistency and durability",
        "1 word changed (reliability→durability)",
    ),
    (
        "never commit untested code to main branch without approval from senior engineer",
        "never commit untested code to main without approval from a senior engineer review",
        "minor restructure (+1 word, reorder)",
    ),
    (
        "all api endpoints return json with data error meta envelope structure always",
        "all rest endpoints return json with data error meta envelope format always",
        "2 words changed (api→rest, structure→format)",
    ),
    (
        "authentication uses jwt tokens with short expiry and refresh cookie storage",
        "authentication relies on tokens with short expiry but different refresh mechanism",
        "significant rewording — ~55% overlap",
    ),
    (
        "use typescript strict mode for type safety across all packages",
        "use python type hints for safety checking in backend services",
        "different technology — ~20% overlap",
    ),
]

class TestNoveltyThresholdSensitivity:

    def test_measured_overlaps(self):
        print(f"\n{'='*60}")
        print("BENCHMARK 6 — Novelty threshold sensitivity")
        print(f"{'='*60}")
        print(f"\n  Measured overlap for each pair:")
        print(f"  {'Label':<45} {'Overlap':>8}")
        for orig, rewrite, label in THRESHOLD_PAIRS:
            ov = _overlap(orig, rewrite)
            print(f"  {label:<45} {ov*100:>7.1f}%")

    def test_threshold_sweep(self):
        thresholds = [0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90]
        print(f"\n  Threshold sweep — B=blocked (duplicate), P=passed (novel):")
        print(f"  {'Pair':<43}", end="")
        for t in thresholds: print(f"  {t:.2f}", end="")
        print()
        print(f"  {'-'*85}")
        for orig, rewrite, label in THRESHOLD_PAIRS:
            ov = _overlap(orig, rewrite)
            print(f"  {label[:43]:<43}", end="")
            for t in thresholds:
                print(f"    {'B' if ov > t else 'P'}", end="")
            print(f"  ({ov*100:.0f}%)")

        print(f"\n  At current threshold=0.70:")
        for orig, rewrite, label in THRESHOLD_PAIRS:
            ov = _overlap(orig, rewrite)
            blocked = ov > 0.70
            if ov > 0.85:
                verdict = "BLOCKED ✓" if blocked else "PASSED ✗ (false negative)"
            elif ov < 0.55:
                verdict = "PASSED ✓" if not blocked else "BLOCKED ✗ (false positive)"
            else:
                verdict = f"BLOCKED" if blocked else "PASSED"
            print(f"    {label:<45} {ov*100:.1f}%  → {verdict}")

    def test_comma_tokenisation_degrades_overlap(self):
        orig_commas   = "types are feat, fix, docs, refactor, chore, test"
        rewrite_clean = "types are feat fix docs refactor chore test"
        orig_clean    = "types are feat fix docs refactor chore test variant"

        ov_c = _overlap(orig_commas, rewrite_clean)
        ov_k = _overlap(orig_clean,  rewrite_clean)
        print(f"\n  Comma tokenisation degradation:")
        print(f"    With commas    vs no-commas rewrite: overlap={ov_c*100:.1f}%  "
              f"→ {'PASSES (false negative)' if ov_c <= 0.70 else 'BLOCKED'} at 0.70")
        print(f"    Without commas vs no-commas rewrite: overlap={ov_k*100:.1f}%  "
              f"→ {'BLOCKED (correct)' if ov_k > 0.70 else 'PASSES'} at 0.70")
        print(f"    Delta: {(ov_k - ov_c)*100:.1f}% — commas reduce measured overlap")
        assert ov_c < ov_k, "Commas must reduce measured overlap (confirms known limitation)"


# ═══════════════════════════════════════════════════════════════════════════════
# 7. DECISION LENGTH SENSITIVITY
# ═══════════════════════════════════════════════════════════════════════════════

SHORT_D  = [("use postgres",       "architecture"), ("strict typescript", "convention"),
            ("no raw sql",         "constraint"),   ("repository pattern", "pattern"),
            ("jwt auth",           "architecture")]

MEDIUM_D = [
    ("We use PostgreSQL as the primary database because ACID transactions are required for order processing", "architecture"),
    ("TypeScript strict mode is enabled globally to prevent type errors in production code",                  "convention"),
    ("No raw SQL queries outside repository classes to maintain clean data access boundaries",                "constraint"),
    ("Repository pattern wraps all database access so services never touch Prisma directly",                 "pattern"),
    ("JWT tokens with 15-minute expiry are used for authentication with httpOnly cookie refresh",            "architecture"),
]

LONG_D = [
    ("We evaluated PostgreSQL, MySQL, MongoDB, CockroachDB, and DynamoDB over six weeks before selecting "
     "PostgreSQL as our primary database. The deciding factors were: ACID transaction support required for "
     "multi-step order processing where partial failures must be rolled back atomically, existing deep team "
     "expertise with SQL reducing onboarding cost, lower operational complexity versus distributed NoSQL "
     "solutions, native JSONB support for semi-structured product metadata, and proven scalability to our "
     "projected 10M rows within 18 months without sharding.", "architecture"),
    ("TypeScript strict mode is enabled globally across all packages via the root tsconfig.json. This means "
     "strictNullChecks, noImplicitAny, strictFunctionTypes, and strictPropertyInitialization are all active. "
     "No package may override these settings in a local tsconfig. The decision was made after three production "
     "incidents caused by undefined access in JavaScript code that TypeScript strict mode would have caught "
     "at compile time. All existing any usages must be removed before a file is considered migrated.", "convention"),
]

class TestDecisionLengthSensitivity:

    @pytest.fixture(autouse=True)
    def _fresh(self, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "STORE_DIR", tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        self.repo = "/bench/length"

    def test_token_cost_by_length(self):
        print(f"\n{'='*60}")
        print("BENCHMARK 7 — Decision length sensitivity")
        print(f"{'='*60}")
        print(f"\n  Token cost by length:")
        print(f"  {'Category':<12} {'Avg words':>10} {'word×1.3':>10} {'char÷4':>8}")
        for label, decisions in [("short", SHORT_D), ("medium", MEDIUM_D), ("long", LONG_D)]:
            texts  = [d[0] for d in decisions]
            avg_w  = statistics.mean(len(t.split()) for t in texts)
            avg_tw = statistics.mean(_tokens_words(t) for t in texts)
            avg_tc = statistics.mean(_tokens_chars(t) for t in texts)
            print(f"  {label:<12} {avg_w:>10.1f} {avg_tw:>10.1f} {avg_tc:>8.1f}")

    def test_novelty_filter_on_short_decisions(self):
        for content, subtype in SHORT_D:
            store.update_decision(self.repo, content, SESSION, subtype)

        near = "use postgresql db"
        exact = "use postgres"
        stored_near,  _ = store.update_decision(self.repo, near,  SESSION, "architecture")
        stored_exact, _ = store.update_decision(self.repo, exact, SESSION, "architecture")

        ov_near  = _overlap("use postgres", near)
        ov_exact = _overlap("use postgres", exact)
        print(f"\n  Short decision novelty filter:")
        print(f"    'use postgres' vs 'use postgresql db': overlap={ov_near*100:.0f}%  "
              f"→ {'PASSED' if stored_near else 'BLOCKED'}")
        print(f"    'use postgres' vs 'use postgres' (exact): overlap={ov_exact*100:.0f}%  "
              f"→ {'PASSED' if stored_exact else 'BLOCKED'}")
        print(f"    Note: short decisions are sensitive to single-word changes")
        assert not stored_exact, "Exact duplicate must be blocked"

    def test_novelty_filter_on_long_decisions(self):
        for content, subtype in LONG_D:
            store.update_decision(self.repo, content, SESSION, subtype)

        near_dup = LONG_D[0][0].replace(
            "proven scalability to our projected 10M rows within 18 months without sharding.",
            "demonstrated scalability to our target 10M rows within 18 months."
        )
        ov = _overlap(LONG_D[0][0], near_dup)
        stored, _ = store.update_decision(self.repo, near_dup, SESSION, "architecture")
        print(f"\n  Long decision novelty filter (~80 words, minor conclusion change):")
        print(f"    Overlap: {ov*100:.1f}%  → {'BLOCKED ✓' if not stored else 'PASSED (false negative)'}")
        print(f"    Long decisions are more robust to minor rewording — more signal tokens")

    def test_session_start_tokens_by_length(self):
        print(f"\n  Session start tokens by decision length (all stored as convention/constraint):")
        print(f"  {'Category':<12} {'Rules preloaded':>16} {'Tokens injected':>16} {'Tokens/rule':>12}")
        for label, decisions in [("short", SHORT_D), ("medium", MEDIUM_D), ("long", LONG_D)]:
            repo = f"/bench/length-{label}"
            for content, _ in decisions:
                store.update_decision(repo, content, SESSION, "convention")
            result     = store.get_session_start_context(repo)
            ctx        = result["hookSpecificOutput"]["additionalContext"]
            tokens     = _tokens_words(ctx)
            preloaded  = [l for l in ctx.splitlines() if l.startswith("- [")]
            per_rule   = tokens // max(len(preloaded), 1)
            print(f"  {label:<12} {len(preloaded):>16} {tokens:>16} {per_rule:>12}")


# ═══════════════════════════════════════════════════════════════════════════════
# 8. CONCURRENT SESSION ISOLATION
# ═══════════════════════════════════════════════════════════════════════════════

class TestConcurrentSessionIsolation:

    def test_current_repo_race_condition(self, base_store_dir, tmp_path, monkeypatch):
        monkeypatch.setattr(store, "STORE_DIR", tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        current_file = tmp_path / ".current_repo"
        current_file.write_text("/init")

        repo_a, repo_b = "/projects/service-a", "/projects/service-b"
        def session(path: str):
            for _ in range(50):
                current_file.write_text(path)
                time.sleep(0)

        t1 = threading.Thread(target=session, args=(repo_a,))
        t2 = threading.Thread(target=session, args=(repo_b,))
        t1.start(); t2.start()
        t1.join();  t2.join()

        final = current_file.read_text().strip()
        valid = final in (repo_a, repo_b)

        print(f"\n{'='*60}")
        print("BENCHMARK 8 — Concurrent session isolation")
        print(f"{'='*60}")
        print(f"\n  .current_repo race (2 threads × 50 writes each):")
        print(f"    Final value:   {final!r}")
        print(f"    Corrupt:       {'no ✓' if valid else 'YES ✗'}")
        print(f"    Risk:          stale context (wrong repo), not corrupted data")
        print(f"    Severity:      low — anchor hook corrects it on the next prompt")
        assert valid, f"File corrupted: {final!r}"

    def test_store_file_integrity_under_concurrent_writes(self, tmp_path, monkeypatch):
        # INVARIANT: _save is atomic (temp file + os.replace), so no matter how
        # writers interleave, the store file on disk is always complete valid JSON.
        # Lost updates (last-write-wins) can still happen — that needs locking and
        # is reported informationally below — but a torn/corrupt file cannot.
        monkeypatch.setattr(store, "STORE_DIR", tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        repo = "/bench/concurrent"
        errors: list[str] = []

        def writer(tid: int):
            for i in range(10):
                try:
                    store.update_decision(
                        repo,
                        f"Thread {tid} novel decision {i:02d}: observability strategy "
                        f"with distributed tracing spans for microservice {tid*10+i}",
                        f"s-{tid}", "architecture"
                    )
                except Exception as e:
                    errors.append(str(e))

        threads = [threading.Thread(target=writer, args=(i,)) for i in range(3)]
        for t in threads: t.start()
        for t in threads: t.join()

        store_file = tmp_path / f"{store._slug(repo)}.json"
        data = json.loads(store_file.read_text())  # must parse — atomic save invariant
        count = len(data["entries"])

        print(f"\n  Concurrent store writes (3 threads × 10 writes = 30 attempted):")
        print(f"    File valid JSON post-writes: yes ✓ (atomic temp-file + os.replace)")
        print(f"    Entries stored:              {count}/30  (gap = novelty filter rejecting "
              f"near-identical payloads + last-write-wins races; locking would fix only the latter)")
        print(f"    Writer exceptions:           {len(errors)}")
        assert not errors, f"Writers raised: {errors}"
        assert count >= 1, "At least the final writer's entries must survive"
        assert all(e["type"] == "decision" for e in data["entries"])
        leftover_tmps = list(tmp_path.glob("*.tmp"))
        assert not leftover_tmps, f"Temp files leaked: {leftover_tmps}"

    def test_reader_never_sees_torn_json_during_writes(self, tmp_path, monkeypatch):
        # INVARIANT: while writers continuously rewrite the store, every read of the
        # file parses as valid JSON — readers see old-or-new content, never partial.
        # With the previous non-atomic write_text() this flaked; os.replace makes it law.
        monkeypatch.setattr(store, "STORE_DIR", tmp_path)
        tmp_path.mkdir(parents=True, exist_ok=True)
        repo = "/bench/torn-read"
        # Large payload so a non-atomic write would have a wide torn window.
        big = {"repo_path": repo,
               "entries": [{"id": str(n), "type": "decision", "subtype": "architecture",
                            "content": "x" * 500, "session_id": "s", "timestamp": "t"}
                           for n in range(120)]}
        store._save(repo, big)
        store_file = tmp_path / f"{store._slug(repo)}.json"
        stop = threading.Event()
        torn: list[str] = []

        def writer():
            while not stop.is_set():
                store._save(repo, big)

        def reader():
            while not stop.is_set():
                try:
                    json.loads(store_file.read_text())
                except (json.JSONDecodeError, OSError) as e:
                    torn.append(str(e))

        workers = [threading.Thread(target=writer) for _ in range(2)] + \
                  [threading.Thread(target=reader) for _ in range(2)]
        for t in workers: t.start()
        time.sleep(0.5)
        stop.set()
        for t in workers: t.join()

        print(f"\n  Torn-read hammer (2 writers vs 2 readers, 0.5s):")
        print(f"    Torn/failed reads: {len(torn)}")
        assert not torn, f"Reader saw torn JSON: {torn[:3]}"
