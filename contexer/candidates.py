"""Deterministic grouping and scoring of evidence events into decision candidates.

A candidate is a PROPOSAL: the group of evidence events that, taken together, look like one
engineering decision worth a developer's attention. This module is pure - it reads validated
events (`evidence.validate_event` shapes) plus a read-only projection of the existing
decisions, and returns dicts. It writes nothing and imports nothing that does; the only
import is `retrieval`, itself a leaf, for the ONE tokenizer this repo has.

`score` is an AGGREGATION-RANKING INPUT, never a confidence that a statement is true. 50 for
a user directive means "a developer said this out loud, put it near the top of the review
queue" - not "this is 50% likely to be correct". Nothing here approves, enforces, retires or
materializes anything: the review flow is the only gate.

Idempotency is the load-bearing property. `candidate_id` is a uuid5 over the candidate's kind,
its target decision when it names one, the sorted contributing event ids, and - for a
`reconsider` candidate only - the basis revision the question is asked against; events are
ordered by (occurred_at, event_id) before anything reads them, and candidates come back sorted
by (-score, candidate_id). The same event set in ANY input order therefore produces
byte-identical output. A `uuid4` in this module is a defect.
"""

import re
import uuid
from collections import Counter
from datetime import datetime

from contexer import retrieval

# Fixed namespace for candidate ids. Derived, not a magic literal, and never regenerated:
# changing this string renames every candidate in every ledger.
_CANDIDATE_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://contexer.dev/evidence/candidate")

# A seed carries a SEMANTIC STATEMENT (something was decided/said); support events corroborate
# one. Kinds in neither set never group - they are ledger bookkeeping about candidates, not
# evidence for one.
SEED_KINDS = frozenset({"user_directive", "agent_conclusion", "decision_repeated"})
SUPPORT_KINDS = frozenset({"file_changed", "test_result", "diff_observed"})
# Which kinds contribute PATHS. `test_result` files name the tests, not what was decided.
_FILE_KINDS = frozenset({"file_changed", "diff_observed"})

# The closed V1 relationship vocabulary: WHY an event is in a candidate, recorded on every
# signal row so a reviewer reads the link rather than inferring it from a weight.
#
# `unrelated` is the one that never attaches - it is what a leftover event is, and it is
# carried on the leftover candidate's own rows so that report says why it exists.
RELATIONS = frozenset({"explicit", "structural", "causal_forward", "temporal_backward",
                       "repetition", "contradiction", "validation", "unrelated"})
CERTAINTIES = frozenset({"confirmed", "supporting", "uncertain"})

_CERTAINTY = {
    "explicit": "confirmed",
    "structural": "confirmed",
    "contradiction": "confirmed",
    "repetition": "supporting",
    "causal_forward": "supporting",
    "validation": "supporting",
    "temporal_backward": "uncertain",
    "unrelated": "uncertain",
}

# Attachment preference when several groups qualify for one support event: the strongest
# relation first, then the shortest absolute time distance, then the lowest seed event id.
# All three are properties of the data, never of list order, so the pick is stable under any
# input permutation.
_RELATION_RANK = {"structural": 0, "validation": 1, "causal_forward": 2, "temporal_backward": 3}

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

# How long after a seed that names NO files a support event still corroborates it. The number
# is the store's own `_EDITED_FILES_WINDOW` (30 minutes), which already answers this exact
# question for capture-time anchor accrual: which of this session's edits relate to what was
# just said. Restated rather than imported - this module cannot reach `store`.
_PROXIMITY_SECONDS = 1800

_NEGATION_RE = re.compile(r"\b(?:not|never|don't|stop|instead of|no longer)\b")
# Deterministic keyword proxies for `subtype`. Both are PROXIES, not classifiers: they pick a
# retrieval bucket so a candidate is findable, and the review flow is what actually rules.
_PRESCRIPTIVE_RE = re.compile(r"\b(?:always|never|don't|do not|must)\b")
# Prefix match on purpose: "commits", "testing", "deploys", "formatting" are the same word.
_TOOLING_RE = re.compile(r"\b(?:commit|test|lint|deploy|format)")


