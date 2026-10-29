"""Match registered native-port imports after RomM discovers their files."""
import json
import time
import hashlib
import uuid
import urllib.parse
import urllib.request
from pathlib import Path

from .romm import _OriginRedirect, _origin, _atomic_json, _cover_url, artwork_url
from .safety import Invalid
from .romm_schedule import miss_due, note_search


def validate_identity(identity):
    if not isinstance(identity, dict) or any(k not in {"filename_prefix", "game", "igdb_id", "romm_rom_id", "cover_policy", "url_cover"} for k in identity):
        raise Invalid("Invalid game metadata link")
    result = dict(identity)
    for key in ("filename_prefix", "game"):
        if not isinstance(result.get(key), str) or not result[key].strip() or len(result[key]) > 200:
            raise Invalid("Enter a game title and package filename prefix")
        result[key] = result[key].strip()
    if any(c in result['filename_prefix'] for c in '/\\'):
        raise Invalid("Package prefix must be a filename, not a path")
    for key in ('igdb_id', 'romm_rom_id'):
        if key in result and (type(result[key]) is not int or result[key] < 1):
            raise Invalid("Metadata IDs must be positive integers")
    policy = result.get('cover_policy', 'preserve')
    if not result.get('igdb_id') and policy != 'custom':
        raise Invalid("Enter an IGDB game ID or reuse a matched RomM entry")
    if policy not in ('preserve', 'romm', 'custom'):
        raise Invalid("Choose whether to keep covers, reuse RomM artwork or apply a chosen cover")
    if policy == 'custom':
        result['url_cover'] = artwork_url(result.get('url_cover'))
    else:
        result.pop('url_cover', None)
    if result.get('cover_policy') == 'romm' and not result.get('romm_rom_id'):
        raise Invalid("Link a RomM entry to reuse its artwork")
    return result


