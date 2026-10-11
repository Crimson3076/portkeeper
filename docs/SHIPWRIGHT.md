# Ship of Harkinian worker

The reviewed `shipwright` profile packages **Shipwright 9.3.0 (Dewey Alpha)** at source commit `ecd889c2019b87e78b0a3d942a6a3fe98b7efaed`. It extracts assets from your supported Ocarina of Time dump and assembles checksum-verified official Linux x86-64 / Steam Deck and Windows x64 runtimes. It does not compile the full game runtime. See the [official release](https://github.com/HarbourMasters/Shipwright/releases/tag/9.3.0), [build instructions](https://github.com/HarbourMasters/Shipwright/blob/9.3.0/docs/BUILDING.md), and [supported ROM hashes](https://github.com/HarbourMasters/Shipwright/blob/9.3.0/docs/supportedHashes.json).

## Prepare the runner

Use a Linux build host with Python 3.11+, CMake 3.26+, Ninja, a C/C++ compiler, `patch`, `lsb_release`, and zlib development headers. Debian/Ubuntu package names are `build-essential`, `cmake`, `ninja-build`, `zlib1g-dev`, `patch`, `git`, and `lsb-release`; verify that the installed CMake is at least 3.26. Start the reviewed runner explicitly:

```bash
./start.sh \
  --data-dir ./data \
  --input-root /path/to/owned-game-files \
  --library-root /path/to/romm/ports \
  --enable-profile-builds
```

The supplied Compose service includes these tools and enables the profile. General Docker recipes stay disabled and no Docker socket is mounted. Enabling a profile permits fixed compiler/extractor commands from reviewed, pinned upstream source to execute on the build host or in that service container. A custom recipe or model cannot substitute arbitrary host commands.

## Select a game file and test

In **Settings → RomM**, save a read-only Client API Token and test the connection. The worker wizard searches your RomM library, lets you select a listed file, downloads it privately, and inspects supported revisions. ZIP files with multiple supported ROMs require a member choice. The wizard exposes Linux / Steam Deck and Windows targets for this profile.

Use **Test build** to capture and build the reviewed release. The worker:

1. Preserves the Shipwright source and selected official runtime assets.
2. Preserves pinned Torch, libultraship, spdlog, yaml-cpp, tinyxml2, and zlib source archives before compiling.
3. Verifies and applies the included `soh-torch` patch, then builds the headless extractor with CMake dependency fetching disconnected.
4. Normalizes N64 byte order, checks the selected ROM SHA1 against the supported revision list, and verifies its extraction configuration.
5. Generates `oot.o2r` or `oot-mq.o2r`, verifies physical O2R entry CRCs and the release version, and packages the official target runtime.

Extractor provenance includes source/dependency revisions, patch and binary hashes, and host-toolchain notes. Valid extractor and game-asset caches are reused. The input file remains unchanged; final packages contain extracted game assets and exclude the original ROM.

New upstream releases are preserved automatically. Their builds are blocked until the profile has been reviewed for that version and exact commit. Enabling automatic builds does not bypass this release guard.

## Local-input fallback

If RomM downloading is unavailable, place your own file beneath `--input-root`. The API accepts a relative local path, optional exact ZIP-member name, and a supported normalized-ROM SHA1. This is an API fallback; the main wizard uses RomM.

```bash
curl -X POST http://127.0.0.1:8787/api/projects \
  -H 'Content-Type: application/json' \
  -d '{
    "profile": "shipwright",
    "name": "Ship of Harkinian",
    "targets": ["linux", "windows"],
    "poll_minutes": 360,
    "auto_build": false,
    "auto_publish": false,
    "rom_source": {
      "kind": "local",
      "path": "owned/ocarina-of-time.zip",
      "member": "exact/path/inside/archive.z64",
      "sha1": "41b3bdc48d98c48529219919015a1af22f5057c2"
    }
  }'
```

That SHA1 identifies the supported US 1.2 revision. Replace the path/member and hash with the exact supported input you own. Omit `member` for an unpacked `.z64`, `.v64`, or `.n64`, or a ZIP with exactly one supported ROM. Inputs outside the configured root, symlinks, unsupported hashes, and changed revisions are rejected.

## Use and validate a package

Extract the entire Linux ZIP and run `chmod +x launch.sh && ./launch.sh`. The launcher uses the extracted folder as `SHIP_HOME`. If FUSE is unavailable, try `APPIMAGE_EXTRACT_AND_RUN=1 ./launch.sh`. Extract the Windows ZIP and run `launch.bat`. Saves/settings live in the extracted package directory; keep those outside replaceable server ZIPs and preserve them when changing versions.

Package validation is not a play test. Record a launch check or gameplay confirmation in **Versions** before selecting a known-good artifact. Known-good versions and pinned/current packages are protected from retention cleanup. Native Windows launch, full gameplay, and physical Steam Deck testing have not been established by automated package checks.

## Preservation limits

Preservation exports include source, captured runtimes, available dependency archives, the extractor patch, and extractor provenance. They exclude ROMs and generated game assets. The closure scope is headless extraction plus prebuilt-runtime assembly, not a full runtime source build. Host CMake/compiler/system libraries are not automatically archived. Optionally register or save a toolchain archive, and verify an offline rebuild before treating an export as complete.

A fresh offline rebuild of this profile passed in the supplied service image, and an audited bundle includes that image, required source/dependencies, and build proof. Its exact scope and remaining gaps are recorded in [validation](VALIDATION.md). Status recognizes a saved verified bundle only for matching capture/scope/toolchain material; ordinary exports do not automatically run or certify a rebuild. Saved bundles are downloadable from the worker overview.

## Standalone helper

`tools/shipwright_package.py` remains available for offline manual packaging with a supplied source tree, locally built `soh-torch`, your own ROM, and official runtime files:

```bash
python3 tools/shipwright_package.py \
  --rom /local/owned/ocarina-of-time.zip \
  --rom-member 'exact/path/inside/archive.z64' \
  --shipwright-source /local/Shipwright \
  --extractor /local/build/soh-torch \
  --extractor-patch tools/patches/soh-torch-cli-read-rom.patch \
  --linux-appimage /local/SoH-Dewey-Alpha.appimage \
  --windows-zip /local/SoH-Dewey-Alpha-Win64.zip \
  --output-dir /local/private/packages
```

Omit `--windows-zip` for Linux only. Existing package filenames require `--overwrite`. The helper is independent of the scheduler and never downloads ROMs or uses RomM credentials; the integrated worker handles the authenticated library flow separately.
