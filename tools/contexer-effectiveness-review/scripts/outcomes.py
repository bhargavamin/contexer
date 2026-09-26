#!/usr/bin/env python3
"""Attach later observed status to logged records: PR state, CI result, reverted work.

Appends a row to outcomes.jsonl only when a record's outcome changed since its last row;
summarize.py reads the latest row per record. Records are never rewritten.
Usage: outcomes.py [--repo-key KEY]
"""
import argparse
import fcntl
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from log_usage import ROOT, pr_lookup  # noqa: E402

OUTCOMES = ROOT / "outcomes.jsonl"
_FETCHED = set()


def run(cmd, cwd=None):
    try:
        out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def checks_conclusion(rollup):
    if not isinstance(rollup, list) or any(not isinstance(c, dict) for c in rollup):
        return "unknown"
    states = {str(c.get("conclusion") or c.get("state") or "").upper() for c in rollup or []}
    if not states:
        return "none"
    if states & {"FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED"}:
        return "failure"
    if states <= {"SUCCESS", "NEUTRAL", "SKIPPED"}:
        return "success"
    return "pending"


def pr_outcome(url, repo):
    raw = run(["gh", "pr", "view", url, "--json",
               "state,mergedAt,statusCheckRollup,mergeCommit"],
              cwd=repo if repo and Path(repo).is_dir() else None)
    try:
        data = json.loads(raw) if raw else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {"pr_state": "unknown"}, None
    merge_data = data.get("mergeCommit")
    merge = merge_data.get("oid") if isinstance(merge_data, dict) else None
    state = str(data.get("state", "unknown")).lower()
    return {"pr_state": state if state in ("open", "closed", "merged") else "unknown",
            "merged_at": data.get("mergedAt"),
            "checks": checks_conclusion(data.get("statusCheckRollup"))}, merge


def reverted(repo, shas):
    """Shas (branch commits or the squash/merge commit) that a later commit reverts."""
    if not repo or not Path(repo).is_dir():
        return None
    if not shas:
        return None
    if repo not in _FETCHED:
        remotes = run(["git", "-C", repo, "remote"])
        if remotes is None:
            return None
        if 'origin' in remotes.splitlines() and run(["git", "-C", repo, "fetch", "--quiet", "origin"]) is None:
            return None
        _FETCHED.add(repo)
    hits = []
    for sha in shas:
        out = run(["git", "-C", repo, "log", "--all", "--format=%h",
                   "--fixed-strings", f"--grep=This reverts commit {sha}"])
        if out is None:
            return None
        if out and out.strip():
            hits.append(sha)
    return hits


def outcome_for(rec):
    row = {"record_id": rec["record_id"]}
    url, repo = rec.get("pr_url"), rec.get("repo") or ""
    canonical = rec.get("canonical_repo")
    if not Path(repo).is_dir() and isinstance(canonical, str) and Path(canonical).is_dir():
        repo = canonical
    if not url and rec.get("pr_lookup", "error") != "no_branch":
        url, status = pr_lookup(repo, rec.get("branch"))
        row["pr_lookup"] = status
    merge = None
    if url:
        state, merge = pr_outcome(url, repo)
        row.update(state, pr_url=url)
    elif row.get("pr_lookup", rec.get("pr_lookup")) in ("none", "no_branch"):
        row["pr_state"] = "no_pr"
    else:
        row["pr_state"] = "unknown"
    shas = [c["sha"] for c in rec.get("trigger", {}).get("commits", [])] + ([merge] if merge else [])
    row["reverted"] = reverted(repo, shas)
    return row


def latest_rows():
    latest = {}
    if OUTCOMES.is_file():
        for line in OUTCOMES.read_text().splitlines():
            try:
                row = json.loads(line)
                if isinstance(row, dict) and isinstance(row.get("record_id"), str):
                    latest[row["record_id"]] = row
            except (ValueError, KeyError):
                continue
    return latest


def append_changed(row):
    OUTCOMES.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTCOMES, 'a+', encoding='utf-8') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        stream.seek(0)
        prior, last_line = {}, ''
        for line in stream:
            last_line = line
            try:
                candidate = json.loads(line)
            except ValueError:
                continue
            if isinstance(candidate, dict) and candidate.get('record_id') == row['record_id']:
                prior = {k: v for k, v in candidate.items() if k != 'checked_at'}
        if prior == row:
            return False
        stream.seek(0, 2)
        if last_line and not last_line.endswith('\n'):
            stream.write('\n')
        saved = dict(row, checked_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'))
        stream.write(json.dumps(saved, sort_keys=True) + '\n')
        return True


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-key")
    args = parser.parse_args()
    checked = changed = 0
    seen = set()
    for path in sorted((ROOT / "records").glob(f"{args.repo_key or '*'}.jsonl")):
        for line in path.read_text().splitlines():
            try:
                rec = json.loads(line)
                if not isinstance(rec, dict) or not isinstance(rec.get("record_id"), str):
                    continue
                if rec['record_id'] in seen:
                    continue
                row = outcome_for(rec)
            except (ValueError, KeyError, TypeError, AttributeError):
                continue
            seen.add(rec['record_id'])
            checked += 1
            changed += append_changed(row)
    print(f"Checked {checked} record(s); {changed} outcome(s) changed")


if __name__ == "__main__":
    main()
