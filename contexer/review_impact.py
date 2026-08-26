"""What approving THIS decision would actually do - the one interpretation the three review
surfaces share (hardening Task 07).

`review_pending` (MCP), `contexer review` (CLI) and the local console each used to decide for
themselves what a pending decision's evidence, files, history and policy effect were, which is
how the same decision could read as "would anchor: src/a.py" in the terminal and as nothing at
all in the console. Every category below is therefore computed ONCE, here, and rendered from
one `impact_lines` list; the console consumes the same dict structurally. Agreement is not a
convention the three surfaces keep, it is the only code path they have.

The module is a READ. It never approves, arms, anchors or restores anything, and it must stay
that way: the entire point of the block is that a developer reads it BEFORE signing, so a
surface that acted while describing would be describing its own side effects.

Two rules it enforces rather than merely renders:

* **Uncertain never reads as confirmed.** `possible_source_files` are labelled as files that
  will NOT be anchored, in their own section, with the reason. They live only on the candidate
  manifest in the spool (Task 04's ruling: they are never carried onto a proposal), so this is
  the one place they surface at all.
* **Approval is not arming.** The policy preview says what approval does (retrieval) and what
  it does not (blocking), and names the separate explicit gesture that would. Nothing here can
  create a rule; `guard_engine._armed_rules` is read to REPORT one that already exists.

Store-owned helpers are read through the `store` module OBJECT at call time, the load-order
discipline `guard_engine.py` documents, and the dependency runs one way only: store.py,
cli.py and console_api.py call in, and nothing here calls back into any of them.
"""

from contexer import store          # module object, not `from`-imports: see docstring above


# How a decision got captured, in the developer's terms rather than the schema's. Owned here
# because all three surfaces render it now; `cli._ORIGIN_LABELS` used to be the only copy.
ORIGIN_LABELS = {
    "human": "your prompt",
    "ai": "captured by the assistant",
    "plan": "from an approved plan",
    "scan": "measured from this repo",
    "bootstrap": "repo bootstrap",
    "memory": "imported from memory",
    "agent": "reported by the assistant",
}

# What each relationship type MEANS to a human reading the queue. Task 03's vocabulary is the
# aggregator's; this is its one translation, so MCP, CLI and console cannot describe the same
# link differently. An unknown relation renders under its own raw name rather than being
# dropped - a link nobody can name is still a link that was counted.
RELATION_LABELS = {
    "explicit": "the developer said it",
    "structural": "the same files or identifiers",
    "contradiction": "contradicts an earlier statement",
    "repetition": "restated independently",
    "causal_forward": "work that followed it",
    "validation": "a test run",
    "temporal_backward": "changed close in time, no shared file",
    "unrelated": "unexplained, left over",
}

# Verbatim from the Task 07 brief. It is normative text, so it is a constant rather than
# f-string fragments: an armed rule is reported BESIDE it, never by rewriting it.
POLICY_KNOWLEDGE = "Knowledge after approval: eligible for normal retrieval."
POLICY_BLOCKING = "Blocking after approval: none. Approval does not arm a guard rule."
POLICY_HOW_TO_ARM = (
    "To add a local deterministic block after approval, use `contexer guard arm` and review "
    "its exact pattern, paths, and message separately.")

_MAX_LISTED = 5          # rows per section before the honest "+N more" tail
_TITLE_CLIP = 80         # a similar decision is identified, not reproduced


def one_line(text: str) -> str:
    """Untrusted free text, collapsed to a single line.

    THE render-boundary guard, and the reason it exists is a reproduced injection. `impact_lines`
    returns one string per line and `store.format_pending_review` indents each by four spaces, so
    a field carrying a newline emits extra lines indented exactly like the surface's own action
    lines. A retirement reason is free text on the `retire_decision` MCP tool, so an assistant
    that read a crafted reason out of the repository could put
    `approve_decision(entry_id="...", action="approve")` into the payload as what looks like the
    surface telling itself to approve.

    Applied to EVERY untrusted string this module renders - the reason, any proposal or decision
    title, an armed rule's own pattern and paths - rather than to the field the injection was
    demonstrated on, because "which of these can contain a newline today" is not a property a
    render boundary should depend on. `impact_lines` re-applies it to every finished line as the
    module's own invariant: one item is one line.
    """
    return " ".join(str(text or "").split())


