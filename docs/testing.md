# Tests

## Running locally

```bash
# Install dev dependencies
uv sync

# Run all tests (includes coverage gate — must stay ≥85%)
uv run pytest tests/

# Run a single file
uv run pytest tests/test_store.py

# Run a specific test class or method
uv run pytest tests/test_store.py::TestSanitizeDirective
uv run pytest tests/test_store.py::TestSanitizeDirective::test_profanity_stripped

# Run without coverage (faster for quick iteration)
uv run pytest tests/ --no-cov

# Show which lines are not covered
uv run pytest tests/ --cov=contexer --cov-report=term-missing
```

## File guide and recommended order

Run in this order when verifying a significant change:

| File | What it covers | When to run |
|---|---|---|
| `test_store.py` | All store logic — novelty filter, task capture, constraint detection, sanitization, bootstrap scan, `is_simple_repo` detection, session start breakdown | Always |
| `test_e2e.py` | End-to-end hook sequences — install, session start, bootstrap trigger, constraint capture, `PostCompact` re-offer, decision storage | Always |
| `test_global.py` | Global store — `update_global_decision`, `get_global_context`, cross-repo rules | When touching global store or session start |
| `test_install.py` | `contexer install` / `uninstall` hook registration and migration | When touching `cli.py` |
| `test_cli_commands.py` | CLI commands — `status`, `version`, `reinstall`, `uninstall --purge`, dispatch, and `status` resilience against corrupt config/store files | When touching `cli.py` |
| `test_benchmark.py` | Hit/miss rates, token cost, novelty filter at scale, rationale injection accuracy | When changing filter logic or `get_context_for_prompt` |
| `test_benchmark_extended.py` | Noise tolerance, edge cases for constraint detection and rationale matching | When changing `_is_prescriptive_constraint`, `_sanitize_directive`, or `get_context_for_prompt` |

## What each test suite validates

**`test_store.py`** — unit tests for every function in `store.py`:
- `_is_novel`, `_passes_filter` — novelty filter (70% token overlap)
- `_is_prescriptive_constraint` — directive detection (always/never/ensure/make sure/from now on)
- `_sanitize_directive` — profanity stripping, frustration opener removal, trailing filler, caps normalisation, sarcasm exclusion
- `capture_user_constraint` — full pipeline: detect → sanitize → store (returns tuple)
- `get_session_start_context` — constraints/conventions pre-loaded, arch/patterns deferred, bootstrap offered when no context
- `bootstrap_scan` — repo file scanning, `is_simple_repo` suppression, gap question generation
- `get_context_for_prompt` — rationale injection, project-context overview fallback
- `TestAtomicSave` / `TestCorruptionRecovery` — atomic write invariants (temp file + `os.replace`, 0o600, failure cleanup) and corrupt-store recovery (truncated JSON reads as empty, next save rewrites a valid file)

**`test_e2e.py`** — integration tests simulating the full hook lifecycle:
- Install writes correct hooks (SessionStart, PostCompact, UserPromptSubmit)
- Bootstrap triggers on new repo, stays silent when context exists
- `get_post_compact_context` re-offers bootstrap after `/compact` with no context
- Bootstrap instructions tell Claude to ALWAYS show the offer, even for task prompts
- Constraint capture and decision storage across sessions

**`test_benchmark.py` / `test_benchmark_extended.py`** — accuracy benchmarks:
- Rationale hit rate ≥70% on real-world prompts
- Miss rate 100% for non-rationale prompts (zero false positives)
- Novelty filter blocks ≥90% of exact/near duplicates
- Token cost bounded at session start

## Coverage requirement

The CI gate requires **≥85% overall coverage**. The two excluded files are:
- `contexer/server.py` — thin MCP wrappers; tested indirectly through store
- `contexer/__main__.py` — CLI entry point

If you add a new function to `store.py`, add at least one test covering the happy path and the most likely failure mode before committing.
