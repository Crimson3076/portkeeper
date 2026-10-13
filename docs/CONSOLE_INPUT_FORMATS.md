# Console dump formats: preparation plan

Research date: 2026-10-13. This is a format roadmap, not a claim that every
format below is implemented or that an emulator's input support establishes a
native port's support. A port ultimately requires its own accepted game data,
region, revision and hashes. Keep those facts in its catalog definition.

## What is implemented for Wind Waker HD

The fixed `wwhd_extract` operation accepts a Cemu `.wua`, a ZIP containing a
decrypted game folder, or a separately selected decrypted folder. The folder
must contain `code/cking.rpx`, `content/`, and `meta/meta.xml`. It checks the
metadata title/version and the catalog's accepted RPX SHA-256 before including
`data/game` in the package. A ZIP may have an enclosing folder, but must have
exactly one supported version-0 game. Original folders and archives are never
modified. ZIP extraction uses Portkeeper's bounded, traversal-safe extraction.
The first launcher start still compiles the supplied game code on the player's
PC using the release's supported setup command. Packaging is not gameplay
verification.

Upstream also supports WUD/WUX with the appropriate disc and Wii U common keys.
Those formats are not yet accepted by this Portkeeper operation. A directory
containing encrypted `.app`/`.h3` content is a different input from a decrypted
`code/content/meta` folder.

