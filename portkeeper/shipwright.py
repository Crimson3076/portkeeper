"""Reviewed Shipwright asset pipeline; arbitrary recipes never run on the host."""

import importlib.util
import json
import os
import shutil
import signal
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from types import SimpleNamespace

from .safety import Invalid, checksum, safe_extract
from .storage import now


COMMIT = "ecd889c2019b87e78b0a3d942a6a3fe98b7efaed"
VERSION = "9.3.0"
DEPENDENCIES = {
    "torch": ("HarbourMasters/Torch", "2ab12fe9660aec04e02ee89fe81baed304a1a1d6"),
    "libultraship": ("kenix3/libultraship", "62e973aeb4a53ad4d22bb91e2d9373ecdfcd246c"),
    "spdlog": ("gabime/spdlog", "486b55554f11c9cccc913e11a87085b2a91f706f"),
    "yaml-cpp": ("jbeder/yaml-cpp", "56e3bb550c91fd7005566f19c079cb7a503223cf"),
    "tinyxml2": ("leethomason/tinyxml2", "321ea883b7190d4e85cae5512a12e5eaa8f8731f"),
    "zlib": ("madler/zlib", "51b7f2abdade71cd9bb0e7a373ef2610ec6f9daf"),
}
PATCH_SHA256 = "64808cddff86dd631728dcd2cf657a98201e8dd77ca9fd723bd36c62aa9eb082"


