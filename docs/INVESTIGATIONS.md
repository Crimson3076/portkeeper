# Investigate, plan, draft and test

Open a game → **Advanced → Draft packaging steps → Investigate before drafting**.
This is the recommended starting point for an unfamiliar repository. It works with
Ollama, OpenAI-compatible servers, Claude API and the connected Codex, Claude Code
and Gemini subscription/account integrations. Models exchange ordinary structured
JSON; native function calling and direct filesystem access are unnecessary.

## Guided workflow

1. **Check upstream** to preserve the release first. Choose whether the desired
   package should prepare your game files or leave them external.
2. Set an **Investigation limit** of one, three or six model requests and click
   **Investigate with…**. Each follow-up consumes another request. The investigation
   reads public material and limited metadata; it does not compile, extract owned
   assets, test the game, change the active definition or publish anything.
3. Review the result, platform/runtime assessment, owned-data requirements,
   proposed supported steps, uncertainties and **View plan evidence**. A quoted
   reference must match supplied text, but matching does not prove its interpretation.
4. A supported plan offers **Draft packaging steps from this plan**. The new
   definition must use the planned runtime assets and preparation capabilities.
   It still passes the ordinary schema/archive checks and remains a proposal until
   approved. Changed releases, platforms, preparation choices or active definitions
   require another investigation.
5. **Use my selected game files in disposable draft tests** is off by default.
   With it off, tests assemble public runtimes and mark game-data-dependent steps
   unverified. With it on, the runner may read selected inputs and create private
   working copies. These tests retain their logs/results, not packaged or extracted
   game files. Public dependency downloads may remain cached. An unselected input
   cannot be chosen automatically by the model.
6. Inspect the draft, test result and optional advisory review before approving.
   A test that passed packaging checks is not a native launch or gameplay test.
   Normal builds and sending completed packages to RomM remain separate actions.

The one-request direct drafting option, JSON import/editor and prepared prompt
remain available for advanced use. Investigation requests, definition/correction
requests and optional review requests have separate visible budgets. API charges
or subscription/local model allowances apply to every additional model request.

## Evidence the host serves

The initial request includes a bounded README/source sample, source paths, release
metadata, current runner capabilities and a sanitized inventory of inputs already
selected in that project. The model can ask for:

- A specific text source file or another character window within it.
- A source-path listing or bounded literal-text search.
- Pages of a preserved or already inspected release archive's full member listing.
- A bounded text file inside a preserved release ZIP/tar (`archive_file`, with
  asset name, member path and optional character offset). Packaged README/setup
  instructions are sampled automatically and can be quoted as public evidence.
- Current capabilities or selected-input metadata.

Source and preserved assets are checked against their capture hashes. Files are
read without executing them. Individual text files are capped at 2 MB, returned
windows at 5,000 characters, searches at 8 MB/20 matches, and each request at three
evidence queries. A model request contains at most 24,000 characters. External
URLs, private filesystem paths, arbitrary commands and installation requests are
not accepted as evidence actions. Full manifests stay on the server for checks.

For a source-only game, investigation can retain up to four inspected public
release assets, each at most 400 MB, beside the preserved source. This needs disk
space but does not change the active definition or its capture record. The saved
investigation records the runtime hashes and research snapshot. Guided drafts and
disposable tests reuse that snapshot rather than silently refetching changed assets.
Archive selection uses the same OS filename detection as the add-game screen,
including `_win.zip` and `_win32.zip`; requested runtime platforms are inspected
first. Direct drafts also receive packaged README/setup instructions. These reads
do not extract or execute a release, follow links, or read your game files.

Input metadata covers declared, selected inputs: kind, recognized format, size,
verified dump hashes, PS4 title/version/role, and standard layout markers. It
contains no input filenames, locations, credentials or game contents. Empty
metadata means nothing has been selected in the active definition; it never means
the game needs no original data. Declare/select new game inputs after reviewing
the resulting definition. Manual notes should describe requirements, not secrets.

## Honest outcomes

**Plan ready to review** means the investigation established a candidate workflow
using available runtime assets and fixed capabilities for the selected platforms.
It does not certify that it will build or run.

**Current runner cannot package this release** is a valid outcome. For example,
`n64decomp/sm64` ships a compiler helper and rebuilds N64 ROMs; packaging its
`qemu-irix` helper does not produce a Mario PC game. The investigation cannot
turn a helper or an SDK into a playable runtime.

**More evidence or capabilities are needed** appears when quotations cannot be
matched, a runtime/launch path is absent, a selected platform is unsupported,
preparation was requested but left external, or the bounded research budget ends
before a plan is established. Inspect the recorded requests or investigate again
with more focused notes/a larger limit. No invalid game package is substituted.

Arbitrary source compilation remains unavailable to model-generated operation
definitions. New compilation or extraction procedures need reviewed, fixed build
adapters with pinned dependencies, resource limits and tests. The language model
can identify that gap; it cannot install its own adapter or bypass the runner.
OpenLara may have a usable runtime but an unsupported owned-data preparation path;
the report should identify the actual evidence and gap for the selected release.

## Audit and outside agents

Connected agents use `action` and `payload_json` for structured replies. The string
encodes only a plan object (with `outcome` equal to `ready`, `unsupported` or
`needs_evidence`) or an evidence request array. Equivalent natural and bounded
nested wrappers are accepted; missing/invalid outcomes are still rejected.
Rejection feedback includes the returned value or missing fields so a subsequent
request can correct the format within the selected budget. This does not replace
runtime, capability or evidence validation.

**View model responses**, **Copy response** and **Download drafting record (JSON)**
include the investigation exchanges. Records preserve the outcome, evidence,
source/capture identity and selected request limits. They can be used to compare
models against the same release and settings without treating confidence as proof.

Outside agents using Portkeeper's MCP tools can query `investigation_capabilities`
and `read_input_inventory`, alongside the existing source readers, definition
validation and disposable test tools. They still cannot approve or publish.
