import json
import os
import shutil
import sys
import time
import urllib.request
from importlib.metadata import PackageNotFoundError, version as _dist_version
from pathlib import Path

from contexer import adapters
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
  ui            Local web console over the stored decisions: ui [--open] [--stop]
                [--status] [--port N] [--foreground] [--reset-token].
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


def _num(value: object, default: float = 0.0) -> float:
    """A number out of one of the JSON files `status` reads, or `default` when it is not one.

    `_read_team_creds` and `_read_team_cache` validate no further than "it is a dict", so a
    hand-edited or half-written `"expires_at": "2026-01-01T00:00:00Z"` reaches arithmetic as a
    string. `main()` runs `status` outside `_run_guarded`, so that TypeError is a raw traceback
    in place of every other diagnostic line — from the command whose whole job is surviving the
    state it is asked to diagnose. Booleans are excluded deliberately: JSON `true` is an int in
    Python and would date a session to 1970."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    return float(value)


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


def _is_repo_store(path: Path) -> bool:
    """Whether a `~/.contexer/*.json` file is one repo's decision store.

    The same directory also holds the global rules (`_global.json`), the console statefile
    (`ui.json`), per-repo tombstone sidecars (`<slug>.deleted.json`) and a family of
    dot-prefixed caches (team cache, outbox, retrieval index, working sets). pathlib's glob
    does not hide any of them, so counting them inflates both `repo stores` and
    `entries total`. A leading underscore alone is NOT disqualifying: `store._slug` keeps one
    from a repo path like /_vendor/app, and a pre-hash legacy store keeps its old name.

    The global store's file name comes from `store.GLOBAL_SLUG` rather than a second literal:
    the store owns that name, and two spellings of it drift apart silently."""
    from contexer import store

    name = path.name
    return not (name.startswith(".") or name.endswith(".deleted.json")
                or name in (f"{store.GLOBAL_SLUG}.json", "ui.json"))


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
            title, body = store._title_and_body(entry)
            print(f"[{subtype}] {title}")
            if body is not None:
                print(f'  "{body}"')
            print()
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

    stores = sorted(p for p in store_dir.glob("*.json") if _is_repo_store(p)) \
        if store_dir.exists() else []
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
            # UnicodeDecodeError too, not just OSError: the shell hooks write this pointer
            # with `printf` (raw bytes, no encoding contract), so a non-UTF-8 path must
            # print "(unreadable)" rather than traceback out of `contexer status`.
            print(f"  current repo: {current.read_text(encoding='utf-8').strip()}")
        except (OSError, UnicodeDecodeError):
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
            # "oauth" alone is what this line used to say for a session that expired days ago,
            # so status agreed with itself while every sync failed. Derived from the creds dict
            # already read off `store_dir` rather than from auth_state, which resolves the real
            # home and would report on the wrong store when --store-dir is in play. No network,
            # no refresh, no secret.
            token_source = "oauth"
            expires_at = _num(creds.get("expires_at"))
            if creds.get("refresh_failed_at"):
                token_source += " (refresh rejected - run `contexer login`)"
            elif expires_at and expires_at <= time.time():
                age = _format_age(time.time() - expires_at)
                # Past expiry is not the same as dead. Access tokens are minted with
                # expires_in 3600, so an hour after the last sync a perfectly healthy session
                # lands here and resolve_token renews it on the next call with no interaction.
                # Only a session with nothing to renew from is worth sending someone to a
                # browser. Mirrors auth_state's `renewable`, re-derived rather than imported
                # because status reads a caller-supplied store_dir, not auth's frozen paths.
                fix = ("renews on next sync" if creds.get("refresh_token")
                       else "run `contexer login`")
                token_source += f" (expired {age} ago - {fix})"
        elif profile.token:
            token_source = "config token"
        else:
            token_source = "none"
        print(f"    token:      {token_source}")
        unreadable = False
        try:
            repo = current.read_text(encoding="utf-8").strip() if current.exists() else ""
        except (OSError, UnicodeDecodeError):
            # Distinguished from an absent pointer on purpose: the line above already
            # printed "(unreadable)", and reporting the same file as "not detected" here
            # would send the reader hunting for a hook that in fact ran.
            repo, unreadable = "", True
        if unreadable:
            print("    cache:      (current repo pointer unreadable)")
        elif not repo:
            print("    cache:      (no current repo detected)")
        else:
            # Every value below is typed by whatever is in the cache file, not by us —
            # same reason `_num` exists: a torn write costs these lines, never the command.
            cache = _read_team_cache(store_dir, repo)
            rows = cache.get("decisions")
            count = len(rows) if isinstance(rows, list) else 0
            print(f"    cache:      {count} decision(s), cursor={cache.get('cursor') or '(none)'}")
            last_sync = cache.get("last_sync")
            if not isinstance(last_sync, dict):
                print("    last sync:  never")
            else:
                outcome = "ok" if last_sync.get("ok") else "failed"
                age = _format_age(time.time() - _num(last_sync.get("at"), time.time()))
                print(f"    last sync:  {outcome}, {age} ago ({last_sync.get('duration_ms', 0)}ms)")
            last_render = cache.get("last_render")
            if isinstance(last_render, dict):
                kb = _num(last_render.get("chars")) / 1024
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
    if not upserted and not removed:
        # "Pulled 0 team decision(s)." is the same sentence for "nothing new upstream" and
        # "your session died three days ago", and that ambiguity is what let an expired login
        # sit unnoticed. auth_state is a local read, so naming the cause costs nothing.
        from contexer import auth, config
        state = auth.auth_state(config.load_profile())
        if state["state"] in ("expired", "refresh_failed", "none"):
            print(f"contexer: {state['message']}", file=sys.stderr)


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
        picked = _pick_shareable(repo, profile)
        if not picked:
            print("Cancelled — nothing was pushed.")
            return
        # Selecting IS the confirm here, so an unreviewed decision would otherwise go up with
        # no second look — gate it explicitly before the push.
        chosen = set(picked)
        selection = [p for p in store.get_shareable_all(repo) if (p.get("id") or "") in chosen]
        if not _confirm_unapproved(selection):
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


def _parse_selection(raw: str, loaded: int) -> tuple[list[int], list[str]]:
    """Parse a picker answer into 1-based row numbers, in the order typed, deduped.

    Accepts single numbers and inclusive ranges, mixed freely: `1,3,5-8`. A descending range
    (`8-5`) reads the same as `5-8` — either way the developer meant those four rows. A range
    that runs past the last loaded row is CLAMPED to it rather than thrown away, since with
    paging `1-20` on a 10-row page is a natural way to say "everything I can see"; the clamped
    part comes back as a note so the caller can say what it didn't take. Returns
    (row_numbers, ignored_tokens) — a token contributing nothing (a stray word, `-3`, a number
    past the end) lands in `ignored_tokens` and never silently changes the selection."""
    picked: list[int] = []
    ignored: list[str] = []
    for tok in raw.replace(" ", "").split(","):
        if not tok:
            continue
        if "-" in tok:
            lo_s, _, hi_s = tok.partition("-")
            if not (lo_s.isdigit() and hi_s.isdigit()):
                ignored.append(tok)
                continue
            lo, hi = sorted((int(lo_s), int(hi_s)))
            lo, hi = max(lo, 1), min(hi, loaded)
            if lo > hi:                      # wholly outside the loaded window
                ignored.append(tok)
                continue
            if hi < max(int(lo_s), int(hi_s)):
                ignored.append(f"{tok} (clamped to {lo}-{hi})")
            picked.extend(range(lo, hi + 1))
        elif tok.isdigit() and 1 <= int(tok) <= loaded:
            picked.append(int(tok))
        else:
            ignored.append(tok)
    seen: set[int] = set()
    return [n for n in picked if not (n in seen or seen.add(n))], ignored


def _pick_shareable(repo: str, profile) -> list:
    """Interactive numbered multi-select of shareable decisions, paged `store._SHARE_PAGE`
    at a time. `m` loads the next page — numbering stays continuous, so e.g. `11` resolves once
    page 2 is loaded; `all`'s count in the prompt label (`all (10)`, then `all (20)` after paging)
    makes explicit that it shares exactly the currently-loaded set, not the whole store — that's
    what `contexer share --all` is for, and the two used to collide silently. Selections accept
    ranges as well as single numbers (`1,3,5-8`) via `_parse_selection`. Returns the chosen
    ids ([] to cancel / nothing to share). Pure local read; no network until the caller pushes.

    DISPLAY ONLY: resorts a local copy so not-yet-shared decisions come first and already-shared
    ones last (stable within each group), and marks the latter `✓ shared` via
    `share.shared_map(profile.endpoint)` - endpoint-scoped so a marker from a different endpoint
    never bleeds through. `store.get_shareable_all`'s oldest-first order (the actual push order)
    is untouched; a shared decision stays selectable (re-sharing legitimately updates the row)."""
    from contexer import share, store

    items = store.get_shareable_all(repo)
    if not items:
        print("No decisions available to share.")
        return []
    shared = share.shared_map(profile.endpoint)
    items = sorted(items, key=lambda it: (it.get("id") or "") in shared)
    print("\nShareable decisions — pushing sends them to your PERSONAL cloud.")
    print(f"{store._SHARE_SECRETS_HINT}:\n")

    page = store._SHARE_PAGE
    shown_from = 0
    loaded = min(page, len(items))
    while True:
        for i in range(shown_from, loaded):
            print(store._share_item_block(items[i], index=i + 1,
                                          shared=(items[i].get("id") or "") in shared))
        remaining = len(items) - loaded
        if remaining:
            print(f"  …and {remaining} more (m to load, or share by id)")
        shown_from = loaded

        opts = f"e.g. 1,3,5-8 | all ({loaded})" + (" | m=more" if remaining else "") + " | q"
        try:
            raw = input(f"\nSelect to share [{opts}]: ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print()
            return []
        if raw == "m" and remaining:
            loaded = min(loaded + page, len(items))
            continue
        if raw == "all":
            return [it.get("id") or "" for it in items[:loaded]]
        if raw in ("q", "quit", ""):
            return []  # documented quit key - must not be reported as an ignored token
        rows, ignored = _parse_selection(raw, loaded)
        if ignored:
            # Say what was dropped BEFORE returning: a clamped range still pushes, and a push
            # is outward, so the developer must not learn about it only from the result count.
            print(f"  Ignored: {', '.join(ignored)}")
        return [items[n - 1].get("id") or "" for n in rows]


def _pending_review_warning(projs: list) -> list[str]:
    """Warning lines when a selection contains `pending_approval` decisions, else [].

    Scoped to `pending_approval` ONLY, matching the store's own trust boundary: auto-injection
    (`get_context(_active_only=True)`) already serves `approved` AND `suggested`, so a suggested
    decision is trusted context locally and sharing it promotes nothing new. `pending_approval`
    is the one state deliberately held back until a human reviews it — and a personal-cloud push
    AUTO-APPROVES, so pushing one silently ratifies a decision that was never reviewed. The
    status pill shows every state while picking; this gate fires only for that real hazard."""
    pending = sum(1 for p in projs if (p.get("status") or "approved") == "pending_approval")
    if not pending:
        return []
    return [
        f"  Warning: {pending} of {len(projs)} are PENDING REVIEW (not yet approved).",
        "  Pushing auto-approves them into living context in your personal cloud.",
    ]


def _confirm_unapproved(projs: list) -> bool:
    """Standalone gate for the picker path, which has no other confirm step (selecting IS the
    confirm). Returns True when nothing is pending review, or the developer says yes."""
    warning = _pending_review_warning(projs)
    if not warning:
        return True
    print()
    for line in warning:
        print(line)
    try:
        return input("  Continue? [y/N]: ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


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
        print(f"\nAbout to push {len(items)} decision(s) to your PERSONAL cloud. "
              f"{store._SHARE_SECRETS_HINT}:\n")
        for it in items[:10]:
            print(store._share_item_block(it))
        if len(items) > 10:
            print(f"  …and {len(items) - 10} more")
        selection = items
    else:
        proj = store.get_shareable(repo, ids[0] if ids else "")
        if proj is None:
            print("Nothing to share — no matching decision found.")
            return None
        print(f"\nAbout to push to your PERSONAL cloud. {store._SHARE_SECRETS_HINT}:\n")
        print(store._share_item_block(proj))
        selection = [proj]
    # Surfaced INLINE here rather than as a second prompt: this path already gates on the
    # y/N below, so one deliberate confirmation is enough (the picker path, which has no
    # such gate, asks separately via _confirm_unapproved).
    warning = _pending_review_warning(selection)
    if warning:
        print()
        for line in warning:
            print(line)
    try:
        return input("\nPush to cloud? [y/N]: ").strip().lower() in ("y", "yes")
    except (EOFError, KeyboardInterrupt):
        print()
        return False


def login_cmd(rest: list | None = None) -> None:
    """`contexer login [--endpoint URL]`: sign in to Contexer Teams via the browser (OAuth).

    Self-configuring — writes config.toml itself, so no manual setup. Endpoint defaults to prod
    (or localhost under CONTEXER_ENV=local); override with --endpoint."""
    from contexer import auth, config

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
    except (ValueError, RuntimeError, config.ConfigError) as e:
        # ValueError: bad --endpoint; RuntimeError: OAuth flow failure (state mismatch,
        # no code, no token); ConfigError: an unusable ~/.contexer/config.toml reached the
        # profile write — it is NOT a ValueError subclass, so it needs naming here or it
        # surfaces as a traceback out of a login that already spent the browser flow.
        # All three are user-actionable — print cleanly, no traceback.
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


def ui_cmd(rest: list | None = None) -> None:
    """`contexer ui [--open] [--stop] [--status] [--port N] [--foreground] [--reset-token]`.

    Starts (or reports on) the local console — a loopback web UI over every store on this
    machine. The printed URL carries a short-lived pairing code, never the console token."""
    from contexer.ui import daemon

    rest = rest or []
    if "--stop" in rest:
        print("Console stopped." if daemon.stop() else "Console was not running.")
        return
    if "--status" in rest:
        _print_ui_status(daemon.status())
        return

    port = _ui_port(rest)
    if "--foreground" in rest:
        # Imported here, not at module scope: server.py pulls in http.server and the whole
        # store, and cli.py is imported by every `contexer` invocation.
        from contexer.ui import server
        # server.main returns the process exit code: a failed bind must not look like a clean
        # run to whatever supervises `contexer ui --foreground`.
        code = server.main(["--port", str(port)])
        if code:
            sys.exit(code)
        return
    if "--reset-token" in rest:
        old = daemon.read_state()  # stop() drops the statefile, so read the port off it first
        if daemon.stop() and old is not None:
            daemon.await_port_free(old.port)
        daemon.clear_state()  # a fresh token is minted only when no statefile exists

    state = daemon.read_state()
    ours = state is not None and state.port == port and daemon.is_alive(state)
    # await_port_free, not port_occupied alone: SIGTERM is asynchronous, and `--stop` drops the
    # statefile, so the documented `contexer ui --stop && contexer ui` restart arrives while our
    # own dying daemon still holds the socket and nothing is left to recognise it by. Blaming
    # "another process" there sends the user to a different port to escape themselves.
    if not ours and daemon.port_occupied(port) and not daemon.await_port_free(port):
        print(f"Port {port} is in use by another process — the console cannot bind it.",
              file=sys.stderr)
        print(f"Pick another one: set `[ui] port` in {_ui_config_path()}, "
              f"or run `contexer ui --port N`.", file=sys.stderr)
        sys.exit(1)
    # ensure_running short-circuits on a live daemon and hands back ITS port, so without this
    # the URL printed below would name a port nothing was ever bound on. Refuse rather than
    # SIGTERM the incumbent: a mistyped --port must not kill a console someone is using, and
    # `--stop` (or `--reset-token`, which already stops it) is the explicit way to move it.
    if "--port" in rest and state is not None and state.port != port and daemon.is_alive(state):
        print(f"A console is already running on port {state.port} — there is one console per "
              f"machine, so `--port {port}` cannot apply to it.", file=sys.stderr)
        print(f"Move it: contexer ui --stop && contexer ui --port {port}", file=sys.stderr)
        sys.exit(1)

    running = daemon.ensure_running(port)
    if running is None:
        print(f"Could not start the console — see {daemon.LOG_PATH}.", file=sys.stderr)
        sys.exit(1)
    url = daemon.console_url(*running)
    print(f"Console: {url}")
    print(f"  log:          {daemon.LOG_PATH}")
    if "--open" in rest:
        import webbrowser
        webbrowser.open(url)


def _ui_config_path() -> Path:
    """config.toml under THIS invocation's home, not config.CONFIG_PATH (frozen at import) —
    same resolution status() uses, so the file we read is the file we name in errors."""
    return Path.home() / ".contexer" / "config.toml"


def _ui_port(rest: list) -> int:
    """The port `contexer ui` targets: --port wins, else `[ui] port` from config.toml."""
    from contexer import config

    if "--port" in rest:
        i = rest.index("--port")
        value = rest[i + 1] if i + 1 < len(rest) else ""
        if not value.isdigit() or not 1 <= int(value) <= 65535:
            print("contexer ui: --port requires a port number (1-65535)", file=sys.stderr)
            sys.exit(1)
        return int(value)
    try:
        return config.load_ui_settings(_ui_config_path()).port
    except config.ConfigError as e:
        print(f"contexer ui: {e}", file=sys.stderr)
        sys.exit(1)


def _print_ui_status(info: dict) -> None:
    """`contexer ui --status`, laid out like `contexer status`."""
    if info["running"]:
        state = f"running (pid {info['pid']}, started {info['started_at']})"
    elif info["stale"]:
        state = "not running (stale statefile — the next `contexer ui` replaces it)"
    else:
        state = "not running"
    print(f"contexer console {info['version'] or _version()}")
    print(f"  state:        {state}")
    print(f"  port:         {info['port']}")
    if info["url"]:
        print(f"  url:          {info['url']}")
    print(f"  statefile:    {info['state_path']}")
    print(f"  log:          {info['log_path']}")


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
    elif cmd == "ui":
        _run_guarded(lambda: ui_cmd(rest))
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
