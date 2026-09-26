import json

import pytest

import log_usage
import outcomes
import stop_hook
import transcript
from conftest import Transcript, commit, fired, git, payload, pending_of, run_script, stop, token_of
from test_log_usage import example
from test_reports import record, write_records


@pytest.mark.parametrize('field', list(log_usage.LISTS))
@pytest.mark.parametrize('bad', [42, True, 'wrong', {'id': []}])
def test_wrong_list_types_are_rejected_without_crashing(field, bad):
    judgment = example()
    judgment[field] = bad
    assert log_usage.validate(judgment)


def test_unhashable_ids_are_rejected_without_crashing():
    judgment = example()
    judgment['captures'][0]['id'] = []
    assert log_usage.validate(judgment)


def test_unknown_fields_cannot_bypass_note_limits():
    judgment = example()
    judgment['key_facts'][0]['decision_body'] = 'x' * 1000
    assert log_usage.validate(judgment)


def test_fact_ids_must_reference_surfaced_items():
    judgment = example()
    judgment['key_facts'][0].update(source='contexer', contexer_ids=['deadbeef'])
    assert log_usage.validate(judgment)


def test_discovery_timeout_preserves_evidence(home, repo, tmp_path, monkeypatch):
    data_dir, _ = home
    monkeypatch.setattr(stop_hook, 'ROOT', data_dir)
    t = Transcript(tmp_path / 't.jsonl', 'claude')
    data = json.loads(payload('claude', 'timeout-discovery', t.path, repo))
    assert stop_hook.run_hook('claude', data) is None
    commit(repo, 'b.txt', 'fix: b')
    t.shell('git commit -m b')
    original = stop_hook.repo_info
    with monkeypatch.context() as patch:
        def fail(path):
            raise stop_hook.GitTimeout('discovery timed out')
        patch.setattr(stop_hook, 'repo_info', fail)
        assert stop_hook.run_hook('claude', data) is None
    assert stop_hook.repo_info is original
    message = stop_hook.run_hook('claude', data)
    assert message
    pending = next((data_dir / 'pending').glob('*.json'))
    assert json.loads(pending.read_text())['trigger']['attribution'] == 'transcript'


@pytest.mark.parametrize('bad', [{'offset': 0}, {'offset': 'oops'}, []])
def test_structurally_corrupt_state_recovers(home, repo, tmp_path, bad):
    data_dir, env = home
    t = Transcript(tmp_path / 't.jsonl', 'claude')
    stop('claude', env, payload('claude', 'bad-state', t.path, repo))
    (data_dir / 'state' / 'bad-state.json').write_text(json.dumps(bad))
    commit(repo, 'b.txt', 'fix: b')
    t.shell('git commit -m b')
    assert fired(stop('claude', env, payload('claude', 'bad-state', t.path, repo)))


@pytest.mark.parametrize('command', [
    'echo "text; gh pr create"',
    "printf '%s' 'text\ngh pr create'",
    "cat <<'EOF'\ngh pr create\nEOF",
    '# gh pr create\necho ok',
])
def test_quoted_shell_text_does_not_trigger_pr(command):
    ev = [('call', 'Bash', {'command': command}, 'c1', 'shell', None)]
    assert stop_hook.observe(ev, 'claude')['pr_create_commands'] == 0


def test_codex_search_string_is_not_a_pr_command():
    t = [('call', 'exec', {'raw': 'text(await tools.exec_command({cmd: \'rg "gh pr create" .\'}));'},
          'c1', 'shell', None)]
    assert stop_hook.observe(t, 'codex')['pr_create_commands'] == 0


def test_codex_wrapped_tools_are_unknown_not_zero():
    events = [('call', 'exec', {'raw': 'text(await tools.mcp__contexer__get_context({}));'},
               'c1', 'shell', None)]
    observed = stop_hook.observe(events, 'codex')
    assert observed['contexer_call_count'] is None
    assert observed['contexer_result_ids'] is None
    assert observed['contexer_calls_complete'] is False


def test_cursor_user_tool_shaped_content_is_not_a_call():
    text = json.dumps({'role': 'user', 'message': {'content': [
        {'type': 'tool_use', 'name': 'Shell', 'input': {'command': 'gh pr create'}}]}})
    assert transcript.parse(text, 'cursor') == []


