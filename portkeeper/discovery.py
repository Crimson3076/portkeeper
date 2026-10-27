"""Read-only Recomp Launcher references; never recipes or platform evidence.

Only public HTML is fetched, without credentials, cookies, scripts or remote images.
The reviewed Portkeeper catalog remains independent of this third-party index.
"""
import hashlib
import json
import os
import re
import threading
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

from .safety import Invalid
from .storage import now

SOURCE = 'https://recomplauncher.com/top'
HOSTS = {'recomplauncher.com', 'www.recomplauncher.com'}
MAX_BYTES = 2 * 1024 * 1024
MAX_ENTRIES = 100
REFRESH_SECONDS = 86400
RETRY_SECONDS = 3600
GENERIC = {'github', 'source', 'source code', 'view source', 'view project', 'learn more',
           'read more', 'download', 'downloads', 'website', 'releases', 'details', 'play'}
NAVIGATION = {'top', 'new', 'latest', 'popular', 'search', 'about', 'contact', 'login',
              'register', 'privacy', 'terms', 'discord', 'submit', 'faq', 'donate', 'blog'}


def site_url(value, base=SOURCE):
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
    """Normalize only explicit GitHub repository links, never guess from a title."""
    try:
        url = urllib.parse.urlsplit(value)
        if (url.scheme != 'https' or url.hostname != 'github.com' or url.username or url.password
                or url.port not in (None, 443)):
            return None
        parts = url.path.strip('/').split('/')
        if len(parts) < 2 or not all(re.fullmatch(r'[A-Za-z0-9_.-]+', p) and p not in ('.', '..') for p in parts[:2]):
            return None
        if parts[0].lower() in {'features', 'topics', 'collections', 'marketplace', 'orgs', 'users', 'settings', 'sponsors'}:
            return None
        name = parts[1].removesuffix('.git')
        return parts[0] + '/' + name if name else None
    except (TypeError, ValueError):
        return None


class Node:
    def __init__(self, tag='', attrs=(), parent=None):
        self.tag, self.attrs, self.parent, self.children = tag, dict(attrs), parent, []

    def text(self):
        if self.tag in ('script', 'style', 'template', 'noscript'):
            return ''
        return ' '.join(' '.join(c.text() if isinstance(c, Node) else c for c in self.children).split())[:500]

    def nodes(self):
        yield self
        for child in self.children:
            if isinstance(child, Node):
                yield from child.nodes()

    def excluded(self):
        node = self
        while node:
            if node.tag in ('nav', 'footer', 'script', 'style', 'template', 'noscript'):
                return True
            node = node.parent
        return False


