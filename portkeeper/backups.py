"""Small, allowlisted state snapshots. Never copy a data directory wholesale."""
import argparse
from contextlib import closing
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3

from .safety import Invalid, checksum, confined
from .storage import Store, identifier, now

KIND = 'portkeeper-state-backup'
MAX_FILE = 4 * 1024**2
MAX_METADATA = 64 * 1024**2
METADATA_DIRS = ('catalog', 'drafts', 'recipes', 'definitions', 'build-definitions')
EXTENSIONS = {'.json', '.toml', '.yaml', '.yml', '.py', '.sh', '.cmd', '.bat', '.ps1', '.txt', '.md'}
FILES = ('romm-import-identities.json', 'romm-artwork-receipts.json',
         'secrets/assistant.json', 'secrets/romm.json', 'secrets/home-assistant.json',
         'secrets/agents/codex/.codex/auth.json',
         'secrets/agents/claude_code/.claude/.credentials.json',
         'secrets/agents/claude_code/.claude.json',
         'secrets/agents/gemini/.gemini/oauth_creds.json',
         'secrets/agents/gemini/.gemini/google_accounts.json')


def keep_count(value=None):
    try:
        value = int(os.environ.get('PORTKEEPER_BACKUP_KEEP', '3') if value is None else value)
    except (ValueError, TypeError):
        raise Invalid('PORTKEEPER_BACKUP_KEEP must be an integer between 1 and 20') from None
    if not 1 <= value <= 20:
        raise Invalid('PORTKEEPER_BACKUP_KEEP must be between 1 and 20')
    return value


def metadata_files(data):
    for name in FILES:
        path = confined(data, name)
        if path.is_file():
            yield path
    for name in METADATA_DIRS:
        root = confined(data, name)
        if not root.is_dir():
            continue
        for directory, dirs, files in os.walk(root, followlinks=False):
            dirs[:] = [item for item in dirs if not (Path(directory) / item).is_symlink()]
            for item in files:
                path = Path(directory) / item
                if path.suffix.lower() in EXTENSIONS and not path.is_symlink() and path.is_file():
                    yield path


def managed_manifest(root):
    """Foreign/legacy/edited backups are never automatic retention targets."""
    if root.is_symlink() or not re.fullmatch(r'state-[a-f0-9]{32}', root.name):
        return None
    try:
        marker = confined(root, 'manifest.json', must_exist=True)
        if marker.stat().st_size > 4 * 1024**2:
            return None
        manifest = json.loads(marker.read_text())
        if manifest.get('kind') != KIND or manifest.get('schema') != 1 or not isinstance(manifest.get('files'), dict):
            return None
        for name in manifest['files']:
            path = Path(name)
            text_recipe = (len(path.parts) > 1 and path.parts[0] in METADATA_DIRS and path.suffix.lower() in EXTENSIONS)
            if name != 'portkeeper.sqlite3' and name not in FILES and not text_recipe:
                return None
        actual = set()
        for directory, dirs, files in os.walk(root, followlinks=False):
            if any((Path(directory) / name).is_symlink() or (Path(directory) / name).is_mount() for name in dirs + files):
                return None
            actual.update((Path(directory) / name).relative_to(root).as_posix() for name in files)
        if actual != set(manifest['files']) | {'manifest.json'}:
            return None
        for name, digest in manifest['files'].items():
            if checksum(confined(root, name, must_exist=True)) != digest:
                return None
        if not isinstance(manifest.get('created_at'), str):
            return None
        return manifest
    except (Invalid, OSError, ValueError, TypeError):
        return None


def create_backup(store, data, *, reason='manual', keep=None, context=None):
    keep = keep_count(keep)
    data = Path(data).resolve()
    parent = confined(data, 'state-backups')
    parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    parent.chmod(0o700)
    # Serialize different CLI/server processes, not only threads in one Engine.
    descriptor = os.open(parent / '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, 'a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        key = identifier()
        temporary = parent / ('.incomplete-' + key)
        final = parent / ('state-' + key)
        temporary.mkdir(mode=0o700)
        try:
            with store.connect() as source, closing(sqlite3.connect(temporary / 'portkeeper.sqlite3')) as target:
                source.backup(target)
                # Compact only the copy, so cleared log pages don't inflate snapshots.
                target.execute('VACUUM')
            total = count = 0
            for path in metadata_files(data):
                size = path.stat().st_size
                total += size
                count += 1
                if size > MAX_FILE or total > MAX_METADATA or count > 10000:
                    raise Invalid('State metadata exceeds the bounded backup size; review it before cleanup')
                destination = temporary / path.relative_to(data)
                destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
                shutil.copyfile(path, destination, follow_symlinks=False)
                destination.chmod(0o600)
            (temporary / 'portkeeper.sqlite3').chmod(0o600)
            manifest = {'kind': KIND, 'schema': 1, 'created_at': now(), 'reason': reason, 'context': context or {},
                        'files': {path.relative_to(temporary).as_posix(): checksum(path)
                                  for path in temporary.rglob('*') if path.is_file()}}
            (temporary / 'manifest.json').write_text(json.dumps(manifest, indent=2))
            (temporary / 'manifest.json').chmod(0o600)
            temporary.rename(final)
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
        eligible = []
        for candidate in parent.glob('state-*'):
            manifest = managed_manifest(candidate)
            if manifest:
                eligible.append((manifest['created_at'], candidate.name, candidate))
        for _, _, candidate in sorted(eligible, reverse=True)[keep:]:
            shutil.rmtree(candidate)
        return final


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--keep', type=int)
    args = parser.parse_args()
    try:
        database = confined(args.data_dir, 'portkeeper.sqlite3', must_exist=True)
        path = create_backup(Store(database), args.data_dir, keep=args.keep)
    except (Invalid, OSError) as error:
        parser.exit(1, str(error) + '\n')
    print(f'State snapshot: {path}')


if __name__ == '__main__':
    main()
