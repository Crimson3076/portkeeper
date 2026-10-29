import tempfile
import time
import unittest
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
                                            'romm': {'status': 'awaiting_scan', 'uploaded_at': '2026-01-01T00:00:00+00:00'}})
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

    def test_publish_and_cover_failure_kick_the_schedule(self):
        self.assertIsNone(self.schedule.pending()[0])
        self.schedule.kick('x')
        self.assertTrue(self.schedule.due())
        self.assertEqual(self.schedule.status()['pending'], ['x'])


if __name__ == '__main__':
    unittest.main()
