"""Applicability benchmark: the real decisions_for_files engine vs. 24-PR ground truth.

Runs contexer's actual mechanical applicability code (guard_engine.decisions_for_files)
against each benchmark PR's changed files, with the decision store reconstructed to its
at-PR-time state, and scores the result against the frozen ground truth in
ground_truth.json (built from the 2026-08-27 falsification test — see
docs/internal/falsification-test-2026-08-27.md §11 for why this harness exists).

Modes:
    uv run python benchmarks/applicability/run.py            # baseline (engine as-is)
    uv run python benchmarks/applicability/run.py --fix1     # + commit_window temporal-authority filter (SHIPPED)
    uv run python benchmarks/applicability/run.py --fix2     # + title-token diff gate (EXPERIMENTAL — rejected v1)
    uv run python benchmarks/applicability/run.py --fixed    # both, --explain names every dropped GT hit

Private corpora may stay in another checkout: pass an absolute `--gt` and, with
`--rank-meta`, an absolute `--rank-meta-file`. The corpus-integrity floor follows
the ground-truth basename, not the checkout that holds it.

Store reconstruction (approximations disclosed, per honest-measurement):
- An entry participates if its earliest timestamp (entry.timestamp or first
  revision created_at) <= the PR's merge time.
- Its content/title are rewound to the latest revision created at or before merge
  time, so a later revision never leaks into a historical run.
- `status` is NOT rewound (status flips are not timestamped in the schema); the
  current status is used, and every GT decision that is currently `ignored` is
  reported by name — production decisions_for_files can never return those, so
  they are a standing recall ceiling, not noise.
- The global store participates (time-filtered the same way), matching production.
"""
from __future__ import annotations

import argparse
import copy
import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from contexer import guard_engine, retrieval  # noqa: E402
from contexer.guard_engine import _parse_iso  # noqa: E402  # offset-aware, shared with fix 1

HERE = Path(__file__).resolve().parent
STORE = {
    "contexer": Path.home() / ".contexer/Users_bhargavamin_repos_personal_contexer-8596da46.json",
    "contexer-teams": Path.home() / ".contexer/Users_bhargavamin_repos_personal_contexer-teams-46841f8e.json",
}
GLOBAL_STORE = Path.home() / ".contexer/_global.json"
REPO_PATH = {
    "contexer": Path.home() / "repos/personal/contexer",
    "contexer-teams": Path.home() / "repos/personal/contexer-teams",
}


def _input_path(value: str) -> Path:
    """Resolve private benchmark inputs without requiring them in this worktree."""
    path = Path(value).expanduser()
    return path if path.is_absolute() else HERE / path


def _corpus_floor(path: Path) -> int:
    """Keep corpus-integrity floors stable when the input is outside this worktree."""
    return 20 if path.name == "ground_truth.json" else 10


def _entry_birth(entry: dict) -> str:
    revs = entry.get("revisions") or []
    candidates = [entry.get("timestamp") or ""] + [r.get("created_at") or "" for r in revs]
    candidates = [c for c in candidates if c]
    return min(candidates) if candidates else ""


def rewind_entries(entries: list[dict], cutoff: str) -> list[dict]:
    """Entries as they stood at `cutoff` (ISO): later entries dropped, revisions rewound.
    Comparisons are datetime-parsed, never string — git stamps carry local offsets
    while store stamps are +00:00, and string order lies across offsets."""
    cut = _parse_iso(cutoff)
    assert cut is not None, f"unparseable cutoff {cutoff!r} — refusing to rewind"

    def at_or_before(ts: str) -> bool:
        parsed = _parse_iso(ts)
        return parsed is not None and parsed <= cut

    out = []
    for entry in entries:
        birth = _entry_birth(entry)
        if not birth or not at_or_before(birth):
            continue
        e = copy.deepcopy(entry)
        revs = [r for r in (e.get("revisions") or []) if at_or_before(r.get("created_at") or "")]
        if revs:
            head = revs[-1]
            e["revisions"] = revs
            e["current_revision_id"] = head.get("revision_id")
            e["content"] = head.get("content", e.get("content", ""))
            if head.get("title"):
                e["title"] = head["title"]
        out.append(e)
    return out


