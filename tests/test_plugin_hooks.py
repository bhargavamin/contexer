"""The Claude plugin ships a static hooks/hooks.json. It must not drift back into the
superseded wiring (review finding C2): no pointer-poisoning fallbacks, no legacy
mcp_tool capture, and it must run the same code paths as the console-script installer."""
import json
from pathlib import Path

import pytest

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


def test_post_compact_uses_bootstrap_aware_path(plugin_hooks):
    poc = _all_commands({"PostCompact": plugin_hooks["PostCompact"]})
    assert any("get_post_compact_context" in c for c in poc)
    # The old plugin used bare get_context (no bootstrap offer) — must be gone.
    assert not any("store.get_context(" in c for c in poc)


@pytest.mark.parametrize("event", ["SessionStart", "SessionEnd", "PreCompact",
                                   "PostCompact", "PostToolUse", "UserPromptSubmit"])
def test_event_present(plugin_hooks, event):
    assert event in plugin_hooks, f"plugin missing {event} hook the adapter installs"


def test_capture_hooks_present(plugin_hooks):
    cmds = _all_commands({"UserPromptSubmit": plugin_hooks["UserPromptSubmit"]})
    assert any("capture_task" in c for c in cmds)
    assert any("capture_constraint" in c for c in cmds)
    assert any("rationale" in c for c in cmds)
