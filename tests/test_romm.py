import hashlib
import io
import json
import stat
import tempfile
import threading
import unittest
import urllib.parse
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from portkeeper.romm import RomM, normalize_rom
from portkeeper.safety import Invalid

ROM = b'\x80\x37\x12\x40' + bytes(range(60))
SHA1 = hashlib.sha1(ROM).hexdigest()


def archive(entries):
    output = io.BytesIO()
    with zipfile.ZipFile(output, 'w') as bundle:
        for name, data in entries:
            bundle.writestr(name, data)
    return output.getvalue()


class RomMTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        swapped = bytearray(ROM)
        swapped[0::2], swapped[1::2] = swapped[1::2], swapped[0::2]
        self.state = {'token': 'rmm_supersecret', 'data': archive([('rom.z64', ROM), ('swap.v64', swapped)]),
                      'requests': [], 'downloads': 0, 'status': None, 'redirect': None, 'deny_me': False}
        state = self.state

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                state['requests'].append((self.path, self.headers.get('Authorization')))
                if self.headers.get('Authorization') != 'Bearer ' + state['token']:
                    self.send_error(401)
                    return
                if state['status']:
                    self.send_error(state['status'], state['token'])
                    return
                route = urllib.parse.urlsplit(self.path).path
                if route == '/api/heartbeat':
                    result = {'SYSTEM': {'VERSION': '5.2.0'}, 'secret': state['token']}
                elif route == '/api/users/me':
                    if state['deny_me']:
                        self.send_error(403)
                        return
                    result = {'id': 2, 'username': 'example', 'password': state['token']}
                elif route == '/api/roms/22':
                    result = {'id': 22, 'name': 'Ocarina', 'platform_slug': 'n64', 'files': [
                        {'id': 901, 'file_name': 'Ocarina.zip', 'file_size_bytes': len(state['data']),
                         'sha1_hash': state.get('sha1', hashlib.sha1(state['data']).hexdigest()), 'full_path': '/private/server/rom.zip'}]}
                elif route == '/api/roms':
                    result = {'items': [{'id': 22, 'name': 'Ocarina', 'platform_slug': 'n64', 'files': []}], 'total': 1}
                elif route == '/api/roms/901/files/content/Ocarina.zip':
                    state['downloads'] += 1
                    if state['redirect']:
                        self.send_response(302)
                        self.send_header('Location', state['redirect'])
                        self.end_headers()
                        return
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(state['data'])))
                    self.end_headers()
                    self.wfile.write(state['data'])
                    return
                elif route == '/api/same-origin-download':
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(state['data'])))
                    self.end_headers()
                    self.wfile.write(state['data'])
                    return
                else:
                    self.send_error(404)
                    return
                data = json.dumps(result).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close)
        self.url = f'http://127.0.0.1:{self.server.server_port}'
        self.client = RomM(self.root)
        self.client.configure(self.url, self.state['token'])

    def _close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def inspect(self):
        return self.client.inspect_rom(22, 'Ocarina.zip', {SHA1: 'Synthetic revision'})

    def test_credentials_private_and_configuration_redacted(self):
        self.assertEqual(stat.S_IMODE(self.client.secrets.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.client.secrets.parent.stat().st_mode), 0o700)
        public = self.client.public_config()
        self.assertNotIn(self.state['token'], json.dumps(public))
        self.client.configure(self.url, '')
        self.assertTrue(self.client.public_config()['has_token'])
        with self.assertRaises(Invalid):
            self.client.configure('https://different.test')
        self.client.configure(self.url, clear_token=True)
        self.assertFalse(self.client.public_config()['configured'])
        with self.assertRaisesRegex(Invalid, 'Client API Token'):
            self.client.search('Ocarina')

    def test_configuration_rejects_plaintext_remote_and_embedded_credentials(self):
        for value in ('http://romm.example', 'https://user:password@example.test', 'https://example.test?key=secret',
                      'https://example.test/#x', 'https://example.test:invalid', 'file:///tmp/server', 'https://example.test/../other'):
            with self.subTest(value=value), self.assertRaises(Invalid):
                self.client.configure(value, 'token')
        with self.assertRaises(Invalid):
            self.client.configure(self.url, 'token\nheader')

    def test_read_only_connection_and_search_normalize_public_fields(self):
        connection = self.client.test_connection()
        self.assertTrue(connection['ok'])
        self.assertEqual(connection['version'], '5.2.0')
        self.assertEqual(connection['scopes'], ['roms.read', 'me.read'])
        self.assertNotIn(self.state['token'], json.dumps(connection))
        result = self.client.search('Ocarina & Time')
        self.assertEqual(result['items'][0]['platform'], 'n64')
        path, header = self.state['requests'][-1]
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(path).query)['search_term'], ['Ocarina & Time'])
        self.assertEqual(header, 'Bearer ' + self.state['token'])
        self.assertNotIn('full_path', json.dumps(self.client.detail(22)))

    def test_optional_identity_scope_does_not_block_rom_read_access(self):
        self.state['deny_me'] = True
        result = self.client.test_connection()
        self.assertTrue(result['ok'])
        self.assertIsNone(result['user'])
        self.assertEqual(result['scopes'], ['roms.read'])
        self.assertTrue(result['warnings'])

    def test_permission_errors_do_not_echo_upstream_secret_bodies(self):
        for status in (401, 403, 500):
            self.state['status'] = status
            with self.subTest(status=status), self.assertRaises(Invalid) as caught:
                self.client.search('Ocarina')
            self.assertNotIn(self.state['token'], str(caught.exception))
            self.assertIn(str(status), str(caught.exception))

    def test_inspection_normalizes_matches_and_cache_is_reused(self):
        result = self.inspect()
        self.assertFalse(result['cache_reused'])
        self.assertEqual(len(result['candidates']), 2)
        self.assertTrue(all(item['supported'] for item in result['candidates']))
        self.assertEqual({item['sha1'] for item in result['candidates']}, {SHA1})
        selected = self.client.selected_input(result['cache_key'], 'swap.v64', SHA1)
        self.assertEqual(Path(selected['path']).read_bytes(), ROM)
        self.assertEqual(selected['byte_order'], 'byte-swapped')
        self.assertNotIn(self.state['token'], json.dumps(result))
        second = self.inspect()
        self.assertTrue(second['cache_reused'])
        self.assertEqual(self.state['downloads'], 1)
        self.assertEqual(self.client.selected_input(result['cache_key'], 'swap.v64', SHA1)['path'], selected['path'])

    def test_raw_and_normalized_cache_tampering_is_rejected(self):
        result = self.inspect()
        selected = self.client.selected_input(result['cache_key'], 'rom.z64', SHA1)
        Path(selected['path']).write_bytes(b'tampered')
        with self.assertRaisesRegex(Invalid, 'Normalized ROM cache'):
            self.client.selected_input(result['cache_key'], 'rom.z64', SHA1)
        (self.client.cache / result['cache_key'] / 'download.bin').write_bytes(b'tampered')
        with self.assertRaisesRegex(Invalid, 'checksum'):
            self.inspect()
        self.assertEqual(self.state['downloads'], 1)

    def test_wrong_hash_and_unverified_revision_cannot_be_selected(self):
        result = self.client.inspect_rom(22, 'Ocarina.zip', {})
        self.assertFalse(result['candidates'][0]['supported'])
        with self.assertRaises(Invalid):
            self.client.selected_input(result['cache_key'], 'rom.z64', SHA1)
        result = self.inspect()
        with self.assertRaises(Invalid):
            self.client.selected_input(result['cache_key'], 'rom.z64', 'f' * 40)
        self.state['sha1'] = '0' * 40
        with self.assertRaisesRegex(Invalid, 'RomM SHA1'):
            self.inspect()

    def test_cross_origin_redirect_is_not_requested_and_token_not_forwarded(self):
        received = []
        class Sink(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                received.append(self.headers.get('Authorization'))
                self.send_response(200)
                self.end_headers()
        sink = ThreadingHTTPServer(('127.0.0.1', 0), Sink)
        thread = threading.Thread(target=sink.serve_forever, daemon=True)
        thread.start()
        try:
            self.state['redirect'] = f'http://127.0.0.1:{sink.server_port}/steal'
            with self.assertRaisesRegex(Invalid, 'different origin'):
                self.inspect()
            self.assertEqual(received, [])
        finally:
            sink.shutdown()
            sink.server_close()
            thread.join()

    def test_same_origin_redirect_is_allowed(self):
        self.state['redirect'] = self.url + '/api/same-origin-download'
        result = self.inspect()
        self.assertTrue(result['candidates'][0]['supported'])

    def test_archive_traversal_links_duplicates_and_limits_rejected(self):
        fixtures = [('parent', archive([('../escape.z64', ROM)])),
                    ('duplicate', archive([('rom.z64', ROM), ('ROM.Z64', ROM)])),
                    ('collision', archive([('dir', b'x'), ('dir/rom.z64', ROM)]))]
        linked = io.BytesIO()
        with zipfile.ZipFile(linked, 'w') as bundle:
            entry = zipfile.ZipInfo('link.z64')
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            bundle.writestr(entry, ROM)
        fixtures.append(('link', linked.getvalue()))
        for label, data in fixtures:
            self.state['data'] = data
            with self.subTest(label=label), self.assertRaises(Invalid):
                self.inspect()
        self.state['data'] = archive([('rom.z64', ROM)])
        for limits in ({'MAX_ARCHIVE': 2}, {'MAX_EXPANDED': 2}, {'MAX_MEMBERS': 0}, {'MAX_ROM': 2}):
            with self.subTest(limits=limits), patch.multiple('portkeeper.romm', **limits):
                if 'MAX_ROM' in limits:
                    self.assertFalse(self.inspect()['candidates'][0]['supported'])
                else:
                    with self.assertRaises(Invalid):
                        self.inspect()

    def test_normalization_and_invalid_input_validation(self):
        little = bytearray(ROM)
        little[0::4], little[1::4], little[2::4], little[3::4] = little[3::4], little[2::4], little[1::4], little[0::4]
        self.assertEqual(normalize_rom(little), (ROM, 'little-endian'))
        for value in (b'abc', b'wrong123'):
            with self.assertRaises(Invalid):
                normalize_rom(value)
        for value in (True, 0, '../rom'):
            with self.assertRaises(Invalid):
                self.client.detail(value)
        with self.assertRaises(Invalid):
            self.client.selected_input('../escape', None, SHA1)


if __name__ == '__main__':
    unittest.main()
