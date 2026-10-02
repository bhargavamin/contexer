#!/usr/bin/env python3
"""Offline preview of what Contexer DELIVERS to an agent, for one Contexer version.

No model calls, no network, never touches the real ~/.contexer (a temporary store dir is
used). Run it once per Contexer checkout to compare versions before spending on a live
campaign; the `contexer` package is resolved from the `--project` checkout:

    uv run --frozen --project <contexer-checkout> python benchmarks/replay_delivery.py \\
        tasks --tasks-file benchmarks/retrieval_tasks.json

    uv run --frozen --project <contexer-checkout> python benchmarks/replay_delivery.py \\
        snapshot --snapshot <frozen store .json> --repo <task checkout> --prompt-file <prompt>

`tasks` builds the benchmark fixture repo, seeds each task's `seed_decisions` exactly as
`run.py` does for the contexer arm, then runs SessionStart and the prompt hook, and reports
whether the task's `needed_decision` arrived in full, only named in a pointer, or not at all.
It runs once per writing style (rep), matching what each live rep seeds.
`snapshot` replays a real session's frozen decision store against one prompt.

This file deliberately does not put the repository root on sys.path: the `contexer` it
measures must be the one `--project` installed, not this checkout's.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

import contexer
from contexer import store

# Pointer lines write "(id=abcd1234)" after a title, or a bare "id=abcd1234" without one.
_NAMED = re.compile(r"\bid=([0-9a-zA-Z-]{4,})")
_HERE = Path(__file__).resolve().parent


def _load(name: str, relpath: str):
    """A stdlib-only harness file by path: under `--project <other checkout>`, `import
    benchmarks` would resolve to that checkout's copy, not this one."""
    spec = importlib.util.spec_from_file_location(f"_replay_{name}", _HERE / relpath)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextlib.contextmanager
def _sandbox_store(path: Path):
    """Point every store read and write at `path` for the block, then restore: the same
    `store.store_dir` override the relevance baseline uses, never the real ~/.contexer."""
    previous = store.store_dir
    store.store_dir = lambda: path
    try:
        yield
    finally:
        store.store_dir = previous


def _contexer_revision() -> dict:
    root = Path(contexer.__file__).resolve().parents[1]
    rev = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                         capture_output=True, text=True).stdout.strip()
    return {"path": str(root), "revision": rev or "unknown"}


def _flat(text: str) -> str:
    return " ".join(text.split())


def _shown_in_full(entry: dict, text: str) -> bool:
    """Whether `text` carries this decision's whole guidance: its title and, when the
    measured version's own `store.title_and_body` splits a body off, that body too. Asking
    the version's renderer (not this file's guess at its layout) keeps the verdict right for
    a short decision whose title IS its content: shown in full with no body line."""
    title, body = store.title_and_body(entry)
    flat = _flat(text)
    return _flat(title) in flat and (body is None or _flat(body) in flat)


def _short(entry_id: str) -> str:
    return entry_id[:8]


def _deliver(repo: str, prompt: str, session_id: str) -> dict:
    start_text = store.session_start_payload(repo, "startup", session_id).get("context", "")
    text, meta = store.get_context_for_prompt_with_meta(repo, prompt, session_id)
    entries = [e for e in store.load(repo).get("entries", [])
               if e.get("type") == "decision" and e.get("id")]
    # Pointer lines name decisions (titles included) without delivering them, so they are
    # judged separately from the rendered bullets.
    pointers = [line for line in text.splitlines() if line.startswith("[Contexer]")]
    rendered = "\n".join(line for line in text.splitlines() if not line.startswith("[Contexer]"))
    start_full = [_short(e["id"]) for e in entries if _shown_in_full(e, start_text)]
    prompt_full = [_short(e["id"]) for e in entries if _shown_in_full(e, rendered)]
    named = list(dict.fromkeys(
        m for line in pointers for m in _NAMED.findall(line) if m not in prompt_full))
    return {
        "startup_chars": len(start_text), "startup_full_ids": start_full,
        "prompt_chars": len(text), "prompt_kind": meta.get("kind"),
        "prompt_full_ids": prompt_full, "prompt_named_ids": named,
        "repeated_from_startup": [i for i in prompt_full if i in start_full],
        "startup_text": start_text, "prompt_text": text,
    }


def _verdict(labelled: bool, needed: str | None, result: dict) -> str:
    """How the task's needed decision (short id, None if never stored) reached the agent."""
    if not labelled:
        return "unlabelled"
    if needed is None:
        return "not_stored"  # the live arm would fail setup
    if needed in result["prompt_full_ids"] or needed in result["startup_full_ids"]:
        return "full"
    if needed in result["prompt_named_ids"]:
        return "named"
    return "missing"


def _by_style(rows: list[dict]) -> dict:
    """Per author style: rows, and how many delivered the needed decision full/named/missing."""
    by_style = {}
    for row in rows:
        counts = by_style.setdefault(row["needed_style"], {"full": 0, "named": 0, "missing": 0,
                                                           "tasks": 0})
        counts["tasks"] += 1
        key = "missing" if row["needed"] in ("missing", "not_stored") else row["needed"]
        if key in counts:
            counts[key] += 1
    return by_style


