import copy
import json

import pytest

import assessment
import log_usage
import measurement
from conftest import run_script
from test_reports import record, write_records


def scenario(i, kind):
    r = record(i, 'rationale', 'neutral')
    j = r['judgment']
    if kind == 'no_opportunity':
        return r
    j['opportunity'].update(non_code_context_opportunity='yes', opportunity_type=['exception'],
                            could_current_code_reveal='no', relevant_knowledge_existed='yes',
                            knowledge_surfaced='yes', required_fact_location=['conversation_only'])
    f = j['key_facts'][0]
    f.update(source='contexer', contexer_ids=['a1b2c3d4'], knowledge_category='exception',
             code_would_reveal='human_or_organizational_knowledge_only',
             **dict.fromkeys(assessment.RECOVERY_FLAGS, 'no'))
    if kind == 'no_knowledge':
        j['opportunity'].update(relevant_knowledge_existed='no', knowledge_surfaced='no')
        f.update(source='user', contexer_ids=[])
    elif kind == 'not_retrieved':
        j['opportunity']['knowledge_surfaced'] = 'no'
        f.update(source='user', contexer_ids=[])
    elif kind == 'redundant':
        j['contexer_items'][0]['relevance'] = 'redundant'
    else:
        change = 'prevented_wrong_action' if kind == 'decisive' else 'material_change'
        effect = 'harmful' if kind == 'harmful' else 'decisive' if kind == 'decisive' else 'helpful'
        j['verdict']['contexer_effect'] = effect
        j['contexer_items'][0].update(relevance='misleading' if kind == 'harmful' else effect,
                                     note='The prior exception altered the implementation choice')
        f['action_change'] = change
        j['action'].update(action_change=change, material_fact_ids=['fact-1'], severity='high',
                           impact_class='avoided_architecture_drift')
        if kind == 'code':
            j['opportunity'].update(non_code_context_opportunity='no', opportunity_type=[],
                                    could_current_code_reveal='yes')
            f.update(code_would_reveal='current_code_would_reveal', could_current_code_reveal='yes')
        elif kind == 'docs':
            f.update(code_would_reveal='repo_docs_would_reveal', could_repo_docs_reveal='yes')
        elif kind == 'unknown':
            f.update(code_would_reveal='unknown', **dict.fromkeys(assessment.RECOVERY_FLAGS, 'uncertain'))
    assert not log_usage.validate(j), log_usage.validate(j)
    return r


@pytest.mark.parametrize('kind,opportunity,success', [
    ('no_opportunity', 0, 0), ('no_knowledge', 1, 0), ('not_retrieved', 1, 0),
    ('redundant', 1, 0), ('helpful', 1, 1), ('decisive', 1, 1), ('harmful', 1, 0),
    ('code', 0, 0), ('docs', 1, 1), ('intent', 1, 1)])
def test_scenarios(kind, opportunity, success):
    data = measurement.metrics([scenario(1, kind)])
    assert data['opportunity_rate']['count'] == opportunity
    assert data['conditional_success']['count'] == success
    assert data['conditional_success']['denominator'] == opportunity


def test_funnel_denominators_and_independent_evidence():
    kinds = ['no_opportunity', 'no_knowledge', 'not_retrieved', 'redundant', 'helpful', 'decisive', 'harmful']
    records = [scenario(i, kind) for i, kind in enumerate(kinds)]
    review = {'secondary_helpful_judgment': 'yes', 'later_outcome_corroborated': 'uncertain',
              'blind_assessment': {'blinding': 'blind'}}
    data = measurement.metrics(records, {'r4': [review]}, eligible_work=10)
    stages = [r for _, r in data['funnel']]
    assert [(r['count'], r['denominator']) for r in stages] == [(6, 7), (5, 6), (4, 5), (3, 4), (1, 3), (0, 1)]
    assert stages[4]['unknown'] == 2 and stages[5]['unknown'] == 1
    assert data['unreviewed'] == 3
    review['blind_assessment']['blinding'] = 'partial'
    assert measurement.metrics(records, {'r4': [review]})['funnel'][4][1]['count'] == 0
    with pytest.raises(ValueError):
        measurement.metrics(records, eligible_work=6)


def test_recovery_denominators_include_unknown_and_harmful():
    records = [scenario(i, kind) for i, kind in enumerate(['code', 'docs', 'intent', 'unknown', 'harmful'])]
    data = measurement.metrics(records)
    assert data['irreducible_context'] == {'count': 3, 'denominator': 5, 'percentage': 60, 'unknown': 1, 'no': 1}
    assert data['unique_context'] == {'count': 1, 'denominator': 4, 'percentage': 25, 'unknown': 1, 'no': 2}


