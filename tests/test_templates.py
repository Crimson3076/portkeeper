import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from portkeeper import drafting, packaging, templates
from portkeeper.engine import Engine
from portkeeper.safety import Invalid
from test_packaging import Releases, definition

LINUX, WINDOWS = 'Newgame-2.0-Linux.zip', 'Newgame-2.0-Windows.zip'


def reference(**changes):
    """A saved definition with nothing game-specific in it: unpack both archives and write launchers."""
    value = {
        'schema': 2, 'id': 'older-port', 'revision': 3, 'name': 'Older Port', 'package_prefix': 'older-port',
        'runner': 'operations', 'runner_version': 1, 'upstream': {'repository': 'example/older'}, 'targets': ['linux', 'windows'],
        'downloads': {
            'linux': {'asset_pattern': r'^Older-1\.0-Linux\.zip$', 'platform': 'linux', 'format': 'zip', 'required_members': ['older.AppImage']},
            'windows': {'asset_pattern': r'^Older-1\.0-Windows\.zip$', 'platform': 'windows', 'format': 'zip', 'required_members': ['Older.exe']}},
        'inputs': [], 'source_checks': [],
        'operations': [
            {'op': 'unpack_download', 'download': 'linux', 'to': '$package', 'targets': ['linux']},
            {'op': 'unpack_download', 'download': 'windows', 'to': '$package', 'targets': ['windows']},
            {'op': 'launcher', 'targets': ['linux'], 'path': 'Launch.sh', 'template': 'posix', 'chmod_exec': True, 'exec': 'older.AppImage'},
            {'op': 'launcher', 'targets': ['windows'], 'path': 'Launch.cmd', 'template': 'windows_cmd', 'exec': 'Older.exe'}],
        'outputs': {'linux': {'expected_files': ['older.AppImage', 'Launch.sh']}, 'windows': {'expected_files': ['Older.exe', 'Launch.cmd']}},
        'launch': {'linux': {'command': 'Launch.sh'}, 'windows': {'command': 'Launch.cmd'}},
        'first_run': {'mode': 'ready', 'summary': 'Older Port starts immediately.'}, 'history': [], 'verified_releases': []}
    value.update(changes)
    return packaging.validate_definition(value)


def context(readme='A new game.', linux=None, windows=None):
    linux = ['newgame.AppImage', 'config.yml'] if linux is None else linux
    windows = ['Newgame.exe', 'config.yml'] if windows is None else windows
    return {'project': {'name': 'Newgame', 'repository': 'example/newgame', 'targets': ['linux', 'windows'], 'definition_id': 'newgame'},
            'release': {'version': 'v2.0', 'assets': [{'name': LINUX}, {'name': WINDOWS}],
                        'archive_layouts': {LINUX: {'complete': True}, WINDOWS: {'complete': True}},
                        'archive_contents': {LINUX: linux, WINDOWS: windows}},
            'archive_evidence': {LINUX: linux, WINDOWS: windows},
            'material': {'readme': readme}, 'game_file_preparation': {}}


def refs(*definitions, origin='your approved definitions'):
    return [{'origin': origin, 'definition': item} for item in definitions]


