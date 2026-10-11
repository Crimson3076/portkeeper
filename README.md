# Portkeeper

A self-hosted companion for RomM that preserves game port releases, turns your selected game files into versioned packages, and keeps a working version available while updates are being prepared.

Portkeeper captures source before building. The reviewed Ship of Harkinian profile supports release **9.3.0** for Linux x86-64 / Steam Deck and Windows x64. Custom recipes can preserve or assemble other projects, including device-specific handheld ports.

## Start locally

Python 3.11 or later runs the server with no Python package dependencies:

```bash
cd /path/to/portkeeper
./start.sh
```

Open [http://127.0.0.1:8787](http://127.0.0.1:8787). In **Settings → Demo**, run the synthetic demonstration to exercise three target packages without game files, internet access, or Docker.

To enable the reviewed Shipwright extractor build, supply a Linux build host with CMake 3.26+, Ninja, a C/C++ compiler, `patch`, `lsb_release`, and zlib development headers:

```bash
./start.sh \
  --data-dir ./data \
  --input-root /path/to/owned-game-files \
  --library-root /path/to/romm/ports \
  --enable-profile-builds
```

`start.sh` forwards flags to `python3 -m portkeeper`. Without flags, data lives in `.data`, local inputs in `.data/inputs`, and published packages in `.data/library`. The original input directory is read without modifying its files.

## Start with Docker Compose

```bash
cd /path/to/portkeeper
mkdir -p game-inputs
docker compose up --build
```

Compose binds the web service to `127.0.0.1:8787`, mounts `game-inputs` read-only, persists application data and the local library, and enables the fixed reviewed profile runner. The image includes its build tools and runs as UID 10001; local input files must be readable by that user. Replace `portkeeper-library:/library` in `compose.yaml` with your writable RomM ports directory when ready; that directory must be writable by UID 10001.

The input bind uses the shared SELinux label option `z` (`:ro,z`). On SELinux hosts, use shared `:z` for a RomM library bind too, such as `/srv/romm/ports:/library:z`, so other containers sharing that directory remain able to access it. The private label option `Z` is inappropriate for a directory shared with RomM. See [Docker's SELinux mount guidance](https://docs.docker.com/engine/storage/bind-mounts/#configure-the-selinux-label).

Custom Docker recipes remain disabled in Compose. The Docker socket is not mounted. The service image passed a fresh network-disabled Shipwright extractor build and Linux/Windows packaging from preserved material plus an owned input. See [validation](docs/VALIDATION.md) for the checked scope and remaining limits.

## Set up a worker

1. In **Settings → RomM**, enter your server URL and a [Client API Token](https://docs.romm.app/latest/developers/client-api-tokens/) with `roms.read`. `me.read` is optional for the connection probe. Save and test the connection.
2. Select **Add worker**, choose Ship of Harkinian, select your game file and supported ROM revision, then choose Linux / Steam Deck and/or Windows.
3. Run **Test build**. The worker captures the release and pinned extractor dependencies, compiles the reviewed extractor, verifies your ROM, generates assets, and packages the official runtime.
4. Download and try a package. Record a launch check or gameplay confirmation, then mark a version **known good**.
5. Enable automatic builds and, optionally, automatic local-library publishing in the worker's **Advanced** tab.

Credentials stay in server-side files with mode `0600`; they are not returned to the browser after saving. RomM-selected game files are downloaded into a private local cache. Local-file fallback is available through the API for files beneath `--input-root`; see [Shipwright setup](docs/SHIPWRIGHT.md).

**Publish** copies a successful ZIP atomically into the configured local library folder. Selecting an older version rolls back that target. Automatic publishing is opt-in, requires automatic builds, and preserves a failed target's previous current package. Uploading packages through the RomM API and triggering its indexing are deferred; configure RomM's shared directory and scanning separately.

## What is included

- Public GitHub stable-release monitoring, source/release archival, staged jobs, logs, retries, and checksums.
- A reviewed Shipwright 9.3.0 extraction/packaging worker; newer releases are preserved and held for a reviewed profile update.
- Custom capture, assembly, and optional digest-pinned Docker recipes with per-target overrides.
- Version pinning, validation history, known-good pointers, and retention previews protecting current, pinned, latest, and known-good packages.
- Preservation exports containing captured sources, available pinned dependencies, patches, optional archived toolchains, scoped proof status, and saved-bundle downloads.
- An inbox for meaningful worker events and optional browser notifications.
- Optional loopback Ollama recipe proposals: review and apply a draft before saved recipes change. Proposals never execute commands.

The **Library**, **Activity**, and **Settings** pages and each worker's overview, versions, jobs, and advanced tabs expose these workflows. [Recipes](docs/RECIPES.md) and [architecture](docs/ARCHITECTURE.md) describe the execution boundaries.

## Optional custom Docker recipes

Run on a host with an accessible Docker daemon and add `--enable-docker`. Recipe images must be pinned by digest. Build containers receive only their workspace, use a read-only root filesystem, drop capabilities, block privilege escalation, and disable networking by default. They have CPU, memory, process, time, and log limits. Arbitrary recipe commands are never executed directly on the host.

The reviewed profile runner is separate: `--enable-profile-builds` authorizes fixed build/extraction commands from the pinned profile on the Linux host or service container. It does not authorize commands invented by a recipe or model.

## Checks

```bash
python3 -W error -m unittest discover -s tests -v
node --check static/app.js
docker compose config --quiet
```

GitHub CI is configured to run the standard-library suite on Python 3.11 and 3.13 and check frontend JavaScript syntax. It does not contain ROMs, compile game assets, or require a Docker daemon. Current evidence and untested paths are recorded in [VALIDATION.md](docs/VALIDATION.md).

## Boundaries

This early application has no user authentication. Keep the loopback bind or add authenticated access before exposing it. Back up the data directory privately: caches and packages can contain your game assets and secrets.

Packaging success is distinct from launch checks and gameplay confirmation. The verified profile does not support ARM handhelds, Switch, or macOS, and it assembles official Windows binaries rather than compiling them natively. Generic target labels do not guarantee device compatibility.

A source ZIP or preservation export is not automatically a complete offline rebuild. Dependency declarations, toolchains, system libraries, and offline-build evidence are tracked separately. Game files and credentials are excluded from preservation exports and the source repository. Supply your own required game files; Portkeeper does not obtain them from public ROM sites.
