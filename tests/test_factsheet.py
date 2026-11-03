import copy
import json
import unittest
from unittest.mock import patch

from portkeeper import factsheet, investigation
from test_investigation import InvestigationTests as Base

MD5S = ['0b' * 16, '1c' * 16, '2d' * 16, '3e' * 16]
README = '\n'.join(['# Perfect Dark', 'Place your ROM as `data/pd.ntsc-final.z64` before launching.', 'Accepted ROMs (md5):'] + [f'- {h}  {v}' for h, v in zip(MD5S, ['ntsc-final', 'pal-final', 'jpn-final', 'ntsc-1.0'])] + ['Build with cmake and python.'])


def build(archives, readme='', assets=None):
    assets = assets if assets is not None else [{'name': name, 'size': 100} for name in archives]
    return factsheet.build({'assets': assets}, {'archive_evidence': archives, 'material': {'readme': readme}})


def texts(sheet, kind):
    return [fact['text'] for fact in sheet['facts'] if fact['kind'] == kind]


class FactSheetTests(unittest.TestCase):
    def test_release_with_top_level_folder_and_data_dir(self):
        sheet = build({'pd-x86_64-linux.tar.gz': ['pd/', 'pd/pd.x86_64', 'pd/data/', 'pd/data/a.bin']})
        layout = next(f for f in sheet['facts'] if f['kind'] == 'archive_layout')
        self.assertEqual(layout['data']['enclosing_folder'], 'pd/')
        self.assertIn('data/', layout['data']['folders'])
        executable = next(f for f in sheet['facts'] if f['kind'] == 'executable')
        self.assertEqual((executable['data']['path'], executable['data']['relative']), ('pd/pd.x86_64', 'pd.x86_64'))
        launch = next(f for f in sheet['facts'] if f['kind'] == 'launch_candidate')
        self.assertEqual(launch['data']['platform'], 'linux')
        self.assertEqual([f['id'] for f in sheet['facts']], ['f%d' % i for i in range(1, len(sheet['facts']) + 1)])

    def test_release_without_top_level_folder(self):
        sheet = build({'pd-x86_64-windows.zip': ['pd.exe', 'data/a.bin', 'readme.txt']})
        layout = next(f for f in sheet['facts'] if f['kind'] == 'archive_layout')
        self.assertIsNone(layout['data']['enclosing_folder'])
        self.assertIn('pd.exe', layout['data']['root_files'])
        self.assertEqual(next(f for f in sheet['facts'] if f['kind'] == 'executable')['data']['path'], 'pd.exe')

    def test_bundled_libraries_are_listed_not_treated_as_launch_candidates(self):
        sheet = build({'game-windows.zip': ['Game.exe', 'SDL2.dll', 'bin/zlib1.dll', 'libfoo.so.1']})
        libs = next(f for f in sheet['facts'] if f['kind'] == 'libraries')
        self.assertEqual(libs['data']['count'], 3)
        self.assertEqual([f['data']['path'] for f in sheet['facts'] if f['kind'] == 'launch_candidate'], ['Game.exe'])

    def test_readme_hashes_names_and_tools_have_line_sources(self):
        sheet = build({}, README, assets=[])
        hashes = [f for f in sheet['facts'] if f['kind'] == 'hash']
        self.assertEqual({f['data']['value'] for f in hashes}, set(MD5S))
        self.assertTrue(all(f['data']['algorithm'] == 'md5' and f['source']['file'] == 'README.md' and f['source']['lines'] for f in hashes))
        self.assertEqual(hashes[0]['source']['lines'], [4, 4])
        self.assertTrue(any('pd.ntsc-final.z64' in text for text in texts(sheet, 'data_file')))
        self.assertTrue(any('ntsc-final' in text for text in texts(sheet, 'version_name')))
        self.assertEqual({f['data']['tool'] for f in sheet['facts'] if f['kind'] == 'tool_mention'}, {'cmake', 'python'})

    def test_sheet_is_deterministic(self):
        args = ({'pd.zip': ['pd.exe']}, README)
        self.assertEqual(build(*args), build(*args))


