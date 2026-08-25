"""Anchor lifecycle verification: session-start re-check of every anchored decision's
`source_files` against the working tree. `store.py` carries only the session-start call
site (`anchors.verify_anchors`, wired in immediately after `store.verify_scan_conventions`
— same payload re-read convention on change); this module owns the TTL gate, the
git-budgeted rename detection, and the review-gated retirement proposal shape.

Extracted out of store.py (user directive: don't crowd store.py with a second
verification family) but reads/writes the store through the `store` module OBJECT (not
`from`-imported), the same load-order discipline `guard_engine.py` documents at its own
top: `store.STORE_DIR`/`store.load`/`store.save`/`store.store_lock`/... are looked up
at call time, so anything a test monkeypatches on `contexer.store` is still seen here.

Shape mirrors `store.verify_scan_conventions` deliberately:
  - a 24h TTL stamp (`.anchor_verify_<slug>`) is written BEFORE any git work, so a crash
    mid-verification can't retry-storm on the next session start;
  - a fast-path exit — before the stamp is even touched, before any git call — when no
    entry in the store carries `source_files` at all (the common case for every repo
    whose decisions were never anchored);
  - one `load` + one `save` under one `store_lock`, not a write per entry;
  - proposals are attached via `store.attach_lifecycle_proposal` and armed for review via
    `store._touch_pending_review`, AFTER the save (same ordering as every other proposal
    site in store.py) — never a direct status flip, and never a retirement.

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
  - total loss (every anchored file is gone, none renamed) -> a `proposed_lifecycle`
    retirement (`store.attach_lifecycle_proposal`, source "scan") is attached and the
    pending-review nudge is armed. The decision's CONTENT is never touched: a retirement
    is a state transition, not a rewording, and encoding it as new decision text was
    what the plan's C2 lane exists to stop. Approving it (`store.retire_decision`) moves
    the decision into the tombstone sidecar with its lifecycle history; dismissing it
    (`store.dismiss_lifecycle`) leaves the entry, `source_files` included, exactly as it
    was, so the next TTL cycle re-proposes — dismiss means "not now", not "never ask
    again", until the developer retires the decision or ignores it.

    Dedupe is structural rather than a reason comparison: an entry already carrying a
    `proposed_lifecycle` is not a participant at all, so a sitting proposal can never be
    duplicated. The older content-marker guard stays for one narrow case it still
    answers — a LEGACY entry whose live content already carries the withdrawal clause an
    approved pre-lane proposal wrote — since re-proposing retirement for text that
    already says its anchors were withdrawn is noise.

Contexer NEVER retires an anchored decision on its own: the total-loss path only ever
proposes, through the ordinary review flow (`review_pending` / the `.pending_review`
nudge / `contexer review`), and a developer who dismisses that proposal keeps the entry
exactly as it was.

Rename detection is git-budgeted (`_ANCHOR_GIT_BUDGET` git calls per run, `store.
GIT_FAST_TIMEOUT`-second timeouts, fail-soft throughout) and CHASES THE RENAME CHAIN, not
just one hop: `git log --follow --format=%H -1 -- <old>` finds the most recent commit that
touched the path, then `git show --format= --name-status <commit>` (deliberately no
pathspec — see `_parse_rename_target`) is parsed for the rename record(s) whose old side is
that path. Confident means exactly one distinct new path at THAT hop; anything else (no
commit, no rename record, more than one distinct target) is a plain miss, same as no rename
at all, at whatever hop it happens. If the resolved target itself doesn't exist in the
working tree (the file was renamed again since), the same two-call lookup repeats on that
target, up to `_RENAME_CHAIN_MAX` hops (a -> b -> c -> ... — the plausible case inside a
24h TTL window or after several skipped sessions) — the FIRST hop whose target exists wins.
A chain that never bottoms out within the cap is treated as a plain miss too.

Budget exhaustion is HONEST, not degraded into a false verdict: once the budget is spent,
`_call` raises `_BudgetExceeded` instead of returning a value indistinguishable from "git
found nothing" — returning `None` on exhaustion previously meant a cut-off entry could be
wrongly classified as a total loss (a spurious retirement proposal) or a partial loss (a
wrong list refresh) purely because the budget ran out mid-check, not because git actually
said so. The entries loop catches `_BudgetExceeded` and breaks immediately: the entry
being processed when the budget ran out, and every entry after it, are left completely
UNVERIFIED this run — no proposal, no refresh, no re-anchor, no mutation of any kind —
deferred to the next TTL window rather than guessed at. Achieving this requires that any
entry outcome branch needing one more git call (the `rev-parse HEAD` that refreshes
`anchor_commit` after a rename) makes that call BEFORE mutating the entry, so a mid-branch
exhaustion never leaves a half-applied change behind.

`anchor_candidates` (unconfirmed, human-not-yet-blessed anchor guesses) are never a reason
to participate: participation requires a real `source_files` anchor, which a candidate by
definition is not. They can still RIDE ALONG on a participant, though — an approved entry
that was anchored and also carries a stale guess (candidates survive the pending-twin
promote path and `_route_containment`, neither of which pops them). Under the lifecycle
lane a retired decision leaves the live store entirely, so a leftover guess goes with it
and can no longer be blessed into a fresh anchor by a later approval; the `clear_anchors`
handling in `store._promote_proposal` / `store._apply_approval` that used to be the answer
stays only for LEGACY stores still holding a pre-lane proposal."""

