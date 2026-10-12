"""GitHub project catalog: verification records and packaging definitions, cached locally.

The bundled catalog in the image is the first-use fallback. A refresh replaces the cache
only with data that validates; when GitHub is unavailable or returns invalid data, the
last valid cache stays in use and the error is reported with its age and source.
Catalog records describe reviewed support and evidence. They never mark a download as
tested, and a definition is applied to a project only when someone chooses to.
"""
import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path

from . import packaging
from .safety import Invalid, relative
from .storage import now

REPO_CATALOG = Path(__file__).resolve().parent.parent / "catalog"
DEFAULT_SOURCE = "https://raw.githubusercontent.com/Crimson3076/portkeeper/main/catalog"
REFRESH_SECONDS = 24 * 3600
SCOPE_STATUSES = ("candidate", "verified", "needs_review")


def validate_projects(data):
    if not isinstance(data, dict) or data.get("schema_version") != 1 or not isinstance(data.get("projects"), list) or len(data["projects"]) > 500:
        raise Invalid("Catalog projects.json must use schema_version 1 with a projects list")
    for entry in data["projects"]:
        if not isinstance(entry, dict):
            raise Invalid("Catalog project entries must be objects")
        for key in ("id", "name", "repository", "procedure_kind"):
            if not isinstance(entry.get(key), str) or not entry[key] or len(entry[key]) > 200:
                raise Invalid(f"Catalog project is missing {key}")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", entry["repository"]):
            raise Invalid("Catalog repository must be owner/repo")
        if entry.get("definition") is not None:
            path = relative(entry["definition"])
            if not str(path).startswith("catalog/definitions/") or not str(path).endswith(".json"):
                raise Invalid("Catalog definitions live under catalog/definitions/")
        scopes = entry.get("verification_scopes", [])
        if not isinstance(scopes, list):
            raise Invalid("verification_scopes must be a list")
        for scope in scopes:
            if not isinstance(scope, dict) or scope.get("status") not in SCOPE_STATUSES or not isinstance(scope.get("platform"), str):
                raise Invalid("Each verification scope needs a platform and status candidate, verified or needs_review")
            approval = scope.get("approval")
            if scope["status"] == "verified" and not (isinstance(approval, dict) and approval.get("by") and approval.get("reference")):
                raise Invalid("A verified scope must record who approved it and an approval reference")
    return data


