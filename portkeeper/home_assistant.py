"""Optional Home Assistant notifications with a server-side long-lived token."""
import json
import threading
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

from .romm import _atomic_json
from .safety import Invalid
from .storage import identifier, now


def server_url(value):
    if not isinstance(value, str) or len(value) > 2048 or any(ord(c) < 32 for c in value):
        raise Invalid('Enter a Home Assistant server URL')
    try:
        parts = urllib.parse.urlsplit(value.strip())
        parts.port
        path = urllib.parse.unquote(parts.path)
        if (any(c.isspace() for c in value.strip()) or parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password
                or parts.query or parts.fragment or '\\' in value or '\\' in path
                or any(p in ('.', '..') for p in path.split('/'))):
            raise ValueError()
    except ValueError:
        raise Invalid('Use an HTTP or HTTPS Home Assistant URL without credentials, query parameters or a fragment') from None
    return value.strip().rstrip('/')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise Invalid('Home Assistant redirected the request. Use its direct server URL; the token was not forwarded.')


class HomeAssistant:
    def __init__(self, engine):
        self.engine = engine
        self.secrets = engine.data / 'secrets' / 'home-assistant.json'
        self.lock = threading.RLock()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='portkeeper-ha')
        self.slots = threading.BoundedSemaphore(32)
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def _config(self):
        if not self.secrets.exists():
            return {'url': '', 'token': '', 'enabled': False, 'generation': ''}
        if self.secrets.is_symlink() or self.secrets.parent.is_symlink():
            raise Invalid('Home Assistant credentials must not be symlinks')
        try:
            if self.secrets.stat().st_size > 16384:
                raise ValueError()
            config = json.loads(self.secrets.read_text(encoding='utf-8'))
            if not isinstance(config, dict) or not isinstance(config.get('url'), str):
                raise ValueError()
            if config.get('url'):
                server_url(config['url'])
            if not isinstance(config.get('token'), str) or not isinstance(config.get('enabled'), bool):
                raise ValueError()
            if len(config['token']) > 4096 or any(not 33 <= ord(c) <= 126 for c in config['token']):
                raise ValueError()
            config.setdefault('generation', '')
            if not isinstance(config['generation'], str) or len(config['generation']) > 64:
                raise ValueError()
            return config
        except (OSError, ValueError, TypeError, AttributeError):
            raise Invalid('Home Assistant configuration could not be read') from None

    def public_config(self):
        with self.lock:
            config = self._config()
            health = self.engine.get_setting('home_assistant_health', {})
            if health.get('generation') != config.get('generation'):
                health = {}
            return {'url': config['url'], 'has_token': bool(config['token']),
                    'configured': bool(config['url'] and config['token']), 'enabled': config['enabled'],
                    'connected': health.get('connected', False), 'checked_at': health.get('checked_at'),
                    'last_error': health.get('last_error'), 'last_sent_at': health.get('last_sent_at')}

    def configure(self, payload):
        if not isinstance(payload, dict) or set(payload) - {'url', 'token', 'clear_token', 'enabled'}:
            raise Invalid('Home Assistant settings accept URL, token and notification controls')
        with self.lock:
            old = self._config()
            url = payload.get('url', old['url'])
            if not isinstance(url, str):
                raise Invalid('Enter a Home Assistant server URL')
            url = server_url(url) if url else ''
            token = payload.get('token')
            if token is not None and (not isinstance(token, str) or len(token) > 4096):
                raise Invalid('The long-lived access token must be text of at most 4096 characters')
            token = token.strip() if token else ''
            if any(c.isspace() or not 33 <= ord(c) <= 126 for c in token):
                raise Invalid('The long-lived access token must use printable characters without whitespace')
            clear = payload.get('clear_token', False)
            enabled = payload.get('enabled', old['enabled'])
            if not isinstance(clear, bool) or not isinstance(enabled, bool):
                raise Invalid('Notification controls must be true or false')
            if not clear and not token and old['token'] and url != old['url']:
                raise Invalid('Enter a new token or remove the saved token when changing Home Assistant servers')
            token = '' if clear else token or old['token']
            enabled = False if clear else enabled
            if enabled and not (url and token):
                raise Invalid('Save your Home Assistant server URL and token before enabling notifications')
            config = {'url': url, 'token': token, 'enabled': enabled, 'generation': identifier()}
            _atomic_json(self.secrets, config)
            self.secrets.parent.chmod(0o700)
            self.engine.store.put('settings', {'id': 'home_assistant_health', 'value': {}})
            return self.public_config()

    def _request(self, config, path='', payload=None):
        if not config['url'] or not config['token']:
            raise Invalid('Save a Home Assistant server URL and long-lived access token in Notifications first')
        request = urllib.request.Request(config['url'] + '/api/' + path,
            data=json.dumps(payload).encode() if payload is not None else None,
            headers={'Authorization': 'Bearer ' + config['token'], 'Accept': 'application/json',
                     'Content-Type': 'application/json', 'User-Agent': 'Portkeeper/0.1'})
        try:
            with self.opener.open(request, timeout=8) as response:
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise Invalid('Home Assistant response exceeded its size limit')
                return json.loads(raw)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if code in (401, 403):
                raise Invalid('Home Assistant rejected the token. Check that it is a valid long-lived access token.') from None
            raise Invalid('Home Assistant request failed (HTTP ' + str(code) + ')') from None
        except (OSError, ValueError) as error:
            if isinstance(error, Invalid):
                raise
            raise Invalid('Could not read the Home Assistant response. Check the server URL, connection and certificate.') from None

    def _health(self, config, **changes):
        with self.lock:
            if self._config().get('generation') != config.get('generation'):
                return
            health = self.engine.get_setting('home_assistant_health', {})
            if health.get('generation') != config.get('generation'):
                health = {}
            self.engine.store.put('settings', {'id': 'home_assistant_health', 'value': {**health, **changes,
                                    'generation': config.get('generation'), 'checked_at': now()}})

    def test(self):
        config = self._config()
        try:
            result = self._request(config)
            if not isinstance(result, dict) or result.get('message') != 'API running.':
                raise Invalid('This URL did not return the Home Assistant REST API. Check the server address.')
            self._health(config, connected=True, last_error=None)
            return {'ok': True, 'message': 'Home Assistant connection verified.'}
        except Invalid as error:
            self._health(config, connected=False, last_error=str(error))
            raise

    def _send(self, config, title, message, event_id):
        self._request(config, 'services/persistent_notification/create',
                      {'title': title[:200], 'message': message[:1200], 'notification_id': 'portkeeper_' + event_id})
        self._health(config, connected=True, last_error=None, last_sent_at=now())

    def send_test(self):
        config = self._config()
        try:
            self._send(config, 'Portkeeper', 'Home Assistant notifications are connected.', 'test')
            return {'ok': True, 'message': 'Test notification sent to Home Assistant.'}
        except Invalid as error:
            self._health(config, connected=False, last_error=str(error))
            raise

    def enqueue(self, notice):
        """Network failures cannot turn a successful build into a failed job."""
        if self.engine.stop.is_set():
            return
        try:
            config = self._config()
            if not config['enabled'] or not config['url'] or not config['token']:
                return
            if not self.slots.acquire(blocking=False):
                self._health(config, last_error='Home Assistant queue is full. The update remains in your Portkeeper inbox.')
                return
            try:
                task = self.executor.submit(self._deliver, dict(notice), config.get('generation'))
                task.add_done_callback(lambda _: self.slots.release())
            except RuntimeError:
                self.slots.release()
        except (Invalid, OSError):
            # The local inbox is already persisted and remains the source of truth.
            return

    def _deliver(self, notice, generation):
        config = self._config()
        if self.engine.stop.is_set() or not config['enabled'] or config.get('generation') != generation:
            return
        try:
            self._send(config, 'Portkeeper · ' + notice['project_name'], notice['message'], notice['id'])
        except Invalid as error:
            self._health(config, connected=False, last_error=str(error))

    def close(self):
        self.executor.shutdown(wait=True, cancel_futures=True)
