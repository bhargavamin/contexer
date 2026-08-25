"""Repo-scoped policy evaluation: the ONE place an operation becomes a policy answer.

`policy.py` is pure — it judges a request against policies somebody else already loaded. The
four steps in front of that (resolve the repo, load the participating decisions, select,
evaluate) are the same for every general caller, and there are now two of them: the
`evaluate_policy` MCP tool and `contexer policy evaluate`. They exist here ONCE rather than
twice, following the `console_api.py` precedent: a thin layer ABOVE store, store-owned helpers
read through the store MODULE OBJECT at call time (so a value a test patches on
`contexer.store` is still seen), and a one-way dependency — `server.py`/`cli.py` ->
`policy_api` -> `store`/`policy`, never back. `policy.py` cannot host any of this: it must
never import store, and a store import here is exactly what that purity buys.

`guard_engine` is deliberately NOT a caller. It is the GIT adapter over the same evaluator and
owns things this layer has no concept of — staged paths, throttle sidecars, a wall-clock
budget — so routing it through here would mean teaching this module about git rather than
sharing anything real.

Two rules this module holds the line on:

BOUNDS. Every string and list bound is `policy.validate_request`'s, applied HERE, at the
chokepoint every caller funnels through — the same shape `remote.bound_source_files` has,
where the one definition is applied again below the surface because a surface schema only
covers the callers that go through that surface. A caller that skips the tool and the CLI
still gets bounded. The one bound a surface must apply for itself is the CLI's read of a diff
file, which has to stop before the bytes are in memory; it reads `policy.MAX_ARTIFACT_BYTES`
rather than stating a second number.

REDACTION IS EGRESS-ONLY. `format_result` scrubs, because rendering is where a secret leaves.
Nothing scrubs the artifact on the way IN — a `secret` check that sees redacted bytes detects
nothing, which is the whole point of `redact.py`'s egress-only rule — and nothing mutates the
structured result the caller receives. The result is authoritative; the text is a rendering.
"""

from contexer import policy      # pure stdlib leaf (no cycle): vocabularies, validation, judging
from contexer import redact      # pure stdlib leaf: the egress scrub
from contexer import store       # module object, not `from`-imports: see docstring above


def _participants(repo: str) -> list:
    """Every decision that may speak about this repo: its own store plus the global one.

    Both participate for the same reason they do in `guard_engine.decisions_for_files` — a
    cross-repo rule is still a rule here. `select_policies` does the filtering (status, trust,
    applicability), so nothing is pre-filtered on the way in; a global entry simply carries no
    `source_files`, so it can only ever select as an armed rule."""
    return ((store.load(repo).get("entries") or [])
            + (store.load_global().get("entries") or []))


def _answer(repo: str, result: dict, errors: list) -> dict:
    """One evaluation answer plus the provenance a renderer needs: which repo answered, and
    what (if anything) stopped the request at the gate."""
    return {**result, "repo_path": repo, "errors": list(errors)}


def _rejected(repo: str, errors: list) -> dict:
    """A request that never reached the evaluator.

    Shaped like every other answer so a renderer has one shape to handle, and reported as
    `allow` + `error` for the reason `build_result` gives: the verdict says what was found
    (nothing was judged, so nothing objected) and `evaluation_status` says the evaluation did
    not happen. Reading the verdict alone was never safe here and still isn't. `unchecked` is
    empty rather than dishonest — no policy was ever selected, so there is no policy to name
    as unjudged; `errors` is the report."""
    return _answer(repo, policy.build_result("allow", "error", "deterministic", [], [],
                                             policy.policy_set_version([])), errors)


