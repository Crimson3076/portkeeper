"""Recomp Launcher's public game catalog: artwork, browsing and metadata hints.

Only the public ``pc-catalog.json`` is fetched, with a plain HTTPS GET and no credentials, cookies
or scripts. Everything in it is untrusted *hints*: platform support, launch commands and
verification status always come from Portkeeper's own checks and reviewed catalog.
"""
import fnmatch
import hashlib
import json
import os
import re
import threading
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

from .safety import Invalid
from .storage import now

SOURCE = 'https://recomplauncher.com/pc-catalog.json'
SITE = 'https://recomplauncher.com'
HOSTS = {'recomplauncher.com', 'www.recomplauncher.com'}
IMAGE_HOSTS = HOSTS | {'recompstore.com', 'www.recompstore.com'}
USER_AGENT = 'Portkeeper/0.1 (+https://github.com/Crimson3076/portkeeper; Recomp catalog reader)'
MAX_BYTES = 8 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_APPS = 5000
REFRESH_SECONDS = 86400
RETRY_SECONDS = 3600
SYSTEMS = ('windows', 'linux', 'macos')
IMAGE_KINDS = ('icon', 'cover', 'hero', 'logo')
IMAGE_TYPES = ('image/png', 'image/jpeg', 'image/webp')
ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,99}')
SHA256 = re.compile(r'[0-9a-fA-F]{64}')
CREDIT = 'Game data and artwork: Recomp Launcher (recomplauncher.com), IGDB and the projects’ authors'
FAILURE = 'Could not refresh the Recomp catalog. The last saved copy is still available.'
# Recomp Launcher consoles → RomM platform slugs; unknown consoles simply never match.
ROMM_PLATFORMS = {'n64': ['n64'], 'gc': ['ngc', 'gamecube'], 'wii': ['wii'], 'wiiu': ['wiiu'], 'snes': ['snes'],
                  'nes': ['nes'], 'gba': ['gba'], 'gb': ['gb'], 'gbc': ['gbc'], 'nds': ['nds'], '3ds': ['3ds'],
                  'ps1': ['psx', 'ps'], 'ps2': ['ps2'], 'ps3': ['ps3'], 'ps4': ['ps4'], 'psp': ['psp'],
                  'xbox': ['xbox'], 'x360': ['xbox360'], 'dc': ['dc', 'dreamcast'], 'saturn': ['saturn'],
                  'genesis': ['genesis', 'genesis-slash-megadrive', 'megadrive'], 'switch': ['switch']}


def site_url(value, base=SITE + '/'):
    """A recomplauncher.com HTTPS URL, normalized; anything else is rejected."""
    if not isinstance(value, str) or not value.strip() or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise Invalid('Use a public Recomp Launcher page URL')
    try:
        url = urllib.parse.urlsplit(urllib.parse.urljoin(base, value))
        if (url.scheme != 'https' or url.hostname not in HOSTS or url.username or url.password
                or url.port not in (None, 443) or '\\' in value):
            raise ValueError()
        path = urllib.parse.unquote(url.path)
        if any(part in ('.', '..') for part in path.split('/')) or path.startswith('//'):
            raise ValueError()
    except ValueError as error:
        raise Invalid('Only HTTPS pages on recomplauncher.com can be read here') from error
    return urllib.parse.urlunsplit(('https', 'recomplauncher.com', url.path or '/', '', ''))


def repository(value):
    """Normalize an explicit GitHub owner/name (or github.com URL); never guess from a title."""
    if not isinstance(value, str) or len(value) > 2048:
        return None
    value = value.strip()
    if value.startswith('https://') or value.startswith('http://'):
        try:
            url = urllib.parse.urlsplit(value)
            if url.scheme != 'https' or url.hostname != 'github.com' or url.username or url.password or url.port not in (None, 443):
                return None
            value = url.path.strip('/')
        except ValueError:
            return None
    parts = value.split('/')
    if len(parts) < 2 or not all(re.fullmatch(r'[A-Za-z0-9_.-]+', p) and p not in ('.', '..') for p in parts[:2]):
        return None
    if parts[0].lower() in {'features', 'topics', 'collections', 'marketplace', 'orgs', 'users', 'settings', 'sponsors'}:
        return None
    name = parts[1].removesuffix('.git')
    repo = parts[0] + '/' + name
    return repo if name and len(repo) <= 200 else None


