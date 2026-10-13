import copy
import json
import tempfile
import time
import unittest
from pathlib import Path

from portkeeper.engine import Engine
from portkeeper.safety import Invalid
from test_packaging import ROM, Releases, byteswapped, definition


class ModelToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "inputs/n64/roms").mkdir(parents=True)
        (root / "inputs/n64/roms/Example (USA).v64").write_bytes(byteswapped(ROM))
        self.engine = Engine(root / "data", root / "inputs", root / "library", github=Releases())
        self.addCleanup(self.engine.close)
        self.tools = self.engine.model_tools
        project = self.engine.create_project({"name": "Example Port", "repo": "example/port", "targets": ["linux", "windows"], "packaging": definition()})
        self.engine.check(project["id"])
        deadline = time.monotonic() + 15
        while project["id"] in self.engine.busy and time.monotonic() < deadline:
            time.sleep(0.01)
        self.id = project["id"]

    def run_test(self, value, targets=None):
        started = self.tools.call("test_build_draft", {"game_id": self.id, "definition": value, **({"targets": targets} if targets else {})})
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            result = self.tools.call("get_test_build", {"test_id": started["test_id"]})
            if result["status"] not in ("queued", "running"):
                return result
            time.sleep(0.02)
        self.fail("Timed out")

    def test_unselected_game_file_steps_are_skipped_and_reported_as_unverified(self):
        result = self.run_test(definition())
        self.assertEqual(result["status"], "partial", result)
        linux = result["result"]["targets"]["linux"]
        self.assertEqual(linux["missing"], [])
        self.assertEqual(linux["unverified"], ["game.z64"])
        self.assertIn("port", linux["sample_files"])
        self.assertEqual(self.engine.store.all("artifacts"), [])

    def test_selected_game_files_are_used_and_nothing_is_archived(self):
        self.engine.bind_input(self.id, {"input": "rom"})
        result = self.run_test(definition())
        self.assertEqual(result["status"], "passed", result)
        self.assertIn("game.z64", result["result"]["targets"]["windows"]["sample_files"])
        self.assertEqual(self.engine.store.all("artifacts"), [])
        self.assertEqual(list((self.engine.work_root).glob("trial-*")), [])

    def test_wrong_paths_fail_with_the_produced_listing(self):
        wrong = definition()
        wrong["outputs"]["linux"]["expected_files"] = ["bin/port", "Launch.sh"]
        result = self.run_test(wrong, ["linux"])
        self.assertEqual(result["status"], "failed")
        self.assertIn("bin/port", result["result"]["targets"]["linux"]["error"])
        feedback = self.tools.trial_feedback(self.engine.store.get("trials", result["id"]))
        self.assertIn("top-level entries", feedback)

    def test_validation_reports_problems_without_building(self):
        bad = definition(targets=["linux"])
        self.assertFalse(self.tools.call("validate_definition", {"game_id": self.id, "definition": bad})["valid"])
        good = self.tools.call("validate_definition", {"game_id": self.id, "definition": definition()})
        self.assertTrue(good["valid"])
        self.assertEqual(good["game_files"], [{"id": "rom", "label": "Example ROM", "required": True, "selected": False}])

    def test_submit_records_tests_and_never_reveals_game_file_locations(self):
        self.engine.bind_input(self.id, {"input": "rom"})
        self.run_test(definition())
        submitted = self.tools.call("submit_draft", {"game_id": self.id, "definition": definition(), "explanation": "Tested.", "agent": "Codex · fixture"})
        proposal = self.engine.store.get("proposals", submitted["proposal_id"])
        self.assertEqual(proposal["status"], "pending_review")
        self.assertEqual(proposal["model_tests"][0]["status"], "passed")
        self.assertIn("Codex", proposal["source"])
        game = json.dumps(self.tools.call("read_game", {"game_id": self.id}))
        self.assertNotIn("n64/roms", game)
        self.assertIn('"selected": true', game)

    def test_source_can_be_read_and_searched(self):
        listing = self.tools.call("read_source", {"game_id": self.id})
        self.assertTrue(listing["files"])
        path = listing["files"][0]
        self.assertEqual(self.tools.call("read_source", {"game_id": self.id, "path": path})["path"], path)
        self.assertIn("matches", self.tools.call("read_source", {"game_id": self.id, "pattern": "."}))
        with self.assertRaises(Invalid):
            self.tools.call("read_source", {"game_id": self.id, "path": "../etc/passwd"})

    def test_release_archives_are_listed_and_unknown_tools_refused(self):
        assets = self.tools.call("read_game", {"game_id": self.id})["release"]["assets"]
        listing = self.tools.call("list_release_archive", {"game_id": self.id, "asset": assets[0]["name"]})
        self.assertGreater(listing["count"], 0)
        with self.assertRaises(Invalid):
            self.tools.call("approve_draft", {})

    def test_built_in_assistant_fixes_a_draft_after_a_failed_test_build(self):
        from test_drafting import Server
        good = definition(source_checks=[])
        good["inputs"][0] = {key: value for key, value in good["inputs"][0].items() if key != "accepted"} | {"verify": "downstream"}
        wrong = copy.deepcopy(good)
        wrong["outputs"]["linux"]["expected_files"] = ["bin/port", "Launch.sh"]
        review = {"summary": "Looks consistent.", "findings": []}
        server = Server([{"choices": [{"message": {"content": json.dumps(reply)}}]} for reply in
                         [{"explanation": "first", "definition": wrong}, {"explanation": "fixed", "definition": good}, review]])
        self.addCleanup(server.close)
        self.engine.assistant.configure_provider({"provider": "openai", "base_url": server.url + "/v1", "model": "fixture-model", "api_key": "test-key"})
        project = self.engine.store.get("projects", self.id)
        job = self.engine.job(project, "capture", "v1")
        self.engine.store.update("jobs", job["id"], kind="draft")
        self.engine._draft(self.id, job["id"], "")
        job = self.engine.store.get("jobs", job["id"])
        self.assertEqual(job["status"], "ready", job.get("logs"))
        proposal = self.engine.store.get("proposals", job["proposal_id"])
        self.assertEqual([t["status"] for t in proposal["model_tests"]], ["partial", "failed"])
        self.assertEqual(proposal["explanation"], "fixed")
        feedback = server.requests[1]["body"]["messages"][-1]["content"]
        self.assertIn("test-built your definition: failed", feedback)
        self.assertIn("bin/port", feedback)
        self.assertEqual(self.engine.store.all("artifacts"), [])


if __name__ == "__main__":
    unittest.main()