# ── text primitives ──────────────────────────────────────────────────────────────

def _tokens(text: str) -> set:
    return set(retrieval.index_tokens(text or ""))


def _overlap_tokens(a: set, b: set) -> float:
    """|A∩B| / |smaller| over two ALREADY-tokenized statements. Empty on either side scores 0.

    Split out of `_overlap` so every hot path can tokenize each statement exactly once and
    compare the sets, rather than re-tokenizing both sides of every pair."""
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def _overlap(left: str, right: str) -> float:
    """|A∩B| / |smaller| over the shared index tokenizer. Empty on either side scores 0."""
    return _overlap_tokens(_tokens(left), _tokens(right))


def _first_sentence(text: str) -> str:
    """The seed's opening sentence, clipped to `_MAX_TITLE_CHARS`."""
    head = re.split(r"(?<=[.!?])(?:\s|$)", (text or "").strip(), maxsplit=1)[0]
    return head.strip()[:_MAX_TITLE_CHARS]


def _negates(text: str) -> bool:
    return _NEGATION_RE.search((text or "").lower()) is not None


def _has_rationale(summary: str) -> bool:
    """A DETERMINISTIC PROXY for "this conclusion explains itself": two or more
    sentence-ending periods, or an explicit causal connective. It is a shape test, not
    comprehension - a two-sentence statement of fact scores as rationale, and a one-sentence
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
    """Events by (occurred_at, event_id) - the one read order, so grouping cannot depend on
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


def _new_index() -> dict:
    """The lookup structures `_group` maintains beside its group list.

    Dicts and lists, in one object, because they are written at exactly one place (a group
    being opened) and every one of them is keyed by that group's own position in `groups`:

    * `postings` - index token to the ASCENDING group positions whose seed carries it. What
      lets `_merge_target` look at only the groups that could possibly overlap this seed.
    * `sizes` / `negations` - each group's seed token count and negation polarity, so neither
      is recomputed once per comparison.
    * `sessions` - the session key to its groups, in group order. Cross-session attachment is
      forbidden outright (`_attach_target`), so this is that RULE expressed as a lookup rather
      than as a test repeated against every group in the run.
    """
    return {"postings": {}, "sizes": [], "negations": [], "sessions": {}}


def _index_group(index: dict, position: int, tokens: set, negating: bool, session) -> None:
    for token in tokens:
        index["postings"].setdefault(token, []).append(position)
    index["sizes"].append(len(tokens))
    index["negations"].append(negating)
    index["sessions"].setdefault(session, []).append(position)


def _shared_counts(index: dict, tokens: set) -> dict:
    """`{group position: |A∩B|}` for every group sharing at least one token with `tokens`.

    `Counter.update` over each posting list, so the counting itself runs in C: the Python-level
    work is one pass per token, not one pass per (seed, group) pair. A group absent from the
    result shares no token, which scores 0.0 and can clear no bar.
    """
    counts: Counter = Counter()
    postings = index["postings"]
    for token in tokens:
        hits = postings.get(token)
        if hits:
            counts.update(hits)
    return counts


def _session_key(event):
    """A hashable identity for an event's session, so groups can be bucketed by it.

    The RAW value, because that is what the equality test this bucketing replaces compared -
    `""` and `None` are different sessions to it, and quietly normalizing them here would merge
    two things the old scan kept apart. The fallback covers only a malformed, unvalidated event
    carrying an unhashable session, where raising out of a pure function would be the worse
    answer.
    """
    session = event.get("session_id")
    try:
        hash(session)
    except TypeError:
        return repr(session)
    return session


