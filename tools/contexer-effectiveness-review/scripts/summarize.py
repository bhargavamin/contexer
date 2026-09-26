#!/usr/bin/env python3
"""Markdown report over every logged record. Every share prints its denominator.

Usage: summarize.py [--repo-key KEY] [--host cursor|claude|codex]
"""
import argparse
import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from log_usage import ENUMS, ROOT, SCHEMA, validate  # noqa: E402

MIN_RECORDS = 20
MIN_ROW = 5
DIMENSIONS = [
    ("question_type", "Knowledge the task needed"),
    ("knowledge_location", "Where the answer lived"),
    ("scope", "Scope of the change"),
    ("category", "Task type"),
    ("difficulty", "Difficulty"),
]


def usable(rec):
    if not isinstance(rec, dict) or rec.get("schema") != SCHEMA:
        return False
    if any(not isinstance(rec.get(k), str) or not rec[k] for k in
           ("record_id", "recorded_at", "repo_key", "session_id", "host")):
        return False
    j = rec.get("judgment")
    if validate(j):
        return False
    trigger = rec.get("trigger")
    if not isinstance(trigger, dict) or not isinstance(trigger.get("commits"), list):
        return False
    if any(not isinstance(c, dict) or not isinstance(c.get("sha"), str)
           for c in trigger["commits"]):
        return False
    if not isinstance(trigger.get("attribution", ""), str):
        return False
    observed = rec.get("observed")
    if not isinstance(observed, dict):
        return False
    for field in ("contexer_call_count", "autofetch_blocks", "exploration_before_first_contexer_call"):
        value = observed.get(field)
        if value is not None and (type(value) is not int or value < 0):
            return False
    calls = observed.get("contexer_calls", [])
    return isinstance(calls, list) and all(isinstance(c, dict) and isinstance(c.get("tool"), str)
                                          for c in calls)


def dedupe_key(rec):
    """The same commits reviewed twice in one session count once; PR-only reviews never merge."""
    shas = ",".join(sorted(c.get("sha", "") for c in rec.get("trigger", {}).get("commits", [])))
    return (rec.get('repo_key'), rec.get('host'), rec.get('session_id'), shas or rec.get('record_id'))


def load(repo_key, host):
    records, skipped, dupes, keys = [], 0, 0, set()
    for path in sorted((ROOT / "records").glob(f"{repo_key or '*'}.jsonl")):
        for line in path.read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                skipped += 1
                continue
            if not usable(rec):
                skipped += 1
                continue
            if host and rec.get("host") != host:
                continue
            key = dedupe_key(rec)
            if key in keys:
                dupes += 1
                continue
            keys.add(key)
            records.append(rec)
    records.sort(key=lambda r: r.get("recorded_at", ""))
    latest = {}
    outcomes = ROOT / "outcomes.jsonl"
    if outcomes.is_file():
        for line in outcomes.read_text().splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict) and isinstance(row.get("record_id"), str):
                    latest[row["record_id"]] = row
            except (ValueError, KeyError):
                continue
    return records, latest, skipped, dupes


def share(counter, total, order):
    if not total:
        return "no data (0 items)"
    return ", ".join(f"{k} {counter[k]}/{total}" for k in order if counter[k])


def table(rows, cols, cells):
    out = ["| | " + " | ".join(cols) + " | n |", "|---" * (len(cols) + 2) + "|"]
    for row in rows:
        counts = cells.get(row, Counter())
        n = sum(counts.values())
        if n:
            out.append(f"| {row} | " + " | ".join(str(counts[c]) for c in cols) + f" | {n} |")
    return "\n".join(out)