@pytest.mark.parametrize('bad', [None, [], 42, 'text'])
def test_non_object_rows_do_not_crash_reports(home, bad):
    data_dir, env = home
    write_records(data_dir, [record(1, 'convention', 'neutral'), bad])
    (data_dir / 'outcomes.jsonl').write_text(json.dumps(bad) + '\n')
    assert run_script('summarize.py', env=env).returncode == 0
    assert run_script('outcomes.py', env=env).returncode == 0


def test_wrong_nested_types_are_skipped_in_report(home):
    data_dir, env = home
    broken = record(2, 'convention', 'neutral')
    broken['judgment']['feature_ratings'] = 42
    write_records(data_dir, [record(1, 'convention', 'neutral'), broken])
    result = run_script('summarize.py', env=env)
    assert result.returncode == 0
    assert 'Records: 1 ' in result.stdout


def test_outcomes_recheck_previously_absent_pr(monkeypatch):
    rec = record(1, 'convention', 'neutral')
    rec.update(repo='/repo', branch='feature', pr_lookup='none')
    monkeypatch.setattr(outcomes, 'pr_lookup', lambda *args: ('https://example/pr/2', 'ok'))
    monkeypatch.setattr(outcomes, 'pr_outcome', lambda *args: ({'pr_state': 'open'}, None))
    monkeypatch.setattr(outcomes, 'reverted', lambda *args: [])
    assert outcomes.outcome_for(rec)['pr_state'] == 'open'


def test_git_log_failure_is_unknown_revert_status(repo, monkeypatch):
    monkeypatch.setattr(outcomes, 'run', lambda *args, **kwargs: None)
    assert outcomes.reverted(str(repo), ['abcdef012345']) is None


def test_default_branch_missing_git_is_safe(monkeypatch):
    def fail(*args, **kwargs):
        raise FileNotFoundError('git missing')
    monkeypatch.setattr(log_usage.subprocess, 'run', fail)
    assert log_usage.pr_lookup('/repo', 'feature') == (None, 'error')


def test_manual_review_does_not_retrigger_same_commit(home, repo, tmp_path):
    data_dir, env = home
    t = Transcript(tmp_path / 't.jsonl', 'claude')
    stop('claude', env, payload('claude', 'manual', t.path, repo))
    commit(repo, 'b.txt', 'fix: b')
    t.shell('git commit -m b')
    result = run_script('stop_hook.py', ['--manual', '--host', 'claude', '--session', 'manual',
                        '--transcript', str(t.path), '--repo', str(repo)], env=env)
    assert result.returncode == 0
    t.shell('git status')
    assert not fired(stop('claude', env, payload('claude', 'manual', t.path, repo)))


def test_longer_replacement_transcript_is_read(home, repo, tmp_path):
    data_dir, env = home
    t = Transcript(tmp_path / 't.jsonl', 'claude')
    t.call('Read', {'path': 'a'})
    stop('claude', env, payload('claude', 'replace', t.path, repo))
    t.path.write_text('')
    t.shell('gh pr create --title ' + 'x' * 300)
    result = stop('claude', env, payload('claude', 'replace', t.path, repo))
    assert pending_of(data_dir, result)['segment']['transcript_reset'] is True


def test_observed_capture_cannot_be_credited_by_omitting_captures():
    j = example()
    j['captures'] = []
    j['contexer_items'] = [dict(id='1234abcd', surfaced_by='get_context', relevance='helpful',
                              created_this_session=False, note='A deciding fact')]
    j['verdict']['contexer_effect'] = 'helpful'
    observed = {'session_captured_ids': ['1234abcd'], 'contexer_result_ids': ['1234abcd']}
    assert log_usage.validate(j, observed)


def test_capture_from_previous_segment_remains_identified(home, repo, tmp_path):
    data_dir, env = home
    t = Transcript(tmp_path / 't.jsonl', 'claude')
    stop('claude', env, payload('claude', 'captured', t.path, repo))
    t.call('mcp__contexer__update_context', {'content': 'rule'}, result='Stored. id=1234abcd')
    t.shell('gh pr create --title first')
    stop('claude', env, payload('claude', 'captured', t.path, repo))
    t.call('mcp__contexer__get_context', {}, result='(id=1234abcd)')
    t.shell('gh pr create --title second')
    result = stop('claude', env, payload('claude', 'captured', t.path, repo))
    assert pending_of(data_dir, result)['observed']['session_captured_ids'] == ['1234abcd']