def _merge_target(tokens, negating, groups, index):
    """`(group, relation)` for the group this later seed joins, or `(None, "")`.

    Two bars: a restatement merges above `_MERGE_OVERLAP`, and a seed whose negation POLARITY
    differs from the group's merges above the lower `_CONTRADICTION_OVERLAP` - a rebuttal
    shares fewer words with what it rebuts, and burying it in its own group would hide the
    disagreement from the developer instead of scoring it.

    Polarity, not the bare presence of a negation word, is what makes it a `contradiction`
    (ledger ruling D1). `_negates` matches "not"/"never"/"don't" anywhere in the summary, so a
    PROHIBITION restated verbatim in a second session used to contradict ITSELF: "Do not edit
    the generated client" repeated scored 50 - 30 = 20 and fell under the review bar, where the
    same rule phrased affirmatively scored 65. A restatement that negates exactly what the
    group's seed negates is `repetition`; only a seed that flips the polarity is a rebuttal.
    That keeps the ratified high-overlap case intact - a near-verbatim REVERSAL ("never run
    migrations before deploying" against "migrations must run before deploying") still flips
    polarity and is still charged as the contradiction it is.

    SEMANTICS ARE FIRST-MATCH IN GROUP ORDER, and they are preserved EXACTLY while the scan
    itself is order-free. The loop this replaces returned the first group above
    `_MERGE_OVERLAP` and only fell back to the first POLARITY-FLIPPED group above
    `_CONTRADICTION_OVERLAP` when no group anywhere cleared the higher bar - so the answer was
    never "the earliest of the two kinds of hit", it was "the earliest merge hit if one exists
    at all, else the earliest contradiction hit". Both are the MINIMUM qualifying group index,
    which is computable from any traversal order, so this tracks the two minima instead of
    depending on where it happens to be when it finds one.

    The scan itself is the group index's token postings: only groups sharing at least one
    index token with this seed are even looked at, since a disjoint pair scores 0.0 and can
    clear neither bar. Each surviving pair is then judged by the same exact expression as
    before - `|A∩B| / |smaller|` against the same two constants - so no approximate matcher is
    anywhere in this path.

    ponytail: this remains O(N^2) in the WORST case - 1,000 statements that all share one
    common word are still 1,000 x 1,000 counted pairs - but the pair is now an integer count
    and a division rather than two tokenizations and a set intersection, and a pair sharing no
    token costs nothing at all. Measured at `spool._MAX_PENDING_EVENTS` = 1000 (three-run
    median after warm-up, aggregation alone): 2667.67ms -> 106.45ms for 1,000 mutually distinct
    statements, 8.65ms -> 8.26ms for 1,000 boilerplate ones, and 30.57ms -> 5.68ms for a
    realistically shaped session. The distinct case is the one this changes, and it is still
    quadratic: its 1,000 statements share four ordinary words, so every pair IS counted. What
    fell by 25x is the price of a pair. Upgrade path if that ceiling ever matters again: a
    length-banded prefix filter over the rarest tokens, which needs a frequency-ordered token
    vocabulary this module does not keep.
    """
    if not tokens:
        return None, ""
    size = len(tokens)
    sizes, negations = index["sizes"], index["negations"]
    merge_at = contra_at = None
    for group_index, shared in _shared_counts(index, tokens).items():
        # The token-length impossibility filter, before the division: |A∩B| / |smaller| can
        # only exceed the LOWER of the two bars when twice the shared count exceeds the
        # smaller token set. Exact, not a heuristic - it is the same inequality, rearranged.
        smaller = size if size < sizes[group_index] else sizes[group_index]
        if shared + shared <= smaller:
            continue
        overlap = shared / smaller
        if overlap > _MERGE_OVERLAP:
            if merge_at is None or group_index < merge_at:
                merge_at = group_index
        elif (overlap > _CONTRADICTION_OVERLAP
                and negating != negations[group_index]
                and (contra_at is None or group_index < contra_at)):
            contra_at = group_index
    if merge_at is not None:
        flipped = negating != negations[merge_at]
        return groups[merge_at], "contradiction" if flipped else "repetition"
    if contra_at is not None:
        return groups[contra_at], "contradiction"
    return None, ""


