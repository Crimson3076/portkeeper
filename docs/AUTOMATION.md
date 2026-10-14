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

Successful recipes live in the project’s `autonomous_recipe`, with a distinct
`schema: 1`, `runner: isolated_scripts` contract. They do not reinterpret the
schema-2 packaging definition. Public replay files are checksummed beneath
`data/automation-recipes/`. Script edits increment the recipe revision and require
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
mkdir -p "$PORTKEEPER_BROKER_DIRECTORY/exchange"
chmod 700 "$PORTKEEPER_BROKER_DIRECTORY"

python3 -m portkeeper.worker_broker \
  --root "$PORTKEEPER_BROKER_DIRECTORY/exchange" \
  --input-root "$PORTKEEPER_OWNED_DIRECTORY" \
  --socket "$PORTKEEPER_BROKER_DIRECTORY/broker.sock" \
  --protected-root "$PWD/.data" \
  --image "$PORTKEEPER_WORKER_IMAGE_ID"
```

Keep the broker running under the host’s service manager. In another shell with the
same exported directory settings, start the app using the optional rootless override:

```bash
docker compose -f compose.yaml -f compose.automation.yaml up -d --build
```

The input directory should contain game inputs only. Supply additional
`--protected-root` paths if controller data or account credentials could occur
inside that collection; the broker rejects both a protected path and any selected
folder containing it. The exchange directory cannot overlap the input collection.

Inside this **rootless** Compose deployment, UID 0 maps to the unprivileged host
account and lets the app reach the same owner-only socket/files. This override is
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

## Enable and use it

1. Connect your model in **Settings → Drafting assistant**.
2. Open **Settings → Advanced → Storage & runners → Automatic port setup**. Confirm
   the broker reports ready, enable automatic setup and choose eligible providers
   for this deployment. Provider-specific subscription eligibility is an operator
   choice; no undocumented API-key fallback or account-token proxy is introduced.
3. Add a port and preserve its source. Choose **Set up automatically**. A unique
   matching local input can be selected automatically; ambiguous/missing matches
   pause the job with a **Waiting for you** question, rather than taking a guess.
   Choose a match in the dropdown or browse your collection, then click
   **Continue setup**. Models can also ask a short question with suggested answers
   or a free-text reply. Questions appear beside the Updates logs and under
   Advanced → Automatic setup; the main next-step card points to the answer.
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
