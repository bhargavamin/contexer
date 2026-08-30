"""Policy-evaluation vocabularies, request validation, pure policy SELECTION and JUDGING.

A policy evaluation asks one question - "may this operation proceed?" - about a request an
assistant is about to make. This module owns every part of that question that is pure: what
the vocabularies are, whether a request is well-formed, WHICH stored decisions apply to it,
whether an applicable one is actually violated, how to name the exact policy set that
answered, and how to shape the answer.

Judging lives here and ONLY here. `guard_engine` owned it first, against staged git files; it
now delegates (`_evaluate_rules`, `_rule_selects`, `arm_guard`'s validation) so the armed
regex, the `i` flag, the glob and the secret scan each exist once. A second copy of "what
counts as a violation" is the drift this split exists to prevent, so the guard keeps only what
is I/O-shaped - enumerating staged paths, reading them, the throttle sidecars, the wall-clock
budget - and hands the bytes here.

The evaluator owns no time budget and no fail-open/fail-closed policy: it reports what it
judged and what it could not, and the CALLER decides what that means. A commit gate and a
read-only prompt hook want opposite answers from the same `partial`.

A leaf: stdlib plus `contexer.redact` (itself a stdlib leaf, for the secret patterns). It
imports no store, no guard_engine, and touches no filesystem, so selection and judging can be
tested against hand-built entry dicts and reused by any caller that has already loaded them.
Resolving which store's entries to pass in is the CALLER's job - global entries may be mixed
in and are selected by the same rules.

The trust rule below is a MIRROR of `guard_engine._guard_trusted`, restated rather than
imported (that module imports store, this one cannot). A parity test pins the two equal.
"""
import fnmatch
import hashlib
import json
import re
from collections.abc import Mapping

from contexer import redact  # pure stdlib leaf (no cycle): the secret-check patterns

VERDICTS = ("allow", "warn", "block")
EVALUATION_STATUSES = ("complete", "partial", "error")
BASES = ("deterministic", "semantic", "mixed")
OPERATIONS = ("read_files", "write_files", "shell", "commit", "merge", "deploy", "api_request")
ARTIFACT_KINDS = ("diff", "file_content", "command", "request", "deployment")

# The only two things a rule can be armed with, because they are the only two a machine can
# settle without a model: one regex, or the fixed high-confidence secret patterns.
CHECK_TYPES = frozenset({"regex", "secret"})

# Why an applicable policy was not judged. `omitted` is the request carrying no artifact at
# all; `truncated`/`unreadable`/`binary`/`too-large`/`budget` are the CALLER's reasons for
# handing over bytes it could not supply (the guard's `_staged_content` vocabulary, kept
# spelling-for-spelling so one token means one thing on both sides of the boundary);
# `bad-pattern`, `unsupported-check`, `unattributable` and `evaluator-error` are the
# evaluator's own. Every one of them means a check that DID NOT HAPPEN, which must never read
# as a check that found nothing.
#
# `unattributable` is the one the ARTIFACT'S SHAPE causes. A rule scoped to a `paths` glob may
# only ever be judged against the bytes belonging to the files that glob selects, and an
# artifact that names several files while carrying no file headers to split on gives the
# evaluator no way to say which bytes are whose. Judging the lot would let a match inside an
# out-of-scope file block on a rule that never governed it - a verdict attributed to a
# decision that never objected, which is the one defect this plane exists to prevent.
#
# `evaluator-error` is the one that is not about the artifact at all: the evaluator itself
# broke partway through the set, so the policy it was on and every one after it were never
# reached. It exists rather than being folded into an existing reason because `error` in the
# status says only THAT the run broke, and a caller reading `unchecked` to learn what it did
# not get an answer about would otherwise see an empty list beside it.
UNCHECKED_REASONS = ("omitted", "truncated", "unreadable", "binary", "too-large", "budget",
                     "bad-pattern", "unsupported-check", "unattributable", "evaluator-error")

# The most severe verdict each kind of policy may reach. An armed rule is machine-checked, so
# it may block; advisory prose can only ever warn, because nothing here can machine-check a
# sentence. Enforced in `_match`, not merely documented - a prose decision that could block
# would turn every approved opinion into a commit gate.
MAX_VERDICT = {"armed": "block", "advisory": "warn"}

# Mirror of guard_engine._GUARD_TRUSTED_SOURCES. An `ai`/`memory` entry never enforces on its
# own confidence; `plan` is trusted because a plan-sourced decision that survived
# reconciliation AND approval is more vetted than `scan`.
TRUSTED_SOURCES = frozenset({"human", "scan", "bootstrap", "plan"})

