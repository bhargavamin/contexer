"""Repo-scoped policy evaluation: the ONE place an operation becomes a policy answer.

`policy.py` is pure - it judges a request against policies somebody else already loaded. The
four steps in front of that (resolve the repo, load the participating decisions, select,
evaluate) are the same for every general caller, and there are now two of them: the
`evaluate_policy` MCP tool and `contexer policy evaluate`. They exist here ONCE rather than
twice, following the `console_api.py` precedent: a thin layer ABOVE store, store-owned helpers
read through the store MODULE OBJECT at call time (so a value a test patches on
`contexer.store` is still seen), and a one-way dependency - `server.py`/`cli.py` ->
`policy_api` -> `store`/`policy`, never back. `policy.py` cannot host any of this: it must
never import store, and a store import here is exactly what that purity buys.

`guard_engine` is deliberately NOT a caller. It is the GIT adapter over the same evaluator and
owns things this layer has no concept of - staged paths, throttle sidecars, a wall-clock
budget - so routing it through here would mean teaching this module about git rather than
sharing anything real.

Two rules this module holds the line on:

BOUNDS. Every string and list bound is `policy.validate_request`'s, applied HERE, at the
chokepoint every caller funnels through - the same shape `remote.bound_source_files` has,
where the one definition is applied again below the surface because a surface schema only
covers the callers that go through that surface. A caller that skips the tool and the CLI
still gets bounded. The one bound a surface must apply for itself is the CLI's read of a diff
file, which has to stop before the bytes are in memory; it reads `policy.MAX_ARTIFACT_BYTES`
rather than stating a second number.

REDACTION IS EGRESS-ONLY. `format_result` scrubs, because rendering is where a secret leaves.
Nothing scrubs the artifact on the way IN - a `secret` check that sees redacted bytes detects
nothing, which is the whole point of `redact.py`'s egress-only rule - and nothing mutates the
structured result the caller receives. The result is authoritative; the text is a rendering.
"""

import hashlib
import os
import stat
import time
from pathlib import Path, PurePosixPath

from contexer import config, decision_impact
from contexer import policy      # pure stdlib leaf (no cycle): vocabularies, validation, judging
from contexer import redact      # pure stdlib leaf: the egress scrub
from contexer import store       # module object, not `from`-imports: see docstring above


_FILE_READ_CHUNK = 16 * 1024
_FILE_EVALUATION_SECONDS = 0.250


