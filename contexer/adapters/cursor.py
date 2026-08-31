"""Cursor integration adapter."""
import json
import shutil
import sys
from pathlib import Path

from contexer import evidence, store, updates
from contexer.adapters import base

NAME = "cursor"

# Prompt directives ONLY. Cursor's hooks cannot observe an edit (`beforeSubmitPrompt` is the
# one payload this adapter sees, #175), so `file_changes` says unavailable rather than
# reporting the zero events that absence produces - those are different facts.
EVIDENCE_COVERAGE = {
    "user_directives": "captured",              # capture_constraint, beforeSubmitPrompt
    "file_changes": "unavailable",              # no write hook
    "assistant_conclusions": "model_reported",  # the MCP tool, agent-invoked
    "test_results": "unavailable",
    "diffs": "unavailable",
}


def is_present(home: Path) -> bool:
    return (home / ".cursor").exists()


# Claude delivers these reminders via per-prompt hooks; Cursor can only inject at
# session start, so the nudge rides in additional_context (see the spec's parity matrix).
# It is intentionally prescriptive: Cursor's beforeSubmitPrompt cannot inject per-prompt
# context, so this once-per-session text is the main lever steering the model to use
# Contexer instead of reading files blindly or writing native .cursor/rules files.
_NUDGE = (
    "Contexer is active for this repo.\n"
    "- For ANY question about what this repo is, its architecture, design decisions, "
    "rationale, conventions, patterns, or 'why' something was done: CALL the contexer "
    "get_context tool FIRST, before reading files. For broad 'what is this repo' / "
    "overview questions call get_context with NO query (empty) to get the overview; "
    "use a query only for a specific topic. Only fall back to reading files if "
    "get_context returns nothing.\n"
    "- After writing or editing files: if the change involved an architectural, design, or "
    "engineering decision (a tech choice, a naming change, a constraint, a pattern) - tell "
    "the user what decision was made and call update_context to store it. If unsure whether "
    "it qualifies, surface it to the user and let them confirm.\n"
    "- Keep unratified work provisional: store observations and decisions the user approved, but "
    "do NOT record your own not-yet-approved proposals - or approaches you tried and then reverted "
    "- as settled fact. A decision from an approved-but-unimplemented plan is provisional until "
    "implementation validates it; capture it, then reconcile (approve what held, update what "
    "changed, drop what was abandoned) once it stuck.\n"
    "- To save a rule, constraint, convention, or decision: CALL contexer update_context "
    "(or update_global_context for cross-repo rules). Do NOT create a .cursor/rules/*.mdc "
    "file for these — Contexer is the store. The server deduplicates, so err on the side "
    "of calling it.\n"
    "- ALWAYS pass repo_path set to the absolute path of this workspace when calling any "
    "contexer tool — do not rely on auto-detection."
)

# Body of the managed always-apply Cursor rule. Cursor injects rules on every prompt
# natively, which is the only reliable per-prompt steering available (beforeSubmitPrompt
# cannot inject). Marker-guarded so a user's own rule files are never touched.
_RULE_FILENAME = "contexer.mdc"
_RULE_BODY = (
    "---\n"
    "description: Use Contexer (MCP) as the source of repo decisions and rules.\n"
    "alwaysApply: true\n"
    "---\n"
    f"<!-- {base._BOOTSTRAP_CMD_MARKER} -->\n"
    "# Contexer\n\n"
    "Before answering any question about what this repo is, its architecture, design "
    "decisions, rationale, conventions, patterns, or why something was done: call the "
    "contexer `get_context` MCP tool first (no query = overview; a query for a specific "
    "topic). Only read files if it returns nothing.\n\n"
    "After writing or editing files: if the change involved an architectural, design, or "
    "engineering decision (a tech choice, a naming change, a constraint, a pattern) - tell "
    "the user what decision was made and call `update_context` to store it. If unsure "
    "whether it qualifies, surface it to the user and let them confirm.\n\n"
    "Keep unratified work provisional: store observations and decisions the user approved, but "
    "do not record your own not-yet-approved proposals - or approaches you tried and then "
    "reverted - as settled fact. A decision from an approved-but-unimplemented plan is provisional "
    "until implementation validates it; capture it, then reconcile (approve what held, update what "
    "changed, drop what was abandoned) once it stuck.\n\n"
    "If Contexer reports decisions pending your review (its SessionStart context mentions a "
    "pending count), offer at a natural pause to show them — call `review_pending` — and "
    "approve via `approve_decision`, ONE id at a time, once the user confirms each — there is "
    "no bulk approve. "
    "They stay inactive until approved.\n\n"
    "When the user asks to create/save/remember a rule, constraint, convention, or "
    "decision: call contexer `update_context` (or `update_global_context` for cross-repo "
    "rules). Do not create a separate `.cursor/rules/*.mdc` file for it — Contexer is the "
    "store.\n\n"
    "Always pass `repo_path` set to the absolute path of this workspace when calling any "
    "contexer tool — do not rely on auto-detection.\n"
)


