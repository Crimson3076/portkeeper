import io
import tempfile
import threading
import unittest
import urllib.request
import subprocess
import sys
from email.message import Message
from pathlib import Path
from unittest.mock import patch

from portkeeper.discovery import (Discovery, MAX_BYTES, Page, SOURCE, SiteRedirect,
                                  parse_index, repository, site_url)
from portkeeper.engine import Engine, allowed_url
from portkeeper.safety import Invalid


# Contract fixtures, not snapshots of the live site. See docs/DISCOVERY.md.
INDEX = '''<!doctype html><html><body>
<nav><a href="/about">About</a><a href="https://github.com/site/frontend">GitHub</a></nav>
<main><article class="game-card"><a href="/games/zelda"><h2>Zelda &amp; friends</h2></a>
<a href="https://github.com/example/Zelda/releases">Source</a></article>
<article><a href="/games/mario"><h3>Mario port</h3></a></article>
<a href="https://github.com/example/Other.git">Other port</a>
<a href="javascript:alert(1)">Ignore this</a>
<a href="https://evil.test/games/unsafe"><h2>Not on this site</h2></a>
</main><footer><a href="https://github.com/site/frontend">Website source</a></footer>
<script>window.location='https://evil.test'</script></body></html>'''
DETAIL = '''<main><h1>Mario port</h1><a href="https://github.com/example/mario">Project source</a>
<a href="https://github.com/EXAMPLE/MARIO/releases">Releases</a></main>
<footer><a href="https://github.com/site/frontend">Site source</a></footer>'''


