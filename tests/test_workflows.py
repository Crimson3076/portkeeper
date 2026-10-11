"""Cross-module workflows use synthetic data and never contact upstream services."""
import copy
import hashlib
import http.client
import json
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from portkeeper.engine import Engine
from portkeeper.profiles import ensure_release, profile, supported_hashes
from portkeeper.safety import Invalid, checksum
from portkeeper.server import Handler, make_server
from portkeeper.storage import Store
from test_engine import FakeGithub, archive_bytes


class ProfileGithub(FakeGithub):
    def __init__(self):
        self.downloads, self.calls, self.payloads = [], [], {}
        spec = profile("shipwright")
        commit, version, repo = spec["source_commit"], spec["tested_release"], spec["repository"]
        assets = []
        for number, (target, runtime) in enumerate(spec["runtimes"].items()):
            payload = b"synthetic AppImage fixture" if target == "linux" else archive_bytes({
                "soh.exe": b"synthetic Windows executable", "soh.o2r": b"synthetic port assets"})
            self.payloads[runtime["url"]] = payload
            assets.append({"id": number + 1, "name": runtime["name"], "browser_download_url": runtime["url"],
                           "updated_at": "fixture-update", "size": len(payload)})
        source_url = f"https://codeload.github.com/{repo}/zip/{commit}"
        self.payloads[source_url] = archive_bytes({"fixture/CMakeLists.txt": "project(Ship VERSION 9.3.0)"})
        self.current = {"version": version, "commit": commit, "release_id": 93, "assets": assets,
                        "url": spec["release_url"]}
        self.tagged_release = copy.deepcopy(self.current)

    def release_tag(self, repository, version):
        self.calls.append(f"{repository}@{version}")
        if version != self.tagged_release["version"]:
            raise Invalid("Fixture tag not available")
        return copy.deepcopy(self.tagged_release)

    def set_unverified_release(self):
        newer = copy.deepcopy(self.tagged_release)
        newer.update(version="9.4.0", commit="f" * 40, release_id=94)
        for asset in newer["assets"]:
            old_url = asset["browser_download_url"]
            asset["browser_download_url"] = old_url.replace("/9.3.0/", "/9.4.0/")
            asset["id"] += 100
            self.payloads[asset["browser_download_url"]] = self.payloads[old_url]
        repository = profile("shipwright")["repository"]
        self.payloads[f"https://codeload.github.com/{repository}/zip/{newer['commit']}"] = archive_bytes({
            "fixture-newer/CMakeLists.txt": "project(Ship VERSION 9.4.0)"})
        self.current = newer


class FixtureProfileRunner:
    def __init__(self, engine):
        self.helper = engine.shipwright.helper
        self.builds = []

    def availability(self):
        return {"enabled": True, "ready": True, "missing_tools": []}

    def capture_dependencies(self, stage):
        stage("capturing_dependencies", "Synthetic pinned dependencies already available")
        return {}

    def build(self, project, capture, job, workspace, stage):
        ensure_release(project["profile"], capture["version"], capture["commit"])
        self.builds.append(job["target"])
        stage("extracting", "Synthetic fixture extraction; no game or ROM")
        stage("packaging_" + job["target"], "Packaging the synthetic fixture")
        folder = workspace / "package"
        folder.mkdir()
        (folder / "oot.o2r").write_bytes(b"synthetic extracted assets")
        (folder / "launcher.txt").write_text(job["target"])
        (folder / "portkeeper-manifest.json").write_text(json.dumps({
            "target": job["target"], "validation": {"gameplay": "not tested"}, "fixture": True}))
        return folder, {"fixture": True}


