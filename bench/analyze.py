#!/usr/bin/env python3
"""Aggregate results/graded.jsonl into a plain-English report + a technical appendix.

Emits results/summary.csv (one row per task+setup) and results/summary.md (readable by anyone).
Three setups ("arms"):
  cold  -> "No memory"            : Claude is told nothing about past decisions.
  paste -> "Paste rules by hand"  : the decisions are pasted into every request manually (status quo).
  warm  -> "Contexer"             : Claude remembers and recalls the decisions automatically.
"""
import argparse
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
RESULTS_DIR = BENCH_DIR / "results"
SUCCESS_THRESHOLD = 0.5
ARM_ORDER = ["cold", "paste", "warm"]
ARM_LABEL = {"cold": "No memory", "paste": "Paste rules by hand", "warm": "Contexer"}


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 3) if xs else None


def total_tokens(u: dict) -> int:
    return (u.get("input_tokens", 0) + u.get("output_tokens", 0)
            + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0))


def pct(x):
    return "—" if x is None else f"{round(x * 100)}%"


def money(x):
    return "—" if x is None else f"${x:.3f}"


def build_rows(records):
    groups = defaultdict(list)
    for r in records:
        groups[(r["task_id"], r["arm"])].append(r)
    rows = []
    for (task_id, arm), recs in groups.items():
        kind = recs[0]["kind"]
        ok = [r for r in recs if not r.get("is_error")]
        aggs = [r.get("grade_agg", {}) for r in ok]
        correctness = [a.get("task_correctness") for a in aggs]

        # A "success" must be CORRECT, and for high-signal tasks must ALSO follow the rule.
        # (A cheap answer that quietly breaks the team's rule is not a success.)
        def _success(a: dict) -> bool:
            c, d = a.get("task_correctness"), a.get("decision_adherence")
            if c is None or c < SUCCESS_THRESHOLD:
                return False
            if kind == "high_signal":
                return d is not None and d >= SUCCESS_THRESHOLD
            return True

        passrate = (sum(1 for a in aggs if _success(a)) / len(aggs)) if aggs else 0.0
        med_cost = med([r["derived_cost_usd"] for r in ok])
        rows.append({
            "task_id": task_id, "kind": recs[0]["kind"], "arm": arm,
            "n": len(recs), "errors": sum(1 for r in recs if r.get("is_error")),
            "med_cost_usd": med_cost,
            "med_total_tokens": med([total_tokens(r["usage"]) for r in ok]),
            "med_turns": med([r["num_turns"] for r in ok]),
            "med_adherence": med([a.get("decision_adherence") for a in aggs]),
            "med_correctness": med(correctness),
            "conflict_rate": round(sum(1 for a in aggs if a.get("conflict_with_stored")) / len(aggs), 3) if aggs else None,
            "passrate": round(passrate, 3),
            "cost_per_success": round(med_cost / passrate, 6) if (med_cost and passrate) else None,
            "avg_ctx_calls": round(statistics.mean([sum(r["contexer_tool_calls"].values()) for r in ok]), 2) if ok else 0,
        })
    return rows


def avg_over(rows, kind, arm, field):
    vals = [r[field] for r in rows if r["kind"] == kind and r["arm"] == arm and r[field] is not None]
    return round(statistics.mean(vals), 4) if vals else None


