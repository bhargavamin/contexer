#!/usr/bin/env python3
"""Validate the agent's judgment, merge it with the script-observed facts, append one record.

Usage: log_usage.py --token TOKEN < judgment.json
       log_usage.py --check [--token TOKEN] < judgment.json   (validate only, writes nothing)
Exit 2 with a list of problems when the judgment does not match the schema, contradicts the
observed facts, or the pending file was edited after the hook wrote it.
"""
import argparse
import fcntl
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import stop_hook  # noqa: E402
import privacy  # noqa: E402

ROOT = stop_hook.ROOT
SCHEMA = "contexer-effectiveness/v2"

FEATURES = ["session_start_rules", "autofetch", "get_context", "update_context",
            "capture_reminder", "prompt_constraint_capture", "review_pending", "review_nudge",
            "bootstrap", "guard", "team_context", "console_ui", "other"]

ENUMS = {
    "task.category": ["bugfix", "feature", "refactor", "test", "docs", "investigation", "review",
                      "release", "config", "other"],
    "task.difficulty": ["trivial", "moderate", "hard"],
    "task.question_type": ["rationale", "convention", "constraint", "code_behavior", "how_to",
                           "none"],
    "task.knowledge_location": ["in_code", "in_docs", "in_git_history", "unwritten", "external",
                                "mixed"],
    "task.scope": ["single_file", "module", "cross_module", "cross_repo", "no_code"],
    "key_facts.source": ["contexer", "code", "docs", "git_history", "tool_output", "user",
                         "model_knowledge", "web"],
    "key_facts.code_would_reveal": ["yes_quickly", "yes_slowly", "no", "unknown"],
    "contexer_items.surfaced_by": ["session_start", "autofetch", "get_context", "review_pending",
                                   "other_tool"],
    "contexer_items.relevance": ["decisive", "helpful", "redundant", "irrelevant", "misleading"],
    "gaps.kind": ["missing_decision", "stale_decision", "retrieval_miss", "noise",
                  "wrong_capture", "tool_error"],
    "captures.kind": ["decision", "constraint", "convention", "comprehension", "correction"],
    "verdict.contexer_effect": ["decisive", "helpful", "neutral", "harmful", "not_used"],
    "verdict.without_contexer": ["same", "slower", "worse", "failed", "unknown"],
    "verdict.confidence": ["low", "medium", "high"],
    "feature_ratings.feature": FEATURES,
    "feature_ratings.verdict": ["useful", "not_useful", "noise", "harmful"],
    "improvements.kind": ["new_feature", "improve_feature", "remove_or_reduce"],
    "improvements.area": FEATURES,
}
LISTS = {
    "key_facts": ["fact", "source", "code_would_reveal", "contexer_ids"],
    "contexer_items": ["id", "surfaced_by", "relevance", "created_this_session", "note"],
    "gaps": ["kind", "need"],
    "captures": ["id", "kind"],
    "feature_ratings": ["feature", "verdict", "note"],
    "improvements": ["kind", "area", "idea", "evidence"],
}
TEXT = {"key_facts": ["fact"], "contexer_items": ["note"], "gaps": ["need"],
        "feature_ratings": ["note"], "improvements": ["idea", "evidence"]}
NON_EMPTY = {"key_facts": ["fact"], "gaps": ["need"], "feature_ratings": ["note"],
             "improvements": ["idea", "evidence"]}
TASK_KEYS = ("category", "difficulty", "question_type", "knowledge_location", "scope")
CREDITING = {"decisive", "helpful"}
NOTE_LIMIT = 200
LONG_LIMIT = 300
DECISION_ID = re.compile(r"^[0-9a-f]{8}$")
SURFACE_SOURCES = {"session_start": "injected_ids", "autofetch": "autofetch_ids",
                   "get_context": "contexer_result_ids", "review_pending": "contexer_result_ids",
                   "other_tool": "contexer_result_ids"}


def _texts(j):
    """Every free-text value in a judgment, for the secret check."""
    stack, out = [j], []
    while stack:
        cur = stack.pop()
        if isinstance(cur, dict):
            stack.extend(cur.values())
        elif isinstance(cur, list):
            stack.extend(cur)
        elif isinstance(cur, str):
            out.append(cur)
    return out


