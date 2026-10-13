"""Execute packaging definitions with Portkeeper's fixed operation and tool vocabulary.

Each operation is implemented here; definitions only choose and parameterize them. Tools
run official upstream binaries from captured, checksummed release files, or pinned
dependency downloads, with fixed argument lists, bounded time and bounded logs.
"""
import hashlib
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import tarfile
import threading
import time
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

from . import packaging
from .safety import MAX_MEMBERS, Invalid, checksum, confined, relative, safe_extract, slug
from .storage import identifier, now
from . import ps4

HELPERS = Path(__file__).resolve().parent / "helpers"
CHUNK = 8 * 1024 * 1024
COMPAT_SSL = os.environ.get("PORTKEEPER_COMPAT_SSL_DIR", "/opt/portkeeper/compat-ssl")


def digests(path, normalize=None):
    """SHA-1 and SHA-256 of a file, optionally after N64 byte-order normalization."""
    # SHA-1 only matches published dump checksums; integrity decisions also use SHA-256.
    sha1, sha256 = hashlib.sha1(usedforsecurity=False), hashlib.sha256()
    if normalize == "n64_big_endian":
        data = normalize_n64(Path(path).read_bytes())
        sha1.update(data)
        sha256.update(data)
        return {"sha1": sha1.hexdigest(), "sha256": sha256.hexdigest(), "size": len(data)}
    size = 0
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(CHUNK), b""):
            sha1.update(chunk)
            sha256.update(chunk)
            size += len(chunk)
    return {"sha1": sha1.hexdigest(), "sha256": sha256.hexdigest(), "size": size}


def normalize_n64(data):
    """Convert .n64 (little-endian) and .v64 (byte-swapped) dumps to big-endian .z64."""
    magic = data[:4]
    if magic == b"\x80\x37\x12\x40":
        return data
    if magic == b"\x40\x12\x37\x80":
        return b"".join(data[i:i + 4][::-1] for i in range(0, len(data), 4))
    if magic == b"\x37\x80\x40\x12":
        swapped = bytearray(len(data))
        swapped[0::2], swapped[1::2] = data[1::2], data[0::2]
        return bytes(swapped)
    raise Invalid("The file is not an N64 ROM in .z64, .n64 or .v64 byte order")


def accepted(item, digest):
    for entry in item.get("accepted", []):
        if entry.get("sha1") == digest["sha1"] or entry.get("sha256") == digest["sha256"]:
            return entry
    return None


def overlay(source, target):
    """Move source's files into target, replacing same-named files (later PKGs patch earlier ones)."""
    for directory, _dirs, files in os.walk(source):
        relative = Path(directory).relative_to(source)
        (target / relative).mkdir(parents=True, exist_ok=True)
        for name in files:
            os.replace(Path(directory) / name, target / relative / name)


def read_sfo(path):
    """Return string entries from a PS4 param.sfo."""
    with Path(path).open('rb') as stream:
        return ps4.sfo_values(stream.read(ps4.MAX_SFO))


class Context:
    """Paths and state for one build group."""

    def __init__(self, runner, project, definition, capture, workspace, log, stage):
        self.runner, self.project, self.definition, self.capture = runner, project, definition, capture
        self.workspace, self.log, self.stage = workspace, log, stage
        self.shared = workspace / "shared"
        self.shared.mkdir(parents=True, exist_ok=True)
        self.aliases = {}
        self.includes = []
        self.inputs = {}
        self.provenance = {"tools": [], "inputs": {}, "downloads": {}}
        self.assets, problems = packaging.download_assets(definition, capture.get("assets", []))
        self.download_problems = problems

    def path(self, value, package=None):
        root, rest = packaging.split_root(value, "path")
        if root == "shared":
            for alias, target in self.aliases.items():
                if rest == alias or rest.startswith(alias + "/"):
                    tail = rest[len(alias):].lstrip("/")
                    return confined(target, tail) if tail else Path(target)
            return confined(self.shared, rest) if rest else self.shared
        if package is None:
            raise Invalid("Shared operations cannot write into a platform package")
        return confined(package, rest) if rest else package


