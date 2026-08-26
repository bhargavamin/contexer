"""Google Gemini CLI integration adapter."""
import hashlib
import json
import shutil
import sys
from pathlib import Path

from contexer import evidence, store
from contexer.adapters import base

NAME = "gemini"

# What this host's installed hooks observe. `before_agent` captures directives and
# `AfterTool(write_file|replace)` captures edits; nothing hands over the model's response.
EVIDENCE_COVERAGE = {
    "user_directives": "captured",              # capture_constraint, BeforeAgent
    "file_changes": "captured",                 # after_write, AfterTool(write_file|replace)
    "assistant_conclusions": "model_reported",  # the MCP tool, agent-invoked
    "test_results": "unavailable",
    "diffs": "unavailable",
}

# Fix 1: namespaced so it doesn't collide with Claude's ~/.contexer/.pending_capture flag.
_PENDING_CAPTURE = ".gemini_pending_capture"
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


def _session_marker(raw: str) -> Path | None:
    # Fix 5: return None when no stable session identity is available so callers
    # know to skip the first-prompt gate rather than all sessions colliding on one
    # shared "unknown" marker file.
    try:
        data = json.loads(raw)
        identity = data.get("session_id") or data.get("transcript_path")
    except Exception:
        identity = None
    if not identity:
        return None
    digest = hashlib.sha256(str(identity).encode()).hexdigest()[:24]
    return store.STORE_DIR / f".gemini_first_prompt_{digest}"


def _anchor(repo: str) -> None:
    # Sanity-checked AND fail-soft (#152): an unwritable ~/.contexer must not cost the
    # session its stored rules — session_start's blanket except would swallow them.
    store.anchor_repo(repo)


# Flag files are bookkeeping, and session_start/before_agent wrap their whole body in a
# blanket `except Exception` that degrades to an EMPTY injection. So an unguarded flag
# write under an unwritable ~/.contexer (#152) does not just skip the flag — it silently
# costs the prompt its entire context: bootstrap, constraint ack, review nudge, the
# post-compaction reload, everything. These two helpers keep that failure local.

def _flag_drop(path: Path) -> None:
    """Best-effort consume of a flag file. A flag that cannot be cleared simply re-fires
    next turn — the same degradation the Claude/Codex shell hooks accept via `rm -f … ||
    true` — which is strictly better than losing the turn's injection."""
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def _flag_set(path: Path) -> None:
    """Best-effort raise of a flag file. A flag that cannot be set just means the
    guarded work re-runs next turn; every caller here is idempotent."""
    try:
        path.parent.mkdir(mode=0o700, exist_ok=True)
        path.touch()
    except OSError:
        pass


def session_start(repo_path: str, raw: str) -> str:
    """Inject stored rules on startup, resume, and /clear without user-facing noise.

    Repo resolution is `hook_cwd_repo`, NOT `resolve_repo` (Greptile P1 #2, PR #181,
    follow-up to 3fde7aa): this is a hook-invoked process, so `_SESSION_REPO` is always
    empty here and bare `resolve_repo` would fall through to the shared `.current_repo`
    pointer on an empty hook-supplied repo — which another session can have pointed at a
    DIFFERENT repo between hook events. See `after_write`'s docstring for the full
    rationale; this mirrors it so SessionStart keys the same store `before_agent` and
    `after_write` do."""
    try:
        repo = store.hook_cwd_repo(repo_path)
        if not repo:
            return _output("SessionStart", [])
        _anchor(repo)
        # Fix 7: only reset the first-prompt marker on a genuinely new session.
        # Resume and /clear continue an existing session — preserve the marker so
        # before_agent does not re-run bootstrap and task capture on the next prompt.
        source = store.source_from_hook_stdin(raw)
        if source not in ("resume", "clear"):
            marker = _session_marker(raw)
            if marker is not None:
                _flag_drop(marker)
        payload = store.session_start_payload(repo, source)
        return _output("SessionStart", [payload.get("context", "")])
    except Exception:
        return _output("SessionStart", [])


