# Packaging definitions

A packaging definition is the editable JSON source of truth for how a project becomes a
playable, portable download. The readable packaging instructions under **Advanced** are generated from it, so the
readable steps and the steps Portkeeper runs cannot drift apart. Bundled definitions live in
[`catalog/definitions/`](../catalog/definitions); each project keeps its own copy, which you can
edit in **Advanced → Edit packaging definition**.

Definitions select and parameterize a fixed set of operations and tools that Portkeeper
implements. They cannot add shell commands. Launcher fields accept only plain path-like text,
with `$PWD` (shell) or `%CD%`/`%~dp0` (cmd) as the only variables.

## Runners

| `runner` | Meaning |
| --- | --- |
| `operations` | Portkeeper runs the definition's `operations` to build each platform package. |
| `profile` | A reviewed, fixed build profile (currently Ship of Harkinian 9.3.0) does the build. The definition documents it. |
| `recipe` | A schema 1 recipe saved before definitions existed (for example the Dusklight project). It runs exactly as before. |
| `capture` | Releases are preserved only. No download is produced. |

Projects saved before schema 2 are migrated on startup without changing what they do. A
capture-only project whose repository has a bundled `operations` definition adopts it, which
makes it buildable; automatic build and publication settings are not changed. The previous
recipe is kept in `legacy_recipe` and earlier notes move to `history`.

## Fields

| Field | Purpose |
| --- | --- |
| `schema`, `id`, `revision` | Schema 2, a stable ID and a revision. Any content change gets a new revision, so packages and their tests are never confused with older ones. |
| `runner_version` | The runner support the definition needs. This build provides version 1. |
| `upstream` | `repository`, `documentation` links and `release_rules` (`stable_only`, `tag_pattern`, `notes`). |
| `targets` | Platforms the definition can produce (`linux`, `windows`, `handheld`). A project builds a subset. |
| `downloads` | Release files by ID: `asset_pattern` (must match exactly one file), `platform`, `format`, `required_members` and `strip_prefix`. A download from another repository pins `repository`, `tag`, `asset` and `sha256`. |
| `inputs` | Files you provide: `label`, `kind` (`file` or `directory`), `platform_hint`/`name_hint` for automatic search, `normalize` (`n64_big_endian`), `accepted` hashes, `verify` (`hash` or `downstream`), `max_bytes`. |
| `source_checks` | Text that must still appear in a new release's source, or `contains_input_hash` to require that upstream still lists your input's hash. |
| `operations` | Ordered build steps (below). |
| `outputs` | Per platform: `expected_files`, `compression` (`deflate` or `stored`) and `max_bytes`. |
| `first_run` | What the package does about the game's first-launch setup: `mode` is `ready`, `preloaded` (your file is placed where the game looks for it), `generated` (files are generated during the build), `configured` (settings or launcher arguments point the game at its data) or `asks_user` (the game still asks for something), plus a `summary`. Shown when adding the game and on its page. |
| `launch`, `saves`, `dependencies`, `limitations` | Launch command and instructions, where saves/settings live, host requirements and known limitations. They are shown in the UI and copied into each package manifest. |
| `verified_releases` | Reviewed releases with the SHA-256 of each upstream file and the evidence. |
| `history` | Provenance notes, such as how a package was assembled before automation. |

`strip_prefix` removes an enclosing archive folder. For a ZIP containing
`game-windows/Game.exe`, use `strip_prefix: "game-windows"` and
`required_members: ["Game.exe"]`. Without stripping, the package retains
`game-windows/`. Expected output paths are relative to the final package.
`required_members` and `expected_files` use literal paths, not wildcards:
choose an actual filename or an expected output directory such as `bin/`, never `bin/*.dll`.
`required_members` is a JSON array of at most 50 entries; usually 2–10 essential
files are enough. It checks compatibility and does not filter extraction: all archive
files are unpacked. `expected_files` has at most 100 entries; executables, generated
launchers and key directories usually make a clearer check than an entire file manifest.

