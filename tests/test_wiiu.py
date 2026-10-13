import copy
import hashlib
import json
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from portkeeper import packaging
from portkeeper.engine import Engine
from test_packaging import archive_bytes

ROOT = Path(__file__).resolve().parent.parent
RPX = b"fixture rpx"
# Stands in for the release's wwhd-extract: same arguments and "info" output format.
EXTRACTOR = b"""#!/bin/sh
title="$2"; command="$3"; archive="$4"
read first < "$archive"
case "$first" in WUA*) ;; *) echo "error: not a Cemu Wii U archive (.wua): $archive" >&2; exit 7 ;; esac
held=$(sed -n 2p "$archive")
echo "format wua"
echo "title ${held%_v*} ${held#*_v} $held 3 100"
if [ "$command" = info ]; then
  case "$held" in "$title"_v*|"$title") echo "selected $held"; echo "version ${held#*_v}" ;;
  *) echo "error: the archive does not contain title $title (it contains $held)" >&2; exit 10 ;; esac
  exit 0
fi
out="$5"; mkdir -p "$out/code" "$out/content" "$out/meta"
sed -n 3p "$archive" | tr -d '\\n' > "$out/code/cking.rpx"; echo pack > "$out/content/a.pack"; echo meta > "$out/meta/meta.xml"
"""


def release_zip():
    return archive_bytes({"WW/wind-waker-hd": b"#!/bin/sh\n", "WW/portable.txt": b"p", "WW/tools/bin/wwhd-extract": EXTRACTOR,
                          "WW/tools/setup-in-terminal.sh": b"#!/bin/sh\n", "WW/tools/installer/setup.py": b""},
                         {"WW/wind-waker-hd": 0o755, "WW/tools/bin/wwhd-extract": 0o755, "WW/tools/setup-in-terminal.sh": 0o755})


class Github:
    def __init__(self):
        self.url = "https://github.com/x/ww/releases/download/v1/WW-linux.zip"
        self.payloads = {self.url: release_zip(), "https://codeload.github.com/x/ww/zip/" + "b" * 40: archive_bytes({"ww-b/README.md": b"x"})}

    def release(self, repo, version=None):
        return {"version": "v1", "commit": "b" * 40, "release_id": 1, "url": "https://github.com/x/ww",
                "assets": [{"id": 1, "name": "WW-linux.zip", "browser_download_url": self.url, "updated_at": "2026-10-01", "size": 1}]}
    release_tag = release

    def download(self, url, output):
        Path(output).write_bytes(self.payloads[url])
        return {"sha256": hashlib.sha256(self.payloads[url]).hexdigest(), "size": len(self.payloads[url])}


def definition():
    value = json.loads((ROOT / "catalog/definitions/zelda-wind-waker-hd.json").read_text())
    value["upstream"]["repository"] = "x/ww"
    value["upstream"]["release_rules"]["tag_pattern"] = "^v[0-9]+$"
    value["targets"] = ["linux"]
    value["downloads"] = {"linux": {"asset_pattern": "^WW-linux\\.zip$", "platform": "linux", "format": "zip", "strip_prefix": "WW"}}
    value["operations"] = [op for op in value["operations"] if not op.get("targets") or op["targets"] == ["linux"]]
    value["operations"][1]["accepted_rpx_sha256"] = [hashlib.sha256(RPX).hexdigest()]
    value["outputs"] = {"linux": value["outputs"]["linux"]}
    value["launch"] = {"linux": value["launch"]["linux"]}
    value["dependencies"] = {"linux": value["dependencies"]["linux"]}
    value["source_checks"] = []
    return value


class WiiUTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "inputs/wiiu").mkdir(parents=True)
        self.engine = Engine(self.root / "data", self.root / "inputs", self.root / "library", github=Github())
        self.addCleanup(self.engine.close)

    def build(self, archive, value=None):
        (self.root / "inputs/wiiu/Game.wua").write_bytes(archive)
        project = self.engine.create_project({"name": "WW", "repo": "x/ww", "targets": ["linux"], "packaging": value or definition()})
        self.engine.check(project["id"])
        while project["id"] in self.engine.busy:
            time.sleep(0.01)
        self.engine.bind_input(project["id"], {"input": "game_wua"})
        job = self.engine.build(project["id"])["jobs"][0]
        while project["id"] in self.engine.busy:
            time.sleep(0.01)
        return self.engine.store.get("jobs", job["id"])

    def test_archive_is_extracted_into_the_package_and_setup_runs_once_from_the_launcher(self):
        job = self.build(b"WUA\n0005000010143500_v0\n" + RPX + b"\n")
        self.assertEqual(job["status"], "ready", job.get("error"))
        artifact = self.engine.store.get("artifacts", job["artifact_id"])
        with zipfile.ZipFile(artifact["file"]) as bundle:
            self.assertEqual(bundle.read("data/game/code/cking.rpx"), RPX)
            self.assertIn("data/game/meta/meta.xml", bundle.namelist())
            launcher = bundle.read("Launch.sh").decode()
        self.assertIn('if [ ! -e "./data/install.json" ]; then "./tools/setup-in-terminal.sh" "--yes" "--no-launch"; fi', launcher)
        self.assertLess(launcher.index("install.json"), launcher.index('exec "./wind-waker-hd"'))
        self.assertEqual(artifact["manifest"]["tools"][0]["title"], "0005000010143500_v0")

    def test_other_titles_versions_and_unknown_code_are_refused(self):
        for archive, message in ((b"WUA\n0005000e10143500_v0\n" + RPX + b"\n", "does not contain title"),
                                 (b"WUA\n0005000010143500_v32\n" + RPX + b"\n", "version 32"),
                                 (b"WUA\n0005000010143500_v0\nother\n", "not an accepted"),
                                 (b"PK\x03\x04", "not a readable Cemu Wii U archive")):
            with self.subTest(message=message):
                job = self.build(archive)
                self.assertEqual(job["status"], "failed")
                self.assertIn(message, job["error"])
                for project in self.engine.store.all("projects"):
                    self.engine.store.delete("projects", project["id"])

    def test_windows_launcher_runs_setup_once_and_stops_on_failure(self):
        op = {"op": "launcher", "path": "Launch.cmd", "template": "windows_cmd", "exec": "Wind Waker HD.exe",
              "prepare_once": {"exec": "Wind Waker HD.exe", "args": ["--console-setup", "--yes"], "unless_exists": "data/install.json"}}
        body, _ = self.engine.packager.launcher(op, None)
        self.assertIn(b'if not exist "data\\install.json" "%~dp0Wind Waker HD.exe" "--console-setup" "--yes"\r\nif errorlevel 1 exit /b 1\r\n', body)
        for bad in ({"exec": "x"}, {"exec": "../x", "unless_exists": "a"}, {"exec": "x", "unless_exists": "a", "args": ["a;b"]}, {"exec": "x", "unless_exists": "a*"}):
            value = definition()
            value["operations"][-1]["prepare_once"] = bad
            with self.subTest(bad=bad), self.assertRaises(packaging.Invalid):
                packaging.validate_definition(value)

    def dump_files(self, version='0', rpx=RPX):
        return {'code/cking.rpx': rpx, 'content/game.pack': b'fixture data', 'meta/meta.xml': f'<menu><title_id>0005000010143500</title_id><title_version>{version}</title_version></menu>'.encode()}

    def test_decrypted_zip_is_included_and_wrong_version_is_rejected(self):
        payload = archive_bytes({'Nested/Game/' + name: value for name, value in self.dump_files().items()})
        job = self.build(payload)
        self.assertEqual(job['status'], 'ready', job.get('error'))
        artifact = self.engine.store.get('artifacts', job['artifact_id'])
        with zipfile.ZipFile(artifact['file']) as package:
            self.assertEqual(package.read('data/game/code/cking.rpx'), RPX)
        bad = archive_bytes({'Game/' + name: value for name, value in self.dump_files(version='1').items()})
        job = self.build(bad)
        self.assertEqual(job['status'], 'failed')
        self.assertIn('version-0', job['error'])

    def test_decrypted_folder_discovery_and_packaging_preserve_originals(self):
        folder = self.root / 'inputs/wiiu/roms/Unrelated filename'
        for name, content in self.dump_files().items():
            path = folder / name;path.parent.mkdir(parents=True, exist_ok=True);path.write_bytes(content)
        project = self.engine.create_project({'name': 'WW', 'repo': 'x/ww', 'targets': ['linux'], 'packaging': definition()})
        choices = self.engine.packager.find_files(project['packaging'])
        found = next(x for x in choices if x['id'] == 'game_folder')['found']
        self.assertEqual(found, [{'source': 'inputs', 'path': 'wiiu/roms/Unrelated filename'}])
        self.engine.bind_input(project['id'], {'input': 'game_folder', **found[0]})
        self.engine.check(project['id'])
        while project['id'] in self.engine.busy:time.sleep(0.01)
        job = self.engine.build(project['id'])['jobs'][0]
        while project['id'] in self.engine.busy:time.sleep(0.01)
        job = self.engine.store.get('jobs', job['id'])
        self.assertEqual(job['status'], 'ready', job.get('error'))
        self.assertEqual((folder / 'code/cking.rpx').read_bytes(), RPX)

    def test_decrypted_folder_rejects_wrong_hash_and_symlinks(self):
        folder = self.root / 'folder'
        for name, content in self.dump_files(rpx=b'wrong version').items():
            path = folder / name;path.parent.mkdir(parents=True, exist_ok=True);path.write_bytes(content)
        op = next(x for x in definition()['operations'] if x.get('tool') == 'wwhd_extract')
        with self.assertRaisesRegex(packaging.Invalid, 'SHA-256'):
            self.engine.packager.validate_wiiu_folder(op, folder)
        (folder / 'content/link').symlink_to(self.root)
        with self.assertRaises(packaging.Invalid):
            self.engine.packager.validate_wiiu_folder(op, folder)

    def test_catalog_definition_is_bundled_and_offered_to_the_existing_project(self):
        bundled = packaging.bundled()["zeldawwhdrecomp"]
        self.assertEqual(bundled["inputs"][0]["id"], "game_wua")
        project = {"repo": "ZeldaWWHDRecomp/ZeldaWWHDRecomp", "packaging": {"id": "zeldawwhdrecomp", "runner": "operations", "revision": 2}}
        self.assertEqual(self.engine.catalog.update_for(project)["revision"], bundled["revision"])


if __name__ == "__main__":
    unittest.main()
