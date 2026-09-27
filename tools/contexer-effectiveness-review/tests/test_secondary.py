import json

import pytest

import log_usage
import secondary
import stop_hook
from conftest import run_script
from test_measurement import scenario
from test_reports import write_records


def blind():
    return {'secondary_reviewer_type': 'independent_agent', 'reviewer_id': 'reviewer-2',
            'reviewer_session_id': 'separate-session', 'blinding': 'blind',
            'secondary_required_facts': [{'fact': 'The temporary compatibility exception remains active',
                                         'available_from': ['conversation_only'], 'current_repo_recoverable': 'no'}],
            'secondary_code_recoverable_judgment': 'no', 'secondary_notes': 'Implementation alone does not establish the intended expiry.'}


def final():
    return {'secondary_helpful_judgment': 'yes', 'secondary_notes': 'The exception prevented an unsupported removal.',
            'later_outcome_corroborated': 'uncertain', 'later_outcome_evidence': 'No independent later outcome was collected.'}


@pytest.fixture
def case(home, tmp_path, monkeypatch):
    root, env = home
    monkeypatch.setattr(secondary, 'ROOT', root)
    monkeypatch.setattr(log_usage, 'ROOT', root)
    monkeypatch.setattr(stop_hook, 'ROOT', root)
    r = scenario(1, 'helpful')
    write_records(root, [r])
    paths = {}
    for key, text in {'task': 'Implement the expiry migration', 'snapshot': 'curated snapshot bytes',
                      'change': 'Retained old-client compatibility', 'evidence': 'Inspected expiry.py',
                      'attribution': 'Prior decision a1b2c3d4 supplies the current exception'}.items():
        paths[key] = tmp_path / f'{key}.txt'
        paths[key].write_text(text)
    c = secondary.prepare('r1', **{k: paths[k] for k in ('task', 'snapshot', 'change', 'evidence')})
    return c, r, paths, env


def test_packet_excludes_primary_judgment_and_requires_blind_stage(case):
    c, _, paths, _ = case
    packet = json.loads(secondary.Path(c['packet_path']).read_text())
    assert not {'record_id', 'judgment', 'verdict', 'session_id', 'contexer_items'} & packet.keys()
    assert 'helpful' not in json.dumps(packet)
    with pytest.raises(ValueError, match='Seal the blind'):
        secondary.reveal(c['case_id'], paths['attribution'])
    with pytest.raises(ValueError, match='stages first'):
        secondary.complete(c['case_id'], final())


def test_two_stage_cli_flow_and_report(case):
    c, _, paths, env = case
    cid = ['--case-id', c['case_id']]
    run_script('secondary.py', ['blind', *cid], stdin=json.dumps(blind()), env=env, check=True)
    run_script('secondary.py', ['reveal', *cid, '--evidence', str(paths['attribution'])], env=env, check=True)
    run_script('secondary.py', ['complete', *cid], stdin=json.dumps(final()), env=env, check=True)
    out = run_script('summarize.py', env=env, check=True).stdout
    assert '| Blind secondary reviewer judged benefit | 1 | 1 | 100.0% | 0 |' in out
    assert 'agreement 1/1' in out
    assert '| Later evidence corroborated benefit | 0 | 1 | 0.0% | 1 |' in out


def test_self_review_and_missing_fields_rejected(case):
    c, r, _, _ = case
    b = blind()
    b['reviewer_session_id'] = r['session_id']
    with pytest.raises(ValueError, match='different'):
        secondary.seal_blind(c['case_id'], b)
    for key in blind():
        value = blind()
        del value[key]
        assert secondary.validate_blind(value, r)
    assert secondary.validate_final({})


@pytest.mark.parametrize('tamper', ['snapshot', 'packet', 'source', 'sealed', 'attribution'])
def test_changed_artifacts_rejected(case, tamper):
    c, _, paths, _ = case
    secondary.seal_blind(c['case_id'], blind())
    c = secondary.reveal(c['case_id'], paths['attribution'])
    if tamper == 'snapshot':
        paths['snapshot'].write_text('changed')
    elif tamper == 'packet':
        secondary.Path(c['packet_path']).write_text('{}')
    elif tamper == 'attribution':
        secondary.Path(c['attribution_path']).write_text('{}')
    elif tamper == 'sealed':
        c['blind_review']['secondary_notes'] = 'changed'
        stop_hook.write_json(secondary.case_path(c['case_id']), c)
    else:
        path = log_usage.record_paths()[0]
        path.write_text(path.read_text().replace('Inspect the expiry comparison.', 'Read a different file.'))
    with pytest.raises(ValueError, match='changed'):
        secondary.complete(c['case_id'], final())


def test_completed_review_idempotence_and_append_recovery(case, monkeypatch):
    c, r, paths, _ = case
    secondary.seal_blind(c['case_id'], blind())
    secondary.reveal(c['case_id'], paths['attribution'])
    real_write = stop_hook.write_json
    monkeypatch.setattr(stop_hook, 'write_json', lambda *a: (_ for _ in ()).throw(OSError('interrupted')))
    with pytest.raises(OSError):
        secondary.complete(c['case_id'], final())
    monkeypatch.setattr(stop_hook, 'write_json', real_write)
    a = secondary.complete(c['case_id'], final())
    assert secondary.complete(c['case_id'], final()) == a
    rows, skipped = secondary.load_reviews([r])
    assert len(rows['r1']) == 1 and skipped == 0
    changed = final()
    changed['secondary_helpful_judgment'] = 'no'
    with pytest.raises(ValueError, match='immutable'):
        secondary.complete(c['case_id'], changed)


def test_disagreement_is_not_cherry_picked(case):
    c, r, paths, _ = case
    secondary.seal_blind(c['case_id'], blind())
    secondary.reveal(c['case_id'], paths['attribution'])
    secondary.complete(c['case_id'], final())
    other = secondary.prepare('r1', **{k: paths[k] for k in ('task', 'snapshot', 'change', 'evidence')})
    secondary.seal_blind(other['case_id'], blind())
    secondary.reveal(other['case_id'], paths['attribution'])
    dissent = dict(final(), secondary_helpful_judgment='no')
    secondary.complete(other['case_id'], dissent)
    rows, skipped = secondary.load_reviews([r])
    assert skipped == 0
    assert secondary.consensus(rows['r1'], 'secondary_helpful_judgment') == 'uncertain'


@pytest.mark.parametrize('stamp', ['1', True, float('nan'), float('inf'), -1])
def test_bad_timestamps_cannot_grant_benefit(case, stamp):
    c, r, paths, _ = case
    secondary.seal_blind(c['case_id'], blind())
    secondary.reveal(c['case_id'], paths['attribution'])
    row = secondary.complete(c['case_id'], final())
    row['blind_sealed_at'] = stamp
    (secondary.ROOT / 'secondary_reviews.jsonl').write_text(json.dumps(row) + '\n')
    rows, skipped = secondary.load_reviews([r])
    assert rows == {} and skipped == 1


def test_candidate_sampling_is_repeatable_and_includes_all_promising(home):
    records = [scenario(i, kind) for i, kind in enumerate(['helpful', 'decisive', 'harmful', 'redundant'])]
    write_records(home[0], records)
    a = run_script('secondary.py', ['candidates', '--neutral-percent', '0'], env=home[1], check=True)
    assert a.stdout.splitlines() == ['r0', 'r1', 'r2']
    b = run_script('secondary.py', ['candidates', '--neutral-percent', '100'], env=home[1], check=True)
    assert b.stdout.splitlines() == ['r0', 'r1', 'r2', 'r3']
