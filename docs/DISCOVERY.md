# Finding ports with Recomp Launcher

**Add a game → Discover more ports** opens references from
[Recomp Launcher's top list](https://recomplauncher.com/top). Search by project
name or repository, follow **Reference**, or choose a project to investigate.
The normal catalog and Custom game work independently of the reference site.

- A matching repository with existing Portkeeper packaging steps opens the
  existing setup flow. A game already in your library opens that game.
- A new repository opens Custom game and reads its upstream release, README and
  linked build guides. Choose automatic setup, a model draft, your own chat,
  manual packaging steps, or release preservation, just as for a pasted GitHub URL.
- You can also paste a Recomp Launcher game page into Custom game's project
  field. Portkeeper reads its explicit GitHub links. If it links to several
  repositories, choose the game engine or port; an extraction tool is not
  necessarily the game. No repository is guessed from a title.
- Game data requirements, supported operating systems, packaging checks and
  gameplay verification come from upstream evidence and the existing build
  workflow. A listing is not a working recipe or a verified playable package.
  Discovery itself uses no LLM calls or model budget.

## Refreshes and local records

Opening the normal game picker makes no reference-site requests. Expanding
**Discover more ports** reads a saved list, then requests a background refresh
if that list is missing or more than 24 hours old. Automatic retries wait at
least an hour after an attempt, including across app restarts. **Refresh list**
can retry sooner, with a one-minute minimum between attempts. Concurrent
refresh requests share one background request.

Each refresh reads `/top` once, without crawling every game page. A detail page
is read only when selected or pasted. The last successful list stays available
when the site is offline, returns no readable entries or changes its layout.
The panel shows the saved date and last refresh failure. A failed lookup never
changes an existing project or starts a build. Browse the website and paste the
actual upstream GitHub repository if the importer cannot read a page.

These small, editable JSON files live in `<data-dir>/catalog/`:

| File | Contents |
| --- | --- |
| `recomp-launcher.json` | Up to 100 project names, explicit reference/repository URLs, and the successful fetch time |
| `recomp-launcher-status.json` | Last attempt time and refresh error |
| `recomp-launcher-events.json` | Last 30 successful/failed refreshes and resolved-page references |

New custom projects retain a `discovery_source` reference in SQLite. Advanced
shows its source link. This attribution is distinct from recipe provenance and
verification. URLs lose query strings/fragments. Event records contain public
references and repository names, never owned file paths, game bytes, account
credentials or model responses. These records fit the metadata backup policy;
no source archives or game files are downloaded into appdata by discovery.

## Import contract and limits

The adapter reads semantic HTML links/cards/headings. It accepts explicit
GitHub repository links, including release/tree links normalized to the
repository, and same-site game/detail links. Navigation, footer, scripts and
style content are ignored. Duplicate repositories are collapsed, the site's
listing order is retained, and importing stops at 100 entries. It does not
infer projects from social links or invent an undocumented API endpoint.

The client is separate from the GitHub artifact downloader. Only HTTPS pages
on `recomplauncher.com` and `www.recomplauncher.com` may be read, and redirects
stay on those hosts. No account token, ambient proxy credentials, cookies,
remote JavaScript or artwork is used. Requests time out after 15 seconds,
HTML responses are limited to 2 MiB, and deeply nested/oversized HTML trees
are rejected. The app renders imported names as plain text. GitHub artifact
download permissions are unchanged.

This is an experimental HTML adapter, not an official API integration or a
complete mirror of the website. Client-rendered lists without readable HTML,
non-GitHub upstreams and unrecognized layouts require the website/manual
repository route. No pagination or additional rankings are fetched.

## Validation and known limitation

The implementation was tested with synthetic HTML contract fixtures covering
listing imports, details, ambiguous repositories, navigation filtering,
duplicates, unsafe links, size limits and failed refreshes. HTTP tests check
origin protection; browser interaction tests cover search, source links,
upstream-derived platforms, ambiguous choices and the offline fallback.

**Live compatibility with Recomp Launcher's current markup has not been
verified.** On the implementation date the cloud environment's outbound proxy
rejected this domain (`CONNECT 403`), and the production-style direct client
could not resolve it. No authoritative public source repository or official
API contract for this website was established. The similarly named desktop
launcher found during research was not assumed to be the website's source.
The tests are deliberately labelled contract fixtures, not live snapshots.
If the actual site uses different markup, update `portkeeper/discovery.py`
with a sanitized public-page fixture; retain the last-good-cache behavior.

To test the actual page from a local installation without starting the app or
opening its database, run `python -m portkeeper.discovery`. To inspect a detail
page's upstream references, run
`python -m portkeeper.discovery --resolve --page https://recomplauncher.com/games/EXAMPLE`
using a real page URL from the website. These commands print only public
reference JSON and use a temporary directory that is removed on exit.
