"""Narrow Unix-socket broker for offline, disposable script workers.

Run this service beside a dedicated rootless Docker daemon, outside the web app.
The model never receives daemon access or controls container flags/host mounts.
All writable worker storage is tmpfs; no writable host directory is mounted.
"""
import argparse
import hashlib
import http.client
import ipaddress
import json
import os
import re
import selectors
import shutil
import signal
import socket
import socketserver
import ssl
import stat
import subprocess
import tarfile
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from .safety import Invalid, checksum, confined, relative
from .storage import Store, now

MAX_BYTES = 2 * 1024**3
MAX_FILES = 30000
MAX_LOG = 1024**2
ID = re.compile(r'[a-f0-9]{32}')
IMAGE = re.compile(r'(?:[a-zA-Z0-9./:_-]+@)?sha256:[a-f0-9]{64}')


def tree_identity(root):
    """Bounded identity, rejecting devices and escaping links, including empty dirs."""
    root = Path(root).resolve()
    entries, total = [], 0
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in sorted(dirs + files):
            path = Path(directory) / name
            rel = path.relative_to(root).as_posix()
            relative(rel)
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                target = os.readlink(path)
                try:
                    resolved = path.resolve()
                except (RuntimeError, OSError):
                    raise Invalid('Workspace contains an unsafe or cyclic link') from None
                if Path(target).is_absolute() or not resolved.is_relative_to(root):
                    raise Invalid('Workspace contains a link outside its snapshot')
                entries.append([rel, 'link', target])
            elif stat.S_ISDIR(mode):
                entries.append([rel, 'directory'])
            elif stat.S_ISREG(mode):
                size = path.stat().st_size
                total += size
                if total > MAX_BYTES:
                    raise Invalid('Workspace exceeds the 2 GiB worker storage limit')
                entries.append([rel, 'file', size, bool(mode & 0o111), checksum(path)])
            else:
                raise Invalid('Workspace contains a device, socket or special file')
            if len(entries) > MAX_FILES:
                raise Invalid('Workspace exceeds the file limit')
    encoded = json.dumps(sorted(entries), separators=(',', ':')).encode()
    return {'sha256': hashlib.sha256(encoded).hexdigest(), 'bytes': total, 'files': len(entries), 'entries': sorted(entries)}


def unpack_snapshot(archive, destination):
    """Extract untrusted Docker output without following links or trusting headers."""
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=False)
    seen, links, total = set(), [], 0
    try:
        with tarfile.open(archive, 'r|') as bundle:
            for member in bundle:
                name = member.name
                if name in ('.', './'):
                    continue
                name = name.removeprefix('./').rstrip('/')
                path = confined(destination, name)
                if name in seen or len(seen) >= MAX_FILES:
                    raise Invalid('Duplicate path or too many workspace files')
                seen.add(name)
                total += member.size
                if member.size < 0 or total > MAX_BYTES:
                    raise Invalid('Workspace exceeds the 2 GiB worker storage limit')
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                elif member.isfile():
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with bundle.extractfile(member) as source, path.open('xb') as output:
                        shutil.copyfileobj(source, output)
                    path.chmod(0o755 if member.mode & 0o111 else 0o644)
                elif member.issym():
                    links.append((name, member.linkname))
                else:
                    raise Invalid('Workspace contains a hard link or special file')
        # Links are created last, so a later archive entry cannot write through one.
        for name, target in links:
            path = confined(destination, name)
            if Path(target).is_absolute() or not (path.parent / target).resolve().is_relative_to(destination.resolve()):
                raise Invalid('Workspace contains a link outside its snapshot')
            path.parent.mkdir(parents=True, exist_ok=True)
            path.symlink_to(target)
        for directory, dirs, _ in os.walk(destination, followlinks=False):
            Path(directory).chmod(0o755)
        return tree_identity(destination)
    except Exception:
        shutil.rmtree(destination, ignore_errors=True)
        raise


