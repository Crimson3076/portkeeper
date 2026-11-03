import json
import os
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from portkeeper.discovery import (Discovery, MAX_BYTES, SiteRedirect, SOURCE, parse_catalog, parse_manifest,
                                  read_manifest, release_hint, repository, site_url, validate_app)
from portkeeper.engine import Engine, allowed_url
from portkeeper.safety import Invalid

SHA = 'ab' * 32
PNG = b'\x89PNG\r\n\x1a\nimage'


# A trimmed copy of the public catalog's shape (see docs/DISCOVERY.md); malformed entries are deliberate.
def catalog_json():
    return {'catalogVersion': 1, 'platform': 'pc', 'updated': '2026-11-03', 'origins': {}, 'developers': {}, 'titles': {}, 'apps': [
        {'id': 'soh', 'name': 'Ship of Harkinian', 'game': 'The Legend of Zelda: Ocarina of Time', 'origin': 'n64',
         'repo': 'HarbourMasters/Shipwright', 'version': '9.0', 'tag': '9.0', 'released': '2026-10-01',
         'systems': ['windows', 'linux', 'beos'], 'assets': {'linux': 'SoH-*.appimage', 'windows': 'SoH-*-Win.zip'},
         'files': {'linux': {'name': 'SoH-9.0-Linux.appimage', 'sha256': SHA, 'scan': 'clean'}, 'windows': {'name': 'x', 'sha256': 'bad'}},
         'needs': 'Your own copy of The Legend of Zelda: Ocarina of Time', 'blurb': 'Play Ocarina on PC.',
         'features': ['60 FPS', 'Mods'], 'tags': ['zelda'], 'genre': ['Adventure'], 'license': 'MIT', 'downloads': 900, 'stars': 5,
         'added': '2026-01-02', 'cover': 'https://recompstore.com/img/soh-cover.png', 'hero': 'https://evil.test/hero.png',
         'icon': 'http://recompstore.com/icon.png',
         'about': {'id': 1029, 'name': 'The Legend of Zelda: Ocarina of Time', 'summary': 'A classic.', 'genres': ['Adventure'], 'rating': 93.4}},
        {'id': 'sm64', 'name': 'Super Mario 64 PC', 'origin': 'n64', 'repo': 'example/sm64', 'systems': ['linux'], 'downloads': 2000,
         'added': '2026-05-01', 'genre': ['Platformer'], 'cover': 'https://recompstore.com/img/sm64.png'},
        {'id': 'ps2tool', 'name': 'Tool', 'origin': 'ps2', 'repo': 'not a repo', 'systems': ['windows'], 'downloads': 3, 'added': '2026-08-01'},
        {'name': 'No id'}, {'id': '../evil', 'name': 'Bad id'}, {'id': 'noname'}, 'garbage', None,
        {'id': 'soh', 'name': 'Duplicate'},
    ]}


