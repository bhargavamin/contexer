import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

import pytest

import install


@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'source'
    shutil.copytree(install.SOURCE, root, ignore=shutil.ignore_patterns('__pycache__', '.pytest_cache'))
    return root


def read(path):
    return json.loads(path.read_text())


@pytest.mark.parametrize('host', list(install.CONFIGS))
def test_install_is_idempotent_and_keeps_other_settings(tmp_path, source, host):
    home = tmp_path / 'home with spaces'
    config_path = home / install.CONFIGS[host][0]
    config_path.parent.mkdir(parents=True)
    event = install.CONFIGS[host][1]
    foreign = {'type': 'command', 'command': 'echo unrelated'}
    entries = [foreign] if host == 'cursor' else [{'matcher': '*', 'hooks': [foreign]}]
    config_path.write_text(json.dumps({'permissions': {'allow': ['Read']}, 'hooks': {event: entries}}))
    install.install(home, [host], source=source)
    config = read(config_path)
    assert config['permissions'] == {'allow': ['Read']}
    assert config['hooks'][event][0] == entries[0]
    before = config_path.read_bytes()
    backups = set(home.rglob('*.bak-effectiveness-*'))
    install.install(home, [host], source=source)
    assert config_path.read_bytes() == before
    assert set(home.rglob('*.bak-effectiveness-*')) == backups
    entry = config['hooks'][event][-1]
    hook = entry if host == 'cursor' else entry['hooks'][0]
    argv = shlex.split(hook['command'])
    assert Path(argv[0]).is_absolute() and Path(argv[1]).is_file()
    result = subprocess.run(argv, input='{}', text=True, capture_output=True, timeout=10)
    assert result.returncode == 0 and result.stdout.strip() == '{}'


def test_dry_run_writes_nothing(tmp_path, source):
    home = tmp_path / 'new-home'
    install.install(home, list(install.CONFIGS), dry_run=True, source=source)
    assert not home.exists()


def test_bad_later_host_configuration_prevents_partial_install(tmp_path, source):
    home = tmp_path / 'home'
    path = home / '.codex/hooks.json'
    path.parent.mkdir(parents=True)
    path.write_text('{"hooks":')
    with pytest.raises(ValueError):
        install.install(home, ['claude', 'codex'], source=source)
    assert not (home / '.claude').exists()
    assert not (home / '.agents').exists()
    assert path.read_text() == '{"hooks":'


@pytest.mark.parametrize('bad', [[], {'hooks': []}, {'hooks': {'Stop': {}}},
                                {'hooks': {'Stop': ['bad']}}, {'hooks': {'Stop': [{}]}}])
def test_malformed_hook_shapes_are_not_overwritten(tmp_path, source, bad):
    path = tmp_path / '.claude/settings.json'
    path.parent.mkdir()
    original = json.dumps(bad)
    path.write_text(original)
    with pytest.raises(ValueError):
        install.install(tmp_path, ['claude'], source=source)
    assert path.read_text() == original


def test_uninstall_removes_only_our_hook_and_preserves_data(tmp_path, source):
    install.install(tmp_path, ['claude'], source=source)
    path = tmp_path / '.claude/settings.json'
    config = read(path)
    own = config['hooks']['Stop'][0]['hooks'][0]
    foreign = {'type': 'command', 'command': 'echo contexer stop_hook.py'}
    config['hooks']['Stop'] = [{'matcher': '*', 'hooks': [own, foreign]}]
    path.write_text(json.dumps(config))
    records = tmp_path / '.contexer-usage/records/demo.jsonl'
    records.parent.mkdir(parents=True)
    records.write_text('keep this data')
    install.install(tmp_path, ['claude'], uninstall=True)
    assert read(path)['hooks']['Stop'] == [{'matcher': '*', 'hooks': [foreign]}]
    assert records.read_text() == 'keep this data'
    assert (tmp_path / '.agents/skills' / install.NAME / 'SKILL.md').is_file()


def test_existing_skill_is_backed_up_and_extra_files_are_kept(tmp_path, source):
    install.install(tmp_path, ['claude'], source=source)
    destination = tmp_path / '.agents/skills' / install.NAME
    (destination / 'schema.md').write_text('local edits')
    (destination / 'my-notes.md').write_text('retain')
    install.install(tmp_path, ['claude'], source=source)
    backups = list(destination.parent.glob(install.NAME + '.bak-effectiveness-*'))
    assert len(backups) == 1 and (backups[0] / 'schema.md').read_text() == 'local edits'
    assert (destination / 'my-notes.md').read_text() == 'retain'
    assert (destination / 'schema.md').read_bytes() == (source / 'schema.md').read_bytes()


def test_symlinked_configuration_is_not_replaced(tmp_path, source):
    target = tmp_path / 'original.json'
    target.write_text('{}')
    path = tmp_path / '.claude/settings.json'
    path.parent.mkdir()
    path.symlink_to(target)
    with pytest.raises(ValueError, match='symlink'):
        install.install(tmp_path, ['claude'], source=source)
    assert target.read_text() == '{}'
    assert not (tmp_path / '.agents').exists()


def test_failed_write_rolls_back_skill_and_configs(tmp_path, source, monkeypatch):
    install.install(tmp_path, ['claude', 'codex'], source=source)
    schema = tmp_path / '.agents/skills' / install.NAME / 'schema.md'
    schema.write_text('before')
    paths = [tmp_path / install.CONFIGS[h][0] for h in ('claude', 'codex')]
    for path in paths:
        path.write_text('{}')
    original = install.atomic_write
    failed = False
    def failing_write(path, data, mode=0o600):
        nonlocal failed
        if path == paths[1] and not failed:
            failed = True
            raise OSError('simulated disk failure')
        original(path, data, mode)
    monkeypatch.setattr(install, 'atomic_write', failing_write)
    with pytest.raises(OSError):
        install.install(tmp_path, ['claude', 'codex'], source=source)
    assert schema.read_text() == 'before'
    assert all(path.read_text() == '{}' for path in paths)


def test_cli_requires_explicit_host():
    result = subprocess.run([sys.executable, str(install.SOURCE / 'scripts/install.py')],
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 2


def test_home_alias_is_resolved_but_nested_symlinks_are_rejected(tmp_path, source):
    real = tmp_path / 'real-home'
    real.mkdir()
    alias = tmp_path / 'home-alias'
    alias.symlink_to(real, target_is_directory=True)
    install.install(alias, ['cursor'], source=source)
    assert (real / '.cursor/hooks.json').is_file()
    other = tmp_path / 'other-config'
    other.mkdir()
    (real / '.codex').symlink_to(other, target_is_directory=True)
    with pytest.raises(ValueError, match='symlink'):
        install.install(alias, ['codex'], source=source)
    assert not (other / 'hooks.json').exists()