class PinnedHTTPS(http.client.HTTPSConnection):
    """Connect to an already checked address while preserving TLS hostname checks."""
    def __init__(self, hostname, address):
        super().__init__(hostname, timeout=30, context=ssl.create_default_context())
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except Exception:
            sock.close()
            raise


def fetch_public(url, destination, expected=None, cancelled=lambda: False):
    """HTTPS only, pinned DNS per hop, no proxies/cookies/credentials/private egress."""
    total, started = 0, time.monotonic()
    destination = Path(destination)
    temporary = destination.with_suffix('.part')
    original = url
    try:
        for _ in range(6):
            parts = urllib.parse.urlsplit(url)
            if (parts.scheme != 'https' or not parts.hostname or parts.port not in (None, 443)
                    or parts.username or parts.password or any(ord(c) < 32 for c in url)):
                raise Invalid('Dependencies require public HTTPS URLs without credentials')
            addresses = {entry[4][0] for entry in socket.getaddrinfo(parts.hostname, 443, type=socket.SOCK_STREAM)}
            def public_address(value):
                address = ipaddress.ip_address(value)
                embedded = getattr(address, 'ipv4_mapped', None) or getattr(address, 'sixtofour', None)
                return address.is_global and not getattr(address, 'teredo', None) and (embedded is None or embedded.is_global)
            if not addresses or any(not public_address(address) for address in addresses):
                raise Invalid('Dependency destination resolves to a non-public address')
            connection = PinnedHTTPS(parts.hostname, sorted(addresses)[0])
            try:
                route = urllib.parse.urlunsplit(('', '', parts.path or '/', parts.query, ''))
                connection.request('GET', route, headers={'User-Agent': 'Portkeeper-worker/1'})
                response = connection.getresponse()
                if response.status in (301, 302, 303, 307, 308):
                    url = urllib.parse.urljoin(url, response.getheader('Location') or '')
                    continue
                if response.status != 200:
                    raise Invalid(f'Dependency download returned HTTP {response.status}')
                length = response.getheader('Content-Length')
                if length and int(length) > MAX_BYTES:
                    raise Invalid('Dependency exceeds the download limit')
                with temporary.open('wb') as output:
                    while True:
                        if cancelled() or time.monotonic() - started > 300:
                            raise Invalid('Dependency download cancelled or timed out')
                        chunk = response.read(1024**2)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > MAX_BYTES:
                            raise Invalid('Dependency exceeds the download limit')
                        output.write(chunk)
                digest = checksum(temporary)
                if expected and digest != expected:
                    raise Invalid('Dependency SHA-256 does not match')
                os.replace(temporary, destination)
                return {'url': original, 'resolved_url': url, 'sha256': digest, 'size': total}
            finally:
                connection.close()
        raise Invalid('Dependency has too many redirects')
    finally:
        temporary.unlink(missing_ok=True)


