# Project verification and catalog maintenance

A **verified project** has a reviewed, repeatable packaging procedure and a successful startup check for a named release, package revision, platform and device. It does **not** mean every platform works or that gameplay is confirmed. Prefer the UI label **Verified packaging profile**; show the verification scope beside it.

Verification is a recorded maintainer decision, not a status an LLM infers from a successful command, an upstream release, a source capture, or a downloaded ZIP.

## Required evidence

Before proposing verification, record all of the following:

1. **Identity and provenance:** upstream repository, exact release and source commit when applicable, runtime SHA-256, package revision, supported game-input revisions/hashes, and any local fixes. Never use an unpinned `latest` asset as evidence.
2. **Repeatable procedure:** extraction/build/assembly steps, required tools, expected outputs, and how another person can reproduce the package. State whether the procedure is an installed automatic build profile, a custom recipe, or a documented manual procedure. Source preservation alone does not establish build support.
3. **Integrity:** completed archive validation, SHA-256 of the exact tested archive, required assets present, and original inputs preserved. A ZIP that only contains a launcher or source code is insufficient.
4. **Portability:** extract to a fresh writable directory and launch through the included launcher. Record how saves/settings remain inside the package, required host dependencies, and any deliberate exceptions. A move to another directory must not depend on the packager's original absolute paths or pre-existing caches. Report incomplete portability as a limitation; do not call it portable without evidence.
5. **Startup:** the exact package starts, loads the required game assets and reaches a usable menu or game startup state without a fatal error. Record the actual observation, device, operating system, graphics driver, duration and intentional timeout if used. Logs can establish renderer/asset startup; do not claim a visually inspected menu or gameplay from logs alone.
6. **Limitations:** known issues, unsupported devices, packaging-only platforms, and checks not performed. Fedora x86-64 evidence does not establish Steam Deck, AMD, Intel, ARM, macOS or Windows compatibility.
7. **Approval:** a human maintainer explicitly approves the stated version, revision and platform scope, or has explicitly delegated that decision to a named reviewer under this policy. Record who approved, when, the scope, and a durable approval reference.

A project can be verified for Linux while Windows remains packaged but untested. List those scopes separately. Unknown information must say **unknown**, not silently count as a pass.

## Independent download test results

- **Not tested:** no startup or gameplay check for this exact download.
- **Starts successfully:** startup checked; gameplay unconfirmed.
- **Played successfully:** an explicitly recorded gameplay check, including device, what was played and observed limitations. Prefer a save/load check before claiming normal play; record whether it was performed.
- **Doesn't work:** a recorded failure with a description and device.

Bind results to the archive checksum or RomM file signature. Do not copy a positive result between platforms, package revisions, runtime releases, or changed game assets. A new download starts untested. Retain earlier failures in history; a superseding result applies only to the tested revision. GUI-only, software-rendered and headless checks must be labeled as such.

## Windows evidence without a Windows host

Static checks on Linux can validate ZIP integrity, PE x86/x64 architecture, included assets, expected DLLs, launcher paths and declared Windows requirements. Mark this **Package checked — native Windows not tested**. These checks cannot establish working driver, API, graphics, audio, input or save behavior on Windows.

Wine or Proton can provide useful startup/gameplay evidence. Label it **Starts under Wine/Proton** or **Gameplay checked under Wine/Proton**, recording the compatibility-layer version, Linux host and driver. It is a separate compatibility scope, not native Windows verification. A real Windows machine, Windows VM, or remote Windows tester can provide native evidence; a VM verifies only its actual hardware/graphics configuration and may lack the required GPU support. Do not transfer a Linux/Wine result to the native Windows download status.

A Windows catalog scope cannot be promoted to verified on static checks or Wine/Proton evidence alone. Native Windows startup and the other required checks remain necessary. Packaging-only Windows support can still be listed clearly.

## GitHub workflow for humans and LLMs

1. Read this policy and the existing catalog entry before changing it.
2. Add or revise an entry in `catalog/projects.json` with status `candidate`; collect evidence references. An LLM may prepare and publish a catalog change within the maintainer's authorization.
3. Open a PR describing the exact scope, evidence, outstanding checks, and approval reference. Another LLM can gather evidence or review the proposal; it cannot invent test results, devices, hashes, dates or human approval.
4. Promote only the approved platform scopes to `verified`. A release, package or procedure change needs its own evidence and approval. Approval for an earlier revision does not automatically approve the new one.
5. When a reproducible problem invalidates a verified scope, mark that scope `needs_review` and retain the historical evidence. Cosmetic catalog edits do not require game revalidation.
6. Keep credentials, private host paths, ROMs, game assets and saves out of the public catalog and PR. Use redacted logs and archive hashes; permission to package private inputs is not permission to publish them on GitHub.

## Catalog contract

`catalog/projects.json` is the human-editable catalog source. Schema version 1 uses a `projects` array. Each project has `id`, `name`, `repository`, `procedure_kind`, `build_profile_available`, `verification_scopes`, and `notes`.

Every scope records `release`, `package_revision`, `platform`, `status` (`candidate`, `verified`, `needs_review`), `archive_sha256`, `evidence`, `device`, `checks`, and `approval`. Missing historical facts remain null/unknown. Checks identify `packaging`, `portability`, `startup`, and `gameplay` separately. An approval records `by`, `at`, `reference`, and `scope`; null means approval has not been established.

GitHub entries describe reviewed support and evidence. They must not be treated as arbitrary executable commands. Catalog membership does not install a new build runner or bypass the app's pinned build-profile guards.

The initial catalog is an evidence inventory, not a retroactive approval. The current application still reads its bundled profiles; fetching and caching this catalog is a separate implementation step. When implemented, invalid/unavailable remote data should retain the last valid cache, report its age/source, and fall back to bundled support on first use. Refreshing a catalog must not mark an existing download playable.