Inputs are files needed on the Portkeeper server during packaging. If a Windows
launcher asks the player to choose an existing game folder on their PC, keep
`inputs: []` for a runtime-only package and describe it with `first_run.mode: "asks_user"`.
When supported preparation is selected, declare the owned file inputs and a fixed
extraction tool, include the resulting data and configure the documented launcher.
See [PS4 base/update preparation and Unraid file placement](PS4_GAME_FILES.md).
Directory inputs
must be consumed by the fixed PS4 extraction tool as `prepared_input`.
The **Your game files** section shows these first-launch requirements separately
from files uploaded for packaging. An empty `inputs` list does not mean the player
can run the game without their own game files.

## Operations

Paths start with `$package` (the platform package being assembled) or `$shared` (a work
area shared by all platforms in one build). Steps that write only to `$shared`, and all tools,
run once before the platform packages.

| `op` | Effect |
| --- | --- |
| `unpack_download` | Extract a ZIP/TAR download (or copy a single file, optionally as `file_name`). Windows backslash paths are accepted. |
| `stage_input` | Copy a verified input, normalizing N64 byte order when declared, and check the copy again. |
| `copy` | Copy a file or folder, for example a generated archive from `$shared` into the package. |
| `include_tree` | Add a large `$shared` folder to the ZIP under a name without copying it. |
| `launcher` | Write a `posix` or `windows_cmd` launcher from structured fields: `exec`, `args`, `env`, `mkdir`, `run_python`, `chmod_exec`, `prepare_once`. `prepare_once: {exec, args, unless_exists}` runs a release-provided setup command before `exec` only while `unless_exists` is missing, and stops the launcher if it fails. |
| `helper` | Add a reviewed helper script shipped with Portkeeper (`bbport_portable_paths`, `bbport_nvidia_libraries`). |
| `write_text`, `touch` | Write a README or configuration file, or an empty marker such as `portable.txt`. `{version}`, `{target}`, `{name}`, `{repository}` and `{launch_command}` are replaced. |
| `tool` | Run one of the tools below. |

## Tools

Tools run official upstream programs from checksummed captures with fixed argument lists,
bounded time and bounded logs, inside the Portkeeper container.

| Tool | What it does | Image support |
| --- | --- | --- |
| `ship_o2r_headless` | Runs an official Ship/2Ship AppImage once with your ROM under a virtual display and stops it after the `.o2r` archive is complete and has at least `min_entries` entries. | Xvfb and Mesa |
| `opengoal_extract` | Runs the release's OpenGOAL `extractor` with `-e -v -d -c` (extract, validate, decompile, compile) on your ISO and keeps the listed output folders. | — |
| `ps4_pkg_extract` | Copies PS4 PKGs privately and extracts them in base/update order with pinned LibOrbisPkg. Uses `pkg_makegp4` for supported package types, or `pkg_extract` plus fixed named-header extraction for complete DP updates. Checks extracted title, version and accepted `eboot.bin` hash before overlaying and caching. Reuses a validated earlier extraction or `prepared/` folder. | OpenSSL 1.1 libraries for PkgTool |

| `wwhd_extract` | Runs the release's `wwhd-extract` on your Cemu archive (`.wua`): asks for each listed title ID in order, requires version 0, extracts `code/`, `content/` and `meta/`, and requires an accepted `code/cking.rpx` SHA-256. Disc images need keys and are not supported. | — |

## New releases

When **Check upstream** preserves a release, Portkeeper applies the definition's rules:

1. The tag must match `release_rules.tag_pattern`; only stable releases are checked.
2. Every download must match exactly one release file.
3. A release listed in `verified_releases` must have byte-identical files.
4. Archives must still contain their `required_members`.
5. `source_checks` must pass against the preserved source.

A release that passes is **compatible**: with **Automatically build compatible new releases**
on, it is packaged at once, and its packages start **Not tested**. Anything else is preserved
and marked **Needs review** with the exact problems, and a notification lists them. It is not
packaged until the definition is updated. During a build, a missing or mismatched input, a tool
that no longer produces its output, or a missing expected file fails that platform's build and
leaves earlier packages untouched. Nothing marks a new release verified.

