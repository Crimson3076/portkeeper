"""Read-only RomM integration with server-side credentials and verified ROM caching.

Wire contract follows RomM 5.2.0's endpoints/roms and endpoints/roms/files.py.
No browser credentials, password login, uploads, or third-party download URLs.
"""
import hashlib
import ipaddress
import json
import os
import re
import stat
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from .safety import Invalid, checksum

MAX_ARCHIVE = 1024 * 1024 * 1024
MAX_ROM = 128 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024
MAX_MEMBERS = 50000
MAX_JSON = 8 * 1024 * 1024
ROM_EXTENSIONS = {'.z64', '.v64', '.n64'}


def _origin(url):
    parsed = urllib.parse.urlsplit(url)
    return (parsed.scheme.lower(), (parsed.hostname or '').lower(), parsed.port or (443 if parsed.scheme == 'https' else 80))


def _server_url(value):
    if not isinstance(value, str) or not value.strip() or any(ord(c) < 32 for c in value):
        raise Invalid('Enter a valid RomM server URL')
    parsed = urllib.parse.urlsplit(value.strip())
    try:
        parsed.port
    except ValueError:
        raise Invalid('Invalid RomM server port') from None
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise Invalid('RomM URL must not contain credentials, query parameters, or a fragment')
    loopback = parsed.hostname.lower() == 'localhost'
    try:
        loopback = loopback or ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        pass
    if parsed.scheme != 'https' and not (parsed.scheme == 'http' and loopback):
        raise Invalid('RomM requires HTTPS; HTTP is allowed only for a loopback server')
    if '\\' in parsed.path or any(p in ('.', '..') for p in parsed.path.split('/')):
        raise Invalid('Invalid RomM URL path')
    return value.strip().rstrip('/')