class WorkflowFixture:
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.github = FakeGithub()
        self.engine = Engine(self.root / "data", self.root / "inputs", self.root / "library", github=self.github)
        self.addCleanup(self.engine.close)

    def wait_project(self, project_id):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            with self.engine.lock:
                busy = project_id in self.engine.busy
            if not busy:
                return self.engine.store.get("projects", project_id)
            time.sleep(0.01)
        self.fail("Worker did not finish before its test timeout")

    def generic_project(self):
        return self.engine.create_project({"name": "Fixture game", "repo": "example/game", "targets": ["linux", "windows"],
            "recipe": {"version": 1, "mode": "assemble", "expected_files": ["start.txt"],
                       "payload": {"kind": "release", "asset_patterns": {
                           "linux": r"^game-linux[.]zip$", "windows": r"^game-windows[.]zip$"}}}})


class WorkerWorkflowTests(WorkflowFixture, unittest.TestCase):
    def test_profile_project_uses_verified_repository_and_rom_hashes(self):
        spec = profile("shipwright")
        revision = next(iter(supported_hashes()))
        origin = {"kind": "local", "path": "owned/oot.z64", "sha1": revision}
        project = self.engine.create_project({"profile": spec["id"], "targets": spec["targets"], "rom_source": origin})
        self.assertEqual(project["name"], spec["name"])
        self.assertEqual(project["repo"], spec["repository"])
        self.assertEqual(project["recipe"]["mode"], "profile")
        self.assertEqual(project["rom_source"], origin)
        for config in ({"targets": ["handheld"]}, {"repo": "untrusted/other"},
                       {"rom_source": {**origin, "sha1": "0" * 40}},
                       {"rom_source": {**origin, "path": "../private.z64"}}):
            with self.subTest(config=config), self.assertRaises(Invalid):
                self.engine.create_project({"profile": "shipwright", **config})
        self.assertEqual(len(self.engine.store.all("projects")), 1)

    def test_curated_profile_rejects_execution_changes_and_raw_profile_recipes(self):
        project = self.engine.create_project({"profile": "shipwright", "targets": ["linux"]})
        canonical = copy.deepcopy(project["recipe"])
        changes = ({"expected_files": ["different-assets.o2r"]}, {"payload": {"kind": "source"}},
                   {"command": ["arbitrary-command"]}, {"profile": "unverified-profile"},
                   {"mode": "docker", "image": "fixture@sha256:" + "a" * 64, "command": ["arbitrary-command"]})
        for change in changes:
            with self.subTest(change=change), self.assertRaises(Invalid):
                self.engine.update_project(project["id"], {"recipe": {**canonical, **change}})
            self.assertEqual(self.engine.store.get("projects", project["id"])["recipe"], canonical)
        with self.assertRaises(Invalid):
            self.engine.create_project({"name": "Unbound profile", "repo": "example/game", "recipe": canonical})
        self.assertEqual(len(self.engine.store.all("projects")), 1)

    def test_profile_proposals_reject_execution_changes_and_apply_only_notes(self):
        project = self.engine.create_project({"profile": "shipwright", "targets": ["linux"]})
        changed = {**project["recipe"], "expected_files": ["new-assets.o2r"]}
        draft = {"recipe": changed, "explanation": "Unverified change", "requires_review": True, "executed": False}
        with patch.object(self.engine.assistant, "draft", return_value=draft):
            proposal = self.engine.recipe_proposal(project["id"])
        with self.assertRaises(Invalid):
            self.engine.apply_proposal(proposal["id"])
        self.assertEqual(self.engine.store.get("projects", project["id"])["recipe"], project["recipe"])
        self.assertEqual(self.engine.store.get("proposals", proposal["id"])["status"], "pending_review")
        notes = {**project["recipe"], "notes": "Use the verified profile for 9.3.0; newer releases need review."}
        with patch.object(self.engine.assistant, "draft", return_value={**draft, "recipe": notes}):
            proposal = self.engine.recipe_proposal(project["id"])
        applied = self.engine.apply_proposal(proposal["id"])
        self.assertEqual(applied["recipe"], notes)
        self.assertEqual(self.engine.store.get("proposals", proposal["id"])["status"], "applied")

    def test_partial_capture_retry_reuses_verified_downloads_and_persists_stages(self):
        project = self.generic_project()
        download = self.github.download
        windows_url = next(asset["browser_download_url"] for asset in self.github.current["assets"] if "windows" in asset["name"])

        def fail_windows(url, output):
            if url == windows_url:
                raise Invalid("Fixture release asset temporarily unavailable")
            return download(url, output)

        with patch.object(self.github, "download", side_effect=fail_windows):
            failed_id = self.engine.check(project["id"])["job"]["id"]
            project = self.wait_project(project["id"])
        failed = self.engine.store.get("jobs", failed_id)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["stage"], "preserving_assets")
        persisted = Store(self.engine.store.path).get("jobs", failed_id)
        self.assertEqual(persisted["checkpoints"], failed["checkpoints"])
        original_capture = project["capture_id"]
        self.assertEqual(len(self.github.downloads), 2)
        retried = self.engine.retry(failed_id)["job"]
        project = self.wait_project(project["id"])
        self.assertEqual(project["capture_id"], original_capture)
        self.assertEqual(len(self.engine.store.all("captures")), 1)
        self.assertEqual(len(self.github.downloads), 3)
        self.assertEqual(self.github.downloads.count(windows_url), 1)
        retried = self.engine.store.get("jobs", retried["id"])
        self.assertEqual(retried["status"], "archived")
        self.assertEqual(retried["stage"], "complete")
        self.assertTrue(all(checkpoint["status"] == "complete" for checkpoint in retried["checkpoints"]))
        with self.assertRaisesRegex(Invalid, "failed jobs"):
            self.engine.retry(retried["id"])

    def test_profile_test_build_packages_both_targets_and_keeps_gameplay_unconfirmed(self):
        self.github = ProfileGithub()
        self.engine.github = self.github
        runner = FixtureProfileRunner(self.engine)
        self.engine.shipwright = runner
        spec = profile("shipwright")
        project = self.engine.create_project({"profile": spec["id"], "targets": spec["targets"]})
        capture_job = self.engine.test_build(project["id"])["job"]
        project = self.wait_project(project["id"])
        self.assertIn(f"{spec['repository']}@{spec['tested_release']}", self.github.calls)
        self.assertEqual(project["status"], "ready", project["last_error"])
        self.assertEqual(self.engine.store.get("jobs", capture_job["id"])["status"], "archived")
        self.assertEqual(set(runner.builds), {"linux", "windows"})
        artifacts = self.engine.store.all("artifacts")
        self.assertEqual(len(artifacts), 2)
        for artifact in artifacts:
            self.assertEqual(artifact["validation_level"], "packaged")
            self.assertFalse(artifact["published"])
            self.assertEqual(checksum(artifact["file"]), artifact["sha256"])
            with zipfile.ZipFile(artifact["file"]) as bundle:
                manifest_name = next(name for name in bundle.namelist() if name.endswith("portkeeper-manifest.json"))
                manifest = json.loads(bundle.read(manifest_name))
                self.assertEqual(manifest["profile"], "shipwright")
                self.assertEqual(manifest["validation"]["gameplay"], "not tested")
                self.assertTrue(manifest["pipeline"]["fixture"])
                self.assertFalse(any(Path(name).suffix in (".z64", ".v64", ".n64") for name in bundle.namelist()))

    def test_newer_release_is_preserved_while_default_build_uses_compatible_capture(self):
        transport = ProfileGithub()
        self.engine.github = transport
        runner = FixtureProfileRunner(self.engine)
        self.engine.shipwright = runner
        project = self.engine.create_project({"profile": "shipwright", "targets": ["linux", "windows"]})
        self.engine.test_build(project["id"])
        project = self.wait_project(project["id"])
        verified = project["compatible_capture_id"]
        transport.set_unverified_release()
        capture_job = self.engine.check(project["id"])["job"]
        project = self.wait_project(project["id"])
        latest = project["capture_id"]
        self.assertNotEqual(latest, verified)
        self.assertEqual(project["latest_version"], "9.4.0")
        self.assertEqual(project["compatible_capture_id"], verified)
        self.assertTrue(self.engine.store.get("captures", latest)["complete"])
        self.assertEqual(self.engine.store.get("jobs", capture_job["id"])["status"], "failed")
        result = self.engine.build(project["id"], ["linux"])
        project = self.wait_project(project["id"])
        job = self.engine.store.get("jobs", result["jobs"][0]["id"])
        self.assertEqual(job["status"], "ready")
        self.assertEqual(job["capture_id"], verified)
        self.assertEqual(job["version"], "9.3.0")
        self.assertEqual(project["capture_id"], latest)
        self.assertEqual(project["latest_version"], "9.4.0")
        result = self.engine.build(project["id"], ["windows"], capture_id=verified)
        self.wait_project(project["id"])
        self.assertEqual(self.engine.store.get("jobs", result["jobs"][0]["id"])["status"], "ready")
        with self.assertRaises(Invalid):
            self.engine.build(project["id"], capture_id=latest)
        foreign = {**self.engine.store.get("captures", verified), "id": "0" * 32, "project_id": "other-project"}
        self.engine.store.put("captures", foreign)
        with self.assertRaisesRegex(Invalid, "another project"):
            self.engine.build(project["id"], capture_id=foreign["id"])
        self.engine.store.update("captures", verified, complete=False)
        with self.assertRaisesRegex(Invalid, "incomplete"):
            self.engine.build(project["id"], capture_id=verified)

    def test_initial_tagged_profile_build_keeps_previously_captured_newer_release(self):
        transport = ProfileGithub()
        transport.set_unverified_release()
        self.engine.github = transport
        self.engine.shipwright = FixtureProfileRunner(self.engine)
        project = self.engine.create_project({"profile": "shipwright", "targets": ["linux"]})
        self.engine.check(project["id"])
        project = self.wait_project(project["id"])
        newer = project["capture_id"]
        self.assertEqual(project["latest_version"], "9.4.0")
        result = self.engine.test_build(project["id"])
        self.assertEqual(result["job"]["release_version"], "9.3.0")
        project = self.wait_project(project["id"])
        self.assertEqual(project["status"], "ready", project.get("last_error"))
        self.assertEqual(project["capture_id"], newer)
        self.assertEqual(project["latest_version"], "9.4.0")
        self.assertEqual(project["compatible_version"], "9.3.0")
        self.assertNotEqual(project["compatible_capture_id"], newer)
        self.assertEqual(len(self.engine.store.all("captures")), 2)
        self.assertEqual(self.engine.store.all("artifacts")[0]["version"], "9.3.0")

    def test_adding_windows_recaptures_profile_runtimes_without_replacing_newer_release(self):
        transport = ProfileGithub()
        self.engine.github = transport
        runner = FixtureProfileRunner(self.engine)
        self.engine.shipwright = runner
        project = self.engine.create_project({"profile": "shipwright", "targets": ["linux"]})
        self.engine.test_build(project["id"])
        project = self.wait_project(project["id"])
        linux_capture = self.engine.store.get("captures", project["compatible_capture_id"])
        spec = profile("shipwright")
        self.assertEqual([asset["name"] for asset in linux_capture["assets"]], [spec["runtimes"]["linux"]["name"]])

        transport.set_unverified_release()
        self.engine.check(project["id"])
        project = self.wait_project(project["id"])
        latest_capture = project["capture_id"]
        self.assertEqual(project["latest_version"], "9.4.0")
        project = self.engine.update_project(project["id"], {"targets": ["linux", "windows"]})
        self.assertIsNone(self.engine.compatible_capture(project))

        result = self.engine.test_build(project["id"])
        self.assertEqual(result["job"]["release_version"], spec["tested_release"])
        project = self.wait_project(project["id"])
        self.assertEqual(project["status"], "ready", project.get("last_error"))
        self.assertEqual(project["capture_id"], latest_capture)
        self.assertEqual(project["latest_version"], "9.4.0")
        expanded = self.engine.store.get("captures", project["compatible_capture_id"])
        self.assertNotEqual(expanded["id"], linux_capture["id"])
        self.assertEqual({asset["name"] for asset in expanded["assets"]},
                         {runtime["name"] for runtime in spec["runtimes"].values()})
        artifacts = [artifact for artifact in self.engine.store.all("artifacts")
                     if artifact["manifest"]["capture_id"] == expanded["id"]]
        self.assertEqual({artifact["target"] for artifact in artifacts}, {"linux", "windows"})
        self.assertEqual(runner.builds, ["linux", "linux", "windows"])

    def test_real_profile_runner_uses_synthetic_inputs_and_reuses_extracted_assets(self):
        self.engine.github = ProfileGithub()
        runner, helper = self.engine.shipwright, self.engine.shipwright.helper
        canonical = b"\x80\x37\x12\x40" + bytes(range(28))
        revision = hashlib.sha1(canonical).hexdigest()
        rom = self.engine.input_root / "fixture.z64"
        rom.write_bytes(canonical)
        source = self.root / "fixture-source"
        (source / "docs").mkdir(parents=True)
        (source / "soh/assets/yml/fixture").mkdir(parents=True)
        (source / "CMakeLists.txt").write_text("project(Ship VERSION 9.3.0 LANGUAGES C CXX)")
        (source / "docs/supportedHashes.json").write_text(json.dumps([{"sha1": revision, "name": "synthetic fixture"}]))
        (source / "soh/assets/yml/config.yml").write_text(f"{revision}:\n  path: fixture\n  config:\n    output:\n      binary: oot.o2r\n")
        binary = self.root / "fixture-extractor"
        binary.write_bytes(b"synthetic extractor, never executed")
        (runner.root / "extractor.json").write_text(json.dumps({"binary_sha256": checksum(binary), "fixture": True}))
        runtimes = {target: {**helper.RUNTIMES[target], "sha256": hashlib.sha256(
            self.engine.github.payloads[spec["url"]]).hexdigest()} for target, spec in profile("shipwright")["runtimes"].items()}
        calls = []

        def extract(arguments, log, cwd=None):
            calls.append(arguments)
            destination = Path(arguments[arguments.index("--dest") + 1])
            with zipfile.ZipFile(destination / "oot.o2r", "w") as bundle:
                bundle.writestr("portVersion", bytes.fromhex("01000900030000"))
                bundle.writestr("fixture-resource", b"synthetic resource")

        with patch("portkeeper.profiles.supported_hashes", return_value={revision: "synthetic fixture"}), \
             patch.object(runner, "prepare", return_value=(source, binary)), \
             patch.object(runner, "capture_dependencies", side_effect=lambda stage: stage("capturing_dependencies", "Fixtures")), \
             patch.object(runner, "_run", side_effect=extract), patch.object(helper, "RUNTIMES", runtimes):
            project = self.engine.create_project({"profile": "shipwright", "targets": ["linux", "windows"],
                "rom_source": {"kind": "local", "path": "fixture.z64", "sha1": revision}})
            self.engine.test_build(project["id"])
            project = self.wait_project(project["id"])
            self.assertEqual(project["status"], "ready", project.get("last_error"))
            self.assertEqual(len(calls), 1)
            for artifact in self.engine.store.all("artifacts"):
                with zipfile.ZipFile(artifact["file"]) as bundle:
                    names = bundle.namelist()
                    self.assertFalse(any(Path(name).suffix in (".z64", ".v64", ".n64") for name in names))
                    manifest = json.loads(bundle.read(next(name for name in names if name.endswith("portkeeper-manifest.json"))))
                    self.assertEqual(manifest["rom"]["sha1"], revision)
                    self.assertEqual(manifest["pipeline"]["rom_sha256"], checksum(rom))
                    self.assertEqual(manifest["validation"]["gameplay"], "not tested")
            rom.write_bytes(canonical[:-1] + b"\xff")
            failed_id = self.engine.build(project["id"], ["linux"])["jobs"][0]["id"]
            self.wait_project(project["id"])
            self.assertEqual(self.engine.store.get("jobs", failed_id)["status"], "failed")
            self.assertEqual(len(self.engine.store.all("artifacts")), 2)
            self.assertEqual(len(calls), 1)

    def test_failure_notifications_deduplicate_across_retries_and_remain_read(self):
        project = self.generic_project()
        message = "Unavailable fixture upstream: " + "x" * 1500
        with patch.object(self.github, "release", side_effect=Invalid(message)):
            first_id = self.engine.check(project["id"])["job"]["id"]
            self.wait_project(project["id"])
            notifications = self.engine.lifecycle.notifications(project["id"])
            self.assertEqual(len(notifications), 1)
            self.assertEqual(notifications[0]["kind"], "upstream_unavailable")
            self.assertLessEqual(len(notifications[0]["message"]), 1000)
            self.engine.lifecycle.mark_read(notifications[0]["id"])
            self.engine.retry(first_id)
            self.wait_project(project["id"])
        notifications = self.engine.lifecycle.notifications(project["id"])
        self.assertEqual(len(notifications), 1)
        self.assertTrue(notifications[0]["read"])
        self.assertEqual(self.engine.store.get("projects", project["id"])["status"], "needs_attention")

    def test_stale_assistant_proposal_does_not_overwrite_a_edited_recipe(self):
        project = self.generic_project()
        proposal_recipe = {**project["recipe"], "notes": "drafted proposal"}
        draft = {"recipe": proposal_recipe, "explanation": "Fixture proposal", "requires_review": True, "executed": False}
        with patch.object(self.engine.assistant, "draft", return_value=draft):
            proposal = self.engine.recipe_proposal(project["id"], "Fixture instruction")
        current = self.engine.update_project(project["id"], {"recipe": {**project["recipe"], "notes": "manual edit"}})
        with self.assertRaisesRegex(Invalid, "Recipe changed"):
            self.engine.apply_proposal(proposal["id"])
        self.assertEqual(self.engine.store.get("projects", project["id"])["recipe"], current["recipe"])
        self.assertEqual(self.engine.store.get("proposals", proposal["id"])["status"], "pending_review")
        with patch.object(self.engine.assistant, "draft", return_value=draft):
            fresh = self.engine.recipe_proposal(project["id"])
        applied = self.engine.apply_proposal(fresh["id"])
        self.assertEqual(applied["recipe"]["notes"], "drafted proposal")
        with self.assertRaisesRegex(Invalid, "already reviewed"):
            self.engine.apply_proposal(fresh["id"])

    def test_preservation_status_requires_matching_capture_toolchain_and_proof_scope(self):
        transport = ProfileGithub()
        self.engine.github = transport
        project = self.engine.create_project({"profile": "shipwright", "targets": ["linux"]})
        with patch.object(self.engine.shipwright, "capture_dependencies", side_effect=lambda stage: stage("capturing_dependencies", "Fixture sources")):
            self.engine.check(project["id"])
            project = self.wait_project(project["id"])
        captured = project["compatible_capture_id"]
        directory = self.engine.data / "toolchains"
        directory.mkdir()
        archive = directory / "fixture-toolchain.zip"
        original_toolchain = archive_bytes({"toolchain/manifest.json": '{"fixture": true}'})
        archive.write_bytes(original_toolchain)
        identity = "fixture-extractor@sha256:" + "a" * 64
        self.engine.configure_toolchain({"identity": identity, "material": archive.name})
        log = self.root / "offline-build.log"
        log.write_text("Synthetic fixture evidence: network disabled; extractor build completed.")
        materials = {"toolchain": {"path": archive, "revision": identity}, "offline-build-log": log}
        evidence = {"succeeded": True, "network_disabled": True, "log_material": "offline-build-log",
                    "command": ["cmake", "--build", "fixture"]}

        def export(scope):
            return self.engine.preservation.export(project["id"], capture_id=captured, materials=materials,
                toolchain={"material": "toolchain", "identity": identity}, offline_build=evidence,
                dependency_scope={"name": scope, "labels": []})

        unrelated = export("unrelated documentation renderer")
        self.assertTrue(unrelated["manifest"]["closure"]["offline_rebuild_complete"])
        self.assertFalse(self.engine.preservation_status(project["id"])["offline_rebuild_verified"])
        scope = "Shipwright 9.3.0 headless extraction and prebuilt runtime assembly"
        proof = export(scope)
        saved_proof = copy.deepcopy(proof)
        status = self.engine.preservation_status(project["id"])
        self.assertTrue(status["offline_rebuild_verified"])
        self.assertTrue(status["toolchain_archived"])
        self.assertIn(scope, status["note"])
        self.assertNotIn("complete_source_declarations", status["note"])

        self.engine.configure_toolchain({"identity": "different-toolchain", "material": archive.name})
        self.assertFalse(self.engine.preservation_status(project["id"])["offline_rebuild_verified"])
        self.engine.configure_toolchain({"identity": identity, "material": archive.name})
        self.assertTrue(self.engine.preservation_status(project["id"])["offline_rebuild_verified"])
        archive.write_bytes(archive_bytes({"toolchain/manifest.json": '{"fixture": "changed"}'}))
        status = self.engine.preservation_status(project["id"])
        self.assertFalse(status["offline_rebuild_verified"])
        self.assertFalse(status["toolchain_archived"])
        self.assertIn("checksum changed", status["toolchain_error"])
        self.engine.configure_toolchain({"identity": identity, "material": archive.name})
        self.assertFalse(self.engine.preservation_status(project["id"])["offline_rebuild_verified"])
        archive.write_bytes(original_toolchain)
        self.engine.configure_toolchain({"identity": identity, "material": archive.name})
        self.assertTrue(self.engine.preservation_status(project["id"])["offline_rebuild_verified"])

        transport.current['assets'][0]['updated_at'] = 'changed-release-asset-metadata'
        with patch.object(self.engine.shipwright, "capture_dependencies", side_effect=lambda stage: stage("capturing_dependencies", "Fixture sources")):
            self.engine.check(project["id"])
            current = self.wait_project(project["id"])
        self.assertNotEqual(current["compatible_capture_id"], captured)
        self.assertFalse(self.engine.preservation_status(project["id"])["offline_rebuild_verified"])
        self.assertEqual(self.engine.store.get("preservations", proof["id"]), saved_proof)
        audited = self.engine.preservation.audit(proof["id"])
        self.assertTrue(audited["integrity_verified"])
        self.assertTrue(audited["closure"]["offline_rebuild_complete"])
        self.assertEqual(audited["closure"]["scope"], scope)