def _check_lists(j, errs):
    for name, fields in LISTS.items():
        items = j.get(name)
        if not isinstance(items, list):
            errs.append(f"{name} must be a list (use [] when empty)")
            continue
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                errs.append(f"{name}[{i}] must be an object")
                continue
            for extra in item.keys() - set(fields):
                errs.append(f"{name}[{i}].{extra} is not a schema field")
            for f in fields:
                if f not in item:
                    errs.append(f"{name}[{i}].{f} is required")
            for f, value in item.items():
                allowed = ENUMS.get(f"{name}.{f}")
                if allowed and value not in allowed:
                    errs.append(f"{name}[{i}].{f} must be one of {allowed}")
            for f in TEXT.get(name, []):
                if f in item and not isinstance(item[f], str):
                    errs.append(f"{name}[{i}].{f} must be a string")
                elif len(item.get(f) or "") > NOTE_LIMIT:
                    errs.append(f"{name}[{i}].{f} is over {NOTE_LIMIT} characters")
            for f in NON_EMPTY.get(name, []):
                if isinstance(item.get(f), str) and not item[f].strip():
                    errs.append(f"{name}[{i}].{f} must not be empty")


def _check_ids(j, errs):
    for i, item in enumerate(j.get("contexer_items") or []):
        if isinstance(item, dict):
            if not isinstance(item.get("id"), str) or not DECISION_ID.fullmatch(item["id"]):
                errs.append(f"contexer_items[{i}].id must be an 8-character hex decision id")
            if not isinstance(item.get("created_this_session"), bool):
                errs.append(f"contexer_items[{i}].created_this_session must be true or false")
    for i, item in enumerate(j.get("captures") or []):
        if isinstance(item, dict) and (not isinstance(item.get("id"), str)
                                       or not DECISION_ID.fullmatch(item["id"])):
            errs.append(f"captures[{i}].id must be an 8-character hex decision id")
    for i, fact in enumerate(j.get("key_facts") or []):
        if not isinstance(fact, dict):
            continue
        ids = fact.get("contexer_ids")
        if not isinstance(ids, list) or not all(isinstance(x, str) and DECISION_ID.fullmatch(x)
                                                for x in ids):
            errs.append(f"key_facts[{i}].contexer_ids must be a list of 8-character hex ids")
        elif fact.get("source") == "contexer" and not ids:
            errs.append(f"key_facts[{i}] comes from contexer, so contexer_ids must name the "
                        "decision(s)")


def surface_ids(observed, method):
    if method in ("get_context", "review_pending", "other_tool"):
        by_surface = observed.get("contexer_result_ids_by_surface")
        return by_surface.get(method) if isinstance(by_surface, dict) else None
    return observed.get(SURFACE_SOURCES.get(method, ""))


def _check_consistency(j, observed, errs):
    items = [i for i in j.get("contexer_items") or [] if isinstance(i, dict)]
    captured = {c.get("id") for c in j.get("captures") or [] if isinstance(c, dict)}
    if observed:
        captured.update(observed.get("session_captured_ids") or observed.get("captured_ids") or [])
    for i, item in enumerate(items):
        if item.get("id") in captured and item.get("created_this_session") is False:
            errs.append(f"contexer_items[{i}] ({item['id']}) is listed in captures, so "
                        "created_this_session must be true")
        if item.get("relevance") in CREDITING and not str(item.get("note", "")).strip():
            errs.append(f"contexer_items[{i}]: a {item['relevance']} rating needs a note naming "
                        "the step it changed")
    verdict = j.get("verdict") if isinstance(j.get("verdict"), dict) else {}
    effect = verdict.get("contexer_effect")
    credited = [i for i in items if i.get("relevance") in CREDITING
                and i.get("created_this_session") is False]
    if effect in CREDITING and not credited:
        errs.append(f"verdict.contexer_effect={effect} needs at least one decisive/helpful "
                    "contexer_item that was not created in this session")
    decisive_ids = {item['id'] for item in credited if item['relevance'] == 'decisive'}
    if effect == "decisive" and not any(
            f.get("source") == "contexer" and decisive_ids.intersection(f['contexer_ids'])
            for f in j['key_facts']):
        errs.append("verdict.contexer_effect=decisive needs a key_fact linked to an existing decisive item")
    if effect == "not_used":
        if any(i.get("relevance") in CREDITING for i in items):
            errs.append("verdict.contexer_effect=not_used contradicts a decisive/helpful item")
        if observed and (observed.get("contexer_call_count") or observed.get("contexer_observed_call_count")
                         or observed.get("autofetch_blocks")):
            errs.append("verdict.contexer_effect=not_used, but the transcript shows Contexer "
                        "calls or auto-fetched blocks; use neutral if they changed nothing")
    if observed:
        for i, item in enumerate(items):
            if item.get("created_this_session"):
                continue
            source = surface_ids(observed, item.get("surfaced_by"))
            if source is not None and item.get("id") not in source:
                errs.append(f"contexer_items[{i}] ({item.get('id')}) was not in any "
                            f"{item.get('surfaced_by')} output this segment")
        expected_features = {c["tool"] for c in observed.get("contexer_calls", [])}
        aliases = {"bootstrap_context": "bootstrap", "get_global_context": "get_context",
                   "update_global_context": "update_context"}
        expected_features = {aliases.get(tool, tool) for tool in expected_features}
        expected_features &= set(FEATURES)
        if observed.get("autofetch_blocks"):
            expected_features.add("autofetch")
        rated = {item["feature"] for item in j["feature_ratings"]}
        if expected_features - rated:
            errs.append("feature_ratings missing observed features: " + ', '.join(sorted(expected_features - rated)))


