"""Commit-time guard engine: staged-file plumbing, Tier-1 advisory pairing, and
Tier-2 armed (machine-checkable) blocking rules. Extracted out of store.py, whose
`STORE_DIR`/`load`/`save`/... are read through the `store` module object (not
`from`-imported) so store-owned values tests monkeypatch on `contexer.store` are
still seen here at call time. `contexer/store.py` stays the public facade: it
re-exports this module's five public entrypoints at the BOTTOM of its file
(after everything else is defined) so the store -> guard_engine -> store import
cycle resolves cleanly. Approval-time anchoring (`approve_decision`,
`_apply_approval`, `_anchor_sources`) is approval machinery, not guard engine,
and stays in store.py.
"""

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from contexer import redact          # pure stdlib leaf (no cycle): secret redaction
from contexer import retrieval       # pure stdlib leaf (no cycle): path/module artifact shapes
from contexer import revisions      # pure stdlib leaf (no cycle): revision lifecycle
from contexer import sidecars
from contexer import store           # module object, not `from`-imports: see docstring above


# ── Commit-time guard: staged-file plumbing + path-matching helpers ──────────
# Task 1 of the commit-time guard feature (later tasks hash staged content against
# stored decisions). All fail-soft: any git failure -> empty result, never raise.

# Above this, a staged file is not scanned at all. Raised from 200_000, which silently
# disqualified this repo's own two largest tracked files (contexer/store.py at 376KB and
# tests/test_store.py at 390KB, the two most-edited files here), so an armed Tier-2 rule
# could not see a secret committed inside either.
#
# 1MB, not more, and the number comes from the EXPENSIVE rule type rather than the cheap
# one. A `regex` rule compiles one pattern; a `secret` rule runs all 14 of
# redact.HIGH_CONFIDENCE_PATTERNS over the whole content, which measures ~155ms per MB
# (57ms for store.py's 374KB, 306ms at 2MB) against _GUARD_TIME_BUDGET's 2000ms. So the
# per-file cost is ~57x a four-regex probe over the same bytes, and a 2MB cap would let
# SEVEN staged files exhaust the whole budget. That matters because a budget overrun is
# not a per-file skip: it discards every violation gathered so far, including the ones
# found in the small readable files, and reports only "internal error". 1MB clears this
# repo's worst file with 2.5x headroom while keeping one pathological file well short of
# the budget on its own.
_GUARD_MAX_FILE_BYTES = 1_000_000

# Seconds of _GUARD_TIME_BUDGET held back from Tier-2 scanning. Scanning stops at
# `deadline - _GUARD_SCAN_RESERVE` and NAMES the selected files it did not reach, which
# is the whole point: the hard deadline fails open by discarding every violation gathered
# so far (see guard_staged), so a run that is going to run out of time must bail out
# early enough to return what it found instead of tripping that.
#
# This replaced a fixed 4MB aggregate byte cap, which was the same idea measured against
# the wrong resource. Bytes are only a PROXY for cost, and the proxy's exchange rate
# depends on the armed rule mix (one cheap regex versus 14 secret patterns differ by
# ~57x), so a byte number safe for the worst mix throws away most of the budget for every
# other one: 4MB is ~620ms of worst-case scanning against a 2000ms deadline, so files
# between ~4MB and ~12.9MB of staged selected text stopped being scanned even though
# there was ample time for them (found by review on PR #241). Spending the real resource
# directly has no exchange rate to get wrong and adapts to the actual rule mix.
#
# The reserve must exceed the cost of the single largest file that can still be started
# just before the cut-off, which is what _GUARD_MAX_FILE_BYTES bounds: 1MB of worst-case
# secret scanning is ~155ms, so 500ms leaves ~3x margin plus room for Tier-1 pairing.
# The two constants are therefore related, not independent guesses.
_GUARD_SCAN_RESERVE = 0.5

# Why a staged file was not scanned, as a stable token. `binary` is deliberately NOT
# reported to the developer: a regex over binary content is meaningless, so skipping a
# PNG is the correct outcome rather than a gap worth a line on every commit that stages
# one. The rest each mean a file that COULD have been checked was not: `too-large` and
# `unreadable` come from `_staged_content`, `budget` from _GUARD_SCAN_RESERVE above.
_GUARD_UNCHECKED_REPORTED = ("too-large", "unreadable", "budget")


def _staged_files(repo: str) -> list[str]:
    """Repo-relative paths of staged Added/Copied/Modified/Renamed files. R is
    included deliberately: renamed files must still be scanned, and `--name-only`
    on an R entry yields only the new path, which is exactly what the guard scans.
    Deleted files (filter D) are excluded — nothing to scan. `[]` on any git
    failure (fail-soft, never raises).

    `-z` (NUL-separated, splitting on b"\\0") is load-bearing, not a style
    choice: without it git C-QUOTES any path holding a non-ASCII byte, a quote,
    or a backslash — `"caf\\303\\251/m\\303\\263dulo.py"` — and that quoted
    spelling survives canonicalization intact, only to make the later
    `git show :<path>` fail. _staged_content then reports `unreadable` and every
    armed Tier-2 rule skips the file, so a secret in it ships. `-z` turns
    quoting off entirely. That means reading raw bytes (the same subprocess
    shape _staged_content already uses) and decoding each path AFTER the split,
    since the separator is a byte.

    Decoded with `errors="surrogateescape"`, not "replace": a filename holding a
    byte sequence that isn't valid UTF-8 (rare, but real — a stray Latin-1 export,
    a broken merge tool) would otherwise collapse to U+FFFD, an information-losing
    spelling that can no longer round-trip back to the real path. `git show
    :<path>` on that mangled spelling then fails, and _staged_content's `unreadable`
    report makes every armed regex/secret rule skip the file — the exact bypass
    class this whole module exists to close. surrogateescape keeps
    each unmappable byte recoverable (as a lone surrogate codepoint), so
    _staged_content can re-encode the SAME bytes back out via os.fsencode and the
    lookup succeeds."""
    try:
        out = subprocess.run(
            ["git", "-C", repo, "diff", "--cached", "--name-only", "-z",
             "--diff-filter=ACMR"],
            capture_output=True, timeout=store.GIT_FAST_TIMEOUT,
        )
    except Exception:
        return []
    if out.returncode != 0:
        return []
    return [p.decode("utf-8", errors="surrogateescape") for p in out.stdout.split(b"\0") if p]


