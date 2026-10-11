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


ALLOWED_HOSTS = {"github.com", "api.github.com", "codeload.github.com",
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
        for project in self.store.all("projects"):
            if project["status"] in ("checking", "building"):
                self.store.update("projects", project["id"], status="needs_attention", last_error="Previous job was interrupted. Retry explicitly.")

    def close(self):
        self.stop.set()
        if self.scheduler:
            self.scheduler.join(timeout=5)
        if self.identity_scheduler:
            self.identity_scheduler.join(timeout=5)
        self.executor.shutdown(wait=True, cancel_futures=False)

    def state(self):
        projects = self.store.all("projects")
        jobs = self.store.all("jobs")[:150]
        artifacts = self.store.all("artifacts")
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
        return {"projects": projects, "jobs": jobs, "artifacts": artifacts,
                "settings": {"input_root": str(self.input_root), "library_root": str(self.library_root),
                             "archive_root": str(self.archive_root), "docker_enabled": self.enable_docker,
                             "toolchain": self.get_setting("toolchain", {})},
                "runner": {"docker_available": self.docker_available, "docker_error": self.docker_error,
                           "profile_builds_enabled": self.enable_profile_builds, "profile_runner": self.shipwright.availability()},
                "romm": public_romm, "profiles": catalog(), "notifications": self.lifecycle.notifications(),
                "assistant": assistant,
                "summary": {"projects": len(projects), "artifacts": len(artifacts),
                            "needs_attention": sum(p["status"] == "needs_attention" for p in projects)}}

    def templates(self):
        return {"recipes": [
            {"id": "shipwright", "name": "Ship of Harkinian · source preservation", "description": "Archives stable tagged source. Asset extraction and playable builds need your own tested recipe.", "recipe": CAPTURE_RECIPE},
            {"id": "release", "name": "Release assembly · fill in your files", "description": "Select an upstream asset for each target, supply input SHA-256 values, and declare expected output files.", "recipe": {
                "version": 1, "mode": "capture", "payload": {"kind": "release", "asset_patterns": {}},
                "inputs": [], "expected_files": [], "notes": "Change mode to assemble after configuring patterns and expected files."}}]}

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
        self.store.update("jobs", job_id, stage=stage, checkpoints=checkpoints)
        self.store.append_log(job_id, message)

    def notify(self, project_id, kind, message, **details):
        return self.lifecycle.notify(project_id, kind, str(message)[:1000], **details)

    def retry(self, job_id):
        job = self.store.get("jobs", job_id)
        if job["status"] != "failed":
            raise Invalid("Only failed jobs can be retried")
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
        if recipe["mode"] == "profile" and not profile_id:
            raise Invalid("Select a verified project profile before using its fixed recipe")
        if auto_build and recipe["mode"] == "capture":
            raise Invalid("Configure a build recipe before enabling automatic builds")
        checked = {"name": name, "repo": repo, "targets": targets,
                "poll_minutes": interval, "auto_build": auto_build, "auto_publish": auto_publish, "recipe": recipe}
        metadata = config.get("metadata")
        if metadata is not None:
            from .romm_identity import validate_identity
            metadata = validate_identity(metadata)
        checked["metadata"] = metadata
        if profile_id:
            checked.update(profile=profile_id, rom_source=config.get("rom_source"))
        return checked

    def create_project(self, config):
        project = {**self.validate_project(config), "id": identifier(),
                   "created_at": now(), "last_checked": None, "status": "idle", "last_error": None,
                   "latest_version": None, "capture_id": None}
        return self.store.put("projects", project)

    def update_project(self, project_id, config):
        with self.lock:
            if project_id in self.busy:
                raise Busy("Wait for the running project job before editing its recipe")
            project = self.store.get("projects", project_id)
            allowed = {"name", "targets", "poll_minutes", "auto_build", "auto_publish", "recipe", "rom_source", "metadata"}
            if not isinstance(config, dict) or any(key not in allowed for key in config):
                raise Invalid("Only name, targets, poll_minutes, auto_build, auto_publish, and recipe can be edited")
            merged = {**project, **config}
            checked = self.validate_project(merged)
            for key in allowed:
                if key in checked:
                    project[key] = checked[key]
            return self.store.put("projects", project)

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

    def claim(self, project_id):
        with self.lock:
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

    def check(self, project_id, build_after=False, release_version=None):
        project = self.store.get("projects", project_id)
        if project.get("demo"):
            raise Invalid("The synthetic demo is local and has no upstream updates")
        if release_version is not None:
            from .profiles import ensure_release
            if not project.get("profile"):
                raise Invalid("Explicit release capture is available for verified profiles")
            ensure_release(project["profile"], release_version)
        self.claim(project_id)
        job = self.job(project, "capture")
        job = self.store.update("jobs", job["id"], build_after=build_after, release_version=release_version)
        self.executor.submit(self._check, project, job, build_after, release_version)
        return {"job": job}

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
            patterns = {target: target_recipe(recipe, target).get("payload", {}).get("asset_patterns", {}).get(target)
                        for target in project["targets"]}
            selected = []
            for asset in release["assets"]:
                name = asset.get("name", "")
                if len(name) > 255 or not name:
                    raise Invalid("Invalid upstream asset name")
                if any(re.search(patterns[target], name) for target in project["targets"] if patterns.get(target)):
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
            self.store.update("jobs", jid, version=capture["version"], status="archived", finished_at=now())
            if project.get("profile"):
                from .profiles import ensure_release
                ensure_release(project["profile"], capture["version"], capture["commit"])
                self.store.update("projects", project["id"], compatible_capture_id=capture["id"], compatible_version=capture["version"])
                self.shipwright.capture_dependencies(lambda stage, message: self.stage(jid, stage, message))
            self.stage(jid, "complete", "Release preservation finished")
            if build_after or (changed and project["auto_build"]):
                jobs = [self.store.update("jobs", self.job(project, target, capture["version"])["id"], capture_id=capture["id"]) for target in project["targets"]]
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

    def build(self, project_id, targets=None, capture_id=None):
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
        self.claim(project_id)
        jobs = [self.store.update("jobs", self.job(project, target, capture["version"])["id"], capture_id=capture["id"]) for target in chosen]
        self.executor.submit(self._run_group, project, capture, jobs)
        return {"jobs": jobs}

    def _run_group(self, project, capture, jobs):
        try:
            self._build_group(project, capture, jobs)
        finally:
            self.release_claim(project["id"])

    def _build_group(self, project, capture, jobs):
        self.store.update("projects", project["id"], status="building", last_error=None)
        errors = []
        for job in jobs:
            try:
                artifact = self._build_one(project, capture, job)
                if project.get("auto_publish"):
                    self.publish(artifact["id"])
                    self.store.append_log(job["id"], "Automatically published validated package to the library")
                self.notify(project["id"], "ready", f"{project['name']} {capture['version']} is packaged for {job['target']}", artifactid=artifact["id"])
            except Exception as error:
                self.store.append_log(job["id"], str(error))
                self.store.update("jobs", job["id"], status="failed", error=str(error), finished_at=now())
                errors.append(f"{job['target']}: {error}")
                self.notify(project["id"], "needs_input" if "ROM" in str(error) else "build_failed", str(error), jobid=job["id"],
                                      event_key=f"build:{project['id']}:{capture['id']}:{job['target']}:{str(error)[:150]}")
        self.store.update("projects", project["id"], status="needs_attention" if errors else "ready", last_error="; ".join(errors) if errors else None)

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
                    except Exception:
                        # Do not expose credentials or remote response bodies in the status.
                        self.save_setting("romm_import_matching", {"checked_at": now(), "error": "Import matching failed. Check RomM connection and roms.write permission."})
                if self.stop.wait(60):
                    return
        self.identity_scheduler = threading.Thread(target=match_imports, daemon=True, name="portkeeper-romm-matching")
        self.identity_scheduler.start()
