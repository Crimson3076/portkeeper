import hashlib
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from portkeeper.engine import Engine
from portkeeper.romm_identity import ImportIdentities
from portkeeper.romm_schedule import MODES, miss_due, note_search, retry_delay
from portkeeper.safety import Invalid


class BackoffTests(unittest.TestCase):
    def test_ladder_and_miss_cache(self):
        self.assertEqual([retry_delay(a) for a in (0, 1799, 1800, 3600, 7200, 10 ** 6)], [60, 60, 300, 900, 3600, 3600])
        misses = {}
        self.assertTrue(miss_due(misses, 'k', 1000))
        note_search(misses, 'k', False, 1000)
        self.assertFalse(miss_due(misses, 'k', 1030))
        self.assertTrue(miss_due(misses, 'k', 1060))
        note_search(misses, 'k', False, 1060)
        self.assertEqual(misses['k'], {'first': 1000, 'last': 1060})
        note_search(misses, 'k', True, 1100)
        self.assertEqual(misses, {})

    def test_identity_sync_skips_known_misses(self):
        romm = Mock()
        romm._config.return_value = {'url': 'http://x', 'token': 't'}
        romm._json.return_value = {'items': [], 'total': 0}
        with tempfile.TemporaryDirectory() as d:
            Path(d, 'romm-import-identities.json').write_text('[{"filename_prefix":"Game","game":"Game","igdb_id":5}]')
            matcher, misses = ImportIdentities(romm, d), {}
            matcher.sync(misses=misses, at=1000)
            matcher.sync(misses=misses, at=1030)
            self.assertEqual(romm._json.call_count, 1)
            matcher.sync(misses=misses, at=1061)
            self.assertEqual(romm._json.call_count, 2)
            matcher.sync(misses={}, at=1062)
            self.assertEqual(romm._json.call_count, 3)


class ScheduleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(Path(self.temp.name, 'data'), Path(self.temp.name, 'in'), Path(self.temp.name, 'lib'))
        self.addCleanup(self.engine.close)
        self.schedule = self.engine.match_schedule
        patcher = patch.object(self.engine.romm, 'public_config', return_value={'configured': True, 'url': 'http://x'})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_default_is_event_driven_and_idle_does_not_poll(self):
        self.assertEqual(self.schedule.mode(), 'event')
        self.assertIsNone(self.schedule.next_check()[0])
        self.assertFalse(self.schedule.due())
        self.assertEqual(self.schedule.status()['pending'], [])

    def test_event_mode_checks_every_minute_then_backs_off(self):
        t = 10_000.0
        self.schedule.kick('Uploaded', at=t)
        self.assertTrue(self.schedule.due(t))
        self.engine.save_setting('romm_match_state', {'event_at': t, 'last_run': t + 1, 'reason': 'Uploaded'})
        self.assertEqual(self.schedule.next_check(t + 2)[0], t + 1 + 60)
        self.engine.save_setting('romm_match_state', {'event_at': t, 'last_run': t + 2000, 'reason': 'Uploaded'})
        self.assertEqual(self.schedule.next_check(t + 2001)[0], t + 2000 + 300)
        self.engine.save_setting('romm_match_state', {'event_at': t, 'last_run': t + 8000, 'reason': 'Uploaded'})
        self.assertEqual(self.schedule.next_check(t + 8001)[0], t + 8000 + 3600)
        self.assertEqual(self.schedule.pending(t + 8001)[1], ['Uploaded'])
        self.assertIsNone(self.schedule.pending(t + 90_000)[0])  # given up after a day

    def test_awaiting_upload_is_pending(self):
        project = self.engine.create_project({'name': 'Game', 'repo': 'example/game', 'targets': ['linux']})
        self.engine.store.put('artifacts', {'id': 'a1', 'project_id': project['id'], 'filename': 'Game-1.zip',
                                            'romm': {'status': 'awaiting_scan', 'uploaded_at': datetime.now(timezone.utc).isoformat()}})
        self.assertIn('Game-1.zip', self.schedule.pending()[1][0])
        self.assertTrue(self.schedule.due())

    def test_fixed_interval_modes(self):
        for mode in ('1', '5', '15', '30', '60'):
            self.schedule.set_mode(mode)
            self.engine.save_setting('romm_match_state', {'last_run': 1000})
            self.assertEqual(self.schedule.next_check(1001)[0], 1000 + int(mode) * 60)
            self.assertTrue(self.schedule.due(1000 + int(mode) * 60))
            self.assertFalse(self.schedule.due(1000 + int(mode) * 60 - 1))

    def test_off_never_polls_and_setting_persists(self):
        self.schedule.kick('Uploaded')
        self.schedule.set_mode('off')
        self.assertFalse(self.schedule.due(time.time() + 10 ** 6))
        self.assertEqual(self.schedule.status()['mode'], 'off')
        self.assertEqual(self.engine.get_setting('romm_match_mode'), 'off')
        self.assertEqual(set(self.schedule.status()['modes']), set(MODES))
        with self.assertRaises(Invalid):
            self.schedule.set_mode('2')

    def test_run_records_misses_and_manual_run_ignores_them(self):
        searches = []
        self.engine.romm.library_stamp = lambda: 'stamp'
        self.engine.romm.search = lambda *a, **k: searches.append(a) or {'items': [], 'total': 0}
        self.engine.create_project({'name': 'Game', 'repo': 'example/game', 'targets': ['linux'], 'library_basename': 'game-abc',
                                    'metadata': {'filename_prefix': 'Game', 'game': 'Game', 'igdb_id': 5}})
        with patch.object(self.engine.romm, '_json', return_value={'items': [], 'total': 0}) as call, patch.object(self.engine.romm, '_config', return_value={'url': 'http://x', 'token': 't'}):
            self.schedule.run(at=1000)
            first = call.call_count + len(searches)
            self.assertGreater(first, 0)
            self.schedule.run(at=1010)
            self.assertEqual(call.call_count + len(searches), first)  # known misses are not searched again
            self.assertTrue(self.engine.get_setting('romm_match_misses'))
            self.schedule.run(manual=True, at=1020)
            self.assertEqual(call.call_count + len(searches), first * 2)
            self.engine.romm.library_stamp = lambda: 'changed'  # library change clears the cache
            self.schedule.run(at=1030)
            self.assertEqual(call.call_count + len(searches), first * 3)

    def test_manual_run_reports_failure_and_keeps_event(self):
        self.engine.romm.library_stamp = lambda: 'stamp'
        self.schedule.kick('Uploaded', at=1000)
        with patch('portkeeper.romm_identity.ImportIdentities.sync', side_effect=RuntimeError('boom')):
            with self.assertRaises(Invalid):
                self.schedule.run(manual=True, at=1010)
            self.schedule.run(at=1020)  # automatic runs record the error instead of raising
        self.assertIn('error', self.engine.get_setting('romm_import_matching'))
        state = self.engine.get_setting('romm_match_state')
        self.assertEqual((state['event_at'], state['last_run']), (1000, 1020))

    def test_kick_during_run_survives_and_failed_stamp_keeps_library(self):
        self.engine.romm.library_stamp = lambda: 'stamp'
        self.schedule.run(at=1000)
        kicked = lambda *a, **k: self.schedule.kick('Newer', at=1500) or {'matched': 0}
        self.engine.romm.library_stamp = Mock(side_effect=Invalid('down'))
        with patch('portkeeper.romm_identity.ImportIdentities.sync', side_effect=kicked):
            self.schedule.run(at=1010)
        state = self.engine.get_setting('romm_match_state')
        self.assertEqual((state['event_at'], state['library']), (1500, 'stamp'))

    def test_stale_awaiting_scan_is_given_up(self):
        project = self.engine.create_project({'name': 'Game', 'repo': 'example/game', 'targets': ['linux']})
        self.engine.store.put('artifacts', {'id': 'a1', 'project_id': project['id'], 'filename': 'Game-1.zip',
                                            'romm': {'status': 'awaiting_scan', 'uploaded_at': '2026-01-01T00:00:00+00:00'}})
        far = time.time() + 2 * 86400
        self.assertEqual(self.schedule.pending(far), (None, []))

    def test_set_mode_rejects_non_strings(self):
        for value in ([], {}, None, 5):
            with self.assertRaises(Invalid):
                self.schedule.set_mode(value)

    def test_publish_and_cover_failure_kick_the_schedule(self):
        self.assertIsNone(self.schedule.pending()[0])
        project = self.engine.create_project({'name': 'Game', 'repo': 'example/game', 'targets': ['linux', 'windows']})
        with patch.object(self.engine, 'match_imports_now', side_effect=RuntimeError('403')):
            self.engine.choose_cover(project['id'], {'url_cover': 'https://cdn.example/c.png', 'filename_prefix': 'Game'})
        self.assertTrue(self.schedule.due())
        self.assertIn('Cover change', self.schedule.status()['pending'][0])
        self.engine.save_setting('romm_match_state', {})
        self.assertFalse(self.schedule.due())
        package = Path(self.temp.name, 'Game-1.zip')
        package.write_bytes(b'zip')
        digest = hashlib.sha256(b'zip').hexdigest()
        self.engine.store.put('artifacts', {'id': 'a1', 'project_id': project['id'], 'filename': 'Game-1.zip', 'file': str(package),
                                            'sha256': digest,
                                            'size': 3, 'target': 'linux'})
        romm = self.engine.romm
        with patch('portkeeper.publishing.checksum', return_value=digest), \
                patch.object(romm, 'platform_for', return_value={'id': 1, 'fs_slug': 'linux', 'name': 'Linux'}), \
                patch.object(romm, 'find_file', return_value=None), patch.object(romm, 'upload'), \
                patch.object(self.engine.publisher, 'check'):
            self.engine.publisher.publish('a1', lambda message: None)
        self.assertTrue(self.schedule.due())
        self.assertEqual(self.engine.get_setting('romm_match_state')['reason'], 'Game-1.zip was published')

    def test_cover_with_no_immediate_match_kicks_schedule(self):
        project = self.engine.create_project({'name': 'Game', 'repo': 'example/game', 'targets': ['linux']})
        with patch.object(self.engine, 'match_imports_now', return_value={'matched': 0}):
            result = self.engine.choose_cover(project['id'], {'url_cover': 'https://cdn.example/c.png', 'filename_prefix': 'Game'})
        self.assertEqual(result['matched'], 0)
        self.assertTrue(self.schedule.due())
        self.assertIn('Cover change', self.schedule.status()['pending'][0])

    def test_unrelated_cover_updates_do_not_suppress_selected_game_retry(self):
        project = self.engine.create_project({'name': 'Game', 'repo': 'example/game', 'targets': ['linux']})
        self.engine.create_project({'name': 'Other', 'repo': 'example/other', 'targets': ['linux'],
                                    'metadata': {'filename_prefix': 'Other', 'game': 'Other', 'igdb_id': 5}})
        with patch.object(self.engine, 'match_imports_now', return_value={'matched': 1}):
            result = self.engine.choose_cover(project['id'], {'url_cover': 'https://cdn.example/c.png', 'filename_prefix': 'Game'})
        self.assertEqual(result['matched'], 1)
        self.assertTrue(self.schedule.due())
        self.engine.romm.library_stamp = lambda: 'stamp'
        self.engine.romm.search = lambda *a, **k: {'items': [], 'total': 0}
        with patch('portkeeper.romm_identity.ImportIdentities.sync', return_value={'matched': 1}):
            self.schedule.run()
        self.assertIsNotNone(self.schedule.next_check()[0])
        self.assertIn('import:game', self.engine.get_setting('romm_match_misses'))

    def test_poll_confirms_indexing_before_identity_sync_can_settle_event(self):
        self.engine.romm.library_stamp = lambda: 'stamp'
        self.schedule.kick('Uploaded', at=1000)
        self.engine.store.put('artifacts', {'id': 'a1', 'filename': 'Game.zip', 'romm': {'status': 'awaiting_scan'}})
        def poll():
            self.engine.store.update('artifacts', 'a1', romm={'status': 'in_romm'})
        def sync(**kwargs):
            self.assertEqual(self.engine.store.get('artifacts', 'a1')['romm']['status'], 'in_romm')
            return {'matched': 1}
        with patch.object(self.engine.publisher, 'poll', side_effect=poll), patch('portkeeper.romm_identity.ImportIdentities.sync', side_effect=sync) as matching:
            self.assertEqual(self.schedule.run(at=1010), {'matched': 1})
        matching.assert_called_once()
        self.assertNotIn('event_at', self.engine.get_setting('romm_match_state'))

    def test_local_publish_kicks_schedule(self):
        project = self.engine.create_project({'name': 'Game', 'repo': 'example/game', 'targets': ['linux']})
        package = Path(self.temp.name, 'Game.zip')
        package.write_bytes(b'zip')
        self.engine.store.put('artifacts', {'id': 'a1', 'project_id': project['id'], 'filename': package.name,
                                           'file': str(package), 'sha256': hashlib.sha256(b'zip').hexdigest(),
                                           'target': 'linux', 'published': False})
        result = self.engine.publish('a1')
        self.assertTrue(result['published'])
        self.assertTrue(self.schedule.due())

    def test_check_romm_now_requires_connection_and_reports_failure(self):
        with patch.object(self.engine.romm, 'public_config', return_value={'configured': False}):
            with self.assertRaises(Invalid):
                self.engine.check_romm_now()
        self.engine.romm.library_stamp = lambda: 'stamp'
        with patch('portkeeper.romm_identity.ImportIdentities.sync', side_effect=RuntimeError('boom')):
            with self.assertRaises(Invalid):
                self.engine.check_romm_now()
        with patch('portkeeper.romm_identity.ImportIdentities.sync', return_value={'matched': 3}):
            result = self.engine.check_romm_now()
        self.assertEqual(result['matched'], 3)
        self.assertEqual(result['matching']['mode'], 'event')

    def test_library_stamp_is_stable_and_tolerates_mixed_values(self):
        entries = [{'id': None, 'rom_count': 'x'}, {'id': 2, 'rom_count': 5, 'updated_at': 't'}, {'id': 1, 'rom_count': None}, 'junk']
        with patch.object(self.engine.romm, '_json', return_value=entries):
            first = self.engine.romm.library_stamp()
        with patch.object(self.engine.romm, '_json', return_value=list(reversed(entries))):
            self.assertEqual(self.engine.romm.library_stamp(), first)
        with patch.object(self.engine.romm, '_json', return_value=[{'id': 2, 'rom_count': 6}]):
            self.assertNotEqual(self.engine.romm.library_stamp(), first)
        with patch.object(self.engine.romm, '_json', return_value={'items': []}):
            with self.assertRaises(Invalid):
                self.engine.romm.library_stamp()

    def test_reviews_imports_with_misses_skips_searches_and_leaves_ui_cache(self):
        project = self.engine.create_project({'name': 'Game', 'repo': 'example/game', 'targets': ['linux'],
                                              'metadata': {'filename_prefix': 'Game', 'game': 'Game', 'igdb_id': 5}})
        searches = []
        self.engine.romm.search = lambda *a, **k: searches.append(a) or {'items': [], 'total': 0}
        misses = {}
        self.engine.reviews.imports(project['id'], misses=misses, at=1000)
        self.assertEqual(len(searches), 1)
        self.assertIn('import:game', misses)
        self.engine.reviews.imports(project['id'], misses=misses, at=1010)
        self.assertEqual(len(searches), 1)  # known miss skipped
        self.assertEqual(self.engine.reviews.cache, {})  # incomplete pass is not served to the UI
        self.engine.reviews.imports(project['id'])
        self.assertEqual(len(searches), 2)
        self.assertTrue(self.engine.reviews.cache)

    def test_library_stamp_transport_error_does_not_escape_run(self):
        self.engine.romm.library_stamp = Mock(side_effect=TimeoutError('read timed out'))
        with patch.object(self.engine.romm, '_json', return_value={'items': [], 'total': 0}), \
                patch.object(self.engine.romm, '_config', return_value={'url': 'http://x', 'token': 't'}):
            self.schedule.run(at=1000)
        self.assertEqual(self.engine.get_setting('romm_match_state')['last_run'], 1000)

    def test_new_event_is_not_stuck_behind_old_backoff(self):
        t = 1_000_000.0
        self.engine.create_project({'name': 'Game', 'repo': 'example/game', 'targets': ['linux']})
        old = datetime.fromtimestamp(t - 10_000, timezone.utc).isoformat()
        self.engine.store.put('artifacts', {'id': 'old', 'project_id': 'p', 'filename': 'Old.zip',
                                            'romm': {'status': 'awaiting_scan', 'uploaded_at': old}})
        self.engine.save_setting('romm_match_state', {'last_run': t - 5})
        self.assertEqual(self.schedule.next_check(t)[0], t - 5 + 3600)
        self.schedule.kick('New upload', at=t)
        self.assertTrue(self.schedule.due(t + 1))
        self.engine.save_setting('romm_match_state', {'event_at': t, 'last_run': t + 1})
        self.assertEqual(self.schedule.next_check(t + 2)[0], t + 1 + 60)


if __name__ == '__main__':
    unittest.main()
