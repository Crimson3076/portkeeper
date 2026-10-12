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
import struct
import subprocess
import tarfile
import threading
import time
import zipfile
from pathlib import Path

from . import packaging
from .safety import MAX_MEMBERS, Invalid, checksum, confined, relative, safe_extract, slug
from .storage import identifier, now

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
    data = Path(path).read_bytes()[:1024 * 1024]
    if data[:4] != b"\x00PSF" or len(data) < 20:
        raise Invalid("param.sfo is not a PS4 SFO file")
    key_start, data_start, count = struct.unpack_from("<III", data, 8)
    result = {}
    for index in range(min(count, 512)):
        key_offset, fmt, length, _maximum, data_offset = struct.unpack_from("<HHIII", data, 20 + index * 16)
        end = data.find(b"\x00", key_start + key_offset)
        key = data[key_start + key_offset:end].decode("ascii", "replace")
        value = data[data_start + data_offset:data_start + data_offset + length]
        if fmt in (0x0004, 0x0204):
            result[key] = value.rstrip(b"\x00").decode("utf-8", "replace")
        elif fmt == 0x0404 and length == 4:
            result[key] = struct.unpack("<I", value)[0]
    return result


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

    def availability(self):
        ssl = Path(COMPAT_SSL)
        return {"xvfb": bool(shutil.which("xvfb-run")),
                "compat_ssl": (ssl / "libssl.so.1.1").is_file() and (ssl / "libcrypto.so.1.1").is_file(),
                "tools": {"ship_o2r_headless": "needs xvfb-run and Mesa (included in the container image)",
                          "opengoal_extract": "runs the release's Linux extractor",
                          "ps4_pkg_extract": "needs the pinned PkgTool and OpenSSL 1.1 libraries (included in the container image)"}}

    # Inputs -----------------------------------------------------------------
    def locate(self, _project, definition, input_id):
        """Find a unique accepted file below the input root (or prepared folder) for an input."""
        item = next(i for i in definition["inputs"] if i["id"] == input_id)
        if item["kind"] == "directory":
            roots, depth = [(self.prepared, "prepared")], 4
        else:
            hint = item.get("platform_hint")
            base = self.engine.input_root / hint if hint else self.engine.input_root
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
                    if path.is_symlink() or (pattern and not pattern.search(name)):
                        continue
                    if item["kind"] == "file":
                        if not path.is_file() or path.stat().st_size > item.get("max_bytes", packaging.GIB):
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
            entry = {"id": item["id"], "label": item["label"], "kind": item["kind"], "required": item.get("required", True),
                     "notes": item.get("notes"), "platform_hint": item.get("platform_hint"),
                     "accepted": [a["label"] for a in item.get("accepted", [])], "found": [], "rejected": []}
            if item["kind"] == "directory":
                entry["found"] = self.locate(None, definition, item["id"])
                result.append(entry)
                continue
            hint = item.get("platform_hint")
            base = self.engine.input_root / hint if hint else self.engine.input_root
            pattern = re.compile(item["name_hint"]) if item.get("name_hint") else None
            if base.is_dir() and not base.is_symlink():
                for directory, dirs, files in os.walk(base):
                    if len(Path(directory).relative_to(base).parts) >= 4:
                        dirs[:] = []
                    dirs[:] = sorted(d for d in dirs if not (Path(directory) / d).is_symlink())
                    for name in sorted(files):
                        path = Path(directory) / name
                        if path.is_symlink() or (pattern and not pattern.search(name)):
                            continue
                        record = {"source": "inputs", "path": path.relative_to(self.engine.input_root).as_posix(), "name": name}
                        try:
                            size = path.stat().st_size
                        except OSError:
                            continue
                        if size > item.get("max_bytes", packaging.GIB):
                            entry["rejected"].append({**record, "reason": "larger than this game accepts"})
                            continue
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
        return result

    def bind_inputs(self, project, definition, log=None, search=True):
        bindings = dict(project.get("input_bindings") or {})
        changed = False
        for item in definition["inputs"]:
            if item["id"] in bindings or not search:
                continue
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
        return self._ps4(ctx, op, destination, timeout)

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
        eboot = folder / "eboot.bin"
        sfo = folder / "sce_sys" / "param.sfo"
        if not eboot.is_file() or not sfo.is_file():
            return None, "eboot.bin or sce_sys/param.sfo is missing"
        values = read_sfo(sfo)
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
        packages = [self.resolve_input(ctx, key) for key in op["packages"]]
        tool_item = ctx.definition["downloads"][op["tool_download"]]
        key = hashlib.sha256(json.dumps({"packages": [p["fingerprint"] for p in packages], "tool": tool_item["sha256"],
                                         "title": op["title_id"], "version": op["app_version"]}, sort_keys=True).encode()).hexdigest()[:24]
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
            for index, (input_id, package) in enumerate(zip(op["packages"], packages)):
                # PkgTool maps files read-write, so it gets a private copy, never the original.
                ctx.stage("preparing_inputs", f"Copying PKG {index + 1} of {len(packages)} to a private work folder")
                copy = work / "pkgs" / f"{index}.pkg"
                shutil.copyfile(package["path"], copy)
                if copy.stat().st_size != package["size"]:
                    raise Invalid("A PKG changed while it was being copied")
                ctx.stage("extracting", f"Extracting PKG {index + 1} of {len(packages)} with PkgTool")
                log_path = work / f"pkgtool-{index}.log"
                # pkg_extract leaves out the header entries (sce_sys/param.sfo and friends);
                # pkg_makegp4 writes the file system and those entries into one folder.
                part = work / f"part-{index}"
                try:
                    self.run([str(binary), "pkg_makegp4", str(copy), str(part)], work, env, log_path, timeout)
                finally:
                    self.keep_log(ctx, "pkgtool", log_path)
                copy.unlink()
                (part / "Project.gp4").unlink(missing_ok=True)
                overlay(part, work / "game")
                shutil.rmtree(part)
            result, problem = self.validate_ps4(op, work / "game")
            if not result:
                raise Invalid(f"The extracted game failed validation: {problem}. Nothing was packaged.")
            if cache.exists():
                shutil.rmtree(cache)
            cache.mkdir(parents=True)
            os.replace(work / "game", cache / "game")
            manifest.write_text(json.dumps({**result, "packages": [p["fingerprint"] for p in packages], "tool_sha256": tool_item["sha256"], "created_at": now()}, indent=2))
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
            lines.append(f'exec "./{op["exec"]}"' + "".join(f' "{arg}"' for arg in args) + ' "$@"')
            return ("\n".join(lines) + "\n").encode(), 0o755
        lines = ["@echo off", "setlocal", 'cd /d "%~dp0"']
        lines += [f'if not exist "{path}" mkdir "{path}"' for path in (p.replace("/", "\\") for p in op.get("mkdir", []))]
        lines += [f'set "{key}={value}"' for key, value in env.items()]
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
                if not confined(include[0], name[len(include[1]) + 1:]).is_file():
                    raise Invalid(f"Expected package file is missing: {name}")
            else:
                confined(package, name, must_exist=True)
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
