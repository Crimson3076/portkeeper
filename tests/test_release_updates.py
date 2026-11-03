import copy
import hashlib
import unittest
from unittest.mock import patch

from portkeeper import release_updates
from portkeeper.safety import Invalid
import test_packaging


class ReleaseUpdateTests(unittest.TestCase):
    """Reuses the packaging fixture; only the new-release behaviour is exercised here."""

    def setUp(self):
        fixture = test_packaging.PackagingEngineTests("test_definition_builds_portable_packages_with_located_verified_inputs")
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        self.engine, self.github, self.check, self.project = fixture.engine, fixture.github, fixture.check, fixture.project

    def rename_assets(self, names):
        """Serve the current release's assets under new names (version string with a dot, for example)."""
        kept = []
        for asset in self.github.current["assets"]:
            new = names.get(asset["name"], asset["name"])
            if new is None:
                continue
            url = asset["browser_download_url"].rsplit("/", 1)[0] + "/" + new
            self.github.payloads[url] = self.github.payloads[asset["browser_download_url"]]
            kept.append({**asset, "name": new, "browser_download_url": url})
        self.github.current["assets"] = kept

    def first_release(self):
        return self.check(self.project())

    def pending(self, project):
        return [p for p in self.engine.store.all("proposals") if p["project_id"] == project["id"] and p["status"] == "pending_review"]

    def test_version_only_change_proposes_a_patch_without_a_model_call(self):
        project = self.first_release()
        self.github.publish(2)
        self.rename_assets({"Port-v2-Linux.zip": "Port-v2.0-Linux.zip", "Port-v2-Win64.zip": "Port-v2.0-Win64.zip"})
        with patch.object(self.engine.assistant, "connected", return_value=True), patch.object(self.engine, "queue_investigation") as draft:
            project = self.check(project)
        draft.assert_not_called()
        capture = self.engine.store.get("captures", project["capture_id"])
        self.assertEqual(capture["review"]["status"], "needs_review")
        diff = capture["review"]["diff"]
        self.assertEqual(diff["outcome"], "patch")
        self.assertEqual({r["from"] for r in diff["assets"]["renamed"]}, {"Port-v1-Linux.zip", "Port-v1-Win64.zip"})
        proposals = self.pending(project)
        self.assertEqual(len(proposals), 1)
        proposal = proposals[0]
        self.assertEqual((proposal["kind"], proposal["update_kind"]), ("definition", "patch"))
        patched = proposal["definition"]
        self.assertEqual(patched["revision"], 2)
        self.assertRegex("Port-v2.0-Linux.zip", patched["downloads"]["linux"]["asset_pattern"])
        self.assertRegex("Port-v1-Linux.zip", patched["downloads"]["linux"]["asset_pattern"])
        # Only the download patterns changed.
        expected = copy.deepcopy(project["packaging"])
        for key in ("linux", "windows"):
            expected["downloads"][key]["asset_pattern"] = patched["downloads"][key]["asset_pattern"]
        expected["revision"] = 2
        expected["history"] = patched["history"]
        self.assertEqual(patched, expected)

    def test_patch_is_never_applied_automatically(self):
        project = self.first_release()
        before = copy.deepcopy(project["packaging"])
        self.github.publish(2)
        self.rename_assets({"Port-v2-Linux.zip": "Port-v2.0-Linux.zip", "Port-v2-Win64.zip": "Port-v2.0-Win64.zip"})
        project = self.check(project)
        self.assertEqual(project["packaging"], before)
        self.assertFalse(project.get("auto_approve_version_patches"))
        self.assertFalse([a for a in self.engine.store.all("artifacts") if a["version"] == "v2"])
        queue = self.engine.review_queue()
        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["proposal_id"], self.pending(project)[0]["id"])
        # Approving goes through the normal review path, after which a recheck packages the release.
        self.engine.apply_proposal(queue[0]["proposal_id"])
        project = self.check(project)
        self.assertEqual(self.engine.store.get("captures", project["capture_id"])["review"]["status"], "compatible")
        self.assertEqual(self.engine.review_queue(), [])

    def test_layout_change_starts_an_investigation_seeded_with_the_diff(self):
        project = self.first_release()
        self.github.publish(2, linux_files={"renamed-binary": b"x"})
        with patch.object(self.engine.assistant, "connected", return_value=True), patch.object(self.engine, "queue_investigation") as draft:
            project = self.check(project)
        capture = self.engine.store.get("captures", project["capture_id"])
        diff = capture["review"]["diff"]
        self.assertEqual(diff["outcome"], "investigate")
        self.assertEqual(diff["layout"][0]["missing"], ["port"])
        self.assertFalse(self.pending(project))
        draft.assert_called_once()
        self.assertIn("renamed-binary", draft.call_args.args[1] if len(draft.call_args.args) > 1 else draft.call_args.kwargs["instruction"])
        self.assertIn("no longer contains: port", draft.call_args.args[1] if len(draft.call_args.args) > 1 else draft.call_args.kwargs["instruction"])
        self.assertEqual(self.engine.review_queue()[0]["kind"], "investigate")

    def test_removed_asset_is_reported_and_not_patched(self):
        project = self.first_release()
        self.github.publish(2)
        self.rename_assets({"Port-v2-Linux.zip": None})
        with patch.object(self.engine.assistant, "connected", return_value=False):
            project = self.check(project)
        diff = self.engine.store.get("captures", project["capture_id"])["review"]["diff"]
        self.assertEqual(diff["assets"]["removed"], ["Port-v1-Linux.zip"])
        self.assertEqual(diff["outcome"], "investigate")
        self.assertFalse(self.pending(project))
        item = self.engine.review_queue()[0]
        self.assertIn("Port-v1-Linux.zip", item["summary"])
        self.assertIn("removed", item["summary"])

    def test_dismiss_removes_the_item_and_discards_its_patch(self):
        project = self.first_release()
        self.github.publish(2)
        self.rename_assets({"Port-v2-Linux.zip": "Port-v2.0-Linux.zip", "Port-v2-Win64.zip": "Port-v2.0-Win64.zip"})
        project = self.check(project)
        item = self.engine.review_queue()[0]
        self.engine.dismiss_review(project["id"], item["capture_id"])
        self.assertEqual(self.engine.review_queue(), [])
        self.assertFalse(self.pending(project))
        with self.assertRaises(Invalid):
            self.engine.dismiss_review(project["id"], "0" * 32)