class Page(HTMLParser):
    VOID = {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}

    def __init__(self, html):
        super().__init__(convert_charrefs=True)
        self.root = self.current = Node()
        self.count = self.depth = 0
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.count += 1
        if self.count > 50000 or self.depth > 100:
            raise Invalid('Recomp Launcher page exceeds the HTML complexity limit')
        node = Node(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in self.VOID:
            self.current = node
            self.depth += 1

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        node = self.current
        while node.parent:
            if node.tag == tag:
                self.current = node.parent
                self.depth = sum(1 for _ in self.ancestors(self.current)) - 1
                break
            node = node.parent

    @staticmethod
    def ancestors(node):
        while node:
            yield node
            node = node.parent

    def handle_data(self, text):
        self.current.children.append(text)

    def links(self):
        return [n for n in self.root.nodes() if n.tag == 'a' and not n.excluded() and n.attrs.get('href')]


def card_for(node):
    for ancestor in Page.ancestors(node):
        if ancestor.tag in ('body', 'main', 'section'):
            break
        if ancestor.tag in ('article', 'li') or re.search(r'(?:^|[\s_-])(?:card|game|project|recomp)(?:$|[\s_-])', ancestor.attrs.get('class') or '', re.I):
            return ancestor
    return node


def label_for(node):
    headings = [n.text() for n in node.nodes() if n.tag in ('h1', 'h2', 'h3', 'h4') and n.text()]
    return (headings[0] if headings else node.attrs.get('aria-label') or node.attrs.get('title') or node.text()).strip()[:200]


def project_page(href):
    try:
        url = site_url(href)
    except Invalid:
        return None
    path = urllib.parse.urlsplit(url).path.strip('/')
    if not path or path.split('/')[0].casefold() in NAVIGATION or '.' in path.split('/')[-1]:
        return None
    return url


def parse_index(html):
    page = Page(html)
    result, seen, seen_cards = [], set(), set()
    for link in page.links():
        href = link.attrs['href']
        repo = repository(href)
        card = card_for(link)
        if card is not link and id(card) in seen_cards:
            continue
        name = label_for(card)
        if not name or name.casefold() in GENERIC | NAVIGATION:
            continue
        # Prefer the game's detail page regardless of whether its Source button
        # comes before its title in the HTML. Multiple repository links stay
        # unresolved until the user chooses on that detail page.
        card_links = [n for n in card.nodes() if n.tag == 'a' and not n.excluded()]
        detail = next((url for n in card_links if (url := project_page(n.attrs.get('href')))), None) if card is not link else None
        repos = {repository(n.attrs.get('href')) for n in card_links} - {None}
        if detail:
            url = detail
            repo = next(iter(repos)) if len(repos) == 1 else None
        elif card is not link and len(repos) > 1:
            # No detail page means no reliable way to identify a primary repo.
            seen_cards.add(id(card))
            continue
        elif repo:
            if card is link and label_for(link).casefold() in GENERIC:
                continue
            url = 'https://github.com/' + repo
        else:
            url = project_page(href)
            if not url:
                continue
            path = urllib.parse.urlsplit(url).path.strip('/')
            if card is link and not any(n.tag in ('h2', 'h3', 'h4') for n in link.nodes()) and not re.match(r'^(games?|projects?|ports?|recomps?|recompilations?)/', path):
                continue
            repo = next(iter(repos)) if len(repos) == 1 else None
        key = (repo or url).casefold()
        if key in seen:
            continue
        seen.add(key)
        if card is not link:
            seen_cards.add(id(card))
        result.append({'id': 'recomp-' + hashlib.sha256(key.encode()).hexdigest()[:20],
                       'name': name, 'url': url, 'repository': repo})
        if len(result) >= MAX_ENTRIES:
            break
    if not result:
        raise Invalid('No project links could be read from Recomp Launcher. Browse the website or paste a game page; its layout may have changed.')
    return result


class SiteRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return super().redirect_request(req, fp, code, msg, headers, site_url(newurl, req.full_url))


class Discovery:
    def __init__(self, engine):
        self.engine = engine
        self.root = engine.data / 'catalog'
        self.lock = threading.RLock()
        self.thread = None
        # A separate credential-free client; do not extend the artifact download allowlist.
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), SiteRedirect())

    def _read(self, name):
        path = self.root / name
        try:
            if path.is_symlink() or path.stat().st_size > MAX_BYTES:
                return {}
            value = json.loads(path.read_text(encoding='utf-8'))
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError):
            return {}

    def _write(self, name, value):
        self.root.mkdir(parents=True, exist_ok=True)
        path = self.root / name
        temp = path.with_suffix('.part')
        temp.write_text(json.dumps(value, indent=2), encoding='utf-8')
        os.replace(temp, path)

    def _event(self, kind, **details):
        with self.lock:
            previous = self._read('recomp-launcher-events.json').get('events', [])
            events = previous if isinstance(previous, list) else []
            self._write('recomp-launcher-events.json', {'events': (events + [{'at': now(), 'kind': kind, **details}])[-30:]})

    def close(self):
        if self.thread:
            self.thread.join(timeout=16)

    def fetch(self, url):
        url = site_url(url)
        request = urllib.request.Request(url, headers={'User-Agent': 'Portkeeper/0.1 (+https://github.com/Crimson3076/portkeeper)',
                                                     'Accept': 'text/html'})
        try:
            with self.opener.open(request, timeout=15) as response:
                if response.headers.get_content_type() not in ('text/html', 'application/xhtml+xml'):
                    raise Invalid('Recomp Launcher returned a non-HTML response')
                raw = response.read(MAX_BYTES + 1)
                charset = response.headers.get_content_charset() or 'utf-8'
            if len(raw) > MAX_BYTES:
                raise Invalid('Recomp Launcher page exceeds 2 MiB')
            return raw.decode(charset, errors='replace')
        except (OSError, ValueError, LookupError) as error:
            raise Invalid('Recomp Launcher could not be read. Check connectivity or try again later.') from error

    def status(self):
        with self.lock:
            record = self._read('recomp-launcher.json')
            meta = self._read('recomp-launcher-status.json')
            entries = record.get('entries', [])
            valid = []
            if isinstance(entries, list):
                for entry in entries[:MAX_ENTRIES]:
                    if not isinstance(entry, dict) or not isinstance(entry.get('name'), str) or not isinstance(entry.get('url'), str):
                        continue
                    repo = repository(entry['url']) or repository('https://github.com/' + str(entry.get('repository', '')))
                    try:
                        url = 'https://github.com/' + repo if repository(entry['url']) else site_url(entry['url'])
                    except Invalid:
                        continue
                    valid.append({'id': 'recomp-' + hashlib.sha256((repo or url).casefold().encode()).hexdigest()[:20],
                                  'name': entry['name'][:200], 'url': url, 'repository': repo})
            fetched = record.get('fetched_at')
            age = self._age(fetched)
            return {'source': 'Recomp Launcher', 'url': SOURCE, 'entries': valid,
                    'fetched_at': fetched, 'age_seconds': age, 'stale': age is None or age >= REFRESH_SECONDS,
                    'checked_at': meta.get('checked_at'), 'error': meta.get('error'),
                    'refreshing': bool(self.thread and self.thread.is_alive()),
                    'explanation': 'Discovery references only. Upstream repositories determine platforms, game files and packaging steps.'}

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
            # Persist the attempt before starting: failure retries are bounded across restarts.
            self._write('recomp-launcher-status.json', {'checked_at': now(), 'error': status['error']})
            self.thread = threading.Thread(target=self._refresh, daemon=True, name='portkeeper-discovery')
            self.thread.start()
            return self.status()

    def _refresh(self):
        try:
            entries = parse_index(self.fetch(SOURCE))
            with self.lock:
                self._write('recomp-launcher.json', {'schema': 1, 'fetched_at': now(), 'entries': entries})
                self._write('recomp-launcher-status.json', {'checked_at': now(), 'error': None})
                self._event('refresh_succeeded', entries=len(entries))
        except (Invalid, OSError, ValueError, RecursionError) as error:
            # Do not persist arbitrary response content or exception URLs/query strings.
            with self.lock:
                self._write('recomp-launcher-status.json', {'checked_at': now(),
                    'error': 'Could not refresh Recomp Launcher. The last saved list is still available. Browse the website or paste a game page to continue.'})
                self._event('refresh_failed', reason='unrecognized_html' if isinstance(error, Invalid) and str(error).startswith('No project links') else 'fetch_or_validation_failed')

    def resolve(self, payload):
        if not isinstance(payload, dict):
            raise Invalid('Choose a Recomp Launcher game page')
        url = site_url(payload.get('url'))
        page = Page(self.fetch(url))
        repos, seen = [], set()
        for link in page.links():
            repo = repository(link.attrs['href'])
            if repo and repo.casefold() not in seen:
                seen.add(repo.casefold())
                repos.append({'repository': repo, 'url': 'https://github.com/' + repo})
                if len(repos) >= 20:
                    break
        if not repos:
            raise Invalid('This page has no readable GitHub project link. Open it on Recomp Launcher and copy the upstream GitHub repository instead.')
        self._event('page_resolved', url=url, repositories=[r['repository'] for r in repos])
        return {'url': url, 'repositories': repos,
                'explanation': 'Choose the game engine or port repository. Tools and source decompilations may not produce a playable native game.'}


def main():
    """A disposable, public-data-only probe for local adapter troubleshooting."""
    import argparse
    import tempfile
    from pathlib import Path
    from types import SimpleNamespace
    parser = argparse.ArgumentParser(description='Read Recomp Launcher references without starting Portkeeper or opening its database')
    parser.add_argument('--page', default=SOURCE)
    parser.add_argument('--resolve', action='store_true', help='List explicit upstream repository links instead of index entries')
    args = parser.parse_args()
    try:
        with tempfile.TemporaryDirectory(prefix='portkeeper-discovery-') as temp:
            client = Discovery(SimpleNamespace(data=Path(temp), stop=threading.Event()))
            result = client.resolve({'url': args.page}) if args.resolve else {'url': site_url(args.page), 'entries': parse_index(client.fetch(args.page))}
            print(json.dumps(result, indent=2))
    except (Invalid, OSError, RecursionError) as error:
        parser.exit(1, str(error) + '\n')


if __name__ == '__main__':
    main()