def _within_proximity(seed, event) -> bool:
    """Whether `event` falls inside `_PROXIMITY_SECONDS` AFTER `seed`. Reads only the events'
    own `occurred_at` - this module owns no clock - and is fail-soft: a timestamp that will
    not parse, or a naive one that cannot be subtracted from an aware one, corroborates
    nothing rather than raising out of a pure function."""
    try:
        elapsed = (datetime.fromisoformat(str(event.get("occurred_at") or ""))
                   - datetime.fromisoformat(str(seed.get("occurred_at") or ""))).total_seconds()
    except (TypeError, ValueError):
        return False
    return 0 <= elapsed <= _PROXIMITY_SECONDS


def _distance(seed, event) -> float:
    """Absolute seconds between two events, `inf` when either timestamp will not parse - so an
    unparseable pair always loses a tie-break rather than winning one by accident."""
    try:
        return abs((datetime.fromisoformat(str(event.get("occurred_at") or ""))
                    - datetime.fromisoformat(str(seed.get("occurred_at") or ""))).total_seconds())
    except (TypeError, ValueError):
        return float("inf")


def _relation_for(group, event, files):
    """WHY this support event would attach to this group, or None for no link at all.

    Structural first, forward proximity second, backward proximity last and only as an
    uncertain display link (the brief's three-tier rule):

    * `structural` - the event touches a file the group already carries, or one the group's
      SEED names in its own text. The text half is what fixes outstanding issue 6: the
      generated-client rule names `src/generated/client.ts`, so the regeneration recorded a
      minute BEFORE the developer typed the rule is linked by the rule's own words rather than
      by the order the two happened to land in. Time-direction-blind by design, because a
      shared identifier is proof whichever way the clock ran.
    * `validation` - a test result for a group that has no files to share yet.
    * `causal_forward` - the seed names no files and the event followed it inside
      `_PROXIMITY_SECONDS`. This is what makes a real session aggregate at all: a
      `user_directive`, the strongest seed there is, never carries `files`, so a shared-file
      rule alone could never let anything attach to one. The bound is the SEED's own files,
      not the group's, so the window stays open once the first edit has given the group files;
      and it is measured from the seed, so a chain of edits cannot slide the window forward
      and swallow a whole long session.
    * `temporal_backward` - the same window, the other way round, with nothing structural to
      show for it. ALWAYS uncertain, worth zero, and never an anchor: an edit that merely
      happened shortly before a statement is not evidence for it, and a wrong anchor is guard
      and staleness input, so it is worse than none.
    """
    seed = group["seed"]
    if any(f in group["files"] or f in group["artifacts"] for f in files):
        return "structural"
    if not group["files"] and event.get("kind") == "test_result":
        return "validation"
    if not _event_files(seed):
        if _within_proximity(seed, event):
            return "causal_forward"
        if _within_proximity(event, seed):
            return "temporal_backward"
    return None


def _attach_target(event, groups):
    """`(group, relation)` for the group this support event links to, or `(None, "")`.

    Always the same session first: cross-session temporal attachment is forbidden outright, so
    an edit made in another session can never corroborate this one's statement. That rule is
    now the CALLER's lookup - `groups` is this event's own session bucket, never the whole run
    - which is the same filter applied once per group instead of once per (event, group) pair.
    Among the groups that do qualify, the pick is the deterministic tie-break `_RELATION_RANK`
    documents - strongest relation, shortest absolute time distance, lowest seed event id.

    The distance term is what the old "last qualifying seed wins" fallback was really saying:
    an edit corroborates what was JUST said, not the oldest thing said inside the window.
    Spelling it as a distance makes it hold for backward links too, where "last" has no
    meaning, and makes it independent of group order rather than merely consistent with it.
    """
    files = _event_files(event)
    best = None
    for group in groups:
        seed = group["seed"]
        relation = _relation_for(group, event, files)
        if relation is None:
            continue
        key = (_RELATION_RANK[relation], _distance(seed, event),
               str(seed.get("event_id") or ""))
        if best is None or key < best[0]:
            best = (key, group, relation)
    return (best[1], best[2]) if best else (None, "")


