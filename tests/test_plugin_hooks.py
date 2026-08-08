"""The Claude plugin ships a static hooks/hooks.json. It must not drift back into the
superseded wiring (review finding C2): no pointer-poisoning fallbacks, no legacy
mcp_tool capture, and it must run the same code paths as the console-script installer."""
import json
import re
import sys
from pathlib import Path

import pytest

from contexer.adapters import claude

HOOKS_PATH = Path(__file__).resolve().parent.parent / "hooks" / "hooks.json"


@pytest.fixture(scope="module")
def plugin_hooks():
    data = json.loads(HOOKS_PATH.read_text())
    return data["hooks"]


def _all_commands(hooks: dict) -> list[str]:
    return [h.get("command", "") for groups in hooks.values()
            for grp in groups for h in grp.get("hooks", [])]


def test_is_valid_json():
    json.loads(HOOKS_PATH.read_text())


def test_no_pointer_poisoning_fallback(plugin_hooks):
    cmds = _all_commands(plugin_hooks)
    for c in cmds:
        assert "|| pwd" not in c
        assert '|| echo "${CLAUDE_PROJECT_DIR}"' not in c
        assert "|| echo" not in c


def test_pointer_write_is_guarded(plugin_hooks):
    # Any hook that writes .current_repo must guard on a non-empty REPO (in a git tree),
    # never write it unconditionally.
    for c in _all_commands(plugin_hooks):
        if "> ~/.contexer/.current_repo" in c or "'.current_repo'" in c:
            assert ('if [ -n "$REPO" ]' in c) or ("_is_sane_repo" in c), \
                f"unguarded .current_repo write: {c[:80]}"


def test_no_legacy_mcp_tool_capture(plugin_hooks):
    for grp in plugin_hooks.get("UserPromptSubmit", []):
        for h in grp.get("hooks", []):
            assert h.get("type") != "mcp_tool", "capture must be a command hook, not mcp_tool"


def test_session_start_syncs_memory_and_reads_source(plugin_hooks):
    ss = _all_commands({"SessionStart": plugin_hooks["SessionStart"]})
    assert any("sync_memory" in c for c in ss)
    assert any("source_from_hook_stdin" in c for c in ss)


def test_no_post_compact_hook(plugin_hooks):
    # PostCompact can't inject context (no additionalContext; systemMessage is
    # user-facing only), so wiring it only dumped visible noise on /compact.
    # SessionStart(source="compact") reloads silently — the plugin wires no PostCompact.
    assert "PostCompact" not in plugin_hooks


@pytest.mark.parametrize("event", ["SessionStart", "SessionEnd", "PreCompact",
                                   "PostToolUse", "UserPromptSubmit"])
def test_event_present(plugin_hooks, event):
    assert event in plugin_hooks, f"plugin missing {event} hook the adapter installs"


def test_capture_hooks_present(plugin_hooks):
    cmds = _all_commands({"UserPromptSubmit": plugin_hooks["UserPromptSubmit"]})
    assert any("capture_constraint" in c for c in cmds)
    assert any("rationale" in c for c in cmds)


# ── Command-content parity (issue #179) ─────────────────────────────────────────
# Event presence alone (test_event_present above) is exactly how this rotted twice:
# the bundle can carry a SessionStart/PostToolUse/etc. hook that "exists" while its
# command text is stale (pre-#152 unguarded forms) or missing an entrypoint entirely
# (post_write, team_poll, review_nudge). This section drives the REAL claude.install()
# into an isolated temp home and diffs its generated command strings against the
# bundle's, one hook at a time, so drift in command CONTENT fails loudly.
#
# Two differences are legitimate and documented in hooks/hooks.json's _comment:
#   1. invocation: the plugin runs `uv run --directory "${CLAUDE_PLUGIN_ROOT}" python`
#      (durable plugin dir) where the installer uses `"{sys.executable}"` directly.
#   2. sentinel: every installer-generated command carries a trailing
#      `# contexer-managed-hook` comment (so `install()`/`uninstall()` can recognize
#      and migrate their own previously-written hooks on reinstall) — the static
#      bundle never self-migrates, so it carries no sentinel.
# Both are normalized away below before comparing; anything else that differs is drift.
_SENTINEL_RE = re.compile(r" # contexer-managed-hook.*$")


