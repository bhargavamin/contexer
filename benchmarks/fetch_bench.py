"""Deterministic fetch benchmark: CLAUDE.md-style whole-file context vs Contexer's
per-prompt router. No LLM in the loop — measures only the mechanical layer:

  latency  — wall-clock to produce the context for one prompt
  tokens   — context injected per prompt (chars/4, the repo's own convention)
  hit rate — for targeted prompts, does the injection actually contain the
             decision the prompt asks about

Run: uv run python benchmarks/fetch_bench.py

The claudemd condition reads the file once per prompt (worst case for latency,
which it still wins) and pays its full token count on EVERY prompt, because a
rules file rides in context for the whole session. The contexer condition calls
store.get_context_for_prompt per prompt. Whole-session outcome quality is out of
scope here — that's the (frozen, paid) memory campaign's job.
"""

import statistics
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

from contexer import store

# 9 topic families (vocabulary-diverse so BM25 has a fair target), from the
# extended benchmark's corpus, plus the prompt that targets each and the term
# that proves the right decision was fetched.
FAMILIES = [
    ("We use PostgreSQL as the primary database for ACID compliance and transactional reliability", "architecture",
     "why did we choose postgresql as the primary database?", "postgresql"),
    ("Authentication uses JWT access tokens with 15-minute expiry and httpOnly refresh cookies", "architecture",
     "why do we use jwt tokens for authentication?", "jwt"),
    ("All services communicate internally via gRPC and expose REST only at the API gateway", "architecture",
     "what is the reason services talk grpc internally?", "grpc"),
    ("Always use conventional commits: feat fix docs refactor chore test as allowed types", "convention",
     "which commit message convention do we follow and why?", "conventional"),
    ("TypeScript strict mode is enabled globally so any types are banned in production code", "convention",
     "why is typescript strict mode enabled globally?", "strict"),
    ("No raw SQL outside repository classes to maintain clean data access boundaries", "pattern",
     "what is the rationale for banning raw sql outside repositories?", "sql"),
    ("Circuit breaker wraps every external HTTP call to prevent cascade failures across services", "pattern",
     "why does every external http call need a circuit breaker?", "circuit"),
    ("Never commit untested code to main because CI blocks merge when coverage drops below the floor", "constraint",
     "what coverage rule was decided for merging to main?", "coverage"),
    ("PII must never appear in logs so emails phone numbers and card data are stripped before logging", "constraint",
     "why do we strip pii from logs?", "pii"),
]
# Prompts that should fetch nothing (silence is the correct, cheap answer).
MISS_PROMPTS = ["what time is the standup tomorrow?", "please rename the readme file"]

SERVICES = ["billing", "search", "ingest", "notify", "reports", "gateway", "scheduler",
            "profiles", "exports", "webhooks", "audit", "invoices", "catalog", "sessions"]

REPEATS = 30  # latency samples per prompt


def build_corpus(n: int) -> list[dict]:
    """n decisions cycling the 9 families, each variant scoped to a distinct service
    so entries stay textually distinct (like a real store, not n duplicates)."""
    out = []
    for i in range(n):
        content, subtype, _, _ = FAMILIES[i % len(FAMILIES)]
        if i >= len(FAMILIES):
            svc = SERVICES[(i // len(FAMILIES)) % len(SERVICES)]
            content = f"{content} (ratified for the {svc}-{i // len(FAMILIES)} service)"
        out.append({
            "id": str(uuid.uuid4()), "type": "decision", "subtype": subtype,
            "content": content, "session_id": "fetch-bench",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        })
    return out


def render_claudemd(entries: list[dict]) -> str:
    lines = ["# Project rules", ""]
    for e in entries:
        lines.append(f"- [{e['subtype']}] {e['content']}")
    return "\n".join(lines) + "\n"


def pstat(vals):
    s = sorted(vals)
    return s[len(s) // 2], s[int(len(s) * 0.95)]


def bench(n: int) -> dict:
    with TemporaryDirectory() as td:
        old = store.STORE_DIR
        store.STORE_DIR = Path(td)
        try:
            repo = "/bench/fetch"
            entries = build_corpus(n)
            data = store._load(repo)
            data["entries"] = entries
            store._migrate_entries(data)
            store._save(repo, data)  # writes the BM25 index sidecar

            md_path = Path(td) / "CLAUDE.md"
            md_text = render_claudemd(entries)
            md_path.write_text(md_text)
            md_tokens = len(md_text) // 4

            prompts = [(p, term) for _, _, p, term in FAMILIES] + [(p, None) for p in MISS_PROMPTS]

            md_lat, cx_lat, cx_tokens, hits, targeted = [], [], [], 0, 0
            for prompt, term in prompts:
                for _ in range(REPEATS):
                    t0 = time.perf_counter()
                    md_path.read_text()
                    md_lat.append((time.perf_counter() - t0) * 1000)

                    t0 = time.perf_counter()
                    ctx = store.get_context_for_prompt(repo, prompt, "")
                    cx_lat.append((time.perf_counter() - t0) * 1000)
                cx_tokens.append(len(ctx) // 4)
                if term is not None:
                    targeted += 1
                    hits += 1 if term in ctx.lower() else 0

            md_p50, md_p95 = pstat(md_lat)
            cx_p50, cx_p95 = pstat(cx_lat)
            return {
                "n": n,
                "md_tokens": md_tokens,
                "cx_tokens_median": int(statistics.median(cx_tokens)),
                "md_p50": md_p50, "md_p95": md_p95,
                "cx_p50": cx_p50, "cx_p95": cx_p95,
                "hit_rate": hits / targeted,
            }
        finally:
            store.STORE_DIR = old


def main():
    print(f"{'N':>4} | {'md tok/prompt':>13} | {'cx tok/prompt':>13} | {'ratio':>6} | "
          f"{'md p50/p95 ms':>14} | {'cx p50/p95 ms':>14} | {'cx hit rate':>11}")
    print("-" * 95)
    for n in (10, 50, 100, 500):
        r = bench(n)
        ratio = r["md_tokens"] / max(1, r["cx_tokens_median"])
        print(f"{r['n']:>4} | {r['md_tokens']:>13} | {r['cx_tokens_median']:>13} | {ratio:>5.1f}x | "
              f"{r['md_p50']:>6.3f}/{r['md_p95']:.3f} | {r['cx_p50']:>6.3f}/{r['cx_p95']:.3f} | "
              f"{r['hit_rate']:>10.0%}")
    print("\nNotes: md tok/prompt is the whole rules file (it rides in context every prompt).")
    print("cx tok/prompt is the median injection across 9 targeted + 2 no-match prompts.")
    print("Latency: md = one file read; cx = full router (gate + BM25 + render). No LLM involved;")
    print("model-side effects (attention, answer quality) are the paid campaign's scope, not this script's.")


if __name__ == "__main__":
    main()
