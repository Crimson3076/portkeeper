import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from portkeeper.engine import Engine
from portkeeper.safety import Invalid
from test_packaging import definition


class PlatformWatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(Path(self.temp.name) / 'data')
        self.addCleanup(self.engine.close)
        self.project = self.engine.create_project({'name': 'Game', 'repo': 'example/game', 'targets': ['windows']})

    def release(self, version, *names):
        return {'version': version, 'assets': [{'name': name} for name in names]}

    def test_baseline_then_new_linux_release_notifies_once_without_enabling_it(self):
        with patch.object(self.engine, 'queue_draft') as draft:
            self.engine.watch_platform_releases(self.project, self.release('v1', 'game-windows.zip'), lambda _: None)
            self.assertEqual(self.engine.store.all('notifications'), [])
            self.engine.watch_platform_releases(self.project, self.release('v2', 'game-windows.zip', 'game-linux.zip'), lambda _: None)
            self.engine.watch_platform_releases(self.project, self.release('v3', 'game-windows.zip', 'game-linux.zip'), lambda _: None)
            draft.assert_not_called()
        notes = self.engine.store.all('notifications')
        self.assertEqual(len(notes), 1)
        self.assertIn('Linux', notes[0]['message'])
        self.assertEqual(self.engine.store.get('projects', self.project['id'])['targets'], ['windows'])
        self.assertEqual(self.engine.store.all('jobs'), [])

    def test_opt_in_drafts_new_native_target_without_changing_live_platforms(self):
        self.engine.update_project(self.project['id'], {'auto_draft_platforms': True})
        self.engine.watch_platform_releases(self.project, self.release('v1', 'game-windows.zip'), lambda _: None)
        with patch.object(self.engine.assistant, 'connected', return_value=True), patch.object(self.engine, 'queue_draft') as draft:
            self.engine.watch_platform_releases(self.project, self.release('v2', 'game-linux.zip', 'game-windows.zip'), lambda _: None)
            self.assertEqual(draft.call_args.kwargs['targets'], ['linux', 'windows'])
        self.assertEqual(self.engine.store.get('projects', self.project['id'])['targets'], ['windows'])

    def test_unsupported_mobile_platform_notifies_without_drafting(self):
        self.engine.update_project(self.project['id'], {'auto_draft_platforms': True})
        self.engine.watch_platform_releases(self.project, self.release('v1', 'game-windows.zip'), lambda _: None)
        with patch.object(self.engine.assistant, 'connected', return_value=True), patch.object(self.engine, 'queue_draft') as draft:
            self.engine.watch_platform_releases(self.project, self.release('v2', 'game.apk'), lambda _: None)
            draft.assert_not_called()
        self.assertIn('does not package', self.engine.store.all('notifications')[0]['message'])

    def test_pending_review_prevents_another_automatic_draft(self):
        self.engine.update_project(self.project['id'], {'auto_draft_platforms': True})
        self.engine.store.put('proposals', {'id':'pending', 'project_id':self.project['id'], 'status':'pending_review'})
        self.engine.watch_platform_releases(self.project, self.release('v1', 'game-windows.zip'), lambda _: None)
        with patch.object(self.engine.assistant, 'connected', return_value=True), patch.object(self.engine, 'queue_draft') as draft:
            self.engine.watch_platform_releases(self.project, self.release('v2', 'game-linux.zip'), lambda _: None)
            draft.assert_not_called()

    def prepare_expanded_draft(self):
        candidate = definition(upstream={'repository': 'example/game'})
        job = self.engine.job(self.project, 'capture', 'v2')
        self.engine.store.update('jobs', job['id'], kind='draft', requested_targets=['linux', 'windows'])
        context = {'project': self.project, 'release': {'version': 'v2'}, 'material': {}}
        def reply(system, user, read, log):
            return read(json.dumps(candidate))
        with patch.object(self.engine, '_draft_material', return_value=(None, context)), \
             patch('portkeeper.drafting.prompt_parts', return_value=('system', 'material')), \
             patch('portkeeper.drafting.unsupported_hashes', return_value=[]), \
             patch.object(self.engine.assistant, 'draft_definition', side_effect=reply):
            self.engine._draft(self.project['id'], job['id'], '')
        self.assertEqual(self.engine.store.get('jobs', job['id'])['status'], 'ready')
        return self.engine.store.all('proposals')[0]

    def test_new_platform_draft_enables_target_only_after_review(self):
        proposal = self.prepare_expanded_draft()
        self.assertEqual(proposal['status'], 'pending_review')
        self.assertEqual(proposal['base_targets'], ['windows'])
        self.assertEqual(proposal['requested_targets'], ['linux', 'windows'])
        self.assertEqual(self.engine.store.get('projects', self.project['id'])['targets'], ['windows'])
        updated = self.engine.apply_proposal(proposal['id'])
        self.assertEqual(updated['targets'], ['linux', 'windows'])
        self.assertFalse(any(job['kind'] == 'build' for job in self.engine.store.all('jobs')))

    def test_changed_selection_blocks_stale_new_platform_draft(self):
        self.engine.update_project(self.project['id'], {'packaging': definition(upstream={'repository': 'example/game'})})
        proposal = self.prepare_expanded_draft()
        self.engine.update_project(self.project['id'], {'targets': ['linux']})
        with self.assertRaisesRegex(Invalid, 'selected platforms changed'):
            self.engine.apply_proposal(proposal['id'])