def _staged_content(repo: str, path: str) -> tuple[str, str | None, str | None]:
    """Staged (index) content of `path` via `git show :<path>` — deliberately NOT
    the working-tree version, since the guard must judge what's about to be
    committed. Returns `(text, reason, fingerprint)`.

    `reason` is None when the text is the real staged content, else the token saying
    why there is none — `unreadable` (git failed), `too-large` (over
    _GUARD_MAX_FILE_BYTES), or `binary` (a null byte in the first 1024 bytes, since a
    regex over binary content can false-match encoded bytes).

    `fingerprint` is the raw bytes' sha1 and is present whenever git READ the file, so
    it is None only for `unreadable`. The distinction is the whole point: "I could not
    scan this" and "I do not know what this is" are different, and only the second one
    should stop the throttle from working. An over-cap or binary file still has a known,
    comparable identity, so its Tier-1 pair still throttles normally instead of
    re-advising on every commit.

    Reads raw bytes directly rather than through the text-mode `run_git` helper, because
    the binary/size checks need the untouched byte stream; only decodes utf-8
    (errors="replace") once those checks pass. Fail-soft: any failure returns
    `("", "unreadable", None)`, never raises.

    The `reason` half is not decoration. This used to return a bare `""` for all
    three failures AND for a genuinely empty staged file, so every caller read
    "I could not read this" as "there is nothing here" — the same unreadable-vs-empty
    collapse `store.load_diagnostics`, `store._read_global`, `console_api._read_store`
    and `console_api._inspect_store_file` all exist to prevent, and it cost two real things.
    Tier-2's `_guard_violations` skipped the file with no trace, so an armed rule
    silently passed a file it never saw (this module's own docstring above already
    names that outcome for the C-quoting cause, which `-z` fixed; the size cause was
    not fixed). And Tier-1's throttle hashed the `""`, freezing the pair on the
    empty-string sha1 so it could never re-advise however much the file changed.
    Both callers now branch on `reason` instead of on emptiness. `(value, error)` is
    the shape the four store readers named above already use, so this adds no new
    vocabulary.

    `path` may carry surrogate-escaped bytes from _staged_files's
    surrogateescape decode (an invalid-UTF-8 filename). A plain f-string arg
    would hand subprocess a str it re-encodes with the *default* filesystem
    error handler — surrogateescape on POSIX too, so this usually round-trips,
    but relying on that default is exactly the kind of implicit behavior that
    broke once already (the C-quoting bug this module's docstring above
    describes). Building the argv element as bytes explicitly — b":" +
    os.fsencode(path) — makes the byte-for-byte round trip the actual
    contract, not an accident of subprocess's default encoding path."""
    try:
        out = subprocess.run(
            ["git", "-C", repo, "show", b":" + os.fsencode(path)],
            capture_output=True, timeout=store.GIT_FAST_TIMEOUT,
        )
    except Exception:
        return "", "unreadable", None
    if out.returncode != 0:
        return "", "unreadable", None
    data = out.stdout
    fingerprint = _guard_content_hash(data)
    if len(data) > _GUARD_MAX_FILE_BYTES:
        return "", "too-large", fingerprint
    if b"\x00" in data[:1024]:
        return "", "binary", fingerprint
    return data.decode("utf-8", errors="replace"), None, fingerprint


def _merge_in_progress(repo: str) -> bool:
    """True iff a merge is in progress (MERGE_HEAD resolves). Fail-soft: any git
    failure (including "no such repo") reads as no merge in progress."""
    return store.run_git(repo, "rev-parse", "-q", "--verify", "MERGE_HEAD",
                timeout=store.GIT_FAST_TIMEOUT) is not None


def _guard_relpath(repo: str, path: str) -> str:
    """THE single canonicalization chokepoint for the commit-time guard: any
    absolute or relative spelling of a file resolves to one normalized
    repo-relative POSIX (forward-slash) path. Every hash and path-pairing
    comparison downstream must consume only this function's output — never a raw
    staged path or artifact string. Works for paths that don't exist on disk yet
    (Path.resolve() is non-strict), since guard callers canonicalize staged paths
    that may not exist in the working tree in every context. Fail-soft: any
    resolution failure returns "" rather than raising."""
    try:
        repo_root = Path(repo).resolve()
        raw = Path(path)
        abs_path = (raw if raw.is_absolute() else repo_root / raw).resolve()
        rel = os.path.relpath(str(abs_path), str(repo_root))
        return rel.replace(os.sep, "/")
    except Exception:
        return ""


def _escapes_repo(relpath: str) -> bool:
    """True iff `relpath` (assumed already run through _guard_relpath) cannot
    denote a file inside the repo: empty, "..", "../"-prefixed, or absolute."""
    return (not relpath or relpath == ".." or relpath.startswith("../")
            or os.path.isabs(relpath))


_GUARD_PATH_ARTIFACT_RE = re.compile(r"^[\w][\w./-]*\.\w+$")
_GUARD_MODULE_ARTIFACT_RE = re.compile(r"^[a-z_]\w*(\.[a-z_]\w*)+$")


def _pathlike_artifact(artifact: str) -> bool:
    """True iff `artifact` is shaped like something that can participate in path
    pairing against a staged file: a relative path with an extension (e.g.
    "contexer/store.py", no leading "/") or a dotted module (e.g.
    "contexer.store", no "/"). Excludes symbol artifacts ("FooError"),
    route-shaped strings ("/api/users"), and bare names — none of those pair
    against a staged file path."""
    if not isinstance(artifact, str) or not artifact:
        return False
    return bool(_GUARD_PATH_ARTIFACT_RE.match(artifact)
                or _GUARD_MODULE_ARTIFACT_RE.match(artifact))


def _artifact_path_match(artifact: str, staged: str) -> bool:
    """Pure — no I/O. `staged` is assumed already canonical (see _guard_relpath).
    True iff: exact relpath equality; OR `artifact` is a dotted module that maps
    onto `staged` ("contexer.store" -> "contexer/store.py" or
    "contexer/store/__init__.py"); OR `artifact` contains "/" and `staged` ends
    with "/" + artifact (a multi-segment suffix match at a path boundary, so
    "za/utils.py" does NOT match artifact "a/utils.py").

    Bare basename matching is forbidden by construction: a slashless artifact
    that isn't an exact match and isn't a mapping dotted module (e.g. "utils.py"
    against staged "a/utils.py") falls through to the final `return False` —
    it never reaches the suffix-match branch, which requires "/" in `artifact`."""
    if not artifact or not staged:
        return False
    if artifact == staged:
        return True
    if "/" not in artifact and _GUARD_MODULE_ARTIFACT_RE.match(artifact):
        as_path = artifact.replace(".", "/")
        return staged == f"{as_path}.py" or staged == f"{as_path}/__init__.py"
    if "/" in artifact:
        return staged.endswith("/" + artifact)
    return False


# ── Commit-time guard: Tier-1 advisory engine (Task 2) — pairing, throttle, ──
# dismissals. Builds on Task 1's plumbing above. The whole engine is READ-ONLY
# against the decision store (never calls save/save_global) and its public
# entrypoint (guard_staged) never raises. Only the guard's own sidecar files
# (dismiss list, throttle stamp) are ever written here, and always best-effort
# except on the explicit management path (dismiss_guard).

_GUARD_TRUSTED_SOURCES = frozenset({"human", "scan", "bootstrap", "plan"})
_GUARD_MAX_ADVISORIES = 5
_GUARD_THROTTLE_CAP = 500


