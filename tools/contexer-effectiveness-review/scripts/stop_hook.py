#!/usr/bin/env python3
"""End-of-turn hook for Cursor (stop), Claude Code (Stop) and Codex (Stop).

When this session's turn produced a commit or ran `gh pr create`, it records what a script can
observe (git plus the host transcript) into a pending file, stores that file's digest in the
session state, and asks the agent to add its judgment through log_usage.py. Any other turn,
and any failure, prints {} and leaves the agent's turn alone.

Usage:
  stop_hook.py --host cursor|claude|codex                 (hook: JSON on stdin)
  stop_hook.py --manual --host H --session ID [--transcript PATH] [--repo PATH]
"""
import argparse
import fcntl
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import transcript  # noqa: E402
import privacy  # noqa: E402

ROOT = Path(os.environ.get("CONTEXER_USAGE_HOME") or Path.home() / ".contexer-usage")
SKILL = Path(__file__).resolve().parent.parent / "SKILL.md"
LOGGER = Path(__file__).resolve().parent / "log_usage.py"
OBSERVER_VERSION = "observe-v6"
GIT_TIMEOUT = float(os.environ.get("CONTEXER_USAGE_GIT_TIMEOUT") or 4)
_GIT_DEADLINE = None
NO_TRANSCRIPT_LOOKBACK = 900
PENDING_TTL = 7 * 86400
SEEN_LIMIT = 500
AUTOFETCH = "[Contexer: auto-fetched for"
DECISION_ID = re.compile(r"\bid=([0-9a-f]{8})")
_ENV = r"(?:\w+=\S*\s+)*"
_GIT_WRITE = r"git(?:\s+-[Cc]\s+\S+)*\s+(?:commit|merge|cherry-pick|revert|rebase|am|pull)\b"
_PR_CREATE = r"gh\s+pr\s+create\b"


class GitError(Exception):
    pass


class GitTimeout(GitError):
    pass


# ── git ──────────────────────────────────────────────────────────────────────────────

def git(repo, *args, required=False):
    timeout = GIT_TIMEOUT
    if _GIT_DEADLINE is not None:
        timeout = min(timeout, _GIT_DEADLINE - time.monotonic())
        if timeout <= 0:
            raise GitTimeout("hook git budget exhausted")
    try:
        out = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                             timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        raise GitTimeout(f"git {args[0]} over {GIT_TIMEOUT}s in {repo}") from exc
    if required and out.returncode:
        raise GitError(f"git {args[0]} failed with exit {out.returncode}")
    return out.stdout.strip() if out.returncode == 0 else ""


def repo_info(path):
    """Toplevel and canonical identity; linked worktrees share the main checkout's key."""
    top = git(path, "rev-parse", "--show-toplevel")
    if not top:
        return None
    common = git(top, "rev-parse", "--path-format=absolute", "--git-common-dir")
    canonical = str(Path(common).parent) if common.endswith("/.git") else (common or top)
    key = f"{Path(canonical).name}-{hashlib.sha1(canonical.encode()).hexdigest()[:8]}"
    return {"repo": top, "repo_key": key, "canonical": canonical}


def repos_for(paths):
    found, keys = [], set()
    for p in paths:
        if not p or not Path(p).is_dir():
            continue
        info = repo_info(p)
        if info and info["repo_key"] not in keys:
            keys.add(info["repo_key"])
            found.append(info)
    return found