def before_agent(repo_path: str, raw: str) -> str:
    """Run per-prompt capture, retrieval, and deferred post-compression reinjection.

    Repo resolution is `hook_cwd_repo`, NOT `resolve_repo` (Greptile P1 #2, PR #181,
    follow-up to 3fde7aa): CAPTURE runs here (capture_user_constraint, the pending-review
    nudge, context payloads), so this must key the SAME store `after_write` records edits
    into. Before this fix, an empty hook-supplied repo (non-git project) fell through
    `resolve_repo` to the shared `.current_repo` pointer, which another session can move
    between hook events — a writer/reader repo-key split identical in shape to the
    session-id bug: `after_write` recorded the edit under the cwd-keyed store while capture
    read anchor candidates from the pointer-keyed store, so pending captures in non-git
    Gemini projects got no anchor candidates. `hook_cwd_repo` is guarded by `is_sane_repo`,
    so a hook firing in the home/config dir still resolves to nothing rather than a junk
    store."""
    try:
        repo = store.hook_cwd_repo(repo_path)
        if not repo:
            return _output("BeforeAgent", [])
        _anchor(repo)
        prompt = store.prompt_from_hook_stdin(raw)
        session_id = store.session_from_hook_stdin(raw)
        contexts: list[str] = []

        # Fix 3: check reload FIRST. A full post-compression reload makes the
        # "you edited files last turn" reminder redundant and misleading — the
        # write happened before compression, not on the immediately preceding turn.
        # When both flags are present, consume the capture flag silently.
        reload_flag = store.STORE_DIR / _PENDING_RELOAD
        pending = store.STORE_DIR / _PENDING_CAPTURE
        if reload_flag.exists():
            _flag_drop(reload_flag)
            _flag_drop(pending)
            # session_id (Retrieval V1 compact-reload parity): rehydrates this session's
            # pre-compaction working set, mirroring Claude's SessionStart(compact) path.
            payload = store.post_compact_payload(repo, session_id)
            contexts.extend(part for part in (payload.get("status"), payload.get("context")) if part)
        elif pending.exists():
            _flag_drop(pending)
            contexts.append(_REMINDER)

        # A decision awaiting the developer's review — independent of the reload/edit reminders
        # (a reload re-injects get_context, which EXCLUDES pending decisions, so the nudge must
        # still fire). store.pending_review_nudge is per-repo and verifies the store still has
        # something pending, so an approved-away or cross-repo flag yields nothing.
        review = store.pending_review_nudge(repo)
        if review:
            contexts.append(review)

        # Fix 5: when no stable session identity exists, always run bootstrap
        # (idempotent - returns empty when context already stored).
        marker = _session_marker(raw)
        if marker is None or not marker.exists():
            payload = store.bootstrap_prompt_payload(repo, prompt)
            contexts.append(payload.get("context", ""))
            if marker is not None:
                _flag_set(marker)

        near: list = []
        # Provenance for the wrong-store audit. Derived from which signal `hook_cwd_repo`
        # used rather than by re-resolving: this host deliberately does NOT run the
        # `resolve_repo` chain here (see the docstring above), and a stamp must never change
        # what it is describing.
        repo_source = "hook-arg" if (repo_path or "").strip() else "hook-cwd"
        # Same store call plus the shadow-mode user_directive event (see
        # evidence.capture_directive): identical return, identical exceptions, so this
        # hook's existing outer handler still owns what happens on failure.
        entry_id, content, status = evidence.capture_directive(
            repo, prompt, session_id, "gemini_prompt", near=near, repo_source=repo_source)
        if entry_id is not None:
            contexts.append(store.constraint_ack(content, status, entry_id, near))

        rationale = store.get_context_for_prompt(repo, prompt, session_id)
        if rationale:
            contexts.append(rationale)
        return _output("BeforeAgent", contexts)
    except Exception:
        return _output("BeforeAgent", [])