def replay_tasks(tasks_file: Path, seed: int) -> dict:
    tasks = [t for t in json.loads(tasks_file.read_text()) if t.get("seed_decisions")]
    if not tasks:
        raise SystemExit(f"{tasks_file}: no task carries seed_decisions")
    build_webapi = _load("generate", "fixtures/generate.py").build_webapi
    seeding = _load("seeding", "seeding.py")
    # One pass per writing style: rep r is what a live campaign's rep r seeds.
    reps = max(len(s.get("variants") or [s]) for t in tasks for s in t["seed_decisions"])
    rows = []
    with (tempfile.TemporaryDirectory(prefix="contexer-replay-") as tmp,
          _sandbox_store(Path(tmp) / ".contexer")):
        tmp_path = Path(tmp)
        from contexer import server
        for rep in range(reps):
            for task in tasks:
                repo = str(build_webapi(tmp_path / f"{task['id']}-rep{rep}", seed=seed)
                           .resolve())
                # Same order as run.py's contexer arm: bootstrap first, then the seeds.
                server.bootstrap_context(repo_path=repo)
                items = seeding.seed_items(task, seed, rep)
                scope = {}
                refusal = ""
                try:  # the same writes run.py makes
                    exec(compile(seeding.seed_script(repo, items), "<seed>", "exec"), scope)
                except AssertionError as exc:  # a seed this version refused
                    refusal = str(exc)
                done = [_short(i) for i in scope.get("seeded_ids", [])]
                ids = done + [None] * (len(items) - len(done))
                prompt = task["prompt"].replace("{seed}", str(seed))
                result = _deliver(repo, prompt, f"replay-{task['id']}-{rep}")
                index = task.get("needed_decision")
                needed = ids[index] if index is not None else None
                rows.append({"task_id": task["id"], "rep": rep,
                             "needed_style": items[index]["style"] if index is not None else "",
                             "seeded_ids": ids, "needed_id": needed,
                             "needed": _verdict(index is not None, needed, result),
                             "refusal": refusal, **result})
    return {
        "mode": "tasks", "tasks_file": str(tasks_file), "seed": seed,
        "contexer": _contexer_revision(), "tasks": rows,
        "summary": {
            "needed_full": sum(r["needed"] == "full" for r in rows), "tasks": len(rows),
            "needed_named_only": sum(r["needed"] == "named" for r in rows),
            "needed_missing": sum(r["needed"] in ("missing", "not_stored") for r in rows),
            "seeds_not_stored": sum(i is None for r in rows for i in r["seeded_ids"]),
            "repeated_from_startup": sum(len(r["repeated_from_startup"]) for r in rows),
            "prompt_chars": sum(r["prompt_chars"] for r in rows),
            "by_style": _by_style(rows),
        },
    }


def replay_snapshot(snapshot: Path, repo: Path, prompt_file: Path,
                    global_snapshot: Path | None) -> dict:
    repo_path = str(repo.resolve())
    with (tempfile.TemporaryDirectory(prefix="contexer-replay-") as tmp,
          _sandbox_store(Path(tmp) / ".contexer")):
        store.save(repo_path, json.loads(snapshot.read_text(encoding="utf-8")))
        if global_snapshot:
            store.save_global(json.loads(global_snapshot.read_text(encoding="utf-8")))
        result = _deliver(repo_path, prompt_file.read_text(encoding="utf-8"), "replay-session")
    return {"mode": "snapshot", "contexer": _contexer_revision(), **result}


def render_text(report: dict) -> str:
    lines = [f"contexer: {report['contexer']['revision'][:12]} ({report['contexer']['path']})"]
    if report["mode"] == "tasks":
        s = report["summary"]
        lines.append(f"needed decision in full: {s['needed_full']}/{s['tasks']} "
                     f"(named only {s['needed_named_only']}, missing {s['needed_missing']}); "
                     f"startup rules repeated: {s['repeated_from_startup']}; "
                     f"prompt chars: {s['prompt_chars']}")
        for style, counts in s["by_style"].items():
            lines.append(f"  {style or 'single'} style: full {counts['full']}/{counts['tasks']}, "
                         f"named only {counts['named']}, missing {counts['missing']}")
        if s["seeds_not_stored"]:
            lines.append(f"WARNING: {s['seeds_not_stored']} seed(s) refused by this version's "
                         "store; its live arm would fail setup")
        for row in report["tasks"]:
            if row["refusal"]:
                lines.append(f"  refused ({row['task_id']} rep {row['rep']}): {row['refusal']}")
            lines.append(f"- {row['task_id']} rep {row['rep']} ({row['needed_style']}): "
                         f"needed {row['needed']}, full "
                         f"{row['prompt_full_ids']}, named {row['prompt_named_ids']}, "
                         f"repeated {row['repeated_from_startup']}")
    else:
        lines.append(f"startup chars {report['startup_chars']}, prompt chars "
                     f"{report['prompt_chars']}, full {report['prompt_full_ids']}, named "
                     f"{report['prompt_named_ids']}, repeated {report['repeated_from_startup']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    tasks = sub.add_parser("tasks", help="preview delivery for a benchmark task file")
    tasks.add_argument("--tasks-file", type=Path, default=_HERE / "retrieval_tasks.json")
    tasks.add_argument("--seed", type=int, default=0)
    snap = sub.add_parser("snapshot", help="replay a frozen store against one prompt")
    snap.add_argument("--snapshot", type=Path, required=True)
    snap.add_argument("--repo", type=Path, required=True)
    snap.add_argument("--prompt-file", type=Path, required=True)
    snap.add_argument("--global-snapshot", type=Path)
    for p in (tasks, snap):
        p.add_argument("--format", choices=("text", "json"), default="text")
        p.add_argument("--out", type=Path, help="also write the JSON report here")
    args = parser.parse_args(argv)
    if args.mode == "tasks":
        report = replay_tasks(args.tasks_file, args.seed)
    else:
        report = replay_snapshot(args.snapshot, args.repo, args.prompt_file,
                                 args.global_snapshot)
    if args.out:
        args.out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2) if args.format == "json" else render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
