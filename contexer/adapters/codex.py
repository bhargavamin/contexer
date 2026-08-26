"""OpenAI Codex CLI integration adapter.

Codex's hook system is JSON and uses the *same schema and event names* as Claude Code
(SessionStart / PostToolUse / PreCompact / PostCompact / UserPromptSubmit, with the same
hookSpecificOutput / additionalContext / systemMessage output). So this adapter reaches
near-full Claude parity and reuses Claude's runtime entrypoints verbatim — the hook command
strings call store.get_*/claude.capture_* directly, exactly as the Claude hooks do.

Two things differ from the Claude adapter:
  - the MCP server is registered in ~/.codex/config.toml (TOML), not JSON;
  - hooks live in a separate ~/.codex/hooks.json (JSON, same shape as Claude's settings.json
    `hooks` block).
There is no permissions.allow — Codex approves MCP tools interactively (like Cursor).
"""
import shutil
import sys
import tomllib
from pathlib import Path

from contexer.adapters import base
from contexer.adapters import claude
from contexer import store as _store   # module object, not a `from`-import: a value
                                       # patched on contexer.store must resolve at
                                       # CALL time (CLAUDE.md, module boundaries).

NAME = "codex"

# Codex runs claude.capture_constraint and claude.post_write verbatim, so this map must equal
# claude's. Restated rather than aliased (CLAUDE.md module boundaries: no module copies another
# module's names onto itself) and pinned equal by a parity test, the same shape
# `policy.select_policies` and `guard_engine._guard_trusted` already carry.
EVIDENCE_COVERAGE = {
    "user_directives": "captured",              # claude.capture_constraint, UserPromptSubmit
    "file_changes": "captured",                 # claude.post_write, PostToolUse(Write|Edit)
    "assistant_conclusions": "model_reported",  # the MCP tool, agent-invoked
    "test_results": "unavailable",
    "diffs": "unavailable",
}


def is_present(home: Path) -> bool:
    return (home / ".codex").exists()


# ── TOML config.toml helpers (surgical: touch only our [mcp_servers.contexer] stanza) ──────

def _stanza_bounds(lines: list[str]) -> tuple[int, int] | None:
    """Line range [start, end) of the [mcp_servers.contexer] table (incl. any sub-tables),
    or None if absent. The block ends at the next table header that is not ours."""
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "[mcp_servers.contexer]":
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        s = lines[j].lstrip()
        if s.startswith("[") and not s.startswith("[mcp_servers.contexer.") \
                and not s.startswith("[mcp_servers.contexer]"):
            end = j
            break
    return start, end


def _set_contexer_stanza(text: str, cmd: str) -> str:
    """Insert or replace just the [mcp_servers.contexer] stanza, leaving the rest of the
    config byte-for-byte intact."""
    escaped = cmd.replace("\\", "\\\\").replace('"', '\\"')
    stanza = f'[mcp_servers.contexer]\ncommand = "{escaped}"\n'
    lines = text.splitlines(keepends=True)
    bounds = _stanza_bounds(lines)
    if bounds:
        start, end = bounds
        return "".join(lines[:start] + [stanza] + lines[end:])
    body = text.rstrip("\n")
    return stanza if not body else body + "\n\n" + stanza


def _remove_contexer_stanza(text: str) -> str:
    """Drop the [mcp_servers.contexer] stanza; return text unchanged if it isn't present."""
    lines = text.splitlines(keepends=True)
    bounds = _stanza_bounds(lines)
    if not bounds:
        return text
    start, end = bounds
    result = "".join(lines[:start] + lines[end:]).rstrip("\n")
    return result + "\n" if result else ""


def _read_config_command(home: Path):
    """The registered contexer MCP command from config.toml, or None. Tolerant — a missing
    or unparseable config reads as 'not registered' rather than crashing diagnostics."""
    path = home / ".codex" / "config.toml"
    if not path.exists():
        return None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, OSError, UnicodeDecodeError):
        return None
    servers = data.get("mcp_servers", {})
    entry = servers.get("contexer") if isinstance(servers, dict) else None
    return entry.get("command") if isinstance(entry, dict) else None


# ── install / uninstall / status ───────────────────────────────────────────────────────────

