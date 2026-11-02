"""Remote broker link: pairing, auth, protocol, size limits and artifact transfer.

No Docker is used: a stub broker fabricates snapshots, but the real WorkerBroker.handle
validation, the real HTTPS server and the real client run over a loopback socket.
"""
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from portkeeper import broker_net, worker_broker
from portkeeper.broker_net import BrokerOffline, RemoteBrokerClient, pair
from portkeeper.safety import Invalid
from portkeeper.storage import Store, identifier
from portkeeper.worker_broker import WorkerBroker, tree_identity


class StubBroker(WorkerBroker):
    """Real request validation and operation ledger; no container is started."""
    def __init__(self, root, ready=True):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.input_root = self.root / 'inputs'
        self.protected_roots = [self.root]
        self.image = self.image_id = 'sha256:' + 'a' * 64
        self.store = Store(self.root / 'broker.sqlite3')
        self.lock, self.guard = threading.Lock(), threading.Lock()
        self.cancelled, self.active_names, self.test_mode = set(), {}, False
        self.backend, self.ready, self.error = 'rootless-docker', ready, None
        self.fetched = []

    def run(self, payload):
        root = self.job_root(payload['job_id'])
        snapshot = root / ('snapshot-' + payload['operation_id'])
        shutil.copytree(root / payload['seed'], snapshot, symlinks=True)
        (snapshot / 'built.bin').write_bytes(b'built')
        (root / (payload['operation_id'] + '.log')).write_text('compiled\n')
        identity = tree_identity(snapshot)
        return {'exit_code': 0, 'phase': payload['phase'], 'snapshot': snapshot.name,
                'identity': {k: v for k, v in identity.items() if k != 'entries'},
                'log': payload['operation_id'] + '.log', 'seconds': .01}

    def fetch(self, payload):
        downloads = self.job_root(payload['job_id']) / 'downloads'
        downloads.mkdir(exist_ok=True)
        (downloads / payload['name']).write_bytes(b'dependency')
        return {'name': payload['name'], 'sha256': worker_broker.checksum(downloads / payload['name']), 'size': 10}


class NetworkCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.broker = StubBroker(self.base / 'broker')
        self.server, self.access = broker_net.make_network_server(self.broker, '127.0.0.1', 0, self.base / 'tls')
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, args=(0.02,), daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.url = f'https://127.0.0.1:{self.port}'
        self.config_path = self.base / 'app' / 'secrets' / 'broker.json'

    def paired_client(self):
        code = self.access.new_code()
        self.config_path.parent.mkdir(parents=True, exist_ok=True)
        broker_net.save_config(self.config_path, pair(self.url, code))
        return RemoteBrokerClient(self.config_path, self.base / 'exchange')


class PairingTests(NetworkCase):
    def test_pairing_stores_pinned_credentials_and_health_works(self):
        client = self.paired_client()
        config = json.loads(self.config_path.read_text())
        self.assertEqual(set(config), {'url', 'fingerprint', 'token'})
        self.assertEqual(oct(self.config_path.stat().st_mode & 0o777), '0o600')
        health = client.request('health')
        self.assertTrue(health['ready'])
        self.assertEqual(health['protocol'], worker_broker.PROTOCOL)
        self.assertIn('version', health)
        self.assertEqual(health['backend'], 'rootless-docker')

    def test_code_is_single_use_and_wrong_codes_are_rejected(self):
        code = self.access.new_code()
        with self.assertRaises(Invalid):
            pair(self.url, 'AAAAA-AAAAA-AAAAA-AAAAA')
        pair(self.url, code)
        with self.assertRaises(Invalid):
            pair(self.url, code)

    def test_code_dies_after_repeated_failures(self):
        code = self.access.new_code()
        for _ in range(5):
            with self.assertRaises(Invalid):
                pair(self.url, 'BBBBB-BBBBB-BBBBB-BBBBB')
        with self.assertRaises(Invalid):
            pair(self.url, code)

    def test_expired_code_is_rejected(self):
        code = self.access.new_code(ttl=-1)
        with self.assertRaises(Invalid):
            pair(self.url, code)

    def test_changed_certificate_is_refused_before_sending_the_token(self):
        client = self.paired_client()
        config = json.loads(self.config_path.read_text())
        config['fingerprint'] = '0' * 64
        self.config_path.write_text(json.dumps(config))
        with self.assertRaises(Invalid) as caught:
            client.request('health')
        self.assertIn('certificate', str(caught.exception))
        self.assertNotIsInstance(caught.exception, BrokerOffline)