def ids_verified(judgment, observed):
    """True when every surfaced id could be checked against what the transcript showed."""
    items = [i for i in judgment.get("contexer_items", []) if not i.get("created_this_session")]
    return all(surface_ids(observed, i.get("surfaced_by")) is not None
               for i in items)


def validate(j, observed=None):
    errs = []
    if not isinstance(j, dict):
        return ["judgment must be a JSON object"]
    for extra in j.keys() - (set(LISTS) | {"task", "verdict", "llm_did_better"}):
        errs.append(f"{extra} is not a schema field")
    task = j.get("task") if isinstance(j.get("task"), dict) else {}
    for extra in task.keys() - (set(TASK_KEYS) | {"summary"}):
        errs.append(f"task.{extra} is not a schema field")
    if not isinstance(task.get("summary"), str) or not task["summary"].strip():
        errs.append("task.summary must be a non-empty string")
    elif len(task["summary"]) > NOTE_LIMIT:
        errs.append(f"task.summary is over {NOTE_LIMIT} characters")
    for key in TASK_KEYS:
        if task.get(key) not in ENUMS[f"task.{key}"]:
            errs.append(f"task.{key} must be one of {ENUMS['task.' + key]}")
    _check_lists(j, errs)
    # Later cross-field checks require the validated list/object/enum shapes.
    if errs:
        return errs
    features = [item["feature"] for item in j["feature_ratings"]]
    if len(features) != len(set(features)):
        errs.append("feature_ratings must rate each feature once")
    _check_ids(j, errs)
    if not j.get("key_facts"):
        errs.append("key_facts needs at least one fact that the task's outcome depended on")
    if not j.get("feature_ratings"):
        errs.append("feature_ratings needs every Contexer feature that appeared this segment "
                    "(session start rules appear in almost every session)")
    verdict = j.get("verdict")
    if not isinstance(verdict, dict):
        errs.append("verdict is required")
        verdict = {}
    for extra in verdict.keys() - {"contexer_effect", "without_contexer", "confidence", "evidence"}:
        errs.append(f"verdict.{extra} is not a schema field")
    for key in ("contexer_effect", "without_contexer", "confidence"):
        if verdict.get(key) not in ENUMS[f"verdict.{key}"]:
            errs.append(f"verdict.{key} must be one of {ENUMS['verdict.' + key]}")
    ev = verdict.get("evidence")
    if not isinstance(ev, str) or not ev.strip() or len(ev) > LONG_LIMIT:
        errs.append(f"verdict.evidence must be a non-empty string of at most {LONG_LIMIT} characters")
    better = j.get("llm_did_better", "missing")
    if better == "missing":
        errs.append("llm_did_better is required (null when nothing applies)")
    elif better is not None and (not isinstance(better, str) or len(better) > LONG_LIMIT):
        errs.append(f"llm_did_better must be null or a string of at most {LONG_LIMIT} characters")
    if any(privacy.SECRET.search(t) for t in _texts(j)):
        errs.append("judgment text looks like it contains a secret or credential; remove it")
    if errs:
        return errs
    item_ids = {item["id"] for item in j["contexer_items"]}
    for i, fact in enumerate(j["key_facts"]):
        if set(fact["contexer_ids"]) - item_ids:
            errs.append(f"key_facts[{i}].contexer_ids must reference contexer_items")
    _check_consistency(j, observed, errs)
    return errs