def _new_group(seed, tokens: set) -> dict:
    """One seed's group. `artifacts` are the path- and module-shaped spans of the seed's own
    text, used ONLY to recognize a structural link - they never become `source_files` by
    themselves, because a rule that NAMES a file is not the same fact as a session that
    CHANGED it (which is why scenario 3's directive still anchors nothing).

    `tokens` rides along because the seed's summary is ALSO the candidate's content, so
    `_classify` compares the very same token set against the stored decisions - tokenizing it
    a second time there would be the same string, twice, for every candidate over the bar."""
    return {
        "seed": seed,
        "tokens": tokens,
        "events": [seed],
        "files": list(dict.fromkeys(_event_files(seed))),
        "artifacts": set(retrieval.raw_path_artifacts(seed.get("summary") or "")),
        "links": {},
        "possible": [],
        "uncertain": [],
    }


def _group(events):
    """(groups, leftover support by session, ignored kind counts, merged seed count).

    Two passes, deliberately: every seed group is created and merged first, and only then is
    support attached. One pass could only ever link an event to a statement made BEFORE it,
    which is the whole of outstanding issue 6 - the same directive and edit grouped one way or
    two depending purely on which the developer did first.

    A group's `events` therefore read statements-first, then support, rather than in strict
    wall-clock order - both passes read `_ordered`, so it is fixed, and it is what a signal
    list now shows. Nothing scores off that order (`_candidate_id` sorts the ids, the
    files/tests counters are first-wins within support, and the repetition counter walks seeds
    only), so the change is a display one.

    An UNCERTAIN link does not consume its event. It is recorded on the group for display
    (`possible`/`uncertain`) and the event ALSO stays in the leftover set, because nothing
    about it has been explained: it still belongs in the "files changed with no stated
    decision" report, and letting a group swallow it would delete that gap from the run while
    anchoring nothing.
    """
    groups: list = []
    index = _new_index()
    leftovers: dict = {}
    ignored: dict = {}
    support: list = []
    merged = 0
    for event in _ordered(events):
        kind = event.get("kind")
        if kind in SEED_KINDS:
            tokens = _tokens(event.get("summary"))
            negating = _negates(event.get("summary"))
            target, relation = _merge_target(tokens, negating, groups, index)
            if target is None:
                _index_group(index, len(groups), tokens, negating, _session_key(event))
                groups.append(_new_group(event, tokens))
            else:
                target["events"].append(event)
                target["links"][str(event.get("event_id") or "")] = relation
                merged += 1
        elif kind in SUPPORT_KINDS:
            support.append(event)
        else:
            ignored[str(kind)] = ignored.get(str(kind), 0) + 1

    for event in support:
        target, relation = _attach_target(
            event, [groups[i] for i in index["sessions"].get(_session_key(event), ())])
        session = str(event.get("session_id") or "")
        if target is None:
            leftovers.setdefault(session, []).append(event)
            continue
        paths = _event_files(event) if event.get("kind") in _FILE_KINDS else []
        if _CERTAINTY[relation] == "uncertain":
            target["uncertain"].append((event, relation))
            for path in paths:
                if path not in target["possible"]:
                    target["possible"].append(path)
            leftovers.setdefault(session, []).append(event)
            continue
        target["events"].append(event)
        target["links"][str(event.get("event_id") or "")] = relation
        for path in paths:
            if path not in target["files"]:
                target["files"].append(path)
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
    event that scored nothing indistinguishable from one that was never seen. Each row carries
    the typed link `_group` recorded (`relation`) and how much that link is worth as proof
    (`certainty`) beside the ranking weight, so the reason a candidate holds together is read
    off the candidate rather than reconstructed from a number.
    """
    events = group["events"]
    seed = group.get("seed")
    links = group.get("links") or {}
    signals: list = []
    uncertainties: list = []
    sessions = {str(seed.get("session_id") or "")} if seed else set()
    counted_files = counted_tests = False

    for index, event in enumerate(events):
        kind = event.get("kind")
        # A leftover set has no seed, so nothing in it was linked to anything: `unrelated` is
        # the honest relation for those rows, and it is what makes the insufficient candidate
        # a report of an unexplained change rather than a weak proposal.
        relation = links.get(str(event.get("event_id") or ""), "unrelated")
        if seed is not None and index == 0:
            weight, reason = _seed_weight(event)
            relation = "explicit"
        elif kind in SEED_KINDS:
            if relation == "contradiction":
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
            # is NOT settled, so it corroborates nothing and must not raise the rank - and it
            # is `uncertain` for the same reason, whatever link brought it into the group.
            weight, reason = 0, "test result did not pass"
            relation = "validation"
        else:
            weight = 0 if counted_tests else _SCORES["tests_validate"]
            reason = "tests already counted" if counted_tests else "tests validate the behavior"
            counted_tests = True
        certainty = ("uncertain" if reason == "test result did not pass"
                     else _CERTAINTY[relation])
        signals.append({"event_id": str(event.get("event_id") or ""), "weight": weight,
                        "relation": relation, "certainty": certainty, "reason": reason})
    return sum(s["weight"] for s in signals), signals, uncertainties


def _uncertain_signals(group) -> list:
    """Display-only rows for the links that were made but proved nothing: the event, why it
    was linked, and a plain statement that it counts for nothing. They are NOT in `signals`
    and NOT in the candidate's event set, so nothing downstream can hold them, score them, or
    read their files as an anchor - `possible_source_files` is the whole of what they buy."""
    return [{"event_id": str(event.get("event_id") or ""), "weight": 0,
             "relation": relation, "certainty": _CERTAINTY[relation],
             "reason": "changed close in time with no structural link - not evidence"}
            for event, relation in group.get("uncertain") or []]


# ── classification against the existing decisions ────────────────────────────────

def _is_live(decision) -> bool:
    return not decision.get("tombstoned") and decision.get("status") != "ignored"


def _decision_index(decisions) -> dict:
    """The stored decisions tokenized ONCE per run, split into the two sets `_classify` asks
    about, each sorted by id so a tie always names the same decision.

    A row is `(decision, content tokens, negation polarity)`. Every candidate over the review
    bar compares itself against every decision twice - live and inactive - so re-tokenizing
    each decision's content per candidate was the whole store, once per proposal.
    """
    rows = [(d, _tokens(d.get("content") or ""), _negates(d.get("content")))
            for d in sorted(decisions, key=lambda d: str(d.get("id") or ""))]
    return {
        "all": rows,
        "live": [row for row in rows if _is_live(row[0])],
        "inactive": [row for row in rows if not _is_live(row[0])],
    }


def _best_match(tokens, rows) -> tuple:
    """(row, overlap) for the closest decision, `(None, 0.0)` for no match at all. Ties break
    on the lowest id (the order `_decision_index` already put the rows in), so the same corpus
    always names the same decision.

    The ROW rather than the id: the retire branch needs the matched text's own negation
    polarity, and re-looking it up by id would be a second scan for a value this loop already
    held."""
    best_row, best = None, 0.0
    for row in rows:
        overlap = _overlap_tokens(tokens, row[1])
        if overlap > best:
            best_row, best = row, overlap
    return best_row, best


def _has_directive(group) -> bool:
    """Whether a developer said this out loud somewhere in the group.

    The OPENING gate for reconsideration, and the whole of what separates it from every other
    kind: an agent conclusion, a file edit, a test run or a repetition may corroborate a
    reconsideration that is already open, but none of them may raise the question. Read across
    the group's events rather than off its seed alone, so a directive that merged into an
    earlier conclusion's group still counts as the developer having spoken."""
    return any(e.get("kind") == "user_directive" for e in group.get("events") or [])


