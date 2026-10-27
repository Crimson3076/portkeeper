# Portkeeper

A self-hosted companion for RomM that preserves game port releases, turns your selected game files into versioned packages, and keeps a working version available while updates are being prepared.

Portkeeper captures source before building. Each project has a JSON [packaging definition](docs/PACKAGING.md) that describes upstream release rules, the files you provide, build steps, platform packages, launchers, saves, dependencies, checksums and limitations; the readable **Packaging instructions** are generated from it. Bundled definitions automate 2 Ship 2 Harkinian, Banjo Recompiled, Jak and Daxter (OpenGOAL), Bloodborne bbport (Linux and Windows) and Dusklight. The reviewed Ship of Harkinian profile supports release **9.3.0** for Linux x86-64 / Steam Deck and Windows x64.

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

`start.sh` forwards flags to `python3 -m portkeeper`. New installations keep small
state in `.data` and bulky files in `.data-storage`; use `--storage-dir` to choose
another location. Existing databases keep their old paths until explicitly migrated.
The original input directory is read without modifying its files. See
[storage and small backups](docs/STORAGE.md) before migrating an existing installation.

For PS4 ports, [prepare your base and update PKGs automatically](docs/PS4_GAME_FILES.md)
with checked extraction and editable packaging steps. The GOW3 Windows candidate
includes the prepared game folder; startup and gameplay remain untested.

## Start with Docker Compose

```bash
cd /path/to/portkeeper
mkdir -p game-inputs
docker compose up --build
```

Compose binds the web service to `127.0.0.1:8787`, mounts `game-inputs` read-only,
and persists small state, bulk storage and the local library in separate volumes.
`PORTKEEPER_STORAGE_DIR=/storage` keeps archives, prepared games and caches out of
appdata. Existing installations must follow the [migration guide](docs/STORAGE.md)
before adding this setting. Built-in metadata snapshots keep the latest three;
`PORTKEEPER_BACKUP_KEEP` configures 1–20. No full-folder backup task is installed.
The image includes its build tools and runs as UID 10001; local input files must be
readable by that user. Replace `portkeeper-library:/library` with your writable RomM
ports directory when ready; that directory must be writable by UID 10001.

