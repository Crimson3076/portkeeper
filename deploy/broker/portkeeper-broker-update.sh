#!/usr/bin/env bash
# Self-update for the Portkeeper broker VM. Run by portkeeper-broker-update.timer.
# Installs a new GitHub release only after its test suite passes, restarts the
# broker and rolls back to the previous release if the new one is not healthy.
set -euo pipefail

REPO="${PORTKEEPER_REPO:-Crimson3076/portkeeper}"
BASE="${PORTKEEPER_INSTALL_DIR:-$HOME/portkeeper-broker}"
SERVICE="${PORTKEEPER_BROKER_SERVICE:-portkeeper-broker}"
PORT="${PORTKEEPER_BROKER_PORT:-8788}"
SYSTEMCTL=(systemctl)
[ -n "${PORTKEEPER_USER_SERVICE:-}" ] && SYSTEMCTL=(systemctl --user)

mkdir -p "$BASE/releases"
exec 9>"$BASE/.update.lock"
flock -n 9 || { echo "another update is running"; exit 0; }

tag=$(curl -fsSL --max-time 30 -H 'Accept: application/vnd.github+json' \
  "https://api.github.com/repos/$REPO/releases/latest" | python3 -I -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')
[[ "$tag" =~ ^[A-Za-z0-9._-]{1,64}$ ]] || { echo "unexpected release tag"; exit 1; }

current=$(basename "$(readlink -f "$BASE/current" 2>/dev/null || true)")
if [ "$tag" = "$current" ]; then echo "already on $tag"; exit 0; fi
if [ -e "$BASE/rejected-$tag" ]; then echo "$tag previously failed; waiting for a newer release"; exit 0; fi

work=$(mktemp -d "$BASE/releases/.incoming-XXXXXX")
trap 'rm -rf "$work"' EXIT
curl -fsSL --max-time 300 -o "$work/release.tar.gz" "https://github.com/$REPO/archive/refs/tags/$tag.tar.gz"
mkdir "$work/src"
tar -xzf "$work/release.tar.gz" -C "$work/src" --strip-components=1
# Tests that need Docker/models skip themselves; everything else must pass.
if ! (cd "$work/src" && python3 -W error -m unittest discover -s tests > "$work/tests.log" 2>&1); then
  tail -n 40 "$work/tests.log" >&2
  touch "$BASE/rejected-$tag"
  echo "tests failed for $tag; keeping $current"
  exit 1
fi
rm -rf "$BASE/releases/$tag"
mv "$work/src" "$BASE/releases/$tag"

previous=$(readlink -f "$BASE/current" 2>/dev/null || true)
ln -sfn "$BASE/releases/$tag" "$BASE/current.new"
mv -T "$BASE/current.new" "$BASE/current"
"${SYSTEMCTL[@]}" restart "$SERVICE"

# No -I: it drops the current directory, and the release is an unpacked source tree.
protocol=$(cd "$BASE/current" && python3 -c 'from portkeeper.broker_net import PROTOCOL; print(PROTOCOL)' || true)
protocol=${protocol:-0}
healthy=""
for _ in $(seq 1 30); do
  sleep 2
  if curl -fsk --max-time 5 -H "X-Portkeeper-Protocol: $protocol" "https://127.0.0.1:$PORT/v1/health" >/dev/null; then healthy=1; break; fi
done
if [ -z "$healthy" ]; then
  echo "$tag did not become healthy; rolling back"
  touch "$BASE/rejected-$tag"
  if [ -n "$previous" ]; then
    ln -sfn "$previous" "$BASE/current.new"; mv -T "$BASE/current.new" "$BASE/current"
    "${SYSTEMCTL[@]}" restart "$SERVICE"
  fi
  exit 1
fi
echo "updated to $tag"
# Keep the current and one previous release.
ls -1dt "$BASE"/releases/*/ 2>/dev/null | tail -n +3 | xargs -r rm -rf