def new_commits(info, since, seen, result_shas=()):
    """Configured-identity commits or commits matched to a visible Git command result."""
    repo = info["repo"]
    email = git(repo, "config", "user.email", required=not result_shas)
    if not email and not result_shas:
        raise GitError("commit attribution requires user.email")
    worktrees = git(repo, "worktree", "list", "--porcelain", required=True)
    heads = [ln.split()[1] for ln in worktrees.splitlines()
             if ln.startswith("HEAD ")]
    paths_by_head = {}
    for block in worktrees.split('\n\n'):
        fields = dict(line.split(' ', 1) for line in block.splitlines() if ' ' in line)
        if fields.get('HEAD') and fields.get('worktree'):
            paths_by_head[fields['HEAD']] = fields['worktree']
    raw = git(repo, "log", "--branches", *heads, f"--since=@{int(since)}",
              "--format=%x1e%H%x09%ct%x09%ce%x09%s", "--shortstat", required=True)
    found = []
    for block in raw.split("\x1e"):
        head, _, stat = block.strip().partition("\n")
        parts = head.split("\t", 3)
        if len(parts) != 4:
            continue
        sha, ct, ce, subject = parts
        result_matched = any(sha.startswith(prefix) for prefix in result_shas)
        if sha in seen or int(ct) < int(since) or (ce != email and not result_matched):
            continue
        found.append({"sha": sha[:12], "full_sha": sha, "time": int(ct),
                      "subject": privacy.scrub(subject)[:200], "shortstat": stat.strip(),
                      "worktree": paths_by_head.get(sha)})
    return sorted(found, key=lambda c: c["time"])


# ── transcript ───────────────────────────────────────────────────────────────────────

def read_segment(path, offset):
    """(complete lines from `offset`, new offset, reset). A partial last line is left for the
    next read; a transcript shorter than `offset` was rewritten and is read from the start."""
    if not path or not Path(path).is_file():
        return "", offset, False
    with open(path, "rb") as f:
        size = f.seek(0, 2)
        reset = offset > size
        if reset:
            offset = 0
        f.seek(offset)
        data = f.read()
    cut = data.rfind(b"\n") + 1
    return data[:cut].decode("utf-8", "replace"), offset + cut, reset


def observe(events, host):
    counts = {c: 0 for c in (*transcript.CATEGORIES, "discovery", "other")}
    calls, contexer_ids_by_call = [], {}
    captured_ids = set()
    surface_ids = {name: set() for name in ("get_context", "review_pending", "other_tool")}
    first_contexer_at, exploration = None, 0
    git_writes = pr_creates = 0
    git_call_ids, git_result_shas = set(), set()
    result_ids, autofetch_ids, hook_ids, autofetch_blocks = set(), set(), set(), 0
    opaque = False
    for ev in events:
        if ev[0] == "call":
            _, name, args, call_id, cat, ctx = ev
            if ctx:
                calls.append({"tool": ctx, "args": summarize_args(args)})
                contexer_ids_by_call[call_id] = ctx
                if first_contexer_at is None:
                    first_contexer_at = exploration
                continue
            counts[cat] += 1
            if cat in transcript.EXPLORATION:
                exploration += 1
            if cat == "shell":
                wrapped = host == "codex" and name.rsplit('.', 1)[-1] in ("exec", "js")
                opaque |= wrapped
                commands = transcript.shell_commands(args, wrapped)
                git_writes += any(re.match(_ENV + _GIT_WRITE, c) for c in commands)
                if any(re.match(_ENV + _GIT_WRITE, c) for c in commands):
                    git_call_ids.add(call_id)
                pr_creates += any(re.match(_ENV + _PR_CREATE, c) for c in commands)
            if "create_pull_request" in name.lower():
                pr_creates += 1
        elif ev[0] == "result" and ev[1] in git_call_ids:
            git_result_shas.update(re.findall(r'(?m)^\[[^\]\n]*?\b([0-9a-f]{7,40})\]', ev[2]))
        elif ev[0] == "result" and ev[1] in contexer_ids_by_call:
            ids = decision_ids(ev[2])
            result_ids.update(ids)
            tool = contexer_ids_by_call[ev[1]]
            surface = "get_context" if tool in ("get_context", "get_global_context") else (
                "review_pending" if tool == "review_pending" else "other_tool")
            surface_ids[surface].update(ids)
            if contexer_ids_by_call[ev[1]] in ("update_context", "update_global_context"):
                captured_ids.update(decision_ids(ev[2]))
        elif ev[0] == "hook_context":
            ids = DECISION_ID.findall(ev[1])
            hook_ids.update(ids)
            if AUTOFETCH in ev[1]:
                autofetch_blocks += 1
                autofetch_ids.update(ids)
    results = transcript.TOOL_RESULTS_VISIBLE[host]
    hooks = transcript.HOOK_CONTEXT_VISIBLE[host]
    return {
        "observer": OBSERVER_VERSION,
        "tool_calls": counts,
        "contexer_calls": calls,
        "contexer_call_count": None if opaque else len(calls),
        "contexer_observed_call_count": len(calls),
        "contexer_calls_complete": not opaque,
        "captured_ids": sorted(captured_ids) if results else None,
        "capture_ids_complete": results and not opaque,
        "exploration_before_first_contexer_call": None if opaque else first_contexer_at,
        "git_write_commands": git_writes,
        "git_result_commits": sorted(git_result_shas),
        "pr_create_commands": pr_creates,
        "tool_results_visible": results,
        "contexer_result_ids": sorted(result_ids) if results and not opaque else None,
        "contexer_result_ids_by_surface": ({k: sorted(v) for k, v in surface_ids.items()}
                                           if results and not opaque else None),
        "autofetch_visible": hooks,
        "autofetch_blocks": autofetch_blocks if hooks else None,
        "autofetch_ids": sorted(autofetch_ids) if hooks else None,
        "injected_ids": sorted(hook_ids) if hooks else None,
    }