def retire_capture_task(home: Path) -> None:
    """Remove the pre-#58 capture-task hook group from ~/.codex/hooks.json.

    Called by the claude.capture_task stub: Codex wired the very same
    `claude.capture_task` command string, so the stub firing may mean the stale
    hook lives in either host's config. install() also strips it on reinstall;
    this covers the package-only upgrade path. Fail-soft, writes only on change."""
    try:
        path = home / ".codex" / "hooks.json"
        cfg = base._load_safe(path)
        hooks = cfg.get("hooks")
        if not isinstance(hooks, dict):
            return
        ups = hooks.get("UserPromptSubmit")
        if not isinstance(ups, list):
            return
        after = base._filter_groups(ups, ["claude.capture_task"])
        if after != ups:
            if after:
                hooks["UserPromptSubmit"] = after
            else:
                hooks.pop("UserPromptSubmit", None)
            if not hooks:
                cfg.pop("hooks", None)
            base._save(path, cfg)
    except Exception:
        pass


def install(home: Path) -> list[str]:
    """Wire the Codex MCP server (config.toml) + hooks (hooks.json). Returns log lines."""
    log: list[str] = []
    python = sys.executable
    contexer_bin = shutil.which("contexer") or "contexer"

    def _py(code: str) -> str:
        return (
            f'REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd) && '
            f'"{python}" -c "{code}" "$REPO"'
        )

    ss_code = (
        "from contexer import store; from contexer.adapters import claude as _c; import json,sys; "
        "repo=sys.argv[1]; raw=sys.stdin.read(); "
        # Sanity-checked AND fail-soft (#152): under Codex's managed sandbox the workspace
        # is writable but ~/.contexer may not be, and the old unguarded write raised
        # PermissionError — aborting SessionStart over a pointer file it did not need.
        "store.anchor_repo(repo); "
        # Refresh team context (Path B seam) before building context — fail-soft, so a sync
        # hiccup never breaks session start. session_start_payload then renders it (T1).
        "_c.pull_team(repo); "
        # session_id (Retrieval V1 Part B): threaded through for compact-source working-set
        # rehydration, mirroring claude.py's ss_code.
        "print(json.dumps(store.get_session_start_context(repo, store.source_from_hook_stdin(raw), "
        "store.session_from_hook_stdin(raw))))"
    )
    boot_code = (
        "from contexer import store; import json,sys; "
        "result=store.get_bootstrap_context_prompt(sys.argv[1], store.prompt_from_hook_stdin(sys.stdin.read())); "
        "print(json.dumps(result))"
    )
    post_code = (
        "from contexer import store; import json,sys; "
        "raw=sys.stdin.read(); "
        "print(json.dumps(store.get_post_compact_context(sys.argv[1], store.session_from_hook_stdin(raw))))"
    )
    # Every ~/.contexer write here is best-effort (#152) — see claude.py's anchor_cmd for
    # why the redirect is wrapped in braces rather than trailing a bare `2>/dev/null`.
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
    )
    # Nudge to review decisions pending the developer. Reuses claude.review_nudge (codex-parity):
    # a Python entrypoint so it is per-repo and verifies the store — no false / cross-repo nudge.
    review_cmd = ('REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd) && '
                  f'"{python}" -c "from contexer.adapters import claude; import sys; '
                  'print(claude.review_nudge(sys.argv[1], sys.stdin.read()))" "$REPO"')
    cap_con = ('REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd) && '
               f'"{python}" -c "from contexer.adapters import claude; import sys; '
               'print(claude.capture_constraint(sys.argv[1], sys.stdin.read()))" "$REPO"')
    cap_rat = ('REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd) && '
               f'"{python}" -c "from contexer.adapters import claude; import sys; '
               'print(claude.rationale(sys.argv[1], sys.stdin.read()))" "$REPO"')
    # Team delta poll (T2): Codex shares Claude's UserPromptSubmit output schema, so
    # claude.team_poll is reused — non-blocking, fail-soft, injects newly-approved team
    # decisions on the next prompt. The third arg tags this consumer "codex" so a Codex and a
    # Claude session on the same repo each get every synced batch once (independent high-water
    # markers) instead of racing for a single per-repo delivery.
    cap_poll = ('REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd) && '
                f'"{python}" -c "from contexer.adapters import claude; import sys; '
                'print(claude.team_poll(sys.argv[1], sys.stdin.read(), \'codex\'))" "$REPO"')
    # PostToolUse (issue #175 Task 2): reuse claude.post_write VERBATIM — Codex shares
    # Claude's PostToolUse hookSpecificOutput schema, so the same Python entrypoint records
    # edited files into the per-session sidecar and arms .pending_capture. The $REPO prefix
    # is copied character-for-character from claude.py's own post_write_cmd (`|| true`, not
    # this file's usual `|| pwd` fallback) — verbatim reuse, established by the shelved
    # feat/doc-drift branch. See claude.post_write's docstring for the hazard this guards
    # against: a cwd-vs-toplevel mismatch would silently key a different sidecar.
    post_write_cmd = ('REPO=$(git rev-parse --show-toplevel 2>/dev/null || true) && '
                      f'"{python}" -c "from contexer.adapters import claude; import sys; '
                      'print(claude.post_write(sys.argv[1], sys.stdin.read()))" "$REPO" '
                      '# .pending_capture')

    # MCP server (~/.codex/config.toml) — surgical text edit so the user's plugins,
    # marketplaces, projects, other mcp_servers, and secrets stay byte-for-byte intact.
    config_path = home / ".codex" / "config.toml"
    try:
        existing = config_path.read_text(encoding="utf-8") if config_path.exists() else ""
    except (OSError, UnicodeDecodeError):
        # The same tolerance _read_config_command already applies to this file. Raising here
        # would surface through cli._run_guarded as "not valid JSON" (it is TOML), and it
        # would strand `--target all` half-installed: the adapters iterated before Codex
        # already wired, the ones after it never reached.
        existing = None
        log.append("  ! ~/.codex/config.toml is unreadable (not UTF-8?) — left untouched (fix it, then re-run)")
    if existing is not None:
        new_text = _set_contexer_stanza(existing, contexer_bin)
        try:
            tomllib.loads(new_text)
        except tomllib.TOMLDecodeError:
            log.append("  ! ~/.codex/config.toml is not valid TOML — left untouched (fix it, then re-run)")
        else:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            _store.atomic_write(config_path, new_text)
            log.append("  ✓ MCP server registered in ~/.codex/config.toml")

    # Hooks (~/.codex/hooks.json) — same JSON schema and event names as Claude Code.
    hooks_path = home / ".codex" / "hooks.json"
    cfg = base._load(hooks_path)
    hooks = cfg.setdefault("hooks", {})

    ss = hooks.setdefault("SessionStart", [])
    # Migrate: replace any installed SessionStart group whose command isn't byte-identical
    # to the current ss_code (_strip_stale). Mirrors claude.py's SessionStart gate — see
    # its comment for why marker-missing checks can't catch a NEWER/sibling-branch hook
    # (a marker superset with an incompatible signature that crashes every session start).
    ss = base._strip_stale(ss, ["get_session_start_context"], _py(ss_code))
    hooks["SessionStart"] = ss
    if not base._in_groups(ss, "get_session_start_context"):
        ss.insert(0, {"hooks": [{"type": "command",
            "statusMessage": "Loading session context...", "command": _py(ss_code)}]})

    # PostToolUse: claude.post_write (reused verbatim) records edited files into the
    # per-session sidecar (issue #175 Task 2) AND still arms the deterministic
    # .pending_capture flag; the next UserPromptSubmit (anchor_cmd) consumes it and injects
    # the capture reminder. No Stop hook - end-of-turn prompting added latency/tokens with
    # no functional gain over the next-prompt anchor.
    put = hooks.setdefault("PostToolUse", [])
    # Migrate: replace the old shell-only `.pending_capture` touch (pre- or post-#152) with
    # claude.post_write. Detected by the `.pending_capture` marker without `claude.post_write`.
    if base._in_groups(put, ".pending_capture") and not base._in_groups(put, "claude.post_write"):
        put = base._filter_groups(put, [".pending_capture"])
        hooks["PostToolUse"] = put
    # Migrate: an installed post_write hook resolving the repo from raw cwd (no $REPO
    # threading) — mirrors claude.py's migration gate for the same doc-drift hazard.
    if base._in_groups(put, "claude.post_write") and not base._in_groups(put, "show-toplevel"):
        put = base._filter_groups(put, ["claude.post_write"])
        hooks["PostToolUse"] = put
    if not base._in_groups(put, "claude.post_write"):
        put.append({"matcher": "Write|Edit", "hooks": [{"type": "command",
            "command": post_write_cmd}]})

    # Retire any previously-installed Stop hook. The Stop entry stays in _EVENT_MARKERS so
    # uninstall/reinstall strips an old Stop hook from hooks.json.
    st = hooks.get("Stop", [])
    new_st = base._filter_groups(st, [".pending_capture"])
    if new_st != st:
        if new_st:
            hooks["Stop"] = new_st
        else:
            hooks.pop("Stop", None)

    pc = hooks.setdefault("PreCompact", [])
    if not base._in_groups(pc, "compaction starting"):
        pc.append({"hooks": [{"type": "command",
            "statusMessage": "Saving decisions before compact...",
            "command": "echo '{\"systemMessage\": \"Contexer: context compaction starting — call update_context for any decisions not yet stored\"}'"}]})

    poc = hooks.setdefault("PostCompact", [])
    # Migrate: replace any installed PostCompact group whose command isn't byte-identical
    # to the current post_code (_strip_stale) — same skew class as the SessionStart gate.
    poc = base._strip_stale(poc, ["get_post_compact_context"], _py(post_code))
    hooks["PostCompact"] = poc
    if not base._in_groups(poc, "get_post_compact_context"):
        poc.append({"hooks": [{"type": "command",
            "statusMessage": "Reloading context after compact...", "command": _py(post_code)}]})

    ups = hooks.setdefault("UserPromptSubmit", [])
    # Migrate: replace the old capture-only anchor text with the reconciliation-framed one
    # (settle checkpoint: promote / revise / drop provisional decisions). Mirrors claude.py.
    if base._in_groups(ups, "you wrote or edited files") and not base._in_groups(ups, "last turn settled"):
        ups = base._filter_groups(ups, [".pending_capture"])
        hooks["UserPromptSubmit"] = ups
    # Migrate (#152): an anchor hook that writes ~/.contexer unguarded. Same gate as
    # claude.py — _in_commands because _ANCHOR_GUARD contains a quote.
    if base._in_groups(ups, ".pending_capture") and not base._in_commands(ups, claude._ANCHOR_GUARD):
        ups = base._filter_groups(ups, [".pending_capture"])
        hooks["UserPromptSubmit"] = ups
    if not base._in_groups(ups, ".pending_capture"):
        ups.insert(0, {"hooks": [{"type": "command",
            "statusMessage": "Anchoring repo context...", "command": anchor_cmd}]})
    # `once` mirrors Claude. If Codex ignores it the bootstrap offer degrades gracefully
    # to a silent {} once context exists.
    if not base._in_groups(ups, "get_bootstrap_context_prompt"):
        ups.append({"hooks": [{"type": "command", "once": True,
            "statusMessage": "Checking bootstrap context...", "command": _py(boot_code)}]})
    # Retire any previously-installed task-capture hook (the feature was removed).
    if base._in_groups(ups, "claude.capture_task"):
        ups = base._filter_groups(ups, ["claude.capture_task"])
        hooks["UserPromptSubmit"] = ups
    if not base._in_groups(ups, "claude.capture_constraint"):
        ups.append({"hooks": [{"type": "command",
            "statusMessage": "Checking for constraint directives...", "command": cap_con}]})
    if not base._in_groups(ups, "claude.rationale"):
        ups.append({"hooks": [{"type": "command",
            "statusMessage": "Checking for relevant decisions...", "command": cap_rat}]})
    # Migrate: the pre-consumer codex team-poll hook called claude.team_poll WITHOUT the
    # "codex" tag, so a Claude and a Codex session on the same repo raced to claim a single
    # per-repo delivery and only one got the injection. Replace it with the tagged call.
    # Keyed on the QUOTED 'codex' consumer marker (absent from the old string) so it runs
    # once. Quoting matters: a bare "codex" substring check would false-positive on any
    # unrelated hook whose command merely mentions "codex" (e.g. a path containing
    # "codex-tools"), silently suppressing the migration. Only the tagged call this codebase
    # generates contains 'codex' as a quoted argument.
    # _in_commands, NOT _in_groups: the latter matches a dict repr, which escapes the
    # quotes to \'codex\' — so this gate never recognized its own output and re-fired on
    # every single install, stripping and re-appending the hook (harmless but endless
    # churn: it reordered UserPromptSubmit, so no install was ever idempotent).
    if base._in_groups(ups, "claude.team_poll") and not base._in_commands(ups, "'codex'"):
        ups = base._filter_groups(ups, ["claude.team_poll"])
        hooks["UserPromptSubmit"] = ups
    if not base._in_groups(ups, "claude.team_poll"):
        ups.append({"hooks": [{"type": "command",
            "statusMessage": "Checking for new team decisions...", "command": cap_poll}]})
    if not base._in_groups(ups, "claude.review_nudge"):
        ups.append({"hooks": [{"type": "command",
            "statusMessage": "Checking for decisions pending review...", "command": review_cmd}]})

    base._save(hooks_path, cfg)
    log.append("  ✓ Hooks registered in ~/.codex/hooks.json")
    log.append("  ℹ Codex will ask once to approve Contexer's MCP tools — approve to allow.")
    return log