def format_session_start(payload: dict) -> dict:
    """Neutral payload -> Cursor sessionStart output. Cursor has no systemMessage
    channel and cannot inject per-prompt, so the behavioral nudge is appended here."""
    ctx = payload.get("context") or ""
    combined = f"{ctx}\n\n{_NUDGE}" if ctx else _NUDGE
    return {"additional_context": combined}


def format_prompt_passthrough() -> dict:
    """beforeSubmitPrompt output: allow the prompt. Cursor cannot inject context here,
    so capture hooks are write-only side effects that return this pass-through."""
    return {"continue": True}


def notify(text: str) -> dict | None:
    """This host's user-facing notice channel, or None when it has none. None here.

    Checked against Cursor's hooks documentation rather than assumed. Cursor has no
    `systemMessage` and no notification, toast, or status API. Its user-facing field is
    `user_message`, and on every event that carries one, including `beforeSubmitPrompt`, the
    docs define it as the message shown when the action is BLOCKED. Reaching the developer
    would therefore cost them their prompt, which is not a price a release announcement may
    charge. The one unconditional exception is `preCompact.user_message`, which fires only at
    context compaction and so is not a general channel.

    Returning None is the honest answer rather than a defect. The caller falls back to the
    host-independent terminal backstop. Routing the notice through Cursor's sessionStart
    prescriptive block was rejected deliberately: that is model relay, which makes an
    operational notice probabilistic, and a notice the model may or may not repeat is worse
    than a silent host. The day Cursor ships a real channel, this function is the only thing
    that changes.
    """
    return None


def _repo_from(raw: str, repo_path: str) -> str:
    """Cursor sessionStart provides workspace_roots[]; fall back to .current_repo."""
    return _repo_from_verbose(raw, repo_path)[0]


def _repo_from_verbose(raw: str, repo_path: str) -> tuple[str, str]:
    """`_repo_from` plus WHICH of its signals produced the repo — the provenance the
    wrong-store audit reads (`scope_audit.py`). Cursor has one signal the other hosts do not
    (`workspace_roots`), so it gets its own label rather than being flattened into
    `argument`, which the audit reads as a deliberate cross-repo write."""
    hooked = store.hook_repo_from_stdin(raw, repo_path)
    repo, source = store.resolve_repo_verbose(hooked)
    if source == "argument":
        try:
            roots = json.loads(raw).get("workspace_roots") or []
        except (AttributeError, TypeError, ValueError):
            roots = []
        source = "workspace-root" if not repo_path and roots else "hook-arg"
    return repo, source


def _ensure_rule_file(repo_dir: str) -> None:
    """Write a Contexer-managed always-apply rule into <workspace>/.cursor/rules/.

    Cursor injects rules on every prompt natively — the only reliable per-prompt steering,
    since beforeSubmitPrompt cannot inject context. Marker-guarded and idempotent: a user's
    own rule files are never touched, and we only (re)write our own managed file. Best-effort;
    never raises — sessionStart must not crash Cursor."""
    try:
        if not repo_dir:
            return
        rules_dir = Path(repo_dir) / ".cursor" / "rules"
        rule_path = rules_dir / _RULE_FILENAME
        if rule_path.exists():
            current = rule_path.read_text(encoding="utf-8")
            # Only overwrite a file that is already ours; refresh it if our body changed.
            if base._BOOTSTRAP_CMD_MARKER not in current:
                return
            if current == _RULE_BODY:
                return
        rules_dir.mkdir(parents=True, exist_ok=True)
        rule_path.write_text(_RULE_BODY, encoding="utf-8")
    except Exception:
        pass


def session_start(repo_path: str, raw: str) -> str:
    """Cursor sessionStart: write .current_repo, ensure managed rule, inject rules + nudge.
    Never raises."""
    try:
        repo = _repo_from(raw, repo_path)
        if repo and store.is_sane_repo(repo):
            # Sanity-checked AND fail-soft (#152): never poison the shared pointer with a
            # config/home dir, and never let an unwritable ~/.contexer drop us into the
            # except below, which would inject the bare nudge instead of the real rules.
            store.anchor_repo(repo)
            _ensure_rule_file(repo)
            # `host=NAME`: the shared payload reconciles this repo's unconsumed evidence, and
            # Cursor is the host that CAN say least about what it saw (no write hook at all),
            # so a receipt naming it is the difference between "no file changes" and "no way
            # to observe one". This is Cursor's only reconciliation checkpoint of any kind.
            payload = store.session_start_payload(repo, "", "", NAME)
        else:
            payload = {"status": "", "context": ""}
        return json.dumps(format_session_start(payload))
    except Exception:
        return json.dumps({"additional_context": _NUDGE})


