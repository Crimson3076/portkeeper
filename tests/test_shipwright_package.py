import argparse
import hashlib
import importlib.util
import json
import stat
import sys
import tempfile
import unittest
import warnings
import zipfile
from pathlib import Path
from unittest.mock import patch


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools/shipwright_package.py"
SPEC = importlib.util.spec_from_file_location("shipwright_package", MODULE_PATH)
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)


class ShipwrightPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.canonical = b"\x80\x37\x12\x40" + bytes(range(28))
        self.sha1 = hashlib.sha1(self.canonical).hexdigest()
        self.supported = {self.sha1: "fixture US revision"}

    def rom_zip(self, entries):
        path = self.root / "rom.zip"
        with zipfile.ZipFile(path, "w") as bundle:
            for name, data in entries:
                bundle.writestr(name, data)
        return path

    def test_normalization_identifies_byte_order_by_header(self):
        swapped = b"".join(self.canonical[i:i + 2][::-1] for i in range(0, len(self.canonical), 2))
        little = b"".join(self.canonical[i:i + 4][::-1] for i in range(0, len(self.canonical), 4))
        for data in (self.canonical, swapped, little):
            self.assertEqual(helper.normalize_rom(data), self.canonical)
        for data in (b"bad header!!", self.canonical[:-1]):
            with self.assertRaises(helper.PackageError):
                helper.normalize_rom(data)

    def test_rom_hash_matches_normalized_bytes_and_ignores_filename_revision(self):
        swapped = b"".join(self.canonical[i:i + 2][::-1] for i in range(0, len(self.canonical), 2))
        path = self.rom_zip([("wrong-revision.v64", swapped), ("unrelated.txt", b"text")])
        data, record = helper.select_rom(path, self.supported)
        self.assertEqual(data, self.canonical)
        self.assertEqual(record["sha1"], self.sha1)
        self.assertEqual(record["revision"], "fixture US revision")
        with self.assertRaisesRegex(helper.PackageError, "No supported ROM"):
            helper.select_rom(path, {"0" * 40: "other revision"})

    def test_multiple_supported_members_require_explicit_selection(self):
        path = self.rom_zip([("one.z64", self.canonical), ("two.z64", self.canonical)])
        with self.assertRaisesRegex(helper.PackageError, "--rom-member"):
            helper.select_rom(path, self.supported)
        _, record = helper.select_rom(path, self.supported, "two.z64")
        self.assertEqual(record["member"], "two.z64")
        with self.assertRaisesRegex(helper.PackageError, "does not name"):
            helper.select_rom(path, self.supported, "missing.z64")

    def test_dangerous_entries_are_rejected_even_when_another_member_is_selected(self):
        for name in ("../evil", "/evil", "folder\\evil", "C:/evil", "folder//evil"):
            path = self.rom_zip([("good.z64", self.canonical), (name, b"x")])
            with self.subTest(name=name), self.assertRaises(helper.PackageError):
                helper.select_rom(path, self.supported, "good.z64")
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        path = self.rom_zip([("good.z64", self.canonical), (info, b"../other")])
        with self.assertRaisesRegex(helper.PackageError, "link or special"):
            helper.select_rom(path, self.supported, "good.z64")

    def test_expansion_and_member_bounds_are_enforced(self):
        path = self.rom_zip([("good.z64", self.canonical)])
        for limits in ({"MAX_ROM": 16}, {"MAX_EXPANDED": 16}, {"MAX_MEMBERS": 0}):
            with self.subTest(limits=limits), patch.multiple(helper, **limits), self.assertRaises(helper.PackageError):
                helper.select_rom(path, self.supported)

    def test_runtime_hash_failure_prevents_extraction_and_final_packages(self):
        fake = self.root / "runtime.appimage"
        fake.write_bytes(b"unexpected")
        with self.assertRaisesRegex(helper.PackageError, "SHA256 mismatch"):
            helper.verify_runtime(fake, "linux")

    def fixture_args(self):
        source = self.root / "source"
        (source / "docs").mkdir(parents=True)
        (source / "soh/assets/yml/fixture").mkdir(parents=True)
        (source / "CMakeLists.txt").write_text("project(Ship VERSION 9.3.0 LANGUAGES C CXX)\n")
        (source / "docs/supportedHashes.json").write_text(json.dumps([{"sha1": self.sha1, "name": "fixture US revision"}]))
        (source / "soh/assets/yml/config.yml").write_text(
            f"{self.sha1}:\n  path: fixture\n  config:\n    output:\n      binary: oot.o2r\n")
        rom = self.root / "owned.z64"
        rom.write_bytes(self.canonical)
        extractor = self.root / "extractor"
        extractor.write_text(f"#!{sys.executable}\n" + """import pathlib, sys, zipfile
destination = pathlib.Path(sys.argv[sys.argv.index('--dest') + 1])
with zipfile.ZipFile(destination / 'oot.o2r', 'w') as archive:
    archive.writestr('portVersion', bytes.fromhex('01000900030000'))
    archive.writestr('fixture-game-asset', b'extracted data')
""")
        extractor.chmod(0o755)
        appimage = self.root / "official.appimage"
        appimage.write_bytes(b"fixture official Linux executable")
        windows = self.root / "official.zip"
        with zipfile.ZipFile(windows, "w") as bundle:
            bundle.writestr("soh.exe", b"fixture official Windows executable")
            bundle.writestr("soh.o2r", b"fixture official port assets")
            bundle.writestr("debug/soh.pdb", b"fixture symbols")
        runtimes = {target: {**helper.RUNTIMES[target], "sha256": helper.digest(path)}
                    for target, path in (("linux", appimage), ("windows", windows))}
        args = argparse.Namespace(rom=str(rom), rom_member=None, shipwright_source=str(source),
                                  extractor=str(extractor), linux_appimage=str(appimage), windows_zip=str(windows),
                                  output_dir=str(self.root / "packages"), overwrite=False, extractor_patch=None,
                                  source_commit=helper.SOURCE_COMMIT, timeout=10)
        return args, runtimes

    def test_assembly_keeps_asset_and_provenance_excludes_rom_and_preserves_executable_mode(self):
        args, runtimes = self.fixture_args()
        with patch.object(helper, "RUNTIMES", runtimes):
            packages = helper.package(args)
        self.assertEqual(len(packages), 2)
        self.assertEqual(list(Path(args.output_dir).glob(".shipwright-build-*")), [])
        for package in packages:
            path = Path(package["path"])
            self.assertEqual(package["sha256"], helper.digest(path))
            with zipfile.ZipFile(path) as bundle:
                names = bundle.namelist()
                self.assertFalse(any(Path(name).suffix in {".z64", ".v64", ".n64"} for name in names))
                manifest_name = next(name for name in names if name.endswith("portkeeper-manifest.json"))
                manifest = json.loads(bundle.read(manifest_name))
                self.assertEqual(manifest["rom"]["sha1"], self.sha1)
                self.assertEqual(manifest["extractor"]["kind"], "locally compiled soh-torch")
                self.assertEqual(manifest["validation"]["gameplay"], "not tested")
                asset_name = next(name for name in names if name.endswith("/oot.o2r"))
                self.assertEqual(manifest["files"]["oot.o2r"]["sha256"], hashlib.sha256(bundle.read(asset_name)).hexdigest())
                if manifest["target"] == "linux":
                    executable = next(info for info in bundle.infolist() if info.filename.endswith(".appimage"))
                    self.assertEqual(stat.S_IMODE(executable.external_attr >> 16), 0o755)
                    launcher = next(name for name in names if name.endswith("launch.sh"))
                    self.assertIn(b'SHIP_HOME="$package_dir"', bundle.read(launcher))
                else:
                    self.assertTrue(any(name.endswith("debug/soh.pdb") for name in names))
        with patch.object(helper, "RUNTIMES", runtimes), self.assertRaisesRegex(helper.PackageError, "already exists"):
            helper.package(args)

    def test_windows_structure_failure_commits_no_target_and_cleans_scratch(self):
        args, runtimes = self.fixture_args()
        with zipfile.ZipFile(args.windows_zip, "w") as bundle:
            bundle.writestr("wrong.exe", b"x")
        runtimes["windows"]["sha256"] = helper.digest(args.windows_zip)
        with patch.object(helper, "RUNTIMES", runtimes), self.assertRaisesRegex(helper.PackageError, "exactly one soh.exe"):
            helper.package(args)
        self.assertEqual(list(Path(args.output_dir).iterdir()), [])

    def test_generated_assets_require_release_port_version(self):
        path = self.root / "oot.o2r"
        for version in (b"wrong", b"\x01\x00\x08\x00\x03\x00\x00"):
            with zipfile.ZipFile(path, "w") as bundle:
                bundle.writestr("portVersion", version)
            with self.assertRaisesRegex(helper.PackageError, "9.3.0"):
                helper.validate_game_assets(path)

    def test_generated_assets_validate_crc_for_every_duplicate_resource_entry(self):
        path = self.root / "oot.o2r"
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(path, "w") as bundle:
                bundle.writestr("portVersion", bytes.fromhex("01000900030000"))
                bundle.writestr("shared", b"first resource")
                bundle.writestr("shared", b"last resource")
        helper.validate_game_assets(path)
        with zipfile.ZipFile(path) as bundle:
            first = next(info for info in bundle.infolist() if info.filename == "shared")
            offset = first.header_offset + 30 + len(first.filename.encode("utf-8")) + len(first.extra)
        data = bytearray(path.read_bytes())
        data[offset] ^= 1
        path.write_bytes(data)
        with self.assertRaises(zipfile.BadZipFile):
            helper.validate_game_assets(path)


if __name__ == "__main__":
    unittest.main()
