"""Exercise the updater with local releases and fake service/network commands."""
import io
import os
from pathlib import Path
import subprocess
import tarfile
import tempfile
import unittest


class UpdateTests(unittest.TestCase):
    def test_release_success_test_failure_rollback_and_rejected_release(self):
        script = Path(__file__).resolve().parents[1] / 'deploy/broker/portkeeper-broker-update.sh'
        for scenario in ('success', 'tests-fail', 'not-ready', 'restart-fail', 'rejected'):
            with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                base, commands = root / 'install', root / 'bin'
                commands.mkdir()
                previous = base / 'releases/old'
                previous.mkdir(parents=True)
                (base / 'current').symlink_to(previous)
                archive = root / 'release.tar.gz'
                files = {
                    'release/portkeeper/__init__.py': '',
                    'release/portkeeper/broker_net.py': 'PROTOCOL = 2\n',
                    'release/tests/test_release.py': 'import unittest\nclass ReleaseTest(unittest.TestCase):\n def test_release(self): self.assertTrue(' + str(scenario != 'tests-fail') + ')\n',
                }
                with tarfile.open(archive, 'w:gz') as bundle:
                    for name, value in files.items():
                        data = value.encode()
                        info = tarfile.TarInfo(name)
                        info.size = len(data)
                        bundle.addfile(info, io.BytesIO(data))
                scripts = {
                    'curl': '''#!/usr/bin/env python3
import json, os, shutil, sys
args = sys.argv[1:]
if '-o' in args:
 shutil.copyfile(os.environ['TEST_ARCHIVE'], args[args.index('-o')+1])
elif args[-1].endswith('/v1/health'):
 print(json.dumps({'ready': os.environ['TEST_SCENARIO'] != 'not-ready'}))
else:
 print('{"tag_name":"new"}')
''',
                    'systemctl': '#!/bin/sh\necho "$*" >> "$TEST_RESTARTS"\nif [ "$TEST_SCENARIO" = restart-fail ] && [ "$(wc -l < "$TEST_RESTARTS")" -eq 1 ]; then exit 1; fi\n',
                    'sleep': '#!/bin/sh\nexit 0\n',
                }
                for name, content in scripts.items():
                    target = commands / name
                    target.write_text(content)
                    target.chmod(0o755)
                if scenario == 'rejected':
                    (base / 'rejected-new').touch()
                restarts = root / 'restarts'
                env = {**os.environ, 'PATH': str(commands) + ':' + os.environ['PATH'],
                       'PORTKEEPER_INSTALL_DIR': str(base), 'TEST_ARCHIVE': str(archive),
                       'TEST_SCENARIO': scenario, 'TEST_RESTARTS': str(restarts)}
                result = subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 1 if scenario in ('tests-fail', 'not-ready', 'restart-fail') else 0, result.stdout + result.stderr)
                self.assertEqual((base / 'current').resolve().name, 'new' if scenario == 'success' else 'old')
                self.assertEqual((base / 'rejected-new').exists(), scenario != 'success')
                calls = restarts.read_text().splitlines() if restarts.exists() else []
                self.assertEqual(len(calls), {'success': 1, 'not-ready': 2, 'restart-fail': 2}.get(scenario, 0))
                self.assertFalse(list((base / 'releases').glob('.incoming-*')))
                if scenario in ('tests-fail', 'not-ready', 'restart-fail'):
                    again = subprocess.run(['bash', str(script)], env=env, capture_output=True, text=True, timeout=30)
                    self.assertEqual(again.returncode, 0)
                    self.assertIn('previously failed', again.stdout)