def _anchor_current_repo(repo: str) -> None:
    """Persist the active workspace to ~/.contexer/.current_repo so bare MCP calls (no
    repo_path) resolve correctly.

    Cursor launches the MCP server outside the project, so the server's cwd-derived
    _SESSION_REPO is empty — and only sessionStart wrote the pointer, which then went stale
    or got clobbered. Refreshing it on every prompt from workspace_roots (a base field on
    all Cursor hooks) mirrors Claude's per-prompt anchor and is what keeps get_context({})
    working. Guarded by is_sane_repo + best-effort (hooks must never crash the host)."""
    store.anchor_repo(repo)


def capture_constraint(repo_path: str, raw: str) -> str:
    """beforeSubmitPrompt: anchor the repo pointer + auto-store 'always/never' directives.

    Cursor's evidence is prompt-only: its hooks cannot observe an edit, so this host emits
    `user_directive` and never `file_changed` - an absent event here means Cursor could not
    see the edit, which is what the spool should say.

    This hook also keeps the update state warm. Cursor has no channel that can SHOW a notice,
    so it never renders one, but without a refresh somewhere the state on a Cursor-only machine
    is never fetched at all: the terminal backstop would then only START the fetch on the
    developer's first `contexer` command and have nothing to say until their second. One stat
    on a hook that already runs, and a fork only on the day the state expires.
    """
    try:
        # Its own try, before the capture: a refresh failure must not cost the directive
        # capture, which is this hook's actual job.
        updates.spawn_refresh()
    except Exception:
        pass
    try:
        repo, repo_source = _repo_from_verbose(raw, repo_path)
        if repo:
            _anchor_current_repo(repo)
            # Store call + shadow-mode event in one (evidence.capture_directive): identical
            # return and exceptions, so this hook's swallow-and-pass-through is unchanged.
            evidence.capture_directive(repo, store.prompt_from_hook_stdin(raw),
                                       store.session_from_hook_stdin(raw), "cursor_prompt",
                                       repo_source=repo_source)
    except Exception:
        pass
    return json.dumps(format_prompt_passthrough())


# ── install / uninstall / status ──────────────────────────────────────────────

_HOOK_MARKER_TASK = "cursor.capture_task"
_HOOK_MARKER_CON = "cursor.capture_constraint"
_HOOK_MARKER_SS = "cursor.session_start"


def capture_task(repo_path: str, raw: str) -> str:
    """Self-retiring no-op stub for the removed "last task" hook entrypoint (#58).

    Mirrors claude.capture_task: install() strips the capture-task hook on
    reinstall, but a package-only upgrade leaves the installed beforeSubmitPrompt
    hook calling this function — without the stub that is an AttributeError
    traceback on every prompt. Being invoked is proof of the stale hook, so remove
    it from ~/.cursor/hooks.json and pass the prompt through. Args are unused but
    keep the installed hook's call signature. Fail-soft."""
    try:
        path = Path.home() / ".cursor" / "hooks.json"
        cfg = base._load_safe(path)
        hk = cfg.get("hooks")
        if isinstance(hk, dict) and isinstance(hk.get("beforeSubmitPrompt"), list):
            bsp = hk["beforeSubmitPrompt"]
            after = [h for h in bsp
                     if _HOOK_MARKER_TASK not in base._hook_command(h)]
            if after != bsp:
                if after:
                    hk["beforeSubmitPrompt"] = after
                else:
                    hk.pop("beforeSubmitPrompt", None)
                if not hk:
                    cfg.pop("hooks", None)
                base._save(path, cfg)
    except Exception:
        pass
    return json.dumps(format_prompt_passthrough())


def _cmd(entry: str) -> str:
    """A Cursor command hook: pass repo via "" (session_start reads workspace_roots from
    stdin); read stdin for prompt/session. Cursor runs hooks from the project root."""
    python = sys.executable
    return (f'"{python}" -P -c "from contexer.adapters import cursor; import sys; '
            f'print(cursor.{entry}(\'\', sys.stdin.read()))"')


def _has(hook_list: list, marker: str) -> bool:
    return any(marker in base._hook_command(h) for h in hook_list)


