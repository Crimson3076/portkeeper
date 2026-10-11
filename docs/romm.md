
## Match native-port imports to their original game

Portkeeper can assign a verified IGDB identity after RomM scans a portable archive.
Register known games in `data/romm-import-identities.json`:

```json
[{"filename_prefix":"Ship-of-Harkinian","game":"The Legend of Zelda: Ocarina of Time","igdb_id":1029,"url_cover":"https://images.igdb.com/igdb/image/upload/t_1080p/co3nnx.jpg"}]
```

Use the metadata ID and cover from the original game selected in RomM. The connector
needs `roms.write` as well as `roms.read`. Matching runs every minute, applies only
to ZIP archives beginning with the registered prefix followed by `-` on Linux or
Windows platforms, and preserves existing version labels and testing notes. Original
console files are untouched. Unregistered games require an explicit identity first;
this avoids guessing from port names. This step matches files already imported by
RomM; it does not upload files or initiate library scans.
