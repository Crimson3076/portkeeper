"""Optional checks against the shipped CLIs; no login, turns, or network use."""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from portkeeper.subscription_agents import SubscriptionAgents, _IO


@unittest.skipUnless(os.environ.get('PORTKEEPER_TEST_SUBSCRIPTION_CLIS') == '1', 'Set PORTKEEPER_TEST_SUBSCRIPTION_CLIS=1 inside the built image')
class NativeAgentContractTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.agents = SubscriptionAgents(Path(self.temp.name) / 'data')
        self.addCleanup(self.agents.close)

    def test_shipped_clis_support_controls_and_have_no_ambient_login(self):
        for provider in ('codex', 'claude_code', 'gemini'):
            with self.subTest(provider=provider):
                state = self.agents.status(provider)
                self.assertTrue(state['installed'], state)
                self.assertTrue(state.get('drafting_supported'), state)
                self.assertFalse(state['authenticated'], state)

    def test_codex_accepts_managed_config_and_thread_without_environment(self):
        cwd = self.agents._control_cwd('codex')
        io = _IO(self.agents, 'codex', ['--strict-config', 'app-server', '--stdio'], cwd, 20)
        try:
            self.agents._rpc_request(io, 'initialize', {'clientInfo': {'name': 'portkeeper', 'version': '1'},
                                                     'capabilities': {'experimentalApi': True}}, 1)
            io.send(json.dumps({'jsonrpc': '2.0', 'method': 'initialized'}) + '\n')
            response = self.agents._rpc_request(io, 'thread/start', {
                'cwd': str(cwd), 'environments': [], 'dynamicTools': [], 'runtimeWorkspaceRoots': [],
                'sandbox': 'read-only', 'approvalPolicy': 'never', 'ephemeral': True,
                'baseInstructions': 'Return JSON only.', 'developerInstructions': 'Text-only drafting.',
                'config': {'web_search': 'disabled', 'project_doc_max_bytes': 0}}, 2)
            self.assertTrue(response.get('thread', {}).get('id'))
            self.assertEqual(response['thread']['modelProvider'], 'openai')
            self.assertEqual(response.get('instructionSources'), [])
            # Intentionally never sends turn/start: this check uses no model allowance.
        finally:
            io.close()

    def test_gemini_native_policy_denies_reads_shell_mcp_and_unknown_tools(self):
        bundle = Path(shutil.which('gemini')).resolve().parent
        core = sorted(bundle.glob('core-*.js'))[0]
        policy = self.agents._home('gemini') / '.gemini/policies/portkeeper.toml'
        script = '''
import {createPolicyEngineConfig,PolicyEngine} from CORE;
const p=POLICY;
const c=await createPolicyEngineConfig({policyPaths:[p],adminPolicyPaths:[p]},'default',DEFAULTS,false);
const e=new PolicyEngine(c);
for(const name of ['read_file','run_shell_command','write_file','web_fetch','mcp__x__tool','unknown_future_tool']) {
  const r=await e.check({name,args:{}});
  if(r.decision!=='deny') throw Error('Native policy allowed '+name);
}
'''.replace('CORE', json.dumps(str(core))).replace('POLICY', json.dumps(str(policy))).replace('DEFAULTS', json.dumps(str(bundle / 'policies')))
        result = subprocess.run([shutil.which('node'), '--input-type=module', '-e', script],
                                env=self.agents._env('gemini'), capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr[-4000:])