def decision_ids(text):
    ids = set(DECISION_ID.findall(text))
    try:
        pending = [json.loads(text)]
    except ValueError:
        return ids
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            identity = value.get('id')
            if isinstance(identity, str) and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f-]{27})?', identity):
                if 'content' in value or 'outcome' in value:
                    ids.add(identity[:8])
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
    return ids


def summarize_args(args):
    inner = args.get("arguments") if isinstance(args.get("arguments"), dict) else args
    # Arguments can contain decision bodies or credentials; only record field names.
    fields = sorted(k for k in ("query", "entry_type", "files", "entry_id", "action", "subtype",
                                "title", "content", "source_files") if k in inner)
    return json.dumps(fields) if fields else ""


# ── state ────────────────────────────────────────────────────────────────────────────

def _safe(session):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", session)[:120]


def state_path(session):
    return ROOT / "state" / f"{_safe(session)}.json"


@contextmanager
def session_lock(session):
    path = ROOT / "state" / f"{_safe(session)}.lock"
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with privacy.open_append(path) as f:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def load_state(path, transcript_path):
    try:
        state = json.loads(path.read_text())
        if valid_state(state):
            return state
        raise ValueError("invalid state shape")
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        try:
            path.rename(path.with_suffix(f".corrupt-{int(time.time())}"))
        except OSError:
            pass
    started = time.time() - NO_TRANSCRIPT_LOOKBACK
    if transcript_path and Path(transcript_path).is_file():
        st = Path(transcript_path).stat()
        started = getattr(st, "st_birthtime", st.st_mtime)
    return {"checked_at": started, "offset": 0, "review_at": started, "review_offset": 0,
            "seen": [], "digests": {}}


