"""State/bulk separation and an explicit, offline path migration.

No directory is moved or deleted by this module. The operator stops the app and
copies/verifies payloads first; --apply verifies those copies before rewriting DB
paths. Without --apply this command only reports a plan.
"""
import argparse
import json
import os
import re
from pathlib import Path
import sqlite3
import tempfile
from contextlib import closing

from .safety import Invalid, checksum
from .storage import Store, identifier

BULK_NAMES = ('archive', 'prepared', 'work', 'cache', 'automation-recipes',
              'agent-workspaces', 'toolchains', 'demo-inputs')


def default_storage(data):
    data = Path(data).resolve()
    # Existing installations remain usable until explicitly migrated. New CLI
    # installs use a sibling directory; Compose/Unraid explicitly select /storage.
    database = data / 'portkeeper.sqlite3'
    if database.is_file():
        with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
            try:
                row = db.execute("SELECT body FROM settings WHERE id='storage_layout'").fetchone()
            except sqlite3.OperationalError:
                row = None
        if row:
            saved = json.loads(row[0])['value']['storage']
            if not isinstance(saved, str) or not Path(saved).is_absolute():
                raise Invalid('Saved storage layout is invalid; review its configuration')
            return Path(saved)
        return data
    if any((data / name).exists() for name in (*BULK_NAMES, 'inputs', 'library')):
        return data
    return data.with_name(data.name + '-storage')


def storage_identity(root, expected=None):
    marker = root / '.portkeeper-storage-id'
    if marker.is_symlink():
        raise Invalid('Bulk storage identity must not be a symlink')
    if marker.exists() and (not marker.is_file() or marker.stat().st_size != 33):
        raise Invalid('Invalid bulk storage identity; inspect the configured mount')
    if expected and (not marker.is_file() or marker.read_text() != expected + '\n'):
        raise Invalid('The configured bulk storage mount is missing or different; restore its original mount before starting')
    if not marker.exists():
        root.mkdir(parents=True, exist_ok=True)
        with marker.open('x') as stream:
            stream.write(identifier() + '\n')
    value = marker.read_text().strip()
    if not re.fullmatch(r'[a-f0-9]{32}', value):
        raise Invalid('Invalid bulk storage identity; inspect the configured mount')
    return value


def check_layout(engine):
    try:
        layout = engine.store.get('settings', 'storage_layout')['value']
        previous = layout['storage']
    except KeyError:
        layout, previous = {}, None
    legacy = any(path.is_dir() and any(path.iterdir()) for path in (engine.data / name for name in BULK_NAMES))
    if (previous and Path(previous).resolve() != engine.storage) or (not previous and engine.storage != engine.data and legacy):
        raise Invalid('Bulk storage changed. Stop Portkeeper, copy and verify the old storage, then run '
                      'python -m portkeeper.storage_layout to review and apply the path migration. No files were moved or deleted.')
    value = {'storage': str(engine.storage)}
    if engine.storage != engine.data:
        value['storage_id'] = storage_identity(engine.storage, layout.get('storage_id'))
    engine.store.put('settings', {'id': 'storage_layout', 'value': value})


