import json
import os
import stat
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from portkeeper.safety import Invalid
from portkeeper.subscription_agents import MAX_OUTPUT, SubscriptionAgents


class SubscriptionAgentsTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.calls = self.root / 'calls.jsonl'
        self.binaries = {}
        self.agents = SubscriptionAgents(self.root / 'data')
        self.addCleanup(self.agents.close)
        self.which = patch('portkeeper.subscription_agents.shutil.which', side_effect=lambda name: self.binaries.get(name))
        self.which.start()
        self.addCleanup(self.which.stop)

    def fake(self, provider, behavior='ok'):
        name = {'codex': 'codex', 'claude_code': 'claude', 'gemini': 'gemini'}[provider]
        path = self.root / name
        path.write_text(f'#!{sys.executable}\n' + f'PROVIDER={provider!r}\nBEHAVIOR={behavior!r}\nLOG={str(self.calls)!r}\n' + r'''
import json, os, pathlib, sys, time
args=sys.argv[1:]
home=pathlib.Path(os.environ['HOME'])
auth=home/'fixture-login'
with open(LOG,'a') as log:
    log.write(json.dumps({'provider':PROVIDER,'args':args,'env':dict(os.environ),'cwd':os.getcwd()})+'\n')
def emit(value):
    print(json.dumps(value),flush=True)
def result(request,value):
    emit({'jsonrpc':'2.0','id':request['id'],'result':value})
if '--version' in args:
    print({'codex':'codex 0.162.1','claude_code':'2.1.287 (Claude Code)','gemini':'0.63.0'}[PROVIDER]);sys.exit()
if '--help' in args:
    print('app-server --safe-mode --restricted --tools --strict-mcp-config --setting-sources --no-session-persistence --disable-slash-commands --acp --admin-policy --policy --extensions --output-format');sys.exit()
if args[:2] == ['login','status']:
    print('Logged in using an API key: sk-secret-never-public' if BEHAVIOR=='api' else ('Logged in using ChatGPT' if auth.exists() else 'Not logged in'))
    sys.exit(0 if auth.exists() or BEHAVIOR=='api' else 1)
if args[:2] == ['auth','status']:
    emit({'loggedIn':auth.exists() or BEHAVIOR=='api','authMethod':'api_key' if BEHAVIOR=='api' else 'claude.ai','apiProvider':'firstParty','email':'private@example.test','token':'never-public'})
    sys.exit(0 if auth.exists() or BEHAVIOR=='api' else 1)
if args[:1] == ['logout'] or args[:2] == ['auth','logout']:
    auth.unlink(missing_ok=True);sys.exit()
if args[:1] == ['login'] or args[:2] == ['auth','login']:
    if PROVIDER=='codex':
        print('\x1b[32mhttps://auth.openai.com/codex/device\x1b[0m\n  ABCD-ABCDE\naccess_token=never-public',flush=True)
        time.sleep(60)
    else:
        print('\x1b]8;;https://claude.com/cai/oauth/authorize?state=public-state\x1b\\Sign in\x1b]8;;\x1b\\\nhttps://claude.com/cai/oauth/authorize?state=public-state\nPaste code here if prompted >\naccess_token=never-public',flush=True)
        code=sys.stdin.readline().strip()
        auth.write_text('opaque-native-state')
        print('token='+code,flush=True)
    sys.exit()
if PROVIDER=='gemini' and args==['--extensions','none']:
    print('https://accounts.google.com/o/oauth2/v2/auth?state=public\nEnter the authorization code:',flush=True)
    code=sys.stdin.readline().strip()
    auth.write_text('opaque-native-state')
    (home/'.gemini/oauth_creds.json').write_text('opaque-provider-token')
    time.sleep(60)
    sys.exit()
if 'app-server' in args or '--acp' in args:
    for line in sys.stdin:
        request=json.loads(line)
        with open(LOG,'a') as log: log.write(json.dumps({'rpc':request,'provider':PROVIDER})+'\n')
        if 'id' not in request: continue
        method=request.get('method')
        if method=='initialize': result(request,{})
        elif method=='authenticate':
            if BEHAVIOR=='auth_failure': emit({'id':request['id'],'error':{'message':'access_token=never-public'}})
            else: result(request,{})
        elif method=='model/list': result(request,{'data':[{'model':'fixture-frontier'},{'model':'invalid; command'}]})
        elif method=='thread/start': result(request,{'modelProvider':'openai','instructionSources':[],'thread':{'id':'draft-thread'}})
        elif method=='turn/start':
            result(request,{'turn':{'id':'draft-turn','status':'inProgress'}})
            if BEHAVIOR=='callback': emit({'jsonrpc':'2.0','id':77,'method':'item/tool/call','params':{'tool':'shell'}})
            elif BEHAVIOR=='native_tool': emit({'method':'item/started','params':{'item':{'type':'commandExecution','command':'never run this'}}})
            else:
                emit({'method':'item/completed','params':{'item':{'type':'agentMessage','phase':'final_answer','text':'{"draft":"safe"}'}}})
                emit({'method':'turn/completed','params':{'turn':{'status':'completed'}}})
    sys.exit()
if BEHAVIOR=='timeout': time.sleep(60)
if BEHAVIOR=='overflow': print('x'*(2*1024*1024+1),flush=True);sys.exit()
if BEHAVIOR=='failure': print('access_token=never-public sk-private',file=sys.stderr);sys.exit(2)
if BEHAVIOR=='bad_json': print('private-token invalid JSON');sys.exit()
prompt=sys.stdin.read()
with open(LOG,'a') as log: log.write(json.dumps({'prompt':prompt,'provider':PROVIDER,'files':[x.name for x in pathlib.Path.cwd().iterdir()]})+'\n')
if PROVIDER=='claude_code': emit({'type':'result','is_error':False,'result':'{"draft":"safe"}'})
else: emit({'response':'{"draft":"safe"}'})
''', encoding='utf-8')
        path.chmod(0o755)
        self.binaries[name] = str(path)
        return path

    def enable(self, provider):
        home = self.agents._home(provider)
        (home / 'fixture-login').write_text('opaque-native-state')
        if provider == 'gemini':
            (home / '.gemini/oauth_creds.json').write_text('unreadable-to-adapter-opaque-token')
        self.agents._auth_cache.pop(provider, None)

    def records(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_missing_binary_is_a_normal_status_and_never_connected(self):
        state = self.agents.status('codex')
        self.assertFalse(state['installed'])
        self.assertFalse(state['authenticated'])
        self.assertEqual(self.agents.login('codex')['status'], 'failed')
        with self.assertRaises(Invalid):
            self.agents.chat('codex', 'Draft text', [])

    def test_status_is_native_cached_single_flight_and_never_returns_account_or_tokens(self):
        self.fake('claude_code')
        self.enable('claude_code')
        results = []
        threads = [threading.Thread(target=lambda: results.append(self.agents.status('claude_code'))) for _ in range(5)]
        for thread in threads: thread.start()
        for thread in threads: thread.join()
        self.assertTrue(all(state['authenticated'] for state in results))
        calls = [entry for entry in self.records() if entry.get('args', [])[:2] == ['auth', 'status']]
        self.assertEqual(len(calls), 1)
        self.assertNotIn('private@example', json.dumps(results))
        self.assertNotIn('never-public', json.dumps(results))

    def test_api_login_is_rejected_for_both_native_subscription_providers(self):
        for provider in ('codex', 'claude_code'):
            self.fake(provider, 'api')
            state = self.agents.status(provider)
            self.assertFalse(state['authenticated'])
            self.assertEqual(state['billing'], 'api_key')
            self.assertNotIn('sk-secret', json.dumps(state))
            with self.assertRaises(Invalid):
                self.agents.chat(provider, 'Draft', [])

    def test_all_clis_strip_paid_api_endpoint_and_execution_environment_and_isolate_files(self):
        poisoned = {'ANTHROPIC_API_KEY':'private', 'ANTHROPIC_AUTH_TOKEN':'private', 'ANTHROPIC_BASE_URL':'https://evil.test',
                    'OPENAI_API_KEY':'private', 'OPENAI_BASE_URL':'https://evil.test', 'CODEX_ACCESS_TOKEN':'private',
                    'GEMINI_API_KEY':'private', 'GOOGLE_CLOUD_ACCESS_TOKEN':'private', 'GOOGLE_GENAI_USE_GCA':'true',
                    'GOOGLE_APPLICATION_CREDENTIALS':'/private/key', 'NODE_OPTIONS':'--require=/private/injection',
                    'HTTPS_PROXY':'https://private:secret@evil.test', 'GOOGLE_GEMINI_BASE_URL':'https://evil.test'}
        with patch.dict(os.environ, poisoned):
            for provider in ('codex', 'claude_code', 'gemini'):
                self.fake(provider)
                self.enable(provider)
                self.assertEqual(self.agents.chat(provider, 'Draft from masked text', [{'role':'user','content':'public-context'}]), '{"draft":"safe"}')
        for entry in self.records():
            if 'env' not in entry: continue
            self.assertFalse(set(poisoned).intersection(entry['env']))
            self.assertIn('/secrets/agents/', entry['env']['HOME'])
            self.assertIn('/agent-workspaces/', entry['cwd'])
        for provider in ('codex', 'claude_code', 'gemini'):
            home = self.agents.homes / provider
            self.assertEqual(stat.S_IMODE(home.stat().st_mode), 0o700)
        self.assertFalse(any(path.name.startswith(('codex-', 'claude_code-', 'gemini-')) for path in self.agents.workspaces.iterdir()))

    def test_claude_disables_native_tools_mcp_skills_hooks_and_persistence(self):
        self.fake('claude_code')
        self.enable('claude_code')
        self.agents.chat('claude_code', 'Draft', [], model='opus')
        args = next(item['args'] for item in self.records() if '--print' in item.get('args', []))
        for flag in ('--safe-mode','--restricted','--strict-mcp-config','--disable-slash-commands','--no-session-persistence','--no-chrome'):
            self.assertIn(flag, args)
        self.assertEqual(args[args.index('--tools')+1], '')
        self.assertEqual(args[args.index('--setting-sources')+1], '')
        self.assertEqual(json.loads(args[args.index('--mcp-config')+1]), {'mcpServers':{}})
        self.assertEqual(args[args.index('--disallowedTools')+1], 'mcp__*')

    def test_codex_disables_environment_at_thread_and_turn_and_uses_native_catalog(self):
        self.fake('codex')
        self.enable('codex')
        schema = {'type':'object','properties':{'draft':{'type':'string'}},'required':['draft'],'additionalProperties':False}
        self.agents.chat('codex', 'Draft', [], response_schema=schema)
        requests = [entry['rpc'] for entry in self.records() if 'rpc' in entry]
        thread = next(item['params'] for item in requests if item.get('method') == 'thread/start')
        turn = next(item['params'] for item in requests if item.get('method') == 'turn/start')
        self.assertEqual(thread['environments'], [])
        self.assertEqual(turn['environments'], [])
        self.assertEqual(thread['dynamicTools'], [])
        self.assertEqual(thread['sandbox'], 'read-only')
        self.assertEqual(turn['outputSchema'], schema)
        self.assertEqual(self.agents.models('codex')['models'], ['fixture-frontier'])
        self.assertEqual(self.agents.models('codex')['models'], ['fixture-frontier'])
        requests = [entry['rpc'] for entry in self.records() if 'rpc' in entry]
        self.assertEqual(sum(item.get('method') == 'model/list' for item in requests), 1)

    def test_codex_rejects_native_tools_and_server_callbacks(self):
        for behavior in ('callback', 'native_tool'):
            with self.subTest(behavior=behavior):
                self.fake('codex', behavior)
                self.enable('codex')
                with self.assertRaisesRegex(Invalid, 'tool|callback'):
                    self.agents.chat('codex', 'Draft', [])
                self.assertFalse(self.agents._processes)
        callback_errors = [entry['rpc'] for entry in self.records() if entry.get('rpc', {}).get('id') == 77]
        # The subprocess may be killed before consuming the rejection; no tool
        # implementation is ever called or passed into the native protocol.
        self.assertTrue(all('error' in item for item in callback_errors))

    def test_gemini_native_auth_is_verified_not_inferred_from_credential_file(self):
        self.fake('gemini', 'auth_failure')
        self.enable('gemini')
        self.assertFalse(self.agents.status('gemini')['authenticated'])
        requests = [entry['rpc'] for entry in self.records() if 'rpc' in entry]
        self.assertEqual([item['method'] for item in requests], ['initialize', 'authenticate'])
        self.assertEqual(requests[1]['params']['methodId'], 'oauth-personal')
        self.assertNotIn('unreadable-to-adapter', json.dumps(requests))

    def test_gemini_disables_extensions_with_admin_deny_all_policy(self):
        self.fake('gemini')
        self.enable('gemini')
        self.agents.chat('gemini', 'Draft', [], model='auto')
        args = next(item['args'] for item in self.records() if '--prompt' in item.get('args', []))
        self.assertEqual(args[args.index('--extensions')+1], 'none')
        self.assertNotIn('--model', args)
        policy = Path(args[args.index('--admin-policy')+1])
        self.assertIn('toolName = "*"', policy.read_text())
        self.assertIn('decision = "deny"', policy.read_text())
        self.assertIn('priority = 999', policy.read_text())
        self.assertEqual(stat.S_IMODE(policy.stat().st_mode), 0o600)

    def test_gemini_supplied_text_cannot_trigger_implicit_file_mentions(self):
        text = 'Read @prompt.json and @/data/secrets/example; source has @decorators.'
        prompt = self.agents._prompt(text, [{'role': 'user', 'content': text}], None, 'gemini')
        self.assertNotIn('@', prompt)
        self.assertEqual(json.loads(prompt)['system'], text)
        self.assertEqual(json.loads(prompt)['messages'][0]['content'], text)

    def test_timeout_overflow_failed_and_malformed_responses_are_bounded_and_secret_free(self):
        for behavior, pattern, timeout in [('timeout','timed out',.1),('overflow','output limit',3),('failure','could not draft',3),('bad_json','invalid response',3)]:
            with self.subTest(behavior=behavior):
                self.fake('claude_code', behavior)
                self.enable('claude_code')
                with self.assertRaisesRegex(Invalid, pattern) as error:
                    self.agents.chat('claude_code', 'Draft', [], timeout=timeout)
                self.assertNotIn('private', str(error.exception))
                self.assertFalse(self.agents._processes)

    def test_untrusted_model_input_never_reaches_process_arguments(self):
        for value in ('--dangerously-skip-permissions','model;cat /private','https://endpoint/model','model\n--tools default'):
            with self.assertRaises(Invalid):
                self.agents.chat('codex', 'Draft', [], model=value)
        self.assertFalse(self.records())

    def wait_login(self, provider, desired=None):
        deadline = time.monotonic()+3
        state = self.agents.login_status(provider)
        while time.monotonic() < deadline:
            if (desired and state['status']==desired) or (not desired and state.get('url')): return state
            time.sleep(.01)
            state = self.agents.login_status(provider)
        self.fail(f'Native login did not reach expected status: {state}')

    def test_device_login_is_idempotent_sanitized_and_close_cleans_processes(self):
        self.fake('codex')
        self.agents.login('codex')
        state = self.wait_login('codex')
        process = self.agents._logins['codex']['process']
        self.assertEqual(state['url'], 'https://auth.openai.com/codex/device')
        self.assertEqual(state['code'], 'ABCD-ABCDE')
        self.assertNotIn('never-public', json.dumps(state))
        self.agents.login('codex')
        self.assertIs(process, self.agents._logins['codex']['process'])
        with self.assertRaises(Invalid): self.agents.login_input('codex', 'arbitrary-shell-command')
        with self.assertRaises(Invalid): self.agents.chat('codex','Draft',[])
        self.agents.close()
        self.assertIsNotNone(process.poll())
        self.assertFalse(self.agents._processes)

    def test_claude_login_accepts_only_one_time_code_never_returns_it_and_logout_removes_home(self):
        self.fake('claude_code')
        self.agents.login('claude_code')
        state = self.wait_login('claude_code')
        self.assertIn('claude.com/cai/oauth/authorize', state['url'])
        self.assertTrue(state['needs_input'])
        self.assertIn('--claudeai', state['terminal_command'])
        with self.assertRaises(Invalid): self.agents.login_input('claude_code', 'code\ncat /private')
        self.agents.login_input('claude_code','one-time-code#state')
        state = self.wait_login('claude_code','connected')
        self.assertNotIn('one-time-code', json.dumps(state))
        self.assertNotIn('never-public', json.dumps(state))
        self.assertTrue(self.agents.status('claude_code')['authenticated'])
        self.agents.logout('claude_code')
        self.assertFalse((self.agents.homes/'claude_code').exists())
        self.assertFalse(self.agents.status('claude_code')['authenticated'])

    def test_native_google_code_format_and_completion_without_waiting_for_tui_exit(self):
        self.fake('gemini')
        self.agents.login('gemini')
        self.assertTrue(self.wait_login('gemini')['needs_input'])
        self.agents.login_input('gemini','4/0AbC_def-gh')
        state = self.wait_login('gemini','connected')
        self.assertNotIn('4/0AbC',json.dumps(state))
        self.assertFalse(self.agents._processes)

    def test_native_retry_uses_latest_authorization_link(self):
        text = 'https://accounts.google.com/o/oauth2/v2/auth?state=first\nEnter authorization code:\n'
        text += 'https://accounts.google.com/o/oauth2/v2/auth?state=retry\nEnter authorization code:'
        public = self.agents._login_public('gemini', text)
        self.assertTrue(public['url'].endswith('state=retry'))
        self.assertTrue(public['needs_input'])

    def test_abandoned_login_expires_without_polling_and_releases_pty(self):
        self.fake('codex')
        with patch('portkeeper.subscription_agents.LOGIN_TIMEOUT', .1):
            self.agents.login('codex')
            record = self.agents._logins['codex']
            deadline = time.monotonic()+3
            while record['process'].poll() is None and time.monotonic()<deadline:
                time.sleep(.01)
            self.assertIsNotNone(record['process'].poll())
            self.assertIsNone(record['fd'])
            self.assertFalse(self.agents._processes)
            self.assertEqual(record['state'],'expired')
            self.assertEqual(self.agents.login_status('codex')['status'],'failed')

    def test_symlink_homes_are_rejected_and_login_logout_are_guarded_during_drafting(self):
        self.fake('codex')
        self.agents._dir(self.agents.homes)
        (self.agents.homes/'codex').symlink_to(self.root/'unrelated', target_is_directory=True)
        self.assertFalse(self.agents.status('codex')['authenticated'])
        with self.assertRaises(Invalid): self.agents.logout('codex')
        self.agents._active.add('claude_code')
        for operation in (self.agents.login,self.agents.logout):
            with self.assertRaisesRegex(Invalid,'Wait'): operation('claude_code')


if __name__ == '__main__':
    unittest.main()
