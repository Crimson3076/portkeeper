# Home Assistant settings and fruit icon — 2026-10-27

Requested: configure the Home Assistant long-lived access token in the app,
create a Wumpa-inspired 3D Frutiger Aero fruit icon for the Unraid dashboard,
and push the update.

## Implemented

- Added **Settings → Notifications → Home Assistant** with server URL,
  password-style token input, optional inbox forwarding, connection test,
  explicit test notification and token removal. No Docker variables or
  command-line token are required.
- Saved credentials atomically with private file/directory permissions.
  Public API state never includes the token. Empty token fields preserve the
  existing value, replacing the server requires a new token, and removing a
  token disables delivery. Saved inputs survive polling; failed tests explain
  that settings were saved.
- Added optional persistent Home Assistant notifications for newly created
  inbox events. One background worker, a 32-notice queue, eight-second request
  timeouts and response limits keep delivery bounded. Existing inbox identity
  deduplicates events. Network failures never remove the local notice or fail
  a build; delivery errors remain visible in Settings.
- Blocked credential-bearing request redirects, avoided ambient proxy
  credentials, rejected malformed/oversized configuration and discarded queued
  notices belonging to an older connection. Connection-status records and
  credentials use separate storage.
- Included `secrets/home-assistant.json` in the existing metadata-only backup
  allowlist. No game files, bulk backup copies or new delivery archives are
  created.
- Generated original glossy orange/red fruit artwork with green leaves and
  alpha transparency. Stored the PNG at `static/portkeeper-fruit.png`, updated
  the Unraid template, image's `net.unraid.docker.icon` label and browser favicon, and replaced the legacy
  `unraid/portkeeper.png` asset for compatibility.
- Added **Settings → Appearance → Copy icon URL** with the icon preview and
  existing-container instructions, including the HTTP-compatible clipboard
  fallback. Saved Unraid templates require their Icon URL to be changed once;
  pulling a new image cannot change a host template from inside the container.
- Updated [HA setup](HOME_ASSISTANT.md), [Unraid instructions](UNRAID_UPDATES.md),
  storage/backup documentation and the architecture reference.

## Validation

- `python -W error -m unittest discover -s tests -p test_home_assistant.py -v`:
  11 tests passed. Includes a real local HTTP server with synthetic credentials
  checking API paths, bearer authentication, notification JSON and rejected
  tokens, plus private storage, backup inclusion, queue bounds, disabled/stale
  connections, deduplication and failure isolation.
- HTTP route tests verify private token configuration, redacted state and
  cross-origin rejection for configuration and test actions.
- Browser interaction checks cover token saving/clearing, polling preservation,
  test actions, saved-settings feedback after a failed test, icon preview and
  clipboard URL copying.
- Full local suite: 404 tests completed successfully, with 8 optional native/
  worker integration checks skipped locally. Browser suite, JavaScript syntax
  and patch whitespace checks passed.
- Parsed the Unraid XML and checked that its icon URL matches the new asset.
  Both PNG paths contain the same image: 1,381,245 bytes, 1254 × 1254 RGBA,
  with alpha extrema 0 and 255 (actual transparency).

No real HA address or token was available in this cloud session. The in-app
tests verify the user's installation after configuration; no live NAS settings
or files were modified. The GitHub Tests and Docker publishing workflows run
on the update push; their release results are reported separately.