Each release in **Releases & packaging status** shows whether it is only **Preserved**,
**Needs review**, **Packaged** (with platforms), or also **In RomM**.

## Package names and rebuilding the same version

Package files are named `<prefix>-<version>-<platform>-<YYYY-MM-DD>.zip`, the date being the build date.

A build is identified by the definition, the release (commit and file checksums), the selected game files
(path, size and modification time) and the platform. Building the same release again with nothing
changed does nothing and says so. When something changed, the new package replaces the earlier package
of the same version and platform, unless that one is pinned, known-good, has a recorded test result,
was copied to the library or is in RomM; those are kept and the log says why.

Every launcher's program must exist in the package, so a definition cannot produce a download whose
launcher points at nothing.

## Adding a game

**＋ Add a game** lists the catalog's games with what each needs. Choosing one searches the RomM library
folder (`/inputs`) for each needed file, verifies it, and explains a wrong version or a missing file in plain
words. **Build and send to RomM** creates the game, preserves the latest release, builds the chosen
platforms and uploads them to RomM in one step. The destination and future-update controls separately choose whether to upload now, automatically build later, or automatically build and upload later. New games default to keeping downloads in Portkeeper and waiting for your build decision.

**Custom game** looks up any GitHub repository, saves its latest release, and then either drafts packaging
steps with a model, prepares a prompt for your own chat or agent, opens the definition editor, or only keeps
releases.

## Drafting with a model

For unfamiliar projects, start with [Investigate before drafting](INVESTIGATIONS.md):
bounded evidence retrieval, a reviewable plan, capability checks and explicit
unsupported outcomes. Supported plans can then become checked packaging JSON.

Settings → **Drafting assistant** chooses how drafts are made. All of it is optional; catalog games never need it.

- **My own chat or agent** (default): no connection. A game's **Draft packaging steps** card copies a prompt,
  or downloads a folder (`INSTRUCTIONS.md`, `PROMPT.md`, `FORMAT.md`, examples and the project material) for an
  agent such as Claude Code. Paste the JSON answer back. This works with a chat subscription and no API key.
- **Local model with Ollama**: an address on loopback or the private LAN, a model and a context size. The
  context size is sent as `num_ctx`; use 32768 or more. A 30B-class coding model fits a 32 GB GPU.
- **Claude API**: an API key; defaults to `claude-opus-5-5` with structured output and server-side fallback.
- **Another API (OpenAI-compatible)**: a base URL (https, or http on the private LAN), model and optional key.

The model receives the release's file names and the member lists of its platform archives, the README and
documentation, the file tree and short source excerpts that mention ROMs, extraction or portable mode.
Never game files, local paths or tokens. The material is marked as untrusted data. Replies are validated
as definitions; a rejected draft is sent back with the reason, up to three attempts. Accepted hashes must
appear in the material. Drafts are proposals: nothing runs until a person approves one, and the approved
definition then needs a successful build like any other.

The prompt includes a small worked example for an archive with an enclosing folder
and a launcher that asks for game files, plus a final checklist. Archive summaries
show enclosing folders; only the first 300 member names per download are sent to
the model to keep the prompt manageable. Connected drafts are checked against full
manifests of up to 30,000 members. For simple unpack-and-launch definitions, Portkeeper
also checks whether expected files and launcher executables would appear at their
declared paths. These errors are sent back to Ollama or another connected provider
for correction before a proposal is saved. Unavailable or incomplete inspections
do not prove a file is missing, and complex tool workflows still need build validation.
These checks do not launch the game or establish gameplay compatibility.

Connected drafting follows one guided draft with up to three attempts to correct
automatic-check failures, followed by one model review in a fresh conversation.
Portkeeper provides an editable starter definition with the repository, chosen platforms,
unambiguous release downloads and enclosing folders already filled in. It leaves launcher
choice and first-launch requirements for the model to establish from the project material.
Several downloads for one OS remain an explicit unresolved choice rather than a guessed asset.

