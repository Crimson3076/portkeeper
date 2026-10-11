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
