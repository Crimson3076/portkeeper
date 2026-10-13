import copy
import json
import tempfile
import threading
import unittest
from unittest.mock import patch
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from portkeeper.assistant import RecipeAssistant, redact_text
from portkeeper.safety import Invalid


class AssistantTests(unittest.TestCase):
    def test_subscription_provider_keeps_api_and_local_settings_separate(self):
        with tempfile.TemporaryDirectory() as directory:
            assistant = RecipeAssistant(Path(directory))
            with patch.object(assistant.agents, 'status', return_value={'installed': True, 'authenticated': True, 'billing': 'subscription'}):
                assistant.configure_provider({'provider': 'openai', 'base_url': 'https://api.openai.com/v1', 'model': 'api-model', 'api_key': 'private-key'})
                assistant.configure_provider({'provider': 'codex', 'model': 'codex-model'})
                assistant.configure_provider({'provider': 'claude_code', 'model': 'sonnet'})
                public = assistant.configure_provider({'provider': 'codex'})
                self.assertTrue(public['configured'])
                self.assertEqual(public['agent_models'], {'codex': '', 'claude_code': 'sonnet'})
                self.assertEqual(public['cloud_model'], 'api-model')
                self.assertNotIn('private-key', json.dumps(public))
                with patch.object(assistant, '_post') as http, patch.object(assistant.agents, 'chat', return_value='{}') as chat:
                    self.assertEqual(assistant.chat('system', [{'role': 'user', 'content': 'public material'}]), '{}')
                    http.assert_not_called()
                    self.assertEqual(chat.call_args.args[0], 'codex')
                    self.assertNotIn('private-key', str(chat.call_args))

    def test_subscription_provider_requires_native_sign_in_without_paid_fallback(self):
        with tempfile.TemporaryDirectory() as directory:
            assistant = RecipeAssistant(Path(directory))
            with patch.object(assistant.agents, 'status', return_value={'installed': True, 'authenticated': False, 'billing': 'subscription'}):
                public = assistant.configure_provider({'provider': 'claude_code', 'model': 'sonnet'})
                self.assertFalse(public['configured'])
                with patch.object(assistant, '_post') as http:
                    with self.assertRaisesRegex(Invalid, 'Save a model connection'):
                        assistant.test()
                    http.assert_not_called()

    def test_ollama_limit_explains_empty_final_answer_without_using_reasoning(self):
        with tempfile.TemporaryDirectory() as directory:
            assistant = RecipeAssistant(Path(directory))
            response = {'message': {'content': '', 'thinking': 'not a definition'}, 'done_reason': 'length', 'prompt_eval_count': 29325}
            with patch.object(assistant, '_post', return_value=response):
                with self.assertRaisesRegex(Invalid, 'context or output limit.*29325'):
                    assistant.chat('system', [], {'provider': 'ollama', 'endpoint': 'http://localhost:11434', 'model': 'fixture'})
            response['done_reason'] = 'stop'
            with patch.object(assistant, '_post', return_value=response):
                with self.assertRaisesRegex(Invalid, 'reasoning but no final answer'):
                    assistant.chat('system', [], {'provider': 'ollama', 'endpoint': 'http://localhost:11434', 'model': 'fixture'})

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.assistant = RecipeAssistant(Path(self.temp.name))
        self.recipe = {'version': 1, 'mode': 'assemble', 'payload': {'kind': 'source'},
                       'expected_files': ['game'], 'inputs': [{'path': 'owned/private.z64', 'destination': 'game', 'sha256': 'a' * 64}]}
        self.project = {'name': 'Example', 'repository': 'owner/project', 'recipe': self.recipe, 'targets': ['linux'],
                        'token': 'hidden-project-token', 'rom': b'ROM bytes'}
        self.state = {'received': None, 'reply': None, 'error': False}
        state = self.state
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                state['received'] = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if state['error']:
                    self.send_error(500)
                    return
                if state['reply'] is None:
                    context = json.loads(state['received']['messages'][1]['content'])
                    proposal = {'recipe': context['project']['recipe'], 'explanation': 'Kept verified inputs.'}
                else:
                    proposal = state['reply']
                data = json.dumps({'message': {'content': json.dumps(proposal)}}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self._close)
        self.url = f'http://127.0.0.1:{self.server.server_port}'

    def _close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def enable(self):
        self.assistant.configure(self.url, 'qwen:latest')

    def test_private_lan_endpoints_are_allowed_but_public_and_metadata_addresses_are_not(self):
        from portkeeper.assistant import _endpoint
        for endpoint in ['http://192.168.0.189:11434', 'http://10.0.0.2:11434', 'http://172.16.1.2:11434', 'http://[fd00::2]:11434']:
            self.assertEqual(_endpoint(endpoint), endpoint)
        for endpoint in ['http://8.8.8.8:11434', 'http://169.254.169.254', 'http://0.0.0.0:11434', 'http://192.0.2.1:11434']:
            with self.assertRaises(Invalid):
                _endpoint(endpoint)

    def test_not_configured_and_remote_endpoints_are_rejected(self):
        with self.assertRaisesRegex(Invalid, 'Configure a local Ollama'):
            self.assistant.draft(self.project)
        for endpoint in ('https://external.test', 'http://8.8.8.8:11434', 'http://user:secret@localhost:11434',
                         'http://localhost:11434/api', 'http://localhost:bad'):
            with self.subTest(endpoint=endpoint), self.assertRaises(Invalid):
                self.assistant.configure(endpoint, 'model')
        self.enable()
        self.assertTrue(self.assistant.public_config()['configured'])
        self.assistant.configure()
        self.assertFalse(self.assistant.public_config()['configured'])

    def test_proposal_has_no_execution_and_prompt_omits_credentials_and_rom_inputs(self):
        self.enable()
        (self.assistant.path.parent / 'romm.json').write_text(json.dumps({'url': 'https://server.test', 'token': 'unlabelledsecret'}))
        self.project['recipe']['token'] = 'hidden-recipe-token'
        logs = [f'line {index}' for index in range(200)]
        logs += ['unlabelledsecret', 'owned/private.z64 failed: Authorization: Bearer rmm_logsecret', 'password=verysecret token=othersecret']
        result = self.assistant.draft(self.project, logs)
        self.assertEqual(result['recipe'], {key: value for key, value in self.recipe.items() if key != 'token'})
        self.assertTrue(result['requires_review'])
        self.assertFalse(result['executed'])
        received = json.dumps(self.state['received'])
        for secret in ('hidden-project-token', 'ROM bytes', 'owned/private.z64', 'rmm_logsecret', 'verysecret', 'othersecret', 'unlabelledsecret', 'hidden-recipe-token'):
            self.assertNotIn(secret, received)
        self.assertNotIn('line 0"', received)
        self.assertIn('private-input-0', received)

    def test_invalid_commands_mode_changes_and_input_changes_are_rejected(self):
        self.enable()
        masked = {**self.recipe, 'inputs': [{**self.recipe['inputs'][0], 'path': 'private-input-0'}]}
        fixtures = [{**masked, 'mode': 'docker', 'image': 'builder:latest', 'command': ['run']},
                    {**masked, 'steps': ['shell']}, {**masked, 'expected_files': ['../escape']},
                    {**masked, 'inputs': []}, {**masked, 'payload': {'kind': 'release'}}]
        for recipe in fixtures:
            self.state['reply'] = {'recipe': recipe, 'explanation': 'proposed'}
            with self.subTest(recipe=recipe), self.assertRaises(Invalid):
                self.assistant.draft(self.project)

    def override_project(self):
        self.recipe['target_overrides'] = {
            'windows': {'inputs': [{'path': 'owned/windows-private.z64', 'destination': 'game.exe', 'sha256': 'b' * 64}],
                        'expected_files': ['game.exe']},
            'handheld': {'inputs': [], 'expected_files': ['game']}}
        self.project['targets'] = ['linux', 'windows', 'handheld']

    def test_override_input_paths_are_masked_everywhere_and_restored_exactly(self):
        self.enable()
        self.override_project()
        original = copy.deepcopy(self.recipe)
        paths = [self.recipe['inputs'][0]['path'], self.recipe['target_overrides']['windows']['inputs'][0]['path']]
        self.recipe['notes'] = 'Review ' + paths[1]
        original['notes'] = self.recipe['notes']
        result = self.assistant.draft(self.project, logs=['Failed to copy ' + path for path in paths],
                                      instruction='Keep ' + paths[1] + ' unchanged')
        received = json.dumps(self.state['received'])
        for path in paths:
            self.assertNotIn(path, received)
        context = json.loads(self.state['received']['messages'][1]['content'])
        masked = context['project']['recipe']
        self.assertEqual(masked['inputs'][0]['path'], 'private-input-0')
        self.assertEqual(masked['target_overrides']['windows']['inputs'][0]['path'], 'private-input-windows-0')
        self.assertEqual(masked['target_overrides']['handheld']['inputs'], [])
        self.assertEqual(result['recipe']['inputs'], original['inputs'])
        self.assertEqual(result['recipe']['target_overrides'], original['target_overrides'])
        self.assertFalse(result['executed'])
        self.assertEqual(self.recipe, original)

    def test_override_input_changes_and_added_or_removed_groups_are_rejected(self):
        self.enable()
        self.override_project()
        self.assistant.draft(self.project)
        masked = json.loads(self.state['received']['messages'][1]['content'])['project']['recipe']
        fixtures = []
        for key, value in (('sha256', 'c' * 64), ('destination', 'different.exe'), ('path', 'different-private-file')):
            changed = copy.deepcopy(masked)
            changed['target_overrides']['windows']['inputs'][0][key] = value
            fixtures.append((key, changed))
        changed = copy.deepcopy(masked)
        changed['target_overrides']['windows'].pop('inputs')
        fixtures.append(('removed-input-group', changed))
        changed = copy.deepcopy(masked)
        changed['target_overrides'].pop('handheld')
        fixtures.append(('removed-empty-group', changed))
        changed = copy.deepcopy(masked)
        changed['target_overrides']['linux'] = {'inputs': []}
        fixtures.append(('added-empty-group', changed))
        changed = copy.deepcopy(masked)
        changed['target_overrides']['windows']['inputs'] = []
        fixtures.append(('removed-input', changed))
        for label, changed in fixtures:
            self.state['reply'] = {'recipe': changed, 'explanation': 'Unsafe input change'}
            with self.subTest(label=label), self.assertRaisesRegex(Invalid, 'private inputs'):
                self.assistant.draft(self.project)

    def test_input_path_masking_preserves_identical_package_destination(self):
        self.enable()
        self.recipe['inputs'][0]['path'] = self.recipe['inputs'][0]['destination']
        self.override_project()
        self.recipe['target_overrides']['windows']['inputs'][0]['path'] = 'game.exe'
        result = self.assistant.draft(self.project, logs=['Copy game and game.exe from supplied inputs'])
        self.assertEqual(result['recipe'], self.recipe)

    def test_service_failure_is_clear_and_secret_free(self):
        self.enable()
        self.state['error'] = True
        with self.assertRaisesRegex(Invalid, 'Local Ollama'):
            self.assistant.draft(self.project)
        self.state['error'] = False
        self.state['reply'] = {'unexpected': 'rmm_secrethere'}
        with self.assertRaisesRegex(Invalid, 'invalid JSON'):
            self.assistant.draft(self.project)

    def test_redaction(self):
        result = redact_text('rmm_ab123 password=secret token="private" Authorization: Bearer unknown https://user:pass@host')
        for value in ('rmm_ab123', 'secret', 'private', 'unknown', 'user:pass'):
            self.assertNotIn(value, result)


if __name__ == '__main__':
    unittest.main()
