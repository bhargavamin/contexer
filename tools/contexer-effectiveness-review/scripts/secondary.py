#!/usr/bin/env python3
"""Optional two-stage secondary review. Artifacts stay local; reviewer independence is declared."""
import argparse
import hashlib
import json
import math
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parent))
import assessment  # noqa: E402
import log_usage  # noqa: E402
import privacy  # noqa: E402
import stop_hook  # noqa: E402

ROOT = log_usage.ROOT
SCHEMA = 'contexer-secondary/v1'
BLIND_FIELDS = ['secondary_reviewer_type', 'reviewer_id', 'reviewer_session_id', 'blinding',
                'secondary_required_facts', 'secondary_code_recoverable_judgment', 'secondary_notes']
FINAL_FIELDS = ['secondary_helpful_judgment', 'secondary_notes',
                'later_outcome_corroborated', 'later_outcome_evidence']


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def file_digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def text_file(path):
    with Path(path).open('rb') as stream:
        data = stream.read(250_001)
    if len(data) > 250_000:
        raise ValueError('Curated text evidence must be at most 250 KB per file')
    text = data.decode('utf-8')
    if not text.strip() or privacy.SECRET.search(text):
        raise ValueError('Evidence must be non-empty and omit known credential formats')
    return text


def record_for(record_id):
    found = []
    for path in log_usage.record_paths():
        for line in path.read_text().splitlines():
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if isinstance(record, dict) and record.get('record_id') == record_id:
                found.append(record)
    if len(found) != 1 or log_usage.validate(found[0].get('judgment'), schema=found[0].get('schema')):
        raise ValueError('Select exactly one valid v2 or v3 record')
    return found[0]


def case_path(case_id):
    if not isinstance(case_id, str) or len(case_id) != 12 or any(c not in '0123456789abcdef' for c in case_id):
        raise ValueError('Invalid case id')
    return ROOT / 'secondary_cases' / f'{case_id}.json'


def load_case(case_id):
    case = json.loads(case_path(case_id).read_text())
    record = record_for(case['record_id'])
    if digest(record) != case['record_sha256']:
        raise ValueError('The source record changed; prepare a new case')
    if file_digest(case['snapshot']['path']) != case['snapshot']['sha256']:
        raise ValueError('The repository snapshot changed; prepare a new case')
    packet = json.loads(Path(case['packet_path']).read_text())
    if digest(packet) != case['packet_sha256']:
        raise ValueError('The blind packet changed; prepare a new case')
    return case, record


def prepare(record_id, task, snapshot, change, evidence):
    record = record_for(record_id)
    case_id = uuid.uuid4().hex[:12]
    snapshot = {'path': str(Path(snapshot).resolve()), 'sha256': file_digest(snapshot)}
    # No primary verdict, Contexer-item judgment, model, session or record id goes in this packet.
    packet = {'schema': 'contexer-secondary-packet/v1', 'case_id': case_id,
              'task_request': text_file(task), 'repository_snapshot': snapshot,
              'resulting_change': text_file(change), 'tool_evidence': text_file(evidence),
              'blinding': 'curator_must_check_for_treatment_and_judgment_leakage'}
    packet_path = ROOT / 'secondary_packets' / f'{case_id}-blind.json'
    case = {'case_id': case_id, 'record_id': record_id, 'record_sha256': digest(record),
            'snapshot': snapshot, 'packet_path': str(packet_path), 'packet_sha256': digest(packet),
            'secondary_review_status': 'prepared', 'prepared_at': time.time()}
    stop_hook.write_json(packet_path, packet)
    stop_hook.write_json(case_path(case_id), case)
    return case


