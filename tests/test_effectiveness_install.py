"""The opt-in measurement hook survives ordinary product installation changes."""
import importlib.util
import json
from pathlib import Path

import pytest

from contexer.adapters import claude, codex, cursor

SOURCE = Path(__file__).resolve().parents[1] / 'tools/contexer-effectiveness-review'
spec = importlib.util.spec_from_file_location('effectiveness_installer', SOURCE / 'scripts/install.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.mark.parametrize('host,adapter', [('claude', claude), ('codex', codex), ('cursor', cursor)])
def test_product_install_reinstall_uninstall_preserves_review_hook(tmp_path, monkeypatch, host, adapter):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.chdir(tmp_path)
    installer.install(tmp_path, [host])
    path = tmp_path / installer.CONFIGS[host][0]
    event = installer.CONFIGS[host][1]

    def review_commands():
        entries = json.loads(path.read_text())['hooks'].get(event, [])
        hooks = entries if host == 'cursor' else [hook for group in entries for hook in group['hooks']]
        return [hook['command'] for hook in hooks if installer.owned(hook, tmp_path / '.agents/skills' / installer.NAME)]

    expected = review_commands()
    assert len(expected) == 1
    adapter.install(tmp_path)
    assert review_commands() == expected
    adapter.install(tmp_path)
    assert review_commands() == expected
    adapter.uninstall(tmp_path)
    assert review_commands() == expected