class ValidationTests(unittest.TestCase):
    def test_malformed_entries_are_ignored_and_fields_are_sanitized(self):
        apps, skipped = parse_catalog(catalog_json())
        self.assertEqual([a['id'] for a in apps], ['soh', 'sm64', 'ps2tool'])
        self.assertEqual(skipped, 6)
        soh = apps[0]
        self.assertEqual(soh['systems'], ['windows', 'linux'])
        self.assertEqual(soh['images'], {'cover': 'https://recompstore.com/img/soh-cover.png'})
        self.assertEqual(soh['files'], {'linux': {'name': 'SoH-9.0-Linux.appimage', 'sha256': SHA}, 'windows': {'name': 'x'}})  # bad checksum dropped
        self.assertNotIn('scan', soh['files']['linux'])
        self.assertIsNone(apps[2]['repo'])
        self.assertEqual(soh['about']['rating'], 93.4)

    def test_wrong_document_shape_or_version_is_rejected(self):
        for value in ([], {'catalogVersion': 1}, {'catalogVersion': 2, 'apps': [{'id': 'a', 'name': 'A'}]},
                      {'catalogVersion': 1, 'apps': [{'id': 'bad'}]}):
            with self.subTest(value=value), self.assertRaises(Invalid):
                parse_catalog(value)
        self.assertIsNone(validate_app({'id': 'a', 'name': 5}))

    def test_site_and_repository_helpers(self):
        self.assertEqual(site_url('/game/soh'), 'https://recomplauncher.com/game/soh')
        for bad in ('http://recomplauncher.com/', 'https://evil.test/', 'https://recomplauncher.com@evil.test/', '', None):
            with self.subTest(bad=bad), self.assertRaises(Invalid):
                site_url(bad)
        self.assertEqual(repository('https://github.com/a/b.git'), 'a/b')
        self.assertEqual(repository('a/b'), 'a/b')
        self.assertIsNone(repository('https://evil.test/a/b'))
        with self.assertRaises(Invalid):
            allowed_url(SOURCE)

    def test_redirects_cannot_leave_the_site(self):
        request = urllib.request.Request(SOURCE)
        with self.assertRaises(Invalid):
            SiteRedirect().redirect_request(request, None, 302, '', {}, 'https://github.com/x')
        images = urllib.request.Request('https://recompstore.com/a.png', headers={'Accept': 'image/*'})
        with self.assertRaises(urllib.error.URLError):
            SiteRedirect().redirect_request(images, None, 302, '', {}, 'https://169.254.169.254/a.png')

    def test_release_hints_need_a_unique_match_and_carry_checksum(self):
        app = validate_app(catalog_json()['apps'][0])
        hint = release_hint(app, 'linux', ['SoH-9.0-Linux.AppImage', 'notes.txt'])
        self.assertEqual(hint['asset'], 'SoH-9.0-Linux.AppImage')
        self.assertIsNone(hint['sha256'])  # the name differs in case, so the catalog checksum does not apply
        self.assertEqual(release_hint(app, 'linux', ['SoH-9.0-Linux.appimage'])['asset'], 'SoH-9.0-Linux.appimage')
        self.assertIsNone(release_hint(app, 'linux', ['SoH-1.appimage', 'SoH-2.appimage']))
        self.assertIsNone(release_hint(app, 'macos', ['SoH-1.appimage']))

    def test_manifest_hints_are_validated(self):
        result = parse_manifest(json.dumps({'start': 'bin/game', 'prepare': ['./extract', {'run': 'tool'}, 5],
                                            'folders': ['mods', '../escape', '/abs'], 'saves': {'paths': ['saves', 'C:\\x']}}))
        self.assertEqual(result, {'start': 'bin/game', 'prepare': ['./extract', 'tool'], 'folders': ['mods'], 'saves': ['saves']})
        self.assertEqual(parse_manifest('{"start": "../x"}'), {})
        for bad in ('[]', 'not json', 'x' * 300000):
            with self.subTest(bad=bad[:10]), self.assertRaises(Invalid):
                parse_manifest(bad)


class ManifestReadTests(unittest.TestCase):
    def test_manifest_is_read_at_the_release_commit_and_failures_are_quiet(self):
        class Github:
            def __init__(self, body):
                self.body, self.urls = body, []

            def open(self, url, accept):
                self.urls.append(url)
                if isinstance(self.body, Exception):
                    raise self.body
                return Response(self.body, 'text/plain')
        commit = 'a' * 40
        github = Github(b'{"start": "game", "saves": ["saves"]}')
        self.assertEqual(read_manifest(github, 'o/r', commit), {'start': 'game', 'saves': ['saves']})
        self.assertEqual(github.urls, [f'https://raw.githubusercontent.com/o/r/{commit}/recomplauncher.json'])
        for body in (b'not json', Invalid('404'), b'[]'):
            self.assertIsNone(read_manifest(Github(body), 'o/r', commit))
        self.assertIsNone(read_manifest(Github(b'{}'), 'o/r', 'main'))