def _by_id(rows, decision_id):
    """The projected decision with this id, or None."""
    if not decision_id:
        return None
    return next((d for d, _tok, _neg in rows if str(d.get("id") or "") == str(decision_id)),
                None)


def _repeated_target(group, live_ids):
    """The live decision id a `decision_repeated` event in this group names, if any."""
    for event in group["events"]:
        if event.get("kind") != "decision_repeated":
            continue
        named = str(_attributes(event).get("decision_id") or "")
        if named in live_ids:
            return named
    return None


def _classify(group, index) -> tuple:
    """(kind, target_decision_id, extra uncertainties) for a group that cleared the bar."""
    notes: list = []
    tokens = group["tokens"]

    dead_row, dead_overlap = _best_match(tokens, index["inactive"])
    target_row, overlap = _best_match(tokens, index["live"])
    dead = dead_row[0] if dead_row else None
    target = target_row[0] if target_row else None

    # RECONSIDERATION. A restatement of an ignored or retired decision is neither a duplicate
    # of it nor a fresh decision beside it: it is the question "should this come back?", asked
    # against the ORIGINAL decision identity so its revisions and its retirement history stay
    # one continuous record. Only an explicit `user_directive` may raise it (`_has_directive`),
    # and only when the inactive match is STRICTLY closer than any live one - a live decision
    # that matches at least as well is still a live duplicate or update, and answering it does
    # not resurrect anything.
    if dead is not None and dead_overlap > _DUPLICATE_OVERLAP and dead_overlap > overlap:
        dead_id = str(dead.get("id") or "")
        if _has_directive(group):
            notes.append(f"reconsiders {dead_id}, which the developer made inactive - only "
                         "their explicit review restores it")
            return "reconsider", dead_id, notes
        # Deliberately NAMES no decision. The candidate goes on to be classified as ordinary
        # content, and a note pointing a reviewer at the inactive decision would be the
        # reopening itself in prose - the developer never said anything.
        notes.append("restates a decision that is no longer active - only an explicit "
                     "developer directive reopens one")

    if overlap > _DUPLICATE_OVERLAP:
        return "duplicate", str(target.get("id") or ""), notes
    # POLARITY, not the bare presence of a negation word (the discipline `_merge_target`
    # documents, applied to the candidate-vs-store comparison it was missing from). `_negates`
    # matches "never"/"not"/"don't" anywhere, so a PROHIBITION restating a stored prohibition
    # in slightly different words landed in this band and was classified as a proposal to
    # RETIRE the very rule it repeats. A retirement is a reversal, so it takes a flip.
    if (overlap > _RETIRE_OVERLAP
            and _negates(group["seed"].get("summary")) != target_row[2]):
        return "retire", str(target.get("id") or ""), notes
    if overlap > _UPDATE_OVERLAP:
        return "update", str(target.get("id") or ""), notes
    repeated = _repeated_target(group, {str(d.get("id") or "") for d, _tok, _neg
                                        in index["live"]})
    if repeated:
        return "update", repeated, notes
    return "new", None, notes


