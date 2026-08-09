"""Anchor lifecycle verification: session-start re-check of every anchored decision's
`source_files` against the working tree. `store.py` carries only the session-start call
site (`anchors.verify_anchors`, wired in immediately after `store.verify_scan_conventions`
— same payload re-read convention on change); this module owns the TTL gate, the
git-budgeted rename detection, and the review-gated retirement proposal shape.

Extracted out of store.py (user directive: don't crowd store.py with a second
verification family) but reads/writes the store through the `store` module OBJECT (not
`from`-imported), the same load-order discipline `guard_engine.py` documents at its own
top: `store.STORE_DIR`/`store._load`/`store._save`/`store._store_lock`/... are looked up
at call time, so anything a test monkeypatches on `contexer.store` is still seen here.

Shape mirrors `store.verify_scan_conventions` deliberately:
  - a 24h TTL stamp (`.anchor_verify_<slug>`) is written BEFORE any git work, so a crash
    mid-verification can't retry-storm on the next session start;
  - a fast-path exit — before the stamp is even touched, before any git call — when no
    entry in the store carries `source_files` at all (the common case for every repo
    whose decisions were never anchored);
  - one `_load` + one `_save` under one `_store_lock`, not a write per entry;
  - proposals are attached via `store._build_proposal` and armed for review via
    `store._touch_pending_review`, AFTER the save (same ordering as every other proposal
    site in store.py) — never a direct status flip.

Per anchored, active-status entry with no proposal already pending, each `source_files`
path is classified against the working tree:
  - exists on disk -> nothing.
  - missing, but git confidently identifies exactly one rename target (and that target
    exists now) -> the surviving path list is corrected in place; NOT a review event,
    since the decision's content is unchanged, only its address moved.
  - missing, no confident rename -> collected.

The entry-level outcome then follows from what was collected:
  - partial loss (something survives, whether as-is or renamed) -> the anchor list is
    refreshed to the surviving/renamed set in place; still not a review event.
  - total loss (every anchored file is gone, none renamed) -> a rule-shaped
    `proposed_revision` is attached (current content + a trailing "anchored files no
    longer exist" clause) and the pending-review nudge is armed. Rule-shaped, not
    meta-shaped, for the same hard-learned reason `verify_scan_conventions` documents:
    approving a proposal makes its content the LIVE revision, so it must read like a
    decision a developer can still live with, not a status memo.

Contexer NEVER deletes an anchored decision on its own: the total-loss path only ever
proposes, through the ordinary review flow (`review_pending` / the `.pending_review`
nudge / `contexer review`), and a developer who dismisses that proposal keeps the entry
exactly as it was.

Rename detection is git-budgeted (`_ANCHOR_GIT_BUDGET` git calls per run, `store.
_GIT_FAST_TIMEOUT`-second timeouts, fail-soft throughout): `git log --follow
--diff-filter=R --format=%H -1 -- <old>` finds the most recent commit that renamed the
path, then `git show --format= --name-status <commit> -- <old>` is parsed for the rename
record(s) for that path. Confident means exactly one distinct new path, and that new path
must actually exist in the working tree now — anything else (no commit, no rename record,
more than one distinct target, or a target that itself no longer exists) is treated as a
plain miss, same as no rename at all. Once the budget is exhausted, every further git call
this run short-circuits to "no rename" (a clean stop, never a raised exception) rather than
spawning more subprocesses; entries already processed keep their result.

`anchor_candidates` (unconfirmed, human-not-yet-blessed anchor guesses) are out of scope
for this module by design — they die with review anyway, the same way a plain
`pending_approval` entry does, and are never anchored in the first place (no
`source_files`), so they never enter `verify_anchors`'s participant set."""

import time
from datetime import datetime, timezone
from pathlib import Path

from contexer import store          # module object, not `from`-imports: see docstring above

_ANCHOR_VERIFY_TTL = 86400   # 24h — file layouts don't churn fast enough to re-check every
                              # session start; mirrors store._MINER_VERIFY_TTL.
