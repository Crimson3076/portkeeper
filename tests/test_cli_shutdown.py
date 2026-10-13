import io
import os
import signal
import unittest
from unittest.mock import patch

from portkeeper.__main__ import main


class ContainerShutdownTests(unittest.TestCase):
    def test_sigterm_closes_engine_and_restores_signal_handler(self):
        previous = signal.getsignal(signal.SIGTERM)
        with patch('sys.argv', ['portkeeper']), patch('sys.stdout', io.StringIO()), \
             patch('portkeeper.__main__.Engine') as engine, patch('portkeeper.__main__.make_server') as server:
            server.return_value.serve_forever.side_effect = lambda: os.kill(os.getpid(), signal.SIGTERM)
            main()
            engine.return_value.close.assert_called_once()
            server.return_value.server_close.assert_called_once()
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)
