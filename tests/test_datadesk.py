"""DataDesk tools, source task migration and multi-turn lifecycle, all offline."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from kernel.datadesk import Toolbox, check_answer, run_session
from kernel.datadesk_tasks import EVOLVE, HELDOUT_ONLY, generate
from kernel.engine import Engine, SOURCE


MODEL = [sys.executable, str(SOURCE / 'tests/fixtures/datadesk_model.py')]


class DataDeskTests(unittest.TestCase):
    def make_data(self, root):
        (root / 'people.csv').write_text('name,qty\n"Doe, Jane",2\nBob,3\n', encoding='utf-8')
        (root / 'products.json').write_text('[{"name":"Doe, Jane","cost":1}]')

    def test_tools_csv_paging_quoted_values_and_sql_join(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.make_data(root)
            tools = Toolbox(root)
            try:
                page = tools.call('read_csv', {'path':'people.csv','limit':1})
                self.assertEqual(page['rows'][0]['name'], 'Doe, Jane')
                self.assertEqual(page['next_offset'], 1)
                self.assertIsNone(tools.call('read_csv', {'path':'people.csv','offset':1})['next_offset'])
                rows = tools.call('sql', {'query':'SELECT SUM(CAST(qty AS REAL)*cost) FROM people JOIN products USING(name)'})
                self.assertEqual(rows['rows'], [[2.0]])
                self.assertEqual(tools.call('schema', {})['tables']['people'], ['name','qty'])
                text = tools.call('read_file', {'path':'people.csv','limit':4})
                self.assertEqual(text, {'text':'name','next_offset':4})
                self.assertEqual(len(tools.call('list_files', {})['files']), 2)
            finally:
                tools.close()

    def test_tools_reject_paths_writes_attach_and_unbounded_queries(self):
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            self.make_data(root)
            secret = Path(outside) / 'secret.csv'
            secret.write_text('secret\nvalue\n')
            (root / 'link.csv').symlink_to(secret)
            # Outward symlinks are rejected during import as well as tool calls.
            with self.assertRaises(ValueError):
                Toolbox(root)
            (root / 'link.csv').unlink()
            tools = Toolbox(root)
            try:
                self.assertIn('error', tools.call('read_file', {'path':str(secret)}))
                self.assertIn('error', tools.call('read_file', {'path':'../secret.csv'}))
                for query in ['DELETE FROM people', "ATTACH ':memory:' AS other", 'PRAGMA table_info(people)',
                              'SELECT 1; SELECT 2',
                              'WITH RECURSIVE x(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM x) SELECT SUM(n) FROM x']:
                    self.assertIn('error', tools.call('sql', {'query':query}), query)
                self.assertEqual(tools.call('sql', {'query':'SELECT COUNT(*) FROM people'})['rows'], [[2]])
                page = tools.call('sql', {'query':'WITH RECURSIVE x(n) AS (VALUES(1) UNION ALL SELECT n+1 FROM x WHERE n<105) SELECT n FROM x'})
                self.assertTrue(page['truncated'])
                self.assertEqual(len(page['rows']), 100)
                self.assertIn('error', tools.call('read_csv', {'path':'people.csv','limit':True}))
            finally:
                tools.close()

    def test_session_recovers_from_errors_and_has_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            self.make_data(Path(directory))
            tools = Toolbox(directory)
            actions = iter([{'tool':'sql','args':{'query':'SELECT missing'}},
                            {'tool':'sql','args':{'query':'SELECT SUM(qty) FROM people'}}, {'answer':5}])
            try:
                result = run_session('sum quantities', tools, lambda context: next(actions))
                self.assertEqual(result['answer'], '5')
                self.assertIn('error', result['history'][0]['result'])
                self.assertEqual(result['steps'], 3)
                result = run_session('q', tools, lambda _: {'tool':'schema','args':{}})
                self.assertEqual(result['stop_reason'], 'repeated_action')
                result = run_session('q', tools, lambda _: {'bad':'json'}, max_steps=2)
                self.assertEqual(result['stop_reason'], 'step_limit')
                result = run_session('q', tools, lambda _: self.fail('should not call'), max_context_chars=1)
                self.assertEqual(result['stop_reason'], 'context_limit')
            finally:
                tools.close()

    def test_migrated_generator_and_grader_cover_original_splits(self):
        with tempfile.TemporaryDirectory() as directory:
            plain = {}
            splits = generate(Path(directory), plain=plain)
            self.assertEqual({k:len(v) for k,v in splits.items()}, {'evolve':15,'heldout':20,'smoke':1})
            hashes = json.loads((Path(directory) / 'bench/answers.json').read_text())
            for tid, (kind, answer) in plain.items():
                self.assertTrue(check_answer(tid, kind, answer, hashes[tid]), tid)
            tid = next(t for t,(kind,_) in plain.items() if kind == 'number')
            kind, answer = plain[tid]
            self.assertTrue(check_answer(tid, kind, f'${answer:,.2f}', hashes[tid]))
            self.assertFalse(check_answer(tid, kind, f'2025: {answer}', hashes[tid]))

    def test_all_seven_families_run_through_model_and_tool_subprocesses(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory, task_command=MODEL + ['--bad-query'], benchmark='datadesk', max_calls=50)
            engine.initialize()
            active = engine.state()['active']
            result = engine.score(engine.version(active), 202, heldout=True)
            self.assertEqual(result['score'], 1, result['traces'])
            self.assertEqual(result['families'], EVOLVE + HELDOUT_ONLY)
            self.assertEqual(len(result['traces']), 7)
            self.assertTrue(all(row['steps'] == 4 for row in result['traces']))
            self.assertTrue(all('error' in row['history'][1]['result'] for row in result['traces']))
            self.assertEqual(json.loads((engine.root / 'usage.json').read_text())['calls'], 28)
            self.assertEqual(engine.state()['active'], active)

    def test_infra_failures_and_budget_exhaustion_do_not_become_wrong_answers(self):
        from kernel.backends import ModelUnavailable
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory, task_command=MODEL, benchmark='datadesk', max_calls=1)
            engine.initialize()
            with self.assertRaises(ModelUnavailable):
                engine.score(engine.version(engine.state()['active']), 101)
            self.assertEqual(json.loads((engine.root / 'usage.json').read_text())['calls'], 1)

    def test_cli_evaluate_persists_backend_and_keeps_history_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory, task_command=MODEL, benchmark='datadesk')
            engine.initialize()
            history = (engine.root / 'history.jsonl').read_bytes()
            result = subprocess.run([sys.executable, '-m', 'kernel', '--state', directory,
                                     'evaluate', '--split','heldout','--max-calls','30'],
                                    cwd=SOURCE, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            evaluation = json.loads(result.stdout)['evaluation']
            self.assertEqual(evaluation['score'], 1)
            self.assertEqual(evaluation['split'], 'heldout')
            self.assertEqual((engine.root / 'history.jsonl').read_bytes(), history)
            self.assertEqual(Engine(directory).benchmark, 'datadesk')
            with self.assertRaisesRegex(ValueError, 'benchmark changed'):
                Engine(directory, benchmark='basic')

    def test_search_excludes_heldout_families_and_uses_fresh_shops(self):
        class RecordingEngine(Engine):
            def datadesk_session(self, root, question, data_dir):
                self.data.append((Path(data_dir) / 'orders.csv').read_bytes())
                return {'answer':'', 'history':[], 'stop_reason':'answer', 'steps':1}
        with tempfile.TemporaryDirectory() as directory:
            engine = RecordingEngine(directory, task_command=MODEL, benchmark='datadesk')
            engine.data = []
            engine.initialize()
            root = engine.version(engine.state()['active'])
            first = engine.score(root, 101)
            snapshot = list(engine.data)
            engine.data = []
            second = engine.score(root, 102)
            self.assertEqual(first['families'], EVOLVE)
            self.assertEqual(len(first['traces']), 5)
            self.assertNotEqual(snapshot, engine.data)

    def test_full_size_preserves_original_task_counts_and_is_locked(self):
        class NoModel(Engine):
            def datadesk_session(self, root, question, data_dir):
                return {'answer':'', 'history':[], 'stop_reason':'answer', 'steps':1}
        with tempfile.TemporaryDirectory() as directory:
            engine = NoModel(directory, task_command=MODEL, benchmark='datadesk', datadesk_size='full')
            engine.initialize()
            root = engine.version(engine.state()['active'])
            self.assertEqual(len(engine.score(root, 101)['traces']), 15)
            self.assertEqual(len(engine.score(root, 202, heldout=True)['traces']), 20)
            self.assertEqual(Engine(directory).datadesk_size, 'full')
            with self.assertRaisesRegex(ValueError, 'size changed'):
                Engine(directory, datadesk_size='compact')

    def test_missing_backend_fails_before_creating_an_unusable_experiment(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'requires a task model'):
                Engine(directory, benchmark='datadesk')
            self.assertFalse((Path(directory) / 'state.json').exists())
            self.assertFalse((Path(directory) / 'runtime.json').exists())

    def test_generation_action_parser_handles_fences_and_reports_bad_json(self):
        from agent.solver import parse_action
        self.assertEqual(parse_action('```json\n{"answer":5}\n```'), {'answer':5})
        self.assertIn('invalid_response', parse_action('not JSON'))

    def test_datadesk_candidate_is_activated_after_fresh_confirmation(self):
        class ScriptedCoder(Engine):
            def code(self, workspace, prompt, log, command=None):
                if command is not None:
                    return super().code(workspace, prompt, log, command)
                solver = workspace / 'agent/solver.py'
                if solver.exists():
                    solver.write_text(solver.read_text().replace('Answer the question using', 'USE_SQL Answer the question using'))
                    (workspace / '.rsi-proposal.json').write_text(json.dumps({
                        'analysis':'Scripted test of candidate prompt activation',
                        'changes':[{'files':['agent/solver.py'],'failure_id':'task-0','cause':'fixture requires marker',
                                    'expected_effect':'exercise tool evaluation','validation':'fixed scoring'}]}))
                return ''
        with tempfile.TemporaryDirectory() as directory:
            engine = ScriptedCoder(directory, ['scripted-coder'], task_command=MODEL + ['--require-marker'],
                                   benchmark='datadesk', trials=1, max_calls=100)
            engine.initialize()
            initial = engine.state()['active']
            with contextlib.redirect_stdout(io.StringIO()):
                engine.run(1)
            self.assertNotEqual(engine.state()['active'], initial)
            report = json.loads(next((engine.root / 'rounds').glob('*/result.json')).read_text())
            self.assertTrue(report['accepted'], report)
            self.assertEqual(report['baseline'][0], 0)
            self.assertEqual(report['candidate'][0], 1)


if __name__ == '__main__':
    unittest.main()
