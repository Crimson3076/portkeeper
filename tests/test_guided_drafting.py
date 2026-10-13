import copy
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from portkeeper import drafting, packaging
from portkeeper.engine import Engine
from portkeeper.safety import Invalid, checksum
from test_drafting import Server
from test_packaging import Releases


class GuidedDraftTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(Path(self.temp.name) / 'data', github=Releases())
        self.addCleanup(self.engine.close)
        self.engine.save_setting('draft_model_review', True)
        self.engine.save_setting('draft_max_calls', 3)
        self.engine.save_setting('draft_test_builds', False)  # This fixture preserves no release assets.
        self.project = self.engine.create_project({'name': 'Example', 'repo': 'example/port', 'targets': ['windows']})
        root = self.engine.data / 'fixture-source'
        root.mkdir()
        source = root / 'source.zip'
        with zipfile.ZipFile(source, 'w') as archive:
            archive.writestr('port/README.md', '# Example\nChoose your owned game folder on first launch.\nLauncher settings are stored in APPDATA.\n')
        self.capture = self.engine.store.put('captures', {'id': 'a' * 32, 'project_id': self.project['id'],
            'root': str(root), 'version': 'v1', 'commit': self.engine.github.current['commit'],
            'source': {'file': source.name, 'sha256': checksum(source)}})
        self.engine.store.update('projects', self.project['id'], capture_id=self.capture['id'])
        self.good = drafting.extract_json(drafting.SIMPLE_EXAMPLE)
        self.good['upstream']['repository'] = 'example/port'
        self.good['downloads']['windows'] = {'asset_pattern': '^Port-v1-Win64\\.zip$', 'platform': 'windows', 'required_members': ['port.exe']}
        self.good['operations'][1]['exec'] = 'port.exe'
        self.good['outputs']['windows']['expected_files'] = ['port.exe', 'Launch.cmd']
        self.review = {'summary': 'Check the missing settings location.', 'findings': [
            {'severity': 'issue', 'field': 'saves', 'message': 'Describe the actual APPDATA settings location.',
             'evidence': {'path': 'README.md', 'quote': 'Launcher settings are stored in APPDATA.'}}]}

    def run_model(self, replies):
        server = Server([{'choices': [{'message': {'content': reply if isinstance(reply, str) else json.dumps(reply)}}]} for reply in replies])
        self.addCleanup(server.close)
        self.engine.assistant.configure_provider({'provider': 'openai', 'base_url': server.url + '/v1', 'model': 'fixture-model', 'api_key': 'private-test-key'})
        job = self.engine.job(self.project, 'capture', 'v1')
        self.engine.store.update('jobs', job['id'], kind='draft')
        self.engine._draft(self.project['id'], job['id'], '')
        return self.engine.store.get('jobs', job['id']), server

    def test_default_draft_uses_one_model_call_and_skips_model_review(self):
        self.engine.save_setting('draft_model_review', False)
        self.engine.save_setting('draft_max_calls', 1)
        job, server = self.run_model([{'explanation': 'Fixture', 'definition': self.good}])
        self.assertEqual(job['status'], 'ready', job.get('error'))
        self.assertEqual(len(server.requests), 1)
        record = self.engine.draft_record(job['id'])
        self.assertEqual(record['model_review']['status'], 'not_requested')
        self.assertLessEqual(len(record['draft_prompt']['system']) + len(record['draft_prompt']['user']), 24000)

    def test_corrected_draft_gets_fresh_review_and_exportable_record_without_credentials(self):
        bad = copy.deepcopy(self.good)
        bad['outputs']['windows']['expected_files'].append('bin/*.dll')
        job, server = self.run_model([bad, {'explanation': 'PRIVATE_DRAFT_REASON', 'definition': self.good}, self.review])
        self.assertEqual(job['status'], 'ready')
        proposal = self.engine.store.all('proposals')[0]
        self.assertEqual(proposal['status'], 'pending_review')
        self.assertEqual(proposal['model_review']['status'], 'needs_attention')
        self.assertEqual(proposal['model_review']['findings'][0]['evidence_status'], 'quote_found')
        fresh = server.requests[2]['body']['messages']
        self.assertEqual(len(fresh), 2)
        self.assertNotIn('PRIVATE_DRAFT_REASON', json.dumps(fresh))
        record = self.engine.draft_record(job['id'])
        self.assertEqual([item['result'] for item in record['attempts']], ['rejected', 'accepted', 'complete'])
        self.assertEqual(len(record['attempts'][0]['messages']), 1)
        self.assertEqual(len(record['attempts'][1]['messages']), 3)
        self.assertIn('outputs.windows.expected_files', record['attempts'][0]['error'])
        self.assertEqual(record['build_test'], 'not_run')
        self.assertEqual(record['gameplay_test'], 'not_run')
        self.assertEqual(record['context']['release']['source_sha256'], self.capture['source']['sha256'])
        self.assertIn('Port-v1-Win64.zip', record['context']['release']['archive_sha256'])
        self.assertNotIn('private-test-key', json.dumps(record))
        self.assertNotIn(str(self.engine.input_root), json.dumps(record))
        self.assertEqual(self.engine.store.all('artifacts'), [])
        self.assertEqual(self.engine.store.get('projects', self.project['id'])['recipe']['mode'], 'capture')

    def test_unavailable_review_preserves_valid_draft_and_warns(self):
        job, _ = self.run_model([self.good, 'not a JSON review'])
        self.assertEqual(job['status'], 'ready')
        proposal = self.engine.store.all('proposals')[0]
        self.assertEqual(proposal['model_review']['status'], 'unavailable')
        self.assertEqual(self.engine.draft_record(job['id'])['attempts'][-1]['result'], 'unavailable')

    def test_all_failed_attempts_are_retained_without_a_proposal(self):
        bad = copy.deepcopy(self.good)
        bad['outputs']['windows']['expected_files'].append('bin/*.dll')
        job, _ = self.run_model([bad, bad, bad])
        self.assertEqual(job['status'], 'failed')
        self.assertEqual(self.engine.store.all('proposals'), [])
        record = self.engine.draft_record(job['id'])
        self.assertEqual(record['status'], 'failed')
        self.assertEqual(len(record['attempts']), 3)
        self.assertTrue(all(item['result'] == 'rejected' for item in record['attempts']))

    def test_starter_uses_observed_folder_and_does_not_guess_between_downloads(self):
        context = {'project': {'name': 'Demo', 'repository': 'example/demo', 'definition_id': 'demo', 'targets': ['windows']},
                   'release': {'assets': [{'name': 'demo-windows.zip'}],
                       'archive_layouts': {'demo-windows.zip': {'complete': True, 'enclosing_folder': 'demo-windows'}}},
                   'archive_evidence': {'demo-windows.zip': ['demo-windows/Game.exe', 'demo-windows/Play.exe']}}
        starter = drafting.starter_definition(context)
        item = starter['definition']['downloads']['windows']
        self.assertEqual(item['strip_prefix'], 'demo-windows')
        self.assertEqual(item['required_members'], ['Game.exe', 'Play.exe'])
        self.assertEqual(starter['definition']['launch'], {})
        self.assertEqual(starter['definition']['inputs'], [])
        context['release']['assets'].append({'name': 'demo-windows-installer.exe'})
        self.assertEqual(drafting.starter_definition(context)['definition']['downloads'], {})

    def test_review_quotes_are_checked_and_invented_quotes_remain_uncertain(self):
        context = {'material': {'readme': 'Settings use APPDATA.', 'source_excerpts': [
            {'path': 'launcher.py', 'lines': '36: CONFIG_DIR = Path(os.environ["APPDATA"])'}]}}
        review = {'summary': 'Inspect settings.', 'findings': [
            {'severity': 'issue', 'field': 'saves', 'message': 'Uses APPDATA.', 'evidence': {'path': 'launcher.py', 'quote': 'CONFIG_DIR = Path(os.environ["APPDATA"])'}},
            {'severity': 'issue', 'field': 'saves', 'message': 'Invented.', 'evidence': {'path': 'README.md', 'quote': 'Portable mode is supported.'}}]}
        checked = drafting.read_review(json.dumps(review), context)
        self.assertEqual(checked['findings'][0]['evidence_status'], 'quote_found')
        self.assertEqual(checked['findings'][1]['severity'], 'uncertain')
        self.assertEqual(checked['findings'][1]['evidence_status'], 'unconfirmed')


