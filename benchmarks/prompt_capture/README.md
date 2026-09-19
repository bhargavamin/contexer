# Prompt-capture benchmark

This deterministic benchmark measures prompt classification, extracted text, durable store
transitions, revision safety, recurrence, target selection, and host guidance independently.
It complements the applicability/relevance benchmark; retrieval quality cannot establish that a
prompt was captured safely.

Run the merge gate and write a reproducible report:

```bash
uv run python benchmarks/prompt_capture/run.py --format text
uv run python benchmarks/prompt_capture/run.py --format json \
  --output prompt-capture-report.json
uv run pytest tests/test_prompt_capture_benchmark.py --no-cov
```

`--strict` additionally fails while any declared known gap remains. Normal mode tolerates only
the exact assertion IDs registered in `cases.json`; crashes, unexpected failures, and unexpected
passes always fail. Timing is reported for visibility but is not a shared-runner CI gate. Measure
the fixed-hardware smoke budget locally with:

```bash
uv run pytest tests/test_prompt_capture_benchmark.py -m perf --no-cov
```

## Maintaining the corpus

Update this benchmark in the same change whenever prompt extraction/sanitization, lifecycle
targeting, capture persistence, proposal/review/history handling, recurrence, evidence recovery,
or host prompt adapters change.

- Add the reported prompt as a regression plus neighboring positive and negative cases. Include
  alternate service/environment names when the behavior is meant to be generic.
- Write the intended outcome before using the current implementation as an oracle. Never change
  gold labels merely to make a regression pass.
- Keep stable case and assertion IDs, bump `fixture_version` for semantic fixture changes, and
  add important regressions to `required_case_ids`.
- Register a real unresolved defect only at the exact failing assertion, with its baseline,
  rationale, and owner. Never xfail a scenario or a safety assertion. Remove registrations in
  the same change that fixes them; an XPASS is a failure until that cleanup happens.
- Preserve all twelve scenario families and the minimum corpus size enforced by the runner.
- Report semantic results, known gaps, unexpected failures, and timing in the PR handoff.

The runner uses disposable repositories and stores, disables optional update/proposal workers,
and never touches the developer's Contexer data. Adapter cases use the real evidence and capture
paths; only unrelated background work is disabled. The benchmark reports elapsed time, throughput,
and per-case p50/p95. These numbers characterize harness efficiency rather than production traffic
latency or natural-language accuracy.

The design rationale and adversarial acceptance checks live in
[`docs/prompt-capture-benchmark-plan.md`](../../docs/prompt-capture-benchmark-plan.md).
