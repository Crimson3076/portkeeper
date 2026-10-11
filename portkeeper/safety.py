import hashlib
import os
import re
import shutil
import stat
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


TARGETS = ("linux", "windows", "handheld")
MAX_FILE = 1024 * 1024 * 1024
MAX_EXPANDED = 2 * MAX_FILE
MAX_MEMBERS = 30000


class Invalid(ValueError):
    pass


def relative(value):
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise Invalid("Paths must be non-empty relative POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ("..", ".") for p in value.split("/")) or ":" in value:
        raise Invalid("Absolute paths and parent traversal are not allowed")
    return path


def confined(root, value, must_exist=False):
    root = Path(root).resolve()
    rel = relative(value)
    candidate = root.joinpath(*rel.parts)
    # Reject symlink components, even when the target would remain inside root.
    cursor = root
    for part in rel.parts:
        cursor = cursor / part
        if cursor.is_symlink():
            raise Invalid("Symlinks are not allowed in recipe paths")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root):
        raise Invalid("Path leaves its configured root")
    if must_exist and not candidate.is_file():
        raise Invalid(f"Missing file: {value}")
    return candidate


def checksum(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def slug(value):
    result = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value)).strip(".-")[:100]
    return result or "package"


def validate_recipe(recipe, allow_overrides=True):
    if not isinstance(recipe, dict) or recipe.get("version") != 1:
        raise Invalid("Recipe must be an object with version: 1")
    fields = {"version", "mode", "profile", "payload", "inputs", "expected_files", "notes", "steps",
              "image", "command", "network", "output_dir", "target_overrides"}
    if any(key not in fields for key in recipe):
        raise Invalid("Recipe contains unsupported fields; keep credentials in Settings")
    mode = recipe.get("mode")
    if mode not in ("capture", "assemble", "docker", "profile"):
        raise Invalid("Recipe mode must be capture, assemble, docker, or profile")
    if mode == "profile":
        if recipe.get("profile") != "shipwright" or any(recipe.get(key) for key in ("command", "image", "network", "target_overrides")):
            raise Invalid("Profile recipes use the fixed reviewed Shipwright runner; arbitrary commands and overrides are not allowed")
    if recipe.get("steps"):
        raise Invalid("Arbitrary steps are not supported; use a pinned Docker recipe for commands")
    inputs = recipe.get("inputs", [])
    if not isinstance(inputs, list) or len(inputs) > 100:
        raise Invalid("Recipe inputs must be a list of at most 100 files")
    destinations = set()
    for item in inputs:
        if not isinstance(item, dict):
            raise Invalid("Each input must be an object")
        relative(item.get("path"))
        dest = str(relative(item.get("destination")))
        if dest == "portkeeper-manifest.json" or dest in destinations:
            raise Invalid("Input destinations must be unique and must not replace the manifest")
        destinations.add(dest)
        if not isinstance(item.get("sha256"), str) or not re.fullmatch(r"[a-fA-F0-9]{64}", item["sha256"]):
            raise Invalid("Every input needs its expected SHA-256")
    payload = recipe.get("payload", {"kind": "source"})
    if not isinstance(payload, dict) or payload.get("kind") not in ("source", "release"):
        raise Invalid("Payload kind must be source or release")
    patterns = payload.get("asset_patterns", {})
    if not isinstance(patterns, dict) or any(t not in TARGETS for t in patterns):
        raise Invalid("Asset patterns must be keyed by target")
    for pattern in patterns.values():
        if not isinstance(pattern, str) or len(pattern) > 300:
            raise Invalid("Asset patterns must be regular expressions up to 300 characters")
        try:
            re.compile(pattern)
        except re.error as error:
            raise Invalid(f"Invalid asset pattern: {error}") from error
    expected = recipe.get("expected_files", [])
    if not isinstance(expected, list) or len(expected) > 100:
        raise Invalid("expected_files must be a list of paths")
    for value in expected:
        relative(value)
    if mode != "capture" and not expected:
        raise Invalid("Build recipes need at least one expected file")
    if mode == "docker":
        image = recipe.get("image", "")
        if not isinstance(image, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9._/:\-]*@sha256:[a-f0-9]{64}", image):
            raise Invalid("Docker images must be pinned with @sha256:<digest>")
        command = recipe.get("command")
        if not isinstance(command, list) or not 1 <= len(command) <= 64 or any(not isinstance(x, str) or "\x00" in x for x in command):
            raise Invalid("Docker command must be a non-empty argument array")
        if not isinstance(recipe.get("network", False), bool):
            raise Invalid("network must be true or false")
        relative(recipe.get("output_dir", "out"))
    overrides = recipe.get("target_overrides", {})
    if not isinstance(overrides, dict) or any(target not in TARGETS for target in overrides):
        raise Invalid("target_overrides must be keyed by target")
    allowed = {"mode", "image", "command", "output_dir", "network", "expected_files", "inputs", "payload", "notes"}
    if overrides and not allow_overrides:
        raise Invalid("Nested target_overrides are not supported")
    for override in overrides.values():
        if not isinstance(override, dict) or any(key not in allowed for key in override):
            raise Invalid("Target overrides contain unsupported fields")
        effective = {**recipe, **override}
        effective.pop("target_overrides", None)
        if mode == "capture" and effective["mode"] != "capture":
            raise Invalid("Use an assemble or docker base recipe for build target overrides")
        if mode != "capture" and effective["mode"] == "capture":
            raise Invalid("Build target overrides must use assemble or docker mode")
        validate_recipe(effective, allow_overrides=False)
    return recipe