def validate_blind(review, record):
    errors = []
    if not assessment.shape(review, BLIND_FIELDS, 'secondary', errors):
        return errors
    assessment.choice(review['secondary_reviewer_type'], ['human', 'independent_agent'], 'secondary_reviewer_type', errors)
    assessment.note(review['reviewer_id'], 'reviewer_id', errors)
    assessment.choice(review['blinding'], ['blind', 'partial', 'unblinded'], 'blinding', errors)
    assessment.choice(review['secondary_code_recoverable_judgment'], assessment.TRI,
                      'secondary_code_recoverable_judgment', errors)
    assessment.note(review['secondary_notes'], 'secondary_notes', errors, 300)
    session = review['reviewer_session_id']
    if review['secondary_reviewer_type'] == 'independent_agent':
        if not isinstance(session, str) or not session.strip() or len(session) > 200 or session == record.get('session_id'):
            errors.append('An independent agent must declare a different reviewer_session_id')
    elif session is not None:
        errors.append('A human review uses reviewer_session_id=null')
    facts = review['secondary_required_facts']
    if not isinstance(facts, list) or not facts:
        errors.append('secondary_required_facts must be a non-empty list')
    else:
        for index, fact in enumerate(facts):
            path = f'secondary_required_facts[{index}]'
            if assessment.shape(fact, ['fact', 'available_from', 'current_repo_recoverable'], path, errors):
                assessment.note(fact['fact'], f'{path}.fact', errors)
                assessment.choices(fact['available_from'], assessment.SOURCES, f'{path}.available_from', errors)
                assessment.choice(fact['current_repo_recoverable'], assessment.TRI, f'{path}.current_repo_recoverable', errors)
    if any(privacy.SECRET.search(text) for text in log_usage._texts(review)):
        errors.append('Secondary review must omit known credentials')
    return errors


def validate_final(review):
    errors = []
    if not assessment.shape(review, FINAL_FIELDS, 'secondary', errors):
        return errors
    for key in ('secondary_helpful_judgment', 'later_outcome_corroborated'):
        assessment.choice(review[key], assessment.TRI, key, errors)
    for key in ('secondary_notes', 'later_outcome_evidence'):
        assessment.note(review[key], key, errors, 300)
    if any(privacy.SECRET.search(text) for text in log_usage._texts(review)):
        errors.append('Secondary review must omit known credentials')
    return errors


def seal_blind(case_id, review):
    with stop_hook.session_lock(f'secondary-{case_id}'):
        case, record = load_case(case_id)
        if case['secondary_review_status'] != 'prepared':
            raise ValueError('The blind assessment is already sealed; create another case for another reviewer')
        errors = validate_blind(review, record)
        if errors:
            raise ValueError('; '.join(errors))
        case.update(blind_review=review, blind_sha256=digest(review), blind_sealed_at=time.time(),
                    secondary_review_status='blind_complete')
        stop_hook.write_json(case_path(case_id), case)
    return case


def reveal(case_id, evidence):
    with stop_hook.session_lock(f'secondary-{case_id}'):
        case, _ = load_case(case_id)
        if case['secondary_review_status'] != 'blind_complete':
            raise ValueError('Seal the blind required-facts assessment before revealing attribution')
        packet = {'case_id': case_id, 'attributed_tool_evidence': text_file(evidence),
                  'instruction': 'Assess benefit using the sealed facts; do not consult the original helpfulness judgment.'}
        path = ROOT / 'secondary_packets' / f'{case_id}-attribution.json'
        stop_hook.write_json(path, packet)
        case.update(attribution_path=str(path), attribution_sha256=digest(packet), revealed_at=time.time(),
                    secondary_review_status='attribution_revealed')
        stop_hook.write_json(case_path(case_id), case)
    return case


def complete(case_id, review):
    with stop_hook.session_lock(f'secondary-{case_id}'):
        case, record = load_case(case_id)
        # Recover a successful append whose subsequent case-state write was interrupted.
        rows, _ = load_reviews([record])
        prior = next((row for row in rows.get(record['record_id'], []) if row['case_id'] == case_id), None)
        if prior is not None:
            if review != {key: prior[key] for key in FINAL_FIELDS}:
                raise ValueError('Completed secondary review is immutable; prepare another case')
            case.update(secondary_review_status='completed')
            stop_hook.write_json(case_path(case_id), case)
            return prior
        if case['secondary_review_status'] != 'attribution_revealed':
            raise ValueError('Complete the blind and attribution stages first')
        if digest(case['blind_review']) != case['blind_sha256']:
            raise ValueError('Sealed blind assessment changed')
        attribution = json.loads(Path(case['attribution_path']).read_text())
        if digest(attribution) != case['attribution_sha256']:
            raise ValueError('Attribution evidence changed')
        errors = validate_blind(case['blind_review'], record) + validate_final(review)
        if errors:
            raise ValueError('; '.join(errors))
        result = {'schema': SCHEMA, 'case_id': case_id, 'record_id': case['record_id'],
                  'record_key': f'secondary:{case_id}', 'record_sha256': case['record_sha256'],
                  'secondary_review_status': 'completed', 'independence': 'declared_not_externally_attested',
                  'blind_assessment': case['blind_review'], **review,
                  'blind_sealed_at': case['blind_sealed_at'], 'revealed_at': case['revealed_at'],
                  'completed_at': time.time()}
        log_usage.append(ROOT / 'secondary_reviews.jsonl', result)
        case.update(secondary_review_status='completed')
        stop_hook.write_json(case_path(case_id), case)
    return result