def after_write(repo_path: str, raw: str) -> str:
    """AfterTool(write_file|replace): immediately remind the AI to surface and store any
    decision, AND record the edited file into the per-session sidecar (issue #175 Task 2)
    so a later capture call can propose anchor candidates without asking the model to name
    source_files itself — same signal Claude/Codex's post_write records via PostToolUse.

    The recording half is wrapped in its own try/except: a missing/garbage tool_input, or
    an unresolvable repo, must never cost the reminder this hook exists to deliver.

    Repo resolution is `hook_cwd_repo`, NOT `resolve_repo` (Greptile P1, PR #181): this
    is a hook-invoked process, not the MCP server, so `_SESSION_REPO` is always empty here
    and `resolve_repo` would fall through to the shared `.current_repo` pointer - which can
    name a DIFFERENT repo entirely. In a non-git project the installed hook's `$REPO` shell
    var is empty (see `_cmd`'s `git rev-parse --show-toplevel || true`), and non-git projects
    are first-class stores keyed by absolute path, so silently recording under whatever repo
    the pointer happens to hold (or discarding the edit if it holds nothing sane) starves the
    real project's pending captures of anchor candidates. `hook_cwd_repo` falls back to this
    process's own cwd instead — which IS the project directory for a hook — guarded by
    `is_sane_repo` so a session opened in the home/config dir still records nothing. Matches
    claude.post_write's identical fallback for the sibling PostToolUse recording path."""
    _flag_set(store.STORE_DIR / _PENDING_CAPTURE)
    try:
        repo = store.hook_cwd_repo(repo_path)
        if repo:
            data = json.loads(raw)
            tool_input = data.get("tool_input") if isinstance(data, dict) else None
            fp = tool_input.get("file_path") if isinstance(tool_input, dict) else None
            if isinstance(fp, str) and fp:
                # Shadow-mode evidence rides the SAME recorded path (record_edited_file's
                # return), so this event and Claude's post_tool_use one differ only in
                # `source`. Inside the existing handler: the reminder below is unaffected.
                relpath = store.record_edited_file(repo, fp)
                if relpath:
                    evidence.emit_hook_event(
                        repo, "file_changed",
                        session_id=store.session_from_hook_stdin(raw),
                        source="gemini_after_tool", files=[relpath])
    except Exception:
        pass
    return json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "AfterTool",
            "additionalContext": (
                "Contexer: if this edit involved an architectural, design, or engineering decision "
                "(a tech choice, a naming change, a constraint, a pattern) — tell the user "
                "what decision was made and call update_context to store it. If unsure whether it "
                "qualifies, surface it anyway and let the user confirm."
            ),
        }
    })


def _reconcile_evidence(repo_path: str) -> None:
    """Materialize recorded evidence into decisions pending review, at Gemini's own two
    checkpoint events. Fail-soft to the point of swallowing an import error, and cheap in the
    quiet case: `reconcile_session` reads no store until it has unconsumed evidence. The twin
    of `claude._reconcile_evidence`, which rides on `sync_memory` because Claude's three
    checkpoints all call it - Gemini has no such shared entrypoint, so it is wired per event."""
    try:
        from contexer import reconcile
        repo = store.hook_cwd_repo(repo_path)
        if repo:
            reconcile.reconcile_session(repo, host=NAME)
    except Exception:
        pass


def pre_compress(repo_path: str, raw: str) -> str:
    """Defer full context reload to the first turn after compression."""
    # Fix 3: only set the reload flag here. Compression is not a file write, so
    # setting _PENDING_CAPTURE would inject a misleading "you edited files last turn"
    # reminder alongside the reload. after_write owns _PENDING_CAPTURE.
    _flag_set(store.STORE_DIR / _PENDING_RELOAD)
    _reconcile_evidence(repo_path)
    return json.dumps({"suppressOutput": True})


def session_end(repo_path: str, raw: str) -> str:
    """Best-effort cleanup of the per-session first-prompt marker."""
    try:
        marker = _session_marker(raw)
        if marker is not None:
            marker.unlink(missing_ok=True)
    except Exception:
        pass
    _reconcile_evidence(repo_path)
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
        markers = _EVENT_MARKERS[event]
        current_cmd = group["hooks"][0]["command"]
        # Fix 2: strip stale hooks (e.g. after a Python path change) before checking
        # presence, otherwise _in_groups matches the old command and reinstall is a no-op.
        groups[:] = base._strip_stale(groups, markers, current_cmd)
        if not base._in_groups(groups, markers[0]):
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
    mcp_removed = bool(settings.get("mcpServers", {}).pop("contexer", None))
    if mcp_removed:
        log.append("  ✓ MCP server removed from ~/.gemini/settings.json")
    hooks = settings.get("hooks", {})
    hooks_changed = False
    if isinstance(hooks, dict):
        for event, markers in _EVENT_MARKERS.items():
            before = hooks.get(event, [])
            after = base._filter_groups(before, markers)
            if after != before:
                hooks_changed = True
                if after:
                    hooks[event] = after
                else:
                    hooks.pop(event, None)
    if hooks_changed:
        log.append("  ✓ Hooks removed from ~/.gemini/settings.json")
    # Fix 6: only write back when something actually changed, consistent with
    # claude/cursor/codex adapters which all guard _save behind a changed flag.
    if mcp_removed or hooks_changed:
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

    # Fix 4: include PreCompress — it is load-bearing for post-compression context
    # re-injection. A partial install that drops it silently breaks that mechanism.
    hooks_ok = (
        base._in_groups(groups("SessionStart"), "gemini.session_start")
        and base._in_groups(groups("BeforeAgent"), "gemini.before_agent")
        and base._in_groups(groups("AfterTool"), "gemini.after_write")
        and base._in_groups(groups("PreCompress"), "gemini.pre_compress")
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
