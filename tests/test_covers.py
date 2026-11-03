import io
import tempfile
import unittest
from email.message import Message
from unittest.mock import patch

from portkeeper.romm import RomM
from portkeeper.safety import Invalid


class CoverTests(unittest.TestCase):
    def test_custom_cover_uses_same_origin_credentials_and_short_cache(self):
        with tempfile.TemporaryDirectory() as d:
            romm = RomM(d)
            body = b'\x89PNG\r\n\x1a\ncover'
            response = io.BytesIO(body)
            response.headers = Message(); response.headers['Content-Type'] = 'image/png'
            with patch.object(romm, '_config', return_value={'url':'http://localhost:8091','token':'private-token'}), patch.object(romm, '_json', return_value={'path_cover_large':'/assets/custom.png?ts=2026-10-11 19:27:18'}), patch('portkeeper.romm.urllib.request.build_opener') as factory:
                opener=factory.return_value; opener.open.return_value=response
                self.assertEqual(romm.cover(10), ('image/png',body))
                self.assertEqual(romm.cover(10), ('image/png',body))
                self.assertEqual(opener.open.call_count, 1)
                request=opener.open.call_args.args[0]
                self.assertEqual(request.full_url, 'http://localhost:8091/assets/custom.png?ts=2026-10-11%2019:27:18')
                self.assertEqual(request.get_header('Authorization'), 'Bearer private-token')

    def test_missing_external_and_nonimage_covers_are_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            romm=RomM(d)
            with patch.object(romm, '_config', return_value={'url':'http://localhost:8091','token':'private-token'}):
                for record in [{}, {'path_cover_large':'https://other.example/cover.png'}]:
                    with patch.object(romm, '_json', return_value=record), patch('portkeeper.romm.urllib.request.build_opener') as factory:
                        with self.assertRaises(Invalid): romm.cover(10)
                        factory.assert_not_called()
                response=io.BytesIO(b'<svg></svg>');response.headers=Message();response.headers['Content-Type']='image/svg+xml'
                with patch.object(romm, '_json', return_value={'path_cover_large':'/assets/cover.png'}), patch('portkeeper.romm.urllib.request.build_opener') as factory:
                    factory.return_value.open.return_value=response
                    with self.assertRaises(Invalid): romm.cover(10)


def _png_response(body=b'\x89PNG\r\n\x1a\nart'):
    response = io.BytesIO(body)
    response.headers = Message(); response.headers['Content-Type'] = 'image/png'
    return response


class MatchedCoverTests(unittest.TestCase):
    KEY = 'a' * 32

    def entries(self):
        return {'items': [
            {'id': 5, 'name': 'Perfect Dark Remix', 'platform_slug': 'n64', 'path_cover_large': '/assets/other.png'},
            {'id': 6, 'name': 'Perfect Dark', 'platform_slug': 'n64', 'path_cover_large': '/assets/n64.png'},
            {'id': 7, 'name': 'perfect dark', 'platform_slug': 'win', 'path_cover_large': '/assets/win.png'}], 'total': 3}

    def romm(self, directory):
        romm = RomM(directory)
        patcher = patch.object(romm, '_config', return_value={'url': 'http://localhost:8091', 'token': 't'})
        patcher.start(); self.addCleanup(patcher.stop)
        return romm

    def test_matches_exact_title_prefers_project_platform_and_caches_on_disk(self):
        with tempfile.TemporaryDirectory() as d:
            romm = self.romm(d)
            with patch.object(romm, '_json', side_effect=lambda path: self.entries() if path.startswith('roms?') else {'path_cover_large': '/assets/win.png'}), patch('portkeeper.romm.urllib.request.build_opener') as factory:
                factory.return_value.open.return_value = _png_response()
                content_type, body, rom_id = romm.matched_cover(self.KEY, 'Perfect Dark Windows', ['win'])
                self.assertEqual((content_type, rom_id), ('image/png', 7))
            # A new instance (page reload / restart) serves from disk without contacting RomM.
            fresh = self.romm(d)
            with patch.object(fresh, '_json', side_effect=AssertionError('RomM must not be contacted')):
                self.assertEqual(fresh.matched_cover(self.KEY, 'Perfect Dark Windows', ['win']), (content_type, body, 7))

    def test_no_match_is_remembered_and_raises(self):
        with tempfile.TemporaryDirectory() as d:
            romm = self.romm(d)
            with patch.object(romm, '_json', return_value={'items': [], 'total': 0}) as fetch:
                with self.assertRaises(Invalid): romm.matched_cover(self.KEY, 'Unknown Game', ['linux'])
                with self.assertRaises(Invalid): romm.matched_cover(self.KEY, 'Unknown Game', ['linux'])
                self.assertEqual(fetch.call_count, 1)

    def test_unreachable_romm_is_not_remembered_as_a_miss(self):
        with tempfile.TemporaryDirectory() as d:
            romm = self.romm(d)
            with patch.object(romm, '_json', side_effect=Invalid('RomM could not be reached')):
                with self.assertRaises(Invalid): romm.matched_cover(self.KEY, 'Perfect Dark', ['linux'])
            with patch.object(romm, '_json', side_effect=lambda path: self.entries() if path.startswith('roms?') else {'path_cover_large': '/assets/n64.png'}), patch('portkeeper.romm.urllib.request.build_opener') as factory:
                factory.return_value.open.return_value = _png_response()
                self.assertEqual(romm.matched_cover(self.KEY, 'Perfect Dark', ['linux'])[2], 6)

    def test_unreachable_cover_download_is_not_remembered_as_a_miss(self):
        with tempfile.TemporaryDirectory() as d:
            romm = self.romm(d)
            def fetch(path):
                if path.startswith('roms?'): return self.entries()
                raise Invalid('RomM could not be reached')
            with patch.object(romm, '_json', side_effect=fetch):
                with self.assertRaises(Invalid): romm.matched_cover(self.KEY, 'Perfect Dark', ['n64'])
            self.assertEqual(list((romm.cache / 'covers').glob('*.json')), [])