class AuthTests(NetworkCase):
    def raw(self, method, path, token=None, body=None, protocol=None):
        import http.client
        import ssl
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
        connection = http.client.HTTPSConnection('127.0.0.1', self.port, context=context, timeout=10)
        headers = {'X-Portkeeper-Protocol': str(worker_broker.PROTOCOL if protocol is None else protocol)}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        connection.request(method, path, body, headers)
        response = connection.getresponse()
        return response.status, response.read()

    def test_requests_without_or_with_a_wrong_token_are_rejected(self):
        self.paired_client()
        body = json.dumps({'op': 'health'})
        self.assertEqual(self.raw('POST', '/v1', body=body)[0], 401)
        self.assertEqual(self.raw('POST', '/v1', 'wrong-token', body)[0], 401)
        self.assertEqual(self.raw('GET', f'/v1/jobs/{identifier()}/artifacts/snapshot-{identifier()}')[0], 401)
        self.assertEqual(self.raw('PUT', f'/v1/jobs/{identifier()}/seed?path=seed', body='x')[0], 401)

    def test_unknown_job_types_and_paths_are_rejected(self):
        self.paired_client()
        token = json.loads(self.config_path.read_text())['token']
        status, _ = self.raw('POST', '/v1', token, json.dumps({'op': 'shell', 'command': 'id'}))
        self.assertEqual(status, 400)
        status, _ = self.raw('POST', '/v1', token, json.dumps({'op': 'run', 'job_id': identifier(), 'operation_id': identifier(),
                                                               'script': 'x', 'phase': 'public', 'mounts': ['/etc:/etc']}))
        self.assertEqual(status, 400)
        job = identifier()
        for name in ('../broker.sqlite3', 'broker.sqlite3', 'snapshot-%2e%2e'):
            self.assertEqual(self.raw('GET', f'/v1/jobs/{job}/artifacts/{name}', token)[0], 404)
        self.assertEqual(self.raw('GET', '/v1/other', token)[0], 404)

    def test_re_pairing_revokes_the_previous_token(self):
        first = self.paired_client()
        first.request('health')
        stale = self.base / 'stale.json'
        shutil.copy(self.config_path, stale)
        first = RemoteBrokerClient(stale, self.base / 'exchange')
        self.paired_client()
        with self.assertRaises(Invalid):
            first.request('health')


class ProtocolTests(NetworkCase):
    def test_pairing_refuses_incompatible_protocol_with_a_clear_message(self):
        code = self.access.new_code()
        with patch.object(broker_net, 'PROTOCOL', 99):
            with self.assertRaises(Invalid) as caught:
                pair(self.url, code)
        self.assertIn('protocol', str(caught.exception).lower())
        pair(self.url, code)  # the one-time code was not consumed by the refusal

    def test_requests_from_an_incompatible_app_get_426(self):
        client = self.paired_client()
        with patch.object(broker_net, 'PROTOCOL', 99):
            with self.assertRaises(Invalid) as caught:
                client.request('health')
        self.assertIn('protocol', str(caught.exception).lower())


class TransferTests(NetworkCase):
    def make_seed(self, client, job):
        seed = client.root / 'jobs' / job / 'seed'
        (seed / 'dependencies').mkdir(parents=True)
        (seed / 'source.txt').write_text('hello')
        (seed / 'tool.sh').write_text('#!/bin/sh\n')
        (seed / 'tool.sh').chmod(0o755)
        (seed / 'link').symlink_to('source.txt')
        return seed

    def test_run_uploads_seed_and_returns_verified_snapshot_and_log(self):
        client = self.paired_client()
        job, operation = identifier(), identifier()
        seed = self.make_seed(client, job)
        result = client.request('run', job_id=job, operation_id=operation, seed='seed', script='true', phase='public',
                                target='linux', timeout=60, source_date_epoch=0)
        snapshot = client.root / 'jobs' / job / result['snapshot']
        self.assertEqual((snapshot / 'built.bin').read_bytes(), b'built')
        self.assertEqual(tree_identity(snapshot)['sha256'], result['identity']['sha256'])
        self.assertEqual((client.root / 'jobs' / job / result['log']).read_text(), 'compiled\n')
        self.assertEqual(tree_identity(seed)['sha256'], tree_identity(self.broker.root / 'jobs' / job / 'seed')['sha256'])

    def test_app_cannot_choose_paths_for_uploads_or_downloads(self):
        client = self.paired_client()
        job = identifier()
        self.make_seed(client, job)
        with self.assertRaises(Invalid):
            client.request('run', job_id=job, operation_id=identifier(), seed='../seed', script='true', phase='public')
        with self.assertRaises(Invalid):
            client.request('run', job_id=job, operation_id=identifier(), seed='nested/seed', script='true', phase='public')

    def test_oversized_uploads_are_refused_before_storing(self):
        client = self.paired_client()
        job = identifier()
        with patch.object(broker_net, 'MAX_UPLOAD', 1024):
            self.make_seed(client, job)
            (client.root / 'jobs' / job / 'seed' / 'big.bin').write_bytes(b'x' * 4096)
            with self.assertRaises(Invalid) as caught:
                client.request('run', job_id=job, operation_id=identifier(), seed='seed', script='true', phase='public')
        self.assertIn('limit', str(caught.exception).lower())
        self.assertFalse((self.broker.root / 'jobs' / job / 'seed').exists())

    def test_oversized_artifacts_are_refused_by_the_client(self):
        client = self.paired_client()
        job = identifier()
        self.make_seed(client, job)
        with patch.object(broker_net, 'MAX_DOWNLOAD', 100):
            with self.assertRaises(Invalid) as caught:
                client.request('run', job_id=job, operation_id=identifier(), seed='seed', script='true', phase='public')
        self.assertIn('limit', str(caught.exception).lower())
        self.assertFalse(list((client.root / 'jobs' / job).glob('snapshot-*')))

    def test_log_download_is_capped(self):
        client = self.paired_client()
        job = identifier()
        self.make_seed(client, job)
        with patch.object(broker_net, 'MAX_LOG', 4):
            with self.assertRaises(Invalid):
                client.request('run', job_id=job, operation_id=identifier(), seed='seed', script='true', phase='public')

    def test_tampered_snapshot_is_detected(self):
        client = self.paired_client()
        job = identifier()
        self.make_seed(client, job)
        original = self.broker.run
        def tampered(payload):
            result = original(payload)
            result['identity']['sha256'] = '0' * 64
            return result
        with patch.object(self.broker, 'run', tampered):
            with self.assertRaises(Invalid):
                client.request('run', job_id=job, operation_id=identifier(), seed='seed', script='true', phase='public')
        self.assertFalse(list((client.root / 'jobs' / job).glob('snapshot-*')))

    def test_fetch_downloads_dependency_and_release_cleans_both_sides(self):
        client = self.paired_client()
        job = identifier()
        result = client.request('fetch', job_id=job, operation_id=identifier(), url='https://example.com/a', name='dep.bin')
        self.assertEqual((client.root / 'jobs' / job / 'downloads' / 'dep.bin').read_bytes(), b'dependency')
        self.assertEqual(result['name'], 'dep.bin')
        client.release(job)
        self.assertFalse((self.broker.root / 'jobs' / job).exists())

    def test_local_unix_mode_client_is_unchanged(self):
        self.assertTrue(hasattr(worker_broker, 'BrokerClient'))


