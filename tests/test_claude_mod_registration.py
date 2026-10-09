"""Registration of the Claude Code mod (the in-session review pane) by the Claude adapter.

The mod ships INSIDE the installed package (`contexer/claude_mod/`), and `contexer install`
points Claude Code at that folder through `CLAUDE_CODE_PLUGIN_DIRS` in the `env` block of
`~/.claude/settings.json`. Pointing at the package rather than copying the files is the whole
upgrade story: `uv tool upgrade contexer` replaces the mod together with the Python code, and
the next Claude Code session loads it, with no reinstall. These tests pin the parts that make
that safe: other tools' folders in the same variable survive every write, Contexer's entry is
never duplicated, a stale entry (another Python, another venv) is replaced rather than kept,
the opt-out removes it, and the SessionStart repair never raises and never registers a mod the
developer removed.
"""
import json
import os

import pytest

from contexer.adapters import claude

ENV = claude.MOD_ENV
FOREIGN = "/opt/other-tool/plugin"
STALE = "/old/venv/lib/python3.11/site-packages/contexer/claude_mod"


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(claude.MOD_OPT_OUT, raising=False)
    (tmp_path / ".claude").mkdir()
    return tmp_path


def _settings(home) -> dict:
    return json.loads((home / ".claude" / "settings.json").read_text())


def _write(home, cfg: dict) -> None:
    (home / ".claude" / "settings.json").write_text(json.dumps(cfg))


def _dirs(home) -> list[str]:
    value = _settings(home).get("env", {}).get(ENV)
    return value.split(os.pathsep) if value else []


class TestTheModShipsInThePackage:
    def test_the_plugin_folder_is_complete(self):
        root = claude.mod_dir()
        manifest = json.loads((root / ".claude-plugin" / "plugin.json").read_text())
        hooks = json.loads((root / "hooks" / "hooks.json").read_text())
        assert manifest["name"] == "contexer-review"
        (module,) = hooks["modules"]
        assert (root / "hooks" / module).is_file()


class TestInstall:
    def test_registers_the_package_folder(self, home):
        log = claude.install(home)
        assert _dirs(home) == [str(claude.mod_dir())]
        assert any("review pane" in line for line in log)

    def test_keeps_other_tools_folders(self, home):
        _write(home, {"env": {ENV: FOREIGN, "OTHER": "1"}})
        claude.install(home)
        assert _dirs(home) == [FOREIGN, str(claude.mod_dir())]
        assert _settings(home)["env"]["OTHER"] == "1"

    def test_reinstall_never_duplicates(self, home):
        claude.install(home)
        claude.install(home)
        assert _dirs(home) == [str(claude.mod_dir())]

    def test_a_stale_contexer_folder_is_replaced(self, home):
        _write(home, {"env": {ENV: os.pathsep.join([STALE, FOREIGN])}})
        claude.install(home)
        assert _dirs(home) == [FOREIGN, str(claude.mod_dir())]

    def test_the_opt_out_removes_it(self, home, monkeypatch):
        claude.install(home)
        monkeypatch.setenv(claude.MOD_OPT_OUT, "1")
        log = claude.install(home)
        assert _dirs(home) == []
        assert ENV not in _settings(home).get("env", {})
        assert any(claude.MOD_OPT_OUT in line for line in log)


class TestUninstall:
    def test_removes_only_contexers_folder(self, home):
        _write(home, {"env": {ENV: FOREIGN}})
        claude.install(home)
        claude.uninstall(home)
        assert _dirs(home) == [FOREIGN]

    def test_drops_the_variable_when_nothing_else_is_in_it(self, home):
        claude.install(home)
        claude.uninstall(home)
        assert ENV not in _settings(home).get("env", {})


class TestSessionStartRepair:
    def test_leaves_another_live_install_alone(self, home, tmp_path):
        # Two installs on one machine must not flip the setting back and forth each session.
        other = tmp_path / "other-venv" / "contexer" / "claude_mod"
        other.mkdir(parents=True)
        _write(home, {"env": {ENV: str(other)}})
        claude.heal_mod_registration(home)
        assert _dirs(home) == [str(other)]

    def test_replaces_a_stale_registration(self, home):
        _write(home, {"env": {ENV: os.pathsep.join([FOREIGN, STALE])}})
        claude.heal_mod_registration(home)
        assert _dirs(home) == [FOREIGN, str(claude.mod_dir())]

    def test_never_registers_a_mod_the_developer_removed(self, home):
        _write(home, {"env": {ENV: FOREIGN}})
        claude.heal_mod_registration(home)
        assert _dirs(home) == [FOREIGN]

    def test_leaves_a_current_registration_byte_identical(self, home):
        claude.install(home)
        path = home / ".claude" / "settings.json"
        before = path.read_bytes()
        claude.heal_mod_registration(home)
        assert path.read_bytes() == before

    def test_respects_the_opt_out(self, home, monkeypatch):
        _write(home, {"env": {ENV: STALE}})
        monkeypatch.setenv(claude.MOD_OPT_OUT, "1")
        claude.heal_mod_registration(home)
        assert _dirs(home) == [STALE], "repair is not install: it changes nothing when opted out"

    @pytest.mark.parametrize("raw", ["{not json", "[]", '{"env": "nope"}', ""])
    def test_never_raises_on_a_damaged_settings_file(self, home, raw):
        (home / ".claude" / "settings.json").write_text(raw)
        claude.heal_mod_registration(home)

    def test_never_raises_without_a_settings_file(self, tmp_path):
        claude.heal_mod_registration(tmp_path)

    def test_the_session_checkpoint_entrypoint_runs_the_repair(self, monkeypatch):
        # sync_memory is what every installed SessionStart/PreCompact/SessionEnd hook already
        # calls, so the repair reaches existing installs with no hook-command change.
        calls = []
        monkeypatch.setattr(claude, "heal_mod_registration", lambda: calls.append(1))
        monkeypatch.setattr(claude, "_import_memory_facts", lambda repo: 0)
        monkeypatch.setattr(claude, "_reconcile_evidence", lambda repo: None)
        monkeypatch.setattr(claude, "_scan_automatic_proposals", lambda repo: None)
        claude.sync_memory("/repo")
        assert calls == [1]


class TestStatus:
    def test_reports_the_registration(self, home):
        claude.install(home)
        assert any("review pane" in line and str(claude.mod_dir()) in line
                   and "not registered" not in line for line in claude.status_lines(home))

    def test_reports_a_gone_folder_as_stale(self, home):
        _write(home, {"env": {ENV: STALE}})
        assert any("review pane: STALE" in line for line in claude.status_lines(home))

    def test_reports_its_absence(self, home):
        assert any("review pane" in line and "not registered" in line
                   for line in claude.status_lines(home))