import time
from datetime import datetime, timezone
from pathlib import Path

from contexer import revisions              # pure stdlib leaf (no cycle): revision lifecycle
from contexer import store          # module object, not `from`-imports: see docstring above

_ANCHOR_VERIFY_TTL = 86400   # 24h — file layouts don't churn fast enough to re-check every
                              # session start; mirrors store._MINER_VERIFY_TTL.
_RENAME_CHAIN_MAX = 4
# Cap on rename hops _confident_rename will chase past the first (a -> b -> c -> d -> ...)
# before giving up on a vanished anchor path. A file renamed more than a handful of times
# between two verification runs (a 24h TTL window, or a few skipped sessions) is
# vanishingly rare; chasing an UNBOUNDED chain risks unbounded git-call cost per path on a
# pathological or cyclic history, which is worse than the alternative below. A chain that
# exceeds the cap is treated as NOT confident — same as any other hop that can't produce a
# single, currently-existing target — so the file counts as missing under the entry's
# existing missing/partial/total-loss classification (a possibly-spurious proposal or list
# trim, not a hung verify run); a human reviewing that proposal can always dismiss it.
_ANCHOR_GIT_BUDGET = _RENAME_CHAIN_MAX * 2 * store.MAX_SOURCE_FILES + 1
# git calls per verify_anchors run — a session-start latency guarantee, same spirit as the
# guard engine's budgets. The number is DERIVED, not picked: one entry's worst case is every
# one of its (at most MAX_SOURCE_FILES) anchored paths missing, and each of those paths
# potentially needing the full rename chain (up to _RENAME_CHAIN_MAX hops, 2 calls per hop
# — `log` + `show`) before resolving or giving up, plus at most one `rev-parse HEAD` for
# the outcome — so a budget of `_RENAME_CHAIN_MAX * 2 * MAX_SOURCE_FILES + 1` guarantees
# the FIRST entry of every run always completes. That guarantee is what makes the run make
# forward progress: a smaller budget lets a single fat entry exhaust it inside its own file
# loop, so that entry — and every entry after it — is skipped on every run, forever,
# silently. Later entries can still be cut off (that's the budget doing its job), but they
# are then reached on a subsequent run, once the entries ahead of them have been repaired.

_ACTIVE_STATUSES = ("approved", "suggested")

# The stable wording every anchor-decay withdrawal carries. The CORE is the lifecycle
# proposal's reason; the MARKER is the parenthesised clause a pre-lane `proposed_revision`
# used to write into decision content, still recognised so a legacy entry that already
# carries one is not asked about again (verify_anchors's dedupe guard).
_ANCHOR_WITHDRAWN_CORE = "anchors withdrawn on re-verification:"
_ANCHOR_WITHDRAWN_MARKER = f"({_ANCHOR_WITHDRAWN_CORE}"


class _BudgetExceeded(Exception):
    """Raised by `_call` (inside `verify_anchors`) the instant the per-run git budget is
    spent. Caught only at the entries loop, where it means "stop here" — never let a
    cut-off, still-in-progress classification be interpreted as a real git verdict."""


def _anchor_verify_stamp_path(repo_path: str) -> Path:
    """Same key as every other verification stamp in this codebase: `store.repo_slug`
    applied to the repo_path the session-start caller already resolved, so a writer
    here and the reader on the next session start agree on the file without either
    side re-resolving the repo (see store.verify_scan_conventions's identical
    `_miner_verify_stamp_path`)."""
    return store.STORE_DIR / f".anchor_verify_{store.repo_slug(repo_path)}"