def test_unrated_observed_feature_is_rejected():
    j = example()
    j['verdict']['contexer_effect'] = 'neutral'
    j['feature_ratings'] = [r for r in j['feature_ratings'] if r['feature'] != 'get_context']
    assert log_usage.validate(j, {'contexer_calls': [{'tool': 'get_context'}]})


def test_unknown_codex_counts_are_not_reported_as_zero(home):
    data_dir, env = home
    rec = record(1, 'convention', 'neutral')
    rec['observed']['contexer_call_count'] = None
    write_records(data_dir, [rec])
    out = run_script('summarize.py', env=env, check=True).stdout
    assert 'unknown in 1/1 records' in out
    assert 'no Contexer tool call: 0/0' in out


def test_duplicate_outcome_rows_in_same_input_append_once(home):
    data_dir, env = home
    rec = record(1, 'convention', 'neutral')
    write_records(data_dir, [rec, rec])
    run_script('outcomes.py', env=env, check=True)
    assert len((data_dir / 'outcomes.jsonl').read_text().splitlines()) == 1


def test_same_commit_in_distinct_repositories_is_not_deduplicated(home):
    data_dir, env = home
    first = record(1, 'convention', 'neutral')
    second = dict(first, record_id='second', repo_key='another-repo')
    write_records(data_dir, [first, second])
    assert 'Records: 2 ' in run_script('summarize.py', env=env, check=True).stdout


def test_sensitive_tool_arguments_are_not_recorded():
    args = {'query': 'private decision body', 'title': 'private title', 'content': 'private content'}
    recorded = stop_hook.summarize_args(args)
    assert 'private' not in recorded
    assert 'query' in recorded


def test_git_nonzero_failure_retries_commit_evidence(home, repo, tmp_path, monkeypatch):
    data_dir, _ = home
    monkeypatch.setattr(stop_hook, 'ROOT', data_dir)
    t = Transcript(tmp_path / 't.jsonl', 'claude')
    data = json.loads(payload('claude', 'git-failure', t.path, repo))
    stop_hook.run_hook('claude', data)
    commit(repo, 'b.txt', 'fix: b')
    t.shell('git commit -m b')
    original = stop_hook.subprocess.run
    with monkeypatch.context() as patch:
        def fail_log(command, **kwargs):
            if 'log' in command:
                import subprocess
                return subprocess.CompletedProcess(command, 128, '', 'cannot read objects')
            return original(command, **kwargs)
        patch.setattr(stop_hook.subprocess, 'run', fail_log)
        assert stop_hook.run_hook('claude', data) is None
    assert stop_hook.run_hook('claude', data)


def test_commit_batch_does_not_silently_drop_after_fifty(repo):
    import time
    since = time.time()
    for i in range(55):
        git(repo, 'commit', '--allow-empty', '-qm', f'batch {i}')
    found = stop_hook.new_commits(stop_hook.repo_info(repo), since, set())
    assert len(found) == 55


def test_linked_worktree_record_names_its_actual_branch(home, repo, tmp_path):
    data_dir, env = home
    wt = tmp_path / 'worktree'
    git(repo, 'worktree', 'add', '-qb', 'feature', str(wt))
    t = Transcript(tmp_path / 't.jsonl', 'claude')
    stop('claude', env, payload('claude', 'wt', t.path, repo))
    commit(wt, 'b.txt', 'fix: worktree')
    t.shell(f'git -C {wt} commit -m b')
    result = stop('claude', env, payload('claude', 'wt', t.path, repo))
    pending = pending_of(data_dir, result)
    assert pending['repo'] == str(wt)
    assert pending['branch'] == 'feature'


def test_pending_metadata_scrubs_credentials(home, repo, tmp_path):
    data_dir, env = home
    secret = 'ghp_' + 'a' * 30
    git(repo, 'remote', 'add', 'origin', f'https://user:{secret}@example.invalid/repo')
    t = Transcript(tmp_path / 't.jsonl', 'claude')
    stop('claude', env, payload('claude', 'privacy', t.path, repo))
    commit(repo, 'b.txt', 'fix: ' + secret)
    t.shell('git commit -m b')
    result = stop('claude', env, payload('claude', 'privacy', t.path, repo))
    pending = pending_of(data_dir, result)
    assert secret not in json.dumps(pending)
    assert '[REDACTED]' in pending['remote']