def diff_text(repo: str, base: str, head: str) -> str:
    r = subprocess.run(["git", "-C", str(REPO_PATH[repo]), "diff", base, head],
                       capture_output=True, text=True, timeout=60)
    return r.stdout


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--fix1", action="store_true", help="temporal-authority filter (prior only)")
    ap.add_argument("--fix2", action="store_true", help="diff term-gate filter (governs tier only)")
    ap.add_argument("--fixed", action="store_true", help="both fixes")
    ap.add_argument("--rank", action="store_true",
                    help="score guard_engine.rank_applicable's tiers (strong P/R + union recall)")
    ap.add_argument("--rank-meta", action="store_true",
                    help="with --rank: prepend the PR title+description (pr_meta.json) to the query")
    ap.add_argument("--rank-meta-file", default="pr_meta.json",
                    help="PR metadata path (absolute paths support clean worktrees)")
    ap.add_argument("--explain", action="store_true", help="name every dropped/missed GT decision")
    ap.add_argument("--gt", default="ground_truth.json",
                    help="ground-truth file (ground_truth_holdout.json = the held-out set)")
    args = ap.parse_args()
    fix1 = args.fix1 or args.fixed
    fix2 = args.fix2 or args.fixed

    gt_path = _input_path(args.gt)
    bench = json.loads(gt_path.read_text())
    prs = bench["prs"]
    # The frozen corpus holds 24 PRs, the held-out set 12 — floors sized to each.
    min_prs = _corpus_floor(gt_path)
    assert len(prs) >= min_prs, f"corpus shrank to {len(prs)} PRs (floor {min_prs}) — refusing to report rates"

    stores = {name: json.loads(p.read_text())["entries"] for name, p in STORE.items()}
    global_entries = json.loads(GLOBAL_STORE.read_text())["entries"]

    tp = fp = fn = 0
    rows = []
    ignored_gt: list[str] = []
    nonprior_gt: list[str] = []
    drops: list[str] = []
    r_tp = r_pred = 0          # --rank: strong tier
    u_tp = 0                   # --rank: strong+candidates union recall numerator
    rank_rows = []

    rank_meta = (
        json.loads(_input_path(args.rank_meta_file).read_text())
        if args.rank and args.rank_meta
        else {}
    )

    for pr in prs:
        repo, num = pr["repo"], pr["num"]
        cutoff = pr["window_end"]
        window = (pr["window_start"], pr["window_end"])
        decisions = rewind_entries(stores[repo], cutoff) + rewind_entries(global_entries, cutoff)

        kwargs: dict = {}
        if fix1:
            kwargs["commit_window"] = window

        hits = guard_engine.decisions_for_files(
            str(REPO_PATH[repo]), pr["files"], decisions=decisions, **kwargs)
        if fix2:
            # EXPERIMENTAL, and REJECTED by this benchmark's own first run
            # (see docstring / report): >=2 title-token overlap with the diff.
            # Kept here so the rejection stays reproducible, never in production.
            change_tokens = set(retrieval.index_tokens(
                diff_text(repo, pr["base_sha"], pr["head_sha"])))
            for h in hits:
                title_tokens = set(retrieval.index_tokens(h["title"]))
                h["tier"] = ("governs" if len(title_tokens & change_tokens) >= 2
                             else "candidate")
        kept = [h for h in hits
                if h.get("authority", "prior") == "prior"
                and h.get("tier", "governs") == "governs"]
        predicted = {h["decision_id"] for h in kept}

        # GT bucketed by the SAME objective timestamps both modes see: the governs
        # denominator is only PRIOR-authority GT decisions — a decision captured
        # during or after the PR cannot have governed it (the report's own
        # construct), and the 24-PR GT lists conflated "discussed" with
        # "governing", which punished fix 1 for doing exactly what the ground
        # truth's reasoning concluded. Bucketing is timestamp-derived, never
        # hand-edited, and identical for baseline and fixed runs.
        gt_prior: set[str] = set()
        for d in pr["gt_applicable"]:
            entry = next((e for e in stores[repo] + global_entries
                          if e.get("id") == d["decision_id"]), None)
            if entry is None:
                continue
            tag = f"{repo}#{num}:{d['decision_id'][:8]}"
            if entry.get("status") == "ignored":
                ignored_gt.append(tag)
            authority = guard_engine.temporal_authority(entry, *window)
            if authority == "prior":
                gt_prior.add(d["decision_id"])
            else:
                nonprior_gt.append(f"{tag}({authority})")

        pr_tp = len(predicted & gt_prior)
        pr_fp = len(predicted - gt_prior)
        pr_fn = len(gt_prior - predicted)
        if args.explain:
            for h in hits:
                if h["decision_id"] in gt_prior and h["decision_id"] not in predicted:
                    drops.append(f"{repo}#{num}:{h['decision_id'][:8]} dropped by "
                                 f"authority={h.get('authority', 'prior')}/"
                                 f"tier={h.get('tier', 'governs')} | {h['title'][:60]}")
        tp += pr_tp
        fp += pr_fp
        fn += pr_fn
        rows.append((f"{repo}#{num}", pr_tp, pr_fp, pr_fn, len(gt_prior)))

        if args.rank:
            change = diff_text(repo, pr["base_sha"], pr["head_sha"])
            if args.rank_meta:
                m = rank_meta.get(f"{repo}#{num}", {})
                change = f"{m.get('title', '')}\n{m.get('body', '')}\n{change}"
            tiers = guard_engine.rank_applicable(
                str(REPO_PATH[repo]), pr["files"], change,
                decisions=decisions, commit_window=window)
            strong = {h["decision_id"] for h in tiers["strong"]}
            union = strong | {h["decision_id"] for h in tiers["candidates"]}
            r_tp += len(strong & gt_prior)
            r_pred += len(strong)
            u_tp += len(union & gt_prior)
            rank_rows.append((f"{repo}#{num}", len(strong & gt_prior), len(strong),
                              len(union & gt_prior), len(union), len(gt_prior)))

    assert (tp + fp) > 0 or (tp + fn) > 0, "engine returned nothing and GT is empty — harness is broken"
    gt_total = tp + fn
    assert gt_total >= 30, f"prior-GT denominator {gt_total} below floor 30 — corpus damaged"

    mode = (("fix1+fix2" if fix1 and fix2 else "fix1" if fix1 else "fix2")
            if (fix1 or fix2) else "BASELINE (engine as-is)")
    print(f"\n== applicability benchmark — {mode} ==")
    print("   (GT denominator = prior-authority decisions only; concurrent/retroactive"
          " GT reported below, never scored)")
    print(f"{'PR':<22}{'tp':>4}{'fp':>4}{'fn':>4}{'gt':>4}")
    for name, a, b, c, d in rows:
        print(f"{name:<22}{a:>4}{b:>4}{c:>4}{d:>4}")
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / gt_total
    print(f"\nmicro precision: {tp}/{tp + fp} = {precision:.2%}")
    print(f"micro recall:    {tp}/{gt_total} = {recall:.2%}")
    if nonprior_gt:
        print(f"\nGT decisions excluded as non-prior (self-captures/retroactive — cannot govern): "
              f"{sorted(set(nonprior_gt))}")
    if ignored_gt:
        print(f"GT decisions currently status=ignored (unreachable by production engine, "
              f"a standing recall ceiling): {sorted(set(ignored_gt))}")
    unver = [(p['repo'], p['num'], p['gt_unverifiable_ids']) for p in prs if p['gt_unverifiable_ids']]
    if unver:
        print(f"GT refs outside any store (capture gaps, excluded from denominator): {unver}")
    if args.explain and drops:
        print("\ndropped prior-GT hits (fix cost, by name):")
        for d in drops:
            print(f"  {d}")
    if args.rank:
        print(f"\n== rank_applicable tiers (strong={guard_engine._RANK_STRONG}, "
              f"bm25 pool={guard_engine._RANK_BM25_POOL}) ==")
        print("   (the strong tier is ALWAYS prior-filtered via commit_window, "
              "with or without --fix1 — unlike the table above)")
        print(f"{'PR':<22}{'s_tp':>5}{'s_n':>4}{'u_tp':>5}{'u_n':>4}{'gt':>4}")
        for name, stp, sn, utp, un, g in rank_rows:
            print(f"{name:<22}{stp:>5}{sn:>4}{utp:>5}{un:>4}{g:>4}")
        assert r_pred > 0, "--rank produced zero strong predictions — lane is broken"
        print(f"\nstrong precision: {r_tp}/{r_pred} = {r_tp/r_pred:.2%}")
        print(f"strong recall:    {r_tp}/{gt_total} = {r_tp/gt_total:.2%}")
        print(f"union recall (strong+candidates, the never-discarded pool): "
              f"{u_tp}/{gt_total} = {u_tp/gt_total:.2%}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
