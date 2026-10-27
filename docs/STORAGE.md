# Small state, separate bulk storage

New installations keep application state apart from game payloads. Updating an
existing image does **not** move or delete files, modify the saved Unraid template,
or prune old migration snapshots. Existing databases keep their legacy paths until
an explicit, verified migration. Settings → Advanced → Storage & runners shows the
actual state and bulk paths and identifies a legacy layout.

## Installation defaults

| Purpose | Container | New Unraid host default |
| --- | --- | --- |
| SQLite, credentials, catalog, drafts, recipe definitions and state snapshots | `/data` | `/mnt/user/appdata/portkeeper` |
| Preserved sources/releases, generated packages, extraction, work and caches | `/storage` | `/mnt/user/data/Emulation/portkeeper` |
| Owned ROMs and game files, read-only | `/inputs` | `/mnt/user/data/media/roms` |
| Published packages | `/library` | `/mnt/user/data/Emulation/portkeeper/library` |

The Unraid template sets `PORTKEEPER_STORAGE_DIR=/storage`. Compose uses a separate
`portkeeper-storage` volume; its existing library volume remains separate too.
Keep the input mount read-only. Choose an array-backed share for bulk storage on
Unraid; the path name alone does not establish the share's cache/array policy.

For a new host-run installation, `--data-dir ./state` defaults to a sibling
`./state-storage` directory. `--storage-dir` or `PORTKEEPER_STORAGE_DIR` overrides
that default. The Python `Engine` constructor retains its old single-directory
default for callers; pass `storage_dir=` to select the separated layout.

The selected bulk path persists in SQLite, so a new host-run install keeps the same
path after restarting without flags. A small `.portkeeper-storage-id` on the bulk
filesystem must match the ID in state. A missing/replaced mount stops startup rather
than filling the container filesystem. Retain this marker with bulk-file backups.

Bulk storage holds:

- `archive/`: captured upstream sources/releases, preservation bundles and packages.
- `prepared/`: extracted/merged game data, including PS4 base/update results.
- `cache/`: dependencies, Shipwright extraction/build material, RomM downloads and
  provider XDG caches/temporary files.
- `work/`, `agent-workspaces/`, and `demo-inputs/`.
- `automation-recipes/`: **full public replay payloads**, not recipe JSON alone.
- `toolchains/`: preserved toolchain archives, which can be large.

Active recipe definitions and saved revisions remain in SQLite. Native account
credentials stay in `/data/secrets`; ephemeral provider work and caches use bulk
storage. Vendor-specific files in account homes should still be inspected during
an existing-install migration, without deleting credentials.

## Built-in snapshots

Cleanup actions and the storage migration command use one shared backup helper.
They never copy the state directory recursively. A snapshot includes:

- A consistent, compacted SQLite copy (including active recipes, revisions, settings,
  test records and automation history).
- Catalog/draft/recipe/definition text files from explicit metadata directories.
- RomM identity/artwork receipts and known account/configuration credential files.
  This includes the optional Home Assistant long-lived token in
  `secrets/home-assistant.json`; keep that file covered by your private backups.

Archives, ROMs, game data, packages, prepared folders, dependency archives, worker
logs, agent caches, replay payloads and worker images are excluded even when an
individual file is small. Metadata files are limited to 4 MiB each and 64 MiB total;
oversized metadata stops backup/cleanup for review rather than silently omitting it.
These limits do not truncate SQLite.

Snapshots live in `/data/state-backups/state-<id>/`. `manifest.json` lists checksums,
creation time, reason and action context. Files and credentials are private.
`PORTKEEPER_BACKUP_KEEP=3` keeps the last **three** complete, unmodified managed
snapshots; configure 1–20. Only recognized, checksum-valid snapshots in this new
namespace are retention candidates. Foreign, unfinished, edited and legacy backups
are retained for explicit review. Retention does not inspect/delete old
`maintenance-backups/`, `portkeeper-automation-backups`, unique acceptance artifacts,
or worker-image exports.

Create a snapshot independently, using the service account and the actual state path:

```sh
python -m portkeeper.backups --data-dir /data --keep 3
```

The helper does not start Engine, replay jobs, or access `/inputs`. There is no new
scheduled full-backup task. An existing Appdata Backup plugin can continue taking
nightly state backups.

## Migrate an existing installation

Do this on the machine running the app, with its actual mount paths and service UID.
The cloud checkout cannot inspect or migrate a NAS it cannot reach.

1. Inventory the running container **and Docker context**, template/Compose file,
   broker service/exchange, current paths, ownership, image IDs and disk space. A
   rootless deployment may run in a Linux VM; use that daemon's account. Inventory
   old snapshots before recommending any deletions, particularly partial snapshots.
