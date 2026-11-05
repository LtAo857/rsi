"""Offline lifecycle and independent repair diagnostics; no model calls."""

import contextlib
import io
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest

from kernel.engine import Engine, SOURCE
from kernel.evaluation import answers_equal, heldout_repair_cases, repair_case


CSV_FIX = '''import csv
import io
def read_names(text):
    return [row['name'] for row in csv.DictReader(io.StringIO(text))]
'''
INTERVAL_FIX = '''def merge(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1][1] = max(result[-1][1], end)
        else:
            result.append([start, end])
    return result
'''


class PolicyValidationTests(unittest.TestCase):
    def test_health_rejects_invalid_analysis_and_accepts_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory)
            engine.initialize()
            root = engine.version(engine.state()['active'])
            policy = root / 'evolution/policy.py'
            policy.write_text('def build_prompt(evidence, goal): return goal\ndef analyze(evidence): return {}\n')
            with self.assertRaisesRegex(ValueError, 'invalid mutable analysis'):
                engine.health(root)
            policy.write_text('def build_prompt(evidence, goal): return goal\n')
            engine.health(root)

    def test_candidate_checked_on_real_evidence_before_activation(self):
        class InvalidCandidate(Engine):
            def code(self, workspace, prompt, log, command=None):
                result = super().code(workspace, prompt, log, command)
                policy = workspace / 'evolution/policy.py'
                if policy.exists():
                    with policy.open('a') as stream:
                        stream.write("\ndef analyze(evidence):\n    return {'failure_ids':['health' if evidence['failures'][0]['id'] == 'health' else 'invented'],'target':'agent','hypothesis':'test'}\n")
                    path = workspace / '.rsi-proposal.json'
                    proposal = json.loads(path.read_text())
                    proposal['changes'][0]['files'].append('evolution/policy.py')
                    path.write_text(json.dumps(proposal))
                return result
        with tempfile.TemporaryDirectory() as directory:
            engine = InvalidCandidate(directory, [sys.executable, str(SOURCE / 'examples/demo_coder.py')], trials=1)
            engine.initialize()
            active = engine.state()['active']
            with contextlib.redirect_stdout(io.StringIO()):
                engine.run(1)
            self.assertEqual(engine.state()['active'], active)
            result = json.loads(next((engine.root / 'rounds').glob('*/result.json')).read_text())
            self.assertIn('invalid mutable analysis', result['reason'])
            self.assertFalse(result['accepted'])

    def test_replacement_does_not_receive_incumbent_strategy(self):
        with tempfile.TemporaryDirectory() as directory:
            engine = Engine(directory)
            engine.initialize()
            root = engine.version(engine.state()['active'])
            (root / 'evolution/policy.py').write_text("def analyze(evidence):\n    assert 'strategy' not in evidence\n    return {'failure_ids':['x'],'target':'evolution','hypothesis':'own analysis'}\n")
            result = engine.analyze(root, {'failures':[{'id':'x'}], 'strategy':{'secret':'incumbent'}})
            self.assertEqual(result['target'], 'evolution')

    def test_nested_booleans_do_not_pass_numeric_checks(self):
        self.assertFalse(answers_equal([[True]], [[1]]))
        self.assertTrue(answers_equal([[1.0]], [[1]]))
        self.assertFalse(answers_equal([[1,2]], [[1]]))

    def test_holdout_families_are_excluded_from_search(self):
        seen = {repair_case('advanced', i)['name'] for i in range(30)}
        self.assertFalse(seen & {case['name'] for case in heldout_repair_cases()})

    def test_policy_diagnostic_scores_repairs_without_changing_history(self):
        class ScriptedRepair(Engine):
            def code(self, workspace, prompt, log, command=None):
                source = CSV_FIX if 'read_names' in prompt else INTERVAL_FIX
                (workspace / 'solution.py').write_text(source)
                self.assert_hidden_not_exposed(prompt)

            @staticmethod
            def assert_hidden_not_exposed(prompt):
                assert 'Doe, Jane' not in prompt
                assert '[-3, 2]' not in prompt

        with tempfile.TemporaryDirectory() as directory:
            engine = ScriptedRepair(directory, ['scripted-test-fixture'])
            engine.initialize()
            history = (engine.root / 'history.jsonl').read_bytes()
            state = engine.state()
            evidence = engine.search_evidence()
            with engine.lock():
                result = engine.validate_policy()
            self.assertEqual(result['baseline_score'], 1)
            self.assertEqual(result['active_score'], 1)
            self.assertEqual(len(result['cases']), 2)
            self.assertEqual(engine.state(), state)
            self.assertEqual((engine.root / 'history.jsonl').read_bytes(), history)
            self.assertEqual(engine.search_evidence(), evidence)

    def test_policy_diagnostic_requires_coder(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, 'coding command'):
                Engine(directory).validate_policy()

    def test_holdout_detects_broken_and_test_tampering_repairs(self):
        for tamper in (False, True):
            with self.subTest(tamper=tamper), tempfile.TemporaryDirectory() as directory:
                class Repair(Engine):
                    def code(self, workspace, prompt, log, command=None):
                        if tamper:
                            (workspace / 'solution.py').write_text(CSV_FIX)
                            (workspace / 'test_solution.py').write_text('pass\n')
                engine = Repair(directory, ['scripted-test-fixture'])
                engine.initialize()
                score = engine.repair_probe(engine.version(engine.state()['active']), Path(directory),
                                            'probe', heldout_repair_cases()[0])
                if tamper:
                    self.assertEqual(score, 0)
                else:
                    self.assertLess(score, 1)

    def test_cli_diagnostic_accounts_calls_and_obeys_budget(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            coder = root / 'coder.py'
            coder.write_text("import sys,json\nfrom pathlib import Path\nprompt=sys.stdin.read()\n" +
                             "Path('solution.py').write_text(" + repr(CSV_FIX) +
                             " if 'read_names' in prompt else " + repr(INTERVAL_FIX) + ")\n" +
                             "print(json.dumps({'type':'usage','input_tokens':1,'output_tokens':1}))\n")
            state = root / 'state'
            command = [sys.executable, '-m', 'kernel', '--state', str(state), 'validate-policy',
                       '--coder', shlex.join([sys.executable, str(coder)]), '--max-calls', '4']
            process = subprocess.run(command, cwd=SOURCE, capture_output=True, text=True)
            self.assertEqual(process.returncode, 0, process.stderr)
            self.assertEqual(json.loads(process.stdout)['active_score'], 1)
            self.assertEqual(json.loads((state / 'usage.json').read_text())['calls'], 4)
            previous = (state / 'policy-validation.json').read_bytes()
            process = subprocess.run(command, cwd=SOURCE, capture_output=True, text=True)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn('budget', process.stderr)
            self.assertEqual(json.loads((state / 'usage.json').read_text())['calls'], 4)
            self.assertEqual((state / 'policy-validation.json').read_bytes(), previous)