# Request bounds. The path RULES (repo-relative only: no absolute, no `..`, no windows drive)
# and the 300-char path/text caps deliberately MIRROR `evidence.py`'s, so one path never
# passes one gate and fails the other. They are COPIED, not shared, and the duplication is
# forced rather than lazy: `evidence.py` imports `store`, and this module is a pure leaf that
# must not. Hoisting them into a third module would be an abstraction with two callers and no
# behaviour of its own, which this repo's design constraints forbid. If either side's path
# rules change, change both. (`_MAX_FILES` is NOT mirrored: an event describes one edit, a
# request can name a whole change set.)
_MAX_INTENT_CHARS = 300
_MAX_REPO_KEY_CHARS = 300
_MAX_FILES = 100
_MAX_PATH_CHARS = 300
# 2 MiB: a diff or file this size is already past what any deterministic scan is useful on,
# and the cap is what keeps one request bounded in memory. An oversized artifact is an ERROR,
# never a truncation - judging half a diff would report "clean" about bytes nobody read.
#
# The one PUBLIC bound, because it is the one with a second reader: a caller reading an
# artifact off disk (`contexer policy evaluate --diff-file`) has to stop before the bytes are
# in memory, so it cannot wait for validation here to tell it. It reads this constant rather
# than restating the number - two spellings of one bound drift, and the drift is silent.
MAX_ARTIFACT_BYTES = 2 * 1024 * 1024

_KEYS = frozenset({"intent", "operation", "files", "artifact", "repo_key"})
_ARTIFACT_KEYS = frozenset({"kind", "content"})

_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:[\\/]")

# The severity ladder. A total order over `VERDICTS`, which is what makes `worst_verdict`
# associative - a caller may fold in any order and get one answer.
_VERDICT_RANK = {"allow": 0, "warn": 1, "block": 2}


# ── request validation ───────────────────────────────────────────────────────────

def _normalized_files(request: Mapping, errors: list[str]) -> list[str]:
    """The repo-relative file list. Absolute and parent-escaping paths are rejected, not
    rewritten, and an over-length list is an error rather than a silent truncation: a policy
    answer about 100 of 150 files is not an answer about the request that was asked."""
    value = request.get("files", [])
    if not isinstance(value, list):
        errors.append("files must be a list of strings")
        return []
    if len(value) > _MAX_FILES:
        errors.append(f"files has more than {_MAX_FILES} entries ({len(value)})")
    for i, path in enumerate(value):
        if not isinstance(path, str) or not path.strip():
            errors.append(f"files[{i}] must be a non-empty string")
        elif len(path) > _MAX_PATH_CHARS:
            errors.append(f"files[{i}] exceeds {_MAX_PATH_CHARS} characters ({len(path)})")
        elif path.startswith("/") or _WINDOWS_DRIVE.match(path):
            errors.append(f"files[{i}] must be repo-relative, got {path!r}")
        elif ".." in path.replace("\\", "/").split("/"):
            errors.append(f"files[{i}] must not contain a '..' segment, got {path!r}")
    return [p for p in value if isinstance(p, str)]


def _normalized_artifact(request: Mapping, errors: list[str]) -> dict | None:
    """The thing being judged - a diff, a file, a command - or None when the request carries
    no payload (a `read_files` intent, say). The schema is frozen here too."""
    value = request.get("artifact")
    if value is None:
        return None
    if not isinstance(value, Mapping):
        errors.append("artifact must be a mapping or null")
        return None
    for key in sorted(str(k) for k in value if k not in _ARTIFACT_KEYS):
        errors.append(f"unknown artifact key: {key!r}")

    kind = value.get("kind")
    if not isinstance(kind, str) or kind not in ARTIFACT_KINDS:
        errors.append(f"artifact.kind must be one of {sorted(ARTIFACT_KINDS)}, got {kind!r}")
    content = value.get("content", "")
    if not isinstance(content, str):
        errors.append("artifact.content must be a string")
        return None
    size = len(content.encode("utf-8"))
    if size > MAX_ARTIFACT_BYTES:
        errors.append(f"artifact.content exceeds {MAX_ARTIFACT_BYTES} bytes ({size})")
    return {"kind": kind, "content": content}


