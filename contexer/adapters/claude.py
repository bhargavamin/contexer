"""Claude Code integration adapter."""
import json
import os
import re
import shutil
import sys
from pathlib import Path

from contexer import config, memory_sync, store
from contexer.adapters.base import (
    _BOOTSTRAP_CMD_MARKER,
    _bootstrap_command_text,
    _filter_groups,
    _hooks_of,
    _in_groups,
    _load,
    _load_safe,
    _save,
)

NAME = "claude"


def _teams_url() -> str:
    """Endpoint for the opt-in native contexer-teams MCP entry (CONTEXER_TEAMS_MCP).

    Prefers the user's configured team endpoint (config.toml) so a dev override like
    `contexer login --endpoint <dev-url>` governs this entry too; falls back to the
    built-in default. Fail-soft: a malformed config.toml must never break install —
    and local-only users (no config, flag unset) never reach this path at all."""
    try:
        configured = config.load_profile().endpoint
    except config.ConfigError:
        configured = None
    return configured or config.default_endpoint()


# Embedded as a trailing shell comment in every hook command we generate, so a hook's
# Contexer identity survives any change to its command text. Lets reinstall/uninstall
# recognize and replace stale hooks (e.g. a dead from-source `uv run --directory`).
_HOOK_SENTINEL = "contexer-managed-hook"

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

    Guarded by _is_sane_repo so a home directory that is itself a git repo (dotfiles
    setups) can never select ~/.claude/settings.json — the GLOBAL config, whose modern
    hooks legitimately contain the legacy markers and would otherwise be stripped.
    The guard lives here, not at call sites, so every future caller inherits it."""
    try:
        if not repo_path or not store._is_sane_repo(repo_path):
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
    """UserPromptSubmit (every prompt): auto-store 'always/never/from now on' directives."""
    try:
        repo = store._resolve_repo(store._hook_cwd_repo(repo_path))
        if not repo:
            return "{}"
        entry_id, content = store.capture_user_constraint(
            repo, store.prompt_from_hook_stdin(raw), store.session_from_hook_stdin(raw))
        if entry_id is None:
            return "{}"
        msg = (f"Auto-stored as constraint: '{content}'. "
               "Acknowledge this briefly to the user — e.g. 'Stored as a constraint in Contexer.'")
        return json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": msg}})
    except Exception:
        return "{}"


def rationale(repo_path: str, raw: str) -> str:
    """UserPromptSubmit (every prompt): inject matching decisions for rationale questions."""
    try:
        repo = store._resolve_repo(store._hook_cwd_repo(repo_path))
        if not repo:
            return "{}"
        ctx = store.get_context_for_prompt(repo, store.prompt_from_hook_stdin(raw))
        if not ctx:
            return "{}"
        return json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": ctx}})
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
        new = team_context.poll_for_injection(store._hook_cwd_repo(repo_path), consumer)
        if not new:
            return "{}"
        lines = ["Team decisions just approved (now in effect):"]
        for d in new:
            type_tag = f" ({d.get('type')})" if d.get("type") else ""
            lines.append(f"- {d.get('content', '')}{type_tag}")
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
    """Import Claude memory-tool facts into the store. Silent, fail-soft, idempotent.

    Skips the whole import when the memory dir is unchanged since last sync
    (content fingerprint in ~/.contexer/.memory_synced_<slug>). Returns the count
    of newly-stored entries (0 on skip/absence/error). Wired to SessionStart,
    PreCompact, and SessionEnd hooks."""
    try:
        repo = store._resolve_repo(store._hook_cwd_repo(repo_path))
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
        marker = store.STORE_DIR / f".memory_synced_{store._slug(repo)}"
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
        "repo=sys.argv[1]; store.STORE_DIR.mkdir(exist_ok=True); "
        # Only record a sane repo — never poison the pointer with a config/home dir.
        "store._is_sane_repo(repo) and (store.STORE_DIR/'.current_repo').write_text(repo); "
        # Import any memory-tool facts before building context (crash-recovery net:
        # catches facts whose session ended without a clean SessionEnd flush).
        "_c.sync_memory(repo); "
        # Refresh team context (Path B, C5) before building the session context —
        # fail-soft so a sync hiccup never breaks the session.
        "_c.pull_team(repo); "
        "print(json.dumps(store.get_session_start_context(repo, store.source_from_hook_stdin(sys.stdin.read()))))"
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
    anchor_cmd = (
        "REPO=$(git rev-parse --show-toplevel 2>/dev/null || true); "
        "if [ -n \"$REPO\" ]; then printf '%s' \"$REPO\" > ~/.contexer/.current_repo; fi; "
        "FLAG=\"$HOME/.contexer/.pending_capture\"; "
        "if [ -f \"$FLAG\" ]; then "
        "rm -f \"$FLAG\"; "
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

    contexer_bin = shutil.which("contexer") or "contexer"

    # MCP server (~/.claude.json)
    claude_json = home / ".claude.json"
    claude = _load(claude_json)
    claude.setdefault("mcpServers", {})["contexer"] = {
        "type": "stdio",
        "command": contexer_bin,
    }
    # Team sync is the Python client path (`contexer login` + pull/share/poll). The native
    # remote-MCP entry (Claude Code's OWN OAuth client) is redundant under that design and
    # would show a failed/unauthenticated server for every user (incl. local-only), so it is
    # registered ONLY when explicitly opted in via CONTEXER_TEAMS_MCP. `uninstall` strips any
    # existing entry, so a plain `contexer reinstall` removes a stale one.
    register_teams = bool(os.environ.get("CONTEXER_TEAMS_MCP"))
    if register_teams:
        claude["mcpServers"]["contexer-teams"] = {"type": "http", "url": _teams_url()}
    else:
        claude["mcpServers"].pop("contexer-teams", None)  # drop a stale opt-in entry on plain install
    _save(claude_json, claude)
    log.append("  ✓ MCP server registered in ~/.claude.json")
    if register_teams:
        log.append(f"  ✓ contexer-teams (remote MCP) registered → {_teams_url()}")

    # Hooks and permissions (~/.claude/settings.json)
    settings_json = home / ".claude" / "settings.json"
    settings = _load(settings_json)
    hooks = settings.setdefault("hooks", {})

    ss = hooks.setdefault("SessionStart", [])
    # Migrate: old SessionStart hook didn't read the session source from stdin, or
    # predates memory-tool sync; replace it so the current ss_code (which calls
    # sync_memory) is installed.
    if _in_groups(ss, "get_session_start_context") and not (
            _in_groups(ss, "source_from_hook_stdin") and _in_groups(ss, "sync_memory")):
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

    # PostToolUse: set the .pending_capture flag after any Write/Edit. This is the single
    # deterministic "files changed this turn" signal. It is consumed by the next
    # UserPromptSubmit (anchor_cmd), which injects the capture reminder at the start of the
    # next prompt - a non-interrupting moment. No Stop hook: end-of-turn prompting added
    # latency + tokens and depended on model behavior for no functional gain (the anchor
    # already delivers the same reminder deterministically).
    put = hooks.setdefault("PostToolUse", [])
    if not _in_groups(put, ".pending_capture"):
        put.append({"matcher": "Write|Edit", "hooks": [{"type": "command",
            "command": f"touch ~/.contexer/.pending_capture && echo '{{}}' # {_HOOK_SENTINEL}"}]})
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
    existing_cmd = cmd_path.read_text() if cmd_path.exists() else ""
    if not existing_cmd or _BOOTSTRAP_CMD_MARKER in existing_cmd:
        cmd_path.parent.mkdir(parents=True, exist_ok=True)
        cmd_path.write_text(_bootstrap_command_text())
        log.append("  ✓ /bootstrap command installed to ~/.claude/commands/")
    else:
        log.append("  ! ~/.claude/commands/bootstrap.md exists and is not Contexer's — left untouched")

    _save(settings_json, settings)
    log.append("  ✓ Hooks and permissions written to ~/.claude/settings.json")

    # Upgrade hygiene: a pre-CLI install wrote hooks into the REPO's .claude/settings.json.
    # Clean the repo we're being run from (sync_memory self-heals every other repo the
    # user opens a session in), and warn about a stale plugin install we cannot edit.
    repo = store._git_root(os.getcwd())
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
                if hooks_file.is_file() and "capture_context" in hooks_file.read_text():
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
            "PostToolUse":      [".pending_capture", "plan approved", _HOOK_SENTINEL],
            "Stop":             [".pending_capture", _HOOK_SENTINEL],
            "PreCompact":       ["compaction starting", _HOOK_SENTINEL],
            "PostCompact":      ["reloaded after compaction", "get_post_compact_context",
                                 "decision(s) available", "uv run --directory", _HOOK_SENTINEL],
            "UserPromptSubmit": [".current_repo", ".pending_capture", "get_bootstrap_context_prompt",
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
    if cmd_path.exists() and _BOOTSTRAP_CMD_MARKER in cmd_path.read_text():
        cmd_path.unlink()
        log.append("  ✓ /bootstrap command removed from ~/.claude/commands/")

    # Also remove legacy pre-CLI hooks from the repo we're being run from (the old
    # from-source installer wrote into <repo>/.claude/settings.json, not the home dir).
    repo = store._git_root(os.getcwd())
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
    teams = _load_safe(home / ".claude.json").get("mcpServers", {}).get("contexer-teams")
    teams_url = teams.get("url") if isinstance(teams, dict) else None
    return [
        "  [claude]",
        f"    MCP server: {'registered → ' + mcp_cmd if mcp else 'NOT registered'}",
        f"    teams (remote): {'registered → ' + teams_url if teams_url else 'NOT registered'}",
        f"    hooks:      {'installed' if hooks_ok else 'missing or partial'}",
    ]


def is_installed(home: Path) -> bool:
    """True when both the MCP server and the core hooks are wired for Claude."""
    mcp, hooks_ok = _mcp_and_hooks_ok(home)
    return bool(mcp) and hooks_ok
