# The Recomp catalog (Recomp Launcher)

Portkeeper reads Recomp Launcher's public catalog,
[`pc-catalog.json`](https://recomplauncher.com/pc-catalog.json), for artwork,
descriptions and a browsable list of games. It replaces the earlier approach of
parsing the site's HTML.

## What you get

- **Settings → Advanced → Catalog** shows "Recomp catalog: N games, updated
  <date>", a **Refresh catalog** button and the last error, if any.
- **Add a game → Browse all Recomp games**: search every catalog game, filter by
  console, system and genre, and sort by popularity, newest or name. Cards show
  the cover, the port, the original game, the console and supported systems.
  Games already in your library are marked. **RomM?** looks for the original
  console + title in your RomM library (best effort: it only tells you; it never
  selects a file). Choosing a game continues into the normal setup flow: a
  reviewed Portkeeper recipe if one exists, otherwise the upstream lookup.
- **Artwork**: a game with no cover of its own and none from RomM uses the
  catalog `cover`; the game page can show `hero` and `logo`. Images are
  downloaded by the Portkeeper server into its cache (the browser never contacts
  the site) and a cover you chose is never overwritten.
- **Game page**: title, description, features/tags, "You need: …", IGDB summary,
  genres and rating for games whose GitHub repository matches a catalog entry.
  A game that shows a repository name is displayed with the catalog title; its
  stored name is unchanged.
- **Release file hints**: when you look up a repository, catalog `assets`/`files`
  patterns suggest which release file belongs to each system, with the catalog
  `sha256` shown as an extra comparison value.
- **Setup manifest**: if the upstream repository ships a `recomplauncher.json`
  (see the [schema](https://recomplauncher.com/schema/recomplauncher.json)), its
  `start`, `prepare`, `folders` and `saves` are read at the release commit and
  shown as hints. They are untrusted: only simple relative paths and short strings
  are kept, and nothing is executed from them.

## Trust and limits

- Plain HTTPS GET to `recomplauncher.com`, with a User-Agent naming Portkeeper.
  No credentials, cookies or scripts; redirects stay on the site's hosts. Image
  hosts are limited to `recomplauncher.com` and `recompstore.com`.
- The catalog is fetched at most once a day (and on demand from Settings or the
  browser), using `ETag`/`If-Modified-Since`. A 15 second timeout and an 8 MiB
  cap apply (images: 5 MiB, PNG/JPEG/WebP only). Failed attempts are retried at
  most hourly, including across restarts.
- Each entry is validated; malformed entries (bad id, no name, duplicates, unknown
  systems, bad checksums or image hosts) are dropped and counted.
- The last good copy is always kept. If the site is down or returns something
  unexpected, browsing, artwork and metadata keep working from that copy, and
  Portkeeper's own reviewed catalog is unaffected.
- Catalog data is never trusted for platform support, launch commands or
  verification status; the repository's release and Portkeeper's own checks decide.

## Stored files (small, in the `catalog` metadata folder)

| File | Contents |
| --- | --- |
| `recomp-catalog.json` | Validated entries, the update date, validators (ETag/Last-Modified) |
| `recomp-catalog-status.json` | Last attempt time and refresh error |
| `images/` | Cached artwork (`.img` plus its content type) |

The earlier `recomp-launcher*.json` files are no longer used and can be deleted.
