"""Claude Code integration adapter."""
import json
import os
import re
import shutil
import sys
from pathlib import Path

from contexer import evidence, memory_sync, store
from contexer.adapters.base import (
    _BOOTSTRAP_CMD_MARKER,
    _bootstrap_command_text,
    _filter_groups,
    _hooks_of,
    _in_commands,
    _in_groups,
    _load,
    _load_safe,
    _save,
)

NAME = "claude"

# What THIS host's installed hooks can observe (evidence.host_coverage renders it, and only
# ever downgrades it at runtime). Codex reuses `capture_constraint` and `post_write`
# verbatim, so its own map is identical by construction rather than by coincidence.
EVIDENCE_COVERAGE = {
    "user_directives": "captured",              # capture_constraint, UserPromptSubmit
    "file_changes": "captured",                 # post_write, PostToolUse(Write|Edit)
    # No hook on any host hands Contexer the assistant's own response, so a conclusion is
    # only ever what the agent chose to report through `record_agent_conclusion`.
    "assistant_conclusions": "model_reported",
    "test_results": "unavailable",              # reserved kind, no emitter
    "diffs": "unavailable",                     # reserved kind, no emitter
}


# Embedded as a trailing shell comment in every hook command we generate, so a hook's
# Contexer identity survives any change to its command text. Lets reinstall/uninstall
# recognize and replace stale hooks (e.g. a dead from-source `uv run --directory`).
_HOOK_SENTINEL = "contexer-managed-hook"

# For the UserPromptSubmit anchor: the flag-clearing `rm` with stderr silenced is
# unique to that hook's command and absent from the pre-#152 form, so it identifies an
# anchor hook that still writes ~/.contexer unguarded. Also shared with the Codex adapter.
_ANCHOR_GUARD = 'rm -f "$FLAG" 2>/dev/null'

# Fingerprints of hooks the pre-CLI from-source installer (scripts/install.sh, June 2026)
# wrote into a REPO's .claude/settings.json — before hooks went global (be12ecd). Modern
# installs only manage ~/.claude/settings.json, so an upgrade left these behind: the stale
# SessionStart hook runs a dead clone via `uv run --directory` (second, contradictory
# "no context stored yet" startup message next to the real one) and the stale mcp_tool
# hook calls the removed `capture_context` tool ("Unknown tool" error on every prompt).
# Modern hooks are never written to repo-level settings, so these substrings can only
# match hooks we owned; anything else in the file is foreign and must survive.
_LEGACY_REPO_HOOK_MARKERS = [
    "Contexer:",                                   # inline SessionStart/PreCompact/PostCompact echoes
    "get_session_start_context",                   # repo-level SessionStart (dead-clone uv run)
    "capture_context",                             # mcp_tool hook for the removed tool
    "Reminder: if you make a significant decision",  # unconditional every-prompt reminder echo
]


def clean_legacy_repo_settings(repo_path: str) -> bool:
    """Strip legacy Contexer hooks from <repo>/.claude/settings.json. Fail-soft, silent.

    Removes only hook groups that are recognizably ours (a _LEGACY_REPO_HOOK_MARKERS
    match, or an mcp_tool hook targeting the contexer server); foreign hooks and every
    other key in the file are preserved. Writes only when something was removed.
    Returns True when the file was modified.

    Guarded by is_sane_repo so a home directory that is itself a git repo (dotfiles
    setups) can never select ~/.claude/settings.json — the GLOBAL config, whose modern
    hooks legitimately contain the legacy markers and would otherwise be stripped.
    The guard lives here, not at call sites, so every future caller inherits it."""
    try:
        if not repo_path or not store.is_sane_repo(repo_path):
            return False
        path = Path(repo_path) / ".claude" / "settings.json"
        if not path.is_file():
            return False
        settings = _load_safe(path)
        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            return False
        changed = False
        for event in list(hooks):
            before = hooks[event]
            if not isinstance(before, list):
                continue
            after = _filter_groups(before, _LEGACY_REPO_HOOK_MARKERS)
            after = [grp for grp in after if not any(
                isinstance(h, dict) and h.get("type") == "mcp_tool"
                and h.get("server") == "contexer"
                for h in _hooks_of(grp))]
            if after != before:
                changed = True
                if after:
                    hooks[event] = after
                else:
                    hooks.pop(event)
        if changed:
            if not hooks:
                settings.pop("hooks", None)
            _save(path, settings)
        return changed
    except Exception:
        return False


def is_present(home: Path) -> bool:
    # Claude's config may be a directory (~/.claude) or a standalone file (~/.claude.json).
    return (home / ".claude").exists() or (home / ".claude.json").exists()


def format_session_start(payload: dict) -> dict:
    """Neutral payload -> Claude SessionStart hook output. Empty context => status only."""
    if not payload.get("context"):
        return {"systemMessage": payload["status"]} if payload.get("status") else {}
    return {
        "systemMessage": payload["status"],
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": payload["context"],
        },
    }


def format_bootstrap_prompt(payload: dict) -> dict:
    """Neutral payload -> Claude UserPromptSubmit output. Empty context => no-op {}."""
    if not payload.get("context"):
        return {}
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": payload["context"],
        }
    }


def format_post_compact(payload: dict) -> dict:
    """Neutral payload -> Claude PostCompact output (injected via systemMessage)."""
    parts = [p for p in (payload.get("status"), payload.get("context")) if p]
    return {"systemMessage": "\n".join(parts)} if parts else {}


def capture_constraint(repo_path: str, raw: str) -> str:
    """UserPromptSubmit (every prompt): auto-store 'always/never/from now on' directives.
    A deictic directive ('this feature', 'it could be') lands pending_approval, not trusted -
    the ack tells the user to generalize/approve/discard it rather than treating it as stored."""
    try:
        # Verbose resolve: this hook process has no MCP server binding of its own, so it is
        # the capture path most exposed to a wrong repo — exactly the provenance the stamp
        # exists to make visible. `_hook_repo_verbose`, not `resolve_repo_verbose`: the hook
        # always supplies a path (its shell's git root, or cwd), which the plain resolver
        # would label `argument` — the one label the audit reads as a DELIBERATE cross-repo
        # write, dismissing the very misroute this is meant to surface.
        repo, repo_source = store._hook_repo_verbose(repo_path)
        if not repo:
            return "{}"
        near: list = []
        # evidence.capture_directive is store.capture_user_constraint plus the shadow-mode
        # user_directive event: same return, same exceptions (this hook's existing
        # swallow-and-return-"{}" is the outer handler below, unchanged), and Codex reuses
        # this entrypoint verbatim, so the source stays host-neutral.
        entry_id, content, status = evidence.capture_directive(
            repo, store.prompt_from_hook_stdin(raw), store.session_from_hook_stdin(raw),
            "claude_prompt", near=near, repo_source=repo_source)
        if entry_id is None:
            return "{}"
        msg = store.constraint_ack(content, status, entry_id, near)
        return json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": msg}})
    except Exception:
        return "{}"


