import copy
import json
import unittest
from unittest.mock import patch

from portkeeper import trail
import test_investigation


class TrailTests(unittest.TestCase):
    """Research trail: ordered steps plus verified / inferred / unverified claims, built from the saved record."""
    setUp = test_investigation.InvestigationTests.setUp

    def investigate(self, responses, max_calls=3):
        self.engine.assistant.configure_provider({'provider': 'openai', 'base_url': 'http://localhost:12345/v1', 'model': 'fixture'})
        job = self.engine.job(self.project, 'capture', 'v1')
        self.engine.store.update('jobs', job['id'], kind='investigation', investigation_max_calls=max_calls, prepare_game_files=True, instruction='')
        with patch.object(self.engine, '_draft_material', return_value=(self.capture, copy.deepcopy(self.context))), patch.object(self.engine.assistant, 'chat', side_effect=responses):
            self.engine._investigate(self.project, job['id'])
        return job['id']

    def plan_reply(self, plan=None):
        return json.dumps({'action': 'plan', 'plan': plan or self.plan})

    def test_successful_run_lists_steps_in_order_with_tagged_sources(self):
        job_id = self.investigate([self.plan_reply()])
        result = self.engine.research_trail(job_id)
        self.assertEqual(result['outcome'], 'ready')
        kinds = [step['type'] for step in result['steps']]
        self.assertEqual(kinds[0], 'read')
        self.assertEqual(kinds[-1], 'model_request')
        request = result['steps'][-1]
        self.assertEqual((request['provider'], request['model'], request['attempt']), ('openai', 'fixture', 1))
        self.assertIsInstance(request['seconds'], float)
        claims = {claim['claim']: claim for claim in result['claims']}
        launch = claims['Windows launch path']
        self.assertEqual((launch['value'], launch['tag']), ('Game.exe', 'verified'))
        self.assertIn('game-windows.zip', launch['source'])
        self.assertEqual(claims['Windows platform support']['tag'], 'verified')
        self.assertEqual(claims['Required game data']['tag'], 'verified')
        self.assertIn('README.md', claims['Required game data']['source'])

    def test_corrected_run_shows_the_correction_and_inferred_claims(self):
        invalid = {**self.plan, 'outcome': 'supported'}
        inferred = copy.deepcopy(self.plan)
        inferred['evidence'] = []
        job_id = self.investigate([self.plan_reply(invalid), self.plan_reply(inferred)])
        result = self.engine.research_trail(job_id)
        requests = [step for step in result['steps'] if step['type'] == 'model_request']
        self.assertEqual([step['attempt'] for step in requests], [1, 2])
        corrections = [step for step in result['steps'] if step['type'] == 'correction']
        self.assertEqual(len(corrections), 1)
        self.assertIn('supported', corrections[0]['detail'])
        self.assertEqual(result['corrections'], 1)
        order = [step['type'] for step in result['steps']]
        self.assertLess(order.index('correction'), len(order) - 1)
        claims = {claim['claim']: claim for claim in result['claims']}
        self.assertEqual(claims['Required game data']['tag'], 'inferred')

    def test_uncertainty_flagged_by_the_model_is_unverified(self):
        plan = copy.deepcopy(self.plan)
        plan['uncertainties'] = ['The launch path could not be confirmed from the README.']
        result = trail.build({'kind': 'investigation', 'status': 'ready', 'attempts': [], 'plan': plan, 'model': {}})
        claims = {claim['claim']: claim for claim in result['claims']}
        self.assertEqual(claims['Windows launch path']['tag'], 'unverified')

    def test_failed_run_keeps_every_step_and_the_error(self):
        request = json.dumps({'action': 'request_evidence', 'requests': [{'kind': 'source', 'path': 'README.md'}]})
        job_id = self.investigate([request, 'not json', 'still not json'], max_calls=3)
        job = self.engine.store.get('jobs', job_id)
        self.assertEqual(job['status'], 'ready')  # exhausted budget returns needs_evidence rather than raising
        result = self.engine.research_trail(job_id)
        self.assertEqual(result['outcome'], 'needs_evidence')
        self.assertEqual([step['attempt'] for step in result['steps'] if step['type'] == 'model_request'], [1, 2, 3])
        self.assertTrue(any(step['type'] == 'read' and 'README.md' in step['label'] for step in result['steps']))

    def test_hard_failure_without_a_plan_still_renders(self):
        record = {'kind': 'investigation', 'status': 'failed', 'error': 'Preserved source changed', 'attempts': [], 'model': {'provider': 'ollama', 'drafting_model': 'qwen'}}
        result = trail.build(record)
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(result['failure'], 'Preserved source changed')
        self.assertEqual(result['claims'], [])

    def test_draft_attempts_tests_and_review_are_listed(self):
        record = {'kind': 'draft', 'status': 'ready', 'model': {'provider': 'ollama', 'drafting_model': 'qwen'},
                  'attempts': [{'kind': 'draft', 'attempt': 1, 'result': 'rejected', 'error': 'launch path changed', 'seconds': 4.2, 'at': 't1'},
                               {'kind': 'draft', 'attempt': 2, 'result': 'accepted', 'seconds': 3.0, 'at': 't2'},
                               {'kind': 'test_build', 'round': 1, 'feedback': 'Test build passed'},
                               {'kind': 'review', 'result': 'complete', 'seconds': 1.0}]}
        result = trail.build(record, plan=self.plan)
        self.assertEqual([step['type'] for step in result['steps']], ['model_request', 'correction', 'model_request', 'test_build', 'model_request'])
        self.assertEqual(result['corrections'], 1)
        self.assertIn('launch path changed', result['steps'][1]['detail'])

    def test_trail_is_available_over_http_for_drafting_records(self):
        job_id = self.investigate([self.plan_reply()])
        self.assertEqual(self.engine.research_trail(job_id)['job_id'], job_id)
        self.engine.store.update('jobs', job_id, draft_record_available=False)
        with self.assertRaises(Exception):
            self.engine.research_trail(job_id)


if __name__ == '__main__':
    unittest.main()