@pytest.mark.parametrize('host', ['cursor', 'claude', 'codex'])
def test_full_review_pipeline_in_throwaway_repository(home, repo, tmp_path, host):
    data_dir, env = home
    t = Transcript(tmp_path / 't.jsonl', host)
    data = payload(host, f'pipeline-{host}', t.path, repo)
    stop(host, env, data)
    commit(repo, 'feature.txt', 'fix: sample feature')
    t.shell('git commit -m feature')
    result = stop(host, env, data)
    j = example()
    j['contexer_items'] = []
    j['captures'] = []
    j['verdict']['contexer_effect'] = 'neutral'
    token = token_of(result)
    for args in (['--check', '--token', token], ['--token', token]):
        run_script('log_usage.py', args, stdin=json.dumps(j), env=env, check=True)
    record_path = next((data_dir / 'records').glob('*.jsonl'))
    rec = json.loads(record_path.read_text())
    assert rec['host'] == host
    assert rec['trigger']['commits'][0]['subject'] == 'fix: sample feature'
    run_script('outcomes.py', env=env, check=True)
    report = run_script('summarize.py', env=env, check=True).stdout
    assert 'Records: 1 ' in report and 'neutral 1/1' in report
    assert not fired(stop(host, env, data))


def test_quoted_heredoc_marker_does_not_hide_real_command():
    assert 'gh pr create' in transcript.shell_commands({'cmd': 'echo "<<EOF"\ngh pr create'})


def test_structured_bootstrap_results_verify_decision_ids():
    result = json.dumps({'decisions': [{'id': '1234abcd-1234-1234-1234-123456789abc',
                                       'content': 'A scoped rule'}]})
    events = [('call', 'bootstrap_context', {}, 'c1', 'contexer', 'bootstrap_context'),
              ('result', 'c1', result)]
    assert stop_hook.observe(events, 'claude')['contexer_result_ids'] == ['1234abcd']


def test_decisive_verdict_cannot_use_own_fact_and_unrelated_old_item():
    j = example()
    j['verdict']['contexer_effect'] = 'decisive'
    j['contexer_items'][0].update(relevance='helpful', note='Useful unrelated rule')
    j['key_facts'][0].update(source='contexer', contexer_ids=[j['captures'][0]['id']])
    assert log_usage.validate(j)


def test_pr_outcome_can_use_saved_url_after_worktree_removed(monkeypatch):
    seen = []
    def fake_run(command, cwd=None):
        seen.append(cwd)
        return json.dumps({'state': 'MERGED', 'statusCheckRollup': []})
    monkeypatch.setattr(outcomes, 'run', fake_run)
    result, _ = outcomes.pr_outcome('https://example/pr/1', '/missing/worktree')
    assert result['pr_state'] == 'merged' and seen == [None]


def test_missing_transcript_has_unknown_observations(home, repo):
    data_dir, env = home
    commit(repo, 'b.txt', 'fix: b')
    result = stop('claude', env, payload('claude', 'missing-transcript', None, repo))
    observed = pending_of(data_dir, result)['observed']
    assert observed['contexer_call_count'] is None
    assert observed['autofetch_blocks'] is None
    assert observed['contexer_result_ids'] is None
    assert observed['tool_calls']['shell'] is None


def test_logging_after_partial_record_preserves_new_record(tmp_path):
    path = tmp_path / 'records.jsonl'
    path.write_text('{"interrupted":')
    log_usage.append(path, {'record_key': 'new-record'})
    assert json.loads(path.read_text().splitlines()[-1]) == {'record_key': 'new-record'}


def test_outcome_append_repairs_partial_line_and_is_idempotent(tmp_path, monkeypatch):
    path = tmp_path / 'outcomes.jsonl'
    path.write_text('{"interrupted":')
    monkeypatch.setattr(outcomes, 'OUTCOMES', path)
    row = {'record_id': 'r1', 'pr_state': 'open', 'reverted': None}
    assert outcomes.append_changed(row) is True
    assert outcomes.append_changed(row) is False
    assert json.loads(path.read_text().splitlines()[-1])['record_id'] == 'r1'


