import copy
import hashlib
import io
import json
import struct
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from portkeeper import packaging
from portkeeper.engine import Engine
from portkeeper.runner import normalize_n64, overlay, read_sfo
from portkeeper.safety import Invalid
from portkeeper.storage import now


ROM = b"\x80\x37\x12\x40" + bytes(range(256)) * 16


def byteswapped(data):
    swapped = bytearray(len(data))
    swapped[0::2], swapped[1::2] = data[1::2], data[0::2]
    return bytes(swapped)


def archive_bytes(files, modes=None):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = ((modes or {}).get(name, 0o644) | 0o100000) << 16
            archive.writestr(info, body)
    return stream.getvalue()


def definition(**changes):
    value = {
        "schema": 2, "id": "example-port", "revision": 1, "name": "Example Port", "package_prefix": "Example-Port",
        "runner": "operations", "runner_version": 1,
        "upstream": {"repository": "example/port", "release_rules": {"stable_only": True, "tag_pattern": "v[0-9]+"}},
        "targets": ["linux", "windows"],
        "downloads": {
            "linux": {"asset_pattern": r"^Port-v[0-9]+-Linux\.zip$", "platform": "linux", "format": "zip", "required_members": ["port"]},
            "windows": {"asset_pattern": r"^Port-v[0-9]+-Win64\.zip$", "platform": "windows", "format": "zip", "required_members": ["port.exe"]}},
        "inputs": [{"id": "rom", "label": "Example ROM", "kind": "file", "platform_hint": "n64", "normalize": "n64_big_endian",
                    "accepted": [{"label": "USA", "sha1": hashlib.sha1(ROM).hexdigest()}], "max_bytes": 1048576}],
        "source_checks": [{"description": "Upstream still lists the ROM", "path": "docs/hashes.json", "contains_input_hash": "rom"}],
        "operations": [
            {"op": "unpack_download", "download": "linux", "to": "$package", "targets": ["linux"]},
            {"op": "unpack_download", "download": "windows", "to": "$package", "targets": ["windows"]},
            {"op": "stage_input", "input": "rom", "to": "$shared/game.z64"},
            {"op": "copy", "from": "$shared/game.z64", "to": "$package/game.z64"},
            {"op": "touch", "path": "$package/portable.txt"},
            {"op": "launcher", "targets": ["linux"], "path": "Launch.sh", "template": "posix", "env": {"HOME_DIR": "$PWD/data"}, "mkdir": ["data"], "chmod_exec": True, "exec": "port", "args": ["--rom", "game.z64"]},
            {"op": "launcher", "targets": ["windows"], "path": "Launch.cmd", "template": "windows_cmd", "env": {"HOME_DIR": "%CD%\\data"}, "exec": "port.exe"},
            {"op": "write_text", "path": "$package/README.txt", "text": "{name} {version} for {target}. Run {launch_command}."}],
        "outputs": {"linux": {"expected_files": ["port", "game.z64", "Launch.sh", "README.txt"]},
                    "windows": {"expected_files": ["port.exe", "game.z64", "Launch.cmd", "README.txt"]}},
        "launch": {"linux": {"command": "Launch.sh"}, "windows": {"command": "Launch.cmd"}},
        "verified_releases": []}
    value.update(changes)
    return value


