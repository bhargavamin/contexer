"""Policy-evaluation vocabularies, request validation, pure policy SELECTION, and results.

A policy evaluation asks one question — "may this operation proceed?" — about a request an
assistant is about to make. This module owns the halves of that question that are pure: what
the vocabularies are, whether a request is well-formed, WHICH stored decisions apply to it,
how to name the exact policy set that answered, and how to shape the answer.

JUDGING IS NOT HERE. Deciding whether an applicable policy is actually violated (regex, secret
scanning) is `guard_engine`'s deterministic engine today; a later task moves that ONE
implementation into this module. Nothing here evaluates a rule, and there is no stub for it —
a second copy of "what counts as a violation" is exactly the drift this split exists to avoid.

A leaf: stdlib only. It imports no store, no guard_engine, and touches no filesystem, so
selection can be tested against hand-built entry dicts and reused by any caller that has
already loaded them. Resolving which store's entries to pass in is the CALLER's job — global
entries may be mixed in and are selected by the same rules.

The trust rule below is a MIRROR of `guard_engine._guard_trusted`, restated rather than
imported (that module imports store, this one cannot). A parity test pins the two equal.
"""
import fnmatch
import hashlib
import json
import re
from collections.abc import Mapping

VERDICTS = ("allow", "warn", "block")
EVALUATION_STATUSES = ("complete", "partial", "error")
BASES = ("deterministic", "semantic", "mixed")
OPERATIONS = ("read_files", "write_files", "shell", "commit", "merge", "deploy", "api_request")
ARTIFACT_KINDS = ("diff", "file_content", "command", "request", "deployment")

# Mirror of guard_engine._GUARD_TRUSTED_SOURCES. An `ai`/`memory` entry never enforces on its
# own confidence; `plan` is trusted because a plan-sourced decision that survived
# reconciliation AND approval is more vetted than `scan`.
TRUSTED_SOURCES = frozenset({"human", "scan", "bootstrap", "plan"})

# Request bounds. `files` and paths reuse the evidence event's rules so one path never passes
# one gate and fails the other; `intent`/`repo_key` share evidence's 300-char text cap.
_MAX_INTENT_CHARS = 300
_MAX_REPO_KEY_CHARS = 300
_MAX_FILES = 100
_MAX_PATH_CHARS = 300
# 2 MiB: a diff or file this size is already past what any deterministic scan is useful on,
# and the cap is what keeps one request bounded in memory. An oversized artifact is an ERROR,
# never a truncation — judging half a diff would report "clean" about bytes nobody read.
_MAX_ARTIFACT_BYTES = 2 * 1024 * 1024

_KEYS = frozenset({"intent", "operation", "files", "artifact", "repo_key"})
_ARTIFACT_KEYS = frozenset({"kind", "content"})

_WINDOWS_DRIVE = re.compile(r"[A-Za-z]:[\\/]")

# Severity ladders. Both are total orders over their vocabulary, which is what makes the two
# merge helpers below associative — a caller may fold in any order and get one answer.
_VERDICT_RANK = {"allow": 0, "warn": 1, "block": 2}
_STATUS_RANK = {"complete": 0, "partial": 1, "error": 2}


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
    """The thing being judged — a diff, a file, a command — or None when the request carries
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
    if size > _MAX_ARTIFACT_BYTES:
        errors.append(f"artifact.content exceeds {_MAX_ARTIFACT_BYTES} bytes ({size})")
    return {"kind": kind, "content": content}


def validate_request(request: Mapping) -> tuple[dict | None, list[str]]:
    """Return `(normalized_copy, [])` for a structurally valid request, else `(None, errors)`.

    Same contract as `evidence.validate_event`: collects EVERY structural error, never raises
    on any input, and never mutates `request`. The schema is FROZEN — an unknown top-level key
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
    # The isinstance guard is the never-raises half: `operation not in OPERATIONS` hashes its
    # left side, so an unhashable value (a JSON list or object) would raise out of a function
    # whose whole contract is to return its errors instead.
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
    """Whether a decision may enforce policy at all — the mirror of `guard_engine._guard_trusted`.

    Approved AND (born from a trusted provenance OR explicitly ratified by a human). An
    AI-inferred or memory-imported entry never enforces on its own confidence, however strong,
    until a human has actually looked at it; `approved_by == "human"` is set only at genuine
    ratification points, so an `ai`-sourced decision a developer approved by hand IS trusted
    while an auto-approved entry nobody reviewed is not.

    A falsy revision `source` (legacy entries predating provenance) falls back to the entry's
    `created_by` for this check ONLY — a read-time fallback, never a storage rewrite, because
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
    """Whether an armed rule's `paths` glob applies to `path` — no glob selects every file.

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

    Three gates, in this order. **Status**: only `approved` selects — a `suggested`,
    `pending_approval` or `ignored` decision has not been ratified and cannot speak. A
    `tombstoned` entry never reaches here by contract and is deselected anyway, because the
    cost of the defensive check is one `.get` and the cost of missing it is a retired policy
    still enforcing. **Trust**: `is_trusted` above — an untrusted entry is excluded from the
    set ENTIRELY rather than downgraded, so it can neither warn nor block. **Applicability**:

    - an ARMED entry (a non-empty `guard_check`) applies when the request names no files at
      all — `commit`, `merge`, `deploy` and `shell` are repo-wide, and a rule scoped to a glob
      still governs the repo-wide operation — or when some named file matches its glob.
    - an ADVISORY entry applies when its `source_files` anchors intersect the named files.
      Prose, so it may warn and may never block: nothing here can machine-check a sentence.

    An armed entry whose glob selects none of the named files FALLS THROUGH to the advisory
    test rather than dropping out: the rule cannot judge these files, but the decision's own
    anchors may still make its prose relevant, and losing that is a silent gap rather than a
    conservative one.

    Matching an entry's `intent` or its content artifacts is deliberately absent — that is the
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


# ── results ──────────────────────────────────────────────────────────────────────

def _checked(value: str, vocab: tuple, name: str) -> str:
    if value not in vocab:
        raise ValueError(f"{name} must be one of {list(vocab)}, got {value!r}")
    return value


def build_result(verdict: str, evaluation_status: str, basis: str, matches: list,
                 unchecked: list, policy_set_version: str) -> dict:
    """One evaluation answer.

    `allow` means every applicable policy the engine COULD evaluate passed — it is not a claim
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
    """The most severe verdict present: block > warn > allow. Empty is `allow` — nothing
    applied, so nothing objected."""
    return max((_checked(v, VERDICTS, "verdict") for v in verdicts or []),
               key=_VERDICT_RANK.__getitem__, default="allow")


def merge_status(statuses) -> str:
    """The least complete status present: error > partial > complete. Empty is `complete` —
    every one of the zero policies was evaluated. Merging never launders an `error` into
    something softer; that is the same invariant `build_result` documents."""
    return max((_checked(s, EVALUATION_STATUSES, "evaluation_status") for s in statuses or []),
               key=_STATUS_RANK.__getitem__, default="complete")