def _listed(items: list, limit: int = _MAX_LISTED) -> str:
    """`a, b, c (+2 more)` - the review surfaces' list clip, honest about what it dropped."""
    head = ", ".join(one_line(i) for i in items[:limit])
    extra = len(items) - limit
    return f"{head} (+{extra} more)" if extra > 0 else head


def evidence_index(repo_path: str) -> dict:
    """`{entry_id: [manifest, ...]}` for every held candidate that names a decision.

    ONE spool listing per render call, not one per decision: `format_pending_review` renders up
    to 25 entries and the console polls, so a per-entry scan would multiply directory reads by
    the size of the queue. Fail-soft to `{}` - a review that cannot read the spool renders
    without evidence rows rather than not rendering at all.
    """
    try:
        from contexer import spool
        index: dict = {}
        for candidate_id, meta in spool.held_candidates(repo_path).items():
            if not isinstance(meta, dict) or meta.get("unreadable"):
                continue
            entry_id = str(meta.get("entry_id") or "")
            if entry_id:
                index.setdefault(entry_id, []).append({**meta, "candidate_id": candidate_id})
        return index
    except Exception:
        return {}


def coverage_lines() -> list[str]:
    """One capture-coverage line per installed host, through `evidence.format_coverage`.

    Capabilities only (`pass_status=False`): a review runs no reconciliation pass, so rendering
    the block's runtime half would assert an outcome that never happened - the same reason
    `contexer status` passes it. Fail-soft: an undetectable host set reports `manual`, which is
    what an unknown host means everywhere else in the coverage vocabulary.
    """
    try:
        from contexer import adapters, evidence
        targets = adapters.detect() or []
        blocks = [evidence.host_coverage(a.NAME) for a in targets] or \
            [evidence.host_coverage("")]
        return [evidence.format_coverage(b, pass_status=False) for b in blocks]
    except Exception:
        return []


def review_context(repo_path: str, decisions: list | None = None) -> dict:
    """The per-render-call reads every entry's impact block shares: the loaded decisions, the
    held-evidence index and the host coverage. Built once by each surface and threaded in, so
    rendering a queue of 25 costs one store read, one spool listing and one host detection.

    `decisions` is for a caller that has already read the store this tick (the console's
    dashboard poll): handing them in is what keeps this from being a second whole-file parse
    inside a projection built to do exactly one.
    """
    if decisions is None:
        try:
            decisions = [e for e in store.load(repo_path).get("entries", [])
                         if e.get("type") == "decision"]
        except Exception:
            decisions = []
    return {"decisions": list(decisions), "evidence": evidence_index(repo_path),
            "coverage": coverage_lines()}


def _manifests_for(entry: dict, context: dict) -> list[dict]:
    return list((context.get("evidence") or {}).get(str(entry.get("id") or "")) or [])


def _pending_proposal(entry: dict) -> dict:
    """The proposal this review is actually answering, whichever lane owns it. Order matches
    the render sites': a reconsideration outranks a retirement outranks a content update,
    because each question is moot while the one above it is open."""
    for slot in ("proposed_reconsideration", "proposed_lifecycle", "proposed_revision"):
        prop = entry.get(slot)
        if prop:
            return {"slot": slot, **prop}
    return {}


def _live_manifest(entry: dict, context: dict) -> dict:
    """The held candidate this review answers: the one whose id the sitting proposal names,
    else the most recently created. A settled hold is skipped - its question is already
    answered and rendering its evidence would describe a decision nobody is being asked to
    make."""
    prop = _pending_proposal(entry)
    live = [m for m in _manifests_for(entry, context) if m.get("status") == "pending"]
    named = str(prop.get("candidate_id") or "")
    for meta in live:
        if named and meta.get("candidate_id") == named:
            return meta
    return max(live, key=lambda m: str(m.get("created_at") or ""), default={})


