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
