# Starship game-containing packages

The reviewed v2.0.0 definition requires a supported, user-supplied Star Fox 64 USA ROM. A runtime ZIP's small `starship.o2r` contains port resources; it does not replace the generated `sf64.o2r` game archive.

Portkeeper stages and normalizes a private ROM copy, checks its supported SHA-1, and runs a fixed Torch CLI against the release's `config.yml` and `assets/` extraction metadata. The CLI is built from Torch commit `f75facb20883570ed091e8ae733ec0539f606e57`, the revision pinned by Starship v2.0.0. Its source download is checked against a fixed SHA-256; recipes cannot select arbitrary tool source or shell commands. First use builds the extractor with CMake; subsequent uses check and reuse the cached binary. Network access is needed for the initial public tool dependencies.

The generated ZIP archive is checked for entry count, paths, size limits and CRC before inclusion as `sf64.o2r` in both Linux and Windows packages. The Linux launcher sets `SHIP_HOME` to the package directory and enables AppImage extraction without FUSE. The Windows launcher changes to its own directory. Existing runtime-only packages must be rebuilt with this definition; downloading an old package will still omit game data.

The original ROM is preserved. Generated game assets are private and must never be committed to the catalog or included in model prompts. Successful extraction and ZIP verification establish packaging success; native Windows startup and gameplay remain separate checks.

Model drafting receives the generic tool interface and upstream evidence, not a prewritten Starship recipe. A matched preparation definition lets server-side validation reject runtime-only drafts when game preparation is requested. Local model drafts still require review and a build test.
