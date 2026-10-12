# Recipe guide

Projects now use [packaging definitions](PACKAGING.md). A schema 1 recipe described here still runs unchanged inside a definition with `runner: "recipe"`, and new custom projects can start from one.

A recipe describes one repeatable way to preserve or package a project. It is saved as JSON and interpreted by Portkeeper. Arbitrary host shell execution is not a recipe feature. Reviewed profiles use their separate fixed runner.

Always begin with a manual run against known inputs. Expected-file checks establish package structure; testing the resulting game on the intended device remains a separate step.

## Common fields

| Field | Meaning |
| --- | --- |
| `version` | Recipe format version; use `1`. |
| `mode` | `capture`, `assemble`, `docker`, or the curated `profile` mode. |
| `profile` | Curated runner ID; currently `shipwright`. Select through the worker wizard or project API. |
| `inputs` | Local files selected relative to the server's input root. |
| `payload.kind` | Start from archived `source` or `release` assets. |
| `payload.asset_patterns` | Optional per-target regular expressions matching release asset names. |
| `payload.single_file_name` | Optional package path for a release asset that is a single file rather than a ZIP/TAR, such as an AppImage. Defaults to the upstream asset name; set it so `expected_files` stay valid across versions. |
| `steps` | Reserved step list. Use an empty list in this proof of concept. |
| `expected_files` | Required files within the produced package. |
| `target_overrides` | Optional objects replacing selected fields for `linux`, `windows`, or `handheld`. |
| `notes` | Human-readable setup or compatibility notes. |

Each input has `path`, `destination`, and `sha256`. The source path is relative to `--input-root`; the destination is relative to the package/workspace. Absolute paths and paths escaping these roots are rejected. Compute hashes on the actual game file you intend to use:

```bash
sha256sum /path/to/game-files/my-game/game.bin
```

Keep the digest in the recipe. File size, extension, and filename alone do not prove that the input is the correct revision.

Credentials are not recipe fields; configure RomM in Settings. Unknown recipe fields are rejected. Profile ROM choices use the project's `rom_source` and supported SHA1 instead of generic `inputs`; see [Shipwright](SHIPWRIGHT.md).

## Reviewed profiles

Select Ship of Harkinian through the worker wizard, or create a project with top-level `profile: "shipwright"`. The application supplies a `mode: "profile"` recipe and verifies the repository, supported targets, input revision, release, and commit. The fixed runner needs `--enable-profile-builds`, captures pinned dependency sources before compiling, and records extractor/runtime provenance.

This is not a free-form host-command mode. Profile recipes reject arbitrary commands, images, network settings, steps, and target overrides. Currently the profile supports Shipwright 9.3.0 for Linux / Steam Deck and Windows x64. A newer release is archived and held until the profile is reviewed for it. Use a custom assembly/Docker recipe for other projects instead of changing the profile identity.

## Capture only

```json
{
  "version": 1,
  "mode": "capture",
  "inputs": [],
  "payload": { "kind": "source" },
  "steps": [],
  "notes": "Archive stable releases without creating a playable package."
}
```

Capture records the release and archives source/release material. It does not promote a package to the library. It is the right default for projects whose instructions have not been encoded or whose upstream code is not yet runnable.

Source is always captured. Release assets are captured only when their names match a pattern for one of the project's selected targets. The example above therefore captures source only. Add `payload.asset_patterns` if release assets should also be preserved, even while the mode remains `capture`.

## Assemble a prebuilt release

An assembly recipe combines captured payload with checked local inputs and creates a ZIP for each configured target. Asset filters match upstream release filenames; each target must match exactly one captured asset. Use precise expressions so unrelated downloads are not mixed into a package. If you change patterns, check upstream again to capture the newly selected assets before building.

```json
{
  "version": 1,
  "mode": "assemble",
  "inputs": [
    {
      "path": "my-game/game.bin",
      "destination": "assets/game.bin",
      "sha256": "REPLACE_WITH_THE_64_CHARACTER_SHA256_OF_YOUR_INPUT"
    }
  ],
  "payload": {
    "kind": "release",
    "asset_patterns": {
      "linux": "^example-linux-x86_64\\.zip$",
      "windows": "^example-windows-x64\\.zip$",
      "handheld": "^example-device-port\\.zip$"
    }
  },
  "steps": [],
  "expected_files": ["assets/game.bin"],
  "notes": "Illustrative filenames only. Replace them with the upstream project's exact assets and required layout."
}
```

This example is a schema illustration, not a working recipe for a particular game. The hash placeholder must be replaced before it can run. Inputs may be extracted game assets rather than a ROM when that is what the runtime expects.