def valid_state(state):
    if not isinstance(state, dict):
        return False
    for key in ("offset", "review_offset"):
        if type(state.get(key)) is not int or state[key] < 0:
            return False
    for key in ("checked_at", "review_at"):
        value = state.get(key)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            return False
    if not isinstance(state.get("seen"), list) or not all(isinstance(s, str) for s in state["seen"]):
        return False
    if not isinstance(state.get("digests"), dict):
        return False
    captures = state.get('session_captured_ids', [])
    if not isinstance(captures, list) or not all(isinstance(x, str) for x in captures):
        return False
    carry = state.get("carry", {})
    if not isinstance(carry, dict):
        return False
    commits = carry.get('git_result_commits', [])
    return (isinstance(commits, list) and all(isinstance(sha, str) and re.fullmatch(r'[0-9a-f]{7,40}', sha) for sha in commits)
            and all(type(v) is int and v >= 0 for k, v in carry.items() if k != 'git_result_commits'))


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, indent=2, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def digest(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()


def prune_pending(state):
    now = time.time()
    for p in (ROOT / "pending").glob("*.json"):
        try:
            if now - p.stat().st_mtime > PENDING_TTL:
                p.unlink(missing_ok=True)
        except OSError:
            continue  # Another logger may consume a pending file while pruning scans it.
    retained = {}
    for token, digest in state.get("digests", {}).items():
        try:
            (ROOT / "pending" / f"{token}.json").stat()
        except FileNotFoundError:
            continue
        except OSError:
            pass  # Unreadable is not proof that an observation was consumed.
        retained[token] = digest
    state["digests"] = retained


# ── review ───────────────────────────────────────────────────────────────────────────

def build_pending(state, host, session, model, transcript_path, commits_by_repo, attribution,
                  pr_seen, kind):
    text, to_offset, _ = read_segment(transcript_path, state["review_offset"])
    observed = observe(transcript.parse(text, host), host)
    available = bool(transcript_path and Path(transcript_path).is_file())
    observed['transcript_available'] = available
    if not available:
        for key in ('contexer_call_count', 'contexer_result_ids', 'contexer_result_ids_by_surface', 'autofetch_blocks',
                    'autofetch_ids', 'injected_ids', 'captured_ids', 'git_write_commands',
                    'pr_create_commands'):
            observed[key] = None
        observed['tool_calls'] = {key: None for key in observed['tool_calls']}
        for key in ('contexer_calls_complete', 'tool_results_visible', 'autofetch_visible',
                    'capture_ids_complete'):
            observed[key] = False
    full_text, _, _ = read_segment(transcript_path, 0)
    full_observed = observe(transcript.parse(full_text, host), host)
    session_captures = set(state.get("session_captured_ids", []))
    session_captures.update(full_observed["captured_ids"] or [])
    observed["session_captured_ids"] = sorted(session_captures)
    state["session_captured_ids"] = sorted(session_captures)
    groups = [g for g in commits_by_repo if g["commits"]] or commits_by_repo[:1]
    groups.sort(key=lambda g: max((c["time"] for c in g["commits"]), default=0), reverse=True)
    primary = groups[0] if groups else {"repo": "", "repo_key": "unknown", "commits": []}
    latest = max(primary['commits'], key=lambda c: c['time'], default={})
    repo = latest.get('worktree') or primary["repo"]
    token = uuid.uuid4().hex[:12]
    pending = {
        "token": token,
        "kind": kind,
        "host": host,
        "session_id": session,
        "model": model,
        "transcript_path": transcript_path,
        "repo": repo,
        "canonical_repo": primary.get("canonical", repo),
        "repo_key": primary["repo_key"],
        "branch": git(repo, "rev-parse", "--abbrev-ref", "HEAD") if repo else "",
        "remote": git(repo, "remote", "get-url", "origin") if repo else "",
        "segment": {"from_offset": state["review_offset"], "to_offset": to_offset, "since": state["review_at"],
                    "transcript_reset": bool(state.get("transcript_reset"))},
        "trigger": {
            "attribution": attribution,
            "commits": [{k: c[k] for k in ("sha", "subject", "shortstat")}
                        for c in primary["commits"]],
            "other_repos": [{"repo": g["repo"], "repo_key": g["repo_key"],
                             "commits": [c["sha"] for c in g["commits"]]}
                            for g in groups[1:] if g["commits"]],
            "pr_create_seen": pr_seen,
        },
        "observed": observed,
        "created_at": time.time(),
    }
    pending = privacy.scrub(pending)
    write_json(ROOT / "pending" / f"{token}.json", pending)
    state.setdefault("digests", {})[token] = digest(pending)
    return pending


def prompt(pending):
    trig = pending["trigger"]
    shas = ", ".join(c["sha"][:8] for c in trig["commits"])
    parts = [f"commit(s) {shas}"] if shas else []
    if trig["pr_create_seen"]:
        parts.append("a PR-creation command")
    what = " and ".join(parts) or "no commit"
    return (f"Contexer effectiveness review due: this turn recorded {what}. Read {SKILL} and follow "
            f"it now, before any other work. host={pending['host']} "
            f"session_id={pending['session_id']} token={pending['token']} "
            f"pending={ROOT / 'pending' / (pending['token'] + '.json')} logger={LOGGER}")


def respond(host, message):
    if not message:
        print("{}")
    elif host == "cursor":
        print(json.dumps({"followup_message": message}))
    else:
        print(json.dumps({"decision": "block", "reason": message}))


def cursor_payload(data):
    return any(k in data for k in ("cursor_version", "conversation_id", "workspace_roots"))


def run_hook(host, data):
    global _GIT_DEADLINE
    if host != "cursor" and cursor_payload(data):
        return None  # Cursor also runs Claude-format hooks; the native cursor hook owns it
    session = data.get("conversation_id") or data.get("session_id")
    if not session:
        return None
    transcript_path = data.get("transcript_path")
    review_turn = bool(data.get("stop_hook_active") or data.get("loop_count", 0))
    guarded = review_turn or (host == "cursor" and data.get("status", "completed") != "completed")
    with session_lock(session):
        path = state_path(session)
        state = load_state(path, transcript_path)
        previous = state.get("transcript_digest")
        current = transcript_digest(transcript_path, state["offset"])
        if previous is not None and current is not None and current != previous:
            state.update(offset=0, review_offset=0, transcript_reset=True)
        text, end, reset = read_segment(transcript_path, state["offset"])
        if reset:
            state.update(review_offset=0, transcript_reset=True)
        state.update(offset=end, host=host)
        state["transcript_digest"] = transcript_digest(transcript_path, end)
        seg = observe(transcript.parse(text, host), host)
        _GIT_DEADLINE = time.monotonic() + 7
        try:
            if guarded:
                # Keep the commit window open: the next normal turn reviews commits made here.
                add_carry(state, seg, include_pr=not review_turn)
                return None
            return review_if_due(state, host, session, data, transcript_path, seg, end)
        except (OSError, ValueError) as exc:
            add_carry(state, seg)
            log_error(host, exc)
            return None
        finally:
            _GIT_DEADLINE = None
            try:
                prune_pending(state)
            except OSError as exc:
                log_error(host, exc)
            write_json(path, state)


def transcript_digest(path, end):
    if not path or not Path(path).is_file():
        return None
    hasher = hashlib.sha256()
    with open(path, 'rb') as stream:
        remaining = end
        while remaining:
            chunk = stream.read(min(remaining, 1024 * 1024))
            if not chunk:
                break
            hasher.update(chunk)
            remaining -= len(chunk)
    return hasher.hexdigest()


def add_carry(state, seg, include_pr=True):
    carry = state.setdefault("carry", {})
    for key in ("git_write_commands", "pr_create_commands") if include_pr else (
            "git_write_commands",):
        carry[key] = carry.get(key, 0) + seg[key]
    for key in ("shell", "subagent"):
        carry[key] = carry.get(key, 0) + seg["tool_calls"][key]
    carry['git_result_commits'] = sorted(set(carry.get('git_result_commits', [])) | set(seg.get('git_result_commits', [])))


def unresolved_pending(state, host, session):
    """Reoffer immutable unlogged evidence; never synthesize a new token for the same sample."""
    for token, expected in state.get('digests', {}).items():
        path = ROOT / 'pending' / f'{token}.json'
        try:
            pending = json.loads(path.read_text())
        except FileNotFoundError:
            continue
        if (digest(pending) != expected or pending.get('host') != host
                or pending.get('session_id') != session or pending.get('kind') != 'hook'
                or time.time() - pending['created_at'] >= PENDING_TTL):
            continue
        records = ROOT / 'records' / f"{pending['repo_key']}.jsonl"
        logged = False
        if records.is_file():
            with records.open() as stream:
                for line in stream:
                    try:
                        row = json.loads(line)
                        logged |= isinstance(row, dict) and row.get('record_key') == f'{session}|{token}'
                    except ValueError:
                        continue
        if logged:
            path.unlink(missing_ok=True)
        else:
            return pending
    return None


def attribute(transcript_path, evidence):
    """How this session is connected to new commits, or None when it plainly is not."""
    if not transcript_path or not Path(transcript_path).is_file():
        return "git_only"
    if evidence["git_write_commands"]:
        return "transcript"
    if evidence["subagent"]:
        return "subagent"
    if evidence["shell"]:
        return "unverified"  # a script, alias or tool may have committed
    return None


def review_if_due(state, host, session, data, transcript_path, seg, end):
    unresolved = unresolved_pending(state, host, session)
    if unresolved:
        add_carry(state, seg)
        return prompt(unresolved)
    carry = state.get("carry", {})
    evidence = {
        "git_write_commands": seg["git_write_commands"] + carry.get("git_write_commands", 0),
        "pr_create_commands": seg["pr_create_commands"] + carry.get("pr_create_commands", 0),
        "shell": seg["tool_calls"]["shell"] + carry.get("shell", 0),
        "subagent": seg["tool_calls"]["subagent"] + carry.get("subagent", 0),
    }
    now = time.time()
    seen = set(state.get("seen", []))
    message = None
    try:
        infos = repos_for([data.get("cwd"), *(data.get("workspace_roots") or [])])
        if not infos:
            infos = repos_for([os.getcwd()])
        result_shas = sorted(set(seg.get('git_result_commits', [])) | set(carry.get('git_result_commits', [])))
        groups = [dict(info, commits=new_commits(info, state["checked_at"], seen, result_shas))
                  for info in infos]
        found = [c for g in groups for c in g["commits"]]
        attribution = attribute(transcript_path, evidence)
        commits_ok = bool(found) and attribution is not None
        pr_seen = evidence["pr_create_commands"] > 0
        if commits_ok or pr_seen:
            if not commits_ok:
                groups = [dict(g, commits=[]) for g in groups]
            pending = build_pending(state, host, session, data.get("model"), transcript_path,
                                    groups, attribution if commits_ok else "pr_only", pr_seen,
                                    "hook")
            message = prompt(pending)
    except GitError as exc:
        log_error(host, exc)
        add_carry(state, seg)  # nothing advances; the next stop retries the same window
        return None
    if message:
        state.update(review_at=now, review_offset=end, transcript_reset=False)
    state["seen"] = (state.get("seen", []) + [c["full_sha"] for c in found])[-SEEN_LIMIT:]
    state.update(checked_at=now, carry={})
    return message


def run_manual(args):
    with session_lock(args.session):
        path = state_path(args.session)
        state = load_state(path, args.transcript)
        infos = repos_for([args.repo or os.getcwd()])
        groups = [dict(i, commits=new_commits(i, state["review_at"], set(state["seen"]))) for i in infos]
        pending = build_pending(state, args.host, args.session, args.model, args.transcript,
                                groups, "manual", False, "manual")
        _, end, _ = read_segment(args.transcript, 0)
        now = time.time()
        state.update(checked_at=now, offset=end, review_at=now, review_offset=end)
        state["transcript_digest"] = transcript_digest(args.transcript, end)
        state["seen"] = (state["seen"] + [c["full_sha"] for g in groups for c in g["commits"]])[-SEEN_LIMIT:]
        state["carry"] = {}
        write_json(path, state)
    print(prompt(pending))


def log_error(host, exc):
    try:
        ROOT.mkdir(parents=True, exist_ok=True)
        with privacy.open_append(ROOT / "hook-errors.log") as f:
            f.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {host} {exc!r}\n")
    except OSError:
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", required=True, choices=["cursor", "claude", "codex"])
    parser.add_argument("--manual", action="store_true")
    parser.add_argument("--session")
    parser.add_argument("--transcript")
    parser.add_argument("--repo")
    parser.add_argument("--model")
    args = parser.parse_args()
    if args.manual:
        if not args.session:
            parser.error("--manual needs --session")
        run_manual(args)
        return
    message = None
    try:
        data = json.loads(sys.stdin.read() or "{}")
        message = run_hook(args.host, data if isinstance(data, dict) else {})
    except Exception as exc:  # a broken review must never break the agent's turn
        log_error(args.host, exc)
    respond(args.host, message)


if __name__ == "__main__":
    main()
