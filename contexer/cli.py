import json
import os
import shutil
import sys
import time
import urllib.request
from importlib.metadata import PackageNotFoundError, version as _dist_version
from pathlib import Path

from contexer import adapters
from contexer.adapters import claude
from contexer.adapters.base import _is_corrupt, _load_safe

_PYPI_JSON_URL = "https://pypi.org/pypi/contexer/json"

USAGE = """contexer — persistent context for Claude Code, Cursor, Codex, and Gemini CLI

Usage: contexer [command]

Commands:
  (no args)     Run the MCP server over stdio (how your AI assistant launches it).
  install       Register the MCP server + hooks. Auto-detects supported AI assistants;
                use --target claude|cursor|codex|gemini|all to override.
  uninstall     Remove the MCP server + hooks. Add --purge to also delete the store.
  reinstall     Re-sync config (uninstall + install). Does NOT rebuild the binary.
  review        Interactively approve, edit, or ignore pending engineering decisions;
                also surfaces possibly-overlapping rules for manual consolidation.
  share         Push local decisions to your team cloud context: share [id | --all] (default: latest).
  login         Sign in to Contexer Teams (browser OAuth); enables pull/share with no pasted token.
  logout        Remove stored Contexer Teams credentials.
  status        Show install state: version, binary path, MCP/hooks, store summary.
  version       Print the installed version.
  help          Show this message.

Flags:
  -V, --version   Same as `version`.
  -h, --help      Same as `help`.
  --target NAME   With install/uninstall/status: claude, cursor, codex, gemini, or all.
  --purge         With `uninstall`: also delete ~/.contexer/ (stored context).
                  Prompts for confirmation unless --yes is given.
  -y, --yes       Skip the --purge confirmation prompt (for unattended use).

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


def _format_age(seconds: float) -> str:
    """A short human age like '5s', '3m', '2h', '4d' for a `last_sync` timestamp."""
    seconds = max(0, seconds)
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _read_team_cache(store_dir: Path, repo_path: str) -> dict:
    """Read the team cache file for `repo_path`, resolved off `store_dir` (the SAME home
    `status()` already derived for this call) rather than team_context.STORE_DIR, which is
    a module constant frozen at import time. Tolerant of missing/corrupt files, like
    team_context._load_cache - a diagnostic must never raise on bad state, ZERO network."""
    from contexer import store as _store
    path = store_dir / f".team_{_store._slug(repo_path)}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _read_team_creds(store_dir: Path) -> dict | None:
    """Read stored Teams OAuth credentials directly off `store_dir` - the read-only,
    home-consistent counterpart to auth._load_creds() (which is pinned to the frozen
    auth.STORE_DIR constant). Never prints the token itself, only whether one exists."""
    path = store_dir / ".team_auth.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _usage(stream=None) -> None:
    print(USAGE, file=stream or sys.stdout)


def _resolve_targets(rest: list) -> list:
    """Parse --target or auto-detect installed assistants, falling back to Claude."""
    target = None
    if "--target" in rest:
        i = rest.index("--target")
        if i + 1 < len(rest):
            target = rest[i + 1]
    if target:
        try:
            return adapters.select(target)
        except KeyError:
            print(f"Unknown target: {target} (choose claude, cursor, codex, gemini, or all)",
                  file=sys.stderr)
            sys.exit(1)
    detected = adapters.detect()
    return detected or [adapters.get("claude")]


def install(rest: list | None = None) -> None:
    home = Path.home()
    (home / ".contexer").mkdir(exist_ok=True)
    for adapter in _resolve_targets(rest or []):
        print(f"Installing for {adapter.NAME}...")
        for line in adapter.install(home):
            print(line)
    print()
    print("Done. Restart your AI assistant and open any git repo to activate Contexer.")


def _confirm_purge(store_dir: Path) -> bool:
    """Guard the destructive --purge: require an explicit 'yes'. In a non-interactive
    context (no TTY) refuse rather than risk an accidental delete of stored context —
    use --yes to purge unattended."""
    if not sys.stdin.isatty():
        print("  Refusing to purge non-interactively — re-run with --yes to confirm.")
        return False
    print(f"  WARNING: this permanently deletes {store_dir} and ALL stored context.")
    try:
        reply = input("  Type 'yes' to confirm, anything else to cancel: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return reply in ("yes", "y")


def uninstall(rest: list | None = None, purge: bool = False, assume_yes: bool = False) -> None:
    home = Path.home()
    _rest = rest or []
    _purge = purge or ("--purge" in _rest)
    _assume_yes = assume_yes or ("--yes" in _rest) or ("-y" in _rest)
    for adapter in _resolve_targets(_rest):
        for line in adapter.uninstall(home):
            print(line)

    store_dir = home / ".contexer"
    print()
    if _purge:
        if not store_dir.exists():
            print(f"  - No store to purge ({store_dir} absent)")
            print("Uninstall complete.")
        elif _assume_yes or _confirm_purge(store_dir):
            shutil.rmtree(store_dir)
            print(f"  ✓ Removed {store_dir} (stored context purged)")
            print("Uninstall complete.")
        else:
            print("Uninstall complete. Context store (~/.contexer/) was not removed.")
    else:
        print("Uninstall complete. Context store (~/.contexer/) was not removed.")
        print("To delete stored context too: contexer uninstall --purge")


def version() -> None:
    print(f"contexer {_version()}")


def review() -> None:
    """Interactively review and approve/ignore/edit pending engineering decisions."""
    from contexer import store

    repo_path = store._git_root(os.getcwd())
    if not repo_path:
        print("Not inside a git repository.", file=sys.stderr)
        sys.exit(1)

    pending = store.get_pending_decisions(repo_path)
    if not pending:
        print("No decisions pending approval.")
        _print_overlap_section(repo_path)
        return

    print(f"\n{len(pending)} decision(s) pending approval for {Path(repo_path).name}\n")

    approved = ignored = dismissed = edited = skipped = 0
    for i, entry in enumerate(pending, 1):
        prop = entry.get("proposed_revision")
        print("─" * 60)
        print(f"Decision {i} of {len(pending)}\n")
        subtype = entry.get("subtype") or "decision"
        if prop:
            # Suggested Update: show the current revision and the detected change.
            score = prop.get("confidence", 0)
            factors = prop.get("confidence_factors") or []
            rev = entry.get("revision", 1)
            print(f"[{subtype}] Suggested update")
            print(f'  Current (revision {rev}): "{entry["content"]}"')
            print(f'  Detected:                "{prop.get("content", "")}"\n')
        else:
            score, factors = store._compute_confidence(entry)
            print(f"[{subtype}] \"{entry['content']}\"\n")
        print(f"Confidence: {score}%")
        if factors:
            print("Evidence:")
            for f in factors:
                print(f"  - {f}")
        print()
        if prop:
            print("[Y] Approve  [E] Edit  [D] Dismiss  [S] Skip")
        else:
            print("[Y] Approve  [E] Edit  [N] Ignore  [S] Skip")

        try:
            choice = input("Choice: ").strip().upper()
        except (KeyboardInterrupt, EOFError):
            print("\nAborted.")
            break

        if choice in ("Y", "YES"):
            ok, msg = store.approve_decision(repo_path, entry["id"], "approve")
            if ok:
                approved += 1
                print(f"Approved.")
        elif choice in ("D", "DISMISS"):
            ok, msg = store.approve_decision(repo_path, entry["id"], "dismiss")
            if ok:
                dismissed += 1
                print(msg)
        elif choice in ("N", "NO"):
            ok, msg = store.approve_decision(repo_path, entry["id"], "ignore")
            if ok:
                ignored += 1
                print(msg)
        elif choice in ("E", "EDIT"):
            print(f'Current: "{entry["content"]}"')
            try:
                new_content = input("Edit: ").strip()
            except (KeyboardInterrupt, EOFError):
                print("\nSkipped.")
                skipped += 1
                continue
            if new_content:
                ok, msg = store.approve_decision(repo_path, entry["id"], "edit", new_content)
                if ok:
                    edited += 1
                    print("Approved with edits.")
            else:
                print("No changes made, skipping.")
                skipped += 1
        else:
            skipped += 1
            print("Skipped.")

    print("\n" + "─" * 60)
    parts = []
    if approved:
        parts.append(f"{approved} approved")
    if edited:
        parts.append(f"{edited} edited and approved")
    if dismissed:
        parts.append(f"{dismissed} dismissed")
    if ignored:
        parts.append(f"{ignored} ignored")
    if skipped:
        parts.append(f"{skipped} skipped")
    print(f"Review complete: {', '.join(parts) if parts else 'nothing changed'}.")
    _print_overlap_section(repo_path)


def _print_overlap_section(repo_path: str) -> None:
    """Tail of `contexer review`: read-only report of possibly-overlapping
    constraint/convention rules, for manual consolidation. Silent when clean —
    never merges or deletes."""
    from contexer import store

    clusters = store.overlap_report(repo_path)
    if not clusters:
        return
    print(f"\nPossibly overlapping rules ({len(clusters)} cluster(s)):\n")
    for i, cluster in enumerate(clusters, 1):
        print(f"Cluster {i} ({len(cluster)} rules):")
        for d in cluster:
            print(f'  {d["id"]}  [{d["subtype"]}, {d["status"]}]  "{d["content"]}"')
        print()
    print("To consolidate: keep the best rule (edit its wording via your agent or "
          "update_context with replace_id if it needs cleanup) and retire the rest with "
          "approve_decision(entry_id, action=\"ignore\") — ignore now works on approved "
          "rules too, not just pending ones. Contexer never merges or deletes automatically.")


def reinstall() -> None:
    print("Re-syncing Contexer config (uninstall + install)...\n")
    uninstall()
    print()
    install()
    print()
    print("Note: this only re-synced the MCP/hook config. To upgrade the program itself,")
    print("run `uv tool install --reinstall contexer`, then restart your AI assistant.")


def status(rest: list | None = None) -> None:
    home = Path.home()
    bin_path = shutil.which("contexer") or "(not on PATH)"

    # Resolve targets once — used for both the status_lines loop and installed_ok.
    # status is a diagnostic — it must survive any state it might be asked to
    # diagnose, including corrupt config files and hand-edited entries.
    targets = _resolve_targets(rest or [])
    installed_ok = all(a.is_installed(home) for a in targets)

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
    for adapter in targets:
        for line in adapter.status_lines(home):
            print(line)
    print(f"  store dir:    {store_dir}{'' if store_dir.exists() else ' (absent)'}")
    print(f"  repo stores:  {len(stores)} ({entries} entries total)")
    if latest_t and installed_t and latest_t > installed_t:
        print(f"  update:       {latest} available — run `uv tool upgrade contexer`, "
              f"then restart your AI assistant")
    if swept:
        print(f"  cleaned:      {swept} stale temp file(s) from interrupted writes")
    if current.exists():
        try:
            print(f"  current repo: {current.read_text().strip()}")
        except OSError:
            print("  current repo: (unreadable)")

    # Team sync block (Phase 2 observability). ZERO network calls - config.toml + the team
    # cache file are read straight off disk, same as everything else in status(). Never
    # prints the token itself, only where it came from (oauth / config token / none).
    from contexer import auth, config

    profile = config.load_profile(path=store_dir / "config.toml")
    if profile.mode != "team" or not profile.endpoint:
        print("  team sync:    off (local mode)")
    else:
        print(f"  team sync:    on ({profile.endpoint})")
        creds = _read_team_creds(store_dir)
        if creds and creds.get("issuer") == auth._issuer_from_endpoint(profile.endpoint):
            token_source = "oauth"
        elif profile.token:
            token_source = "config token"
        else:
            token_source = "none"
        print(f"    token:      {token_source}")
        repo = current.read_text().strip() if current.exists() else ""
        if not repo:
            print("    cache:      (no current repo detected)")
        else:
            cache = _read_team_cache(store_dir, repo)
            rows = cache.get("decisions", [])
            print(f"    cache:      {len(rows)} decision(s), cursor={cache.get('cursor') or '(none)'}")
            last_sync = cache.get("last_sync")
            if not last_sync:
                print("    last sync:  never")
            else:
                outcome = "ok" if last_sync.get("ok") else "failed"
                age = _format_age(time.time() - last_sync.get("at", time.time()))
                print(f"    last sync:  {outcome}, {age} ago ({last_sync.get('duration_ms', 0)}ms)")
            last_render = cache.get("last_render")
            if last_render:
                kb = last_render.get("chars", 0) / 1024
                print(f"    last render: {last_render.get('rows', 0)} rows, ~{kb:.1f}KB")

    config_paths = (
        home / ".claude.json",
        home / ".claude" / "settings.json",
        home / ".cursor" / "mcp.json",
        home / ".cursor" / "hooks.json",
        home / ".codex" / "hooks.json",
        home / ".gemini" / "settings.json",
    )
    corrupt = [p for p in config_paths
               if _is_corrupt(p)]
    if corrupt:
        for p in corrupt:
            print(f"\n  WARNING: {p} exists but is not valid JSON — fix or remove it.")
        print("  (`contexer install` fails loudly on a corrupt file rather than overwrite it.)")
    elif not installed_ok:
        print("\n  Not fully installed — run `contexer install`.")


def _run_guarded(fn) -> None:
    """Run a mutating command; turn a PermissionError into actionable advice.

    contexer only ever writes inside the user's own home (assistant config directories
    and ~/.contexer/), so permission errors almost always
    mean a previous `sudo` run left those files owned by root — the fix is to
    restore ownership, never to escalate."""
    try:
        fn()
    except PermissionError as e:
        target = e.filename or "a config file"
        print(f"Permission denied: {target}", file=sys.stderr)
        print("contexer writes only to files in your own home directory "
              "(~/.claude*, ~/.cursor, ~/.codex, ~/.gemini, ~/.contexer) — "
              "it never needs sudo.", file=sys.stderr)
        print("A previous run with sudo can leave those files owned by root. "
              "Restore ownership:", file=sys.stderr)
        print('  sudo chown -R "$USER" ~/.claude.json ~/.claude ~/.cursor ~/.codex '
              '~/.gemini ~/.contexer', file=sys.stderr)
        print("then re-run this command without sudo.", file=sys.stderr)
        sys.exit(1)
    except (json.JSONDecodeError, ValueError) as e:
        # A corrupt or non-object assistant config. Abort cleanly
        # and leave the file untouched for the user to fix — never overwrite it.
        print(f"Corrupt config: {e}", file=sys.stderr)
        print("An assistant config file is not valid JSON (or not a JSON object). "
              "contexer won't overwrite it.", file=sys.stderr)
        print("Fix or remove the offending file, then re-run this command.", file=sys.stderr)
        sys.exit(1)


def pull(rest: list | None = None) -> None:
    """`contexer pull`: fetch team context for the current repo into the local team cache.

    Local-first: no-op when not in team mode / no remote / cloud unreachable (degrades
    quietly). Requires being run inside a git repository."""
    import os

    from contexer import store, team_context

    repo = store._git_root(os.getcwd()) or store._resolve_repo("")
    if not repo:
        print("No git repo detected - run `contexer pull` inside a repository.", file=sys.stderr)
        sys.exit(1)
    upserted, removed = team_context.pull(repo)
    msg = f"Pulled {upserted} team decision(s)"
    if removed:
        msg += f", removed {removed}"
    print(msg + ".")


def share_cmd(rest: list | None = None) -> None:
    """`contexer share [id | --all]`: push local decision(s) up to your team cloud context.

    Local-first: prints a clear message when not in team mode / offline (never crashes).
    Must be run inside a git repository."""
    import os

    from contexer import config, share, store

    rest = rest or []
    yes = "--yes" in rest or "-y" in rest
    share_all = "--all" in rest
    ids = [a for a in rest if a not in ("--all", "--yes", "-y")]
    if share_all and ids:
        print("Pass either an id or --all, not both.", file=sys.stderr)
        sys.exit(1)
    repo = store._git_root(os.getcwd()) or store._resolve_repo("")
    if not repo:
        print("No git repo detected - run `contexer share` inside a repository.", file=sys.stderr)
        sys.exit(1)

    # Confirm-before-push (outward action). --yes or config skip_confirm bypasses the prompts.
    profile = config.load_profile()  # loaded once, reused by the push below
    bypass = yes or profile.skip_confirm

    # No id and no --all: don't guess ('most recent') — show a numbered picker so the developer
    # sees the options and multi-selects. Selecting IS the confirm, so we push directly.
    if not share_all and not ids and not bypass:
        picked = _pick_shareable(repo)
        if not picked:
            print("Cancelled — nothing was pushed.")
            return
        print(share.share_ids(repo, picked, profile=profile))
        return

    if not bypass:
        decision = _confirm_share(repo, share_all, ids)
        if decision is None:   # nothing to share — _confirm_share already said so
            return
        if not decision:       # developer declined
            print("Cancelled — nothing was pushed.")
            return

    if share_all:
        print(share.share_all(repo, profile=profile))
    else:
        print(share.share(repo, ids[0] if ids else "", profile=profile))


def _pick_shareable(repo: str) -> list:
    """Interactive numbered multi-select of shareable decisions. Returns the chosen ids ([] to
    cancel / nothing to share). Pure local read; no network until the caller pushes."""
    from contexer import store

    items = store.get_shareable_all(repo)
    if not items:
        print("No decisions available to share.")
        return []
    shown = items[:store._FILTERED_DISPLAY]
    print(f"\nShareable decisions — pushing sends them to your PERSONAL cloud "
          f"({store._SHARE_OUTWARD_WARNING}):\n")
    for i, it in enumerate(shown, 1):
        print(f"  {i}. " + store._share_item_line(it, maxlen=80).strip())
    if len(items) > len(shown):
        print(f"  …and {len(items) - len(shown)} more (share by id)")
    try:
        raw = input("\nSelect to share [e.g. 1,3 | all | q]: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return []
    if raw == "all":
        return [it.get("id") or "" for it in shown]
    picked = []
    for tok in raw.replace(" ", "").split(","):
        if tok.isdigit() and 1 <= int(tok) <= len(shown):
            picked.append(shown[int(tok) - 1].get("id") or "")
    return picked


def _confirm_share(repo: str, share_all: bool, ids: list) -> bool | None:
    """Preview what a personal-cloud push would send and ask to proceed. Returns True to push,
    False if the developer declined, None if there is nothing to share (message already printed).
    Pure local read — no network happens until the caller actually calls share()."""
    from contexer import store

    if share_all:
        items = store.get_shareable_all(repo)
        if not items:
            print("Nothing to share.")
            return None
        print(f"\nAbout to push {len(items)} decision(s) to your PERSONAL cloud — "
              f"{store._SHARE_OUTWARD_WARNING}:\n")
        for it in items[:10]:
            print(store._share_item_line(it, maxlen=80))
        if len(items) > 10:
            print(f"  …and {len(items) - 10} more")
    else:
        proj = store.get_shareable(repo, ids[0] if ids else "")
        if proj is None:
            print("Nothing to share — no matching decision found.")
            return None
        print(f"\nAbout to push to your PERSONAL cloud — {store._SHARE_OUTWARD_WARNING}:\n")
        print(store._share_item_line(proj))
    try:
        return input("\nPush to cloud? [y/N]: ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def login_cmd(rest: list | None = None) -> None:
    """`contexer login [--endpoint URL]`: sign in to Contexer Teams via the browser (OAuth).

    Self-configuring — writes config.toml itself, so no manual setup. Endpoint defaults to prod
    (or localhost under CONTEXER_ENV=local); override with --endpoint."""
    from contexer import auth

    rest = rest or []
    endpoint = None
    if "--endpoint" in rest:
        i = rest.index("--endpoint")
        if i + 1 >= len(rest):
            print("contexer login: --endpoint requires a URL", file=sys.stderr)
            sys.exit(1)
        endpoint = rest[i + 1]
    try:
        auth.login(endpoint=endpoint)
    except (ValueError, RuntimeError) as e:
        # ValueError: bad --endpoint; RuntimeError: OAuth flow failure (state mismatch,
        # no code, no token). Both are user-actionable — print cleanly, no traceback.
        print(f"contexer login: {e}", file=sys.stderr)
        sys.exit(1)
    _post_login_sync()  # refresh team sync so `contexer status` isn't stale after login


def _post_login_sync() -> None:
    """Best-effort team refresh right after a successful login, so `contexer status` doesn't
    keep showing the stale pre-auth `last sync: failed`.

    Refreshes BOTH the current working repo and the repo `contexer status` actually displays
    (the `.current_repo` pointer) — these differ when login is run outside the project you
    last worked in (e.g. logging in from the CLI repo while status still points at your app),
    which is exactly when the stale line is most visible. Uses `team_context.refresh()`: it
    bounds each pull to a short transport timeout (never stalls login on a slow cloud), never
    raises, and drains any queued offline shares. Login has already succeeded and printed, so
    nothing here may fail the command or emit a traceback."""
    try:
        import os

        from contexer import store, team_context

        repos: list[str] = []
        for candidate in (store._git_root(os.getcwd()), store._current_repo_path()):
            if candidate and candidate not in repos:
                repos.append(candidate)
        upserted = removed = 0
        for repo in repos:
            up, rm = team_context.refresh(repo)  # bounded timeout, never raises
            upserted += up
            removed += rm
    except Exception:
        return  # login is done — never let a post-login sync problem surface as a failure
    if upserted or removed:
        msg = f"Synced {upserted} team decision(s)"
        if removed:
            msg += f", removed {removed}"
        print(msg + ".")


def logout_cmd(rest: list | None = None) -> None:
    """`contexer logout`: remove stored Contexer Teams credentials."""
    from contexer import auth

    if auth.logout():
        print("Logged out of Contexer Teams.")
    else:
        print("Not logged in.")


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
        _run_guarded(lambda: install(rest))
    elif cmd == "uninstall":
        _run_guarded(lambda: uninstall(rest))
    elif cmd == "reinstall":
        _run_guarded(reinstall)
    elif cmd == "review":
        review()
    elif cmd == "status":
        status(rest)
    elif cmd == "pull":
        _run_guarded(lambda: pull(rest))
    elif cmd == "share":
        _run_guarded(lambda: share_cmd(rest))
    elif cmd == "login":
        _run_guarded(lambda: login_cmd(rest))
    elif cmd == "logout":
        _run_guarded(lambda: logout_cmd(rest))
    else:
        print(f"Unknown command: {cmd}\n", file=sys.stderr)
        _usage(sys.stderr)
        sys.exit(1)