_ANCHOR_GIT_BUDGET = 10      # git calls per verify_anchors run (rename checks cost up to 2
                              # each, plus one rev-parse per rename found) — a session-start
                              # latency guarantee, same spirit as the guard engine's budgets.

_ACTIVE_STATUSES = ("approved", "suggested")


def _anchor_verify_stamp_path(repo_path: str) -> Path:
    """Same key as every other verification stamp in this codebase: `store._slug`
    applied to the repo_path the session-start caller already resolved, so a writer
    here and the reader on the next session start agree on the file without either
    side re-resolving the repo (see store.verify_scan_conventions's identical
    `_miner_verify_stamp_path`)."""
    return store.STORE_DIR / f".anchor_verify_{store._slug(repo_path)}"


def _run_git(repo_path: str, *args: str) -> str | None:
    """Single git-call chokepoint for anchor verification: every git invocation in this
    module funnels through here, so a test can monkeypatch `anchors._run_git` to count
    calls (fast-path / budget-exhaustion tests) exactly like store's tests monkeypatch
    `miner.mine_conventions`. Fail-soft via `store._git` itself: any git failure is
    `None`, never raises."""
    return store._git(repo_path, *args, timeout=store._GIT_FAST_TIMEOUT)


def _parse_rename_target(name_status: str, old_path: str) -> str | None:
    """Parse the FULL, unfiltered `git show --format= --name-status <commit>` output for
    the rename record(s) whose OLD side is exactly `old_path`. Deliberately not restricted
    via a `-- <old_path>` pathspec on the `show` call itself: git's rename detection is
    computed against the raw two-sided diff, and passing a pathspec that only matches the
    OLD name collapses the pair back down to a plain delete (`D old.py`) instead of the
    rename record (`R100 old.py new.py`) — verified against real git behavior, not just
    documentation. So the full commit diff is parsed here instead, filtered in Python.

    Confident means exactly one DISTINCT new path across every matching R-status line;
    anything else (no rename record for this old path, or more than one distinct target)
    is ambiguous and returns None. Pure — no I/O."""
    targets = set()
    for line in name_status.splitlines():
        parts = line.split("\t")
        if len(parts) >= 3 and parts[0].startswith("R") and parts[1] == old_path:
            targets.add(parts[-1])
    if len(targets) == 1:
        return next(iter(targets))
    return None


def _confident_rename(repo_path: str, old_path: str, repo_root: Path, call) -> str | None:
    """Best-effort git rename detection for one vanished anchor path, using `call` (the
    caller's budget-tracking wrapper around `_run_git`) so a budget-exhausted run
    short-circuits to "no rename" instead of spawning more subprocesses. Confident
    requires BOTH a single distinct rename target from the commit's name-status AND that
    target actually existing in the working tree right now — a target that was itself
    later deleted or renamed again is not a usable address correction.

    Two calls: (1) the most recent commit that touched `old_path` at all (`--follow` so a
    file renamed more than once is still found; `-1` since only the LATEST touch — the
    rename/delete itself — matters here); (2) that commit's full name-status, parsed (see
    _parse_rename_target) for the rename record whose old side is `old_path` — the `show`
    call carries NO pathspec, for the reason documented there."""
    commit = call("log", "--follow", "--format=%H", "-1", "--", old_path)
    if not commit:
        return None
    name_status = call("show", "--format=", "--name-status", commit)
    if not name_status:
        return None
    target = _parse_rename_target(name_status, old_path)
    if target is None or target == old_path:
        return None
    if not (repo_root / target).exists():
        return None
    return target