def _guard_trusted(entry: dict) -> bool:
    """A decision may only pair as an advisory if it is BOTH developer-approved
    (status) and EITHER born from a trusted provenance (current revision's source)
    OR explicitly ratified by a human (`approved_by == "human"`) — an AI-inferred
    or memory-imported entry never nags at commit time on its own confidence, no
    matter how strong, until a human has actually looked at it. `plan` is trusted
    too: a plan-sourced decision that survived reconciliation AND developer
    approval is more vetted than `scan`; the status check above still keeps an
    unapproved plan suggestion untrusted.

    The `approved_by` clause (issue #180) is the refinement: the gate's real job
    is excluding entries that reached `approved` WITHOUT a human ever looking —
    not entries a human explicitly looked at and blessed. `approved_by` is set
    ONLY at genuine ratification points (`_apply_approval`'s approve/edit paths,
    including bulk `approve_decision("all")`/comma-list approvals — reviewing a
    shown list and approving it is a real ratification gesture — and the
    constraint-restatement promotions in `capture_user_constraint`), never on any
    auto-approval route (born-`approved` scan/bootstrap/memory/legacy-migration
    entries, or an `ai`-captured entry whose content happens to match a scan-fact
    pattern). So an `ai`-captured decision a developer explicitly approved is now
    guard-trusted even though its revision `source` stays `ai`; an auto-approved
    entry a human never reviewed stays untrusted regardless of status.

    A falsy revision `source` (legacy entries that predate provenance tracking)
    falls back to the decision's `created_by` for this check ONLY — a read-time
    fallback, not a storage rewrite. `share.py`'s `_wire_source` deliberately
    preserves a stored `source: None` end-to-end (the cloud stores NULL = honest
    unknown provenance); back-stamping it in storage would fabricate a false
    provenance on the share wire, so the fallback lives here instead, at the
    point the guard actually needs a trust verdict. A falsy `created_by` too
    still resolves to untrusted."""
    if store.entry_status(entry) != "approved":
        return False
    if entry.get("approved_by") == "human":
        return True
    rev = revisions.current_revision(entry)
    if rev is None:
        return False
    source = rev.get("source") or entry.get("created_by")
    return source in _GUARD_TRUSTED_SOURCES


def _guard_hash(decision_id: str, relpath: str) -> str:
    """Identity for one (decision, staged-file) advisory pair — sha1 of
    "<decision_id>\\n<relpath>", first 12 hex chars. `relpath` must already be
    _guard_relpath's canonical output; the caller owns that, this function is
    pure. Ports the shape of doc-drift's `_drift_hash` (see
    `git show feat/doc-drift:contexer/store.py`): keying on (decision, FILE) only
    — never content or line — means ordinary editing of the file never
    resurrects a dismissed or already-advised pair.

    Encoded with errors="surrogateescape", not "replace": `relpath` can carry
    surrogate-escaped bytes from an invalid-UTF-8 filename (see _staged_files),
    and "replace" would collapse every such byte to the same literal "?",
    letting two DIFFERENT exotic filenames collide onto the same pair hash.
    surrogateescape recovers the original bytes instead, so the hash — like
    the git lookup in _staged_content — stays keyed on the real path."""
    return hashlib.sha1(
        f"{decision_id}\n{relpath}".encode("utf-8", "surrogateescape")).hexdigest()[:12]


def _guard_content_hash(data: bytes) -> str:
    """Full sha1 hex digest of staged file content — the throttle key's value.
    Deliberately untruncated (unlike _guard_hash): this hashes arbitrary file
    content, not a short identity string, so the full digest is cheap insurance
    against collision.

    Takes the RAW staged bytes, not decoded text, so a fingerprint exists for every
    file git could read — including one the scanner then declines (over the size cap,
    or binary). Those are not unknown content: git handed the bytes over and only
    scannability was in question, so they can and must still answer the throttle's
    "has this changed since we last advised". Hashing decoded text instead left them
    with no fingerprint at all, which made them un-throttleable and un-stampable, so
    the pair re-advised on every single commit forever. For a valid-UTF-8 file this
    produces the IDENTICAL digest to the previous `text.encode("utf-8", "replace")`
    form, so on-disk throttle stamps carry over untouched; only a file whose bytes are
    not valid UTF-8 (where the decode was lossy) gets one benign re-advise."""
    return hashlib.sha1(data).hexdigest()


def _guard_dismissed_path(repo_path: str) -> Path:
    return store.STORE_DIR / sidecars.filename("guard_dismissed", slug=store.repo_slug(repo_path))


def _dismissed_guard(repo_path: str) -> set[str]:
    """The set of permanently dismissed guard-pair hashes for this repo. Fail-soft:
    a missing or corrupt file reads as no dismissals, never raises."""
    path = _guard_dismissed_path(repo_path)
    try:
        hashes = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(hashes, list):
            hashes = []
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        hashes = []
    return set(hashes)


def dismiss_guard(repo_path: str, decision_id: str, source_ref: str) -> None:
    """Permanently dismiss one (decision_id, source_ref) advisory pair, repo-wide
    and cross-session. `source_ref` is canonicalized via _guard_relpath before
    hashing, so an absolute or relative spelling of the same file dismisses the
    identical pair. This is the MANAGEMENT path (the CLI's `--dismiss`), not the
    guard run path: unlike every other guard helper it is deliberately NOT
    fail-soft — a write failure here must surface to the developer, not vanish
    silently, since a dismissal the developer believes succeeded but didn't
    would let a "permanent" suppression silently not stick."""
    relpath = _guard_relpath(repo_path, source_ref)
    h = _guard_hash(decision_id, relpath)
    store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
    dismissed = _dismissed_guard(repo_path)
    if h in dismissed:
        return
    dismissed.add(h)
    store.atomic_write(_guard_dismissed_path(repo_path), json.dumps(sorted(dismissed)))


def _guard_advised_path(repo_path: str) -> Path:
    return store.STORE_DIR / sidecars.filename("guard_advised", slug=store.repo_slug(repo_path))


