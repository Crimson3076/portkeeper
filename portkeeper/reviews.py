"""Explicit checks for exact build archives and discovered RomM packages."""
import hashlib
import json
import time
from .romm import _normalize_record
from .safety import Invalid
from .storage import identifier, now

RESULTS = {'untested': 'Not tested', 'starts': 'Starts successfully', 'played': 'Played successfully', 'failed': 'Doesn’t work'}


def signature(record):
    files = [{key: file.get(key) for key in ('id', 'name', 'size', 'sha1', 'updated_at')} for file in record.get('files', [])]
    files.sort(key=lambda file: (str(file.get('id')), str(file.get('name'))))
    if not files:
        raise Invalid('RomM has not indexed the files for this download yet')
    return hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()


class Reviews:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store
        self.cache = {}

    def family(self, project):
        metadata = project.get('metadata') or {}
        if not metadata.get('filename_prefix'):
            return [project]
        return [item for item in self.store.all('projects') if (item.get('metadata') or {}).get('filename_prefix', '').lower() == metadata['filename_prefix'].lower() and (item.get('metadata') or {}).get('igdb_id') == metadata.get('igdb_id')]

    def prefixes(self, project):
        return list(dict.fromkeys([value for value in [(project.get('metadata') or {}).get('filename_prefix'), *[item.get('library_basename') for item in self.family(project)]] if value]))

    def belongs(self, project, record):
        return record.get('platform_slug') in ('linux', 'win') and any(file.get('name', '').lower().endswith('.zip') and any(file.get('name', '').lower().startswith(prefix.lower() + '-') for prefix in self.prefixes(project)) for file in record.get('files', []))

    def reference(self, record):
        server = self.engine.romm.public_config()['url']
        return {'kind': 'romm', 'romm_rom_id': record['id'], 'server': server, 'signature': signature(record)}

    def latest(self, reference):
        return next((item for item in self.store.all('playtests') if item['package'] == reference), None)

    def imports(self, project_id, force=False):
        project = self.store.get('projects', project_id)
        server = self.engine.romm.public_config().get('url')
        key = (server, project_id, tuple(self.prefixes(project)))
        cached = self.cache.get(key)
        if cached and not force and time.monotonic() - cached[0] < 60:
            records = cached[1]
        else:
            records = {}
            for prefix in self.prefixes(project):
                offset = 0
                while True:
                    result = self.engine.romm.search(prefix, offset=offset, limit=100)
                    for record in result['items']:
                        if self.belongs(project, record):
                            records[record['id']] = record
                    offset += len(result['items'])
                    if not result['items'] or offset >= result['total']:
                        break
            records = list(records.values())
            self.cache[key] = (time.monotonic(), records)
        items = []
        for record in records:
            reference = self.reference(record)
            event_id = hashlib.sha256(json.dumps(reference, sort_keys=True).encode()).hexdigest()
            with self.engine.lock:
                try:
                    self.store.get('imports', event_id)
                except KeyError:
                    self.store.put('imports', {'id': event_id, 'kind': 'import', 'project_id': project_id, 'project_name': project['name'], 'target': 'windows' if record['platform_slug'] == 'win' else 'linux', 'package': reference, 'package_name': record['name'], 'created_at': now(), 'message': 'Found in RomM. This records when Portkeeper discovered it, not its original upload time.'})
            items.append({**record, 'package_signature': reference['signature'], 'review': self.latest(reference)})
        return {'items': items}

    def record(self, project_id, payload, origin='manual'):
        project = self.store.get('projects', project_id)
        result = payload.get('result')
        if not isinstance(result, str) or result not in RESULTS:
            raise Invalid('Choose Not tested, Starts successfully, Played successfully, or Doesn’t work')
        device, notes = payload.get('device', ''), payload.get('notes', '')
        if not isinstance(device, str) or len(device) > 200 or not isinstance(notes, str) or len(notes) > 4000:
            raise Invalid('Enter a device up to 200 characters and notes up to 4000 characters')
        if result != 'untested' and (not device.strip() or payload.get('confirmed') is not True):
            raise Invalid('Enter your device and confirm you tested this exact download')
        if result == 'failed' and not notes.strip():
            raise Invalid('Describe what did not work')
        artifact_id, rom_id = payload.get('artifact_id'), payload.get('romm_rom_id')
        if bool(artifact_id) == bool(rom_id):
            raise Invalid('Choose exactly one build archive or RomM download')
        artifact = None
        if artifact_id:
            if not isinstance(artifact_id, str):
                raise Invalid('Invalid build archive ID')
            artifact = self.store.get('artifacts', artifact_id)
            if artifact['project_id'] not in {item['id'] for item in self.family(project)}:
                raise Invalid('This build archive belongs to another game')
            reference = {'kind': 'artifact', 'artifact_id': artifact_id, 'signature': artifact['sha256']}
            target, name = artifact['target'], artifact.get('filename', artifact['version'])
        else:
            if type(rom_id) is not int or rom_id < 1:
                raise Invalid('Choose a valid RomM download')
            record = _normalize_record(self.engine.romm._json('roms/' + str(rom_id)))
            if not self.belongs(project, record):
                raise Invalid('This RomM download belongs to another game or is not a PC port archive')
            reference = self.reference(record)
            target, name = ('windows' if record['platform_slug'] == 'win' else 'linux'), record['name']
        expected = payload.get('signature')
        if expected and expected != reference['signature']:
            raise Invalid('The download changed since you opened this check. Reload it and test the new version')
        if not expected and origin == 'manual':
            raise Invalid('Reload this download before recording a check')
        with self.engine.lock:
            if artifact:
                levels = {'untested': 'packaged', 'starts': 'launch_checked', 'played': 'user_confirmed_playable', 'failed': 'failed'}
                self.engine.lifecycle.validate(artifact_id, levels[result], (device.strip() + ': ' + notes.strip()).strip(': '), {'manual_check': True, 'device': device.strip()})
            event = {'id': identifier(), 'kind': 'test', 'project_id': project_id, 'project_name': project['name'], 'target': target, 'package': reference, 'package_name': name, 'result': result, 'device': device.strip(), 'notes': notes.strip(), 'origin': origin, 'created_at': now()}
            self.store.put('playtests', event)
        return event

    def activity(self):
        return sorted(self.store.all('imports') + self.store.all('playtests'), key=lambda item: item['created_at'], reverse=True)[:500]