class Releases:
    """GitHub stand-in serving tagged releases with ZIP assets and a source archive."""

    def __init__(self):
        self.payloads, self.downloads = {}, []
        self.publish(1)

    def publish(self, number, linux_files=None, hashes=None):
        commit = f"{number:040x}"
        linux = archive_bytes(linux_files or {"port": b"#!/bin/sh\n", "data/x.txt": b"x"}, {"port": 0o755})
        windows = archive_bytes({"port.exe": b"MZ"})
        assets = []
        for index, (name, body) in enumerate(((f"Port-v{number}-Linux.zip", linux), (f"Port-v{number}-Win64.zip", windows))):
            url = f"https://github.com/example/port/releases/download/v{number}/{name}"
            self.payloads[url] = body
            assets.append({"id": number * 10 + index, "name": name, "browser_download_url": url, "updated_at": "2026-10-01", "size": len(body)})
        source = archive_bytes({f"port-{commit}/docs/hashes.json": json.dumps(hashes if hashes is not None else [hashlib.sha1(ROM).hexdigest()])})
        self.payloads[f"https://codeload.github.com/example/port/zip/{commit}"] = source
        self.current = {"version": f"v{number}", "commit": commit, "release_id": number, "url": "https://github.com/example/port", "assets": assets}

    def release(self, repo, version=None):
        return copy.deepcopy(self.current)

    release_tag = release

    def download(self, url, output):
        self.downloads.append(url)
        Path(output).write_bytes(self.payloads[url])
        return {"sha256": hashlib.sha256(self.payloads[url]).hexdigest(), "size": len(self.payloads[url])}


class DefinitionTests(unittest.TestCase):
    def test_bundled_definitions_validate_and_render_steps(self):
        bundled = packaging.bundled()
        self.assertTrue({"2ship2harkinian", "banjo-recompiled", "opengoal-jak1", "bloodborne-bbport-linux", "bloodborne-bbport-windows", "shipwright", "dusklight"} <= set(bundled))
        for item in bundled.values():
            view = packaging.view(item)
            self.assertTrue(view["requirements"]["supported"])
            if item["runner"] == "operations":
                self.assertEqual(len(view["steps"]), len(item["operations"]))
                for target in item["targets"]:
                    self.assertTrue(item["outputs"][target]["expected_files"])

    def test_unknown_runner_support_is_reported_as_needing_an_update(self):
        for change in ({"runner_version": 2}, {"operations": [{"op": "flash_firmware", "path": "$package/x"}]},
                       {"operations": [{"op": "tool", "tool": "new_extractor", "to": "$shared/x"}]}):
            with self.subTest(change=change):
                result = packaging.requirements(definition(**change))
                self.assertFalse(result["supported"])
                self.assertTrue(result["needs_update"])
        malformed = packaging.requirements(definition(targets="linux"))
        self.assertFalse(malformed["supported"])
        self.assertFalse(malformed["needs_update"])

    def test_launchers_and_paths_reject_unsafe_values(self):
        unsafe = [
            {"op": "launcher", "path": "Launch.sh", "template": "posix", "exec": "port", "args": ["x; rm -rf /"]},
            {"op": "launcher", "path": "Launch.sh", "template": "posix", "exec": "port", "args": ["trailing\\"]},
            {"op": "launcher", "path": "Launch.sh", "template": "posix", "exec": "port", "env": {"A": "$(id)"}},
            {"op": "launcher", "path": "Launch.cmd", "template": "windows_cmd", "exec": "port.exe", "args": ["%PATH%"]},
            {"op": "write_text", "path": "$package/../escape", "text": "x"},
            {"op": "copy", "from": "/etc/passwd", "to": "$package/x"},
            {"op": "launcher", "path": "Launch.sh", "template": "posix", "exec": "port", "run_python": ["../x.py"]}]
        for op in unsafe:
            with self.subTest(op=op), self.assertRaises(Invalid):
                packaging.validate_definition(definition(operations=[op]))

    def test_migration_keeps_existing_behavior(self):
        bundled = packaging.bundled()
        capture = {"version": 1, "mode": "capture", "payload": {"kind": "source"}, "notes": "Imported by hand: RomM 12963"}
        adopted = packaging.migrate({"name": "2 Ship 2 Harkinian", "repo": "2ship2harkinian/2ship2harkinian", "targets": ["linux", "windows"], "recipe": capture}, bundled)
        self.assertEqual(adopted["runner"], "operations")
        self.assertEqual(adopted["legacy_recipe"], capture)
        self.assertIn("RomM 12963", adopted["history"][-1])
        unknown = packaging.migrate({"name": "Other", "repo": "someone/other", "targets": ["linux"], "recipe": capture}, bundled)
        self.assertEqual(unknown["runner"], "capture")
        self.assertEqual(packaging.recipe_for(unknown)["mode"], "capture")
        recipe = {"version": 1, "mode": "assemble", "inputs": [{"path": "ngc/disc.ciso", "destination": "game.ciso", "sha256": "a" * 64}],
                  "payload": {"kind": "release", "asset_patterns": {"linux": "^x$"}}, "expected_files": ["game.ciso"],
                  "portable": {"kind": "dusklight", "disc": "game.ciso"}}
        wrapped = packaging.migrate({"name": "Dusklight", "repo": "TwilitRealm/dusklight", "targets": ["linux"], "recipe": recipe}, bundled)
        self.assertEqual(wrapped["runner"], "recipe")
        self.assertEqual(wrapped["recipe"], recipe)
        self.assertEqual(packaging.recipe_for(wrapped), recipe)
        self.assertTrue(wrapped["launch"]["linux"]["command"])
        profile = packaging.migrate({"name": "Ship of Harkinian", "repo": "HarbourMasters/Shipwright", "targets": ["linux"], "profile": "shipwright", "recipe": {}}, bundled)
        self.assertEqual((profile["runner"], profile["profile"]), ("profile", "shipwright"))