def _guard_advised(repo_path: str) -> dict:
    """{pair_hash: staged_content_sha1} throttle stamp for this repo. Fail-soft:
    a missing or corrupt file reads as {}, never raises."""
    path = _guard_advised_path(repo_path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            data = {}
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        data = {}
    return data


def _guard_stamp_advised(repo_path: str, pairs: dict[str, str]) -> None:
    """Best-effort update of the content-keyed throttle stamp: pairs is
    {pair_hash: staged_content_sha1} for every advisory just surfaced. A refreshed
    entry is moved to the end (re-inserted) so the cap evicts truly-stale pairs
    first, not a pair that just advised again with new content. Capped at
    _GUARD_THROTTLE_CAP, oldest dropped. Never raises — a failed stamp write only
    costs one extra (harmless) re-advisory next run, never blocks anything. The
    except is deliberately BROAD (not just OSError): this runs after the
    advisories are already computed, so ANY failure here — a corrupt stamp file
    deserializing oddly, a JSON encoding error — would otherwise propagate into
    guard_staged's fail-open handler and convert real, computed advisories into
    a spurious "internal error"."""
    try:
        store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        advised = _guard_advised(repo_path)
        for h, content_hash in pairs.items():
            advised.pop(h, None)
            advised[h] = content_hash
        while len(advised) > _GUARD_THROTTLE_CAP:
            advised.pop(next(iter(advised)))
        store.atomic_write(_guard_advised_path(repo_path), json.dumps(advised))
    except Exception:
        pass


def _guard_content_artifacts(content: str) -> list[str]:
    """Path/module-shaped artifacts pulled from decision content, for exact-path
    pairing against a staged file via _artifact_path_match. Deliberately reuses
    the same path/module shapes as retrieval.extract_artifacts, via the primitive
    both share (retrieval.raw_path_artifacts), but WITHOUT that function's word-
    segmentation post-processing step: extract_artifacts is built for BM25/topic
    token overlap and intentionally throws away path structure
    ("contexer/store.py" -> ["contexer", "store", "py"]), which would never
    satisfy _pathlike_artifact (it requires the "." / "/" structure back). The
    guard needs the raw matched span intact so it can compare it against a real
    staged path, so this stays a separate helper rather than a call to
    extract_artifacts."""
    return [a for a in retrieval.raw_path_artifacts(content) if _pathlike_artifact(a)]


def _guard_artifact_reason(artifact: str) -> str:
    return f"module artifact {artifact}" if "/" not in artifact else f"path artifact {artifact}"


class _GuardBudgetExceeded(Exception):
    """Raised inside the Tier-1 pairing engine when guard_staged's wall-clock
    deadline passes mid-evaluation. Never escapes guard_staged (whose fail-open
    handler turns it into {"advisories": [], "violations": [], "error": True});
    exists as a distinct type so the bail-out reads as a budget bail-out rather
    than as an accidental internal error."""


def _guard_artifact_matches(artifact: str, staged_set: set[str],
                             staged_by_base: dict[str, list[str]]) -> list[str]:
    """Every staged path `artifact` pairs with, resolved by LOOKUP rather than a
    scan over the whole staged list — semantically identical to filtering
    staged paths through _artifact_path_match, but O(1) for the two cases that
    demand exact equality:

      * exact relpath equality -> one set membership test;
      * a dotted module -> its two possible spellings, two membership tests.

    Only the "/"-suffix case genuinely needs a scan (any staged path may END
    with the artifact), and even there a suffix match at a path boundary
    REQUIRES the last segment to be equal — so the scan runs over just the
    staged paths sharing the artifact's basename, never the full list. The
    endswith test is still applied, so the semantics are exactly
    _artifact_path_match's ("za/utils.py" still doesn't match "a/utils.py")."""
    if "/" not in artifact:
        if artifact in staged_set:
            return [artifact]
        if _GUARD_MODULE_ARTIFACT_RE.match(artifact):
            as_path = artifact.replace(".", "/")
            return [p for p in (f"{as_path}.py", f"{as_path}/__init__.py")
                    if p in staged_set]
        return []
    out = [artifact] if artifact in staged_set else []
    suffix = "/" + artifact
    out.extend(p for p in staged_by_base.get(artifact.rsplit("/", 1)[-1], ())
               if p.endswith(suffix))
    return out


def _guard_pairs(repo_path: str, staged: list[str], decisions: list[dict] | None = None,
                  deadline: float | None = None) -> list[dict]:
    """Candidate generation: pair every staged file against every trusted decision
    from the repo store AND the global store (loaded via load / load_global).
    `decisions=` overrides BOTH loaded sources with the given list (tagged
    scope="personal") — an extension point that keeps this engine reusable by a
    future CI runner without touching this function's body.

    A candidate is produced only when there is an actual pairing SIGNAL: the
    staged file is one of the decision's `source_files` (repo-store entries
    only — global entries never carry source_files and pair via artifact match
    only), or a path/module-shaped artifact extracted from the decision's
    content matches the staged path (_artifact_path_match). No signal -> no
    candidate at all (not even a rejected one). When a signal exists, the
    decision must ALSO pass _guard_trusted or the candidate is emitted=False
    with reason="rejected: untrusted provenance" — trust is checked AFTER
    matching so an untrusted/irrelevant decision never manufactures noise for
    every staged file it happens not to mention.

    `deadline` (a time.time() value, from guard_staged) bounds this half of the
    run exactly like the Tier-2 half: an overrun raises _GuardBudgetExceeded
    rather than finishing an unbounded scan. Checked once per decision, which is
    the loop that scales with store size.

    Each candidate: {decision_id, title, file, hash, scope, reason, emitted}."""
    staged_rel = [p for p in (_guard_relpath(repo_path, s) for s in staged) if p]
    if not staged_rel:
        return []
    if decisions is not None:
        sources: list[tuple[list[dict], str]] = [(decisions, "personal")]
    else:
        sources = [(store.load(repo_path).get("entries") or [], "personal"),
                   (store.load_global().get("entries") or [], "global")]

    # Built ONCE for the whole call: matching is then a lookup per artifact
    # instead of a scan over every staged path per artifact (the pairing loop
    # used to be files x decisions x artifacts — seconds on a large staged set).
    staged_set = set(staged_rel)
    staged_by_base: dict[str, list[str]] = {}
    for relpath in staged_rel:
        staged_by_base.setdefault(relpath.rsplit("/", 1)[-1], []).append(relpath)

    candidates: list[dict] = []
    for entries, scope in sources:
        for entry in entries:
            if deadline is not None and time.time() > deadline:
                raise _GuardBudgetExceeded
            decision_id = entry.get("id", "")
            rev = revisions.current_revision(entry)
            content = rev.get("content", "") if rev else entry.get("content", "")
            title = entry.get("title") or revisions.derive_title(content)
            # source_files goes through _guard_relpath like every other path the
            # guard compares — a decision anchored with an absolute or "./"-
            # prefixed spelling must still pair (fail-soft: unresolvable -> dropped).
            source_files = ({p for p in (_guard_relpath(repo_path, f)
                                          for f in (entry.get("source_files") or [])) if p}
                             if scope == "personal" else set())
            # relpath -> reason, source_files winning over artifacts (as before),
            # and the FIRST matching artifact winning among artifacts.
            matched: dict[str, str] = {p: "source_files match"
                                       for p in source_files & staged_set}
            for artifact in _guard_content_artifacts(content):
                reason = _guard_artifact_reason(artifact)
                for relpath in _guard_artifact_matches(artifact, staged_set, staged_by_base):
                    matched.setdefault(relpath, reason)
            if not matched:
                continue
            trusted = _guard_trusted(entry)
            # Emitted in staged order, which is the order the old nested loop produced.
            for relpath in staged_rel:
                reason = matched.get(relpath)
                if reason is None:
                    continue
                h = _guard_hash(decision_id, relpath)
                if not trusted:
                    candidates.append({"decision_id": decision_id, "title": title,
                                        "file": relpath, "hash": h, "scope": scope,
                                        "reason": "rejected: untrusted provenance",
                                        "emitted": False})
                    continue
                candidates.append({"decision_id": decision_id, "title": title,
                                    "file": relpath, "hash": h, "scope": scope,
                                    "reason": reason, "emitted": True})
    return candidates


def _guard_evaluate(repo_path: str, staged: list[str], decisions: list[dict] | None = None,
                     deadline: float | None = None) -> list[dict]:
    """_guard_pairs plus the dismiss/throttle checks — still entirely read-only
    (no stamp mutation, that's guard_staged's job after capping). Every
    candidate from _guard_pairs comes through; one that matched, was trusted,
    but is dismissed or throttled has its reason/emitted flipped to say why it
    stops short of surfacing. A staged file's content is read (and hashed) at
    most once per call regardless of how many decisions pair to it — and only
    for a pair the throttle actually has a stamp for, so a run that surfaces N
    fresh advisories costs no `git show` per pair.

    `deadline` is passed straight through to _guard_pairs (see there)."""
    pairs = _guard_pairs(repo_path, staged, decisions, deadline)
    if not pairs:
        return pairs
    dismissed = _dismissed_guard(repo_path)
    advised = _guard_advised(repo_path)
    content_hashes: dict[str, str | None] = {}

    def _content_hash_for(relpath: str) -> str | None:
        """The staged blob's fingerprint, or None when git could not read the file at
        all. None is NOT interchangeable with the hash of "": the throttle's whole
        question is "has this file changed since we last advised on it", and an
        unreadable file cannot answer it. Hashing the old `""` return answered "no,
        unchanged" for every unreadable file forever, which froze the pair. Note this
        is the fingerprint, not a hash of the scanned text, so a file that was read but
        declined as too-large or binary still answers normally and still throttles."""
        if relpath not in content_hashes:
            content_hashes[relpath] = _staged_content(repo_path, relpath)[2]
        return content_hashes[relpath]

    out: list[dict] = []
    for p in pairs:
        if not p["emitted"]:
            out.append(p)
            continue
        if p["hash"] in dismissed:
            out.append({**p, "reason": "rejected: dismissed", "emitted": False})
            continue
        stamped = advised.get(p["hash"])
        # Short-circuit order is load-bearing (see the docstring's "only for a pair the
        # throttle actually has a stamp for"): no stamp means no `git show` at all.
        # An unreadable file (hash None) is deliberately NOT throttled — unproven
        # sameness must surface, not suppress.
        current = _content_hash_for(p["file"]) if stamped is not None else None
        if stamped is not None and current is not None and stamped == current:
            out.append({**p, "reason": "rejected: throttled (content unchanged)",
                        "emitted": False})
            continue
        out.append(p)
    return out


def _guard_staged_paths(repo_path: str, paths: list[str] | None) -> list[str]:
    """Canonicalized staged-file list: `paths` (canonicalized) when explicitly
    given, else the real `git diff --cached` staged set. Shared by guard_staged
    and guard_candidates so both agree on what "staged" means."""
    if paths is not None:
        raw = paths
    else:
        raw = _staged_files(repo_path)
    return [p for p in (_guard_relpath(repo_path, s) for s in raw) if p]


# ── Assisted anchor backfill (Task 1 of #175) — read-only candidate derivation ─
# for `contexer guard anchors`. The stock converter: the existing corpus is
# unanchored (trusted+anchored == 0 on real stores), so this mines each trusted
# decision's OWN content for candidate anchors instead of waiting for a future
# capture/approval to anchor it. Read-only (never calls save); the CLI (cli.py's
# _guard_anchors) ratifies selections per decision and applies them in one batch
# via store.apply_backfill_anchors.

def _artifact_path_spellings(artifact: str) -> list[str]:
    """Every repo-relative path spelling `artifact` could refer to, before an
    existence check. The literal artifact is always tried first — a two-segment
    filename like "config.yaml" also satisfies _GUARD_MODULE_ARTIFACT_RE's
    lowercase-dotted-segments shape, so treating module-mapping as exclusive of
    the literal spelling would mistake it for a "config/yaml.py" module and
    never try the real file. When the artifact is ALSO dotted-module-shaped
    (mirrors _artifact_path_match's / _guard_artifact_matches's module-mapping),
    its two possible file spellings are appended as further guesses. The caller's
    existence check is what actually decides which spelling (if any) is real."""
    candidates = [artifact]
    if "/" not in artifact and _GUARD_MODULE_ARTIFACT_RE.match(artifact):
        as_path = artifact.replace(".", "/")
        candidates.append(f"{as_path}.py")
        candidates.append(f"{as_path}/__init__.py")
    return candidates


def _candidate_paths_for_entry(repo: str, repo_root: Path, content: str) -> list[str]:
    """Existing-file anchor candidates for one decision's content: every
    path-like artifact it mentions (_guard_content_artifacts), expanded to its
    possible file spellings (_artifact_path_spellings), canonicalized, and kept
    only if the file exists in the working tree. Deduped (first-seen order)
    and capped at store.MAX_SOURCE_FILES — the same cap _anchor_sources
    itself enforces on write."""
    seen: set[str] = set()
    results: list[str] = []
    for artifact in _guard_content_artifacts(content):
        for raw_path in _artifact_path_spellings(artifact):
            resolved = _guard_relpath(repo, raw_path)
            if resolved in seen or _escapes_repo(resolved):
                continue
            if not (repo_root / resolved).is_file():
                continue
            seen.add(resolved)
            results.append(resolved)
            if len(results) >= store.MAX_SOURCE_FILES:
                return results
    return results


def anchor_candidates_for_backfill(repo_path: str) -> list[dict]:
    """For every trusted, unanchored decision in the repo store, derive
    candidate anchor paths from its content (_candidate_paths_for_entry) and
    skip decisions with no surviving candidates — no rename detection, so a
    renamed file yields nothing rather than a guess.

    Returns [{decision_id, title, candidates}, ...]. Read-only (never calls
    save) and fail-soft (any failure returns [])."""
    try:
        repo = store.resolve_repo(repo_path)
        entries = store.load(repo).get("entries") or []
        repo_root = Path(repo)
        results: list[dict] = []
        for entry in entries:
            if entry.get("source_files"):
                continue
            if not _guard_trusted(entry):
                continue
            rev = revisions.current_revision(entry)
            content = rev.get("content", "") if rev else entry.get("content", "")
            title = entry.get("title") or revisions.derive_title(content)
            candidates = _candidate_paths_for_entry(repo, repo_root, content)
            if not candidates:
                continue
            results.append({"decision_id": entry.get("id", ""), "title": title,
                             "candidates": candidates})
        return results
    except Exception:
        return []


# ── Commit-time guard: Tier-2 armed rules (Task 3) — machine-checkable, ──────
# blocking. Two paths, sharply separated:
#   MANAGEMENT (arm_guard / disarm_guard): under store_lock, WRITES the store,
#   and MAY raise ValueError — arming/disarming is a deliberate developer act, so
#   a malformed request should fail loudly, not degrade silently.
#   RUN (_armed_rules / _rule_violations, and guard_staged's violations half):
#   store-READ-ONLY and fail-soft, exactly like the Tier-1 engine above — rule
#   evaluation must never raise out of guard_staged and never block a commit on
#   its own failure (see _GUARD_TIME_BUDGET below: a catastrophic regex fails
#   OPEN, never partial-blocks).

_GUARD_CHECK_TYPES = frozenset({"regex", "secret"})
_GUARD_MACHINE_CHECKABLE_MSG = "guard rules must be machine-checkable"
# Wall-clock seconds, across the WHOLE guard_staged call: the deadline is stamped
# at the top of guard_staged and threaded through BOTH tiers — Tier-2 rule
# evaluation (checked between files/rules) and Tier-1 pairing (checked per
# decision, bailing via _GuardBudgetExceeded). Either overrun fails OPEN with
# error=True; neither tier can run unbounded while the developer waits to commit.
_GUARD_TIME_BUDGET = 2.0


def _validate_guard_check(check_type: str, pattern: str, flags: str) -> None:
    """Refuse anything not deterministically machine-checkable — the structural
    half of arm_guard's refusal contract (entry existence and approval status are
    checked by the caller, which needs the store loaded first). Every failure
    here raises the SAME message: the caller only needs to know arming was
    refused because the request wasn't checkable, not which specific rule of
    the check tripped."""
    if check_type not in _GUARD_CHECK_TYPES:
        raise ValueError(_GUARD_MACHINE_CHECKABLE_MSG)
    if check_type == "secret":
        # `secret` always means "match redact.HIGH_CONFIDENCE_PATTERNS" — a
        # pattern alongside it is nonsensical, not merely redundant.
        if pattern:
            raise ValueError(_GUARD_MACHINE_CHECKABLE_MSG)
        return
    if not pattern:
        raise ValueError(_GUARD_MACHINE_CHECKABLE_MSG)
    if set(flags) - {"i"}:
        raise ValueError(_GUARD_MACHINE_CHECKABLE_MSG)
    try:
        re.compile(pattern, re.IGNORECASE if "i" in flags else 0)
    except re.error:
        raise ValueError(_GUARD_MACHINE_CHECKABLE_MSG)


def arm_guard(repo_path: str, entry_id: str, check_type: str, pattern: str = "",
              flags: str = "", paths: str = "", message: str = "") -> str:
    """Arm a decision with a machine-checkable commit-time rule — the blocking
    (Tier-2) counterpart to Tier-1's advisory pairing. MANAGEMENT path: under
    store_lock, may raise ValueError (see _validate_guard_check for the
    machine-checkable refusals; separately refuses an entry that doesn't exist,
    or one whose entry_status isn't "approved" — an armed rule must already be
    developer-trusted, since arming an unreviewed AI guess would let it block a
    commit no human ever signed off on).

    Id resolution mirrors approve_decision's (_apply_approval): exact id, then
    an 8-char prefix — tried against the REPO store first, then the GLOBAL
    store, so a global armed rule (see _armed_rules) also blocks every repo's
    commits, matching how global rules are already injected everywhere else."""
    _validate_guard_check(check_type, pattern, flags)
    repo = store.resolve_repo(repo_path)
    guard_check = {"type": check_type, "pattern": pattern, "flags": flags,
                    "paths": paths, "message": message,
                    "armed_at": datetime.now(timezone.utc).isoformat()}

    with store.store_lock(store.repo_slug(repo)):
        data = store.load(repo)
        entry = store.entry_by_id(data["entries"], entry_id)
        if entry is not None:
            if store.entry_status(entry) != "approved":
                raise ValueError("only approved decisions can be armed")
            entry["guard_check"] = guard_check
            store.save(repo, data)
            return f"Armed {entry['id'][:8]} ({check_type})."

    with store.store_lock(store.GLOBAL_SLUG):
        data = store.load_global()
        entry = store.entry_by_id(data["entries"], entry_id)
        if entry is not None:
            if store.entry_status(entry) != "approved":
                raise ValueError("only approved decisions can be armed")
            entry["guard_check"] = guard_check
            store.save_global(data)
            return f"Armed {entry['id'][:8]} ({check_type})."

    raise ValueError(f"Decision {entry_id!r} not found.")


def disarm_guard(repo_path: str, entry_id: str) -> str:
    """Remove a decision's guard_check (Tier-2 armed rule), repo store first then
    global — same id-resolution order as arm_guard. MANAGEMENT path: under
    store_lock, raises ValueError when the id resolves in neither store. A
    resolved entry that isn't currently armed is a no-op (not an error) —
    disarming an already-unarmed decision is a harmless idempotent request."""
    repo = store.resolve_repo(repo_path)

    with store.store_lock(store.repo_slug(repo)):
        data = store.load(repo)
        entry = store.entry_by_id(data["entries"], entry_id)
        if entry is not None:
            had_check = entry.pop("guard_check", None) is not None
            if had_check:
                store.save(repo, data)
                return f"Disarmed {entry['id'][:8]}."
            return f"{entry['id'][:8]} was not armed."

    with store.store_lock(store.GLOBAL_SLUG):
        data = store.load_global()
        entry = store.entry_by_id(data["entries"], entry_id)
        if entry is not None:
            had_check = entry.pop("guard_check", None) is not None
            if had_check:
                store.save_global(data)
                return f"Disarmed {entry['id'][:8]}."
            return f"{entry['id'][:8]} was not armed."

    raise ValueError(f"Decision {entry_id!r} not found.")


def _armed_rules(entries: list[dict]) -> list[dict]:
    """The subset of `entries` that are BOTH carrying a guard_check AND STILL
    entry_status == "approved" right now — status is re-checked at RUN time,
    never trusted from arm time, so a decision later ignored or superseded
    stops firing without an explicit disarm. Pure, no I/O; the caller gathers
    from repo + global stores by calling this once per store and concatenating
    (see guard_staged), so a global armed rule fires in every repo's run."""
    return [e for e in entries if e.get("guard_check") and store.entry_status(e) == "approved"]


def _rule_selects(rule: dict, path: str) -> bool:
    """Whether one armed rule's `paths` glob applies to `path` (no glob = every file).

    Extracted so the ONE definition serves both callers rather than each keeping its own
    copy: `_rule_violations` uses it to decide what to scan, and `_guard_violations` uses
    it to decide whether an unscannable file is worth REPORTING as unchecked. Without it
    there, a rule armed `--paths "src/**/*.py"` made a staged 4MB `data/dump.json` print
    a "not checked by armed rules" line on every commit, naming a gap that does not exist
    because no armed rule would ever have been run against that file."""
    paths_glob = (rule.get("guard_check") or {}).get("paths") or ""
    return not paths_glob or fnmatch.fnmatch(path, paths_glob)


def _rule_violations(rules: list[dict], path: str, content: str) -> list[dict]:
    """Evaluate every entry in `rules` (as returned by _armed_rules) against one
    staged file's content. `path` must already be _guard_relpath's canonical
    output. A rule whose guard_check["paths"] glob doesn't fnmatch `path` is
    skipped entirely (empty paths = applies to every staged file).

    `regex` rules match line-by-line (so a hit's reported line number is exact);
    an unparseable pattern (defensive only — arm_guard already validates at arm
    time) is skipped rather than raised. `secret` rules match any hit from
    redact.HIGH_CONFIDENCE_PATTERNS against the WHOLE file content, not
    per-line — the PEM private-key pattern spans multiple lines (BEGIN/…/END),
    so per-line splitting would silently defeat it; the line number is then
    derived from the match's character offset via a newline count.

    Each hit: {path, line, decision_id, title, message}."""
    out: list[dict] = []
    for rule in rules:
        gc = rule.get("guard_check") or {}
        if not _rule_selects(rule, path):
            continue
        decision_id = rule.get("id", "")
        title = rule.get("title") or revisions.derive_title(revisions.current_content(rule))
        message = gc.get("message") or ""
        check_type = gc.get("type")

        if check_type == "regex":
            flags = re.IGNORECASE if "i" in (gc.get("flags") or "") else 0
            try:
                compiled = re.compile(gc.get("pattern", ""), flags)
            except re.error:
                continue
            for lineno, line in enumerate(content.splitlines(), start=1):
                if compiled.search(line):
                    out.append({"path": path, "line": lineno, "decision_id": decision_id,
                                "title": title, "message": message})
        elif check_type == "secret":
            for pat in redact.HIGH_CONFIDENCE_PATTERNS:
                for m in pat.finditer(content):
                    lineno = content.count("\n", 0, m.start()) + 1
                    out.append({"path": path, "line": lineno, "decision_id": decision_id,
                                "title": title, "message": message})
    return out


def _guard_violations(repo: str, staged: list[str],
                      deadline: float) -> tuple[list[dict], list[dict], bool]:
    """Run every armed rule (repo + global stores) against every staged file,
    checked against `deadline` (an absolute time.time() value) between files AND
    between rules — Python's `re` has no per-call timeout, so this is the only
    budget enforcement possible; a single catastrophically backtracking regex
    can still overrun mid-match, which is the documented, deliberate residual
    risk (fail OPEN when that happens, never partial-block). Returns
    (violations, unchecked, budget_exceeded); on overrun the caller discards
    whatever violations were gathered so far — an overrun run reports nothing, not a
    partial scan, so a commit is never blocked on an incomplete evaluation.

    `unchecked` is the honesty half: one `{"file", "reason"}` row per staged file an
    armed rule could not be run against, so a skip is REPORTED rather than passing as
    a clean result. Same principle as `anchors._BudgetExceeded` and `cli._budgeted`'s
    "(git is slow, checks skipped this run)" row: a check that did not happen must not
    read as a check that found nothing. Three disciplines keep it signal rather than
    nagging. Only reasons in _GUARD_UNCHECKED_REPORTED qualify, so a staged PNG stays
    silent. It is empty when no rule is armed, which is accurate rather than a gap:
    nothing needed checking, so nothing went unchecked. And a file is reported only when
    some armed rule's `paths` glob actually SELECTS it (`_rule_selects`), so a rule
    scoped to `src/**/*.py` does not make an unscannable `data/dump.json` look like
    missed coverage.

Scanning stops at `deadline - _GUARD_SCAN_RESERVE` and the files it did not reach are
    reported through the same list (reason `budget`) rather than being left to the hard
    deadline. That ordering is deliberate: the deadline fails OPEN by discarding every
    violation gathered so far, so reaching it costs the report even for the files that
    scanned cleanly, whereas stopping early keeps those violations and names only what
    was missed. The first selected file always scans, so a run always makes progress."""
    rules = (_armed_rules(store.load(repo).get("entries") or [])
             + _armed_rules(store.load_global().get("entries") or []))
    if not rules:
        return [], [], False
    violations: list[dict] = []
    unchecked: list[dict] = []
    # Stop scanning with _GUARD_SCAN_RESERVE left, so the run returns normally (violations
    # kept, gaps named) rather than tripping the hard deadline, which discards everything.
    soft_deadline = deadline - _GUARD_SCAN_RESERVE
    checked_any = False

    def _note(relpath: str, reason: str) -> None:
        if reason in _GUARD_UNCHECKED_REPORTED:
            unchecked.append({"file": relpath, "reason": reason})

    for relpath in staged:
        if time.time() > deadline:
            return [], [], True
        # Scope is decided ONCE, here, before the file is read or charged to the budget,
        # and the rest of the loop trusts it. Applicability used to be settled inside
        # `_rule_violations` instead, which meant a file no rule selects was still
        # `git show`n and still charged to the budget before being scanned against nothing.
        # That is a security bug, not just waste: arm one rule `--paths "src/*.py"`, stage
        # a few MB of `data/*.json` next to one `src/app.py` holding a secret, and the
        # out-of-scope bulk exhausts the scan budget first, so the one file the rule
        # covers is skipped with a non-blocking notice and its violation ships. Deciding
        # once also keeps this and the `unchecked` gate from being two rules that can
        # drift, which is why `_note` no longer re-tests it.
        selectors = [r for r in rules if _rule_selects(r, relpath)]
        if not selectors:
            continue
        # `checked_any` guarantees forward progress: if the process arrived here already
        # past the soft cut-off (a slow store load, a loaded machine), the first selected
        # file is still scanned rather than the run reporting a gap for everything.
        if checked_any and time.time() > soft_deadline:
            _note(relpath, "budget")
            continue
        content, reason, _fingerprint = _staged_content(repo, relpath)
        if reason is not None:
            _note(relpath, reason)
            continue
        if not content:
            continue
        checked_any = True
        for rule in selectors:
            if time.time() > deadline:
                return [], [], True
            violations.extend(_rule_violations([rule], relpath, content))
    return violations, unchecked, False


def guard_staged(repo_path: str, paths: list[str] | None = None) -> dict:
    """The commit-time entrypoint (Task 4's CLI hook) combining Tier-1's
    advisory engine with Tier-2's armed blocking rules. Store-READ-ONLY (never
    calls save/save_global) and fail-soft: the ENTIRE body is wrapped so any
    exception — or a Tier-2 time-budget overrun — degrades to
    {"advisories": [], "violations": [], "error": True} rather than raising or
    ever blocking a commit on the guard's OWN failure.

    Order: CONTEXER_GUARD=0 short-circuits before any other work; then the
    deadline is stamped — _GUARD_TIME_BUDGET wall-clock seconds covering
    EVERYTHING below it, both tiers; then repo resolution; then the staged-file
    list (empty -> empty result); then Tier-2 violations run FIRST (budget
    checked between files/rules), then Tier-1 pairing (checked per decision, via
    _GuardBudgetExceeded) — on overrun in either tier, both advisories and
    violations come back empty with error=True, since a hung run must fail open,
    never half-block; then the merge-in-progress check (Tier-1's advisory pairing is
    skipped during a merge, but Tier-2's violations already ran above and are
    still reported — a merge conflict is not a license to skip a blocking
    rule); then pairing -> drop dismissed -> drop throttled -> cap at
    _GUARD_MAX_ADVISORIES (the true count is reported as "total_advisories"
    only when capping actually happened) -> best-effort stamp the throttle for
    exactly the advisories that surfaced (a pair pushed past the cap is NOT
    stamped, so it's free to surface next run once something ahead of it
    clears).

    "unchecked" carries the staged files an armed rule could not be run against
    (see _guard_violations), and like "total_advisories" the key is present only
    when there is something to report, so a clean run's dict shape is unchanged.
    It never affects the exit code: a file the guard could not read is a gap in
    the report, not a violation to block on."""
    try:
        if os.environ.get("CONTEXER_GUARD") == "0":
            return {"advisories": [], "violations": [], "skipped": "env"}
        deadline = time.time() + _GUARD_TIME_BUDGET
        repo = store.resolve_repo(repo_path)
        staged = _guard_staged_paths(repo, paths)
        if not staged:
            return {"advisories": [], "violations": []}

        violations, unchecked, budget_exceeded = _guard_violations(repo, staged, deadline)
        if budget_exceeded:
            return {"advisories": [], "violations": [], "error": True}

        if _merge_in_progress(repo):
            result: dict = {"advisories": [], "violations": violations, "skipped": "merge"}
            if unchecked:
                result["unchecked"] = unchecked
            return result

        evaluated = _guard_evaluate(repo, staged, deadline=deadline)
        surfaced = [p for p in evaluated if p["emitted"]]
        capped = surfaced[:_GUARD_MAX_ADVISORIES]
        result = {"advisories": capped, "violations": violations}
        if unchecked:
            result["unchecked"] = unchecked
        if len(surfaced) > len(capped):
            result["total_advisories"] = len(surfaced)
        if capped:
            # Keyed on the fingerprint, so a file that was read but declined as
            # too-large or binary IS stamped and throttles like any other. Only a file
            # git could not read at all is left unstamped: the stamp asserts "we advised
            # on exactly this content", and there is no content to say that about.
            # Stamping the empty-string hash there is what froze such a pair permanently,
            # since every later unreadable read matched it.
            stamps = {}
            for p in capped:
                fingerprint = _staged_content(repo, p["file"])[2]
                if fingerprint is not None:
                    stamps[p["hash"]] = fingerprint
            if stamps:
                _guard_stamp_advised(repo, stamps)
        return result
    except Exception:
        return {"advisories": [], "violations": [], "error": True}


def guard_candidates(repo_path: str, paths: list[str] | None = None, explain: bool = False) -> list[dict]:
    """Read-only `--explain` counterpart to guard_staged: the identical pairing +
    dismiss/throttle pipeline, but NEVER mutates the throttle stamp (or anything
    else). explain=False returns only candidates that would actually surface
    right now (matched, trusted, not dismissed, not throttled) — unlike
    guard_staged, NOT capped at _GUARD_MAX_ADVISORIES, since this is a diagnostic
    listing, not the noise-controlled commit-time nag. explain=True additionally
    includes every rejected candidate, each carrying its reason. Fail-soft: any
    exception -> []."""
    try:
        repo = store.resolve_repo(repo_path)
        staged = _guard_staged_paths(repo, paths)
        if not staged:
            return []
        evaluated = _guard_evaluate(repo, staged)
        if explain:
            return evaluated
        return [p for p in evaluated if p["emitted"]]
    except Exception:
        return []


# ── Decisions-for-files retrieval (Task 1 of #174) — reuses the Tier-1 pairing ─
# signals (source_files membership, path-like content artifacts) but is pure
# RETRIEVAL, not advisory noise control: unlike _guard_pairs there is no
# guard-trust filter (a pending/suggested decision can still govern a file —
# the caller decides what to do with its status) and no throttle/dismissals
# (nothing here is ever surfaced repeatedly at commit time, so there is
# nothing to suppress). Every non-ignored entry of BOTH stores participates.

def decisions_for_files(repo_path: str, files: list[str],
                         decisions: list[dict] | None = None) -> list[dict]:
    """Which stored decisions govern the given files: `[{decision_id, title, status,
    scope, files_matched, reason}]`, one entry per matching decision. `files` may be
    repo-relative or absolute; each is canonicalized via `_guard_relpath` and any
    path that escapes the repo (`_escapes_repo`) is dropped before pairing, exactly
    like every other path this module compares.

    Pairing signal per decision (mirrors `_guard_pairs`): the decision's
    `source_files` (repo-store entries only — global entries never carry
    `source_files` and pair via artifact match only), OR a path/module-shaped
    artifact extracted from its content (`_guard_content_artifacts`) matching one
    of the given files (`_artifact_path_match`, via the same O(1)
    `_guard_artifact_matches` lookup `_guard_pairs` uses). No signal -> the
    decision is simply absent from the result, not emitted-false — there is no
    rejected-candidate concept here, only hits.

    `files_matched` lists, in the CALLER'S input order, which of the queried files
    matched this decision — the reverse-tracing property: given a decision back
    from this call, the caller can tell which of the files it asked about are the
    ones this decision actually governs. `reason` is the single strongest signal
    across every matched file for this decision — `source_files match` beats any
    artifact reason, mirroring `_guard_pairs`' per-file `matched.setdefault` order
    (source_files inserted first, artifacts only fill gaps).

    `decisions=` overrides BOTH loaded stores with the given list (tagged
    scope="personal"), the same extension point `_guard_pairs` offers.

    Fail-soft: any exception -> []."""
    try:
        canon = [p for p in (_guard_relpath(repo_path, f) for f in (files or []))
                 if p and not _escapes_repo(p)]
        if not canon:
            return []
        if decisions is not None:
            sources: list[tuple[list[dict], str]] = [(decisions, "personal")]
        else:
            sources = [(store.load(repo_path).get("entries") or [], "personal"),
                       (store.load_global().get("entries") or [], "global")]

        canon_set = set(canon)
        canon_by_base: dict[str, list[str]] = {}
        for relpath in canon:
            canon_by_base.setdefault(relpath.rsplit("/", 1)[-1], []).append(relpath)

        hits: list[dict] = []
        for entries, scope in sources:
            for entry in entries:
                if entry.get("type") != "decision" or store.entry_status(entry) == "ignored":
                    continue
                decision_id = entry.get("id", "")
                rev = revisions.current_revision(entry)
                content = rev.get("content", "") if rev else entry.get("content", "")
                title = entry.get("title") or revisions.derive_title(content)
                source_files = ({p for p in (_guard_relpath(repo_path, f)
                                              for f in (entry.get("source_files") or [])) if p}
                                 if scope == "personal" else set())
                # relpath -> reason, source_files winning over artifacts — same
                # setdefault order _guard_pairs uses for its per-file matched dict.
                matched: dict[str, str] = {p: "source_files match"
                                           for p in source_files & canon_set}
                for artifact in _guard_content_artifacts(content):
                    reason = _guard_artifact_reason(artifact)
                    for relpath in _guard_artifact_matches(artifact, canon_set, canon_by_base):
                        matched.setdefault(relpath, reason)
                if not matched:
                    continue
                files_matched = [p for p in canon if p in matched]
                reason = ("source_files match"
                          if any(r == "source_files match" for r in matched.values())
                          else matched[files_matched[0]])
                hits.append({
                    "decision_id": decision_id,
                    "title": title,
                    "status": store.entry_status(entry),
                    "scope": scope,
                    "files_matched": files_matched,
                    "reason": reason,
                })
        return hits
    except Exception:
        return []

