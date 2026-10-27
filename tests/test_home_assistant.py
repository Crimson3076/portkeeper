import json
import stat
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from portkeeper.backups import create_backup
from portkeeper.engine import Engine
from portkeeper.home_assistant import NoRedirect, server_url
from portkeeper.safety import Invalid


TOKEN = 'synthetic-long-lived-access-token'


class HomeAssistantTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(Path(self.temp.name) / 'state')
        self.addCleanup(self.engine.close)
        self.ha = self.engine.home_assistant

    def config(self, **changes):
        return self.ha.configure({'url': 'http://homeassistant.local:8123', 'token': TOKEN, **changes})

    def test_credentials_are_private_persisted_and_never_in_state(self):
        config = self.config()
        self.assertTrue(config['has_token'])
        self.assertFalse(config['enabled'])
        self.assertEqual(stat.S_IMODE(self.ha.secrets.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.ha.secrets.parent.stat().st_mode), 0o700)
        self.assertEqual(json.loads(self.ha.secrets.read_text())['token'], TOKEN)
        self.assertNotIn(TOKEN, json.dumps(self.engine.state()))
        self.ha.configure({'url': config['url'], 'token': ''})
        self.assertEqual(self.ha._config()['token'], TOKEN)
        other = Engine(self.engine.data)
        try:
            self.assertTrue(other.home_assistant.public_config()['has_token'])
            self.assertEqual(other.home_assistant._config()['token'], TOKEN)
        finally:
            other.close()

    def test_changing_server_requires_new_token_and_removing_disables_delivery(self):
        self.config(enabled=True)
        with self.assertRaisesRegex(Invalid, 'new token'):
            self.ha.configure({'url': 'https://different.example'})
        cleared = self.ha.configure({'clear_token': True})
        self.assertFalse(cleared['has_token'])
        self.assertFalse(cleared['enabled'])
        with self.assertRaises(Invalid):
            self.ha.configure({'enabled': True})

    def test_input_validation_never_echoes_tokens(self):
        self.config()
        for changes in ({'token': 'private\nsecret'}, {'token': 123}, {'enabled': 'true'},
                        {'clear_token': 'false'}, {'token': 'x' * 4097}, {'url': None}, {'unknown': TOKEN}):
            with self.subTest(changes=tuple(changes)):
                with self.assertRaises(Invalid) as raised:
                    self.ha.configure(changes)
                self.assertNotIn(TOKEN, str(raised.exception))
        for url in ('file:///secret', 'https://user:secret@ha.example', 'http://ha.local?token=secret',
                    'http://ha.local#fragment', 'http://ha.local:bad', 'http://ha.local/%2e%2e/secret', 'http://bad host:8123'):
            with self.subTest(url=url), self.assertRaises(Invalid):
                server_url(url)

    def test_redirects_never_forward_the_token_even_on_same_host(self):
        request = urllib.request.Request('https://ha.example/api/', headers={'Authorization': 'Bearer ' + TOKEN})
        for url in ('https://evil.example/', 'https://ha.example/login', 'http://ha.example/api/'):
            with self.subTest(url=url), self.assertRaisesRegex(Invalid, 'not forwarded'):
                NoRedirect().redirect_request(request, None, 302, '', {}, url)

    def test_metadata_backup_includes_ha_credentials_with_private_modes(self):
        self.config()
        backup = create_backup(self.engine.store, self.engine.data)
        saved = backup / 'secrets/home-assistant.json'
        self.assertEqual(json.loads(saved.read_text())['token'], TOKEN)
        self.assertEqual(stat.S_IMODE(saved.stat().st_mode), 0o600)

    def test_inbox_events_are_deduplicated_and_delivery_runs_in_background(self):
        self.config(enabled=True)
        project = self.engine.create_project({'name': 'Example', 'repo': 'example/port', 'targets': ['linux']})
        entered, release = threading.Event(), threading.Event()
        def send(*args):
            entered.set()
            release.wait(timeout=5)
        try:
            with patch.object(self.ha, '_send', side_effect=send) as send:
                first = self.engine.lifecycle.notify(project['id'], 'ready', 'Package ready', event_key='same-build')
                self.assertTrue(entered.wait(timeout=3))
                self.assertFalse(release.is_set(), 'The caller returns while HA is still processing the request')
                second = self.engine.lifecycle.notify(project['id'], 'ready', 'Package ready', event_key='same-build')
                self.assertEqual(first, second)
                release.set()
                self.ha.executor.shutdown(wait=True)
                self.assertEqual(send.call_count, 1)
        finally:
            release.set()

    def test_failure_leaves_local_inbox_and_visible_error_without_secret(self):
        self.config(enabled=True)
        project = self.engine.create_project({'name': 'Example', 'repo': 'example/port', 'targets': ['linux']})
        with patch.object(self.ha, '_request', side_effect=Invalid('Could not connect to Home Assistant')):
            notice = self.engine.lifecycle.notify(project['id'], 'ready', 'Package ready')
            self.ha.executor.shutdown(wait=True)
        self.assertEqual(self.engine.lifecycle.notifications()[0]['id'], notice['id'])
        config = self.ha.public_config()
        self.assertIn('Could not connect', config['last_error'])
        self.assertNotIn(TOKEN, json.dumps(self.engine.state()))

    def test_disabled_or_old_connection_never_sends_queued_notifications(self):
        self.config(enabled=True)
        old = self.ha._config()['generation']
        self.ha.configure({'enabled': False})
        with patch.object(self.ha, '_send') as send:
            self.ha.enqueue({'id': 'new', 'project_name': 'Example', 'message': 'Ready'})
            self.ha._deliver({'id': 'old'}, old)
            send.assert_not_called()

    def test_full_queue_is_bounded_and_does_not_block_notification_creation(self):
        self.config(enabled=True)
        for _ in range(32):
            self.ha.slots.acquire(blocking=False)
        try:
            self.ha.enqueue({'id': 'new', 'project_name': 'Example', 'message': 'Ready'})
            self.assertIn('queue is full', self.ha.public_config()['last_error'])
        finally:
            for _ in range(32):
                self.ha.slots.release()

    def test_bad_config_symlinks_and_oversized_responses_fail_safely(self):
        self.config()
        config = self.ha._config()
        self.ha.secrets.unlink()
        target = Path(self.temp.name) / 'outside.json'
        target.write_text('{}')
        self.ha.secrets.symlink_to(target)
        with self.assertRaises(Invalid):
            self.ha.public_config()
        self.ha.secrets.unlink()
        self.config()
        from io import BytesIO
        with patch.object(self.ha.opener, 'open', return_value=BytesIO(b'x' * 65537)), self.assertRaisesRegex(Invalid, 'size limit'):
            self.ha._request(config)
        self.ha.secrets.write_text('{"token":"private","enabled":true}')
        with self.assertRaisesRegex(Invalid, 'could not be read'):
            self.ha.public_config()
        # A malformed optional configuration never escapes into a build's inbox call.
        self.ha.enqueue({'id': 'new', 'project_name': 'Example', 'message': 'Ready'})


