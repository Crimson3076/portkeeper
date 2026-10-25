import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from portkeeper.backups import create_backup, keep_count, managed_manifest
from portkeeper.engine import Engine
from portkeeper.safety import Invalid
from portkeeper.storage import Store
from portkeeper.storage_layout import BULK_NAMES, default_storage, migrate


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'state'
        self.inputs = self.root / 'owned'
        self.bulk = self.root / 'bulk'

    def test_explicit_bulk_keeps_all_payload_roots_outside_state(self):
        engine = Engine(self.data, self.inputs, storage_dir=self.bulk)
        self.addCleanup(engine.close)
        self.assertEqual(engine.archive_root, self.bulk / 'archive')
        self.assertEqual(engine.work_root, self.bulk / 'work')
        self.assertEqual(engine.packager.prepared, self.bulk / 'prepared')
        self.assertEqual(engine.packager.cache, self.bulk / 'cache/dependencies')
        self.assertTrue(engine.shipwright.root.is_relative_to(self.bulk / 'cache'))
        self.assertEqual(engine.romm.cache, self.bulk / 'cache/romm')
        self.assertEqual(engine.replay_root, self.bulk / 'automation-recipes')
        self.assertEqual(engine.library_root, self.bulk / 'library')
        agent = engine.assistant.agents
        env = agent._env('codex')
        self.assertTrue(Path(env['CODEX_HOME']).is_relative_to(self.data / 'secrets'))
        self.assertTrue(Path(env['TMPDIR']).is_relative_to(self.bulk))
        self.assertTrue(Path(env['XDG_CACHE_HOME']).is_relative_to(self.bulk))
        self.assertEqual(agent.workspaces, self.bulk / 'agent-workspaces')
        self.assertFalse(any((self.data / name).exists() for name in BULK_NAMES))
        status = engine.state()['settings']
        self.assertTrue(status['storage_separated'])
        self.assertEqual(status['backup_keep'], 3)

    def test_inputs_cannot_become_writable_payload_storage(self):
        self.inputs.mkdir()
        (self.inputs / 'owned.rom').write_bytes(b'owned data')
        with self.assertRaises(Invalid):
            Engine(self.data, self.inputs, storage_dir=self.inputs)
        self.assertEqual(list(p.name for p in self.inputs.iterdir()), ['owned.rom'])
        self.assertEqual((self.inputs / 'owned.rom').read_bytes(), b'owned data')
        with self.assertRaises(Invalid):
            Engine(self.data, self.inputs, storage_dir=self.data / 'bulk')

    def test_cli_default_separates_new_installs_and_preserves_legacy(self):
        self.assertEqual(default_storage(self.data), self.root / 'state-storage')
        engine = Engine(self.data, self.inputs)
        engine.close()
        self.assertEqual(default_storage(self.data), self.data)
        with self.assertRaisesRegex(Invalid, 'Bulk storage changed'):
            Engine(self.data, self.inputs, storage_dir=self.bulk)

    def prepare_migration(self):
        engine = Engine(self.data, self.inputs)
        (self.inputs / 'original.rom').write_bytes(b'original')
        archive = engine.archive_root / 'capture'
        archive.mkdir()
        (archive / 'source.zip').write_bytes(b'upstream source')
        engine.store.put('captures', {'id': 'a' * 32, 'project_id': 'b' * 32, 'root': str(archive)})
        drafts = self.data / 'drafts'
        drafts.mkdir()
        (drafts / 'paths.json').write_text(json.dumps({'source': str(archive / 'source.zip')}))
        (self.data / 'automation-recipes').mkdir()
        (self.data / 'automation-recipes/runtime.zip').write_bytes(b'exact replay payload')
        engine.close()
        self.bulk.mkdir()
        for name in BULK_NAMES:
            if (self.data / name).exists():
                shutil.copytree(self.data / name, self.bulk / name)
        return archive

    def test_new_layout_persists_across_restarts_without_flags_and_rejects_missing_mount(self):
        selected = default_storage(self.data)
        engine = Engine(self.data, self.inputs, storage_dir=selected)
        engine.close()
        self.assertEqual(default_storage(self.data), selected)
        restarted = Engine(self.data, self.inputs, storage_dir=default_storage(self.data))
        restarted.close()
        (selected / '.portkeeper-storage-id').unlink()
        with self.assertRaisesRegex(Invalid, 'mount is missing'):
            Engine(self.data, self.inputs, storage_dir=default_storage(self.data))
        self.assertFalse((selected / '.portkeeper-storage-id').exists(), 'A missing mount must not silently be initialized again')

    def test_migration_plans_then_verifies_rewrites_and_starts_without_deletion(self):
        archive = self.prepare_migration()
        store = Store(self.data / 'portkeeper.sqlite3')
        plan = migrate(self.data, self.data, self.bulk)
        self.assertFalse(plan['applied'])
        self.assertEqual(plan['deletions'], [])
        self.assertEqual(store.get('captures', 'a' * 32)['root'], str(archive))
        result = migrate(self.data, self.data, self.bulk, apply=True)
        self.assertTrue(result['applied'])
        self.assertTrue((Path(result['backup']) / 'portkeeper.sqlite3').is_file())
        self.assertEqual(store.get('captures', 'a' * 32)['root'], str(self.bulk / 'archive/capture'))
        self.assertEqual(json.loads((self.data / 'drafts/paths.json').read_text())['source'], str(self.bulk / 'archive/capture/source.zip'))
        self.assertTrue((archive / 'source.zip').is_file(), 'Old copies are retained for operator confirmation')
        engine = Engine(self.data, self.inputs, storage_dir=self.bulk)
        engine.close()
        self.assertEqual((self.inputs / 'original.rom').read_bytes(), b'original')

    def test_bad_copy_cannot_rewrite_database(self):
        archive = self.prepare_migration()
        (self.bulk / 'archive/capture/source.zip').write_bytes(b'different bytes')
        with self.assertRaisesRegex(Invalid, 'verification failed'):
            migrate(self.data, self.data, self.bulk, apply=True)
        store = Store(self.data / 'portkeeper.sqlite3')
        self.assertEqual(store.get('captures', 'a' * 32)['root'], str(archive))
        self.assertFalse((self.data / 'state-backups').exists())

    def test_json_failure_rolls_back_paths_and_configs(self):
        archive = self.prepare_migration()
        second = self.data / 'drafts/second.json'
        second.write_text(json.dumps({'source': str(archive / 'source.zip')}))
        from portkeeper.storage_layout import atomic_write
        calls = []
        def fail_second(path, value):
            calls.append(path)
            if len(calls) == 2:
                raise OSError('disk write failed')
            return atomic_write(path, value)
        with patch('portkeeper.storage_layout.atomic_write', side_effect=fail_second):
            with self.assertRaises(OSError): migrate(self.data, self.data, self.bulk, apply=True)
        store = Store(self.data / 'portkeeper.sqlite3')
        self.assertEqual(store.get('captures', 'a' * 32)['root'], str(archive))
        self.assertEqual(json.loads((self.data / 'drafts/paths.json').read_text())['source'], str(archive / 'source.zip'))
        self.assertEqual(json.loads(second.read_text())['source'], str(archive / 'source.zip'))
        self.assertEqual(len(calls), 3, 'The successfully written first config is restored after the second fails')

    def test_absolute_links_cannot_silently_keep_using_old_storage(self):
        archive = self.prepare_migration()
        (archive / 'alias').symlink_to(archive / 'source.zip')
        (self.bulk / 'archive/capture/alias').symlink_to(archive / 'source.zip')
        with self.assertRaisesRegex(Invalid, 'Absolute symlink needs migration review'):
            migrate(self.data, self.data, self.bulk, apply=True)
        self.assertEqual(Store(self.data / 'portkeeper.sqlite3').get('captures', 'a' * 32)['root'], str(archive))

    def test_migration_refuses_input_paths_and_paused_jobs(self):
        self.prepare_migration()
        with self.assertRaisesRegex(Invalid, 'read-only input'):
            migrate(self.data, self.data, self.inputs, input_root=self.inputs)
        store = Store(self.data / 'portkeeper.sqlite3')
        store.put('automation', {'id': 'paused', 'status': 'waiting_user'})
        with self.assertRaisesRegex(Invalid, 'cancel automatic setup'):
            migrate(self.data, self.data, self.bulk)

    def test_wrong_namespace_cannot_replace_runtime_paths_with_host_paths(self):
        archive = self.prepare_migration()
        with self.assertRaisesRegex(Invalid, 'path namespace'):
            migrate(self.data, self.root / 'different-alias', self.bulk, apply=True)
        self.assertEqual(Store(self.data / 'portkeeper.sqlite3').get('captures', 'a' * 32)['root'], str(archive))


class StateBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = Path(self.temp.name)
        self.store = Store(self.data / 'portkeeper.sqlite3')
        self.store.put('projects', {'id': 'example', 'recipe': {'script': 'build'}})

    def test_only_allowlisted_metadata_is_copied_even_for_small_game_files(self):
        for name in ('archive/game.zip', 'prepared/small.bin', 'cache/tool.zip', 'work/game.exe',
                     'automation-recipes/runtime.zip', 'secrets/agents/codex/.cache/package.json'):
            path = self.data / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'game payload sentinel')
        for name in ('secrets/assistant.json', 'secrets/agents/codex/.codex/auth.json', 'drafts/draft.json', 'recipes/build.py'):
            path = self.data / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('{}')
        root = create_backup(self.store, self.data)
        files = {p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file()}
        self.assertEqual(files, {'portkeeper.sqlite3', 'manifest.json', 'secrets/assistant.json',
                                'secrets/agents/codex/.codex/auth.json', 'drafts/draft.json', 'recipes/build.py'})
        self.assertEqual(Store(root / 'portkeeper.sqlite3').get('projects', 'example')['recipe']['script'], 'build')
        self.assertIsNotNone(managed_manifest(root))

    def test_keep_last_three_never_prunes_legacy_or_edited_backups(self):
        legacy = self.data / 'maintenance-backups/old'
        legacy.mkdir(parents=True)
        (legacy / 'unique.zip').write_bytes(b'keep unique data')
        first = create_backup(self.store, self.data)
        (first / 'unexpected.zip').write_bytes(b'keep manual addition')
        snapshots = [create_backup(self.store, self.data) for _ in range(5)]
        self.assertTrue(first.is_dir(), 'Unrecognized/edited snapshots require explicit review')
        self.assertEqual([p for p in snapshots if p.exists()], snapshots[-3:])
        self.assertEqual((legacy / 'unique.zip').read_bytes(), b'keep unique data')

    def test_symlinks_cannot_pull_game_files_into_backups(self):
        outside = self.data / 'game-files'
        outside.mkdir()
        (outside / 'game.json').write_text('private game data')
        (self.data / 'catalog').symlink_to(outside, target_is_directory=True)
        with self.assertRaises(Invalid): create_backup(self.store, self.data)
        self.assertEqual((outside / 'game.json').read_text(), 'private game data')
        self.assertFalse(list((self.data / 'state-backups').glob('state-*')))

    def test_keep_count_is_configurable_but_bounded(self):
        with patch.dict(os.environ, {'PORTKEEPER_BACKUP_KEEP': '1'}):
            one = create_backup(self.store, self.data)
            two = create_backup(self.store, self.data)
            self.assertFalse(one.exists())
            self.assertTrue(two.exists())
        for value in (0, 21, 'bad'):
            with self.assertRaises(Invalid): keep_count(value)


if __name__ == '__main__':
    unittest.main()
