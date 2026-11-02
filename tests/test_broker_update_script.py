"""Behaviour of the broker self-update script with stubbed curl, systemctl, python3 and sleep."""
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / 'tools' / 'broker-vm' / 'portkeeper-broker-update.sh'


@unittest.skipUnless(shutil.which('bash') and shutil.which('flock') and shutil.which('timeout'), 'needs bash, flock, timeout')
class UpdateScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.base, self.bin = root / 'home', root / 'bin'
        self.bin.mkdir()
        release = root / 'src' / 'rel'
        (release / 'portkeeper').mkdir(parents=True)
        (release / 'portkeeper' / '__init__.py').write_text('__version__ = "9.9.9"\n')
        self.tarball = root / 'release.tar.gz'
        with tarfile.open(self.tarball, 'w:gz') as bundle:
            bundle.add(release, arcname='rel')
        self.write('curl', f'#!/bin/sh\ncase "$*" in\n*api.github.com*) echo \'{{"tag_name": "v9"}}\';;\n*) cat "{self.tarball}";;\nesac\n')
        self.write('sleep', '#!/bin/sh\nexit 0\n')
        self.write('systemctl', '#!/bin/sh\necho "$@" >> "$STUB_LOG"\n[ "$STUB_RESTART_FAILS" = 1 ] && [ ! -e "$STUB_LOG.rolled" ] && { touch "$STUB_LOG.rolled"; exit 1; }\nexit 0\n')
        self.write('python3', f'''#!/bin/sh
case "$1" in
-c) exec "{sys.executable}" "$@";;
-m) case "$2" in
  unittest) [ "$STUB_TESTS_FAIL" = 1 ] && exit 1; exit 0;;
  portkeeper.broker_net) case "$*" in *--idle*) exit "${{STUB_IDLE:-0}}";; *) [ "$STUB_READY" = 1 ]; exit $?;; esac;;
esac;;
esac
exit 0
''')
        self.log = root / 'systemctl.log'

    def write(self, name, text):
        path = self.bin / name
        path.write_text(text)
        path.chmod(0o755)

    def run_script(self, **env):
        values = {'PATH': f'{self.bin}:{os.environ["PATH"]}', 'PORTKEEPER_BROKER_HOME': str(self.base), 'STUB_LOG': str(self.log),
                  'STUB_READY': '1', 'STUB_TESTS_FAIL': '0', 'STUB_RESTART_FAILS': '0', 'HOME': str(self.base)}
        values.update(env)
        return subprocess.run(['bash', str(SCRIPT)], env=values, capture_output=True, text=True, timeout=60)

    def old_release(self):
        (self.base / 'releases' / 'old').mkdir(parents=True)
        (self.base / 'current').symlink_to('releases/old')

    def test_successful_install(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.base / 'installed').read_text().strip(), 'v9')
        self.assertEqual(os.readlink(self.base / 'current'), 'releases/v9')

    def test_failed_tests_reject_the_tag_and_keep_the_current_release(self):
        self.old_release()
        result = self.run_script(STUB_TESTS_FAIL='1')
        self.assertEqual(result.returncode, 1)
        self.assertEqual((self.base / 'rejected').read_text().strip(), 'v9')
        self.assertEqual(os.readlink(self.base / 'current'), 'releases/old')
        self.assertIn('rejected earlier', self.run_script().stdout)

    def test_busy_broker_defers_the_update(self):
        self.old_release()
        result = self.run_script(STUB_IDLE='3')
        self.assertEqual(result.returncode, 0)
        self.assertFalse((self.base / 'releases' / 'v9').exists())
        self.assertFalse(self.log.exists())

    def test_not_ready_rolls_back(self):
        self.old_release()
        result = self.run_script(STUB_READY='0')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(os.readlink(self.base / 'current'), 'releases/old')
        self.assertEqual((self.base / 'rejected').read_text().strip(), 'v9')
        self.assertFalse((self.base / 'installed').exists())
        self.assertEqual(self.log.read_text().count('restart'), 2)

    def test_failed_restart_is_rejected_and_rolled_back(self):
        self.old_release()
        result = self.run_script(STUB_RESTART_FAILS='1')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(os.readlink(self.base / 'current'), 'releases/old')
        self.assertEqual((self.base / 'rejected').read_text().strip(), 'v9')

    def test_old_releases_are_pruned_but_the_active_one_is_kept(self):
        self.old_release()
        for index, name in enumerate(['a', 'b', 'c']):
            directory = self.base / 'releases' / name
            directory.mkdir()
            os.utime(directory, (1000 + index, 1000 + index))
        os.utime(self.base / 'releases' / 'old', (1, 1))
        self.assertEqual(self.run_script().returncode, 0)
        names = {path.name for path in (self.base / 'releases').iterdir()}
        self.assertIn('v9', names)
        self.assertLessEqual(len(names), 3)
        self.assertNotIn('old', names)


if __name__ == '__main__':
    unittest.main()
