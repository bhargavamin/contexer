import json
import os
import shutil
import sys
import time
import urllib.request
from importlib.metadata import PackageNotFoundError, version as _dist_version
from pathlib import Path

from contexer.adapters import claude
from contexer.adapters.base import _is_corrupt, _load_safe

_PYPI_JSON_URL = "https://pypi.org/pypi/contexer/json"

USAGE = """contexer — persistent context for Claude Code

Usage: contexer [command]

Commands:
  (no args)     Run the MCP server over stdio (how Claude Code launches it).
  install       Register the MCP server + hooks in your global Claude config.
  uninstall     Remove the MCP server + hooks. Add --purge to also delete the store.
  reinstall     Re-sync config (uninstall + install). Does NOT rebuild the binary.
  status        Show install state: version, binary path, MCP/hooks, store summary.
  version       Print the installed version.
  help          Show this message.

Flags:
  -V, --version   Same as `version`.
  -h, --help      Same as `help`.
  --purge         With `uninstall`: also delete ~/.contexer/ (stored context).

To upgrade the program itself (rebuild the binary):
  uv tool install --reinstall contexer
"""


def _version() -> str:
    try:
        return _dist_version("contexer")
    except PackageNotFoundError:
        return "unknown (not installed as a package)"


def _latest_pypi_version() -> str | None:
    """Latest release on PyPI, or None. Best-effort: never raises, short timeout,
    and skipped entirely when CONTEXER_NO_UPDATE_CHECK is set (airgapped boxes)."""
    if os.environ.get("CONTEXER_NO_UPDATE_CHECK"):
        return None
    try:
        with urllib.request.urlopen(_PYPI_JSON_URL, timeout=2) as resp:
            return json.load(resp)["info"]["version"]
    except Exception:
        return None


def _version_tuple(v: str) -> tuple | None:
    try:
        return tuple(int(p) for p in v.split("."))
    except (ValueError, AttributeError):
        return None


def _usage(stream=None) -> None:
    print(USAGE, file=stream or sys.stdout)


def install() -> None:
    home = Path.home()
    (home / ".contexer").mkdir(exist_ok=True)
    for line in claude.install(home):
        print(line)
    print()
    print("Done. Restart Claude Code and open any git repo to activate Contexer.")


def uninstall(purge: bool = False) -> None:
    home = Path.home()
    for line in claude.uninstall(home):
        print(line)

    store_dir = home / ".contexer"
    print()
    if purge:
        if store_dir.exists():
            shutil.rmtree(store_dir)
            print(f"  ✓ Removed {store_dir} (stored context purged)")
        else:
            print(f"  - No store to purge ({store_dir} absent)")
        print("Uninstall complete.")
    else:
        print("Uninstall complete. Context store (~/.contexer/) was not removed.")
        print("To delete stored context too: contexer uninstall --purge")


def version() -> None:
    print(f"contexer {_version()}")


def reinstall() -> None:
    print("Re-syncing Contexer config (uninstall + install)...\n")
    uninstall()
    print()
    install()
    print()
    print("Note: this only re-synced the MCP/hook config. To upgrade the program itself,")
    print("run `uv tool install --reinstall contexer`, then restart Claude Code.")


def status() -> None:
    home = Path.home()
    bin_path = shutil.which("contexer") or "(not on PATH)"

    # status is a diagnostic — it must survive any state it might be asked to
    # diagnose, including corrupt config files and hand-edited entries.
    installed_ok = claude.is_installed(home)

    store_dir = home / ".contexer"
    swept = 0
    if store_dir.exists():
        # Sweep temp files leaked by interrupted atomic writes (hard crash between
        # mkstemp and os.replace). Never matched by the *.json glob below. The age
        # gate keeps us from unlinking a temp another process is writing right now —
        # that would make its os.replace fail and lose the save.
        for tmp in store_dir.glob("*.tmp"):
            try:
                if time.time() - tmp.stat().st_mtime < 3600:
                    continue
                tmp.unlink()
                swept += 1
            except OSError:
                pass

    def _entry_count(p: Path) -> int:
        entries = _load_safe(p).get("entries", [])
        return len(entries) if isinstance(entries, list) else 0

    stores = sorted(store_dir.glob("*.json")) if store_dir.exists() else []
    entries = sum(_entry_count(p) for p in stores)
    current = store_dir / ".current_repo"

    installed = _version()
    installed_t = _version_tuple(installed)
    latest = _latest_pypi_version() if installed_t else None
    latest_t = _version_tuple(latest) if latest else None

    print(f"contexer {installed}")
    print(f"  binary:       {bin_path}")
    for line in claude.status_lines(home):
        print(line)
    print(f"  store dir:    {store_dir}{'' if store_dir.exists() else ' (absent)'}")
    print(f"  repo stores:  {len(stores)} ({entries} entries total)")
    if latest_t and installed_t and latest_t > installed_t:
        print(f"  update:       {latest} available — run `uv tool upgrade contexer`, "
              f"then restart Claude Code")
    if swept:
        print(f"  cleaned:      {swept} stale temp file(s) from interrupted writes")
    if current.exists():
        try:
            print(f"  current repo: {current.read_text().strip()}")
        except OSError:
            print("  current repo: (unreadable)")

    corrupt = [p for p in (home / ".claude.json", home / ".claude" / "settings.json")
               if _is_corrupt(p)]
    if corrupt:
        for p in corrupt:
            print(f"\n  WARNING: {p} exists but is not valid JSON — fix or remove it.")
        print("  (`contexer install` fails loudly on a corrupt file rather than overwrite it.)")
    elif not installed_ok:
        print("\n  Not fully installed — run `contexer install`.")


def _run_guarded(fn) -> None:
    """Run a mutating command; turn a PermissionError into actionable advice.

    contexer only ever writes inside the user's own home (~/.claude.json,
    ~/.claude/settings.json, ~/.contexer/), so permission errors almost always
    mean a previous `sudo` run left those files owned by root — the fix is to
    restore ownership, never to escalate."""
    try:
        fn()
    except PermissionError as e:
        target = e.filename or "a config file"
        print(f"Permission denied: {target}", file=sys.stderr)
        print("contexer writes only to files in your own home directory "
              "(~/.claude.json, ~/.claude/settings.json, ~/.contexer/) — "
              "it never needs sudo.", file=sys.stderr)
        print("A previous run with sudo can leave those files owned by root. "
              "Restore ownership:", file=sys.stderr)
        print('  sudo chown -R "$USER" ~/.claude.json ~/.claude ~/.contexer', file=sys.stderr)
        print("then re-run this command without sudo.", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    args = sys.argv[1:]

    if not args:
        from contexer.server import main as _server
        _server()
        return

    cmd, rest = args[0], args[1:]
    if cmd in ("version", "--version", "-V"):
        version()
    elif cmd in ("help", "--help", "-h"):
        _usage()
    elif cmd == "install":
        _run_guarded(install)
    elif cmd == "uninstall":
        _run_guarded(lambda: uninstall(purge="--purge" in rest))
    elif cmd == "reinstall":
        _run_guarded(reinstall)
    elif cmd == "status":
        status()
    else:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        _usage(sys.stderr)
        sys.exit(1)
