# Activity views

Activity has separate views for all events, builds, update checks, model drafts, uploads, test results, and RomM imports. The game filter applies to each log view. Build archives lists the actual downloadable packages; Inbox lists notifications. Counts reflect visible entries after any activity cleanup.

An update check compares the stable upstream release and selected files with the preserved copy. **No upstream changes** means the check found no newer release or changed downloads. It does not represent a new build or an upload. A changed release can be saved while automatic builds are off, need instruction review, queue builds, or reuse packages that already match the release, definition and selected game files. Each outcome is recorded separately. Build and publication results come from their actual jobs and package records.

Drafts use the selected model and remain reviewable before changing active instructions. Upload records include manual uploads and publication that occurs inside an automatic build; an upload can await RomM indexing before it is shown as in the library. Test records preserve the distinction between packaging, startup and gameplay. Import discovery records show packages found in RomM.

Clear activity & logs applies to completed records, with a private local backup first. It retains package files, release captures, current instructions and validation results. See [cleanup controls](CLEANUP.md).
