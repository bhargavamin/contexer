"""Deterministic grouping and scoring of evidence events into decision candidates.

A candidate is a PROPOSAL: the group of evidence events that, taken together, look like one
engineering decision worth a developer's attention. This module is pure — it reads validated
events (`evidence.validate_event` shapes) plus a read-only projection of the existing
decisions, and returns dicts. It writes nothing and imports nothing that does; the only
import is `retrieval`, itself a leaf, for the ONE tokenizer this repo has.

`score` is an AGGREGATION-RANKING INPUT, never a confidence that a statement is true. 50 for
a user directive means "a developer said this out loud, put it near the top of the review
queue" — not "this is 50% likely to be correct". Nothing here approves, enforces, retires or
materializes anything: the review flow is the only gate.

Idempotency is the load-bearing property. `candidate_id` is a uuid5 over the sorted
contributing event ids, events are ordered by (occurred_at, event_id) before anything reads
them, and candidates come back sorted by (-score, candidate_id). The same event set in ANY
input order therefore produces byte-identical output. A `uuid4` in this module is a defect.
"""

import re
import uuid

from contexer import retrieval

# Fixed namespace for candidate ids. Derived, not a magic literal, and never regenerated:
# changing this string renames every candidate in every ledger.
_CANDIDATE_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://contexer.dev/evidence/candidate")

# A seed carries a SEMANTIC STATEMENT (something was decided/said); support events corroborate
# one. Kinds in neither set never group — they are ledger bookkeeping about candidates, not
# evidence for one.
SEED_KINDS = frozenset({"user_directive", "agent_conclusion", "decision_repeated"})
SUPPORT_KINDS = frozenset({"file_changed", "test_result", "diff_observed"})
# Which kinds contribute PATHS. `test_result` files name the tests, not what was decided.
_FILE_KINDS = frozenset({"file_changed", "diff_observed"})

# Workstream B2's table, in one object. Ranking weights, not probabilities. Each entry has a
# test in tests/test_candidates.py explaining what its number buys at the review bar.
_SCORES = {
    "user_directive": 50,
    "agent_conclusion_with_rationale": 25,
    "agent_conclusion_bare": 15,
    "repeated_independently": 15,
    "files_changed": 10,
    "tests_validate": 10,
    "contradiction": -30,
}
_MIN_CANDIDATE_SCORE = 25

# Token-overlap bars (|A∩B| / |smaller|, over `retrieval.index_tokens`).
_MERGE_OVERLAP = 0.7            # a later seed restates an existing group's seed
_CONTRADICTION_OVERLAP = 0.5    # the lowered bar a NEGATING seed merges on
_DUPLICATE_OVERLAP = 0.7        # candidate already stored
_RETIRE_OVERLAP = 0.5           # candidate negates a stored decision
_UPDATE_OVERLAP = 0.3           # candidate revises a stored decision

_MAX_SOURCE_FILES = 10          # the store's own anchor cap; a candidate must not exceed it
_MAX_TITLE_CHARS = 100

_NEGATION_RE = re.compile(r"\b(?:not|never|don't|stop|instead of|no longer)\b")
# Deterministic keyword proxies for `subtype`. Both are PROXIES, not classifiers: they pick a
# retrieval bucket so a candidate is findable, and the review flow is what actually rules.
_PRESCRIPTIVE_RE = re.compile(r"\b(?:always|never|don't|do not|must)\b")
# Prefix match on purpose: "commits", "testing", "deploys", "formatting" are the same word.
_TOOLING_RE = re.compile(r"\b(?:commit|test|lint|deploy|format)")


# ── text primitives ──────────────────────────────────────────────────────────────

def _tokens(text: str) -> set:
    return set(retrieval.index_tokens(text or ""))


def _overlap(left: str, right: str) -> float:
    """|A∩B| / |smaller| over the shared index tokenizer. Empty on either side scores 0."""
    a, b = _tokens(left), _tokens(right)
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _first_sentence(text: str) -> str:
    """The seed's opening sentence, clipped to `_MAX_TITLE_CHARS`."""
    head = re.split(r"(?<=[.!?])(?:\s|$)", (text or "").strip(), maxsplit=1)[0]
    return head.strip()[:_MAX_TITLE_CHARS]


