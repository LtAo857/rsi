import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from kernel.engine import Engine, SOURCE


class SearchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def engine(self, **settings):
        return Engine(self.root / 'state',
                      [sys.executable, '-B', str(SOURCE / 'examples/demo_coder.py')],
                      timeout=30, **settings)

    def run_engine(self, engine, rounds):
        with contextlib.redirect_stdout(io.StringIO()):
            return engine.run(rounds)

    def test_small_or_noisy_gains_are_rejected(self):
        from kernel.search import confirmed_gain
        self.assertFalse(confirmed_gain([[.51, 1], [.51, 1]], [[.5, 1], [.5, 1]], .02))
        self.assertFalse(confirmed_gain([[1, 1], [.2, 1]], [[.5, 1], [.5, 1]], .01))
        self.assertFalse(confirmed_gain([[1, .8], [1, .8]], [[.5, 1], [.5, 1]], .01))
        self.assertTrue(confirmed_gain([[1, 1], [1, 1]], [[.5, 1], [.5, 1]], .01))

    def test_byte_only_edits_still_require_failure_mapping(self):
        from kernel.search import patch_stats
        before, after = self.root / 'before', self.root / 'after'
        before.mkdir()
        after.mkdir()
        (before / 'x.py').write_bytes(b'x=1\n')
        (after / 'x.py').write_bytes(b'x=1\r\n')
        (after / 'empty.py').touch()
        self.assertEqual(patch_stats(before, after)['files'], ['empty.py', 'x.py'])

    def test_baseline_trace_failure_reaches_next_proposal(self):
        engine = self.engine()
        script = self.root / 'trace_coder.py'
        script.write_text("import json, runpy\nfrom pathlib import Path\n"
                          "if Path('solution.py').exists():\n"
                          "    print(json.dumps({'type':'give_up'}))\n"
                          "else:\n"
                          f"    runpy.run_path({str(SOURCE / 'examples/demo_coder.py')!r}, run_name='__main__')\n")
        engine.command = [sys.executable, str(script)]
        record = self.run_engine(engine, 1)[0]
        self.assertTrue(record['accepted'])
        evidence = json.loads((Path(record['audit']) / 'evidence.json').read_text())
        self.assertTrue(any(row.get('kind') == 'trace_failure' and
                            'give_up' in row['trace']['failures'] for row in evidence['failures']))

    def test_trace_events_and_unknown_usage(self):
        from kernel.search import inspect_trace
        result = inspect_trace('\n'.join(json.dumps(x) for x in [
            {'type': 'tool_call', 'name': 'read', 'arguments': {'path': 'x'}},
            {'type': 'tool_call', 'name': 'read', 'arguments': {'path': 'x'}},
            {'type': 'tool_call', 'name': 'read', 'arguments': {'path': 'x'}},
            {'type': 'tool_error'}, {'type': 'give_up'},
            {'type': 'usage', 'input_tokens': 10, 'output_tokens': 5, 'cost_usd': .02}]))
        self.assertEqual(result['tokens'], 15)
        self.assertEqual(result['cost_usd'], .02)
        self.assertEqual(set(result['failures']), {'repeated_tool_call', 'tool_error', 'give_up'})
        self.assertIsNone(inspect_trace('plain output')['tokens'])

    def test_call_budget_survives_restart(self):
        engine = self.engine(max_calls=1)
        engine.initialize()
        with self.assertRaisesRegex(RuntimeError, 'budget'):
            self.run_engine(engine, 1)
        restart = self.engine(max_calls=1)
        with self.assertRaisesRegex(RuntimeError, 'budget'):
            self.run_engine(restart, 1)
        self.assertEqual(json.loads((engine.root / 'usage.json').read_text())['calls'], 1)
        self.assertEqual(engine.state()['round'], 0)

    def test_missing_token_usage_blocks_next_call(self):
        engine = self.engine(max_tokens=100)
        engine.command = [sys.executable, "-c", "print('no usage report')"]
        engine.initialize()
        with self.assertRaisesRegex(RuntimeError, 'usage'):
            self.run_engine(engine, 1)
        self.assertEqual(engine.state()['round'], 0)

    def test_full_history_and_diffs_reach_proposer(self):
        engine = self.engine()
        engine.initialize()
        for i in range(10):
            audit = self.root / f'audit-{i}'
            audit.mkdir()
            (audit / 'changes.patch').write_text(f'patch number {i}')
            engine.event(type='round', round=i, accepted=False, audit=str(audit), changed=['agent/solver.py'])
        evidence = engine.search_evidence()
        self.assertEqual(len(evidence['history']), 11)
        self.assertEqual(evidence['history'][1]['patch'], 'patch number 0')
        self.assertEqual(evidence['search']['target'], 'evolution')

    def test_manifest_required_before_scoring(self):
        engine = self.engine()
        engine.initialize()
        script = self.root / 'coder.py'
        script.write_text("from pathlib import Path\np=Path('agent/solver.py')\n"
                          "if p.exists(): p.write_text(p.read_text()+'\\n# changed\\n')\n")
        engine.command = [sys.executable, str(script)]
        record = self.run_engine(engine, 1)[0]
        self.assertFalse(record['accepted'])
        self.assertIn('proposal', record['reason'])
        self.assertNotIn('candidate', record)

    def test_independent_reviewer_veto(self):
        engine = self.engine(reviewer=[sys.executable, '-c',
                                     'import json; print(json.dumps({"approved":False,"reason":"unsupported claim"}))'])
        record = self.run_engine(engine, 1)[0]
        self.assertFalse(record['accepted'])
        self.assertIn('unsupported claim', record['reason'])
        self.assertNotIn('candidate', record)

    def test_edit_budget_rejects_large_fixture(self):
        engine = self.engine(edit_budget=1, min_edit_budget=1)
        record = self.run_engine(engine, 1)[0]
        self.assertFalse(record['accepted'])
        self.assertIn('edit budget', record['reason'])

    def test_each_candidate_can_use_git_worktree(self):
        engine = self.engine(worktrees=True)
        record = self.run_engine(engine, 1)[0]
        self.assertTrue(record['accepted'])
        self.assertEqual(record['workspace'], 'git-worktree')
        self.assertFalse((Path(record['audit']) / 'candidate/.git').exists())
        self.assertEqual(list(engine.root.glob('worktree-*')), [])

    def test_pruning_accepts_measured_preservation_with_less_code(self):
        engine = self.engine(prune_every=1)
        script = self.root / 'prune.py'
        script.write_text("""import json
from pathlib import Path
p=Path('agent/solver.py')
if p.exists():
    text=p.read_text()
    end=text.index(chr(34)*3, 3)+3
    p.write_text(text[end:].lstrip())
    Path('.rsi-proposal.json').write_text(json.dumps({
        'analysis':'Remove redundant module documentation for fixture only',
        'changes':[{'files':['agent/solver.py'], 'failure_id':'complexity',
                    'cause':'Redundant source text', 'expected_effect':'Less source, same outputs',
                    'validation':'Repeated paired evaluations'}]}))
print(json.dumps({'type':'usage','input_tokens':0,'output_tokens':0,'cost_usd':0}))
""")
        engine.command = [sys.executable, str(script)]
        record = self.run_engine(engine, 1)[0]
        self.assertTrue(record['accepted'])
        self.assertEqual(record['candidate'], record['baseline'])
        self.assertTrue(record['search']['prune'])

    def test_exploration_and_pruning_are_enforced(self):
        from kernel.search import review_proposal
        proposal = {'analysis':'evidence', 'changes':[{
            'files':['agent/solver.py'], 'failure_id':'failure-1', 'cause':'cause',
            'expected_effect':'effect', 'validation':'tests'}]}
        evidence = {'failures':[{'id':'failure-1'}],
                    'search':{'edit_budget':20, 'target':'evolution', 'prune':False}}
        stats = {'files':['agent/solver.py'], 'lines':2, 'removed':1, 'added':1}
        with self.assertRaisesRegex(ValueError, 'exploring'):
            review_proposal(proposal, stats, evidence)
        evidence['search'].update(target=None, prune=True)
        with self.assertRaisesRegex(ValueError, 'net deletion'):
            review_proposal(proposal, stats, evidence)

    def test_reported_token_overrun_does_not_activate_candidate(self):
        engine = self.engine(max_tokens=10)
        engine.command = [sys.executable, '-c',
                          "import json; print(json.dumps(dict(type='usage', input_tokens=10, output_tokens=1)))"]
        engine.initialize()
        before = engine.state()
        with self.assertRaisesRegex(RuntimeError, 'budget exceeded'):
            self.run_engine(engine, 1)
        self.assertEqual(engine.state(), before)
        usage = json.loads((engine.root / 'usage.json').read_text())
        self.assertEqual(usage['tokens'], 11)

    def test_trials_and_proposal_are_audited(self):
        engine = self.engine(trials=2)
        record = self.run_engine(engine, 1)[0]
        self.assertTrue(record['accepted'])
        self.assertEqual(len(record['trials']['candidate']), 2)
        audit = Path(record['audit'])
        proposal = json.loads((audit / 'proposal.json').read_text())
        self.assertEqual(proposal['changes'][0]['files'], ['agent/solver.py'])
        self.assertTrue(json.loads((audit / 'review.json').read_text())['approved'])


if __name__ == '__main__':
    unittest.main()
