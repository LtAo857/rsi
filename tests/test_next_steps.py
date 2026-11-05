"""Tests for mutable diagnosis, noisy selection and bounded execution."""

import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
import subprocess
import time

from kernel.engine import Engine, SOURCE
from kernel.search import confirmed_gain, review_proposal


class NextStepsTests(unittest.TestCase):
    def test_mutable_policy_analysis_controls_selected_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory)
            engine.initialize()
            root = engine.version(engine.state()['active'])
            evidence = {'failures': [{'id': 'x', 'kind': 'repair_failure'}]}
            strategy = engine.call(root, 'analyze', evidence=evidence)
            self.assertEqual(strategy['failure_ids'], ['x'])
            self.assertEqual(strategy['target'], 'evolution')
            policy = root / 'evolution/policy.py'
            policy.write_text("def analyze(evidence):\n    return {'failure_ids':['x'],'target':'agent','hypothesis':'changed strategy'}\n")
            self.assertEqual(engine.call(root, 'analyze', evidence=evidence)['target'], 'agent')

    def test_legacy_policy_without_analysis_still_loads(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory)
            engine.initialize()
            root = engine.version(engine.state()['active'])
            (root / 'evolution/policy.py').write_text('def build_prompt(evidence, goal): return goal\n')
            self.assertIsNone(engine.call(root, 'analyze', evidence={}))

    def test_pruning_strategy_selects_maintenance_target(self):
        from evolution.policy import analyze
        evidence = {'failures':[{'id':'task-1','kind':'task_failure'},
                                 {'id':'complexity','kind':'maintenance'}], 'search':{'prune':True}}
        self.assertEqual(analyze(evidence)['failure_ids'], ['complexity'])

    def test_engine_uses_cost_measurements_for_both_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory)
            audit = Path(directory)
            for label, cost in [('base',100),('candidate',70)]:
                for i in range(2):
                    (audit / f'{label}-{i}.cost.json').write_text(json.dumps({'tokens':cost}))
            rows = [[1,1],[1,1]]
            self.assertTrue(engine.accept_trials(rows, rows, audit, 'candidate', 'base'))

    def test_strategy_selection_is_enforced(self):
        evidence = {'failures':[{'id':'x'},{'id':'y'}], 'strategy':{'failure_ids':['x']},
                    'search':{'edit_budget':10,'target':None,'prune':False}}
        proposal = {'analysis':'reason', 'changes':[{'files':['agent/a.py'], 'failure_id':'y',
                    'cause':'cause','expected_effect':'effect','validation':'test'}]}
        with self.assertRaisesRegex(ValueError, 'mutable strategy'):
            review_proposal(proposal, {'files':['agent/a.py'],'lines':1,'added':1,'removed':0}, evidence)

    def test_optional_tolerance_handles_noise_without_accepting_large_loss(self):
        new = [[.79, 1], [.81, 1], [.80, 1]]
        old = [[.8, 1]] * 3
        self.assertFalse(confirmed_gain(new, old, .01))
        self.assertTrue(confirmed_gain(new, old, .01, regression_tolerance=.02,
                                      candidate_costs=[80]*3, baseline_costs=[100]*3))
        self.assertFalse(confirmed_gain([[.7, 1]]*3, old, .01, regression_tolerance=.02,
                                       candidate_costs=[1]*3, baseline_costs=[100]*3))

    def test_cost_savings_need_known_cost_and_material_change(self):
        rows = [[1, 1]] * 2
        self.assertTrue(confirmed_gain(rows, rows, .01, candidate_costs=[80,80], baseline_costs=[100,100]))
        self.assertFalse(confirmed_gain(rows, rows, .01, candidate_costs=[99,99], baseline_costs=[100,100]))
        self.assertFalse(confirmed_gain(rows, rows, .01, candidate_costs=[None,None], baseline_costs=[100,100]))

    def test_prompt_limit_rejects_before_call_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory, [sys.executable, '-c', 'pass'], max_prompt_chars=5)
            with self.assertRaisesRegex(RuntimeError, 'before launch'):
                engine.code(Path(directory), 'too long', Path(directory) / 'test.log')
            self.assertFalse((Path(directory) / 'usage.json').exists())

    def test_output_limit_preserves_bounded_partial_log(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory, [sys.executable, '-c', "print('x'*100000)"], max_output_bytes=1024)
            log = Path(directory) / 'test.log'
            with self.assertRaisesRegex(RuntimeError, 'output byte limit'):
                engine.code(Path(directory), 'test', log)
            self.assertLessEqual(log.stat().st_size, 1025)

    def test_live_usage_limit_terminates_and_accounts_reported_tokens(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory, [sys.executable, '-c',
                            "import json,time; print(json.dumps({'type':'usage','input_tokens':20,'output_tokens':1}),flush=True); time.sleep(20)"],
                            max_call_tokens=10)
            started = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, 'token limit'):
                engine.code(Path(directory), 'test', Path(directory) / 'test.log')
            self.assertLess(time.monotonic() - started, 5)
            self.assertEqual(json.loads((Path(directory) / 'usage.json').read_text())['tokens'], 21)

    def test_unknown_per_call_usage_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory, [sys.executable, '-c', "print('no usage')"], max_call_tokens=10)
            with self.assertRaisesRegex(RuntimeError, 'usage missing'):
                engine.code(Path(directory), 'test', Path(directory) / 'test.log')

    def test_timeout_kills_descendant_before_it_writes(self):
        from kernel.execution import run_bounded
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'leaked'
            child = "import time; from pathlib import Path; time.sleep(1); Path('leaked').touch()"
            wrapper = "import subprocess,sys,time; subprocess.Popen([sys.executable,'-c'," + repr(child) + "]); time.sleep(10)"
            with self.assertRaises(subprocess.TimeoutExpired):
                run_bounded([sys.executable, '-c', wrapper], '', directory, .2)
            time.sleep(1.1)
            self.assertFalse(marker.exists())

    def test_multiseed_validation_does_not_feed_search_history(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory, [sys.executable, '-B', str(SOURCE / 'examples/demo_coder.py')], trials=1)
            with contextlib.redirect_stdout(io.StringIO()):
                engine.run(1)
            state = engine.state()
            history = (engine.root / 'history.jsonl').read_bytes()
            with engine.lock():
                result = engine.validate_versions(3)
            self.assertEqual(len(result['seeds']), 3)
            self.assertEqual(result['baseline_score'], .5)
            self.assertEqual(result['active_score'], 1)
            self.assertEqual(sum(r['regressed'] for r in result['seeds']), 0)
            self.assertEqual(engine.state(), state)
            self.assertEqual((engine.root / 'history.jsonl').read_bytes(), history)

    def test_proposal_explains_the_enforced_strategy_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory, [sys.executable, '-B', str(SOURCE / 'examples/demo_coder.py')], trials=1)
            with contextlib.redirect_stdout(io.StringIO()):
                result = engine.run(1)[0]
            prompt = (Path(result['audit']) / 'proposal.txt').read_text()
            self.assertIn('EVERY change.failure_id', prompt)
            self.assertIn('Evidence.strategy.failure_ids', prompt)

    def test_advanced_operation_semantics_are_explicit(self):
        from kernel.evaluation import tasks
        rows = [task for task,_ in tasks(10, suite='advanced')]
        self.assertTrue(all(task.get('instruction') for task in rows))
        self.assertIn('Do not apply weights', next(task['instruction'] for task in rows
                                                 if task['operation'] == 'group_max_total'))

    def test_saved_live_snapshot_matches_published_generation(self):
        from kernel.engine import identity
        root = SOURCE / 'docs/experiments/artifacts/2026-11-05-accepted'
        self.assertEqual(identity(root), '56a1e0402a8dd075e67729c32bfaca09b8887033c007a5937f935106ce27b80b')
