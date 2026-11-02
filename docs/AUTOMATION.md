# Automatic port setup

Portkeeper now has a separate script runner. A model can inspect a preserved
project, fetch public dependencies, write and execute arbitrary preparation/build
scripts in offline containers, inspect public failures, and save a reusable recipe.
The runner is game agnostic: a maintainer does not need to add a fixed extraction
adapter before an unfamiliar port can be attempted. Existing schema-2 operations
and reviewed profiles keep their behavior.

This first backend is intentionally bounded. It is **not a guarantee that every
project builds**, and it does not claim Windows startup or gameplay was verified.
It requires a separately deployed broker; updating the ordinary Unraid app image
does not create a secure general-purpose worker or install a rootless daemon.

## What runs today

1. Preserve a GitHub release/source. Repositories without a release fall back to
   a pinned default-branch commit, not a fabricated downloadable game.
2. A provider-neutral action loop reads public source/runtime files. The configured
   Ollama, compatible API, Claude API, Codex, Claude Code or Gemini transport can
   participate using the same versioned JSON protocol; native tool calling is not
   required. Native agent tools and credentials stay outside build workers.
3. Public scripts compile, unpack and prepare tools inside an offline Linux
   container. Dependencies are fetched by the broker, checked and retained.
4. Private scripts receive only selected owned inputs, mounted read-only. Separate
   base/update inputs have separate roles. The generated procedure must establish
   compatible title/version and merge the update after the base; it cannot invent
   missing keys. Input identities are checked before and after execution.
5. The host checks concrete output files and runtime format, then repeats the
   entire procedure from an empty worker with the same source, dependencies and
   immutable image, **without an LLM request**. Different package contents fail.
6. Validated packages enter the existing download/RomM lifecycle. An unsuccessful
   replacement does not delete previous downloads. Publication remains a separate
   action or follows the existing per-game automatic publication policy.

Successful recipe definitions live in the project’s `autonomous_recipe`, with a distinct
`schema: 1`, `runner: isolated_scripts` contract. They do not reinterpret the
schema-2 packaging definition. Public replay files are checksummed beneath the bulk
storage's `automation-recipes/` (`/storage/automation-recipes` for new installs).
These payloads need separate backup coverage; JSON alone cannot recreate unavailable
upstream files. See [storage](STORAGE.md). Script edits increment the recipe revision and require
another replay before accepting a new package. Updates can replay the same capture
or use the model to investigate a new preserved capture under the saved policy.

## Worker deployment

The web app never receives an unrestricted Docker socket. Run
`python3 -m portkeeper.worker_broker` **outside the app**, as the account owning a
dedicated rootless Docker daemon. The broker communicates over an owner-only Unix
socket; its directory is `0700`, socket `0600`. The app and broker must share that
account’s accessible exchange directory. Only this trusted broker controls mounts,
image and resource flags; model requests cannot choose host paths or Docker flags.

Use a Linux machine/VM with **cgroup v2 delegation**, rootless Docker, Python 3.11+
and enough disk/RAM. On Debian/Ubuntu, rootless cgroup control also needs
`dbus-user-session`, the user's running systemd manager and its session bus.
Run the rootless daemon as that user's delegated service (the official rootless
installer supports this), rather than a detached process under a system service.
The latter can make runc ask the privileged system manager for interactive
authentication instead of controlling the user's own groups.
A dedicated Linux VM on Unraid is a suitable deployment option:
run the app and broker in that VM and mount the NAS game collection read-only.
The standard Unraid Docker daemon is rootful and is refused. Rootless/cgroup behavior
on a particular Unraid host has not been validated here; do not replace the boundary
with a privileged worker or socket mount to make a failed check pass.

Example on the dedicated rootless host, from the checked-out Portkeeper repository:

