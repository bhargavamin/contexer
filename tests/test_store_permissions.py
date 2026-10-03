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


@pytest.mark.parametrize("surface", ["store", "install"])
def test_symlink_target_is_never_chmodded(tmp_path, monkeypatch, surface):
    target = tmp_path / "shared"
    target.mkdir(mode=0o755)
    link = tmp_path / ".contexer"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(store, "store_dir", lambda: link)
    monkeypatch.setattr(cli.Path, "home", lambda: tmp_path)
    monkeypatch.setattr(cli, "_resolve_targets", lambda rest: [])
    monkeypatch.chdir(tmp_path)
    with pytest.raises(OSError, match="symlink"):
        store.ensure_store_dir() if surface == "store" else cli.install([])
    assert target.stat().st_mode & 0o777 == 0o755


def test_spool_reuse_tightens_existing_store(tmp_path, monkeypatch):
    from contexer import spool
    target = tmp_path / ".contexer"
    target.mkdir(mode=0o755)
    monkeypatch.setattr(store, "store_dir", lambda: target)
    spool._ensure_dir(target / "evidence" / "pending")
    assert target.stat().st_mode & 0o777 == 0o700


def test_console_state_write_tightens_existing_directory(tmp_path, monkeypatch):
    from contexer.ui import daemon
    target = tmp_path / ".contexer"
    target.mkdir(mode=0o755)
    monkeypatch.setattr(daemon, "STATE_PATH", target / "ui.json")
    daemon.write_state(daemon.UiState(0, 31415, "fixture", "now", "dev"))
    assert target.stat().st_mode & 0o777 == 0o700
