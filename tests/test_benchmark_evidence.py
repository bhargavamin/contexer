"""Latency benchmarks for the evidence pipeline and the policy plane.

Run with:
    uv run pytest tests/test_benchmark_evidence.py -m perf --no-cov -s

Everything timing-shaped here carries `@pytest.mark.perf`, so it is deselected in CI and
skipped whenever coverage is on (a wall-clock number taken under a tracer is not the number
the assertion is about - see docs/testing.md). The thresholds are deliberately several times
the measured figure: they exist to catch an ORDER-OF-MAGNITUDE regression, not to police
jitter on a shared runner, which is the failure mode the novelty-filter write-latency
benchmark already paid for twice.

Four things are measured, one per section:

1. **Append cost is FLAT.** The spool's whole design premise is that the cost of event N does
   not depend on events 1..N-1 - one file, no listing, no lock. That is asserted as a RATIO
   (the last 50 appends into a full spool against the first 50 into an empty one), which is
   the property itself rather than a machine-speed proxy, and it holds under concurrent
   writers because a uuid in the filename means two writers can never name one target.
2. **Reconciliation at the spool's bound** (`_MAX_PENDING_EVENTS` = 1000): the directory
   listing alone, a full `dry_run` pass over a realistically shaped corpus, and - separately,
   because it is an order of magnitude dearer - the O(N^2) ceiling a corpus of entirely
   unrelated statements reaches.
3. **The prompt path never loads evidence.** This is the plan's exit gate, and it is asserted
   as BEHAVIOUR rather than timed - a timing test would pass just as well if the spool were
   read and simply happened to be small. Unmarked, therefore, and in the default gate.
4. **Policy selection and evaluation at the 500-decision store cap** (`store.MAX_ENTRIES`),
   both as the pure evaluator and through the repo-scoped facade that loads the store first.
"""

import io
import os
import statistics
import threading
import time
import uuid
from datetime import datetime, timezone

import pytest

from contexer import candidates, evidence, policy, policy_api, reconcile, spool, store

_SAMPLES = 50          # per timed loop; enough for a stable p95 without a slow suite