def tree_manifest(root):
    """Do not follow symlinks or enter nested mounts during copy verification."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        raise Invalid(f'Expected a real directory: {root}')
    result = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in dirs + files:
            path = Path(directory) / name
            key = path.relative_to(root).as_posix()
            if path.is_mount():
                raise Invalid(f'Nested mount requires separate review: {path}')
            if path.is_symlink():
                target = os.readlink(path)
                if Path(target).is_absolute():
                    raise Invalid(f'Absolute symlink needs migration review before removing old copies: {path}')
                result[key] = ['link', target]
            elif path.is_file():
                result[key] = ['file', path.stat().st_size, checksum(path), path.stat().st_mode & 0o777]
            elif path.is_dir():
                result[key] = ['directory']
            else:
                raise Invalid(f'Unsupported special file: {path}')
    return result


def rewrite(value, mappings):
    if isinstance(value, dict):
        return {key: rewrite(item, mappings) for key, item in value.items()}
    if isinstance(value, list):
        return [rewrite(item, mappings) for item in value]
    if isinstance(value, str):
        for old, new in mappings:
            if value == old or value.startswith(old + '/'):
                return new + value[len(old):]
    return value


def atomic_write(path, value):
    descriptor, name = tempfile.mkstemp(prefix='.storage-migration-', dir=path.parent)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, path.stat().st_mode & 0o777)
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def migrate(data, old_storage, new_storage, *, apply=False, old_library=None, new_library=None, input_root='/inputs'):
    data, old, new = map(lambda p: Path(p).resolve(), (data, old_storage, new_storage))
    if old == new or old.is_relative_to(new) or new.is_relative_to(old):
        raise Invalid('Old and new bulk roots must be separate, non-nested directories')
    if data == new or data.is_relative_to(new) or new.is_relative_to(data):
        raise Invalid('New bulk storage must be outside the state directory')
    if not (data / 'portkeeper.sqlite3').is_file():
        raise Invalid('Choose the existing state database; migration never creates one')
    mappings = [(str(old / name), str(new / name)) for name in BULK_NAMES]
    if bool(old_library) != bool(new_library):
        raise Invalid('Supply both old and new library paths, or neither')
    if old_library:
        mappings.append((str(Path(old_library).resolve()), str(Path(new_library).resolve())))
    readonly = Path(input_root).resolve()
    if any(a == '/' or b == '/' or Path(a).is_relative_to(readonly) or readonly.is_relative_to(Path(a)) or
           Path(b).is_relative_to(readonly) or readonly.is_relative_to(Path(b)) for a, b in mappings):
        raise Invalid('Migration must not move or rewrite the read-only input collection')
    mappings.sort(key=lambda item: len(item[0]), reverse=True)
    database = data / 'portkeeper.sqlite3'
    # Planning never creates tables or writes the database.
    records = {}
    with closing(sqlite3.connect(database.as_uri() + '?mode=ro', uri=True)) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'projects', 'settings'} <= tables:
            raise Invalid('This file is not a Portkeeper state database')
        for table in sorted(Store.tables & tables):
            records[table] = [json.loads(row[0]) for row in db.execute(f'SELECT body FROM {table}')]
    saved_layout = next((r.get('value', {}).get('storage') for r in records.get('settings', [])
                         if r.get('id') == 'storage_layout'), None)
    if saved_layout and saved_layout not in (str(old), str(new)):
        raise Invalid('Use the application’s path namespace for migration. Container paths and host bind-source paths are different.')
    for capture in records.get('captures', []):
        location = capture.get('root')
        if location and not any(location == prefix or location.startswith(prefix + '/')
                                for prefix in (str(old / 'archive'), str(new / 'archive'))):
            raise Invalid('Capture paths do not match these storage roots; run maintenance with the application’s original path aliases')
    # A paused setup also owns a checkpoint in the separately deployed broker.
    # Never silently invalidate that checkpoint by partially moving storage.
    if any(r.get('status') in ('queued', 'running', 'waiting_user') for r in records.get('automation', [])):
        raise Invalid('Finish or explicitly cancel automatic setup before migrating storage')
    if any(r.get('status') in ('queued', 'running', 'building', 'checking') for r in records.get('jobs', [])):
        raise Invalid('Finish or cancel running jobs before migrating storage')
    changed = []
    for table, items in records.items():
        for record in items:
            updated = rewrite(record, mappings)
            if updated != record:
                changed.append((table, record['id'], updated))
    from .backups import metadata_files
    files = []
    for path in metadata_files(data):
        if path.suffix != '.json':
            continue
        if path.stat().st_size > 4 * 1024**2:
            raise Invalid('Review oversized metadata before migrating paths')
        raw = path.read_bytes()
        try:
            value = json.loads(raw)
        except ValueError:
            continue
        updated = rewrite(value, mappings)
        if updated != value:
            files.append((path, raw, (json.dumps(updated, indent=2) + '\n').encode()))
    plan = {'mappings': [{'old': a, 'new': b} for a, b in mappings],
            'records_changed': len(changed), 'config_files_changed': len(files), 'applied': False, 'deletions': []}
    if not apply:
        return plan
    if not new.is_dir():
        raise Invalid('Copy the bulk data to an existing new storage directory before applying')
    for a, b in mappings:
        if Path(a).exists() and tree_manifest(a) != tree_manifest(b):
            raise Invalid(f'Copy verification failed for {a}; database unchanged')
    from .backups import create_backup
    store = Store(database)
    backup = create_backup(store, data, reason='storage-migration')
    # All tables are rewritten together; a crash cannot leave half the paths changed.
    written = []
    try:
        with store.lock, store.connect() as db:
            for table, key, value in changed:
                db.execute(f'UPDATE {table} SET body=? WHERE id=?', (json.dumps(value), key))
            marker = {'id': 'storage_layout', 'value': {'storage': str(new), 'storage_id': storage_identity(new)}}
            db.execute('INSERT INTO settings(id,body) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET body=excluded.body',
                       ('storage_layout', json.dumps(marker)))
            for path, original, value in files:
                atomic_write(path, value)
                written.append((path, original))
    except Exception:
        for path, original in reversed(written):
            atomic_write(path, original)
        raise
    return {**plan, 'applied': True, 'backup': str(backup)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data-dir', type=Path, required=True)
    parser.add_argument('--old-storage', type=Path, required=True)
    parser.add_argument('--new-storage', type=Path, required=True)
    parser.add_argument('--old-library', type=Path)
    parser.add_argument('--new-library', type=Path)
    parser.add_argument('--input-root', type=Path, default=Path('/inputs'), help='Actual read-only input root to protect')
    parser.add_argument('--apply', action='store_true', help='App must be stopped; verify copies, save a state backup and rewrite paths')
    args = parser.parse_args()
    try:
        result = migrate(args.data_dir, args.old_storage, args.new_storage, apply=args.apply,
                         old_library=args.old_library, new_library=args.new_library, input_root=args.input_root)
    except (Invalid, OSError) as error:
        parser.exit(1, str(error) + '\n')
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
