"""Authenticated network link between the Portkeeper app and a remote worker broker.

The broker keeps every safety decision (mounts, network, resources, dependency
fetching). The app can only send the same bounded job requests the local Unix
socket accepts, upload a public seed tree, and download snapshots/logs.

Trust model: the broker serves HTTPS with a self-signed certificate. The app pins
that certificate's SHA-256 fingerprint during pairing and then sends a long random
bearer token. Pairing uses a one-time code printed by the broker. The user must
supply the fingerprint from the trusted broker console before any code proof is
sent, preventing an intermediary from collecting proofs for offline guessing.
"""
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
import tempfile
import tarfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__
from .safety import Invalid, checksum
from .worker_broker import ID, MAX_BYTES, MAX_LOG, tree_identity, unpack_snapshot

PROTOCOL = 2
TREE = re.compile(r'seed|replay-seed|snapshot-[a-f0-9]{32}')
FILE = re.compile(r'[a-f0-9]{32}\.log|downloads/[a-zA-Z0-9][a-zA-Z0-9._-]{0,120}')
MAX_TREE_UPLOAD = MAX_BYTES + 256 * 1024**2
PAIR_LIFETIME = 900
JOB_RETENTION = 7 * 86400


def mismatch_message(remote):
    return (f'Incompatible Portkeeper protocol: this side speaks {PROTOCOL}, the other speaks {remote}. '
            'Update the older side (the broker updates itself from GitHub releases; the app updates from the Docker tab) and try again')


def proof(code, role, fingerprint, nonce):
    return hmac.new(code.encode(), f'{role}|{fingerprint}|{nonce}'.encode(), hashlib.sha256).hexdigest()


