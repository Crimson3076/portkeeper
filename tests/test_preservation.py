import hashlib
import json
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from portkeeper.preservation import MANIFEST, Preservation
from portkeeper.safety import Invalid, checksum
from portkeeper.storage import Store


class PreservationStore(Store):
    tables = Store.tables | {"preservations"}


class PreservationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = PreservationStore(self.root / "state.sqlite3")
        self.engine = SimpleNamespace(store=self.store, lock=threading.RLock(), archive_root=self.root / "archive")
        self.service = Preservation(self.engine)
        self.store.put("projects", {"id": "project", "name": "Example game", "repo": "example/game", "capture_id": "capture",
                                    "recipe": {"version": 1, "mode": "capture", "romm_token": "do-not-export",
                                               "inputs": [{"path": "/private/rom.z64", "destination": "assets/game.bin", "sha256": "f" * 64}]}})
        self.source_root = self.engine.archive_root / "sources/project/capture"
        self.source_root.mkdir(parents=True)
        self.source = self.source_root / "source.zip"
        self.write_source({"game/main.cpp": "int main() {}"})

    def tearDown(self):
        self.temp.cleanup()

    def write_source(self, contents):
        with zipfile.ZipFile(self.source, "w") as bundle:
            for name, body in contents.items():
                bundle.writestr(name, body)
        self.store.put("captures", {"id": "capture", "project_id": "project", "root": str(self.source_root),
                                    "version": "v1", "commit": "a" * 40,
                                    "source": {"file": "source.zip", "sha256": checksum(self.source)}, "assets": []})

    def material(self, name, body="preserved material"):
        path = self.root / name
        path.write_text(body)
        return path

    def test_export_preserves_capture_and_redacted_recipe_with_verified_manifest(self):
        record = self.service.export("project")
        self.assertTrue(record["manifest"]["closure"]["source_preserved"])
        self.assertFalse(record["manifest"]["closure"]["offline_rebuild_complete"])
        with zipfile.ZipFile(record["file"]) as bundle:
            self.assertEqual(bundle.read("capture/source.zip"), self.source.read_bytes())
            recipe = json.loads(bundle.read("recipe.json"))
            self.assertEqual(recipe["romm_token"], "[omitted]")
            self.assertNotIn("path", recipe["inputs"][0])
            self.assertTrue(recipe["inputs"][0]["requires_user_owned_input"])
            self.assertNotIn(b"/private/rom", bundle.read("recipe.json"))
        result = self.service.audit(record["id"])
        self.assertTrue(result["integrity_verified"])
        self.assertEqual(result["files_verified"], 2)

    def test_source_declarations_report_missing_submodules_and_fetched_dependencies(self):
        self.write_source({"game/.gitmodules": '[submodule "torch"]\n path = torch\n url = https://github.com/example/torch.git\n',
                           "game/CMakeLists.txt": 'FetchContent_Declare(spdlog GIT_REPOSITORY https://github.com/gabime/spdlog.git GIT_TAG v1.16.0)\n'})
        record = self.service.export("project")
        closure = record["manifest"]["closure"]
        self.assertEqual(closure["missing_materials"], ["dependency:spdlog", "submodule:torch"])
        self.assertFalse(closure["dependency_sources_preserved"])

    def test_explicit_tool_scope_is_reported_and_requires_pinned_sources(self):
        self.write_source({"game/.gitmodules": '[submodule "torch"]\n path = torch\n url = https://github.com/example/torch.git\n\n[submodule "runtime"]\n path = runtime\n url = https://github.com/example/runtime.git\n'})
        path = self.material("torch.tar", "source tar placeholder")
        record = self.service.export("project", {"submodule:torch": path},
                                     dependency_scope={"name": "headless extractor", "labels": ["submodule:torch"]})
        closure = record["manifest"]["closure"]
        self.assertEqual(closure["declarations_outside_scope"], ["submodule:runtime"])
        self.assertEqual(closure["unpinned_materials"], ["submodule:torch"])
        self.assertFalse(closure["dependency_sources_preserved"])

    def test_complete_claim_requires_dependencies_toolchain_and_offline_build_evidence(self):
        self.write_source({"game/CMakeLists.txt": 'FetchContent_Declare(helper URL https://example.invalid/helper.tar.gz URL_HASH SHA256=abcd)'})
        source = self.material("helper.txt")
        image = self.material("toolchain-image.tar")
        log = self.material("offline-build.log", "Build passed with network disabled")
        materials = {"dependency:helper": {"path": source, "revision": "v1"}, "toolchain": image, "build-log": log}
        descriptor = {"material": "toolchain", "identity": "example@sha256:" + "a" * 64}
        record = self.service.export("project", materials, toolchain=descriptor)
        self.assertTrue(record["manifest"]["closure"]["dependency_sources_preserved"])
        self.assertTrue(record["manifest"]["closure"]["toolchain_preserved"])
        self.assertFalse(record["manifest"]["closure"]["offline_rebuild_complete"])
        complete = self.service.export("project", materials, toolchain=descriptor,
                                       offline_build={"succeeded": True, "network_disabled": True,
                                                      "log_material": "build-log", "command": ["cmake", "--build", "build"]})
        self.assertTrue(complete["manifest"]["closure"]["offline_rebuild_complete"])

    def test_dependency_materials_are_inspected_for_transitive_requirements(self):
        self.write_source({"game/.gitmodules": '[submodule "torch"]\n path = torch\n url = https://github.com/example/torch.git\n'})
        dependency = self.root / "torch"
        dependency.mkdir()
        (dependency / "CMakeLists.txt").write_text('FetchContent_Declare(helper URL https://example.invalid/helper.tar.gz)')
        (dependency / ".git").write_text("gitdir: private metadata")
        record = self.service.export("project", {"submodule:torch": {"path": dependency, "revision": "a" * 40}})
        self.assertEqual(record["manifest"]["closure"]["missing_materials"], ["dependency:helper"])
        with zipfile.ZipFile(record["file"]) as bundle:
            self.assertFalse(any(name.endswith("/.git") for name in bundle.namelist()))

    def test_export_rejects_capture_tampering_missing_materials_and_symlinks(self):
        self.source.write_bytes(b"changed source")
        with self.assertRaisesRegex(Invalid, "checksum changed"):
            self.service.export("project")
        self.write_source({"game/main.cpp": "source"})
        with self.assertRaisesRegex(Invalid, "Missing or linked"):
            self.service.export("project", {"patch": self.root / "missing.patch"})
        actual = self.material("actual.patch")
        linked = self.root / "linked.patch"
        linked.symlink_to(actual)
        with self.assertRaisesRegex(Invalid, "Missing or linked"):
            self.service.export("project", {"patch": linked})

    def test_archive_audit_rejects_tampered_hashes_even_if_container_record_is_updated(self):
        record = self.service.export("project")
        path = Path(record["file"])
        with zipfile.ZipFile(path) as bundle:
            data = {name: bundle.read(name) for name in bundle.namelist()}
        data["recipe.json"] = b"modified recipe"
        with zipfile.ZipFile(path, "w") as bundle:
            for name, body in data.items():
                bundle.writestr(name, body)
        self.store.update("preservations", record["id"], sha256=checksum(path))
        with self.assertRaisesRegex(Invalid, "Invalid preservation archive member|checksum mismatch"):
            self.service.audit(record["id"])


if __name__ == "__main__":
    unittest.main()
