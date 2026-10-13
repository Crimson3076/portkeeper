"""Opt-in real containers; synthetic owned data, controlled model replies.

Rootful CI verifies worker execution/flags, NOT rootless deployment readiness.
The production application refuses this test broker; only this test patches that
readiness gate to exercise the full controller through its real Unix socket.
"""
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from portkeeper.autonomous import Automation
from portkeeper.engine import Engine
from portkeeper.safety import Invalid, checksum
from portkeeper.storage import identifier
from portkeeper.worker_broker import BrokerClient, WorkerBroker, make_server


@unittest.skipUnless(os.environ.get('PORTKEEPER_TEST_WORKER_IMAGE'), 'Set PORTKEEPER_TEST_WORKER_IMAGE to an installed immutable worker image ID')
class RealWorkers(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='portkeeper-real-workers-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = Engine(self.root / 'data', self.root / 'owned', self.root / 'library')
        self.addCleanup(self.engine.close)
        self.broker = WorkerBroker(self.root / 'exchange', self.engine.input_root,
                                   os.environ['PORTKEEPER_TEST_WORKER_IMAGE'], allow_rootful_test=os.environ.get('PORTKEEPER_TEST_ROOTLESS') != '1')
        self.assertTrue(self.broker.ready, self.broker.error)
        self.socket = self.root / 'broker.sock'
        self.server = make_server(self.broker, self.socket)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': .01}, daemon=True)
        self.thread.start()
        self.client = BrokerClient(self.socket, self.broker.root)
        self.engine.automation = Automation(self.engine, self.client)

    def tearDown(self):
        self.engine.close()
        self.server.shutdown(); self.server.server_close(); self.thread.join(timeout=5)
        self.temp.cleanup()

    def test_source_compile_private_preparation_and_clean_replay(self):
        if self.broker.test_mode:
            self.assertFalse(self.engine.automation.status()['worker']['ready'], 'Production must refuse a rootful test broker')
        else:
            self.assertTrue(self.engine.automation.status()['worker']['ready'])
        self.assertEqual(self.socket.stat().st_mode & 0o777, 0o600)
        health = {**self.broker.health(), 'test_only': False}
        self.engine.automation.configure({'enabled': True, 'eligible_providers': ['openai']})
        project = self.engine.create_project({'name': 'Generic C fixture', 'repo': 'example/unknown-engine', 'targets': ['linux']})
        capture_root = self.engine.archive_root / 'fixture'; capture_root.mkdir()
        with zipfile.ZipFile(capture_root / 'source.zip', 'w') as source:
            source.writestr('repo/README.md', 'This engine needs owned game data. Compile game.c, then put data.bin next to game.')
            source.writestr('repo/game.c', '#include <stdio.h>\nint main(int argc,char **argv){FILE *f=fopen(argc>1?argv[1]:"data.bin","rb");if(!f)return 2;fclose(f);puts("startup fixture passed");return 0;}\n')
        capture = self.engine.store.put('captures', {'id': identifier(), 'project_id': project['id'], 'root': str(capture_root),
            'version': 'v1', 'commit': 'a' * 40, 'source': {'file': 'source.zip', 'sha256': checksum(capture_root / 'source.zip')}, 'assets': []})
        project = self.engine.store.update('projects', project['id'], capture_id=capture['id'])
        owned = self.engine.input_root / 'owned.bin'; owned.write_bytes(b'synthetic-owned-data'); owned.chmod(0o644)
        build = '''mkdir -p out/linux
gcc -Wl,--build-id=none -ffile-prefix-map=/work=. source/game.c -o out/linux/game
python3 -I -c "import os,socket; assert 'PORTKEEPER_WORKER_CANARY' not in os.environ; assert not os.path.exists('/var/run/docker.sock'); assert os.statvfs('/work').f_blocks*os.statvfs('/work').f_frsize == 2147483648"
'''
        stage = '''cp /inputs/input0 out/linux/data.bin
python3 -I -c "import errno; p='/inputs/input0'; exec('try:\\n open(p,\\\"wb\\\").write(b\\\"wrong\\\")\\nexcept OSError as e:\\n assert e.errno in (errno.EROFS,errno.EACCES)\\nelse:\\n raise RuntimeError(\\\"Owned input was writable\\\")')"
cd out/linux
./game data.bin
'''
        manifest = {'runtime_kind': 'native_game', 'summary': 'Synthetic generic C engine',
                    'packages': {'linux': {'path': 'out/linux', 'runtime': 'game', 'command': 'game',
                        'expected_files': ['game', 'data.bin'], 'instructions': 'Extract and run game'}},
                    'game_files': {'required': True, 'included': True, 'instructions': 'Owned fixture data was staged read-only'},
                    'saves': 'Not applicable to the fixture', 'limitations': ['This is a test fixture, not a commercial game'],
                    'evidence': [{'path': 'source/README.md', 'quote': 'This engine needs owned game data.'}]}
        responses = [json.dumps({'action': 'run', 'payload': {'script': build, 'timeout': 90}}),
                     json.dumps({'action': 'add_private_step', 'payload': {'script': stage, 'timeout': 90}}),
                     json.dumps({'action': 'finish', 'payload': manifest})]
        with patch.object(self.engine.automation, 'status', return_value={'worker': health}), \
             patch.object(self.engine.assistant, 'connected', return_value=True), \
             patch.object(self.engine.assistant, '_config', return_value={'provider': 'openai'}), \
             patch.object(self.engine.assistant, 'chat', side_effect=responses) as model, \
             patch.object(self.engine, '_draft_material', return_value=(capture, {'material': {}, 'release': {}})), \
             patch.dict(os.environ, {'PORTKEEPER_WORKER_CANARY': 'controller-only'}):
            result = self.engine.automation.queue(project['id'], {'inputs': [{'role': 'game', 'kind': 'file', 'path': 'owned.bin'}]})
            deadline = time.monotonic() + 240
            while project['id'] in self.engine.busy and time.monotonic() < deadline:
                threading.Event().wait(.1)
            job = self.engine.store.get('jobs', result['job']['id'])
            self.assertEqual(job['status'], 'ready', job.get('error'))
            self.assertEqual(model.call_count, 3)
            self.assertNotIn('synthetic-owned-data', str(model.call_args_list))
            artifact = self.engine.store.get('artifacts', job['artifact_id'])
            with zipfile.ZipFile(artifact['file']) as bundle:
                self.assertEqual(bundle.read('data.bin'), b'synthetic-owned-data')
                self.assertEqual(bundle.read('game')[:4], b'\x7fELF')
            self.assertEqual(owned.read_bytes(), b'synthetic-owned-data')
            record = self.engine.automation.record(job['id'])
            self.assertEqual(len(record['worker_runs']), 4)
            self.assertEqual(artifact['manifest']['verification']['clean_replay'], 'passed')
            self.assertEqual(artifact['manifest']['verification']['native_startup'], 'untested')
            self.assertFalse(list((self.broker.root / 'jobs' / job['id']).glob('snapshot-*')))
            self.assertFalse(self.broker.active_names)

    def test_boundary_failure_idempotency_and_cancellation(self):
        job = identifier(); seed = self.broker.job_root(job) / 'seed'; seed.mkdir(); seed.chmod(0o755)
        (seed / 'known').write_text('original'); (seed / 'known').chmod(0o644)
        operation = identifier()
        result = self.client.request('run', job_id=job, operation_id=operation, seed='seed', phase='public', timeout=90,
                                     script='test ! -w /seed/known; mkdir out; printf ok > out/result')
        self.assertEqual(result['exit_code'], 0)
        self.assertEqual((seed / 'known').read_text(), 'original')
        again = self.client.request('run', job_id=job, operation_id=operation, seed='seed', phase='public', timeout=90,
                                    script='test ! -w /seed/known; mkdir out; printf ok > out/result')
        self.assertEqual(result, again, 'Lost acknowledgements cannot re-execute an operation')
        with self.assertRaises(Invalid):
            self.client.request('run', job_id=job, operation_id=operation, seed='seed', phase='public', script='different')
        with self.assertRaises(Invalid):
            self.client.request('run', job_id=job, operation_id=identifier(), seed='seed', phase='public', script='true', privileged=True)
        with self.assertRaises(Invalid):
            self.client.request('run', job_id=job, operation_id=identifier(), seed='seed', phase='public', script='true', inputs=[{'path':'../escape','kind':'file'}])
        waiting = identifier(); outcome = []
        def run():
            try:
                self.client.request('run', job_id=job, operation_id=waiting, seed='seed', phase='public', timeout=90, script='sleep 60')
            except Invalid as error:
                outcome.append(str(error))
        thread = threading.Thread(target=run); thread.start()
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            names = subprocess.check_output(['docker', 'ps', '--format', '{{.Names}}'], text=True)
            if 'portkeeper-worker-' + waiting in names:
                break
            threading.Event().wait(.1)
        self.client.request('cancel', job_id=job)
        thread.join(timeout=45)
        self.assertFalse(thread.is_alive(), 'Cancellation must end a real running container')
        self.assertTrue(outcome)
        self.assertFalse(self.broker.active_names)
        self.assertNotIn('portkeeper-worker-' + waiting, subprocess.check_output(['docker', 'ps', '-a', '--format', '{{.Names}}'], text=True))

    @unittest.skipUnless(os.environ.get('PORTKEEPER_TEST_OPENLARA_ZIP'), 'Optional checksummed public Windows runtime fixture')
    def test_prebuilt_windows_archive_and_owned_folder_layout_replay(self):
        archive = Path(os.environ['PORTKEEPER_TEST_OPENLARA_ZIP'])
        self.assertEqual(checksum(archive), 'd24cbf90ee4ef94414c64808d3aa13198079faf72d9ea3cae393675284929ad1')
        job = identifier(); seed = self.broker.job_root(job) / 'seed'; seed.mkdir(); seed.chmod(0o755)
        shutil.copyfile(archive, seed / 'runtime.zip'); (seed / 'runtime.zip').chmod(0o644)
        owned = self.engine.input_root / 'owned.phd'; owned.write_bytes(b'synthetic level fixture'); owned.chmod(0o644)
        script = '''python3 -I -c "import zipfile; zipfile.ZipFile('runtime.zip').extractall('out/windows')"
mkdir -p out/windows/DATA
cp /inputs/input0 out/windows/DATA/LEVEL1.PHD
'''
        results = []
        manifest = {'packages': {'windows': {'path': 'out/windows', 'runtime': 'OpenLara.exe', 'command': 'OpenLara.exe',
                                            'expected_files': ['OpenLara.exe', 'readme.txt', 'DATA/LEVEL1.PHD']}}}
        for _ in range(2):
            result = self.client.request('run', job_id=job, operation_id=identifier(), seed='seed', phase='private', target='windows',
                                         inputs=[{'path': 'owned.phd', 'kind': 'file'}], script=script, timeout=90)
            self.assertEqual(result['exit_code'], 0)
            root = self.broker.job_root(job) / result['snapshot']
            results.append(self.engine.automation.validate_outputs(root, manifest))
            self.assertEqual((root / 'out/windows/DATA/LEVEL1.PHD').read_bytes(), b'synthetic level fixture')
        self.assertEqual(results[0], results[1])
        self.assertEqual(owned.read_bytes(), b'synthetic level fixture')


if __name__ == '__main__':
    unittest.main()
