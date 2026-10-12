import copy
import hashlib
import json
import os
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from portkeeper import packaging, ps4
from portkeeper.engine import Engine
from portkeeper.runner import Context
from portkeeper.safety import Invalid, checksum


def make_sfo(title='CUSA01623', version='01.02', category='gd'):
    values = {'TITLE_ID': title, 'APP_VER': version, 'CATEGORY': category, 'TITLE': 'Portkeeper synthetic fixture',
              'CONTENT_ID': f'UP0000-{title}_00-PORTKEEPERTEST00'}
    keys, data, entries = bytearray(), bytearray(), bytearray()
    for key, value in values.items():
        encoded = value.encode() + b'\x00'
        entries += struct.pack('<HHIII', len(keys), 0x0204, len(encoded), len(encoded), len(data))
        keys += key.encode() + b'\x00'
        data += encoded
    start = 20 + len(entries)
    return b'\x00PSF' + struct.pack('<IIII', 0x101, start, start + len(keys), len(values)) + entries + keys + data


def make_pkg(path, title='CUSA01623', version='01.00', role='base', marker=b'fixture'):
    sfo = make_sfo(title, version, 'gp' if role == 'update' else 'gd')
    header = bytearray(0x1000)
    header[:4] = b'\x7fCNT'
    struct.pack_into('>I', header, 0x10, 1)
    struct.pack_into('>I', header, 0x18, 0x1000)
    content = f'UP0000-{title}_00-PORTKEEPERTEST00'.encode()
    header[0x40:0x40 + len(content)] = content
    flags = 0x00100000 if role == 'update' else 0x41000000 if role == 'delta_update' else 0
    struct.pack_into('>II', header, 0x74, 0x1A, flags)
    entry = struct.pack('>IIIIIIQ', 0x1000, 0, 0, 0, 0x1020, len(sfo), 0)
    struct.pack_into('>Q', header, 0x430, len(header) + len(entry) + len(sfo) + len(marker))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + entry + sfo + marker)
    return path


class PkgMetadataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_base_and_update_identified_from_metadata_not_names(self):
        base = make_pkg(self.root / 'random-update-name.pkg')
        update = make_pkg(self.root / 'random-base-name.pkg', version='01.02', role='update')
        self.assertEqual(ps4.pkg_metadata(base)['role'], 'base')
        self.assertEqual(ps4.pkg_metadata(update)['role'], 'update')
        self.assertEqual(ps4.pkg_metadata(update)['app_version'], '01.02')

    def test_wrong_title_role_version_and_delta_update_have_specific_reasons(self):
        expected = {'title_id': 'CUSA01623', 'app_version': '01.02', 'role': 'update', 'last_update': True}
        cases = [('wrong.pkg', 'CUSA99999', '01.02', 'update', 'expected CUSA01623'),
                 ('base.pkg', 'CUSA01623', '01.00', 'base', 'not the update'),
                 ('old.pkg', 'CUSA01623', '01.01', 'update', 'expected 01.02'),
                 ('delta.pkg', 'CUSA01623', '01.02', 'delta_update', 'delta update')]
        for name, title, version, role, message in cases:
            with self.subTest(name=name):
                self.assertIn(message, ps4.mismatch(ps4.pkg_metadata(make_pkg(self.root / name, title, version, role)), expected))

    def test_incomplete_pkg_and_out_of_bounds_tables_are_rejected(self):
        path = make_pkg(self.root / 'game.pkg')
        data = bytearray(path.read_bytes())
        struct.pack_into('>Q', data, 0x430, len(data) + 1)
        path.write_bytes(data)
        with self.assertRaisesRegex(Invalid, 'incomplete'):
            ps4.pkg_metadata(path)
        struct.pack_into('>Q', data, 0x430, len(data))
        struct.pack_into('>I', data, 0x18, len(data))
        path.write_bytes(data)
        with self.assertRaisesRegex(Invalid, 'entry table'):
            ps4.pkg_metadata(path)
        with self.assertRaisesRegex(Invalid, 'table offsets'):
            bad = bytearray(make_sfo());struct.pack_into('<I', bad, 8, 0);ps4.sfo_values(bad)


class PkgPreparationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = Engine(self.root / 'data', input_root=self.root / 'roms')
        self.addCleanup(self.engine.close)
        self.definition = copy.deepcopy(packaging.bundled()['gow3_pc'])
        self.op = self.definition['operations'][0]
        self.op['accepted_eboot_sha256'] = [hashlib.sha256(b'updated-eboot').hexdigest()]
        self.project = self.engine.create_project({'name': 'Fixture', 'repo': 'FelipePrado0/gow3_pc', 'targets': ['windows'], 'packaging': self.definition})
        self.base = make_pkg(self.engine.input_root / 'ps4/roms/arbitrary-first.pkg')
        self.update = make_pkg(self.engine.input_root / 'ps4/roms/arbitrary-second.pkg', version='01.02', role='update')
        self.tool = self.root / 'tool.zip'
        with zipfile.ZipFile(self.tool, 'w') as archive:
            archive.writestr('PkgTool.Core', b'fixture tool placeholder')
        self.ssl = self.root / 'ssl';self.ssl.mkdir();(self.ssl / 'libssl.so.1.1').touch()
        self.calls = []

    def context(self):
        project = self.engine.packager.bind_inputs(self.engine.store.get('projects', self.project['id']), self.definition)
        work = self.root / ('work-%d' % len(list(self.root.glob('work-*'))));work.mkdir()
        return Context(self.engine.packager, project, self.definition, {}, work, lambda _: None, lambda *_: None)

    def extract(self, args, work, env, log_path, timeout):
        self.assertEqual(args[1], 'pkg_makegp4')
        self.assertNotIn(args[2], [str(self.base), str(self.update)])
        metadata = ps4.pkg_metadata(Path(args[2]))
        self.calls.append(metadata['role'])
        part = Path(args[3]);(part / 'sce_sys').mkdir(parents=True)
        (part / 'sce_sys/param.sfo').write_bytes(make_sfo(metadata['title_id'], metadata['app_version'], 'gp' if metadata['role'] == 'update' else 'gd'))
        (part / 'eboot.bin').write_bytes(b'updated-eboot' if metadata['app_version'] == '01.02' else b'base-eboot')
        (part / 'sce_module').mkdir()
        (part / 'sce_module/libc.prx').write_bytes(b'synthetic module')
        (part / ('base-only.dat' if metadata['role'] == 'base' else 'update-only.dat')).write_bytes(b'fixture')
        log_path.write_text('synthetic extractor fixture')
        # A real tool can modify its mapped private copy; original files must stay unchanged.
        Path(args[2]).write_bytes(b'modified private copy')

    def run_tool(self, ctx, extract=None):
        with patch('portkeeper.runner.COMPAT_SSL', str(self.ssl)), patch.object(self.engine.packager, 'dependency', return_value=self.tool), patch.object(self.engine.packager, 'run', side_effect=extract or self.extract):
            self.engine.packager._ps4(ctx, self.op, ctx.shared / 'game', 60)

    def test_discovery_and_binding_distinguish_base_from_update_and_ignore_filenames(self):
        found = self.engine.packager.find_files(self.definition)
        base, update = found[:2]
        self.assertEqual(base['found'][0]['path'], 'ps4/roms/arbitrary-first.pkg')
        self.assertEqual(update['found'][0]['path'], 'ps4/roms/arbitrary-second.pkg')
        self.assertTrue(update['required'], 'A 01.00 base needs its separate 01.02 update')
        with self.assertRaisesRegex(Invalid, 'not the base'):
            self.engine.bind_input(self.project['id'], {'input': 'game_pkg', 'path': 'ps4/roms/arbitrary-second.pkg'})

    def test_base_then_update_overlay_preserves_originals_and_reuses_checked_cache(self):
        originals = [checksum(self.base), checksum(self.update)]
        ctx = self.context();self.run_tool(ctx)
        folder = ctx.aliases['game']
        self.assertEqual(self.calls, ['base', 'update'])
        self.assertEqual((folder / 'eboot.bin').read_bytes(), b'updated-eboot')
        self.assertTrue((folder / 'base-only.dat').exists())
        self.assertTrue((folder / 'update-only.dat').exists())
        self.assertEqual([checksum(self.base), checksum(self.update)], originals)
        self.assertEqual(ctx.provenance['inputs']['game_pkg']['sha256'], originals[0])
        second = self.context();self.run_tool(second)
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(second.provenance['tools'][0]['reused'], 'earlier extraction')

    def test_missing_filesystem_key_reports_actionable_error_and_preserves_originals(self):
        originals = [checksum(self.base), checksum(self.update)]
        def encrypted(args, work, env, log_path, timeout):
            log_path.write_text('PFS image is encrypted but no decryption key was provided')
            raise Invalid('Tool exited with code -6: PFS image is encrypted but no decryption key was provided')
        with self.assertRaisesRegex(Invalid, 'Choose an extractable dump.*prepared game folder'):
            self.run_tool(self.context(), encrypted)
        self.assertEqual([checksum(self.base), checksum(self.update)], originals)
        self.assertEqual(list(self.engine.packager.prepared.glob('ps4/*/portkeeper-extraction.json')), [])

    def test_other_extraction_errors_are_preserved(self):
        with patch.object(self.engine.packager, 'run', side_effect=Invalid('Tool exceeded its time limit')):
            with self.assertRaisesRegex(Invalid, 'Tool exceeded its time limit'):
                self.engine.packager.run_ps4_extraction([], self.root, {}, self.root / 'tool.log', 1)

    def test_missing_update_is_reported_before_extraction(self):
        self.update.unlink()
        with self.assertRaisesRegex(Invalid, 'separate CUSA01623 update PKG'):
            self.run_tool(self.context())
        self.assertEqual(self.calls, [])

    def test_already_updated_base_can_omit_optional_update(self):
        self.update.unlink();make_pkg(self.base, version='01.02')
        choices = self.engine.packager.find_files(self.definition)
        self.assertFalse(choices[1]['required'])
        self.run_tool(self.context())
        self.assertEqual(self.calls, ['base'])

    def test_wrong_extracted_title_is_rejected_before_merging(self):
        def wrong(args, work, env, log_path, timeout):
            self.extract(args, work, env, log_path, timeout)
            (Path(args[3]) / 'sce_sys/param.sfo').write_bytes(make_sfo('CUSA99999', '01.00'))
        with self.assertRaisesRegex(Invalid, 'extracted title.*CUSA99999'):
            self.run_tool(self.context(), wrong)
        self.assertEqual(list(self.engine.packager.prepared.glob('ps4/*/portkeeper-extraction.json')), [])

    def test_pkg_content_change_invalidates_cache_even_with_same_stat_values(self):
        capture = {'commit': 'a' * 40, 'assets': []}
        project = self.engine.packager.bind_inputs(self.project, self.definition)
        original_key = self.engine.packager.build_key(project, self.definition, capture, 'windows')
        self.run_tool(self.context())
        state = self.base.stat();data = bytearray(self.base.read_bytes());data[-1] ^= 1
        self.base.write_bytes(data);os.utime(self.base, ns=(state.st_atime_ns, state.st_mtime_ns))
        self.assertNotEqual(original_key, self.engine.packager.build_key(project, self.definition, capture, 'windows'))
        self.run_tool(self.context())
        self.assertEqual(self.calls, ['base', 'update', 'base', 'update'])

    def test_wrong_executable_is_not_saved_as_prepared(self):
        self.op['accepted_eboot_sha256'] = ['f' * 64]
        with self.assertRaisesRegex(Invalid, 'eboot.bin SHA-256.*not accepted'):
            self.run_tool(self.context())
        self.assertEqual(list(self.engine.packager.prepared.glob('ps4/*/portkeeper-extraction.json')), [])

    def test_dp_update_uses_filesystem_and_named_header_extraction(self):
        data = bytearray(self.update.read_bytes());struct.pack_into('>I', data, 0x74, 0x1E);self.update.write_bytes(data)
        self.assertEqual(ps4.pkg_metadata(self.update)['role'], 'update')
        def extract(args, work, env, log_path, timeout):
            if args[1] == 'pkg_makegp4':
                return self.extract(args, work, env, log_path, timeout)
            if args[1] == 'pkg_extract':
                self.calls.append('update')
                folder = Path(args[3]) / 'uroot';folder.mkdir(parents=True)
                (folder / 'eboot.bin').write_bytes(b'updated-eboot')
                (folder / 'update-only.dat').write_bytes(b'fixture')
            else:
                self.assertEqual(args[1], 'pkg_extractentry')
                self.assertEqual(args[3], '0')
                self.assertTrue(args[4].endswith('sce_sys/param.sfo'))
                Path(args[4]).write_bytes(make_sfo(version='01.02', category='gp'))
                self.calls.append('system-metadata')
            log_path.write_text('synthetic DP update fixture')
        ctx = self.context();self.run_tool(ctx, extract)
        self.assertEqual(self.calls, ['base', 'update', 'system-metadata'])
        self.assertEqual((ctx.aliases['game'] / 'eboot.bin').read_bytes(), b'updated-eboot')
        self.assertTrue((ctx.aliases['game'] / 'base-only.dat').is_file())

    def test_discovery_ignores_wrong_prepared_folder_and_new_pkgs_clear_alternative(self):
        self.run_tool(self.context())
        valid = self.engine.packager.locate(None, self.definition, 'prepared_game')
        self.assertEqual(len(valid), 1)
        wrong = self.engine.packager.prepared / 'ps4/wrong/game'
        (wrong / 'sce_sys').mkdir(parents=True)
        (wrong / 'sce_sys/param.sfo').write_bytes(make_sfo('CUSA99999'))
        (wrong / 'eboot.bin').write_bytes(b'updated-eboot')
        self.assertEqual(self.engine.packager.locate(None, self.definition, 'prepared_game'), valid)
        self.engine.bind_input(self.project['id'], {'input': 'prepared_game', **valid[0]})
        updated = self.engine.bind_input(self.project['id'], {'input': 'game_pkg', 'path': 'ps4/roms/arbitrary-first.pkg'})
        self.assertNotIn('prepared_game', updated['input_bindings'])

    def test_full_package_contains_updated_game_and_portable_launcher_configuration(self):
        source = self.root / 'source.zip'
        checks = {}
        for check in self.definition['source_checks']:
            checks.setdefault(check['path'], []).append(check['contains'])
        with zipfile.ZipFile(source, 'w') as archive:
            for path, contents in checks.items():
                archive.writestr('fixture/' + path, '\n'.join(contents))
        runtime = self.root / 'runtime.zip'
        with zipfile.ZipFile(runtime, 'w') as archive:
            for name in ['God of War III.exe', 'Play God of War III.exe', 'run.py', 'bin/runtime.dll',
                         '_internal/fixture.dat', 'launcher/icon.png', 'scripts/fixture.py', 'patches/fixture.xml']:
                archive.writestr('gow3-windows/' + name, b'synthetic runtime fixture')
        capture = {'id': 'c' * 32, 'version': 'windows-v2.1.5', 'commit': 'a' * 40, 'root': str(self.root),
                   'source': {'file': source.name, 'sha256': checksum(source)},
                   'assets': [{'name': 'gow3-windows.zip', 'file': runtime.name, 'sha256': checksum(runtime)}]}
        results = []
        with patch('portkeeper.runner.COMPAT_SSL', str(self.ssl)), patch.object(self.engine.packager, 'dependency', return_value=self.tool), patch.object(self.engine.packager, 'run', side_effect=self.extract):
            self.engine.packager.build_group(self.project, self.definition, capture, [{'id': 'j', 'target': 'windows'}],
                lambda _: lambda *_: None, lambda _: lambda *_: None, lambda *args: results.append(args))
        self.assertEqual(len(results), 1)
        job, artifact, error = results[0]
        self.assertIsNone(error, str(error))
        with zipfile.ZipFile(artifact['file']) as archive:
            self.assertEqual(archive.read('game/eboot.bin'), b'updated-eboot')
            self.assertIn('game/base-only.dat', archive.namelist())
            self.assertIn('game/update-only.dat', archive.namelist())
            settings = json.loads(archive.read('data/config/gow3-launcher/settings.json'))
            self.assertEqual(settings, {'game_dir': 'game', 'user_dir': 'user'})
            self.assertIn(b'set "APPDATA=%CD%\\data\\config"', archive.read('Launch.cmd'))
            self.assertIn(b'set "GOW3_GAME_DIR=%CD%\\game"', archive.read('Launch.cmd'))
            manifest = json.loads(archive.read('portkeeper-manifest.json'))
            self.assertEqual(manifest['tools'][0]['app_version'], '01.02')
            self.assertEqual(manifest['inputs']['update_pkg']['sha256'], checksum(self.update))
        self.assertEqual(artifact['validation_level'], 'packaged')

    def test_unwritable_cache_stops_before_hashing_large_inputs(self):
        ctx = self.context()
        original_mkdir = Path.mkdir
        def mkdir(path, *args, **kwargs):
            if path == self.engine.packager.prepared / 'ps4':
                raise PermissionError('fixture cache is read-only')
            return original_mkdir(path, *args, **kwargs)
        with patch.object(Path, 'mkdir', mkdir), patch('portkeeper.runner.checksum') as hashing:
            with self.assertRaisesRegex(Invalid, 'cache is not writable'):
                self.run_tool(ctx)
            hashing.assert_not_called()
        self.assertEqual(self.calls, [])

    def test_low_disk_space_stops_before_private_copy_or_extraction(self):
        usage = type('Usage', (), {'free': 1})()
        with patch('portkeeper.runner.shutil.disk_usage', return_value=usage):
            with self.assertRaisesRegex(Invalid, 'Not enough space'):
                self.run_tool(self.context())
        self.assertEqual(self.calls, [])


if __name__ == '__main__':
    unittest.main()