def load_reviews(records):
    by_id = {record['record_id']: record for record in records}
    grouped, skipped, seen = {}, 0, set()
    path = ROOT / 'secondary_reviews.jsonl'
    if not path.exists():
        return grouped, skipped
    for line in path.read_text().splitlines():
        try:
            row = json.loads(line)
            record = by_id.get(row.get('record_id')) if isinstance(row, dict) else None
            if record is None:
                continue
            if (row.get('schema') != SCHEMA or row.get('secondary_review_status') != 'completed'
                    or row.get('record_sha256') != digest(record)
                    or validate_blind(row.get('blind_assessment'), record)
                    or validate_final({key: row.get(key) for key in FINAL_FIELDS})):
                raise ValueError('Invalid secondary review')
            times = [row[key] for key in ('blind_sealed_at', 'revealed_at', 'completed_at')]
            if (any(type(value) not in (int, float) or not math.isfinite(value) or value <= 0 for value in times)
                    or times != sorted(times)
                    or row.get('independence') != 'declared_not_externally_attested'):
                raise ValueError('Invalid review order')
            case_path(row['case_id'])
            identity = (row['record_id'], row['case_id'])
            if identity in seen:
                continue
            seen.add(identity)
            grouped.setdefault(row['record_id'], []).append(row)
        except (ValueError, KeyError, TypeError):
            skipped += 1
    return grouped, skipped


def consensus(rows, field):
    values = {row[field] for row in rows}
    if values == {'yes'}:
        return 'yes'
    if values == {'no'}:
        return 'no'
    return 'uncertain'  # Missing review, uncertainty and reviewer disagreement never become approval.


def blind_consensus(rows, field):
    """Only a declared blind first stage qualifies for the independent-benefit funnel."""
    return consensus([row for row in rows if row['blind_assessment']['blinding'] == 'blind'], field)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest='command', required=True)
    candidates = subs.add_parser('candidates')
    candidates.add_argument('--neutral-percent', type=int, default=10)
    candidates.add_argument('--seed', default='pilot-1')
    prep = subs.add_parser('prepare')
    for flag in ('record-id', 'task', 'snapshot', 'change', 'evidence'):
        prep.add_argument(f'--{flag}', required=True)
    for name in ('blind', 'reveal', 'complete'):
        sub = subs.add_parser(name)
        sub.add_argument('--case-id', required=True)
        if name == 'reveal':
            sub.add_argument('--evidence', required=True)
    args = parser.parse_args()
    try:
        if args.command == 'candidates':
            if not 0 <= args.neutral_percent <= 100:
                raise ValueError('neutral-percent must be from 0 to 100')
            for path in log_usage.record_paths():
                for line in path.read_text().splitlines():
                    try:
                        record = json.loads(line)
                        effect = record['judgment']['verdict']['contexer_effect']
                        sampled = int(digest([args.seed, record['record_id']])[:8], 16) % 100 < args.neutral_percent
                        if effect in ('helpful', 'decisive', 'harmful') or effect == 'neutral' and sampled:
                            print(record['record_id'])
                    except (ValueError, KeyError, TypeError):
                        continue
            return
        if args.command == 'prepare':
            result = prepare(args.record_id, args.task, args.snapshot, args.change, args.evidence)
        elif args.command == 'reveal':
            result = reveal(args.case_id, args.evidence)
        else:
            review = json.load(sys.stdin)
            result = seal_blind(args.case_id, review) if args.command == 'blind' else complete(args.case_id, review)
        print(json.dumps({key: result[key] for key in ('case_id', 'secondary_review_status', 'packet_path', 'attribution_path') if key in result}))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f'Secondary review failed: {exc}\n')


if __name__ == '__main__':
    main()
