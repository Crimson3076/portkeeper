"""Readable research trail built from a saved drafting or investigation record.

Read-only: nothing here changes what approve, discard or build do. Every claim is tagged
verified (matched evidence), inferred (no matched evidence) or unverified (flagged by the
model or left unestablished)."""
import json
import re

READ_LABELS = {'release': 'Read release metadata', 'source': 'Read', 'search': 'Searched source for',
               'source_tree': 'Listed source files under', 'archive': 'Inspected archive listing',
               'archive_file': 'Read archive file', 'capabilities': 'Checked runner capabilities', 'inputs': 'Checked selected input metadata'}


def _model(record):
    model = record.get('model') or {}
    return model.get('provider'), model.get('drafting_model')


def _read(node_kind, source, at=None):
    label = READ_LABELS.get(node_kind, 'Read')
    return {'type': 'read', 'label': f'{label} {source}'.strip() if node_kind not in ('release', 'capabilities', 'inputs') else label,
            'source': source, 'at': at}


def _request(record, event):
    provider, model = _model(record)
    return {'type': 'model_request', 'label': {'review': 'Model review', 'investigation': 'Investigation request', 'draft': 'Draft attempt'}.get(event.get('kind'), 'Model request'),
            'provider': provider, 'model': model, 'attempt': event.get('attempt'), 'seconds': event.get('seconds'),
            'result': event.get('result'), 'at': event.get('at')}


def steps_for(record):
    events = record.get('attempts') or []
    requested = {item.get('id') for event in events for item in event.get('evidence_results') or [] if isinstance(item, dict) and item.get('id')}
    steps = [_read(node.get('kind'), node.get('source') or '') for node in record.get('evidence') or [] if isinstance(node, dict) and node.get('id') not in requested and node.get('kind') != 'inputs']
    corrections = 0
    for event in events:
        kind = event.get('kind')
        if kind == 'test_build':
            steps.append({'type': 'test_build', 'label': f"Test build {event.get('round', '')}".strip(), 'detail': str(event.get('feedback') or '')[:600], 'at': event.get('at')})
            continue
        steps.append(_request(record, event))
        for item in [i for i in event.get('evidence_results') or [] if isinstance(i, dict)]:
            steps.append({'type': 'read', 'label': 'Read ' + str(item['source']), 'source': item['source'], 'at': event.get('at')} if item.get('source') else
                         {'type': 'read_error', 'label': 'Evidence request failed', 'detail': str(item.get('error'))[:600], 'at': event.get('at')})
        if event.get('result') == 'rejected':
            corrections += 1
            steps.append({'type': 'correction', 'label': 'Correction needed', 'detail': str(event.get('error') or '')[:600], 'at': event.get('at')})
    return steps, corrections


def _flagged(plan, *words):
    return any(any(word in item.lower() for word in words) for item in plan.get('uncertainties') or [] if isinstance(item, str))


def _claim(claim, value, tag, source, detail=''):
    return {'claim': claim, 'value': value, 'tag': tag, 'source': source, 'detail': detail}


DATA_WORDS = re.compile(r'\b(data|game ?files?|rom|assets?|iso|disc|owned|extract)', re.I)


def _drafted(definition, target):
    """Whether the draft still launches what the plan says; None when the draft is unknown."""
    if not isinstance(definition, dict):
        return None
    command = ((definition.get('launch') or {}).get(target.get('target')) or {}).get('command')
    execs = {op.get('exec') for op in definition.get('operations') or [] if isinstance(op, dict) and op.get('op') == 'launcher' and op.get('path') == command}
    return command == target.get('launch') or target.get('launch') in execs


def claims_for(plan, record, definition=None):
    if not isinstance(plan, dict):
        return []
    claims, listings = [], record.get('archive_evidence') or (record.get('context') or {}).get('archive_evidence') or {}
    layouts = record.get('archive_layouts') or ((record.get('context') or {}).get('release') or {}).get('archive_layouts') or {}
    refs = [ref for ref in plan.get('evidence') or [] if isinstance(ref, dict) and ref.get('matched')]
    for target in plan.get('targets') or []:
        name = str(target.get('target', 'target')).capitalize()
        asset, launch = target.get('asset'), target.get('launch')
        released = target.get('method') == 'release' and asset
        problems = ' '.join(plan.get('capability_problems') or [])
        if released and launch:
            members = listings.get(asset) or []
            found = any(path.rstrip('/') == launch or path.rstrip('/').endswith('/' + launch) for path in members)
            if _flagged(plan, 'launch'):
                tag, source = 'unverified', 'Flagged by the model'
            elif found:
                tag, source = 'verified', f'Archive listing of {asset}'
            else:
                tag, source = 'inferred', f'Model reasoning; {asset} listing ' + ('does not contain it' if layouts.get(asset, {}).get('complete') else 'not fully inspected')
            if definition is not None and _drafted(definition, target) is False:
                tag, source = 'unverified', 'The draft changed this launch path from the investigation plan'
            claims.append(_claim(f'{name} launch path', launch, tag, source))
        else:
            claims.append(_claim(f'{name} launch path', launch or 'Not established', 'unverified', 'No playable release runtime established'))
        if released:
            named = f'{target.get("target")}: ' in problems
            positive = asset in listings or any(asset in (ref.get('quote') or '') or asset in (ref.get('path') or '') for ref in refs)
            tag = 'unverified' if named or _flagged(plan, str(target.get('target', '\0'))) else 'verified' if positive else 'inferred'
            if tag == 'unverified' and named:
                source = 'A capability check rejected this release file'
            else:
                source = f'Release file {asset}' + ('' if positive else '; not inspected or quoted')
            if definition is not None and tag != 'unverified' and asset not in json.dumps(definition):
                tag, source = 'unverified', 'The draft does not use this release file'
            claims.append(_claim(f'{name} platform support', asset, tag, source))
        else:
            claims.append(_claim(f'{name} platform support', target.get('method', 'unknown'), 'unverified', 'No playable release runtime established'))
    files = plan.get('game_files') or {}
    if files:
        if files.get('needed_to_play') is None or files.get('needed_for_packaging') is None or _flagged(plan, 'game data', 'game file', 'owned'):
            claims.append(_claim('Required game data', files.get('requirements'), 'unverified', 'Flagged by the model or not established'))
        else:
            support = [ref for ref in refs if DATA_WORDS.search(str(ref.get('quote') or ''))]
            if support:
                ref = support[0]
                claims.append(_claim('Required game data', files.get('requirements'), 'verified', f"{ref.get('path') or ref.get('source')} (fact {ref.get('source')})", ref.get('quote', '')))
            else:
                claims.append(_claim('Required game data', files.get('requirements'), 'inferred', 'Model reasoning; no matched quotation'))
    return claims


def build(record, plan=None):
    plan = plan or record.get('plan')
    steps, corrections = steps_for(record)
    provider, model = _model(record)
    return {'job_id': record.get('job_id'), 'kind': record.get('kind', 'draft'), 'status': record.get('status'), 'outcome': (plan or {}).get('outcome'),
            'failure': record.get('error'), 'provider': provider, 'model': model, 'started_at': record.get('created_at'), 'finished_at': record.get('finished_at'),
            'steps': steps, 'corrections': corrections, 'claims': claims_for(plan, record, record.get('accepted_definition')),
            'claims_note': 'Claims come from the reviewed investigation plan' + (' and are checked against the draft.' if record.get('accepted_definition') else '.') if plan and record.get('kind') != 'investigation' else ''}
