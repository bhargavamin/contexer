"""Google Gemini CLI integration adapter."""
import hashlib
import json
import shutil
import sys
from pathlib import Path

from contexer import store
from contexer.adapters import base

NAME = "gemini"

_PENDING_CAPTURE = ".pending_capture"
_PENDING_RELOAD = ".gemini_pending_reload"
_REMINDER = (
    "Contexer: you wrote or edited files last turn — call update_context for: "
    "(1) any NEW architecture/pattern/constraint/convention decisions; "
    "(2) any EXISTING approach you applied again (the server deduplicates)."
)


def is_present(home: Path) -> bool:
    return (home / ".gemini").exists()


def _output(event: str, contexts: list[str]) -> str:
    context = "\n\n".join(part for part in contexts if part)
    if not context:
        return json.dumps({"suppressOutput": True})
    return json.dumps({
        "suppressOutput": True,
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": context,
        },
    })


def _session_marker(raw: str) -> Path:
    try:
        data = json.loads(raw)
        identity = data.get("session_id") or data.get("transcript_path") or "unknown"
    except Exception:
        identity = "unknown"
    digest = hashlib.sha256(str(identity).encode()).hexdigest()[:24]
    return store.STORE_DIR / f".gemini_first_prompt_{digest}"


def _anchor(repo: str) -> None:
    if repo and store._is_sane_repo(repo):
        store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        (store.STORE_DIR / ".current_repo").write_text(repo)


def session_start(repo_path: str, raw: str) -> str:
    """Inject stored rules on startup, resume, and /clear without user-facing noise."""
    try:
        repo = store._resolve_repo(repo_path)
        if not repo:
            return _output("SessionStart", [])
        _anchor(repo)
        _session_marker(raw).unlink(missing_ok=True)
        payload = store.session_start_payload(repo, store.source_from_hook_stdin(raw))
        return _output("SessionStart", [payload.get("context", "")])
    except Exception:
        return _output("SessionStart", [])


def before_agent(repo_path: str, raw: str) -> str:
    """Run per-prompt capture, retrieval, and deferred post-compression reinjection."""
    try:
        repo = store._resolve_repo(repo_path)
        if not repo:
            return _output("BeforeAgent", [])
        _anchor(repo)
        prompt = store.prompt_from_hook_stdin(raw)
        session_id = store.session_from_hook_stdin(raw)
        contexts: list[str] = []

        pending = store.STORE_DIR / _PENDING_CAPTURE
        if pending.exists():
            pending.unlink(missing_ok=True)
            contexts.append(_REMINDER)

        reload_flag = store.STORE_DIR / _PENDING_RELOAD
        if reload_flag.exists():
            reload_flag.unlink(missing_ok=True)
            payload = store.post_compact_payload(repo)
            contexts.extend(part for part in (payload.get("status"), payload.get("context")) if part)

        marker = _session_marker(raw)
        if not marker.exists():
            payload = store.bootstrap_prompt_payload(repo, prompt)
            contexts.append(payload.get("context", ""))
            store.capture_task(repo, prompt, session_id)
            marker.parent.mkdir(mode=0o700, exist_ok=True)
            marker.touch()

        entry_id, content = store.capture_user_constraint(repo, prompt, session_id)
        if entry_id is not None:
            contexts.append(
                f"Auto-stored as constraint: '{content}'. Acknowledge this briefly to the user."
            )

        rationale = store.get_context_for_prompt(repo, prompt)
        if rationale:
            contexts.append(rationale)
        return _output("BeforeAgent", contexts)
    except Exception:
        return _output("BeforeAgent", [])


def after_write(repo_path: str, raw: str) -> str:
    """AfterTool(write_file|replace): flag a reminder for the next user prompt."""
    try:
        store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        (store.STORE_DIR / _PENDING_CAPTURE).touch()
    except Exception:
        pass
    return json.dumps({"suppressOutput": True})


def pre_compress(repo_path: str, raw: str) -> str:
    """Defer capture reminder and full reload to the first turn after compression."""
    try:
        store.STORE_DIR.mkdir(mode=0o700, exist_ok=True)
        (store.STORE_DIR / _PENDING_CAPTURE).touch()
        (store.STORE_DIR / _PENDING_RELOAD).touch()
    except Exception:
        pass
    return json.dumps({"suppressOutput": True})


