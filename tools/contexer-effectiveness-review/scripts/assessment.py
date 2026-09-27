"""Version-three assessments and auditable descriptive metrics; no causal estimator."""
import copy
import re

TRI = ['yes', 'no', 'uncertain']
CATEGORIES = ['rationale', 'intent', 'exception', 'migration_future_state', 'ownership_boundary',
              'business_constraint', 'rejected_alternative', 'incident_lesson', 'authority',
              'lifecycle_supersession', 'implementation_fact', 'other']
SOURCES = ['current_code', 'repo_docs', 'git_history', 'pr_history', 'issue_history',
           'external_docs', 'conversation_only', 'no_reasonable_alternative', 'uncertain']
RECOVERY = ['current_code_would_reveal', 'current_code_suggests_but_does_not_prove',
            'repo_docs_would_reveal', 'git_or_pr_history_would_reveal', 'external_docs_required',
            'human_or_organizational_knowledge_only', 'unknown']
RECOVERY_FLAGS = ['could_current_code_reveal', 'could_repo_docs_reveal',
                  'could_git_history_reveal', 'could_pr_history_reveal', 'could_external_docs_reveal']
CHANGES = ['no_change', 'minor_change', 'material_change', 'prevented_wrong_action', 'uncertain']
MATERIAL = {'material_change', 'prevented_wrong_action'}
IMPACTS = ['convenience', 'reduced_rediscovery', 'avoided_rework', 'avoided_architecture_drift',
           'avoided_policy_violation', 'prevented_potentially_consequential_mistake', 'uncertain']
SEVERITY = ['low', 'medium', 'high', 'unknown']
FACT_FIELDS = ['fact_id', 'knowledge_category', 'action_change', 'alternative_sources', *RECOVERY_FLAGS]
OPPORTUNITY_FIELDS = ['non_code_context_opportunity', 'opportunity_type', 'opportunity_reason',
                      'required_fact_location', 'relevant_knowledge_existed', 'knowledge_surfaced',
                      'knowledge_evidence', *RECOVERY_FLAGS]
ACTION_FIELDS = ['contexer_used', 'action_change', 'alternative_sources', 'impact_class',
                 'severity', 'action_before', 'action_after', 'evidence', 'material_fact_ids']


def shape(value, fields, path, errors):
    if not isinstance(value, dict):
        errors.append(f'{path} must be an object')
        return False
    for missing in set(fields) - value.keys():
        errors.append(f'{path}.{missing} is required; use an explicit uncertainty value')
    for extra in value.keys() - set(fields):
        errors.append(f'{path}.{extra} is not a schema field')
    return not (set(fields) - value.keys())


def choice(value, allowed, path, errors):
    if not isinstance(value, str) or value not in allowed:
        errors.append(f'{path} must be one of {allowed}')


def choices(value, allowed, path, errors, empty=False):
    if not isinstance(value, list) or any(not isinstance(v, str) or v not in allowed for v in value):
        errors.append(f'{path} must be a list drawn from {allowed}')
    elif len(value) != len(set(value)) or (not empty and not value):
        errors.append(f'{path} must contain unique values' + ('' if empty else ' and cannot be empty'))
    elif 'uncertain' in value and len(value) > 1:
        errors.append(f'{path}: uncertain cannot be combined with known sources')