2. Finish or cancel active/paused jobs, stop the app, and preserve a metadata snapshot
   and deployment configuration. Do not make another recursive appdata/library copy.
   Keep an existing verified full rollback backup and any unique older artifacts.
3. Copy each bulk directory above and the actual library to the new host storage.
   Use `rsync -aHAX --numeric-ids` where the filesystem supports it. Keep sources;
   **do not use `--delete`**. Verify with checksum comparison, not size/mtime alone.
   Inspect additional payload directories and provider-home caches on this installation.
   Absolute symlinks require explicit review; the helper refuses them so a copied
   cache cannot silently depend on an old directory that will later be deleted.
4. Review the path plan while the app remains stopped. Example **container aliases**
   below assume `/data` and `/storage` are accessible in a maintenance process:

   ```sh
   python -m portkeeper.storage_layout \
     --data-dir /data --old-storage /data --new-storage /storage
   ```

   Add `--old-library ... --new-library ...` only when its **in-container** path
   changes. If `/library` still maps to the new host directory, no library-path
   rewrite is needed. For a container deployment, use a maintenance container with
   the app's original path aliases and new `/storage` bind; host paths are bind
   sources, not substitutes for `/data` in database values. `--volumes-from` the
   stopped app can retain its old aliases. Use the service UID, no network, and
   invoke `python` directly without starting the server. Supply real host paths
   only when Portkeeper itself is host-run, with `--input-root` set to the actual
   owned collection (for example `/mnt/user/data/media/roms`). The helper rejects
   mismatched namespaces instead of recording host paths in a container database.
5. After reviewing the plan, repeat with `--apply`. This verifies copied trees by
   file hashes, file modes and link targets, creates a metadata snapshot, then
   rewrites structured absolute path values in SQLite and allowlisted JSON metadata.
   It updates the layout marker so the new configuration can start. It never moves
   or deletes source files. Embedded shell commands/historical log text are not
   rewritten by substring replacement; review any custom absolute paths there.
   If maintenance is interrupted, restore its state snapshot before retrying; do
   not start against a partially applied configuration.
6. Update the real Unraid template/Compose mappings, `PORTKEEPER_STORAGE_DIR`, any
   explicit CLI flags and the broker's host paths/protected roots. The broker must
   still share the app's exchange files and use an immutable, retained worker image.
   Do not put an unrestricted Docker socket in the app.
7. Start and verify the UI's reported paths, original ROM read-only mount, existing
   downloads, prepared-input lookup and a small saved-recipe replay. Check actual
   new writes land on the bulk filesystem. Do not use a large extraction as a smoke test.
8. Present exact old-copy/snapshot deletions and reclaimed-space estimates for
   confirmation, then remove only approved copies. This code never performs that
   host cleanup automatically.

An image upgrade or new path setting with unmigrated data fails with a migration
message instead of silently losing downloads or rebuilding into a second empty root.

## Appdata Backup exclusions and irreplaceable material

After verified migration and approved old-copy removal, steady-state appdata contains
SQLite/WAL/SHM, `secrets/`, `catalog/`, `drafts/`, optional text definitions/recipes,
identity/artwork JSON and bounded `state-backups/`. None is game payload. Back up this
small state; optionally exclude `state-backups/` from the plugin to avoid duplicating
the built-in historical copies inside nightly snapshots. Review any legacy backup
directories still present rather than excluding/deleting them blindly.

The old appdata exclusions for `archive`, `prepared`, `work`, `cache` and
`portkeeper-library/native-ports` become obsolete **after** those files have moved.
Moving them outside appdata does not make them part of the plugin's backup.

Do not equate bulk storage with disposable storage:

| Material | Regenerable from `/inputs` and recipes alone? |
| --- | --- |
| Prepared games, temporary work, published package copies | Usually, given the same complete inputs, tools, keys and build environment. Preserve anything hand-modified. |
| `archive/sources`, preserved release assets and preservation bundles | **No.** Upstream commits/assets can disappear or change. Keep independent backup coverage for captures you need. |
| `automation-recipes` public seeds/dependencies | **No.** Exact replay uses their saved bytes; a JSON recipe alone is insufficient. |
| Pinned worker images and `toolchains` | **No.** An image ID cannot recreate an unavailable image, and rebuilding a Dockerfile need not produce identical bytes. |
| Unique local/acceptance builds, patches, manually added files, saves | **No**, unless another verified copy or reproducible procedure exists. |
| DB/settings, credentials, recipes/revisions, test and draft history | **No.** Keep in the small-state backup. |

At minimum, consider separate coverage for `archive/sources`, required preserved
assets/bundles, `automation-recipes`, pinned worker-image exports, toolchains and
unique older artifacts. `archive/packages` and the published library can often stay
excluded when their exact source/data/environment are retained and rebuilds work.
Avoid backing up duplicate game-filled package copies merely to preserve a small
upstream source archive. Keep backup destinations outside source trees.