def validate_request(request: Mapping) -> tuple[dict | None, list[str]]:
    """Return `(normalized_copy, [])` for a structurally valid request, else `(None, errors)`.

    Same contract as `evidence.validate_event`: collects EVERY structural error, never raises
    on any input, and never mutates `request`. The schema is FROZEN - an unknown top-level key
    is an error rather than being preserved, so a caller that invents a field learns at the
    gate instead of having it silently ignored by every evaluator downstream.

    Normalization is defaults only, never lossy: `intent` defaults to "", `files` to `[]`,
    `artifact` to None. Every bound is an error, so a normalized request describes exactly the
    operation that was asked about.
    """
    if not isinstance(request, Mapping):
        return None, [f"request must be a mapping, got {type(request).__name__}"]

    errors: list[str] = []
    for key in sorted(str(k) for k in request if k not in _KEYS):
        errors.append(f"unknown top-level key: {key!r}")

    intent = request.get("intent", "")
    if not isinstance(intent, str):
        errors.append("intent must be a string")
        intent = ""
    elif len(intent) > _MAX_INTENT_CHARS:
        errors.append(f"intent exceeds {_MAX_INTENT_CHARS} characters ({len(intent)})")

    operation = request.get("operation")
    # The isinstance guard keeps a non-string out of a vocabulary-membership branch entirely.
    # `OPERATIONS` is a tuple, so `in` compares by equality and a JSON list or object would
    # merely test False rather than raise - but the error message then reports a dict as an
    # "operation", and a later `in` against a set-shaped vocabulary WOULD raise out of a
    # function whose whole contract is to return its errors instead.
    if not isinstance(operation, str) or operation not in OPERATIONS:
        errors.append(f"operation must be one of {sorted(OPERATIONS)}, got {operation!r}")

    repo_key = request.get("repo_key")
    if not isinstance(repo_key, str) or not repo_key.strip():
        errors.append("repo_key must be a non-empty string")
        repo_key = ""
    elif len(repo_key) > _MAX_REPO_KEY_CHARS:
        errors.append(f"repo_key exceeds {_MAX_REPO_KEY_CHARS} characters ({len(repo_key)})")

    files = _normalized_files(request, errors)
    artifact = _normalized_artifact(request, errors)

    if errors:
        return None, errors
    return {"intent": intent, "operation": operation, "files": files,
            "artifact": artifact, "repo_key": repo_key}, []


# ── policy selection ─────────────────────────────────────────────────────────────

def entry_status(entry: Mapping) -> str:
    """The entry's effective status. Mirrors `store.entry_status`: a MISSING key means an
    entry that predates the status field, which the store reads as `approved`.

    Deliberately `.get("status", "approved")` and not `.get("status") or "approved"`: an
    explicit empty status is a malformed value, not a legacy entry, and reading it as approved
    would let a broken write enforce policy. That is also the spelling the guard uses, so the
    two cannot disagree about one entry.
    """
    return entry.get("status", "approved")


def current_revision(entry: Mapping) -> dict | None:
    """The active revision. Mirrors `revisions.current_revision`: the POINTER decides, with a
    fallback to the last revision, never a timestamp comparison."""
    revs = entry.get("revisions") or []
    current_id = entry.get("current_revision_id")
    if current_id:
        for revision in revs:
            if isinstance(revision, Mapping) and revision.get("revision_id") == current_id:
                return revision
    return revs[-1] if revs and isinstance(revs[-1], Mapping) else None


def is_trusted(entry: Mapping) -> bool:
    """Whether a decision may enforce policy at all - the mirror of `guard_engine._guard_trusted`.

    Approved AND (born from a trusted provenance OR explicitly ratified by a human). An
    AI-inferred or memory-imported entry never enforces on its own confidence, however strong,
    until a human has actually looked at it; `approved_by == "human"` is set only at genuine
    ratification points, so an `ai`-sourced decision a developer approved by hand IS trusted
    while an auto-approved entry nobody reviewed is not.

    A falsy revision `source` (legacy entries predating provenance) falls back to the entry's
    `created_by` for this check ONLY - a read-time fallback, never a storage rewrite, because
    `share.py` deliberately preserves a stored `source: None` on the wire as honest unknown
    provenance. A falsy `created_by` too still resolves to untrusted.
    """
    if entry_status(entry) != "approved":
        return False
    if entry.get("approved_by") == "human":
        return True
    rev = current_revision(entry)
    if rev is None:
        return False
    source = rev.get("source") or entry.get("created_by")
    return source in TRUSTED_SOURCES


def rule_selects(rule: Mapping, path: str) -> bool:
    """Whether an armed rule's `paths` glob applies to `path` - no glob selects every file.

    Takes the guard_check CONFIG, where guard_engine's `_rule_selects` takes the entry that
    carries it; same fnmatch semantics either way, and this module never sees an entry that
    isn't already unpacked.
    """
    paths_glob = rule.get("paths") or ""
    return not paths_glob or fnmatch.fnmatch(path, paths_glob)