def _run_git(repo_path: str, *args: str) -> str | None:
    """Single git-call chokepoint for anchor verification: every git invocation in this
    module funnels through here, so a test can monkeypatch `anchors._run_git` to count
    calls (fast-path / budget-exhaustion tests) exactly like store's tests monkeypatch
    `miner.mine_conventions`. Fail-soft via `store.run_git` itself: any git failure is
    `None`, never raises."""
    return store.run_git(repo_path, *args, timeout=store.GIT_FAST_TIMEOUT)


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
    caller's budget-tracking wrapper around `_run_git`, which RAISES `_BudgetExceeded`
    once the run's git budget is spent rather than returning a value indistinguishable
    from "git found nothing" — see verify_anchors). Confident requires, at every hop, BOTH
    a single distinct rename target from the commit's name-status AND (once the chain
    stops) that final target actually existing in the working tree right now.

    Follows the rename CHAIN, not just one hop: a file anchored at commit T that was
    renamed a -> b before T and b -> c after T is still `a` in the entry's `source_files`,
    but `a`'s single most-recent-touch commit only ever resolves to `b` — which itself no
    longer exists. So each resolved hop that doesn't exist on disk is fed back in as the
    next `old_path` to resolve, same two calls as before (`log --follow` + `show
    --name-status`, see _parse_rename_target for why `show` carries no pathspec), up to
    `_RENAME_CHAIN_MAX` hops. Ambiguity (not exactly one distinct target) at ANY hop is
    treated exactly like a single-hop ambiguity: not confident, full stop — a chain must
    be an unbroken line of confident hops, not confident-then-guess.

    A chain that never bottoms out in an existing file within the hop cap is NOT
    confident: see _RENAME_CHAIN_MAX for why that's a deliberate "count it as missing"
    choice rather than a further widened search."""
    current = old_path
    for _ in range(_RENAME_CHAIN_MAX):
        commit = call("log", "--follow", "--format=%H", "-1", "--", current)
        if not commit:
            return None
        name_status = call("show", "--format=", "--name-status", commit)
        if not name_status:
            return None
        target = _parse_rename_target(name_status, current)
        if target is None or target == current:
            return None
        if (repo_root / target).exists():
            return target
        current = target  # this hop's target vanished too — chase it one more hop
    return None


def verify_anchors(repo_path: str, force: bool = False) -> dict:
    """Re-checks every anchored, active-status decision's `source_files` against the
    working tree, correcting renames in place and proposing (never applying) retirement
    for total losses. Returns {"reanchored": int, "proposed": int}. Called fail-soft from
    store's session-start path, immediately after `verify_scan_conventions`.

    Participants: `type == "decision"`, non-empty `source_files`, `entry_status` in
    ("approved", "suggested") — the same "active status" set `_rehydrate_working_set`
    uses — and neither a `proposed_revision` nor a `proposed_lifecycle` already pending (an
    entry mid-review is skipped outright, never piled onto with a second proposal; that is
    also what makes retirement-proposal dedupe structural rather than a text comparison).

    Fast path (session-start latency): participants are collected from a single `load`
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
        with store.store_lock(store.repo_slug(repo_path)):
            data = store.load(repo_path)
            entries = data.get("entries") or []
            participants = [
                e for e in entries
                if e.get("type") == "decision"
                and e.get("source_files")
                and store.entry_status(e) in _ACTIVE_STATUSES
                and e.get("proposed_revision") is None
                and e.get("proposed_lifecycle") is None
            ]
            if not participants:
                return {"reanchored": 0, "proposed": 0}

            stamp = _anchor_verify_stamp_path(repo_path)
            if not force:
                mtime = store.file_mtime(stamp)
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
                    raise _BudgetExceeded
                calls_used += 1
                return _run_git(repo_path, *args)

            now = datetime.now(timezone.utc).isoformat()
            reanchored = 0
            proposed = 0
            review_needed = False
            changed = False

            for entry in participants:
                try:
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
                            # rev-parse BEFORE any mutation: if the budget runs out here,
                            # _BudgetExceeded must find this entry still untouched.
                            new_commit = _call("rev-parse", "HEAD")
                            entry["source_files"] = surviving[:store.MAX_SOURCE_FILES]
                            entry["anchor_commit"] = new_commit or entry.get("anchor_commit", "")
                            reanchored += 1
                            changed = True
                        continue

                    if surviving:
                        # Partial loss: address correction, not a review event — refresh
                        # the list to what's actually still reachable (as-is or renamed).
                        # Same pre-mutation call ordering as above.
                        new_commit = _call("rev-parse", "HEAD") if renamed else None
                        entry["source_files"] = surviving[:store.MAX_SOURCE_FILES]
                        if renamed:
                            entry["anchor_commit"] = new_commit or entry.get("anchor_commit", "")
                        reanchored += 1
                        changed = True
                        continue

                    # Total loss: every anchored file is gone, none renamed. Propose,
                    # never apply — the decision keeps rendering exactly as it does now
                    # until a human answers (retire_decision tombstones it with its
                    # history, dismiss_lifecycle keeps it live and lets the next TTL
                    # cycle ask again).
                    if _ANCHOR_WITHDRAWN_MARKER in revisions.current_content(entry):
                        continue  # legacy entry whose content already says this
                    # In-memory attach, not store.propose_lifecycle: this run already holds
                    # the store lock and saves once at the end, and flock is not reentrant
                    # across two opens of the same file in one process.
                    if store.attach_lifecycle_proposal(
                            entry, "retire",
                            f"{_ANCHOR_WITHDRAWN_CORE} {', '.join(missing)} no longer exist",
                            source="scan", now=now) is None:
                        continue   # a developer's own retirement proposal holds the slot
                    proposed += 1
                    changed = True
                    review_needed = True
                except _BudgetExceeded:
                    # This entry (and every entry after it, since the budget stays spent)
                    # is left completely unverified — no proposal, no refresh, no
                    # re-anchor. Whatever entries finished before this one keep their
                    # already-applied result.
                    break

            if changed:
                store.save(repo_path, data)
                if review_needed:
                    store._touch_pending_review(repo_path)  # a retirement now awaits review
            return {"reanchored": reanchored, "proposed": proposed}
    except Exception:
        return {"reanchored": 0, "proposed": 0}
