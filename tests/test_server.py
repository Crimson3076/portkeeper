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
from portkeeper.safety import checksum


class AllowedHostTests(unittest.TestCase):
    def test_allowed_host_entries_may_include_a_port_or_scheme(self):
        with tempfile.TemporaryDirectory() as temp:
            engine = Engine(Path(temp) / "data")
            self.addCleanup(engine.close)
            try:
                server = make_server(engine, port=0, allowed_hosts=[
                    "192.168.0.83:8787", " Tower ", "http://tower.local:8787/", "[fd00::5]:8787", ""])
            except PermissionError:
                self.skipTest("Sandbox forbids localhost sockets")
            server.server_close()
        self.assertTrue({"192.168.0.83", "tower", "tower.local", "fd00::5"} <= server.allowed_hosts)
        self.assertNotIn("", server.allowed_hosts)


class HttpIntegrationTests(unittest.TestCase):
    def test_discovery_routes_are_read_only_until_explicit_refresh_and_check_origin(self):
        with patch.object(self.engine.discovery, 'fetch') as fetch:
            status, _, body = self.request('GET', '/api/discovery')
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)['entries'], [])
            fetch.assert_not_called()
        with patch.object(self.engine.discovery, 'start_refresh', return_value={'refreshing': True}) as refresh:
            self.assertEqual(self.request('POST', '/api/discovery/refresh', {})[0], 202)
            refresh.assert_called_once_with(force=False)
            self.assertEqual(self.request('POST', '/api/discovery/refresh', {'force': 'yes'})[0], 400)
            self.assertEqual(self.request('POST', '/api/discovery/refresh', {}, {'Origin': 'https://other.test'})[0], 400)
            self.assertEqual(refresh.call_count, 1)
        with patch.object(self.engine.discovery, 'resolve', return_value={'repositories': []}) as resolve:
            self.assertEqual(self.request('POST', '/api/discovery/resolve', {'url': '/games/zelda'})[0], 200)
            resolve.assert_called_once_with({'url': '/games/zelda'})
            self.assertEqual(self.request('POST', '/api/discovery/resolve', {}, {'Origin': 'https://other.test'})[0], 400)
            self.assertEqual(resolve.call_count, 1)
        self.assertEqual(self.request('POST', '/api/discovery/resolve', {'url': 'http://localhost/secrets'})[0], 400)
        self.assertEqual(self.engine.store.all('projects'), [])

    def test_automation_routes_preserve_origin_and_path_boundaries(self):
        worker = self.engine.automation
        with patch.object(worker, 'configure', return_value={'policy': {'enabled': True}}) as configure:
            self.assertEqual(self.request('POST', '/api/automation/config', {'enabled': True})[0], 200)
            self.assertEqual(self.request('POST', '/api/automation/config', {'enabled': True}, {'Origin': 'https://other.test'})[0], 400)
            self.assertEqual(configure.call_count, 1)
        (self.engine.input_root / 'owned.bin').write_bytes(b'synthetic')
        status, _, body = self.request('GET', '/api/automation/inputs')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)['items'][0]['path'], 'owned.bin')
        self.assertEqual(self.request('GET', '/api/automation/inputs?path=../secrets')[0], 400)
        job = 'a' * 32
        with patch.object(worker, 'cancel', return_value={'cancel_requested': True}) as cancel:
            self.assertEqual(self.request('POST', f'/api/jobs/{job}/cancel-automation', {})[0], 200)
            cancel.assert_called_once_with(job)
        with patch.object(worker, 'resume', return_value={'job': {'id': job}}) as resume:
            answer = {'request_id': 'pending', 'reply': 'Use the standard build'}
            self.assertEqual(self.request('POST', f'/api/jobs/{job}/reply-automation', answer)[0], 202)
            resume.assert_called_once_with(job, answer)
            self.assertEqual(self.request('POST', f'/api/jobs/{job}/reply-automation', answer,
                                          {'Origin': 'https://other.test'})[0], 400)
            self.assertEqual(resume.call_count, 1)

    def test_native_agent_sign_in_routes_keep_mutation_origin_checks(self):
        agent = self.engine.assistant.agents
        safe = {'provider': 'codex', 'status': 'waiting', 'url': 'https://auth.openai.com/codex/device', 'code': 'EXAMPLE'}
        with patch.object(agent, 'login', return_value=safe) as login:
            status, _, body = self.request('POST', '/api/assistant/agent/login', {'provider': 'codex'})
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body), safe)
            login.assert_called_once_with('codex')
            self.assertEqual(self.request('POST', '/api/assistant/agent/login', {'provider': 'codex'}, {'Origin': 'https://other.test'})[0], 400)
            self.assertEqual(login.call_count, 1)
        with patch.object(agent, 'login_status', return_value=safe) as status:
            self.assertEqual(self.request('GET', '/api/assistant/agent/login?provider=codex')[0], 200)
            status.assert_called_once_with('codex')
        with patch.object(agent, 'login_input', return_value=safe) as input_:
            self.assertEqual(self.request('POST', '/api/assistant/agent/login/input', {'provider': 'claude_code', 'value': 'one-time-code'})[0], 200)
            input_.assert_called_once_with('claude_code', 'one-time-code')
        with patch.object(agent, 'logout', return_value={'provider': 'codex', 'status': 'not_started'}) as logout:
            self.assertEqual(self.request('POST', '/api/assistant/agent/logout', {'provider': 'codex'})[0], 200)
            logout.assert_called_once_with('codex')

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

    def test_ps4_input_picker_reads_metadata_without_binding_and_rejects_swapped_pkg(self):
        from portkeeper import packaging
        from test_ps4 import make_pkg
        self.engine.input_root = Path(self.temp.name) / 'roms'
        base = make_pkg(self.engine.input_root / 'ps4/roms/random-one.pkg')
        make_pkg(self.engine.input_root / 'ps4/roms/random-two.pkg', role='update', version='01.02')
        project = self.engine.create_project({'name': 'Fixture', 'repo': 'FelipePrado0/gow3_pc', 'targets': ['windows'], 'packaging': packaging.bundled()['gow3_pc']})
        route = f'/api/projects/{project["id"]}/inputs'
        status, _, body = self.request('GET', route)
        self.assertEqual(status, 200)
        choices = json.loads(body)
        self.assertEqual(choices['input_root'], str(self.engine.input_root))
        self.assertEqual(choices['inputs'][0]['found'][0]['ps4']['role'], 'base')
        self.assertEqual(self.engine.store.get('projects', project['id'])['input_bindings'], {})
        self.assertEqual(self.request('POST', route, {'input': 'game_pkg', 'path': 'ps4/roms/random-two.pkg'})[0], 400)
        status, _, body = self.request('POST', route, {'input': 'game_pkg', 'path': base.relative_to(self.engine.input_root).as_posix()})
        self.assertEqual(status, 200, body.decode())
        self.assertEqual(json.loads(body)['input_bindings']['game_pkg']['ps4']['app_version'], '01.00')

    def test_draft_prompt_passes_game_file_preference_and_rejects_non_boolean(self):
        project = self.engine.create_project({'name': 'Fixture', 'repo': 'example/game', 'targets': ['windows']})
        route = f'/api/projects/{project["id"]}/draft-prompt'
        with patch.object(self.engine, 'draft_prompt', return_value={'prompt': 'fixture'}) as prepare:
            self.assertEqual(self.request('POST', route, {'prepare_game_files': False})[0], 200)
            self.assertFalse(prepare.call_args.kwargs['prepare_game_files'])
        with patch.object(self.engine, '_draft_material') as material:
            self.assertEqual(self.request('POST', route, {'prepare_game_files': 'false'})[0], 400)
            material.assert_not_called()

    def test_draft_endpoint_preserves_opt_in_corrections_and_rejects_invalid_limits(self):
        project = self.engine.create_project({'name': 'Fixture', 'repo': 'example/game', 'targets': ['windows']})
        route = f'/api/projects/{project["id"]}/draft'
        with patch.object(self.engine.assistant, 'connected', return_value=True), patch.object(self.engine.executor, 'submit'):
            status, _, body = self.request('POST', route, {'max_calls': 3, 'review_model': True})
            self.assertEqual(status, 202)
            job = json.loads(body)['job']
            self.assertEqual(job['draft_max_calls'], 3)
            self.assertTrue(job['review_model'])
            for value in [True, 0, 4, '3']:
                self.assertEqual(self.request('POST', route, {'max_calls': value})[0], 400)

    def test_investigation_route_keeps_host_origin_guards_and_bounded_call_choice(self):
        project = self.engine.create_project({'name':'Fixture','repo':'example/game','targets':['windows']})
        self.engine.store.update('projects',project['id'],capture_id='a'*32)
        route = f'/api/projects/{project["id"]}/investigate'
        with patch.object(self.engine.assistant,'connected',return_value=True),patch.object(self.engine.executor,'submit') as submit:
            status, _, body = self.request('POST',route,{'max_calls':3,'prepare_game_files':False})
            self.assertEqual(status,202)
            self.assertEqual(json.loads(body)['job']['kind'],'investigation')
            self.assertFalse(json.loads(body)['job']['prepare_game_files'])
            self.assertEqual(self.request('POST',route,{},headers={'Origin':'https://evil.example'})[0],400)
            self.assertEqual(submit.call_count, 1)

    def test_import_download_streams_full_file_ranges_and_checks_game(self):
        project_id = 'a' * 32
        self.engine.store.put('projects', {'id': project_id, 'name': 'Example', 'metadata': {'filename_prefix': 'Example', 'igdb_id': 1}})
        raw = {'id': 10, 'platform_slug': 'linux', 'files': [{'id': 20, 'file_name': 'Example-v1-linux.zip', 'file_size_bytes': 8}]}
        content = b'PK\x03\x04test'
        def opened(route, byte_range=None):
            self.assertEqual(route, 'roms/20/files/content/Example-v1-linux.zip')
            start, end = map(int, byte_range[6:].split('-')) if byte_range else (0, 7)
            response = io.BytesIO(content[start:end + 1])
            response.status = 206 if byte_range else 200
            response.headers = {'Content-Length': str(end - start + 1), 'Content-Range': f'bytes {start}-{end}/8'}
            return response
        route = f'/api/projects/{project_id}/imports/10/files/20/download'
        with patch.object(self.engine.romm, '_json', return_value=raw), patch.object(self.engine.romm, '_open', side_effect=opened) as stream:
            status, headers, body = self.request('GET', route)
            self.assertEqual((status, body), (200, content))
            self.assertIn('attachment;', headers['Content-Disposition'])
            self.assertNotIn('Authorization', headers)
            for request_range, expected in [('bytes=0-3', content[:4]), ('bytes=-4', content[-4:]), ('bytes=4-', content[4:])]:
                status, headers, body = self.request('GET', route, headers={'Range': request_range})
                self.assertEqual((status, body), (206, expected))
            calls = stream.call_count
            for request_range in ['bytes=8-', 'bytes=-0', 'bytes=1-0', 'bytes=0-1,3-4']:
                self.assertEqual(self.request('GET', route, headers={'Range': request_range})[0], 416)
            self.assertEqual(stream.call_count, calls)
            raw['files'][0]['file_name'] = 'Other-v1-linux.zip'
            self.assertEqual(self.request('GET', route)[0], 400)
            self.assertEqual(stream.call_count, calls)

    def test_artifact_download_filename_is_escaped_in_content_disposition(self):
        archive = self.engine.archive_root / 'odd.zip'
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(b'PK\x03\x04test')
        name = 'Gam"e\r\nX-Evil: 1 \u00e9.zip'
        artifact = {'id': 'b' * 32, 'file': str(archive), 'sha256': checksum(archive), 'filename': name}
        self.engine.store.put('artifacts', artifact)
        status, headers, body = self.request('GET', f'/api/artifacts/{artifact["id"]}/download')
        self.assertEqual(status, 200)
        self.assertNotIn('X-Evil', headers)
        self.assertTrue(headers['Content-Disposition'].startswith("attachment; filename*=UTF-8''"))
        self.assertNotIn('"', headers['Content-Disposition'])

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

    def test_configuration_validation_endpoint_does_not_save_or_build(self):
        project = self.engine.create_project({"name": "Example", "repo": "example/game", "targets": ["linux"]})
        saved = self.engine.store.get("projects", project["id"])
        status, _, body = self.request("POST", f"/api/projects/{project['id']}/configuration/validate",
                                       {"packaging": {**saved["packaging"], "summary": "New description"}})
        self.assertEqual(status, 200)
        self.assertTrue(json.loads(body)["valid"])
        self.assertEqual(self.engine.store.get("projects", project["id"]), saved)
        self.assertEqual(self.engine.store.all("jobs"), [])
        preview = {**saved["packaging"], "summary": "Catalog update", "revision": 5}
        with patch.object(self.engine.catalog, "definitions", return_value={preview["id"]: preview}):
            status, _, body = self.request("GET", f"/api/catalog/definitions/{preview['id']}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), preview)
        self.assertEqual(self.engine.store.get("projects", project["id"]), saved)
        status, _, _ = self.request("POST", f"/api/projects/{project['id']}/configuration/validate",
                                    {"packaging": {"schema": 99}})
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

    def test_project_cover_is_proxied_without_exposing_credentials(self):
        project = self.engine.create_project({"name": "Port", "repo": "example/port", "targets": ["linux"]})
        path = f"/api/projects/{project['id']}/cover"
        status, _, _ = self.request("GET", path)
        self.assertEqual(status, 404)
        self.engine.store.update("projects", project['id'], metadata={"romm_rom_id": 10})
        with patch.object(self.engine.romm, "cover", return_value=("image/png", b"cover-bytes")) as cover:
            status, headers, body = self.request("GET", path)
        self.assertEqual(status, 200)
        self.assertEqual(body, b"cover-bytes")
        self.assertEqual(headers["Content-Type"], "image/png")
        self.assertEqual(headers["Cache-Control"], "private, max-age=60")
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        cover.assert_called_once_with(10)

    def test_cover_preview_only_proxies_offered_artwork(self):
        status, _, body = self.request("GET", "/api/romm/cover-preview?url=https%3A%2F%2Finternal.example%2Fx.png")
        self.assertEqual(status, 400)
        self.assertIn("Search covers first", json.loads(body)["error"])
        self.engine.romm.offered_art["https://cdn.example/t.png"] = True
        with patch.object(self.engine.romm, "artwork_preview", return_value=("image/png", b"\x89PNG")):
            status, headers, body = self.request("GET", "/api/romm/cover-preview?url=https%3A%2F%2Fcdn.example%2Ft.png")
        self.assertEqual((status, headers["Content-Type"], body), (200, "image/png", b"\x89PNG"))


    def test_mcp_needs_a_token_and_lists_and_calls_model_tools(self):
        def rpc(payload, token=None):
            headers = {"Authorization": "Bearer " + token} if token else {}
            status, _, body = self.request("POST", "/mcp", payload, headers)
            return status, json.loads(body) if body else None
        self.assertEqual(rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})[0], 401)
        status, _, body = self.request("POST", "/api/mcp/config", {"enabled": True})
        token = json.loads(body)["token"]
        self.assertEqual(rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, "wrong")[0], 401)
        status, reply = rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}, token)
        self.assertEqual(reply["result"]["serverInfo"]["name"], "portkeeper")
        self.assertEqual(rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}, token)[0], 202)
        names = [tool["name"] for tool in rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}, token)[1]["result"]["tools"]]
        self.assertIn("test_build_draft", names)
        self.assertNotIn("approve_draft", names)
        listed = rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "list_games", "arguments": {}}}, token)[1]["result"]
        self.assertFalse(listed["isError"])
        self.assertEqual(listed["structuredContent"], {"games": []})
        failed = rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "read_game", "arguments": {"game_id": "0" * 32}}}, token)[1]["result"]
        self.assertTrue(failed["isError"])
        state = json.loads(self.request("GET", "/api/state")[2])
        self.assertTrue(state["mcp"]["enabled"])
        self.assertNotIn(token, json.dumps(state))
        self.request("POST", "/api/mcp/config", {"enabled": False})
        self.assertEqual(rpc({"jsonrpc": "2.0", "id": 5, "method": "tools/list"}, token)[0], 401)


if __name__ == "__main__":
    unittest.main()