class WorkflowHttpTests(WorkflowFixture, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.server = make_server(self.engine, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        patcher = patch.object(Handler, "log_message")
        patcher.start()
        self.addCleanup(patcher.stop)

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive())

    def request(self, method, path, body=None):
        headers = {"Host": f"127.0.0.1:{self.server.server_port}"}
        if body is not None:
            body = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        try:
            connection.request(method, path, body, headers)
            response = connection.getresponse()
            return response.status, json.loads(response.read())
        finally:
            connection.close()

    def test_profile_and_secret_configuration_routes_never_return_tokens(self):
        token = "rmm_workflowprivatetoken"
        status, result = self.request("POST", "/api/romm/config", {"url": "https://romm.example.invalid", "token": token})
        self.assertEqual(status, 200)
        self.assertTrue(result["has_token"])
        self.assertNotIn(token, json.dumps(result))
        self.assertEqual(self.engine.romm._config()["token"], token)
        self.assertEqual(self.engine.romm.secrets.stat().st_mode & 0o777, 0o600)
        status, result = self.request("POST", "/api/assistant/config", {"endpoint": "http://localhost:11434", "model": "fixture-model"})
        self.assertEqual(status, 200)
        self.assertTrue(result["local_only"])
        status, result = self.request("GET", "/api/profiles")
        self.assertEqual(status, 200)
        self.assertEqual(result["profiles"][0]["repository"], profile("shipwright")["repository"])
        status, result = self.request("POST", "/api/projects", {"profile": "shipwright", "targets": ["linux"]})
        self.assertEqual(status, 201)
        self.assertEqual(result["recipe"]["mode"], "profile")
        status, state = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertNotIn(token, json.dumps(state))
        self.assertTrue(state["romm"]["token_configured"])
        status, result = self.request("POST", "/api/romm/config", {"url": "https://romm.example.invalid", "clear_token": True})
        self.assertEqual(status, 200)
        self.assertFalse(result["has_token"])

    def test_toolchain_registration_is_confined_and_export_does_not_include_secrets(self):
        directory = self.engine.data / "toolchains"
        directory.mkdir()
        archive = directory / "fixture-toolchain.zip"
        archive.write_bytes(archive_bytes({"toolchain/manifest.json": '{"fixture": true}'}))
        status, record = self.request("POST", "/api/toolchains/config", {"identity": "fixture@sha256:" + "a" * 64, "material": archive.name})
        self.assertEqual(status, 200)
        self.assertEqual(record["sha256"], checksum(archive))
        status, _ = self.request("POST", "/api/toolchains/config", {"identity": "invalid", "material": "../private.zip"})
        self.assertEqual(status, 400)
        self.engine.romm.configure("https://romm.example.invalid", "rmm_neverexportthis")
        project = self.generic_project()
        self.engine.check(project["id"])
        self.wait_project(project["id"])
        status, result = self.request("POST", f"/api/projects/{project['id']}/preservation/export", {})
        self.assertEqual(status, 201)
        with zipfile.ZipFile(result["file"]) as bundle:
            self.assertFalse(any("secrets/" in name or "romm.json" in name for name in bundle.namelist()))
            self.assertFalse(any(b"rmm_neverexportthis" in bundle.read(name) for name in bundle.namelist()))
        self.assertFalse(result["manifest"]["closure"]["offline_rebuild_complete"])

    def test_changed_registered_toolchain_is_reported_and_cannot_be_exported(self):
        directory = self.engine.data / "toolchains"
        directory.mkdir()
        archive = directory / "fixture-toolchain.zip"
        archive.write_bytes(archive_bytes({"toolchain/manifest.json": '{"version": 1}'}))
        status, registered = self.request("POST", "/api/toolchains/config", {
            "identity": "fixture-toolchain-v1", "material": archive.name})
        self.assertEqual(status, 200)
        project = self.generic_project()
        self.engine.check(project["id"])
        self.wait_project(project["id"])
        status, report = self.request("GET", f"/api/projects/{project['id']}/preservation")
        self.assertEqual(status, 200)
        self.assertTrue(report["toolchain_archived"])

        archive.write_bytes(archive_bytes({"toolchain/manifest.json": '{"version": 2}'}))
        self.assertNotEqual(checksum(archive), registered["sha256"])
        status, report = self.request("GET", f"/api/projects/{project['id']}/preservation")
        self.assertEqual(status, 200)
        self.assertFalse(report["toolchain_archived"])
        self.assertIn("checksum changed", report["toolchain_error"])
        status, result = self.request("POST", f"/api/projects/{project['id']}/preservation/export", {})
        self.assertEqual(status, 400)
        self.assertIn("checksum changed", result["error"])
        self.assertEqual(self.engine.store.all("preservations"), [])

        archive.unlink()
        status, report = self.request("GET", f"/api/projects/{project['id']}/preservation")
        self.assertEqual(status, 200)
        self.assertFalse(report["toolchain_archived"])
        self.assertIn("Missing file", report["toolchain_error"])


if __name__ == "__main__":
    unittest.main()
