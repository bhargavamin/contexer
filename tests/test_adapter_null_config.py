"""Regression: a config key present but set to JSON null (e.g. `"mcpServers": null`)
crashed install/uninstall/status with a raw TypeError/AttributeError (#324 follow-up).
null holds nothing to lose, so it reads as absent; a non-null wrong type still aborts
cleanly as ValueError and leaves the file untouched."""
import json

import pytest

from contexer.adapters import claude, codex, cursor, gemini

# adapter -> (mcp config file, hooks config file, one hook event the adapter installs)
CASES = {
    "claude": (claude, ".claude.json", ".claude/settings.json", "SessionStart"),
    "cursor": (cursor, ".cursor/mcp.json", ".cursor/hooks.json", "sessionStart"),
    "gemini": (gemini, ".gemini/settings.json", ".gemini/settings.json", "SessionStart"),
    "codex": (codex, None, ".codex/hooks.json", "SessionStart"),
}

NULL_SHAPES = {
    "mcpServers": lambda event: ("mcp", {"mcpServers": None}),
    "hooks": lambda event: ("hooks", {"hooks": None}),
    "event": lambda event: ("hooks", {"hooks": {event: None}}),
}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    return tmp_path


def _write(home, rel, data):
    path = home / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


@pytest.mark.parametrize("shape", NULL_SHAPES)
@pytest.mark.parametrize("name", CASES)
def test_null_key_does_not_crash_install_status_or_uninstall(home, name, shape):
    adapter, mcp_rel, hooks_rel, event = CASES[name]
    which, data = NULL_SHAPES[shape](event)
    rel = mcp_rel if which == "mcp" else hooks_rel
    if rel is None:
        pytest.skip("codex registers MCP in TOML, not JSON")
    _write(home, rel, data)

    adapter.status_lines(home)
    assert adapter.is_installed(home) is False
    adapter.uninstall(home)

    adapter.install(home)
    assert adapter.is_installed(home)
    adapter.uninstall(home)
    assert adapter.is_installed(home) is False


@pytest.mark.parametrize("name", ["claude", "cursor", "gemini"])
def test_wrong_type_mcp_servers_aborts_and_leaves_file_untouched(home, name):
    adapter, mcp_rel, _, _ = CASES[name]
    path = _write(home, mcp_rel, {"mcpServers": ["not", "a", "dict"]})
    before = path.read_text()
    with pytest.raises(ValueError, match="mcpServers"):
        adapter.install(home)
    assert path.read_text() == before
