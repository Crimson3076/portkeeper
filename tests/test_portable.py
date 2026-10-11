import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from portkeeper.portable import prepare_dusklight
from portkeeper.safety import Invalid, validate_recipe


class PortableTests(unittest.TestCase):
    def test_linux_launcher_keeps_disc_and_data_local_after_move(self):
        with tempfile.TemporaryDirectory() as tmp:
            original = Path(tmp) / "original"
            original.mkdir()
            (original / "game.ciso").write_bytes(b"owned disc fixture")
            (original / "Dusklight.AppImage").write_text(
                '#!/bin/sh\nprintf "%s\\n" "$PWD" "$APPIMAGE_EXTRACT_AND_RUN" "$@" > arguments.txt\n')
            prepare_dusklight(original, "linux", "game.ciso")
            moved = Path(tmp) / "moved folder with spaces"
            shutil.move(original, moved)
            subprocess.run([str(moved / "Launch Dusklight.sh"), "--test"], cwd=tmp, check=True)
            args = (moved / "arguments.txt").read_text().splitlines()
            self.assertEqual(args, [str(moved), "1", "--dvd", "game.ciso", "--user-dir", str(moved / "data"), "--test"])
            self.assertTrue((moved / "data").is_dir())
            self.assertTrue(os.access(moved / "Launch Dusklight.sh", os.X_OK))

    def test_windows_launcher_uses_its_own_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "game.ciso").write_bytes(b"disc")
            (root / "dusklight.exe").write_bytes(b"exe fixture")
            prepare_dusklight(root, "windows", "game.ciso")
            content = (root / "Launch Dusklight.cmd").read_bytes()
            self.assertIn(b'cd /d "%~dp0"\r\n', content)
            self.assertIn(b'--dvd "game.ciso" --user-dir "%~dp0data"', content)
            with self.assertRaises(Invalid):
                prepare_dusklight(root, "windows", "game.ciso")

    def test_recipe_requires_verified_disc_and_rejects_shell_metacharacters(self):
        recipe = {"version": 1, "mode": "assemble", "expected_files": ["game.ciso"],
                  "inputs": [{"path": "rom.ciso", "destination": "game.ciso", "sha256": "0" * 64}],
                  "portable": {"kind": "dusklight", "disc": "game.ciso"}}
        validate_recipe(recipe)
        for disc in ('../game.ciso', 'game.ciso";touch pwned', 'game%PATH%.ciso', 'missing.iso'):
            changed = json.loads(json.dumps(recipe))
            changed["portable"]["disc"] = disc
            with self.assertRaises(Invalid):
                validate_recipe(changed)
        recipe["inputs"] = []
        with self.assertRaises(Invalid):
            validate_recipe(recipe)