class ReviewCandidateEvidenceTests(unittest.TestCase):
    def test_candidate_quote_required_for_issue_and_duplicate_quotes_are_collapsed(self):
        context = {'material': {'readme': 'Settings use APPDATA.'}}
        candidate = {'saves': 'Settings stay inside the package.'}
        finding = {'severity': 'issue', 'field': 'saves', 'message': 'Check the settings claim.', 'candidate_quote': 'Settings stay inside the package.', 'evidence': {'path': 'README.md', 'quote': 'Settings use APPDATA.'}}
        response = {'summary': 'Inspect settings.', 'findings': [finding, finding]}
        result = drafting.read_review(json.dumps(response), context, candidate)
        self.assertEqual(len(result['findings']), 1)
        self.assertEqual(result['findings'][0]['severity'], 'issue')
        finding['candidate_quote'] = 'An invented candidate statement.'
        result = drafting.read_review(json.dumps(response), context, candidate)
        self.assertEqual(result['findings'][0]['severity'], 'uncertain')
        self.assertEqual(result['findings'][0]['candidate_evidence_status'], 'unconfirmed')


class MatchingPreparationCandidateTests(unittest.TestCase):
    def context(self):
        context = {'project': {'name': 'Fixture', 'definition_id': 'fixture', 'repository': 'ZeldaWWHDRecomp/ZeldaWWHDRecomp', 'targets': ['linux', 'windows']}, 'release': {'assets': []}, 'material': {'readme': ''}}
        context['game_file_preparation'] = drafting.preparation_guidance(context, packaging.bundled())
        return context

    def test_matching_catalog_starter_exposes_supported_preparation(self):
        context = self.context()
        starter = drafting.starter_definition(context)['definition']
        checked = packaging.validate_definition(starter)
        self.assertTrue(any(op.get('tool') == 'wwhd_extract' for op in checked['operations']))
        self.assertTrue(checked['inputs'])
        drafting.check_preparation_choice(checked, context)
        checked['operations'] = [op for op in checked['operations'] if not op.get('tool')]
        with self.assertRaisesRegex(Invalid, 'matching catalog candidate'):
            drafting.check_preparation_choice(checked, context)

    def test_preparation_off_does_not_insert_catalog_extraction(self):
        context = self.context()
        context['game_file_preparation']['enabled'] = False
        starter = drafting.starter_definition(context)['definition']
        self.assertEqual(starter['inputs'], [])
        drafting.check_preparation_choice(starter, context)