def _pstats(values: list[float]) -> dict:
    ordered = sorted(values)
    n = len(ordered)
    return {"mean": statistics.mean(ordered), "p50": ordered[n // 2],
            "p95": ordered[min(n - 1, int(n * 0.95))],
            "p99": ordered[min(n - 1, int(n * 0.99))],
            "min": ordered[0], "max": ordered[-1]}


def _report(label: str, stats: dict) -> None:
    print(f"\n  {label}")
    print(f"    mean {stats['mean']:.3f}ms  p50 {stats['p50']:.3f}ms  "
          f"p95 {stats['p95']:.3f}ms  p99 {stats['p99']:.3f}ms  max {stats['max']:.3f}ms")


def _timed(call) -> float:
    start = time.perf_counter()
    call()
    return (time.perf_counter() - start) * 1000


def _event(summary: str = "an observed fact", session: str = "sess-1", **overrides) -> dict:
    event = {"schema_version": evidence.SCHEMA_VERSION, "event_id": str(uuid.uuid4()),
             "session_id": session, "repo_key": "/repo", "kind": "user_directive",
             "occurred_at": datetime.now(timezone.utc).isoformat(), "source": "bench",
             "summary": summary, "files": [], "attributes": {}}
    event.update(overrides)
    return event


# ── 1. append cost is flat, with and without concurrent writers ──────────────────

@pytest.mark.perf
def test_append_latency_does_not_grow_with_the_spool(tmp_repo):
    """The premise of one-file-per-event: appending is O(1) in what the spool already holds.

    Asserted as a RATIO rather than as a millisecond figure, because the ratio IS the property
    - a listing, a re-read or a lock would make the last batch measurably dearer than the
    first, on any machine. The absolute bound beside it is the loose sanity check."""
    first = [_timed(lambda: spool.append_evidence(tmp_repo, _event())) for _ in range(50)]
    for _ in range(spool._MAX_PENDING_EVENTS - 100):
        spool.append_evidence(tmp_repo, _event())
    last = [_timed(lambda: spool.append_evidence(tmp_repo, _event())) for _ in range(50)]

    empty, full = _pstats(first), _pstats(last)
    _report("append into an empty spool (50)", empty)
    _report(f"append into a spool of ~{spool._MAX_PENDING_EVENTS} (50)", full)
    print(f"    ratio (full / empty, median): {full['p50'] / empty['p50']:.2f}x")

    assert full["p50"] < empty["p50"] * 4, "append cost is growing with the spool"
    assert full["p99"] < 20.0, f"append p99 too slow: {full['p99']:.3f}ms"


@pytest.mark.perf
def test_append_latency_under_concurrent_writers(tmp_repo):
    """Eight writers, no lock anywhere. A uuid in the filename is what removes contention, so
    the per-append distribution should look like the single-writer one rather than showing the
    queueing a shared lock would produce."""
    writers, per_writer = 8, 50
    samples: list[list[float]] = [[] for _ in range(writers)]
    barrier = threading.Barrier(writers)

    def run(index: int) -> None:
        barrier.wait()
        for _ in range(per_writer):
            samples[index].append(_timed(lambda: spool.append_evidence(tmp_repo, _event())))

    threads = [threading.Thread(target=run, args=(i,)) for i in range(writers)]
    start = time.perf_counter()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    wall = (time.perf_counter() - start) * 1000

    stats = _pstats([value for row in samples for value in row])
    _report(f"append under {writers} concurrent writers ({writers * per_writer} appends)",
            stats)
    print(f"    wall clock for the whole run: {wall:.1f}ms")

    assert len(spool.list_pending_evidence(tmp_repo)) == writers * per_writer, "an event was lost"
    assert stats["p99"] < 50.0, f"contention under concurrent writers: {stats['p99']:.3f}ms"


# ── 2. reconciliation and listing at the spool's bound ───────────────────────────

def _fill_to_the_bound(repo: str, seeds: int = 100) -> None:
    """`_MAX_PENDING_EVENTS` events in the shape a real session leaves: a few statements and
    many corroborating file changes, not 1000 unrelated directives."""
    for i in range(seeds):
        spool.append_evidence(repo, _event(f"Decision number {i} about subsystem {i}.",
                                           session=f"sess-{i}"))
    for i in range(spool._MAX_PENDING_EVENTS - seeds):
        spool.append_evidence(repo, _event(kind="file_changed", session=f"sess-{i % seeds}",
                                           summary=f"module {i} changed",
                                           files=[f"src/module_{i % seeds}.py"]))


@pytest.mark.perf
def test_listing_and_reconciliation_at_the_spool_bound(tmp_repo):
    """Both halves of a pass at `_MAX_PENDING_EVENTS`: the directory listing (which parses and
    re-validates every file) and a full `dry_run` reconciliation over the same corpus.

    A dry run is what is repeated, deliberately: it does the identical read, projection,
    grouping and scoring work a real pass does and writes nothing, so the measurement can be
    taken more than once against one fixed corpus."""
    _fill_to_the_bound(tmp_repo)
    store.update_decision(tmp_repo, "Postgres backs the decision store, pooled by pgbouncer.",
                          "sess-x", "architecture", created_by="human")

    listing = _pstats([_timed(lambda: spool.list_pending_evidence(tmp_repo)) for _ in range(5)])
    _report(f"list_pending_evidence at {spool._MAX_PENDING_EVENTS} events (5)", listing)

    passes = _pstats([_timed(lambda: reconcile.reconcile_session(tmp_repo, dry_run=True))
                      for _ in range(5)])
    _report(f"reconcile_session(dry_run) at {spool._MAX_PENDING_EVENTS} events (5)", passes)

    assert listing["p50"] < 500.0, f"listing too slow: {listing['p50']:.1f}ms"
    assert passes["p50"] < 1000.0, f"reconciliation too slow: {passes['p50']:.1f}ms"


@pytest.mark.perf
def test_aggregation_cost_when_every_event_is_a_distinct_statement(tmp_repo):
    """The measured CEILING of a pass, recorded rather than avoided.

    Grouping compares each new seed against every group opened so far, so a corpus of N
    entirely unrelated statements is O(N^2) token-overlap comparisons - and the shape above,
    where most events corroborate one of a hundred statements, does not reach it. At the
    spool's own bound this is seconds rather than the ~70ms the realistic shape costs, and
    reconciliation runs at session start, so the number is worth knowing before somebody
    raises `_MAX_PENDING_EVENTS`. Pinned loosely: this asserts the ceiling has not moved by an
    order of magnitude, not that it is fast."""
    for i in range(spool._MAX_PENDING_EVENTS):
        spool.append_evidence(tmp_repo, _event(
            f"Decision number {i} concerns subsystem alpha{i} and its owner team{i}."))
    events = spool.list_pending_evidence(tmp_repo)

    elapsed = _timed(lambda: candidates.aggregate_candidates(events, []))
    print(f"\n  aggregate_candidates over {len(events)} distinct statements: {elapsed:.1f}ms")

    assert len(candidates.aggregate_candidates(events, [])["candidates"]) == len(events)
    assert elapsed < 30000.0, f"aggregation ceiling has moved: {elapsed:.1f}ms"


# ── 3. the prompt path never loads evidence (the plan's exit gate) ───────────────

def _refuse(*_args, **_kwargs):
    raise AssertionError("the prompt path reached the evidence spool")


def test_the_prompt_path_never_loads_the_evidence_spool(tmp_repo, monkeypatch):
    """The plan's exit gate, asserted as BEHAVIOUR rather than as a timing.

    A latency assertion would pass equally well if the spool were read and merely happened to
    be small, which is the regression that matters - the prompt path is the one place where
    the developer is blocked and never asked for the work. So this proves the reads do not
    happen: every callable in `spool` is replaced with one that raises, and the three syscalls
    a read of a spooled file must go through are watched for any path under the evidence root.

    `os.stat` is deliberately NOT watched: the gate is about LOADING evidence, and a `stat`
    that never opens anything reads nothing.
    """
    spool.append_evidence(tmp_repo, _event("Always pin the auth middleware."))
    store.update_decision(tmp_repo, "Postgres backs the decision store because the decision "
                                    "history is relational and we already run one.",
                          "sess-1", "architecture", created_by="human")
    root = str(spool._repo_dir(tmp_repo))

    touched: list[str] = []
    for module, name in ((io, "open"), (os, "listdir"), (os, "scandir")):
        real = getattr(module, name)

        def watched(path, *args, _real=real, **kwargs):
            if root in str(path):
                touched.append(str(path))
            return _real(path, *args, **kwargs)
        monkeypatch.setattr(module, name, watched)
    for name, value in vars(spool).items():
        if callable(value) and not name.startswith("__") and value.__module__ == spool.__name__:
            monkeypatch.setattr(spool, name, _refuse)

    injected = store.get_context_for_prompt(tmp_repo, "why did we choose postgres?")

    assert touched == [], f"the prompt path read the evidence spool: {touched}"
    assert "Postgres" in injected, "the prompt path stopped injecting, so it proved nothing"


# ── 4. policy selection and evaluation at the store cap ──────────────────────────

def _fill_store_to_the_cap(repo: str, armed_every: int = 50) -> None:
    """`store.MAX_ENTRIES` approved decisions, every `armed_every`-th one carrying a regex
    rule, all anchored so the advisory lane selects too."""
    data = store.load(repo)
    for i in range(store.MAX_ENTRIES):
        entry = store._new_decision_entry(
            f"Decision {i}: subsystem {i} is owned by team {i % 7} and changes go through "
            f"review.", "sess-bench", "architecture", created_by="human", status="approved")
        entry["source_files"] = [f"src/module_{i}.py"]
        if i % armed_every == 0:
            entry["guard_check"] = {"type": "regex", "pattern": "TODO", "flags": "",
                                    "paths": "", "message": "no TODOs", "armed_at": "t"}
        data["entries"].append(entry)
    store.save(repo, data)


@pytest.mark.perf
def test_policy_selection_and_evaluation_at_the_store_cap(tmp_repo):
    """The pure half: `select_policies` over `store.MAX_ENTRIES` entries, then
    `evaluate_policies` over what it picked, against a realistic diff."""
    _fill_store_to_the_cap(tmp_repo)
    entries = store.load(tmp_repo)["entries"]
    request = {"intent": "commit the auth change", "operation": "commit",
               "repo_key": tmp_repo, "files": [f"src/module_{i}.py" for i in range(5)],
               "artifact": {"kind": "diff",
                            "content": "\n".join(f"+ line {i}" for i in range(500))
                                       + "\n+ # TODO fix this\n"}}

    selection = _pstats([_timed(lambda: policy.select_policies(entries, request))
                         for _ in range(_SAMPLES)])
    _report(f"select_policies over {store.MAX_ENTRIES} decisions ({_SAMPLES})", selection)

    selected = policy.select_policies(entries, request)
    judging = _pstats([_timed(lambda: policy.evaluate_policies(selected, request))
                       for _ in range(_SAMPLES)])
    _report(f"evaluate_policies over {len(selected)} selected ({_SAMPLES})", judging)

    assert [m["verdict"] for m in policy.evaluate_policies(selected, request)["matches"]], \
        "the armed rule never fired, so the numbers describe an empty run"
    assert selection["p99"] < 100.0, f"selection too slow: {selection['p99']:.3f}ms"
    assert judging["p99"] < 100.0, f"judging too slow: {judging['p99']:.3f}ms"


@pytest.mark.perf
def test_evaluate_operation_end_to_end_at_the_store_cap(tmp_repo, monkeypatch):
    """The facade, which loads the repo store and the global one before selecting - the figure
    an `evaluate_policy` caller actually waits on."""
    _fill_store_to_the_cap(tmp_repo)
    monkeypatch.setattr(store, "resolve_repo", lambda _p: tmp_repo)
    diff = "\n".join(f"+ line {i}" for i in range(500)) + "\n+ # TODO fix this\n"

    stats = _pstats([_timed(lambda: policy_api.evaluate_operation(
        tmp_repo, operation="commit", files=[f"src/module_{i}.py" for i in range(5)],
        artifact_kind="diff", artifact=diff)) for _ in range(_SAMPLES)])
    _report(f"evaluate_operation at {store.MAX_ENTRIES} decisions ({_SAMPLES})", stats)

    assert stats["p99"] < 500.0, f"evaluate_operation too slow: {stats['p99']:.3f}ms"