def _grouped(rows: list) -> list[dict]:
    """Signal rows collapsed to one row per relationship type, count first.

    Grouping rather than listing is what makes the section readable at a glance AND is the
    interpretation the brief forbids duplicating per surface: "3 x the same files or
    identifiers" is one fact about the candidate, not three."""
    counts: dict = {}
    for row in rows:
        relation = str(row.get("relation") or "unknown")
        bucket = counts.setdefault(relation, {"relation": relation, "count": 0,
                                              "certainty": str(row.get("certainty") or ""),
                                              "reasons": []})
        bucket["count"] += 1
        reason = str(row.get("reason") or "")
        if reason and reason not in bucket["reasons"]:
            bucket["reasons"].append(reason)
    # Strongest link first, by the vocabulary's own order, then by count, then by name. Not
    # by count alone: with one of each (the ordinary case) that degrades to alphabetical, so
    # "causal_forward" would lead a group whose whole meaning is the explicit directive at the
    # front of it. The third term keeps an unknown relation deterministic.
    order = list(RELATION_LABELS)
    return [{**b, "label": RELATION_LABELS.get(b["relation"], b["relation"])}
            for b in sorted(counts.values(),
                            key=lambda b: (order.index(b["relation"])
                                           if b["relation"] in order else len(order),
                                           -b["count"], b["relation"]))]


# The lanes whose answer is a MOVE rather than a content approval. Neither
# `lifecycle.reconsider_decision` nor `lifecycle.retire_decision` calls `store._anchor_sources`
# (grep: `_anchor_sources` does not appear in lifecycle.py at all), so nothing they do writes
# `source_files` and a "would anchor" line on either is a promise the code never keeps.
_NON_ANCHORING_SLOTS = ("proposed_reconsideration", "proposed_lifecycle")


def _origin_label(source) -> str:
    """One provenance value in the developer's terms; an unknown one keeps its raw spelling
    rather than vanishing, since "captured by something this version does not know" is still
    more than nothing."""
    raw = one_line(source)
    return ORIGIN_LABELS.get(raw, raw)


def _confirmed_anchors(entry: dict) -> list[str]:
    """Exactly the files `_apply_approval` would write as `source_files`, in its own
    precedence order: a proposal's own stashed anchor, else the capture-time candidates.

    Never `possible_source_files` and never a similar decision's files - this list is what the
    action confirmation repeats back, so anything speculative in it would be a wrong anchor
    signed by a human who was told it was confirmed.

    LANE-AWARE, because `_apply_approval` is not the only answer a review has. Restoring an
    inactive decision and retiring a live one are state MOVES: they run through
    `lifecycle.py`, which anchors nothing, and a reconsideration proposal genuinely carries
    `source_files` (reconcile passes the candidate's confirmed files), so ranking it first here
    printed "Will anchor on approval: src/generated/client.ts" about a restore that writes no
    anchor at all. Those files are real evidence and are rendered as `_evidence_files` under a
    label that promises nothing; a retirement's fall-through to `anchor_candidates` was the
    same false claim one lane over, with a staler input.
    """
    prop = _pending_proposal(entry)
    if prop.get("slot") in _NON_ANCHORING_SLOTS:
        return []
    if prop.get("source_files"):
        return [str(f) for f in prop["source_files"]]
    return [str(f) for f in entry.get("anchor_candidates") or []]


def _evidence_files(entry: dict) -> list[str]:
    """The files a NON-ANCHORING proposal was observed with: confirmed evidence, not an anchor.

    The distinction this whole block exists to keep: they are confirmed (a structural link, not
    a temporal guess), so they belong beside the decision, and they are not anchors, because the
    action being offered writes none. Empty for every lane whose answer does anchor - there the
    files are already the `Would anchor` line and saying them twice would blur the two labels
    back together.
    """
    prop = _pending_proposal(entry)
    if prop.get("slot") not in _NON_ANCHORING_SLOTS:
        return []
    return [str(f) for f in prop.get("source_files") or []]