def load_pending(token):
    """The pending file, verified against the digest the hook stored when it wrote it."""
    if not re.fullmatch(r"[0-9a-f]{12}", token):
        raise SystemExit("Invalid pending token")
    path = ROOT / "pending" / f"{token}.json"
    if not path.is_file():
        raise SystemExit(f"No pending observation for token {token} at {path}")
    try:
        pending = json.loads(path.read_text())
        if (not isinstance(pending, dict) or pending.get("token") != token
                or not isinstance(pending.get('session_id'), str)):
            raise ValueError("invalid pending shape")
    except (OSError, ValueError) as exc:
        raise SystemExit(f"Cannot read pending observation: {exc}") from exc
    state_file = stop_hook.state_path(pending.get("session_id", ""))
    try:
        stored = json.loads(state_file.read_text()).get("digests", {}).get(token)
    except (OSError, ValueError, AttributeError):
        stored = None
    if stored != stop_hook.digest(pending):
        raise SystemExit(f"Pending file {path} does not match what the hook wrote; it was edited "
                         "or its session state is missing. Do not edit pending files.")
    return path, pending


def default_branch(repo):
    """origin's default branch; commits made directly on it have no PR of their own."""
    try:
        out = subprocess.run(["git", "-C", repo, "symbolic-ref", "--short",
                              "refs/remotes/origin/HEAD"], capture_output=True, text=True,
                             timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if out and out.returncode == 0 and "/" in out.stdout:
        return out.stdout.strip().split("/", 1)[1]
    try:
        out = subprocess.run(["git", "-C", repo, "rev-parse", "--verify", "-q", "main"],
                             capture_output=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return "main" if out.returncode == 0 else "master"


def pr_lookup(repo, branch):
    """(url or None, status): status is ok, none, no_branch, or error."""
    if not repo or not branch or branch == "HEAD":
        return None, "no_branch"
    default = default_branch(repo)
    if default is None:
        return None, "error"
    if branch == default:
        return None, "no_branch"
    try:
        out = subprocess.run(["gh", "pr", "list", "--head", branch, "--state", "all", "--json",
                              "url", "-L", "1", "-q", ".[0].url"],
                             cwd=repo, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None, "error"
    if out.returncode != 0:
        return None, "error"
    url = out.stdout.strip()
    return (url, "ok") if url else (None, "none")


def record_key(pending):
    """One record per pending observation; a second review in a session is a new token."""
    return f"{pending['session_id']}|{pending['token']}"


def append(path, record):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with privacy.open_append(path) as f:
        fcntl.flock(f, fcntl.LOCK_EX)
        f.seek(0)
        last_line = ''
        for line in f:
            last_line = line
            try:
                prior = json.loads(line)
                if isinstance(prior, dict) and prior.get("record_key") == record.get("record_key"):
                    raise SystemExit(f"A record for {record['record_key']} already exists")
            except ValueError:
                continue
        f.seek(0, 2)
        if last_line and not last_line.endswith('\n'):
            f.write('\n')
        f.write(json.dumps(record, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def record_paths(repo_key=None):
    return sorted(path for path in (ROOT / "records").glob("*.jsonl")
                  if repo_key is None or path.name == f"{repo_key}.jsonl")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token")
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    try:
        judgment = json.loads(sys.stdin.read())
    except ValueError as exc:
        print(f"judgment is not valid JSON: {exc}")
        sys.exit(2)
    pending_path = pending = None
    if args.token:
        try:
            pending_path, pending = load_pending(args.token)
        except SystemExit as exc:
            print(exc)
            sys.exit(2)
    errs = validate(judgment, pending["observed"] if pending else None)
    if errs:
        print("Judgment rejected; fix and resubmit:\n- " + "\n- ".join(errs))
        sys.exit(2)
    if args.check:
        print("ok")
        return
    if not pending:
        parser.error("--token is required (from the review prompt or stop_hook.py --manual)")
    url, status = pr_lookup(pending["repo"], pending.get("branch"))
    record = {
        "schema": SCHEMA,
        "record_id": uuid.uuid4().hex,
        "record_key": record_key(pending),
        "recorded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "kind": pending["kind"],
        "host": pending["host"],
        "model": pending.get("model"),
        "session_id": pending["session_id"],
        "repo": pending["repo"],
        "canonical_repo": pending.get("canonical_repo", pending["repo"]),
        "repo_key": pending["repo_key"],
        "remote": pending.get("remote"),
        "branch": pending.get("branch"),
        "pr_url": url,
        "pr_lookup": status,
        "trigger": pending["trigger"],
        "segment": pending["segment"],
        "observed": pending["observed"],
        "ids_verified": ids_verified(judgment, pending["observed"]),
        "judgment": judgment,
        "judged_by": "self-assessed by the working agent",
    }
    path = ROOT / "records" / f"{record['repo_key']}.jsonl"
    try:
        append(path, record)
    except SystemExit as exc:
        print(exc)
        sys.exit(2)
    pending_path.unlink()
    print(f"Logged {record['record_id']} to {path}")


if __name__ == "__main__":
    main()