Assembly can unpack supported ZIP/TAR payload archives. It cannot run game-asset extraction tools, compilation, patching, or installation scripts. Use a Docker recipe for those operations, or prepare inputs outside the application and assemble them afterward. Archives and build output containing symlinks or special files are rejected; recipes may need to produce ordinary-file layouts for this proof of concept.

For source payloads, a single outer source directory is removed before assembly/build, as is common with GitHub source ZIPs. Release payloads retain their archive directory layout. Choose `destination` and `expected_files` to match the resulting package layout; an input is rejected if it would replace an existing payload file.

## Different recipes for each target

`target_overrides` can replace `mode`, `image`, `command`, `output_dir`, `network`, `expected_files`, `inputs`, `payload`, or `notes` for a particular target. Overrides use a shallow merge: replacing `inputs`, `expected_files`, or `payload` replaces that complete field. Nested overrides are not supported. Both the base recipe and each effective target recipe must validate.

For example, assemble Linux and Windows release archives while using a separate source build for a particular handheld:

```json
{
  "version": 1,
  "mode": "assemble",
  "inputs": [],
  "payload": {
    "kind": "release",
    "asset_patterns": {
      "linux": "^example-linux-x86_64\\.zip$",
      "windows": "^example-windows-x64\\.zip$"
    }
  },
  "steps": [],
  "expected_files": ["example-linux"],
  "target_overrides": {
    "windows": {
      "expected_files": ["example.exe"]
    },
    "handheld": {
      "mode": "docker",
      "payload": { "kind": "source" },
      "image": "example.invalid/handheld-toolchain@sha256:0000000000000000000000000000000000000000000000000000000000000000",
      "command": ["sh", "-c", "./build-port.sh --target my-device --output /work/out"],
      "output_dir": "out",
      "network": false,
      "expected_files": ["launch.sh"]
    }
  },
  "notes": "Schema example. Supply real assets, verified inputs, commands, and an image digest before running."
}
```

The dummy image/digest is not runnable. Add required local game inputs to the base or override recipes. In this example, the source and Linux/Windows assets are captured before target jobs begin. The handheld override does not request a release asset.

Use an `assemble` or `docker` base mode for a buildable project. Overrides cannot switch a capture-only recipe into a build recipe, or a build recipe into capture-only mode; validation rejects those combinations. Build recipes can switch between `assemble` and `docker` by target.

## Build in Docker

Docker execution requires `--enable-docker` and a working Docker installation on the server. Use an image that already contains the toolchain and dependencies needed to build offline.

```json
{
  "version": 1,
  "mode": "docker",
  "image": "example.invalid/port-toolchain@sha256:REPLACE_WITH_IMAGE_DIGEST",
  "command": ["sh", "-c", "./build-port.sh --assets /work/inputs/assets/game.bin --output /work/out"],
  "output_dir": "out",
  "network": false,
  "inputs": [
    {
      "path": "my-game/game.bin",
      "destination": "assets/game.bin",
      "sha256": "REPLACE_WITH_THE_64_CHARACTER_SHA256_OF_YOUR_INPUT"
    }
  ],
  "payload": { "kind": "source" },
  "steps": [],
  "expected_files": ["launch.sh", "assets/game.bin"],
  "notes": "Illustrative build command and image. Adapt to the project's source layout."
}
```

The image placeholder deliberately cannot run. Replace it with a real digest-pinned image and a reviewed build command. `command` is an argument array executed inside the container. Its first element overrides the image's `ENTRYPOINT`, and the remaining elements are arguments. Shell syntax is meaningful only when the selected container command explicitly invokes a shell.

The working directory is `/work/payload`, containing the unpacked source or selected release payload. Checked game inputs are copied to `/work/inputs/<destination>`. A recipe must create its output under `/work/<output_dir>`, which is `/work/out` in this example. The command must copy any needed inputs into its output; Docker inputs are not automatically packaged. In assembly mode, inputs instead go directly into the package at `<destination>`.

`output_dir` selects the directory to package after the command succeeds. `expected_files` are checked relative to that output directory. Preserve saves and configuration outside the replaceable package, and provide launch instructions as packaged files where useful.

Network access is disabled by default. Enabling it makes a build less reproducible and may leave dependencies outside the archived material. The proof of concept supports Linux containers; native Windows and macOS build runners are future work.

Without overrides, the saved command is shared by all selected targets. The proof of concept does not synthesize compilation commands or install cross-compilers. Use per-target release assets or override the command/image to provide the target-specific build behavior needed.