class PatchRefusalTests(unittest.TestCase):
    DEFINITION = {"revision": 1, "downloads": {"linux": {"asset_pattern": "^Port-v1-Linux\\.zip$", "platform": "linux"}}}

    def capture(self, version, names):
        return {"id": version, "version": version, "assets": [{"name": n} for n in names]}

    def test_no_patch_when_two_new_files_look_like_the_old_one(self):
        old = self.capture("v1", ["Port-v1-Linux.zip"])
        new = self.capture("v2", ["Port-v2-Linux.zip", "Port-v3-Linux.zip"])
        self.assertIsNone(release_updates.propose_patch(self.DEFINITION, old, new, {}, targets=["linux"]))

    def test_no_patch_when_the_new_name_differs_by_more_than_a_version(self):
        old = self.capture("v1", ["Port-v1-Linux.zip"])
        new = self.capture("v2", ["Port-v2-Steam.zip"])
        self.assertIsNone(release_updates.propose_patch(self.DEFINITION, old, new, {}, targets=["linux"]))


class DiffHelperTests(unittest.TestCase):
    def test_normalize_replaces_version_strings(self):
        self.assertEqual(release_updates.normalize("gow3-windows-v3.0.0.zip", ["windows-v3.0.0"]), release_updates.normalize("gow3-windows-v2.1.4.zip", ["windows-v2.1.4"]))
        self.assertNotEqual(release_updates.normalize("a-v1.zip", []), release_updates.normalize("b-v1.zip", []))


if __name__ == "__main__":
    unittest.main()
