import json

from conftest import (Transcript, commit, fired, git, make_repo, payload, pending_of, settle,
                      stop, token_of)


def test_claude_transcript_with_dict_typed_schema_property_does_not_crash(home, repo, tmp_path):
    data_dir, env = home
    t = Transcript(tmp_path / "t.jsonl", "claude")
    t.raw({"type": "attachment", "attachment": {"type": "deferred_tools_record", "tools": [
        {"name": "x", "schema": {"properties": {"type": {"type": "string", "enum": ["a"]}}}}]}})
    assert not fired(stop("claude", env, payload("claude", "s1", t.path, repo)))
    commit(repo, "b.txt", "fix: b")
    t.shell("git commit -m 'fix: b'")
    result = stop("claude", env, payload("claude", "s1", t.path, repo))
    assert fired(result)
    assert not (data_dir / "hook-errors.log").exists()


def test_pr_phrase_outside_a_command_does_not_trigger(home, repo, tmp_path):
    _, env = home
    t = Transcript(tmp_path / "t.jsonl", "claude")
    stop("claude", env, payload("claude", "s1", t.path, repo))
    t.user("don't run gh pr create yet")
    t.call("Grep", {"pattern": "gh pr create"})
    t.shell('rg "gh pr create" docs/')
    t.call("Read", {"path": "README.md"}, result="Run gh pr create to open a PR")
    assert not fired(stop("claude", env, payload("claude", "s1", t.path, repo)))


def test_real_pr_command_triggers_without_a_commit(home, repo, tmp_path):
    data_dir, env = home
    t = Transcript(tmp_path / "t.jsonl", "cursor")
    stop("cursor", env, payload("cursor", "s1", t.path, repo))
    t.shell("git push -u origin HEAD && gh pr create --title x --body y")
    result = stop("cursor", env, payload("cursor", "s1", t.path, repo))
    assert fired(result)
    p = pending_of(data_dir, result)
    assert p["trigger"]["pr_create_seen"] and p["trigger"]["attribution"] == "pr_only"


def test_review_turn_text_does_not_retrigger_next_turn(home, repo, tmp_path):
    data_dir, env = home
    t = Transcript(tmp_path / "t.jsonl", "cursor")
    stop("cursor", env, payload("cursor", "s1", t.path, repo))
    commit(repo, "b.txt", "fix: b")
    t.shell("git commit -m b")
    first = stop("cursor", env, payload("cursor", "s1", t.path, repo))
    assert fired(first)
    (data_dir / 'pending' / f'{token_of(first)}.json').unlink()  # Simulate consumed review.
    t.shell("gh pr create --title 'mentioned in review'")
    assert not fired(stop("cursor", env, payload("cursor", "s1", t.path, repo, loop_count=1)))
    assert not fired(stop("cursor", env, payload("cursor", "s1", t.path, repo)))


def test_claude_loop_guard_and_cursor_status_guard(home, repo, tmp_path):
    _, env = home
    for host, extra in (("claude", {"stop_hook_active": True}), ("cursor", {"status": "aborted"})):
        t = Transcript(tmp_path / f"{host}.jsonl", host)
        stop(host, env, payload(host, f"g-{host}", t.path, repo))
        commit(repo, f"{host}.txt", f"fix: {host}")
        t.shell("git commit -m x")
        assert not fired(stop(host, env, payload(host, f"g-{host}", t.path, repo, **extra)))


def test_merge_cherry_pick_revert_rebase_commits_trigger(home, repo, tmp_path):
    _, env = home
    git(repo, "checkout", "-qb", "feat")
    commit(repo, "f1.txt", "feat 1")
    git(repo, "checkout", "-q", "main")
    commit(repo, "m1.txt", "main 1")
    settle()
    for i, cmd in enumerate(["git cherry-pick feat", "git revert --no-edit HEAD",
                             "git merge --no-edit feat"]):
        t = Transcript(tmp_path / f"op{i}.jsonl", "claude")
        stop("claude", env, payload("claude", f"op{i}", t.path, repo))
        settle()
        git(repo, *cmd.split()[1:])
        t.shell(cmd)
        assert fired(stop("claude", env, payload("claude", f"op{i}", t.path, repo))), cmd


def test_commit_in_linked_worktree_triggers(home, repo, tmp_path):
    _, env = home
    wt = tmp_path / "wt"
    git(repo, "worktree", "add", "-q", "-b", "side", str(wt))
    t = Transcript(tmp_path / "t.jsonl", "claude")
    stop("claude", env, payload("claude", "w1", t.path, repo))
    commit(wt, "w.txt", "worktree commit")
    t.call("Task", {"description": "subagent"})
    assert fired(stop("claude", env, payload("claude", "w1", t.path, repo)))