class FactCitationTests(Base):
    def cited_plan(self, ids):
        plan = copy.deepcopy(self.plan)
        plan['evidence'] = [{'facts': ids, 'supports': 'Runtime, launch and included data'}]
        return plan

    def test_fact_sheet_is_in_the_prompt_and_fact_citations_are_accepted(self):
        reader = self.reader()
        ids = [f['id'] for f in reader.sheet['facts'] if f['kind'] in ('launch_candidate', 'executable') or f['doc']]
        self.assertTrue(ids)
        result = investigation.check_plan(self.cited_plan(ids[:2]), self.project, self.capture, reader)
        self.assertTrue(result['evidence'][0]['matched'])
        self.assertEqual(result['evidence'][0]['facts'], ids[:2])
        self.assertEqual(result['evidence'][0]['path'] is not None, True)

    def test_fact_sheet_reaches_the_outgoing_request(self):
        reader = self.reader()
        with patch.object(self.engine.assistant, 'chat', return_value=json.dumps({'action': 'plan', 'plan': self.plan})) as chat:
            investigation.run(self.engine, self.project, self.capture, copy.deepcopy(self.context), {'drafting_model': 'fixture'}, 1, lambda _: None, lambda _: None)
        request = json.loads(chat.call_args.args[1][0]['content'])
        self.assertTrue(request['fact_sheet'])
        self.assertEqual([f['id'] for f in request['fact_sheet']], [f['id'] for f in reader.sheet['facts']][:len(request['fact_sheet'])])
        self.assertEqual(request['fact_sheet'][0]['text'], reader.sheet['facts'][0]['text'])

    def test_oversized_fact_sheet_is_trimmed_before_evidence_and_samples(self):
        context = copy.deepcopy(self.context)
        context['archive_evidence'] = {f'a{i}.zip': [f'dir/tool{j}.exe' for j in range(200)] for i in range(4)}
        with patch.object(self.engine.assistant, 'chat', return_value=json.dumps({'action': 'plan', 'plan': self.plan})) as chat:
            try:
                investigation.run(self.engine, self.project, self.capture, context, {'drafting_model': 'fixture'}, 1, lambda _: None, lambda _: None)
            except investigation.Invalid:
                pass
        request = json.loads(chat.call_args.args[1][0]['content'])
        self.assertTrue(request['evidence'])
        self.assertEqual(len(request['archive_samples']), 4)


    def test_unknown_fact_ids_are_rejected_listing_each_invalid_id(self):
        with self.assertRaisesRegex(investigation.Invalid, r'Unknown fact IDs: f998, f999'):
            investigation.check_plan(self.cited_plan(['f1', 'f998', 'f999']), self.project, self.capture, self.reader())

    def test_quote_fallback_still_works_alongside_fact_ids(self):
        plan = self.cited_plan(['f1'])
        plan['evidence'].append(self.plan['evidence'][0])
        result = investigation.check_plan(plan, self.project, self.capture, self.reader())
        self.assertTrue(result['can_draft'])


class DraftingFactTests(unittest.TestCase):
    def test_cited_facts_survive_the_prompt_cap(self):
        from portkeeper import drafting
        sheet = [{'id': f'f{i}', 'kind': 'data_file', 'text': f'fact {i}', 'source': {'file': 'README.md'}, 'doc': True} for i in range(1, 121)]
        plan = {'outcome': 'ready', 'summary': 's', 'targets': [], 'game_files': {'requirements': ''}, 'steps': [],
                'evidence': [{'source': 'f106', 'quote': 'fact 106', 'supports': 'x', 'facts': ['f106']}], 'fact_sheet': sheet}
        context = {'project': {'repository': 'o/r', 'targets': ['linux']}, 'release': {'version': '1', 'archive_contents': {}}, 'source': {},
                   'investigation_plan': plan}
        _, prompt = drafting.prompt_parts(context, {})
        self.assertIn('"id":"f106"', prompt)


if __name__ == '__main__':
    unittest.main()
