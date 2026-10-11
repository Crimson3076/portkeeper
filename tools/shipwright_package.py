#!/usr/bin/env python3
"""Build local ROM assets and package official Shipwright 9.3.0 runtimes.

This standalone helper uses only Python's standard library and never accesses
the network. It does execute the explicitly supplied local extractor.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath


VERSION = "9.3.0"
SOURCE_COMMIT = "ecd889c2019b87e78b0a3d942a6a3fe98b7efaed"
RELEASE_URL = "https://github.com/HarbourMasters/Shipwright/releases/tag/9.3.0"
RUNTIMES = {
    "linux": {
        "name": "SoH-Dewey-Alpha.appimage",
        "sha256": "8507bb70413e130e452ebaf5f156bfd78013ee0144364b7968680e8cf064a0fa",
    },
    "windows": {
        "name": "SoH-Dewey-Alpha-Win64.zip",
        "sha256": "bf46cfc41be17f3b63901db46dac46ac2a63ed3851aa13e3ba6c9ec5d0ac2632",
    },
}
MAX_ROM = 128 * 1024 * 1024
MAX_MEMBER = 512 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024
MAX_MEMBERS = 50000
ROM_EXTENSIONS = {".z64", ".v64", ".n64"}


class PackageError(ValueError):
    pass


def digest(path, algorithm="sha256"):
    result = hashlib.new(algorithm)
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def normalize_rom(data):
    """Return canonical big-endian bytes, rejecting unidentified ROM headers."""
    if len(data) < 4 or len(data) % 4 or len(data) > MAX_ROM:
        raise PackageError("ROM size must be a multiple of four bytes and at most 128 MiB")
    header = data[:4]
    if header == b"\x80\x37\x12\x40":
        return data
    result = bytearray(data)
    if header == b"\x37\x80\x40\x12":
        result[0::2], result[1::2] = result[1::2], result[0::2]
    elif header == b"\x40\x12\x37\x80":
        result[0::4], result[1::4], result[2::4], result[3::4] = (
            result[3::4], result[2::4], result[1::4], result[0::4]
        )
    else:
        raise PackageError("Unrecognized N64 ROM header")
    return bytes(result)


def safe_zip_members(bundle, allow_duplicates=False):
    """Validate the entire archive before reading or extracting any member."""
    members = bundle.infolist()
    if len(members) > MAX_MEMBERS:
        raise PackageError("ZIP has too many entries")
    seen = set()
    total = 0
    for member in members:
        name = member.filename
        path = PurePosixPath(name)
        if (not name or "\\" in name or any(ord(character) < 32 for character in name) or ":" in name
                or path.is_absolute() or any(part in {"", ".", ".."} for part in name.rstrip("/").split("/"))):
            raise PackageError(f"Unsafe ZIP entry: {name!r}")
        canonical = name.rstrip("/").casefold()
        if not canonical or (canonical in seen and not allow_duplicates):
            raise PackageError(f"Duplicate ZIP entry: {name!r}")
        seen.add(canonical)
        mode = member.external_attr >> 16
        kind = stat.S_IFMT(mode)
        if kind not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise PackageError(f"ZIP entry is a link or special file: {name!r}")
        if member.flag_bits & 1:
            raise PackageError("Encrypted ZIP entries are not supported")
        total += member.file_size
        if member.file_size > MAX_MEMBER or total > MAX_EXPANDED:
            raise PackageError("ZIP exceeds the allowed expanded size")
    # Reject file/directory collisions before extraction, including Windows case folding.
    files = {m.filename.rstrip("/").casefold() for m in members if not m.is_dir()}
    for member in members:
        parts = member.filename.rstrip("/").casefold().split("/")
        if any("/".join(parts[:i]) in files for i in range(1, len(parts))):
            raise PackageError(f"ZIP entry has a file as its parent: {member.filename!r}")
    return members


def read_bounded(handle, maximum):
    data = handle.read(maximum + 1)
    if len(data) > maximum:
        raise PackageError("ROM exceeds the allowed size")
    return data


def load_supported(source):
    source = Path(source)
    cmake = (source / "CMakeLists.txt").read_text(encoding="utf-8")
    if not re.search(r"project\(Ship\s+VERSION\s+9\.3\.0\b", cmake):
        raise PackageError("This helper requires the Shipwright 9.3.0 source")
    records = json.loads((source / "docs/supportedHashes.json").read_text(encoding="utf-8"))
    supported = {}
    for record in records:
        if not re.fullmatch(r"[0-9a-f]{40}", record.get("sha1", "")):
            raise PackageError("Invalid supported ROM hash in source")
        supported[record["sha1"]] = str(record["name"])
    return supported


def output_for_hash(source, sha1):
    """Read the narrowly defined hash blocks in the pinned Torch YAML config."""
    asset_dir = Path(source) / "soh/assets/yml"
    config = (asset_dir / "config.yml").read_text(encoding="utf-8")
    block = re.search(r"^" + re.escape(sha1) + r":\s*\n(.*?)(?=^[0-9a-f]{40}:|\Z)", config, re.M | re.S)
    if not block:
        raise PackageError(f"ROM {sha1} is listed as supported but has no extractor config")
    path_match = re.search(r"^  path: ([A-Za-z0-9_-]+)\s*$", block[1], re.M)
    output_match = re.search(r"^      binary: (oot(?:-mq)?\.o2r)\s*$", block[1], re.M)
    if not path_match or not output_match or not (asset_dir / path_match[1]).is_dir():
        raise PackageError("Extractor config has an invalid path or archive output")
    return output_match[1]


def select_rom(path, supported, member_name=None):
    """Select exactly one supported dump; archive ambiguity requires a selector."""
    path = Path(path)
    if path.suffix.lower() != ".zip":
        if member_name:
            raise PackageError("--rom-member is only valid when --rom is a ZIP")
        if path.suffix.lower() not in ROM_EXTENSIONS:
            raise PackageError("--rom must be a .z64, .v64, .n64, or .zip file")
        with path.open("rb") as handle:
            data = normalize_rom(read_bounded(handle, MAX_ROM))
        sha1 = hashlib.sha1(data).hexdigest()
        if sha1 not in supported:
            raise PackageError(f"Unsupported normalized ROM SHA1: {sha1}")
        return data, {"member": None, "sha1": sha1, "sha256": hashlib.sha256(data).hexdigest(),
                      "revision": supported[sha1], "size": len(data)}
    with zipfile.ZipFile(path) as bundle:
        members = safe_zip_members(bundle)
        candidates = [m for m in members if not m.is_dir() and Path(m.filename).suffix.lower() in ROM_EXTENSIONS]
        if member_name:
            candidates = [m for m in candidates if m.filename == member_name]
            if not candidates:
                raise PackageError("--rom-member does not name an N64 ROM entry in the ZIP")
        matches = []
        reports = []
        for member in candidates:
            if member.file_size > MAX_ROM:
                reports.append(f"{member.filename}: too large")
                continue
            try:
                with bundle.open(member) as handle:
                    data = normalize_rom(read_bounded(handle, MAX_ROM))
                sha1 = hashlib.sha1(data).hexdigest()
                if sha1 in supported:
                    matches.append((data, {"member": member.filename, "sha1": sha1,
                                           "sha256": hashlib.sha256(data).hexdigest(),
                                           "revision": supported[sha1], "size": len(data)}))
                else:
                    reports.append(f"{member.filename}: unsupported SHA1 {sha1}")
            except PackageError as error:
                reports.append(f"{member.filename}: {error}")
        if not matches:
            detail = "\n".join(reports) or "No .z64, .v64, or .n64 entries found"
            raise PackageError("No supported ROM found in ZIP:\n" + detail)
        if len(matches) > 1:
            choices = "\n".join(f"  {item[1]['member']} ({item[1]['revision']})" for item in matches)
            raise PackageError("Multiple supported ROMs found. Choose one with --rom-member:\n" + choices)
        return matches[0]


def verify_runtime(path, target):
    actual = digest(path)
    if actual != RUNTIMES[target]["sha256"]:
        raise PackageError(f"{target} runtime SHA256 mismatch: {actual}")
    return actual


def validate_game_assets(path):
    with zipfile.ZipFile(path) as bundle:
        # Torch emits duplicate resource names in this release. O2R remains an opaque
        # payload: never unpack it. Read each physical entry so duplicate CRCs are
        # checked too (ZipFile.testzip() resolves repeated names to the final entry).
        members = safe_zip_members(bundle, allow_duplicates=True)
        for member in members:
            with bundle.open(member) as handle:
                while handle.read(1024 * 1024):
                    pass
        versions = [member for member in members if member.filename == "portVersion"]
        if len(versions) != 1:
            raise PackageError("Generated game archive must contain exactly one portVersion")
        version = bundle.read(versions[0])
        if version != b"\x01\x00\x09\x00\x03\x00\x00":
            raise PackageError("Generated game archive is not marked for Shipwright 9.3.0")


def extract_runtime(archive, destination):
    with zipfile.ZipFile(archive) as bundle:
        members = safe_zip_members(bundle)
        for member in members:
            target = destination.joinpath(*PurePosixPath(member.filename).parts)
            if member.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(member) as incoming, target.open("xb") as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
            target.chmod(0o755 if (member.external_attr >> 16) & 0o111 else 0o644)


LINUX_LAUNCHER = """#!/bin/sh
set -eu
package_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
cd "$package_dir"
chmod u+x SoH-Dewey-Alpha.appimage
export SHIP_HOME="$package_dir"
exec ./SoH-Dewey-Alpha.appimage "$@"
"""


def write_package_metadata(folder, base_manifest, target):
    instructions = (
        "Ship of Harkinian 9.3.0 — local package\n\n"
        "This contains an official prebuilt runtime and game assets extracted from your\n"
        "locally supplied ROM using a locally compiled soh-torch extractor. The original\n"
        "ROM is not included. Keep this package private.\n\n"
    )
    if target == "linux":
        instructions += ("Extract the whole ZIP. In this folder, run: chmod +x launch.sh && ./launch.sh\n"
                         "The launcher sets SHIP_HOME so game assets, saves and settings live here.\n"
                         "The x86-64 AppImage requires a compatible Linux desktop and FUSE support.\n"
                         "If FUSE is unavailable, try: APPIMAGE_EXTRACT_AND_RUN=1 ./launch.sh\n")
    else:
        instructions += ("Extract the whole ZIP, then run launch.bat. Keep the extracted files together.\n"
                         "This is the official x86-64 Windows runtime. Saves and settings live here.\n")
    instructions += "\nPackaging and checksums are validated; gameplay has not been tested by this helper.\n"
    (folder / "README-Portkeeper.txt").write_text(instructions, encoding="utf-8")
    files = {}
    for path in sorted(folder.rglob("*")):
        if path.is_symlink():
            raise PackageError("Package contains an unexpected symlink")
        if path.is_file():
            files[path.relative_to(folder).as_posix()] = {"sha256": digest(path), "bytes": path.stat().st_size}
    manifest = {**base_manifest, "target": target, "runtime": {
        **RUNTIMES[target], "kind": "official prebuilt release asset", "release_url": RELEASE_URL,
    }, "files": files, "validation": {"packaging": "checked", "gameplay": "not tested"}}
    (folder / "portkeeper-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")


def write_zip(folder, destination):
    """Use Unix mode metadata so AppImage/launch.sh retain executable permissions."""
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(folder.rglob("*")):
            if path.is_symlink():
                raise PackageError("Package contains an unexpected symlink")
            if not path.is_file():
                continue
            relative = path.relative_to(folder.parent).as_posix()
            info = zipfile.ZipInfo(relative, (2026, 10, 6, 0, 0, 0))
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | (0o755 if path.stat().st_mode & 0o111 else 0o644)) << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            with path.open("rb") as incoming, bundle.open(info, "w") as outgoing:
                shutil.copyfileobj(incoming, outgoing, 1024 * 1024)
    with zipfile.ZipFile(destination) as bundle:
        if bundle.testzip():
            raise PackageError("Final package failed its ZIP CRC check")


def package(args):
    source = Path(args.shipwright_source).resolve()
    extractor = Path(args.extractor).resolve()
    output_dir = Path(args.output_dir).resolve()
    if not extractor.is_file() or not os.access(extractor, os.X_OK):
        raise PackageError("--extractor must point to an executable local soh-torch")
    supported = load_supported(source)
    rom_bytes, rom_record = select_rom(args.rom, supported, args.rom_member)
    asset_name = output_for_hash(source, rom_record["sha1"])
    runtimes = {"linux": Path(args.linux_appimage).resolve()}
    if args.windows_zip:
        runtimes["windows"] = Path(args.windows_zip).resolve()
    for target, path in runtimes.items():
        verify_runtime(path, target)
    output_dir.mkdir(parents=True, exist_ok=True)
    names = {target: f"ship-of-harkinian-{VERSION}-{rom_record['sha1'][:8]}-{target}.zip" for target in runtimes}
    for name in names.values():
        if (output_dir / name).exists() and not args.overwrite:
            raise PackageError(f"Output already exists: {name}; use --overwrite to replace it")
    patch_record = None
    if args.extractor_patch:
        patch_path = Path(args.extractor_patch)
        patch_record = {"name": patch_path.name, "sha256": digest(patch_path)}
    base_manifest = {
        "format": 1, "project": "Ship of Harkinian", "version": VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source": {"repository": "https://github.com/HarbourMasters/Shipwright",
                   "commit": args.source_commit, "version": VERSION,
                   "supported_hashes_sha256": digest(source / "docs/supportedHashes.json"),
                   "asset_config_sha256": digest(source / "soh/assets/yml/config.yml")},
        "extractor": {"kind": "locally compiled soh-torch", "sha256": digest(extractor), "patch": patch_record},
        "rom": rom_record,
    }
    with tempfile.TemporaryDirectory(prefix=".shipwright-build-", dir=output_dir) as temporary:
        scratch = Path(temporary)
        normalized_rom = scratch / "input.z64"
        normalized_rom.write_bytes(rom_bytes)
        del rom_bytes
        extracted = scratch / "extracted"
        extracted.mkdir()
        command = [str(extractor), "--src", str(source / "soh/assets/yml"), "--dest", str(extracted),
                   "--version", VERSION, str(normalized_rom)]
        with (scratch / "extractor.log").open("wb") as log:
            result = subprocess.run(command, stdout=log, stderr=subprocess.STDOUT, timeout=args.timeout,
                                    cwd=scratch, check=False)
        if result.returncode:
            log_tail = (scratch / "extractor.log").read_bytes()[-8192:].decode("utf-8", errors="replace")
            raise PackageError(f"Extractor failed with status {result.returncode}:\n{log_tail}")
        asset = extracted / asset_name
        if asset.is_symlink() or not asset.is_file() or not asset.stat().st_size:
            raise PackageError(f"Extractor did not produce a regular, nonempty {asset_name}")
        if asset.stat().st_size > MAX_MEMBER:
            raise PackageError("Extracted game archive exceeds the allowed size")
        validate_game_assets(asset)
        pending = []
        for target, runtime in runtimes.items():
            folder = scratch / f"ship-of-harkinian-{VERSION}-{target}"
            folder.mkdir()
            game_folder = folder
            if target == "linux":
                appimage = folder / RUNTIMES[target]["name"]
                shutil.copyfile(runtime, appimage)
                appimage.chmod(0o755)
                (folder / "launch.sh").write_text(LINUX_LAUNCHER, encoding="utf-8")
                (folder / "launch.sh").chmod(0o755)
            else:
                extract_runtime(runtime, folder)
                executables = [p for p in folder.rglob("*") if p.is_file() and p.name.casefold() == "soh.exe"]
                if len(executables) != 1:
                    raise PackageError("Windows runtime must contain exactly one soh.exe")
                executable = executables[0]
                game_folder = executable.parent
                if not (game_folder / "soh.o2r").is_file():
                    raise PackageError("Windows runtime is missing soh.o2r beside soh.exe")
                relative = executable.relative_to(folder).as_posix().replace("/", "\\")
                working = executable.parent.relative_to(folder).as_posix().replace("/", "\\")
                if working == ".":
                    working = ""
                launcher = ("@echo off\r\nsetlocal\r\ncd /d \"%~dp0" + working + "\"\r\n"
                            "set \"SHIP_HOME=%CD%\"\r\n\"%~dp0" + relative + "\" %*\r\n")
                (folder / "launch.bat").write_bytes(launcher.encode("utf-8"))
            if (game_folder / asset_name).exists():
                raise PackageError("Official runtime unexpectedly already contains game assets")
            shutil.copyfile(asset, game_folder / asset_name)
            write_package_metadata(folder, base_manifest, target)
            staged = scratch / names[target]
            write_zip(folder, staged)
            pending.append((staged, output_dir / names[target]))
        # Commit only after all requested target packages have been created and checked.
        for staged, final in pending:
            os.replace(staged, final)
        return [{"path": str(final), "sha256": digest(final), "bytes": final.stat().st_size} for _, final in pending]


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--rom", required=True, help="Your local .z64/.v64/.n64 or ZIP")
    result.add_argument("--rom-member", help="Exact ZIP member name; required for multiple supported dumps")
    result.add_argument("--shipwright-source", required=True, help="Local Shipwright 9.3.0 source directory")
    result.add_argument("--extractor", required=True, help="Locally compiled soh-torch executable")
    result.add_argument("--linux-appimage", required=True, help="Official Dewey Alpha x86-64 AppImage")
    result.add_argument("--windows-zip", help="Optional official Dewey Alpha Win64 ZIP")
    result.add_argument("--output-dir", required=True, help="Directory for final packages and temporary build files")
    result.add_argument("--extractor-patch", help="Local patch file to record in the provenance manifest")
    result.add_argument("--source-commit", default=SOURCE_COMMIT, help="Source commit to record; caller verifies checkout")
    result.add_argument("--timeout", type=int, default=1800, help="Extractor timeout in seconds (default: 1800)")
    result.add_argument("--overwrite", action="store_true", help="Atomically replace an existing package")
    return result


def main():
    args = parser().parse_args()
    if args.timeout <= 0:
        parser().error("--timeout must be positive")
    try:
        packages = package(args)
    except (PackageError, OSError, zipfile.BadZipFile, json.JSONDecodeError, subprocess.TimeoutExpired) as error:
        print(f"Packaging failed: {error}", file=sys.stderr)
        return 1
    print(json.dumps({"packages": packages}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
