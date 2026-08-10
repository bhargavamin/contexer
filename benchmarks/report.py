"""Render the A/B/C table, chain curves, and telemetry agreement from runs.jsonl.

Conditions are detected from the data (stable order: without, claudemd, with;
unknown names appended). The main table gets one column per condition plus
delta columns for the two comparisons that matter: with vs without and
with vs claudemd. A two-condition file renders with just the with-vs-without
delta, so old artifacts keep working.
"""
import json
import statistics
import sys
from pathlib import Path

METRICS = ["tokens_total", "cost_usd", "turns", "tool_calls", "duration_ms",
           "violations", "rationale", "success"]
_CONDITION_ORDER = ("without", "agentsmd", "claudemd", "claudemd_agentsmd",
                    "with", "claudemd_with")
_COMPARISONS = (("with", "without"), ("with", "claudemd"),
                ("claudemd_with", "claudemd"), ("agentsmd", "claudemd"),
                ("claudemd_agentsmd", "claudemd"))
_RATIONALE_NOTE = ("_Note: rationale 0.0 can mean the information was unavailable "
                   "to that condition, not model failure — see per-condition design._")

_MEMORY_ARM_ORDER = ("without", "memory", "with")
_MEMORY_TIERS = ("implicit", "explicit")
_HEADLINE_TASKS = ("sup-current", "cont-log")


def wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * ((p * (1 - p) / n + z * z / (4 * n * n)) ** 0.5) / denom
    return (max(0.0, round(centre - half, 4)), min(1.0, round(centre + half, 4)))


def _wilson_cell(cell_rows: list) -> str:
    k = sum(1 for r in cell_rows if r["success"])
    n = len(cell_rows)
    lo, hi = wilson_interval(k, n)
    return f"{k}/{n} ({lo:.2f}-{hi:.2f})"


def _memory_arms_present(rows):
    present = {r["arm"] for r in rows}
    return [a for a in _MEMORY_ARM_ORDER if a in present] + sorted(present - set(_MEMORY_ARM_ORDER))


def _render_memory_campaign(rows: list) -> str:
    """Renders the memory-tool-vs-Contexer campaign section: per-headline-task
    wilson-interval tables, a post-teaching capture-rate table, a cost-of-capture
    line per arm, enf-commit narrated separately (never in an aggregate table),
    and a simple rat-mem summary line."""
    models = {r["model"] for r in rows if r.get("model")}
    if len(models) > 1:
        raise ValueError(f"Mixed models in campaign: {sorted(models)} — refusing to aggregate.")
    arms = _memory_arms_present(rows)
    measure = [r for r in rows if r.get("phase") == "measure"]
    teach = [r for r in rows if r.get("phase") == "teach"]
    errored = [r for r in rows if r.get("error")]

    lines = [f"# Memory-vs-Contexer Benchmark — model {next(iter(models), 'unknown')}, "
             f"{len(rows)} rows ({', '.join(arms)})", ""]
    if errored:
        lines += [f"_{len(errored)} errored run(s) excluded from success-rate cells "
                  f"(see runs.jsonl)._", ""]

    for task_id in _HEADLINE_TASKS:
        task_rows = [r for r in measure if r["task_id"] == task_id]
        if not task_rows:
            continue
        lines += ["", f"## {task_id}", "",
                  "| arm | " + " | ".join(_MEMORY_TIERS) + " |",
                  "|" + "---|" * (1 + len(_MEMORY_TIERS))]
        for arm in arms:
            cells = [_wilson_cell([r for r in task_rows if r["arm"] == arm
                                   and r["tier"] == tier and not r.get("error")])
                     for tier in _MEMORY_TIERS]
            lines.append(f"| {arm} | " + " | ".join(cells) + " |")

    # Capture rate: every measure row in a rep shares the same restored,
    # post-teaching snapshot (captured once after teaching, before the
    # per-task restore loop) — the first measure row per (arm, rep) is a
    # deterministic pick of that snapshot.
    first_measure = {}
    for r in measure:
        key = (r["arm"], r["rep"])
        if key not in first_measure:
            first_measure[key] = r
    groups: dict = {}
    for r in first_measure.values():
        groups.setdefault((r["arm"], r["tier"]), []).append(r)
    if groups:
        lines += ["", "## Capture rate (post-teaching)", "",
                  "| arm | tier | n | median memory_files | median contexer_entries |",
                  "|---|---|---|---|---|"]
        for arm in arms:
            for tier in _MEMORY_TIERS:
                grp = groups.get((arm, tier), [])
                if not grp:
                    continue
                mf = statistics.median(r["capture"].get("memory_files", 0) for r in grp)
                ce = statistics.median(r["capture"].get("contexer_entries", 0) for r in grp)
                lines.append(f"| {arm} | {tier} | {len(grp)} | {mf:g} | {ce:g} |")

    teach_arms = [a for a in arms if [r for r in teach if r["arm"] == a]]
    if teach_arms:
        lines += ["", "## Cost of capture (teach-phase tokens)", ""]
        for arm in teach_arms:
            arm_teach = [r for r in teach if r["arm"] == arm]
            med = statistics.median(r["tokens_total"] for r in arm_teach)
            lines.append(f"- {arm}: median {med:g} tokens across {len(arm_teach)} "
                        f"teach session(s)")

    enf_rows = [r for r in measure if r["task_id"] == "enf-commit"]
    if enf_rows:
        lines += ["", "## Mechanism demonstration (enf-commit)", ""]
        for r in enf_rows:
            if r["arm"] == "with":
                if r.get("error"):
                    outcome = f"error: {r['error']}"
                else:
                    outcome = "blocked" if r["success"] else "not blocked"
            else:
                outcome = "no mechanism"
            lines.append(f"- {r['arm']} ({r['tier']}, rep {r['rep']}): {outcome}")

    rat_rows = [r for r in measure if r["task_id"] == "rat-mem"]
    if rat_rows:
        lines += ["", "## rat-mem", ""]
        for arm in arms:
            arm_rows = [r for r in rat_rows if r["arm"] == arm and not r.get("error")]
            if not arm_rows:
                continue
            med_tok = statistics.median(r["tokens_total"] for r in arm_rows)
            k = sum(1 for r in arm_rows if r["success"])
            lines.append(f"- {arm}: median {med_tok:g} tokens, success {k}/{len(arm_rows)}")

    return "\n".join(lines)


