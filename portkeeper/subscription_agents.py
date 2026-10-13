"""Text-only adapters for the official subscription-backed agent CLIs.

Credentials remain in each CLI's private home. Portkeeper never reads a token or
implements an OAuth flow. CLI responses are untrusted drafts; the caller must
apply the existing proposal validator before presenting/applying them.

Controls verified against Codex 0.162.1, Claude Code 2.1.287 and Gemini 0.63.0:
https://learn.chatgpt.com/docs/developer-commands?surface=cli
https://learn.chatgpt.com/docs/app-server
https://code.claude.com/docs/en/cli-reference
https://geminicli.com/docs/reference/policy-engine/
https://geminicli.com/docs/cli/acp-mode/
"""
import collections
import json
import os
import re
import selectors
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
import time
import urllib.parse
from pathlib import Path

from .safety import Invalid

PROVIDERS = ('codex', 'claude_code', 'gemini')
BINARIES = {'codex': 'codex', 'claude_code': 'claude', 'gemini': 'gemini'}
LABELS = {'codex': 'Codex', 'claude_code': 'Claude Code', 'gemini': 'Gemini CLI'}
MAX_OUTPUT = 2 * 1024 * 1024
MAX_PROMPT = 2 * 1024 * 1024
AUTH_TTL = 30
LOGIN_TIMEOUT = 600
_MODEL = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,149}')
_ANSI = re.compile(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*?(?:\x07|\x1b\\))')
_DENY_POLICY = '[[rule]]\ntoolName = "*"\ndecision = "deny"\npriority = 999\n'
_CODEX_CONFIG = '''forced_login_method = "chatgpt"
cli_auth_credentials_store = "file"
approval_policy = "never"
sandbox_mode = "read-only"
web_search = "disabled"
project_doc_max_bytes = 0
check_for_update_on_startup = false
[agents]
enabled = false
[apps._default]
enabled = false
[features]
shell_tool = false
unified_exec = false
view_image = false
apps = false
browser_use = false
computer_use = false
image_generation = false
hooks = false
plugins = false
multi_agent = false
memories = false
skill_search = false
skill_mcp_dependency_install = false
workspace_dependencies = false
shell_snapshot = false
code_mode = false
code_mode_host = false
sleep_tool = false
goals = false
tool_suggest = false
'''


def _terminate(process):
    if process.poll() is None:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                process.kill()
            except ProcessLookupError:
                pass
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


class _IO:
    """Bounded pipe I/O, including when a child stops reading its stdin."""
    def __init__(self, owner, provider, argv, cwd, timeout, limit=MAX_OUTPUT):
        self.owner, self.provider = owner, provider
        self.process = owner._spawn(provider, argv, cwd)
        self.deadline, self.limit = time.monotonic() + timeout, limit
        self.total = 0
        self.stdout, self.stderr = bytearray(), bytearray()
        self.partial, self.lines = bytearray(), collections.deque()
        self.events = collections.deque()
        self.selector = selectors.DefaultSelector()
        for stream in (self.process.stdout, self.process.stderr, self.process.stdin):
            os.set_blocking(stream.fileno(), False)
        for stream in (self.process.stdout, self.process.stderr):
            self.selector.register(stream, selectors.EVENT_READ)

    def _pump(self, pending=None):
        if time.monotonic() >= self.deadline:
            raise Invalid(f'{LABELS[self.provider]} timed out. No draft was applied.')
        if self.owner._closed:
            raise Invalid('Subscription drafting was stopped')
        for key, _ in self.selector.select(min(.2, max(0, self.deadline - time.monotonic()))):
            stream = key.fileobj
            if stream is self.process.stdin:
                try:
                    count = os.write(stream.fileno(), pending[:65536])
                    del pending[:count]
                except (BrokenPipeError, OSError):
                    pending.clear()
                if not pending:
                    self.selector.unregister(stream)
                continue
            try:
                data = os.read(stream.fileno(), 65536)
            except BlockingIOError:
                continue
            if not data:
                self.selector.unregister(stream)
                continue
            self.total += len(data)
            if self.total > self.limit:
                raise Invalid(f'{LABELS[self.provider]} exceeded the output limit. No draft was applied.')
            if stream is self.process.stderr:
                self.stderr.extend(data)
            else:
                self.stdout.extend(data)
                self.partial.extend(data)
                while b'\n' in self.partial:
                    line, _, rest = self.partial.partition(b'\n')
                    self.partial = bytearray(rest)
                    self.lines.append(line)

    def send(self, value):
        pending = bytearray(value.encode('utf-8') if isinstance(value, str) else value)
        self.selector.register(self.process.stdin, selectors.EVENT_WRITE)
        while pending:
            self._pump(pending)

    def collect(self, value=''):
        if value:
            self.send(value)
        self.process.stdin.close()
        while self.selector.get_map():
            self._pump()
            self.lines.clear()
        while self.process.poll() is None:
            self._pump()
        return self.process.returncode, self.stdout.decode('utf-8', 'replace'), self.stderr.decode('utf-8', 'replace')

    def next(self):
        while True:
            if self.events:
                return self.events.popleft()
            while self.lines:
                try:
                    value = json.loads(self.lines.popleft())
                except (ValueError, UnicodeDecodeError):
                    continue
                if isinstance(value, dict):
                    return value
            if self.process.poll() is not None and not self.selector.get_map():
                raise Invalid(f'{LABELS[self.provider]} ended before returning a draft. Check your login and plan limits.')
            self._pump()

    def close(self):
        _terminate(self.process)
        self.selector.close()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            try:
                stream.close()
            except OSError:
                pass
        self.owner._forget(self.process)