def test_payload_repository_does_not_scan_unrelated_launch_directory(home, repo, tmp_path, monkeypatch):
    from conftest import make_repo
    unrelated = make_repo(tmp_path / 'unrelated')
    # Empty local identity reproduces a clean CI runner regardless of global config.
    git(unrelated, 'config', 'user.email', '')
    monkeypatch.chdir(unrelated)
    data_dir, env = home
    t = Transcript(tmp_path / 'payload.jsonl', 'claude')
    data = payload('claude', 'payload-repo', t.path, repo)
    stop('claude', env, data)
    commit(repo, 'fix.txt', 'fix: use the host repository')
    t.shell('git commit -m fix')
    result = stop('claude', env, data)
    assert fired(result)
    assert pending_of(data_dir, result)['repo'] == str(repo)


def test_launch_directory_is_used_when_payload_has_no_repository(home, repo, tmp_path, monkeypatch):
    monkeypatch.chdir(repo)
    data_dir, env = home
    t = Transcript(tmp_path / 'fallback.jsonl', 'claude')
    data = json.dumps({'session_id': 'fallback-repo', 'transcript_path': str(t.path)})
    stop('claude', env, data)
    commit(repo, 'fix.txt', 'fix: fallback')
    t.shell('git commit -m fix')
    result = stop('claude', env, data)
    assert fired(result)
    assert pending_of(data_dir, result)['repo'] == str(repo)


@pytest.mark.parametrize('tool,feature', [('get_global_context', 'get_context'),
                                         ('update_global_context', 'update_context')])
def test_global_tools_require_the_corresponding_feature_rating(tool, feature):
    judgment = example()
    judgment['feature_ratings'] = [r for r in judgment['feature_ratings'] if r['feature'] != feature]
    observed = stop_hook.observe([('call', tool, {}, 'c', 'contexer', tool)], 'claude')
    errors = log_usage.validate(judgment, observed)
    assert any('missing observed features' in e and feature in e for e in errors)


@pytest.mark.parametrize('tool,correct', [('get_context', 'get_context'),
                                        ('get_global_context', 'get_context'),
                                        ('review_pending', 'review_pending'),
                                        ('bootstrap_context', 'other_tool')])
def test_decision_ids_are_verified_for_the_actual_surfacing_method(tool, correct):
    observed = stop_hook.observe([('call', tool, {}, 'c', 'contexer', tool),
                                 ('result', 'c', '(id=a1b2c3d4)')], 'claude')
    for method in ('get_context', 'review_pending', 'other_tool'):
        assert ('a1b2c3d4' in log_usage.surface_ids(observed, method)) == (method == correct)
    judgment = example()
    judgment['contexer_items'] = [dict(judgment['contexer_items'][0], surfaced_by=correct)]
    judgment['captures'] = []
    judgment['feature_ratings'] += [{'feature': feature, 'verdict': 'not_useful', 'note': 'No effect'}
                                    for feature in ('bootstrap', 'review_pending')]
    assert log_usage.validate(judgment, observed) == []
    judgment['contexer_items'][0]['surfaced_by'] = 'other_tool' if correct != 'other_tool' else 'get_context'
    assert any('not in any' in e for e in log_usage.validate(judgment, observed))


def test_old_observations_cannot_verify_tool_specific_provenance():
    observed = {'contexer_result_ids': ['a1b2c3d4']}
    judgment = {'contexer_items': [{'id': 'a1b2c3d4', 'surfaced_by': 'get_context'}]}
    assert not log_usage.ids_verified(judgment, observed)


def test_pruning_tolerates_a_pending_file_consumed_by_another_logger(tmp_path, monkeypatch):
    monkeypatch.setattr(stop_hook, 'ROOT', tmp_path)
    target = tmp_path / 'pending/consumed.json'
    stop_hook.write_json(target, {})
    original = type(target).stat
    def vanished(path, *args, **kwargs):
        if path == target:
            raise FileNotFoundError(path)
        return original(path, *args, **kwargs)
    monkeypatch.setattr(type(target), 'stat', vanished)
    state = {'digests': {'consumed': 'digest'}}
    stop_hook.prune_pending(state)
    assert state['digests'] == {}