class ImportIdentities:
    def __init__(self, romm, data_dir, store=None):
        self.romm = romm
        self.path = Path(data_dir) / 'romm-import-identities.json'
        self.store = store
        self.receipts = Path(data_dir) / 'romm-artwork-receipts.json'

    def sync(self, misses=None, at=None):
        """Match imports. With a miss cache, prefixes that recently found nothing are not searched again."""
        at = at or time.time()
        if self.path.is_symlink():
            raise Invalid('Import identity configuration must not be a symlink')
        identities = json.loads(self.path.read_text()) if self.path.is_file() else []
        if not isinstance(identities, list) or len(identities) > 100:
            raise Invalid('Import identities must be a list of at most 100 games')
        if self.store:
            registered = {item['filename_prefix']: item for item in identities}
            for project in self.store.all('projects'):
                if project.get('metadata'):
                    identity = validate_identity(project['metadata'])
                    registered[identity['filename_prefix']] = identity
                    if project.get('library_basename'):
                        alias = {**identity, 'filename_prefix': project['library_basename']}
                        registered[alias['filename_prefix']] = alias
            identities = list(registered.values())
        if not identities:
            return {'matched': 0}
        if self.receipts.is_symlink():
            raise Invalid('Artwork receipts must not be a symlink')
        receipts = json.loads(self.receipts.read_text()) if self.receipts.is_file() else {}
        config = self.romm._config()
        matched = 0
        for identity in identities:
            identity = dict(identity)
            miss_key = 'identity:' + str(identity.get('filename_prefix')).casefold()
            if misses is not None and not miss_due(misses, miss_key, at):
                continue
            reference = None
            artwork = None
            artwork_type = None
            if identity.get('romm_rom_id'):
                reference = self.romm._json('roms/' + str(identity['romm_rom_id']))
                if not reference.get('igdb_id') and identity.get('cover_policy') != 'custom':
                    continue  # Wait for the user's manual match; never guess.
                if reference.get('igdb_id'):
                    identity['igdb_id'] = reference['igdb_id']
                if self.store and reference.get('igdb_id'):
                    for project in self.store.all('projects'):
                        metadata = project.get('metadata') or {}
                        if metadata.get('romm_rom_id') == identity['romm_rom_id'] and metadata.get('igdb_id') != reference['igdb_id']:
                            self.store.update('projects', project['id'], metadata={**metadata, 'igdb_id': reference['igdb_id']})
                if identity.get('cover_policy') == 'romm':
                    identity['url_cover'] = reference.get('url_cover', '')
                    if not identity['url_cover'] and reference.get('path_cover_large'):
                        url = _cover_url(config['url'], reference['path_cover_large'])
                        req = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + config['token']})
                        with urllib.request.build_opener(_OriginRedirect(_origin(config['url']))).open(req, timeout=30) as response:
                            artwork_type = response.headers.get_content_type()
                            artwork = response.read(10 * 1024 * 1024 + 1)
                        if artwork_type not in ('image/png', 'image/jpeg', 'image/webp') or len(artwork) > 10 * 1024 * 1024:
                            raise Invalid('Invalid or oversized RomM cover')
            prefix, game, igdb_id = identity['filename_prefix'], identity['game'], identity.get('igdb_id')
            custom = identity.get('cover_policy') == 'custom'
            if not isinstance(prefix, str) or not prefix or '/' in prefix or not isinstance(game, str) or not game or not ((custom and igdb_id is None) or (type(igdb_id) is int and igdb_id >= 1)):
                raise Invalid('Invalid registered game identity')
            offset = 0
            found = False
            while True:
                params = urllib.parse.urlencode({'search_term': prefix, 'limit': 100, 'offset': offset, 'with_files': 'false', 'with_char_index': 'false', 'with_filter_values': 'false', 'with_rom_id_index': 'false'})
                result = self.romm._json('roms?' + params)
                items = result.get('items', [])
                for record in items:
                    # Only explicit archive prefixes on PC platforms; console inputs remain untouched.
                    filename = record.get('fs_name', '')
                    if record.get('platform_slug') not in ('linux', 'win') or not filename.casefold().startswith(prefix.casefold() + '-') or not filename.lower().endswith('.zip'):
                        continue
                    found = True
                    # The reference belongs to the user; never overwrite their edits. A cover
                    # chosen in Portkeeper is the one exception, so every platform shares it.
                    is_reference = bool(reference) and record['id'] == identity['romm_rom_id']
                    if is_reference and not custom:
                        continue
                    cover = identity.get('url_cover', '') if identity.get('cover_policy', 'legacy') != 'preserve' else ''
                    art_key = hashlib.sha256(artwork).hexdigest() if artwork else cover
                    receipt_key = str(record['id'])
                    current_name = record.get('name', filename)
                    target_igdb = record.get('igdb_id') if is_reference or igdb_id is None else igdb_id
                    name = current_name if is_reference or igdb_id is None or current_name.startswith(game + ' — ') else game + ' — ' + current_name
                    cover_done = not cover or record.get('url_cover') == cover or (custom and receipts.get(receipt_key) == art_key)
                    if record.get('igdb_id') == target_igdb and current_name == name and cover_done and (not artwork or receipts.get(receipt_key) == art_key):
                        continue
                    data = {'name': name, 'summary': record.get('summary') or ''}
                    if target_igdb:
                        data['igdb_id'] = str(target_igdb)
                    if cover:
                        data['url_cover'] = cover
                    body = urllib.parse.urlencode(data).encode()
                    content_type = 'application/x-www-form-urlencoded'
                    if artwork:
                        boundary = 'portkeeper-' + uuid.uuid4().hex
                        parts = []
                        for key, value in data.items():
                            parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n').encode())
                        ext = {'image/png': 'png', 'image/jpeg': 'jpg', 'image/webp': 'webp'}[artwork_type]
                        parts.append((f'--{boundary}\r\nContent-Disposition: form-data; name="artwork"; filename="cover.{ext}"\r\nContent-Type: {artwork_type}\r\n\r\n').encode() + artwork + b'\r\n')
                        parts.append(f'--{boundary}--\r\n'.encode())
                        body = b''.join(parts)
                        content_type = 'multipart/form-data; boundary=' + boundary
                    request = urllib.request.Request(config['url'] + '/api/roms/' + str(record['id']), data=body, headers={'Authorization': 'Bearer ' + config['token'], 'Content-Type': content_type}, method='PUT')
                    opener = urllib.request.build_opener(_OriginRedirect(_origin(config['url'])))
                    with opener.open(request, timeout=60) as response:
                        updated = json.load(response)
                    if target_igdb and updated.get('igdb_id') != target_igdb:
                        raise Invalid('RomM did not retain the registered game identity')
                    if artwork or custom:
                        receipts[receipt_key] = art_key
                        _atomic_json(self.receipts, receipts)
                    matched += 1
                offset += len(items)
                if not items or offset >= result.get('total', offset):
                    break
            if misses is not None:
                note_search(misses, miss_key, found, at)
        return {'matched': matched}
