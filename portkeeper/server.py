import json
import mimetypes
import re
import shutil
import urllib.parse
from functools import lru_cache, partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .engine import Busy
from .safety import Invalid, checksum


STATIC = Path(__file__).resolve().parent.parent / "static"


class Handler(BaseHTTPRequestHandler):
    server_version = "Portkeeper/0.1"

    def __init__(self, *args, engine, **kwargs):
        self.engine = engine
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        # Avoid logging query strings or local input/recipe content.
        print(f"{self.log_date_time_string()} {self.command} {urllib.parse.urlsplit(self.path).path} {args[1] if len(args) > 1 else ''}", flush=True)

    def respond_headers(self, status, content_type, length, disposition=None, cache_control="no-store", extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", cache_control)
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if disposition:
            self.send_header("Content-Disposition", disposition)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def json(self, status, value):
        body = json.dumps(value).encode()
        self.respond_headers(status, "application/json; charset=utf-8", len(body))
        self.wfile.write(body)

    def body(self):
        if self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise Invalid("Mutations require Content-Type: application/json")
        origin = self.headers.get("Origin")
        if origin:
            parts = urllib.parse.urlsplit(origin)
            if parts.scheme not in ("http", "https") or parts.netloc.lower() != self.headers.get("Host", "").lower():
                raise Invalid("Cross-origin requests are not allowed")
        if self.headers.get("Transfer-Encoding"):
            raise Invalid("Chunked request bodies are not supported")
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as error:
            raise Invalid("Invalid Content-Length") from error
        if not 0 <= length <= 1024 * 1024:
            raise Invalid("Request exceeds 1 MiB limit")
        self.connection.settimeout(15)
        try:
            payload = json.loads(self.rfile.read(length) or b"{}")
        except (ValueError, TimeoutError) as error:
            raise Invalid("Invalid JSON request") from error
        if not isinstance(payload, dict):
            raise Invalid("Request must be a JSON object")
        return payload

    def valid_host(self):
        try:
            parts = urllib.parse.urlsplit("http://" + self.headers.get("Host", ""))
            parts.port  # Rejects malformed ports. Docker may publish on a different host port.
            return parts.hostname in self.server.allowed_hosts
        except ValueError:
            return False

    def do_GET(self):
        self.dispatch("GET")

    def do_POST(self):
        self.dispatch("POST")

    def do_PATCH(self):
        self.dispatch("PATCH")

    def dispatch(self, method):
        try:
            if not self.valid_host():
                self.json(403, {"error": "Unrecognized Host header; use the printed local URL or add this "
                                         "address with --allowed-host / PORTKEEPER_ALLOWED_HOSTS"})
                return
            path = urllib.parse.urlsplit(self.path).path
            if path == "/mcp":
                self.mcp(method)
                return
            if method == "GET" and path == "/api/state":
                self.json(200, self.engine.state())
            elif method == "POST" and path == "/api/maintenance":
                from .maintenance import manage
                self.json(200, manage(self.engine, self.body()))
            elif method == "GET" and path == "/api/profiles":
                from .profiles import catalog
                self.json(200, {"profiles": catalog()})
            elif method == "POST" and path == "/api/romm/config":
                payload = self.body()
                self.json(200, self.engine.configure_romm(payload))
            elif method == "POST" and path == "/api/romm/test":
                self.body()
                self.json(200, self.engine.test_romm())
            elif method == "GET" and path == "/api/romm/search":
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                self.json(200, self.engine.romm.search(query.get("q", [""])[0]))
            elif method == "GET" and path == "/api/romm/covers":
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                self.json(200, self.engine.romm.search_covers(query.get("q", [""])[0]))
            elif method == "GET" and path == "/api/romm/cover-preview":
                url = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("url", [""])[0]
                if not self.engine.romm.offered(url):
                    raise Invalid("Search covers first; only offered artwork can be previewed")
                self.image(*self.engine.romm.artwork_preview(url))
            elif method == "GET" and re.fullmatch(r"/api/romm/roms/[0-9]+", path):
                self.json(200, self.engine.romm.detail(int(path.split("/")[4])))
            elif method == "POST" and path == "/api/romm/inspect":
                from .profiles import supported_hashes
                payload = self.body()
                self.json(200, self.engine.romm.inspect_rom(payload.get("rom_id"), payload.get("filename"), supported_hashes(payload.get("profile", "shipwright"))))
            elif method == "POST" and path == "/api/assistant/models":
                payload = self.body()
                if payload.get("provider") and payload["provider"] != "ollama":
                    self.json(200, self.engine.assistant.list_models(payload))
                else:
                    self.json(200, self.engine.assistant.models(payload.get("endpoint")))
            elif method == "POST" and path == "/api/assistant/provider":
                self.json(200, self.engine.assistant.configure_provider(self.body()))
            elif method == "POST" and path == "/api/assistant/agent/login":
                payload = self.body()
                self.json(200, self.engine.assistant.agents.login(payload.get("provider")))
            elif method == "GET" and path == "/api/assistant/agent/login":
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                self.json(200, self.engine.assistant.agents.login_status(query.get("provider", [""])[0]))
            elif method == "POST" and path == "/api/assistant/agent/login/input":
                payload = self.body()
                self.json(200, self.engine.assistant.agents.login_input(payload.get("provider"), payload.get("value")))
            elif method == "POST" and path == "/api/assistant/agent/logout":
                payload = self.body()
                self.json(200, self.engine.assistant.agents.logout(payload.get("provider")))
            elif method == "POST" and path == "/api/mcp/config":
                self.json(200, self.engine.mcp.configure(self.body()))
            elif method == "GET" and path == "/api/model-tests":
                project = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("project", [""])[0]
                self.json(200, {"tests": [{k: v for k, v in t.items() if k != "definition"} for t in self.engine.store.all("trials") if t["project_id"] == project][:20]})
            elif method == "POST" and path == "/api/assistant/test":
                self.body()
                self.json(200, self.engine.assistant.test())
            elif method == 'POST' and path == '/api/automation/config':
                self.json(200, self.engine.automation.configure(self.body()))
            elif method == 'GET' and path == '/api/automation/inputs':
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                self.json(200, self.engine.automation.input_choices(query.get('path', [''])[0]))
            elif method == 'POST' and re.fullmatch(r'/api/projects/[a-f0-9]{32}/automate', path):
                self.json(202, self.engine.automation.queue(path.split('/')[3], self.body()))
            elif re.fullmatch(r'/api/projects/[a-f0-9]{32}/script-recipe', path) and method == 'GET':
                self.json(200, self.engine.automation.export_recipe(path.split('/')[3]))
            elif re.fullmatch(r'/api/projects/[a-f0-9]{32}/script-recipe', path) and method == 'POST':
                self.json(200, self.engine.automation.edit_recipe(path.split('/')[3], self.body()))
            elif re.fullmatch(r'/api/jobs/[a-f0-9]{32}/automation-record', path) and method == 'GET':
                self.json(200, self.engine.automation.record(path.split('/')[3]))
            elif re.fullmatch(r'/api/jobs/[a-f0-9]{32}/automation-logs/[a-f0-9]{32}', path) and method == 'GET':
                self.json(200, self.engine.automation.worker_log(path.split('/')[3], path.split('/')[5]))
            elif re.fullmatch(r'/api/jobs/[a-f0-9]{32}/cancel-automation', path) and method == 'POST':
                self.body()
                self.json(200, self.engine.automation.cancel(path.split('/')[3]))
            elif re.fullmatch(r'/api/jobs/[a-f0-9]{32}/reply-automation', path) and method == 'POST':
                self.json(202, self.engine.automation.resume(path.split('/')[3], self.body()))
            elif method == "GET" and path == "/api/catalog/games":
                self.json(200, self.engine.catalog_games())
            elif re.fullmatch(r"/api/catalog/games/[a-z0-9][a-z0-9_.-]{0,63}/files", path) and method == "POST":
                self.body()
                self.json(200, self.engine.find_game_files(path.split("/")[4]))
            elif method == "POST" and path == "/api/setup":
                self.json(202, self.engine.add_from_catalog(self.body()))
            elif method == "GET" and path == "/api/github/lookup":
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                self.json(200, self.engine.github_lookup(query.get("repo", [""])[0]))
            elif method == "POST" and path == "/api/assistant/config":
                payload = self.body()
                self.json(200, self.engine.assistant.configure(payload.get("endpoint", ""), payload.get("model", "")))
            elif method == "POST" and path == "/api/toolchains/config":
                self.json(200, self.engine.configure_toolchain(self.body()))
            elif method == "POST" and path == "/api/toolchains/archive":
                payload = self.body()
                self.json(200, self.engine.archive_toolchain(payload.get("image")))
            elif method == "GET" and path == "/api/recipes":
                self.json(200, self.engine.templates())
            elif method == "POST" and path == "/api/projects":
                self.json(201, self.engine.create_project(self.body()))
            elif method == "POST" and path == "/api/demo":
                self.body()
                self.json(202, self.engine.demo())
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}", path) and method == "PATCH":
                self.json(200, self.engine.update_project(path.split("/")[3], self.body()))
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/configuration/validate", path) and method == "POST":
                self.json(200, self.engine.validate_configuration(path.split("/")[3], self.body()))
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/imports", path) and method == "GET":
                self.json(200, self.engine.reviews.imports(path.split("/")[3]))
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/imports/[0-9]+/files/[0-9]+/download", path) and method == "GET":
                parts = path.split("/")
                self.import_download(parts[3], int(parts[5]), int(parts[7]))
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/playtest", path) and method == "POST":
                self.json(201, self.engine.reviews.record(path.split("/")[3], self.body()))
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/cover", path) and method == "GET":
                project = self.engine.store.get("projects", path.split("/")[3])
                metadata = project.get("metadata") or {}
                rom_id = metadata.get("romm_rom_id")
                if metadata.get("cover_policy") == "custom" and metadata.get("url_cover"):
                    self.image(*self.engine.romm.artwork_preview(metadata["url_cover"]))
                elif not rom_id:
                    self.json(404, {"error": "Link a RomM entry to display its cover"})
                else:
                    content_type, body = self.engine.romm.cover(rom_id)
                    self.respond_headers(200, content_type, len(body), cache_control="private, max-age=60")
                    self.wfile.write(body)
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/cover", path) and method == "POST":
                self.json(200, self.engine.choose_cover(path.split("/")[3], self.body()))
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/metadata-link", path) and method == "POST":
                self.json(200, self.engine.link_metadata(path.split("/")[3], self.body()))
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/check", path) and method == "POST":
                payload = self.body()
                self.json(202, self.engine.check(path.split("/")[3], release_version=payload.get("version"), draft_after=payload.get("draft") is True, automate_after=payload.get('automate') is True))
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/build", path) and method == "POST":
                payload = self.body()
                self.json(202, self.engine.build(path.split("/")[3], payload.get("targets"), capture_id=payload.get("capture_id"),
                                                 publish=bool(payload.get("publish"))))
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/setup", path) and method == "POST":
                payload = self.body()
                self.json(202, self.engine.setup(path.split("/")[3], publish=payload.get("publish", True) is True))
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/investigate", path) and method == "POST":
                payload = self.body()
                self.json(202, self.engine.queue_investigation(path.split('/')[3], payload.get('instruction', ''), payload.get('max_calls', 3), payload.get('prepare_game_files', True)))
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/draft", path) and method == "POST":
                payload = self.body()
                self.json(202, self.engine.queue_draft(path.split("/")[3], payload.get("instruction", ""), prepare_game_files=payload.get('prepare_game_files', True), review_model=payload.get('review_model', False), max_calls=payload.get('max_calls'), investigation_id=payload.get('investigation_id'), test_inputs=payload.get('test_inputs', False)))
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/draft-prompt", path) and method == "POST":
                payload = self.body()
                result = self.engine.draft_prompt(path.split("/")[3], payload.get("instruction", ""), prepare_game_files=payload.get('prepare_game_files', True))
                self.json(200, {"prompt": result["prompt"]})
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/draft-workspace", path) and method == "GET":
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                mode = query.get('prepare_game_files', ['true'])[0]
                if mode not in ('true', 'false'):
                    raise Invalid('Choose true or false for game file preparation')
                data = self.engine.draft_workspace(path.split("/")[3], prepare_game_files=mode == 'true')
                self.respond_headers(200, "application/zip", len(data), 'attachment; filename="portkeeper-draft-workspace.zip"')
                self.wfile.write(data)
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/draft-import", path) and method == "POST":
                self.json(201, self.engine.import_draft(path.split("/")[3], self.body().get("text", "")))
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/test-build", path) and method == "POST":
                self.body()
                self.json(202, self.engine.test_build(path.split("/")[3]))
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/inputs", path) and method == "POST":
                self.json(200, self.engine.bind_input(path.split("/")[3], self.body()))
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/inputs", path) and method == "GET":
                self.json(200, self.engine.project_game_files(path.split('/')[3]))
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/catalog-definition", path) and method == "POST":
                self.body()
                self.json(200, self.engine.catalog.apply(path.split("/")[3]))
            elif method == "GET" and re.fullmatch(r"/api/catalog/definitions/[a-z0-9_.-]+", path):
                self.json(200, self.engine.catalog_definition(path.split("/")[-1]))
            elif method == "GET" and path == "/api/catalog":
                self.json(200, self.engine.catalog.status())
            elif method == "POST" and path == "/api/catalog/refresh":
                self.body()
                self.json(200, self.engine.catalog.refresh())
            elif re.fullmatch(r"/api/artifacts/[a-f0-9]{32}/publish-romm", path) and method == "POST":
                self.body()
                self.json(202, self.engine.publisher.queue(path.split("/")[3]))
            elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}", path) and method == "GET":
                self.json(200, self.engine.store.get("jobs", path.split("/")[3]))
            elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}/retry", path) and method == "POST":
                self.body()
                self.json(202, self.engine.retry(path.split("/")[3]))
            elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}/draft-record", path) and method == "GET":
                data = (json.dumps(self.engine.draft_record(path.split('/')[3]), indent=2) + '\n').encode()
                self.respond_headers(200, 'application/json', len(data), 'attachment; filename="portkeeper-drafting-record.json"')
                self.wfile.write(data)
            elif re.fullmatch(r"/api/artifacts/[a-f0-9]{32}/pin", path) and method == "POST":
                self.json(200, self.engine.lifecycle.pin(path.split("/")[3], self.body().get("pinned", True)))
            elif re.fullmatch(r"/api/artifacts/[a-f0-9]{32}/validation", path) and method == "POST":
                payload = self.body()
                self.json(200, self.engine.lifecycle.validate(path.split("/")[3], payload.get("level"), payload.get("notes", ""), payload.get("evidence")))
            elif re.fullmatch(r"/api/artifacts/[a-f0-9]{32}/known-good", path) and method == "POST":
                self.body()
                self.json(200, self.engine.lifecycle.set_known_good(path.split("/")[3]))
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/retention", path) and method == "POST":
                payload = self.body()
                operation = self.engine.apply_retention if payload.get("apply") else self.engine.lifecycle.retention
                self.json(200, operation(path.split("/")[3], payload.get("keep_last")))
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/preservation", path) and method == "GET":
                self.json(200, self.engine.preservation_status(path.split("/")[3]))
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/preservation/export", path) and method == "POST":
                self.body()
                self.json(201, self.engine.export_preservation(path.split("/")[3]))
            elif re.fullmatch(r"/api/preservations/[a-f0-9]{32}/download", path) and method == "GET":
                record = self.engine.store.get("preservations", path.split("/")[3])
                self.download(record["file"], record["sha256"], record["filename"])
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/recipe-proposal", path) and method == "POST":
                payload = self.body()
                self.json(201, self.engine.recipe_proposal(path.split("/")[3], payload.get("instruction", "")))
            elif re.fullmatch(r"/api/proposals/[a-f0-9]{32}/apply", path) and method == "POST":
                self.body()
                self.json(200, self.engine.apply_proposal(path.split("/")[3]))
            elif re.fullmatch(r"/api/proposals/[a-f0-9]{32}/discard", path) and method == "POST":
                self.body()
                self.json(200, self.engine.discard_proposal(path.split("/")[3]))
            elif re.fullmatch(r"/api/notifications/[a-f0-9]{64}/read", path) and method == "POST":
                self.body()
                self.json(200, self.engine.lifecycle.mark_read(path.split("/")[3]))
            elif path == "/api/notifications/read-all" and method == "POST":
                self.body()
                for record in self.engine.lifecycle.notifications():
                    self.engine.lifecycle.mark_read(record["id"])
                self.json(200, {"read": True})
            elif re.fullmatch(r"/api/artifacts/[a-f0-9]{32}/publish", path) and method == "POST":
                self.body()
                self.json(200, self.engine.publish(path.split("/")[3]))
            elif re.fullmatch(r"/api/artifacts/[a-f0-9]{32}/download", path) and method == "GET":
                artifact = self.engine.store.get("artifacts", path.split("/")[3])
                self.download(artifact["file"], artifact["sha256"], artifact["filename"])
            elif re.fullmatch(r"/api/captures/[a-f0-9]{32}/download", path) and method == "GET":
                capture = self.engine.store.get("captures", path.split("/")[3])
                self.download(str(Path(capture["root"]) / capture["source"]["file"]), capture["source"]["sha256"], "preserved-source.zip")
            elif method == "GET" and not path.startswith("/api/"):
                self.static(path)
            else:
                self.json(404, {"error": "Route not found"})
        except Busy as error:
            self.json(409, {"error": str(error)})
        except KeyError as error:
            self.json(404, {"error": str(error).strip("'")})
        except (Invalid, ValueError, TypeError) as error:
            self.json(400, {"error": str(error)})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as error:
            print(f"Request failed: {type(error).__name__}: {error}", flush=True)
            self.json(500, {"error": "Operation failed; see the local server log"})

    def mcp(self, method):
        if method != "POST":
            self.respond_headers(405, "text/plain", 0, extra_headers={"Allow": "POST"})
            return
        if not self.engine.mcp.authorized(self.headers.get("Authorization")):
            body = json.dumps({"error": "Turn on the model connection in Portkeeper Settings and use its token"}).encode()
            self.respond_headers(401, "application/json", len(body), extra_headers={"WWW-Authenticate": 'Bearer realm="portkeeper"'})
            self.wfile.write(body)
            return
        reply = self.engine.mcp.handle(self.body())
        if reply is None:
            self.respond_headers(202, "application/json", 0)
            return
        self.json(200, reply)

    def image(self, content_type, body):
        self.respond_headers(200, content_type, len(body), cache_control="private, max-age=60")
        self.wfile.write(body)

    def import_download(self, project_id, rom_id, file_id):
        from .romm import _normalize_record
        project = self.engine.store.get('projects', project_id)
        record = _normalize_record(self.engine.romm._json('roms/' + str(rom_id)))
        if not self.engine.reviews.belongs(project, record):
            raise Invalid('This download does not belong to this game')
        files = [file for file in record['files'] if file['id'] == file_id and isinstance(file['name'], str) and file['name'].lower().endswith('.zip') and any(file['name'].lower().startswith(prefix.lower() + '-') for prefix in self.engine.reviews.prefixes(project))]
        if len(files) != 1:
            raise Invalid('Choose a unique portable ZIP listed for this game')
        file = files[0]
        size = file['size']
        if type(size) is not int or size < 1:
            raise Invalid('RomM has not indexed this download size yet')
        requested = self.headers.get('Range')
        byte_range, expected_range = None, None
        if requested:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', requested)
            if not match or not any(match.groups()):
                self.respond_headers(416, 'application/octet-stream', 0, extra_headers={'Content-Range': f'bytes */{size}'})
                return
            first, last = match.groups()
            start = int(first) if first else max(0, size - int(last))
            end = min(size - 1, int(last)) if first and last else size - 1
            if start >= size or end < start or (not first and int(last) == 0):
                self.respond_headers(416, 'application/octet-stream', 0, extra_headers={'Content-Range': f'bytes */{size}'})
                return
            byte_range = f'bytes={start}-{end}'
            expected_range = f'bytes {start}-{end}/{size}'
        route = f'roms/{file_id}/files/content/{urllib.parse.quote(file["name"], safe="")}'
        with self.engine.romm._open(route, byte_range=byte_range) as response:
            length = response.headers.get('Content-Length', '')
            expected_length = end - start + 1 if requested else size
            if response.status != (206 if requested else 200) or not length.isdigit() or int(length) != expected_length or (requested and response.headers.get('Content-Range') != expected_range):
                raise Invalid('RomM download changed or could not serve this range. Refresh and retry')
            headers = {'Accept-Ranges': 'bytes'}
            if expected_range:
                headers['Content-Range'] = expected_range
            name = file['name']
            disposition = "attachment; filename*=UTF-8''" + urllib.parse.quote(name, safe='')
            self.respond_headers(response.status, 'application/zip', int(length), disposition=disposition, extra_headers=headers)
            try:
                remaining = int(length)
                while remaining:
                    chunk = response.read(min(1024 * 1024, remaining))
                    if not chunk:
                        self.close_connection = True
                        break
                    self.wfile.write(chunk)
                    remaining -= len(chunk)
            except (OSError, TimeoutError):
                self.close_connection = True

    def download(self, path, expected_hash, filename):
        archive = Path(path).resolve()
        if not archive.is_relative_to(self.engine.archive_root.resolve()) or not archive.is_file():
            raise Invalid("Archive unavailable")
        if checksum(archive) != expected_hash:
            raise Invalid("Archive checksum changed")
        self.respond_headers(200, "application/zip", archive.stat().st_size, f'attachment; filename="{filename}"')
        with archive.open("rb") as stream:
            shutil.copyfileobj(stream, self.wfile)

    def static(self, request_path):
        if request_path == "/":
            path = "index.html"
        elif request_path.startswith("/static/"):
            path = urllib.parse.unquote(request_path[len("/static/"):])
        else:
            self.json(404, {"error": "File not found"})
            return
        # Serve only files shipped in static/; the request picks an entry, never a path.
        file = static_files().get(path)
        if file is None:
            raise Invalid(f"Missing file: {path}")
        data = file.read_bytes()
        content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        self.respond_headers(200, content_type, len(data))
        self.wfile.write(data)


@lru_cache(maxsize=1)
def static_files():
    root = STATIC.resolve()
    return {path.relative_to(root).as_posix(): path for path in root.rglob("*") if path.is_file() and not path.is_symlink()}

def make_server(engine, host="127.0.0.1", port=8787, allowed_hosts=()):
    server = ThreadingHTTPServer((host, port), partial(Handler, engine=engine))
    server.allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    for name in allowed_hosts:
        name = name.strip()
        if name:  # Accept "host", "host:port", "[v6]:port" or a URL.
            server.allowed_hosts.add(urllib.parse.urlsplit(name if "//" in name else "//" + name).hostname or name.lower())
    if host not in ("0.0.0.0", "::"):
        server.allowed_hosts.add(host)
    return server