class ParserTests(unittest.TestCase):
    def test_index_uses_explicit_links_entities_and_deduplicates_repository(self):
        entries = parse_index(INDEX)
        self.assertEqual([e['name'] for e in entries], ['Zelda & friends', 'Mario port', 'Other port'])
        self.assertEqual(entries[0]['repository'], 'example/Zelda')
        self.assertEqual(entries[0]['url'], 'https://recomplauncher.com/games/zelda')
        self.assertIsNone(entries[1]['repository'])
        self.assertEqual(entries[2]['repository'], 'example/Other')
        self.assertTrue(all(set(e) == {'id', 'name', 'url', 'repository'} for e in entries))

    def test_card_link_with_nested_markup_and_no_specific_route(self):
        entries = parse_index('<div class><div class="project-card"><a href="/zelda"><h3>Zelda</h3></a></div></div>')
        self.assertEqual(entries[0]['name'], 'Zelda')
        self.assertEqual(parse_index('<div class><a href="/games/zelda"><h3>Zelda</h3></a></div>')[0]['name'], 'Zelda')

    def test_multiple_repositories_on_a_card_are_not_silently_selected(self):
        html = '<article><a href="https://github.com/example/tool">Extractor</a><h3>Port</h3><a href="/games/port">Details</a><a href="https://github.com/example/port">Source</a></article>'
        entries = parse_index(html)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]['url'], 'https://recomplauncher.com/games/port')
        self.assertIsNone(entries[0]['repository'])

    def test_empty_or_script_only_index_fails_instead_of_erasing_cache(self):
        for html in ('<html>Access denied</html>', '<script><a href="/games/fake">Fake</a></script>',
                     '<nav><a href="/games/x">Navigation</a></nav>'):
            with self.subTest(html=html), self.assertRaises(Invalid):
                parse_index(html)

    def test_html_depth_and_entry_count_are_bounded(self):
        with self.assertRaisesRegex(Invalid, 'complexity'):
            Page('<div>' * 110)
        entries = parse_index(''.join(f'<article><a href="/games/g{i}"><h2>Game {i}</h2></a></article>' for i in range(150)))
        self.assertEqual(len(entries), 100)

    def test_only_fixed_public_https_site_urls_are_accepted(self):
        self.assertEqual(site_url('/games/zelda?sort=1#details'), 'https://recomplauncher.com/games/zelda')
        self.assertEqual(site_url('https://www.recomplauncher.com/games/zelda'), 'https://recomplauncher.com/games/zelda')
        for url in (None, '', 'http://recomplauncher.com/top', 'https://localhost/top',
                    'https://127.0.0.1/top', 'https://recomplauncher.com.evil.test/top',
                    'https://user:secret@recomplauncher.com/top', 'https://recomplauncher.com:444/top',
                    'https://recomplauncher.com/%2e%2e/secrets', 'https://recomplauncher.com/\\evil.test',
                    'https://recomplauncher.com/\nsecret'):
            with self.subTest(url=url), self.assertRaises(Invalid):
                site_url(url)

    def test_repository_links_are_normalized_without_inference(self):
        self.assertEqual(repository('https://github.com/Owner/Port.git/releases/tag/v1'), 'Owner/Port')
        for url in ('https://github.com/owner', 'https://github.com/topics/recompilation',
                    'https://user:pass@github.com/owner/port', 'http://github.com/owner/port',
                    'https://github.com.evil.test/owner/port', 'https://github.com/../bad', None):
            self.assertIsNone(repository(url), url)
        self.assertIsNone(repository('https://github.com/owner/.git'))

    def test_disposable_cli_rejects_arbitrary_hosts_without_opening_app_state(self):
        result = subprocess.run([sys.executable, '-m', 'portkeeper.discovery', '--page', 'http://localhost/private'],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 1)
        self.assertIn('Only HTTPS pages', result.stderr)
        self.assertNotIn('Traceback', result.stderr)

    def test_redirect_cannot_fetch_github_or_private_hosts(self):
        request = urllib.request.Request(SOURCE)
        for url in ('http://recomplauncher.com/top', 'http://localhost/secret',
                    'https://github.com/example/port', 'https://evil.test/'):
            with self.subTest(url=url), self.assertRaises(Invalid):
                SiteRedirect().redirect_request(request, None, 302, '', {}, url)
        # This integration must not broaden the artifact download boundary.
        with self.assertRaises(Invalid):
            allowed_url(SOURCE)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(Path(self.temp.name) / 'state')
        self.addCleanup(self.engine.close)
        self.discovery = self.engine.discovery

    def refresh(self, html=INDEX):
        with patch.object(self.discovery, 'fetch', return_value=html):
            result = self.discovery.start_refresh(force=True)
            self.discovery.thread.join(timeout=5)
            self.assertFalse(self.discovery.thread.is_alive())
        return result, self.discovery.status()

    def test_status_never_makes_network_requests_and_fresh_list_is_reused(self):
        with patch.object(self.discovery, 'fetch') as fetch:
            self.assertEqual(self.discovery.status()['entries'], [])
            fetch.assert_not_called()
        _, saved = self.refresh()
        self.assertEqual(len(saved['entries']), 3)
        self.assertFalse(saved['stale'])
        with patch.object(self.discovery, 'fetch') as fetch:
            self.discovery.start_refresh()
            fetch.assert_not_called()
        self.assertLess((self.discovery.root / 'recomp-launcher.json').stat().st_size, 4096)

    def test_failed_or_changed_site_keeps_last_list_and_retry_is_bounded(self):
        self.refresh()
        previous = self.discovery.status()['entries']
        self.discovery._write('recomp-launcher-status.json', {})
        self.refresh('<html>No cards</html>')
        status = self.discovery.status()
        self.assertEqual(status['entries'], previous)
        self.assertIn('last saved list', status['error'])
        with patch.object(self.discovery, 'fetch') as fetch:
            self.discovery.start_refresh(force=True)
            fetch.assert_not_called()
        replacement = Discovery(self.engine)
        with patch.object(replacement, 'fetch') as fetch:
            replacement.start_refresh()
            fetch.assert_not_called()

    def test_parallel_refreshes_share_one_background_request(self):
        entered, release = threading.Event(), threading.Event()
        def fetch(url):
            entered.set()
            release.wait(timeout=5)
            return INDEX
        with patch.object(self.discovery, 'fetch', side_effect=fetch) as fetch:
            self.discovery.start_refresh()
            self.assertTrue(entered.wait(timeout=3))
            self.assertTrue(self.discovery.status()['refreshing'])
            for _ in range(5):
                self.discovery.start_refresh(force=True)
            release.set()
            self.discovery.thread.join(timeout=5)
            self.assertEqual(fetch.call_count, 1)

    def test_resolve_keeps_ambiguity_visible_and_filters_site_navigation(self):
        with patch.object(self.discovery, 'fetch', return_value=DETAIL):
            result = self.discovery.resolve({'url': 'https://recomplauncher.com/games/mario'})
        self.assertEqual(result['repositories'], [{'repository': 'example/mario', 'url': 'https://github.com/example/mario'}])
        with patch.object(self.discovery, 'fetch', return_value=DETAIL + '<article><a href="https://github.com/example/tool">Extractor</a></article>'):
            result = self.discovery.resolve({'url': '/games/mario'})
        self.assertEqual(len(result['repositories']), 2)
        self.assertEqual(self.engine.store.all('projects'), [])
        with patch.object(self.discovery, 'fetch', return_value='<p>No upstream</p>'), self.assertRaisesRegex(Invalid, 'no readable GitHub'):
            self.discovery.resolve({'url': '/games/mario'})

    def test_untrusted_cache_urls_are_not_returned(self):
        self.discovery._write('recomp-launcher.json', {'fetched_at': 'broken', 'entries': [
            {'name': 'Bad', 'url': 'javascript:alert(1)'},
            {'name': 'Bad too', 'url': 'https://evil.test/game'},
            {'name': '<img src=x onerror=alert(1)>', 'url': '/games/safe'},
        ]})
        status = self.discovery.status()
        self.assertEqual(len(status['entries']), 1)
        self.assertEqual(status['entries'][0]['url'], 'https://recomplauncher.com/games/safe')
        self.assertTrue(status['stale'])

    def test_activity_log_retains_only_last_thirty_public_records(self):
        for _ in range(35):
            self.discovery._event('refresh_succeeded', entries=3)
        record = self.discovery._read('recomp-launcher-events.json')
        self.assertEqual(len(record['events']), 30)
        self.assertTrue(all(set(event) == {'at', 'kind', 'entries'} for event in record['events']))

    def test_fetch_is_bounded_html_only_and_sends_no_private_headers(self):
        class Response(io.BytesIO):
            pass
        def response(body=INDEX.encode(), content_type='text/html; charset=utf-8'):
            result = Response(body)
            result.headers = Message(); result.headers['Content-Type'] = content_type
            return result
        with patch.object(self.discovery.opener, 'open', return_value=response()) as opener:
            self.assertIn('Zelda', self.discovery.fetch(SOURCE))
            request = opener.call_args.args[0]
            self.assertEqual(set(k.lower() for k in request.headers), {'user-agent', 'accept'})
            self.assertEqual(opener.call_args.kwargs['timeout'], 15)
        with patch.object(self.discovery.opener, 'open', return_value=response(b'x' * (MAX_BYTES + 1))), self.assertRaises(Invalid):
            self.discovery.fetch(SOURCE)
        with patch.object(self.discovery.opener, 'open', return_value=response(b'{}', 'application/json')), self.assertRaises(Invalid):
            self.discovery.fetch(SOURCE)

    def test_source_attribution_is_small_validated_metadata_only(self):
        source = {'kind': 'recomp_launcher', 'url': 'https://recomplauncher.com/games/mario?tracking=1'}
        project = self.engine.create_project({'name': 'Mario', 'repo': 'example/mario', 'targets': ['windows'], 'discovery_source': source})
        self.assertEqual(project['discovery_source']['url'], 'https://recomplauncher.com/games/mario')
        self.assertEqual(project['status'], 'idle')
        self.assertFalse(project['auto_build'])
        self.assertEqual(self.engine.store.all('jobs'), [])
        self.assertEqual(self.engine.update_project(project['id'], {'name': 'My Mario'})['discovery_source'], project['discovery_source'])
        for url in ('https://evil.test/game', 'file:///inputs/owned.gba'):
            with self.assertRaises(Invalid):
                self.engine.create_project({'name': 'Mario', 'repo': 'example/mario', 'targets': ['windows'],
                                            'discovery_source': {'kind': 'recomp_launcher', 'url': url}})