def verify_anchors(repo_path: str, force: bool = False) -> dict:
    """Re-checks every anchored, active-status decision's `source_files` against the
    working tree, correcting renames in place and proposing (never applying) retirement
    for total losses. Returns {"reanchored": int, "proposed": int}. Called fail-soft from
    store's session-start path, immediately after `verify_scan_conventions`.

    Participants: `type == "decision"`, non-empty `source_files`, `_entry_status` in
    ("approved", "suggested") — the same "active status" set `_rehydrate_working_set`
    uses — and no `proposed_revision` already pending (an entry mid-review is skipped
    outright, never piled onto with a second proposal).

    Fast path (session-start latency): participants are collected from a single `_load`
    BEFORE the TTL stamp is touched and before any git work. Zero participants — every
    repo that has never anchored a decision — returns immediately, with no stamp write
    and no git call at all.

    TTL: a 24h stamp (mtime-based) written BEFORE any per-entry work, so a crash
    mid-verification can't retry-storm the next session start. `force=True` bypasses the
    TTL read (tests only); the stamp is still (re)written so the next un-forced call is
    correctly gated.

    Fail-soft: the entire body is wrapped, so any unexpected failure — a corrupt store, a
    malformed entry, a git surprise beyond what `_run_git` already absorbs — degrades to
    a no-op {"reanchored": 0, "proposed": 0} rather than raising out of session start."""
    try:
        with store._store_lock(store._slug(repo_path)):
            data = store._load(repo_path)
            entries = data.get("entries") or []
            participants = [
                e for e in entries
                if e.get("type") == "decision"
                and e.get("source_files")
                and store._entry_status(e) in _ACTIVE_STATUSES
                and e.get("proposed_revision") is None
            ]
            if not participants:
                return {"reanchored": 0, "proposed": 0}

            stamp = _anchor_verify_stamp_path(repo_path)
            if not force:
                mtime = store._file_mtime(stamp)
                if mtime is not None and time.time() - mtime < _ANCHOR_VERIFY_TTL:
                    return {"reanchored": 0, "proposed": 0}
            try:
                store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
                stamp.touch()
            except OSError:
                pass

            repo_root = Path(repo_path)
            calls_used = 0

            def _call(*args):
                nonlocal calls_used
                if calls_used >= _ANCHOR_GIT_BUDGET:
                    return None
                calls_used += 1
                return _run_git(repo_path, *args)

            now = datetime.now(timezone.utc).isoformat()
            reanchored = 0
            proposed = 0
            review_needed = False
            changed = False

            for entry in participants:
                files = [f for f in entry.get("source_files") or [] if isinstance(f, str)]
                if not files:
                    continue
                surviving: list[str] = []
                missing: list[str] = []
                renamed = False
                for f in files:
                    if (repo_root / f).exists():
                        surviving.append(f)
                        continue
                    target = _confident_rename(repo_path, f, repo_root, _call)
                    if target is not None:
                        surviving.append(target)
                        renamed = True
                    else:
                        missing.append(f)

                if not missing:
                    if renamed:
                        entry["source_files"] = surviving[:store._MAX_SOURCE_FILES]
                        entry["anchor_commit"] = (_call("rev-parse", "HEAD")
                                                  or entry.get("anchor_commit", ""))
                        reanchored += 1
                        changed = True
                    continue

                if surviving:
                    # Partial loss: address correction, not a review event — refresh the
                    # list to what's actually still reachable (as-is or renamed).
                    entry["source_files"] = surviving[:store._MAX_SOURCE_FILES]
                    if renamed:
                        entry["anchor_commit"] = (_call("rev-parse", "HEAD")
                                                  or entry.get("anchor_commit", ""))
                    reanchored += 1
                    changed = True
                    continue

                # Total loss: every anchored file is gone, none renamed. Propose, never
                # apply — the decision keeps rendering its current content until a human
                # reviews the proposal (approve keeps it live with a corrected note,
                # dismiss keeps it exactly as it was, ignore retires it deliberately).
                current = store._current_content(entry)
                proposal_content = (
                    f"{current} (anchored files no longer exist: {', '.join(missing)} — "
                    f"confirm whether this decision still applies)"
                )
                entry["proposed_revision"] = store._build_proposal(
                    entry, proposal_content, "", "", now, source="scan")
                proposed += 1
                changed = True
                review_needed = True

            if changed:
                store._save(repo_path, data)
                if review_needed:
                    store._touch_pending_review(repo_path)  # a retirement now awaits review
            return {"reanchored": reanchored, "proposed": proposed}
    except Exception:
        return {"reanchored": 0, "proposed": 0}
