"""Opportunity-conditional field analysis. All primary assessments remain self-reported."""
from collections import Counter

import assessment
import secondary


def rate(values):
    counts = Counter(values)
    total = len(values)
    return {'count': counts['yes'], 'denominator': total,
            'percentage': 100 * counts['yes'] / total if total else None,
            'unknown': counts['uncertain'], 'no': counts['no']}


def opportunity(record):
    return record['judgment'].get('opportunity', {})


def action(record):
    return record['judgment'].get('action', {})


def material(record):
    return action(record).get('action_change') in assessment.MATERIAL


def useful_facts(record):
    j = record['judgment']
    if not material(record) or j['verdict']['contexer_effect'] not in ('helpful', 'decisive'):
        return []
    credited = {item['id'] for item in j['contexer_items']
                if not item['created_this_session'] and item['relevance'] in ('helpful', 'decisive')}
    material_ids = set(action(record).get('material_fact_ids', []))
    return [fact for fact in j['key_facts'] if fact.get('fact_id') in material_ids
            and fact['source'] == 'contexer' and fact.get('action_change') in assessment.MATERIAL
            and credited.intersection(fact['contexer_ids'])]


def affected_facts(record):
    if not material(record):
        return []
    j = record['judgment']
    prior = {item['id'] for item in j['contexer_items'] if not item['created_this_session']}
    linked = action(record)['material_fact_ids']
    return [fact for fact in j['key_facts'] if fact['fact_id'] in linked and fact['source'] == 'contexer'
            and prior.intersection(fact['contexer_ids'])]


def successful(record):
    if useful_facts(record):
        return 'yes'
    if action(record).get('action_change', 'uncertain') == 'uncertain':
        return 'uncertain'
    return 'no'


def irreducible(fact):
    return {'yes': 'no', 'no': 'yes'}.get(fact['could_current_code_reveal'], 'uncertain')


def unique(fact):
    values = [fact[key] for key in assessment.RECOVERY_FLAGS[:4]]
    if 'yes' in values:
        return 'no'
    return 'yes' if all(value == 'no' for value in values) else 'uncertain'


def metrics(records, reviews=None, eligible_work=None):
    reviews = reviews or {}
    n = len(records)
    if eligible_work is not None and (type(eligible_work) is not int or eligible_work < n):
        raise ValueError('Eligible-work count must be an integer at least as large as the reviewed cohort')
    opportunities = [r for r in records if opportunity(r).get('non_code_context_opportunity') == 'yes']
    known = [r for r in opportunities if opportunity(r).get('relevant_knowledge_existed') == 'yes']
    surfaced = [r for r in known if opportunity(r).get('knowledge_surfaced') == 'yes']
    changed = [r for r in surfaced if material(r)]
    beneficial = [r for r in changed if secondary.blind_consensus(reviews.get(r['record_id'], []), 'secondary_helpful_judgment') == 'yes']
    stages = [
        ('Non-code opportunity existed', rate([opportunity(r).get('non_code_context_opportunity', 'uncertain') for r in records])),
        ('Relevant knowledge existed', rate([opportunity(r).get('relevant_knowledge_existed', 'uncertain') for r in opportunities])),
        ('Relevant knowledge surfaced', rate([opportunity(r).get('knowledge_surfaced', 'uncertain') for r in known])),
        ('Surfaced knowledge materially changed action', rate([
            'yes' if material(r) else 'uncertain' if action(r).get('action_change', 'uncertain') == 'uncertain' else 'no'
            for r in surfaced])),
        ('Blind secondary reviewer judged benefit', rate([secondary.blind_consensus(reviews.get(r['record_id'], []), 'secondary_helpful_judgment') for r in changed])),
        ('Later evidence corroborated benefit', rate([secondary.blind_consensus(reviews.get(r['record_id'], []), 'later_outcome_corroborated') for r in beneficial])),
    ]
    facts = [fact for record in records for fact in useful_facts(record)]
    affected = [fact for record in records for fact in affected_facts(record)]
    tasks = Counter((r['repo_key'], r['judgment']['task']['task_id']) for r in records
                    if r['judgment']['task'].get('task_id'))
    return {'reviewed': n, 'eligible_work': eligible_work,
            'unreviewed': eligible_work - n if eligible_work is not None else None,
            'opportunity_rate': stages[0][1], 'conditional_success': rate([successful(r) for r in opportunities]),
            'irreducible_context': rate([irreducible(f) for f in affected]),
            'unique_context': rate([unique(f) for f in facts]), 'funnel': stages,
            'material_useful_facts': facts,
            'unassessed_fact_materiality': sum(1 for r in records for f in r['judgment']['key_facts']
                if f['source'] == 'contexer' and ('action' not in r['judgment'] or f.get('action_change') == 'uncertain')),
            'distinct_identified_tasks': len(tasks), 'repeated_task_groups': sum(count > 1 for count in tasks.values()),
            'extra_segments_from_repeated_tasks': sum(count - 1 for count in tasks.values()),
            'records_without_task_id': n - sum(tasks.values()),
            'categories': dict(Counter(f['knowledge_category'] for f in facts)),
            'irreducible_categories': dict(Counter(f['knowledge_category'] for f in facts if irreducible(f) == 'yes'))}