def _possible_files(entry: dict, meta: dict) -> list[str]:
    """The uncertain paths, from the candidate manifest ONLY.

    They are deliberately absent from every proposal (Task 04 ruling): a backward temporal link
    is not evidence, so nothing that could become an anchor is allowed to carry it. Anything
    already CONFIRMED is filtered out, by either label - a path that earned its way into the
    anchor list, or into the evidence list, is not also a maybe."""
    confirmed = (set(_confirmed_anchors(entry)) | set(_evidence_files(entry))
                 | set(entry.get("source_files") or []))
    return [str(f) for f in (meta.get("candidate") or {}).get("possible_source_files") or []
            if str(f) not in confirmed]


def _inactive_history(entry: dict) -> dict:
    """Why this decision is not live, and how often the question has been asked before.

    The retirement reason comes off the decision's own lifecycle record rather than the
    proposal, because that is what a restoration would be undoing."""
    from contexer import lifecycle

    if not entry.get("proposed_reconsideration"):
        return {}
    state = "retired" if entry.get("deleted_at") else "ignored"
    record = next((r for r in reversed(entry.get("lifecycle") or [])
                   if isinstance(r, dict) and r.get("kind") in lifecycle.RETIRED_KINDS), {})
    dismissed = [r for r in entry.get("reconsideration_history") or []
                 if isinstance(r, dict) and r.get("disposition") == "dismissed"]
    return {
        "state": state,
        "since": str(record.get("occurred_at") or entry.get("deleted_at") or "")[:10],
        # Untrusted free text: `retire_decision`'s `reason` is an MCP tool parameter, so it is
        # collapsed to one line BEFORE clipping (see `one_line`) rather than after.
        "reason": store.clip_body(one_line(record.get("reason")), 200),
        "replacement": str(record.get("replacement_decision_id") or "")[:8],
        "dismissed": len(dismissed),
        "last_dismissed": str((dismissed[-1].get("occurred_at") if dismissed else "") or "")[:10],
    }


def _revisions(entry: dict, meta: dict) -> dict:
    """Current revision id vs the revision the sitting proposal was formed against.

    A content proposal carries no basis of its own (it is only ever attached with HEAD
    unmoved), so the manifest's `basis_revision_id` is the fallback. `stale` is the lane's own
    verdict where it has one, never a fresh comparison - two answers to "is this stale" is how
    a surface ends up offering an action the owner refuses.
    """
    from contexer import lifecycle, revisions

    current = str(entry.get("current_revision_id") or "")
    prop = _pending_proposal(entry)
    basis = str(prop.get("basis_revision_id") or meta.get("basis_revision_id") or "")
    if prop.get("slot") == "proposed_reconsideration":
        stale = lifecycle.reconsideration_stale(entry)
    elif prop.get("slot") == "proposed_lifecycle":
        stale = lifecycle.lifecycle_proposal_stale(entry)
    else:
        stale = bool(basis) and bool(current) and basis != current
    rev = revisions.current_revision(entry) or {}
    return {"current": current, "version": rev.get("version_number", entry.get("revision", 1)),
            "basis": basis, "stale": stale}


def _armed_rule(entry: dict) -> dict:
    """The armed Tier-2 rule already on this decision, or `{}`.

    Read through `guard_engine._armed_rules`, the same selector the commit-time guard runs, so
    the preview cannot claim a rule is inert that the guard would fire (or the reverse). It is
    a REPORT: nothing in this module can arm, and an approval never will."""
    try:
        from contexer import guard_engine
        if not guard_engine._armed_rules([entry]):
            return {}
        check = entry.get("guard_check") or {}
        return {"type": one_line(check.get("type")), "pattern": one_line(check.get("pattern")),
                "paths": one_line(check.get("paths")),
                "message": store.clip_body(one_line(check.get("message")), 200)}
    except Exception:
        return {}