# The token note is surfaced on exception, not routinely: below this injected-size
# estimate the systemMessage names what was recalled but stays silent about tokens.
_COST_NOTE_TOKENS = 150

# Golden savings multiplier: median without/with session-token ratio over the 10
# recall-event task-cells (rationale + continuity) in campaigns 3/4-sonnet/4-opus/5
# runs.jsonl (median 4.13; developer-ratified 2026-07-16). The notice shows net
# benefit — est × (multiplier − 1) — rounded to the nearest 10. Re-derive on
# re-benchmark.
_SAVED_MULTIPLIER = 4


def rationale(repo_path: str, raw: str) -> str:
    """UserPromptSubmit (every prompt): inject matching decisions for rationale questions.

    Passes the host's session id (Retrieval V1 Part B) so the BM25 router's working set
    can dedup repeat injections within a session; Codex reuses this verbatim."""
    try:
        repo = store.resolve_repo(store.hook_cwd_repo(repo_path))
        if not repo:
            return "{}"
        session_id = store.session_from_hook_stdin(raw)
        ctx, meta = store.get_context_for_prompt_with_meta(
            repo, store.prompt_from_hook_stdin(raw), session_id)
        if not ctx:
            return "{}"
        # systemMessage is user-facing only (the model never sees it): name WHAT was
        # recalled so retrieval is observable, not spooky. Savings are flagged on
        # exception — the benchmark-derived estimate appears only when the injection
        # is big enough to care about.
        # Built from the router's structured meta, not scraped from the rendered text.
        est = max(1, len(ctx) // 4)
        topics = meta.get("topics") or []
        if meta.get("kind") == "pointer":
            msg = "Contexer: pointed at related decisions"
            if topics:
                msg += f" ({', '.join(topics[:3])})"
        else:
            n = meta.get("count", 0)
            noun = "decision" if n == 1 else "decisions"
            msg = f"Contexer: recalled {n} {noun}" if n else "Contexer: injected stored context"
            if n and topics:
                msg += f" ({', '.join(topics[:3])})"
        if est > _COST_NOTE_TOKENS:
            saved = int(round(est * (_SAVED_MULTIPLIER - 1), -1))
            msg += f" · ~{saved} tokens saved"
        return json.dumps({
            "systemMessage": msg,
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit", "additionalContext": ctx}})
    except Exception:
        return "{}"


def post_write(repo_path: str, raw: str) -> str:
    """PostToolUse (Write|Edit): record the file(s) this turn edited into the PER-REPO
    edited-files sidecar (issue #175 Task 2 — a deterministic flow signal capture can later
    propose anchor candidates from) AND arm the .pending_capture flag. Silent, fail-soft,
    never raises; always returns "{}".

    The host session id in `raw` is deliberately NOT used to key that sidecar: this hook is
    a different process from the MCP server that later reads it, and the two never share a
    session id (see store.record_edited_file). `raw` is still parsed for `tool_input`.

    Replaces the old shell-only `touch .pending_capture` PostToolUse hook: this Python
    entrypoint does both jobs so the file-edit signal and the capture-reminder flag stay
    in one hook, exactly as the shelved feat/doc-drift branch shipped it.

    THE HAZARD THIS MUST NOT REPEAT: doc-drift's post_write shell wrapper resolved the repo
    via raw cwd while its sibling UserPromptSubmit hooks resolved it via `git rev-parse
    --show-toplevel` — a mismatch that silently keyed a DIFFERENT sidecar slug (record_
    edited_file wrote under one repo identity, the reader looked under another) and killed
    the feature for any project not opened at its git root. The installed wrapper for THIS
    hook (see install()'s post_write_cmd) copies the exact `REPO=$(git rev-parse
    --show-toplevel 2>/dev/null || true) &&` prefix every other UserPromptSubmit hook here
    uses (cap_con/cap_rat/cap_poll/review_cmd), so record_edited_file's write and Task 3's
    capture-time read key the identical sidecar. store.hook_cwd_repo is still the fallback
    for a non-git project (first-class stores keyed by absolute path), matching every other
    hook-invoked entrypoint in this module.

    Touching ~/.contexer/.pending_capture (via store.STORE_DIR, not a hardcoded home path,
    so tests that redirect STORE_DIR never touch the real store — #152's best-effort
    invariant) preserves the capture-reminder signal the shell hook this replaces used to
    set (consumed by the next UserPromptSubmit anchor)."""
    try:
        repo = store.hook_cwd_repo(repo_path)
        try:
            data = json.loads(raw)
        except Exception:
            data = {}
        tool_input = data.get("tool_input") if isinstance(data, dict) else None
        fp = tool_input.get("file_path") if isinstance(tool_input, dict) else None
        relpath = ""
        if isinstance(fp, str) and fp:
            # Own try/except: this signal must not share failure fate with the
            # .pending_capture arm below — a non-OSError escaping record_edited_file
            # (e.g. from guard_engine) must not also cost the capture reminder.
            try:
                relpath = store.record_edited_file(repo, fp)
            except Exception:
                pass
        try:
            store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
            (store.STORE_DIR / ".pending_capture").touch()
        except OSError:
            pass
        # Shadow-mode evidence, emitted LAST so neither existing signal above can be
        # affected by it, and keyed on the path record_edited_file actually recorded (its
        # return, not the host's raw file_path) so the event and the sidecar agree. The
        # source is host-neutral: Codex runs this same entrypoint. emit_hook_event never
        # raises, and the outer handler returns the identical "{}" if it somehow did.
        if relpath:
            evidence.emit_hook_event(repo, "file_changed",
                                     session_id=store.session_from_hook_stdin(raw),
                                     source="post_tool_use", files=[relpath])
        return "{}"
    except Exception:
        return "{}"


def review_nudge(repo_path: str, raw: str) -> str:
    """UserPromptSubmit (every prompt): if THIS repo has a decision newly pending review AND still
    has pending ones, inject a one-time nudge. store.pending_review_nudge is per-repo and verifies
    the store, so an already-approved or cross-repo flag yields nothing. Codex reuses this verbatim."""
    try:
        nudge = store.pending_review_nudge(store.hook_cwd_repo(repo_path))
        if not nudge:
            return "{}"
        return json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": nudge}})
    except Exception:
        return "{}"


def _retire_capture_task_hook() -> None:
    """Remove the pre-#58 capture-task hook group from ~/.claude/settings.json.

    Fail-soft and write-only-on-change, like clean_legacy_repo_settings."""
    try:
        path = Path.home() / ".claude" / "settings.json"
        settings = _load_safe(path)
        hooks = settings.get("hooks")
        if not isinstance(hooks, dict):
            return
        ups = hooks.get("UserPromptSubmit")
        if not isinstance(ups, list):
            return
        after = _filter_groups(ups, ["claude.capture_task"])
        if after != ups:
            if after:
                hooks["UserPromptSubmit"] = after
            else:
                hooks.pop("UserPromptSubmit", None)
            if not hooks:
                settings.pop("hooks", None)
            _save(path, settings)
    except Exception:
        pass


def capture_task(repo_path: str, raw: str) -> str:
    """Self-retiring no-op stub for the removed "last task" hook entrypoint (#58).

    install() strips the capture-task hook on reinstall, but a package-only upgrade
    leaves the installed hook text calling this function — without the stub that is
    an AttributeError traceback on every prompt (the same failure class as the
    removed capture_context tool, PR #96). Being invoked is proof of a stale hook,
    so the stub removes that hook group before returning the silent no-op: the
    error disappears immediately and the dead per-prompt subprocess stops spawning
    from the next prompt on. Both hosts that ever wired this exact command are
    healed — this module's own settings.json here, Codex's hooks.json via its
    adapter (each module edits only its own file). Args are unused but keep the
    installed hook's call signature."""
    _retire_capture_task_hook()
    try:
        from contexer.adapters import codex
        codex.retire_capture_task(Path.home())
    except Exception:
        pass
    return "{}"


def team_poll(repo_path: str, raw: str, consumer: str = "claude") -> str:
    """UserPromptSubmit (C7): inject team decisions newly approved since the last poll.

    Fail-soft. Uses the non-blocking poll: the network sync runs in a detached background
    process and its results inject on the NEXT prompt, so this hook never waits on the
    cloud — a slow or timing-out endpoint cannot stall prompt submission. `consumer`
    identifies the polling host (defaults to "claude" so the original installed Claude hook
    string keeps working); each consumer gets every newly-synced batch exactly once via its
    own high-water marker, so a Codex session on the same repo never steals Claude's injection
    (or vice versa)."""
    try:
        from contexer import team_context
        new = team_context.poll_for_injection(store.hook_cwd_repo(repo_path), consumer)
        if not new:
            return "{}"
        # Architecture-typed rows are deferred to a count-only pointer here too, mirroring
        # the SessionStart team section (store.session_start_payload) — a freshly-approved
        # architecture decision shouldn't flood the prompt any more than a bulk-loaded one.
        visible = [d for d in new if d.get("type") != "architecture"]
        deferred = [d for d in new if d.get("type") == "architecture"]
        lines = []
        if visible:
            lines.append("Team decisions just approved (now in effect):")
            for d in visible:
                type_tag = f" ({d.get('type')})" if d.get("type") else ""
                lines.append(f"- {d.get('content', '')}{type_tag}")
        if deferred:
            lines.append(
                f"{len(deferred)} team architecture decision(s) just approved but deferred. "
                'Call get_context(entry_type="architecture") for full content.'
            )
        # No `if not lines: return "{}"` guard here: `new` is non-empty (checked above) and
        # every row lands in `visible` or `deferred`, so `lines` is always populated.
        return json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": "\n".join(lines)}})
    except Exception:
        return "{}"


def _memory_dir(repo_path: str) -> Path | None:
    """Locate Claude Code's memory-tool dir for a repo, or None if absent.

    COUPLING POINT: Claude Code encodes a project dir by replacing every
    non-alphanumeric char in the absolute path with '-'
    (``/Users/me/repos/x`` -> ``-Users-me-repos-x``). If that encoding ever
    changes upstream, this silently finds nothing and the whole memory-sync
    feature no-ops — fail-safe (never wrong data), but it can go quietly dead."""
    slug = re.sub(r"[^a-zA-Z0-9]", "-", repo_path)
    d = Path.home() / ".claude" / "projects" / slug / "memory"
    return d if d.is_dir() else None


def sync_memory(repo_path: str) -> int:
    """Import Claude memory-tool facts, then reconcile recorded evidence. Silent, fail-soft.

    Returns the memory-import count, exactly as before. The reconciliation rides along here
    because this is the one entrypoint the installed SessionStart, PreCompact and SessionEnd
    hooks ALL already call - all three evidence checkpoints, with no hook command, settings or
    installer change - and the session-start call doubles as the next-session recovery net for
    a session that ended without one. It runs on every invocation, including the ones where the
    memory fingerprint is unchanged and the import short-circuits: the two have nothing to do
    with each other, and gating one on the other would silence reconciliation on the common
    path. `reconcile_session` never raises and reads no store - let alone takes the store lock -
    until it has unconsumed evidence, so a quiet repo costs two lock-free sidecar reads.
    """
    stored = _import_memory_facts(repo_path)
    _reconcile_evidence(repo_path)
    return stored


def _reconcile_evidence(repo_path: str) -> None:
    """Materialize recorded evidence into decisions pending review. Fail-soft to the point of
    swallowing an import error: this is a passenger on `sync_memory`, and a passenger must
    never cost the session its memory import - let alone its SessionStart output.

    CLAUDE-ONLY, and it stays that way (task-05 review, minor M6, settled by Task 06). Codex
    reuses a lot of this module verbatim - `capture_constraint`, `post_write`, `review_nudge` -
    and an earlier version of this comment claimed it reused this entrypoint too. It never did,
    and it must not: `host=NAME` is a literal, so every Codex receipt would have named
    `claude` as the host that observed the session, in the one block whose whole job is saying
    who observed what. Codex reconciles through the shared store-side session-start path
    (`store.session_start_payload`), which its own installed hook calls with `'codex'`.

    THE SESSION-START CALL IS NOW A DOUBLE, DELIBERATELY. Claude's SessionStart hook runs
    `sync_memory` (this) and then `get_session_start_context`, which reconciles again on the
    shared path. The second call is a no-op by construction rather than by a marker: this one
    has already moved the spool's events into their holds, so `reconcile._has_work` finds
    nothing, returns before the store is read or any lock is taken, and costs two directory
    listings. Measured at 0.04ms. The brief's rule is to keep the duplicate HARMLESS first and
    remove it only against a measured cost, using an explicit hook-event argument rather than
    an external skip marker - and there is no measured cost to remove.
    """
    try:
        from contexer import reconcile
        repo = store.resolve_repo(store.hook_cwd_repo(repo_path))
        if repo:
            reconcile.reconcile_session(repo, host=NAME)
    except Exception:
        pass


def _import_memory_facts(repo_path: str) -> int:
    """Import Claude memory-tool facts into the store. Silent, fail-soft, idempotent.

    Skips the whole import when the memory dir is unchanged since last sync
    (content fingerprint in ~/.contexer/.memory_synced_<slug>). Returns the count
    of newly-stored entries (0 on skip/absence/error)."""
    try:
        repo = store.resolve_repo(store.hook_cwd_repo(repo_path))
        if not repo:
            return 0
        # Self-heal: strip hooks a pre-CLI install left in <repo>/.claude/settings.json
        # (stale second startup message + "Unknown tool: capture_context" every prompt).
        # This lives here — not only in install() — because sync_memory is the one
        # adapter entrypoint every installed SessionStart hook already calls, so a plain
        # package upgrade heals each repo the user opens without requiring a reinstall.
        clean_legacy_repo_settings(repo)
        mem = _memory_dir(repo)
        if mem is None:
            return 0
        fingerprint = memory_sync.dir_fingerprint(mem)
        store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        marker = store.STORE_DIR / f".memory_synced_{store.repo_slug(repo)}"
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == fingerprint:
            return 0
        stored = memory_sync.import_dir(mem, repo)
        marker.write_text(fingerprint, encoding="utf-8")
        return stored
    except Exception:
        return 0


def pull_team(repo_path: str) -> tuple[int, int]:
    """SessionStart: refresh the local team-context cache before building context.

    Delegates to the neutral, fail-soft team_context.refresh() seam (Option A) — a sync
    hiccup (offline, bad token, anything) degrades to a no-op. Returns (upserted, removed).
    Kept as a named entrypoint because installed Claude hooks call `_c.pull_team`.

    The try/except also guards the lazy import itself: a broken/partial install (import
    error in team_context or its deps) must not crash the SessionStart hook."""
    try:
        from contexer import team_context
        return team_context.refresh(repo_path)
    except Exception:
        return (0, 0)


def install(home: Path) -> list[str]:
    """Wire the Claude MCP server + hooks + permissions. Returns log lines."""
    log: list[str] = []
    python = sys.executable

    def _py(code: str) -> str:
        # `|| true` (not `|| pwd`): outside a git work tree REPO is empty, and the
        # entrypoints treat "" as "no repo" (resolve via session binding / pointer).
        # A `pwd` fallback could write a non-repo dir into the shared .current_repo.
        return (
            f'REPO=$(git rev-parse --show-toplevel 2>/dev/null || true) && '
            f'"{python}" -c "{code}" "$REPO" # {_HOOK_SENTINEL}'
        )

    ss_code = (
        "from contexer import store; from contexer.adapters import claude as _c; import json,sys; "
        "repo=sys.argv[1]; raw=sys.stdin.read(); "
        # Only record a sane repo — never poison the pointer with a config/home dir —
        # and never let an unwritable ~/.contexer abort the hook (store.anchor_repo is
        # sanity-checked AND fail-soft; see #152).
        "store.anchor_repo(repo); "
        # Import any memory-tool facts before building context (crash-recovery net:
        # catches facts whose session ended without a clean SessionEnd flush).
        "_c.sync_memory(repo); "
        # Refresh team context (Path B, C5) before building the session context —
        # fail-soft so a sync hiccup never breaks the session.
        "_c.pull_team(repo); "
        # session_id (Retrieval V1 Part B): threaded through for compact-source working-set
        # rehydration; "" on hosts/events that omit it, preserving today's behavior.
        # The trailing 'claude' (Task 06) names the host for the evidence-reconciliation
        # coverage block the shared session-start path now produces. SINGLE-quoted, because
        # `_py` wraps this whole string in double quotes - which is also why the migration
        # gate below matches it with `_in_commands` rather than `_in_groups`.
        "print(json.dumps(store.get_session_start_context(repo, store.source_from_hook_stdin(raw), "
        "store.session_from_hook_stdin(raw), 'claude')))"
    )
    boot_code = (
        "from contexer import store; import json,sys; "
        "result=store.get_bootstrap_context_prompt(sys.argv[1], store.prompt_from_hook_stdin(sys.stdin.read())); "
        "print(json.dumps(result))"
    )

    # Memory-tool sync (SessionEnd + PreCompact). Runs claude.sync_memory then emits
    # `tail` as the hook's stdout JSON. The python call prints nothing — only `tail`
    # reaches stdout, so the hook output stays valid.
    def _sync(tail: str) -> str:
        return (
            'REPO=$(git rev-parse --show-toplevel 2>/dev/null || true) && '
            f'"{python}" -c "from contexer.adapters import claude; import sys; '
            'claude.sync_memory(sys.argv[1])" "$REPO"; '
            f"echo '{tail}' # {_HOOK_SENTINEL}"
        )

    precompact_cmd = _sync(
        '{"systemMessage": "Contexer: context compaction starting '
        '\\u2014 call update_context for any decisions not yet stored. '
        'If update_context appears as a deferred tool, first call: '
        'ToolSearch(query=\'select:mcp__contexer__update_context\')"}')
    sessionend_cmd = _sync("{}")

    # Record the git root in ~/.contexer/.current_repo, but only when we're actually inside
    # a git work tree — the old `|| pwd` fallback could write a non-repo dir (e.g. ~/.claude),
    # poisoning the shared pointer so decisions landed in the wrong store file.
    # Every ~/.contexer write here is best-effort (#152): on a host where the store dir is
    # not writable the redirect/rm would otherwise fail mid-hook and swallow the reminder
    # echo. The braces matter — `cmd > f 2>/dev/null` opens the redirect BEFORE stderr is
    # silenced, so a failed open still leaks its error; `{ cmd > f; } 2>/dev/null` doesn't.
    anchor_cmd = (
        "REPO=$(git rev-parse --show-toplevel 2>/dev/null || true); "
        "if [ -n \"$REPO\" ]; then { printf '%s' \"$REPO\" > ~/.contexer/.current_repo; } "
        "2>/dev/null || true; fi; "
        "FLAG=\"$HOME/.contexer/.pending_capture\"; "
        "if [ -f \"$FLAG\" ]; then "
        "rm -f \"$FLAG\" 2>/dev/null || true; "
        "echo '{\"hookSpecificOutput\": {\"hookEventName\": \"UserPromptSubmit\", "
        "\"additionalContext\": \"Contexer: last turn settled - reconcile decisions before continuing. "
        "(1) NEW decisions that STUCK: call update_context with the full reasoning. "
        "(2) A PROVISIONAL decision from earlier this session (e.g. from an approved plan) that HELD: "
        "approve it; that CHANGED during implementation: call update_context with the new value "
        "(it supersedes the old revision); that was ABANDONED: mark it ignored via approve_decision. "
        "(3) Do NOT store approaches you tried and reverted, and keep your own unratified proposals "
        "provisional (created_by=ai) rather than storing them as settled fact. "
        "The server deduplicates and tracks repetition. "
        "If update_context appears as a deferred tool, first call "
        "ToolSearch(query=select:mcp__contexer__update_context).\"}}'; "
        "else echo '{}'; fi"
        f" # {_HOOK_SENTINEL}"
    )

    # ExitPlanMode: fires PostToolUse when the user approves a plan. Approval continues execution
    # in the SAME flow - there is no fresh UserPromptSubmit to consume a .pending_capture flag - so
    # we inject the capture reminder DIRECTLY here. Plan decisions are captured PROVISIONAL
    # (created_by=plan -> suggested, never authoritative) and reconciled at the next prompt's anchor.
    plan_cmd = (
        "echo '{\"hookSpecificOutput\": {\"hookEventName\": \"PostToolUse\", "
        "\"additionalContext\": \"Contexer: plan approved. Capture each key decision in the plan now "
        "via update_context with created_by=plan - architecture choices, constraints, conventions, "
        "including the REASONING not just the conclusion. They are stored as PROVISIONAL (suggested), "
        "not authoritative, until implementation validates them; you will reconcile them (approve / "
        "update / ignore) at the next prompt. "
        "If update_context appears as a deferred tool, first call "
        "ToolSearch(query=select:mcp__contexer__update_context).\"}}'"
        f" # {_HOOK_SENTINEL}"
    )

    cap_con = ('REPO=$(git rev-parse --show-toplevel 2>/dev/null || true) && '
               f'"{python}" -c "from contexer.adapters import claude; import sys; '
               f'print(claude.capture_constraint(sys.argv[1], sys.stdin.read()))" "$REPO" # {_HOOK_SENTINEL}')
    cap_rat = ('REPO=$(git rev-parse --show-toplevel 2>/dev/null || true) && '
               f'"{python}" -c "from contexer.adapters import claude; import sys; '
               f'print(claude.rationale(sys.argv[1], sys.stdin.read()))" "$REPO" # {_HOOK_SENTINEL}')
    cap_poll = ('REPO=$(git rev-parse --show-toplevel 2>/dev/null || true) && '
                f'"{python}" -c "from contexer.adapters import claude; import sys; '
                f'print(claude.team_poll(sys.argv[1], sys.stdin.read()))" "$REPO" # {_HOOK_SENTINEL}')

    # Nudge to review decisions pending the developer (dropped by store.update_decision). A Python
    # entrypoint (not pure shell) so it is per-repo and can verify the store still has something
    # pending — no false nudge for an already-approved or cross-repo flag.
    review_cmd = ('REPO=$(git rev-parse --show-toplevel 2>/dev/null || true) && '
                  f'"{python}" -c "from contexer.adapters import claude; import sys; '
                  f'print(claude.review_nudge(sys.argv[1], sys.stdin.read()))" "$REPO" # {_HOOK_SENTINEL}')

    # PostToolUse (Write|Edit): records edited files into the per-session sidecar (issue
    # #175 Task 2) AND still arms .pending_capture — replaces the old pure-shell touch.
    # $REPO resolution is copied VERBATIM from the sibling UserPromptSubmit hooks above
    # (cap_con/cap_rat/cap_poll/review_cmd) — see post_write's docstring for why a
    # cwd-vs-toplevel mismatch here would silently kill the feature.
    post_write_cmd = ('REPO=$(git rev-parse --show-toplevel 2>/dev/null || true) && '
                      f'"{python}" -c "from contexer.adapters import claude; import sys; '
                      f'print(claude.post_write(sys.argv[1], sys.stdin.read()))" "$REPO" '
                      f'# {_HOOK_SENTINEL} .pending_capture')

    contexer_bin = shutil.which("contexer") or "contexer"

    # MCP server (~/.claude.json)
    claude_json = home / ".claude.json"
    claude = _load(claude_json)
    claude.setdefault("mcpServers", {})["contexer"] = {
        "type": "stdio",
        "command": contexer_bin,
    }
    # Legacy cleanup: older builds registered a native `contexer-teams` remote-MCP entry
    # (it authenticated Claude Code's OWN MCP client, had no push/pull, and showed a failed
    # server for every user). Team sync is the Python client path now (`contexer login` +
    # pull/share/poll), so any leftover entry is stripped on every install. Idempotent.
    claude["mcpServers"].pop("contexer-teams", None)
    _save(claude_json, claude)
    log.append("  ✓ MCP server registered in ~/.claude.json")

    # Hooks and permissions (~/.claude/settings.json)
    settings_json = home / ".claude" / "settings.json"
    settings = _load(settings_json)
    hooks = settings.setdefault("hooks", {})

    ss = hooks.setdefault("SessionStart", [])
    # Migrate: old SessionStart hook didn't read the session source from stdin, predates
    # memory-tool sync, predates session-id threading (compact-source working-set
    # rehydration), predates the fail-soft repo anchor (#152: an unwritable
    # ~/.contexer aborted the hook, injecting nothing), or predates the host argument
    # (Task 06, without which this host's reconciliation receipts report `manual`);
    # replace it so the current ss_code is installed.
    #
    # `_in_commands` for the host marker and nowhere else: it carries a quote, and `_in_groups`
    # matches a dict REPR, where the surrounding double-quoted command forces `'claude'` to be
    # re-escaped and the marker never matches (the live bug `base.py` documents).
    if _in_groups(ss, "get_session_start_context") and not (
            _in_groups(ss, "source_from_hook_stdin") and _in_groups(ss, "sync_memory")
            and _in_groups(ss, "session_from_hook_stdin")
            and _in_groups(ss, "anchor_repo") and _in_commands(ss, "'claude'")):
        ss = _filter_groups(ss, ["get_session_start_context"])
        hooks["SessionStart"] = ss
    if not _in_groups(ss, "get_session_start_context"):
        ss.insert(0, {"hooks": [{"type": "command",
            "statusMessage": "Loading session context...",
            "command": _py(ss_code)}]})

    # SessionEnd: flush memory-tool facts on clean exit (deterministic — needs no model).
    se = hooks.setdefault("SessionEnd", [])
    if not _in_groups(se, "sync_memory"):
        se.append({"hooks": [{"type": "command",
            "statusMessage": "Syncing memory to Contexer...", "command": sessionend_cmd}]})

    # PostToolUse: claude.post_write records edited files into the per-session sidecar
    # (issue #175 Task 2) AND still arms the .pending_capture flag. The flag is consumed by
    # the next UserPromptSubmit (anchor_cmd), which injects the capture reminder at the
    # start of the next prompt - a non-interrupting moment. No Stop hook: end-of-turn
    # prompting added latency + tokens and depended on model behavior for no functional
    # gain (the anchor already delivers the same reminder deterministically).
    put = hooks.setdefault("PostToolUse", [])
    # Migrate: replace the old shell-only `.pending_capture` touch (pre- or post-#152) with
    # the Python post_write hook — it records edited files AND still touches
    # .pending_capture. Detected by the `.pending_capture` marker without `claude.post_write`
    # (which the migrated hook also carries via its trailing comment token), so this is a
    # one-time swap and idempotent thereafter.
    if _in_groups(put, ".pending_capture") and not _in_groups(put, "claude.post_write"):
        put = _filter_groups(put, [".pending_capture"])
        hooks["PostToolUse"] = put
    # Migrate: an installed post_write hook that resolves the repo from raw process cwd (no
    # $REPO threading via `git rev-parse --show-toplevel`) diverges from record_edited_file's
    # reader whenever cwd is a monorepo subdirectory — see post_write's docstring for the
    # doc-drift hazard this guards against. Detected by the absence of "show-toplevel"
    # alongside "claude.post_write".
    if _in_groups(put, "claude.post_write") and not _in_groups(put, "show-toplevel"):
        put = _filter_groups(put, ["claude.post_write"])
        hooks["PostToolUse"] = put
    if not _in_groups(put, "claude.post_write"):
        put.append({"matcher": "Write|Edit", "hooks": [{"type": "command",
            "command": post_write_cmd}]})
    # Plan-approval capture: separate matcher on ExitPlanMode, injects the reminder directly.
    if not _in_groups(put, "plan approved"):
        put.append({"matcher": "ExitPlanMode", "hooks": [{"type": "command",
            "command": plan_cmd}]})

    # Retire any previously-installed Stop hook: end-of-turn prompting is replaced by the
    # deterministic PostToolUse flag + next-prompt anchor reminder. The Stop entry remains
    # in the uninstall marker table so reinstall strips an old Stop hook from settings.json.
    st = hooks.get("Stop", [])
    new_st = _filter_groups(st, [".pending_capture", _HOOK_SENTINEL])
    if new_st != st:
        if new_st:
            hooks["Stop"] = new_st
        else:
            hooks.pop("Stop", None)

    pc = hooks.setdefault("PreCompact", [])
    # Migrate: old PreCompact only echoed a reminder; replace with the sync variant
    # that flushes memory-tool facts before the context window collapses.
    if _in_groups(pc, "compaction starting") and not _in_groups(pc, "sync_memory"):
        pc = _filter_groups(pc, ["compaction starting"])
        hooks["PreCompact"] = pc
    if not _in_groups(pc, "compaction starting"):
        pc.append({"hooks": [{"type": "command",
            "statusMessage": "Saving decisions before compact...",
            "command": precompact_cmd}]})

    # PostCompact is intentionally NOT wired. A PostCompact hook cannot inject into
    # Claude's context — the event supports no `additionalContext`, and its `systemMessage`
    # is user-facing only (the model never sees it). The old Contexer PostCompact hook
    # therefore did no real work: it dumped the full stored context into a visible
    # systemMessage on every /compact — pure transcript noise — while reloading nothing.
    # SessionStart fires again with source="compact" after compaction and silently
    # re-injects via additionalContext (session_start_payload's normal path), so that
    # event already owns post-compaction reload. Strip any previously-installed Contexer
    # PostCompact hook so an upgrade goes quiet; leave foreign PostCompact hooks intact.
    poc = hooks.get("PostCompact", [])
    new_poc = _filter_groups(poc, [
        "get_post_compact_context", "reloaded after compaction",
        "decision(s) available", "uv run --directory", _HOOK_SENTINEL])
    if new_poc != poc:
        if new_poc:
            hooks["PostCompact"] = new_poc
        else:
            hooks.pop("PostCompact", None)

    ups = hooks.setdefault("UserPromptSubmit", [])

    # Replace old anchor hook (without .pending_capture logic) with new one
    if _in_groups(ups, ".current_repo") and not _in_groups(ups, ".pending_capture"):
        ups = _filter_groups(ups, [".current_repo"])
        hooks["UserPromptSubmit"] = ups

    # Migrate: replace the old capture-only anchor text with the reconciliation-framed one
    # (settle checkpoint: promote / revise / drop provisional decisions).
    if _in_groups(ups, "you wrote or edited files") and not _in_groups(ups, "last turn settled"):
        ups = _filter_groups(ups, [".pending_capture"])
        hooks["UserPromptSubmit"] = ups

    # Migrate: an anchor hook predating #152 writes ~/.contexer unguarded, so on a host
    # where that dir is not writable the redirect fails noisily and the flag is never
    # cleared. Replace it with the fail-soft form.
    # _in_commands for the guard: _ANCHOR_GUARD contains a quote, and the rule is
    # "quoted marker -> match the raw command", not "reason about repr per marker".
    if _in_groups(ups, ".pending_capture") and not _in_commands(ups, _ANCHOR_GUARD):
        ups = _filter_groups(ups, [".pending_capture"])
        hooks["UserPromptSubmit"] = ups

    if not _in_groups(ups, ".pending_capture"):
        ups.insert(0, {"hooks": [{"type": "command",
            "statusMessage": "Anchoring repo context...",
            "command": anchor_cmd}]})

    # Migrate: old capture hooks used the mcp_tool type; replace with command hooks
    if any(h.get("type") == "mcp_tool" and h.get("server") == "contexer"
           for grp in ups for h in (grp.get("hooks", []) if isinstance(grp, dict) else [])):
        ups = [grp for grp in ups if not any(
            h.get("type") == "mcp_tool" and h.get("server") == "contexer"
            for h in (grp.get("hooks", []) if isinstance(grp, dict) else []))]
        hooks["UserPromptSubmit"] = ups

    # Migrate: old bootstrap hook didn't read the prompt from stdin; replace it
    if _in_groups(ups, "get_bootstrap_context_prompt") and not _in_groups(ups, "prompt_from_hook_stdin"):
        ups = _filter_groups(ups, ["get_bootstrap_context_prompt"])
        hooks["UserPromptSubmit"] = ups

    if not _in_groups(ups, "get_bootstrap_context_prompt"):
        ups.append({"hooks": [{"type": "command", "once": True,
            "statusMessage": "Checking bootstrap context...",
            "command": _py(boot_code)}]})

    # Retire any previously-installed task-capture hook (the feature was removed).
    if _in_groups(ups, "claude.capture_task"):
        ups = _filter_groups(ups, ["claude.capture_task"])
        hooks["UserPromptSubmit"] = ups

    # Retire the legacy unconditional reminder echo (pre-CLI installs): the
    # .pending_capture anchor delivers the same reminder deterministically and only
    # when files actually changed, so the every-prompt echo is pure duplicate context.
    if _in_groups(ups, "Reminder: if you make a significant decision"):
        ups = _filter_groups(ups, ["Reminder: if you make a significant decision"])
        hooks["UserPromptSubmit"] = ups

    if not _in_groups(ups, "claude.capture_constraint"):
        ups.append({"hooks": [{"type": "command",
            "statusMessage": "Checking for constraint directives...", "command": cap_con}]})
    if not _in_groups(ups, "claude.rationale"):
        ups.append({"hooks": [{"type": "command",
            "statusMessage": "Checking for relevant decisions...", "command": cap_rat}]})
    if not _in_groups(ups, "claude.team_poll"):
        ups.append({"hooks": [{"type": "command",
            "statusMessage": "Checking for new team decisions...", "command": cap_poll}]})
    if not _in_groups(ups, "claude.review_nudge"):
        ups.append({"hooks": [{"type": "command",
            "statusMessage": "Checking for decisions pending review...", "command": review_cmd}]})

    allow = settings.setdefault("permissions", {}).setdefault("allow", [])
    for p in [
        "mcp__contexer__update_context",
        "mcp__contexer__get_context", "mcp__contexer__bootstrap_context",
        "mcp__contexer__get_context_for_prompt",
        "mcp__contexer__update_global_context", "mcp__contexer__get_global_context",
        "mcp__contexer__capture_user_constraint",
    ]:
        if p not in allow:
            allow.append(p)
    # Prune allow entries for tools that no longer exist (capture_context was removed
    # with the "last task" feature) — harmless but confusing when users audit settings.
    for stale in ("mcp__contexer__capture_context",):
        while stale in allow:
            allow.remove(stale)

    # Global /bootstrap command (~/.claude/commands/bootstrap.md) — a project-level
    # command file only works inside that repo, so the command ships in the package
    # and installs globally. Never clobber a bootstrap.md we don't own.
    cmd_path = home / ".claude" / "commands" / "bootstrap.md"
    try:
        existing_cmd = cmd_path.read_text(encoding="utf-8") if cmd_path.exists() else ""
        ours = not existing_cmd or _BOOTSTRAP_CMD_MARKER in existing_cmd
    except (OSError, UnicodeDecodeError):
        # Unreadable or not UTF-8 (a hand-authored file in a legacy encoding) → treat as
        # a file we do NOT own: leave it alone, and do not let it abort install here.
        # claude.json is already saved above and settings.json is saved below, so raising
        # between them would leave the MCP server registered with zero hooks — the same
        # tolerance base._load_safe and base._is_corrupt already apply to host configs.
        ours = False
    if ours:
        cmd_path.parent.mkdir(parents=True, exist_ok=True)
        cmd_path.write_text(_bootstrap_command_text(), encoding="utf-8")
        log.append("  ✓ /bootstrap command installed to ~/.claude/commands/")
    else:
        log.append("  ! ~/.claude/commands/bootstrap.md exists and is not Contexer's — left untouched")

    _save(settings_json, settings)
    log.append("  ✓ Hooks and permissions written to ~/.claude/settings.json")

    # Upgrade hygiene: a pre-CLI install wrote hooks into the REPO's .claude/settings.json.
    # Clean the repo we're being run from (sync_memory self-heals every other repo the
    # user opens a session in), and warn about a stale plugin install we cannot edit.
    repo = store.git_root(os.getcwd())
    if repo and clean_legacy_repo_settings(repo):
        log.append(f"  ✓ Removed legacy Contexer hooks from {repo}/.claude/settings.json")
    plugin_warning = _stale_plugin_warning(home)
    if plugin_warning:
        log.append(plugin_warning)
    return log


def _stale_plugin_warning(home: Path) -> str | None:
    """Warn when an installed Contexer plugin still ships the removed capture hook.

    Plugin caches belong to Claude Code — `contexer install` must not edit them — so
    the only lever is telling the user to update/remove the plugin. Fail-soft: any
    parse problem reads as "no warning"."""
    try:
        reg = _load_safe(home / ".claude" / "plugins" / "installed_plugins.json")
        plugins = reg.get("plugins")
        if not isinstance(plugins, dict):
            return None
        for name, installs in plugins.items():
            if not str(name).startswith("contexer@"):
                continue
            for inst in installs if isinstance(installs, list) else []:
                if not isinstance(inst, dict):
                    continue
                hooks_file = Path(inst.get("installPath", "")) / "hooks" / "hooks.json"
                if hooks_file.is_file() and "capture_context" in hooks_file.read_text(encoding="utf-8"):
                    return ("  ! Outdated Contexer plugin detected (calls the removed "
                            "capture_context tool). Run `claude plugin update contexer` "
                            "or uninstall the plugin — its hooks fire in addition to "
                            "the ones installed here.")
    except Exception:
        return None
    return None


def uninstall(home: Path) -> list[str]:
    """Remove the Claude MCP server + hooks + permissions + /bootstrap command.
    Does NOT touch the store (that's the CLI's --purge concern). Returns log lines."""
    log: list[str] = []

    claude_json = home / ".claude.json"
    if claude_json.exists():
        claude = _load(claude_json)
        removed = claude.get("mcpServers", {}).pop("contexer", None)
        removed_teams = claude.get("mcpServers", {}).pop("contexer-teams", None)
        if removed or removed_teams:
            _save(claude_json, claude)
            log.append("  ✓ MCP server removed from ~/.claude.json")
        else:
            log.append("  - No MCP server entry found in ~/.claude.json")

    settings_json = home / ".claude" / "settings.json"
    if settings_json.exists():
        settings = _load(settings_json)
        hooks = settings.get("hooks", {})
        changed = False

        # _HOOK_SENTINEL is a catch-all: every command we generate carries it, so even a
        # hook whose text changed across versions is recognized and removed. The explicit
        # markers also remove pre-sentinel installs (including the dead from-source
        # `uv run --directory` / "decision(s) available" PostCompact count-pointer).
        event_markers = {
            "SessionStart":     ["get_session_start_context", _HOOK_SENTINEL],
            "SessionEnd":       ["sync_memory", _HOOK_SENTINEL],
            "PostToolUse":      [".pending_capture", "claude.post_write", "plan approved", _HOOK_SENTINEL],
            "Stop":             [".pending_capture", _HOOK_SENTINEL],
            "PreCompact":       ["compaction starting", _HOOK_SENTINEL],
            "PostCompact":      ["reloaded after compaction", "get_post_compact_context",
                                 "decision(s) available", "uv run --directory", _HOOK_SENTINEL],
            "UserPromptSubmit": [".current_repo", ".pending_capture", "claude.review_nudge",
                                 "get_bootstrap_context_prompt",
                                 "claude.capture_task", "claude.capture_constraint", "claude.rationale",
                                 "Reminder: if you make a significant decision",
                                 _HOOK_SENTINEL],
        }
        for event, markers in event_markers.items():
            before = hooks.get(event, [])
            after = _filter_groups(before, markers)
            if event == "UserPromptSubmit":
                # Also strip any legacy mcp_tool capture hooks (pre-migration installs).
                after = [
                    grp for grp in after
                    if not any(
                        h.get("type") == "mcp_tool" and h.get("server") == "contexer"
                        for h in (grp.get("hooks", []) if isinstance(grp, dict) else [])
                    )
                ]
            if after != before:
                changed = True
                if after:
                    hooks[event] = after
                else:
                    hooks.pop(event, None)

        allow = settings.get("permissions", {}).get("allow", [])
        cleaned = [p for p in allow if "contexer" not in p]
        if cleaned != allow:
            settings["permissions"]["allow"] = cleaned
            changed = True

        if changed:
            _save(settings_json, settings)
            log.append("  ✓ Hooks and permissions removed from ~/.claude/settings.json")
        else:
            log.append("  - No Contexer hooks found in ~/.claude/settings.json")

    cmd_path = home / ".claude" / "commands" / "bootstrap.md"
    try:
        ours = cmd_path.exists() and _BOOTSTRAP_CMD_MARKER in cmd_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        ours = False      # not ours if we cannot read it — and uninstall must still finish
    if ours:
        cmd_path.unlink()
        log.append("  ✓ /bootstrap command removed from ~/.claude/commands/")

    # Also remove legacy pre-CLI hooks from the repo we're being run from (the old
    # from-source installer wrote into <repo>/.claude/settings.json, not the home dir).
    repo = store.git_root(os.getcwd())
    if repo and clean_legacy_repo_settings(repo):
        log.append(f"  ✓ Removed legacy Contexer hooks from {repo}/.claude/settings.json")

    return log


def _mcp_and_hooks_ok(home: Path) -> tuple:
    """Read the Claude config (tolerant of corruption — this feeds diagnostics that
    must survive any state) and report (mcp_entry, hooks_ok). Shared by status_lines
    and is_installed."""
    mcp = _load_safe(home / ".claude.json").get("mcpServers", {}).get("contexer")
    raw_hooks = _load_safe(home / ".claude" / "settings.json").get("hooks", {})
    hooks = raw_hooks if isinstance(raw_hooks, dict) else {}

    def _groups(event: str) -> list:
        v = hooks.get(event, [])
        return v if isinstance(v, list) else []

    hooks_ok = (_in_groups(_groups("SessionStart"), "get_session_start_context")
                and _in_groups(_groups("UserPromptSubmit"), "claude.rationale"))
    return mcp, hooks_ok


def status_lines(home: Path) -> list[str]:
    """Diagnostic lines for `contexer status`: MCP/hooks state for the Claude target."""
    mcp, hooks_ok = _mcp_and_hooks_ok(home)
    mcp_cmd = mcp.get("command", "?") if isinstance(mcp, dict) else "?"
    return [
        "  [claude]",
        f"    MCP server: {'registered → ' + mcp_cmd if mcp else 'NOT registered'}",
        f"    hooks:      {'installed' if hooks_ok else 'missing or partial'}",
    ]


def is_installed(home: Path) -> bool:
    """True when both the MCP server and the core hooks are wired for Claude."""
    mcp, hooks_ok = _mcp_and_hooks_ok(home)
    return bool(mcp) and hooks_ok