def packager():
    path = Path(__file__).resolve().parent.parent / "tools/shipwright_package.py"
    spec = importlib.util.spec_from_file_location("portkeeper_shipwright_package", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ShipwrightRunner:
    def __init__(self, engine):
        self.engine = engine
        self.root = engine.data / "cache/shipwright" / VERSION
        self.root.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.helper = packager()

    def availability(self):
        missing = [name for name in ("cmake", "ninja", "cc", "c++", "patch", "lsb_release") if not shutil.which(name)]
        return {"enabled": self.engine.enable_profile_builds, "missing_tools": missing,
                "ready": self.engine.enable_profile_builds and not missing}

    def materials(self):
        result = {}
        manifest = self.root / "dependencies.json"
        if manifest.is_file():
            for name, record in json.loads(manifest.read_text()).items():
                path = self.root / record["file"]
                if path.is_file() and checksum(path) == record["sha256"]:
                    result[name] = {"path": str(path), "revision": record["commit"]}
            result["dependency-lock"] = str(manifest)
        patch = Path(__file__).resolve().parent.parent / "tools/patches/soh-torch-cli-read-rom.patch"
        result["extractor-patch"] = str(patch)
        built = self.root / "build/soh-torch"
        if built.is_file():
            result["extractor-binary"] = str(built)
        provenance = self.root / "extractor.json"
        if provenance.is_file():
            result["extractor-provenance"] = str(provenance)
        return result

    def capture_dependencies(self, stage):
        """Capture exact source archives before running a compiler, including retries."""
        with self.lock:
            stage("capturing_dependencies", "Preserving pinned submodules and extraction dependencies")
            manifest_path = self.root / "dependencies.json"
            records = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
            for name, (repo, commit) in DEPENDENCIES.items():
                record = records.get(name)
                output = self.root / (name + ".zip")
                if not record or not output.is_file() or checksum(output) != record.get("sha256"):
                    url = f"https://codeload.github.com/{repo}/zip/{commit}"
                    record = {"file": output.name, "repo": repo, "commit": commit,
                              **self.engine.github.download(url, output)}
                    records[name] = record
                    temp = manifest_path.with_suffix(".json.part")
                    temp.write_text(json.dumps(records, indent=2))
                    os.replace(temp, manifest_path)
            return records

    def _unpack(self, archive, destination):
        if destination.exists():
            shutil.rmtree(destination)
        temp = destination.with_name(destination.name + "-unpack")
        if temp.exists():
            shutil.rmtree(temp)
        safe_extract(archive, temp)
        children = list(temp.iterdir())
        if len(children) != 1 or not children[0].is_dir():
            raise Invalid("Pinned dependency archive must have a single source directory")
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(children[0], destination)
        temp.rmdir()

    def _run(self, arguments, log, cwd=None):
        """Bound only the explicitly reviewed tool commands and their process group."""
        with log.open("wb") as output:
            process = subprocess.Popen(arguments, cwd=cwd, stdout=output, stderr=subprocess.STDOUT,
                                       start_new_session=True)
            started = time.monotonic()
            while process.poll() is None:
                if self.engine.stop.is_set() or time.monotonic() - started > 1800 or log.stat().st_size > 8 * 1024 * 1024:
                    os.killpg(process.pid, signal.SIGTERM)
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                    raise Invalid("Reviewed extractor command exceeded its time/log limit or service stopped")
                time.sleep(0.1)
        if process.returncode:
            tail = log.read_bytes()[-4000:].decode(errors="replace")
            raise Invalid(f"Extractor tool preparation failed ({process.returncode}): {tail}")

    def prepare(self, capture, stage):
        if not self.engine.enable_profile_builds:
            raise Invalid("Enable the reviewed profile runner with --enable-profile-builds to build Shipwright")
        missing = self.availability()["missing_tools"]
        if missing:
            raise Invalid("Profile runner needs: " + ", ".join(missing))
        with self.lock:
            self.capture_dependencies(stage)
            binary = self.root / "build/soh-torch"
            record_file = self.root / "extractor.json"
            if record_file.is_file() and binary.is_file():
                record = json.loads(record_file.read_text())
                if record.get("binary_sha256") == checksum(binary) and record.get("source_commit") == COMMIT:
                    return self.root / "source", binary
            stage("building_extractor", "Building the reviewed headless extraction tool from preserved sources")
            source_zip = Path(capture["root"]) / capture["source"]["file"]
            if checksum(source_zip) != capture["source"]["sha256"]:
                raise Invalid("Preserved Shipwright source checksum changed")
            source = self.root / "source"
            self._unpack(source_zip, source)
            for name in ("torch", "libultraship"):
                self._unpack(self.root / (name + ".zip"), source / name)
            for name in ("spdlog", "yaml-cpp", "tinyxml2", "zlib"):
                self._unpack(self.root / (name + ".zip"), self.root / "dependency-sources" / name)
            patch = Path(__file__).resolve().parent.parent / "tools/patches/soh-torch-cli-read-rom.patch"
            if checksum(patch) != PATCH_SHA256:
                raise Invalid("Reviewed extractor patch checksum changed")
            self._run(["patch", "-p1", "--batch", "-i", str(patch)], self.root / "patch.log", source)
            arguments = ["cmake", "-S", str(source), "-B", str(self.root / "build"), "-GNinja",
                         "-DSOH_TOOLS_ONLY=ON", "-DCMAKE_BUILD_TYPE=Release",
                         "-DCMAKE_C_COMPILER_LAUNCHER=", "-DCMAKE_CXX_COMPILER_LAUNCHER=",
                         "-DFETCHCONTENT_FULLY_DISCONNECTED=ON", "-DCMAKE_DISABLE_FIND_PACKAGE_spdlog=ON"]
            for name, variable in (("spdlog", "SPDLOG"), ("yaml-cpp", "YAML-CPP"),
                                   ("tinyxml2", "TINYXML2"), ("zlib", "ZLIB")):
                arguments.append(f"-DFETCHCONTENT_SOURCE_DIR_{variable}={self.root / 'dependency-sources' / name}")
            self._run(arguments, self.root / "configure.log")
            self._run(["cmake", "--build", str(self.root / "build"), "--target", "soh-torch", "-j", "4"], self.root / "compile.log")
            record = {"source_commit": COMMIT, "version": VERSION, "binary_sha256": checksum(binary),
                      "patch_sha256": PATCH_SHA256, "dependencies": json.loads((self.root / "dependencies.json").read_text()),
                      "created_at": now(), "dependency_network_during_build": False,
                      "toolchain": "Host CMake/Ninja/C++ compiler and system development libraries; not archived automatically"}
            record_file.write_text(json.dumps(record, indent=2))
            return source, binary

    def _runtime(self, capture, target):
        expected = self.helper.RUNTIMES[target]
        entries = [a for a in capture["assets"] if a["name"] == expected["name"]]
        if len(entries) != 1:
            raise Invalid(f"Preserved release lacks the verified {target} runtime")
        path = Path(capture["root"]) / entries[0]["file"]
        self.helper.verify_runtime(path, target)
        return path

    def build(self, project, capture, job, workspace, stage):
        from .profiles import ensure_release
        ensure_release("shipwright", capture["version"], capture["commit"])
        source, binary = self.prepare(capture, stage)
        origin = project.get("rom_source")
        if not origin:
            raise Invalid("Choose a compatible ROM before testing this worker")
        stage("downloading_rom", "Verifying the selected ROM in the local cache")
        if origin.get("kind") == "local":
            from .safety import confined
            path = confined(self.engine.input_root, origin["path"], must_exist=True)
            data, record = self.helper.select_rom(path, self.helper.load_supported(source), origin.get("member"))
            if record["sha1"] != origin["sha1"]:
                raise Invalid("Selected ROM revision changed")
            normalized = workspace / "input.z64"
            normalized.write_bytes(data)
        else:
            key = origin.get("cache_key")
            try:
                record = self.engine.romm.selected_input(key, origin.get("member"), origin["sha1"])
            except (Invalid, KeyError, OSError):
                inspection = self.engine.romm.inspect_rom(origin["rom_id"], origin["filename"], self.helper.load_supported(source))
                record = self.engine.romm.selected_input(inspection["cache_key"], origin.get("member"), origin["sha1"])
                updated = {**origin, "cache_key": inspection["cache_key"]}
                self.engine.store.update("projects", project["id"], rom_source=updated)
            normalized = Path(record["path"])
        digest = checksum(normalized)
        asset_dir = self.root / "game-assets" / digest
        with self.lock:
            asset_dir.mkdir(parents=True, exist_ok=True)
            output_name = self.helper.output_for_hash(source, origin["sha1"])
            asset = asset_dir / output_name
            metadata = asset_dir / "asset.json"
            valid = (asset.is_file() and metadata.is_file() and
                     checksum(asset) == json.loads(metadata.read_text()).get("sha256"))
            if not valid:
                stage("extracting", "Extracting game assets from the verified ROM")
                self._run([str(binary), "--src", str(source / "soh/assets/yml"), "--dest", str(asset_dir),
                           "--version", VERSION, str(normalized)], asset_dir / "extract.log")
                self.helper.validate_game_assets(asset)
                metadata.write_text(json.dumps({"sha256": checksum(asset), "rom_sha256": digest,
                                                "rom_sha1": origin["sha1"], "created_at": now()}))
            else:
                stage("extracting", "Reusing previously validated extracted assets")
        target = job["target"]
        stage("packaging_" + target, "Assembling the verified " + target + " release package")
        folder = workspace / "package"
        folder.mkdir()
        runtime = self._runtime(capture, target)
        if target == "linux":
            shutil.copyfile(runtime, folder / self.helper.RUNTIMES[target]["name"])
            (folder / self.helper.RUNTIMES[target]["name"]).chmod(0o755)
            (folder / "launch.sh").write_text(self.helper.LINUX_LAUNCHER)
            (folder / "launch.sh").chmod(0o755)
        else:
            self.helper.extract_runtime(runtime, folder)
            (folder / "launch.bat").write_bytes(b'@echo off\r\ncd /d "%~dp0"\r\nset "SHIP_HOME=%CD%"\r\nsoh.exe %*\r\n')
        shutil.copyfile(asset, folder / output_name)
        base = {"format": 1, "project": project["name"], "version": VERSION,
                "created_at": now(), "source": {"commit": COMMIT},
                "rom": {"sha1": origin["sha1"], "sha256": digest, "member": origin.get("member")},
                "extractor": json.loads((self.root / "extractor.json").read_text())}
        self.helper.write_package_metadata(folder, base, target)
        return folder, {"rom_sha1": origin["sha1"], "rom_sha256": digest, "asset_sha256": checksum(asset),
                        "runtime_sha256": checksum(runtime), "extractor_sha256": checksum(binary)}
