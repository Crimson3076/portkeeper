import copy
import io
import json
import os
import shutil
import tarfile
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from portkeeper.autonomous import Automation, SYSTEM, action_reply, portable_permissions, validate_manifest
from portkeeper.engine import Engine
from portkeeper.safety import Invalid, checksum
from portkeeper.storage import identifier
from portkeeper.worker_broker import WorkerBroker, fetch_public, tree_identity, unpack_snapshot


def reply(action, payload):
    return json.dumps({'action': action, 'payload': payload})


def manifest(target='linux', included=True):
    name = 'game.exe' if target == 'windows' else 'game'
    return {'runtime_kind': 'native_game', 'summary': 'Fixture runtime with owned data',
            'packages': {target: {'path': 'out/' + target, 'runtime': name, 'command': name,
                                 'expected_files': [name, 'data.bin'], 'instructions': 'Extract the complete package and run ' + name}},
            'game_files': {'required': True, 'included': included, 'instructions': 'Owned data is required and was included' if included else 'Copy your owned data beside the runtime'},
            'saves': 'Unknown; no gameplay test performed', 'limitations': ['Startup/gameplay untested'],
            'evidence': [{'path': 'source/README.md', 'quote': 'This engine needs owned game data.'}]}


class FixtureBroker:
    """Controlled worker replies. NEVER executes model code on the test host."""
    def __init__(self, root, inputs):
        self.root, self.inputs, self.calls = root, inputs, []
        self.image = 'sha256:' + 'a' * 64
        self.nondeterministic = False

    def request(self, op, **payload):
        self.calls.append((op, copy.deepcopy(payload)))
        if op == 'health':
            return {'ready': True, 'protocol': 1, 'image': self.image, 'backend': 'fixture', 'test_only': False}
        if op == 'cancel':
            return {'cancelled': True}
        if op != 'run':
            raise Invalid('Unsupported fixture operation')
        root = self.root / 'jobs' / payload['job_id']
        destination = root / ('snapshot-' + payload['operation_id'])
        shutil.copytree(root / payload['seed'], destination, symlinks=True)
        output = destination / 'out' / payload['target']
        output.mkdir(parents=True, exist_ok=True)
        if payload['script'] == 'fail':
            (root / (payload['operation_id'] + '.log')).write_text('compiler error')
            code = 1
        else:
            code = 0
            (output / 'game').write_bytes(b'#!/bin/sh\nexit 0\n')
            (output / 'game').chmod(0o755)
            if payload['phase'] == 'private':
                data = (self.inputs / payload['inputs'][0]['path']).read_bytes()
                (output / 'data.bin').write_bytes(data)
            else:
                (output / 'data.bin').write_bytes(b'public placeholder')
            if self.nondeterministic:
                (output / 'unstable.txt').write_text(identifier())
            (root / (payload['operation_id'] + '.log')).write_text('private-data-sentinel' if payload['phase'] == 'private' else 'compiled')
        return {'exit_code': code, 'phase': payload['phase'], 'snapshot': destination.name,
                'identity': {'sha256': tree_identity(destination)['sha256']},
                'log': payload['operation_id'] + '.log', 'seconds': .01}


class AutonomousTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.engine = Engine(root / 'data', root / 'inputs', root / 'library')
        self.broker = FixtureBroker(root / 'exchange', self.engine.input_root)
        self.engine.automation = Automation(self.engine, self.broker)
        self.engine.automation.configure({'enabled': True, 'eligible_providers': ['openai']})
        self.project = self.engine.create_project({'name': 'Unknown port', 'repo': 'example/engine', 'targets': ['linux']})
        source = self.engine.archive_root / 'fixture'
        source.mkdir()
        with zipfile.ZipFile(source / 'source.zip', 'w') as bundle:
            bundle.writestr('upstream/README.md', 'This engine needs owned game data. Build a native engine from source.')
        self.capture = self.engine.store.put('captures', {'id': identifier(), 'project_id': self.project['id'],
            'version': 'v1', 'commit': 'a' * 40, 'root': str(source), 'source': {'file': 'source.zip', 'sha256': checksum(source / 'source.zip')},
            'assets': [], 'complete': True})
        self.project = self.engine.store.update('projects', self.project['id'], capture_id=self.capture['id'])
        (self.engine.input_root / 'private.bin').write_bytes(b'private-data-sentinel')
        self.connected = patch.object(self.engine.assistant, 'connected', return_value=True)
        self.config = patch.object(self.engine.assistant, '_config', return_value={'provider': 'openai'})
        self.material = patch.object(self.engine, '_draft_material', return_value=(self.capture, {'material': {}, 'release': {}}))
        self.connected.start(); self.config.start(); self.material.start()

    def tearDown(self):
        self.engine.close()
        self.connected.stop(); self.config.stop(); self.material.stop()
        self.temp.cleanup()

    def run_setup(self, responses, **options):
        inputs = [{'role': 'base_game', 'kind': 'file', 'path': 'private.bin'}]
        with patch.object(self.engine.assistant, 'chat', side_effect=responses) as chat:
            result = self.engine.automation.queue(self.project['id'], {'inputs': inputs, **options})
            # Drain the executor without replacing it; all submitted setup work completes.
            self.engine.executor.submit(lambda: None).result(timeout=10)
            # Two pool slots means the drain can finish first; wait on the job state.
            for _ in range(200):
                job = self.engine.store.get('jobs', result['job']['id'])
                if job['status'] not in ('queued', 'running') and self.project['id'] not in self.engine.busy:
                    return job, chat
                threading.Event().wait(.01)
            self.fail('Automation fixture did not finish')

    def strict_responses(self):
        return [reply('run', {'script': 'build', 'phase': 'public'}),
                reply('add_private_step', {'script': 'stage', 'target': 'linux'}), reply('finish', manifest())]

    def test_strict_cloud_setup_and_model_free_replay_keep_owned_data_local(self):
        job, chat = self.run_setup(self.strict_responses())
        self.assertEqual(job['status'], 'ready', job.get('error'))
        self.assertEqual(chat.call_count, 3)
        sent = json.dumps(chat.call_args_list, default=str)
        self.assertNotIn('private-data-sentinel', sent)
        self.assertNotIn('private.bin', sent)
        artifact = self.engine.store.get('artifacts', job['artifact_id'])
        with zipfile.ZipFile(artifact['file']) as bundle:
            self.assertEqual(bundle.read('data.bin'), b'private-data-sentinel')
        self.assertEqual(artifact['manifest']['verification']['native_startup'], 'untested')
        self.assertFalse(list((self.broker.root / 'jobs' / job['id']).glob('snapshot-*')))
        replay, chat = self.run_setup([], replay=True)
        self.assertEqual(replay['status'], 'ready', replay.get('error'))
        chat.assert_not_called()

    def test_malformed_reply_repair_is_counted_and_bound(self):
        job, chat = self.run_setup(['{}', '{}', '{}'])
        self.assertEqual(job['status'], 'failed')
        self.assertIn('three repetitions', job['error'])
        self.assertEqual(chat.call_count, 3)
        self.assertEqual(self.engine.automation.record(job['id'])['model_calls'], 3)

    def test_nondeterministic_output_cannot_be_published_or_saved_as_good(self):
        self.broker.nondeterministic = True
        job, _ = self.run_setup(self.strict_responses())
        self.assertEqual(job['status'], 'failed')
        self.assertIn('different package files', job['error'])
        self.assertEqual(self.engine.store.all('artifacts'), [])
        self.assertNotIn('autonomous_recipe', self.engine.store.get('projects', self.project['id']))

    def test_private_cloud_read_and_return_to_public_are_rejected(self):
        responses = [reply('run', {'script': 'stage', 'phase': 'private'}), reply('read', {'path': 'out/linux/data.bin'}),
                     reply('fetch', {'url': 'https://example.org/tool', 'name': 'tool'}), reply('blocked', {'reason': 'done'})]
        self.engine.automation.configure({'privacy': 'metadata'})
        job, chat = self.run_setup(responses)
        self.assertEqual(job['status'], 'failed')
        sent = json.dumps(chat.call_args_list, default=str)
        self.assertNotIn('private-data-sentinel', sent)
        self.assertFalse(any(op == 'fetch' for op, _ in self.broker.calls))
        self.assertEqual(sum(op == 'run' for op, _ in self.broker.calls), 1)

    def test_input_search_selects_only_unique_match_without_sending_path(self):
        job, chat = self.run_setup([reply('find_input', {'role': 'base_game', 'kind': 'file', 'name_glob': '*.bin'}),
                                   reply('blocked', {'reason': 'fixture stop'})], inputs=[])
        self.assertEqual(job['status'], 'failed')
        record = self.engine.automation.record(job['id'])
        self.assertEqual(record['inputs'][0]['path'], 'private.bin')
        self.assertNotIn('private.bin', json.dumps(chat.call_args_list, default=str))

    def test_daily_budget_persists_and_no_hidden_request_retry(self):
        self.engine.automation.configure({'daily_calls': 1})
        job, chat = self.run_setup([reply('list', {'path': ''})])
        self.assertEqual(job['status'], 'failed')
        self.assertIn('rolling daily', job['error'])
        self.assertEqual(chat.call_count, 1)
        self.engine.store.delete('automation', job['id'])
        second, second_chat = self.run_setup([])
        self.assertEqual(second['status'], 'failed')
        second_chat.assert_not_called()

    def test_public_compatible_endpoint_cannot_be_attested_as_private_local(self):
        self.engine.automation.configure({'privacy': 'metadata', 'private_model_local': True})
        with patch.object(self.engine.assistant, '_config', return_value={'provider': 'openai', 'base_url': 'https://api.openai.com/v1'}):
            job, chat = self.run_setup([reply('run', {'script': 'stage', 'phase': 'private'}),
                                       reply('read', {'path': 'out/linux/data.bin'}), reply('blocked', {'reason': 'done'})])
        self.assertEqual(job['status'], 'failed')
        self.assertNotIn('private-data-sentinel', str(chat.call_args_list))

    def test_automatic_setup_does_not_accept_runtime_only_when_preparation_requested(self):
        responses = [reply('run', {'script': 'build'}), reply('finish', manifest(included=False)), reply('blocked', {'reason': 'Need a preparation method'})]
        job, _ = self.run_setup(responses)
        self.assertEqual(job['status'], 'failed')
        self.assertFalse(self.engine.store.all('artifacts'))

    def test_input_browser_stays_under_library_and_skips_links(self):
        (self.engine.input_root / 'escape').symlink_to('/etc')
        choices = self.engine.automation.input_choices()
        self.assertEqual([i['name'] for i in choices['items']], ['private.bin'])
        with self.assertRaises(Invalid): self.engine.automation.input_choices('../data')
        with self.assertRaises(Invalid): self.engine.automation.input_choices('escape')

    def test_controller_cancellation_persists_before_broker_dispatch(self):
        job = identifier()
        self.engine.store.put('automation', {'id': job, 'status': 'running'})
        result = self.engine.automation.cancel(job)
        self.assertTrue(result['cancel_requested'])
        self.assertTrue(self.engine.automation.record(job)['cancel_requested'])
        self.assertEqual(self.broker.calls[-1], ('cancel', {'job_id': job}))

    def test_replay_refuses_changed_environment_or_seed(self):
        job, _ = self.run_setup(self.strict_responses())
        self.assertEqual(job['status'], 'ready', job.get('error'))
        recipe = self.engine.automation.export_recipe(self.project['id'])
        preserved = self.engine.data / 'automation-recipes' / recipe['seed_sha256'] / 'source' / 'README.md'
        preserved.write_text('modified')
        replay, chat = self.run_setup([], replay=True)
        self.assertEqual(replay['status'], 'failed')
        self.assertIn('missing or changed', replay['error'])
        chat.assert_not_called()

    def test_native_binary_and_game_data_claims_are_validated(self):
        value = manifest()
        value['runtime_kind'] = 'build_helper'
        with self.assertRaises(Invalid): validate_manifest(value, ['linux'])
        value = manifest(); value['packages']['linux']['expected_files'] = ['*.exe']
        with self.assertRaises(Invalid): validate_manifest(value, ['linux'])
        self.assertEqual(action_reply(json.dumps({'action': 'list', 'payload_json': '{"path":""}'})), ('list', {'path': ''}))

    def test_recipe_edit_creates_revision_and_requires_new_checks(self):
        job, _ = self.run_setup(self.strict_responses())
        self.assertEqual(job['status'], 'ready', job.get('error'))
        recipe = self.engine.automation.export_recipe(self.project['id'])
        result = self.engine.automation.edit_recipe(self.project['id'], {'steps': recipe['steps'], 'manifest': recipe['manifest']})
        self.assertEqual(result['recipe']['revision'], 2)
        self.assertNotIn('output_identity', result['recipe'])
        self.assertEqual(len(self.engine.store.get('projects', self.project['id'])['autonomous_history']), 1)

    def test_automation_and_new_target_selection_preserve_active_script_recipe(self):
        job, _ = self.run_setup(self.strict_responses())
        self.assertEqual(job['status'], 'ready', job.get('error'))
        project = self.engine.update_project(self.project['id'], {'auto_build': True})
        self.assertTrue(project['auto_build'])
        self.assertIn('autonomous_recipe', project)
        project = self.engine.update_project(self.project['id'], {'targets': ['linux', 'windows']})
        self.assertEqual(project['targets'], ['linux', 'windows'])
        self.assertIn('autonomous_recipe', project, 'Selecting a future platform must not silently discard the working script')

    def test_strict_private_failure_never_reenters_cloud_loop(self):
        responses = [reply('add_private_step', {'script': 'fail'}), reply('finish', manifest())]
        job, chat = self.run_setup(responses)
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(chat.call_count, 2)
        self.assertEqual(self.engine.store.all('artifacts'), [])


