# Updating Portkeeper on Unraid

Portkeeper publishes `ghcr.io/crimson3076/portkeeper:latest` when a commit reaches
`main`. The image is available after the **Docker** GitHub Actions workflow's build
and push step succeeds; pushing source alone does not mean the image is available.

1. Wait for the commit's **Tests** and **Docker** workflows to finish at
   <https://github.com/Crimson3076/portkeeper/actions>.
2. Stop Portkeeper in Unraid's **Docker** tab. Back up the host folder mapped to
   `/data` (normally `/mnt/user/appdata/portkeeper`) while the container is stopped.
   Keep this backup private: it can include credentials and game material. Record
   the current image ID or digest if you want a rollback option.
3. Edit the container and confirm **Repository** is
   `ghcr.io/crimson3076/portkeeper:latest`. Keep the existing `/data`, `/inputs`, and
   `/library` mappings, allowed hosts, port, PUID and PGID settings.
4. On the **Docker** tab, choose **Check for Updates**, then update Portkeeper.
   If Unraid does not detect the new image, enable **Advanced View**, open the
   container's menu and choose **Force Update**.
5. Start Portkeeper if it is stopped. Open its WebUI and refresh the browser
   (Ctrl+Shift+R, or Cmd+Shift+R on macOS) to load the new interface.
6. Confirm your games and settings remain present. Each game now has **Play**,
   **Updates**, and **Advanced** tabs. Check the container logs if startup fails.

An update recreates the container using the image and retained template settings.
Persistent data survives through the host mounts; files stored only inside the
container do not. Do not delete the app-data folder as part of updating.

## Editing your own build instructions

Open a game → **Advanced** → **Edit packaging definition**. Use **Settings** for
common fields, **JSON** for the complete definition, and **Changes** to review the
draft. **Copy JSON** copies the current text, including unsaved edits, on HTTP as well
as HTTPS connections. Import/export work with ordinary JSON files. **Validate** checks the same
rules used for saving without building. Saving affects future builds and stores the
previous definition in **Saved revisions** (up to 20). Review a previous revision
and save it to restore its content as a new revision. Existing packages and their
test results stay unchanged.

Automation lives under **Updates**. Newly added games default to keeping packages
in Portkeeper and waiting for your decision on future builds. Building, uploading
to RomM, and confirming gameplay are separate choices. Existing automation settings
are retained. Platform-specific definitions such as Bloodborne's Linux and Windows
variants share one game card in the picker; add each platform's definition separately.

## Rolling back

If needed, set **Repository** to a previously recorded image digest, for example
`ghcr.io/crimson3076/portkeeper@sha256:<previous-digest>`, and apply the template.
If the older version cannot read the current app data, stop the container and
restore the matching backup before starting it. Return to `:latest` when ready to
resume normal updates.

## Dashboard icon

New installs using `unraid/portkeeper.xml` include the Portkeeper icon. For an existing container, set its Icon URL in the Unraid template to:

```text
https://raw.githubusercontent.com/Crimson3076/portkeeper/main/unraid/portkeeper.png
```

Image updates do not replace an existing container's saved icon setting.
