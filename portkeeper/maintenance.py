"""Explicit housekeeping with private backups; never removes game/archive files."""
import json
import re
import shutil
import sqlite3
from . import packaging
from .safety import Invalid
from .storage import identifier, now


def manage(engine, payload):
    action = payload.get('action')
    if action not in ('clear_activity', 'clear_revisions', 'reset_instructions', 'remove_game'):
        raise Invalid('Choose a supported cleanup action')
    project_id = payload.get('project_id')
    job_id = payload.get('job_id')
    if any(value is not None and (not isinstance(value, str) or not re.fullmatch(r'[a-f0-9]{32}', value)) for value in (project_id, job_id)):
        raise Invalid('Choose a valid game or activity entry')
    if job_id and action != 'clear_activity':
        raise Invalid('A job can only have its activity and logs cleared')
    project = engine.store.get('projects', project_id) if project_id else None
    if action != 'clear_activity' and not project:
        raise Invalid('Choose a game first')
    jobs = [j for j in engine.store.all('jobs') if not project_id or j.get('project_id') == project_id]
    if job_id:
        jobs = [engine.store.get('jobs', job_id)]
        if project_id and jobs[0].get('project_id') != project_id:
            raise Invalid('This job belongs to another game')
    active = lambda j: not j.get('finished_at') and j.get('status') not in ('ready', 'failed', 'cancelled', 'complete', 'completed')
    if action != 'clear_activity' and (any(active(j) for j in jobs) or any(t.get('project_id') == project_id and t.get('status') in ('running', 'queued') for t in engine.store.all('trials'))):
        raise Invalid('Wait for this game’s running work to finish before changing or removing it')
    if job_id and active(jobs[0]):
        raise Invalid('Running activity cannot be cleared')
    with engine.lock:
        root = engine.data / 'maintenance-backups' / identifier()
        root.mkdir(parents=True, mode=0o700)
        # SQLite backup includes a consistent snapshot of WAL transactions.
        with engine.store.connect() as source, sqlite3.connect(root / 'portkeeper.sqlite3') as target:
            source.backup(target)
        result = {'action': action, 'backup': str(root), 'jobs_cleared': 0, 'files_deleted': 0}
        if action == 'clear_activity':
            for job in jobs:
                if active(job):
                    continue
                draft = engine.data / 'drafts' / (job['id'] + '.json')
                if draft.is_file() and not draft.is_symlink():
                    shutil.move(str(draft), root / draft.name)
                engine.store.update('jobs', job['id'], logs=[], checkpoints=[], activity_hidden=True, draft_record_available=False)
                result['jobs_cleared'] += 1
            if not job_id:
                cutoffs = engine.get_setting('activity_cutoffs', {'projects': {}})
                cutoffs.setdefault('projects', {})
                if project_id:
                    cutoffs['projects'][project_id] = now()
                else:
                    cutoffs['all'] = now()
                engine.save_setting('activity_cutoffs', cutoffs)
                for notice in engine.store.all('notifications'):
                    if not project_id or notice.get('project_id') == project_id:
                        engine.store.delete('notifications', notice['id'])
        elif action == 'clear_revisions':
            engine.store.update('projects', project_id, definition_history=[])
        elif action == 'reset_instructions':
            definition = packaging.capture_definition(project, 'Build instructions cleared by the user. Releases and existing packages are retained.')
            history = project.get('definition_history', [])
            if project.get('packaging'):
                history = (history + [{'saved_at': now(), 'definition': project['packaging']}])[-20:]
            project.update(packaging=definition, recipe={'version': 1, 'mode': 'capture', 'payload': {'kind': 'source'}}, definition_history=history, input_bindings={}, auto_build=False, auto_publish=False, auto_draft_platforms=False, definition_source=None)
            project.pop('profile', None)
            engine.store.put('projects', project)
            for proposal in engine.store.all('proposals'):
                if proposal.get('project_id') == project_id and proposal.get('status') == 'pending_review':
                    engine.store.update('proposals', proposal['id'], status='discarded')
        else:
            # Remove only app records. Packages, source captures, owned inputs,
            # library copies, RomM data and configuration backups stay on disk.
            for table in engine.store.tables - {'settings', 'projects'}:
                for record in engine.store.all(table):
                    if record.get('project_id') == project_id:
                        engine.store.delete(table, record['id'])
            engine.store.delete('projects', project_id)
        (root / 'action.json').write_text(json.dumps({**result, 'project_id': project_id, 'job_id': job_id, 'created_at': now()}, indent=2))
        return result
