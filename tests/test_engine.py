import copy
import hashlib
import io
import json
import os
import tempfile
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from portkeeper.engine import Busy, Engine
from portkeeper.safety import Invalid, TARGETS, checksum, validate_recipe
from portkeeper.storage import Store, now


def archive_bytes(files):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, body in files.items():
            archive.writestr(name, body)
    return stream.getvalue()


class FakeGithub:
    """Local release transport that never opens a network connection."""

    def __init__(self):
        self.downloads = []
        self.calls = []
        self.payloads = {}
        self.set_version(1)

    def set_version(self, number):
        commit = f"{number:040x}"
        assets = []
        for index, target in enumerate(TARGETS):
            name = f"game-{target}.zip"
            url = f"https://github.com/example/game/releases/download/v{number}/{name}"
            payload = archive_bytes({"start.txt": f"{target} v{number}", "version.txt": f"v{number}"})
            self.payloads[url] = payload
            assets.append({"id": number * 10 + index, "name": name, "browser_download_url": url,
                           "updated_at": f"2026-10-{number:02d}T00:00:00Z", "size": len(payload)})
        self.payloads[f"https://codeload.github.com/example/game/zip/{commit}"] = archive_bytes({
            f"game-{commit}/start.txt": f"source v{number}", f"game-{commit}/version.txt": f"v{number}"})
        self.current = {"version": f"v{number}", "commit": commit, "release_id": number,
                        "url": f"https://github.com/example/game/releases/tag/v{number}", "assets": assets}

    def release(self, repo):
        self.calls.append(repo)
        return copy.deepcopy(self.current)

    def download(self, url, output):
        self.downloads.append(url)
        payload = self.payloads[url]
        Path(output).write_bytes(payload)
        return {"sha256": hashlib.sha256(payload).hexdigest(), "size": len(payload)}


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.github = FakeGithub()
        self.engine = Engine(self.root / "data", self.root / "inputs", self.root / "library", github=self.github)

    def tearDown(self):
        self.engine.close()
        self.temp.cleanup()

    def wait_project(self, project_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with self.engine.lock:
                busy = project_id in self.engine.busy
            if not busy:
                return self.engine.store.get("projects", project_id)
            time.sleep(0.01)
        self.fail(f"Timed out waiting for project {project_id}")

    def create_project(self, payload="release", targets=None, inputs=None, auto_build=False):
        recipe = {"version": 1, "mode": "assemble", "payload": {"kind": payload},
                  "inputs": inputs or [], "expected_files": ["start.txt", "version.txt"]}
        if payload == "release":
            recipe["payload"]["asset_patterns"] = {target: f"^game-{target}\\.zip$" for target in TARGETS}
        if inputs:
            recipe["expected_files"].extend(item["destination"] for item in inputs)
        return self.engine.create_project({"name": "Example game", "repo": "example/game", "targets": targets or list(TARGETS),
                                           "recipe": recipe, "auto_build": auto_build})

    def capture(self, project):
        job = self.engine.check(project["id"])["job"]
        self.wait_project(project["id"])
        job = self.engine.store.get("jobs", job["id"])
        self.assertEqual(job["status"], "archived", job.get("error"))
        return self.engine.store.get("projects", project["id"])

    def build(self, project, targets=None):
        result = self.engine.build(project["id"], targets)
        self.wait_project(project["id"])
        return [self.engine.store.get("jobs", job["id"]) for job in result["jobs"]]

    def artifact_for(self, project, target="linux", version=None):
        return next(item for item in self.engine.store.all("artifacts") if item["project_id"] == project["id"]
                    and item["target"] == target and (version is None or item["version"] == version))

    def assert_library_unchanged(self, published, before):
        self.assertEqual(Path(published["published_path"]).read_bytes(), before)
        self.assertTrue(self.engine.store.get("artifacts", published["id"])["published"])
        self.assertFalse(list(self.engine.library_root.glob("*.part")))

    def test_demo_produces_three_packages_with_honest_manifests(self):
        result = self.engine.demo()
        project = self.wait_project(result["project"]["id"])
        self.assertEqual(project["status"], "ready")
        artifacts = [item for item in self.engine.store.all("artifacts") if item["project_id"] == project["id"]]
        self.assertEqual({item["target"] for item in artifacts}, set(TARGETS))
        self.assertEqual(len(artifacts), 3)
        for artifact in artifacts:
            self.assertEqual(checksum(artifact["file"]), artifact["sha256"])
            with zipfile.ZipFile(artifact["file"]) as bundle:
                manifest = json.loads(bundle.read("portkeeper-manifest.json"))
                self.assertTrue(manifest["demo"])
                self.assertIn("no launch", manifest["validation"])
                self.assertIn(b"NOT A ROM", bundle.read("assets/demo-assets.bin"))

    def test_single_file_release_assets_are_packaged_under_a_stable_name(self):
        appimage = b"\x7fELF synthetic AppImage"
        asset = self.github.current["assets"][0]
        asset["name"], asset["size"] = "Game-v1-linux-x86_64.AppImage", len(appimage)
        self.github.payloads[asset["browser_download_url"]] = appimage
        recipe = {"version": 1, "mode": "assemble", "inputs": [], "expected_files": ["Game.AppImage"],
                  "payload": {"kind": "release", "single_file_name": "Game.AppImage",
                              "asset_patterns": {"linux": r"^Game-v[0-9]+-linux-x86_64\.AppImage$"}}}
        project = self.capture(self.engine.create_project(
            {"name": "Single file", "repo": "example/game", "targets": ["linux"], "recipe": recipe}))
        [job] = self.build(project, ["linux"])
        self.assertEqual(job["status"], "ready", job.get("error"))
        with zipfile.ZipFile(self.artifact_for(project)["file"]) as bundle:
            self.assertEqual(bundle.read("Game.AppImage"), appimage)
            self.assertTrue(bundle.getinfo("Game.AppImage").external_attr >> 16 & 0o100)

    def test_single_file_name_must_stay_inside_the_package(self):
        for name in ("../escape", "/abs", "portkeeper-manifest.json", ""):
            with self.subTest(name=name), self.assertRaises(Invalid):
                validate_recipe({"version": 1, "mode": "assemble", "inputs": [], "expected_files": ["x"],
                                 "payload": {"kind": "release", "single_file_name": name}})

    def test_capture_deduplicates_and_builds_target_specific_assets(self):
        project = self.capture(self.create_project())
        captures = self.engine.store.all("captures")
        self.assertEqual(len(captures), 1)
        self.assertEqual(len(captures[0]["assets"]), 3)
        self.assertEqual(len(self.github.downloads), 4)
        project = self.capture(project)
        self.assertEqual(len(self.engine.store.all("captures")), 1)
        self.assertEqual(len(self.github.downloads), 4)
        jobs = self.build(project)
        self.assertEqual([item["status"] for item in jobs], ["ready"] * 3)
        for target in TARGETS:
            artifact = self.artifact_for(project, target)
            with zipfile.ZipFile(artifact["file"]) as bundle:
                self.assertEqual(bundle.read("start.txt"), f"{target} v1".encode())
                manifest = json.loads(bundle.read("portkeeper-manifest.json"))
                self.assertEqual(manifest["target"], target)
                self.assertEqual(manifest["capture_id"], project["capture_id"])

    def test_source_build_flattens_github_top_level_directory(self):
        project = self.capture(self.create_project(payload="source", targets=["linux"]))
        jobs = self.build(project)
        self.assertEqual(jobs[0]["status"], "ready", jobs[0].get("error"))
        with zipfile.ZipFile(self.artifact_for(project)["file"]) as bundle:
            self.assertEqual(bundle.read("start.txt"), b"source v1")

    def test_verified_user_input_recorded_in_manifest(self):
        body = b"supplied synthetic game asset; not a ROM"
        source = self.engine.input_root / "games/synthetic.bin"
        source.parent.mkdir()
        source.write_bytes(body)
        digest = checksum(source)
        inputs = [{"path": "games/synthetic.bin", "destination": "assets/game.bin", "sha256": digest}]
        project = self.capture(self.create_project(inputs=inputs, targets=["linux"]))
        jobs = self.build(project)
        self.assertEqual(jobs[0]["status"], "ready", jobs[0].get("error"))
        with zipfile.ZipFile(self.artifact_for(project)["file"]) as bundle:
            self.assertEqual(bundle.read("assets/game.bin"), body)
            manifest = json.loads(bundle.read("portkeeper-manifest.json"))
            self.assertEqual(manifest["inputs"][0]["sha256"], digest)

    def test_failed_new_version_input_keeps_capture_and_previous_published_package(self):
        source = self.engine.input_root / "game.bin"
        source.write_bytes(b"owned synthetic input")
        inputs = [{"path": "game.bin", "destination": "assets/game.bin", "sha256": checksum(source)}]
        project = self.capture(self.create_project(inputs=inputs, targets=["linux"]))
        self.assertEqual(self.build(project)[0]["status"], "ready")
        published = self.engine.publish(self.artifact_for(project)["id"])
        before = Path(published["published_path"]).read_bytes()
        source.write_bytes(b"wrong revision")
        self.github.set_version(2)
        project = self.capture(project)
        jobs = self.build(project)
        self.assertEqual(jobs[0]["status"], "failed")
        self.assertIn("SHA-256 mismatch", jobs[0]["error"])
        self.assertEqual(len(self.engine.store.all("captures")), 2)
        self.assertEqual(len(self.engine.store.all("artifacts")), 1)
        self.assertEqual(self.engine.store.get("projects", project["id"])["status"], "needs_attention")
        self.assert_library_unchanged(published, before)

    def test_input_symlink_is_rejected_without_replacing_current_package(self):
        source = self.engine.input_root / "game.bin"
        source.write_bytes(b"fixture")
        inputs = [{"path": "game.bin", "destination": "assets/game.bin", "sha256": checksum(source)}]
        project = self.capture(self.create_project(inputs=inputs, targets=["linux"]))
        self.assertEqual(self.build(project)[0]["status"], "ready")
        published = self.engine.publish(self.artifact_for(project)["id"])
        before = Path(published["published_path"]).read_bytes()
        other = self.root / "outside.bin"
        other.write_bytes(b"fixture")
        source.unlink()
        source.symlink_to(other)
        jobs = self.build(project)
        self.assertEqual(jobs[0]["status"], "failed")
        self.assertIn("Symlinks", jobs[0]["error"])
        self.assert_library_unchanged(published, before)

    def test_docker_output_ancestor_symlink_cannot_package_host_files(self):
        private = self.root / "private-host-directory/out/private.txt"
        private.parent.mkdir(parents=True)
        private.write_bytes(b"private host bytes must not enter any package")
        project = self.create_project(payload="source", targets=["linux"])
        recipe = {"version": 1, "mode": "docker", "payload": {"kind": "source"},
                  "image": "synthetic-builder@sha256:" + "a" * 64, "command": ["synthetic-build"],
                  "output_dir": "nested/out", "expected_files": ["private.txt"]}
        project = self.engine.update_project(project["id"], {"recipe": recipe})
        project = self.capture(project)

        def malicious_build(recipe, workspace, job_id, log):
            (workspace / "nested").symlink_to(private.parent.parent, target_is_directory=True)

        with patch.object(self.engine, "_docker", side_effect=malicious_build):
            jobs = self.build(project)
        self.assertEqual(jobs[0]["status"], "failed")
        self.assertIn("Symlinks", jobs[0]["error"])
        self.assertEqual(self.engine.store.all("artifacts"), [])
        self.assertEqual(private.read_bytes(), b"private host bytes must not enter any package")
        self.assertFalse((private.parent / "portkeeper-manifest.json").exists())
        self.assertEqual(list(self.engine.work_root.iterdir()), [])

    def test_recipe_traversal_edit_is_rejected_and_preserves_package(self):
        project = self.capture(self.create_project(targets=["linux"]))
        self.assertEqual(self.build(project)[0]["status"], "ready")
        published = self.engine.publish(self.artifact_for(project)["id"])
        before = Path(published["published_path"]).read_bytes()
        recipe = copy.deepcopy(project["recipe"])
        recipe["inputs"] = [{"path": "../secret", "destination": "asset", "sha256": "a" * 64}]
        with self.assertRaises(Invalid):
            self.engine.update_project(project["id"], {"recipe": recipe})
        self.assertEqual(self.engine.store.get("projects", project["id"])["recipe"], project["recipe"])
        self.assert_library_unchanged(published, before)

    def test_publish_latest_and_rollback_are_atomic(self):
        project = self.capture(self.create_project(targets=["linux"]))
        self.build(project)
        old = self.engine.publish(self.artifact_for(project)["id"])
        old_bytes = Path(old["published_path"]).read_bytes()
        self.github.set_version(2)
        project = self.capture(project)
        self.build(project)
        latest = self.artifact_for(project, version="v2")
        original_replace = os.replace
        observed = []

        def inspect_replace(source, destination):
            observed.append((Path(destination).read_bytes(), Path(source).read_bytes()))
            return original_replace(source, destination)

        with patch("portkeeper.engine.os.replace", side_effect=inspect_replace):
            published = self.engine.publish(latest["id"])
        self.assertEqual(len(observed), 1)
        self.assertEqual(observed[0][0], old_bytes)
        self.assertEqual(observed[0][1], Path(latest["file"]).read_bytes())
        self.assertFalse(self.engine.store.get("artifacts", old["id"])["published"])
        rolled_back = self.engine.publish(old["id"])
        self.assertEqual(rolled_back["published_path"], published["published_path"])
        self.assertEqual(Path(rolled_back["published_path"]).read_bytes(), old_bytes)
        self.assertFalse(self.engine.store.get("artifacts", latest["id"])["published"])

    def test_target_overrides_select_platform_files_and_preserve_partial_success(self):
        launchers = {"linux": "game", "windows": "game.exe", "handheld": "game.sh"}
        for asset in self.github.current["assets"]:
            target = next(target for target in TARGETS if target in asset["name"])
            body = archive_bytes({launchers[target]: f"launcher for {target}"})
            self.github.payloads[asset["browser_download_url"]] = body
            asset["size"] = len(body)
        project = self.create_project()
        recipe = copy.deepcopy(project["recipe"])
        recipe["expected_files"] = ["base-file-is-not-in-release"]
        recipe["target_overrides"] = {target: {"expected_files": [path]} for target, path in launchers.items()}
        project = self.engine.update_project(project["id"], {"recipe": recipe})
        project = self.capture(project)
        jobs = self.build(project)
        self.assertTrue(all(job["status"] == "ready" for job in jobs), jobs)
        for target, launcher in launchers.items():
            artifact = self.artifact_for(project, target)
            with zipfile.ZipFile(artifact["file"]) as bundle:
                self.assertEqual(bundle.read(launcher), f"launcher for {target}".encode())
                effective = json.loads(bundle.read("portkeeper-manifest.json"))["recipe"]
                self.assertEqual(effective["expected_files"], [launcher])
                self.assertNotIn("target_overrides", effective)
        recipe["target_overrides"]["windows"]["expected_files"] = ["missing.exe"]
        project = self.engine.update_project(project["id"], {"recipe": recipe})
        jobs = {job["target"]: job for job in self.build(project)}
        self.assertEqual(jobs["windows"]["status"], "failed")
        self.assertIn("Missing file", jobs["windows"]["error"])
        self.assertEqual(jobs["linux"]["status"], "ready")
        self.assertEqual(jobs["handheld"]["status"], "ready")

    def test_project_rename_retains_single_current_library_filename(self):
        project = self.capture(self.create_project(targets=["linux"]))
        old_job = self.build(project)[0]
        original = self.engine.publish(old_job["artifact_id"])
        project = self.engine.update_project(project["id"], {"name": "A new game title"})
        new_job = self.build(project)[0]
        self.assertEqual(new_job["status"], "ready")
        renamed = self.engine.publish(new_job["artifact_id"])
        self.assertEqual(renamed["published_path"], original["published_path"])
        self.assertEqual(list(self.engine.library_root.glob("*.zip")), [Path(original["published_path"])])
        self.assertEqual(checksum(renamed["published_path"]), renamed["sha256"])
        self.assertEqual(renamed["project_name"], "A new game title")
        self.assertFalse(self.engine.store.get("artifacts", original["id"])["published"])

    def test_tampered_archive_and_failed_copy_leave_current_package_untouched(self):
        project = self.capture(self.create_project(targets=["linux"]))
        self.build(project)
        published = self.engine.publish(self.artifact_for(project)["id"])
        before = Path(published["published_path"]).read_bytes()
        self.github.set_version(2)
        project = self.capture(project)
        self.build(project)
        latest = self.artifact_for(project, version="v2")
        archive = Path(latest["file"])
        original = archive.read_bytes()
        archive.write_bytes(b"tampered")
        with self.assertRaisesRegex(Invalid, "checksum changed"):
            self.engine.publish(latest["id"])
        self.assert_library_unchanged(published, before)
        archive.write_bytes(original)
        with patch("portkeeper.engine.shutil.copyfile", side_effect=OSError("disk failure")):
            with self.assertRaisesRegex(OSError, "disk failure"):
                self.engine.publish(latest["id"])
        self.assert_library_unchanged(published, before)

    def test_capture_checksum_tampering_prevents_build(self):
        project = self.capture(self.create_project(payload="source", targets=["linux"]))
        capture = self.engine.store.get("captures", project["capture_id"])
        (Path(capture["root"]) / capture["source"]["file"]).write_bytes(b"tampered source")
        jobs = self.build(project)
        self.assertEqual(jobs[0]["status"], "failed")
        self.assertIn("source checksum changed", jobs[0]["error"])
        self.assertEqual(self.engine.store.all("artifacts"), [])

    def test_auto_build_only_on_new_capture(self):
        project = self.create_project(targets=["linux"], auto_build=True)
        self.capture(project)
        self.assertEqual(len(self.engine.store.all("artifacts")), 1)
        self.assertFalse(self.engine.store.all("artifacts")[0]["published"])
        self.assertEqual(list(self.engine.library_root.glob("*.zip")), [])
        self.capture(project)
        self.assertEqual(len(self.engine.store.all("artifacts")), 1)
        self.github.set_version(2)
        self.capture(project)
        self.assertEqual(len(self.engine.store.all("artifacts")), 2)

    def test_unattended_publication_advances_successful_targets_and_preserves_failed_current(self):
        project = self.create_project(auto_build=True)
        with self.assertRaisesRegex(Invalid, "automatic builds"):
            self.engine.update_project(project["id"], {"auto_build": False, "auto_publish": True})
        project = self.engine.update_project(project["id"], {"auto_publish": True, "auto_publish_destination": "library"})
        project = self.capture(project)
        old = {target: self.artifact_for(project, target, "v1") for target in TARGETS}
        self.assertTrue(all(artifact["published"] for artifact in old.values()))
        original_bytes = {target: Path(artifact["published_path"]).read_bytes() for target, artifact in old.items()}
        self.assertEqual(len(list(self.engine.library_root.glob("*.zip"))), 3)

        recipe = copy.deepcopy(project["recipe"])
        recipe["target_overrides"] = {"windows": {"expected_files": ["missing-new-windows-file.exe"]}}
        project = self.engine.update_project(project["id"], {"recipe": recipe})
        self.github.set_version(2)
        project = self.capture(project)
        self.assertEqual(project["status"], "needs_attention")
        self.assertEqual(len(self.engine.store.all("captures")), 2)
        self.assertEqual(len(self.engine.store.all("artifacts")), 5)
        failed = next(job for job in self.engine.store.all("jobs") if job["project_id"] == project["id"]
                      and job["target"] == "windows" and job["version"] == "v2")
        self.assertEqual(failed["status"], "failed")
        self.assertIn("Missing file", failed["error"])
        self.assert_library_unchanged(old["windows"], original_bytes["windows"])
        for target in ("linux", "handheld"):
            promoted = self.artifact_for(project, target, "v2")
            self.assertTrue(promoted["published"])
            self.assertEqual(promoted["published_path"], old[target]["published_path"])
            self.assertEqual(checksum(promoted["published_path"]), promoted["sha256"])
            self.assertNotEqual(Path(promoted["published_path"]).read_bytes(), original_bytes[target])
            self.assertFalse(self.engine.store.get("artifacts", old[target]["id"])["published"])
        self.assertEqual(len(list(self.engine.library_root.glob("*.zip"))), 3)

    def test_failed_asset_capture_preserves_source_and_blocks_incomplete_build(self):
        project = self.create_project()
        download = self.github.download

        def fail_release_asset(url, output):
            if "/releases/download/" in url:
                raise OSError("upstream asset disappeared")
            return download(url, output)

        with patch.object(self.github, "download", side_effect=fail_release_asset):
            job = self.engine.check(project["id"])["job"]
            project = self.wait_project(project["id"])
        job = self.engine.store.get("jobs", job["id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(project["status"], "needs_attention")
        self.assertIsNotNone(project["capture_id"])
        capture = self.engine.store.get("captures", project["capture_id"])
        self.assertFalse(capture["complete"])
        source = Path(capture["root"]) / capture["source"]["file"]
        self.assertEqual(checksum(source), capture["source"]["sha256"])
        self.assertTrue(zipfile.is_zipfile(source))
        self.assertEqual(json.loads((Path(capture["root"]) / "capture.json").read_text())["id"], capture["id"])
        with self.assertRaisesRegex(Invalid, "incomplete"):
            self.engine.build(project["id"])
        self.assertEqual(self.engine.store.all("artifacts"), [])
        restored = self.capture(project)
        self.assertEqual(restored["capture_id"], capture["id"])
        self.assertEqual(sum("codeload.github.com" in url for url in self.github.downloads), 1)
        self.assertTrue(self.engine.store.get("captures", restored["capture_id"])["complete"])
        self.assertTrue(all(job["status"] == "ready" for job in self.build(restored)))

    def test_busy_project_rejects_simultaneous_jobs_and_edits(self):
        project = self.create_project()
        self.engine.claim(project["id"])
        try:
            with self.assertRaises(Busy):
                self.engine.check(project["id"])
            with self.assertRaises(Busy):
                self.engine.update_project(project["id"], {"name": "Renamed"})
        finally:
            self.engine.release_claim(project["id"])

    def test_restart_marks_interrupted_jobs_and_project_as_needing_attention(self):
        project = self.create_project()
        job = self.engine.job(project, "linux")
        self.engine.store.update("jobs", job["id"], status="building")
        self.engine.store.update("projects", project["id"], status="building")
        self.engine.close()
        self.engine = Engine(self.root / "data", self.root / "inputs", self.root / "library", github=self.github)
        interrupted = self.engine.store.get("jobs", job["id"])
        self.assertEqual(interrupted["status"], "failed")
        self.assertIsNotNone(interrupted["finished_at"])
        self.assertIn("restarted", interrupted["error"])
        self.assertEqual(self.engine.store.get("projects", project["id"])["status"], "needs_attention")
        self.assertEqual(self.engine.busy, set())


class StorageTests(unittest.TestCase):
    def test_updated_records_and_bounded_logs_survive_new_store_instance(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.sqlite3"
            store = Store(path)
            store.put("jobs", {"id": "job", "status": "queued", "logs": []})
            store.update("jobs", "job", status="ready", finished_at=now())
            for number in range(505):
                store.append_log("job", f"message {number}")
            restored = Store(path).get("jobs", "job")
            self.assertEqual(restored["status"], "ready")
            self.assertEqual(len(restored["logs"]), 500)
            self.assertTrue(restored["logs"][-1].endswith("message 504"))


if __name__ == "__main__":
    unittest.main()
