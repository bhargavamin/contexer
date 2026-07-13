# Red-team review — campaign3 (adversarial, generated before publication)

Reviewer: adversarial agent instructed to refute every claim. All numbers recomputed from runs.jsonl.

## Challenges

1. **FATAL — seeded-secret design.** The rationale/continuity facts exist nowhere except condition B's store (fixture grep: 0 hits for postgres/mysql/transactional/compliance/ULID/"never log"). "Without" answered correctly that no such decision exists and scored 0.0. The comparison is contexer-vs-NO-PERSISTENCE, not contexer-vs-stock-Claude-Code. The descoped CLAUDE.md-only condition C is the honest competitor and must be added before any "beats stock" claim.
2. **FATAL/SERIOUS — ordering confound.** All 33 "without" rows ran as one block before all 33 "with" rows; no per-row timestamps. Server-side drift/cache warming/time-of-day fully confounded with condition. Small deltas (turns 8v9, cost, chain tokens) cannot be attributed. Fix: interleave conditions, record timestamps.
3. **SERIOUS — OTel cross-check inert.** telemetry_ok None on all 66 rows; token/cost figures are single-source (claude -p JSON). Must be disclosed (claude 2.1.207 emitted no OTel under the documented env vars).
4. **SERIOUS — aggregate medians favor WITHOUT.** tokens +17%, cost +11.6%, turns 8→9 with contexer across the whole suite; with loses 40% of paired token comparisons (13/33). Wins are task-type-specific (rationale/continuity). Reports must lead with the aggregate.
5. **SERIOUS — substring scoring artifacts.** Correct "no such decision exists" scores 0.0; verbatim parroting scores 1.0; rat-errors' second gold substring ("machine-parseable") never uttered → 0.5s drag the "0.75" down as a scorer artifact, not model quality.
6. **SERIOUS — violations metric floor effect.** Only snake_case+type-hints checked; fixture is 100% clean on both, so the metric can only rise. The chain's actual rule (ULID ids) is unmeasured by it; the chain-3 check passed 3/3 BOTH conditions — discriminates nothing.
7. **SERIOUS — N=3.** Chain step-2 ranges overlap almost completely (without [569k, 793k, 413k] vs with [377k, 373k, 800k]); chain compounding is anecdote at this N. Spec itself demands 8–10 for publishable numbers.
8. **MINOR — chain step-1 prompt primed** ("IMPORTANT project rule you should record") — advertises the capture mechanism; no measured effect (both conditions fail step-1 checks 3/3 equally).
9. **MINOR — continuity 1/3 "without" pass is luck**, not knowledge (rule was a seeded secret; same class as #1).

## Verdict

- **Publishable (with N caveat + reworded framing):** rationale directional result and continuity 3/3-vs-1/3 as "recorded memory vs no persistence"; the honest +17% single-shot overhead.
- **Rewording needed:** drop "vs 0.00" as a quality signal; report the 13 paired token losses; disclose the inert OTel channel.
- **More data / redesign needed:** chain compounding (N≥8, interleaved, timestamps); condition C (CLAUDE.md-only); scorer fixes (credit correct ignorance, measure the chain rule, broaden violations).

Bottom line: campaign3 demonstrates "recorded memory beats no memory" — real but narrower than "contexer makes Claude Code cheaper and more accurate."