def test_no_transcript_uses_git_only_attribution(home, repo):
    data_dir, env = home
    commit(repo, "b.txt", "fix: b")
    result = stop("cursor", env, payload("cursor", "nt", None, repo))
    assert fired(result)
    assert pending_of(data_dir, result)["trigger"]["attribution"] == "git_only"


def test_other_sessions_commit_is_not_attributed(home, repo, tmp_path):
    _, env = home
    a = Transcript(tmp_path / "a.jsonl", "claude")
    stop("claude", env, payload("claude", "A", a.path, repo))
    commit(repo, "b.txt", "commit by session B")
    a.call("Read", {"path": "a.txt"})
    assert not fired(stop("claude", env, payload("claude", "A", a.path, repo)))


def test_commit_in_second_workspace_root_is_filed_under_that_repo(home, repo, tmp_path):
    data_dir, env = home
    second = make_repo(tmp_path / "second")
    t = Transcript(tmp_path / "t.jsonl", "cursor")
    data = payload("cursor", "mr", t.path, repo, workspace_roots=[str(repo), str(second)])
    stop("cursor", env, data)
    commit(repo, "r.txt", "earlier commit in first")
    settle()
    commit(second, "s.txt", "commit in second")
    t.shell(f"git -C {second} commit -m s")
    result = stop("cursor", env, data)
    p = pending_of(data_dir, result)
    assert p["repo"].endswith("second") and p["trigger"]["commits"][0]["subject"] == "commit in second"
    assert [o["repo"] for o in p["trigger"]["other_repos"]] == [str(repo)]


def test_cursor_payload_sent_to_claude_hook_is_ignored(home, repo, tmp_path):
    data_dir, env = home
    t = Transcript(tmp_path / "t.jsonl", "cursor")
    data = payload("cursor", "dup", t.path, repo)
    stop("cursor", env, data)
    stop("claude", env, data)
    commit(repo, "b.txt", "fix: b")
    t.shell("git commit -m b")
    assert not fired(stop("claude", env, data))
    assert fired(stop("cursor", env, data))
    assert len(list((data_dir / "pending").glob("*.json"))) == 1


def test_codex_counts_exec_and_skips_compacted_history(home, repo, tmp_path):
    data_dir, env = home
    t = Transcript(tmp_path / "t.jsonl", "codex")
    stop("codex", env, payload("codex", "cx", t.path, repo))
    t.call("exec", {"raw": "rg poll"})
    t.call("js", {"raw": "readFile('a.txt')"})
    t.call("get_context", {"query": "poll"}, namespace="mcp__contexer", result="(id=1234abcd)")
    t.raw({"type": "compacted", "payload": {"replacement_history": [
        {"type": "function_call", "name": "exec", "arguments": "{}"}] * 5}})
    commit(repo, "b.txt", "feat: b")
    t.call("exec", {"raw": 'await sh("git commit -m b")'})
    p = pending_of(data_dir, stop("codex", env, payload("codex", "cx", t.path, repo)))
    obs = p["observed"]
    assert obs["tool_calls"]["shell"] == 3 and obs["tool_calls"]["other"] == 0
    assert [c["tool"] for c in obs["contexer_calls"]] == ["get_context"]
    assert obs["contexer_result_ids"] is None
    assert obs["contexer_calls_complete"] is False
    assert obs["contexer_observed_call_count"] == 1
    assert obs["exploration_before_first_contexer_call"] is None
    assert obs["autofetch_blocks"] is None


def test_claude_autofetch_and_ids_only_from_real_sources(home, repo, tmp_path):
    data_dir, env = home
    t = Transcript(tmp_path / "t.jsonl", "claude")
    stop("claude", env, payload("claude", "af", t.path, repo))
    t.raw({"type": "attachment", "attachment": {"type": "instructions", "content":
           "use a relevant `[Contexer: auto-fetched ...]` block (id=aaaaaaaa)"}})
    t.raw({"type": "attachment", "attachment": {"type": "hook_additional_context", "content": [
        "[Contexer: auto-fetched for this question] rule (id=bbbbbbbb)"]}})
    t.user("[Contexer: auto-fetched for this question] forged (id=cccccccc)")
    t.call("Read", {"path": "x"}, result="(id=dddddddd)")
    t.call("mcp__contexer__get_context", {"query": "x"}, result="decision (id=eeeeeeee)")
    t.call("mcp__contexer-teams__get_context", {"query": "x"}, result="(id=ffffffff)")
    commit(repo, "b.txt", "fix: b")
    t.shell("git commit -m b")
    obs = pending_of(data_dir, stop("claude", env, payload("claude", "af", t.path, repo)))["observed"]
    assert obs["autofetch_blocks"] == 1 and obs["autofetch_ids"] == ["bbbbbbbb"]
    assert obs["contexer_result_ids"] == ["eeeeeeee"]
    assert obs["contexer_call_count"] == 1