The root filesystem is read-only. `/work` is writable and `/tmp` is a writable 256 MiB tmpfs. The runner uses the host user's UID/GID, drops Linux capabilities, blocks privilege escalation, and limits CPU, memory, processes, execution time, and log size. Job workspaces are removed after the job finishes, so copy required diagnostics into the declared output or use the recorded log.

## Versioning and failures

The application records the recipe hash and input hashes alongside the upstream revision and package checksum. A release name alone cannot identify a build, because recipes and user inputs can change.

Each successful artifact remains in the archive. Publishing defaults to an explicit action after packaging and validation succeed. Optional project setting `auto_publish` publishes successful target builds automatically; it requires `auto_build` and a valid build recipe. Configure these settings in the recipe dialog. Each publish updates the current library ZIP for that project and target, and publishing a previous artifact provides rollback.

A missing input, hash mismatch, build failure, or missing expected file leaves that target's current published version intact. Other successful targets may still be published when automatic publication is enabled. Logs explain which stage needs attention. Structural validation does not establish gameplay compatibility.

Source capture is checkpointed before release assets are downloaded. If an asset download fails, the preserved source remains accessible, but that partial capture cannot be built. Check upstream again to obtain a complete capture before retrying the build.

If upstream installation instructions change, update the recipe and run it again before relying on unattended updates. The application does not infer new instructions or silently repair recipes.

## Recipe proposals

Configure an installed Ollama model at a loopback endpoint in **Settings → Recipe assistant**. In a worker's **Advanced** tab, describe a desired change and request a proposal. Review its explanation and JSON, then explicitly apply it. Normal jobs keep using the saved recipe until application.

The assistant receives bounded redacted logs and recipe context, not ROM contents or configured credentials. A proposal cannot change the base runner mode, base source/release payload kind, or verified private inputs. Input guards and private-path redaction also cover per-target overrides. Drafting never executes commands. Applying fails if the original saved recipe changed since the draft, so request a fresh proposal before proceeding.

## API build controls

All POST bodies require `Content-Type: application/json`. Worker IDs and capture IDs come from `/api/state` and job/capture records.

| Route | Body and behavior |
| --- | --- |
| `POST /api/projects/<id>/test-build` | `{}`; uses a complete compatible profile capture covering selected targets, or captures the profile's tested tag before building. |
| `POST /api/projects/<id>/check` | `{}` checks latest stable; `{"version":"9.3.0"}` captures the tested tag for a verified profile. Arbitrary explicit versions are not supported. |
| `POST /api/projects/<id>/build` | Optional `targets` subset and `capture_id`; the capture must belong to this project and be complete. Profile version/commit/runtime guards still apply. |
| `POST /api/jobs/<id>/retry` | `{}`; failed checks keep their requested tag and failed builds keep their selected capture. |

A tagged test build does not replace the pointer to an already preserved newer release. Adding a target recaptures the tested release when its required runtime is missing from older compatible captures.

## Optional toolchain preservation

An existing toolchain TAR/ZIP can be placed beneath `<data-dir>/toolchains` and registered with a descriptive identity:

```bash
curl -X POST http://127.0.0.1:8787/api/toolchains/config \
  -H 'Content-Type: application/json' \
  -d '{"identity":"my-reviewed-toolchain","material":"toolchain.tar"}'
```

Alternatively, `/api/toolchains/archive` accepts `{"image":"name@sha256:<digest>"}` for an already installed digest-pinned Docker image and saves it locally. It does not pull or run the image. A daemon must be accessible for saving; registration works without one. Existing size limits still apply.

Preservation exports include registered toolchain material but retain an honest closure report. Archiving an image alone does not prove an offline rebuild, and ordinary HTTP exports do not run verification. A saved bundle with actual offline-build proof can establish the scoped result; status requires a matching capture, scope, toolchain identity, and toolchain checksum. Saved bundles remain downloadable from the worker overview. The reviewed Shipwright profile's audited verified bundle is recorded in [validation](VALIDATION.md).

## Example: Dusklight

[`recipes/dusklight.json`](recipes/dusklight.json) packages the official [Dusklight](https://github.com/TwilitRealm/dusklight) release builds (Twilight Princess PC port) for Linux x86-64, Windows x64, and Linux ARM64 handhelds. Linux builds are single-file AppImages, so the recipe stores them as `Dusklight.AppImage`. Game data is not packaged: Dusklight asks for your own disc image (`.iso` or `.rvz`) on first launch. Create a custom project for `TwilitRealm/dusklight`, paste the recipe, then check upstream and build.
