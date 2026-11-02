#!/usr/bin/env bash
# Self-update for the Portkeeper build broker VM (runs from a systemd user timer).
# Installs the latest GitHub release only if its tests pass, restarts the broker,
# and rolls back to the previous release if the new one does not become ready.
set -euo pipefail

BASE="${PORTKEEPER_BROKER_HOME:-$HOME/portkeeper-broker}"
REPO="${PORTKEEPER_REPO:-Crimson3076/portkeeper}"
LINK_URL="${PORTKEEPER_BROKER_CHECK_URL:-https://127.0.0.1:8799}"
SERVICE="${PORTKEEPER_BROKER_SERVICE:-portkeeper-broker.service}"
KEEP=3

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*"; }
mkdir -p "$BASE/releases"
exec 9>"$BASE/update.lock"
flock -n 9 || { log "another update is running"; exit 0; }

tag="$(curl -fsSL --max-time 30 -H 'Accept: application/vnd.github+json' \
  "https://api.github.com/repos/$REPO/releases/latest" |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')"
[[ "$tag" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,60}$ ]] || { log "refusing odd tag name"; exit 1; }

installed="$(cat "$BASE/installed" 2>/dev/null || true)"
if [[ "$tag" == "$installed" ]]; then log "already on $tag"; exit 0; fi
if grep -qxF "$tag" "$BASE/rejected" 2>/dev/null; then log "$tag was rejected earlier; waiting for a newer release"; exit 0; fi
reject() { echo "$tag" >> "$BASE/rejected"; log "rejected $tag: $1"; }

status=0
(cd "$BASE/current" 2>/dev/null && python3 -m portkeeper.broker_net check --url "$LINK_URL" --idle >/dev/null 2>&1) || status=$?
if [[ "$status" == 3 ]]; then log "a build is running; $tag will be installed on a later run"; exit 0; fi

staging="$BASE/releases/$tag.new"
rm -rf "$staging"; mkdir -p "$staging"
trap 'rm -rf "$staging"' EXIT
curl -fsSL --max-time 300 "https://github.com/$REPO/archive/refs/tags/$tag.tar.gz" |
  tar -xz --strip-components=1 --no-same-owner -C "$staging"

log "running tests for $tag"
if ! (cd "$staging" && timeout 1800 python3 -m unittest discover -s tests >"$BASE/last-test.log" 2>&1); then
  reject "tests failed (see $BASE/last-test.log)"; exit 1
fi
version="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$staging/portkeeper/__init__.py")"
rm -rf "$BASE/releases/$tag"; mv "$staging" "$BASE/releases/$tag"; trap - EXIT

previous="$(readlink "$BASE/current" 2>/dev/null || true)"
switch() { ln -sfn "$1" "$BASE/current.new"; mv -T "$BASE/current.new" "$BASE/current"; systemctl --user restart "$SERVICE"; }

ready() {
  for _ in $(seq 1 60); do
    if (cd "$BASE/current" && python3 -m portkeeper.broker_net check --url "$LINK_URL" --version "$1" >/dev/null 2>&1); then return 0; fi
    sleep 2
  done
  return 1
}

switch "releases/$tag"
if ready "$version"; then
  echo "$tag" > "$BASE/installed"
  log "now running $tag"
else
  reject "broker did not become ready"
  if [[ -n "$previous" ]]; then switch "$previous"; log "rolled back to $previous"; fi
  exit 1
fi

# Keep the newest releases, never the active one.
ls -1dt "$BASE"/releases/*/ 2>/dev/null | tail -n +$((KEEP + 1)) | while read -r old; do
  [[ "$(readlink -f "$BASE/current")" == "$(readlink -f "$old")" ]] || rm -rf "$old"
done
