# Recomp Launcher integration work log — 2026-10-27

Requested scope: integrate `https://recomplauncher.com/top` as an easier source
of port references, document the work, test it and push an update. No live NAS
migration or backup deletion is part of this change.

## Investigation

- Inspected the existing reviewed catalog, Add a game wizard, custom GitHub
  lookup, automatic setup handoff, HTTP origin checks and metadata backup rules.
- Attempted to read the supplied public page. The cloud proxy refused the
  connection with `CONNECT 403`; an elevated request had the same result.
  A check using the production client's credential-free direct networking
  failed DNS resolution. No page content was obtained.
- Searched public GitHub repository metadata for the domain and project name.
  Found similarly named desktop launchers, but no established source/API
  contract for the supplied website. No unrelated launcher's game database
  was substituted, and no live-site validation is claimed.

## Implemented

- Added a separately scoped public HTML reference adapter with bounded parsing,
  same-site redirects and no credentials, script execution or remote images.
- Added a last-good list cache, persisted retry timestamps, one shared
  background refresh, and a rolling 30-event activity record. These are small
  catalog metadata files covered by the existing metadata backup allowlist.
- Added searchable **Discover more ports** inside Add a game, website/source
  links, saved-list dates, refresh/error feedback and manual fallback.
- Added Recomp Launcher game-page input to Custom game. Multiple upstream
  links require an explicit choice. Imported text cannot supply platform
  support, a launcher command, a recipe or a verification status.
- Routed known repositories to their existing catalog setup and existing games
  to their library page. Unknown repositories use the existing upstream lookup
  and model/manual/automatic setup choices.
- Retained a validated source reference for newly added custom games and
  exposed it in Advanced. Editing a looked-up repository invalidates the old
  lookup before a game can be added.
- Preserved discovery search text, selection and list scroll during background
  updates. Site outages do not block normal catalog setup.
- Final parser review added handling for valueless HTML class attributes, so
  malformed third-party markup does not terminate a refresh without feedback.
- Final storage review bounded GitHub URL/repository lengths as well as record
  counts, preventing oversized third-party links from inflating catalog backups.
- Documented the user flow, JSON records, limits and unverified live markup in
  [DISCOVERY.md](DISCOVERY.md).

## Validation record

Commands and results are recorded here before publication. The focused tests
use synthetic contract HTML; none are represented as real-site fixtures.

- `python -W error -m unittest discover -s tests -p test_discovery.py -v`:
  17 tests passed, covering parsing, trust boundaries, metadata provenance,
  refresh limits, cache fallback, concurrent refreshes and response limits.
- `python -W error -m unittest discover -s tests -p test_server.py -v`:
  18 tests passed, including discovery routes and cross-origin rejection.
- `PORTKEEPER_JSDOM=/workspace/portkeeper-ui-tests/node_modules/jsdom node tests/test_ui_layouts.cjs`:
  browser interaction checks passed, including new discovery cases.
- `python -W error -m unittest discover -s tests -v`:
  392 tests completed successfully; 8 optional native/worker integration tests
  were skipped locally. No gameplay or native Windows verification is claimed.
- `node --check static/app.js` and `git diff --check`: passed.
- `python -m portkeeper.discovery --help`: passed. The CLI's arbitrary-host
  rejection is included in the regression suite.
- Publication: this change is pushed to `main`; the repository's Tests and
  Docker workflows run on that push. Their results are available in GitHub
  Actions and are reported separately from these local checks.

## Deployment

Updating the application is sufficient; no volume or database migration is
required. Discovery is contacted only after the user opens its panel and
requests a missing/stale list or explicitly refreshes it. Existing games,
recipes, packages, storage mappings and backup retention stay intact.
