# Home Assistant notifications

Configure the connection in **Settings → Notifications → Home Assistant**.
No Docker environment variable, edited recipe or command-line token is needed.

1. Enter the Home Assistant server address, for example
   `http://homeassistant.local:8123` or your HTTPS address. It must be reachable
   from the Portkeeper container; `localhost` inside a container refers to that
   container. Use HTTPS when connecting outside your trusted local network.
2. In Home Assistant, open your profile → **Security → Long-lived access tokens**
   and create a token. Paste it into Portkeeper's password field.
3. Choose **Save & test connection**. This checks the REST API with the saved
   token. If the test fails, the settings remain saved and the error explains
   that the connection could not be verified.
4. Use **Send test notification** to create a notification in Home Assistant's
   own notifications panel. This is an explicit test even when automatic
   delivery is off.
5. Enable **Send new Portkeeper inbox updates to Home Assistant**, then save,
   to forward future package-ready, build-failure, required-input, upstream,
   review and new-platform notices.

The connection works while the browser is closed, as long as Portkeeper is
running. It creates persistent Home Assistant notifications, not direct mobile
push notifications. Old inbox entries are not replayed when delivery is enabled.
Home Assistant's `persistent_notification.create` service must be available to
the token's user.

## Credentials and delivery

- The token stays in `<data-dir>/secrets/home-assistant.json`, readable only by
  the Portkeeper service account. The browser receives `has_token` and connection
  status, never the saved token. Tokens are not placed in recipes, model prompts,
  image labels or public activity logs.
- Leave the token field empty to keep it. Replacing the server URL requires a
  replacement token or clearing the old one, preventing accidental reuse at a
  different server. **Remove token** also turns delivery off. Revoke unwanted
  tokens separately in the Home Assistant profile.
- Requests use `Authorization: Bearer` with no ambient proxy credentials. No
  redirect is followed, including redirects to another origin or a login page.
  Tests and notifications use only the saved server address.
- Notifications are deduplicated by Portkeeper inbox event identity and use a
  matching persistent notification ID in Home Assistant. Delivery runs through
  one background worker with at most 32 pending/in-flight notices, eight-second
  request timeouts and bounded responses. It cannot block a build on network IO.
- Errors and the last successful delivery time appear below the settings form.
  Failed deliveries remain in the Portkeeper inbox; they are not automatically
  retried. Pending work is cancelled on shutdown and is not replayed on restart.
  Disabling delivery or changing connections drops obsolete queued work.
- Private credential JSON is included in the existing metadata-only snapshots
  and should remain covered by the nightly appdata backup. There are no game
  files, growing delivery archives or additional bulk backup directories.

REST endpoints used: `GET /api/` for the connection test and
`POST /api/services/persistent_notification/create` for delivery. The tests use
a local synthetic Home Assistant HTTP server to verify authorization, paths,
JSON payloads and rejected-token behavior. A real Home Assistant installation
was not available in the cloud session; the in-app test verifies yours.
