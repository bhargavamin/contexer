"""Capture-loop report: per arm, how often session 1's rule was recorded and session 2 succeeded,
and for Contexer where each chain dropped off (not captured, pending review, not delivered,
delivered but not applied). Usage: python benchmarks/capture_report.py <campaign-dir>."""
import json
import sys
from collections import Counter
from pathlib import Path

ARMS = ("without", "claudemd_maintained", "with")


def _stage(first: dict, second: dict) -> str:
    if not first.get("captured"):
        return "not captured"
    if first.get("capture_status") == "pending_approval":
        return "captured, pending review"
    if second.get("needed_delivery") != "full":
        return "captured, not delivered"
    return "delivered and applied" if second.get("success") else "delivered, not applied"


def summarize(rows: list) -> dict:
    rows = [r for r in rows if r.get("kind") == "capture"]
    # Every attempted chain-rep, errored or not, so lost runs show up instead of vanishing.
    attempted = Counter(c for c in {(r["chain"], r["condition"], r["rep"]) for r in rows})
    rows = [r for r in rows if not r.get("error")]
    pairs: dict = {}
    for r in rows:
        pairs.setdefault((r["chain"], r["condition"], r["rep"]), {})[r["step"]] = r
    arms = {}
    for arm in ARMS:
        runs = [p for (_, c, _), p in pairs.items() if c == arm and 1 in p and 2 in p]
        tried = sum(n for (_, c, _), n in attempted.items() if c == arm)
        arms[arm] = {"runs": len(runs), "incomplete": tried - len(runs),
                     "captured": None if arm == "without" else sum(bool(p[1].get("captured"))
                                                                   for p in runs),
                     "success": sum(bool(p[2].get("success")) for p in runs)}
    funnel = Counter((_stage(p[1], p[2]), bool(p[2].get("success")))
                     for (_, c, _), p in pairs.items() if c == "with" and 1 in p and 2 in p)
    chains = {}
    for (chain, arm, _), p in sorted(pairs.items()):
        if 2 in p:
            chains.setdefault(chain, Counter())[arm] += bool(p[2].get("success"))
    return {"arms": arms, "funnel": dict(funnel), "chains": chains}


def render(summary: dict) -> str:
    out = ["| Arm | Runs | Incomplete (errored) | Rule recorded after session 1 | Session 2 success |",
           "| --- | --- | --- | --- | --- |"]
    for arm, a in summary["arms"].items():
        captured = "-" if a["captured"] is None else f"{a['captured']}/{a['runs']}"
        out.append(f"| {arm} | {a['runs']} | {a['incomplete']} | {captured} | "
                   f"{a['success']}/{a['runs']} |")
    lost = sum(a["incomplete"] for a in summary["arms"].values())
    if lost:
        out += ["", f"**{lost} chain run(s) errored and are excluded above; rerun them before "
                "comparing arms.**"]
    out += ["", "Contexer drop-off (stage, session 2 succeeded): count", ""]
    out += [f"- {stage}, {'succeeded' if ok else 'failed'}: {n}"
            for (stage, ok), n in sorted(summary["funnel"].items())]
    out += ["", "| Chain | " + " | ".join(ARMS) + " |", "| --- |" + " --- |" * len(ARMS)]
    out += [f"| {chain} | " + " | ".join(str(c[arm]) for arm in ARMS) + " |"
            for chain, c in summary["chains"].items()]
    return "\n".join(out)


if __name__ == "__main__":
    path = Path(sys.argv[1]) / "runs.jsonl"
    print(render(summarize([json.loads(line) for line in path.read_text().splitlines()
                            if line.strip()])))