Sources: [upstream README](https://github.com/ZeldaWWHDRecomp/ZeldaWWHDRecomp/blob/main/README.md),
[Cemu archive format announcement](https://cemu.info/changelog.html).

## Format families and candidate adapters

| Family | Common inputs to inventory | Preparation approach | Status / important limits |
| --- | --- | --- | --- |
| N64 | `.z64`, `.v64`, `.n64`, ROM inside ZIP | Detect byte order by header; normalize a private copy; check canonical ROM hashes before the port's asset tool | Next priority. Existing Portkeeper ROM hash code already has normalization support; apply consistently to pickers and staging. Extension alone cannot identify a valid game. |
| GameCube / Wii | ISO/GCM, RVZ, WIA, GCZ, CISO, WBFS, split images | Pinned DolphinTool reader/converter/extractor, then port-specific identity checks | Next priority for Dusklight. Dolphin's ability to play a format does not guarantee every conversion command accepts it. CISO/WBFS can discard disc data; conversion cannot recreate missing bytes. Do not silently scrub. |
| Wii U | WUA; decrypted `code/content/meta`; ZIP of that folder; WUD/WUX; encrypted NUS content | Release extractor for WUA; direct validated folder/ZIP; keyed disc adapter later; separate NUS adapter | Folder/ZIP/WUA implemented for the catalog recipe. Disc formats require private keys. NUS packages must be distinguished from already decrypted folders. |
| PS4 | PKG base/update, decrypted game folder; ISO containing PKG | Existing pinned PKG tool, private base copy and update overlay, or validated prepared folder | Implemented for matching catalog recipes. Retail PFS encryption can lack required key material. Unwrapping an ISO does not decrypt the PKG inside. Actual GOW3 encrypted-base failures remain blockers. |
| PS1 / Saturn / Sega CD | BIN+CUE, CHD; multi-track sets | `chdman info/verify`, `extractcd`, preserve track layout/audio and required sidecars | Future adapter. A multi-track CD must not be flattened into a generic DVD ISO. Native ports may require one track or individual assets rather than a disc image. |
| Dreamcast | GDI+track files, CHD; other image containers | CHD optical adapter preserving track mapping; title-specific extraction | Future. Keep track files and descriptors together; converter command must match media type. |
| PS2 | ISO, BIN+CUE, CHD; some CSO/ZSO inputs | CHD media-specific extraction or maxcso decompression into private image, then OpenGOAL/other port tool | Existing OpenGOAL ISO extraction; extra compressed-container adapters planned. Check the exact accepted revision after conversion. |
| PSP | ISO, CSO/ZSO/DAX, PBP | maxcso for supported compressed ISO variants; separate PBP inspection/extraction | Future. PBP is a package container and is not interchangeable with ISO. |
| Original Xbox | XISO/XDVDFS, extracted folder; larger raw dumps | pinned extract-xiso/XDVDFS adapter, then executable/content checks | Future, tied to an actual native port recipe. `.iso` alone does not establish Xbox filesystem type. |
| PS3 | Decrypted disc folder, disc image, PKG and license-dependent content | Separate disc/filesystem/package adapters, with title/version and decrypted executable checks | Research required before implementation. PS4 PKG tooling must not be reused merely because the extension matches. |
| Nintendo 3DS | CCI/3DS, CIA, CXI and extracted filesystem | Dedicated container reader, privately supplied keys where needed | Later phase. ninfs documents container and key requirements; it is a FUSE mounter, not automatically a container-friendly batch extractor. |
| Nintendo Switch | XCI, NSP, NCA and extracted content | Dedicated content/archive tooling with user's key material and title/update relationships | Later phase. hactool documents extraction and keysets; format support alone cannot remove key requirements. |
| Other cartridges | NES/SNES/GB/GBC/GBA/Mega Drive/DS and others | Detect format/header; preserve raw data; optional documented header removal/byte-order conversion | Add as ports need them. No generic conversion should discard headers or alter bytes without catalog evidence and accepted hashes. |
| Outer wrappers | ZIP, TAR, 7z/RAR and multipart archives | bounded extraction, then rediscover the inner console format | ZIP/TAR safe extraction already exists. 7z/RAR/multipart need pinned helpers, volume checks, path/link defenses and expansion limits before general enablement. |

Primary tool references:

- [Dolphin supported dump formats](https://alwaysdata.dolphin-emu.org/docs/faq/),
  [DolphinTool commands](https://github.com/dolphin-emu/dolphin/blob/master/Readme.md),
  [RVZ/WIA specification](https://github.com/dolphin-emu/dolphin/blob/master/docs/WiaAndRvz.md).
- [MAME chdman: media-specific conversion and verification](https://docs.mamedev.org/tools/chdman.html).
- [maxcso: supported input variants and decompression](https://github.com/unknownbrackets/maxcso).
- [XboxDev extract-xiso](https://github.com/XboxDev/extract-xiso).
- [ninfs: Nintendo filesystem formats and requirements](https://github.com/ihaveamac/ninfs).
- [hactool: Switch containers and keyset handling](https://github.com/SciresM/hactool).
- [LibOrbisPkg: PKG encryption requirements](https://github.com/maxton/LibOrbisPkg/wiki/PKG-Information).
- [Shipwright: supported game hashes and asset preparation](https://github.com/HarbourMasters/Shipwright/blob/develop/README.md).

## Implementation order

1. Make matching catalog preparation available to both local and cloud models.
   If the person requested preparation, reject a runtime-only draft when a
   matching fixed preparation tool exists. Test builds must explicitly report
   skipped game-dependent steps rather than presenting them as verified.
2. Wii U decrypted folder and ZIP: immediately useful for the existing owned
   Wind Waker dump; validate metadata, RPX hash, size and tree safety.
3. Shared format inspection: magic/container/metadata, not filenames. Show
   `ready`, `needs conversion`, `needs key`, `wrong region/version`, or
   `unsupported` with a concrete reason. Directory structure is evidence to
   inspect, not a sufficient game identity check.
4. N64 byte-order normalization and DolphinTool conversions, with pinned tool
   version/hash and fixture round-trips. Test each supported input mode; do not
   infer conversion support from an emulator's advertised playback formats.
5. CHD and compressed ISO adapters. Select optical media type from metadata,
   preserve multi-track sidecars and record input plus canonical output hashes.
6. Private key handling, then encrypted formats. Keys must never go in model
   prompts, project JSON, GitHub, finished packages or diagnostic logs. Require
   explicit local key selection, private storage/mounts and sanitized command
   diagnostics. Adding a key field does not guarantee successful decryption.

Every adapter needs bounded execution, free-space preflight, private output,
original preservation, title/version/hash validation, cache identity including
source content and adapter version, and actionable failure messages. Windows
startup and gameplay remain separate validation states after packaging.
