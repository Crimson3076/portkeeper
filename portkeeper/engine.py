import copy
import hashlib
import json
import os
import re
import shutil
import subprocess
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .safety import (Invalid, MAX_FILE, TARGETS, checksum, confined, relative,
                     safe_extract, slug, target_recipe, validate_recipe, verify_tree)
from .storage import Store, identifier, now


class Busy(Invalid):
    pass


ALLOWED_HOSTS = {"github.com", "api.github.com", "codeload.github.com", "raw.githubusercontent.com",
                 "objects.githubusercontent.com", "release-assets.githubusercontent.com"}


def allowed_url(url):
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname not in ALLOWED_HOSTS or parts.username or parts.password or parts.port not in (None, 443):
        raise Invalid("Downloads must use HTTPS on approved GitHub hosts")
    return url


class SafeRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        allowed_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Github:
    def __init__(self):
        # Ignore ambient proxy credentials/settings; this service has no secrets.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), SafeRedirect())

    def open(self, url, accept="application/octet-stream"):
        request = urllib.request.Request(allowed_url(url), headers={
            "User-Agent": "Portkeeper-POC/0.1", "Accept": accept})
        try:
            return self.opener.open(request, timeout=30)
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise Invalid("GitHub resource not found. Check the public repository and stable release.") from error
            if error.code in (403, 429):
                raise Invalid("GitHub denied the request or its unauthenticated rate limit was reached. Retry later.") from error
            raise Invalid(f"GitHub request failed (HTTP {error.code})") from error

    def json(self, url):
        with self.open(url, "application/vnd.github+json") as response:
            data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise Invalid("GitHub response exceeds size limit")
        return json.loads(data)

    def release(self, repo, version=None):
        route = "latest" if version is None else "tags/" + urllib.parse.quote(version, safe="")
        release = self.json(f"https://api.github.com/repos/{repo}/releases/{route}")
        tag = release["tag_name"]
        commit = self.json(f"https://api.github.com/repos/{repo}/commits/{urllib.parse.quote(tag, safe='')}")["sha"]
        if not re.fullmatch(r"[a-f0-9]{40}", commit):
            raise Invalid("GitHub returned an invalid commit")
        return {"version": tag, "commit": commit, "release_id": release["id"],
                "url": release["html_url"], "assets": release.get("assets", [])}

    def release_tag(self, repo, version):
        return self.release(repo, version)

    def download(self, url, output):
        output = Path(output)
        temp = output.with_suffix(output.suffix + ".part")
        total = 0
        try:
            with self.open(url) as response, temp.open("wb") as stream:
                length = response.headers.get("Content-Length")
                if length and int(length) > MAX_FILE:
                    raise Invalid("Download exceeds 1 GiB limit")
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_FILE:
                        raise Invalid("Download exceeds 1 GiB limit")
                    stream.write(chunk)
            os.replace(temp, output)
            return {"sha256": checksum(output), "size": total}
        finally:
            temp.unlink(missing_ok=True)


CAPTURE_RECIPE = {"version": 1, "mode": "capture", "payload": {"kind": "source"},
                  "notes": "Preserve the tagged source now. Add a tested target recipe before building."}