class BrokerBoundaryTests(unittest.TestCase):
    def test_rootful_daemon_refused_by_production_backend(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.object(WorkerBroker, 'docker', return_value='["name=seccomp,profile=builtin"]'):
                broker = WorkerBroker(Path(root) / 'exchange', Path(root) / 'owned', 'sha256:' + 'a' * 64)
            self.assertFalse(broker.health()['ready'])
            self.assertIn('rootless', broker.health()['error'])

    def test_snapshot_rejects_links_to_host_and_link_then_child_writes(self):
        with tempfile.TemporaryDirectory() as root:
            for target, child in (('/etc/passwd', False), ('../outside', False), ('safe', True)):
                archive = Path(root) / 'bad.tar'
                with tarfile.open(archive, 'w') as bundle:
                    link = tarfile.TarInfo('escape'); link.type = tarfile.SYMTYPE; link.linkname = target; bundle.addfile(link)
                    if child:
                        file = tarfile.TarInfo('escape/child'); file.size = 1; bundle.addfile(file, io.BytesIO(b'x'))
                with self.assertRaises((Invalid, FileExistsError)):
                    unpack_snapshot(archive, Path(root) / 'out')
                self.assertFalse((Path(root) / 'out').exists())

    def test_snapshot_internal_links_roundtrip_but_identity_detects_changes(self):
        with tempfile.TemporaryDirectory() as root:
            archive = Path(root) / 'good.tar'
            with tarfile.open(archive, 'w') as bundle:
                item = tarfile.TarInfo('data'); item.size = 3; bundle.addfile(item, io.BytesIO(b'abc'))
                link = tarfile.TarInfo('alias'); link.type = tarfile.SYMTYPE; link.linkname = 'data'; bundle.addfile(link)
            first = unpack_snapshot(archive, Path(root) / 'out')
            self.assertEqual((Path(root) / 'out/alias').read_bytes(), b'abc')
            (Path(root) / 'out/data').write_bytes(b'def')
            self.assertNotEqual(first['sha256'], tree_identity(Path(root) / 'out')['sha256'])

    def test_fetch_rejects_non_public_dns_and_credentials_before_connecting(self):
        with tempfile.TemporaryDirectory() as root:
            for address in ('127.0.0.1', '192.168.0.83', '169.254.169.254', '::1'):
                with patch('portkeeper.worker_broker.socket.getaddrinfo', return_value=[(2, 1, 6, '', (address, 443))]):
                    with self.assertRaises(Invalid): fetch_public('https://example.org/file', Path(root) / 'file')
            with self.assertRaises(Invalid): fetch_public('https://user:password@example.org/file', Path(root) / 'file')


if __name__ == '__main__':
    unittest.main()