def make_certificate(directory):
    """Create a self-signed certificate once; returns (cert, key, sha256 fingerprint)."""
    directory.mkdir(parents=True, exist_ok=True)
    directory.chmod(0o700)
    cert, key = directory / 'cert.pem', directory / 'key.pem'
    if not (cert.exists() and key.exists()):
        names = {socket.gethostname(), 'localhost'}
        try:
            names.add(socket.gethostbyname(socket.gethostname()))
        except OSError:
            pass
        alt = ','.join(('IP:' if re.fullmatch(r'[0-9.]+', n) else 'DNS:') + n for n in sorted(names))
        old = os.umask(0o077)
        try:
            subprocess.run(['openssl', 'req', '-x509', '-newkey', 'ec', '-pkeyopt', 'ec_paramgen_curve:prime256v1',
                            '-nodes', '-days', '3650', '-subj', '/CN=portkeeper-broker', '-addext', 'subjectAltName=' + alt,
                            '-keyout', str(key), '-out', str(cert)], check=True, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as error:
            raise Invalid('Could not create the broker certificate; install the openssl command') from error
        finally:
            os.umask(old)
    key.chmod(0o600)
    return cert, key, fingerprint_of(ssl.PEM_cert_to_DER_cert(cert.read_text()))


def fingerprint_of(der):
    return hashlib.sha256(der).hexdigest()


class LinkState:
    """Broker-side pairing code and paired-token hash (token itself is never stored)."""
    def __init__(self, root, fingerprint):
        self.path = Path(root) / 'link.json'
        self.fingerprint = fingerprint
        self.lock = threading.Lock()
        self.code, self.code_at = None, 0
        try:
            self.token_hash = json.loads(self.path.read_text()).get('token_sha256')
        except (OSError, ValueError):
            self.token_hash = None

    def new_code(self):
        with self.lock:
            digits = ''.join(secrets.choice('0123456789') for _ in range(12))
            self.code, self.code_at = f'{digits[:4]}-{digits[4:8]}-{digits[8:]}', time.monotonic()
            return self.code

    def pair(self, client_proof, nonce):
        with self.lock:
            if not self.code or time.monotonic() - self.code_at > PAIR_LIFETIME:
                raise Invalid('No valid pairing code; restart the broker with --pair to print a new one')
            expected = proof(self.code, 'client', self.fingerprint, nonce)
            if not hmac.compare_digest(expected, client_proof):
                raise Invalid('Pairing code or broker certificate did not match')
            code, self.code = self.code, None
            token = secrets.token_urlsafe(48)
            self.token_hash = hashlib.sha256(token.encode()).hexdigest()
            temporary = self.path.with_suffix('.tmp')
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump({'token_sha256': self.token_hash}, stream)
            os.replace(temporary, self.path)
            return token, proof(code, 'broker', self.fingerprint, nonce)

    def authorized(self, header):
        if not self.token_hash or not header or not header.startswith('Bearer '):
            return False
        return hmac.compare_digest(hashlib.sha256(header[7:].encode()).hexdigest(), self.token_hash)


class NetHandler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    timeout = 30  # per socket operation; also bounds the TLS handshake

    def log_message(self, *args):
        pass

    def reply(self, status, value):
        body = json.dumps(value).encode()
        if status >= 400:
            self.close_connection = True  # the request body may be unread; never parse it as the next request
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        if self.close_connection:
            self.send_header('Connection', 'close')
        self.end_headers()
        self.wfile.write(body)

    def guard(self, public=False):
        remote = self.headers.get('X-Portkeeper-Protocol', '')
        if remote != str(PROTOCOL):
            self.reply(426, {'error': mismatch_message(remote or 'unknown'), 'protocol': PROTOCOL})
            return False
        if not public and not self.server.link.authorized(self.headers.get('Authorization')):
            self.reply(401, {'error': 'Not paired with this broker or the token was rejected; pair again in Settings'})
            return False
        return True

    def body(self, limit):
        if self.headers.get('Transfer-Encoding'):
            raise Invalid('Chunked requests are not accepted')
        try:
            length = int(self.headers.get('Content-Length', ''))
        except ValueError:
            raise Invalid('A Content-Length is required') from None
        if not 0 <= length <= limit:
            raise Invalid('Request exceeds the size limit')
        return length

    def route(self):
        return urllib.parse.urlsplit(self.path).path

    def handle_safely(self, action):
        try:
            action()
        except (Invalid, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
            try:
                self.reply(400, {'error': str(error)[:500]})
                self.close_connection = True
            except OSError:
                pass

    def do_POST(self):
        self.handle_safely(self.post)

    def do_PUT(self):
        self.handle_safely(self.put)

    def do_GET(self):
        self.handle_safely(self.get)

    def post(self):
        path = self.route()
        if path == '/v1/pair':
            if not self.guard(public=True):
                return
            data = json.loads(self.rfile.read(self.body(4096)))
            nonce, given = data.get('nonce'), data.get('proof')
            if not all(isinstance(v, str) and re.fullmatch(r'[a-f0-9]{32,128}', v) for v in (nonce, given)):
                raise Invalid('Invalid pairing request')
            token, broker_proof = self.server.link.pair(given, nonce)
            return self.reply(200, {'token': token, 'proof': broker_proof, 'protocol': PROTOCOL, 'version': __version__})
        if path != '/v1' or not self.guard():
            return
        self.connection.settimeout(15)
        payload = json.loads(self.rfile.read(self.body(256 * 1024)))
        self.connection.settimeout(self.timeout)
        result = self.server.broker.handle(payload)
        if payload.get('op') == 'health':
            result = {**result, 'protocol': PROTOCOL, 'version': __version__}
        self.reply(200, {'result': result})

    def put(self):
        match = re.fullmatch(r'/v1/jobs/([a-f0-9]{32})/trees/([a-z0-9-]+)', self.route())
        if not self.guard():
            return
        if not match or not TREE.fullmatch(match[2]):
            return self.reply(404, {'error': 'Unknown upload'})
        job, name = match[1], match[2]
        length = self.body(MAX_TREE_UPLOAD)
        root = self.server.broker.job_root(job)
        destination = root / name
        # Drain the body even when refusing so the connection stays well-formed.
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=root, prefix='.upload-', delete=False) as archive:
                temporary = Path(archive.name)
                remaining = length
                while remaining:
                    chunk = self.rfile.read(min(1024**2, remaining))
                    if not chunk:
                        raise Invalid('Upload ended early')
                    archive.write(chunk)
                    remaining -= len(chunk)
            if destination.exists() or destination.is_symlink():
                if destination.is_symlink() or not destination.is_dir():
                    raise Invalid('Tree path is unsafe')
                shutil.rmtree(destination)
            identity = unpack_snapshot(temporary, destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        expected = self.headers.get('X-Tree-Sha256')
        if expected and not hmac.compare_digest(expected, identity['sha256']):
            shutil.rmtree(destination, ignore_errors=True)
            raise Invalid('Uploaded tree did not match its identity')
        self.reply(200, {'sha256': identity['sha256']})

    def get(self):
        path = self.route()
        if path == '/v1/health':
            # Unauthenticated, minimal: lets the app distinguish "wrong version" from "offline".
            if self.guard(public=True):
                self.reply(200, {'protocol': PROTOCOL, 'version': __version__, 'paired': bool(self.server.link.token_hash),
                                  'ready': bool(self.server.broker.ready)})
            return
        match = re.fullmatch(r'/v1/jobs/([a-f0-9]{32})/(trees|identity|files)/(.+)', path)
        if not self.guard():
            return
        if not match:
            return self.reply(404, {'error': 'Unknown artifact'})
        job, kind, name = match.groups()
        root = self.server.broker.job_root(job)
        if kind in ('trees', 'identity'):
            if not TREE.fullmatch(name):
                raise Invalid('Unknown tree')
            tree = root / name
            if tree.is_symlink() or not tree.is_dir():
                return self.reply(404, {'error': 'Tree is not present'})
            if kind == 'identity':
                return self.reply(200, {'sha256': tree_identity(tree)['sha256']})
            return self.send_archive(tree)
        if not FILE.fullmatch(name):
            raise Invalid('Unknown file')
        target = root / name
        if target.is_symlink() or not target.is_file():
            return self.reply(404, {'error': 'File is not present'})
        limit = MAX_BYTES if name.startswith('downloads/') else MAX_LOG
        size = target.stat().st_size
        if size > limit:
            raise Invalid('Artifact exceeds its size limit')
        self.send_file(target, size)

    def send_file(self, path, size):
        self.send_response(200)
        self.send_header('Content-Type', 'application/octet-stream')
        self.send_header('Content-Length', str(size))
        self.end_headers()
        with path.open('rb') as source:
            shutil.copyfileobj(source, self.wfile, 1024**2)

    def send_archive(self, tree):
        with tempfile.NamedTemporaryFile(dir=tree.parent, prefix='.download-') as archive:
            with tarfile.open(fileobj=archive, mode='w', format=tarfile.PAX_FORMAT) as bundle:
                bundle.add(tree, arcname='.', recursive=True)
            archive.flush()
            size = Path(archive.name).stat().st_size
            if size > MAX_TREE_UPLOAD:
                raise Invalid('Snapshot exceeds its size limit')
            archive.seek(0)
            self.send_response(200)
            self.send_header('Content-Type', 'application/x-tar')
            self.send_header('Content-Length', str(size))
            self.send_header('X-Tree-Sha256', tree_identity(tree)['sha256'])
            self.end_headers()
            shutil.copyfileobj(archive, self.wfile, 1024**2)


class NetServer(ThreadingHTTPServer):
    daemon_threads = True
    max_connections = 32

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.slots = threading.BoundedSemaphore(self.max_connections)

    def process_request(self, request, client_address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)  # over the connection cap
            return
        super().process_request(request, client_address)

    def process_request_thread(self, request, client_address):
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.slots.release()

    def handle_error(self, request, client_address):
        pass  # failed handshakes (scanners, wrong pins) are expected noise


def prune_jobs(root, now=None):
    jobs = Path(root) / 'jobs'
    if not jobs.is_dir():
        return
    cutoff = (now or time.time()) - JOB_RETENTION
    for child in jobs.iterdir():
        if child.is_dir() and not child.is_symlink() and child.stat().st_mtime < cutoff:
            shutil.rmtree(child, ignore_errors=True)


def prune_periodically(root):
    while True:
        time.sleep(3600)
        prune_jobs(root)


def make_net_server(broker, host, port, state_root):
    cert, key, fingerprint = make_certificate(Path(state_root) / 'tls')
    server = NetServer((host, port), NetHandler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(cert, key)
    # Handshake in the per-connection thread (bounded by the handler timeout), not the accept loop.
    server.socket = context.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    server.broker = broker
    server.fingerprint = fingerprint
    server.link = LinkState(state_root, fingerprint)
    return server


class PinnedTLS(http.client.HTTPSConnection):
    """TLS that checks the supplied certificate fingerprint before sending HTTP data."""
    def __init__(self, host, port, fingerprint, timeout):
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = False  # NOSONAR: self-signed identity is checked against the out-of-band SHA-256 pin in connect().
        context.verify_mode = ssl.CERT_NONE  # NOSONAR: connect() checks the certificate pin before any HTTP request.
        super().__init__(host, port, timeout=timeout, context=context)
        self.pin, self.seen = fingerprint, None

    def connect(self):
        super().connect()
        self.seen = fingerprint_of(self.sock.getpeercert(binary_form=True))
        if self.pin and not hmac.compare_digest(self.pin, self.seen):
            self.close()
            raise Invalid('The broker certificate does not match the paired fingerprint; pair again if you replaced the broker')


def parse_url(url):
    parts = urllib.parse.urlsplit(url if isinstance(url, str) else '')
    if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
            or parts.path not in ('', '/') or parts.query or parts.fragment or len(url) > 300):
        raise Invalid('Use an https://host:port address for the broker')
    return parts.hostname, parts.port or 8788


def pair(url, code, fingerprint):
    """Pair with a broker; returns the link record to persist."""
    host, port = parse_url(url)
    if not isinstance(code, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{4}-[0-9]{4}', code.strip()):
        raise Invalid('Enter the pairing code printed by the broker (e.g. 1234-5678-9012)')
    code = code.strip()
    if not isinstance(fingerprint, str) or not re.fullmatch(r'[a-f0-9]{64}', fingerprint):
        raise Invalid('Enter the full SHA-256 fingerprint printed in the broker VM console')
    connection = PinnedTLS(host, port, fingerprint, 10)
    nonce = secrets.token_hex(16)
    try:
        connection.connect()
        fingerprint = connection.seen
        connection.request('POST', '/v1/pair', json.dumps({'nonce': nonce, 'proof': proof(code, 'client', fingerprint, nonce)}),
                           {'Content-Type': 'application/json', 'X-Portkeeper-Protocol': str(PROTOCOL)})
        response = connection.getresponse()
        value = json.loads(response.read(65536))
    except Invalid:
        raise
    except (OSError, ValueError, http.client.HTTPException):
        raise Invalid('Could not reach the broker to pair; check its address, port and firewall') from None
    finally:
        connection.close()
    if response.status == 426:
        raise Invalid(value.get('error', 'Incompatible protocol'))
    if response.status != 200:
        raise Invalid(value.get('error', 'Pairing was rejected'))
    if not hmac.compare_digest(value.get('proof', ''), proof(code, 'broker', fingerprint, nonce)) or not isinstance(value.get('token'), str):
        raise Invalid('The broker could not prove it knows the pairing code; do not trust this connection')
    return {'url': f'https://{"[" + host + "]" if ":" in host else host}:{port}', 'fingerprint': fingerprint, 'token': value['token']}


class RemoteBrokerClient:
    """Same interface as BrokerClient, but the exchange directory is local to the app."""
    offline_errors = ('could not be reached',)

    def __init__(self, link, root):
        self.link, self.root = link, Path(root)
        self.host, self.port = parse_url(link['url'])

    def connection(self, timeout):
        return PinnedTLS(self.host, self.port, self.link['fingerprint'], timeout)

    def headers(self, extra=None):
        return {'Authorization': 'Bearer ' + self.link['token'], 'X-Portkeeper-Protocol': str(PROTOCOL), **(extra or {})}

    def call(self, method, path, body=None, headers=None, timeout=30):
        connection = self.connection(timeout)
        try:
            connection.request(method, path, body, self.headers(headers))
            return connection, connection.getresponse()
        except Invalid:
            connection.close()
            raise
        except (OSError, http.client.HTTPException):
            connection.close()
            raise Invalid('Worker broker could not be reached; check the broker URL, network and service') from None

    def check(self, response, data=None):
        if response.status == 200:
            return
        try:
            message = json.loads(data if data is not None else response.read(65536)).get('error')
        except ValueError:
            message = None
        raise Invalid(message or f'Worker broker rejected the request (HTTP {response.status})')

    def json_call(self, method, path, payload=None, timeout=30):
        connection, response = self.call(method, path, None if payload is None else json.dumps(payload),
                                         {'Content-Type': 'application/json'}, timeout)
        try:
            data = response.read(1024**2)
        except (OSError, http.client.HTTPException):
            raise Invalid('Worker broker could not be reached; connection dropped') from None
        finally:
            connection.close()
        self.check(response, data)
        return json.loads(data)

    def download(self, path, destination, limit):
        connection, response = self.call('GET', path, timeout=300)
        try:
            self.check(response)
            length = response.getheader('Content-Length')
            if not length or not length.isdigit() or int(length) > limit:
                raise Invalid('Broker artifact is missing a size or exceeds its limit')
            total = 0
            with open(destination, 'wb') as output:
                while True:
                    chunk = response.read(1024**2)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > limit:
                        raise Invalid('Broker artifact exceeds its size limit')
                    output.write(chunk)
            if total != int(length):
                raise Invalid('Broker artifact was truncated')
            return response.getheader('X-Tree-Sha256')
        except (OSError, http.client.HTTPException):
            raise Invalid('Worker broker could not be reached; download interrupted') from None
        finally:
            connection.close()

    def upload_seed(self, job, name):
        tree = self.root / 'jobs' / job / name
        if not TREE.fullmatch(name) or tree.is_symlink() or not tree.is_dir():
            raise Invalid('Worker seed is missing')
        identity = tree_identity(tree)['sha256']
        try:
            if self.json_call('GET', f'/v1/jobs/{job}/identity/{name}')['sha256'] == identity:
                return
        except Invalid as error:
            if 'not present' not in str(error):
                raise
        with tempfile.NamedTemporaryFile(prefix='portkeeper-seed-') as archive:
            with tarfile.open(fileobj=archive, mode='w', format=tarfile.PAX_FORMAT) as bundle:
                bundle.add(tree, arcname='.', recursive=True)
            archive.flush()
            size = Path(archive.name).stat().st_size
            if size > MAX_TREE_UPLOAD:
                raise Invalid('Seed exceeds the worker storage limit')
            archive.seek(0)
            connection, response = self.call('PUT', f'/v1/jobs/{job}/trees/{name}', archive,
                                             {'Content-Length': str(size), 'X-Tree-Sha256': identity,
                                              'Content-Type': 'application/x-tar'}, timeout=900)
            try:
                self.check(response)
            finally:
                connection.close()

    def request(self, op, **payload):
        timeout = 5 if op in ('health', 'status') else 1900
        if op == 'run':
            self.upload_seed(payload['job_id'], payload.get('seed', 'seed'))
        result = self.json_call('POST', '/v1', {'op': op, **payload}, timeout)['result']
        if op == 'health':
            if result.get('protocol') != PROTOCOL:
                raise Invalid(mismatch_message(result.get('protocol')))
            result = {**result, 'link': {'url': self.link['url'], 'fingerprint': self.link['fingerprint'][:16]}}
        elif op == 'run':
            self.pull_run(payload['job_id'], result)
        elif op == 'fetch':
            self.pull_download(payload['job_id'], result)
        return result

    def pull_run(self, job, result):
        local = self.root / 'jobs' / job
        local.mkdir(parents=True, exist_ok=True)
        snapshot, log = result.get('snapshot'), result.get('log')
        if not isinstance(snapshot, str) or not TREE.fullmatch(snapshot) or not isinstance(log, str) or not FILE.fullmatch(log):
            raise Invalid('Broker returned an unexpected artifact name')
        with tempfile.NamedTemporaryFile(dir=local, prefix='.snapshot-', delete=False) as archive:
            temporary = Path(archive.name)
        try:
            self.download(f'/v1/jobs/{job}/trees/{snapshot}', temporary, MAX_TREE_UPLOAD)
            shutil.rmtree(local / snapshot, ignore_errors=True)
            identity = unpack_snapshot(temporary, local / snapshot)
        finally:
            temporary.unlink(missing_ok=True)
        if identity['sha256'] != result.get('identity', {}).get('sha256'):
            shutil.rmtree(local / snapshot, ignore_errors=True)
            raise Invalid('Downloaded snapshot did not match the broker’s identity')
        self.download(f'/v1/jobs/{job}/files/{log}', local / log, MAX_LOG)

    def pull_download(self, job, result):
        name = result.get('name')
        if not isinstance(name, str) or not FILE.fullmatch('downloads/' + name):
            raise Invalid('Broker returned an unexpected dependency name')
        destination = self.root / 'jobs' / job / 'downloads'
        destination.mkdir(parents=True, exist_ok=True)
        target = destination / name
        self.download(f'/v1/jobs/{job}/files/downloads/{name}', target, MAX_BYTES)
        if checksum(target) != result.get('sha256'):
            target.unlink(missing_ok=True)
            raise Invalid('Downloaded dependency did not match the broker’s checksum')


def is_offline(error):
    return 'could not be reached' in str(error)