```bash
docker info --format '{{json .SecurityOptions}}'
docker pull ghcr.io/crimson3076/portkeeper:worker-latest
PORTKEEPER_WORKER_IMAGE_ID=$(docker image inspect --format '{{.Id}}' ghcr.io/crimson3076/portkeeper:worker-latest)

export PORTKEEPER_BROKER_DIRECTORY="$PWD/.worker"
export PORTKEEPER_OWNED_DIRECTORY="/srv/owned-game-files"
export PORTKEEPER_STATE_DIRECTORY="$PWD/.data"
export PORTKEEPER_STORAGE_DIRECTORY="/srv/portkeeper-bulk"
export PORTKEEPER_LIBRARY_DIRECTORY="$PORTKEEPER_STORAGE_DIRECTORY/library"
# These paths must already be on the intended filesystems and writable by this
# rootless account. In a VM, use that VM's mounted NAS path for bulk storage.
mkdir -p "$PORTKEEPER_STATE_DIRECTORY" "$PORTKEEPER_STORAGE_DIRECTORY" "$PORTKEEPER_LIBRARY_DIRECTORY"
mkdir -p "$PORTKEEPER_BROKER_DIRECTORY/exchange"
chmod 700 "$PORTKEEPER_STATE_DIRECTORY"
chmod 700 "$PORTKEEPER_BROKER_DIRECTORY"

python3 -m portkeeper.worker_broker \
  --root "$PORTKEEPER_BROKER_DIRECTORY/exchange" \
  --input-root "$PORTKEEPER_OWNED_DIRECTORY" \
  --socket "$PORTKEEPER_BROKER_DIRECTORY/broker.sock" \
  --protected-root "$PORTKEEPER_STATE_DIRECTORY" \
  --image "$PORTKEEPER_WORKER_IMAGE_ID"
```

Keep the broker running under the host’s service manager. In another shell with the
same exported directory settings, start the app using the optional rootless override.
Its state/bulk/library bind mounts must be owned/writable by the daemon account;
the default image-owned named volumes are not suitable for this mapped UID:

```bash
docker compose -f compose.yaml -f compose.automation.yaml up -d --build
```

The input directory should contain game inputs only. Supply additional
`--protected-root` paths if controller data or account credentials could occur
inside that collection; the broker rejects both a protected path and any selected
folder containing it. The exchange directory cannot overlap the input collection.

Inside this **rootless** Compose deployment, UID 0 maps to the unprivileged host
account and lets the app reach the same owner-only socket/files. This override is
accepted by the entrypoint only when the kernel UID map confirms a non-root parent
ID; ordinary host-root execution is still refused. This override is
for a new deployment on that host; migrating existing Unraid data also requires a
private backup and deliberate ownership/mount configuration. Do not copy secrets
into the worker image. Existing Unraid images/settings are unchanged by deployment
of the broker elsewhere.

Alternatively, a host-run app can use:

```bash
export PORTKEEPER_WORKER_SOCKET="$PORTKEEPER_BROKER_DIRECTORY/broker.sock"
export PORTKEEPER_WORKER_EXCHANGE="$PORTKEEPER_BROKER_DIRECTORY/exchange"
python3 -m portkeeper --data-dir .data --input-root "$PORTKEEPER_OWNED_DIRECTORY"
```

The broker requires a preinstalled immutable image ID/digest. It does not pull a
model-selected image. `Dockerfile.worker` supplies common C/C++, CMake and SDL build
dependencies without model CLIs or credentials. Other build environments can be
configured by the operator; the resulting exact image must be retained for replay.
Archive it with `docker image save` on the worker host and restore with
`docker image load`; rebuilding an unpinned Dockerfile is not equivalent. Missing
images or changed public seed files stop replay instead of fetching replacements.

## Split deployment: app on Unraid, broker in a VM

The single-machine mode above still works. To keep Portkeeper as an ordinary Unraid
container (updated from the Docker tab, no Docker socket, no privileges) run **only
the broker** in the VM and connect them over the network.

- **App** (UI, catalog, upstream polling, RomM matching, publishing): the Unraid container.
- **Broker** (isolated builds in rootless Docker with cgroup v2 delegation): the VM. It
  still refuses rootful daemons and alone decides mounts, network, resources and
  which dependencies are fetched.

### What crosses the link

HTTPS on port 8799 (configurable). The broker generates a self-signed certificate and
the app **pins its SHA-256 fingerprint**; the token is never sent to a different
certificate. The app can only send the same allow-listed job requests as the local
socket (unknown operations and extra arguments are rejected), upload a seed tree for a
job, and download that job's snapshot, log and fetched dependencies. It cannot name
host paths: the broker reads owned game inputs from its own read-only
`--input-root` (mount the NAS read-only in the VM; selected inputs are relative paths
inside it). Uploads and downloads are capped (2 GiB + 64 MiB archive overhead; logs 1 MiB),
logs and artifacts are streamed, extracted with the same hardened unpacker, and checked
against the broker's SHA-256 identity. Built packages come back to the app and enter
its normal download/RomM lifecycle; the VM keeps no copy once a job finishes.