class Catalog:
    def __init__(self, engine):
        self.engine = engine
        self.root = engine.data / "catalog"
        self.cache_path = self.root / "cache.json"
        self.source = os.environ.get("PORTKEEPER_CATALOG_URL", DEFAULT_SOURCE).rstrip("/")
        self.lock = threading.RLock()
        self.thread = None
        self._memo = None

    def bundled(self):
        projects = validate_projects(json.loads((REPO_CATALOG / "projects.json").read_text(encoding="utf-8")))
        return {"projects": projects, "definitions": packaging.bundled()}

    def cached(self):
        if not self.cache_path.is_file() or self.cache_path.is_symlink():
            return None
        try:
            record = json.loads(self.cache_path.read_text(encoding="utf-8"))
            validate_projects(record["projects"])
            return record
        except (OSError, ValueError, KeyError, Invalid):
            return None

    def _stamp(self):
        try:
            cached = self.cache_path.stat().st_mtime_ns if self.cache_path.is_file() else 0
        except OSError:
            cached = 0
        return cached, max((p.stat().st_mtime_ns for p in packaging.BUNDLED.glob("*.json")), default=0)

    def definitions(self):
        """Definitions this installation can run: bundled ones, replaced by newer valid cached revisions."""
        stamp = self._stamp()
        with self.lock:
            if self._memo and self._memo[0] == stamp:
                return json.loads(self._memo[1])
        result = self._definitions()
        with self.lock:
            self._memo = (stamp, json.dumps(result))
        return result

    def _definitions(self):
        result = self.bundled()["definitions"]
        record = self.cached()
        for key, item in ((record or {}).get("definitions") or {}).items():
            definition = item.get("definition")
            if not item.get("supported") or not isinstance(definition, dict):
                continue
            try:
                definition = packaging.validate_definition(definition)
            except Invalid:
                continue
            if key not in result or definition["revision"] > result[key]["revision"]:
                result[key] = definition
        return result

    def status(self):
        record = self.cached()
        bundled = self.bundled()
        projects = (record or {}).get("projects") or bundled["projects"]
        fetched = (record or {}).get("fetched_at")
        age = None
        if fetched:
            age = int((datetime.now(timezone.utc) - datetime.fromisoformat(fetched)).total_seconds())
        definitions = []
        current = self.definitions()
        for key, definition in sorted(current.items()):
            cached = ((record or {}).get("definitions") or {}).get(key) or {}
            definitions.append({"id": key, "name": definition["name"], "repository": definition["upstream"]["repository"],
                                "revision": definition["revision"], "runner": definition["runner"],
                                "origin": "github" if cached.get("supported") and cached.get("definition", {}).get("revision") == definition["revision"] else "bundled",
                                "supported": True, "reason": None,
                                'prepares_ps4_pkgs': any(op.get('tool') == 'ps4_pkg_extract' for op in definition.get('operations', []))})
        for key, cached in ((record or {}).get("definitions") or {}).items():
            if not cached.get("supported"):
                definitions.append({"id": key, "name": (cached.get("definition") or {}).get("name", key), "revision": (cached.get("definition") or {}).get("revision"),
                                    "origin": "github", "supported": False, "reason": cached.get("reason")})
        return {"source": self.source if record else "bundled with this Portkeeper image", "fetched_at": fetched, "age_seconds": age,
                "checked_at": (record or {}).get("checked_at") or self._meta().get("checked_at"), "error": self._meta().get("error"),
                "using": "github cache" if record else "bundled catalog", "projects": projects["projects"], "definitions": definitions,
                "runner_version": packaging.RUNNER_VERSION,
                "explanation": "Definitions that use only operations and tools this Portkeeper version provides update from the catalog without a container update. "
                               "A definition that needs a newer runner_version, operation or tool is listed but not applied until Portkeeper is updated."}

    def _meta(self):
        path = self.root / "refresh.json"
        try:
            return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
        except (OSError, ValueError):
            return {}

    def _write(self, name, record):
        self.root.mkdir(parents=True, exist_ok=True)
        temp = self.root / (name + ".part")
        temp.write_text(json.dumps(record, indent=2), encoding="utf-8")
        os.replace(temp, self.root / name)

    def _fetch(self, path):
        url = self.source + "/" + path
        with self.engine.github.open(url, "application/json") as response:
            data = response.read(2 * 1024 * 1024 + 1)
        if len(data) > 2 * 1024 * 1024:
            raise Invalid(f"{path} exceeds the catalog size limit")
        return json.loads(data)

    def refresh(self):
        with self.lock:
            checked = now()
            try:
                projects = validate_projects(self._fetch("projects.json"))
                previous = (self.cached() or {}).get("definitions") or {}
                definitions = {}
                for entry in projects["projects"]:
                    if not entry.get("definition"):
                        continue
                    name = entry["definition"][len("catalog/"):]
                    raw = self._fetch(name)
                    key = raw.get("id") if isinstance(raw, dict) else None
                    if not isinstance(key, str):
                        raise Invalid(f"{name} has no definition id")
                    support = packaging.requirements(raw)
                    if not support["supported"] and not support["needs_update"]:
                        # Invalid data keeps the last valid copy instead of replacing it.
                        if key in previous:
                            definitions[key] = previous[key]
                        continue
                    definitions[key] = {"definition": raw, "supported": support["supported"], "reason": support["reason"], "path": name}
                record = {"source": self.source, "fetched_at": checked, "checked_at": checked, "projects": projects, "definitions": definitions}
                self._write("cache.json", record)
                self._write("refresh.json", {"checked_at": checked, "error": None})
                return self.status()
            except (Invalid, OSError, ValueError) as error:
                self._write("refresh.json", {"checked_at": checked, "error": "Catalog refresh failed; the last valid catalog stays in use: " + str(error)[:300]})
                return self.status()

    def start(self):
        if self.thread and self.thread.is_alive():
            return

        def loop():
            while not self.engine.stop.is_set():
                record = self.cached()
                fetched = (record or {}).get("fetched_at")
                stale = not fetched or (datetime.now(timezone.utc) - datetime.fromisoformat(fetched)).total_seconds() > REFRESH_SECONDS
                meta = self._meta()
                recently = meta.get("checked_at") and (datetime.now(timezone.utc) - datetime.fromisoformat(meta["checked_at"])).total_seconds() < 3600
                if stale and not recently:
                    self.refresh()
                if self.engine.stop.wait(900):
                    return
        self.thread = threading.Thread(target=loop, daemon=True, name="portkeeper-catalog")
        self.thread.start()

    def update_for(self, project):
        current = project.get("packaging") or {}
        definition = self.definitions().get(current.get("id"))
        if not definition or definition["runner"] != current.get("runner") or definition["revision"] <= current.get("revision", 0):
            return None
        if definition["upstream"]["repository"].lower() != project["repo"].lower():
            return None
        return {"id": definition["id"], "revision": definition["revision"], "current_revision": current.get("revision")}

    def apply(self, project_id):
        project = self.engine.store.get("projects", project_id)
        current = project.get("packaging") or {}
        definition = self.definitions().get(current.get("id"))
        if not definition:
            matches = [d for d in self.definitions().values() if d["upstream"]["repository"].lower() == project["repo"].lower()]
            if len(matches) != 1:
                raise Invalid("The catalog has no packaging definition for this repository")
            definition = matches[0]
        if definition["runner"] == "profile" and project.get("profile") != definition.get("profile"):
            raise Invalid("Profile definitions apply only to projects created from that profile")
        targets = [t for t in project["targets"] if t in definition["targets"]]
        adopted = json.loads(json.dumps(definition))
        if not targets:
            raise Invalid("The catalog definition does not produce this project's platforms")
        adopted["history"] = adopted.get("history", []) + [f"Applied catalog revision {adopted['revision']} on {now()[:10]}."]
        if current.get("revision") and adopted["revision"] <= current["revision"] and adopted != current:
            adopted["revision"] = current["revision"] + 1
        return self.engine.update_project(project_id, {"packaging": adopted, "targets": targets})
