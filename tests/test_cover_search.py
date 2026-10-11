import io
import json
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from portkeeper.romm import RomM
from portkeeper.romm_identity import ImportIdentities, validate_identity
from portkeeper.safety import Invalid


class CoverSearchTests(unittest.TestCase):
    def test_search_keeps_static_https_covers_and_remembers_offers(self):
        response = [{'name': 'Twilight Princess', 'provider': 'sgdb', 'resources': [
            {'thumb': 'https://cdn.example/t1.png', 'url': 'https://cdn.example/c1.png', 'type': 'static', 'width': 600, 'height': 900, 'author': 'artist'},
            {'thumb': 'https://cdn.example/a.webm', 'url': 'https://cdn.example/a.webm', 'type': 'animated'},
            {'thumb': 'http://cdn.example/t2.png', 'url': 'http://cdn.example/c2.png', 'type': 'static'}]},
            {'name': 'Old RomM shape', 'resources': [{'thumb': 'https://cdn.example/t3.jpg', 'url': 'https://cdn.example/c3.jpg'}]}]
        with tempfile.TemporaryDirectory() as d:
            romm = RomM(d)
            with patch.object(romm, '_json', return_value=response) as call:
                result = romm.search_covers(' Dusklight ')
            self.assertEqual(call.call_args.args[0], 'search/cover?search_term=Dusklight')
            self.assertEqual([c['url'] for c in result['covers']], ['https://cdn.example/c1.png', 'https://cdn.example/c3.jpg'])
            self.assertEqual(result['covers'][0]['author'], 'artist')
            self.assertTrue(romm.offered('https://cdn.example/t1.png'))
            self.assertFalse(romm.offered('https://cdn.example/a.webm'))
            with self.assertRaises(Invalid): romm.search_covers('')

    def test_missing_provider_is_explained(self):
        with tempfile.TemporaryDirectory() as d:
            romm = RomM(d)
            with patch.object(romm, '_json', side_effect=Invalid('RomM request failed with HTTP 500')):
                with self.assertRaisesRegex(Invalid, 'SteamGridDB'): romm.search_covers('Game')

    def test_preview_rejects_non_images_and_plain_http(self):
        with tempfile.TemporaryDirectory() as d:
            romm = RomM(d)
            with self.assertRaises(Invalid): romm.artwork_preview('http://cdn.example/c.png')
            response = io.BytesIO(b'<svg/>'); response.headers = Message(); response.headers['Content-Type'] = 'image/svg+xml'
            with patch('portkeeper.romm.urllib.request.build_opener') as factory:
                factory.return_value.open.return_value = response
                with self.assertRaises(Invalid): romm.artwork_preview('https://cdn.example/c.svg')

    def test_custom_cover_validation(self):
        identity = validate_identity(dict(filename_prefix='Port', game='Game', cover_policy='custom', url_cover='https://cdn.example/c.png'))
        self.assertNotIn('igdb_id', identity)
        self.assertNotIn('url_cover', validate_identity(dict(filename_prefix='Port', game='Game', igdb_id=1, url_cover='https://cdn.example/c.png')))
        for value in [dict(filename_prefix='Port', game='Game', cover_policy='custom'), dict(filename_prefix='Port', game='Game', cover_policy='custom', url_cover='http://cdn.example/c.png'), dict(filename_prefix='Port', game='Game', url_cover='https://x/c.png')]:
            with self.assertRaises(Invalid): validate_identity(value)

    def test_chosen_cover_reaches_windows_linux_and_the_reference_once(self):
        cover = 'https://cdn.example/chosen.png'
        records = {
            10: dict(id=10, igdb_id=1036, name='My edited name', fs_name='Dusklight-abc-linux.zip', platform_slug='linux', url_cover='https://old/1.png', summary='mine'),
            11: dict(id=11, igdb_id=None, name='Dusklight-abc-windows', fs_name='Dusklight-abc-windows.zip', platform_slug='win', url_cover=''),
            12: dict(id=12, igdb_id=None, name='Disc', fs_name='Dusklight-abc-linux.zip', platform_slug='ngc')}
        class Connector:
            def _config(self): return {'url': 'http://localhost:8091', 'token': 'private'}
            def _json(self, path): return records[10] if path.startswith('roms/') else {'items': list(records.values()), 'total': 3}
        class Store:
            def all(self, table): return [{'id': 'p', 'metadata': dict(filename_prefix='Dusklight-abc', game='Twilight Princess', romm_rom_id=10, cover_policy='custom', url_cover=cover)}]
            def update(self, *args, **kwargs): pass
        writes = []
        class Opener:
            def open(self, request, timeout):
                from urllib.parse import parse_qs
                record_id = int(request.full_url.rsplit('/', 1)[1])
                data = {k: v[0] for k, v in parse_qs(request.data.decode()).items()}
                writes.append((record_id, data))
                # RomM normalizes the stored URL, so only the receipt prevents repeats.
                records[record_id].update(name=data['name'], igdb_id=int(data['igdb_id']) if data.get('igdb_id') else records[record_id]['igdb_id'])
                return io.BytesIO(json.dumps(records[record_id]).encode())
        with tempfile.TemporaryDirectory() as d:
            matcher = ImportIdentities(Connector(), d, Store())
            with patch('portkeeper.romm_identity.urllib.request.build_opener', return_value=Opener()):
                self.assertEqual(matcher.sync()['matched'], 2)
                self.assertEqual(matcher.sync()['matched'], 0)
        by_id = dict(writes)
        self.assertEqual(set(by_id), {10, 11})
        self.assertEqual(by_id[10], {'name': 'My edited name', 'summary': 'mine', 'igdb_id': '1036', 'url_cover': cover})
        self.assertEqual(by_id[11]['igdb_id'], '1036')
        self.assertEqual(by_id[11]['name'], 'Twilight Princess — Dusklight-abc-windows')
        self.assertEqual(by_id[11]['url_cover'], cover)
        self.assertIsNone(records[12]['igdb_id'])

    def test_choose_cover_saves_without_metadata_and_applies_now(self):
        from portkeeper.engine import Engine
        with tempfile.TemporaryDirectory() as d:
            engine = Engine(Path(d, 'data'), Path(d, 'inputs'), Path(d, 'library'))
            try:
                project = engine.create_project(dict(name='Dusklight', repo='example/port', targets=['linux', 'windows']))
                with self.assertRaises(Invalid): engine.choose_cover(project['id'], {'url_cover': 'https://cdn.example/c.png'})
                with patch.object(engine, 'match_imports_now', return_value={'matched': 2}):
                    result = engine.choose_cover(project['id'], {'url_cover': 'https://cdn.example/c.png', 'filename_prefix': 'Dusklight'})
                self.assertEqual(result['matched'], 2)
                self.assertEqual(result['project']['metadata'], {'game': 'Dusklight', 'filename_prefix': 'Dusklight', 'cover_policy': 'custom', 'url_cover': 'https://cdn.example/c.png'})
                with patch.object(engine, 'match_imports_now', side_effect=Invalid('403')):
                    result = engine.choose_cover(project['id'], {'url_cover': 'https://cdn.example/d.png'})
                self.assertIn('retries', result['warning'])
                self.assertEqual(engine.store.get('projects', project['id'])['metadata']['url_cover'], 'https://cdn.example/d.png')
            finally: engine.close()
