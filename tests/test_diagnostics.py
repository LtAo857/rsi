import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from kernel.diagnostics import diagnose_evaluation, diagnose_trace
from kernel.engine import Engine, SOURCE


def page(offset, next_offset):
    return {'action':{'tool':'read_csv','args':{'path':'orders.csv','offset':offset}},
            'result':{'rows':[], 'next_offset':next_offset}}


class DiagnosticTests(unittest.TestCase):
    def kinds(self, trace):
        return {f['kind'] for f in diagnose_trace(trace)['findings']}

    def test_sql_recovery_is_observed_without_rejecting_correct_answer(self):
        trace = {'ok':True, 'stop_reason':'answer', 'history':[
            {'action':{'tool':'sql','args':{'query':'bad'}}, 'result':{'error':'syntax error'}},
            {'action':{'tool':'sql','args':{'query':'SELECT 1'}}, 'result':{'rows':[[1]]}}]}
        result = diagnose_trace(trace)
        self.assertEqual(result['outcome'], 'correct')
        self.assertEqual(result['findings'][0]['kind'], 'sql_error')
        self.assertEqual(result['findings'][0]['steps'], [0])
        self.assertNotIn('wrong_answer', self.kinds(trace))

    def test_pagination_completion_skips_and_restart(self):
        for history, incomplete in [([page(0,20)], True), ([page(0,20),page(20,None)],False),
                                   ([page(0,20),page(40,60),page(60,None)],True),
                                   ([page(20,None)],True),
                                   ([page(0,20),page(40,None),page(0,None)],False)]:
            with self.subTest(history=history):
                result = self.kinds({'ok':True, 'stop_reason':'answer','history':history})
                self.assertEqual('unread_pages' in result, incomplete)

    def test_wrong_answer_is_not_invented_arithmetic_cause(self):
        result = diagnose_trace({'ok':False,'stop_reason':'answer','history':[]})
        self.assertEqual({f['kind'] for f in result['findings']}, {'answer_without_tools','wrong_answer'})
        self.assertEqual(result['findings'][0]['certainty'], 'risk')

    def test_invalid_action_truncation_and_runner_limits(self):
        history = [{'action':['bad'], 'result':{'error':'invalid'}},
                   {'action':{'tool':'sql','args':{}},'result':{'truncated':True,'rows':[]}}]
        for reason in ('step_limit','context_limit','repeated_action'):
            self.assertEqual(self.kinds({'history':history,'stop_reason':reason}),
                             {'invalid_action','truncated_sql_result',reason})
        self.assertIn('execution_error', self.kinds({'error':'worker failed'}))

    def test_report_is_copied_and_summary_is_deterministic(self):
        original = {'score':0,'traces':[{'ok':False,'stop_reason':'answer','history':[]}]}
        result = diagnose_evaluation(original)
        self.assertNotIn('diagnostics', original['traces'][0])
        self.assertEqual(result['score'], original['score'])
        self.assertEqual(result['diagnostic_summary']['counts']['wrong_answer'],1)
        self.assertEqual(diagnose_evaluation(result), result)
        with self.assertRaises(ValueError):
            diagnose_evaluation({'traces':[{'history':[None]}]})

    def test_cli_reads_existing_report_without_creating_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            report = root / 'input.json'
            report.write_text(json.dumps({'evaluation':{'traces':[{'ok':True,'history':[]}]}}))
            state = root / 'unused'
            process = subprocess.run([sys.executable,'-m','kernel','--state',str(state),'diagnose',str(report)],
                                     cwd=SOURCE, capture_output=True,text=True)
            self.assertEqual(process.returncode,0,process.stderr)
            self.assertEqual(json.loads(process.stdout)['diagnostic_summary']['tasks'],1)
            self.assertFalse(state.exists())

    def test_failure_diagnostics_reach_next_proposal(self):
        class RecordingEngine(Engine):
            def score(self, root, seed, heldout=False):
                result = super().score(root, seed, heldout)
                return diagnose_evaluation(result)
            def code(self, workspace, prompt, log, command=None):
                if (workspace / 'agent').exists():
                    self.evidence = json.loads(prompt.rsplit('Evidence:\n',1)[1])
                return super().code(workspace,prompt,log,command)
        with tempfile.TemporaryDirectory() as directory:
            engine = RecordingEngine(directory, [sys.executable,str(SOURCE / 'examples/demo_coder.py')],trials=1)
            with contextlib.redirect_stdout(io.StringIO()):
                engine.run(1)
            failures = [r for r in engine.evidence['failures'] if r['kind']=='task_failure']
            self.assertTrue(failures)
            self.assertTrue(all('diagnostics' in r['trace'] for r in failures))