Pairing uses a one-time 20-character code (15 minutes, five attempts). The code is
never transmitted: app and broker prove knowledge of it with an HMAC bound to the
certificate fingerprint, so a man in the middle can neither learn it nor impersonate
the broker. Pairing yields a random token; only its hash is stored on the broker, and
the app keeps it in `/data/secrets/broker.json` (mode 0600). Pairing again revokes the
previous app. Mutual TLS is not used; certificate pinning plus the token provides the
same two-way authentication with one fewer file to manage.

If the broker is unreachable, new automatic builds **wait in the queue** (their time
budget starts when the broker returns). A certificate change or a rejected token is
shown as an error instead, never queued silently. The app and broker refuse to pair or
talk across different protocol versions (`protocol` in Settings), naming both versions.

### One-time VM setup

Prepare the VM as described under *Worker deployment* (Linux, cgroup v2 delegation,
rootless Docker, Python 3.11+, `openssl`, `curl`), logged in as the rootless user.
Mount the NAS game collection **read-only** (for example at `/srv/owned-game-files`).

```bash
set -euo pipefail
REPO=Crimson3076/portkeeper
BASE="$HOME/portkeeper-broker"
mkdir -p "$BASE/releases" "$BASE/state" "$HOME/.config/systemd/user"
chmod 700 "$BASE" "$BASE/state"

# Install the latest release (the update timer takes over from here).
TAG=$(curl -fsSL https://api.github.com/repos/$REPO/releases/latest | python3 -c 'import json,sys;print(json.load(sys.stdin)["tag_name"])')
mkdir -p "$BASE/releases/$TAG"
curl -fsSL "https://github.com/$REPO/archive/refs/tags/$TAG.tar.gz" | tar -xz --strip-components=1 -C "$BASE/releases/$TAG"
ln -sfn "releases/$TAG" "$BASE/current"
echo "$TAG" > "$BASE/installed"

# Worker image (immutable ID) and broker settings.
docker pull ghcr.io/crimson3076/portkeeper:worker-latest
cat > "$BASE/broker.env" <<ENV
BROKER_ROOT=$BASE/state
INPUT_ROOT=/srv/owned-game-files
LISTEN=0.0.0.0:8799
WORKER_IMAGE_ID=$(docker image inspect --format '{{.Id}}' ghcr.io/crimson3076/portkeeper:worker-latest)
ENV
chmod 600 "$BASE/broker.env"

# Services: broker plus the hourly self-update timer.
cp "$BASE"/current/tools/broker-vm/portkeeper-broker*.{service,timer} "$HOME/.config/systemd/user/"
loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now portkeeper-broker.service portkeeper-broker-update.timer
journalctl --user -u portkeeper-broker -n 20 --no-pager   # shows the pairing code
```

Allow only the Unraid server to reach TCP 8799 in the VM firewall. If the code has
expired or you need a new one:

```bash
cd "$HOME/portkeeper-broker/current" && python3 -m portkeeper.broker_net pair --root "$HOME/portkeeper-broker/state"
```