def _applicable(entry: Mapping, kind: str, rule, matched_files: list[str]) -> dict:
    rev = current_revision(entry) or {}
    return {"decision_id": str(entry.get("id") or ""),
            "revision_id": str(rev.get("revision_id") or ""),
            "kind": kind,
            "title": entry.get("title") or "",
            "rule": dict(rule) if rule else None,
            "matched_files": matched_files}


def select_policies(decisions: list, request: Mapping) -> list[dict]:
    """The policies that apply to one request, in `decision_id` order.

    Three gates, in this order. **Status**: only `approved` selects - a `suggested`,
    `pending_approval` or `ignored` decision has not been ratified and cannot speak. A
    `tombstoned` entry never reaches here by contract and is deselected anyway, because the
    cost of the defensive check is one `.get` and the cost of missing it is a retired policy
    still enforcing. **Trust**: `is_trusted` above - an untrusted entry is excluded from the
    set ENTIRELY rather than downgraded, so it can neither warn nor block. **Applicability**:

    - an ARMED entry (a non-empty `guard_check`) applies when the request names no files at
      all - `commit`, `merge`, `deploy` and `shell` are repo-wide, and a rule scoped to a glob
      still governs the repo-wide operation - or when some named file matches its glob.
    - an ADVISORY entry applies when its `source_files` anchors intersect the named files.
      Prose, so it may warn and may never block: nothing here can machine-check a sentence.

    An armed entry whose glob selects none of the named files FALLS THROUGH to the advisory
    test rather than dropping out: the rule cannot judge these files, but the decision's own
    anchors may still make its prose relevant, and losing that is a silent gap rather than a
    conservative one.

    Matching an entry's `intent` or its content artifacts is deliberately absent - that is the
    one matcher `guard_engine` owns, moved here whole by a later task rather than copied.
    """
    files = [f for f in (request.get("files") or []) if isinstance(f, str)]
    selected = []
    for entry in decisions or []:
        if not isinstance(entry, Mapping) or entry.get("tombstoned"):
            continue
        if not is_trusted(entry):
            continue

        rule = entry.get("guard_check")
        if isinstance(rule, Mapping) and rule:
            matched = [f for f in files if rule_selects(rule, f)]
            if not files or matched:
                selected.append(_applicable(entry, "armed", rule, matched))
                continue

        anchors = {f for f in (entry.get("source_files") or []) if isinstance(f, str)}
        matched = [f for f in files if f in anchors]
        if matched:
            selected.append(_applicable(entry, "advisory", None, matched))
    return sorted(selected, key=lambda p: p["decision_id"])