def _displaced(entry: dict, context: dict) -> list[str]:
    """Held reconsideration candidates for this decision that ask about a DIFFERENT revision
    than the sitting question (Task 04's residual, ledgered to this task).

    Such a hold can never be settled by the answer on screen - its question genuinely was not
    the one asked - so nothing judges it and it is invisible to `held_unattributed`, which
    counts holds naming no entry at all. Naming it here is the whole fix: the developer learns
    which decision and which basis is stuck, and a fresh restatement is what clears it.
    """
    live = _live_manifest(entry, context)
    if not live:
        return []
    basis = str(live.get("basis_revision_id") or "")
    out = []
    for meta in _manifests_for(entry, context):
        if meta.get("status") != "pending" or meta is live:
            continue
        other = str(meta.get("basis_revision_id") or "")
        if other and other != basis:
            out.append(f"another restatement of {str(entry.get('id') or '')[:8]} is held "
                       f"against revision {other[:8]}, which is not the one under review "
                       f"({basis[:8] or 'unknown'}); it stays held until a fresh restatement "
                       f"raises it")
    return out


def review_impact(repo_path: str, entry: dict, context: dict | None = None) -> dict:
    """Everything the three review surfaces render about ONE pending decision.

    `context` is `review_context(repo_path)`, built once per queue by the caller; omitting it
    is supported for a single-decision caller and costs that caller the shared reads.
    """
    context = review_context(repo_path) if context is None else context
    meta = _live_manifest(entry, context)
    candidate = meta.get("candidate") or {}
    prop = _pending_proposal(entry)
    possible = _possible_files(entry, meta)
    # The thing under review is the PROPOSAL when one is sitting, so its provenance is the
    # proposal's own `source`. Reading the entry's `created_by` told a developer that an
    # ai-written rewrite of their own decision came from "your prompt" - the standing entry's
    # provenance attached to text they never wrote, which is the exact mistaken approval this
    # block exists to prevent. The standing origin is kept beside it, never replaced by it.
    standing = _origin_label(entry.get("created_by"))
    proposed = _origin_label(prop.get("source")) if prop else ""
    return {
        "identity": {
            "id": str(entry.get("id") or ""),
            "title": one_line(prop.get("title") or entry.get("title") or ""),
            "subtype": entry.get("subtype") or "decision",
            "status": store.entry_status(entry),
            "origin": proposed or standing,
            "standing_origin": standing,
            "lane": prop.get("slot", ""),
        },
        # Labelled at the point of rendering, never as a bare number: it ranks a review queue
        # and says nothing about whether the statement is true (candidates.py's own rule).
        "priority": {"score": candidate.get("score"),
                     "label": "review priority (ranking only, not a probability)"},
        # THREE tiers, not two. Task 03 made `supporting` a separate certainty on purpose
        # (repetition / causal_forward / validation), and folding it into "confirmed" reported
        # an inference as an observation - 30 edits that merely followed a directive read as 30
        # confirmations of it, which is the observed-versus-inferred line this whole block is
        # for. Uncertain rows come from `uncertain_signals` plus any signal row whose own
        # certainty says so (a red test run is `validation` at `uncertain`).
        "evidence": {
            "confirmed": _grouped(_tier(candidate, "confirmed")),
            "supporting": _grouped(_tier(candidate, "supporting")),
            "uncertain": _grouped(_tier(candidate, "uncertain")
                                  + list(candidate.get("uncertain_signals") or [])),
            "receipts": len(entry.get("evidence_summary") or []),
            "dropped": int(entry.get("evidence_summary_dropped") or 0),
        },
        # `possible_source_files` keeps the aggregator's own spelling all the way to the edge.
        # It is the name `tests/test_evidence_hardening_replays.py`'s structural scan watches,
        # so a future reader anywhere in the package is caught by a test that already exists -
        # renaming it to `possible` here would have created a second spelling that no guard
        # looks for, which is a weaker ban than the one this task narrowed.
        "files": {"confirmed": _confirmed_anchors(entry), "evidence": _evidence_files(entry),
                  "possible_source_files": possible},
        "coverage": list(context.get("coverage") or []),
        "related": {
            "similar": store.similar_decisions(entry, context.get("decisions") or []),
            "conflict": _has_conflict(entry),
        },
        "history": _inactive_history(entry),
        "revisions": _revisions(entry, meta),
        "policy": {"knowledge": POLICY_KNOWLEDGE, "blocking": POLICY_BLOCKING,
                   "how_to_arm": POLICY_HOW_TO_ARM, "armed": _armed_rule(entry)},
        "diagnostics": _displaced(entry, context),
    }