def _participants(repo: str) -> list:
    """Every decision that may speak about this repo: its own store plus the global one.

    Both participate for the same reason they do in `guard_engine.decisions_for_files` - a
    cross-repo rule is still a rule here. `select_policies` does the filtering (status, trust,
    applicability), so nothing is pre-filtered on the way in; a global entry simply carries no
    `source_files`, so it can only ever select as an armed rule."""
    personal_data = store.load(repo)
    global_data = store.load_global()
    personal = [dict(
                    entry, _policy_scope="personal",
                    _policy_revision_persisted=store.revision_identity_is_persisted(
                        personal_data, entry))
                for entry in (personal_data.get("entries") or [])
                if isinstance(entry, dict)]
    global_rows = [dict(
                       entry, _policy_scope="global",
                       _policy_revision_persisted=store.revision_identity_is_persisted(
                           global_data, entry))
                   for entry in (global_data.get("entries") or [])
                   if isinstance(entry, dict)]
    return personal + global_rows


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
    empty rather than dishonest - no policy was ever selected, so there is no policy to name
    as unjudged; `errors` is the report."""
    return _answer(repo, policy.build_result("allow", "error", "deterministic", [], [],
                                             policy.policy_set_version([])), errors)


def _artifact_relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > policy._MAX_PATH_CHARS:
        raise ValueError("artifact_invalid_path")
    path = PurePosixPath(value)
    if path.is_absolute() or value != path.as_posix() or any(
            part in ("", ".", "..") for part in path.parts) or any(
                part.casefold() == ".git" for part in path.parts):
        raise ValueError("artifact_invalid_path")
    return path.as_posix()


def _authorized_physical_repo(repo_path: str) -> tuple[str, str, tuple[int, int]]:
    """Resolve a file-read root without ever accepting the shared current-repo pointer."""
    resolved, source = store.resolve_repo_verbose(repo_path)
    if (not resolved or source == "pointer"
            or (bool((repo_path or "").strip()) and source != "argument")):
        raise ValueError("workspace_not_authorized")
    try:
        physical = str(Path(resolved).resolve(strict=True))
        authorized_root = os.stat(physical, follow_symlinks=False)
        if not stat.S_ISDIR(authorized_root.st_mode):
            raise ValueError("workspace_not_authorized")
        grants = config.load_policy_settings(
            store.store_dir() / "config.toml").artifact_read_roots
    except (config.ConfigError, OSError, RuntimeError) as exc:
        raise ValueError("workspace_not_authorized") from exc
    if physical not in grants:
        raise ValueError("workspace_not_authorized")
    return resolved, physical, (authorized_root.st_dev, authorized_root.st_ino)


def _read_confined_file(root: str, relative: str, *,
                        root_identity: tuple[int, int] | None = None) -> tuple[str, dict]:
    """Read one regular UTF-8 file through no-follow directory-relative handles."""
    parts = PurePosixPath(relative).parts
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow or os.open not in getattr(os, "supports_dir_fd", set()):
        raise ValueError("artifact_mode_unsupported")
    cloexec = getattr(os, "O_CLOEXEC", 0)
    directory = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | nofollow | cloexec
    # Opening a FIFO read-only blocks before fstat can reject it. O_NONBLOCK is harmless for
    # regular files and lets us acquire then classify a special leaf without waiting for a
    # peer process. The regular-file check below remains the authority; this flag is only
    # what makes reaching that check bounded.
    leaf_flags = os.O_RDONLY | nofollow | cloexec | getattr(os, "O_NONBLOCK", 0)
    fds: list[int] = []
    try:
        root_fd = os.open(root, directory)
        fds.append(root_fd)
        root_before = os.fstat(root_fd)
        # Bind authorization to the same directory we traverse. Checking only the current
        # pathname after the read would accept a replacement made between grant and open.
        if root_identity is not None and (
                root_before.st_dev, root_before.st_ino) != root_identity:
            raise ValueError("artifact_unstable")
        parent_fd = root_fd
        for part in parts[:-1]:
            child = os.open(part, directory, dir_fd=parent_fd)
            fds.append(child)
            parent_fd = child
        leaf = os.open(parts[-1], leaf_flags, dir_fd=parent_fd)
        fds.append(leaf)
        before = os.fstat(leaf)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError("artifact_not_regular")
        chunks: list[bytes] = []
        size = 0
        while size <= policy.MAX_BOUNDED_FILE_BYTES:
            chunk = os.read(leaf, min(
                _FILE_READ_CHUNK, policy.MAX_BOUNDED_FILE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        if size > policy.MAX_BOUNDED_FILE_BYTES:
            raise ValueError("artifact_too_large")
        after = os.fstat(leaf)
        root_after = os.fstat(root_fd)
        try:
            named_root = os.stat(root, follow_symlinks=False)
        except OSError as exc:
            raise ValueError("artifact_unstable") from exc
        identity_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in identity_fields):
            raise ValueError("artifact_unstable")
        if ((root_before.st_dev, root_before.st_ino) != (root_after.st_dev, root_after.st_ino)
                or (root_after.st_dev, root_after.st_ino)
                != (named_root.st_dev, named_root.st_ino)):
            raise ValueError("artifact_unstable")
        raw = b"".join(chunks)
        if b"\x00" in raw:
            raise ValueError("artifact_binary")
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("artifact_invalid_encoding") from exc
        if any(len(line) > policy.MAX_BOUNDED_FILE_LINE_CHARS for line in text.splitlines()):
            raise ValueError("artifact_line_too_long")
        return text, {
            "path": relative,
            "digest": "sha256:" + hashlib.sha256(raw).hexdigest(),
            "bytes": len(raw),
            "kind": "file_content",
            "provenance": "authorized_server_read",
        }
    except OSError as exc:
        raise ValueError("artifact_unreadable") from exc
    finally:
        for fd in reversed(fds):
            try:
                os.close(fd)
            except OSError:
                pass


def evaluate_operation(repo_path: str, *, intent: str = "", operation: str,
                       files=None, artifact_kind: str = "", artifact: str = "",
                       unchecked=None, artifact_path: str = "",
                       guidance_refs=None) -> dict:
    """Evaluate one operation against this repo's stored policies. Never raises for a bad
    REQUEST - a malformed one comes back as `errors` on an `error`-status answer.

    `artifact_kind`/`artifact` are the flat spelling of `policy`'s nested artifact, because
    both surfaces are flat-argument ones (an MCP tool schema, a CLI flag). The artifact is
    built whenever EITHER is non-empty, so content handed over without a kind is an error
    from `validate_request` rather than content silently dropped. Neither one means the
    request genuinely carries no artifact, and every armed policy then reports `omitted` -
    which is the distinction the guard learned the hard way, and is why a caller that could
    not READ its artifact must pass no artifact plus an `unchecked` row, never empty content.

    `unchecked` is the caller's own gap list (`policy.UNCHECKED_REASONS`), for exactly that:
    a diff file too large to hand over is reported beside the policies it prevented judging,
    not silently absent. Rows are `policy.evaluate_policies`' to validate - a malformed one
    raises there, deliberately, because swallowing a caller's broken gap report turns their
    bug into a false clean verdict.
    """
    if artifact_path and (files or artifact_kind or artifact):
        return _rejected("", ["artifact_path conflicts with files/artifact input"])

    physical_repo = ""
    artifact_meta: dict | None = None
    profile = ""
    if artifact_path:
        try:
            relative = _artifact_relative_path(artifact_path)
            repo, physical_repo, root_identity = _authorized_physical_repo(repo_path)
            artifact, artifact_meta = _read_confined_file(
                physical_repo, relative, root_identity=root_identity)
        except ValueError as exc:
            resolved, _source = store.resolve_repo_verbose(repo_path)
            return _rejected(resolved, [str(exc)])
        files = [relative]
        artifact_kind = "file_content"
        profile = policy.BOUNDED_FILE_PROFILE
    else:
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

    participants = _participants(repo)
    selected = policy.select_policies(
        participants, normalized, include_malformed_armed=bool(profile))
    identities: dict[str, int] = {}
    for item in selected:
        decision_id = str(item.get("decision_id") or "")
        identities[decision_id] = identities.get(decision_id, 0) + 1
    for item in selected:
        decision_id = str(item.get("decision_id") or "")
        sources = [entry for entry in participants
                   if str(entry.get("id") or "") == decision_id
                   and str((policy.current_revision(entry) or {}).get("revision_id") or "")
                   == str(item.get("revision_id") or "")]
        item["identity_ambiguous"] = identities.get(decision_id, 0) > 1 or len(sources) != 1
        if len(sources) == 1:
            source = sources[0]
            item["scope"] = str(source.get("_policy_scope") or "personal")
            item["authority"] = ("human_approved"
                                 if source.get("approved_by") == "human"
                                 else "trusted_approved")
            item["revision_persisted"] = source.get("_policy_revision_persisted") is True
    conditions: list[dict] = []
    deadline = time.monotonic() + _FILE_EVALUATION_SECONDS
    result = policy.evaluate_policies(
        selected, normalized, unchecked, profile=profile,
        observer=conditions.append if profile else None,
        budget_exhausted=(lambda: time.monotonic() >= deadline) if profile else None)
    answer = _answer(repo, result, [])
    if artifact_meta is not None:
        answer["artifact_provenance"] = dict(artifact_meta)
        answer["evaluation_profile"] = profile
        if guidance_refs is None:
            refs = []
        elif isinstance(guidance_refs, (list, tuple)):
            refs = list(guidance_refs[:9])
        else:
            refs = [guidance_refs]
        try:
            linked, linkage_gaps = decision_impact.validate_guidance_refs(
                repo, physical_repo, refs, conditions)
        except Exception:
            linked, linkage_gaps = [], ["history_unavailable"]
        coverage = result["evaluation_status"]
        if not conditions:
            coverage = "no_applicable_conditions"
        elif any(row.get("complete") is True and row.get("verified") is not True
                 for row in conditions):
            coverage = "partial"
        envelope = decision_impact.evaluation_envelope(
            repo, physical_repo, artifact=artifact_meta,
            policy_set_version=result["policy_set_version"], conditions=conditions,
            guidance_refs=linked, linkage_gaps=linkage_gaps,
            coverage=coverage)
        try:
            receipt_id = decision_impact.append(repo, envelope)
        except Exception:
            receipt_id = ""
        if receipt_id:
            answer["receipt_id"] = receipt_id
    return answer


# ── rendering (the egress boundary) ──────────────────────────────────────────────

def scrubbed_result(result: dict) -> dict:
    """A COPY of one answer with every string leaf scrubbed - the egress form of the
    structured result, for a caller that emits it as JSON instead of as text.

    Scrub the leaves and then encode, NEVER encode and then scrub the document: JSON escaping
    rewrites `"` as `\\"`, and `redact`'s keyword-gated generic pattern matches a QUOTED value
    (`password="s3cr3t"`), so scrubbing the encoded text silently stops detecting exactly that
    class while still looking like it works - the high-confidence provider patterns are
    quote-independent and keep matching, so a test written against an AWS key passes either
    way. Encoding is a transformation of the text the detector reads; the detector must run
    first. (Caught in review; the earlier code dumped and then scrubbed.)

    A copy, because the caller's dict stays authoritative and unmutated - the same guarantee
    `format_result` gives.
    """
    if isinstance(result, dict):
        return {k: scrubbed_result(v) for k, v in result.items()}
    if isinstance(result, list):
        return [scrubbed_result(v) for v in result]
    return redact.scrub_text(result) if isinstance(result, str) else result


def _match_line(match: dict, lines: list) -> str:
    """One violated policy, as a line a human or a model reads.

    Both ids, always: `decision_id` is which decision objected and `revision_id` is which
    wording of it did - the same pairing `policy._match` documents, since a decision whose
    text has moved on has not made this objection.

    The offending source line is quoted when the caller supplied the artifact, because a line
    number alone sends the reader back to a file to find out what happened. This is the one
    place a secret can reach the output - `format_result` is what scrubs it, once, for the
    whole render."""
    # Every key here is one `policy._match` always sets, so they are read directly: a missing
    # one is a broken producer, which should surface rather than render as an empty field.
    line = match["line"]
    quoted = lines[line - 1].strip() if isinstance(line, int) and 0 < line <= len(lines) else ""
    where = f" line {line}" if isinstance(line, int) else ""
    note = f" - {match['message']}" if match["message"] else ""
    return (f"  [{match['verdict']}] {match['title'] or '(untitled)'}"
            f" ({match['decision_id']} rev {match['revision_id']}){where}{note}"
            + (f"\n      {quoted}" if quoted else ""))


def format_result(result: dict, artifact: str = "") -> str:
    """Render one answer as text. The ONLY egress point in this module, and the only place
    `redact.scrub` is applied: the structured `result` the caller holds is never mutated, and
    the artifact reached the evaluator as real bytes (a scrubbed artifact is a `secret` check
    that finds nothing).

    Names the verdict, the evaluation status and the basis together, because none of the three
    means anything alone: `allow` with status `partial` is "nothing objected, and some of it
    was never checked". Every gap in `unchecked` is listed with its reason, for the same
    reason the guard lists its own - a check that did not happen must never read as a check
    that found nothing.
    """
    if result.get("errors"):
        body = ("Not evaluated - the request was refused at the gate:\n"
                + "\n".join(f"  - {e}" for e in result["errors"]))
        return redact.scrub_text(body)

    lines = artifact.splitlines() if artifact else []
    out = [f"verdict: {result['verdict']}  (evaluation_status: "
           f"{result['evaluation_status']}, basis: {result['basis']})"]

    matches = result["matches"]
    out.append(f"matched {len(matches)} polic{'y' if len(matches) == 1 else 'ies'}:"
               if matches else "matched no policies.")
    out.extend(_match_line(m, lines) for m in matches)

    gaps = result["unchecked"]
    if gaps:
        out.append(f"unchecked ({len(gaps)}) - these were NOT judged, not judged clean:")
        out.extend(f"  - {g.get('reason', '')}"
                   + (f" [{g['file']}]" if g.get("file") else "")
                   + (f" [decision {g['decision_id']}]" if g.get("decision_id") else "")
                   for g in gaps)

    out.append(f"policy_set: {result['policy_set_version']}")
    provenance = result.get("artifact_provenance") or {}
    if provenance:
        out.append(f"artifact: {provenance.get('path')} ({provenance.get('provenance')}, "
                   f"{provenance.get('bytes')} bytes, {provenance.get('digest')})")
    if result.get("receipt_id"):
        out.append(f"decision_impact_receipt: {result['receipt_id']}")
    return redact.scrub_text("\n".join(out))