def percentage(value):
    return f'{value:.1f}%' if value is not None else 'unknown'


def rate_text(value):
    return f"{value['count']}/{value['denominator']} ({percentage(value['percentage'])}); unknown {value['unknown']}/{value['denominator']}"


def render(records, eligible_work=None):
    reviews, skipped = secondary.load_reviews(records)
    data = metrics(records, reviews, eligible_work)
    print('## Primary question: does non-code engineering intent change the action?\n')
    print('These are assessed field frequencies, not causal estimates. Unknown assessments stay in the stated denominator; a yes share is a lower bound on recorded yes, not a claim that unknown means no.')
    print(f"- Opportunity rate: {rate_text(data['opportunity_rate'])}")
    print(f"- Conditional success, among assessed opportunities: {rate_text(data['conditional_success'])}")
    print('  Success requires a material/prevented-wrong-action change linked to a previously stored helpful/decisive fact. Tool calls and convenience alone do not qualify.')
    print(f"- Irreducible-context rate, among materially affecting Contexer facts (including harmful): {rate_text(data['irreducible_context'])}")
    print(f"- Unique-context rate, excluding recovery from code/docs/Git/PR history: {rate_text(data['unique_context'])}")
    print(f"- Contexer facts with unassessed materiality: {data['unassessed_fact_materiality']}; these cannot enter the material-fact denominator.")
    print(f"- Task clustering: {data['distinct_identified_tasks']} identified tasks; {data['repeated_task_groups']} repeated groups, {data['extra_segments_from_repeated_tasks']} extra segments; task id missing in {data['records_without_task_id']}/{len(records)} records.")
    print(f"- Cohort schema versions: {dict(Counter(r['schema'] for r in records))}; review kinds: {dict(Counter(r.get('kind', 'unknown') for r in records))}.")
    print('Repeated task segments are correlated. Repository keys need a curator mapping before cross-developer aggregation. Legacy records have unknown new assessments; no retrospective zero filling occurs.')
    print('\n### Funnel\n')
    print('| Stage | Count | Denominator | Percentage | Unknown/missing in denominator |')
    print('|---|---:|---:|---:|---:|')
    eligible = data['eligible_work']
    print(f'| All eligible work (externally declared) | {eligible if eligible is not None else "unknown"} | {eligible if eligible is not None else "unknown"} | {"100.0%" if eligible else "unknown"} | {"0" if eligible is not None else "unknown"} |')
    for name, values in data['funnel']:
        print(f"| {name} | {values['count']} | {values['denominator']} | {percentage(values['percentage'])} | {values['unknown']} |")
    print(f"Unreviewed eligible work: {data['unreviewed'] if data['unreviewed'] is not None else 'unknown'}. The opportunity row uses reviewed segments; each later row conditions on known yes in the preceding stage. Earlier unknowns and unreviewed work remain outside downstream denominators.")
    print('Knowledge existence and material change are primary-agent assessments. Secondary review has declared reviewer separation and blinding, not external identity attestation. CI/merge/revert status never automatically fills either benefit stage.')
    print('\n### Fact recovery and useful knowledge categories\n')
    all_facts = [f for r in records for f in r['judgment']['key_facts']]
    recovery = Counter(f['code_would_reveal'] for f in all_facts)
    for label, count in recovery.most_common():
        print(f'- {label}: {count}/{len(all_facts)} ({percentage(100 * count / len(all_facts))})')
    print('\n| Category | Materially useful facts | Share | Irreducible useful facts | Material-fact denominator |')
    print('|---|---:|---:|---:|---:|')
    for category, count in sorted(data['categories'].items(), key=lambda item: (-item[1], item[0])):
        total = len(data['material_useful_facts'])
        print(f"| {category} | {count} | {percentage(100 * count / total)} | {data['irreducible_categories'].get(category, 0)} | {total} |")
    print('\n### Importance and intervention class (assessed)\n')
    material_records = [r for r in records if material(r)]
    print('| Severity | Beneficial self-assessment | Harmful self-assessment | Other/uncertain | n |')
    print('|---|---:|---:|---:|---:|')
    for severity in assessment.SEVERITY:
        cohort = [r for r in material_records if action(r)['severity'] == severity]
        positive = sum(r['judgment']['verdict']['contexer_effect'] in ('helpful', 'decisive') for r in cohort)
        negative = sum(r['judgment']['verdict']['contexer_effect'] == 'harmful' for r in cohort)
        print(f'| {severity} | {positive} | {negative} | {len(cohort)-positive-negative} | {len(cohort)} |')
    for label, count in Counter(action(r)['impact_class'] for r in material_records).most_common():
        print(f'- {label}: {count}/{len(material_records)} materially changed segments')
    print('Frequency, severity and intervention value are separate. A rare/high-impact pattern is testable here; it is not assumed, monetized or proof of commercial viability.')
    print('\n### Secondary review coverage and agreement\n')
    reviewed = [r for r in records if reviews.get(r['record_id'])]
    agreements = disagreements = uncertain = 0
    for record in reviewed:
        judgment = secondary.consensus(reviews[record['record_id']], 'secondary_helpful_judgment')
        primary = record['judgment']['verdict']['contexer_effect'] in ('helpful', 'decisive')
        if judgment == 'uncertain':
            uncertain += 1
        elif (judgment == 'yes') == primary:
            agreements += 1
        else:
            disagreements += 1
    print(f'Reviewed {len(reviewed)}/{len(records)} records; agreement {agreements}/{len(reviewed)}, disagreement {disagreements}/{len(reviewed)}, uncertain or conflicting reviewers {uncertain}/{len(reviewed)}; invalid/stale review rows skipped: {skipped}.')
    blind = Counter(row['blind_assessment']['blinding'] for rows in reviews.values() for row in rows)
    print(f'Blinding declarations: {dict(blind)}. Promising-case selection is biased; include a preregistered neutral sample and report selection separately.')
    print('\n### Product discovery hypotheses\n')
    if data['irreducible_categories']:
        focus = ', '.join(key for key, _ in Counter(data['irreducible_categories']).most_common(3))
        print(f'- Inspect capture/review support for {focus}; these categories supplied the most assessed irreducible, materially useful facts in this cohort.')
    else:
        print('- No assessed irreducible, materially useful facts yet; the data does not support a capture-category recommendation.')
    features = {}
    for record in records:
        for rating in record['judgment']['feature_ratings']:
            features.setdefault(rating['feature'], Counter())[rating['verdict']] += 1
    print('| Feature | Useful | Noise/harmful | Rated observations |')
    print('|---|---:|---:|---:|')
    ranked = sorted(features.items(), key=lambda item: (-(item[1]['noise']+item[1]['harmful'])/sum(item[1].values()), item[0]))
    recommendations = []
    for feature, counts in ranked:
        burden, total = counts['noise'] + counts['harmful'], sum(counts.values())
        print(f"| {feature} | {counts['useful']}/{total} | {burden}/{total} | {total} |")
        if total >= 5 and burden > counts['useful']:
            recommendations.append(f'Investigate reducing or correcting {feature}: {burden}/{total} noise/harmful versus {counts["useful"]}/{total} useful ratings. Test the change before generalizing.')
    print('\n' + '\n'.join(recommendations))
    print('Low opportunity frequency suggests rarity; low knowledge availability conditional on opportunity suggests a capture gap; low surfacing conditional on stored knowledge suggests retrieval/integration gaps. Retrieved but unchanged actions suggest redundancy. Disagreement from separate reviewers may expose persuasive but wrong context. These are hypotheses, not automatic product verdicts.')
    return data