class WireContractTests(unittest.TestCase):
    def test_actual_http_auth_api_probe_and_persistent_notification(self):
        requests, response_code = [], [200]
        class FakeHA(BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append({'path': self.path, 'authorization': self.headers.get('Authorization')})
                self.reply({'message': 'API running.'})
            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append({'path': self.path, 'authorization': self.headers.get('Authorization'), 'body': body})
                self.reply([])
            def reply(self, data):
                raw = json.dumps(data).encode()
                self.send_response(response_code[0]); self.send_header('Content-Length', str(len(raw)))
                self.end_headers(); self.wfile.write(raw)
            def log_message(self, *args):
                pass
        try:
            server = ThreadingHTTPServer(('127.0.0.1', 0), FakeHA)
        except PermissionError:
            self.skipTest('Local sockets are unavailable')
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as temp:
                engine = Engine(Path(temp))
                try:
                    ha = engine.home_assistant
                    ha.configure({'url': f'http://127.0.0.1:{server.server_port}', 'token': TOKEN})
                    self.assertTrue(ha.test()['ok'])
                    self.assertTrue(ha.send_test()['ok'])
                    self.assertEqual(requests[0]['path'], '/api/')
                    self.assertEqual(requests[1]['path'], '/api/services/persistent_notification/create')
                    self.assertEqual(requests[1]['body']['notification_id'], 'portkeeper_test')
                    self.assertTrue(all(r['authorization'] == 'Bearer ' + TOKEN for r in requests))
                    self.assertTrue(ha.public_config()['connected'])
                    response_code[0] = 401
                    with self.assertRaisesRegex(Invalid, 'rejected the token'):
                        ha.test()
                    self.assertFalse(ha.public_config()['connected'])
                finally:
                    engine.close()
        finally:
            server.shutdown(); server.server_close(); thread.join(timeout=5)
