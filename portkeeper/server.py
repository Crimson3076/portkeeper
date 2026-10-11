import json
import mimetypes
import re
import shutil
import urllib.parse
from functools import partial
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .engine import Busy
from .safety import Invalid, confined, checksum


STATIC = Path(__file__).resolve().parent.parent / "static"


class Handler(BaseHTTPRequestHandler):
    server_version = "Portkeeper/0.1"

    def __init__(self, *args, engine, **kwargs):
        self.engine = engine
        super().__init__(*args, **kwargs)

    def log_message(self, fmt, *args):
        # Avoid logging query strings or local input/recipe content.
        print(f"{self.log_date_time_string()} {self.command} {urllib.parse.urlsplit(self.path).path} {args[1] if len(args) > 1 else ''}", flush=True)

    def respond_headers(self, status, content_type, length, disposition=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if disposition:
            self.send_header("Content-Disposition", disposition)
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
            return parts.hostname in self.server.allowed_hosts and parts.port in (None, self.server.server_port)
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
                self.json(403, {"error": "Unrecognized Host header; use the printed local URL"})
                return
            path = urllib.parse.urlsplit(self.path).path
            if method == "GET" and path == "/api/state":
                self.json(200, self.engine.state())
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
            elif method == "GET" and re.fullmatch(r"/api/romm/roms/[0-9]+", path):
                self.json(200, self.engine.romm.detail(int(path.split("/")[4])))
            elif method == "POST" and path == "/api/romm/inspect":
                from .profiles import supported_hashes
                payload = self.body()
                self.json(200, self.engine.romm.inspect_rom(payload.get("rom_id"), payload.get("filename"), supported_hashes(payload.get("profile", "shipwright"))))
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
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/check", path) and method == "POST":
                payload = self.body()
                self.json(202, self.engine.check(path.split("/")[3], release_version=payload.get("version")))
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/build", path) and method == "POST":
                payload = self.body()
                self.json(202, self.engine.build(path.split("/")[3], payload.get("targets"), capture_id=payload.get("capture_id")))
            elif re.fullmatch(r"/api/projects/[a-f0-9]{32}/test-build", path) and method == "POST":
                self.body()
                self.json(202, self.engine.test_build(path.split("/")[3]))
            elif re.fullmatch(r"/api/jobs/[a-f0-9]{32}/retry", path) and method == "POST":
                self.body()
                self.json(202, self.engine.retry(path.split("/")[3]))
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
        file = confined(STATIC, path, must_exist=True)
        data = file.read_bytes()
        content_type = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
        self.respond_headers(200, content_type, len(data))
        self.wfile.write(data)


def make_server(engine, host="127.0.0.1", port=8787):
    server = ThreadingHTTPServer((host, port), partial(Handler, engine=engine))
    server.allowed_hosts = {"localhost", "127.0.0.1", "::1"}
    if host not in ("0.0.0.0", "::"):
        server.allowed_hosts.add(host)
    return server
