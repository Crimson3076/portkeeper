# Architecture

Portkeeper is a single-process self-hosted service: a Python standard-library HTTP server serves the UI/API, SQLite stores worker state, and a scheduler checks public GitHub releases. The UI has Library, Activity, and Settings pages, a worker setup wizard, and project tabs for status, versions, jobs, and advanced configuration.

## Update pipeline

```text
Detect stable release
  -> checkpoint tagged source and selected release assets
  -> capture reviewed profile dependencies, when applicable
  -> verify selected user inputs
  -> assemble, run a reviewed profile, or use an opt-in Docker recipe
  -> validate outputs and archive a checksummed package
  -> manual or opt-in automatic local-library publication
```

Capture precedes input validation and execution. The source is checkpointed before release-asset downloads, and each completed asset is checkpointed afterward. A failed asset download leaves preserved source accessible but blocks building until a complete capture exists. Profile dependency sources are checkpointed before compiler execution. Unsupported new profile releases are still preserved, then held for review.

The scheduler ticks every 15 seconds and checks projects whose `poll_minutes` interval is due. Two executor threads are available. A per-project claim prevents overlapping jobs; targets within a job group run sequentially. Restarted services mark interrupted jobs failed for explicit retry. Jobs expose stages and bounded logs.

## Storage and publication

```text
<data-dir>/
  portkeeper.sqlite3
  archive/
    sources/<project-id>/<capture-id>/
      source.zip
      asset-<number>-<name>
      capture.json
    packages/<project-id>/<artifact-id>/
      <project>-<version>-<target>.zip
      manifest.json
    preservation/                 # sources, optional toolchains/build proof
  cache/
    shipwright/9.3.0/             # sources, extractor, generated assets
    romm/                         # selected game files; private data
  secrets/
    romm.json
    assistant.json
  toolchains/                     # optional TAR/ZIP material
  work/<job-id>/                  # temporary build workspace

<library-root>/
  <frozen-project-basename>-<target>.zip
```

The input root is separate and treated as read-only. RomM inputs use a private local cache. Generic workspaces are removed after builds; reusable profile extractor/assets remain in the profile cache. Back up the full data directory privately.

Packages have immutable IDs, manifests, recipe/input hashes, and ZIP checksums. Publishing verifies the archived ZIP, copies it to a temporary file in the library directory, verifies that copy, and atomically renames it into place. A failed target leaves its previous publication intact. `auto_publish` is disabled by default and requires `auto_build`; manual rollback republishes any retained artifact. The publication basename freezes on first publish, so later project renames do not leave duplicate current filenames.

## Runners and targets

| Runner | Execution and scope |
| --- | --- |
| Capture | Preserve upstream material without execution. |
| Assembly | Unpack supported archives, copy verified inputs, check expected files, ZIP outputs. |
| Reviewed profile | Fixed pinned Shipwright extractor build/extraction commands, enabled with `--enable-profile-builds`. |
| Docker recipe | User-reviewed argument array inside a digest-pinned Linux container, enabled with `--enable-docker`. |

Custom recipes support `linux`, `windows`, and `handheld` labels and shallow per-target overrides. They do not synthesize cross-compilers or imply universal device support. The Shipwright profile supports Linux / Steam Deck and Windows x86-64 at a specific release/commit. Windows packages use prebuilt runtimes; native Windows compilation is not implemented.

Profile mode does not accept arbitrary commands. It captures pinned Torch, libultraship, spdlog, yaml-cpp, tinyxml2, and zlib sources; applies a checksummed extractor patch; configures CMake with fetches disconnected; and invokes fixed build/extraction tools. Opting in permits those trusted upstream sources to run on the host or service container. The host toolchain/system development libraries are not automatically preserved.

Docker recipes override the image entrypoint with the declared command. Containers get `/work`, not the original input root or Docker socket. They use a read-only root filesystem, the host UID/GID, dropped capabilities, no new privileges, no network by default, and `/tmp` tmpfs. Limits are 2 CPUs, 2 GiB RAM, 256 processes, 30 minutes, and 8 MiB runner output. The Compose service enables reviewed profiles and does not enable this arbitrary Docker runner.

Archive extraction rejects traversal, links, special files, and oversized output. Current generic limits are 1 GiB per input/download and 2 GiB / 30,000 entries for expanded trees. File checks establish package structure and integrity; gameplay requires separate evidence.

## RomM and credentials

Settings configure a RomM server URL and Client API Token. The server writes secrets atomically with mode `0600` and returns only configured status to the browser. API calls remain on the configured origin, validate downloaded files against available metadata, inspect supported ROM revisions, and provide bounded ZIP-member selection. No token or ROM is sent to the optional model.

Use `roms.read` for searching/downloading; `me.read` is optional for the probe. RomM API upload/indexing is deferred. Publication uses a configured local shared directory. Source monitoring currently supports unauthenticated public GitHub repositories; private upstream credentials and other forge providers are not implemented.

The HTTP service has no application user authentication. It rejects inappropriate mutation content types and cross-origin requests, but those checks do not replace an authenticated reverse proxy for remote access.

## Validation and retention

Artifacts start at `packaged`. Users can record `launch_checked` or `user_confirmed_playable` with notes/evidence; a known-good pointer requires one of those higher levels. Synthetic demos cannot be marked playable. Known-good pointers are per project/target and do not follow new packages automatically.

Retention defaults to keep all packages. Preview/apply keeps the latest N per target while protecting the newest, current published, pinned, and known-good artifacts. Applying a policy saves it for cleanup after subsequent project jobs. Cleanup refuses a running project and validates all candidate paths before deletion. Source captures and preservation bundles are outside package retention.

The inbox stores deduplicated ready, failed-build, missing-input, and unavailable-upstream events. Read/dismissed events stay quiet when repeated. Browser notifications are optional and require a browser permission grant; they are not a separate push service.

## Preservation completeness

Exports bundle captured source/assets, available reviewed dependency archives, the extractor patch/provenance, and an optional registered toolchain archive. ROMs, extracted game assets, user input paths, and credentials are excluded. Source declarations are inventoried conservatively; this is not a CMake interpreter or recursive package-registry mirror.

Closure reports show missing materials and the dependency scope. The extractor/prebuilt-runtime scope does not claim full-game compilation closure. Ordinary HTTP exports do not execute an offline rebuild or automatically mark it verified. A separately verified bundle can include actual build logs and proof; preservation status recognizes it only when the selected capture, expected scope, toolchain identity, and toolchain checksum match. The overview lists persistent downloads for saved bundles. The observed Shipwright rebuild and audited bundle are recorded in [validation](VALIDATION.md).

Toolchains can be saved from a locally installed digest-pinned Docker image or registered from an existing TAR/ZIP below `<data-dir>/toolchains`. Saving an image archives it; it does not load or execute it. Status and export compare registered material against its saved checksum; changed or missing archives do not count as preserved.

## Optional recipe assistant

Ollama endpoints are restricted to loopback addresses. The assistant receives a bounded, redacted recipe/log context and returns validated JSON proposals. The base runner mode, base payload kind, and verified private inputs cannot be changed by a proposal. Private input guards and path redaction include per-target overrides. No tools or commands execute during drafting; the user reviews and explicitly applies a proposal. Applying checks that the base recipe has not changed and saves the recipe for future normal jobs.

Under Compose, loopback refers to the service container, so a host Ollama service is not reachable at the container's `127.0.0.1`. Use the host Python deployment for a host-local assistant unless you deliberately provide a loopback service in the same network namespace.
