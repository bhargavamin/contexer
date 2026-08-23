"""Tests for the Codex adapter install/uninstall."""
import json
import sys
import tomllib
from pathlib import Path

import pytest

from contexer.adapters import base, codex


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _config(home: Path) -> str:
    return (home / ".codex" / "config.toml").read_text()


def _hooks(home: Path) -> dict:
    return json.loads((home / ".codex" / "hooks.json").read_text())


class TestCodexInstall:
    def test_registers_mcp_in_config_toml(self, home):
        codex.install(home)
        data = tomllib.loads(_config(home))
        assert "contexer" in data["mcp_servers"]["contexer"]["command"]

    def test_config_toml_is_valid_toml(self, home):
        codex.install(home)
        tomllib.loads(_config(home))  # must not raise

    def test_session_start_hook_loads_context(self, home):
        codex.install(home)
        cmds = [g["hooks"][0]["command"] for g in _hooks(home)["hooks"]["SessionStart"]]
        assert any("get_session_start_context" in c for c in cmds)

    def test_session_start_hook_threads_session_id(self, home):
        # Mirrors claude.py's ss_code: reads stdin once, passes both source and session id
        # so compact-source working-set rehydration works for Codex too.
        codex.install(home)
        cmds = [g["hooks"][0]["command"] for g in _hooks(home)["hooks"]["SessionStart"]]
        assert any("session_from_hook_stdin" in c for c in cmds)

    def test_migrates_stale_session_start_to_thread_session_id(self, home):
        # An older install has pull_team but predates session-id threading — must still be
        # replaced on reinstall so the current ss_code (session-id-aware) is installed.
        hooks_path = home / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        old = (
            'py -c "from contexer import store; from contexer.adapters import claude as _c; '
            'import json,sys; repo=sys.argv[1]; _c.pull_team(repo); '
            'print(json.dumps(store.get_session_start_context(repo, '
            'store.source_from_hook_stdin(sys.stdin.read()))))" "$REPO"'
        )
        hooks_path.write_text(json.dumps({"hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": old}]}]}}))
        codex.install(home)
        ss = _hooks(home)["hooks"]["SessionStart"]
        cmds = [g["hooks"][0]["command"] for g in ss]
        assert len(ss) == 1  # replaced in place, not duplicated
        assert any("session_from_hook_stdin" in c for c in cmds)
        assert any("pull_team" in c for c in cmds)

    def test_post_compact_hook_threads_session_id(self, home):
        codex.install(home)
        cmds = [h["command"] for g in _hooks(home)["hooks"]["PostCompact"] for h in g["hooks"]]
        assert any("session_from_hook_stdin" in c for c in cmds)

    def test_migrates_stale_post_compact_to_thread_session_id(self, home):
        hooks_path = home / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        old = 'py -c "print(store.get_post_compact_context(sys.argv[1]))" "$REPO"'
        hooks_path.write_text(json.dumps({"hooks": {"PostCompact": [
            {"hooks": [{"type": "command", "command": old}]}]}}))
        codex.install(home)
        poc = _hooks(home)["hooks"]["PostCompact"]
        cmds = [h["command"] for g in poc for h in g["hooks"]]
        assert len(poc) == 1  # replaced in place, not duplicated
        assert any("session_from_hook_stdin" in c for c in cmds)

    def test_full_hook_event_set_wired(self, home):
        codex.install(home)
        hooks = _hooks(home)["hooks"]
        assert set(hooks) >= {"SessionStart", "PostToolUse", "PreCompact",
                              "PostCompact", "UserPromptSubmit"}

    def test_user_prompt_submit_capture_hooks(self, home):
        codex.install(home)
        cmds = [h["command"] for g in _hooks(home)["hooks"]["UserPromptSubmit"]
                for h in g["hooks"]]
        joined = "\n".join(cmds)
        for marker in ("get_bootstrap_context_prompt",
                       "claude.capture_constraint", "claude.rationale", ".pending_capture"):
            assert marker in joined

    def test_pending_review_hook_registered(self, home):
        codex.install(home)
        cmds = [h["command"] for g in _hooks(home)["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
        assert any("claude.review_nudge" in c for c in cmds)

    def test_pending_review_in_event_markers(self):
        assert "claude.review_nudge" in codex._EVENT_MARKERS["UserPromptSubmit"]

    def test_uninstall_removes_pending_review(self, home):
        codex.install(home)
        codex.uninstall(home)
        ups = _hooks(home).get("hooks", {}).get("UserPromptSubmit", [])
        cmds = [h.get("command", "") for g in ups for h in g.get("hooks", [])]
        assert not any("claude.review_nudge" in c for c in cmds)

    # ── T2: team sync ────────────────────────────────────────────────────────────

    def test_session_start_pulls_team(self, home):
        codex.install(home)
        cmds = [g["hooks"][0]["command"] for g in _hooks(home)["hooks"]["SessionStart"]]
        assert any("pull_team" in c for c in cmds)  # team cache refreshed at session start

    def test_user_prompt_submit_wires_team_poll(self, home):
        codex.install(home)
        cmds = [h["command"] for g in _hooks(home)["hooks"]["UserPromptSubmit"]
                for h in g["hooks"]]
        assert any("claude.team_poll" in c for c in cmds)  # per-prompt delta injection

    def test_team_poll_wired_once(self, home):
        codex.install(home)
        codex.install(home)
        cmds = [h["command"] for g in _hooks(home)["hooks"]["UserPromptSubmit"]
                for h in g["hooks"]]
        assert sum("claude.team_poll" in c for c in cmds) == 1

    def test_team_poll_hook_tags_codex_consumer(self, home):
        codex.install(home)
        cmds = [h["command"] for g in _hooks(home)["hooks"]["UserPromptSubmit"]
                for h in g["hooks"]]
        poll = next(c for c in cmds if "claude.team_poll" in c)
        assert "'codex'" in poll  # per-consumer tag so codex isn't starved by a claude session

    def test_migrates_old_untagged_team_poll_hook(self, home):
        # A pre-consumer install: the codex team-poll hook reused claude.team_poll WITHOUT the
        # "codex" tag, so it raced a concurrent Claude session for a single delivery. Reinstall
        # must replace it in place with the tagged call.
        hooks_path = home / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        old = ('REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd) && '
               '"py" -c "from contexer.adapters import claude; import sys; '
               'print(claude.team_poll(sys.argv[1], sys.stdin.read()))" "$REPO"')
        hooks_path.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": old}]}]}}))
        codex.install(home)
        cmds = [h["command"] for g in _hooks(home)["hooks"]["UserPromptSubmit"]
                for h in g["hooks"]]
        polls = [c for c in cmds if "claude.team_poll" in c]
        assert len(polls) == 1  # replaced in place, not duplicated
        assert "'codex'" in polls[0]  # now tagged

    def test_migrates_untagged_team_poll_despite_foreign_codex_substring(self, home):
        # The migration guard is keyed on the QUOTED 'codex' marker, not a bare "codex"
        # substring. An unrelated foreign hook whose command merely mentions "codex"
        # (e.g. a path) must not be mistaken for the tagged call and suppress migration.
        hooks_path = home / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        old = ('REPO=$(git rev-parse --show-toplevel 2>/dev/null || pwd) && '
               '"py" -c "from contexer.adapters import claude; import sys; '
               'print(claude.team_poll(sys.argv[1], sys.stdin.read()))" "$REPO"')
        foreign = "/usr/local/codex-tools/run.sh"  # bare "codex" substring, unquoted
        hooks_path.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": old}]},
            {"hooks": [{"type": "command", "command": foreign}]},
        ]}}))
        codex.install(home)
        cmds = [h["command"] for g in _hooks(home)["hooks"]["UserPromptSubmit"]
                for h in g["hooks"]]
        polls = [c for c in cmds if "claude.team_poll" in c]
        assert len(polls) == 1  # migration still fired, not suppressed by the foreign hook
        assert "'codex'" in polls[0]  # now tagged
        assert any(foreign in c for c in cmds)  # unrelated foreign hook left untouched

    def test_tagged_team_poll_hook_not_re_migrated(self, home):
        codex.install(home)
        codex.install(home)  # a reinstall over the already-tagged hook must be a no-op
        cmds = [h["command"] for g in _hooks(home)["hooks"]["UserPromptSubmit"]
                for h in g["hooks"]]
        assert sum("'codex'" in c for c in cmds) == 1

    def test_session_start_pull_team_wired_once(self, home):
        codex.install(home)
        codex.install(home)
        assert len(_hooks(home)["hooks"]["SessionStart"]) == 1  # not duplicated on reinstall

    def test_migrates_stale_session_start_to_add_team_pull(self, home):
        # An older install: SessionStart loads context but has NO team pull. Reinstall must
        # replace it so team context refreshes at session start.
        hooks_path = home / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        hooks_path.write_text(json.dumps({"hooks": {"SessionStart": [
            {"hooks": [{"type": "command",
                        "command": 'py -c "store.get_session_start_context(repo)" "$REPO"'}]}]}}))
        codex.install(home)
        ss = _hooks(home)["hooks"]["SessionStart"]
        cmds = [g["hooks"][0]["command"] for g in ss]
        assert len(ss) == 1  # replaced in place, not duplicated
        assert any("pull_team" in c for c in cmds)
        assert any("get_session_start_context" in c for c in cmds)

    def test_post_tool_use_matches_write_edit(self, home):
        codex.install(home)
        put = _hooks(home)["hooks"]["PostToolUse"]
        assert any(g.get("matcher") == "Write|Edit" for g in put)

    def test_no_stop_hook_installed(self, home):
        codex.install(home)
        stop = _hooks(home)["hooks"].get("Stop", [])
        cmds = [h.get("command", "") for g in stop for h in g.get("hooks", [])]
        assert not any(".pending_capture" in c for c in cmds)

    def test_install_removes_preexisting_contexer_stop_hook(self, home):
        hooks_path = home / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        hooks_path.write_text(json.dumps({"hooks": {"Stop": [
            {"hooks": [{"type": "command",
                        "command": "rm -f $HOME/.contexer/.pending_capture; echo '{}'"}]}]}}))
        codex.install(home)
        stop = _hooks(home)["hooks"].get("Stop", [])
        cmds = [h.get("command", "") for g in stop for h in g.get("hooks", [])]
        assert not any(".pending_capture" in c for c in cmds)

    def test_install_preserves_foreign_stop_hook(self, home):
        hooks_path = home / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        hooks_path.write_text(json.dumps({"hooks": {"Stop": [
            {"hooks": [{"type": "command", "command": "./mine.sh"}]}]}}))
        codex.install(home)
        cmds = [h.get("command", "") for g in _hooks(home)["hooks"]["Stop"]
                for h in g.get("hooks", [])]
        assert "./mine.sh" in cmds

    def test_uses_current_python(self, home):
        codex.install(home)
        cmds = [g["hooks"][0]["command"] for g in _hooks(home)["hooks"]["SessionStart"]]
        assert any(sys.executable in c for c in cmds)

    def test_install_idempotent(self, home):
        codex.install(home)
        codex.install(home)
        cmds = [h["command"] for g in _hooks(home)["hooks"]["UserPromptSubmit"]
                for h in g["hooks"]]
        assert sum("claude.capture_constraint" in c for c in cmds) == 1
        # the stanza must appear exactly once too
        assert _config(home).count("[mcp_servers.contexer]") == 1

    def test_does_not_pre_approve_tools(self, home):
        log = codex.install(home)
        assert any("approve" in line.lower() for line in log)

    def test_preserves_existing_config_toml_byte_for_byte(self, home):
        cfg = home / ".codex" / "config.toml"
        cfg.parent.mkdir(parents=True)
        original = (
            "# my codex config\n"
            "model = \"gpt-5\"\n\n"
            "[mcp_servers.other]\n"
            "command = \"/usr/bin/other\"\n\n"
            "[mcp_servers.other.env]\n"
            "SECRET = \"hunter2\"\n"
        )
        cfg.write_text(original)
        codex.install(home)
        text = _config(home)
        # everything the user had is still present, untouched
        assert original in text
        assert "[mcp_servers.contexer]" in text
        # parses and both servers are visible
        data = tomllib.loads(text)
        assert data["mcp_servers"]["other"]["env"]["SECRET"] == "hunter2"
        assert "contexer" in data["mcp_servers"]["contexer"]["command"]

    def test_replaces_existing_contexer_stanza_in_place(self, home):
        cfg = home / ".codex" / "config.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("[mcp_servers.contexer]\ncommand = \"/old/path\"\n\n[other]\nx = 1\n")
        codex.install(home)
        text = _config(home)
        assert text.count("[mcp_servers.contexer]") == 1
        assert "/old/path" not in text
        assert tomllib.loads(text)["other"]["x"] == 1

    def test_refuses_to_touch_invalid_config_toml(self, home):
        cfg = home / ".codex" / "config.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("this is = = not valid toml [[[")
        log = codex.install(home)
        assert cfg.read_text() == "this is = = not valid toml [[["  # untouched
        assert any("not valid TOML" in line for line in log)


class TestCodexBookkeepingWritesAreFailSoft:
    """#152 was reported against Codex specifically: its managed sandbox leaves the
    workspace writable while ~/.contexer may not be, so the SessionStart hook's
    best-effort `.current_repo` write raised PermissionError and aborted the whole hook —
    Contexer looked broken over a pointer file it did not need to read context."""

    def _cmds(self, home: Path, event: str) -> list[str]:
        return [h.get("command", "") for grp in _hooks(home)["hooks"][event]
                for h in grp.get("hooks", [])]

    def test_session_start_anchors_via_fail_soft_helper(self, home):
        codex.install(home)
        cmds = self._cmds(home, "SessionStart")
        assert any("store.anchor_repo(repo)" in c for c in cmds)
        assert not any(".current_repo').write_text" in c for c in cmds)

    def test_post_tool_use_is_the_python_post_write_entrypoint(self, home):
        # #175 Task 2: reuses claude.post_write verbatim — it records edited files and
        # touches .pending_capture itself, best-effort inside a Python try/except, not a
        # shelled-out `touch`.
        codex.install(home)
        cmds = self._cmds(home, "PostToolUse")
        post_write = next(c for c in cmds if "claude.post_write" in c)
        assert "touch" not in post_write
        assert ".pending_capture" in post_write

    def test_anchor_hook_guards_the_pointer_write_and_the_flag_removal(self, home):
        codex.install(home)
        anchor = next(c for c in self._cmds(home, "UserPromptSubmit") if ".current_repo" in c)
        assert "{ printf '%s' \"$REPO\" > ~/.contexer/.current_repo; } 2>/dev/null || true" in anchor
        assert 'rm -f "$FLAG" 2>/dev/null || true' in anchor

    def test_reinstall_replaces_the_unguarded_session_start_hook(self, home):
        # Verbatim shape of the hook that failed in the report.
        hooks_path = home / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        old = ('py -c "from contexer import store; from contexer.adapters import claude as _c; '
               'import json,sys; repo=sys.argv[1]; raw=sys.stdin.read(); '
               "store.STORE_DIR.mkdir(exist_ok=True); "
               "store.is_sane_repo(repo) and (store.STORE_DIR/'.current_repo').write_text(repo); "
               '_c.pull_team(repo); print(json.dumps(store.get_session_start_context(repo, '
               'store.source_from_hook_stdin(raw), store.session_from_hook_stdin(raw))))" "$REPO"')
        hooks_path.write_text(json.dumps({"hooks": {"SessionStart": [
            {"hooks": [{"type": "command", "command": old}]}]}}))
        codex.install(home)
        cmds = self._cmds(home, "SessionStart")
        assert not any(".current_repo').write_text" in c for c in cmds)
        assert any("store.anchor_repo(repo)" in c for c in cmds)

    def test_reinstall_replaces_the_unguarded_post_tool_use_hook(self, home):
        hooks_path = home / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        hooks_path.write_text(json.dumps({"hooks": {"PostToolUse": [
            {"matcher": "Write|Edit", "hooks": [{"type": "command",
             "command": "touch ~/.contexer/.pending_capture && echo '{}'"}]}]}}))
        codex.install(home)
        cmds = self._cmds(home, "PostToolUse")
        assert not any("touch ~/.contexer" in c for c in cmds), \
            "the old shell touch must not survive a reinstall"
        assert sum("claude.post_write" in c for c in cmds) == 1, "must replace, not duplicate"

    def test_reinstall_replaces_a_post_write_hook_missing_git_toplevel_resolution(self, home):
        hooks_path = home / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        old = ('"python3" -c "from contexer.adapters import claude; import sys; '
               'print(claude.post_write(sys.argv[1], sys.stdin.read()))" "$(pwd)" '
               '# .pending_capture')
        hooks_path.write_text(json.dumps({"hooks": {"PostToolUse": [
            {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": old}]}]}}))
        codex.install(home)
        cmds = self._cmds(home, "PostToolUse")
        post_write_cmds = [c for c in cmds if "claude.post_write" in c]
        assert len(post_write_cmds) == 1, "must replace, not duplicate"
        assert "show-toplevel" in post_write_cmds[0]

    def test_reinstall_replaces_the_unguarded_anchor_hook(self, home):
        hooks_path = home / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        old = ('REPO=$(git rev-parse --show-toplevel 2>/dev/null || true); '
               'if [ -n "$REPO" ]; then printf \'%s\' "$REPO" > ~/.contexer/.current_repo; fi; '
               'FLAG="$HOME/.contexer/.pending_capture"; if [ -f "$FLAG" ]; then rm -f "$FLAG"; '
               'echo \'{"x": "last turn settled"}\'; else echo \'{}\'; fi')
        hooks_path.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": old}]}]}}))
        codex.install(home)
        anchors = [c for c in self._cmds(home, "UserPromptSubmit") if ".current_repo" in c]
        assert len(anchors) == 1, "must replace, not duplicate"
        assert 'rm -f "$FLAG" 2>/dev/null || true' in anchors[0]

    def test_reinstall_is_idempotent(self, home):
        # Every migration gate must recognize its own output — otherwise install strips
        # and re-adds hooks forever. This is the property that catches a gate keyed on a
        # marker it can never match (see TestCodexQuotedMarkerGate).
        codex.install(home)
        before = _hooks(home)
        codex.install(home)
        assert _hooks(home) == before


class TestCodexQuotedMarkerGate:
    """The team-poll migration gate is keyed on the QUOTED marker `'codex'`. It used
    `_in_groups`, which matches a dict *repr* — where the quotes come back escaped as
    \\'codex\\' — so the gate never recognized the very hook it had just installed and
    re-fired on every install, stripping and re-appending the group. Functionally
    harmless (the tagged hook was always what ended up installed) but it meant no Codex
    install was ever idempotent, and the same trap waits for any future quoted marker."""

    def _ups_cmds(self, home: Path) -> list[str]:
        return [h.get("command", "") for grp in _hooks(home)["hooks"]["UserPromptSubmit"]
                for h in grp.get("hooks", [])]

    def test_in_groups_cannot_match_a_quoted_marker(self):
        # The root cause, pinned directly: this is why the gate needs _in_commands.
        # Note the `"` in the command — that is what makes the bug bite. Python's repr
        # only escapes `'` when it cannot use `"` as the delimiter, so a command with
        # single quotes ALONE reprs them untouched and _in_groups appears to work. Every
        # real hook command is `py -c "..."`, i.e. holds both quote kinds, which flips
        # repr to `'`-delimited and escapes the marker out of existence.
        def _grp(cmd):
            return [{"hooks": [{"type": "command", "command": cmd}]}]

        real = """py -c "print(team_poll(x, 'codex'))" "$REPO\""""
        assert not base._in_groups(_grp(real), "'codex'"), "repr escaping — the bug"
        assert base._in_commands(_grp(real), "'codex'"), "raw command match — the fix"
        # Same marker, no double quote in the command: _in_groups happens to work, which
        # is exactly why this went unnoticed. _in_commands is correct in both cases.
        naive = "print(team_poll(x, 'codex'))"
        assert base._in_groups(_grp(naive), "'codex'")
        assert base._in_commands(_grp(naive), "'codex'")

    def test_in_commands_tolerates_hand_edited_hook_shapes(self):
        # _hooks_of is documented as tolerating hand-edited configs, and _in_groups did
        # via str(h). _in_commands must not be the one place a foreign or hand-written
        # group with a null/list command turns `contexer install` into a TypeError.
        for bad in (None, [], 42, {"a": 1}):
            groups = [{"hooks": [{"type": "command", "command": bad}]}]
            assert base._in_commands(groups, "'codex'") is False
        assert base._in_commands([{"hooks": [{"type": "command"}]}], "x") is False
        assert base._in_commands([{"hooks": ["not-a-dict"]}], "x") is False

    def test_in_commands_still_distinguishes_the_untagged_call(self):
        # The quoting is load-bearing: a bare "codex" check would false-positive on any
        # command merely mentioning codex (a path like /opt/codex-tools/bin), suppressing
        # a migration that should run.
        untagged = [{"hooks": [{"type": "command",
                                "command": "/opt/codex-tools/py -c print(team_poll(x))"}]}]
        assert not base._in_commands(untagged, "'codex'")

    def test_team_poll_hook_is_stable_across_reinstalls(self, home):
        codex.install(home)
        before = self._ups_cmds(home)
        codex.install(home)
        assert self._ups_cmds(home) == before, "the gate must not reorder its own hook"

    def test_untagged_team_poll_is_still_migrated(self, home):
        # The gate must keep doing its actual job: a pre-consumer install called
        # claude.team_poll with no consumer tag, so Claude and Codex sessions on one repo
        # raced for a single delivery. That hook must still be replaced.
        hooks_path = home / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        old = ('py -c "from contexer.adapters import claude; import sys; '
               'print(claude.team_poll(sys.argv[1], sys.stdin.read()))" "$REPO"')
        hooks_path.write_text(json.dumps({"hooks": {"UserPromptSubmit": [
            {"hooks": [{"type": "command", "command": old}]}]}}))
        codex.install(home)
        polls = [c for c in self._ups_cmds(home) if "claude.team_poll" in c]
        assert len(polls) == 1, "must replace, not duplicate"
        assert "'codex'" in polls[0], "the replacement must carry the consumer tag"


class TestCodexUninstall:
    def test_removes_mcp_stanza_only(self, home):
        cfg = home / ".codex" / "config.toml"
        cfg.parent.mkdir(parents=True)
        cfg.write_text("[mcp_servers.other]\ncommand = \"x\"\n")
        codex.install(home)
        codex.uninstall(home)
        text = _config(home)
        assert "[mcp_servers.contexer]" not in text
        assert tomllib.loads(text)["mcp_servers"]["other"]["command"] == "x"

    def test_removes_contexer_hooks_keeps_user_hooks(self, home):
        hooks_path = home / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        hooks_path.write_text(json.dumps(
            {"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "./mine.sh"}]}]}}))
        codex.install(home)
        codex.uninstall(home)
        hooks = _hooks(home)["hooks"]
        assert hooks["Stop"][0]["hooks"][0]["command"] == "./mine.sh"
        assert not hooks.get("SessionStart")

    def test_uninstall_idempotent(self, home):
        codex.install(home)
        codex.uninstall(home)
        codex.uninstall(home)  # must not raise

    def test_uninstall_removes_team_poll(self, home):
        codex.install(home)
        codex.uninstall(home)
        ups = _hooks(home)["hooks"].get("UserPromptSubmit", [])
        cmds = [h.get("command", "") for g in ups for h in g.get("hooks", [])]
        assert not any("claude.team_poll" in c for c in cmds)


class TestCodexPostWriteRepoResolutionParity:
    """issue #175 Task 2: Codex reuses claude.post_write VERBATIM, and its $REPO
    resolution must be copied character-for-character from claude.py's own post_write_cmd —
    not from this file's usual `|| pwd` sibling fallback. A cwd-vs-toplevel mismatch here
    would silently key record_edited_file's write under a different sidecar slug than
    Task 3's capture-time read (the doc-drift hazard)."""

    def test_prefix_matches_claude_post_write_verbatim(self, home, monkeypatch):
        from contexer import store
        from contexer.adapters import claude as claude_adapter

        codex.install(home)
        cmds = [h["command"] for g in _hooks(home)["hooks"]["PostToolUse"] for h in g["hooks"]]
        codex_post_write = next(c for c in cmds if "claude.post_write" in c)
        codex_prefix = codex_post_write.split("&&")[0] + "&&"

        # claude.py's own post_write_cmd is generated the same way — reconstruct it via a
        # real claude.install() and compare prefixes rather than duplicating the literal.
        # claude_adapter.install() also runs clean_legacy_repo_settings against
        # store.git_root(os.getcwd()) — the PROCESS cwd's git root, unaffected by
        # claude_home — so chdir into an untracked temp dir before calling it, exactly
        # like the parity fixtures in test_plugin_hooks.py / test_adapters.py; otherwise
        # this could silently rewrite the real checkout's <repo>/.claude/settings.json.
        claude_home = home.parent / "claude_home_for_parity"
        (claude_home / ".claude").mkdir(parents=True)

        real_repo = store.git_root(str(Path.cwd()))
        real_settings = Path(real_repo) / ".claude" / "settings.json" if real_repo else None
        before = real_settings.read_bytes() if real_settings and real_settings.is_file() else None

        monkeypatch.chdir(home)
        claude_adapter.install(claude_home)

        if real_settings is not None:
            after = real_settings.read_bytes() if real_settings.is_file() else None
            assert after == before, (
                f"claude.install() must never touch the real {real_settings} — "
                "cwd isolation leaked")
        claude_settings = json.loads((claude_home / ".claude" / "settings.json").read_text())
        claude_cmds = [h["command"] for g in claude_settings["hooks"]["PostToolUse"]
                       for h in g.get("hooks", []) if "command" in h]
        claude_post_write = next(c for c in claude_cmds if "claude.post_write" in c)
        claude_prefix = claude_post_write.split("&&")[0] + "&&"

        assert codex_prefix == claude_prefix
        assert "show-toplevel" in codex_prefix


class TestCodexStatus:
    def test_is_installed_true_after_install(self, home):
        codex.install(home)
        assert codex.is_installed(home) is True

    def test_is_installed_false_when_absent(self, home):
        assert codex.is_installed(home) is False

    def test_status_lines_report_registered(self, home):
        codex.install(home)
        lines = "\n".join(codex.status_lines(home))
        assert "[codex]" in lines
        assert "registered" in lines
        assert "installed" in lines

    def test_status_tolerates_corrupt_files(self, home):
        d = home / ".codex"
        d.mkdir(parents=True)
        (d / "config.toml").write_text("not = = valid [[[")
        (d / "hooks.json").write_text("{ not json")
        # must not raise and must read as not-installed
        assert codex.is_installed(home) is False
        assert codex.status_lines(home)  # returns lines, no crash

    def test_is_present(self, home):
        assert codex.is_present(home) is False
        (home / ".codex").mkdir()
        assert codex.is_present(home) is True