On Unraid, open **Settings → Advanced → Storage & runners → Automatic port setup**, enter
`https://<vm-address>:8799` (the template's *Build broker URL* variable pre-fills it) and the
code, then press **Pair with broker**. The panel shows the broker version, protocol and
whether the rootless check passed. Leave `PORTKEEPER_WORKER_SOCKET` unset in the container.

### Broker self-update

`portkeeper-broker-update.timer` runs `tools/broker-vm/portkeeper-broker-update.sh`
hourly. It reads the latest GitHub release, downloads it into a new directory, runs the
**full test suite there**, and only then switches the `current` link and restarts the
broker. If the new broker is not ready within two minutes it relinks the previous
release, restarts, and records the tag in `rejected` so it is not retried until a newer
release appears. Output: `journalctl --user -u portkeeper-broker-update`; test output:
`~/portkeeper-broker/last-test.log`. Run it by hand with
`systemctl --user start portkeeper-broker-update.service`. Pairing survives updates
(`state/` is outside the releases); the certificate is created once. The last three
releases are kept.

### Migrating an existing single-VM install

Everything below is a copy; the VM install stays untouched until you are satisfied.

1. Stop the app in the VM: `docker compose -f compose.yaml -f compose.automation.yaml stop`
   (leave the broker running).
2. Export the app data from the VM. With the default bind mounts from the rootless
   override, `PORTKEEPER_STATE_DIRECTORY` holds the database, settings and `secrets/`:
   `tar -C "$PORTKEEPER_STATE_DIRECTORY" -cpf /tmp/portkeeper-data.tar .`
   Bulk storage (`PORTKEEPER_STORAGE_DIRECTORY`: archives, replay files) and the
   library move as ordinary folders onto Unraid shares.
3. Install the Unraid template, but **do not start it yet**. Unpack the export into the
   appdata path (`/mnt/user/appdata/portkeeper`) and copy bulk storage and library to
   their mapped paths (see [storage](STORAGE.md) for the layout). Then fix ownership:
   `chown -R 99:100 /mnt/user/appdata/portkeeper`.
4. Delete any stale pairing: `rm -f /mnt/user/appdata/portkeeper/secrets/broker.json`.
5. Start the container, pair it with the broker as above, and run one automatic build.
   Replay recipes keep working because their public files travel in bulk storage and the
   worker image ID is unchanged.
6. Only after that works, remove the app from the VM (`docker compose ... down`) and keep
   the broker service. The broker needs no app data and no secrets from the app.

## Enable and use it

1. Connect your model in **Settings → Drafting assistant**.
2. Open **Settings → Advanced → Storage & runners → Automatic port setup**. Confirm
   the broker reports ready, enable automatic setup and choose eligible providers
   for this deployment. Provider-specific subscription eligibility is an operator
   choice; no undocumented API-key fallback or account-token proxy is introduced.
3. Add a port and preserve its source. Choose **Set up automatically**. A unique
   matching local input can be selected automatically; ambiguous/missing matches
   pause the job with a **Waiting for you** question, rather than taking a guess.
   Select a detected file and click **Continue setup**, directly on the current
   game tab. Filenames and their folders appear as selectable rows; large lists
   have a filter. **Choose another file or folder** opens a browser in the same
   panel, without navigating to Advanced. Optional notes are collapsed by default.
   Models can also ask a short question with suggested answers or a free-text reply.
   The same answer panel appears on Play, Updates and Advanced; no tab switch is
   required. Manual overrides and JSON remain available under Advanced.
4. **Advanced → Automatic setup** exposes input selection, notes, prompts/responses,
   downloaded records, scripts and editable recipe JSON. Input selections use paths
   relative to the configured library and stay local. For example:

   ```json
   [
     {"role": "base_game", "kind": "file", "path": "ps4/base.pkg"},
     {"role": "update", "kind": "file", "path": "ps4/update.pkg"}
   ]
   ```

5. Download and try the package; record the actual startup/gameplay result. **Replay
   saved recipe** makes no model calls. Edit the script JSON when you want to tinker;
   old packages remain available while those changes are tested.

### Answering a paused setup

A reply continues the **same job**, preserving its public investigation, completed
scripts, dependencies, workspace and remaining request/time budgets. Waiting does
not consume model requests, worker minutes or the job's elapsed-time allowance.
No container or executor thread runs while waiting. A paused job survives an app
restart and retains its project claim; cancel it to release that claim and remove
its workspace before starting another setup. Only one automatic setup, including
a paused setup, is allowed at a time.

Input candidate names and paths are shown only in the local interface. The model
receives the selected input's opaque ID, role and file/folder kind. Text deliberately
entered in the reply box is sent to the job's model: do not paste credentials, keys
or private file contents. The original provider/model and immutable worker image
must still be configured, and retained workspace checksums must match before a
reply is accepted. Replies cannot reset exhausted budgets or replace previously
selected inputs. Changed source, targets or instructions require a new setup.

Input searches can use a documented SHA-256 or a list of supported SHA-1 ROM
identities to distinguish different games/variants with the same extension. Hashes
are checked locally; choosing another file still checks any declared identity.
SHA-1 is only an upstream identification convention; replay integrity uses SHA-256.
Models must distinguish terminal environment/upstream blockers from input choices.
A structured input blocker goes through the same picker; an unclassified blocker
is returned to the model for a concrete action instead of immediately stopping.

Jobs that already failed before this feature was installed have no resumable
checkpoint. Select their inputs and start a new setup once; future ambiguous-input
requests will pause instead of failing.

## Privacy

Default **strict** mode allows cloud investigation/debugging of public material.
The model registers private scripts, supplies the package manifest, then private
work runs locally with no further cloud feedback. A private failure stops the job
and leaves a local record for diagnosis; it does not silently upload raw logs.

Optional **status feedback** allows private run/repair but exposes only exit status,
not raw private files, listings or logs. This has a residual covert channel: a
malicious script could encode private information into statuses. It is not formal
non-interference or a zero-leak guarantee.

Private file/log debugging requires the operator’s **local-only attestation** and
an Ollama/compatible endpoint at a literal local/LAN address. An ordinary public
API URL does not qualify. The operator must know that endpoint does not forward
requests or telemetry. A provider label alone cannot prove this.

Once a worker uses owned data, its workspace stays private; it cannot return to
public mode or fetch more dependencies. Original public seed files remain separate.
Private intermediate snapshots are removed after each job. Local logs and model
records remain inspectable. Recipe downloads contain no bundled input files, but
scripts generated during local debugging can contain private-derived facts; those
downloads are local, not automatically published as public recipes.

## Enforced bounds and current limits

- Default: 20 controller model calls/job, 100 across a rolling day, one setup job at a time,
  three repeated failures before stopping, 120 elapsed minutes and 120 worker minutes.
  Reservations are persisted before dispatch. Rejected actions count. This mode
  disables the API transport’s hidden retry/fallback. Native CLI token usage may be
  unavailable; native clients may retry internally. Request/time counts do not claim
  an exact upstream request count or hard dollar cap.
- Every worker has no network, a read-only root and seed/input mounts, dropped
  capabilities, seccomp/no-new-privileges, 4 CPUs, 8 GiB memory with no additional
  swap, 256 PIDs, 2 GiB `/work` tmpfs and 256 MiB `/tmp`. The broker checks actual
  cgroup/process enforcement before running model code; ignored limits fail closed.
- Commands default to 10 minutes, with an explicit maximum of 30. Logs stop at
  1 MiB. Snapshot extraction rejects traversal, devices, hard links, duplicate
  paths, escaping/cyclic symlinks and more than 30,000 files. Package outputs
  must contain regular files and no symlinks. Host exports reserve disk headroom;
  place broker storage on a quota-controlled filesystem for stronger protection
  against unrelated host writers consuming that space.
- This tmpfs backend will **not** fit many large disc/PS4 packages. It stops with a
  storage/resource reason; it does not open the NAS filesystem to an arbitrary
  writable worker. A larger disk-backed quota-enforced or VM backend is future work.
- Workers are Linux. They can assemble verified-format Windows binaries or attempt
  cross-compilation, but native Windows/MSVC execution needs a future Windows backend.
  There is no claim that Wine or file checks establish native Windows startup.
- Service restarts cancel/quarantine executing jobs; explicitly paused jobs retain
  their checkpoint for a user reply. Operation IDs are durable;
  retrying the same acknowledged broker operation returns its result, not a second
  execution. App retries start a fresh job. There is no unsafe mid-script resume or
  automatic restart of an inference request whose acknowledgement was lost.
- Actual input compatibility, missing keys, upstream defects, unavailable target
  environments and exhausted budgets are concrete blockers. “No fixed adapter”
  alone is not one. A runtime signature and a model’s classification do not prove
  the artifact is a functioning game; startup/gameplay confirmation remains separate.

## Verification scope

Standard tests use controlled model/broker replies for protocol, budgets, privacy,
editing, non-identical replay and rejected evidence. Opt-in `test_real_workers.py`
uses actual offline containers, the real Unix broker, synthetic owned inputs and
controlled model replies to compile a C program, stage data, check read-only input
mounts, replay, test operation idempotency and cancellation. A second case assembles
the checksummed public OpenLara Windows runtime with synthetic level data; it does
not test gameplay. CI runs these cases against a real rootless daemon under a
delegated user service. The rootful **test-only** switch is rejected by the production
app. This validates the Ubuntu CI deployment profile, not an arbitrary Unraid host.

No authenticated live provider/Unraid deployment or native Windows/gameplay check
was performed in the cloud implementation session. See the broader acceptance
matrix in [the design proposal](AUTONOMOUS_PORTING_PROPOSAL.md); large-storage and
Windows backends and broader live-project/model acceptance remain follow-up work.