def _tier(candidate: dict, certainty: str) -> list:
    """One certainty tier's signal rows. A row with no certainty recorded (a manifest written
    before Task 03's typing) counts as `confirmed` for nothing - it lands in no tier rather
    than being promoted into one."""
    return [row for row in candidate.get("signals") or []
            if isinstance(row, dict) and row.get("certainty") == certainty]


def _has_conflict(entry: dict) -> bool:
    from contexer import conflicts
    return conflicts.has_open_conflict(entry)


def impact_lines(impact: dict, seen_coverage: set | None = None) -> list[str]:
    """The block, as text, for BOTH text surfaces. One list, one label vocabulary: the MCP
    tool indents it and the CLI prints it, and neither gets to phrase a category its own way.

    A section with nothing to say is OMITTED rather than rendered empty - "Would anchor: none"
    reads as a checked-and-empty anchor list, and a reviewer who sees no line asks, which is
    the safer of the two silences. The policy preview is the one section that always renders:
    what approval does NOT do is exactly the thing a reviewer cannot infer from absence.

    `seen_coverage` is for a LIST surface rendering a whole queue in one payload: capture
    coverage is a property of the installed hosts, identical for every decision in the render,
    so `review_pending` threads a set through and prints each host's line once instead of 25
    times. The interactive surfaces pass nothing - a terminal screen and a console card are
    each read on their own, where the line is a fact the reader has not just seen.

    Every finished line goes through `one_line`: one item is one line, and the surfaces indent
    what they are given, so a field carrying a newline would emit forged lines dressed as the
    surface's own. The fields are collapsed where they enter the dict too - this is the
    invariant, not the only guard.
    """
    lines = []
    identity = impact.get("identity") or {}
    if identity.get("origin"):
        standing = identity.get("standing_origin") or ""
        # Both, whenever the proposal and the standing decision disagree: "this rewrite came
        # from the assistant, the decision under it came from you" is one fact a reviewer needs
        # whole, and either half alone misattributes something.
        detail = (f"{identity['origin']} (the version under review); "
                  f"the standing decision came from {standing}"
                  if standing and standing != identity["origin"] else identity["origin"])
        lines.append(f"Origin: {detail}")
    score = (impact.get("priority") or {}).get("score")
    if score is not None:
        lines.append(f"Review priority: {score} - "
                     "ranking only, not a probability that this is correct")

    evidence = impact.get("evidence") or {}
    # Three headings for three tiers. "Supporting" is not "Confirmed": a test run and an edit
    # that merely followed a directive corroborate it, they do not witness it.
    for key, label in (("confirmed", "Confirmed evidence"),
                       ("supporting", "Supporting evidence"),
                       ("uncertain", "Possibly related")):
        rows = evidence.get(key) or []
        if rows:
            lines.append(f"{label}: " + _listed(
                [f"{r['count']}x {r['relation']} ({r['label']})" for r in rows]))
    if evidence.get("receipts") or evidence.get("dropped"):
        tail = (f", {evidence['dropped']} older receipt(s) dropped"
                if evidence.get("dropped") else "")
        lines.append(f"Evidence receipts: {evidence.get('receipts', 0)} recorded{tail}")

    files = impact.get("files") or {}
    if files.get("confirmed"):
        lines.append(f"Would anchor: {_listed(files['confirmed'])}")
    if files.get("evidence"):
        lines.append(f"Evidence files: {_listed(files['evidence'])} - observed with this "
                     "restatement; answering it does NOT anchor them")
    if files.get("possible_source_files"):
        lines.append(f"Possible files: {_listed(files['possible_source_files'])} - NOT anchored "
                     "on approval; the link is uncertain")
    for line in impact.get("coverage") or []:
        if seen_coverage is not None:
            if line in seen_coverage:
                continue
            seen_coverage.add(line)
        lines.append(f"Capture coverage: {line}")

    related = impact.get("related") or {}
    if related.get("similar"):
        lines.append("Similar decisions: " + _listed(
            [f"{s['id'][:8]} \"{store.clip_body(one_line(s['title']), _TITLE_CLIP)}\""
             + (" [open conflict]" if s.get("conflict") else "") for s in related["similar"]]))
    if related.get("conflict"):
        lines.append("Open conflict: this decision already carries an unreviewed update; "
                     "the standing version stays operative until you rule on it")

    history = impact.get("history") or {}
    if history:
        parts = [f"{history['state']}" + (f" on {history['since']}" if history.get("since") else "")]
        if history.get("reason"):
            parts.append(f'reason: "{history["reason"]}"')
        if history.get("replacement"):
            parts.append(f"replaced by {history['replacement']}")
        if history.get("dismissed"):
            parts.append(f"asked before: dismissed {history['dismissed']} time(s)"
                         + (f", most recently {history['last_dismissed']}"
                            if history.get("last_dismissed") else ""))
        lines.append("Inactive history: " + "; ".join(parts))

    rev = impact.get("revisions") or {}
    if rev.get("current") or rev.get("basis"):
        detail = f"current {rev.get('current', '')[:8]} (v{rev.get('version', 1)})"
        if rev.get("basis"):
            detail += f", proposed against {rev['basis'][:8]}"
        if rev.get("stale"):
            detail += " - STALE, the decision moved since"
        lines.append(f"Revisions: {detail}")

    policy = impact.get("policy") or {}
    lines.append(policy.get("knowledge", POLICY_KNOWLEDGE))
    armed = policy.get("armed") or {}
    if armed:
        # Built from the parts that EXIST rather than from a fixed shape with holes punched in
        # it: a `secret` rule takes no pattern and often no paths, and the fixed shape rendered
        # `armed secret rule ()` and `(, paths src/*.py)`.
        detail = [f"{key} {armed[key]}" for key in ("pattern", "paths") if armed.get(key)]
        scope = f" ({', '.join(detail)})" if detail else ""
        lines.append(f"Blocking after approval: this decision ALREADY has an armed "
                     f"{armed.get('type') or 'guard'} rule{scope}. The approved revision on "
                     "record stays operative until you review this update; approving does not "
                     "change the rule.")
    else:
        lines.append(policy.get("blocking", POLICY_BLOCKING))
    lines.append(policy.get("how_to_arm", POLICY_HOW_TO_ARM))
    for note in impact.get("diagnostics") or []:
        lines.append(f"Held evidence: {note}")
    return [one_line(line) for line in lines]


def anchor_confirmation(entry: dict) -> str:
    """The one sentence an action confirmation repeats back before it writes an anchor.

    Confirmed files only, spelled out in full rather than counted: the informed-signature rule
    is about the developer having SEEN the paths at the moment they signed. `_confirmed_anchors`
    is lane-aware, so a restore or a retirement - neither of which anchors anything - says so
    rather than reading back the evidence files as if they were about to be written."""
    files = _confirmed_anchors(entry)
    if not files:
        return "No files will be anchored by this approval."
    return one_line(f"Will anchor on approval: {', '.join(files)}")
