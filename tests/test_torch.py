import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from portkeeper import packaging
from portkeeper.runner import OperationRunner
from portkeeper.safety import Invalid


class TorchTests(unittest.TestCase):
    def test_catalog_requires_rom_and_game_archive_in_both_packages(self):
        definition = json.loads((Path(__file__).parents[1] / 'catalog/definitions/starship.json').read_text())
        packaging.validate_definition(definition)
        self.assertTrue(definition['inputs'])
        for target in ('linux', 'windows'):
            self.assertIn('sf64.o2r', definition['outputs'][target]['expected_files'])
        launcher = next(op for op in definition['operations'] if op.get('path') == 'Launch.sh')
        self.assertEqual(launcher['env']['SHIP_HOME'], '$PWD')

    def exercise(self, names, minimum=2):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        rom = root / 'owned.z64'
        rom.write_bytes(b'owned fixture')
        binary = root / 'torch'
        binary.write_bytes(b'fixture tool')
        runner = OperationRunner(SimpleNamespace(data=root, cache_root=root / 'cache', prepared_root=root / 'prepared'))
        ctx = SimpleNamespace(workspace=root, path=lambda _: rom, stage=lambda *a: None,
                              log=lambda *a: None, provenance={'tools': []})
        def unpack(ctx, key, work):
            work.mkdir()
            (work / 'config.yml').write_text('fixture')
            (work / 'assets').mkdir()
        def run(args, work, env, log, timeout):
            self.assertEqual(args[1], 'o2r')
            self.assertNotEqual(Path(args[2]), rom)
            self.assertEqual(Path(args[2]).read_bytes(), rom.read_bytes())
            with zipfile.ZipFile(work / 'sf64.o2r', 'w') as archive:
                for name in names:
                    archive.writestr(name, b'fixture')
        destination = root / 'shared/sf64.o2r'
        with patch.object(runner, 'torch_binary', return_value=binary), patch.object(runner, 'unpack', side_effect=unpack), patch.object(runner, 'run', side_effect=run), patch.object(runner, 'keep_log'):
            runner._torch_o2r(ctx, {'download': 'linux', 'rom': '$shared/rom', 'output': 'sf64.o2r', 'min_entries': minimum}, destination, 60)
        self.assertEqual(rom.read_bytes(), b'owned fixture')
        return destination, ctx

    def test_private_extraction_records_checked_archive(self):
        destination, ctx = self.exercise(['assets/a', 'assets/b'])
        self.assertTrue(destination.is_file())
        self.assertEqual(ctx.provenance['tools'][0]['entries'], 2)

    def test_incomplete_archive_is_rejected(self):
        with self.assertRaisesRegex(Invalid, 'complete asset archive'):
            self.exercise(['assets/a'])

    def test_unsafe_archive_member_is_rejected(self):
        with self.assertRaises(Invalid):
            self.exercise(['assets/a', '../escape'])
