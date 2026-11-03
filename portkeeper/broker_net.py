"""Authenticated HTTPS link between the Portkeeper app and a remote worker broker.

The broker keeps every safety decision (mounts, network, resources, dependencies).
Over this link the app can only: send the same allow-listed job requests the local
Unix socket accepts, upload a seed tree for a job, and download the resulting
snapshot, log and fetched dependency. Nothing here accepts a host path.

Trust model: the broker generates a self-signed certificate. The app pins its
SHA-256 fingerprint. First contact is protected by a one-time pairing code that
is never sent: both sides prove knowledge of it with an HMAC bound to the
certificate fingerprint, so a man in the middle can neither learn the code nor
impersonate the broker. Afterwards every request carries a random bearer token
(only its hash is stored on the broker) and the fingerprint is verified before
the token is sent.
"""
import argparse
import hashlib
import hmac
import http.client
import json
import os
import re
import secrets
import shutil
import socket
import ssl
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .safety import Invalid, confined
from .worker_broker import ID, MAX_BYTES, MAX_FILES, MAX_LOG, PROTOCOL, tree_identity, unpack_snapshot

DEFAULT_PORT = 8799
MAX_UPLOAD = MAX_BYTES + 64 * 1024**2
MAX_DOWNLOAD = MAX_BYTES + 64 * 1024**2
CODE_ALPHABET = 'ABCDEFGHJKLMNPQRSTUVWXYZ23456789'
CODE_TTL = 15 * 60
MAX_FAILURES = 5
SEED_NAME = re.compile(r'[a-z0-9][a-z0-9-]{0,79}')
SNAPSHOT = re.compile(r'snapshot-[a-f0-9]{32}')
LOGFILE = re.compile(r'[a-f0-9]{32}\.log')
DEPENDENCY = re.compile(r'downloads/[a-zA-Z0-9][a-zA-Z0-9._-]{0,120}')
OPERATIONS = ('health', 'run', 'fetch', 'status', 'cancel', 'release')


class BrokerOffline(Invalid):
    """The broker could not be reached; queued work can wait for it."""


def normalize_code(value):
    return re.sub(r'[^A-Z0-9]', '', str(value).upper())


def proof(code, role, fingerprint):
    return hmac.new(normalize_code(code).encode(), f'portkeeper-pair:{role}:{fingerprint}'.encode(), hashlib.sha256).hexdigest()


def fingerprint_of(certificate_der):
    return hashlib.sha256(certificate_der).hexdigest()


# ---------------------------------------------------------------- broker side