def _v(row, metric):
    if metric == "success":
        return 1.0 if row["success"] else 0.0
    return float(row[metric])


def _fmt(v: float) -> str:
    return f"{v:.3f}" if v % 1 else f"{v:g}"


def _median(rows, metric):
    return statistics.median(_v(r, metric) for r in rows) if rows else 0.0


def _conditions_present(rows):
    present = {r["condition"] for r in rows}
    ordered = [c for c in _CONDITION_ORDER if c in present]
    return ordered + sorted(present - set(_CONDITION_ORDER))


def render(runs_path: Path) -> str:
    rows = [json.loads(line) for line in runs_path.read_text().splitlines() if line.strip()]
    if any("arm" in r for r in rows):
        return _render_memory_campaign(rows)
    models = {r["model"] for r in rows if r.get("model")}
    if len(models) > 1:
        raise ValueError(f"Mixed models in campaign: {sorted(models)} — refusing to aggregate.")
    errored = [r for r in rows if r.get("error")]
    ok = [r for r in rows if not r.get("error")]
    conds = _conditions_present(ok)
    by = {c: [r for r in ok if r["condition"] == c] for c in conds}
    comparisons = [(a, b) for a, b in _COMPARISONS if a in by and b in by]
    checked = [r for r in ok if r["telemetry_ok"] is not None]
    agree = sum(1 for r in checked if r["telemetry_ok"])

    counts = " / ".join(f"{len(by[c])} {c}" for c in conds)
    lines = [f"# Contexer A/B Benchmark — model {next(iter(models), 'unknown')}, "
             f"{len(ok)} runs ({counts})", ""]
    if errored:
        lines += [f"_{len(errored)} errored run(s) excluded (see runs.jsonl)._", ""]
    if checked:
        lines += [f"telemetry agreement: {agree}/{len(checked)} checked rows within 5%", ""]
    head = "| metric | " + " | ".join(conds)
    for a, b in comparisons:
        head += f" | Δ ({a}−{b}) | Δ% ({a}−{b})"
    ncols = 1 + len(conds) + 2 * len(comparisons)
    lines += [head + " |", "|" + "---|" * ncols]
    for m in METRICS:
        meds = {c: _median(by[c], m) for c in conds}
        cells = [_fmt(meds[c]) for c in conds]
        for a, b in comparisons:
            pct = f"{(meds[a] - meds[b]) / meds[b] * 100:+.1f}%" if meds[b] else "n/a"
            cells += [_fmt(meds[a] - meds[b]), pct]
        lines.append(f"| {m} | " + " | ".join(cells) + " |")

    # Honest-ignorance footnote: a 0.0 rationale median in some condition can mean
    # the information never existed in that condition (per-condition design), not
    # that the model failed. Presentation only — the scorer is untouched.
    rat = [r for r in ok if r.get("kind") == "rationale"]
    if rat and any(_median([r for r in rat if r["condition"] == c], "rationale") == 0.0
                   for c in conds if any(r["condition"] == c for r in rat)):
        lines += ["", _RATIONALE_NOTE]

    chains = sorted({r["chain"] for r in ok if r["chain"]})
    for chain in chains:
        cr = [r for r in ok if r["chain"] == chain]
        chead = ("| step | "
                 + " | ".join(f"{c} tokens | {c} violations" for c in conds) + " |")
        lines += ["", f"## Chain: {chain}", "", chead,
                  "|" + "---|" * (1 + 2 * len(conds))]
        for step in sorted({r["step"] for r in cr}):
            cells = []
            for c in conds:
                sr = [r for r in cr if r["step"] == step and r["condition"] == c]
                cells += [_fmt(_median(sr, "tokens_total")), _fmt(_median(sr, "violations"))]
            lines.append(f"| {step} | " + " | ".join(cells) + " |")
    return "\n".join(lines)


if __name__ == "__main__":
    print(render(Path(sys.argv[1])))
