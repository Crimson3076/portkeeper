
## Match native-port imports to their original game

Portkeeper can assign a verified IGDB identity after RomM scans a portable archive.
Register known games in `data/romm-import-identities.json`:

```json
[{"filename_prefix":"Ship-of-Harkinian","game":"The Legend of Zelda: Ocarina of Time","igdb_id":1029,"url_cover":"https://images.igdb.com/igdb/image/upload/t_1080p/co3nnx.jpg"}]
```

Use the metadata ID and cover from the original game selected in RomM. The connector
needs `roms.write` as well as `roms.read`. Matching runs on the schedule chosen in Settings → RomM (default: only after an upload, checking every minute for 30 minutes, then backing off to 5, 15 and 60 minutes; idle installs make no RomM requests), applies only
to ZIP archives beginning with the registered prefix followed by `-` on Linux or
Windows platforms, and preserves existing version labels and testing notes. Original
console files are untouched. Unregistered games require an explicit identity first;
this avoids guessing from port names. This step matches files already imported by
RomM; it does not upload files or initiate library scans.

Known misses follow the same retry backoff even with a fixed check interval. A
library change or **Match now** retries them immediately.

### Project metadata settings

Open a game's Overview and select **Edit metadata link**. Enter the package filename
prefix and either an IGDB game ID or a link to a matched RomM entry. A RomM reference
is followed on each matching pass, so a later manual metadata correction carries
forward without another setup step. The reference itself is never overwritten.

Artwork is independent: **Keep current covers** leaves package artwork alone;
**Reuse the linked RomM entry's cover** follows its cover URL or custom uploaded
PNG/JPEG/WebP, including on future platform builds. Custom artwork copies are limited
to 10 MiB and fetched only from the configured RomM origin. Matching retains package
versions and validation notes. Project settings override legacy registrations with
the same filename prefix. The token still needs `roms.write`.

### Cover previews

Linked RomM covers appear on library cards and in the game's Overview. The server
proxies the current stored cover, including custom uploads, using its existing
connector token. Tokens and remote artwork URLs are never embedded in the page.
Images are limited to PNG/JPEG/WebP up to 10 MiB; previews refresh within a minute.
Projects without a linked entry or cover retain their initials.

### Find cover art once for every platform

Open a game's Overview and select **Find cover art**. Portkeeper searches through
RomM's own cover search (`/api/search/cover`), so it shows the same SteamGridDB and
Steam results as RomM's cover picker. RomM needs a SteamGridDB API key for this.

Pick a cover and select **Apply to Windows and Linux**. Portkeeper sends that cover
to every Linux and Windows ZIP whose filename starts with the game's package prefix,
right away and on every later import, so a new build never needs its cover picked
again. A linked RomM entry gets the chosen cover too; its name and metadata match
stay as you left them. Choosing a cover does not require an IGDB ID. Each entry is
updated once per chosen cover, so a later manual change in RomM is not overwritten
until you pick a different cover here. The token needs `roms.write`.

Previews of search results are proxied by the server; only covers the search
returned can be previewed, and only HTTPS PNG/JPEG/WebP images are accepted.
