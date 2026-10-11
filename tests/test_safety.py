import io
import stat
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from portkeeper.engine import allowed_url
from portkeeper.safety import Invalid, confined, safe_extract, target_recipe, validate_recipe, verify_tree


class ArchiveSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_zip_traversal_and_absolute_names_are_rejected(self):
        for name in ("../escaped.txt", "/absolute.txt", "dir/../../escaped.txt", "C:/file", "dir\\file"):
            with self.subTest(name=name):
                archive = self.root / "payload.zip"
                with zipfile.ZipFile(archive, "w") as bundle:
                    bundle.writestr(name, "bad")
                with self.assertRaises(Invalid):
                    safe_extract(archive, self.root / "unpacked")
                self.assertFalse((self.root / "escaped.txt").exists())

    def test_zip_symlinks_and_special_files_are_rejected(self):
        for file_type in (stat.S_IFLNK, stat.S_IFIFO, stat.S_IFCHR):
            with self.subTest(file_type=file_type):
                archive = self.root / "payload.zip"
                info = zipfile.ZipInfo("unsafe")
                info.create_system = 3
                info.external_attr = (file_type | 0o777) << 16
                with zipfile.ZipFile(archive, "w") as bundle:
                    bundle.writestr(info, "../secret")
                with self.assertRaises(Invalid):
                    safe_extract(archive, self.root / "unpacked")

    def test_tar_traversal_links_and_special_files_are_rejected(self):
        fixtures = [("../escaped.txt", tarfile.REGTYPE), ("/absolute.txt", tarfile.REGTYPE),
                    ("symlink", tarfile.SYMTYPE), ("hardlink", tarfile.LNKTYPE),
                    ("pipe", tarfile.FIFOTYPE), ("device", tarfile.CHRTYPE)]
        for name, file_type in fixtures:
            with self.subTest(name=name):
                archive = self.root / "payload.tar"
                info = tarfile.TarInfo(name)
                info.type = file_type
                info.linkname = "../secret"
                data = b"payload" if file_type == tarfile.REGTYPE else b""
                info.size = len(data)
                with tarfile.open(archive, "w") as bundle:
                    bundle.addfile(info, io.BytesIO(data))
                with self.assertRaises(Invalid):
                    safe_extract(archive, self.root / "unpacked")
                self.assertFalse((self.root / "escaped.txt").exists())

    def test_safe_tar_preserves_executable_but_strips_other_permissions(self):
        archive = self.root / "safe.tar"
        info = tarfile.TarInfo("./bin/start")
        info.mode = 0o4755
        info.size = 4
        with tarfile.open(archive, "w") as bundle:
            bundle.addfile(info, io.BytesIO(b"demo"))
        safe_extract(archive, self.root / "unpacked")
        extracted = self.root / "unpacked/bin/start"
        self.assertEqual(extracted.read_bytes(), b"demo")
        self.assertEqual(stat.S_IMODE(extracted.stat().st_mode), 0o755)

    def test_confined_rejects_existing_symlink_components(self):
        (self.root / "real").mkdir()
        (self.root / "alias").symlink_to(self.root / "real", target_is_directory=True)
        with self.assertRaises(Invalid):
            confined(self.root, "alias/asset.bin")
        with self.assertRaises(Invalid):
            verify_tree(self.root)

    def test_archive_member_and_expansion_limits_are_enforced(self):
        archive = self.root / "large.zip"
        with zipfile.ZipFile(archive, "w") as bundle:
            bundle.writestr("one", b"abc")
            bundle.writestr("two", b"def")
        limits = ({"MAX_FILE": 2}, {"MAX_EXPANDED": 5}, {"MAX_MEMBERS": 1})
        for limit in limits:
            with self.subTest(limit=limit), patch.multiple("portkeeper.safety", **limit):
                with self.assertRaises(Invalid):
                    safe_extract(archive, self.root / "unpacked")

    def test_recipe_rejects_unsafe_input_paths_and_unpinned_execution(self):
        base = {"version": 1, "mode": "assemble", "expected_files": ["start"]}
        for path in ("../secret", "/secret", "assets/../secret", "C:/secret"):
            with self.subTest(path=path):
                recipe = {**base, "inputs": [{"path": path, "destination": "asset", "sha256": "a" * 64}]}
                with self.assertRaises(Invalid):
                    validate_recipe(recipe)
        with self.assertRaises(Invalid):
            validate_recipe({**base, "steps": ["run anything"]})
        with self.assertRaises(Invalid):
            validate_recipe({**base, "mode": "docker", "image": "builder:latest", "command": ["build"]})

    def test_download_urls_require_known_https_hosts_without_credentials(self):
        good = "https://codeload.github.com/example/project/zip/commit"
        self.assertEqual(allowed_url(good), good)
        for url in ("http://github.com/example", "https://example.com/file.zip", "https://github.com.evil.test/file",
                    "https://user:secret@github.com/file", "https://github.com:444/file", "file:///etc/passwd"):
            with self.subTest(url=url), self.assertRaises(Invalid):
                allowed_url(url)

    def test_target_overrides_merge_runner_mode_and_command_with_validation(self):
        recipe = {"version": 1, "mode": "assemble", "payload": {"kind": "source"}, "expected_files": ["game"],
                  "target_overrides": {"windows": {"mode": "docker", "image": "builder@sha256:" + "a" * 64,
                                                     "command": ["make", "TARGET=windows"], "expected_files": ["game.exe"]}}}
        effective = target_recipe(recipe, "windows")
        self.assertEqual(effective["mode"], "docker")
        self.assertEqual(effective["command"], ["make", "TARGET=windows"])
        self.assertEqual(effective["payload"], {"kind": "source"})
        self.assertEqual(effective["expected_files"], ["game.exe"])
        self.assertNotIn("target_overrides", effective)
        self.assertEqual(target_recipe(recipe, "linux")["mode"], "assemble")
        self.assertEqual(recipe["expected_files"], ["game"])
        for override in ({"mode": "docker"}, {"mode": "docker", "image": "builder:latest", "command": ["make"]},
                         {"mode": "docker", "image": "builder@sha256:" + "a" * 64, "command": "make"},
                         {"expected_files": ["../game.exe"]}, {"target_overrides": {"linux": {}}}):
            invalid = {**recipe, "target_overrides": {"windows": override}}
            with self.subTest(override=override), self.assertRaises(Invalid):
                validate_recipe(invalid)


if __name__ == "__main__":
    unittest.main()
