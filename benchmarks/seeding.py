"""The one definition of how a benchmark task's `seed_decisions` become store writes.

Stdlib-only and import-free on purpose: `run.py` imports it, and `replay_delivery.py` loads it
by file path while measuring ANOTHER Contexer checkout, where `import benchmarks` would resolve
to that checkout's copy. Keeping it dependency-free is what lets both read the same file."""


def seed_items(task: dict, seed: int) -> list[dict]:
    """A task's `seed_decisions` ({content, subtype, source_files?}) with `{seed}` filled in,
    in list order (which is store order, and so slot order on versions that rank by it).
    Tasks carrying only the legacy single `seed_decision` return []."""
    items = []
    for item in task.get("seed_decisions") or []:
        items.append({
            "content": item["content"].replace("{seed}", str(seed)),
            "subtype": item.get("subtype", ""),
            "source_files": [f.replace("{seed}", str(seed))
                             for f in item.get("source_files") or []],
        })
    return items