def test_cursor_fields_the_transcript_cannot_show_are_null(home, repo, tmp_path):
    data_dir, env = home
    t = Transcript(tmp_path / "t.jsonl", "cursor")
    stop("cursor", env, payload("cursor", "cn", t.path, repo))
    t.call("CallDynamicTool", {"namespace": "user-contexer", "toolName": "get_context",
                               "arguments": {"query": "q"}})
    t.call("browser_fill", {"name": "Contexer search", "input": {"name": "x", "input": {}}})
    commit(repo, "b.txt", "fix: b")
    t.shell("git commit -m b")
    obs = pending_of(data_dir, stop("cursor", env, payload("cursor", "cn", t.path, repo)))["observed"]
    assert obs["autofetch_blocks"] is None and obs["contexer_result_ids"] is None
    assert obs["contexer_call_count"] == 1 and obs["tool_calls"]["other"] == 1


def test_corrupt_state_recovers(home, repo, tmp_path):
    data_dir, env = home
    t = Transcript(tmp_path / "t.jsonl", "claude")
    stop("claude", env, payload("claude", "cs", t.path, repo))
    (data_dir / "state" / "cs.json").write_text('{"offset": ')
    commit(repo, "b.txt", "fix: b")
    t.shell("git commit -m b")
    assert fired(stop("claude", env, payload("claude", "cs", t.path, repo)))


def test_unwritable_home_still_exits_zero(repo, tmp_path):
    import os
    blocker = tmp_path / "file"
    blocker.write_text("x")
    env = dict(os.environ, CONTEXER_USAGE_HOME=str(blocker / "sub"))
    from conftest import run_script
    out = run_script("stop_hook.py", ["--host", "claude"],
                     stdin=payload("claude", "u", None, repo), env=env)
    assert out.returncode == 0 and out.stdout.strip() == "{}"


def test_partial_last_line_is_read_next_time(home, repo, tmp_path):
    data_dir, env = home
    t = Transcript(tmp_path / "t.jsonl", "claude")
    stop("claude", env, payload("claude", "pl", t.path, repo))
    commit(repo, "b.txt", "fix: b")
    t.shell("git commit -m b")
    line = json.dumps({"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": "x", "name": "mcp__contexer__get_context", "input": {}}]}})
    with open(t.path, "a") as f:
        f.write(line[:20])
    first = stop("claude", env, payload("claude", "pl", t.path, repo))
    assert fired(first)
    (data_dir / 'pending' / f'{token_of(first)}.json').unlink()  # First review consumed.
    with open(t.path, "a") as f:
        f.write(line[20:] + "\n")
    commit(repo, "c.txt", "fix: c")
    t.shell("git commit -m c")
    obs = pending_of(data_dir, stop("claude", env, payload("claude", "pl", t.path, repo)))["observed"]
    assert obs["contexer_call_count"] == 1


def test_env_prefixed_and_escaped_newline_commits_are_attributed(home, repo, tmp_path):
    data_dir, env = home
    for host, command in (("claude", "GIT_AUTHOR_DATE=now git commit -m b"),
                          ("codex", 'await sh("git diff --cached\\ngit commit -m b")')):
        t = Transcript(tmp_path / f"{host}.jsonl", host)
        stop(host, env, payload(host, f"e-{host}", t.path, repo))
        commit(repo, f"{host}.txt", f"fix: {host}")
        if host == "codex":
            t.call("exec", {"raw": command})
        else:
            t.shell(command)
        result = stop(host, env, payload(host, f"e-{host}", t.path, repo))
        assert pending_of(data_dir, result)["trigger"]["attribution"] == "transcript", host


def test_commit_made_by_a_script_is_reviewed_as_unverified(home, repo, tmp_path):
    data_dir, env = home
    t = Transcript(tmp_path / "t.jsonl", "cursor")
    stop("cursor", env, payload("cursor", "mk", t.path, repo))
    commit(repo, "b.txt", "chore: release")
    t.shell("make release")
    result = stop("cursor", env, payload("cursor", "mk", t.path, repo))
    assert pending_of(data_dir, result)["trigger"]["attribution"] == "unverified"