def target_recipe(recipe, target):
    validate_recipe(recipe)
    effective = {**recipe, **recipe.get("target_overrides", {}).get(target, {})}
    effective.pop("target_overrides", None)
    return validate_recipe(effective)


def safe_extract(archive, destination):
    """Extract only ordinary files/directories; reject traversal, links and bombs."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    total = 0
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as bundle:
            members = bundle.infolist()
            if len(members) > MAX_MEMBERS:
                raise Invalid("Archive contains too many members")
            for item in members:
                name = item.filename.rstrip("/")
                if not name:
                    continue
                output = confined(destination, name)
                mode = item.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode))):
                    raise Invalid("Archive contains a symlink or special file")
                total += item.file_size
                if item.file_size > MAX_FILE or total > MAX_EXPANDED:
                    raise Invalid("Expanded archive exceeds size limit")
                if item.is_dir():
                    output.mkdir(parents=True, exist_ok=True)
                else:
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.open(item) as src, output.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    output.chmod(0o755 if mode & 0o111 else 0o644)
    elif tarfile.is_tarfile(archive):
        with tarfile.open(archive) as bundle:
            count = 0
            for item in bundle:
                count += 1
                if count > MAX_MEMBERS:
                    raise Invalid("Archive contains too many members")
                name = item.name.rstrip("/")
                if name in ("", "."):
                    continue
                if name.startswith("./"):
                    name = name[2:]
                output = confined(destination, name)
                total += item.size
                if item.size > MAX_FILE or total > MAX_EXPANDED:
                    raise Invalid("Expanded archive exceeds size limit")
                if item.isdir():
                    output.mkdir(parents=True, exist_ok=True)
                elif item.isfile():
                    output.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.extractfile(item) as src, output.open("wb") as dst:
                        shutil.copyfileobj(src, dst)
                    output.chmod(0o755 if item.mode & 0o111 else 0o644)
                else:
                    raise Invalid("Archive contains a link or special file")
    else:
        raise Invalid("Payload is not a supported ZIP or TAR archive")


def verify_tree(root):
    root = Path(root)
    total = 0
    count = 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                raise Invalid("Output contains symlinks or special files")
            count += 1
            total += path.stat().st_size if path.is_file() else 0
            if count > MAX_MEMBERS or total > MAX_EXPANDED:
                raise Invalid("Build output exceeds size limit")
