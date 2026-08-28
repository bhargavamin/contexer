"""FP-population diagnosis + ranked-retrieval oracle for the applicability benchmark.

Two questions, both downstream of run.py's measured baseline (P 6.45% / R 35.56% in fix1 mode):

A) WHAT are the false positives? Concentration by decision, by matched file, by pairing
   reason - is the FP mass a hub-file phenomenon (few decisions x few files) or diffuse?

B) Does ranked semantic retrieval (BM25, the diff as query - improvement plan R1/P1)
   actually reach the missed GT decisions, and does it separate true hits from junk?
   This is validation experiment 1 of docs/internal/applicability-redteam-2026-08-28.md:
   run BEFORE building any product code.

Approximations disclosed (honest-measurement):
- The oracle index is built from each entry's title + rewound content via
  retrieval.index_tokens - NOT production _build_retrieval_index (which adds topics and
  double-weighted artifacts). Oracle results therefore LOWER-bound what the production
  index shape could do on term overlap, but are not the shipped ranking.
- status is not rewound (same disclosure as run.py); ignored entries are excluded from
  the oracle index exactly as production excludes them, and named in the output.
- The BM25 query is the full unified diff's tokens; no truncation, no artifact
  double-weighting. Repeated terms weigh more, which is bm25_rank's documented contract.
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from run import GLOBAL_STORE, HERE, REPO_PATH, STORE, diff_text, rewind_entries  # noqa: E402
from contexer import guard_engine  # noqa: E402
from contexer.retrieval import bm25_rank, index_tokens  # noqa: E402


def build_index(entries: list[dict]) -> dict:
    """Minimal BM25 index over non-ignored rewound entries (title + content)."""
    docs = {}
    for e in entries:
        if e.get("status") == "ignored" or not e.get("id"):
            continue
        toks = index_tokens((e.get("title") or "") + " " + (e.get("content") or ""))
        docs[e["id"]] = {"tf": dict(Counter(toks)), "len": len(toks)}
    df: Counter = Counter()
    for d in docs.values():
        df.update(d["tf"].keys())
    n = len(docs)
    avgdl = sum(d["len"] for d in docs.values()) / n if n else 0.0
    return {"docs": docs, "df": dict(df), "n_docs": n, "avgdl": avgdl}


def main() -> int:
    bench = json.loads((HERE / "ground_truth.json").read_text())
    prs = bench["prs"]
    assert len(prs) >= 20, f"corpus shrank to {len(prs)} PRs - refusing to report rates"

    stores = {name: json.loads(p.read_text())["entries"] for name, p in STORE.items()}
    global_entries = json.loads(GLOBAL_STORE.read_text())["entries"]

    # ---- accumulators -------------------------------------------------------
    fp_by_decision: Counter = Counter()
    fp_by_file: Counter = Counter()
    fp_by_reason: Counter = Counter()
    fp_meta: dict[str, dict] = {}
    total_fp = total_tp = 0

    gt_total = 0
    fn_rows: list[tuple[str, str, str, int | None, int]] = []  # (pr, id8, title, rank, n_docs)
    tp_rank_rows: list[int | None] = []
    k_hits = {1: 0, 3: 0, 5: 0, 10: 0}          # BM25-only recall@k numerators
    pk_pred = {3: 0}                            # BM25-only predictions at k=3
    pk_tp = {3: 0}
    hyb_pred = hyb_tp = 0                       # mechanical hits re-ranked by BM25, top-3
    ignored_gt: list[str] = []

    for pr in prs:
        repo, num = pr["repo"], pr["num"]
        window = (pr["window_start"], pr["window_end"])
        decisions = (rewind_entries(stores[repo], pr["window_end"])
                     + rewind_entries(global_entries, pr["window_end"]))

        hits = guard_engine.decisions_for_files(
            str(REPO_PATH[repo]), pr["files"], decisions=decisions, commit_window=window)
        kept = [h for h in hits if h.get("authority") == "prior"]
        predicted = {h["decision_id"] for h in kept}

        gt_prior: set[str] = set()
        for d in pr["gt_applicable"]:
            entry = next((e for e in stores[repo] + global_entries
                          if e.get("id") == d["decision_id"]), None)
            if entry is None:
                continue
            if entry.get("status") == "ignored":
                ignored_gt.append(f"{repo}#{num}:{d['decision_id'][:8]}")
            if guard_engine.temporal_authority(entry, *window) == "prior":
                gt_prior.add(d["decision_id"])
        gt_total += len(gt_prior)

        # ---- A: FP population ----------------------------------------------
        for h in kept:
            if h["decision_id"] in gt_prior:
                total_tp += 1
                continue
            total_fp += 1
            fp_by_decision[h["decision_id"]] += 1
            fp_by_reason["source_files" if "source_files" in h["reason"] else "artifact"] += 1
            for f in h.get("files_matched", []):
                fp_by_file[f] += 1
            fp_meta.setdefault(h["decision_id"], {
                "title": h.get("title", "")[:70], "reason": h["reason"]})

        # ---- B: ranked-retrieval oracle ------------------------------------
        index = build_index(decisions)
        assert index["n_docs"] > 0, f"{repo}#{num}: empty oracle index - harness broken"
        query = index_tokens(diff_text(repo, pr["base_sha"], pr["head_sha"]))
        ranked = bm25_rank(query, index)
        rank_of = {did: i + 1 for i, (did, _, _, _) in enumerate(ranked)}

        top3 = {did for did, _, _, _ in ranked[:3]}
        pk_pred[3] += len(top3)
        pk_tp[3] += len(top3 & gt_prior)
        for k in k_hits:
            topk = {did for did, _, _, _ in ranked[:k]}
            k_hits[k] += len(topk & gt_prior)

        mech_ranked = sorted(kept, key=lambda h: rank_of.get(h["decision_id"], 10**6))
        hyb3 = {h["decision_id"] for h in mech_ranked[:3]}
        hyb_pred += len(hyb3)
        hyb_tp += len(hyb3 & gt_prior)

        for did in gt_prior:
            r = rank_of.get(did)
            if did in predicted:
                tp_rank_rows.append(r)
            else:
                title = next((h["title"] for h in hits if h["decision_id"] == did), None)
                if title is None:
                    e = next((e for e in decisions if e.get("id") == did), {})
                    title = (e.get("title") or e.get("content") or "")[:60]
                fn_rows.append((f"{repo}#{num}", did[:8], title[:60], r, index["n_docs"]))

    assert gt_total >= 30, f"prior-GT denominator {gt_total} below floor 30 - corpus damaged"

    # ---- report A -----------------------------------------------------------
    print(f"== A. FP population (fix1 mode: {total_fp} FP / {total_tp} TP, "
          f"{len(fp_by_decision)} distinct FP decisions) ==")
    print(f"\nby pairing reason: {dict(fp_by_reason)}")
    print(f"\ntop offender decisions (FP count / share of {total_fp}):")
    running = 0
    for did, n in fp_by_decision.most_common(15):
        running += n
        m = fp_meta[did]
        print(f"  {n:>3}  ({running/total_fp:>5.1%} cum)  {did[:8]}  [{m['reason'][:28]}] {m['title']}")
    print("\ntop matched files (FP pairings attributed to that file):")
    for f, n in fp_by_file.most_common(10):
        print(f"  {n:>3}  {f}")

    # ---- report B -----------------------------------------------------------
    print("\n== B. ranked-retrieval oracle (BM25 over title+content, diff as query) ==")
    print(f"GT-prior denominator: {gt_total}; per-PR index sizes ~"
          f"{sorted({r[4] for r in fn_rows}) if fn_rows else 'n/a'} docs")
    for k in sorted(k_hits):
        print(f"  BM25-only recall@{k:<2}: {k_hits[k]}/{gt_total} = {k_hits[k]/gt_total:.2%}")
    print(f"  BM25-only precision@3: {pk_tp[3]}/{pk_pred[3]} = {pk_tp[3]/pk_pred[3]:.2%}")
    print(f"  hybrid (mechanical hits re-ranked by BM25, top-3): "
          f"P {hyb_tp}/{hyb_pred} = {hyb_tp/hyb_pred:.2%}, R {hyb_tp}/{gt_total} = {hyb_tp/gt_total:.2%}")

    print(f"\nevery mechanical FN, with its BM25 rank ({len(fn_rows)} total; "
          f"rank None = zero term overlap with the diff):")
    for prname, id8, title, r, n_docs in sorted(fn_rows, key=lambda x: (x[3] is None, x[3] or 0)):
        print(f"  rank {str(r):>4}/{n_docs:<3}  {prname:<20} {id8}  {title}")
    reached = {k: sum(1 for *_r, r, _ in fn_rows if r is not None and r <= k) for k in (3, 5, 10)}
    print(f"\nFNs reached by BM25: top3={reached[3]}, top5={reached[5]}, top10={reached[10]} of {len(fn_rows)}")
    if ignored_gt:
        print(f"GT currently status=ignored (excluded from oracle index, as in production): "
              f"{sorted(set(ignored_gt))}")
    if tp_rank_rows:
        shown = sorted((r if r is not None else -1) for r in tp_rank_rows)
        print(f"BM25 ranks of the mechanical TPs (would rank-and-cap keep them?): {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