def ensure_certificate(directory):
    """Create (once) a private key and self-signed certificate; return paths and fingerprint."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    key, certificate = directory / 'broker.key', directory / 'broker.pem'
    if not (key.exists() and certificate.exists()):
        names = ['DNS:localhost', 'IP:127.0.0.1']
        hostname = socket.gethostname()
        if re.fullmatch(r'[A-Za-z0-9.-]{1,100}', hostname):
            names.append('DNS:' + hostname)
        old = os.umask(0o077)
        try:
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1', '-nodes',
                            '-keyout', str(key), '-out', str(certificate), '-days', '3650', '-subj', '/CN=portkeeper-broker',
                            '-addext', 'subjectAltName=' + ','.join(names)],
                           check=True, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as error:
            key.unlink(missing_ok=True)
            certificate.unlink(missing_ok=True)
            raise Invalid('Could not create the broker certificate; install openssl: ' + str(error)[-200:]) from None
        finally:
            os.umask(old)
    key.chmod(0o600)
    fingerprint = fingerprint_of(ssl.PEM_cert_to_DER_cert(certificate.read_text()))
    return key, certificate, fingerprint


class AccessControl:
    """One-time pairing code and the (hashed) bearer token of the paired app."""
    def __init__(self, root, fingerprint):
        self.root, self.fingerprint = Path(root), fingerprint
        self.lock = threading.Lock()
        self.pending, self.clients = self.root / 'pairing.json', self.root / 'clients.json'

    def paired(self):
        return self.clients.is_file()

    def _write(self, path, value):
        temporary = path.with_name(path.name + '.tmp')
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, 'w') as stream:
            json.dump(value, stream)
        os.replace(temporary, path)

    def new_code(self, ttl=CODE_TTL):
        raw = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(20))
        with self.lock:
            self._write(self.pending, {'code': raw, 'expires': time.time() + ttl, 'failures': 0})
        return '-'.join(raw[i:i + 5] for i in range(0, 20, 5))

    def redeem(self, client_proof):
        with self.lock:
            try:
                state = json.loads(self.pending.read_text())
            except (OSError, ValueError):
                raise Invalid('No pairing code is active; run the broker pairing command to create one') from None
            if time.time() > state['expires']:
                self.pending.unlink(missing_ok=True)
                raise Invalid('The pairing code expired; create a new one on the broker')
            if not isinstance(client_proof, str) or not hmac.compare_digest(client_proof, proof(state['code'], 'client', self.fingerprint)):
                state['failures'] += 1
                if state['failures'] >= MAX_FAILURES:
                    self.pending.unlink(missing_ok=True)
                else:
                    self._write(self.pending, state)
                raise Invalid('Pairing code was not accepted')
            token = secrets.token_urlsafe(32)
            # One paired app at a time: pairing again revokes the previous token.
            self._write(self.clients, {'token_sha256': hashlib.sha256(token.encode()).hexdigest(), 'paired_at': time.time()})
            self.pending.unlink(missing_ok=True)
            return token, proof(state['code'], 'broker', self.fingerprint)

    def authorized(self, header):
        if not isinstance(header, str) or not header.startswith('Bearer '):
            return False
        try:
            expected = json.loads(self.clients.read_text())['token_sha256']
        except (OSError, ValueError, KeyError):
            return False
        return hmac.compare_digest(hashlib.sha256(header[7:].encode()).hexdigest(), expected)


class NetworkHandler(BaseHTTPRequestHandler):
    timeout = 120
    protocol_version = 'HTTP/1.0'

    def setup(self):
        self.request.settimeout(30)
        self.request.do_handshake()
        super().setup()

    def log_message(self, *args):
        pass

    def reply(self, status, value=None, headers=None):
        body = json.dumps(value if value is not None else {}).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        for key, item in (headers or {}).items():
            self.send_header(key, item)
        self.end_headers()
        self.wfile.write(body)

    def fail(self, status, message):
        self.reply(status, {'error': message})

    def route(self):
        parts = urllib.parse.urlsplit(self.path)
        return parts.path, urllib.parse.parse_qs(parts.query)

    def json_body(self, limit=256 * 1024):
        length = self.headers.get('Content-Length', '')
        if not length.isdigit() or not 0 < int(length) <= limit or self.headers.get('Transfer-Encoding'):
            raise Invalid('Invalid broker request')
        return json.loads(self.rfile.read(int(length)))

    def guard(self):
        """Common checks for authenticated routes. Returns False after replying."""
        if not self.server.access.authorized(self.headers.get('Authorization')):
            self.fail(401, 'This app is not paired with the broker (or its pairing was revoked)')
            return False
        if self.headers.get('X-Portkeeper-Protocol') != str(self.server.protocol):
            self.fail(426, f'Incompatible protocol: the broker speaks version {self.server.protocol}; '
                           f'the app sent {self.headers.get("X-Portkeeper-Protocol")}. Update the older side.')
            return False
        return True

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def do_PUT(self):
        self.dispatch('PUT')

    def dispatch(self, method):
        broker = self.server.broker
        try:
            path, query = self.route()
            if method == 'GET' and path == '/v1/hello':
                with broker.guard:
                    busy = bool(broker.active_names)
                if not busy:
                    # fetch and seed uploads hold the operation lock without being an active run
                    if broker.lock.acquire(blocking=False):
                        broker.lock.release()
                    else:
                        busy = True
                return self.reply(200, {'protocol': self.server.protocol, 'version': __version__, 'ready': broker.ready,
                                        'paired': self.server.access.paired(), 'busy': busy})
            if method == 'POST' and path == '/v1/pair':
                value = self.json_body(4096)
                if value.get('protocol') != self.server.protocol:
                    raise Invalid(f'Incompatible protocol: the broker speaks version {self.server.protocol}')
                token, server_proof = self.server.access.redeem(value.get('proof'))
                return self.reply(200, {'token': token, 'proof': server_proof, 'version': __version__})
            if not self.guard():
                return
            if method == 'POST' and path == '/v1':
                payload = self.json_body()
                if not isinstance(payload, dict) or payload.get('op') not in OPERATIONS:
                    raise Invalid('Unknown broker operation')
                return self.reply(200, {'result': broker.handle(payload)})
            match = re.fullmatch(r'/v1/jobs/([a-f0-9]{32})/(seed|artifacts/(.+))', path)
            if match and method == 'PUT' and match.group(2) == 'seed':
                return self.upload(match.group(1), query)
            if match and method == 'GET' and match.group(3):
                return self.download(match.group(1), match.group(3))
            self.fail(404, 'Unknown broker route')
        except (Invalid, ValueError, KeyError, OSError, subprocess.SubprocessError, tarfile.TarError) as error:
            self.fail(400, str(error)[:500])

    def upload(self, job, query):
        broker = self.server.broker
        name = (query.get('path') or [''])[0]
        length = self.headers.get('Content-Length', '')
        if not SEED_NAME.fullmatch(name) or name == 'downloads':
            raise Invalid('Seed upload needs a simple directory name')
        if not length.isdigit() or int(length) <= 0:
            raise Invalid('Seed upload needs a Content-Length')
        if int(length) > MAX_UPLOAD:
            return self.fail(413, 'Seed upload exceeds the broker size limit')
        if not broker.lock.acquire(blocking=False):
            return self.fail(409, 'Another worker operation is active; wait for it to finish')
        try:
            root = broker.job_root(job)
            if shutil.disk_usage(root).free < 2 * MAX_BYTES + 256 * 1024**2:
                raise Invalid('Broker needs at least 4.25 GiB free to accept a seed')
            archive = root / ('upload-' + secrets.token_hex(8) + '.tar')
            try:
                remaining = int(length)
                with archive.open('wb') as output:
                    while remaining:
                        chunk = self.rfile.read(min(1024**2, remaining))
                        if not chunk:
                            raise Invalid('Seed upload ended early')
                        output.write(chunk)
                        remaining -= len(chunk)
                destination = confined(root, name)
                shutil.rmtree(destination, ignore_errors=True)
                identity = unpack_snapshot(archive, destination)
            finally:
                archive.unlink(missing_ok=True)
        finally:
            broker.lock.release()
        self.reply(200, {'result': {k: v for k, v in identity.items() if k != 'entries'}})

    def download(self, job, name):
        broker = self.server.broker
        root = broker.root / 'jobs' / job
        temporary = None
        if SNAPSHOT.fullmatch(name):
            source = root / name
            if not source.is_dir() or source.is_symlink():
                return self.fail(404, 'No such artifact')
            tree_identity(source)
            handle, temporary = tempfile.mkstemp(dir=root, suffix='.tar')
            os.close(handle)
            with tarfile.open(temporary, 'w', format=tarfile.PAX_FORMAT) as bundle:
                bundle.add(source, arcname='.', recursive=True)
            source, kind = Path(temporary), 'application/x-tar'
        elif LOGFILE.fullmatch(name) or DEPENDENCY.fullmatch(name):
            source, kind = root / name, 'application/octet-stream'
            if not source.is_file() or source.is_symlink() or source.parent.is_symlink():
                return self.fail(404, 'No such artifact')
        else:
            return self.fail(404, 'No such artifact')
        try:
            size = source.stat().st_size
            if size > MAX_DOWNLOAD:
                return self.fail(413, 'Artifact exceeds the broker size limit')
            self.send_response(200)
            self.send_header('Content-Type', kind)
            self.send_header('Content-Length', str(size))
            self.end_headers()
            with source.open('rb') as stream:
                shutil.copyfileobj(stream, self.wfile, 1024**2)
        finally:
            if temporary:
                Path(temporary).unlink(missing_ok=True)


class NetworkServer(ThreadingHTTPServer):
    daemon_threads = True

    def get_request(self):
        client, address = super().get_request()
        # Handshake in the worker thread (see NetworkHandler.setup), not the accept loop.
        return self.context.wrap_socket(client, server_side=True, do_handshake_on_connect=False), address

    def handle_error(self, request, client_address):
        pass


def make_network_server(broker, host, port, tls_directory):
    key, certificate, fingerprint = ensure_certificate(tls_directory)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(certificate, key)
    server = NetworkServer((host, port), NetworkHandler)
    server.context, server.broker, server.protocol = context, broker, PROTOCOL
    server.access = AccessControl(broker.root, fingerprint)
    return server, server.access


# ------------------------------------------------------------------ app side

class PinnedConnection(http.client.HTTPSConnection):
    """TLS to one address, accepting only the certificate with the pinned fingerprint."""
    def __init__(self, host, port, fingerprint, timeout):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname, context.verify_mode = False, ssl.CERT_NONE
        super().__init__(host, port, timeout=timeout, context=context)
        self.pinned, self.seen = fingerprint, None

    def connect(self):
        super().connect()
        self.seen = fingerprint_of(self.sock.getpeercert(binary_form=True))
        if self.pinned is not None and not hmac.compare_digest(self.seen, self.pinned):
            self.close()
            raise Invalid('The broker presented a different certificate than the one paired; '
                          'pair again only if you reinstalled the broker')


def parse_url(url):
    parts = urllib.parse.urlsplit(url if isinstance(url, str) else '')
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.path not in ('', '/') or parts.query or parts.fragment):
        raise Invalid('Use the broker address as https://host:port')
    try:
        port = parts.port or DEFAULT_PORT
    except ValueError:
        raise Invalid('Use the broker address as https://host:port') from None
    return parts.hostname, port


def exchange(connection, method, path, body=None, headers=None):
    try:
        connection.request(method, path, body, headers or {})
        return connection.getresponse()
    except (OSError, http.client.HTTPException) as error:
        raise BrokerOffline('The build broker could not be reached; builds will wait until it is back') from error


def read_json(response, limit=1024**2):
    try:
        value = json.loads(response.read(limit))
    except ValueError:
        raise Invalid('The broker sent an unreadable reply') from None
    if response.status != 200:
        raise Invalid(value.get('error', 'The broker rejected the request') if isinstance(value, dict) else 'The broker rejected the request')
    return value


def pair(url, code):
    """Pair with a broker using its one-time code; returns the config to save."""
    host, port = parse_url(url)
    if len(normalize_code(code)) != 20:
        raise Invalid('Enter the 20-character pairing code printed by the broker')
    connection = PinnedConnection(host, port, None, 15)
    try:
        hello = read_json(exchange(connection, 'GET', '/v1/hello'))
        if hello.get('protocol') != PROTOCOL:
            raise Invalid(f'Incompatible protocol: this app speaks version {PROTOCOL} but the broker speaks '
                          f'{hello.get("protocol")}. Update the older side before pairing.')
        seen = connection.seen
        body = json.dumps({'protocol': PROTOCOL, 'proof': proof(code, 'client', seen)})
        connection.close()
        result = read_json(exchange(connection, 'POST', '/v1/pair', body, {'Content-Type': 'application/json'}))
        if connection.seen != seen:
            raise Invalid('The broker certificate changed during pairing')
        if not hmac.compare_digest(str(result.get('proof')), proof(code, 'broker', seen)) or not isinstance(result.get('token'), str):
            raise Invalid('The broker could not prove it knows the pairing code; pairing was cancelled')
        return {'url': f'https://{host}:{port}', 'fingerprint': seen, 'token': result['token']}
    finally:
        connection.close()


def save_config(path, config):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.tmp')
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, 'w') as stream:
        json.dump({k: config[k] for k in ('url', 'fingerprint', 'token')}, stream)
    os.replace(temporary, path)


def load_config(path):
    try:
        value = json.loads(Path(path).read_text())
        parse_url(value['url'])
        if not all(isinstance(value[k], str) and value[k] for k in ('fingerprint', 'token')):
            raise ValueError
        return value
    except (OSError, ValueError, KeyError, Invalid):
        return None


class RemoteBrokerClient:
    """Same interface as BrokerClient, plus local copies of seeds/snapshots/logs under ``root``."""
    def __init__(self, config_path, root, default_url=None):
        self.config_path, self.root, self.default_url = Path(config_path), Path(root), default_url

    def config(self):
        config = load_config(self.config_path)
        if not config:
            raise Invalid('This app is not paired with a build broker; pair it in Settings')
        return config

    def connection(self, timeout):
        config = self.config()
        host, port = parse_url(config['url'])
        return PinnedConnection(host, port, config['fingerprint'], timeout), {
            'Authorization': 'Bearer ' + config['token'], 'X-Portkeeper-Protocol': str(PROTOCOL)}

    def call(self, method, path, body=None, timeout=30, content_type='application/json', stream=None, limit=None, length=None):
        connection, headers = self.connection(timeout)
        headers['Content-Type'] = content_type
        if length is not None:
            headers['Content-Length'] = str(length)
        try:
            response = exchange(connection, method, path, body, headers)
            if stream is None:
                return read_json(response)
            if response.status != 200:
                read_json(response)
            length = response.getheader('Content-Length')
            if not length or not length.isdigit() or int(length) > limit:
                raise Invalid('Artifact exceeds the size limit')
            total = 0
            with stream.open('wb') as output:
                while True:
                    chunk = response.read(1024**2)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise Invalid('Artifact exceeds the size limit')
                    output.write(chunk)
            if total != int(length):
                raise Invalid('Artifact download was incomplete')
            return total
        except (OSError, http.client.HTTPException) as error:
            raise BrokerOffline('The build broker could not be reached; builds will wait until it is back') from error
        finally:
            connection.close()

    def request(self, op, **payload):
        if op not in OPERATIONS:
            raise Invalid('Unknown broker operation')
        if op in ('run', 'fetch'):
            return getattr(self, op)(payload)
        result = self.call('POST', '/v1', json.dumps({'op': op, **payload}), 5 if op in ('health', 'status') else 60)['result']
        return result

    def job(self, payload):
        job = payload.get('job_id')
        if not isinstance(job, str) or not ID.fullmatch(job):
            raise Invalid('Invalid worker job')
        return job, self.root / 'jobs' / job

    def run(self, payload):
        job, root = self.job(payload)
        name = payload.get('seed', 'seed')
        if not isinstance(name, str) or not SEED_NAME.fullmatch(name):
            raise Invalid('Seed must be a simple directory name')
        seed = confined(root, name)
        if not seed.is_dir():
            raise Invalid('Worker seed is missing')
        identity = tree_identity(seed)
        root.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=root, suffix='.tar') as archive:
            with tarfile.open(fileobj=archive, mode='w', format=tarfile.PAX_FORMAT) as bundle:
                bundle.add(seed, arcname='.', recursive=True)
            archive.flush()
            size = archive.seek(0, os.SEEK_END)
            if size > MAX_UPLOAD:
                raise Invalid('Seed exceeds the upload size limit')
            archive.seek(0)
            uploaded = self.call('PUT', f'/v1/jobs/{job}/seed?path={name}', archive, 1900, 'application/x-tar', length=size)['result']
        if uploaded.get('sha256') != identity['sha256']:
            raise Invalid('The broker stored a different seed than was sent')
        result = self.call('POST', '/v1', json.dumps({'op': 'run', **payload}), 1900)['result']
        log, snapshot = result.get('log'), result.get('snapshot')
        if not (isinstance(log, str) and LOGFILE.fullmatch(log) and isinstance(snapshot, str) and SNAPSHOT.fullmatch(snapshot)):
            raise Invalid('The broker returned an unexpected artifact name')
        self.call('GET', f'/v1/jobs/{job}/artifacts/{log}', timeout=120, stream=root / log, limit=MAX_LOG)
        with tempfile.NamedTemporaryFile(dir=root, suffix='.tar') as archive:
            self.call('GET', f'/v1/jobs/{job}/artifacts/{snapshot}', timeout=1900, stream=Path(archive.name), limit=MAX_DOWNLOAD)
            destination = root / snapshot
            shutil.rmtree(destination, ignore_errors=True)
            received = unpack_snapshot(archive.name, destination)
        if received['sha256'] != result.get('identity', {}).get('sha256'):
            shutil.rmtree(destination, ignore_errors=True)
            raise Invalid('The returned package does not match the broker\'s checksum')
        return result

    def fetch(self, payload):
        job, root = self.job(payload)
        result = self.call('POST', '/v1', json.dumps({'op': 'fetch', **payload}), 1900)['result']
        name = result.get('name')
        if not isinstance(name, str) or not DEPENDENCY.fullmatch('downloads/' + name):
            raise Invalid('The broker returned an unexpected dependency name')
        destination = root / 'downloads' / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        self.call('GET', f'/v1/jobs/{job}/artifacts/downloads/{name}', timeout=1900, stream=destination, limit=MAX_BYTES)
        if hashlib.sha256(destination.read_bytes()).hexdigest() != result.get('sha256'):
            destination.unlink()
            raise Invalid('Downloaded dependency does not match the broker\'s checksum')
        return result

    def release(self, job):
        """Free the broker's copy of a finished job (best effort)."""
        try:
            self.request('release', job_id=job)
        except Invalid:
            pass


