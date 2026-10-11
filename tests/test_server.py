import hashlib
import http.client
import io
import json
import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from portkeeper.engine import Engine
from portkeeper.server import Handler, make_server


class HttpIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(Path(self.temp.name) / "data")
        self.addCleanup(self.engine.close)
        try:
            self.server = make_server(self.engine, port=0)
        except PermissionError:
            self.skipTest("Sandbox forbids localhost sockets; run with local network permission")
        self.port = self.server.server_port
        self.silence_logs = patch.object(Handler, "log_message")
        self.silence_logs.start()
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.assertFalse(self.thread.is_alive(), "HTTP server did not stop")
        self.silence_logs.stop()

    def request(self, method, path, body=None, headers=None):
        request_headers = {"Host": f"127.0.0.1:{self.port}"}
        if body is not None:
            request_headers["Content-Type"] = "application/json"
            body = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        request_headers.update(headers or {})
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_static_assets_and_state_are_served_and_traversal_rejected(self):
        for path, mime in (("/", "text/html"), ("/static/styles.css", "text/css"), ("/static/app.js", "javascript")):
            with self.subTest(path=path):
                status, headers, body = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertIn(mime, headers["Content-Type"])
                self.assertTrue(body)
                self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        status, _, body = self.request("GET", "/api/state")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["summary"]["projects"], 0)
        status, _, _ = self.request("GET", "/static/%2e%2e/portkeeper/engine.py")
        self.assertEqual(status, 400)

    def test_mutations_reject_foreign_host_origin_and_non_json_content(self):
        attempts = (({"Host": "attacker.example"}, 403), ({"Origin": "https://attacker.example"}, 400),
                    ({"Origin": "null"}, 400), ({"Content-Type": "text/plain"}, 400),
                    ({"Transfer-Encoding": "chunked"}, 400))
        for headers, expected in attempts:
            with self.subTest(headers=headers):
                status, _, body = self.request("POST", "/api/demo", {}, headers)
                self.assertEqual(status, expected)
                self.assertIn("error", json.loads(body))
        self.assertEqual(self.engine.store.all("projects"), [])

    def test_configured_lan_host_is_accepted_on_any_published_port(self):
        self.server.allowed_hosts.add("192.168.1.20")
        for host, expected in (("192.168.1.20:8787", 200), ("192.168.1.20:18787", 200), ("127.0.0.1:18787", 200),
                               ("192.168.1.21:8787", 403), ("192.168.1.20:bad", 403)):
            with self.subTest(host=host):
                status, _, _ = self.request("GET", "/api/state", headers={"Host": host})
                self.assertEqual(status, expected)

    def test_invalid_and_non_object_json_are_errors(self):
        for body in ("[1, 2]", "null", '"string"', "{broken"):
            with self.subTest(body=body):
                status, _, payload = self.request("POST", "/api/demo", body)
                self.assertEqual(status, 400)
                self.assertIn("error", json.loads(payload))
        self.assertEqual(self.engine.store.all("projects"), [])

    def test_demo_api_builds_downloads_and_publishes_verified_artifacts(self):
        status, _, body = self.request("POST", "/api/demo", {}, {"Origin": f"http://127.0.0.1:{self.port}"})
        self.assertEqual(status, 202)
        result = json.loads(body)
        project_id = result["project"]["id"]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status, _, body = self.request("GET", "/api/state")
            self.assertEqual(status, 200)
            state = json.loads(body)
            project = next(item for item in state["projects"] if item["id"] == project_id)
            if project["status"] in ("ready", "needs_attention"):
                break
            time.sleep(0.01)
        else:
            self.fail("Demo build did not finish within timeout")
        self.assertEqual(project["status"], "ready", project.get("last_error"))
        self.assertEqual(len(state["artifacts"]), 3)
        for artifact in state["artifacts"]:
            status, headers, download = self.request("GET", artifact["download_url"])
            self.assertEqual(status, 200)
            self.assertEqual(headers["Content-Type"], "application/zip")
            self.assertEqual(hashlib.sha256(download).hexdigest(), artifact["sha256"])
            with zipfile.ZipFile(io.BytesIO(download)) as bundle:
                manifest = json.loads(bundle.read("portkeeper-manifest.json"))
                self.assertEqual(manifest["target"], artifact["target"])
        artifact = state["artifacts"][0]
        status, _, body = self.request("POST", f"/api/artifacts/{artifact['id']}/publish", {})
        self.assertEqual(status, 200)
        published = json.loads(body)
        self.assertTrue(published["published"])
        self.assertEqual(hashlib.sha256(Path(published["published_path"]).read_bytes()).hexdigest(), artifact["sha256"])
        status, _, source = self.request("GET", f"/api/captures/{project['capture_id']}/download")
        self.assertEqual(status, 200)
        self.assertTrue(zipfile.is_zipfile(io.BytesIO(source)))
        self.engine.store.update("captures", project["capture_id"], complete=False)
        status, _, incomplete_source = self.request("GET", f"/api/captures/{project['capture_id']}/download")
        self.assertEqual(status, 200)
        self.assertEqual(incomplete_source, source)
        Path(artifact["file"]).write_bytes(b"corrupted package")
        status, _, body = self.request("GET", artifact["download_url"])
        self.assertEqual(status, 400)
        self.assertIn("checksum changed", json.loads(body)["error"])


if __name__ == "__main__":
    unittest.main()