def session_end(repo_path: str, raw: str) -> str:
    """Best-effort cleanup of the per-session first-prompt marker."""
    try:
        _session_marker(raw).unlink(missing_ok=True)
    except Exception:
        pass
    return json.dumps({"suppressOutput": True})


def _cmd(entry: str) -> str:
    python = sys.executable
    return (
        "REPO=$(git rev-parse --show-toplevel 2>/dev/null || true) && "
        f'"{python}" -c "from contexer.adapters import gemini; import sys; '
        f'print(gemini.{entry}(sys.argv[1], sys.stdin.read()))" "$REPO"'
    )


_EVENT_MARKERS = {
    "SessionStart": ["gemini.session_start"],
    "BeforeAgent": ["gemini.before_agent"],
    "AfterTool": ["gemini.after_write"],
    "PreCompress": ["gemini.pre_compress"],
    "SessionEnd": ["gemini.session_end"],
}


def _group(entry: str, name: str, matcher: str | None = None) -> dict:
    group = {
        "hooks": [{
            "name": name,
            "type": "command",
            "command": _cmd(entry),
            "timeout": 10000,
            "description": "Managed by Contexer",
        }]
    }
    if matcher is not None:
        group["matcher"] = matcher
    return group


def install(home: Path) -> list[str]:
    """Register Contexer's MCP server and Gemini CLI hooks in settings.json."""
    gemini_dir = home / ".gemini"
    settings_path = gemini_dir / "settings.json"
    settings = base._load(settings_path)
    contexer_bin = shutil.which("contexer") or "contexer"
    settings.setdefault("mcpServers", {})["contexer"] = {"command": contexer_bin}

    hooks = settings.setdefault("hooks", {})
    desired = {
        "SessionStart": _group("session_start", "contexer-session-start"),
        "BeforeAgent": _group("before_agent", "contexer-before-agent", "*"),
        "AfterTool": _group("after_write", "contexer-after-write", "write_file|replace"),
        "PreCompress": _group("pre_compress", "contexer-pre-compress"),
        "SessionEnd": _group("session_end", "contexer-session-end"),
    }
    for event, group in desired.items():
        groups = hooks.setdefault(event, [])
        if not base._in_groups(groups, _EVENT_MARKERS[event][0]):
            groups.append(group)

    base._save(settings_path, settings)
    return [
        "  ✓ MCP server registered in ~/.gemini/settings.json",
        "  ✓ Hooks registered in ~/.gemini/settings.json",
        "  ℹ Gemini CLI will ask you to trust newly installed hooks.",
    ]


def uninstall(home: Path) -> list[str]:
    settings_path = home / ".gemini" / "settings.json"
    if not settings_path.exists():
        return []
    settings = base._load(settings_path)
    log: list[str] = []
    if settings.get("mcpServers", {}).pop("contexer", None):
        log.append("  ✓ MCP server removed from ~/.gemini/settings.json")
    hooks = settings.get("hooks", {})
    changed = False
    if isinstance(hooks, dict):
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
        log.append("  ✓ Hooks removed from ~/.gemini/settings.json")
    base._save(settings_path, settings)
    return log


def _mcp_and_hooks_ok(home: Path) -> tuple:
    settings = base._load_safe(home / ".gemini" / "settings.json")
    mcp = settings.get("mcpServers", {}).get("contexer")
    raw_hooks = settings.get("hooks", {})
    hooks = raw_hooks if isinstance(raw_hooks, dict) else {}

    def groups(event: str) -> list:
        value = hooks.get(event, [])
        return value if isinstance(value, list) else []

    hooks_ok = (
        base._in_groups(groups("SessionStart"), "gemini.session_start")
        and base._in_groups(groups("BeforeAgent"), "gemini.before_agent")
        and base._in_groups(groups("AfterTool"), "gemini.after_write")
    )
    return mcp, hooks_ok


def status_lines(home: Path) -> list[str]:
    mcp, hooks_ok = _mcp_and_hooks_ok(home)
    command = mcp.get("command") if isinstance(mcp, dict) else None
    return [
        "  [gemini]",
        f"    MCP server: {'registered → ' + command if command else 'NOT registered'}",
        f"    hooks:      {'installed' if hooks_ok else 'missing or partial'}",
    ]


def is_installed(home: Path) -> bool:
    mcp, hooks_ok = _mcp_and_hooks_ok(home)
    return bool(mcp) and hooks_ok