def test_legacy_records_are_unknown_not_reinterpreted(home):
    r = scenario(1, 'no_opportunity')
    r['schema'] = log_usage.LEGACY_SCHEMA
    r['judgment'] = assessment.base_projection(r['judgment'])
    assert not log_usage.validate(r['judgment'], schema=r['schema'])
    write_records(home[0], [r])
    out = run_script('summarize.py', env=home[1], check=True).stdout
    assert 'Opportunity rate: 0/1 (0.0%); unknown 1/1' in out
    assert 'Conditional success, among assessed opportunities: 0/0 (unknown)' in out
    assert 'task id missing in 1/1' in out


def test_repeated_task_segments_are_reported_and_not_deduplicated(home):
    records = [scenario(i, 'helpful') for i in range(2)]
    records[1]['judgment']['task']['task_id'] = records[0]['judgment']['task']['task_id']
    write_records(home[0], records)
    out = run_script('summarize.py', env=home[1], check=True).stdout
    assert '1 identified tasks; 1 repeated groups, 1 extra segments' in out
    assert 'Conditional success, among assessed opportunities: 2/2' in out


@pytest.mark.parametrize('area', ['opportunity', 'action'])
@pytest.mark.parametrize('bad', [None, [], True, {}, 'invented'])
def test_invalid_shapes_and_enums_fail_without_crashing(area, bad):
    j = scenario(1, 'helpful')['judgment']
    for field in list(j[area]):
        if bad == 'invented' and field in ('opportunity_reason', 'knowledge_evidence', 'action_before', 'action_after', 'evidence'):
            continue
        candidate = copy.deepcopy(j)
        candidate[area][field] = bad
        assert log_usage.validate(candidate), (area, field, bad)


def test_missing_fields_and_self_credit_rejected():
    j = scenario(1, 'helpful')['judgment']
    for area in ('opportunity', 'action'):
        for field in j[area]:
            candidate = copy.deepcopy(j)
            del candidate[area][field]
            assert log_usage.validate(candidate)
    j['contexer_items'][0].update(created_this_session=True, relevance='redundant')
    j['verdict']['contexer_effect'] = 'neutral'
    assert any('own captures' in e for e in log_usage.validate(j))


def test_no_use_can_have_no_feature_ratings():
    j = scenario(1, 'no_opportunity')['judgment']
    j.update(contexer_items=[], captures=[], feature_ratings=[], improvements=[], gaps=[])
    j['action']['contexer_used'] = 'no'
    j['verdict']['contexer_effect'] = 'not_used'
    assert not log_usage.validate(j)


def test_outcomes_do_not_supply_independent_benefit(home):
    r = scenario(1, 'helpful')
    write_records(home[0], [r])
    (home[0] / 'outcomes.jsonl').write_text(json.dumps({'record_id': 'r1', 'pr_state': 'merged', 'ci': 'success', 'reverted': []}) + '\n')
    out = run_script('summarize.py', env=home[1], check=True).stdout
    assert '| Blind secondary reviewer judged benefit | 0 | 1 | 0.0% | 1 |' in out


def test_empty_cohort_has_unknown_rates_and_visible_unreviewed_work(home):
    out = run_script('summarize.py', ['--eligible-work', '5'], env=home[1], check=True).stdout
    assert 'Opportunity rate: 0/0 (unknown)' in out
    assert 'Unreviewed eligible work: 5.' in out
    assert run_script('summarize.py', ['--eligible-work', '-1'], env=home[1]).returncode == 2


def test_kind_filter_and_uncertain_action(home):
    a, b = scenario(1, 'helpful'), scenario(2, 'redundant')
    a['kind'], b['kind'] = 'hook', 'manual'
    b['judgment']['action']['action_change'] = 'uncertain'
    assert measurement.metrics([b])['conditional_success']['unknown'] == 1
    write_records(home[0], [a, b])
    out = run_script('summarize.py', ['--kind', 'manual'], env=home[1], check=True).stdout
    assert 'Records: 1 ' in out and 'Conditional success, among assessed opportunities: 0/1 (0.0%); unknown 1/1' in out


def test_known_surfaced_knowledge_cannot_claim_uncertain_use():
    j = scenario(1, 'redundant')['judgment']
    j['action']['contexer_used'] = 'uncertain'
    assert any('requires contexer_used=yes' in e for e in log_usage.validate(j))


def test_unknown_recovery_label_is_printed_once(home):
    write_records(home[0], [scenario(1, 'unknown')])
    out = run_script('summarize.py', env=home[1], check=True).stdout
    line = next(line for line in out.splitlines() if line.startswith('- Contexer-sourced facts,'))
    assert line.count('unknown 1/1') == 1
