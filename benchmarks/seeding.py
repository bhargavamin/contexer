"""The one definition of how a benchmark task's `seed_decisions` become store writes.

Stdlib-only and import-free on purpose: `run.py` imports it, and `replay_delivery.py` loads it
by file path while measuring ANOTHER Contexer checkout, where `import benchmarks` would resolve
to that checkout's copy. Keeping it dependency-free is what lets both read the same file."""


def seed_items(task: dict, seed: int, rep: int = 0) -> list[dict]:
    """A task's `seed_decisions` with `{seed}` filled in, in list order (which is store order,
    and so slot order on versions that rank by it). An item is either one decision
    ({content, subtype, source_files?, title?}) or a decision written several ways
    ({subtype, source_files?, variants: [{style, content, title?}]}). A variant item takes
    variant `(rep + position) % len(variants)`: each rep shows a needed decision in a different
    author's style while the store around it stays a mix of styles, as a real one is.
    Tasks carrying only the legacy single `seed_decision` return []."""
    items = []
    for position, item in enumerate(task.get("seed_decisions") or []):
        variants = item.get("variants")
        picked = variants[(rep + position) % len(variants)] if variants else item
        items.append({
            "content": picked["content"].replace("{seed}", str(seed)),
            "title": picked.get("title", "").replace("{seed}", str(seed)),
            "style": picked.get("style", ""),
            "subtype": item.get("subtype", ""),
            "source_files": [f.replace("{seed}", str(seed))
                             for f in item.get("source_files") or []],
        })
    return items


def seed_script(repo: str, items: list[dict]) -> str:
    """Python source that stores `items` the way an agent's decisions reach a real store:
    `update_context` as the AI author (title, anchors, capture-quality gate and novelty filter
    all applied), then a developer approval for each one the store queued for review. One the
    store made active on capture ('suggested') is left as stored, as it is in a real store.
    Runs inside the measured checkout, so it imports only `contexer`. A seed the store refuses
    (gate bounce, duplicate, failed approval) raises with the store's own message: the
    static-file arm always gets every seed, so a silent drop would break arm parity. The ids
    land in `seeded_ids`, in item order."""
    lines = ["import re", "from contexer import server, store", "seeded_ids = []"]
    for item in items:
        lines.append(
            f"_r = server.update_context({item['content']!r}, repo_path={repo!r}, "
            f"subtype={item['subtype']!r}, created_by='ai', title={item['title']!r}, "
            f"source_files={item['source_files'] or None!r})\n"
            "_m = re.search(r'\\bid=([0-9a-f-]{8,})', _r)\n"
            f"assert _m, {'seed not stored: ' + item['content'][:60]!r} + ' -> ' + _r[:300]\n"
            f"_e = next(e for e in store.load({repo!r})['entries'] "
            "if str(e.get('id', '')).startswith(_m.group(1)))\n"
            "if _e.get('status') == 'pending_approval':\n"
            f"    _ok, _msg = store.approve_decision({repo!r}, _m.group(1), 'approve')\n"
            f"    assert _ok, {'seed not approved: ' + item['content'][:60]!r} + ' -> ' + _msg\n"
            "else:\n"
            "    assert _e.get('status') in ('suggested', 'approved'), "
            f"{'seed inactive: ' + item['content'][:60]!r} + ' -> ' + str(_e.get('status'))\n"
            "seeded_ids.append(_m.group(1))")
    return "\n".join(lines) + "\n"


def bootstrap_script(repo: str, finish: bool = False) -> str:
    """Python source that runs Contexer's bootstrap the way setup always has (first call), and
    with `finish` drives it to its completed stage with no findings, so the bootstrap prompt
    hook stays silent: the steady state a repository reaches once setup is done. Runs inside the
    measured checkout, so it imports only `contexer`."""
    lines = ["import json", "from contexer import server",
             f"_boot = server.bootstrap_context(repo_path={repo!r})"]
    if finish:
        lines.append(f"server.bootstrap_context(repo_path={repo!r}, "
                     "snapshot_id=json.loads(_boot)['snapshot_id'], findings=[], finish=True)")
    return "\n".join(lines) + "\n"


def steady_check_script(repo: str) -> str:
    """Python source asserting the bootstrap prompt is not due, run after every setup write so
    a steady-state session can't silently start with a setup detour."""
    return ("from contexer import bootstrap\n"
            f"assert not bootstrap.directive({repo!r}, check_freshness=True), "
            "'bootstrap prompt still due after steady-state setup'\n")