def policy_set_version(policies: list) -> str:
    """`"sha256:<hex>"` naming exactly the policy set that answered a request.

    Over sorted `(decision_id, revision_id, rule)` triples, so it is stable under input
    ordering and changes the moment a decision's current revision advances or its armed rule
    is re-armed with different terms. That is what lets a cached or logged verdict be tied to
    the policies that produced it: same version, same set, same answer.

    Rows are sorted by their own canonical JSON rather than compared as tuples, because a rule
    is a dict or None and neither is orderable against the other.
    """
    rows = [[str(p.get("decision_id") or ""), str(p.get("revision_id") or ""), p.get("rule")]
            for p in (policies or []) if isinstance(p, Mapping)]
    blob = json.dumps(sorted(rows, key=lambda r: json.dumps(r, sort_keys=True, default=str)),
                      sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


# ── judging ──────────────────────────────────────────────────────────────────────
# The ONE deterministic evaluator. Two sharply separated paths, inherited from the guard:
#   ARM TIME (`validate_check`) raises - arming is a deliberate developer act, so a request
#   that is not machine-checkable must fail loudly rather than degrade into a rule that
#   silently never fires.
#   RUN TIME (`rule_matches`, `evaluate_policies`) never raises for bad DATA: an unparseable
#   pattern or an unknown check type comes back as an `unchecked` reason, so the caller can
#   report the gap instead of reading it as a clean pass.

# Every arm-time refusal shares this message: the caller only needs to know that arming was
# refused because the request was not checkable, never which clause of the check tripped.
MACHINE_CHECKABLE_MSG = "guard rules must be machine-checkable"


def validate_check(check_type: str, pattern: str, flags: str) -> None:
    """Refuse anything not deterministically machine-checkable. Raises ValueError, returns
    None on success. The structural half of arming's refusal contract only - whether the
    decision exists and is approved is the caller's, which needs a store loaded first."""
    if check_type not in CHECK_TYPES:
        raise ValueError(MACHINE_CHECKABLE_MSG)
    if check_type == "secret":
        # `secret` always means "match redact.HIGH_CONFIDENCE_PATTERNS" - a pattern alongside
        # it is nonsensical, not merely redundant.
        if pattern:
            raise ValueError(MACHINE_CHECKABLE_MSG)
        return
    if not pattern:
        raise ValueError(MACHINE_CHECKABLE_MSG)
    if set(flags) - {"i"}:
        raise ValueError(MACHINE_CHECKABLE_MSG)
    try:
        re.compile(pattern, re.IGNORECASE if "i" in flags else 0)
    except re.error:
        raise ValueError(MACHINE_CHECKABLE_MSG)


def rule_matches(rule: Mapping, content: str) -> tuple[list[int], str | None]:
    """Run one armed rule's check over `content`. Returns `(line_numbers, unchecked_reason)`;
    exactly one side is meaningful - a reason means the rule could not be evaluated at all,
    never that it was evaluated and found nothing.

    `regex` matches line by line, so a hit's line number is exact. An unparseable pattern is
    `bad-pattern` (defensive: `validate_check` rejects one at arm time, but a store is a JSON
    file a human can edit). TypeError is caught beside `re.error` deliberately: a JSON `null`
    pattern is not a string, and letting that escape would take down the caller's whole run
    over one corrupt rule instead of naming it. An ABSENT pattern still reads as `""` - which
    matches every line - because that is what the guard has always done with one, and
    `validate_check` is what stops an empty rule being armed in the first place.

    `secret` matches `redact.HIGH_CONFIDENCE_PATTERNS` against the WHOLE content rather than
    per line - the PEM private-key pattern spans BEGIN/…/END lines, so splitting first would
    silently defeat it - and derives the line from the match offset.
    """
    check_type = (rule or {}).get("type")
    if check_type == "regex":
        try:
            compiled = re.compile(rule.get("pattern", ""),
                                  re.IGNORECASE if "i" in (rule.get("flags") or "") else 0)
        except (re.error, TypeError):
            return [], "bad-pattern"
        return [n for n, line in enumerate(content.splitlines(), start=1)
                if compiled.search(line)], None
    if check_type == "secret":
        return [content.count("\n", 0, m.start()) + 1
                for pat in redact.HIGH_CONFIDENCE_PATTERNS for m in pat.finditer(content)], None
    return [], "unsupported-check"


# ── artifact attribution ─────────────────────────────────────────────────────────
# Which bytes a rule is allowed to see. Selection already decided WHICH files a `paths` glob
# picks out of the request (`matched_files`); this is the other half, and the half that was
# missing: judging ran the rule over the WHOLE artifact, so a hit inside a file the glob never
# selected blocked in the name of a decision scoped away from it. A rule with no glob is not
# scoped at all and still sees everything, which is what it was armed to do.

_GIT_HEADER = re.compile(r"^diff --git a/(.+) b/(.+)$")


def _header_path(field: str) -> str:
    """The repo-relative path off a `---`/`+++` header line: the tab-separated timestamp some
    diff writers append is dropped, and the conventional `a/`/`b/` prefix stripped."""
    path = field.split("\t")[0].strip()
    return path[2:] if path[:2] in ("a/", "b/") else path


def _section_path(lines: list[str]) -> str:
    """The file one diff section is about, read from its headers - `+++` first, because a
    rename or a copy makes the post-image the file the change lands in, then `---` for a
    deletion whose `+++` is `/dev/null`, then the `diff --git` line for a mode-only or
    rename-only section that carries no `---`/`+++` pair at all. `""` when none of the three
    says, which is what makes the whole diff unattributable rather than half-judged."""
    minus = plus = ""
    for line in lines:
        if line.startswith("@@"):
            break
        if line.startswith("--- "):
            minus = _header_path(line[4:])
        elif line.startswith("+++ "):
            plus = _header_path(line[4:])
    for candidate in (plus, minus):
        if candidate and candidate != "/dev/null":
            return candidate
    head = _GIT_HEADER.match(lines[0]) if lines else None
    return head.group(2) if head else ""


_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def _hunk_end(lines: list[str], start: int) -> int:
    """The index just past the body of the hunk headed at `start`, or `-1` when that body
    cannot be walked with certainty.

    The end is COUNTED, never recognised. `@@ -a,b +c,d @@` declares that b lines of the old
    file and d of the new one follow, an omitted count meaning 1; a body line is context
    (` `, spends one of each), a deletion (`-`, spends an old one), an addition (`+`, spends a
    new one), or the `\\ No newline` note, which spends neither. The body ends the instant both
    budgets are spent, so hunk content can never be taken for what comes after it however it
    is shaped - which is the only thing that holds, since a header and a body line are drawn
    from the same characters and any fixed lookahead can be written to satisfy.

    `-1` for a body that runs off the end of the input, for one whose lines disagree with the
    declared counts, and for a header this regex cannot read: an unwalkable diff is reported
    `unattributable` upstream, never split on a guess.
    """
    head = _HUNK_HEADER.match(lines[start])
    if not head:
        return -1
    old = int(head.group(1)) if head.group(1) is not None else 1
    new = int(head.group(2)) if head.group(2) is not None else 1
    i = start + 1
    while old > 0 or new > 0:
        if i >= len(lines):
            return -1
        line, i = lines[i], i + 1
        if line.startswith("\\"):
            continue
        if line.startswith("-"):
            old -= 1
        elif line.startswith("+"):
            new -= 1
        elif line.startswith(" ") or not line:
            old -= 1
            new -= 1
        else:
            return -1
    return i if old == 0 and new == 0 else -1


def _plain_starts(lines: list[str]) -> list[int]:
    """Section starts for a diff carrying no `diff --git ` markers: a walk alternating a
    `---`/`+++` pair with its hunks, counting its way out of every hunk body via `_hunk_end`.

    Pattern-matching the boundary is what cannot be made safe. A deleted line reading
    `-- separator` renders as `--- separator` and an added `++ separator` as `+++ separator`,
    so a pair alone splits a section mid-hunk; demanding a following `@@` only raises the
    price of the forgery, since the SAME file's next genuine hunk header can sit right behind
    such a pair by coincidence. Either way the bytes on both sides are still judged, so a
    secret in the real file lands in a phantom section no glob selects and the run comes back
    a clean `allow` with no `unchecked` row - the one fabricated pass this module exists to
    prevent. Counting removes the question: content inside a hunk is never LOOKED at to decide
    where the hunk ends.

    `[]` when the walk cannot account for every line, which reads as `unattributable`.

    The `diff --git ` branch needs no equivalent: every line inside a hunk carries a
    ` `/`+`/`-` prefix, so its raw text cannot start with `diff --git ` at column 0.
    """
    starts, i = [], 0
    while i < len(lines):
        if not (lines[i].startswith("--- ") and i + 1 < len(lines)
                and lines[i + 1].startswith("+++ ")):
            return []
        starts.append(i)
        i += 2
        while i < len(lines) and lines[i].startswith("@@"):
            i = _hunk_end(lines, i)
            if i < 0:
                return []
            # A trailing "\ No newline at end of file" note can sit right after a hunk's last
            # body line even though both budgets already hit zero on that line, so `_hunk_end`
            # returns before consuming it. Skip it here rather than in `_hunk_end` itself: it
            # belongs to no budget, and leaving it unconsumed would make the very next line
            # match neither "@@" nor "--- ", walking off a diff that is otherwise well-formed.
            while i < len(lines) and lines[i].startswith("\\"):
                i += 1
    return starts


def _section_starts(lines: list[str]) -> list[int]:
    """The 0-based line index each file section begins at. `diff --git` when the writer emits
    it, else the counted walk over the `---`/`+++` pairs a plain `diff -u` leads with."""
    git = [i for i, line in enumerate(lines) if line.startswith("diff --git ")]
    return git if git else _plain_starts(lines)


def _diff_sections(content: str) -> list[tuple[str, int, str]]:
    """One `(path, first_line_number, text)` per file in a unified diff.

    `[]` means NOT ATTRIBUTABLE, and it is deliberately all-or-nothing: a diff with no file
    headers at all, and equally a diff one of whose sections names no file, both come back
    empty. Judging the sections that did parse and staying silent about the one that did not
    is precisely the half-answer this module refuses everywhere else.

    ponytail: boundaries are counted (`_hunk_end`), paths are still read off header shapes -
    the line numbers here are offsets into the artifact, which is all a match row reports. A
    path containing " b/", or one git quoted under `core.quotePath`, can be misread; the
    upgrade path is a real diff parser, not a cleverer regex.
    """
    lines = content.splitlines()
    starts = _section_starts(lines)
    sections = []
    for n, start in enumerate(starts):
        body = lines[start:starts[n + 1] if n + 1 < len(starts) else len(lines)]
        path = _section_path(body)
        if not path:
            return []
        sections.append((path, start + 1, "\n".join(body)))
    return sections


def _scoped_chunks(rule: Mapping, kind: str, files: list[str],
                   content: str) -> tuple[list[tuple[int, str]], str | None]:
    """The parts of the artifact one armed rule may be judged against, as `(line_offset, text)`
    chunks, or `([], reason)` when the artifact cannot be split the way the rule needs.

    Four cases, in this order:

    - NO GLOB: the rule was never scoped, so it sees the whole artifact, exactly as before.
    - NO FILES NAMED: a repo-wide `commit`/`merge`/`deploy`/`shell` request, which selection
      already rules a glob-scoped rule governs (there is nothing to filter by). The whole
      artifact again - and this is the case `guard_engine` runs on, where the content is ONE
      staged file it read itself.
    - A DIFF THAT SPLITS: only the sections whose path the glob selects. A glob that selects
      none of them is `unattributable`, not clean: selection put this rule in the set because
      the request NAMED a file it governs, so an artifact with no bytes for that file
      disagrees with the request, and reporting "found nothing" about bytes that were never
      there is the fabricated pass this whole list exists to prevent.
    - ONE FILE NAMED: the artifact is that file's, so the glob has already selected all of it.

    Anything else - several files named, nothing in the bytes saying which is which - is
    `unattributable`. The gate is the DECLARED `kind`, never a guess at the content, because a
    single file's content that happens to quote a diff header would otherwise be judged as a
    diff and have most of itself scoped away.
    """
    if not (rule.get("paths") or "") or not files:
        return [(0, content)], None
    if kind == "diff":
        sections = _diff_sections(content)
        if sections:
            chunks = [(start - 1, text) for path, start, text in sections
                      if rule_selects(rule, path)]
            return (chunks, None) if chunks else ([], "unattributable")
    if len(files) == 1:
        return [(0, content)], None
    return [], "unattributable"


def _match(applicable: Mapping, verdict: str, line: int | None) -> dict:
    """One violated policy. Always names BOTH ids: `decision_id` is which decision objected,
    `revision_id` is which wording of it did - a decision whose text has moved on has not made
    the same objection, and a match that cannot say which revision spoke cannot be audited."""
    kind = applicable.get("kind") or ""
    ceiling = MAX_VERDICT.get(kind, "warn")
    if _VERDICT_RANK[_checked(verdict, VERDICTS, "verdict")] > _VERDICT_RANK[ceiling]:
        raise ValueError(f"a {kind!r} policy may not {verdict}, at most {ceiling}")
    return {"decision_id": str(applicable.get("decision_id") or ""),
            "revision_id": str(applicable.get("revision_id") or ""),
            "kind": kind,
            "title": applicable.get("title") or "",
            "message": (applicable.get("rule") or {}).get("message") or "",
            "verdict": verdict,
            "line": line}


def _unchecked(reason: str, **fields) -> dict:
    """One gap, named. The reason is vocabulary-checked for the same reason a verdict is: a
    typo'd reason is a gap nobody can act on, and this list is the only thing standing between
    "not judged" and "judged clean"."""
    return {"reason": _checked(reason, UNCHECKED_REASONS, "reason"), **fields}


def _caller_gap(row) -> dict:
    """One gap the CALLER reported, validated rather than filtered.

    A row that is not a mapping is a bug in the caller, and dropping it silently would turn
    that bug into a false clean verdict - the same defect this whole list exists to prevent,
    and inconsistent with the typo'd reason beside it, which raises. So this raises too.
    """
    if not isinstance(row, Mapping):
        raise ValueError(f"an unchecked row must be a mapping, got {type(row).__name__}")
    return _unchecked(row.get("reason"), **{k: v for k, v in row.items() if k != "reason"})


def _decision_id(applicable) -> str:
    return str(applicable.get("decision_id") or "") if isinstance(applicable, Mapping) else ""


def evaluate_policies(policies: list, request: Mapping, unchecked: list | None = None) -> dict:
    """Judge every selected policy against one request's artifact. Pure: no filesystem, no
    subprocess, no store, no git, no clock.

    An ARMED policy is machine-checked against the artifact's content and may `block` - and
    only against the part of that content its `paths` glob actually scopes it to, which is
    `_scoped_chunks`' job: an unscoped rule still sees everything, a scoped one sees the diff
    sections belonging to its own files, and an artifact that cannot be split that way is
    `unattributable` rather than judged whole. A hit in a file the glob never selected would
    otherwise block in the name of a decision that never governed it. An
    ADVISORY policy has already earned its place by anchoring on a named file, so it `warn`s
    on applicability alone - there is nothing further to check about a sentence.

    `unchecked` is the caller's list of artifacts it could not hand over (see
    `UNCHECKED_REASONS`); rows it supplies are validated - a malformed one RAISES, exactly as
    a typo'd reason does, because swallowing a caller's broken gap report would convert their
    bug into a false clean verdict - and passed through beside the evaluator's own. An armed
    policy facing a request with NO artifact is `omitted` rather than passing: that is the
    whole discipline the guard learned the hard way, where an unreadable staged file read as
    a clean result.

    Status follows from that list: `complete` when nothing went unjudged, `partial` when
    something did, `error` on an internal failure. An `error` is never converted to `allow`:
    the verdict still reports the matches actually found, and the status still says the
    evaluation broke. On that failure the policy being judged and every one after it are
    listed as `evaluator-error`, so `unchecked` stays an honest coverage report rather than
    an empty list beside a broken run. Whether a `partial`/`error` should fail open or closed
    is the caller's call - this module owns no budget and no exit behaviour.
    """
    skipped = [_caller_gap(r) for r in (unchecked or [])]
    version = policy_set_version(policies)
    artifact = request.get("artifact") if isinstance(request, Mapping) else None
    # None means "no artifact at all", which is different from an artifact that is legitimately
    # empty: the first cannot be judged, the second was judged and found nothing.
    content = artifact.get("content") or "" if isinstance(artifact, Mapping) else None
    # Both read once, for `_scoped_chunks`: the declared artifact shape, and the files the
    # request says the operation touches. A path-scoped rule may only be judged against the
    # bytes those two together attribute to the files its glob selects.
    kind = artifact.get("kind") or "" if isinstance(artifact, Mapping) else ""
    files = ([f for f in (request.get("files") or []) if isinstance(f, str)]
             if isinstance(request, Mapping) else [])
    matches: list[dict] = []
    # Bound before the try so the handler can name what went unreached even if materializing
    # the list is itself what failed (in which case there is nothing to name, and it says so).
    pending: list = []
    reached = 0

    try:
        pending = list(policies or [])
        for reached, applicable in enumerate(pending):
            if not isinstance(applicable, Mapping):
                continue
            if applicable.get("kind") != "armed":
                matches.append(_match(applicable, "warn", None))
                continue
            decision_id = str(applicable.get("decision_id") or "")
            if content is None:
                skipped.append(_unchecked("omitted", decision_id=decision_id))
                continue
            rule = applicable.get("rule") or {}
            chunks, reason = _scoped_chunks(rule, kind, files, content)
            hits: list[int] = []
            for offset, text in chunks:
                lines, reason = rule_matches(rule, text)
                if reason is not None:
                    break
                hits.extend(n + offset for n in lines)
            if reason is not None:
                skipped.append(_unchecked(reason, decision_id=decision_id))
                continue
            matches.extend(_match(applicable, "block", n) for n in hits)
    except Exception:
        status = "error"
        # `reached` is the policy that raised, so the slice starts AT it: it was not judged
        # either, and a report that skipped it would be the same silent gap one line smaller.
        skipped.extend(_unchecked("evaluator-error", decision_id=_decision_id(p))
                       for p in pending[reached:])
    else:
        status = "partial" if skipped else "complete"

    return build_result(worst_verdict([m["verdict"] for m in matches]), status,
                        "deterministic", matches, skipped, version)


# ── results ──────────────────────────────────────────────────────────────────────

def _checked(value: str, vocab: tuple, name: str) -> str:
    if value not in vocab:
        raise ValueError(f"{name} must be one of {list(vocab)}, got {value!r}")
    return value


def build_result(verdict: str, evaluation_status: str, basis: str, matches: list,
                 unchecked: list, policy_set_version: str) -> dict:
    """One evaluation answer.

    `allow` means every applicable policy the engine COULD evaluate passed - it is not a claim
    that nothing applies, which is why `evaluation_status` travels beside it and why an
    `error` is never silently converted into an `allow`. `unchecked` names what the engine
    could not judge (an unreadable artifact, an exhausted budget), and a non-empty `unchecked`
    is what `partial` means. Whether a `partial`/`error` result should fail open or closed is
    the CALLER's policy, not this module's: a commit gate and a read-only prompt hook want
    opposite answers from the same result.

    The three vocabulary arguments are validated because a typo here is a wrong verdict, and a
    wrong verdict is the one defect this whole plane exists to prevent.
    """
    return {"verdict": _checked(verdict, VERDICTS, "verdict"),
            "evaluation_status": _checked(evaluation_status, EVALUATION_STATUSES,
                                          "evaluation_status"),
            "basis": _checked(basis, BASES, "basis"),
            "matches": list(matches or []),
            "unchecked": list(unchecked or []),
            "policy_set_version": policy_set_version}


def worst_verdict(verdicts) -> str:
    """The most severe verdict present: block > warn > allow. Empty is `allow` - nothing
    applied, so nothing objected."""
    return max((_checked(v, VERDICTS, "verdict") for v in verdicts or []),
               key=_VERDICT_RANK.__getitem__, default="allow")