def main():
    parser = argparse.ArgumentParser(description='Broker network administration')
    commands = parser.add_subparsers(dest='command', required=True)
    code = commands.add_parser('pair', help='Print a new one-time pairing code (valid 15 minutes)')
    code.add_argument('--root', type=Path, required=True)
    code.add_argument('--tls-dir', type=Path)
    check = commands.add_parser('check', help='Exit 0 when the local broker reports ready with the expected version')
    check.add_argument('--url', required=True)
    check.add_argument('--version')
    check.add_argument('--idle', action='store_true', help='Exit 3 when a build is running')
    args = parser.parse_args()
    if args.command == 'pair':
        _, _, fingerprint = ensure_certificate(args.tls_dir or args.root / 'tls')
        print(AccessControl(args.root, fingerprint).new_code())
        return
    host, port = parse_url(args.url)
    connection = PinnedConnection(host, port, None, 10)
    try:
        hello = read_json(exchange(connection, 'GET', '/v1/hello'))
    except Invalid as error:
        raise SystemExit(str(error))
    finally:
        connection.close()
    if not hello.get('ready') or (args.version and hello.get('version') != args.version) or hello.get('protocol') != PROTOCOL:
        raise SystemExit('Broker is not ready: ' + json.dumps(hello))
    if args.idle and hello.get('busy'):
        print('Broker is busy with a build')
        raise SystemExit(3)
    print(json.dumps(hello))


if __name__ == '__main__':
    main()
