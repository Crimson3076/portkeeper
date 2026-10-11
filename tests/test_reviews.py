import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from portkeeper.reviews import Reviews, signature
from portkeeper.romm import _normalize_record
from portkeeper.safety import Invalid
from portkeeper.storage import Store


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = Store(Path(self.temp.name) / 'state.sqlite3')
        self.store.put('projects', {'id': 'game', 'name': 'Example', 'metadata': {'filename_prefix': 'Example', 'igdb_id': 1}})
        self.raw = {'id': 10, 'name': 'Example Linux', 'platform_slug': 'linux', 'files': [{'id': 1, 'file_name': 'Example-v1-linux.zip', 'file_size_bytes': 100, 'sha1_hash': 'abc'}]}
        self.romm = Mock()
        self.romm.public_config.return_value = {'url': 'https://romm.example'}
        self.romm._json.side_effect = lambda path: self.raw
        self.romm.search.side_effect = lambda *a, **k: {'items': [_normalize_record(self.raw)], 'total': 1}
        self.service = Reviews(SimpleNamespace(store=self.store, romm=self.romm, lock=threading.RLock(), lifecycle=Mock()))

    def payload(self, **changes):
        return {'romm_rom_id': 10, 'signature': signature(_normalize_record(self.raw)), 'result': 'starts', 'device': 'Linux laptop', 'confirmed': True, **changes}

    def test_discovery_is_deduplicated_and_explains_timestamp(self):
        self.service.imports('game')
        self.service.imports('game', force=True)
        events = self.store.all('imports')
        self.assertEqual(len(events), 1)
        self.assertIn('not its original upload time', events[0]['message'])

    def test_confirmation_device_and_failure_reason_are_required(self):
        for changes in ({'confirmed': False}, {'device': ''}, {'result': 'failed', 'notes': ''}, {'result': []}, {'signature': ''}):
            with self.subTest(changes=changes), self.assertRaises(Invalid):
                self.service.record('game', self.payload(**changes))
        self.service.record('game', self.payload(result='untested', device='', confirmed=False))

    def test_history_and_exact_download_version(self):
        first = self.service.record('game', self.payload(result='failed', notes='Black screen'))
        second = self.service.record('game', self.payload(result='played', notes='Played for an hour'))
        self.assertEqual(self.service.imports('game')['items'][0]['review']['id'], second['id'])
        self.assertEqual(len(self.store.all('playtests')), 2)
        old_signature = first['package']['signature']
        self.raw['files'][0]['sha1_hash'] = 'new'
        self.assertIsNone(self.service.imports('game', force=True)['items'][0]['review'])
        with self.assertRaisesRegex(Invalid, 'download changed'):
            self.service.record('game', self.payload(signature=old_signature))

    def test_platforms_do_not_share_results(self):
        self.service.record('game', self.payload(result='played'))
        self.raw['id'] = 11
        self.raw['platform_slug'] = 'win'
        self.raw['files'][0]['file_name'] = 'Example-v1-win.zip'
        self.assertIsNone(self.service.imports('game', force=True)['items'][0]['review'])

    def test_unrelated_and_console_downloads_are_rejected(self):
        for changes in ({'platform_slug': 'ps4'}, {'files': [{'file_name': 'Another-v1-linux.zip'}]}):
            original = self.raw.copy()
            self.raw.update(changes)
            with self.assertRaisesRegex(Invalid, 'another game'):
                self.service.record('game', self.payload())
            self.raw = original

    def test_artifact_review_updates_validation_and_keeps_evidence(self):
        self.store.put('artifacts', {'id': 'archive', 'project_id': 'game', 'sha256': 'hash', 'target': 'linux', 'version': '1'})
        event = self.service.record('game', {'artifact_id': 'archive', 'signature': 'hash', 'result': 'failed', 'device': 'Laptop', 'notes': 'Crashes at launch', 'confirmed': True})
        self.assertEqual(event['result'], 'failed')
        self.service.engine.lifecycle.validate.assert_called_once_with('archive', 'failed', 'Laptop: Crashes at launch', {'manual_check': True, 'device': 'Laptop'})
