import os
import io
import tarfile
import copy
import http.client
import json
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
import unittest.mock
from pathlib import Path
from unittest.mock import patch

from portkeeper import broker_net
from portkeeper.autonomous import Automation
from portkeeper.broker_net import PROTOCOL, RemoteBrokerClient, make_net_server, pair
from portkeeper.engine import Engine
from portkeeper.safety import Invalid
from portkeeper.worker_broker import ID, tree_identity


class FakeBroker:
    """Stands in for WorkerBroker; never executes anything."""
    ready = True

    def __init__(self, root):
        self.root = Path(root)
        self.calls = []

    def job_root(self, job):
        path = self.root / 'jobs' / job
        path.mkdir(parents=True, exist_ok=True)
        return path

    def handle(self, payload):
        self.calls.append(copy.deepcopy(payload))
        if payload['op'] == 'health':
            return {'ready': True, 'backend': 'rootless-docker', 'image': 'sha256:' + 'a' * 64, 'protocol': 1, 'test_only': False}
        if payload['op'] == 'run':
            root = self.job_root(payload['job_id'])
            snapshot = root / ('snapshot-' + payload['operation_id'])
            shutil.copytree(root / payload['seed'], snapshot, symlinks=True)
            (snapshot / 'built.txt').write_text('built')
            (root / (payload['operation_id'] + '.log')).write_text('compiled')
            return {'exit_code': 0, 'phase': payload['phase'], 'snapshot': snapshot.name,
                    'identity': {'sha256': tree_identity(snapshot)['sha256']}, 'log': payload['operation_id'] + '.log', 'seconds': .1}
        raise Invalid('Unknown broker operation')


class LinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.broker = FakeBroker(self.root / 'broker' / 'exchange')
        self.server = make_net_server(self.broker, '127.0.0.1', 0, self.root / 'broker' / 'link')
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f'https://127.0.0.1:{self.port}'
        self.code = self.server.link.new_code()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.temp.cleanup()

    def paired(self):
        return RemoteBrokerClient(pair(self.url, self.code, self.server.fingerprint), self.root / 'app-exchange')

    def test_pairing_then_health_reports_version_and_rootless_backend(self):
        client = self.paired()
        health = client.request('health')
        self.assertTrue(health['ready'])
        self.assertEqual(health['backend'], 'rootless-docker')
        self.assertEqual(health['protocol'], PROTOCOL)
        self.assertTrue(health['version'])
        self.assertEqual(client.link['fingerprint'], self.server.fingerprint)

    def test_wrong_code_is_rejected_and_code_is_single_use(self):
        with self.assertRaisesRegex(Invalid, 'did not match'):
            pair(self.url, '0000-0000-0000', self.server.fingerprint)
        self.paired()
        with self.assertRaisesRegex(Invalid, 'No valid pairing code'):
            pair(self.url, self.code, self.server.fingerprint)

    def test_bad_proofs_do_not_burn_pairing_code(self):
        for _ in range(10):
            with self.assertRaises(Invalid):
                pair(self.url, '0000-0000-0000', self.server.fingerprint)
        self.paired()

    def test_unconfirmed_fingerprint_sends_no_pairing_proof(self):
        with patch.object(broker_net.PinnedTLS, 'request') as request:
            with self.assertRaisesRegex(Invalid, 'fingerprint'):
                pair(self.url, self.code, '0' * 64)
            request.assert_not_called()
        self.paired()

    def test_proof_for_another_fingerprint_is_refused(self):
        nonce = 'a' * 32
        with self.assertRaisesRegex(Invalid, 'did not match'):
            self.server.link.pair(broker_net.proof(self.code, 'client', '0' * 64, nonce), nonce)
        self.paired()

    def test_remote_fetch_stages_dependency_and_rejects_checksum_mismatch(self):
        client = self.paired()
        job = 'a' * 32
        def fetch(payload):
            self.assertEqual(payload['op'], 'fetch')
            target = self.broker.job_root(job) / 'downloads' / 'dep.tar'
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(b'dependency')
            return {'name': target.name, 'sha256': broker_net.checksum(target)}
        with patch.object(self.broker, 'handle', side_effect=fetch):
            client.request('fetch', job_id=job)
        target = client.root / 'jobs' / job / 'downloads' / 'dep.tar'
        self.assertEqual(target.read_bytes(), b'dependency')
        with patch.object(self.broker, 'handle', return_value={'name': 'dep.tar', 'sha256': '0' * 64}):
            with self.assertRaisesRegex(Invalid, 'checksum'):
                client.request('fetch', job_id=job)
        self.assertFalse(target.exists())

    def test_dependency_with_log_extension_uses_dependency_size_limit(self):
        client = self.paired()
        job = 'a' * 32
        data = b'x' * 101
        def fetch(payload):
            target = self.broker.job_root(job) / 'downloads' / 'build.log'
            target.parent.mkdir(exist_ok=True)
            target.write_bytes(data)
            return {'name': target.name, 'sha256': broker_net.checksum(target)}
        with patch.object(self.broker, 'handle', side_effect=fetch), patch.object(broker_net, 'MAX_LOG', 100):
            client.request('fetch', job_id=job)
        target = client.root / 'jobs' / job / 'downloads' / 'build.log'
        self.assertEqual(target.read_bytes(), data)

    def test_interrupted_upload_removes_temporary_archive(self):
        handler = object.__new__(broker_net.NetHandler)
        handler.server = self.server
        handler.route = lambda: '/v1/jobs/' + 'b' * 32 + '/trees/seed'
        handler.guard = lambda: True
        handler.body = lambda limit: 100
        handler.rfile = io.BytesIO(b'partial')
        with self.assertRaisesRegex(Invalid, 'ended early'):
            handler.put()
        self.assertEqual(list(self.broker.job_root('b' * 32).glob('.upload-*')), [])

    def test_ipv6_pairing_preserves_brackets(self):
        with patch.object(broker_net, 'PinnedTLS') as tls:
            connection = tls.return_value
            connection.seen = self.server.fingerprint
            def response():
                nonce = json.loads(connection.request.call_args.args[2])['nonce']
                result = unittest.mock.Mock(status=200)
                result.read.return_value = json.dumps({'token': 'private', 'proof': broker_net.proof(self.code, 'broker', self.server.fingerprint, nonce)}).encode()
                return result
            connection.getresponse.side_effect = response
            link = pair('https://[::1]:8788', self.code, self.server.fingerprint)
        self.assertEqual(link['url'], 'https://[::1]:8788')
        self.assertEqual(RemoteBrokerClient(link, self.root).host, '::1')

    def test_hourly_pruning_waits_then_removes_only_old_jobs(self):
        old = self.broker.job_root('c' * 32)
        fresh = self.broker.job_root('d' * 32)
        os.utime(old, (1, 1))
        with patch.object(broker_net.time, 'sleep', side_effect=[None, InterruptedError]) as sleep:
            with self.assertRaises(InterruptedError):
                broker_net.prune_periodically(self.broker.root)
        self.assertEqual(sleep.call_args_list, [unittest.mock.call(3600)] * 2)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_requests_without_valid_token_are_rejected(self):
        client = self.paired()
        for header in (None, 'Bearer wrong', 'Basic abc'):
            connection = http.client.HTTPSConnection('127.0.0.1', self.port, context=__import__('ssl')._create_unverified_context())
            headers = {'X-Portkeeper-Protocol': str(PROTOCOL), 'Content-Type': 'application/json'}
            if header:
                headers['Authorization'] = header
            connection.request('POST', '/v1', json.dumps({'op': 'health'}), headers)
            self.assertEqual(connection.getresponse().status, 401)
            connection.close()
        client.link = {**client.link, 'token': 'x' * 64}
        with self.assertRaisesRegex(Invalid, 'pair again'):
            client.request('health')
        self.assertFalse(self.broker.calls)

    def test_token_is_absent_from_job_logs_and_error_text(self):
        client = self.paired()
        token = client.link['token']
        job, operation = 'e' * 32, 'f' * 32
        seed = client.root / 'jobs' / job / 'seed'
        seed.mkdir(parents=True)
        (seed / 'source').write_text('public')
        result = client.request('run', job_id=job, operation_id=operation, seed='seed', script='true', phase='public')
        self.assertNotIn(token, (client.root / 'jobs' / job / result['log']).read_text())
        client.link = {**client.link, 'token': token + 'invalid'}
        with self.assertRaises(Invalid) as error:
            client.request('health')
        self.assertNotIn(token, str(error.exception))

    def test_rejected_requests_close_the_connection(self):
        connection = http.client.HTTPSConnection('127.0.0.1', self.port, context=__import__('ssl')._create_unverified_context())
        connection.request('POST', '/v1', json.dumps({'op': 'health'}), {'X-Portkeeper-Protocol': str(PROTOCOL)})
        response = connection.getresponse()
        self.assertEqual(response.status, 401)
        self.assertEqual(response.getheader('Connection'), 'close')
        connection.close()

    def test_stalled_clients_are_bounded_and_do_not_block_others(self):
        import socket
        stalled = [socket.create_connection(('127.0.0.1', self.port)) for _ in range(3)]  # never handshake
        try:
            self.assertEqual(broker_net.NetHandler.timeout, 30)
            self.assertTrue(self.paired().request('health')['ready'])
        finally:
            for sock in stalled:
                sock.close()

    def test_connection_cap_refuses_excess_connections(self):
        self.assertEqual(self.server.max_connections, 32)
        for _ in range(self.server.max_connections):
            self.assertTrue(self.server.slots.acquire(blocking=False))
        try:
            with self.assertRaises(Exception):
                self.paired()
        finally:
            for _ in range(self.server.max_connections):
                self.server.slots.release()
        self.assertTrue(self.paired().request('health')['ready'])

    def test_tls_requires_a_complete_certificate_pin(self):
        for pin in (None, '', 'a' * 12, 'z' * 64):
            with self.subTest(pin=pin), self.assertRaisesRegex(Invalid, 'fingerprint'):
                broker_net.PinnedTLS('127.0.0.1', self.port, pin, 10)

    def test_certificate_pin_mismatch_is_refused(self):
        client = self.paired()
        client.link = {**client.link, 'fingerprint': '0' * 64}
        with self.assertRaisesRegex(Invalid, 'does not match the paired fingerprint'):
            client.request('health')

    def test_protocol_mismatch_is_refused_with_clear_message(self):
        client = self.paired()
        # The broker refuses a client speaking another protocol, for jobs and pairing alike.
        for path in ('/v1', '/v1/pair'):
            connection = client.connection(10)
            connection.request('POST', path, json.dumps({'op': 'health'}), {**client.headers(), 'X-Portkeeper-Protocol': str(PROTOCOL + 1)})
            response = connection.getresponse()
            self.assertEqual(response.status, 426)
            self.assertIn('Incompatible Portkeeper protocol', json.loads(response.read())['error'])
            connection.close()
        self.assertFalse(self.broker.calls)
        # The app refuses a broker that reports another protocol.
        with patch.object(client, 'json_call', return_value={'result': {'ready': True, 'protocol': PROTOCOL + 1}}):
            with self.assertRaisesRegex(Invalid, 'Incompatible Portkeeper protocol'):
                client.request('health')

    def test_replay_seed_tree_round_trips(self):
        client = self.paired()
        job, operation = '3' * 32, '4' * 32
        seed = client.root / 'jobs' / job / 'replay-seed'
        (seed / 'src').mkdir(parents=True)
        (seed / 'src' / 'main.c').write_text('int main(){}')
        result = client.request('run', job_id=job, operation_id=operation, seed='replay-seed', script='true', phase='public')
        snapshot = client.root / 'jobs' / job / result['snapshot']
        self.assertEqual((snapshot / 'src' / 'main.c').read_text(), 'int main(){}')

    def test_update_script_imports_release_protocol_from_source_tree(self):
        script = (Path(__file__).resolve().parent.parent / 'deploy' / 'broker' / 'portkeeper-broker-update.sh').read_text()
        self.assertIn('cd "$work/src" && python3 -c', script)
        root = Path(__file__).resolve().parent.parent
        done = subprocess.run(['python3', '-c', 'from portkeeper.broker_net import PROTOCOL; print(PROTOCOL)'],
                              cwd=root, capture_output=True, text=True)
        self.assertEqual(done.stdout.strip(), str(broker_net.PROTOCOL), done.stderr)

    def test_public_health_reports_worker_readiness_and_update_script_requires_it(self):
        conn = http.client.HTTPSConnection('127.0.0.1', self.server.server_address[1], context=ssl._create_unverified_context())
        conn.request('GET', '/v1/health', headers={'X-Portkeeper-Protocol': str(broker_net.PROTOCOL)})
        self.assertIs(json.loads(conn.getresponse().read())['ready'], True)
        conn.close()
        script = (Path(__file__).resolve().parent.parent / 'deploy' / 'broker' / 'portkeeper-broker-update.sh').read_text()
        self.assertIn('"ready": *true', script)

    def test_unknown_job_types_are_rejected_by_broker_handler(self):
        client = self.paired()
        with patch.object(self.broker, 'handle', side_effect=Invalid('Unknown broker operation')):
            with self.assertRaisesRegex(Invalid, 'Unknown broker operation'):
                client.request('shell', command='id')

    def test_run_round_trip_uploads_seed_and_downloads_snapshot_and_log(self):
        client = self.paired()
        job, operation = '1' * 32, '2' * 32
        seed = client.root / 'jobs' / job / 'seed'
        (seed / 'src').mkdir(parents=True)
        (seed / 'src' / 'main.c').write_text('int main(){}')
        result = client.request('run', job_id=job, operation_id=operation, seed='seed', script='true', phase='public')
        snapshot = client.root / 'jobs' / job / result['snapshot']
        self.assertEqual((snapshot / 'built.txt').read_text(), 'built')
        self.assertEqual((snapshot / 'src' / 'main.c').read_text(), 'int main(){}')
        self.assertEqual((client.root / 'jobs' / job / result['log']).read_text(), 'compiled')
        self.assertEqual(tree_identity(snapshot)['sha256'], result['identity']['sha256'])

    def test_oversized_upload_is_refused_before_reading_body(self):
        client = self.paired()
        job = '3' * 32
        connection = client.connection(10)
        connection.request('PUT', f'/v1/jobs/{job}/trees/seed', b'', client.headers({'Content-Length': str(broker_net.MAX_TREE_UPLOAD + 1)}))
        response = connection.getresponse()
        self.assertEqual(response.status, 400)
        self.assertIn('size limit', json.loads(response.read())['error'])
        connection.close()

    def test_artifact_download_limit_is_enforced_by_client(self):
        client = self.paired()
        job = '4' * 32
        (self.broker.job_root(job) / ('a' * 32 + '.log')).write_bytes(b'x' * 100)
        destination = self.root / 'out.log'
        with self.assertRaisesRegex(Invalid, 'size'):
            client.download(f'/v1/jobs/{job}/files/{"a" * 32}.log', destination, 10)
        client.download(f'/v1/jobs/{job}/files/{"a" * 32}.log', destination, 100)
        self.assertEqual(destination.stat().st_size, 100)

    def test_oversized_log_on_broker_is_not_served(self):
        client = self.paired()
        job = '5' * 32
        with patch.object(broker_net, 'MAX_LOG', 10):
            (self.broker.job_root(job) / ('b' * 32 + '.log')).write_bytes(b'x' * 11)
            with self.assertRaisesRegex(Invalid, 'size limit'):
                client.download(f'/v1/jobs/{job}/files/{"b" * 32}.log', self.root / 'x', 1024)

    def test_artifact_paths_cannot_escape_job_directory(self):
        client = self.paired()
        job = '6' * 32
        for name in ('../secret', 'downloads/../../x', 'link.json'):
            with self.assertRaises(Invalid):
                client.download(f'/v1/jobs/{job}/files/{name}', self.root / 'x', 1024)