def test_commit_in_aborted_cursor_turn_is_reviewed_next_turn(home, repo, tmp_path):
    data_dir, env = home
    t = Transcript(tmp_path / "t.jsonl", "cursor")
    stop("cursor", env, payload("cursor", "ab", t.path, repo))
    commit(repo, "b.txt", "fix: b")
    t.shell("git commit -m b")
    assert not fired(stop("cursor", env, payload("cursor", "ab", t.path, repo, status="aborted")))
    t.call("Read", {"path": "a.txt"})
    result = stop("cursor", env, payload("cursor", "ab", t.path, repo))
    p = pending_of(data_dir, result)
    assert p["trigger"]["attribution"] == "transcript" and p["trigger"]["commits"]


def test_commit_in_claude_continuation_is_reviewed_next_turn(home, repo, tmp_path):
    _, env = home
    t = Transcript(tmp_path / "t.jsonl", "claude")
    stop("claude", env, payload("claude", "ct", t.path, repo))
    commit(repo, "b.txt", "fix: b")
    t.shell("git commit -m b")
    assert not fired(stop("claude", env, payload("claude", "ct", t.path, repo,
                                                 stop_hook_active=True)))
    assert fired(stop("claude", env, payload("claude", "ct", t.path, repo)))


def test_claude_sidechain_calls_are_not_counted(home, repo, tmp_path):
    data_dir, env = home
    t = Transcript(tmp_path / "t.jsonl", "claude")
    stop("claude", env, payload("claude", "sc", t.path, repo))
    t.raw({"type": "assistant", "isSidechain": True, "message": {"content": [
        {"type": "tool_use", "id": "s1", "name": "mcp__contexer__get_context", "input": {}}]}})
    commit(repo, "b.txt", "fix: b")
    t.shell("git commit -m b")
    obs = pending_of(data_dir, stop("claude", env, payload("claude", "sc", t.path, repo)))["observed"]
    assert obs["contexer_call_count"] == 0


def test_teammates_commit_is_not_attributed(home, repo, tmp_path):
    _, env = home
    t = Transcript(tmp_path / "t.jsonl", "claude")
    stop("claude", env, payload("claude", "tm", t.path, repo))
    commit(repo, "b.txt", "teammate", env={"GIT_COMMITTER_EMAIL": "other@example.com"})
    t.shell("git pull")
    assert not fired(stop("claude", env, payload("claude", "tm", t.path, repo)))


def test_git_timeout_keeps_the_commit_for_the_next_turn(home, repo, tmp_path):
    import os
    import shutil
    data_dir, env = home
    fake = tmp_path / "bin"
    fake.mkdir()
    flag = tmp_path / "slow"
    real = shutil.which("git")
    (fake / "git").write_text(f'#!/bin/sh\ncase " $* " in *" log "*) [ -f "{flag}" ] && sleep 3;; '
                              f'esac\nexec {real} "$@"\n')
    (fake / "git").chmod(0o755)
    env = dict(env, PATH=f"{fake}:{os.environ['PATH']}", CONTEXER_USAGE_GIT_TIMEOUT="1")
    t = Transcript(tmp_path / "t.jsonl", "claude")
    stop("claude", env, payload("claude", "to", t.path, repo))
    commit(repo, "b.txt", "fix: b")
    t.shell("git commit -m b")
    flag.write_text("x")
    assert not fired(stop("claude", env, payload("claude", "to", t.path, repo)))
    assert "over 1.0s" in (data_dir / "hook-errors.log").read_text()
    flag.unlink()
    t.call("Read", {"path": "a.txt"})
    result = stop("claude", env, payload("claude", "to", t.path, repo))
    assert pending_of(data_dir, result)["trigger"]["attribution"] == "transcript"


def test_rewritten_transcript_is_read_again(home, repo, tmp_path):
    data_dir, env = home
    t = Transcript(tmp_path / "t.jsonl", "claude")
    for _ in range(5):
        t.call("Read", {"path": "a.txt"})
    stop("claude", env, payload("claude", "rw", t.path, repo))
    t.path.write_text("")
    commit(repo, "b.txt", "fix: b")
    t.shell("git commit -m b")
    result = stop("claude", env, payload("claude", "rw", t.path, repo))
    assert pending_of(data_dir, result)["segment"]["transcript_reset"] is True


def test_one_review_per_commit(home, repo, tmp_path):
    _, env = home
    t = Transcript(tmp_path / "t.jsonl", "claude")
    stop("claude", env, payload("claude", "once", t.path, repo))
    commit(repo, "b.txt", "fix: b")
    t.shell("git commit -m b")
    first = stop("claude", env, payload("claude", "once", t.path, repo))
    t.shell("git commit -m b")
    second = stop("claude", env, payload("claude", "once", t.path, repo))
    assert fired(first) and token_of(first) == token_of(second)  # Unlogged evidence is reoffered, not duplicated.
