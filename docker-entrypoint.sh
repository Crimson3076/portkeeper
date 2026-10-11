#!/bin/sh
# Started as root (Unraid's default): take ownership of the app data folder,
# then drop to PUID:PGID. Started as any other user (Compose): run as-is.
set -eu

if [ "$(id -u)" = "0" ]; then
    PUID="${PUID:-99}"
    PGID="${PGID:-100}"
    if [ "$PUID" = "0" ]; then
        echo "Refusing to run Portkeeper as root; set PUID to a non-root user." >&2
        exit 1
    fi
    if [ "$(stat -c %u:%g /data)" != "$PUID:$PGID" ]; then
        chown -R "$PUID:$PGID" /data
    fi
    # The library is usually a folder shared with RomM, so it is only claimed
    # when it is the empty root-owned folder Docker creates for a new mapping.
    if [ "$(stat -c %u /library)" = "0" ] && [ -z "$(ls -A /library)" ]; then
        chown "$PUID:$PGID" /library
    fi
    if ! setpriv --reuid="$PUID" --regid="$PGID" --clear-groups test -w /library; then
        echo "Warning: /library is not writable by $PUID:$PGID; publishing will fail." >&2
    fi
    export HOME=/tmp
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups -- "$@"
fi

# Started with --user (Compose, or Unraid with --user 99:100): run as-is.
# Arbitrary UIDs have no passwd entry, so give tools a writable HOME.
if [ ! -w "${HOME:-/}" ]; then
    export HOME=/tmp
fi
exec "$@"