def evaluate_operation(repo_path: str, *, intent: str = "", operation: str,
                       files=None, artifact_kind: str = "", artifact: str = "",
                       unchecked=None) -> dict:
    """Evaluate one operation against this repo's stored policies. Never raises for a bad
    REQUEST — a malformed one comes back as `errors` on an `error`-status answer.

    `artifact_kind`/`artifact` are the flat spelling of `policy`'s nested artifact, because
    both surfaces are flat-argument ones (an MCP tool schema, a CLI flag). The artifact is
    built whenever EITHER is non-empty, so content handed over without a kind is an error
    from `validate_request` rather than content silently dropped. Neither one means the
    request genuinely carries no artifact, and every armed policy then reports `omitted` —
    which is the distinction the guard learned the hard way, and is why a caller that could
    not READ its artifact must pass no artifact plus an `unchecked` row, never empty content.

    `unchecked` is the caller's own gap list (`policy.UNCHECKED_REASONS`), for exactly that:
    a diff file too large to hand over is reported beside the policies it prevented judging,
    not silently absent. Rows are `policy.evaluate_policies`' to validate — a malformed one
    raises there, deliberately, because swallowing a caller's broken gap report turns their
    bug into a false clean verdict.
    """
    repo = store.resolve_repo(repo_path)
    if not repo:
        return _rejected(repo, ["repo path not detected"])

    request = {"intent": intent, "operation": operation, "repo_key": repo,
               "files": list(files or []),
               "artifact": {"kind": artifact_kind, "content": artifact}
               if (artifact_kind or artifact) else None}
    normalized, errors = policy.validate_request(request)
    if errors:
        return _rejected(repo, errors)

    selected = policy.select_policies(_participants(repo), normalized)
    return _answer(repo, policy.evaluate_policies(selected, normalized, unchecked), [])


# ── rendering (the egress boundary) ──────────────────────────────────────────────

def _match_line(match: dict, lines: list) -> str:
    """One violated policy, as a line a human or a model reads.

    Both ids, always: `decision_id` is which decision objected and `revision_id` is which
    wording of it did — the same pairing `policy._match` documents, since a decision whose
    text has moved on has not made this objection.

    The offending source line is quoted when the caller supplied the artifact, because a line
    number alone sends the reader back to a file to find out what happened. This is the one
    place a secret can reach the output — `format_result` is what scrubs it, once, for the
    whole render."""
    line = match.get("line")
    quoted = lines[line - 1].strip() if isinstance(line, int) and 0 < line <= len(lines) else ""
    where = f" line {line}" if isinstance(line, int) else ""
    note = f" — {match['message']}" if match.get("message") else ""
    return (f"  [{match['verdict']}] {match.get('title') or '(untitled)'}"
            f" ({match.get('decision_id', '')} rev {match.get('revision_id', '')}){where}{note}"
            + (f"\n      {quoted}" if quoted else ""))


def format_result(result: dict, artifact: str = "") -> str:
    """Render one answer as text. The ONLY egress point in this module, and the only place
    `redact.scrub` is applied: the structured `result` the caller holds is never mutated, and
    the artifact reached the evaluator as real bytes (a scrubbed artifact is a `secret` check
    that finds nothing).

    Names the verdict, the evaluation status and the basis together, because none of the three
    means anything alone: `allow` with status `partial` is "nothing objected, and some of it
    was never checked". Every gap in `unchecked` is listed with its reason, for the same
    reason the guard lists its own — a check that did not happen must never read as a check
    that found nothing.
    """
    if result.get("errors"):
        body = ("Not evaluated — the request was refused at the gate:\n"
                + "\n".join(f"  - {e}" for e in result["errors"]))
        return redact.scrub_text(body)

    lines = artifact.splitlines() if artifact else []
    out = [f"verdict: {result['verdict']}  (evaluation_status: "
           f"{result['evaluation_status']}, basis: {result['basis']})"]

    matches = result.get("matches") or []
    out.append(f"matched {len(matches)} polic{'y' if len(matches) == 1 else 'ies'}:"
               if matches else "matched no policies.")
    out.extend(_match_line(m, lines) for m in matches)

    gaps = result.get("unchecked") or []
    if gaps:
        out.append(f"unchecked ({len(gaps)}) — these were NOT judged, not judged clean:")
        out.extend(f"  - {g.get('reason', '')}"
                   + (f" [{g['file']}]" if g.get("file") else "")
                   + (f" [decision {g['decision_id']}]" if g.get("decision_id") else "")
                   for g in gaps)

    out.append(f"policy_set: {result.get('policy_set_version', '')}")
    return redact.scrub_text("\n".join(out))