def _atomic_json(path, record):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if path.is_symlink() or path.parent.is_symlink():
        raise Invalid('Credential or cache paths must not be symlinks')
    fd, name = tempfile.mkstemp(prefix='.pending-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            json.dump(record, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


class _OriginRedirect(urllib.request.HTTPRedirectHandler):
    def __init__(self, origin):
        self.origin = origin

    def redirect_request(self, request, fp, code, msg, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.username or parsed.password or _origin(newurl) != self.origin:
            raise Invalid('RomM redirected to a different origin; credentials were not forwarded')
        return super().redirect_request(request, fp, code, msg, headers, newurl)


def normalize_rom(data):
    if len(data) < 4 or len(data) % 4 or len(data) > MAX_ROM:
        raise Invalid('ROM must have a size divisible by four and at most 128 MiB')
    header = data[:4]
    if header == b'\x80\x37\x12\x40':
        return data, 'big-endian'
    result = bytearray(data)
    if header == b'\x37\x80\x40\x12':
        result[0::2], result[1::2] = result[1::2], result[0::2]
        order = 'byte-swapped'
    elif header == b'\x40\x12\x37\x80':
        result[0::4], result[1::4], result[2::4], result[3::4] = result[3::4], result[2::4], result[1::4], result[0::4]
        order = 'little-endian'
    else:
        raise Invalid('Unrecognized N64 ROM header')
    return bytes(result), order


def _members(bundle):
    members = bundle.infolist()
    if len(members) > MAX_MEMBERS:
        raise Invalid('ROM archive has too many entries')
    seen, files, total = set(), set(), 0
    for item in members:
        name = item.filename
        clean = name.rstrip('/')
        if (not clean or '\\' in name or ':' in name or any(ord(c) < 32 for c in name)
                or PurePosixPath(name).is_absolute() or any(p in ('', '.', '..') for p in clean.split('/'))):
            raise Invalid('ROM archive contains an unsafe path')
        folded = clean.casefold()
        if folded in seen:
            raise Invalid('ROM archive contains duplicate names')
        seen.add(folded)
        if not item.is_dir():
            files.add(folded)
        kind = stat.S_IFMT(item.external_attr >> 16)
        if kind not in (0, stat.S_IFREG, stat.S_IFDIR) or item.flag_bits & 1:
            raise Invalid('ROM archive contains encrypted entries, links, or special files')
        total += item.file_size
        if item.file_size > MAX_ARCHIVE or total > MAX_EXPANDED:
            raise Invalid('ROM archive exceeds the expanded size limit')
    for item in members:
        parts = item.filename.rstrip('/').casefold().split('/')
        if any('/'.join(parts[:i]) in files for i in range(1, len(parts))):
            raise Invalid('ROM archive has a file/directory collision')
    return members


def _bounded(stream, maximum):
    data = stream.read(maximum + 1)
    if len(data) > maximum:
        raise Invalid('ROM exceeds the allowed size')
    return data


def _positive(value, field='ROM id'):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise Invalid(f'{field} must be a positive integer')
    return value


def _normalize_file(record):
    return {key: record.get(source) for key, source in {
        'id': 'id', 'name': 'file_name', 'size': 'file_size_bytes', 'sha1': 'sha1_hash',
        'updated_at': 'updated_at', 'category': 'category',
    }.items()}


def _normalize_record(record):
    return {'id': record.get('id'), 'name': record.get('name') or record.get('fs_name') or 'Untitled ROM',
            'platform': record.get('platform_display_name') or record.get('platform_name') or record.get('platform_slug'),
            'platform_id': record.get('platform_id'), 'platform_slug': record.get('platform_slug'),
            'files': [_normalize_file(item) for item in record.get('files', []) if isinstance(item, dict)]}


class RomM:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir)
        self.secrets = self.data_dir / 'secrets' / 'romm.json'
        self.cache = self.data_dir / 'cache' / 'romm'
        self.lock = threading.RLock()
        self.cover_cache = {}

    def _config(self):
        if not self.secrets.exists():
            return {'url': '', 'token': ''}
        if self.secrets.is_symlink() or self.secrets.parent.is_symlink():
            raise Invalid('RomM credentials must not be symlinks')
        try:
            config = json.loads(self.secrets.read_text(encoding='utf-8'))
            _server_url(config['url'])
            return config
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            raise Invalid('RomM configuration could not be read') from None

    def public_config(self):
        config = self._config()
        return {'url': config.get('url', ''), 'configured': bool(config.get('url') and config.get('token')),
                'has_token': bool(config.get('token')), 'required_scopes': ['roms.read'],
                'recommended_scopes': ['roms.read', 'me.read']}

    def configure(self, url, token=None, clear_token=False):
        url = _server_url(url)
        if token is not None and (not isinstance(token, str) or len(token) > 4096 or any(c.isspace() or ord(c) < 32 for c in token)):
            raise Invalid('API token must not contain whitespace and must be at most 4096 characters')
        with self.lock:
            old = self._config()
            if not clear_token and not token and old.get('token') and old.get('url') != url:
                raise Invalid('Enter a new token or clear the existing token when changing RomM servers')
            value = '' if clear_token else (token or old.get('token', ''))
            _atomic_json(self.secrets, {'url': url, 'token': value})
            self.secrets.parent.chmod(0o700)
        return self.public_config()

    def _open(self, path, config=None):
        config = config or self._config()
        if not config.get('url') or not config.get('token'):
            raise Invalid('Connect RomM with a read-only Client API Token in Settings first')
        url = config['url'] + '/api/' + path.lstrip('/')
        if _origin(url) != _origin(config['url']):
            raise Invalid('RomM request must stay on the configured server')
        request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + config['token'],
                                        'User-Agent': 'Portkeeper/0.2', 'Accept': '*/*'})
        opener = urllib.request.build_opener(_OriginRedirect(_origin(url)))
        try:
            return opener.open(request, timeout=60)
        except urllib.error.HTTPError as error:
            error.close()
            if error.code == 401:
                raise Invalid('RomM rejected the API token (401). Check its value and expiry.') from None
            if error.code == 403:
                raise Invalid('RomM denied permission (403). Grant the token roms.read; me.read is optional.') from None
            if error.code == 404:
                raise Invalid('RomM could not find the requested ROM or API endpoint (404).') from None
            raise Invalid(f'RomM request failed with HTTP {error.code}') from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Invalid('RomM could not be reached. Check the server URL and TLS certificate.') from None

    def _json(self, path):
        with self._open(path) as response:
            data = response.read(MAX_JSON + 1)
        if len(data) > MAX_JSON:
            raise Invalid('RomM API response exceeds the size limit')
        try:
            return json.loads(data)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise Invalid('RomM returned an invalid JSON response') from None

    def test_connection(self):
        heartbeat = self._json('heartbeat')
        # Probe roms.read separately: the heartbeat itself is publicly accessible.
        self._json('roms?limit=1&with_char_index=false&with_filter_values=false&with_rom_id_index=false')
        user, warnings = None, []
        try:
            identity = self._json('users/me')
            if isinstance(identity, dict):
                user = {'id': identity.get('id'), 'username': identity.get('username')}
        except Invalid:
            warnings.append('Identity is unavailable; add me.read to show your username.')
        version = heartbeat.get('SYSTEM', {}).get('VERSION') if isinstance(heartbeat, dict) else None
        return {'ok': True, 'url': self.public_config()['url'], 'version': version, 'user': user,
                'scopes': ['roms.read'] + (['me.read'] if user else []),
                'scopes_note': 'These are successfully probed permissions, not every scope on the token.', 'warnings': warnings}

    def search(self, query, offset=0, limit=50):
        if not isinstance(query, str) or len(query) > 200 or any(ord(c) < 32 for c in query):
            raise Invalid('ROM search must be at most 200 characters')
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 0 or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise Invalid('Invalid ROM search pagination')
        params = urllib.parse.urlencode({'search_term': query.strip(), 'offset': offset, 'limit': limit,
                    'with_files': 'true', 'with_char_index': 'false', 'with_filter_values': 'false', 'with_rom_id_index': 'false'})
        result = self._json('roms?' + params)
        items = result.get('items', []) if isinstance(result, dict) else result
        if not isinstance(items, list):
            raise Invalid('RomM returned an invalid ROM list')
        return {'items': [_normalize_record(item) for item in items if isinstance(item, dict)],
                'total': result.get('total', len(items)) if isinstance(result, dict) else len(items), 'offset': offset, 'limit': limit}

    def cover(self, rom_id):
        rom_id = _positive(rom_id)
        config = self._config()
        key = (config.get('url'), rom_id)
        with self.lock:
            cached = self.cover_cache.get(key)
            if cached and time.monotonic() - cached[0] < 60:
                return cached[1], cached[2]
        record = self._json('roms/' + str(rom_id))
        path = record.get('path_cover_large') or record.get('path_cover_small')
        if not path:
            raise Invalid('This RomM entry has no cover yet')
        url = urllib.parse.urljoin(config.get('url', ''), path)
        if _origin(url) != _origin(config.get('url', '')):
            raise Invalid('Cover must be served by the configured RomM server')
        request = urllib.request.Request(url, headers={'Authorization': 'Bearer ' + config.get('token', '')})
        opener = urllib.request.build_opener(_OriginRedirect(_origin(url)))
        try:
            with opener.open(request, timeout=20) as response:
                content_type = response.headers.get_content_type()
                body = response.read(10 * 1024 * 1024 + 1)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Invalid('RomM cover could not be loaded') from None
        if content_type not in ('image/png', 'image/jpeg', 'image/webp') or not body or len(body) > 10 * 1024 * 1024:
            raise Invalid('RomM returned an invalid or oversized cover')
        with self.lock:
            if len(self.cover_cache) >= 100:
                self.cover_cache.clear()
            self.cover_cache[key] = (time.monotonic(), content_type, body)
        return content_type, body

    def detail(self, rom_id):
        result = self._json('roms/' + str(_positive(rom_id)))
        if not isinstance(result, dict):
            raise Invalid('RomM returned an invalid ROM detail')
        return _normalize_record(result)

    def _cache_record(self, key):
        if not isinstance(key, str) or not re.fullmatch('[a-f0-9]{64}', key):
            raise Invalid('Invalid ROM cache key')
        directory = self.cache / key
        if self.cache.is_symlink() or directory.is_symlink():
            raise Invalid('ROM cache must not use symlinks')
        index, archive = directory / 'index.json', directory / 'download.bin'
        if index.is_symlink() or archive.is_symlink():
            raise Invalid('ROM cache must not use symlinks')
        try:
            record = json.loads(index.read_text(encoding='utf-8'))
            if archive.stat().st_size > MAX_ARCHIVE or checksum(archive) != record['archive_sha256']:
                raise Invalid('Cached ROM download failed its checksum; remove it and download again')
        except (OSError, KeyError, TypeError, json.JSONDecodeError):
            raise Invalid('ROM cache is missing or incomplete; inspect the ROM again') from None
        return directory, record

    def inspect_rom(self, rom_id, filename, supported):
        _positive(rom_id)
        if not isinstance(filename, str) or not filename or len(filename) > 1024 or any(ord(c) < 32 for c in filename):
            raise Invalid('Choose a ROM file name')
        if not isinstance(supported, dict) or any(not re.fullmatch('[a-f0-9]{40}', key) for key in supported):
            raise Invalid('Invalid supported ROM hash catalog')
        with self.lock:
            detail = self.detail(rom_id)
            files = [item for item in detail['files'] if item.get('name') == filename]
            if len(files) != 1:
                raise Invalid('Choose a unique file listed in this RomM ROM')
            selected = files[0]
            file_id = _positive(selected.get('id'), 'ROM file id')
            key_data = [self.public_config()['url'], rom_id, file_id, filename, selected.get('sha1'), selected.get('size'), selected.get('updated_at')]
            key = hashlib.sha256(json.dumps(key_data, sort_keys=True).encode()).hexdigest()
            directory = self.cache / key
            index, archive = directory / 'index.json', directory / 'download.bin'
            reused = index.exists()
            if reused:
                self._cache_record(key)
            else:
                if directory.is_symlink() or self.cache.is_symlink():
                    raise Invalid('ROM cache must not use symlinks')
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd, temporary = tempfile.mkstemp(prefix='.download-', dir=directory)
                sha1, sha256, size = hashlib.sha1(), hashlib.sha256(), 0
                try:
                    route = f'roms/{file_id}/files/content/{urllib.parse.quote(filename, safe="")}'
                    with self._open(route) as response, os.fdopen(fd, 'wb') as output:
                        length = response.headers.get('Content-Length')
                        if length and (not length.isdigit() or int(length) > MAX_ARCHIVE):
                            raise Invalid('ROM download exceeds the size limit')
                        for chunk in iter(lambda: response.read(1024 * 1024), b''):
                            size += len(chunk)
                            if size > MAX_ARCHIVE:
                                raise Invalid('ROM download exceeds the size limit')
                            output.write(chunk)
                            sha1.update(chunk)
                            sha256.update(chunk)
                        output.flush()
                        os.fsync(output.fileno())
                    upstream_hash = selected.get('sha1')
                    if upstream_hash and re.fullmatch('[a-fA-F0-9]{40}', upstream_hash) and sha1.hexdigest() != upstream_hash.lower():
                        raise Invalid('Downloaded ROM archive does not match its RomM SHA1')
                    expected_size = selected.get('size')
                    if isinstance(expected_size, int) and expected_size > 0 and size != expected_size:
                        raise Invalid('ROM download size does not match its RomM metadata')
                    os.chmod(temporary, 0o600)
                    os.replace(temporary, archive)
                    _atomic_json(index, {'rom_id': rom_id, 'filename': filename, 'archive_sha256': sha256.hexdigest(),
                                         'size': size, 'candidates': []})
                except (urllib.error.URLError, OSError):
                    raise Invalid('ROM download was interrupted; retry the download') from None
                finally:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    if os.path.exists(temporary):
                        os.unlink(temporary)
            candidates = self._inspect_file(archive, filename, supported)
            _, record = self._cache_record(key)
            record['candidates'] = candidates
            _atomic_json(index, record)
            return {'cache_key': key, 'rom_id': rom_id, 'filename': filename, 'candidates': candidates, 'cache_reused': reused}

    def _inspect_file(self, archive, filename, supported):
        def inspect(data, member):
            try:
                data, order = normalize_rom(data)
                sha1 = hashlib.sha1(data).hexdigest()
                return {'member': member, 'sha1': sha1, 'sha256': hashlib.sha256(data).hexdigest(),
                        'revision': supported.get(sha1), 'supported': sha1 in supported, 'size': len(data), 'byte_order': order}
            except Invalid as error:
                return {'member': member, 'supported': False, 'reason': str(error)}
        try:
            if filename.lower().endswith('.zip'):
                with zipfile.ZipFile(archive) as bundle:
                    entries = _members(bundle)
                    candidates = []
                    for entry in entries:
                        if entry.is_dir() or Path(entry.filename).suffix.lower() not in ROM_EXTENSIONS:
                            continue
                        if entry.file_size > MAX_ROM:
                            candidates.append({'member': entry.filename, 'supported': False, 'reason': 'ROM exceeds 128 MiB'})
                            continue
                        with bundle.open(entry) as stream:
                            candidates.append(inspect(_bounded(stream, MAX_ROM), entry.filename))
                    return candidates
            if Path(filename).suffix.lower() not in ROM_EXTENSIONS:
                raise Invalid('Select a ZIP, .z64, .v64, or .n64 file for this profile')
            with archive.open('rb') as stream:
                return [inspect(_bounded(stream, MAX_ROM), None)]
        except (zipfile.BadZipFile, RuntimeError, EOFError, NotImplementedError):
            raise Invalid('ROM ZIP is corrupt or uses an unsupported compression format') from None

    def selected_input(self, cache_key, member, expected_sha1):
        if not isinstance(expected_sha1, str) or not re.fullmatch('[a-f0-9]{40}', expected_sha1):
            raise Invalid('Choose a verified ROM SHA1')
        with self.lock:
            directory, record = self._cache_record(cache_key)
            candidates = [item for item in record.get('candidates', []) if item.get('member') == member and item.get('sha1') == expected_sha1 and item.get('supported')]
            if len(candidates) != 1:
                raise Invalid('Inspect and select exactly one supported ROM revision first')
            candidate = candidates[0]
            archive = directory / 'download.bin'
            if record['filename'].lower().endswith('.zip'):
                with zipfile.ZipFile(archive) as bundle:
                    _members(bundle)
                    with bundle.open(member) as stream:
                        data, _ = normalize_rom(_bounded(stream, MAX_ROM))
            else:
                with archive.open('rb') as stream:
                    data, _ = normalize_rom(_bounded(stream, MAX_ROM))
            sha1, sha256 = hashlib.sha1(data).hexdigest(), hashlib.sha256(data).hexdigest()
            if sha1 != expected_sha1 or sha256 != candidate['sha256']:
                raise Invalid('Selected ROM no longer matches its verified checksums')
            output = directory / (sha256 + '.z64')
            if output.is_symlink():
                raise Invalid('Normalized ROM cache must not use symlinks')
            if output.exists():
                if checksum(output) != sha256:
                    raise Invalid('Normalized ROM cache failed its checksum; remove it and inspect again')
            else:
                fd, name = tempfile.mkstemp(prefix='.normalized-', dir=directory)
                try:
                    with os.fdopen(fd, 'wb') as stream:
                        stream.write(data)
                        stream.flush()
                        os.fsync(stream.fileno())
                    os.chmod(name, 0o600)
                    os.replace(name, output)
                finally:
                    if os.path.exists(name):
                        os.unlink(name)
            return {**candidate, 'path': str(output.resolve()), 'cache_key': cache_key}