The fresh review checks source claims about game files, first launch, dependencies,
settings and saves. It does not see the drafting explanation or correction history.
Findings show their quoted evidence; Portkeeper checks that each quote occurs in the
supplied README, documentation or source excerpt. An unmatched issue becomes **Uncertain**.
A matching quote still needs a person to judge whether the interpretation is correct.
The same configured model performs both passes; this is an advisory review, not independent
verification or a quality score. A failed model review preserves the structurally valid
draft with a **Model review unavailable** warning. Imported chat drafts keep their normal
manual-review flow and do not automatically make a model call.

Normally a connected draft uses one drafting call and one review call; rejected drafts
use additional correction calls. The provider and model settings are held constant for
that run. **View starter JSON**, **View review JSON**, and **Download drafting record (JSON)**
keep the details open for inspection. Run records are stored under `/data/drafts/` and include
public material, source/archive checksums, model name/settings, prompts, original model replies,
validation errors and findings. They exclude application credentials and private game-file
locations. Failed attempts can also be downloaded. Model tags can change over time; a recorded
model name alone does not guarantee the same weights on a later run.

### Test builds and the correction loop

After a connected draft passes the automatic checks, Portkeeper test-builds it against the preserved
release in a throwaway folder (nothing is archived or published). A failed test build is sent back to the
model with the error, the produced top-level entries and file listing, for up to two rounds. Steps that need
game files you have not selected are skipped; files they would produce are reported as unverified, not
missing. The review card shows the test results. Turn this off with the `draft_test_builds` setting.

### Outside agents (MCP)

Settings → **Drafting assistant** → **Connect an outside agent** creates a token for Portkeeper's MCP endpoint
(streamable HTTP at `/mcp`, `Authorization: Bearer <token>`). Only the token's SHA-256 is stored; creating a
new token replaces the old one, and turning the connection off removes it. Agents get the same tools as the
built-in assistant: `packaging_format`, `list_games`, `read_game`, `read_material`, `read_source`,
`list_release_archive`, `validate_definition`, `test_build_draft`, `get_test_build` and `submit_draft`.
They cannot approve, publish, choose game files, change settings or delete anything, and `read_game`
reports only whether a game file is selected, never where it is. Submitted drafts land in the normal review
queue with the agent's name and their test results.

Draft quality, a successful package build and recorded launch/gameplay tests remain separate.
Neither a clean automatic check nor a model review marks a package as gameplay tested.

## Manual actions

