"""The Claude plugin ships a static hooks/hooks.json. It must not drift back into the
superseded wiring (review finding C2): no pointer-poisoning fallbacks, no legacy
mcp_tool capture, and it must run the same code paths as the console-script installer."""
import json
import os
import re
import sys
from pathlib import Path

import pytest

from contexer import store
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
# into an isolated temp home and diffs its generated command/matcher/once against the
# bundle's, one hook at a time, so drift in ANY of those three fields fails loudly —
# in both directions: a marker missing from the adapter (test_command_matches_adapter)
# and an adapter hook / bundle hook missing a marker (the two orphan tests below).
#
# Two differences are legitimate and documented in hooks/hooks.json's _comment:
#   1. invocation: the plugin runs `uv run --directory "${CLAUDE_PLUGIN_ROOT}" python`
#      (durable plugin dir) where the installer uses `"{sys.executable}"` directly.
#   2. sentinel: every installer-generated command carries a trailing
#      `# contexer-managed-hook` comment (so `install()`/`uninstall()` can recognize
#      and migrate their own previously-written hooks on reinstall) — the static
#      bundle never self-migrates, so it carries no sentinel.
# Both are normalized away below before comparing; anything else that differs is drift.
# Normalization applies to `command` only — `matcher` and `once` are compared as-is.
_SENTINEL_RE = re.compile(r" # contexer-managed-hook.*$")


def _normalize_adapter_command(cmd: str, python: str) -> str:
    """installer-generated command -> the form the plugin bundle should carry."""
    cmd = cmd.replace(f'"{python}" -c', 'uv run --directory "${CLAUDE_PLUGIN_ROOT}" python -c')
    return _SENTINEL_RE.sub("", cmd)


@pytest.fixture
def adapter_hooks(tmp_path, monkeypatch):
    """[(event, command, matcher, once), ...] exactly as claude.install() generates
    today — the source of truth the bundle must mirror. Driven into an isolated temp
    home so this never touches the real ~/.claude config.

    Isolating the home dir alone is NOT enough: claude.install() also runs
    clean_legacy_repo_settings against store.git_root(os.getcwd()) - the PROCESS cwd's
    git root, not the injected home — to clean up a pre-CLI installer's repo-level
    hooks. Left unpatched, a test run from a checkout whose <repo>/.claude/settings.json
    still carries legacy Contexer hook markers would get silently rewritten (the same
    class of test-state escaping the ui.log leak fixture above exists to catch, just for
    a different real file). monkeypatch.chdir(tmp_path) contains that structurally: cwd's
    git root becomes tmp_path (untracked, no .claude/settings.json), so
    clean_legacy_repo_settings has nothing of ours to touch. The explicit byte-identical
    assertion below is belt-and-suspenders — the session's leak-guard fixture
    (no_real_store_writes) only watches ~/.contexer, not <repo>/.claude/settings.json."""
    real_repo = store.git_root(os.getcwd())
    real_settings = Path(real_repo) / ".claude" / "settings.json" if real_repo else None
    before = real_settings.read_bytes() if real_settings and real_settings.is_file() else None

    monkeypatch.chdir(tmp_path)
    claude.install(tmp_path)

    if real_settings is not None:
        after = real_settings.read_bytes() if real_settings.is_file() else None
        assert after == before, (
            f"claude.install() must never touch the real {real_settings} — "
            "cwd isolation leaked")

    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    return [
        (event, h["command"], grp.get("matcher"), h.get("once", False))
        for event, groups in settings["hooks"].items()
        for grp in groups
        for h in grp.get("hooks", [])
        if "command" in h
    ]


def _one_adapter_hook(adapter_hooks: list, event: str, marker: str) -> tuple:
    """(command, matcher, once) for the single adapter hook matching (event, marker).
    Asserts exactly one match — zero means the adapter no longer installs this hook
    (or the marker is stale), more than one means the marker is ambiguous and must
    be tightened, either way silently comparing against the wrong hook is worse
    than failing loudly here."""
    matches = [(cmd, matcher, once) for ev, cmd, matcher, once in adapter_hooks
               if ev == event and marker in cmd]
    assert len(matches) == 1, (
        f"expected exactly one adapter {event} hook matching {marker!r}, found "
        f"{len(matches)} — 0 means the adapter no longer installs this hook (update "
        "_ADAPTER_HOOK_MARKERS), >1 means the marker is ambiguous (pick a more "
        "specific substring)")
    return matches[0]


def _one_plugin_hook(plugin_hooks: dict, event: str, marker: str) -> tuple:
    """(command, matcher, once) for the single bundle hook matching (event, marker).
    Same uniqueness guarantee as _one_adapter_hook."""
    matches = [
        (h["command"], grp.get("matcher"), h.get("once", False))
        for grp in plugin_hooks.get(event, [])
        for h in grp.get("hooks", [])
        if marker in h.get("command", "")
    ]
    assert len(matches) == 1, (
        f"expected exactly one plugin {event} hook matching {marker!r}, found "
        f"{len(matches)} — 0 means the bundle is missing this hook, >1 means the "
        "marker is ambiguous (pick a more specific substring)")
    return matches[0]


# (event, substring identifying one hook within that event) for every hook the
# adapter installs today. A marker present here but missing from the bundle means
# the plugin needs the hook (or a reconciled equivalent); a bundle hook matching no
# marker here means the adapter no longer installs it and the bundle should drop it
# too (see test_bundle_carries_no_hooks_the_adapter_no_longer_installs). An adapter
# hook matching no marker here means _ADAPTER_HOOK_MARKERS itself is stale (see
# test_adapter_markers_cover_every_installed_hook) — this list must stay exhaustive
# in BOTH directions, or a new adapter hook could ship with no bundle coverage and
# every check here would stay green.
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
    adapter_cmd, adapter_matcher, adapter_once = _one_adapter_hook(adapter_hooks, event, marker)
    plugin_cmd, plugin_matcher, plugin_once = _one_plugin_hook(plugin_hooks, event, marker)
    assert _normalize_adapter_command(adapter_cmd, sys.executable) == plugin_cmd, (
        f"{event} hook ({marker}) command has drifted from the adapter-generated command")
    assert adapter_matcher == plugin_matcher, (
        f"{event} hook ({marker}) matcher drifted: "
        f"adapter={adapter_matcher!r} bundle={plugin_matcher!r}")
    assert adapter_once == plugin_once, (
        f"{event} hook ({marker}) 'once' flag drifted: "
        f"adapter={adapter_once!r} bundle={plugin_once!r}")


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


def test_adapter_markers_cover_every_installed_hook(adapter_hooks):
    """Mirror of the test above, in the other direction: every hook claude.install()
    generates today must be covered by _ADAPTER_HOOK_MARKERS. Without this, a new
    hook added to install() with no marker row (and no bundle entry) would pass
    every other check here silently — exactly the class of drift this file exists
    to catch, just introduced from the adapter side instead of the bundle side."""
    for event, cmd, _matcher, _once in adapter_hooks:
        assert any(ev == event and marker in cmd for ev, marker in _ADAPTER_HOOK_MARKERS), (
            f"{event} hook has no _ADAPTER_HOOK_MARKERS entry — add a marker row here "
            f"AND the corresponding hooks/hooks.json bundle entry: {cmd[:80]}")
