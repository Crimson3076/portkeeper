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
