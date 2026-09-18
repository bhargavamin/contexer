# Incremental bootstrap: acceptance and adversarial review

## Product standard

An ordinary edit must not discard unrelated grounded capture. The user sees actual saved,
protected or deferred outcomes, not another fact-confirmation interview. Capture completion
does not assert current applicability or architectural correctness. Only concrete conflicts
produce a clarification, with the complete evidence group available.

This fixes a reproducible failure; its real-world frequency has not been measured. No claim
of an adoption-rate improvement follows from unit tests. Semantic interpretation still needs
the host agent and bounded source inspection; citation validation cannot prove inferred intent.

## Executable gates

The tests in `tests/test_bootstrap_heads.py` and `tests/test_bootstrap_applicability.py` cover:

- Freshness → exact revert does not invalidate an in-flight report. Human approval, editing
  and dismissal still do. A loaded legacy entry without status has the approved default.
- Three parsed facts, two model findings and one human decision: an uncited file addition,
  followed by repeated refresh, produces exactly two inventory caveats, zero withheld
  entries and six non-ignored entries. A matching assessment clears those caveats and they
  stay cleared after repeated refresh.
- A second inventory change invalidates an acknowledgment of the first. A human correction
  invalidates acknowledgment under the old policy basis. Assessment cannot lift invalid evidence.
- Citation-budget and inventory warnings coexist and clear independently. Legacy string
  warnings render, survive unavailable checks and clear after successful citation verification.
- A real competing store-lock holder causes no store-file write. Only the rendering gains an
  unavailable-check caveat. Already-known stale withholding remains intact.
- Failed reads are uncertainty; revoked authorization and missing sources are invalid.
  Disappeared external documentation does not block rescanning other sources.
- A stale citation defers its finding without discarding a valid peer. Five consecutive uncited
  edits preserve one decision UUID/revision, and a final assessment converges without warnings.
- Deleted reported observations are not replayed. Reworded rules revise the explicit UUID;
  repeated late reports cannot recreate a superseded observation.
- Monotonic analysis generation prevents A → B → A token reuse. Refresh does not advance it.
- A partial conflict report and repeated replay preserve the whole pending group, including
  the stale member. The derived worklist defers its question; rechecking restores the full group.
- Malformed new durable fields refuse writes. Duplicate document candidates with different
  scopes still reject the batch. The existing MCP tool exposes exact-delta assessment.

## Adversarial findings resolved during implementation

The first isolated commit fixed decision heads before changing admission. Subsequent code review
found two additional replay hazards: superseded observations could be recreated by old batches,
and filtering stale report rows could erase the other side of a conflict. Both now have direct
regression tests. A duplicate candidate could also evade batch-wide validation when each finding
was validated separately; identity is now checked across the entire batch.

This was a local adversarial pass with executable counterexamples, not an independent model
review. The full-suite result should be recorded on the PR for the exact committed version.

## Cost and scope

A synthetic maximum report set (80 findings, eight long excerpts each) measured approximately
3.52 MB before applicability metadata and 3.53 MB afterward: **18,500 additional bytes**.
The deterministic test caps the extra serialized metadata below 30 KB. Diagnostic mean store
saves were roughly 68/77 ms in one local run; these are not a latency SLO or an isolated benchmark.
The existing repeated citation/revision storage dominates that fixture. This change stores no
full source blobs and introduces no retained-source egress path.

Delta path summaries are capped at 40 paths, observation receipts at the 20 nominated candidates,
and report storage retains the existing 80-finding bound. Omitted coverage remains explicit.
Unavailability is render-only and the conflict recheck worklist is derived, not persisted.
Fuzzy identity matching, distributed report merging and full-source retention remain out of scope.
