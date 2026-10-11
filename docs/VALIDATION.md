# Validation record

Updated October 11, 2026. This document separates implementation checks from real game/host checks; a successful package is not a gameplay certification.

## Reproduce the source checks

```bash
python3 -W error -m unittest discover -s tests -v
node --check static/app.js
docker compose config --quiet
```

GitHub CI is configured for Python 3.11 and 3.13 with warnings treated as errors, and frontend syntax checking. It uses SHA-pinned official [checkout](https://github.com/actions/checkout/releases/tag/v7.0.1), [setup-python](https://github.com/actions/setup-python/releases/tag/v7.0.0), and [setup-node](https://github.com/actions/setup-node/releases/tag/v7.1.0) actions. Configured CI has not yet been observed running on the destination repository.

The automated suite exercises capture/assembly/publication, input hashes, archive confinement, target overrides, failure preservation and retry, local HTTP handling, RomM request/configuration behavior with fixtures, profile guards, packaging, lifecycle/retention, preservation reports, and local-assistant proposal validation. Mocked external responses do not establish live interoperability.

All **97 automated tests passed**, with no skips and warnings treated as errors (`-W error`), on host Python 3.11 (11.318 seconds) and container Python 3.13.16 (11.893 seconds). The container suite ran as the image's default UID 10001 with a read-only root filesystem and networking disabled; its test tmpfs allowed executable fixtures. Frontend syntax passed `node --check static/app.js`. Workflow coverage includes tagged initial profile builds, selecting an older compatible capture while preserving the newer-release pointer, recapturing runtimes when adding a target, recognizing matching verified preservation bundles, rejecting changed registered toolchains, and private-input redaction/guards across target overrides.

The current Compose configuration passed `docker compose config --quiet`, and `start.sh` passed `bash -n`.

Docker Engine 29.6.2 was accessible after permissions were updated. The corrected `portkeeper:poc` image built successfully. Python 3.13.16 initialized the application as UID 10001 with a read-only root filesystem, writable tmpfs, dropped capabilities, and no new privileges. The image includes `lsb-release`, required by upstream CMake. Fresh profile compilation is recorded below.

Browser checks passed for Library, Activity, Settings, worker setup, version pin/unpin, retention preview, and a warm-cache test build, with no console errors.

One synthetic custom Docker recipe passed with a digest-pinned official Python image, networking disabled, a read-only root filesystem, a non-root user, dropped capabilities, no new privileges, and a private per-job workspace with SELinux `:Z` labeling. It created fixture output that the engine validated and archived; the resulting ZIP contents matched the expected bytes. This checks the generic Docker runner, not a recipe for an additional game project.

## Real profile pipeline

The integrated Shipwright worker completed all stages for Linux and Windows using a local user-owned ZIP: real source/runtime capture, capture of all six pinned dependency sources, headless extractor compilation, ROM verification, asset extraction, packaging, and package checks. Generated packages exclude the original ROM, and the original input remained unchanged.

A source/dependency preservation bundle was exported first, with toolchain preservation and offline-rebuild verification reported as false. A later verified bundle is described below; the earlier partial export remains available separately.

A bounded **launch check of the actual integrated-worker Linux ZIP** passed under Xvfb/software rendering: its generated assets loaded and scene `0x51` initialized. The process ran for 20 seconds, then was intentionally stopped by timeout (exit 124). This establishes a launch check for that package; it does not certify full gameplay or physical Steam Deck behavior. Windows has packaging/integrity verification only.

## Offline rebuild verification

A fresh **offline profile rebuild passed** in the corrected service image in 58.34 seconds. It began with the exact preserved source archive, official Linux/Windows runtime assets, six pinned dependency ZIPs, and a separately supplied user-owned ROM ZIP. There was no compiled extractor or extracted-game-asset cache. GCC 14 and CMake 3.31 compiled the extractor, assets were extracted, and both target packages passed verification and reached `ready`.

The check used Docker `--network none`, a read-only root filesystem, UID/GID 1000, dropped capabilities, no new privileges, four CPUs, 4 GiB memory, and a 512-process limit. An additional download guard rejected every network-download attempt. This verifies the reviewed headless-extractor/prebuilt-runtime assembly scope from preserved material with the supplied image and owned input; it does not prove full runtime source compilation or gameplay.

The corrected Docker image was archived with the preserved source/runtime assets, all required dependency sources, the extractor patch, real configure/compile logs, and offline-build proof into a **324,187,066-byte preservation ZIP**. Integrity auditing passed. Its manifest records `offline_rebuild_complete: true` for the exact scope **Shipwright 9.3.0 headless extraction and prebuilt runtime assembly**. The bundle excludes the owned ROM and generated game assets; reproducing the build still requires a supported input supplied separately.

The application's preservation status now recognizes this saved proof only when its capture, scope, toolchain identity, and archived toolchain checksum match the selected materials. The overview exposes persistent download links for saved bundles, including this verified bundle and the earlier partial export. Ordinary HTTP exports do not run Docker verification or automatically acquire proof; an image archive alone does not establish an offline rebuild.

## Limits and pending verification

- No full gameplay session, native Windows launch, physical Steam Deck, ARM handheld, Switch, or macOS test is recorded.
- Custom Docker validation covers one synthetic recipe. Other projects' recipes, toolchains, and gameplay require separate checks.
- RomM configuration/request tests use fixtures. A user-provided Client API Token and a separate live authenticated API download check are still required. API upload/indexing is not implemented.
- Local assistant tests validate request shaping, redaction, and proposal guards. An installed live Ollama model is not part of CI.
- Preservation reports remain conservative and scope-specific. The verified offline profile check does not establish complete full-runtime source-build closure or certify arbitrary exports.

Game files, extracted assets, credentials, private cache data, and personal validation input JSON are excluded from the source repository and CI.