def test_persisted_measurements_are_private_and_appends_are_synced(tmp_path, monkeypatch):
    import os
    import stat
    synced = []
    original_sync = os.fsync
    def sync(fd):
        synced.append(fd)
        original_sync(fd)
    monkeypatch.setattr(os, 'fsync', sync)
    json_path = tmp_path / 'pending/record.json'
    record_path = tmp_path / 'records/repo.jsonl'
    outcome_path = tmp_path / 'outcomes.jsonl'
    monkeypatch.setattr(outcomes, 'OUTCOMES', outcome_path)
    previous_umask = os.umask(0)
    try:
        stop_hook.write_json(json_path, {'value': 1})
        log_usage.append(record_path, {'record_key': 'one'})
        outcomes.append_changed({'record_id': 'one'})
    finally:
        os.umask(previous_umask)
    assert len(synced) == 3
    assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in
               (json_path, record_path, outcome_path))
    assert not list(json_path.parent.glob('.record.json.*'))


def test_failed_json_replace_retains_previous_state_and_cleans_temporary_file(tmp_path, monkeypatch):
    path = tmp_path / 'state.json'
    stop_hook.write_json(path, {'value': 'before'})
    def fail(*args):
        raise OSError('replace failed')
    monkeypatch.setattr(stop_hook.os, 'replace', fail)
    with pytest.raises(OSError):
        stop_hook.write_json(path, {'value': 'after'})
    assert json.loads(path.read_text()) == {'value': 'before'}
    assert not list(tmp_path.glob('.state.json.*'))


@pytest.mark.parametrize('key', ['repo[1]-1234', 'repo*-1234', 'repo?-1234'])
def test_repo_key_filters_are_literal_for_both_reports(home, key):
    data_dir, env = home
    own, other = record(1, 'convention', 'neutral'), record(2, 'convention', 'neutral')
    own['repo_key'] = key
    path = data_dir / 'records'
    path.mkdir(parents=True)
    (path / f'{key}.jsonl').write_text(json.dumps(own) + '\n')
    (path / 'repoX-1234.jsonl').write_text(json.dumps(other) + '\n')
    report = run_script('summarize.py', ['--repo-key', key], env=env, check=True)
    assert 'Records: 1 ' in report.stdout
    run_script('outcomes.py', ['--repo-key', key], env=env, check=True)
    rows = [json.loads(line) for line in (data_dir/'outcomes.jsonl').read_text().splitlines()]
    assert [row['record_id'] for row in rows] == [own['record_id']]


@pytest.mark.parametrize('conclusion', ['STALE', 'STARTUP_FAILURE'])
def test_terminal_ci_failures_do_not_look_pending(conclusion):
    assert outcomes.checks_conclusion([{'conclusion': conclusion}]) == 'failure'


def test_report_exposes_each_ci_state_and_its_denominator(home):
    data_dir, env = home
    states = ['success', 'failure', 'pending', 'none', 'unknown', 'not_checked']
    recs = [record(i, 'convention', 'neutral') for i in range(len(states))]
    write_records(data_dir, recs)
    (data_dir / 'outcomes.jsonl').write_text('\n'.join(
        json.dumps({'record_id': r['record_id'], 'checks': status, 'pr_state': 'open'})
        for r, status in zip(recs, states) if status != 'not_checked') + '\n')
    report = run_script('summarize.py', env=env, check=True).stdout.split('## CI checks')[1]
    assert all(status in report for status in states)
    assert '| neutral | 1 | 1 | 1 | 1 | 1 | 1 | 6 |' in report


def test_pruning_failure_does_not_lose_seen_commit_state(home, repo, tmp_path, monkeypatch):
    data_dir, _ = home
    monkeypatch.setattr(stop_hook, 'ROOT', data_dir)
    t = Transcript(tmp_path / 'prune.jsonl', 'claude')
    data = json.loads(payload('claude', 'prune-failure', t.path, repo))
    stop_hook.run_hook('claude', data)
    commit(repo, 'fix.txt', 'fix: retain state')
    t.shell('git commit -m fix')
    def fail(state):
        raise PermissionError('pending directory unavailable')
    monkeypatch.setattr(stop_hook, 'prune_pending', fail)
    assert stop_hook.run_hook('claude', data)
    assert stop_hook.run_hook('claude', data) is None
    assert json.loads(stop_hook.state_path('prune-failure').read_text())['seen']
