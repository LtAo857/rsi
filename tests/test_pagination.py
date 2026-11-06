import csv
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from kernel.datadesk import PaginatedToolbox, run_session
from kernel.engine import Engine, SOURCE
from kernel.pagination_tasks import make_pagination_tasks


MODEL = [sys.executable,str(SOURCE/'tests/fixtures/pagination_model.py')]


class PaginationTests(unittest.TestCase):
    def test_generated_data_is_reproducible_and_requires_later_rows(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            first = make_pagination_tasks(Path(a),123)
            second = make_pagination_tasks(Path(b),123)
            for x,y in zip(first,second):
                self.assertEqual(x['digest'],y['digest'])
                data = (x['directory']/'records.csv').read_bytes()
                self.assertEqual(data,(y['directory']/'records.csv').read_bytes())
                with (x['directory']/'records.csv').open() as stream:
                    rows = list(csv.DictReader(stream))
                self.assertGreater(len(rows),60)
                self.assertLess(len(rows),80)
                self.assertEqual(rows[-1]['status'],'active')

    def test_tool_interface_enforces_pagination_without_sql_bypass(self):
        with tempfile.TemporaryDirectory() as directory:
            item = make_pagination_tasks(Path(directory),123)[0]
            tools = PaginatedToolbox(item['directory'])
            self.assertEqual(set(tools.help),{'list_files','read_csv'})
            for tool in ('sql','schema','read_file'):
                self.assertIn('error',tools.call(tool,{}))
            self.assertIn('error',tools.call('read_csv',{'path':'records.csv','limit':100}))
            self.assertIn('error',tools.call('read_csv',{'path':'records.csv','offset':True}))
            self.assertIn('error',tools.call('read_csv',{'path':'../outside.csv'}))
            page = tools.call('read_csv',{'path':'records.csv'})
            self.assertEqual(len(page['rows']),20)
            self.assertEqual(page['next_offset'],20)
            context = []
            run_session('test',tools,lambda task: context.append(task) or {'answer':0})
            self.assertEqual(set(context[0]['tools']),set(tools.help))

    def test_full_reading_passes_and_incomplete_reading_fails(self):
        for flags,expected in (([],1),(['--first-page'],0)):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as directory:
                engine = Engine(directory,task_command=MODEL+flags,benchmark='datadesk',datadesk_size='pagination')
                engine.initialize()
                report = engine.score(engine.version(engine.state()['active']),123)
                self.assertEqual(report['score'],expected)
                for row in report['traces']:
                    self.assertEqual(row['history'][0]['result']['next_offset'],20)
                    kinds = {f['kind'] for f in row['diagnostics']['findings']}
                    self.assertEqual('unread_pages' in kinds,expected==0)
                    if expected:
                        self.assertEqual([h['action']['args']['offset'] for h in row['history']],[0,20,40,60])
                        self.assertIsNone(row['history'][-1]['result']['next_offset'])

    def test_heldout_uses_distinct_data_and_profile_is_locked(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory,task_command=MODEL,benchmark='datadesk',datadesk_size='pagination')
            engine.initialize()
            root = engine.version(engine.state()['active'])
            a,b = engine.score(root,123),engine.score(root,123,heldout=True)
            self.assertNotEqual(a['data_seed'],b['data_seed'])
            self.assertNotEqual(a['traces'][0]['history'][0]['result']['rows'],b['traces'][0]['history'][0]['result']['rows'])
            self.assertEqual(b['score'],1)
            self.assertEqual(Engine(directory).datadesk_size,'pagination')
            with self.assertRaises(ValueError):
                Engine(directory,datadesk_size='compact')

    def test_cli_evaluate_and_paging_hint_ablation(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory,task_command=MODEL+['--respect-hint'],benchmark='datadesk',datadesk_size='pagination')
            engine.initialize()
            state = engine.state()
            history = (engine.root/'history.jsonl').read_bytes()
            result = subprocess.run([sys.executable,'-m','kernel','--state',directory,'evaluate','--max-calls','20'],
                                    cwd=SOURCE,capture_output=True,text=True)
            self.assertEqual(result.returncode,0,result.stderr)
            self.assertEqual(json.loads(result.stdout)['evaluation']['score'],1)
            spec = json.loads((SOURCE/'examples/ablation-prompts.json').read_text())[:1]
            with engine.lock():
                report = engine.ablate(spec,1)
            trial = report['variants'][0]['trials'][0]
            self.assertEqual(trial['baseline']['score'],1)
            self.assertEqual(trial['ablated']['score'],0)
            self.assertEqual(trial['regressed'],2)
            self.assertEqual(engine.state(),state)
            self.assertEqual((engine.root/'history.jsonl').read_bytes(),history)