def _text(value, limit):
    if not isinstance(value, str):
        return None
    value = ''.join(c for c in value if c >= ' ' or c in '\n\t').strip()
    return value[:limit] or None


def _texts(value, limit=20, each=100):
    if not isinstance(value, list):
        return []
    return [t for t in (_text(v, each) for v in value[:limit]) if t]


def _count(value):
    return value if type(value) is int and 0 <= value < 10**12 else 0


def image_url(value):
    """HTTPS artwork on the catalog's own hosts only."""
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 or c == '\\' for c in value):
        return None
    try:
        url = urllib.parse.urlsplit(value)
        if url.scheme != 'https' or url.hostname not in IMAGE_HOSTS or url.username or url.password or url.port not in (None, 443):
            return None
    except ValueError:
        return None
    return value


def validate_app(raw):
    """One catalog entry reduced to the fields Portkeeper uses, or None when malformed."""
    if not isinstance(raw, dict) or not isinstance(raw.get('id'), str) or not ID.fullmatch(raw['id']):
        return None
    name = _text(raw.get('name'), 200)
    if not name:
        return None
    systems = [s for s in SYSTEMS if isinstance(raw.get('systems'), list) and s in raw['systems']]
    assets = {s: t for s, v in raw['assets'].items() if s in SYSTEMS and (t := _text(v, 200))} if isinstance(raw.get('assets'), dict) else {}
    files = {}
    if isinstance(raw.get('files'), dict):
        for system, item in raw['files'].items():
            if system in SYSTEMS and isinstance(item, dict):
                entry = {'name': _text(item.get('name'), 255)}
                if isinstance(item.get('sha256'), str) and SHA256.fullmatch(item['sha256']):
                    entry['sha256'] = item['sha256'].lower()
                if entry['name']:
                    files[system] = entry
    # Saved entries keep their artwork under 'images'; the site lists each kind at the top level.
    pictures = raw['images'] if isinstance(raw.get('images'), dict) else raw
    about = None
    if isinstance(raw.get('about'), dict):
        a = raw['about']
        rating = a.get('rating')
        about = {'name': _text(a.get('name'), 200), 'summary': _text(a.get('summary'), 2000), 'genres': _texts(a.get('genres')),
                 'platforms': _texts(a.get('platforms')), 'developer': _text(a.get('developer'), 200),
                 'publisher': _text(a.get('publisher'), 200), 'series': _text(a.get('series'), 200),
                 'released': _text(str(a['released']) if type(a.get('released')) in (int, str) else None, 40),
                 'rating': round(float(rating), 1) if type(rating) in (int, float) and 0 <= rating <= 100 else None,
                 'igdb_id': a.get('id', a.get('igdb_id')) if type(a.get('id', a.get('igdb_id'))) is int and a.get('id', a.get('igdb_id')) > 0 else None}
    return {'id': raw['id'], 'name': name, 'game': _text(raw.get('game'), 200), 'origin': _text(raw.get('origin'), 40),
            'repo': repository(raw.get('repo')), 'version': _text(raw.get('version'), 100), 'tag': _text(raw.get('tag'), 100),
            'released': _text(raw.get('released'), 40), 'systems': systems, 'assets': assets, 'files': files,
            'needs': _text(raw.get('needs'), 500), 'blurb': _text(raw.get('blurb'), 1500), 'features': _texts(raw.get('features')),
            'tags': _texts(raw.get('tags')), 'genre': _texts(raw.get('genre') if isinstance(raw.get('genre'), list) else [raw.get('genre')], 8),
            'license': _text(raw.get('license'), 100), 'downloads': _count(raw.get('downloads')), 'stars': _count(raw.get('stars')),
            'added': _text(raw.get('added'), 40),
            'images': {k: u for k in IMAGE_KINDS if (u := image_url(pictures.get(k)))}, 'about': about}


