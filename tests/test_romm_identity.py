import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from portkeeper.romm_identity import ImportIdentities
from portkeeper.safety import Invalid


class ImportIdentityTests(unittest.TestCase):
    def test_only_registered_pc_archives_are_matched_and_retry_is_idempotent(self):
        records = [dict(id=1, platform_slug='linux', fs_name='Dusklight-v2-linux.zip', name='Dusklight v2', igdb_id=401002, summary='Launch checked, no gameplay.'), dict(id=2, platform_slug='ngc', fs_name='Dusklight-v2-linux.zip'), dict(id=3, platform_slug='win', fs_name='DusklightExtra-v2.zip')]
        class Connector:
            def _config(self): return {'url': 'http://localhost:8091', 'token': 'private'}
            def _json(self, path): return {'items': records, 'total': len(records)}
        requests = []
        class Opener:
            def open(self, request, timeout):
                from urllib.parse import parse_qs
                data = parse_qs(request.data.decode())
                requests.append(data)
                records[0].update(igdb_id=int(data['igdb_id'][0]), name=data['name'][0], url_cover=data['url_cover'][0])
                return io.BytesIO(json.dumps(records[0]).encode())
        with tempfile.TemporaryDirectory() as d:
            Path(d, 'romm-import-identities.json').write_text(json.dumps([dict(filename_prefix='Dusklight', game='Twilight Princess', igdb_id=1036, url_cover='https://example.org/cover.jpg')]))
            matcher = ImportIdentities(Connector(), d)
            with patch('portkeeper.romm_identity.urllib.request.build_opener', return_value=Opener()):
                self.assertEqual(matcher.sync()['matched'], 1)
                self.assertEqual(matcher.sync()['matched'], 0)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]['summary'], ['Launch checked, no gameplay.'])
            self.assertEqual(records[1].get('igdb_id'), None)
            self.assertEqual(records[2].get('igdb_id'), None)

    def test_disabled_without_registration_and_invalid_id_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            matcher = ImportIdentities(None, d)
            self.assertEqual(matcher.sync(), {'matched': 0})
            Path(d, 'romm-import-identities.json').write_text(json.dumps([dict(filename_prefix='Game', game='Game', igdb_id=True)]))
            class Connector:
                def _config(self): return {}
            matcher.romm = Connector()
            with self.assertRaises(Invalid): matcher.sync()

    def test_project_reference_follows_manual_match_without_overwriting_custom_cover(self):
        reference = dict(id=10, igdb_id=1030, name='Custom port', url_cover='https://example.org/port.jpg')
        record = dict(id=11, igdb_id=1030, name='Majora — 2Ship v5', fs_name='2Ship-v5-windows.zip', platform_slug='win', url_cover='https://example.org/old.jpg', summary='Windows untested')
        class Connector:
            def _config(self): return {'url': 'http://localhost:8091', 'token': 'private'}
            def _json(self, path):
                return reference if path.startswith('roms/') else {'items': [reference, record], 'total': 2}
        class Store:
            def all(self, table): return [{'id': 'project', 'metadata': dict(filename_prefix='2Ship', game='Majora', igdb_id=1, romm_rom_id=10, cover_policy='romm')}]
            def update(self, *args, **kwargs): pass
        requests = []
        class Opener:
            def open(self, request, timeout):
                from urllib.parse import parse_qs
                data = parse_qs(request.data.decode());requests.append(data)
                self_record = record
                self_record.update(igdb_id=int(data['igdb_id'][0]), name=data['name'][0], url_cover=data['url_cover'][0])
                return io.BytesIO(json.dumps(self_record).encode())
        with tempfile.TemporaryDirectory() as d:
            matcher = ImportIdentities(Connector(), d, Store())
            with patch('portkeeper.romm_identity.urllib.request.build_opener', return_value=Opener()):
                self.assertEqual(matcher.sync()['matched'], 1)
                self.assertEqual(requests[0]['igdb_id'], ['1030'])
                self.assertEqual(requests[0]['url_cover'], ['https://example.org/port.jpg'])
                self.assertEqual(matcher.sync()['matched'], 0)
                reference['igdb_id'] = 999
                self.assertEqual(matcher.sync()['matched'], 1)
                self.assertEqual(record['igdb_id'], 999)
            self.assertEqual(reference['name'], 'Custom port')

    def test_metadata_validation_keeps_igdb_and_artwork_separate(self):
        from portkeeper.romm_identity import validate_identity
        self.assertEqual(validate_identity(dict(filename_prefix='Port', game='Game', igdb_id=1030, cover_policy='preserve'))['igdb_id'], 1030)
        for value in [dict(filename_prefix='../Port', game='Game', igdb_id=1), dict(filename_prefix='Port', game='Game', igdb_id=1, cover_policy='romm'), dict(filename_prefix='Port', game='Game', igdb_id=True)]:
            with self.assertRaises(Invalid): validate_identity(value)

    def test_uploaded_custom_artwork_is_copied_once(self):
        from email.message import Message
        art = b'\x89PNG\r\n\x1a\ncustom-cover'
        reference = dict(id=10, igdb_id=1030, url_cover='', path_cover_large='/assets/custom.png')
        record = dict(id=11, igdb_id=1030, name='Majora — Port v1', fs_name='Port-v1.zip', platform_slug='win', summary='Untested')
        class Connector:
            def _config(self): return {'url': 'http://localhost:8091', 'token': 'private'}
            def _json(self, path): return reference if path.startswith('roms/') else {'items': [record], 'total': 1}
        class Store:
            def all(self, table): return [{'id': 'project', 'metadata': dict(filename_prefix='Port', game='Majora', igdb_id=1030, romm_rom_id=10, cover_policy='romm')}]
            def update(self, *args, **kwargs): pass
        writes=[]
        class Opener:
            def open(self, request, timeout):
                if request.get_method() == 'GET':
                    response=io.BytesIO(art);response.headers=Message();response.headers['Content-Type']='image/png';return response
                writes.append(request)
                return io.BytesIO(json.dumps(record).encode())
        with tempfile.TemporaryDirectory() as d:
            matcher=ImportIdentities(Connector(), d, Store())
            with patch('portkeeper.romm_identity.urllib.request.build_opener', return_value=Opener()):
                self.assertEqual(matcher.sync()['matched'], 1)
                self.assertEqual(matcher.sync()['matched'], 0)
            self.assertEqual(len(writes), 1)
            self.assertIn(art, writes[0].data)
            self.assertIn('multipart/form-data', writes[0].headers['Content-type'])

    def test_project_link_is_saved_and_can_be_cleared(self):
        from portkeeper.engine import Engine
        with tempfile.TemporaryDirectory() as d:
            engine = Engine(Path(d, 'data'), Path(d, 'inputs'), Path(d, 'library'))
            try:
                project=engine.create_project(dict(name='2Ship', repo='example/port', targets=['linux']))
                with patch.object(engine.romm, '_json', return_value={'igdb_id':1030, 'name':'Majora'}):
                    linked=engine.link_metadata(project['id'], dict(romm_rom_id=12963, filename_prefix='2Ship', game='Majora', cover_policy='romm'))
                self.assertEqual(linked['metadata']['igdb_id'], 1030)
                self.assertEqual(engine.store.get('projects', project['id'])['metadata']['romm_rom_id'], 12963)
                engine.update_project(project['id'], {'metadata':None})
                self.assertIsNone(engine.store.get('projects', project['id'])['metadata'])
                with patch.object(engine.romm, '_json', return_value={'igdb_id':None}):
                    with self.assertRaises(Invalid): engine.link_metadata(project['id'], dict(romm_rom_id=12963))
            finally: engine.close()
