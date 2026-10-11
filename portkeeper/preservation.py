"""Export verifiable source bundles without claiming unproven offline rebuilds.

Materials are supplied by trusted worker profiles, not executable instructions
from upstream README files. This module copies files and inspects declarations;
it never fetches dependencies, invokes a compiler, or includes project inputs.
"""

import configparser
import hashlib
import json
import os
import re
import stat
import tarfile
import zipfile
from pathlib import Path

from .safety import Invalid, MAX_EXPANDED, MAX_FILE, MAX_MEMBERS, checksum, confined, relative, slug
from .storage import identifier, now


MANIFEST = "preservation-manifest.json"
_SECRET_FIELDS = {"token", "api_key", "api_token", "password", "authorization", "secret", "credentials"}
_SOURCE_NAMES = {".gitmodules", "CMakeLists.txt", "vcpkg.json"}


def _recipe_copy(value, key=None):
    if key and (key.lower() in _SECRET_FIELDS or key.lower().endswith(("_token", "_password", "_secret", "_api_key"))):
        return "[omitted]"
    if key == "inputs" and isinstance(value, list):
        return [{"destination": item.get("destination"), "sha256": item.get("sha256"),
                 "requires_user_owned_input": True} for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        return {k: _recipe_copy(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [_recipe_copy(v) for v in value]
    return value


def _text_name(name):
    path = Path(name)
    return path.name in _SOURCE_NAMES or path.suffix == ".cmake"


def _declarations(name, text):
    references = []
    if Path(name).name == ".gitmodules":
        parser = configparser.ConfigParser(interpolation=None)
        try:
            parser.read_string(text)
            for section in parser.sections():
                if not section.startswith("submodule "):
                    continue
                path = parser.get(section, "path", fallback="")
                if path:
                    references.append({"label": f"submodule:{path}", "kind": "submodule", "path": path,
                                       "url": parser.get(section, "url", fallback=""), "declared_in": name})
        except configparser.Error:
            references.append({"label": "unparsed:gitmodules", "kind": "unknown", "declared_in": name})
    if Path(name).name == "CMakeLists.txt" or Path(name).suffix == ".cmake":
        # This is a conservative declaration inventory, not a CMake interpreter.
        # Variable URLs remain visible and prevent an unconditional closure claim.
        without_comments = re.sub(r"(?m)#.*$", "", text)
        for match in re.finditer(r"(?:FetchContent_Declare|ExternalProject_Add)\s*\(\s*([A-Za-z0-9_+.-]+)\s+([^)]*)\)",
                                 without_comments, re.I | re.S):
            body = match.group(2)
            if not re.search(r"\b(?:GIT_REPOSITORY|URL)\b", body, re.I):
                continue
            url = re.search(r"\b(?:GIT_REPOSITORY|URL)\s+([^\s]+)", body, re.I)
            revision = re.search(r"\b(?:GIT_TAG|URL_HASH)\s+([^\s]+)", body, re.I)
            references.append({"label": f"dependency:{match.group(1).lower()}", "kind": "dependency",
                               "url": url.group(1).strip('"') if url else "",
                               "revision": revision.group(1).strip('"') if revision else None,
                               "declared_in": name})
    if Path(name).name == "vcpkg.json":
        try:
            manifest = json.loads(text)
            for dependency in manifest.get("dependencies", []):
                dep_name = dependency if isinstance(dependency, str) else dependency.get("name")
                if dep_name:
                    references.append({"label": f"dependency:{dep_name.lower()}", "kind": "dependency",
                                       "declared_in": name, "package_manager": "vcpkg"})
        except (ValueError, AttributeError, TypeError):
            references.append({"label": "unparsed:vcpkg", "kind": "unknown", "declared_in": name})
    return references


def _inspection(path):
    """Inspect bounded source declarations in a source tree or archived source."""
    references = []
    budget = 8 * 1024 * 1024
    truncated = False

    def consume(name, size, read):
        nonlocal budget, truncated
        if not _text_name(name):
            return
        if size > 512 * 1024 or size > budget:
            truncated = True
            return
        text = read().decode("utf-8", errors="replace")
        budget -= size
        references.extend(_declarations(name, text))

    if path.is_dir():
        for directory, dirs, files in os.walk(path, followlinks=False):
            dirs[:] = sorted(d for d in dirs if d not in (".git", ".cache", "__pycache__"))
            for name in sorted(files):
                item = Path(directory) / name
                if item.is_file() and not item.is_symlink():
                    consume(item.relative_to(path).as_posix(), item.stat().st_size, item.read_bytes)
    elif zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            if len(archive.infolist()) > MAX_MEMBERS:
                raise Invalid("Source inspection archive contains too many members")
            for item in archive.infolist():
                if not item.is_dir():
                    consume(item.filename, item.file_size, lambda item=item: archive.read(item))
    elif tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:
            for index, item in enumerate(archive):
                if index >= MAX_MEMBERS:
                    raise Invalid("Source inspection archive contains too many members")
                if item.isfile():
                    consume(item.name, item.size, lambda item=item: archive.extractfile(item).read())
    return references, truncated


class Preservation:
    def __init__(self, engine):
        self.engine = engine
        self.store = engine.store

    def export(self, project_id, materials=None, required_materials=None, toolchain=None,
               capture_id=None, offline_build=None, dependency_scope=None):
        """Create a source export with a factual closure report.

        materials maps reviewed labels to paths, or {path, revision} descriptors.
        dependency_scope may select declared labels required by a tested profile
        (for example an extractor-only build); omitted declarations stay visible
        and the report describes that scope instead of the complete application.
        """
        materials = materials or {}
        required_materials = required_materials or []
        if not isinstance(materials, dict) or not isinstance(required_materials, list):
            raise Invalid("Preservation materials must be a mapping and requirements must be a list")
        if any(not isinstance(label, str) or not label or len(label) > 200 for label in materials):
            raise Invalid("Preservation material labels must be non-empty text")
        if any(not isinstance(label, str) or not label for label in required_materials):
            raise Invalid("Preservation requirements must be material labels")
        if dependency_scope is not None and (not isinstance(dependency_scope, dict)
                                             or not isinstance(dependency_scope.get("name"), str)
                                             or not isinstance(dependency_scope.get("labels"), list)
                                             or any(not isinstance(label, str) for label in dependency_scope["labels"])):
            raise Invalid("Dependency scope needs a name and declared labels")
        with self.engine.lock:
            project = self.store.get("projects", project_id)
            capture = self.store.get("captures", capture_id or project.get("capture_id"))
            if capture["project_id"] != project_id:
                raise Invalid("Capture belongs to another project")
            expected_root = confined(self.engine.archive_root, f"sources/{project_id}/{capture['id']}")
            if Path(capture["root"]) != expected_root:
                raise Invalid("Capture path leaves its source archive")
            capture_files = [capture["source"], *capture.get("assets", [])]
            collected = []
            declarations = []
            truncated = False
            total = 0
            seen = set()

            def collect(path, dest):
                nonlocal total
                relative(dest)
                if dest in seen:
                    raise Invalid("Preservation bundle has duplicate paths")
                if path.is_symlink() or not path.is_file():
                    raise Invalid("Preservation materials must be ordinary files without symlinks")
                size = path.stat().st_size
                total += size
                if size > MAX_FILE or total > MAX_EXPANDED or len(collected) >= MAX_MEMBERS:
                    raise Invalid("Preservation bundle exceeds its size limit")
                seen.add(dest)
                collected.append({"path": dest, "sha256": checksum(path), "size": size, "source": path})

            for item in capture_files:
                path = confined(expected_root, item["file"], must_exist=True)
                if checksum(path) != item.get("sha256"):
                    raise Invalid("Archived capture checksum changed")
                collect(path, f"capture/{item['file']}")
            source_path = confined(expected_root, capture["source"]["file"], must_exist=True)
            refs, partial = _inspection(source_path)
            declarations.extend(refs)
            truncated |= partial
            material_records = {}
            used_prefixes = set()
            for label, descriptor in sorted(materials.items()):
                descriptor = {"path": descriptor} if isinstance(descriptor, (str, Path)) else descriptor
                if not isinstance(descriptor, dict) or "path" not in descriptor:
                    raise Invalid("Each preservation material needs a reviewed path")
                path = Path(descriptor["path"])
                if path.is_symlink() or not path.exists():
                    raise Invalid(f"Missing or linked preservation material: {label}")
                prefix = slug(label)
                if prefix in used_prefixes:
                    raise Invalid("Preservation material labels collide after normalization")
                used_prefixes.add(prefix)
                base = f"materials/{prefix}"
                record = {"label": label, "path": base, "revision": descriptor.get("revision"), "files": 0}
                before = len(collected)
                if path.is_dir():
                    for directory, dirs, files in os.walk(path, followlinks=False):
                        dirs[:] = sorted(d for d in dirs if d not in (".git", ".cache", "__pycache__"))
                        for name in dirs:
                            if (Path(directory) / name).is_symlink():
                                raise Invalid("Preservation material trees must not contain symlinks")
                        for name in sorted(files):
                            if name == ".git" or name.endswith((".pyc", ".pyo")):
                                continue
                            item = Path(directory) / name
                            if not stat.S_ISREG(item.lstat().st_mode):
                                raise Invalid("Preservation material trees contain a link or special file")
                            collect(item, f"{base}/{item.relative_to(path).as_posix()}")
                else:
                    collect(path, f"{base}/{path.name}")
                record["files"] = len(collected) - before
                if record["files"] == 0:
                    raise Invalid(f"Preservation material contains no files: {label}")
                material_records[label] = record
                refs, partial = _inspection(path)
                for ref in refs:
                    ref["declared_in"] = f"{base}/{ref['declared_in']}"
                declarations.extend(refs)
                truncated |= partial
            unique = {}
            for ref in declarations:
                unique.setdefault(ref["label"], ref)
            scope_labels = set(dependency_scope["labels"]) if dependency_scope else set(unique)
            # Requirements can include patches/binaries/licenses in addition to discovered dependencies.
            required = sorted(set(required_materials) | scope_labels)
            missing = [label for label in required if label not in material_records]
            unpinned = [label for label in required if label in material_records
                        and label.startswith(("dependency:", "submodule:")) and not material_records[label].get("revision")]
            dependency_complete = not missing and not unpinned and not truncated
            toolchain = toolchain or {}
            if not isinstance(toolchain, dict):
                raise Invalid("Toolchain descriptor must be an object")
            toolchain_complete = bool(toolchain.get("identity") and toolchain.get("material") in material_records)
            offline_build = offline_build or {}
            if not isinstance(offline_build, dict):
                raise Invalid("Offline build evidence must be an object")
            offline_verified = bool(offline_build.get("succeeded") is True
                                    and offline_build.get("network_disabled") is True
                                    and offline_build.get("log_material") in material_records
                                    and offline_build.get("command"))
            capture_complete = capture.get("complete", True) is True
            closure = {"scope": dependency_scope.get("name") if dependency_scope else "complete_source_declarations",
                       "source_preserved": True, "dependency_sources_preserved": dependency_complete,
                       "capture_complete": capture_complete,
                       "toolchain_preserved": toolchain_complete, "offline_build_verified": offline_verified,
                       "offline_rebuild_complete": capture_complete and dependency_complete and toolchain_complete and offline_verified,
                       "missing_materials": missing, "unpinned_materials": unpinned,
                       "inspection_truncated": truncated,
                       "declarations_outside_scope": sorted(set(unique) - scope_labels),
                       "limitations": ["Source declaration inspection is conservative; this report is not a dependency resolver.",
                                       "User-owned ROMs and extracted game assets are not included by this exporter."]}
            export_id = identifier()
            root = confined(self.engine.archive_root, f"preservation/{project_id}/{export_id}")
            root.mkdir(parents=True)
            filename = f"{slug(project['name'])}-{slug(capture['version'])}-preservation.zip"
            destination = root / filename
            temporary = root / f"{filename}.part"
            manifest = {"schema_version": 1, "created_at": now(), "project_id": project_id,
                        "project_name": project["name"], "repo": project["repo"], "capture_id": capture["id"],
                        "version": capture["version"], "commit": capture.get("commit"),
                        "capture": {"source": capture["source"], "assets": capture.get("assets", []),
                                    "complete": capture.get("complete", True), "release_url": capture.get("release_url")},
                        "materials": list(material_records.values()), "dependency_declarations": list(unique.values()),
                        "closure": closure, "toolchain": _recipe_copy(toolchain),
                        "offline_build": _recipe_copy(offline_build),
                        "files": [{k: v for k, v in entry.items() if k != "source"} for entry in collected]}
            recipe = json.dumps(_recipe_copy(project["recipe"]), indent=2).encode()
            manifest["files"].append({"path": "recipe.json", "sha256": hashlib.sha256(recipe).hexdigest(), "size": len(recipe)})
            try:
                with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
                    for entry in collected:
                        if checksum(entry["source"]) != entry["sha256"]:
                            raise Invalid("Preservation material changed while exporting")
                        bundle.write(entry["source"], entry["path"])
                    bundle.writestr("recipe.json", recipe)
                    bundle.writestr(MANIFEST, json.dumps(manifest, indent=2))
                os.replace(temporary, destination)
                record = {"id": export_id, "project_id": project_id, "capture_id": capture["id"],
                          "created_at": now(), "filename": filename, "file": str(destination),
                          "size": destination.stat().st_size, "sha256": checksum(destination), "manifest": manifest,
                          "download_url": f"/api/preservations/{export_id}/download"}
                self.store.put("preservations", record)
                return record
            finally:
                temporary.unlink(missing_ok=True)

    def audit(self, export_id):
        record = self.store.get("preservations", export_id)
        root = confined(self.engine.archive_root, f"preservation/{record['project_id']}/{export_id}")
        path = confined(root, record["filename"], must_exist=True)
        if Path(record["file"]) != path or checksum(path) != record["sha256"]:
            raise Invalid("Preservation archive checksum changed")
        if not zipfile.is_zipfile(path):
            raise Invalid("Preservation archive is not a valid ZIP")
        with zipfile.ZipFile(path) as bundle:
            if len(bundle.infolist()) > MAX_MEMBERS + 2:
                raise Invalid("Preservation archive has too many files")
            names = bundle.namelist()
            if len(set(names)) != len(names):
                raise Invalid("Preservation archive has duplicate paths")
            try:
                info = bundle.getinfo(MANIFEST)
            except KeyError as error:
                raise Invalid("Preservation archive is missing its manifest") from error
            if info.file_size > 8 * 1024 * 1024:
                raise Invalid("Preservation manifest exceeds size limit")
            try:
                manifest = json.loads(bundle.read(MANIFEST))
                if not isinstance(manifest, dict) or not isinstance(manifest.get("files"), list) or not isinstance(manifest.get("closure"), dict):
                    raise ValueError("Invalid manifest shape")
                if any(not isinstance(entry, dict) or not isinstance(entry.get("path"), str)
                       or not isinstance(entry.get("size"), int) or entry["size"] < 0
                       or not isinstance(entry.get("sha256"), str) or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])
                       for entry in manifest["files"]):
                    raise ValueError("Invalid manifest file")
                if len({entry["path"] for entry in manifest["files"]}) != len(manifest["files"]):
                    raise ValueError("Duplicate manifest paths")
            except (ValueError, UnicodeError, TypeError) as error:
                raise Invalid("Invalid preservation manifest") from error
            expected = {entry["path"] for entry in manifest["files"]}
            if set(names) != expected | {MANIFEST}:
                raise Invalid("Preservation archive does not match its manifest")
            total = 0
            for entry in manifest["files"]:
                relative(entry["path"])
                info = bundle.getinfo(entry["path"])
                mode = info.external_attr >> 16
                total += info.file_size
                if stat.S_ISLNK(mode) or info.is_dir() or (stat.S_IFMT(mode) and not stat.S_ISREG(mode)) or info.file_size != entry["size"] or info.file_size > MAX_FILE or total > MAX_EXPANDED:
                    raise Invalid("Invalid preservation archive member")
                digest = hashlib.sha256()
                with bundle.open(info) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(chunk)
                if digest.hexdigest() != entry["sha256"]:
                    raise Invalid("Preservation file checksum mismatch")
        return {"id": export_id, "integrity_verified": True, "files_verified": len(expected),
                "closure": manifest["closure"]}
