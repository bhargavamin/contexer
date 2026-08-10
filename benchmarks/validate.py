"""Independent verification pass over an A/B campaign.

A skeptical outsider should be able to run this and re-derive every headline
number straight from the raw ``runs.jsonl`` rows. It deliberately does NOT import
``report.py`` — the median / delta / pairing math is re-implemented here from
scratch so the two never share a bug. It fails loudly on integrity problems and
warns on statistical smells that don't prove tampering but deserve a human look.

    validate(campaign_dir) -> {"ok", "failures", "warnings", "recomputed"}
    render_validation(v)   -> markdown section
    python -m benchmarks.validate <campaign_dir>   # prints markdown, exit 1 if not ok
"""
import json
import statistics
import sys
from pathlib import Path

# The exact row contract produced by run.py::_one_run. Kept as a literal here (not
# imported) so this validator notices if the producer silently drops a column.
# ("ts" is deliberately NOT required: old artifacts predate it; its absence is
# reported by the interleaving check as a warning, never a schema failure.)
EXPECTED_KEYS = (
    "task_id", "kind", "chain", "step", "condition", "rep", "model",
    "tokens_in", "tokens_out", "tokens_cache_read", "tokens_cache_write",
    "tokens_total", "cost_usd", "turns", "duration_ms", "tool_calls",
    "violations", "rationale", "success", "result_snippet",
    "otel_tokens_total", "otel_cost_usd", "telemetry_ok", "error",
)
# Additional columns every memory-campaign row must carry (a row is one iff it has
# "arm"). Required, not warned about: _check_memory_isolation short-circuits on a
# missing "contaminated", so a producer refactor that dropped the field would make
# the campaign's central isolation check pass vacuously.
MEMORY_KEYS = ("arm", "tier", "phase", "contaminated", "capture", "sup_result")
TOKEN_PARTS = ("tokens_in", "tokens_out", "tokens_cache_read", "tokens_cache_write")
# Cost-like metrics: lower is better, so a paired "win" for the first arm of a
# pair means its value is strictly below the second arm's.
PAIRED_METRICS = ("tokens_total", "cost_usd", "turns", "tool_calls", "duration_ms")
MEDIAN_METRICS = ("tokens_total", "cost_usd", "turns", "tool_calls",
                  "duration_ms", "violations", "rationale", "success")
EDITING_KINDS = ("convention", "efficiency", "continuity")
# memory_campaign.py's headline tasks (memory_tasks.json's "headline": true).
# Kept as a literal, like EDITING_KINDS above, so this validator notices drift.
MEMORY_HEADLINE_TASKS = ("sup-current", "cont-log")
# Stable display order for known conditions; unknown names are appended.
CONDITION_ORDER = ("without", "agentsmd", "claudemd", "claudemd_agentsmd",
                   "with", "claudemd_with")
# The condition pairs that matter, first arm vs second arm. claudemd_with-vs-claudemd
# is the adoption question: contexer's marginal value on an already-documented repo.
# agentsmd-vs-claudemd measures whether the assistant honors AGENTS.md like CLAUDE.md.
PAIRS = (("with", "without"), ("with", "claudemd"), ("claudemd", "without"),
         ("claudemd_with", "claudemd"), ("agentsmd", "claudemd"),
         ("claudemd_agentsmd", "claudemd"))


def _conditions_present(rows):
    present = {r.get("condition") for r in rows if r.get("condition")}
    ordered = [c for c in CONDITION_ORDER if c in present]
    return ordered + sorted(present - set(CONDITION_ORDER))


# --- our own math (never report.py's) ---------------------------------------

def _num(row, metric):
    """Coerce a metric to float; success is a 0/1 indicator."""
    if metric == "success":
        return 1.0 if row.get("success") else 0.0
    return float(row.get(metric, 0) or 0)


def _median(values):
    vals = list(values)
    return statistics.median(vals) if vals else 0.0


def _is_errored(row):
    return bool(row.get("error"))


# --- loading ----------------------------------------------------------------

