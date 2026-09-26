#!/usr/bin/env python3
"""Install or remove this opt-in review hook, preserving other user configuration."""
import argparse
import copy
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import sys
import tempfile
import uuid

NAME = 'contexer-effectiveness-review'
SOURCE = Path(__file__).resolve().parent.parent
CONFIGS = {'cursor': ('.cursor/hooks.json', 'stop'),
           'claude': ('.claude/settings.json', 'Stop'),
           'codex': ('.codex/hooks.json', 'Stop')}


def owned(hook):
    if not isinstance(hook, dict) or not isinstance(hook.get('command'), str):
        return False
    try:
        parts = shlex.split(hook['command'])
    except ValueError:
        return False
    return any(Path(part).parts[-3:] == (NAME, 'scripts', 'stop_hook.py') for part in parts)


def reject_symlinks(path):
    if any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError(f'Refusing to replace a symlinked path: {path}')


def read_config(path):
    reject_symlinks(path)
    if not path.exists():
        return {}
    config = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(config, dict) or not isinstance(config.get('hooks', {}), dict):
        raise ValueError(f'Expected an object with a hooks object: {path}')
    return config


def configured(config, host, destination, python, uninstall=False):
    result = copy.deepcopy(config)
    event = CONFIGS[host][1]
    hooks = result.setdefault('hooks', {})
    entries = hooks.get(event, [])
    if not isinstance(entries, list):
        raise ValueError(f'Expected hooks.{event} to be a list')
    kept = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError(f'Expected an object in hooks.{event}')
        if host == 'cursor':
            if not owned(entry):
                kept.append(entry)
        else:
            members = entry.get('hooks')
            if not isinstance(members, list):
                raise ValueError(f'Expected a hooks list inside hooks.{event}')
            remaining = [hook for hook in members if not owned(hook)]
            if remaining or not members:
                kept.append(dict(entry, hooks=remaining))
    if not uninstall:
        command = shlex.join([str(python), str(destination / 'scripts/stop_hook.py'), '--host', host])
        hook = {'type': 'command', 'command': command, 'timeout': 10}
        if host == 'cursor':
            kept.append(dict(hook, loop_limit=1))
            result.setdefault('version', 1)
        else:
            kept.append({'hooks': [hook]})
    if kept:
        hooks[event] = kept
    else:
        hooks.pop(event, None)
    if not hooks and 'hooks' not in config:
        result.pop('hooks', None)
    return result


def atomic_write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=f'.{path.name}.', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(content)
            os.fchmod(stream.fileno(), mode)
        os.replace(temp, path)
    finally:
        Path(temp).unlink(missing_ok=True)


def skill_files(source):
    paths = [source / name for name in ('SKILL.md', 'schema.md', 'README.md', 'pytest.ini')]
    paths += sorted((source / 'scripts').glob('*.py'))
    paths += sorted((source / 'tests').glob('*.py'))
    for path in paths:
        if not path.is_file() or path.is_symlink():
            raise ValueError(f'Missing or symlinked skill source: {path}')
    return {path.relative_to(source): path.read_bytes() for path in paths}


def install(home, hosts, *, dry_run=False, uninstall=False, source=SOURCE, python=None):
    home = Path(home).expanduser().resolve()
    destination = home / '.agents/skills' / NAME
    python = Path(python or sys.executable).resolve()
    reject_symlinks(destination)
    plans = []
    # Validate every selected config and source before modifying any of them.
    for host in hosts:
        path = home / CONFIGS[host][0]
        current = read_config(path)
        new = configured(current, host, destination, python, uninstall)
        if new != current:
            original = path.read_bytes() if path.exists() else None
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
            plans.append((path, original, mode, (json.dumps(new, indent=2) + '\n').encode()))
    files = {} if uninstall else skill_files(source)
    changed = []
    for relative, content in files.items():
        path = destination / relative
        reject_symlinks(path)
        if not path.exists() or path.read_bytes() != content:
            changed.append((path, path.read_bytes() if path.exists() else None, content))
    for path, *_ in plans:
        print(f'{"Would update" if dry_run else "Update"}: {path}')
    if changed:
        print(f'{"Would install" if dry_run else "Install"}: {destination} ({len(changed)} changed files)')
    if not plans and not changed:
        print('Already unregistered.' if uninstall else 'Already installed; no changes.')
    if dry_run:
        return
    applied = []
    backup_suffix = f'.bak-effectiveness-{uuid.uuid4().hex[:12]}'
    try:
        if changed and destination.exists():
            backup = destination.with_name(destination.name + backup_suffix)
            shutil.copytree(destination, backup, symlinks=True)
            print(f'Skill backup: {backup}')
        for path, previous, content in changed:
            mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o600
            applied.append((path, previous, mode))
            atomic_write(path, content, mode)
        for path, previous, mode, content in plans:
            if previous is not None:
                backup = path.with_name(path.name + backup_suffix)
                atomic_write(backup, previous, 0o600)
                print(f'Config backup: {backup}')
            applied.append((path, previous, mode))
            atomic_write(path, content, mode)
    except OSError:
        for path, previous, mode in reversed(applied):
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                atomic_write(path, previous, mode)
        raise
    print('Records and pending reviews are retained.' if uninstall else
          'Installed. Restart the selected hosts, then run the verification steps in README.md.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', action='append', required=True, choices=[*CONFIGS, 'all'])
    parser.add_argument('--home', type=Path, default=Path.home(), help='User home (use a temporary directory for testing)')
    parser.add_argument('--dry-run', action='store_true', help='Preview changes without writing')
    parser.add_argument('--uninstall', action='store_true', help='Remove only this hook; keep the skill and records')
    args = parser.parse_args()
    if os.name != 'posix' or sys.version_info < (3, 12):
        parser.error('Requires macOS or Linux and Python 3.12+')
    hosts = list(CONFIGS) if 'all' in args.host else list(dict.fromkeys(args.host))
    try:
        install(args.home, hosts, dry_run=args.dry_run, uninstall=args.uninstall)
    except (OSError, ValueError) as exc:
        parser.exit(2, f'Installation failed: {exc}\n')


if __name__ == '__main__':
    main()