def _negates(text: str) -> bool:
    return _NEGATION_RE.search((text or "").lower()) is not None


def _has_rationale(summary: str) -> bool:
    """A DETERMINISTIC PROXY for "this conclusion explains itself": two or more
    sentence-ending periods, or an explicit causal connective. It is a shape test, not
    comprehension — a two-sentence statement of fact scores as rationale, and a one-sentence
    explanation without "because" does not."""
    low = (summary or "").lower()
    return low.count(".") >= 2 or "because" in low or "so that" in low


def _subtype_for(seed) -> str:
    """`constraint` for a prescriptive directive, else `convention` when tooling/process words
    appear, else `architecture`. Keyword rules, documented as a proxy (see `_PRESCRIPTIVE_RE`)."""
    summary = (seed.get("summary") or "").lower() if seed else ""
    if seed and seed.get("kind") == "user_directive" and _PRESCRIPTIVE_RE.search(summary):
        return "constraint"
    if _TOOLING_RE.search(summary):
        return "convention"
    return "architecture"


# ── grouping ─────────────────────────────────────────────────────────────────────

def _ordered(events) -> list:
    """Events by (occurred_at, event_id) — the one read order, so grouping cannot depend on
    how the caller happened to hold the list."""
    return sorted((e for e in events if isinstance(e, dict)),
                  key=lambda e: (str(e.get("occurred_at") or ""), str(e.get("event_id") or "")))


def _event_files(event) -> list:
    files = event.get("files")
    return [f for f in files if isinstance(f, str)] if isinstance(files, list) else []


def _attributes(event) -> dict:
    """`.get("attributes", {})` is not enough: a JSON null under an EXISTING key returns None,
    and every read below would then be an AttributeError on a pure function."""
    attributes = event.get("attributes")
    return attributes if isinstance(attributes, dict) else {}


def _merge_target(seed, groups):
    """The group this later seed joins, or None to start its own.

    Two bars: a restatement merges above `_MERGE_OVERLAP`, and a seed carrying a negation
    marker merges above the lower `_CONTRADICTION_OVERLAP` — a rebuttal shares fewer words
    with what it rebuts, and burying it in its own group would hide the disagreement from the
    developer instead of scoring it. Whichever bar it crossed, a merged negating seed is
    counted as a contradiction by `_score_group`.
    """
    fallback = None
    negating = _negates(seed.get("summary"))
    for group in groups:
        overlap = _overlap(seed.get("summary"), group["seed"].get("summary"))
        if overlap > _MERGE_OVERLAP:
            return group
        if negating and fallback is None and overlap > _CONTRADICTION_OVERLAP:
            fallback = group
    return fallback


def _attach_target(event, groups):
    """The group this support event corroborates, or None if it corroborates nothing.

    Same session AND (a file in common with the group's files so far, OR the group has no
    files yet and this is a test result). Consequence, stated rather than worked around: the
    seed is always the group's earliest event, so a file change recorded BEFORE the statement
    it belongs to attaches to nothing and lands in the leftover set — which is exactly the
    plan's "only a file changed, with no semantic statement" row.
    """
    files = _event_files(event)
    for group in groups:
        if group["seed"].get("session_id") != event.get("session_id"):
            continue
        if any(f in group["files"] for f in files) \
                or (not group["files"] and event.get("kind") == "test_result"):
            return group
    return None


def _group(events):
    """(groups, leftover support by session, ignored kind counts, merged seed count)."""
    groups: list = []
    leftovers: dict = {}
    ignored: dict = {}
    merged = 0
    for event in _ordered(events):
        kind = event.get("kind")
        if kind in SEED_KINDS:
            target = _merge_target(event, groups)
            if target is None:
                groups.append({"seed": event, "events": [event],
                               "files": list(dict.fromkeys(_event_files(event)))})
            else:
                target["events"].append(event)
                merged += 1
        elif kind in SUPPORT_KINDS:
            target = _attach_target(event, groups)
            if target is None:
                leftovers.setdefault(str(event.get("session_id") or ""), []).append(event)
                continue
            target["events"].append(event)
            for path in _event_files(event) if kind in _FILE_KINDS else []:
                if path not in target["files"]:
                    target["files"].append(path)
        else:
            ignored[str(kind)] = ignored.get(str(kind), 0) + 1
    return groups, leftovers, ignored, merged