def parse_catalog(raw):
    """(apps, skipped) from catalog JSON; the document itself must be well formed."""
    if not isinstance(raw, dict) or not isinstance(raw.get('apps'), list):
        raise Invalid('The Recomp catalog has an unexpected format')
    if type(raw.get('catalogVersion')) is not int or raw['catalogVersion'] != 1:
        raise Invalid('The Recomp catalog version is not supported')
    apps, seen, skipped = [], set(), 0
    for item in raw['apps'][:MAX_APPS]:
        app = validate_app(item)
        if app is None or app['id'] in seen:
            skipped += 1
            continue
        seen.add(app['id'])
        apps.append(app)
    if not apps:
        raise Invalid('The Recomp catalog has no usable games')
    return apps, skipped + max(0, len(raw['apps']) - MAX_APPS)


def release_hint(app, system, release_assets):
    """Which release file the catalog expects for a system, matched against the real release files.

    Only a hint to preselect a file: the match must be unique, and the checksum is an extra comparison."""
    pattern = (app.get('assets') or {}).get(system)
    if not pattern:
        return None
    names = [a for a in release_assets if isinstance(a, str)]
    found = [n for n in names if fnmatch.fnmatchcase(n.lower(), pattern.lower())]
    if len(found) != 1:
        return None
    expected = (app.get('files') or {}).get(system, {})
    return {'system': system, 'asset': found[0], 'sha256': expected.get('sha256') if expected.get('name') == found[0] else None}


def parse_manifest(text):
    """Hints from an upstream ``recomplauncher.json``: start, prepare, folders and saves.

    Untrusted. Values are only suggestions for setup and are never executed by this function."""
    try:
        data = json.loads(text) if isinstance(text, (str, bytes)) and len(text) <= 256 * 1024 else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise Invalid('recomplauncher.json must be a JSON object')

    def safe_path(value):
        value = _text(value, 300)
        if not value or value.startswith(('/', '\\')) or re.match(r'^[A-Za-z]:', value) or '..' in re.split(r'[\\/]', value):
            return None
        return value

    def paths(value):
        if isinstance(value, str):
            value = [value]
        return [p for p in (safe_path(v) for v in (value[:20] if isinstance(value, list) else [])) if p]

    def steps(value):
        value = value if isinstance(value, list) else [value] if value else []
        out = []
        for item in value[:20]:
            item = item.get('command') or item.get('run') if isinstance(item, dict) else item
            item = _text(item, 300)
            if item:
                out.append(item)
        return out

    start = data.get('start')
    start = start.get('command') or start.get('exe') or start.get('path') if isinstance(start, dict) else start
    saves = data.get('saves')
    saves = saves.get('paths') or saves.get('folders') if isinstance(saves, dict) else saves
    result = {'start': safe_path(start), 'prepare': steps(data.get('prepare')),
              'folders': paths(data.get('folders')), 'saves': paths(saves)}
    return {key: value for key, value in result.items() if value}


def read_manifest(github, repo, commit):
    """The upstream repository's optional recomplauncher.json at the release commit, as validated hints (or None)."""
    if not isinstance(commit, str) or not re.fullmatch(r'[a-f0-9]{40}', commit) or not repository(repo):
        return None
    try:
        with github.open(f'https://raw.githubusercontent.com/{repo}/{commit}/recomplauncher.json', 'text/plain') as response:
            body = response.read(256 * 1024 + 1)
        return parse_manifest(body.decode('utf-8')) or None
    except (Invalid, OSError, UnicodeDecodeError, ValueError):
        return None


class SiteRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        if req.get_header('Accept', '').startswith('image/'):
            if not image_url(target):
                raise urllib.error.URLError('Redirect leaves the catalog hosts')
        else:
            site_url(target)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Discovery:
    def __init__(self, engine):
        self.engine = engine
        self.root = engine.data / 'catalog'
        self.lock = threading.RLock()
        self.thread = None
        self._loaded = None
        # Credential-free client, separate from the artifact download allowlist.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), SiteRedirect())

    # Storage ---------------------------------------------------------------
    def _read(self, name):
        path = self.root / name
        try:
            if path.is_symlink() or path.stat().st_size > 2 * MAX_BYTES:
                return {}
            value = json.loads(path.read_text(encoding='utf-8'))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, name, value):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / name
        temp = path.with_suffix('.part')
        temp.write_text(json.dumps(value), encoding='utf-8')
        os.replace(temp, path)

    def close(self):
        if self.thread:
            self.thread.join(timeout=16)

    # Catalog ---------------------------------------------------------------
    def _catalog(self):
        """The last good catalog; entries are re-validated because the file is local but not trusted."""
        with self.lock:
            path = self.root / 'recomp-catalog.json'
            try:
                stamp = path.stat().st_mtime_ns
            except OSError:
                stamp = None
            if self._loaded and self._loaded[0] == stamp:
                return self._loaded[1]
            record = self._read('recomp-catalog.json') if stamp else {}
            apps = [a for a in (validate_app(x) for x in (record.get('apps') if isinstance(record.get('apps'), list) else [])[:MAX_APPS]) if a]
            catalog = {'apps': apps, 'by_id': {a['id']: a for a in apps},
                       'by_repo': {a['repo'].casefold(): a for a in apps if a['repo']},
                       'fetched_at': record.get('fetched_at') if apps else None, 'updated': _text(record.get('updated'), 40) if apps else None,
                       'skipped': _count(record.get('skipped')), 'etag': record.get('etag') if apps else None,
                       'last_modified': record.get('last_modified') if apps else None}
            self._loaded = (stamp, catalog)
            return catalog

    def app(self, app_id):
        app = self._catalog()['by_id'].get(app_id) if isinstance(app_id, str) else None
        if not app:
            raise Invalid('That game is not in the saved Recomp catalog')
        return app

    def for_repository(self, repo):
        repo = repository(repo)
        return self._catalog()['by_repo'].get(repo.casefold()) if repo else None

    def fetch(self, url, conditional=None, accept='application/json', limit=MAX_BYTES):
        """One bounded GET. Returns (status, headers, body); 304 has an empty body."""
        headers = {'User-Agent': USER_AGENT, 'Accept': accept}
        headers.update(conditional or {})
        request = urllib.request.Request(url, headers=headers)
        try:
            with self.opener.open(request, timeout=15) as response:
                if response.headers.get_content_type() not in (('application/json', 'text/json') if accept == 'application/json' else IMAGE_TYPES):
                    raise Invalid('The Recomp catalog server returned an unexpected content type')
                declared = response.headers.get('Content-Length')
                if declared and declared.isdigit() and int(declared) > limit:
                    raise Invalid('The Recomp catalog response is too large')
                body = response.read(limit + 1)
                content_type = response.headers.get_content_type()
                result = (response.status, {'etag': response.headers.get('ETag'), 'last_modified': response.headers.get('Last-Modified'),
                                            'content_type': content_type}, body)
        except urllib.error.HTTPError as error:
            if error.code == 304:
                return 304, {}, b''
            raise Invalid('The Recomp catalog server refused the request') from None
        except (OSError, ValueError, LookupError):
            raise Invalid('The Recomp catalog server could not be reached') from None
        if len(result[2]) > limit:
            raise Invalid('The Recomp catalog response is too large')
        return result

    def status(self):
        with self.lock:
            catalog = self._catalog()
            meta = self._read('recomp-catalog-status.json')
            age = self._age(catalog['fetched_at'])
            error = meta.get('error') if isinstance(meta.get('error'), str) else None
            return {'source': 'Recomp Launcher', 'url': SOURCE, 'count': len(catalog['apps']), 'skipped': catalog['skipped'],
                    'updated': catalog['updated'], 'fetched_at': catalog['fetched_at'], 'age_seconds': age,
                    'stale': age is None or age >= REFRESH_SECONDS, 'checked_at': meta.get('checked_at'), 'error': error,
                    'refreshing': bool(self.thread and self.thread.is_alive()), 'credit': CREDIT,
                    'explanation': 'Reference data only. Upstream repositories and Portkeeper’s own checks decide platforms, game files and packaging.'}

    @staticmethod
    def _age(stamp):
        try:
            return max(0, int((datetime.now(timezone.utc) - datetime.fromisoformat(stamp)).total_seconds()))
        except (ValueError, TypeError):
            return None

    def start_refresh(self, force=False):
        with self.lock:
            status = self.status()
            checked_age = self._age(status['checked_at'])
            if (self.engine.stop.is_set() or status['refreshing'] or
                    checked_age is not None and checked_age < (60 if force else RETRY_SECONDS) or
                    not force and not status['stale']):
                return status
            # Persist the attempt first so failures retry at a bounded rate across restarts.
            self._write('recomp-catalog-status.json', {'checked_at': now(), 'error': status['error']})
            self.thread = threading.Thread(target=self._refresh, daemon=True, name='portkeeper-catalog')
            self.thread.start()
            return self.status()

    def _refresh(self):
        try:
            catalog = self._catalog()
            conditional = {}
            if catalog['etag']:
                conditional['If-None-Match'] = catalog['etag']
            if catalog['last_modified']:
                conditional['If-Modified-Since'] = catalog['last_modified']
            status, headers, body = self.fetch(SOURCE, conditional if catalog['apps'] else None)
            with self.lock:
                if status == 304:
                    record = self._read('recomp-catalog.json')
                    record['fetched_at'] = now()
                    self._write('recomp-catalog.json', record)
                else:
                    try:
                        raw = json.loads(body.decode('utf-8'))
                    except (ValueError, UnicodeDecodeError) as error:
                        raise Invalid('The Recomp catalog is not valid JSON') from error
                    apps, skipped = parse_catalog(raw)
                    self._write('recomp-catalog.json', {'schema': 1, 'fetched_at': now(), 'updated': _text(raw.get('updated'), 40),
                                                        'skipped': skipped, 'etag': _text(headers.get('etag'), 200),
                                                        'last_modified': _text(headers.get('last_modified'), 100), 'apps': apps})
                self._write('recomp-catalog-status.json', {'checked_at': now(), 'error': None})
        except (Invalid, OSError, ValueError, RecursionError):
            # Never persist response content or URLs; the last good catalog keeps working.
            with self.lock:
                self._write('recomp-catalog-status.json', {'checked_at': now(), 'error': FAILURE})

    # Browsing ----------------------------------------------------------------
    def summary(self, app, libraries=None):
        return {'id': app['id'], 'name': app['name'], 'game': app['game'], 'origin': app['origin'], 'repository': app['repo'],
                'systems': app['systems'], 'genre': app['genre'], 'downloads': app['downloads'], 'added': app['added'],
                'blurb': (app['blurb'] or '')[:200], 'needs': app['needs'], 'has_cover': 'cover' in app['images'],
                'in_library': (libraries or {}).get(app['repo'].casefold()) if app['repo'] else None,
                'known': bool(app['repo'] and self._known(app['repo']))}

    def _known(self, repo):
        try:
            return any(d['upstream']['repository'].lower() == repo.lower() for d in self.engine.catalog.definitions().values())
        except (AttributeError, KeyError, TypeError):
            return False

    def browse(self, query):
        """Search, filter and sort the saved catalog. Pure local work; never contacts the site."""
        q = (query.get('q') or '').strip().casefold()[:200]
        origin, system, genre = (query.get(k) or '' for k in ('origin', 'system', 'genre'))
        sort = query.get('sort') or 'popular'
        if sort not in ('popular', 'newest', 'name'):
            raise Invalid('Sort by popular, newest or name')
        try:
            offset, limit = max(0, int(query.get('offset') or 0)), min(200, max(1, int(query.get('limit') or 60)))
        except (TypeError, ValueError):
            raise Invalid('Invalid paging') from None
        catalog = self._catalog()
        libraries = {(p.get('repo') or '').casefold(): p['id'] for p in self.engine.store.all('projects') if p.get('repo')}
        hits = []
        for app in catalog['apps']:
            haystack = ' '.join(filter(None, [app['name'], app['game'], app['repo'], app['origin']] + app['tags'])).casefold()
            if ((q and not all(word in haystack for word in q.split())) or (origin and app['origin'] != origin)
                    or (system and system not in app['systems']) or (genre and genre.casefold() not in [g.casefold() for g in app['genre'] + ((app['about'] or {}).get('genres') or [])])):
                continue
            hits.append(app)
        if sort == 'name':
            hits.sort(key=lambda a: a['name'].casefold())
        elif sort == 'newest':
            hits.sort(key=lambda a: (a['added'] or '', a['name'].casefold()), reverse=True)
        else:
            hits.sort(key=lambda a: (-a['downloads'], a['name'].casefold()))
        genres = sorted({g for a in catalog['apps'] for g in a['genre']}, key=str.casefold)
        return {'total': len(hits), 'items': [self.summary(a, libraries) for a in hits[offset:offset + limit]],
                'origins': sorted({a['origin'] for a in catalog['apps'] if a['origin']}), 'systems': list(SYSTEMS), 'genres': genres,
                'count': len(catalog['apps']), 'credit': CREDIT}

    def detail(self, app_id):
        app = self.app(app_id)
        return {**app, 'url': SITE + '/game/' + urllib.parse.quote(app['id']), 'credit': CREDIT,
                'images': {k: '/api/discovery/image/%s/%s' % (urllib.parse.quote(app['id']), k) for k in app['images']}}

    def for_project(self, project):
        """Compact metadata for a library game that matches a catalog entry, or None."""
        app = self.for_repository(project.get('repo'))
        if not app:
            return None
        slug = (project.get('name') or '').strip()
        looks_like_slug = bool(re.fullmatch(r'[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)?', slug)) and ' ' not in slug and (
            '/' in slug or slug.casefold() in (app['repo'].split('/')[1].casefold(), app['repo'].casefold(), app['id'].casefold()))
        about = app['about'] or {}
        return {'id': app['id'], 'name': app['name'], 'display_name': app['name'] if looks_like_slug else None, 'game': app['game'],
                'origin': app['origin'], 'blurb': app['blurb'], 'features': app['features'], 'tags': app['tags'], 'genre': app['genre'],
                'needs': app['needs'], 'license': app['license'], 'version': app['version'], 'released': app['released'],
                'about': {k: about.get(k) for k in ('summary', 'genres', 'rating', 'developer', 'publisher', 'released', 'series')} if about else None,
                'images': {k: '/api/discovery/image/%s/%s' % (urllib.parse.quote(app['id']), k) for k in app['images']},
                'url': SITE + '/game/' + urllib.parse.quote(app['id']), 'credit': CREDIT}

    def resolve(self, payload):
        """Turn a catalog game id or a Recomp Launcher game-page URL into its upstream repository."""
        if not isinstance(payload, dict):
            raise Invalid('Choose a Recomp Launcher game')
        app_id = payload.get('id')
        if app_id is None:
            url = site_url(payload.get('url'))
            match = re.fullmatch(r'/game/([A-Za-z0-9][A-Za-z0-9_.-]{0,99})/?', urllib.parse.urlsplit(url).path)
            if not match:
                raise Invalid('Paste a game page such as https://recomplauncher.com/game/<id>, or choose the game from the list')
            app_id = match.group(1)
        app = self.app(app_id)
        if not app['repo']:
            raise Invalid('This catalog entry has no GitHub repository. Open it on Recomp Launcher and add the upstream repository as a custom game.')
        return {'url': SITE + '/game/' + urllib.parse.quote(app['id']), 'id': app['id'], 'name': app['name'],
                'repositories': [{'repository': app['repo'], 'url': 'https://github.com/' + app['repo']}],
                'explanation': 'Portkeeper checks the upstream repository itself before suggesting platforms or packaging steps.'}

    # Artwork -------------------------------------------------------------------
    def image(self, app_id, kind):
        """Cached artwork bytes, downloaded on the server so the browser never contacts the site."""
        if kind not in IMAGE_KINDS:
            raise Invalid('Unknown artwork type')
        app = self.app(app_id)
        url = app['images'].get(kind)
        if not url:
            raise Invalid('The catalog has no such artwork for this game')
        folder = self.root / 'images'
        key = hashlib.sha256(url.encode()).hexdigest()[:40]
        data_path, meta_path = folder / (key + '.img'), folder / (key + '.json')
        try:
            if not data_path.is_symlink() and not meta_path.is_symlink():
                content_type = json.loads(meta_path.read_text(encoding='utf-8')).get('content_type')
                if content_type in IMAGE_TYPES and data_path.stat().st_size <= MAX_IMAGE_BYTES:
                    return content_type, data_path.read_bytes()
        except (OSError, ValueError, AttributeError):
            pass
        _, headers, body = self.fetch(url, accept='image/*', limit=MAX_IMAGE_BYTES)
        if not body:
            raise Invalid('The catalog artwork was empty')
        folder.mkdir(parents=True, exist_ok=True)
        with self.lock:
            part = data_path.with_suffix('.part')
            part.write_bytes(body)
            os.replace(part, data_path)
            self._write_image_meta(meta_path, headers['content_type'])
        return headers['content_type'], body

    @staticmethod
    def _write_image_meta(path, content_type):
        temp = path.with_suffix('.part')
        temp.write_text(json.dumps({'content_type': content_type}), encoding='utf-8')
        os.replace(temp, path)

    # RomM ----------------------------------------------------------------------
    def romm_match(self, app_id):
        """Best-effort: does RomM list the original game for this port's console? Never selects a file."""
        app = self.app(app_id)
        title = (app['about'] or {}).get('name') or re.sub(r'^\s*your own copy of\s+', '', app['needs'] or '', flags=re.I).strip(' .') or app['game']
        slugs = ROMM_PLATFORMS.get(app['origin'] or '')
        if not title or not slugs:
            return {'checked': False, 'owned': False, 'title': title}
        from .romm import _title_key
        if not self.engine.romm.public_config().get('configured'):
            return {'checked': False, 'owned': False, 'title': title}
        try:
            found = self.engine.romm.search(title[:200], limit=50)['items']
        except Invalid:
            return {'checked': False, 'owned': False, 'title': title}
        wanted = _title_key(title)
        matches = [{'id': i['id'], 'name': i['name'], 'platform': i['platform']} for i in found
                   if i.get('platform_slug') in slugs and _title_key(i['name']) == wanted]
        return {'checked': True, 'owned': bool(matches), 'title': title, 'matches': matches[:5]}

    def hints(self, repo, release_assets):
        """Release-file hints for a repository, per system, from the saved catalog."""
        app = self.for_repository(repo)
        if not app:
            return None
        found = [h for h in (release_hint(app, s, release_assets) for s in app['systems'] or SYSTEMS) if h]
        return {'catalog_id': app['id'], 'files': found} if found else None


def main():
    """A disposable, public-data-only probe for local troubleshooting."""
    import argparse
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace
    parser = argparse.ArgumentParser(description='Read the public Recomp catalog without starting Portkeeper')
    parser.parse_args()
    try:
        with tempfile.TemporaryDirectory(prefix='portkeeper-catalog-') as temp:
            client = Discovery(SimpleNamespace(data=Path(temp), stop=threading.Event()))
            _, _, body = client.fetch(SOURCE)
            apps, skipped = parse_catalog(json.loads(body.decode('utf-8')))
            print(json.dumps({'games': len(apps), 'skipped': skipped}, indent=2))
    except (Invalid, OSError, ValueError, RecursionError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