class WorkerBroker:
    def __init__(self, root, input_root, image, *, allow_rootful_test=False, protected_roots=()):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.input_root = Path(input_root).resolve()
        self.protected_roots = [self.root, *[Path(path).resolve() for path in protected_roots]]
        if self.root.is_relative_to(self.input_root) or self.input_root.is_relative_to(self.root):
            raise Invalid('Broker storage and owned inputs must not overlap')
        if not IMAGE.fullmatch(image):
            raise Invalid('Configure an immutable worker image digest or image ID')
        self.image = image
        self.store = Store(self.root / 'broker.sqlite3')
        self.lock = threading.Lock()
        self.cancelled = set()
        self.guard = threading.Lock()
        self.active_names = {}
        self.test_mode = allow_rootful_test
        self.backend = 'unavailable'
        self.ready, self.error = False, None
        try:
            options = json.loads(self.docker('info', '--format', '{{json .SecurityOptions}}'))
            if not any('rootless' in value for value in options) and not allow_rootful_test:
                raise Invalid('A dedicated rootless Docker daemon is required; the host daemon is refused')
            if not any('seccomp' in value for value in options):
                raise Invalid('Worker daemon must enforce a seccomp profile')
            self.backend = 'rootful-test-only' if allow_rootful_test else 'rootless-docker'
            self.image_id = self.docker('image', 'inspect', '--format', '{{.Id}}', image).strip()
            # Recover only containers labelled by this broker installation.
            owner = hashlib.sha256(str(self.root).encode()).hexdigest()[:16]
            self.owner = owner
            names = self.docker('ps', '-aq', '--filter', 'label=portkeeper.broker=' + owner).split()
            for name in names:
                self.docker('rm', '-f', name)
            for op in self.store.all('worker_operations'):
                if op['status'] == 'running':
                    self.store.update('worker_operations', op['id'], status='interrupted', error='Broker restarted during this operation')
            # Readiness means the profile actually ran, not just that flags parsed.
            probe_job, probe_operation = os.urandom(16).hex(), os.urandom(16).hex()
            probe_root = self.job_root(probe_job)
            try:
                seed = probe_root / 'seed'
                seed.mkdir(); seed.chmod(0o755)
                result = self.run({'op': 'run', 'job_id': probe_job, 'operation_id': probe_operation,
                                  'seed': 'seed', 'phase': 'public', 'script': '''printf profile-ok > profile-check
python3 -I -c "import json,platform,shutil; print(json.dumps({'architecture':platform.machine(),'tools':{name:bool(shutil.which(name)) for name in ('python3','gcc','g++','clang','cmake','ninja','make','meson','pkg-config','git','unzip','xvfb-run','java','dotnet','cargo','rustc','wine')}}))"
''', 'timeout': 60})
                if result['exit_code'] or (probe_root / result['snapshot'] / 'profile-check').read_text() != 'profile-ok':
                    raise Invalid('Worker profile did not execute and export its files correctly')
                self.toolchain = json.loads((probe_root / result['log']).read_text())
            finally:
                shutil.rmtree(probe_root, ignore_errors=True)
            self.ready = True
        except (Invalid, OSError, subprocess.SubprocessError, ValueError) as error:
            self.error = str(error)[-1000:]

    def docker(self, *arguments):
        result = subprocess.run(['docker', *arguments], capture_output=True, timeout=30, check=False)
        if result.returncode:
            detail = result.stderr.decode(errors='replace')
            if len(detail) > 1000:
                detail = detail[:200] + ' … ' + detail[-800:]
            raise Invalid('Worker daemon operation failed: ' + detail)
        return result.stdout.decode(errors='replace')

    def health(self):
        return {'ready': self.ready, 'error': self.error, 'backend': self.backend,
                'image': getattr(self, 'image_id', self.image), 'protocol': 1,
                'toolchain': getattr(self, 'toolchain', {}),
                'limits': {'workspace_bytes': MAX_BYTES, 'files': MAX_FILES, 'log_bytes': MAX_LOG,
                           'cpus': 4, 'memory_bytes': 8 * 1024**3, 'pids': 256}, 'test_only': self.test_mode}

    def is_cancelled(self, job):
        with self.guard:
            return job in self.cancelled

    def cancel(self, job):
        if not ID.fullmatch(job):
            raise Invalid('Invalid worker job')
        with self.guard:
            self.cancelled.add(job)
            name = self.active_names.get(job)
        if name:
            self.docker('rm', '-f', name)
        return {'cancelled': True}

    def handle(self, payload):
        if not isinstance(payload, dict) or payload.get('op') not in ('health', 'run', 'fetch', 'status', 'cancel'):
            raise Invalid('Unknown broker operation')
        if payload['op'] == 'health':
            return self.health()
        job = payload.get('job_id', '')
        if not isinstance(job, str) or not ID.fullmatch(job):
            raise Invalid('Invalid worker job')
        if payload['op'] == 'cancel':
            return self.cancel(job)
        operation = payload.get('operation_id', '')
        if not isinstance(operation, str) or not ID.fullmatch(operation):
            raise Invalid('Invalid worker operation')
        if payload['op'] == 'status':
            record = self.store.get('worker_operations', operation)
            if record['job_id'] != job:
                raise Invalid('Operation belongs to another job')
            return record
        if not self.ready:
            raise Invalid(self.error or 'Worker broker is unavailable')
        if self.is_cancelled(job):
            raise Invalid('Worker job was cancelled')
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        if not self.lock.acquire(blocking=False):
            raise Invalid('Another worker operation is active; wait for it to finish')
        try:
            try:
                record = self.store.get('worker_operations', operation)
            except KeyError:
                record = None
            if record:
                if record.get('request_sha256') != digest:
                    raise Invalid('Operation ID was reused with different arguments')
                if record['status'] != 'complete':
                    raise Invalid(record.get('error') or 'Operation is still running')
                return record['result']
            self.store.put('worker_operations', {'id': operation, 'job_id': job, 'status': 'running',
                           'request_sha256': digest, 'started_at': now()})
            try:
                if payload['op'] == 'fetch':
                    result = self.fetch(payload)
                else:
                    result = self.run(payload)
                self.store.update('worker_operations', operation, status='complete', result=result, finished_at=now())
                return result
            except Exception as error:
                self.store.update('worker_operations', operation, status='failed', error=str(error)[:500], finished_at=now())
                raise
        finally:
            self.lock.release()

    def job_root(self, job):
        path = self.root / 'jobs' / job
        path.mkdir(parents=True, exist_ok=True)
        if path.is_symlink() or path.parent.is_symlink():
            raise Invalid('Broker job paths cannot be symlinks')
        return path

    def fetch(self, payload):
        if set(payload) - {'op', 'job_id', 'operation_id', 'url', 'name', 'sha256'}:
            raise Invalid('Unsupported dependency argument')
        name = payload.get('name')
        if not isinstance(name, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]{0,120}', name):
            raise Invalid('Dependencies need a simple unique filename')
        if not isinstance(payload.get('url'), str) or not 1 <= len(payload['url']) <= 4000:
            raise Invalid('Dependencies need a bounded public HTTPS URL')
        expected = payload.get('sha256')
        if expected is not None and (not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected)):
            raise Invalid('Dependency SHA-256 must have 64 lowercase hexadecimal characters')
        root = self.job_root(payload['job_id']) / 'downloads'
        root.mkdir(exist_ok=True)
        if any(p.is_symlink() for p in (root, root / name)) or (root / name).exists():
            raise Invalid('Dependency name already exists or is unsafe')
        result = fetch_public(payload.get('url', ''), root / name, expected, lambda: self.is_cancelled(payload['job_id']))
        result.update(name=name)
        return result

    def run(self, payload):
        allowed = {'op', 'job_id', 'operation_id', 'script', 'seed', 'inputs', 'phase', 'timeout', 'target', 'source_date_epoch'}
        if set(payload) - allowed:
            raise Invalid('Unsupported worker argument')
        job, operation = payload['job_id'], payload['operation_id']
        phase, script = payload.get('phase'), payload.get('script')
        if phase not in ('public', 'private') or not isinstance(script, str) or not 1 <= len(script.encode()) <= 32768 or '\x00' in script:
            raise Invalid('Use a public/private phase and a script of at most 32 KiB')
        timeout = payload.get('timeout', 600)
        if type(timeout) is not int or not 1 <= timeout <= 1800:
            raise Invalid('Command timeout must be between 1 and 1800 seconds')
        target = payload.get('target', 'linux')
        epoch = payload.get('source_date_epoch', 0)
        if target not in ('linux', 'windows', 'handheld') or type(epoch) is not int or epoch < 0:
            raise Invalid('Invalid target or source date')
        root = self.job_root(job)
        seed = confined(root, payload.get('seed', 'seed'))
        if not seed.is_dir():
            raise Invalid('Worker seed is missing')
        identity = tree_identity(seed)
        if shutil.disk_usage(root).free < 2 * MAX_BYTES + 256 * 1024**2:
            raise Invalid('Broker needs at least 4.25 GiB free for a bounded snapshot export')
        # Rootless UID 0 maps to the unprivileged broker account, so its read-only
        # input mounts remain readable without chmodding the user's original files.
        worker_uid = '10001' if self.test_mode else '0'
        inputs = payload.get('inputs', [])
        if not isinstance(inputs, list) or len(inputs) > 16 or (inputs and phase != 'private'):
            raise Invalid('Owned inputs are limited to private workers')
        mounts = []
        for index, item in enumerate(inputs):
            if not isinstance(item, dict) or set(item) != {'path', 'kind'} or item['kind'] not in ('file', 'directory'):
                raise Invalid('Invalid owned input mount')
            path = confined(self.input_root, item['path'])
            if any(path.is_relative_to(protected) or protected.is_relative_to(path) for protected in self.protected_roots):
                raise Invalid('Owned input overlaps protected broker/controller storage')
            if (item['kind'] == 'file' and not path.is_file()) or (item['kind'] == 'directory' and not path.is_dir()):
                raise Invalid('Selected owned input is missing')
            if any(c in str(path) for c in (',', '\n', '\r')):
                raise Invalid('Input path cannot be represented as a safe worker mount')
            mounts.extend(['--mount', f'type=bind,source={path},target=/inputs/input{index},readonly'])
        if any(c in str(seed) for c in (',', '\n', '\r')):
            raise Invalid('Broker root cannot contain Docker mount delimiters')
        name = 'portkeeper-worker-' + operation
        log_path = root / (operation + '.log')
        snapshot = root / ('snapshot-' + operation)
        archive = root / (operation + '.tar')
        args = ['create', '--name', name, '--label', 'portkeeper.broker=' + self.owner,
                '--network', 'none', '--read-only', '--user', worker_uid + ':' + worker_uid, '--init',
                '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges:true',
                '--pids-limit', '256', '--cpus', '4', '--memory', '8g', '--memory-swap', '8g',
                '--ulimit', 'nofile=1024:1024', '--ulimit', 'core=0:0',
                '--log-driver', 'none', '--stop-timeout', '2',
                '--tmpfs', f'/work:rw,exec,nosuid,nodev,size=2g,uid={worker_uid},gid={worker_uid},mode=0700',
                '--tmpfs', f'/tmp:rw,exec,nosuid,nodev,size=256m,uid={worker_uid},gid={worker_uid},mode=1777',
                '--workdir', '/work', '--env', 'HOME=/tmp', '--env', 'PORTKEEPER_TARGET=' + target,
                '--env', 'SOURCE_DATE_EPOCH=' + str(epoch), '--env', 'TZ=UTC', '--env', 'LC_ALL=C.UTF-8',
                '--mount', f'type=bind,source={seed},target=/seed,readonly', *mounts,
                '--entrypoint', '/bin/sh', self.image_id, '-c', 'exec sleep infinity']
        started = time.monotonic()
        process = None
        with self.guard:
            self.active_names[job] = name
        try:
            self.docker(*args)
            self.docker('start', name)
            # Check *actual* cgroup enforcement rather than trusting Docker flags.
            probe = '''import pathlib
root=pathlib.Path('/sys/fs/cgroup')
assert (root/'memory.max').read_text().strip() == '8589934592', '8 GiB memory limit is not enforced'
assert (root/'memory.swap.max').read_text().strip() == '0', 'Swap limit is not enforced'
assert (root/'pids.max').read_text().strip() == '256', 'PID limit is not enforced'
quota,period=(root/'cpu.max').read_text().split()
assert quota != 'max' and int(quota)/int(period) <= 4, 'CPU limit is not enforced'
status=pathlib.Path('/proc/self/status').read_text()
assert 'NoNewPrivs:\\t1' in status and 'Seccomp:\\t2' in status, 'Process isolation is not enforced'
assert all(int(line.split()[1],16)==0 for line in status.splitlines() if line.startswith(('CapEff:', 'CapBnd:'))), 'Capabilities are available'
assert not pathlib.Path('/var/run/docker.sock').exists(), 'Daemon socket exposed'
assert not any(p.name != 'lo' for p in pathlib.Path('/sys/class/net').iterdir()), 'Network interface exposed'
try:
    pathlib.Path('/portkeeper-write-probe').write_text('unsafe')
except OSError:
    pass
else:
    raise RuntimeError('Root filesystem is writable')
'''
            self.docker('exec', name, 'python3', '-c', probe)
            self.docker('exec', name, 'python3', '-c', "import shutil; shutil.copytree('/seed','/work',dirs_exist_ok=True,symlinks=True)")
            keeper = self.docker('exec', name, 'python3', '-I', '-S', '-c',
                                 "import os; print(' '.join(p for p in os.listdir('/proc') if p.isdigit() and int(p) not in (1,os.getpid())))").split()
            if len(keeper) != 1 or not keeper[0].isdigit():
                raise Invalid('Worker process supervisor could not be identified')
            # Only the shell *inside the container* sees the model's program.
            with log_path.open('wb') as output:
                process = subprocess.Popen(['docker', 'exec', '-i', name, '/bin/sh', '-se'],
                                           stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
                process.stdin.write(script.encode())
                process.stdin.close()
                selector = selectors.DefaultSelector()
                selector.register(process.stdout, selectors.EVENT_READ)
                size = 0
                try:
                    while True:
                        if self.is_cancelled(job) or time.monotonic() - started > timeout:
                            raise Invalid('Worker command cancelled or timed out')
                        for key, _ in selector.select(0.1):
                            chunk = os.read(key.fileobj.fileno(), 65536)
                            if chunk:
                                size += len(chunk)
                                if size > MAX_LOG:
                                    raise Invalid('Worker command exceeded its 1 MiB log limit')
                                output.write(chunk)
                            else:
                                selector.unregister(key.fileobj)
                        if process.poll() is not None and not selector.get_map():
                            break
                finally:
                    selector.close()
            # Stop every remaining process before exporting. A background writer
            # cannot race host checks or mutate the read-only seed/input mounts.
            export = '''import os,signal,sys,tarfile,time
keepers={1,int(sys.argv[1]),os.getpid()}
for attempt in range(100):
    others=[int(p) for p in os.listdir('/proc') if p.isdigit() and int(p) not in keepers]
    # Ignore already reaped/zombie processes; they cannot write or fork.
    live=[]
    for pid in others:
        try:
            status=open('/proc/%d/stat'%pid).read().rsplit(')',1)[1].split()[0]
            if status != 'Z':
                live.append(pid)
                os.kill(pid,signal.SIGKILL)
        except ProcessLookupError:
            pass
        except FileNotFoundError:
            pass
    if not live:
        break
    time.sleep(.02)
else:
    raise RuntimeError('Background worker processes could not be stopped')
with tarfile.open(fileobj=sys.stdout.buffer,mode='w|',format=tarfile.PAX_FORMAT) as archive:
    archive.add('/work',arcname='.',recursive=True)
'''
            with archive.open('wb') as output:
                copy_process = subprocess.Popen(['docker', 'exec', name, 'python3', '-I', '-S', '-c', export, keeper[0]],
                                                stdout=output, stderr=subprocess.PIPE)
                try:
                    while copy_process.poll() is None:
                        if self.is_cancelled(job) or archive.stat().st_size > MAX_BYTES + 64 * 1024**2 or time.monotonic() - started > timeout + 60:
                            copy_process.kill()
                            raise Invalid('Worker snapshot exceeded its size/time limit')
                        time.sleep(0.1)
                    stderr = copy_process.stderr.read()
                    if copy_process.returncode:
                        raise Invalid('Could not preserve the worker snapshot: ' + stderr.decode(errors='replace')[:200])
                finally:
                    if copy_process.poll() is None:
                        copy_process.kill()
                    copy_process.wait()
                    copy_process.stderr.close()
            result_identity = unpack_snapshot(archive, snapshot)
            if tree_identity(seed)['sha256'] != identity['sha256']:
                raise Invalid('Worker seed changed during execution')
            return {'exit_code': process.returncode, 'phase': phase, 'snapshot': snapshot.name,
                    'identity': {k: v for k, v in result_identity.items() if k != 'entries'},
                    'log': log_path.name, 'seconds': round(time.monotonic() - started, 3)}
        finally:
            if process is not None:
                if process.poll() is None:
                    process.kill()
                process.wait()
                process.stdout.close()
            try:
                self.docker('rm', '-f', name)
            finally:
                archive.unlink(missing_ok=True)
                with self.guard:
                    self.active_names.pop(job, None)


class BrokerHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_POST(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
            if self.path != '/v1' or not 0 < length <= 256 * 1024 or self.headers.get('Transfer-Encoding'):
                raise Invalid('Invalid broker request')
            self.connection.settimeout(15)
            payload = json.loads(self.rfile.read(length))
            result = self.server.broker.handle(payload)
            status, response = 200, {'result': result}
        except (Invalid, ValueError, KeyError, OSError, subprocess.SubprocessError) as error:
            status, response = 400, {'error': str(error)[:500]}
        body = json.dumps(response).encode()
        self.send_response(status)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(body)


class UnixServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout):
        super().__init__('localhost', timeout=timeout)
        self.path = str(path)

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.path)