class OfflineQueueTests(unittest.TestCase):
    def test_jobs_queue_while_remote_broker_is_offline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            engine = Engine(root / 'data', root / 'inputs', root / 'library')
            try:
                link = {'url': 'https://127.0.0.1:9', 'fingerprint': 'a' * 64, 'token': 't' * 40}
                automation = Automation(engine, RemoteBrokerClient(link, root / 'exchange'))
                automation.offline_poll = 0.01
                status = automation.status()
                self.assertTrue(status['remote'])
                self.assertTrue(status['worker']['offline'])
                self.assertFalse(status['worker']['ready'])
                # Offline, then reachable: the waiting job proceeds with a fresh time budget.
                job = engine.store.put('automation', {'id': '7' * 32, 'policy': {**automation.policy()}, 'deadline': 0})
                calls = []
                def health(op, **payload):
                    calls.append(op)
                    if len(calls) < 3:
                        raise Invalid('Worker broker could not be reached')
                    return {'ready': True, 'protocol': PROTOCOL}
                automation.broker.request = health
                # The stored deadline (0) is long past: offline time must not count against it.
                automation.wait_for_broker('7' * 32)
                self.assertGreaterEqual(len(calls), 3)
                self.assertGreater(engine.store.get('automation', '7' * 32)['deadline'], 0)
            finally:
                engine.close()

    def test_pair_and_unpair_persist_the_link_privately(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            engine = Engine(root / 'data', root / 'inputs', root / 'library')
            broker = FakeBroker(root / 'broker' / 'exchange')
            server = make_net_server(broker, '127.0.0.1', 0, root / 'broker' / 'link')
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                code = server.link.new_code()
                automation = engine.automation
                status = automation.pair({'url': f'https://127.0.0.1:{server.server_address[1]}', 'code': code, 'fingerprint': server.fingerprint})
                self.assertTrue(status['worker']['ready'])
                token = json.loads(automation.link_path.read_text())['token']
                self.assertNotIn(token, json.dumps(status))
                self.assertNotIn(token, json.dumps(automation.status()))
                self.assertNotIn(token, server.link.path.read_text())
                self.assertEqual(server.link.path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(oct(automation.link_path.stat().st_mode & 0o777), '0o600')
                self.assertIsNotNone(Automation(engine).broker)
                self.assertFalse(automation.unpair()['worker']['ready'])
                self.assertFalse(automation.link_path.exists())
            finally:
                server.shutdown(); server.server_close(); engine.close()

    def test_pairing_does_not_replace_broker_when_job_starts_during_handshake(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            engine = Engine(root / 'data', root / 'inputs', root / 'library')
            try:
                automation = engine.automation
                original = automation.broker = unittest.mock.MagicMock()
                def handshake(*args):
                    with engine.lock:
                        engine.store.put('automation', {'id': '9' * 32, 'project_id': 'p', 'status': 'running'})
                    return {'url': 'https://broker:8788', 'fingerprint': 'a' * 64, 'token': 'private-token'}
                with patch.object(broker_net, 'pair', side_effect=handshake):
                    with self.assertRaisesRegex(Invalid, 'active'):
                        automation.pair({'url': 'https://broker:8788', 'code': '1234-5678-9012', 'fingerprint': 'a' * 64})
                self.assertIs(automation.broker, original)
                self.assertFalse(automation.link_path.exists())
            finally:
                engine.close()

    def test_pair_and_unpair_are_refused_while_a_job_is_active(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            engine = Engine(root / 'data', root / 'inputs', root / 'library')
            try:
                automation = engine.automation
                automation.broker = unittest.mock.MagicMock()
                engine.store.put('automation', {'id': '8' * 32, 'project_id': 'p', 'status': 'queued'})
                with self.assertRaisesRegex(Invalid, 'active'):
                    automation.unpair()
                with self.assertRaisesRegex(Invalid, 'active'):
                    automation.pair({'url': 'https://127.0.0.1:1', 'code': '1' * 12, 'fingerprint': 'a' * 64})
                self.assertIsNotNone(automation.broker)
            finally:
                engine.close()


class TemplateTests(unittest.TestCase):
    def test_unraid_template_has_icon_webui_appdata_and_broker_url(self):
        import xml.etree.ElementTree as ET
        root = ET.parse(Path(__file__).parent.parent / 'unraid' / 'portkeeper.xml').getroot()
        self.assertTrue(root.findtext('Icon').startswith('https://'))
        self.assertIn(':8787', root.findtext('WebUI'))
        configs = {c.get('Target'): c for c in root.findall('Config')}
        self.assertEqual(configs['/data'].get('Type'), 'Path')
        self.assertEqual(configs['PORTKEEPER_BROKER_URL'].get('Type'), 'Variable')
        self.assertEqual(root.findtext('Privileged'), 'false')
        self.assertFalse([c for c in root.findall('Config') if 'docker.sock' in (c.get('Target') or '')])


if __name__ == '__main__':
    unittest.main()