class SubscriptionAgents:
    def __init__(self, data_dir):
        self.data_dir = Path(data_dir).absolute()
        self.homes = self.data_dir / 'secrets' / 'agents'
        self.workspaces = self.data_dir / 'agent-workspaces'
        self._lock = threading.RLock()
        self._auth_cache, self._help_cache, self._logins = {}, {}, {}
        self._models_cache = {}
        self._active, self._processes = set(), set()
        self._closed = False

    @staticmethod
    def _provider(provider):
        if provider not in PROVIDERS:
            raise Invalid('Choose Codex, Claude Code, or Gemini CLI')
        return provider

    def _check_path(self, path):
        for current in (path, *path.parents):
            if current.is_symlink():
                raise Invalid('Agent state must not use symlinks')
            if current == self.data_dir:
                break

    def _dir(self, path):
        self._check_path(path)
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        path.chmod(0o700)
        return path

    def _write(self, path, content):
        self._dir(path.parent)
        self._check_path(path)
        if path.is_file() and path.read_text(encoding='utf-8') == content:
            path.chmod(0o600)
            return
        descriptor, temporary = tempfile.mkstemp(prefix='.portkeeper-', dir=path.parent)
        try:
            with os.fdopen(descriptor, 'w', encoding='utf-8') as stream:
                stream.write(content)
            os.replace(temporary, path)
            path.chmod(0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _home(self, provider):
        self._dir(self.data_dir / 'secrets')
        self._dir(self.homes)
        home = self._dir(self.homes / provider)
        self._dir(home / 'tmp')
        for name in ('.config', '.cache', '.local/share'):
            self._dir(home / name)
        if provider == 'codex':
            self._write(home / '.codex/config.toml', _CODEX_CONFIG)
        elif provider == 'gemini':
            # User and system sources are owned by this adapter. Native OAuth
            # credentials are separate and are never read or rewritten here.
            settings = {'security': {'auth': {'selectedType': 'oauth-personal', 'enforcedType': 'oauth-personal'},
                                     'disableYoloMode': True, 'disableAlwaysAllow': True, 'blockGitExtensions': True},
                        'advanced': {'ignoreLocalEnv': True}, 'mcpServers': {},
                        'general': {'enableAutoUpdate': False}, 'hooks': {'disabled': True},
                        'telemetry': {'enabled': False}, 'context': {'fileName': []}}
            self._write(home / '.gemini/settings.json', json.dumps(settings))
            self._write(home / '.gemini/system-settings.json', json.dumps(settings))
            self._write(home / '.gemini/system-defaults.json', '{}')
            self._write(home / '.gemini/policies/portkeeper.toml', _DENY_POLICY)
        else:
            self._dir(home / '.claude')
        return home

    def _env(self, provider):
        home = self._home(provider)
        # An allowlist removes every API key, cloud credential, custom endpoint,
        # NODE_OPTIONS injection, proxy, inherited agent configuration and hook.
        env = {name: os.environ[name] for name in ('PATH', 'LANG', 'LC_ALL', 'SYSTEMROOT', 'WINDIR') if name in os.environ}
        env.update(HOME=str(home), USERPROFILE=str(home), TMPDIR=str(home / 'tmp'),
                   XDG_CONFIG_HOME=str(home / '.config'), XDG_CACHE_HOME=str(home / '.cache'),
                   XDG_DATA_HOME=str(home / '.local/share'), NO_COLOR='1', TERM='dumb', NO_BROWSER='true',
                   DISABLE_AUTOUPDATER='1', DISABLE_UPDATES='1')
        if provider == 'codex':
            env['CODEX_HOME'] = str(home / '.codex')
        elif provider == 'claude_code':
            env['CLAUDE_CONFIG_DIR'] = str(home / '.claude')
            env['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'] = '1'
        else:
            env.update(GEMINI_CLI_HOME=str(home), GEMINI_CLI_SYSTEM_SETTINGS_PATH=str(home / '.gemini/system-settings.json'),
                       GEMINI_CLI_SYSTEM_DEFAULTS_PATH=str(home / '.gemini/system-defaults.json'), GEMINI_TELEMETRY_ENABLED='false')
        return env

    def _binary(self, provider):
        return shutil.which(BINARIES[provider])

    def _control_cwd(self, provider):
        return self._dir(self.workspaces / ('control-' + provider))

    def _spawn(self, provider, argv, cwd, **kwargs):
        binary = self._binary(provider)
        if not binary:
            raise Invalid(f'Install the official {LABELS[provider]} CLI on the Portkeeper server')
        with self._lock:
            if self._closed:
                raise Invalid('Subscription drafting was stopped')
            try:
                process = subprocess.Popen([binary, *argv], cwd=cwd, env=self._env(provider),
                                           stdin=kwargs.pop('stdin', subprocess.PIPE), stdout=kwargs.pop('stdout', subprocess.PIPE),
                                           stderr=kwargs.pop('stderr', subprocess.PIPE), start_new_session=True, umask=0o077, **kwargs)
            except OSError:
                raise Invalid(f'{LABELS[provider]} could not be started on the Portkeeper server') from None
            self._processes.add(process)
        return process

    def _forget(self, process):
        with self._lock:
            self._processes.discard(process)

    def _run(self, provider, argv, cwd=None, value='', timeout=15, limit=MAX_OUTPUT):
        io = _IO(self, provider, argv, cwd or self._control_cwd(provider), timeout, limit)
        try:
            return io.collect(value)
        finally:
            io.close()

    def _capabilities(self, provider):
        binary = self._binary(provider)
        cached = self._help_cache.get(provider)
        identity = (binary, Path(binary).stat().st_mtime_ns) if binary else None
        if cached and cached[0] == identity:
            return cached[1]
        version_code, version, _ = self._run(provider, ['--version'], limit=65536)
        _, help_text, _ = self._run(provider, ['--help'], limit=262144)
        match = re.search(r'\b\d+\.\d+\.\d+(?:[-+][A-Za-z0-9.-]+)?\b', version) if version_code == 0 else None
        result = {'version': match.group(0) if match else '', 'help': help_text}
        if provider == 'codex':
            # This minimum has an explicitly represented no-environment contract.
            parts = tuple(map(int, match.group(0).split('-')[0].split('+')[0].split('.'))) if match else ()
            result['supported'] = parts >= (0, 162, 1) and 'app-server' in help_text
        else:
            required = ('--safe-mode', '--restricted', '--tools', '--strict-mcp-config', '--setting-sources', '--no-session-persistence', '--disable-slash-commands') if provider == 'claude_code' else ('--acp', '--admin-policy', '--policy', '--extensions', '--output-format')
            result['supported'] = all(flag in help_text for flag in required)
        self._help_cache[provider] = (identity, result)
        return result

    def _rpc_request(self, io, method, params, identifier):
        io.send(json.dumps({'jsonrpc': '2.0', 'id': identifier, 'method': method, 'params': params}) + '\n')
        deferred = []
        while True:
            event = io.next()
            self._deny_requests(io, event)
            if event.get('id') == identifier:
                io.events.extend(deferred)
                if 'error' in event:
                    raise Invalid(f'{LABELS[io.provider]} rejected the restricted session. Check login, plan limits, and CLI version.')
                return event.get('result', {})
            deferred.append(event)

    @staticmethod
    def _deny_requests(io, event):
        if 'method' in event and 'id' in event:
            # No server-requested shell, tool, approval or credentials callbacks
            # are serviced. Reply with a protocol error, then stop the session.
            io.send(json.dumps({'jsonrpc': '2.0', 'id': event['id'], 'error': {'code': -32601, 'message': 'Tools and callbacks are disabled for Portkeeper drafting'}}) + '\n')
            raise Invalid('The agent requested a tool or interactive callback. Text-only drafting was stopped.')

    def _gemini_authenticated(self):
        home = self._home('gemini')
        credential = home / '.gemini/oauth_creds.json'
        self._check_path(credential)
        if not credential.is_file():
            return False
        io = _IO(self, 'gemini', ['--acp', '--extensions', 'none'], self._control_cwd('gemini'), 15, 65536)
        try:
            self._rpc_request(io, 'initialize', {'protocolVersion': 1, 'clientCapabilities': {}}, 1)
            self._rpc_request(io, 'authenticate', {'methodId': 'oauth-personal'}, 2)
            return True
        finally:
            io.close()

    def status(self, provider):
        self._provider(provider)
        # A single native probe per cache interval, including initial concurrent
        # requests. The RLock also serializes config creation and login/logout.
        with self._lock:
            return self._status(provider)

    def _status(self, provider):
        result = {'installed': bool(self._binary(provider)), 'authenticated': False,
                  'billing': 'free_account' if provider == 'gemini' else 'subscription',
                  'message': f'Install the official {LABELS[provider]} CLI on the Portkeeper server.'}
        if not result['installed']:
            return result
        cached = self._auth_cache.get(provider)
        if cached and time.monotonic() - cached[0] < AUTH_TTL:
            return dict(cached[1])
        try:
            caps = self._capabilities(provider)
            if caps['version']:
                result['version'] = caps['version']
            if provider == 'codex':
                code, output, error = self._run(provider, ['login', 'status'], limit=65536)
                result['authenticated'] = code == 0 and bool(re.search(r'logged in (?:with|using) chatgpt', output + error, re.I))
                if re.search(r'api.?key', output + error, re.I):
                    result['billing'] = 'api_key'
            elif provider == 'claude_code':
                code, output, _ = self._run(provider, ['auth', 'status', '--json'], limit=65536)
                native = json.loads(output) if code == 0 else {}
                # A generic OAuth label could refer to Claude Console billing.
                # Only the native CLI's explicit consumer-account mode counts.
                result['authenticated'] = native.get('loggedIn') is True and native.get('authMethod') == 'claude.ai' and native.get('apiProvider', 'firstParty') == 'firstParty'
                if native.get('loggedIn') and not result['authenticated']:
                    result['billing'] = 'api_key'
            else:
                result['authenticated'] = self._gemini_authenticated() if caps['supported'] else False
                result['billing'] = 'account_quota' if result['authenticated'] else 'free_account'
            result['drafting_supported'] = caps['supported']
            result['message'] = ('Signed in with the native CLI. Subscription limits and provider extra-usage settings apply; API keys are not used.' if result['authenticated'] else
                                 f'Sign in to {LABELS[provider]} with your own subscription on the Portkeeper server.')
            if result['billing'] == 'api_key':
                result['message'] = f'{LABELS[provider]} is signed in with API billing. Sign out and sign in with your subscription.'
            elif provider == 'gemini':
                result['message'] = ('Signed in with the native CLI. Google account quotas apply; API keys are not used.' if result['authenticated'] else
                                     'Sign in with a free Google account or Google AI subscription on the Portkeeper server.')
            if not caps['supported']:
                result['message'] = f'Update the official {LABELS[provider]} CLI to support restricted text-only drafting.'
        except (Invalid, ValueError, OSError, TypeError):
            result['message'] = f'{LABELS[provider]} login could not be verified. Sign in again with the official CLI.'
        with self._lock:
            self._auth_cache[provider] = (time.monotonic(), dict(result))
        return result

    def models(self, provider):
        self._provider(provider)
        # Stable documented aliases; empty choice uses each CLI's account default.
        models = {'codex': [], 'claude_code': ['sonnet', 'opus', 'haiku'], 'gemini': ['auto']}[provider]
        if provider == 'codex':
            cached = self._models_cache.get(provider)
            if cached and time.monotonic() - cached[0] < 300:
                return dict(cached[1])
            if self.status(provider)['authenticated']:
                io = _IO(self, provider, ['--strict-config', 'app-server', '--stdio'], self._control_cwd(provider), 20, 262144)
                try:
                    self._rpc_request(io, 'initialize', {'clientInfo': {'name': 'portkeeper', 'version': '1'}, 'capabilities': {'experimentalApi': True}}, 1)
                    io.send(json.dumps({'jsonrpc': '2.0', 'method': 'initialized'}) + '\n')
                    native = self._rpc_request(io, 'model/list', {'limit': 100}, 2)
                    models = [item['model'] for item in native.get('data', []) if isinstance(item, dict) and isinstance(item.get('model'), str) and _MODEL.fullmatch(item['model'])]
                except Invalid:
                    raise Invalid('Codex model choices could not be loaded. Check your native sign-in; the provider default is still available.') from None
                finally:
                    io.close()
            result = {'models': models, 'source': 'cli choices', 'default': ''}
            if models:
                self._models_cache[provider] = (time.monotonic(), dict(result))
            return result
        return {'models': models, 'source': 'cli choices', 'default': ''}

    def _prompt(self, system, messages, schema, provider):
        if not isinstance(system, str) or not isinstance(messages, list):
            raise Invalid('Drafting requires a system instruction and text messages')
        conversation = []
        for item in messages:
            if not isinstance(item, dict) or item.get('role') not in ('user', 'assistant', 'system') or not isinstance(item.get('content'), str):
                raise Invalid('Agent drafting accepts text messages only')
            conversation.append({'role': item['role'], 'content': item['content']})
        record = {'system': system, 'messages': conversation}
        if schema is not None:
            if not isinstance(schema, dict):
                raise Invalid('The response schema must be an object')
            record['response_schema'] = schema
        try:
            prompt = json.dumps(record, ensure_ascii=False, allow_nan=False)
        except (ValueError, TypeError):
            raise Invalid('The draft context must be JSON text') from None
        if provider == 'gemini':
            # Gemini expands @file mentions before its tool policy runs. Keep
            # upstream text literal; JSON decoding still restores the same text.
            prompt = prompt.replace('@', r'\u0040')
        if len(prompt.encode('utf-8')) > MAX_PROMPT:
            raise Invalid('The draft context exceeds the agent input limit')
        if provider != 'gemini':
            record.pop('system', None)
            record.pop('response_schema', None)
            # Native system/schema fields carry these once rather than repeating
            # them in the user context sent to the model.
            prompt = json.dumps(record, ensure_ascii=False, allow_nan=False)
        return prompt

    def chat(self, provider, system, messages, model='', timeout=900, response_schema=None):
        self._provider(provider)
        if not isinstance(model, str) or (model and not _MODEL.fullmatch(model)):
            raise Invalid('Enter a model name or use the CLI default')
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= 900:
            raise Invalid('Agent timeout must be between 0 and 900 seconds')
        prompt = self._prompt(system, messages, response_schema, provider)
        with self._lock:
            if provider in self._active or self._login_running(provider):
                raise Invalid('Wait for the current agent draft or sign-in to finish')
            self._active.add(provider)
        workspace = None
        try:
            state = self.status(provider)
            if not state['authenticated']:
                raise Invalid(state['message'])
            if not state.get('drafting_supported'):
                raise Invalid(f'Update {LABELS[provider]} before restricted drafting can run')
            self._dir(self.workspaces)
            workspace = Path(tempfile.mkdtemp(prefix=provider + '-', dir=self.workspaces))
            self._write(workspace / 'prompt.json', prompt)
            if provider == 'codex':
                reply = self._codex_chat(workspace, system, prompt, model, timeout, response_schema)
            elif provider == 'claude_code':
                args = ['--print', '--safe-mode', '--restricted', '--output-format', 'json', '--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                        '--disallowedTools', 'mcp__*', '--setting-sources', '', '--settings', '{"hooks":{},"disableAllHooks":true}',
                        '--disable-slash-commands', '--no-chrome', '--no-session-persistence', '--system-prompt', system]
                if model:
                    args += ['--model', model]
                if response_schema is not None:
                    args += ['--json-schema', json.dumps(response_schema)]
                code, output, _ = self._run(provider, args, workspace, prompt, timeout)
                native = self._json_reply(provider, code, output)
                if native.get('is_error'):
                    raise Invalid('Claude Code could not draft. Check your native login and subscription limits.')
                reply = json.dumps(native['structured_output']) if native.get('structured_output') is not None else native.get('result', '')
            else:
                self._check_gemini_system_policy()
                policy = self._home(provider) / '.gemini/policies/portkeeper.toml'
                args = ['--prompt', 'Return only the requested draft from the supplied text. Do not use tools.', '--output-format', 'json',
                        '--extensions', 'none', '--approval-mode', 'default', '--policy', str(policy), '--admin-policy', str(policy)]
                if model and model != 'auto':
                    args += ['--model', model]
                code, output, _ = self._run(provider, args, workspace, prompt, timeout)
                native = self._json_reply(provider, code, output)
                if native.get('error'):
                    raise Invalid('Gemini CLI could not draft. Check your native login and subscription limits.')
                reply = native.get('response', '')
            if not isinstance(reply, str) or not reply.strip():
                raise Invalid(f'{LABELS[provider]} returned no final draft')
            return reply
        finally:
            if workspace:
                shutil.rmtree(workspace, ignore_errors=True)
            with self._lock:
                self._active.discard(provider)

    @staticmethod
    def _json_reply(provider, code, output):
        if code:
            raise Invalid(f'{LABELS[provider]} could not draft. Check your native login, subscription limits, and CLI version.')
        try:
            value = json.loads(output)
        except ValueError:
            raise Invalid(f'{LABELS[provider]} returned an invalid response') from None
        if not isinstance(value, dict):
            raise Invalid(f'{LABELS[provider]} returned an invalid response')
        return value

    @staticmethod
    def _check_gemini_system_policy():
        # Gemini ignores --admin-policy when central policy files exist. Reject
        # that situation instead of assuming our deny-all override took effect.
        roots = ('/etc/gemini-cli/policies', '/Library/Application Support/GeminiCli/policies')
        if os.name == 'nt':
            roots += (str(Path(os.environ.get('PROGRAMDATA', 'C:/ProgramData')) / 'gemini-cli/policies'),)
        if any(Path(root).is_dir() and list(Path(root).glob('*.toml')) for root in roots):
            raise Invalid('Gemini CLI has a system tool policy. Restricted drafting cannot override it safely.')

    def _codex_chat(self, workspace, system, prompt, model, timeout, schema):
        io = _IO(self, 'codex', ['--strict-config', 'app-server', '--stdio'], workspace, timeout)
        try:
            self._rpc_request(io, 'initialize', {'clientInfo': {'name': 'portkeeper', 'version': '1'},
                                               'capabilities': {'experimentalApi': True}}, 1)
            io.send(json.dumps({'jsonrpc': '2.0', 'method': 'initialized'}) + '\n')
            params = {'cwd': str(workspace), 'environments': [], 'dynamicTools': [], 'runtimeWorkspaceRoots': [],
                      'sandbox': 'read-only', 'approvalPolicy': 'never', 'ephemeral': True,
                      'baseInstructions': system, 'developerInstructions': 'Return only a text draft. Tools, files, shell, and external services are unavailable.',
                      'config': {'web_search': 'disabled', 'project_doc_max_bytes': 0}}
            if model:
                params['model'] = model
            thread = self._rpc_request(io, 'thread/start', params, 2)
            if thread.get('modelProvider', thread.get('thread', {}).get('modelProvider')) not in (None, 'openai'):
                raise Invalid('Codex selected a different inference provider. Drafting was stopped.')
            if thread.get('instructionSources'):
                raise Invalid('Codex loaded external instructions. Restricted drafting was stopped.')
            identifier = thread.get('thread', {}).get('id')
            if not isinstance(identifier, str) or not identifier:
                raise Invalid('Codex did not create a restricted drafting session')
            turn = {'threadId': identifier, 'environments': [], 'runtimeWorkspaceRoots': [],
                    'input': [{'type': 'text', 'text': prompt, 'text_elements': []}]}
            if schema is not None:
                turn['outputSchema'] = schema
            self._rpc_request(io, 'turn/start', turn, 3)
            reply = ''
            while True:
                event = io.next()
                self._deny_requests(io, event)
                method, payload = event.get('method'), event.get('params', {})
                if method in ('item/started', 'item/completed'):
                    item = payload.get('item', {})
                    kind = item.get('type')
                    if kind not in ('userMessage', 'agentMessage', 'reasoning', 'contextCompaction'):
                        raise Invalid('Codex attempted a native tool. Text-only drafting was stopped.')
                    if method == 'item/completed' and kind == 'agentMessage' and item.get('phase') in (None, 'final_answer'):
                        reply = item.get('text', '')
                elif method == 'turn/completed':
                    if payload.get('turn', {}).get('status') != 'completed':
                        raise Invalid('Codex could not draft. Check your native login and subscription limits.')
                    return reply
                elif method == 'error' and not payload.get('willRetry', False):
                    raise Invalid('Codex could not draft. Check your native login and subscription limits.')
        finally:
            io.close()

    def _login_running(self, provider):
        record = self._logins.get(provider)
        return bool(record and record['process'].poll() is None)

    def _stop_login(self, record):
        timer = record.get('timer')
        if timer:
            timer.cancel()
        _terminate(record['process'])
        if record.get('fd') is not None:
            os.close(record['fd'])
            record['fd'] = None
        self._forget(record['process'])

    def _expire_login(self, provider, record):
        with self._lock:
            if self._logins.get(provider) is record and record['state'] == 'waiting':
                record['state'] = 'expired'
                self._stop_login(record)

    def _terminal_command(self, provider):
        home = self._home(provider)
        binary = self._binary(provider) or BINARIES[provider]
        args = {'codex': ['login', '--device-auth'], 'claude_code': ['auth', 'login', '--claudeai'], 'gemini': ['--extensions', 'none']}[provider]
        env = self._env(provider)
        # env -i prevents API fallback and custom endpoints in a host terminal too.
        command = shlex.join(['env', '-i', *[key + '=' + value for key, value in env.items()], binary, *args])
        if os.name == 'posix':
            drop = shlex.join(['setpriv', '--reuid=' + str(os.getuid()), '--regid=' + str(os.getgid()), '--clear-groups', '--'])
            return 'if [ "$(id -u)" = "0" ]; then exec ' + drop + ' ' + command + '; else exec ' + command + '; fi'
        return command

    def login(self, provider):
        self._provider(provider)
        with self._lock:
            if provider in self._active:
                raise Invalid('Wait for the current draft to finish before signing in')
            if self._login_running(provider):
                return self.login_status(provider)
            if not self._binary(provider):
                return {**self.status(provider), 'provider': provider, 'status': 'failed'}
            if os.name != 'posix':
                return {'provider': provider, 'status': 'not_started', 'message': 'Run the official sign-in command on the Portkeeper server.',
                        'terminal_command': self._terminal_command(provider)}
            previous = self._logins.pop(provider, None)
            if previous:
                self._stop_login(previous)
            import pty
            import termios
            master, slave = pty.openpty()
            attrs = termios.tcgetattr(slave)
            attrs[3] &= ~termios.ECHO
            termios.tcsetattr(slave, termios.TCSANOW, attrs)
            args = {'codex': ['login', '--device-auth'], 'claude_code': ['auth', 'login', '--claudeai'], 'gemini': ['--extensions', 'none']}[provider]
            try:
                process = self._spawn(provider, args, self._control_cwd(provider), stdin=slave, stdout=slave, stderr=slave)
            except Exception:
                os.close(master)
                raise
            finally:
                os.close(slave)
            os.set_blocking(master, False)
            record = {'process': process, 'fd': master, 'started': time.monotonic(), 'buffer': bytearray(),
                      'state': 'waiting', 'input_sent': False}
            self._logins[provider] = record
            timer = threading.Timer(LOGIN_TIMEOUT, self._expire_login, (provider, record))
            timer.daemon = True
            record['timer'] = timer
            timer.start()
            self._auth_cache.pop(provider, None)
            self._models_cache.pop(provider, None)
        return self.login_status(provider)

    @staticmethod
    def _login_public(provider, text):
        text = _ANSI.sub('', text)
        public = {}
        allowed = {'codex': {'auth.openai.com'}, 'claude_code': {'claude.com', 'claude.ai', 'console.anthropic.com', 'platform.claude.com'},
                   'gemini': {'accounts.google.com'}}[provider]
        for candidate in re.findall(r'https://[^\s<>"\x1b]+', text):
            candidate = candidate.rstrip(').,')
            parsed = urllib.parse.urlsplit(candidate)
            query = urllib.parse.parse_qs(parsed.query)
            if (parsed.hostname in allowed and not parsed.username and not parsed.password and
                    not parsed.fragment and not any(re.search(r'token|secret|password|api.?key', key, re.I) for key in query)):
                if (provider == 'codex' and 'device' in parsed.path) or (provider != 'codex' and ('oauth' in parsed.path or 'authorize' in parsed.path or 'auth' in parsed.path)):
                    public['url'] = candidate
        if provider == 'codex':
            matches = re.findall(r'(?im)^\s*([A-Z0-9]{4,8}-[A-Z0-9]{4,8})\s*$', text)
            if matches:
                public['code'] = matches[-1]
        public['needs_input'] = bool(provider in ('claude_code', 'gemini') and re.search(r'(?i)(?:paste|enter)[^\n]{0,80}(?:code|authorization)', text))
        return public

    def login_status(self, provider):
        self._provider(provider)
        with self._lock:
            record = self._logins.get(provider)
            if not record:
                return {'provider': provider, 'status': 'not_started', **self.status(provider)}
            if record.get('fd') is not None:
                while True:
                    try:
                        data = os.read(record['fd'], 8192)
                    except (BlockingIOError, OSError):
                        break
                    if not data:
                        break
                    record['buffer'].extend(data)
                    if len(record['buffer']) > 65536:
                        record['state'] = 'failed'
                        record['buffer'].clear()
                        self._stop_login(record)
                        break
            if record['state'] == 'waiting' and time.monotonic() - record['started'] > LOGIN_TIMEOUT:
                record['state'] = 'expired'
                self._stop_login(record)
            code = record['process'].poll()
            if code is not None:
                if record['state'] not in ('failed', 'expired'):
                    if record['state'] == 'waiting':
                        self._auth_cache.pop(provider, None)
                    record['state'] = 'complete' if code == 0 and self.status(provider)['authenticated'] else 'failed'
                self._stop_login(record)
            text = record['buffer'].decode('utf-8', 'replace')
            if record['state'] == 'waiting' and record['input_sent']:
                fresh = record['buffer'][record.get('input_offset', len(record['buffer'])):].decode('utf-8', 'replace')
                if self._login_public(provider, fresh).get('needs_input'):
                    # The official CLI may offer a fresh OAuth authorization
                    # attempt after an invalid code. Only a new native prompt
                    # permits another write to the PTY.
                    record['input_sent'] = False
            if provider == 'gemini' and record['state'] == 'waiting':
                credential = self.homes / provider / '.gemini/oauth_creds.json'
                self._check_path(credential)
                if credential.is_file() and time.monotonic() - record.get('auth_probe_at', 0) >= 5:
                    record['auth_probe_at'] = time.monotonic()
                    self._auth_cache.pop(provider, None)
                    if self.status(provider)['authenticated']:
                        record['state'] = 'complete'
                        self._stop_login(record)
            public = self._login_public(provider, text) if record['state'] == 'waiting' else {}
            if record['state'] == 'complete':
                self._models_cache.pop(provider, None)
            if record['input_sent']:
                public['needs_input'] = False
            messages = {'waiting': 'Complete sign-in using the official provider page.', 'complete': 'Native subscription sign-in completed.',
                        'failed': 'Native sign-in did not complete. Try again or run the command on the Portkeeper server.',
                        'expired': 'Sign-in expired. Start again.'}
            states = {'waiting': 'waiting', 'complete': 'connected', 'failed': 'failed', 'expired': 'failed'}
            return {'provider': provider, 'status': states[record['state']], 'message': messages[record['state']],
                    'terminal_command': self._terminal_command(provider), **public}

    def login_input(self, provider, value):
        self._provider(provider)
        if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._~/+=%\-]{1,2048}(?:#[A-Za-z0-9._~/+=%\-]{1,2048})?', value):
            raise Invalid('Paste only the one-time authorization code from the official login page')
        with self._lock:
            state = self.login_status(provider)
            record = self._logins.get(provider)
            if state['status'] != 'waiting' or not state.get('needs_input') or not record or record['input_sent']:
                raise Invalid('The native login is not waiting for an authorization code')
            try:
                os.write(record['fd'], (value + '\n').encode('ascii'))
            except OSError:
                raise Invalid('The native sign-in session has ended') from None
            record['input_sent'] = True
            record['input_offset'] = len(record['buffer'])
        return self.login_status(provider)

    def logout(self, provider):
        self._provider(provider)
        with self._lock:
            if provider in self._active:
                raise Invalid('Wait for the current draft to finish before signing out')
            record = self._logins.pop(provider, None)
            if record:
                self._stop_login(record)
            if self._binary(provider) and provider != 'gemini':
                args = ['logout'] if provider == 'codex' else ['auth', 'logout']
                self._run(provider, args, limit=65536)
            # Remove this isolated home, including all CLI-managed credentials.
            # This also signs Gemini out without reading or exporting its tokens.
            path = self.homes / provider
            self._check_path(path)
            shutil.rmtree(path, ignore_errors=False) if path.exists() else None
            self._auth_cache.pop(provider, None)
            self._models_cache.pop(provider, None)
        return {'provider': provider, 'status': 'not_started', 'installed': bool(self._binary(provider)), 'authenticated': False,
                'billing': 'free_account' if provider == 'gemini' else 'subscription', 'message': 'Signed out. The private native CLI login was removed.'}

    def close(self):
        with self._lock:
            self._closed = True
            for process in tuple(self._processes):
                _terminate(process)
            self._processes.clear()
            for record in self._logins.values():
                self._stop_login(record)
                record['state'] = 'failed'