def install(home: Path) -> list[str]:
    log: list[str] = []
    cursor_dir = home / ".cursor"
    contexer_bin = shutil.which("contexer") or "contexer"

    mcp_path = cursor_dir / "mcp.json"
    mcp = base._load(mcp_path)
    servers = mcp.setdefault("mcpServers", {})
    servers["contexer"] = {"command": contexer_bin}
    # The native Teams MCP surface is retired. Team sync now goes exclusively through
    # the local Python client, so reinstall must remove the exact legacy key while
    # preserving every similarly named user-owned server.
    servers.pop("contexer-teams", None)
    base._save(mcp_path, mcp)
    log.append("  ✓ MCP server registered in ~/.cursor/mcp.json")

    hooks_path = cursor_dir / "hooks.json"
    cfg = base._load(hooks_path)
    cfg["version"] = 1
    hk = cfg.setdefault("hooks", {})

    # Converge each Contexer hook on its exact current command (`base._strip_stale_flat`,
    # the claude.py/codex.py `_strip_stale` rule for Cursor's FLAT hook shape - these lists
    # hold bare `{type, command}` dicts, not the grouped shape): a drift the marker checks
    # below don't know about - the -P flag (without which `python -c` prepends cwd to
    # sys.path and a checked-out contexer repo shadows the installed package) - would
    # otherwise leave an installed hook running the old command forever.
    ss = hk.setdefault("sessionStart", [])
    base._strip_stale_flat(ss, _HOOK_MARKER_SS, _cmd("session_start"))
    if not _has(ss, _HOOK_MARKER_SS):
        ss.append({"type": "command", "command": _cmd("session_start")})

    bsp = hk.setdefault("beforeSubmitPrompt", [])
    # Retire any previously-installed task-capture hook (the feature was removed).
    bsp[:] = [h for h in bsp if _HOOK_MARKER_TASK not in base._hook_command(h)]
    base._strip_stale_flat(bsp, _HOOK_MARKER_CON, _cmd("capture_constraint"))
    if not _has(bsp, _HOOK_MARKER_CON):
        bsp.append({"type": "command", "command": _cmd("capture_constraint")})

    base._save(hooks_path, cfg)
    log.append("  ✓ Hooks registered in ~/.cursor/hooks.json")
    log.append("  ℹ Cursor hooks require Cursor 1.7+.")
    log.append("  ℹ Cursor will ask once to approve Contexer's MCP tools — approve to allow.")
    return log


def uninstall(home: Path) -> list[str]:
    log: list[str] = []
    cursor_dir = home / ".cursor"

    mcp_path = cursor_dir / "mcp.json"
    if mcp_path.exists():
        mcp = base._load(mcp_path)
        servers = mcp.get("mcpServers", {})
        removed = servers.pop("contexer", None)
        removed_teams = servers.pop("contexer-teams", None)
        if removed is not None or removed_teams is not None:
            base._save(mcp_path, mcp)
            log.append("  ✓ MCP server removed from ~/.cursor/mcp.json")

    hooks_path = cursor_dir / "hooks.json"
    if hooks_path.exists():
        cfg = base._load(hooks_path)
        hk = cfg.get("hooks", {})
        changed = False
        for event, markers in {
            "sessionStart": [_HOOK_MARKER_SS],
            "beforeSubmitPrompt": [_HOOK_MARKER_TASK, _HOOK_MARKER_CON],
        }.items():
            before = hk.get(event, [])
            after = [h for h in before
                     if not any(m in base._hook_command(h) for m in markers)]
            if after != before:
                changed = True
                if after:
                    hk[event] = after
                else:
                    hk.pop(event, None)
        if changed:
            base._save(hooks_path, cfg)
            log.append("  ✓ Hooks removed from ~/.cursor/hooks.json")
    return log


def _mcp_and_hooks_ok(home: Path) -> tuple:
    """Read the Cursor config (tolerant of corruption) and report (mcp_entry, hooks_ok).
    Shared by status_lines and is_installed."""
    cursor_dir = home / ".cursor"
    mcp = base._load_safe(cursor_dir / "mcp.json").get("mcpServers", {}).get("contexer")
    hk = base._load_safe(cursor_dir / "hooks.json").get("hooks", {})
    ss = hk.get("sessionStart", []) if isinstance(hk, dict) else []
    hooks_ok = any(_HOOK_MARKER_SS in base._hook_command(h) for h in ss)
    return mcp, hooks_ok


def status_lines(home: Path) -> list[str]:
    mcp, hooks_ok = _mcp_and_hooks_ok(home)
    return [
        "  [cursor]",
        f"    MCP server: {'registered' if mcp else 'NOT registered'}",
        f"    hooks:      {'installed' if hooks_ok else 'missing or partial'}",
    ]


def is_installed(home: Path) -> bool:
    """True when both the MCP server and the sessionStart hook are wired for Cursor."""
    mcp, hooks_ok = _mcp_and_hooks_ok(home)
    return bool(mcp) and hooks_ok
