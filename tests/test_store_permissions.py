import os

import pytest

from contexer import cli, store


def test_fresh_install_is_private_under_normal_umask(tmp_path, monkeypatch):
    target = tmp_path / ".contexer"
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(store, "store_dir", lambda: target)
    monkeypatch.setattr(cli, "_resolve_targets", lambda rest: [])
    monkeypatch.chdir(tmp_path)
    old = os.umask(0o022)
    try:
        cli.install([])
    finally:
        os.umask(old)
    assert target.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("initial,expected", [(0o755, 0o700), (0o750, 0o700), (0o500, 0o500), (0o700, 0o700), (0o2700, 0o2700)])
def test_next_use_only_removes_non_owner_permissions(tmp_path, monkeypatch, initial, expected):
    target = tmp_path / ".contexer"
    target.mkdir()
    target.chmod(initial)
    monkeypatch.setattr(store, "store_dir", lambda: target)
    assert store.ensure_store_dir() == target
    assert target.stat().st_mode & 0o7777 == expected