_EVENT_MARKERS = {
    "SessionStart":     ["get_session_start_context"],
    "PostToolUse":      [".pending_capture", "claude.post_write"],
    "Stop":             [".pending_capture"],
    "PreCompact":       ["compaction starting"],
    "PostCompact":      ["get_post_compact_context"],
    "UserPromptSubmit": [".current_repo", ".pending_capture", "claude.review_nudge",
                         "get_bootstrap_context_prompt",
                         "claude.capture_task", "claude.capture_constraint", "claude.rationale",
                         "claude.team_poll"],
}


def uninstall(home: Path) -> list[str]:
    """Remove the Codex MCP server + hooks. Does NOT touch the store (--purge's concern)."""
    log: list[str] = []

    config_path = home / ".codex" / "config.toml"
    try:
        text = config_path.read_text(encoding="utf-8") if config_path.exists() else None
    except (OSError, UnicodeDecodeError):
        text = None      # must not abort uninstall — the user has to get out of this state
        log.append("  ! ~/.codex/config.toml is unreadable (not UTF-8?) — left untouched")
    if text is not None:
        new_text = _remove_contexer_stanza(text)
        if new_text == text:
            log.append("  - No MCP server entry found in ~/.codex/config.toml")
        else:
            try:
                tomllib.loads(new_text)
            except tomllib.TOMLDecodeError:
                log.append("  ! ~/.codex/config.toml is not valid TOML — left untouched")
            else:
                _store.atomic_write(config_path, new_text)
                log.append("  ✓ MCP server removed from ~/.codex/config.toml")

    hooks_path = home / ".codex" / "hooks.json"
    if hooks_path.exists():
        cfg = base._load(hooks_path)
        hooks = cfg.get("hooks", {})
        changed = False
        for event, markers in _EVENT_MARKERS.items():
            before = hooks.get(event, [])
            after = base._filter_groups(before, markers)
            if after != before:
                changed = True
                if after:
                    hooks[event] = after
                else:
                    hooks.pop(event, None)
        if changed:
            base._save(hooks_path, cfg)
            log.append("  ✓ Hooks removed from ~/.codex/hooks.json")
        else:
            log.append("  - No Contexer hooks found in ~/.codex/hooks.json")

    return log