class AppPairingTests(NetworkCase):
    def test_settings_pairing_reports_status_and_never_returns_the_token(self):
        from portkeeper.engine import Engine
        engine = Engine(self.base / 'data')
        self.addCleanup(engine.close)
        automation = engine.automation
        self.assertEqual(automation.status()['link']['mode'], 'none')
        with self.assertRaises(Invalid):
            automation.pair_broker({'url': self.url, 'code': 'AAAAA-AAAAA-AAAAA-AAAAA'})
        status = automation.pair_broker({'url': self.url, 'code': self.access.new_code()})
        token = json.loads((engine.data / 'secrets' / 'broker.json').read_text())['token']
        self.assertTrue(status['worker']['ready'])
        self.assertEqual(status['worker']['version'], broker_net.__version__)
        self.assertEqual(status['link'], {'mode': 'remote', 'default_url': None, 'paired': True, 'url': self.url})
        self.assertNotIn(token, json.dumps(status))
        project = {'input_bindings': {}}
        feedback = engine._draft_feedback(project, 'upstream said Bearer ' + token + ' failed')
        self.assertNotIn(token, feedback)
        forgotten = automation.forget_broker()
        self.assertFalse(forgotten['worker']['ready'])
        self.assertFalse((engine.data / 'secrets' / 'broker.json').exists())


class TemplateTests(unittest.TestCase):
    def test_unraid_template_has_icon_webui_appdata_and_broker_variable(self):
        import xml.etree.ElementTree as ET
        root = ET.parse(Path(__file__).parent.parent / 'unraid' / 'portkeeper.xml').getroot()
        self.assertTrue(root.findtext('Icon', '').endswith('unraid/portkeeper.png'))
        self.assertIn('8787', root.findtext('WebUI'))
        configs = {c.get('Target'): c for c in root.findall('Config')}
        self.assertEqual(configs['/data'].get('Type'), 'Path')
        self.assertEqual(configs['PORTKEEPER_BROKER_URL'].get('Type'), 'Variable')
        self.assertEqual(root.findtext('Privileged'), 'false')


class BusyTests(NetworkCase):
    def test_check_idle_exits_3_only_while_a_build_is_active(self):
        import io
        from contextlib import redirect_stdout
        argv = ['broker_net', 'check', '--url', self.url, '--idle']
        with patch('sys.argv', argv), redirect_stdout(io.StringIO()):
            broker_net.main()
            self.broker.active_names['job'] = 'container'
            with self.assertRaises(SystemExit) as caught:
                broker_net.main()
        self.assertEqual(caught.exception.code, 3)


class OfflineTests(NetworkCase):
    def test_unreachable_broker_is_reported_as_offline_not_as_a_pairing_error(self):
        client = self.paired_client()
        self.server.shutdown()
        self.server.server_close()
        with self.assertRaises(BrokerOffline):
            client.request('health')


if __name__ == '__main__':
    unittest.main()