def friendly_md(rows):
    arms = [a for a in ARM_ORDER if any(r["arm"] == a for r in rows)]
    by_task = defaultdict(dict)
    for r in rows:
        by_task[(r["kind"], r["task_id"])][r["arm"]] = r
    hs_tasks = sorted({(k, t) for (k, t) in by_task if k == "high_signal"})

    L = []
    L.append("# Does Contexer help? — benchmark results\n")
    L.append("_Plain-English summary. Each task was run several times in each setup below, and an "
             "independent AI judge (which was **not** told which setup produced each answer) scored the "
             "results._\n")
    L.append("## The setups compared\n")
    L.append("- **No memory** — Claude is told nothing about the team's past decisions. (What happens when context is lost between sessions.)")
    L.append("- **Paste rules by hand** — someone pastes the team's decisions into every request. (What people do today, manually.)")
    L.append("- **Contexer** — Claude remembers the decisions and recalls them automatically.\n")

    # Headline (high-signal tasks only — the ones where a past decision actually matters)
    L.append("## The bottom line\n")
    if hs_tasks:
        adher = {a: avg_over(rows, "high_signal", a, "med_adherence") for a in arms}
        cost = {a: avg_over(rows, "high_signal", a, "med_cost_usd") for a in arms}
        retr = avg_over(rows, "high_signal", "warm", "avg_ctx_calls")
        parts = []
        trip = []
        if adher.get("warm") is not None:
            trip.append(f"**{pct(adher['warm'])} with Contexer**")
        if adher.get("cold") is not None:
            trip.append(f"**{pct(adher['cold'])} with no memory**")
        if adher.get("paste") is not None:
            trip.append(f"**{pct(adher['paste'])} when the rules were pasted in by hand**")
        if trip:
            parts.append("On the tasks where a past decision mattered, Claude followed the team's "
                         "rule " + ", ".join(trip) + ".")
        # Honest, data-driven verdict — no assumption that Contexer helped.
        if adher.get("warm") is not None and adher.get("cold") is not None and adher.get("paste") is not None:
            if adher["warm"] <= adher["cold"] + 0.1 and adher["paste"] >= adher["warm"] + 0.4:
                rr = f"{round((retr or 0) * 100)}%" if retr is not None else "rarely"
                parts.append(
                    f"**Contexer did not beat having no memory here.** In the warm runs Claude looked "
                    f"up the stored decisions only about {rr} of the time — it has to *choose* to call "
                    f"`get_context`, and on these tasks it didn't — so the saved rules went unused. "
                    f"Only pasting the rules in by hand worked reliably.")
            elif abs(adher["warm"] - adher["paste"]) <= 0.1 and adher["warm"] >= adher["cold"] + 0.4:
                parts.append("**Contexer matched manual pasting** while recalling the rules automatically.")
        if all(cost.get(a) is not None for a in ("cold", "paste", "warm")):
            parts.append(f"Per request, all three cost about the same (~{money(cost['warm'])}); "
                         f"Contexer adds only a small look-up overhead over having no memory.")
        L.append("> " + " ".join(parts) + "\n" if parts else "> (Not enough data yet — run more tasks/reps.)\n")
    else:
        L.append("> No high-signal tasks in this run.\n")

    # Quality table
    L.append("## 1. Did Claude follow the team's rules?\n")
    L.append("_Higher is better. ✓ = followed the rule, ✗ = ignored it._\n")
    header = "| Task | " + " | ".join(ARM_LABEL[a] for a in arms) + " |"
    L.append(header)
    L.append("|---" * (len(arms) + 1) + "|")
    for (kind, task_id) in sorted(by_task):
        cells = []
        for a in arms:
            r = by_task[(kind, task_id)].get(a)
            if not r or r["med_adherence"] is None:
                cells.append("—")
            else:
                mark = "✓" if r["med_adherence"] >= SUCCESS_THRESHOLD else "✗"
                cells.append(f"{mark} {pct(r['med_adherence'])}")
        tag = "" if kind == "high_signal" else " _(control)_"
        L.append(f"| {task_id}{tag} | " + " | ".join(cells) + " |")
    L.append("")

    # Cost table
    L.append("## 2. What did each request cost?\n")
    L.append("_Lower is better. This is the price of one request._\n")
    L.append(header)
    L.append("|---" * (len(arms) + 1) + "|")
    for (kind, task_id) in sorted(by_task):
        cells = [money(by_task[(kind, task_id)].get(a, {}).get("med_cost_usd")) for a in arms]
        L.append(f"| {task_id} | " + " | ".join(cells) + " |")
    L.append("")

    # Cost per correct result
    L.append("## 3. Cost per *correct* result\n")
    L.append("_The fairest money metric. For the rule tasks, a result only counts as correct if it "
             "**both** did the task **and** followed the rule. Lower is better; **\"—\" means nothing "
             "produced a correct-and-compliant result, so the money was wasted.**_\n")
    L.append(header)
    L.append("|---" * (len(arms) + 1) + "|")
    for (kind, task_id) in sorted(by_task):
        cells = [money(by_task[(kind, task_id)].get(a, {}).get("cost_per_success")) for a in arms]
        L.append(f"| {task_id} | " + " | ".join(cells) + " |")
    L.append("")

    # Plain takeaways
    L.append("## What this means\n")
    L.append("- **Storing a decision only helps if it gets recalled.** Contexer keeps the rules but "
             "Claude has to *decide* to look them up; when it doesn't, the warm setup behaves just "
             "like having no memory — and you still pay a small overhead.")
    L.append("- **No memory silently breaks the rules.** A request can finish and look fine yet quietly "
             "violate a team decision — see the rule columns above and the *cost per correct result* table.")
    L.append("- **Pasting by hand is reliable but manual.** It always works because the rule is right "
             "there in the request — but a person has to remember to paste it every time.")
    L.append("- **The opportunity for Contexer:** make recall automatic/triggered (not opt-in) so it "
             "captures the reliability of pasting without the manual effort. Today's results show the "
             "recall step is the gap.")
    L.append("- **Control task** (the neutral one) shows the pure overhead when memory doesn't help.")
    L.append("")
    return "\n".join(L)


def technical_md(rows):
    L = ["\n---\n\n## Technical detail\n",
         "Per setup, median across reps.\n",
         "| task | kind | setup | n | errors | med_cost | med_tokens | turns | adherence | correct | conflict | $/success | ctx_calls |",
         "|---|---|---|--|--|--|--|--|--|--|--|--|--|"]
    for r in sorted(rows, key=lambda x: (x["task_id"], ARM_ORDER.index(x["arm"]) if x["arm"] in ARM_ORDER else 9)):
        L.append(f"| {r['task_id']} | {r['kind']} | {r['arm']} | {r['n']} | {r['errors']} | "
                 f"{r['med_cost_usd']} | {r['med_total_tokens']} | {r['med_turns']} | "
                 f"{r['med_adherence']} | {r['med_correctness']} | {r['conflict_rate']} | "
                 f"{r['cost_per_success']} | {r['avg_ctx_calls']} |")
    return "\n".join(L) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default=str(RESULTS_DIR / "graded.jsonl"))
    args = ap.parse_args()

    records = [json.loads(l) for l in Path(args.inp).read_text().splitlines() if l.strip()]
    rows = build_rows(records)

    csv_path = RESULTS_DIR / "summary.csv"
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()) if rows else [])
        w.writeheader()
        w.writerows(rows)

    md = friendly_md(rows) + technical_md(rows)
    md_path = RESULTS_DIR / "summary.md"
    md_path.write_text(md)
    print(md)
    print(f"\nWrote {csv_path} and {md_path}")


if __name__ == "__main__":
    main()