def _load_rows(runs_path):
    rows = []
    for i, line in enumerate(runs_path.read_text().splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise ValueError(f"runs.jsonl line {i + 1} is not valid JSON: {exc}")
    return rows


# --- the validator ----------------------------------------------------------

def validate(campaign_dir):
    campaign_dir = Path(campaign_dir)
    failures, warnings = [], []
    recomputed = {}

    runs_path = campaign_dir / "runs.jsonl"
    camp_path = campaign_dir / "campaign.json"
    if not runs_path.exists():
        return {"ok": False, "failures": [f"missing {runs_path}"],
                "warnings": [], "recomputed": {}}
    campaign = {}
    if camp_path.exists():
        try:
            campaign = json.loads(camp_path.read_text())
        except json.JSONDecodeError as exc:
            failures.append(f"campaign.json is not valid JSON: {exc}")
    else:
        warnings.append("campaign.json missing — reps/model cross-checks skipped")

    try:
        rows = _load_rows(runs_path)
    except ValueError as exc:
        return {"ok": False, "failures": [str(exc)], "warnings": warnings,
                "recomputed": {}}
    if not rows:
        return {"ok": False, "failures": ["runs.jsonl is empty"],
                "warnings": warnings, "recomputed": {}}

    reps = campaign.get("reps")
    camp_model = campaign.get("model") or ""

    ok_rows = [r for r in rows if not _is_errored(r)]
    err_rows = [r for r in rows if _is_errored(r)]

    _check_schema(rows, failures)
    _check_model(ok_rows, camp_model, failures)
    _check_coverage(rows, reps, warnings, recomputed)
    recomputed["medians"] = _recompute_medians(_median_rows(ok_rows))
    recomputed["excluded_errored"] = len(err_rows)
    _check_anomalies(rows, ok_rows, err_rows, warnings, recomputed)
    _check_paired(ok_rows, warnings, recomputed)
    _check_chains(rows, failures)
    _check_interleaving(rows, warnings)
    _check_memory_isolation(rows, failures)
    _check_tier_coverage(rows, warnings)

    return {"ok": not failures, "failures": failures, "warnings": warnings,
            "recomputed": recomputed}


def _check_schema(rows, failures):
    """Check 1: every row carries every expected key; token parts sum to total."""
    for i, r in enumerate(rows):
        missing = [k for k in EXPECTED_KEYS if k not in r]
        if missing:
            failures.append(f"row {i + 1} ({r.get('task_id', '?')}) missing keys: "
                            f"{', '.join(missing)}")
            continue
        if "arm" in r:
            missing = [k for k in MEMORY_KEYS if k not in r]
            if missing:
                failures.append(f"row {i + 1} ({r.get('task_id', '?')}) missing "
                                f"memory-campaign keys: {', '.join(missing)}")
        parts = sum(int(r.get(k, 0) or 0) for k in TOKEN_PARTS)
        if int(r.get("tokens_total", 0) or 0) != parts:
            failures.append(
                f"row {i + 1} ({r.get('task_id', '?')}/{r.get('condition', '?')}) "
                f"tokens_total={r.get('tokens_total')} != sum of parts={parts}")


def _check_model(ok_rows, camp_model, failures):
    """Check 2: one model across non-errored rows, matching campaign.json when set."""
    models = sorted({r.get("model") for r in ok_rows if r.get("model")})
    if len(models) > 1:
        failures.append(f"multiple models across non-errored rows: {models}")
    if camp_model:
        off = sorted({m for m in models if m != camp_model})
        if off:
            failures.append(f"row model(s) {off} do not match campaign model "
                            f"'{camp_model}'")


def _check_coverage(rows, reps, warnings, recomputed):
    """Check 3: each observed (task_id, condition) cell should hold `reps` rows.

    A short cell is a warning, not a failure. Cells are derived from the rows
    themselves (not tasks.json) so a small partial campaign isn't drowned in
    warnings for tasks it never intended to run — yet a genuinely absent cell
    (its task appears in the other condition) is still caught at zero rows.
    """
    tasks = sorted({r.get("task_id") for r in rows})
    conditions = sorted({r.get("condition") for r in rows})
    # A teach-phase task id can only exist in an arm that teaches; the bare arm never
    # does, so pairing them would emit a guaranteed spurious "0 of N rows" per teach
    # id. Both sets are empty for legacy campaigns (no "phase"), so nothing changes.
    teach_tasks = {r.get("task_id") for r in rows if r.get("phase") == "teach"}
    teach_conds = {r.get("condition") for r in rows if r.get("phase") == "teach"}
    counts = {}
    for t in tasks:
        for c in conditions:
            if t in teach_tasks and c not in teach_conds:
                continue
            n = sum(1 for r in rows if r.get("task_id") == t and r.get("condition") == c)
            counts[f"{t}|{c}"] = n
            if reps is not None and n < reps:
                warnings.append(f"cell ({t}, {c}) has {n} of {reps} rows (short)")
    recomputed["cell_counts"] = counts


def _median_rows(ok_rows):
    """Rows eligible for median recomputation.

    Legacy campaigns: every non-errored row, unchanged. Memory campaigns (any row
    carries "arm"): measured, non-enforcement rows only — the same exclusion
    report.py already applies. Folding them in would put teach rows (success always
    False, and their own token cost) and enforcement rows (success hardcoded True
    for the "with" arm) into the very medians MEMORY_CAMPAIGN.md publishes."""
    if not any("arm" in r for r in ok_rows):
        return ok_rows
    return [r for r in ok_rows
            if r.get("phase") == "measure" and r.get("kind") != "enforcement"]


def _recompute_medians(ok_rows):
    """Check 4: medians per metric per observed condition, errored rows excluded."""
    out = {}
    for c in _conditions_present(ok_rows):
        cond_rows = [r for r in ok_rows if r.get("condition") == c]
        out[c] = {m: _median(_num(r, m) for r in cond_rows) for m in MEDIAN_METRICS}
    return out


def _check_anomalies(rows, ok_rows, err_rows, warnings, recomputed):
    """Check 5: statistical smells — all warnings, none proves tampering."""
    # zero-token non-errored rows
    for r in ok_rows:
        if int(r.get("tokens_total", 0) or 0) == 0:
            warnings.append(f"non-errored row ({r.get('task_id')}/{r.get('condition')}"
                            f"/rep{r.get('rep')}) has tokens_total == 0")
    # suspicious no-op "wins" on editing tasks
    for r in ok_rows:
        if (r.get("success") and r.get("kind") in EDITING_KINDS
                and int(r.get("violations", 0) or 0) == 0
                and not str(r.get("result_snippet", "")).strip()
                and int(r.get("tokens_out", 0) or 0) < 50):
            warnings.append(f"suspicious no-op win: {r.get('task_id')}/"
                            f"{r.get('condition')}/rep{r.get('rep')} succeeded with "
                            f"empty output and tokens_out<50")
    # duration_ms outliers vs their own (task, condition) cell median
    cells = {}
    for r in ok_rows:
        cells.setdefault((r.get("task_id"), r.get("condition")), []).append(r)
    for (t, c), cell in cells.items():
        med = _median(_num(r, "duration_ms") for r in cell)
        if med <= 0:
            continue
        for r in cell:
            d = _num(r, "duration_ms")
            if d > 5 * med:
                warnings.append(f"duration outlier: {t}/{c}/rep{r.get('rep')} "
                                f"{int(d)}ms > 5x cell median {int(med)}ms")
    # error-rate asymmetry between conditions (max pairwise gap)
    conds = _conditions_present(rows)
    rates = {}
    for c in conds:
        total = sum(1 for r in rows if r.get("condition") == c)
        errs = sum(1 for r in err_rows if r.get("condition") == c)
        rates[c] = (errs / total) if total else 0.0
    recomputed["error_rates"] = rates
    if rates:
        gap = (max(rates.values()) - min(rates.values())) * 100
        if gap > 20:
            detail = ", ".join(f"{c}={rates[c]:.0%}" for c in conds)
            warnings.append(f"error-rate asymmetry {gap:.1f}pp between conditions "
                            f"({detail})")
    # rationale tasks scoring identically extreme in with AND without — the pair
    # where an identical extreme signals a leak or a broken injection.
    rat_tasks = sorted({r.get("task_id") for r in ok_rows if r.get("kind") == "rationale"})
    for t in rat_tasks:
        scores = {}
        for c in ("without", "with"):
            cr = [r for r in ok_rows if r.get("task_id") == t and r.get("condition") == c]
            if cr:
                scores[c] = _median(_num(r, "rationale") for r in cr)
        if len(scores) == 2 and scores["with"] == scores["without"]:
            if scores["with"] == 1.0:
                warnings.append(f"rationale task '{t}' scores 1.0 in BOTH conditions "
                                f"(gold may be leaking into the prompt)")
            elif scores["with"] == 0.0:
                warnings.append(f"rationale task '{t}' scores 0.0 in BOTH conditions "
                                f"(injection may be broken)")


def _check_paired(ok_rows, warnings, recomputed):
    """Check 6: per-task paired win/loss/tie matched by (task, rep), for every
    condition pair present (with-vs-without, with-vs-claudemd, claudemd-vs-without)."""
    index = {}
    for r in ok_rows:
        index[(r.get("task_id"), r.get("rep"), r.get("condition"))] = r
    conds = set(_conditions_present(ok_rows))

    paired = {}
    for a, b in PAIRS:
        if a not in conds or b not in conds:
            continue
        pairs = sorted({(task, rep) for (task, rep, cond) in index
                        if cond == a and (task, rep, b) in index})
        pair_name = f"{a}_vs_{b}"
        paired[pair_name] = {}
        for metric in PAIRED_METRICS:
            by_task = {}
            wins = losses = ties = 0
            for (task, rep) in pairs:
                va = _num(index[(task, rep, a)], metric)
                vb = _num(index[(task, rep, b)], metric)
                bucket = by_task.setdefault(task, {"wins": 0, "losses": 0, "ties": 0})
                if va < vb:       # lower is better -> first arm wins
                    wins += 1
                    bucket["wins"] += 1
                elif va > vb:
                    losses += 1
                    bucket["losses"] += 1
                else:
                    ties += 1
                    bucket["ties"] += 1
            paired[pair_name][metric] = {"wins": wins, "losses": losses, "ties": ties,
                                         "by_task": by_task}

            # sign-consistency: is a non-zero headline direction the work of one task?
            net = wins - losses
            if net != 0:
                task_nets = {t: bk["wins"] - bk["losses"] for t, bk in by_task.items()}
                # the single largest same-signed contributor
                same = [(t, n) for t, n in task_nets.items()
                        if (n > 0) == (net > 0) and n != 0]
                if same:
                    lead_task, lead_net = max(same, key=lambda kv: abs(kv[1]))
                    # remove that task: does the direction survive?
                    if (net - lead_net) == 0 or ((net - lead_net) > 0) != (net > 0):
                        direction = f"{a}-better" if net > 0 else f"{a}-worse"
                        warnings.append(
                            f"paired {metric} ({pair_name}) direction ({direction}) "
                            f"is driven by a single task '{lead_task}' — removing it "
                            f"flips/erases the sign")
    recomputed["paired"] = paired


def _check_chains(rows, failures):
    """Check 7 (coverage only): each chain must cover steps 1..3 in every
    condition observed in the campaign."""
    chains = sorted({r.get("chain") for r in rows if r.get("chain")})
    conds = _conditions_present(rows)
    for chain in chains:
        for c in conds:
            steps = {int(r.get("step", 0) or 0) for r in rows
                     if r.get("chain") == chain and r.get("condition") == c}
            missing = [s for s in (1, 2, 3) if s not in steps]
            if missing:
                failures.append(f"chain '{chain}' condition '{c}' missing step(s): "
                                f"{missing}")


def _check_interleaving(rows, warnings):
    """Check 8 (red-team #2): conditions must be interleaved in time.

    A condition whose rows form one contiguous time block earlier than every
    other condition's rows means the campaign ran condition-by-condition, so
    server-side drift / cache warming is confounded with condition. Rows
    without ``ts`` (old artifacts) skip the check with a warning.
    """
    if any("ts" not in r for r in rows):
        warnings.append("row(s) missing 'ts' — interleaving check skipped "
                        "(pre-timestamp artifact?)")
        return
    conds = _conditions_present(rows)
    if len(conds) < 2:
        return
    ts_by = {c: [float(r["ts"]) for r in rows if r.get("condition") == c]
             for c in conds}
    for c in conds:
        others = [t for oc in conds if oc != c for t in ts_by[oc]]
        if ts_by[c] and others and max(ts_by[c]) < min(others):
            warnings.append(f"condition '{c}' ran as one contiguous time block "
                            f"before all other conditions — conditions were not "
                            f"interleaved")


def _check_memory_isolation(rows, failures):
    """Check 9 (memory campaign): a measured row that leaked state across arms
    (contaminated=True) is a failure. Enforcement rows (which deliberately act
    on the fixture) and teach rows are exempt. No-op for legacy campaigns whose
    rows predate Task 6's arm/tier/phase/contaminated fields."""
    for r in rows:
        if "arm" not in r or not r.get("contaminated"):
            continue
        if r.get("phase") != "measure" or r.get("kind") == "enforcement":
            continue
        failures.append(f"contaminated row: {r.get('task_id')}/{r.get('arm')}/"
                        f"rep{r.get('rep')}")


def _check_tier_coverage(rows, warnings):
    """Check 10 (memory campaign): each headline task/arm needs equal, non-zero
    implicit vs. explicit measured reps. No-op for legacy campaigns (no `arm`)."""
    counts = {}
    for r in rows:
        if "arm" not in r:
            continue
        if r.get("phase") != "measure" or r.get("task_id") not in MEMORY_HEADLINE_TASKS:
            continue
        cell = counts.setdefault((r["task_id"], r["arm"]), {"implicit": 0, "explicit": 0})
        if r.get("tier") in cell:
            cell[r["tier"]] += 1
    for (task_id, arm), c in counts.items():
        imp, exp = c["implicit"], c["explicit"]
        if imp != exp or imp < 1 or exp < 1:
            warnings.append(f"tier imbalance for {task_id}/{arm}: "
                            f"implicit={imp} explicit={exp}")


# --- rendering --------------------------------------------------------------

def _fmt(v):
    return f"{v:.3f}" if isinstance(v, float) and v % 1 else f"{v:g}"


def render_validation(v):
    ok = v.get("ok")
    lines = ["## Independent Validation",
             "",
             f"**Status: {'PASS' if ok else 'FAIL'}** — "
             f"{len(v.get('failures', []))} failure(s), "
             f"{len(v.get('warnings', []))} warning(s)",
             ""]
    if v.get("failures"):
        lines += ["### Failures", ""]
        lines += [f"- {f}" for f in v["failures"]] + [""]
    if v.get("warnings"):
        lines += ["### Warnings", ""]
        lines += [f"- {w}" for w in v["warnings"]] + [""]

    rec = v.get("recomputed", {})
    meds = rec.get("medians")
    if meds:
        conds = ([c for c in CONDITION_ORDER if c in meds]
                 + sorted(set(meds) - set(CONDITION_ORDER)))
        lines += [f"### Recomputed medians (errored rows excluded, "
                  f"{rec.get('excluded_errored', 0)} excluded)",
                  "",
                  "| metric | " + " | ".join(conds) + " |",
                  "|" + "---|" * (1 + len(conds))]
        for m in MEDIAN_METRICS:
            cells = " | ".join(_fmt(meds.get(c, {}).get(m, 0.0)) for c in conds)
            lines.append(f"| {m} | {cells} |")
        lines.append("")

    paired = rec.get("paired") or {}
    for a, b in PAIRS:
        pair = paired.get(f"{a}_vs_{b}")
        if not pair:
            continue
        lines += [f"### Paired win/loss/tie ({a} vs {b}, by task+rep; "
                  f"lower is a {a}-win)",
                  "",
                  "| metric | wins | losses | ties |",
                  "|---|---|---|---|"]
        for m in PAIRED_METRICS:
            p = pair.get(m, {})
            lines.append(f"| {m} | {p.get('wins', 0)} | {p.get('losses', 0)} "
                         f"| {p.get('ties', 0)} |")
        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        print("usage: python -m benchmarks.validate <campaign_dir>", file=sys.stderr)
        return 2
    v = validate(Path(argv[0]))
    print(render_validation(v))
    return 0 if v["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
