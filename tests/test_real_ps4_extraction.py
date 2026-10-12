"""Optional integration check: real pinned PkgTool, entirely synthetic PS4 packages.

Run inside the Portkeeper image with PORTKEEPER_TEST_PKGTOOL_ZIP pointing at
the checksummed tool archive. No game assets or game executables are used.
"""
import copy
import hashlib
import json
import os
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from portkeeper import packaging, ps4
from portkeeper.engine import Engine
from portkeeper.runner import COMPAT_SSL, Context
from portkeeper.safety import checksum, safe_extract
from test_ps4 import make_sfo


@unittest.skipUnless(os.environ.get('PORTKEEPER_TEST_PKGTOOL_ZIP'), 'Optional real PkgTool integration requires the pinned archive and container libraries')
class RealPkgToolTests(unittest.TestCase):
    def test_real_base_update_extraction_preserves_originals_and_reuses_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            definition = copy.deepcopy(packaging.bundled()['gow3_pc'])
            operation = definition['operations'][0]
            operation['accepted_eboot_sha256'] = [hashlib.sha256(b'updated-eboot').hexdigest()]
            tool_download = definition['downloads']['pkgtool']
            tool_zip = Path(os.environ['PORTKEEPER_TEST_PKGTOOL_ZIP'])
            self.assertEqual(checksum(tool_zip), tool_download['sha256'])
            self.assertTrue((Path(COMPAT_SSL) / 'libssl.so.1.1').is_file())
            safe_extract(tool_zip, root / 'tool')
            binary = root / 'tool/PkgTool.Core';binary.chmod(0o755)
            engine = Engine(root / 'data', input_root=root / 'roms')
            try:
                env = {'LD_LIBRARY_PATH': COMPAT_SSL, 'DOTNET_SYSTEM_GLOBALIZATION_INVARIANT': '1',
                       'DOTNET_BUNDLE_EXTRACT_BASE_DIR': str(root / 'bundle')}
                originals = []
                for role, version in [('base', '01.00'), ('update', '01.02')]:
                    data = root / role
                    (data / 'sce_sys').mkdir(parents=True)
                    (data / 'sce_module').mkdir()
                    (data / 'sce_sys/param.sfo').write_bytes(make_sfo(version=version, category='gp' if role == 'update' else 'gd'))
                    (data / 'eboot.bin').write_bytes(b'updated-eboot' if role == 'update' else b'base-eboot')
                    (data / 'sce_module/libc.prx').write_bytes(b'synthetic fixture module')
                    (data / f'{role}-only.dat').write_bytes(b'synthetic fixture data')
                    project = ET.Element('psproject', fmt='gp4', version='1000')
                    volume = ET.SubElement(project, 'volume')
                    ET.SubElement(volume, 'volume_type').text = 'pkg_ps4_patch' if role == 'update' else 'pkg_ps4_app'
                    ET.SubElement(volume, 'volume_ts').text = '2026-10-12 00:00:00'
                    ET.SubElement(volume, 'package', content_id='UP0000-CUSA01623_00-PORTKEEPERTEST00', passcode='0' * 32)
                    files = ET.SubElement(project, 'files', img_no='0')
                    for path in sorted(data.rglob('*')):
                        if path.is_file():
                            relative = path.relative_to(data).as_posix()
                            ET.SubElement(files, 'file', targ_path=relative, orig_path=relative)
                    tree = ET.SubElement(project, 'rootdir')
                    for name in ['sce_sys', 'sce_module']:
                        ET.SubElement(tree, 'dir', targ_name=name)
                    ET.ElementTree(project).write(data / 'Project.gp4', encoding='utf-8', xml_declaration=True)
                    output = root / f'{role}-pkg';output.mkdir()
                    engine.packager.run([str(binary), 'pkg_build', str(data / 'Project.gp4'), str(output)], root, env, root / f'{role}.log', 120)
                    package = next(output.glob('*.pkg'))
                    self.assertEqual(ps4.pkg_metadata(package)['role'], role)
                    destination = engine.input_root / f'ps4/roms/{role}.pkg'
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(package, destination)
                    originals.append((destination, checksum(destination)))
                dependency = engine.packager.cache / tool_download['sha256'] / tool_download['asset']
                dependency.parent.mkdir(parents=True, exist_ok=True);shutil.copyfile(tool_zip, dependency)
                project = engine.create_project({'name': 'Synthetic fixture', 'repo': 'FelipePrado0/gow3_pc', 'targets': ['windows'], 'packaging': definition})
                project = engine.packager.bind_inputs(project, definition)
                for index in range(2):
                    workspace = root / f'work-{index}';workspace.mkdir()
                    ctx = Context(engine.packager, project, definition, {}, workspace, lambda _: None, lambda *_: None)
                    engine.packager._ps4(ctx, operation, ctx.shared / 'game', 120)
                    game = ctx.aliases['game']
                    self.assertEqual((game / 'eboot.bin').read_bytes(), b'updated-eboot')
                    self.assertTrue((game / 'base-only.dat').is_file())
                    self.assertTrue((game / 'update-only.dat').is_file())
                    self.assertEqual(ps4.sfo_values((game / 'sce_sys/param.sfo').read_bytes())['APP_VER'], '01.02')
                    self.assertEqual([checksum(path) for path, _ in originals], [digest for _, digest in originals])
                    if index:
                        self.assertEqual(ctx.provenance['tools'][0]['reused'], 'earlier extraction')
                manifest = json.loads((game.parent / 'portkeeper-extraction.json').read_text())
                self.assertEqual([item['role'] for item in manifest['pkg_metadata']], ['base', 'update'])
            finally:
                engine.close()