The input bind uses the shared SELinux label option `z` (`:ro,z`). On SELinux hosts, use shared `:z` for a RomM library bind too, such as `/srv/romm/ports:/library:z`, so other containers sharing that directory remain able to access it. The private label option `Z` is inappropriate for a directory shared with RomM. See [Docker's SELinux mount guidance](https://docs.docker.com/engine/storage/bind-mounts/#configure-the-selinux-label).

Custom Docker recipes remain disabled in Compose. The Docker socket is not mounted. The service image passed a fresh network-disabled Shipwright extractor build and Linux/Windows packaging from preserved material plus an owned input. See [validation](docs/VALIDATION.md) for the checked scope and remaining limits.

## Install on Unraid

The image is published to `ghcr.io/crimson3076/portkeeper:latest` on every push to `main`.

1. Copy [`unraid/portkeeper.xml`](unraid/portkeeper.xml) to `/boot/config/plugins/dockerMan/templates-user/my-portkeeper.xml` on the server (or add the container by hand with the same settings).
2. In **Docker → Add Container**, pick the `portkeeper` template.
3. Set **Allowed hosts** to every address you type into the browser, such as `192.168.1.20,tower,tower.local`. Any other address is refused with `403 Unrecognized Host header`; this protects the unauthenticated UI from DNS-rebinding attacks.
4. Point **Library** at your RomM ports folder when ready. Apply, then open the WebUI.

The new template keeps small state at `/mnt/user/appdata/portkeeper`, bulk files at
`/mnt/user/data/Emulation/portkeeper`, and published packages beneath that bulk
directory. Game inputs default to `/mnt/user/data/media/roms` and are mounted
read-only. Image updates do not rewrite an existing saved template or migrate data.
See [storage layout, migration and backup coverage](docs/STORAGE.md).

Unraid starts containers as root. The entrypoint takes ownership of the app-data folder for `PUID`/`PGID` (default `99:100`, nobody:users), then drops to that user. A non-empty library folder is never re-owned; it must already be writable by that user.

For updates to an existing Unraid container, see [Unraid update instructions](docs/UNRAID_UPDATES.md).

## Set up a worker

1. In **Settings → RomM**, enter your server URL and a [Client API Token](https://docs.romm.app/latest/developers/client-api-tokens/) with `roms.read`. `me.read` is optional for the connection probe. Save and test the connection.
2. Select **Add worker**, choose Ship of Harkinian, select your game file and supported ROM revision, then choose Linux / Steam Deck and/or Windows.
3. Run **Test build**. The worker captures the release and pinned extractor dependencies, compiles the reviewed extractor, verifies your ROM, generates assets, and packages the official runtime.
4. Download and try a package. Record a launch check or gameplay confirmation, then mark a version **known good**.
5. Enable automatic builds and, optionally, automatic local-library publishing in the worker's **Advanced** tab.

Credentials stay in server-side files with mode `0600`; they are not returned to the browser after saving. RomM-selected game files are downloaded into a private local cache. Local-file fallback is available through the API for files beneath `--input-root`; see [Shipwright setup](docs/SHIPWRIGHT.md).

**Publish to RomM** uploads a package through RomM's chunked upload API (token scope `roms.write`). RomM creates the library entry after its filesystem watcher rescans; Portkeeper confirms the entry and links its game metadata and cover. **Copy to local library folder** is a separate action that reaches RomM only when that folder is RomM's own platform folder. Automatic publication is opt-in per game, requires automatic builds, and uploads to RomM by default. A new release that does not match its definition's compatibility rules is preserved and reported for review instead of being packaged.

## What is included

- [Automatic port setup](docs/AUTOMATION.md): a separate offline script worker, provider-neutral model loop, preserved dependencies, editable recipes and model-free clean replay. Requires a dedicated rootless broker; the ordinary Unraid image alone does not enable arbitrary model-authored builds.
- Public GitHub stable-release monitoring, source/release archival, staged jobs, logs, retries, and checksums.
- Packaging definitions with a fixed operation vocabulary, headless extraction tools (Ship-style `.o2r` generation, OpenGOAL, PS4 PKG), explicit compatibility rules for new releases, and per-release status: preserved, needs review, packaged, in RomM.
- A cached GitHub project catalog with verification scopes and definition updates.
- [Recomp Launcher discovery](docs/DISCOVERY.md) inside Add a game, with searchable project references and upstream repository lookup.
- A reviewed Shipwright 9.3.0 extraction/packaging worker; newer releases are preserved and held for a reviewed profile update.
- Custom capture, assembly, and optional digest-pinned Docker recipes with per-target overrides.
- Version pinning, validation history, known-good pointers, and retention previews protecting current, pinned, latest, and known-good packages.
- Preservation exports containing captured sources, available pinned dependencies, patches, optional archived toolchains, scoped proof status, and saved-bundle downloads.
- An inbox for meaningful worker events and optional browser notifications.
- Optional drafting with recommended Codex (ChatGPT subscription) and Claude Code (Pro/Max subscription), Gemini CLI (free Google account), your own chat, Ollama, or a separately billed API. [Investigate before drafting](docs/INVESTIGATIONS.md) retrieves bounded public evidence, reports missing capabilities and proposes a reviewable plan before generating packaging instructions. [Subscription agent setup](docs/SUBSCRIPTION_AGENTS.md) uses official CLI sign-in and isolated text-only workspaces. Optional temporary test builds use Portkeeper's supported operations; instructions require review before they become active. See [packaging](docs/PACKAGING.md#drafting-with-a-model).

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

### Library layouts

Game workspace is the default: select a game beside its platform downloads and
artwork. Settings → Appearance also offers Collection and Library list. The choice
is saved per browser; if browser storage is unavailable, it lasts for the session.
Games with the same registered import prefix and IGDB identity are shown together.
The library includes matching Linux/Windows imports from RomM, so manually imported
ports appear alongside Portkeeper builds. Platform download counts exclude older
archive copies. Build recipes and saved releases remain available in game details.

Frontend interaction tests: install `jsdom@26.1.0` in a temporary Node environment,
then set `PORTKEEPER_JSDOM` to that environment's `node_modules/jsdom` path and run
`node tests/test_ui_layouts.cjs`. CI runs these checks alongside the Python suite.

## Project verification

See [verification criteria and the human/LLM review workflow](docs/PROJECT_VERIFICATION.md). The [GitHub catalog](catalog/projects.json) records version- and platform-specific evidence and approval. Catalog verification is separate from download startup/gameplay results; the entries await an explicit approval reference. Portkeeper fetches the catalog from GitHub daily, keeps the last valid copy when GitHub is unavailable, and shows its age and source in **Settings → Catalog**.