class RunnerHelperTests(unittest.TestCase):
    def test_n64_byte_orders_normalize_to_z64(self):
        little = b"".join(ROM[i:i + 4][::-1] for i in range(0, len(ROM), 4))
        for data in (ROM, byteswapped(ROM), little):
            self.assertEqual(normalize_n64(data), ROM)
        with self.assertRaises(Invalid):
            normalize_n64(b"not a rom")

    def test_param_sfo_strings_and_integers_are_read(self):
        entries = [("APP_VER", 0x0204, b"01.09\x00"), ("TITLE_ID", 0x0204, b"CUSA00900\x00"), ("ATTRIBUTE", 0x0404, struct.pack("<I", 7))]
        keys = b"".join(name.encode() + b"\x00" for name, _, _ in entries)
        key_start = 20 + 16 * len(entries)
        data_start = key_start + len(keys)
        index, data, key_offset = b"", b"", 0
        for name, fmt, value in entries:
            index += struct.pack("<HHIII", key_offset, fmt, len(value), len(value), len(data))
            key_offset += len(name) + 1
            data += value
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "param.sfo"
            path.write_bytes(b"\x00PSF" + struct.pack("<IIII", 0x101, key_start, data_start, len(entries)) + index + keys + data)
            self.assertEqual(read_sfo(path), {"APP_VER": "01.09", "TITLE_ID": "CUSA00900", "ATTRIBUTE": 7})


    def test_later_pkg_files_overlay_earlier_ones(self):
        with tempfile.TemporaryDirectory() as temp:
            base, update, game = Path(temp) / "base", Path(temp) / "update", Path(temp) / "game"
            for root, files in ((base, {"eboot.bin": b"1.00", "sce_sys/param.sfo": b"base", "dvdroot_ps4/a.dat": b"a"}),
                                (update, {"eboot.bin": b"1.09", "sce_sys/param.sfo": b"patch", "dvdroot_ps4/b.dat": b"b"})):
                for name, data in files.items():
                    (root / name).parent.mkdir(parents=True, exist_ok=True)
                    (root / name).write_bytes(data)
            overlay(base, game)
            overlay(update, game)
            self.assertEqual((game / "eboot.bin").read_bytes(), b"1.09")
            self.assertEqual((game / "sce_sys/param.sfo").read_bytes(), b"patch")
            self.assertEqual(sorted(p.name for p in (game / "dvdroot_ps4").iterdir()), ["a.dat", "b.dat"])

class PackagingEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root / "inputs/n64/roms").mkdir(parents=True)
        (self.root / "inputs/n64/roms/Example (USA).v64").write_bytes(byteswapped(ROM))
        (self.root / "inputs/n64/roms/Other.z64").write_bytes(b"\x80\x37\x12\x40" + b"\x00" * 64)
        self.github = Releases()
        self.engine = Engine(self.root / "data", self.root / "inputs", self.root / "library", github=self.github)
        self.addCleanup(self.engine.close)

    def wait(self, project_id):
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            with self.engine.lock:
                if project_id not in self.engine.busy:
                    return self.engine.store.get("projects", project_id)
            time.sleep(0.01)
        self.fail("Timed out")

    def project(self, **changes):
        return self.engine.create_project({"name": "Example Port", "repo": "example/port", "targets": ["linux", "windows"], "packaging": definition(), **changes})

    def check(self, project):
        self.engine.check(project["id"])
        return self.wait(project["id"])

    def test_definition_builds_portable_packages_with_located_verified_inputs(self):
        project = self.check(self.project())
        self.assertEqual(project["recipe"]["mode"], "definition")
        result = self.engine.build(project["id"])
        self.wait(project["id"])
        jobs = [self.engine.store.get("jobs", job["id"]) for job in result["jobs"]]
        self.assertEqual([job["status"] for job in jobs], ["ready", "ready"], [job.get("error") for job in jobs])
        project = self.engine.store.get("projects", project["id"])
        self.assertEqual(project["input_bindings"]["rom"]["path"], "n64/roms/Example (USA).v64")
        self.assertEqual(project["input_bindings"]["rom"]["accepted"], "USA")
        artifacts = {a["target"]: a for a in self.engine.store.all("artifacts")}
        self.assertEqual(artifacts["linux"]["filename"], f"Example-Port-v1-linux-{now()[:10]}.zip")
        with zipfile.ZipFile(artifacts["linux"]["file"]) as bundle:
            self.assertEqual(bundle.read("game.z64"), ROM)
            launcher = bundle.read("Launch.sh").decode()
            self.assertIn('export HOME_DIR="$PWD/data"', launcher)
            self.assertIn('exec "./port" "--rom" "game.z64" "$@"', launcher)
            self.assertEqual((bundle.getinfo("Launch.sh").external_attr >> 16) & 0o777, 0o755)
            self.assertEqual((bundle.getinfo("port").external_attr >> 16) & 0o777, 0o755)
            self.assertEqual(bundle.read("README.txt").decode(), "Example Port v1 for linux. Run Launch.sh.")
            manifest = json.loads(bundle.read("portkeeper-manifest.json"))
        self.assertEqual(manifest["definition"]["id"], "example-port")
        self.assertEqual(manifest["package_revision"], 1)
        self.assertEqual(manifest["inputs"]["rom"]["accepted"], "USA")
        self.assertIn("no launch", manifest["validation"])
        with zipfile.ZipFile(artifacts["windows"]["file"]) as bundle:
            self.assertIn(b'set "HOME_DIR=%CD%\\data"', bundle.read("Launch.cmd"))
        self.assertEqual(artifacts["linux"]["validation_level"], "packaged")

    def test_compatible_new_release_builds_automatically_and_mismatches_are_held(self):
        project = self.check(self.project(auto_build=True))
        self.assertEqual(len(self.engine.store.all("artifacts")), 2)
        self.github.publish(2)
        self.check(project)
        artifacts = [a for a in self.engine.store.all("artifacts") if a["version"] == "v2"]
        self.assertEqual(len(artifacts), 2)
        self.assertTrue(all(a["release_review"] == "compatible" for a in artifacts))
        self.github.publish(3, linux_files={"renamed-binary": b"x"})
        project = self.check(project)
        self.assertFalse([a for a in self.engine.store.all("artifacts") if a["version"] == "v3"])
        capture = self.engine.store.get("captures", project["capture_id"])
        self.assertEqual(capture["review"]["status"], "needs_review")
        self.assertIn("no longer contains: port", capture["review"]["problems"][0])
        self.assertTrue(any(n["kind"] == "needs_review" for n in self.engine.lifecycle.notifications()))
        with self.assertRaisesRegex(Invalid, "needs review"):
            self.engine.build(project["id"])
        self.github.publish(4, hashes=["0" * 40])
        project = self.check(project)
        review = self.engine.store.get("captures", project["capture_id"])["review"]
        self.assertIn("no longer lists the accepted input hash", review["problems"][0])
        releases = {r["version"]: r["status"] for r in self.engine.releases(project, self.engine.store.all("captures"), self.engine.store.all("artifacts"))}
        self.assertEqual(releases, {"v1": "packaged", "v2": "packaged", "v3": "needs_review", "v4": "needs_review"})

    def test_reviewed_release_checksums_must_match(self):
        reviewed = definition(verified_releases=[{"release": "v1", "assets": {"Port-v1-Linux.zip": "0" * 64}}])
        project = self.check(self.project(packaging=reviewed))
        review = self.engine.store.get("captures", project["capture_id"])["review"]
        self.assertEqual(review["status"], "needs_review")
        self.assertIn("differs from the reviewed v1 file", review["problems"][0])

    def test_mismatched_inputs_are_never_packaged(self):
        (self.root / "inputs/n64/roms/Example (USA).v64").unlink()
        project = self.check(self.project())
        result = self.engine.build(project["id"], ["linux"])
        self.wait(project["id"])
        job = self.engine.store.get("jobs", result["jobs"][0]["id"])
        self.assertEqual(job["status"], "failed")
        self.assertIn("Missing game file", job["error"])
        with self.assertRaisesRegex(Invalid, "not an accepted revision"):
            self.engine.bind_input(project["id"], {"input": "rom", "path": "n64/roms/Other.z64"})
        self.assertFalse(self.engine.store.all("artifacts"))

    def test_definition_edits_create_revisions_and_recipe_edits_are_refused(self):
        project = self.project()
        changed = copy.deepcopy(project["packaging"])
        changed["limitations"] = ["New limitation"]
        project = self.engine.update_project(project["id"], {"packaging": changed})
        self.assertEqual(project["packaging"]["revision"], 2)
        with self.assertRaisesRegex(Invalid, "Edit the definition"):
            self.engine.update_project(project["id"], {"recipe": {"version": 1, "mode": "capture"}})
        with self.assertRaisesRegex(Invalid, "another repository"):
            self.engine.update_project(project["id"], {"packaging": definition(upstream={"repository": "else/where"})})

    def test_existing_projects_are_migrated_once_on_start(self):
        capture = {"version": 1, "mode": "capture", "payload": {"kind": "source"}, "notes": "manual"}
        self.engine.store.put("projects", {"id": "a" * 32, "name": "Banjo Recompiled", "repo": "BanjoRecomp/BanjoRecomp", "targets": ["linux", "windows"],
                                           "poll_minutes": 360, "auto_build": False, "auto_publish": False, "recipe": capture, "status": "archived", "created_at": "2026-10-11"})
        self.engine.migrate_definitions()
        project = self.engine.store.get("projects", "a" * 32)
        self.assertEqual(project["packaging"]["id"], "banjo-recompiled")
        self.assertEqual(project["recipe"]["mode"], "definition")
        self.assertFalse(project["auto_build"])
        self.assertEqual(project["auto_publish_destination"], "romm")
        self.assertEqual(project["definition_source"]["from_mode"], "capture")

    def test_romm_publication_uploads_then_confirms_indexing(self):
        project = self.check(self.project(targets=["linux"]))
        result = self.engine.build(project["id"])
        self.wait(project["id"])
        artifact = self.engine.store.get("artifacts", self.engine.store.get("jobs", result["jobs"][0]["id"])["artifact_id"])
        romm = Mock()
        romm.public_config.return_value = {"url": "https://romm.example", "configured": True}
        romm.platform_for.return_value = {"id": 43, "fs_slug": "linux", "name": "Linux"}
        uploaded = []

        def find(platform, name):
            return {"id": 99, "files": [{"name": name, "size": artifact["size"]}]} if name in uploaded and len(uploaded) > 1 else None

        def upload(path, platform, name, progress, stop=None):
            uploaded.append(name)
            if name == artifact["filename"]:
                raise FileExistsError(name)
            progress(1, 1)
        romm.find_file.side_effect = find
        romm.upload.side_effect = upload
        self.engine.romm = romm
        state = self.engine.publisher.publish(artifact["id"], lambda m: None)
        self.assertEqual(uploaded, [artifact["filename"], artifact["filename"][:-4] + "-r2.zip"])
        self.assertEqual(state["status"], "in_romm")
        self.assertEqual(state["rom_id"], 99)
        romm.find_file.side_effect = lambda platform, name: None
        romm.upload.side_effect = lambda *a, **k: (_ for _ in ()).throw(Invalid("RomM denied permission (403)"))
        self.engine.store.update("artifacts", artifact["id"], romm=None)
        with self.assertRaisesRegex(Invalid, "403"):
            self.engine.publisher.publish(artifact["id"], lambda m: None)
        self.assertEqual(self.engine.store.get("artifacts", artifact["id"])["romm"]["status"], "failed")

    def test_automatic_publication_goes_to_romm_by_default(self):
        project = self.project(targets=["linux"], auto_build=True, auto_publish=True)
        self.assertEqual(project["auto_publish_destination"], "romm")
        calls = []
        self.engine.publisher.publish = lambda artifact_id, log, stage=None: calls.append(artifact_id) or {"status": "awaiting_scan"}
        self.check(project)
        self.assertEqual(len(calls), 1)
        self.assertFalse(list(self.engine.library_root.glob("*.zip")))


    def test_unchanged_rebuild_does_nothing_and_changed_rebuild_replaces_unprotected_package(self):
        project = self.check(self.project())
        self.engine.build(project["id"])
        self.wait(project["id"])
        first = {a["target"]: a for a in self.engine.store.all("artifacts")}
        again = self.engine.build(project["id"])
        self.assertEqual(again["jobs"], [])
        self.assertEqual(sorted(item["target"] for item in again["skipped"]), ["linux", "windows"])
        self.assertIn("Nothing changed", again["message"])
        self.engine.lifecycle.pin(first["windows"]["id"])
        changed = copy.deepcopy(project["packaging"])
        changed["limitations"] = ["Edited"]
        self.engine.update_project(project["id"], {"packaging": changed})
        result = self.engine.build(project["id"])
        self.wait(project["id"])
        self.assertEqual(len(result["jobs"]), 2)
        artifacts = self.engine.store.all("artifacts")
        self.assertNotIn(first["linux"]["id"], [a["id"] for a in artifacts])
        self.assertFalse(Path(first["linux"]["file"]).exists())
        self.assertIn(first["windows"]["id"], [a["id"] for a in artifacts])
        linux = [a for a in artifacts if a["target"] == "linux"]
        self.assertEqual([a["package_revision"] for a in linux], [2])
        logs = self.engine.store.get("jobs", result["jobs"][1]["id"])["logs"]
        self.assertTrue(any("because it is pinned" in line for line in logs), logs)

    def test_configuration_validation_and_revision_restore_do_not_build(self):
        project = self.project()
        original = project["packaging"]
        edited = json.loads(json.dumps(original))
        edited["summary"] = "My own build instructions"
        result = self.engine.validate_configuration(project["id"], {"packaging": edited})
        self.assertTrue(result["valid"])
        self.assertEqual(self.engine.store.get("projects", project["id"])["packaging"], original)
        saved = self.engine.update_project(project["id"], {"packaging": edited})
        self.assertEqual(saved["packaging"]["revision"], original["revision"] + 1)
        self.assertEqual(saved["definition_history"][0]["definition"], original)
        restored = self.engine.update_project(project["id"], {"packaging": original})
        self.assertEqual(restored["packaging"].get("summary"), original.get("summary"))
        self.assertEqual(restored["packaging"]["revision"], saved["packaging"]["revision"] + 1)
        self.assertEqual(len(restored["definition_history"]), 2)
        self.assertEqual(self.engine.store.all("jobs"), [])
        invalid = json.loads(json.dumps(edited))
        invalid["upstream"]["repository"] = "someone/else"
        with self.assertRaisesRegex(Invalid, "another repository"):
            self.engine.validate_configuration(project["id"], {"packaging": invalid})
        self.assertEqual(self.engine.store.get("projects", project["id"]), restored)

    def test_configuration_history_is_bounded_and_ignores_noop_edits(self):
        project = self.project()
        for index in range(23):
            edited = json.loads(json.dumps(project["packaging"]))
            edited["summary"] = f"Edit {index}"
            project = self.engine.update_project(project["id"], {"packaging": edited})
        self.assertEqual(len(project["definition_history"]), 20)
        again = self.engine.update_project(project["id"], {"packaging": project["packaging"]})
        self.assertEqual(again["definition_history"], project["definition_history"])

    def test_first_build_destination_is_independent_of_future_uploads(self):
        self.engine.catalog.definitions = lambda: {"example-port": definition()}
        with patch.object(self.engine.romm, "public_config", return_value={"configured": True}), patch.object(self.engine, "auto_publish") as upload:
            result = self.engine.add_from_catalog({"definition_id": "example-port", "targets": ["linux"],
                                                  "keep_updated": True, "auto_publish": True, "publish": False})
            project = self.wait(result["project"]["id"])
            upload.assert_not_called()
        self.assertTrue(project["auto_publish"], "Future uploads remain enabled")
        self.assertEqual(len(self.engine.store.all("artifacts")), 1)

    def test_catalog_setup_separates_build_and_upload_automation(self):
        self.engine.catalog.definitions = lambda: {"example-port": definition()}
        with patch.object(self.engine.romm, "public_config", return_value={"configured": True}), patch.object(self.engine, "setup", return_value={}) as setup:
            result = self.engine.add_from_catalog({"definition_id": "example-port", "keep_updated": True,
                                                  "auto_publish": False, "publish": False})
        self.assertTrue(result["project"]["auto_build"])
        self.assertFalse(result["project"]["auto_publish"])
        setup.assert_called_once_with(result["project"]["id"], publish=False, publication_override=False)

    def test_find_files_reports_accepted_files_and_wrong_versions(self):
        found = self.engine.packager.find_files(definition())
        self.assertEqual([f["path"] for f in found[0]["found"]], ["n64/roms/Example (USA).v64"])
        self.assertEqual(found[0]["found"][0]["accepted"], "USA")
        self.assertEqual([(r["path"], r["reason"]) for r in found[0]["rejected"]], [("n64/roms/Other.z64", "not an accepted version")])

    def test_add_from_catalog_binds_files_and_sets_up_in_one_step(self):
        self.engine.catalog.definitions = lambda: {"example-port": definition(first_run={"mode": "preloaded"})}
        games = self.engine.catalog_games()["games"]
        self.assertEqual(games[0]["needs"][0]["label"], "Example ROM")
        self.assertIsNone(games[0]["in_library"])
        result = self.engine.add_from_catalog({"definition_id": "example-port", "targets": ["linux"],
                                               "files": {"rom": "n64/roms/Example (USA).v64"}, "keep_updated": True})
        project = self.wait(result["project"]["id"])
        self.assertTrue(project["auto_build"])
        self.assertFalse(project["auto_publish"])  # RomM is not connected in this test
        self.assertEqual(project["input_bindings"]["rom"]["accepted"], "USA")
        self.assertEqual([a["target"] for a in self.engine.store.all("artifacts")], ["linux"])
        self.assertEqual(self.engine.state()["projects"][0]["packaging_view"]["first_run"]["mode"], "preloaded")
        with self.assertRaisesRegex(Invalid, "already in your library"):
            self.engine.add_from_catalog({"definition_id": "example-port"})

    def test_first_run_is_validated(self):
        with self.assertRaisesRegex(Invalid, "first_run"):
            packaging.validate_definition(definition(first_run={"mode": "magic"}))
        view = packaging.view(packaging.validate_definition(definition(first_run={"mode": "generated", "summary": "Made at build time."})))
        self.assertEqual(view["first_run"], {"mode": "generated", "text": "Made at build time."})

class CatalogTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.github = Mock()
        self.engine = Engine(Path(self.temp.name) / "data", Path(self.temp.name) / "inputs", Path(self.temp.name) / "library", github=self.github)
        self.addCleanup(self.engine.close)

    def serve(self, files):
        def opener(url, accept=None):
            name = url.rsplit("/catalog/", 1)[1]
            if name not in files:
                raise Invalid("GitHub resource not found")
            response = Mock()
            response.__enter__ = lambda s: s
            response.__exit__ = lambda s, *a: None
            response.read = lambda limit=None: json.dumps(files[name]).encode()
            return response
        self.github.open.side_effect = opener

    def test_first_use_falls_back_to_bundled_catalog(self):
        status = self.engine.catalog.status()
        self.assertEqual(status["using"], "bundled catalog")
        self.assertIsNone(status["age_seconds"])
        self.assertIn("2ship2harkinian", {item["id"] for item in status["definitions"]})

    def test_refresh_caches_valid_data_and_keeps_it_when_github_fails(self):
        newer = copy.deepcopy(packaging.bundled()["banjo-recompiled"])
        newer["revision"] = 7
        future = definition(id="future-port", runner_version=9)
        projects = {"schema_version": 1, "projects": [
            {"id": "banjo", "name": "Banjo", "repository": "BanjoRecomp/BanjoRecomp", "procedure_kind": "automatic_definition", "definition": "catalog/definitions/banjo-recompiled.json", "verification_scopes": []},
            {"id": "future", "name": "Future", "repository": "example/port", "procedure_kind": "automatic_definition", "definition": "catalog/definitions/future.json", "verification_scopes": []}]}
        self.serve({"projects.json": projects, "definitions/banjo-recompiled.json": newer, "definitions/future.json": future})
        status = self.engine.catalog.refresh()
        self.assertEqual(status["using"], "github cache")
        self.assertIsNone(status["error"])
        self.assertEqual(self.engine.catalog.definitions()["banjo-recompiled"]["revision"], 7)
        unsupported = next(item for item in status["definitions"] if item["id"] == "future-port")
        self.assertFalse(unsupported["supported"])
        self.assertNotIn("future-port", self.engine.catalog.definitions())
        self.github.open.side_effect = Invalid("GitHub denied the request")
        status = self.engine.catalog.refresh()
        self.assertIn("last valid catalog stays in use", status["error"])
        self.assertEqual(self.engine.catalog.definitions()["banjo-recompiled"]["revision"], 7)
        self.serve({"projects.json": {"schema_version": 1, "projects": [{"id": "x", "name": "x", "repository": "a/b", "procedure_kind": "x",
                                                                          "verification_scopes": [{"platform": "linux", "status": "verified", "approval": None}]}]}})
        status = self.engine.catalog.refresh()
        self.assertIn("approval", status["error"])
        self.assertEqual(self.engine.catalog.definitions()["banjo-recompiled"]["revision"], 7)

    def test_catalog_revision_is_offered_and_applied_on_request(self):
        project = self.engine.create_project({"name": "Banjo Recompiled", "repo": "BanjoRecomp/BanjoRecomp", "targets": ["linux"]})
        self.assertEqual(project["packaging"]["id"], "banjo-recompiled")
        self.assertIsNone(self.engine.catalog.update_for(project))
        newer = copy.deepcopy(packaging.bundled()["banjo-recompiled"])
        newer["revision"] = 3
        self.serve({"projects.json": {"schema_version": 1, "projects": [{"id": "banjo", "name": "Banjo", "repository": "BanjoRecomp/BanjoRecomp", "procedure_kind": "x", "definition": "catalog/definitions/banjo.json", "verification_scopes": []}]},
                    "definitions/banjo.json": newer})
        self.engine.catalog.refresh()
        self.assertEqual(self.engine.catalog.update_for(project)["revision"], 3)
        self.assertEqual(self.engine.store.get("projects", project["id"])["packaging"]["revision"], 1)
        applied = self.engine.catalog.apply(project["id"])
        self.assertEqual(applied["packaging"]["revision"], 3)
        self.assertEqual(applied["targets"], ["linux"])


if __name__ == "__main__":
    unittest.main()