class Engine:
    def __init__(self, data_dir, input_root=None, library_root=None, enable_docker=False, github=None, enable_profile_builds=False):
        self.data = Path(data_dir).resolve()
        self.data.mkdir(parents=True, exist_ok=True)
        self.input_root = Path(input_root or self.data / "inputs").resolve()
        self.library_root = Path(library_root or self.data / "library").resolve()
        self.archive_root = self.data / "archive"
        self.work_root = self.data / "work"
        for path in (self.input_root, self.library_root, self.archive_root, self.work_root):
            path.mkdir(parents=True, exist_ok=True)
        if self.input_root == self.library_root or self.input_root.is_relative_to(self.library_root) or self.library_root.is_relative_to(self.input_root):
            raise Invalid("Input and library directories must not overlap")
        self.store = Store(self.data / "portkeeper.sqlite3")
        self.github = github or Github()
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.enable_profile_builds = enable_profile_builds
        from .romm import RomM
        from .lifecycle import Lifecycle
        from .preservation import Preservation
        from .shipwright import ShipwrightRunner
        from .assistant import RecipeAssistant
        self.romm = RomM(self.data)
        self.match_lock = threading.Lock()
        self.lifecycle = Lifecycle(self)
        self.preservation = Preservation(self)
        self.shipwright = ShipwrightRunner(self)
        self.assistant = RecipeAssistant(self.data)
        from .reviews import Reviews
        self.reviews = Reviews(self)
        from .runner import OperationRunner
        from .publishing import Publisher
        from .catalog import Catalog
        self.packager = OperationRunner(self)
        from .modeltools import ModelTools
        self.model_tools = ModelTools(self)
        from .mcp import MCP
        self.mcp = MCP(self)
        self.publisher = Publisher(self)
        self.catalog = Catalog(self)
        self.enable_docker = enable_docker
        self.docker_available = False
        self.docker_error = "Docker builds are disabled; start with --enable-docker to opt in."
        if enable_docker and shutil.which("docker"):
            try:
                probe = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"], capture_output=True, text=True, timeout=5)
                self.docker_available = probe.returncode == 0
                self.docker_error = None if self.docker_available else "Docker daemon is unavailable or permission was denied."
            except (OSError, subprocess.TimeoutExpired):
                self.docker_error = "Docker daemon is unavailable."
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="portkeeper")
        self.busy = set()
        self.scheduler = None
        self.identity_scheduler = None
        for job in self.store.all("jobs"):
            if job["status"] in ("queued", "running", "building", "checking"):
                self.store.update("jobs", job["id"], status="failed", error="Service restarted during this job. Retry explicitly.", finished_at=now())
        for trial in self.store.all("trials"):
            if trial["status"] in ("queued", "running"):
                self.store.update("trials", trial["id"], status="failed", error="Service restarted during this test build.", finished_at=now())
        for project in self.store.all("projects"):
            if project["status"] in ("checking", "building"):
                self.store.update("projects", project["id"], status="needs_attention", last_error="Previous job was interrupted. Retry explicitly.")
        self.publisher.recover()
        self.migrate_definitions()

    def migrate_definitions(self):
        """Give every project saved before schema 2 a packaging definition, keeping its behavior."""
        from . import packaging
        definitions = self.catalog.definitions()
        for project in self.store.all("projects"):
            if project.get("packaging"):
                continue
            try:
                definition = packaging.migrate(project, definitions)
            except Invalid as error:
                self.store.update("projects", project["id"], last_error="Packaging definition migration failed: " + str(error))
                continue
            recipe = project.get("recipe") if definition["runner"] in ("recipe", "profile") else packaging.recipe_for(definition)
            self.store.update("projects", project["id"], packaging=definition, recipe=recipe,
                              auto_publish_destination=project.get("auto_publish_destination") or ("library" if project.get("auto_publish") else "romm"),
                              definition_source={"kind": "migration", "migrated_at": now(), "from_mode": (project.get("recipe") or {}).get("mode", "capture")})

    def close(self):
        self.stop.set()
        if self.scheduler:
            self.scheduler.join(timeout=5)
        if self.identity_scheduler:
            self.identity_scheduler.join(timeout=5)
        self.assistant.agents.close()
        self.executor.shutdown(wait=True, cancel_futures=False)

    def state(self):
        projects = self.store.all("projects")
        for project in projects:
            if project.get('capture_id'):
                try:
                    capture = self.store.get('captures', project['capture_id'])
                    project['preserved_release'] = {key: capture.get(key) for key in ('version', 'commit', 'release_url', 'source', 'assets', 'created_at')}
                except KeyError:
                    pass
        from . import packaging
        captures = self.store.all("captures")
        all_artifacts = self.store.all("artifacts")
        catalog_definitions = self.catalog.definitions()
        for project in projects:
            definition = project.get("packaging")
            if definition:
                project["packaging_view"] = packaging.view(definition)
                known = catalog_definitions.get(definition["id"])
                if not project["packaging_view"]["first_run"] and known and known.get("first_run"):
                    project["packaging_view"]["first_run"] = packaging.view(known)["first_run"]
                project["releases"] = self.releases(project, captures, all_artifacts)
                project["catalog_update"] = self.catalog.update_for(project)
        all_jobs = self.store.all("jobs")
        jobs = all_jobs[:150]
        included = {job['id'] for job in jobs}
        for project in projects:
            latest_job = next((job for job in all_jobs if job['project_id'] == project['id']), None)
            if latest_job and latest_job['id'] not in included:
                jobs.append(latest_job)
                included.add(latest_job['id'])
        artifacts = all_artifacts
        for artifact in artifacts:
            project = next((p for p in projects if p["id"] == artifact["project_id"]), {})
            artifact["known_good"] = project.get("known_good", {}).get(artifact["target"]) == artifact["id"]
            artifact.setdefault("validation_level", "packaged")
        from .profiles import catalog
        public_romm = self.romm.public_config()
        public_romm["token_configured"] = public_romm.get("has_token", False)
        health = self.get_setting("romm_health", {})
        public_romm["last_test"] = health if health.get("url") == public_romm.get("url") else None
        public_romm["connected"] = bool(public_romm.get("last_test", {}).get("connected")) if public_romm["last_test"] else False
        assistant = self.assistant.public_config()
        proposals = [p for p in self.store.all("proposals") if p.get("kind") in ('definition', 'investigation') and p.get("status") == "pending_review"]
        return {"projects": projects, "jobs": jobs, "artifacts": artifacts, "proposals": proposals, "activity": self.reviews.activity(), "activity_cutoffs": self.get_setting("activity_cutoffs", {"projects": {}}), "playtests": self.store.all("playtests")[:1000],
                "settings": {"input_root": str(self.input_root), "library_root": str(self.library_root),
                             "archive_root": str(self.archive_root), "docker_enabled": self.enable_docker,
                             "toolchain": self.get_setting("toolchain", {})},
                "runner": {"docker_available": self.docker_available, "docker_error": self.docker_error,
                           "profile_builds_enabled": self.enable_profile_builds, "profile_runner": self.shipwright.availability(),
                           "packaging_runner": {**self.packager.availability(), "runner_version": packaging.RUNNER_VERSION}},
                "catalog": self.catalog.status(),
                "romm": public_romm, "profiles": catalog(), "notifications": self.lifecycle.notifications(),
                "assistant": assistant, "mcp": self.mcp.public_config(),
                "summary": {"projects": len(projects), "artifacts": len(artifacts),
                            "needs_attention": sum(p["status"] == "needs_attention" for p in projects)}}

    def releases(self, project, captures, artifacts):
        """Per preserved release: whether it was only preserved, needs review, or was packaged and published."""
        result = []
        for capture in captures:
            if capture["project_id"] != project["id"]:
                continue
            built = [a for a in artifacts if a["project_id"] == project["id"] and (a.get("manifest") or {}).get("capture_id") == capture["id"]]
            review = capture.get("review")
            packaged = sorted({a["target"] for a in built})
            in_romm = sorted({a["target"] for a in built if (a.get("romm") or {}).get("status") == "in_romm"})
            if packaged:
                status = "packaged"
            elif not capture.get("complete", True):
                status = "incomplete"
            elif review and review.get("status") == "needs_review":
                status = "needs_review"
            else:
                status = "preserved"
            result.append({"capture_id": capture["id"], "version": capture.get("version"), "commit": capture.get("commit"),
                           "created_at": capture.get("created_at"), "release_url": capture.get("release_url"),
                           "complete": capture.get("complete", True), "review": review, "status": status,
                           "packaged_targets": packaged, "romm_targets": in_romm})
        return result[:30]

    def input_hashes(self, project):
        return {key: value.get("sha1") or value.get("sha256") for key, value in (project.get("input_bindings") or {}).items()
                if isinstance(value, dict) and (value.get("sha1") or value.get("sha256"))}

    def templates(self):
        return {"recipes": [
            {"id": "shipwright", "name": "Ship of Harkinian · source preservation", "description": "Archives stable tagged source. Asset extraction and playable builds need your own tested recipe.", "recipe": CAPTURE_RECIPE},
            {"id": "release", "name": "Release assembly · fill in your files", "description": "Select an upstream asset for each target, supply input SHA-256 values, and declare expected output files.", "recipe": {
                "version": 1, "mode": "capture", "payload": {"kind": "release", "asset_patterns": {}},
                "inputs": [], "expected_files": [], "notes": "Change mode to assemble after configuring patterns and expected files."}}],
                "definitions": [{"id": d["id"], "name": d["name"], "repository": d["upstream"]["repository"], "runner": d["runner"],
                                 "revision": d["revision"], "definition": d} for d in self.catalog.definitions().values()]}

    def get_setting(self, key, default=None):
        try:
            return self.store.get("settings", key)["value"]
        except KeyError:
            return copy.deepcopy(default)

    def save_setting(self, key, value):
        self.store.put("settings", {"id": key, "value": value})
        return value

    def configure_romm(self, payload):
        result = self.romm.configure(payload.get("url"), payload.get("token"), payload.get("clear_token", False))
        self.save_setting("romm_health", {})
        return result

    def test_romm(self):
        try:
            result = self.romm.test_connection()
            self.save_setting("romm_health", {"url": self.romm.public_config()["url"], "connected": True, "checked_at": now(), "result": result})
            return result
        except Invalid as error:
            self.save_setting("romm_health", {"url": self.romm.public_config()["url"], "connected": False, "checked_at": now(), "error": str(error)})
            raise

    def stage(self, job_id, stage, message):
        job = self.store.get("jobs", job_id)
        checkpoints = job.get("checkpoints", [])
        if checkpoints and checkpoints[-1]["stage"] != stage:
            checkpoints[-1]["status"] = "complete"
        if not checkpoints or checkpoints[-1]["stage"] != stage:
            checkpoints.append({"stage": stage, "started_at": now(), "status": "complete" if stage == "complete" else "running"})
        self.store.update("jobs", job_id, stage=stage, checkpoints=checkpoints, **({"status": "running"} if job.get("status") == "queued" and stage not in ("queued", "complete") else {}))
        self.store.append_log(job_id, message)

    def notify(self, project_id, kind, message, **details):
        return self.lifecycle.notify(project_id, kind, str(message)[:1000], **details)

    def retry(self, job_id):
        job = self.store.get("jobs", job_id)
        if job["status"] != "failed":
            raise Invalid("Only failed jobs can be retried")
        if job.get("kind") == "publish":
            return self.publisher.queue(job["artifact_id"])
        if job.get('kind') == 'investigation':
            return self.queue_investigation(job['project_id'], job.get('instruction', ''), job.get('investigation_max_calls', 3), job.get('prepare_game_files', True))
        if job.get("kind") == "draft":
            return self.queue_draft(job["project_id"], job.get("instruction", ""), targets=job.get("requested_targets"), prepare_game_files=job.get('prepare_game_files', True), review_model=job.get('review_model', False), max_calls=job.get('draft_max_calls'), investigation_id=job.get('investigation_id'), test_inputs=job.get('test_inputs', False))
        project = self.store.get("projects", job["project_id"])
        if job["target"] == "capture":
            return self.check(project["id"], build_after=job.get("build_after", False), release_version=job.get("release_version"))
        return self.build(project["id"], [job["target"]], capture_id=job.get("capture_id"))

    def validate_project(self, config):
        if not isinstance(config, dict):
            raise Invalid("Project must be an object")
        config = copy.deepcopy(config)
        profile_id = config.get("profile")
        if profile_id:
            from .profiles import profile, supported_hashes
            spec = profile(profile_id)
            config.setdefault("name", spec["name"])
            if config.get("repo", spec["repository"]) != spec["repository"]:
                raise Invalid("Verified profile repository cannot be changed")
            config["repo"] = spec["repository"]
            if any(t not in ("linux", "windows") for t in config.get("targets", ["linux"])):
                raise Invalid("Shipwright's verified profile supports Linux / Steam Deck and Windows x86-64")
            canonical = spec["default_recipe"]
            proposed = config.get("recipe")
            if proposed is not None:
                validate_recipe(proposed)
                normalized = copy.deepcopy(proposed)
                notes = normalized.pop("notes", None)
                # Accept the equivalent asset patterns written by the first worker revision.
                patterns = normalized.get("payload", {}).get("asset_patterns", {})
                if patterns == {"linux": r"^SoH-.*\.appimage$", "windows": r"^SoH-.*-Win64\.zip$"}:
                    normalized["payload"]["asset_patterns"] = copy.deepcopy(canonical["payload"]["asset_patterns"])
                if normalized != canonical:
                    raise Invalid("Verified profile execution instructions are fixed. Use a custom project for a different recipe")
                if notes is not None:
                    if not isinstance(notes, str) or len(notes) > 4000:
                        raise Invalid("Profile notes must be text up to 4000 characters")
                    canonical["notes"] = notes
            config["recipe"] = canonical
            origin = config.get("rom_source")
            if origin is not None:
                if not isinstance(origin, dict) or origin.get("sha1") not in supported_hashes(profile_id):
                    raise Invalid("Select a ROM revision supported by this profile")
                if any(k not in {"kind", "path", "rom_id", "filename", "member", "sha1", "cache_key"} for k in origin):
                    raise Invalid("Unexpected ROM source fields")
                if origin.get("kind") == "local":
                    relative(origin.get("path"))
                elif origin.get("kind") != "romm" or not isinstance(origin.get("rom_id"), int) or isinstance(origin.get("rom_id"), bool) or not isinstance(origin.get("filename"), str):
                    raise Invalid("Choose a RomM file or a local input file")
                if origin.get("member"):
                    relative(origin["member"])
        name = config.get("name", "").strip() if isinstance(config.get("name", ""), str) else ""
        repo = config.get("repo", "")
        if not name or len(name) > 100:
            raise Invalid("Project name is required (up to 100 characters)")
        if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo) or any(p in (".", "..") for p in repo.split("/")):
            raise Invalid("Repository must be owner/repo for a public GitHub project")
        targets = config.get("targets", ["linux"])
        if not isinstance(targets, list) or not targets or any(t not in TARGETS for t in targets) or len(set(targets)) != len(targets):
            raise Invalid("Choose unique targets: linux, windows, handheld")
        interval = config.get("poll_minutes", 360)
        if isinstance(interval, bool) or not isinstance(interval, int) or not 1 <= interval <= 10080:
            raise Invalid("Polling interval must be 1–10080 minutes")
        auto_build = config.get("auto_build", False)
        if not isinstance(auto_build, bool):
            raise Invalid("auto_build must be true or false")
        auto_publish = config.get("auto_publish", False)
        if not isinstance(auto_publish, bool):
            raise Invalid("auto_publish must be true or false")
        if auto_publish and not auto_build:
            raise Invalid("Enable automatic builds before enabling automatic publication")
        recipe = copy.deepcopy(validate_recipe(config.get("recipe", CAPTURE_RECIPE)))
        from . import packaging
        definition = config.get("packaging")
        if definition is not None:
            definition = packaging.validate_definition(definition)
            if definition["upstream"]["repository"].lower() != repo.lower():
                raise Invalid("The packaging definition describes another repository")
            unsupported = [t for t in targets if t not in definition["targets"]]
            if unsupported:
                raise Invalid("The packaging definition does not produce: " + ", ".join(unsupported))
            if definition["runner"] == "profile" and definition.get("profile") != profile_id:
                raise Invalid("Profile definitions belong to projects created from that verified profile")
            if definition["runner"] in ("operations", "capture"):
                recipe = packaging.recipe_for(definition)
            elif definition["runner"] == "recipe":
                recipe = copy.deepcopy(validate_recipe(definition["recipe"]))
        elif recipe["mode"] == "definition":
            raise Invalid("A definition-backed recipe needs its packaging definition")
        bindings = config.get("input_bindings") or {}
        if not isinstance(bindings, dict) or len(bindings) > 20:
            raise Invalid("Game file selections must be an object")
        declared = {item["id"] for item in (definition or {}).get("inputs", [])}
        for key, value in bindings.items():
            if key not in declared or not isinstance(value, dict) or value.get("source") not in ("inputs", "prepared") or set(value) - {"source", "path", "sha1", "sha256", "accepted", "verified_at", "ps4"}:
                raise Invalid("Game file selections must name a declared input, its source and a relative path")
            relative(value.get("path"))
            if 'ps4' in value:
                metadata = value['ps4']
                if not isinstance(metadata, dict) or set(metadata) - {'title_id', 'app_version', 'role'} or any(v is not None and (not isinstance(v, str) or len(v) > 100) for v in metadata.values()):
                    raise Invalid('Invalid PS4 metadata in the game file selection')
        destination = config.get("auto_publish_destination", "romm")
        if destination not in ("romm", "library"):
            raise Invalid("Automatic publication goes to RomM or to the local library folder")
        if recipe["mode"] == "profile" and not profile_id:
            raise Invalid("Select a verified project profile before using its fixed recipe")
        if auto_build and recipe["mode"] == "capture":
            raise Invalid("Configure a build recipe before enabling automatic builds")
        auto_draft_platforms = config.get("auto_draft_platforms", False)
        if not isinstance(auto_draft_platforms, bool):
            raise Invalid("auto_draft_platforms must be true or false")
        checked = {"name": name, "repo": repo, "targets": targets,
                "poll_minutes": interval, "auto_build": auto_build, "auto_publish": auto_publish, "recipe": recipe,
                "packaging": definition, "input_bindings": bindings, "auto_publish_destination": destination, "auto_draft_platforms": auto_draft_platforms}
        metadata = config.get("metadata")
        if metadata is not None:
            from .romm_identity import validate_identity
            metadata = validate_identity(metadata)
        checked["metadata"] = metadata
        if profile_id:
            checked.update(profile=profile_id, rom_source=config.get("rom_source"))
        return checked

    def create_project(self, config):
        if isinstance(config, dict) and config.get("packaging") is None and not config.get("demo_fixture"):
            # New projects start from the catalog definition for their repository, when there is one.
            from . import packaging
            draft = self.validate_project(config)
            try:
                config = {**config, "packaging": packaging.migrate({**draft, "recipe": draft["recipe"]}, self.catalog.definitions())}
            except Invalid:
                pass
        project = {**self.validate_project(config), "id": identifier(),
                   "created_at": now(), "last_checked": None, "status": "idle", "last_error": None,
                   "latest_version": None, "capture_id": None}
        return self.store.put("projects", project)

    def update_project(self, project_id, config):
        with self.lock:
            if project_id in self.busy:
                raise Busy("Wait for the running project job before editing its recipe")
            project = self.store.get("projects", project_id)
            allowed = {"name", "targets", "poll_minutes", "auto_build", "auto_publish", "recipe", "rom_source", "metadata",
                       "packaging", "input_bindings", "auto_publish_destination", "auto_draft_platforms"}
            if not isinstance(config, dict) or any(key not in allowed for key in config):
                raise Invalid("Only name, targets, poll_minutes, automation, packaging, game files, recipe and metadata can be edited")
            config = self._definition_update(project, config)
            merged = {**project, **config}
            checked = self.validate_project(merged)
            for key in allowed:
                if key in checked:
                    project[key] = checked[key]
            previous = self.store.get("projects", project_id).get("packaging")
            if previous and previous != project.get("packaging"):
                history = project.get("definition_history", [])
                history.append({"saved_at": now(), "definition": copy.deepcopy(previous)})
                project["definition_history"] = history[-20:]
            return self.store.put("projects", project)

    def validate_configuration(self, project_id, payload):
        """Check edits using the same rules as saving, without changing state."""
        if not isinstance(payload, dict) or set(payload) - {"packaging", "recipe"} or not payload:
            raise Invalid("Provide packaging or recipe configuration to validate")
        project = self.store.get("projects", project_id)
        config = self._definition_update(project, payload)
        checked = self.validate_project({**project, **config})
        return {"valid": True, "configuration": checked.get("packaging") or checked["recipe"]}

    def _definition_update(self, project, config):
        """Keep the definition the source of truth when an older client edits only the recipe."""
        current = project.get("packaging")
        if not current:
            return config
        config = dict(config)
        if "recipe" in config and "packaging" not in config:
            mode = (config["recipe"] or {}).get("mode") if isinstance(config["recipe"], dict) else None
            if current["runner"] == "operations":
                if mode != "definition":
                    raise Invalid("This project is built from its packaging definition. Edit the definition instead of a recipe.")
                config.pop("recipe")
            elif current["runner"] in ("capture", "recipe") and mode in ("capture", "assemble", "docker"):
                updated = copy.deepcopy(current)
                updated.pop("recipe", None)
                if mode == "capture":
                    updated.update(runner="capture", operations=[])
                else:
                    updated.update(runner="recipe", recipe=copy.deepcopy(config["recipe"]), operations=[])
                config["packaging"] = updated
        if "packaging" in config and isinstance(config["packaging"], dict):
            proposed = copy.deepcopy(config["packaging"])
            strip = lambda d: {k: v for k, v in d.items() if k != "revision"}
            if strip(proposed) != strip(current) and isinstance(proposed.get("revision"), int) and proposed["revision"] <= current.get("revision", 1):
                # Changed instructions get a new revision so packages and tests never look identical.
                proposed["revision"] = current.get("revision", 1) + 1
            config["packaging"] = proposed
            config.pop("recipe", None)
            if "input_bindings" not in config:
                declared = {item.get("id") for item in proposed.get("inputs", []) if isinstance(item, dict)}
                config["input_bindings"] = {k: v for k, v in (project.get("input_bindings") or {}).items() if k in declared}
        return config

    def bind_input(self, project_id, payload):
        """Choose or automatically find the user's file for one declared input."""
        from . import ps4
        project = self.store.get("projects", project_id)
        definition = project.get("packaging") or {}
        input_id = payload.get("input")
        item = next((i for i in definition.get("inputs", []) if i["id"] == input_id), None)
        if not item:
            raise Invalid("Choose a declared game file")
        bindings = dict(project.get("input_bindings") or {})
        expectation = ps4.input_expectation(definition, input_id)
        if payload.get("clear"):
            bindings.pop(input_id, None)
            if expectation:
                for op in definition.get('operations', []):
                    if input_id in op.get('packages', []) and op.get('prepared_input'):
                        bindings.pop(op['prepared_input'], None)
            return self.update_project(project_id, {"input_bindings": bindings})
        if payload.get("path"):
            source = payload.get("source", "inputs")
            root = self.packager.prepared if source == "prepared" else self.input_root
            path = confined(root, payload["path"])
            if (item["kind"] == "file" and not path.is_file()) or (item["kind"] == "directory" and not path.is_dir()):
                raise Invalid("That file or folder does not exist below the configured input root")
            binding = {"source": source, "path": str(relative(payload["path"]))}
            if expectation:
                identified = self.packager.pkg_metadata(path)
                problem = ps4.mismatch(identified, expectation)
                if problem:
                    raise Invalid(problem + '. Choose the correct base game or matching update PKG.')
                binding['ps4'] = {key: identified[key] for key in ('title_id', 'app_version', 'role')}
                for op in definition.get('operations', []):
                    if input_id in op.get('packages', []) and op.get('prepared_input'):
                        bindings.pop(op['prepared_input'], None)
            prepared_tool = next((op for op in definition.get('operations', []) if op.get('prepared_input') == input_id), None)
            if prepared_tool:
                if prepared_tool.get('tool') == 'wwhd_extract':
                    verified = self.packager.validate_wiiu_folder(prepared_tool, path)
                    binding.update(accepted=verified['title'] + ' version 0', verified_at=now())
                else:
                    verified, problem = self.packager.validate_ps4(prepared_tool, path)
                    if problem:
                        raise Invalid('Prepared game folder cannot be used: ' + problem)
                    binding.update(accepted=verified['title_id'] + ' ' + verified['app_version'], verified_at=now())
            if item["kind"] == "file" and item.get("verify", "hash") == "hash":
                from .runner import accepted, digests
                digest = digests(path, item.get("normalize"))
                match = accepted(item, digest)
                if not match:
                    raise Invalid(f"This file is not an accepted revision (SHA-1 {digest['sha1']})")
                binding.update(sha1=digest["sha1"], accepted=match["label"], verified_at=now())
            bindings[input_id] = binding
            return self.update_project(project_id, {"input_bindings": bindings})
        found = self.packager.locate(project, definition, input_id)
        if len(found) != 1:
            return {"project": project, "found": found, "message": "No accepted file was found" if not found else "Several files match; choose one"}
        return {"project": self.bind_input(project_id, {'input': input_id, **found[0]}), "found": found}

    def project_game_files(self, project_id):
        project = self.store.get('projects', project_id)
        definition = project.get('packaging')
        if not definition:
            raise Invalid('Add packaging steps before choosing game files')
        return {'inputs': self.packager.find_files(definition), 'input_root': str(self.input_root)}

    def link_metadata(self, project_id, payload):
        rom_id = payload.get("romm_rom_id")
        if type(rom_id) is not int or rom_id < 1:
            raise Invalid("Choose a RomM entry ID")
        reference = self.romm._json("roms/" + str(rom_id))
        if not reference.get("igdb_id"):
            raise Invalid("Match this entry to a game in RomM first, then link it here")
        identity = {"romm_rom_id": rom_id, "igdb_id": reference["igdb_id"],
                    "game": payload.get("game") or reference.get("name"),
                    "filename_prefix": payload.get("filename_prefix"),
                    "cover_policy": payload.get("cover_policy", "preserve")}
        if identity["cover_policy"] == "custom":
            identity["url_cover"] = payload.get("url_cover")
        return self.update_project(project_id, {"metadata": identity})

    def choose_cover(self, project_id, payload):
        """Apply one picked cover to every Linux and Windows import of this project in RomM."""
        project = self.store.get("projects", project_id)
        metadata = dict(project.get("metadata") or {})
        prefix = payload.get("filename_prefix") or metadata.get("filename_prefix") or project.get("library_basename")
        if not prefix:
            raise Invalid("Enter the package filename prefix, or publish a build first")
        metadata.update(game=metadata.get("game") or project["name"], filename_prefix=prefix,
                        cover_policy="custom", url_cover=payload.get("url_cover"))
        project = self.update_project(project_id, {"metadata": metadata})
        # Platform-specific projects sharing one import prefix must agree on artwork.
        # Otherwise a later registration can silently override the user's selection.
        from .romm_identity import validate_identity
        for related in self.store.all("projects"):
            identity = related.get("metadata") or {}
            if related["id"] != project_id and identity.get("filename_prefix", "").lower() == prefix.lower() and identity.get("igdb_id") == metadata.get("igdb_id"):
                shared = {**identity, "cover_policy": "custom", "url_cover": metadata["url_cover"]}
                self.store.update("projects", related["id"], metadata=validate_identity(shared))
        result = {"project": project, "matched": None, "warning": None}
        try:
            result["matched"] = self.match_imports_now()["matched"]
        except Exception:
            result["warning"] = "Cover saved. RomM could not be updated yet; Portkeeper retries every minute. Check the token has roms.write."
        return result

    def match_imports_now(self):
        from .romm_identity import ImportIdentities
        with self.match_lock:
            return ImportIdentities(self.romm, self.data, self.store).sync()

    def claim(self, project_id, expected_project=None):
        with self.lock:
            current = self.store.get("projects", project_id)
            if expected_project is not None and any(current.get(key) != expected_project.get(key) for key in ("packaging", "recipe", "targets")):
                raise Busy("This game's instructions changed while the request was being prepared; try again")
            if project_id in self.busy:
                raise Busy("A job is already running for this project")
            self.busy.add(project_id)

    def release_claim(self, project_id):
        with self.lock:
            self.busy.discard(project_id)
            project = self.store.get("projects", project_id)
            if project.get("retention_enabled"):
                try:
                    self.lifecycle.apply_retention(project_id, project.get("retention_keep_last"))
                except (Invalid, OSError) as error:
                    self.notify(project_id, "build_failed", "Retention cleanup needs attention: " + str(error),
                                event_key="retention:" + project_id + ":" + str(error)[:100])

    def apply_retention(self, project_id, keep_last=None):
        with self.lock:
            result = self.lifecycle.apply_retention(project_id, keep_last)
            self.store.update("projects", project_id, retention_enabled=keep_last is not None, retention_keep_last=keep_last)
            return result

    def job(self, project, target, version=None):
        record = {"id": identifier(), "project_id": project["id"], "project_name": project["name"],
                  "target": target, "version": version, "status": "queued", "created_at": now(),
                  "finished_at": None, "error": None, "logs": [], "artifact_id": None, "stage": "queued", "checkpoints": []}
        return self.store.put("jobs", record)

    def check(self, project_id, build_after=False, release_version=None, publish_after=False, draft_after=False, publication_override=None):
        project = self.store.get("projects", project_id)
        if project.get("demo"):
            raise Invalid("The synthetic demo is local and has no upstream updates")
        if release_version is not None:
            from .profiles import ensure_release
            if not project.get("profile"):
                raise Invalid("Explicit release capture is available for verified profiles")
            ensure_release(project["profile"], release_version)
        self.claim(project_id, expected_project=project)
        job = self.job(project, "capture")
        job = self.store.update("jobs", job["id"], build_after=build_after, release_version=release_version,
                                publish_after=publish_after, draft_after=draft_after)
        if publication_override is not None:
            job = self.store.update("jobs", job["id"], publication_override=publication_override)
        self.executor.submit(self._check, project, job, build_after, release_version)
        return {"job": job}

    def setup(self, project_id, publish=True, publication_override=None):
        """Set up a game in one step: preserve the latest release, build it and send it to RomM."""
        project = self.store.get("projects", project_id)
        runner = (project.get("packaging") or {}).get("runner")
        if runner == "capture" or (runner is None and (project.get("recipe") or {}).get("mode") == "capture"):
            raise Invalid("This game has no packaging steps yet. Draft or write them first.")
        if publish and not self.romm.public_config().get("configured"):
            publish = False
        if project.get("profile"):
            return self.test_build(project_id)
        return self.check(project_id, build_after=True, publish_after=publish, publication_override=publication_override)

    def test_build(self, project_id):
        project = self.store.get("projects", project_id)
        if project.get("demo"):
            return self.build(project_id)
        if project.get("profile"):
            compatible = self.compatible_capture(project)
            if compatible:
                return self.build(project_id, capture_id=compatible["id"])
            from .profiles import profile
            return self.check(project_id, build_after=True, release_version=profile(project["profile"])["tested_release"])
        return self.check(project_id, build_after=True)

    def _check(self, project, job, build_after=False, release_version=None):
        jid = job["id"]
        log = lambda message: self.store.append_log(jid, message)
        try:
            self.store.update("jobs", jid, status="checking")
            self.store.update("projects", project["id"], status="checking", last_error=None)
            self.stage(jid, "checking", "Looking for release " + release_version if release_version else "Looking for the latest stable release")
            log(f"Checking {release_version or 'latest stable release'} of {project['repo']}")
            release = self.github.release_tag(project["repo"], release_version) if release_version else self.github.release(project["repo"])
            recipe = project["recipe"]
            definition = project.get("packaging") if (project.get("packaging") or {}).get("runner") == "operations" else None
            if definition:
                from . import packaging
                wanted = packaging.asset_patterns(definition)
            else:
                patterns = {target: target_recipe(recipe, target).get("payload", {}).get("asset_patterns", {}).get(target)
                            for target in project["targets"]}
                wanted = [patterns[target] for target in project["targets"] if patterns.get(target)]
            selected = []
            for asset in release["assets"]:
                name = asset.get("name", "")
                if len(name) > 255 or not name:
                    raise Invalid("Invalid upstream asset name")
                if any(re.search(pattern, name) for pattern in wanted):
                    selected.append(asset)
            if len(selected) > 30:
                raise Invalid("Asset patterns matched too many files (limit 30)")
            key = hashlib.sha256(json.dumps({"repo": project["repo"], "commit": release["commit"], "release_id": release["release_id"],
                                            "assets": [(a["id"], a.get("updated_at"), a.get("size")) for a in selected]}, sort_keys=True).encode()).hexdigest()
            previous = next((c for c in self.store.all("captures") if c["project_id"] == project["id"] and c.get("capture_key") == key), None)
            changed = not previous or not previous.get("complete", True)
            if previous and previous.get("complete", True):
                capture = previous
                log("This source revision and selected assets are already archived")
            else:
                cid = previous["id"] if previous else identifier()
                root = Path(previous["root"]) if previous else self.archive_root / "sources" / project["id"] / cid
                root.mkdir(parents=True, exist_ok=True)
                source_url = f"https://codeload.github.com/{project['repo']}/zip/{release['commit']}"
                log(f"Capturing source at {release['commit'][:12]}")
                self.stage(jid, "preserving_source", "Preserving source; completed downloads are reused on retries")
                source = previous.get("source") if previous else None
                if not source or not (root / source["file"]).is_file() or checksum(root / source["file"]) != source["sha256"]:
                    source = {"file": "source.zip", "url": source_url, **self.github.download(source_url, root / "source.zip")}
                assets = previous.get("assets", []) if previous else []
                capture = {"id": cid, "project_id": project["id"], "repo": project["repo"], "capture_key": key,
                           "version": release["version"], "commit": release["commit"], "release_url": release["url"],
                           "created_at": now(), "root": str(root), "source": source, "assets": assets, "complete": False}
                # Preserve a usable source record even if a later release asset fails.
                self.checkpoint_capture(capture)
                if release_version is None or not project.get("capture_id"):
                    self.store.update("projects", project["id"], capture_id=cid, latest_version=capture["version"])
                for number, asset in enumerate(selected):
                    self.stage(jid, "preserving_assets", "Preserving release assets")
                    filename = f"asset-{number}-{slug(asset['name'])}"
                    saved = next((a for a in assets if a["name"] == asset["name"]), None)
                    if saved and (root / saved["file"]).is_file() and checksum(root / saved["file"]) == saved["sha256"]:
                        continue
                    if saved:
                        assets.remove(saved)
                    log(f"Capturing release asset: {asset['name']}")
                    assets.append({"name": asset["name"], "file": filename,
                                   "url": asset["browser_download_url"], **self.github.download(asset["browser_download_url"], root / filename)})
                    self.checkpoint_capture(capture)
                capture["complete"] = True
                self.checkpoint_capture(capture)
                log(f"Preserved source and {len(assets)} selected release assets")
            fields = {"last_checked": now(), "status": "archived", "last_error": None}
            if release_version is None or not project.get("capture_id"):
                fields.update(latest_version=capture["version"], capture_id=capture["id"])
            self.store.update("projects", project["id"], **fields)
            check_message = (f"Upstream changes saved for {capture['version']}." if changed
                             else f"No new release or download changes; latest is {capture['version']}.")
            if changed and not build_after and not project['auto_build']:
                check_message += " Automatic builds are off."
            self.store.update("jobs", jid, version=capture["version"], status="archived", finished_at=now(),
                              upstream_changed=changed, check_outcome="changed" if changed else "unchanged", message=check_message)
            if project.get("profile"):
                from .profiles import ensure_release
                ensure_release(project["profile"], capture["version"], capture["commit"])
                self.store.update("projects", project["id"], compatible_capture_id=capture["id"], compatible_version=capture["version"])
                self.shipwright.capture_dependencies(lambda stage, message: self.stage(jid, stage, message))
            review = None
            if definition:
                review = self.review_release(project, definition, capture, log)
            self.watch_platform_releases(project, release, log)
            self.stage(jid, "complete", check_message)
            if review and review["status"] == "needs_review":
                self.store.update("jobs", jid, check_outcome="needs_review", message=f"{capture['version']} was preserved but needs instruction review before building.")
                if build_after or project["auto_build"]:
                    log("Not packaged: this release needs review first")
                return
            if job.get("draft_after") and not definition and self.assistant.connected():
                self.queue_draft(project["id"])
            if build_after or (changed and project["auto_build"]):
                planned, skipped = self.plan_targets(self.store.get("projects", project["id"]), capture, project["targets"])
                message = self.skipped_message(capture, skipped)
                if message:
                    log(message)
                if job.get("publish_after"):
                    self.publish_existing(skipped, log)
                if not planned and skipped:
                    self.store.update("jobs", jid, check_outcome="packages_current", message=f"Existing packages already match {capture['version']} and the current instructions and inputs; no rebuild needed.")
                if planned:
                    self.store.update("jobs", jid, check_outcome="builds_queued", message=f"Builds queued for {capture['version']}: " + ", ".join(target for target, _key in planned))
                    jobs = self.queue_jobs(project, capture, planned, bool(job.get("publish_after")))
                    if job.get("publication_override") is not None:
                        jobs = [self.store.update("jobs", item["id"], publication_override=job["publication_override"]) for item in jobs]
                    self._build_group(project, capture, jobs)
        except Exception as error:
            log(str(error))
            self.store.update("jobs", jid, status="failed", error=str(error), finished_at=now())
            self.store.update("projects", project["id"], status="needs_attention", last_error=str(error), last_checked=now())
            current_job = self.store.get("jobs", jid)
            self.notify(project["id"], "upstream_unavailable" if current_job.get("stage") == "checking" or "not found" in str(error).lower() else "build_failed",
                                  str(error), jobid=jid, event_key=f"check:{project['id']}:{str(error)[:180]}")
        finally:
            self.release_claim(project["id"])

    def watch_platform_releases(self, project, release, log):
        """Notify on new OS release assets. Drafting is opt-in; never change live builds."""
        from . import platforms
        detected = platforms.detect(release.get("assets", []))
        released = {item["id"] for item in detected["platforms"] if item["support"] == "released"}
        current = self.store.get("projects", project["id"])
        previous = current.get("platform_release_seen")
        self.store.update("projects", project["id"], platform_release_seen=sorted(set(previous or []) | released))
        if previous is None:
            return  # The first successful check establishes a baseline, not a new-platform event.
        new = released - set(previous)
        for platform in sorted(new):
            label = platforms.LABELS[platform]
            note = "Review platform choices and packaging steps under Advanced." if platform in platforms.PACKAGING else "This runner does not package that OS yet."
            self.notify(project["id"], "new_platform", f"{label} release files appeared in {release['version']}. {note}",
                        event_key=f"platform:{project['id']}:{platform}")
            log(f"New {label} release files detected; existing platform selection is unchanged")
        supported = new & platforms.PACKAGING
        pending = any(p.get("project_id") == project["id"] and p.get("status") == "pending_review" for p in self.store.all("proposals"))
        drafting = any(j["project_id"] == project["id"] and j.get("kind") == "draft" and j["status"] in ("queued", "running") for j in self.store.all("jobs"))
        if supported and current.get("auto_draft_platforms") and not pending and not drafting and self.assistant.connected() and not current.get("profile"):
            targets = sorted(set(current["targets"]) | supported)
            try:
                self.queue_draft(project["id"], "A new native OS release download was detected. Add packaging for the requested platforms using real release files. Keep existing platforms; do not invent assets. This draft must be reviewed before use.", targets=targets)
                log("Queued a model draft for the new platforms; approval is required before use")
            except Invalid as error:
                log(f"Could not queue the optional platform draft: {error}")

    def review_release(self, project, definition, capture, log=None):
        """Apply compatibility rules; preserve the release either way and report what needs review."""
        from . import packaging
        if log:
            self.packager.capture_dependencies(definition, log)
        review = packaging.evaluate_release(definition, capture, self.input_hashes(project), targets=project["targets"])
        self.store.update("captures", capture["id"], review={**review, "checked_at": now()})
        if log:
            if review["status"] == "needs_review":
                log("Release preserved, but packaging needs review: " + "; ".join(review["problems"]))
            else:
                log(" ".join(review["notes"]))
        if review["status"] == "needs_review":
            self.notify(project["id"], "needs_review",
                        f"{project['name']} {capture['version']} was preserved but not packaged. Review: " + "; ".join(review["problems"]),
                        event_key=f"review:{project['id']}:{capture['id']}:{definition['revision']}")
        return review

    def checkpoint_capture(self, capture):
        root = Path(capture["root"])
        temp = root / "capture.json.part"
        temp.write_text(json.dumps(capture, indent=2), encoding="utf-8")
        os.replace(temp, root / "capture.json")
        self.store.put("captures", capture)

    def compatible_capture(self, project, targets=None):
        from .profiles import ensure_release
        chosen = project["targets"] if targets is None else targets
        for capture in self.store.all("captures"):
            if capture["project_id"] != project["id"] or not capture.get("complete", True):
                continue
            try:
                spec = ensure_release(project["profile"], capture["version"], capture["commit"])
            except Invalid:
                continue
            names = {asset["name"] for asset in capture.get("assets", [])}
            if any(spec["runtimes"][target]["name"] not in names for target in chosen):
                continue
            return capture
        return None

    def plan_targets(self, project, capture, targets):
        """Split targets into those to build and those whose identical package already exists."""
        definition = project.get("packaging") or {}
        if definition.get("runner") != "operations":
            return [(target, None) for target in targets], []
        project = self.packager.bind_inputs(project, definition)
        build, skipped = [], []
        for target in targets:
            key = self.packager.build_key(project, definition, capture, target)
            existing = self.packager.same_package(project, capture["version"], target, key)
            if existing:
                skipped.append({"target": target, "artifact_id": existing["id"], "filename": existing.get("filename"),
                                "built_at": existing.get("created_at"), "in_romm": (existing.get("romm") or {}).get("status") == "in_romm"})
            else:
                build.append((target, key))
        return build, skipped

    def queue_jobs(self, project, capture, planned, publish=False):
        return [self.store.update("jobs", self.job(project, target, capture["version"])["id"], capture_id=capture["id"],
                                  build_key=key, publish_after=publish) for target, key in planned]

    @staticmethod
    def skipped_message(capture, skipped):
        if not skipped:
            return None
        dates = sorted({(item.get("built_at") or "")[:10] for item in skipped})
        return (f"Nothing changed since the {', '.join(d for d in dates if d) or 'earlier'} build of {capture['version']} for "
                + ", ".join(item["target"] for item in skipped) + ", so it wasn't built again.")

    def publish_existing(self, skipped, log=None):
        for item in skipped:
            if item.get("in_romm"):
                continue
            try:
                self.publisher.queue(item["artifact_id"])
                if log:
                    log(f"Sending the existing {item['filename']} to RomM")
            except Invalid as error:
                if log:
                    log(f"Could not send {item['filename']} to RomM: {error}")

    def build(self, project_id, targets=None, capture_id=None, publish=False):
        project = self.store.get("projects", project_id)
        if project["recipe"]["mode"] == "capture":
            raise Invalid("This recipe preserves source only. Configure an assembly or Docker recipe first.")
        chosen = project["targets"] if targets is None else targets
        if not isinstance(chosen, list) or not chosen or any(t not in project["targets"] for t in chosen) or len(set(chosen)) != len(chosen):
            raise Invalid("Build targets must be a non-empty subset of this project's targets")
        compatible = self.compatible_capture(project, targets) if project.get("profile") and capture_id is None else None
        selected_id = capture_id or (compatible["id"] if compatible else project.get("capture_id"))
        if not selected_id:
            raise Invalid("Capture an upstream release before building")
        capture = self.store.get("captures", selected_id)
        if capture["project_id"] != project_id:
            raise Invalid("Capture belongs to another project")
        if not capture.get("complete", True):
            raise Invalid("Source was preserved, but release asset capture is incomplete. Check again before building.")
        if project.get("profile"):
            from .profiles import ensure_release
            spec = ensure_release(project["profile"], capture["version"], capture["commit"])
            names = {asset["name"] for asset in capture.get("assets", [])}
            if any(spec["runtimes"][target]["name"] not in names for target in chosen):
                raise Invalid("Capture the verified release again to preserve runtimes for all selected targets")
        definition = project.get("packaging")
        if definition and definition["runner"] == "operations":
            from . import packaging
            support = packaging.requirements(definition)
            if not support["supported"]:
                raise Invalid(support["reason"])
            review = packaging.evaluate_release(definition, capture, self.input_hashes(project), targets=chosen)
            self.store.update("captures", capture["id"], review={**review, "checked_at": now()})
            if review["status"] == "needs_review":
                raise Invalid(f"{capture['version']} needs review before it can be packaged: " + "; ".join(review["problems"]))
        planned, skipped = self.plan_targets(project, capture, chosen)
        message = self.skipped_message(capture, skipped)
        if publish:
            self.publish_existing(skipped)
        if not planned:
            return {"jobs": [], "skipped": skipped, "message": message}
        self.claim(project_id, expected_project=project)
        jobs = self.queue_jobs(project, capture, planned, publish)
        self.executor.submit(self._run_group, project, capture, jobs)
        return {"jobs": jobs, "skipped": skipped, "message": message}

    def _run_group(self, project, capture, jobs):
        try:
            self._build_group(project, capture, jobs)
        finally:
            self.release_claim(project["id"])

    def _build_group(self, project, capture, jobs):
        self.store.update("projects", project["id"], status="building", last_error=None)
        errors = []

        def finish(job, artifact, error):
            if artifact and not error:
                if self.store.get("jobs", job["id"])["status"] != "ready":
                    self.store.update("jobs", job["id"], status="ready", artifact_id=artifact["id"], finished_at=now())
                    self.stage(job["id"], "complete", "Package ready; launch has not been tested")
                self.notify(project["id"], "ready", f"{project['name']} {capture['version']} is packaged for {job['target']}", artifactid=artifact["id"])
                if job.get("publication_override") is not None:
                    if job["publication_override"]:
                        self.auto_publish({**project, "auto_publish_destination": "romm"}, artifact, job)
                elif job.get("publish_after") and not project.get("auto_publish"):
                    self.auto_publish({**project, "auto_publish_destination": "romm"}, artifact, job)
                elif project.get("auto_publish") or job.get("publish_after"):
                    self.auto_publish(project, artifact, job)
                return
            self.store.append_log(job["id"], str(error))
            self.store.update("jobs", job["id"], status="failed", error=str(error), finished_at=now())
            errors.append(f"{job['target']}: {error}")
            missing = "ROM" in str(error) or "Missing game file" in str(error) or "does not match an accepted" in str(error)
            self.notify(project["id"], "needs_input" if missing else "build_failed", str(error), jobid=job["id"],
                        event_key=f"build:{project['id']}:{capture['id']}:{job['target']}:{str(error)[:150]}")

        definition = project.get("packaging")
        if definition and definition["runner"] == "operations":
            self.packager.build_group(project, definition, capture, jobs,
                                      lambda jid: (lambda stage, message: self.stage(jid, stage, message)),
                                      lambda jid: (lambda message: self.store.append_log(jid, message)), finish)
        else:
            for job in jobs:
                try:
                    artifact = self._build_one(project, capture, job)
                except Exception as error:
                    finish(job, None, error)
                else:
                    finish(job, artifact, None)
        self.store.update("projects", project["id"], status="needs_attention" if errors else "ready", last_error="; ".join(errors) if errors else None)

    def auto_publish(self, project, artifact, job):
        log = lambda message: self.store.append_log(job["id"], message)
        if project.get("auto_publish_destination", "romm") == "library":
            self.publish(artifact["id"])
            log("Automatically copied the validated package to the local library folder (this is not a RomM upload)")
            return
        try:
            result = self.publisher.publish(artifact["id"], log)
            log("Automatically published to RomM" + (f" (entry {result['rom_id']})" if result.get("rom_id") else "; waiting for RomM to index it"))
        except Exception as error:
            log("Automatic RomM publication failed; the package stays archived: " + str(error))
            self.notify(project["id"], "build_failed", f"Automatic RomM publication failed for {artifact['filename']}: {error}",
                        artifactid=artifact["id"], event_key="autopublish:" + artifact["id"])

    def _payload(self, recipe, target, capture, destination):
        payload = recipe.get("payload", {"kind": "source"})
        if payload["kind"] == "source":
            entry = capture["source"]
            archive = Path(capture["root"]) / entry["file"]
            if checksum(archive) != entry["sha256"]:
                raise Invalid("Archived source checksum changed")
            unpack = destination.parent / "unpacked"
            safe_extract(archive, unpack)
            children = list(unpack.iterdir())
            root = children[0] if len(children) == 1 and children[0].is_dir() else unpack
            shutil.copytree(root, destination, dirs_exist_ok=True)
        else:
            pattern = payload.get("asset_patterns", {}).get(target)
            if not pattern:
                raise Invalid(f"No release asset pattern configured for {target}")
            matches = [entry for entry in capture["assets"] if re.search(pattern, entry["name"])]
            if len(matches) != 1:
                raise Invalid(f"Asset pattern for {target} must match exactly one captured file; matched {len(matches)}. Check again after editing patterns.")
            entry = matches[0]
            archive = Path(capture["root"]) / entry["file"]
            if checksum(archive) != entry["sha256"]:
                raise Invalid("Archived release asset checksum changed")
            if zipfile.is_zipfile(archive) or tarfile.is_tarfile(archive):
                # Releases keep their directory layout; recipe paths refer to that layout.
                safe_extract(archive, destination)
            else:
                # A single-file release such as an AppImage is packaged as-is, under a stable
                # name when the recipe sets one so expected_files survive version bumps.
                output = confined(destination, payload.get("single_file_name") or entry["name"])
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(archive, output)
                output.chmod(0o755)
        return entry["sha256"]

    def _build_one(self, project, capture, job):
        recipe = copy.deepcopy(target_recipe(project["recipe"], job["target"]))
        validate_recipe(recipe)
        jid = job["id"]
        log = lambda message: self.store.append_log(jid, message)
        self.store.update("jobs", jid, status="building")
        workspace = self.work_root / jid
        workspace.mkdir()
        try:
            if recipe["mode"] == "profile":
                return self._profile_build(project, capture, job, workspace)
            payload_dir = workspace / "payload"
            payload_dir.mkdir()
            log(f"Preparing {job['target']} payload for {capture['version']}")
            self.stage(jid, "preparing", "Preparing archived files and verified inputs")
            payload_hash = self._payload(recipe, job["target"], capture, payload_dir)
            package = payload_dir if recipe["mode"] == "assemble" else workspace / recipe.get("output_dir", "out")
            input_destination = package if recipe["mode"] == "assemble" else workspace / "inputs"
            input_hashes = []
            for item in recipe.get("inputs", []):
                source = confined(self.input_root, item["path"], must_exist=True)
                if source.stat().st_size > MAX_FILE:
                    raise Invalid("Input exceeds 1 GiB limit")
                output = confined(input_destination, item["destination"])
                if output.exists():
                    raise Invalid(f"Input destination already exists in payload: {item['destination']}")
                output.parent.mkdir(parents=True, exist_ok=True)
                # Hash the copied bytes; changes during copying cannot pass unnoticed.
                shutil.copyfile(source, output)
                digest = checksum(output)
                if digest.lower() != item["sha256"].lower():
                    raise Invalid(f"Input SHA-256 mismatch: {item['path']}")
                input_hashes.append({"path": item["path"], "destination": item["destination"], "sha256": digest})
                log(f"Verified supplied input: {item['path']}")
            if recipe["mode"] == "docker":
                self._docker(recipe, workspace, jid, log)
                # A container can create links in /work. Validate every output path component
                # before the host packager reads it, including parents of output_dir.
                package = confined(workspace, recipe.get("output_dir", "out"))
            if not package.is_dir() or package.is_symlink():
                raise Invalid("Recipe did not produce an output directory")
            if recipe.get("portable"):
                from .portable import prepare_dusklight
                prepare_dusklight(package, job["target"], recipe["portable"]["disc"])
                log("Prepared portable launcher with bundled disc and local user data")
            verify_tree(package)
            self.stage(jid, "validating", "Validating expected package files")
            for name in recipe["expected_files"]:
                confined(package, name, must_exist=True)
            log("Expected files and output safety checks passed. Game launch has not been tested.")
            aid = identifier()
            recipe_hash = hashlib.sha256(json.dumps(recipe, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
            manifest = {"schema": 1, "project": project["name"], "repo": project["repo"], "version": capture["version"],
                        "target": job["target"], "upstream_commit": capture["commit"], "capture_id": capture["id"],
                        "recipe_sha256": recipe_hash, "recipe": recipe, "payload_sha256": payload_hash,
                        "inputs": input_hashes, "created_at": now(), "demo": project.get("demo", False),
                        "validation": "Expected files present; no launch or gameplay verification"}
            manifest_path = package / "portkeeper-manifest.json"
            if manifest_path.exists():
                raise Invalid("Payload must not already contain portkeeper-manifest.json")
            manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            root = self.archive_root / "packages" / project["id"] / aid
            root.mkdir(parents=True)
            filename = f"{slug(project['name'])}-{slug(capture['version'])}-{job['target']}.zip"
            archive = root / filename
            temp = root / (filename + ".part")
            with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
                for path in sorted(package.rglob("*")):
                    if path.is_file():
                        bundle.write(path, path.relative_to(package).as_posix())
            os.replace(temp, archive)
            artifact = {"id": aid, "project_id": project["id"], "project_name": project["name"],
                        "target": job["target"], "version": capture["version"], "created_at": now(),
                        "size": archive.stat().st_size, "sha256": checksum(archive), "published": False,
                        "file": str(archive), "filename": filename, "manifest": manifest,
                        "download_url": f"/api/artifacts/{aid}/download", "demo": project.get("demo", False),
                        "validation_level": "packaged", "pinned": False}
            (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            self.store.put("artifacts", artifact)
            log(f"Archived package: {filename}")
            self.store.update("jobs", jid, status="ready", artifact_id=aid, finished_at=now())
            self.stage(jid, "complete", "Package ready; launch has not been tested")
            return artifact
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def _docker(self, recipe, workspace, jid, log):
        if not self.enable_docker or not self.docker_available:
            raise Invalid(self.docker_error)
        container_name = "portkeeper-" + jid
        args = ["docker", "run", "--rm", "--name", container_name, "--read-only", "--cap-drop=ALL",
                "--security-opt=no-new-privileges", "--pids-limit=256", "--memory=2g", "--cpus=2",
                "--user", f"{os.getuid()}:{os.getgid()}", "--network", "bridge" if recipe.get("network") else "none",
                "--tmpfs", "/tmp:rw,nosuid,nodev,size=256m", "--volume", f"{workspace}:/work:Z",
                "--workdir", "/work/payload", "--entrypoint", recipe["command"][0], recipe["image"], *recipe["command"][1:]]
        log(f"Running pinned Docker image {recipe['image']}; network {'enabled' if recipe.get('network') else 'disabled'}")
        log_path = workspace / "runner.log"
        try:
            with log_path.open("w") as output:
                process = subprocess.Popen(args, stdout=output, stderr=subprocess.STDOUT)
                started = time.monotonic()
                while process.poll() is None:
                    if time.monotonic() - started > 1800 or self.stop.is_set() or log_path.stat().st_size > 8 * 1024 * 1024:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
                        raise Invalid("Docker job exceeded time/log limit or service stopped")
                    time.sleep(0.2)
            with log_path.open(errors="replace") as stream:
                lines = stream.read(8 * 1024 * 1024).splitlines()
            for line in lines[-150:]:
                log(line)
            if process.returncode != 0:
                raise Invalid(f"Docker build exited with code {process.returncode}")
        finally:
            try:
                subprocess.run(["docker", "rm", "-f", container_name], capture_output=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def _profile_build(self, project, capture, job, workspace):
        jid = job["id"]
        package, provenance = self.shipwright.build(project, capture, job, workspace,
                              lambda stage, message: self.stage(jid, stage, message))
        self.stage(jid, "validating", "Checking all package files and archive integrity")
        verify_tree(package)
        manifest = json.loads((package / "portkeeper-manifest.json").read_text())
        manifest.update(capture_id=capture["id"], profile=project["profile"], pipeline=provenance)
        (package / "portkeeper-manifest.json").write_text(json.dumps(manifest, indent=2))
        aid = identifier()
        root = self.archive_root / "packages" / project["id"] / aid
        root.mkdir(parents=True)
        filename = f"{slug(project['name'])}-{slug(capture['version'])}-{job['target']}.zip"
        archive = root / filename
        temp = archive.with_suffix(".zip.part")
        try:
            self.shipwright.helper.write_zip(package, temp)
            os.replace(temp, archive)
        except Exception:
            shutil.rmtree(root)
            raise
        artifact = {"id": aid, "project_id": project["id"], "project_name": project["name"],
                    "target": job["target"], "version": capture["version"], "created_at": now(),
                    "size": archive.stat().st_size, "sha256": checksum(archive), "published": False,
                    "file": str(archive), "filename": filename, "manifest": manifest,
                    "validation_level": "packaged", "pinned": False, "demo": False,
                    "download_url": f"/api/artifacts/{aid}/download"}
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
        self.store.put("artifacts", artifact)
        self.store.update("jobs", jid, status="ready", artifact_id=aid, finished_at=now())
        self.stage(jid, "complete", "Package ready; gameplay confirmation is separate")
        return artifact

    def preservation_status(self, project_id):
        project = self.store.get("projects", project_id)
        materials = self.shipwright.materials() if project.get("profile") == "shipwright" else {}
        exports = [p for p in self.store.all("preservations") if p["project_id"] == project_id]
        toolchain = self.get_setting("toolchain", {})
        toolchain_valid, toolchain_error = False, None
        if toolchain.get("material"):
            try:
                path = confined(self.data / "toolchains", toolchain["material"], must_exist=True)
                if checksum(path) != toolchain.get("sha256"):
                    raise Invalid("Registered toolchain checksum changed; register the archive again before exporting")
                toolchain_valid = True
            except (Invalid, OSError) as error:
                toolchain_error = str(error)
        selected_capture = self.compatible_capture(project) if project.get("profile") else None
        selected_id = selected_capture["id"] if selected_capture else project.get("capture_id")
        from .profiles import profile
        expected_scope = profile(project["profile"])["preservation_scope"] if project.get("profile") else "complete_source_declarations"
        verified = next((record for record in exports
                         if record.get("capture_id") == selected_id
                         and record.get("manifest", {}).get("closure", {}).get("offline_rebuild_complete") is True
                         and record["manifest"]["closure"].get("scope") == expected_scope
                         and record["manifest"].get("toolchain", {}).get("identity") == toolchain.get("identity")
                         and any(item.get("sha256") == toolchain.get("sha256")
                                 and item.get("path", "").startswith("materials/toolchain/")
                                 for item in record["manifest"].get("files", []))), None) if toolchain_valid else None
        return {"project_id": project_id, "source_archived": bool(project.get("capture_id")),
                "materials": list(materials), "toolchain_archived": toolchain_valid,
                "toolchain_error": toolchain_error,
                "offline_rebuild_verified": bool(verified), "exports": exports,
                "note": ("An archived bundle records a successful offline rebuild for " + verified["manifest"]["closure"]["scope"] + ". User-owned game inputs remain required."
                         if verified else "Dependency sources are preserved for verified profiles. Archive a toolchain and verify an offline rebuild before claiming completeness.")}

    def export_preservation(self, project_id):
        from .shipwright import DEPENDENCIES
        project = self.store.get("projects", project_id)
        materials, required, scope = {}, [], None
        compatible = self.compatible_capture(project) if project.get("profile") else None
        if project.get("profile") == "shipwright" and compatible:
            from .profiles import profile
            materials = self.shipwright.materials()
            required = list(DEPENDENCIES) + ["extractor-patch"]
            # Source declarations outside the headless extraction profile remain visible.
            labels = ["submodule:torch", "submodule:libultraship", "dependency:spdlog", "dependency:yaml-cpp", "dependency:tinyxml2", "dependency:zlib"]
            scope = {"name": profile(project["profile"])["preservation_scope"], "labels": labels}
            for name in DEPENDENCIES:
                if name in materials:
                    alias = "submodule:" + name if name in ("torch", "libultraship") else "dependency:" + name
                    materials[alias] = materials.pop(name)
                    required[required.index(name)] = alias
        toolchain = self.get_setting("toolchain", {})
        if toolchain.get("material"):
            path = confined(self.data / "toolchains", toolchain["material"], must_exist=True)
            if checksum(path) != toolchain.get("sha256"):
                raise Invalid("Registered toolchain checksum changed; register the archive again before exporting")
            materials["toolchain"] = {"path": str(path), "revision": toolchain.get("identity")}
            toolchain = {"material": "toolchain", "identity": toolchain.get("identity")}
        else:
            toolchain = None
        return self.preservation.export(project_id, materials=materials, required_materials=required,
                                        toolchain=toolchain, dependency_scope=scope,
                                        capture_id=compatible["id"] if compatible else None)

    # Guided setup -------------------------------------------------------------
    def catalog_games(self):
        """Supported games for Add a game, with what each needs and whether it is already set up."""
        projects = self.store.all("projects")
        games = []
        for definition in self.catalog.definitions().values():
            if definition["runner"] not in ("operations", "profile"):
                continue
            existing = next((p for p in projects if (p.get("packaging") or {}).get("id") == definition["id"]), None)
            first_run = definition.get("first_run")
            games.append({"id": definition["id"], "name": definition["name"], "game": definition.get("game"),
                          "summary": definition.get("summary"), "repository": definition["upstream"]["repository"],
                          "targets": definition["targets"], "runner": definition["runner"], "profile": definition.get("profile"),
                          "needs": [{"label": i["label"], "required": i.get("required", True), "platform": i.get("platform_hint")}
                                    for i in definition.get("inputs", []) if i.get("required", True)],
                          "first_run": {"mode": first_run["mode"], "text": first_run.get("summary")} if first_run else None,
                          "limitations": definition.get("limitations", []),
                          "in_library": existing["id"] if existing else None})
        return {"games": sorted(games, key=lambda g: g["name"].casefold()), "romm_connected": self.romm.public_config().get("configured", False)}

    def catalog_definition(self, definition_id):
        definition = self.catalog.definitions().get(definition_id)
        if not definition:
            raise Invalid("That game is not in the catalog")
        return definition

    def find_game_files(self, definition_id):
        definition = self.catalog_definition(definition_id)
        if definition["runner"] != "operations":
            raise Invalid("This game is set up with its own guided steps")
        return {"inputs": self.packager.find_files(definition), "platform_root": str(self.input_root)}

    def add_from_catalog(self, payload):
        """Create a game from a catalog definition with the chosen files, then set it up."""
        if not isinstance(payload, dict):
            raise Invalid("Choose a game to add")
        definition = self.catalog_definition(payload.get("definition_id"))
        if definition["runner"] != "operations":
            raise Invalid("This game is set up with its own guided steps")
        if any((p.get("packaging") or {}).get("id") == definition["id"] for p in self.store.all("projects")):
            raise Invalid(f"{definition['name']} is already in your library")
        targets = payload.get("targets") or definition["targets"]
        keep = bool(payload.get("keep_updated", True))
        romm = bool(self.romm.public_config().get("configured"))
        project = self.create_project({"name": (payload.get("name") or definition["name"]).strip(), "repo": definition["upstream"]["repository"],
                                       "targets": targets, "poll_minutes": 60, "auto_build": keep, "auto_publish": keep and romm and bool(payload.get("auto_publish", True)),
                                       "auto_publish_destination": "romm", "packaging": json.loads(json.dumps(definition))})
        files = payload.get("files") or {}
        if not isinstance(files, dict):
            raise Invalid("Game files must map each needed file to a path")
        for input_id, path in files.items():
            if path:
                self.bind_input(project["id"], {"input": input_id, "path": path, "source": "inputs"})
        self.store.update("projects", project["id"], definition_source={"kind": "catalog", "id": definition["id"], "revision": definition["revision"], "added_at": now()})
        publish = bool(payload.get("publish", True)) and romm
        result = self.setup(project["id"], publish=publish, publication_override=publish)
        return {"project": self.store.get("projects", project["id"]), **result}

    def github_lookup(self, repo):
        if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo.strip()):
            raise Invalid("Use owner/repository, for example Zelda64Recomp/Zelda64Recomp")
        repo = repo.strip()
        release = self.github.release(repo)
        names = [a.get("name") for a in release.get("assets", []) if isinstance(a.get("name"), str)][:60]
        from . import platforms
        known = next((d for d in self.catalog.definitions().values() if d["upstream"]["repository"].lower() == repo.lower()), None)
        documents, read_error = platforms.read_documents(self.github, repo, release.get("commit"))
        detection = platforms.detect(release.get("assets", []), documents, (known or {}).get("targets", []))
        if read_error:
            detection["documentation_error"] = read_error
        return {"repository": repo, "version": release["version"], "assets": names,
                "platforms": detection["suggested_targets"], "platform_detection": detection,
                "release_url": release["url"], "catalog_id": known["id"] if known else None}

    # Drafting packaging with a model -------------------------------------------
    def _draft_material(self, project, budget, preserve_evidence=False):
        from . import drafting
        if not project.get("capture_id"):
            raise Invalid("Check upstream first, so Portkeeper has a release to read")
        capture = self.store.get("captures", project["capture_id"])
        capture = copy.deepcopy(capture)
        release = self.github.release_tag(project["repo"], capture["version"])
        if preserve_evidence and release.get('commit') != capture['commit']:
            raise Invalid('The release now points to different source; check upstream before investigating')
        assets = [a for a in release.get("assets", []) if isinstance(a.get("name"), str)]
        listings = {}
        archive_checksums = {}
        runtime_documents = []
        work = self.work_root / ("draft-" + identifier())
        work.mkdir(parents=True)
        try:
            for asset in drafting.platform_assets(assets, project['targets']):
                if (asset.get("size") or 0) > 400 * 1024 * 1024:
                    listings[asset["name"]] = ["(too large to list)"]
                    continue
                target = work / "asset"
                try:
                    existing = next((item for item in capture.get('assets', []) if item['name'] == asset['name']), None)
                    if preserve_evidence and existing:
                        path = confined(capture['root'], existing['file'], must_exist=True)
                        if checksum(path) != existing['sha256']:
                            raise Invalid('Preserved runtime changed; check upstream again')
                        shutil.copyfile(path, target)
                        saved = {'sha256': existing['sha256'], 'size': path.stat().st_size}
                    else:
                        saved = self.github.download(asset["browser_download_url"], target)
                    archive_checksums[asset['name']] = saved['sha256']
                    listings[asset["name"]] = drafting.archive_listing(target, limit=30_001)
                    runtime_documents.extend(drafting.archive_documents(target, asset['name'], listings[asset['name']]))
                    if preserve_evidence and not existing:
                        filename = 'investigation-' + saved['sha256'] + '-' + slug(asset['name'])
                        path = confined(capture['root'], filename)
                        temporary = path.with_suffix('.partial')
                        try:
                            shutil.copyfile(target, temporary)
                            temporary.replace(path)
                        finally:
                            temporary.unlink(missing_ok=True)
                        capture['assets'].append({'name': asset['name'], 'file': filename,
                                                  'url': asset['browser_download_url'], **saved})
                except Invalid as error:
                    listings[asset["name"]] = [f"(could not list: {error})"]
                finally:
                    target.unlink(missing_ok=True)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        context = drafting.build_context(project, capture, assets, listings, self.catalog.definitions(), budget)
        context['material']['runtime_documentation'] = runtime_documents
        context['release']['archive_sha256'] = archive_checksums
        context['release']['source_sha256'] = capture['source']['sha256']
        return capture, context

    def draft_prompt(self, project_id, instruction="", prepare_game_files=True):
        """The prompt for the person's own chat, and a workspace folder for their own agent."""
        from . import drafting
        project = self.store.get("projects", project_id)
        if not isinstance(prepare_game_files, bool):
            raise Invalid('Choose whether game files should be prepared during packaging')
        _, context = self._draft_material(project, 20_000)
        context['game_file_preparation']['enabled'] = prepare_game_files
        return {"prompt": drafting.prompt_text(context, self.catalog.definitions(), instruction),
                "context": {key: value for key, value in context.items() if key != "archive_evidence"}}

    def draft_workspace(self, project_id, instruction="", prepare_game_files=True):
        from . import drafting
        result = self.draft_prompt(project_id, instruction, prepare_game_files=prepare_game_files)
        return drafting.workspace_zip(result["prompt"], result["context"], self.catalog.definitions())

    def _draft_feedback(self, project, feedback):
        """Keep trial diagnostics useful without sending private input paths or keys."""
        if feedback is None:
            return None
        from .assistant import redact_text
        replacements = [(str(root), '[private-workspace]') for root in
                        (self.data, self.work_root, self.input_root, self.library_root) if root]
        for input_id, binding in (project.get('input_bindings') or {}).items():
            if isinstance(binding, dict) and isinstance(binding.get('path'), str) and binding['path']:
                path = binding['path']
                replacements += [(path, '[private-input:' + input_id + ']'),
                                 (Path(path).name, '[private-input:' + input_id + ']')]
        for filename in ('assistant.json', 'romm.json'):
            path = self.data / 'secrets' / filename
            if path.is_file() and not path.is_symlink():
                try:
                    values = json.loads(path.read_text())
                    replacements += [(value, '[redacted]') for key, value in values.items()
                                     if key in ('api_key', 'token', 'password', 'secret') and isinstance(value, str) and len(value) > 4]
                except (OSError, ValueError, AttributeError):
                    pass
        text = str(feedback)
        for original, replacement in sorted(set(replacements), key=lambda item: len(item[0]), reverse=True):
            text = text.replace(original, replacement)
        return redact_text(text)

    def queue_investigation(self, project_id, instruction='', max_calls=3, prepare_game_files=True):
        with self.lock:
            project = self.store.get('projects', project_id)
            if not self.assistant.connected():
                raise Invalid('Connect a model before investigating')
            if isinstance(max_calls, bool) or not isinstance(max_calls, int) or not 1 <= max_calls <= 6:
                raise Invalid('Investigation limit must be 1–6 model requests')
            if not isinstance(instruction, str) or len(instruction) > 4000 or not isinstance(prepare_game_files, bool):
                raise Invalid('Use a short investigation instruction and a boolean preparation choice')
            if not project.get('capture_id'):
                raise Invalid('Check upstream first so there is a preserved release to investigate')
            if any(job['project_id'] == project_id and job.get('kind') in ('draft', 'investigation') and job['status'] in ('queued', 'running') for job in self.store.all('jobs')):
                raise Invalid('Wait for the current investigation or draft to finish')
            job = self.job(project, 'capture', project.get('latest_version'))
            job = self.store.update('jobs', job['id'], kind='investigation', stage='investigating', status='running', instruction=instruction,
                                    investigation_max_calls=max_calls, prepare_game_files=prepare_game_files)
            self.executor.submit(self._investigate, project, job['id'])
            return {'job': job}

    def _investigate(self, project, job_id):
        from . import investigation
        log = lambda message: self.store.append_log(job_id, message)
        record = None
        try:
            job = self.store.get('jobs', job_id)
            self.stage(job_id, 'investigating', 'Reading the preserved release and available runner capabilities')
            capture, context = self._draft_material(project, 20_000, preserve_evidence=True)
            context['game_file_preparation']['enabled'] = job['prepare_game_files']
            config = self.assistant._config()
            public = self.assistant.public_config(config)
            record = {'schema': 1, 'job_id': job_id, 'project_id': project['id'], 'kind': 'investigation', 'created_at': now(),
                      'capture_id': capture['id'], 'source_sha256': capture['source']['sha256'], 'attempts': [], 'status': 'investigating',
                      'research_capture': capture,
                      'input_inventory': investigation.input_inventory(self, project), 'model': {'provider': public['provider'], 'drafting_model': public['drafting_model']}}
            self.save_draft_record(job_id, record)
            self.store.update('jobs', job_id, draft_record_available=True)
            def remember(event):
                record['attempts'].append({**event, 'at': now()})
                self.save_draft_record(job_id, record)
            plan, evidence, context = investigation.run(self, project, capture, context, public | config, job['investigation_max_calls'], log, remember, job['instruction'])
            record.update(status='ready', finished_at=now(), plan=plan, evidence=evidence,
                          draft_context={key: value for key, value in context.items() if key != 'archive_evidence'},
                          archive_evidence=context.get('archive_evidence', {}), archive_layouts=context['release'].get('archive_layouts', {}))
            self.save_draft_record(job_id, record)
            for older in self.store.all('proposals'):
                if older.get('kind') == 'investigation' and older['project_id'] == project['id'] and older['status'] == 'pending_review':
                    self.store.update('proposals', older['id'], status='superseded')
            report = self.store.put('proposals', {'id': identifier(), 'kind': 'investigation', 'project_id': project['id'], 'status': 'pending_review',
                'created_at': now(), 'job_id': job_id, 'capture_id': capture['id'], 'source_sha256': capture['source']['sha256'],
                'base_definition_sha256': hashlib.sha256(json.dumps(project.get('packaging'), sort_keys=True).encode()).hexdigest(),
                'targets': project['targets'], 'prepare_game_files': job['prepare_game_files'], 'plan': plan, 'source': public['drafting_model']})
            self.store.update('jobs', job_id, status='ready', finished_at=now(), investigation_id=report['id'], outcome=plan['outcome'])
            self.stage(job_id, 'complete', 'Investigation complete: ' + plan['outcome'] + '. ' + plan['summary'])
        except Exception as error:
            if record is not None:
                record.update(status='failed', error=str(error), finished_at=now())
                self.save_draft_record(job_id, record)
            log(str(error))
            self.store.update('jobs', job_id, status='failed', error=str(error), finished_at=now())

    def reviewed_investigation(self, project, investigation_id):
        if not isinstance(investigation_id, str) or not re.fullmatch(r'[a-f0-9]{32}', investigation_id):
            raise Invalid('Choose a saved investigation plan')
        report = self.store.get('proposals', investigation_id)
        if report.get('kind') != 'investigation' or report['project_id'] != project['id'] or report['status'] != 'pending_review':
            raise Invalid('This investigation is not available for the selected game')
        if not report['plan'].get('can_draft'):
            raise Invalid('The investigation has not established a supported packaging workflow')
        capture = self.store.get('captures', project.get('capture_id'))
        current = hashlib.sha256(json.dumps(project.get('packaging'), sort_keys=True).encode()).hexdigest()
        if report['capture_id'] != capture['id'] or report['source_sha256'] != capture['source']['sha256'] or report['targets'] != project['targets'] or report['base_definition_sha256'] != current:
            raise Invalid('Release, selected platforms or build instructions changed; investigate again before drafting')
        return report

    def queue_draft(self, project_id, instruction="", targets=None, prepare_game_files=True, review_model=False, max_calls=None, investigation_id=None, test_inputs=False):
        with self.lock:
            project = self.store.get("projects", project_id)
            if not self.assistant.connected():
                raise Invalid("No model is connected. Use the prepared prompt with your own chat, or connect a model in Settings.")
            if not isinstance(review_model, bool):
                raise Invalid('Choose whether a separate model review should run')
            if not isinstance(test_inputs, bool):
                raise Invalid('Choose whether selected game files may be used in guided draft tests')
            if max_calls is not None and (isinstance(max_calls, bool) or not isinstance(max_calls, int) or not 1 <= max_calls <= 3):
                raise Invalid('Draft correction limit must be an integer from 1 to 3 model calls')
            if not isinstance(instruction, str) or len(instruction) > 4000:
                raise Invalid("Keep the extra instructions under 4000 characters")
            if not isinstance(prepare_game_files, bool):
                raise Invalid('Choose whether game files should be prepared during packaging')
            if investigation_id is not None:
                plan = self.reviewed_investigation(project, investigation_id)
                if plan['prepare_game_files'] != prepare_game_files:
                    raise Invalid('The game preparation choice changed; investigate again for that choice')
            if targets is not None and (not isinstance(targets, list) or not targets or any(t not in TARGETS for t in targets) or len(set(targets)) != len(targets) or not set(project["targets"]).issubset(targets)):
                raise Invalid("Draft platforms must include current platforms and supported target IDs")
            job = self.job(project, "capture", project.get("latest_version"))
            job = self.store.update("jobs", job["id"], kind="draft", stage="drafting", status="running", requested_targets=targets, instruction=instruction, prepare_game_files=prepare_game_files, review_model=review_model)
            if max_calls is not None:
                job = self.store.update('jobs', job['id'], draft_max_calls=max_calls)
            if investigation_id is not None:
                job = self.store.update('jobs', job['id'], investigation_id=investigation_id, test_inputs=test_inputs)
            self.executor.submit(self._draft, project_id, job["id"], instruction)
            return {"job": job}

    def _draft(self, project_id, job_id, instruction):
        from . import drafting
        log = lambda message: self.store.append_log(job_id, message)
        record = None
        try:
            project = self.store.get("projects", project_id)
            original_targets = list(project["targets"])
            requested_targets = self.store.get("jobs", job_id).get("requested_targets")
            if requested_targets:
                project = {**project, "targets": requested_targets}
            model_config = self.assistant._config()
            config = self.assistant.public_config(model_config)
            # JSON paths and source snippets tokenize more densely than prose.
            # Keep room for both reasoning and the final definition/corrections.
            budget = max(8000, (config["context_tokens"] - 8000) * 2) if config["provider"] == "ollama" else 20_000
            self.stage(job_id, "drafting", "Reading the release, README and source")
            investigated = None
            investigation_id = self.store.get('jobs', job_id).get('investigation_id')
            if investigation_id:
                investigated = self.reviewed_investigation(project, investigation_id)
                report = self.draft_record(investigated['job_id'])
                capture = copy.deepcopy(report['research_capture'])
                context = copy.deepcopy(report['draft_context'])
                context['investigation_plan'] = investigated['plan']
                context['investigation_evidence'] = report.get('evidence', [])
                context['input_inventory'] = report.get('input_inventory', {})
                # Complete manifests remain server-side for deterministic checks.
                for asset, members in report.get('archive_evidence', {}).items():
                    context.setdefault('archive_evidence', {})[asset] = members
                context['release'].setdefault('archive_layouts', {}).update(report.get('archive_layouts', {}))
            else:
                capture, context = self._draft_material(project, budget)
            context.setdefault('game_file_preparation', {})['enabled'] = self.store.get('jobs', job_id).get('prepare_game_files', True)
            system, user = drafting.prompt_parts(context, self.catalog.definitions(), instruction)
            starter = drafting.starter_definition(context)
            record = {'schema': 1, 'job_id': job_id, 'project_id': project_id, 'created_at': now(),
                      'model': {key: config.get(key) for key in ('provider', 'drafting_model', 'context_tokens', 'billing')},
                      'temperature': 0.1 if config['provider'] == 'ollama' else None,
                      'starter': starter, 'context': context, 'draft_prompt': {'system': system, 'user': user},
                      'attempts': [], 'status': 'drafting', 'build_test': 'not_run', 'gameplay_test': 'not_run'}
            self.save_draft_record(job_id, record)
            self.store.update('jobs', job_id, draft_record_available=True)
            def remember(event):
                record['attempts'].append({**event, 'at': now()})
                self.save_draft_record(job_id, record)
            log(f"Prepared {len(user) // 1000} KB of project material for {config['drafting_model']}")
            self.stage(job_id, "drafting", "Waiting for the model's draft")
            def read(text):
                checked = drafting.read_reply(text, project)
                if investigated:
                    from .investigation import check_definition
                    check_definition(checked[0], investigated['plan'], capture)
                drafting.check_preparation_choice(checked[0], context)
                drafting.check_archive_evidence(checked[0], context)
                invented = drafting.unsupported_hashes(checked[0], json.dumps([context.get('material', {}), context.get('investigation_evidence', [])]))
                if invented:
                    raise Invalid("These accepted hashes do not appear in the project material: " + ", ".join(invented)
                                  + ". Use only hashes quoted in the project material. File inputs can use verify \"downstream\" when a supported tool validates them; PS4 extraction still requires a sourced executable hash.")
                return checked
            trials = []
            def test(definition):
                self.stage(job_id, "drafting", "Test-building the draft (nothing is kept)")
                log("Test-building the draft against the preserved release; nothing is archived")
                try:
                    trial = self.model_tools.start_trial(project_id, definition, [t for t in definition["targets"] if t in project["targets"]] or None,
                                                         source=config["drafting_model"], background=False,
                                                         allow_selected_inputs=self.store.get('jobs', job_id).get('test_inputs', False) if investigated else True,
                                                         capture_id=capture['id'], capture_snapshot=capture if investigated else None)
                except Invalid as error:
                    log(f"Test build could not start: {error}")
                    return self._draft_feedback(project, f"Portkeeper could not test-build your definition: {error}")
                trials.append({"id": trial["id"], "status": trial["status"], "finished_at": trial.get("finished_at")})
                log(f"Test build {trial['status']}")
                self.stage(job_id, "drafting", "Waiting for the model's draft")
                return self._draft_feedback(project, self.model_tools.trial_feedback(trial))
            max_calls = self.store.get('jobs', job_id).get('draft_max_calls', self.get_setting('draft_max_calls', 1))
            log(f'Draft correction limit: {max_calls} model call(s); separate review is an independent choice')
            definition, explanation = self.assistant.draft_definition(system, user, read, log, attempts=max_calls, test_rounds=self.get_setting("draft_test_rounds", 1), record=remember, config=model_config,
                                                                      test=test if self.get_setting("draft_test_builds", True) else None)
            record['automatic_checks'] = {'schema': 'passed', 'release_assets': 'checked',
                                          'archive_inspections': context['release'].get('archive_layouts', {})}
            review = {'status': 'not_requested', 'summary': 'Automatic model review is off. Inspect the draft and deterministic checks before approval.', 'findings': [], 'note': 'No extra model call was made.'}
            if self.store.get('jobs', job_id).get('review_model') or self.get_setting('draft_model_review', False):
                self.stage(job_id, 'drafting', 'Reviewing first-launch instructions and source claims')
                log('Starting a fresh model review; this does not run a build or the game')
                review_system, review_user = drafting.review_prompt(definition, context)
                record['review_prompt'] = {'system': review_system, 'user': review_user}
                try:
                    review = self.assistant.review_definition(definition, context, record=remember, config=model_config)
                except Invalid as error:
                    review = {'status': 'unavailable', 'summary': str(error), 'findings': [],
                              'note': 'The draft passed automatic checks, but the model review could not be completed. Inspect it manually. Build and gameplay are untested.'}
                    log('Model review unavailable: ' + str(error))
            record.update(status='ready', finished_at=now(), model_review=review, accepted_definition=definition,
                          build_test=trials[-1]['status'] if trials else 'not_run')
            self.save_draft_record(job_id, record)
            details = {'model_review': review, 'starter': starter, 'automatic_checks': record['automatic_checks'], 'draft_job_id': job_id}
            if investigated:
                details.update(investigation_id=investigated['id'], investigation_plan=investigated['plan'])
            if trials:
                details['model_tests'] = trials[::-1]
            if requested_targets:
                details.update(requested_targets=requested_targets, base_targets=original_targets)
            proposal = self.save_definition_proposal(project, definition, explanation, f"{config['provider_label']} · {config['drafting_model']}", capture, draft_details=details)
            self.store.update("jobs", job_id, status="ready", finished_at=now(), proposal_id=proposal["id"])
            self.stage(job_id, "complete", "Draft ready for your review")
            self.notify(project_id, "ready", f"A packaging draft for {project['name']} is ready for your review", event_key="draft:" + proposal["id"])
        except Exception as error:
            if record is not None:
                record.update(status='failed', error=str(error), finished_at=now())
                try:
                    self.save_draft_record(job_id, record)
                except OSError:
                    log('Could not save the final drafting record')
            log(str(error))
            self.store.update("jobs", job_id, status="failed", error=str(error), finished_at=now())

    def save_draft_record(self, job_id, record):
        root = self.data / 'drafts'
        root.mkdir(parents=True, exist_ok=True)
        path = root / (job_id + '.json')
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(record, indent=2) + '\n')
        temporary.replace(path)

    def draft_record(self, job_id):
        job = self.store.get('jobs', job_id)
        if job.get('kind') not in ('draft', 'investigation') or not job.get('draft_record_available'):
            raise Invalid('This job has no saved drafting record')
        return json.loads(confined(self.data / 'drafts', job_id + '.json', must_exist=True).read_text())

    def import_draft(self, project_id, text):
        from . import drafting
        project = self.store.get("projects", project_id)
        definition, explanation = drafting.read_reply(text, project)
        capture = self.store.get("captures", project["capture_id"]) if project.get("capture_id") else None
        return self.save_definition_proposal(project, definition, explanation, "Your own chat or agent", capture)

    def save_definition_proposal(self, project, definition, explanation, source, capture=None, draft_details=None):
        from . import drafting, packaging
        definition["history"] = list(definition.get("history", [])) + [f"Drafted by {source} on {now()[:10]} for release {capture['version'] if capture else 'unknown'}; requires human review before use."]
        definition = packaging.validate_definition(definition)
        for older in self.store.all("proposals"):
            if older.get("project_id") == project["id"] and older.get("kind") == "definition" and older.get("status") == "pending_review":
                self.store.update("proposals", older["id"], status="superseded")
        proposal = {"id": identifier(), "project_id": project["id"], "kind": "definition", "created_at": now(), "status": "pending_review",
                    "definition": definition, "explanation": explanation, "source": source, "view": packaging.view(definition),
                    "warnings": drafting.warnings(definition), "requires_review": True, "executed": False,
                    "base_definition_sha256": hashlib.sha256(json.dumps(project.get("packaging"), sort_keys=True).encode()).hexdigest()}
        if draft_details:
            proposal.update(draft_details)
        return self.store.put("proposals", proposal)

    def apply_definition_proposal(self, proposal):
        project = self.store.get("projects", proposal["project_id"])
        if proposal.get('investigation_id'):
            self.reviewed_investigation(project, proposal['investigation_id'])
        if hashlib.sha256(json.dumps(project.get("packaging"), sort_keys=True).encode()).hexdigest() != proposal["base_definition_sha256"]:
            raise Invalid("The packaging changed after this draft; draft again before approving")
        if proposal.get("requested_targets") and project["targets"] != proposal.get("base_targets"):
            raise Invalid("The selected platforms changed after this draft; draft again before approving")
        targets = proposal.get("requested_targets") or [t for t in project["targets"] if t in proposal["definition"]["targets"]] or proposal["definition"]["targets"]
        updated = self.update_project(project["id"], {"packaging": proposal["definition"], "targets": targets})
        self.store.update("projects", project["id"], definition_source={"kind": "draft", "source": proposal["source"], "approved_at": now()})
        self.store.update("proposals", proposal["id"], status="applied", reviewed_at=now())
        return updated

    def discard_proposal(self, proposal_id):
        proposal = self.store.get("proposals", proposal_id)
        if proposal["status"] != "pending_review":
            raise Invalid("This draft was already reviewed")
        return self.store.update("proposals", proposal_id, status="discarded", reviewed_at=now())

    def recipe_proposal(self, project_id, instruction=""):
        project = self.store.get("projects", project_id)
        jobs = [j for j in self.store.all("jobs") if j["project_id"] == project_id]
        logs = jobs[0].get("logs", []) if jobs else []
        draft = self.assistant.draft(project, logs=logs, instruction=instruction)
        proposal = {**draft, "id": identifier(), "project_id": project_id, "created_at": now(),
                    "base_recipe_sha256": hashlib.sha256(json.dumps(project["recipe"], sort_keys=True).encode()).hexdigest(),
                    "status": "pending_review"}
        return self.store.put("proposals", proposal)

    def apply_proposal(self, proposal_id):
        with self.lock:
            proposal = self.store.get("proposals", proposal_id)
            if proposal["status"] != "pending_review":
                raise Invalid("Proposal was already reviewed")
            if proposal.get("kind") == "definition":
                return self.apply_definition_proposal(proposal)
            if proposal.get('kind') == 'investigation':
                raise Invalid('An investigation is a plan, not executable instructions. Draft packaging steps from the plan, then review the resulting definition.')
            project = self.store.get("projects", proposal["project_id"])
            if hashlib.sha256(json.dumps(project["recipe"], sort_keys=True).encode()).hexdigest() != proposal["base_recipe_sha256"]:
                raise Invalid("Recipe changed after this proposal; draft again before applying")
            updated = self.update_project(project["id"], {"recipe": proposal["recipe"]})
            self.store.update("proposals", proposal_id, status="applied", reviewed_at=now())
            return updated

    def configure_toolchain(self, payload):
        if not isinstance(payload, dict) or not isinstance(payload.get("identity"), str) or not payload["identity"].strip():
            raise Invalid("Provide the toolchain identity and an archive beneath the data/toolchains directory")
        material = payload.get("material")
        path = confined(self.data / "toolchains", material, must_exist=True)
        if path.stat().st_size > MAX_FILE:
            raise Invalid("Toolchain archive exceeds 1 GiB")
        if not zipfile.is_zipfile(path):
            import tarfile
            if not tarfile.is_tarfile(path):
                raise Invalid("Toolchain material must be a Docker TAR or ZIP archive")
        return self.save_setting("toolchain", {"identity": payload["identity"][:500], "material": material,
                                              "sha256": checksum(path), "size": path.stat().st_size})

    def archive_toolchain(self, image):
        if not isinstance(image, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:\-]*@sha256:[a-f0-9]{64}", image):
            raise Invalid("Choose an already installed toolchain image pinned by digest")
        if not shutil.which("docker"):
            raise Invalid("Docker is unavailable; place an existing toolchain archive under data/toolchains and register it instead")
        try:
            probe = subprocess.run(["docker", "image", "inspect", "--format", "{{json .RepoDigests}}", image],
                                   capture_output=True, text=True, timeout=10)
            if probe.returncode or image not in json.loads(probe.stdout):
                raise Invalid("Pinned toolchain image is not installed or the Docker daemon is inaccessible")
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise Invalid("Could not inspect the local Docker toolchain image") from None
        root = self.data / "toolchains"
        root.mkdir(exist_ok=True)
        name = "toolchain-" + image.rsplit(":", 1)[-1] + ".tar"
        output = confined(root, name)
        temp = confined(root, name + ".part")
        process = None
        try:
            process = subprocess.Popen(["docker", "image", "save", "--output", str(temp), image],
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            started = time.monotonic()
            while process.poll() is None:
                if self.stop.is_set() or time.monotonic() - started > 1800 or (temp.exists() and temp.stat().st_size > MAX_FILE):
                    process.terminate()
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise Invalid("Toolchain archival exceeded the time/size limit")
                time.sleep(0.2)
            if process.returncode or not temp.is_file():
                raise Invalid("Docker could not save the installed toolchain image")
            os.replace(temp, output)
            return self.configure_toolchain({"identity": image, "material": name})
        finally:
            temp.unlink(missing_ok=True)

    def publish(self, artifact_id):
        # Single publisher serializes current-version swaps and database state.
        with self.lock:
            artifact = self.store.get("artifacts", artifact_id)
            archive = Path(artifact["file"])
            if checksum(archive) != artifact["sha256"]:
                raise Invalid("Package checksum changed; refusing to publish")
            project = self.store.get("projects", artifact["project_id"])
            # Freeze the library basename once, so renaming the display name cannot leave stale current files.
            if not project.get("library_basename"):
                project = self.store.update("projects", project["id"], library_basename=f"{slug(project['name'])}-{project['id'][:8]}")
            output_name = f"{project['library_basename']}-{artifact['target']}.zip"
            output = confined(self.library_root, output_name)
            temp = confined(self.library_root, f".{output_name}.{identifier()}.part")
            try:
                shutil.copyfile(archive, temp)
                if checksum(temp) != artifact["sha256"]:
                    raise Invalid("Copied package failed checksum verification")
                os.replace(temp, output)
            finally:
                temp.unlink(missing_ok=True)
            for old in self.store.all("artifacts"):
                if old["project_id"] == artifact["project_id"] and old["target"] == artifact["target"] and old["published"]:
                    self.store.update("artifacts", old["id"], published=False)
            return self.store.update("artifacts", artifact_id, published=True, published_at=now(), published_path=str(output))

    def demo(self):
        # Synthetic input belongs under our data directory; never write into a user's input mount.
        demo_input = b"PORTKEEPER SYNTHETIC FIXTURE - NOT A ROM\n"
        fixture_root = self.data / "demo-inputs"
        fixture_root.mkdir(exist_ok=True)
        (fixture_root / "demo-assets.bin").write_bytes(demo_input)
        recipe = {"version": 1, "mode": "assemble", "payload": {"kind": "source"}, "inputs": [],
                  "expected_files": ["DEMO.txt", "assets/demo-assets.bin"],
                  "notes": "Synthetic multi-target packaging test. Contains no game, executable, or ROM."}
        project = self.create_project({"name": "Synthetic preservation demo", "repo": "portkeeper/synthetic-demo",
                                       "targets": list(TARGETS), "poll_minutes": 360, "recipe": recipe})
        project["demo"] = True
        project["status"] = "archived"
        project["latest_version"] = "fixture-1.0"
        cid = identifier()
        root = self.archive_root / "sources" / project["id"] / cid
        root.mkdir(parents=True)
        with zipfile.ZipFile(root / "source.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("fixture/DEMO.txt", "Portkeeper synthetic fixture. This package demonstrates preservation and assembly only.\n")
            archive.writestr("fixture/assets/demo-assets.bin", demo_input)
        capture = {"id": cid, "project_id": project["id"], "repo": project["repo"], "version": "fixture-1.0",
                   "commit": "synthetic-fixture", "release_url": None, "created_at": now(), "root": str(root),
                   "source": {"file": "source.zip", "sha256": checksum(root / "source.zip"), "size": (root / "source.zip").stat().st_size}, "assets": []}
        (root / "capture.json").write_text(json.dumps(capture, indent=2), encoding="utf-8")
        self.store.put("captures", capture)
        project["capture_id"] = cid
        self.store.put("projects", project)
        return {"project": project, **self.build(project["id"])}

    def start_scheduler(self):
        def poll():
            while not self.stop.wait(15):
                for project in self.store.all("projects"):
                    if self.stop.is_set():
                        return
                    if project.get("demo"):
                        continue
                    with self.lock:
                        if project["id"] in self.busy:
                            continue
                    last = project.get("last_checked")
                    if last:
                        from datetime import datetime
                        elapsed = time.time() - datetime.fromisoformat(last).timestamp()
                        if elapsed < project["poll_minutes"] * 60:
                            continue
                    try:
                        self.check(project["id"])
                    except Busy:
                        pass
        if self.scheduler and self.scheduler.is_alive():
            return
        self.scheduler = threading.Thread(target=poll, daemon=True, name="portkeeper-scheduler")
        self.scheduler.start()
        # Matching is independent of builds and runs after RomM's asynchronous file scan.
        def match_imports():
            from .romm_identity import ImportIdentities
            matcher = ImportIdentities(self.romm, self.data, self.store)
            while not self.stop.is_set():
                if self.romm.public_config().get("configured"):
                    try:
                        with self.match_lock:
                            result = matcher.sync()
                        self.save_setting("romm_import_matching", {"checked_at": now(), **result})
                        self.publisher.poll()
                        checked_prefixes = set()
                        for project in self.store.all("projects"):
                            prefix = (project.get("metadata") or {}).get("filename_prefix")
                            if prefix and prefix not in checked_prefixes:
                                checked_prefixes.add(prefix)
                                self.reviews.imports(project["id"])
                    except Exception:
                        # Do not expose credentials or remote response bodies in the status.
                        self.save_setting("romm_import_matching", {"checked_at": now(), "error": "Import matching failed. Check RomM connection and roms.write permission."})
                if self.stop.wait(60):
                    return
        self.identity_scheduler = threading.Thread(target=match_imports, daemon=True, name="portkeeper-romm-matching")
        self.catalog.start()
        self.identity_scheduler.start()