# ── scoring ──────────────────────────────────────────────────────────────────────

def _seed_weight(seed) -> tuple:
    kind = seed.get("kind")
    if kind == "user_directive":
        return _SCORES["user_directive"], "explicit user directive"
    if kind == "decision_repeated":
        # A repetition event IS the "same conclusion repeated" signal, so it carries that
        # weight even when it is the one that opened the group.
        return _SCORES["repeated_independently"], "stored decision restated"
    if _has_rationale(seed.get("summary")):
        return _SCORES["agent_conclusion_with_rationale"], "agent conclusion with rationale"
    return _SCORES["agent_conclusion_bare"], "agent conclusion without rationale"


def _score_group(group) -> tuple:
    """(score, signals, uncertainties) for one group.

    Every event in the group gets a signal row, weight 0 included: the row is the audit trail
    for why the candidate exists, and dropping the zero-weight ones would make a corroborating
    event that scored nothing indistinguishable from one that was never seen.
    """
    events = group["events"]
    seed = group.get("seed")
    signals: list = []
    uncertainties: list = []
    sessions = {str(seed.get("session_id") or "")} if seed else set()
    counted_files = counted_tests = False

    for index, event in enumerate(events):
        kind = event.get("kind")
        if seed is not None and index == 0:
            weight, reason = _seed_weight(event)
        elif kind in SEED_KINDS:
            if _negates(event.get("summary")):
                weight, reason = _SCORES["contradiction"], "contradicts the group's statement"
                uncertainties.append(
                    f"contradicted by a later statement: {_first_sentence(event.get('summary'))}")
            elif str(event.get("session_id") or "") not in sessions:
                sessions.add(str(event.get("session_id") or ""))
                weight, reason = (_SCORES["repeated_independently"],
                                  "repeated independently in another session")
            else:
                weight, reason = 0, "restated in the same session"
        elif kind in _FILE_KINDS:
            weight = 0 if counted_files else _SCORES["files_changed"]
            reason = "files already counted" if counted_files else "relevant files changed"
            counted_files = True
        elif _attributes(event).get("status") != "passed":
            # test_result, the only remaining support kind. A red run is evidence the behavior
            # is NOT settled, so it corroborates nothing and must not raise the rank.
            weight, reason = 0, "test result did not pass"
        else:
            weight = 0 if counted_tests else _SCORES["tests_validate"]
            reason = "tests already counted" if counted_tests else "tests validate the behavior"
            counted_tests = True
        signals.append({"event_id": str(event.get("event_id") or ""),
                        "weight": weight, "reason": reason})
    return sum(s["weight"] for s in signals), signals, uncertainties


# ── classification against the existing decisions ────────────────────────────────

def _is_live(decision) -> bool:
    return not decision.get("tombstoned") and decision.get("status") != "ignored"


def _best_match(content, decisions) -> tuple:
    """(decision_id, overlap) for the closest decision. Ties break on the lowest id, so the
    same corpus always names the same decision."""
    best_id, best = None, 0.0
    for decision in sorted(decisions, key=lambda d: str(d.get("id") or "")):
        overlap = _overlap(content, decision.get("content") or "")
        if overlap > best:
            best_id, best = str(decision.get("id") or ""), overlap
    return best_id, best


def _repeated_target(group, live_ids):
    """The live decision id a `decision_repeated` event in this group names, if any."""
    for event in group["events"]:
        if event.get("kind") != "decision_repeated":
            continue
        named = str(_attributes(event).get("decision_id") or "")
        if named in live_ids:
            return named
    return None


