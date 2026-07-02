"""Claude Code integration adapter."""
import json
import os
import re
import shutil
import sys
from pathlib import Path

from contexer import memory_sync, store
from contexer.adapters.base import (
    _BOOTSTRAP_CMD_MARKER,
    _bootstrap_command_text,
    _filter_groups,
    _in_groups,
    _load,
    _load_safe,
    _save,
    _strip_stale,
)

NAME = "claude"

# Remote teams MCP endpoint. Prod HTTPS by default; localhost only via the explicit
# CONTEXER_ENV=local developer opt-in (never registered for a normal user).
CONTEXER_TEAMS_PROD = "https://dev.contexer.ai/mcp"
CONTEXER_TEAMS_LOCAL = "http://localhost:8080/mcp"


def _teams_url() -> str:
    return CONTEXER_TEAMS_LOCAL if os.environ.get("CONTEXER_ENV") == "local" else CONTEXER_TEAMS_PROD


# Embedded as a trailing shell comment in every hook command we generate, so a hook's
# Contexer identity survives any change to its command text. Lets reinstall/uninstall
# recognize and replace stale hooks (e.g. a dead from-source `uv run --directory`).
_HOOK_SENTINEL = "contexer-managed-hook"


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
        repo = store._resolve_repo(repo_path)
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
        repo = store._resolve_repo(repo_path)
        if not repo:
            return "{}"
        ctx = store.get_context_for_prompt(repo, store.prompt_from_hook_stdin(raw))
        if not ctx:
            return "{}"
        return json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit", "additionalContext": ctx}})
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
        repo = store._resolve_repo(repo_path)
        if not repo:
            return 0
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
        "print(json.dumps(store.get_session_start_context(repo, store.source_from_hook_stdin(sys.stdin.read()))))"
    )
    boot_code = (
        "from contexer import store; import json,sys; "
        "result=store.get_bootstrap_context_prompt(sys.argv[1], store.prompt_from_hook_stdin(sys.stdin.read())); "
        "print(json.dumps(result))"
    )
    post_code = (
        "from contexer import store; import json,sys; "
        "print(json.dumps(store.get_post_compact_context(sys.argv[1])))"
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
        "\"additionalContext\": \"Contexer: you wrote or edited files last turn "
        "— call update_context for: (1) any NEW architecture/pattern/constraint/convention decisions; "
        "(2) any EXISTING approach you applied again (same or similar content is fine — "
        "the server deduplicates and tracks repetition without storing a duplicate). "
        "If update_context appears as a deferred tool, first call: "
        "ToolSearch(query='select:mcp__contexer__update_context')\"}}'; "
        "else echo '{}'; fi"
        f" # {_HOOK_SENTINEL}"
    )

    cap_con = ('REPO=$(git rev-parse --show-toplevel 2>/dev/null || true) && '
               f'"{python}" -c "from contexer.adapters import claude; import sys; '
               f'print(claude.capture_constraint(sys.argv[1], sys.stdin.read()))" "$REPO" # {_HOOK_SENTINEL}')
    cap_rat = ('REPO=$(git rev-parse --show-toplevel 2>/dev/null || true) && '
               f'"{python}" -c "from contexer.adapters import claude; import sys; '
               f'print(claude.rationale(sys.argv[1], sys.stdin.read()))" "$REPO" # {_HOOK_SENTINEL}')

    contexer_bin = shutil.which("contexer") or "contexer"

    # MCP server (~/.claude.json)
    claude_json = home / ".claude.json"
    claude = _load(claude_json)
    claude.setdefault("mcpServers", {})["contexer"] = {
        "type": "stdio",
        "command": contexer_bin,
    }
    # Remote teams MCP server (additive; leaves the local stdio entry above intact).
    # {type:http,url} is the shape that triggers Claude Code's native OAuth on first
    # use (401 → DCR → browser PKCE → token). No token is written here.
    teams_url = _teams_url()
    claude["mcpServers"]["contexer-teams"] = {"type": "http", "url": teams_url}
    _save(claude_json, claude)
    log.append("  ✓ MCP server registered in ~/.claude.json")
    log.append(f"  ✓ contexer-teams (remote) registered → {teams_url}")

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

    poc = hooks.setdefault("PostCompact", [])
    # Heal stale PostCompact hooks. A from-source install wrote a `uv run --directory
    # <clone>` count-pointer whose path dies the instant the clone moves; older installs
    # used get_context / a "reloaded after compaction" echo. The previous migration only
    # matched one of those phrasings, so the dead from-source hook survived reinstall and
    # ran alongside the new one. Strip any PostCompact hook that isn't the current command
    # (matched by identity marker or the sentinel), then add the current one if absent.
    desired_poc = _py(post_code)
    poc = _strip_stale(poc, [
        "get_post_compact_context", "reloaded after compaction",
        "decision(s) available", _HOOK_SENTINEL], desired_poc)
    hooks["PostCompact"] = poc
    if not _in_groups(poc, "get_post_compact_context"):
        poc.append({"hooks": [{"type": "command",
            "statusMessage": "Reloading context after compact...",
            "command": desired_poc}]})

    ups = hooks.setdefault("UserPromptSubmit", [])

    # Replace old anchor hook (without .pending_capture logic) with new one
    if _in_groups(ups, ".current_repo") and not _in_groups(ups, ".pending_capture"):
        ups = _filter_groups(ups, [".current_repo"])
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

    if not _in_groups(ups, "claude.capture_constraint"):
        ups.append({"hooks": [{"type": "command",
            "statusMessage": "Checking for constraint directives...", "command": cap_con}]})
    if not _in_groups(ups, "claude.rationale"):
        ups.append({"hooks": [{"type": "command",
            "statusMessage": "Checking for relevant decisions...", "command": cap_rat}]})

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
    return log


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
            "PostToolUse":      [".pending_capture", _HOOK_SENTINEL],
            "Stop":             [".pending_capture", _HOOK_SENTINEL],
            "PreCompact":       ["compaction starting", _HOOK_SENTINEL],
            "PostCompact":      ["reloaded after compaction", "get_post_compact_context",
                                 "decision(s) available", "uv run --directory", _HOOK_SENTINEL],
            "UserPromptSubmit": [".current_repo", ".pending_capture", "get_bootstrap_context_prompt",
                                 "claude.capture_task", "claude.capture_constraint", "claude.rationale",
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