class Response:
    status = 200

    def __init__(self, body, content_type='application/json', headers=None):
        import io
        from email.message import Message
        self.stream = io.BytesIO(body)
        self.headers = Message()
        self.headers['Content-Type'] = content_type
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def read(self, n=-1):
        return self.stream.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(Path(self.temp.name) / 'state')
        self.addCleanup(self.engine.close)
        self.discovery = self.engine.discovery

    def refresh(self, force=True, **kwargs):
        with patch.object(self.discovery, 'fetch', **kwargs) as fetch:
            self.discovery.start_refresh(force=force)
            if self.discovery.thread:
                self.discovery.thread.join(timeout=5)
        return fetch

    def load(self):
        self.refresh(return_value=(200, {'etag': '"v1"', 'last_modified': 'Mon, 02 Nov 2026 00:00:00 GMT'}, json.dumps(catalog_json()).encode()))

    def test_status_makes_no_requests_and_a_fresh_catalog_is_reused(self):
        with patch.object(self.discovery, 'fetch') as fetch:
            self.assertEqual(self.discovery.status()['count'], 0)
            fetch.assert_not_called()
        self.load()
        status = self.discovery.status()
        self.assertEqual((status['count'], status['skipped'], status['updated'], status['error']), (3, 6, '2026-11-03', None))
        self.assertFalse(status['stale'])
        self.refresh(force=False).assert_not_called()

    def test_refresh_sends_conditional_headers_and_keeps_data_on_304(self):
        self.load()
        self.discovery._write('recomp-catalog-status.json', {})
        fetch = self.refresh(return_value=(304, {}, b''))
        self.assertEqual(fetch.call_args.args[1], {'If-None-Match': '"v1"', 'If-Modified-Since': 'Mon, 02 Nov 2026 00:00:00 GMT'})
        self.assertEqual(self.discovery.status()['count'], 3)
        self.assertIsNone(self.discovery.status()['error'])

    def test_site_down_or_bad_document_keeps_the_last_good_catalog(self):
        self.load()
        for kwargs in ({'side_effect': Invalid('x')}, {'return_value': (200, {}, b'<html>down</html>')},
                       {'return_value': (200, {}, json.dumps({'catalogVersion': 1, 'apps': [{'bad': 1}]}).encode())}):
            self.discovery._write('recomp-catalog-status.json', {})
            self.refresh(**kwargs)
            status = self.discovery.status()
            self.assertIn('last saved copy', status['error'])
            self.assertEqual(status['count'], 3)
            self.assertEqual(self.discovery.browse({})['total'], 3)
        # Retry spacing survives a restart, so a down site is not hammered.
        with patch.object(Discovery, 'fetch') as fetch:
            Discovery(self.engine).start_refresh(force=True)
            fetch.assert_not_called()

    def test_site_down_with_no_saved_catalog_does_not_block_anything(self):
        self.refresh(side_effect=Invalid('down'))
        self.assertEqual(self.discovery.status()['count'], 0)
        self.assertEqual(self.discovery.browse({})['items'], [])
        self.assertTrue(self.engine.catalog_games()['games'])  # the reviewed catalog is unaffected
        self.assertIsNone(self.discovery.for_repository('HarbourMasters/Shipwright'))

    def test_fetch_is_bounded_json_only_and_sends_a_clear_user_agent(self):
        seen = []

        class Opener:
            def open(self, request, timeout):
                seen.append((request, timeout))
                return Response(b'{}' + b' ' * (MAX_BYTES + 1))
        self.discovery.opener = Opener()
        with self.assertRaises(Invalid):
            self.discovery.fetch(SOURCE)
        self.assertIn('Portkeeper', seen[0][0].get_header('User-agent'))
        self.assertIsNone(seen[0][0].get_header('Cookie'))
        self.assertIsNone(seen[0][0].get_header('Authorization'))
        self.assertEqual(seen[0][1], 15)

        class Html:
            def open(self, request, timeout):
                return Response(b'<html>', 'text/html')
        self.discovery.opener = Html()
        with self.assertRaises(Invalid):
            self.discovery.fetch(SOURCE)

    def test_browse_searches_filters_and_sorts(self):
        self.load()
        browse = self.discovery.browse
        self.assertEqual([i['id'] for i in browse({})['items']], ['sm64', 'soh', 'ps2tool'])
        self.assertEqual([i['name'] for i in browse({'sort': 'name'})['items']], ['Ship of Harkinian', 'Super Mario 64 PC', 'Tool'])
        self.assertEqual([i['id'] for i in browse({'sort': 'newest'})['items']], ['ps2tool', 'sm64', 'soh'])
        self.assertEqual([i['id'] for i in browse({'q': 'ocarina'})['items']], ['soh'])
        self.assertEqual([i['id'] for i in browse({'origin': 'ps2'})['items']], ['ps2tool'])
        self.assertEqual([i['id'] for i in browse({'system': 'linux'})['items']], ['sm64', 'soh'])
        self.assertEqual([i['id'] for i in browse({'genre': 'platformer'})['items']], ['sm64'])
        self.assertEqual(browse({})['origins'], ['n64', 'ps2'])
        with self.assertRaises(Invalid):
            browse({'sort': 'random'})

    def test_library_games_are_marked_and_renamed_for_display_only(self):
        self.load()
        project = self.engine.store.put('projects', {'id': 'a' * 32, 'name': 'HarbourMasters/Shipwright', 'repo': 'harbourmasters/shipwright', 'targets': ['linux']})
        browse = {i['id']: i for i in self.discovery.browse({})['items']}
        self.assertEqual(browse['soh']['in_library'], project['id'])
        self.assertIsNone(browse['sm64']['in_library'])
        meta = self.discovery.for_project(project)
        self.assertEqual(meta['display_name'], 'Ship of Harkinian')
        self.assertEqual(self.engine.store.get('projects', project['id'])['name'], 'HarbourMasters/Shipwright')
        self.assertIsNone(self.discovery.for_project({'name': 'Ship of Harkinian', 'repo': 'HarbourMasters/Shipwright'})['display_name'])
        self.assertIsNone(self.discovery.for_project({'name': 'x', 'repo': 'other/repo'}))

    def test_resolve_by_id_or_game_page(self):
        self.load()
        for payload in ({'id': 'soh'}, {'url': 'https://recomplauncher.com/game/soh'}):
            self.assertEqual(self.discovery.resolve(payload)['repositories'][0]['repository'], 'HarbourMasters/Shipwright')
        for payload in ({'id': 'ps2tool'}, {'id': 'nope'}, {'url': 'https://recomplauncher.com/about'}, {'url': 'http://localhost/game/soh'}, []):
            with self.subTest(payload=payload), self.assertRaises(Invalid):
                self.discovery.resolve(payload)

    def test_images_are_downloaded_once_into_the_cache(self):
        self.load()
        with patch.object(self.discovery, 'fetch', return_value=(200, {'content_type': 'image/png'}, PNG)) as fetch:
            self.assertEqual(self.discovery.image('soh', 'cover'), ('image/png', PNG))
            self.assertEqual(self.discovery.image('soh', 'cover'), ('image/png', PNG))
            self.assertEqual(fetch.call_count, 1)
            self.assertEqual(fetch.call_args.args[0], 'https://recompstore.com/img/soh-cover.png')
            for app_id, kind in (('soh', 'hero'), ('soh', 'bogus'), ('nope', 'cover')):
                with self.subTest(kind=kind), self.assertRaises(Invalid):
                    self.discovery.image(app_id, kind)
        self.assertTrue(any(self.discovery.root.joinpath('images').glob('*.img')))
        with patch.object(self.discovery, 'fetch', side_effect=Invalid('down')):
            self.assertEqual(self.discovery.image('soh', 'cover')[1], PNG)  # cached copy survives a site outage
            with self.assertRaises(Invalid):
                self.discovery.image('sm64', 'cover')

    def test_304_with_a_missing_saved_catalog_does_not_write_an_empty_one(self):
        self.load()
        self.discovery._write('recomp-catalog-status.json', {})
        path = self.discovery.root / 'recomp-catalog.json'
        orig = self.discovery._catalog()

        def vanish(*args, **kwargs):
            path.unlink()
            return 304, {}, b''
        with patch.object(self.discovery, '_catalog', return_value=orig):
            self.refresh(side_effect=vanish)
        self.assertFalse(path.exists())
        self.assertIn('last saved copy', self.discovery.status()['error'])

    def test_image_cache_prunes_stale_and_oversized_entries(self):
        import time
        from portkeeper import discovery as module
        self.load()
        folder = self.discovery.root / 'images'
        folder.mkdir(parents=True)
        stale = folder / ('0' * 40 + '.img')
        stale.write_bytes(b'x')
        stale.with_suffix('.json').write_text('{}')
        with patch.object(self.discovery, 'fetch', return_value=(200, {'content_type': 'image/png'}, PNG)):
            self.discovery.image('soh', 'cover')
            self.assertFalse(stale.exists())
            self.assertFalse(stale.with_suffix('.json').exists())
            old = next(folder.glob('*.img'))
            os.utime(old, (time.time() - 100, time.time() - 100))
            with patch.object(module, 'MAX_IMAGE_CACHE', len(PNG) + 1):
                self.discovery.image('sm64', 'cover')
        self.assertEqual(len(list(folder.glob('*.img'))), 1)
        self.assertFalse(old.exists())

    def test_concurrent_requests_for_one_image_download_it_once(self):
        import threading
        import time
        self.load()
        calls = []

        def slow(*args, **kwargs):
            calls.append(1)
            time.sleep(0.2)
            return 200, {'content_type': 'image/png'}, PNG
        with patch.object(self.discovery, 'fetch', side_effect=slow):
            threads = [threading.Thread(target=self.discovery.image, args=('soh', 'cover')) for _ in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(calls), 1)

    def test_romm_ownership_is_best_effort_and_selects_nothing(self):
        self.load()
        romm = self.engine.romm
        found = {'items': [{'id': 7, 'name': 'Legend of Zelda, The: Ocarina of Time', 'platform': 'N64', 'platform_slug': 'n64'},
                           {'id': 8, 'name': 'The Legend of Zelda: Ocarina of Time', 'platform': 'GC', 'platform_slug': 'ngc'}]}
        with patch.object(romm, 'public_config', return_value={'configured': False}), patch.object(romm, 'search') as search:
            self.assertFalse(self.discovery.romm_match('soh')['checked'])
            search.assert_not_called()
        with patch.object(romm, 'public_config', return_value={'configured': True}):
            with patch.object(romm, 'search', return_value=found):
                self.assertFalse(self.discovery.romm_match('soh')['owned'])  # "Legend of Zelda, The" is not an exact title match
                found['items'][0]['name'] = 'The Legend of Zelda: Ocarina of Time (USA)'
                found['items'][1]['platform_slug'] = 'n64'
                result = self.discovery.romm_match('soh')
                self.assertTrue(result['owned'])
                self.assertEqual(set(result), {'checked', 'owned', 'title', 'matches'})
            with patch.object(romm, 'search', side_effect=Invalid('offline')):
                self.assertEqual(self.discovery.romm_match('soh')['checked'], False)
            self.assertFalse(self.discovery.romm_match('ps2tool')['checked'])  # no usable title/console mapping

    def test_github_lookup_adds_release_file_hints(self):
        self.load()
        hints = self.discovery.hints('harbourmasters/shipwright', ['SoH-9.0-Linux.appimage', 'SoH-9.0-Win.zip'])
        self.assertEqual({h['system']: h['asset'] for h in hints['files']}, {'windows': 'SoH-9.0-Win.zip', 'linux': 'SoH-9.0-Linux.appimage'})
        self.assertEqual({h['system']: h['sha256'] for h in hints['files']}['linux'], SHA)
        self.assertIsNone(self.discovery.hints('other/repo', []))


if __name__ == '__main__':
    unittest.main()