def _classify(content, group, decisions) -> tuple:
    """(kind, target_decision_id, extra uncertainties) for a group that cleared the bar."""
    live = [d for d in decisions if _is_live(d)]
    retired = [d for d in decisions if not _is_live(d)]
    notes: list = []

    # A re-stated retired decision is a NEW candidate: the developer retired it once, so it is
    # surfaced fresh for review rather than silently matched back onto the tombstone.
    dead_id, dead_overlap = _best_match(content, retired)
    if dead_overlap > _DUPLICATE_OVERLAP:
        notes.append(f"restates {dead_id}, which was retired or ignored — review it as new")

    target, overlap = _best_match(content, live)
    if overlap > _DUPLICATE_OVERLAP:
        return "duplicate", target, notes
    if overlap > _RETIRE_OVERLAP and _negates(group["seed"].get("summary")):
        return "retire", target, notes
    if overlap > _UPDATE_OVERLAP:
        return "update", target, notes
    repeated = _repeated_target(group, {str(d.get("id") or "") for d in live})
    if repeated:
        return "update", repeated, notes
    return "new", None, notes


# ── assembly ─────────────────────────────────────────────────────────────────────

def _candidate_id(events) -> str:
    return str(uuid.uuid5(_CANDIDATE_NAMESPACE,
                          ",".join(sorted(str(e.get("event_id") or "") for e in events))))


def _seeded_candidate(group, decisions) -> dict:
    seed = group["seed"]
    content = seed.get("summary") or ""
    score, signals, uncertainties = _score_group(group)
    if score < _MIN_CANDIDATE_SCORE:
        kind, target = "insufficient", None
        uncertainties.append(f"score {score} is below the {_MIN_CANDIDATE_SCORE} review bar")
    else:
        kind, target, notes = _classify(content, group, decisions)
        uncertainties.extend(notes)
    return {
        "candidate_id": _candidate_id(group["events"]),
        "kind": kind,
        "title": _first_sentence(content),
        "content": content,
        "subtype": _subtype_for(seed),
        "target_decision_id": target,
        # Replacement inference is not part of V1 grouping; the `replace` kind exists for the
        # materialization/lifecycle work to use.
        "replacement_decision_id": None,
        "source_files": group["files"][:_MAX_SOURCE_FILES],
        "score": score,
        "signals": signals,
        "uncertainties": uncertainties,
    }


def _leftover_candidate(session_id, events) -> dict:
    """One `insufficient` candidate per session whose support events corroborated no
    statement. It carries no content because there is none: something changed and nobody said
    why. It exists so the run REPORTS the gap instead of dropping those events silently."""
    score, signals, uncertainties = _score_group({"seed": None, "events": events})
    files = list(dict.fromkeys(f for e in events if e.get("kind") in _FILE_KINDS
                               for f in _event_files(e)))
    uncertainties.append(
        f"files changed in session {session_id} with no stated decision to review")
    return {
        "candidate_id": _candidate_id(events),
        "kind": "insufficient",
        "title": "Files changed with no stated decision",
        "content": "",
        "subtype": "architecture",
        "target_decision_id": None,
        "replacement_decision_id": None,
        "source_files": files[:_MAX_SOURCE_FILES],
        "score": score,
        "signals": signals,
        "uncertainties": uncertainties,
    }


def aggregate_candidates(events: list, decisions: list) -> dict:
    """Group and score `events` against `decisions`, returning
    `{"candidates": [...], "diagnostics": {...}}`.

    `events` are normalized evidence events; `decisions` is a read-only projection
    (`id`/`status`/`tombstoned`/`content`/... — never mutated, never written back).
    """
    groups, leftovers, ignored, merged = _group(events or [])
    decisions = [d for d in (decisions or []) if isinstance(d, dict)]

    candidates = [_seeded_candidate(g, decisions) for g in groups]
    candidates += [_leftover_candidate(sid, evs) for sid, evs in sorted(leftovers.items())]
    candidates.sort(key=lambda c: (-c["score"], c["candidate_id"]))

    kinds = [e.get("kind") for e in (events or []) if isinstance(e, dict)]
    return {
        "candidates": candidates,
        "diagnostics": {
            # One group per candidate: the seeded groups plus each session's leftover set.
            "groups": len(candidates),
            "seeds": sum(1 for k in kinds if k in SEED_KINDS),
            "support": sum(1 for k in kinds if k in SUPPORT_KINDS),
            "ignored_kinds": ignored,
            "insufficient": sum(1 for c in candidates if c["kind"] == "insufficient"),
            "merged_duplicates": merged,
        },
    }