- **Build packages** (on a game's Play page) checks upstream and builds. Uploading is a separate action unless the game already has automatic publication enabled.
- **Check for a new version** under **Updates** preserves the latest stable release and applies the rules above.
- **Build packages…** under **Updates** chooses a preserved release and platforms.
- **Build progress & logs** under **Updates** shows each job's stages, logs and errors.
- **Send to RomM** (Updates tab) uploads a package with RomM's chunked upload API
  into the Linux or Windows platform. The token needs `roms.write`. RomM creates the library
  entry after its filesystem watcher rescans (`ENABLE_RESCAN_ON_FILESYSTEM_CHANGE`, five minutes
  by default); Portkeeper checks on the RomM check schedule (every minute for 30 minutes after an upload, then backing off) and records the RomM entry. An existing file name
  is never overwritten: a different package gets a `-r2` style name.
- **Copy to local library folder** is separate. It reaches RomM only when that folder is RomM's
  own platform folder.

Per project, **Updates → Automation** sets automatic builds and automatic publication to RomM (default)
or to the local library folder.

## Catalog

For more projects, **Add a game → Discover more ports** reads references from
[Recomp Launcher](https://recomplauncher.com/top). You can also paste one of its
game pages into Custom game. Existing packaging steps are reused when a
repository matches; unfamiliar projects go through upstream investigation and
your chosen setup method. Reference listings do not establish platform support
or packaging verification. See [discovery behavior, local JSON records and
current limitations](DISCOVERY.md).

The GitHub catalog (`catalog/projects.json` and `catalog/definitions/`) is fetched daily from
`raw.githubusercontent.com` and cached under `<data-dir>/catalog/`. Invalid or unavailable
data keeps the last valid cache; on first use the catalog bundled in the image is used.
**Settings → Advanced → Catalog** shows the source, cache age, last error and a **Refresh catalog** button.

Definitions that use only the operations, helpers and tools listed here update from the catalog
without a container update. A project offers **Review catalog changes** when a newer revision
exists; nothing is applied automatically. A definition that needs a newer `runner_version`, a
new operation, helper or tool is listed as needing a Portkeeper update and is not used.

## Verification is separate

Packaging success, download test results and catalog verification are independent. See
[project verification](PROJECT_VERIFICATION.md). A rebuilt package starts **Not tested**, even
when an earlier package of the same release was tested. Wine/Proton results on a Windows
download are labeled as such and do not change its native result.

## Editing and restoring definitions

The editor has Settings, JSON and Changes views of the same definition. Guided edits preserve all other fields. Validate checks structure and project compatibility without saving or building. JSON can be imported, formatted, copied and exported. **Copy JSON** copies the full definition or the editor's current unsaved text, including on an HTTP Unraid connection. Background updates preserve the JSON viewer's scroll and selection. Saving a changed definition retains the previous content in Saved revisions (up to 20). Review & restore loads that content as a draft; saving creates a new revision. Catalog changes also open as a draft comparison, so local edits are never replaced just by opening the preview. Automation controls remain separate under Updates.

## Platform detection and changing selections

Custom game lookup reads release filenames and the release commit's README plus up to five
linked first-party build guides. Detected platforms show the supporting filename or
instruction. Suggestions prefer platforms with release downloads. If there are none,
documented build instructions are suggested instead. References and comparisons with
another OS are shown as mentions and do not establish support. A Windows-specific guide
comparing its code with Linux therefore does not select Linux. Android, macOS and iOS are reported separately; they are not translated into
Handheld and this runner does not package those OS targets. An architecture label such as
ARM64 alone does not identify an operating system. Unknown support leaves the selection
empty for a manual choice. Detection is a suggestion, not launch or gameplay verification;
a documented source build also does not guarantee a downloadable runtime.

For an existing game, open **Advanced → Platforms to build**. **Detect supported platforms**
loads suggestions from the latest release, and **Save platforms** applies your reviewed
selection without starting a build. A preserve-only game can add an omitted platform and
then draft packaging steps again. For a game with packaging steps, only platforms already
covered by those steps can be enabled; edit or redraft the definition before adding another.

Scheduled release checks also watch for new OS downloads. The first successful check
establishes a baseline; a later Linux, Windows or other OS download creates a notification
without changing the platforms you build. Under **Updates**, opt into **Automatically draft
steps for newly released platforms** to ask your connected model to prepare a draft when
a new Linux or Windows download appears. This option is off by default and uses your
model's normal usage or credits. Existing pending drafts take precedence. Review and
approve the draft to enable the additional platform; detection and drafting alone never
build or upload it. Android, macOS and iOS releases notify you but cannot be packaged by
this runner. Detection depends on recognizable OS filenames; an unlabeled asset still
needs a manual review.

## Draft path errors and game directories

Definition path fields use relative POSIX paths with forward slashes, even for Windows
packages. `launch.command` is a filename, not an entire command line. Omit optional fields
such as `strip_prefix` when unused. Validation identifies the field that failed and sends
that detail back to the model on retries. Every selected platform must remain in its draft.

`stage_input` stages one declared **file**, not a directory. A runtime that asks the player
to choose an existing game directory should describe that choice with
`first_run.mode: asks_user`; a model must not invent an operation to copy or process the
folder. Directory inputs currently serve the fixed PS4 extraction tool's prepared-data
workflow. These rules do not automatically preconfigure a game's launcher or prove that
a model-generated package will work.