# ── assembly ─────────────────────────────────────────────────────────────────────

def _candidate_id(kind, target_decision_id, events, basis_revision_id="") -> str:
    """The candidate's identity: its kind, the decision it acts on when it names one, the
    sorted ids of the events it is built from, and - for a reconsideration - the revision of
    the inactive decision the proposal is formed against.

    Deterministic by construction, which is what the storage layer leans on: `held/<id>/` IS
    the "already pending" record, so two passes over the same evidence must name the same
    directory. The kind and target join the seed because they are what the candidate PROPOSES
    - the same events read as `update the auth decision` and as `retire it` are two different
    proposals, and one directory could only ever hold one of them. The basis joins them for
    the same reason: reconsidering revision 3 of a retired decision is a different question
    from reconsidering revision 5, and a reviewer must be answering the one they were shown.

    APPENDED only when there is one, rather than always joined as an empty component: every
    candidate id already in a ledger was minted from three parts, and adding a fourth
    unconditionally would rename all of them.
    """
    parts = [
        str(kind or ""),
        str(target_decision_id or ""),
        ",".join(sorted(str(e.get("event_id") or "") for e in events)),
    ]
    if basis_revision_id:
        parts.append(str(basis_revision_id))
    return str(uuid.uuid5(_CANDIDATE_NAMESPACE, "\n".join(parts)))


