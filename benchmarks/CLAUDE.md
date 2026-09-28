# Benchmarks: agent guidance

Loaded automatically by Claude Code when files under `benchmarks/` are read. Other agents: read
this before changing a benchmark. Prompt-capture benchmark maintenance rules stay in the root
`CLAUDE.md` because they apply to production capture code too; its runnable guide is
[`prompt_capture/README.md`](prompt_capture/README.md).

## Experiment and readiness boundaries

- **Contract 06 experiment boundary** - `store.get_context_for_prompt*` accepts one keyword-only internal `ordinary_task_v1` variant. It may open only a gate the existing indexed router would otherwise leave closed, then reuses the question-only discriminative safeguard, unchanged ranker/caps/renderer, revision-aware working set, and existing authority rules. It is not an MCP parameter, install setting, environment switch, or production shadow mode. `benchmarks/applicability/task_outcome_experiment.py` is the offline-only owner of its synthetic baseline/candidate runs, protected stub validators, deterministic aggregation, and reports; it never authorizes a live agent run.
- **Contract 07/08 readiness boundary** - `benchmarks/applicability/pilot_readiness.py` owns the offline campaign manifest, readiness gates, approval-bound ledger, canary validation, and outcome analysis; its public CLI cannot launch a live host. `benchmarks/applicability/qualification_preparation.py` owns bounded explicit-root imports, the frozen six-task core, blinded review artifacts, append-only exposure/run histories, deterministic baseline/candidate collection, and the versioned source-bound qualification projection consumed by Contract 07 schema 2. Development and legacy evidence remain diagnostic, adapter emission is not host receipt, and neither owner can create human review, budget approval, timing acceptance, or live eligibility.
