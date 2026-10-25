# Preparing PS4 game files

Portkeeper can prepare extractable PS4 PKG dumps with its fixed, checksummed
LibOrbisPkg tool. It reads your base game and matching update from the configured
game files folder, extracts private copies, applies the update, checks the result,
and includes the prepared folder in the port's download. Original PKGs are preserved.

## Put your files where the container can see them

Move or copy your own files into your ROMs share. For example:

```text
/mnt/user/Roms/ps4/roms/CUSA01623/base.pkg
/mnt/user/Roms/ps4/roms/CUSA01623/update-01.02.pkg
```

Use your actual share path and spelling. In Unraid's container settings, map that
ROMs root to **Game inputs → `/inputs`**, with **read-only** access. If your current
mapping already points at that folder, keep it. With the example mapping above,
Portkeeper sees `ps4/roms/CUSA01623/base.pkg` and
`ps4/roms/CUSA01623/update-01.02.pkg`. If the mapped host path is already the PS4
folder, the relative paths will be shorter. The configured input root appears in
the file picker when no match is found. Files must be readable by the container's
PUID/PGID, and copying must finish before you select or build them.

Renaming is optional. The picker reads bounded PKG header metadata to identify the
title and base/update role; a filename containing "update" does not decide the role.
When several copies match, select the one you want. A header match is a candidate,
not proof that the data can be extracted or played.

## God of War III Remastered

The bundled Windows packaging candidate requires **CUSA01623, version 01.02** and
the `eboot.bin` SHA-256 documented by upstream. A different region or version is
rejected. These steps were checked against `windows-v2.1.5` source and runtime
archive; native Windows startup and gameplay remain untested.

1. [Update the Unraid container](UNRAID_UPDATES.md) and refresh its WebUI.
2. Open your GOW3 game → **Advanced → Review PKG packaging steps**. Review
   **Changes** or **JSON**, then **Validate** and save. This replaces that project's
   future build instructions; existing packages and test results remain available.
   The complete candidate is also in
   [`catalog/definitions/gow3-pc-windows.json`](../catalog/definitions/gow3-pc-windows.json).
3. Under **3. Your game files**, choose the **base game PKG** and the matching
   **01.02 update PKG**. **Choose a file** lists matching files with metadata;
   **Save path** and **Find automatically** remain available.
4. Under **Updates**, check for the release if necessary, then build the Windows
   package. Build logs show checksum checks, copying, extraction and validation.
   The first build can take time and produce a ZIP tens of GB in size.
5. Download and extract the entire ZIP to a writable folder on Windows. Run
   **Launch.cmd**, then press **PLAY** in the launcher. **Play.cmd** uses the saved
   settings for later starts. The package includes `game/`, seeds the launcher
   configuration, and keeps settings in `data/config/` and saves/cache in `user/`.
   Launching the EXE directly bypasses the wrapper's environment settings.

You can use the bundled steps without asking Qwen to rediscover them. To redraft,
leave **Prepare and include my game files when supported** checked. The prompt,
starter JSON and automatic corrections require the documented checks, separate
base/update inputs and inclusion of the prepared folder when sufficient evidence
exists. The fresh model review checks source claims; it does not test gameplay.
The guidance is shared across games; title-specific requirements live in each
definition and upstream documentation. Turn the checkbox off for a runtime-only
package that requires an already prepared game folder on the PC where you play.

The automated checks include a real pinned-tool extraction of tiny synthetic base
and update PKGs in the container, with network access disabled. It checks the
update format workaround, final version/hash, unchanged originals and cache reuse.
CI runs this separately from the ordinary Python and browser interaction suites.
It does not substitute for a build with your actual dumps or a Windows launch test.

## What happens to a separate update

The `ps4_pkg_extract` operation lists inputs in order: **base first, updates after
it**. Portkeeper extracts each into a separate work folder and verifies its title
and version before applying it. An update replaces same-named files and retains
base-only files. The final folder must match the definition's exact title, app
version and accepted executable hash before it is cached or packaged.

For GOW3, a 01.00 base needs the separate matching 01.02 update. An optional update
may be omitted only if the base dump already contains the required version and
passes final validation. The app reports the missing update early when the header
reveals an older base. Unknown header versions are checked after extraction.
Selecting a new PKG clears a manually selected prepared-folder alternative.

The definition can declare several ordered update inputs. Known downgrades are
rejected, including after extraction. Only full, extractable patch filesystems are
supported; explicitly marked binary delta updates are rejected. This is an
overlay, not a PS4 system installer, and does not apply arbitrary binary deltas.
Some complete updates use the DP package type: the runner extracts their filesystem
and named `sce_sys` entries separately because the pinned tool's GP4 command does
not support that project type. This retains `param.sfo` for version checks and
other recognized header files such as icons and change information.

## Storage, reuse and limits

Portkeeper hashes the complete selected PKGs, checks its private copies, and keys
the extraction cache by ordered content hashes, pinned tool hash and required game
checks. Changed PKG contents cannot reuse that extraction entry. A cached folder
is checked again before use. A previously prepared folder under the configured bulk
`prepared/` directory (`/storage/prepared/` in new installs)
can also be selected explicitly if it passes the same checks.

Allow roughly **twice the combined PKG size free in bulk storage**, plus room for
existing archives. Actual extraction size can vary; the preflight estimate is not
a guarantee. The process needs space for a private PKG copy, extracted files and
the final ZIP. `include_tree` streams cached game files into that ZIP without a
second full folder copy. Originals stay in your ROMs share; cached extraction and
build artifacts live in bulk storage. Failed work is removed; validated cache entries
are retained for reuse. Old extraction entries are not automatically pruned.

Encrypted retail PKGs are not universally extractable. This workflow cannot
recover missing decryption information and does not offer a passcode field. Use
an extractable dump or a correctly prepared folder from your own game. A plain ISO
is not accepted by this PS4 extraction operation.
If PkgTool reports that the PFS image is encrypted with no decryption key,
Portkeeper stops and preserves the tool log. Successfully reading the PKG's
TITLE_ID and APP_VER does not prove that its game filesystem is extractable.
The prepared-folder alternative still checks title, version and eboot hash;
selecting it does not bypass those requirements.

Your PKGs and extracted game data are not sent to the model or committed to GitHub.
The model receives public upstream release/source material. A completed download
contains your game data; sending it to RomM is a separate publication choice.
Extraction and packaging checks establish structure and required identities,
not native Windows compatibility, first-launch success or gameplay.