def _seeded_candidate(group, index) -> dict:
    seed = group["seed"]
    content = seed.get("summary") or ""
    score, signals, uncertainties = _score_group(group)
    if score < _MIN_CANDIDATE_SCORE:
        kind, target = "insufficient", None
        uncertainties.append(f"score {score} is below the {_MIN_CANDIDATE_SCORE} review bar")
    else:
        kind, target, notes = _classify(group, index)
        uncertainties.extend(notes)
    # Only a reconsideration carries these, and only it binds its identity to the basis: for
    # every other kind the target's revision is read at materialization time, where the
    # proposal actually lands.
    inactive = (_by_id(index["all"], target) if kind == "reconsider" else None) or {}
    basis = str(inactive.get("current_revision_id") or "") if inactive else ""
    return {
        "candidate_id": _candidate_id(kind, target, group["events"], basis),
        "kind": kind,
        "target_state": ("retired" if inactive.get("tombstoned") else "ignored")
                        if inactive else None,
        "basis_revision_id": basis or None,
        "title": _first_sentence(content),
        "content": content,
        "subtype": _subtype_for(seed),
        "target_decision_id": target,
        # Replacement inference is not part of V1 grouping; the `replace` kind exists for the
        # materialization/lifecycle work to use.
        "replacement_decision_id": None,
        "source_files": group["files"][:_MAX_SOURCE_FILES],
        # Paths reached only through an uncertain link. Kept SEPARATE from `source_files` for
        # the whole length of the pipeline: nothing may promote one into an anchor, a policy
        # rule's scope, `anchor_candidates`, or Teams. See `_relation_for`'s temporal_backward
        # note for why an uncertain anchor is worse than no anchor at all.
        "possible_source_files": group["possible"][:_MAX_SOURCE_FILES],
        "score": score,
        "signals": signals,
        "uncertain_signals": _uncertain_signals(group),
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
        "candidate_id": _candidate_id("insufficient", None, events),
        "kind": "insufficient",
        "target_state": None,
        "basis_revision_id": None,
        "title": "Files changed with no stated decision",
        "content": "",
        "subtype": "architecture",
        "target_decision_id": None,
        "replacement_decision_id": None,
        "source_files": files[:_MAX_SOURCE_FILES],
        "possible_source_files": [],
        "score": score,
        "signals": signals,
        "uncertain_signals": [],
        "uncertainties": uncertainties,
    }


def aggregate_candidates(events: list, decisions: list) -> dict:
    """Group and score `events` against `decisions`, returning
    `{"candidates": [...], "diagnostics": {...}}`.

    `events` are normalized evidence events; `decisions` is a read-only projection
    (`id`/`status`/`tombstoned`/`content`/... - never mutated, never written back).
    """
    groups, leftovers, ignored, merged = _group(events or [])
    index = _decision_index([d for d in (decisions or []) if isinstance(d, dict)])

    candidates = [_seeded_candidate(g, index) for g in groups]
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