class BrokerClient:
    def __init__(self, socket_path, root):
        self.socket_path, self.root = socket_path, Path(root)

    def request(self, op, **payload):
        connection = UnixConnection(self.socket_path, 5 if op in ('health', 'status') else 1900)
        try:
            connection.request('POST', '/v1', json.dumps({'op': op, **payload}), {'Content-Type': 'application/json'})
            response = connection.getresponse()
            value = json.loads(response.read(65536))
            if response.status != 200:
                raise Invalid(value.get('error', 'Worker broker rejected the action'))
            return value['result']
        except (OSError, http.client.HTTPException):
            raise Invalid('Worker broker could not be reached; check its socket and service') from None
        finally:
            connection.close()


def make_server(broker, socket_path):
    socket_path = Path(socket_path)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    socket_path.parent.chmod(0o700)
    if socket_path.exists():
        if not stat.S_ISSOCK(socket_path.lstat().st_mode):
            raise Invalid('Broker socket path already contains a file')
        # Never replace a live broker socket.
        probe = socket.socket(socket.AF_UNIX)
        try:
            probe.connect(str(socket_path))
        except ConnectionRefusedError:
            socket_path.unlink()
        else:
            raise Invalid('Another broker is already listening')
        finally:
            probe.close()
    server = UnixServer(str(socket_path), BrokerHandler)
    server.broker = broker
    socket_path.chmod(0o600)
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--input-root', type=Path, required=True)
    parser.add_argument('--socket', type=Path, required=True)
    parser.add_argument('--image', required=True, help='Pre-pulled immutable image with /bin/sh, Python 3 and any base build tools')
    parser.add_argument('--protected-root', type=Path, action='append', default=[], help='Never expose this controller/credential directory through selected input mounts (repeatable)')
    parser.add_argument('--allow-rootful-test', action='store_true', help='TESTS ONLY: refuse use by the application')
    args = parser.parse_args()
    broker = WorkerBroker(args.root, args.input_root, args.image, allow_rootful_test=args.allow_rootful_test, protected_roots=args.protected_root)
    server = make_server(broker, args.socket)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt()))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        for job in list(broker.active_names):
            broker.cancel(job)
        server.server_close()
        args.socket.unlink(missing_ok=True)


if __name__ == '__main__':
    main()