def _normalize_adapter_command(cmd: str, python: str) -> str:
    """installer-generated command -> the form the plugin bundle should carry."""
    cmd = cmd.replace(f'"{python}" -c', 'uv run --directory "${CLAUDE_PLUGIN_ROOT}" python -c')
    return _SENTINEL_RE.sub("", cmd)


@pytest.fixture
def adapter_hooks(tmp_path):
    """{event: [command, ...]} exactly as claude.install() generates today — the
    source of truth the bundle must mirror. Driven into an isolated temp home so
    this never touches the real ~/.claude config."""
    claude.install(tmp_path)
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    return {
        event: [h["command"] for grp in groups for h in grp.get("hooks", []) if "command" in h]
        for event, groups in settings["hooks"].items()
    }


def _plugin_command_for(plugin_hooks: dict, event: str, marker: str) -> str:
    for grp in plugin_hooks.get(event, []):
        for h in grp.get("hooks", []):
            if marker in h.get("command", ""):
                return h["command"]
    raise AssertionError(f"plugin bundle has no {event} hook matching {marker!r}")


# (event, substring identifying one hook within that event) for every hook the
# adapter installs today. A marker present here but missing from the bundle means
# the plugin needs the hook (or a reconciled equivalent); a bundle hook matching no
# marker here means the adapter no longer installs it and the bundle should drop it
# too (see test_bundle_carries_no_hooks_the_adapter_no_longer_installs).
_ADAPTER_HOOK_MARKERS = [
    ("SessionStart", "get_session_start_context"),
    ("SessionEnd", "sync_memory"),
    ("PostToolUse", "claude.post_write"),
    ("PostToolUse", "plan approved"),
    ("PreCompact", "compaction starting"),
    ("UserPromptSubmit", ".pending_capture"),
    ("UserPromptSubmit", "get_bootstrap_context_prompt"),
    ("UserPromptSubmit", "claude.capture_constraint"),
    ("UserPromptSubmit", "claude.rationale"),
    ("UserPromptSubmit", "claude.team_poll"),
    ("UserPromptSubmit", "claude.review_nudge"),
]


@pytest.mark.parametrize("event,marker", _ADAPTER_HOOK_MARKERS)
def test_command_matches_adapter(adapter_hooks, plugin_hooks, event, marker):
    adapter_cmd = next((c for c in adapter_hooks.get(event, []) if marker in c), None)
    assert adapter_cmd is not None, (
        f"adapter no longer installs a {event} hook matching {marker!r} — "
        "update _ADAPTER_HOOK_MARKERS")
    plugin_cmd = _plugin_command_for(plugin_hooks, event, marker)
    assert _normalize_adapter_command(adapter_cmd, sys.executable) == plugin_cmd, (
        f"{event} hook ({marker}) has drifted from the adapter-generated command")


def test_bundle_carries_no_hooks_the_adapter_no_longer_installs(plugin_hooks):
    """Inverse of test_command_matches_adapter: every command in the bundle must
    match one of the known adapter markers, so a hook the adapter has since dropped
    (or renamed) can't linger in the bundle unnoticed."""
    known_markers = [marker for _, marker in _ADAPTER_HOOK_MARKERS]
    for event, groups in plugin_hooks.items():
        for grp in groups:
            for h in grp.get("hooks", []):
                cmd = h.get("command", "")
                assert any(marker in cmd for marker in known_markers), (
                    f"{event} hook has no matching adapter marker — "
                    f"orphaned or needs a new entry in _ADAPTER_HOOK_MARKERS: {cmd[:80]}")
