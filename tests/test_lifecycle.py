import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from portkeeper.lifecycle import Lifecycle
from portkeeper.safety import Invalid, checksum
from portkeeper.storage import Store


class LifecycleStore(Store):
    tables = Store.tables | {"notifications", "preservations"}

    def delete(self, table, record_id):
        assert table in self.tables
        with self.lock, self.connect() as db:
            db.execute(f"DELETE FROM {table} WHERE id=?", (record_id,))


class LifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = LifecycleStore(self.root / "state.sqlite3")
        self.engine = SimpleNamespace(store=self.store, lock=threading.RLock(),
                                      archive_root=self.root / "archive", busy=set())
        self.service = Lifecycle(self.engine)
        self.store.put("projects", {"id": "project", "name": "Example"})

    def tearDown(self):
        self.temp.cleanup()

    def artifact(self, number, target="linux", **extra):
        artifact_id = f"artifact{number}_{target}"
        directory = self.engine.archive_root / "packages/project" / artifact_id
        directory.mkdir(parents=True)
        archive = directory / "game.zip"
        archive.write_bytes(f"package {number}".encode())
        (directory / "manifest.json").write_text(json.dumps({"version": number}))
        return self.store.put("artifacts", {"id": artifact_id, "project_id": "project", "target": target,
                                           "version": str(number), "file": str(archive), "filename": archive.name,
                                           "size": archive.stat().st_size, "sha256": checksum(archive),
                                           "created_at": f"2026-10-{number:02d}T00:00:00Z", **extra})

    def test_validation_requires_evidence_and_verified_bytes(self):
        artifact = self.artifact(1)
        with self.assertRaisesRegex(Invalid, "Describe"):
            self.service.validate(artifact["id"], "launch_checked")
        self.service.validate(artifact["id"], "launch_checked", "Reached title screen on Linux")
        self.service.validate(artifact["id"], "user_confirmed_playable", "Played through the first dungeon")
        saved = self.store.get("artifacts", artifact["id"])
        self.assertEqual(saved["validation_level"], "user_confirmed_playable")
        self.assertEqual(len(saved["validation_history"]), 2)
        Path(artifact["file"]).write_bytes(b"modified")
        with self.assertRaisesRegex(Invalid, "checksum changed"):
            self.service.validate(artifact["id"], "packaged")

    def test_demo_cannot_be_marked_playable(self):
        artifact = self.artifact(1, demo=True)
        with self.assertRaisesRegex(Invalid, "Synthetic"):
            self.service.validate(artifact["id"], "user_confirmed_playable", "It looks fine")

    def test_known_good_pointer_does_not_follow_new_packages(self):
        first = self.artifact(1)
        with self.assertRaisesRegex(Invalid, "Record a launch"):
            self.service.set_known_good(first["id"])
        self.service.validate(first["id"], "launch_checked", "Reached title screen")
        self.service.set_known_good(first["id"])
        self.artifact(2)
        self.assertEqual(self.store.get("projects", "project")["known_good"]["linux"], first["id"])

    def test_keep_all_default_and_per_target_retention_preserve_special_versions(self):
        artifacts = [self.artifact(i) for i in range(1, 7)]
        windows = [self.artifact(i, "windows") for i in range(1, 3)]
        self.service.pin(artifacts[0]["id"])
        self.store.update("artifacts", artifacts[1]["id"], published=True)
        self.service.validate(artifacts[2]["id"], "launch_checked", "Launch checked")
        self.service.set_known_good(artifacts[2]["id"])
        self.assertEqual(self.service.retention("project")["candidates"], [])
        plan = self.service.retention("project", keep_last=2)
        self.assertEqual([x["id"] for x in plan["candidates"]], [artifacts[3]["id"]])
        protected = {x["id"]: x["reasons"] for x in plan["protected"]}
        self.assertIn("pinned", protected[artifacts[0]["id"]])
        self.assertIn("published", protected[artifacts[1]["id"]])
        self.assertIn("known_good", protected[artifacts[2]["id"]])
        self.assertTrue(all(a["id"] in protected for a in windows))

    def test_cleanup_removes_only_previewed_package_directories(self):
        first, second = self.artifact(1), self.artifact(2)
        source = self.engine.archive_root / "sources/project/capture/source.zip"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"preserved source")
        result = self.service.apply_retention("project", 1)
        self.assertEqual(result["removed"], [first["id"]])
        self.assertFalse(Path(first["file"]).parent.exists())
        self.assertTrue(Path(second["file"]).exists())
        self.assertEqual(source.read_bytes(), b"preserved source")
        with self.assertRaises(KeyError):
            self.store.get("artifacts", first["id"])

    def test_cleanup_rechecks_pins_and_rejects_running_jobs(self):
        first = self.artifact(1)
        self.artifact(2)
        self.assertEqual(len(self.service.retention("project", 1)["candidates"]), 1)
        self.service.pin(first["id"])
        self.assertEqual(self.service.apply_retention("project", 1)["removed"], [])
        self.engine.busy.add("project")
        with self.assertRaisesRegex(Invalid, "running job"):
            self.service.apply_retention("project", 1)

    def test_cleanup_validates_every_path_before_removing_anything(self):
        first, second = self.artifact(1), self.artifact(2)
        self.artifact(3)
        outside = self.root / "outside.zip"
        outside.write_bytes(b"keep me")
        self.store.update("artifacts", first["id"], file=str(outside))
        with self.assertRaisesRegex(Invalid, "does not match"):
            self.service.apply_retention("project", 1)
        self.assertTrue(Path(second["file"]).exists())
        self.assertEqual(outside.read_bytes(), b"keep me")

    def test_cleanup_rejects_package_directory_symlinks(self):
        first = self.artifact(1)
        self.artifact(2)
        outside = self.root / "outside"
        outside.mkdir()
        (Path(first["file"]).parent / "linked").symlink_to(outside, target_is_directory=True)
        with self.assertRaisesRegex(Invalid, "symlinks"):
            self.service.apply_retention("project", 1)
        self.assertTrue(outside.exists())

    def test_notifications_deduplicate_and_preserve_read_dismissal(self):
        first = self.service.notify("project", "ready", "Linux package is ready", artifactid="artifact1")
        self.service.mark_read(first["id"])
        repeated = self.service.notify("project", "ready", "Linux package is ready", artifactid="artifact1")
        self.assertEqual(first["id"], repeated["id"])
        self.assertTrue(repeated["read"])
        self.assertEqual(self.service.notifications(unread_only=True), [])
        self.service.dismiss(first["id"])
        self.assertEqual(self.service.notifications(), [])
        self.service.notify("project", "ready", "Linux package is ready", artifactid="artifact2")
        self.assertEqual(len(self.service.notifications()), 1)

    def test_invalid_retention_and_validation_parameters_are_rejected(self):
        artifact = self.artifact(1)
        for value in (0, -1, True, 1001, "3"):
            with self.assertRaises(Invalid):
                self.service.retention("project", value)
        with self.assertRaises(Invalid):
            self.service.pin(artifact["id"], "yes")
        with self.assertRaises(Invalid):
            self.service.validate(artifact["id"], "fully_tested")


if __name__ == "__main__":
    unittest.main()