class OperationRunner:
    def __init__(self, engine):
        self.engine = engine
        self.cache = engine.data / "cache" / "dependencies"
        self.prepared = engine.data / "prepared"
        self.lock = threading.RLock()
        self.pkg_metadata_cache = {}

    def pkg_metadata(self, path):
        state = path.stat()
        key = (str(path), state.st_size, state.st_mtime_ns, state.st_ctime_ns)
        with self.lock:
            if key in self.pkg_metadata_cache:
                return self.pkg_metadata_cache[key]
        result = ps4.pkg_metadata(path)
        with self.lock:
            if len(self.pkg_metadata_cache) >= 512:
                self.pkg_metadata_cache.clear()
            self.pkg_metadata_cache[key] = result
        return result

    def availability(self):
        ssl = Path(COMPAT_SSL)
        return {"xvfb": bool(shutil.which("xvfb-run")),
                "compat_ssl": (ssl / "libssl.so.1.1").is_file() and (ssl / "libcrypto.so.1.1").is_file(),
                "tools": {"ship_o2r_headless": "needs xvfb-run and Mesa (included in the container image)",
                          "opengoal_extract": "runs the release's Linux extractor",
                          "ps4_pkg_extract": "needs the pinned PkgTool and OpenSSL 1.1 libraries (included in the container image)",
                          "wwhd_extract": "runs the release's Linux Wii U extractor on a Cemu archive"}}

    # Inputs -----------------------------------------------------------------
    def locate(self, _project, definition, input_id):
        """Find a unique accepted file below the input root (or prepared folder) for an input."""
        item = next(i for i in definition["inputs"] if i["id"] == input_id)
        expected = ps4.input_expectation(definition, input_id)
        if item["kind"] == "directory":
            roots, depth = [(self.prepared, "prepared")], 4
            if any(op.get('tool') == 'wwhd_extract' and op.get('prepared_input') == input_id for op in definition['operations']):
                base = self.engine.input_root / item['platform_hint'] if item.get('platform_hint') else self.engine.input_root
                roots.append((base, 'inputs'))
        else:
            hint = item.get("platform_hint")
            base = self.engine.input_root / hint if hint else self.engine.input_root
            if expected and not base.is_dir():
                base = self.engine.input_root
            roots, depth = [(base, "inputs")], 4
        pattern = re.compile(item["name_hint"]) if item.get("name_hint") else None
        found = []
        for base, source in roots:
            if not base.is_dir() or base.is_symlink():
                continue
            root_for = self.engine.input_root if source == "inputs" else self.prepared
            for directory, dirs, files in os.walk(base):
                level = Path(directory).relative_to(base).parts
                if len(level) >= depth:
                    dirs[:] = []
                dirs[:] = [d for d in dirs if not (Path(directory) / d).is_symlink()]
                names = dirs if item["kind"] == "directory" else files
                for name in names:
                    path = Path(directory) / name
                    if path.is_symlink() or (not expected and pattern and not pattern.search(name)):
                        continue
                    if item['kind'] == 'directory':
                        preparation = next((op for op in definition['operations'] if op.get('tool') == 'ps4_pkg_extract' and op.get('prepared_input') == input_id), None)
                        if preparation and not self.validate_ps4(preparation, path)[0]:
                            continue
                        wiiu = next((op for op in definition['operations'] if op.get('tool') == 'wwhd_extract' and op.get('prepared_input') == input_id), None)
                        if wiiu:
                            try:
                                self.validate_wiiu_folder(wiiu, path)
                            except (Invalid, OSError):
                                continue
                    if item["kind"] == "file":
                        if not path.is_file() or path.stat().st_size > item.get("max_bytes", packaging.GIB):
                            continue
                        if expected:
                            if path.suffix.lower() != '.pkg':
                                continue
                            try:
                                if ps4.mismatch(self.pkg_metadata(path), expected):
                                    continue
                            except (Invalid, OSError):
                                continue
                        if item.get("verify", "hash") == "hash":
                            try:
                                digest = digests(path, item.get("normalize"))
                            except (Invalid, OSError):
                                continue
                            if not accepted(item, digest):
                                continue
                    found.append({"source": source, "path": path.relative_to(root_for).as_posix()})
        return found

    def find_files(self, definition):
        """For each declared input: accepted files found, and near misses with the reason they were not used."""
        result = []
        for item in definition["inputs"]:
            expected = ps4.input_expectation(definition, item['id'])
            entry = {"id": item["id"], "label": item["label"], "kind": item["kind"], "required": item.get("required", True),
                     "notes": item.get("notes"), "platform_hint": item.get("platform_hint"),
                     "accepted": [a["label"] for a in item.get("accepted", [])], "found": [], "rejected": []}
            if expected:
                entry['ps4'] = expected
            if item["kind"] == "directory":
                entry["found"] = self.locate(None, definition, item["id"])
                result.append(entry)
                continue
            hint = item.get("platform_hint")
            base = self.engine.input_root / hint if hint else self.engine.input_root
            if expected and not base.is_dir():
                base = self.engine.input_root
            pattern = re.compile(item["name_hint"]) if item.get("name_hint") else None
            if base.is_dir() and not base.is_symlink():
                for directory, dirs, files in os.walk(base):
                    if len(Path(directory).relative_to(base).parts) >= 4:
                        dirs[:] = []
                    dirs[:] = sorted(d for d in dirs if not (Path(directory) / d).is_symlink())
                    for name in sorted(files):
                        path = Path(directory) / name
                        if path.is_symlink() or (expected and path.suffix.lower() != '.pkg') or (not expected and pattern and not pattern.search(name)):
                            continue
                        record = {"source": "inputs", "path": path.relative_to(self.engine.input_root).as_posix(), "name": name}
                        try:
                            size = path.stat().st_size
                        except OSError:
                            continue
                        if size > item.get("max_bytes", packaging.GIB):
                            entry["rejected"].append({**record, "reason": "larger than this game accepts"})
                            continue
                        if expected:
                            try:
                                metadata = self.pkg_metadata(path)
                                problem = ps4.mismatch(metadata, expected)
                            except (Invalid, OSError) as error:
                                metadata, problem = None, str(error)
                            if problem:
                                entry['rejected'].append({**record, 'reason': problem})
                                continue
                            record['ps4'] = metadata
                        if item.get("verify", "hash") != "hash":
                            entry["found"].append({**record, "accepted": "checked during the build", "size": size})
                            continue
                        try:
                            digest = digests(path, item.get("normalize"))
                        except (Invalid, OSError):
                            entry["rejected"].append({**record, "reason": "not a readable game file"})
                            continue
                        match = accepted(item, digest)
                        if match:
                            entry["found"].append({**record, "accepted": match["label"], "sha1": digest["sha1"], "size": size})
                        else:
                            entry["rejected"].append({**record, "reason": "not an accepted version", "sha1": digest["sha1"]})
                        if len(entry["found"]) + len(entry["rejected"]) >= 25:
                            break
            entry["rejected"] = entry["rejected"][:10]
            result.append(entry)
        for entry in result:
            if not entry.get('ps4') or entry['ps4']['role'] != 'update' or entry['required']:
                continue
            base = next((item for item in result if item.get('ps4', {}).get('role') == 'base' and item['ps4']['title_id'] == entry['ps4']['title_id']), None)
            if base and base['found'] and any(item['ps4'].get('app_version') != entry['ps4']['app_version'] for item in base['found']):
                entry['required'] = True
                entry['notes'] = f"Needed to bring the base game to {entry['ps4']['app_version']}. " + (entry.get('notes') or '')
        return result

    def bind_inputs(self, project, definition, log=None, search=True):
        bindings = dict(project.get("input_bindings") or {})
        changed = False
        for item in definition["inputs"]:
            if item["id"] in bindings or not search:
                continue
            if item['kind'] == 'directory' and any(op.get('prepared_input') == item['id'] and any(key in bindings for key in op.get('packages', [])) for op in definition['operations']):
                continue  # Explicit PKGs take precedence over a discovered older prepared folder.
            found = self.locate(project, definition, item["id"])
            if len(found) == 1:
                bindings[item["id"]] = found[0]
                changed = True
                if log:
                    log(f"Found {item['label']}: {found[0]['path']}")
            elif len(found) > 1 and log:
                log(f"Several files match {item['label']}; choose one in Packaging instructions → Game files")
        if changed:
            project = self.engine.store.update("projects", project["id"], input_bindings=bindings)
        return project

    def resolve_input(self, ctx, input_id):
        if input_id in ctx.inputs:
            return ctx.inputs[input_id]
        item = next(i for i in ctx.definition["inputs"] if i["id"] == input_id)
        binding = (ctx.project.get("input_bindings") or {}).get(input_id)
        if not binding:
            if item.get("required", True):
                raise Invalid(f"Missing game file: {item['label']}. Choose it in Packaging instructions → Game files.")
            ctx.inputs[input_id] = None
            return None
        root = self.prepared if binding.get("source") == "prepared" else self.engine.input_root
        path = confined(root, binding["path"])
        if item["kind"] == "directory":
            if not path.is_dir() or path.is_symlink():
                raise Invalid(f"{item['label']} folder is missing: {binding['path']}")
            record = {"path": path, "kind": "directory"}
        else:
            if not path.is_file() or path.is_symlink():
                raise Invalid(f"Missing game file: {item['label']} ({binding['path']})")
            size = path.stat().st_size
            if size > item.get("max_bytes", packaging.GIB):
                raise Invalid(f"{item['label']} is larger than its declared limit")
            record = {"path": path, "kind": "file", "size": size}
            state = path.stat()
            record['fingerprint'] = {'path': binding['path'], 'size': state.st_size, 'mtime_ns': state.st_mtime_ns}
            if item.get("verify", "hash") == "hash":
                ctx.stage("verifying_inputs", "Checking " + item["label"])
                digest = digests(path, item.get("normalize"))
                match = accepted(item, digest)
                if not match:
                    raise Invalid(f"{item['label']} does not match an accepted revision (SHA-1 {digest['sha1']}). Nothing was packaged.")
                record.update(digest=digest, accepted=match["label"])
                ctx.log(f"Verified {item['label']}: {match['label']}")
                if binding.get("sha1") != digest["sha1"]:
                    with self.engine.lock:
                        current = self.engine.store.get("projects", ctx.project["id"])
                        bindings = dict(current.get("input_bindings") or {})
                        if bindings.get(input_id, {}).get("path") == binding["path"]:
                            bindings[input_id] = {**bindings[input_id], "sha1": digest["sha1"], "accepted": match["label"], "verified_at": now()}
                            self.engine.store.update("projects", ctx.project["id"], input_bindings=bindings)
            else:
                stat_result = path.stat()
                record["fingerprint"] = {"path": binding["path"], "size": stat_result.st_size, "mtime_ns": stat_result.st_mtime_ns}
        ctx.inputs[input_id] = record
        ctx.provenance["inputs"][input_id] = {k: v for k, v in record.items() if k in ("accepted", "digest", "fingerprint", "size")}
        return record

    def build_key(self, project, definition, capture, target):
        """Identify what a package is made from. The same key means the same package would be built again."""
        inputs = {}
        for item in definition.get("inputs", []):
            binding = (project.get("input_bindings") or {}).get(item["id"])
            if not binding:
                inputs[item["id"]] = None
                continue
            root = self.prepared if binding.get("source") == "prepared" else self.engine.input_root
            try:
                stat_result = confined(root, binding["path"]).stat()
                inputs[item["id"]] = [binding["path"], stat_result.st_size, stat_result.st_mtime_ns]
                if ps4.input_expectation(definition, item['id']):
                    inputs[item['id']].append(stat_result.st_ctime_ns)
            except (Invalid, OSError):
                inputs[item["id"]] = [binding["path"], None, None]
        payload = {"definition": hashlib.sha256(json.dumps(definition, sort_keys=True, separators=(",", ":")).encode()).hexdigest(),
                   "runner_version": packaging.RUNNER_VERSION, "target": target, "commit": capture.get("commit"),
                   "assets": sorted((a.get("name"), a.get("sha256")) for a in capture.get("assets", [])), "inputs": inputs}
        return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()

    def same_package(self, project, version, target, key):
        """An archived package built from exactly the same release, definition and game files, if any."""
        for artifact in self.engine.store.all("artifacts"):
            if (artifact["project_id"] == project["id"] and artifact["target"] == target and artifact.get("version") == version
                    and artifact.get("build_key") == key and Path(artifact.get("file", "")).is_file()):
                return artifact
        return None

    def replace_older(self, artifact, log):
        """Remove earlier packages of the same version and platform that nothing else refers to."""
        engine = self.engine
        project = engine.store.get("projects", artifact["project_id"])
        good = set((project.get("known_good") or {}).values())
        tested = {(t.get("package") or {}).get("artifact_id") for t in engine.store.all("playtests")}
        for older in engine.store.all("artifacts"):
            if (older["id"] == artifact["id"] or older["project_id"] != artifact["project_id"] or older["target"] != artifact["target"]
                    or older.get("version") != artifact.get("version")):
                continue
            reasons = [label for flag, label in ((older.get("pinned"), "pinned"), (older["id"] in good, "known-good"),
                                                 (older["id"] in tested, "has a test result"), (older.get("published"), "copied to the library"),
                                                 ((older.get("romm") or {}).get("status") in ("uploading", "awaiting_scan", "in_romm"), "in RomM"))
                       if flag]
            if reasons:
                log(f"Kept the earlier {older.get('filename')} because it is " + " and ".join(reasons))
                continue
            try:
                path = engine.lifecycle._archive_directory(older)
            except Invalid as error:
                log(f"Kept the earlier {older.get('filename')}: {error}")
                continue
            shutil.rmtree(path)
            engine.store.delete("artifacts", older["id"])
            log(f"Replaced the earlier {older.get('filename')} to save space")

    # Downloads --------------------------------------------------------------
    def dependency(self, item, log=None):
        """Capture a pinned download from another repository, verified by SHA-256."""
        folder = self.cache / item["sha256"]
        archive = folder / item["asset"]
        with self.lock:
            if archive.is_file() and checksum(archive) == item["sha256"]:
                return archive
            folder.mkdir(parents=True, exist_ok=True)
            url = f"https://github.com/{item['repository']}/releases/download/{item['tag']}/{item['asset']}"
            if log:
                log(f"Preserving pinned dependency {item['repository']} {item['tag']} {item['asset']}")
            result = self.engine.github.download(url, archive)
            if result["sha256"] != item["sha256"]:
                archive.unlink(missing_ok=True)
                raise Invalid(f"{item['asset']} does not match its pinned SHA-256; it was not used")
            (folder / "source.json").write_text(json.dumps({"url": url, **item, "captured_at": now()}, indent=2))
            return archive

    def capture_dependencies(self, definition, log=None):
        return {key: str(self.dependency(item, log)) for key, item in definition.get("downloads", {}).items() if item.get("repository")}

    def download_file(self, ctx, key):
        item = ctx.definition["downloads"][key]
        if item.get("repository"):
            path = self.dependency(item, ctx.log)
            ctx.provenance["downloads"][key] = {"name": item["asset"], "sha256": item["sha256"], "repository": item["repository"], "tag": item["tag"]}
            return path, item["asset"]
        asset = ctx.assets.get(key)
        if not asset:
            raise Invalid(f"The preserved release has no file for download {key}. " + "; ".join(ctx.download_problems))
        path = Path(ctx.capture["root"]) / asset["file"]
        if checksum(path) != asset["sha256"]:
            raise Invalid(f"Preserved release file {asset['name']} changed on disk; check upstream again")
        ctx.provenance["downloads"][key] = {"name": asset["name"], "sha256": asset["sha256"]}
        return path, asset["name"]

    def unpack(self, ctx, key, destination, file_name=None):
        item = ctx.definition["downloads"][key]
        path, name = self.download_file(ctx, key)
        fmt = item.get("format", "auto")
        if fmt == "auto":
            fmt = "zip" if zipfile.is_zipfile(path) else "tar" if tarfile.is_tarfile(path) else "file"
        destination.mkdir(parents=True, exist_ok=True)
        if fmt == "file":
            output = confined(destination, file_name or name)
            if output.exists():
                raise Invalid(f"{output.name} already exists in the package")
            shutil.copyfile(path, output)
            output.chmod(0o755)
            return
        temp = ctx.workspace / ("unpack-" + identifier())
        safe_extract(path, temp, windows_names=True)
        source = temp
        if item.get("strip_prefix"):
            source = confined(temp, item["strip_prefix"])
            if not source.is_dir():
                raise Invalid(f"{name} no longer contains its {item['strip_prefix']} folder")
        for child in source.iterdir():
            target = destination / child.name
            if target.exists():
                raise Invalid(f"{child.name} from {name} would replace an existing package file")
            shutil.move(str(child), str(target))
        shutil.rmtree(temp, ignore_errors=True)

    # Processes --------------------------------------------------------------
    def run(self, arguments, cwd, env, log_path, timeout, done=None):
        """Run a fixed argument list in its own process group with time and log limits.

        done() may return True once the expected output is complete; the process is then
        stopped. This is how GUI-only extractors are ended after they finish their work.
        """
        base = {"PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"), "LANG": "C.UTF-8"}
        with open(log_path, "wb") as output:
            process = subprocess.Popen(arguments, cwd=cwd, env={**base, **env}, stdout=output, stderr=subprocess.STDOUT,
                                       stdin=subprocess.DEVNULL, start_new_session=True)
            started, finished = time.monotonic(), False
            try:
                while process.poll() is None:
                    if done and done():
                        finished = True
                        break
                    if self.engine.stop.is_set() or time.monotonic() - started > timeout or Path(log_path).stat().st_size > 64 * 1024 * 1024:
                        raise Invalid("Tool exceeded its time or log limit, or the service stopped")
                    time.sleep(1)
            finally:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                        process.wait(timeout=10)
                    except (ProcessLookupError, subprocess.TimeoutExpired):
                        try:
                            os.killpg(process.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                        process.wait()
        if not finished and process.returncode != 0:
            raise Invalid(f"Tool exited with code {process.returncode}: " + self.tail(log_path, 1500))
        return finished or process.returncode == 0

    @staticmethod
    def tail(path, limit=4000):
        try:
            data = Path(path).read_bytes()[-limit:]
        except OSError:
            return ""
        return re.sub(r"\x1b\[[0-9;]*m", "", data.decode("utf-8", "replace"))

    def keep_log(self, ctx, name, path):
        lines = self.tail(path, 6000).splitlines()[-40:]
        for line in lines:
            ctx.log(f"[{name}] {line[:400]}")

    # Tools ------------------------------------------------------------------
    def tool(self, ctx, op):
        name = op["tool"]
        timeout = op.get("timeout_minutes", 30) * 60
        destination = ctx.path(op["to"])
        if destination.exists():
            raise Invalid(f"{op['to']} already exists")
        if name == "ship_o2r_headless":
            return self._ship_o2r(ctx, op, destination, timeout)
        if name == "opengoal_extract":
            return self._opengoal(ctx, op, destination, timeout)
        if name == "wwhd_extract":
            return self._wwhd(ctx, op, destination, timeout)
        return self._ps4(ctx, op, destination, timeout)

    def validate_wiiu_folder(self, op, folder):
        self.check_tree(folder, 16 * packaging.GIB)
        rpx, metadata = folder / 'code/cking.rpx', folder / 'meta/meta.xml'
        if not rpx.is_file() or not metadata.is_file() or not (folder / 'content').is_dir():
            raise Invalid('The Wii U folder must contain code/cking.rpx, content/ and meta/meta.xml')
        if metadata.stat().st_size > 1024 * 1024:
            raise Invalid('Wii U meta.xml exceeds its size limit')
        text = metadata.read_bytes()
        if b'<!DOCTYPE' in text.upper() or b'<!ENTITY' in text.upper():
            raise Invalid('Wii U metadata must not contain XML entity declarations')
        try:
            root = ET.fromstring(text)
            title = (root.findtext('title_id') or '').strip().lower()
            version = int((root.findtext('title_version') or '-1').strip())
        except (ET.ParseError, ValueError):
            raise Invalid('Wii U meta.xml has invalid title/version metadata') from None
        if title not in op['titles'] or version != 0:
            raise Invalid(f'Wii U dump is title {title or "unknown"}, version {version}; choose a supported base game, version 0')
        digest = checksum(rpx)
        if digest not in op['accepted_rpx_sha256']:
            raise Invalid('code/cking.rpx does not match a supported base-game SHA-256; nothing was packaged')
        return {'title': title, 'version': version, 'rpx_sha256': digest}

    def _wwhd(self, ctx, op, destination, timeout):
        """Extract one title folder (code/, content/, meta/) from the person's Cemu archive (.wua)
        with the extractor the release ships, then check the game code against accepted hashes."""
        ctx.stage("extracting", "Extracting your Wii U game with the release's extractor")
        prepared = self.resolve_input(ctx, op['prepared_input']) if op.get('prepared_input') else None
        if prepared:
            info = self.validate_wiiu_folder(op, prepared['path'])
            rel = packaging.split_root(op['to'], 'to')[1]
            ctx.aliases[rel] = prepared['path']
            ctx.provenance['tools'].append({'tool': 'wwhd_extract', 'format': 'decrypted folder', **info})
            ctx.log('Validated your decrypted Wii U folder; originals remain unchanged')
            return
        archive = self.resolve_input(ctx, op["archive"])
        if archive is None:
            raise Invalid('Choose a Wii U .wua, ZIP of a decrypted game, or a decrypted code/content/meta folder')
        if zipfile.is_zipfile(archive['path']):
            run = ctx.workspace / ('wiiu-zip-' + identifier())
            try:
                safe_extract(archive['path'], run, max_expanded=16 * packaging.GIB)
                matches = []
                for rpx in run.rglob('cking.rpx'):
                    if rpx.parent.name != 'code':
                        continue
                    try:
                        info = self.validate_wiiu_folder(op, rpx.parent.parent)
                    except Invalid:
                        continue
                    matches.append((rpx.parent.parent, info))
                if len(matches) != 1:
                    raise Invalid('The ZIP must contain exactly one supported version-0 code/content/meta game folder with an accepted cking.rpx SHA-256')
                game, info = matches[0]
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(game), str(destination))
                ctx.provenance['tools'].append({'tool': 'wwhd_extract', 'format': 'decrypted ZIP', **info})
                ctx.log('Extracted and validated your decrypted Wii U ZIP; originals remain unchanged')
                return
            finally:
                shutil.rmtree(run, ignore_errors=True)
        run = ctx.workspace / ("wwhd-" + identifier())
        self.unpack(ctx, op["download"], run)
        extractor = confined(run, op["extractor"], must_exist=True)
        extractor.chmod(0o755)
        (run / "home").mkdir()
        env = {"HOME": str(run / "home")}
        info, found = {}, []
        for title in op["titles"]:
            log_path = ctx.workspace / "wwhd-info.log"
            try:
                self.run([str(extractor), "--title", title, "info", str(archive["path"])], run, env, log_path, 600)
                ok = True
            except Invalid:
                ok = False
            info = {}
            for line in Path(log_path).read_text("utf-8", "replace").splitlines():
                key, _, value = line.partition(" ")
                if key == "title":
                    found.append(value.split(" ")[0])
                elif key in ("selected", "version", "format", "error:"):
                    info[key] = value.strip()
            if ok and info.get("selected"):
                break
            if info.get("format") != "wua":
                raise Invalid(f"{archive['path'].name} is not a readable Cemu Wii U archive: {info.get('error:') or self.tail(log_path, 300)}")
            info = {}
        if not info.get("selected"):
            raise Invalid(f"{archive['path'].name} does not contain title " + " or ".join(op["titles"]) + (f" (it contains {', '.join(sorted(set(found)))})" if found else "") + ". Make the Cemu archive with the base game included.")
        if info.get("version") not in ("0", "00"):
            raise Invalid(f"The game in this archive is version {info.get('version')}; only version 0 (without the update merged in) is supported.")
        ctx.log(f"Using {info['selected']} from {archive['path'].name}")
        output = run / "game"
        log_path = ctx.workspace / "wwhd-extract.log"
        try:
            self.run([str(extractor), "--title", info["selected"], "extract", str(archive["path"]), str(output)], run, env, log_path, timeout)
        finally:
            self.keep_log(ctx, "wwhd-extract", log_path)
        rpx = output / "code" / "cking.rpx"
        if not rpx.is_file() or not (output / "content").is_dir() or not (output / "meta" / "meta.xml").is_file():
            raise Invalid("The extractor did not produce code/cking.rpx, content/ and meta/meta.xml")
        digest = checksum(rpx)
        if digest not in op["accepted_rpx_sha256"]:
            raise Invalid(f"code/cking.rpx has SHA-256 {digest[:16]}…, which is not an accepted version 0 build. Nothing was packaged.")
        self.check_tree(output, 64 * packaging.GIB)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(output), str(destination))
        ctx.provenance["tools"].append({"tool": "wwhd_extract", "title": info["selected"], "rpx_sha256": digest})
        ctx.log(f"Extracted and verified {info['selected']} (code/cking.rpx {digest[:16]}…)")
        shutil.rmtree(run, ignore_errors=True)

    def _ship_o2r(self, ctx, op, destination, timeout):
        if not shutil.which("xvfb-run"):
            raise Invalid("This build needs xvfb-run and Mesa; use the Portkeeper container image or install xvfb")
        ctx.stage("extracting", f"Generating {op['output']} with the official runtime under a virtual display")
        run = ctx.workspace / ("o2r-" + identifier())
        self.unpack(ctx, op["download"], run)
        appimage = confined(run, op["appimage"], must_exist=True)
        appimage.chmod(0o755)
        rom = ctx.path(op["rom"])
        if not rom.is_file():
            raise Invalid("The ROM must be staged before generating assets")
        shutil.copyfile(rom, run / rom.name)
        (run / "home").mkdir()
        (run / "tmp").mkdir()
        output = run / op["output"]
        state = {"size": -1, "since": time.monotonic()}

        def complete():
            if not output.is_file():
                return False
            size = output.stat().st_size
            if size != state["size"]:
                state.update(size=size, since=time.monotonic())
                return False
            if time.monotonic() - state["since"] < 8:
                return False
            try:
                with zipfile.ZipFile(output) as archive:
                    return len(archive.namelist()) >= op.get("min_entries", 1)
            except (zipfile.BadZipFile, OSError):
                return False

        env = {"HOME": str(run / "home"), "TMPDIR": str(run / "tmp"), "SHIP_HOME": str(run), "APPIMAGE_EXTRACT_AND_RUN": "1",
               "LIBGL_ALWAYS_SOFTWARE": "1", "SDL_AUDIODRIVER": "dummy", "XDG_RUNTIME_DIR": str(run / "tmp")}
        log_path = ctx.workspace / "o2r.log"
        try:
            self.run(["xvfb-run", "-a", "-s", "-screen 0 1280x720x24", "./" + op["appimage"], rom.name], run, env, log_path, timeout, complete)
        finally:
            self.keep_log(ctx, "o2r", log_path)
        if not complete():
            raise Invalid(f"The runtime exited without producing a complete {op['output']}. Upstream extraction may have changed; review the log.")
        with zipfile.ZipFile(output) as archive:
            names = archive.namelist()
            if archive.testzip() is not None:
                raise Invalid(f"{op['output']} failed its ZIP CRC check")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(output), str(destination))
        ctx.provenance["tools"].append({"tool": "ship_o2r_headless", "output": op["output"], "entries": len(names),
                                        "sha256": checksum(destination), "note": "Not byte-for-byte reproducible between runs"})
        ctx.log(f"Generated {op['output']} with {len(names)} entries")
        shutil.rmtree(run, ignore_errors=True)

    def _opengoal(self, ctx, op, destination, timeout):
        ctx.stage("extracting", "Extracting, validating, decompiling and compiling game data with OpenGOAL")
        iso = self.resolve_input(ctx, op["iso"])
        run = ctx.workspace / ("opengoal-" + identifier())
        self.unpack(ctx, op["download"], run)
        extractor = confined(run, "extractor", must_exist=True)
        extractor.chmod(0o755)
        (run / "home").mkdir()
        log_path = ctx.workspace / "opengoal.log"
        try:
            self.run(["./extractor", str(iso["path"]), "--proj-path", str(run / "data"), "-e", "-v", "-d", "-c",
                      "--game", op["game"], "--disable-ansi"], run, {"HOME": str(run / "home")}, log_path, timeout)
        finally:
            self.keep_log(ctx, "extractor", log_path)
        for path in op["collect"]:
            source = confined(run, path)
            if not source.is_dir() or not any(source.iterdir()):
                raise Invalid(f"The extractor did not produce {path}; upstream behavior may have changed")
            target = confined(destination, path)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(source), str(target))
        ctx.provenance["tools"].append({"tool": "opengoal_extract", "game": op["game"], "collected": op["collect"]})
        shutil.rmtree(run, ignore_errors=True)

    def validate_ps4(self, op, folder):
        if not folder.is_dir() or folder.is_symlink():
            return None, 'the prepared folder is missing or is a link'
        try:
            eboot = confined(folder, 'eboot.bin')
            sfo = confined(folder, 'sce_sys/param.sfo')
        except Invalid as error:
            return None, str(error)
        if not eboot.is_file() or not sfo.is_file():
            return None, "eboot.bin or sce_sys/param.sfo is missing"
        try:
            values = read_sfo(sfo)
        except Invalid as error:
            return None, str(error)
        if values.get("TITLE_ID") != op["title_id"]:
            return None, f"title is {values.get('TITLE_ID')!r}, expected {op['title_id']}"
        if values.get("APP_VER") != op["app_version"]:
            return None, f"version is {values.get('APP_VER')!r}, expected {op['app_version']}"
        digest = checksum(eboot)
        if digest not in op["accepted_eboot_sha256"]:
            return None, f"eboot.bin SHA-256 {digest} is not accepted"
        total, count = 0, 0
        for directory, dirs, files in os.walk(folder):
            for name in dirs + files:
                path = Path(directory) / name
                mode = path.lstat().st_mode
                if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    return None, "the extracted game contains links or special files"
                if stat.S_ISREG(mode):
                    total += path.stat().st_size
                    count += 1
        return {"title_id": op["title_id"], "app_version": op["app_version"], "eboot_sha256": digest, "files": count, "bytes": total}, None

    def run_ps4_extraction(self, arguments, work, env, log_path, timeout):
        try:
            self.run(arguments, work, env, log_path, timeout)
        except Invalid as error:
            if 'PFS image is encrypted but no decryption key was provided' in str(error):
                raise Invalid('This PKG has an encrypted game filesystem and PkgTool could not obtain its decryption key. Readable title/version metadata does not establish extractability. Choose an extractable dump from your owned game, or a prepared game folder matching the required title, version and eboot.bin SHA-256. Original PKGs and existing packages are unchanged; see the extraction log for tool details.') from None
            raise

    def extract_ps4_package(self, ctx, binary, package, part, metadata, work, env, log_path, timeout):
        if metadata['content_type'] != 0x1E:
            self.run_ps4_extraction([str(binary), 'pkg_makegp4', str(package), str(part)], work, env, log_path, timeout)
            return
        # PkgTool v0.2's GP4 creator rejects DP project types even for full patch
        # filesystems. Its fixed extraction commands handle those filesystems.
        ctx.log('Extracting the update filesystem and its named sce_sys entries separately')
        if not any(entry['path'] == 'param.sfo' for entry in metadata['system_entries']):
            raise Invalid('The update PKG has no param.sfo entry; its version cannot be checked')
        deadline = time.monotonic() + timeout
        raw = part.with_name(part.name + '-filesystem')
        self.run_ps4_extraction([str(binary), 'pkg_extract', str(package), str(raw)], work, env, log_path, timeout)
        self.keep_log(ctx, 'pkgtool-filesystem', log_path)
        self.check_tree(raw, packaging.MAX_PACKAGE_BYTES)
        if (raw / 'uroot').is_dir():
            os.replace(raw / 'uroot', part)
            shutil.rmtree(raw)
        elif (raw / 'eboot.bin').is_file():
            os.replace(raw, part)
        else:
            raise Invalid('The update did not contain a usable game filesystem; use a full, extractable update PKG')
        for entry in metadata['system_entries']:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise Invalid('PS4 update extraction exceeded its time limit')
            output = confined(part, 'sce_sys/' + entry['path'])
            output.parent.mkdir(parents=True, exist_ok=True)
            entry_log = work / f'{log_path.stem}-entry-{entry["index"]}.log'
            try:
                self.run([str(binary), 'pkg_extractentry', str(package), str(entry['index']), str(output)], work, env, entry_log, remaining)
            finally:
                self.keep_log(ctx, 'pkgtool-system-entry', entry_log)

    def _ps4(self, ctx, op, destination, timeout):
        rel = packaging.split_root(op["to"], "to")[1]
        if op.get("prepared_input"):
            prepared = self.resolve_input(ctx, op["prepared_input"])
            if prepared:
                ctx.stage("verifying_inputs", "Checking the previously extracted game folder")
                result, problem = self.validate_ps4(op, prepared["path"])
                if result:
                    ctx.aliases[rel] = prepared["path"]
                    ctx.provenance["tools"].append({"tool": "ps4_pkg_extract", "reused": "prepared folder", **result})
                    ctx.log(f"Using the previously extracted game: {result['files']} files, eboot.bin accepted")
                    return
                ctx.log(f"The prepared game folder was not used: {problem}")
        # Check cache write access before hashing/copying potentially huge inputs.
        cache_root = confined(self.prepared, 'ps4')
        try:
            cache_root.mkdir(parents=True, exist_ok=True)
            probe = cache_root / ('.write-check-' + identifier())
            try:
                with probe.open('xb'):
                    pass
            finally:
                probe.unlink(missing_ok=True)
        except PermissionError:
            raise Invalid('PS4 preparation cache is not writable. Give the Portkeeper service user write access to /data/prepared/ps4 in the app-data mount, then retry; original PKGs remain read-only.') from None
        selected = [(key, self.resolve_input(ctx, key)) for key in op['packages']]
        selected = [(key, package) for key, package in selected if package is not None]
        if not selected or selected[0][0] != op['packages'][0]:
            raise Invalid('Choose the base game PKG first, or a validated prepared game folder')
        metadata = []
        for input_id, package in selected:
            # Refresh metadata for a build; the picker cache is only a browsing aid.
            identified = ps4.pkg_metadata(package['path'])
            problem = ps4.mismatch(identified, ps4.input_expectation(ctx.definition, input_id))
            if problem:
                raise Invalid(f"{input_id}: {problem}. Choose the matching base game and update PKGs under Advanced → Your game files.")
            metadata.append(identified)
        base_version = metadata[0].get('app_version')
        if len(selected) == 1 and base_version and base_version != op['app_version']:
            raise Invalid(f"The base PKG is version {base_version}; this port needs {op['app_version']}. Choose the separate {op['title_id']} update PKG. Extract the base first and apply the update second.")
        known_versions = [item.get('app_version') for item in metadata]
        for earlier, later in zip(known_versions, known_versions[1:]):
            if earlier and later and re.fullmatch(r'\d{2}\.\d{2}', earlier) and re.fullmatch(r'\d{2}\.\d{2}', later) and later < earlier:
                raise Invalid(f'The selected update would downgrade the game from {earlier} to {later}; choose updates in increasing version order')
        fingerprints = []
        for input_id, package in selected:
            ctx.stage('verifying_inputs', f"Checking {input_id} PKG checksum ({package['size'] / packaging.GIB:.1f} GB)")
            digest, read, last = hashlib.sha256(), 0, time.monotonic()
            with package['path'].open('rb') as stream:
                while chunk := stream.read(CHUNK):
                    digest.update(chunk)
                    read += len(chunk)
                    if time.monotonic() - last >= 5:
                        ctx.stage('verifying_inputs', f"Checking {input_id}: {read / packaging.GIB:.1f} of {package['size'] / packaging.GIB:.1f} GB")
                        last = time.monotonic()
            state = package['path'].stat()
            if read != package['size'] or state.st_mtime_ns != package['fingerprint']['mtime_ns']:
                raise Invalid('A PKG changed while its checksum was being checked; finish copying the file and try again')
            fingerprint = {**package['fingerprint'], 'sha256': digest.hexdigest()}
            fingerprints.append(fingerprint)
            ctx.provenance['inputs'][input_id]['sha256'] = digest.hexdigest()
        packages = [package for _key, package in selected]
        tool_item = ctx.definition["downloads"][op["tool_download"]]
        key = hashlib.sha256(json.dumps({"packages": [p['sha256'] for p in fingerprints], "tool": tool_item["sha256"],
                                         "title": op["title_id"], "version": op["app_version"], 'accepted_eboot': op['accepted_eboot_sha256']}, sort_keys=True).encode()).hexdigest()[:24]
        cache = confined(self.prepared, f"ps4/{op['title_id']}-{key}")
        manifest = cache / "portkeeper-extraction.json"
        if manifest.is_file():
            result, problem = self.validate_ps4(op, cache / "game")
            if result:
                ctx.aliases[rel] = cache / "game"
                ctx.provenance["tools"].append({"tool": "ps4_pkg_extract", "reused": "earlier extraction", **result})
                ctx.log("Reusing the earlier validated PKG extraction")
                return
            ctx.log(f"Ignoring an earlier extraction: {problem}")
        if not (Path(COMPAT_SSL) / "libssl.so.1.1").is_file():
            raise Invalid("PkgTool needs OpenSSL 1.1 libraries; use the Portkeeper container image")
        tool_zip = self.dependency(tool_item, ctx.log)
        required_space = sum(p['size'] for p in packages) * 2 + 256 * 1024 * 1024
        self.prepared.mkdir(parents=True, exist_ok=True)
        if shutil.disk_usage(self.prepared).free < required_space:
            raise Invalid(f"Not enough space in app data for extraction and packaging. Allow roughly {required_space / packaging.GIB:.1f} GB free for these PKGs; actual extracted size can vary.")
        work = cache.with_name(cache.name + ".work")
        shutil.rmtree(work, ignore_errors=True)
        (work / "pkgs").mkdir(parents=True)
        (work / "home").mkdir()
        safe_extract(tool_zip, work / "tool")
        binary = confined(work / "tool", "PkgTool.Core", must_exist=True)
        binary.chmod(0o755)
        env = {"HOME": str(work / "home"), "DOTNET_SYSTEM_GLOBALIZATION_INVARIANT": "1",
               "DOTNET_BUNDLE_EXTRACT_BASE_DIR": str(work / "home"), "LD_LIBRARY_PATH": COMPAT_SSL}
        try:
            previous_version = None
            for index, (input_id, package) in enumerate(selected):
                # PkgTool maps files read-write, so it gets a private copy, never the original.
                ctx.stage("preparing_inputs", f"Copying PKG {index + 1} of {len(packages)} to a private work folder")
                copy = work / "pkgs" / f"{index}.pkg"
                copied, last_report = 0, time.monotonic()
                with package['path'].open('rb') as source, copy.open('wb') as output:
                    for chunk in iter(lambda: source.read(CHUNK), b''):
                        output.write(chunk)
                        copied += len(chunk)
                        if time.monotonic() - last_report >= 5:
                            ctx.log(f"Copying {input_id}: {copied / packaging.GIB:.1f} of {package['size'] / packaging.GIB:.1f} GB")
                            last_report = time.monotonic()
                ctx.log(f"Private copy of {input_id} complete; verifying its checksum")
                if copy.stat().st_size != package["size"]:
                    raise Invalid("A PKG changed while it was being copied")
                if checksum(copy) != fingerprints[index]['sha256']:
                    raise Invalid('A PKG changed while being copied; the private copy does not match the checked source')
                ctx.stage("extracting", f"Extracting PKG {index + 1} of {len(packages)} with PkgTool")
                log_path = work / f"pkgtool-{index}.log"
                # pkg_extract leaves out the header entries (sce_sys/param.sfo and friends);
                # pkg_makegp4 writes the file system and those entries into one folder.
                part = work / f"part-{index}"
                try:
                    self.extract_ps4_package(ctx, binary, copy, part, metadata[index], work, env, log_path, timeout)
                finally:
                    self.keep_log(ctx, "pkgtool", log_path)
                copy.unlink()
                (part / "Project.gp4").unlink(missing_ok=True)
                sfo = confined(part, 'sce_sys/param.sfo', must_exist=True)
                values = read_sfo(sfo)
                if values.get('TITLE_ID') != op['title_id']:
                    raise Invalid(f"{input_id} extracted title {values.get('TITLE_ID')!r}, expected {op['title_id']}. No prepared folder was saved.")
                version = values.get('APP_VER')
                if not isinstance(version, str) or not re.fullmatch(r'\d{2}\.\d{2}', version):
                    raise Invalid(f'{input_id} has no readable PS4 app version after extraction')
                if previous_version and version < previous_version:
                    raise Invalid(f"{input_id} would downgrade the game from {previous_version} to {version}; select the correct update PKG")
                previous_version = version
                self.check_tree(part, packaging.MAX_PACKAGE_BYTES)
                ctx.log(f"Validated {metadata[index]['role']} PKG: {op['title_id']} {version}; applying {'base files' if index == 0 else 'update over the base game'}")
                overlay(part, work / "game")
                shutil.rmtree(part)
            result, problem = self.validate_ps4(op, work / "game")
            if not result:
                raise Invalid(f"The extracted game failed validation: {problem}. Check the matching base/update PKGs and whether they are extractable dumps. Nothing was packaged.")
            if cache.exists():
                shutil.rmtree(cache)
            cache.mkdir(parents=True)
            os.replace(work / "game", cache / "game")
            manifest.write_text(json.dumps({**result, "packages": fingerprints, 'pkg_metadata': metadata, "tool_sha256": tool_item["sha256"], "created_at": now()}, indent=2))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        ctx.aliases[rel] = cache / "game"
        ctx.provenance["tools"].append({"tool": "ps4_pkg_extract", **result})
        ctx.log(f"Extracted and validated the game: {result['files']} files")

    # Operations -------------------------------------------------------------
    def launcher(self, op, ctx):
        env, args = op.get("env", {}), op.get("args", [])
        if op["template"] == "posix":
            lines = ["#!/bin/sh", "set -eu", 'cd -- "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"']
            if op.get("mkdir"):
                lines.append("mkdir -p " + " ".join(op["mkdir"]))
            lines += [f'export {key}="{value}"' for key, value in env.items()]
            lines += [f"python3 ./{script}" for script in op.get("run_python", [])]
            if op.get("chmod_exec"):
                lines.append(f'chmod +x "./{op["exec"]}"')
            prepare = op.get("prepare_once")
            if prepare:
                lines.append(f'if [ ! -e "./{prepare["unless_exists"]}" ]; then "./{prepare["exec"]}"' + "".join(f' "{arg}"' for arg in prepare.get("args", [])) + "; fi")
            lines.append(f'exec "./{op["exec"]}"' + "".join(f' "{arg}"' for arg in args) + ' "$@"')
            return ("\n".join(lines) + "\n").encode(), 0o755
        lines = ["@echo off", "setlocal", 'cd /d "%~dp0"']
        lines += [f'if not exist "{path}" mkdir "{path}"' for path in (p.replace("/", "\\") for p in op.get("mkdir", []))]
        lines += [f'set "{key}={value}"' for key, value in env.items()]
        prepare = op.get("prepare_once")
        if prepare:
            lines.append(f'if not exist "{prepare["unless_exists"].replace("/", chr(92))}" "%~dp0{prepare["exec"].replace("/", chr(92))}"' + "".join(f' "{arg}"' for arg in prepare.get("args", [])))
            lines.append("if errorlevel 1 exit /b 1")
        lines.append(f'"%~dp0{op["exec"].replace("/", chr(92))}"' + "".join(f' "{arg}"' for arg in args) + " %*")
        return ("\r\n".join(lines) + "\r\n").encode(), 0o644

    def apply(self, ctx, op, package=None, target=None):
        kind = op["op"]
        context = {"version": ctx.capture["version"], "target": target or "", "name": ctx.definition["name"],
                   "repository": ctx.definition["upstream"]["repository"],
                   "launch_command": ctx.definition.get("launch", {}).get(target or "", {}).get("command", "")}
        if kind == "unpack_download":
            self.unpack(ctx, op["download"], ctx.path(op["to"], package), op.get("file_name"))
        elif kind == "stage_input":
            item = next(i for i in ctx.definition["inputs"] if i["id"] == op["input"])
            record = self.resolve_input(ctx, op["input"])
            output = ctx.path(op["to"], package)
            if output.exists():
                raise Invalid(f"{op['to']} already exists")
            output.parent.mkdir(parents=True, exist_ok=True)
            data = Path(record["path"]).read_bytes() if item.get("normalize") else None
            if data is not None:
                with output.open("wb") as handle:
                    handle.write(packaging_normalize(item, data))
            else:
                shutil.copyfile(record["path"], output)
            if item.get("verify", "hash") == "hash":
                digest = digests(output)
                if not accepted(item, digest):
                    raise Invalid(f"The staged copy of {item['label']} does not match; the file changed during the build")
        elif kind == "copy":
            source, output = ctx.path(op["from"], package), ctx.path(op["to"], package)
            if source.is_dir():
                shutil.copytree(source, output, dirs_exist_ok=True, copy_function=shutil.copyfile)
            elif source.is_file():
                if output.exists():
                    raise Invalid(f"{op['to']} already exists")
                output.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, output)
            else:
                raise Invalid(f"{op['from']} was not produced by an earlier step")
        elif kind == "include_tree":
            source = ctx.path(op["from"])
            if not source.is_dir():
                raise Invalid(f"{op['from']} was not produced by an earlier step")
            ctx.includes.append((source, str(relative(op["as"]))))
        elif kind == "launcher":
            body, mode = self.launcher(op, ctx)
            output = confined(package, op["path"])
            if output.exists():
                raise Invalid(f"{op['path']} already exists in the package")
            output.write_bytes(body)
            output.chmod(mode)
        elif kind == "helper":
            output = ctx.path(op["to"], package)
            if output.exists():
                raise Invalid(f"{op['to']} already exists")
            output.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(HELPERS / (op["name"] + ".py"), output)
            output.chmod(0o644)
        elif kind == "write_text":
            output = ctx.path(op["path"], package)
            if output.exists():
                raise Invalid(f"{op['path']} already exists")
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(packaging.render_text(op["text"], context), encoding="utf-8")
        elif kind == "touch":
            output = ctx.path(op["path"], package)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.touch()
        elif kind == "tool":
            self.tool(ctx, op)

    @staticmethod
    def shared_op(op):
        if op["op"] == "tool":
            return True
        targets = [op.get(k) for k in ("to", "path") if op.get(k)]
        return bool(targets) and all(str(t).startswith("$shared") for t in targets)

    def check_tree(self, root, limit, includes=()):
        total, count = 0, 0
        for base in [root, *[source for source, _ in includes]]:
            for directory, dirs, files in os.walk(base, followlinks=False):
                for name in dirs + files:
                    path = Path(directory) / name
                    mode = path.lstat().st_mode
                    if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                        raise Invalid("Package output contains links or special files")
                    count += 1
                    if stat.S_ISREG(mode):
                        total += path.stat().st_size
                if count > MAX_MEMBERS * 4 or total > limit:
                    raise Invalid("Package output exceeds its declared size limit")
        return total, count

    def write_zip(self, package, includes, destination, compression):
        method = zipfile.ZIP_STORED if compression == "stored" else zipfile.ZIP_DEFLATED
        names = set()
        with zipfile.ZipFile(destination, "w", method, compresslevel=None if method == zipfile.ZIP_STORED else 6, allowZip64=True) as bundle:
            sources = [(package, "")] + [(source, prefix) for source, prefix in includes]
            for base, prefix in sources:
                for path in sorted(base.rglob("*")):
                    if path.is_symlink():
                        raise Invalid("Package contains a symlink")
                    if not path.is_file():
                        continue
                    name = (prefix + "/" if prefix else "") + path.relative_to(base).as_posix()
                    if name in names:
                        raise Invalid(f"Two package steps produced {name}")
                    names.add(name)
                    info = zipfile.ZipInfo(name, time.localtime(path.stat().st_mtime)[:6] if path.stat().st_mtime > 315532800 else (1980, 1, 1, 0, 0, 0))
                    info.create_system = 3
                    info.external_attr = (stat.S_IFREG | (0o755 if path.stat().st_mode & 0o111 else 0o644)) << 16
                    info.compress_type = method
                    with path.open("rb") as incoming, bundle.open(info, "w", force_zip64=path.stat().st_size > 2 ** 31) as outgoing:
                        shutil.copyfileobj(incoming, outgoing, CHUNK)
        return names

    def build_group(self, project, definition, capture, jobs, stage_for, log_for, finish):
        """Build each job's target. finish(job, artifact|None, error|None) records the result."""
        group = self.engine.work_root / ("group-" + identifier())
        group.mkdir(parents=True)
        ctx = Context(self, project, definition, capture, group, lambda m: [log_for(j["id"])(m) for j in jobs],
                      lambda s, m: [stage_for(j["id"])(s, m) for j in jobs])
        try:
            try:
                project = self.bind_inputs(project, definition, ctx.log)
                ctx.project = project
                ctx.stage("preparing", "Preparing shared build steps")
                for op in definition["operations"]:
                    if self.shared_op(op):
                        self.apply(ctx, op)
            except Exception as error:
                for job in jobs:
                    finish(job, None, error)
                return
            for job in jobs:
                target = job["target"]
                ctx.log = log_for(job["id"])
                ctx.stage = stage_for(job["id"])
                ctx.includes = []
                try:
                    package = group / ("package-" + target)
                    package.mkdir()
                    ctx.stage("packaging_" + target, f"Assembling the {target} package")
                    for op in definition["operations"]:
                        if self.shared_op(op) or (op.get("targets") and target not in op["targets"]):
                            continue
                        self.apply(ctx, op, package, target)
                    artifact = self.archive(ctx, package, target, job)
                    finish(job, artifact, None)
                except Exception as error:
                    finish(job, None, error)
                finally:
                    shutil.rmtree(group / ("package-" + target), ignore_errors=True)
        finally:
            shutil.rmtree(group, ignore_errors=True)

    @staticmethod
    def op_inputs(op):
        """Game file inputs an operation cannot run without."""
        if op["op"] == "stage_input":
            return [op["input"]]
        if op.get("tool") == "opengoal_extract":
            return [op["iso"]]
        if op.get("tool") == "ps4_pkg_extract":
            return op["packages"][:1]
        if op.get("tool") == "wwhd_extract":
            return [op["archive"]]
        return []

    def trial(self, project, definition, capture, targets, log):
        """Build a draft without archiving it. Steps that need unselected game files are skipped,
        and checks that depend on them are reported as unverified instead of failed."""
        bindings = project.get("input_bindings") or {}
        group = self.engine.work_root / ("trial-" + identifier())
        group.mkdir(parents=True)
        skipped, skipped_paths, results = [], [], {}

        def blocked(op):
            missing = [i for i in self.op_inputs(op) if i not in bindings]
            if op.get('tool') == 'wwhd_extract' and op.get('prepared_input') in bindings:
                missing = []
            sources = [op.get(key) for key in ("from", "rom") if isinstance(op.get(key), str)]
            if not missing and not any(s == p or s.startswith(p.rstrip("/") + "/") for s in sources for p in skipped_paths):
                return False
            skipped_paths.extend(op[key] for key in ("to", "path") if isinstance(op.get(key), str))
            if op["op"] == "include_tree":
                skipped_paths.append("$package/" + op["as"])
            skipped.append({"op": op["op"], "tool": op.get("tool"), "needs": missing or ["output of a skipped step"]})
            log(f"Skipped {op.get('tool') or op['op']}: " + (f"game file {', '.join(missing)} is not selected" if missing else "it uses the output of a skipped step"))
            return True

        def under_skipped(name):
            path = "$package/" + name
            return any(path == p or path.startswith(p.rstrip("/") + "/") for p in skipped_paths)

        try:
            ctx = Context(self, project, definition, capture, group, log, lambda _stage, message: log(message))
            try:
                if ctx.download_problems:
                    raise Invalid("; ".join(ctx.download_problems))
                for op in definition["operations"]:
                    if self.shared_op(op) and not blocked(op):
                        self.apply(ctx, op)
            except Exception as error:
                log(f"Shared steps failed: {error}")
                return {"status": "failed", "stage": "shared steps", "error": str(error), "skipped": skipped, "targets": {}}
            for target in targets:
                ctx.includes = []
                package = group / ("package-" + target)
                package.mkdir()
                result = {"status": "passed", "missing": [], "unverified": []}
                try:
                    for op in definition["operations"]:
                        if self.shared_op(op) or (op.get("targets") and target not in op["targets"]):
                            continue
                        if not blocked(op):
                            self.apply(ctx, op, package, target)
                    for op in definition["operations"]:
                        if op["op"] == "launcher" and (not op.get("targets") or target in op["targets"]):
                            if not confined(package, op["exec"]).is_file():
                                (result["unverified"] if under_skipped(op["exec"]) else result["missing"]).append(op["exec"])
                    for name in definition["outputs"][target]["expected_files"]:
                        include = next(((source, prefix) for source, prefix in ctx.includes if name.startswith(prefix + "/")), None)
                        path = confined(include[0], name[len(include[1]) + 1:]) if include else confined(package, name)
                        if not path.exists():
                            (result["unverified"] if under_skipped(name) else result["missing"]).append(name)
                    total, count = self.check_tree(package, definition["outputs"][target].get("max_bytes", 2 * packaging.GIB), ctx.includes)
                    files = sorted(p.relative_to(package).as_posix() for p in package.rglob("*") if p.is_file())
                    result.update(files=count, bytes=total, top_level=sorted({f.split("/", 1)[0] for f in files})[:40], sample_files=files[:200])
                    if result["missing"]:
                        result.update(status="failed", error="Expected files or launcher targets are missing: " + ", ".join(result["missing"][:20]))
                    elif result["unverified"] or skipped:
                        result["status"] = "partial"
                except Exception as error:
                    result.update(status="failed", error=str(error))
                results[target] = result
                log(f"{target}: {result['status']}" + (f": {result['error']}" if result.get("error") else ""))
                shutil.rmtree(package, ignore_errors=True)
        finally:
            shutil.rmtree(group, ignore_errors=True)
        statuses = {r["status"] for r in results.values()}
        status = "failed" if "failed" in statuses else "partial" if "partial" in statuses else "passed"
        return {"status": status, "skipped": skipped, "targets": results}

    def archive(self, ctx, package, target, job):
        output = ctx.definition["outputs"][target]
        ctx.stage("validating", "Checking expected files and package limits")
        for op in ctx.definition["operations"]:
            if op["op"] == "launcher" and (not op.get("targets") or target in op["targets"]):
                if not confined(package, op["exec"]).is_file():
                    raise Invalid(f"The launcher {op['path']} runs {op['exec']}, which is not in the {target} package. Nothing was packaged.")
        total, count = self.check_tree(package, output.get("max_bytes", 2 * packaging.GIB), ctx.includes)
        for name in output["expected_files"]:
            include = next(((source, prefix) for source, prefix in ctx.includes if name.startswith(prefix + "/")), None)
            if include:
                expected = confined(include[0], name[len(include[1]) + 1:])
            else:
                expected = confined(package, name)
            if not expected.exists() or (name.endswith('/') and not expected.is_dir()):
                raise Invalid(f"Expected package file or directory is missing: {name}")
        ctx.log("Expected files and output safety checks passed. Game launch has not been tested.")
        engine = self.engine
        previous = [a for a in engine.store.all("artifacts") if a["project_id"] == ctx.project["id"] and a["target"] == target and a["version"] == ctx.capture["version"]]
        counter = f"{target}:{ctx.capture['version']}"
        with engine.lock:
            counters = dict(engine.store.get("projects", ctx.project["id"]).get("package_revisions") or {})
            revision = max([counters.get(counter, 0), *[a.get("package_revision") or 0 for a in previous], len(previous)]) + 1
            counters[counter] = revision
            engine.store.update("projects", ctx.project["id"], package_revisions=counters)
        build_key = job.get("build_key") or self.build_key(ctx.project, ctx.definition, ctx.capture, target)
        definition_sha = hashlib.sha256(json.dumps(ctx.definition, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        compatibility = packaging.evaluate_release(ctx.definition, ctx.capture, targets=[target])
        manifest = {"schema": 2, "project": ctx.project["name"], "repo": ctx.project["repo"], "version": ctx.capture["version"],
                    "target": target, "upstream_commit": ctx.capture["commit"], "capture_id": ctx.capture["id"],
                    "definition": {"id": ctx.definition["id"], "revision": ctx.definition["revision"], "sha256": definition_sha},
                    "package_revision": revision, "release_review": compatibility["status"], "build_key": build_key,
                    "downloads": ctx.provenance["downloads"], "inputs": ctx.provenance["inputs"], "tools": ctx.provenance["tools"],
                    "launch": ctx.definition.get("launch", {}).get(target), "dependencies": ctx.definition.get("dependencies", {}).get(target, []),
                    "limitations": ctx.definition.get("limitations", []), "files": count, "bytes": total, "created_at": now(),
                    "validation": "Expected files present; no launch or gameplay verification"}
        manifest_path = package / "portkeeper-manifest.json"
        if manifest_path.exists():
            raise Invalid("The package must not already contain portkeeper-manifest.json")
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        aid = identifier()
        root = engine.archive_root / "packages" / ctx.project["id"] / aid
        root.mkdir(parents=True)
        prefix = (ctx.project.get("metadata") or {}).get("filename_prefix") or ctx.definition.get("package_prefix") or slug(ctx.project["name"])
        filename = f"{slug(prefix)}-{slug(ctx.capture['version'])}-{target}-{now()[:10]}.zip"
        archive = root / filename
        temp = root / (filename + ".part")
        try:
            ctx.stage("archiving", f"Writing {filename}")
            self.write_zip(package, ctx.includes, temp, output.get("compression", "deflate"))
            size = temp.stat().st_size
            crc_checked = False
            if size <= 4 * packaging.GIB:
                with zipfile.ZipFile(temp) as bundle:
                    if bundle.testzip() is not None:
                        raise Invalid("The package failed its ZIP CRC check")
                crc_checked = True
            os.replace(temp, archive)
        except Exception:
            shutil.rmtree(root, ignore_errors=True)
            raise
        (root / "manifest.json").write_text(json.dumps(manifest, indent=2))
        artifact = {"id": aid, "project_id": ctx.project["id"], "project_name": ctx.project["name"], "target": target,
                    "version": ctx.capture["version"], "created_at": now(), "size": archive.stat().st_size, "sha256": checksum(archive),
                    "published": False, "file": str(archive), "filename": filename, "manifest": manifest,
                    "download_url": f"/api/artifacts/{aid}/download", "demo": False, "validation_level": "packaged", "pinned": False,
                    "package_revision": revision, "definition_id": ctx.definition["id"], "definition_revision": ctx.definition["revision"],
                    "release_review": compatibility["status"], "zip_crc_checked": crc_checked, "build_key": build_key}
        engine.store.put("artifacts", artifact)
        ctx.log(f"Archived package: {filename} (revision {revision})")
        self.replace_older(artifact, ctx.log)
        return artifact


def packaging_normalize(item, data):
    if item.get("normalize") == "n64_big_endian":
        return normalize_n64(data)
    return data
