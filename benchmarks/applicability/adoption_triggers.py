"""Materialize the frozen adoption corpus with explicit capture-time applicability phrases.

This measures new captures enriched by their author, not automatic legacy backfill. Prompts,
labels, prose variants, order and all unrelated decisions remain unchanged. No live agent runs.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path

TRIGGERS = {
    "adopt-k3-atomic": ["export_records JSON", "writing record files"],
    "adopt-k4-append": ["append_record JSON", "adding persisted records"],
    "adopt-k5-no-threads": ["slow upstream", "finishing quickly", "fetch_many_slot3 records"],
}


def enrich(tasks: list[dict]) -> list[dict]:
    enriched = copy.deepcopy(tasks)
    seed_rules = [(next(t for t in tasks if t["id"] == key)["seed_decisions"][
        next(t for t in tasks if t["id"] == key)["needed_decision"]], phrases)
        for key, phrases in TRIGGERS.items()]
    for task in enriched:
        for item in task["seed_decisions"]:
            for rule, phrases in seed_rules:
                if item == rule:
                    item["applies_when"] = list(phrases)
    return enriched


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks-file", type=Path, default=Path("benchmarks/adoption_tasks.json"))
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    source = args.tasks_file.read_bytes()
    args.out.write_text(json.dumps(enrich(json.loads(source)), indent=2) + "\n", encoding="utf-8")
    print(f"Capture applicability fixture v1; source sha256={hashlib.sha256(source).hexdigest()}")


if __name__ == "__main__":
    main()