def note(value, path, errors, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        errors.append(f'{path} must be non-empty text of at most {limit} characters')


def recovery(value, path, errors):
    for key in RECOVERY_FLAGS:
        choice(value.get(key), TRI, f'{path}.{key}', errors)


def validate(judgment, observed=None):
    errors = []
    if not isinstance(judgment, dict):
        return ['judgment must be an object']
    task = judgment.get('task', {})
    task_id = task.get('task_id') if isinstance(task, dict) else None
    if not isinstance(task_id, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,79}', task_id):
        errors.append('task.task_id must be a stable task identifier of 1–80 simple characters')
    opportunity = judgment.get('opportunity')
    if shape(opportunity, OPPORTUNITY_FIELDS, 'opportunity', errors):
        choice(opportunity['non_code_context_opportunity'], TRI, 'opportunity.non_code_context_opportunity', errors)
        choices(opportunity['opportunity_type'], [c for c in CATEGORIES if c != 'implementation_fact'],
                'opportunity.opportunity_type', errors, empty=True)
        for key in ('opportunity_reason', 'knowledge_evidence'):
            note(opportunity[key], f'opportunity.{key}', errors)
        choices(opportunity['required_fact_location'], SOURCES, 'opportunity.required_fact_location', errors)
        for key in ('relevant_knowledge_existed', 'knowledge_surfaced'):
            choice(opportunity[key], TRI, f'opportunity.{key}', errors)
        recovery(opportunity, 'opportunity', errors)
        if opportunity['non_code_context_opportunity'] == 'yes':
            if not opportunity['opportunity_type']:
                errors.append('An opportunity needs at least one opportunity_type')
            if opportunity['could_current_code_reveal'] == 'yes':
                errors.append('A non-code opportunity cannot be safely revealed by current code alone')
        if opportunity['non_code_context_opportunity'] == 'no' and opportunity['opportunity_type']:
            errors.append('No opportunity requires an empty opportunity_type list')
        if opportunity['knowledge_surfaced'] == 'yes' and opportunity['relevant_knowledge_existed'] != 'yes':
            errors.append('Surfaced relevant knowledge must have existed')
    action = judgment.get('action')
    if shape(action, ACTION_FIELDS, 'action', errors):
        for key, allowed in [('contexer_used', TRI), ('action_change', CHANGES),
                             ('impact_class', IMPACTS), ('severity', SEVERITY)]:
            choice(action[key], allowed, f'action.{key}', errors)
        choices(action['alternative_sources'], SOURCES, 'action.alternative_sources', errors)
        for key in ('action_before', 'action_after', 'evidence'):
            note(action[key], f'action.{key}', errors)
        if action['contexer_used'] == 'no' and action['action_change'] in [*MATERIAL, 'minor_change']:
            errors.append('Unused Contexer cannot change an action')
        if observed and action['contexer_used'] == 'no' and (
                observed.get('contexer_observed_call_count') or observed.get('contexer_call_count')
                or observed.get('autofetch_blocks')):
            errors.append('action.contexer_used=no contradicts observed Contexer activity')
        if action['contexer_used'] == 'yes' and not judgment.get('feature_ratings'):
            errors.append('feature_ratings must rate the Contexer features that appeared')
        if action['contexer_used'] == 'no' and (judgment.get('contexer_items') or judgment.get('captures')):
            errors.append('action.contexer_used=no contradicts surfaced items or captures')
        if isinstance(opportunity, dict) and opportunity.get('knowledge_surfaced') == 'yes' and action['contexer_used'] != 'yes':
            errors.append('Known surfaced Contexer knowledge requires contexer_used=yes')
    facts = judgment.get('key_facts')
    ids = []
    if isinstance(facts, list):
        for index, fact in enumerate(facts):
            if not isinstance(fact, dict):
                continue  # The common validator reports the base shape error.
            path = f'key_facts[{index}]'
            fact_id = fact.get('fact_id')
            if not isinstance(fact_id, str) or not re.fullmatch(r'fact-[1-9][0-9]*', fact_id):
                errors.append(f'{path}.fact_id must look like fact-1')
            else:
                ids.append(fact_id)
            choice(fact.get('knowledge_category'), CATEGORIES, f'{path}.knowledge_category', errors)
            choice(fact.get('action_change'), CHANGES, f'{path}.action_change', errors)
            choice(fact.get('code_would_reveal'), RECOVERY, f'{path}.code_would_reveal', errors)
            choices(fact.get('alternative_sources'), SOURCES, f'{path}.alternative_sources', errors)
            recovery(fact, path, errors)
            label = fact.get('code_would_reveal')
            if label == 'current_code_would_reveal' and fact.get('could_current_code_reveal') != 'yes':
                errors.append(f'{path}: current-code recovery must be marked yes')
            if label == 'current_code_suggests_but_does_not_prove' and fact.get('could_current_code_reveal') == 'yes':
                errors.append(f'{path}: suggestion is not reliable recovery')
            if label == 'repo_docs_would_reveal' and fact.get('could_repo_docs_reveal') != 'yes':
                errors.append(f'{path}: document recovery must be marked yes')
            if label == 'git_or_pr_history_would_reveal' and not any(
                    fact.get(key) == 'yes' for key in ('could_git_history_reveal', 'could_pr_history_reveal')):
                errors.append(f'{path}: history recovery needs a known yes source')
            if label == 'human_or_organizational_knowledge_only' and any(fact.get(key) != 'no' for key in RECOVERY_FLAGS):
                errors.append(f'{path}: human-only knowledge requires all repository/external recovery flags to be no')
            if label == 'external_docs_required' and fact.get('could_external_docs_reveal') != 'yes':
                errors.append(f'{path}: external document recovery must be marked yes')
        if len(ids) != len(set(ids)):
            errors.append('key_facts.fact_id values must be unique within a record')
    if errors:
        return errors
    if isinstance(action, dict):
        material_ids = action.get('material_fact_ids')
        if not isinstance(material_ids, list) or any(not isinstance(v, str) or v not in ids for v in material_ids):
            errors.append('action.material_fact_ids must reference key_facts.fact_id')
        elif len(material_ids) != len(set(material_ids)):
            errors.append('action.material_fact_ids cannot contain duplicates')
        elif action.get('action_change') in MATERIAL:
            linked = [fact for fact in facts or [] if isinstance(fact, dict) and fact.get('fact_id') in material_ids]
            if not linked or any(fact.get('source') != 'contexer' or fact.get('action_change') not in MATERIAL for fact in linked):
                errors.append('Material Contexer action change needs linked material Contexer-sourced facts')
            items = judgment.get('contexer_items', [])
            prior_ids = {item.get('id') for item in items if isinstance(item, dict)
                         and isinstance(item.get('id'), str) and item.get('created_this_session') is False} if isinstance(items, list) else set()
            if any(not isinstance(fact.get('contexer_ids'), list)
                   or not any(isinstance(key, str) and key in prior_ids for key in fact['contexer_ids']) for fact in linked):
                errors.append('Material action change cannot rely on this session’s own captures')
            if action.get('contexer_used') != 'yes':
                errors.append('Material action change requires contexer_used=yes')
            if isinstance(opportunity, dict) and opportunity.get('non_code_context_opportunity') == 'yes' and opportunity.get('knowledge_surfaced') != 'yes':
                errors.append('Material opportunity intervention requires relevant knowledge to have surfaced')
        elif material_ids:
            errors.append('Only a material action change may name material_fact_ids')
    return errors


def base_projection(judgment):
    """Keep the existing provenance/credit checks while validating new fields separately."""
    projected = copy.deepcopy(judgment)
    projected.pop('opportunity', None)
    projected.pop('action', None)
    task = projected.get('task')
    if isinstance(task, dict):
        task.pop('task_id', None)
    for fact in projected.get('key_facts', []) if isinstance(projected.get('key_facts'), list) else []:
        if isinstance(fact, dict):
            for key in FACT_FIELDS:
                fact.pop(key, None)
            fact['code_would_reveal'] = 'unknown'  # Rich recovery is validated above, not guessed from the old aggregate.
    return projected
