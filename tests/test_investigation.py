import copy
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from portkeeper import drafting, investigation
from portkeeper.engine import Engine
from portkeeper.safety import Invalid, checksum
from test_drafting import Server
from test_packaging import Releases


README = 'This is a playable Windows port. Run Game.exe. It includes all data required to play. Settings are stored beside the executable.'


class InvestigationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.engine = Engine(self.root / 'data', input_root=self.root / 'private-roms', github=Releases())
        self.addCleanup(self.engine.close)
        self.project = self.engine.create_project({'name': 'Fixture', 'repo': 'example/port', 'targets': ['windows']})
        source = self.root / 'source.zip'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('port/README.md', README)
            archive.writestr('port/src/launch.cpp', 'const char* game_dir = "data";\nlaunch_game();')
            archive.writestr('port/src/binary.dat', b'\x00private-looking binary')
        runtime = self.root / 'game-windows.zip'
        with zipfile.ZipFile(runtime, 'w') as archive:
            archive.writestr('Game.exe', b'public runtime fixture')
        self.capture = self.engine.store.put('captures', {'id': 'a' * 32, 'root': str(self.root), 'version': 'v1', 'commit': 'b' * 40,
            'project_id': self.project['id'], 'source': {'file': source.name, 'sha256': checksum(source)},
            'assets': [{'name': runtime.name, 'file': runtime.name, 'sha256': checksum(runtime), 'size': runtime.stat().st_size}]})
        self.project = self.engine.store.update('projects', self.project['id'], capture_id=self.capture['id'])
        self.context = drafting.build_context(self.project, self.capture, self.capture['assets'], {runtime.name: ['Game.exe']}, {}, 20_000)
        self.plan = {'outcome': 'ready', 'summary': 'Package the Windows runtime; data is already included.',
            'targets': [{'target': 'windows', 'method': 'release', 'asset': runtime.name, 'launch': 'Game.exe'}],
            'game_files': {'needed_to_play': False, 'needed_for_packaging': False, 'requirements': 'Release includes all data'},
            'steps': [{'capability': 'unpack_download', 'purpose': 'Extract runtime', 'targets': ['windows']}, {'capability': 'launcher', 'purpose': 'Launch Game.exe'}],
            'evidence': [{'source': 'e2', 'quote': README, 'supports': 'Runtime, launch and included data'}], 'uncertainties': []}

    def reader(self):
        return investigation.Evidence(self.engine, self.project, self.capture, self.context)

    def runtime_definition(self):
        definition = drafting.extract_json(drafting.SIMPLE_EXAMPLE)
        definition['upstream']['repository'] = self.project['repo']
        definition['downloads']['windows'] = {'asset_pattern': '^game-windows\\.zip$', 'platform': 'windows', 'format': 'zip', 'required_members': ['Game.exe']}
        definition['first_run'] = {'mode': 'ready', 'summary': 'Release includes game data.'}
        definition['operations'][1]['exec'] = 'Game.exe'
        definition['outputs']['windows']['expected_files'] = ['Game.exe', 'Launch.cmd']
        return definition

    def test_source_requests_are_bounded_read_only_and_integrity_checked(self):
        reader = self.reader()
        source = reader.request({'kind': 'source', 'path': 'src/launch.cpp'})
        self.assertIn('game_dir', source['text'])
        matches = reader.request({'kind': 'search', 'query': 'game_dir'})
        self.assertIn('src/launch.cpp', matches['text'])
        for request in [{'kind': 'shell', 'path': 'anything'}, {'kind': 'source', 'path': '../secret'},
                        {'kind': 'source', 'path': 'src/binary.dat'}, {'kind': 'source', 'path': 'README.md', 'offset': True}]:
            with self.subTest(request=request), self.assertRaises(Invalid):reader.request(request)
        self.capture['source']['sha256'] = 'f' * 64
        with self.assertRaisesRegex(Invalid, 'changed'):reader.request({'kind': 'source', 'path': 'README.md'})

    def test_archive_requests_do_not_execute_or_allow_external_urls(self):
        reader = self.reader()
        entry = reader.request({'kind': 'archive', 'asset': 'game-windows.zip'})
        self.assertIn('Game.exe', entry['text'])
        self.assertTrue(self.context['release']['archive_layouts']['game-windows.zip']['complete'])
        with self.assertRaises(Invalid):reader.request({'kind': 'archive', 'asset': 'http://localhost/private'})

    def test_valid_plan_and_unmatched_quotes_unknown_tools_missing_platforms(self):
        reader = self.reader()
        self.assertTrue(investigation.check_plan(self.plan, self.project, self.capture, reader)['can_draft'])
        for mutate in [lambda p: p['evidence'][0].update(quote='A fabricated quote'),
                       lambda p: p['steps'].append({'capability': 'shell', 'purpose': 'compile arbitrary source'}),
                       lambda p: p.update(targets=[]), lambda p: p['targets'][0].update(launch='missing.exe')]:
            bad = copy.deepcopy(self.plan);mutate(bad)
            result = investigation.check_plan(bad, self.project, self.capture, reader)
            self.assertFalse(result['can_draft']);self.assertEqual(result['outcome'], 'needs_evidence')

    def test_build_helper_and_source_compilation_cannot_become_ready_packages(self):
        capture = {**self.capture, 'assets': [{'name': 'qemu-irix'}]}
        plan = copy.deepcopy(self.plan);plan['targets'][0].update(asset='qemu-irix', launch='qemu-irix')
        self.assertFalse(investigation.check_plan(plan, self.project, capture, self.reader())['can_draft'])
        plan['outcome'] = 'unsupported';plan['targets'][0]['method'] = 'source_build'
        result = investigation.check_plan(plan, self.project, capture, self.reader())
        self.assertEqual(result['outcome'], 'unsupported')
        self.assertTrue(any('reviewed build adapter' in problem for problem in result['capability_problems']))

    def test_local_ollama_requests_evidence_then_returns_a_plan_without_tools(self):
        server = Server([{'message': {'content': json.dumps({'action': 'request_evidence', 'requests': [{'kind': 'source', 'path': 'src/launch.cpp'}]})}},
                         {'message': {'content': json.dumps({'action': 'plan', 'plan': self.plan})}}])
        self.addCleanup(server.close)
        self.engine.assistant.configure_provider({'provider': 'ollama', 'endpoint': server.url, 'model': 'local-fixture', 'context_tokens': 32768})
        events = []
        config = self.engine.assistant.public_config() | self.engine.assistant._config()
        plan, evidence, _ = investigation.run(self.engine, self.project, self.capture, self.context, config, 3, lambda _: None, events.append)
        self.assertTrue(plan['can_draft'])
        self.assertEqual(len(server.requests), 2)
        second = server.requests[1]['body']
        self.assertEqual(second['format'], 'json')
        self.assertEqual(len(second['messages']), 2)
        self.assertIn('game_dir', second['messages'][1]['content'])
        self.assertNotIn(str(self.root), json.dumps(server.requests))
        self.assertEqual([event['result'] for event in events], ['evidence_requested', 'ready'])

    def test_native_codex_plan_accepts_canonical_and_nested_structured_payloads(self):
        bodies = [self.plan, {'plan': self.plan}, {'action': 'plan', 'plan': self.plan},
                  {'action': 'plan', 'payload_json': json.dumps(self.plan)}]
        config = {'provider': 'codex', 'drafting_model': 'Codex default', 'agent_models': {'codex': ''}}
        for body in bodies:
            with self.subTest(body_fields=list(body)):
                response = json.dumps({'action': 'plan', 'payload_json': json.dumps(body)})
                events = []
                with patch.object(self.engine.assistant.agents, 'chat', return_value=response) as chat:
                    result, _, _ = investigation.run(self.engine, self.project, self.capture, copy.deepcopy(self.context), config, 1, lambda _: None, events.append)
                self.assertTrue(result['can_draft'], result)
                self.assertEqual(chat.call_count, 1)
                self.assertEqual(chat.call_args.kwargs['response_schema'], investigation.SCHEMA)
                self.assertEqual(events[0]['response'], response)

    def test_natural_flattened_and_bare_plan_variants_keep_the_same_facts(self):
        replies = [{'action': 'plan', 'plan': self.plan}, {'action': 'plan', 'plan': json.dumps(self.plan)},
                   {'action': 'plan', **self.plan}, self.plan]
        for reply in replies:
            action, body = investigation.read_response(json.dumps(reply))
            self.assertEqual(action, 'plan')
            self.assertEqual(body, self.plan)

    def test_nested_structured_evidence_requests_reach_the_second_call(self):
        request = {'kind': 'source', 'path': 'src/launch.cpp'}
        first = json.dumps({'action': 'request_evidence', 'payload_json': json.dumps({'action': 'request_evidence', 'requests': [request]})})
        second = json.dumps({'action': 'plan', 'payload_json': json.dumps(self.plan)})
        events = []
        config = {'provider': 'codex', 'drafting_model': 'Codex default'}
        with patch.object(self.engine.assistant.agents, 'chat', side_effect=[first, second]) as chat:
            result, _, _ = investigation.run(self.engine, self.project, self.capture, copy.deepcopy(self.context), config, 2, lambda _: None, events.append)
        self.assertTrue(result['can_draft'], result)
        self.assertEqual(chat.call_count, 2)
        self.assertIn('game_dir', chat.call_args.args[2][0]['content'])
        self.assertEqual([event['result'] for event in events], ['evidence_requested', 'ready'])

    def test_transport_normalization_does_not_fix_invalid_outcomes_or_evidence(self):
        for change in [{'outcome': 'supported'}, {'outcome': None}]:
            plan = {**self.plan, **change}
            _, body = investigation.read_response(json.dumps({'action': 'plan', 'payload_json': json.dumps({'plan': plan})}))
            with self.assertRaisesRegex(Invalid, 'Plan outcome.*received'):
                investigation.check_plan(body, self.project, self.capture, self.reader())
        _, body = investigation.read_response(json.dumps({'action': 'plan', 'plan': {'summary': 'No outcome supplied'}}))
        with self.assertRaisesRegex(Invalid, 'missing.*summary'):
            investigation.check_plan(body, self.project, self.capture, self.reader())
        invalid = copy.deepcopy(self.plan);invalid['evidence'][0]['quote'] = 'fabricated evidence'
        _, body = investigation.read_response(json.dumps({'action': 'plan', 'payload_json': json.dumps({'plan': invalid})}))
        self.assertFalse(investigation.check_plan(body, self.project, self.capture, self.reader())['can_draft'])

    def test_conflicting_malformed_and_unbounded_reply_wrappers_are_rejected(self):
        nested = {'plan': self.plan}
        for _ in range(6):nested = {'action': 'plan', 'payload_json': json.dumps(nested)}
        replies = [{'action': 'plan', 'payload_json': 'not JSON'},
                   {'action': 'plan', 'plan': self.plan, 'payload_json': json.dumps(self.plan)},
                   {'action': 'plan', 'payload_json': json.dumps({'action': 'request_evidence', 'requests': []})},
                   {'action': 'plan', 'requests': []}, {'action': 'execute', 'plan': self.plan}, nested]
        for reply in replies:
            with self.subTest(reply_fields=list(reply)), self.assertRaises(Invalid):
                investigation.read_response(json.dumps(reply))

    def test_invalid_outcome_feedback_is_actionable_and_preserves_the_raw_reply(self):
        invalid = {**self.plan, 'outcome': 'supported'}
        responses = [json.dumps({'action': 'plan', 'payload_json': json.dumps(invalid)}),
                     json.dumps({'action': 'plan', 'payload_json': json.dumps(self.plan)})]
        events = []
        with patch.object(self.engine.assistant, 'chat', side_effect=responses) as chat:
            result, _, _ = investigation.run(self.engine, self.project, self.capture, copy.deepcopy(self.context), {'drafting_model': 'fixture'}, 2, lambda _: None, events.append)
        self.assertTrue(result['can_draft'])
        self.assertIn("received 'supported'", chat.call_args.args[1][0]['content'])
        self.assertEqual(events[0]['result'], 'rejected')
        self.assertEqual(events[0]['response'], responses[0])

    def test_limit_returns_insufficient_evidence_without_an_extra_model_call(self):
        request = json.dumps({'action': 'request_evidence', 'requests': [{'kind': 'source', 'path': 'README.md'}]})
        with patch.object(self.engine.assistant, 'chat', return_value=request) as chat:
            plan, _, _ = investigation.run(self.engine, self.project, self.capture, self.context, {'drafting_model': 'fixture'}, 1, lambda _: None, lambda _: None)
        self.assertEqual(chat.call_count, 1);self.assertFalse(plan['can_draft']);self.assertEqual(plan['outcome'], 'needs_evidence')

    def test_investigation_is_persisted_and_cannot_be_applied_as_a_definition(self):
        self.engine.assistant.configure_provider({'provider': 'openai', 'base_url': 'http://localhost:12345/v1', 'model': 'fixture'})
        job = self.engine.job(self.project, 'capture', 'v1')
        self.engine.store.update('jobs', job['id'], kind='investigation', investigation_max_calls=3, prepare_game_files=True, instruction='')
        with patch.object(self.engine, '_draft_material', return_value=(self.capture, self.context)), patch.object(self.engine.assistant, 'chat', return_value=json.dumps({'action': 'plan', 'plan': self.plan})):
            self.engine._investigate(self.project, job['id'])
        finished = self.engine.store.get('jobs', job['id']);self.assertEqual(finished['status'], 'ready', finished.get('error'))
        report = self.engine.store.get('proposals', finished['investigation_id'])
        self.assertTrue(self.engine.reviewed_investigation(self.project, report['id'])['plan']['can_draft'])
        self.assertEqual(self.engine.draft_record(job['id'])['plan']['outcome'], 'ready')
        with self.assertRaisesRegex(Invalid, 'not executable'):self.engine.apply_proposal(report['id'])
        changed = self.engine.store.update('projects', self.project['id'], targets=['linux'])
        with self.assertRaisesRegex(Invalid, 'changed'):self.engine.reviewed_investigation(changed, report['id'])

    def test_plan_cannot_silently_ignore_requested_preparation(self):
        plan = copy.deepcopy(self.plan);plan['game_files'].update(needed_to_play=True, needed_for_packaging=False)
        self.assertFalse(investigation.check_plan(plan, self.project, self.capture, self.reader())['can_draft'])
        self.context['game_file_preparation']['enabled'] = False
        self.assertTrue(investigation.check_plan(plan, self.project, self.capture, self.reader())['can_draft'])

    def test_guided_definition_must_use_planned_runtime_and_capabilities(self):
        definition = drafting.extract_json(drafting.SIMPLE_EXAMPLE)
        plan = {**self.plan, 'can_draft': True}
        with self.assertRaisesRegex(Invalid, 'runtime asset'):investigation.check_definition(definition, plan, self.capture)

    def test_ready_plan_cannot_claim_generic_or_missing_game_preparation(self):
        for steps in [[{'capability': 'copy', 'purpose': 'prepare owned assets'}],
                      [{'capability': 'tool', 'purpose': 'extract without specifying an adapter'}]]:
            plan = copy.deepcopy(self.plan)
            plan['game_files'].update(needed_to_play=True, needed_for_packaging=True)
            plan['steps'] = steps
            self.assertFalse(investigation.check_plan(plan, self.project, self.capture, self.reader())['can_draft'])

    def test_archive_listing_failure_is_not_a_complete_manifest(self):
        self.context['archive_evidence']['unpreserved.zip'] = ['(could not inspect archive)']
        self.reader().request({'kind': 'archive', 'asset': 'unpreserved.zip'})
        self.assertFalse(self.context['release']['archive_layouts']['unpreserved.zip']['complete'])

    def test_supported_plan_generates_a_reviewable_definition_and_disposable_test(self):
        self.engine.assistant.configure_provider({'provider': 'openai', 'base_url': 'http://localhost:12345/v1', 'model': 'fixture'})
        job = self.engine.job(self.project, 'capture', 'v1')
        self.engine.store.update('jobs', job['id'], kind='investigation', investigation_max_calls=3, prepare_game_files=True, instruction='')
        with patch.object(self.engine, '_draft_material', return_value=(self.capture, copy.deepcopy(self.context))), patch.object(self.engine.assistant, 'chat', return_value=json.dumps({'action': 'plan', 'plan': self.plan})):
            self.engine._investigate(self.project, job['id'])
        report_id = self.engine.store.get('jobs', job['id'])['investigation_id']
        definition = self.runtime_definition()
        with patch.object(self.engine.executor, 'submit'):
            draft_job = self.engine.queue_draft(self.project['id'], investigation_id=report_id, max_calls=1, test_inputs=False)['job']
        with patch.object(self.engine, '_draft_material', return_value=(self.capture, copy.deepcopy(self.context))), patch.object(self.engine.assistant, 'chat', return_value=json.dumps({'definition': definition})) as chat:
            self.engine._draft(self.project['id'], draft_job['id'], '')
        finished = self.engine.store.get('jobs', draft_job['id'])
        self.assertEqual(finished['status'], 'ready', finished.get('error'))
        proposal = self.engine.store.get('proposals', finished['proposal_id'])
        self.assertEqual(proposal['investigation_id'], report_id)
        self.assertEqual(proposal['model_tests'][0]['status'], 'passed')
        self.assertIn('reviewed_plan', chat.call_args.args[1][0]['content'])
        self.assertEqual(self.engine.store.all('artifacts'), [])
        self.assertEqual(self.engine.store.get('projects', self.project['id'])['packaging']['runner'], 'capture')

    def test_source_only_project_retains_public_runtime_and_guided_test_uses_same_bytes(self):
        self.engine.assistant.configure_provider({'provider': 'openai', 'base_url': 'http://localhost:12345/v1', 'model': 'fixture'})
        runtime = self.root / 'game-windows.zip'
        url = 'https://github.com/example/port/releases/download/v1/game-windows.zip'
        self.engine.github.current.update(commit=self.capture['commit'], assets=[{'name': runtime.name, 'size': runtime.stat().st_size, 'browser_download_url': url}])
        self.engine.github.payloads[url] = runtime.read_bytes()
        self.engine.store.update('captures', self.capture['id'], assets=[])
        job = self.engine.job(self.project, 'capture', 'v1')
        self.engine.store.update('jobs', job['id'], kind='investigation', investigation_max_calls=3, prepare_game_files=True, instruction='')
        with patch.object(self.engine.assistant, 'chat', return_value=json.dumps({'action': 'plan', 'plan': self.plan})):
            self.engine._investigate(self.project, job['id'])
        finished = self.engine.store.get('jobs', job['id'])
        self.assertEqual(finished['status'], 'ready', finished.get('error'))
        record = self.engine.draft_record(job['id'])
        self.assertTrue(record['plan']['can_draft'], record['plan'])
        self.assertEqual(record['research_capture']['assets'][0]['sha256'], checksum(runtime))
        self.assertEqual(self.engine.store.get('captures', self.capture['id'])['assets'], [])
        with patch.object(self.engine.executor, 'submit'):
            draft = self.engine.queue_draft(self.project['id'], investigation_id=finished['investigation_id'], max_calls=1)['job']
        self.engine.github.payloads[url] = b'changed upstream after investigation'
        with patch.object(self.engine, '_draft_material', side_effect=AssertionError('Guided drafting must reuse saved evidence')), patch.object(self.engine.assistant, 'chat', return_value=json.dumps({'definition': self.runtime_definition()})):
            self.engine._draft(self.project['id'], draft['id'], '')
        result = self.engine.store.get('jobs', draft['id'])
        self.assertEqual(result['status'], 'ready', result.get('error'))
        proposal = self.engine.store.get('proposals', result['proposal_id'])
        self.assertEqual(proposal['model_tests'][0]['status'], 'passed')

    def test_verbose_plans_are_compacted_for_local_model_context(self):
        context = copy.deepcopy(self.context)
        plan = copy.deepcopy(self.plan)
        plan['summary'] = 'S' * 3000
        plan['steps'] = [{'capability': 'unpack_download', 'purpose': 'P' * 1500} for _ in range(30)]
        plan['uncertainties'] = ['U' * 1500 for _ in range(20)]
        context['investigation_plan'] = plan
        system, user = drafting.prompt_parts(context, {}, '')
        self.assertLessEqual(len(system) + len(user), 24000)
        self.assertIn('game-windows.zip', user)
        self.assertIn('unpack_download', user)
        self.assertEqual(len(context['investigation_plan']['steps'][0]['purpose']), 1500)

    def test_unsupported_report_cannot_request_a_draft(self):
        self.engine.assistant.configure_provider({'provider': 'openai', 'base_url': 'http://localhost:12345/v1', 'model': 'fixture'})
        job = self.engine.job(self.project, 'capture', 'v1')
        self.engine.store.update('jobs', job['id'], kind='investigation', investigation_max_calls=1, prepare_game_files=True, instruction='')
        unsupported = copy.deepcopy(self.plan);unsupported.update(outcome='unsupported', summary='Source compilation requires a reviewed adapter.')
        unsupported['targets'][0]['method'] = 'source_build'
        with patch.object(self.engine, '_draft_material', return_value=(self.capture, self.context)), patch.object(self.engine.assistant, 'chat', return_value=json.dumps({'action':'plan','plan':unsupported})):
            self.engine._investigate(self.project, job['id'])
        report_id = self.engine.store.get('jobs', job['id'])['investigation_id']
        with self.assertRaisesRegex(Invalid, 'supported packaging workflow'):
            self.engine.queue_draft(self.project['id'], investigation_id=report_id)

