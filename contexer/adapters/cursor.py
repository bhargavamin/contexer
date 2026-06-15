"""Cursor integration adapter."""
import json
import shutil
import sys
from pathlib import Path

from contexer import store
from contexer.adapters import base

NAME = "cursor"


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
    "- To save a rule, constraint, convention, or decision: CALL contexer update_context "
    "(or update_global_context for cross-repo rules). Do NOT create a .cursor/rules/*.mdc "
    "file for these — Contexer is the store. The server deduplicates, so err on the side "
    "of calling it."
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
    "When the user asks to create/save/remember a rule, constraint, convention, or "
    "decision: call contexer `update_context` (or `update_global_context` for cross-repo "
    "rules). Do not create a separate `.cursor/rules/*.mdc` file for it — Contexer is the "
    "store.\n"
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


def _repo_from(raw: str, repo_path: str) -> str:
    """Cursor sessionStart provides workspace_roots[]; fall back to .current_repo."""
    if repo_path:
        return store._resolve_repo(repo_path)
    try:
        roots = json.loads(raw).get("workspace_roots") or []
        if roots:
            return store._resolve_repo(roots[0])
    except Exception:
        pass
    return store._resolve_repo("")


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
        if repo:
            store.STORE_DIR.mkdir(exist_ok=True)
            (store.STORE_DIR / ".current_repo").write_text(repo)
            _ensure_rule_file(repo)
            payload = store.session_start_payload(repo)
        else:
            payload = {"status": "", "context": ""}
        return json.dumps(format_session_start(payload))
    except Exception:
        return json.dumps({"additional_context": _NUDGE})


def capture_task(repo_path: str, raw: str) -> str:
    """beforeSubmitPrompt: store the prompt as the task (write-only).

    v1 note: Cursor's beforeSubmitPrompt has no "once" semantics, so this runs on
    every prompt and store.capture_task replaces the task entry each time — Cursor
    tracks the *latest* prompt-as-task, not just the first. Acceptable for v1.
    """
    try:
        repo = _repo_from(raw, repo_path)
        if repo:
            store.capture_task(repo, store.prompt_from_hook_stdin(raw),
                               store.session_from_hook_stdin(raw))
    except Exception:
        pass
    return json.dumps(format_prompt_passthrough())


def capture_constraint(repo_path: str, raw: str) -> str:
    """beforeSubmitPrompt: auto-store 'always/never' directives (write-only; no ack)."""
    try:
        repo = _repo_from(raw, repo_path)
        if repo:
            store.capture_user_constraint(repo, store.prompt_from_hook_stdin(raw),
                                          store.session_from_hook_stdin(raw))
    except Exception:
        pass
    return json.dumps(format_prompt_passthrough())


# ── install / uninstall / status ──────────────────────────────────────────────

_HOOK_MARKER_TASK = "cursor.capture_task"
_HOOK_MARKER_CON = "cursor.capture_constraint"
_HOOK_MARKER_SS = "cursor.session_start"


def _cmd(entry: str) -> str:
    """A Cursor command hook: pass repo via "" (session_start reads workspace_roots from
    stdin); read stdin for prompt/session. Cursor runs hooks from the project root."""
    python = sys.executable
    return (f'"{python}" -c "from contexer.adapters import cursor; import sys; '
            f'print(cursor.{entry}(\'\', sys.stdin.read()))"')


def _has(hook_list: list, marker: str) -> bool:
    return any(marker in h.get("command", "") for h in hook_list)


def install(home: Path) -> list[str]:
    log: list[str] = []
    cursor_dir = home / ".cursor"
    contexer_bin = shutil.which("contexer") or "contexer"

    mcp_path = cursor_dir / "mcp.json"
    mcp = base._load(mcp_path)
    mcp.setdefault("mcpServers", {})["contexer"] = {"command": contexer_bin}
    base._save(mcp_path, mcp)
    log.append("  ✓ MCP server registered in ~/.cursor/mcp.json")

    hooks_path = cursor_dir / "hooks.json"
    cfg = base._load(hooks_path)
    cfg["version"] = 1
    hk = cfg.setdefault("hooks", {})

    ss = hk.setdefault("sessionStart", [])
    if not _has(ss, _HOOK_MARKER_SS):
        ss.append({"type": "command", "command": _cmd("session_start")})

    bsp = hk.setdefault("beforeSubmitPrompt", [])
    if not _has(bsp, _HOOK_MARKER_TASK):
        bsp.append({"type": "command", "command": _cmd("capture_task")})
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
        if mcp.get("mcpServers", {}).pop("contexer", None):
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
                     if not any(m in h.get("command", "") for m in markers)]
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
    hooks_ok = any(_HOOK_MARKER_SS in h.get("command", "") for h in ss)
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