class TemplateFitTests(unittest.TestCase):
    def test_close_match_builds_a_validated_candidate_with_new_file_names(self):
        matched = templates.fit(context(), templates.select(context(), refs(reference())))
        built = matched['definition']
        self.assertEqual(built['id'], 'newgame')
        self.assertEqual(built['upstream'], {'repository': 'example/newgame'})
        self.assertEqual(built['downloads']['linux']['asset_pattern'], r'^Newgame\-2\.0\-Linux\.zip$')
        self.assertEqual([op['exec'] for op in built['operations'] if op['op'] == 'launcher'], ['newgame.AppImage', 'Newgame.exe'])
        self.assertEqual(built['outputs']['windows']['expected_files'], ['Newgame.exe', 'Launch.cmd'])
        self.assertEqual(built['verified_releases'], [])
        self.assertEqual(matched['template']['id'], 'older-port')
        self.assertNotIn('Older', json.dumps(built))
        packaging.validate_definition(built)

    def test_game_name_in_an_input_label_or_staged_path_blocks_a_template_fit(self):
        named = reference()
        named['operations'].insert(2, {'op': 'touch', 'targets': ['linux'], 'path': '$package/older-port.flag'})
        self.assertIsNone(templates.fit(context(), refs(named)))
        self.assertTrue(templates._mentions([{'label': 'Older Port ROM'}], {'Older Port'}))
        self.assertFalse(templates._mentions([{'label': 'A ROM'}], {'Older Port'}))

    def test_archive_with_a_different_layout_is_not_a_fit(self):
        wrapped = context(windows=['Newgame.exe'])
        wrapped['release']['archive_layouts'][WINDOWS]['enclosing_folder'] = 'Newgame'
        wrapped['archive_evidence'][WINDOWS] = ['Newgame/Newgame.exe']
        self.assertIsNone(templates.fit(wrapped, refs(reference())))
        self.assertIsNone(templates.fit(context(windows=['A.exe', 'B.exe']), refs(reference())))

    def test_no_match_for_other_platforms_or_game_data(self):
        self.assertEqual(templates.select(context(), refs(reference(targets=['linux'], downloads={'linux': reference()['downloads']['linux']},
            operations=[reference()['operations'][0], reference()['operations'][2]], outputs={'linux': reference()['outputs']['linux']},
            launch={'linux': {'command': 'Launch.sh'}}))), [])
        self.assertIsNone(templates.fit(context(readme='Place your ROM next to the executable.'), refs(reference())))
        self.assertIsNone(templates.fit(context(), refs(definition())))  # hashes and source checks are game-specific

    def test_rom_requiring_reference_is_an_example_not_a_template(self):
        close = definition(targets=['linux', 'windows'])
        selected = templates.select(context('Needs a ROM.'), refs(close))
        self.assertIsNone(templates.fit(context('Needs a ROM.'), selected))
        self.assertEqual(templates.summarize(selected)[0]['id'], 'example-port')

    def test_examples_are_bounded(self):
        big = reference(operations=reference()['operations'] + [{'op': 'write_text', 'path': '$package/n%d.txt' % i, 'text': 'x' * 900} for i in range(30)])
        material = templates.example_material(templates.select(context(), refs(big, reference(id='b'), reference(id='c'))))
        self.assertLessEqual(len(material), templates.MAX_EXAMPLES)
        for item in material:
            self.assertLessEqual(len(json.dumps(item['definition'])), templates.EXAMPLE_CHARS)
        ctx = context(); ctx['examples'] = material
        _, user = drafting.prompt_parts(ctx, {})
        self.assertIn('similar_approved_definitions', user)
        self.assertLessEqual(len(drafting.SYSTEM) + len(user), 24000)


class ShareTests(unittest.TestCase):
    def test_export_and_import_round_trip(self):
        original = reference(verified_releases=[{'release': 'v1', 'assets': {}}])
        text = json.dumps(templates.export_definition(original))
        imported = templates.read_import(text, 'Example/Older')
        self.assertEqual({**imported, 'verified_releases': []}, {**original, 'verified_releases': []})
        self.assertEqual(imported['verified_releases'], [])
        self.assertEqual(templates.read_import(json.dumps(original), 'example/older')['id'], 'older-port')

    def test_invalid_imports_are_refused(self):
        bad = reference(); bad['operations'][0]['download'] = 'missing'
        for text in ('', 'not json', '[]', json.dumps({'kind': templates.EXPORT_KIND, 'format': 2, 'definition': reference()}),
                     json.dumps(templates.export_definition(bad)), json.dumps({**reference(), 'surprise': 1}), 'x' * (templates.MAX_IMPORT_BYTES + 1)):
            with self.assertRaises(Invalid, msg=text[:30]):
                templates.read_import(text)
        with self.assertRaisesRegex(Invalid, 'not example/other'):
            templates.read_import(json.dumps(reference()), 'example/other')


class EngineTemplateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.engine = Engine(Path(self.temp.name) / 'data', github=Releases())
        self.addCleanup(self.engine.close)
        older = self.engine.create_project({'name': 'Older', 'repo': 'example/older', 'targets': ['linux', 'windows']})
        self.engine.store.update('projects', older['id'], packaging=reference())
        self.project = self.engine.create_project({'name': 'Newgame', 'repo': 'example/newgame', 'targets': ['linux', 'windows']})
        self.engine.assistant.configure_provider({'provider': 'codex'})

    def run_draft(self, ctx, **job):
        job_row = self.engine.job(self.project, 'capture', 'v2.0')
        self.engine.store.update('jobs', job_row['id'], kind='draft', **job)
        agent = self.engine.assistant.agents
        good = json.dumps({'explanation': 'x', 'definition': templates.fit(context(), refs(reference()))['definition']})
        with patch.object(self.engine, '_draft_material', side_effect=lambda project, budget: ({'id': 'c' * 32, 'version': 'v2.0'}, self._with_examples(project, ctx))), \
             patch.object(agent, 'status', return_value={'installed': True, 'authenticated': True, 'billing': 'subscription'}), \
             patch.object(agent, 'chat', return_value=good) as chat:
            self.engine._draft(self.project['id'], job_row['id'], '')
        return self.engine.store.get('jobs', job_row['id']), chat

    def _with_examples(self, project, ctx):
        ctx = copy.deepcopy(ctx)
        self.engine._attach_examples(project, ctx)
        return ctx

    def test_same_layout_gets_a_template_draft_without_a_model_call(self):
        job, chat = self.run_draft(context())
        self.assertEqual(job['status'], 'ready', job.get('error'))
        chat.assert_not_called()
        proposal = self.engine.store.get('proposals', job['proposal_id'])
        self.assertEqual(proposal['status'], 'pending_review')
        self.assertEqual(proposal['from_template']['id'], 'older-port')
        self.assertIn('From template', proposal['explanation'])
        self.assertEqual(self.engine.draft_record(job['id'])['examples_used'][0]['id'], 'older-port')
        self.assertEqual(self.engine.store.get('projects', self.project['id'])['packaging']['runner'], 'capture')  # approval is unchanged

    def test_cloud_models_never_receive_the_persons_other_saved_definitions(self):
        job, chat = self.run_draft(context(), skip_template=True)
        self.assertEqual(job['status'], 'ready', job.get('error'))
        record = self.engine.draft_record(job['id'])
        self.assertNotIn('older-port', [item['id'] for item in record['examples_used']])
        self.assertNotIn('older-port', record['draft_prompt']['user'])

    def test_refine_skips_the_template_and_records_examples_sent_to_the_model(self):
        patcher = patch.object(self.engine, '_local_model', return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        job, chat = self.run_draft(context(), skip_template=True)
        self.assertEqual(job['status'], 'ready', job.get('error'))
        self.assertGreaterEqual(chat.call_count, 1)
        record = self.engine.draft_record(job['id'])
        self.assertIn('older-port', [item['id'] for item in record['examples_used']])
        self.assertIn('similar_approved_definitions', record['draft_prompt']['user'])

    def test_unrelated_project_has_no_examples_and_still_uses_the_model(self):
        ctx = context(); ctx['project']['targets'] = ['handheld']
        ctx['release']['assets'] = [{'name': 'Newgame-handheld.zip'}]
        selected = templates.select(ctx, self.engine._template_references(self.project))
        self.assertFalse([item for item in selected if item['definition']['id'] == 'older-port'])

    def test_export_import_through_the_engine_creates_a_reviewable_draft(self):
        older = next(p for p in self.engine.store.all('projects') if p['repo'] == 'example/older')
        shared = json.dumps(self.engine.export_definition(older['id']))
        proposal = self.engine.import_definition(older['id'], shared)
        self.assertEqual(proposal['status'], 'pending_review')
        self.assertEqual(proposal['definition']['id'], 'older')
        other = self.engine.create_project({'name': 'Older fork', 'repo': 'example/older', 'targets': ['linux', 'windows']})
        foreign = reference(id='someone-else', package_prefix='someone-else', history=['by them'])
        pinned = self.engine.import_definition(other['id'], json.dumps(foreign))['definition']
        self.assertEqual((pinned['id'], pinned['package_prefix']), ('older', 'older'))
        with self.assertRaises(Invalid):
            self.engine.import_definition(older['id'], json.dumps({'schema': 2}))
        with self.assertRaises(Invalid):
            self.engine.import_definition(self.project['id'], shared)  # a recipe for another repository
        with self.assertRaises(Invalid):
            self.engine.export_definition(self.project['id'])  # nothing approved yet


if __name__ == '__main__':
    unittest.main()