def ranked(judgments, key):
    """Rows with at least MIN_ROW records ranked by credited share; the rest listed apart."""
    groups = defaultdict(Counter)
    for x in judgments:
        groups[x["task"][key]][x["verdict"]["contexer_effect"]] += 1
    rankable, few = [], []
    for value, c in groups.items():
        n = sum(c.values())
        credited = c["decisive"] + c["helpful"]
        (rankable if n >= MIN_ROW else few).append((credited / n, n, value, credited, c))
    rankable.sort(key=lambda r: (-r[0], -r[1]))
    out = ["| value | credited | neutral | not_used | harmful | n |", "|---|---|---|---|---|---|"]
    for _, n, value, credited, c in rankable:
        out.append(f"| {value} | {credited}/{n} | {c['neutral']} | {c['not_used']} | "
                   f"{c['harmful']} | {n} |")
    if not rankable:
        out.append(f"| (no value has {MIN_ROW}+ records yet) | | | | | |")
    if few:
        out.append("\nToo few to rank: " + ", ".join(
            f"{value} {credited}/{n}" for _, n, value, credited, _ in sorted(few, key=lambda r: r[2])))
    return "\n".join(out)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-key")
    parser.add_argument("--host")
    args = parser.parse_args()
    records, outcomes, skipped, dupes = load(args.repo_key, args.host)
    n = len(records)
    print("# Contexer effectiveness report\n")
    if skipped or dupes:
        print(f"Skipped {skipped} unreadable or older-schema record(s) and {dupes} duplicate(s).\n")
    if not n:
        print("No usable records yet.")
        return
    j = [r["judgment"] for r in records]
    print(f"Records: {n} across {len({r.get('repo_key') for r in records})} repo(s); hosts: "
          f"{dict(Counter(r.get('host') for r in records))}; "
          f"{records[0]['recorded_at'][:10]} to {records[-1]['recorded_at'][:10]}.")
    attrib = Counter(r.get("trigger", {}).get("attribution", "unrecorded") for r in records)
    print(f"Commit attribution: {share(attrib, n, sorted(attrib))}.")
    print("Judgments are self-assessed by the working agent. Automatic reviews sample commits "
          "or PR commands; manual reviews may also be present. Credited shares are the agent's "
          "opinion, not a measured effect. Observed "
          "counts come from git and transcripts; outcomes come from GitHub and git later.")
    if n < MIN_RECORDS:
        print(f"\n> Only {n} record(s), under {MIN_RECORDS}: read this as descriptive, "
              "not as a conclusion.")

    print("\n## Verdicts (self-assessed)\n")
    for key in ("contexer_effect", "without_contexer", "confidence"):
        c = Counter(x["verdict"].get(key) for x in j)
        print(f"- {key}: {share(c, n, ENUMS['verdict.' + key])}")

    print("\n## Where the deciding facts came from\n")
    facts = [f for x in j for f in x["key_facts"] if isinstance(f, dict)]
    print(f"- source: {share(Counter(f.get('source') for f in facts), len(facts), ENUMS['key_facts.source'])}")
    ctx = [f for f in facts if f.get("source") == "contexer"]
    print("- Contexer-sourced facts, could the code alone have shown them: "
          f"{share(Counter(f.get('code_would_reveal') for f in ctx), len(ctx), ENUMS['key_facts.code_would_reveal'])}")
    other = [f for f in facts if f.get("source") != "contexer"]
    print("- facts from other sources, could the code alone have shown them: "
          f"{share(Counter(f.get('code_would_reveal') for f in other), len(other), ENUMS['key_facts.code_would_reveal'])}")

    print("\n## Where Contexer works best, by kind of problem\n")
    print(f"Credited = decisive or helpful (self-assessed). Only values with {MIN_ROW}+ records "
          "are ranked.\n")
    for key, label in DIMENSIONS:
        print(f"**{label}**\n")
        print(ranked(j, key))
        print()

    print("## Surfaced decisions\n")
    items = [i for x in j for i in x["contexer_items"] if isinstance(i, dict)]
    own = [i for i in items if i.get("created_this_session")]
    rated = [i for i in items if not i.get("created_this_session")]
    print(f"{len(rated)} rated item(s); excluded {len(own)} created in the same session.\n")
    cells = defaultdict(Counter)
    for i in rated:
        cells[i.get("surfaced_by")][i.get("relevance")] += 1
    print(table(ENUMS["contexer_items.surfaced_by"], ENUMS["contexer_items.relevance"], cells))

    print("\n## Where the LLM or code did better\n")
    gaps = [g for x in j for g in x["gaps"] if isinstance(g, dict)]
    print(f"- gaps: {share(Counter(g.get('kind') for g in gaps), len(gaps), ENUMS['gaps.kind'])}")
    notes = [(r["recorded_at"][:10], r["judgment"]["llm_did_better"]) for r in records
             if r["judgment"].get("llm_did_better")]
    print(f"- records naming a case where the LLM or code did better: {len(notes)}/{n}")
    for day, note in notes[-10:]:
        print(f"  - {day}: {str(note)[:200]}")

    print("\n## Feature usefulness (self-assessed)\n")
    cells = defaultdict(Counter)
    for x in j:
        for f in x["feature_ratings"]:
            if isinstance(f, dict):
                cells[f.get("feature")][f.get("verdict")] += 1
    print(table(ENUMS["feature_ratings.feature"], ENUMS["feature_ratings.verdict"], cells))

    print("\n## Improvement ideas\n")
    ideas = [(r["recorded_at"][:10], i) for r in records for i in r["judgment"]["improvements"]
             if isinstance(i, dict)]
    print(f"{len(ideas)} idea(s) across {n} record(s).\n")
    cells = defaultdict(Counter)
    for _, i in ideas:
        cells[i.get("area")][i.get("kind")] += 1
    print(table(ENUMS["improvements.area"], ENUMS["improvements.kind"], cells))
    for day, i in ideas[-15:]:
        print(f"- {day} [{i.get('kind')} / {i.get('area')}] {str(i.get('idea'))[:200]}")

    print("\n## Observed behaviour\n")
    obs = [r["observed"] for r in records]
    counted = [o for o in obs if o.get("contexer_call_count") is not None]
    print(f"- records with no Contexer tool call: "
          f"{sum(1 for o in counted if o['contexer_call_count'] == 0)}/{len(counted)} "
          f"with complete counts; unknown in {n - len(counted)}/{n} records")
    before = [o["exploration_before_first_contexer_call"] for o in obs
              if o.get("exploration_before_first_contexer_call") is not None]
    print("- exploration calls before the first Contexer call: "
          + (f"median {statistics.median(before)} (n={len(before)} of {n})" if before
             else f"no complete observations (0 of {n} records)"))
    visible = [o for o in obs if o.get("autofetch_blocks") is not None]
    print(f"- auto-fetch visible in {len(visible)}/{n} records; blocks seen "
          f"{sum(o['autofetch_blocks'] for o in visible)} across those {len(visible)}")
    tools = Counter(c.get("tool") for o in obs for c in o.get("contexer_calls", []))
    print(f"- Directly observed Contexer calls across {n} records (lower bound when coverage is incomplete): {dict(tools.most_common()) or 'none'}")

    print("\n## Outcomes (observed status, by self-assessed effect)\n")
    cells = defaultdict(Counter)
    for r in records:
        row = outcomes.get(r["record_id"])
        state = row.get("pr_state", "unknown") if row else "not_checked"
        if not isinstance(state, str) or state not in ("merged", "open", "closed", "no_pr", "unknown", "not_checked"):
            state = "unknown"
        if row and isinstance(row.get("reverted"), list) and row["reverted"] and all(
                isinstance(sha, str) for sha in row['reverted']):
            state = "reverted"
        cells[r["judgment"]["verdict"].get("contexer_effect")][state] += 1
    cols = ["merged", "open", "closed", "reverted", "no_pr", "unknown", "not_checked"]
    print(table(ENUMS["verdict.contexer_effect"], cols, cells))


if __name__ == "__main__":
    main()