def _mcp_and_hooks_ok(home: Path) -> tuple:
    """Read the Codex config (tolerant of corruption) and report (mcp_command, hooks_ok).
    Shared by status_lines and is_installed."""
    mcp = _read_config_command(home)
    raw_hooks = base._load_safe(home / ".codex" / "hooks.json").get("hooks", {})
    hooks = raw_hooks if isinstance(raw_hooks, dict) else {}

    def _groups(event: str) -> list:
        v = hooks.get(event, [])
        return v if isinstance(v, list) else []

    hooks_ok = (base._in_groups(_groups("SessionStart"), "get_session_start_context")
                and base._in_groups(_groups("UserPromptSubmit"), "claude.rationale"))
    return mcp, hooks_ok


def status_lines(home: Path) -> list[str]:
    mcp, hooks_ok = _mcp_and_hooks_ok(home)
    return [
        "  [codex]",
        f"    MCP server: {'registered → ' + mcp if mcp else 'NOT registered'}",
        f"    hooks:      {'installed' if hooks_ok else 'missing or partial'}",
    ]


def is_installed(home: Path) -> bool:
    """True when both the MCP server and the core hooks are wired for Codex."""
    mcp, hooks_ok = _mcp_and_hooks_ok(home)
    return bool(mcp) and hooks_ok
