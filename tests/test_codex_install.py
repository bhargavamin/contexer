"""Tests for the Codex adapter install/uninstall."""
import json
import sys
import tomllib
from pathlib import Path

import pytest

from contexer.adapters import codex


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

    # ── Doc Drift Layer 1 — Task 1.7: Codex reuses claude.post_write/claude.drift verbatim ──

    def test_post_write_hook_registered_with_git_toplevel_wrapper(self, home):
        codex.install(home)
        put = _hooks(home)["hooks"]["PostToolUse"]
        cmds = [h["command"] for g in put for h in g["hooks"]]
        matches = [c for c in cmds if "claude.post_write" in c]
        assert matches
        # LOAD-BEARING: identical git-toplevel $REPO resolution to drift's wrapper —
        # not a raw-cwd or one-arg version.
        assert "git rev-parse --show-toplevel" in matches[0]
        assert "$REPO" in matches[0]
        assert "sys.argv[1]" in matches[0]

    def test_post_write_matcher_covers_write_edit(self, home):
        codex.install(home)
        put = _hooks(home)["hooks"]["PostToolUse"]
        matches = [g for g in put if any("claude.post_write" in h["command"] for h in g["hooks"])]
        assert matches
        assert matches[0].get("matcher") == "Write|Edit"

    def test_drift_hook_registered_on_user_prompt_submit(self, home):
        codex.install(home)
        ups = _hooks(home)["hooks"]["UserPromptSubmit"]
        cmds = [h["command"] for g in ups for h in g["hooks"]]
        matches = [c for c in cmds if "claude.drift" in c]
        assert matches
        assert "git rev-parse --show-toplevel" in matches[0]
        assert "sys.argv[1]" in matches[0]

    def test_post_write_and_drift_in_event_markers(self):
        assert "claude.post_write" in codex._EVENT_MARKERS["PostToolUse"]
        assert "claude.drift" in codex._EVENT_MARKERS["UserPromptSubmit"]

    def test_uninstall_removes_post_write_and_drift(self, home):
        codex.install(home)
        codex.uninstall(home)
        hooks = _hooks(home).get("hooks", {})
        put_cmds = [h.get("command", "") for g in hooks.get("PostToolUse", []) for h in g.get("hooks", [])]
        ups_cmds = [h.get("command", "") for g in hooks.get("UserPromptSubmit", []) for h in g.get("hooks", [])]
        assert not any("claude.post_write" in c for c in put_cmds)
        assert not any("claude.drift" in c for c in ups_cmds)

    def test_install_preserves_foreign_post_tool_use_hook(self, home):
        hooks_path = home / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        hooks_path.write_text(json.dumps({"hooks": {"PostToolUse": [
            {"matcher": "Bash", "hooks": [{"type": "command", "command": "./mine.sh"}]}]}}))
        codex.install(home)
        cmds = [h.get("command", "") for g in _hooks(home)["hooks"]["PostToolUse"]
                for h in g.get("hooks", [])]
        assert "./mine.sh" in cmds

    def test_post_write_and_drift_wired_once(self, home):
        codex.install(home)
        codex.install(home)
        put_cmds = [h["command"] for g in _hooks(home)["hooks"]["PostToolUse"] for h in g["hooks"]]
        ups_cmds = [h["command"] for g in _hooks(home)["hooks"]["UserPromptSubmit"] for h in g["hooks"]]
        assert sum("claude.post_write" in c for c in put_cmds) == 1
        assert sum("claude.drift" in c for c in ups_cmds) == 1

    def test_migrates_old_one_arg_post_write(self, home):
        # A pre-git-toplevel-fix install resolved the repo from raw os.getcwd() (no $REPO
        # threading) — that diverges from drift's git-toplevel repo in a monorepo subdir, so
        # post_write's sidecar write and drift's read land under different slugs and drift
        # silently never fires. Reinstall must replace it in place with the $REPO-threading
        # version, detected the same way claude.py detects it: absence of "show-toplevel"
        # alongside "claude.post_write".
        hooks_path = home / ".codex" / "hooks.json"
        hooks_path.parent.mkdir(parents=True)
        old = ('"py" -c "from contexer.adapters import claude; import sys; '
               'print(claude.post_write(\'\', sys.stdin.read()))"')
        hooks_path.write_text(json.dumps({"hooks": {"PostToolUse": [
            {"matcher": "Write|Edit", "hooks": [{"type": "command", "command": old}]}]}}))
        codex.install(home)
        put = _hooks(home)["hooks"]["PostToolUse"]
        cmds = [h["command"] for g in put for h in g["hooks"]]
        matches = [c for c in cmds if "claude.post_write" in c]
        assert len(matches) == 1  # replaced in place, not duplicated
        assert "show-toplevel" in matches[0]

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
