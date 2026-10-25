import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from portkeeper.maintenance import manage
from portkeeper.engine import Engine, Busy
from portkeeper.storage import Store
from portkeeper.safety import Invalid


class MaintenanceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        store = Store(self.root / 'state.sqlite3')
        self.engine = SimpleNamespace(data=self.root, store=store, lock=threading.RLock())
        def get_setting(key, default):
            try: return store.get('settings', key)['value']
            except KeyError: return default
        self.engine.get_setting = get_setting
        self.engine.save_setting = lambda key, value: store.put('settings', {'id': key, 'value': value})
        self.project = {'id': 'a'*32, 'name': 'Test', 'repo': 'example/game', 'targets': ['linux'], 'packaging': {'revision': 1}, 'auto_build': True, 'auto_publish': True}
        store.put('projects', self.project)
        store.put('jobs', {'id': 'b'*32, 'project_id': self.project['id'], 'status': 'ready', 'finished_at': '2026', 'logs': ['private log']})
        self.archive = self.root / 'game.zip'; self.archive.write_bytes(b'owned assets')
        store.put('artifacts', {'id': 'c'*32, 'project_id': self.project['id'], 'file': str(self.archive), 'validation_level': 'launch_checked'})

    def test_clear_activity_retains_package_validation_and_backups(self):
        drafts = self.root / 'drafts'; drafts.mkdir(); (drafts / ('b'*32+'.json')).write_text('{"private":"prompt"}')
        result = manage(self.engine, {'action': 'clear_activity'})
        self.assertEqual(result['jobs_cleared'], 1)
        self.assertTrue((Path(result['backup']) / 'portkeeper.sqlite3').is_file())
        self.assertTrue((Path(result['backup']) / 'drafts' / ('b'*32+'.json')).is_file())
        self.assertEqual(self.engine.store.get('jobs', 'b'*32)['logs'], [])
        self.assertEqual(self.engine.store.get('artifacts', 'c'*32)['validation_level'], 'launch_checked')
        self.assertEqual(self.archive.read_bytes(), b'owned assets')

    def test_remove_game_preserves_physical_download_and_snapshot(self):
        result = manage(self.engine, {'action': 'remove_game', 'project_id': self.project['id']})
        self.assertEqual(self.engine.store.all('projects'), [])
        self.assertEqual(self.engine.store.all('artifacts'), [])
        backup = Store(Path(result['backup']) / 'portkeeper.sqlite3')
        self.assertEqual(backup.get('projects', self.project['id'])['name'], 'Test')
        self.assertTrue(self.archive.is_file())

    def test_active_work_blocks_removal_but_survives_global_cleanup(self):
        self.engine.store.update('jobs', 'b'*32, status='running', finished_at=None)
        with self.assertRaises(Invalid): manage(self.engine, {'action': 'remove_game', 'project_id': self.project['id']})
        result = manage(self.engine, {'action': 'clear_activity'})
        self.assertEqual(result['jobs_cleared'], 0)
        self.assertEqual(self.engine.store.get('jobs', 'b'*32)['logs'], ['private log'])

    def test_claimed_work_blocks_removal_before_a_job_exists(self):
        self.engine.busy = {self.project['id']}
        with self.assertRaises(Invalid):
            manage(self.engine, {'action': 'remove_game', 'project_id': self.project['id']})
        self.assertTrue(self.engine.store.get('projects', self.project['id']))
        self.assertFalse((self.root / 'maintenance-backups').exists())

    def test_claim_rejects_deleted_or_changed_instructions(self):
        self.engine.busy = set()
        self.engine.store.update('projects', self.project['id'], packaging={'revision': 2})
        with self.assertRaises(Busy):
            Engine.claim(self.engine, self.project['id'], expected_project=self.project)
        self.engine.store.delete('projects', self.project['id'])
        with self.assertRaises(KeyError):
            Engine.claim(self.engine, self.project['id'])
        self.assertFalse(self.engine.busy)

    def test_trial_logs_clear_without_changing_results_or_running_work(self):
        self.engine.store.put('trials', {'id': 'd'*32, 'project_id': self.project['id'], 'status': 'passed', 'logs': ['diagnostic'], 'result': {'status': 'passed'}})
        self.engine.store.put('trials', {'id': 'e'*32, 'project_id': self.project['id'], 'status': 'running', 'logs': ['active']})
        result = manage(self.engine, {'action': 'clear_activity'})
        self.assertEqual(result['trials_cleared'], 1)
        self.assertEqual(self.engine.store.get('trials', 'd'*32)['result'], {'status': 'passed'})
        self.assertEqual(self.engine.store.get('trials', 'd'*32)['logs'], [])
        self.assertEqual(self.engine.store.get('trials', 'e'*32)['logs'], ['active'])

    def test_reset_instructions_preserves_prior_definition_and_download(self):
        manage(self.engine, {'action': 'reset_instructions', 'project_id': self.project['id']})
        p = self.engine.store.get('projects', self.project['id'])
        self.assertEqual(p['packaging']['runner'], 'capture')
        self.assertFalse(p['auto_build'])
        self.assertEqual(p['definition_history'][0]['definition'], {'revision': 1})
        self.assertTrue(self.archive.is_file())
