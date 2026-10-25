#!/bin/sh
# Started as root (Unraid's default): take ownership of the app data folder,
# then drop to PUID:PGID. Started as any other user (Compose): run as-is.
set -eu

if [ "$(id -u)" = "0" ]; then
    PUID="${PUID:-99}"
    PGID="${PGID:-100}"
    if [ "$PUID" = "0" ]; then
        # The rootless Compose deployment uses container UID 0 to access its
        # owner's private broker socket. Accept it only when the kernel maps
        # that UID to a non-root ID in the parent namespace, never host root.
        portkeeper_mapped_root=false
        while read -r portkeeper_inner_uid portkeeper_parent_uid portkeeper_map_count; do
            if [ "$portkeeper_inner_uid" = "0" ] && [ "$portkeeper_parent_uid" != "0" ]; then
                portkeeper_mapped_root=true
            fi
        done < /proc/self/uid_map
        if [ "$portkeeper_mapped_root" != "true" ]; then
            echo "Refusing to run Portkeeper as host root; set PUID to a non-root user." >&2
            exit 1
        fi
        exec "$@"
    fi
    if [ "$(stat -c %u:%g /data)" != "$PUID:$PGID" ]; then
        chown -R "$PUID:$PGID" /data
    fi
    # A new empty bulk mount may be claimed, but never recursively chown an
    # existing collection. Operators retain control of large shared directories.
    if [ -n "${PORTKEEPER_STORAGE_DIR:-}" ]; then
        if [ ! -d "$PORTKEEPER_STORAGE_DIR" ]; then
            echo "Bulk storage is missing; mount PORTKEEPER_STORAGE_DIR before starting." >&2
            exit 1
        fi
        if [ "$(stat -c %u "$PORTKEEPER_STORAGE_DIR")" = "0" ] && [ -z "$(ls -A "$PORTKEEPER_STORAGE_DIR")" ]; then
            chown "$PUID:$PGID" "$PORTKEEPER_STORAGE_DIR"
        fi
        if ! setpriv --reuid="$PUID" --regid="$PGID" --clear-groups test -w "$PORTKEEPER_STORAGE_DIR"; then
            echo "Bulk storage is not writable by $PUID:$PGID; fix the mount ownership." >&2
            exit 1
        fi
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
